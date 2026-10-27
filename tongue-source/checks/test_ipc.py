"""Host-only, real daemon process + mapped C ABI shim + localhost TCP tests."""
import argparse
import ctypes as C
import hashlib
import io
import json
import os
import pathlib
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
import zlib
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent
BUILD = pathlib.Path(os.environ.get('PTJ_HOST_BUILD', ROOT.parent/'build-host'))
RUN = ROOT / 'tests-work' / str(time.time_ns())
WIRE = struct.Struct('<4sHHII12Q4I')
NAMES = ('magic version header_bytes payload_bytes flags session_id source_seq sensor_ns vendor_ns '
         'copy_mono_ns encode_begin_ns encode_end_ns emit_mono_ns eye0_sensor_ns eye1_sensor_ns '
         'mouth_sensor_ns raw_identity width height raw_crc32 quality').split()
COUNTERS = ('submit_calls enqueued drop_no_subscriber drop_duplicate drop_busy drop_full drop_stopped '
            'drop_session drop_invalid cancel_disconnect cancel_stop encoded encode_errors sent_sources '
            'sent_packets sent_payload_bytes send_errors send_timeouts connections pings queue_peak '
            'copy_count copy_total_ns copy_max_ns copy_last_ns encode_total_ns encode_max_ns last_sent_seq').split()
OBSERVATIONS = []
NEXT_RUNTIME = 0


class Meta(C.Structure):
    _fields_ = [(n, C.c_uint64) for n in ('session_id source_seq sensor_ns vendor_ns eye0_sensor_ns '
                                        'eye1_sensor_ns mouth_sensor_ns raw_identity').split()] + [
        ('flags', C.c_uint32), ('reserved', C.c_uint32)]


class Config(C.Structure):
    _fields_ = [('struct_bytes', C.c_uint32), ('port', C.c_uint16), ('reserved', C.c_uint16),
                ('send_timeout_ms', C.c_uint32), ('reserved2', C.c_uint32), ('session_id', C.c_uint64)]


class Stats(C.Structure):
    _fields_ = [(n, C.c_uint64) for n in
                ['struct_bytes', 'session_id', 'running', 'connected', 'queue_in_use'] + COUNTERS]


class Probe(C.Structure):
    _fields_ = [(n, C.c_uint64) for n in
                ('bytes session daemon_pid daemon_birth heartbeat subscription state pending slot0 slot1 '
                 'in_use peak core_queue completed dispatched cancel_disconnect cancel_stop '
                 'dispatch_dropped stop_ack capture_ns dispatch_ns producer_enqueued '
                 'version client_ready attach_ack error_stage client_pid client_birth request_session '
                 'attach_session stop_session root0 root1 nonce0 nonce1 attach0 attach1 attach_root0 attach_root1 '
                 'stop0 stop1').split()]


PROBE = C.CDLL(str(BUILD / 'ipc_test_probe.dll'))
PROBE.ipc_test_probe.argtypes = [C.c_char_p, C.POINTER(Probe)]
PROBE.ipc_test_probe.restype = C.c_int
PROBE.ipc_test_fixture.argtypes = [C.c_char_p, C.c_uint32]
PROBE.ipc_test_fixture.restype = C.c_int


def wait(predicate, seconds=3):
    deadline = time.perf_counter() + seconds
    while not predicate():
        if time.perf_counter() >= deadline:
            raise AssertionError('predicate timeout')
        time.sleep(.001)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def exact(sock, n):
    data = bytearray()
    while len(data) < n:
        part = sock.recv(n - len(data))
        if not part:
            raise EOFError(f'partial {len(data)}/{n}')
        data += part
    return bytes(data)


def packet(sock):
    h = dict(zip(NAMES, WIRE.unpack(exact(sock, 128))))
    assert (h['magic'], h['version'], h['header_bytes']) == (b'PTJ1', 1, 128)
    return h, exact(sock, h['payload_bytes'])


