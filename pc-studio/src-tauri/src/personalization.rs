//! PC calibration, consented sample collection and transactional personal profiles.
//!
//! Exact Rust port of `pc-studio/personalization.py` with a pluggable `PersonalTrainer`
//! trait (zero Python dependency).

use crate::config::write_text_atomic;
use crate::face_model::downsample400;
use crate::wire::FrameHeader;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, VecDeque};
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{SystemTime, UNIX_EPOCH};

pub const STEPS: [&str; 6] = ["fit", "closed", "open", "center", "left", "right"];
pub const TRAINING_STEPS: [&str; 9] = [
    "fit", "closed", "open", "wide", "smile", "speech", "center", "left", "right",
];

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Calibration {
    pub left: f64,
    pub center: f64,
    pub right: f64,
    pub min_area: i64,
}

pub const DEFAULT_CALIBRATION: Calibration = Calibration {
    left: 0.33,
    center: 0.66,
    right: 1.11,
    min_area: 100,
};

impl Default for Calibration {
    fn default() -> Self {
        DEFAULT_CALIBRATION
    }
}

pub fn label_for_step(step: &str) -> Option<u8> {
    match step {
        "closed" | "smile" | "speech" => Some(1),
        "open" | "wide" => Some(2),
        "center" => Some(3),
        "left" => Some(4),
        "right" => Some(5),
        _ => None,
    }
}

pub fn valid_calibration(cal: &Calibration) -> bool {
    let vals = [cal.left, cal.center, cal.right];
    vals.iter().all(|v| v.is_finite() && *v > 0.0 && *v < 1.7)
        && (cal.left - cal.center).abs() >= 0.08
        && (cal.right - cal.center).abs() >= 0.08
        && (cal.left - cal.center) * (cal.right - cal.center) < 0.0
        && (20..=600).contains(&cal.min_area)
}

