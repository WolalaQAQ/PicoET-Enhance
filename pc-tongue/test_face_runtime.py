"""Whole-mouth lifecycle, recording and CLI wiring tests; controlled input only."""
from dataclasses import replace
import io
import json
from pathlib import Path
import socket
import socketserver
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from backend import make_server
from face_schema import CHANNELS, INDEX, SCHEMA
from runtime import ROOT, Runtime
from stock import CaptureContext, StockRecord
from test_runtime import header, jpeg, wait_for
from wire import FACE_RESULT


class FaceStub:
    trained = ['JawOpen', 'TongueOut', 'TongueLeft', 'LipFunnelLower']

    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.block = False

    def describe(self):
        return dict(engine='test-only', device='cpu', schema=SCHEMA, trained_channels=self.trained,
                    validated_channels=self.trained,
                    valid_mask=sum(1 << INDEX[name] for name in self.trained))

    def predict(self, image, config):
        if image[0, 0]:
            self.entered.set()
            if self.block:
                self.release.wait(2)
        values = [.9]*50  # Untrained random heads must never become supported.
        for name, value in zip(self.trained, [.3, .1, .2, .7]):
            values[INDEX[name]] = value
        return dict(values=values, valid_mask=(1 << 50)-1)


class FaceManualRuntime(Runtime):
    def _receive(self):
        with self.lock:
            self.clock = dict(offset_ns=0., uncertainty_ms=0., calibrated_pc_ns=time.perf_counter_ns())
            self.connected = True
        self.stop_event.wait()

    def _clock_refresh(self):
        self.stop_event.wait()

    def _stock_receive(self):
        self.stock_stop.wait()


