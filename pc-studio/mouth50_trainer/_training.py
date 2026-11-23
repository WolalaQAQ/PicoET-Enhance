"""Offline execution helpers for the Mouth50 training stages."""
import copy
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import numpy as np
import onnx
import onnxruntime as ort
import torch
from torch.utils.data import DataLoader, Dataset, TensorDataset, WeightedRandomSampler

from face_schema import CHANNELS, SCHEMA
from . import ROI_END
from . import eval_continuous as evaluation
from .augment import apply_teacher_mixup, augment_clip_ir_physical, augment_ir_physical, extract_teacher_roi400
from .losses import (
    build_soft_kd_weights, clip_temporal_loss, debinarized_continuous_loss,
    make_scheduler, score_val_continuous_v2,
)
from .models import StatefulGRUStudent50, VFTStudent50, VFTTeacher50, save_teacher_delta, teacher_from_saved


def preload_sup_rows(jsonl_path: Path):
    rows = _load_training_rows(jsonl_path)
    if not rows:
        raise ValueError(f"No usable rows in {jsonl_path}")
    imgs = []
    targets = np.zeros((len(rows), 50), dtype=np.float32)
    weights = np.zeros((len(rows), 50), dtype=np.float32)
    for idx, r in enumerate(rows):
        imgs.append(evaluation.load_gray400(r["image"]))
        for j in range(50):
            v, w = r["targets"][j], r["weights"][j]
            if v is not None and w > 0:
                targets[idx, j] = float(v)
                weights[idx, j] = float(w)
    return rows, torch.from_numpy(np.stack(imgs, axis=0))[:, None, :, :], torch.from_numpy(targets), torch.from_numpy(weights)


