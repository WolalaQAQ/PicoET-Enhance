import unittest

from face_schema import ALL_VALID, CHANNELS, INDEX, STOCK_CHANNELS, STOCK_SLOTS
from wire import FACE_RESULT, FrameHeader, face_result_packet


def source():
    return FrameHeader(123, 1, 17, 81, 1000, 1001, 1002, 1003, 1004, 1005,
                       1000, 1000, 1000, 81)


class WholeMouthWireTests(unittest.TestCase):
    def test_contract_names_and_order(self):
        self.assertEqual(len(CHANNELS), 50)
        self.assertEqual(len(INDEX), 50)
        self.assertEqual(CHANNELS[:33], STOCK_CHANNELS)
        self.assertEqual(len(set(STOCK_SLOTS)), 33)
        self.assertEqual(CHANNELS[44:], ('MouthUpperLeft', 'MouthUpperRight',
                         'MouthLowerLeft', 'MouthLowerRight', 'LipFunnelUpper', 'LipFunnelLower'))

    def test_full_packet_preserves_source_and_continuous_controls(self):
        values = [index / 50 for index in range(50)]
        packet = face_result_packet(source(), values, ALL_VALID, 3.25)
        fields = FACE_RESULT.unpack(packet)
        self.assertEqual(len(packet), 256)
        self.assertEqual(fields[:6], (b'MFR1', 1, 256, 17, 81, 1000))
        self.assertGreater(fields[6], 0)
        self.assertEqual(fields[7:10], (ALL_VALID, 3.25, 0))
        for actual, expected in zip(fields[10:], values):
            self.assertAlmostEqual(actual, expected, places=6)

    def test_unknown_heads_have_no_valid_bit_or_wire_value(self):
        values = [float('nan')] * 50
        values[INDEX['TongueOut']] = .1
        mask = 1 << INDEX['TongueOut']
        fields = FACE_RESULT.unpack(face_result_packet(source(), values, mask))
        self.assertEqual(fields[7], mask)
        self.assertAlmostEqual(fields[10 + INDEX['TongueOut']], .1)
        self.assertEqual(sum(value != 0 for value in fields[10:]), 1)

    def test_explicit_invalid_releases_all_owned_controls(self):
        fields = FACE_RESULT.unpack(face_result_packet(source(), [float('nan')] * 50,
                                                        ALL_VALID, state=1))
        self.assertEqual(fields[7], 0)
        self.assertEqual(fields[9], 1)
        self.assertEqual(fields[10:], (0.,) * 50)

    def test_no_trained_heads_means_invalid_not_neutral_measurement(self):
        fields = FACE_RESULT.unpack(face_result_packet(source(), [.5] * 50, 0))
        self.assertEqual(fields[7], 0)
        self.assertEqual(fields[9], 1)
        self.assertEqual(fields[10:], (0.,) * 50)

    def test_rejects_malformed_contract(self):
        for values, mask, age, state in [([0.] * 49, 1, 0., 0),
                                         ([0.] * 50, 1 << 50, 0., 0),
                                         ([0.] * 50, -1, 0., 0),
                                         ([2.] * 50, 1, 0., 0),
                                         ([float('nan')] * 50, 1, 0., 0),
                                         ([0.] * 50, 1, -1., 0),
                                         ([0.] * 50, 1, float('inf'), 0),
                                         ([0.] * 50, 1, 0., 2)]:
            with self.subTest(mask=mask, age=age, state=state):
                with self.assertRaises(ValueError):
                    face_result_packet(source(), values, mask, age, state)


if __name__ == '__main__':
    unittest.main()
