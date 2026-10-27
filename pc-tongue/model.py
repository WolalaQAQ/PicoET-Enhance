"""Stateless PC implementation of PR4's shadow-final-v2 segmentation baseline."""
from pathlib import Path
import json
import time
import warnings

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F
from torchvision.models import mobilenet_v3_small

REFERENCE_HEAD = "04fe68ac88e8c1fcaf504eba8223ebf33c629d93"
CALIBRATION = (.33, .66, 1.11)
DEFAULT_CHECKPOINT = Path(__file__).resolve().parent / "assets/shadow-final-v2.pt"
ORT_PROVIDERS = {"cuda": "CUDAExecutionProvider", "dml": "DmlExecutionProvider",
                 "openvino": "OpenVINOExecutionProvider", "cpu": "CPUExecutionProvider"}


def downsample400(image):
    f = image.astype(np.uint16)
    return ((f[::2, ::2] + f[1::2, ::2] + f[::2, 1::2] + f[1::2, 1::2] + 2) >> 2).astype(np.uint8)


def to_input(frames):
    x = frames.float()[:, None] / 255
    return F.interpolate(x, (224, 224), mode="bilinear", align_corners=False)


class Segment(nn.Module):
    def __init__(self):
        super().__init__()
        # All weights come from the checkpoint, so no ImageNet download is needed.
        self.encoder = mobilenet_v3_small(weights=None).features[:9]
        self.head = nn.Sequential(nn.Conv2d(89, 16, 1), nn.ReLU(),
                                  nn.Conv2d(16, 16, 3, padding=1), nn.ReLU(), nn.Conv2d(16, 1, 1))

    def features(self, x):
        gray = x.sqrt()
        x = gray.repeat(1, 3, 1, 1)
        x = (x - x.new_tensor([.485, .456, .406])[None, :, None, None]) / x.new_tensor([.229, .224, .225])[None, :, None, None]
        parts = [F.interpolate(gray, (56, 56), mode="bilinear", align_corners=False)]
        for i, block in enumerate(self.encoder):
            x = block(x)
            if i in (1, 3, 8):
                parts.append(F.interpolate(x, (56, 56), mode="bilinear", align_corners=False))
        return torch.cat(parts, 1)

    def forward(self, x):
        return self.head(self.features(x))


def geometry(frame200, probability):
    """Reference thresholds/weighted geometry; overlay coordinates use the 400px image."""
    mask = probability > .5
    area = int(mask.sum())
    nose_mask = frame200.astype(np.float32) / 255 >= .94
    nyy, nxx = np.nonzero(nose_mask)
    nose = [float(nxx.mean() * 2), float(nyy.mean() * 2)] if len(nxx) else None
    my, mx = np.nonzero(mask)
    centroid = None
    if area:
        w = probability[my, mx]
        scale = frame200.shape[0] / probability.shape[0]
        tx = ((mx + .5) * scale * w).sum() / w.sum()
        ty = ((my + .5) * scale * w).sum() / w.sum()
        centroid = [float(tx * 2), float(ty * 2)]
    raw_angle = None
    if area >= 20 and len(nxx) >= 50:
        candidate = float(np.arctan2(ty - nyy.mean(), tx - nxx.mean()))
        if 0.0 < candidate < 1.7:
            raw_angle = candidate
    present = area >= 100 and len(nxx) >= 50
    angle, position = None, 0.0
    if present:
        candidate = float(np.arctan2(ty - nyy.mean(), tx - nxx.mean()))
        if 0.0 < candidate < 1.7:
            angle = candidate
            left, center, right = CALIBRATION
            p = (angle - center) / ((center - left) if angle < center else (right - center))
            p = max(-1.0, min(1.0, p))
            position = 0.0 if abs(p) < .06 else (abs(p) - .06) / .94 * (1 if p > 0 else -1)
        else:
            present = False
    return dict(present=bool(present), mask_area=area, centroid=centroid, nose=nose,
                nose_area=int(len(nxx)), left=-position if position < 0 else 0.0, right=position if position > 0 else 0.0,
                 out=1.0 if present else 0.0, angle=angle, position=float(position), mask=mask,
                 raw=raw_angle is not None, raw_angle=raw_angle)


