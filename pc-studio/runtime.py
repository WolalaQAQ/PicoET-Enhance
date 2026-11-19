"""Bounded JPEG/RAW LAN camera receive and web preview runtime (no ML dependencies)."""
from collections import deque
from dataclasses import asdict
import io
import json
import math
import os
from pathlib import Path
import socket
import sys
import threading
import time

from PIL import Image, ImageDraw

from stock import CaptureContextUnavailable, read_capture_context
from updater import APP_VERSION
from wire import END, JPEG, RAW, IdleStreamTimeout, SourceOrder, enable_keepalive, read_frame, sync_clock


def _resolve_paths():
    if getattr(sys, 'frozen', False):
        bundle_root = Path(getattr(sys, '_MEIPASS', Path(sys.executable).resolve().parent))
        app_dir = Path(sys.executable).resolve().parent
        local_data = app_dir / 'user-data'
        if local_data.exists() or not os.environ.get('LOCALAPPDATA'):
            data_dir = local_data
        else:
            data_dir = Path(os.environ['LOCALAPPDATA']) / 'PicoET-PC-Studio' / 'user-data'
        return bundle_root, data_dir
    root = Path(__file__).resolve().parent
    return root, root / 'user-data'


ROOT, DATA = _resolve_paths()

CAMERAS = ('mouth', 'eye_left', 'eye_right')
DEFAULT_PORTS = {'mouth': 45681, 'eye_left': 45682, 'eye_right': 45683}
DEFAULT_CONFIG = dict(
    enabled=False,
    headset_host='',
    headset_port=45681,
    eye_left_port=45682,
    eye_right_port=45683,
    preview_camera='mouth',
    stale_ms=1000.0,
    preview_fps=10,
)


def _valid_port(value, name):
    if isinstance(value, bool):
        raise ValueError(f'{name} must be an integer in 1..65535')
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError(f'{name} must be an integer in 1..65535')
    return port


