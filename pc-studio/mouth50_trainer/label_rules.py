"""Mouth50 v4 label rules. Capture windows, holdout membership and calibration are caller inputs."""
from collections.abc import Sequence
from copy import deepcopy

from face_schema import CHANNELS, INDEX as IDX

TONGUE_ALL = ("TongueOut", "TongueLeft", "TongueRight", "TongueUp", "TongueDown", "TongueRoll")
TONGUE_ALLOW_JAW = {"JawOpen", "MouthLowerDownL", "MouthLowerDownR", "MouthUpperUpL", "MouthUpperUpR", "MouthShrugLower"}

HIGH_SNR_RAW33 = (
    "JawOpen", "CheekPuff", "MouthFunnel", "MouthPucker",
    "MouthRollLower", "MouthLeft", "MouthRight", "MouthShrugLower",
    "MouthShrugUpper", "MouthSmileL", "MouthSmileR", "MouthUpperUpL",
    "MouthUpperUpR", "MouthLowerDownR", "MouthDimpleL", "MouthDimpleR",
)

COMPOSITE_PROMPTS = {
    "jaw_tongue_small", "jaw_tongue_large", "smile_puff", "funnel_both",
    "suck_both", "lips_opposed_a", "lips_opposed_b", "negative_teeth", "negative_speech",
}

SPEECH_WEAK_CHANNELS = {
    "JawOpen", "MouthFunnel", "MouthPucker", "MouthRollLower",
    "MouthShrugLower", "MouthShrugUpper", "MouthUpperUpL", "MouthUpperUpR", "MouthLowerDownR",
}


# Directional tongue TongueOut targets raised so turning tongue does not under-extend TongueOut
DIRECTIONAL_TONGUE_OUT_TARGETS = {
    "TongueLeft": 0.78,
    "TongueRight": 0.85,
    "TongueUp": 0.85,
    "TongueDown": 0.85,
    "tongue_right_up": 0.78,
    "tongue_right_down": 0.78,
    "tongue_left_down": 0.78,
    "tongue_left_up": 0.68,
    "TongueRoll": 0.65,
}

# Conflicting composite prompts excluded from training/val/KD/clips (kept in session_order for adjacency settling)
SKIPPED_PROMPTS = frozenset({"lips_opposed_a", "lips_opposed_b"})

# Native channels where calibrated raw33 >= 0.30 on non-tongue holds reflects genuine physical co-activation
# (mask orthogonal 0.0 instead of forcing false negatives; excludes all 18 curated-model targets & optical-noise channels)
PHYSICAL_COACT_GUARD_CHANNELS = frozenset({
    "JawLeft", "JawRight", "MouthLeft", "MouthRight",
    "MouthPressL", "MouthPressR", "MouthStretchL", "MouthStretchR",
    "MouthDimpleL", "MouthDimpleR", "MouthLowerDownL", "MouthLowerDownR",
})
DECOUPLED_ZERO_PAIRS = {
    "JawOpen": frozenset({"MouthUpperUpL", "MouthUpperUpR", "MouthLowerDownL", "MouthLowerDownR"}),
    "negative_wide_open": frozenset({"MouthUpperUpL", "MouthUpperUpR", "MouthLowerDownL", "MouthLowerDownR"}),
}

ISOLATED_POSE_POSITIVES = {
    "MouthLeft": {"MouthLeft": 0.90, "MouthUpperLeft": 0.85, "MouthLowerLeft": 0.85},
    "MouthRight": {"MouthRight": 0.90, "MouthUpperRight": 0.85, "MouthLowerRight": 0.85},
    "TongueLeft": {"TongueLeft": 0.85, "TongueOut": DIRECTIONAL_TONGUE_OUT_TARGETS["TongueLeft"]},
}
_SQUINT = ("CheekSquintL", "CheekSquintR")
_SNEER = ("NoseSneerL", "NoseSneerR")
_SUCK = ("CheekSuckLeft", "CheekSuckRight")
_ROLL = ("MouthRollLower", "MouthRollUpper")
_FROWN = ("MouthFrownL", "MouthFrownR")
_FUNNEL = ("MouthFunnel", "LipFunnelUpper", "LipFunnelLower")
_PUCKER_PROMPTS = ("MouthPucker", "negative_pucker_release")
_FUNNEL_PROMPTS = (*_FUNNEL, "funnel_both")

