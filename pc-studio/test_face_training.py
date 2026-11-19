"""Synthetic engineering tests, NOT evidence of real-mouth or DINOv2 accuracy."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

import numpy as np
from PIL import Image
import torch
from torch import nn

import face_training as ft
import face_train


class TinyBackbone(nn.Module):
    """Injected only in tests: exercises DINO's token API, not DINO representation quality."""
    def __init__(self, dim=8):
        super().__init__()
        self.stem = nn.Conv2d(3, dim, 1)
        self.blocks = nn.ModuleList([nn.Linear(dim, dim) for _ in range(4)])

    def forward_features(self, frame):
        tokens = nn.functional.adaptive_avg_pool2d(self.stem(frame), (2, 2)).flatten(2).transpose(1, 2)
        for block in self.blocks:
            tokens = torch.tanh(block(tokens))
        return dict(x_norm_clstoken=tokens.mean(1), x_norm_patchtokens=tokens)


def recording_id(person, wear):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, person+'/'+wear))


def make_corpus(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    ft.write_json(root/'manifest.json', dict(schema=ft.SCHEMA, channels=list(ft.CHANNELS50), samples_complete=True))
    rows = []
    for person_index, person in enumerate(('alice', 'bob', 'carol')):
        for session_index, session in enumerate(('wear1', 'wear2')):
            for index in range(4):
                image = f'{person}-{session}-{index}.png'
                Image.fromarray(np.full((40, 40), 30+index*60, np.uint8)).save(root/image)
                targets, sources = [None]*50, [None]*50
                targets[0], sources[0] = float(index % 2), 'human-reviewed'
                # Unknown on alternate training frames, but reviewed on all holdout frames.
                if person != 'alice' or index % 2:
                    targets[1], sources[1] = float(index % 2), 'prompt_reviewed'
                capture = 100+person_index*10+session_index
                row = dict(image=image, person_id=person, session_id=recording_id(person, session),
                           fit_id=recording_id(person, session), capture_session_id=capture,
                           service_generation='synthetic-boot:pid1:birth1', source_identity=index+10000,
                           source_group_id=f'recording:{recording_id(person, session)}:fit:{recording_id(person, session)}',
                           source_seq=index, mouth_sensor_ns=1000+index,
                           targets=targets, target_sources=sources, label_stage='raw_only',
                           stock=dict(raw33=[-.2]*33))
                rows.append(row)
    with (root/'samples.jsonl').open('w', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row)+'\n')
    meta, rows = ft.load_corpus(root)
    split = ft.make_split(rows, validation_groups=['bob'], test_groups=['carol'])
    ft.write_json(root/'split.json', split)
    return meta, rows, split


def synthetic_approval(teacher, meta, rows, split, **thresholds):
    # A deliberately artificial comparator to exercise channel gates only.
    baseline_meta = dict(meta, model_id=meta['model_id']+'-fixture-comparator')
    return ft.validate_candidate(teacher, meta, teacher, baseline_meta, rows, split, **thresholds)


class DataAndSplitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(1)

    def test_contract_and_raw_are_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            _, rows, _ = make_corpus(temp)
            targets, soft = ft.row_targets(rows[0])
            self.assertEqual(targets[0], 0)
            self.assertTrue(torch.isnan(targets[2:]).all())
            self.assertTrue(torch.isnan(soft).all())
            with self.assertRaisesRegex(ValueError, 'not unscaled stock raw'):
                ft.target_vector([-.2]+[None]*49, 'targets')
            with self.assertRaisesRegex(ValueError, 'exact 50-channel'):
                ft.contract(dict(schema=ft.SCHEMA, channels=list(ft.CHANNELS50)[::-1]))

    def test_shared_dataset_completion_and_integer_capture_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            make_corpus(temp)
            root = Path(temp)
            manifest = ft.read_json(root/'manifest.json')
            manifest['samples_complete'] = False
            ft.write_json(root/'manifest.json', manifest)
            with self.assertRaisesRegex(ValueError, 'Stop recording'):
                ft.load_corpus(root)
            manifest['samples_complete'] = True
            ft.write_json(root/'manifest.json', manifest)
            rows = [json.loads(line) for line in (root/'samples.jsonl').read_text().splitlines()]
            rows[0]['capture_session_id'] = '100'
            (root/'samples.jsonl').write_text('\n'.join(json.dumps(row) for row in rows)+'\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'capture_session_id must be an integer'):
                ft.load_corpus(root)

    def test_actual_capture_raw_only_all_unknown_can_be_pseudo_labelled_after_stop(self):
        from face_capture import WholeMouthRecording
        from wire import FrameHeader, JPEG
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stream = io.BytesIO()
            Image.new('L', (400, 400), 100).save(stream, 'JPEG', quality=95)
            jpeg = stream.getvalue()
            for index, person in enumerate(('alice', 'bob', 'carol')):
                recorder = WholeMouthRecording(root/'recordings')
                recorder.begin(person, confirmed=True, guide=False)
                recorder.bind_capture(100+index, 'synthetic-boot:pid1', evidence='Synthetic contract fixture')
                header = FrameHeader(len(jpeg), JPEG, 100+index, 1, 1000, 1001, 1002, 1003, 1004,
                                     1005, 1000, 1000, 1000, 1234)
                recorder.on_frame(header, jpeg)
                with self.assertRaisesRegex(ValueError, 'Stop recording'):
                    ft.load_corpus(recorder.path)
                recorder.stop()
            dm, rows = ft.load_corpus(root/'recordings')
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(row['targets'] == [None]*50 for row in rows))
            split = ft.make_split(rows, validation_groups=['bob'], test_groups=['carol'])
            teacher = ft.Teacher50(TinyBackbone(), embed_dim=8)
            teacher.width = 24
            tm = ft.checkpoint_meta(teacher, ft.CHANNELS50[:1], dict(runs=[]))
            ft.write_json(root/'fixture-approval.json', dict(candidate_model_id=tm['model_id'], partition='validation',
                                                            accepted=True, approved_channels=list(ft.CHANNELS50[:1])))
            ft.predict_pseudo(teacher, tm, rows, split, root/'pseudo', root/'fixture-approval.json', dm)
            pseudo_meta, pseudo = ft.load_corpus(root/'pseudo')
            self.assertTrue(pseudo_meta['samples_complete'])
            for row in pseudo:
                self.assertEqual(row['targets'], [None]*50)
                self.assertIs(type(row['capture_session_id']), int)
                if row['person_id'] == 'alice':
                    self.assertIsNotNone(row['teacher_targets'][0])
                else:
                    self.assertNotIn('teacher_targets', row)

    def test_actual_review_exclusions_cannot_be_revived_by_pseudo_or_online_offline_kd(self):
        from face_capture import WholeMouthRecording
        from wire import FrameHeader, JPEG
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stream = io.BytesIO()
            Image.new('L', (400, 400), 100).save(stream, 'JPEG', quality=95)
            jpeg = stream.getvalue()
            for index, person in enumerate(('alice', 'bob', 'carol')):
                recorder = WholeMouthRecording(root/'recordings')
                recorder.begin(person, confirmed=True, guide=False)
                recorder.bind_capture(100+index, 'synthetic-boot:pid1', evidence='Synthetic contract fixture')
                for seq in range(1, 5):
                    header = FrameHeader(len(jpeg), JPEG, 100+index, seq, seq*1000, seq*1000+1,
                                         1002, 1003, 1004, 1005, seq*1000, seq*1000, seq*1000, seq+1234)
                    recorder.on_frame(header, jpeg)
                if person == 'alice':
                    for sample_index, observation in enumerate(('uncertain', 'occluded', 'no_mouth')):
                        recorder.review([f'f{sample_index:09d}'], {}, confirmed=True,
                                        reviewer='synthetic-reviewer', observation=observation)
                recorder.stop()
            dm, rows = ft.load_corpus(root/'recordings')
            split = ft.make_split(rows, validation_groups=['bob'], test_groups=['carol'])
            teacher = ft.Teacher50(TinyBackbone(), embed_dim=8)
            teacher.width = 24
            tm = ft.checkpoint_meta(teacher, ft.CHANNELS50[:1], dict(runs=[]))
            ft.write_json(root/'fixture-approval.json', dict(candidate_model_id=tm['model_id'], partition='validation',
                                                            accepted=True, approved_channels=list(ft.CHANNELS50[:1])))
            generated = ft.predict_pseudo(teacher, tm, rows, split, root/'pseudo', root/'fixture-approval.json', dm)
            self.assertEqual(generated['pseudo']['review_or_image_excluded_samples'], 3)
            _, pseudo = ft.load_corpus(root/'pseudo')
            alice = [row for row in pseudo if row['person_id'] == 'alice']
            for row in alice[:3]:
                self.assertTrue(row['review_excluded'])
                self.assertNotIn('teacher_targets', row)
                self.assertEqual(row['targets'], [None]*50)
            self.assertIsNotNone(alice[3]['teacher_targets'][0])
            # Even a stale pseudo file cannot revive a subsequently excluded review.
            alice[0]['teacher_targets'] = [1.]*50
            self.assertTrue(torch.isnan(ft.row_targets(alice[0])[1]).all())
            dataset = ft.FaceDataset([alice[0], alice[3]], 24, 24)
            batch = next(iter(torch.utils.data.DataLoader(dataset, batch_size=2)))
            student = ft.Teacher50(TinyBackbone(), embed_dim=8)
            _, _, targets, soft = ft.training_loss(student, batch, teacher, ft.CHANNELS50[:1])
            self.assertTrue(torch.isnan(targets[0]).all())
            self.assertTrue(torch.isnan(soft[0]).all())
            self.assertTrue(torch.isfinite(soft[1, 0]))

    def test_person_split_and_derived_images_cannot_leak(self):
        with tempfile.TemporaryDirectory() as temp:
            _, rows, split = make_corpus(temp)
            self.assertEqual(split['counts'], dict(train=8, validation=8, test=8))
            derived = deepcopy(rows[0])
            derived.update(person_id='renamed', session_id=recording_id('renamed', 'wear1'), capture_session_id=888,
                           _image='elsewhere.jpg', lineage=[dict(person_id='alice',
                                                               session_id=recording_id('alice', 'wear1'),
                                                               source_group_id=rows[0]['source_group_id'])])
            self.assertEqual(ft.partition_for(derived, split), 'train')
            derived['lineage'].append(dict(person_id='bob', session_id=recording_id('bob', 'wear2')))
            with self.assertRaisesRegex(ValueError, 'conflicting split lineage'):
                ft.partition_for(derived, split)
            with self.assertRaisesRegex(ValueError, 'connects validation and test'):
                ft.make_split(rows+[derived], validation_groups=['alice'], test_groups=['bob'])

    def test_session_holdout_is_complete_not_interleaved(self):
        with tempfile.TemporaryDirectory() as temp:
            _, rows, _ = make_corpus(temp)
            rows = [row for row in rows if row['person_id'] == 'alice']
            split = ft.make_split(rows, 'session', validation_groups=['alice/'+recording_id('alice', 'wear2')])
            self.assertEqual(len(ft.select_rows(rows, split, 'train')), 4)
            self.assertEqual(len(ft.select_rows(rows, split, 'validation')), 4)
            with self.assertRaisesRegex(ValueError, 'Not enough independent groups'):
                ft.make_split(rows, 'person')

    def test_new_fits_can_share_camera_owner_without_sharing_original_frames(self):
        with tempfile.TemporaryDirectory() as temp:
            _, rows, _ = make_corpus(temp)
            rows = [row for row in rows if row['person_id'] == 'alice']
            for row in rows:
                row['capture_session_id'] = 100
                row['source_group_id'] = 'recording:'+row['session_id']+':fit:'+row['fit_id']
                if row['session_id'] == recording_id('alice', 'wear2'):
                    row['source_seq'] += 100
                    row['mouth_sensor_ns'] += 1_000_000
                    row['source_identity'] += 1000
            split = ft.make_split(rows, 'session', validation_groups=['alice/'+recording_id('alice', 'wear2')])
            self.assertEqual(split['counts'], dict(train=4, validation=4))

    def test_reserved_ancestor_people_cannot_become_pseudo_train(self):
        with tempfile.TemporaryDirectory() as temp:
            _, rows, split = make_corpus(temp)
            meta = dict(training=dict(runs=[dict(group_by='person', split=split,
                                                train_tokens=ft.training_tokens(rows[:8], 'person'))]))
            ft.assert_train_pool(meta, rows[:8])
            with self.assertRaisesRegex(ValueError, 'ancestor validation/test'):
                ft.assert_train_pool(meta, rows[8:9])
            with self.assertRaisesRegex(ValueError, 'overlaps model training'):
                ft.assert_holdout(meta, rows[:1])
            forged = dict(rows[0], person_id='renamed', session_id=recording_id('renamed', 'wear3'),
                          fit_id=recording_id('renamed', 'wear3'), source_group_id='renamed-source', _image='renamed.jpg')
            with self.assertRaisesRegex(ValueError, 'overlaps model training'):
                ft.assert_holdout(meta, [forged])
            conflicting = dict(forged, person_id='bob', session_id=recording_id('bob', 'wear1'))
            with self.assertRaisesRegex(ValueError, 'conflicting split lineage'):
                ft.partition_for(conflicting, split)

    def test_reviewed_supervision_is_not_overwritten_by_softteacher(self):
        row = dict(targets=[.9]+[None]*49, target_sources=['human']+[None]*49,
                   teacher_targets=[.1, .7]+[None]*48)
        supervised, soft = ft.row_targets(row)
        self.assertAlmostEqual(float(supervised[0]), .9, places=6)
        self.assertTrue(torch.isnan(soft[0]))
        self.assertAlmostEqual(float(soft[1]), .7, places=6)

    def test_masked_loss_has_finite_gradients_and_channel_balancing(self):
        prediction = torch.tensor([[.1, .5, .3], [.3, .5, .8]], requires_grad=True)
        targets = torch.tensor([[0., 1., float('nan')], [0., float('nan'), float('nan')]])
        loss = ft.masked_mse(prediction, targets)
        self.assertAlmostEqual(float(loss.detach()), (.05+.25)/2, places=6)
        loss.backward()
        self.assertTrue(torch.isfinite(prediction.grad).all())
        self.assertTrue((prediction.grad[:, 2] == 0).all())
        unknown = torch.full_like(targets, float('nan'))
        loss = ft.masked_mse(prediction, unknown)
        self.assertEqual(float(loss.detach()), 0)

    def test_source_weights_and_huber_do_not_revive_unknown_targets(self):
        for kind in ('mse', 'huber'):
            prediction = torch.tensor([[.2, .9], [.8, .7]], requires_grad=True)
            targets = torch.tensor([[0., float('nan')], [1., float('nan')]])
            weights = torch.tensor([[.5, 1.], [1., 1.]])
            loss = ft.masked_objective(prediction, targets, weights, kind)
            expected = (.04*.5+.04)/2 if kind == 'mse' else (.15*.5+.15)/2
            self.assertAlmostEqual(float(loss.detach()), expected, places=6)
            loss.backward()
            self.assertTrue(torch.isfinite(prediction.grad).all())
            self.assertEqual(float(prediction.grad[:, 1].abs().sum()), 0.)

    def test_training_augmentation_keeps_labels_and_is_seed_reproducible(self):
        with tempfile.TemporaryDirectory() as temp:
            _, rows, _ = make_corpus(temp)
            row = dict(rows[0], target_weights=[.5]*50)
            plain = ft.FaceDataset([row], 24)[0]
            augmented = ft.FaceDataset([row], 24, augmentation='mild')
            torch.manual_seed(123); first = augmented[0]
            torch.manual_seed(123); second = augmented[0]
            torch.testing.assert_close(first['frame'], second['frame'], rtol=0, atol=0)
            self.assertFalse(torch.equal(plain['frame'], first['frame']))
            torch.testing.assert_close(first['targets'], plain['targets'], equal_nan=True)
            self.assertEqual(first['target_weights'].tolist(), [.5]*50)
            self.assertGreaterEqual(float(first['frame'].min()), 0.)
            self.assertLessEqual(float(first['frame'].max()), 1.)

    def test_reviewed_weak_tier_is_reported_without_counting_it_as_strong_positive(self):
        with tempfile.TemporaryDirectory() as temp:
            _, rows, _ = make_corpus(temp)
            row = deepcopy(rows[0])
            row['targets'], row['target_sources'] = [None]*50, [None]*50
            row['targets'][32], row['target_sources'][32] = .25, 'human-reviewed'
            values = np.zeros((1, 50), np.float32)
            values[0, 32] = .2
            metrics = ft.regression_metrics([row], values)['channels']['TongueOut']['reviewed']
            self.assertEqual(metrics['positive_count'], 0)
            self.assertEqual(metrics['positive_levels']['0.25']['count'], 1)
            self.assertEqual(metrics['positive_levels']['0.25']['recall'], 1.)
            self.assertEqual(metrics['positive_levels']['0.25']['activation_threshold'], .125)

    def test_tongue_response_balance_uses_only_known_train_values_and_keeps_source_weights(self):
        rows = []
        for value in (.05, .05, .7, None):
            targets, sources = [None]*50, [None]*50
            targets[0], sources[0] = 0., 'human-reviewed'
            if value is not None:
                targets[32], sources[32] = value, 'stock_raw'
            rows.append(dict(targets=targets, target_sources=sources))
        original = deepcopy(rows)
        balance = ft.tongue_bin_balance(rows)
        self.assertEqual(balance['counts'], [2, 0, 0, 1])
        np.testing.assert_allclose(balance['factors'], [.75, 0., 0., 1.5])
        self.assertEqual(rows, original)
        class Constant(nn.Module):
            def __init__(self):
                super().__init__()
                self.logits = nn.Parameter(torch.zeros(50))
            def forward(self, frame):
                return self.logits.sigmoid().expand(len(frame), -1)
        model = Constant()
        targets = torch.stack([ft.row_targets(row)[0] for row in rows])
        weights = torch.zeros_like(targets); weights[:, 0] = 1.; weights[:3, 32] = .5
        before_weights = weights.clone()
        batch = dict(frame=torch.zeros(4, 1, 8, 8), targets=targets,
                     soft=torch.full_like(targets, float('nan')), target_weights=weights)
        ft.training_loss(model, batch)[0].backward()
        unbalanced = model.logits.grad.clone()
        model.zero_grad()
        result = ft.training_loss(model, batch, tongue_balance=balance)
        result[0].backward()
        self.assertEqual(float(unbalanced[0]), float(model.logits.grad[0]))
        self.assertNotEqual(float(unbalanced[32]), float(model.logits.grad[32]))
        self.assertTrue(torch.isnan(result[2][3, 32]))
        torch.testing.assert_close(batch['target_weights'], before_weights, rtol=0, atol=0)

    def test_no_missing_dino_path_triggers_hub_or_network(self):
        with patch.object(torch.hub, 'load') as hub:
            with self.assertRaisesRegex(FileNotFoundError, 'no download attempted'):
                ft.local_dino('missing-source', 'missing-checkpoint')
            hub.assert_not_called()


