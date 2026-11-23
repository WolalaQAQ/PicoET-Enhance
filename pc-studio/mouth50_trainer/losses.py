"""Losses, KD masking, LR schedulers, and clean val71 composite scorer."""
import math
import numpy as np
import torch
import torch.nn.functional as F

from face_schema import CHANNELS


def continuous_regression_loss(logits, target, weight):
    """Channel-balanced squared regression, without focal/BCE or target-dependent sharpening."""
    error=(logits.sigmoid()-target).square()*weight
    counts=(weight>0).sum(0)
    active=counts>0
    if not active.any():
        return logits.sum()*0
    return (error.sum(0)[active]/counts[active]).mean()


def smooth_continuous_loss(
    logits: torch.Tensor,
    target: torch.Tensor,
    weight: torch.Tensor,
    pos_boost: torch.Tensor | None = None,
) -> torch.Tensor:
    """Channel-balanced smooth continuous regression (no midpoint sharpening, minimum strictly equals target)."""
    t_clamped = target.clamp(0.0, 1.0)
    bce = F.binary_cross_entropy_with_logits(logits, t_clamped, reduction="none")
    prob = torch.sigmoid(logits)
    reg = 0.75 * F.smooth_l1_loss(prob, t_clamped, beta=0.05, reduction="none") + 0.25 * (prob - t_clamped).square()
    el = 0.32 * bce + 0.68 * reg
    if pos_boost is not None:
        mild = pos_boost.clamp(1.0, 6.0).view(1, -1)
        eff_w = weight * (1.0 + (mild - 1.0) * t_clamped)
    else:
        eff_w = weight
    valid = eff_w > 0
    ch_w = torch.where(valid, eff_w, torch.zeros_like(eff_w)).sum(dim=0)
    ch_l = torch.where(valid, el * eff_w, torch.zeros_like(el)).sum(dim=0)
    active = ch_w > 0
    if not active.any():
        return logits.sum() * 0.0
    return (ch_l[active] / ch_w[active]).mean()


def logit_kd_loss(logits: torch.Tensor, soft_probs: torch.Tensor, kd_w: torch.Tensor) -> torch.Tensor:
    """Logit-space Huber distillation preserving subtle near-zero ordering without gradient blowup."""
    t_logits = torch.logit(soft_probs.clamp(2e-3, 0.998))
    err = F.smooth_l1_loss(logits.clamp(-7.0, 7.0), t_logits, beta=1.5, reduction="none")
    mask = (kd_w > 0).float()
    return (err * mask).sum() / mask.sum().clamp(min=1.0)


def build_continuous_kd_weights(w_b: torch.Tensor, t_b: torch.Tensor, sm_b: torch.Tensor) -> torch.Tensor:
    """Continuous KD weights: Teacher 5-TTA guides unlabeled slots (1.0), refines weak raw33 slots (0.65), and softens strong positives (0.25), while hard zeros stay anchored."""
    is_strict_zero = (w_b >= 0.34) & (t_b <= 0.02)
    is_unlabeled = w_b <= 0.01
    is_weak = (w_b > 0.01) & (w_b < 0.80)
    raw_kd_w = torch.where(
        is_unlabeled,
        torch.full_like(w_b, 1.0),
        torch.where(is_weak, torch.full_like(w_b, 0.65), torch.full_like(w_b, 0.25)),
    )
    return torch.where((sm_b > 0) & (~is_strict_zero), raw_kd_w, torch.zeros_like(w_b))


def compute_pos_boost(stats_path, max_boost: float = 6.0) -> torch.Tensor:
    """Square-root rarity weighting capped conservatively for smooth continuous balance."""
    import json
    from pathlib import Path
    stats = json.loads(Path(stats_path).read_text(encoding="utf-8"))["channels"]
    boost = torch.ones(50, dtype=torch.float32)
    for i, ch in enumerate(CHANNELS):
        s = stats[ch]
        if s["tr_pos"] > 0 and s["tr_neg"] > 0:
            ratio = math.sqrt(s["tr_neg"] / max(s["tr_pos"], 1))
            boost[i] = min(max_boost, max(1.0, ratio))
    return boost