def export_student_onnx(model: VFTStudent50, out_onnx: Path, trained_channels: list[str], note: str) -> dict:
    model = copy.deepcopy(model).cpu().eval()
    model.use_logits = False
    out_onnx = Path(out_onnx)
    out_onnx.parent.mkdir(parents=True, exist_ok=True)
    with torch.inference_mode():
        torch.onnx.export(
            model,
            torch.zeros(1, 1, 400, 400),
            str(out_onnx),
            input_names=["frame"],
            output_names=["blendshapes"],
            opset_version=17,
            dynamo=False,
        )
    onnx.checker.check_model(str(out_onnx))
    sess = evaluation._cpu_ort_session(out_onnx)
    rng = torch.Generator().manual_seed(73)
    probes = [
        torch.zeros(1, 1, 400, 400),
        torch.rand(1, 1, 400, 400, generator=rng),
        torch.linspace(0, 1, 400 * 400).reshape(1, 1, 400, 400),
    ]
    max_err = 0.0
    with torch.inference_mode():
        for pr in probes:
            exp = model(pr).numpy()
            act = sess.run(["blendshapes"], {"frame": pr.numpy()})[0]
            max_err = max(max_err, float(np.max(np.abs(exp - act))))
    manifest = {
        "schema": SCHEMA,
        "channels": list(CHANNELS),
        "model_kind": f"{model.arch}-{model.head_kind}-w{model.net_width}-roi{model.roi_end}",
        "model_id": str(uuid.uuid4()),
        "width": 400,
        "height": 400,
        "trained_channels": _ordered_channels(trained_channels),
        "validated_channels": [],
        "valid_mask": 0,
        "candidate_only": True,
        "input_name": "frame",
        "output_name": "blendshapes",
        "max_onnx_abs_diff": max_err,
        "note": note,
    }
    out_onnx.with_suffix(".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _ordered_channels(channels):
    selected = set(channels)
    if unknown := selected - set(CHANNELS):
        raise ValueError(f"Unknown trained channels: {sorted(unknown)}")
    return [channel for channel in CHANNELS if channel in selected]


def _reject_sealed(value):
    if isinstance(value, dict):
        if value.get("evaluation_only") or value.get("observability_role") == "C":
            raise ValueError("Sealed C is not development material")
        for key in ("lineage", "source_manifests", "parents"):
            if key in value:
                _reject_sealed(value[key])
    elif isinstance(value, list):
        for item in value:
            _reject_sealed(item)


def _load_training_rows(path):
    rows = evaluation.load_rows(path)
    _reject_sealed(rows)
    return rows


def _check_disjoint(val_rows, train_paths):
    if {row["image"] for row in val_rows}.intersection(train_paths):
        raise ValueError("Training or KD images overlap val71")


def _training_device(requested):
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    return ("cuda" if torch.cuda.is_available() else "cpu") if requested == "auto" else requested


def _eval_options(args):
    return dict(xsession_json=args.xsession_json, refined_json=args.refined_json, trajectory_json=args.trajectory_json)


def git_info() -> dict:
    def run_git(*args):
        try:
            return subprocess.check_output(
                ["git", "-C", str(Path(__file__).resolve().parent), *args], encoding="utf-8", stderr=subprocess.DEVNULL,
            ).strip()
        except (FileNotFoundError, subprocess.CalledProcessError):
            return ""
    return {
        "commit": run_git("rev-parse", "--short=12", "HEAD"),
        "branch": run_git("branch", "--show-current"),
        "dirty": bool(run_git("status", "--porcelain")),
    }


def check_disk_headroom(path: Path, min_gb: float = 2.0) -> float:
    path = Path(path)
    probe = path if path.exists() else path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free_gb = shutil.disk_usage(probe).free / (1024.0 ** 3)
    if free_gb < min_gb:
        raise RuntimeError(f"Insufficient free disk space on {probe}: {free_gb:.2f} GB < {min_gb:.2f} GB")
    return free_gb


def preload_kd_rows_v3(jsonl_path: Path):
    """Load KD JSONL supporting optional per-slot kd_conf (defaults to 1.0 where soft_targets is present)."""
    rows = _load_training_rows(jsonl_path)
    if not rows:
        raise ValueError(f"No usable rows in {jsonl_path}")
    imgs = []
    targets = np.zeros((len(rows), 50), dtype=np.float32)
    weights = np.zeros((len(rows), 50), dtype=np.float32)
    soft = np.zeros((len(rows), 50), dtype=np.float32)
    soft_mask = np.zeros((len(rows), 50), dtype=np.float32)
    kd_conf = np.zeros((len(rows), 50), dtype=np.float32)
    for idx, r in enumerate(rows):
        imgs.append(evaluation.load_gray400(r["image"]))
        r_t = r["targets"]
        r_w = r["weights"]
        r_s = r.get("soft_targets") or [None] * 50
        r_c = r.get("kd_conf")
        for j in range(50):
            v, w = r_t[j], r_w[j]
            if v is not None and w > 0:
                targets[idx, j] = float(v)
                weights[idx, j] = float(w)
            sv = r_s[j]
            if sv is not None:
                soft[idx, j] = float(sv)
                soft_mask[idx, j] = 1.0
                kd_conf[idx, j] = float(r_c[j]) if (r_c is not None and r_c[j] is not None) else 1.0
    return (
        rows,
        torch.from_numpy(np.stack(imgs, axis=0))[:, None, :, :],
        torch.from_numpy(targets),
        torch.from_numpy(weights),
        torch.from_numpy(soft),
        torch.from_numpy(soft_mask),
        torch.from_numpy(kd_conf),
    )


def build_mid_aware_sampler(rows: list[dict], target_mid_share: float = 0.38, seed: int = 42) -> WeightedRandomSampler | None:
    """Oversample rows containing active mid-range targets (0.05 < t < 0.72) to ~target_mid_share of each epoch."""
    if not rows:
        return None
    is_mid = []
    for r in rows:
        has_mid = False
        for t_v, w_v in zip(r["targets"], r["weights"]):
            if t_v is not None and w_v > 0 and 0.05 < float(t_v) < 0.72:
                has_mid = True
                break
        is_mid.append(has_mid)
    n_mid = sum(is_mid)
    n_non = len(rows) - n_mid
    if n_mid == 0 or n_non == 0:
        return None
    cur_share = n_mid / len(rows)
    if cur_share >= target_mid_share:
        return None
    w_mid = (target_mid_share / (1.0 - target_mid_share)) * (n_non / n_mid)
    w_mid = min(4.0, max(1.0, w_mid))
    sample_weights = torch.tensor([w_mid if m else 1.0 for m in is_mid], dtype=torch.double)
    gen = torch.Generator().manual_seed(seed)
    return WeightedRandomSampler(sample_weights, num_samples=len(rows), replacement=True, generator=gen)


class KDStudentDatasetV3(Dataset):
    def __init__(self, imgs_u8, t, w, s, sm, kc):
        self.imgs, self.t, self.w, self.s, self.sm, self.kc = imgs_u8, t, w, s, sm, kc

    def __len__(self):
        return self.imgs.shape[0]

    def __getitem__(self, idx):
        return self.imgs[idx], self.t[idx], self.w[idx], self.s[idx], self.sm[idx], self.kc[idx]


def apply_kd_mixup_v3(x, t_b, w_b, s_b, sm_b, kc_b, beta: float = 0.30):
    perm = torch.randperm(x.shape[0], device=x.device)
    lam = float(np.random.beta(beta, beta))
    lam = max(lam, 1.0 - lam)
    if lam >= 0.97:
        return x, t_b, w_b, s_b, sm_b, kc_b
    x = lam * x + (1.0 - lam) * x[perm]
    both_sup = (w_b >= 0.20) & (w_b[perm] >= 0.20)
    full_a = torch.where(
        w_b >= 0.80,
        t_b,
        torch.where((w_b >= 0.20) & (sm_b > 0), 0.50 * t_b + 0.50 * s_b, torch.where(sm_b > 0, s_b, t_b)),
    )
    full_b = torch.where(
        w_b[perm] >= 0.80,
        t_b[perm],
        torch.where(
            (w_b[perm] >= 0.20) & (sm_b[perm] > 0),
            0.50 * t_b[perm] + 0.50 * s_b[perm],
            torch.where(sm_b[perm] > 0, s_b[perm], t_b[perm]),
        ),
    )
    valid_both = ((w_b >= 0.20) | (sm_b > 0)) & ((w_b[perm] >= 0.20) | (sm_b[perm] > 0))
    t_b = torch.where(both_sup, lam * t_b + (1.0 - lam) * t_b[perm], torch.zeros_like(t_b))
    w_b = torch.where(both_sup, torch.minimum(w_b, w_b[perm]), torch.zeros_like(w_b))
    s_b = torch.where(valid_both, lam * full_a + (1.0 - lam) * full_b, s_b)
    sm_b = torch.where(valid_both, torch.ones_like(sm_b), torch.zeros_like(sm_b))
    kc_b = torch.where(valid_both, torch.minimum(kc_b.clamp(min=0.5), kc_b[perm].clamp(min=0.5)), torch.zeros_like(kc_b))
    return x, t_b, w_b, s_b, sm_b, kc_b


def run_teacher(args):
    device = _training_device(args.device)
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    data_dir = Path(args.data_dir)
    ckpt_dir = Path(args.out_dir)
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    check_disk_headroom(ckpt_dir, min_gb=2.0)

    tr_rows, tr_imgs, tr_t, tr_w = preload_sup_rows(data_dir / "vft50_train.jsonl")
    va_rows, va_imgs, _, _ = preload_sup_rows(data_dir / "vft50_val71.jsonl")
    _check_disjoint(va_rows, (row["image"] for row in tr_rows))
    print(
        f"[v3 Teacher] data={data_dir} init={args.init_teacher} train={len(tr_rows)} val71={len(va_rows)} "
        f"ep={args.epochs} lr_bb={args.lr_backbone:.2e} lr_hd={args.lr_head:.2e}",
        flush=True,
    )

    init_path = Path(args.init_teacher) if args.init_teacher else None
    dino = dict(dino_repo=args.dino_repo, dino_weights=args.dino_weights)
    if init_path is not None:
        init_ckpt = torch.load(init_path, map_location="cpu", weights_only=True)
        model = teacher_from_saved(init_ckpt, **dino).to(device)
        # This stage trains on the full ROI, even when seeded from a cropped teacher.
        model.roi_end = ROI_END
    else:
        model = VFTTeacher50(unfreeze_last_n=0, **dino).to(device)

    model.set_unfreeze(args.unfreeze_last_n)

    @torch.inference_mode()
    def eval_t():
        model.eval()
        preds = []
        for i in range(0, len(va_imgs), 32):
            roi = extract_teacher_roi400(va_imgs[i : i + 32].to(device), augment=False)
            with torch.amp.autocast("cuda", dtype=torch.bfloat16, enabled=(device == "cuda")):
                preds.append(model(roi).float().cpu().numpy())
        pv = np.concatenate(preds, axis=0)
        return score_val_continuous_v2(pv, va_rows), pv

    sampler = build_mid_aware_sampler(tr_rows, target_mid_share=args.mid_share, seed=args.seed)
    loader = DataLoader(
        TensorDataset(tr_imgs, tr_t, tr_w),
        batch_size=args.batch_size,
        shuffle=(sampler is None),
        sampler=sampler,
    )
    opt = torch.optim.AdamW([
        {"params": [p for p in model.backbone.parameters() if p.requires_grad], "lr": args.lr_backbone, "weight_decay": 1e-4},
        {"params": list(model.head.parameters()), "lr": args.lr_head, "weight_decay": 1e-4},
    ])
    accum = max(1, args.grad_accum)
    total_steps = max(1, (args.epochs * len(loader) + accum - 1) // accum)
    warmup = max(20, len(loader) // accum)
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt,
        lambda st: (st + 1) / warmup if st < warmup else 0.05 + 0.95 * 0.5 * (1.0 + math.cos(math.pi * min(1.0, (st - warmup) / max(1, total_steps - warmup)))),
    )

    best_path = ckpt_dir / f"teacher_{args.out_tag}_best.pt"
    best_score = -1e9
    trainable_names = [n for n, p in model.named_parameters() if p.requires_grad]
    swa_delta = None
    swa_n = 0

    for ep in range(1, args.epochs + 1):
        model.train()
        opt.zero_grad(set_to_none=True)
        ep_loss, nb, t0 = 0.0, 0, time.perf_counter()
        for bi, (imgs_u8, t_b, w_b) in enumerate(loader):
            roi = extract_teacher_roi400(imgs_u8.to(device), augment=True)
            t_b, w_b = t_b.to(device), w_b.to(device)
            if args.mixup_p > 0.0 and torch.rand(1).item() < args.mixup_p and roi.shape[0] > 1:
                roi, t_b, w_b = apply_teacher_mixup(roi, t_b, w_b)
            with torch.amp.autocast("cuda", dtype=torch.bfloat16, enabled=(device == "cuda")):
                logits = model.forward_logits(roi)
                loss = debinarized_continuous_loss(logits.float(), t_b, w_b, logit_reg_w=args.logit_reg_w)
            (loss / accum).backward()
            if (bi + 1) % accum == 0 or (bi + 1) == len(loader):
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
                opt.zero_grad(set_to_none=True)
                sched.step()
            ep_loss += float(loss.detach())
            nb += 1

        m, _ = eval_t()
        print(
            f"[T-v3 Ep {ep:2d}/{args.epochs}] ({time.perf_counter()-t0:.1f}s) loss={ep_loss/nb:.5f} | "
            f"csep={m['cal_sep']:.3f} midM={m['mid_mae_macro']:.4f} occ={m['mid_occ_gap']:.3f} "
            f"mae={m['mean_mae']:.4f} fp={m['mean_fp']:.3f} wo={m['wo_to']:.3f} sw={m['sw_to']:.3f} "
            f"cpl={m['smile_puff_l_leak']:.3f} TL/TR={m['tongue_left_med']:.2f}/{m['tongue_right_med']:.2f} "
            f"sc={m['score']:.4f}",
            flush=True,
        )
        if m["score"] > best_score:
            best_score = m["score"]
            save_teacher_delta(model, best_path, metrics=m, stage="v3_debin", epoch=ep)
        swa_start = max(2, args.epochs - 1)
        if ep >= swa_start:
            pdict = dict(model.named_parameters())
            sd_now = {k: pdict[k].detach().to(device="cpu", dtype=torch.float32, copy=True) for k in trainable_names}
            if swa_delta is None:
                swa_delta = sd_now
            else:
                for k in trainable_names:
                    swa_delta[k] += sd_now[k]
            swa_n += 1

    saved = torch.load(best_path, map_location="cpu", weights_only=True)
    best_tag = f"e{saved['epoch']}"
    if swa_delta is not None and swa_n >= 2:
        for k in trainable_names:
            swa_delta[k] /= float(swa_n)
        model.load_state_dict(swa_delta, strict=False)
        m_swa, _ = eval_t()
        print(
            f"[T-v3 SWA-{swa_n}] csep={m_swa['cal_sep']:.3f} midM={m_swa['mid_mae_macro']:.4f} "
            f"occ={m_swa['mid_occ_gap']:.3f} sc={m_swa['score']:.4f} vs best={best_score:.4f}",
            flush=True,
        )
        if m_swa["score"] >= best_score:
            save_teacher_delta(model, best_path, metrics=m_swa, stage="v3_debin", epoch="swa")
            best_tag = "swa"
            saved = torch.load(best_path, map_location="cpu", weights_only=True)

    model.load_state_dict(saved["state_dict"], strict=saved["checkpoint_kind"] == "full")
    model.set_unfreeze(0)
    rep = evaluation.evaluate_model(
        model, "teacher", f"cont400_teacher_{args.out_tag}_{best_tag}",
        data_dir=data_dir, device=device, val_cache=va_imgs.float() / 255.0, **_eval_options(args),
    )
    eval_path = ckpt_dir / f"teacher_{args.out_tag}_eval.json"
    eval_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    (ckpt_dir / "run_meta.json").write_text(json.dumps({
        "stage": "teacher", "picked": best_tag, "git": git_info(), "args": vars(args),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved Teacher eval -> {eval_path}", flush=True)
    return rep


@torch.inference_mode()
def run_kd(args):
    device = _training_device(args.device)
    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = (Path(args.kd_jsonl) if args.kd_jsonl else out_dir / "vft50_train_kd.jsonl").resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    check_disk_headroom(out_path.parent, min_gb=1.0)
    val_rows = _load_training_rows(data_dir / "vft50_val71.jsonl")
    kd_rows = _load_training_rows(data_dir / "vft50_kd_pool.jsonl")
    _check_disjoint(val_rows, (row["image"] for row in kd_rows))

    ckpt_path = Path(args.teacher_ckpt)
    ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=True)
    model = teacher_from_saved(ckpt, dino_repo=args.dino_repo, dino_weights=args.dino_weights).to(device).eval()
    tau = float(args.disp_tau)
    if tau <= 0:
        raise ValueError("--disp-tau must be positive")
    max_drop = float(args.max_disp_drop)
    prefix = _kd_resume_prefix(out_path, kd_rows) if args.resume and out_path.is_file() else []
    start_idx = len(prefix)
    print(
        f"[v3 KD] 5-TTA soft KD from {ckpt_path.name} for {len(kd_rows)} frames "
        f"(start={start_idx}, bs={args.batch_size}, tau={tau}, max_disp_drop={max_drop}) -> {out_path}",
        flush=True,
    )

    counts = np.zeros(50, dtype=np.int64)
    conf_sum = np.zeros(50, dtype=np.float64)
    t0 = time.perf_counter()
    if prefix:
        # Preserve usable rows if inference was interrupted mid-line.
        tmp_path = out_path.with_suffix(".jsonl.tmp")
        tmp_path.write_text("".join(line + "\n" for line, _ in prefix), encoding="utf-8")
        tmp_path.replace(out_path)
        for _, row in prefix:
            counts += [value is not None for value in row["soft_targets"]]
            conf_sum += row["kd_conf"]
    with out_path.open("a" if prefix else "w", encoding="utf-8") as fout:
        for i in range(start_idx, len(kd_rows), args.batch_size):
            chunk = kd_rows[i : i + args.batch_size]
            arrs = [evaluation.load_gray400(r["image"]) for r in chunk]
            x_u8 = torch.from_numpy(np.stack(arrs, axis=0))[:, None, :, :].to(device)
            roi0 = extract_teacher_roi400(x_u8, augment=False, dx_const=0.0, dy_const=0.0)
            roi_xp = extract_teacher_roi400(x_u8, augment=False, dx_const=6.0, dy_const=0.0)
            roi_xm = extract_teacher_roi400(x_u8, augment=False, dx_const=-6.0, dy_const=0.0)
            with torch.amp.autocast("cuda", dtype=torch.bfloat16, enabled=(device == "cuda")):
                p0 = model(roi0).float().cpu().numpy()
                p_lo = model((roi0 * 0.85).clamp(0.0, 1.0)).float().cpu().numpy()
                p_hi = model((roi0 * 1.18).clamp(0.0, 1.0)).float().cpu().numpy()
                p_xp = model(roi_xp).float().cpu().numpy()
                p_xm = model(roi_xm).float().cpu().numpy()
            p_mean = (p0 + p_lo + p_hi + p_xp + p_xm) / 5.0
            disp = np.maximum.reduce([np.abs(p_lo - p0), np.abs(p_hi - p0), np.abs(p_xp - p0), np.abs(p_xm - p0)])
            conf = np.exp(-disp / tau)
            for k, r in enumerate(chunk):
                soft = [None] * 50
                kconf = [0.0] * 50
                for j in range(50):
                    if max_drop <= 0.0 or disp[k, j] <= max_drop:
                        soft[j] = round(float(p_mean[k, j]), 4)
                        c_val = round(float(conf[k, j]), 4)
                        kconf[j] = c_val
                        counts[j] += 1
                        conf_sum[j] += c_val
                row_out = dict(r)
                # Make a relocated KD output usable without assuming the original cwd.
                image = Path(r["image"])
                if image.drive == out_path.drive:
                    row_out["image"] = Path(os.path.relpath(image, out_path.parent)).as_posix()
                row_out["soft_targets"] = soft
                row_out["kd_conf"] = kconf
                fout.write(json.dumps(row_out, ensure_ascii=False) + "\n")
            fout.flush()
            if ((i - start_idx) // args.batch_size) % 80 == 0:
                print(f"  KD progress {min(i + args.batch_size, len(kd_rows))}/{len(kd_rows)} ({time.perf_counter()-t0:.1f}s)", flush=True)

    mean_conf = conf_sum / np.maximum(counts, 1)
    summary = {
        "teacher_ckpt": str(ckpt_path),
        "n_rows": len(kd_rows),
        "disp_tau": tau,
        "max_disp_drop": max_drop,
        "min_ch_cov": int(counts.min()),
        "max_ch_cov": int(counts.max()),
        "mean_kd_conf": round(float(mean_conf.mean()), 4),
    }
    (out_dir / "kd_summary.json").write_text(json.dumps({
        **summary, "git": git_info(), "args": vars(args),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[v3 KD] Done ({time.perf_counter()-t0:.1f}s): {json.dumps(summary)}", flush=True)
    return summary


def _kd_resume_prefix(path, source_rows):
    prefix = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                break
            if len(row.get("soft_targets", [])) != len(CHANNELS) or len(row.get("kd_conf", [])) != len(CHANNELS):
                break
            if (len(prefix) >= len(source_rows)
                    or str((path.parent / row["image"]).resolve()) != source_rows[len(prefix)]["image"]):
                raise ValueError("Cannot resume KD: output does not match the input image sequence")
            prefix.append((line, row))
    return prefix


def run_student(args):
    device = _training_device(args.device)
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    check_disk_headroom(out_dir, min_gb=2.0)
    gmeta = git_info()

    print(
        f"=== [v3 Student] {args.exp} git={gmeta['branch']}@{gmeta['commit']}{'*' if gmeta['dirty'] else ''} | "
        f"{args.arch}@{args.width} {args.head} roi={args.roi_end} ep={args.epochs} sched={args.sched} ema={args.ema_decay} ===",
        flush=True,
    )

    va_rows, va_imgs, _, _ = preload_sup_rows(data_dir / "vft50_val71.jsonl")
    kd_file = Path(args.kd_jsonl) if args.kd_jsonl else (data_dir / "vft50_train_kd.jsonl")
    tr_rows, tr_imgs, tr_t, tr_w, tr_s, tr_sm, tr_kc = preload_kd_rows_v3(kd_file)
    _check_disjoint(va_rows, (row["image"] for row in tr_rows))
    observed = tr_w > 0
    if args.kd_weight > 0:
        observed |= build_soft_kd_weights(tr_w, tr_t, tr_sm, tr_kc) > 0
    trained = {ch for ch, active in zip(CHANNELS, observed.any(dim=0)) if active}

    model = VFTStudent50(
        arch=args.arch,
        net_width=args.width,
        head_kind=args.head,
        roi_end=args.roi_end,
        load_pretrained=not (args.init_ckpt or args.random_init),
        use_logits=False,
        mnv3_weights=args.mnv3_weights,
    ).to(device)
    if args.init_ckpt:
        init_saved = torch.load(args.init_ckpt, map_location="cpu", weights_only=True)
        model.load_state_dict(init_saved["state_dict"])
        trained.update(init_saved.get("trained_channels", ()))

    ema_model = copy.deepcopy(model).eval()
    trainable_names = {n for n, p in model.named_parameters() if p.requires_grad}

    sampler = build_mid_aware_sampler(tr_rows, target_mid_share=args.mid_share, seed=args.seed)
    loader = DataLoader(
        KDStudentDatasetV3(tr_imgs, tr_t, tr_w, tr_s, tr_sm, tr_kc),
        batch_size=args.batch_size,
        shuffle=(sampler is None),
        sampler=sampler,
    )
    head_params = (
        list(model.head.parameters())
        + (list(model.proj.parameters()) if model.proj is not None else [])
        + (list(model.proj16.parameters()) if model.proj16 is not None else [])
    )
    opt = torch.optim.AdamW([
        {"params": list(model.features.parameters()), "lr": 4e-4 * args.lr_scale, "weight_decay": 1e-4},
        {"params": head_params, "lr": 1.2e-3 * args.lr_scale, "weight_decay": 1e-4},
    ])
    accum = max(1, args.grad_accum)
    steps_per_ep = max(1, (len(loader) + accum - 1) // accum)
    sched = make_scheduler(opt, args.sched, args.epochs, steps_per_ep)

    @torch.inference_mode()
    def eval_s(m_eval):
        m_eval.eval()
        preds = [m_eval((va_imgs[i : i + 64].float() / 255.0).to(device)).cpu().numpy() for i in range(0, len(va_imgs), 64)]
        pv = np.concatenate(preds, axis=0)
        return score_val_continuous_v2(pv, va_rows), pv

    best_score, best_path = -1e9, out_dir / "student_best.pt"
    top_candidates = []
    global_step = 0

    for ep in range(1, args.epochs + 1):
        model.train()
        opt.zero_grad(set_to_none=True)
        ep_loss, nb, t0 = 0.0, 0, time.perf_counter()
        for bi, (imgs_u8, t_b, w_b, s_b, sm_b, kc_b) in enumerate(loader):
            x = augment_ir_physical(imgs_u8.to(device), roi_end=args.roi_end)
            t_b, w_b = t_b.to(device), w_b.to(device)
            s_b, sm_b, kc_b = s_b.to(device), sm_b.to(device), kc_b.to(device)
            if args.mixup_p > 0.0 and x.shape[0] > 1 and torch.rand(1).item() < args.mixup_p:
                x, t_b, w_b, s_b, sm_b, kc_b = apply_kd_mixup_v3(x, t_b, w_b, s_b, sm_b, kc_b)

            with torch.amp.autocast("cuda", dtype=torch.bfloat16, enabled=(device == "cuda")):
                logits = model.forward_logits(x).float()

            loss_sup = debinarized_continuous_loss(logits, t_b, w_b, logit_reg_w=args.logit_reg_w)
            kd_w = build_soft_kd_weights(w_b, t_b, sm_b, kd_conf=kc_b)
            loss_kd = debinarized_continuous_loss(logits, s_b, kd_w, logit_reg_w=0.0)
            loss = loss_sup + args.kd_weight * loss_kd

            (loss / accum).backward()
            if (bi + 1) % accum == 0 or (bi + 1) == len(loader):
                torch.nn.utils.clip_grad_norm_(model.parameters(), 2.0)
                opt.step()
                opt.zero_grad(set_to_none=True)
                sched.step()
                global_step += 1
                d_cur = min(args.ema_decay, (1.0 + global_step) / (10.0 + global_step))
                with torch.no_grad():
                    ema_params = dict(ema_model.named_parameters())
                    for n, p in model.named_parameters():
                        if n in trainable_names:
                            ema_params[n].mul_(d_cur).add_(p.data, alpha=1.0 - d_cur)
                    ema_bufs = dict(ema_model.named_buffers())
                    for n, b in model.named_buffers():
                        if n in ema_bufs:
                            ema_bufs[n].copy_(b.data)
            ep_loss += float(loss.detach())
            nb += 1

        m_raw, _ = eval_s(model)
        m_ema, _ = eval_s(ema_model) if (ep >= 8 or ep in (1, 5)) else (None, None)
        cand_kind, cand_m, cand_sd = ("raw", m_raw, model.state_dict())
        if m_ema is not None and m_ema["score"] > m_raw["score"]:
            cand_kind, cand_m, cand_sd = ("ema", m_ema, ema_model.state_dict())

        improved = ep >= args.min_pick_ep and cand_m["score"] > best_score
        if ep in (1, 5, 10, 15, 20, args.min_pick_ep, 35, 45, args.epochs) or improved:
            e_tag = f" [ema={m_ema['score']:.4f} raw={m_raw['score']:.4f}->{cand_kind}]" if m_ema else ""
            print(
                f"[{args.exp} Ep {ep:2d}/{args.epochs}] ({time.perf_counter()-t0:.1f}s) lr={opt.param_groups[0]['lr']:.2e} "
                f"loss={ep_loss/nb:.5f} | csep={cand_m['cal_sep']:.3f} midM={cand_m['mid_mae_macro']:.4f} "
                f"occ={cand_m['mid_occ_gap']:.3f} mae={cand_m['mean_mae']:.4f} fp={cand_m['mean_fp']:.3f} "
                f"wo={cand_m['wo_to']:.3f} cpl={cand_m['smile_puff_l_leak']:.3f} sc={cand_m['score']:.4f}{e_tag}",
                flush=True,
            )
        if improved or ep == 1:
            best_score = cand_m["score"] if ep >= args.min_pick_ep else best_score
            cpu_sd = {k: v.detach().cpu().clone() for k, v in cand_sd.items()}
            torch.save(dict(state_dict=cpu_sd, metrics=cand_m, epoch=ep, pick_kind=cand_kind,
                            trained_channels=_ordered_channels(trained)), best_path)
            if ep >= args.min_pick_ep:
                top_candidates.append((cand_m["score"], ep, cand_kind, cpu_sd))
                top_candidates.sort(key=lambda item: item[0], reverse=True)
                top_candidates = top_candidates[:3]

    saved = torch.load(best_path, map_location="cpu", weights_only=True)
    pick_note = f"{saved.get('pick_kind', 'raw')}_e{saved['epoch']}"
    if len(top_candidates) >= 2:
        soup_sd = {
            k: (sum(c[3][k].float() for c in top_candidates) / float(len(top_candidates))) if k in trainable_names else top_candidates[0][3][k]
            for k in top_candidates[0][3]
        }
        model.load_state_dict(soup_sd)
        m_soup, _ = eval_s(model)
        print(f"[{args.exp} Soup top-{len(top_candidates)}] sc={m_soup['score']:.4f} vs best={best_score:.4f}", flush=True)
        if m_soup["score"] > best_score:
            best_score = m_soup["score"]
            pick_note = f"soup{len(top_candidates)}_e{'_'.join(str(c[1]) for c in top_candidates)}"
            torch.save(dict(state_dict=soup_sd, metrics=m_soup, epoch=pick_note, pick_kind="soup",
                            trained_channels=_ordered_channels(trained)), best_path)
            saved = torch.load(best_path, map_location="cpu", weights_only=True)

    model.load_state_dict(saved["state_dict"])
    onnx_path = out_dir / "vft_student_50.onnx"
    manifest = export_student_onnx(
        model, onnx_path, _ordered_channels(trained),
        f"v3 debinarized single-frame [{args.exp}] ({pick_note}, git {gmeta['branch']}@{gmeta['commit']})",
    )
    rep = evaluation.evaluate_model(
        model, "student", f"{args.exp}_{pick_note}",
        data_dir=data_dir, device=device, val_cache=va_imgs.float() / 255.0, **_eval_options(args),
    )
    (out_dir / "student_eval.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "run_meta.json").write_text(json.dumps({
        "exp": args.exp, "stage": "v3_student", "picked": pick_note, "git": gmeta,
        "args": vars(args), "onnx_max_diff": manifest["max_onnx_abs_diff"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{args.exp}] Exported {onnx_path} (max_diff={manifest['max_onnx_abs_diff']:.2e})", flush=True)
    return rep


def _normalize_frame_record(rec: dict, kd_by_image: dict[str, dict]) -> dict:
    img = rec["image"]
    kd = kd_by_image.get(img)
    targets = rec.get("targets") if rec.get("targets") is not None else (kd["targets"] if kd else [None] * 50)
    weights = rec.get("weights") if rec.get("weights") is not None else (kd["weights"] if kd else [0.0] * 50)
    soft_targets = rec.get("soft_targets") if rec.get("soft_targets") is not None else (kd.get("soft_targets") if kd else [None] * 50)
    kd_conf = rec.get("kd_conf") if rec.get("kd_conf") is not None else (kd.get("kd_conf") if kd else None)
    return {
        "image": img,
        "phase": rec.get("phase") or (kd.get("phase") if kd else "unknown"),
        "t_rel": rec.get("t_rel") if rec.get("t_rel") is not None else (kd.get("t_rel", 0.0) if kd else 0.0),
        "targets": targets,
        "weights": weights,
        "soft_targets": soft_targets,
        "kd_conf": kd_conf,
    }


def load_clip_spans(
    data_dir: Path, clips_path: Path | None = None, *, kd_jsonl=None, clip_len: int = 16,
) -> list[dict]:
    """Load contiguous training clip spans from vft50_clips_train.json (merging KD soft targets by image path)."""
    kd_list = []
    kd_path = Path(kd_jsonl) if kd_jsonl else data_dir / "vft50_train_kd.jsonl"
    if kd_jsonl is not None or kd_path.is_file():
        # Keep the row indexes stable for clip indices, but don't use excluded frames.
        kd_list = evaluation.load_rows(kd_path, include_review_excluded=True)
        _reject_sealed(kd_list)
    excluded = {row["image"] for row in kd_list if row.get("review_excluded")}
    kd_by_image = {row["image"]: row for row in kd_list if row["image"] not in excluded}

    def append_runs(records, sid, pid, transition=False):
        run = []
        for row in records:
            if row.get("review_excluded") or row["image"] in excluded:
                if len(run) >= clip_len:
                    spans.append(dict(session_id=sid, prompt_id=pid, has_transition=transition, frames=run))
                run = []
            else:
                run.append(_normalize_frame_record(row, kd_by_image))
        if len(run) >= clip_len:
            spans.append(dict(session_id=sid, prompt_id=pid, has_transition=transition, frames=run))

    cpath = Path(clips_path) if clips_path else (data_dir / "vft50_clips_train.json")
    spans = []
    if clips_path is not None or cpath.is_file():
        raw = json.loads(cpath.read_text(encoding="utf-8-sig"))
        entries = (raw.get("clips") or raw.get("spans") or []) if isinstance(raw, dict) else raw
        _reject_sealed(raw)
        _reject_sealed(entries)
        for entry in entries:
            if isinstance(entry, list):
                entry = {"frames": entry}
            if entry.get("review_excluded"):
                continue
            if "frames" in entry:
                frames = evaluation._resolve_images(entry["frames"], cpath.parent)
            elif "indices" in entry:
                frames = [kd_list[idx] for idx in entry["indices"]]
            else:
                frames = evaluation._resolve_images(({"image": p} for p in entry["images"]), cpath.parent)
            _reject_sealed(frames)
            append_runs(frames, entry.get("session_id", ""), entry.get("prompt_id", ""), bool(entry.get("has_transition")))
    elif kd_list:
        # Fallback: reconstruct contiguous spans from sorted (session_id, prompt_id)
        grouped = {}
        for r in kd_list:
            if r.get("session_id") and r.get("prompt_id") and r.get("t_rel") is not None:
                grouped.setdefault((r["session_id"], r["prompt_id"]), []).append(r)
        for (sid, pid), rows in grouped.items():
            rows.sort(key=lambda r: float(r["t_rel"]))
            start = 0
            for end in range(1, len(rows) + 1):
                if end == len(rows) or not 0 < float(rows[end]["t_rel"]) - float(rows[end - 1]["t_rel"]) <= 0.15:
                    append_runs(rows[start:end], sid, pid)
                    start = end

    return spans


class ContiguousClipDataset(Dataset):
    """Samples T-frame contiguous clips with stride jitter in {1,2,3}, mid-clip resets, and static anti-drift clips."""

    def __init__(
        self,
        spans: list[dict],
        clip_len: int = 16,
        samples_per_epoch: int = 2400,
        reset_p: float = 0.20,
        static_p: float = 0.12,
        seed: int = 42,
    ):
        self.clip_len = int(clip_len)
        self.samples_per_epoch = int(samples_per_epoch)
        self.reset_p = float(reset_p)
        self.static_p = float(static_p)
        self.rng = np.random.default_rng(seed)
        self._img_cache: dict[str, np.ndarray] = {}

        # Categorize spans so transitions get strong representation
        self.spans = []
        self.trans_spans = []
        self.other_spans = []
        for sp in spans:
            frames = sp["frames"]
            if len(frames) < self.clip_len:
                continue
            has_trans = bool(sp.get("has_transition")) or any(
                f.get("phase") in ("transition", "carryover_release", "sweep_onset_left", "sweep_retract")
                or any(t is not None and w > 0 and 0.05 < float(t) < 0.72 for t, w in zip(f["targets"], f["weights"]))
                for f in frames
            )
            idx_sp = len(self.spans)
            self.spans.append(sp)
            if has_trans:
                self.trans_spans.append(idx_sp)
            else:
                self.other_spans.append(idx_sp)
        if not self.spans:
            raise ValueError(f"No clip spans with length >= {self.clip_len}")

    def _load_img(self, path: str) -> np.ndarray:
        arr = self._img_cache.get(path)
        if arr is None:
            arr = evaluation.load_gray400(path)
            if len(self._img_cache) < 40000:
                self._img_cache[path] = arr
        return arr

    def __len__(self):
        return self.samples_per_epoch

    def __getitem__(self, _idx):
        if self.trans_spans and self.other_spans and self.rng.random() < 0.60:
            sp = self.spans[int(self.rng.choice(self.trans_spans))]
        else:
            sp = self.spans[int(self.rng.integers(0, len(self.spans)))]
        frames = sp["frames"]
        n = len(frames)
        T = self.clip_len

        is_static = self.rng.random() < self.static_p
        if is_static:
            pick = int(self.rng.integers(0, n))
            chosen = [frames[pick]] * T
        else:
            valid_strides = [s for s in (1, 2, 3) if (T - 1) * s < n]
            if not valid_strides:
                stride = 1
            else:
                probs = np.array([{1: 0.55, 2: 0.30, 3: 0.15}[s] for s in valid_strides], dtype=np.float64)
                probs /= probs.sum()
                stride = int(self.rng.choice(valid_strides, p=probs))
            max_start = max(0, n - 1 - (T - 1) * stride)
            start = int(self.rng.integers(0, max_start + 1)) if max_start > 0 else 0
            chosen = [frames[start + k * stride] for k in range(T)]

        imgs = np.stack([self._load_img(f["image"]) for f in chosen], axis=0)[:, None, :, :]
        t_arr = np.zeros((T, 50), dtype=np.float32)
        w_arr = np.zeros((T, 50), dtype=np.float32)
        s_arr = np.zeros((T, 50), dtype=np.float32)
        sm_arr = np.zeros((T, 50), dtype=np.float32)
        kc_arr = np.zeros((T, 50), dtype=np.float32)

        for ti, f in enumerate(chosen):
            r_t, r_w = f["targets"], f["weights"]
            r_s = f.get("soft_targets") or [None] * 50
            r_c = f.get("kd_conf")
            for j in range(50):
                if r_t[j] is not None and r_w[j] > 0:
                    t_arr[ti, j] = float(r_t[j])
                    w_arr[ti, j] = float(r_w[j])
                if r_s[j] is not None:
                    s_arr[ti, j] = float(r_s[j])
                    sm_arr[ti, j] = 1.0
                    kc_arr[ti, j] = float(r_c[j]) if (r_c is not None and r_c[j] is not None) else 1.0

        reset_mask = np.zeros(T, dtype=np.bool_)
        if not is_static and T >= 8 and self.rng.random() < self.reset_p:
            t0 = int(self.rng.integers(4, min(13, T - 1)))
            reset_mask[t0] = True

        return (
            torch.from_numpy(imgs),
            torch.from_numpy(t_arr),
            torch.from_numpy(w_arr),
            torch.from_numpy(s_arr),
            torch.from_numpy(sm_arr),
            torch.from_numpy(kc_arr),
            torch.from_numpy(reset_mask),
            torch.tensor(is_static, dtype=torch.bool),
        )


def export_stateful_gru_onnx(
    model: StatefulGRUStudent50,
    out_onnx: Path,
    trained_channels: list[str],
    note: str,
    *,
    check_cuda: bool = True,
) -> dict:
    """Export StatefulGRUStudent50 to opset-17 ONNX (frame, h_in -> blendshapes, h_out) and verify CPU/CUDA parity."""
    model_cpu = copy.deepcopy(model).cpu().eval()
    out_onnx = Path(out_onnx)
    out_onnx.parent.mkdir(parents=True, exist_ok=True)
    H = model_cpu.hid_dim
    dummy_frame = torch.zeros(1, 1, 400, 400, dtype=torch.float32)
    dummy_h = torch.zeros(1, H, dtype=torch.float32)

    with torch.inference_mode():
        torch.onnx.export(
            model_cpu,
            (dummy_frame, dummy_h),
            str(out_onnx),
            input_names=["frame", "h_in"],
            output_names=["blendshapes", "h_out"],
            opset_version=17,
            dynamo=False,
        )
    omodel = onnx.load(str(out_onnx))
    onnx.checker.check_model(omodel)
    op_types = {n.op_type for n in omodel.graph.node}
    assert "GRU" not in op_types, f"ONNX graph unexpectedly contains GRU op: {op_types}"

    # Verify multi-step parity on CPU and (if available) CUDA fp32
    sess_cpu = evaluation._cpu_ort_session(out_onnx)
    rng = torch.Generator().manual_seed(73)
    seq_frames = [
        torch.zeros(1, 1, 400, 400),
        torch.rand(1, 1, 400, 400, generator=rng),
        torch.rand(1, 1, 400, 400, generator=rng),
        torch.linspace(0, 1, 400 * 400).reshape(1, 1, 400, 400),
    ]
    h_pt = torch.zeros(1, H, dtype=torch.float32)
    h_cpu = np.zeros((1, H), dtype=np.float32)
    max_cpu_y = 0.0
    max_cpu_h = 0.0
    pt_ys, pt_hs = [], []
    with torch.inference_mode():
        for fr in seq_frames:
            y_pt, h_pt = model_cpu.forward_step(fr, h_pt)
            y_cpu, h_cpu = sess_cpu.run(["blendshapes", "h_out"], {"frame": fr.numpy(), "h_in": h_cpu})
            pt_ys.append(y_pt.numpy())
            pt_hs.append(h_pt.numpy())
            max_cpu_y = max(max_cpu_y, float(np.max(np.abs(y_pt.numpy() - y_cpu))))
            max_cpu_h = max(max_cpu_h, float(np.max(np.abs(h_pt.numpy() - h_cpu))))

    max_cuda_y = None
    max_cuda_h = None
    if check_cuda and "CUDAExecutionProvider" in ort.get_available_providers():
        try:
            ort.preload_dlls()
            sess_cuda = ort.InferenceSession(
                str(out_onnx),
                providers=[("CUDAExecutionProvider", {"use_tf32": "0"}), "CPUExecutionProvider"],
            )
            if "CUDAExecutionProvider" in sess_cuda.get_providers():
                h_cu = np.zeros((1, H), dtype=np.float32)
                max_cuda_y, max_cuda_h = 0.0, 0.0
                for fr, y_ref, h_ref in zip(seq_frames, pt_ys, pt_hs):
                    y_cu, h_cu = sess_cuda.run(["blendshapes", "h_out"], {"frame": fr.numpy(), "h_in": h_cu})
                    max_cuda_y = max(max_cuda_y, float(np.max(np.abs(y_ref - y_cu))))
                    max_cuda_h = max(max_cuda_h, float(np.max(np.abs(h_ref - h_cu))))
        except Exception as exc:
            print(f"[export_stateful_gru_onnx] CUDA parity skipped: {exc}", flush=True)

    manifest = {
        "schema": SCHEMA,
        "channels": list(CHANNELS),
        "model_kind": f"{model_cpu.arch}-{model_cpu.head_kind}-w{model_cpu.net_width}-roi{model_cpu.roi_end}-gru{H}",
        "model_id": str(uuid.uuid4()),
        "width": 400,
        "height": 400,
        "trained_channels": _ordered_channels(trained_channels),
        "validated_channels": [],
        "valid_mask": 0,
        "candidate_only": True,
        "input_name": "frame",
        "output_name": "blendshapes",
        "temporal_kind": "gru_stateful",
        "state_input_name": "h_in",
        "state_output_name": "h_out",
        "state_shape": [1, H],
        "temporal": {
            "kind": "gru_stateful",
            "state_input": "h_in",
            "state_output": "h_out",
            "state_shape": [1, H],
            "nominal_hz": 90,
        },
        "max_onnx_abs_diff": max_cpu_y,
        "max_onnx_state_diff": max_cpu_h,
        "max_onnx_cuda_abs_diff": max_cuda_y,
        "max_onnx_cuda_state_diff": max_cuda_h,
        "note": note,
    }
    out_onnx.with_suffix(".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def benchmark_stateful_onnx(
    onnx_path: Path, hid_dim: int, n_warmup: int = 20, n_iters: int = 100, *, check_cuda: bool = True,
) -> dict:
    """Benchmark stateful ONNX latency on CPU 1-thread and CUDA Graph IO-binding (when available)."""
    frame = np.zeros((1, 1, 400, 400), dtype=np.float32)
    state = np.zeros((1, hid_dim), dtype=np.float32)
    res = {"onnx": str(onnx_path)}

    # CPU 1-thread
    s_cpu = evaluation._cpu_ort_session(onnx_path)
    h = state.copy()
    for _ in range(min(10, n_warmup)):
        _, h = s_cpu.run(["blendshapes", "h_out"], {"frame": frame, "h_in": h})
    ts_cpu = []
    for _ in range(n_iters):
        t0 = time.perf_counter()
        _, h = s_cpu.run(["blendshapes", "h_out"], {"frame": frame, "h_in": h})
        ts_cpu.append((time.perf_counter() - t0) * 1000.0)
    res["cpu_1t_ms"] = {
        "p50": round(float(np.percentile(ts_cpu, 50)), 3),
        "p95": round(float(np.percentile(ts_cpu, 95)), 3),
    }

    # CUDA Graph IO-binding
    if check_cuda and "CUDAExecutionProvider" in ort.get_available_providers():
        try:
            ort.preload_dlls()
            opts_cu = ort.SessionOptions()
            opts_cu.intra_op_num_threads = 1
            opts_cu.inter_op_num_threads = 1
            s_cg = ort.InferenceSession(
                str(onnx_path),
                opts_cu,
                providers=[
                    ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC", "use_tf32": "0", "enable_cuda_graph": "1"}),
                    "CPUExecutionProvider",
                ],
            )
            if "CUDAExecutionProvider" in s_cg.get_providers():
                in_val = ort.OrtValue.ortvalue_from_numpy(frame, "cuda", 0)
                st_in = ort.OrtValue.ortvalue_from_numpy(state, "cuda", 0)
                out_val = ort.OrtValue.ortvalue_from_shape_and_type((1, 50), np.float32, "cuda", 0)
                st_out = ort.OrtValue.ortvalue_from_shape_and_type((1, hid_dim), np.float32, "cuda", 0)
                binding = s_cg.io_binding()
                binding.bind_ortvalue_input("frame", in_val)
                binding.bind_ortvalue_input("h_in", st_in)
                binding.bind_ortvalue_output("blendshapes", out_val)
                binding.bind_ortvalue_output("h_out", st_out)
                for _ in range(n_warmup):
                    s_cg.run_with_iobinding(binding)
                    binding.synchronize_outputs()
                ts_cg = []
                h_cur = state.copy()
                for _ in range(n_iters):
                    t0 = time.perf_counter()
                    in_val.update_inplace(frame)
                    st_in.update_inplace(h_cur)
                    binding.synchronize_inputs()
                    s_cg.run_with_iobinding(binding)
                    binding.synchronize_outputs()
                    outs = binding.copy_outputs_to_cpu()
                    h_cur = outs[1]
                    ts_cg.append((time.perf_counter() - t0) * 1000.0)
                res["cuda_graph_ms"] = {
                    "p50": round(float(np.percentile(ts_cg, 50)), 3),
                    "p95": round(float(np.percentile(ts_cg, 95)), 3),
                }
        except Exception as exc:
            res["cuda_graph_error"] = str(exc)
    return res


def run_temporal(args):
    device = _training_device(args.device)
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    check_disk_headroom(out_dir, min_gb=2.0)
    gmeta = git_info()

    init_path = Path(args.init_student)
    assert init_path.is_file(), f"Missing Stage-1 student checkpoint: {init_path}"
    init_ckpt = torch.load(init_path, map_location="cpu", weights_only=True)

    # Instantiate & verify step-0 exact equivalence with VFTStudent50
    sf_ref = VFTStudent50(
        arch=args.arch, net_width=args.width, head_kind=args.head, roi_end=args.roi_end, load_pretrained=False
    ).eval()
    sf_ref.load_state_dict(init_ckpt["state_dict"])

    model = StatefulGRUStudent50(
        arch=args.arch,
        net_width=args.width,
        head_kind=args.head,
        roi_end=args.roi_end,
        hid_dim=args.hid_dim,
        load_pretrained=False,
    ).eval()
    model.load_from_single_frame(init_ckpt["state_dict"])

    with torch.inference_mode():
        probe_x = torch.rand(2, 1, 400, 400)
        probe_h = torch.zeros(2, args.hid_dim)
        y_sf = sf_ref(probe_x)
        y_gru, _ = model.forward_step(probe_x, probe_h)
        step0_diff = float((y_sf - y_gru).abs().max().item())
        assert step0_diff < 1e-6, f"Step-0 equivalence failed: max_abs_diff={step0_diff}"
    del sf_ref
    print(f"[v3 Temporal] Initialized StatefulGRUStudent50(H={args.hid_dim}) from {init_path.name} (step0_diff={step0_diff:.2e})", flush=True)

    spans = load_clip_spans(data_dir, args.clips_json, kd_jsonl=args.kd_jsonl, clip_len=args.clip_len)
    print(f"[v3 Temporal] Loaded {len(spans)} contiguous clip spans from {data_dir}", flush=True)
    va_rows, va_imgs, _, _ = preload_sup_rows(data_dir / "vft50_val71.jsonl")
    _check_disjoint(va_rows, (frame["image"] for span in spans for frame in span["frames"]))
    inherited = set(init_ckpt.get("trained_channels", ()))
    observed = torch.zeros(len(CHANNELS), dtype=torch.bool, device=device)

    model = model.to(device)
    ema_model = copy.deepcopy(model).eval()
    trainable_names = {n for n, p in model.named_parameters() if p.requires_grad}

    ds = ContiguousClipDataset(
        spans,
        clip_len=args.clip_len,
        samples_per_epoch=args.clips_per_epoch,
        reset_p=args.reset_p,
        static_p=args.static_p,
        seed=args.seed,
    )
    loader = DataLoader(ds, batch_size=args.batch_size, shuffle=True, num_workers=0)

    head_params = (
        list(model.feat_head.parameters())
        + list(model.static_head.parameters())
        + list(model.gru_cell.parameters())
        + list(model.temp_head.parameters())
        + (list(model.proj.parameters()) if model.proj is not None else [])
        + (list(model.proj16.parameters()) if model.proj16 is not None else [])
    )
    opt = torch.optim.AdamW([
        {"params": list(model.features.parameters()), "lr": args.lr_backbone, "weight_decay": 1e-4},
        {"params": head_params, "lr": args.lr_head, "weight_decay": 1e-4},
    ])
    sched = make_scheduler(opt, "cosine", args.epochs, max(1, len(loader)))

    @torch.inference_mode()
    def eval_val(m_eval):
        m_eval.eval()
        preds = []
        for i in range(0, len(va_imgs), 64):
            xb = (va_imgs[i : i + 64].float() / 255.0).to(device)
            h0 = torch.zeros((xb.shape[0], args.hid_dim), device=device, dtype=torch.float32)
            y, _ = m_eval.forward_step(xb, h0)
            preds.append(y.float().cpu().numpy())
        pv = np.concatenate(preds, axis=0)
        return score_val_continuous_v2(pv, va_rows)

    best_score, best_path = -1e9, out_dir / "temporal_best.pt"
    global_step = 0
    for ep in range(1, args.epochs + 1):
        model.train()
        ep_loss, nb, t0 = 0.0, 0, time.perf_counter()
        for imgs_u8, t_b, w_b, s_b, sm_b, kc_b, rmask_b, is_stat_b in loader:
            x_seq = augment_clip_ir_physical(imgs_u8.to(device))
            t_b, w_b = t_b.to(device), w_b.to(device)
            s_b, sm_b, kc_b = s_b.to(device), sm_b.to(device), kc_b.to(device)
            rmask_b, is_stat_b = rmask_b.to(device), is_stat_b.to(device)

            with torch.amp.autocast("cuda", dtype=torch.bfloat16, enabled=(device == "cuda")):
                logits_seq, _ = model.forward_sequence_logits(x_seq, h0=None, reset_mask=rmask_b)
                logits_seq = logits_seq.float()
                probs_seq = torch.sigmoid(logits_seq)

            loss_sup = debinarized_continuous_loss(logits_seq, t_b, w_b, logit_reg_w=args.logit_reg_w)
            kd_w = build_soft_kd_weights(w_b, t_b, sm_b, kd_conf=kc_b)
            observed |= (w_b > 0).any(dim=(0, 1))
            if args.kd_weight > 0:
                observed |= (kd_w > 0).any(dim=(0, 1))
            loss_kd = debinarized_continuous_loss(logits_seq, s_b, kd_w, logit_reg_w=0.0)
            loss_temp = clip_temporal_loss(
                probs_seq, t_b, w_b,
                vel_w=args.vel_w, acc_w=args.acc_w, mono_w=args.mono_w,
                reset_mask=rmask_b,
            )
            loss = loss_sup + args.kd_weight * loss_kd + loss_temp

            if args.drift_w > 0.0 and is_stat_b.any():
                p_st = probs_seq[is_stat_b]
                loss_drift = (p_st[:, 1:] - p_st[:, :1].detach()).abs().mean()
                loss = loss + args.drift_w * loss_drift

            opt.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 2.0)
            opt.step()
            sched.step()
            global_step += 1

            d_cur = min(args.ema_decay, (1.0 + global_step) / (10.0 + global_step))
            with torch.no_grad():
                ema_params = dict(ema_model.named_parameters())
                for n, p in model.named_parameters():
                    if n in trainable_names:
                        ema_params[n].mul_(d_cur).add_(p.data, alpha=1.0 - d_cur)
                ema_bufs = dict(ema_model.named_buffers())
                for n, b in model.named_buffers():
                    if n in ema_bufs:
                        ema_bufs[n].copy_(b.data)

            ep_loss += float(loss.detach())
            nb += 1

        m_raw = eval_val(model)
        m_ema = eval_val(ema_model) if ep >= 3 else None
        cand_kind, cand_m, cand_sd = ("raw", m_raw, model.state_dict())
        if m_ema is not None and m_ema["score"] > m_raw["score"]:
            cand_kind, cand_m, cand_sd = ("ema", m_ema, ema_model.state_dict())
        print(
            f"[{args.exp} Ep {ep:2d}/{args.epochs}] ({time.perf_counter()-t0:.1f}s) loss={ep_loss/nb:.5f} | "
            f"csep={cand_m['cal_sep']:.3f} midM={cand_m['mid_mae_macro']:.4f} occ={cand_m['mid_occ_gap']:.3f} "
            f"mae={cand_m['mean_mae']:.4f} fp={cand_m['mean_fp']:.3f} wo={cand_m['wo_to']:.3f} "
            f"sc={cand_m['score']:.4f} [{cand_kind}]",
            flush=True,
        )
        if cand_m["score"] > best_score or ep == 1:
            best_score = cand_m["score"]
            cpu_sd = {k: v.detach().cpu().clone() for k, v in cand_sd.items()}
            trained = inherited | {ch for ch, used in zip(CHANNELS, observed.tolist()) if used}
            torch.save(dict(state_dict=cpu_sd, metrics=cand_m, epoch=ep, pick_kind=cand_kind,
                            hid_dim=args.hid_dim, trained_channels=_ordered_channels(trained)), best_path)

    saved = torch.load(best_path, map_location="cpu", weights_only=True)
    pick_note = f"{saved.get('pick_kind', 'raw')}_e{saved['epoch']}"
    model.load_state_dict(saved["state_dict"])

    onnx_name = f"vft_student_50_gru{args.hid_dim}.onnx"
    onnx_path = out_dir / onnx_name
    manifest = export_stateful_gru_onnx(
        model, onnx_path, saved["trained_channels"],
        f"v3 causal stateful GRU-{args.hid_dim} [{args.exp}] ({pick_note}, git {gmeta['branch']}@{gmeta['commit']})",
        check_cuda=device == "cuda",
    )
    bench = benchmark_stateful_onnx(onnx_path, args.hid_dim, check_cuda=device == "cuda")
    runtime_report = {
        "onnx": str(onnx_path),
        "pt_ort_cpu_max_abs_diff": manifest["max_onnx_abs_diff"],
        "pt_ort_cpu_state_diff": manifest["max_onnx_state_diff"],
        "pt_ort_cuda_max_abs_diff": manifest["max_onnx_cuda_abs_diff"],
        "pt_ort_cuda_state_diff": manifest["max_onnx_cuda_state_diff"],
        "benchmark": bench,
    }
    # Persist parity & latency report BEFORE threshold assertions
    (out_dir / "onnx_runtime_check.json").write_text(json.dumps(runtime_report, ensure_ascii=False, indent=2), encoding="utf-8")
    for metric, diff in runtime_report.items():
        if metric.startswith("pt_ort_") and diff is not None:
            assert diff < 5e-5, f"{metric} exceeded 5e-5: {diff}"

    rep = evaluation.evaluate_model(
        model, "temporal", f"{args.exp}_{pick_note}", data_dir=data_dir, device=device,
        val_cache=va_imgs.float() / 255.0, **_eval_options(args),
    )
    rep["runtime_check"] = runtime_report
    (out_dir / "temporal_eval.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "run_meta.json").write_text(json.dumps({
        "exp": args.exp, "stage": "v3_temporal", "picked": pick_note, "git": gmeta,
        "args": vars(args), "step0_diff": step0_diff,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{args.exp}] Exported {onnx_path} | runtime={json.dumps(runtime_report)}", flush=True)
    return rep