class Runtime:
    def __init__(self, config_path=None, *, run_root=None, record_input=False):
        self.config_path = Path(config_path or DATA / 'config.json')
        self.config = dict(DEFAULT_CONFIG)
        self._preserved_extra = {}
        if self.config_path.exists():
            saved = json.loads(self.config_path.read_text(encoding='utf-8'))
            if isinstance(saved, dict):
                for key, value in saved.items():
                    if key in DEFAULT_CONFIG:
                        self.config[key] = value
                    else:
                        self._preserved_extra[key] = value
        self.config['enabled'] = False  # Opening the local service never auto-starts capture.
        if self.config['preview_camera'] not in CAMERAS:
            self.config['preview_camera'] = 'mouth'
        self.run_root = Path(run_root) if run_root is not None else None
        self.record_input = bool(record_input)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.stop_event = threading.Event()
        self.running = self.connected = False
        self.mode = 'idle'
        self.last_error = None
        self.pending = None
        self.source = None
        self.order = SourceOrder()
        self.preview = None
        self.clock = None
        self.socket = None
        self.threads = []
        self.log = None
        self.run_dir = None
        self.counts = {}
        self.received_times = deque(maxlen=900)
        self.decoded_times = deque(maxlen=900)
        self.last_header = None
        self.last_preview_ns = 0
        self.inference_ready = threading.Event()
        self.lifecycle = 0
        self.output_session_id = time.time_ns()
        self.input_stream = None
        self.clock_log = None
        self.model_version = 0
        self.capture_context = None
        self._session_target = None

    def _active_port(self, config=None):
        cfg = config or self.config
        cam = cfg.get('preview_camera', 'mouth')
        if cam == 'eye_left':
            return int(cfg['eye_left_port'])
        if cam == 'eye_right':
            return int(cfg['eye_right_port'])
        return int(cfg['headset_port'])

    def configure(self, changes):
        if not isinstance(changes, dict):
            raise ValueError('Configuration payload must be a JSON object')
        unknown = set(changes) - set(DEFAULT_CONFIG)
        if unknown:
            raise ValueError('Unknown configuration fields: ' + ', '.join(sorted(unknown)))
        updated = dict(self.config, **changes)
        updated['headset_host'] = str(updated.get('headset_host') or '').strip()
        if updated['preview_camera'] not in CAMERAS:
            raise ValueError('Unknown preview camera: ' + str(updated['preview_camera']))
        for key in ('headset_port', 'eye_left_port', 'eye_right_port'):
            updated[key] = _valid_port(updated[key], key)
        stale_ms = float(updated['stale_ms'])
        if not math.isfinite(stale_ms) or stale_ms <= 0:
            raise ValueError('stale_ms must be a finite positive number')
        updated['stale_ms'] = stale_ms
        if isinstance(updated['preview_fps'], bool):
            raise ValueError('preview_fps must be 1–30')
        fps = int(updated['preview_fps'])
        if not 1 <= fps <= 30:
            raise ValueError('preview_fps must be 1–30')
        updated['preview_fps'] = fps
        updated['enabled'] = bool(updated['enabled'])

        if self.running and (
            any(updated[k] != self.config[k] for k in (
                'headset_host', 'headset_port', 'eye_left_port', 'eye_right_port', 'preview_camera'
            ))
            or not updated['enabled']
        ):
            self.stop()
            updated['enabled'] = False
        if not updated['enabled'] and self.running:
            self.stop()

        with self.lock:
            self.config = updated
            self.last_error = None
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            saved = dict(self._preserved_extra, **updated)
            self.config_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding='utf-8')
        return dict(self.config)

    def start(
        self,
        mode='capture',
        *,
        camera=None,
        headset_host=None,
        headset_port=None,
        eye_left_port=None,
        eye_right_port=None,
    ):
        if mode not in ('live', 'capture'):
            raise ValueError('mode must be live or capture')
        with self.lock:
            if self.running:
                raise ValueError('Runtime is already running')
            if camera is not None:
                if camera not in CAMERAS:
                    raise ValueError('Unknown preview camera: ' + str(camera))
                self.config['preview_camera'] = camera
            host = (
                str(headset_host).strip()
                if headset_host is not None
                else self.config['headset_host'].strip()
            )
            if not host:
                raise ValueError('Enter the headset address before starting live input')
            target = dict(
                headset_host=host,
                headset_port=_valid_port(headset_port, 'headset_port') if headset_port is not None else int(self.config['headset_port']),
                eye_left_port=_valid_port(eye_left_port, 'eye_left_port') if eye_left_port is not None else int(self.config['eye_left_port']),
                eye_right_port=_valid_port(eye_right_port, 'eye_right_port') if eye_right_port is not None else int(self.config['eye_right_port']),
                preview_camera=self.config['preview_camera'],
            )
        self.stop()
        generation = self.lifecycle
        with self.condition:
            if generation != self.lifecycle:
                raise RuntimeError('Startup was cancelled by stop/shutdown')
            self._session_target = target
            self.stop_event.clear()
            self.inference_ready.clear()
            self.running = True
            self.connected = False
            self.mode = 'live'
            self.config['enabled'] = True
            self.last_error = None
            self.pending = None
            self.source = None
            self.order = SourceOrder()
            self.preview = None
            self.clock = None
            self.received_times.clear()
            self.decoded_times.clear()
            self.last_header = None
            self.capture_context = None
            self.output_session_id = time.time_ns()
            self.last_preview_ns = 0
            self.counts = dict(
                received=0, decoded=0, pending_dropped=0,
                source_gaps=0, duplicates=0, out_of_order=0,
                retired_session=0, non_advancing_time=0,
                superseded=0, stale=0, decode_errors=0,
            )
            if self.run_root is not None or self.record_input:
                base_runs = self.run_root or (self.config_path.parent / 'runs')
                self.run_dir = base_runs / str(time.time_ns())
                self.run_dir.mkdir(parents=True, exist_ok=False)
                self.log = (self.run_dir / 'frames.jsonl').open('x', encoding='utf-8', buffering=65536)
                self.clock_log = (self.run_dir / 'clock.jsonl').open('x', encoding='utf-8', buffering=1)
                if self.record_input:
                    self.input_stream = (self.run_dir / 'input.ptj').open('xb', buffering=262144)
                (self.run_dir / 'config.json').write_text(json.dumps(self.config, indent=2), encoding='utf-8')
            else:
                self.run_dir = None
        self.threads = [
            threading.Thread(target=self._decode_worker, name='camera-preview', daemon=True),
            threading.Thread(target=self._receive, name='camera-source', daemon=True),
            threading.Thread(target=self._clock_refresh, name='camera-clock', daemon=True),
        ]
        for thread in self.threads:
            thread.start()
        return self.status()

    def _publish(self, header, payload, received_ns):
        with self.condition:
            accepted, reason, gaps = self.order.accept(header)
            if not accepted:
                self.counts[{'duplicate': 'duplicates'}.get(reason, reason)] += 1
                return False
            if reason == 'new_session':
                self.output_session_id = time.time_ns()
                self.preview = None
                self.last_header = None
            self.counts['received'] += 1
            self.counts['source_gaps'] += gaps
            self.received_times.append(received_ns)
            self.source = header
            if self.pending is not None:
                self.counts['pending_dropped'] += 1
            self.pending = (header, payload, received_ns)
            self.condition.notify()
        return True

    def _receive(self):
        with self.lock:
            config = dict(self._session_target or self.config)
        host = config['headset_host'].strip()
        port = self._active_port(config)
        try:
            self.inference_ready.wait()
            if self.stop_event.is_set():
                return
            try:
                calibrated = sync_clock(host, port)
                self._set_clock(calibrated)
            except (OSError, EOFError, ValueError):
                pass
            context = None
            try:
                with socket.create_connection((host, port), timeout=3) as control:
                    context = read_capture_context(control)
                with self.lock:
                    self.capture_context = context.evidence
            except (CaptureContextUnavailable, OSError, EOFError, ValueError):
                context = None
            sock = socket.create_connection((host, port), timeout=3)
            enable_keepalive(sock)
            sock.settimeout(10)  # The first subscription may cold-start the native camera.
            with self.lock:
                self.socket = sock
                self.connected = True
            sock.sendall(b'JPEG\n')
            with sock:
                first_frame = True
                while not self.stop_event.is_set():
                    try:
                        header, payload = read_frame(sock)
                    except IdleStreamTimeout:
                        if first_frame:
                            sock.settimeout(1)
                            first_frame = False
                        continue
                    if first_frame:
                        sock.settimeout(1)
                        first_frame = False
                    received_ns = time.perf_counter_ns()
                    if context is not None:
                        context.require_session(header.session_id)
                    if header.flags & END:
                        break
                    if not (header.flags & (JPEG | RAW)):
                        raise ValueError('Subscription returned a non-image frame')
                    if self.input_stream:
                        self.input_stream.write(header.pack() + payload)
                    self._publish(header, payload, received_ns)
        except (OSError, EOFError, ValueError) as error:
            if not self.stop_event.is_set():
                with self.lock:
                    self.last_error = str(error)
        finally:
            self.stop_event.set()
            with self.condition:
                self.running = False
                self.connected = False
                self.mode = 'idle'
                self.config['enabled'] = False
                self.socket = None
                self._session_target = None
                self.model_version += 1
                self.pending = None
                self.preview = None
                self.condition.notify_all()

    def _clock_refresh(self):
        while not self.stop_event.wait(10):
            try:
                with self.lock:
                    target = dict(self._session_target or self.config)
                host = target['headset_host'].strip()
                if not host:
                    continue
                calibrated = sync_clock(host, self._active_port(target), 3)
                self._set_clock(calibrated)
            except (OSError, EOFError, ValueError):
                pass

    def _set_clock(self, calibrated):
        with self.lock:
            self.clock = calibrated
            if self.clock_log:
                self.clock_log.write(json.dumps(calibrated, allow_nan=False) + '\n')

    def _age(self, header, pc_ns):
        with self.lock:
            clock = self.clock
        if clock is None:
            return float('inf'), float('inf')
        age = (pc_ns + clock['offset_ns'] - header.copy_mono_ns) / 1e6
        if pc_ns - clock['calibrated_pc_ns'] > 60_000_000_000:
            return age, float('inf')
        return age, age + clock['uncertainty_ms']

    def _decode_worker(self):
        try:
            # Warm Pillow's lazy JPEG plugin and default font before real sources.
            warm_picture = Image.new('RGB', (400, 400), 0)
            ImageDraw.Draw(warm_picture).text((0, 0), 'warm', fill='white')
            warm_jpeg = io.BytesIO()
            warm_picture.save(warm_jpeg, format='JPEG', quality=95)
            Image.open(io.BytesIO(warm_jpeg.getvalue())).load()
            self.inference_ready.set()
            while not self.stop_event.is_set():
                with self.condition:
                    if self.pending is None:
                        self.condition.wait(0.05)
                    if self.pending is None:
                        continue
                    header, payload, received = self.pending
                    self.pending = None
                    config = dict(self.config)
                    model_version = self.model_version
                begin = time.perf_counter_ns()
                with self.lock:
                    has_clock = self.clock is not None
                age, upper = self._age(header, begin) if has_clock else ((begin - received) / 1e6, (begin - received) / 1e6)
                if math.isfinite(upper) and upper > config['stale_ms']:
                    with self.lock:
                        self.counts['stale'] += 1
                    continue
                try:
                    if header.flags & RAW:
                        if len(payload) != 400 * 400:
                            raise ValueError('Decoded source dimensions changed')
                        image = Image.frombytes('L', (400, 400), payload)
                    else:
                        image = Image.open(io.BytesIO(payload)).convert('L')
                        if image.size != (400, 400):
                            raise ValueError('Decoded source dimensions changed')
                    decoded = time.perf_counter_ns()
                except Exception:
                    with self.lock:
                        if self.stop_event.is_set() or header.session_id != self.order.session or model_version != self.model_version:
                            self.counts['superseded'] += 1
                            continue
                        self.counts['decode_errors'] += 1
                    raise
                with self.lock:
                    if self.stop_event.is_set() or header.session_id != self.order.session or model_version != self.model_version:
                        self.counts['superseded'] += 1
                        continue
                    self.counts['decoded'] += 1
                    self.decoded_times.append(decoded)
                    self.last_header = header
                    preview_version = self.model_version
                if decoded - self.last_preview_ns >= 1e9 / config['preview_fps']:
                    preview = self._preview_image(image)
                    with self.lock:
                        if (
                            header.session_id == self.order.session
                            and not self.stop_event.is_set()
                            and preview_version == self.model_version
                        ):
                            self.preview = preview
                            self.last_preview_ns = decoded
                if self.log:
                    age_end, upper_end = self._age(header, decoded) if has_clock else ((decoded - received) / 1e6, (decoded - received) / 1e6)
                    entry = dict(
                        asdict(header),
                        received_pc_ns=received,
                        decode_begin_pc_ns=begin,
                        decoded_pc_ns=decoded,
                        source_age_ms=age_end if math.isfinite(age_end) else None,
                        source_age_upper_ms=upper_end if math.isfinite(upper_end) else None,
                    )
                    self.log.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + '\n')
        except Exception as error:
            self.inference_ready.set()
            with self.lock:
                self.last_error = type(error).__name__ + ': ' + str(error)
            self.stop_event.set()
            with self.lock:
                self.running = False
                self.config['enabled'] = False

    def _preview_image(self, image):
        output = io.BytesIO()
        image.convert('RGB').save(output, format='JPEG', quality=90)
        return output.getvalue()

    def stop(self):
        self.stop_event.set()
        with self.condition:
            self.model_version += 1
            self.pending = None
            self.preview = None
            self.lifecycle += 1
            self.condition.notify_all()
            sock = self.socket
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=5)
                if thread.is_alive():
                    raise RuntimeError('Owned runtime worker did not stop: ' + thread.name)
        with self.lock:
            if self.log:
                self.log.flush()
                self.log.close()
                self.log = None
            if self.input_stream:
                self.input_stream.close()
                self.input_stream = None
            if self.clock_log:
                self.clock_log.close()
                self.clock_log = None
            self.threads = []
            self.running = self.connected = False
            self.mode = 'idle'
            self.last_error = None
            self.config['enabled'] = False
            self._session_target = None
            self.pending = None
            self.preview = None
            if self.run_dir is not None:
                (self.run_dir / 'summary.json').write_text(
                    json.dumps(self.status(), ensure_ascii=False, indent=2), encoding='utf-8'
                )

    def close(self):
        self.stop()

    def status(self):
        with self.lock:
            def hz(values):
                return (len(values) - 1) * 1e9 / (values[-1] - values[0]) if len(values) > 1 and values[-1] > values[0] else 0.0

            def identity(header):
                if header is None:
                    return None
                return {
                    key: str(value) if key.endswith('_ns') or key in ('session_id', 'source_seq', 'raw_identity') else value
                    for key, value in asdict(header).items()
                }

            now = time.perf_counter_ns()
            age, _upper = self._age(self.last_header, now) if self.last_header else (None, None)
            video_fresh = bool(
                self.connected
                and self.received_times
                and (now - self.received_times[-1]) / 1e6 <= max(float(self.config['stale_ms']), 1500.0)
            )
            return {
                'version': APP_VERSION,
                'server_pid': os.getpid(),
                'running': self.running,
                'source_mode': self.mode,
                'connected': self.connected,
                'capture_only': True,
                'preview_camera': self.config['preview_camera'],
                'source': identity(self.source),
                'output_session_id': str(self.output_session_id),
                'metrics': dict(
                    self.counts,
                    received_hz=hz(self.received_times),
                    decoded_hz=hz(self.decoded_times),
                    source_age_ms=age if age is not None and math.isfinite(age) else None,
                    receive_age_ms=(now - self.received_times[-1]) / 1e6 if self.received_times else None,
                    video_fresh=video_fresh,
                    queue_depth=int(self.pending is not None),
                    clock_uncertainty_ms=self.clock['uncertainty_ms'] if self.clock else None,
                ),
                'config': dict(self.config),
                'capture_context': self.capture_context,
                'last_error': self.last_error,
                'run_directory': str(self.run_dir) if self.run_dir else None,
                'source_directory': str(ROOT),
                'fixed': {'width': 400, 'height': 400, 'fps': 90, 'codec': 'JPEG', 'quality': 95},
            }
