"""MouthLink ONNX interoperability over the existing PTJ1 camera pipeline.

Model contract and preprocessing: UndefinedSignal/MouthLink (MIT),
39e091f4191d2ea1ad8536120924cdf61949dae2, training/export_onnx.py.
Only tongue channels are used; the stock mouth and eyes remain untouched.
"""
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image

from model import TongueModel

CHANNELS = {'out': 'TongueOut', 'left': 'TongueLeft', 'right': 'TongueRight',
            'up': 'TongueUp', 'down': 'TongueDown', 'roll': 'TongueRoll'}


def prepare_input(image, width, height):
    # Pillow's downscale filter is not torch.interpolate / OpenCV INTER_LINEAR.
    resized = np.asarray(Image.fromarray(image).resize((width, height), Image.Resampling.BILINEAR))
    return resized.astype(np.float32)[None, None]/255


class MouthLinkModel(TongueModel):
    input_name = 'frame'
    output_name = 'blendshapes'

    def __init__(self, checkpoint, device='auto'):
        path = Path(checkpoint)
        if path.suffix.lower() != '.onnx':
            raise ValueError('MouthLink requires an ONNX model with adjacent JSON metadata')
        meta = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        self.channels = meta['channels']
        required = set(CHANNELS.values())-{'TongueRoll'}
        if not required.issubset(self.channels) or len(set(self.channels)) != len(self.channels):
            raise ValueError('MouthLink metadata requires unique TongueOut/Left/Right/Up/Down channels')
        self.width, self.height = int(meta['width']), int(meta['height'])
        if not 1 <= self.width <= 400 or not 1 <= self.height <= 400:
            raise ValueError('MouthLink input dimensions must be between 1 and 400')
        self.input_shape = (1, 1, self.height, self.width)
        self.output_shape = (1, len(self.channels))
        if device.startswith(('torch:', 'torchscript:')):
            raise ValueError('MouthLink supports ONNX Runtime backends only')
        super().__init__(str(path), device)
        inputs, outputs = self.session.get_inputs(), self.session.get_outputs()
        if (len(inputs) != 1 or inputs[0].name != self.input_name or inputs[0].type != 'tensor(float)' or
                inputs[0].shape[1:] != list(self.input_shape[1:]) or
                len(outputs) != 1 or outputs[0].name != self.output_name or
                outputs[0].type != 'tensor(float)' or
                outputs[0].shape[1:] != list(self.output_shape[1:])):
            raise ValueError('MouthLink ONNX input/output does not match its metadata')
        self.predict(np.zeros((400, 400), np.uint8))

    def describe(self):
        return dict(engine=self.engine, device=self.device, checkpoint=self.checkpoint, artifact=self.artifact,
                    requested_device=self.requested_device, provider=self.provider, providers=self.providers,
                    available_providers=self.available_providers, fallbacks=self.fallbacks,
                    model_kind='mouthlink', channels=list(CHANNELS), model_channels=self.channels,
                    image_shape=[400, 400], input_shape=list(self.input_shape),
                    preprocessing='Pillow BILINEAR GRAY8 /255; no crop or horizontal flip',
                    direction_range=[.15, .55], out_hysteresis=[.5, .3], personalization=False,
                    temporal_smoothing=False, cpu_threads=1, load_ms=self.load_ms)

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
            values = self.binding.copy_outputs_to_cpu()[0]
        else:
            values = self.session.run([self.output_name], {self.input_name: x})[0]
        if values.shape != self.output_shape or not np.isfinite(values).all():
            raise ValueError('MouthLink returned invalid blendshapes')
        values = dict(zip(self.channels, np.clip(values[0], 0, 1).tolist()))
        tongue_out = values['TongueOut']
        result = {key: max(0., min(1., (values.get(name, 0.)-.15)/.4))
                  for key, name in CHANNELS.items() if key != 'out'}
        # No max(stock, model): an explicit model negative must release the tongue.
        result.update(out=tongue_out, present=tongue_out >= .5,
                      extended=True, out_hysteresis=[.5, .3])
        end = time.perf_counter()
        result['timing_ms'] = dict(preprocess=(pre-start)*1000, inference=(end-pre)*1000,
                                   postprocess=0., total=(end-start)*1000)
        return result
