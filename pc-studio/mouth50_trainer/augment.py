"""IR near-mouth camera domain augmentations (std geometric+grad and strong physical IR randomization)."""
import math
import numpy as np
import torch
import torch.nn.functional as F

from . import ROI_END


def augment_std(x_u8: torch.Tensor) -> torch.Tensor:
    x = x_u8.float() / 255.0
    B, device = x.shape[0], x.device
    dx = (torch.rand(B, device=device) * 2.0 - 1.0) * 20.0
    dy = (torch.rand(B, device=device) * 2.0 - 1.0) * 20.0
    scale = 0.88 + 0.26 * torch.rand(B, device=device)
    ang = (torch.rand(B, device=device) * 2.0 - 1.0) * (4.0 * math.pi / 180.0)
    ca, sa = torch.cos(ang), torch.sin(ang)
    theta = torch.zeros((B, 2, 3), device=device, dtype=torch.float32)
    theta[:, 0, 0], theta[:, 0, 1], theta[:, 0, 2] = scale * ca, -scale * sa, dx / 200.0
    theta[:, 1, 0], theta[:, 1, 1], theta[:, 1, 2] = scale * sa, scale * ca, dy / 200.0
    grid = F.affine_grid(theta, size=(B, 1, 400, 400), align_corners=False)
    aug = F.grid_sample(x, grid, mode="bilinear", padding_mode="zeros", align_corners=False)
    if torch.rand(1).item() < 0.25:
        aug = F.avg_pool2d(aug, kernel_size=3, stride=1, padding=1)
    if torch.rand(1).item() < 0.55:
        gx = (torch.rand(B, 1, 1, 1, device=device) * 2.0 - 1.0) * 0.18
        gy = (torch.rand(B, 1, 1, 1, device=device) * 2.0 - 1.0) * 0.18
        xs = torch.linspace(-1.0, 1.0, 400, device=device).view(1, 1, 1, 400)
        ys = torch.linspace(-1.0, 1.0, 400, device=device).view(1, 1, 400, 1)
        aug = aug * (1.0 + gx * xs + gy * ys).clamp(0.70, 1.30)
    bright = 0.62 + 0.90 * torch.rand(B, 1, 1, 1, device=device)
    gamma = 0.75 + 0.50 * torch.rand(B, 1, 1, 1, device=device)
    contrast = 0.80 + 0.45 * torch.rand(B, 1, 1, 1, device=device)
    bias = (torch.rand(B, 1, 1, 1, device=device) * 2.0 - 1.0) * 0.04
    aug = ((aug.clamp(1e-4, 1.0) ** gamma) - 0.15) * contrast + 0.15
    aug = aug * bright + bias
    if torch.rand(1).item() < 0.5:
        aug = aug + torch.randn_like(aug) * 0.012
    return aug.clamp(0.0, 1.0)


