"""Ordered capture plans referencing the existing prompts and demonstration assets."""
from copy import deepcopy
from face_prompts import prompt_plan

DEFAULT_PRESET = 'complete'
PRESETS = [
    dict(id='basics', name=dict(zh='基础嘴部', en='Mouth basics'),
         description=dict(zh='放松、开闭嘴、微笑、圆唇、鼓腮、下颌横移与伸舌。',
                          en='Relaxation, opening/closing, smiles, rounded lips, cheeks, jaw shifts and tongue extension.'),
         actions=['neutral', 'JawOpen', 'MouthClose', 'MouthSmileL', 'MouthSmileR',
                  'MouthPucker', 'MouthFunnel', 'CheekPuff', 'JawLeft', 'JawRight',
                  'TongueOut', 'neutral']),
    dict(id='tongue', name=dict(zh='舌头专项', en='Tongue movements'),
         description=dict(zh='短中长伸舌、左右上下、卷舌和舌尖弯曲对照。',
                          en='Extension levels, four directions, side roll and a tip-curl contrast.'),
         actions=['neutral', 'tongue_short', 'tongue_medium', 'tongue_long',
                  'TongueLeft', 'TongueRight', 'TongueUp', 'TongueDown',
                  'TongueRoll', 'negative_tip_curl_not_roll', 'neutral']),
    dict(id='lips', name=dict(zh='独立唇形', en='Independent lip movements'),
         description=dict(zh='上下唇横移、内卷、漏斗、噘嘴和单侧压唇。',
                          en='Upper/lower lip shifts, inward rolls, funnels, puckering and presses.'),
         actions=['neutral', 'MouthUpperLeft', 'MouthUpperRight', 'MouthLowerLeft',
                  'MouthLowerRight', 'MouthRollUpper', 'MouthRollLower',
                  'LipFunnelUpper', 'LipFunnelLower', 'MouthPucker',
                  'MouthPressL', 'MouthPressR', 'neutral']),
    dict(id='complete', name=dict(zh='完整引导', en='Complete guide'),
         description=dict(zh='全部命名控制、组合、伸舌档位、困难负例及重戴提示。',
                          en='All named controls, combinations, extension levels, hard negatives and refitting.'),
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
