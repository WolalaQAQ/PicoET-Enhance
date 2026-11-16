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


if __name__ == '__main__':
    unittest.main(verbosity=2)