def make_scheduler(opt, sched_type: str, epochs: int, steps_per_ep: int):
    total = epochs * steps_per_ep
    warmup = max(40, 2 * steps_per_ep)
    if sched_type == "wsd":
        stable_end = max(warmup + 1, int(0.38 * total))
        def lr_fn(st):
            if st < warmup:
                return (st + 1) / warmup
            if st < stable_end:
                return 1.0
            prog = min(1.0, (st - stable_end) / max(1, total - stable_end))
            return 0.015 + 0.985 * 0.5 * (1.0 + math.cos(math.pi * prog))
        return torch.optim.lr_scheduler.LambdaLR(opt, lr_fn)
    if sched_type == "restarts":
        c1 = int(0.62 * total)
        def lr_fn(st):
            if st < warmup:
                return (st + 1) / warmup
            if st < c1:
                p = (st - warmup) / max(1, c1 - warmup)
                return 0.05 + 0.95 * 0.5 * (1.0 + math.cos(math.pi * p))
            p = min(1.0, (st - c1) / max(1, total - c1))
            return 0.015 + (0.35 - 0.015) * 0.5 * (1.0 + math.cos(math.pi * p))
        return torch.optim.lr_scheduler.LambdaLR(opt, lr_fn)
    def lr_fn(st):
        if st < warmup:
            return (st + 1) / warmup
        p = min(1.0, (st - warmup) / max(1, total - warmup))
        return 0.015 + 0.985 * 0.5 * (1.0 + math.cos(math.pi * p))
    return torch.optim.lr_scheduler.LambdaLR(opt, lr_fn)


@torch.inference_mode()
def score_val_continuous(pv: np.ndarray, val_rows: list[dict]) -> dict:
    """Continuous-calibration validation scorer rewarding calibrated separation, low mid-range MAE, and low open/speech tongue FP."""
    seps, cal_seps, maes, fps, mid_errs = [], [], [], [], []
    for ci, _ch in enumerate(CHANNELS):
        pairs = [
            (float(r["targets"][ci]), float(pv[ri, ci]))
            for ri, r in enumerate(val_rows)
            if r["targets"][ci] is not None and r["weights"][ci] >= 0.8
        ]
        if pairs:
            ts, ps = np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])
            maes.append(float(np.mean(np.abs(ps - ts))))
            pm, nm = ts >= 0.45, ts <= 0.10
            if pm.any() and nm.any():
                sep = float(np.median(ps[pm]) - np.median(ps[nm]))
                t_med = float(np.median(ts[pm]))
                seps.append(sep)
                cal_seps.append(min(sep, t_med))
                fps.append(float(np.mean(ps[nm] >= 0.20)))
        for ri, r in enumerate(val_rows):
            t_val, w_val = r["targets"][ci], r["weights"][ci]
            if t_val is not None and w_val >= 0.25 and 0.08 <= float(t_val) <= 0.72:
                mid_errs.append(abs(float(pv[ri, ci]) - float(t_val)))

    mean_sep = float(np.mean(seps)) if seps else 0.0
    cal_sep = float(np.mean(cal_seps)) if cal_seps else 0.0
    mean_fp = float(np.mean(fps)) if fps else 0.0
    mean_mae = float(np.mean(maes)) if maes else 1.0
    mid_mae = float(np.mean(mid_errs)) if mid_errs else mean_mae

    to_pairs = [
        (float(r["targets"][32]), float(pv[ri, 32]))
        for ri, r in enumerate(val_rows)
        if r["targets"][32] is not None and r["weights"][32] >= 0.8
    ]
    if to_pairs:
        ts = np.array([p[0] for p in to_pairs])
        ps = np.array([p[1] for p in to_pairs])
        pm, nm = ts >= 0.45, ts <= 0.10
        to_sep = float(np.median(ps[pm]) - np.median(ps[nm])) if (pm.any() and nm.any()) else 0.0
    else:
        to_sep = 0.0
    wo_idx = [ri for ri, r in enumerate(val_rows) if r["prompt_id"] in ("negative_wide_open", "JawOpen") and r["phase"] == "hold"]
    wo_to = float(np.median(pv[wo_idx, 32])) if wo_idx else 0.0
    sw_idx = [ri for ri, r in enumerate(val_rows) if r["prompt_id"] == "tongue_sweep_neu"]
    sw_to = float(np.median(pv[sw_idx, 32])) if sw_idx else 0.0
    ns_idx = [ri for ri, r in enumerate(val_rows) if r["prompt_id"] == "negative_speech" and r["phase"] == "hold"]
    ns_to = float(np.max(pv[ns_idx, 32])) if ns_idx else 0.0

    score = cal_sep - 1.2 * mean_mae - 1.0 * mid_mae - 1.2 * mean_fp - 0.40 * wo_to - 0.35 * sw_to - 0.15 * ns_to
    return dict(
        score=score, cal_sep=cal_sep, mean_sep=mean_sep, mean_mae=mean_mae, mid_mae=mid_mae,
        mean_fp=mean_fp, to_sep=to_sep, wo_to=wo_to, sw_to=sw_to, ns_to=ns_to,
        ch_ge_04=sum(1 for s in seps if s >= 0.4), ch_ge_06=sum(1 for s in seps if s >= 0.6),
    )