_NEGATIVE_GROUPS = (
    (("MouthClose", "MouthPucker", "negative_pucker_release", *_FROWN, *_ROLL,
      "MouthPressL", "MouthPressR", "MouthTightenerLeft", "MouthTightenerRight",
      *_FUNNEL, "TongueRoll"), _SUCK),
    ((*_SUCK, "suck_both"), _ROLL),
    (("MouthSmileL", "MouthSmileR", "negative_teeth"), ("CheekPuffLeft", "CheekPuffRight")),
    (("MouthPressL", "MouthPressR", "MouthClose", "CheekPuff", "CheekPuffLeft", "CheekPuffRight"), _FROWN),
    (_FUNNEL_PROMPTS, ("MouthPucker",)),
    (_PUCKER_PROMPTS, _FUNNEL),
    (("JawOpen", "negative_wide_open", "MouthPucker", "MouthFunnel", "funnel_both"), _SQUINT),
)
ISOLATED_POSE_NEGATIVES = {
    "MouthLeft": ("MouthUpperRight", "MouthLowerRight", *_SNEER, *_SQUINT),
    "MouthRight": ("MouthUpperLeft", "MouthLowerLeft", *_SNEER, *_SQUINT),
    "MouthUpperLeft": ("MouthUpperRight", "MouthLowerRight", "MouthLowerLeft", "JawRight"),
    "MouthUpperRight": ("MouthUpperLeft", "MouthLowerLeft", "MouthLowerRight", "JawLeft"),
    "MouthLowerLeft": ("MouthLowerRight", "MouthUpperLeft", "MouthUpperRight", "JawRight"),
    "MouthLowerRight": ("MouthLowerLeft", "MouthUpperLeft", "MouthUpperRight", "JawLeft"),
    "TongueLeft": ("TongueRight", "TongueUp", "TongueDown", "TongueRoll"),
}
for _prompts, _channels in _NEGATIVE_GROUPS:
    for _pid in _prompts:
        ISOLATED_POSE_NEGATIVES[_pid] = ISOLATED_POSE_NEGATIVES.get(_pid, ()) + _channels


def apply_v4_prompt_rules(pid: str, spec: dict) -> dict:
    """Apply channel relationships to a reviewed prompt spec, leaving its visibility and timing intact."""
    out = deepcopy(spec)
    if pid in ISOLATED_POSE_POSITIVES:
        out["pos"] = dict(ISOLATED_POSE_POSITIVES[pid])
    if pid == "MouthFrownL":
        out.setdefault("pos", {})[pid] = 0.70
    if pid in DIRECTIONAL_TONGUE_OUT_TARGETS:
        out.setdefault("pos", {})["TongueOut"] = DIRECTIONAL_TONGUE_OUT_TARGETS[pid]
    if pid in ISOLATED_POSE_NEGATIVES:
        base = () if pid == "TongueLeft" else out.get("neg", ())
        out["neg"] = list(dict.fromkeys((*base, *ISOLATED_POSE_NEGATIVES[pid])))
    if "coact" in out:
        blocked = set(DECOUPLED_ZERO_PAIRS.get(pid, ()))
        if pid in _FUNNEL_PROMPTS:
            blocked.add("MouthPucker")
        if pid in _PUCKER_PROMPTS:
            blocked.update(_FUNNEL)
        out["coact"] = set(out["coact"]) - blocked
    return out


def allowed_weak_channels_v3(pid: str, spec: dict) -> set[str]:
    """Scope calibrated raw33 supervision strictly to a prompt's target and verified physical co-activations (isolating PICO stock optical crosstalk)."""
    if pid == "negative_speech":
        return set(SPEECH_WEAK_CHANNELS)
    allowed = set(spec.get("pos", {}).keys()) & set(HIGH_SNR_RAW33)
    if not spec.get("tongue_safe_zero") or pid in ("JawOpen", "negative_wide_open", "jaw_only_pre"):
        allowed.add("JawOpen")
    if pid in ("CheekPuff", "CheekPuffLeft", "CheekPuffRight"):
        allowed.add("CheekPuff")
    if pid in ("smile_puff", "negative_teeth"):
        allowed.update({"MouthSmileL", "MouthSmileR"})
    if pid == "negative_teeth":
        allowed.update({"JawOpen", "MouthUpperUpL", "MouthUpperUpR"})
    if pid == "funnel_both" or pid.startswith("LipFunnel"):
        allowed.update({"MouthFunnel", "JawOpen"})
    if pid in ("MouthPucker", "negative_pucker_release"):
        allowed.add("MouthPucker")
    return allowed


