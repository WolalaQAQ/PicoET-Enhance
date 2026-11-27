"""Continuous full-50 ROI=400 evaluator covering val71, cross-session benchmarks, held-out MouthPucker ramp, wide-open stability, and composites."""
import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image
import torch

from face_schema import CHANNELS, INDEX as IDX
from . import ROI_END
from .losses import score_val_continuous, score_val_continuous_v2


COMPOSITE_CHANNELS = {
    "jaw_tongue_small": ("JawOpen", "TongueOut"),
    "jaw_tongue_large": ("JawOpen", "TongueOut"),
    "smile_puff": ("CheekPuffLeft", "MouthSmileL", "MouthSmileR"),
    "funnel_both": ("MouthFunnel", "LipFunnelUpper", "LipFunnelLower"),
    "suck_both": ("CheekSuckLeft", "CheekSuckRight"),
    "lips_opposed_a": ("MouthUpperLeft", "MouthLowerRight"),
    "lips_opposed_b": ("MouthUpperRight", "MouthLowerLeft"),
    "negative_teeth": ("MouthSmileL", "MouthSmileR", "MouthUpperUpL", "MouthUpperUpR", "TongueOut"),
}


def load_gray400(path) -> np.ndarray:
    with Image.open(path) as im:
        if im.size != (ROI_END, ROI_END):
            raise ValueError(f"Expected a 400x400 mouth frame, got {im.size}: {path}")
        return np.asarray(im.convert("L"), dtype=np.uint8)


def load_gray400_batch(paths: list[str]) -> torch.Tensor:
    """Return normalized NCHW float32 from 400x400 images."""
    if not paths:
        return torch.empty((0, 1, ROI_END, ROI_END), dtype=torch.float32)
    return torch.from_numpy(np.stack([load_gray400(p) for p in paths]))[:, None].float() / 255.0


def load_rows(jsonl_path, *, include_review_excluded=False):
    """Resolve relative images against their JSONL file, not the process directory."""
    path = Path(jsonl_path)
    with path.open(encoding="utf-8-sig") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    return _resolve_images(
        (row for row in rows if include_review_excluded or not row.get("review_excluded")), path.parent,
    )


def _resolve_images(rows, base):
    return [{**row, "image": str((base / row["image"]).resolve())} for row in rows]


def _cpu_ort_session(path):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    return ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])


def _prepare_onnx_input(image: np.ndarray, width: int, height: int) -> np.ndarray:
    resized = Image.fromarray(image).resize((width, height), Image.Resampling.BILINEAR)
    return np.asarray(resized, dtype=np.float32)[None, None] / 255.0


@torch.inference_mode()
def predict_paths(
    model_or_onnx, kind: str, paths: list[str], device: str = "cpu", batch_size: int = 32,
    *, propagate: bool = False,
) -> np.ndarray:
    if not paths:
        return np.zeros((0, len(CHANNELS)), dtype=np.float32)
    if kind == "temporal":
        return predict_stateful_sequence(model_or_onnx, paths, device=device, propagate=propagate)
    if kind == "onnx":
        import onnxruntime as ort

        sess = model_or_onnx if isinstance(model_or_onnx, ort.InferenceSession) else _cpu_ort_session(model_or_onnx)
        frame_input, *state_inputs = sess.get_inputs()
        h, w = frame_input.shape[-2:]
        state = np.zeros(state_inputs[0].shape, dtype=np.float32) if state_inputs else None
        initial = state
        outs = []
        for p in paths:
            inputs = {frame_input.name: _prepare_onnx_input(load_gray400(p), int(w), int(h))}
            if state_inputs:
                inputs[state_inputs[0].name] = state
            y, *next_state = sess.run(None, inputs)
            outs.append(y[0])
            if state_inputs:
                state = next_state[0] if propagate else initial
        return np.stack(outs, axis=0)

    if kind not in ("teacher", "student"):
        raise ValueError(f"Unsupported kind: {kind}")
    model = model_or_onnx.eval().to(device)
    outs = []
    for i in range(0, len(paths), batch_size):
        x400 = load_gray400_batch(paths[i : i + batch_size])
        outs.append(_predict_tensor(model, kind, x400, device=device, batch_size=batch_size))
    return np.concatenate(outs, axis=0)