class Module:
    def __init__(self, folder, existing_library=None):
        self.root = pathlib.Path(folder)
        self.root.mkdir(parents=True, exist_ok=True)
        library = existing_library or self.root / ('shim-' + self.root.name + '.dll')
        if existing_library is None:
            shutil.copy2(BUILD / 'ptj_source.dll', library)
        self.library_path = pathlib.Path(library)
        self.lib = C.CDLL(str(self.library_path))
        self.lib.ptj_start.argtypes = [C.POINTER(Config), C.POINTER(C.c_void_p)]
        self.lib.ptj_start.restype = C.c_int
        self.lib.ptj_session_id.argtypes = [C.c_void_p]
        self.lib.ptj_session_id.restype = C.c_uint64
        self.lib.ptj_submit.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(Meta)]
        self.lib.ptj_submit.restype = C.c_int
        self.lib.ptj_get_stats.argtypes = [C.c_void_p, C.POINTER(Stats)]
        self.lib.ptj_get_stats.restype = C.c_int
        self.lib.ptj_stop.argtypes = [C.c_void_p]
        self.lib.ptj_destroy.argtypes = [C.c_void_p]
        self.sources = []

    def start(self):
        handle = C.c_void_p()
        cfg = Config(C.sizeof(Config), 0, 0, 0, 0, 0)
        result = self.lib.ptj_start(C.byref(cfg), C.byref(handle))
        if result:
            return result, None
        source = Source(self, handle)
        self.sources.append(source)
        return 0, source

    def probe(self):
        p = Probe()
        result = PROBE.ipc_test_probe(str(self.root).encode(), C.byref(p))
        assert result == 0, result
        return {n: getattr(p, n) for n, _ in p._fields_}

    def fixture(self, action):
        assert PROBE.ipc_test_fixture(str(self.root).encode(), action) == 0

    def shim_status(self):
        return json.loads((self.root / 'ipc-shim-status.json').read_text())


class Source:
    def __init__(self, module, handle):
        self.module, self.handle = module, handle
        self.session = module.lib.ptj_session_id(handle)
        self.seq = 0

    def meta(self, seq=None):
        if seq is None:
            self.seq += 1
            seq = self.seq
        return Meta(self.session, seq, 1230000000 + seq * 11111111,
                    4560000000 + seq * 11111111, 111 + seq, 222 + seq, 333 + seq,
                    0xABC00000 + seq, 0, 0)

    def submit(self, data=None, meta=None, stride=400, address=None):
        meta = meta or self.meta()
        if address is None:
            data = data if data is not None else bytes([meta.source_seq % 256]) * 160000
            buf = (C.c_uint8 * len(data)).from_buffer_copy(data)
            address = C.addressof(buf)
        return self.module.lib.ptj_submit(self.handle, address, stride, C.byref(meta))

    def stats(self):
        s = Stats()
        assert self.module.lib.ptj_get_stats(self.handle, C.byref(s)) == 0
        return {n: getattr(s, n) for n, _ in s._fields_}

    def stop(self):
        self.module.lib.ptj_stop(self.handle)

    def close(self):
        if self.handle:
            self.stop()
            OBSERVATIONS.append({'shim': self.module.shim_status(), 'stats': self.stats()})
            self.module.lib.ptj_destroy(self.handle)
            self.handle = None