def debinarized_continuous_loss(
    logits: torch.Tensor,
    target: torch.Tensor,
    weight: torch.Tensor,
    logit_reg_w: float = 2e-4,
) -> torch.Tensor:
    """Pure probability-space 3-bin stratified continuous regression (zero BCE, zero target-dependent pos_boost).

    Supports both (B, C) and (B, T, C) inputs. Scaling all weights by any positive constant c > 0 leaves
    the loss strictly unchanged.
    """
    if logits.ndim > 2:
        c = logits.shape[-1]
        logits = logits.reshape(-1, c)
        target = target.reshape(-1, c)
        weight = weight.reshape(-1, c)

    t_clamped = target.clamp(0.0, 1.0)
    prob = torch.sigmoid(logits)
    el = 0.75 * F.smooth_l1_loss(prob, t_clamped, beta=0.04, reduction="none") + 0.25 * (prob - t_clamped).square()

    valid = weight > 0
    if not valid.any():
        return logits.sum() * 0.0

    zeros_like_w = torch.zeros_like(weight)
    bin_masks = (
        (t_clamped <= 0.05, 0.35),
        ((t_clamped > 0.05) & (t_clamped < 0.72), 0.35),
        (t_clamped >= 0.72, 0.30),
    )

    ch_num = torch.zeros(logits.shape[-1], device=logits.device, dtype=logits.dtype)
    pi_sum = torch.zeros(logits.shape[-1], device=logits.device, dtype=logits.dtype)
    for mask_k, pi_k in bin_masks:
        m = valid & mask_k
        w_k = torch.where(m, weight, zeros_like_w)
        sum_w_k = w_k.sum(dim=0)
        has_k = sum_w_k > 0
        mean_k = torch.where(has_k, (w_k * el).sum(dim=0) / sum_w_k.clamp(min=1e-12), torch.zeros_like(sum_w_k))
        ch_num = ch_num + torch.where(has_k, pi_k * mean_k, torch.zeros_like(mean_k))
        pi_sum = pi_sum + torch.where(has_k, torch.full_like(sum_w_k, pi_k), torch.zeros_like(sum_w_k))

    active = pi_sum > 0
    if not active.any():
        return logits.sum() * 0.0
    reg_loss = (ch_num[active] / pi_sum[active]).mean()

    if logit_reg_w > 0.0:
        valid_f = valid.to(dtype=logits.dtype)
        logit_pen = F.relu(logits.abs() - 4.5).square()
        logit_reg = (logit_pen * valid_f).sum() / valid_f.sum().clamp(min=1.0)
        return reg_loss + logit_reg_w * logit_reg
    return reg_loss


