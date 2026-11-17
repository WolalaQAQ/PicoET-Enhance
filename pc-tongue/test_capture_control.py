"""Capture orchestration contracts; these tests never access a headset."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend import make_server
from capture_control import CaptureControl
from test_face_runtime import FaceManualRuntime
from test_runtime import header, jpeg, wait_for


class CaptureControlTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Mock()
        self.runtime.connected = True
        self.runtime.running = False
        self.runtime.last_error = None
        self.runtime.record_status = dict(state='recording', guide=dict(phase='prepare'))
        self.runtime.status.return_value = dict(recording=dict(state='idle'))
        self.source = Mock()
        self.source.start.return_value = dict(host='127.0.0.1', port=45681)
        self.control = CaptureControl(self.runtime, self.source)
        self.addCleanup(self.control.close)

    def begin(self):
        called = threading.Event()
        self.runtime.recording.side_effect = lambda *a, **k: called.set()
        self.control.recording('begin', confirmed=True, person_id='fixture', guide=True,
                               preset_id='basics', automatic=True)
        self.assertTrue(called.wait(3))

    def test_consent_and_preset_checked_before_source(self):
        with self.assertRaises(ValueError):
            self.control.recording('begin', confirmed=False, person_id='fixture')
        with self.assertRaises(ValueError):
            self.control.recording('begin', confirmed=True, person_id='fixture', preset_id='missing')
        self.source.start.assert_not_called()

    def test_one_click_starts_capture_without_a_model_and_finishes_owned_source(self):
        self.begin()
        self.runtime.configure.assert_called_once_with(dict(headset_host='127.0.0.1', headset_port=45681,
            model_kind='face50'))
        self.runtime.start.assert_called_once_with('capture')
        self.control.stop()
        self.control.thread.join(3)
        self.assertFalse(self.control.thread.is_alive())
        self.source.stop.assert_called_once()
        self.runtime.stop.assert_called_once_with(cancel_recording=False)
        self.assertEqual(self.control.state, 'completed')

    def test_last_step_ends_without_agent_or_another_click(self):
        self.begin()
        self.runtime.record_status['guide']['phase'] = 'finished'
        self.control.thread.join(3)
        self.assertEqual(self.control.state, 'completed')
        self.source.stop.assert_called_once()

    def test_cancel_keeps_runtime_cancel_semantics(self):
        self.begin()
        self.control.recording('cancel')
        self.control.thread.join(3)
        self.runtime.stop.assert_called_once_with(cancel_recording=True)
        self.assertEqual(self.control.state, 'cancelled')

    def test_start_failure_still_closes_source_and_runtime(self):
        self.source.start.side_effect = RuntimeError('fixture unavailable')
        self.control.recording('begin', confirmed=True, person_id='fixture')
        self.control.thread.join(3)
        self.assertEqual(self.control.state, 'error')
        self.assertIn('fixture unavailable', self.control.error)
        self.source.stop.assert_called_once()
        self.runtime.stop.assert_called_once()

    def test_pause_resume_and_skip_are_ui_actions(self):
        for action in ('pause', 'resume', 'skip'):
            self.control.recording(action)
            self.runtime.recording.assert_called_with(action)

    def test_existing_input_is_not_replaced_by_recording(self):
        self.runtime.running = True
        with self.assertRaisesRegex(ValueError, 'Close the current input'):
            self.control.recording('begin', confirmed=True, person_id='fixture')
        self.source.start.assert_not_called()

    def test_idle_stop_also_stops_manually_opened_input(self):
        self.control.stop()
        self.runtime.stop.assert_called_once_with()


class ManagedHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = FaceManualRuntime(Path(self.temp.name)/'config.json')
        self.source = Mock()
        self.source.start.return_value = dict(host='127.0.0.1', port=45681)
        self.control = CaptureControl(self.runtime, self.source, display_viewer='headset')
        self.server = make_server(self.runtime, port=0, capture=self.control)
        self.worker = threading.Thread(target=self.server.serve_forever)
        self.worker.start()
        self.addCleanup(self.close)

    def close(self):
        self.control.close()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()
        self.runtime.udp.close()

    def request(self, path, body=None):
        url = f'http://127.0.0.1:{self.server.server_port}'+path
        request = Request(url, json.dumps(body).encode(), {'Content-Type': 'application/json'}) if body is not None else url
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def begin(self):
        self.request('/api/recording', dict(action='begin', person_id='fixture', confirmed=True,
                                          preset_id='basics', automatic=True))
        wait_for(lambda: self.control.state == 'recording')
        return self.runtime.recorder

    def test_http_start_display_pause_skip_automatic_finish_and_complete_archive(self):
        self.assertEqual(self.request('/api/status')['capture_control']['state'], 'idle')
        self.source.start.assert_not_called()
        recorder = self.begin()
        guide = recorder.guide
        receipt = dict(session_id=recorder.session_id, frame=guide.frame, phase=guide.phase,
                       visible=True, media_ready=True)
        self.assertFalse(self.request('/api/recording/display', dict(receipt, viewer='desktop'))['accepted'])
        self.assertFalse(guide.display_ready())
        self.assertTrue(self.request('/api/recording/display', dict(receipt, viewer='headset'))['accepted'])
        self.request('/api/recording', dict(action='pause'))
        self.assertTrue(guide.paused)
        self.request('/api/recording', dict(action='resume'))
        for _ in range(guide.status()['total']-1):
            self.request('/api/recording', dict(action='skip'))
        # Finish the final real step through its clock, display and frame conditions.
        now = [0.]
        guide.clock = lambda: now[0]
        payload = jpeg()
        for seq in range(1, 90):
            now[0] = seq*.1
            self.request('/api/recording/display', dict(session_id=recorder.session_id,
                frame=guide.frame, phase=guide.phase, viewer='headset', visible=True, media_ready=True))
            self.runtime._publish(replace(header(seq), payload_bytes=len(payload),
                mouth_sensor_ns=seq*100_000_000), payload, time.perf_counter_ns())
            wait_for(lambda: len(recorder.images) >= seq or self.control.state == 'completed')
            if self.control.state == 'completed':
                break
        self.control.thread.join(5)
        self.assertEqual(self.control.state, 'completed')
        self.assertFalse(self.runtime.running)
        self.source.stop.assert_called_once()
        manifest = json.loads((recorder.path/'manifest.json').read_text())
        self.assertTrue(manifest['samples_complete'])
        self.assertEqual(manifest['state'], 'stopped')
        self.assertEqual(manifest['trainable'], 0)
        self.assertEqual(len(guide.completed), 1)
        self.assertEqual(len(guide.skipped), 11)
        self.assertTrue(all(all(v is None for v in row['targets']) for row in recorder.images))
        self.assertTrue(all((recorder.path/row['image']).read_bytes() == payload for row in recorder.images))

    def test_active_managed_capture_rejects_reconfiguration_and_preserves_cancelled_archive(self):
        recorder = self.begin()
        with self.assertRaises(HTTPError) as error:
            self.request('/api/config', dict(model_path='another-model.onnx'))
        self.assertEqual(error.exception.code, 400)
        payload = jpeg()
        self.runtime._publish(replace(header(1), payload_bytes=len(payload)), payload,
                              time.perf_counter_ns())
        wait_for(lambda: bool(recorder.images))
        self.request('/api/recording', dict(action='cancel'))
        self.control.thread.join(5)
        self.assertEqual(self.control.state, 'cancelled')
        self.assertEqual(json.loads((recorder.path/'manifest.json').read_text())['state'], 'cancelled')
        self.assertTrue(next((recorder.path/'images').glob('*.jpg')).is_file())

    def test_archive_join_has_no_five_second_cutoff(self):
        self.runtime.record_thread = Mock()
        self.runtime.recorder = Mock()
        self.runtime.recorder.status.return_value = dict(state='stopped')
        writer = self.runtime.record_thread
        self.runtime._finish_recording()
        writer.join.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
