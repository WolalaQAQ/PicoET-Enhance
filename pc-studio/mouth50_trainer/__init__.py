"""Offline Mouth50 training; model and augmentation dependencies are loaded on demand."""
from importlib import import_module

from face_schema import CHANNELS, SCHEMA

ROI_END = 400

_EXPORTS = {
    name: module
    for module, names in {
        "models": (
            "SpatialAttentionHead", "VFTTeacher50", "VFTStudent50", "StatefulGRUStudent50",
            "load_dinov3_large_backbone", "save_teacher_delta", "teacher_from_saved",
        ),
        "losses": (
            "debinarized_continuous_loss", "build_soft_kd_weights", "clip_temporal_loss",
            "smooth_continuous_loss", "continuous_regression_loss", "logit_kd_loss",
            "build_continuous_kd_weights", "compute_pos_boost", "make_scheduler",
            "score_val_continuous", "score_val_continuous_v2",
        ),
        "augment": (
            "augment_std", "augment_ir_physical", "augment_clip_ir_physical",
            "extract_teacher_roi400", "apply_teacher_mixup",
        ),
        "label_rules": ("PHYSICAL_COACT_GUARD_CHANNELS",),
    }.items()
    for name in names
}
__all__ = ["CHANNELS", "SCHEMA", "ROI_END", *_EXPORTS]


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    obj = getattr(import_module(f".{_EXPORTS[name]}", __name__), name)
    globals()[name] = obj
    return obj
