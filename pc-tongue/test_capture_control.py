"""Capture orchestration contracts; these tests never access a headset."""
from dataclasses import replace
import json
from pathlib import Path
import socket
import socketserver
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend import main, make_server
from capture_control import CaptureControl, LANCameraSource
from discovery import DISCOVER, discover_sources
from runtime import Runtime
from stock import CaptureContext
from test_face_runtime import FaceManualRuntime
from test_runtime import header, jpeg, wait_for


class LoopbackCamera:
    """Generated JPEGs through the real discovery/context/clock/image protocol."""
    def __init__(self):
        self.stopped = threading.Event()
        self.commands = []
        self.active_images = 0
        self.payload = jpeg()
        owner = self
        context = CaptureContext(70001, 99).pack()

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                command = bytearray()
                while not command.endswith(b'\n'):
                    part = self.request.recv(1)
                    if not part:
                        return
                    command.extend(part)
                owner.commands.append(bytes(command))
                if command == b'CONTEXT\n':
                    self.request.sendall(context)
                elif command == b'PING\n':
                    now = time.perf_counter_ns()
                    self.request.sendall(struct.pack('<QQ', now, now))
                elif command == b'JPEG\n':
                    owner.active_images += 1
                    try:
                        seq = 0
                        while not owner.stopped.wait(.02):
                            seq += 1
                            frame = replace(header(seq, 70001), payload_bytes=len(owner.payload),
                                            mouth_sensor_ns=time.perf_counter_ns())
                            self.request.sendall(frame.pack()+owner.payload)
                    except OSError:
                        pass
                    finally:
                        owner.active_images -= 1
                elif command == b'LABELS\n':
                    self.request.recv(1)

        self.tcp = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
        self.tcp.daemon_threads = True
        self.endpoint = self.tcp.server_address
        self.tcp_thread = threading.Thread(target=self.tcp.serve_forever, kwargs={'poll_interval': .01})
        self.tcp_thread.start()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(self.endpoint)
        self.udp.settimeout(.1)

        def answer():
            while not self.stopped.is_set():
                try:
                    data, peer = self.udp.recvfrom(256)
                except socket.timeout:
                    continue
                if data == DISCOVER:
                    self.udp.sendto(context, peer)

        self.udp_thread = threading.Thread(target=answer)
        self.udp_thread.start()

    def discover(self):
        return discover_sources(.05, [self.endpoint])

    def close(self):
        self.stopped.set()
        self.udp_thread.join()
        self.udp.close()
        self.tcp.shutdown()
        self.tcp_thread.join()
        self.tcp.server_close()


class CaptureControlTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Mock()
        self.runtime.connected = True
        self.runtime.running = False
        self.runtime.config = dict(model_kind='face50')
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

    def test_capture_preview_is_reused_without_reconnecting_or_starting_inference(self):
        self.runtime.running = self.runtime.capture_only = True
        self.runtime.mode = 'live'
        self.begin()
        self.source.start.assert_not_called()
        self.runtime.configure.assert_not_called()
        self.runtime.start.assert_not_called()
        self.control.stop()
        self.control.thread.join(3)
        self.runtime.stop.assert_called_once_with(cancel_recording=False)
        self.assertEqual(self.control.state, 'completed')

    def test_replay_is_not_reused_as_live_capture(self):
        self.runtime.running = self.runtime.capture_only = True
        self.runtime.mode = 'replay'
        with self.assertRaisesRegex(ValueError, 'Close the current input'):
            self.control.recording('begin', confirmed=True, person_id='fixture')
        self.source.start.assert_not_called()

    def test_explicitly_running_live_model_can_still_be_recorded_without_model_changes(self):
        self.runtime.running = True
        self.runtime.capture_only = False
        self.runtime.mode = 'live'
        self.begin()
        self.source.start.assert_not_called()
        self.runtime.configure.assert_not_called()
        self.runtime.start.assert_not_called()
        self.runtime.prepare.assert_not_called()

    def test_idle_stop_also_stops_manually_opened_input(self):
        self.control.stop()
        self.runtime.stop.assert_called_once_with()


class LANCameraSourceTests(unittest.TestCase):
    def test_existing_service_uses_the_shared_discovery_protocol_without_device_control(self):
        runtime = Mock(config=dict(headset_host='192.0.2.10', headset_port=45681))
        source = LANCameraSource(runtime)
        with patch('capture_control.discover_sources',
                   return_value=dict(devices=[dict(host='192.0.2.10', port=45681)])) as discovery:
            self.assertEqual(source.start(), dict(host='192.0.2.10', port=45681))
            discovery.assert_called_once_with(targets=[('192.0.2.10', 45681)])
        source.stop()
        runtime.stop.assert_not_called()
        runtime.configure.assert_not_called()

    def test_unselected_source_uses_discovery_and_multiple_devices_require_selection(self):
        source = LANCameraSource(Mock(config={}))
        with patch('capture_control.discover_sources', return_value=dict(devices=[])) as discovery:
            with self.assertRaises(ValueError):
                source.start()
            discovery.assert_called_once_with(targets=None)
        with patch('capture_control.discover_sources', return_value=dict(devices=[{}, {}])):
            with self.assertRaises(ValueError):
                source.start()


