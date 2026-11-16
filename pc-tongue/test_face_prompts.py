import unittest

from face_prompts import POSES, coverage, prompt_plan
from face_schema import CHANNELS, STOCK_CHANNELS


class PromptTests(unittest.TestCase):
    def test_complete_named_channel_coverage_without_reordering(self):
        self.assertEqual(set(POSES), set(CHANNELS))
        self.assertEqual(set(coverage()['covered']), set(CHANNELS))
        self.assertEqual(coverage()['stock_channels'], list(STOCK_CHANNELS))
        self.assertEqual([s['id'] for s in prompt_plan()[1:51]], list(CHANNELS))

    def test_every_step_has_bilingual_phases_and_skip(self):
        steps = prompt_plan()
        self.assertEqual(len({s['id'] for s in steps}), len(steps))
        for step in steps:
            for phase in ('prepare', 'hold', 'confirm', 'skip'):
                self.assertEqual(set(step[phase]), {'zh', 'en'})
                self.assertTrue(all(step[phase].values()))
            self.assertTrue(step['skippable'])
            self.assertTrue(set(step['labels']) <= set(CHANNELS))

    def test_all_diagonals_and_length_order(self):
        steps = {s['id']: s for s in prompt_plan()}
        for side in ('left', 'right'):
            for vertical in ('up', 'down'):
                labels = steps[f'tongue_{side}_{vertical}']['labels']
                self.assertEqual(set(labels), {'Tongue'+side.title(), 'Tongue'+vertical.title()})
        lengths = [steps['tongue_'+name]['labels']['TongueOut'] for name in ('short', 'medium', 'long')]
        self.assertEqual(lengths, sorted(set(lengths)))
        self.assertEqual(steps['tongue_sweep']['labels'], {})

    def test_unknown_not_unspecified_zero(self):
        for step in prompt_plan():
            if step['kind'] == 'pose':
                self.assertEqual(len(step['labels']), 1)
        self.assertIn('skip', POSES['TongueRoll'][1])
        self.assertTrue(prompt_plan()[-1]['requires_new_session'])

    def test_plans_are_independent(self):
        first = prompt_plan()
        first[0]['labels']['TongueLeft'] = 0.
        self.assertNotIn('TongueLeft', prompt_plan()[0]['labels'])


if __name__ == '__main__':
    unittest.main()
