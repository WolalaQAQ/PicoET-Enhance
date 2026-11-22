"""Real CPU ONNX contract checks using synthetic weights, not trained capabilities."""
import json
from pathlib import Path
import tempfile
import socket
import time
from dataclasses import replace
import unittest

import numpy as np
import torch

from face_model import FaceModel
from face_schema import CHANNELS, INDEX, SCHEMA
from mouthlink import prepare_input
from runtime import ROOT, DEFAULT_CONFIG, load_model
from test_face_runtime import FaceManualRuntime
from test_runtime import header, jpeg, wait_for
from wire import FACE_RESULT


class SyntheticFace(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.register_buffer('bias', torch.linspace(-3, 2, 50))

    def forward(self, frame):
        return torch.sigmoid(frame.mean((1, 2, 3))[:, None]+self.bias)


class SyntheticStatefulGRUFace(torch.nn.Module):
    """Minimal pure-opset17 unrolled GRU face model (frame + h_in -> blendshapes + h_out)."""

    def __init__(self, hid_dim=16):
        super().__init__()
        self.hid_dim = hid_dim
        self.x_zr = torch.nn.Linear(1, 2 * hid_dim)
        self.h_zr = torch.nn.Linear(hid_dim, 2 * hid_dim)
        self.x_n = torch.nn.Linear(1, hid_dim)
        self.h_n = torch.nn.Linear(hid_dim, hid_dim)
        self.static_head = torch.nn.Linear(1, 50)
        self.temp_head = torch.nn.Linear(hid_dim, 50)
        torch.manual_seed(17)
        for p in self.parameters():
            torch.nn.init.uniform_(p, -0.3, 0.3)
        # Ensure strong state dependence on h_in
        with torch.no_grad():
            self.x_zr.bias.fill_(-0.5)
            self.h_zr.bias.fill_(0.0)
            self.temp_head.weight.fill_(0.25)

    def forward(self, frame, h_in):
        feat = frame.mean((1, 2, 3), keepdim=False)[:, None]
        xz, xr = torch.split(self.x_zr(feat), self.hid_dim, dim=-1)
        hz, hr = torch.split(self.h_zr(h_in), self.hid_dim, dim=-1)
        z = torch.sigmoid(xz + hz)
        r = torch.sigmoid(xr + hr)
        n = torch.tanh(self.x_n(feat) + r * self.h_n(h_in))
        h_out = (1.0 - z) * n + z * h_in
        logits = self.static_head(feat) + self.temp_head(h_out)
        return torch.sigmoid(logits), h_out


class FaceModelTests(unittest.TestCase):
    def setUp(self):
        work = ROOT/'tests-work'; work.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=work); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'synthetic.onnx'
        self.network = SyntheticFace().eval()
        self.meta = dict(schema=SCHEMA, channels=list(reversed(CHANNELS)), width=160, height=160,
                         trained_channels=['TongueOut', 'TongueLeft', 'LipFunnelLower'],
                         validated_channels=['TongueOut', 'TongueLeft', 'LipFunnelLower'],
                         valid_mask=sum(1 << INDEX[name] for name in ('TongueOut', 'TongueLeft', 'LipFunnelLower')))
        torch.onnx.export(self.network, torch.zeros(1, 1, 160, 160), str(self.path), input_names=['frame'],
                          output_names=['blendshapes'], opset_version=17, dynamo=False)
        self.write_meta()

    def write_meta(self):
        self.path.with_suffix('.json').write_text(json.dumps(self.meta), encoding='utf-8')

    def test_real_onnx_direct_values_follow_names_without_out_gate_or_direction_scaling(self):
        model = load_model(dict(DEFAULT_CONFIG, model_path=str(self.path), device='cpu'))
        image = np.full((400, 400), 120, np.uint8)
        with torch.inference_mode():
            expected = self.network(torch.from_numpy(prepare_input(image, 160, 160))).numpy()[0]
        result = model.predict(image)
        for name in self.meta['trained_channels']:
            self.assertAlmostEqual(result['channels'][name], expected[self.meta['channels'].index(name)], delta=1e-5)
        self.assertEqual(result['valid_mask'].bit_count(), 3)
        self.assertEqual(result['values'][INDEX['JawOpen']], 0.)
        self.assertNotIn('JawOpen', result['channels'])
        self.assertEqual(model.describe()['unsupported_channels'].__len__(), 47)
        self.assertEqual(model.provider, 'CPUExecutionProvider')
        self.assertNotIn('mask', result); self.assertNotIn('out_hysteresis', result)

    def test_missing_or_unknown_manifest_support_is_rejected_before_inference(self):
        for trained in (None, ['MadeUp'], ['TongueOut', 'TongueOut']):
            self.meta['trained_channels'] = trained; self.write_meta()
            with self.assertRaisesRegex(ValueError, 'trained_channels'):
                FaceModel(str(self.path), 'cpu')

    def test_schema_or_channel_set_cannot_silently_load_legacy44(self):
        self.meta['schema'] = 'legacy44'; self.write_meta()
        with self.assertRaisesRegex(ValueError, 'schema'):
            FaceModel(str(self.path), 'cpu')
        self.meta['schema'] = SCHEMA; self.meta['channels'] = list(CHANNELS[:44]); self.write_meta()
        with self.assertRaisesRegex(ValueError, '50 unique'):
            FaceModel(str(self.path), 'cpu')

    def test_empty_trained_list_is_explicitly_unsupported_not_50_valid_random_heads(self):
        self.meta.update(trained_channels=[], validated_channels=[], valid_mask=0); self.write_meta()
        result = FaceModel(str(self.path), 'cpu').predict(np.zeros((400, 400), np.uint8))
        self.assertEqual(result['valid_mask'], 0); self.assertEqual(result['channels'], {})

    def test_optimized_but_unvalidated_legacy_candidate_cannot_publish(self):
        self.meta.pop('validated_channels'); self.meta.pop('valid_mask'); self.write_meta()
        model = FaceModel(str(self.path), 'cpu')
        result = model.predict(np.zeros((400, 400), np.uint8))
        self.assertEqual(result['valid_mask'], 0)
        self.assertEqual(result['channels'], {})
        self.assertEqual(model.describe()['trained_channels'], self.meta['trained_channels'])

    def test_mask_zero_and_subset_are_obeyed_even_with_validated_training(self):
        for mask in (0, 1 << INDEX['TongueOut']):
            self.meta['valid_mask'] = mask; self.write_meta()
            result = FaceModel(str(self.path), 'cpu').predict(np.zeros((400, 400), np.uint8))
            self.assertEqual(result['valid_mask'], mask)
            self.assertEqual(set(result['channels']), {'TongueOut'} if mask else set())
            self.assertEqual(result['values'][INDEX['TongueLeft']], 0.)

    def test_invalid_validation_or_mask_is_rejected_before_backend_creation(self):
        original = dict(self.meta)
        for fields in (dict(validated_channels=['JawOpen']), dict(validated_channels=['TongueOut']*2),
                       dict(valid_mask=1 << INDEX['JawOpen']), dict(valid_mask=-1), dict(valid_mask='1'),
                       dict(validated_channels=[], valid_mask=1 << INDEX['TongueOut'])):
            self.meta = dict(original, **fields); self.write_meta()
            with self.assertRaisesRegex(ValueError, 'validated_channels|valid_mask'):
                FaceModel(str(self.path), 'cpu')

    def test_manifest_dimensions_must_match_frame_graph(self):
        self.meta['width'] = 100; self.write_meta()
        with self.assertRaises(Exception):
            FaceModel(str(self.path), 'cpu')

    def test_nonfinite_outputs_fail_instead_of_retaining_a_positive(self):
        model = FaceModel(str(self.path), 'cpu')
        class BadSession:
            def run(self, *args): return [np.full((1, 50), np.nan, np.float32)]
        model.session = BadSession()
        with self.assertRaisesRegex(ValueError, 'invalid blendshapes'):
            model.predict(np.zeros((400, 400), np.uint8))

    def test_real_jpeg_to_cpu_onnx_to_mfr1_preserves_the_trained_named_heads(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sink:
            sink.bind(('127.0.0.1', 0)); sink.settimeout(2)
            runtime = FaceManualRuntime(self.path.parent/'config.json')
            try:
                runtime.configure(dict(headset_host='controlled-input', model_path=str(self.path), device='cpu',
                                       output_enabled=True, output_port=sink.getsockname()[1]))
                runtime.start(); wait_for(lambda: runtime.connected)
                payload = jpeg(); h = replace(header(), payload_bytes=len(payload))
                runtime._publish(h, payload, time.perf_counter_ns())
                fields = FACE_RESULT.unpack(sink.recv(1024))
                self.assertEqual(fields[:3], (b'MFR1', 1, 256)); self.assertEqual(fields[9], 0)
                self.assertEqual(fields[7], sum(1 << INDEX[name] for name in self.meta['trained_channels']))
                for name in self.meta['trained_channels']:
                    self.assertAlmostEqual(fields[10+INDEX[name]], runtime.prediction['channels'][name])
                self.assertEqual(fields[10+INDEX['JawOpen']], 0.)
                runtime.stop(); self.assertEqual(FACE_RESULT.unpack(sink.recv(1024))[7], 0)
            finally:
                runtime.stop(); runtime.udp.close()

    def _export_stateful(self, hid_dim=16, include_manifest_temporal=True):
        gru_path = Path(self.tmp.name)/('synthetic_gru.onnx' if include_manifest_temporal else 'synthetic_gru_auto.onnx')
        net = SyntheticStatefulGRUFace(hid_dim=hid_dim).eval()
        with torch.inference_mode():
            torch.onnx.export(
                net,
                (torch.zeros(1, 1, 160, 160), torch.zeros(1, hid_dim)),
                str(gru_path),
                input_names=['frame', 'h_in'],
                output_names=['blendshapes', 'h_out'],
                opset_version=17,
                dynamo=False,
            )
        meta = dict(self.meta)
        if include_manifest_temporal:
            meta.update(
                temporal_kind='gru_stateful',
                state_input_name='h_in',
                state_output_name='h_out',
                state_shape=[1, hid_dim],
                temporal=dict(kind='gru_stateful', state_input='h_in', state_output='h_out',
                              state_shape=[1, hid_dim], nominal_hz=90),
            )
        gru_path.with_suffix('.json').write_text(json.dumps(meta), encoding='utf-8')
        return gru_path, net, meta

    def test_stateless_model_reports_stateless_and_accepts_optional_temporal_kwargs(self):
        model = FaceModel(str(self.path), 'cpu')
        desc = model.describe()
        self.assertFalse(desc['temporal_smoothing'])
        self.assertEqual(desc['temporal_kind'], 'stateless')
        self.assertFalse(model.is_temporal)
        img = np.full((400, 400), 100, np.uint8)
        r1 = model.predict(img)
        r2 = model.predict(img, dict(stale_ms=120), session_id=7, source_ns=1_000_000_000)
        self.assertEqual(r1['values'], r2['values'])

    def test_stateful_gru_propagates_state_and_resets_cleanly_on_cpu_and_cuda(self):
        import onnxruntime as ort

        for include_meta in (True, False):
            gru_path, net, meta = self._export_stateful(hid_dim=16, include_manifest_temporal=include_meta)
            devices = ['cpu']
            if 'CUDAExecutionProvider' in ort.get_available_providers():
                devices.extend(['onnx:cuda', 'onnxgraph:cuda'])
            imgs = [
                np.full((400, 400), 40, np.uint8),
                np.full((400, 400), 180, np.uint8),
                np.full((400, 400), 220, np.uint8),
            ]
            # PyTorch reference sequence
            with torch.inference_mode():
                h_ref = torch.zeros(1, 16)
                ref_outs = []
                for im in imgs:
                    y_ref, h_ref = net(torch.from_numpy(prepare_input(im, 160, 160)), h_ref)
                    ref_outs.append(y_ref.numpy()[0])
                y_cold1, _ = net(torch.from_numpy(prepare_input(imgs[1], 160, 160)), torch.zeros(1, 16))
                cold1 = y_cold1.numpy()[0]

            for dev in devices:
                model = FaceModel(str(gru_path), dev)
                desc = model.describe()
                self.assertTrue(model.is_temporal)
                self.assertTrue(desc['temporal_smoothing'])
                self.assertEqual(desc['temporal_kind'], 'gru_stateful')
                self.assertEqual(desc['state_shape'], [1, 16])
                # Probe during __init__ must leave state cleanly zeroed
                self.assertEqual(float(np.abs(model._state).max()), 0.0)

                cfg = dict(stale_ms=120)
                t0 = 10_000_000_000
                outs = []
                for k, im in enumerate(imgs):
                    res = model.predict(im, cfg, session_id=1, source_ns=t0 + k * 11_111_111)
                    outs.append(res)
                    for name in meta['trained_channels']:
                        self.assertAlmostEqual(
                            res['channels'][name],
                            ref_outs[k][meta['channels'].index(name)],
                            delta=1e-5,
                            msg=f'mismatch dev={dev} meta={include_meta} step={k} ch={name}',
                        )

                # Confirm state actually affected step 1 vs cold start
                ch0 = meta['trained_channels'][0]
                self.assertGreater(abs(outs[1]['channels'][ch0] - cold1[meta['channels'].index(ch0)]), 1e-4)

                # Auto-reset on session_id change -> matches cold start for imgs[1]
                res_new_sess = model.predict(imgs[1], cfg, session_id=2, source_ns=t0 + 40_000_000)
                for name in meta['trained_channels']:
                    self.assertAlmostEqual(res_new_sess['channels'][name], cold1[meta['channels'].index(name)], delta=1e-5)

                # Auto-reset on stale dt (> stale_ms) -> matches cold start for imgs[1]
                res_stale = model.predict(imgs[1], cfg, session_id=2, source_ns=t0 + 500_000_000)
                for name in meta['trained_channels']:
                    self.assertAlmostEqual(res_stale['channels'][name], cold1[meta['channels'].index(name)], delta=1e-5)

                # Auto-reset on non-positive dt (dt <= 0) -> matches cold start for imgs[1]
                res_backwards = model.predict(imgs[1], cfg, session_id=2, source_ns=t0 + 500_000_000)
                for name in meta['trained_channels']:
                    self.assertAlmostEqual(res_backwards['channels'][name], cold1[meta['channels'].index(name)], delta=1e-5)

                # Explicit reset_state() -> matches cold start for imgs[1]
                model.reset_state()
                self.assertEqual(float(np.abs(model._state).max()), 0.0)
                res_manual = model.predict(imgs[1], cfg, session_id=2, source_ns=t0 + 511_111_111)
                for name in meta['trained_channels']:
                    self.assertAlmostEqual(res_manual['channels'][name], cold1[meta['channels'].index(name)], delta=1e-5)

    def test_stateful_gru_runtime_integration_resets_across_sessions_and_stop(self):
        gru_path, _, _ = self._export_stateful(hid_dim=16, include_manifest_temporal=True)
        runtime = FaceManualRuntime(self.path.parent/'config_gru.json')
        try:
            runtime.configure(dict(headset_host='controlled-input', model_path=str(gru_path), device='cpu',
                                   output_enabled=False, filter_enabled=False))
            runtime.start(); wait_for(lambda: runtime.connected)
            self.assertTrue(runtime.inference_ready.wait(2))
            self.assertTrue(runtime.model.is_temporal)
            # Warmup must leave hidden state clean before live frames arrive
            self.assertEqual(float(np.abs(runtime.model._state).max()), 0.0)
            payload = jpeg()
            h1 = replace(header(1, session=10), payload_bytes=len(payload))
            runtime._publish(h1, payload, time.perf_counter_ns())
            wait_for(lambda: runtime.counts['inferred'] == 1)
            self.assertGreater(float(np.abs(runtime.model._state).max()), 0.0)
            # Switching camera session triggers clean reset and continues streaming
            h2 = replace(header(1, session=11), payload_bytes=len(payload))
            runtime._publish(h2, payload, time.perf_counter_ns())
            wait_for(lambda: runtime.counts['inferred'] == 2)
            runtime.stop()
            self.assertEqual(float(np.abs(runtime.model._state).max()), 0.0)
        finally:
            runtime.stop(); runtime.udp.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