class FaceRuntimeTests(unittest.TestCase):
    def setUp(self):
        work = ROOT/'tests-work'; work.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=work); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.model = FaceStub()
        self.runtime = FaceManualRuntime(self.root/'config.json', lambda c: self.model)
        self.runtime.configure(dict(headset_host='test.example', model_path='fixture.onnx', stale_ms=200))
        self.addCleanup(self.close)

    def close(self):
        self.model.release.set(); self.runtime.stop(); self.runtime.udp.close()

    def submit(self, seq=10, session=1):
        payload = jpeg(); h = replace(header(seq, session), payload_bytes=len(payload))
        self.runtime._publish(h, payload, time.perf_counter_ns()); return h

    def start(self):
        self.runtime.start(); wait_for(lambda: self.runtime.connected)

    def test_default_has_no_weights_and_old_config_is_preserved_without_mask_autostart(self):
        path = self.root/'old.json'
        original = dict(model_kind='segmentation', model_path='old-user-wip.pt', enabled=True, face_source='cymple')
        path.write_text(json.dumps(original))
        r = Runtime(path)
        try:
            self.assertEqual(r.config['model_kind'], 'face50'); self.assertEqual(r.config['model_path'], '')
            self.assertFalse(r.config['enabled']); self.assertNotIn('face_source', r.config)
            with self.assertRaisesRegex(ValueError, 'Configure a model'):
                r.prepare()
            self.assertEqual(json.loads(path.read_text()), original)
            r.configure({'device': 'cpu'})
            self.assertEqual(json.loads(path.read_text())['legacy_configuration'], original)
        finally:
            r.stop(); r.udp.close()

    def test_image_only_recording_does_not_start_stock_reader(self):
        self.start()
        with self.assertRaisesRegex(ValueError, 'Instant labels'):
            self.runtime.recording('begin', person_id='fixture', confirmed=True,
                                   stage='instant', receive_results=False)
        self.runtime.recording('begin', person_id='fixture', confirmed=True, guide=False,
                               receive_results=False)
        self.assertIsNone(self.runtime.stock_thread)
        self.submit()
        wait_for(lambda: self.runtime.recorder.status()['images'] == 1)
        self.runtime.recording('stop')
        self.assertEqual(self.runtime.recorder.status()['stocks'], 0)

    def test_named_values_are_direct_and_manifest_mask_wins_over_random_heads(self):
        with patch('runtime.apply_calibration', side_effect=AssertionError('mask geometry used')):
            self.start(); self.submit(); wait_for(lambda: self.runtime.counts['inferred'] == 1)
        p = self.runtime.prediction
        self.assertEqual(p['channels'], dict(zip(self.model.trained, [.3, .1, .2, .7])))
        self.assertEqual(p['valid_mask'].bit_count(), 4)
        self.assertEqual(p['values'][INDEX['TongueUp']], 0.)
        self.assertTrue(p['fresh'])

    def test_runtime_does_not_reconstruct_output_permission_from_training(self):
        self.runtime.description = self.model.describe()
        raw = self.model.predict(__import__('numpy').zeros((400, 400), dtype='uint8'), {})
        self.runtime.description['valid_mask'] = 0
        self.assertEqual(self.runtime._face_prediction(raw)['valid_mask'], 0)
        self.runtime.description.pop('validated_channels')
        self.runtime.description['valid_mask'] = (1 << 50)-1
        self.assertEqual(self.runtime._face_prediction(raw)['valid_mask'], 0)
        self.runtime.description['validated_channels'] = ['TongueOut']
        self.assertEqual(self.runtime._face_prediction(raw)['valid_mask'], 1 << INDEX['TongueOut'])

    def test_mfr1_all_fields_stop_expiry_and_session_release(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sink:
            sink.bind(('127.0.0.1', 0)); sink.settimeout(2)
            self.runtime.configure(dict(output_enabled=True, output_port=sink.getsockname()[1]))
            self.start(); self.submit()
            fields = FACE_RESULT.unpack(sink.recv(1024))
            self.assertEqual(fields[:3], (b'MFR1', 1, 256)); self.assertEqual(fields[9], 0)
            self.assertAlmostEqual(fields[10+INDEX['TongueLeft']], .2)
            self.assertEqual(fields[7].bit_count(), 4)
            self.model.block = True; self.submit(1, 2)
            released = FACE_RESULT.unpack(sink.recv(1024)); self.assertEqual(released[7], 0); self.assertEqual(released[9], 1)
            self.model.release.set(); wait_for(lambda: self.runtime.counts['inferred'] == 2)
            sink.recv(1024)
            wait_for(lambda: self.runtime.prediction['state'] == 'stale')
            self.assertEqual(FACE_RESULT.unpack(sink.recv(1024))[7], 0)
            self.runtime.stop(); self.assertEqual(FACE_RESULT.unpack(sink.recv(1024))[9], 1)
            self.assertIsNone(self.runtime.preview)

    def test_no_model_capture_decodes_preview_without_inference_or_valid_output(self):
        self.runtime.configure({'model_path': ''})
        self.runtime.model_factory = lambda _: self.fail('capture must not load a model')
        self.runtime.start('capture'); wait_for(lambda: self.runtime.connected)
        self.submit(); wait_for(lambda: self.runtime.preview is not None)
        self.assertEqual(self.runtime.counts['inferred'], 0); self.assertEqual(self.runtime.counts['decoded'], 1)
        self.assertFalse(self.runtime.prediction['fresh']); self.assertEqual(self.runtime.prediction['valid_mask'], 0)
        self.assertTrue(self.runtime.status()['metrics']['video_fresh'])
        self.runtime.stop()
        with self.assertRaisesRegex(ValueError, 'Configure a model'):
            self.runtime.start()

    def test_recording_consent_precedes_input_and_real_data_api_writes_original_jpeg(self):
        self.runtime.configure({'model_path': ''})
        with self.assertRaisesRegex(ValueError, 'Confirm local'):
            self.runtime.recording('begin', person_id='local', confirmed=False)
        self.assertFalse(self.runtime.running); self.assertFalse((self.root/'recordings').exists())
        with self.assertRaisesRegex(ValueError, 'Start capture-only'):
            self.runtime.recording('begin', person_id='local', confirmed=True)
        self.assertFalse(self.runtime.running)
        self.runtime.start('capture'); wait_for(lambda: self.runtime.connected)
        status = self.runtime.recording('begin', person_id='local', confirmed=True)
        wait_for(lambda: self.runtime.connected); self.submit()
        wait_for(lambda: len(self.runtime.recorder.images) == 1)
        self.runtime.recording('ready'); self.runtime.recording('skip')
        status = self.runtime.recording('cancel')
        self.assertEqual(status['state'], 'cancelled'); self.assertFalse(self.runtime.running)
        path = Path(status['path']); self.assertEqual(next((path/'images').glob('*.jpg')).read_bytes(), jpeg())
        self.assertEqual(json.loads((path/'manifest.json').read_text())['consent']['upload'], False)
        self.assertEqual(self.runtime.prediction['valid_mask'], 0)

    def test_new_recording_reaps_automatically_finished_writer_without_reopening_input(self):
        self.runtime.start('capture')
        wait_for(lambda: self.runtime.connected)
        first = self.runtime.recording('begin', person_id='fixture', confirmed=True, automatic=True)
        guide = self.runtime.recorder.guide
        for _ in range(guide.status()['total']):
            self.runtime.recording('skip')
        self.submit()
        self.runtime.record_thread.join(3)
        self.assertFalse(self.runtime.record_thread.is_alive())
        self.assertTrue(self.runtime.running)
        second = self.runtime.recording('begin', person_id='fixture', confirmed=True, automatic=True)
        self.assertNotEqual(first['session_id'], second['session_id'])
        self.assertTrue(json.loads((Path(first['path'])/'manifest.json').read_text())['samples_complete'])

    def test_training_form_uses_delivered_parser_and_never_launches_for_invalid_args(self):
        operations = self.runtime.training_operations()
        self.assertIn('local-adapt', operations); self.assertIn('teacher-train', operations)
        self.assertIn('teacher_approval', [f['name'] for f in operations['local-adapt']])
        with patch('runtime.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(ValueError, 'Required local'):
                self.runtime.train('begin', 'local-adapt', {})
            spawn.assert_not_called()

    def test_every_training_form_label_has_bilingual_text(self):
        import re
        source = (Path(__file__).parent/'web'/'i18n.mjs').read_text(encoding='utf-8')
        english, chinese = source.split('"zh-CN": {')
        alias = dict(device='trainDevice', split='splitPath')
        keys = set()
        for operation, fields in self.runtime.training_operations().items():
            keys.add(operation)
            for field in fields:
                keys.add(alias.get(field['name'], field['name']))
                keys.update('choice-'+choice for choice in field['choices'] or [])
        for key in keys:
            pattern = r'(?:^|[\s,{])"?'+re.escape(key)+r'"?\s*:'
            for language, text in (('en', english), ('zh-CN', chinese)):
                self.assertRegex(text, re.compile(pattern, re.M), (language, key))

    def test_C_recording_role_reaches_first_manifest_before_queued_frame(self):
        self.runtime.start('capture'); wait_for(lambda: self.runtime.connected)
        status = self.runtime.recording('begin', person_id='fixture', confirmed=True,
                                        guide=False, role='C', evaluation_only=True)
        manifest = json.loads((Path(status['path'])/'manifest.json').read_text())
        self.assertTrue(manifest['evaluation_only'])
        self.assertEqual(manifest['observability_role'], 'C')
        self.assertFalse(self.runtime.recorder.images)
        self.submit(); wait_for(lambda: len(self.runtime.recorder.images) == 1)
        self.runtime.recording('stop')
        self.assertTrue(self.runtime.recorder.samples()[0]['evaluation_only'])

    def test_runtime_unknown_role_does_not_create_recording_or_workers(self):
        self.runtime.start('capture'); wait_for(lambda: self.runtime.connected)
        with self.assertRaises(ValueError):
            self.runtime.recording('begin', person_id='fixture', confirmed=True, role='unknown')
        self.assertIsNone(self.runtime.record_thread)
        self.assertFalse((self.root/'recordings').exists())

    def test_http_recording_forwards_explicit_role_and_restriction(self):
        server = make_server(self.runtime, port=0)
        worker = threading.Thread(target=server.serve_forever); worker.start()
        try:
            body = dict(action='begin', person_id='fixture', confirmed=True, guide=False,
                        stage='raw_only', role='C', evaluation_only=True, receive_results=False)
            request = Request(f'http://127.0.0.1:{server.server_port}/api/recording',
                              json.dumps(body).encode(), {'Content-Type': 'application/json'})
            with patch.object(self.runtime, 'recording', return_value={}) as recording:
                with urlopen(request) as response:
                    self.assertEqual(response.status, 200)
                recording.assert_called_once_with('begin', person_id='fixture', confirmed=True,
                    guide=False, stage='raw_only', role='C', evaluation_only=True, preset_id='complete',
                      automatic=False, receive_results=False, step_index=None, guide_frame=None, prepare_seconds=3.)
        finally:
            server.shutdown(); worker.join(); server.server_close()

    def test_training_subprocess_command_uses_local_cli_and_owned_cancel_releases_output(self):
        class Process:
            stdout = io.StringIO('{"event":"progress","epoch":1}\n{"event":"result","result":{"output":"candidate.pt"}}\n')
            def wait(self): return 0
            def poll(self): return 0
        with patch('runtime.subprocess.Popen', return_value=Process()) as spawn:
            self.runtime.train('begin', 'export', {'checkpoint': 'student.pt', 'output': 'candidate.onnx'})
            self.runtime.training_thread.join(2)
        command = spawn.call_args.args[0]
        self.assertIn(str(ROOT/'face_train.py'), command); self.assertNotIn('shell', spawn.call_args.kwargs)
        self.assertEqual(self.runtime.training['state'], 'completed')
        self.assertEqual(self.runtime.training['result']['output'], 'candidate.pt')
        self.runtime.train('cancel'); self.assertEqual(self.runtime.prediction['valid_mask'], 0)

    def test_stock_record_never_invents_image_generation_or_training_labels(self):
        self.runtime.start('capture'); wait_for(lambda: self.runtime.connected)
        self.runtime.recording('begin', person_id='local', confirmed=True, guide=False, stage='instant')
        record = StockRecord(1, 99, 10, 10*11_111_111, [.2]*33, [.3]*33, [.4]*33, [1., 1.],
                             True, association_valid=True, raw_identity=10, warmup=False)
        self.runtime._record_enqueue('stock', record)
        self.submit(); wait_for(lambda: len(self.runtime.recorder.images) == 1)
        self.runtime.recording('stop')
        self.assertEqual(self.runtime.recorder.bindings, {})
        row = self.runtime.recorder.samples()[0]
        self.assertEqual(row['pairing_status'], 'unproven_generation')
        self.assertTrue(all(value is None for value in row['targets']))

    def test_stock_reader_counts_actual_source_associations_without_claiming_training_labels(self):
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def sendall(self, data): self.command = data
        connection = Connection()
        self.runtime.stock_stop.clear()
        record = StockRecord(1, 99, 0, 0, [.2]*33, [.3]*33, [.4]*33, [1., 1.], True)
        exact = replace(record, source_seq=10, mouth_sensor_ns=10*11_111_111,
                        association_valid=True, raw_identity=10)
        with patch('runtime.socket.create_connection', return_value=connection), \
             patch('stock.read_stock', side_effect=[record, exact, EOFError('finished')]):
            Runtime._stock_receive(self.runtime)
        self.assertEqual(connection.command, b'LABELS\n')
        status = self.runtime.status()['stock']
        self.assertEqual((status['records'], status['associated'], status['unassociated']), (2, 1, 1))
        self.assertFalse(status['active'])
        self.assertEqual(self.runtime.transport_bindings, {})

    def test_trusted_preframe_capture_context_can_bind_without_stock_inference(self):
        self.runtime.start('capture'); wait_for(lambda: self.runtime.connected)
        context = CaptureContext(1, 99)
        self.runtime.bind_capture(1, 99, evidence=context.evidence)
        self.runtime.recording('begin', person_id='local', confirmed=True, guide=False, stage='instant')
        self.submit()
        unassociated = StockRecord(1, 99, 0, 0, [.2]*33, [.3]*33, [.4]*33, [1., 1.], True, warmup=False)
        self.runtime._record_enqueue('stock', unassociated)
        wait_for(lambda: len(self.runtime.recorder.stocks) == 1)
        self.runtime.recording('stop')
        row = self.runtime.recorder.samples()[0]
        self.assertEqual(row['service_generation'], 99)
        self.assertFalse(row['trainable']); self.assertEqual(row['pairing_status'], 'missing_stock')

    def test_record_disk_io_does_not_block_video_inference(self):
        self.start(); self.runtime.recording('begin', person_id='local', confirmed=True, guide=False)
        entered, release = threading.Event(), threading.Event()
        original = self.runtime.recorder.on_frame
        def slow_frame(*args):
            entered.set(); release.wait(2); return original(*args)
        self.runtime.recorder.on_frame = slow_frame
        try:
            self.submit(); self.assertTrue(entered.wait(1)); wait_for(lambda: self.runtime.counts['inferred'] == 1)
            self.submit(11); wait_for(lambda: self.runtime.counts['inferred'] == 2)
            self.assertTrue(self.runtime.prediction['fresh'])
        finally:
            release.set(); self.runtime.recording('stop')

    def test_http_review_batch_and_images_are_restricted_to_current_recording(self):
        self.runtime.start('capture'); wait_for(lambda: self.runtime.connected)
        self.runtime.recording('begin', person_id='local', confirmed=True, guide=False)
        self.submit(); self.submit(11); wait_for(lambda: len(self.runtime.recorder.images) == 2)
        self.runtime.recording('stop')
        server = make_server(self.runtime, port=0); worker = threading.Thread(target=server.serve_forever); worker.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            with urlopen(base+'/api/recording/review?all=true') as response:
                page = json.load(response)
            self.assertEqual(page['total'], 2)
            session = page['session_id']; sample = page['samples'][0]['sample_id']
            with urlopen(base+f'/api/recording/image?session={session}&sample={sample}') as response:
                self.assertEqual(response.headers.get_content_type(), 'image/jpeg'); self.assertEqual(response.read(), jpeg())
            for query in (f'session=old&sample={sample}', f'session={session}&sample=../config.json'):
                with self.assertRaises(HTTPError) as error:
                    urlopen(base+'/api/recording/image?'+query)
                self.assertEqual(error.exception.code, 400); error.exception.close()
            body = dict(session_id=session, sample_ids=[row['sample_id'] for row in page['samples']],
                        targets={'TongueOut': .5}, confirmed=True, reviewer='local-user', observation='visible')
            request = Request(base+'/api/recording/review', json.dumps(body).encode(), {'Content-Type': 'application/json'})
            with urlopen(request) as response:
                self.assertEqual(json.load(response)['samples'], 2)
            samples = self.runtime.recorder.samples()
            self.assertTrue(all(row['targets'][INDEX['TongueOut']] == .5 for row in samples))
            self.assertTrue(all(row['targets'][INDEX['TongueLeft']] is None for row in samples))
            self.assertTrue(json.loads((self.runtime.recorder.path/'manifest.json').read_text())['samples_complete'])
        finally:
            server.shutdown(); worker.join(); server.server_close()

    def test_real_ptc1_handshake_precedes_images_and_labels_only_connect_after_consent(self):
        commands = []; payload = jpeg(); context = CaptureContext(1, 99)
        labels_sent = threading.Event()
        class Source(socketserver.BaseRequestHandler):
            def handle(inner):
                command = bytearray()
                while not command.endswith(b'\n'):
                    part = inner.request.recv(1)
                    if not part: return
                    command.extend(part)
                commands.append(bytes(command))
                if command == b'PING\n':
                    now = time.perf_counter_ns(); inner.request.sendall(struct.pack('<QQ', now, now))
                elif command == b'CONTEXT\n':
                    for part in (context.pack()[:7], context.pack()[7:]): inner.request.sendall(part)
                elif command == b'JPEG\n':
                    for seq in range(10, 100):
                        try:
                            h = replace(header(seq), payload_bytes=len(payload))
                            inner.request.sendall(h.pack()+payload)
                            if seq == 10:
                                labels_sent.wait(2)
                            time.sleep(.01)
                        except OSError:
                            break
                elif command == b'LABELS\n':
                    record = StockRecord(1, 99, 0, 0, [.2]*33, [.3]*33, [.4]*33, [1., 1.], True, warmup=False)
                    inner.request.sendall(record.pack())
                    h = header(11)
                    exact = replace(record, source_seq=h.source_seq, mouth_sensor_ns=h.mouth_sensor_ns,
                                    stock_input_ns=h.mouth_sensor_ns, vendor_ns=h.vendor_ns,
                                    raw_identity=h.raw_identity, association_valid=True, label_seq=1)
                    inner.request.sendall(exact.pack())
                    labels_sent.set()
                    inner.request.recv(1)
        server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Source); server.daemon_threads = True
        serving = threading.Thread(target=server.serve_forever); serving.start()
        runtime = Runtime(self.root/'network.json')
        try:
            runtime.configure(dict(headset_host='127.0.0.1', headset_port=server.server_address[1]))
            runtime.start('capture'); wait_for(lambda: runtime.counts.get('decoded', 0) >= 1)
            self.assertLess(commands.index(b'CONTEXT\n'), commands.index(b'JPEG\n'))
            self.assertNotIn(b'LABELS\n', commands)
            self.assertFalse((self.root/'recordings').exists())
            runtime.recording('begin', person_id='local', confirmed=True, guide=False, stage='instant')
            wait_for(lambda: runtime.recorder.status()['stocks'] == 2 and runtime.recorder.status()['images'] >= 2)
            runtime.recording('stop')
            self.assertEqual(runtime.capture_context, context.evidence)
            rows = runtime.recorder.samples()
            self.assertTrue(all(row['service_generation'] == 99 for row in rows))
            paired = [row for row in rows if row['source_seq'] == 11]
            self.assertEqual(len(paired), 1)
            self.assertEqual(paired[0]['pairing_status'], 'paired')
            self.assertAlmostEqual(paired[0]['targets'][INDEX['JawOpen']], .3)
            self.assertTrue(all(row['targets'][INDEX['TongueRoll']] is None for row in rows))
            self.assertFalse(any(row['trainable'] for row in rows if row['source_seq'] != 11))
            self.assertEqual((runtime.stock_status['associated'], runtime.stock_status['unassociated']), (1, 1))
            self.assertFalse(runtime.stock_status['active'])
            self.assertEqual(runtime.counts['inferred'], 0)
        finally:
            runtime.stop(); runtime.udp.close(); server.shutdown(); serving.join(); server.server_close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