class EvaluationOnlyTests(unittest.TestCase):
    """No optimization/GPU: exercise the production loaders and pre-training gates."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix='mouth50-C-gates-', dir=Path(os.environ['LOCALAPPDATA'])/'Temp/opencode')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.corpus = self.root/'source'
        self.meta, self.rows, self.split = make_corpus(self.corpus)

    def mark_manifest_C(self):
        meta = ft.read_json(self.corpus/'manifest.json')
        meta.update(observability_role='C', evaluation_only=True)
        ft.write_json(self.corpus/'manifest.json', meta)

    def test_loader_refuses_C_and_explicit_readonly_load_preserves_row_markers(self):
        self.mark_manifest_C()
        with self.assertRaisesRegex(ValueError, 'evaluation_only/C'):
            ft.load_corpus(self.corpus)
        meta, rows = ft.load_corpus(self.corpus, allow_evaluation_only=True)
        self.assertTrue(meta['evaluation_only'])
        self.assertTrue(all(r['evaluation_only'] and r['observability_role'] == 'C' for r in rows))

    def test_merge_and_flatten_cannot_remove_restriction(self):
        parent = self.root/'merged'
        make_corpus(parent/'ordinary')
        make_corpus(parent/'C')
        marked = ft.read_json(parent/'C/manifest.json')
        ft.write_json(parent/'C/manifest.json', dict(marked, observability_role='C', evaluation_only=True))
        with self.assertRaises(ValueError):
            ft.load_corpus(parent)
        meta, combined = ft.load_corpus(parent, allow_evaluation_only=True)
        self.assertTrue(meta['evaluation_only'])
        self.assertTrue(any(m['evaluation_only'] for m in meta['source_manifests']))
        flat = self.root/'flat'; flat.mkdir()
        ft.write_json(flat/'manifest.json', dict(schema=ft.SCHEMA, channels=list(ft.CHANNELS50),
                                                samples_complete=True))
        with (flat/'samples.jsonl').open('w') as stream:
            for row in combined:
                stream.write(json.dumps({k: v for k, v in row.items() if not k.startswith('_')})+'\n')
        with self.assertRaisesRegex(ValueError, 'evaluation_only/C'):
            ft.load_corpus(flat)

    def test_lineage_C_survives_copy_even_when_top_flag_is_false(self):
        row = deepcopy(self.rows[0])
        row.update(evaluation_only=False, lineage=[dict(person_id='sealed', session_id='sealed',
                                                       observability_role='C', evaluation_only=True)])
        for action in (lambda: ft.require_development([row]),
                       lambda: ft.FaceDataset([row], 24),
                       lambda: ft.make_split([row])):
            with self.assertRaisesRegex(ValueError, 'evaluation_only/C'):
                action()

    def test_direct_train_KD_pseudo_and_candidate_reject_before_model_or_images(self):
        rows = deepcopy(self.rows); rows[0]['evaluation_only'] = True
        output = self.root/'forbidden.pt'
        with patch.object(ft, 'read_frame', side_effect=AssertionError('image read')), \
             patch.object(torch.optim, 'AdamW', side_effect=AssertionError('optimizer')):
            for action in (
                    lambda: ft.train_model(None, rows, self.split, output, dataset_source=self.corpus),
                    lambda: ft.predict_pseudo(None, {}, rows, self.split, self.root/'pseudo', None,
                                              self.meta),
                    lambda: ft.validate_candidate(None, {}, None, {}, rows, self.split),
                    lambda: ft.evaluate_rows(None, rows)):
                with self.assertRaisesRegex(ValueError, 'evaluation_only/C'):
                    action()
        self.assertFalse(output.exists())
        self.assertFalse((self.root/'pseudo').exists())

    def test_pseudo_rejects_manifest_restriction_even_when_rows_are_flattened(self):
        with self.assertRaisesRegex(ValueError, 'evaluation_only/C'):
            ft.predict_pseudo(None, {}, self.rows, self.split, self.root/'pseudo', None,
                              dict(self.meta, evaluation_only=True))
        self.assertFalse((self.root/'pseudo').exists())

    def test_C_raw_overlay_retains_sticky_row_restriction(self):
        from face_weak_supervision import choose_mapping, derive_row
        source = deepcopy(self.rows[1])
        source.update(evaluation_only=True, observability_role='C')
        result = derive_row(source, choose_mapping([source]))
        self.assertTrue(result['evaluation_only'])
        with self.assertRaisesRegex(ValueError, 'evaluation_only/C'):
            ft.require_development([result])

    def test_cli_default_rejects_all_selection_and_training_commands_before_model_load(self):
        self.mark_manifest_C()
        for command, extras in (
                ('split', []), ('student-baseline', ['--output', str(self.root/'student.pt'), '--random-init']),
                ('predict-pseudo', ['--teacher', 'missing.pt', '--teacher-approval', 'missing.json',
                                    '--output', str(self.root/'pseudo')]),
                ('validate-candidate', ['--candidate', 'missing.pt', '--baseline', 'missing.pt',
                                        '--output', str(self.root/'approval.json')]),
                ('evaluate', ['--checkpoint', 'missing.pt', '--output', str(self.root/'metrics.json')])):
            args = face_train.parser().parse_args([command, '--dataset', str(self.corpus),
                                                   '--split', str(self.corpus/'split.json'), *extras])
            with patch.object(ft, 'load_checkpoint', side_effect=AssertionError('checkpoint read')), \
                 self.assertRaisesRegex(ValueError, 'evaluation_only/C'):
                face_train.run(args)

    def test_explicit_readonly_evaluation_cannot_create_candidate_approval(self):
        self.mark_manifest_C()
        meta, rows = ft.load_corpus(self.corpus, allow_evaluation_only=True)
        args = face_train.parser().parse_args([
            'evaluate', '--dataset', str(self.corpus), '--split', str(self.corpus/'split.json'),
            '--checkpoint', 'fixture.pt', '--output', str(self.root/'readonly.json'),
            '--allow-evaluation-only'])
        with patch.object(ft, 'load_checkpoint', return_value=(object(), dict(model_id='fixture'))), \
             patch.object(ft, 'evaluate_rows', return_value=dict(scope='mock readonly metrics')) as evaluate:
            result = face_train.run(args)
        self.assertTrue(result['evaluation_only'])
        self.assertTrue(evaluate.call_args.kwargs['allow_evaluation_only'])
        self.assertNotIn('approved_channels', result)


class TeacherTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(1)
        torch.manual_seed(17)

    def test_dino_v3_teacher_uses_400_full_frame_and_distinct_checkpoint_kind(self):
        teacher = ft.Teacher50(TinyBackbone(), embed_dim=8, family='dinov3')
        self.assertEqual(teacher.width, 400)
        self.assertEqual(teacher.kind, 'dinov3-vitb16-50')
        self.assertEqual(teacher(torch.rand(1, 1, 400, 400)).shape, (1, 50))
        self.assertEqual(ft.Teacher50(TinyBackbone(), embed_dim=8).width, 392)

    def test_dino_v3_checkpoint_reload_inherits_backbone_kind_and_tuning(self):
        with tempfile.TemporaryDirectory() as temp:
            teacher = ft.Teacher50(TinyBackbone(dim=768), family='dinov3', tune='last-two')
            meta = ft.checkpoint_meta(teacher, ['JawOpen'], dict(runs=[]), tune=teacher.tune)
            path = Path(temp)/'v3.pt'
            ft.save_checkpoint(path, teacher, meta)
            with patch.object(ft, 'local_dino', return_value=TinyBackbone(dim=768)) as factory:
                loaded, loaded_meta = ft.load_checkpoint(path, 'local-v3-source')
            factory.assert_called_once_with('local-v3-source', family='dinov3')
            self.assertEqual(loaded.kind, teacher.kind)
            self.assertEqual(loaded.width, 400)
            self.assertEqual(loaded.tune, 'last-two')
            self.assertEqual(loaded_meta, meta)
            for key, value in teacher.state_dict().items():
                torch.testing.assert_close(loaded.state_dict()[key], value, rtol=0, atol=0)

    def test_dino_v3_local_factory_has_no_download_and_loads_explicit_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'hubconf.py').write_text('# fixture local source', encoding='utf-8')
            original = TinyBackbone()
            torch.save(original.state_dict(), root/'backbone.pt')
            factory = unittest.mock.Mock(return_value=TinyBackbone())
            with patch.object(torch.hub, 'load') as hub, patch.object(
                    ft.importlib, 'import_module', return_value=SimpleNamespace(dinov3_vitb16=factory)) as imported:
                loaded = ft.local_dino(root, root/'backbone.pt', family='dinov3')
            hub.assert_not_called()
            imported.assert_called_once_with('dinov3.hub.backbones')
            factory.assert_called_once_with(pretrained=False)
            self.assertEqual(loaded.source_metadata['family'], 'dinov3')
            for key, value in original.state_dict().items():
                torch.testing.assert_close(loaded.state_dict()[key], value, rtol=0, atol=0)

    def test_large_and_huge_dimensions_and_checkpoint_reload(self):
        for family, dim, kind in (('dinov3-large', 1024, 'dinov3-vitl16-50'),
                                  ('dinov3-huge', 1280, 'dinov3-vith16plus-50')):
            with self.subTest(family=family), tempfile.TemporaryDirectory() as temp:
                teacher = ft.Teacher50(TinyBackbone(dim), family=family, tune='last-two')
                self.assertEqual(teacher.width, 400)
                self.assertEqual(teacher.kind, kind)
                self.assertEqual(teacher.head[1].in_features, dim*2)
                teacher.eval()
                probe = torch.rand(1, 1, 24, 24)
                with torch.inference_mode():
                    expected = teacher(probe)
                self.assertEqual(expected.shape, (1, 50))
                meta = ft.checkpoint_meta(teacher, ['JawOpen'], dict(runs=[]), tune=teacher.tune)
                path = Path(temp)/'teacher.pt'
                ft.save_checkpoint(path, teacher, meta)
                with patch.object(ft, 'local_dino', return_value=TinyBackbone(dim)) as factory:
                    loaded, loaded_meta = ft.load_checkpoint(path, 'source')
                factory.assert_called_once_with('source', family=family)
                self.assertEqual(loaded_meta, meta)
                self.assertEqual(loaded.tune, 'last-two')
                self.assertEqual(loaded.kind, kind)
                with torch.inference_mode():
                    torch.testing.assert_close(loaded(probe), expected, atol=0, rtol=0)

    def test_large_and_huge_dispatch_to_official_factories(self):
        for family, hub_model in (('dinov3-large', 'dinov3_vitl16'), ('dinov3-huge', 'dinov3_vith16plus')):
            with self.subTest(family=family), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root/'hubconf.py').touch()
                factory = unittest.mock.Mock(return_value=TinyBackbone())
                with patch.object(torch.hub, 'load') as hub, patch.object(
                        ft.importlib, 'import_module', return_value=SimpleNamespace(**{hub_model: factory})):
                    backbone = ft.local_dino(root, family=family)
                hub.assert_not_called()
                factory.assert_called_once_with(pretrained=False)
                self.assertEqual(backbone.source_metadata['backbone'], hub_model)

    def test_teacher_cli_selects_v3_explicitly_and_keeps_v2_default(self):
        common = ['teacher-train', '--dataset', 'data', '--split', 'split.json', '--output', 'teacher.pt']
        self.assertEqual(face_train.parser().parse_args(common).teacher_backbone, 'dinov2')
        for family in ('dinov3', 'dinov3-large', 'dinov3-huge'):
            self.assertEqual(face_train.parser().parse_args(common+['--teacher-backbone', family]).teacher_backbone, family)
        self.assertEqual(face_train.parser().parse_args(common).lr, 1e-4)
        student = ['student-baseline','--dataset','data','--split','split.json','--output','student.pt']
        self.assertEqual(face_train.parser().parse_args(student).lr, 1e-3)

    def test_real_optimization_head_frozen_and_last_two_blocks(self):
        teacher = ft.Teacher50(TinyBackbone(), embed_dim=8)
        frame = torch.rand(2, 1, 24, 24)
        targets = torch.zeros(2, 50)
        before = {key: value.clone() for key, value in teacher.backbone.state_dict().items()}
        head_before = teacher.head[1].weight.detach().clone()
        optimizer = torch.optim.AdamW([p for p in teacher.parameters() if p.requires_grad], lr=.01)
        teacher.train()
        loss = ft.masked_mse(teacher(frame), targets)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        self.assertFalse(torch.equal(head_before, teacher.head[1].weight))
        for key, value in teacher.backbone.state_dict().items():
            torch.testing.assert_close(value, before[key], rtol=0, atol=0)
        self.assertFalse(teacher.backbone.training)

        teacher.set_tune('last-two')
        before = {key: value.clone() for key, value in teacher.backbone.state_dict().items()}
        optimizer = torch.optim.AdamW([p for p in teacher.parameters() if p.requires_grad], lr=.001)
        optimizer.zero_grad()
        ft.masked_mse(teacher(frame), targets).backward()
        optimizer.step()
        for key, value in teacher.backbone.state_dict().items():
            if key.startswith(('stem', 'blocks.0.', 'blocks.1.')):
                torch.testing.assert_close(value, before[key], rtol=0, atol=0)
        self.assertFalse(torch.equal(before['blocks.2.weight'], teacher.backbone.blocks[2].weight))
        self.assertFalse(torch.equal(before['blocks.3.weight'], teacher.backbone.blocks[3].weight))
        self.assertFalse(teacher.backbone.blocks[0].training)
        self.assertTrue(teacher.backbone.blocks[-1].training)

    def test_continuous_kd_teacher_is_eval_frozen_and_trusted_targets_win(self):
        student = ft.Teacher50(TinyBackbone(), embed_dim=8)
        teacher = ft.Teacher50(TinyBackbone(), embed_dim=8)
        target = torch.full((2, 50), float('nan'))
        target[:, 0] = .9
        batch = dict(frame=torch.rand(2, 1, 16, 16), teacher_frame=torch.rand(2, 1, 28, 28),
                     targets=target, soft=torch.full_like(target, float('nan')))
        loss, parts, _, soft = ft.training_loss(student, batch, teacher, ft.CHANNELS50[:2])
        loss.backward()
        self.assertGreater(parts['kd'], 0)
        self.assertFalse(teacher.training)
        self.assertTrue(all(not parameter.requires_grad and parameter.grad is None for parameter in teacher.parameters()))
        self.assertTrue(torch.isnan(soft[:, 0]).all())
        self.assertTrue(torch.isfinite(soft[:, 1]).all())
        self.assertTrue(torch.isnan(soft[:, 2:]).all())

    def test_new_channel_zero_only_and_unreviewed_positives_are_not_export_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, rows, split = make_corpus(root/'data')
            for row in rows:
                row['targets'][43], row['target_sources'][43] = 0., 'automatic-neutral'
                row['targets'][44], row['target_sources'][44] = 0., 'human-reviewed'
                row['targets'][45], row['target_sources'][45] = 1., 'prompt_reviewed'
                row['targets'][47], row['target_sources'][47] = 1., 'prompt-unreviewed'
            model = ft.Teacher50(TinyBackbone(), embed_dim=8)
            model.width = 24
            meta = ft.train_model(model, rows, split, root/'test.pt', dataset_source=root/'data',
                                  epochs=1, batch_size=8, max_steps=1)
            self.assertEqual(set(meta['trained_channels']), set(ft.CHANNELS50[:2]) |
                             {ft.CHANNELS50[i] for i in (43, 44, 45, 47)})
            self.assertEqual(set(ft.supported_channels(meta)), set(ft.CHANNELS50[:2]) | {ft.CHANNELS50[45]})
            self.assertEqual(meta['label_coverage'][ft.CHANNELS50[43]]['supervised_count'], 8)
            self.assertEqual(meta['label_coverage'][ft.CHANNELS50[43]]['positive_count'], 0)
            self.assertEqual(meta['label_coverage'][ft.CHANNELS50[44]]['reviewed_positive_count'], 0)
            self.assertEqual(meta['label_coverage'][ft.CHANNELS50[45]]['reviewed_positive_count'], 8)
            self.assertIn(ft.CHANNELS50[47], meta['excluded_channels'])
            manifest = ft.export_onnx(model, meta, root/'new-channels.onnx')
            self.assertEqual(manifest['valid_mask'], 0)
            self.assertEqual(manifest['validated_channels'], [])
            self.assertEqual(manifest['label_coverage'], meta['label_coverage'])

    def test_new_channel_kd_needs_teacher_reviewed_evidence_and_local_positive_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, rows, split = make_corpus(root/'data')
            name = ft.CHANNELS50[45]
            for soft_value, expected_valid in ((0.05, False), (.8, True)):
                teacher = ft.Teacher50(TinyBackbone(), embed_dim=8)
                teacher.width = 24
                with torch.no_grad():
                    teacher.head[-2].weight.zero_()
                    teacher.head[-2].bias.fill_(float(torch.logit(torch.tensor(soft_value))))
                teacher_meta = dict(model_id='synthetic-teacher', training=dict(runs=[]), trained_channels=[name],
                                    positive_supervision_channels=[name])
                model = ft.Teacher50(TinyBackbone(), embed_dim=8)
                model.width = 24
                meta = ft.train_model(model, rows, split, root/f'kd-{soft_value}.pt', dataset_source=root/'data',
                                      teacher=teacher, teacher_meta=teacher_meta, approved=[name],
                                      epochs=1, batch_size=8, max_steps=1)
                self.assertIn(name, meta['trained_channels'])
                self.assertEqual(name in ft.supported_channels(meta), expected_valid)
                self.assertEqual(meta['label_coverage'][name]['supervised_count'], 0)
                self.assertEqual(meta['label_coverage'][name]['kd_count'], 8)

    def test_candidate_requires_reviewed_positives_and_negatives(self):
        with tempfile.TemporaryDirectory() as temp:
            _, rows, split = make_corpus(temp)
            teacher = ft.Teacher50(TinyBackbone(), embed_dim=8)
            meta = ft.checkpoint_meta(teacher, ft.CHANNELS50[:3], dict(runs=[]))
            report = synthetic_approval(teacher, meta, rows, split, min_recall=0, max_fp_rate=1)
            self.assertTrue(report['accepted'])
            self.assertEqual(report['approved_channels'], list(ft.CHANNELS50[:2]))
            self.assertIn('needs reviewed positive', report['rejected_channels'][ft.CHANNELS50[2]])
            self.assertIn('human-reviewed', report['candidate']['by_source'])
            self.assertEqual(set(report['candidate']['by_person']), {'bob'})
            strict = synthetic_approval(teacher, meta, rows, split)
            self.assertFalse(strict['accepted'])
            with self.assertRaisesRegex(ValueError, 'same model'):
                ft.validate_candidate(teacher, meta, teacher, meta, rows, split)

    def test_pseudo_only_train_preserves_labels_and_sealed_test(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dm, rows, split = make_corpus(root/'data')
            teacher = ft.Teacher50(TinyBackbone(), embed_dim=8)
            meta = ft.checkpoint_meta(teacher, ft.CHANNELS50[:2], dict(runs=[]))
            approval = root/'approval.json'
            report = synthetic_approval(teacher, meta, rows, split, min_recall=0, max_fp_rate=1)
            ft.write_json(approval, report)
            result = ft.predict_pseudo(teacher, meta, rows, split, root/'pseudo', approval, dm)
            self.assertEqual(result['pseudo']['accepted_counts'][ft.CHANNELS50[1]], 4)
            _, saved = ft.load_corpus(root/'pseudo')
            for original, row in zip(rows, saved):
                self.assertEqual(original['targets'], row['targets'])
                self.assertEqual(original['target_sources'], row['target_sources'])
                self.assertEqual(Path(row['_image']).read_bytes(), Path(original['_image']).read_bytes())
                self.assertEqual(row['source_group_id'], original['source_group_id'])
                if original['person_id'] != 'alice':
                    self.assertNotIn('teacher_targets', row)
                else:
                    self.assertIsNone(row['teacher_targets'][0])
            self.assertTrue(all(not parameter.requires_grad for parameter in teacher.parameters()))
            with self.assertRaisesRegex(ValueError, 'not an old frozen student'):
                ft.predict_pseudo(ft.Student50(), meta, rows, split, root/'bad', approval)


class EndToEndSyntheticTests(unittest.TestCase):
    def test_student224_checkpoint_export_and_runtime_keep_input_size(self):
        from face_model import FaceModel
        torch.set_num_threads(1)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = ft.Student50(width=224).eval()
            meta = ft.checkpoint_meta(model,[],dict(runs=[]))
            ft.save_checkpoint(root/'student.pt',model,meta)
            loaded,loaded_meta = ft.load_checkpoint(root/'student.pt')
            self.assertEqual(loaded.width,224)
            manifest = ft.export_onnx(loaded,loaded_meta,root/'student.onnx')
            self.assertEqual((manifest['width'],manifest['height']),(224,224))
            runtime = FaceModel(str(root/'student.onnx'),'cpu')
            self.assertEqual(runtime.input_shape,(1,1,224,224))
            self.assertEqual(runtime.predict(np.zeros((400,400),np.uint8))['valid_mask'],0)

    def test_export_preserves_existing_model_or_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            for suffix in ('.onnx', '.json'):
                root = Path(temp)/suffix[1:]
                root.mkdir()
                existing = root/('active'+suffix)
                existing.write_bytes(b'original user artifact')
                with self.assertRaisesRegex(FileExistsError, 'not overwritten'):
                    ft.export_onnx(None, {}, root/'active.onnx')
                self.assertEqual(existing.read_bytes(), b'original user artifact')
                self.assertEqual(list(root.iterdir()), [existing])

    def test_export_rejects_wrong_or_rejected_approval_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for model_id, accepted in (('other', True), ('candidate', False)):
                approval = root/'approval.json'
                ft.write_json(approval, dict(candidate_model_id=model_id, partition='validation',
                                             accepted=accepted, approved_channels=[]))
                with self.assertRaisesRegex(ValueError, 'accepted validation report'):
                    ft.export_onnx(None, dict(model_id='candidate'), root/'new'/'candidate.onnx', approval)
                self.assertFalse((root/'new').exists())

    def test_real_cli_process_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_corpus(root/'data')
            cli = str(Path(face_train.__file__).resolve())
            common = ['--dataset', str(root/'data'), '--split', str(root/'cli-split.json')]
            commands = [
                (['split', *common, '--validation-group', 'bob', '--test-group', 'carol'], 0),
                (['student-baseline', *common, '--random-init', '--epochs', '1', '--max-steps', '1',
                  '--batch-size', '8', '--output', str(root/'cli.pt')], 0),
                (['export', '--checkpoint', str(root/'cli.pt'), '--output', str(root/'cli.onnx')], 0),
                (['evaluate', *common, '--checkpoint', str(root/'cli.pt'), '--partition', 'test',
                  '--output', str(root/'test.json')], 0),
                (['teacher-train', *common, '--dino-repo', str(root/'missing-dino'),
                  '--backbone-checkpoint', str(root/'missing.pt'), '--output', str(root/'teacher.pt')], 1),
                (['validate-candidate', *common, '--candidate', str(root/'cli.pt'), '--baseline', str(root/'cli.pt'),
                  '--output', str(root/'rejected.json')], 1),
            ]
            results = []
            environment = dict(os.environ, OMP_NUM_THREADS='1', MKL_NUM_THREADS='1')
            for arguments, expected in commands:
                start = time.perf_counter()
                child = subprocess.run([sys.executable, '-B', '-X', 'utf8', cli, *arguments],
                                       capture_output=True, text=True, encoding='utf-8', env=environment, timeout=45)
                self.assertEqual(child.returncode, expected, child.stdout+'\n'+child.stderr)
                result = dict(command=arguments[0], returncode=child.returncode,
                              seconds=time.perf_counter()-start)
                if expected == 0:
                    event = json.loads(child.stdout.splitlines()[-1])
                    self.assertEqual(event['event'], 'result')
                else:
                    result['error'] = child.stderr.strip()
                results.append(result)
            self.assertFalse((root/'teacher.pt').exists())
            self.assertFalse((root/'rejected.json').exists())
            meta = ft.read_json(root/'cli.json')
            self.assertEqual(meta['valid_mask'], 0)
            self.assertEqual(meta['trained_channels'], list(ft.CHANNELS50[:2]))
            evidence = os.environ.get('FACE_TRAINING_EVIDENCE')
            if evidence:
                ft.write_json(Path(evidence)/'cli-smoke.json', dict(scope='Synthetic CLI subprocesses on CPU',
                                                                  commands=results, onnx_check=meta['numerical_check']))

    def test_real_mnv3_baseline_pseudo_distill_adapt_export_ort(self):
        """Short actual torch steps and ONNX/ORT CPU; no production/individual data."""
        torch.set_num_threads(1)
        torch.manual_seed(41)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dm, rows, split = make_corpus(root/'data')
            args = ['--dataset', str(root/'data'), '--split', str(root/'data/split.json'),
                    '--epochs', '1', '--batch-size', '2', '--max-steps', '2']
            baseline_meta = face_train.run(face_train.parser().parse_args(
                ['student-baseline', *args, '--random-init', '--output', str(root/'baseline.pt')]))
            self.assertEqual(baseline_meta['trained_channels'], list(ft.CHANNELS50[:2]))
            self.assertEqual(baseline_meta['optimizer_updates'], 2)
            baseline, _ = ft.load_checkpoint(root/'baseline.pt')
            initial_head = baseline.head[0].weight.detach().clone()
            self.assertTrue(all(not module.training for module in baseline.features.modules()
                                if isinstance(module, nn.BatchNorm2d)))

            # Injected tiny DINO-API backbone: real head training, no full DINO weights.
            teacher = ft.Teacher50(TinyBackbone(), embed_dim=8)
            tm = ft.train_model(teacher, rows, split, root/'injected-teacher.pt', dataset_source=root/'data',
                                epochs=1, batch_size=2, max_steps=2, mode='synthetic-injected-teacher')
            # Permissive fixture gates exercise plumbing, not a model-effect claim.
            approval = synthetic_approval(teacher, tm, rows, split, min_recall=0, max_fp_rate=1)
            ft.write_json(root/'approval.json', approval)
            ft.predict_pseudo(teacher, tm, rows, split, root/'pseudo', root/'approval.json', dm)
            common = ['--dataset', str(root/'pseudo'), '--split', str(root/'data/split.json'),
                      '--epochs', '1', '--batch-size', '2', '--max-steps', '2']
            distilled_meta = face_train.run(face_train.parser().parse_args(
                ['student-distill', *common, '--initial', str(root/'baseline.pt'), '--output', str(root/'distilled.pt')]))
            self.assertGreater(sum(distilled_meta['kd_counts']), 0)
            distilled, _ = ft.load_checkpoint(root/'distilled.pt')
            self.assertFalse(torch.equal(initial_head, distilled.head[0].weight))
            frozen_features = {key: value.clone() for key, value in distilled.features.state_dict().items()}
            adapted_meta = face_train.run(face_train.parser().parse_args(
                ['local-adapt', *common, '--initial', str(root/'distilled.pt'), '--output', str(root/'personal.pt')]))
            adapted, loaded_meta = ft.load_checkpoint(root/'personal.pt')
            for key, value in adapted.features.state_dict().items():
                torch.testing.assert_close(value, frozen_features[key], rtol=0, atol=0)
            manifest = ft.export_onnx(adapted, loaded_meta, root/'personal.onnx')
            self.assertEqual(manifest['width'], 160)
            self.assertEqual(manifest['valid_mask'], 0)
            self.assertEqual(manifest['trained_channels'], list(ft.CHANNELS50[:2]))
            self.assertEqual(len(manifest['numerical_check']['per_channel_max_abs']), 50)
            self.assertLess(manifest['numerical_check']['max_abs'], 1e-5)
            self.assertEqual(ft.read_json(root/'personal.json'), manifest)
            self.assertEqual(len(adapted_meta['training']['runs']), 4)
            held = ft.select_rows(rows, split, 'test')
            ft.assert_holdout(loaded_meta, held)
            sealed_metrics = ft.evaluate_rows(adapted, held)
            evidence = os.environ.get('FACE_TRAINING_EVIDENCE')
            if evidence:
                import torchvision
                import onnx
                import onnxruntime
                ft.write_json(Path(evidence)/'synthetic-smoke.json',
                              dict(scope='Synthetic engineering only; injected tiny backbone, full DINOv2 NOT trained',
                                   environment=dict(torch=torch.__version__, torchvision=torchvision.__version__,
                                                    onnx=onnx.__version__, onnxruntime=onnxruntime.__version__),
                                   baseline_steps=baseline_meta['steps'], teacher_steps=tm['steps'],
                                   distill_steps=distilled_meta['steps'], distill_kd_counts=distilled_meta['kd_counts'],
                                   adapt_steps=adapted_meta['steps'], encoder_frozen_exact=True,
                                    export=manifest, sealed_synthetic_test=sealed_metrics))

    def test_export_output_permission_requires_exact_model_approval(self):
        from face_model import FaceModel
        torch.set_num_threads(1)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = ft.Student50().eval()
            meta = ft.checkpoint_meta(model, ['JawOpen', 'TongueOut'], dict(runs=[]))
            approval = root/'approval.json'
            ft.write_json(approval, dict(candidate_model_id=meta['model_id'], partition='validation',
                                         accepted=True, approved_channels=['JawOpen']))
            manifest = ft.export_onnx(model, meta, root/'approved.onnx', approval)
            self.assertEqual(manifest['trained_channels'], ['JawOpen', 'TongueOut'])
            self.assertEqual(manifest['validated_channels'], ['JawOpen'])
            self.assertEqual(manifest['valid_mask'], 1 << 5)
            runtime = FaceModel(str(root/'approved.onnx'), 'cpu')
            self.assertEqual(set(runtime.predict(np.zeros((400, 400), np.uint8))['channels']), {'JawOpen'})


if __name__ == '__main__':
    unittest.main()