class TongueModel:
    """No temporal state. Call from one inference worker, not concurrently.

    auto tries installed ORT providers: CUDA Graph, CUDA, OpenVINO GPU, DirectML, CPU.
    Explicit cuda/dml/openvino/cpu and comparison engine:device choices are strict.
    """
    def __init__(self, checkpoint: str, device: str = "auto"):
        start = time.perf_counter()
        # Process-local: this dedicated pipeline only needs one CPU preprocessing thread.
        torch.set_num_threads(1)
        self.checkpoint = str(Path(checkpoint).resolve())
        self.artifact = self.checkpoint
        self.requested_device = device
        self.provider = None
        self.providers = []
        self.available_providers = []
        self.fallbacks = []
        if ":" in device and device.split(":", 1)[0] in ("torch", "torchscript", "onnx", "onnxgraph"):
            self.engine, self.device = device.split(":", 1)
        else:
            self.device = device
            self.engine = "onnxgraph" if self.device == "cuda" else "onnx"
        if self.device not in ("auto", *ORT_PROVIDERS):
            raise ValueError("device must be auto, cuda, dml, openvino, cpu, or engine:device")
        if self.engine == "onnxgraph" and self.device != "cuda":
            raise ValueError("onnxgraph requires CUDA")
        if self.engine in ("torch", "torchscript"):
            if self.device not in ("cuda", "cpu"):
                raise ValueError("PyTorch comparisons require cuda or cpu")
            torch.backends.cuda.matmul.allow_tf32 = False
            torch.backends.cudnn.allow_tf32 = False
            self.network = Segment().eval()
            self.network.load_state_dict(torch.load(self.checkpoint, map_location="cpu", weights_only=True))
            self.network.to(self.device)
            if self.engine == "torchscript":
                with torch.inference_mode():
                    self.network = torch.jit.freeze(torch.jit.trace(self.network, torch.zeros(1, 1, 224, 224, device=self.device)))
        else:
            import onnxruntime as ort
            self.artifact = str(Path(self.checkpoint).with_suffix(".onnx"))
            if not Path(self.artifact).is_file():
                raise FileNotFoundError("ONNX model does not exist: "+self.artifact)
            self.available_providers = ort.get_available_providers()
            choices = [(self.engine, self.device)]
            if device == "auto":
                choices = [("onnxgraph", "cuda"), ("onnx", "cuda"), ("onnx", "openvino"),
                           ("onnx", "dml"), ("onnx", "cpu")]
                choices = [(engine, target) for engine, target in choices
                           if ORT_PROVIDERS[target] in self.available_providers]
            if not choices:
                raise RuntimeError("No supported ONNX Runtime execution provider is available")
            for engine, target in choices:
                try:
                    self._load_ort(ort, engine, target)
                    break
                except Exception as error:
                    if device != "auto":
                        raise
                    self.fallbacks.append(dict(engine=engine, device=target, error=str(error)))
                    warnings.warn(f"ORT {engine}/{target} failed: {error}", RuntimeWarning)
            else:
                raise RuntimeError("All ONNX Runtime backends failed: "+json.dumps(self.fallbacks))
        self.load_ms = (time.perf_counter() - start) * 1000

    def _load_ort(self, ort, engine, device):
        provider = ORT_PROVIDERS[device]
        if provider not in self.available_providers:
            raise RuntimeError(provider+" is not installed in this runtime")
        options = ort.SessionOptions()
        options.intra_op_num_threads = options.inter_op_num_threads = 1
        provider_options = {}
        if device == "cuda":
            # Importing torch first makes its existing CUDA 12/cuDNN DLLs available.
            ort.preload_dlls()
            provider_options = {"cudnn_conv_algo_search": "HEURISTIC", "use_tf32": "0"}
            if engine == "onnxgraph":
                provider_options["enable_cuda_graph"] = "1"
        elif device == "dml":
            options.enable_mem_pattern = False
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            provider_options = {"device_id": "0"}
        elif device == "openvino":
            import openvino  # Preload its native runtime/DLL search paths on Windows.
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
            provider_options = {"device_type": "GPU", "load_config": json.dumps({
                "GPU": {"INFERENCE_PRECISION_HINT": "f32", "PERFORMANCE_HINT": "LATENCY"}})}
        providers = [(provider, provider_options)]
        if device != "cpu":
            providers.append("CPUExecutionProvider")  # Normal per-operator fallback.
        session = ort.InferenceSession(self.artifact, options, providers=providers)
        if provider not in session.get_providers():
            raise RuntimeError(provider+" did not initialize; active providers: "+str(session.get_providers()))
        # Own the selection, so ORT cannot switch providers behind the status display.
        session.disable_fallback()
        sample = np.zeros((1, 1, 224, 224), np.float32)
        if engine == "onnxgraph":
            input_value = ort.OrtValue.ortvalue_from_numpy(sample, "cuda", 0)
            output_value = ort.OrtValue.ortvalue_from_shape_and_type((1, 1, 56, 56), np.float32, "cuda", 0)
            binding = session.io_binding()
            binding.bind_ortvalue_input("image", input_value)
            binding.bind_ortvalue_output("logits", output_value)
            for _ in range(3):
                session.run_with_iobinding(binding)
                binding.synchronize_outputs()
            self.input_value, self.output_value, self.binding = input_value, output_value, binding
        else:
            # Test compilation and first execution before choosing this provider.
            session.run(None, {"image": sample})
        self.engine, self.device, self.provider = engine, device, provider
        self.session, self.providers = session, session.get_providers()

    def describe(self) -> dict:
        return dict(engine=self.engine, device=self.device, checkpoint=self.checkpoint, artifact=self.artifact,
                    requested_device=self.requested_device, provider=self.provider, providers=self.providers,
                    available_providers=self.available_providers, fallbacks=self.fallbacks,
                    reference_head=REFERENCE_HEAD, calibration=dict(left=.33, center=.66, right=1.11,
                    deadzone=.06, min_area=100, nose_threshold=.94, min_nose_area=50,
                    angle_min=0.0, angle_max=1.7, mask_threshold=.5),
                    channels=["out", "left", "right"], unsupported_channels=["up", "down"],
                    image_shape=[400, 400], mask_shape=[56, 56],
                    coordinate_space="centroid/nose [x,y] in original 400x400 image pixels",
                    temporal_smoothing=False, cpu_threads=1, precision="float32; CUDA TF32 disabled",
                    load_ms=self.load_ms)

    @torch.inference_mode()
    def _infer200(self, frame200):
        """Checks also use this with the author's original 200px live samples."""
        start = time.perf_counter()
        x = to_input(torch.from_numpy(np.ascontiguousarray(frame200))[None])
        pre = time.perf_counter()
        if self.engine == "onnxgraph":
            self.input_value.update_inplace(x.numpy())
            self.binding.synchronize_inputs()
            self.session.run_with_iobinding(self.binding)
            self.binding.synchronize_outputs()
            logits = self.binding.copy_outputs_to_cpu()[0][0, 0]
            probability = torch.from_numpy(logits).sigmoid().numpy()
        elif self.engine == "onnx":
            # session.run returns CPU output only after CUDA work and D2H complete.
            logits = self.session.run(None, {"image": x.numpy()})[0][0, 0]
            probability = torch.from_numpy(logits).sigmoid().numpy()
        else:
            x = x.to(self.device)
            logits_t = self.network(x)
            probability = logits_t.sigmoid()[0, 0].cpu().numpy()
            logits = logits_t[0, 0].cpu().numpy()
        infer = time.perf_counter()
        result = geometry(frame200, probability)
        end = time.perf_counter()
        result.update(engine=self.engine, device=self.device,
                      timing_ms=dict(preprocess=(pre-start)*1000, inference=(infer-pre)*1000,
                                     postprocess=(end-infer)*1000, total=(end-start)*1000))
        return result, logits, probability

    def predict(self, image: np.ndarray, config: dict | None = None) -> dict:
        # Config belongs to the service pipeline; baseline thresholds stay frozen here.
        if image.shape != (400, 400) or image.dtype != np.uint8:
            raise ValueError("image must be a 400x400 uint8 grayscale array")
        start = time.perf_counter()
        frame = downsample400(image)
        downsample_ms = (time.perf_counter() - start) * 1000
        result, _, _ = self._infer200(frame)
        result["timing_ms"]["preprocess"] += downsample_ms
        result["timing_ms"]["total"] = (time.perf_counter() - start) * 1000
        return result
