"""PC calibration, consented sample collection and transactional personal profiles."""
from collections import deque
import copy
import json
import math
import os
from pathlib import Path
import threading
import time

import numpy as np

DEFAULT_CALIBRATION = dict(left=.33, center=.66, right=1.11, min_area=100)
STEPS = ('fit', 'closed', 'open', 'center', 'left', 'right')
LABELS = dict(closed=1, open=2, center=3, left=4, right=5)


def valid_calibration(cal):
    left, center, right = (cal[key] for key in ('left', 'center', 'right'))
    return (all(math.isfinite(v) and 0 < v < 1.7 for v in (left, center, right)) and
            abs(left-center) >= .08 and abs(right-center) >= .08 and
            (left-center)*(right-center) < 0 and 20 <= cal['min_area'] <= 600)


def calibration_from_samples(samples):
    angles = {label: [s['raw_angle'] for s in samples if s['label'] == label and s['raw_angle'] is not None]
              for label in (3, 4, 5)}
    if any(len(values) < 3 for values in angles.values()):
        raise ValueError('Not enough visible tongue samples for calibration')
    negative = sorted(s['area'] if s['raw_angle'] is not None else 0 for s in samples if s['label'] <= 2)
    positive = sorted(s['area'] for s in samples if s['label'] >= 3 and s['raw_angle'] is not None)
    high = negative[min(len(negative)-1, int(len(negative)*.95))] if negative else 0
    low = positive[min(len(positive)-1, int(len(positive)*.05))]
    area = max(40, min(300, int(high+(low-high)*.4+.5))) if high+10 < low else 100
    cal = dict(left=float(np.median(angles[4])), center=float(np.median(angles[3])),
               right=float(np.median(angles[5])), min_area=area)
    if not valid_calibration(cal):
        raise ValueError('Left and right calibration poses are not separated')
    return cal


def apply_calibration(raw, cal, active=False, hysteresis=True):
    # Test/model adapters without mask geometry retain their original contract.
    if 'raw_angle' not in raw:
        return raw
    result = dict(raw)
    threshold = max(20, int(cal['min_area']*.6)) if active and hysteresis else cal['min_area']
    angle = raw['raw_angle']
    present = angle is not None and raw['mask_area'] >= threshold
    position = 0.
    if present:
        left, center, right = (cal[key] for key in ('left', 'center', 'right'))
        p = -(angle-center)/(left-center) if (angle-center)*(left-center) > 0 else (angle-center)/(right-center)
        p = max(-1., min(1., p))
        position = 0. if abs(p) < .06 else (abs(p)-.06)/.94*(1 if p > 0 else -1)
    result.update(present=present, angle=angle if present else None, position=position,
                  left=max(0., -position), right=max(0., position), out=float(present),
                  area_threshold=threshold, size_held=bool(present and raw['mask_area'] < cal['min_area']))
    return result


