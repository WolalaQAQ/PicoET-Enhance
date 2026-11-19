"""Offline loopback tests for PC Studio camera forwarding preview, update check, and HTTP API."""
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
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

from backend import is_instance_running, make_server, stop_running_instance
from runtime import DEFAULT_CONFIG, ROOT, Runtime
from stock import CaptureContext
from updater import APP_VERSION, UpdateChecker, classify_assets, is_newer_version, parse_version
from wire import END, JPEG, RAW, FrameHeader, IdleStreamTimeout, SourceOrder, read_exact, read_frame


def header(seq=10, session=1, flags=JPEG, payload_bytes=0, age_ms=0):
    now = time.perf_counter_ns() - int(age_ms * 1e6)
    return FrameHeader(
        payload_bytes, flags, session, seq, seq * 11_111_111, seq * 11_111_111 + 4,
        now, now, now, now, seq * 11_111_111 - 2, seq * 11_111_111 - 1,
        seq * 11_111_111, seq, raw_crc32=42,
    )


def jpeg(fill=100):
    output = io.BytesIO()
    Image.new('L', (400, 400), fill).save(output, format='JPEG', quality=95)
    return output.getvalue()


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Timed out waiting for condition')
        time.sleep(0.01)


class _FakeCameraServer:
    def __init__(self, *, mode='jpeg', fill=100, session_id=18446744073709551000, generation=42, send_end=False):
        self.mode = mode
        self.payload = jpeg(fill) if mode == 'jpeg' else bytes([fill]) * (400 * 400)
        self.flags = JPEG if mode == 'jpeg' else RAW
        self.session_id = session_id
        self.generation = generation
        self.send_end = send_end
        self.stopped = threading.Event()
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                cmd = bytearray()
                while not cmd.endswith(b'\n'):
                    ch = self.request.recv(1)
                    if not ch:
                        return
                    cmd.extend(ch)
                line = bytes(cmd)
                if line == b'PING\n':
                    a = time.perf_counter_ns()
                    self.request.sendall(struct.pack('<QQ', a, time.perf_counter_ns()))
                    return
                if line == b'CONTEXT\n':
                    self.request.sendall(CaptureContext(owner.session_id, owner.generation).pack())
                    return
                if line == b'JPEG\n':
                    seq = 0
                    try:
                        while not owner.stopped.is_set():
                            seq += 1
                            hdr = header(seq, owner.session_id, owner.flags, len(owner.payload))
                            self.request.sendall(hdr.pack() + owner.payload)
                            if owner.send_end and seq >= 2:
                                self.request.sendall(header(seq + 1, owner.session_id, END, 0).pack())
                                return
                            owner.stopped.wait(0.015)
                    except OSError:
                        pass

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True

        self.server = Server(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()


class WireAndTimeoutTests(unittest.TestCase):
    def test_roundtrip_fragmentation_and_source_order(self):
        h = header(payload_bytes=len(jpeg()))
        self.assertEqual(len(h.pack()), 128)
        self.assertEqual(FrameHeader.unpack(h.pack()), h)
        self.assertEqual(h.identity(), replace(h, flags=RAW, payload_bytes=160000).identity())

        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        h_frag = replace(header(), payload_bytes=17)
        data = h_frag.pack() + b'x' * 17

        def send():
            for i in range(0, len(data), 3):
                left.sendall(data[i:i + 3])
            left.shutdown(socket.SHUT_WR)

        worker = threading.Thread(target=send)
        worker.start()
        self.assertEqual(read_frame(right), (h_frag, b'x' * 17))
        worker.join()
        with self.assertRaises(EOFError):
            read_frame(right)

        order = SourceOrder()
        self.assertTrue(order.accept(header(10))[0])
        self.assertFalse(order.accept(header(10))[0])
        self.assertFalse(order.accept(header(9))[0])
        self.assertEqual(order.accept(header(13))[2], 2)
        self.assertFalse(order.accept(replace(header(14), mouth_sensor_ns=1))[0])
        self.assertTrue(order.accept(header(1, session=2))[0])
        self.assertFalse(order.accept(header(20, session=1))[0])

    def test_live_socket_allows_native_cold_start_then_restores_stream_timeout(self):
        work = ROOT / 'tests-work'
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as folder:
            rt = Runtime(Path(folder) / 'config.json')
            self.addCleanup(rt.close)
            rt.configure({'headset_host': '127.0.0.1'})
            rt.inference_ready.set()
            rt._publish = MagicMock()
            rt._set_clock = MagicMock()
            sock = MagicMock()
            packets = [
                (replace(header(), payload_bytes=10), b'0' * 10),
                (replace(header(11), flags=END, payload_bytes=0), b''),
            ]
            with patch('runtime.sync_clock', return_value={}), \
                 patch('runtime.read_capture_context', side_effect=OSError('skip')), \
                 patch('runtime.socket.create_connection', return_value=sock) as mock_conn, \
                 patch('runtime.read_frame', side_effect=packets):
                rt._receive()
            self.assertEqual(mock_conn.call_args_list[-1], call(('127.0.0.1', DEFAULT_CONFIG['headset_port']), timeout=3))
            self.assertEqual(sock.settimeout.call_args_list, [call(10), call(1)])
            rt._publish.assert_called_once()
            self.assertIsNone(rt.last_error)

    def test_idle_frame_boundary_timeout_waits_while_midframe_timeout_stops_runtime(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        right.settimeout(0.05)
        with self.assertRaises(IdleStreamTimeout):
            read_exact(right, 128, part='header')
        left.sendall(b'PTJ1')
        with self.assertRaises(TimeoutError) as ctx:
            read_exact(right, 128, part='header')
        self.assertNotIsInstance(ctx.exception, IdleStreamTimeout)

        work = ROOT / 'tests-work'
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as folder:
            rt = Runtime(Path(folder) / 'config.json')
            self.addCleanup(rt.close)
            rt.configure({'headset_host': '127.0.0.1'})

            # Case 1: IdleStreamTimeout waits in connected-idle without error until a frame arrives.
            allow_frame = threading.Event()
            close_stream = threading.Event()
            raw_jpeg = jpeg(95)

            def fake_read_frame(_sock):
                if close_stream.is_set():
                    raise EOFError('PTJ1 stream closed between frames')
                if not allow_frame.is_set() or getattr(fake_read_frame, 'sent', False):
                    time.sleep(0.02)
                    raise IdleStreamTimeout('Waiting for next PTJ1 frame')
                fake_read_frame.sent = True
                return replace(header(1), payload_bytes=len(raw_jpeg)), raw_jpeg

            fake_read_frame.sent = False
            with patch('runtime.sync_clock', side_effect=OSError('skip')), \
                 patch('runtime.read_capture_context', side_effect=OSError('skip')), \
                 patch('runtime.socket.create_connection', return_value=MagicMock()), \
                 patch('runtime.read_frame', side_effect=fake_read_frame):
                rt.start('capture')
                wait_for(lambda: rt.connected and rt.running)
                time.sleep(0.06)
                st_idle = rt.status()
                self.assertTrue(st_idle['running'])
                self.assertTrue(st_idle['connected'])
                self.assertFalse(st_idle['metrics']['video_fresh'])
                self.assertIsNone(st_idle['last_error'])
                allow_frame.set()
                wait_for(lambda: rt.counts.get('decoded', 0) >= 1)
                self.assertTrue(rt.status()['metrics']['video_fresh'])
                close_stream.set()
                wait_for(lambda: not rt.running)
                self.assertFalse(rt.connected)
            rt.stop()

            # Case 2: Fatal timeout clears running=False (never zombie running=True, connected=False).
            with patch('runtime.sync_clock', side_effect=OSError('skip')), \
                 patch('runtime.read_capture_context', side_effect=OSError('skip')), \
                 patch('runtime.socket.create_connection', side_effect=TimeoutError('timed out')):
                rt.start('capture')
                wait_for(lambda: not rt.running)
            st_err = rt.status()
            self.assertFalse(st_err['running'])
            self.assertFalse(st_err['connected'])
            self.assertEqual(st_err['last_error'], 'timed out')


class RuntimeAndHttpTests(unittest.TestCase):
    def test_config_validation_and_multi_camera_preview_api(self):
        mouth = _FakeCameraServer(mode='jpeg', fill=70)
        eye_l = _FakeCameraServer(mode='jpeg', fill=130)
        eye_r = _FakeCameraServer(mode='raw', fill=190)
        self.addCleanup(mouth.close)
        self.addCleanup(eye_l.close)
        self.addCleanup(eye_r.close)

        def fake_discover(timeout=1.2, targets=None):
            return {
                'devices': [{
                    'host': '127.0.0.1',
                    'port': mouth.port,
                    'name': 'PICO Cameras',
                    'session_id': str(mouth.session_id),
                    'service_generation': str(mouth.generation),
                    'streams': [
                        {'id': 'mouth', 'port': mouth.port, 'path': '/stream.mjpg', 'url': f'http://127.0.0.1:{mouth.port}/stream.mjpg'},
                        {'id': 'eye_left', 'port': eye_l.port, 'path': '/stream.mjpg', 'url': f'http://127.0.0.1:{eye_l.port}/stream.mjpg'},
                        {'id': 'eye_right', 'port': eye_r.port, 'path': '/stream.mjpg', 'url': f'http://127.0.0.1:{eye_r.port}/stream.mjpg'},
                    ],
                }]
            }

        work = ROOT / 'tests-work'
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as folder:
            cfg_path = Path(folder) / 'config.json'
            cfg_path.write_text(json.dumps({'model_path': 'D:/branch.onnx', 'device': 'cuda'}), encoding='utf-8')
            rt = Runtime(cfg_path, run_root=Path(folder) / 'runs', record_input=True)
            self.assertEqual(rt.config['headset_host'], '')
            self.assertFalse(rt.running)
            with self.assertRaisesRegex(ValueError, 'Enter the headset address'):
                rt.start()

            for bad in ({'unknown_key': 1}, {'preview_fps': 0}, {'preview_camera': 'invalid'}, {'headset_port': 70000}):
                with self.assertRaises(ValueError):
                    rt.configure(bad)

            server = make_server(rt, port=0, discover=fake_discover)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(base + '/api/status') as r:
                    st0 = json.load(r)
                self.assertEqual(st0['version'], APP_VERSION)
                self.assertFalse(st0['running'])
                with urlopen(base + '/api/preview.jpg') as r:
                    self.assertEqual(r.status, 204)

                # Static JS module serves cleanly and path traversal outside web/ fails with 404.
                with urlopen(base + '/static/app.mjs') as r:
                    self.assertIn('javascript', r.headers.get('Content-Type', ''))
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(base + '/static/../runtime.py')
                self.assertEqual(ctx.exception.code, 404)
                ctx.exception.close()

                # Cross-origin POST is rejected with 403.
                bad_req = Request(
                    base + '/api/start',
                    b'{"mode":"capture"}',
                    {'Content-Type': 'application/json', 'Origin': 'http://evil.example'},
                )
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(bad_req)
                self.assertEqual(ctx.exception.code, 403)
                ctx.exception.close()

                # Discovery returns the 3-stream PICO Cameras device.
                with urlopen(base + '/api/discovery') as r:
                    disc = json.load(r)
                self.assertEqual(len(disc['devices']), 1)

                # Ephemeral headset_host in /api/start connects without saving into config.json.
                with urlopen(Request(
                    base + '/api/config',
                    json.dumps({'headset_host': '', 'headset_port': 45681}).encode('utf-8'),
                    {'Content-Type': 'application/json'},
                )) as r:
                    self.assertEqual(json.load(r)['headset_host'], '')
                with urlopen(Request(
                    base + '/api/start',
                    json.dumps({
                        'mode': 'capture',
                        'camera': 'eye_left',
                        'headset_host': '127.0.0.1',
                        'headset_port': mouth.port,
                        'eye_left_port': eye_l.port,
                        'eye_right_port': eye_r.port,
                    }).encode('utf-8'),
                    {'Content-Type': 'application/json'},
                )) as r:
                    st_eph = json.load(r)
                self.assertTrue(st_eph['running'])
                self.assertEqual(st_eph['config']['headset_host'], '')
                wait_for(lambda: rt.preview is not None and rt.counts['decoded'] >= 1)
                with urlopen(Request(base + '/api/stop', b'{}', {'Content-Type': 'application/json'})) as r:
                    self.assertFalse(json.load(r)['running'])
                self.assertEqual(json.loads(cfg_path.read_text(encoding='utf-8'))['headset_host'], '')

                # Configure headset host + all 3 ports while preserving extra branch keys on disk.
                cfg_req = Request(
                    base + '/api/config',
                    json.dumps({
                        'headset_host': '127.0.0.1',
                        'headset_port': mouth.port,
                        'eye_left_port': eye_l.port,
                        'eye_right_port': eye_r.port,
                        'preview_camera': 'mouth',
                        'preview_fps': 20,
                    }).encode('utf-8'),
                    {'Content-Type': 'application/json'},
                )
                with urlopen(cfg_req) as r:
                    saved_cfg = json.load(r)
                self.assertEqual(saved_cfg['preview_fps'], 20)
                disk_cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
                self.assertEqual(disk_cfg['model_path'], 'D:/branch.onnx')

                # Preview mouth (JPEG) then switch to eye_right (RAW) -> both produce 400x400 JPEG previews.
                for cam_id in ('mouth', 'eye_right'):
                    start_req = Request(
                        base + '/api/start',
                        json.dumps({'mode': 'capture', 'camera': cam_id}).encode('utf-8'),
                        {'Content-Type': 'application/json'},
                    )
                    with urlopen(start_req) as r:
                        st = json.load(r)
                    self.assertTrue(st['running'])
                    self.assertEqual(st['preview_camera'], cam_id)

                    wait_for(lambda: rt.preview is not None and rt.counts['decoded'] >= 2)
                    with urlopen(base + '/api/preview.jpg') as r:
                        self.assertEqual(r.status, 200)
                        im = Image.open(io.BytesIO(r.read()))
                        self.assertEqual(im.size, (400, 400))

                    with urlopen(Request(base + '/api/stop', b'{}', {'Content-Type': 'application/json'})) as r:
                        stopped = json.load(r)
                    self.assertFalse(stopped['running'])
                    self.assertIsNone(rt.preview)

                with urlopen(base + '/api/preview.jpg') as r:
                    self.assertEqual(r.status, 204)
            finally:
                server.relay.close()
                rt.close()
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

    def test_update_checker_and_shutdown_endpoint(self):
        self.assertTrue(is_newer_version('v2.7.0', '2.6.0'))
        self.assertTrue(is_newer_version('2.6.0', '2.6.0-dev.2'))
        self.assertFalse(is_newer_version('v2.6.0', '2.6.0'))
        self.assertFalse(is_newer_version('2.5.2', '2.6.0'))
        self.assertIsNone(parse_version('not-a-version'))

        calls = []

        class _FakeResp:
            def __init__(self, payload):
                self._raw = json.dumps(payload).encode('utf-8')
            def read(self):
                return self._raw
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        def fake_opener(req, timeout=4.0):
            calls.append(req.full_url)
            return _FakeResp({
                'tag_name': 'v2.7.0',
                'html_url': 'https://github.com/WolalaQAQ/PicoET-Enhance/releases/tag/v2.7.0',
                'published_at': '2026-11-25T00:00:00Z',
                'body': '## [2.7.0]\n### Features\n- Add installer and update check.\n- Improve preview.',
                'assets': [
                    {'name': 'PicoET-PC-Studio-Setup-v2.7.0.exe', 'browser_download_url': 'https://example.invalid/Setup.exe'},
                    {'name': 'picoet-pc-studio-v2.7.0.zip', 'browser_download_url': 'https://example.invalid/studio.zip'},
                    {'name': 'magisk-picoet-enhance-v2.7.0.zip', 'browser_download_url': 'https://example.invalid/magisk.zip'},
                    {'name': 'magisk-picoet-enhance-v2.7.0.zip.sha256', 'browser_download_url': 'https://example.invalid/magisk.sha256'},
                ],
            })

        checker = UpdateChecker(current_version='2.6.0', opener=fake_opener)
        work = ROOT / 'tests-work'
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as folder:
            rt = Runtime(Path(folder) / 'config.json')
            server = make_server(rt, port=0, update_checker=checker)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
            thread.start()
            port = server.server_port
            base = f'http://127.0.0.1:{port}'
            try:
                self.assertTrue(is_instance_running('127.0.0.1', port))
                with urlopen(base + '/favicon.ico') as r:
                    self.assertEqual(r.status, 200)
                    self.assertEqual(r.headers.get('Content-Type'), 'image/x-icon')
                    self.assertTrue(r.read().startswith(b'\x00\x00\x01\x00'))
                with urlopen(base + '/static/icon.png') as r:
                    self.assertEqual(r.status, 200)
                    self.assertEqual(r.headers.get('Content-Type'), 'image/png')
                    im = Image.open(io.BytesIO(r.read()))
                    self.assertEqual(im.size, (256, 256))
                with urlopen(base + '/api/update-check') as r:
                    u1 = json.load(r)
                self.assertTrue(u1['update_available'])
                self.assertEqual(u1['latest_version'], '2.7.0')
                self.assertEqual(u1['installer_name'], 'PicoET-PC-Studio-Setup-v2.7.0.exe')
                self.assertEqual(u1['portable_name'], 'picoet-pc-studio-v2.7.0.zip')
                self.assertEqual(u1['module_name'], 'magisk-picoet-enhance-v2.7.0.zip')
                self.assertEqual(len(u1['notes']), 2)
                self.assertEqual(len(calls), 1)

                with urlopen(base + '/api/update-check') as r:
                    json.load(r)
                self.assertEqual(len(calls), 1)

                with urlopen(base + '/api/update-check?force=1') as r:
                    json.load(r)
                self.assertEqual(len(calls), 2)

                self.assertEqual(stop_running_instance('127.0.0.1', port), 0)
                thread.join(timeout=2)
                self.assertFalse(is_instance_running('127.0.0.1', port))
            finally:
                server.relay.close()
                rt.close()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
