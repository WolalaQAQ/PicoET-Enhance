"""Training mechanics and export tests; fixture acceptance is not a personal-effect claim."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
import torch

from model import Segment, to_input
from personal_training import (TrainingCancelled, automatic_mask, better_than, export_candidate,
                               largest_blob, load_network, loss_function, split_samples, train_head)


def training_samples():
    rows = []
    for label in range(1, 6):
        for index in range(16):
            frame = np.full((200, 200), 50, np.uint8)
            mask = np.zeros((56, 56), bool)
            if label >= 3:
                frame[100:150, 100:150] = 140
                mask[28:42, 28:42] = True
            rows.append(dict(frame=frame, mask=mask, label=label, raw_angle=.66,
                             area=int(mask.sum()), session_id='test-only', source_seq=str(label*100+index), sensor_ns=str(index)))
    return rows


class TrainingTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(1)
        work = Path(__file__).resolve().parent/'tests-work'
        work.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.checkpoint = self.root/'standard.pt'
        self.network = Segment().eval()
        # Keep export masks far from the threshold in this controlled fixture.
        with torch.no_grad():
            self.network.head[-1].weight.zero_()
            self.network.head[-1].bias.fill_(-2.)
        torch.save(self.network.state_dict(), self.checkpoint)

    def test_largest_blob_is_four_connected_and_does_not_wrap_rows(self):
        mask = np.zeros((56, 56), bool)
        mask[10:13, 10:13] = True
        mask[0, 55] = mask[1, 0] = True
        result = largest_blob(mask)
        self.assertEqual(int(result.sum()), 9)
        self.assertFalse(result[0, 55])
        self.assertFalse(largest_blob(np.zeros_like(mask)).any())

    def test_automatic_mask_merges_model_mask_and_ignores_glare(self):
        sample = training_samples()[32]
        result = automatic_mask(sample, np.full((200, 200), 50, np.float32))
        self.assertGreater(int(result.sum()), 180)
        glare = dict(sample, frame=np.full((200, 200), 255, np.uint8), mask=np.zeros((56, 56), bool))
        self.assertFalse(automatic_mask(glare, np.zeros((200, 200), np.float32)).any())

    def test_every_fourth_frame_per_pose_is_held_out_and_not_trained(self):
        train, held = split_samples(training_samples())
        self.assertEqual((len(train), len(held)), (60, 20))
        self.assertTrue(set(s['source_seq'] for s in train).isdisjoint(s['source_seq'] for s in held))
        self.assertEqual([sum(s['label'] == label for s in held) for label in range(1, 6)], [4]*5)

    def test_loss_positive_weight_dice_and_finite_gradient(self):
        logits = torch.zeros((1, 1, 2, 2), requires_grad=True)
        target = torch.tensor([[[[1., 0.], [0., 0.]]]])
        loss = loss_function(logits, target)
        expected = torch.log(torch.tensor(2.))*1.5+.5
        self.assertAlmostEqual(float(loss.detach()), float(expected), places=6)
        loss.backward()
        self.assertTrue(torch.isfinite(logits.grad).all())
        self.assertLess(float(logits.grad[0, 0, 0, 0]), 0.)

    def test_freeze_keeps_encoder_weights_and_batchnorm_buffers_unchanged(self):
        network = load_network(self.checkpoint)
        before = {k: v.clone() for k, v in network.encoder.state_dict().items()}
        self.assertEqual(sum(p.numel() for p in network.head.parameters()), 3777)
        self.assertTrue(all(not p.requires_grad for p in network.encoder.parameters()))
        optimizer = torch.optim.Adam(network.head.parameters(), lr=.001)
        loss = loss_function(network(to_input(torch.full((1, 200, 200), 100, dtype=torch.uint8))),
                             torch.ones((1, 1, 56, 56)))
        loss.backward(); optimizer.step()
        self.assertTrue(all(torch.equal(value, before[key]) for key, value in network.encoder.state_dict().items()))
        self.assertTrue(all(p.grad is None for p in network.encoder.parameters()))
        self.assertFalse(torch.equal(network.head[-1].bias, self.network.head[-1].bias))

    def test_acceptance_requires_direction_score_and_fp_tolerance(self):
        before = dict(score=.5, fp=.1)
        self.assertTrue(better_than(before, dict(score=.56, fp=.12, direction=True)))
        for row in (dict(score=.54, fp=.1, direction=True), dict(score=.6, fp=.121, direction=True),
                    dict(score=.8, fp=.1, direction=False)):
            self.assertFalse(better_than(before, row))

    def test_rejected_training_records_consent_samples_without_export_or_standard_overwrite(self):
        original = self.checkpoint.read_bytes()
        progress = []
        result = train_head(training_samples(), self.checkpoint, self.checkpoint, self.root,
                            threading.Event(), progress.append, steps=2)
        self.assertFalse(result['accepted'])
        self.assertTrue((self.root/'recording.npz').exists())
        self.assertFalse((self.root/'personal.pt').exists())
        self.assertEqual(self.checkpoint.read_bytes(), original)
        self.assertEqual(json.loads((self.root/'training.json').read_text())['steps'], 2)
        self.assertTrue(progress)

    def test_accepted_branch_exports_real_onnx_and_preserves_standard(self):
        rows = training_samples()
        before = self.checkpoint.read_bytes()
        metrics = [dict(score=0., fp=0., direction=False), dict(score=1., fp=0., direction=True), {}]
        calibrated = [dict(s, raw_angle={3: .66, 4: .33, 5: 1.11}.get(s['label']), area=200 if s['label'] >= 3 else 0) for s in rows]
        # Acceptance is controlled here; the exported graph and CPU ORT comparisons are real.
        with patch('personal_training.evaluate', side_effect=[(metrics[0], []), (metrics[1], []), ({}, calibrated)]):
            result = train_head(rows, self.checkpoint, self.checkpoint, self.root,
                                threading.Event(), lambda value: None, steps=1)
        self.assertTrue(result['accepted'])
        self.assertTrue((self.root/'personal.onnx').is_file())
        self.assertEqual(self.checkpoint.read_bytes(), before)
        personal = load_network(result['checkpoint'])
        self.assertTrue(all(torch.equal(v, self.network.encoder.state_dict()[k]) for k, v in personal.encoder.state_dict().items()))

    def test_cancel_before_record_or_export_and_during_training(self):
        cancelled = threading.Event(); cancelled.set()
        with self.assertRaises(TrainingCancelled):
            train_head(training_samples(), self.checkpoint, self.checkpoint, self.root, cancelled, lambda value: None)
        self.assertFalse((self.root/'recording.npz').exists())
        with self.assertRaises(TrainingCancelled):
            export_candidate(self.network, self.root/'cancelled.pt', [], cancelled)
        self.assertFalse((self.root/'cancelled.pt').exists())
        cancelled.clear()
        def progress(value):
            if value > .1:
                cancelled.set()
        with self.assertRaises(TrainingCancelled):
            train_head(training_samples(), self.checkpoint, self.checkpoint, self.root, cancelled, progress, steps=20)
        self.assertFalse((self.root/'personal.pt').exists())

    def test_insufficient_data_keeps_model_inactive(self):
        with self.assertRaisesRegex(ValueError, 'Not enough open-mouth'):
            train_head(training_samples()[:8], self.checkpoint, self.checkpoint, self.root,
                       threading.Event(), lambda value: None, steps=1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