@torch.inference_mode()
def _predict_tensor(model, kind: str, img_float400: torch.Tensor, device: str, batch_size: int = 32) -> np.ndarray:
    model = model.eval().to(device)
    outs = []
    for i in range(0, len(img_float400), batch_size):
        xb = img_float400[i : i + batch_size].to(device)
        if kind == "temporal":
            h0 = xb.new_zeros((len(xb), model.hid_dim))
            y, _ = model.forward_step(xb, h0)
        else:
            with torch.amp.autocast("cuda", dtype=torch.bfloat16, enabled=(kind == "teacher" and device == "cuda")):
                y = model(xb)
        outs.append(y.float().cpu().numpy())
    return np.concatenate(outs, axis=0)


DIAG_SEQ_PROMPTS = (
    ("LipFunnelUpper", "LipFunnelUpper"),
    ("JawLeft", "JawLeft"),
    ("TongueRoll", "TongueRoll"),
    ("MouthPucker", "MouthPucker"),
    ("JawOpen", "JawOpen"),
    ("CheekPuff", "CheekPuff"),
    ("MouthSmileL", "MouthSmileL"),
    ("TongueLeft", "TongueLeft"),
    ("TongueRight", "TongueRight"),
)


def compute_v3_diagnostics(
    predict_seq_fn,
    preds_val: np.ndarray,
    val_rows: list[dict],
    base_eval: dict | None = None,
    trajectory_buckets: dict | None = None,
) -> dict:
    """Compute v3 de-binarization & crosstalk diagnostics (smile leak, tongue symmetry, heldout pucker, seq max_step)."""
    sc_v2 = score_val_continuous_v2(preds_val, val_rows)
    idx_cpl = IDX["CheekPuffLeft"]
    idx_tl = IDX["TongueLeft"]
    idx_tr = IDX["TongueRight"]
    idx_to = IDX["TongueOut"]

    smile_breakdown = {}
    for pid in ("MouthSmileL", "MouthSmileR", "smile_puff", "negative_teeth"):
        idxs = [ri for ri, r in enumerate(val_rows) if r["prompt_id"] == pid and r["phase"] == "hold"]
        if idxs:
            vals = preds_val[idxs, idx_cpl]
            smile_breakdown[pid] = {
                "n": len(idxs),
                "cheek_puff_l_med": round(float(np.median(vals)), 4),
                "cheek_puff_l_p90": round(float(np.percentile(vals, 90)), 4),
            }

    turn_pids = (
        "TongueLeft", "TongueRight", "TongueUp", "TongueDown",
        "tongue_left_up", "tongue_left_down", "tongue_right_up", "tongue_right_down",
    )
    turn_breakdown = {}
    for pid in turn_pids:
        idxs = [ri for ri, r in enumerate(val_rows) if r["prompt_id"] == pid and r["phase"] == "hold"]
        if idxs:
            turn_breakdown[pid] = {
                "n": len(idxs),
                "to_med": round(float(np.median(preds_val[idxs, idx_to])), 4),
                "tl_med": round(float(np.median(preds_val[idxs, idx_tl])), 4),
                "tr_med": round(float(np.median(preds_val[idxs, idx_tr])), 4),
            }

    tl_med = float(sc_v2["tongue_left_med"])
    tr_med = float(sc_v2["tongue_right_med"])
    tongue_symmetry = {
        "tongue_left_med": round(tl_med, 4),
        "tongue_right_med": round(tr_med, 4),
        "abs_diff": round(abs(tl_med - tr_med), 4),
        "ratio_min_over_max": round(min(tl_med, tr_med) / max(tl_med, tr_med, 1e-6), 4),
        "tongue_turn_to_med": round(float(sc_v2["tongue_turn_to_med"]), 4),
        "per_turn_prompt": turn_breakdown,
    }

    seq_max_step = {}
    max_steps_all = []
    for pid, ch in DIAG_SEQ_PROMPTS:
        rows_b = (trajectory_buckets or {}).get(pid, [])
        if len(rows_b) < 4:
            continue
        # Sample at ~30Hz (stride=3) to measure 33ms step cliffs comparable to design spec
        sub = rows_b[::3]
        pr = predict_seq_fn([r["image"] for r in sub])
        y = pr[:, IDX[ch]]
        diffs = np.abs(np.diff(y))
        ms = float(diffs.max()) if len(diffs) else 0.0
        p95 = float(np.percentile(diffs, 95)) if len(diffs) else 0.0
        mid_cnt = int(((y >= 0.12) & (y <= 0.78)).sum())
        max_steps_all.append(ms)
        seq_max_step[pid] = {
            "channel": ch,
            "n_frames_30hz": len(sub),
            "max_step_33ms": round(ms, 4),
            "p95_step_33ms": round(p95, 4),
            "mid_cnt_012_078": mid_cnt,
        }

    optional = {}
    if base_eval and "heldout_pucker_ramp" in base_eval:
        pr_pk = base_eval["heldout_pucker_ramp"]
        optional["heldout_pucker_summary"] = {
            "mae": pr_pk.get("mae"),
            "mid_mae": pr_pk.get("mid_mae"),
            "mid_cnt_012_078": pr_pk.get("mid_pred_count_012_078"),
            "pearson_r": pr_pk.get("pearson_r"),
            "ordering_accuracy": pr_pk.get("ordering_accuracy"),
        }

    if max_steps_all:
        optional["sequential_max_step"] = {
            "mean_max_step_33ms": round(float(np.mean(max_steps_all)), 4),
            "worst_max_step_33ms": round(float(np.max(max_steps_all)), 4),
            "per_prompt": seq_max_step,
        }
    return {
        "val71_v2": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in sc_v2.items()},
        "smile_to_cheek_puff_l_leak": {
            "overall_med": round(float(sc_v2["smile_puff_l_leak"]), 4),
            "per_prompt": smile_breakdown,
        },
        "tongue_symmetry_and_turns": tongue_symmetry,
        **optional,
    }


