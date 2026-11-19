"""Ordered capture plans referencing the existing prompts and demonstration assets."""
from copy import deepcopy
from datetime import datetime
import json
import re
from face_prompts import _step, prompt_plan
from face_schema import SCHEMA

DEFAULT_PRESET = 'complete'
SEALED_PRESET = 'sealed-c'
SEALED_JAW_PRESET = 'sealed-c-jaw'
SEALED_PROFILES = {'mouth': SEALED_PRESET, 'jaw': SEALED_JAW_PRESET}
PRESETS = [
    dict(id='basics', name=dict(zh='基础嘴部', en='Mouth basics'),
         description=dict(zh='放松、张嘴、闭唇、左右微笑、噘嘴、漏斗口型、鼓腮、下颌左右移和伸舌。',
                          en='Relax, open your jaw, close your lips, smile on each side, pucker, funnel, puff your cheeks, shift your jaw left and right, and extend your tongue.'),
         actions=['neutral', 'JawOpen', 'MouthClose', 'MouthSmileL', 'MouthSmileR',
                  'MouthPucker', 'MouthFunnel', 'CheekPuff', 'JawLeft', 'JawRight',
                  'TongueOut', 'neutral']),
    dict(id='tongue', name=dict(zh='舌头专项', en='Tongue movements'),
         description=dict(zh='短、中、长三档伸舌，向左右上下伸舌，卷舌，以及只弯起舌尖的动作。',
                          en='Short, medium and long extension, the four directions, tongue roll, and a tip-only curl.'),
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
         description=dict(zh='全部命名控制、组合动作、三档伸舌，以及舌头留在口内的口型、说话和舌尖上弯。',
                          en='Every named control, combined movements, three tongue lengths, and mouth shapes with the tongue inside, speaking and a tongue-tip curl.'),
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
    if preset_id == SEALED_PRESET:
        return sealed_preset()
    if preset_id == SEALED_JAW_PRESET:
        return sealed_jaw_preset()
    for preset in capture_presets():
        if preset['id'] == preset_id:
            return preset
    raise ValueError('Unknown capture preset: '+str(preset_id))


def sealed_preset():
    """Fixed TongueOut/JawOpen contrasts, using the existing demonstration media."""
    prompts = {step['id']: step for step in prompt_plan()}
    steps = [prompts['neutral']]
    for opening, zh_open, en_open in (
        ('small', '小幅张嘴', 'Open your jaw slightly'),
        ('large', '大幅张嘴', 'Open your jaw wide'),
    ):
        for level, zh, en, demo in (
            ('none', '舌头留在口内', 'keep your tongue inside your mouth', 'negative_wide_open'),
            ('weak', '只让舌尖刚超过唇缘', 'extend only the tip just past your lips', 'tongue_short'),
            ('medium', '舌头伸到舒适长度的一半左右', 'extend your tongue about halfway', f'jaw_tongue_{opening}'),
            ('strong', '舌头充分但舒适地向前伸', 'extend your tongue fully but comfortably', 'tongue_long'),
        ):
            steps.append(_step(f'c_{opening}_{level}', f'{zh_open}，{zh}', f'{en_open} and {en}',
                               demo_id=demo, hold_s=6. if level == 'none' else 3.))
    steps.extend(dict(prompts[key], labels={}, hold_s=hold) for key, hold in (
        ('negative_teeth', 6.), ('negative_speech', 8.), ('neutral', 4.)))
    steps[0]['labels'] = {}
    return dict(id=SEALED_PRESET, name=dict(zh='独立评估 C', en='Independent evaluation C'),
                description=dict(zh='两档开口分别做无、弱、中、强伸舌，再做露牙微笑和自然说话。开口大小以每步文字为准。',
                                 en='At each jaw opening, hold no, weak, medium and strong extension, then smile with teeth and speak naturally. Follow the step text for how far to open your jaw.'),
                steps=steps)


def sealed_jaw_preset():
    """Repeat small and large openings with different closed-lip contrasts."""
    actions = (
        ('closed_relaxed', '轻轻合上双唇，嘴部放松', 'Close your lips gently and relax your mouth', 'MouthClose'),
        ('open_small_1', '小幅张嘴，保持这个开口', 'Open your jaw slightly and hold that opening', 'JawOpen'),
        ('closed_pucker', '双唇保持贴合，向前噘嘴', 'Pucker forward, keeping your lips together', 'MouthPucker'),
        ('open_large_1', '舒适地大幅张嘴，保持这个开口', 'Open your jaw comfortably wide and hold that opening', 'JawOpen'),
        ('closed_smile_left', '双唇保持贴合，只抬起左嘴角微笑', 'Lift your left mouth corner in a smile, keeping your lips together', 'MouthSmileL'),
        ('open_small_2', '再次小幅张嘴，保持这个开口', 'Open your jaw slightly again and hold that opening', 'JawOpen'),
        ('closed_smile_right', '双唇保持贴合，只抬起右嘴角微笑', 'Lift your right mouth corner in a smile, keeping your lips together', 'MouthSmileR'),
        ('open_large_2', '再次舒适地大幅张嘴，保持这个开口', 'Open your jaw comfortably wide again and hold that opening', 'JawOpen'),
        ('closed_relaxed_end', '轻轻合上双唇，嘴部放松', 'Close your lips gently and relax your mouth', 'MouthClose'),
    )
    return dict(id=SEALED_JAW_PRESET,
                name=dict(zh='独立评估 C · 仅开闭口', en='Independent evaluation C · JawOpen only'),
                description=dict(zh='按文字做两次小开口和两次大开口，全程舌头留在口内。闭唇动作中双唇始终贴合。',
                                 en='Follow the text for two small and two large openings. Keep your tongue inside throughout, and keep your lips together during closed-lip actions.'),
                steps=[_step(f'c_jaw_{key}', zh, en, demo_id=demo) for key, zh, en, demo in actions])


def capture_continuation(root, session_id, *, allow_complete=False):
    """Build a new plan from unfinished positions, preserving the frozen order."""
    if not isinstance(session_id, str) or not re.fullmatch(r'[0-9a-f]{32}', session_id):
        raise ValueError('Continuation requires a local recording session ID')
    root = root.resolve()
    recording = (root/session_id).resolve()
    if recording != root/session_id:
        raise ValueError('Continuation must use the original local recording directory')
    manifest = json.loads((recording/'manifest.json').read_text(encoding='utf-8'))
    if (manifest.get('schema') != SCHEMA or manifest.get('session_id') != session_id or
            manifest.get('state') != 'stopped' or manifest.get('samples_complete') is not True or
            not manifest.get('capture_plan') or manifest.get('evaluation_only') or manifest.get('observability_role') == 'C'):
        raise ValueError('Continue only a stopped guided development recording')
    steps = (manifest.get('capture_plan') or {}).get('steps') or []
    index = manifest.get('guide_start_step', 0)
    resolved = set()
    with (recording/'events.jsonl').open(encoding='utf-8') as ledger:
        for line in ledger:
            event = json.loads(line)
            if event['kind'] != 'guide_action' or event['action'] not in ('confirm', 'skip', 'select_step'):
                continue
            if (not 0 <= index < len(steps) or event.get('step_index', index) != index or
                    event.get('prompt_id') != steps[index]['id']):
                raise ValueError('Recording events do not match the frozen guide order')
            if event['action'] == 'select_step':
                index = event['next_step_index']
            else:
                resolved.add(index)
                index += 1
    remaining = [step for i, step in enumerate(steps)
                 if i not in resolved and step['id'] not in ('negative_occluded', 'remount')]
    if not remaining:
        if allow_complete:
            return None
        raise ValueError('No unfinished actions in this recording')
    first = remaining[0]['title']
    preset = dict(id='continuation', name=dict(zh='续录剩余动作', en='Continue remaining actions'),
                  description=dict(zh=f'从「{first["zh"]}」开始，之前的录制会保留。',
                                   en=f'Starts at “{first["en"]}”. The previous recording is kept.'),
                  steps=remaining)
    return dict(preset=preset, session_id=session_id, person_id=manifest['person_id'],
                fit_id=manifest['fit_id'], source_group_id=manifest['source_group_id'],
                label_stage=manifest['label_stage'], role=manifest.get('observability_role'))


def latest_capture_continuation(root):
    if not root.is_dir():
        return None
    candidates = []
    for archive in root.iterdir():
        if not re.fullmatch(r'[0-9a-f]{32}', archive.name) or archive.is_symlink():
            continue
        try:
            manifest = json.loads((archive/'manifest.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if (manifest.get('schema') == SCHEMA and manifest.get('state') == 'stopped' and
                manifest.get('samples_complete') is True and manifest.get('capture_plan') and
                not manifest.get('evaluation_only') and manifest.get('observability_role') != 'C'):
            candidates.append((datetime.fromisoformat(manifest['created_utc']).timestamp(), archive.name))
    if candidates:
        return capture_continuation(root, max(candidates)[1], allow_complete=True)
    return None
