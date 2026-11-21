"""Bounded JPEG receive/decode/inference runtime, independent of the web preview."""
from collections import deque
from dataclasses import asdict, replace
import io
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import zlib

import numpy as np
from PIL import Image, ImageDraw

from state import TongueState
from personalization import Personalization, apply_calibration
from face_schema import (
    AGGREGATE_CHILDREN, AGGREGATE_STOCK_CHANNELS, ALL_VALID, CHANNELS, INDEX,
    MOUTH_MODES, MOUTH_PRESETS, PRESET_MODES, PURE_EXTENDED_CHANNELS, ROUTABLE_CHANNELS,
    SCHEMA, STOCK_AGGREGATE_PARENTS, STOCK_CHANNELS,
    matching_preset, resolve_channel_sources, validate_channel_sources,
)
from face_filter import FaceFilter
from wire import (
    END, JPEG, REPLAY, FrameHeader, SourceOrder,
    connect_cancellable, enable_keepalive, read_frame, result_packet, face_result_packet, sync_clock,
)


def _write_json_atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)

ROOT = Path(__file__).resolve().parent
DATA = ROOT/'user-data'
DEFAULT_CONFIG = dict(enabled=False, headset_host='',
                       headset_port=45681, model_path='', model_kind='face50',
                       device='auto', smoothing_ms=20.,
                      uncertain_hold_ms=100., stale_ms=350., preview_fps=10, size_hysteresis=True,
                      replay_path='',
                      mouth_mode='curated', hybrid_channels=None, channel_sources=None,
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
    if config['model_kind'] == 'face50':
        from face_model import FaceModel
        return FaceModel(config['model_path'], config['device'])
    if config['model_kind'] == 'mouthlink':
        from mouthlink import MouthLinkModel
        return MouthLinkModel(config['model_path'], config['device'])
    from model import TongueModel
    return TongueModel(config['model_path'], config['device'])


class Runtime:
    def __init__(self, config_path=None, model_factory=load_model, run_root=None, record_input=False):
        self.config_path = Path(config_path or DATA/'config.json')
        self.config = dict(DEFAULT_CONFIG)
        self.legacy_configuration = None
        if self.config_path.exists():
            try:
                saved = json.loads(self.config_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                saved = None
            if isinstance(saved, dict):
                self.legacy_configuration = saved.pop('legacy_configuration', None)
                if saved.get('model_kind', 'segmentation') != 'face50':
                    # Preserve the old settings, but never silently resume mask geometry.
                    self.legacy_configuration = saved
                    saved = {key: value for key, value in saved.items() if key not in ('model_kind', 'model_path')}
                self.config.update(saved)
        self.config.pop('face_source', None)  # Obsolete PC-side override; VRCFT owns source selection.
        if self.config.get('mouth_mode') not in MOUTH_MODES:
            self.config['mouth_mode'] = 'curated'
        saved_hybrid = self.config.get('hybrid_channels')
        if isinstance(saved_hybrid, list) and all(isinstance(name, str) for name in saved_hybrid):
            allowed = set(saved_hybrid)
            self.config['hybrid_channels'] = [name for name in STOCK_CHANNELS if name in allowed]
        else:
            self.config['hybrid_channels'] = None
        saved_sources = self.config.get('channel_sources')
        try:
            validated_sources = validate_channel_sources(saved_sources) if saved_sources is not None else None
        except ValueError:
            validated_sources = None
        if validated_sources is not None:
            full = resolve_channel_sources(self.config['mouth_mode'], self.config['hybrid_channels'], validated_sources)
            detected = matching_preset(full)
            self.config['mouth_mode'] = detected
            self.config['channel_sources'] = None if detected in PRESET_MODES else full
            self.config['hybrid_channels'] = None
        else:
            self.config['channel_sources'] = None
            if self.config['mouth_mode'] == 'custom':
                self.config['mouth_mode'] = 'curated'
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
        self.face_filter = FaceFilter()
        self.prediction = self._invalid('stopped')
        self.model = None
        self.model_key = None
        self.description = {}
        self.order = SourceOrder()
        self.tongue = TongueState()
        self.preview = None
        self.clock = None
        self.socket = None
        self._pending_sockets = set()
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
        self.capture_only = False
        self.recorder = None
        self.record_status = dict(state='idle')
        self.record_condition = threading.Condition()
        self.record_control = threading.RLock()
        self.record_queue = deque()
        self.record_stopping = False
        self.record_cancelled = False
        self.record_thread = None
        self.psl_lock = threading.Lock()
        self.psl_thread = None
        self.latest_stock = None
        self.record_wants_stock = False
        self.source_closed = False
        self.stock_socket = None
        self.stock_stop = threading.Event()
        self.stock_thread = None
        self.stock_status = dict(active=False, records=0, associated=0, unassociated=0, error=None)
        self.training = dict(state='idle', running=False, error=None, result=None)
        self.training_process = None
        self.training_thread = None
        self.transport_bindings = {}
        self.capture_context = None

    def _invalid(self, reason):
        if self.config['model_kind'] != 'face50':
            return TongueState.invalid(reason)
        self.face_filter.clear()
        return dict(schema=SCHEMA, values=[0.]*50, channels={}, valid_mask=0,
                    native_channels=[], state=reason, fresh=False, override=False)

    def _face_prediction(self, raw):
        values = raw['values']
        if len(values) != 50:
            raise ValueError('Whole-mouth prediction requires 50 values')
        validated = set(self.description.get('validated_channels', []))
        trained = set(self.description['trained_channels'])
        supported = sum(1 << i for i, name in enumerate(CHANNELS) if name in trained & validated)
        mask = int(raw['valid_mask']) & int(self.description.get('valid_mask', 0)) & supported & ALL_VALID
        controls = [float(value) if mask & (1 << i) else 0. for i, value in enumerate(values)]
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in controls):
            raise ValueError('Whole-mouth supported controls must be finite and in [0,1]')
        return dict(schema=SCHEMA, values=controls,
                    channels={name: controls[i] for i, name in enumerate(CHANNELS) if mask & (1 << i)},
                    valid_mask=mask, native_channels=[], fresh=bool(mask), override=bool(mask),
                    state='valid' if mask else 'untrained')

    def _stock_is_fresh(self, record, received_ns, session_id, config, now_pc_ns):
        if record is None or not record.valid:
            return False
        if session_id and record.capture_session_id not in (0, session_id):
            return False
        if (now_pc_ns - received_ns) / 1e6 > config['stale_ms']:
            return False
        if record.produced_mono_ns > 0 and self.clock is not None:
            source_age = (now_pc_ns + self.clock['offset_ns'] - record.produced_mono_ns) / 1e6 + self.clock['uncertainty_ms']
            if source_age > config['stale_ms']:
                return False
        return True

    def _resolved_sources(self, config=None):
        cfg = self.config if config is None else config
        return resolve_channel_sources(cfg['mouth_mode'], cfg.get('hybrid_channels'), cfg.get('channel_sources'))

    def _merge_mouth(self, prediction, header, config, now_pc_ns):
        model_values = list(prediction['values'])
        model_channels = dict(prediction['channels'])
        model_mask = int(prediction['valid_mask'])
        values = list(model_values)
        mask = model_mask
        native_channels = []
        sources = self._resolved_sources(config)
        record, received_ns = self.latest_stock if self.latest_stock else (None, None)
        stock_ok = any(src == 'stock' for src in sources.values()) and self._stock_is_fresh(
            record, received_ns, header.session_id, config, now_pc_ns)
        for i, name in enumerate(STOCK_CHANNELS):
            if name in AGGREGATE_STOCK_CHANNELS:
                continue
            src = sources[name]
            if src == 'off':
                values[i] = 0.
                mask &= ~(1 << i)
            elif src == 'stock' and stock_ok and i < len(record.final33):
                value = float(record.final33[i])
                if math.isfinite(value):
                    values[i] = min(1., max(0., value))
                    mask |= 1 << i
                    native_channels.append(name)
        for name, parent in STOCK_AGGREGATE_PARENTS.items():
            sub_i, parent_i = INDEX[name], INDEX[parent]
            src = sources[name]
            if src == 'off':
                values[sub_i] = 0.
                mask &= ~(1 << sub_i)
            elif src == 'stock' and stock_ok and parent_i < len(record.final33):
                value = float(record.final33[parent_i])
                if math.isfinite(value):
                    values[sub_i] = min(1., max(0., value))
                    mask |= 1 << sub_i
                    native_channels.append(name)
            elif not (model_mask & (1 << sub_i)) and (model_mask & (1 << parent_i)):
                values[sub_i] = model_values[parent_i]
                mask |= 1 << sub_i
        for parent, children in AGGREGATE_CHILDREN.items():
            parent_i = INDEX[parent]
            if any(sources[c] == 'off' for c in children):
                values[parent_i] = 0.
                mask &= ~(1 << parent_i)
            elif all(sources[c] == 'stock' for c in children) and stock_ok and parent_i < len(record.final33):
                value = float(record.final33[parent_i])
                if math.isfinite(value):
                    values[parent_i] = min(1., max(0., value))
                    mask |= 1 << parent_i
                    native_channels.append(parent)
        for name in PURE_EXTENDED_CHANNELS:
            if sources[name] == 'off':
                ext_i = INDEX[name]
                values[ext_i] = 0.
                mask &= ~(1 << ext_i)
        native_set = set(native_channels)
        ordered_native = [name for name in CHANNELS if name in native_set]
        return dict(prediction,
                    model_values=model_values, model_channels=model_channels, model_valid_mask=model_mask,
                    native_channels=ordered_native,
                    values=values, valid_mask=mask,
                    channels={name: values[i] for i, name in enumerate(CHANNELS) if mask & (1 << i)},
                    fresh=bool(mask), override=bool(mask),
                    state='valid' if mask else prediction['state'])

    def configure(self, changes):
        unknown = set(changes)-set(DEFAULT_CONFIG)
        if unknown:
            raise ValueError('Unknown configuration fields: '+', '.join(sorted(unknown)))
        updated = dict(self.config, **changes)
        if updated['model_kind'] not in ('face50', 'segmentation', 'mouthlink'):
            raise ValueError('Unknown model kind')
        if updated['mouth_mode'] not in MOUTH_MODES:
            raise ValueError("mouth_mode must be 'curated', 'hybrid', 'model_only', or 'custom'")
        hc = updated['hybrid_channels']
        if hc is not None:
            if (not isinstance(hc, (list, tuple)) or any(not isinstance(name, str) for name in hc)
                    or len(hc) != len(set(hc)) or set(hc) - set(STOCK_CHANNELS)):
                raise ValueError('hybrid_channels must be null or a unique list of native STOCK_CHANNELS')
            allowed = set(hc)
            updated['hybrid_channels'] = [name for name in STOCK_CHANNELS if name in allowed]
        if 'channel_sources' in changes:
            if changes['channel_sources'] is None:
                updated['channel_sources'] = None
                if updated['mouth_mode'] == 'custom':
                    raise ValueError("mouth_mode 'custom' cannot have null channel_sources")
            else:
                validated = validate_channel_sources(changes['channel_sources'])
                if 'mouth_mode' in changes and changes['mouth_mode'] in PRESET_MODES:
                    base = resolve_channel_sources(changes['mouth_mode'], None, None)
                elif 'mouth_mode' in changes and changes['mouth_mode'] == 'custom':
                    base = resolve_channel_sources('curated', None, self.config.get('channel_sources'))
                else:
                    base = resolve_channel_sources(
                        self.config['mouth_mode'], self.config.get('hybrid_channels'), self.config.get('channel_sources'))
                base.update(validated)
                full = {name: base[name] for name in ROUTABLE_CHANNELS}
                detected = matching_preset(full)
                updated['mouth_mode'] = detected
                updated['channel_sources'] = None if detected in PRESET_MODES else full
                updated['hybrid_channels'] = None
        elif 'mouth_mode' in changes:
            if updated['mouth_mode'] == 'custom':
                if not self.config.get('channel_sources'):
                    raise ValueError("mouth_mode 'custom' requires channel_sources")
                updated['channel_sources'] = dict(self.config['channel_sources'])
                updated['hybrid_channels'] = None
            else:
                updated['channel_sources'] = None
                if 'hybrid_channels' not in changes:
                    updated['hybrid_channels'] = None
        elif 'hybrid_channels' in changes and updated['mouth_mode'] == 'hybrid':
            updated['channel_sources'] = None
        for key in ('smoothing_ms', 'uncertain_hold_ms', 'stale_ms'):
            if not math.isfinite(float(updated[key])) or float(updated[key]) < 0:
                raise ValueError(key+' must be a finite nonnegative number')
        if not 1 <= int(updated['preview_fps']) <= 30:
            raise ValueError('preview_fps must be 1–30')
        if self.running and any(updated[k] != self.config[k] for k in
                                ('headset_host', 'headset_port', 'model_path', 'model_kind', 'device',
                                 'output_enabled', 'output_host', 'output_port')):
            self.stop()
            updated['enabled'] = False
        if not updated['enabled']:
            self.stop()
        with self.lock:
            self.config = updated
            self.personal.set_checkpoint(updated['model_path'])
            saved = dict(updated)
            if self.legacy_configuration:
                saved['legacy_configuration'] = self.legacy_configuration
            _write_json_atomic(self.config_path, saved)
        if updated['enabled'] and self.running:
            self._sync_psl_worker()
        return dict(self.config)

    def prepare(self):
        if self.running:
            return dict(self.description)
        self.personal.set_checkpoint(self.config['model_path'])
        checkpoint = self.config['model_path']
        if self.config['model_kind'] == 'segmentation':
            checkpoint = self.personal.profile['head'] or checkpoint
        key = (checkpoint, self.config['device'], self.config['model_kind'])
        if self.model is None or self.model_key != key:
            if not key[0]:
                raise ValueError('Configure a model checkpoint path before starting')
            self.model = self.model_factory(dict(self.config, model_path=key[0]))
            self.model_key = key
            self.description = json_value(self.model.describe())
            self.description.update(standard_checkpoint=self.config['model_path'],
                                    personal_head=key[2] == 'segmentation' and bool(self.personal.profile['head']))
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
            if self.config['model_kind'] == 'face50':
                raise ValueError('Whole-mouth models use local whole-mouth recording and training, not mask calibration')
            self.personal.set_checkpoint(self.config['model_path'])
            if action in ('calibrate', 'retrain'):
                if self.config['model_kind'] != 'segmentation':
                    raise ValueError('MouthLink models use the MouthLink recording and training tools')
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
            self.model, self.model_key = model, (config['model_path'], config['device'], config['model_kind'])
            self.description = dict(description, standard_checkpoint=self.config['model_path'], personal_head=bool(profile['head']))
            self.profile_reload = False

    def start(self, mode='live', replay_path=None, *, capture_only=False):
        if mode == 'capture':
            mode, capture_only = 'live', True
        if self.running:
            raise ValueError('Runtime is already running')
        if mode not in ('live', 'replay'):
            raise ValueError('mode must be live or replay')
        if mode == 'live' and not self.config['headset_host'].strip():
            raise ValueError('Enter the headset address before starting live input')
        self.stop()
        generation = self.lifecycle
        if capture_only:
            self.model = self.model_key = None
            self.description = dict(model_kind='face50', schema=SCHEMA, trained_channels=[], capture_only=True)
        else:
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
            self.capture_only = capture_only
            self.connected = False
            self.mode = mode
            self.config['enabled'] = True
            self.last_error = None
            self.pending = None
            self.source = self.prediction_source = None
            self.prediction = self._invalid('waiting')
            self.order = SourceOrder()
            self.tongue.clear()
            self.preview = None
            self.clock = None
            self.latencies.clear()
            self.received_times.clear()
            self.inferred_times.clear()
            self.last_header = None
            self.latest_stock = None
            self.record_wants_stock = False
            self.source_closed = False
            self.stock_status = dict(active=False, records=0, associated=0, unassociated=0, error=None)
            self.transport_bindings = {}
            self.capture_context = None
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
                        threading.Thread(target=source, name='camera-source', daemon=True)]
        if mode == 'live':
            self.threads.append(threading.Thread(target=self._clock_refresh, name='tongue-clock', daemon=True))
        for thread in self.threads:
            thread.start()
        self._sync_psl_worker()
        return self.status()

    def _publish(self, header, payload, received_ns):
        with self.condition:
            accepted, reason, gaps = self.order.accept(header)
            if not accepted:
                self.counts[{'duplicate': 'duplicates'}.get(reason, reason)] += 1
                return False
            if reason == 'new_session':
                if self.last_header is not None:
                    self.prediction = self._invalid('new_session')
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
        self._record_enqueue('frame', (header, payload))
        return True

    def _track_pending_socket(self, sock):
        with self.lock:
            if sock is None:
                return
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

    def _connect(self, host, port, timeout=3):
        holder = [None]
        def on_socket(sock):
            if sock is not None:
                holder[0] = sock
                self._track_pending_socket(sock)
            elif holder[0] is not None:
                self._untrack_pending_socket(holder[0])
                holder[0] = None
        return connect_cancellable(host, port, timeout=timeout, stop_event=self.stop_event, on_socket=on_socket)

    def _record_enqueue(self, kind, value):
        with self.record_condition:
            if self.record_thread is None or self.record_stopping:
                return
            if len(self.record_queue) >= 256:
                self.record_status = dict(self.record_status, error='Local recording queue overflow; recording stopped')
                self.record_stopping = True
            else:
                self.record_queue.append((kind, value))
            self.record_condition.notify()

    def _record_worker(self):
        last_status = 0.
        try:
            while True:
                with self.record_condition:
                    self.record_condition.wait_for(lambda: self.record_queue or self.record_stopping, timeout=.2)
                    if not self.record_queue and self.record_stopping:
                        break
                    item = self.record_queue.popleft() if self.record_queue else None
                if item:
                    kind, value = item
                    if kind == 'frame':
                        self.recorder.on_frame(*value)
                    elif kind == 'stock':
                        self.recorder.on_stock(value)
                    guide = self.recorder.guide
                    if guide and guide.automatic and guide.phase == 'finished':
                        self.record_stopping = True
                if time.monotonic()-last_status > .2:
                    self.record_status = self.recorder.status()
                    last_status = time.monotonic()
        except Exception as error:
            self.record_status = dict(self.record_status, error=str(error))
        finally:
            (self.recorder.cancel if self.record_cancelled else self.recorder.stop)()
            self.record_status = dict(self.recorder.status(), error=self.record_status.get('error'))
            self.record_wants_stock = False
            self.stock_thread = None
            self._sync_psl_worker()

    def _wants_psl(self):
        with self.lock:
            if (not self.running or self.stop_event.is_set() or self.source_closed
                    or self.mode != 'live' or self.config['model_kind'] != 'face50'):
                return False
            if self.record_wants_stock:
                return True
            if self.capture_only:
                return False
            sources = self._resolved_sources()
            return any(src == 'stock' for src in sources.values())

    def _psl_worker(self):
        while not self.stock_stop.is_set() and self._wants_psl():
            if not self.connected:
                self.stock_stop.wait(.02)
                continue
            self._stock_receive()
            if not self.stock_stop.is_set() and self._wants_psl():
                self.stock_stop.wait(.5)

    def _sync_psl_worker(self):
        with self.psl_lock:
            if self.psl_thread is not None and not self.psl_thread.is_alive():
                self.psl_thread = None
            if self._wants_psl():
                if self.psl_thread is None:
                    self.stock_stop.clear()
                    self.psl_thread = threading.Thread(target=self._psl_worker, name='mouth-stock', daemon=True)
                    self.psl_thread.start()
            else:
                self._stop_psl_worker_locked()

    def _stop_psl_worker_locked(self):
        self.stock_stop.set()
        sock = self.stock_socket
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            if hasattr(sock, 'close'):
                try:
                    sock.close()
                except OSError:
                    pass
        thread = self.psl_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError('Owned stock worker did not stop')
        self.psl_thread = None
        with self.lock:
            self.latest_stock = None
            self.stock_status['active'] = False

    def _stock_receive(self):
        from stock import read_stock
        try:
            sock = socket.create_connection((self.config['headset_host'], int(self.config['headset_port'])), timeout=3)
            if self.stock_stop.is_set():
                if hasattr(sock, 'close'):
                    sock.close()
                return
            self.stock_socket = sock
            with self.lock:
                self.stock_status['active'] = True
                self.stock_status['error'] = None
            with sock:
                sock.sendall(b'LABELS\n')
                while not self.stock_stop.is_set():
                    record = read_stock(sock)
                    received_ns = time.perf_counter_ns()
                    with self.lock:
                        self.latest_stock = (record, received_ns)
                        self.stock_status['records'] += 1
                        self.stock_status['associated' if record.association_valid else 'unassociated'] += 1
                    if self.record_wants_stock:
                        self._record_enqueue('stock', record)
        except (OSError, EOFError, ValueError) as error:
            if not self.stock_stop.is_set():
                with self.lock:
                    self.stock_status['error'] = str(error)
        finally:
            with self.lock:
                self.stock_status['active'] = False
                self.latest_stock = None
            self.stock_socket = None

    def recording(self, action, *, person_id='', confirmed=False, guide=True, stage='raw_only',
                  role=None, evaluation_only=None, preset_id='complete', automatic=False, receive_results=True,
                  continuation=None, step_index=None, prepare_seconds=3., guide_frame=None):
        if action == 'begin':
            if confirmed is not True:
                raise ValueError('Confirm local image/stock recording and local training first')
            if not isinstance(person_id, str) or not person_id.strip():
                raise ValueError('person_id must be a nonempty anonymous string')
            if type(receive_results) is not bool or (stage == 'instant' and not receive_results):
                raise ValueError('Instant labels require receiving mouth results')
            if self.record_thread is not None and not self.record_thread.is_alive():
                self._stop_recording()
            if self.record_thread is not None:
                raise ValueError('Stop the active recording before beginning another')
            if not self.running or not self.connected or self.mode != 'live' or self.config['model_kind'] != 'face50':
                raise ValueError('Start capture-only or live whole-mouth input explicitly before recording')
            from face_capture import WholeMouthRecording
            self.recorder = WholeMouthRecording(self.config_path.parent/'recordings')
            self.recorder.set_label_stage(stage)
            self.record_status = self.recorder.begin(person_id, confirmed=True, guide=guide,
                                                      role=role, evaluation_only=evaluation_only, preset_id=preset_id,
                                                       automatic=automatic, continuation=continuation,
                                                       step_index=step_index if step_index is not None else 0,
                                                       prepare_seconds=prepare_seconds)
            for capture_session_id, binding in self.transport_bindings.items():
                self.recorder.bind_capture(capture_session_id, **binding)
            self.record_stopping = False
            self.record_cancelled = False
            self.record_queue.clear()
            self.record_wants_stock = receive_results
            with self.lock:
                self.stock_status = dict(
                    active=bool(self.psl_thread and self.psl_thread.is_alive() and self.stock_status.get('active')),
                    records=0, associated=0, unassociated=0, error=None)
            self.record_thread = threading.Thread(target=self._record_worker, name='mouth-recording', daemon=True)
            self.record_thread.start()
            self._sync_psl_worker()
            self.stock_thread = self.psl_thread if receive_results else None
        elif action in ('ready', 'finish', 'confirm', 'skip', 'pause', 'resume', 'select_step'):
            if self.recorder is None:
                raise ValueError('Begin a recording first')
            self.recorder.advance(action, step_index=step_index, guide_frame=guide_frame)
            self.record_status = self.recorder.status()
        elif action in ('stop', 'cancel'):
            # End the run as well: no previous output or preview survives cancellation.
            self.stop(cancel_recording=action == 'cancel')
        else:
            raise ValueError('Unknown whole-mouth recording action')
        return dict(self.record_status)

    def bind_capture(self, capture_session_id, service_generation, *, evidence):
        """Called by a verified transport-metadata reader BEFORE its first PTJ frame.

        A stock record, receive time or HTTP form is not transport metadata.
        """
        if type(capture_session_id) is not int or service_generation is None or not evidence:
            raise ValueError('Capture binding requires explicit trusted transport metadata')
        binding = dict(service_generation=service_generation, evidence=evidence)
        with self.lock:
            if capture_session_id in self.transport_bindings and self.transport_bindings[capture_session_id] != binding:
                raise ValueError('Capture session cannot change service generation')
            self.transport_bindings[capture_session_id] = binding
        if self.recorder is not None and self.record_thread is not None:
            self.recorder.bind_capture(capture_session_id, **binding)

    def review_samples(self, offset=0, limit=24, all_frames=False):
        if self.recorder is None:
            return dict(total=0, offset=0, samples=[], session_id=None)
        if offset < 0 or not 1 <= limit <= 120:
            raise ValueError('Review page must use offset >= 0 and limit 1..120')
        result = self.recorder.review_candidates(offset=offset, limit=limit)
        if all_frames:
            rows = self.recorder.samples()
            fields = ('sample_id', 'image', 'prompt_candidate', 'review_labels', 'observation',
                      'capture_session_id', 'source_seq', 'mouth_sensor_ns', 'pairing_status')
            result = dict(total=len(rows), offset=offset,
                          samples=[{key: row.get(key) for key in fields} for row in rows[offset:offset+limit]])
        return dict(result, session_id=self.recorder.session_id)

    def review_image(self, session_id, sample_id):
        if self.recorder is None or session_id != self.recorder.session_id:
            raise ValueError('Image does not belong to the current local recording')
        with self.recorder.lock:
            row = next((row for row in self.recorder.images if row['sample_id'] == sample_id), None)
            if row is None:
                raise ValueError('Unknown local recording sample')
            root = self.recorder.path.resolve()
            path = (root/row['image']).resolve()
            if not path.is_relative_to(root/'images') or not path.is_file():
                raise ValueError('Image must remain inside the current recording images directory')
            return path.read_bytes()

    def review(self, session_id, sample_ids, targets, *, confirmed=False, reviewer='', observation='visible'):
        if self.recorder is None or session_id != self.recorder.session_id:
            raise ValueError('Review does not belong to the current local recording')
        if not isinstance(targets, dict) or not isinstance(sample_ids, list):
            raise ValueError('Review requires a target dictionary and selected sample IDs')
        result = self.recorder.review(sample_ids, targets, confirmed=confirmed,
                                      reviewer=reviewer, observation=observation)
        self.record_status = self.recorder.status()
        return result

    def _stop_recording(self, cancel=False):
        with self.record_control:
            self._finish_recording(cancel)

    def _finish_recording(self, cancel=False):
        self.record_cancelled = cancel
        self.record_wants_stock = False
        self._sync_psl_worker()
        self.stock_thread = None
        with self.record_condition:
            self.record_stopping = True
            self.record_condition.notify_all()
        if self.record_thread is not None:
            # Draining and exporting a real recording can exceed five seconds.
            # Keep ownership until the writer has actually finished.
            self.record_thread.join()
            self.record_status = dict(self.recorder.status(), error=self.record_status.get('error'))
        self.record_thread = None

    @staticmethod
    def training_operations():
        # Derive the web form and flags from the delivered CLI, not a second parser.
        import argparse
        from face_train import parser
        commands = next(action for action in parser()._actions if isinstance(action, argparse._SubParsersAction)).choices
        operations = {}
        optional = {'dino_repo', 'initial', 'backbone_checkpoint', 'pretrained', 'teacher',
                    'teacher_approval', 'approval', 'epochs', 'device', 'group_by', 'tune'}
        for name, command in commands.items():
            operations[name] = [dict(name=action.dest, required=action.required,
                                     type='number' if action.type in (int, float) else 'text',
                                     default=action.default if action.default is not None else '',
                                     choices=list(action.choices) if action.choices else None)
                                for action in command._actions
                                if action.dest != 'help' and (action.required or action.dest in optional)]
        return operations

    def train(self, action, operation=None, args=None):
        if action == 'cancel':
            process = self.training_process
            if process is not None and process.poll() is None:
                self.training['state'] = 'cancelling'
                process.terminate()
            if self.training_thread is not None:
                self.training_thread.join(timeout=5)
                if self.training_thread.is_alive():
                    raise RuntimeError('Owned whole-mouth training worker did not stop')
            self.training.update(state='cancelled', running=False)
            self.stop()
            return dict(self.training)
        if action != 'begin':
            raise ValueError('Unknown local training action')
        if self.training.get('running'):
            raise ValueError('A local training job is already running')
        operations = self.training_operations()
        if operation not in operations:
            raise ValueError('Unknown local training operation')
        args = args or {}
        fields = {field['name']: field for field in operations[operation]}
        if set(args)-set(fields):
            raise ValueError('Unknown local training arguments')
        command = [operation]
        for name, field in fields.items():
            value = args.get(name, field['default'])
            if field['required'] and (value is None or str(value).strip() == ''):
                raise ValueError('Required local training argument: '+name)
            if value is not None and str(value).strip() != '':
                command.extend(['--'+name.replace('_', '-'), str(value)])
        from face_train import parser
        try:
            parser().parse_args(command)
        except SystemExit as error:
            raise ValueError('Invalid local training arguments') from error
        self.stop()  # Starting an offline training job ends this runtime's live run.
        process = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', str(ROOT/'face_train.py'), *command],
                                   cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   encoding='utf-8', text=True)
        self.training_process = process
        self.training = dict(state='training', running=True, operation=operation, error=None, result=None,
                             progress=None, log_tail=[])
        self.training_thread = threading.Thread(target=self._training_wait, args=(process,), name='mouth-local-training', daemon=True)
        self.training_thread.start()
        return dict(self.training)

    def _training_wait(self, process):
        try:
            for line in process.stdout:
                try:
                    row = json.loads(line)
                except ValueError:
                    row = None
                with self.lock:
                    self.training['log_tail'] = (self.training['log_tail']+[line.rstrip()])[-30:]
                    if isinstance(row, dict) and row.get('event') == 'result':
                        self.training['result'] = row['result']
                    elif isinstance(row, dict) and row.get('event') == 'progress':
                        self.training['progress'] = row
            code = process.wait()
            with self.lock:
                cancelled = self.training['state'] == 'cancelling'
                self.training.update(running=False, state='cancelled' if cancelled else 'completed' if code == 0 else 'failed',
                                     error=None if code == 0 or cancelled else '\n'.join(self.training['log_tail']))
        finally:
            process.stdout.close()

    def _receive(self):
        config = dict(self.config)
        try:
            self.inference_ready.wait()
            if self.stop_event.is_set():
                return
            host = config['headset_host'].strip()
            port = int(config['headset_port'])
            holder = [None]
            def on_sock(sock):
                if sock is not None:
                    holder[0] = sock
                    self._track_pending_socket(sock)
                elif holder[0] is not None:
                    self._untrack_pending_socket(holder[0])
                    holder[0] = None
            calibrated = sync_clock(host, port, stop_event=self.stop_event, on_socket=on_sock)
            self._set_clock(calibrated)
            if self.stop_event.is_set():
                return
            context = None
            if config['model_kind'] == 'face50':
                from stock import read_capture_context
                with self._connect(host, port, timeout=3) as control:
                    context = read_capture_context(control)
                self.bind_capture(context.capture_session_id, context.service_generation, evidence=context.evidence)
                self.capture_context = context.evidence
            if self.stop_event.is_set():
                return
            sock = self._connect(host, port, timeout=3)
            enable_keepalive(sock)
            sock.settimeout(10)  # The first subscription may cold-start the native camera.
            with self.lock:
                self.socket = sock
                self.connected = True
            if self.stop_event.is_set():
                sock.close()
                return
            sock.sendall(b'JPEG\n')
            with sock:
                first_frame = True
                while not self.stop_event.is_set():
                    header, payload = read_frame(sock)
                    if first_frame:
                        sock.settimeout(1)
                        first_frame = False
                    received_ns = time.perf_counter_ns()
                    if context is not None:
                        context.require_session(header.session_id)
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
                self.source_closed = True
                self.socket = None
                self.model_version += 1
                self.pending = None
                self.prediction = self._invalid('disconnected')
                self.preview = None
                if self.last_header is not None:
                    self._send_result(self.last_header, self.prediction, 0.)
                self.condition.notify_all()
            self._stop_recording()

    def _clock_refresh(self):
        while not self.stop_event.wait(10):
            try:
                host = self.config['headset_host'].strip()
                if not host:
                    continue
                holder = [None]
                def on_sock(sock):
                    if sock is not None:
                        holder[0] = sock
                        self._track_pending_socket(sock)
                    elif holder[0] is not None:
                        self._untrack_pending_socket(holder[0])
                        holder[0] = None
                calibrated = sync_clock(host, int(self.config['headset_port']), 3,
                                        stop_event=self.stop_event, on_socket=on_sock)
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
        if self.config['model_kind'] == 'face50':
            data = face_result_packet(replace(header, session_id=self.output_session_id),
                                      prediction['values'], prediction['valid_mask'],
                                      max(0., age) if math.isfinite(age) else self.config['stale_ms'],
                                      state=0 if prediction['fresh'] else 1)
            self.udp.sendto(data, (self.config['output_host'], int(self.config['output_port'])))
            return
        state = {'present': 1, 'absent': 2, 'uncertain': 4, 'stopped': 16}.get(prediction['state'], 8)
        if not prediction['override'] and state != 16:
            state = 8
        data = result_packet(replace(header, session_id=self.output_session_id), state, prediction['left'], prediction['right'],
                       prediction['out'] or 0., max(0., age) if math.isfinite(age) else self.config['stale_ms'],
                       extended=self.config['model_kind'] == 'mouthlink',
                       up=prediction.get('up', 0.), down=prediction.get('down', 0.), roll=prediction.get('roll', 0.))
        self.udp.sendto(data, (self.config['output_host'], int(self.config['output_port'])))

    def _expire(self):
        if self.last_header is None or not self.prediction.get('fresh'):
            return
        now = time.perf_counter_ns()
        _, upper = self._age(self.last_header, now)
        if upper > self.config['stale_ms']:
            self.prediction = self._invalid('stale')
            self.tongue.clear()
            self._send_result(self.last_header, self.prediction, upper)

    def _inference(self):
        try:
            # CUDA's first call is per-thread. Finish warmup before subscribing,
            # so the native producer can choose its fixed epoch afterwards.
            if not self.capture_only:
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
                if self.config['model_kind'] == 'segmentation':
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
                    raw = (dict(values=[0.]*50, valid_mask=0) if self.capture_only else
                           self.model.predict(image, config))
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
                    if not self.capture_only:
                        self.counts['inferred'] += 1
                        self.inferred_times.append(finished)
                    if upper <= config['stale_ms'] and config['model_kind'] == 'segmentation':
                        self.personal.feed(image, raw, header)
                    elif upper > config['stale_ms']:
                        self.tongue.clear()
                    if config['model_kind'] == 'face50':
                        prediction = self._face_prediction(raw) if upper <= config['stale_ms'] else self._invalid('stale')
                        prediction = self.face_filter.apply(prediction, header, config)
                        if not self.capture_only and upper <= config['stale_ms']:
                            prediction = self._merge_mouth(prediction, header, config, time.perf_counter_ns())
                    else:
                        raw = apply_calibration(raw, self.personal.profile['calibration'],
                                                active=self.tongue.visual is not None,
                                                hysteresis=config['size_hysteresis'])
                        prediction = self.tongue.update(raw, header, config) if upper <= config['stale_ms'] else self._invalid('stale')
                    published = time.perf_counter_ns()
                    age, upper = self._age(header, published)
                    if upper > config['stale_ms']:
                        prediction = self._invalid('stale')
                    self.prediction = dict(json_value(raw), **prediction)
                    frame_prediction = dict(self.prediction)
                    output_session_id = self.output_session_id
                    preview_version = self.model_version
                    self.prediction_source = self.last_header = header
                    self.last_result_ns = published
                    self.latencies.append({'source_copy_to_result_ms': age,
                                           'queue_ms': (begin-received)/1e6,
                                           'pc_total_ms': (published-received)/1e6,
                                           'filter_ms': (published-finished)/1e6,
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
                             decoded_pc_ns=decoded, inferred_pc_ns=finished, published_pc_ns=published,
                             source_age_ms=age, source_age_upper_ms=upper if math.isfinite(upper) else None,
                             output_session_id=output_session_id, prediction=frame_prediction)
                self.log.write(json.dumps(entry, ensure_ascii=False, allow_nan=False)+'\n')
        except Exception as error:
            self.inference_ready.set()
            with self.lock:
                self.last_error = type(error).__name__+': '+str(error)
            self.stop_event.set()
            with self.lock:
                self.prediction = self._invalid('error')
                self.running = False
                self.config['enabled'] = False
                if self.last_header is not None:
                    self._send_result(self.last_header, self.prediction, 0.)

    def _preview_image(self, image, mask, prediction):
        picture = Image.fromarray(image).convert('RGB')
        if self.config['model_kind'] != 'face50' and mask is not None:
            alpha = Image.fromarray((np.asarray(mask, dtype=np.uint8)>0).astype(np.uint8)*110).resize((400, 400), Image.Resampling.NEAREST)
            overlay = Image.new('RGB', picture.size, '#fb7185')
            picture = Image.composite(overlay, picture, alpha)
        if self.config['model_kind'] != 'face50':
            text = f"{prediction['state']}  L={prediction['left']:.2f} R={prediction['right']:.2f}"
            draw = ImageDraw.Draw(picture)
            draw.rectangle((0, 377, 400, 400), fill='#0f172a')
            draw.text((8, 382), text, fill='white')
        output = io.BytesIO()
        picture.save(output, format='JPEG', quality=90)
        return output.getvalue()

    def stop(self, *, cancel_recording=False):
        self.stop_event.set()
        with self.condition:
            self.model_version += 1
            self.pending = None
            self.prediction = self._invalid('stopped')
            self.preview = None
            if self.last_header is not None:
                self._send_result(self.last_header, self.prediction, 0.)
        self._stop_recording(cancel=cancel_recording)
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
            pending_socks = list(self._pending_sockets)
            self._pending_sockets.clear()
        for pending in pending_socks:
            try:
                pending.close()
            except OSError:
                pass
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # Closing an already-disconnected owned connection.
            try:
                sock.close()
            except OSError:
                pass
        join_error = None
        try:
            for thread in self.threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=5)
                    if thread.is_alive() and join_error is None:
                        join_error = RuntimeError('Owned runtime worker did not stop: ' + thread.name)
        finally:
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
                self.socket = None
                self.running = self.connected = False
                self.config['enabled'] = False
                self.pending = None
                self.prediction = self._invalid('stopped')
                self.tongue.clear()
                self.preview = None
                if self.run_dir is not None:
                    (self.run_dir/'summary.json').write_text(
                        json.dumps(self.status(), ensure_ascii=False, indent=2), encoding='utf-8'
                    )
        if join_error is not None:
            raise join_error

    def close(self):
        try:
            self.stop()
        except RuntimeError:
            pass

    def expression_snapshot(self):
        with self.lock:
            now = time.perf_counter_ns()
            age, upper = self._age(self.last_header, now) if self.last_header else (None, None)
            stock_fresh = False
            stock_age_ms = None
            if self.latest_stock is not None:
                record, received_ns = self.latest_stock
                stock_age_ms = round(max(0., (now - received_ns) / 1e6), 2)
                session_id = self.source.session_id if self.source is not None else 0
                stock_fresh = self._stock_is_fresh(record, received_ns, session_id, self.config, now)
            return {
                'running': self.running,
                'connected': self.connected,
                'source_mode': self.mode,
                'capture_only': self.capture_only,
                'metrics': {
                    'source_age_ms': age if age is not None and math.isfinite(age) else None,
                    'source_fresh': self.prediction['fresh'] and upper is not None and upper <= self.config['stale_ms'] and self.connected,
                    'video_fresh': upper is not None and upper <= self.config['stale_ms'] and self.connected,
                },
                'prediction': json_value(self.prediction),
                'live_stock': {
                    'wanted': self._wants_psl(),
                    'active': bool(self.stock_status['active']),
                    'fresh': bool(stock_fresh),
                    'age_ms': stock_age_ms,
                    'error': self.stock_status.get('error'),
                },
            }

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
            stock_fresh = False
            stock_age_ms = None
            if self.latest_stock is not None:
                record, received_ns = self.latest_stock
                stock_age_ms = round(max(0., (now - received_ns) / 1e6), 2)
                session_id = self.source.session_id if self.source is not None else 0
                stock_fresh = self._stock_is_fresh(record, received_ns, session_id, self.config, now)
            return {'server_pid': os.getpid(), 'running': self.running, 'source_mode': self.mode, 'connected': self.connected,
                    'engine': self.description.get('engine'), 'device': self.description.get('device'),
                    'model': self.description, 'source': identity(self.source),
                    'prediction_source': identity(self.prediction_source),
                    'output_session_id': str(self.output_session_id),
                    'metrics': dict(self.counts, received_hz=hz(self.received_times), inference_hz=hz(self.inferred_times),
                                    source_age_ms=age if age is not None and math.isfinite(age) else None,
                                    receive_age_ms=(now-self.received_times[-1])/1e6 if self.received_times else None,
                                     source_fresh=self.prediction['fresh'] and upper is not None and upper <= self.config['stale_ms'] and self.connected,
                                     video_fresh=upper is not None and upper <= self.config['stale_ms'] and self.connected,
                                    queue_depth=int(self.pending is not None),
                                    queue_ms=self.latencies[-1]['queue_ms'] if self.latencies else None,
                                    pc_total_ms=self.latencies[-1]['pc_total_ms'] if self.latencies else None,
                                    latency_ms=latency, clock_uncertainty_ms=self.clock['uncertainty_ms'] if self.clock else None),
                    'prediction': json_value(self.prediction), 'config': dict(self.config),
                      'contract': dict(schema=SCHEMA, channels=list(CHANNELS), stock_channels=list(STOCK_CHANNELS),
                                     routable_channels=list(ROUTABLE_CHANNELS),
                                     presets={k: dict(v) for k, v in MOUTH_PRESETS.items()}),
                       'live_stock': dict(wanted=self._wants_psl(), active=bool(self.stock_status['active']),
                                          fresh=bool(stock_fresh), age_ms=stock_age_ms,
                                          error=self.stock_status.get('error')),
                       'recording': dict(self.record_status), 'stock': dict(self.stock_status),
                       'capture_context': self.capture_context,
                      'training': dict(self.training, operations=self.training_operations()),
                      'last_error': self.last_error, 'replay': dict(self.replay),
                      'capture_only': self.capture_only,
                      'legacy_configuration_available': bool(self.legacy_configuration),
                     'personalization': self.personal.status(),
                    'run_directory': str(self.run_dir) if self.run_dir else None,
                    'source_directory': str(ROOT),
                    'fixed': {'width': 400, 'height': 400, 'fps': 90, 'codec': 'JPEG', 'quality': 95}}
