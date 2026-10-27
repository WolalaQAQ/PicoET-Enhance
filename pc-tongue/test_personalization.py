"""Calibration and profile lifecycle checks using controlled inputs, never a headset."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import numpy as np

from personalization import (DEFAULT_CALIBRATION, Personalization, Wizard, apply_calibration,
                             calibration_from_samples, valid_calibration)
from backend import make_server
from test_runtime import FakeModel, ManualRuntime, header, jpeg, wait_for


def samples():
    return [dict(label=label, raw_angle=angle, area=area)
            for label, angle, area in ((1, None, 0), (2, .65, 30), (3, .66, 200), (4, .33, 200), (5, 1.11, 200))
            for _ in range(12)]


def run_wizard(mode):
    wizard = Wizard(mode)
    for index in range(1200):
        angle = {'center': .66, 'left': .33, 'right': 1.11}.get(wizard.step)
        shade = {'fit': 100, 'closed': 100, 'open': 110, 'center': 120, 'left': 130, 'right': 140}[wizard.step]
        raw = dict(nose_area=5000, raw_angle=angle, mask_area=200 if angle else 0,
                   mask=np.zeros((56, 56), bool))
        result = wizard.feed(np.full((400, 400), shade, np.uint8), raw,
                             replace(header(index+1), mouth_sensor_ns=(index+1)*50_000_000))
        if result is not None:
            return wizard, result
    raise AssertionError('Wizard did not finish')


class CalibrationTests(unittest.TestCase):
    def test_geometry_endpoints_deadzone_and_mirrored_order(self):
        for cal in (DEFAULT_CALIBRATION, dict(left=1.11, center=.66, right=.33, min_area=100)):
            self.assertTrue(valid_calibration(cal))
            for key, left, right in (('left', 1., 0.), ('center', 0., 0.), ('right', 0., 1.)):
                result = apply_calibration(dict(raw_angle=cal[key], mask_area=100), cal)
                self.assertAlmostEqual(result['left'], left)
                self.assertAlmostEqual(result['right'], right)
                self.assertTrue(result['present'])

    def test_hysteresis_requires_an_existing_positive_and_releases_below_60_percent(self):
        raw = dict(raw_angle=.5, mask_area=60)
        self.assertFalse(apply_calibration(raw, DEFAULT_CALIBRATION)['present'])
        self.assertTrue(apply_calibration(raw, DEFAULT_CALIBRATION, active=True)['size_held'])
        self.assertFalse(apply_calibration(raw, DEFAULT_CALIBRATION, active=True, hysteresis=False)['present'])
        self.assertFalse(apply_calibration(dict(raw, mask_area=59), DEFAULT_CALIBRATION, active=True)['present'])
        self.assertFalse(apply_calibration(dict(raw, raw_angle=None), DEFAULT_CALIBRATION, active=True)['present'])

    def test_calibration_threshold_and_direction_validation(self):
        cal = calibration_from_samples(samples())
        self.assertEqual(cal, dict(DEFAULT_CALIBRATION, min_area=98))
        bad = [dict(s, raw_angle=.66) if s['label'] >= 3 else s for s in samples()]
        with self.assertRaisesRegex(ValueError, 'not separated'):
            calibration_from_samples(bad)
        with self.assertRaisesRegex(ValueError, 'Not enough visible'):
            calibration_from_samples([])

    def test_complete_calibration_and_retraining_guides(self):
        for mode, expected in (('calibrate', [30, 30, 24, 40, 40]), ('retrain', [60]*5)):
            with self.subTest(mode=mode):
                wizard, rows = run_wizard(mode)
                self.assertTrue(wizard.finished)
                self.assertEqual([sum(s['label'] == i for s in rows) for i in range(1, 6)], expected)
                self.assertEqual(len({s['source_seq'] for s in rows}), len(rows))
                self.assertTrue(valid_calibration(calibration_from_samples(rows)))

    def test_duplicate_timestamps_and_fast_inference_do_not_advance_guide(self):
        wizard = Wizard('calibrate')
        image = np.zeros((400, 400), np.uint8)
        for index in range(5):
            wizard.feed(image, {'nose_area': 5000}, replace(header(index), mouth_sensor_ns=100_000_000+index*10_000_000))
        self.assertEqual(wizard.count, 1)
        wizard.feed(image, {'nose_area': 5000}, replace(header(8), mouth_sensor_ns=500_000_000))
        self.assertEqual(wizard.count, 1)
        wizard.feed(image, {'nose_area': 0}, replace(header(9), mouth_sensor_ns=550_000_000))
        self.assertEqual(wizard.count, 0)
        self.assertEqual(wizard.hint, 'fit_low')

    def test_camera_session_change_aborts_guide(self):
        wizard = Wizard('calibrate')
        wizard.feed(np.zeros((400, 400), np.uint8), {'nose_area': 5000}, header())
        with self.assertRaisesRegex(ValueError, 'Camera session changed'):
            wizard.feed(np.zeros((400, 400), np.uint8), {}, header(session=2))


class ProfileTests(unittest.TestCase):
    def setUp(self):
        work = Path(__file__).resolve().parent/'tests-work'
        work.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.tmp.cleanup)
        self.lock = threading.RLock()
        self.changes = []
        self.personal = Personalization(Path(self.tmp.name)/'personalization', self.lock, self.changes.append)
        self.personal.set_checkpoint(str(Path(self.tmp.name)/'standard.pt'))

    def train(self, trainer):
        self.personal.trainer = trainer
        self.personal.begin('retrain', True)
        class Complete:
            mode = 'retrain'
            def feed(self, *args):
                return samples()
        self.personal.wizard = Complete()
        with self.lock:
            self.personal.feed(None, None, None)

    def candidate(self):
        return dict(accepted=True, metrics={'test_only': True}, calibration=dict(DEFAULT_CALIBRATION),
                    checkpoint=str(Path(self.tmp.name)/'personal.pt'))

    def test_retraining_requires_literal_consent(self):
        with self.assertRaisesRegex(ValueError, 'Confirm local image'):
            self.personal.begin('retrain')
        self.assertFalse(self.personal.root.exists())

    def test_independent_profile_persistence_and_restore_preserves_standard_and_job(self):
        standard = Path(self.personal.checkpoint)
        standard.write_bytes(b'unchanged standard')
        with self.lock:
            self.personal.persist(dict(self.personal.profile, head='personal.pt', calibration=dict(DEFAULT_CALIBRATION, min_area=120)))
        other = Personalization(self.personal.root, self.lock, self.changes.append)
        other.set_checkpoint(standard)
        self.assertEqual(other.profile, self.personal.profile)
        archive = self.personal.root/'jobs'/'kept'
        archive.mkdir(parents=True)
        self.personal.restore()
        self.assertIsNone(self.personal.profile['head'])
        self.assertEqual(self.personal.profile['calibration'], DEFAULT_CALIBRATION)
        self.assertEqual(standard.read_bytes(), b'unchanged standard')
        self.assertTrue(archive.exists())
        self.assertEqual(self.changes, [True])
        other.set_checkpoint(Path(self.tmp.name)/'another.pt')
        self.assertIsNone(other.profile['head'])

    def test_training_only_publishes_pending_until_inference_commit(self):
        self.train(lambda *args: self.candidate())
        self.personal.thread.join(2)
        self.assertEqual(self.personal.state, 'applying')
        self.assertIsNone(self.personal.profile['head'])
        self.assertFalse((self.personal.root/'active.json').exists())
        with self.lock:
            self.assertTrue(self.personal.commit_pending(self.personal.pending))
        self.assertEqual(self.personal.state, 'accepted')
        self.assertTrue(self.personal.profile['head'].endswith('personal.pt'))

    def test_cancelled_candidate_cannot_commit(self):
        self.train(lambda *args: self.candidate())
        self.personal.thread.join(2)
        request = self.personal.pending
        self.personal.cancel()
        with self.lock:
            self.assertFalse(self.personal.commit_pending(request))
        self.assertIsNone(self.personal.profile['head'])

    def test_restore_while_training_prevents_late_reactivation(self):
        entered, release = threading.Event(), threading.Event()
        def trainer(*args):
            entered.set()
            release.wait(2)
            return self.candidate()
        self.train(trainer)
        self.assertTrue(entered.wait(1))
        self.personal.restore()
        release.set()
        self.personal.thread.join(2)
        self.assertEqual(self.personal.state, 'restored')
        self.assertIsNone(self.personal.pending)
        self.assertIsNone(json.loads((self.personal.root/'active.json').read_text())['head'])

    def test_rejected_and_failed_training_keep_active_profile(self):
        for result in ('reject', 'error'):
            def trainer(*args):
                if result == 'error':
                    raise ValueError('test training failure')
                return dict(accepted=False, metrics={'accepted': False})
            self.train(trainer)
            self.personal.thread.join(2)
            self.assertEqual(self.personal.state, 'failed' if result == 'error' else 'rejected')
            self.assertIsNone(self.personal.profile['head'])
            self.assertIsNone(self.personal.pending)


class RuntimePersonalTests(unittest.TestCase):
    def setUp(self):
        work = Path(__file__).resolve().parent/'tests-work'
        work.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.tmp.cleanup)
        self.model = FakeModel()
        self.runtime = ManualRuntime(Path(self.tmp.name)/'config.json', lambda c: self.model, Path(self.tmp.name)/'runs')
        self.runtime.configure({'headset_host': '127.0.0.1', 'model_path': 'test-only'})
        def close():
            self.model.release.set()
            self.runtime.stop()
            self.runtime.udp.close()
        self.addCleanup(close)

    def start(self):
        self.runtime.start()
        wait_for(lambda: self.runtime.connected)
        self.submit(header())
        wait_for(lambda: self.runtime.counts['inferred'] == 1)

    def submit(self, h):
        payload = jpeg()
        self.runtime._publish(replace(h, payload_bytes=len(payload)), payload, time.perf_counter_ns())

    def pending(self):
        personal = self.runtime.personal
        personal.cancel_event.clear()
        personal.pending = dict(profile=dict(personal.profile, head='candidate.pt'), generation=personal.generation)
        personal.state = 'applying'
        return personal.pending

    def test_requires_fresh_live_source_and_explicit_consent(self):
        with self.assertRaisesRegex(ValueError, 'Start live input'):
            self.runtime.personalize('calibrate')
        self.start()
        with self.assertRaisesRegex(ValueError, 'Confirm local image'):
            self.runtime.personalize('retrain', 1)
        self.assertEqual(self.runtime.personalize('calibrate')['state'], 'collecting')
        self.runtime.personalize('cancel')
        self.runtime.mode = 'replay'
        with self.assertRaisesRegex(ValueError, 'Start live input'):
            self.runtime.personalize('calibrate')

    def test_camera_change_cancels_collection_and_stale_input_is_not_recorded(self):
        self.start()
        self.runtime.personalize('calibrate')
        self.submit(header(11, age_ms=1000))
        wait_for(lambda: self.runtime.counts['stale'] > 0)
        self.assertEqual(self.runtime.personal.wizard.count, 0)
        self.submit(header(1, session=2))
        wait_for(lambda: self.runtime.personal.wizard is None)
        self.assertEqual(self.runtime.personal.state, 'cancelled')

    def test_candidate_failure_keeps_old_model_and_profile(self):
        self.pending()
        self.runtime.model = self.model
        self.runtime.model_factory = lambda c: (_ for _ in ()).throw(ValueError('test bad candidate'))
        self.runtime._activate_personal_profile()
        self.assertIs(self.runtime.model, self.model)
        self.assertIsNone(self.runtime.personal.profile['head'])
        self.assertIsNone(self.runtime.personal.pending)
        self.assertEqual(self.runtime.personal.state, 'failed')

    def test_candidate_pointer_write_failure_keeps_old_session(self):
        self.runtime.stop_event.clear()
        self.pending()
        self.runtime.model = self.model
        with patch.object(self.runtime.personal, 'persist', side_effect=OSError('test disk failure')):
            self.runtime._activate_personal_profile()
        self.assertIs(self.runtime.model, self.model)
        self.assertIsNone(self.runtime.personal.profile['head'])
        self.assertEqual(self.runtime.personal.state, 'failed')

    def test_restore_during_candidate_construction_prevents_commit(self):
        self.pending()
        def factory(config):
            self.runtime.personalize('restore')
            return FakeModel()
        self.runtime.model_factory = factory
        self.runtime._activate_personal_profile()
        self.assertIsNone(self.runtime.personal.profile['head'])
        self.assertEqual(self.runtime.personal.state, 'restored')

    def test_pending_activation_warms_up_before_commit_and_changes_output_epoch(self):
        self.runtime.stop_event.clear()
        self.pending()
        epoch = self.runtime.output_session_id
        self.runtime._activate_personal_profile()
        self.assertEqual(self.runtime.personal.state, 'accepted')
        self.assertTrue(self.runtime.description['personal_head'])
        self.assertNotEqual(epoch, self.runtime.output_session_id)

    def test_restore_discards_inflight_old_prediction(self):
        self.start()
        self.model.entered.clear()
        self.model.block = True
        self.submit(header(11))
        self.assertTrue(self.model.entered.wait(1))
        self.runtime.personalize('restore')
        self.model.release.set()
        wait_for(lambda: self.runtime.counts['superseded'] == 1)
        self.assertEqual(self.runtime.prediction['state'], 'profile_changed')
        self.assertIsNone(self.runtime.preview)

    def test_restore_discards_inflight_old_preview(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def preview(*args):
            entered.set()
            release.wait(2)
            return b'old-preview'
        self.runtime._preview_image = preview
        self.start()
        self.assertTrue(entered.wait(1))
        original_log = self.runtime.log
        class PublicationObserved:
            def write(self, text):
                original_log.write(text)
                completed.set()  # Log follows the preview-publication boundary.
        self.runtime.log = PublicationObserved()
        self.runtime.personalize('restore')
        release.set()
        try:
            self.assertTrue(completed.wait(1))
            self.assertIsNone(self.runtime.preview)
        finally:
            self.runtime.log = original_log

    def test_restore_waits_for_standard_session_before_processing_new_frames(self):
        self.start()
        with self.runtime.lock:
            self.pending()
        wait_for(lambda: self.runtime.personal.state == 'accepted')
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def factory(config):
            entered.set()
            release.wait(2)
            return FakeModel()
        self.runtime.model_factory = factory
        self.runtime.personalize('restore')
        self.assertTrue(entered.wait(1))
        self.submit(header(11))
        self.assertEqual(self.runtime.counts['inferred'], 1)
        self.assertEqual(self.runtime.prediction['state'], 'profile_changed')
        release.set()
        wait_for(lambda: self.runtime.counts['inferred'] == 2)
        self.assertFalse(self.runtime.description['personal_head'])

    def test_stop_joins_owned_training_worker(self):
        cancelled = threading.Event()
        self.runtime.personal.cancel_event = cancelled
        worker = threading.Thread(target=lambda: cancelled.wait(2))
        self.runtime.personal.thread = worker
        worker.start()
        self.runtime.stop()
        self.assertFalse(worker.is_alive())

    def test_stale_completion_clears_hysteresis_history(self):
        self.start()
        self.model.entered.clear()
        self.model.block = True
        self.submit(header(11))
        self.assertTrue(self.model.entered.wait(1))
        with self.runtime.lock:
            self.runtime.clock['offset_ns'] = 1_000_000_000
        self.model.release.set()
        wait_for(lambda: self.runtime.counts['inferred'] == 2)
        self.assertIsNone(self.runtime.tongue.visual)
        self.assertEqual(self.runtime.prediction['state'], 'stale')

    def test_http_personalization_consent_cancel_restore_and_idle_guard(self):
        server = make_server(self.runtime, port=0)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_port}/api/personalization'
        def post(body):
            return urlopen(Request(url, json.dumps(body).encode(), {'Content-Type': 'application/json'}))
        with self.assertRaises(HTTPError) as failure:
            post({'action': 'calibrate'})
        self.assertEqual(failure.exception.code, 400)
        failure.exception.close()
        self.start()
        for consent in (False, 'true', 1):
            with self.assertRaises(HTTPError) as failure:
                post({'action': 'retrain', 'confirmed': consent})
            self.assertEqual(failure.exception.code, 400)
            failure.exception.close()
        with post({'action': 'retrain', 'confirmed': True}) as response:
            self.assertEqual(json.load(response)['state'], 'collecting')
        with post({'action': 'cancel'}) as response:
            self.assertEqual(json.load(response)['state'], 'cancelled')
        with post({'action': 'restore'}) as response:
            self.assertIsNone(json.load(response)['profile']['head'])
        self.assertTrue(self.runtime.running)
        self.assertFalse(self.runtime.config['output_enabled'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
