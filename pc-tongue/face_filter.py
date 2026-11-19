"""Per-valid-channel One-Euro filtering on the image sensor clock."""
import math

from face_schema import CHANNELS


class FaceFilter:
    def __init__(self):
        self.clear()

    def clear(self):
        self.key = self.source_ns = self.values = self.raw = self.derivative = None

    @staticmethod
    def alpha(cutoff, dt):
        return 1. / (1. + 1. / (2. * math.pi * cutoff * dt))

    def apply(self, prediction, header, config):
        raw = list(prediction['values'])
        mask = prediction['valid_mask']
        smoothing = float(config['smoothing_ms'])
        key = (header.session_id, mask, smoothing)
        source = header.mouth_sensor_ns
        dt = (source-self.source_ns)/1e9 if self.source_ns is not None else 0.
        reset = (key != self.key or dt <= 0 or dt*1000 > config['stale_ms'])
        if not mask:
            self.clear()
        elif smoothing == 0 or reset:
            self.values, self.raw, self.derivative = raw[:], raw[:], [0.]*len(raw)
        else:
            # smoothing_ms is the resting time constant, not a fixed output delay.
            minimum_cutoff = 1000. / (2.*math.pi*smoothing)
            derivative_alpha = self.alpha(1., dt)
            for i, value in enumerate(raw):
                if not mask & (1 << i):
                    continue
                velocity = (value-self.raw[i])/dt
                self.derivative[i] += derivative_alpha*(velocity-self.derivative[i])
                cutoff = minimum_cutoff + 4.*abs(self.derivative[i])
                self.values[i] += self.alpha(cutoff, dt)*(value-self.values[i])
            self.raw = raw[:]
        values = self.values[:] if mask else raw
        if mask:
            self.key, self.source_ns = key, source
        return dict(prediction, raw_values=raw, raw_channels=dict(prediction['channels']),
                    values=values, channels={name: values[i] for i, name in enumerate(CHANNELS)
                                              if mask & (1 << i)})
