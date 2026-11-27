"""Unit tests for v3 de-binarized losses, scorer, StatefulGRUStudent50, and ONNX opset-17 export/parity."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import onnx
import onnxruntime as ort
from PIL import Image
import torch

import face_schema
import mouth50_trainer as trainer
from mouth50_trainer import augment, eval_continuous, label_rules, pipeline
from mouth50_trainer.losses import (
    build_soft_kd_weights,
    clip_temporal_loss,
    debinarized_continuous_loss,
    score_val_continuous_v2,
    smooth_continuous_loss,
)
from mouth50_trainer.models import (
    CHANNELS, StatefulGRUStudent50, VFTStudent50, VFTTeacher50, save_teacher_delta, teacher_from_saved,
)


def setUpModule():
    threads = torch.get_num_threads()
    unittest.addModuleCleanup(torch.set_num_threads, threads)
    torch.set_num_threads(1)


class V3LossAndGRUTests(unittest.TestCase):
    def test_debinarized_loss_zero_weight_tilt_bias_around_030(self):
        """Uniform noise around t=0.30 must converge strictly to 0.300 +/- 0.005 (no pos_boost tilt)."""
        torch.manual_seed(123)
        n = 4096
        # Targets uniformly distributed in [0.15, 0.45] -> exact mean 0.300 inside mid bin
        t = 0.15 + 0.30 * torch.rand(n, 1)
        w = torch.ones(n, 1)

        z_debin = torch.zeros(1, 1, requires_grad=True)
        opt = torch.optim.Adam([z_debin], lr=0.05)
        for _ in range(300):
            opt.zero_grad()
            loss = debinarized_continuous_loss(z_debin.expand(n, 1), t, w, logit_reg_w=2e-4)
            loss.backward()
            opt.step()
        p_debin = float(torch.sigmoid(z_debin).item())
        self.assertAlmostEqual(p_debin, 0.300, delta=0.005, msg=f"debinarized optimum tilted: {p_debin}")

        # Contrast: legacy smooth_continuous_loss with pos_boost=6.0 tilts upward significantly
        z_old = torch.zeros(1, 1, requires_grad=True)
        opt_old = torch.optim.Adam([z_old], lr=0.05)
        pos_boost = torch.tensor([6.0])
        for _ in range(300):
            opt_old.zero_grad()
            loss_old = smooth_continuous_loss(z_old.expand(n, 1), t, w, pos_boost=pos_boost)
            loss_old.backward()
            opt_old.step()
        p_old = float(torch.sigmoid(z_old).item())
        self.assertGreater(p_old - p_debin, 0.015, msg=f"Expected legacy pos_boost tilt, got old={p_old} vs debin={p_debin}")

    def test_debinarized_loss_weight_scale_invariance_and_logit_reg(self):
        torch.manual_seed(7)
        logits = torch.randn(32, 50, requires_grad=True)
        # Mix across all three bins (zero, mid, high)
        target = torch.rand(32, 50)
        target[target < 0.25] = 0.0
        target[target > 0.80] = 0.90
        weight = torch.rand(32, 50) * (torch.rand(32, 50) > 0.3).float()

        l1 = debinarized_continuous_loss(logits, target, weight, logit_reg_w=2e-4)
        g1 = torch.autograd.grad(l1, logits, retain_graph=True)[0]

        for scale in (0.2, 3.7, 50.0):
            l_s = debinarized_continuous_loss(logits, target, weight * scale, logit_reg_w=2e-4)
            g_s = torch.autograd.grad(l_s, logits, retain_graph=True)[0]
            self.assertAlmostEqual(float(l1.item()), float(l_s.item()), delta=1e-6)
            self.assertLess(float((g1 - g_s).abs().max().item()), 1e-6)

        # Logit regularizer is zero inside [-4.5, 4.5] and positive outside
        z_in = torch.full((4, 5), 4.0)
        z_out = torch.full((4, 5), 6.0)
        t_hi = torch.ones(4, 5)
        w_one = torch.ones(4, 5)
        self.assertAlmostEqual(
            float(debinarized_continuous_loss(z_in, t_hi, w_one, logit_reg_w=2e-4).item()),
            float(debinarized_continuous_loss(z_in, t_hi, w_one, logit_reg_w=0.0).item()),
            delta=1e-7,
        )
        self.assertGreater(
            float(debinarized_continuous_loss(z_out, t_hi, w_one, logit_reg_w=2e-4).item()),
            float(debinarized_continuous_loss(z_out, t_hi, w_one, logit_reg_w=0.0).item()),
        )

    def test_build_soft_kd_weights_contract(self):
        w_b = torch.tensor([[0.0, 0.35, 0.65, 0.80, 1.80, 0.90]])
        t_b = torch.tensor([[0.0, 0.0, 0.40, 0.01, 0.85, 0.0]])
        sm_b = torch.tensor([[1.0, 1.0, 1.0, 1.0, 1.0, 0.0]])
        kd_conf = torch.tensor([[0.8, 0.6, 0.5, 0.9, 0.7, 1.0]])

        kw_no_conf = build_soft_kd_weights(w_b, t_b, sm_b)
        # w=0 -> 1.0; w=0.35,t=0 -> NOT strict zero (0.50); w=0.65 -> 0.50; w=0.80,t=0.01 -> strict zero (0.0); w=1.80,t=0.85 -> 0.15; sm=0 -> 0.0
        expected_no_conf = [1.0, 0.50, 0.50, 0.0, 0.15, 0.0]
        for got, exp in zip(kw_no_conf[0].tolist(), expected_no_conf):
            self.assertAlmostEqual(got, exp, delta=1e-6)

        kw_conf = build_soft_kd_weights(w_b, t_b, sm_b, kd_conf=kd_conf)
        for got, exp in zip(kw_conf[0].tolist(), [e * c for e, c in zip(expected_no_conf, kd_conf[0].tolist())]):
            self.assertAlmostEqual(got, exp, delta=1e-6)

    def test_clip_temporal_loss_ramp_cliff_and_reset_mask(self):
        T = 10
        ramp = torch.linspace(0.0, 0.9, T).view(1, T, 1).expand(1, T, 50).clone()
        w = torch.ones(1, T, 50)
        # Exact linear ramp matching target has zero velocity, zero acceleration, zero monotonicity error
        loss_exact = clip_temporal_loss(ramp, ramp, w)
        self.assertAlmostEqual(float(loss_exact.item()), 0.0, delta=1e-7)

        # Step cliff incurs acceleration + velocity penalty
        cliff = torch.where(ramp >= 0.45, torch.full_like(ramp, 0.9), torch.zeros_like(ramp))
        loss_cliff = clip_temporal_loss(cliff, ramp, w)
        self.assertGreater(float(loss_cliff.item()), 0.02)

        # Reversed prediction on rising ramp incurs monotonicity penalty
        rev = torch.linspace(0.9, 0.0, T).view(1, T, 1).expand(1, T, 50).clone()
        loss_mono = clip_temporal_loss(rev, ramp, w, vel_w=0.0, acc_w=0.0, mono_w=1.0)
        self.assertGreater(float(loss_mono.item()), 0.05)

        # Reset mask at a discontinuity suppresses velocity/acceleration across that reset step
        t_discont = torch.cat([torch.zeros(1, 5, 50), torch.full((1, 5, 50), 0.8)], dim=1)
        rmask = torch.zeros(1, 10, dtype=torch.bool)
        rmask[0, 5] = True
        loss_masked = clip_temporal_loss(t_discont, t_discont, w, reset_mask=rmask)
        self.assertAlmostEqual(float(loss_masked.item()), 0.0, delta=1e-7)

    def test_score_val_continuous_v2_macro_mid_and_diagnostics(self):
        idx_jo = CHANNELS.index("JawOpen")
        idx_mp = CHANNELS.index("MouthPucker")
        idx_cpl = CHANNELS.index("CheekPuffLeft")
        idx_tl = CHANNELS.index("TongueLeft")
        idx_tr = CHANNELS.index("TongueRight")
        idx_to = CHANNELS.index("TongueOut")

        val_rows = []
        preds = []
        # 20 JawOpen mid rows with 0.01 error, 2 MouthPucker mid rows with 0.21 error -> macro mid_mae = 0.11 (not ~0.028 micro)
        for _ in range(20):
            t = [None] * 50
            w = [0.0] * 50
            t[idx_jo], w[idx_jo] = 0.40, 0.65
            val_rows.append({"prompt_id": "JawOpen", "phase": "transition", "targets": t, "weights": w})
            p = np.zeros(50, dtype=np.float32)
            p[idx_jo] = 0.41
            preds.append(p)
        for _ in range(2):
            t = [None] * 50
            w = [0.0] * 50
            t[idx_mp], w[idx_mp] = 0.40, 0.65
            val_rows.append({"prompt_id": "MouthPucker", "phase": "transition", "targets": t, "weights": w})
            p = np.zeros(50, dtype=np.float32)
            p[idx_mp] = 0.61
            preds.append(p)
        # Smile hold with CheekPuffLeft leak = 0.25
        for pid in ("MouthSmileL", "MouthSmileR", "negative_teeth"):
            t = [None] * 50
            w = [0.0] * 50
            t[idx_cpl], w[idx_cpl] = 0.0, 1.0
            val_rows.append({"prompt_id": pid, "phase": "hold", "targets": t, "weights": w})
            p = np.zeros(50, dtype=np.float32)
            p[idx_cpl] = 0.25
            preds.append(p)
        # TongueLeft / TongueRight holds
        for pid, idx_ch, val_ch, val_to in (("TongueLeft", idx_tl, 0.72, 0.60), ("TongueRight", idx_tr, 0.68, 0.64)):
            t = [None] * 50
            w = [0.0] * 50
            t[idx_ch], w[idx_ch] = 0.80, 2.0
            val_rows.append({"prompt_id": pid, "phase": "hold", "targets": t, "weights": w})
            p = np.zeros(50, dtype=np.float32)
            p[idx_ch] = val_ch
            p[idx_to] = val_to
            preds.append(p)

        pv = np.stack(preds, axis=0)
        sc = score_val_continuous_v2(pv, val_rows)
        self.assertAlmostEqual(sc["mid_mae_macro"], 0.11, delta=1e-4)
        self.assertGreater(sc["mid_mae_macro"], sc["mid_mae"] * 2.5)
        self.assertAlmostEqual(sc["smile_puff_l_leak"], 0.25, delta=1e-5)
        self.assertAlmostEqual(sc["tongue_left_med"], 0.72, delta=1e-5)
        self.assertAlmostEqual(sc["tongue_right_med"], 0.68, delta=1e-5)
        self.assertAlmostEqual(sc["tongue_turn_to_med"], 0.62, delta=1e-5)
        self.assertEqual(sc["mid_ch_count"], 2)

    def test_stateful_gru_step0_equivalence_and_sequence_consistency(self):
        torch.manual_seed(42)
        sf = VFTStudent50(arch="small", net_width=384, head_kind="pyramid", roi_end=400, load_pretrained=False).eval()
        gru = StatefulGRUStudent50(arch="small", net_width=384, head_kind="pyramid", roi_end=400, hid_dim=256, load_pretrained=False).eval()
        gru.load_from_single_frame(sf.state_dict())

        x = torch.rand(2, 1, 400, 400)
        h0 = torch.zeros(2, 256)
        with torch.inference_mode():
            y_sf = sf(x)
            y_gru, h1 = gru.forward_step(x, h0)
            self.assertLess(float((y_sf - y_gru).abs().max().item()), 1e-6)
            self.assertEqual(tuple(h1.shape), (2, 256))

            # Now set non-zero temp_head and verify forward_sequence_logits == frame-by-frame forward_step_logits
            torch.nn.init.normal_(gru.temp_head.weight, std=0.1)
            x_seq = torch.rand(2, 6, 1, 400, 400)
            rmask = torch.zeros(2, 6, dtype=torch.bool)
            rmask[0, 3] = True
            rmask[1, 4] = True
            logits_seq, h_last = gru.forward_sequence_logits(x_seq, h0=None, reset_mask=rmask)

            h_cur = torch.zeros(2, 256)
            step_logits = []
            for t in range(6):
                h_cur = torch.where(rmask[:, t].unsqueeze(-1), torch.zeros_like(h_cur), h_cur)
                lt, h_cur = gru.forward_step_logits(x_seq[:, t], h_cur)
                step_logits.append(lt)
            logits_ref = torch.stack(step_logits, dim=1)
            self.assertLess(float((logits_seq - logits_ref).abs().max().item()), 1e-5)
            self.assertLess(float((h_last - h_cur).abs().max().item()), 1e-5)

    def test_stateful_gru_onnx_opset17_export_and_cpu_cuda_parity(self):
        torch.manual_seed(99)
        gru = StatefulGRUStudent50(arch="small", net_width=384, head_kind="pyramid", roi_end=400, hid_dim=256, load_pretrained=False).eval()
        # Activate temp_head so h_in -> h_out -> blendshapes path is genuinely exercised
        with torch.no_grad():
            torch.nn.init.uniform_(gru.temp_head.weight, -0.05, 0.05)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_onnx = Path(tmpdir) / "vft_student_50_gru256.onnx"
            manifest = pipeline.export_stateful_gru_onnx(
                gru, out_onnx, list(CHANNELS), note="unit-test synthetic stateful gru"
            )
            self.assertTrue(out_onnx.is_file())
            self.assertTrue(out_onnx.with_suffix(".json").is_file())
            self.assertEqual(manifest["temporal_kind"], "gru_stateful")
            self.assertEqual(manifest["state_input_name"], "h_in")
            self.assertEqual(manifest["state_output_name"], "h_out")
            self.assertEqual(manifest["state_shape"], [1, 256])
            self.assertEqual(manifest["temporal"]["state_shape"], [1, 256])
            self.assertEqual(manifest["valid_mask"], 0)
            self.assertLess(manifest["max_onnx_abs_diff"], 1e-5)
            self.assertLess(manifest["max_onnx_state_diff"], 1e-5)
            if manifest["max_onnx_cuda_abs_diff"] is not None:
                self.assertLess(manifest["max_onnx_cuda_abs_diff"], 1e-5)
                self.assertLess(manifest["max_onnx_cuda_state_diff"], 1e-5)

            # Verify no ONNX GRU op was emitted
            omodel = onnx.load(str(out_onnx))
            op_types = {n.op_type for n in omodel.graph.node}
            self.assertNotIn("GRU", op_types)

            # Quick CUDA Graph + CPU 1-thread benchmark smoke test
            bench = pipeline.benchmark_stateful_onnx(out_onnx, hid_dim=256, n_warmup=3, n_iters=5)
            self.assertIn("cpu_1t_ms", bench)
            if "CUDAExecutionProvider" in ort.get_available_providers():
                self.assertIn("cuda_graph_ms", bench, msg=f"CUDA Graph failed: {bench}")

            session = ort.InferenceSession(str(out_onnx), providers=["CPUExecutionProvider"])
            self.assertEqual([i.name for i in session.get_inputs()], ["frame", "h_in"])
            self.assertEqual([o.name for o in session.get_outputs()], ["blendshapes", "h_out"])
            frame = np.full((400, 400), 127, dtype=np.uint8)
            x_np = frame[None, None].astype(np.float32) / 255.0
            with torch.inference_mode():
                x = torch.from_numpy(x_np)
                _, h = gru.forward_step(x, torch.zeros(1, 256))
                _, h_next = gru.forward_step(x, h)
            _, h1 = session.run(["blendshapes", "h_out"], {"frame": x_np, "h_in": np.zeros((1, 256), np.float32)})
            np.testing.assert_allclose(h1, h.numpy(), rtol=0, atol=1e-5)
            _, h2 = session.run(["blendshapes", "h_out"], {"frame": x_np, "h_in": h1})
            np.testing.assert_allclose(h2, h_next.numpy(), rtol=0, atol=1e-5)

    def test_pipeline_cli_and_clip_augmentation(self):
        parser = pipeline.build_parser()
        paths = ["--data-dir", "data", "--out-dir", "out"]
        a_t = parser.parse_args(["teacher", *paths])
        self.assertEqual(a_t.epochs, 8)
        self.assertAlmostEqual(a_t.lr_backbone, 2.4e-5)
        a_kd = parser.parse_args(["kd", *paths, "--teacher-ckpt", "teacher.pt"])
        self.assertAlmostEqual(a_kd.disp_tau, 0.14)
        a_s = parser.parse_args(["student", *paths])
        self.assertEqual(a_s.epochs, 55)
        self.assertEqual(a_s.sched, "restarts")
        self.assertAlmostEqual(a_s.ema_decay, 0.996)
        a_m = parser.parse_args(["temporal", *paths, "--init-student", "dummy.pt"])
        self.assertEqual(a_m.epochs, 20)
        self.assertEqual(a_m.hid_dim, 256)
        self.assertEqual(a_m.clip_len, 16)

        clip_u8 = torch.randint(0, 256, (2, 4, 1, 400, 400), dtype=torch.uint8)
        aug = augment.augment_clip_ir_physical(clip_u8)
        self.assertEqual(tuple(aug.shape), (2, 4, 1, 400, 400))
        self.assertGreaterEqual(float(aug.min().item()), 0.0)
        self.assertLessEqual(float(aug.max().item()), 1.0)


class PortableTrainerTests(unittest.TestCase):
    def test_help_does_not_import_training_dependencies(self):
        root = Path(__file__).resolve().parent
        for cmd in (
            ["-m", "mouth50_trainer.pipeline", "--help"],
            *([str(root / "mouth50_trainer" / "pipeline.py"), stage, "--help"]
              for stage in ("teacher", "kd", "student", "temporal")),
        ):
            with self.subTest(cmd=cmd):
                # -S prevents imports of torch, numpy and ONNX Runtime from site-packages.
                result = subprocess.run([sys.executable, "-B", "-S", *cmd], cwd=root, capture_output=True,
                                        encoding="utf-8", timeout=10, check=True)
                self.assertIn("usage:", result.stdout)
                self.assertEqual(result.stderr, "")
                if "-m" in cmd:
                    self.assertIn("{teacher,kd,student,temporal}", result.stdout)
                else:
                    self.assertIn("--data-dir", result.stdout)
                    self.assertIn("--out-dir", result.stdout)

    def test_single_frame_student_export_and_face_model_round_trip(self):
        torch.manual_seed(13)
        self.assertEqual(trainer.ROI_END, 400)
        self.assertIs(trainer.VFTStudent50, VFTStudent50)
        self.assertEqual(CHANNELS, face_schema.CHANNELS)
        model = VFTStudent50(arch="small", net_width=384, head_kind="pyramid", roi_end=400,
                             load_pretrained=False).eval()
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "frame.png"
            pixels = np.random.default_rng(13).integers(0, 256, (400, 400), dtype=np.uint8)
            Image.fromarray(pixels).save(image_path)
            x = eval_continuous.load_gray400_batch([image_path])
            with torch.inference_mode():
                expected = model(x).numpy()[0]
            self.assertEqual(expected.shape, (50,))
            self.assertTrue(np.isfinite(expected).all())
            self.assertTrue(((expected >= 0) & (expected <= 1)).all())

            path = Path(tmp) / "student.onnx"
            trained = ["JawOpen", "TongueOut"]
            manifest = pipeline.export_student_onnx(model, path, trained, "synthetic student")
            self.assertEqual(manifest["schema"], face_schema.SCHEMA)
            self.assertEqual(manifest["channels"], list(CHANNELS))
            self.assertEqual(manifest["trained_channels"], trained)
            self.assertEqual(manifest["validated_channels"], [])
            self.assertEqual(manifest["valid_mask"], 0)
            self.assertLess(manifest["max_onnx_abs_diff"], 1e-5)
            np.testing.assert_allclose(eval_continuous.predict_paths(path, "onnx", [image_path])[0],
                                       expected, rtol=0, atol=1e-5)
            session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
            self.assertEqual([i.name for i in session.get_inputs()], ["frame"])
            self.assertEqual([o.name for o in session.get_outputs()], ["blendshapes"])
            ort_out = session.run(["blendshapes"], {"frame": pixels[None, None].astype(np.float32) / 255.0})[0][0]
            np.testing.assert_allclose(ort_out, expected, rtol=0, atol=1e-5)

    def test_ir_augment_and_teacher_full_roi(self):
        torch.manual_seed(157)
        frame = torch.randint(0, 256, (2, 1, 400, 400), dtype=torch.uint8)
        for fn, size in ((trainer.augment_ir_physical, 400), (trainer.augment_std, 400),
                         (lambda t: augment.extract_teacher_roi400(t, augment=True), 384)):
            out = fn(frame)
            self.assertEqual(tuple(out.shape), (2, 1, size, size))
            self.assertEqual(out.dtype, torch.float32)
            self.assertTrue(torch.isfinite(out).all())
            self.assertTrue(((out >= 0) & (out <= 1)).all())
            if size == 400:
                self.assertFalse(torch.allclose(out, frame.float() / 255.0))
        constant = torch.full((1, 1, 400, 400), 255, dtype=torch.uint8)
        full = augment.extract_teacher_roi400(constant, augment=False)
        torch.testing.assert_close(full, torch.ones_like(full), atol=2e-5, rtol=0)
        shifted = augment.extract_teacher_roi400(constant, augment=False, dx_const=6)
        self.assertEqual(shifted[0, 0, 192, -1].item(), 0.0)
        self.assertAlmostEqual(shifted[0, 0, 192, 192].item(), 1.0)

    def test_teacher_mixup_does_not_make_unknown_targets_supervised(self):
        images = torch.tensor([0.0, 1.0]).view(2, 1, 1, 1)
        targets = torch.tensor([[0.2, 0.0, 0.7], [0.8, 0.5, 0.3]])
        weights = torch.tensor([[0.8, 1.0, 0.1], [1.4, 0.0, 1.0]])
        with patch.object(augment.np.random, "beta", return_value=0.75), \
             patch.object(augment.torch, "randperm", return_value=torch.tensor([1, 0])):
            mixed, t, w = trainer.apply_teacher_mixup(images, targets, weights)
        torch.testing.assert_close(mixed.flatten(), torch.tensor([0.25, 0.75]))
        torch.testing.assert_close(t, torch.tensor([[0.35, 0.0, 0.0], [0.65, 0.0, 0.0]]))
        torch.testing.assert_close(w, torch.tensor([[0.8, 0.0, 0.0], [0.8, 0.0, 0.0]]))
        with patch.object(augment.np.random, "beta", return_value=0.99):
            unchanged = augment.apply_teacher_mixup(images, targets, weights)
        for got, expected in zip(unchanged, (images, targets, weights)):
            self.assertIs(got, expected)

    def test_v4_label_channel_tables_and_rules(self):
        rules = label_rules
        channels = set(face_schema.CHANNELS)
        self.assertEqual(rules.CHANNELS, face_schema.CHANNELS)
        for names in (*rules.DECOUPLED_ZERO_PAIRS.values(), *rules.ISOLATED_POSE_POSITIVES.values(),
                      *rules.ISOLATED_POSE_NEGATIVES.values(), *eval_continuous.COMPOSITE_CHANNELS.values(),
                      rules.SPEECH_WEAK_CHANNELS, rules.TONGUE_ALL, rules.TONGUE_ALLOW_JAW):
            self.assertLessEqual(set(names), channels)
        self.assertLessEqual(rules.PHYSICAL_COACT_GUARD_CHANNELS, set(face_schema.STOCK_CHANNELS))
        self.assertFalse(rules.PHYSICAL_COACT_GUARD_CHANNELS.intersection(face_schema.CURATED_MODEL_TARGETS))
        self.assertLessEqual(set(rules.HIGH_SNR_RAW33), set(face_schema.STOCK_CHANNELS))

        source = dict(vis="CLEAR", hold=(1, 2), tongue_safe_zero=True, pos={"MouthLeft": 1})
        left = rules.apply_v4_prompt_rules("MouthLeft", source)
        self.assertEqual(left["hold"], source["hold"])
        self.assertEqual(left["pos"], {"MouthLeft": 0.90, "MouthUpperLeft": 0.85, "MouthLowerLeft": 0.85})
        self.assertEqual(source["pos"], {"MouthLeft": 1})
        self.assertIn("MouthUpperRight", left["neg"])
        self.assertIn("JawRight", rules.apply_v4_prompt_rules("MouthLowerLeft", source)["neg"])
        suck = rules.apply_v4_prompt_rules("CheekSuckLeft", {"tongue_safe_zero": True})
        self.assertEqual(suck["pos"], {"CheekSuckLeft": 0.90})
        self.assertEqual(suck["pos_weight"], 2.8)
        self.assertIn("MouthRollLower", suck["neg"])
        self.assertIn("MouthRollUpper", suck["neg"])
        self.assertIn("MouthTightenerLeft", suck["neg"])
        self.assertIn("MouthTightenerRight", suck["neg"])
        self.assertNotIn("MouthRollLower", rules.allowed_weak_channels_v3("CheekSuckLeft", suck))
        t_hold, w_hold = rules.build_frame_labels("CheekSuckLeft", "hold", suck)
        self.assertEqual(t_hold[face_schema.INDEX["CheekSuckLeft"]], 0.90)
        self.assertEqual(w_hold[face_schema.INDEX["CheekSuckLeft"]], 2.8)
        t_carry, w_carry = rules.build_frame_labels(
            "CheekSuckLeft", "carryover_release", suck,
            ramp_targets={"CheekSuckLeft": 0.45, "CheekSuckRight": 0.55},
        )
        self.assertEqual(t_carry[face_schema.INDEX["CheekSuckLeft"]], 0.45)
        self.assertIsNone(t_carry[face_schema.INDEX["CheekSuckRight"]])
        self.assertEqual(w_carry[face_schema.INDEX["CheekSuckRight"]], 0.0)
        self.assertIn("CheekSuckRight", rules.apply_v4_prompt_rules("TongueRoll", {})["neg"])
        funnel = rules.apply_v4_prompt_rules("LipFunnelUpper", {"coact": {"MouthPucker", "JawOpen"}})
        self.assertEqual(funnel["coact"], {"JawOpen"})
        self.assertIn("MouthPucker", funnel["neg"])
        self.assertEqual(rules.apply_v4_prompt_rules("TongueLeft", {})["pos"], {"TongueLeft": 0.85, "TongueOut": 0.94})
        self.assertEqual(rules.apply_v4_prompt_rules("TongueRoll", {})["pos"], {"TongueOut": 0.90})
        self.assertEqual(rules.apply_v4_prompt_rules("tongue_medium", {})["pos"], {"TongueOut": 0.82})
        self.assertEqual(rules.SKIPPED_PROMPTS, {"lips_opposed_a", "lips_opposed_b"})

    def test_v4_physical_coactivation_and_decoupled_zeros(self):
        idx = face_schema.INDEX
        stock = [0.0] * len(face_schema.STOCK_CHANNELS)
        stock[idx["JawLeft"]] = stock[idx["MouthLowerDownL"]] = 0.30
        jaw = label_rules.apply_v4_prompt_rules(
            "JawOpen", dict(pos={"JawOpen": 0.8}, tongue_safe_zero=True, coact={"MouthLowerDownL"}),
        )
        for phase in ("hold", "transition"):
            with self.subTest(phase=phase):
                t, w = label_rules.build_frame_labels("JawOpen", phase, jaw, cal33=stock)
                self.assertIsNone(t[idx["JawLeft"]])
                self.assertEqual(w[idx["JawLeft"]], 0.0)
                self.assertEqual(t[idx["MouthLowerDownL"]], 0.0)
                self.assertEqual(w[idx["MouthLowerDownL"]], 0.45 if phase == "hold" else 0.35)
        t, w = label_rules.build_frame_labels("JawOpen", "hold", jaw, cal33=stock,
                                              manual_targets={"MouthLowerDownL": 0.62})
        self.assertEqual(t[idx["MouthLowerDownL"]], 0.62)
        self.assertEqual(w[idx["MouthLowerDownL"]], 2.4)
        self.assertEqual(w[idx["TongueOut"]], 3.5)
        self.assertEqual(w[idx["MouthUpperUpL"]], 2.4)

    def test_evaluation_optional_benchmarks_and_weak_only_channels(self):
        class Constant(torch.nn.Module):
            def forward(self, frames):
                return frames.new_full((len(frames), 50), 0.35)

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            Image.new("L", (400, 400)).save(data / "one.png")
            row = _row("one.png", "smile_puff", "MouthPucker", 0.5, 0.65)
            _write_jsonl(data / "vft50_val71.jsonl", [row])
            base = eval_continuous.evaluate_model(Constant(), "student", "synthetic", data)
            self.assertEqual(list(base["val71_per_channel"]), list(CHANNELS))
            self.assertAlmostEqual(base["val71_per_channel"]["MouthPucker"]["mid_mae"], 0.15)
            self.assertEqual(base["val71_per_channel"]["MouthPucker"]["count"], 0)
            self.assertEqual(base["composite_coactivation"]["smile_puff"]["n"], 1)
            self.assertIn("tongue_symmetry_and_turns", base["v3_diagnostics"])
            for key in ("cross_session", "heldout_pucker_ramp", "wide_open_trajectory"):
                self.assertNotIn(key, base)

            cross = data / "cross.json"
            cross.write_text(json.dumps({
                "segments": {"example": [{"image": "one.png"}]},
                "jaw_review": [{"image": "one.png", "review_labels": {"JawOpen": 0.0}}],
            }), encoding="utf-8")
            refined = data / "reviews.json"
            refined.write_text(json.dumps({
                "evaluation": [{"sheet_index": "sheet.json", "index": 0, "labels": {"MouthPucker": 0.5}}],
            }), encoding="utf-8")
            (data / "sheet.json").write_text(json.dumps([{"image": "one.png", "t_rel": 0.1}]), encoding="utf-8")
            trajectory = data / "trajectory.json"
            trajectory.write_text(json.dumps({
                "buckets": {"JawOpen": [{"image": "one.png", "t_rel": t / 90, "phase": "hold"} for t in range(7)]},
            }), encoding="utf-8")
            extra = eval_continuous.evaluate_model(Constant(), "student", "synthetic", data,
                                                   xsession_json=cross, refined_json=refined, trajectory_json=trajectory)
            self.assertEqual(extra["cross_session"]["segments"]["example"]["n"], 1)
            self.assertEqual(extra["cross_session"]["jaw_review"]["jaw_closed_fp"], "0/1")
            self.assertAlmostEqual(extra["heldout_pucker_ramp"]["mae"], 0.15)
            self.assertEqual(extra["wide_open_trajectory"]["JawOpen"]["n_hold"], 4)
            self.assertEqual(extra["v3_diagnostics"]["sequential_max_step"]["worst_max_step_33ms"], 0.0)

    def test_missing_materials_name_the_explicit_option_and_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "--mnv3-weights.*MOUTH50_MNV3_WEIGHTS"):
                VFTStudent50(arch="small")
            with self.assertRaisesRegex(ValueError, "--dino-weights.*MOUTH50_DINO_WEIGHTS"):
                VFTTeacher50()
            with self.assertRaisesRegex(ValueError, "--dino-repo.*MOUTH50_DINO_REPO"):
                VFTTeacher50(load_pretrained=False)

    def test_mobilenet_explicit_weights_override_environment(self):
        from torchvision.models import mobilenet_v3_small

        with tempfile.TemporaryDirectory() as tmp:
            weights = Path(tmp) / "mobile.pth"
            backbone = mobilenet_v3_small(weights=None)
            torch.save(backbone.state_dict(), weights)
            expected = next(backbone.features.parameters())
            with patch.dict(os.environ, {"MOUTH50_MNV3_WEIGHTS": str(weights)}):
                torch.testing.assert_close(next(VFTStudent50(arch="small").features.parameters()), expected)
            with patch.dict(os.environ, {"MOUTH50_MNV3_WEIGHTS": str(weights.with_name("missing.pth"))}):
                model = VFTStudent50(arch="small", mnv3_weights=weights)
                torch.testing.assert_close(next(model.features.parameters()), expected)

    def test_teacher_delta_full_checkpoint_and_teacher_kd_cli(self):
        class TestBackbone(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.blocks = torch.nn.ModuleList([torch.nn.Linear(3, 1024)])
                self.norm = torch.nn.LayerNorm(1024)

            def forward_features(self, x):
                token = self.norm(self.blocks[0](x.mean(dim=(2, 3))))
                return dict(x_norm_clstoken=token, x_norm_patchtokens=token[:, None].expand(-1, 4, -1))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backbones = root / "dinov3" / "hub" / "backbones.py"
            backbones.parent.mkdir(parents=True)
            backbones.touch()
            weights = root / "backbone.pth"
            torch.save(TestBackbone().state_dict(), weights)
            fake = SimpleNamespace(__file__=str(backbones), dinov3_vitl16=lambda pretrained: TestBackbone())
            dino_env = {"MOUTH50_DINO_REPO": str(root), "MOUTH50_DINO_WEIGHTS": str(weights)}
            with patch("mouth50_trainer.models.importlib.import_module", return_value=fake), patch.dict(os.environ, dino_env):
                teacher = VFTTeacher50(unfreeze_last_n=1).eval()
                with torch.no_grad():
                    teacher.backbone.blocks[0].bias.add_(0.125)
                delta = save_teacher_delta(teacher, root / "delta.pt")
                saved = torch.load(delta, weights_only=True)
                self.assertEqual(saved["backbone_material"], weights.name)
                self.assertEqual(saved["checkpoint_kind"], "official-delta")
                self.assertNotIn("mean", saved["state_dict"])
                probe = torch.rand(1, 1, 400, 400)
                restored = teacher_from_saved(saved, dino_repo=root, dino_weights=weights).eval()
                torch.testing.assert_close(restored(probe), teacher(probe))
                self.assertEqual(restored.unfreeze_last_n, 0)
                resaved = torch.load(save_teacher_delta(restored, root / "resaved.pt"), weights_only=True)
                self.assertEqual(resaved["unfreeze_last_n"], 1)
                torch.testing.assert_close(teacher_from_saved(resaved).eval()(probe), teacher(probe))
                full = VFTTeacher50(load_pretrained=False).eval()
                saved_full = torch.load(save_teacher_delta(full, root / "full.pt"), weights_only=True)
                self.assertEqual(saved_full["checkpoint_kind"], "full")
                torch.testing.assert_close(teacher_from_saved(saved_full).eval()(probe), full(probe))

                data, teacher_dir, kd_dir = root / "data", root / "teacher", root / "kd"
                data.mkdir()
                for name, fill in (("train.png", 120), ("val.png", 150)):
                    Image.new("L", (400, 400), fill).save(data / name)
                train_row = _row("train.png", "JawOpen", "JawOpen", 0.6)
                _write_jsonl(data / "vft50_train.jsonl", [train_row])
                _write_jsonl(data / "vft50_val71.jsonl", [_row("val.png", "JawOpen", "JawOpen", 0.4)])
                _write_jsonl(data / "vft50_kd_pool.jsonl", [train_row, {**train_row, "review_excluded": True}])
                opts = ["--data-dir", str(data), "--device", "cpu"]
                rep = pipeline.main(["teacher", *opts, "--out-dir", str(teacher_dir), "--init-teacher", str(delta),
                                     "--epochs", "1", "--batch-size", "1"])
                self.assertEqual(len(rep["val71_per_channel"]), 50)
                kd_cmd = ["kd", *opts, "--out-dir", str(kd_dir),
                          "--teacher-ckpt", str(teacher_dir / "teacher_continuous_best.pt")]
                kd = pipeline.main(kd_cmd)
                self.assertEqual(kd["n_rows"], 1)
                out = kd_dir / "vft50_train_kd.jsonl"
                generated = eval_continuous.load_rows(out)
                self.assertEqual(generated[0]["image"], str(data / "train.png"))
                self.assertEqual(len(generated[0]["soft_targets"]), 50)
                with out.open("a", encoding="utf-8") as file:
                    file.write("{")
                self.assertEqual(kd, pipeline.main([*kd_cmd, "--resume"]))

    def test_cli_student_and_temporal_with_explicit_kd_and_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, kd_dir = root / "data", root / "kd"
            data.mkdir()
            kd_dir.mkdir()
            samples = []
            for i, level in enumerate((92, 137, 201)):
                filename = f"frame{i}.png"
                Image.new("L", (400, 400), level).save(data / filename)
                samples.append(dict(_row(filename, "JawOpen", "JawOpen", level / 255),
                                    session_id="synthetic", t_rel=i / 90))
            _write_jsonl(data / "vft50_val71.jsonl", [samples[-1]])
            kd = []
            for sample in samples[:2]:
                soft = [None] * 50
                soft[face_schema.INDEX["TongueOut"]] = 0.2
                kd.append(dict(sample, image=f"../data/{sample['image']}", soft_targets=soft))
            kd_path = kd_dir / "kd.jsonl"
            _write_jsonl(kd_path, kd)
            clips = data / "clips.json"
            clips.write_text(json.dumps({"clips": [{"images": [r["image"] for r in samples[:2]]}]}), encoding="utf-8")
            student, temporal = root / "student", root / "temporal"
            common = ["--data-dir", str(data), "--kd-jsonl", str(kd_path), "--device", "cpu",
                      "--epochs", "1", "--batch-size", "1", "--width", "256"]
            rep = pipeline.main(["student", *common, "--out-dir", str(student), "--random-init", "--min-pick-ep", "1"])
            self.assertEqual(len(rep["val71_per_channel"]), 50)
            rep = pipeline.main(["temporal", *common, "--out-dir", str(temporal), "--clips-json", str(clips),
                                 "--init-student", str(student / "student_best.pt"),
                                 "--clip-len", "2", "--clips-per-epoch", "1"])
            self.assertEqual(len(rep["val71_sequential_per_channel"]), 50)
            for path in (student / "vft_student_50.onnx", temporal / "vft_student_50_gru256.onnx"):
                self.assertTrue(path.is_file())
                manifest = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["trained_channels"], ["JawOpen", "TongueOut"])
                self.assertEqual(manifest["valid_mask"], 0)


def _row(image, prompt, channel, value, weight=1):
    t, w = [None] * 50, [0.0] * 50
    i = face_schema.INDEX[channel]
    t[i], w[i] = value, weight
    return dict(image=image, prompt_id=prompt, phase="hold", targets=t, weights=w)


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