def protects_physical_coactivation(pid, channel, spec, calibrated_raw33):
    """Keep an orthogonal target unknown when calibrated stock evidence indicates co-activation."""
    return (
        bool(spec.get("tongue_safe_zero"))
        and channel in PHYSICAL_COACT_GUARD_CHANNELS
        and channel not in DECOUPLED_ZERO_PAIRS.get(pid, ())
        and calibrated_raw33 is not None
        and float(calibrated_raw33[IDX[channel]]) >= 0.30
    )


def build_frame_labels(
    pid: str,
    phase: str,
    spec: dict,
    *,
    cal33: Sequence[float] | None = None,
    coact_map: dict[str, set[str]] | None = None,
    manual_targets: dict[str, float] | None = None,
    pucker_excluded: bool = False,
    ramp_targets: dict[str, float] | None = None,
    next_spec: dict | None = None,
    next_pid: str | None = None,
) -> tuple[list[float | None], list[float]]:
    """Build ordered targets and weights from a labeled phase and optional calibrated raw33.

    A value of None is unknown, not a supervised zero. Reviewed manual labels
    override prompt priors, calibrated stock supervision and inferred ramps.
    """
    coact_map = coact_map or {}
    if pid in SKIPPED_PROMPTS:
        raise ValueError(f"Excluded v4 prompt: {pid}")
    targets: list[float | None] = [None] * 50
    weights: list[float] = [0.0] * 50

    if phase == "neutral_or_released":
        key_idxs = {IDX["TongueOut"], IDX["JawOpen"], IDX["MouthSmileL"], IDX["MouthSmileR"], IDX["MouthLowerDownL"], IDX["CheekSquintL"]}
        for i in range(50):
            targets[i] = 0.0
            weights[i] = 2.0 if i in key_idxs else 1.2

    elif phase in ("jaw_only_pre", "post_retract_open_jaw"):
        for c in TONGUE_ALL:
            targets[IDX[c]] = 0.0
            weights[IDX[c]] = 3.5 if c == "TongueOut" else 1.8
        opposing = {
            "MouthSmileL", "MouthSmileR", "MouthFrownL", "MouthFrownR",
            "MouthClose", "MouthPucker", "CheekPuff", "MouthLowerDownL", "CheekSquintL",
        }
        for c in opposing:
            targets[IDX[c]] = 0.0
            weights[IDX[c]] = 1.5
        if cal33 is not None:
            # Remove 0.15 floor on tongue_sweep jaw_only_pre & post_retract_open_jaw
            if pid == "tongue_sweep":
                targets[IDX["JawOpen"]] = round(min(0.70, max(0.0, float(cal33[IDX["JawOpen"]]))), 4)
            else:
                targets[IDX["JawOpen"]] = round(min(0.90, max(0.15, float(cal33[IDX["JawOpen"]]))), 4)
        else:
            targets[IDX["JawOpen"]] = 0.28
        weights[IDX["JawOpen"]] = 1.8

    elif phase == "sweep_onset_left":
        if cal33 is not None:
            to_v = round(float(cal33[IDX["TongueOut"]]), 4)
            targets[IDX["TongueOut"]] = to_v
            weights[IDX["TongueOut"]] = 1.2
            targets[IDX["TongueLeft"]] = round(min(1.0, max(0.0, 0.85 * to_v)), 4)
            weights[IDX["TongueLeft"]] = 1.0
            targets[IDX["JawOpen"]] = round(float(cal33[IDX["JawOpen"]]), 4)
            weights[IDX["JawOpen"]] = 0.40
        for c in ("TongueRight", "TongueUp", "TongueDown", "TongueRoll"):
            targets[IDX[c]] = 0.0
            weights[IDX[c]] = 1.5
        for c in ("MouthSmileL", "MouthSmileR", "CheekPuff", "MouthPucker"):
            targets[IDX[c]] = 0.0
            weights[IDX[c]] = 1.4

    elif phase == "sweep_left":
        targets[IDX["TongueOut"]] = 0.80
        weights[IDX["TongueOut"]] = 2.2
        targets[IDX["TongueLeft"]] = 0.85
        weights[IDX["TongueLeft"]] = 2.2
        for c in ("TongueRight", "TongueUp", "TongueDown", "TongueRoll"):
            targets[IDX[c]] = 0.0
            weights[IDX[c]] = 2.0
        for c in ("MouthSmileL", "MouthSmileR", "CheekPuff", "MouthPucker"):
            targets[IDX[c]] = 0.0
            weights[IDX[c]] = 1.4
        if cal33 is not None:
            targets[IDX["JawOpen"]] = round(float(cal33[IDX["JawOpen"]]), 4)
            weights[IDX["JawOpen"]] = 0.25

    elif phase == "sweep_center":
        targets[IDX["TongueOut"]] = 0.90
        weights[IDX["TongueOut"]] = 2.2
        for c in ("TongueLeft", "TongueRight", "TongueUp", "TongueDown", "TongueRoll"):
            targets[IDX[c]] = 0.0
            weights[IDX[c]] = 1.8
        for c in ("MouthSmileL", "MouthSmileR", "CheekPuff", "MouthPucker"):
            targets[IDX[c]] = 0.0
            weights[IDX[c]] = 1.4
        if cal33 is not None:
            targets[IDX["JawOpen"]] = round(float(cal33[IDX["JawOpen"]]), 4)
            weights[IDX["JawOpen"]] = 0.25

    elif phase == "sweep_right":
        targets[IDX["TongueOut"]] = 0.85
        weights[IDX["TongueOut"]] = 2.2
        targets[IDX["TongueRight"]] = 0.85
        weights[IDX["TongueRight"]] = 2.2
        for c in ("TongueLeft", "TongueUp", "TongueDown", "TongueRoll"):
            targets[IDX[c]] = 0.0
            weights[IDX[c]] = 2.0
        for c in ("MouthSmileL", "MouthSmileR", "CheekPuff", "MouthPucker"):
            targets[IDX[c]] = 0.0
            weights[IDX[c]] = 1.4
        if cal33 is not None:
            targets[IDX["JawOpen"]] = round(float(cal33[IDX["JawOpen"]]), 4)
            weights[IDX["JawOpen"]] = 0.25

    elif phase == "sweep_retract":
        if cal33 is not None:
            targets[IDX["TongueOut"]] = round(float(cal33[IDX["TongueOut"]]), 4)
            weights[IDX["TongueOut"]] = 1.2
            targets[IDX["JawOpen"]] = round(float(cal33[IDX["JawOpen"]]), 4)
            weights[IDX["JawOpen"]] = 0.40
        for c in ("TongueRoll", "CheekPuff", "MouthPucker"):
            targets[IDX[c]] = 0.0
            weights[IDX[c]] = 1.2
        for c in ("MouthSmileL", "MouthSmileR"):
            targets[IDX[c]] = 0.0
            weights[IDX[c]] = 1.4

    elif phase == "carryover_release":
        # Re-attributed prompt k release tail inside prompt k+1 [0.0, t_carry_end]
        allowed_weak = allowed_weak_channels_v3(pid, spec)
        next_avoid = set()
        if next_spec is not None and next_pid is not None:
            next_avoid |= set(next_spec.get("pos", {}).keys())
            next_avoid |= (next_spec.get("coact") if "coact" in next_spec else coact_map.get(next_pid, set()))
            next_avoid |= allowed_weak_channels_v3(next_pid, next_spec)
            if not next_spec.get("tongue_safe_zero"):
                next_avoid |= set(TONGUE_ALL) | TONGUE_ALLOW_JAW

        # Safe tongue zeros only when both prev and next prompts are tongue_safe_zero
        if spec.get("tongue_safe_zero") and (next_spec is None or next_spec.get("tongue_safe_zero")):
            for c in TONGUE_ALL:
                targets[IDX[c]] = 0.0
                weights[IDX[c]] = 1.8 if c == "TongueOut" else 1.2

        # Safe opposing negatives that don't collide with next_pid
        neg_set = set(spec.get("neg", []))
        for c in neg_set:
            if c in next_avoid:
                continue
            targets[IDX[c]] = 0.0
            weights[IDX[c]] = max(weights[IDX[c]], 1.4 if c in ("MouthSmileL", "MouthSmileR") else 1.2)

        # Priority 2: Calibrated HIGH_SNR_RAW33 on prev_pid's allowed_weak
        if cal33 is not None and spec.get("vis") != "NONE":
            for ch in allowed_weak:
                ci = IDX[ch]
                if ch in neg_set or (ch == "MouthPucker" and pucker_excluded):
                    continue
                v = float(cal33[ci])
                targets[ci] = round(v, 4)
                weights[ci] = 0.65 if (0.05 < v < 0.80) else 0.40

        # Priority 3: Gated frozen-DINOv3 ramp targets for non-HIGH_SNR channels
        if ramp_targets:
            for ch, val in ramp_targets.items():
                if ch in HIGH_SNR_RAW33:
                    continue
                targets[IDX[ch]] = float(val)
                weights[IDX[ch]] = 0.80

    elif phase == "transition":
        pos_dict = spec.get("pos", {})
        coact = spec.get("coact") if "coact" in spec else coact_map.get(pid, set())
        allowed_weak = allowed_weak_channels_v3(pid, spec)
        if spec.get("tongue_safe_zero"):
            for c in TONGUE_ALL:
                targets[IDX[c]] = 0.0
                weights[IDX[c]] = 1.8 if c == "TongueOut" else 1.2
        else:
            for c in ("MouthSmileL", "MouthSmileR"):
                targets[IDX[c]] = 0.0
                weights[IDX[c]] = max(weights[IDX[c]], 1.4)
        neg_set = set(spec.get("neg", []))
        for c in neg_set:
            targets[IDX[c]] = 0.0
            weights[IDX[c]] = max(weights[IDX[c]], 1.4 if c in ("MouthSmileL", "MouthSmileR") else 1.2)
        if pid not in COMPOSITE_PROMPTS and pid != "tongue_sweep" and spec.get("vis") != "NONE":
            is_tongue_pose = not spec.get("tongue_safe_zero")
            for ci, ch in enumerate(CHANNELS):
                if ch in pos_dict or ch in neg_set or ch in coact or ch in allowed_weak or weights[ci] > 0.0:
                    continue
                if is_tongue_pose and (ch in TONGUE_ALLOW_JAW or ch in TONGUE_ALL):
                    continue
                if protects_physical_coactivation(pid, ch, spec, cal33):
                    continue
                targets[ci] = 0.0
                weights[ci] = 0.35

        # Priority 2: Calibrated HIGH_SNR_RAW33 (upgraded weights: 0.65 mid / 0.40 poles)
        if cal33 is not None and spec.get("vis") != "NONE":
            for ch in allowed_weak:
                ci = IDX[ch]
                if ch in neg_set or (ch == "MouthPucker" and pucker_excluded):
                    continue
                v = float(cal33[ci])
                targets[ci] = round(v, 4)
                weights[ci] = 0.65 if (0.05 < v < 0.80) else 0.40

        # Priority 3: Gated frozen-DINOv3 ramp targets for the 34 non-HIGH_SNR channels
        if ramp_targets:
            for ch, val in ramp_targets.items():
                if ch in HIGH_SNR_RAW33:
                    continue
                targets[IDX[ch]] = float(val)
                weights[IDX[ch]] = 0.80

    elif phase == "hold":
        coact = spec.get("coact") if "coact" in spec else coact_map.get(pid, set())
        pos_dict = spec.get("pos", {})
        neg_set = set(spec.get("neg", []))
        allowed_weak = allowed_weak_channels_v3(pid, spec)

        # 1. Target-scoped high-SNR weak targets on non-pos/non-neg channels
        if cal33 is not None and spec.get("vis") != "NONE":
            weak_w = 0.35 if (pid in COMPOSITE_PROMPTS or pid == "negative_speech") else 0.25
            for ch in allowed_weak:
                ci = IDX[ch]
                if ch in pos_dict or ch in neg_set or (ch == "MouthPucker" and pucker_excluded):
                    continue
                targets[ci] = round(float(cal33[ci]), 4)
                weights[ci] = weak_w

        # 2. Orthogonal zero ONLY on isolated single-pose holds outside target/coact/allowed_weak
        if pid not in COMPOSITE_PROMPTS and pid != "tongue_sweep" and spec.get("vis") != "NONE":
            is_tongue_pose = not spec.get("tongue_safe_zero")
            for ci, ch in enumerate(CHANNELS):
                if ch in pos_dict or ch in neg_set or ch in coact or ch in allowed_weak:
                    continue
                if is_tongue_pose and (ch in TONGUE_ALLOW_JAW or ch in TONGUE_ALL):
                    continue
                if protects_physical_coactivation(pid, ch, spec, cal33):
                    continue
                targets[ci] = 0.0
                weights[ci] = 0.45

        # 3. Tongue safe zeros
        if spec.get("tongue_safe_zero"):
            for c in TONGUE_ALL:
                if c == "TongueOut" and pid in ("JawOpen", "negative_wide_open"):
                    w_to = 3.5
                elif c == "TongueOut" and spec.get("hard_open_to_neg"):
                    w_to = 2.8
                else:
                    w_to = 1.6
                targets[IDX[c]] = 0.0
                weights[IDX[c]] = max(weights[IDX[c]], w_to)

        # 4. Explicit pose positives
        pos_w = 2.2 if spec.get("vis") == "CLEAR" else 1.8
        for ch, val in pos_dict.items():
            v = float(val)
            if ch in HIGH_SNR_RAW33 and cal33 is not None and not (ch == "MouthPucker" and pucker_excluded):
                if ch == "JawOpen" and pid in ("JawOpen", "negative_wide_open"):
                    v = 0.5 * v + 0.5 * max(0.75, float(cal33[IDX[ch]]))
                else:
                    floor_v = 0.55 if v >= 0.55 else 0.20 * v
                    v = 0.55 * v + 0.45 * max(floor_v, float(cal33[IDX[ch]]))
            targets[IDX[ch]] = round(v, 4)
            weights[IDX[ch]] = 3.0 if (pid in ("jaw_tongue_large", "jaw_tongue_small") and ch == "TongueOut") else pos_w

        # 5. Anti-crosstalk suppression on all tongue poses + wide-open smile/frown/stretch/upperup suppression
        if not spec.get("tongue_safe_zero"):
            for ch in ("MouthSmileL", "MouthSmileR", "MouthFrownL", "MouthFrownR"):
                targets[IDX[ch]] = 0.0
                weights[IDX[ch]] = max(weights[IDX[ch]], 2.0)
            for ch in ("CheekPuff", "CheekPuffLeft", "CheekPuffRight", "CheekSuckLeft", "CheekSuckRight", "MouthPucker", "CheekSquintL", "CheekSquintR"):
                targets[IDX[ch]] = 0.0
                weights[IDX[ch]] = max(weights[IDX[ch]], 1.6)
            for ch in ("MouthTightenerLeft", "MouthTightenerRight"):
                targets[IDX[ch]] = 0.0
                weights[IDX[ch]] = max(weights[IDX[ch]], 1.4)
        if pid in ("JawOpen", "negative_wide_open"):
            for ch in ("MouthSmileL", "MouthSmileR", "MouthFrownL", "MouthFrownR", "MouthStretchL", "MouthStretchR"):
                targets[IDX[ch]] = 0.0
                weights[IDX[ch]] = 3.0
            for ch in ("MouthUpperUpL", "MouthUpperUpR"):
                targets[IDX[ch]] = 0.0
                weights[IDX[ch]] = max(weights[IDX[ch]], 2.4)

        # 6. Explicit anatomical opposing negatives
        for ch in neg_set:
            targets[IDX[ch]] = 0.0
            weights[IDX[ch]] = max(weights[IDX[ch]], 2.0)

    for channel, value in (manual_targets or {}).items():
        targets[IDX[channel]] = float(value)
        weights[IDX[channel]] = 2.4
    return targets, weights
