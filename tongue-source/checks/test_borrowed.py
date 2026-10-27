"""Borrow lifetime proof with the same core, plus real cross-process daemon/shim regression."""
import argparse
import ctypes as C
import hashlib
import io
import json
import os
import pathlib
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
import zlib
from PIL import Image
import test_ipc as T

ROOT = pathlib.Path(__file__).resolve().parent
OBSERVATIONS = []
BORROW_FIELDS = ('admissions completions active last_admitted_address last_completed_address '
                 'last_admitted_seq last_completed_seq last_subscription').split()


class BorrowStats(C.Structure):
    _fields_ = [(n, C.c_uint64) for n in BORROW_FIELDS]


LIB = C.CDLL(str(T.BUILD / 'ipc_core_test.dll'))
HOOK = C.CFUNCTYPE(None, C.c_uint32, C.c_void_p, C.c_void_p)
LIB.core_test_set_hook.argtypes = [HOOK, C.c_void_p]
LIB.core_ptj_start.argtypes = [C.POINTER(T.Config), C.POINTER(C.c_void_p)]
LIB.core_ptj_submit_borrowed.argtypes = [C.c_void_p, C.c_void_p, C.POINTER(T.Meta), C.c_uint64, C.c_uint64]
LIB.core_ptj_submit.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(T.Meta)]
LIB.core_ptj_get_stats.argtypes = [C.c_void_p, C.POINTER(T.Stats)]
LIB.core_ptj_get_borrow_stats.argtypes = [C.c_void_p, C.POINTER(BorrowStats)]
LIB.core_ptj_stop.argtypes = [C.c_void_p]
LIB.core_ptj_destroy.argtypes = [C.c_void_p]
KERNEL = C.WinDLL('kernel32', use_last_error=True)
KERNEL.VirtualAlloc.argtypes = [C.c_void_p, C.c_size_t, C.c_uint32, C.c_uint32]
KERNEL.VirtualAlloc.restype = C.c_void_p
KERNEL.VirtualProtect.argtypes = [C.c_void_p, C.c_size_t, C.c_uint32, C.POINTER(C.c_uint32)]
KERNEL.VirtualFree.argtypes = [C.c_void_p, C.c_size_t, C.c_uint32]


class Pages:
    def __init__(self, data, readonly=True):
        self.address = KERNEL.VirtualAlloc(None, len(data), 0x3000, 0x04)
        assert self.address, C.get_last_error()
        C.memmove(self.address, data, len(data))
        if readonly:
            previous = C.c_uint32()
            assert KERNEL.VirtualProtect(self.address, len(data), 0x02, C.byref(previous)), C.get_last_error()

    def close(self):
        assert KERNEL.VirtualFree(self.address, 0, 0x8000), C.get_last_error()


def fields(value):
    return {n: getattr(value, n) for n, _ in value._fields_}


class Core:
    def __init__(self, case, blocked_stage=0):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.events, self.errors, self.pages, self.sockets = [], [], [], []
        self.blocked_stage = blocked_stage
        self.handle = C.c_void_p()
        self.session = time.perf_counter_ns()

        def checkpoint(stage, address, _context):
            self.events.append({'stage': stage, 'address': address})
            if stage == self.blocked_stage:
                self.entered.set()
                if not self.release.wait(5):
                    self.errors.append('test barrier release timeout')

        self.callback = HOOK(checkpoint)
        LIB.core_test_set_hook(self.callback, None)
        self.port = T.free_port()
        config = T.Config(C.sizeof(T.Config), self.port, 0, 100, 0, self.session)
        assert LIB.core_ptj_start(C.byref(config), C.byref(self.handle)) == 0
        case.addCleanup(self.close)

    def pages_for(self, data, readonly=True):
        pages = Pages(data, readonly)
        self.pages.append(pages)
        return pages

    def stats(self):
        value = T.Stats()
        assert LIB.core_ptj_get_stats(self.handle, C.byref(value)) == 0
        return fields(value)

    def borrow_stats(self):
        value = BorrowStats()
        assert LIB.core_ptj_get_borrow_stats(self.handle, C.byref(value)) == 0
        return fields(value)

    def connect(self, pair=True, receive_buffer=None):
        sock = socket.socket()
        if receive_buffer:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
        sock.settimeout(3)
        sock.connect(('127.0.0.1', self.port))
        sock.sendall(b'PAIR\n' if pair else b'JPEG\n')
        self.sockets.append(sock)
        T.wait(lambda: self.stats()['connected'] == 1)
        return sock

    def meta(self, seq):
        return T.Meta(self.session, seq, 111 + seq, 222 + seq, 333 + seq, 444 + seq, 555 + seq,
                      0xABC000 + seq, 0, 0)

    def borrow(self, pages, seq, subscription=None):
        meta = self.meta(seq)
        generation = self.stats()['connections'] if subscription is None else subscription
        return LIB.core_ptj_submit_borrowed(self.handle, pages.address, C.byref(meta), 123456789, generation)

    def stop(self):
        LIB.core_ptj_stop(self.handle)

    def close(self):
        self.release.set()
        self.stop()
        OBSERVATIONS.append({'core_stats': self.stats(), 'borrow': self.borrow_stats(), 'read_pointers': self.events})
        LIB.core_ptj_destroy(self.handle)
        LIB.core_test_set_hook(HOOK(), None)
        for sock in self.sockets:
            sock.close()
        # VirtualFree must follow join, including when an assertion failed while the worker was blocked.
        for pages in self.pages:
            pages.close()
        assert not self.errors, self.errors