fn median_f64(values: &[f64]) -> f64 {
    assert!(!values.is_empty());
    let mut sorted = values.to_vec();
    sorted.sort_by(|a, b| a.total_cmp(b));
    let mid = sorted.len() / 2;
    if sorted.len() % 2 == 1 {
        sorted[mid]
    } else {
        (sorted[mid - 1] + sorted[mid]) * 0.5
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct CalibrationSample {
    pub frame: Vec<u8>,
    pub mask: Vec<bool>,
    pub label: u8,
    pub pose: String,
    pub raw_angle: Option<f64>,
    pub area: i64,
    pub session_id: String,
    pub source_seq: String,
    pub sensor_ns: String,
}

impl CalibrationSample {
    /// Lightweight sample constructor (useful for calibration math & unit tests).
    pub fn minimal(label: u8, raw_angle: Option<f64>, area: i64) -> Self {
        Self {
            frame: Vec::new(),
            mask: Vec::new(),
            label,
            pose: String::new(),
            raw_angle,
            area,
            session_id: String::new(),
            source_seq: String::new(),
            sensor_ns: String::new(),
        }
    }
}

pub fn calibration_from_samples(samples: &[CalibrationSample]) -> Result<Calibration, String> {
    let mut angles_center = Vec::new();
    let mut angles_left = Vec::new();
    let mut angles_right = Vec::new();
    for s in samples {
        if let Some(angle) = s.raw_angle {
            match s.label {
                3 => angles_center.push(angle),
                4 => angles_left.push(angle),
                5 => angles_right.push(angle),
                _ => {}
            }
        }
    }
    if angles_center.len() < 3 || angles_left.len() < 3 || angles_right.len() < 3 {
        return Err("Not enough visible tongue samples for calibration".to_string());
    }

    let mut negative: Vec<i64> = samples
        .iter()
        .filter(|s| s.label <= 2)
        .map(|s| if s.raw_angle.is_some() { s.area } else { 0 })
        .collect();
    negative.sort_unstable();

    let mut positive: Vec<i64> = samples
        .iter()
        .filter(|s| s.label >= 3 && s.raw_angle.is_some())
        .map(|s| s.area)
        .collect();
    positive.sort_unstable();

    let high = if negative.is_empty() {
        0
    } else {
        let idx = ((negative.len() as f64) * 0.95) as usize;
        negative[idx.min(negative.len() - 1)]
    };
    let low = {
        let idx = ((positive.len() as f64) * 0.05) as usize;
        positive[idx.min(positive.len() - 1)]
    };
    let area = if high + 10 < low {
        let raw_area = (high as f64 + (low - high) as f64 * 0.4 + 0.5) as i64;
        raw_area.clamp(40, 300)
    } else {
        100
    };

    let cal = Calibration {
        left: median_f64(&angles_left),
        center: median_f64(&angles_center),
        right: median_f64(&angles_right),
        min_area: area,
    };
    if !valid_calibration(&cal) {
        return Err("Left and right calibration poses are not separated".to_string());
    }
    Ok(cal)
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct CalibratedGeometry {
    pub present: bool,
    pub angle: Option<f64>,
    pub position: f64,
    pub left: f64,
    pub right: f64,
    pub out: f64,
    pub area_threshold: i64,
    pub size_held: bool,
}

/// Apply personal calibration & size hysteresis to raw segmentation geometry.
pub fn apply_calibration(
    raw_angle: Option<f64>,
    mask_area: i64,
    cal: &Calibration,
    active: bool,
    hysteresis: bool,
) -> CalibratedGeometry {
    let threshold = if active && hysteresis {
        ((cal.min_area as f64 * 0.6) as i64).max(20)
    } else {
        cal.min_area
    };
    let present = raw_angle.is_some() && mask_area >= threshold;
    let mut position = 0.0;
    if present {
        let angle = raw_angle.unwrap();
        let p = if (angle - cal.center) * (cal.left - cal.center) > 0.0 {
            -(angle - cal.center) / (cal.left - cal.center)
        } else {
            (angle - cal.center) / (cal.right - cal.center)
        };
        let p = p.clamp(-1.0, 1.0);
        position = if p.abs() < 0.06 {
            0.0
        } else {
            (p.abs() - 0.06) / 0.94 * if p > 0.0 { 1.0 } else { -1.0 }
        };
    }
    CalibratedGeometry {
        present,
        angle: if present { raw_angle } else { None },
        position,
        left: (-position).max(0.0),
        right: position.max(0.0),
        out: if present { 1.0 } else { 0.0 },
        area_threshold: threshold,
        size_held: present && mask_area < cal.min_area,
    }
}

/// JSON helper matching `personalization.py`: adapters without `raw_angle` are returned unchanged.
pub fn apply_calibration_json(
    raw: &Value,
    cal: &Calibration,
    active: bool,
    hysteresis: bool,
) -> Value {
    let Some(obj) = raw.as_object() else {
        return raw.clone();
    };
    if !obj.contains_key("raw_angle") {
        return raw.clone();
    }
    let raw_angle = obj.get("raw_angle").and_then(|v| v.as_f64());
    let mask_area = obj.get("mask_area").and_then(|v| v.as_i64()).unwrap_or(0);
    let applied = apply_calibration(raw_angle, mask_area, cal, active, hysteresis);
    let mut out = obj.clone();
    out.insert("present".to_string(), Value::Bool(applied.present));
    out.insert(
        "angle".to_string(),
        applied.angle.map(Value::from).unwrap_or(Value::Null),
    );
    out.insert("position".to_string(), Value::from(applied.position));
    out.insert("left".to_string(), Value::from(applied.left));
    out.insert("right".to_string(), Value::from(applied.right));
    out.insert("out".to_string(), Value::from(applied.out));
    out.insert(
        "area_threshold".to_string(),
        Value::from(applied.area_threshold),
    );
    out.insert("size_held".to_string(), Value::Bool(applied.size_held));
    Value::Object(out)
}

/// Subset of raw segmentation fields needed by `Wizard::feed`.
#[derive(Debug, Clone, Default)]
pub struct WizardFrameRaw {
    pub nose_area: i64,
    pub raw_angle: Option<f64>,
    pub mask_area: i64,
    pub mask: Vec<bool>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WizardStatus {
    pub mode: String,
    pub step: String,
    pub hint: String,
    pub collected: usize,
    pub target: usize,
    pub progress: f64,
}

fn compute_blocks50(frame200: &[u8]) -> [f64; 2500] {
    let mut blocks = [0.0f64; 2500];
    for by in 0..50 {
        for bx in 0..50 {
            let mut sum = 0u32;
            for dy in 0..4 {
                let row_off = (by * 4 + dy) * 200 + bx * 4;
                sum += frame200[row_off] as u32
                    + frame200[row_off + 1] as u32
                    + frame200[row_off + 2] as u32
                    + frame200[row_off + 3] as u32;
            }
            blocks[by * 50 + bx] = (sum as f64) / 16.0;
        }
    }
    blocks
}

fn mean_abs_diff(a: &[f64; 2500], b: &[f64; 2500]) -> f64 {
    let mut sum = 0.0;
    for i in 0..2500 {
        sum += (a[i] - b[i]).abs();
    }
    sum / 2500.0
}

/// Independent <=20Hz guide sampling; the video/inference pipeline stays at 90Hz.
#[derive(Debug, Clone)]
pub struct Wizard {
    pub mode: String,
    steps: Vec<&'static str>,
    pub step: &'static str,
    pub hint: String,
    pub session: Option<u64>,
    last_ns: Option<u64>,
    entered_ns: Option<u64>,
    pub count: usize,
    window: VecDeque<f64>,
    rows: BTreeMap<&'static str, Vec<CalibrationSample>>,
    current: Vec<CalibrationSample>,
    closed_ref: Option<[f64; 2500]>,
    open_ref: Option<[f64; 2500]>,
    previous_blocks: Option<[f64; 2500]>,
    center: Option<f64>,
    left: Option<f64>,
    side_best: Option<f64>,
    pub finished: bool,
}

impl Wizard {
    pub fn new(mode: &str) -> Result<Self, String> {
        if mode != "calibrate" && mode != "retrain" {
            return Err("Unknown personalization mode".to_string());
        }
        let steps: Vec<&'static str> = if mode == "retrain" {
            TRAINING_STEPS.to_vec()
        } else {
            STEPS.to_vec()
        };
        let mut rows = BTreeMap::new();
        for &s in &steps[1..] {
            rows.insert(s, Vec::new());
        }
        Ok(Self {
            mode: mode.to_string(),
            steps,
            step: "fit",
            hint: "fit".to_string(),
            session: None,
            last_ns: None,
            entered_ns: None,
            count: 0,
            window: VecDeque::with_capacity(8),
            rows,
            current: Vec::new(),
            closed_ref: None,
            open_ref: None,
            previous_blocks: None,
            center: None,
            left: None,
            side_best: None,
            finished: false,
        })
    }

    pub fn target(&self) -> usize {
        if self.step == "fit" {
            return 20;
        }
        if self.mode == "retrain" {
            return 60;
        }
        match self.step {
            "closed" | "open" => 30,
            "center" => 24,
            "left" | "right" => 40,
            _ => 20,
        }
    }

    pub fn reset_hold(&mut self, hint: &str) {
        self.count = 0;
        self.current.clear();
        self.window.clear();
        self.hint = hint.to_string();
    }

    pub fn feed(
        &mut self,
        image400: &[u8],
        raw: &WizardFrameRaw,
        header: &FrameHeader,
    ) -> Result<Option<Vec<CalibrationSample>>, String> {
        let ns = header.mouth_sensor_ns;
        if self.session.is_none() {
            self.session = Some(header.session_id);
        }
        if self.session != Some(header.session_id) {
            return Err("Camera session changed during calibration".to_string());
        }
        if let Some(prev_ns) = self.last_ns {
            let delta = ns as i128 - prev_ns as i128;
            if delta < 50_000_000 {
                return Ok(None);
            }
            if delta > 250_000_000 {
                self.reset_hold("fresh_video");
            }
        }
        self.last_ns = Some(ns);
        if self.entered_ns.is_none() {
            self.entered_ns = Some(ns);
        }

        let frame = downsample400(image400)?;
        let blocks = compute_blocks50(&frame);
        let nose = raw.nose_area;
        if !(3000..=11000).contains(&nose) {
            self.reset_hold(if nose < 3000 { "fit_low" } else { "fit_glare" });
            return Ok(None);
        }

        if self.step == "fit" {
            self.count += 1;
        } else {
            let entered = self.entered_ns.unwrap_or(ns);
            if (ns as i128 - entered as i128) < 1_500_000_000 {
                self.hint = "settle".to_string();
                return Ok(None);
            }
            if self.step == "open" {
                if let Some(ref closed_ref) = self.closed_ref {
                    if mean_abs_diff(&blocks, closed_ref) < 5.0 {
                        self.reset_hold("open");
                        return Ok(None);
                    }
                }
            }
            if matches!(self.step, "center" | "left" | "right") {
                if self.mode == "retrain" {
                    let still = match &self.previous_blocks {
                        None => true,
                        Some(prev) => mean_abs_diff(&blocks, prev) < 2.5,
                    };
                    self.previous_blocks = Some(blocks);
                    let moved_from_open = self
                        .open_ref
                        .as_ref()
                        .is_some_and(|open_ref| mean_abs_diff(&blocks, open_ref) >= 4.0);
                    if !moved_from_open || !still {
                        self.reset_hold("hold_tongue");
                        return Ok(None);
                    }
                } else {
                    let Some(angle) = raw.raw_angle else {
                        self.reset_hold("visible_tongue");
                        return Ok(None);
                    };
                    if raw.mask_area < 40 {
                        self.reset_hold("visible_tongue");
                        return Ok(None);
                    }
                    if self.step != "center" {
                        let center = self.center.unwrap_or(0.0);
                        let wrong_side = self.step == "right"
                            && self
                                .left
                                .is_some_and(|left| (angle - center) * (left - center) > 0.0);
                        if (angle - center).abs() < 0.1 || wrong_side {
                            self.reset_hold("move_side");
                            return Ok(None);
                        }
                    }
                    if self.window.len() == 8 {
                        self.window.pop_front();
                    }
                    self.window.push_back(angle);
                    let steady = if self.window.len() == 8 {
                        let min_v = self.window.iter().copied().fold(f64::INFINITY, f64::min);
                        let max_v = self
                            .window
                            .iter()
                            .copied()
                            .fold(f64::NEG_INFINITY, f64::max);
                        (max_v - min_v) <= 0.12
                    } else {
                        false
                    };
                    if self.step == "center" && self.window.len() == 8 && !steady {
                        self.reset_hold("steady");
                        return Ok(None);
                    }
                    if self.step != "center" {
                        if steady {
                            let win_slice: Vec<f64> = self.window.iter().copied().collect();
                            let med = median_f64(&win_slice);
                            let center = self.center.unwrap_or(0.0);
                            if self
                                .side_best
                                .is_none_or(|best| (med - center).abs() > (best - center).abs())
                            {
                                self.side_best = Some(med);
                            }
                        }
                        if self.side_best.is_none() {
                            self.hint = "steady".to_string();
                            return Ok(None);
                        }
                    }
                }
            }

            let mask = if raw.mask.len() == 56 * 56 {
                raw.mask.clone()
            } else {
                vec![false; 56 * 56]
            };
            let label = label_for_step(self.step).unwrap_or(0);
            self.current.push(CalibrationSample {
                frame,
                mask,
                label,
                pose: self.step.to_string(),
                raw_angle: raw.raw_angle,
                area: raw.mask_area,
                session_id: header.session_id.to_string(),
                source_seq: header.source_seq.to_string(),
                sensor_ns: ns.to_string(),
            });
            self.count += 1;
        }

        self.hint = "holding".to_string();
        if self.count < self.target() {
            return Ok(None);
        }

        if self.step != "fit" {
            if self.mode == "calibrate" && matches!(self.step, "left" | "right") {
                for sample in &mut self.current {
                    sample.raw_angle = self.side_best;
                }
            }
            if matches!(self.step, "closed" | "open") && !self.current.is_empty() {
                let mut reference = [0.0f64; 2500];
                for sample in &self.current {
                    let b = compute_blocks50(&sample.frame);
                    for i in 0..2500 {
                        reference[i] += b[i];
                    }
                }
                let denom = self.current.len() as f64;
                for v in &mut reference {
                    *v /= denom;
                }
                if self.step == "closed" {
                    self.closed_ref = Some(reference);
                } else {
                    self.open_ref = Some(reference);
                }
            } else if self.mode == "calibrate" && self.step == "center" && !self.window.is_empty() {
                let win_slice: Vec<f64> = self.window.iter().copied().collect();
                self.center = Some(median_f64(&win_slice));
            } else if self.mode == "calibrate" && self.step == "left" {
                self.left = self.side_best;
            }
            let finished_current = std::mem::take(&mut self.current);
            self.rows.insert(self.step, finished_current);
        }

        if self.step == "right" {
            self.finished = true;
            let mut all = Vec::new();
            for &s in &self.steps[1..] {
                if let Some(samples) = self.rows.get(s) {
                    all.extend(samples.iter().cloned());
                }
            }
            return Ok(Some(all));
        }

        let idx = self
            .steps
            .iter()
            .position(|&s| s == self.step)
            .unwrap_or(0);
        let next_step = self.steps[idx + 1];
        self.step = next_step;
        self.entered_ns = Some(ns);
        self.side_best = None;
        self.previous_blocks = None;
        self.reset_hold(next_step);
        Ok(None)
    }

    pub fn status(&self) -> WizardStatus {
        let idx = self
            .steps
            .iter()
            .position(|&s| s == self.step)
            .unwrap_or(0);
        let target = self.target();
        let frac = ((self.count as f64) / (target.max(1) as f64)).min(1.0);
        WizardStatus {
            mode: self.mode.clone(),
            step: self.step.to_string(),
            hint: self.hint.clone(),
            collected: self.count,
            target,
            progress: (idx as f64 + frac) / (self.steps.len() as f64),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PersonalProfile {
    pub checkpoint: String,
    pub calibration: Calibration,
    pub head: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct PendingProfile {
    pub profile: PersonalProfile,
    pub generation: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TrainOutcome {
    pub accepted: bool,
    pub metrics: Option<Value>,
    pub checkpoint: Option<String>,
    pub calibration: Option<Calibration>,
}

/// Optional callback trait for personal head retraining (zero Python dependency).
pub trait PersonalTrainer: Send + Sync {
    fn train(
        &self,
        samples: Vec<CalibrationSample>,
        base_checkpoint: &str,
        current_checkpoint: &str,
        job_dir: &Path,
        cancelled: Arc<AtomicBool>,
        on_progress: Box<dyn Fn(f64) + Send + Sync>,
    ) -> Result<TrainOutcome, String>;
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PersonalizationStatus {
    pub state: String,
    pub job_id: Option<String>,
    pub wizard: Option<WizardStatus>,
    pub progress: f64,
    pub error: String,
    pub metrics: Option<Value>,
    pub profile: PersonalProfile,
    pub training_worker_alive: bool,
}

fn unix_time_ns_string() -> String {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos().to_string())
        .unwrap_or_else(|_| "0".to_string())
}

/// Normalize path like Python's `Path(checkpoint).resolve()` even when the file does not exist yet.
fn resolve_checkpoint_path(checkpoint: &str) -> String {
    let trimmed = checkpoint.trim();
    if trimmed.is_empty() {
        return String::new();
    }
    let path = Path::new(trimmed);
    if let Ok(canon) = fs::canonicalize(path) {
        return strip_win_verbatim_prefix(&canon);
    }
    let abs = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    };
    let mut normalized = PathBuf::new();
    for comp in abs.components() {
        match comp {
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            other => normalized.push(other.as_os_str()),
        }
    }
    strip_win_verbatim_prefix(&normalized)
}

fn strip_win_verbatim_prefix(path: &Path) -> String {
    let s = path.to_string_lossy();
    #[cfg(windows)]
    if let Some(stripped) = s.strip_prefix(r"\\?\") {
        return stripped.to_string();
    }
    s.into_owned()
}

struct PersonalizationState {
    checkpoint: String,
    profile: PersonalProfile,
    wizard: Option<Wizard>,
    state: String,
    error: String,
    progress: f64,
    metrics: Option<Value>,
    generation: u64,
    cancel_flag: Arc<AtomicBool>,
    worker_alive: Arc<AtomicBool>,
    worker_handle: Option<JoinHandle<()>>,
    pending: Option<PendingProfile>,
    job_id: Option<String>,
}

struct PersonalizationInner {
    root: PathBuf,
    on_changed: Option<Arc<dyn Fn(bool) + Send + Sync>>,
    trainer: Mutex<Option<Arc<dyn PersonalTrainer>>>,
    state: Mutex<PersonalizationState>,
}

/// Transactional personal calibration and profile lifecycle manager.
#[derive(Clone)]
pub struct Personalization {
    inner: Arc<PersonalizationInner>,
}

impl Personalization {
    pub fn new(
        root: PathBuf,
        on_changed: Option<Arc<dyn Fn(bool) + Send + Sync>>,
        trainer: Option<Arc<dyn PersonalTrainer>>,
    ) -> Self {
        let initial_profile = PersonalProfile {
            checkpoint: String::new(),
            calibration: DEFAULT_CALIBRATION,
            head: None,
        };
        Self {
            inner: Arc::new(PersonalizationInner {
                root,
                on_changed,
                trainer: Mutex::new(trainer),
                state: Mutex::new(PersonalizationState {
                    checkpoint: String::new(),
                    profile: initial_profile,
                    wizard: None,
                    state: "idle".to_string(),
                    error: String::new(),
                    progress: 0.0,
                    metrics: None,
                    generation: 0,
                    cancel_flag: Arc::new(AtomicBool::new(false)),
                    worker_alive: Arc::new(AtomicBool::new(false)),
                    worker_handle: None,
                    pending: None,
                    job_id: None,
                }),
            }),
        }
    }

    pub fn root(&self) -> &Path {
        &self.inner.root
    }

    pub fn set_trainer(&self, trainer: Option<Arc<dyn PersonalTrainer>>) {
        *self.inner.trainer.lock().unwrap() = trainer;
    }

    pub fn standard(&self) -> PersonalProfile {
        let st = self.inner.state.lock().unwrap();
        PersonalProfile {
            checkpoint: st.checkpoint.clone(),
            calibration: DEFAULT_CALIBRATION,
            head: None,
        }
    }

    pub fn checkpoint(&self) -> String {
        self.inner.state.lock().unwrap().checkpoint.clone()
    }

    pub fn profile(&self) -> PersonalProfile {
        self.inner.state.lock().unwrap().profile.clone()
    }

    pub fn pending(&self) -> Option<PendingProfile> {
        self.inner.state.lock().unwrap().pending.clone()
    }

    pub fn has_active_wizard(&self) -> bool {
        self.inner.state.lock().unwrap().wizard.is_some()
    }

    pub fn set_error(&self, message: &str) {
        self.inner.state.lock().unwrap().error = message.to_string();
    }

    pub fn set_checkpoint(&self, checkpoint: &str) {
        let resolved = resolve_checkpoint_path(checkpoint);
        let mut st = self.inner.state.lock().unwrap();
        if resolved == st.checkpoint {
            return;
        }
        st.checkpoint = resolved.clone();
        let file = self.inner.root.join("active.json");
        let loaded = fs::read_to_string(&file)
            .ok()
            .and_then(|txt| serde_json::from_str::<PersonalProfile>(&txt).ok());
        st.profile = match loaded {
            Some(p) if p.checkpoint == resolved => p,
            _ => PersonalProfile {
                checkpoint: resolved,
                calibration: DEFAULT_CALIBRATION,
                head: None,
            },
        };
    }

    pub fn persist(&self, profile: PersonalProfile) -> Result<(), String> {
        let json = serde_json::to_string_pretty(&profile).map_err(|e| e.to_string())?;
        write_text_atomic(&self.inner.root.join("active.json"), &json)
            .map_err(|e| e.to_string())?;
        self.inner.state.lock().unwrap().profile = profile;
        Ok(())
    }

    fn cancel_locked(st: &mut PersonalizationState) {
        st.generation += 1;
        st.cancel_flag.store(true, Ordering::SeqCst);
        st.wizard = None;
        st.pending = None;
        if matches!(st.state.as_str(), "collecting" | "training" | "applying") {
            st.state = "cancelled".to_string();
        }
    }

    pub fn begin(&self, mode: &str, confirmed: bool) -> Result<(), String> {
        let mut st = self.inner.state.lock().unwrap();
        if mode != "calibrate" && mode != "retrain" {
            return Err("Unknown personalization mode".to_string());
        }
        if st.worker_alive.load(Ordering::Acquire) {
            return Err("Wait for the previous training job to stop".to_string());
        }
        if mode == "retrain" && !confirmed {
            return Err("Confirm local image recording and personal training first".to_string());
        }
        Self::cancel_locked(&mut st);
        st.cancel_flag = Arc::new(AtomicBool::new(false));
        st.wizard = Some(Wizard::new(mode)?);
        st.job_id = Some(unix_time_ns_string());
        st.state = "collecting".to_string();
        st.error.clear();
        st.metrics = None;
        st.progress = 0.0;
        Ok(())
    }

    pub fn cancel(&self) {
        let mut st = self.inner.state.lock().unwrap();
        Self::cancel_locked(&mut st);
    }

    pub fn restore(&self) -> Result<bool, String> {
        let (head_changed, standard_profile) = {
            let mut st = self.inner.state.lock().unwrap();
            let head_changed = st.profile.head.is_some();
            Self::cancel_locked(&mut st);
            let std_prof = PersonalProfile {
                checkpoint: st.checkpoint.clone(),
                calibration: DEFAULT_CALIBRATION,
                head: None,
            };
            let json = serde_json::to_string_pretty(&std_prof).map_err(|e| e.to_string())?;
            write_text_atomic(&self.inner.root.join("active.json"), &json)
                .map_err(|e| e.to_string())?;
            st.profile = std_prof.clone();
            st.state = "restored".to_string();
            st.error.clear();
            st.metrics = None;
            (head_changed, std_prof)
        };
        let _ = standard_profile;
        if let Some(ref cb) = self.inner.on_changed {
            cb(head_changed);
        }
        Ok(head_changed)
    }

    pub fn feed(
        &self,
        image400: &[u8],
        raw: &WizardFrameRaw,
        header: &FrameHeader,
    ) -> Result<(), String> {
        let mut notify_changed: Option<bool> = None;
        {
            let mut st = self.inner.state.lock().unwrap();
            let Some(wizard) = st.wizard.as_mut() else {
                return Ok(());
            };
            match wizard.feed(image400, raw, header) {
                Ok(None) => return Ok(()),
                Ok(Some(samples)) => {
                    let mode = wizard.mode.clone();
                    st.wizard = None;
                    if mode == "calibrate" {
                        match calibration_from_samples(&samples) {
                            Ok(cal) => {
                                let profile = PersonalProfile {
                                    checkpoint: st.profile.checkpoint.clone(),
                                    calibration: cal,
                                    head: st.profile.head.clone(),
                                };
                                let json = serde_json::to_string_pretty(&profile)
                                    .map_err(|e| e.to_string())?;
                                if let Err(err) =
                                    write_text_atomic(&self.inner.root.join("active.json"), &json)
                                {
                                    st.state = "failed".to_string();
                                    st.error = err.to_string();
                                    return Ok(());
                                }
                                st.profile = profile;
                                st.state = "calibrated".to_string();
                                notify_changed = Some(false);
                            }
                            Err(err) => {
                                st.state = "failed".to_string();
                                st.error = err;
                                return Ok(());
                            }
                        }
                    } else {
                        self.spawn_training_locked(&mut st, samples);
                    }
                }
                Err(err) => {
                    st.wizard = None;
                    st.state = "failed".to_string();
                    st.error = err;
                    return Ok(());
                }
            }
        }
        if let Some(head_changed) = notify_changed {
            if let Some(ref cb) = self.inner.on_changed {
                cb(head_changed);
            }
        }
        Ok(())
    }

    /// Directly submit collected samples for retraining (also handy for unit tests).
    pub fn submit_retrain_samples(&self, samples: Vec<CalibrationSample>) -> Result<(), String> {
        let mut st = self.inner.state.lock().unwrap();
        st.wizard = None;
        self.spawn_training_locked(&mut st, samples);
        Ok(())
    }

    fn spawn_training_locked(
        &self,
        st: &mut PersonalizationState,
        samples: Vec<CalibrationSample>,
    ) {
        st.state = "training".to_string();
        st.progress = 0.0;
        let token = st.generation;
        let cancelled = Arc::clone(&st.cancel_flag);
        let job_id = st.job_id.clone().unwrap_or_else(unix_time_ns_string);
        let checkpoint = st.checkpoint.clone();
        let current = st
            .profile
            .head
            .clone()
            .unwrap_or_else(|| st.checkpoint.clone());
        let alive = Arc::new(AtomicBool::new(true));
        st.worker_alive = Arc::clone(&alive);

        let inner = Arc::clone(&self.inner);
        let handle = thread::Builder::new()
            .name("tongue-personal-training".to_string())
            .spawn(move || {
                struct AliveReset(Arc<AtomicBool>);
                impl Drop for AliveReset {
                    fn drop(&mut self) {
                        self.0.store(false, Ordering::Release);
                    }
                }
                let _reset = AliveReset(alive);

                let run = || -> Result<TrainOutcome, String> {
                    let trainer = inner.trainer.lock().unwrap().clone().ok_or_else(|| {
                        "Personal head retraining is not configured in this build".to_string()
                    })?;
                    let job_dir = inner.root.join("jobs").join(&job_id);
                    if job_dir.exists() {
                        return Err(format!("Job directory already exists: {}", job_dir.display()));
                    }
                    fs::create_dir_all(&job_dir).map_err(|e| e.to_string())?;
                    let progress_inner = Arc::clone(&inner);
                    let on_progress = Box::new(move |val: f64| {
                        let mut guard = progress_inner.state.lock().unwrap();
                        if token == guard.generation {
                            guard.progress = val;
                        }
                    });
                    trainer.train(
                        samples,
                        &checkpoint,
                        &current,
                        &job_dir,
                        Arc::clone(&cancelled),
                        on_progress,
                    )
                };

                match run() {
                    Ok(result) => {
                        let mut guard = inner.state.lock().unwrap();
                        if token != guard.generation || cancelled.load(Ordering::Acquire) {
                            return;
                        }
                        guard.metrics = result.metrics;
                        if !result.accepted {
                            guard.state = "rejected".to_string();
                            guard.progress = 1.0;
                            return;
                        }
                        let profile = PersonalProfile {
                            checkpoint,
                            head: result.checkpoint,
                            calibration: result.calibration.unwrap_or(DEFAULT_CALIBRATION),
                        };
                        guard.pending = Some(PendingProfile {
                            profile,
                            generation: token,
                        });
                        guard.state = "applying".to_string();
                        guard.progress = 0.98;
                    }
                    Err(err) => {
                        let mut guard = inner.state.lock().unwrap();
                        if token == guard.generation && !cancelled.load(Ordering::Acquire) {
                            guard.state = "failed".to_string();
                            guard.error = err;
                        }
                    }
                }
            });

        match handle {
            Ok(h) => {
                st.worker_handle = Some(h);
            }
            Err(err) => {
                st.worker_alive.store(false, Ordering::Release);
                st.state = "failed".to_string();
                st.error = err.to_string();
            }
        }
    }

    pub fn join_worker(&self) {
        let handle = self.inner.state.lock().unwrap().worker_handle.take();
        if let Some(h) = handle {
            let _ = h.join();
        }
    }

    pub fn commit_pending(&self, request: &PendingProfile) -> Result<bool, String> {
        let committed = {
            let mut st = self.inner.state.lock().unwrap();
            let matches_pending = st.pending.as_ref() == Some(request);
            if !matches_pending
                || request.generation != st.generation
                || st.cancel_flag.load(Ordering::Acquire)
            {
                return Ok(false);
            }
            let json =
                serde_json::to_string_pretty(&request.profile).map_err(|e| e.to_string())?;
            write_text_atomic(&self.inner.root.join("active.json"), &json)
                .map_err(|e| e.to_string())?;
            st.profile = request.profile.clone();
            st.pending = None;
            st.state = "accepted".to_string();
            st.progress = 1.0;
            true
        };
        if committed {
            if let Some(ref cb) = self.inner.on_changed {
                cb(true);
            }
        }
        Ok(committed)
    }

    pub fn fail_pending(&self, request: &PendingProfile, error: &str) {
        let mut st = self.inner.state.lock().unwrap();
        if st.pending.as_ref() == Some(request) {
            st.pending = None;
            st.state = "failed".to_string();
            st.error = error.to_string();
        }
    }

    pub fn status(&self) -> PersonalizationStatus {
        let st = self.inner.state.lock().unwrap();
        PersonalizationStatus {
            state: st.state.clone(),
            job_id: st.job_id.clone(),
            wizard: st.wizard.as_ref().map(|w| w.status()),
            progress: st.progress,
            error: st.error.clone(),
            metrics: st.metrics.clone(),
            profile: st.profile.clone(),
            training_worker_alive: st.worker_alive.load(Ordering::Acquire),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::mpsc;

    fn make_header(seq: u64, session_id: u64, mouth_sensor_ns: u64) -> FrameHeader {
        FrameHeader {
            payload_bytes: 100,
            flags: 1,
            session_id,
            source_seq: seq,
            sensor_ns: mouth_sensor_ns,
            vendor_ns: mouth_sensor_ns,
            copy_mono_ns: mouth_sensor_ns,
            encode_begin_ns: mouth_sensor_ns,
            encode_end_ns: mouth_sensor_ns,
            emit_mono_ns: mouth_sensor_ns,
            eye0_sensor_ns: mouth_sensor_ns,
            eye1_sensor_ns: mouth_sensor_ns,
            mouth_sensor_ns,
            raw_identity: seq,
            width: 400,
            height: 400,
            raw_crc32: 0,
            quality: 95,
        }
    }

    fn synthetic_samples() -> Vec<CalibrationSample> {
        let spec = [
            (1u8, None, 0i64),
            (2, Some(0.65), 30),
            (3, Some(0.66), 200),
            (4, Some(0.33), 200),
            (5, Some(1.11), 200),
        ];
        let mut out = Vec::new();
        for (label, angle, area) in spec {
            for _ in 0..12 {
                out.push(CalibrationSample::minimal(label, angle, area));
            }
        }
        out
    }

    fn run_wizard(mode: &str) -> (Wizard, Vec<CalibrationSample>) {
        let mut wizard = Wizard::new(mode).unwrap();
        for index in 0..1200u64 {
            let angle = match wizard.step {
                "center" => Some(0.66),
                "left" => Some(0.33),
                "right" => Some(1.11),
                _ => None,
            };
            let shade: u8 = match wizard.step {
                "fit" | "closed" => 100,
                "open" => 110,
                "wide" => 115,
                "smile" => 105,
                "speech" => 108,
                "center" => 120,
                "left" => 130,
                "right" => 140,
                _ => 100,
            };
            let raw = WizardFrameRaw {
                nose_area: 5000,
                raw_angle: angle,
                mask_area: if angle.is_some() { 200 } else { 0 },
                mask: vec![false; 56 * 56],
            };
            let image = vec![shade; 400 * 400];
            let header = make_header(index + 1, 1, (index + 1) * 50_000_000);
            if let Some(samples) = wizard.feed(&image, &raw, &header).unwrap() {
                return (wizard, samples);
            }
        }
        panic!("Wizard did not finish");
    }

    #[test]
    fn geometry_endpoints_deadzone_and_mirrored_order() {
        let mirrored = Calibration {
            left: 1.11,
            center: 0.66,
            right: 0.33,
            min_area: 100,
        };
        for cal in [DEFAULT_CALIBRATION, mirrored] {
            assert!(valid_calibration(&cal));
            for (angle, exp_left, exp_right) in [
                (cal.left, 1.0, 0.0),
                (cal.center, 0.0, 0.0),
                (cal.right, 0.0, 1.0),
            ] {
                let res = apply_calibration(Some(angle), 100, &cal, false, true);
                assert!((res.left - exp_left).abs() < 1e-9);
                assert!((res.right - exp_right).abs() < 1e-9);
                assert!(res.present);
            }
        }
    }

    #[test]
    fn hysteresis_requires_existing_positive_and_releases_below_60_percent() {
        assert!(!apply_calibration(Some(0.5), 60, &DEFAULT_CALIBRATION, false, true).present);
        let held = apply_calibration(Some(0.5), 60, &DEFAULT_CALIBRATION, true, true);
        assert!(held.present);
        assert!(held.size_held);
        assert!(!apply_calibration(Some(0.5), 60, &DEFAULT_CALIBRATION, true, false).present);
        assert!(!apply_calibration(Some(0.5), 59, &DEFAULT_CALIBRATION, true, true).present);
        assert!(!apply_calibration(None, 60, &DEFAULT_CALIBRATION, true, true).present);

        // JSON passthrough when raw_angle is absent (e.g. MouthLink)
        let mouthlink_raw = json!({"present": true, "out": 0.8});
        assert_eq!(
            apply_calibration_json(&mouthlink_raw, &DEFAULT_CALIBRATION, false, true),
            mouthlink_raw
        );
    }

    #[test]
    fn calibration_threshold_and_direction_validation() {
        let cal = calibration_from_samples(&synthetic_samples()).unwrap();
        assert_eq!(
            cal,
            Calibration {
                min_area: 98,
                ..DEFAULT_CALIBRATION
            }
        );
        let bad: Vec<CalibrationSample> = synthetic_samples()
            .into_iter()
            .map(|mut s| {
                if s.label >= 3 {
                    s.raw_angle = Some(0.66);
                }
                s
            })
            .collect();
        let err = calibration_from_samples(&bad).unwrap_err();
        assert!(err.contains("not separated"));
        let err = calibration_from_samples(&[]).unwrap_err();
        assert!(err.contains("Not enough visible"));
    }

    #[test]
    fn complete_calibration_and_retraining_guides() {
        for (mode, expected) in [
            ("calibrate", [30usize, 30, 24, 40, 40]),
            ("retrain", [180, 120, 60, 60, 60]),
        ] {
            let (wizard, rows) = run_wizard(mode);
            assert!(wizard.finished);
            let counts: Vec<usize> = (1u8..=5)
                .map(|lbl| rows.iter().filter(|s| s.label == lbl).count())
                .collect();
            assert_eq!(counts, expected);
            let unique_seqs: std::collections::HashSet<&str> =
                rows.iter().map(|s| s.source_seq.as_str()).collect();
            assert_eq!(unique_seqs.len(), rows.len());
            assert!(valid_calibration(&calibration_from_samples(&rows).unwrap()));
        }
    }

    #[test]
    fn duplicate_timestamps_fast_inference_and_session_change() {
        let mut wizard = Wizard::new("calibrate").unwrap();
        let image = vec![0u8; 400 * 400];
        let raw_ok = WizardFrameRaw {
            nose_area: 5000,
            ..Default::default()
        };
        for index in 0..5u64 {
            wizard
                .feed(
                    &image,
                    &raw_ok,
                    &make_header(index, 1, 100_000_000 + index * 10_000_000),
                )
                .unwrap();
        }
        assert_eq!(wizard.count, 1);
        wizard
            .feed(&image, &raw_ok, &make_header(8, 1, 500_000_000))
            .unwrap();
        assert_eq!(wizard.count, 1);
        wizard
            .feed(
                &image,
                &WizardFrameRaw {
                    nose_area: 0,
                    ..Default::default()
                },
                &make_header(9, 1, 550_000_000),
            )
            .unwrap();
        assert_eq!(wizard.count, 0);
        assert_eq!(wizard.hint, "fit_low");

        let err = wizard
            .feed(&image, &raw_ok, &make_header(1, 2, 600_000_000))
            .unwrap_err();
        assert!(err.contains("Camera session changed"));
    }

    struct AcceptingTrainer {
        ckpt: String,
    }
    impl PersonalTrainer for AcceptingTrainer {
        fn train(
            &self,
            _samples: Vec<CalibrationSample>,
            _base_checkpoint: &str,
            _current_checkpoint: &str,
            _job_dir: &Path,
            _cancelled: Arc<AtomicBool>,
            on_progress: Box<dyn Fn(f64) + Send + Sync>,
        ) -> Result<TrainOutcome, String> {
            on_progress(0.5);
            Ok(TrainOutcome {
                accepted: true,
                metrics: Some(json!({"test_only": true})),
                checkpoint: Some(self.ckpt.clone()),
                calibration: Some(DEFAULT_CALIBRATION),
            })
        }
    }

    struct BlockingTrainer {
        ckpt: String,
        entered: Mutex<Option<mpsc::Sender<()>>>,
        release: Mutex<mpsc::Receiver<()>>,
    }
    impl PersonalTrainer for BlockingTrainer {
        fn train(
            &self,
            _samples: Vec<CalibrationSample>,
            _base_checkpoint: &str,
            _current_checkpoint: &str,
            _job_dir: &Path,
            _cancelled: Arc<AtomicBool>,
            _on_progress: Box<dyn Fn(f64) + Send + Sync>,
        ) -> Result<TrainOutcome, String> {
            if let Some(tx) = self.entered.lock().unwrap().take() {
                let _ = tx.send(());
            }
            let _ = self.release.lock().unwrap().recv();
            Ok(TrainOutcome {
                accepted: true,
                metrics: Some(json!({"late": true})),
                checkpoint: Some(self.ckpt.clone()),
                calibration: Some(DEFAULT_CALIBRATION),
            })
        }
    }

    #[test]
    fn profile_persistence_restore_and_transactional_commit() {
        let dir = tempfile::tempdir().unwrap();
        let changes = Arc::new(Mutex::new(Vec::<bool>::new()));
        let changes_cb = Arc::clone(&changes);
        let root = dir.path().join("personalization");
        let personal = Personalization::new(
            root.clone(),
            Some(Arc::new(move |head_changed| {
                changes_cb.lock().unwrap().push(head_changed);
            })),
            None,
        );
        let standard = dir.path().join("standard.pt");
        fs::write(&standard, b"unchanged standard").unwrap();
        personal.set_checkpoint(&standard.to_string_lossy());

        // Retraining requires literal consent
        let err = personal.begin("retrain", false).unwrap_err();
        assert!(err.contains("Confirm local image"));
        assert!(!root.exists());

        // Persist and reload from another Personalization instance
        let mut prof = personal.profile();
        prof.head = Some("personal.pt".to_string());
        prof.calibration.min_area = 120;
        personal.persist(prof.clone()).unwrap();

        let other = Personalization::new(root.clone(), None, None);
        other.set_checkpoint(&standard.to_string_lossy());
        assert_eq!(other.profile(), prof);

        // Restore preserves standard file and archives jobs
        let archive = root.join("jobs").join("kept");
        fs::create_dir_all(&archive).unwrap();
        assert!(personal.restore().unwrap());
        assert_eq!(personal.profile().head, None);
        assert_eq!(personal.profile().calibration, DEFAULT_CALIBRATION);
        assert_eq!(fs::read(&standard).unwrap(), b"unchanged standard");
        assert!(archive.exists());
        assert_eq!(*changes.lock().unwrap(), vec![true]);

        // Switching to another checkpoint ignores old profile
        let another = dir.path().join("another.pt");
        other.set_checkpoint(&another.to_string_lossy());
        assert_eq!(other.profile().head, None);
    }

    #[test]
    fn training_publishes_pending_and_respects_cancel_and_restore() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("personalization");
        let personal_ckpt = dir.path().join("personal.pt").to_string_lossy().into_owned();
        let trainer: Arc<dyn PersonalTrainer> = Arc::new(AcceptingTrainer {
            ckpt: personal_ckpt.clone(),
        });
        let personal = Personalization::new(root.clone(), None, Some(trainer));
        personal.set_checkpoint(&dir.path().join("standard.pt").to_string_lossy());

        // 1. Accept & commit pending
        personal.begin("retrain", true).unwrap();
        personal.submit_retrain_samples(synthetic_samples()).unwrap();
        personal.join_worker();
        assert_eq!(personal.status().state, "applying");
        assert!(personal.profile().head.is_none());
        assert!(!root.join("active.json").exists());
        let pending = personal.pending().unwrap();
        assert!(personal.commit_pending(&pending).unwrap());
        assert_eq!(personal.status().state, "accepted");
        assert_eq!(personal.profile().head.as_deref(), Some(personal_ckpt.as_str()));

        // 2. Cancelled pending cannot commit
        personal.restore().unwrap();
        personal.begin("retrain", true).unwrap();
        personal.submit_retrain_samples(synthetic_samples()).unwrap();
        personal.join_worker();
        let pending2 = personal.pending().unwrap();
        personal.cancel();
        assert!(!personal.commit_pending(&pending2).unwrap());
        assert!(personal.profile().head.is_none());

        // 3. Restore while training prevents late reactivation
        let (entered_tx, entered_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        personal.set_trainer(Some(Arc::new(BlockingTrainer {
            ckpt: personal_ckpt.clone(),
            entered: Mutex::new(Some(entered_tx)),
            release: Mutex::new(release_rx),
        })));
        personal.begin("retrain", true).unwrap();
        personal.submit_retrain_samples(synthetic_samples()).unwrap();
        entered_rx.recv().unwrap();
        personal.restore().unwrap();
        let _ = release_tx.send(());
        personal.join_worker();
        assert_eq!(personal.status().state, "restored");
        assert!(personal.pending().is_none());
        assert!(personal.profile().head.is_none());
    }
}
