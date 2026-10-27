"""Source-time tongue smoothing; absent and invalid end visual ownership."""
import math


class TongueState:
    def __init__(self):
        self.session = None
        self.source_ns = None
        self.visual = None
        self.visual_ns = None

    def clear(self):
        self.session = self.source_ns = self.visual = self.visual_ns = None

    def update(self, raw, header, config):
        if self.session != header.session_id:
            self.clear()
            self.session = header.session_id
        source_ns = header.mouth_sensor_ns
        state = raw.get('state') or ('present' if raw['present'] else 'absent')
        old_ns = self.source_ns
        self.source_ns = source_ns
        if state in ('invalid', 'stale'):
            self.visual = None
            self.visual_ns = None
            return self.invalid(state)
        if state == 'absent':
            self.visual = None
            self.visual_ns = None
            return {'state': 'absent', 'fresh': True, 'present': False,
                    'left': 0., 'right': 0., 'out': 0., 'override': True}
        if state == 'uncertain':
            hold_ns = int(config['uncertain_hold_ms']*1e6)
            if self.visual is not None and source_ns-self.visual_ns <= hold_ns:
                return dict(self.visual, state='uncertain', held=True)
            return self.invalid('uncertain')
        current = {key: float(raw[key]) for key in ('left', 'right', 'out')}
        smoothing = config['smoothing_ms']
        if self.visual is not None and old_ns is not None and source_ns > old_ns and smoothing > 0:
            alpha = 1.-math.exp(-(source_ns-old_ns)/(smoothing*1e6))
            for key in ('left', 'right'):
                current[key] = self.visual[key]+alpha*(current[key]-self.visual[key])
        current.update(state='present', fresh=True, present=True, override=True)
        self.visual, self.visual_ns = current, source_ns
        return dict(current)

    @staticmethod
    def invalid(reason='invalid'):
        return {'state': reason, 'fresh': False, 'present': False,
                'left': 0., 'right': 0., 'out': None, 'override': False}
