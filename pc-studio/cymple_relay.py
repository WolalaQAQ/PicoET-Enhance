"""Relay PICO 4 Pro LAN eye streams (PTJ1) to Cymple's native UDP ingress."""
import argparse
from collections import deque
import io
import json
import os
from pathlib import Path
import socket
import struct
import threading
import time

from PIL import Image

from discovery import discover_cameras
import wire

CYMPLE_UDP_HOST = '127.0.0.1'
CYMPLE_UDP_PORT = 23303
DEFAULT_LEFT_PORT = 45682
DEFAULT_RIGHT_PORT = 45683
MSG_TYPE_NATIVE_FRAME = 1
MAX_CYMPLE_FRAME_BYTES = 0xFFFF
CHUNK_BYTES = 1400
PKT_HEADER = struct.Struct('<HHIIBBH')
VALID_ROTATIONS = (0, 90, 180, 270)

DEFAULT_RELAY_CONFIG = dict(
    cymple_host=CYMPLE_UDP_HOST,
    cymple_port=CYMPLE_UDP_PORT,
    left_port=DEFAULT_LEFT_PORT,
    right_port=DEFAULT_RIGHT_PORT,
    left_rotate=0,
    left_hflip=False,
    left_vflip=False,
    right_rotate=0,
    right_hflip=False,
    right_vflip=False,
)

_ROTATE_CW = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}

_PORT_KEYS = ('cymple_port', 'left_port', 'right_port')
_ROTATE_KEYS = ('left_rotate', 'right_rotate')
_BOOL_KEYS = ('left_hflip', 'left_vflip', 'right_hflip', 'right_vflip')
_FPS_WINDOW_NS = 1_500_000_000


def encode_cymple_packets(jpeg: bytes, channel: int, frame_seq: int) -> list[bytes]:
    """Pack one JPEG frame into type-1 UDP chunk datagrams matching ProtocolIngress::decodeMsg."""
    total = len(jpeg)
    if not 0 < total <= MAX_CYMPLE_FRAME_BYTES:
        raise ValueError(f'JPEG size {total} outside [1, {MAX_CYMPLE_FRAME_BYTES}]')
    seq_byte = frame_seq & 0xFF
    packets = []
    for offset in range(0, total, CHUNK_BYTES):
        chunk = jpeg[offset:offset + CHUNK_BYTES]
        # ProtocolIngress checks len(datagram) == payload_len + 4 (header after first 4 bytes is 12B)
        header = PKT_HEADER.pack(
            MSG_TYPE_NATIVE_FRAME,
            12 + len(chunk),
            offset,
            total,
            channel,
            seq_byte,
            0,
        )
        packets.append(header + chunk)
    return packets


def prepare_jpeg(jpeg: bytes, rotate_cw: int = 0, hflip: bool = False, vflip: bool = False) -> bytes:
    """Apply optional orientation transforms and enforce Cymple's 65535-byte frame ceiling."""
    if rotate_cw not in VALID_ROTATIONS:
        raise ValueError(f'Rotation must be one of {VALID_ROTATIONS}, got {rotate_cw!r}')
    needs_edit = rotate_cw != 0 or hflip or vflip
    if not needs_edit and len(jpeg) <= MAX_CYMPLE_FRAME_BYTES:
        return jpeg

    img = Image.open(io.BytesIO(jpeg))
    img.load()
    if img.mode != 'L':
        img = img.convert('L')
    if rotate_cw:
        img = img.transpose(_ROTATE_CW[rotate_cw])
    if hflip:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if vflip:
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)

    for quality in (92, 85, 75, 65, 50, 35):
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality)
        out = buf.getvalue()
        if len(out) <= MAX_CYMPLE_FRAME_BYTES:
            return out
    raise ValueError(f'Re-encoded JPEG still exceeds {MAX_CYMPLE_FRAME_BYTES} bytes')