@torch.inference_mode()
def predict_stateful_sequence(model, paths: list[str], device: str = "cpu", propagate: bool = True) -> np.ndarray:
    """Run StatefulGRUStudent50 on a sequence of image paths, either propagating h or resetting h=0 per frame."""
    if not paths:
        return np.zeros((0, 50), dtype=np.float32)
    model = model.eval().to(device)
    H = model.hid_dim
    if not propagate:
        outs = []
        for i in range(0, len(paths), 64):
            xb = load_gray400_batch(paths[i : i + 64]).to(device)
            h0 = torch.zeros((xb.shape[0], H), device=device, dtype=torch.float32)
            y, _ = model.forward_step(xb, h0)
            outs.append(y.float().cpu().numpy())
        return np.concatenate(outs, axis=0)

    h = torch.zeros((1, H), device=device, dtype=torch.float32)
    outs = []
    # Process in chunks for CNN embedding speed, then step GRU sequentially
    for i in range(0, len(paths), 64):
        xb = load_gray400_batch(paths[i : i + 64]).to(device)
        e_chunk = model.extract_embedding(xb)
        hs = []
        for t in range(e_chunk.shape[0]):
            h = model.gru_cell(e_chunk[t : t + 1], h)
            hs.append(h)
        h_cat = torch.cat(hs, dim=0)
        logits = model.static_head(e_chunk) + model.temp_head(h_cat)
        outs.append(torch.sigmoid(logits).float().cpu().numpy())
    return np.concatenate(outs, axis=0)


