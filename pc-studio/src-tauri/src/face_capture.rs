//! Consented local JPEG/stock ledgers, exact asynchronous joins and reviewed labels.
//!
//! `FrameHeader` has no service generation. Bind its capture session explicitly before
//! delivery (or provide `service_generation` in a header JSON value). Never infer it
//! from the latest stock record.

use crate::face_dataset::{
    append_json, f32_slice_to_json, json_value, write_json, write_samples, KeyAtom,
};
use crate::face_presets::{capture_preset, CaptureContinuation, CapturePreset, DEFAULT_PRESET};
use crate::face_prompts::PromptStep;
use crate::face_schema::{channel_index, CHANNELS, SCHEMA, STOCK_CHANNELS};
use crate::stock::StockRecord;
use crate::wire::{monotonic_ns, FrameHeader, END, JPEG};
use chrono::Utc;
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};
use std::fs::{self, OpenOptions};
use std::path::PathBuf;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Instant;
use uuid::Uuid;

pub type ClockFn = Arc<dyn Fn() -> f64 + Send + Sync>;
pub type MonoNsClockFn = Arc<dyn Fn() -> i64 + Send + Sync>;

static MONO_S_EPOCH: OnceLock<Instant> = OnceLock::new();

pub fn default_monotonic_seconds() -> f64 {
    let epoch = MONO_S_EPOCH.get_or_init(Instant::now);
    Instant::now().duration_since(*epoch).as_secs_f64()
}

pub fn default_clock() -> ClockFn {
    Arc::new(default_monotonic_seconds)
}

pub fn default_mono_ns_clock() -> MonoNsClockFn {
    Arc::new(monotonic_ns)
}

fn finite_f64(value: &Value) -> Option<f64> {
    let Value::Number(n) = value else {
        return None;
    };
    let f = n.as_f64()?;
    if f.is_finite() {
        Some(f)
    } else {
        None
    }
}

fn value_as_i128(value: Option<&Value>) -> Option<i128> {
    let Value::Number(n) = value? else {
        return None;
    };
    if let Some(i) = n.as_i64() {
        Some(i as i128)
    } else {
        n.as_u64().map(|u| u as i128)
    }
}

fn value_as_u64(value: Option<&Value>) -> Option<u64> {
    let Value::Number(n) = value? else {
        return None;
    };
    n.as_u64()
        .or_else(|| n.as_i64().and_then(|i| u64::try_from(i).ok()))
}

fn is_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
    }
}

pub fn frame_header_to_value(header: &FrameHeader) -> Value {
    json!({
        "payload_bytes": header.payload_bytes,
        "flags": header.flags,
        "session_id": header.session_id,
        "source_seq": header.source_seq,
        "sensor_ns": header.sensor_ns,
        "vendor_ns": header.vendor_ns,
        "copy_mono_ns": header.copy_mono_ns,
        "encode_begin_ns": header.encode_begin_ns,
        "encode_end_ns": header.encode_end_ns,
        "emit_mono_ns": header.emit_mono_ns,
        "eye0_sensor_ns": header.eye0_sensor_ns,
        "eye1_sensor_ns": header.eye1_sensor_ns,
        "mouth_sensor_ns": header.mouth_sensor_ns,
        "raw_identity": header.raw_identity,
        "width": header.width,
        "height": header.height,
        "raw_crc32": header.raw_crc32,
        "quality": header.quality,
    })
}

pub fn stock_record_to_value(record: &StockRecord) -> Value {
    json!({
        "capture_session_id": record.capture_session_id,
        "service_generation": record.service_generation,
        "source_seq": record.source_seq,
        "mouth_sensor_ns": record.mouth_sensor_ns,
        "stock_input_ns": record.stock_input_ns,
        "vendor_ns": record.vendor_ns,
        "raw_identity": record.raw_identity,
        "produced_mono_ns": record.produced_mono_ns,
        "label_seq": record.label_seq,
        "classification": record.classification,
        "raw33": f32_slice_to_json(&record.raw33),
        "instant33": f32_slice_to_json(&record.instant33),
        "final33": f32_slice_to_json(&record.final33),
        "quality2": f32_slice_to_json(&record.quality2),
        "instant_preclamp33": f32_slice_to_json(&record.instant_preclamp33),
        "valid": record.valid,
        "association_valid": record.association_valid,
        "warmup": record.warmup,
    })
}

/// Explicit prepare/hold/review/confirm; source continuity is not pose evidence.
pub struct MouthGuide {
    pub preset: CapturePreset,
    pub steps: Vec<PromptStep>,
    pub index: usize,
    pub phase: String,
    pub frames: Vec<Value>,
    pub completed: BTreeSet<usize>,
    pub skipped: BTreeSet<usize>,
    pub automatic: bool,
    clock: ClockFn,
    pub prepare_seconds: f64,
    pub paused: bool,
    pub prepare_started: Option<f64>,
    pub frame: String,
    pub displayed_phase: Option<String>,
    pub displayed_at: Option<f64>,
}

impl MouthGuide {
    pub fn new(
        preset_id: &str,
        automatic: bool,
        continuation: Option<&CaptureContinuation>,
        step_index: usize,
        prepare_seconds: f64,
    ) -> Result<Self, String> {
        Self::with_clock(
            preset_id,
            automatic,
            default_clock(),
            continuation,
            step_index,
            prepare_seconds,
        )
    }

    pub fn with_clock(
        preset_id: &str,
        automatic: bool,
        clock: ClockFn,
        continuation: Option<&CaptureContinuation>,
        step_index: usize,
        prepare_seconds: f64,
    ) -> Result<Self, String> {
        let preset = match continuation {
            Some(c) => c.preset.clone(),
            None => capture_preset(preset_id)?,
        };
        let steps = preset.steps.clone();
        if step_index >= steps.len() {
            return Err("Choose a step in the selected capture plan".to_string());
        }
        if !prepare_seconds.is_finite() || !(3.0..=30.0).contains(&prepare_seconds) {
            return Err("Preparation time must be 3–30 seconds".to_string());
        }
        Ok(Self {
            preset,
            steps,
            index: step_index,
            phase: "prepare".to_string(),
            frames: Vec::new(),
            completed: BTreeSet::new(),
            skipped: BTreeSet::new(),
            automatic,
            clock,
            prepare_seconds,
            paused: false,
            prepare_started: None,
            frame: Uuid::new_v4().simple().to_string(),
            displayed_phase: None,
            displayed_at: None,
        })
    }

    pub fn set_clock(&mut self, clock: ClockFn) {
        self.clock = clock;
    }

    fn validate_index(&self, index: usize) -> Result<(), String> {
        if index >= self.steps.len() {
            return Err("Choose a step in the selected capture plan".to_string());
        }
        Ok(())
    }

    pub fn display(&mut self, frame: &str, phase: &str, visible: bool, media_ready: bool) -> bool {
        if frame != self.frame || phase != self.phase {
            return false;
        }
        self.displayed_phase = Some(phase.to_string());
        self.displayed_at = if visible && media_ready {
            Some((self.clock)())
        } else {
            None
        };
        true
    }

    pub fn display_ready(&self) -> bool {
        self.displayed_phase.as_deref() == Some(self.phase.as_str())
            && self.displayed_at.is_some_and(|t| {
                let dt = (self.clock)() - t;
                (0.0..1.0).contains(&dt)
            })
    }

    fn next_display(&mut self) {
        self.frame = Uuid::new_v4().simple().to_string();
        self.prepare_started = None;
        self.displayed_at = None;
        self.displayed_phase = None;
    }

    pub fn step(&self) -> Option<&PromptStep> {
        self.steps.get(self.index)
    }

    pub fn feed(&mut self, sample: &Value) -> bool {
        if self.paused {
            return false;
        }
        if self.automatic {
            if !self.display_ready() {
                self.frames.clear();
                self.prepare_started = None;
                return false;
            }
            if self.phase == "prepare" {
                let now = (self.clock)();
                let started = *self.prepare_started.get_or_insert(now);
                if now - started >= self.prepare_seconds {
                    let _ = self.advance("ready", None);
                }
                return false;
            }
        }
        if self.phase != "hold" {
            return false;
        }
        if let Some(previous) = self.frames.last() {
            let cur_ns = value_as_i128(sample.get("mouth_sensor_ns")).unwrap_or(0);
            let prev_ns = value_as_i128(previous.get("mouth_sensor_ns")).unwrap_or(0);
            let gap = cur_ns - prev_ns;
            if sample.get("capture_session_id") != previous.get("capture_session_id")
                || sample.get("service_generation") != previous.get("service_generation")
                || gap <= 0
                || gap > 250_000_000
            {
                self.frames.clear();
            }
        }
        self.frames.push(sample.clone());
        true
    }

    pub fn candidates(&self) -> Vec<Value> {
        if self.frames.len() < 2 {
            return Vec::new();
        }
        let Some(step) = self.step() else {
            return Vec::new();
        };
        let start = value_as_i128(self.frames[0].get("mouth_sensor_ns")).unwrap_or(0);
        let end = value_as_i128(self.frames.last().unwrap().get("mouth_sensor_ns")).unwrap_or(0);
        let required = ((step.reaction_s + step.hold_s + step.tail_s) * 1e9) as i128;
        if end - start < required {
            return Vec::new();
        }
        let lo = start + (step.reaction_s * 1e9) as i128;
        let hi = end - (step.tail_s * 1e9) as i128;
        self.frames
            .iter()
            .filter(|row| {
                let ns = value_as_i128(row.get("mouth_sensor_ns")).unwrap_or(0);
                ns >= lo && ns <= hi
            })
            .cloned()
            .collect()
    }

    pub fn advance(&mut self, action: &str, step_index: Option<usize>) -> Result<(), String> {
        if self.step().is_none() {
            return Err("Guide is finished".to_string());
        }
        if matches!(action, "pause" | "resume") {
            self.paused = action == "pause";
            self.phase = "prepare".to_string();
            self.frames.clear();
            self.next_display();
            return Ok(());
        }
        if action == "select_step" {
            let idx = step_index
                .ok_or_else(|| "Choose a step in the selected capture plan".to_string())?;
            self.validate_index(idx)?;
            self.index = idx;
            self.phase = "prepare".to_string();
            self.frames.clear();
            self.next_display();
        } else if self.paused {
            return Err("Resume the guide before continuing the current action".to_string());
        } else if action == "skip" {
            if !self.completed.contains(&self.index) {
                self.skipped.insert(self.index);
            }
            self.index += 1;
            self.phase = "prepare".to_string();
            self.frames.clear();
            self.next_display();
        } else if action == "ready" && self.phase == "prepare" {
            self.phase = "hold".to_string();
            self.frames.clear();
            self.next_display();
        } else if action == "finish" && self.phase == "hold" {
            if self.candidates().is_empty() {
                return Err(
                    "Not enough continuous new frames; keep holding or skip".to_string(),
                );
            }
            self.phase = "review".to_string();
        } else if action == "confirm" && self.phase == "review" {
            // This confirms task completion only. It does not approve image labels.
            self.completed.insert(self.index);
            self.skipped.remove(&self.index);
            self.index += 1;
            self.phase = "prepare".to_string();
            self.frames.clear();
            self.next_display();
        } else {
            return Err(
                "Expected ready, finish, confirm or skip for the current phase".to_string(),
            );
        }
        if self.step().is_none() {
            self.phase = "finished".to_string();
        }
        Ok(())
    }

    pub fn status(&self) -> Value {
        let span = if self.frames.len() < 2 {
            0.0
        } else {
            let first = value_as_i128(self.frames[0].get("mouth_sensor_ns")).unwrap_or(0);
            let last =
                value_as_i128(self.frames.last().unwrap().get("mouth_sensor_ns")).unwrap_or(0);
            (last - first) as f64 / 1e9
        };
        let remaining = if self.phase == "prepare" {
            if let Some(started) = self.prepare_started {
                (self.prepare_seconds - ((self.clock)() - started)).max(0.0)
            } else {
                self.prepare_seconds
            }
        } else if self.phase == "hold" {
            if let Some(step) = self.step() {
                (step.reaction_s + step.hold_s + step.tail_s - span).max(0.0)
            } else {
                0.0
            }
        } else {
            0.0
        };
        let completed_ids: Vec<&str> = self
            .completed
            .iter()
            .map(|&i| self.steps[i].id.as_str())
            .collect();
        let skipped_ids: Vec<&str> = self
            .skipped
            .iter()
            .map(|&i| self.steps[i].id.as_str())
            .collect();
        let completed_indices: Vec<usize> = self.completed.iter().copied().collect();
        let skipped_indices: Vec<usize> = self.skipped.iter().copied().collect();
        let ready_to_finish =
            self.step().is_some() && self.phase == "hold" && !self.candidates().is_empty();
        let progress = (self.completed.len() + self.skipped.len()) as f64 / self.steps.len() as f64;
        json!({
            "step": self.step(),
            "phase": self.phase,
            "index": self.index,
            "total": self.steps.len(),
            "automatic": self.automatic,
            "paused": self.paused,
            "frame": self.frame,
            "display_ready": self.display_ready(),
            "remaining_seconds": remaining,
            "preset_id": self.preset.id,
            "preset_name": self.preset.name,
            "continuous_seconds": span,
            "frames": self.frames.len(),
            "ready_to_finish": ready_to_finish,
            "completed": completed_ids,
            "skipped": skipped_ids,
            "completed_indices": completed_indices,
            "skipped_indices": skipped_indices,
            "progress": progress,
        })
    }
}

