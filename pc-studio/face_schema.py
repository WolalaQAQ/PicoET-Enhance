"""Versioned whole-mouth regression contract; unknown targets are not zeros."""

SCHEMA = 'pico-mouth50-v1'
STOCK_CHANNELS = (
    'CheekPuff', 'CheekSquintL', 'CheekSquintR', 'JawForward', 'JawLeft',
    'JawOpen', 'JawRight', 'MouthClose', 'MouthDimpleL', 'MouthDimpleR',
    'MouthFrownL', 'MouthFrownR', 'MouthFunnel', 'MouthLeft', 'MouthLowerDownL',
    'MouthLowerDownR', 'MouthPressL', 'MouthPressR', 'MouthPucker', 'MouthRight',
    'MouthRollLower', 'MouthRollUpper', 'MouthShrugLower', 'MouthShrugUpper',
    'MouthSmileL', 'MouthSmileR', 'MouthStretchL', 'MouthStretchR',
    'MouthUpperUpL', 'MouthUpperUpR', 'NoseSneerL', 'NoseSneerR', 'TongueOut',
)
CHANNELS = STOCK_CHANNELS + (
    'TongueLeft', 'TongueRight', 'TongueUp', 'TongueDown',
    'CheekPuffLeft', 'CheekPuffRight', 'CheekSuckLeft', 'CheekSuckRight',
    'MouthTightenerLeft', 'MouthTightenerRight', 'TongueRoll',
    'MouthUpperLeft', 'MouthUpperRight', 'MouthLowerLeft', 'MouthLowerRight',
    'LipFunnelUpper', 'LipFunnelLower',
)
STOCK_SLOTS = (43, 29, 37, 25, 32, 7, 15, 5, 49, 23, 27, 40, 10, 24,
               50, 6, 20, 22, 34, 48, 18, 14, 17, 9, 19, 21, 33, 42, 39, 8, 1, 13, 51)
INDEX = {name: index for index, name in enumerate(CHANNELS)}
ALL_VALID = (1 << len(CHANNELS)) - 1
assert len(CHANNELS) == len(INDEX) == 50

EXTENDED_CHANNELS = CHANNELS[len(STOCK_CHANNELS):]
AGGREGATE_STOCK_CHANNELS = ('CheekPuff', 'MouthFunnel', 'MouthLeft', 'MouthRight')
AGGREGATE_CHILDREN = {
    'CheekPuff': ('CheekPuffLeft', 'CheekPuffRight'),
    'MouthFunnel': ('LipFunnelUpper', 'LipFunnelLower'),
    'MouthLeft': ('MouthUpperLeft', 'MouthLowerLeft'),
    'MouthRight': ('MouthUpperRight', 'MouthLowerRight'),
}
STOCK_AGGREGATE_PARENTS = {
    child: parent for parent, children in AGGREGATE_CHILDREN.items() for child in children
}
ROUTABLE_CHANNELS = tuple(name for name in CHANNELS if name not in AGGREGATE_STOCK_CHANNELS)
PURE_EXTENDED_CHANNELS = tuple(name for name in EXTENDED_CHANNELS if name not in STOCK_AGGREGATE_PARENTS)
STOCK_CAPABLE_CHANNELS = tuple(name for name in ROUTABLE_CHANNELS if name not in PURE_EXTENDED_CHANNELS)
CURATED_STOCK_TARGETS = (
    'JawOpen', 'MouthPucker', 'LipFunnelUpper', 'LipFunnelLower',
)
MOUTH_MODES = ('curated', 'hybrid', 'model_only', 'custom')
PRESET_MODES = ('curated', 'hybrid', 'model_only')
MOUTH_PRESETS = {
    'curated': {name: ('stock' if name in CURATED_STOCK_TARGETS else 'model') for name in ROUTABLE_CHANNELS},
    'hybrid': {name: ('stock' if name in STOCK_CAPABLE_CHANNELS else 'model') for name in ROUTABLE_CHANNELS},
    'model_only': {name: 'model' for name in ROUTABLE_CHANNELS},
}

assert len(ROUTABLE_CHANNELS) == 46
assert len(PURE_EXTENDED_CHANNELS) == 9
assert len(STOCK_CAPABLE_CHANNELS) == 37


def validate_channel_sources(channel_sources):
    """Validate an explicit per-morph-target source map and order it by ROUTABLE_CHANNELS."""
    if channel_sources is None:
        return None
    if not isinstance(channel_sources, dict) or any(not isinstance(k, str) for k in channel_sources):
        raise ValueError('channel_sources must be null or a mapping keyed by ROUTABLE_CHANNELS')
    unknown = set(channel_sources) - set(ROUTABLE_CHANNELS)
    if unknown:
        raise ValueError('channel_sources keys must belong to ROUTABLE_CHANNELS: ' + ', '.join(sorted(unknown)))
    pure_extended = set(PURE_EXTENDED_CHANNELS)
    for name, source in channel_sources.items():
        if name in pure_extended:
            if source not in ('model', 'off'):
                raise ValueError(f'{name} is missing in PICO stock and only supports model or off')
        elif source not in ('stock', 'model', 'off'):
            raise ValueError(f'Unsupported source for {name}: {source}')
    return {name: channel_sources[name] for name in ROUTABLE_CHANNELS if name in channel_sources}


def matching_preset(sources):
    """Return the matching preset name ('curated', 'hybrid', 'model_only') or 'custom'."""
    for mode in PRESET_MODES:
        if sources == MOUTH_PRESETS[mode]:
            return mode
    return 'custom'


def resolve_channel_sources(mouth_mode='curated', hybrid_channels=None, channel_sources=None):
    """Resolve the complete 46-target source table from a preset, legacy whitelist, and overrides."""
    base = dict(MOUTH_PRESETS[mouth_mode] if mouth_mode in MOUTH_PRESETS else MOUTH_PRESETS['curated'])
    if channel_sources is None and mouth_mode == 'hybrid' and hybrid_channels is not None:
        allowed = set(hybrid_channels)
        for name in STOCK_CAPABLE_CHANNELS:
            stock_key = STOCK_AGGREGATE_PARENTS.get(name, name)
            base[name] = 'stock' if stock_key in allowed else 'model'
    if channel_sources:
        base.update(channel_sources)
    return {name: base[name] for name in ROUTABLE_CHANNELS}