def build_soft_kd_weights(
    w_b: torch.Tensor,
    t_b: torch.Tensor,
    sm_b: torch.Tensor,
    kd_conf: torch.Tensor | None = None,
) -> torch.Tensor:
    """De-binarized soft KD weights with strict verified-zero anchoring and optional continuous TTA confidence."""
    is_strict_zero = (w_b >= 0.80) & (t_b <= 0.02)
    is_unlabeled = w_b <= 0.01
    is_weak = (w_b > 0.01) & (w_b < 0.80)
    raw_kd_w = torch.where(
        is_unlabeled,
        torch.full_like(w_b, 1.0),
        torch.where(is_weak, torch.full_like(w_b, 0.50), torch.full_like(w_b, 0.15)),
    )
    if kd_conf is not None:
        raw_kd_w = raw_kd_w * kd_conf.to(device=w_b.device, dtype=w_b.dtype).clamp(min=0.0)
    return torch.where((sm_b > 0) & (~is_strict_zero), raw_kd_w, torch.zeros_like(w_b))


def clip_temporal_loss(
    probs_seq: torch.Tensor,
    target_seq: torch.Tensor,
    weight_seq: torch.Tensor,
    vel_w: float = 0.60,
    acc_w: float = 0.15,
    mono_w: float = 0.15,
    reset_mask: torch.Tensor | None = None,
) -> torch.Tensor:
    """Temporal velocity matching, second-order smoothness, and ramp monotonicity loss on (B, T, C) clips."""
    if probs_seq.ndim != 3 or probs_seq.shape[1] < 2:
        return probs_seq.sum() * 0.0

    t_clamped = target_seq.clamp(0.0, 1.0)
    dp = probs_seq[:, 1:] - probs_seq[:, :-1]
    dt = t_clamped[:, 1:] - t_clamped[:, :-1]
    both_valid = (weight_seq[:, 1:] > 0) & (weight_seq[:, :-1] > 0)

    if reset_mask is not None:
        r_b = reset_mask.bool()
        no_reset_step = (~r_b[:, 1:]).unsqueeze(-1)
        both_valid = both_valid & no_reset_step
    else:
        no_reset_step = None

    total = probs_seq.sum() * 0.0

    if vel_w > 0.0 and both_valid.any():
        vel_el = F.smooth_l1_loss(dp, dt, beta=0.03, reduction="none")
        m_f = both_valid.to(dtype=probs_seq.dtype)
        vel_loss = (vel_el * m_f).sum() / m_f.sum().clamp(min=1.0)
        total = total + vel_w * vel_loss

    if acc_w > 0.0 and probs_seq.shape[1] >= 3:
        d2p = probs_seq[:, 2:] - 2.0 * probs_seq[:, 1:-1] + probs_seq[:, :-2]
        acc_el = F.smooth_l1_loss(d2p, torch.zeros_like(d2p), beta=0.02, reduction="none")
        if reset_mask is not None:
            r_b = reset_mask.bool()
            no_reset_acc = ((~r_b[:, 1:-1]) & (~r_b[:, 2:])).unsqueeze(-1).expand_as(acc_el)
            if no_reset_acc.any():
                a_f = no_reset_acc.to(dtype=probs_seq.dtype)
                acc_loss = (acc_el * a_f).sum() / a_f.sum().clamp(min=1.0)
                total = total + acc_w * acc_loss
        else:
            total = total + acc_w * acc_el.mean()

    if mono_w > 0.0:
        ramp_mask = both_valid & (dt.abs() >= 0.015)
        if ramp_mask.any():
            mono_el = F.relu(-dp * torch.sign(dt))
            rm_f = ramp_mask.to(dtype=probs_seq.dtype)
            mono_loss = (mono_el * rm_f).sum() / rm_f.sum().clamp(min=1.0)
            total = total + mono_w * mono_loss

    return total


