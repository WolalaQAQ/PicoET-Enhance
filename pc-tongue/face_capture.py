"""Consented local JPEG/stock ledgers, exact asynchronous joins and reviewed labels.

FrameHeader has no service generation. Bind its capture session explicitly before
delivery (or provide service_generation in a header dict). Never infer it from the
latest stock record. See the local capture contract for the stock dict fields.
"""
from collections import Counter, defaultdict, deque
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time
import uuid

from face_dataset import append_json, json_value, write_json, write_samples
from face_presets import capture_preset, DEFAULT_PRESET
from face_schema import CHANNELS, INDEX, SCHEMA, STOCK_CHANNELS
from wire import END, JPEG


def _dict(value):
    return asdict(value) if is_dataclass(value) else dict(value)


def _finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


class MouthGuide:
    """Explicit prepare/hold/review/confirm; source continuity is not pose evidence."""
    def __init__(self, preset_id=DEFAULT_PRESET, *, automatic=False, clock=time.monotonic):
        self.preset = capture_preset(preset_id)
        self.steps = self.preset['steps']
        self.index = 0
        self.phase = 'prepare'
        self.frames = []
        self.completed = []
        self.skipped = []
        self.automatic, self.clock = automatic, clock
        self.paused = False
        self.prepare_started = None
        self.frame = uuid.uuid4().hex
        self.displayed_phase = None
        self.displayed_at = None

    def display(self, frame, phase, *, visible, media_ready):
        if frame != self.frame or phase != self.phase:
            return False
        self.displayed_phase = phase
        self.displayed_at = self.clock() if visible and media_ready else None
        return True

    def display_ready(self):
        return (self.displayed_phase == self.phase and self.displayed_at is not None
                and 0 <= self.clock()-self.displayed_at < 1.)

    def _next_display(self):
        self.frame = uuid.uuid4().hex
        self.prepare_started = self.displayed_at = self.displayed_phase = None

    @property
    def step(self):
        return self.steps[self.index] if self.index < len(self.steps) else None

    def feed(self, sample):
        if self.automatic:
            if self.paused or not self.display_ready():
                self.frames, self.prepare_started = [], None
                return False
            if self.phase == 'prepare':
                if self.prepare_started is None:
                    self.prepare_started = self.clock()
                if self.clock()-self.prepare_started >= 3.:
                    if self.step['kind'] == 'remount':
                        self.completed.append(self.step['id'])
                        self.index += 1
                        self.phase = 'finished'
                    else:
                        self.advance('ready')
                return False
        if self.phase != 'hold':
            return False
        if self.frames:
            previous = self.frames[-1]
            gap = sample['mouth_sensor_ns']-previous['mouth_sensor_ns']
            if (sample['capture_session_id'] != previous['capture_session_id'] or
                    sample['service_generation'] != previous['service_generation'] or
                    gap <= 0 or gap > 250_000_000):
                self.frames = []
        self.frames.append(sample)
        return True

    def candidates(self):
        if len(self.frames) < 2:
            return []
        start, end = self.frames[0]['mouth_sensor_ns'], self.frames[-1]['mouth_sensor_ns']
        if end-start < int((self.step['reaction_s']+self.step['hold_s']+self.step['tail_s'])*1e9):
            return []
        return [row for row in self.frames if
                start+int(self.step['reaction_s']*1e9) <= row['mouth_sensor_ns'] <= end-int(self.step['tail_s']*1e9)]

    def advance(self, action):
        if self.step is None:
            raise ValueError('Guide is finished')
        if action in ('pause', 'resume'):
            self.paused = action == 'pause'
            self.phase, self.frames = 'prepare', []
            self._next_display()
            return
        if action == 'skip':
            self.skipped.append(self.step['id'])
            self.index += 1
            self.phase, self.frames = 'prepare', []
            self._next_display()
        elif action == 'ready' and self.phase == 'prepare':
            if self.step['kind'] == 'remount':
                raise ValueError('Stop, refit, then begin a new recording session')
            self.phase, self.frames = 'hold', []
            self._next_display()
        elif action == 'finish' and self.phase == 'hold':
            if not self.candidates():
                raise ValueError('Not enough continuous new frames; keep holding or skip')
            self.phase = 'review'
        elif action == 'confirm' and self.phase == 'review':
            # This confirms task completion only. It does not approve image labels.
            self.completed.append(self.step['id'])
            self.index += 1
            self.phase, self.frames = 'prepare', []
            self._next_display()
        else:
            raise ValueError('Expected ready, finish, confirm or skip for the current phase')
        if self.step is None:
            self.phase = 'finished'

    def status(self):
        span = 0 if len(self.frames) < 2 else (self.frames[-1]['mouth_sensor_ns']-self.frames[0]['mouth_sensor_ns'])/1e9
        remaining = (max(0., 3.-(self.clock()-self.prepare_started)) if self.prepare_started is not None
                     else 3.) if self.phase == 'prepare' else max(0., (
                         self.step['reaction_s']+self.step['hold_s']+self.step['tail_s']-span
                     )) if self.step and self.phase == 'hold' else 0.
        return dict(step=deepcopy(self.step), phase=self.phase, index=self.index, total=len(self.steps),
                    automatic=self.automatic, paused=self.paused, frame=self.frame,
                    display_ready=self.display_ready(), remaining_seconds=remaining,
                    preset_id=self.preset['id'], preset_name=deepcopy(self.preset['name']),
                    continuous_seconds=span, frames=len(self.frames),
                    ready_to_finish=bool(self.step and self.phase == 'hold' and self.candidates()),
                    completed=list(self.completed), skipped=list(self.skipped),
                    progress=self.index/len(self.steps))


