from types import SimpleNamespace
import unittest

from face_filter import FaceFilter
from face_schema import CHANNELS, INDEX


class FaceFilterTests(unittest.TestCase):
    def setUp(self):
        self.filter = FaceFilter()
        self.config = dict(smoothing_ms=20., stale_ms=350.)

    def frame(self, value, ms, mask=1, session=1):
        values = [value if mask & (1 << i) else 0. for i in range(50)]
        prediction = dict(values=values, valid_mask=mask,
                          channels={name: values[i] for i, name in enumerate(CHANNELS) if mask & (1 << i)})
        return self.filter.apply(prediction, SimpleNamespace(session_id=session, mouth_sensor_ns=int(ms*1e6)), self.config)

    def test_zero_smoothing_is_exact_passthrough_including_valid_zero(self):
        self.config['smoothing_ms'] = 0
        for ms, value in enumerate([0., .123456789, 1., .01, 0.]):
            result = self.frame(value, ms*11)
            self.assertEqual(result['values'], result['raw_values'])
            self.assertEqual(result['values'][0], value)

    def test_actual_sensor_dt_not_assumed_90hz(self):
        self.frame(0., 1000)
        short = self.frame(1., 1010)['values'][0]
        self.filter.clear()
        self.frame(0., 1000)
        long = self.frame(1., 1040)['values'][0]
        self.assertGreater(long, short)
        self.assertLess(long, 1.)

    def test_mask_session_gap_clock_reset_and_settings_change_restart_at_raw(self):
        for change in ('mask', 'session', 'gap', 'backwards', 'duplicate', 'config', 'clear'):
            self.filter.clear()
            self.frame(0., 1000)
            self.assertLess(self.frame(1., 1011)['values'][0], 1.)
            ms, mask, session = 1022, 1, 1
            if change == 'mask': mask = 3
            if change == 'session': session = 2
            if change == 'gap': ms = 1500
            if change == 'backwards': ms = 1000
            if change == 'duplicate': ms = 1011
            if change == 'config': self.config['smoothing_ms'] += 1
            if change == 'clear': self.filter.clear()
            result = self.frame(.4, ms, mask, session)
            self.assertEqual(result['values'][0], .4, change)

    def test_unknown_channels_and_mask_zero_cannot_keep_state(self):
        self.frame(.7, 1000, 1 << INDEX['JawOpen'])
        result = self.frame(0., 1011, 0)
        self.assertEqual(result['values'], [0.]*50)
        self.assertIsNone(self.filter.values)
        result = self.frame(.2, 1022, 1 << INDEX['JawOpen'])
        self.assertEqual(result['values'][INDEX['JawOpen']], .2)
        self.assertEqual(result['values'][INDEX['TongueOut']], 0.)

    def test_rest_noise_reduced_and_real_step_not_discarded(self):
        raw, filtered = [], []
        for i in range(120):
            value = .5 + (.02 if i % 2 else -.02)
            result = self.frame(value, 1000+i*1000/90)
            if i > 30:
                raw.append(value)
                filtered.append(result['values'][0])
        self.assertLess(max(filtered)-min(filtered), .5*(max(raw)-min(raw)))
        result = self.frame(1., 1000+120*1000/90)
        self.assertGreater(result['values'][0], .75)
        self.assertEqual(result['raw_values'][0], 1.)


if __name__ == '__main__':
    unittest.main()