#[derive(Debug, Clone)]
pub struct BeginOptions {
    pub person_id: String,
    pub confirmed: bool,
    pub guide: bool,
    pub role: Option<String>,
    pub evaluation_only: Option<bool>,
    pub preset_id: String,
    pub automatic: bool,
    pub continuation: Option<CaptureContinuation>,
    pub step_index: usize,
    pub prepare_seconds: f64,
}

impl Default for BeginOptions {
    fn default() -> Self {
        Self {
            person_id: String::new(),
            confirmed: false,
            guide: true,
            role: None,
            evaluation_only: None,
            preset_id: DEFAULT_PRESET.to_string(),
            automatic: false,
            continuation: None,
            step_index: 0,
            prepare_seconds: 3.0,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
struct CaptureBinding {
    service_generation: Value,
    evidence: Value,
}

#[derive(Debug, Clone)]
struct StockEntry {
    stock_id: String,
    record: Value,
    timed_out: bool,
    matched: HashSet<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DeadlineKind {
    Image,
    Stock,
}

type PrefixKey = (KeyAtom, KeyAtom);
type JoinKey = (KeyAtom, KeyAtom, KeyAtom);

struct RecordingInner {
    root: PathBuf,
    state: String,
    path: Option<PathBuf>,
    session_id: Option<String>,
    person_id: Option<String>,
    label_stage: String,
    guide: Option<MouthGuide>,
    manifest: Option<Value>,
    images: Vec<Value>,
    sample_index: HashMap<String, usize>,
    stocks: Vec<StockEntry>,
    bindings: BTreeMap<u64, CaptureBinding>,
    counts: BTreeMap<String, u64>,
    image_keys: HashMap<JoinKey, usize>,
    stock_fingerprints: HashMap<String, String>,
    seq_stocks: HashMap<JoinKey, Vec<usize>>,
    sensor_stocks: HashMap<JoinKey, Vec<usize>>,
    seq_images: HashMap<JoinKey, Vec<usize>>,
    sensor_images: HashMap<JoinKey, Vec<usize>>,
    deadlines: VecDeque<(i64, DeadlineKind, usize)>,
    mono_ns_clock: MonoNsClockFn,
}

impl RecordingInner {
    fn new(root: PathBuf, mono_ns_clock: MonoNsClockFn) -> Self {
        Self {
            root,
            state: "idle".to_string(),
            path: None,
            session_id: None,
            person_id: None,
            label_stage: "raw_only".to_string(),
            guide: None,
            manifest: None,
            images: Vec::new(),
            sample_index: HashMap::new(),
            stocks: Vec::new(),
            bindings: BTreeMap::new(),
            counts: BTreeMap::new(),
            image_keys: HashMap::new(),
            stock_fingerprints: HashMap::new(),
            seq_stocks: HashMap::new(),
            sensor_stocks: HashMap::new(),
            seq_images: HashMap::new(),
            sensor_images: HashMap::new(),
            deadlines: VecDeque::new(),
            mono_ns_clock,
        }
    }

    fn event(&mut self, kind: &str, fields: Map<String, Value>) -> Result<(), String> {
        *self.counts.entry(kind.to_string()).or_insert(0) += 1;
        let mut obj = Map::new();
        obj.insert("kind".to_string(), json!(kind));
        obj.insert("pc_mono_ns".to_string(), json!((self.mono_ns_clock)()));
        for (k, v) in fields {
            obj.insert(k, v);
        }
        let events_path = self
            .path
            .as_ref()
            .ok_or_else(|| "Recording path is not initialized".to_string())?
            .join("events.jsonl");
        append_json(&events_path, &Value::Object(obj))
    }

    fn prefix(row: &Value) -> PrefixKey {
        (
            row.get("capture_session_id")
                .map(KeyAtom::from_value)
                .unwrap_or(KeyAtom::Null),
            row.get("service_generation")
                .map(KeyAtom::from_value)
                .unwrap_or(KeyAtom::Null),
        )
    }

    fn expire(&mut self) -> Result<(), String> {
        let now = (self.mono_ns_clock)();
        let timeout = (WholeMouthRecording::PAIR_TIMEOUT_S * 1e9) as i64;
        while let Some(&(ts, _, _)) = self.deadlines.front() {
            if now - ts <= timeout {
                break;
            }
            let (_, kind, idx) = self.deadlines.pop_front().unwrap();
            match kind {
                DeadlineKind::Image => {
                    let missing = self.images[idx]
                        .get("stock_id")
                        .is_none_or(|v| v.is_null());
                    if missing {
                        self.images[idx]["timed_out"] = json!(true);
                        let sid = self.images[idx]["sample_id"]
                            .as_str()
                            .unwrap_or("")
                            .to_string();
                        let mut fields = Map::new();
                        fields.insert("sample_id".to_string(), json!(sid));
                        self.event("image_pair_timeout", fields)?;
                    }
                }
                DeadlineKind::Stock => {
                    let missing = self.stocks[idx].matched.is_empty();
                    if missing {
                        self.stocks[idx].timed_out = true;
                        let sid = self.stocks[idx].stock_id.clone();
                        let mut fields = Map::new();
                        fields.insert("stock_id".to_string(), json!(sid));
                        self.event("stock_pair_timeout", fields)?;
                    }
                }
            }
        }
        Ok(())
    }

    fn matches(image: &Value, stock: &StockEntry) -> bool {
        let r = &stock.record;
        if image.get("service_generation").is_none_or(|v| v.is_null())
            || r.get("service_generation").is_none_or(|v| v.is_null())
        {
            return false;
        }
        let img_prefix = Self::prefix(image);
        let stock_prefix = (
            r.get("capture_session_id")
                .map(KeyAtom::from_value)
                .unwrap_or(KeyAtom::Null),
            r.get("service_generation")
                .map(KeyAtom::from_value)
                .unwrap_or(KeyAtom::Null),
        );
        if img_prefix != stock_prefix {
            return false;
        }
        if !r.get("identity_evidence").is_some_and(is_truthy) {
            return false;
        }
        // Equal time is only a join key when the input-to-camera sensor mapping is proven.
        let stock_seq = r.get("source_seq").filter(|v| !v.is_null());
        if stock_seq.is_none() {
            if r.get("sensor_identity_proven") != Some(&Value::Bool(true))
                || r.get("mouth_sensor_ns").map(KeyAtom::from_value)
                    != image.get("mouth_sensor_ns").map(KeyAtom::from_value)
            {
                return false;
            }
        } else if r.get("identity_proven") != Some(&Value::Bool(true))
            || stock_seq.map(KeyAtom::from_value)
                != image.get("source_seq").map(KeyAtom::from_value)
            || r.get("mouth_sensor_ns").map(KeyAtom::from_value)
                != image.get("mouth_sensor_ns").map(KeyAtom::from_value)
        {
            return false;
        }
        if let Some(src_id) = r.get("source_identity").filter(|v| !v.is_null()) {
            if image.get("source_identity").map(KeyAtom::from_value)
                != Some(KeyAtom::from_value(src_id))
            {
                return false;
            }
        }
        if let Some(vendor) = r.get("vendor_ns").filter(|v| !v.is_null()) {
            let img_vendor = image
                .get("header")
                .and_then(|h| h.get("vendor_ns"))
                .map(KeyAtom::from_value);
            if img_vendor != Some(KeyAtom::from_value(vendor)) {
                return false;
            }
        }
        true
    }

    fn apply_stock_targets(label_stage: &str, image: &mut Value, record: &Value) {
        // Raw has no established control-scale mapping. Preserve it, do not clip it.
        if label_stage != "instant" {
            return;
        }
        if record.get("mouth_valid") != Some(&Value::Bool(true))
            || record.get("warmup") != Some(&Value::Bool(false))
        {
            return;
        }
        let Some(quality) = record.get("quality2").and_then(|v| v.as_array()) else {
            return;
        };
        if quality.len() != 2 {
            return;
        }
        let (Some(q0), Some(_q1)) = (finite_f64(&quality[0]), finite_f64(&quality[1])) else {
            return;
        };
        if q0 <= 0.1 {
            return;
        }
        let Some(values) = record.get("instant33").and_then(|v| v.as_array()) else {
            return;
        };
        if values.len() != STOCK_CHANNELS.len() {
            return;
        }
        for (&channel, val) in STOCK_CHANNELS.iter().zip(values.iter()) {
            if let Some(f) = finite_f64(val) {
                if (0.0..=1.0).contains(&f) {
                    if channel == "TongueOut" && f == 0.0 {
                        // Thresholded zero is not a verified negative tongue example.
                        continue;
                    }
                    if let Some(idx) = channel_index(channel) {
                        image["targets"][idx] = json!(f);
                        image["target_sources"][idx] = json!("stock_instant_compatibility");
                    }
                }
            }
        }
    }

    fn refresh(&mut self, image_idx: usize) -> Result<(), String> {
        let prefix = Self::prefix(&self.images[image_idx]);
        let seq_atom = self.images[image_idx]
            .get("source_seq")
            .map(KeyAtom::from_value)
            .unwrap_or(KeyAtom::Null);
        let sensor_atom = self.images[image_idx]
            .get("mouth_sensor_ns")
            .map(KeyAtom::from_value)
            .unwrap_or(KeyAtom::Null);

        let seq_key = (prefix.0.clone(), prefix.1.clone(), seq_atom);
        let sensor_key = (prefix.0, prefix.1, sensor_atom);

        let mut candidates: Vec<usize> = Vec::new();
        if let Some(v) = self.seq_stocks.get(&seq_key) {
            candidates.extend(v.iter().copied());
        }
        if let Some(v) = self.sensor_stocks.get(&sensor_key) {
            candidates.extend(v.iter().copied());
        }

        let sample_id = self.images[image_idx]["sample_id"]
            .as_str()
            .unwrap_or("")
            .to_string();

        let matches: Vec<usize> = candidates
            .iter()
            .copied()
            .filter(|&s_idx| Self::matches(&self.images[image_idx], &self.stocks[s_idx]))
            .collect();

        for &s_idx in &candidates {
            self.stocks[s_idx].matched.remove(&sample_id);
        }

        let old_status = self.images[image_idx]["pairing_status"]
            .as_str()
            .unwrap_or("")
            .to_string();
        let old_stock_id = self.images[image_idx]
            .get("stock_id")
            .cloned()
            .unwrap_or(Value::Null);

        let sensor_image_count = self
            .sensor_images
            .get(&sensor_key)
            .map(|v| v.len())
            .unwrap_or(0);

        let img = &mut self.images[image_idx];
        img["targets"] = Value::Array(vec![Value::Null; CHANNELS.len()]);
        img["target_sources"] = Value::Array(vec![Value::Null; CHANNELS.len()]);
        img["stock_id"] = Value::Null;
        if let Some(obj) = img.as_object_mut() {
            obj.remove("stock");
        }

        let identity_conflict = img
            .get("identity_conflict")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let img_timed_out = img
            .get("timed_out")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let has_generation = img
            .get("service_generation")
            .is_some_and(|v| !v.is_null());

        if identity_conflict {
            img["pairing_status"] = json!("image_conflict");
        } else if matches.len() > 1
            || (matches.len() == 1
                && self.stocks[matches[0]]
                    .record
                    .get("source_seq")
                    .is_none_or(|v| v.is_null())
                && sensor_image_count != 1)
        {
            img["pairing_status"] = json!("ambiguous");
        } else if matches.len() == 1 {
            let s_idx = matches[0];
            self.stocks[s_idx].matched.insert(sample_id.clone());
            let stock_id = self.stocks[s_idx].stock_id.clone();
            let stock_rec = self.stocks[s_idx].record.clone();
            let stock_timed_out = self.stocks[s_idx].timed_out;
            let no_seq = stock_rec.get("source_seq").is_none_or(|v| v.is_null());

            let img = &mut self.images[image_idx];
            img["stock_id"] = json!(stock_id);
            img["stock"] = stock_rec.clone();
            img["pairing_status"] = json!(if img_timed_out || stock_timed_out {
                "paired_late"
            } else {
                "paired"
            });
            img["pairing_method"] = json!(if no_seq {
                "verified_sensor"
            } else {
                "verified_source"
            });
            Self::apply_stock_targets(&self.label_stage, img, &stock_rec);
        } else if !has_generation {
            img["pairing_status"] = json!("unproven_generation");
        } else {
            img["pairing_status"] = json!(if !candidates.is_empty() {
                "unproven_identity"
            } else if self.state != "recording" {
                "missing_stock"
            } else {
                "pending"
            });
        }

        let img = &mut self.images[image_idx];
        let pairing_status = img["pairing_status"].as_str().unwrap_or("").to_string();
        if !matches!(pairing_status.as_str(), "ambiguous" | "image_conflict") {
            let review_labels = img
                .get("review_labels")
                .and_then(|v| v.as_object())
                .cloned()
                .unwrap_or_default();
            for (channel, annotation) in review_labels {
                if let Some(ch_idx) = channel_index(&channel) {
                    img["targets"][ch_idx] =
                        annotation.get("value").cloned().unwrap_or(Value::Null);
                    img["target_sources"][ch_idx] =
                        annotation.get("source").cloned().unwrap_or(Value::Null);
                }
            }
        }
        if img
            .get("review_excluded")
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
        {
            img["targets"] = Value::Array(vec![Value::Null; CHANNELS.len()]);
            img["target_sources"] = Value::Array(vec![Value::Null; CHANNELS.len()]);
        }
        let trainable = img["targets"]
            .as_array()
            .is_some_and(|arr| arr.iter().any(|v| !v.is_null()));
        img["trainable"] = json!(trainable);

        let new_status = img["pairing_status"].as_str().unwrap_or("").to_string();
        let new_stock_id = img.get("stock_id").cloned().unwrap_or(Value::Null);
        if old_status != new_status || old_stock_id != new_stock_id {
            let mut fields = Map::new();
            fields.insert("sample_id".to_string(), json!(sample_id));
            fields.insert("status".to_string(), json!(new_status));
            fields.insert("stock_id".to_string(), new_stock_id);
            self.event("pairing", fields)?;
        }
        Ok(())
    }

    fn advance_inner(
        &mut self,
        action: &str,
        step_index: Option<usize>,
        guide_frame: Option<&str>,
    ) -> Result<Value, String> {
        if self.state != "recording" || self.guide.is_none() {
            return Err("No active guide".to_string());
        }
        if let Some(expected_frame) = guide_frame {
            if self.guide.as_ref().unwrap().frame != expected_frame {
                return Err("The current step has changed".to_string());
            }
        }
        let eval_only = self
            .manifest
            .as_ref()
            .and_then(|m| m.get("evaluation_only"))
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        if eval_only && action == "select_step" {
            return Err("The sealed recording must follow its frozen order".to_string());
        }

        let (previous, previous_index, candidates) = {
            let g = self.guide.as_ref().unwrap();
            let prev = g.step().map(|s| s.id.clone());
            let prev_idx = g.index;
            let cands = if action == "finish" && g.phase == "hold" {
                g.candidates()
            } else {
                Vec::new()
            };
            (prev, prev_idx, cands)
        };

        self.guide.as_mut().unwrap().advance(action, step_index)?;

        if !candidates.is_empty() {
            let labels = self
                .guide
                .as_ref()
                .and_then(|g| g.step())
                .map(|s| serde_json::to_value(&s.labels).unwrap_or_else(|_| json!({})))
                .unwrap_or_else(|| json!({}));
            for cand in &candidates {
                if let Some(sid) = cand.get("sample_id").and_then(|v| v.as_str()) {
                    if let Some(&idx) = self.sample_index.get(sid) {
                        self.images[idx]["prompt_candidate"] = json!({
                            "prompt_id": previous,
                            "step_index": previous_index,
                            "labels": labels,
                            "reviewed": false,
                        });
                    }
                }
            }
        }

        let (next_step_index, automatic, status) = {
            let g = self.guide.as_ref().unwrap();
            (g.index, g.automatic, g.status())
        };
        let mut fields = Map::new();
        fields.insert("action".to_string(), json!(action));
        fields.insert(
            "prompt_id".to_string(),
            previous.map(Value::String).unwrap_or(Value::Null),
        );
        fields.insert("step_index".to_string(), json!(previous_index));
        fields.insert("next_step_index".to_string(), json!(next_step_index));
        fields.insert("automatic".to_string(), json!(automatic));
        self.event("guide_action", fields)?;
        Ok(status)
    }

    fn export(&mut self) -> Result<(), String> {
        let path = self
            .path
            .as_ref()
            .ok_or_else(|| "Recording path is not initialized".to_string())?
            .clone();
        write_samples(&path.join("samples.jsonl"), &self.images)?;
        let trainable_count = self
            .images
            .iter()
            .filter(|r| r.get("trainable").and_then(|v| v.as_bool()).unwrap_or(false))
            .count();
        let mut bindings_map = Map::new();
        for (sid, b) in &self.bindings {
            bindings_map.insert(
                sid.to_string(),
                json!({
                    "service_generation": b.service_generation,
                    "evidence": b.evidence,
                }),
            );
        }
        let manifest = self
            .manifest
            .as_mut()
            .ok_or_else(|| "Manifest is not initialized".to_string())?;
        if let Some(obj) = manifest.as_object_mut() {
            obj.insert("state".to_string(), json!(self.state));
            obj.insert("samples_complete".to_string(), json!(true));
            obj.insert("counts".to_string(), json!(self.counts));
            obj.insert("samples".to_string(), json!(self.images.len()));
            obj.insert("trainable".to_string(), json!(trainable_count));
            obj.insert(
                "capture_bindings".to_string(),
                Value::Object(bindings_map),
            );
        }
        let manifest_snapshot = self.manifest.clone().unwrap();
        write_json(&path.join("manifest.json"), &manifest_snapshot)
    }

    fn stop_inner(&mut self, target_state: &str) -> Result<Value, String> {
        if self.state != "recording" {
            return Ok(self.status_snapshot());
        }
        self.state = target_state.to_string();
        self.expire()?;

        for idx in 0..self.images.len() {
            self.refresh(idx)?;
            if self.images[idx].get("stock_id").is_none_or(|v| v.is_null()) {
                let sid = self.images[idx]["sample_id"]
                    .as_str()
                    .unwrap_or("")
                    .to_string();
                let st = self.images[idx]["pairing_status"]
                    .as_str()
                    .unwrap_or("")
                    .to_string();
                let mut fields = Map::new();
                fields.insert("sample_id".to_string(), json!(sid));
                fields.insert("status".to_string(), json!(st));
                self.event("image_without_stock", fields)?;
            }
        }

        let used: HashSet<String> = self
            .images
            .iter()
            .filter_map(|r| r.get("stock_id").and_then(|v| v.as_str()).map(String::from))
            .collect();
        let unused_stocks: Vec<String> = self
            .stocks
            .iter()
            .filter(|s| !used.contains(&s.stock_id))
            .map(|s| s.stock_id.clone())
            .collect();
        for stock_id in unused_stocks {
            let mut fields = Map::new();
            fields.insert("stock_id".to_string(), json!(stock_id));
            self.event("stock_without_image", fields)?;
        }

        let mut groups: HashMap<PrefixKey, (Value, Value, BTreeSet<u64>)> = HashMap::new();
        for row in &self.images {
            let key = Self::prefix(row);
            let entry = groups.entry(key).or_insert_with(|| {
                (
                    row.get("capture_session_id")
                        .cloned()
                        .unwrap_or(Value::Null),
                    row.get("service_generation")
                        .cloned()
                        .unwrap_or(Value::Null),
                    BTreeSet::new(),
                )
            });
            if let Some(seq) = value_as_u64(row.get("source_seq")) {
                entry.2.insert(seq);
            }
        }
        let mut gap_events: Vec<Map<String, Value>> = Vec::new();
        for (_, (capture, generation, seqs)) in groups {
            let ordered: Vec<u64> = seqs.into_iter().collect();
            for pair in ordered.windows(2) {
                let (first, last) = (pair[0], pair[1]);
                if last > first + 1 {
                    let mut fields = Map::new();
                    fields.insert("capture_session_id".to_string(), capture.clone());
                    fields.insert("service_generation".to_string(), generation.clone());
                    fields.insert("first_missing".to_string(), json!(first + 1));
                    fields.insert("last_missing".to_string(), json!(last - 1));
                    fields.insert("count".to_string(), json!(last - first - 1));
                    gap_events.push(fields);
                }
            }
        }
        for fields in gap_events {
            self.event("source_gap", fields)?;
        }

        self.event(target_state, Map::new())?;
        self.export()?;
        Ok(self.status_snapshot())
    }

    fn status_snapshot(&self) -> Value {
        let mut states: BTreeMap<String, u64> = BTreeMap::new();
        let mut trainable = 0u64;
        let mut pending_reviews = 0u64;
        for row in &self.images {
            if let Some(st) = row.get("pairing_status").and_then(|v| v.as_str()) {
                *states.entry(st.to_string()).or_insert(0) += 1;
            }
            if row
                .get("trainable")
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
            {
                trainable += 1;
            }
            if let Some(pc) = row.get("prompt_candidate").and_then(|v| v.as_object()) {
                if pc.get("reviewed").and_then(|v| v.as_bool()) != Some(true) {
                    pending_reviews += 1;
                }
            }
        }
        let evaluation_only = self.path.is_some()
            && self
                .manifest
                .as_ref()
                .and_then(|m| m.get("evaluation_only"))
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
        json!({
            "state": self.state,
            "schema": SCHEMA,
            "session_id": self.session_id,
            "person_id": self.person_id,
            "evaluation_only": evaluation_only,
            "path": self.path.as_ref().map(|p| p.to_string_lossy().into_owned()),
            "label_stage": self.label_stage,
            "images": self.images.len(),
            "stocks": self.stocks.len(),
            "counts": self.counts,
            "pairing": states,
            "trainable": trainable,
            "pending_reviews": pending_reviews,
            "guide": self.guide.as_ref().map(|g| g.status()),
        })
    }
}

pub struct WholeMouthRecording {
    inner: Mutex<RecordingInner>,
}

impl WholeMouthRecording {
    pub const PAIR_TIMEOUT_S: f64 = 2.0;

    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self::with_mono_ns_clock(root, default_mono_ns_clock())
    }

    pub fn with_mono_ns_clock(root: impl Into<PathBuf>, clock: MonoNsClockFn) -> Self {
        Self {
            inner: Mutex::new(RecordingInner::new(root.into(), clock)),
        }
    }

    pub fn set_mono_ns_clock(&self, clock: MonoNsClockFn) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.mono_ns_clock = clock;
        }
    }

    pub fn set_guide_clock(&self, clock: ClockFn) {
        if let Ok(mut inner) = self.inner.lock() {
            if let Some(guide) = inner.guide.as_mut() {
                guide.set_clock(clock);
            }
        }
    }

    pub fn session_id(&self) -> Option<String> {
        self.inner
            .lock()
            .ok()
            .and_then(|i| i.session_id.clone())
    }

    pub fn path(&self) -> Option<PathBuf> {
        self.inner.lock().ok().and_then(|i| i.path.clone())
    }

    pub fn state(&self) -> String {
        self.inner
            .lock()
            .map(|i| i.state.clone())
            .unwrap_or_else(|_| "idle".to_string())
    }

    pub fn manifest(&self) -> Option<Value> {
        self.inner.lock().ok().and_then(|i| i.manifest.clone())
    }

    pub fn guide_frame(&self) -> Option<String> {
        self.inner
            .lock()
            .ok()
            .and_then(|i| i.guide.as_ref().map(|g| g.frame.clone()))
    }

    pub fn guide_phase(&self) -> Option<String> {
        self.inner
            .lock()
            .ok()
            .and_then(|i| i.guide.as_ref().map(|g| g.phase.clone()))
    }

    pub fn guide_index(&self) -> Option<usize> {
        self.inner
            .lock()
            .ok()
            .and_then(|i| i.guide.as_ref().map(|g| g.index))
    }

    pub fn guide_automatic(&self) -> bool {
        self.inner
            .lock()
            .ok()
            .and_then(|i| i.guide.as_ref().map(|g| g.automatic))
            .unwrap_or(false)
    }

    pub fn set_label_stage(&self, stage: &str) -> Result<(), String> {
        let mut inner = self.inner.lock().map_err(|e| e.to_string())?;
        if inner.state == "recording" {
            return Err("Choose label stage before recording".to_string());
        }
        if !matches!(stage, "raw_only" | "instant") {
            return Err(
                "Choose raw_only or explicit instant compatibility labels".to_string(),
            );
        }
        inner.label_stage = stage.to_string();
        Ok(())
    }

    pub fn begin(&self, opts: BeginOptions) -> Result<Value, String> {
        let mut inner = self.inner.lock().map_err(|e| e.to_string())?;
        // Check consent before mkdir, stop, clearing state or any other write.
        if !opts.confirmed {
            return Err(
                "Confirm local image/stock recording and local training first".to_string(),
            );
        }
        if opts.person_id.trim().is_empty() {
            return Err("person_id must be a nonempty anonymous string".to_string());
        }
        if !matches!(opts.role.as_deref(), None | Some("A") | Some("B") | Some("C")) {
            return Err(
                "Recording role must be A, B, C or omitted for ordinary recording".to_string(),
            );
        }
        let is_role_c = opts.role.as_deref() == Some("C");
        if let Some(ev) = opts.evaluation_only {
            if ev != is_role_c {
                return Err(
                    "evaluation_only must match the explicit recording role C".to_string(),
                );
            }
        }
        if is_role_c && opts.step_index != 0 {
            return Err("The sealed recording must follow its frozen order".to_string());
        }
        if inner.state == "recording" {
            return Err("Stop the active recording before beginning another".to_string());
        }

        let selected_guide = if opts.guide {
            Some(MouthGuide::new(
                &opts.preset_id,
                opts.automatic,
                opts.continuation.as_ref(),
                opts.step_index,
                opts.prepare_seconds,
            )?)
        } else {
            None
        };

        let session_id = Uuid::new_v4().simple().to_string();
        let path = inner.root.join(&session_id);
        fs::create_dir_all(&inner.root)
            .map_err(|e| format!("Failed to create recording root: {e}"))?;
        fs::create_dir(&path).map_err(|e| format!("Failed to create recording dir: {e}"))?;
        fs::create_dir(path.join("images"))
            .map_err(|e| format!("Failed to create images dir: {e}"))?;
        for name in [
            "images.jsonl",
            "stock.jsonl",
            "events.jsonl",
            "reviews.jsonl",
            "samples.jsonl",
        ] {
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(path.join(name))
                .map_err(|e| format!("Failed to create {name}: {e}"))?;
        }

        inner.person_id = Some(opts.person_id.clone());
        inner.session_id = Some(session_id.clone());
        inner.path = Some(path.clone());
        inner.images.clear();
        inner.sample_index.clear();
        inner.stocks.clear();
        inner.bindings.clear();
        inner.image_keys.clear();
        inner.stock_fingerprints.clear();
        inner.seq_stocks.clear();
        inner.sensor_stocks.clear();
        inner.seq_images.clear();
        inner.sensor_images.clear();
        inner.deadlines.clear();
        inner.counts.clear();

        let capture_plan = selected_guide
            .as_ref()
            .map(|g| serde_json::to_value(&g.preset).unwrap_or(Value::Null))
            .unwrap_or(Value::Null);
        let automatic_guidance = selected_guide.as_ref().is_some_and(|g| g.automatic);
        let has_guide = selected_guide.is_some();
        inner.guide = selected_guide;

        let now = Utc::now();
        let day = now.format("%Y-%m-%d").to_string();
        let created_utc = now.to_rfc3339();
        let fit_id = opts
            .continuation
            .as_ref()
            .map(|c| c.fit_id.clone())
            .unwrap_or_else(|| session_id.clone());
        let source_group_id = opts
            .continuation
            .as_ref()
            .map(|c| c.source_group_id.clone())
            .unwrap_or_else(|| format!("recording:{session_id}:fit:{session_id}"));

        let mut manifest = json!({
            "schema": SCHEMA,
            "channels": CHANNELS.to_vec(),
            "stock_channels": STOCK_CHANNELS.to_vec(),
            "person_id": opts.person_id,
            "session_id": session_id,
            "fit_id": fit_id,
            "source_group_id": source_group_id,
            "day": day,
            "created_utc": created_utc,
            "state": "recording",
            "label_stage": inner.label_stage,
            "samples_complete": false,
            "observability_role": opts.role,
            "evaluation_only": is_role_c,
            "capture_plan": capture_plan,
            "automatic_guidance": automatic_guidance,
            "consent": {
                "local_recording": true,
                "local_training": !is_role_c,
                "upload": false,
            },
            "nonfinite_raw_encoding": "NaN/Infinity/-Infinity strings",
            "split_fields": ["person_id", "session_id", "fit_id", "source_group_id"],
            "pairing": "exact verified identity only; no nearest/latest joins",
            "prompt_labels": "unreviewed proposals; not observed ground truth",
        });

        if has_guide {
            manifest["guide_start_step"] = json!(opts.step_index);
            manifest["guide_prepare_seconds"] = json!(opts.prepare_seconds);
        }
        if let Some(cont) = opts.continuation {
            manifest["continuation_of"] = json!(cont.session_id);
        }

        write_json(&path.join("manifest.json"), &manifest)?;
        inner.manifest = Some(manifest);
        inner.state = "recording".to_string();
        Ok(inner.status_snapshot())
    }

    /// Bind known transport metadata, never a guessed/current/latest generation.
    pub fn bind_capture(
        &self,
        capture_session_id: u64,
        service_generation: Value,
        evidence: Value,
    ) -> Result<(), String> {
        let mut inner = self.inner.lock().map_err(|e| e.to_string())?;
        if inner.state != "recording" {
            return Err("Begin a recording first".to_string());
        }
        if service_generation.is_null() || !is_truthy(&evidence) {
            return Err(
                "Binding needs capture_session_id, service_generation and evidence".to_string(),
            );
        }
        let new_binding = CaptureBinding {
            service_generation: service_generation.clone(),
            evidence: evidence.clone(),
        };
        if let Some(old) = inner.bindings.get(&capture_session_id) {
            if old.service_generation != service_generation {
                return Err(
                    "Capture session cannot span service generations; use a new capture session"
                        .to_string(),
                );
            }
            if *old == new_binding {
                return Ok(());
            }
        }
        inner.bindings.insert(capture_session_id, new_binding);
        let mut fields = Map::new();
        fields.insert("capture_session_id".to_string(), json!(capture_session_id));
        fields.insert("service_generation".to_string(), service_generation);
        fields.insert("evidence".to_string(), evidence);
        inner.event("capture_bound", fields)?;
        // Frames already written without a generation stay unproven, never backfilled.
        Ok(())
    }

    pub fn on_frame(&self, header: &FrameHeader, jpeg: &[u8]) -> Result<bool, String> {
        self.on_frame_value(frame_header_to_value(header), jpeg)
    }

    pub fn on_frame_value(&self, header: Value, jpeg: &[u8]) -> Result<bool, String> {
        let mut inner = self.inner.lock().map_err(|e| e.to_string())?;
        if inner.state != "recording" {
            return Ok(false);
        }
        inner.expire()?;

        let flags = value_as_u64(header.get("flags")).unwrap_or(0) as u32;
        let session_id = value_as_u64(header.get("session_id")).unwrap_or(0);
        let payload_bytes = value_as_u64(header.get("payload_bytes")).unwrap_or(0) as usize;
        let source_seq = value_as_u64(header.get("source_seq")).unwrap_or(0);
        let mouth_sensor_ns = value_as_u64(header.get("mouth_sensor_ns")).unwrap_or(0);

        if (flags & END) != 0 {
            let mut fields = Map::new();
            fields.insert("capture_session_id".to_string(), json!(session_id));
            inner.event("capture_end", fields)?;
            return Ok(false);
        }
        if (flags & JPEG) == 0 || jpeg.len() != payload_bytes {
            let mut fields = Map::new();
            fields.insert("header".to_string(), header);
            fields.insert(
                "reason".to_string(),
                json!("Expected original JPEG payload with exact length"),
            );
            inner.event("invalid_image", fields)?;
            return Ok(false);
        }

        let binding_gen = inner
            .bindings
            .get(&session_id)
            .map(|b| b.service_generation.clone());
        let generation = header
            .get("service_generation")
            .cloned()
            .unwrap_or_else(|| binding_gen.clone().unwrap_or(Value::Null));
        if let Some(ref bgen) = binding_gen {
            if generation != *bgen {
                return Err("Frame generation contradicts capture binding".to_string());
            }
        }

        let key: JoinKey = (
            KeyAtom::Int(session_id as i128),
            KeyAtom::from_value(&generation),
            KeyAtom::Int(source_seq as i128),
        );

        let rec_path = inner.path.clone().unwrap();

        if let Some(&orig_idx) = inner.image_keys.get(&key) {
            let (orig_sample_id, orig_rel_image, headers_match) = {
                let original = &inner.images[orig_idx];
                let orig_h = &original["header"];
                let same_fields = [
                    "sensor_ns",
                    "vendor_ns",
                    "mouth_sensor_ns",
                    "raw_identity",
                    "raw_crc32",
                ]
                .iter()
                .all(|&k| header.get(k) == orig_h.get(k));
                (
                    original["sample_id"].as_str().unwrap_or("").to_string(),
                    original["image"].as_str().unwrap_or("").to_string(),
                    same_fields,
                )
            };
            // Same identity with different bytes is a conflict, not a retransmission.
            let bytes_match = fs::read(rec_path.join(&orig_rel_image))
                .map(|b| b.as_slice() == jpeg)
                .unwrap_or(false);
            let duplicate = headers_match && bytes_match;
            let conflict_image = if !duplicate {
                inner.images[orig_idx]["identity_conflict"] = json!(true);
                inner.refresh(orig_idx)?;
                let conflict_num = *inner.counts.get("image_conflict").unwrap_or(&0);
                let rel = format!("images/{orig_sample_id}_conflict{conflict_num:06}.jpg");
                fs::write(rec_path.join(&rel), jpeg)
                    .map_err(|e| format!("Failed to write conflict image: {e}"))?;
                Some(rel)
            } else {
                None
            };
            append_json(
                &rec_path.join("images.jsonl"),
                &json!({
                    "header": header,
                    "duplicate_of": orig_sample_id,
                    "conflict": !duplicate,
                    "image": conflict_image,
                }),
            )?;
            let mut fields = Map::new();
            fields.insert("sample_id".to_string(), json!(orig_sample_id));
            inner.event(
                if duplicate {
                    "image_duplicate"
                } else {
                    "image_conflict"
                },
                fields,
            )?;
            return Ok(false);
        }

        let idx = inner.images.len();
        let sample_id = format!("f{idx:09}");
        let image_rel = format!("images/{sample_id}.jpg");
        fs::write(rec_path.join(&image_rel), jpeg)
            .map_err(|e| format!("Failed to write image {image_rel}: {e}"))?;

        let manifest = inner.manifest.as_ref().unwrap();
        let pairing_status = if !generation.is_null() {
            "pending"
        } else {
            "unproven_generation"
        };
        let received_mono_ns = (inner.mono_ns_clock)();
        let row = json!({
            "sample_id": sample_id,
            "image": image_rel,
            "header": header,
            "person_id": inner.person_id,
            "session_id": inner.session_id,
            "fit_id": manifest["fit_id"],
            "day": manifest["day"],
            "capture_session_id": session_id,
            "source_seq": source_seq,
            "mouth_sensor_ns": mouth_sensor_ns,
            "source_identity": header.get("raw_identity").cloned().unwrap_or(Value::Null),
            "service_generation": generation,
            "source_group_id": manifest["source_group_id"],
            "label_stage": inner.label_stage,
            "targets": vec![Value::Null; CHANNELS.len()],
            "target_sources": vec![Value::Null; CHANNELS.len()],
            "observability_role": manifest["observability_role"],
            "evaluation_only": manifest["evaluation_only"],
            "pairing_status": pairing_status,
            "trainable": false,
            "review_labels": {},
            "stock_id": Value::Null,
            "received_mono_ns": received_mono_ns,
            "timed_out": false,
            "identity_conflict": false,
        });

        let prefix = RecordingInner::prefix(&row);
        let seq_join = (
            prefix.0.clone(),
            prefix.1.clone(),
            KeyAtom::Int(source_seq as i128),
        );
        let sensor_join = (
            prefix.0,
            prefix.1,
            KeyAtom::Int(mouth_sensor_ns as i128),
        );

        let mut ledger_row = row.as_object().unwrap().clone();
        ledger_row.remove("targets");
        ledger_row.remove("target_sources");
        ledger_row.remove("review_labels");
        append_json(&rec_path.join("images.jsonl"), &Value::Object(ledger_row))?;

        inner.images.push(row);
        inner.sample_index.insert(sample_id, idx);
        inner.image_keys.insert(key, idx);
        inner.seq_images.entry(seq_join).or_default().push(idx);
        inner
            .sensor_images
            .entry(sensor_join.clone())
            .or_default()
            .push(idx);
        inner
            .deadlines
            .push_back((received_mono_ns, DeadlineKind::Image, idx));
        *inner.counts.entry("images".to_string()).or_insert(0) += 1;

        let row_snapshot = inner.images[idx].clone();
        let feed_result = if let Some(guide) = inner.guide.as_mut() {
            if guide.feed(&row_snapshot) {
                let step_id = guide.step().map(|s| s.id.clone());
                let auto_finish = guide.automatic && !guide.candidates().is_empty();
                Some((step_id, auto_finish))
            } else {
                None
            }
        } else {
            None
        };

        if let Some((step_id, auto_finish)) = feed_result {
            if let Some(sid) = step_id {
                inner.images[idx]["prompt_id"] = json!(sid);
            }
            inner.images[idx]["prompt_phase"] = json!("hold");
            if auto_finish {
                inner.advance_inner("finish", None, None)?;
                inner.advance_inner("confirm", None, None)?;
            }
        }

        inner.refresh(idx)?;
        let other_indices: Vec<usize> = inner
            .sensor_images
            .get(&sensor_join)
            .map(|v| v.iter().copied().filter(|&o| o != idx).collect())
            .unwrap_or_default();
        for other_idx in other_indices {
            inner.refresh(other_idx)?;
        }
        inner.expire()?;
        Ok(true)
    }

    pub fn on_stock(&self, record: &StockRecord) -> Result<bool, String> {
        self.on_stock_value(stock_record_to_value(record))
    }

    pub fn on_stock_value(&self, original_in: Value) -> Result<bool, String> {
        let mut inner = self.inner.lock().map_err(|e| e.to_string())?;
        if inner.state != "recording" {
            return Ok(false);
        }
        inner.expire()?;

        let original = json_value(&original_in);
        let mut record = original.clone();

        // PSL1 ASSOCIATED is set only by the native exact-input association path.
        // Preserve original field names as well as the canonical join fields.
        if let Some(orig_obj) = original.as_object() {
            if let Some(assoc) = orig_obj.get("association_valid") {
                let proven = assoc == &Value::Bool(true);
                record["identity_proven"] = json!(proven);
                record["source_identity"] =
                    orig_obj.get("raw_identity").cloned().unwrap_or(Value::Null);
                record["mouth_valid"] = orig_obj.get("valid").cloned().unwrap_or(Value::Null);
                record["identity_evidence"] = json!(if proven {
                    "PSL1 ASSOCIATED: verified native input/camera mapping"
                } else {
                    ""
                });
            }
        }

        // serde_json::Map is backed by BTreeMap (keys sorted alphabetically), matching sort_keys=True.
        let fingerprint = serde_json::to_string(&original)
            .map_err(|e| format!("Failed to fingerprint stock record: {e}"))?;
        let duplicate = inner.stock_fingerprints.get(&fingerprint).cloned();
        let arrivals = *inner.counts.get("stock_arrivals").unwrap_or(&0);
        let stock_id = format!("s{arrivals:09}");
        *inner
            .counts
            .entry("stock_arrivals".to_string())
            .or_insert(0) += 1;

        let rec_path = inner.path.clone().unwrap();
        append_json(
            &rec_path.join("stock.jsonl"),
            &json!({
                "stock_id": stock_id,
                "record": original,
                "duplicate_of": duplicate,
            }),
        )?;

        if let Some(dup_of) = duplicate {
            let mut fields = Map::new();
            fields.insert("stock_id".to_string(), json!(stock_id));
            fields.insert("duplicate_of".to_string(), json!(dup_of));
            inner.event("stock_duplicate", fields)?;
            return Ok(false);
        }

        inner
            .stock_fingerprints
            .insert(fingerprint, stock_id.clone());
        let received_mono_ns = (inner.mono_ns_clock)();
        let s_idx = inner.stocks.len();
        inner.stocks.push(StockEntry {
            stock_id,
            record: record.clone(),
            timed_out: false,
            matched: HashSet::new(),
        });
        inner
            .deadlines
            .push_back((received_mono_ns, DeadlineKind::Stock, s_idx));
        *inner.counts.entry("stocks".to_string()).or_insert(0) += 1;

        let prefix = (
            record
                .get("capture_session_id")
                .map(KeyAtom::from_value)
                .unwrap_or(KeyAtom::Null),
            record
                .get("service_generation")
                .map(KeyAtom::from_value)
                .unwrap_or(KeyAtom::Null),
        );
        let matching_images: Vec<usize> =
            if let Some(seq) = record.get("source_seq").filter(|v| !v.is_null()) {
                let key = (prefix.0, prefix.1, KeyAtom::from_value(seq));
                inner.seq_stocks.entry(key.clone()).or_default().push(s_idx);
                inner.seq_images.get(&key).cloned().unwrap_or_default()
            } else {
                let sensor = record
                    .get("mouth_sensor_ns")
                    .map(KeyAtom::from_value)
                    .unwrap_or(KeyAtom::Null);
                let key = (prefix.0, prefix.1, sensor);
                inner
                    .sensor_stocks
                    .entry(key.clone())
                    .or_default()
                    .push(s_idx);
                inner.sensor_images.get(&key).cloned().unwrap_or_default()
            };

        for img_idx in matching_images {
            inner.refresh(img_idx)?;
        }
        inner.expire()?;
        Ok(true)
    }

    pub fn advance(
        &self,
        action: &str,
        step_index: Option<usize>,
        guide_frame: Option<&str>,
    ) -> Result<Value, String> {
        let mut inner = self.inner.lock().map_err(|e| e.to_string())?;
        inner.advance_inner(action, step_index, guide_frame)
    }

    pub fn display(
        &self,
        session_id: &str,
        frame: &str,
        phase: &str,
        visible: bool,
        media_ready: bool,
    ) -> Value {
        let Ok(mut inner) = self.inner.lock() else {
            return json!({ "accepted": false });
        };
        if inner.state != "recording"
            || inner.session_id.as_deref() != Some(session_id)
            || inner.guide.is_none()
        {
            return json!({ "accepted": false });
        }
        let accepted = inner
            .guide
            .as_mut()
            .unwrap()
            .display(frame, phase, visible, media_ready);
        json!({ "accepted": accepted })
    }

    /// Explicit image review only. No unspecified channel is implicitly zero.
    pub fn review(
        &self,
        sample_ids: &[String],
        labels: &Map<String, Value>,
        confirmed: bool,
        reviewer: &str,
        observation: &str,
    ) -> Result<Value, String> {
        let mut inner = self.inner.lock().map_err(|e| e.to_string())?;
        if !confirmed || reviewer.is_empty() {
            return Err("Confirm image review and provide a reviewer identifier".to_string());
        }
        if !matches!(observation, "visible" | "uncertain" | "occluded" | "no_mouth") {
            return Err("Unknown observation".to_string());
        }
        if !matches!(inner.state.as_str(), "recording" | "stopped" | "cancelled") {
            return Err("No recording to review".to_string());
        }

        for (channel, value) in labels {
            if channel_index(channel).is_none() {
                return Err(
                    "Review labels must name schema channels and finite [0,1] values or null"
                        .to_string(),
                );
            }
            if !value.is_null() {
                let Some(f) = finite_f64(value) else {
                    return Err(
                        "Review labels must name schema channels and finite [0,1] values or null"
                            .to_string(),
                    );
                };
                if !(0.0..=1.0).contains(&f) {
                    return Err(
                        "Review labels must name schema channels and finite [0,1] values or null"
                            .to_string(),
                    );
                }
            }
        }
        if observation != "visible" && labels.values().any(|v| !v.is_null()) {
            return Err("Unobservable mouth labels must remain unknown".to_string());
        }
        if sample_ids.is_empty()
            || sample_ids
                .iter()
                .any(|sid| !inner.sample_index.contains_key(sid))
        {
            return Err("Select existing sample_ids".to_string());
        }

        let review_id = Uuid::new_v4().simple().to_string();
        let rec_path = inner.path.clone().unwrap();
        append_json(
            &rec_path.join("reviews.jsonl"),
            &json!({
                "review_id": review_id,
                "reviewer": reviewer,
                "observation": observation,
                "sample_ids": sample_ids,
                "labels": labels,
                "confirmed": true,
            }),
        )?;

        let indices: Vec<usize> = sample_ids
            .iter()
            .map(|sid| inner.sample_index[sid])
            .collect();

        for idx in indices {
            let row = &mut inner.images[idx];
            row["observation"] = json!(observation);
            if observation != "visible" {
                // Occlusion/uncertainty excludes stock as well, not just prompt labels.
                row["review_excluded"] = json!(true);
                row["review_labels"] = json!({});
            } else {
                row["review_excluded"] = json!(false);
                if !row["review_labels"].is_object() {
                    row["review_labels"] = json!({});
                }
                let rl = row["review_labels"].as_object_mut().unwrap();
                for (channel, value) in labels {
                    if value.is_null() {
                        rl.insert(
                            channel.clone(),
                            json!({ "value": Value::Null, "source": Value::Null }),
                        );
                    } else {
                        let f = finite_f64(value).unwrap();
                        rl.insert(
                            channel.clone(),
                            json!({
                                "value": f,
                                "source": format!("human_review:{review_id}"),
                            }),
                        );
                    }
                }
            }
            if let Some(pc) = row
                .get_mut("prompt_candidate")
                .and_then(|v| v.as_object_mut())
            {
                pc.insert("reviewed".to_string(), json!(true));
            }
            inner.refresh(idx)?;
        }

        if inner.state != "recording" {
            inner.export()?;
        }
        Ok(json!({
            "review_id": review_id,
            "samples": sample_ids.len(),
        }))
    }

    pub fn samples(&self) -> Vec<Value> {
        self.inner
            .lock()
            .map(|i| i.images.clone())
            .unwrap_or_default()
    }

    fn project_review_row(row: &Value) -> Value {
        let mut out = Map::new();
        for key in [
            "sample_id",
            "image",
            "prompt_candidate",
            "review_labels",
            "observation",
            "capture_session_id",
            "source_seq",
            "mouth_sensor_ns",
            "pairing_status",
        ] {
            out.insert(
                key.to_string(),
                row.get(key).cloned().unwrap_or(Value::Null),
            );
        }
        Value::Object(out)
    }

    /// Small GUI page; image paths are relative to `status()["path"]`.
    pub fn review_candidates(&self, offset: usize, limit: usize) -> Value {
        let Ok(inner) = self.inner.lock() else {
            return json!({ "total": 0, "offset": offset, "samples": [] });
        };
        let rows: Vec<&Value> = inner
            .images
            .iter()
            .filter(|r| r.get("prompt_candidate").is_some_and(is_truthy))
            .collect();
        let total = rows.len();
        let slice = if offset >= total {
            &[][..]
        } else {
            let end = offset.saturating_add(limit).min(total);
            &rows[offset..end]
        };
        let projected: Vec<Value> = slice.iter().map(|r| Self::project_review_row(r)).collect();
        json!({
            "total": total,
            "offset": offset,
            "samples": projected,
        })
    }

    /// GUI review page supporting either prompt candidates or all frames, with `session_id`.
    pub fn review_samples(
        &self,
        offset: usize,
        limit: usize,
        all_frames: bool,
    ) -> Result<Value, String> {
        if !(1..=120).contains(&limit) {
            return Err("Review page must use offset >= 0 and limit 1..120".to_string());
        }
        let inner = self.inner.lock().map_err(|e| e.to_string())?;
        let rows: Vec<&Value> = if all_frames {
            inner.images.iter().collect()
        } else {
            inner
                .images
                .iter()
                .filter(|r| r.get("prompt_candidate").is_some_and(is_truthy))
                .collect()
        };
        let total = rows.len();
        let slice = if offset >= total {
            &[][..]
        } else {
            let end = offset.saturating_add(limit).min(total);
            &rows[offset..end]
        };
        let projected: Vec<Value> = slice.iter().map(|r| Self::project_review_row(r)).collect();
        Ok(json!({
            "total": total,
            "offset": offset,
            "samples": projected,
            "session_id": inner.session_id,
        }))
    }

    /// Safely read a review JPEG image contained inside the active recording's `images/` directory.
    pub fn review_image(&self, session_id: &str, sample_id: &str) -> Result<Vec<u8>, String> {
        let inner = self.inner.lock().map_err(|e| e.to_string())?;
        if inner.session_id.as_deref() != Some(session_id) || inner.path.is_none() {
            return Err("Image does not belong to the current local recording".to_string());
        }
        let &idx = inner
            .sample_index
            .get(sample_id)
            .ok_or_else(|| "Unknown local recording sample".to_string())?;
        let rel_image = inner.images[idx]
            .get("image")
            .and_then(|v| v.as_str())
            .ok_or_else(|| "Image must remain inside the current recording images directory".to_string())?;
        let rec_path = inner.path.as_ref().unwrap();
        let root_canon = fs::canonicalize(rec_path)
            .map_err(|_| "Image must remain inside the current recording images directory".to_string())?;
        let images_canon = fs::canonicalize(root_canon.join("images"))
            .map_err(|_| "Image must remain inside the current recording images directory".to_string())?;
        let target_canon = fs::canonicalize(root_canon.join(rel_image))
            .map_err(|_| "Image must remain inside the current recording images directory".to_string())?;
        if !target_canon.starts_with(&images_canon) || !target_canon.is_file() {
            return Err(
                "Image must remain inside the current recording images directory".to_string(),
            );
        }
        fs::read(&target_canon).map_err(|e| format!("Failed to read review image: {e}"))
    }

    pub fn stop(&self) -> Result<Value, String> {
        let mut inner = self.inner.lock().map_err(|e| e.to_string())?;
        inner.stop_inner("stopped")
    }

    /// Stop callbacks and keep consented evidence; never delete existing samples.
    pub fn cancel(&self) -> Result<Value, String> {
        let mut inner = self.inner.lock().map_err(|e| e.to_string())?;
        inner.stop_inner("cancelled")
    }

    pub fn status(&self) -> Value {
        let Ok(mut inner) = self.inner.lock() else {
            return json!({ "state": "idle", "schema": SCHEMA });
        };
        if inner.state == "recording" {
            let _ = inner.expire();
        }
        inner.status_snapshot()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::face_dataset::{assert_no_split_leakage, f64_to_json, load_dataset};
    use std::path::Path;
    use std::sync::atomic::{AtomicI64, Ordering};

    fn make_jpeg_bytes() -> Vec<u8> {
        use image::codecs::jpeg::JpegEncoder;
        use image::{ExtendedColorType, GrayImage};
        let img = GrayImage::from_pixel(400, 400, image::Luma([80u8]));
        let mut out = Vec::new();
        let mut enc = JpegEncoder::new_with_quality(&mut out, 95);
        enc.encode(img.as_raw(), 400, 400, ExtendedColorType::L8)
            .unwrap();
        out
    }

    fn jpeg_data() -> &'static [u8] {
        static DATA: OnceLock<Vec<u8>> = OnceLock::new();
        DATA.get_or_init(make_jpeg_bytes).as_slice()
    }

    fn make_header(seq: u64, session: u64, ns: Option<u64>) -> FrameHeader {
        let ns = ns.unwrap_or(seq * 11_111_111);
        FrameHeader {
            payload_bytes: jpeg_data().len() as u32,
            flags: JPEG,
            session_id: session,
            source_seq: seq,
            sensor_ns: ns,
            vendor_ns: ns + 3,
            copy_mono_ns: ns + 10,
            encode_begin_ns: ns + 11,
            encode_end_ns: ns + 12,
            emit_mono_ns: ns + 13,
            eye0_sensor_ns: ns,
            eye1_sensor_ns: ns,
            mouth_sensor_ns: ns,
            raw_identity: seq + 100,
            width: 400,
            height: 400,
            raw_crc32: 0,
            quality: 95,
        }
    }

    fn make_stock_value(h: &FrameHeader) -> Value {
        let mut raw33 = vec![0.1; 33];
        raw33[0] = -0.2;
        let mut instant33 = vec![0.2; 33];
        instant33[32] = 0.0;
        let final33 = vec![0.3; 33];
        json!({
            "capture_session_id": h.session_id,
            "service_generation": "bootA:pid1:birth1",
            "source_seq": h.source_seq,
            "mouth_sensor_ns": h.mouth_sensor_ns,
            "source_identity": h.raw_identity,
            "identity_proven": true,
            "identity_evidence": "offline fixture: exact input identity",
            "raw33": raw33,
            "instant33": instant33,
            "final33": final33,
            "quality2": [0.9, 0.1],
            "mouth_valid": true,
            "warmup": false,
        })
    }

    fn read_jsonl(path: &Path) -> Vec<Value> {
        fs::read_to_string(path)
            .unwrap()
            .lines()
            .filter(|l| !l.is_empty())
            .map(|l| serde_json::from_str(l).unwrap())
            .collect()
    }

    fn begin_default(cap: &WholeMouthRecording, stage: &str, guide: bool) {
        cap.set_label_stage(stage).unwrap();
        cap.begin(BeginOptions {
            person_id: "anonymous-1".into(),
            confirmed: true,
            guide,
            ..BeginOptions::default()
        })
        .unwrap();
        cap.bind_capture(8, json!("bootA:pid1:birth1"), json!("fixture transport session"))
            .unwrap();
    }

    #[test]
    fn no_consent_no_directory_or_callbacks() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("recordings");
        let cap = WholeMouthRecording::new(&root);
        assert!(cap
            .begin(BeginOptions {
                person_id: "p".into(),
                confirmed: false,
                ..BeginOptions::default()
            })
            .is_err());
        assert!(!root.exists());
        assert!(!cap.on_frame(&make_header(1, 8, None), jpeg_data()).unwrap());
        assert!(!cap
            .on_stock_value(make_stock_value(&make_header(1, 8, None)))
            .unwrap());
        assert!(!root.exists());
    }