def _read_json(path):
    return Path(path).read_text(encoding="utf-8-sig")


def _metrics(preds, rows):
    per_channel = {}
    for ci, ch in enumerate(CHANNELS):
        pairs = [(float(r["targets"][ci]), float(preds[i, ci]))
                 for i, r in enumerate(rows) if r["targets"][ci] is not None and r["weights"][ci] >= 0.8]
        mid_errors = [abs(float(preds[i, ci]) - float(r["targets"][ci])) for i, r in enumerate(rows)
                      if r["targets"][ci] is not None and r["weights"][ci] >= 0.25
                      and 0.08 <= float(r["targets"][ci]) <= 0.72]
        ts, ps = np.asarray(pairs).reshape(-1, 2).T
        pm, nm = ts >= 0.45, ts <= 0.10
        pos_med = float(np.median(ps[pm])) if pm.any() else None
        neg_med = float(np.median(ps[nm])) if nm.any() else None
        per_channel[ch] = dict(
            count=len(pairs), pos_n=int(pm.sum()), neg_n=int(nm.sum()), mid_n=len(mid_errors),
            mae=round(float(np.mean(np.abs(ps - ts))), 4) if pairs else None,
            mid_mae=round(float(np.mean(mid_errors)), 4) if mid_errors else None,
            pos_med=round(pos_med, 3) if pos_med is not None else None,
            neg_med=round(neg_med, 3) if neg_med is not None else None,
            fp_020=round(float(np.mean(ps[nm] >= 0.20)), 3) if nm.any() else None,
            sep=round(pos_med - neg_med, 3) if pm.any() and nm.any() else None,
        )
    composites = {}
    for pid, names in COMPOSITE_CHANNELS.items():
        idxs = [i for i, row in enumerate(rows) if row["prompt_id"] == pid and row["phase"] == "hold"]
        if idxs:
            composites[pid] = {
                "n": len(idxs),
                "channels": {ch: round(float(np.median(preds[idxs, IDX[ch]])), 4) for ch in names},
            }
    return per_channel, composites


