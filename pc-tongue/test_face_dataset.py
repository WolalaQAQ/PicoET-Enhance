import copy
import unittest

from face_dataset import assert_no_split_leakage, original_frame_identity, split_groups, validate_sample


def sample(session='s1', person='p1', source=None):
    seq = {'s1': 3, 's2': 13, 's3': 23, 'renamed': 33}.get(session, 43)
    return dict(image='images/frame.jpg', person_id=person, session_id=session, fit_id=session,
                source_group_id=source or f'recording:{session}:fit:{session}',
                capture_session_id=2, service_generation=8, source_seq=seq,
                mouth_sensor_ns=seq*11_111_111, source_identity=seq+100,
                targets=[None]*50, target_sources=[None]*50)


class DatasetTests(unittest.TestCase):
    def test_unknown_valid_explicit_zero_valid(self):
        row = sample()
        validate_sample(row)
        row['targets'][3], row['target_sources'][3] = 0., 'human_review:id'
        validate_sample(row)

    def test_optional_source_weights_have_named_channel_length_and_finite_scale(self):
        row = dict(sample(), target_weights=[.5]*33+[0.]*17)
        validate_sample(row)
        for weights in ([.5]*49, [-.1]+[1.]*49, [float('nan')]+[1.]*49):
            with self.assertRaisesRegex(ValueError, 'target_weights'):
                validate_sample(dict(row, target_weights=weights))

    def test_nonfinite_or_unattributed_target_rejected(self):
        for value in (float('nan'), float('inf'), -.1, 1.1, .5):
            row = sample()
            row['targets'][3] = value
            with self.assertRaises(ValueError):
                validate_sample(row)

    def test_path_escape_and_wrong_order_length_rejected(self):
        row = sample()
        row['image'] = '../outside.jpg'
        with self.assertRaises(ValueError):
            validate_sample(row)
        row = sample()
        row['targets'] = [None]*44
        with self.assertRaises(ValueError):
            validate_sample(row)

    def test_person_session_fit_and_original_source_groups(self):
        rows = [sample(), sample(), sample('s2', 'p1', 'source2'), sample('s3', 'p2', 'source3')]
        self.assertEqual([len(v) for v in split_groups(rows).values()], [2, 1, 1])
        self.assertEqual([len(v) for v in split_groups(rows, by='person_id').values()], [3, 1])
        self.assertEqual(set(split_groups(rows, by='fit_id')), {'s1', 's2', 's3'})
        with self.assertRaises(ValueError):
            split_groups(rows, by='source_seq')

    def test_neighbor_frames_cannot_cross_session_split(self):
        one = sample()
        two = copy.deepcopy(one)
        two['source_seq'] += 1
        with self.assertRaises(ValueError):
            assert_no_split_leakage({'train': [one], 'test': [two]})

    def test_derived_recording_retains_source_group(self):
        with self.assertRaises(ValueError):
            assert_no_split_leakage({'train': [sample()], 'test': [sample('renamed', source=sample()['source_group_id'])]})

    def test_same_capture_owner_different_recording_fit_can_be_held_out(self):
        first, refitted = sample(), sample('s2')
        self.assertEqual(first['capture_session_id'], refitted['capture_session_id'])
        self.assertEqual(first['service_generation'], refitted['service_generation'])
        assert_no_split_leakage({'train': [first], 'test': [refitted]})
        assert_no_split_leakage({'train': [first], 'test': [refitted]}, by='fit_id')

    def test_same_frame_cannot_be_unlocked_by_renaming_recording_and_fit(self):
        original = sample()
        renamed = dict(copy.deepcopy(original), session_id='new-session', fit_id='new-fit',
                       source_group_id='recording:new-session:fit:new-fit', image='copied.jpg')
        self.assertEqual(original_frame_identity(original), original_frame_identity(renamed))
        with self.assertRaisesRegex(ValueError, 'original_frame'):
            assert_no_split_leakage({'train': [original], 'test': [renamed]})

    def test_derived_lineage_checks_original_frame_even_if_output_ids_change(self):
        original, derived = sample(), sample('s2')
        derived['derived_from'] = [copy.deepcopy(original)]
        with self.assertRaises(ValueError):
            assert_no_split_leakage({'train': [original], 'test': [derived]})
        derived['derived_from'] = [{field: original[field] for field in
                                    ('capture_session_id', 'service_generation', 'source_seq',
                                     'mouth_sensor_ns', 'source_identity')}]
        with self.assertRaisesRegex(ValueError, 'original_frame'):
            assert_no_split_leakage({'train': [original], 'test': [derived]})

    def test_header_raw_identity_is_same_as_top_level_source_identity(self):
        original = sample()
        derived = dict(copy.deepcopy(original), session_id='renamed', fit_id='renamed', source_group_id='renamed')
        derived['header'] = dict(raw_identity=derived.pop('source_identity'))
        self.assertEqual(original_frame_identity(original), original_frame_identity(derived))
        with self.assertRaisesRegex(ValueError, 'original_frame'):
            assert_no_split_leakage({'train': [original], 'test': [derived]})

    def test_dropping_raw_identity_does_not_unlock_the_same_source_frame(self):
        original = sample()
        derived = dict(copy.deepcopy(original), session_id='renamed', fit_id='renamed', source_group_id='renamed')
        derived.pop('source_identity')
        with self.assertRaisesRegex(ValueError, 'original_frame_source'):
            assert_no_split_leakage({'train': [original], 'test': [derived]})

    def test_continuous_recording_stays_whole_with_changed_source_group(self):
        first, next_frame = sample(), sample('s2')
        next_frame['session_id'] = first['session_id']
        with self.assertRaisesRegex(ValueError, 'session_id'):
            assert_no_split_leakage({'train': [first], 'test': [next_frame]}, by='source_group_id')

    def test_same_fit_across_two_takes_stays_whole(self):
        first, second_take = sample(), sample('s2')
        second_take['fit_id'] = first['fit_id']
        with self.assertRaisesRegex(ValueError, 'fit_id'):
            assert_no_split_leakage({'train': [first], 'test': [second_take]})

    def test_person_holdout_and_valid_session_holdout(self):
        assert_no_split_leakage({'train': [sample()], 'test': [sample('s2', 'p1', 'source2')]})
        with self.assertRaises(ValueError):
            assert_no_split_leakage({'train': [sample()], 'test': [sample('s2', 'p1', 'source2')]}, by='person_id')


if __name__ == '__main__':
    unittest.main()