    #[test]
    fn role_persisted_in_manifest_before_first_frame_and_in_rows() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("recordings");
        let cap = WholeMouthRecording::new(&root);
        for role in [None, Some("A"), Some("B"), Some("C")] {
            cap.begin(BeginOptions {
                person_id: "fixture".into(),
                confirmed: true,
                guide: false,
                role: role.map(String::from),
                ..BeginOptions::default()
            })
            .unwrap();
            let rec_path = cap.path().unwrap();
            let before: Value =
                serde_json::from_str(&fs::read_to_string(rec_path.join("manifest.json")).unwrap())
                    .unwrap();
            assert_eq!(before["observability_role"].as_str(), role);
            assert_eq!(before["evaluation_only"].as_bool(), Some(role == Some("C")));
            assert_eq!(fs::read_dir(rec_path.join("images")).unwrap().count(), 0);
            cap.bind_capture(8, json!("bootA:pid1:birth1"), json!("fixture"))
                .unwrap();
            cap.on_frame(&make_header(1, 8, None), jpeg_data()).unwrap();
            cap.stop().unwrap();
            let (_, rows) = load_dataset(&rec_path).unwrap();
            assert_eq!(rows[0]["observability_role"].as_str(), role);
            assert_eq!(rows[0]["evaluation_only"].as_bool(), Some(role == Some("C")));
        }
    }

    #[test]
    fn unknown_role_and_contradictory_evaluation_flag_rejected_before_mkdir() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("recordings");
        let cap = WholeMouthRecording::new(&root);
        for (role, ev) in [
            (Some("unknown"), None),
            (None, Some(true)),
            (Some("A"), Some(true)),
            (Some("C"), Some(false)),
        ] {
            assert!(cap
                .begin(BeginOptions {
                    person_id: "fixture".into(),
                    confirmed: true,
                    role: role.map(String::from),
                    evaluation_only: ev,
                    ..BeginOptions::default()
                })
                .is_err());
            assert!(!root.exists());
        }
    }

    #[test]
    fn raw_default_preserves_signed_values_no_targets() {
        let dir = tempfile::tempdir().unwrap();
        let cap = WholeMouthRecording::new(dir.path().join("recordings"));
        begin_default(&cap, "raw_only", false);
        let h = make_header(1, 8, None);
        cap.on_frame(&h, jpeg_data()).unwrap();
        cap.on_stock_value(make_stock_value(&h)).unwrap();
        cap.stop().unwrap();

        let rec_path = cap.path().unwrap();
        let (manifest, rows) = load_dataset(&rec_path).unwrap();
        assert_eq!(
            manifest["channels"].as_array().unwrap().len(),
            CHANNELS.len()
        );
        assert_eq!(rows[0]["pairing_status"], json!("paired"));
        assert_eq!(rows[0]["stock"]["raw33"][0], json!(-0.2));
        assert_eq!(rows[0]["targets"], Value::Array(vec![Value::Null; 50]));
        assert_eq!(rows[0]["trainable"], json!(false));
        assert_eq!(
            fs::read(rec_path.join(rows[0]["image"].as_str().unwrap())).unwrap(),
            jpeg_data()
        );
    }

    #[test]
    fn instant_compatibility_is_explicit_and_gated_zero_unknown() {
        let dir = tempfile::tempdir().unwrap();
        let cap = WholeMouthRecording::new(dir.path().join("recordings"));
        begin_default(&cap, "instant", false);
        let h = make_header(1, 8, None);
        cap.on_stock_value(make_stock_value(&h)).unwrap();
        cap.on_frame(&h, jpeg_data()).unwrap();
        let row = &cap.samples()[0];
        let targets = row["targets"].as_array().unwrap();
        for t in &targets[..32] {
            assert_eq!(*t, json!(0.2));
        }
        for t in &targets[32..] {
            assert!(t.is_null());
        }
        assert_eq!(
            row["target_sources"][0],
            json!("stock_instant_compatibility")
        );
        assert_eq!(row["label_stage"], json!("instant"));
    }

    #[test]
    fn out_of_order_join_and_images() {
        let dir = tempfile::tempdir().unwrap();
        let cap = WholeMouthRecording::new(dir.path().join("recordings"));
        begin_default(&cap, "instant", false);
        let h1 = make_header(1, 8, None);
        let h2 = make_header(2, 8, None);
        let mut s2 = make_stock_value(&h2);
        s2["instant33"] = json!(vec![0.8; 33]);
        cap.on_stock_value(s2).unwrap();
        cap.on_frame(&h1, jpeg_data()).unwrap();
        assert_eq!(
            cap.samples()[0]["targets"],
            Value::Array(vec![Value::Null; 50])
        );
        let mut s1 = make_stock_value(&h1);
        s1["instant33"] = json!(vec![0.1; 33]);
        cap.on_stock_value(s1).unwrap();
        cap.on_frame(&h2, jpeg_data()).unwrap();
        let first_targets: Vec<f64> = cap
            .samples()
            .iter()
            .map(|r| r["targets"][0].as_f64().unwrap())
            .collect();
        assert_eq!(first_targets, vec![0.1, 0.8]);

        // Out-of-order images and stock.
        cap.stop().unwrap();
        begin_default(&cap, "raw_only", false);
        for seq in [3, 1, 2] {
            cap.on_frame(&make_header(seq, 8, None), jpeg_data()).unwrap();
        }
        for seq in [2, 3, 1] {
            cap.on_stock_value(make_stock_value(&make_header(seq, 8, None)))
                .unwrap();
        }
        assert_eq!(cap.status()["pairing"], json!({ "paired": 3 }));
    }

    #[test]
    fn duplicates_and_conflicts_and_ambiguous_stock() {
        let dir = tempfile::tempdir().unwrap();
        let cap = WholeMouthRecording::new(dir.path().join("recordings"));
        begin_default(&cap, "raw_only", false);
        let h = make_header(1, 8, None);
        cap.on_frame(&h, jpeg_data()).unwrap();
        cap.on_frame(&h, jpeg_data()).unwrap();
        cap.on_stock_value(make_stock_value(&h)).unwrap();
        cap.on_stock_value(make_stock_value(&h)).unwrap();
        cap.stop().unwrap();
        let p = cap.path().unwrap();
        assert_eq!(read_jsonl(&p.join("images.jsonl")).len(), 2);
        assert_eq!(read_jsonl(&p.join("stock.jsonl")).len(), 2);
        assert_eq!(read_jsonl(&p.join("samples.jsonl")).len(), 1);
        assert_eq!(cap.status()["counts"]["image_duplicate"], json!(1));
        assert_eq!(cap.status()["counts"]["stock_duplicate"], json!(1));

        // Conflicting image invalidates pair and saves conflict file.
        begin_default(&cap, "instant", false);
        cap.on_stock_value(make_stock_value(&h)).unwrap();
        cap.on_frame(&h, jpeg_data()).unwrap();
        let mut conflict = h.clone();
        conflict.raw_identity = 9876;
        cap.on_frame(&conflict, jpeg_data()).unwrap();
        assert_eq!(cap.samples()[0]["trainable"], json!(false));
        assert_eq!(cap.samples()[0]["pairing_status"], json!("image_conflict"));
        let img_rows = read_jsonl(&cap.path().unwrap().join("images.jsonl"));
        let conflict_img = img_rows.last().unwrap()["image"].as_str().unwrap();
        assert!(conflict_img.contains("_conflict000000.jpg"));
        assert!(cap.path().unwrap().join(conflict_img).is_file());
        cap.stop().unwrap();

        // Conflicting stock invalidates ambiguous pair.
        begin_default(&cap, "instant", false);
        cap.on_frame(&h, jpeg_data()).unwrap();
        cap.on_stock_value(make_stock_value(&h)).unwrap();
        let mut s_diff = make_stock_value(&h);
        s_diff["raw33"] = json!(vec![0.8; 33]);
        cap.on_stock_value(s_diff).unwrap();
        assert_eq!(cap.samples()[0]["pairing_status"], json!("ambiguous"));
        assert_eq!(cap.samples()[0]["trainable"], json!(false));
    }

    #[test]
    fn service_generation_session_and_binding_invariants() {
        let dir = tempfile::tempdir().unwrap();
        let cap = WholeMouthRecording::new(dir.path().join("recordings"));
        let h = make_header(1, 8, None);

        // Service generation mismatch.
        begin_default(&cap, "instant", false);
        let mut s = make_stock_value(&h);
        s["service_generation"] = json!("bootA:pid2:birth2");
        cap.on_stock_value(s).unwrap();
        cap.on_frame(&h, jpeg_data()).unwrap();
        cap.stop().unwrap();
        assert_eq!(cap.samples()[0]["trainable"], json!(false));
        assert_eq!(cap.status()["counts"]["stock_without_image"], json!(1));

        // Capture session mismatch.
        begin_default(&cap, "raw_only", false);
        let mut s = make_stock_value(&h);
        s["capture_session_id"] = json!(9);
        cap.on_stock_value(s).unwrap();
        cap.on_frame(&h, jpeg_data()).unwrap();
        assert!(cap.samples()[0]["stock_id"].is_null());
        cap.stop().unwrap();

        // Missing generation never backfilled.
        cap.begin(BeginOptions {
            person_id: "p".into(),
            confirmed: true,
            guide: false,
            ..BeginOptions::default()
        })
        .unwrap();
        cap.on_frame(&h, jpeg_data()).unwrap();
        cap.on_stock_value(make_stock_value(&h)).unwrap();
        cap.bind_capture(8, json!("bootA:pid1:birth1"), json!("arrived after frame"))
            .unwrap();
        assert_eq!(
            cap.samples()[0]["pairing_status"],
            json!("unproven_generation")
        );
        assert!(cap.samples()[0]["service_generation"].is_null());

        // Binding cannot change generation.
        assert!(cap
            .bind_capture(8, json!("new_generation"), json!("fixture"))
            .is_err());
    }

    #[test]
    fn identity_exact_sensor_and_native_stock_record() {
        let dir = tempfile::tempdir().unwrap();
        let cap = WholeMouthRecording::new(dir.path().join("recordings"));
        let h = make_header(1, 8, None);

        // Reject various identity/sensor mismatches.
        let mutations: Vec<(&str, Value)> = vec![
            ("identity_proven", json!(false)),
            ("identity_evidence", json!("")),
            ("source_identity", json!(9999)),
            ("mouth_sensor_ns", json!(h.mouth_sensor_ns + 1)),
            ("vendor_ns", json!(h.vendor_ns + 1)),
        ];
        for (k, v) in mutations {
            begin_default(&cap, "instant", false);
            cap.on_frame(&h, jpeg_data()).unwrap();
            let mut s = make_stock_value(&h);
            s[k] = v;
            cap.on_stock_value(s).unwrap();
            assert_eq!(cap.samples()[0]["trainable"], json!(false));
            cap.stop().unwrap();
        }

        // Sensor-only join requires proof and rejects collision.
        begin_default(&cap, "instant", false);
        cap.on_frame(&h, jpeg_data()).unwrap();
        let mut s = make_stock_value(&h);
        s["source_seq"] = Value::Null;
        cap.on_stock_value(s.clone()).unwrap();
        assert!(cap.samples()[0]["stock_id"].is_null());
        s["sensor_identity_proven"] = json!(true);
        s["source_identity"] = Value::Null;
        cap.on_stock_value(s).unwrap();
        assert_eq!(cap.samples()[0]["pairing_method"], json!("verified_sensor"));
        assert_eq!(cap.samples()[0]["trainable"], json!(true));
        // Second image at same sensor_ns causes collision -> ambiguous.
        cap.on_frame(&make_header(2, 8, Some(h.mouth_sensor_ns)), jpeg_data())
            .unwrap();
        assert!(cap
            .samples()
            .iter()
            .all(|r| r["pairing_status"] == json!("ambiguous") && r["trainable"] == json!(false)));
        cap.stop().unwrap();

        // Native StockRecord pack/unpack roundtrip and original ledger preservation.
        cap.set_label_stage("instant").unwrap();
        cap.begin(BeginOptions {
            person_id: "p".into(),
            confirmed: true,
            guide: false,
            ..BeginOptions::default()
        })
        .unwrap();
        cap.bind_capture(8, json!(99), json!("native transport metadata"))
            .unwrap();
        let native = StockRecord {
            capture_session_id: 8,
            service_generation: 99,
            source_seq: h.source_seq,
            mouth_sensor_ns: h.mouth_sensor_ns,
            stock_input_ns: h.mouth_sensor_ns,
            vendor_ns: h.vendor_ns,
            raw_identity: h.raw_identity,
            produced_mono_ns: 1,
            label_seq: 1,
            classification: 0,
            raw33: [-0.1; 33],
            instant33: [0.2; 33],
            final33: [0.3; 33],
            quality2: [0.9, 0.1],
            instant_preclamp33: [0.2; 33],
            valid: true,
            association_valid: true,
            warmup: false,
        };
        let roundtripped = StockRecord::unpack(&native.pack().unwrap()).unwrap();
        cap.on_stock(&roundtripped).unwrap();
        cap.on_frame(&h, jpeg_data()).unwrap();
        cap.stop().unwrap();

        let (_, rows) = load_dataset(&cap.path().unwrap()).unwrap();
        assert_eq!(rows[0]["pairing_status"], json!("paired"));
        assert!((rows[0]["targets"][0].as_f64().unwrap() - 0.2).abs() < 1e-6);
        assert_eq!(rows[0]["stock"]["source_identity"], json!(h.raw_identity));
        let stock_ledger = read_jsonl(&cap.path().unwrap().join("stock.jsonl"));
        assert_eq!(stock_ledger[0]["record"]["raw_identity"], json!(h.raw_identity));
        assert!(stock_ledger[0]["record"].get("source_identity").is_none());

        // Native unassociated record never assumed proven.
        begin_default(&cap, "instant", false);
        let mut unassoc = make_stock_value(&h);
        unassoc["association_valid"] = json!(false);
        unassoc["raw_identity"] = json!(h.raw_identity);
        unassoc["valid"] = json!(true);
        cap.on_stock_value(unassoc).unwrap();
        cap.on_frame(&h, jpeg_data()).unwrap();
        assert_eq!(cap.samples()[0]["trainable"], json!(false));
    }

    #[test]
    fn timeout_late_exact_match_and_stop_gaps() {
        let dir = tempfile::tempdir().unwrap();
        let clock_ns = Arc::new(AtomicI64::new(10));
        let c = clock_ns.clone();
        let cap = WholeMouthRecording::with_mono_ns_clock(
            dir.path().join("recordings"),
            Arc::new(move || c.load(Ordering::SeqCst)),
        );
        let h = make_header(1, 8, None);

        // Frame timeout then late stock.
        begin_default(&cap, "raw_only", false);
        clock_ns.store(10, Ordering::SeqCst);
        cap.on_frame(&h, jpeg_data()).unwrap();
        clock_ns.store(3_000_000_010, Ordering::SeqCst);
        cap.on_stock_value(make_stock_value(&h)).unwrap();
        assert_eq!(cap.samples()[0]["pairing_status"], json!("paired_late"));
        assert_eq!(cap.status()["counts"]["image_pair_timeout"], json!(1));
        cap.stop().unwrap();

        // Stock timeout then late frame.
        begin_default(&cap, "raw_only", false);
        clock_ns.store(10, Ordering::SeqCst);
        cap.on_stock_value(make_stock_value(&h)).unwrap();
        clock_ns.store(3_000_000_010, Ordering::SeqCst);
        cap.on_frame(&h, jpeg_data()).unwrap();
        assert_eq!(cap.samples()[0]["pairing_status"], json!("paired_late"));
        cap.stop().unwrap();

        // Stop records gaps, missing stock, missing image, and is idempotent.
        begin_default(&cap, "raw_only", false);
        cap.on_frame(&make_header(1, 8, None), jpeg_data()).unwrap();
        cap.on_frame(&make_header(4, 8, None), jpeg_data()).unwrap();
        cap.on_stock_value(make_stock_value(&make_header(3, 8, None)))
            .unwrap();
        let st = cap.stop().unwrap();
        assert_eq!(st["counts"]["image_without_stock"], json!(2));
        assert_eq!(st["counts"]["stock_without_image"], json!(1));
        let events = read_jsonl(&cap.path().unwrap().join("events.jsonl"));
        let gap = events.iter().find(|e| e["kind"] == json!("source_gap")).unwrap();
        assert_eq!(gap["first_missing"], json!(2));
        assert_eq!(gap["last_missing"], json!(3));
        assert_eq!(gap["count"], json!(2));
        let before = fs::read(cap.path().unwrap().join("samples.jsonl")).unwrap();
        cap.stop().unwrap();
        assert_eq!(before, fs::read(cap.path().unwrap().join("samples.jsonl")).unwrap());
    }

    #[test]
    fn nonfinite_warmup_invalid_stock_and_review_rules() {
        let dir = tempfile::tempdir().unwrap();
        let cap = WholeMouthRecording::new(dir.path().join("recordings"));
        begin_default(&cap, "instant", false);

        let bad_cases: Vec<(&str, Value)> = vec![
            ("warmup", json!(true)),
            ("mouth_valid", json!(false)),
            ("warmup", Value::Null),
            ("quality2", json!([f64_to_json(f64::NAN), 0.1])),
        ];
        for (idx, (k, v)) in bad_cases.into_iter().enumerate() {
            let h = make_header((idx + 1) as u64, 8, None);
            cap.on_frame(&h, jpeg_data()).unwrap();
            let mut s = make_stock_value(&h);
            s[k] = v;
            cap.on_stock_value(s).unwrap();
        }
        assert!(cap
            .samples()
            .iter()
            .all(|r| r["targets"] == Value::Array(vec![Value::Null; 50])));
        cap.stop().unwrap();

        // Non-finite raw preserved named and targets null.
        begin_default(&cap, "instant", false);
        let h = make_header(1, 8, None);
        let mut raw33: Vec<Value> = vec![json!(0.1); 33];
        raw33[0] = f64_to_json(f64::NEG_INFINITY);
        let mut inst33: Vec<Value> = vec![json!(0.1); 33];
        inst33[3] = f64_to_json(f64::NAN);
        inst33[4] = f64_to_json(f64::INFINITY);
        cap.on_frame(&h, jpeg_data()).unwrap();
        let mut s = make_stock_value(&h);
        s["raw33"] = Value::Array(raw33);
        s["instant33"] = Value::Array(inst33);
        cap.on_stock_value(s).unwrap();
        cap.stop().unwrap();
        let sample_rows = read_jsonl(&cap.path().unwrap().join("samples.jsonl"));
        assert_eq!(sample_rows[0]["stock"]["raw33"][0], json!("-Infinity"));
        assert!(sample_rows[0]["targets"][3].is_null());
        assert!(sample_rows[0]["targets"][4].is_null());
        let stock_rows = read_jsonl(&cap.path().unwrap().join("stock.jsonl"));
        assert_eq!(stock_rows[0]["record"]["instant33"][3], json!("NaN"));

        // Review consent, explicit zero, clear, occluded exclusion, review_image.
        begin_default(&cap, "instant", false);
        cap.on_frame(&h, jpeg_data()).unwrap();
        let key = cap.samples()[0]["sample_id"].as_str().unwrap().to_string();
        let sid = cap.session_id().unwrap();
        assert_eq!(cap.review_image(&sid, &key).unwrap(), jpeg_data());
        assert!(cap.review_image("wrong", &key).is_err());
        assert!(cap.review_image(&sid, "f999999999").is_err());

        let mut labels = Map::new();
        labels.insert("TongueOut".into(), json!(0.0));
        assert!(cap
            .review(std::slice::from_ref(&key), &labels, false, "", "visible")
            .is_err());
        assert!(cap
            .review(
                std::slice::from_ref(&key),
                &labels,
                true,
                "p",
                "occluded"
            )
            .is_err());

        labels.insert("TongueLeft".into(), json!(0.5));
        cap.review(
            std::slice::from_ref(&key),
            &labels,
            true,
            "local-user",
            "visible",
        )
        .unwrap();
        assert_eq!(
            cap.samples()[0]["targets"][channel_index("TongueOut").unwrap()],
            json!(0.0)
        );
        assert!(cap.samples()[0]["targets"][channel_index("TongueRight").unwrap()].is_null());

        let mut clear_left = Map::new();
        clear_left.insert("TongueLeft".into(), Value::Null);
        cap.review(
            std::slice::from_ref(&key),
            &clear_left,
            true,
            "local-user",
            "visible",
        )
        .unwrap();
        assert!(cap.samples()[0]["targets"][channel_index("TongueLeft").unwrap()].is_null());

        // Occluded excludes late stock too.
        cap.review(
            std::slice::from_ref(&key),
            &Map::new(),
            true,
            "p",
            "occluded",
        )
        .unwrap();
        cap.on_stock_value(make_stock_value(&h)).unwrap();
        assert_eq!(
            cap.samples()[0]["targets"],
            Value::Array(vec![Value::Null; 50])
        );

        // Cancel blocks callbacks, review after cancel exports.
        let old_session = cap.session_id().unwrap();
        cap.cancel().unwrap();
        assert!(!cap.on_frame(&make_header(2, 8, None), jpeg_data()).unwrap());
        assert!(!cap.on_stock_value(make_stock_value(&h)).unwrap());
        let mut up = Map::new();
        up.insert("TongueUp".into(), json!(0.5));
        cap.review(std::slice::from_ref(&key), &up, true, "p", "visible")
            .unwrap();
        let (_, loaded) = load_dataset(&cap.path().unwrap()).unwrap();
        assert_eq!(
            loaded[0]["targets"][channel_index("TongueUp").unwrap()],
            json!(0.5)
        );
        begin_default(&cap, "raw_only", false);
        assert_ne!(old_session, cap.session_id().unwrap());
    }

    #[test]
    fn end_payload_mismatch_guide_candidates_and_split_checks() {
        let dir = tempfile::tempdir().unwrap();
        let cap = WholeMouthRecording::new(dir.path().join("recordings"));
        begin_default(&cap, "raw_only", true);

        let mut end_h = make_header(1, 8, None);
        end_h.flags = END;
        end_h.payload_bytes = 0;
        assert!(!cap.on_frame(&end_h, b"").unwrap());
        assert!(!cap.on_frame(&make_header(1, 8, None), b"bad").unwrap());
        assert_eq!(cap.status()["images"], json!(0));

        // Guide cannot finish with no frames.
        cap.advance("ready", None, None).unwrap();
        assert!(cap.advance("finish", None, None).is_err());
        assert_eq!(cap.status()["guide"]["phase"], json!("hold"));
        assert_eq!(cap.status()["guide"]["completed"], json!([]));

        // Gap > 250ms resets hold frames.
        cap.on_frame(&make_header(1, 8, Some(1)), jpeg_data()).unwrap();
        cap.on_frame(&make_header(2, 8, Some(900_000_001)), jpeg_data())
            .unwrap();
        assert_eq!(cap.status()["guide"]["frames"], json!(1));
        cap.advance("skip", None, None).unwrap();
        assert_eq!(cap.status()["guide"]["skipped"], json!(["neutral"]));
        assert_eq!(cap.status()["guide"]["completed"], json!([]));

        // Prompt proposals exclude reaction/tail and need separate review.
        cap.advance("ready", None, None).unwrap();
        for seq in 10..56u64 {
            cap.on_frame(
                &make_header(seq, 8, Some(1 + (seq - 10) * 100_000_000)),
                jpeg_data(),
            )
            .unwrap();
        }
        cap.advance("finish", None, None).unwrap();
        let candidates: Vec<Value> = cap
            .samples()
            .into_iter()
            .filter(|r| r.get("prompt_candidate").is_some_and(is_truthy))
            .collect();
        assert!(!candidates.is_empty());
        assert!(candidates[0]["mouth_sensor_ns"].as_u64().unwrap() >= 1_000_000_001);
        assert!(candidates.last().unwrap()["mouth_sensor_ns"].as_u64().unwrap() <= 4_200_000_001);
        cap.advance("confirm", None, None).unwrap();
        assert!(cap
            .samples()
            .iter()
            .all(|r| r["targets"] == Value::Array(vec![Value::Null; 50])));

        let page = cap.review_candidates(0, 1);
        assert_eq!(page["samples"].as_array().unwrap().len(), 1);
        let before_pending = cap.status()["pending_reviews"].as_u64().unwrap();
        let first_id = page["samples"][0]["sample_id"].as_str().unwrap().to_string();
        let mut lbl = Map::new();
        lbl.insert("TongueOut".into(), json!(0.0));
        cap.review(&[first_id], &lbl, true, "p", "visible").unwrap();
        assert_eq!(
            cap.status()["pending_reviews"].as_u64().unwrap(),
            before_pending - 1
        );
        assert_eq!(
            cap.review_candidates(0, 1)["samples"][0]["prompt_candidate"]["reviewed"],
            json!(true)
        );

        // Unfinished manifest rejected by load_dataset.
        assert!(load_dataset(&cap.path().unwrap()).is_err());
        assert!(cap.set_label_stage("instant").is_err());
        cap.stop().unwrap();
        let (first_manifest, first) = load_dataset(&cap.path().unwrap()).unwrap();
        assert_eq!(first_manifest["samples_complete"], json!(true));

        // Refit gets independent source group; re-recording same frame caught by leakage check.
        begin_default(&cap, "raw_only", false);
        cap.on_frame(&make_header(200, 8, None), jpeg_data()).unwrap();
        cap.stop().unwrap();
        let (_, second) = load_dataset(&cap.path().unwrap()).unwrap();
        assert_ne!(first[0]["source_group_id"], second[0]["source_group_id"]);
        let splits = BTreeMap::from([("train".into(), first.clone()), ("test".into(), second)]);
        assert_no_split_leakage(&splits, "session_id").unwrap();

        begin_default(&cap, "raw_only", false);
        cap.on_frame(&make_header(1, 8, Some(1)), jpeg_data()).unwrap();
        cap.stop().unwrap();
        let (_, dup) = load_dataset(&cap.path().unwrap()).unwrap();
        let splits = BTreeMap::from([("train".into(), first), ("test".into(), dup)]);
        assert!(assert_no_split_leakage(&splits, "session_id")
            .unwrap_err()
            .contains("original_frame"));
    }

    // ---- Automatic Guide Tests (ported from test_automatic_guide.py) ----

    struct AutoHarness {
        now: Arc<Mutex<f64>>,
        guide: MouthGuide,
        seq: u64,
    }

    impl AutoHarness {
        fn new(prepare_seconds: f64) -> Self {
            let now = Arc::new(Mutex::new(0.0f64));
            let c = now.clone();
            let clock: ClockFn = Arc::new(move || *c.lock().unwrap());
            let guide =
                MouthGuide::with_clock("basics", true, clock, None, 0, prepare_seconds).unwrap();
            Self { now, guide, seq: 0 }
        }

        fn feed(&mut self, paint: bool) -> bool {
            *self.now.lock().unwrap() += 0.1;
            self.seq += 1;
            if paint {
                let f = self.guide.frame.clone();
                let p = self.guide.phase.clone();
                self.guide.display(&f, &p, true, true);
            }
            self.guide.feed(&json!({
                "capture_session_id": 8,
                "service_generation": 99,
                "mouth_sensor_ns": self.seq * 100_000_000,
            }))
        }
    }

    #[test]
    fn auto_no_paint_no_countdown_and_old_phase_does_not_unlock_hold() {
        let mut h = AutoHarness::new(3.0);
        for _ in 0..60 {
            h.feed(false);
        }
        assert_eq!(h.guide.phase, "prepare");
        assert_eq!(h.guide.status()["remaining_seconds"], json!(3.0));
        let frame = h.guide.frame.clone();
        for _ in 0..40 {
            h.feed(true);
        }
        assert_eq!(h.guide.phase, "hold");
        assert!(!h.guide.display(&frame, "prepare", true, true));
    }

    #[test]
    fn auto_countdown_then_continuous_frames_without_phase_buttons() {
        let mut h = AutoHarness::new(3.0);
        for _ in 0..85 {
            h.feed(true);
        }
        assert_eq!(h.guide.phase, "hold");
        assert!(!h.guide.candidates().is_empty());
        assert_eq!(h.guide.status()["automatic"], json!(true));
    }

    #[test]
    fn auto_pause_resume_and_lost_display_reset_current_hold() {
        let mut h = AutoHarness::new(3.0);
        for _ in 0..60 {
            h.feed(true);
        }
        assert!(!h.guide.frames.is_empty());
        h.guide.advance("pause", None).unwrap();
        for _ in 0..90 {
            h.feed(true);
        }
        assert_eq!(h.guide.phase, "prepare");
        assert!(h.guide.frames.is_empty());
        h.guide.advance("resume", None).unwrap();
        for _ in 0..60 {
            h.feed(true);
        }
        assert!(!h.guide.frames.is_empty());
        *h.now.lock().unwrap() += 2.0;
        h.feed(false);
        assert!(h.guide.frames.is_empty());
    }

    #[test]
    fn auto_skip_moves_in_preset_order_with_new_display_token() {
        let mut h = AutoHarness::new(3.0);
        let frame = h.guide.frame.clone();
        h.guide.advance("skip", None).unwrap();
        assert_eq!(h.guide.step().unwrap().id, "JawOpen");
        assert_ne!(h.guide.frame, frame);
    }

    #[test]
    fn auto_custom_preparation_includes_fresh_countdown_after_jumping() {
        let mut h = AutoHarness::new(7.0);
        for _ in 0..60 {
            h.feed(true);
        }
        assert_eq!(h.guide.phase, "prepare");
        h.guide.advance("select_step", Some(4)).unwrap();
        assert_eq!(h.guide.status()["remaining_seconds"], json!(7.0));
        assert_eq!(h.guide.status()["progress"], json!(0.0));
        for _ in 0..72 {
            h.feed(true);
        }
        assert_eq!(h.guide.phase, "hold");
    }

    #[test]
    fn auto_window_completion_preserves_unknown_targets() {
        let dir = tempfile::tempdir().unwrap();
        let rec = WholeMouthRecording::new(dir.path());
        rec.begin(BeginOptions {
            person_id: "fixture".into(),
            confirmed: true,
            preset_id: "basics".into(),
            automatic: true,
            ..BeginOptions::default()
        })
        .unwrap();
        rec.bind_capture(8, json!(99), json!({ "test_only": true }))
            .unwrap();
        let now = Arc::new(Mutex::new(0.0f64));
        let c = now.clone();
        rec.set_guide_clock(Arc::new(move || *c.lock().unwrap()));
        let sid = rec.session_id().unwrap();
        for seq in 1..90u64 {
            *now.lock().unwrap() = (seq as f64) * 0.1;
            let frame = rec.guide_frame().unwrap();
            let phase = rec.guide_phase().unwrap();
            rec.display(&sid, &frame, &phase, true, true);
            rec.on_frame(&make_header(seq, 8, Some(seq * 100_000_000)), jpeg_data())
                .unwrap();
        }
        assert!(rec.guide_index().unwrap() >= 1);
        assert!(rec
            .samples()
            .iter()
            .all(|r| r["targets"].as_array().unwrap().iter().all(|v| v.is_null())));
        assert_eq!(rec.manifest().unwrap()["automatic_guidance"], json!(true));
        rec.stop().unwrap();
    }
}
