//! ONNX Runtime model loading and inference for Face50, MouthLink, and Segmentation models.
//!
//! Exact Rust port of `pc-studio/face_model.py`, `pc-studio/mouthlink.py`,
//! and the ONNX Runtime path of `pc-studio/model.py`, using `ort` 2.0.0-rc.15
//! (`load-dynamic` + `api-17` + `directml`/`cuda`/`openvino`).

use crate::face_schema::{channel_index, CHANNELS as FACE_CHANNELS, SCHEMA};
use crate::personalization::WizardFrameRaw;
use crate::state::RawTongueInput;
use ort::environment::Environment;
use ort::ep::{self, ArbitrarilyConfigurableExecutionProvider, ExecutionProviderDispatch};
use ort::memory::{AllocationDevice, Allocator, AllocatorType, MemoryInfo, MemoryType};
use ort::session::builder::GraphOptimizationLevel;
use ort::session::{IoBinding, Session};
use ort::value::{Tensor, TensorElementType, TensorRef, ValueType};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, HashSet};
use std::ffi::CStr;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Instant;

pub const REFERENCE_HEAD: &str = "04fe68ac88e8c1fcaf504eba8223ebf33c629d93";
pub const SEGMENTATION_CALIBRATION: (f64, f64, f64) = (0.33, 0.66, 1.11);
pub const MOUTHLINK_CHANNELS: [(&str, &str); 6] = [
    ("out", "TongueOut"),
    ("left", "TongueLeft"),
    ("right", "TongueRight"),
    ("up", "TongueUp"),
    ("down", "TongueDown"),
    ("roll", "TongueRoll"),
];

const PRECISION_BITS: u32 = 32 - 8 - 2; // 22-bit fixed-point coefficients matching Pillow ImagingResample.c

static ORT_ENV: Mutex<Option<Environment>> = Mutex::new(None);

fn provider_name(device: &str) -> Option<&'static str> {
    match device {
        "cuda" => Some("CUDAExecutionProvider"),
        "dml" => Some("DmlExecutionProvider"),
        "openvino" => Some("OpenVINOExecutionProvider"),
        "cpu" => Some("CPUExecutionProvider"),
        _ => None,
    }
}

fn default_ort_dylib_filename() -> &'static str {
    #[cfg(target_os = "windows")]
    {
        "onnxruntime.dll"
    }
    #[cfg(target_os = "macos")]
    {
        "libonnxruntime.dylib"
    }
    #[cfg(all(not(target_os = "windows"), not(target_os = "macos")))]
    {
        "libonnxruntime.so"
    }
}

/// Resolve the candidate dynamic library search list for `onnxruntime`.
/// Search order:
/// 1. `ORT_DYLIB_PATH` env var
/// 2. `<exe_dir>/onnxruntime.dll`
/// 3. `<exe_dir>/../onnxruntime.dll`
/// 4. Windows dev fallback: `CONDA_PREFIX/Lib/site-packages/onnxruntime/capi/onnxruntime.dll`
/// 5. Standard PATH / OS loader lookup (`onnxruntime.dll`)
pub fn resolve_ort_dylib_candidates() -> Vec<PathBuf> {
    let mut out = Vec::new();
    let dylib_name = default_ort_dylib_filename();

    if let Ok(env_path) = std::env::var("ORT_DYLIB_PATH") {
        let trimmed = env_path.trim();
        if !trimmed.is_empty() {
            out.push(PathBuf::from(trimmed));
        }
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(exe_dir) = exe.parent() {
            let direct = exe_dir.join(dylib_name);
            if direct.is_file() {
                out.push(direct);
            }
            if let Some(parent_dir) = exe_dir.parent() {
                let parent_candidate = parent_dir.join(dylib_name);
                if parent_candidate.is_file() {
                    out.push(parent_candidate);
                }
            }
        }
    }

    #[cfg(windows)]
    if let Ok(conda_prefix) = std::env::var("CONDA_PREFIX") {
        let trimmed = conda_prefix.trim();
        if !trimmed.is_empty() {
            let conda_dll = PathBuf::from(trimmed)
                .join("Lib")
                .join("site-packages")
                .join("onnxruntime")
                .join("capi")
                .join(dylib_name);
            if conda_dll.is_file() {
                out.push(conda_dll);
            }
        }
    }

    out.push(PathBuf::from(dylib_name));
    out
}

/// Initialize the process-global ONNX Runtime environment at most once safely.
pub fn ensure_ort_environment() -> Result<Environment, String> {
    let mut guard = ORT_ENV.lock().unwrap();
    if let Some(ref env) = *guard {
        return Ok(env.clone());
    }

    let candidates = resolve_ort_dylib_candidates();
    let mut errors = Vec::new();
    for candidate in candidates {
        match ort::init_from(&candidate) {
            Ok(builder) => {
                let env = builder
                    .with_name("picoet-pc-studio")
                    .with_telemetry(false)
                    .build()
                    .map_err(|e| format!("Failed to initialize ONNX Runtime environment: {e}"))?;
                *guard = Some(env.clone());
                return Ok(env);
            }
            Err(err) => {
                errors.push(format!("{}: {err}", candidate.display()));
            }
        }
    }
    Err(format!(
        "Unable to load ONNX Runtime dynamic library (tried: {})",
        errors.join("; ")
    ))
}

