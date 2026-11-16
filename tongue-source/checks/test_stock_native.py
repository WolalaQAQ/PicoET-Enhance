"""Host-only mouth stages, exact provenance guard, mapped shim and LABELS TCP."""
import ctypes as C
import json
import math
import pathlib
import struct
import subprocess
import sys
import unittest

import test_ipc as ipc
import test_camera as camera

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'pc-tongue'))
from stock import (StockRecord, STOCK_BYTES, read_stock, CaptureContext,
                    CaptureContextUnavailable, read_capture_context)
from discovery import DISCOVER, discover_sources

Record = C.c_ubyte * STOCK_BYTES
LIB = C.CDLL(str(ipc.BUILD / 'stock_test.dll'))
LIB.stock_associate.argtypes = [C.POINTER(ipc.Meta), C.c_size_t, C.POINTER(Record), C.c_int]
LIB.stock_mouth.argtypes = [C.POINTER(C.c_float), C.c_size_t, C.POINTER(Record), C.POINTER(C.c_float)]
LIB.stock_capability.argtypes = [C.c_int, C.c_int, C.c_int, C.c_int, C.c_int]
LIB.stock_capability.restype = C.c_char_p
StockSubmit = C.CFUNCTYPE(C.c_int, C.c_void_p, C.POINTER(Record))
camera.DLL.camtest_arm_stock.argtypes = [C.c_void_p, C.c_void_p, C.c_uint64, C.c_uint64,
                                        camera.Submit, StockSubmit, C.c_int]
camera.DLL.camtest_stock.argtypes = [C.c_void_p, C.POINTER(Record)]
camera.DLL.camtest_pipeline_stock.argtypes = [C.c_void_p, C.c_void_p, C.POINTER(Record)]


def stock(seq=1, session=1):
    return StockRecord(session, 99, 0, 0, [-.125] * 33, [0.] * 33, [.25] * 33,
                       [.25, .75], True, stock_input_ns=1000, label_seq=seq)


