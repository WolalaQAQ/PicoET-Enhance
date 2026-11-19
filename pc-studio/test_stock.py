import io
import struct
import unittest

from stock import (StockRecord, read_stock, STOCK_BYTES, CaptureContext,
                   CaptureContextUnavailable, read_capture_context, CONTEXT_WIRE)


def sample(**changes):
    values = dict(capture_session_id=10, service_generation=20, source_seq=0,
                  mouth_sensor_ns=0, raw33=[-0.125] * 33, instant33=[0.0] * 33,
                  final33=[0.25] * 33, quality2=[0.25, 0.75], valid=True,
                  stock_input_ns=12345, instant_preclamp33=[-0.0625] * 33)
    values.update(changes)
    return StockRecord(**values)


class FragmentSocket:
    def __init__(self, data):
        self.data = io.BytesIO(data)
        self.sent = bytearray()

    def sendall(self, data):
        self.sent.extend(data)

    def recv(self, n):
        return self.data.read(min(n, 7))


class StockTests(unittest.TestCase):
    def test_round_trip_all_stages(self):
        record = sample()
        self.assertEqual(len(record.pack()), STOCK_BYTES)
        self.assertEqual(StockRecord.unpack(record.pack()), record)

    def test_unproven_time_is_not_an_image_key(self):
        record = sample()
        self.assertIsNone(record.source_key)
        self.assertIsNone(record.image_key)
        self.assertEqual(record.stock_input_ns, 12345)

    def test_exact_key_includes_generation_and_session(self):
        record = sample(source_seq=7, mouth_sensor_ns=12345, raw_identity=7, association_valid=True)
        self.assertEqual(record.source_key, (10, 20, 7, 12345))
        self.assertEqual(record.image_key, (10, 7, 12345))
        self.assertNotEqual(record.source_key, sample(capture_session_id=11, source_seq=7,
            mouth_sensor_ns=12345, raw_identity=7, association_valid=True).source_key)
        self.assertNotEqual(record.source_key, sample(service_generation=21, source_seq=7,
            mouth_sensor_ns=12345, raw_identity=7, association_valid=True).source_key)

    def test_fragmented_tcp_and_back_to_back(self):
        sock = FragmentSocket(sample().pack() + sample(label_seq=2).pack())
        self.assertEqual(read_stock(sock), sample())
        self.assertEqual(read_stock(sock).label_seq, 2)

    def test_truncated_tcp(self):
        with self.assertRaises(EOFError):
            read_stock(FragmentSocket(sample().pack()[:-1]))

    def test_bad_header_and_stage_are_rejected(self):
        for offset in (0, 4, 6, 8, 92):
            data = bytearray(sample().pack())
            data[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                StockRecord.unpack(data)

    def test_nonfinite_and_wrong_vectors_are_rejected(self):
        with self.assertRaises(ValueError):
            sample(raw33=[float('nan')] * 33).pack()
        with self.assertRaises(ValueError):
            sample(quality2=[1]).pack()
        data = bytearray(sample().pack())
        struct.pack_into('<f', data, 96, float('inf'))
        with self.assertRaises(ValueError):
            StockRecord.unpack(data)

    def test_missing_identity_is_not_accepted_as_associated(self):
        with self.assertRaises(ValueError):
            sample(association_valid=True).pack()

    def test_unassociated_cannot_contain_fabricated_camera_metadata(self):
        with self.assertRaises(ValueError):
            sample(source_seq=1).pack()

    def test_zero_instant_tongue_does_not_rewrite_raw(self):
        record = sample(raw33=[0.0625] * 33)
        decoded = StockRecord.unpack(record.pack())
        self.assertEqual(decoded.raw33[32], 0.0625)
        self.assertEqual(decoded.instant33[32], 0)
        self.assertFalse(hasattr(decoded, 'tongue_negative'))


class CaptureContextTests(unittest.TestCase):
    def test_fixed_ptc1_round_trip_and_binding_evidence(self):
        context = CaptureContext(10, 20)
        self.assertEqual(context.pack(), struct.pack('<4sHHQQ', b'PTC1', 1, 24, 10, 20))
        self.assertEqual(CaptureContext.unpack(context.pack()), context)
        self.assertEqual(context.evidence['protocol'], 'PTC1')
        self.assertEqual(bytes.fromhex(context.evidence['wire_hex']), context.pack())

    def test_control_command_is_sent_and_fragmented_response_is_complete(self):
        sock = FragmentSocket(CaptureContext(10, 20).pack())
        self.assertEqual(read_capture_context(sock), CaptureContext(10, 20))
        self.assertEqual(sock.sent, b'CONTEXT\n')

    def test_unknown_attach_and_partial_context_are_not_fabricated(self):
        for data in [b'', CaptureContext(10, 20).pack()[:-1]]:
            with self.subTest(length=len(data)), self.assertRaises(CaptureContextUnavailable):
                read_capture_context(FragmentSocket(data))

    def test_unknown_generation_and_bad_header_are_rejected(self):
        for values in [(b'PTC1', 1, 24, 10, 0), (b'PTC1', 1, 24, 0, 20),
                       (b'PTC2', 1, 24, 10, 20), (b'PTC1', 2, 24, 10, 20),
                       (b'PTC1', 1, 32, 10, 20)]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                CaptureContext.unpack(CONTEXT_WIRE.pack(*values))

    def test_every_frame_must_keep_the_bound_session(self):
        context = CaptureContext(10, 20)
        context.require_session(10)
        for session in [0, 11]:
            with self.subTest(session=session), self.assertRaises(ValueError):
                context.require_session(session)


if __name__ == '__main__':
    unittest.main()