def _cross_session_report(path, predict):
    """Optional benchmark groups; historical field names are accepted as input aliases."""
    xs = json.loads(_read_json(path))
    base = Path(path).parent
    rep = {}
    segments = {}
    for name, items in xs.get("segments", xs.get("short02", {})).items():
        if not items:
            continue
        pr = predict([it["image"] for it in _resolve_images(items, base)], propagate=True)
        jo, to = pr[:, IDX["JawOpen"]], pr[:, IDX["TongueOut"]]
        segments[name] = dict(
            n=len(items), jaw_med=round(float(np.median(jo)), 3),
            jaw_p90=round(float(np.percentile(jo, 90)), 3), jaw_ge_05=int((jo >= 0.5).sum()),
            smileL_med=round(float(np.median(pr[:, IDX["MouthSmileL"]])), 3),
            smileR_med=round(float(np.median(pr[:, IDX["MouthSmileR"]])), 3),
            to_med=round(float(np.median(to)), 3), to_max=round(float(to.max()), 3),
            to_ge_0125=int((to >= 0.125).sum()), to_ge_025=int((to >= 0.25).sum()),
        )
    if segments:
        rep["segments"] = segments

    items = _resolve_images(xs.get("jaw_review", xs.get("test9", [])), base)
    if items:
        pr = predict([it["image"] for it in items])
        targets = np.array([it["review_labels"]["JawOpen"] for it in items])
        jo, to = pr[:, IDX["JawOpen"]], pr[:, IDX["TongueOut"]]
        opened, closed = targets >= 0.5, targets <= 0.1
        rep["jaw_review"] = dict(
            jaw_open_recall=f"{int((jo[opened] >= 0.5).sum())}/{int(opened.sum())}",
            jaw_closed_fp=f"{int((jo[closed] >= 0.5).sum())}/{int(closed.sum())}",
            jaw_mae=round(float(np.mean(np.abs(jo - targets))), 4),
            to_max_open=round(float(to[opened].max()), 3) if opened.any() else None,
            to_max_closed=round(float(to[closed].max()), 3) if closed.any() else None,
            open_preds=[round(float(v), 4) for v in jo[opened]],
            closed_preds=[round(float(v), 4) for v in jo[closed]],
        )

    items = _resolve_images(xs.get("tongue_review", xs.get("val155", [])), base)
    if items:
        pr = predict([it["image"] for it in items])
        jaw = [it["review_labels"].get("JawOpen") for it in items]
        closed = [i for i, target in enumerate(jaw) if target is not None and target <= 0.10]
        jo, to = pr[closed, IDX["JawOpen"]], pr[:, IDX["TongueOut"]]
        rep["tongue_review"] = dict(
            n=len(items), closed_jaw_fp=f"{int((jo >= 0.5).sum())}/{len(closed)}",
            to_ge_0125=int((to >= 0.125).sum()), to_max=round(float(to.max()), 3),
        )

    if legacy := xs.get("m0_json"):
        manifest = (base / legacy).resolve()
        m0 = json.loads(_read_json(manifest))
        review = (manifest.parent / m0["review"]).resolve()
        by_seq = {it["source_frame"]["seq"]: it for it in _resolve_images(json.loads(_read_json(review)), review.parent)}
        buckets = {}
        for name, info in m0["models"]["old"].items():
            if not isinstance(info, dict) or "scores" not in info:
                continue
            paths, missing = [], 0
            for score in info["scores"]:
                seq = score["frame_identity"][1]
                item = by_seq.get(seq)
                if item is None or item.get("visibility") != "no_visible_tongue":
                    continue
                image = Path(item["image"])
                if not image.is_file() and xs.get("legacy_image_dir"):
                    image = base / xs["legacy_image_dir"] / f"legacy-{seq}.png"
                if image.is_file():
                    paths.append(str(image))
                else:
                    missing += 1
            if paths:
                to = predict(paths)[:, IDX["TongueOut"]]
                buckets[name] = dict(n=len(paths), missing=missing,
                                     to_ge_0125=int((to >= 0.125).sum()), to_max=round(float(to.max()), 3))
            elif missing:
                buckets[name] = dict(n=0, missing=missing)
        rep["legacy_tongue_buckets"] = buckets
    return rep


def _pucker_report(path, predict, ramp_rows):
    path = Path(path)
    items, sheets = [], {}
    for anchor in json.loads(_read_json(path)).get("evaluation", []):
        sheet = path.parent / anchor["sheet_index"]
        if sheet not in sheets:
            sheets[sheet] = _resolve_images(json.loads(_read_json(sheet)), sheet.parent)
        row = sheets[sheet][anchor["index"]]
        items.append((row["image"], round(row["t_rel"], 3), float(anchor["labels"]["MouthPucker"])))
    if not items:
        return None
    channel = IDX["MouthPucker"]
    if ramp_rows:
        paths = [r["image"] for r in ramp_rows]
        by_image = dict(zip(paths, predict(paths, propagate=True)[:, channel]))
        preds = np.array([by_image[p] if p in by_image else predict([p])[0, channel] for p, _, _ in items])
    else:
        preds = predict([p for p, _, _ in items])[:, channel]
    targets = np.array([t for _, _, t in items])
    mid = (targets > 0.05) & (targets < 0.85)
    pairs = [(i, j) for i in range(len(items)) for j in range(i + 1, len(items)) if abs(targets[i] - targets[j]) >= 0.20]
    error = np.abs(preds - targets)
    return dict(
        n=len(items), mae=round(float(error.mean()), 4), mid_n=int(mid.sum()),
        mid_mae=round(float(error[mid].mean()), 4) if mid.any() else None,
        ordering_accuracy=round(float(np.mean([
            np.sign(preds[i] - preds[j]) == np.sign(targets[i] - targets[j]) for i, j in pairs
        ])), 4) if pairs else 0.0,
        pearson_r=round(float(np.corrcoef(targets, preds)[0, 1]), 4)
        if min(np.std(targets), np.std(preds)) > 1e-6 else 0.0,
        mid_pred_count_012_078=int(((preds >= 0.12) & (preds <= 0.78)).sum()),
        pairs=[dict(t_rel=ts, gt=gt, pred=round(float(pred), 4)) for (_, ts, gt), pred in zip(items, preds)],
    )


