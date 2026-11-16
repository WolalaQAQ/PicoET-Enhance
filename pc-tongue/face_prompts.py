"""Bilingual whole-mouth prompts. Instructions are proposals, never observations."""
from copy import deepcopy

from face_schema import CHANNELS, STOCK_CHANNELS


# All directions are the wearer's own, not a mirrored preview's.
POSES = {
    'CheekPuff': ('双侧轻轻鼓腮', 'Gently puff both cheeks'),
    'CheekSquintL': ('抬起左侧面颊，轻轻眯左眼', 'Raise your left cheek with a gentle left squint'),
    'CheekSquintR': ('抬起右侧面颊，轻轻眯右眼', 'Raise your right cheek with a gentle right squint'),
    'JawForward': ('下颌轻轻前伸，不用力咬牙', 'Gently move your jaw forward without clenching'),
    'JawLeft': ('下颌向自身左侧移动', 'Move your jaw to your own left'),
    'JawOpen': ('自然张开下颌，不伸舌', 'Open your jaw naturally without extending your tongue'),
    'JawRight': ('下颌向自身右侧移动', 'Move your jaw to your own right'),
    'MouthClose': ('下颌稍张开但双唇保持闭合', 'Lower your jaw slightly while keeping your lips together'),
    'MouthDimpleL': ('左嘴角向内收紧，做左侧酒窝', 'Draw your left mouth corner inward to make a dimple'),
    'MouthDimpleR': ('右嘴角向内收紧，做右侧酒窝', 'Draw your right mouth corner inward to make a dimple'),
    'MouthFrownL': ('左嘴角向下压', 'Lower your left mouth corner'),
    'MouthFrownR': ('右嘴角向下压', 'Lower your right mouth corner'),
    'MouthFunnel': ('双唇松开，做圆形漏斗口型', 'Part your lips into a rounded funnel shape'),
    'MouthLeft': ('整张嘴向自身左侧移', 'Move your whole mouth to your own left'),
    'MouthLowerDownL': ('左半下唇向下拉，露出下齿', 'Lower the left half of your lower lip to show lower teeth'),
    'MouthLowerDownR': ('右半下唇向下拉，露出下齿', 'Lower the right half of your lower lip to show lower teeth'),
    'MouthPressL': ('轻压左侧上下唇', 'Gently press the left sides of your lips together'),
    'MouthPressR': ('轻压右侧上下唇', 'Gently press the right sides of your lips together'),
    'MouthPucker': ('双唇向前噘起，做亲吻口型', 'Pucker both lips forward as for a kiss'),
    'MouthRight': ('整张嘴向自身右侧移', 'Move your whole mouth to your own right'),
    'MouthRollLower': ('只把下唇向口内卷', 'Roll only your lower lip inward'),
    'MouthRollUpper': ('只把上唇向口内卷', 'Roll only your upper lip inward'),
    'MouthShrugLower': ('下唇向上推，轻轻覆盖上唇', 'Push your lower lip upward toward and slightly over the upper lip'),
    'MouthShrugUpper': ('只把上唇向上耸起，不内卷', 'Raise your upper lip without rolling it inward'),
    'MouthSmileL': ('仅左嘴角轻轻微笑', 'Smile gently with only your left mouth corner'),
    'MouthSmileR': ('仅右嘴角轻轻微笑', 'Smile gently with only your right mouth corner'),
    'MouthStretchL': ('左嘴角水平向外拉，不向上笑', 'Stretch your left mouth corner outward without raising it'),
    'MouthStretchR': ('右嘴角水平向外拉，不向上笑', 'Stretch your right mouth corner outward without raising it'),
    'MouthUpperUpL': ('只抬起左半上唇，露出上齿', 'Raise only the left half of your upper lip to show upper teeth'),
    'MouthUpperUpR': ('只抬起右半上唇，露出上齿', 'Raise only the right half of your upper lip to show upper teeth'),
    'NoseSneerL': ('轻皱左鼻翼', 'Gently wrinkle the left side of your nose'),
    'NoseSneerR': ('轻皱右鼻翼', 'Gently wrinkle the right side of your nose'),
    'TongueOut': ('舌头直向前伸到舒适长度', 'Extend your tongue straight forward to a comfortable length'),
    'TongueLeft': ('舌头向自身左侧伸出', 'Extend your tongue to your own left'),
    'TongueRight': ('舌头向自身右侧伸出', 'Extend your tongue to your own right'),
    'TongueUp': ('舌体朝上伸，不只是弯起舌尖', 'Extend your tongue upward, not just curl the tip'),
    'TongueDown': ('舌体朝下伸', 'Extend your tongue downward'),
    'CheekPuffLeft': ('只鼓左腮，右侧尽量放松', 'Puff only your left cheek and relax the right'),
    'CheekPuffRight': ('只鼓右腮，左侧尽量放松', 'Puff only your right cheek and relax the left'),
    'CheekSuckLeft': ('只轻吸左腮，不内卷嘴唇', 'Gently suck in only your left cheek without rolling your lips'),
    'CheekSuckRight': ('只轻吸右腮，不内卷嘴唇', 'Gently suck in only your right cheek without rolling your lips'),
    'MouthTightenerLeft': ('左嘴角水平收紧，区别于压唇或微笑', 'Tighten your left mouth corner horizontally, distinct from pressing or smiling'),
    'MouthTightenerRight': ('右嘴角水平收紧，区别于压唇或微笑', 'Tighten your right mouth corner horizontally, distinct from pressing or smiling'),
    'TongueRoll': ('舌头两侧向上卷成筒形，不只是舌尖上弯；做不到就跳过', 'Roll the sides of your tongue into a tube, not just curl the tip; skip if unable'),
    'MouthUpperLeft': ('只让上唇向自身左侧横移，下唇放松', 'Move only your upper lip to your own left; relax the lower lip'),
    'MouthUpperRight': ('只让上唇向自身右侧横移，下唇放松', 'Move only your upper lip to your own right; relax the lower lip'),
    'MouthLowerLeft': ('只让下唇向自身左侧横移，尽量不移动下颌', 'Move only your lower lip to your own left without moving your jaw'),
    'MouthLowerRight': ('只让下唇向自身右侧横移，尽量不移动下颌', 'Move only your lower lip to your own right without moving your jaw'),
    'LipFunnelUpper': ('只把上唇外翻成漏斗，下唇放松；不是内卷或上提', 'Turn only your upper lip outward into a funnel; relax the lower lip; do not roll inward or raise it'),
    'LipFunnelLower': ('只把下唇外翻成漏斗，上唇放松；不是内卷或上推', 'Turn only your lower lip outward into a funnel; relax the upper lip; do not roll inward or push up'),
}