/// Query all execution providers compiled into the loaded ONNX Runtime library.
pub fn available_ort_providers() -> Result<Vec<String>, String> {
    let _env = ensure_ort_environment()?;
    let api = ort::api();
    let mut providers: *mut *mut core::ffi::c_char = core::ptr::null_mut();
    let mut num_providers: core::ffi::c_int = 0;
    let status = unsafe { (api.GetAvailableProviders)(&mut providers, &mut num_providers) };
    if !status.0.is_null() {
        let msg = unsafe { CStr::from_ptr((api.GetErrorMessage)(status.0)) }
            .to_string_lossy()
            .into_owned();
        unsafe { (api.ReleaseStatus)(status.0) };
        return Err(format!("GetAvailableProviders failed: {msg}"));
    }
    if providers.is_null() || num_providers <= 0 {
        return Ok(Vec::new());
    }
    let mut out = Vec::with_capacity(num_providers as usize);
    for i in 0..num_providers {
        let cstr = unsafe { CStr::from_ptr(*providers.offset(i as isize)) };
        out.push(cstr.to_string_lossy().into_owned());
    }
    let release_status = unsafe { (api.ReleaseAvailableProviders)(providers, num_providers) };
    if !release_status.0.is_null() {
        unsafe { (api.ReleaseStatus)(release_status.0) };
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Exact Pillow BILINEAR GRAY8 resampling & Segmentation preprocessing/geometry
// ---------------------------------------------------------------------------

struct ResampleKernel {
    bounds: Vec<(usize, usize)>,
    coeffs: Vec<i32>,
    ksize: usize,
}

fn precompute_pillow_bilinear_coeffs(in_size: usize, out_size: usize) -> ResampleKernel {
    let scale = (in_size as f64) / (out_size as f64);
    let filterscale = scale.max(1.0);
    let support = 1.0 * filterscale;
    let ksize = (support.ceil() as usize) * 2 + 1;
    let inv_scale = 1.0 / filterscale;
    let shift_scale = (1u64 << PRECISION_BITS) as f64;

    let mut bounds = Vec::with_capacity(out_size);
    let mut coeffs = vec![0i32; out_size * ksize];
    let mut temp_w = vec![0.0f64; ksize];

    for xx in 0..out_size {
        let center = (xx as f64 + 0.5) * scale;
        let xmin = ((center - support + 0.5).floor() as isize).max(0) as usize;
        let xmax = ((center + support + 0.5).floor() as isize).min(in_size as isize) as usize;
        let xsize = xmax.saturating_sub(xmin);
        let mut ww = 0.0f64;
        for x in 0..xsize {
            let v = ((x + xmin) as f64 - center + 0.5) * inv_scale;
            let av = v.abs();
            let w = if av < 1.0 { 1.0 - av } else { 0.0 };
            temp_w[x] = w;
            ww += w;
        }
        let dst = &mut coeffs[xx * ksize..xx * ksize + xsize];
        for x in 0..xsize {
            let norm = if ww != 0.0 { temp_w[x] / ww } else { 0.0 };
            dst[x] = if norm < 0.0 {
                (-0.5 + norm * shift_scale) as i32
            } else {
                (0.5 + norm * shift_scale) as i32
            };
        }
        bounds.push((xmin, xsize));
    }

    ResampleKernel {
        bounds,
        coeffs,
        ksize,
    }
}

/// Exact Pillow `Image.Resampling.BILINEAR` GRAY8 resize (separable horizontal then vertical,
/// 22-bit fixed-point quantized triangle filter matching `ImagingResample.c`).
pub fn resize_pillow_bilinear_gray8(
    gray: &[u8],
    in_w: usize,
    in_h: usize,
    out_w: usize,
    out_h: usize,
) -> Result<Vec<u8>, String> {
    if in_w == 0 || in_h == 0 || out_w == 0 || out_h == 0 {
        return Err("Resize dimensions must be positive".to_string());
    }
    if gray.len() != in_w * in_h {
        return Err(format!(
            "Expected {} grayscale bytes for {}x{}, got {}",
            in_w * in_h,
            in_w,
            in_h,
            gray.len()
        ));
    }
    if in_w == out_w && in_h == out_h {
        return Ok(gray.to_vec());
    }

    let half: i64 = 1i64 << (PRECISION_BITS - 1);

    // Horizontal pass: in_h x in_w -> in_h x out_w
    let horiz: Vec<u8> = if out_w != in_w {
        let kernel = precompute_pillow_bilinear_coeffs(in_w, out_w);
        let mut tmp = vec![0u8; in_h * out_w];
        for y in 0..in_h {
            let src_row = &gray[y * in_w..(y + 1) * in_w];
            let dst_row = &mut tmp[y * out_w..(y + 1) * out_w];
            for xx in 0..out_w {
                let (xmin, xsize) = kernel.bounds[xx];
                let k = &kernel.coeffs[xx * kernel.ksize..xx * kernel.ksize + xsize];
                let mut acc = half;
                for x in 0..xsize {
                    acc += (src_row[xmin + x] as i64) * (k[x] as i64);
                }
                let v = (acc >> PRECISION_BITS).clamp(0, 255) as u8;
                dst_row[xx] = v;
            }
        }
        tmp
    } else {
        gray.to_vec()
    };

    if out_h == in_h {
        return Ok(horiz);
    }

    // Vertical pass: in_h x out_w -> out_h x out_w
    let kernel = precompute_pillow_bilinear_coeffs(in_h, out_h);
    let mut out = vec![0u8; out_h * out_w];
    for yy in 0..out_h {
        let (ymin, ysize) = kernel.bounds[yy];
        let k = &kernel.coeffs[yy * kernel.ksize..yy * kernel.ksize + ysize];
        let dst_row = &mut out[yy * out_w..(yy + 1) * out_w];
        for x in 0..out_w {
            let mut acc = half;
            for y in 0..ysize {
                acc += (horiz[(ymin + y) * out_w + x] as i64) * (k[y] as i64);
            }
            dst_row[x] = (acc >> PRECISION_BITS).clamp(0, 255) as u8;
        }
    }
    Ok(out)
}

/// Exact Pillow `BILINEAR` GRAY8 resize from 400x400 followed by `/ 255.0` to `f32`.
pub fn prepare_input(gray400: &[u8], width: usize, height: usize) -> Result<Vec<f32>, String> {
    if gray400.len() != 400 * 400 {
        return Err("image must be a 400x400 uint8 grayscale array".to_string());
    }
    if !(1..=400).contains(&width) || !(1..=400).contains(&height) {
        return Err("Input dimensions must be between 1 and 400".to_string());
    }
    let resized = resize_pillow_bilinear_gray8(gray400, 400, 400, width, height)?;
    let mut out = Vec::with_capacity(resized.len());
    for px in resized {
        out.push((px as f32) / 255.0);
    }
    Ok(out)
}

/// Exact 2x2 average downsample of a 400x400 grayscale image to 200x200 with `(+ 2) >> 2`.
pub fn downsample400(gray400: &[u8]) -> Result<Vec<u8>, String> {
    if gray400.len() != 400 * 400 {
        return Err("image must be a 400x400 uint8 grayscale array".to_string());
    }
    let mut out = vec![0u8; 200 * 200];
    for y in 0..200 {
        let r0 = (y * 2) * 400;
        let r1 = (y * 2 + 1) * 400;
        let dst_row = y * 200;
        for x in 0..200 {
            let x2 = x * 2;
            let sum = gray400[r0 + x2] as u16
                + gray400[r1 + x2] as u16
                + gray400[r0 + x2 + 1] as u16
                + gray400[r1 + x2 + 1] as u16
                + 2;
            out[dst_row + x] = (sum >> 2) as u8;
        }
    }
    Ok(out)
}

/// Bilinear resize `200x200 -> 224x224` with `align_corners = false` matching PyTorch `to_input`.
pub fn to_segmentation_input(frame200: &[u8]) -> Result<Vec<f32>, String> {
    if frame200.len() != 200 * 200 {
        return Err("frame200 must be a 200x200 uint8 grayscale array".to_string());
    }
    let mut f200 = vec![0.0f32; 200 * 200];
    for (dst, &src) in f200.iter_mut().zip(frame200.iter()) {
        *dst = (src as f32) / 255.0;
    }

    let in_size = 200usize;
    let out_size = 224usize;
    let scale = (in_size as f32) / (out_size as f32);

    let mut coords = Vec::with_capacity(out_size);
    for i in 0..out_size {
        let src = ((i as f32 + 0.5) * scale - 0.5).clamp(0.0, (in_size - 1) as f32);
        let i0 = src.floor() as usize;
        let i1 = (i0 + 1).min(in_size - 1);
        let w = src - (i0 as f32);
        coords.push((i0, i1, w));
    }

    let mut out = vec![0.0f32; out_size * out_size];
    for (oy, &(y0, y1, wy)) in coords.iter().enumerate() {
        let row0 = &f200[y0 * in_size..(y0 + 1) * in_size];
        let row1 = &f200[y1 * in_size..(y1 + 1) * in_size];
        let dst = &mut out[oy * out_size..(oy + 1) * out_size];
        let one_my = 1.0 - wy;
        for (ox, &(x0, x1, wx)) in coords.iter().enumerate() {
            let one_mx = 1.0 - wx;
            let top = row0[x0] * one_mx + row0[x1] * wx;
            let bot = row1[x0] * one_mx + row1[x1] * wx;
            dst[ox] = top * one_my + bot * wy;
        }
    }
    Ok(out)
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GeometryResult {
    pub present: bool,
    pub mask_area: i64,
    pub centroid: Option<[f64; 2]>,
    pub nose: Option<[f64; 2]>,
    pub nose_area: i64,
    pub left: f64,
    pub right: f64,
    pub out: f64,
    pub angle: Option<f64>,
    pub position: f64,
    #[serde(skip_serializing)]
    pub mask: Vec<bool>,
    pub raw: bool,
    pub raw_angle: Option<f64>,
}

/// Reference thresholds/weighted geometry matching `pc-studio/model.py::geometry`.
/// Overlay coordinates use the 400px image space.
pub fn geometry(frame200: &[u8], prob56: &[f32]) -> Result<GeometryResult, String> {
    if frame200.len() != 200 * 200 {
        return Err("frame200 must be 200x200".to_string());
    }
    if prob56.len() != 56 * 56 {
        return Err("prob56 must be 56x56".to_string());
    }

    let mut mask = vec![false; 56 * 56];
    let mut area: usize = 0;
    let mut w_sum = 0.0f64;
    let mut wx_sum = 0.0f64;
    let mut wy_sum = 0.0f64;
    let scale = 200.0f64 / 56.0f64;

    for y in 0..56 {
        for x in 0..56 {
            let idx = y * 56 + x;
            let p = prob56[idx];
            if p > 0.5 {
                mask[idx] = true;
                area += 1;
                let w = p as f64;
                w_sum += w;
                wx_sum += (x as f64 + 0.5) * scale * w;
                wy_sum += (y as f64 + 0.5) * scale * w;
            }
        }
    }

    let mut nose_area: usize = 0;
    let mut nx_sum = 0.0f64;
    let mut ny_sum = 0.0f64;
    for y in 0..200 {
        let row = y * 200;
        for x in 0..200 {
            if (frame200[row + x] as f32) / 255.0 >= 0.94f32 {
                nose_area += 1;
                nx_sum += x as f64;
                ny_sum += y as f64;
            }
        }
    }

    let (nose, nx_mean, ny_mean) = if nose_area > 0 {
        let xm = nx_sum / (nose_area as f64);
        let ym = ny_sum / (nose_area as f64);
        (Some([xm * 2.0, ym * 2.0]), xm, ym)
    } else {
        (None, 0.0, 0.0)
    };

    let (centroid, tx, ty) = if area > 0 {
        let tx = wx_sum / w_sum;
        let ty = wy_sum / w_sum;
        (Some([tx * 2.0, ty * 2.0]), tx, ty)
    } else {
        (None, 0.0, 0.0)
    };

    let mut raw_angle = None;
    if area >= 20 && nose_area >= 50 {
        let candidate = (ty - ny_mean).atan2(tx - nx_mean);
        if candidate > 0.0 && candidate < 1.7 {
            raw_angle = Some(candidate);
        }
    }

    let mut present = area >= 100 && nose_area >= 50;
    let mut angle = None;
    let mut position = 0.0f64;
    if present {
        let candidate = (ty - ny_mean).atan2(tx - nx_mean);
        if candidate > 0.0 && candidate < 1.7 {
            angle = Some(candidate);
            let (left, center, right) = SEGMENTATION_CALIBRATION;
            let denom = if candidate < center {
                center - left
            } else {
                right - center
            };
            let p = ((candidate - center) / denom).clamp(-1.0, 1.0);
            position = if p.abs() < 0.06 {
                0.0
            } else {
                (p.abs() - 0.06) / 0.94 * if p > 0.0 { 1.0 } else { -1.0 }
            };
        } else {
            present = false;
        }
    }

    Ok(GeometryResult {
        present,
        mask_area: area as i64,
        centroid,
        nose,
        nose_area: nose_area as i64,
        left: if position < 0.0 { -position } else { 0.0 },
        right: if position > 0.0 { position } else { 0.0 },
        out: if present { 1.0 } else { 0.0 },
        angle,
        position,
        mask,
        raw: raw_angle.is_some(),
        raw_angle,
    })
}

// ---------------------------------------------------------------------------
// Predictions & unified MouthModel / LoadedModel API
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct TimingMs {
    pub preprocess: f64,
    pub inference: f64,
    pub postprocess: f64,
    pub total: f64,
}

fn serialize_f32_50<S>(values: &[f32; 50], serializer: S) -> Result<S::Ok, S::Error>
where
    S: serde::Serializer,
{
    values.as_slice().serialize(serializer)
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct FacePrediction {
    #[serde(serialize_with = "serialize_f32_50")]
    pub values: [f32; 50],
    pub channels: BTreeMap<String, f32>,
    pub valid_mask: u64,
    pub schema: &'static str,
    pub timing_ms: TimingMs,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MouthLinkPrediction {
    pub out: f64,
    pub left: f64,
    pub right: f64,
    pub up: f64,
    pub down: f64,
    pub roll: f64,
    pub present: bool,
    pub extended: bool,
    pub out_hysteresis: [f64; 2],
    pub timing_ms: TimingMs,
}

impl MouthLinkPrediction {
    pub fn to_raw_tongue_input(&self) -> RawTongueInput {
        RawTongueInput {
            state: None,
            present: self.present,
            left: self.left,
            right: self.right,
            out: self.out,
            extended: self.extended,
            up: Some(self.up),
            down: Some(self.down),
            roll: Some(self.roll),
            out_hysteresis: Some(self.out_hysteresis),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SegmentationPrediction {
    #[serde(flatten)]
    pub geometry: GeometryResult,
    pub engine: String,
    pub device: String,
    pub timing_ms: TimingMs,
}

impl SegmentationPrediction {
    pub fn to_raw_tongue_input(&self) -> RawTongueInput {
        RawTongueInput {
            state: None,
            present: self.geometry.present,
            left: self.geometry.left,
            right: self.geometry.right,
            out: self.geometry.out,
            extended: false,
            up: None,
            down: None,
            roll: None,
            out_hysteresis: None,
        }
    }

    pub fn to_wizard_raw(&self) -> WizardFrameRaw {
        WizardFrameRaw {
            nose_area: self.geometry.nose_area,
            raw_angle: self.geometry.raw_angle,
            mask_area: self.geometry.mask_area,
            mask: self.geometry.mask.clone(),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(untagged)]
pub enum ModelOutput {
    Face50(FacePrediction),
    MouthLink(MouthLinkPrediction),
    Segmentation(SegmentationPrediction),
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct PredictContext {
    pub stale_ms: f64,
    pub session_id: Option<u64>,
    pub source_ns: Option<u64>,
}

impl Default for PredictContext {
    fn default() -> Self {
        Self {
            stale_ms: 120.0,
            session_id: None,
            source_ns: None,
        }
    }
}

/// Trait implemented by all real models and mock test models for `Runtime`.
pub trait MouthModel: Send {
    fn model_kind(&self) -> &'static str;
    fn describe(&self) -> Value;
    fn predict(&mut self, gray400: &[u8], ctx: PredictContext) -> Result<ModelOutput, String>;
    fn reset_state(&mut self);
    fn is_temporal(&self) -> bool;
}

// ---------------------------------------------------------------------------
// Internal ORT backend session + provider selection + CUDA Graph IoBinding
// ---------------------------------------------------------------------------

struct CudaGraphState {
    binding: IoBinding,
    input_tensor: Tensor<f32>,
    output_tensor: Tensor<f32>,
    state_in_tensor: Option<Tensor<f32>>,
    state_out_tensor: Option<Tensor<f32>>,
}

struct OrtSessionMeta {
    checkpoint: String,
    artifact: String,
    requested_device: String,
    engine: String,
    device: String,
    provider: String,
    providers: Vec<String>,
    available_providers: Vec<String>,
    fallbacks: Vec<Value>,
    load_ms: f64,
    session: Session,
    cuda_graph: Option<CudaGraphState>,
}

fn parse_device_spec(device: &str) -> Result<(String, String), String> {
    let (engine, dev) = if let Some((prefix, rest)) = device.split_once(':') {
        if matches!(prefix, "torch" | "torchscript" | "onnx" | "onnxgraph") {
            (prefix.to_string(), rest.to_string())
        } else {
            (
                if device == "cuda" {
                    "onnxgraph".to_string()
                } else {
                    "onnx".to_string()
                },
                device.to_string(),
            )
        }
    } else {
        (
            if device == "cuda" {
                "onnxgraph".to_string()
            } else {
                "onnx".to_string()
            },
            device.to_string(),
        )
    };

    if matches!(engine.as_str(), "torch" | "torchscript") {
        return Err("ONNX Runtime backend required; torch/torchscript is not supported".to_string());
    }
    if dev != "auto" && provider_name(&dev).is_none() {
        return Err("device must be auto, cuda, dml, openvino, cpu, or engine:device".to_string());
    }
    if engine == "onnxgraph" && dev != "cuda" {
        return Err("onnxgraph requires CUDA".to_string());
    }
    Ok((engine, dev))
}

fn canonical_path_string(path: &Path) -> String {
    match fs::canonicalize(path) {
        Ok(canon) => {
            let s = canon.to_string_lossy();
            #[cfg(windows)]
            if let Some(stripped) = s.strip_prefix(r"\\?\") {
                return stripped.to_string();
            }
            s.into_owned()
        }
        Err(_) => path.to_string_lossy().into_owned(),
    }
}

#[derive(Debug, Clone)]
struct IoSpec {
    input_name: String,
    input_shape: [usize; 4],
    output_name: String,
    output_shape: Vec<usize>,
    state_input_name: Option<String>,
    state_output_name: Option<String>,
    state_shape: Option<[usize; 2]>,
    allow_auto_detect_state: bool,
}

fn auto_detect_state(session: &Session, spec: &mut IoSpec) {
    if !spec.allow_auto_detect_state || spec.state_input_name.is_some() {
        return;
    }
    let inputs = session.inputs();
    let outputs = session.outputs();
    if inputs.len() == 2 && outputs.len() == 2 {
        let s_in = &inputs[1];
        let s_out = &outputs[1];
        if s_in.name() == "h_in" && s_out.name() == "h_out" {
            if let (
                ValueType::Tensor {
                    ty: TensorElementType::Float32,
                    shape: in_shape,
                    ..
                },
                ValueType::Tensor {
                    ty: TensorElementType::Float32,
                    shape: out_shape,
                    ..
                },
            ) = (s_in.dtype(), s_out.dtype())
            {
                if in_shape.len() == 2
                    && in_shape[0] == 1
                    && in_shape[1] > 0
                    && in_shape == out_shape
                {
                    spec.state_input_name = Some("h_in".to_string());
                    spec.state_output_name = Some("h_out".to_string());
                    spec.state_shape = Some([1, in_shape[1] as usize]);
                }
            }
        }
    }
}

fn build_session_for_provider(
    env: &Environment,
    artifact: &Path,
    engine: &str,
    target_device: &str,
    available_providers: &[String],
    spec: &mut IoSpec,
) -> Result<(Session, Option<CudaGraphState>, String, Vec<String>), String> {
    let provider = provider_name(target_device)
        .ok_or_else(|| format!("Unsupported target device: {target_device}"))?;
    if !available_providers.iter().any(|p| p == provider) {
        return Err(format!("{provider} is not installed in this runtime"));
    }

    let mut builder = Session::builder(env)
        .map_err(|e| e.to_string())?
        .with_intra_threads(1)
        .map_err(|e| e.to_string())?
        .with_inter_threads(1)
        .map_err(|e| e.to_string())?;

    let mut ep_list: Vec<ExecutionProviderDispatch> = Vec::new();
    let mut active_providers = Vec::new();

    match target_device {
        "cuda" => {
            let mut cuda = ep::CUDA::default()
                .with_conv_algorithm_search(ep::cuda::ConvAlgorithmSearch::Heuristic)
                .with_tf32(false);
            if engine == "onnxgraph" {
                cuda = cuda.with_cuda_graph(true);
            }
            ep_list.push(cuda.build().error_on_failure());
            ep_list.push(ep::CPU::default().with_arena_allocator(true).build());
            active_providers.push(provider.to_string());
            active_providers.push("CPUExecutionProvider".to_string());
        }
        "dml" => {
            builder = builder
                .with_memory_pattern(false)
                .map_err(|e| e.to_string())?
                .with_parallel_execution(false)
                .map_err(|e| e.to_string())?;
            ep_list.push(ep::DirectML::default().with_device_id(0).build().error_on_failure());
            ep_list.push(ep::CPU::default().with_arena_allocator(true).build());
            active_providers.push(provider.to_string());
            active_providers.push("CPUExecutionProvider".to_string());
        }
        "openvino" => {
            builder = builder
                .with_optimization_level(GraphOptimizationLevel::Disable)
                .map_err(|e| e.to_string())?;
            let ov = ep::OpenVINO::default()
                .with_device_type("GPU")
                .with_arbitrary_config(
                    "load_config",
                    r#"{"GPU":{"INFERENCE_PRECISION_HINT":"f32","PERFORMANCE_HINT":"LATENCY"}}"#,
                );
            ep_list.push(ov.build().error_on_failure());
            ep_list.push(ep::CPU::default().with_arena_allocator(true).build());
            active_providers.push(provider.to_string());
            active_providers.push("CPUExecutionProvider".to_string());
        }
        "cpu" => {
            ep_list.push(ep::CPU::default().with_arena_allocator(true).build().error_on_failure());
            active_providers.push("CPUExecutionProvider".to_string());
        }
        _ => unreachable!(),
    }

    let mut session = builder
        .with_execution_providers(ep_list)
        .map_err(|e| format!("{provider} did not initialize: {e}"))?
        .commit_from_file(artifact)
        .map_err(|e| format!("Failed to create ORT session for {provider}: {e}"))?;

    auto_detect_state(&session, spec);

    // Warmup & verify execution before accepting this provider.
    let input_elems: usize = spec.input_shape.iter().product();
    let sample = vec![0.0f32; input_elems];
    let state_sample = spec.state_shape.map(|[r, c]| vec![0.0f32; r * c]);

    let cuda_graph = if engine == "onnxgraph" {
        let in_alloc = Allocator::new(
            &session,
            MemoryInfo::new(
                AllocationDevice::CUDA_PINNED,
                0,
                AllocatorType::Device,
                MemoryType::CPUInput,
            )
            .map_err(|e| e.to_string())?,
        )
        .map_err(|e| e.to_string())?;
        let out_alloc = Allocator::new(
            &session,
            MemoryInfo::new(
                AllocationDevice::CUDA_PINNED,
                0,
                AllocatorType::Device,
                MemoryType::CPUOutput,
            )
            .map_err(|e| e.to_string())?,
        )
        .map_err(|e| e.to_string())?;

        let mut input_tensor =
            Tensor::<f32>::new(&in_alloc, spec.input_shape).map_err(|e| e.to_string())?;
        input_tensor.extract_tensor_mut().1.fill(0.0);
        let mut output_tensor =
            Tensor::<f32>::new(&out_alloc, spec.output_shape.as_slice()).map_err(|e| e.to_string())?;

        let mut binding = session.create_binding().map_err(|e| e.to_string())?;
        binding
            .bind_input(&spec.input_name, &input_tensor)
            .map_err(|e| e.to_string())?;
        binding
            .bind_output(&spec.output_name, output_tensor.view_mut())
            .map_err(|e| e.to_string())?;

        let (state_in_tensor, state_out_tensor) = if let (Some(s_in), Some(s_out), Some(s_shape)) = (
            &spec.state_input_name,
            &spec.state_output_name,
            spec.state_shape,
        ) {
            let mut st_in = Tensor::<f32>::new(&in_alloc, s_shape).map_err(|e| e.to_string())?;
            st_in.extract_tensor_mut().1.fill(0.0);
            let mut st_out = Tensor::<f32>::new(&out_alloc, s_shape).map_err(|e| e.to_string())?;
            binding
                .bind_input(s_in, &st_in)
                .map_err(|e| e.to_string())?;
            binding
                .bind_output(s_out, st_out.view_mut())
                .map_err(|e| e.to_string())?;
            (Some(st_in), Some(st_out))
        } else {
            (None, None)
        };

        for _ in 0..3 {
            binding.synchronize_inputs().map_err(|e| e.to_string())?;
            let _ = session.run_binding(&binding).map_err(|e| e.to_string())?;
            binding.synchronize_outputs().map_err(|e| e.to_string())?;
        }

        Some(CudaGraphState {
            binding,
            input_tensor,
            output_tensor,
            state_in_tensor,
            state_out_tensor,
        })
    } else {
        let input_ref =
            TensorRef::from_array_view((spec.input_shape, sample.as_slice())).map_err(|e| e.to_string())?;
        if let (Some(s_in), Some(s_shape), Some(s_buf)) = (
            &spec.state_input_name,
            spec.state_shape,
            state_sample.as_deref(),
        ) {
            let state_ref =
                TensorRef::from_array_view((s_shape, s_buf)).map_err(|e| e.to_string())?;
            let _ = session
                .run(ort::inputs![
                    spec.input_name.as_str() => input_ref,
                    s_in.as_str() => state_ref,
                ])
                .map_err(|e| format!("Warmup inference failed on {provider}: {e}"))?;
        } else {
            let _ = session
                .run(ort::inputs![spec.input_name.as_str() => input_ref])
                .map_err(|e| format!("Warmup inference failed on {provider}: {e}"))?;
        }
        None
    };

    Ok((session, cuda_graph, provider.to_string(), active_providers))
}

fn load_ort_session(
    checkpoint: &Path,
    artifact: &Path,
    requested_device: &str,
    spec: &mut IoSpec,
) -> Result<OrtSessionMeta, String> {
    let start = Instant::now();
    let (engine, dev) = parse_device_spec(requested_device)?;
    if !artifact.is_file() {
        return Err(format!("ONNX model does not exist: {}", artifact.display()));
    }

    let env = ensure_ort_environment()?;
    let available_providers = available_ort_providers()?;

    let choices: Vec<(&str, &str)> = if requested_device == "auto" {
        [
            ("onnxgraph", "cuda"),
            ("onnx", "cuda"),
            ("onnx", "openvino"),
            ("onnx", "dml"),
            ("onnx", "cpu"),
        ]
        .into_iter()
        .filter(|&(_, target)| {
            provider_name(target)
                .is_some_and(|p| available_providers.iter().any(|ap| ap == p))
        })
        .collect()
    } else {
        vec![(engine.as_str(), dev.as_str())]
    };

    if choices.is_empty() {
        return Err("No supported ONNX Runtime execution provider is available".to_string());
    }

    let mut fallbacks = Vec::new();
    for (eng, target) in choices {
        let mut trial_spec = spec.clone();
        match build_session_for_provider(
            &env,
            artifact,
            eng,
            target,
            &available_providers,
            &mut trial_spec,
        ) {
            Ok((session, cuda_graph, provider, providers)) => {
                *spec = trial_spec;
                return Ok(OrtSessionMeta {
                    checkpoint: canonical_path_string(checkpoint),
                    artifact: canonical_path_string(artifact),
                    requested_device: requested_device.to_string(),
                    engine: eng.to_string(),
                    device: target.to_string(),
                    provider,
                    providers,
                    available_providers,
                    fallbacks,
                    load_ms: start.elapsed().as_secs_f64() * 1000.0,
                    session,
                    cuda_graph,
                });
            }
            Err(err) => {
                if requested_device != "auto" {
                    return Err(err);
                }
                fallbacks.push(json!({
                    "engine": eng,
                    "device": target,
                    "error": err,
                }));
            }
        }
    }

    Err(format!(
        "All ONNX Runtime backends failed: {}",
        serde_json::to_string(&fallbacks).unwrap_or_default()
    ))
}

fn check_outlet_f32_tail(outlet: &ort::value::Outlet, expected_name: &str, tail: &[i64]) -> bool {
    if outlet.name() != expected_name {
        return false;
    }
    match outlet.dtype() {
        ValueType::Tensor {
            ty: TensorElementType::Float32,
            shape,
            ..
        } => shape.len() == tail.len() + 1 && shape[1..] == *tail,
        _ => false,
    }
}

fn check_outlet_f32_exact(outlet: &ort::value::Outlet, expected_name: &str, expected: &[i64]) -> bool {
    if outlet.name() != expected_name {
        return false;
    }
    match outlet.dtype() {
        ValueType::Tensor {
            ty: TensorElementType::Float32,
            shape,
            ..
        } => shape.as_ref() == expected,
        _ => false,
    }
}

fn parse_strict_usize_dim(val: Option<&Value>, err_msg: &str) -> Result<usize, String> {
    let Some(v) = val else {
        return Err(err_msg.to_string());
    };
    if v.is_boolean() {
        return Err(err_msg.to_string());
    }
    let Some(n) = v.as_i64() else {
        return Err(err_msg.to_string());
    };
    if !(1..=400).contains(&n) {
        return Err(err_msg.to_string());
    }
    Ok(n as usize)
}

fn parse_string_list(val: &Value) -> Option<Vec<String>> {
    let arr = val.as_array()?;
    let mut out = Vec::with_capacity(arr.len());
    for item in arr {
        out.push(item.as_str()?.to_string());
    }
    Some(out)
}

// ---------------------------------------------------------------------------
// FaceModel (pico-mouth50-v1, stateless or stateful GRU)
// ---------------------------------------------------------------------------

#[derive(Debug)]
struct FaceManifest {
    channels: Vec<String>,
    channel_to_schema_idx: Vec<usize>,
    trained_channels: Vec<String>,
    validated_channels: Vec<String>,
    output_channels: Vec<String>,
    valid_mask: u64,
    width: usize,
    height: usize,
    temporal_kind: String,
    state_input_name: Option<String>,
    state_output_name: Option<String>,
    state_shape: Option<[usize; 2]>,
}

fn parse_face_manifest(meta: &Value) -> Result<FaceManifest, String> {
    let Some(obj) = meta.as_object() else {
        return Err("Whole-mouth model manifest must be a JSON object".to_string());
    };
    if obj.get("schema").and_then(|v| v.as_str()) != Some(SCHEMA) {
        return Err(format!("Whole-mouth model manifest schema must be {SCHEMA}"));
    }

    let channels = obj
        .get("channels")
        .and_then(parse_string_list)
        .ok_or_else(|| "Whole-mouth manifest requires all 50 unique named channels".to_string())?;
    let known_set: HashSet<&str> = FACE_CHANNELS.iter().copied().collect();
    let ch_set: HashSet<&str> = channels.iter().map(|s| s.as_str()).collect();
    if channels.len() != 50 || ch_set.len() != 50 || ch_set != known_set {
        return Err("Whole-mouth manifest requires all 50 unique named channels".to_string());
    }

    let trained_raw = obj
        .get("trained_channels")
        .and_then(parse_string_list)
        .ok_or_else(|| {
            "Whole-mouth manifest requires an explicit trained_channels list of known channels"
                .to_string()
        })?;
    let trained_set: HashSet<&str> = trained_raw.iter().map(|s| s.as_str()).collect();
    if trained_set.len() != trained_raw.len() || !trained_set.is_subset(&known_set) {
        return Err(
            "Whole-mouth manifest requires an explicit trained_channels list of known channels"
                .to_string(),
        );
    }
    let trained_channels: Vec<String> = FACE_CHANNELS
        .iter()
        .filter(|&&name| trained_set.contains(name))
        .map(|&s| s.to_string())
        .collect();

    let validated_raw = match obj.get("validated_channels") {
        None => Vec::new(),
        Some(v) => parse_string_list(v).ok_or_else(|| {
            "validated_channels must be a unique subset of trained_channels".to_string()
        })?,
    };
    let validated_set: HashSet<&str> = validated_raw.iter().map(|s| s.as_str()).collect();
    if validated_set.len() != validated_raw.len() || !validated_set.is_subset(&trained_set) {
        return Err("validated_channels must be a unique subset of trained_channels".to_string());
    }

    let mut validated_mask: u64 = 0;
    for name in &validated_raw {
        if let Some(idx) = channel_index(name) {
            validated_mask |= 1u64 << idx;
        }
    }

    let valid_mask: u64 = match obj.get("valid_mask") {
        None => 0,
        Some(v) => {
            if v.is_boolean() {
                return Err(
                    "valid_mask may enable only independently validated_channels".to_string(),
                );
            }
            let Some(u) = v.as_u64() else {
                return Err(
                    "valid_mask may enable only independently validated_channels".to_string(),
                );
            };
            if (u & !validated_mask) != 0 {
                return Err(
                    "valid_mask may enable only independently validated_channels".to_string(),
                );
            }
            u
        }
    };

    let validated_channels: Vec<String> = FACE_CHANNELS
        .iter()
        .filter(|&&name| validated_set.contains(name))
        .map(|&s| s.to_string())
        .collect();
    let output_channels: Vec<String> = FACE_CHANNELS
        .iter()
        .enumerate()
        .filter(|&(i, _)| (valid_mask & (1u64 << i)) != 0)
        .map(|(_, &s)| s.to_string())
        .collect();

    let width = parse_strict_usize_dim(
        obj.get("width"),
        "Whole-mouth input dimensions must be between 1 and 400",
    )?;
    let height = parse_strict_usize_dim(
        obj.get("height"),
        "Whole-mouth input dimensions must be between 1 and 400",
    )?;

    let (temporal_kind, state_input_name, state_output_name, state_shape) =
        parse_temporal_meta(obj)?;

    let channel_to_schema_idx: Vec<usize> = channels
        .iter()
        .map(|name| channel_index(name).expect("validated known channel"))
        .collect();

    Ok(FaceManifest {
        channels,
        channel_to_schema_idx,
        trained_channels,
        validated_channels,
        output_channels,
        valid_mask,
        width,
        height,
        temporal_kind,
        state_input_name,
        state_output_name,
        state_shape,
    })
}

fn parse_temporal_meta(
    obj: &Map<String, Value>,
) -> Result<(String, Option<String>, Option<String>, Option<[usize; 2]>), String> {
    let err = || {
        "Whole-mouth temporal manifest requires valid state_input, state_output, and state_shape [1, H]"
            .to_string()
    };

    let (kind, s_in, s_out, s_shape) = if let Some(temporal) =
        obj.get("temporal").and_then(|v| v.as_object())
    {
        let kind = temporal
            .get("kind")
            .and_then(|v| v.as_str())
            .unwrap_or("gru_stateful")
            .to_string();
        let s_in = temporal
            .get("state_input")
            .map(|v| v.as_str().map(str::to_string))
            .unwrap_or_else(|| Some("h_in".to_string()));
        let s_out = temporal
            .get("state_output")
            .map(|v| v.as_str().map(str::to_string))
            .unwrap_or_else(|| Some("h_out".to_string()));
        let s_shape = temporal.get("state_shape");
        (kind, s_in, s_out, s_shape)
    } else if obj
        .get("state_input_name")
        .is_some_and(|v| !v.is_null() && v.as_str().is_none_or(|s| !s.is_empty()))
        || obj
            .get("temporal_kind")
            .is_some_and(|v| !v.is_null() && v.as_str() != Some("stateless"))
    {
        let kind = obj
            .get("temporal_kind")
            .and_then(|v| v.as_str())
            .unwrap_or("gru_stateful")
            .to_string();
        let s_in = obj
            .get("state_input_name")
            .map(|v| v.as_str().map(str::to_string))
            .unwrap_or_else(|| Some("h_in".to_string()));
        let s_out = obj
            .get("state_output_name")
            .map(|v| v.as_str().map(str::to_string))
            .unwrap_or_else(|| Some("h_out".to_string()));
        let s_shape = obj.get("state_shape");
        (kind, s_in, s_out, s_shape)
    } else {
        return Ok(("stateless".to_string(), None, None, None));
    };

    if kind == "stateless" {
        return Ok(("stateless".to_string(), None, None, None));
    }

    let s_in = s_in.filter(|s| !s.is_empty()).ok_or_else(err)?;
    let s_out = s_out.filter(|s| !s.is_empty()).ok_or_else(err)?;
    let shape_arr = s_shape.and_then(|v| v.as_array()).ok_or_else(err)?;
    if shape_arr.len() != 2 {
        return Err(err());
    }
    let d0 = shape_arr[0].as_i64().filter(|_| !shape_arr[0].is_boolean()).ok_or_else(err)?;
    let d1 = shape_arr[1].as_i64().filter(|_| !shape_arr[1].is_boolean()).ok_or_else(err)?;
    if d0 != 1 || d1 <= 0 {
        return Err(err());
    }
    Ok((kind, Some(s_in), Some(s_out), Some([1, d1 as usize])))
}

pub struct FaceModel {
    meta: OrtSessionMeta,
    #[allow(dead_code)]
    channels: Vec<String>,
    channel_to_schema_idx: Vec<usize>,
    trained_channels: Vec<String>,
    validated_channels: Vec<String>,
    output_channels: Vec<String>,
    valid_mask: u64,
    width: usize,
    height: usize,
    input_shape: [usize; 4],
    temporal_kind: String,
    state_input_name: Option<String>,
    state_output_name: Option<String>,
    state_shape: Option<[usize; 2]>,
    state: Option<Vec<f32>>,
    state_session_id: Option<u64>,
    state_source_ns: Option<u64>,
}

impl FaceModel {
    pub const INPUT_NAME: &'static str = "frame";
    pub const OUTPUT_NAME: &'static str = "blendshapes";

    pub fn load(checkpoint: impl AsRef<Path>, device: &str) -> Result<Self, String> {
        let path = checkpoint.as_ref();
        let ext = path
            .extension()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        if ext != "onnx" {
            return Err(
                "Whole-mouth inference requires an ONNX model and adjacent JSON manifest"
                    .to_string(),
            );
        }
        let manifest_path = path.with_extension("json");
        let manifest_text = fs::read_to_string(&manifest_path).map_err(|e| {
            format!(
                "Failed to read whole-mouth manifest {}: {e}",
                manifest_path.display()
            )
        })?;
        let manifest_json: Value = serde_json::from_str(&manifest_text)
            .map_err(|e| format!("Invalid JSON in {}: {e}", manifest_path.display()))?;
        let manifest = parse_face_manifest(&manifest_json)?;

        if device.starts_with("torch:") || device.starts_with("torchscript:") {
            return Err("Whole-mouth models support ONNX Runtime backends only".to_string());
        }

        let input_shape = [1, 1, manifest.height, manifest.width];
        let mut io_spec = IoSpec {
            input_name: Self::INPUT_NAME.to_string(),
            input_shape,
            output_name: Self::OUTPUT_NAME.to_string(),
            output_shape: vec![1, 50],
            state_input_name: manifest.state_input_name.clone(),
            state_output_name: manifest.state_output_name.clone(),
            state_shape: manifest.state_shape,
            allow_auto_detect_state: true,
        };

        let ort_meta = load_ort_session(path, path, device, &mut io_spec)?;

        let state_input_name = io_spec.state_input_name;
        let state_output_name = io_spec.state_output_name;
        let state_shape = io_spec.state_shape;
        let temporal_kind = if state_input_name.is_some() && manifest.temporal_kind == "stateless" {
            "gru_stateful".to_string()
        } else {
            manifest.temporal_kind
        };

        // Validate session I/O contract strictly against manifest
        let inputs = ort_meta.session.inputs();
        let outputs = ort_meta.session.outputs();
        let frame_ok = !inputs.is_empty()
            && check_outlet_f32_tail(
                &inputs[0],
                Self::INPUT_NAME,
                &[1, manifest.height as i64, manifest.width as i64],
            )
            && !outputs.is_empty()
            && check_outlet_f32_tail(&outputs[0], Self::OUTPUT_NAME, &[50]);

        let state_ok = if let (Some(ref s_in), Some(ref s_out), Some([r, c])) =
            (&state_input_name, &state_output_name, state_shape)
        {
            inputs.len() == 2
                && outputs.len() == 2
                && check_outlet_f32_exact(&inputs[1], s_in, &[r as i64, c as i64])
                && check_outlet_f32_exact(&outputs[1], s_out, &[r as i64, c as i64])
        } else {
            inputs.len() == 1 && outputs.len() == 1
        };

        if !(frame_ok && state_ok) {
            return Err(
                "Whole-mouth ONNX frame/blendshapes contract does not match its manifest"
                    .to_string(),
            );
        }

        let mut model = Self {
            meta: ort_meta,
            channels: manifest.channels,
            channel_to_schema_idx: manifest.channel_to_schema_idx,
            trained_channels: manifest.trained_channels,
            validated_channels: manifest.validated_channels,
            output_channels: manifest.output_channels,
            valid_mask: manifest.valid_mask,
            width: manifest.width,
            height: manifest.height,
            input_shape,
            temporal_kind,
            state_input_name,
            state_output_name,
            state_shape,
            state: None,
            state_session_id: None,
            state_source_ns: None,
        };
        model.reset_state();
        let zeros = vec![0u8; 400 * 400];
        let _ = model.predict_face(&zeros, PredictContext::default())?;
        model.reset_state();
        Ok(model)
    }

    pub fn provider(&self) -> &str {
        &self.meta.provider
    }

    pub fn hidden_state(&self) -> Option<&[f32]> {
        self.state.as_deref()
    }

    pub fn is_temporal(&self) -> bool {
        self.state_input_name.is_some()
    }

    pub fn reset_state(&mut self) {
        self.state = self.state_shape.map(|[r, c]| vec![0.0f32; r * c]);
        self.state_session_id = None;
        self.state_source_ns = None;
    }

    pub fn describe(&self) -> Value {
        let unsupported: Vec<&str> = FACE_CHANNELS
            .iter()
            .copied()
            .filter(|name| !self.output_channels.iter().any(|c| c == name))
            .collect();
        let mut info = json!({
            "engine": self.meta.engine,
            "device": self.meta.device,
            "checkpoint": self.meta.checkpoint,
            "artifact": self.meta.artifact,
            "requested_device": self.meta.requested_device,
            "provider": self.meta.provider,
            "providers": self.meta.providers,
            "available_providers": self.meta.available_providers,
            "fallbacks": self.meta.fallbacks,
            "model_kind": "face50",
            "schema": SCHEMA,
            "channels": FACE_CHANNELS.as_slice(),
            "trained_channels": self.trained_channels,
            "validated_channels": self.validated_channels,
            "output_channels": self.output_channels,
            "valid_mask": self.valid_mask.to_string(),
            "unsupported_channels": unsupported,
            "image_shape": [400, 400],
            "input_shape": self.input_shape,
            "preprocessing": "Pillow BILINEAR GRAY8 /255; no crop or horizontal flip",
            "temporal_smoothing": self.is_temporal(),
            "temporal_kind": self.temporal_kind,
            "runtime_filter": "One-Euro; smoothing_ms=0 bypasses",
            "geometry": false,
            "cpu_threads": 1,
            "load_ms": self.meta.load_ms,
        });
        if let (true, Some(shape)) = (self.is_temporal(), self.state_shape) {
            info["state_shape"] = json!([shape[0], shape[1]]);
        }
        info
    }

    fn should_reset_temporal_state(
        has_state: bool,
        state_session_id: Option<u64>,
        state_source_ns: Option<u64>,
        ctx: PredictContext,
    ) -> bool {
        if !has_state {
            return true;
        }
        if let (Some(sid), Some(prev_sid)) = (ctx.session_id, state_session_id) {
            if sid != prev_sid {
                return true;
            }
        }
        if let (Some(src_ns), Some(prev_ns)) = (ctx.source_ns, state_source_ns) {
            let dt_ms = (src_ns as i128 - prev_ns as i128) as f64 / 1e6;
            if dt_ms <= 0.0 || dt_ms > ctx.stale_ms {
                return true;
            }
        }
        false
    }

    pub fn predict_face(
        &mut self,
        gray400: &[u8],
        ctx: PredictContext,
    ) -> Result<FacePrediction, String> {
        let start = Instant::now();
        let x = prepare_input(gray400, self.width, self.height)?;
        let pre = Instant::now();

        let raw_output: Vec<f32>;

        if self.is_temporal() {
            if Self::should_reset_temporal_state(
                self.state.is_some(),
                self.state_session_id,
                self.state_source_ns,
                ctx,
            ) {
                self.reset_state();
            }

            let state_shape = self.state_shape.unwrap();
            let state_len = state_shape[0] * state_shape[1];
            let state_buf = self
                .state
                .clone()
                .unwrap_or_else(|| vec![0.0f32; state_len]);
            let s_in_name = self.state_input_name.as_deref().unwrap();
            let s_out_name = self.state_output_name.as_deref().unwrap();

            let (out_vec, next_state_vec) = if let Some(ref mut cg) = self.meta.cuda_graph {
                let run_res: Result<(Vec<f32>, Vec<f32>), String> = (|| {
                    cg.input_tensor.extract_tensor_mut().1.copy_from_slice(&x);
                    if let Some(ref mut st_in) = cg.state_in_tensor {
                        st_in.extract_tensor_mut().1.copy_from_slice(&state_buf);
                    }
                    cg.binding.synchronize_inputs().map_err(|e| e.to_string())?;
                    let _ = self
                        .meta
                        .session
                        .run_binding(&cg.binding)
                        .map_err(|e| e.to_string())?;
                    cg.binding.synchronize_outputs().map_err(|e| e.to_string())?;
                    let out_slice = cg.output_tensor.extract_tensor().1.to_vec();
                    let st_slice = cg
                        .state_out_tensor
                        .as_ref()
                        .ok_or_else(|| "Missing CUDA graph state output tensor".to_string())?
                        .extract_tensor()
                        .1
                        .to_vec();
                    Ok((out_slice, st_slice))
                })();
                match run_res {
                    Ok(pair) => pair,
                    Err(err) => {
                        self.reset_state();
                        return Err(err);
                    }
                }
            } else {
                let x_ref = TensorRef::from_array_view((self.input_shape, x.as_slice()))
                    .map_err(|e| e.to_string())?;
                let s_ref = TensorRef::from_array_view((state_shape, state_buf.as_slice()))
                    .map_err(|e| e.to_string())?;
                let run_res: Result<(Vec<f32>, Vec<f32>), String> = (|| {
                    let outputs = self
                        .meta
                        .session
                        .run(ort::inputs![
                            Self::INPUT_NAME => x_ref,
                            s_in_name => s_ref,
                        ])
                        .map_err(|e| e.to_string())?;
                    let (out_shape, out_data) = outputs[Self::OUTPUT_NAME]
                        .try_extract_tensor::<f32>()
                        .map_err(|e| e.to_string())?;
                    if out_shape.as_ref() != [1i64, 50] {
                        return Err("Whole-mouth model returned invalid blendshapes".to_string());
                    }
                    let (st_shape, st_data) = outputs[s_out_name]
                        .try_extract_tensor::<f32>()
                        .map_err(|e| e.to_string())?;
                    if st_shape.as_ref() != [state_shape[0] as i64, state_shape[1] as i64] {
                        return Err(
                            "Whole-mouth model returned invalid temporal state".to_string()
                        );
                    }
                    Ok((out_data.to_vec(), st_data.to_vec()))
                })();
                match run_res {
                    Ok(pair) => pair,
                    Err(err) => {
                        self.reset_state();
                        return Err(err);
                    }
                }
            };

            if out_vec.len() != 50 || !out_vec.iter().all(|v| v.is_finite()) {
                self.reset_state();
                return Err("Whole-mouth model returned invalid blendshapes".to_string());
            }
            if next_state_vec.len() != state_len || !next_state_vec.iter().all(|v| v.is_finite()) {
                self.reset_state();
                return Err("Whole-mouth model returned invalid temporal state".to_string());
            }
            self.state = Some(next_state_vec);
            if let Some(sid) = ctx.session_id {
                self.state_session_id = Some(sid);
            }
            if let Some(ns) = ctx.source_ns {
                self.state_source_ns = Some(ns);
            }
            raw_output = out_vec;
        } else if let Some(ref mut cg) = self.meta.cuda_graph {
            cg.input_tensor.extract_tensor_mut().1.copy_from_slice(&x);
            cg.binding.synchronize_inputs().map_err(|e| e.to_string())?;
            let _ = self
                .meta
                .session
                .run_binding(&cg.binding)
                .map_err(|e| e.to_string())?;
            cg.binding.synchronize_outputs().map_err(|e| e.to_string())?;
            let out_slice = cg.output_tensor.extract_tensor().1;
            if out_slice.len() != 50 || !out_slice.iter().all(|v| v.is_finite()) {
                return Err("Whole-mouth model returned invalid blendshapes".to_string());
            }
            raw_output = out_slice.to_vec();
        } else {
            let x_ref = TensorRef::from_array_view((self.input_shape, x.as_slice()))
                .map_err(|e| e.to_string())?;
            let outputs = self
                .meta
                .session
                .run(ort::inputs![Self::INPUT_NAME => x_ref])
                .map_err(|e| e.to_string())?;
            let (out_shape, out_data) = outputs[Self::OUTPUT_NAME]
                .try_extract_tensor::<f32>()
                .map_err(|e| e.to_string())?;
            if out_shape.as_ref() != [1i64, 50] || !out_data.iter().all(|v| v.is_finite()) {
                return Err("Whole-mouth model returned invalid blendshapes".to_string());
            }
            raw_output = out_data.to_vec();
        }

        let mut values = [0.0f32; 50];
        for (model_idx, &val) in raw_output.iter().enumerate() {
            let schema_idx = self.channel_to_schema_idx[model_idx];
            if (self.valid_mask & (1u64 << schema_idx)) != 0 {
                values[schema_idx] = val.clamp(0.0, 1.0);
            }
        }
        let mut channels = BTreeMap::new();
        for (i, &name) in FACE_CHANNELS.iter().enumerate() {
            if (self.valid_mask & (1u64 << i)) != 0 {
                channels.insert(name.to_string(), values[i]);
            }
        }

        let end = Instant::now();
        Ok(FacePrediction {
            values,
            channels,
            valid_mask: self.valid_mask,
            schema: SCHEMA,
            timing_ms: TimingMs {
                preprocess: pre.duration_since(start).as_secs_f64() * 1000.0,
                inference: end.duration_since(pre).as_secs_f64() * 1000.0,
                postprocess: 0.0,
                total: end.duration_since(start).as_secs_f64() * 1000.0,
            },
        })
    }
}

impl MouthModel for FaceModel {
    fn model_kind(&self) -> &'static str {
        "face50"
    }

    fn describe(&self) -> Value {
        FaceModel::describe(self)
    }

    fn predict(&mut self, gray400: &[u8], ctx: PredictContext) -> Result<ModelOutput, String> {
        Ok(ModelOutput::Face50(self.predict_face(gray400, ctx)?))
    }

    fn reset_state(&mut self) {
        FaceModel::reset_state(self)
    }

    fn is_temporal(&self) -> bool {
        FaceModel::is_temporal(self)
    }
}

// ---------------------------------------------------------------------------
// MouthLinkModel
// ---------------------------------------------------------------------------

pub struct MouthLinkModel {
    meta: OrtSessionMeta,
    channels: Vec<String>,
    width: usize,
    height: usize,
    input_shape: [usize; 4],
}

impl MouthLinkModel {
    pub const INPUT_NAME: &'static str = "frame";
    pub const OUTPUT_NAME: &'static str = "blendshapes";

    pub fn load(checkpoint: impl AsRef<Path>, device: &str) -> Result<Self, String> {
        let path = checkpoint.as_ref();
        let ext = path
            .extension()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        if ext != "onnx" {
            return Err("MouthLink requires an ONNX model with adjacent JSON metadata".to_string());
        }
        let meta_path = path.with_extension("json");
        let meta_text = fs::read_to_string(&meta_path)
            .map_err(|e| format!("Failed to read MouthLink metadata {}: {e}", meta_path.display()))?;
        let meta_json: Value = serde_json::from_str(&meta_text)
            .map_err(|e| format!("Invalid JSON in {}: {e}", meta_path.display()))?;
        let obj = meta_json
            .as_object()
            .ok_or_else(|| "MouthLink metadata must be a JSON object".to_string())?;

        let channels = obj
            .get("channels")
            .and_then(parse_string_list)
            .ok_or_else(|| {
                "MouthLink metadata requires unique TongueOut/Left/Right/Up/Down channels"
                    .to_string()
            })?;
        let ch_set: HashSet<&str> = channels.iter().map(|s| s.as_str()).collect();
        let required = [
            "TongueOut",
            "TongueLeft",
            "TongueRight",
            "TongueUp",
            "TongueDown",
        ];
        if ch_set.len() != channels.len() || !required.iter().all(|r| ch_set.contains(r)) {
            return Err(
                "MouthLink metadata requires unique TongueOut/Left/Right/Up/Down channels"
                    .to_string(),
            );
        }

        let width = parse_strict_usize_dim(
            obj.get("width"),
            "MouthLink input dimensions must be between 1 and 400",
        )?;
        let height = parse_strict_usize_dim(
            obj.get("height"),
            "MouthLink input dimensions must be between 1 and 400",
        )?;

        if device.starts_with("torch:") || device.starts_with("torchscript:") {
            return Err("MouthLink supports ONNX Runtime backends only".to_string());
        }

        let input_shape = [1, 1, height, width];
        let mut io_spec = IoSpec {
            input_name: Self::INPUT_NAME.to_string(),
            input_shape,
            output_name: Self::OUTPUT_NAME.to_string(),
            output_shape: vec![1, channels.len()],
            state_input_name: None,
            state_output_name: None,
            state_shape: None,
            allow_auto_detect_state: false,
        };

        let ort_meta = load_ort_session(path, path, device, &mut io_spec)?;

        let inputs = ort_meta.session.inputs();
        let outputs = ort_meta.session.outputs();
        if inputs.len() != 1
            || !check_outlet_f32_tail(&inputs[0], Self::INPUT_NAME, &[1, height as i64, width as i64])
            || outputs.len() != 1
            || !check_outlet_f32_tail(&outputs[0], Self::OUTPUT_NAME, &[channels.len() as i64])
        {
            return Err("MouthLink ONNX input/output does not match its metadata".to_string());
        }

        let mut model = Self {
            meta: ort_meta,
            channels,
            width,
            height,
            input_shape,
        };
        let zeros = vec![0u8; 400 * 400];
        let _ = model.predict_mouthlink(&zeros)?;
        Ok(model)
    }

    pub fn provider(&self) -> &str {
        &self.meta.provider
    }

    pub fn describe(&self) -> Value {
        let keys: Vec<&str> = MOUTHLINK_CHANNELS.iter().map(|&(k, _)| k).collect();
        json!({
            "engine": self.meta.engine,
            "device": self.meta.device,
            "checkpoint": self.meta.checkpoint,
            "artifact": self.meta.artifact,
            "requested_device": self.meta.requested_device,
            "provider": self.meta.provider,
            "providers": self.meta.providers,
            "available_providers": self.meta.available_providers,
            "fallbacks": self.meta.fallbacks,
            "model_kind": "mouthlink",
            "channels": keys,
            "model_channels": self.channels,
            "image_shape": [400, 400],
            "input_shape": self.input_shape,
            "preprocessing": "Pillow BILINEAR GRAY8 /255; no crop or horizontal flip",
            "direction_range": [0.15, 0.55],
            "out_hysteresis": [0.5, 0.3],
            "personalization": false,
            "temporal_smoothing": false,
            "cpu_threads": 1,
            "load_ms": self.meta.load_ms,
        })
    }

    pub fn predict_mouthlink(&mut self, gray400: &[u8]) -> Result<MouthLinkPrediction, String> {
        let start = Instant::now();
        let x = prepare_input(gray400, self.width, self.height)?;
        let pre = Instant::now();

        let expected_len = self.channels.len();
        let raw_output: Vec<f32> = if let Some(ref mut cg) = self.meta.cuda_graph {
            cg.input_tensor.extract_tensor_mut().1.copy_from_slice(&x);
            cg.binding.synchronize_inputs().map_err(|e| e.to_string())?;
            let _ = self
                .meta
                .session
                .run_binding(&cg.binding)
                .map_err(|e| e.to_string())?;
            cg.binding.synchronize_outputs().map_err(|e| e.to_string())?;
            let out_slice = cg.output_tensor.extract_tensor().1;
            if out_slice.len() != expected_len || !out_slice.iter().all(|v| v.is_finite()) {
                return Err("MouthLink returned invalid blendshapes".to_string());
            }
            out_slice.to_vec()
        } else {
            let x_ref = TensorRef::from_array_view((self.input_shape, x.as_slice()))
                .map_err(|e| e.to_string())?;
            let outputs = self
                .meta
                .session
                .run(ort::inputs![Self::INPUT_NAME => x_ref])
                .map_err(|e| e.to_string())?;
            let (out_shape, out_data) = outputs[Self::OUTPUT_NAME]
                .try_extract_tensor::<f32>()
                .map_err(|e| e.to_string())?;
            if out_shape.as_ref() != [1i64, expected_len as i64]
                || !out_data.iter().all(|v| v.is_finite())
            {
                return Err("MouthLink returned invalid blendshapes".to_string());
            }
            out_data.to_vec()
        };

        let mut named = BTreeMap::new();
        for (name, &val) in self.channels.iter().zip(raw_output.iter()) {
            named.insert(name.as_str(), (val as f64).clamp(0.0, 1.0));
        }

        let tongue_out = *named.get("TongueOut").unwrap_or(&0.0);
        let map_dir = |name: &str| -> f64 {
            let v = *named.get(name).unwrap_or(&0.0);
            ((v - 0.15) / 0.4).clamp(0.0, 1.0)
        };

        let end = Instant::now();
        Ok(MouthLinkPrediction {
            out: tongue_out,
            left: map_dir("TongueLeft"),
            right: map_dir("TongueRight"),
            up: map_dir("TongueUp"),
            down: map_dir("TongueDown"),
            roll: map_dir("TongueRoll"),
            present: tongue_out >= 0.5,
            extended: true,
            out_hysteresis: [0.5, 0.3],
            timing_ms: TimingMs {
                preprocess: pre.duration_since(start).as_secs_f64() * 1000.0,
                inference: end.duration_since(pre).as_secs_f64() * 1000.0,
                postprocess: 0.0,
                total: end.duration_since(start).as_secs_f64() * 1000.0,
            },
        })
    }
}

impl MouthModel for MouthLinkModel {
    fn model_kind(&self) -> &'static str {
        "mouthlink"
    }

    fn describe(&self) -> Value {
        MouthLinkModel::describe(self)
    }

    fn predict(&mut self, gray400: &[u8], _ctx: PredictContext) -> Result<ModelOutput, String> {
        Ok(ModelOutput::MouthLink(self.predict_mouthlink(gray400)?))
    }

    fn reset_state(&mut self) {}

    fn is_temporal(&self) -> bool {
        false
    }
}

// ---------------------------------------------------------------------------
// SegmentationModel (ONNX path of pc-studio/model.py)
// ---------------------------------------------------------------------------

pub struct SegmentationModel {
    meta: OrtSessionMeta,
}

impl SegmentationModel {
    pub const INPUT_NAME: &'static str = "image";
    pub const OUTPUT_NAME: &'static str = "logits";
    pub const INPUT_SHAPE: [usize; 4] = [1, 1, 224, 224];

    pub fn load(checkpoint: impl AsRef<Path>, device: &str) -> Result<Self, String> {
        let path = checkpoint.as_ref();
        let artifact = path.with_extension("onnx");
        let mut io_spec = IoSpec {
            input_name: Self::INPUT_NAME.to_string(),
            input_shape: Self::INPUT_SHAPE,
            output_name: Self::OUTPUT_NAME.to_string(),
            output_shape: vec![1, 1, 56, 56],
            state_input_name: None,
            state_output_name: None,
            state_shape: None,
            allow_auto_detect_state: false,
        };
        let ort_meta = load_ort_session(path, &artifact, device, &mut io_spec)?;
        Ok(Self { meta: ort_meta })
    }

    pub fn provider(&self) -> &str {
        &self.meta.provider
    }

    pub fn describe(&self) -> Value {
        json!({
            "engine": self.meta.engine,
            "device": self.meta.device,
            "checkpoint": self.meta.checkpoint,
            "artifact": self.meta.artifact,
            "requested_device": self.meta.requested_device,
            "provider": self.meta.provider,
            "providers": self.meta.providers,
            "available_providers": self.meta.available_providers,
            "fallbacks": self.meta.fallbacks,
            "model_kind": "segmentation",
            "reference_head": REFERENCE_HEAD,
            "calibration": {
                "left": 0.33,
                "center": 0.66,
                "right": 1.11,
                "deadzone": 0.06,
                "min_area": 100,
                "nose_threshold": 0.94,
                "min_nose_area": 50,
                "angle_min": 0.0,
                "angle_max": 1.7,
                "mask_threshold": 0.5,
            },
            "channels": ["out", "left", "right"],
            "unsupported_channels": ["up", "down"],
            "image_shape": [400, 400],
            "mask_shape": [56, 56],
            "coordinate_space": "centroid/nose [x,y] in original 400x400 image pixels",
            "temporal_smoothing": false,
            "cpu_threads": 1,
            "precision": "float32; CUDA TF32 disabled",
            "load_ms": self.meta.load_ms,
        })
    }

    pub fn predict_segmentation(&mut self, gray400: &[u8]) -> Result<SegmentationPrediction, String> {
        let start = Instant::now();
        let frame200 = downsample400(gray400)?;
        let x = to_segmentation_input(&frame200)?;
        let pre = Instant::now();

        let logits: Vec<f32> = if let Some(ref mut cg) = self.meta.cuda_graph {
            cg.input_tensor.extract_tensor_mut().1.copy_from_slice(&x);
            cg.binding.synchronize_inputs().map_err(|e| e.to_string())?;
            let _ = self
                .meta
                .session
                .run_binding(&cg.binding)
                .map_err(|e| e.to_string())?;
            cg.binding.synchronize_outputs().map_err(|e| e.to_string())?;
            cg.output_tensor.extract_tensor().1.to_vec()
        } else {
            let x_ref = TensorRef::from_array_view((Self::INPUT_SHAPE, x.as_slice()))
                .map_err(|e| e.to_string())?;
            let outputs = self
                .meta
                .session
                .run(ort::inputs![Self::INPUT_NAME => x_ref])
                .map_err(|e| e.to_string())?;
            let (out_shape, out_data) = outputs[Self::OUTPUT_NAME]
                .try_extract_tensor::<f32>()
                .map_err(|e| e.to_string())?;
            if out_shape.as_ref() != [1i64, 1, 56, 56] {
                return Err("Segmentation model returned invalid logits shape".to_string());
            }
            out_data.to_vec()
        };

        if logits.len() != 56 * 56 {
            return Err("Segmentation model returned invalid logits size".to_string());
        }
        let mut prob56 = Vec::with_capacity(56 * 56);
        for &l in &logits {
            prob56.push(1.0f32 / (1.0f32 + (-l).exp()));
        }
        let infer = Instant::now();
        let geom = geometry(&frame200, &prob56)?;
        let end = Instant::now();

        Ok(SegmentationPrediction {
            geometry: geom,
            engine: self.meta.engine.clone(),
            device: self.meta.device.clone(),
            timing_ms: TimingMs {
                preprocess: pre.duration_since(start).as_secs_f64() * 1000.0,
                inference: infer.duration_since(pre).as_secs_f64() * 1000.0,
                postprocess: end.duration_since(infer).as_secs_f64() * 1000.0,
                total: end.duration_since(start).as_secs_f64() * 1000.0,
            },
        })
    }
}

impl MouthModel for SegmentationModel {
    fn model_kind(&self) -> &'static str {
        "segmentation"
    }

    fn describe(&self) -> Value {
        SegmentationModel::describe(self)
    }

    fn predict(&mut self, gray400: &[u8], _ctx: PredictContext) -> Result<ModelOutput, String> {
        Ok(ModelOutput::Segmentation(
            self.predict_segmentation(gray400)?,
        ))
    }

    fn reset_state(&mut self) {}

    fn is_temporal(&self) -> bool {
        false
    }
}

// ---------------------------------------------------------------------------
// LoadedModel enum
// ---------------------------------------------------------------------------

pub enum LoadedModel {
    Face50(FaceModel),
    MouthLink(MouthLinkModel),
    Segmentation(SegmentationModel),
}

impl LoadedModel {
    pub fn load(model_kind: &str, checkpoint: &str, device: &str) -> Result<Self, String> {
        match model_kind {
            "face50" => Ok(Self::Face50(FaceModel::load(checkpoint, device)?)),
            "mouthlink" => Ok(Self::MouthLink(MouthLinkModel::load(checkpoint, device)?)),
            "segmentation" => Ok(Self::Segmentation(SegmentationModel::load(
                checkpoint, device,
            )?)),
            other => Err(format!("Unknown model kind: {other}")),
        }
    }
}

impl MouthModel for LoadedModel {
    fn model_kind(&self) -> &'static str {
        match self {
            LoadedModel::Face50(_) => "face50",
            LoadedModel::MouthLink(_) => "mouthlink",
            LoadedModel::Segmentation(_) => "segmentation",
        }
    }

    fn describe(&self) -> Value {
        match self {
            LoadedModel::Face50(m) => m.describe(),
            LoadedModel::MouthLink(m) => m.describe(),
            LoadedModel::Segmentation(m) => m.describe(),
        }
    }

    fn predict(&mut self, gray400: &[u8], ctx: PredictContext) -> Result<ModelOutput, String> {
        match self {
            LoadedModel::Face50(m) => m.predict(gray400, ctx),
            LoadedModel::MouthLink(m) => m.predict(gray400, ctx),
            LoadedModel::Segmentation(m) => m.predict(gray400, ctx),
        }
    }

    fn reset_state(&mut self) {
        match self {
            LoadedModel::Face50(m) => m.reset_state(),
            LoadedModel::MouthLink(m) => m.reset_state(),
            LoadedModel::Segmentation(m) => m.reset_state(),
        }
    }

    fn is_temporal(&self) -> bool {
        match self {
            LoadedModel::Face50(m) => m.is_temporal(),
            LoadedModel::MouthLink(m) => m.is_temporal(),
            LoadedModel::Segmentation(m) => m.is_temporal(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn deterministic_gray400() -> Vec<u8> {
        // Deterministic pseudo-random pattern using xorshift64 so tests have rich high-frequency content
        let mut x: u64 = 0x1234_5678_9abc_def0;
        let mut img = vec![0u8; 400 * 400];
        for px in &mut img {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            *px = (x & 0xff) as u8;
        }
        img
    }

    #[test]
    fn downsample400_exact_rounding() {
        let mut img = vec![0u8; 400 * 400];
        // Top-left 2x2 block: [1, 1, 0, 0] -> (2 + 2) >> 2 = 1
        img[0] = 1;
        img[1] = 1;
        // Next 2x2 block: [255, 255, 255, 255] -> (1020 + 2) >> 2 = 255
        img[2] = 255;
        img[3] = 255;
        img[402] = 255;
        img[403] = 255;
        let ds = downsample400(&img).unwrap();
        assert_eq!(ds.len(), 200 * 200);
        assert_eq!(ds[0], 1);
        assert_eq!(ds[1], 255);
        assert_eq!(ds[2], 0);
    }

    #[test]
    fn prepare_input_handles_identity_and_non_flip() {
        let img = deterministic_gray400();
        let same = prepare_input(&img, 400, 400).unwrap();
        assert_eq!(same.len(), 400 * 400);
        for (a, &b) in same.iter().zip(img.iter()) {
            assert_eq!(*a, (b as f32) / 255.0);
        }

        let r160 = prepare_input(&img, 160, 160).unwrap();
        assert_eq!(r160.len(), 160 * 160);
        // Flip horizontally and verify prepare_input does not flip
        let mut flipped = vec![0u8; 400 * 400];
        for y in 0..400 {
            for x in 0..400 {
                flipped[y * 400 + x] = img[y * 400 + (399 - x)];
            }
        }
        assert_ne!(r160, prepare_input(&flipped, 160, 160).unwrap());
    }

    #[test]
    fn geometry_thresholds_and_angle_calculation() {
        let mut frame200 = vec![0u8; 200 * 200];
        // Bright nose region (>= 0.94 -> >= 240): 10x10 block at x=50..60, y=50..60 (100 pixels)
        for y in 50..60 {
            for x in 50..60 {
                frame200[y * 200 + x] = 250;
            }
        }
        // Tongue probability mask: 12x12 = 144 pixels at x=30..42, y=30..42 in 56x56 space
        let mut prob56 = vec![0.0f32; 56 * 56];
        for y in 30..42 {
            for x in 30..42 {
                prob56[y * 56 + x] = 0.9;
            }
        }
        let geom = geometry(&frame200, &prob56).unwrap();
        assert!(geom.present);
        assert_eq!(geom.mask_area, 144);
        assert_eq!(geom.nose_area, 100);
        assert!(geom.raw);
        assert!(geom.angle.is_some());
        assert!(geom.centroid.is_some());
        assert!(geom.nose.is_some());
        // Check angle is ~atan2(74.2857, 74.2857) = pi/4 ~ 0.7854 (right of center 0.66)
        let ang = geom.angle.unwrap();
        assert!((ang - std::f64::consts::FRAC_PI_4).abs() < 0.02);
        assert!(geom.right > 0.0);
        assert_eq!(geom.left, 0.0);
        assert_eq!(geom.out, 1.0);
    }

    #[test]
    fn device_spec_rejects_torch_and_invalid_devices() {
        assert!(parse_device_spec("torch:cuda").is_err());
        assert!(parse_device_spec("torchscript:cpu").is_err());
        assert!(parse_device_spec("onnxgraph:cpu").is_err());
        assert!(parse_device_spec("bogus").is_err());
        assert_eq!(
            parse_device_spec("auto").unwrap(),
            ("onnx".to_string(), "auto".to_string())
        );
        assert_eq!(
            parse_device_spec("cuda").unwrap(),
            ("onnxgraph".to_string(), "cuda".to_string())
        );
        assert_eq!(
            parse_device_spec("onnx:cuda").unwrap(),
            ("onnx".to_string(), "cuda".to_string())
        );
    }

    #[test]
    fn face_manifest_validation_enforces_schema_channels_and_mask() {
        let base = json!({
            "schema": SCHEMA,
            "channels": FACE_CHANNELS.as_slice(),
            "width": 160,
            "height": 160,
            "trained_channels": ["TongueOut", "TongueLeft"],
            "validated_channels": ["TongueOut", "TongueLeft"],
            "valid_mask": (1u64 << channel_index("TongueOut").unwrap())
                | (1u64 << channel_index("TongueLeft").unwrap()),
        });
        assert!(parse_face_manifest(&base).is_ok());

        // Bad schema
        let mut bad = base.clone();
        bad["schema"] = json!("legacy44");
        assert!(parse_face_manifest(&bad).unwrap_err().contains("schema"));

        // Duplicate / unknown trained_channels
        let mut bad = base.clone();
        bad["trained_channels"] = json!(["MadeUp"]);
        assert!(parse_face_manifest(&bad)
            .unwrap_err()
            .contains("trained_channels"));

        // validated_channels not subset of trained_channels
        let mut bad = base.clone();
        bad["validated_channels"] = json!(["JawOpen"]);
        assert!(parse_face_manifest(&bad)
            .unwrap_err()
            .contains("validated_channels"));

        // valid_mask bool / negative / string / unvalidated bit
        for bad_mask in [
            json!(true),
            json!(-1),
            json!("1"),
            json!(1u64 << channel_index("JawOpen").unwrap()),
        ] {
            let mut bad = base.clone();
            bad["valid_mask"] = bad_mask;
            assert!(parse_face_manifest(&bad).unwrap_err().contains("valid_mask"));
        }

        // Temporal manifest validation
        let mut temporal = base.clone();
        temporal["temporal"] = json!({
            "kind": "gru_stateful",
            "state_input": "h_in",
            "state_output": "h_out",
            "state_shape": [1, 64]
        });
        let parsed = parse_face_manifest(&temporal).unwrap();
        assert_eq!(parsed.temporal_kind, "gru_stateful");
        assert_eq!(parsed.state_shape, Some([1, 64]));
    }

    #[test]
    fn temporal_state_reset_checks_stale_and_nonpositive_dt_within_same_session() {
        let t0: u64 = 10_000_000_000;
        let stale_ms = 120.0;

        // Missing state always resets.
        assert!(FaceModel::should_reset_temporal_state(
            false,
            Some(1),
            Some(t0),
            PredictContext {
                stale_ms,
                session_id: Some(1),
                source_ns: Some(t0 + 11_111_111),
            },
        ));

        // Same session with normal advancing dt (0 < dt <= stale_ms) keeps state.
        assert!(!FaceModel::should_reset_temporal_state(
            true,
            Some(1),
            Some(t0),
            PredictContext {
                stale_ms,
                session_id: Some(1),
                source_ns: Some(t0 + 11_111_111),
            },
        ));

        // Same session at exact stale_ms boundary keeps state.
        assert!(!FaceModel::should_reset_temporal_state(
            true,
            Some(1),
            Some(t0),
            PredictContext {
                stale_ms,
                session_id: Some(1),
                source_ns: Some(t0 + 120_000_000),
            },
        ));

        // Same session with dt > stale_ms MUST reset state!
        assert!(FaceModel::should_reset_temporal_state(
            true,
            Some(1),
            Some(t0),
            PredictContext {
                stale_ms,
                session_id: Some(1),
                source_ns: Some(t0 + 120_000_001),
            },
        ));

        // Same session with duplicate timestamp (dt == 0) MUST reset state!
        assert!(FaceModel::should_reset_temporal_state(
            true,
            Some(1),
            Some(t0),
            PredictContext {
                stale_ms,
                session_id: Some(1),
                source_ns: Some(t0),
            },
        ));

        // Same session with backwards timestamp (dt < 0) MUST reset state!
        assert!(FaceModel::should_reset_temporal_state(
            true,
            Some(1),
            Some(t0),
            PredictContext {
                stale_ms,
                session_id: Some(1),
                source_ns: Some(t0 - 1_000_000),
            },
        ));

        // Session change resets even when dt is valid.
        assert!(FaceModel::should_reset_temporal_state(
            true,
            Some(1),
            Some(t0),
            PredictContext {
                stale_ms,
                session_id: Some(2),
                source_ns: Some(t0 + 11_111_111),
            },
        ));

        // No session_id still checks dt.
        assert!(!FaceModel::should_reset_temporal_state(
            true,
            None,
            Some(t0),
            PredictContext {
                stale_ms,
                session_id: None,
                source_ns: Some(t0 + 20_000_000),
            },
        ));
        assert!(FaceModel::should_reset_temporal_state(
            true,
            None,
            Some(t0),
            PredictContext {
                stale_ms,
                session_id: None,
                source_ns: Some(t0 + 200_000_000),
            },
        ));
    }

    #[test]
    fn prepare_input_matches_python_pillow_when_available() {
        let python = match std::env::var("PICOET_TEST_PYTHON") {
            Ok(p) if !p.trim().is_empty() => p,
            _ => return,
        };
        if !Path::new(&python).is_file() {
            return;
        }
        let img = deterministic_gray400();
        let tmp = tempfile::tempdir().unwrap();
        let raw_path = tmp.path().join("in400.bin");
        fs::write(&raw_path, &img).unwrap();

        let script = r#"
import sys, numpy as np
from PIL import Image
raw = np.fromfile(sys.argv[1], dtype=np.uint8).reshape(400, 400)
out_dir = sys.argv[2]
for sz in (384, 224, 160, 400):
    arr = np.asarray(Image.fromarray(raw).resize((sz, sz), Image.Resampling.BILINEAR), dtype=np.uint8)
    arr.tofile(f"{out_dir}/pil_{sz}.bin")
"#;
        let status = std::process::Command::new(&python)
            .args([
                "-B",
                "-X",
                "utf8",
                "-c",
                script,
                raw_path.to_str().unwrap(),
                tmp.path().to_str().unwrap(),
            ])
            .status()
            .unwrap();
        assert!(status.success(), "Python Pillow reference script failed");

        for sz in [384usize, 224, 160, 400] {
            let expected_u8 = fs::read(tmp.path().join(format!("pil_{sz}.bin"))).unwrap();
            let actual_u8 = resize_pillow_bilinear_gray8(&img, 400, 400, sz, sz).unwrap();
            assert_eq!(
                actual_u8, expected_u8,
                "Pixel mismatch vs Pillow BILINEAR at 400->{sz}"
            );
            let actual_f32 = prepare_input(&img, sz, sz).unwrap();
            let expected_f32: Vec<f32> = expected_u8.iter().map(|&b| (b as f32) / 255.0).collect();
            assert_eq!(actual_f32, expected_f32);
        }
    }

    #[test]
    fn real_onnx_face50_model_loads_and_infers_when_present() {
        let (Ok(model_env), Ok(ort_env)) = (
            std::env::var("PICOET_TEST_ONNX_MODEL"),
            std::env::var("PICOET_TEST_ORT_DLL"),
        ) else {
            return;
        };
        let model_path = Path::new(model_env.trim());
        let ort_dll = Path::new(ort_env.trim());
        if model_env.trim().is_empty()
            || ort_env.trim().is_empty()
            || !model_path.is_file()
            || !ort_dll.is_file()
        {
            return;
        }
        std::env::set_var("ORT_DYLIB_PATH", ort_dll);

        let mut model = FaceModel::load(model_path, "cpu").expect("FaceModel CPU load failed");
        assert_eq!(model.provider(), "CPUExecutionProvider");

        let desc = model.describe();
        assert_eq!(desc["model_kind"], "face50");
        assert_eq!(desc["schema"], SCHEMA);

        let img = vec![128u8; 400 * 400];
        let pred = model
            .predict_face(&img, PredictContext::default())
            .expect("FaceModel CPU predict failed");
        assert_eq!(pred.schema, SCHEMA);
        assert!(pred.values.iter().all(|v| v.is_finite() && (0.0..=1.0).contains(v)));
        assert!(pred.timing_ms.total >= 0.0);

        // Also exercise LoadedModel + auto provider selection and compare outputs to CPU
        let mut auto_model =
            LoadedModel::load("face50", model_path.to_str().unwrap(), "auto")
                .expect("LoadedModel auto load failed");
        let auto_desc = auto_model.describe();
        assert_eq!(auto_desc["model_kind"], "face50");
        let ModelOutput::Face50(auto_pred) = auto_model
            .predict(&img, PredictContext::default())
            .expect("LoadedModel auto predict failed")
        else {
            panic!("Expected Face50 output");
        };
        let max_diff = pred
            .values
            .iter()
            .zip(auto_pred.values.iter())
            .map(|(a, b)| (a - b).abs())
            .fold(0.0f32, f32::max);
        assert!(
            max_diff < 1e-3,
            "auto provider ({}) diverged from CPU by {max_diff}",
            auto_desc["provider"]
        );
    }

    #[test]
    fn to_segmentation_input_preserves_constants_and_bounds() {
        let zeros = vec![0u8; 200 * 200];
        let out0 = to_segmentation_input(&zeros).unwrap();
        assert_eq!(out0.len(), 224 * 224);
        assert!(out0.iter().all(|&v| v == 0.0));

        let full = vec![255u8; 200 * 200];
        let out1 = to_segmentation_input(&full).unwrap();
        assert!(out1.iter().all(|&v| (v - 1.0).abs() < 1e-6));
    }

    #[test]
    fn mouthlink_and_face_reject_bad_extension_metadata_and_torch() {
        let tmp = tempfile::tempdir().unwrap();
        let bad_ext = tmp.path().join("model.pt");
        assert!(FaceModel::load(&bad_ext, "cpu")
            .err()
            .unwrap()
            .contains("ONNX"));
        assert!(MouthLinkModel::load(&bad_ext, "cpu")
            .err()
            .unwrap()
            .contains("ONNX"));

        // MouthLink missing required tongue channel & duplicate channel
        let onnx_path = tmp.path().join("ml.onnx");
        fs::write(&onnx_path, b"not-used-before-meta").unwrap();
        fs::write(
            onnx_path.with_extension("json"),
            json!({"width": 160, "height": 160, "channels": ["TongueOut", "TongueLeft"]})
                .to_string(),
        )
        .unwrap();
        assert!(MouthLinkModel::load(&onnx_path, "cpu")
            .err()
            .unwrap()
            .contains("unique TongueOut"));

        // MouthLink rejects torch:cpu
        fs::write(
            onnx_path.with_extension("json"),
            json!({
                "width": 160,
                "height": 160,
                "channels": ["TongueOut", "TongueLeft", "TongueRight", "TongueUp", "TongueDown"]
            })
            .to_string(),
        )
        .unwrap();
        assert!(MouthLinkModel::load(&onnx_path, "torch:cpu")
            .err()
            .unwrap()
            .contains("ONNX Runtime"));
    }

    #[test]
    fn mouth_model_trait_object_supports_mock_injection() {
        struct MockModel {
            resets: usize,
        }
        impl MouthModel for MockModel {
            fn model_kind(&self) -> &'static str {
                "face50"
            }
            fn describe(&self) -> Value {
                json!({"model_kind": "face50", "mock": true})
            }
            fn predict(&mut self, gray400: &[u8], _ctx: PredictContext) -> Result<ModelOutput, String> {
                if gray400.len() != 400 * 400 {
                    return Err("bad size".to_string());
                }
                Ok(ModelOutput::Face50(FacePrediction {
                    values: [0.0; 50],
                    channels: BTreeMap::new(),
                    valid_mask: 0,
                    schema: SCHEMA,
                    timing_ms: TimingMs::default(),
                }))
            }
            fn reset_state(&mut self) {
                self.resets += 1;
            }
            fn is_temporal(&self) -> bool {
                false
            }
        }

        let mut boxed: Box<dyn MouthModel> = Box::new(MockModel { resets: 0 });
        assert_eq!(boxed.model_kind(), "face50");
        assert!(!boxed.is_temporal());
        boxed.reset_state();
        let out = boxed
            .predict(&vec![0u8; 400 * 400], PredictContext::default())
            .unwrap();
        assert!(matches!(out, ModelOutput::Face50(_)));
    }
}