@torch.inference_mode()
def score_val_continuous_v2(pv: np.ndarray, val_rows: list[dict]) -> dict:
    """De-binarized validation scorer with macro mid-MAE, mid occupancy gap, and smile/tongue leak diagnostics."""
    base = score_val_continuous(pv, val_rows)
    ch_mid_maes = []
    active_t = []
    active_p = []
    for ci, _ch in enumerate(CHANNELS):
        ch_errs = []
        for ri, r in enumerate(val_rows):
            t_val, w_val = r["targets"][ci], r["weights"][ci]
            if t_val is None or w_val < 0.25:
                continue
            tf = float(t_val)
            pf = float(pv[ri, ci])
            if 0.08 <= tf <= 0.72:
                ch_errs.append(abs(pf - tf))
            if tf > 0.05:
                active_t.append(tf)
                active_p.append(pf)
        if ch_errs:
            ch_mid_maes.append(float(np.mean(ch_errs)))

    mid_mae_macro = float(np.mean(ch_mid_maes)) if ch_mid_maes else float(base["mean_mae"])
    if active_t:
        at = np.asarray(active_t, dtype=np.float32)
        ap = np.asarray(active_p, dtype=np.float32)
        target_mid_frac = float(np.mean((at >= 0.12) & (at <= 0.75)))
        pred_mid_frac = float(np.mean((ap >= 0.12) & (ap <= 0.75)))
        mid_occ_gap = abs(pred_mid_frac - target_mid_frac)
    else:
        target_mid_frac = 0.0
        pred_mid_frac = 0.0
        mid_occ_gap = 0.0

    idx_to = CHANNELS.index("TongueOut")
    idx_tl = CHANNELS.index("TongueLeft")
    idx_tr = CHANNELS.index("TongueRight")
    idx_cpl = CHANNELS.index("CheekPuffLeft")

    smile_puff_idx = [
        ri for ri, r in enumerate(val_rows)
        if r["prompt_id"] in ("MouthSmileL", "MouthSmileR", "smile_puff", "negative_teeth") and r["phase"] == "hold"
    ]
    smile_puff_l_leak = float(np.median(pv[smile_puff_idx, idx_cpl])) if smile_puff_idx else 0.0

    tl_idx = [ri for ri, r in enumerate(val_rows) if r["prompt_id"] == "TongueLeft" and r["phase"] == "hold"]
    tongue_left_med = float(np.median(pv[tl_idx, idx_tl])) if tl_idx else 0.0

    tr_idx = [ri for ri, r in enumerate(val_rows) if r["prompt_id"] == "TongueRight" and r["phase"] == "hold"]
    tongue_right_med = float(np.median(pv[tr_idx, idx_tr])) if tr_idx else 0.0

    turn_pids = (
        "TongueLeft", "TongueRight", "TongueUp", "TongueDown",
        "tongue_left_up", "tongue_left_down", "tongue_right_up", "tongue_right_down",
    )
    turn_idx = [ri for ri, r in enumerate(val_rows) if r["prompt_id"] in turn_pids and r["phase"] == "hold"]
    tongue_turn_to_med = float(np.median(pv[turn_idx, idx_to])) if turn_idx else 0.0

    score_v2 = (
        base["cal_sep"]
        - 1.2 * base["mean_mae"]
        - 1.5 * mid_mae_macro
        - 0.25 * mid_occ_gap
        - 1.2 * base["mean_fp"]
        - 0.40 * base["wo_to"]
        - 0.35 * base["sw_to"]
        - 0.15 * base["ns_to"]
    )
    out = dict(base)
    out.update(
        score=score_v2,
        score_v1=base["score"],
        mid_mae_macro=mid_mae_macro,
        mid_ch_count=len(ch_mid_maes),
        mid_occ_gap=mid_occ_gap,
        target_mid_frac=target_mid_frac,
        pred_mid_frac=pred_mid_frac,
        smile_puff_l_leak=smile_puff_l_leak,
        tongue_left_med=tongue_left_med,
        tongue_right_med=tongue_right_med,
        tongue_turn_to_med=tongue_turn_to_med,
    )
    return out
