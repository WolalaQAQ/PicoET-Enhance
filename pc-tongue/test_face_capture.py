from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from face_capture import WholeMouthRecording
from face_dataset import assert_no_split_leakage, load_dataset
from face_schema import CHANNELS, INDEX
from wire import END, JPEG, FrameHeader


def jpeg_bytes():
    data = io.BytesIO()
    Image.new('L', (400, 400), 80).save(data, 'JPEG', quality=95)
    return data.getvalue()


JPEG_DATA = jpeg_bytes()


def header(seq=1, session=8, ns=None):
    ns = ns if ns is not None else seq*11_111_111
    return FrameHeader(len(JPEG_DATA), JPEG, session, seq, ns, ns+3, ns+10, ns+11, ns+12,
                       ns+13, ns, ns, ns, seq+100)


def record(h=None, **changes):
    h = h or header()
    result = dict(capture_session_id=h.session_id, service_generation='bootA:pid1:birth1',
                  source_seq=h.source_seq, mouth_sensor_ns=h.mouth_sensor_ns,
                  source_identity=h.raw_identity, identity_proven=True,
                  identity_evidence='offline fixture: exact input identity',
                  raw33=[-.2]+[.1]*32, instant33=[.2]*32+[0.], final33=[.3]*33,
                  quality2=[.9, .1], mouth_valid=True, warmup=False)
    result.update(changes)
    return result


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'recordings'
        self.capture = WholeMouthRecording(self.root)

    def begin(self, *, stage='raw_only', guide=False):
        self.capture.set_label_stage(stage)
        self.capture.begin('anonymous-1', confirmed=True, guide=guide)
        self.capture.bind_capture(8, 'bootA:pid1:birth1', evidence='fixture transport session')

    def rows(self, name):
        return [json.loads(line) for line in (self.capture.path/name).read_text(encoding='utf-8').splitlines()]

    def test_no_consent_no_directory_or_callbacks(self):
        with self.assertRaises(ValueError):
            self.capture.begin('p')
        self.assertFalse(self.root.exists())
        self.assertFalse(self.capture.on_frame(header(), JPEG_DATA))
        self.assertFalse(self.capture.on_stock(record()))
        self.assertFalse(self.root.exists())

    def test_role_is_persisted_in_manifest_before_first_frame_and_in_rows(self):
        for role in (None, 'A', 'B', 'C'):
            with self.subTest(role=role):
                self.capture.begin('fixture', confirmed=True, guide=False, role=role)
                before = json.loads((self.capture.path/'manifest.json').read_text())
                self.assertEqual(before['observability_role'], role)
                self.assertEqual(before['evaluation_only'], role == 'C')
                self.assertFalse(list((self.capture.path/'images').iterdir()))
                self.capture.bind_capture(8, 'bootA:pid1:birth1', evidence='fixture')
                self.capture.on_frame(header(), JPEG_DATA)
                self.capture.stop()
                _, rows = load_dataset(self.capture.path)
                self.assertEqual(rows[0]['observability_role'], role)
                self.assertEqual(rows[0]['evaluation_only'], role == 'C')

    def test_unknown_role_and_contradictory_evaluation_flag_rejected_before_mkdir(self):
        for role, restricted in (('unknown', None), (None, True), ('A', True), ('C', False)):
            with self.subTest(role=role):
                with self.assertRaises(ValueError):
                    self.capture.begin('fixture', confirmed=True, role=role, evaluation_only=restricted)
                self.assertFalse(self.root.exists())

    def test_raw_default_preserves_signed_values_no_targets(self):
        self.begin()
        self.capture.on_frame(header(), JPEG_DATA)
        self.capture.on_stock(record())
        self.capture.stop()
        manifest, rows = load_dataset(self.capture.path)
        self.assertEqual(manifest['channels'], list(CHANNELS))
        self.assertEqual(rows[0]['pairing_status'], 'paired')
        self.assertEqual(rows[0]['stock']['raw33'][0], -.2)
        self.assertEqual(rows[0]['targets'], [None]*50)
        self.assertFalse(rows[0]['trainable'])
        self.assertEqual((self.capture.path/rows[0]['image']).read_bytes(), JPEG_DATA)

    def test_instant_compatibility_is_explicit_and_gated_zero_unknown(self):
        self.begin(stage='instant')
        self.capture.on_stock(record())
        self.capture.on_frame(header(), JPEG_DATA)
        row = self.capture.samples()[0]
        self.assertEqual(row['targets'][:32], [.2]*32)
        self.assertEqual(row['targets'][32:], [None]*18)
        self.assertEqual(row['target_sources'][0], 'stock_instant_compatibility')
        self.assertEqual(row['label_stage'], 'instant')

    def test_out_of_order_join_never_uses_latest(self):
        self.begin(stage='instant')
        self.capture.on_stock(record(header(2), instant33=[.8]*33))
        self.capture.on_frame(header(1), JPEG_DATA)
        self.assertEqual(self.capture.samples()[0]['targets'], [None]*50)
        self.capture.on_stock(record(header(1), instant33=[.1]*33))
        self.capture.on_frame(header(2), JPEG_DATA)
        self.assertEqual([r['targets'][0] for r in self.capture.samples()], [.1, .8])

    def test_stock_dict_is_snapshot_not_reused_producer_buffer(self):
        self.begin(stage='instant')
        row = record()
        self.capture.on_stock(row)
        row['instant33'][0] = .99
        row['raw33'][0] = 987.
        self.capture.on_frame(header(), JPEG_DATA)
        sample = self.capture.samples()[0]
        self.assertEqual(sample['targets'][0], .2)
        self.assertEqual(sample['stock']['raw33'][0], -.2)

    def test_out_of_order_images_supported(self):
        self.begin()
        for seq in (3, 1, 2):
            self.capture.on_frame(header(seq), JPEG_DATA)
        for seq in (2, 3, 1):
            self.capture.on_stock(record(header(seq)))
        self.assertEqual(self.capture.status()['pairing'], {'paired': 3})

    def test_duplicates_ledger_no_duplicate_training_rows(self):
        self.begin()
        self.capture.on_frame(header(), JPEG_DATA)
        self.capture.on_frame(header(), JPEG_DATA)
        self.capture.on_stock(record())
        self.capture.on_stock(record())
        self.capture.stop()
        self.assertEqual(len(self.rows('images.jsonl')), 2)
        self.assertEqual(len(self.rows('stock.jsonl')), 2)
        self.assertEqual(len(self.rows('samples.jsonl')), 1)
        self.assertEqual(self.capture.status()['counts']['image_duplicate'], 1)
        self.assertEqual(self.capture.status()['counts']['stock_duplicate'], 1)

    def test_conflicting_image_invalidates_pair_and_preserves_bytes(self):
        self.begin(stage='instant')
        self.capture.on_stock(record())
        self.capture.on_frame(header(), JPEG_DATA)
        conflict = replace(header(), raw_identity=9876)
        self.capture.on_frame(conflict, JPEG_DATA)
        self.assertFalse(self.capture.samples()[0]['trainable'])
        self.assertEqual(self.capture.samples()[0]['pairing_status'], 'image_conflict')
        self.assertTrue((self.capture.path/self.rows('images.jsonl')[-1]['image']).is_file())

    def test_conflicting_stock_invalidates_ambiguous_pair(self):
        self.begin(stage='instant')
        self.capture.on_frame(header(), JPEG_DATA)
        self.capture.on_stock(record())
        self.capture.on_stock(record(raw33=[.8]*33))
        self.assertEqual(self.capture.samples()[0]['pairing_status'], 'ambiguous')
        self.assertFalse(self.capture.samples()[0]['trainable'])

    def test_service_generation_prevents_restart_mix(self):
        self.begin(stage='instant')
        self.capture.on_stock(record(service_generation='bootA:pid2:birth2'))
        self.capture.on_frame(header(), JPEG_DATA)
        self.capture.stop()
        self.assertFalse(self.capture.samples()[0]['trainable'])
        self.assertEqual(self.capture.status()['counts']['stock_without_image'], 1)

    def test_capture_session_prevents_cross_session_mix(self):
        self.begin()
        self.capture.on_stock(record(capture_session_id=9))
        self.capture.on_frame(header(), JPEG_DATA)
        self.assertIsNone(self.capture.samples()[0]['stock_id'])

    def test_missing_generation_never_backfilled_from_stock_or_binding(self):
        self.capture.begin('p', confirmed=True, guide=False)
        self.capture.on_frame(header(), JPEG_DATA)
        self.capture.on_stock(record())
        self.capture.bind_capture(8, 'bootA:pid1:birth1', evidence='arrived after frame')
        self.assertEqual(self.capture.samples()[0]['pairing_status'], 'unproven_generation')
        self.assertIsNone(self.capture.samples()[0]['service_generation'])

    def test_generation_binding_cannot_change(self):
        self.begin()
        with self.assertRaises(ValueError):
            self.capture.bind_capture(8, 'new_generation', evidence='fixture')

    def test_identity_and_exact_sensor_required(self):
        for change in ({'identity_proven': False}, {'identity_evidence': ''},
                       {'source_identity': 9999}, {'mouth_sensor_ns': header().mouth_sensor_ns+1},
                       {'vendor_ns': header().vendor_ns+1}):
            with self.subTest(change=change):
                self.begin(stage='instant')
                self.capture.on_frame(header(), JPEG_DATA)
                self.capture.on_stock(record(**change))
                self.assertFalse(self.capture.samples()[0]['trainable'])
                self.capture.stop()

    def test_no_seq_sensor_join_only_after_proof(self):
        self.begin(stage='instant')
        self.capture.on_frame(header(), JPEG_DATA)
        self.capture.on_stock(record(source_seq=None))
        self.assertIsNone(self.capture.samples()[0]['stock_id'])
        self.capture.on_stock(record(source_seq=None, sensor_identity_proven=True))
        self.assertEqual(self.capture.samples()[0]['pairing_method'], 'verified_sensor')
        self.assertTrue(self.capture.samples()[0]['trainable'])

    def test_native_dataclass_association_and_original_ledger(self):
        from stock import StockRecord
        self.capture.set_label_stage('instant')
        self.capture.begin('p', confirmed=True, guide=False)
        self.capture.bind_capture(8, 99, evidence='native transport metadata')
        h = header()
        native = StockRecord(8, 99, h.source_seq, h.mouth_sensor_ns, [-.1]*33, [.2]*33, [.3]*33,
                             [.9, .1], True, association_valid=True, raw_identity=h.raw_identity,
                             vendor_ns=h.vendor_ns, warmup=False)
        self.capture.on_stock(native)
        self.capture.on_frame(h, JPEG_DATA)
        self.assertEqual(self.capture.samples()[0]['pairing_status'], 'paired')
        self.assertEqual(self.capture.samples()[0]['targets'][:33], [.2]*33)
        self.assertEqual(self.rows('stock.jsonl')[0]['record']['raw_identity'], h.raw_identity)
        self.assertNotIn('source_identity', self.rows('stock.jsonl')[0]['record'])

    def test_native_unassociated_record_never_assumed_proven(self):
        self.begin(stage='instant')
        native = record()
        native.update(association_valid=False, raw_identity=header().raw_identity, valid=True)
        self.capture.on_stock(native)
        self.capture.on_frame(header(), JPEG_DATA)
        self.assertFalse(self.capture.samples()[0]['trainable'])

    def test_sensor_collision_invalidates_previous_pair(self):
        self.begin(stage='instant')
        self.capture.on_frame(header(), JPEG_DATA)
        self.capture.on_stock(record(source_seq=None, sensor_identity_proven=True, source_identity=None))
        self.capture.on_frame(header(2, ns=header().mouth_sensor_ns), JPEG_DATA)
        self.assertTrue(all(r['pairing_status'] == 'ambiguous' for r in self.capture.samples()))
        self.assertTrue(all(not r['trainable'] for r in self.capture.samples()))

    def test_timeout_late_exact_match_retained(self):
        self.begin()
        with patch('face_capture.time.perf_counter_ns', return_value=10):
            self.capture.on_frame(header(), JPEG_DATA)
        with patch('face_capture.time.perf_counter_ns', return_value=3_000_000_010):
            self.capture.on_stock(record())
        self.assertEqual(self.capture.samples()[0]['pairing_status'], 'paired_late')
        self.assertEqual(self.capture.status()['counts']['image_pair_timeout'], 1)

    def test_late_frame_after_stock_timeout(self):
        self.begin()
        with patch('face_capture.time.perf_counter_ns', return_value=10):
            self.capture.on_stock(record())
        with patch('face_capture.time.perf_counter_ns', return_value=3_000_000_010):
            self.capture.on_frame(header(), JPEG_DATA)
        self.assertEqual(self.capture.samples()[0]['pairing_status'], 'paired_late')

    def test_stop_records_gaps_missing_images_missing_stock(self):
        self.begin()
        self.capture.on_frame(header(1), JPEG_DATA)
        self.capture.on_frame(header(4), JPEG_DATA)
        self.capture.on_stock(record(header(3)))
        status = self.capture.stop()
        self.assertEqual(status['counts']['image_without_stock'], 2)
        self.assertEqual(status['counts']['stock_without_image'], 1)
        gap = next(r for r in self.rows('events.jsonl') if r['kind'] == 'source_gap')
        self.assertEqual((gap['first_missing'], gap['last_missing'], gap['count']), (2, 3, 2))
        before = (self.capture.path/'samples.jsonl').read_bytes()
        self.capture.stop()
        self.assertEqual(before, (self.capture.path/'samples.jsonl').read_bytes())

    def test_invalid_warmup_or_nonfinite_stock_never_fabricates_zero(self):
        self.begin(stage='instant')
        for seq, change in enumerate(({'warmup': True}, {'mouth_valid': False}, {'warmup': None}, {'quality2': [float('nan'), .1]}), 1):
            self.capture.on_frame(header(seq), JPEG_DATA)
            self.capture.on_stock(record(header(seq), **change))
        self.assertTrue(all(r['targets'] == [None]*50 for r in self.capture.samples()))

    def test_nonfinite_raw_preserved_named_and_targets_null(self):
        self.begin(stage='instant')
        values = [.1]*33
        values[3] = float('nan')
        values[4] = float('inf')
        self.capture.on_frame(header(), JPEG_DATA)
        self.capture.on_stock(record(raw33=[float('-inf')]+[.1]*32, instant33=values))
        self.capture.stop()
        row = self.rows('samples.jsonl')[0]
        self.assertEqual(row['stock']['raw33'][0], '-Infinity')
        self.assertEqual(row['targets'][3:5], [None, None])
        self.assertEqual(self.rows('stock.jsonl')[0]['record']['instant33'][3], 'NaN')

    def test_review_consent_unknown_and_explicit_zero(self):
        self.begin()
        self.capture.on_frame(header(), JPEG_DATA)
        key = self.capture.samples()[0]['sample_id']
        with self.assertRaises(ValueError):
            self.capture.review([key], {'TongueOut': 0.})
        self.capture.review([key], {'TongueOut': 0., 'TongueLeft': .5}, confirmed=True, reviewer='local-user')
        row = self.capture.samples()[0]
        self.assertEqual(row['targets'][INDEX['TongueOut']], 0.)
        self.assertIsNone(row['targets'][INDEX['TongueRight']])
        self.capture.review([key], {'TongueLeft': None}, confirmed=True, reviewer='local-user')
        self.assertIsNone(self.capture.samples()[0]['targets'][INDEX['TongueLeft']])

    def test_occluded_review_excludes_all_stock_even_late(self):
        self.begin(stage='instant')
        self.capture.on_frame(header(), JPEG_DATA)
        key = self.capture.samples()[0]['sample_id']
        self.capture.review([key], {}, confirmed=True, reviewer='p', observation='occluded')
        self.capture.on_stock(record())
        self.assertEqual(self.capture.samples()[0]['targets'], [None]*50)

    def test_review_after_stop_exports_and_cancel_blocks_late_callbacks(self):
        self.begin()
        self.capture.on_frame(header(), JPEG_DATA)
        old_session = self.capture.session_id
        self.capture.cancel()
        self.assertFalse(self.capture.on_frame(header(2), JPEG_DATA))
        self.assertFalse(self.capture.on_stock(record()))
        self.capture.review(['f000000000'], {'TongueUp': .5}, confirmed=True, reviewer='p')
        _, rows = load_dataset(self.capture.path)
        self.assertEqual(rows[0]['targets'][INDEX['TongueUp']], .5)
        self.begin()
        self.assertNotEqual(old_session, self.capture.session_id)

    def test_end_and_payload_mismatch_do_not_create_images(self):
        self.begin()
        self.assertFalse(self.capture.on_frame(replace(header(), flags=END, payload_bytes=0), b''))
        self.assertFalse(self.capture.on_frame(header(), b'bad'))
        self.assertEqual(self.capture.status()['images'], 0)

    def test_guide_no_frames_cannot_complete_or_create_labels(self):
        self.begin(guide=True)
        self.capture.advance('ready')
        with self.assertRaises(ValueError):
            self.capture.advance('finish')
        self.assertEqual(self.capture.status()['guide']['phase'], 'hold')
        self.assertEqual(self.capture.status()['guide']['completed'], [])

    def test_prompt_proposals_exclude_reaction_tail_and_need_separate_review(self):
        self.begin(guide=True)
        self.capture.advance('ready')
        for seq in range(46):
            self.capture.on_frame(header(seq, ns=1+seq*100_000_000), JPEG_DATA)
        self.capture.advance('finish')
        candidates = [r for r in self.capture.samples() if r.get('prompt_candidate')]
        self.assertTrue(candidates)
        self.assertGreaterEqual(candidates[0]['mouth_sensor_ns'], 1_000_000_001)
        self.assertLessEqual(candidates[-1]['mouth_sensor_ns'], 4_200_000_001)
        self.capture.advance('confirm')
        self.assertTrue(all(r['targets'] == [None]*50 for r in self.capture.samples()))
        page = self.capture.review_candidates(limit=1)
        self.assertEqual(len(page['samples']), 1)
        before = self.capture.status()['pending_reviews']
        self.capture.review([page['samples'][0]['sample_id']], {'TongueOut': 0.}, confirmed=True, reviewer='p')
        self.assertEqual(self.capture.status()['pending_reviews'], before-1)
        self.assertTrue(self.capture.review_candidates(limit=1)['samples'][0]['prompt_candidate']['reviewed'])

    def test_unfinished_manifest_not_exported_as_training_corpus(self):
        self.begin()
        self.capture.on_frame(header(), JPEG_DATA)
        manifest = json.loads((self.capture.path/'manifest.json').read_text(encoding='utf-8'))
        self.assertFalse(manifest['samples_complete'])
        self.assertEqual((self.capture.path/'samples.jsonl').read_text(encoding='utf-8'), '')
        with self.assertRaises(ValueError):
            load_dataset(self.capture.path)
        self.capture.stop()
        manifest, samples = load_dataset(self.capture.path)
        self.assertTrue(manifest['samples_complete'])
        self.assertIsInstance(samples[0]['capture_session_id'], int)
        self.assertIsInstance(samples[0]['source_seq'], int)
        self.assertIsInstance(samples[0]['session_id'], str)

    def test_native_wire_roundtrip_keeps_exact_source_keys(self):
        from stock import StockRecord
        self.capture.set_label_stage('instant')
        self.capture.begin('p', confirmed=True, guide=False)
        self.capture.bind_capture(8, 99, evidence='native transport metadata')
        h = header()
        native = StockRecord(8, 99, h.source_seq, h.mouth_sensor_ns, [-.1]*33, [.2]*33, [.3]*33,
                             [.9, .1], True, association_valid=True, raw_identity=h.raw_identity,
                             vendor_ns=h.vendor_ns, warmup=False)
        self.capture.on_frame(h, JPEG_DATA)
        self.capture.on_stock(StockRecord.unpack(native.pack()))
        self.capture.stop()
        _, samples = load_dataset(self.capture.path)
        self.assertEqual(samples[0]['pairing_status'], 'paired')
        self.assertAlmostEqual(samples[0]['targets'][0], .2, places=6)
        self.assertEqual(samples[0]['stock']['source_identity'], h.raw_identity)

    def test_raw_label_stage_cannot_silently_change_mid_recording(self):
        self.begin()
        with self.assertRaises(ValueError):
            self.capture.set_label_stage('instant')

    def test_refit_recording_under_same_daemon_gets_independent_source_group(self):
        self.begin()
        self.capture.on_frame(header(1), JPEG_DATA)
        self.capture.stop()
        first_manifest, first = load_dataset(self.capture.path)
        self.begin()  # A new recording/fit; transport owner remains unchanged.
        self.capture.on_frame(header(20), JPEG_DATA)
        self.capture.stop()
        second_manifest, second = load_dataset(self.capture.path)
        self.assertEqual(first[0]['capture_session_id'], second[0]['capture_session_id'])
        self.assertEqual(first[0]['service_generation'], second[0]['service_generation'])
        self.assertEqual(first[0]['source_group_id'], first_manifest['source_group_id'])
        self.assertEqual(second[0]['source_group_id'], second_manifest['source_group_id'])
        self.assertNotEqual(first[0]['source_group_id'], second[0]['source_group_id'])
        self.assertIn(first[0]['session_id'], first[0]['source_group_id'])
        self.assertIn(first[0]['fit_id'], first[0]['source_group_id'])
        assert_no_split_leakage({'train': first, 'test': second})

    def test_rerecording_same_frame_does_not_evade_source_identity_check(self):
        self.begin()
        self.capture.on_frame(header(), JPEG_DATA)
        self.capture.stop()
        _, first = load_dataset(self.capture.path)
        self.begin()
        self.capture.on_frame(header(), JPEG_DATA)
        self.capture.stop()
        _, second = load_dataset(self.capture.path)
        with self.assertRaisesRegex(ValueError, 'original_frame'):
            assert_no_split_leakage({'train': first, 'test': second})

    def test_occluded_review_cannot_assert_negative_tongue(self):
        self.begin()
        self.capture.on_frame(header(), JPEG_DATA)
        with self.assertRaises(ValueError):
            self.capture.review(['f000000000'], {'TongueOut': 0.}, confirmed=True,
                                reviewer='p', observation='occluded')

    def test_guide_gap_resets_hold_and_skip_not_completed(self):
        self.begin(guide=True)
        self.capture.advance('ready')
        self.capture.on_frame(header(1, ns=1), JPEG_DATA)
        self.capture.on_frame(header(2, ns=900_000_001), JPEG_DATA)
        self.assertEqual(self.capture.status()['guide']['frames'], 1)
        self.capture.advance('skip')
        self.assertEqual(self.capture.status()['guide']['skipped'], ['neutral'])
        self.assertEqual(self.capture.status()['guide']['completed'], [])


if __name__ == '__main__':
    unittest.main()
