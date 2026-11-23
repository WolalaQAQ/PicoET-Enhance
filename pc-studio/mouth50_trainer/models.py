"""50-channel VFT Teacher and Student model definitions (ONNX opset-17 verified)."""
import importlib
import os
import sys
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F

from face_schema import CHANNELS
from . import ROI_END


def _material_path(explicit, env_name, cli_option):
    value = explicit if explicit is not None else os.environ.get(env_name)
    if not value:
        raise ValueError(f"Specify {cli_option} or set {env_name}.")
    return Path(value).expanduser().resolve()


def load_dinov3_large_backbone(*, dino_repo=None, dino_weights=None, load_pretrained=True):
    """Load ViT-L/16 from a local DINOv3 checkout, without Torch Hub downloads."""
    repo = _material_path(dino_repo, "MOUTH50_DINO_REPO", "--dino-repo")
    weights = (_material_path(dino_weights, "MOUTH50_DINO_WEIGHTS", "--dino-weights")
               if load_pretrained else None)
    if not (repo / "dinov3" / "hub" / "backbones.py").is_file():
        raise FileNotFoundError(f"No DINOv3 source checkout at {repo} (--dino-repo / MOUTH50_DINO_REPO).")
    if weights is not None and not weights.is_file():
        raise FileNotFoundError(f"No DINOv3 weights at {weights} (--dino-weights / MOUTH50_DINO_WEIGHTS).")
    sys.path.insert(0, str(repo))
    try:
        backbones = importlib.import_module("dinov3.hub.backbones")
        if not Path(backbones.__file__).resolve().is_relative_to(repo):
            raise ImportError("DINOv3 is already imported from another checkout; use a fresh Python process.")
        model = backbones.dinov3_vitl16(pretrained=False)
    finally:
        sys.path.remove(str(repo))
    if weights is not None:
        model.load_state_dict(torch.load(weights, map_location="cpu", weights_only=True))
    return model


def build_mobilenet_features(arch: str, load_pretrained: bool, mnv3_weights=None) -> tuple[nn.Module, int]:
    weights = (_material_path(mnv3_weights, "MOUTH50_MNV3_WEIGHTS", "--mnv3-weights")
               if load_pretrained else None)
    if weights is not None and not weights.is_file():
        raise FileNotFoundError(f"No MobileNet weights at {weights} (--mnv3-weights / MOUTH50_MNV3_WEIGHTS).")
    from torchvision.models import mobilenet_v2, mobilenet_v3_large, mobilenet_v3_small

    backbones = {
        "small": (mobilenet_v3_small, 576),
        "large": (mobilenet_v3_large, 960),
        "mv2": (mobilenet_v2, 1280),
    }
    if arch not in backbones:
        raise ValueError(f"Unsupported MobileNet architecture: {arch}")
    factory, channels = backbones[arch]
    model = factory(weights=None)
    if weights is not None:
        model.load_state_dict(torch.load(weights, map_location="cpu", weights_only=True))
    return model.features, channels


