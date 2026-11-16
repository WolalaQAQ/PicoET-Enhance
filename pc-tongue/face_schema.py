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