class Wizard:
    """Independent <=20Hz guide sampling; the video/inference pipeline stays at 90Hz."""
    def __init__(self, mode):
        self.mode = mode
        self.step = 'fit'
        self.hint = 'fit'
        self.session = None
        self.last_ns = self.entered_ns = None
        self.count = 0
        self.window = deque(maxlen=8)
        self.rows = {label: [] for label in range(1, 6)}
        self.current = []
        self.closed_ref = self.open_ref = self.previous_blocks = None
        self.center = self.left = self.side_best = None
        self.finished = False

    @property
    def target(self):
        if self.step == 'fit':
            return 20
        return 60 if self.mode == 'retrain' else {'closed': 30, 'open': 30, 'center': 24, 'left': 40, 'right': 40}[self.step]

    def reset_hold(self, hint):
        self.count = 0
        self.current = []
        self.window.clear()
        self.hint = hint

    def feed(self, image, raw, header):
        ns = header.mouth_sensor_ns
        if self.session is None:
            self.session = header.session_id
        if self.session != header.session_id:
            raise ValueError('Camera session changed during calibration')
        if self.last_ns is not None and ns-self.last_ns < 50_000_000:
            return None
        if self.last_ns is not None and ns-self.last_ns > 250_000_000:
            self.reset_hold('fresh_video')
        self.last_ns = ns
        if self.entered_ns is None:
            self.entered_ns = ns
        from model import downsample400
        frame = downsample400(image)
        blocks = frame.reshape(50, 4, 50, 4).mean(axis=(1, 3))
        nose = raw.get('nose_area', 0)
        if not 3000 <= nose <= 11000:
            self.reset_hold('fit_low' if nose < 3000 else 'fit_glare')
            return None
        if self.step == 'fit':
            self.count += 1
        else:
            if ns-self.entered_ns < 1_500_000_000:
                self.hint = 'settle'
                return None
            if self.step == 'open' and float(np.abs(blocks-self.closed_ref).mean()) < 5:
                self.reset_hold('open')
                return None
            if self.step in ('center', 'left', 'right'):
                if self.mode == 'retrain':
                    still = self.previous_blocks is None or float(np.abs(blocks-self.previous_blocks).mean()) < 2.5
                    self.previous_blocks = blocks
                    if float(np.abs(blocks-self.open_ref).mean()) < 4 or not still:
                        self.reset_hold('hold_tongue')
                        return None
                else:
                    angle = raw.get('raw_angle')
                    if angle is None or raw.get('mask_area', 0) < 40:
                        self.reset_hold('visible_tongue')
                        return None
                    if self.step != 'center' and (abs(angle-self.center) < .1 or
                            self.step == 'right' and (angle-self.center)*(self.left-self.center) > 0):
                        self.reset_hold('move_side')
                        return None
                    self.window.append(angle)
                    steady = len(self.window) == 8 and max(self.window)-min(self.window) <= .12
                    if self.step == 'center' and len(self.window) == 8 and not steady:
                        self.reset_hold('steady')
                        return None
                    if self.step != 'center':
                        if steady:
                            median = float(np.median(self.window))
                            if self.side_best is None or abs(median-self.center) > abs(self.side_best-self.center):
                                self.side_best = median
                        if self.side_best is None:
                            self.hint = 'steady'
                            return None
            self.current.append(dict(frame=frame.copy(), mask=np.asarray(raw.get('mask', np.zeros((56, 56))), dtype=bool).copy(),
                                     label=LABELS[self.step], raw_angle=raw.get('raw_angle'), area=int(raw.get('mask_area', 0)),
                                     session_id=str(header.session_id), source_seq=str(header.source_seq), sensor_ns=str(ns)))
            self.count += 1
        self.hint = 'holding'
        if self.count < self.target:
            return None
        if self.step != 'fit':
            if self.mode == 'calibrate' and self.step in ('left', 'right'):
                for sample in self.current:
                    sample['raw_angle'] = self.side_best
            self.rows[LABELS[self.step]] = self.current
            if self.step in ('closed', 'open'):
                reference = np.stack([s['frame'].reshape(50, 4, 50, 4).mean(axis=(1, 3)) for s in self.current]).mean(axis=0)
                if self.step == 'closed':
                    self.closed_ref = reference
                else:
                    self.open_ref = reference
            elif self.mode == 'calibrate' and self.step == 'center':
                self.center = float(np.median(self.window))
            elif self.mode == 'calibrate' and self.step == 'left':
                self.left = self.side_best
        if self.step == 'right':
            self.finished = True
            return [sample for rows in self.rows.values() for sample in rows]
        self.step = STEPS[STEPS.index(self.step)+1]
        self.entered_ns = ns
        self.side_best = self.previous_blocks = None
        self.reset_hold(self.step)
        return None

    def status(self):
        return dict(mode=self.mode, step=self.step, hint=self.hint, collected=self.count, target=self.target,
                    progress=(STEPS.index(self.step)+min(1., self.count/self.target))/len(STEPS))


