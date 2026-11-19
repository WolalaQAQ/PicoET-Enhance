"""Manifest-bound 50-channel ONNX regression, without segmentation geometry."""
import json
from pathlib import Path
import time

import numpy as np

from face_schema import CHANNELS, INDEX, SCHEMA
from model import TongueModel
from mouthlink import prepare_input


class FaceModel(TongueModel):
    input_name = 'frame'
    output_name = 'blendshapes'

    def __init__(self, checkpoint, device='auto'):
        path = Path(checkpoint)
        if path.suffix.lower() != '.onnx':
            raise ValueError('Whole-mouth inference requires an ONNX model and adjacent JSON manifest')
        meta = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        if meta.get('schema') != SCHEMA:
            raise ValueError('Whole-mouth model manifest schema must be '+SCHEMA)
        self.channels = meta['channels']
        if len(self.channels) != 50 or set(self.channels) != set(CHANNELS):
            raise ValueError('Whole-mouth manifest requires all 50 unique named channels')
        trained = meta.get('trained_channels')
        if not isinstance(trained, list) or len(set(trained)) != len(trained) or not set(trained) <= set(CHANNELS):
            raise ValueError('Whole-mouth manifest requires an explicit trained_channels list of known channels')
        self.trained_channels = [name for name in CHANNELS if name in trained]
        validated = meta.get('validated_channels', [])
        if (not isinstance(validated, list) or len(set(validated)) != len(validated) or
                not set(validated) <= set(trained)):
            raise ValueError('validated_channels must be a unique subset of trained_channels')
        mask = meta.get('valid_mask', 0)
        validated_mask = sum(1 << INDEX[name] for name in validated)
        if type(mask) is not int or mask < 0 or mask & ~validated_mask:
            raise ValueError('valid_mask may enable only independently validated_channels')
        self.validated_channels = [name for name in CHANNELS if name in validated]
        self.output_channels = [name for name in CHANNELS if mask & (1 << INDEX[name])]
        self.valid_mask = mask
        self.width, self.height = int(meta['width']), int(meta['height'])
        if not 1 <= self.width <= 400 or not 1 <= self.height <= 400:
            raise ValueError('Whole-mouth input dimensions must be between 1 and 400')
        self.input_shape = (1, 1, self.height, self.width)
        self.output_shape = (1, 50)
        if device.startswith(('torch:', 'torchscript:')):
            raise ValueError('Whole-mouth models support ONNX Runtime backends only')
        super().__init__(str(path), device)
        inputs, outputs = self.session.get_inputs(), self.session.get_outputs()
        if (len(inputs) != 1 or inputs[0].name != self.input_name or inputs[0].type != 'tensor(float)' or
                inputs[0].shape[1:] != list(self.input_shape[1:]) or
                len(outputs) != 1 or outputs[0].name != self.output_name or
                outputs[0].type != 'tensor(float)' or outputs[0].shape[1:] != [50]):
            raise ValueError('Whole-mouth ONNX frame/blendshapes contract does not match its manifest')
        self.predict(np.zeros((400, 400), np.uint8))

    def describe(self):
        return dict(engine=self.engine, device=self.device, checkpoint=self.checkpoint, artifact=self.artifact,
                    requested_device=self.requested_device, provider=self.provider, providers=self.providers,
                    available_providers=self.available_providers, fallbacks=self.fallbacks,
                    model_kind='face50', schema=SCHEMA, channels=list(CHANNELS),
                    trained_channels=self.trained_channels, validated_channels=self.validated_channels,
                    output_channels=self.output_channels, valid_mask=str(self.valid_mask),
                    unsupported_channels=[name for name in CHANNELS if name not in self.output_channels],
                    image_shape=[400, 400], input_shape=list(self.input_shape),
                    preprocessing='Pillow BILINEAR GRAY8 /255; no crop or horizontal flip',
                    temporal_smoothing=False, runtime_filter='One-Euro; smoothing_ms=0 bypasses',
                    geometry=False, cpu_threads=1, load_ms=self.load_ms)

    def predict(self, image, config=None):
        if image.shape != (400, 400) or image.dtype != np.uint8:
            raise ValueError('image must be a 400x400 uint8 grayscale array')
        start = time.perf_counter()
        x = prepare_input(image, self.width, self.height)
        pre = time.perf_counter()
        if self.engine == 'onnxgraph':
            self.input_value.update_inplace(x)
            self.binding.synchronize_inputs()
            self.session.run_with_iobinding(self.binding)
            self.binding.synchronize_outputs()
            output = self.binding.copy_outputs_to_cpu()[0]
        else:
            output = self.session.run([self.output_name], {self.input_name: x})[0]
        if output.shape != self.output_shape or not np.isfinite(output).all():
            raise ValueError('Whole-mouth model returned invalid blendshapes')
        named = dict(zip(self.channels, np.clip(output[0], 0, 1).tolist()))
        # Optimization is not output permission. An explicit zero mask quarantines a candidate.
        values = [named[name] if name in self.output_channels else 0. for name in CHANNELS]
        end = time.perf_counter()
        return dict(values=values, channels={name: named[name] for name in self.output_channels},
                    valid_mask=self.valid_mask, schema=SCHEMA,
                    timing_ms=dict(preprocess=(pre-start)*1000, inference=(end-pre)*1000,
                                   postprocess=0., total=(end-start)*1000))