class CoreBorrowTests(unittest.TestCase):
    def test_01_actual_crc_jpeg_raw_pointer_identity_on_readonly_pages(self):
        core = Core(self, blocked_stage=1)
        sock = core.connect()
        raw = bytes((i * 13 + i // 400) % 256 for i in range(160000))
        pages = core.pages_for(raw)
        self.assertEqual(core.borrow(pages, 41), 0)
        self.assertTrue(core.entered.wait(2))
        during = core.borrow_stats()
        self.assertEqual((during['admissions'], during['completions'], during['active']), (1, 0, 1))
        self.assertEqual((during['last_admitted_address'], during['last_admitted_seq'], during['last_subscription']),
                         (pages.address, 41, 1))
        self.assertEqual(core.stats()['queue_in_use'], 1)
        core.release.set()
        header, received = T.packet(sock)
        jpeg_header, jpeg = T.packet(sock)
        self.assertEqual(received, raw)
        self.assertEqual(header['raw_crc32'], zlib.crc32(raw))
        self.assertEqual((header['source_seq'], header['raw_identity'], header['copy_mono_ns']),
                         (41, core.meta(41).raw_identity, 123456789))
        self.assertEqual(jpeg_header['raw_crc32'], header['raw_crc32'])
        self.assertEqual(Image.open(io.BytesIO(jpeg)).size, (400, 400))
        T.wait(lambda: core.borrow_stats()['active'] == 0)
        self.assertTrue({1, 2, 3, 4, 5} <= {e['stage'] for e in core.events})
        self.assertEqual({e['address'] for e in core.events}, {pages.address})
        self.assertEqual((core.stats()['copy_count'], core.stats()['copy_total_ns']), (0, 0))

    def test_02_stop_joins_an_unfinished_borrow_before_caller_can_release(self):
        core = Core(self, blocked_stage=3)
        core.connect()
        pages = core.pages_for(bytes([87]) * 160000)
        self.assertEqual(core.borrow(pages, 1), 0)
        self.assertTrue(core.entered.wait(2))
        stopper = threading.Thread(target=core.stop)
        stopper.start()
        try:
            T.wait(lambda: core.stats()['running'] == 0)
            self.assertTrue(stopper.is_alive())
            self.assertEqual((core.borrow_stats()['active'], core.borrow_stats()['completions']), (1, 0))
            self.assertEqual(C.string_at(pages.address, 16), bytes([87]) * 16)
        finally:
            core.release.set()
            stopper.join(2)
        self.assertFalse(stopper.is_alive())
        self.assertEqual((core.borrow_stats()['active'], core.borrow_stats()['completions']), (0, 1))
        self.assertEqual(core.stats()['queue_in_use'], 0)

    def test_03_pending_disconnect_and_new_subscription_keep_leases_separate(self):
        core = Core(self, blocked_stage=1)
        old = core.connect()
        first = core.pages_for(bytes([11]) * 160000)
        pending = core.pages_for(bytes([22]) * 160000)
        fresh = core.pages_for(bytes([33]) * 160000)
        self.assertEqual(core.borrow(first, 1), 0)
        self.assertTrue(core.entered.wait(2))
        self.assertEqual(core.borrow(pending, 2), 0)
        self.assertEqual(core.borrow_stats()['active'], 2)
        old.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('<HH', 1, 0))
        old.close()
        T.wait(lambda: core.stats()['connected'] == 0)
        self.assertEqual((core.borrow_stats()['completions'], core.borrow_stats()['active']), (1, 1))
        new = core.connect()
        self.assertEqual(core.borrow(fresh, 3, subscription=1), 1)
        self.assertEqual(core.borrow(fresh, 4, subscription=2), 0)
        core.release.set()
        header, raw = T.packet(new)
        T.packet(new)
        self.assertEqual((header['source_seq'], raw), (4, bytes([33]) * 160000))
        T.wait(lambda: core.borrow_stats()['active'] == 0)
        self.assertEqual((core.borrow_stats()['admissions'], core.borrow_stats()['completions']), (3, 3))
        self.assertNotIn(pending.address, [e['address'] for e in core.events])

    def test_04_send_timeout_completes_borrow_after_last_reader(self):
        core = Core(self)
        core.connect(receive_buffer=1024)
        pages = core.pages_for(os.urandom(160000))
        self.assertEqual(core.borrow(pages, 1), 0)
        T.wait(lambda: core.stats()['send_timeouts'] != 0)
        T.wait(lambda: core.borrow_stats()['active'] == 0)
        self.assertEqual(core.borrow_stats()['last_completed_address'], pages.address)
        self.assertEqual(core.borrow_stats()['completions'], 1)
        self.assertEqual(core.stats()['copy_count'], 0)

    def test_05_legacy_copy_api_is_a_distinct_control_path(self):
        core = Core(self, blocked_stage=1)
        sock = core.connect()
        raw = bytes([91]) * 160000
        pages = core.pages_for(raw, readonly=False)
        meta = core.meta(1)
        self.assertEqual(LIB.core_ptj_submit(core.handle, pages.address, 400, C.byref(meta)), 0)
        self.assertTrue(core.entered.wait(2))
        # The copy API released caller bytes on return; no reader uses this original allocation.
        C.memset(pages.address, 0xEE, 160000)
        core.release.set()
        _header, actual = T.packet(sock)
        _jpeg_header, copy_jpeg = T.packet(sock)
        self.assertEqual(actual, raw)
        self.assertNotIn(pages.address, [e['address'] for e in core.events])
        self.assertEqual(core.borrow_stats()['admissions'], 0)
        self.assertEqual(core.stats()['copy_count'], 1)
        T.wait(lambda: core.stats()['queue_in_use'] == 0)
        readonly = core.pages_for(raw)
        self.assertEqual(core.borrow(readonly, 2), 0)
        _header, borrowed_raw = T.packet(sock)
        _header, borrowed_jpeg = T.packet(sock)
        self.assertEqual(borrowed_raw, raw)
        self.assertEqual(borrowed_jpeg, copy_jpeg)
        T.wait(lambda: core.borrow_stats()['active'] == 0)
        self.assertEqual(core.borrow_stats()['completions'], 1)
        self.assertEqual(core.stats()['copy_count'], 1)  # Only the explicit compatibility submission copied.


class BridgeBorrowTests(unittest.TestCase):
    setUp = T.IPCTests.setUp
    runtime = T.IPCTests.runtime
    tearDown = T.IPCTests.tearDown

    def final(self, rt, source=None):
        if source:
            source.stop()
        rt.process.wait(timeout=4)
        saved = json.loads((rt.module.root / 'ipc-daemon-status.json').read_text())
        core, borrow = saved['core'], saved['borrowed']
        self.assertEqual([core[n] for n in ['copy_count', 'copy_total_ns', 'copy_max_ns', 'copy_last_ns']], [0] * 4)
        self.assertEqual((borrow['active'], core['queue_in_use'], saved['in_use']), (0, 0, 0))
        self.assertEqual(borrow['admissions'], borrow['completions'])
        self.assertEqual(borrow['admissions'], saved['dispatched'])
        if borrow['admissions']:
            self.assertEqual(borrow['last_admitted_address'], borrow['last_slot_address'])
            self.assertEqual(borrow['last_completed_address'], borrow['last_slot_address'])
            self.assertIn(borrow['last_slot_offset'], [1216 + 96, 1216 + 160128 + 96])
        OBSERVATIONS.append({'bridge_final': saved})
        return saved

    def test_06_two_ipc_slots_cannot_reuse_active_borrow_and_stop_drains(self):
        rt = self.runtime()
        source = rt.source()
        rt.connect(b'PAIR\n', receive_buffer=1024)
        image = os.urandom(160000)
        self.assertEqual(source.submit(image), 0)
        T.wait(lambda: rt.module.probe()['core_queue'] == 1)
        self.assertEqual(source.submit(image), 0)
        self.assertEqual(source.submit(address=1), 4)
        p = rt.module.probe()
        self.assertEqual(p['in_use'], 2)
        self.assertEqual(sorted([p['slot0'], p['slot1']]), [2, 3])
        self.assertEqual(p['core_queue'], 1)
        self.assertEqual(source.stats()['copy_count'], 2)  # First protection copies stay.
        saved = self.final(rt, source)
        self.assertEqual(saved['peak'], 2)
        self.assertEqual(saved['stop_ack'], 1)

    def test_07_real_daemon_pair_reads_the_shared_slot_address(self):
        rt = self.runtime()
        source = rt.source()
        sock = rt.connect(b'PAIR\n')
        raw = os.urandom(160000)
        meta = source.meta(123)
        self.assertEqual(source.submit(raw, meta=meta), 0)
        header, actual = T.packet(sock)
        jpeg_header, _jpeg = T.packet(sock)
        self.assertEqual(actual, raw)
        self.assertEqual(header['raw_crc32'], zlib.crc32(raw))
        self.assertEqual(header['copy_mono_ns'], jpeg_header['copy_mono_ns'])
        self.assertEqual(header['raw_identity'], meta.raw_identity)
        saved = self.final(rt, source)
        self.assertEqual(saved['producer']['copy_count'], 1)
        self.assertEqual(saved['borrowed']['last_admitted_seq'], 123)

    def test_08_daemon_death_with_unfinished_borrow_keeps_mapping_and_blocks_attach(self):
        rt = self.runtime()
        source = rt.source()
        rt.connect(b'PAIR\n', receive_buffer=1024)
        self.assertEqual(source.submit(os.urandom(160000)), 0)
        T.wait(lambda: rt.module.probe()['core_queue'] == 1)
        rt.process.kill()
        rt.process.communicate(timeout=3)
        T.wait(lambda: source.stats()['running'] == 0)
        self.assertEqual(source.submit(address=1), 5)
        source.stop()
        self.assertTrue(rt.module.shim_status()['retained_mapping'])
        self.assertEqual(rt.module.shim_status()['stage'], 'stop_ack')
        self.assertEqual(rt.module.probe()['stop_ack'], 0)
        self.assertLess(rt.module.start()[0], -2)

    def test_09_client_death_while_borrowed_joins_before_ack(self):
        rt = self.runtime()
        child = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', str(__file__), '--client-borrow-worker',
                                  str(rt.module.root), str(rt.module.library_path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = child.stdout.readline()
            self.assertTrue(line.startswith('CLIENT_READY '), line)
            rt.connect(b'PAIR\n', receive_buffer=1024)
            self.assertEqual(child.stdout.readline().strip(), 'CLIENT_BORROW_PENDING')
            T.wait(lambda: rt.module.probe()['core_queue'] == 1)
            child.kill()
            child.communicate(timeout=3)
            saved = self.final(rt)
            self.assertEqual((saved['stage'], saved['stop_ack']), ('client_pid_birth', 1))
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=3)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--client-borrow-worker':
        module = T.Module(sys.argv[2], pathlib.Path(sys.argv[3]))
        rc, source = module.start()
        assert rc == 0, (rc, module.shim_status())
        print('CLIENT_READY', os.getpid(), source.session, flush=True)
        T.wait(lambda: source.stats()['connected'] == 1)
        assert source.submit(os.urandom(160000)) == 0
        print('CLIENT_BORROW_PENDING', flush=True)
        while True:
            source.stats()
            time.sleep(.01)
    parser = argparse.ArgumentParser()
    parser.add_argument('--repeat', type=int, default=1)
    args = parser.parse_args()
    T.RUN.mkdir(parents=True)
    started = time.perf_counter()
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                               for _ in range(args.repeat) for cls in [CoreBorrowTests, BridgeBorrowTests])
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    report = {'passed': result.wasSuccessful(), 'tests_run': result.testsRun, 'repeat': args.repeat,
              'scope': 'Same-core test DLL pointer/barrier/readonly proof and real cross-process production daemon',
              'run': str(T.RUN), 'elapsed_s': time.perf_counter() - started,
              'failures': [(str(t), error) for t, error in result.errors + result.failures],
               'build_artifacts': {name: hashlib.sha256((T.BUILD / name).read_bytes()).hexdigest()
                                  for name in ['ipc_core_test.dll', 'ptj_source.dll', 'ptj_ipc_daemon.exe']},
              'observations': OBSERVATIONS + T.OBSERVATIONS}
    text = json.dumps(report, indent=2)
    (ROOT / 'borrowed-results.json').write_text(text, encoding='utf-8')
    (T.RUN / 'borrowed-results.json').write_text(text, encoding='utf-8')
    sys.exit(not result.wasSuccessful())
