"""Protocol, lifecycle and HTTP tests; never contacts the headset."""
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
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import numpy as np
from PIL import Image

from backend import make_server
from runtime import DEFAULT_CONFIG, ROOT, Runtime
from state import TongueState
from wire import END, JPEG, HEADER, RAW, RESULT, FrameHeader, SourceOrder, read_frame, result_packet


def header(seq=10, session=1, age_ms=0):
    now = time.perf_counter_ns()-int(age_ms*1e6)
    return FrameHeader(1, JPEG, session, seq, seq*11_111_111, seq*11_111_111+4,
                       now, now, now, now, seq*11_111_111-2, seq*11_111_111-1,
                       seq*11_111_111, seq, raw_crc32=42)


def jpeg():
    output = io.BytesIO()
    Image.fromarray(np.full((400, 400), 100, dtype=np.uint8)).save(output, format='JPEG', quality=95)
    return output.getvalue()


def wait_for(predicate, timeout=2):
    deadline = time.monotonic()+timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Timed out waiting for test event')
        time.sleep(.005)


class FakeModel:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False

    def describe(self):
        return {'engine': 'test-only', 'device': 'CPU'}

    def predict(self, image, config):
        if image[0, 0] != 0:
            self.entered.set()
            if self.block:
                self.release.wait(2)
        return {'present': True, 'left': .8, 'right': 0., 'out': 1.,
                'mask': np.ones((56, 56), dtype=np.uint8)}


class ManualRuntime(Runtime):
    def _receive(self):
        with self.lock:
            self.clock = {'offset_ns': 0., 'uncertainty_ms': 0., 'calibrated_pc_ns': time.perf_counter_ns()}
            self.connected = True
        self.stop_event.wait()

    def _clock_refresh(self):
        self.stop_event.wait()


