"""Real ONNX/preprocessing and extended source-state tests using synthetic images."""
from dataclasses import replace
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest

import numpy as np
from PIL import Image
import torch

from mouthlink import CHANNELS, MouthLinkModel, prepare_input
from runtime import DEFAULT_CONFIG, load_model
from state import TongueState
from test_runtime import ManualRuntime, header, jpeg, wait_for
from wire import EXTENDED_RESULT, RESULT, result_packet


class SyntheticRegression(torch.nn.Module):
    def __init__(self, count):
        super().__init__()
        self.register_buffer('bias', torch.linspace(-1, 1, count))

    def forward(self, frame):
        return torch.sigmoid(frame.mean((1, 2, 3))[:, None]+self.bias)


class MouthLinkTests(unittest.TestCase):
    def setUp(self):
        work = Path(__file__).resolve().parent/'tests-work'
        work.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'mouth.onnx'
        self.channels = ['JawOpen', *reversed(list(CHANNELS.values()))]
        self.meta = dict(width=160, height=160, channels=self.channels)
        self.write_meta()
        self.network = SyntheticRegression(len(self.channels)).eval()
        torch.onnx.export(self.network, torch.zeros(1, 1, 160, 160), str(self.path),
                          input_names=['frame'], output_names=['blendshapes'],
                          dynamic_axes={'frame': {0: 'batch'}, 'blendshapes': {0: 'batch'}},
                          opset_version=17, dynamo=False)

    def write_meta(self):
        self.path.with_suffix('.json').write_text(json.dumps(self.meta), encoding='utf-8')

    def test_exact_pillow_preprocessing_without_crop_flip_or_torch_resize(self):
        image = np.random.default_rng(42).integers(0, 256, (400, 400), np.uint8)
        expected = np.asarray(Image.fromarray(image).resize((160, 160), Image.Resampling.BILINEAR))
        actual = prepare_input(image, 160, 160)
        np.testing.assert_array_equal(actual, expected.astype(np.float32)[None, None]/255)
        self.assertFalse(np.array_equal(actual, prepare_input(image[:, ::-1], 160, 160)))

    def test_real_onnx_matches_torch_and_maps_by_names_not_channel_order(self):
        model = load_model(dict(DEFAULT_CONFIG, model_kind='mouthlink', model_path=str(self.path), device='cpu'))
        image = np.full((400, 400), 120, np.uint8)
        with torch.inference_mode():
            expected = self.network(torch.from_numpy(prepare_input(image, 160, 160))).numpy()[0]
        raw = model.predict(image)
        self.assertAlmostEqual(raw['out'], float(expected[self.channels.index('TongueOut')]), delta=1e-5)
        for key in ('left', 'right', 'up', 'down', 'roll'):
            value = expected[self.channels.index(CHANNELS[key])]
            self.assertAlmostEqual(raw[key], float(np.clip((value-.15)/.4, 0, 1)), delta=1e-5)
        self.assertNotIn('JawOpen', raw)
        self.assertNotIn('raw_angle', raw)
        self.assertFalse(model.describe()['personalization'])
        self.assertEqual(model.describe()['channels'], list(CHANNELS))

    def test_metadata_missing_tongue_and_duplicates_are_rejected(self):
        for channels in (['TongueOut', 'TongueLeft'], self.channels+['TongueOut']):
            self.meta['channels'] = channels
            self.write_meta()
            with self.assertRaisesRegex(ValueError, 'unique TongueOut'):
                MouthLinkModel(str(self.path), 'cpu')

    def test_metadata_must_match_real_graph(self):
        self.meta['width'] = 100
        self.write_meta()
        with self.assertRaises(Exception):
            MouthLinkModel(str(self.path), 'cpu')

    def test_optional_roll_is_zero_and_other_tongue_channels_remain_supported(self):
        self.meta['channels'] = [c for c in self.channels if c != 'TongueRoll']
        self.write_meta()
        network = SyntheticRegression(len(self.meta['channels'])).eval()
        torch.onnx.export(network, torch.zeros(1, 1, 160, 160), str(self.path),
                          input_names=['frame'], output_names=['blendshapes'], opset_version=17, dynamo=False)
        model = MouthLinkModel(str(self.path), 'cpu')
        self.assertEqual(model.predict(np.zeros((400, 400), np.uint8))['roll'], 0.)

    def test_invalid_values_fail_instead_of_publishing_a_stale_positive(self):
        model = MouthLinkModel(str(self.path), 'cpu')
        class BadSession:
            def run(self, *args):
                return [np.full(model.output_shape, np.nan, np.float32)]
        model.session = BadSession()
        with self.assertRaisesRegex(ValueError, 'invalid blendshapes'):
            model.predict(np.zeros((400, 400), np.uint8))

    def test_available_auto_backend_matches_real_cpu_model(self):
        image = np.random.default_rng(99).integers(0, 256, (400, 400), np.uint8)
        cpu = MouthLinkModel(str(self.path), 'cpu')
        automatic = MouthLinkModel(str(self.path), 'auto')
        expected, actual = cpu.predict(image), automatic.predict(image)
        for key in CHANNELS:
            self.assertAlmostEqual(actual[key], expected[key], delta=1e-4)
        self.assertIn(automatic.provider, automatic.describe()['providers'])

    def test_extended_runtime_infers_and_sends_real_tjr2_then_stop(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sink:
            sink.bind(('127.0.0.1', 0)); sink.settimeout(2)
            runtime = ManualRuntime(self.root/'config.json')
            self.addCleanup(runtime.udp.close)
            self.addCleanup(runtime.stop)
            runtime.configure(dict(headset_host='127.0.0.1', device='cpu', model_kind='mouthlink',
                                   model_path=str(self.path), output_enabled=True, output_port=sink.getsockname()[1]))
            runtime.start()
            wait_for(lambda: runtime.connected)
            payload = jpeg()
            runtime._publish(replace(header(), payload_bytes=len(payload)), payload, time.perf_counter_ns())
            data = sink.recv(1000)
            fields = EXTENDED_RESULT.unpack(data)
            self.assertEqual(fields[:3], (b'TJR2', 2, 80))
            self.assertGreater(fields[13], 0.)  # Up, from the actual ONNX model.
            with self.assertRaisesRegex(ValueError, 'MouthLink models use'):
                runtime.personalize('retrain', True)
            runtime.stop()
            self.assertEqual(EXTENDED_RESULT.unpack(sink.recv(1000))[11], 16)


class ExtendedStateTests(unittest.TestCase):
    def test_out_hysteresis_and_explicit_negatives_gate_all_directions(self):
        state = TongueState()
        raw = dict(present=True, extended=True, out_hysteresis=[.5, .3], out=.4,
                   left=.7, right=0., up=.8, down=0., roll=.6)
        config = dict(DEFAULT_CONFIG, smoothing_ms=0)
        self.assertEqual(state.update(raw, header(), config)['state'], 'absent')
        first = state.update(dict(raw, out=.6), header(11), config)
        self.assertEqual((first['up'], first['roll']), (.8, .6))
        self.assertEqual(state.update(raw, header(12), config)['state'], 'present')
        negative = state.update(dict(raw, out=.2), header(13), config)
        self.assertTrue(all(negative[k] == 0 for k in ('out', 'left', 'right', 'up', 'down', 'roll')))
        self.assertEqual(state.update(raw, header(14), config)['state'], 'absent')
        state.update(dict(raw, out=.8), header(15), config)
        self.assertEqual(state.update(raw, header(1, session=2), config)['state'], 'absent')

    def test_source_time_smoothing_applies_to_vertical_and_roll(self):
        state = TongueState()
        raw = dict(present=True, extended=True, out=1., left=0., right=0., up=0., down=0., roll=0.)
        state.update(raw, header(), DEFAULT_CONFIG)
        result = state.update(dict(raw, up=1., roll=1.), header(11), DEFAULT_CONFIG)
        self.assertTrue(0 < result['up'] < 1)
        self.assertEqual(result['up'], result['roll'])
        uncertain = state.update(dict(raw, state='uncertain'), header(12), DEFAULT_CONFIG)
        self.assertEqual(uncertain['up'], result['up'])

    def test_tjr1_stays_byte_compatible_and_tjr2_extends_common_fields(self):
        h = header()
        old = result_packet(h, 1, .2, .3, .8, 5.)
        new = result_packet(h, 1, .2, .3, .8, 5., extended=True, up=.9, down=.1, roll=.4)
        self.assertEqual(RESULT.unpack(old)[:3], (b'TJR1', 1, 64))
        self.assertEqual(EXTENDED_RESULT.unpack(new)[:3], (b'TJR2', 2, 80))
        self.assertEqual(old[8:32], new[8:32])
        self.assertEqual(old[40:64], new[40:64])
        np.testing.assert_allclose(EXTENDED_RESULT.unpack(new)[13:16], [.9, .1, .4])


if __name__ == '__main__':
    unittest.main(verbosity=2)