def validate_relay_config(base: dict, changes: dict) -> dict:
    """Validate partial relay configuration changes against defaults and bounds."""
    unknown = set(changes) - set(DEFAULT_RELAY_CONFIG)
    if unknown:
        raise ValueError(f'Invalid Cymple relay settings: unknown field {", ".join(sorted(unknown))}')
    updated = dict(base, **changes)

    host = updated.get('cymple_host')
    if not isinstance(host, str) or not host.strip():
        raise ValueError('Invalid Cymple relay settings: cymple_host must be a non-empty string')
    updated['cymple_host'] = host.strip()

    for key in _PORT_KEYS:
        val = updated.get(key)
        if isinstance(val, bool) or not isinstance(val, int) or not 1 <= val <= 65535:
            raise ValueError(f'Invalid Cymple relay settings: {key} must be an integer in 1..65535')

    for key in _ROTATE_KEYS:
        val = updated.get(key)
        if isinstance(val, bool) or not isinstance(val, int) or val not in VALID_ROTATIONS:
            raise ValueError(f'Invalid Cymple relay settings: {key} must be one of {VALID_ROTATIONS}')

    for key in _BOOL_KEYS:
        val = updated.get(key)
        if not isinstance(val, bool):
            raise ValueError(f'Invalid Cymple relay settings: {key} must be a boolean')

    return updated


