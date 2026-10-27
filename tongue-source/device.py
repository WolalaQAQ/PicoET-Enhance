"""Explicit developer start/stop for an owned camera/JPEG session over rooted ADB."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parent


def birth(stat):
    return int(stat.rsplit(')', 1)[1].split()[19])


def wait_for(read, accept, description, seconds=7):
    deadline = time.monotonic()+seconds
    while True:
        value = read()
        if accept(value):
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(description+': '+json.dumps(value))
        time.sleep(.1)


class Device:
    def __init__(self, adb, serial):
        self.prefix = [str(adb), '-s', serial]

    def adb(self, *args):
        return subprocess.run([*self.prefix, *map(str, args)], check=True, capture_output=True,
                              timeout=30).stdout.decode('utf-8')

    def root(self, script):
        result = subprocess.run([*self.prefix, 'shell', '-T', 'su', '-c', 'sh'],
                                input=('set -eu\n'+script+'\n').encode('utf-8'),
                                capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError((result.stdout+result.stderr).decode('utf-8'))
        return result.stdout.decode('utf-8')

    def read_json(self, stage, name):
        path = shlex.quote(stage+'/'+name)
        text = self.root(f'if [ -f {path} ]; then cat {path}; fi')
        return json.loads(text) if text else None


class Session:
    def __init__(self, device, state_file, serial):
        self.device, self.file, self.serial = device, Path(state_file), serial
        self.state = json.loads(self.file.read_text(encoding='utf-8')) if self.file.exists() else None

    def save(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(self.state, indent=2), encoding='utf-8')

    def verify_service(self):
        s = self.state
        if s['serial'] != self.serial or self.device.root('cat /proc/sys/kernel/random/boot_id').strip() != s['boot']:
            raise RuntimeError('This session belongs to another device or boot; no command was sent')
        stat = self.device.root(f"cat /proc/{int(s['pid'])}/stat")
        if birth(stat) != s['birth']:
            raise RuntimeError('Tracking service instance changed; no command was sent')

    def status(self):
        self.verify_service()
        return {name: self.device.read_json(self.state['stage'], name+'.json') for name in
                ('camera-status', 'ipc-shim-status', 'ipc-daemon-status')}

    def command(self, verb):
        self.verify_service()
        stage = self.state['stage']
        current = self.device.read_json(stage, 'camera-status.json')
        if (current['pid'], current['start_ticks'], current['owner_id']) != (
                self.state['pid'], self.state['birth'], self.state['owner']):
            raise RuntimeError('Camera owner changed; no command was sent')
        command_id = current['command_id']+1
        text = f"{current['owner_id']} {command_id} {verb}\n"
        self.device.root(f'printf %s {shlex.quote(text)} > {stage}/camera-control.pending\n'
                         f'mv {stage}/camera-control.pending {stage}/camera-control.txt')
        value = wait_for(lambda: self.device.read_json(stage, 'camera-status.json'),
                         lambda v: v and v['command_id'] == command_id, 'Camera command timed out')
        if value['command_result']:
            raise RuntimeError('Camera command failed: '+json.dumps(value))
        return value

    def start(self, build):
        build = Path(build)
        names = ['libptj_source.so', 'libpicoet_camera_jpeg.so', 'ptj_ipc_daemon', 'picoet-inject']
        for name in names:
            if not (build/name).is_file():
                raise FileNotFoundError(build/name)
        if self.state:
            self.verify_service()
            if not self.state.get('stopped'):
                raise RuntimeError('Existing session is not stopped; use status/stop with the same --state file')
            self.check_stopped()
            stage = self.state['stage']
            self.device.root(f'rm {stage}/video-channel.bin')
        else:
            pid = int(self.device.root('pidof pxreyetrackingservice').strip())
            stamp = birth(self.device.root(f'cat /proc/{pid}/stat'))
            maps = self.device.root(f'cat /proc/{pid}/maps')
            if 'libpicoet_camera_jpeg.so' in maps:
                raise RuntimeError('A camera adapter is already resident; use its original --state file')
            tracking = self.device.root('dumpsys pxreyetrackingservice')
            if ('FT algorithm is not started' not in tracking or 'ET algorithm is not started' not in tracking or
                    any('clients' in line and line.strip() != 'There is 0 clients' for line in tracking.splitlines() if 'There is ' in line)):
                raise RuntimeError('Stop all eye/face tracking clients before injecting the idle adapter')
            stage = '/data/local/tmp/picoet-tongue-'+uuid.uuid4().hex
            self.state = dict(serial=self.serial, boot=self.device.root('cat /proc/sys/kernel/random/boot_id').strip(),
                              pid=pid, birth=stamp, stage=stage, stopped=False)
            self.save()  # Preserve the recovery handle before uploading or injecting anything.
            self.device.adb('shell', 'mkdir', '-m', '700', stage)
            for name in names:
                self.device.adb('push', build/name, stage+'/'+name)
            self.device.root(f'chown -R 0:0 {stage}\nchmod 700 {stage} {stage}/picoet-inject {stage}/ptj_ipc_daemon\n'
                             f'chmod 644 {stage}/*.so\nchcon u:object_r:system_file:s0 {stage}/*.so')
            script = (f'#!/system/bin/sh\nset -eu\n[ "${{PICOET_CONTROL_LOCKED:-}}" = 1 ]\n'
                      f'[ "$(pidof pxreyetrackingservice)" = {pid} ]\n'
                      f'dumpsys pxreyetrackingservice > {stage}/before-tracking.txt\n'
                      f'grep -q "FT algorithm is not started" {stage}/before-tracking.txt\n'
                      f'grep -q "ET algorithm is not started" {stage}/before-tracking.txt\n'
                      f'if grep -qE "There is [1-9][0-9]* clients" {stage}/before-tracking.txt; then exit 3; fi\n'
                      f'exec {stage}/picoet-inject {pid} {stage}/libpicoet_camera_jpeg.so\n')
            self.device.root(f'printf %s {shlex.quote(script)} > {stage}/inject.sh\nchmod 700 {stage}/inject.sh\n'
                             f'{stage}/picoet-inject --run-locked /data/adb/picoet-enhance/control.lock {stage}/inject.sh apply')
            camera = wait_for(lambda: self.device.read_json(stage, 'camera-status.json'), bool, 'No idle camera status')
            if (camera['pid'], camera['start_ticks'], camera['state'], camera['hook_status']) != (pid, stamp, 'idle', 0):
                raise RuntimeError('Unexpected idle camera owner: '+json.dumps(camera))
            self.state['owner'] = camera['owner_id']
            self.save()
        self.state['stopped'] = False
        self.save()
        self.device.root(f'nohup {stage}/ptj_ipc_daemon {stage} 45681 >{stage}/daemon.stdout.txt '
                         f'2>{stage}/daemon.stderr.txt </dev/null &')
        daemon = wait_for(lambda: self.device.read_json(stage, 'ipc-daemon-status.json'),
                          lambda v: v and v['state'] == 1, 'Daemon not ready')
        self.state['daemon'] = {k: daemon[k] for k in ('session', 'daemon_pid', 'daemon_birth')}
        self.save()
        if birth(self.device.root(f"cat /proc/{daemon['daemon_pid']}/stat")) != daemon['daemon_birth']:
            raise RuntimeError('Daemon PID/birth mismatch')
        camera = self.command('start')
        if camera['hook_status'] != 1 or camera['camera']['armed'] != 1:
            raise RuntimeError('Camera hook is not armed: '+json.dumps(camera))
        live = self.status()
        validate_ipc(live, self.state, stopped=False)
        return live

    def check_stopped(self):
        value = self.status()
        validate_ipc(value, self.state, stopped=True)
        return value

    def stop(self):
        # No process killing, service restart, module reinstall or unmapping without ACK.
        self.command('stop')
        value = self.status()
        if value['ipc-shim-status'] is None and self.state.get('daemon'):
            # An entry guard failure occurs before shim attachment. Explicit Stop
            # may terminate our unattached daemon, not the tracking service.
            daemon = self.state['daemon']
            pid = int(daemon['daemon_pid'])
            if birth(self.device.root(f'cat /proc/{pid}/stat')) != daemon['daemon_birth']:
                raise RuntimeError('Unattached daemon instance changed; no signal was sent')
            self.device.root(f'kill -TERM {pid}')
            self.state['aborted'] = True
            self.save()
            return dict(value, recovery='Unattached daemon stopped; preserve stage; hook failure needs a fresh service instance')
        value = self.check_stopped()
        self.state['stopped'] = True
        self.save()
        return value


def validate_ipc(value, state, stopped):
    camera, shim, daemon = (value[k] for k in ('camera-status', 'ipc-shim-status', 'ipc-daemon-status'))
    identity = state['daemon']
    if not shim or not daemon or any(shim[k] != identity[k] or daemon[k] != identity[k] for k in identity):
        raise RuntimeError('IPC owner mismatch; keep the stage and state file')
    if (shim['pid'], shim['client_birth'], daemon['client_pid'], daemon['client_birth']) != (
            state['pid'], state['birth'], state['pid'], state['birth']):
        raise RuntimeError('IPC client PID/birth mismatch')
    if (daemon['attach_ack'] != 1 or daemon['attach_client_nonce'] != daemon['client_nonce'] or
            daemon['attach_root_nonce'] != daemon['root_nonce'] or daemon['attach_ack_session'] != identity['session']):
        raise RuntimeError('IPC attach nonce ACK mismatch')
    nonce_hex = lambda pair: ''.join(f'{v:016x}' for v in pair)
    if (not any(daemon['root_nonce']) or not any(daemon['client_nonce']) or
            shim['root_nonce'] != nonce_hex(daemon['root_nonce']) or
            shim['client_nonce'] != nonce_hex(daemon['client_nonce'])):
        raise RuntimeError('IPC shim nonce mismatch')
    if not stopped and (shim['state'] != 'attached' or daemon['state'] != 1 or
                        camera['video']['session_id'] != identity['session']):
        raise RuntimeError('Camera/shim/daemon are not attached to the same live session')
    if stopped and (camera['camera']['armed'] or camera['camera']['in_flight'] or
                    camera['video']['running'] or camera['video']['queue_in_use'] or
                    shim['state'] != 'stopped_acknowledged' or shim['retained_mapping'] or
                    daemon['state'] != 3 or daemon['stop_ack'] != 1 or daemon['in_use'] or
                    daemon['stop_ack_session'] != identity['session'] or
                    daemon['stop_client_nonce'] != daemon['client_nonce']):
        raise RuntimeError('Stop was not acknowledged after join; preserve the mapping and do not retry attach')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'stop', 'status'])
    parser.add_argument('--adb', default='adb')
    parser.add_argument('--serial', required=True)
    parser.add_argument('--state', type=Path, required=True, help='Private local session JSON; keep for stop/restart')
    parser.add_argument('--build', type=Path, default=ROOT/'build-android')
    args = parser.parse_args()
    session = Session(Device(args.adb, args.serial), args.state, args.serial)
    if args.action != 'start' and not session.state:
        parser.error('No saved session exists at --state')
    result = session.start(args.build) if args.action == 'start' else getattr(session, args.action)()
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
