"""Actual rebuilt C++ bridge and same-core copy-API integration, localhost only."""
import argparse
import ctypes as C
import io
import json
import os
import pathlib
import re
import socket
import struct
import sys
import threading
import time
import unittest
import zlib
from PIL import Image

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parent
BUILD = pathlib.Path(os.environ.get('PTJ_HOST_BUILD', ROOT.parent/'build-host'))


def split_lanes(raw):
    # Independent byte-wise reference for the camera5 interleaved 400px rows.
    return [b''.join(raw[row*400:(row+1)*400] for row in range(lane, 1600, 4)) for lane in range(4)]
COUNTERS = ('callbacks disarmed stale_epoch gate_busy other_camera null_header null_pixels bad_length '
            'bad_format missing_identity missing_sensor duplicate backwards sensor_duplicate sensor_backwards '
            'identity_gaps identity_missing submitted accepted no_subscriber native_duplicate native_busy '
            'native_full native_stopped native_error last_identity last_sensor_ns last_vendor_ns '
            'last_byte_count first_identity').split()


class Meta(C.Structure):
    _fields_ = [(n, C.c_uint64) for n in ('session_id source_seq sensor_ns vendor_ns eye0_sensor_ns '
                                        'eye1_sensor_ns mouth_sensor_ns raw_identity').split()] + [
        ('flags', C.c_uint32), ('reserved', C.c_uint32)]


class Stats(C.Structure):
    _fields_ = [(n, C.c_uint64) for n in ['armed', 'epoch', 'in_flight', 'identity_fault'] + COUNTERS]


Submit = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(Meta))
DLL = C.CDLL(str(BUILD / 'camera_bridge_test.dll'))
DLL.camtest_create.restype = C.c_void_p
DLL.camtest_destroy.argtypes = [C.c_void_p]
DLL.camtest_arm.argtypes = [C.c_void_p, C.c_void_p, C.c_uint64, Submit]
DLL.camtest_ticket.argtypes = [C.c_void_p]
DLL.camtest_ticket.restype = C.c_uint64
DLL.camtest_frame.argtypes = [C.c_void_p, C.c_uint64, C.c_void_p, C.c_void_p]
DLL.camtest_detach.argtypes = [C.c_void_p]
DLL.camtest_stats.argtypes = [C.c_void_p, C.POINTER(Stats)]
DLL.camtest_guard.argtypes = [C.c_void_p, C.c_uint32]
DLL.camtest_guard.restype = C.c_int
DLL.camtest_command.argtypes = [C.c_char_p, C.c_uint64, C.c_uint64, C.POINTER(C.c_uint64), C.POINTER(C.c_int)]
DLL.camtest_command.restype = C.c_int

NATIVE = C.CDLL(str(BUILD / 'camera_source_test.dll'))


class Config(C.Structure):
    _fields_ = [('struct_bytes', C.c_uint32), ('port', C.c_uint16), ('reserved', C.c_uint16),
                ('send_timeout_ms', C.c_uint32), ('reserved2', C.c_uint32), ('session_id', C.c_uint64)]


NATIVE_COUNTERS = re.findall(r'X\((\w+)\)', (ROOT.parent / 'core/ptj_source.h').read_text())


class NativeStats(C.Structure):
    _fields_ = [(n, C.c_uint64) for n in
                ['struct_bytes', 'session_id', 'running', 'connected', 'queue_in_use'] + NATIVE_COUNTERS]


NATIVE.ptj_start.argtypes = [C.POINTER(Config), C.POINTER(C.c_void_p)]
NATIVE.ptj_start.restype = C.c_int
NATIVE.ptj_session_id.argtypes = [C.c_void_p]
NATIVE.ptj_session_id.restype = C.c_uint64
NATIVE.ptj_get_stats.argtypes = [C.c_void_p, C.POINTER(NativeStats)]
NATIVE.ptj_stop.argtypes = [C.c_void_p]
NATIVE.ptj_destroy.argtypes = [C.c_void_p]