class StockNativeTests(ipc.IPCTests):
    # Only this subclass's named tests are selected by the runner below.
    def submit(self, source, record):
        library = source.module.lib
        library.ptj_submit_stock.argtypes = [C.c_void_p, C.POINTER(Record)]
        library.ptj_submit_stock.restype = C.c_int
        data = Record.from_buffer_copy(record.pack())
        return library.ptj_submit_stock(source.handle, C.byref(data))

    def labels(self, rt):
        sock = rt.connect(b'LABELS\n', wait_subscription=False)
        ipc.wait(lambda: rt.module.probe()['stock_subscription'] != 0)
        return sock

    def context(self, rt, attached=True):
        def query():
            sock = rt.connect(b'', wait_subscription=False)
            return read_capture_context(sock)
        if not attached:
            return query()
        # Attach ACK precedes publication to the core. Wait on the actual one-shot
        # protocol, not an actively replaced Windows JSON status file.
        values = []
        def ready():
            try:
                values.append(query())
            except CaptureContextUnavailable:
                return False
            return True
        ipc.wait(ready)
        return values[0]

    def production_bridge(self, rt, source, audited=True):
        bridge = camera.Bridge(session=source.session)
        self.addCleanup(bridge.close)
        bridge.callback = camera.Submit(C.cast(source.module.lib.ptj_submit, C.c_void_p).value)
        bridge.stock_callback = StockSubmit(C.cast(source.module.lib.ptj_submit_stock, C.c_void_p).value)
        bridge.generation = self.context(rt).service_generation
        camera.DLL.camtest_arm_stock(bridge.handle, source.handle, source.session, bridge.generation,
                                     bridge.callback, bridge.stock_callback, int(audited))
        return bridge

    def emit_pipeline_stock(self, bridge, labels, frame, *, sensor=None, vendor=None, identity=None,
                            capture_session=0, generation=0, captured=True):
        original_identity = struct.unpack_from('<Q', frame.header, 48)[0]
        sensor = frame.sensor if sensor is None else sensor
        vendor = frame.vendor if vendor is None else vendor
        identity = original_identity if identity is None else identity
        execution = C.create_string_buffer(0x188)
        # Deliberately different eye times: only the third image supplies the mouth label.
        struct.pack_into('<Q', execution, 8, sensor - 100)
        struct.pack_into('<Q', execution, 0x88, sensor - 50)
        struct.pack_into('<QQQ', execution, 0x100, vendor, sensor, identity)
        value = stock(session=capture_session)
        value.service_generation = generation
        value.stock_input_ns = sensor
        data = Record.from_buffer_copy(value.pack())
        if captured:
            camera.DLL.camtest_pipeline_stock(bridge.handle, execution, C.byref(data))
        else:
            camera.DLL.camtest_stock(bridge.handle, C.byref(data))
        return read_stock(labels)

    def test_stock_transport_preserves_values_and_original_input(self):
        rt = self.runtime(); source = rt.source(); sock = self.labels(rt)
        record = stock(session=source.session)
        self.assertEqual(self.submit(source, record), 0)
        decoded = read_stock(sock)
        self.assertEqual(decoded.raw33, record.raw33)
        self.assertEqual(decoded.instant33, record.instant33)
        self.assertEqual(decoded.stock_input_ns, 1000)
        self.assertEqual(decoded.service_generation, rt.module.probe()['client_birth'])
        self.assertIsNone(decoded.source_key)

    def test_stock_and_jpeg_are_independent(self):
        rt = self.runtime(); source = rt.source(); labels = self.labels(rt); images = rt.connect()
        self.assertEqual(source.submit(), 0)
        self.assertEqual(self.submit(source, stock(session=source.session)), 0)
        h, image = ipc.packet(images)
        self.assertEqual(h['source_seq'], 1)
        self.assertEqual((h['width'], h['height'], h['quality']), (400, 400, 95))
        self.assertTrue(image.startswith(b'\xff\xd8'))
        self.assertEqual(read_stock(labels).capture_session_id, h['session_id'])
        ipc.wait(lambda: source.stats()['queue_in_use'] == 0)
        self.assertEqual(rt.module.probe()['in_use'], 0)

    def test_stock_no_subscriber_wrong_session_and_stop(self):
        rt = self.runtime(); source = rt.source()
        self.assertEqual(self.submit(source, stock(session=source.session)), 1)
        self.labels(rt)
        self.assertEqual(self.submit(source, stock(session=source.session ^ 1)), 6)
        source.stop()
        self.assertEqual(self.submit(source, stock(session=source.session)), 5)

    def test_stock_session_replacement_does_not_accept_old_record(self):
        old_rt = self.runtime(); old = old_rt.source(); self.labels(old_rt)
        old_session = old.session; old.stop()
        fresh_rt = self.runtime(); fresh = fresh_rt.source(); labels = self.labels(fresh_rt)
        self.assertNotEqual(old_session, fresh.session)
        self.assertEqual(self.submit(fresh, stock(session=old_session)), 6)
        self.assertEqual(self.submit(fresh, stock(session=fresh.session)), 0)
        self.assertEqual(read_stock(labels).capture_session_id, fresh.session)

    def test_stock_ring_full_drops_label_without_touching_image_slots(self):
        rt = self.runtime(daemon=False); source = rt.fixture_source()
        rt.module.fixture(23)
        self.assertEqual(self.submit(source, stock(session=source.session)), 4)
        before = rt.module.probe()
        self.assertEqual((before['slot0'], before['slot1'], before['in_use']), (0, 0, 0))
        rt.module.fixture(24)
        self.assertEqual(self.submit(source, stock(session=source.session)), 0)

    def test_stock_live_production_camera_pipeline_ipc_and_jpeg_exact_match(self):
        rt = self.runtime(); source = rt.source(); labels = self.labels(rt); images = rt.connect()
        bridge = self.production_bridge(rt, source)
        frame = camera.Frame(identity=17, image=bytes(819200))
        bridge.frame(frame)
        record = self.emit_pipeline_stock(bridge, labels, frame)
        header, _ = ipc.packet(images)
        self.assertTrue(record.association_valid)
        self.assertEqual(record.source_key, (source.session, bridge.generation, 17, frame.sensor))
        self.assertEqual(record.image_key, (header['session_id'], header['source_seq'], header['mouth_sensor_ns']))
        self.assertEqual((record.vendor_ns, record.raw_identity), (header['vendor_ns'], header['raw_identity']))
        self.assertEqual(record.stock_input_ns, frame.sensor)
        self.assertEqual(record.raw33, [-.125] * 33)
        self.assertEqual(record.instant33[32], 0)  # Association never creates tongue negatives.
        self.assertEqual(bytes(record.pack())[-8:], bytes(8))  # No internal input marker on wire.

    def test_stock_live_wrong_vendor_identity_and_near_sensor_never_match(self):
        rt = self.runtime(); source = rt.source(); labels = self.labels(rt)
        bridge = self.production_bridge(rt, source)
        frame = camera.Frame(identity=17, image=bytes(819200)); bridge.frame(frame)
        for change in [dict(vendor=frame.vendor + 1), dict(identity=18), dict(sensor=frame.sensor + 1)]:
            with self.subTest(change=change):
                record = self.emit_pipeline_stock(bridge, labels, frame, **change)
                self.assertFalse(record.association_valid)
                self.assertEqual((record.source_seq, record.mouth_sensor_ns, record.vendor_ns, record.raw_identity), (0, 0, 0, 0))

    def test_stock_live_rebased_replay_cannot_borrow_a_current_camera_sensor(self):
        rt = self.runtime(); source = rt.source(); labels = self.labels(rt)
        bridge = self.production_bridge(rt, source)
        live = camera.Frame(identity=17, image=bytes(819200)); bridge.frame(live)
        # Replay reuses/rebases a current sensor but retains another frame's vendor/identity.
        record = self.emit_pipeline_stock(bridge, labels, live, sensor=live.sensor,
                                         vendor=live.vendor - 11111111, identity=16)
        self.assertFalse(record.association_valid)
        # Direct dispatch without the existing pipeline-entry input capture is also unproven.
        self.assertFalse(self.emit_pipeline_stock(bridge, labels, live, captured=False).association_valid)

    def test_stock_live_duplicate_sensor_invalidates_the_original_candidate(self):
        rt = self.runtime(); source = rt.source(); labels = self.labels(rt)
        bridge = self.production_bridge(rt, source)
        original = camera.Frame(identity=17, image=bytes(819200)); bridge.frame(original)
        duplicate = camera.Frame(identity=18, sensor=original.sensor, image=bytes(819200)); bridge.frame(duplicate)
        self.assertEqual(bridge.stats()['sensor_duplicate'], 1)
        self.assertFalse(self.emit_pipeline_stock(bridge, labels, original).association_valid)
        self.assertFalse(self.emit_pipeline_stock(bridge, labels, duplicate).association_valid)

    def test_stock_live_duplicate_identity_is_not_reused_for_a_label(self):
        rt = self.runtime(); source = rt.source(); labels = self.labels(rt)
        bridge = self.production_bridge(rt, source)
        frame = camera.Frame(identity=17, image=bytes(819200)); bridge.frame(frame); bridge.frame(frame)
        self.assertEqual(bridge.stats()['duplicate'], 1)
        self.assertFalse(self.emit_pipeline_stock(bridge, labels, frame).association_valid)

    def test_stock_live_generation_and_session_mismatch_stay_unassociated(self):
        rt = self.runtime(); source = rt.source(); labels = self.labels(rt)
        bridge = self.production_bridge(rt, source)
        frame = camera.Frame(identity=17, image=bytes(819200)); bridge.frame(frame)
        for fields in [dict(generation=bridge.generation + 1), dict(capture_session=source.session ^ 1)]:
            with self.subTest(fields=fields):
                self.assertFalse(self.emit_pipeline_stock(bridge, labels, frame, **fields).association_valid)

    def test_stock_live_history_eviction_and_new_arm_do_not_backfill(self):
        rt = self.runtime(); source = rt.source(); labels = self.labels(rt)
        bridge = self.production_bridge(rt, source)
        frames = [camera.Frame(identity=i, image=bytes(819200)) for i in range(1, 66)]
        for frame in frames:
            bridge.frame(frame)
        self.assertFalse(self.emit_pipeline_stock(bridge, labels, frames[0]).association_valid)
        self.assertTrue(self.emit_pipeline_stock(bridge, labels, frames[-1]).association_valid)
        bridge.detach()
        camera.DLL.camtest_arm_stock(bridge.handle, source.handle, source.session, bridge.generation,
                                     bridge.callback, bridge.stock_callback, 1)
        self.assertFalse(self.emit_pipeline_stock(bridge, labels, frames[-1]).association_valid)

    def test_stock_live_non_camera5_and_unreviewed_entry_never_associate(self):
        for audited, camera_id in [(True, 4), (False, 5)]:
            with self.subTest(audited=audited, camera=camera_id):
                rt = self.runtime(); source = rt.source(); labels = self.labels(rt)
                bridge = self.production_bridge(rt, source, audited=audited)
                frame = camera.Frame(identity=17, camera=camera_id, image=bytes(819200)); bridge.frame(frame)
                self.assertFalse(self.emit_pipeline_stock(bridge, labels, frame).association_valid)

    def test_stock_internal_input_marker_cannot_be_submitted_directly_to_wire(self):
        rt = self.runtime(); source = rt.source(); self.labels(rt)
        data = Record.from_buffer_copy(stock(session=source.session).pack())
        C.c_uint32.from_buffer(data, 632).value = 0x3149534d
        source.module.lib.ptj_submit_stock.argtypes = [C.c_void_p, C.POINTER(Record)]
        self.assertEqual(source.module.lib.ptj_submit_stock(source.handle, C.byref(data)), 7)

    def test_context_unattached_daemon_refuses_instead_of_zero_generation(self):
        rt = self.runtime()
        with self.assertRaises(CaptureContextUnavailable):
            self.context(rt, attached=False)

    def test_context_precedes_images_and_needs_no_latest_stock(self):
        rt = self.runtime(); source = rt.source()
        context = self.context(rt)
        self.assertEqual(context.capture_session_id, source.session)
        self.assertEqual(context.service_generation, rt.module.probe()['client_birth'])
        self.assertNotEqual(context.service_generation, 0)
        self.assertEqual(source.submit(address=1), 1)  # CONTEXT creates no image subscription.
        images = rt.connect(); labels = self.labels(rt)
        self.assertEqual(source.submit(), 0)
        header, _ = ipc.packet(images)
        context.require_session(header['session_id'])
        self.assertEqual(self.submit(source, stock(session=source.session)), 0)
        record = read_stock(labels)
        self.assertEqual((record.capture_session_id, record.service_generation),
                         (context.capture_session_id, context.service_generation))
        self.assertFalse(record.association_valid)  # Context is not timestamp provenance.

    def test_discovery_uses_real_daemon_without_image_or_label_subscriptions(self):
        rt = self.runtime(); source = rt.source()
        context = self.context(rt)
        result = discover_sources(.05, [('127.0.0.1', rt.port)])
        self.assertEqual(len(result['devices']), 1)
        device = result['devices'][0]
        self.assertEqual(device['port'], rt.port)
        self.assertEqual(device['session_id'], str(context.capture_session_id))
        self.assertEqual(device['service_generation'], str(context.service_generation))
        self.assertEqual(source.submit(address=1), 1)
        self.assertEqual(rt.module.probe()['stock_subscription'], 0)

    def test_discovery_unattached_daemon_stays_silent(self):
        rt = self.runtime()
        self.assertEqual(discover_sources(.03, [('127.0.0.1', rt.port)])['devices'], [])

    def test_discovery_ignores_unknown_requests(self):
        import socket
        rt = self.runtime(); rt.source(); self.context(rt)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(.05)
            sock.sendto(b'DISCOVER_DAEMON', ('127.0.0.1', rt.port))
            with self.assertRaises(socket.timeout):
                sock.recvfrom(256)
            sock.sendto(DISCOVER, ('127.0.0.1', rt.port))
            self.assertIsInstance(CaptureContext.unpack(sock.recvfrom(256)[0]), CaptureContext)

    def test_context_two_real_client_generations_are_independent(self):
        contexts = []
        for _ in range(2):
            rt = self.runtime()
            child = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', ipc.__file__, '--client-recover',
                                      str(rt.module.root), str(rt.module.library_path)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertTrue(child.stdout.readline().startswith('CLIENT_READY '))
                context = self.context(rt)
                contexts.append(context)
                self.assertEqual(context.service_generation, rt.module.probe()['client_birth'])
                images = rt.connect()
                header, _ = ipc.packet(images)
                context.require_session(header['session_id'])
                child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0)
            finally:
                if child.poll() is None:
                    child.kill(); child.communicate(timeout=3)
        self.assertNotEqual(contexts[0].capture_session_id, contexts[1].capture_session_id)
        self.assertNotEqual(contexts[0].service_generation, contexts[1].service_generation)
        with self.assertRaises(ValueError):
            contexts[0].require_session(contexts[1].capture_session_id)

    def test_context_standalone_core_rejects_unknown_and_rebinding(self):
        core = C.CDLL(str(ipc.BUILD / 'ipc_core_test.dll'))
        core.core_ptj_start.argtypes = [C.POINTER(ipc.Config), C.POINTER(C.c_void_p)]
        core.core_ptj_stop.argtypes = [C.c_void_p]
        core.core_ptj_destroy.argtypes = [C.c_void_p]
        core.core_ptj_bind_capture_context.argtypes = [C.c_void_p, C.c_uint64, C.c_uint64]
        cfg = ipc.Config(C.sizeof(ipc.Config), ipc.free_port(), 0, 100, 0, 987)
        handle = C.c_void_p()
        self.assertEqual(core.core_ptj_start(C.byref(cfg), C.byref(handle)), 0)
        import socket
        def context():
            with socket.create_connection(('127.0.0.1', cfg.port), timeout=3) as sock:
                return read_capture_context(sock)
        try:
            with self.assertRaises(CaptureContextUnavailable):
                context()
            self.assertEqual(core.core_ptj_bind_capture_context(handle, 987, 0), 7)
            self.assertEqual(core.core_ptj_bind_capture_context(handle, 986, 100), 6)
            self.assertEqual(core.core_ptj_bind_capture_context(handle, 987, 100), 0)
            self.assertEqual(context(), CaptureContext(987, 100))
            self.assertEqual(core.core_ptj_bind_capture_context(handle, 987, 101), 7)
            self.assertEqual(core.core_ptj_bind_capture_context(handle, 987, 100), 0)
            self.assertEqual(context(), CaptureContext(987, 100))
            core.core_ptj_stop(handle)
            self.assertEqual(core.core_ptj_bind_capture_context(handle, 987, 100), 5)
        finally:
            core.core_ptj_destroy(handle)


