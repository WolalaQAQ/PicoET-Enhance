"""Offline tests for PICO 4 Pro to Cymple native UDP eye relay."""
import io
import json
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

from backend import make_server
from cymple_relay import (
    CHUNK_BYTES,
    MAX_CYMPLE_FRAME_BYTES,
    MSG_TYPE_NATIVE_FRAME,
    PKT_HEADER,
    CympleEyeRelay,
    encode_cymple_packets,
    prepare_jpeg,
)
from runtime import ROOT, Runtime
from test_runtime import header, wait_for


def _corner_jpeg(size=(400, 400), quality=95):
    w, h = size
    img = Image.new('L', (w, h), 20)
    patch = Image.new('L', (w // 4, h // 4), 240)
    img.paste(patch, (0, 0))
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=quality)
    return buf.getvalue()


class _FakeEyeStream:
    """Loopback PTJ1 TCP server emitting JPEG frames upon b'JPEG\\n'."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.stopped = threading.Event()
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                cmd = bytearray()
                while not cmd.endswith(b'\n'):
                    chunk = self.request.recv(1)
                    if not chunk:
                        return
                    cmd.extend(chunk)
                if bytes(cmd) != b'JPEG\n':
                    return
                seq = 0
                try:
                    while not owner.stopped.is_set():
                        seq += 1
                        hdr = header(seq, 80001, payload_bytes=len(owner.payload))
                        self.request.sendall(hdr.pack() + owner.payload)
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


class _UdpReassembler:
    """Capture and reassemble chunked Cymple native UDP type-1 frames by channel."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.frames = {0: [], 1: []}
        self._inflight = {}
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stopped.is_set():
            try:
                data, _ = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < PKT_HEADER.size:
                continue
            msg_type, payload_len, offset, total, channel, seq_byte, _ = PKT_HEADER.unpack_from(data, 0)
            if msg_type != MSG_TYPE_NATIVE_FRAME or len(data) != payload_len + 4:
                continue
            chunk = data[PKT_HEADER.size:]
            key = (channel, seq_byte, total)
            buf = self._inflight.setdefault(key, {})
            buf[offset] = chunk
            if sum(len(v) for v in buf.values()) >= total:
                ordered = bytearray()
                pos = 0
                while pos < total and pos in buf:
                    part = buf[pos]
                    ordered.extend(part)
                    pos += len(part)
                if len(ordered) == total:
                    self._inflight.pop(key, None)
                    with self.lock:
                        self.frames.setdefault(channel, []).append(bytes(ordered))

    def close(self):
        self.stopped.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self.thread.join(timeout=2)


class CympleRelayTests(unittest.TestCase):
    def test_packet_encoding_chunks_seq_wrap_and_bounds(self):
        payload = bytes(i & 0xFF for i in range(3000))
        packets = encode_cymple_packets(payload, channel=1, frame_seq=257)
        self.assertEqual(len(packets), 3)
        reassembled = bytearray()
        expected_offset = 0
        for pkt in packets:
            msg_type, payload_len, offset, total, channel, seq_byte, reserved = PKT_HEADER.unpack_from(pkt, 0)
            self.assertEqual(msg_type, MSG_TYPE_NATIVE_FRAME)
            self.assertEqual(len(pkt), payload_len + 4)
            self.assertEqual(offset, expected_offset)
            self.assertEqual(total, 3000)
            self.assertEqual(channel, 1)
            self.assertEqual(seq_byte, 1)
            self.assertEqual(reserved, 0)
            chunk = pkt[PKT_HEADER.size:]
            self.assertLessEqual(len(chunk), CHUNK_BYTES)
            reassembled.extend(chunk)
            expected_offset += len(chunk)
        self.assertEqual(bytes(reassembled), payload)

        with self.assertRaises(ValueError):
            encode_cymple_packets(b'', 0, 0)
        with self.assertRaises(ValueError):
            encode_cymple_packets(b'x' * (MAX_CYMPLE_FRAME_BYTES + 1), 0, 0)

    def test_prepare_jpeg_passthrough_orientation_and_oversize_reencode(self):
        raw = _corner_jpeg((120, 60))
        self.assertEqual(prepare_jpeg(raw, 0, False, False), raw)

        r90 = Image.open(io.BytesIO(prepare_jpeg(raw, rotate_cw=90)))
        self.assertEqual(r90.size, (60, 120))
        self.assertGreater(r90.getpixel((50, 10)), 180)
        self.assertLess(r90.getpixel((10, 10)), 60)

        r180 = Image.open(io.BytesIO(prepare_jpeg(raw, rotate_cw=180)))
        self.assertEqual(r180.size, (120, 60))
        self.assertGreater(r180.getpixel((110, 50)), 180)

        r270 = Image.open(io.BytesIO(prepare_jpeg(raw, rotate_cw=270)))
        self.assertEqual(r270.size, (60, 120))
        self.assertGreater(r270.getpixel((10, 110)), 180)

        hf = Image.open(io.BytesIO(prepare_jpeg(raw, hflip=True)))
        self.assertGreater(hf.getpixel((110, 10)), 180)

        vf = Image.open(io.BytesIO(prepare_jpeg(raw, vflip=True)))
        self.assertGreater(vf.getpixel((10, 50)), 180)

        rng = bytes(((i * 73 + (i >> 3)) & 0xFF) for i in range(400 * 400))
        noisy = Image.frombytes('L', (400, 400), rng)
        buf = io.BytesIO()
        noisy.save(buf, format='JPEG', quality=100, subsampling=0)
        big = buf.getvalue()
        self.assertGreater(len(big), MAX_CYMPLE_FRAME_BYTES)
        shrunk = prepare_jpeg(big, 0, False, False)
        self.assertLessEqual(len(shrunk), MAX_CYMPLE_FRAME_BYTES)

    def test_relay_end_to_end_http_persistence_and_preview_stop_isolation(self):
        left_cam = _FakeEyeStream(_corner_jpeg((400, 400)))
        right_cam = _FakeEyeStream(_corner_jpeg((400, 400)))
        sink = _UdpReassembler()
        self.addCleanup(left_cam.close)
        self.addCleanup(right_cam.close)
        self.addCleanup(sink.close)

        work = ROOT / 'tests-work'
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as folder:
            cfg_dir = Path(folder)
            runtime = Runtime(cfg_dir / 'config.json')
            relay_cfg_path = cfg_dir / 'cymple_relay.json'
            relay = CympleEyeRelay(relay_cfg_path, runtime=runtime)
            server = make_server(runtime, port=0, cymple_relay=relay)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(base + '/api/status') as r:
                    st0 = json.load(r)
                self.assertEqual(st0['cymple_relay']['state'], 'idle')
                self.assertFalse(st0['cymple_relay']['running'])
                self.assertFalse(relay_cfg_path.exists())

                req = Request(
                    base + '/api/cymple-relay',
                    json.dumps({
                        'action': 'start',
                        'headset_host': '127.0.0.1',
                        'cymple_host': '127.0.0.1',
                        'cymple_port': sink.port,
                        'left_port': left_cam.port,
                        'right_port': right_cam.port,
                        'left_rotate': 90,
                        'right_vflip': True,
                    }).encode('utf-8'),
                    {'Content-Type': 'application/json'},
                )
                with urlopen(req) as r:
                    started = json.load(r)
                self.assertTrue(started['running'])

                wait_for(lambda: (
                    relay.status()['state'] == 'running'
                    and relay.status()['eyes']['eye_left']['frames'] >= 3
                    and relay.status()['eyes']['eye_right']['frames'] >= 3
                    and relay.status()['eyes']['eye_left']['fps'] > 0
                    and len(sink.frames[0]) >= 2
                    and len(sink.frames[1]) >= 2
                ), timeout=5.0)

                with urlopen(base + '/api/status') as r:
                    st1 = json.load(r)
                cr = st1['cymple_relay']
                self.assertEqual(cr['state'], 'running')
                self.assertGreater(cr['eyes']['eye_left']['fps'], 0)
                self.assertGreater(cr['eyes']['eye_right']['avg_kb'], 0)

                with sink.lock:
                    im_l = Image.open(io.BytesIO(sink.frames[0][-1]))
                    im_r = Image.open(io.BytesIO(sink.frames[1][-1]))
                self.assertGreater(im_l.getpixel((350, 50)), 180)
                self.assertLess(im_l.getpixel((50, 50)), 60)
                self.assertGreater(im_r.getpixel((50, 350)), 180)
                self.assertLess(im_r.getpixel((50, 50)), 60)

                # Stopping camera /api/stop must NOT stop the independent eye relay.
                req_prev_stop = Request(base + '/api/stop', b'{}', {'Content-Type': 'application/json'})
                with urlopen(req_prev_stop) as r:
                    st_after_stop = json.load(r)
                self.assertTrue(st_after_stop['cymple_relay']['running'])

                t0 = time.monotonic()
                req_stop = Request(base + '/api/cymple-relay', b'{"action":"stop"}', {'Content-Type': 'application/json'})
                with urlopen(req_stop) as r:
                    stopped = json.load(r)
                self.assertLess(time.monotonic() - t0, 2.0)
                self.assertFalse(stopped['running'])
                self.assertEqual(stopped['state'], 'idle')
                self.assertTrue(relay_cfg_path.exists())

                reloaded = CympleEyeRelay(relay_cfg_path, runtime=runtime)
                self.assertEqual(reloaded.config['left_rotate'], 90)
                self.assertTrue(reloaded.config['right_vflip'])
                self.assertEqual(reloaded.config['cymple_port'], sink.port)
            finally:
                relay.close()
                runtime.close()
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

    def test_discovery_guards_validation_and_origin_block(self):
        cam_l = _FakeEyeStream(_corner_jpeg((400, 400)))
        cam_r = _FakeEyeStream(_corner_jpeg((400, 400)))
        sink = _UdpReassembler()
        self.addCleanup(cam_l.close)
        self.addCleanup(cam_r.close)
        self.addCleanup(sink.close)

        empty_relay = CympleEyeRelay(discover=lambda timeout=1.5: {'devices': []})
        with self.assertRaisesRegex(ValueError, 'No PICO camera service found'):
            empty_relay.start({'headset_host': ''})

        multi_relay = CympleEyeRelay(discover=lambda timeout=1.5: {'devices': [{'host': '192.0.2.10'}, {'host': '192.0.2.11'}]})
        with self.assertRaisesRegex(ValueError, 'Multiple PICO camera services found'):
            multi_relay.start({'headset_host': ''})

        one_device = {
            'devices': [{
                'host': '127.0.0.1',
                'port': 45681,
                'name': 'PICO Cameras',
                'streams': [
                    {'id': 'mouth', 'port': 45681, 'path': '/stream.mjpg'},
                    {'id': 'eye_left', 'port': cam_l.port, 'path': '/stream.mjpg'},
                    {'id': 'eye_right', 'port': cam_r.port, 'path': '/stream.mjpg'},
                ],
            }]
        }
        auto_relay = CympleEyeRelay(discover=lambda timeout=1.5: one_device)
        self.addCleanup(auto_relay.close)
        for bad in (
            {'cymple_host': ''},
            {'cymple_port': 0},
            {'left_port': True},
            {'right_port': 70000},
            {'left_rotate': 45},
            {'right_hflip': 'yes'},
        ):
            with self.assertRaisesRegex(ValueError, 'Invalid Cymple relay settings'):
                auto_relay.configure(bad)

        st = auto_relay.start({'cymple_host': '127.0.0.1', 'cymple_port': sink.port})
        self.assertEqual(st['source_host'], '127.0.0.1')
        self.assertEqual(st['left_port'], cam_l.port)
        self.assertEqual(st['right_port'], cam_r.port)
        wait_for(lambda: auto_relay.status()['eyes']['eye_left']['frames'] >= 1, timeout=3.0)
        with self.assertRaisesRegex(ValueError, 'Stop the Cymple eye relay'):
            auto_relay.configure({'left_rotate': 90})
        auto_relay.stop()

        work = ROOT / 'tests-work'
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as folder:
            rt = Runtime(Path(folder) / 'config.json')
            server = make_server(rt, port=0)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
            thread.start()
            try:
                req = Request(
                    f'http://127.0.0.1:{server.server_port}/api/cymple-relay',
                    b'{"action":"start"}',
                    {'Content-Type': 'application/json', 'Origin': 'http://evil.example'},
                )
                with self.assertRaises(HTTPError) as err:
                    urlopen(req)
                self.assertEqual(err.exception.code, 403)
                err.exception.close()
            finally:
                server.relay.close()
                rt.close()
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()


if __name__ == '__main__':
    unittest.main()
