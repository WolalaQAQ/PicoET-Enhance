from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest

from face_dataset import load_dataset, write_samples
from face_schema import CHANNELS, SCHEMA
from face_weak_supervision import (choose_mapping, derive_row, fixed_split, frame_key, hardlink_image,
                                   map_raw, pair_rejections, review_index, time_index, validate_weak_row)
from face_weak_supervision import spaced_time_index


def sample(seq=1, session='train', sensor=None):
    stock = dict(capture_session_id=11 if session == 'train' else 12, service_generation=22,
                 source_seq=seq, mouth_sensor_ns=sensor or seq*100_000_000,
                 source_identity=seq, raw_identity=seq, vendor_ns=seq*100_000_000+50,
                 raw33=[.2]*33, quality2=[.99, .01], valid=True, mouth_valid=True,
                 association_valid=True, identity_proven=True, identity_evidence='verified',
                 warmup=False, stock_input_ns=sensor or seq*100_000_000)
    return dict(sample_id=f'f{seq:09d}', image=f'images/f{seq:09d}.jpg', person_id='person',
                session_id=session, fit_id=session, source_group_id=session,
                capture_session_id=stock['capture_session_id'], service_generation=22,
                source_seq=seq, mouth_sensor_ns=stock['mouth_sensor_ns'], source_identity=seq,
                targets=[None]*50, target_sources=[None]*50, stock=stock, stock_id=f's{seq}',
                pairing_status='paired', pairing_method='verified_source', identity_conflict=False,
                header=dict(session_id=stock['capture_session_id'], source_seq=seq,
                            mouth_sensor_ns=stock['mouth_sensor_ns'], raw_identity=seq,
                            vendor_ns=stock['vendor_ns']))


def inputs(row):
    manifest = {field: row[field] for field in ('person_id', 'session_id', 'fit_id', 'source_group_id')}
    manifest['capture_bindings'] = {str(row['capture_session_id']): dict(
        service_generation=22, evidence=dict(protocol='PTC1', capture_session_id=row['capture_session_id'],
                                             service_generation=22))}
    return manifest, {row['stock_id']: dict(record=deepcopy(row['stock']), duplicate_of=None)}