class CympleEyeRelay:
    """Background dual-eye PTJ1-to-Cymple UDP relay managed by the workbench backend."""

    def __init__(self, config_path=None, *, runtime=None, discover=discover_cameras):
        self.config_path = Path(config_path) if config_path is not None else None
        self.runtime = runtime
        self.discover = discover
        self.config = dict(DEFAULT_RELAY_CONFIG)
        if self.config_path is not None and self.config_path.exists():
            try:
                saved = json.loads(self.config_path.read_text(encoding='utf-8'))
                if isinstance(saved, dict):
                    self.config = validate_relay_config(
                        DEFAULT_RELAY_CONFIG,
                        {k: saved[k] for k in DEFAULT_RELAY_CONFIG if k in saved},
                    )
            except (OSError, ValueError):
                pass
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.running = False
        self._starting = False
        self._generation = 0
        self.error = ''
        self.source_host = ''
        self._active_left_port = None
        self._active_right_port = None
        self._threads = []
        self._sockets = {}
        self._pending_sockets = set()
        self._eyes = self._make_eyes(self.config['left_port'], self.config['right_port'])

    @staticmethod
    def _make_eyes(left_port: int, right_port: int, initial_state: str = 'idle') -> dict:
        return {
            name: {
                'channel': channel,
                'port': port,
                'state': initial_state,
                'connected': False,
                'frames': 0,
                'timestamps': deque(maxlen=180),
                'sizes': deque(maxlen=180),
                'error': None,
            }
            for name, channel, port in (('eye_left', 0, left_port), ('eye_right', 1, right_port))
        }

    def _save_config(self):
        if self.config_path is None:
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_name(f'{self.config_path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
        tmp.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding='utf-8')
        try:
            for attempt in range(6):
                try:
                    os.replace(tmp, self.config_path)
                    return
                except PermissionError as exc:
                    if getattr(exc, 'winerror', None) not in (5, 32) or attempt == 5:
                        raise
                    time.sleep(0.025)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def configure(self, changes: dict) -> dict:
        """Persist validated relay settings while stopped."""
        with self.lock:
            if self.running or self._starting:
                raise ValueError('Stop the Cymple eye relay before changing relay settings')
            self.config = validate_relay_config(self.config, changes)
            self._eyes['eye_left']['port'] = self.config['left_port']
            self._eyes['eye_right']['port'] = self.config['right_port']
            self._save_config()
            return self.status()

    def _resolve_endpoint(self, explicit_host: str, cfg: dict) -> tuple[str, int, int]:
        if explicit_host:
            return explicit_host, cfg['left_port'], cfg['right_port']
        found = self.discover(timeout=1.5)
        devices = found.get('devices', [])
        if not devices:
            raise ValueError('No PICO camera service found; enable LAN cameras on the headset or select a headset first')
        if len(devices) > 1:
            raise ValueError('Multiple PICO camera services found; select one headset first')
        device = devices[0]
        streams = {s['id']: int(s['port']) for s in device.get('streams', [])}
        if 'eye_left' not in streams or 'eye_right' not in streams:
            raise ValueError('No PICO camera service found; enable LAN cameras on the headset or select a headset first')
        return device['host'], streams['eye_left'], streams['eye_right']

    def start(self, options: dict | None = None) -> dict:
        """Start dual-eye forwarding threads using explicit or auto-discovered endpoints."""
        opts = dict(options or {})
        relay_changes = {k: opts[k] for k in DEFAULT_RELAY_CONFIG if k in opts}
        with self.lock:
            if self.running or self._starting:
                raise ValueError('Cymple eye relay is already running')
            updated_cfg = validate_relay_config(self.config, relay_changes)
            self.config = updated_cfg
            if not self.running:
                self._eyes['eye_left']['port'] = updated_cfg['left_port']
                self._eyes['eye_right']['port'] = updated_cfg['right_port']
            self._save_config()
            self._starting = True
            generation = self._generation

        if 'headset_host' in opts:
            explicit_host = str(opts['headset_host'] or '').strip()
        else:
            explicit_host = str(self.runtime.config.get('headset_host', '') if self.runtime else '').strip()

        try:
            src_host, left_port, right_port = self._resolve_endpoint(explicit_host, updated_cfg)
            with self.lock:
                if generation != self._generation:
                    raise RuntimeError('Cymple eye relay startup was cancelled')
                if self.running:
                    raise ValueError('Cymple eye relay is already running')
                self.stop_event.clear()
                self.running = True
                self.error = ''
                self.source_host = src_host
                self._active_left_port = left_port
                self._active_right_port = right_port
                self._sockets.clear()
                self._pending_sockets.clear()
                self._eyes = self._make_eyes(left_port, right_port, initial_state='connecting')
                dst_addr = (updated_cfg['cymple_host'], updated_cfg['cymple_port'])
                self._threads = [
                    threading.Thread(
                        target=self._worker,
                        args=('eye_left', 0, src_host, left_port, dst_addr,
                              updated_cfg['left_rotate'], updated_cfg['left_hflip'], updated_cfg['left_vflip']),
                        name='cymple-relay-eye_left',
                        daemon=True,
                    ),
                    threading.Thread(
                        target=self._worker,
                        args=('eye_right', 1, src_host, right_port, dst_addr,
                              updated_cfg['right_rotate'], updated_cfg['right_hflip'], updated_cfg['right_vflip']),
                        name='cymple-relay-eye_right',
                        daemon=True,
                    ),
                ]
                for t in self._threads:
                    t.start()
                return self.status()
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
            raise
        finally:
            with self.lock:
                self._starting = False

    def _track_pending_socket(self, sock):
        if sock is None:
            return
        with self.lock:
            self._pending_sockets.add(sock)
            if self.stop_event.is_set():
                try:
                    sock.close()
                except OSError:
                    pass

    def _untrack_pending_socket(self, sock):
        if sock is None:
            return
        with self.lock:
            self._pending_sockets.discard(sock)

    def _connect(self, host: str, port: int, timeout: float = 4):
        holder = [None]

        def on_socket(sock):
            if sock is not None:
                holder[0] = sock
                self._track_pending_socket(sock)
            elif holder[0] is not None:
                self._untrack_pending_socket(holder[0])
                holder[0] = None

        return wire.connect_cancellable(
            host, port, timeout=timeout, stop_event=self.stop_event,
            on_socket=on_socket, keep_tracked=True,
        )

    def _worker(
        self,
        name: str,
        channel: int,
        src_host: str,
        src_port: int,
        dst_addr: tuple[str, int],
        rotate_cw: int,
        hflip: bool,
        vflip: bool,
    ):
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        seq = 0
        backoff = 0.5
        try:
            while not self.stop_event.is_set():
                tcp_sock = None
                wait_after = False
                with self.lock:
                    eye = self._eyes[name]
                    if eye['frames'] == 0 and not eye['error']:
                        eye['state'] = 'connecting'
                try:
                    tcp_sock = self._connect(src_host, src_port, timeout=4)
                    tcp_sock.settimeout(4)
                    try:
                        tcp_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    except (OSError, AttributeError):
                        pass
                    wire.enable_keepalive(tcp_sock)
                    with self.lock:
                        if self.stop_event.is_set():
                            break
                        self._sockets[name] = tcp_sock
                        self._pending_sockets.discard(tcp_sock)
                        eye = self._eyes[name]
                        eye['connected'] = True
                        eye['error'] = None
                        if eye['state'] == 'reconnecting':
                            eye['state'] = 'connecting'
                    tcp_sock.sendall(b'JPEG\n')

                    while not self.stop_event.is_set():
                        try:
                            header, payload = wire.read_frame(tcp_sock)
                        except wire.IdleStreamTimeout:
                            continue
                        if header.flags & wire.END:
                            with self.lock:
                                if not self.stop_event.is_set():
                                    eye['connected'] = False
                                    eye['state'] = 'reconnecting'
                            wait_after = True
                            break
                        if not (header.flags & wire.JPEG):
                            raise ValueError('Cymple relay received a non-JPEG frame')
                        jpeg = prepare_jpeg(payload, rotate_cw, hflip, vflip)
                        for pkt in encode_cymple_packets(jpeg, channel, seq):
                            udp_sock.sendto(pkt, dst_addr)
                        seq = (seq + 1) & 0xFF
                        backoff = 0.5
                        now_ns = time.perf_counter_ns()
                        with self.lock:
                            eye['state'] = 'streaming'
                            eye['connected'] = True
                            eye['error'] = None
                            eye['frames'] += 1
                            eye['timestamps'].append(now_ns)
                            eye['sizes'].append(len(jpeg))
                except (OSError, EOFError, ValueError) as exc:
                    if self.stop_event.is_set():
                        break
                    with self.lock:
                        eye = self._eyes[name]
                        eye['connected'] = False
                        eye['state'] = 'reconnecting'
                        eye['error'] = f'{type(exc).__name__}: {exc}'
                    wait_after = True
                finally:
                    with self.lock:
                        if tcp_sock is not None and self._sockets.get(name) is tcp_sock:
                            self._sockets.pop(name, None)
                        if tcp_sock is not None:
                            self._pending_sockets.discard(tcp_sock)
                    if tcp_sock is not None:
                        try:
                            tcp_sock.close()
                        except OSError:
                            pass
                if wait_after and not self.stop_event.is_set():
                    self.stop_event.wait(backoff)
                    backoff = min(backoff * 2.0, 5.0)
        finally:
            udp_sock.close()

    def stop(self) -> dict:
        """Signal workers to stop, interrupt active sockets, and wait for clean exit."""
        self.stop_event.set()
        with self.lock:
            self._generation += 1
            threads = list(self._threads)
            sockets = list(self._sockets.values()) + list(self._pending_sockets)
            self._pending_sockets.clear()
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        for t in threads:
            if t is not threading.current_thread():
                t.join(timeout=5)
        with self.lock:
            self._threads = []
            self._sockets.clear()
            self._pending_sockets.clear()
            self.running = False
            self.error = ''
            self.source_host = ''
            self._active_left_port = None
            self._active_right_port = None
            for key, eye in self._eyes.items():
                eye['port'] = self.config['left_port'] if key == 'eye_left' else self.config['right_port']
                eye['state'] = 'idle'
                eye['connected'] = False
                eye['error'] = None
            return self.status()

    def close(self):
        self.stop()

    def status(self) -> dict:
        """Return a thread-safe snapshot of relay configuration and per-eye throughput."""
        now_ns = time.perf_counter_ns()
        cutoff_ns = now_ns - _FPS_WINDOW_NS
        with self.lock:
            eyes_out = {}
            eye_states = []
            for key, eye in self._eyes.items():
                recent = [ts for ts in eye['timestamps'] if ts >= cutoff_ns]
                fps = (
                    (len(recent) - 1) * 1e9 / (recent[-1] - recent[0])
                    if len(recent) >= 2 and recent[-1] > recent[0]
                    else 0.0
                )
                sizes = eye['sizes']
                avg_kb = (sum(sizes) / len(sizes)) / 1024.0 if sizes else 0.0
                eye_states.append(eye['state'])
                eyes_out[key] = {
                    'channel': eye['channel'],
                    'port': eye['port'],
                    'state': eye['state'],
                    'connected': eye['connected'],
                    'fps': fps,
                    'frames': eye['frames'],
                    'avg_kb': avg_kb,
                    'error': eye['error'],
                }

            if not self.running:
                overall_state = 'idle'
            elif 'reconnecting' in eye_states:
                overall_state = 'reconnecting'
            elif all(s == 'streaming' for s in eye_states):
                overall_state = 'running'
            else:
                overall_state = 'connecting'

            return {
                'running': self.running,
                'state': overall_state,
                'source_host': self.source_host,
                'left_port': self._active_left_port if self._active_left_port is not None else self.config['left_port'],
                'right_port': self._active_right_port if self._active_right_port is not None else self.config['right_port'],
                'cymple_host': self.config['cymple_host'],
                'cymple_port': self.config['cymple_port'],
                'config': dict(self.config),
                'error': self.error,
                'eyes': eyes_out,
            }