def _step(key, zh, en, labels=None, *, kind='pose', hold_s=3., **extra):
    return dict(id=key, title=dict(zh=zh, en=en), kind=kind, labels=dict(labels or {}), hold_s=hold_s,
                reaction_s=1., tail_s=.3, skippable=True,
                prepare=dict(zh=f'准备：{zh}。按自身左右；不适或做不到可跳过。准备好后确认开始。',
                             en=f'Prepare: {en}. Use your own left/right. Skip if uncomfortable or unable. Confirm when ready.'),
                hold=dict(zh=f'保持：{zh}。稳定后再结束保持；计时只统计连续新图，不能证明动作正确。',
                          en=f'Hold: {en}. Finish after holding steadily. Timing counts continuous new frames, not pose correctness.'),
                confirm=dict(zh='确认本次动作是否完成；查看图像后只审核确实可见的通道。未观察、过渡或做不到的项目保持未知。',
                             en='Confirm whether you performed the action. Review images and label only observed channels. Leave unobserved, transitional or unachievable actions unknown.'),
                skip=dict(zh='跳过本阶段，保留原始记录，不生成动作真值。',
                          en='Skip this stage; keep original records without creating pose ground truth.'), **extra)


def prompt_plan():
    """JSON-serializable plan for optional browser speech; no audio/device calls."""
    steps = [_step('neutral', '自然放松嘴部，舌头收回', 'Relax your mouth naturally with your tongue retracted',
                   {'TongueOut': 0.}, kind='negative')]
    steps += [_step(name, *POSES[name], {name: 1.}) for name in CHANNELS]
    for name, value, zh, en in (
        ('short', .25, '舌尖刚超过唇缘，短伸舌', 'Extend your tongue just past your lips'),
        ('medium', .5, '舌头向前伸到个人舒适行程的一半', 'Extend your tongue to about half your comfortable range'),
        ('long', 1., '舌头充分但舒适地向前伸', 'Extend your tongue fully but comfortably')):
        steps.append(_step('tongue_'+name, zh, en, {'TongueOut': value}, intensity_rank=value))
    steps.append(_step('tongue_sweep', '缓慢短、中、长伸舌，再缓慢收回，重复一次',
                       'Slowly extend short, medium, long, then retract; repeat once', kind='continuous', hold_s=6.))
    for side, vertical, zh, en in (
        ('Left', 'Up', '左上', 'up-left'), ('Right', 'Up', '右上', 'up-right'),
        ('Left', 'Down', '左下', 'down-left'), ('Right', 'Down', '右下', 'down-right')):
        steps.append(_step('tongue_'+side.lower()+'_'+vertical.lower(), f'舌头向自身{zh}方伸出',
                           f'Extend your tongue {en}', {'Tongue'+side: .7, 'Tongue'+vertical: .7}, kind='combination'))
    for name, zh, en, labels in (
        ('lips_opposed_a', '上唇左移、下唇右移', 'Move upper lip left and lower lip right', {'MouthUpperLeft': 1., 'MouthLowerRight': 1.}),
        ('lips_opposed_b', '上唇右移、下唇左移', 'Move upper lip right and lower lip left', {'MouthUpperRight': 1., 'MouthLowerLeft': 1.}),
        ('funnel_both', '上下唇同时外翻成漏斗', 'Funnel upper and lower lips outward together', {'LipFunnelUpper': 1., 'LipFunnelLower': 1.}),
        ('suck_both', '双侧轻吸腮，不内卷唇', 'Gently suck both cheeks inward without rolling lips', {'CheekSuckLeft': 1., 'CheekSuckRight': 1.}),
        ('jaw_tongue_small', '小幅张嘴并中等伸舌', 'Open your jaw slightly and extend your tongue halfway', {'TongueOut': .5}),
        ('jaw_tongue_large', '大幅张嘴并保持相同中等伸舌长度', 'Open your jaw widely with the same halfway tongue extension', {'TongueOut': .5}),
        ('smile_puff', '微笑并轻鼓左腮', 'Smile and gently puff your left cheek', {'CheekPuffLeft': .5}),
    ):
        steps.append(_step(name, zh, en, labels, kind='combination'))
    for name, zh, en in (
        ('teeth', '露牙微笑，不伸舌', 'Smile showing teeth without extending your tongue'),
        ('wide_open', '大张嘴但舌头留在口内', 'Open your mouth widely while keeping your tongue inside'),
        ('pucker_release', '噘嘴再放松，不伸舌', 'Pucker and release without extending your tongue'),
        ('speech', '自然说一小段话，不刻意伸舌', 'Speak naturally without deliberately extending your tongue'),
        ('tip_curl_not_roll', '只弯起舌尖，不把舌侧卷成筒', 'Curl only the tongue tip without rolling the sides into a tube'),
        ('occluded', '嘴部被遮挡或不在图内时仅记录未知，不当成无舌', 'Record an occluded or out-of-view mouth as unknown, not as no tongue')):
        labels = ({'TongueRoll': 0.} if name == 'tip_curl_not_roll' else
                  {'TongueOut': 0.} if name in ('teeth', 'wide_open', 'pucker_release') else {})
        steps.append(_step('negative_'+name, zh, en, labels, kind='negative', hold_s=5.))
    steps.append(_step('remount', '结束本轮，取下并重新佩戴后新建录制会话，跨天再重复',
                       'Stop this recording, remove and refit the headset, then begin a new session; repeat on another day',
                       kind='remount', requires_new_session=True))
    return deepcopy(steps)


def coverage():
    return dict(channels=list(CHANNELS), stock_channels=list(STOCK_CHANNELS),
                covered=sorted({key for step in prompt_plan() for key in step['labels']}))