def augment_ir_physical(x_u8: torch.Tensor, roi_end: int = ROI_END, distractor_p: float = 0.0) -> torch.Tensor:
    """Physical IR-camera domain randomization: affine+shear, directional IR lighting, vignette, highlight/shadow, blur/sharpen, gentle border cutout (no distractors)."""
    del distractor_p  # synthetic peripheral distractors removed
    x = x_u8.float() / 255.0
    B, device = x.shape[0], x.device

    dx = (torch.rand(B, device=device) * 2.0 - 1.0) * 18.0
    dy = (torch.rand(B, device=device) * 2.0 - 1.0) * 18.0
    scale = 0.85 + 0.30 * torch.rand(B, device=device)
    ang = (torch.rand(B, device=device) * 2.0 - 1.0) * (5.0 * math.pi / 180.0)
    shx = (torch.rand(B, device=device) * 2.0 - 1.0) * 0.035
    shy = (torch.rand(B, device=device) * 2.0 - 1.0) * 0.035
    ca, sa = torch.cos(ang), torch.sin(ang)
    theta = torch.zeros((B, 2, 3), device=device, dtype=torch.float32)
    theta[:, 0, 0] = scale * (ca + shy * sa)
    theta[:, 0, 1] = scale * (shx * ca - sa)
    theta[:, 0, 2] = dx / 200.0
    theta[:, 1, 0] = scale * (sa + shy * ca)
    theta[:, 1, 1] = scale * (shx * sa + ca)
    theta[:, 1, 2] = dy / 200.0
    grid = F.affine_grid(theta, size=(B, 1, 400, 400), align_corners=False)
    aug = F.grid_sample(x, grid, mode="bilinear", padding_mode="zeros", align_corners=False)

    r_opt = torch.rand(1).item()
    if r_opt < 0.22:
        aug = F.avg_pool2d(aug, kernel_size=3, stride=1, padding=1)
    elif r_opt < 0.34:
        aug = F.avg_pool2d(aug, kernel_size=(1, 3), stride=1, padding=(0, 1))
    elif r_opt < 0.48:
        blurred = F.avg_pool2d(aug, kernel_size=3, stride=1, padding=1)
        aug = (aug + 0.35 * (aug - blurred)).clamp(0.0, 1.0)

    xs = torch.linspace(-1.0, 1.0, 400, device=device).view(1, 1, 1, 400)
    ys = torch.linspace(-1.0, 1.0, 400, device=device).view(1, 1, 400, 1)
    if torch.rand(1).item() < 0.65:
        gx = (torch.rand(B, 1, 1, 1, device=device) * 2.0 - 1.0) * 0.22
        gy = (torch.rand(B, 1, 1, 1, device=device) * 2.0 - 1.0) * 0.22
        aug = aug * (1.0 + gx * xs + gy * ys).clamp(0.68, 1.32)
    if torch.rand(1).item() < 0.45:
        vx = (torch.rand(B, 1, 1, 1, device=device) * 2.0 - 1.0) * 0.25
        vy = (torch.rand(B, 1, 1, 1, device=device) * 2.0 - 1.0) * 0.25
        vs = torch.rand(B, 1, 1, 1, device=device) * 0.35
        r2 = ((xs - vx) ** 2 + (ys - vy) ** 2) * 0.5
        aug = aug * (1.0 - vs * r2).clamp(0.55, 1.05)

    bright = 0.56 + 0.94 * torch.rand(B, 1, 1, 1, device=device)
    gamma = 0.72 + 0.56 * torch.rand(B, 1, 1, 1, device=device)
    contrast = 0.75 + 0.55 * torch.rand(B, 1, 1, 1, device=device)
    bias = (torch.rand(B, 1, 1, 1, device=device) * 2.0 - 1.0) * 0.05
    aug = ((aug.clamp(1e-4, 1.0) ** gamma) - 0.15) * contrast + 0.15
    aug = (aug * bright + bias).clamp(0.0, 1.0)

    if torch.rand(1).item() < 0.15:
        side = int(torch.randint(0, 4, (1,)).item())
        w_cut = int(torch.randint(6, 15, (1,)).item())
        fill_val = float(torch.rand(1).item() * 0.25)
        if side == 0:
            aug[:, :, :w_cut, :] = fill_val
        elif side == 1:
            aug[:, :, roi_end - w_cut : roi_end, :] = fill_val
        elif side == 2:
            aug[:, :, :, :w_cut] = fill_val
        else:
            aug[:, :, :, roi_end - w_cut : roi_end] = fill_val

    if torch.rand(1).item() < 0.55:
        aug = aug + torch.randn_like(aug) * (0.006 + 0.012 * torch.rand(B, 1, 1, 1, device=device))
    return aug.clamp(0.0, 1.0)