class SpatialAttentionHead(nn.Module):
    def __init__(self, embed_dim=1024, num_queries=12, out_dim=50):
        super().__init__()
        self.num_queries = num_queries
        self.attn_proj = nn.Linear(embed_dim, num_queries)
        self.q_proj = nn.Linear(embed_dim, embed_dim // num_queries)
        self.mlp = nn.Sequential(
            nn.LayerNorm(embed_dim*2+(embed_dim//num_queries)*num_queries),
            nn.Linear(embed_dim*2+(embed_dim//num_queries)*num_queries,512),
            nn.GELU(), nn.Dropout(.15), nn.Linear(512,out_dim))

    def forward(self, cls_tok, patch_toks):
        weights = torch.softmax(self.attn_proj(patch_toks),dim=1)
        attended = torch.einsum("bnk,bnd->bkd",weights,patch_toks)
        return self.mlp(torch.cat((cls_tok,patch_toks.mean(1),self.q_proj(attended).flatten(1)),1))


class ResidualMLPBlock(nn.Module):
    def __init__(self, dim: int, drop: float = 0.10):
        super().__init__()
        self.net = nn.Sequential(
            nn.LayerNorm(dim),
            nn.Linear(dim, dim),
            nn.GELU(),
            nn.Dropout(drop),
            nn.Linear(dim, dim),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + 0.5 * self.net(x)


class VFTStudent50(nn.Module):
    """90Hz ONNX Student: input (B,1,400,400) -> ROI[:roi_end,:roi_end] -> MobileNetV3 -> MLP/Pyramid -> (B,50)."""

    def __init__(
        self,
        arch: str = "large",
        net_width: int = 320,
        head_kind: str = "pyramid",
        roi_end: int = ROI_END,
        load_pretrained: bool = True,
        use_logits: bool = False,
        *,
        mnv3_weights=None,
    ):
        super().__init__()
        assert net_width in (256, 320, 384), f"net_width must be 256, 320, or 384 for static ONNX pooling: {net_width}"
        assert head_kind in ("mlp", "pyramid", "pyramid_ms"), f"Unsupported head_kind: {head_kind}"
        assert 320 <= roi_end <= 400, f"roi_end must be in [320, 400], got {roi_end}"
        if head_kind == "pyramid_ms":
            assert arch == "small", f"pyramid_ms currently supports arch=='small', got {arch}"
        self.arch = arch
        self.net_width = net_width
        self.head_kind = head_kind
        self.roi_end = roi_end
        self.use_logits = use_logits

        self.features, feat_c = build_mobilenet_features(arch, load_pretrained, mnv3_weights)
        self.k_full = net_width // 32  # 8, 10, or 12
        self.k_half = self.k_full // 2  # 4, 5, or 6
        self.k_3x3 = 4
        self.s_3x3 = 4 if net_width == 384 else (3 if net_width == 320 else 2)
        self.k16_2x2 = (net_width // 16) // 2
        self.k16_4x4 = (net_width // 16) // 4
        self.proj16 = None

        if head_kind == "mlp":
            self.proj = None
            self.head = nn.Sequential(
                nn.Linear(feat_c * 5, 384),
                nn.LayerNorm(384),
                nn.GELU(),
                nn.Dropout(0.10),
                nn.Linear(384, 50),
            )
        else:
            proj_c = 160 if (arch in ("mv2", "large") and net_width == 256) else 256
            self.proj = nn.Sequential(
                nn.Conv2d(feat_c, proj_c, kernel_size=1, bias=True),
                nn.GELU(),
            )
            in_dim = proj_c * 19
            if head_kind == "pyramid_ms":
                self.proj16 = nn.Sequential(
                    nn.Conv2d(48, 48, kernel_size=1, bias=True),
                    nn.GELU(),
                )
                in_dim += 48 * 24
            hid = 384 if (arch == "small" or proj_c == 160) else 512
            self.head = nn.Sequential(
                nn.Linear(in_dim, hid),
                nn.LayerNorm(hid),
                nn.GELU(),
                nn.Dropout(0.12),
                ResidualMLPBlock(hid, drop=0.10),
                nn.LayerNorm(hid),
                nn.Linear(hid, 50),
            )

        self.register_buffer("mean", torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1))
        self.register_buffer("std", torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1))

    def train(self, mode: bool = True):
        super().train(mode)
        for m in self.features.modules():
            if isinstance(m, nn.modules.batchnorm._BatchNorm):
                m.eval()
        return self

    def forward_logits(self, frame_400: torch.Tensor) -> torch.Tensor:
        roi = frame_400 if self.roi_end == 400 else frame_400[:, :, : self.roi_end, : self.roi_end]
        resized = roi if self.roi_end == self.net_width else F.interpolate(roi, size=(self.net_width, self.net_width), mode="bilinear", align_corners=False)
        x = (resized.repeat(1, 3, 1, 1) - self.mean) / self.std
        if self.head_kind == "pyramid_ms":
            f16 = self.features[:9](x)
            f = self.features[9:](f16)
        else:
            f16 = None
            f = self.features(x)
        if self.head_kind == "mlp":
            p1 = F.avg_pool2d(f, kernel_size=self.k_full, stride=self.k_full).flatten(1)
            p2 = F.avg_pool2d(f, kernel_size=self.k_half, stride=self.k_half).flatten(1)
            return self.head(torch.cat((p1, p2), dim=1))
        fp = self.proj(f)
        p_gavg = F.avg_pool2d(fp, kernel_size=self.k_full, stride=self.k_full).flatten(1)
        p_gmax = F.max_pool2d(fp, kernel_size=self.k_full, stride=self.k_full).flatten(1)
        p_2avg = F.avg_pool2d(fp, kernel_size=self.k_half, stride=self.k_half).flatten(1)
        p_2max = F.max_pool2d(fp, kernel_size=self.k_half, stride=self.k_half).flatten(1)
        p_3avg = F.avg_pool2d(fp, kernel_size=self.k_3x3, stride=self.s_3x3).flatten(1)
        feats = [p_gavg, p_gmax, p_2avg, p_2max, p_3avg]
        if self.proj16 is not None:
            fp16 = self.proj16(f16)
            p16_2avg = F.avg_pool2d(fp16, kernel_size=self.k16_2x2, stride=self.k16_2x2).flatten(1)
            p16_2max = F.max_pool2d(fp16, kernel_size=self.k16_2x2, stride=self.k16_2x2).flatten(1)
            p16_4avg = F.avg_pool2d(fp16, kernel_size=self.k16_4x4, stride=self.k16_4x4).flatten(1)
            feats.extend((p16_2avg, p16_2max, p16_4avg))
        return self.head(torch.cat(feats, dim=1))

    def forward(self, frame_400: torch.Tensor) -> torch.Tensor:
        logits = self.forward_logits(frame_400)
        return logits if self.use_logits else torch.sigmoid(logits)


class UnrolledGRUCell(nn.Module):
    """ONNX-safe GRU cell built purely from Linear + Sigmoid + Tanh + elementwise ops (no ONNX GRU op)."""

    def __init__(self, in_dim: int, hid_dim: int):
        super().__init__()
        self.in_dim = int(in_dim)
        self.hid_dim = int(hid_dim)
        self.x_zr = nn.Linear(self.in_dim, 2 * self.hid_dim)
        self.h_zr = nn.Linear(self.hid_dim, 2 * self.hid_dim)
        self.x_n = nn.Linear(self.in_dim, self.hid_dim)
        self.h_n = nn.Linear(self.hid_dim, self.hid_dim)

    def forward(self, x: torch.Tensor, h_prev: torch.Tensor) -> torch.Tensor:
        xz, xr = torch.split(self.x_zr(x), self.hid_dim, dim=-1)
        hz, hr = torch.split(self.h_zr(h_prev), self.hid_dim, dim=-1)
        z = torch.sigmoid(xz + hz)
        r = torch.sigmoid(xr + hr)
        n = torch.tanh(self.x_n(x) + r * self.h_n(h_prev))
        return (1.0 - z) * n + z * h_prev


class StatefulGRUStudent50(nn.Module):
    """Causal stateful residual GRU Student: exact VFTStudent50 backbone/pyramid/feat_head + UnrolledGRUCell."""

    def __init__(
        self,
        arch: str = "small",
        net_width: int = 384,
        head_kind: str = "pyramid",
        roi_end: int = ROI_END,
        hid_dim: int = 256,
        load_pretrained: bool = False,
        *,
        mnv3_weights=None,
    ):
        super().__init__()
        assert net_width in (256, 320, 384), f"net_width must be 256, 320, or 384 for static ONNX pooling: {net_width}"
        assert head_kind in ("mlp", "pyramid", "pyramid_ms"), f"Unsupported head_kind: {head_kind}"
        assert 320 <= roi_end <= 400, f"roi_end must be in [320, 400], got {roi_end}"
        if head_kind == "pyramid_ms":
            assert arch == "small", f"pyramid_ms currently supports arch=='small', got {arch}"
        self.arch = arch
        self.net_width = net_width
        self.head_kind = head_kind
        self.roi_end = roi_end
        self.hid_dim = int(hid_dim)

        self.features, feat_c = build_mobilenet_features(arch, load_pretrained, mnv3_weights)
        self.k_full = net_width // 32
        self.k_half = self.k_full // 2
        self.k_3x3 = 4
        self.s_3x3 = 4 if net_width == 384 else (3 if net_width == 320 else 2)
        self.k16_2x2 = (net_width // 16) // 2
        self.k16_4x4 = (net_width // 16) // 4
        self.proj16 = None

        if head_kind == "mlp":
            self.proj = None
            feat_dim = 384
            self.feat_head = nn.Sequential(
                nn.Linear(feat_c * 5, feat_dim),
                nn.LayerNorm(feat_dim),
                nn.GELU(),
                nn.Dropout(0.10),
            )
            self._sf_final_idx = 4
        else:
            proj_c = 160 if (arch in ("mv2", "large") and net_width == 256) else 256
            self.proj = nn.Sequential(
                nn.Conv2d(feat_c, proj_c, kernel_size=1, bias=True),
                nn.GELU(),
            )
            in_dim = proj_c * 19
            if head_kind == "pyramid_ms":
                self.proj16 = nn.Sequential(
                    nn.Conv2d(48, 48, kernel_size=1, bias=True),
                    nn.GELU(),
                )
                in_dim += 48 * 24
            feat_dim = 384 if (arch == "small" or proj_c == 160) else 512
            self.feat_head = nn.Sequential(
                nn.Linear(in_dim, feat_dim),
                nn.LayerNorm(feat_dim),
                nn.GELU(),
                nn.Dropout(0.12),
                ResidualMLPBlock(feat_dim, drop=0.10),
                nn.LayerNorm(feat_dim),
            )
            self._sf_final_idx = 6

        self.feat_dim = feat_dim
        self.static_head = nn.Linear(feat_dim, 50)
        self.gru_cell = UnrolledGRUCell(feat_dim, self.hid_dim)
        self.temp_head = nn.Linear(self.hid_dim, 50)
        nn.init.zeros_(self.temp_head.weight)
        nn.init.zeros_(self.temp_head.bias)

        self.register_buffer("mean", torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1))
        self.register_buffer("std", torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1))

    def train(self, mode: bool = True):
        super().train(mode)
        for m in self.features.modules():
            if isinstance(m, nn.modules.batchnorm._BatchNorm):
                m.eval()
        return self

    def load_from_single_frame(self, single_frame_state_dict: dict) -> None:
        """Copy backbone + proj + feat_head + static_head from a VFTStudent50 checkpoint and zero temp_head."""
        src = single_frame_state_dict.get("state_dict", single_frame_state_dict)
        mapped = {}
        final_prefix = f"head.{self._sf_final_idx}."
        for k, v in src.items():
            if k.startswith(final_prefix):
                mapped["static_head." + k[len(final_prefix):]] = v
            elif k.startswith("head."):
                mapped["feat_head." + k[len("head."):]] = v
            else:
                mapped[k] = v

        missing, unexpected = self.load_state_dict(mapped, strict=False)
        if unexpected:
            raise ValueError(f"Unexpected keys when loading single-frame checkpoint: {unexpected}")
        expected_missing = {
            k for k in self.state_dict().keys()
            if k.startswith("gru_cell.") or k.startswith("temp_head.")
        }
        if set(missing) != expected_missing:
            raise ValueError(
                f"Key mismatch loading single-frame checkpoint: missing={sorted(set(missing) - expected_missing)}, "
                f"extra_loaded={sorted(expected_missing - set(missing))}"
            )
        nn.init.zeros_(self.temp_head.weight)
        nn.init.zeros_(self.temp_head.bias)

    def extract_embedding(self, frame_400: torch.Tensor) -> torch.Tensor:
        roi = frame_400 if self.roi_end == 400 else frame_400[:, :, : self.roi_end, : self.roi_end]
        resized = (
            roi
            if self.roi_end == self.net_width
            else F.interpolate(roi, size=(self.net_width, self.net_width), mode="bilinear", align_corners=False)
        )
        x = (resized.repeat(1, 3, 1, 1) - self.mean) / self.std
        if self.head_kind == "pyramid_ms":
            f16 = self.features[:9](x)
            f = self.features[9:](f16)
        else:
            f16 = None
            f = self.features(x)
        if self.head_kind == "mlp":
            p1 = F.avg_pool2d(f, kernel_size=self.k_full, stride=self.k_full).flatten(1)
            p2 = F.avg_pool2d(f, kernel_size=self.k_half, stride=self.k_half).flatten(1)
            return self.feat_head(torch.cat((p1, p2), dim=1))
        fp = self.proj(f)
        p_gavg = F.avg_pool2d(fp, kernel_size=self.k_full, stride=self.k_full).flatten(1)
        p_gmax = F.max_pool2d(fp, kernel_size=self.k_full, stride=self.k_full).flatten(1)
        p_2avg = F.avg_pool2d(fp, kernel_size=self.k_half, stride=self.k_half).flatten(1)
        p_2max = F.max_pool2d(fp, kernel_size=self.k_half, stride=self.k_half).flatten(1)
        p_3avg = F.avg_pool2d(fp, kernel_size=self.k_3x3, stride=self.s_3x3).flatten(1)
        feats = [p_gavg, p_gmax, p_2avg, p_2max, p_3avg]
        if self.proj16 is not None:
            fp16 = self.proj16(f16)
            p16_2avg = F.avg_pool2d(fp16, kernel_size=self.k16_2x2, stride=self.k16_2x2).flatten(1)
            p16_2max = F.max_pool2d(fp16, kernel_size=self.k16_2x2, stride=self.k16_2x2).flatten(1)
            p16_4avg = F.avg_pool2d(fp16, kernel_size=self.k16_4x4, stride=self.k16_4x4).flatten(1)
            feats.extend((p16_2avg, p16_2max, p16_4avg))
        return self.feat_head(torch.cat(feats, dim=1))

    def forward_step_logits(
        self,
        frame_400: torch.Tensor,
        h_prev: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        e_t = self.extract_embedding(frame_400)
        h_next = self.gru_cell(e_t, h_prev)
        logits = self.static_head(e_t) + self.temp_head(h_next)
        return logits, h_next

    def forward_step(
        self,
        frame_400: torch.Tensor,
        h_prev: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        logits, h_next = self.forward_step_logits(frame_400, h_prev)
        return torch.sigmoid(logits), h_next

    def forward(
        self,
        frame_400: torch.Tensor,
        h_prev: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        return self.forward_step(frame_400, h_prev)

    def forward_sequence_logits(
        self,
        x_seq: torch.Tensor,
        h0: torch.Tensor | None = None,
        reset_mask: torch.Tensor | None = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Forward a (B, T, 1, 400, 400) clip sequence, returning (logits_seq (B, T, 50), h_last (B, H))."""
        B, T, C, H, W = x_seq.shape
        e_seq = self.extract_embedding(x_seq.reshape(B * T, C, H, W)).reshape(B, T, self.feat_dim)
        h = torch.zeros((B, self.hid_dim), device=x_seq.device, dtype=e_seq.dtype) if h0 is None else h0
        reset_b = reset_mask.bool() if reset_mask is not None else None
        h_list = []
        for t in range(T):
            if reset_b is not None:
                h = torch.where(reset_b[:, t].unsqueeze(-1), torch.zeros_like(h), h)
            h = self.gru_cell(e_seq[:, t], h)
            h_list.append(h)
        h_seq = torch.stack(h_list, dim=1)
        logits_seq = self.static_head(e_seq) + self.temp_head(h_seq)
        return logits_seq, h


class VFTTeacher50(nn.Module):
    """Current DINOv3-L / 12-query spatial attention architecture."""

    def __init__(self, unfreeze_last_n=0, load_pretrained=True, roi_end: int = ROI_END,
                 *, dino_repo=None, dino_weights=None):
        super().__init__()
        assert 320 <= roi_end <= 400, f"roi_end must be in [320, 400], got {roi_end}"
        self.roi_end = roi_end
        weights = (_material_path(dino_weights, "MOUTH50_DINO_WEIGHTS", "--dino-weights")
                   if load_pretrained else None)
        self.backbone = load_dinov3_large_backbone(
            dino_repo=dino_repo, dino_weights=weights, load_pretrained=load_pretrained,
        )
        self.backbone_material = weights.name if weights is not None else None
        self.delta_unfreeze_last_n = 0
        self.head = SpatialAttentionHead()
        self.register_buffer("mean", torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1))
        self.register_buffer("std", torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1))
        self.set_unfreeze(unfreeze_last_n)

    def set_unfreeze(self, n):
        self.unfreeze_last_n = n
        self.delta_unfreeze_last_n = max(self.delta_unfreeze_last_n, n)
        self.backbone.requires_grad_(False)
        if n:
            for block in self.backbone.blocks[-n:]:
                block.requires_grad_(True)
            self.backbone.norm.requires_grad_(True)
        self.head.requires_grad_(True)

    def train(self, mode=True):
        super().train(mode)
        self.backbone.eval()
        if mode and self.unfreeze_last_n:
            for block in self.backbone.blocks[-self.unfreeze_last_n:]:
                block.train()
            self.backbone.norm.train()
        return self

    def features(self, x):
        if x.shape[-2:] != (384, 384):
            roi = x if self.roi_end == 400 else x[:, :, : self.roi_end, : self.roi_end]
            x = F.interpolate(roi, (384, 384), mode="bilinear", align_corners=False)
        return self.backbone.forward_features((x.repeat(1, 3, 1, 1) - self.mean) / self.std)

    def forward_logits(self, x):
        f = self.features(x)
        return self.head(f["x_norm_clstoken"], f["x_norm_patchtokens"])

    def forward(self, x):
        return self.forward_logits(x).sigmoid()


def save_teacher_delta(model: VFTTeacher50, path: Path, **meta):
    """Save trainable parameters against the original backbone, or all state for a full checkpoint."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if model.backbone_material is None:
        kind = "full"
        state = {name: value.detach().cpu().clone() for name, value in model.state_dict().items()}
    else:
        kind = "official-delta"
        current = model.unfreeze_last_n
        model.set_unfreeze(model.delta_unfreeze_last_n)
        try:
            state = {name: param.detach().cpu().clone() for name, param in model.named_parameters()
                     if param.requires_grad}
        finally:
            model.set_unfreeze(current)
    torch.save({
        **meta,
        "checkpoint_kind": kind,
        "backbone_material": model.backbone_material,
        "unfreeze_last_n": int(model.delta_unfreeze_last_n),
        "roi_end": int(model.roi_end),
        "state_dict": state,
    }, path)
    return path


def teacher_from_saved(saved, *, dino_repo=None, dino_weights=None):
    """Restore a full checkpoint or apply a delta to its matching pretrained backbone."""
    delta = saved.get("checkpoint_kind") == "official-delta"
    unfreeze_n = int(saved.get("unfreeze_last_n", 2 if delta else 0))
    roi_end = int(saved.get("roi_end", 336))
    model = VFTTeacher50(
        unfreeze_last_n=unfreeze_n if delta else 0, load_pretrained=delta, roi_end=roi_end,
        dino_repo=dino_repo, dino_weights=dino_weights,
    )
    state = saved["state_dict"]
    if delta:
        if saved["backbone_material"] != model.backbone_material:
            raise ValueError("DINOv3 weights filename does not match the teacher delta's backbone_material.")
        expected = {name for name, param in model.named_parameters() if param.requires_grad}
        if state.keys() != expected:
            raise ValueError(
                f"Teacher delta keys mismatch: extra={sorted(state.keys() - expected)}, "
                f"missing={sorted(expected - state.keys())}"
            )
        model.load_state_dict(state, strict=False)
    else:
        model.load_state_dict(state)
    model.set_unfreeze(0)
    return model
