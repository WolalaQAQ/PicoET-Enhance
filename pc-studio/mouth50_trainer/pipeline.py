"""Command line for offline Mouth50 teacher, distillation and student training."""
import argparse

_TRAINING_API = {
    "preload_sup_rows", "export_student_onnx", "export_stateful_gru_onnx",
    "benchmark_stateful_onnx", "git_info",
}


def __getattr__(name):
    if name in _TRAINING_API:
        from . import _training
        return getattr(_training, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Offline Mouth50 continuous training with optional causal GRU.")
    sub = ap.add_subparsers(dest="stage", required=True)

    p_t = sub.add_parser("teacher", help="Fine-tune DINOv3-L Teacher with debinarized_continuous_loss.")
    p_t.add_argument("--init-teacher", help="Optional full or delta teacher checkpoint to continue from.")
    p_t.add_argument("--out-tag", default="continuous")
    p_t.add_argument("--epochs", type=int, default=8)
    p_t.add_argument("--lr-backbone", type=float, default=2.4e-5)
    p_t.add_argument("--lr-head", type=float, default=2.0e-4)
    p_t.add_argument("--unfreeze-last-n", type=int, default=2)
    p_t.add_argument("--batch-size", type=int, default=16)
    p_t.add_argument("--grad-accum", type=int, default=2)
    p_t.add_argument("--logit-reg-w", type=float, default=2e-4)
    p_t.add_argument("--mixup-p", type=float, default=0.05)
    p_t.add_argument("--mid-share", type=float, default=0.38)
    p_t.add_argument("--seed", type=int, default=42)

    p_kd = sub.add_parser("kd", help="Generate 5-TTA soft KD with continuous kd_conf = exp(-disp/0.14).")
    p_kd.add_argument("--teacher-ckpt", required=True)
    p_kd.add_argument("--kd-jsonl", help="Output JSONL path (default: vft50_train_kd.jsonl under --out-dir).")
    p_kd.add_argument("--batch-size", type=int, default=48)
    p_kd.add_argument("--disp-tau", type=float, default=0.14)
    p_kd.add_argument("--max-disp-drop", type=float, default=0.35)
    p_kd.add_argument("--resume", action="store_true")

    p_s = sub.add_parser("student", help="Train single-frame VFTStudent50 with de-binarized loss & soft KD.")
    p_s.add_argument("--exp", default="mouth50-student", help="Experiment name recorded in run metadata.")
    init = p_s.add_mutually_exclusive_group()
    init.add_argument("--init-ckpt", help="Optional single-frame student checkpoint.")
    init.add_argument("--random-init", action="store_true", help="Start without pretrained MobileNet weights.")
    p_s.add_argument("--mnv3-weights", help="Local MobileNet weights; fallback: MOUTH50_MNV3_WEIGHTS.")
    p_s.add_argument("--epochs", type=int, default=55)
    p_s.add_argument("--min-pick-ep", type=int, default=24)
    p_s.add_argument("--batch-size", type=int, default=32)
    p_s.add_argument("--grad-accum", type=int, default=1)
    p_s.add_argument("--lr-scale", type=float, default=1.0)
    p_s.add_argument("--sched", default="restarts", choices=("cosine", "wsd", "restarts"))
    p_s.add_argument("--ema-decay", type=float, default=0.996)
    p_s.add_argument("--kd-weight", type=float, default=1.0)
    p_s.add_argument("--logit-reg-w", type=float, default=2e-4)
    p_s.add_argument("--mixup-p", type=float, default=0.06)
    p_s.add_argument("--mid-share", type=float, default=0.38)
    p_s.add_argument("--seed", type=int, default=42)

    p_m = sub.add_parser("temporal", help="Fine-tune StatefulGRUStudent50(H=256) on contiguous T=16 clips.")
    p_m.add_argument("--clips-json", help="Clip index (default: vft50_clips_train.json under --data-dir).")
    p_m.add_argument("--init-student", required=True)
    p_m.add_argument("--exp", default="mouth50-temporal", help="Experiment name recorded in run metadata.")
    p_m.add_argument("--hid-dim", type=int, default=256)
    p_m.add_argument("--clip-len", type=int, default=16)
    p_m.add_argument("--clips-per-epoch", type=int, default=2400)
    p_m.add_argument("--batch-size", type=int, default=8)
    p_m.add_argument("--epochs", type=int, default=20)
    p_m.add_argument("--lr-backbone", type=float, default=5e-5)
    p_m.add_argument("--lr-head", type=float, default=6e-4)
    p_m.add_argument("--ema-decay", type=float, default=0.995)
    p_m.add_argument("--kd-weight", type=float, default=1.0)
    p_m.add_argument("--logit-reg-w", type=float, default=2e-4)
    p_m.add_argument("--vel-w", type=float, default=0.60)
    p_m.add_argument("--acc-w", type=float, default=0.15)
    p_m.add_argument("--mono-w", type=float, default=0.15)
    p_m.add_argument("--drift-w", type=float, default=0.02)
    p_m.add_argument("--reset-p", type=float, default=0.20)
    p_m.add_argument("--static-p", type=float, default=0.12)
    p_m.add_argument("--seed", type=int, default=42)

    for stage in (p_t, p_kd, p_s, p_m):
        stage.add_argument("--data-dir", required=True, help="Directory containing the Mouth50 input JSONL files.")
        stage.add_argument("--out-dir", required=True, help="Directory in which to write stage artifacts.")
        stage.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    for stage in (p_t, p_kd):
        stage.add_argument("--dino-repo", help="Local DINOv3 source checkout; fallback: MOUTH50_DINO_REPO.")
        stage.add_argument("--dino-weights", help="Local DINOv3 weights; fallback: MOUTH50_DINO_WEIGHTS.")
    for stage in (p_s, p_m):
        stage.add_argument("--arch", choices=("small", "large", "mv2"), default="small")
        stage.add_argument("--width", choices=(256, 320, 384), type=int, default=384)
        stage.add_argument("--head", choices=("mlp", "pyramid", "pyramid_ms"), default="pyramid")
        stage.add_argument("--roi-end", type=int, default=400)
        stage.add_argument("--kd-jsonl", help="Input KD JSONL (default: vft50_train_kd.jsonl under --data-dir).")
    for stage in (p_t, p_s, p_m):
        stage.add_argument("--xsession-json", help="Optional cross-session benchmark JSON.")
        stage.add_argument("--refined-json", help="Optional held-out pucker review JSON.")
        stage.add_argument("--trajectory-json", help="Optional prompt trajectories (buckets and hold_windows).")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not __package__:
        # Also support running this file directly, from outside pc-studio.
        from pathlib import Path
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from mouth50_trainer import _training

    return getattr(_training, f"run_{args.stage}")(args)


if __name__ == "__main__":
    main()
