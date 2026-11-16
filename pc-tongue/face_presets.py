"""Ordered capture plans referencing the existing prompts and demonstration assets."""
from copy import deepcopy
from face_prompts import prompt_plan

DEFAULT_PRESET = 'complete'
PRESETS = [
    dict(id='basics', name=dict(zh='基础嘴部', en='Mouth basics'),
         description=dict(zh='放松、张嘴、闭唇、左右微笑、噘嘴、漏斗口型、鼓腮、下颌左右移和伸舌。',
                          en='Relax, open your jaw, close your lips, smile on each side, pucker, funnel, puff your cheeks, shift your jaw left and right, and extend your tongue.'),
         actions=['neutral', 'JawOpen', 'MouthClose', 'MouthSmileL', 'MouthSmileR',
                  'MouthPucker', 'MouthFunnel', 'CheekPuff', 'JawLeft', 'JawRight',
                  'TongueOut', 'neutral']),
    dict(id='tongue', name=dict(zh='舌头专项', en='Tongue movements'),
         description=dict(zh='短、中、长三档伸舌，向左右上下伸舌，卷舌，以及用来和卷舌对照的舌尖上弯。',
                          en='Short, medium and long extension, the four directions, tongue roll, and a tip curl to compare with the roll.'),
         actions=['neutral', 'tongue_short', 'tongue_medium', 'tongue_long',
                  'TongueLeft', 'TongueRight', 'TongueUp', 'TongueDown',
                  'TongueRoll', 'negative_tip_curl_not_roll', 'neutral']),
    dict(id='lips', name=dict(zh='独立唇形', en='Independent lip movements'),
         description=dict(zh='上唇和下唇分别左右横移，分别内卷，分别做漏斗口型，再加噘嘴和左右压唇。',
                          en='Move the upper and lower lips sideways separately, roll each lip inward, funnel each lip, then pucker and press each side.'),
         actions=['neutral', 'MouthUpperLeft', 'MouthUpperRight', 'MouthLowerLeft',
                  'MouthLowerRight', 'MouthRollUpper', 'MouthRollLower',
                  'LipFunnelUpper', 'LipFunnelLower', 'MouthPucker',
                  'MouthPressL', 'MouthPressR', 'neutral']),
    dict(id='complete', name=dict(zh='完整引导', en='Complete guide'),
         description=dict(zh='全部命名控制、组合动作、伸舌的三档长度、对照动作，以及最后的重新佩戴提示。',
                          en='Every named control, combined movements, three tongue-extension lengths, contrast movements, and a refit prompt at the end.'),
         actions=None),
]


def capture_presets():
    prompts = prompt_plan()
    by_id = {step['id']: step for step in prompts}
    result = []
    for definition in PRESETS:
        ids = definition['actions'] if definition['actions'] is not None else [step['id'] for step in prompts]
        result.append(dict(id=definition['id'], name=deepcopy(definition['name']),
                           description=deepcopy(definition['description']),
                           steps=[deepcopy(by_id[key]) for key in ids]))
    return result


def capture_preset(preset_id=DEFAULT_PRESET):
    for preset in capture_presets():
        if preset['id'] == preset_id:
            return preset
    raise ValueError('Unknown capture preset: '+str(preset_id))