class Runtime:
    def __init__(self, root, daemon=True, port=None):
        self.module = Module(root)
        self.port = port or free_port()
        self.process = None
        self.sockets = []
        self.responder = None
        if daemon:
            self.launch()

    def launch(self):
        self.process = subprocess.Popen([str(BUILD / 'ptj_ipc_daemon.exe'),
                                         str(self.module.root), str(self.port)],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ready = self.process.stdout.readline()
        if not ready.startswith('READY '):
            output, error = self.process.communicate(timeout=3)
            raise AssertionError(f'daemon not READY: {ready}{output}{error}')
        OBSERVATIONS.append({'ready': ready.strip(), 'root': str(self.module.root)})

    def source(self):
        result, source = self.module.start()
        assert result == 0, (result, self.module.shim_status())
        return source

    def fixture_source(self, action=10):
        self.module.fixture(0)
        self.responder = FixtureResponder(self.module, action)
        return self.source()

    def connect(self, mode=b'JPEG\n', receive_buffer=None, wait_subscription=True):
        sock = socket.socket()
        if receive_buffer:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
        sock.settimeout(3)
        sock.connect(('127.0.0.1', self.port))
        sock.sendall(mode)
        self.sockets.append(sock)
        if wait_subscription and mode in (b'JPEG\n', b'PAIR\n'):
            wait(lambda: self.module.probe()['subscription'] != 0)
        return sock

    def close(self):
        for source in self.module.sources:
            source.close()
        if self.responder:
            self.responder.close()
        if self.process:
            if self.process.poll() is None and not self.module.sources:
                # A test may deliberately leave an unattached ready daemon.
                result, source = self.module.start()
                if result == 0:
                    source.close()
                else:
                    self.process.kill()
            output, error = self.process.communicate(timeout=4)
            (self.module.root / 'daemon-tail.stdout.txt').write_text(output, encoding='utf-8')
            (self.module.root / 'daemon.stderr.txt').write_text(error, encoding='utf-8')
            OBSERVATIONS.append({'daemon_exit': self.process.returncode, 'tail': output, 'stderr': error})
        for sock in self.sockets:
            sock.close()


class FixtureResponder:
    """Only test fixtures echo ACKs; production handshake uses the separate daemon."""
    def __init__(self, module, action):
        self.module, self.action = module, action
        self.done = threading.Event()
        self.errors = []
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def run(self):
        try:
            while not self.done.is_set():
                self.module.fixture(self.action)
                self.done.wait(.001)
        except BaseException as error:
            self.errors.append(repr(error))

    def close(self):
        self.done.set()
        self.thread.join(2)
        assert not self.thread.is_alive()
        assert not self.errors, self.errors


class IPCTests(unittest.TestCase):
    def setUp(self):
        self.runtimes = []

    def runtime(self, daemon=True, port=None):
        global NEXT_RUNTIME
        NEXT_RUNTIME += 1
        folder = RUN / f'r{NEXT_RUNTIME:03d}'
        rt = Runtime(folder, daemon, port)
        self.runtimes.append(rt)
        return rt

    def tearDown(self):
        for rt in self.runtimes:
            rt.close()

    def test_01_start_errors_keep_stage_and_errno(self):
        rt = self.runtime(daemon=False)
        result, source = rt.module.start()
        self.assertIsNone(source)
        self.assertLess(result, -2)
        self.assertEqual(rt.module.shim_status()['stage'], 'channel_open_map')
        self.assertNotEqual(rt.module.shim_status()['errno'], 0)

    def test_02_stale_heartbeat_and_v1_header_rejected(self):
        for fixture in [1, 2]:
            rt = self.runtime(daemon=False)
            rt.module.fixture(0)
            rt.module.fixture(fixture)
            result, source = rt.module.start()
            self.assertIsNone(source)
            self.assertLess(result, -2)
            self.assertEqual(rt.module.shim_status()['stage'],
                             'daemon_heartbeat' if fixture == 1 else 'channel_header')

    def test_03_no_subscription_never_accesses_pixels(self):
        rt = self.runtime()
        s = rt.source()
        self.assertEqual(s.submit(address=1), 1)
        st = s.stats()
        self.assertEqual((st['copy_count'], st['encoded'], st['enqueued']), (0, 0, 0))
        self.assertEqual(rt.module.probe()['in_use'], 0)
        s.stop()
        self.assertEqual(rt.module.shim_status()['state'], 'stopped_acknowledged')
        self.assertEqual(rt.module.probe()['stop_ack'], 1)

    def test_04_pair_strided_lane_copy_meta_and_capture_timestamp(self):
        rt = self.runtime()
        s = rt.source()
        sock = rt.connect(b'PAIR\n')
        raw = b''.join(bytes([row % 256]) * 400 for row in range(1600)) + b'X' * 179200
        expected = b''.join(raw[row * 400:(row + 1) * 400] for row in range(2, 1600, 4))
        buf = (C.c_uint8 * len(raw)).from_buffer_copy(raw)
        m = s.meta(123456)
        begin = time.perf_counter_ns()
        self.assertEqual(s.submit(meta=m, address=C.addressof(buf) + 800, stride=1600), 0)
        end = time.perf_counter_ns()
        C.memset(buf, 0xEE, len(buf))
        h, image = packet(sock)
        jpeg_h, jpeg = packet(sock)
        self.assertEqual((h['flags'], jpeg_h['flags']), (2, 1))
        self.assertEqual(image, expected)
        self.assertEqual(h['raw_crc32'], zlib.crc32(expected))
        for name, _ in Meta._fields_[:8]:
            self.assertEqual(h[name], getattr(m, name), name)
            self.assertEqual(jpeg_h[name], h[name], name)
        p = rt.module.probe()
        self.assertEqual(h['copy_mono_ns'], p['capture_ns'])
        self.assertLessEqual(begin, h['copy_mono_ns'])
        self.assertLessEqual(h['copy_mono_ns'], end)
        self.assertLessEqual(h['copy_mono_ns'], p['dispatch_ns'])
        self.assertLessEqual(p['dispatch_ns'], jpeg_h['encode_begin_ns'])
        self.assertEqual(jpeg_h['copy_mono_ns'], h['copy_mono_ns'])
        decoded = Image.open(io.BytesIO(jpeg))
        self.assertEqual((decoded.size, decoded.mode), ((400, 400), 'L'))
        OBSERVATIONS.append({'pair_header': {n: v for n, v in h.items() if n != 'magic'}, 'probe': p})

    def test_05_jpeg_only_ping_and_clean_stop(self):
        rt = self.runtime()
        s = rt.source()
        ping = rt.connect(b'PING\n')
        received, emitted = struct.unpack('<QQ', exact(ping, 16))
        self.assertLessEqual(received, emitted)
        sock = rt.connect()
        self.assertEqual(s.submit(), 0)
        h, jpeg = packet(sock)
        self.assertEqual(h['flags'], 1)
        self.assertEqual(jpeg[:2], b'\xff\xd8')
        wait(lambda: rt.module.probe()['in_use'] == 0)
        s.stop()
        finish, payload = packet(sock)
        self.assertEqual((finish['flags'], payload), (4, b''))
        wait(lambda: rt.process.poll() is not None)
        self.assertEqual(rt.process.returncode, 0)
        self.assertEqual(s.stats()['queue_in_use'], 0)

    def test_06_duplicate_old_and_wrong_session(self):
        rt = self.runtime()
        s = rt.source()
        sock = rt.connect()
        meta = s.meta(10)
        self.assertEqual(s.submit(meta=meta), 0)
        packet(sock)
        self.assertEqual(s.submit(meta=meta, address=1), 2)
        meta.source_seq = 9
        self.assertEqual(s.submit(meta=meta, address=1), 2)
        meta.session_id += 1
        self.assertEqual(s.submit(meta=meta, address=1), 6)
        self.assertEqual(s.stats()['copy_count'], 1)

    def test_07_two_shared_sources_max_and_current_held_until_stop(self):
        rt = self.runtime()
        s = rt.source()
        rt.connect(b'PAIR\n', receive_buffer=1024)
        image = os.urandom(160000)
        self.assertEqual(s.submit(image), 0)
        wait(lambda: rt.module.probe()['core_queue'] == 1)
        start = time.perf_counter()
        outcomes = [s.submit(image) for _ in range(100)]
        wall_ms = (time.perf_counter() - start) * 1000
        p = rt.module.probe()
        self.assertLessEqual(p['peak'], 2)
        self.assertLessEqual(p['core_queue'], 1)
        self.assertEqual(p['peak'], 2)
        self.assertGreater(outcomes.count(4) + outcomes.count(3), 0)
        self.assertLess(wall_ms, 500)
        s.stop()
        final = rt.module.probe()
        self.assertEqual((final['in_use'], final['stop_ack'], final['core_queue']), (0, 1, 0))
        self.assertEqual(final['producer_enqueued'], final['completed'] + final['cancel_disconnect'] +
                         final['cancel_stop'] + final['dispatch_dropped'])
        OBSERVATIONS.append({'bounded_probe': p, 'final_probe': final, 'submit_100_wall_ms': wall_ms})

    def test_08_disconnect_reconnect_does_not_replay_old_pending(self):
        rt = self.runtime()
        s = rt.source()
        old = rt.connect(b'PAIR\n', receive_buffer=1024)
        image = os.urandom(160000)
        self.assertEqual(s.submit(image), 0)
        wait(lambda: rt.module.probe()['core_queue'] == 1)
        self.assertEqual(s.submit(image), 0)
        old.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('<HH', 1, 0))
        old.close()
        wait(lambda: rt.module.probe()['subscription'] == 0)
        wait(lambda: rt.module.probe()['in_use'] == 0)
        self.assertGreaterEqual(rt.module.probe()['cancel_disconnect'], 1)
        new = rt.connect()
        new.settimeout(.03)
        with self.assertRaises(socket.timeout):
            new.recv(1)
        new.settimeout(3)
        self.assertEqual(s.submit(image), 0)
        h, _ = packet(new)
        self.assertEqual(h['source_seq'], 3)

    def test_09_daemon_death_rejects_source_and_new_channel_new_owner(self):
        rt = self.runtime()
        old = rt.source()
        rt.process.kill()
        rt.process.communicate(timeout=3)
        wait(lambda: old.stats()['running'] == 0, seconds=2)
        self.assertEqual(old.stats()['running'], 0)
        self.assertEqual(rt.module.shim_status()['stage'], 'daemon_heartbeat')
        self.assertEqual(old.submit(address=1), 5)
        old.stop()
        self.assertEqual(rt.module.shim_status()['stage'], 'stop_ack')
        self.assertTrue(rt.module.shim_status()['retained_mapping'])
        result, missing = rt.module.start()
        self.assertIsNone(missing)
        self.assertLess(result, -2)
        (rt.module.root / 'video-channel.bin').unlink()
        rt.launch()
        result, blocked = rt.module.start()
        self.assertIsNone(blocked)
        self.assertLess(result, -2)
        self.assertEqual(rt.module.shim_status()['stage'], 'stop_ack')
        # Recovered service really is a different process, not a second DLL in the failed process.
        recovered = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', str(__file__), '--client-recover',
                                      str(rt.module.root), str(rt.module.library_path)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready = recovered.stdout.readline()
            self.assertTrue(ready.startswith('CLIENT_READY '), ready)
            new_session = int(ready.split()[2])
            self.assertNotEqual(new_session, old.session)
            self.assertEqual(old.submit(address=1), 5)
            sock = rt.connect()
            h, _ = packet(sock)
            self.assertEqual(h['session_id'], new_session)
            output, error = recovered.communicate(timeout=4)
            self.assertEqual(recovered.returncode, 0, output + error)
            self.assertEqual(rt.module.probe()['stop_ack'], 1)
        finally:
            if recovered.poll() is None:
                recovered.kill()
                recovered.communicate(timeout=3)

    def test_10_pid_bound_client_death_stops_daemon(self):
        rt = self.runtime()
        child = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', str(__file__), '--client-worker',
                                  str(rt.module.root), str(rt.module.library_path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = child.stdout.readline()
            self.assertTrue(line.startswith('CLIENT_READY '), line)
            child.kill()
            child.communicate(timeout=3)
            wait(lambda: rt.process.poll() is not None)
            p = rt.module.probe()
            self.assertEqual((p['state'], p['stop_ack']), (4, 1))
            saved = json.loads((rt.module.root / 'ipc-daemon-status.json').read_text())
            self.assertEqual(saved['stage'], 'client_pid_birth')
            self.assertEqual(rt.process.returncode, 1)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()

    def test_11_heartbeat_failure_after_attach_never_reads_pixels(self):
        rt = self.runtime(daemon=False)
        s = rt.fixture_source()
        rt.responder.close()
        rt.module.fixture(1)
        self.assertEqual(s.submit(address=1), 5)
        self.assertEqual(s.stats()['copy_count'], 0)
        self.assertEqual(rt.module.shim_status()['stage'], 'daemon_heartbeat')
        rt.module.fixture(6)  # Fixture has no workers; explicit test ACK allows local release.

    def test_12_busy_producer_cas_drops_without_pixels(self):
        rt = self.runtime(daemon=False)
        s = rt.fixture_source()
        rt.module.fixture(3)
        rt.module.fixture(4)
        self.assertEqual(s.submit(address=1), 3)
        self.assertEqual(s.stats()['copy_count'], 0)
        rt.module.fixture(5)
        rt.module.fixture(6)

    def test_13_existing_channel_never_overwritten_and_bind_errno_is_specific(self):
        rt = self.runtime()
        port = rt.port
        root = rt.module.root / 'exclusive-channel'
        root.mkdir()
        existing = root / 'video-channel.bin'
        existing.write_bytes(b'original-channel')
        result = subprocess.run([str(BUILD / 'ptj_ipc_daemon.exe'), str(root), str(free_port())],
                                capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('stage=channel_create errno=', result.stderr)
        self.assertEqual(existing.read_bytes(), b'original-channel')
        collision = rt.module.root / 'bind-collision'
        collision.mkdir()
        result = subprocess.run([str(BUILD / 'ptj_ipc_daemon.exe'), str(collision), str(port)],
                                capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('stage=core_bind errno=', result.stderr)
        failure = json.loads((collision / 'ipc-daemon-status.json').read_text())
        self.assertEqual(failure['stage'], 'core_bind')
        self.assertNotEqual(failure['errno'], 0)
        OBSERVATIONS.append({'bind_failure': failure, 'create_stderr': result.stderr})

    def test_14_unacknowledged_live_peer_bounded_stop_retains_and_blocks_retry(self):
        rt = self.runtime(daemon=False)
        s = rt.fixture_source(22)
        begin = time.perf_counter()
        s.stop()  # A fixture claims the current live process but does not acknowledge.
        duration = time.perf_counter() - begin
        self.assertLess(duration, 2.5)
        self.assertGreater(duration, 1.8)
        saved = rt.module.shim_status()
        self.assertEqual(saved['stage'], 'stop_ack')
        self.assertTrue(saved['retained_mapping'])
        result, second = rt.module.start()
        self.assertIsNone(second)
        self.assertLess(result, -2)
        self.assertEqual(rt.module.shim_status()['stage'], 'stop_ack')
        OBSERVATIONS.append({'unacked_stop_s': duration, 'unacked_status': saved})

    def test_15_two_slot_stress_has_no_torn_pixels_or_metadata(self):
        rt = self.runtime()
        s = rt.source()
        sock = rt.connect(b'PAIR\n')
        accepted, received, errors = [], [], []
        expected = {}

        def consume():
            try:
                while True:
                    h, raw = packet(sock)
                    if h['flags'] == 4:
                        return
                    jh, jpeg = packet(sock)
                    seq = h['source_seq']
                    self.assertEqual(raw, expected[seq])
                    self.assertEqual(h['raw_crc32'], zlib.crc32(raw))
                    self.assertEqual(jh['raw_crc32'], h['raw_crc32'])
                    self.assertEqual(jh['copy_mono_ns'], h['copy_mono_ns'])
                    self.assertEqual(jh['raw_identity'], h['raw_identity'])
                    self.assertEqual(h['raw_identity'], 0xABC00000 + seq)
                    self.assertEqual(h['eye0_sensor_ns'], 111 + seq)
                    self.assertEqual(h['eye1_sensor_ns'], 222 + seq)
                    self.assertEqual(h['mouth_sensor_ns'], 333 + seq)
                    Image.open(io.BytesIO(jpeg)).load()
                    received.append(seq)
            except BaseException as error:
                errors.append(repr(error))

        thread = threading.Thread(target=consume)
        thread.start()
        for seq in range(1, 65):
            image = bytes([seq % 256]) * 160000
            expected[seq] = image
            buf = (C.c_uint8 * 160000).from_buffer_copy(image)
            if s.submit(meta=s.meta(seq), address=C.addressof(buf)) == 0:
                accepted.append(seq)
            C.memset(buf, 0xFA, 160000)
            time.sleep(.001)
        wait(lambda: rt.module.probe()['in_use'] == 0)
        s.stop()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(received, sorted(set(received)))
        self.assertTrue(set(received) <= set(accepted))
        p = rt.module.probe()
        self.assertEqual(p['producer_enqueued'], p['completed'] + p['cancel_disconnect'] +
                         p['cancel_stop'] + p['dispatch_dropped'])
        self.assertEqual(s.stats()['sent_sources'], len(received))
        self.assertLessEqual(p['peak'], 2)
        OBSERVATIONS.append({'stress_accepted': len(accepted), 'stress_received': len(received), 'probe': p})

    def test_16_stop_overlaps_active_submit_without_early_unmap(self):
        rt = self.runtime()
        s = rt.source()
        rt.connect(b'PAIR\n', receive_buffer=1024)
        done = threading.Event()
        errors = []
        image = bytes([83]) * 160000

        def produce():
            try:
                while not done.is_set():
                    s.submit(image)
            except BaseException as error:
                errors.append(repr(error))

        thread = threading.Thread(target=produce)
        thread.start()
        time.sleep(.03)
        s.stop()
        done.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(s.stats()['queue_in_use'], 0)
        self.assertEqual(rt.module.shim_status()['state'], 'stopped_acknowledged')

    def test_17_second_shim_claim_is_rejected_without_changing_owner(self):
        rt = self.runtime()
        s = rt.source()
        result, other = rt.module.start()
        self.assertIsNone(other)
        self.assertLess(result, -2)
        self.assertEqual(rt.module.shim_status()['stage'], 'client_claim')
        sock = rt.connect()
        self.assertEqual(s.submit(), 0)
        h, _ = packet(sock)
        self.assertEqual(h['session_id'], s.session)

    def test_18_attach_rejects_wrong_client_root_nonce_and_ack_owner(self):
        for action in [11, 12, 13]:
            rt = self.runtime(daemon=False)
            rt.module.fixture(0)
            rt.responder = FixtureResponder(rt.module, action)
            result, source = rt.module.start()
            self.assertIsNone(source)
            self.assertLess(result, -2)
            saved = rt.module.shim_status()
            self.assertEqual(saved['stage'], 'attach_nonce')
            self.assertFalse(saved['retained_mapping'])
            self.assertEqual(saved['cleanup_stage'], 'none')
            self.assertEqual(rt.module.probe()['producer_enqueued'], 0)
            OBSERVATIONS.append({'bad_attach_action': action, 'status': saved})

    def test_19_missing_attach_ack_with_live_heartbeat_cleans_up(self):
        rt = self.runtime(daemon=False)
        rt.module.fixture(0)
        rt.responder = FixtureResponder(rt.module, 14)
        started = time.perf_counter()
        result, source = rt.module.start()
        self.assertIsNone(source)
        self.assertLess(result, -2)
        self.assertGreater(time.perf_counter() - started, 1.8)
        saved = rt.module.shim_status()
        self.assertEqual(saved['stage'], 'attach_ack')
        self.assertFalse(saved['retained_mapping'])
        self.assertEqual(rt.module.probe()['attach_ack'], 0)

    def test_20_missing_attach_and_cleanup_ack_retains_and_blocks(self):
        rt = self.runtime(daemon=False)
        rt.module.fixture(0)
        rt.responder = FixtureResponder(rt.module, 21)
        started = time.perf_counter()
        result, source = rt.module.start()
        elapsed = time.perf_counter() - started
        self.assertIsNone(source)
        self.assertLess(result, -2)
        self.assertLess(elapsed, 4.8)
        saved = rt.module.shim_status()
        self.assertEqual((saved['stage'], saved['cleanup_stage']), ('attach_ack', 'stop_ack'))
        self.assertTrue(saved['retained_mapping'])
        self.assertLess(rt.module.start()[0], -2)
        self.assertEqual(rt.module.shim_status()['stage'], 'stop_ack')
        OBSERVATIONS.append({'attach_cleanup_missing_s': elapsed, 'status': saved})

    def test_21_root_verifies_real_client_birth_owner_and_nonce_before_gate(self):
        for action, stage in [(8, 'client_pid_birth'), (9, 'owner_echo'),
                              (19, 'owner_echo'), (20, 'attach_nonce')]:
            rt = self.runtime()
            rt.connect(wait_subscription=False)
            time.sleep(.02)
            self.assertEqual(rt.module.probe()['subscription'], 0)
            rt.module.fixture(action)
            wait(lambda: rt.process.poll() is not None)
            p = rt.module.probe()
            self.assertEqual((p['attach_ack'], p['subscription'], p['stop_ack']), (0, 0, 1))
            saved = json.loads((rt.module.root / 'ipc-daemon-status.json').read_text())
            self.assertEqual(saved['stage'], stage)
            self.assertEqual(rt.process.returncode, 1)
            OBSERVATIONS.append({'root_request_rejected': action, 'status': saved})

    def test_22_actual_ack_binds_verified_client_and_request_cannot_change(self):
        rt = self.runtime()
        rt.connect(wait_subscription=False)
        self.assertEqual(rt.module.probe()['subscription'], 0)
        s = rt.source()
        p = rt.module.probe()
        self.assertEqual(p['client_pid'], os.getpid())
        self.assertEqual(p['attach_ack'], 1)
        self.assertEqual(p['attach_session'], s.session)
        self.assertEqual((p['nonce0'], p['nonce1']), (p['attach0'], p['attach1']))
        self.assertNotEqual((p['nonce0'], p['nonce1']), (0, 0))
        self.assertEqual((p['root0'], p['root1']), (p['attach_root0'], p['attach_root1']))
        rt.module.fixture(17)  # Atomic request owner changes, not a race on immutable header.
        wait(lambda: rt.process.poll() is not None)
        self.assertEqual(s.submit(address=1), 5)
        s.stop()
        self.assertEqual(rt.module.shim_status()['stage'], 'owner_echo')
        self.assertTrue(rt.module.shim_status()['retained_mapping'])

    def test_23_changed_attach_nonce_after_attach_never_reads_pixels(self):
        rt = self.runtime(daemon=False)
        s = rt.fixture_source()
        rt.module.fixture(3)
        rt.module.fixture(18)
        self.assertEqual(s.submit(address=1), 5)
        self.assertEqual(s.stats()['copy_count'], 0)
        self.assertEqual(rt.module.shim_status()['stage'], 'attach_nonce')

    def test_24_old_stop_ack_nonce_does_not_release_mapping(self):
        rt = self.runtime(daemon=False)
        s = rt.fixture_source()
        rt.responder.close()
        rt.module.fixture(7)
        s.stop()
        saved = rt.module.shim_status()
        self.assertEqual(saved['stage'], 'stop_ack_nonce')
        self.assertTrue(saved['retained_mapping'])
        self.assertLess(rt.module.start()[0], -2)

    def test_25_normal_join_ack_allows_explicit_new_owner_in_same_shim(self):
        rt = self.runtime()
        old = rt.source()
        old.stop()
        rt.process.communicate(timeout=3)
        self.assertEqual(rt.process.returncode, 0)
        p = rt.module.probe()
        self.assertEqual((p['stop_session'], p['stop0'], p['stop1']),
                         (old.session, p['nonce0'], p['nonce1']))
        self.assertFalse(rt.module.shim_status()['retained_mapping'])
        (rt.module.root / 'video-channel.bin').unlink()
        rt.launch()
        fresh = rt.source()
        self.assertNotEqual(fresh.session, old.session)
        self.assertEqual(old.submit(address=1), 5)
        wrong = fresh.meta(1)
        wrong.session_id = old.session
        self.assertEqual(fresh.submit(meta=wrong, address=1), 6)
        sock = rt.connect()
        self.assertEqual(fresh.submit(), 0)
        h, _ = packet(sock)
        self.assertEqual(h['session_id'], fresh.session)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in ('--client-worker', '--client-recover'):
        module = Module(sys.argv[2], pathlib.Path(sys.argv[3]))
        result, source = module.start()
        if result:
            print('CLIENT_FAILED', result, module.shim_status(), flush=True)
            sys.exit(1)
        print('CLIENT_READY', os.getpid(), source.session, flush=True)
        if sys.argv[1] == '--client-recover':
            wait(lambda: source.stats()['connected'] == 1)
            assert source.submit() == 0
            wait(lambda: source.stats()['sent_sources'] == 1)
            source.close()
            sys.exit(0)
        while True:
            source.stats()
            time.sleep(.05)
    parser = argparse.ArgumentParser()
    parser.add_argument('--repeat', type=int, default=1)
    args = parser.parse_args()
    RUN.mkdir(parents=True)
    started = time.perf_counter()
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(IPCTests)
                               for _ in range(args.repeat))
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    report = {'scope': 'Windows actual mapped shim + separate daemon, localhost TCP, no device/GPU work',
              'passed': result.wasSuccessful(), 'tests_run': result.testsRun, 'repeat': args.repeat,
              'channel_version': 2, 'wire_bytes': WIRE.size, 'stats_bytes': C.sizeof(Stats),
              'case_names': [n for n in dir(IPCTests) if n.startswith('test_')],
               'build_artifacts': {name: hashlib.sha256((BUILD / name).read_bytes()).hexdigest()
                                  for name in ['ptj_source.dll', 'ptj_ipc_daemon.exe', 'ipc_test_probe.dll']},
              'elapsed_s': time.perf_counter() - started, 'run': str(RUN),
              'failures': [(str(t), error) for t, error in result.errors + result.failures],
              'observations': OBSERVATIONS}
    text = json.dumps(report, indent=2)
    (ROOT / 'offline-results.json').write_text(text, encoding='utf-8')
    (RUN / 'results.json').write_text(text, encoding='utf-8')
    sys.exit(not result.wasSuccessful())