class WeakSupervisionTests(unittest.TestCase):
    def test_exact_identity_quality_and_payload(self):
        row = sample()
        manifest, ledger = inputs(row)
        self.assertEqual(pair_rejections(row, manifest, ledger), [])
        for field, value, expected in (
                ('source_seq', 999, 'stock_identity_mismatch'),
                ('raw_identity', 999, 'original_stock_payload_mismatch'),
                ('valid', False, 'mouth_invalid_or_unknown'),
                ('mouth_valid', None, 'mouth_invalid_or_unknown'),
                ('warmup', None, 'warmup_or_unknown'),
                ('association_valid', False, 'unproven_stock_identity'),
                ('quality2', [.1, .9], 'quality0_not_above_0.1'),
                ('quality2', [float('nan'), .1], 'quality2_invalid_or_unknown'),
                ('quality2', [1.1, -.1], 'quality2_invalid_or_unknown'),
                ('raw33', [float('nan')]*33, 'raw33_invalid_or_unknown')):
            with self.subTest(field=field, value=value):
                changed = deepcopy(row)
                changed['stock'][field] = value
                self.assertIn(expected, pair_rejections(changed, manifest, ledger))

    def test_header_binding_group_vendor_and_original_ledger(self):
        row = sample()
        manifest, ledger = inputs(row)
        changed = deepcopy(row)
        changed['header']['raw_identity'] = 2
        self.assertIn('header_identity_mismatch', pair_rejections(changed, manifest, ledger))
        changed = deepcopy(row)
        changed['stock']['vendor_ns'] = 0
        self.assertIn('vendor_identity_mismatch', pair_rejections(changed, manifest, ledger))
        changed = deepcopy(row)
        changed['stock']['stock_input_ns'] = 3
        self.assertIn('stock_input_sensor_mismatch', pair_rejections(changed, manifest, ledger))
        self.assertIn('missing_or_duplicate_original_stock', pair_rejections(row, manifest, {}))
        manifest['capture_bindings'] = {}
        self.assertIn('missing_verified_capture_binding', pair_rejections(row, manifest, ledger))
        changed['fit_id'] = 'other'
        self.assertIn('recording_group_mismatch', pair_rejections(changed, manifest, ledger))

    def test_unknown_validity_and_pairing_rejected(self):
        row = sample()
        manifest, ledger = inputs(row)
        row['pairing_status'] = 'missing_stock'
        self.assertEqual(pair_rejections(row, manifest, ledger), ['not_exact_paired'])
        row['pairing_status'], row['source_identity'] = 'paired', None
        self.assertIn('incomplete_frame_identity', pair_rejections(row, manifest, ledger))

    def test_mapping_keeps_signs_and_does_not_expand_low_action_channel(self):
        row = sample()
        row['stock']['raw33'][:4] = [-.114, .015, 1.01, .004]
        original = deepcopy(row)
        mapping = choose_mapping([row])
        targets = map_raw(row['stock']['raw33'], mapping)
        self.assertEqual(targets[:4], [None, .015, None, .004])
        self.assertEqual(targets[33:], [None]*17)
        self.assertEqual(row, original)
        self.assertEqual(mapping['selected_from_sessions'], ['train'])
        self.assertTrue(all(v['scale'] == 1 and v['offset'] == 0 for v in mapping['channels'].values()))

    def test_review_priority_and_individual_weights(self):
        source = sample()
        source['stock']['raw33'][0] = -.02
        review = deepcopy(source)
        review['targets'][5], review['target_sources'][5] = 1., 'reviewed_visual:control'
        row = derive_row(source, choose_mapping([source]), review)
        self.assertEqual((row['targets'][5], row['target_weights'][5]), (1., 1.))
        self.assertEqual(row['stock_raw_targets'][5], .2)
        self.assertEqual((row['targets'][1], row['target_sources'][1], row['target_weights'][1]), (.2, 'stock_raw', .5))
        self.assertIsNone(row['targets'][0])
        self.assertEqual(row['target_weights'][0], 0)
        self.assertEqual(row['targets'][33:], [None]*17)
        self.assertEqual(row['stock']['raw33'][0], -.02)

    def test_overlay_cannot_match_by_sample_id_or_partial_identity(self):
        source, review = sample(), sample()
        review['targets'][5], review['target_sources'][5] = 1., 'human'
        review['source_identity'] = 2
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            derive_row(source, choose_mapping([source]), review)
        review['source_identity'] = None
        with self.assertRaises(ValueError):
            review_index([review])

    def test_exclusion_cannot_be_resurrected_by_raw_or_review(self):
        for field, value in (('review_excluded', True), ('observation', 'occluded')):
            source = sample()
            review = deepcopy(source)
            review['targets'][5], review['target_sources'][5] = 1., 'human'
            review[field] = value
            row = derive_row(source, choose_mapping([source]), review)
            self.assertTrue(row['review_excluded'])
            self.assertFalse(row['trainable'])
            self.assertEqual(row['targets'], [None]*50)
            self.assertEqual(row['target_weights'], [0.]*50)
            self.assertEqual(time_index([row])[0], [])

    def test_review_index_excludes_unlabelled_and_rejects_duplicate(self):
        row = sample()
        self.assertEqual(review_index([row]), {})
        row['review_excluded'] = True
        self.assertEqual(list(review_index([row])), [frame_key(row)])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            review_index([row, row])

    def test_weight_contract(self):
        row = derive_row(sample(), choose_mapping([sample()]))
        row['target_weights'][1] = 1.
        with self.assertRaisesRegex(ValueError, 'Weight'):
            validate_weak_row(row)

    def test_source_time_sampling_retains_all_anchors_and_grouping(self):
        rows = []
        mapping = choose_mapping([sample()])
        for session in ('train', 'val'):
            for i in range(1, 22):
                row = derive_row(sample(i, session, i*11_111_111), mapping)
                if i in (2, 3):
                    row['target_sources'][5], row['target_weights'][5] = 'human', 1.
                rows.append(row)
        selected, report = time_index(rows)
        self.assertEqual(len(selected), 6)
        self.assertEqual({r['source_seq'] for r in selected}, {2, 3, 20})
        self.assertEqual(report['sessions']['train']['extra_reviewed_anchors'], 1)
        split = fixed_split(selected, 'train', 'val')
        self.assertEqual(split['counts'], dict(train=3, validation=3, test=0))

    def test_fixed_diagnostic_is_not_sealed_and_rejects_frame_leakage(self):
        rows = [sample(session=s) for s in ('train', 'val', 'legacy')]
        rows[2]['capture_session_id'] = 13
        split = fixed_split(rows, 'train', 'val', ['legacy'])
        self.assertEqual(split['counts'], dict(train=1, validation=1, test=1))
        self.assertEqual(split['partition_roles']['test'], 'diagnostic')
        rows[1]['capture_session_id'] = rows[0]['capture_session_id']
        with self.assertRaisesRegex(ValueError, 'crosses splits'):
            fixed_split(rows, 'train', 'val', ['legacy'])

    def test_strict_thinning_reserves_anchors_without_adjacent_automatic_frame(self):
        mapping = choose_mapping([sample()])
        rows = [derive_row(sample(i, sensor=i*11_111_111), mapping) for i in range(1, 101)]
        for i in (17, 40, 70):
            rows[i]['target_sources'][5], rows[i]['target_weights'][5] = 'human', 1.
        selected, report = spaced_time_index(rows)
        self.assertTrue({18, 41, 71}.issubset({r['source_seq'] for r in selected}))
        self.assertTrue(all(b['mouth_sensor_ns']-a['mouth_sensor_ns'] >= 200_000_000
                            for a, b in zip(selected, selected[1:])))
        self.assertEqual(report['sessions']['train']['mandatory_anchor_close_pairs'], 0)
        rows[18]['target_sources'][5], rows[18]['target_weights'][5] = 'human', 1.
        selected, report = spaced_time_index(rows)
        self.assertTrue({18, 19}.issubset({r['source_seq'] for r in selected}))
        self.assertEqual(report['sessions']['train']['mandatory_anchor_close_pairs'], 1)

    def test_hardlinks_and_shared_dataset_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root/'original.jpg'
            original.write_bytes(b'original-read-only-reference')
            before = original.read_bytes(), original.stat().st_mtime_ns
            corpus = root/'corpus'
            corpus.mkdir()
            row = derive_row(sample(), choose_mapping([sample()]))
            hardlink_image(original, corpus, row['image'])
            self.assertTrue(os.path.samefile(original, corpus/row['image']))
            with self.assertRaises(ValueError):
                hardlink_image(original, corpus, '../escape.jpg')
            (corpus/'manifest.json').write_text(json.dumps(dict(
                schema=SCHEMA, channels=list(CHANNELS), samples_complete=True)), encoding='utf-8')
            write_samples(corpus/'samples.jsonl', [row])
            self.assertEqual(load_dataset(corpus)[1][0]['target_weights'][:33], [.5]*33)
            self.assertEqual((original.read_bytes(), original.stat().st_mtime_ns), before)


if __name__ == '__main__':
    unittest.main()