class WholeMouthRecording:
    PAIR_TIMEOUT_S = 2.

    def __init__(self, root, lock=None):
        self.root = Path(root)
        self.lock = lock if lock is not None else threading.RLock()
        self.state = 'idle'
        self.path = None
        self.session_id = None
        self.person_id = None
        self.label_stage = 'raw_only'
        self.guide = None
        self.images, self.stocks = [], []
        self.bindings = {}
        self.counts = Counter()
        self._image_keys = {}
        self._stock_fingerprints = {}
        self._seq_stocks = defaultdict(list)
        self._sensor_stocks = defaultdict(list)
        self._seq_images = defaultdict(list)
        self._sensor_images = defaultdict(list)
        self._deadlines = deque()

    def set_label_stage(self, stage):
        with self.lock:
            if self.state == 'recording':
                raise ValueError('Choose label stage before recording')
            if stage not in ('raw_only', 'instant'):
                raise ValueError('Choose raw_only or explicit instant compatibility labels')
            self.label_stage = stage

    def begin(self, person_id, confirmed=False, guide=True, *, role=None, evaluation_only=None,
              preset_id=DEFAULT_PRESET, automatic=False):
        with self.lock:
            # Check consent before mkdir, stop, clearing state or any other write.
            if confirmed is not True:
                raise ValueError('Confirm local image/stock recording and local training first')
            if not isinstance(person_id, str) or not person_id.strip():
                raise ValueError('person_id must be a nonempty anonymous string')
            if role not in (None, 'A', 'B', 'C'):
                raise ValueError('Recording role must be A, B, C or omitted for ordinary recording')
            if evaluation_only is not None and (type(evaluation_only) is not bool or evaluation_only != (role == 'C')):
                raise ValueError('evaluation_only must match the explicit recording role C')
            if self.state == 'recording':
                raise ValueError('Stop the active recording before beginning another')
            selected_guide = MouthGuide(preset_id, automatic=automatic) if guide else None
            self.person_id, self.session_id = person_id, uuid.uuid4().hex
            self.path = self.root/self.session_id
            self.path.mkdir(parents=True, exist_ok=False)
            (self.path/'images').mkdir()
            for name in ('images.jsonl', 'stock.jsonl', 'events.jsonl', 'reviews.jsonl', 'samples.jsonl'):
                (self.path/name).touch(exist_ok=False)
            self.images, self.stocks, self.bindings = [], [], {}
            self._image_keys, self._stock_fingerprints = {}, {}
            self._seq_stocks, self._sensor_stocks = defaultdict(list), defaultdict(list)
            self._seq_images, self._sensor_images = defaultdict(list), defaultdict(list)
            self._deadlines = deque()
            self.counts = Counter()
            self.guide = selected_guide
            now = datetime.now(timezone.utc)
            self.manifest = dict(schema=SCHEMA, channels=list(CHANNELS), stock_channels=list(STOCK_CHANNELS),
                                 person_id=person_id, session_id=self.session_id, fit_id=self.session_id,
                                 source_group_id=f'recording:{self.session_id}:fit:{self.session_id}',
                                 day=now.date().isoformat(), created_utc=now.isoformat(), state='recording',
                                  label_stage=self.label_stage, samples_complete=False,
                                   observability_role=role, evaluation_only=role == 'C',
                                  capture_plan=deepcopy(self.guide.preset) if self.guide else None,
                                  automatic_guidance=bool(self.guide and self.guide.automatic),
                                 consent=dict(local_recording=True, local_training=True, upload=False),
                                 nonfinite_raw_encoding='NaN/Infinity/-Infinity strings',
                                 split_fields=['person_id', 'session_id', 'fit_id', 'source_group_id'],
                                 pairing='exact verified identity only; no nearest/latest joins',
                                 prompt_labels='unreviewed proposals; not observed ground truth')
            write_json(self.path/'manifest.json', self.manifest)
            self.state = 'recording'
            return self._status()

    def bind_capture(self, capture_session_id, service_generation, *, evidence):
        """Bind known transport metadata, never a guessed/current/latest generation."""
        with self.lock:
            if self.state != 'recording':
                raise ValueError('Begin a recording first')
            if type(capture_session_id) is not int or service_generation is None or not evidence:
                raise ValueError('Binding needs capture_session_id, service_generation and evidence')
            old = self.bindings.get(capture_session_id)
            if old and old['service_generation'] != service_generation:
                raise ValueError('Capture session cannot span service generations; use a new capture session')
            if old == dict(service_generation=service_generation, evidence=evidence):
                return
            self.bindings[capture_session_id] = dict(service_generation=service_generation, evidence=evidence)
            self._event('capture_bound', capture_session_id=capture_session_id, **self.bindings[capture_session_id])
            # Frames already written without a generation stay unproven, never backfilled.

    def _event(self, kind, **fields):
        self.counts[kind] += 1
        append_json(self.path/'events.jsonl', dict(kind=kind, pc_mono_ns=time.perf_counter_ns(), **fields))

    @staticmethod
    def _prefix(row):
        return row['capture_session_id'], row.get('service_generation')

    def on_frame(self, header, jpeg):
        with self.lock:
            if self.state != 'recording':
                return False
            self._expire()
            h = _dict(header)
            if h['flags'] & END:
                self._event('capture_end', capture_session_id=h['session_id'])
                return False
            if not h['flags'] & JPEG or len(jpeg) != h['payload_bytes']:
                self._event('invalid_image', header=h, reason='Expected original JPEG payload with exact length')
                return False
            binding = self.bindings.get(h['session_id'], {})
            generation = h.get('service_generation', binding.get('service_generation'))
            if binding and generation != binding['service_generation']:
                raise ValueError('Frame generation contradicts capture binding')
            key = h['session_id'], generation, h['source_seq']
            if key in self._image_keys:
                original = self._image_keys[key]
                duplicate = all(h.get(k) == original['header'].get(k) for k in ('sensor_ns', 'vendor_ns', 'mouth_sensor_ns', 'raw_identity', 'raw_crc32'))
                # Same identity with different bytes is a conflict, not a retransmission.
                duplicate = duplicate and (self.path/original['image']).read_bytes() == bytes(jpeg)
                if not duplicate:
                    original['identity_conflict'] = True
                    self._refresh(original)
                conflict_image = None
                if not duplicate:
                    conflict_image = f'images/{original["sample_id"]}_conflict{self.counts["image_conflict"]:06d}.jpg'
                    (self.path/conflict_image).write_bytes(bytes(jpeg))
                append_json(self.path/'images.jsonl', dict(header=h, duplicate_of=original['sample_id'],
                                                          conflict=not duplicate, image=conflict_image))
                self._event('image_duplicate' if duplicate else 'image_conflict', sample_id=original['sample_id'])
                return False
            sample_id = f'f{len(self.images):09d}'
            image = f'images/{sample_id}.jpg'
            (self.path/image).write_bytes(bytes(jpeg))
            row = dict(sample_id=sample_id, image=image, header=h,
                       person_id=self.person_id, session_id=self.session_id, fit_id=self.session_id,
                       day=self.manifest['day'], capture_session_id=int(h['session_id']),
                       source_seq=int(h['source_seq']), mouth_sensor_ns=int(h['mouth_sensor_ns']),
                       source_identity=h['raw_identity'], service_generation=generation,
                       source_group_id=self.manifest['source_group_id'],
                       label_stage=self.label_stage, targets=[None]*50, target_sources=[None]*50,
                       observability_role=self.manifest['observability_role'],
                       evaluation_only=self.manifest['evaluation_only'],
                       pairing_status='pending' if generation is not None else 'unproven_generation',
                       trainable=False, review_labels={}, stock_id=None,
                       received_mono_ns=time.perf_counter_ns(), timed_out=False, identity_conflict=False)
            self.images.append(row)
            self._image_keys[key] = row
            prefix = self._prefix(row)
            self._seq_images[(*prefix, row['source_seq'])].append(row)
            self._sensor_images[(*prefix, row['mouth_sensor_ns'])].append(row)
            self._deadlines.append((row['received_mono_ns'], 'image', row))
            append_json(self.path/'images.jsonl', {k: v for k, v in row.items() if k not in ('targets', 'target_sources', 'review_labels')})
            self.counts['images'] += 1
            if self.guide and self.guide.feed(row):
                row['prompt_id'] = self.guide.step['id']
                row['prompt_phase'] = 'hold'
                if self.guide.automatic and self.guide.candidates():
                    self.advance('finish')
                    self.advance('confirm')
            self._refresh(row)
            for other in self._sensor_images[(*prefix, row['mouth_sensor_ns'])]:
                if other is not row:
                    self._refresh(other)
            self._expire()
            return True

    def on_stock(self, record):
        with self.lock:
            if self.state != 'recording':
                return False
            self._expire()
            original = deepcopy(_dict(record))
            record = dict(original)
            # PSL1 ASSOCIATED is set only by the native exact-input association path.
            # Preserve original field names as well as the canonical join fields.
            if 'association_valid' in original:
                record['identity_proven'] = original['association_valid'] is True
                record['source_identity'] = original['raw_identity']
                record['mouth_valid'] = original['valid']
                record['identity_evidence'] = ('PSL1 ASSOCIATED: verified native input/camera mapping'
                                               if record['identity_proven'] else '')
            fingerprint = json.dumps(json_value(original), sort_keys=True, allow_nan=False)
            duplicate = self._stock_fingerprints.get(fingerprint)
            stock_id = f's{self.counts["stock_arrivals"]:09d}'
            self.counts['stock_arrivals'] += 1
            append_json(self.path/'stock.jsonl', dict(stock_id=stock_id, record=original, duplicate_of=duplicate))
            if duplicate is not None:
                self._event('stock_duplicate', stock_id=stock_id, duplicate_of=duplicate)
                return False
            self._stock_fingerprints[fingerprint] = stock_id
            stock = dict(stock_id=stock_id, record=record, received_mono_ns=time.perf_counter_ns(),
                         matched=set(), timed_out=False)
            self.stocks.append(stock)
            self._deadlines.append((stock['received_mono_ns'], 'stock', stock))
            self.counts['stocks'] += 1
            prefix = record.get('capture_session_id'), record.get('service_generation')
            if record.get('source_seq') is not None:
                key = (*prefix, record['source_seq'])
                self._seq_stocks[key].append(stock)
                images = self._seq_images[key]
            else:
                key = (*prefix, record.get('mouth_sensor_ns'))
                self._sensor_stocks[key].append(stock)
                images = self._sensor_images[key]
            for image in images:
                self._refresh(image)
            self._expire()
            return True

    def _matches(self, image, stock):
        r = stock['record']
        if image['service_generation'] is None or r.get('service_generation') is None:
            return False
        if self._prefix(image) != (r.get('capture_session_id'), r.get('service_generation')):
            return False
        if not r.get('identity_evidence'):
            return False
        # Equal time is only a join key when the input-to-camera sensor mapping is proven.
        if r.get('source_seq') is None:
            if r.get('sensor_identity_proven') is not True or r.get('mouth_sensor_ns') != image['mouth_sensor_ns']:
                return False
        elif (r.get('identity_proven') is not True or r['source_seq'] != image['source_seq'] or
              r.get('mouth_sensor_ns') != image['mouth_sensor_ns']):
            return False
        if r.get('source_identity') is not None and r['source_identity'] != image['source_identity']:
            return False
        if r.get('vendor_ns') is not None and r['vendor_ns'] != image['header']['vendor_ns']:
            return False
        return True

    def _refresh(self, image):
        prefix = self._prefix(image)
        candidates = self._seq_stocks[(*prefix, image['source_seq'])]+self._sensor_stocks[(*prefix, image['mouth_sensor_ns'])]
        matches = [s for s in candidates if self._matches(image, s)]
        for stock in candidates:
            stock['matched'].discard(image['sample_id'])
        old_status, old_id = image['pairing_status'], image['stock_id']
        image['targets'], image['target_sources'] = [None]*50, [None]*50
        image['stock_id'] = None
        image.pop('stock', None)
        if image['identity_conflict']:
            image['pairing_status'] = 'image_conflict'
        elif len(matches) > 1 or (matches and matches[0]['record'].get('source_seq') is None and
                                 len(self._sensor_images[(*prefix, image['mouth_sensor_ns'])]) != 1):
            image['pairing_status'] = 'ambiguous'
        elif len(matches) == 1:
            stock = matches[0]
            stock['matched'].add(image['sample_id'])
            r = stock['record']
            image['stock_id'] = stock['stock_id']
            image['stock'] = deepcopy(r)
            image['pairing_status'] = 'paired_late' if image['timed_out'] or stock['timed_out'] else 'paired'
            image['pairing_method'] = 'verified_sensor' if r.get('source_seq') is None else 'verified_source'
            self._stock_targets(image, r)
        elif image['service_generation'] is None:
            image['pairing_status'] = 'unproven_generation'
        else:
            image['pairing_status'] = 'unproven_identity' if candidates else ('missing_stock' if self.state != 'recording' else 'pending')
        if image['pairing_status'] not in ('ambiguous', 'image_conflict'):
            for channel, annotation in image['review_labels'].items():
                image['targets'][INDEX[channel]] = annotation['value']
                image['target_sources'][INDEX[channel]] = annotation['source']
        if image.get('review_excluded'):
            image['targets'], image['target_sources'] = [None]*50, [None]*50
        image['trainable'] = any(value is not None for value in image['targets'])
        if (old_status, old_id) != (image['pairing_status'], image['stock_id']):
            self._event('pairing', sample_id=image['sample_id'], status=image['pairing_status'], stock_id=image['stock_id'])

    def _stock_targets(self, image, record):
        # Raw has no established control-scale mapping. Preserve it, do not clip it.
        if self.label_stage != 'instant':
            return
        quality = record.get('quality2')
        if (record.get('mouth_valid') is not True or record.get('warmup') is not False or
                not isinstance(quality, (tuple, list)) or len(quality) != 2 or
                not all(_finite(v) for v in quality) or quality[0] <= .1):
            return
        values = record.get('instant33')
        if not isinstance(values, (tuple, list)) or len(values) != 33:
            return
        for channel, value in zip(STOCK_CHANNELS, values):
            if _finite(value) and 0 <= value <= 1:
                if channel == 'TongueOut' and value == 0:
                    continue  # Thresholded zero is not a verified negative tongue example.
                image['targets'][INDEX[channel]] = float(value)
                image['target_sources'][INDEX[channel]] = 'stock_instant_compatibility'

    def _expire(self):
        now = time.perf_counter_ns()
        timeout = int(self.PAIR_TIMEOUT_S*1e9)
        while self._deadlines and now-self._deadlines[0][0] > timeout:
            _, kind, row = self._deadlines.popleft()
            missing = row['stock_id'] is None if kind == 'image' else not row['matched']
            if missing:
                row['timed_out'] = True
                self._event(kind+'_pair_timeout', **{'sample_id' if kind == 'image' else 'stock_id':
                                                    row['sample_id'] if kind == 'image' else row['stock_id']})

    def advance(self, action):
        with self.lock:
            if self.state != 'recording' or self.guide is None:
                raise ValueError('No active guide')
            if action == 'finish' and self.guide.phase == 'hold':
                for row in self.guide.candidates():
                    row['prompt_candidate'] = dict(prompt_id=self.guide.step['id'],
                                                   labels=deepcopy(self.guide.step['labels']), reviewed=False)
            previous = self.guide.step['id'] if self.guide.step else None
            self.guide.advance(action)
            self._event('guide_action', action=action, prompt_id=previous,
                        automatic=self.guide.automatic)
            return self.guide.status()

    def display(self, session_id, frame, phase, *, visible, media_ready):
        with self.lock:
            if self.state != 'recording' or session_id != self.session_id or self.guide is None:
                return dict(accepted=False)
            return dict(accepted=self.guide.display(frame, phase, visible=visible, media_ready=media_ready))

    def review(self, sample_ids, labels, *, confirmed=False, reviewer='', observation='visible'):
        """Explicit image review only. No unspecified channel is implicitly zero."""
        with self.lock:
            if confirmed is not True or not reviewer:
                raise ValueError('Confirm image review and provide a reviewer identifier')
            if observation not in ('visible', 'uncertain', 'occluded', 'no_mouth'):
                raise ValueError('Unknown observation')
            if self.state not in ('recording', 'stopped', 'cancelled'):
                raise ValueError('No recording to review')
            for channel, value in labels.items():
                if channel not in INDEX or value is not None and (not _finite(value) or not 0 <= value <= 1):
                    raise ValueError('Review labels must name schema channels and finite [0,1] values or null')
            if observation != 'visible' and any(v is not None for v in labels.values()):
                raise ValueError('Unobservable mouth labels must remain unknown')
            selected = {row['sample_id']: row for row in self.images}
            if not sample_ids or any(key not in selected for key in sample_ids):
                raise ValueError('Select existing sample_ids')
            review_id = uuid.uuid4().hex
            append_json(self.path/'reviews.jsonl', dict(review_id=review_id, reviewer=reviewer, observation=observation,
                                                       sample_ids=list(sample_ids), labels=labels, confirmed=True))
            for key in sample_ids:
                row = selected[key]
                row['observation'] = observation
                for channel, value in labels.items():
                    row['review_labels'][channel] = dict(value=None if value is None else float(value),
                                                         source=None if value is None else 'human_review:'+review_id)
                if observation != 'visible':
                    # Occlusion/uncertainty excludes stock as well, not just prompt labels.
                    row['review_excluded'] = True
                    row['review_labels'] = {}
                else:
                    row['review_excluded'] = False
                if row.get('prompt_candidate'):
                    row['prompt_candidate']['reviewed'] = True
                self._refresh(row)
            if self.state != 'recording':
                self._export()
            return dict(review_id=review_id, samples=len(sample_ids))

    def samples(self):
        with self.lock:
            return deepcopy(self.images)

    def review_candidates(self, offset=0, limit=120):
        """Small GUI page; image paths are relative to status()['path']."""
        with self.lock:
            rows = [row for row in self.images if row.get('prompt_candidate')]
            return dict(total=len(rows), offset=offset, samples=[
                {key: deepcopy(row.get(key)) for key in
                 ('sample_id', 'image', 'prompt_candidate', 'review_labels', 'observation',
                  'capture_session_id', 'source_seq', 'mouth_sensor_ns', 'pairing_status')}
                for row in rows[offset:offset+limit]])

    def _export(self):
        write_samples(self.path/'samples.jsonl', self.images)
        self.manifest.update(state=self.state, samples_complete=True, counts=dict(self.counts),
                             samples=len(self.images), trainable=sum(row['trainable'] for row in self.images),
                             capture_bindings=self.bindings)
        write_json(self.path/'manifest.json', self.manifest)

    def _stop(self, state):
        if self.state != 'recording':
            return self._status()
        self.state = state
        self._expire()
        for image in self.images:
            self._refresh(image)
            if image['stock_id'] is None:
                self._event('image_without_stock', sample_id=image['sample_id'], status=image['pairing_status'])
        used = {row['stock_id'] for row in self.images if row['stock_id'] is not None}
        for stock in self.stocks:
            if stock['stock_id'] not in used:
                self._event('stock_without_image', stock_id=stock['stock_id'])
        groups = defaultdict(set)
        for row in self.images:
            groups[self._prefix(row)].add(row['source_seq'])
        for (capture, generation), seqs in groups.items():
            ordered = sorted(seqs)
            for first, last in zip(ordered, ordered[1:]):
                if last > first+1:
                    self._event('source_gap', capture_session_id=capture, service_generation=generation,
                                first_missing=first+1, last_missing=last-1, count=last-first-1)
        self._event(state)
        self._export()
        return self._status()

    def stop(self):
        with self.lock:
            return self._stop('stopped')

    def cancel(self):
        """Stop callbacks and keep consented evidence; never delete existing samples."""
        with self.lock:
            return self._stop('cancelled')

    def _status(self):
        states = Counter(row['pairing_status'] for row in self.images)
        return dict(state=self.state, schema=SCHEMA, session_id=self.session_id, person_id=self.person_id,
                    path=str(self.path) if self.path else None, label_stage=self.label_stage,
                    images=len(self.images), stocks=len(self.stocks), counts=dict(self.counts), pairing=dict(states),
                    trainable=sum(row['trainable'] for row in self.images),
                    pending_reviews=sum(bool(row.get('prompt_candidate')) and not row['prompt_candidate']['reviewed'] for row in self.images),
                    guide=self.guide.status() if self.guide else None)

    def status(self):
        with self.lock:
            if self.state == 'recording':
                self._expire()
            return self._status()