def _wide_open_report(buckets, hold_windows, predict, stride):
    rep = {}
    for pid in ("JawOpen", "negative_wide_open"):
        rows = buckets.get(pid, [])[::stride]
        if not rows:
            continue
        if pid in hold_windows:
            h0, h1 = hold_windows[pid]
            mask = np.array([h0 <= r["t_rel"] <= h1 for r in rows])
        else:
            mask = np.array([r.get("phase") == "hold" for r in rows])
        if not mask.any():
            continue
        pr = predict([r["image"] for r in rows], propagate=True)
        jo, to = pr[:, IDX["JawOpen"]], pr[mask, IDX["TongueOut"]]
        rep[pid] = dict(
            n_hold=int(mask.sum()), hold_min_jaw=round(float(jo[mask].min()), 4),
            hold_p10_jaw=round(float(np.percentile(jo[mask], 10)), 4),
            hold_median_jaw=round(float(np.median(jo[mask])), 4),
            hold_drop_lt_050_count=int((jo[mask] < 0.50).sum()),
            hold_max_tongue_out=round(float(to.max()), 4),
            hold_med_tongue_out=round(float(np.median(to)), 4),
            ramp_mid_count_015_075=int(((~mask) & (jo >= 0.15) & (jo <= 0.75)).sum()),
        )
    return rep


def _sequential_val(rows, predict):
    """Propagate only inside the same session/prompt and across gaps of at most 250 ms."""
    groups = {}
    for i, row in enumerate(rows):
        # Without an identity and timestamp, there is no evidence that frames are contiguous.
        sid, ts = row.get("session_id"), row.get("t_rel")
        key = (sid, row["prompt_id"]) if sid and ts is not None else (None, i)
        groups.setdefault(key, []).append((float(ts or 0), i, row["image"]))
    preds = np.empty((len(rows), len(CHANNELS)), dtype=np.float32)
    for items in groups.values():
        items.sort()
        start = 0
        for end in range(1, len(items) + 1):
            if end == len(items) or not 0 < items[end][0] - items[end - 1][0] <= 0.25:
                span = items[start:end]
                preds[[i for _, i, _ in span]] = predict([p for _, _, p in span], propagate=True)
                start = end
    return preds