def main(argv=None):
    """CLI entrypoint preserving standalone cymple-eye-relay.py usage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', help='Headset IP (or 127.0.0.1 over adb forward); auto-discovered if omitted')
    parser.add_argument('--left-port', type=int, default=DEFAULT_LEFT_PORT, help='Left eye PTJ1 port (default 45682)')
    parser.add_argument('--right-port', type=int, default=DEFAULT_RIGHT_PORT, help='Right eye PTJ1 port (default 45683)')
    parser.add_argument('--discover-timeout', type=float, default=1.5)
    parser.add_argument('--cymple-host', default=CYMPLE_UDP_HOST)
    parser.add_argument('--cymple-port', type=int, default=CYMPLE_UDP_PORT)
    parser.add_argument('--left-rotate', type=int, choices=VALID_ROTATIONS, default=0, help='Left eye CW rotation')
    parser.add_argument('--right-rotate', type=int, choices=VALID_ROTATIONS, default=0, help='Right eye CW rotation')
    parser.add_argument('--left-hflip', action='store_true', help='Left eye horizontal flip')
    parser.add_argument('--right-hflip', action='store_true', help='Right eye horizontal flip')
    parser.add_argument('--left-vflip', action='store_true', help='Left eye vertical flip')
    parser.add_argument('--right-vflip', action='store_true', help='Right eye vertical flip')
    args = parser.parse_args(argv)

    def _discover_first(timeout=args.discover_timeout):
        found = discover_cameras(timeout=timeout)
        devices = found.get('devices', [])
        if not devices:
            raise RuntimeError(
                'No PICO camera broadcast found; enable LAN cameras on headset or pass --host <IP>'
            )
        return {'devices': [devices[0]]}

    relay = CympleEyeRelay(discover=_discover_first)
    options = dict(
        cymple_host=args.cymple_host,
        cymple_port=args.cymple_port,
        left_port=args.left_port,
        right_port=args.right_port,
        left_rotate=args.left_rotate,
        right_rotate=args.right_rotate,
        left_hflip=args.left_hflip,
        right_hflip=args.right_hflip,
        left_vflip=args.left_vflip,
        right_vflip=args.right_vflip,
    )
    if args.host:
        options['headset_host'] = args.host
    initial = relay.start(options)
    print(
        f"Relaying P4P {initial['source_host']} ({initial['left_port']}/{initial['right_port']}) "
        f"-> Cymple native UDP {initial['cymple_host']}:{initial['cymple_port']}. Ctrl+C to stop."
    )
    try:
        while True:
            time.sleep(5.0)
            st = relay.status()
            parts = []
            for key in ('eye_left', 'eye_right'):
                e = st['eyes'][key]
                parts.append(f"{key}: {e['fps']:.1f} fps ({e['avg_kb']:.1f} KB/f, total {e['frames']}, {e['state']})")
            print(' | '.join(parts))
    except KeyboardInterrupt:
        relay.stop()


if __name__ == '__main__':
    main()
