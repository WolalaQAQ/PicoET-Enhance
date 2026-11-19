"""File bridge for the existing worn runner and a camera-free, display-only browser."""
import json
import logging
import os
from pathlib import Path
import time
import uuid
from contextlib import contextmanager


@contextmanager
def file_transaction(path):
    """Serialize Windows readers/replacers across the runner and HTTP process."""
    if os.name != 'nt':
        yield
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    name = 'Local\\PicoMouthGuide-'+uuid.uuid5(uuid.NAMESPACE_URL, str(Path(path).resolve()).lower()).hex
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        result = kernel.WaitForSingleObject(handle, 0xffffffff)
        if result not in (0, 0x80):  # Includes an abandoned owner; its file was atomically published.
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = True
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


@contextmanager
def open_shared(path):
    with file_transaction(path), Path(path).open(encoding='utf-8') as stream:
        yield stream


def read_json(path):
    with open_shared(path) as stream:
        return json.load(stream)


def atomic_json(path, value):
    path = Path(path)
    with file_transaction(path):
        temp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
        temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        deadline, retries = time.monotonic()+.05, 0
        while True:
            try:
                os.replace(temp, path)
                if retries:
                    logging.warning('Visual file replacement recovered after %d retries: %s', retries, path.name)
                return
            except PermissionError as error:
                if os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32) or time.monotonic() >= deadline:
                    raise
                retries += 1
                time.sleep(.005)


class GuideStore:
    def __init__(self, directory, clock=time.monotonic):
        self.directory = Path(directory)
        self.clock = clock
        self.path = self.directory/'state.json'

    def initialize(self, case):
        assert not self.path.exists(), 'Keep an existing viewer session; do not overwrite it'
        self.directory.mkdir(parents=True, exist_ok=True)
        config = read_json(case/'round.json')
        steps = read_json(case/'actions.json')
        state = dict(case=case.name, role=config['role'], steps=steps, index=0,
                     phase='practice', running=False, frame=uuid.uuid4().hex,
                     updated_pc_s=self.clock(), training_label=False, camera_started=False)
        atomic_json(self.path, state)
        return state

    def status(self):
        state = read_json(self.path)
        if state['running'] and self.clock()-state['updated_pc_s'] > 3.:
            state['phase'] = 'interrupted'
        return state

    def publish(self, **changes):
        state = read_json(self.path)
        state.update(changes, updated_pc_s=self.clock())
        atomic_json(self.path, state)
        return state

    def receipt(self, body):
        if body.get('viewer') not in ('headset', 'desktop', 'test'):
            raise ValueError('Unknown viewer')
        state = self.status()
        if body.get('frame') != state['frame'] or body.get('index') != state['index']:
            raise ValueError('Obsolete display receipt')
        value = dict(viewer=body['viewer'], frame=state['frame'], index=state['index'],
                     visible=body.get('visible') is True, media_ready=body.get('media_ready') is True,
                     received_pc_s=self.clock())
        atomic_json(self.directory/('receipt-'+body['viewer']+'.json'), value)
        return value

    def ready(self, viewer='headset', max_age=1.):
        path = self.directory/('receipt-'+viewer+'.json')
        if not path.exists():
            return False
        state, receipt = self.status(), read_json(path)
        return (state['phase'] != 'interrupted' and receipt['frame'] == state['frame']
                and receipt['index'] == state['index'] and receipt['visible']
                and receipt['media_ready'] and 0 <= self.clock()-receipt['received_pc_s'] < max_age)


class VisualSequence:
    """Nonblocking display/mimic stages; speech and original holds remain in StepGuide."""
    def __init__(self, config, log, clock=time.monotonic):
        self.config, self.log, self.clock = config, log, clock
        self.store = GuideStore(config['state'], clock)
        self.phase = 'next'
        self.deadline = 0
        self.displayed_at = None
        self.last_heartbeat = 0

    def preflight(self, case):
        state = self.store.status()
        if state['case'] != case.name or state['running'] or not self.store.ready(self.config['viewer']):
            raise RuntimeError('Headset viewer must visibly show the first practice step before capture')
        if state['index'] != 0:
            raise RuntimeError('Return to the first practice step')

    def tick(self, index):
        now = self.clock()
        if self.phase == 'next':
            self.store.publish(index=index, phase='display', running=True, frame=uuid.uuid4().hex)
            self.deadline = now+self.config['display_timeout_seconds']
            self.phase = 'display'
            self.log(dict(event='visual_display_requested', index=index, pc_s=now, training_label=False))
        else:
            self.store.publish()
        if self.phase == 'display':
            if self.store.ready(self.config['viewer'], self.config['heartbeat_seconds']):
                self.displayed_at = now
                self.deadline = now+self.config['imitate_seconds']
                self.phase = 'imitate'
                self.store.publish(phase='imitate')
                self.log(dict(event='visual_display_ready', index=index, pc_s=now, training_label=False))
            elif now >= self.deadline:
                raise RuntimeError('Current demonstration was not displayed before its deadline')
            return False
        if not self.store.ready(self.config['viewer'], self.config['heartbeat_seconds']):
            raise RuntimeError('Headset viewer lost visibility, media or heartbeat')
        return self.phase == 'imitate' and now >= self.deadline

    def hold(self, until):
        self.phase = 'hold'
        self.store.publish(phase='hold', hold_until_pc_s=until)

    def next(self):
        self.phase = 'next'

    def stop(self, complete=False):
        self.phase = 'done' if complete else 'paused'
        self.store.publish(phase='done' if complete else 'paused', running=False)


def load_visual(case):
    path = Path(case)/'round.json'
    return read_json(path).get('visual_guidance') if path.is_file() else None