class WireTests(unittest.TestCase):
    def test_roundtrip_and_identity(self):
        h = header()
        self.assertEqual(len(h.pack()), 128)
        self.assertEqual(FrameHeader.unpack(h.pack()), h)
        self.assertEqual(h.identity(), replace(h, flags=RAW, payload_bytes=160000).identity())
        self.assertEqual(len(result_packet(h, 1, .8, 0, 1)), 64)
        self.assertEqual(RESULT.unpack(result_packet(h, 2))[0], b'TJR1')

    def test_fragmentation_and_truncation(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        h = replace(header(), payload_bytes=17)
        data = h.pack()+b'x'*17
        def send():
            for i in range(0, len(data), 3):
                left.sendall(data[i:i+3])
            left.shutdown(socket.SHUT_WR)
        worker = threading.Thread(target=send)
        worker.start()
        self.assertEqual(read_frame(right), (h, b'x'*17))
        worker.join()
        with self.assertRaises(EOFError):
            read_frame(right)

    def test_reject_unsupported_and_oversize(self):
        h = header()
        for data in [b'BAD!'+h.pack()[4:], replace(h, width=200).pack(),
                     replace(h, payload_bytes=3_000_000).pack(),
                     replace(h, flags=END, payload_bytes=3_000_000).pack(),
                     replace(h, flags=0).pack()]:
            with self.subTest(data=data[:16]), self.assertRaises(ValueError):
                FrameHeader.unpack(data)

    def test_order_and_retired_session(self):
        order = SourceOrder()
        self.assertTrue(order.accept(header(10))[0])
        self.assertFalse(order.accept(header(10))[0])
        self.assertFalse(order.accept(header(9))[0])
        self.assertEqual(order.accept(header(13))[2], 2)
        self.assertFalse(order.accept(replace(header(14), mouth_sensor_ns=1))[0])
        self.assertTrue(order.accept(header(1, session=2))[0])
        self.assertFalse(order.accept(header(20, session=1))[0])


class StateTests(unittest.TestCase):
    def test_absent_stale_and_source_time_hold(self):
        state = TongueState()
        config = dict(DEFAULT_CONFIG, smoothing_ms=0, uncertain_hold_ms=100)
        present = {'present': True, 'left': .8, 'right': 0., 'out': 1.}
        self.assertTrue(state.update(present, header(10), config)['override'])
        uncertain = dict(present, state='uncertain')
        self.assertTrue(state.update(uncertain, header(11), config)['held'])
        self.assertFalse(state.update(uncertain, header(30), config)['override'])
        state.update(present, header(31), config)
        self.assertEqual(state.update(dict(present, present=False), header(32), config)['out'], 0.)
        self.assertFalse(state.update(dict(present, state='invalid'), header(33), config)['override'])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'tests-work').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT/'tests-work')
        self.addCleanup(self.tmp.cleanup)
        self.model = FakeModel()
        self.runtime = ManualRuntime(Path(self.tmp.name)/'config.json',
                                     lambda config: self.model, Path(self.tmp.name)/'runs')
        self.addCleanup(self.close_runtime)
        self.runtime.configure({'headset_host': '127.0.0.1', 'model_path': 'test-only', 'stale_ms': 100.})

    def close_runtime(self):
        self.model.release.set()
        self.runtime.stop()
        self.runtime.udp.close()

    def start(self):
        self.runtime.start()
        wait_for(lambda: self.runtime.connected)

    def submit(self, h):
        payload = jpeg()
        return self.runtime._publish(replace(h, payload_bytes=len(payload)), payload, time.perf_counter_ns())

    def test_no_autostart_or_pc_source_selection(self):
        self.assertFalse(self.runtime.running)
        self.assertNotIn('face_source', self.runtime.config)
        with self.assertRaisesRegex(ValueError, 'Unknown configuration fields: face_source'):
            self.runtime.configure({'face_source': 'cymple'})
        self.runtime.configure({'model_path': ''})
        with self.assertRaisesRegex(ValueError, 'Configure a model checkpoint path before starting'):
            self.runtime.prepare()
        for change in [{'stale_ms': float('nan')}, {'preview_fps': 0}, {'quality': 80}]:
            with self.assertRaises(ValueError):
                self.runtime.configure(change)

    def test_portable_defaults_require_explicit_headset_before_live_start(self):
        runtime = Runtime(Path(self.tmp.name)/'fresh.json', lambda config: self.model)
        try:
            self.assertEqual(runtime.config['headset_host'], '')
            self.assertFalse(runtime.config['output_enabled'])
            self.assertFalse(runtime.running)
            self.assertEqual(Path(runtime.config['model_path']), ROOT/'assets/shadow-final-v2.pt')
            with self.assertRaisesRegex(ValueError, 'Enter the headset address'):
                runtime.start()
            self.assertFalse(runtime.threads)
        finally:
            runtime.stop()
            runtime.udp.close()

    def test_predict_duplicate_age_and_stop(self):
        self.start()
        h = header()
        self.submit(h)
        wait_for(lambda: self.runtime.counts['inferred'] == 1)
        self.assertFalse(self.submit(h))
        wait_for(lambda: self.runtime.prediction['state'] == 'stale')
        self.assertFalse(self.runtime.prediction['override'])
        self.runtime.stop()
        self.assertFalse(self.runtime.running)
        self.assertIsNone(self.runtime.preview)
        summary = json.loads((self.runtime.run_dir/'summary.json').read_text(encoding='utf-8'))
        self.assertFalse(summary['running'])

    def test_bounded_latest_handoff_and_old_session_late_completion(self):
        self.model.block = True
        self.start()
        self.submit(header())
        self.model.entered.wait(1)
        self.submit(header(11))
        self.submit(header(12))
        self.assertEqual(self.runtime.pending[0].source_seq, 12)
        self.assertEqual(self.runtime.counts['pending_dropped'], 1)
        self.submit(header(1, session=2))
        self.model.release.set()
        wait_for(lambda: self.runtime.prediction_source is not None)
        self.assertEqual(self.runtime.prediction_source.session_id, 2)
        self.assertEqual(self.runtime.counts['superseded'], 1)

    def test_aged_new_sequences_do_not_hold_fresh_output(self):
        self.start()
        self.submit(header())
        wait_for(lambda: self.runtime.counts['inferred'] == 1)
        for seq in range(11, 38):
            self.submit(header(seq, age_ms=300))
            time.sleep(.005)
        self.assertFalse(self.runtime.prediction['override'])

    def test_connection_change_releases_before_reconfiguration(self):
        self.start()
        self.submit(header())
        wait_for(lambda: self.runtime.counts['inferred'] == 1)
        self.runtime.configure({'headset_host': '127.0.0.2', 'enabled': True})
        self.assertFalse(self.runtime.running)
        self.assertFalse(self.runtime.config['enabled'])
        self.assertFalse(self.runtime.prediction['override'])

    def test_folded_web_settings_save_without_start_and_keep_udp_preference(self):
        changes = dict(headset_host='127.0.0.1', headset_port=45681, model_path='test-only', device='cpu',
                       smoothing_ms=0, uncertain_hold_ms=0, stale_ms=275, preview_fps=4,
                       output_enabled=False, output_host='127.0.0.1', output_port=45692)
        saved = self.runtime.configure(changes)
        self.assertEqual({key: saved[key] for key in changes}, changes)
        self.assertFalse(self.runtime.running)
        self.assertIsNone(self.runtime.model)
        self.start()
        self.assertFalse(self.runtime.config['output_enabled'])
        self.runtime.configure({'smoothing_ms': 30})
        self.assertTrue(self.runtime.running)

    def test_advanced_restart_settings_stop_the_actual_runtime(self):
        for key, value in [('device', 'cpu'), ('output_port', 45693), ('output_enabled', True)]:
            with self.subTest(field=key):
                self.start()
                saved = self.runtime.configure({key: value})
                self.assertFalse(saved['enabled'])
                self.assertFalse(self.runtime.status()['running'])
                self.assertFalse(self.runtime.connected)
                self.assertIsNone(self.runtime.preview)

    def test_source_session_change_immediately_releases_old_prediction(self):
        self.start()
        self.submit(header())
        wait_for(lambda: self.runtime.counts['inferred'] == 1)
        self.model.block = True
        self.submit(header(1, session=2))
        wait_for(lambda: self.runtime.pending is None)
        self.assertFalse(self.runtime.prediction['override'])
        self.assertIsNone(self.runtime.last_header)
        self.model.release.set()

    def test_tjr_output_owner_changes_on_pc_restart(self):
        self.start()
        self.submit(header())
        wait_for(lambda: self.runtime.counts['inferred'] == 1)
        old = self.runtime.output_session_id
        self.runtime.stop()
        self.start()
        self.submit(header(11))
        wait_for(lambda: self.runtime.counts['inferred'] == 1)
        self.assertNotEqual(self.runtime.output_session_id, old)
        self.assertEqual(self.runtime.prediction_source.session_id, 1)
        self.assertEqual(self.runtime.prediction_source.source_seq, 11)

    def test_released_uncertain_is_invalid_on_wire(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        sock.bind(('127.0.0.1', 0))
        sock.settimeout(1)
        self.runtime.configure({'output_enabled': True, 'output_port': sock.getsockname()[1]})
        self.runtime._send_result(header(), TongueState.invalid('uncertain'), 0.)
        packet = RESULT.unpack(sock.recv(64))
        self.assertEqual(packet[-2], 8)

    def test_stop_cancels_inflight_model_start(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def slow_factory(config):
            entered.set()
            release.wait(2)
            return self.model
        self.runtime.model_factory = slow_factory
        def start():
            try:
                self.runtime.start()
            except RuntimeError as error:
                errors.append(str(error))
        starter = threading.Thread(target=start)
        starter.start()
        self.assertTrue(entered.wait(1))
        self.runtime.stop()
        release.set()
        starter.join(2)
        self.assertEqual(errors, ['Startup was cancelled by stop/shutdown'])
        self.assertFalse(self.runtime.running)
        self.assertEqual(self.runtime.threads, [])

    def test_live_capture_preserves_complete_headers_and_payloads(self):
        payload = jpeg()
        expected = []
        class Source(socketserver.BaseRequestHandler):
            def handle(inner):
                command = bytearray()
                while not command.endswith(b'\n'):
                    command.extend(inner.request.recv(1))
                if command == b'PING\n':
                    a = time.perf_counter_ns()
                    inner.request.sendall(struct.pack('<QQ', a, time.perf_counter_ns()))
                elif command == b'JPEG\n':
                    for seq in (10, 11):
                        h = replace(header(seq), payload_bytes=len(payload))
                        expected.append(h)
                        inner.request.sendall(h.pack()+payload)
                        time.sleep(.03)
                    inner.request.sendall(replace(header(12), payload_bytes=0, flags=END).pack())
        server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Source)
        serving = threading.Thread(target=server.serve_forever)
        serving.start()
        runtime = Runtime(Path(self.tmp.name)/'network-config.json', lambda config: self.model,
                          Path(self.tmp.name)/'network-runs', record_input=True)
        try:
            runtime.configure({'model_path': 'test-only', 'headset_host': '127.0.0.1',
                               'headset_port': server.server_address[1]})
            runtime.start()
            wait_for(lambda: runtime.counts['inferred'] == 2)
            wait_for(lambda: not runtime.connected)
            self.assertIsNone(runtime.last_error)
            runtime.stop()
            calibrations = [json.loads(line) for line in (runtime.run_dir/'clock.jsonl').read_text().splitlines()]
            self.assertEqual(len(calibrations), 1)
            self.assertEqual(len(calibrations[0]['samples']), 12)
            with (runtime.run_dir/'input.ptj').open('rb') as stream:
                for h in expected:
                    self.assertEqual(FrameHeader.unpack(stream.read(128)), h)
                    self.assertEqual(stream.read(h.payload_bytes), payload)
                self.assertEqual(stream.read(1), b'')
        finally:
            runtime.stop()
            runtime.udp.close()
            server.shutdown()
            serving.join()
            server.server_close()

    def test_http_config_status_errors_and_stop(self):
        server = make_server(self.runtime, port=0)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        with urlopen(base+'/api/status') as response:
            self.assertFalse(json.load(response)['running'])
        with urlopen(base+'/api/preview.jpg') as response:
            self.assertEqual(response.status, 204)
        for module in (ROOT/'web').glob('*.mjs'):
            with self.subTest(module=module.name), urlopen(base+'/static/'+module.name) as response:
                self.assertIn(response.headers.get_content_type(), ('text/javascript', 'application/javascript'))
                self.assertEqual(response.read(), module.read_bytes())
        request = Request(base+'/api/config', json.dumps({'model_path': ''}).encode(),
                          {'Content-Type': 'application/json'})
        with urlopen(request) as response:
            self.assertEqual(json.load(response)['model_path'], '')
        request = Request(base+'/api/start', b'{}', {'Content-Type': 'application/json'})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(json.load(error.exception)['error'],
                         'Configure a model checkpoint path before starting')
        error.exception.close()

    def test_legacy_pc_source_setting_does_not_decide_module_ownership(self):
        path = Path(self.tmp.name)/'legacy.json'
        path.write_text(json.dumps({'headset_host': '127.0.0.1', 'face_source': 'cymple', 'model_path': 'test-only'}))
        runtime = ManualRuntime(path, lambda config: self.model, Path(self.tmp.name)/'legacy-runs')
        try:
            self.assertNotIn('face_source', runtime.config)
            runtime.start()
            wait_for(lambda: runtime.connected)
            self.assertTrue(runtime.running)
        finally:
            runtime.stop()
            runtime.udp.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
