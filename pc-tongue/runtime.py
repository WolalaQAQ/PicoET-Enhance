"""Bounded JPEG receive/decode/inference runtime, independent of the web preview."""
from collections import deque
from dataclasses import asdict, replace
import io
import json
import math
import os
from pathlib import Path
import socket
import threading
import time
import zlib

import numpy as np
from PIL import Image, ImageDraw

from state import TongueState
from personalization import Personalization, apply_calibration
from wire import END, JPEG, REPLAY, FrameHeader, SourceOrder, read_frame, result_packet, sync_clock

ROOT = Path(__file__).resolve().parent
DATA = ROOT/'user-data'
DEFAULT_CONFIG = dict(enabled=False, headset_host='',
                      headset_port=45681, model_path=str(ROOT/'assets/shadow-final-v2.pt'), device='auto', smoothing_ms=20.,
                      uncertain_hold_ms=100., stale_ms=350., preview_fps=10, size_hysteresis=True,
                      replay_path='',
                      output_enabled=False, output_host='127.0.0.1', output_port=45682)


def json_value(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items() if k != 'mask'}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    return value


def load_model(config):
    from model import TongueModel
    return TongueModel(config['model_path'], config['device'])


class Runtime:
    def __init__(self, config_path=None, model_factory=load_model, run_root=None, record_input=False):
        self.config_path = Path(config_path or DATA/'config.json')
        self.config = dict(DEFAULT_CONFIG)
        if self.config_path.exists():
            self.config.update(json.loads(self.config_path.read_text(encoding='utf-8')))
        self.config.pop('face_source', None)  # Obsolete PC-side override; VRCFT owns source selection.
        self.config['enabled'] = False  # Opening the local service never starts capture.
        self.model_factory = model_factory
        self.run_root = Path(run_root or self.config_path.parent/'runs')
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.stop_event = threading.Event()
        self.running = self.connected = False
        self.mode = 'idle'
        self.last_error = None
        self.pending = None
        self.source = self.prediction_source = None
        self.prediction = TongueState.invalid('stopped')
        self.model = None
        self.model_key = None
        self.description = {}
        self.order = SourceOrder()
        self.tongue = TongueState()
        self.preview = None
        self.clock = None
        self.socket = None
        self.threads = []
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.log = None
        self.run_dir = None
        self.counts = {}
        self.latencies = deque(maxlen=2700)
        self.received_times = deque(maxlen=900)
        self.inferred_times = deque(maxlen=900)
        self.last_header = None
        self.last_result_ns = 0
        self.last_preview_ns = 0
        self.replay = {}
        self.inference_ready = threading.Event()
        self.lifecycle = 0
        self.output_session_id = time.time_ns()
        self.record_input = record_input
        self.input_stream = None
        self.clock_log = None
        self.model_version = 0
        self.profile_reload = False
        self.personal = Personalization(self.config_path.parent/'personalization', self.lock, self._personal_changed)
        self.personal.set_checkpoint(self.config['model_path'])

    def configure(self, changes):
        unknown = set(changes)-set(DEFAULT_CONFIG)
        if unknown:
            raise ValueError('Unknown configuration fields: '+', '.join(sorted(unknown)))
        updated = dict(self.config, **changes)
        for key in ('smoothing_ms', 'uncertain_hold_ms', 'stale_ms'):
            if not math.isfinite(float(updated[key])) or float(updated[key]) < 0:
                raise ValueError(key+' must be a finite nonnegative number')
        if not 1 <= int(updated['preview_fps']) <= 30:
            raise ValueError('preview_fps must be 1–30')
        if self.running and any(updated[k] != self.config[k] for k in
                                ('headset_host', 'headset_port', 'model_path', 'device',
                                 'output_enabled', 'output_host', 'output_port')):
            self.stop()
            updated['enabled'] = False
        if not updated['enabled']:
            self.stop()
        with self.lock:
            self.config = updated
            self.personal.set_checkpoint(updated['model_path'])
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding='utf-8')
        return dict(self.config)

    def prepare(self):
        if self.running:
            return dict(self.description)
        self.personal.set_checkpoint(self.config['model_path'])
        key = (self.personal.profile['head'] or self.config['model_path'], self.config['device'])
        if self.model is None or self.model_key != key:
            if not key[0]:
                raise ValueError('Configure a model checkpoint path before starting')
            self.model = self.model_factory(dict(self.config, model_path=key[0]))
            self.model_key = key
            self.description = json_value(self.model.describe())
            self.description.update(standard_checkpoint=self.config['model_path'], personal_head=bool(self.personal.profile['head']))
        self.profile_reload = False
        return dict(self.description)

    def _personal_changed(self, head_changed):
        # Called under the same lock as prediction publication and profile commits.
        self.model_version += 1
        self.profile_reload = self.profile_reload or head_changed
        if self.last_header is not None:
            self._send_result(self.last_header, self.tongue.invalid('profile_changed'), 0.)
        self.output_session_id = time.time_ns()
        self.tongue.clear()
        self.prediction = self.tongue.invalid('profile_changed')
        self.preview = None
        self.condition.notify_all()

    def personalize(self, action, confirmed=False):
        with self.lock:
            self.personal.set_checkpoint(self.config['model_path'])
            if action in ('calibrate', 'retrain'):
                fresh = self.source is not None and self._age(self.source, time.perf_counter_ns())[1] <= self.config['stale_ms']
                if not self.running or not self.connected or self.mode != 'live' or not fresh:
                    raise ValueError('Start live input and wait for fresh camera images before calibration')
                self.personal.begin(action, confirmed is True)
            elif action == 'cancel':
                self.personal.cancel()
            elif action == 'restore':
                self.personal.restore()
            else:
                raise ValueError('Unknown personalization action')
            return self.personal.status()

    def _activate_personal_profile(self):
        # The inference worker owns session construction and switching. HTTP and
        # training threads only publish a profile request, never touch the live model.
        with self.lock:
            request = self.personal.pending
            if request is None and not self.profile_reload:
                return
            profile = request['profile'] if request else dict(self.personal.profile)
            version = self.model_version
            config = dict(self.config, model_path=profile['head'] or self.config['model_path'])
        try:
            model = self.model_factory(config)
            for _ in range(2):
                model.predict(np.zeros((400, 400), np.uint8), config)
            description = json_value(model.describe())
        except Exception as error:
            if request is None:
                raise
            with self.lock:
                self.personal.fail_pending(request, error)
            return
        with self.lock:
            if self.stop_event.is_set() or version != self.model_version:
                return
            if request is not None:
                try:
                    if not self.personal.commit_pending(request):
                        return
                except OSError as error:
                    self.personal.fail_pending(request, error)
                    return
            self.model, self.model_key = model, (config['model_path'], config['device'])
            self.description = dict(description, standard_checkpoint=self.config['model_path'], personal_head=bool(profile['head']))
            self.profile_reload = False

    def start(self, mode='live', replay_path=None):
        if self.running:
            raise ValueError('Runtime is already running')
        if mode not in ('live', 'replay'):
            raise ValueError('mode must be live or replay')
        if mode == 'live' and not self.config['headset_host'].strip():
            raise ValueError('Enter the headset address before starting live input')
        self.stop()
        generation = self.lifecycle
        self.prepare()
        if mode == 'replay':
            path = Path(replay_path or self.config['replay_path'])
            if not path.is_file():
                raise ValueError('Replay dataset does not exist: '+str(path))
        with self.condition:
            if generation != self.lifecycle:
                raise RuntimeError('Startup was cancelled by stop/shutdown')
            self.stop_event.clear()
            self.inference_ready.clear()
            self.running = True
            self.connected = False
            self.mode = mode
            self.config['enabled'] = True
            self.last_error = None
            self.pending = None
            self.source = self.prediction_source = None
            self.prediction = TongueState.invalid('waiting')
            self.order = SourceOrder()
            self.tongue.clear()
            self.preview = None
            self.clock = None
            self.latencies.clear()
            self.received_times.clear()
            self.inferred_times.clear()
            self.last_header = None
            self.output_session_id = time.time_ns()
            self.last_result_ns = self.last_preview_ns = 0
            self.counts = dict(received=0, decoded=0, inferred=0, pending_dropped=0,
                               source_gaps=0, duplicates=0, out_of_order=0,
                               retired_session=0, non_advancing_time=0,
                               superseded=0, stale=0, decode_errors=0)
            self.run_dir = self.run_root/str(time.time_ns())
            self.run_dir.mkdir(parents=True, exist_ok=False)
            self.log = (self.run_dir/'frames.jsonl').open('x', encoding='utf-8', buffering=65536)
            self.clock_log = (self.run_dir/'clock.jsonl').open('x', encoding='utf-8', buffering=1)
            if self.record_input and mode == 'live':
                self.input_stream = (self.run_dir/'input.ptj').open('xb', buffering=262144)
            (self.run_dir/'config.json').write_text(json.dumps(self.config, indent=2), encoding='utf-8')
            self.replay = {'active': mode == 'replay', 'path': str(path) if mode == 'replay' else None,
                           'position': 0, 'total': None, 'finished': False}
        source = (lambda: self._replay(path)) if mode == 'replay' else self._receive
        self.threads = [threading.Thread(target=self._inference, name='tongue-inference', daemon=True),
                        threading.Thread(target=source, name='tongue-source', daemon=True)]
        if mode == 'live':
            self.threads.append(threading.Thread(target=self._clock_refresh, name='tongue-clock', daemon=True))
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
                if self.last_header is not None:
                    self.prediction = self.tongue.invalid('new_session')
                    self._send_result(self.last_header, self.prediction, 0.)
                self.output_session_id = time.time_ns()
                self.preview = None
                self.prediction_source = None
                self.last_header = None
                self.tongue.clear()
                if self.personal.wizard is not None:
                    self.personal.cancel()
                    self.personal.error = 'Camera session changed during calibration'
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
        config = dict(self.config)
        try:
            self.inference_ready.wait()
            if self.stop_event.is_set():
                return
            calibrated = sync_clock(config['headset_host'], int(config['headset_port']))
            self._set_clock(calibrated)
            sock = socket.create_connection((config['headset_host'], int(config['headset_port'])), timeout=3)
            sock.settimeout(1)
            with self.lock:
                self.socket = sock
                self.connected = True
            sock.sendall(b'JPEG\n')
            with sock:
                while not self.stop_event.is_set():
                    header, payload = read_frame(sock)
                    received_ns = time.perf_counter_ns()
                    if header.flags & END:
                        with self.lock:
                            self.replay['finished'] = True
                        break
                    if not header.flags & JPEG:
                        raise ValueError('JPEG subscription returned a non-JPEG frame')
                    if self.input_stream:
                        self.input_stream.write(header.pack()+payload)
                    self._publish(header, payload, received_ns)
        except (OSError, EOFError, ValueError) as error:
            if not self.stop_event.is_set():
                with self.lock:
                    self.last_error = str(error)
        finally:
            with self.condition:
                self.connected = False
                self.socket = None
                self.condition.notify_all()

    def _clock_refresh(self):
        while not self.stop_event.wait(10):
            try:
                calibrated = sync_clock(self.config['headset_host'], int(self.config['headset_port']), 3)
                self._set_clock(calibrated)
            except (OSError, EOFError, ValueError) as error:
                with self.lock:
                    self.description['clock_error'] = str(error)

    def _replay(self, path):
        try:
            self.inference_ready.wait()
            if self.stop_event.is_set():
                return
            with np.load(path, allow_pickle=False) as data:
                images = data['images']
                seqs, times = data['seq'], data['times']
                vendors = data['vendor_ns'] if 'vendor_ns' in data else times[:, 2]
                identities = data['raw_identity'] if 'raw_identity' in data else seqs
            count = len(images)
            if images.shape != (count, 400, 400) or times.shape != (count, 3):
                raise ValueError('Expected replay images[N,400,400], seq[N], times[N,3]')
            epoch = time.perf_counter_ns()+100_000_000
            session = time.time_ns() & ((1 << 64)-1)
            with self.lock:
                self._set_clock({'offset_ns': 0., 'uncertainty_ms': 0.,
                                 'calibrated_pc_ns': time.perf_counter_ns(), 'domain': 'pc-replay'})
                self.connected = True
                self.replay['total'] = count
            for index, (image, seq, stamps, vendor) in enumerate(zip(images, seqs, times, vendors)):
                due = epoch+int(stamps[2])-int(times[0, 2])
                wait = (due-time.perf_counter_ns())/1e9
                if wait > 0:
                    time.sleep(wait)  # Python's high-resolution Windows waitable timer.
                if self.stop_event.is_set():
                    break
                copied = time.perf_counter_ns()
                if copied-due > 11_111_111:
                    with self.lock:
                        self.counts.setdefault('replay_late_skipped', 0)
                        self.counts['replay_late_skipped'] += 1
                    continue
                begin = time.perf_counter_ns()
                output = io.BytesIO()
                Image.fromarray(image).save(output, format='JPEG', quality=95)
                encoded = time.perf_counter_ns()
                payload = output.getvalue()
                h = FrameHeader(len(payload), JPEG|REPLAY, session, int(seq), int(stamps[2]), int(vendor),
                                copied, begin, encoded, encoded, *(int(t) for t in stamps), int(identities[index]),
                                raw_crc32=zlib.crc32(image.tobytes()))
                self._publish(h, payload, encoded)
                with self.lock:
                    self.replay['position'] = index+1
            with self.lock:
                self.replay['finished'] = not self.stop_event.is_set()
        except Exception as error:
            with self.lock:
                self.last_error = str(error)
        finally:
            with self.condition:
                self.connected = False
                self.condition.notify_all()

    def _set_clock(self, calibrated):
        with self.lock:
            self.clock = calibrated
            if self.clock_log:
                self.clock_log.write(json.dumps(calibrated, allow_nan=False)+'\n')

    def _age(self, header, pc_ns):
        with self.lock:
            clock = self.clock
        if clock is None:
            return float('inf'), float('inf')
        age = (pc_ns+clock['offset_ns']-header.copy_mono_ns)/1e6
        if self.mode == 'live' and pc_ns-clock['calibrated_pc_ns'] > 60_000_000_000:
            return age, float('inf')
        return age, age+clock['uncertainty_ms']

    def _send_result(self, header, prediction, age):
        if not self.config['output_enabled']:
            return
        state = {'present': 1, 'absent': 2, 'uncertain': 4, 'stopped': 16}.get(prediction['state'], 8)
        if not prediction['override'] and state != 16:
            state = 8
        data = result_packet(replace(header, session_id=self.output_session_id), state, prediction['left'], prediction['right'],
                             prediction['out'] or 0., max(0., age) if math.isfinite(age) else self.config['stale_ms'])
        self.udp.sendto(data, (self.config['output_host'], int(self.config['output_port'])))

    def _expire(self):
        if self.last_header is None or not self.prediction.get('fresh'):
            return
        now = time.perf_counter_ns()
        _, upper = self._age(self.last_header, now)
        if upper > self.config['stale_ms']:
            self.prediction = self.tongue.invalid('stale')
            self.tongue.clear()
            self._send_result(self.last_header, self.prediction, upper)

    def _inference(self):
        try:
            # CUDA's first call is per-thread. Finish warmup before subscribing,
            # so the native producer can choose its fixed epoch afterwards.
            for _ in range(2):
                self.model.predict(np.zeros((400, 400), dtype=np.uint8), dict(self.config))
            # Warm Pillow's lazy JPEG plugin and default font before real sources.
            warm_picture = Image.fromarray(np.zeros((400, 400), dtype=np.uint8)).convert('RGB')
            ImageDraw.Draw(warm_picture).text((0, 0), 'warm', fill='white')
            warm_jpeg = io.BytesIO()
            warm_picture.save(warm_jpeg, format='JPEG', quality=95)
            Image.open(io.BytesIO(warm_jpeg.getvalue())).load()
            self.inference_ready.set()
            while not self.stop_event.is_set():
                self._activate_personal_profile()
                with self.condition:
                    self._expire()
                    if self.pending is None:
                        self.condition.wait(.05)
                    if self.pending is None:
                        self._expire()
                        continue
                    if self.profile_reload or self.personal.pending is not None:
                        continue  # Apply the new profile before taking another frame.
                    header, payload, received = self.pending
                    self.pending = None
                    config = dict(self.config)
                    model_version = self.model_version
                begin = time.perf_counter_ns()
                age, upper = self._age(header, begin)
                if upper > config['stale_ms']:
                    with self.lock:
                        self.counts['stale'] += 1
                    continue
                try:
                    image = np.asarray(Image.open(io.BytesIO(payload)).convert('L'))
                    decoded = time.perf_counter_ns()
                    if image.shape != (400, 400):
                        raise ValueError('Decoded source dimensions changed')
                    raw = self.model.predict(image, config)
                except Exception:
                    with self.lock:
                        if self.stop_event.is_set() or header.session_id != self.order.session or model_version != self.model_version:
                            self.counts['superseded'] += 1
                            continue
                        self.counts['decode_errors'] += 1
                    raise
                finished = time.perf_counter_ns()
                age, upper = self._age(header, finished)
                with self.lock:
                    if self.stop_event.is_set() or header.session_id != self.order.session or model_version != self.model_version:
                        self.counts['superseded'] += 1
                        continue
                    self.counts['decoded'] += 1
                    self.counts['inferred'] += 1
                    self.inferred_times.append(finished)
                    if upper <= config['stale_ms']:
                        self.personal.feed(image, raw, header)
                    else:
                        self.tongue.clear()
                    raw = apply_calibration(raw, self.personal.profile['calibration'],
                                            active=self.tongue.visual is not None,
                                            hysteresis=config['size_hysteresis'])
                    prediction = self.tongue.update(raw, header, config) if upper <= config['stale_ms'] else self.tongue.invalid('stale')
                    self.prediction = dict(json_value(raw), **prediction)
                    frame_prediction = dict(self.prediction)
                    output_session_id = self.output_session_id
                    preview_version = self.model_version
                    self.prediction_source = self.last_header = header
                    self.last_result_ns = finished
                    self.latencies.append({'source_copy_to_result_ms': age,
                                           'queue_ms': (begin-received)/1e6,
                                           'pc_total_ms': (finished-received)/1e6,
                                           'decode_ms': (decoded-begin)/1e6,
                                           'predict_ms': (finished-decoded)/1e6})
                    self._send_result(header, prediction, upper)
                if finished-self.last_preview_ns >= 1e9/config['preview_fps']:
                    preview = self._preview_image(image, raw.get('mask'), prediction)
                    with self.lock:
                        if (header.session_id == self.order.session and not self.stop_event.is_set()
                                and preview_version == self.model_version):
                            self.preview = preview
                            self.last_preview_ns = finished
                entry = dict(asdict(header), received_pc_ns=received, decode_begin_pc_ns=begin,
                             decoded_pc_ns=decoded, inferred_pc_ns=finished,
                             source_age_ms=age, source_age_upper_ms=upper if math.isfinite(upper) else None,
                             output_session_id=output_session_id, prediction=frame_prediction)
                self.log.write(json.dumps(entry, ensure_ascii=False, allow_nan=False)+'\n')
        except Exception as error:
            self.inference_ready.set()
            with self.lock:
                self.last_error = type(error).__name__+': '+str(error)
            self.stop_event.set()
            with self.lock:
                self.prediction = self.tongue.invalid('error')
                self.running = False
                self.config['enabled'] = False
                if self.last_header is not None:
                    self._send_result(self.last_header, self.prediction, 0.)

    def _preview_image(self, image, mask, prediction):
        picture = Image.fromarray(image).convert('RGB')
        if mask is not None:
            alpha = Image.fromarray((np.asarray(mask, dtype=np.uint8)>0).astype(np.uint8)*110).resize((400, 400), Image.Resampling.NEAREST)
            overlay = Image.new('RGB', picture.size, '#fb7185')
            picture = Image.composite(overlay, picture, alpha)
        text = f"{prediction['state']}  L={prediction['left']:.2f} R={prediction['right']:.2f}"
        draw = ImageDraw.Draw(picture)
        draw.rectangle((0, 377, 400, 400), fill='#0f172a')
        draw.text((8, 382), text, fill='white')
        output = io.BytesIO()
        picture.save(output, format='JPEG', quality=90)
        return output.getvalue()

    def stop(self):
        self.stop_event.set()
        self.personal.cancel()
        training = self.personal.thread
        if training is not None and training is not threading.current_thread():
            training.join(timeout=5)
            if training.is_alive():
                raise RuntimeError('Owned personal training worker did not stop')
        with self.condition:
            self.lifecycle += 1
            self.condition.notify_all()
            sock = self.socket
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # Closing an already-disconnected owned connection.
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=5)
                if thread.is_alive():
                    raise RuntimeError('Owned runtime worker did not stop: '+thread.name)
        with self.lock:
            if self.last_header is not None:
                self._send_result(self.last_header, self.tongue.invalid('stopped'), 0.)
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
            self.config['enabled'] = False
            self.pending = None
            self.prediction = self.tongue.invalid('stopped')
            self.tongue.clear()
            self.preview = None
            if self.run_dir is not None:
                (self.run_dir/'summary.json').write_text(json.dumps(self.status(), ensure_ascii=False, indent=2), encoding='utf-8')

    def status(self):
        with self.lock:
            def hz(values):
                return (len(values)-1)*1e9/(values[-1]-values[0]) if len(values)>1 else 0.
            latency = {}
            for key in ('source_copy_to_result_ms', 'decode_ms', 'predict_ms', 'queue_ms', 'pc_total_ms'):
                values = [row[key] for row in self.latencies]
                latency[key] = {'p50': float(np.percentile(values, 50)), 'p95': float(np.percentile(values, 95))} if values else None
            def identity(header):
                if header is None:
                    return None
                return {key: str(value) if key.endswith('_ns') or key in ('session_id', 'source_seq', 'raw_identity') else value
                        for key, value in asdict(header).items()}
            now = time.perf_counter_ns()
            age, upper = self._age(self.last_header, now) if self.last_header else (None, None)
            return {'server_pid': os.getpid(), 'running': self.running, 'source_mode': self.mode, 'connected': self.connected,
                    'engine': self.description.get('engine'), 'device': self.description.get('device'),
                    'model': self.description, 'source': identity(self.source),
                    'prediction_source': identity(self.prediction_source),
                    'output_session_id': str(self.output_session_id),
                    'metrics': dict(self.counts, received_hz=hz(self.received_times), inference_hz=hz(self.inferred_times),
                                    source_age_ms=age if age is not None and math.isfinite(age) else None,
                                    receive_age_ms=(now-self.received_times[-1])/1e6 if self.received_times else None,
                                    source_fresh=self.prediction['fresh'] and upper is not None and upper <= self.config['stale_ms'],
                                    queue_depth=int(self.pending is not None),
                                    queue_ms=self.latencies[-1]['queue_ms'] if self.latencies else None,
                                    pc_total_ms=self.latencies[-1]['pc_total_ms'] if self.latencies else None,
                                    latency_ms=latency, clock_uncertainty_ms=self.clock['uncertainty_ms'] if self.clock else None),
                    'prediction': json_value(self.prediction), 'config': dict(self.config),
                     'last_error': self.last_error, 'replay': dict(self.replay),
                     'personalization': self.personal.status(),
                    'run_directory': str(self.run_dir) if self.run_dir else None,
                    'source_directory': str(ROOT),
                    'fixed': {'width': 400, 'height': 400, 'fps': 90, 'codec': 'JPEG', 'quality': 95}}