WIRE = struct.Struct('<4sHHII12Q4I')
WIRE_NAMES = ('magic version header_bytes payload_bytes flags session_id source_seq sensor_ns vendor_ns '
              'copy_mono_ns encode_begin_ns encode_end_ns emit_mono_ns eye0_sensor_ns eye1_sensor_ns '
              'mouth_sensor_ns raw_identity width height raw_crc32 quality').split()
OBSERVATIONS = []


def wait(predicate, seconds=2):
    deadline = time.perf_counter() + seconds
    while not predicate():
        if time.perf_counter() >= deadline:
            raise AssertionError('predicate timeout')
        time.sleep(.001)


def exact(sock, n):
    data = bytearray()
    while len(data) < n:
        part = sock.recv(n - len(data))
        if not part:
            raise EOFError('unexpected EOF')
        data += part
    return bytes(data)


def packet(sock):
    h = dict(zip(WIRE_NAMES, WIRE.unpack(exact(sock, 128))))
    return h, exact(sock, h['payload_bytes'])


class Frame:
    def __init__(self, identity=1, sensor=None, vendor=None, length=819200, camera=5, fmt=36,
                 image=None, pixels=None):
        self.image = image if image is not None else bytes((p * 13 + p // 400) & 255 for p in range(819200))
        self.buffer = (C.c_uint8 * len(self.image)).from_buffer_copy(self.image)
        self.listener = C.create_string_buffer(24)
        struct.pack_into('<I', self.listener, 16, camera)
        self.header = C.create_string_buffer(128)
        struct.pack_into('<III', self.header, 8, 819200, 1, fmt)
        self.sensor = 1000000000 + identity * 11111111 if sensor is None else sensor
        self.vendor = 2000000000 + identity * 11111111 if vendor is None else vendor
        struct.pack_into('<QQ', self.header, 24, self.sensor, self.vendor)
        struct.pack_into('<Q', self.header, 48, identity)
        struct.pack_into('<I', self.header, 60, length)
        struct.pack_into('<Q', self.header, 96, C.addressof(self.buffer) if pixels is None else pixels)


class Bridge:
    def __init__(self, callback=None, session=777):
        self.handle = DLL.camtest_create()
        self.records = []
        self.callback = Submit(callback or self.record)
        self.session = session

    def record(self, target, address, stride, meta):
        pixels = b''.join(C.string_at(address + row * stride, 400) for row in range(400))
        m = Meta.from_buffer_copy(bytes(meta.contents))
        self.records.append((target, address, stride, m, pixels))
        return 0

    def arm(self, source=1234, session=None, callback=None):
        if callback:
            self.callback = callback
        if session is not None:
            self.session = session
        DLL.camtest_arm(self.handle, source, self.session, self.callback)

    def frame(self, frame, epoch=None):
        DLL.camtest_frame(self.handle, self.ticket() if epoch is None else epoch,
                          frame.listener, frame.header)

    def ticket(self):
        return DLL.camtest_ticket(self.handle)

    def stats(self):
        stats = Stats()
        DLL.camtest_stats(self.handle, C.byref(stats))
        return {n: getattr(stats, n) for n, _ in stats._fields_}

    def detach(self):
        DLL.camtest_detach(self.handle)

    def close(self):
        if self.handle:
            DLL.camtest_destroy(self.handle)
            self.handle = None


class NativeSource:
    def __init__(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.handle = C.c_void_p()
        config = Config(C.sizeof(Config), self.port, 0, 100, 0, 0)
        assert NATIVE.ptj_start(C.byref(config), C.byref(self.handle)) == 0
        self.session = NATIVE.ptj_session_id(self.handle)
        self.submit = Submit(C.cast(NATIVE.ptj_submit, C.c_void_p).value)

    def stats(self):
        s = NativeStats()
        NATIVE.ptj_get_stats(self.handle, C.byref(s))
        return {n: getattr(s, n) for n, _ in s._fields_}

    def close(self):
        NATIVE.ptj_stop(self.handle)
        OBSERVATIONS.append({'native_integration': self.stats()})
        NATIVE.ptj_destroy(self.handle)


class CameraTests(unittest.TestCase):
    def setUp(self):
        self.bridges = []
        self.natives = []
        self.sock = None

    def bridge(self, **kwargs):
        b = Bridge(**kwargs)
        self.bridges.append(b)
        return b

    def native(self):
        n = NativeSource()
        self.natives.append(n)
        return n

    def tearDown(self):
        for b in self.bridges:
            if b.handle:
                b.detach()
                OBSERVATIONS.append({'camera': b.stats()})
                b.close()
        for n in self.natives:
            n.close()
        if self.sock:
            self.sock.close()

    def test_01_default_disarmed_never_touches_borrowed_frame(self):
        b = self.bridge()
        DLL.camtest_frame(b.handle, b.ticket(), 1, 1)  # Deliberately unreadable addresses.
        self.assertEqual((b.stats()['callbacks'], b.stats()['disarmed'], len(b.records)), (1, 1, 0))

    def test_02_lane2_matches_independent_interleaved_reference(self):
        b = self.bridge()
        b.arm()
        f = Frame(identity=571)
        b.frame(f)
        self.assertEqual(len(b.records), 1)
        target, address, stride, meta, image = b.records[0]
        self.assertEqual((target, address, stride), (1234, C.addressof(f.buffer) + 800, 1600))
        self.assertEqual(image, split_lanes(f.image)[2])
        self.assertEqual(len(image), 160000)
        self.assertNotEqual(image, split_lanes(f.image)[0])

    def test_03_original_identity_times_and_memory_not_mutated(self):
        b = self.bridge()
        b.arm()
        f = Frame(identity=1234567, sensor=99887766, vendor=55443322)
        original = (bytes(f.header), bytes(f.listener), bytes(f.buffer))
        b.frame(f)
        meta = b.records[0][3]
        self.assertEqual((meta.session_id, meta.source_seq, meta.raw_identity), (777, 1234567, 1234567))
        self.assertEqual((meta.sensor_ns, meta.vendor_ns), (99887766, 55443322))
        self.assertEqual((meta.eye0_sensor_ns, meta.eye1_sensor_ns, meta.mouth_sensor_ns), (99887766,) * 3)
        self.assertEqual(meta.flags, 0)
        self.assertEqual((bytes(f.header), bytes(f.listener), bytes(f.buffer)), original)

    def test_04_blob_length_allows_tail_up_to_2mib(self):
        b = self.bridge()
        b.arm()
        for i, length in enumerate([640000, 819200, 2097152], 1):
            b.frame(Frame(identity=i, length=length))
        self.assertEqual(len(b.records), 3)
        for i, length in enumerate([0, 639999, 2097153], 4):
            b.frame(Frame(identity=i, length=length, pixels=1))
        self.assertEqual(b.stats()['bad_length'], 3)
        self.assertEqual(len(b.records), 3)

    def test_05_other_camera_null_and_format_rejected_before_pixels(self):
        b = self.bridge()
        b.arm()
        wrong = Frame(camera=4)
        DLL.camtest_frame(b.handle, b.ticket(), wrong.listener, 1)
        DLL.camtest_frame(b.handle, b.ticket(), wrong.listener, None)
        good = Frame()
        DLL.camtest_frame(b.handle, b.ticket(), good.listener, None)
        b.frame(Frame(pixels=0))
        b.frame(Frame(fmt=32, pixels=1))
        st = b.stats()
        self.assertEqual((st['other_camera'], st['null_header'], st['null_pixels'], st['bad_format']), (2, 1, 1, 1))
        self.assertEqual(st['submitted'], 0)

    def test_06_missing_identity_or_sensor_never_numbered(self):
        b = self.bridge()
        b.arm()
        b.frame(Frame(identity=0))
        b.frame(Frame(identity=50, sensor=0))
        self.assertEqual(len(b.records), 0)
        b.frame(Frame(identity=100))
        self.assertEqual(b.records[0][3].source_seq, 100)
        self.assertEqual((b.stats()['missing_identity'], b.stats()['missing_sensor']), (1, 1))

    def test_07_duplicate_identity_not_refreshed_and_regression_disarms(self):
        b = self.bridge()
        b.arm()
        b.frame(Frame(identity=10))
        b.frame(Frame(identity=10, sensor=9999999999))
        self.assertEqual(len(b.records), 1)
        b.frame(Frame(identity=9))
        self.assertEqual((b.stats()['backwards'], b.stats()['identity_fault'], b.ticket()), (1, 1, 0))
        b.frame(Frame(identity=11))
        self.assertEqual(len(b.records), 1)
        b.detach()
        b.arm(session=888)
        b.frame(Frame(identity=1))
        self.assertEqual(b.records[-1][3].session_id, 888)
        self.assertEqual(b.stats()['identity_fault'], 0)

    def test_08_sensor_duplicates_and_regression_not_fabricated(self):
        b = self.bridge()
        b.arm()
        b.frame(Frame(identity=1, sensor=1000))
        b.frame(Frame(identity=2, sensor=1000))
        self.assertEqual(len(b.records), 1)
        b.frame(Frame(identity=3, sensor=2000))
        self.assertEqual(b.stats()['identity_missing'], 1)
        b.frame(Frame(identity=4, sensor=1900))
        self.assertEqual((b.stats()['sensor_duplicate'], b.stats()['sensor_backwards'], b.ticket()), (1, 1, 0))

    def test_09_old_callback_ticket_cannot_enter_restarted_owner(self):
        b = self.bridge()
        b.arm()
        old = b.ticket()
        b.detach()
        b.arm(session=888)
        b.frame(Frame(identity=1), epoch=old)
        self.assertEqual(len(b.records), 0)
        b.frame(Frame(identity=1))
        self.assertEqual(b.records[0][3].session_id, 888)
        self.assertEqual(b.stats()['stale_epoch'], 1)

    def test_10_detach_waits_for_inflight_before_handle_can_be_destroyed(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def slow_submit(target, address, stride, meta):
            entered.set()
            release.wait(2)
            return 0
        b = self.bridge(callback=slow_submit)
        b.arm()
        f = Frame()
        producer = threading.Thread(target=b.frame, args=(f,))
        producer.start()
        self.assertTrue(entered.wait(1))
        old = b.ticket()
        def stop():
            b.detach()
            finished.set()
        controller = threading.Thread(target=stop)
        controller.start()
        try:
            wait(lambda: b.ticket() == 0)
            self.assertFalse(finished.is_set())
            self.assertEqual(b.stats()['in_flight'], 1)
            b.frame(Frame(identity=2), epoch=old)
        finally:
            release.set()
            producer.join(2)
            controller.join(2)
        self.assertTrue(finished.is_set())
        self.assertFalse(producer.is_alive() or controller.is_alive())
        self.assertEqual((b.stats()['in_flight'], b.stats()['submitted']), (0, 1))

    def test_11_real_native_no_subscriber_never_reads_pixel_pointer(self):
        n = self.native()
        b = self.bridge()
        b.arm(n.handle, n.session, n.submit)
        b.frame(Frame(pixels=1))  # Native returns NO_SUBSCRIBER without touching this address.
        self.assertEqual((b.stats()['no_subscriber'], n.stats()['copy_count'], n.stats()['encoded']), (1, 0, 0))

    def test_12_real_native_pair_stride_and_copy_lifetime(self):
        n = self.native()
        self.sock = socket.create_connection(('127.0.0.1', n.port), timeout=2)
        self.sock.sendall(b'PAIR\n')
        wait(lambda: n.stats()['connected'] == 1)
        b = self.bridge()
        b.arm(n.handle, n.session, n.submit)
        f = Frame(identity=567890)
        expected = split_lanes(f.image)[2]
        b.frame(f)
        C.memset(f.buffer, 0xEA, len(f.buffer))
        h, raw = packet(self.sock)
        jh, jpeg = packet(self.sock)
        self.assertEqual((h['flags'], jh['flags']), (2, 1))
        self.assertEqual(raw, expected)
        self.assertEqual((h['session_id'], h['source_seq'], h['raw_identity']), (n.session, 567890, 567890))
        self.assertEqual(h['raw_crc32'], zlib.crc32(expected))
        for key in ['session_id', 'source_seq', 'sensor_ns', 'vendor_ns', 'eye0_sensor_ns',
                    'eye1_sensor_ns', 'mouth_sensor_ns', 'raw_identity', 'raw_crc32']:
            self.assertEqual(h[key], jh[key])
        decoded = Image.open(io.BytesIO(jpeg))
        self.assertEqual((decoded.size, decoded.mode), ((400, 400), 'L'))

    def test_13_native_admission_result_accounting(self):
        answer = [0]
        b = self.bridge(callback=lambda *unused: answer[0])
        b.arm()
        for result in range(8):
            answer[0] = result
            b.frame(Frame(identity=1 + result))
        st = b.stats()
        self.assertEqual(st['submitted'], 8)
        self.assertEqual([st[n] for n in ['accepted', 'no_subscriber', 'native_duplicate', 'native_busy',
                                        'native_full', 'native_stopped', 'native_error']], [1, 1, 1, 1, 1, 1, 2])

    def test_14_guards_enforce_pinned_full_window(self):
        expected = bytes.fromhex('ff4303d1f94300f9f85f09a9f6570aa9f44f0ba9fd7b0ca9fd03039157d03bd5')
        guard = (ROOT.parent / 'camera/guard_spec.h').read_text()
        self.assertIn('0xC51A2999AF634D51ULL', guard)
        self.assertIn('0x36c1c', guard)
        self.assertIn('/system/lib64/libpxreyetrackingservice.so', guard)
        actual = bytes(int(x, 16) for x in re.findall(r'0x([0-9a-f]{2})(?=[,\n])',
                       re.search(r'BYTES\[32\]\s*=\s*\{(.*?)\}', guard, re.S).group(1)))
        self.assertEqual(actual, expected)
        window = C.create_string_buffer(expected, 32)
        self.assertEqual(DLL.camtest_guard(window, 0), 1)
        for index in range(32):
            altered = bytearray(expected)
            altered[index] ^= 1
            self.assertEqual(DLL.camtest_guard(C.create_string_buffer(bytes(altered), 32), 0), 0)
        patched = bytearray(expected)
        struct.pack_into('<I', patched, 0, 0x14000100)
        self.assertEqual(DLL.camtest_guard(C.create_string_buffer(bytes(patched), 32), 1), 1)
        for index in range(4, 32):
            altered = bytearray(patched)
            altered[index] ^= 1
            self.assertEqual(DLL.camtest_guard(C.create_string_buffer(bytes(altered), 32), 1), 0)

    def test_15_control_binds_owner_and_monotonic_command_id(self):
        command_id = C.c_uint64(0)
        operation = C.c_int(0)
        def parse(line):
            return DLL.camtest_command(line, 0xABC, 3, C.byref(command_id), C.byref(operation))
        self.assertEqual(parse(b'0000000000000abc 4 start\n'), 0)
        self.assertEqual((command_id.value, operation.value), (4, 1))
        self.assertEqual(parse(b'abc 5 stop\n'), 0)
        self.assertEqual((command_id.value, operation.value), (5, 2))
        self.assertEqual(parse(b'abc 3 start\n'), 1)
        self.assertEqual(parse(b'abc 2 stop\n'), 1)
        self.assertEqual(parse(b'abd 99 start\n'), -3)
        self.assertEqual(parse(b'abc 9 start extra\n'), -2)
        self.assertEqual(parse(b'abc 9 unknown\n'), -2)
        self.assertEqual(command_id.value, 5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repeat', type=int, default=1)
    args = parser.parse_args()
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(CameraTests)
                               for _ in range(args.repeat))
    started = time.perf_counter()
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    report = {'scope': 'Windows rebuilt C++ callback bridge, independent lane reference, same-core DLL loopback integration',
              'device_access': False, 'tests_run': result.testsRun, 'repeat': args.repeat,
              'passed': result.wasSuccessful(), 'elapsed_s': time.perf_counter() - started,
              'failures': [(str(t), error) for t, error in result.errors + result.failures],
              'observations': OBSERVATIONS}
    (ROOT / 'offline-results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    sys.exit(not result.wasSuccessful())