class EntryPointTests(unittest.TestCase):
    def test_normal_backend_entrypoint_enables_self_service_without_starting_input(self):
        runtime, server = Mock(), Mock()
        runtime.running = False
        runtime.status.return_value = dict(running=False)
        server.server_port = 8787
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch('backend.Runtime', return_value=runtime), \
                patch('backend.make_server', return_value=server) as make, \
                patch('capture_control.discover_sources') as discover:
            main(['--port', '8787'])
        capture = make.call_args.kwargs['capture']
        self.assertIsInstance(capture, CaptureControl)
        self.assertIsInstance(capture.source, LANCameraSource)
        self.assertIsNone(capture.display_viewer)
        runtime.start.assert_not_called()
        discover.assert_not_called()
        runtime.stop.assert_called_once_with()
        runtime.train.assert_called_once_with('cancel')
        runtime.udp.close.assert_called_once_with()
        server.server_close.assert_called_once_with()

    def test_normal_entrypoint_discovers_previews_records_saves_and_reviews_without_a_model(self):
        camera = LoopbackCamera()
        self.addCleanup(camera.close)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ready, servers, runtimes = threading.Event(), [], []

            def create(runtime, **options):
                server = make_server(runtime, discover=camera.discover, **options)
                servers.append(server)
                runtimes.append(runtime)
                ready.set()
                return server

            with patch('backend.make_server', side_effect=create):
                worker = threading.Thread(target=main, args=(['--port', '0', '--config', str(root/'config.json')],))
                worker.start()
                self.assertTrue(ready.wait(5))
                server, runtime = servers[0], runtimes[0]
                base = f'http://127.0.0.1:{server.server_port}'

                def request(path, body=None):
                    req = Request(base+path, json.dumps(body).encode(), {'Content-Type': 'application/json'}) if body is not None else base+path
                    with urlopen(req, timeout=10) as response:
                        return json.load(response)

                try:
                    self.assertTrue(request('/api/status')['managed_capture'])
                    device, = request('/api/discovery')['devices']
                    self.assertFalse(runtime.running)
                    self.assertNotIn(b'JPEG\n', camera.commands)
                    request('/api/config', dict(headset_host=device['host'], headset_port=device['port']))
                    request('/api/start', dict(mode='capture'))
                    wait_for(lambda: runtime.counts.get('decoded', 0) > 0)
                    self.assertIsNone(runtime.recorder)
                    request('/api/recording', dict(action='begin', person_id='generated', confirmed=True, guide=False))
                    wait_for(lambda: runtime.recorder and len(runtime.recorder.images) >= 3)
                    run = runtime.run_dir
                    request('/api/recording', dict(action='stop'))
                    wait_for(lambda: request('/api/status')['capture_control']['state'] == 'completed')
                    wait_for(lambda: camera.active_images == 0)
                    self.assertEqual(camera.commands.count(b'JPEG\n'), 1)
                    self.assertEqual(runtime.run_dir, run)
                    self.assertFalse(runtime.running)
                    self.assertEqual(runtime.counts['inferred'], 0)
                    review = request('/api/recording/review?all=true')
                    self.assertGreaterEqual(review['total'], 3)
                    sample = review['samples'][0]
                    with urlopen(base+'/api/recording/image?session='+review['session_id']+'&sample='+sample['sample_id']) as image:
                        self.assertEqual(image.read(), camera.payload)
                    manifest = json.loads((runtime.recorder.path/'manifest.json').read_text())
                    self.assertTrue(manifest['samples_complete'])
                    self.assertEqual(manifest['trainable'], 0)
                    self.assertTrue(all(all(v is None for v in row['targets']) for row in runtime.recorder.images))
                finally:
                    server.shutdown()
                    worker.join(10)
                    self.assertFalse(worker.is_alive())


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

    def test_preview_then_record_save_and_review_uses_the_same_live_input(self):
        self.runtime.configure(dict(headset_host='127.0.0.1'))
        self.request('/api/start', dict(mode='capture'))
        wait_for(lambda: self.runtime.connected)
        run = self.runtime.run_dir
        self.assertEqual(self.request('/api/recording/review')['total'], 0)
        self.request('/api/recording', dict(action='begin', person_id='fixture', confirmed=True, guide=False))
        wait_for(lambda: self.control.state == 'recording')
        self.source.start.assert_not_called()
        self.assertEqual(self.runtime.run_dir, run)
        payload = jpeg()
        self.runtime._publish(replace(header(1), payload_bytes=len(payload)), payload, time.perf_counter_ns())
        wait_for(lambda: bool(self.runtime.recorder.images))
        self.request('/api/recording', dict(action='stop'))
        self.control.thread.join(5)
        self.assertEqual(self.control.state, 'completed')
        self.assertFalse(self.runtime.running)
        self.assertEqual(self.runtime.counts['inferred'], 0)
        review = self.request('/api/recording/review?all=true')
        self.assertEqual(review['total'], 1)
        sample, = review['samples']
        self.assertTrue(all(value is None for value in self.runtime.recorder.images[0]['targets']))
        with urlopen(f'http://127.0.0.1:{self.server.server_port}/api/recording/image?'
                     f'session={review["session_id"]}&sample={sample["sample_id"]}') as image:
            self.assertEqual(image.read(), payload)

    def test_archive_join_has_no_five_second_cutoff(self):
        self.runtime.record_thread = Mock()
        self.runtime.recorder = Mock()
        self.runtime.recorder.status.return_value = dict(state='stopped')
        writer = self.runtime.record_thread
        self.runtime._finish_recording()
        writer.join.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
