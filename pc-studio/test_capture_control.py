"""Capture orchestration contracts; these tests never access a headset."""
from dataclasses import replace
import json
from pathlib import Path
from queue import Queue
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
from face_dataset import load_dataset
from face_dataset import assert_no_split_leakage
from face_capture import WholeMouthRecording
from face_presets import capture_continuation
from face_schema import INDEX
from face_weak_supervision import choose_mapping, derive_row, pair_rejections
from runtime import Runtime
from stock import CaptureContext, StockRecord
from test_face_runtime import FaceManualRuntime
from test_runtime import header, jpeg, wait_for


class LoopbackCamera:
    """Generated JPEGs and optional PSL1 records through the real LAN protocol."""
    def __init__(self, *, send_stock=False, release_frames=None):
        self.stopped = threading.Event()
        self.commands = []
        self.active_images = 0
        self.payload = jpeg()
        self.cut_next_frame = threading.Event()
        labels_open = threading.Event()
        sample_frames = Queue()
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
                        if release_frames is not None and not release_frames.wait(5):
                            return
                        seq = 0
                        while not owner.stopped.wait(.02):
                            seq += 1
                            frame = replace(header(seq, 70001), payload_bytes=len(owner.payload),
                                            mouth_sensor_ns=time.perf_counter_ns())
                            if owner.cut_next_frame.is_set():
                                owner.cut_next_frame.clear()
                                self.request.sendall(frame.pack()+owner.payload[:47])
                                return
                            self.request.sendall(frame.pack()+owner.payload)
                            if send_stock and seq == 1:
                                # Preview gets a frame, then leaves the four test frames for recording.
                                if not labels_open.wait(5):
                                    return
                            if send_stock and 2 <= seq <= 5:
                                sample_frames.put(frame)
                    except OSError:
                        pass
                    finally:
                        owner.active_images -= 1
                elif command == b'LABELS\n':
                    if send_stock:
                        labels_open.set()
                        exact, wrong_identity, low_quality, _ = (sample_frames.get(timeout=5) for _ in range(4))
                        for frame, raw_id, quality in (
                            (low_quality, low_quality.raw_identity, .05),
                            (wrong_identity, wrong_identity.raw_identity+1, .9),
                            (exact, exact.raw_identity, .9),
                        ):
                            stock = StockRecord(frame.session_id, 99, frame.source_seq, frame.mouth_sensor_ns,
                                [.25]*33, [.5]*33, [.75]*33, [quality, 1.], True, association_valid=True,
                                stock_input_ns=frame.mouth_sensor_ns, vendor_ns=frame.vendor_ns, raw_identity=raw_id,
                                label_seq=frame.source_seq, warmup=False, instant_preclamp33=[.4]*33)
                            self.request.sendall(stock.pack())
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
        self.record_from_normal_entry(receive_results=False)

    def test_normal_entrypoint_archives_only_exact_stock_and_audits_raw_supervision(self):
        self.record_from_normal_entry(receive_results=True)

    def test_sealed_mode_owns_a_new_directory_and_never_binds_the_daily_port(self):
        with tempfile.TemporaryDirectory() as folder, patch('backend.Runtime') as runtime:
            new = Path(folder)/'new'
            for args in ([], ['--port', '0', '--config', 'daily.json'], ['--port', '0', '--continue-recording', 'old']):
                with self.assertRaises(SystemExit):
                    main(['--sealed-c', str(new), *args])
                self.assertFalse(new.exists())
            with self.assertRaises(SystemExit):
                main(['--sealed-c', folder, '--port', '0'])
            with self.assertRaises(SystemExit):
                main(['--sealed-c-profile', 'jaw', '--port', '0'])
            runtime.assert_not_called()

    def test_sealed_entrypoint_writes_role_before_frames_and_keeps_disconnect_partial(self):
        self.record_from_sealed_entry([], 'sealed-c', [
            'neutral', *(f'c_{o}_{level}' for o in ('small', 'large') for level in ('none', 'weak', 'medium', 'strong')),
            'negative_teeth', 'negative_speech', 'neutral'])

    def test_sealed_jaw_entrypoint_archives_results_without_inference_or_output(self):
        self.record_from_sealed_entry(['--sealed-c-profile', 'jaw'], 'sealed-c-jaw', [
            'c_jaw_closed_relaxed', 'c_jaw_open_small_1', 'c_jaw_closed_pucker', 'c_jaw_open_large_1',
            'c_jaw_closed_smile_left', 'c_jaw_open_small_2', 'c_jaw_closed_smile_right',
            'c_jaw_open_large_2', 'c_jaw_closed_relaxed_end'])

    def record_from_sealed_entry(self, profile_flags, preset_id, prompt_ids):
        from face_train import main as train
        from PIL import Image

        release_frames = threading.Event()
        camera = LoopbackCamera(send_stock=True, release_frames=release_frames)
        self.addCleanup(camera.close)
        with tempfile.TemporaryDirectory() as folder, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as output_sink:
            output_sink.bind(('127.0.0.1', 0))
            output_sink.settimeout(.1)
            root = Path(folder)/'fixture-only'
            ready, servers = threading.Event(), []

            def create(runtime, **options):
                server = make_server(runtime, discover=camera.discover, **options)
                servers.append(server)
                ready.set()
                return server

            with patch('backend.make_server', side_effect=create):
                worker = threading.Thread(target=main, args=(['--port', '0', '--sealed-c', str(root), *profile_flags],))
                worker.start()
                self.assertTrue(ready.wait(5))
                server = servers[0]
                base = f'http://127.0.0.1:{server.server_port}'

                def request(path, body=None, code=200):
                    url = base+path
                    if body is not None:
                        url = Request(url, json.dumps(body).encode(), {'Content-Type': 'application/json'})
                    if code != 200:
                        with self.assertRaises(HTTPError) as error:
                            urlopen(url, timeout=5)
                        self.assertEqual(error.exception.code, code)
                        with error.exception as response:
                            return json.load(response)
                    with urlopen(url, timeout=5) as response:
                        return response.read() if path == '/' or path.startswith('/static/') else json.load(response)

                try:
                    self.assertIn(b'id="record-start"', request('/'))
                    catalog = request('/api/presets')
                    plan, = catalog['presets']
                    self.assertTrue(catalog['sealed_c'])
                    self.assertEqual(catalog['default'], preset_id)
                    self.assertEqual([step['id'] for step in plan['steps']], prompt_ids)
                    refs = {pose['id']: pose for pose in
                            json.loads(request('/static/guide-reference/manifest.json'))['poses']}
                    for step in plan['steps']:
                        self.assertFalse(step['labels'])
                        demo = refs[step.get('demo_id', step['id'])]
                        if demo['front']:
                            self.assertTrue(request('/static/guide-reference/'+demo['front']))
                    status = request('/api/status')
                    self.assertTrue(status['capture_control']['sealed_c'])
                    self.assertEqual(status['capture_control']['preset_id'], preset_id)
                    self.assertEqual(status['capture_control']['state'], 'idle')
                    self.assertFalse(status['running'])
                    self.assertEqual(list(root.iterdir()), [])
                    device, = request('/api/discovery')['devices']
                    config = request('/api/config', dict(headset_host=device['host'], headset_port=device['port'],
                        enabled=True, output_enabled=True, output_host='127.0.0.1', output_port=output_sink.getsockname()[1],
                        model_path='must-not-load.onnx'))
                    self.assertFalse(config['enabled'])
                    self.assertFalse(config['output_enabled'])
                    self.assertFalse(config['model_path'])
                    vanilla = dict(action='begin', person_id='generated-fixture', preset_id=preset_id, automatic=True)
                    request('/api/recording', dict(vanilla, confirmed=False), 400)
                    request('/api/recording', dict(vanilla, confirmed=True, step_index=1), 400)
                    request('/api/recording', dict(vanilla, confirmed=True, preset_id='continuation'), 400)
                    request('/api/start', dict(mode='live'), 400)
                    self.assertEqual(camera.commands, [b'CONTEXT\n'])  # Discovery checks identity, not the image stream.
                    self.assertFalse((root/'recordings').exists())

                    request('/api/recording', dict(vanilla, confirmed=True))  # No role/evaluation_only in the web request.
                    wait_for(lambda: request('/api/status')['capture_control']['state'] == 'recording')
                    status = request('/api/status')['recording']
                    archive = Path(status['path'])
                    self.assertEqual(archive.parent, root/'recordings')
                    manifest = json.loads((archive/'manifest.json').read_text(encoding='utf-8'))
                    self.assertEqual(manifest['observability_role'], 'C')
                    self.assertTrue(manifest['evaluation_only'])
                    self.assertFalse(manifest['consent']['local_training'])
                    self.assertEqual(manifest['capture_plan'], plan)
                    self.assertEqual(list((archive/'images').iterdir()), [])
                    self.assertEqual((archive/'images.jsonl').stat().st_size, 0)
                    request('/api/recording', dict(action='select_step', step_index=2), 400)
                    release_frames.set()
                    wait_for(lambda: request('/api/status')['recording'].get('stocks', 0) == 3)
                    wait_for(lambda: request('/api/status')['recording'].get('images', 0) >= 5)
                    active = request('/api/status')
                    self.assertTrue(active['capture_only'])
                    self.assertEqual(active['metrics']['inferred'], 0)
                    self.assertEqual(active['prediction']['valid_mask'], 0)
                    camera.cut_next_frame.set()
                    wait_for(lambda: request('/api/status')['capture_control']['state'] == 'error')

                    status = request('/api/status')
                    self.assertFalse(status['running'])
                    self.assertIn('47/', status['capture_control']['error'])
                    outcome = json.loads((archive/'capture-outcome.json').read_text(encoding='utf-8'))
                    self.assertEqual(outcome['error'], status['capture_control']['error'])
                    self.assertFalse(outcome['complete'])
                    self.assertEqual(outcome['archive_state'], 'stopped')
                    self.assertEqual(outcome['session_id'], archive.name)
                    manifest, rows = load_dataset(archive)
                    self.assertTrue(manifest['evaluation_only'])
                    images = [json.loads(line) for line in (archive/'images.jsonl').read_text(encoding='utf-8').splitlines()]
                    self.assertGreaterEqual(len(rows), 5)
                    self.assertEqual(len(images), len(rows))
                    self.assertTrue(all(row['observability_role'] == 'C' and row['evaluation_only']
                                        for row in rows + images))
                    self.assertTrue(all(row['targets'] == [None]*50 for row in rows))
                    self.assertEqual(next(row for row in rows if row['source_seq'] == 2)['pairing_status'], 'paired')
                    for row in rows:
                        with Image.open(archive/row['image']) as frame:
                            frame.load()
                        self.assertEqual((archive/row['image']).read_bytes(), camera.payload)
                    self.assertNotIn('continuation', request('/api/presets'))
                    request('/api/recording', dict(vanilla, confirmed=True), 400)
                    request('/api/recording/review?all=true', code=403)
                    request('/api/recording/image?session='+archive.name+'&sample='+rows[0]['sample_id'], code=403)
                    request('/api/training', dict(action='begin', operation='split'), 403)
                    self.assertEqual(camera.commands.count(b'JPEG\n'), 1)
                    self.assertEqual(camera.commands.count(b'LABELS\n'), 1)
                    with self.assertRaises(socket.timeout):
                        output_sink.recv(4096)
                    self.assertEqual(len(list((root/'recordings').iterdir())), 1)
                    split = root/'must-not-exist.json'
                    with self.assertRaisesRegex(SystemExit, 'evaluation_only/C'):
                        train(['split', '--dataset', str(archive), '--split', str(split)])
                    self.assertFalse(split.exists())
                    output = root/'must-not-exist.pt'
                    with self.assertRaisesRegex(SystemExit, 'evaluation_only/C'):
                        train(['student-baseline', '--dataset', str(archive), '--split', str(split), '--output', str(output)])
                    self.assertFalse(output.exists())
                finally:
                    release_frames.set()
                    server.shutdown()
                    worker.join(10)
                    self.assertFalse(worker.is_alive())

    def test_normal_entrypoint_can_continue_from_last_finished_step_without_touching_original(self):
        camera = LoopbackCamera()
        self.addCleanup(camera.close)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = WholeMouthRecording(root/'recordings')
            original.begin('fixture', confirmed=True, preset_id='lips', automatic=True)
            original.advance('skip')
            original.advance('ready')  # Interrupted before finishing the second step.
            original.stop()
            before = {p.name: p.read_bytes() for p in original.path.iterdir() if p.is_file()}
            servers, ready = [], threading.Event()

            def create(runtime, **options):
                server = make_server(runtime, discover=camera.discover, **options)
                servers.append(server)
                ready.set()
                return server

            with patch('backend.make_server', side_effect=create), \
                    patch('capture_control.discover_sources', return_value=camera.discover()):
                worker = threading.Thread(target=main, args=(['--port', '0', '--config', str(root/'config.json'),
                    '--continue-recording', original.session_id],))
                worker.start()
                self.assertTrue(ready.wait(5))
                server = servers[0]
                base = f'http://127.0.0.1:{server.server_port}'

                def request(path, body=None):
                    req = Request(base+path, json.dumps(body).encode(), {'Content-Type': 'application/json'}) if body else base+path
                    with urlopen(req, timeout=5) as response:
                        return json.load(response)

                try:
                    catalog = request('/api/presets')
                    tail = catalog['presets'][0]
                    self.assertEqual(catalog['default'], 'continuation')
                    self.assertEqual(tail['steps'], original.manifest['capture_plan']['steps'][1:])
                    self.assertNotIn(b'JPEG\n', camera.commands)
                    options = dict(action='begin', person_id='fixture', confirmed=True, guide=True, automatic=True,
                                   preset_id='continuation', stage='raw_only', receive_results=False)
                    with self.assertRaises(HTTPError):
                        request('/api/recording', dict(options, confirmed=False))
                    self.assertNotIn(b'JPEG\n', camera.commands)
                    request('/api/recording', options)
                    wait_for(lambda: request('/api/status')['recording'].get('images', 0) >= 2)
                    status = request('/api/status')['recording']
                    self.assertEqual(status['guide']['step']['id'], tail['steps'][0]['id'])
                    request('/api/recording', dict(action='stop'))
                    wait_for(lambda: request('/api/status')['capture_control']['state'] == 'completed')
                    manifest, samples = load_dataset(Path(status['path']))
                    self.assertNotEqual(manifest['session_id'], original.session_id)
                    self.assertEqual(manifest['continuation_of'], original.session_id)
                    for key in ('fit_id', 'source_group_id'):
                        self.assertEqual(manifest[key], original.manifest[key])
                        self.assertTrue(all(row[key] == original.manifest[key] for row in samples))
                    with self.assertRaisesRegex(ValueError, 'crosses splits'):
                        assert_no_split_leakage({'train': [original.manifest], 'test': samples})
                    self.assertEqual({p.name: p.read_bytes() for p in original.path.iterdir() if p.is_file()}, before)
                finally:
                    server.shutdown()
                    worker.join(10)
                    self.assertFalse(worker.is_alive())

    def test_continuation_refuses_evaluation_only_or_unfinished_archives(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = WholeMouthRecording(root)
            original.begin('fixture', confirmed=True, automatic=True, role='C')
            with self.assertRaisesRegex(ValueError, 'stopped guided development'):
                capture_continuation(root, original.session_id)
            original.stop()
            with self.assertRaisesRegex(ValueError, 'stopped guided development'):
                capture_continuation(root, original.session_id)

    def test_stop_and_resume_a_manual_plan_preserve_out_of_order_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = WholeMouthRecording(root/'recordings')
            original.begin('fixture', confirmed=True, preset_id='basics', step_index=2)
            steps = original.manifest['capture_plan']['steps']
            first_token = original.guide.frame
            original.advance('skip')
            original.advance('select_step', step_index=1)
            with self.assertRaisesRegex(ValueError, 'step has changed'):
                original.advance('skip', guide_frame=first_token)
            original.advance('ready')
            original.stop()  # Interrupted during the second action.
            self.assertEqual(original.guide.status()['skipped_indices'], [2])
            mock = Mock(config_path=root/'config.json')
            presets = CaptureControl(mock, Mock()).presets()
            self.assertEqual(presets['default'], 'complete')
            self.assertEqual(presets['continuation']['session_id'], original.session_id)
            self.assertEqual(presets['presets'][0]['steps'], steps[:2]+steps[3:])
            # The first and last neutral poses are distinct positions.
            self.assertEqual([i for i, step in enumerate(steps) if step['id'] == 'neutral'], [0, len(steps)-1])

    def record_from_normal_entry(self, *, receive_results):
        camera = LoopbackCamera(send_stock=receive_results)
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
                    self.assertNotIn(b'LABELS\n', camera.commands)
                    request('/api/recording', dict(action='begin', person_id='generated', confirmed=True,
                                                   guide=False, stage='raw_only', receive_results=receive_results))
                    wait_for(lambda: runtime.recorder and runtime.recorder.status()['images'] >= 4 and
                             (not receive_results or runtime.recorder.status()['stocks'] == 3), timeout=5)
                    run = runtime.run_dir
                    request('/api/recording', dict(action='stop'))
                    wait_for(lambda: request('/api/status')['capture_control']['state'] == 'completed')
                    wait_for(lambda: camera.active_images == 0)
                    self.assertEqual(camera.commands.count(b'JPEG\n'), 1)
                    self.assertEqual(camera.commands.count(b'LABELS\n'), int(receive_results))
                    self.assertEqual(runtime.run_dir, run)
                    self.assertFalse(runtime.running)
                    self.assertEqual(runtime.counts['inferred'], 0)
                    review = request('/api/recording/review?all=true')
                    self.assertGreaterEqual(review['total'], 3)
                    sample = review['samples'][0]
                    with urlopen(base+'/api/recording/image?session='+review['session_id']+'&sample='+sample['sample_id']) as image:
                        self.assertEqual(image.read(), camera.payload)
                    manifest, rows = load_dataset(runtime.recorder.path)
                    self.assertTrue(manifest['samples_complete'])
                    self.assertEqual(manifest['trainable'], 0)
                    self.assertTrue(all(row['targets'] == [None]*50 for row in rows))
                    self.assertFalse(runtime.status()['stock']['active'])
                    if receive_results:
                        stocks = [json.loads(line) for line in
                                  (runtime.recorder.path/'stock.jsonl').read_text(encoding='utf-8').splitlines()]
                        ledger = {row['stock_id']: row for row in stocks}
                        samples = {row['source_seq']: row for row in rows}
                        outcomes = {row['source_seq']: row['pairing_status'] for row in review['samples']}
                        self.assertEqual(len(ledger), 3)
                        self.assertEqual(pair_rejections(samples[2], manifest, ledger), [])
                        self.assertEqual(outcomes[2], 'paired')
                        self.assertEqual(outcomes[3], 'unproven_identity')
                        self.assertIsNone(samples[3]['stock_id'])
                        self.assertEqual(pair_rejections(samples[4], manifest, ledger), ['quality0_not_above_0.1'])
                        self.assertEqual(outcomes[5], 'missing_stock')
                        self.assertEqual([r['source_seq'] for r in rows if not pair_rejections(r, manifest, ledger)], [2])
                        derived = derive_row(samples[2], choose_mapping([samples[2]]))
                        self.assertEqual(derived['targets'][INDEX['JawOpen']], .25)
                        self.assertEqual(derived['target_weights'][INDEX['JawOpen']], .5)
                        self.assertEqual(derived['targets'][33:], [None]*17)
                        self.assertEqual(samples[2]['targets'], [None]*50)
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