def extract_teacher_roi400(x_u8: torch.Tensor, augment: bool, dx_const: float = 0.0, dy_const: float = 0.0) -> torch.Tensor:
    """Full 400x400 -> 384x384 extraction with cross-fitting photometric & directional IR randomization (no distractors)."""
    x = x_u8.float() / 255.0
    B, device = x.shape[0], x.device
    half = 200.0
    cx0, cy0 = 200.0, 200.0
    theta = torch.zeros((B, 2, 3), device=device, dtype=torch.float32)
    if not augment:
        theta[:, 0, 0] = half / 200.0
        theta[:, 1, 1] = half / 200.0
        theta[:, 0, 2] = (cx0 + dx_const) / 200.0 - 1.0
        theta[:, 1, 2] = (cy0 + dy_const) / 200.0 - 1.0
    else:
        dx = (torch.rand(B, device=device) * 2.0 - 1.0) * 16.0
        dy = (torch.rand(B, device=device) * 2.0 - 1.0) * 16.0
        scale = 0.90 + 0.20 * torch.rand(B, device=device)
        ang = (torch.rand(B, device=device) * 2.0 - 1.0) * (4.0 * math.pi / 180.0)
        ca, sa = torch.cos(ang), torch.sin(ang)
        r = (half / 200.0) * scale
        theta[:, 0, 0] = r * ca
        theta[:, 0, 1] = -r * sa
        theta[:, 1, 0] = r * sa
        theta[:, 1, 1] = r * ca
        theta[:, 0, 2] = (cx0 + dx) / 200.0 - 1.0
        theta[:, 1, 2] = (cy0 + dy) / 200.0 - 1.0

    grid = F.affine_grid(theta, size=(B, 1, 384, 384), align_corners=False)
    roi = F.grid_sample(x, grid, mode="bilinear", padding_mode="zeros", align_corners=False)
    if augment:
        if torch.rand(1).item() < 0.20:
            roi = F.avg_pool2d(roi, kernel_size=3, stride=1, padding=1)
        if torch.rand(1).item() < 0.55:
            gx = (torch.rand(B, 1, 1, 1, device=device) * 2.0 - 1.0) * 0.18
            gy = (torch.rand(B, 1, 1, 1, device=device) * 2.0 - 1.0) * 0.18
            xs = torch.linspace(-1.0, 1.0, 384, device=device).view(1, 1, 1, 384)
            ys = torch.linspace(-1.0, 1.0, 384, device=device).view(1, 1, 384, 1)
            roi = roi * (1.0 + gx * xs + gy * ys).clamp(0.72, 1.28)
        bright = 0.65 + 0.85 * torch.rand(B, 1, 1, 1, device=device)
        gamma = 0.76 + 0.48 * torch.rand(B, 1, 1, 1, device=device)
        contrast = 0.80 + 0.42 * torch.rand(B, 1, 1, 1, device=device)
        bias = (torch.rand(B, 1, 1, 1, device=device) * 2.0 - 1.0) * 0.035
        roi = ((roi.clamp(1e-4, 1.0) ** gamma) - 0.15) * contrast + 0.15
        roi = roi * bright + bias
        if torch.rand(1).item() < 0.50:
            roi = roi + torch.randn_like(roi) * 0.010
        roi = roi.clamp(0.0, 1.0)
    return roi


def apply_teacher_mixup(roi: torch.Tensor, t_b: torch.Tensor, w_b: torch.Tensor, beta: float = 0.30):
    perm = torch.randperm(roi.shape[0], device=roi.device)
    lam = float(np.random.beta(beta, beta))
    lam = max(lam, 1.0 - lam)
    if lam >= 0.97:
        return roi, t_b, w_b
    roi = lam * roi + (1.0 - lam) * roi[perm]
    both_sup = (w_b >= 0.20) & (w_b[perm] >= 0.20)
    t_mix = lam * t_b + (1.0 - lam) * t_b[perm]
    t_b = torch.where(both_sup, t_mix, torch.zeros_like(t_b))
    w_b = torch.where(both_sup, torch.minimum(w_b, w_b[perm]), torch.zeros_like(w_b))
    return roi, t_b, w_b