def evaluate_model(
    model_or_onnx, kind: str, name: str, data_dir: Path, device: str = "cpu",
    val_cache: torch.Tensor | None = None, *,
    xsession_json=None, refined_json=None, trajectory_json=None,
) -> dict:
    """Score val71 across all 50 channels; external benchmark files are opt-in.

    A trajectory JSON contains prompt-keyed `buckets` with ordered image rows
    (`image`, `t_rel`, optionally `phase`) and optional prompt-keyed `hold_windows`.
    Relative image or nested JSON paths are relative to the file containing them.
    """
    val_rows = load_rows(Path(data_dir) / "vft50_val71.jsonl")
    if not val_rows:
        raise ValueError("vft50_val71.jsonl has no evaluation rows")
    if kind == "onnx" and isinstance(model_or_onnx, (str, Path)):
        model_or_onnx = _cpu_ort_session(model_or_onnx)

    def predict(paths, *, propagate=False):
        return predict_paths(model_or_onnx, kind, paths, device=device, propagate=propagate)

    if val_cache is None or kind == "onnx":
        preds = predict([r["image"] for r in val_rows])
    else:
        preds = _predict_tensor(model_or_onnx, kind, val_cache, device)
    per_channel, composites = _metrics(preds, val_rows)
    rep = dict(
        model=name, val71_continuous=_rounded(score_val_continuous(preds, val_rows)),
        val71_per_channel=per_channel, composite_coactivation=composites,
    )
    diag_preds = preds
    if kind == "temporal":
        diag_preds = _sequential_val(val_rows, predict)
        per_seq, comp_seq = _metrics(diag_preds, val_rows)
        rep.update(
            val71_static_h0=_rounded(score_val_continuous_v2(preds, val_rows)),
            val71_sequential_spans=_rounded(score_val_continuous_v2(diag_preds, val_rows)),
            val71_sequential_per_channel=per_seq, sequential_composite_coactivation=comp_seq,
        )
    if xsession_json is not None:
        rep["cross_session"] = _cross_session_report(xsession_json, predict)
    buckets, hold_windows = {}, {}
    if trajectory_json is not None:
        path = Path(trajectory_json)
        trajectory = json.loads(_read_json(path))
        buckets = {pid: _resolve_images(rows, path.parent) for pid, rows in trajectory["buckets"].items()}
        hold_windows = trajectory.get("hold_windows", {})
        rep["wide_open_trajectory"] = _wide_open_report(buckets, hold_windows, predict, 1 if kind == "temporal" else 2)
    if refined_json is not None:
        ramp = _pucker_report(refined_json, predict, buckets.get("negative_pucker_release", []))
        if ramp is not None:
            rep["heldout_pucker_ramp"] = ramp
    rep["v3_diagnostics"] = compute_v3_diagnostics(
        lambda paths: predict(paths, propagate=True), diag_preds, val_rows, base_eval=rep, trajectory_buckets=buckets,
    )
    return rep


def _rounded(scores):
    return {key: round(v, 4) if isinstance(v, float) else v for key, v in scores.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate an offline Mouth50 checkpoint or ONNX graph.")
    parser.add_argument("--kind", choices=("teacher", "student", "temporal", "onnx"), required=True)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--name")
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--arch", default="small")
    parser.add_argument("--width", type=int, default=384)
    parser.add_argument("--head", default="pyramid")
    parser.add_argument("--roi-end", type=int, default=ROI_END)
    parser.add_argument("--dino-repo")
    parser.add_argument("--dino-weights")
    parser.add_argument("--xsession-json")
    parser.add_argument("--refined-json")
    parser.add_argument("--trajectory-json")
    args = parser.parse_args(argv)

    from .models import StatefulGRUStudent50, VFTStudent50, teacher_from_saved

    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    model = args.path
    if args.kind != "onnx":
        saved = torch.load(args.path, map_location="cpu", weights_only=True)
        if args.kind == "teacher":
            model = teacher_from_saved(saved, dino_repo=args.dino_repo, dino_weights=args.dino_weights)
        else:
            kw = dict(arch=args.arch, net_width=args.width, head_kind=args.head, roi_end=args.roi_end, load_pretrained=False)
            model = (VFTStudent50(**kw) if args.kind == "student"
                     else StatefulGRUStudent50(**kw, hid_dim=saved.get("hid_dim", 256)))
            model.load_state_dict(saved["state_dict"])
    rep = evaluate_model(model, args.kind, args.name or args.path.stem, args.data_dir,
                         device=args.device, xsession_json=args.xsession_json,
                         refined_json=args.refined_json, trajectory_json=args.trajectory_json)
    results = json.dumps(rep, ensure_ascii=False, indent=2)
    if args.out_json:
        args.out_json.write_text(results, encoding="utf-8")
    else:
        print(results)


if __name__ == "__main__":
    main()
