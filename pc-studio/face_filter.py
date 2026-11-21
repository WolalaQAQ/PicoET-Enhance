"""Per-valid-channel One-Euro filtering on the image sensor clock."""
import math

from face_schema import CHANNELS


def _resolve_filter_params(config):
    if config.get('filter_enabled') is False:
        return False, 0., 0., 1.
    if 'filter_min_cutoff' in config:
        min_cutoff = float(config['filter_min_cutoff'])
        beta = float(config.get('filter_beta', 4.))
        d_cutoff = float(config.get('filter_d_cutoff', 1.))
        if min_cutoff <= 0:
            return False, 0., beta, d_cutoff
        return True, min_cutoff, beta, d_cutoff
    smoothing = float(config.get('smoothing_ms', 0.))
    if smoothing <= 0:
        return False, 0., 4., 1.
    return True, 1000. / (2. * math.pi * smoothing), 4., 1.


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
        enabled, min_cutoff, beta, d_cutoff = _resolve_filter_params(config)
        key = (header.session_id, mask, enabled, min_cutoff, beta, d_cutoff)
        source = header.mouth_sensor_ns
        dt = (source-self.source_ns)/1e9 if self.source_ns is not None else 0.
        reset = (key != self.key or dt <= 0 or dt*1000 > config['stale_ms'])
        if not mask:
            self.clear()
        elif not enabled or reset:
            self.values, self.raw, self.derivative = raw[:], raw[:], [0.]*len(raw)
        else:
            derivative_alpha = self.alpha(d_cutoff, dt)
            for i, value in enumerate(raw):
                if not mask & (1 << i):
                    continue
                velocity = (value-self.raw[i])/dt
                self.derivative[i] += derivative_alpha*(velocity-self.derivative[i])
                cutoff = min_cutoff + beta*abs(self.derivative[i])
                self.values[i] += self.alpha(cutoff, dt)*(value-self.values[i])
            self.raw = raw[:]
        values = self.values[:] if mask else raw
        if mask:
            self.key, self.source_ns = key, source
        return dict(prediction, raw_values=raw, raw_channels=dict(prediction['channels']),
                    values=values, channels={name: values[i] for i, name in enumerate(CHANNELS)
                                              if mask & (1 << i)})