def augment_clip_ir_physical(x_u8_seq: torch.Tensor) -> torch.Tensor:
    """Clip-consistent physical IR augmentation on (B, T, 1, 400, 400) uint8 tensor -> float [0, 1].

    Applies one shared affine + directional IR + vignette + gamma/brightness/contrast transform per clip,
    plus tiny independent per-frame Gaussian noise and sub-frame +/-0.5px jitter.
    """
    x = x_u8_seq.float() / 255.0
    B, T, C, H, W = x.shape
    device = x.device

    dx = (torch.rand(B, 1, device=device) * 2.0 - 1.0) * 18.0
    dy = (torch.rand(B, 1, device=device) * 2.0 - 1.0) * 18.0
    # Tiny per-frame +/-0.5px jitter
    dx = dx + (torch.rand(B, T, device=device) * 2.0 - 1.0) * 0.5
    dy = dy + (torch.rand(B, T, device=device) * 2.0 - 1.0) * 0.5

    scale = (0.85 + 0.30 * torch.rand(B, 1, device=device)).expand(B, T)
    ang = ((torch.rand(B, 1, device=device) * 2.0 - 1.0) * (5.0 * math.pi / 180.0)).expand(B, T)
    shx = ((torch.rand(B, 1, device=device) * 2.0 - 1.0) * 0.035).expand(B, T)
    shy = ((torch.rand(B, 1, device=device) * 2.0 - 1.0) * 0.035).expand(B, T)
    ca, sa = torch.cos(ang), torch.sin(ang)

    theta = torch.zeros((B * T, 2, 3), device=device, dtype=torch.float32)
    sc_f, ca_f, sa_f = scale.reshape(-1), ca.reshape(-1), sa.reshape(-1)
    shx_f, shy_f = shx.reshape(-1), shy.reshape(-1)
    theta[:, 0, 0] = sc_f * (ca_f + shy_f * sa_f)
    theta[:, 0, 1] = sc_f * (shx_f * ca_f - sa_f)
    theta[:, 0, 2] = dx.reshape(-1) / 200.0
    theta[:, 1, 0] = sc_f * (sa_f + shy_f * ca_f)
    theta[:, 1, 1] = sc_f * (shx_f * sa_f + ca_f)
    theta[:, 1, 2] = dy.reshape(-1) / 200.0

    x_flat = x.reshape(B * T, C, H, W)
    grid = F.affine_grid(theta, size=(B * T, 1, H, W), align_corners=False)
    aug = F.grid_sample(x_flat, grid, mode="bilinear", padding_mode="zeros", align_corners=False)

    r_opt = torch.rand(1).item()
    if r_opt < 0.20:
        aug = F.avg_pool2d(aug, kernel_size=3, stride=1, padding=1)
    elif r_opt < 0.35:
        blurred = F.avg_pool2d(aug, kernel_size=3, stride=1, padding=1)
        aug = (aug + 0.30 * (aug - blurred)).clamp(0.0, 1.0)

    aug = aug.reshape(B, T, 1, H, W)
    xs = torch.linspace(-1.0, 1.0, W, device=device).view(1, 1, 1, 1, W)
    ys = torch.linspace(-1.0, 1.0, H, device=device).view(1, 1, 1, H, 1)
    if torch.rand(1).item() < 0.65:
        gx = (torch.rand(B, 1, 1, 1, 1, device=device) * 2.0 - 1.0) * 0.22
        gy = (torch.rand(B, 1, 1, 1, 1, device=device) * 2.0 - 1.0) * 0.22
        aug = aug * (1.0 + gx * xs + gy * ys).clamp(0.68, 1.32)
    if torch.rand(1).item() < 0.45:
        vx = (torch.rand(B, 1, 1, 1, 1, device=device) * 2.0 - 1.0) * 0.25
        vy = (torch.rand(B, 1, 1, 1, 1, device=device) * 2.0 - 1.0) * 0.25
        vs = torch.rand(B, 1, 1, 1, 1, device=device) * 0.35
        r2 = ((xs - vx) ** 2 + (ys - vy) ** 2) * 0.5
        aug = aug * (1.0 - vs * r2).clamp(0.55, 1.05)

    bright = 0.56 + 0.94 * torch.rand(B, 1, 1, 1, 1, device=device)
    gamma = 0.72 + 0.56 * torch.rand(B, 1, 1, 1, 1, device=device)
    contrast = 0.75 + 0.55 * torch.rand(B, 1, 1, 1, 1, device=device)
    bias = (torch.rand(B, 1, 1, 1, 1, device=device) * 2.0 - 1.0) * 0.05
    aug = ((aug.clamp(1e-4, 1.0) ** gamma) - 0.15) * contrast + 0.15
    aug = (aug * bright + bias).clamp(0.0, 1.0)

    # Independent per-frame sensor noise
    aug = aug + torch.randn_like(aug) * 0.006
    return aug.clamp(0.0, 1.0)
