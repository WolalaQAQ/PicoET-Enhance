"""Check the numerical examples and the documented native channel order."""
import math
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]


class ExplanationExamplesTest(unittest.TestCase):
    def test_mouth_names_are_backed_by_the_native_reorder(self):
        source = (ROOT/'hook/src/expression/mouth.cpp').read_text()
        table = re.search(r'slots\[kMouthChannels\] = \{([^}]+)\}', source)[1]
        slots = list(map(int, re.findall(r'\d+', table)))
        self.assertEqual(slots[5], 7)  # raw mouth[5] -> PICO JawOpen slot 7
        self.assertEqual(slots[7], 5)  # raw mouth[7] -> PICO MouthClose slot 5
        self.assertEqual(slots[32], 51)  # TongueOut
        self.assertIn('slot == 7 || slot == 19 || slot == 21 || slot == 24 || slot == 48', source)
        self.assertGreater(source.index('float gated = mouth[32]'), source.index('::log10f(scale)'))

    def test_brow_coefficient_is_squared_and_time_normalized(self):
        source = (ROOT/'hook/src/expression/numerical.cpp').read_text()
        self.assertIn('static_cast<double>(weight) * weight * (1.0 - weighted_motion)', source)
        reference = .6 ** 2
        self.assertAlmostEqual(reference, .36)
        at_90_hz = 1 - (1 - reference) ** (24 / 90)
        self.assertAlmostEqual(at_90_hz * 100, 11.2, delta=.05)

    def test_adaptive_small_and_large_change_examples(self):
        self.assertAlmostEqual((1 - math.exp(-9 * .01)) * 100, 8.6, delta=.05)
        self.assertAlmostEqual((1 - math.exp(-9 * .1)) * 100, 59.3, delta=.05)

    def test_lip_closure_addition_example(self):
        self.assertAlmostEqual(min(.02 / .04 * .02 + .08, .1), .09)

    def test_second_pass_uses_the_same_previous_output(self):
        current, previous, k = .3, .2, 6
        alpha1 = 1 - math.exp(-k * abs(current - previous))
        middle = previous + alpha1 * (current - previous)
        alpha2 = 1 - math.exp(-k * abs(middle - previous))
        self.assertAlmostEqual(alpha1 * 100, 45.1, delta=.05)
        self.assertAlmostEqual(alpha2 * 100, 23.7, delta=.05)
        self.assertAlmostEqual(alpha1 * alpha2 * 100, 10.7, delta=.05)
        for mix in (0, .25, 1):
            reference = alpha1 * (1 - mix + mix * alpha2)
            output = (1 - mix) * middle + mix * (previous + alpha2 * (middle - previous))
            self.assertAlmostEqual(output, previous + reference * (current - previous))
        source = (ROOT/'hook/src/expression/mouth.cpp').read_text()
        self.assertIn('std::fabs(middle - previous)', source)
        self.assertIn('alpha_dt(composite_alpha, ratio)', source)

    def test_log_curve_can_reduce_or_increase_a_positive_signal(self):
        self.assertAlmostEqual(1 + .6 * math.log10(.1 + .1), .581, delta=.0005)
        self.assertAlmostEqual(1 + .6 * math.log10(1 + .1), 1.025, delta=.0005)
        self.assertLess(math.log10(.1 + .1), 0)
        self.assertGreater(math.log10(1 + .1), 0)

    def test_tongue_reference_weight_is_not_the_90_hz_frame_weight(self):
        self.assertAlmostEqual((1 - (1 - .6) ** (24 / 90)) * 100, 21.7, delta=.05)
        self.assertAlmostEqual((1 - .5) * .4, .2)  # half-strength gate retains some below-threshold input


if __name__ == '__main__':
    unittest.main()