class StageTests(unittest.TestCase):
    def test_old_payload_is_jpeg_only_and_new_sink_is_separate_capability(self):
        self.assertEqual(LIB.stock_capability(1, 0, 1, 1, 0), b'jpeg_only_missing_main_payload_sink')
        self.assertEqual(LIB.stock_capability(1, 1, 1, 1, 0), b'available_unassociated_missing_input_identity')
        self.assertEqual(LIB.stock_capability(1, 1, 1, 1, 1), b'association_ready_not_device_verified')
        self.assertEqual(LIB.stock_capability(1, 1, 1, 0, 1), b'transport_unavailable')
        self.assertEqual(LIB.stock_capability(0, 0, 0, 0, 0), b'inactive')

    def associate(self, metas, proven=True, session=11, stamp=1000):
        data = Record.from_buffer_copy(stock(session=session).pack())
        # stock_input_ns has its own offset; a camera timestamp is never substituted.
        C.c_uint64.from_buffer(data, 48).value = stamp
        candidate = next((m for m in metas if m.mouth_sensor_ns == stamp), None)
        if candidate:
            C.c_uint64.from_buffer(data, 56).value = candidate.vendor_ns
            C.c_uint64.from_buffer(data, 64).value = candidate.raw_identity
        result = LIB.stock_associate((ipc.Meta * len(metas))(*metas), len(metas), C.byref(data), int(proven))
        return result, StockRecord.unpack(bytes(data))

    def test_exact_not_nearest_and_clock_must_be_proven(self):
        meta = ipc.Meta(11, 7, 1000, 2000, 1000, 1000, 1000, 7, 0, 0)
        result, record = self.associate([meta])
        self.assertEqual(result, 1)
        self.assertEqual(record.image_key, (11, 7, 1000))
        for proven, session, stamp in [(False, 11, 1000), (True, 12, 1000), (True, 11, 1001)]:
            result, record = self.associate([meta], proven, session, stamp)
            self.assertEqual(result, 0); self.assertIsNone(record.source_key)

    def test_ambiguous_and_evicted_sources_stay_unassociated(self):
        a = ipc.Meta(11, 1, 1000, 2000, 1000, 1000, 1000, 1, 0, 0)
        self.assertEqual(self.associate([a, a])[0], 0)
        metas = [ipc.Meta(11, i, 999 + i, 2000, 999 + i, 999 + i, 999 + i, i, 0, 0)
                 for i in range(1, 66)]
        self.assertEqual(self.associate(metas)[0], 0)
        self.assertEqual(self.associate(metas, stamp=1064)[0], 1)

    def test_raw_instant_preclamp_final_and_quality_are_separate(self):
        raw = [-.125] + [.03125] * 31 + [.0625, .25, .75]
        values = (C.c_float * (35 * 4))(*(raw * 4))
        records = (Record * 4)(); outputs = (C.c_float * (52 * 4))()
        LIB.stock_mouth(values, 4, records, outputs)
        slots = [43,29,37,25,32,7,15,5,49,23,27,40,10,24,50,6,20,22,34,48,18,14,17,9,19,21,33,42,39,8,1,13,51]
        for i in range(4):
            record = StockRecord.unpack(bytes(records[i]))
            self.assertEqual(record.raw33[0], -.125)
            self.assertLess(record.instant_preclamp33[0], 0)
            self.assertEqual(record.instant33[0], 0)
            self.assertEqual(record.raw33[32], .0625)
            self.assertEqual(record.instant33[32], 0)
            self.assertEqual(record.quality2, [.25, .75])
            self.assertEqual(record.final33, [outputs[52*i+s] for s in slots])
            self.assertEqual(record.warmup, i < 2)
            self.assertTrue(all(math.isfinite(x) for x in record.final33))


if __name__ == '__main__':
    ipc.RUN.mkdir(parents=True, exist_ok=True)
    suite = unittest.TestSuite()
    suite.addTests(StockNativeTests(n) for n in StockNativeTests.__dict__ if n.startswith(('test_stock_', 'test_context_', 'test_discovery_')))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(StageTests))
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