class Personalization:
    def __init__(self, root, lock, changed, trainer=None):
        self.root = Path(root)
        self.lock = lock
        self.changed = changed
        self.trainer = trainer
        self.checkpoint = ''
        self.profile = self.standard()
        self.wizard = None
        self.state = 'idle'
        self.error = ''
        self.progress = 0.
        self.metrics = None
        self.generation = 0
        self.cancel_event = threading.Event()
        self.thread = None
        self.pending = None
        self.job_id = None

    def standard(self):
        return dict(checkpoint=self.checkpoint, calibration=dict(DEFAULT_CALIBRATION), head=None)

    def set_checkpoint(self, checkpoint):
        checkpoint = str(Path(checkpoint).resolve()) if checkpoint else ''
        if checkpoint == self.checkpoint:
            return
        self.checkpoint = checkpoint
        file = self.root/'active.json'
        profile = json.loads(file.read_text(encoding='utf-8')) if file.exists() else self.standard()
        self.profile = profile if profile['checkpoint'] == checkpoint else self.standard()

    def persist(self, profile):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root/'active.new'
        temporary.write_text(json.dumps(profile, indent=2, allow_nan=False), encoding='utf-8')
        os.replace(temporary, self.root/'active.json')
        self.profile = profile

    def begin(self, mode, confirmed=False):
        with self.lock:
            if mode not in ('calibrate', 'retrain'):
                raise ValueError('Unknown personalization mode')
            if self.thread is not None and self.thread.is_alive():
                raise ValueError('Wait for the previous training job to stop')
            if mode == 'retrain' and not confirmed:
                raise ValueError('Confirm local image recording and personal training first')
            self.cancel()
            self.cancel_event = threading.Event()
            self.wizard = Wizard(mode)
            self.job_id = str(time.time_ns())
            self.state, self.error, self.metrics = 'collecting', '', None
            self.progress = 0.

    def cancel(self):
        with self.lock:
            self.generation += 1
            self.cancel_event.set()
            self.wizard = self.pending = None
            if self.state in ('collecting', 'training', 'applying'):
                self.state = 'cancelled'

    def restore(self):
        with self.lock:
            head_changed = self.profile['head'] is not None
            self.cancel()
            self.persist(self.standard())
            self.state, self.error, self.metrics = 'restored', '', None
            self.changed(head_changed)

    def feed(self, image, raw, header):
        if self.wizard is None:
            return
        try:
            samples = self.wizard.feed(image, raw, header)
            if samples is None:
                return
            mode = self.wizard.mode
            self.wizard = None
            if mode == 'calibrate':
                profile = dict(self.profile, calibration=calibration_from_samples(samples))
                self.persist(profile)
                self.state = 'calibrated'
                self.changed(False)
            else:
                self.state, self.progress = 'training', 0.
                token = self.generation
                self.thread = threading.Thread(target=self._train,
                                               args=(samples, token, self.cancel_event, self.job_id,
                                                     self.checkpoint, self.profile['head'] or self.checkpoint),
                                               name='tongue-personal-training', daemon=True)
                self.thread.start()
        except (ValueError, OSError) as error:
            self.wizard = None
            self.state, self.error = 'failed', str(error)

    def _train(self, samples, token, cancelled, job_id, checkpoint, current):
        try:
            from personal_training import train_head
            trainer = self.trainer or train_head
            job = self.root/'jobs'/job_id
            job.mkdir(parents=True, exist_ok=False)
            def progress(value):
                with self.lock:
                    if token == self.generation:
                        self.progress = value
            result = trainer(samples, checkpoint, current, job, cancelled, progress)
            with self.lock:
                if token != self.generation or cancelled.is_set():
                    return
                self.metrics = result['metrics']
                if not result['accepted']:
                    self.state, self.progress = 'rejected', 1.
                    return
                profile = dict(checkpoint=checkpoint, head=result['checkpoint'], calibration=result['calibration'])
                self.pending = dict(profile=profile, generation=token)
                self.state, self.progress = 'applying', .98
        except Exception as error:
            with self.lock:
                if token == self.generation and not cancelled.is_set():
                    self.state, self.error = 'failed', str(error)

    def commit_pending(self, request):
        if request is not self.pending or request['generation'] != self.generation or self.cancel_event.is_set():
            return False
        self.persist(request['profile'])
        self.pending = None
        self.state, self.progress = 'accepted', 1.
        self.changed(True)
        return True

    def fail_pending(self, request, error):
        if request is self.pending:
            self.pending = None
            self.state, self.error = 'failed', str(error)

    def status(self):
        return dict(state=self.state, job_id=self.job_id, wizard=self.wizard.status() if self.wizard else None,
                    progress=self.progress, error=self.error, metrics=copy.deepcopy(self.metrics),
                    profile=copy.deepcopy(self.profile), training_worker_alive=bool(self.thread and self.thread.is_alive()))
