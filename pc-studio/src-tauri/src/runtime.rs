use crate::config::write_text_atomic;
use crate::face_capture::{BeginOptions, WholeMouthRecording};
use crate::face_dataset::{f64_to_json, json_value as dataset_json_value};
use crate::face_filter::{FaceFilter, FilterConfig};
use crate::face_model::{LoadedModel, ModelOutput, MouthModel, PredictContext};
use crate::face_presets::CaptureContinuation;
use crate::face_schema::{
    channel_index, is_aggregate_stock_channel, matching_preset, mouth_presets_json,
    resolve_channel_sources, validate_channel_sources, AGGREGATE_CHILDREN, ALL_VALID, CHANNELS,
    MOUTH_MODES, PRESET_MODES, PURE_EXTENDED_CHANNELS, ROUTABLE_CHANNELS, SCHEMA,
    STOCK_AGGREGATE_PARENTS, STOCK_CHANNELS,
};
use crate::npz_replay::load_npz_replay;
use crate::personalization::{apply_calibration, Personalization};
use crate::state::{RawTongueInput, TonguePrediction, TongueState, TongueStateConfig};
use crate::stock::{
    read_capture_context, CaptureContext, CaptureContextEvidence, StockRecord, STOCK_BYTES,
};
use crate::training::{self, TrainingRunner};
use crate::updater::APP_VERSION;
use crate::wire::{
    self, AcceptOutcome, ClockCalibration, FrameHeader, SourceOrder, WireError, END, JPEG, RAW,
    REPLAY,
};
use image::codecs::jpeg::JpegEncoder;
use image::{ExtendedColorType, ImageFormat};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::f64::consts::PI;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufWriter, Read, Write};
use std::net::{IpAddr, Shutdown, SocketAddr, TcpStream, UdpSocket};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const DEFAULT_MOUTH_PORT: u16 = 45681;
pub const DEFAULT_OUTPUT_PORT: u16 = 45682;

const RECORD_QUEUE_LIMIT: usize = 256;

const KNOWN_CONFIG_KEYS: [&str; 22] = [
    "enabled",
    "headset_host",
    "headset_port",
    "model_path",
    "model_kind",
    "device",
    "smoothing_ms",
    "filter_enabled",
    "filter_min_cutoff",
    "filter_beta",
    "filter_d_cutoff",
    "uncertain_hold_ms",
    "stale_ms",
    "preview_fps",
    "size_hysteresis",
    "replay_path",
    "mouth_mode",
    "hybrid_channels",
    "channel_sources",
    "output_enabled",
    "output_host",
    "output_port",
];

const RESTART_KEYS: [&str; 8] = [
    "headset_host",
    "headset_port",
    "model_path",
    "model_kind",
    "device",
    "output_enabled",
    "output_host",
    "output_port",
];

static RUN_DIR_SEQ: AtomicU64 = AtomicU64::new(0);

pub type ModelFactory =
    Arc<dyn Fn(&RuntimeConfig) -> Result<Box<dyn MouthModel>, String> + Send + Sync>;

pub fn default_model_factory() -> ModelFactory {
    Arc::new(|cfg: &RuntimeConfig| {
        let loaded = LoadedModel::load(&cfg.model_kind, &cfg.model_path, &cfg.device)?;
        Ok(Box::new(loaded))
    })
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RuntimeConfig {
    pub enabled: bool,
    pub headset_host: String,
    pub headset_port: u16,
    pub model_path: String,
    pub model_kind: String,
    pub device: String,
    pub smoothing_ms: f64,
    pub filter_enabled: bool,
    pub filter_min_cutoff: f64,
    pub filter_beta: f64,
    pub filter_d_cutoff: f64,
    pub uncertain_hold_ms: f64,
    pub stale_ms: f64,
    pub preview_fps: u32,
    pub size_hysteresis: bool,
    pub replay_path: String,
    pub mouth_mode: String,
    pub hybrid_channels: Option<Vec<String>>,
    pub channel_sources: Option<BTreeMap<String, String>>,
    pub output_enabled: bool,
    pub output_host: String,
    pub output_port: u16,
}

impl Default for RuntimeConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            headset_host: String::new(),
            headset_port: DEFAULT_MOUTH_PORT,
            model_path: String::new(),
            model_kind: "face50".to_string(),
            device: "auto".to_string(),
            smoothing_ms: 20.0,
            filter_enabled: true,
            filter_min_cutoff: 8.0,
            filter_beta: 4.0,
            filter_d_cutoff: 1.0,
            uncertain_hold_ms: 100.0,
            stale_ms: 350.0,
            preview_fps: 10,
            size_hysteresis: true,
            replay_path: String::new(),
            mouth_mode: "curated".to_string(),
            hybrid_channels: None,
            channel_sources: None,
            output_enabled: false,
            output_host: "127.0.0.1".to_string(),
            output_port: DEFAULT_OUTPUT_PORT,
        }
    }
}

impl RuntimeConfig {
    fn filter_config(&self) -> FilterConfig {
        FilterConfig {
            filter_enabled: Some(self.filter_enabled),
            filter_min_cutoff: Some(self.filter_min_cutoff),
            filter_beta: self.filter_beta,
            filter_d_cutoff: self.filter_d_cutoff,
            smoothing_ms: self.smoothing_ms,
            stale_ms: self.stale_ms,
        }
    }

    fn tongue_config(&self) -> TongueStateConfig {
        TongueStateConfig {
            smoothing_ms: self.smoothing_ms,
            uncertain_hold_ms: self.uncertain_hold_ms,
        }
    }

    fn restart_key_differs(&self, other: &Self) -> bool {
        self.headset_host != other.headset_host
            || self.headset_port != other.headset_port
            || self.model_path != other.model_path
            || self.model_kind != other.model_kind
            || self.device != other.device
            || self.output_enabled != other.output_enabled
            || self.output_host != other.output_host
            || self.output_port != other.output_port
    }
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct StartRequest {
    pub mode: Option<String>,
    pub path: Option<String>,
    pub replay_path: Option<String>,
    pub capture_only: Option<bool>,
}

#[derive(Debug, Clone)]
pub struct RecordingActionOptions {
    pub person_id: String,
    pub confirmed: bool,
    pub guide: bool,
    pub stage: String,
    pub role: Option<String>,
    pub evaluation_only: Option<bool>,
    pub preset_id: String,
    pub automatic: bool,
    pub receive_results: bool,
    pub continuation: Option<CaptureContinuation>,
    pub continuation_session_id: Option<String>,
    pub step_index: Option<usize>,
    pub prepare_seconds: f64,
    pub guide_frame: Option<String>,
}

impl Default for RecordingActionOptions {
    fn default() -> Self {
        Self {
            person_id: String::new(),
            confirmed: false,
            guide: true,
            stage: "raw_only".to_string(),
            role: None,
            evaluation_only: None,
            preset_id: "complete".to_string(),
            automatic: false,
            receive_results: true,
            continuation: None,
            continuation_session_id: None,
            step_index: None,
            prepare_seconds: 3.0,
            guide_frame: None,
        }
    }
}

#[derive(Debug, Clone, Default)]
struct MetricsCounts {
    received: u64,
    decoded: u64,
    inferred: u64,
    pending_dropped: u64,
    source_gaps: u64,
    duplicates: u64,
    out_of_order: u64,
    retired_session: u64,
    non_advancing_time: u64,
    superseded: u64,
    stale: u64,
    decode_errors: u64,
    replay_late_skipped: Option<u64>,
}

impl MetricsCounts {
    fn to_map(&self) -> Map<String, Value> {
        let mut m = Map::new();
        m.insert("received".to_string(), json!(self.received));
        m.insert("decoded".to_string(), json!(self.decoded));
        m.insert("inferred".to_string(), json!(self.inferred));
        m.insert("pending_dropped".to_string(), json!(self.pending_dropped));
        m.insert("source_gaps".to_string(), json!(self.source_gaps));
        m.insert("duplicates".to_string(), json!(self.duplicates));
        m.insert("out_of_order".to_string(), json!(self.out_of_order));
        m.insert("retired_session".to_string(), json!(self.retired_session));
        m.insert(
            "non_advancing_time".to_string(),
            json!(self.non_advancing_time),
        );
        m.insert("superseded".to_string(), json!(self.superseded));
        m.insert("stale".to_string(), json!(self.stale));
        m.insert("decode_errors".to_string(), json!(self.decode_errors));
        if let Some(late) = self.replay_late_skipped {
            m.insert("replay_late_skipped".to_string(), json!(late));
        }
        m
    }
}

#[derive(Debug, Clone, Copy)]
struct LatencySample {
    source_copy_to_result_ms: f64,
    queue_ms: f64,
    pc_total_ms: f64,
    #[allow(dead_code)]
    filter_ms: f64,
    decode_ms: f64,
    predict_ms: f64,
}

#[derive(Debug, Clone, Default)]
struct ReplayState {
    active: bool,
    path: Option<String>,
    position: usize,
    total: Option<usize>,
    finished: bool,
}

impl ReplayState {
    fn to_json(&self) -> Value {
        json!({
            "active": self.active,
            "path": self.path,
            "position": self.position,
            "total": self.total,
            "finished": self.finished,
        })
    }
}

#[derive(Debug, Clone)]
struct StockStatus {
    active: bool,
    records: u64,
    associated: u64,
    unassociated: u64,
    error: Option<String>,
}

impl Default for StockStatus {
    fn default() -> Self {
        Self {
            active: false,
            records: 0,
            associated: 0,
            unassociated: 0,
            error: None,
        }
    }
}

impl StockStatus {
    fn to_json(&self) -> Value {
        json!({
            "active": self.active,
            "records": self.records,
            "associated": self.associated,
            "unassociated": self.unassociated,
            "error": self.error,
        })
    }
}

#[derive(Debug, Clone, PartialEq)]
struct TransportBinding {
    service_generation: Value,
    evidence: Value,
}

struct PendingFrame {
    header: FrameHeader,
    payload: Vec<u8>,
    received_ns: i64,
}

enum RecordItem {
    Frame(FrameHeader, Vec<u8>),
    Stock(StockRecord),
}

struct RecordQueueState {
    queue: VecDeque<RecordItem>,
    stopping: bool,
    cancelled: bool,
    worker_active: bool,
    error: Option<String>,
}

impl Default for RecordQueueState {
    fn default() -> Self {
        Self {
            queue: VecDeque::new(),
            stopping: false,
            cancelled: false,
            worker_active: false,
            error: None,
        }
    }
}

struct RuntimeState {
    config: RuntimeConfig,
    preserved_extra: Map<String, Value>,
    legacy_configuration: Option<Value>,
    running: bool,
    connected: bool,
    capture_only: bool,
    mode: String,
    last_error: Option<String>,
    pending: Option<PendingFrame>,
    source: Option<FrameHeader>,
    prediction_source: Option<FrameHeader>,
    face_filter: FaceFilter,
    prediction: Value,
    model_key: Option<(String, String, String)>,
    description: Value,
    order: SourceOrder,
    tongue: TongueState,
    preview: Option<Vec<u8>>,
    clock: Option<ClockCalibration>,
    clock_domain: Option<String>,
    counts: MetricsCounts,
    latencies: VecDeque<LatencySample>,
    received_times: VecDeque<i64>,
    inferred_times: VecDeque<i64>,
    last_header: Option<FrameHeader>,
    last_result_ns: i64,
    last_preview_ns: i64,
    replay: ReplayState,
    lifecycle: u64,
    psl_gen: u64,
    stop_flag: Arc<AtomicBool>,
    udp_target: Option<SocketAddr>,
    output_session_id: u64,
    model_version: u64,
    model_reset_pending: bool,
    profile_reload: bool,
    capture_context: Option<CaptureContextEvidence>,
    transport_bindings: HashMap<u64, TransportBinding>,
    active_socket: Option<TcpStream>,
    stock_socket: Option<TcpStream>,
    latest_stock: Option<(StockRecord, i64)>,
    record_wants_stock: bool,
    source_closed: bool,
    stock_status: StockStatus,
    record_status: Value,
    run_dir: Option<PathBuf>,
    log_writer: Option<BufWriter<File>>,
    clock_log: Option<File>,
    input_stream: Option<BufWriter<File>>,
}

struct PslWorkerState {
    stop_flag: Arc<AtomicBool>,
    finished: Arc<AtomicBool>,
    thread: Option<wire::BoundedThread>,
}

struct UdpSockets {
    v4: Option<UdpSocket>,
    v6: Option<UdpSocket>,
}

struct RuntimeInner {
    config_path: PathBuf,
    run_root: PathBuf,
    record_input: bool,
    script_root: Mutex<PathBuf>,
    source_directory: String,
    model_factory: ModelFactory,
    config_write_lock: Mutex<()>,
    state: Mutex<RuntimeState>,
    model: Mutex<Option<Box<dyn MouthModel>>>,
    frame_cond: Condvar,
    inference_ready_state: Mutex<bool>,
    inference_ready_cond: Condvar,
    clock_state: Mutex<bool>,
    clock_cond: Condvar,
    threads: Mutex<Vec<wire::BoundedThread>>,
    udp: Mutex<UdpSockets>,
    personal: Personalization,
    recorder: Mutex<Option<Arc<WholeMouthRecording>>>,
    record_control: Mutex<()>,
    record_queue: Mutex<RecordQueueState>,
    record_cond: Condvar,
    record_thread: Mutex<Option<JoinHandle<()>>>,
    psl_lock: Mutex<PslWorkerState>,
    training: TrainingRunner,
}

pub struct Runtime {
    inner: Arc<RuntimeInner>,
}

impl Drop for Runtime {
    fn drop(&mut self) {
        self.close();
    }
}

fn unix_time_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0)
}

fn round2(val: f64) -> f64 {
    (val * 100.0).round() / 100.0
}

fn default_script_root() -> PathBuf {
    // Prefer executable directory when face_train.py lives next to the binary;
    // in debug builds, also allow resolving the pc-studio source checkout next to src-tauri.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            if dir.join("face_train.py").is_file() {
                return dir.to_path_buf();
            }
        }
    }
    #[cfg(debug_assertions)]
    {
        let manifest_parent = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .map(Path::to_path_buf);
        if let Some(parent) = manifest_parent {
            if parent.join("face_train.py").is_file() {
                return parent;
            }
        }
    }
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."))
}

pub(crate) fn parse_port_value(value: &Value, name: &str) -> Result<u16, String> {
    let err = || format!("{name} must be an integer in 1..65535");
    match value {
        Value::Bool(_) => Err(err()),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                if (1..=65535).contains(&i) {
                    return Ok(i as u16);
                }
            } else if let Some(f) = n.as_f64() {
                if f.is_finite() && f.fract() == 0.0 && (1.0..=65535.0).contains(&f) {
                    return Ok(f as u16);
                }
            }
            Err(err())
        }
        Value::String(s) => {
            let parsed: i64 = s.trim().parse().map_err(|_| err())?;
            if (1..=65535).contains(&parsed) {
                Ok(parsed as u16)
            } else {
                Err(err())
            }
        }
        _ => Err(err()),
    }
}

fn parse_finite_f64(value: &Value, err_msg: &str) -> Result<f64, String> {
    let v = match value {
        Value::Bool(_) => return Err(err_msg.to_string()),
        Value::Number(n) => n.as_f64().ok_or_else(|| err_msg.to_string())?,
        Value::String(s) => s
            .trim()
            .parse::<f64>()
            .map_err(|_| err_msg.to_string())?,
        _ => return Err(err_msg.to_string()),
    };
    if !v.is_finite() {
        return Err(err_msg.to_string());
    }
    Ok(v)
}

fn parse_positive_f64(value: &Value, name: &str) -> Result<f64, String> {
    let msg = format!("{name} must be a finite positive number");
    let v = parse_finite_f64(value, &msg)?;
    if v <= 0.0 {
        return Err(msg);
    }
    Ok(v)
}

fn parse_nonnegative_f64(value: &Value, name: &str) -> Result<f64, String> {
    let msg = format!("{name} must be a finite nonnegative number");
    let v = parse_finite_f64(value, &msg)?;
    if v < 0.0 {
        return Err(msg);
    }
    Ok(v)
}

fn parse_preview_fps(value: &Value) -> Result<u32, String> {
    let err = || "preview_fps must be 1–30".to_string();
    let v = match value {
        Value::Bool(_) => return Err(err()),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i
            } else if let Some(f) = n.as_f64() {
                if f.is_finite() && f.fract() == 0.0 {
                    f as i64
                } else {
                    return Err(err());
                }
            } else {
                return Err(err());
            }
        }
        Value::String(s) => s.trim().parse::<i64>().map_err(|_| err())?,
        _ => return Err(err()),
    };
    if !(1..=30).contains(&v) {
        return Err(err());
    }
    Ok(v as u32)
}

fn parse_bool_loose(value: &Value) -> bool {
    match value {
        Value::Bool(b) => *b,
        Value::Null => false,
        Value::Number(n) => n.as_f64().unwrap_or(0.0) != 0.0,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn parse_string_field(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

fn parse_host_string(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(s) => s.trim().to_string(),
        other => other.to_string().trim().to_string(),
    }
}

fn parse_u64_json(value: Option<&Value>) -> u64 {
    let Some(v) = value else {
        return 0;
    };
    match v {
        Value::Number(n) => n
            .as_u64()
            .or_else(|| n.as_i64().and_then(|i| u64::try_from(i).ok()))
            .unwrap_or(0),
        Value::String(s) => s.trim().parse::<u64>().unwrap_or(0),
        _ => 0,
    }
}

fn calc_age(
    clock: Option<&ClockCalibration>,
    mode: &str,
    header: &FrameHeader,
    pc_ns: i64,
) -> (f64, f64) {
    let Some(clock) = clock else {
        return (f64::INFINITY, f64::INFINITY);
    };
    let age = ((pc_ns as f64) + clock.offset_ns - (header.copy_mono_ns as f64)) / 1e6;
    if mode == "live" && pc_ns - clock.calibrated_pc_ns > 60_000_000_000 {
        return (age, f64::INFINITY);
    }
    (age, age + clock.uncertainty_ms)
}

fn calc_hz(values: &VecDeque<i64>) -> f64 {
    if values.len() > 1 {
        let first = *values.front().unwrap();
        let last = *values.back().unwrap();
        if last > first {
            return ((values.len() - 1) as f64) * 1e9 / ((last - first) as f64);
        }
    }
    0.0
}

fn percentile_sorted(sorted: &[f64], pct: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    if sorted.len() == 1 {
        return sorted[0];
    }
    let pos = (pct / 100.0) * ((sorted.len() - 1) as f64);
    let lo = pos.floor() as usize;
    let hi = pos.ceil() as usize;
    let frac = pos - (lo as f64);
    sorted[lo] * (1.0 - frac) + sorted[hi] * frac
}

fn latency_percentiles<F: Fn(&LatencySample) -> f64>(
    samples: &VecDeque<LatencySample>,
    extract: F,
) -> Value {
    if samples.is_empty() {
        return Value::Null;
    }
    let mut vals: Vec<f64> = samples
        .iter()
        .map(extract)
        .filter(|v| v.is_finite())
        .collect();
    if vals.is_empty() {
        return Value::Null;
    }
    vals.sort_by(|a, b| a.total_cmp(b));
    json!({
        "p50": percentile_sorted(&vals, 50.0),
        "p95": percentile_sorted(&vals, 95.0),
    })
}

fn encode_gray_jpeg(gray400: &[u8], quality: u8) -> Result<Vec<u8>, String> {
    let mut out = Vec::with_capacity(32 * 1024);
    let mut encoder = JpegEncoder::new_with_quality(&mut out, quality);
    encoder
        .encode(gray400, 400, 400, ExtendedColorType::L8)
        .map_err(|e| e.to_string())?;
    Ok(out)
}

fn encode_rgb_jpeg(rgb400: &[u8], quality: u8) -> Result<Vec<u8>, String> {
    let mut out = Vec::with_capacity(48 * 1024);
    let mut encoder = JpegEncoder::new_with_quality(&mut out, quality);
    encoder
        .encode(rgb400, 400, 400, ExtendedColorType::Rgb8)
        .map_err(|e| e.to_string())?;
    Ok(out)
}

fn build_preview_jpeg(
    gray400: &[u8],
    model_kind: &str,
    mask56: Option<&[bool]>,
) -> Result<Vec<u8>, String> {
    if model_kind == "face50" || mask56.is_none() {
        return encode_gray_jpeg(gray400, 90);
    }
    let mask = mask56.unwrap();
    if mask.len() != 56 * 56 {
        return encode_gray_jpeg(gray400, 90);
    }
    // Composite rose overlay (#fb7185 = 251, 113, 133) with alpha = 110/255 matching runtime.py:1575-1577.
    let mut rgb = vec![0u8; 400 * 400 * 3];
    for y in 0..400 {
        let my = (y * 56) / 400;
        for x in 0..400 {
            let g = gray400[y * 400 + x] as u32;
            let mx = (x * 56) / 400;
            let dst = (y * 400 + x) * 3;
            if mask[my * 56 + mx] {
                rgb[dst] = ((251 * 110 + g * 145 + 127) / 255) as u8;
                rgb[dst + 1] = ((113 * 110 + g * 145 + 127) / 255) as u8;
                rgb[dst + 2] = ((133 * 110 + g * 145 + 127) / 255) as u8;
            } else {
                rgb[dst] = g as u8;
                rgb[dst + 1] = g as u8;
                rgb[dst + 2] = g as u8;
            }
        }
    }
    encode_rgb_jpeg(&rgb, 90)
}

fn decode_frame_gray400(header: &FrameHeader, payload: &[u8]) -> Result<Vec<u8>, String> {
    if (header.flags & RAW) != 0 {
        if payload.len() != 400 * 400 {
            return Err("Decoded source dimensions changed".to_string());
        }
        return Ok(payload.to_vec());
    }
    let img = image::load_from_memory_with_format(payload, ImageFormat::Jpeg)
        .map_err(|e| e.to_string())?;
    if img.width() != 400 || img.height() != 400 {
        return Err("Decoded source dimensions changed".to_string());
    }
    Ok(img.into_luma8().into_raw())
}

fn read_stock_record(
    stream: &mut TcpStream,
    timeout: Duration,
    stop_flag: Option<&AtomicBool>,
) -> Result<StockRecord, WireError> {
    let mut buf = [0u8; STOCK_BYTES];
    let mut filled = 0usize;
    let deadline = Instant::now() + timeout;

    while filled < STOCK_BYTES {
        if stop_flag.is_some_and(|s| s.load(Ordering::Relaxed)) {
            return Err(WireError::Cancelled);
        }
        let now = Instant::now();
        let remaining = deadline.saturating_duration_since(now);
        if remaining.is_zero() {
            if filled == 0 {
                return Err(WireError::IdleTimeout(
                    "Waiting for next PSL1 record".into(),
                ));
            }
            return Err(WireError::Io(io::Error::new(
                io::ErrorKind::TimedOut,
                "timed out",
            )));
        }

        let ready = wire::poll_readable(stream, remaining, stop_flag)?;
        if !ready {
            if filled == 0 {
                return Err(WireError::IdleTimeout(
                    "Waiting for next PSL1 record".into(),
                ));
            }
            return Err(WireError::Io(io::Error::new(
                io::ErrorKind::TimedOut,
                "timed out",
            )));
        }

        let _ = stream.set_read_timeout(Some(remaining.max(Duration::from_millis(50))));
        match stream.read(&mut buf[filled..]) {
            Ok(0) => {
                return Err(WireError::Eof(format!(
                    "partial PSL1 record: {filled}/{STOCK_BYTES}"
                )));
            }
            Ok(n) => filled += n,
            Err(err) if err.kind() == io::ErrorKind::Interrupted => continue,
            Err(err) => return Err(WireError::Io(err)),
        }
    }

    StockRecord::unpack(&buf)
}

fn invalid_prediction(model_kind: &str, face_filter: &mut FaceFilter, reason: &str) -> Value {
    if model_kind != "face50" {
        return serde_json::to_value(TongueState::invalid(reason)).unwrap_or_else(|_| json!({}));
    }
    face_filter.clear();
    json!({
        "schema": SCHEMA,
        "values": vec![0.0f64; 50],
        "channels": {},
        "valid_mask": 0u64,
        "native_channels": [],
        "stock_channels": {},
        "model_channels": {},
        "model_valid_mask": 0u64,
        "state": reason,
        "fresh": false,
        "override": false,
    })
}

fn validate_face_prediction(
    values: &[f32; 50],
    raw_valid_mask: u64,
    description: &Value,
) -> Result<Value, String> {
    let trained: HashSet<&str> = description
        .get("trained_channels")
        .and_then(|v| v.as_array())
        .map(|arr| arr.iter().filter_map(|v| v.as_str()).collect())
        .unwrap_or_default();
    let validated: HashSet<&str> = description
        .get("validated_channels")
        .and_then(|v| v.as_array())
        .map(|arr| arr.iter().filter_map(|v| v.as_str()).collect())
        .unwrap_or_default();
    let mut supported = 0u64;
    for (i, &name) in CHANNELS.iter().enumerate() {
        if trained.contains(name) && validated.contains(name) {
            supported |= 1u64 << i;
        }
    }
    let desc_mask = parse_u64_json(description.get("valid_mask"));
    let mask = raw_valid_mask & desc_mask & supported & ALL_VALID;

    let mut controls = [0.0f64; 50];
    let mut ch_map = Map::new();
    for (i, &name) in CHANNELS.iter().enumerate() {
        if (mask & (1u64 << i)) != 0 {
            let v = values[i] as f64;
            if !v.is_finite() || !(0.0..=1.0).contains(&v) {
                return Err(
                    "Whole-mouth supported controls must be finite and in [0,1]".to_string()
                );
            }
            controls[i] = v;
            ch_map.insert(name.to_string(), json!(v));
        }
    }

    Ok(json!({
        "schema": SCHEMA,
        "values": controls.to_vec(),
        "channels": Value::Object(ch_map.clone()),
        "valid_mask": mask,
        "native_channels": [],
        "stock_channels": {},
        "model_channels": Value::Object(ch_map),
        "model_valid_mask": mask,
        "fresh": mask != 0,
        "override": mask != 0,
        "state": if mask != 0 { "valid" } else { "untrained" },
    }))
}

fn stock_is_fresh(
    record: Option<&StockRecord>,
    received_ns: Option<i64>,
    session_id: u64,
    config: &RuntimeConfig,
    clock: Option<&ClockCalibration>,
    now_pc_ns: i64,
) -> bool {
    let (Some(rec), Some(recv_ns)) = (record, received_ns) else {
        return false;
    };
    if !rec.valid {
        return false;
    }
    if session_id != 0 && rec.capture_session_id != 0 && rec.capture_session_id != session_id {
        return false;
    }
    if ((now_pc_ns - recv_ns) as f64) / 1e6 > config.stale_ms {
        return false;
    }
    if rec.produced_mono_ns > 0 {
        if let Some(clk) = clock {
            let source_age = ((now_pc_ns as f64) + clk.offset_ns - (rec.produced_mono_ns as f64))
                / 1e6
                + clk.uncertainty_ms;
            if source_age > config.stale_ms {
                return false;
            }
        }
    }
    true
}

pub fn merge_mouth_prediction(
    prediction: &Value,
    header: &FrameHeader,
    config: &RuntimeConfig,
    latest_stock: Option<&(StockRecord, i64)>,
    clock: Option<&ClockCalibration>,
    now_pc_ns: i64,
) -> Value {
    let mut model_values = [0.0f64; 50];
    if let Some(arr) = prediction.get("values").and_then(|v| v.as_array()) {
        for (i, v) in arr.iter().take(50).enumerate() {
            model_values[i] = v.as_f64().unwrap_or(0.0);
        }
    }
    let model_channels = prediction
        .get("channels")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();
    let model_mask = parse_u64_json(prediction.get("valid_mask"));

    let mut values = [0.0f64; 50];
    let mut mask = 0u64;
    let mut native_set: HashSet<&'static str> = HashSet::new();

    let sources = resolve_channel_sources(
        &config.mouth_mode,
        config.hybrid_channels.as_deref(),
        config.channel_sources.as_ref(),
    );
    let (record_opt, recv_opt) = match latest_stock {
        Some((r, ns)) => (Some(r), Some(*ns)),
        None => (None, None),
    };
    let fresh = stock_is_fresh(
        record_opt,
        recv_opt,
        header.session_id,
        config,
        clock,
        now_pc_ns,
    );
    let stock_ok = sources.values().any(|s| s == "stock") && fresh;

    let mut stock_channels = Map::new();
    if fresh {
        if let Some(rec) = record_opt {
            for (i, &name) in STOCK_CHANNELS.iter().enumerate() {
                if i < rec.final33.len() {
                    let v = rec.final33[i] as f64;
                    if v.is_finite() {
                        stock_channels.insert(name.to_string(), json!(v.clamp(0.0, 1.0)));
                    }
                }
            }
            for &(name, parent) in &STOCK_AGGREGATE_PARENTS {
                if let Some(val) = stock_channels.get(parent).cloned() {
                    stock_channels.insert(name.to_string(), val);
                }
            }
        }
    }

    for (i, &name) in STOCK_CHANNELS.iter().enumerate() {
        if is_aggregate_stock_channel(name) {
            continue;
        }
        let src = sources.get(name).map(String::as_str).unwrap_or("model");
        if src == "off" {
            values[i] = 0.0;
            mask |= 1u64 << i;
        } else if src == "stock" {
            if stock_ok {
                if let Some(rec) = record_opt {
                    if i < rec.final33.len() {
                        let v = rec.final33[i] as f64;
                        if v.is_finite() {
                            values[i] = v.clamp(0.0, 1.0);
                            mask |= 1u64 << i;
                            native_set.insert(name);
                        }
                    }
                }
            }
        } else if (model_mask & (1u64 << i)) != 0 {
            values[i] = model_values[i];
            mask |= 1u64 << i;
        }
    }

    for &(name, parent) in &STOCK_AGGREGATE_PARENTS {
        let sub_i = channel_index(name).unwrap();
        let parent_i = channel_index(parent).unwrap();
        let src = sources.get(name).map(String::as_str).unwrap_or("model");
        if src == "off" {
            values[sub_i] = 0.0;
            mask |= 1u64 << sub_i;
        } else if src == "stock" {
            if stock_ok {
                if let Some(rec) = record_opt {
                    if parent_i < rec.final33.len() {
                        let v = rec.final33[parent_i] as f64;
                        if v.is_finite() {
                            values[sub_i] = v.clamp(0.0, 1.0);
                            mask |= 1u64 << sub_i;
                            native_set.insert(name);
                        }
                    }
                }
            }
        } else if (model_mask & (1u64 << sub_i)) != 0 {
            values[sub_i] = model_values[sub_i];
            mask |= 1u64 << sub_i;
        } else if (model_mask & (1u64 << parent_i)) != 0 {
            values[sub_i] = model_values[parent_i];
            mask |= 1u64 << sub_i;
        }
    }

    for &(parent, children) in &AGGREGATE_CHILDREN {
        let parent_i = channel_index(parent).unwrap();
        let all_off = children
            .iter()
            .all(|&c| sources.get(c).map(String::as_str) == Some("off"));
        let all_stock = children
            .iter()
            .all(|&c| sources.get(c).map(String::as_str) == Some("stock"));
        let all_model = children
            .iter()
            .all(|&c| sources.get(c).map(String::as_str) == Some("model"));
        if all_off {
            values[parent_i] = 0.0;
            mask |= 1u64 << parent_i;
        } else if all_stock {
            if stock_ok {
                if let Some(rec) = record_opt {
                    if parent_i < rec.final33.len() {
                        let v = rec.final33[parent_i] as f64;
                        if v.is_finite() {
                            values[parent_i] = v.clamp(0.0, 1.0);
                            mask |= 1u64 << parent_i;
                            native_set.insert(parent);
                        }
                    }
                }
            }
        } else if all_model && (model_mask & (1u64 << parent_i)) != 0 {
            values[parent_i] = model_values[parent_i];
            mask |= 1u64 << parent_i;
        }
    }

    for &name in &PURE_EXTENDED_CHANNELS {
        let ext_i = channel_index(name).unwrap();
        let src = sources.get(name).map(String::as_str).unwrap_or("model");
        if src == "off" {
            values[ext_i] = 0.0;
            mask |= 1u64 << ext_i;
        } else if src == "model" && (model_mask & (1u64 << ext_i)) != 0 {
            values[ext_i] = model_values[ext_i];
            mask |= 1u64 << ext_i;
        }
    }

    let ordered_native: Vec<Value> = CHANNELS
        .iter()
        .filter(|&&name| native_set.contains(name))
        .map(|&name| Value::String(name.to_string()))
        .collect();

    let mut ch_map = Map::new();
    for (i, &name) in CHANNELS.iter().enumerate() {
        if (mask & (1u64 << i)) != 0 {
            ch_map.insert(name.to_string(), json!(values[i]));
        }
    }

    let mut out = prediction.as_object().cloned().unwrap_or_default();
    let prev_state = out
        .get("state")
        .and_then(|v| v.as_str())
        .unwrap_or("untrained")
        .to_string();
    out.insert("model_values".to_string(), json!(model_values.to_vec()));
    out.insert("model_channels".to_string(), Value::Object(model_channels));
    out.insert("model_valid_mask".to_string(), json!(model_mask));
    out.insert("stock_channels".to_string(), Value::Object(stock_channels));
    out.insert("native_channels".to_string(), Value::Array(ordered_native));
    out.insert("values".to_string(), json!(values.to_vec()));
    out.insert("valid_mask".to_string(), json!(mask));
    out.insert("channels".to_string(), Value::Object(ch_map));
    out.insert("fresh".to_string(), json!(mask != 0));
    out.insert("override".to_string(), json!(mask != 0));
    out.insert(
        "state".to_string(),
        json!(if mask != 0 {
            "valid".to_string()
        } else {
            prev_state
        }),
    );
    Value::Object(out)
}

pub fn build_updated_config(
    base_config: &RuntimeConfig,
    changes: &Map<String, Value>,
) -> Result<RuntimeConfig, String> {
    let mut unknown: Vec<String> = changes
        .keys()
        .filter(|k| !KNOWN_CONFIG_KEYS.contains(&k.as_str()))
        .cloned()
        .collect();
    if !unknown.is_empty() {
        unknown.sort();
        return Err(format!("Unknown configuration fields: {}", unknown.join(", ")));
    }

    let mut updated = base_config.clone();
    for (key, value) in changes {
        match key.as_str() {
            "enabled" => updated.enabled = parse_bool_loose(value),
            "headset_host" => updated.headset_host = parse_host_string(value),
            "headset_port" => updated.headset_port = parse_port_value(value, "headset_port")?,
            "model_path" => updated.model_path = parse_string_field(value),
            "model_kind" => {
                let kind = value
                    .as_str()
                    .ok_or_else(|| "Unknown model kind".to_string())?;
                if !matches!(kind, "face50" | "segmentation" | "mouthlink") {
                    return Err("Unknown model kind".to_string());
                }
                updated.model_kind = kind.to_string();
            }
            "device" => updated.device = parse_string_field(value),
            "smoothing_ms" => {
                updated.smoothing_ms = parse_nonnegative_f64(value, "smoothing_ms")?
            }
            "filter_enabled" => updated.filter_enabled = parse_bool_loose(value),
            "filter_min_cutoff" => {
                updated.filter_min_cutoff = parse_positive_f64(value, "filter_min_cutoff")?
            }
            "filter_beta" => updated.filter_beta = parse_nonnegative_f64(value, "filter_beta")?,
            "filter_d_cutoff" => {
                updated.filter_d_cutoff = parse_positive_f64(value, "filter_d_cutoff")?
            }
            "uncertain_hold_ms" => {
                updated.uncertain_hold_ms = parse_nonnegative_f64(value, "uncertain_hold_ms")?
            }
            "stale_ms" => updated.stale_ms = parse_nonnegative_f64(value, "stale_ms")?,
            "preview_fps" => updated.preview_fps = parse_preview_fps(value)?,
            "size_hysteresis" => updated.size_hysteresis = parse_bool_loose(value),
            "replay_path" => updated.replay_path = parse_string_field(value),
            "mouth_mode" => {
                let mode = value.as_str().ok_or_else(|| {
                    "mouth_mode must be 'curated', 'hybrid', 'model_only', or 'custom'".to_string()
                })?;
                if !MOUTH_MODES.contains(&mode) {
                    return Err(
                        "mouth_mode must be 'curated', 'hybrid', 'model_only', or 'custom'"
                            .to_string(),
                    );
                }
                updated.mouth_mode = mode.to_string();
            }
            "hybrid_channels" => {
                if value.is_null() {
                    updated.hybrid_channels = None;
                } else {
                    let arr = value.as_array().ok_or_else(|| {
                        "hybrid_channels must be null or a unique list of native STOCK_CHANNELS"
                            .to_string()
                    })?;
                    let stock_set: HashSet<&str> = STOCK_CHANNELS.iter().copied().collect();
                    let mut seen = HashSet::new();
                    for item in arr {
                        let name = item.as_str().ok_or_else(|| {
                            "hybrid_channels must be null or a unique list of native STOCK_CHANNELS"
                                .to_string()
                        })?;
                        if !stock_set.contains(name) || !seen.insert(name) {
                            return Err(
                                "hybrid_channels must be null or a unique list of native STOCK_CHANNELS"
                                    .to_string(),
                            );
                        }
                    }
                    updated.hybrid_channels = Some(
                        STOCK_CHANNELS
                            .iter()
                            .filter(|&&name| seen.contains(name))
                            .map(|&s| s.to_string())
                            .collect(),
                    );
                }
            }
            "channel_sources" => {
                // Resolved below with mouth_mode interaction.
            }
            "output_enabled" => updated.output_enabled = parse_bool_loose(value),
            "output_host" => updated.output_host = parse_host_string(value),
            "output_port" => updated.output_port = parse_port_value(value, "output_port")?,
            _ => {}
        }
    }

    if let Some(raw_cs) = changes.get("channel_sources") {
        if raw_cs.is_null() {
            updated.channel_sources = None;
            if updated.mouth_mode == "custom" {
                return Err("mouth_mode 'custom' cannot have null channel_sources".to_string());
            }
        } else {
            let validated = validate_channel_sources(Some(raw_cs))?.unwrap_or_default();
            let mut base = if let Some(mm) = changes.get("mouth_mode").and_then(|v| v.as_str()) {
                if PRESET_MODES.contains(&mm) {
                    resolve_channel_sources(mm, None, None)
                } else {
                    resolve_channel_sources("curated", None, base_config.channel_sources.as_ref())
                }
            } else {
                resolve_channel_sources(
                    &base_config.mouth_mode,
                    base_config.hybrid_channels.as_deref(),
                    base_config.channel_sources.as_ref(),
                )
            };
            for (k, v) in validated {
                base.insert(k, v);
            }
            let mut full = BTreeMap::new();
            for &name in &ROUTABLE_CHANNELS {
                if let Some(src) = base.get(name) {
                    full.insert(name.to_string(), src.clone());
                }
            }
            let detected = matching_preset(&full);
            updated.mouth_mode = detected.to_string();
            updated.channel_sources = if PRESET_MODES.contains(&detected) {
                None
            } else {
                Some(full)
            };
            updated.hybrid_channels = None;
        }
    } else if changes.contains_key("mouth_mode") {
        if updated.mouth_mode == "custom" {
            let Some(ref existing) = base_config.channel_sources else {
                return Err("mouth_mode 'custom' requires channel_sources".to_string());
            };
            updated.channel_sources = Some(existing.clone());
            updated.hybrid_channels = None;
        } else {
            updated.channel_sources = None;
            if !changes.contains_key("hybrid_channels") {
                updated.hybrid_channels = None;
            }
        }
    } else if changes.contains_key("hybrid_channels") && updated.mouth_mode == "hybrid" {
        updated.channel_sources = None;
    }

    let filter_keys = [
        "filter_enabled",
        "filter_min_cutoff",
        "filter_beta",
        "filter_d_cutoff",
    ];
    let has_filter_change = filter_keys.iter().any(|k| changes.contains_key(*k));
    if has_filter_change && !changes.contains_key("smoothing_ms") {
        updated.smoothing_ms = if !updated.filter_enabled {
            0.0
        } else {
            round2(1000.0 / (2.0 * PI * updated.filter_min_cutoff))
        };
    } else if changes.contains_key("smoothing_ms") && !has_filter_change {
        let sm = updated.smoothing_ms;
        updated.filter_enabled = sm > 0.0;
        if sm > 0.0 {
            updated.filter_min_cutoff = round2(1000.0 / (2.0 * PI * sm));
        }
    }

    Ok(updated)
}

impl RuntimeInner {
    fn signal_inference_ready(&self) {
        let mut ready = self.inference_ready_state.lock().unwrap();
        *ready = true;
        self.inference_ready_cond.notify_all();
    }

    fn reset_inference_ready(&self) {
        let mut ready = self.inference_ready_state.lock().unwrap();
        *ready = false;
    }

    fn wait_inference_ready(&self, stop_flag: &AtomicBool) -> bool {
        let guard = self.inference_ready_state.lock().unwrap();
        let mut guard = guard;
        while !*guard && !stop_flag.load(Ordering::Acquire) {
            let (next, _) = self
                .inference_ready_cond
                .wait_timeout(guard, Duration::from_millis(50))
                .unwrap();
            guard = next;
        }
        *guard && !stop_flag.load(Ordering::Acquire)
    }

    fn request_clock_resync(&self) {
        let mut req = self.clock_state.lock().unwrap();
        *req = true;
        self.clock_cond.notify_all();
    }

    fn wake_clock_thread(&self) {
        let _guard = self.clock_state.lock().unwrap();
        self.clock_cond.notify_all();
    }

    fn set_clock(
        &self,
        lifecycle: u64,
        stop_flag: &AtomicBool,
        calibrated: ClockCalibration,
        domain: Option<&str>,
    ) {
        let mut state = self.state.lock().unwrap();
        if state.lifecycle != lifecycle || stop_flag.load(Ordering::Acquire) {
            return;
        }
        let mut row = serde_json::to_value(&calibrated).unwrap_or_else(|_| json!({}));
        if let (Some(d), Some(obj)) = (domain, row.as_object_mut()) {
            obj.insert("domain".to_string(), json!(d));
        }
        if let Some(ref mut log) = state.clock_log {
            if let Ok(line) = serde_json::to_string(&row) {
                let _ = writeln!(log, "{line}");
                let _ = log.flush();
            }
        }
        state.clock = Some(calibrated);
        state.clock_domain = domain.map(String::from);
    }
}

fn resolve_udp_target(host: &str, port: u16) -> Option<SocketAddr> {
    let trimmed = host.trim();
    if trimmed.is_empty() || port == 0 {
        return None;
    }
    let addrs = wire::resolve_host_cancellable(trimmed, port, Duration::from_secs(2), None).ok()?;
    addrs
        .iter()
        .find(|a| a.is_ipv4())
        .copied()
        .or_else(|| addrs.into_iter().next())
}

struct PendingUdpPacket {
    target: SocketAddr,
    bytes: Vec<u8>,
}

impl RuntimeInner {
    fn build_result_packet(
        state: &RuntimeState,
        header: &FrameHeader,
        prediction: &Value,
        age: f64,
    ) -> Option<PendingUdpPacket> {
        if !state.config.output_enabled {
            return None;
        }
        let target = state.udp_target.or_else(|| {
            let trimmed = state.config.output_host.trim();
            let literal = trimmed
                .strip_prefix('[')
                .and_then(|s| s.strip_suffix(']'))
                .unwrap_or(trimmed);
            literal
                .parse::<IpAddr>()
                .ok()
                .map(|ip| SocketAddr::new(ip, state.config.output_port))
        })?;
        let age_f32 = if age.is_finite() {
            age.max(0.0) as f32
        } else {
            state.config.stale_ms as f32
        };

        let bytes: Vec<u8> = if state.config.model_kind == "face50" {
            let mut vals = [0.0f32; 50];
            if let Some(arr) = prediction.get("values").and_then(|v| v.as_array()) {
                for (i, v) in arr.iter().take(50).enumerate() {
                    vals[i] = v.as_f64().unwrap_or(0.0) as f32;
                }
            }
            let valid_mask = parse_u64_json(prediction.get("valid_mask"));
            let fresh = prediction
                .get("fresh")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let pkt_state = if fresh { 0u32 } else { 1u32 };
            match wire::face_result_packet(
                header,
                state.output_session_id,
                &vals,
                valid_mask,
                age_f32,
                pkt_state,
            ) {
                Ok(arr) => arr.to_vec(),
                Err(_) => return None,
            }
        } else {
            let state_str = prediction
                .get("state")
                .and_then(|v| v.as_str())
                .unwrap_or("invalid");
            let override_active = prediction
                .get("override")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let mut code = match state_str {
                "present" => 1u32,
                "absent" => 2,
                "uncertain" => 4,
                "stopped" => 16,
                _ => 8,
            };
            if !override_active && code != 16 {
                code = 8;
            }
            let left = prediction.get("left").and_then(|v| v.as_f64()).unwrap_or(0.0) as f32;
            let right = prediction.get("right").and_then(|v| v.as_f64()).unwrap_or(0.0) as f32;
            let out = prediction.get("out").and_then(|v| v.as_f64()).unwrap_or(0.0) as f32;
            let up = prediction.get("up").and_then(|v| v.as_f64()).unwrap_or(0.0) as f32;
            let down = prediction.get("down").and_then(|v| v.as_f64()).unwrap_or(0.0) as f32;
            let roll = prediction.get("roll").and_then(|v| v.as_f64()).unwrap_or(0.0) as f32;
            wire::result_packet(
                header,
                state.output_session_id,
                code,
                left,
                right,
                out,
                age_f32,
                state.config.model_kind == "mouthlink",
                up,
                down,
                roll,
            )
        };

        Some(PendingUdpPacket { target, bytes })
    }

    fn send_udp_packet(&self, pkt: Option<PendingUdpPacket>) {
        let Some(pkt) = pkt else {
            return;
        };
        let mut udp_guard = self.udp.lock().unwrap();
        let sock_opt = if pkt.target.is_ipv4() {
            if udp_guard.v4.is_none() {
                udp_guard.v4 = UdpSocket::bind("0.0.0.0:0").ok();
            }
            udp_guard.v4.as_ref()
        } else {
            if udp_guard.v6.is_none() {
                udp_guard.v6 = UdpSocket::bind("[::]:0").ok();
            }
            udp_guard.v6.as_ref()
        };
        if let Some(sock) = sock_opt {
            let _ = sock.send_to(&pkt.bytes, pkt.target);
        }
    }

    fn publish(
        &self,
        lifecycle: u64,
        stop_flag: &AtomicBool,
        header: FrameHeader,
        payload: Vec<u8>,
        received_ns: i64,
    ) -> bool {
        let mut cancel_wizard = false;
        let mut udp_pkt = None;
        {
            let mut state = self.state.lock().unwrap();
            if state.lifecycle != lifecycle || stop_flag.load(Ordering::Acquire) {
                return false;
            }
            let (accepted, outcome, gaps) = state.order.accept(&header);
            if !accepted {
                match outcome {
                    AcceptOutcome::Duplicate => state.counts.duplicates += 1,
                    AcceptOutcome::OutOfOrder => state.counts.out_of_order += 1,
                    AcceptOutcome::RetiredSession => state.counts.retired_session += 1,
                    AcceptOutcome::NonAdvancingTime => state.counts.non_advancing_time += 1,
                    AcceptOutcome::NewSession | AcceptOutcome::NewSource => {}
                }
                return false;
            }
            if outcome == AcceptOutcome::NewSession {
                if let Some(last_hdr) = state.last_header.take() {
                    let kind = state.config.model_kind.clone();
                    let inv = invalid_prediction(&kind, &mut state.face_filter, "new_session");
                    state.prediction = inv.clone();
                    udp_pkt = Self::build_result_packet(&state, &last_hdr, &inv, 0.0);
                }
                state.output_session_id = unix_time_ns();
                state.preview = None;
                state.prediction_source = None;
                state.tongue.clear();
                state.model_reset_pending = true;
                cancel_wizard = true;
            }
            state.counts.received += 1;
            state.counts.source_gaps = state.counts.source_gaps.saturating_add(gaps);
            if state.received_times.len() >= 900 {
                state.received_times.pop_front();
            }
            state.received_times.push_back(received_ns);
            state.source = Some(header.clone());
            if state.pending.is_some() {
                state.counts.pending_dropped += 1;
            }
            state.pending = Some(PendingFrame {
                header: header.clone(),
                payload: payload.clone(),
                received_ns,
            });
            self.frame_cond.notify_one();
        }
        self.send_udp_packet(udp_pkt);
        if cancel_wizard && self.personal.has_active_wizard() {
            self.personal.cancel();
            self.personal
                .set_error("Camera session changed during calibration");
        }
        self.record_enqueue(RecordItem::Frame(header, payload));
        true
    }

    fn interrupt_active_socket_for_session(&self, lifecycle: u64) {
        let sock = {
            let mut state = self.state.lock().unwrap();
            if state.lifecycle != lifecycle {
                return;
            }
            state.active_socket.take()
        };
        if let Some(s) = sock {
            let _ = s.shutdown(Shutdown::Both);
        }
    }

    fn wants_psl(&self) -> bool {
        let state = self.state.lock().unwrap();
        if !state.running
            || state.stop_flag.load(Ordering::Acquire)
            || state.source_closed
            || state.mode != "live"
            || state.config.model_kind != "face50"
        {
            return false;
        }
        if state.record_wants_stock {
            return true;
        }
        if state.capture_only {
            return false;
        }
        let sources = resolve_channel_sources(
            &state.config.mouth_mode,
            state.config.hybrid_channels.as_deref(),
            state.config.channel_sources.as_ref(),
        );
        sources.values().any(|s| s == "stock")
    }

    fn stop_psl_worker_locked(&self, psl: &mut PslWorkerState) {
        psl.stop_flag.store(true, Ordering::SeqCst);
        let sock = {
            let mut state = self.state.lock().unwrap();
            state.psl_gen = state.psl_gen.wrapping_add(1);
            state.stock_socket.take()
        };
        if let Some(s) = sock {
            let _ = s.shutdown(Shutdown::Both);
        }
        if let Some(worker) = psl.thread.take() {
            wire::join_all_bounded(vec![worker], Duration::from_secs(5));
        }
        let mut state = self.state.lock().unwrap();
        state.latest_stock = None;
        state.stock_status.active = false;
    }

    fn sync_psl_worker(self: &Arc<Self>) {
        let mut psl = self.psl_lock.lock().unwrap();
        if psl.thread.is_some() && psl.finished.load(Ordering::Acquire) {
            if let Some(prev) = psl.thread.take() {
                wire::join_all_bounded(vec![prev], Duration::from_secs(5));
            }
        }
        if self.wants_psl() {
            if psl.thread.is_none() || psl.stop_flag.load(Ordering::Acquire) {
                if let Some(prev) = psl.thread.take() {
                    wire::join_all_bounded(vec![prev], Duration::from_secs(5));
                }
                let stock_stop = Arc::new(AtomicBool::new(false));
                let finished = Arc::new(AtomicBool::new(false));
                psl.stop_flag = Arc::clone(&stock_stop);
                psl.finished = Arc::clone(&finished);
                let (session_stop, psl_gen) = {
                    let mut state = self.state.lock().unwrap();
                    state.psl_gen = state.psl_gen.wrapping_add(1);
                    (Arc::clone(&state.stop_flag), state.psl_gen)
                };
                let inner_clone = Arc::clone(self);
                if let Ok(worker) = wire::spawn_bounded("mouth-stock", move || {
                    struct PslFinishGuard(Arc<AtomicBool>);
                    impl Drop for PslFinishGuard {
                        fn drop(&mut self) {
                            self.0.store(true, Ordering::Release);
                        }
                    }
                    let _finish_guard = PslFinishGuard(finished);
                    inner_clone.psl_worker_loop(psl_gen, stock_stop, session_stop);
                }) {
                    psl.thread = Some(worker);
                }
            }
        } else {
            self.stop_psl_worker_locked(&mut psl);
        }
    }

    fn psl_worker_loop(
        self: Arc<Self>,
        psl_gen: u64,
        stock_stop: Arc<AtomicBool>,
        session_stop: Arc<AtomicBool>,
    ) {
        let mut backoff = Duration::from_millis(500);
        while !stock_stop.load(Ordering::Acquire)
            && !session_stop.load(Ordering::Acquire)
            && self.wants_psl()
        {
            let (gen_ok, connected, before_records) = {
                let state = self.state.lock().unwrap();
                (
                    state.psl_gen == psl_gen,
                    state.connected,
                    state.stock_status.records,
                )
            };
            if !gen_ok {
                break;
            }
            if !connected {
                thread::sleep(Duration::from_millis(20));
                continue;
            }
            self.stock_receive(psl_gen, &stock_stop, &session_stop);
            let (gen_still_ok, received_any) = {
                let state = self.state.lock().unwrap();
                (
                    state.psl_gen == psl_gen,
                    state.stock_status.records > before_records,
                )
            };
            if !gen_still_ok {
                break;
            }
            if received_any {
                backoff = Duration::from_millis(500);
            }
            if !stock_stop.load(Ordering::Acquire)
                && !session_stop.load(Ordering::Acquire)
                && self.wants_psl()
            {
                let deadline = Instant::now() + backoff;
                while Instant::now() < deadline
                    && !stock_stop.load(Ordering::Acquire)
                    && !session_stop.load(Ordering::Acquire)
                {
                    let rem = deadline.saturating_duration_since(Instant::now());
                    thread::sleep(rem.min(Duration::from_millis(25)));
                }
                if !received_any {
                    backoff = (backoff * 2).min(Duration::from_secs(5));
                }
            }
        }
    }

    fn stock_receive(&self, psl_gen: u64, stock_stop: &AtomicBool, session_stop: &AtomicBool) {
        let (host, port) = {
            let state = self.state.lock().unwrap();
            if state.psl_gen != psl_gen {
                return;
            }
            (state.config.headset_host.trim().to_string(), state.config.headset_port)
        };
        if host.is_empty() {
            return;
        }
        let combined_stop = || stock_stop.load(Ordering::Acquire) || session_stop.load(Ordering::Acquire);

        let mut sock = match wire::connect_cancellable(
            &host,
            port,
            Duration::from_secs(3),
            Some(stock_stop),
        ) {
            Ok(s) => s,
            Err(WireError::Cancelled) => return,
            Err(err) => {
                if !combined_stop() {
                    let mut state = self.state.lock().unwrap();
                    if state.psl_gen == psl_gen {
                        state.stock_status.error = Some(err.to_string());
                    }
                }
                return;
            }
        };

        wire::enable_keepalive(&sock, 5000, 1000);
        let _ = sock.set_nodelay(true);
        let _ = sock.set_write_timeout(Some(Duration::from_secs(3)));

        {
            let mut state = self.state.lock().unwrap();
            if combined_stop() || state.psl_gen != psl_gen {
                return;
            }
            state.stock_socket = sock.try_clone().ok();
            state.stock_status.active = true;
            state.stock_status.error = None;
        }

        let res: Result<(), String> = (|| {
            sock.write_all(b"LABELS\n").map_err(|e| e.to_string())?;
            let mut last_record = Instant::now();
            while !combined_stop() {
                match read_stock_record(&mut sock, Duration::from_secs(2), Some(stock_stop)) {
                    Ok(record) => {
                        last_record = Instant::now();
                        let received_ns = wire::monotonic_ns();
                        let (keep_going, wants_record) = {
                            let mut state = self.state.lock().unwrap();
                            if combined_stop() || state.psl_gen != psl_gen {
                                (false, false)
                            } else {
                                state.latest_stock = Some((record.clone(), received_ns));
                                state.stock_status.records += 1;
                                if record.association_valid {
                                    state.stock_status.associated += 1;
                                } else {
                                    state.stock_status.unassociated += 1;
                                }
                                state.stock_status.error = None;
                                (true, state.record_wants_stock)
                            }
                        };
                        if !keep_going {
                            break;
                        }
                        if wants_record {
                            self.record_enqueue(RecordItem::Stock(record));
                        }
                    }
                    Err(WireError::IdleTimeout(_)) => {
                        if combined_stop() {
                            break;
                        }
                        if last_record.elapsed() >= Duration::from_secs(3) {
                            let mut state = self.state.lock().unwrap();
                            if state.psl_gen == psl_gen {
                                state.stock_status.error = Some(
                                    "Timed out waiting for headset mouth results (PSL1)"
                                        .to_string(),
                                );
                            }
                        }
                    }
                    Err(WireError::Cancelled) => break,
                    Err(err) => return Err(err.to_string()),
                }
            }
            Ok(())
        })();

        if let Err(err) = res {
            if !combined_stop() {
                let mut state = self.state.lock().unwrap();
                if state.psl_gen == psl_gen {
                    state.stock_status.error = Some(err);
                }
            }
        }

        let mut state = self.state.lock().unwrap();
        if state.psl_gen == psl_gen {
            state.stock_status.active = false;
            state.latest_stock = None;
            state.stock_socket = None;
        }
    }

    fn record_enqueue(&self, item: RecordItem) {
        let mut q = self.record_queue.lock().unwrap();
        if !q.worker_active || q.stopping {
            return;
        }
        if q.queue.len() >= RECORD_QUEUE_LIMIT {
            q.error = Some("Local recording queue overflow; recording stopped".to_string());
            q.stopping = true;
            let mut state = self.state.lock().unwrap();
            if let Some(obj) = state.record_status.as_object_mut() {
                obj.insert(
                    "error".to_string(),
                    json!("Local recording queue overflow; recording stopped"),
                );
            }
        } else {
            q.queue.push_back(item);
        }
        self.record_cond.notify_one();
    }

    fn record_worker_loop(self: Arc<Self>, recorder: Arc<WholeMouthRecording>) {
        let mut last_status = Instant::now() - Duration::from_secs(1);
        let run_err: Result<(), String> = (|| {
            loop {
                let item = {
                    let guard = self.record_queue.lock().unwrap();
                    let (mut guard, _) = self
                        .record_cond
                        .wait_timeout_while(guard, Duration::from_millis(200), |q| {
                            q.queue.is_empty() && !q.stopping
                        })
                        .unwrap();
                    if guard.queue.is_empty() && guard.stopping {
                        break;
                    }
                    guard.queue.pop_front()
                };

                if let Some(ev) = item {
                    match ev {
                        RecordItem::Frame(hdr, payload) => {
                            recorder.on_frame(&hdr, &payload)?;
                        }
                        RecordItem::Stock(rec) => {
                            recorder.on_stock(&rec)?;
                        }
                    }
                    if recorder.guide_automatic()
                        && recorder.guide_phase().as_deref() == Some("finished")
                    {
                        self.record_queue.lock().unwrap().stopping = true;
                    }
                }

                if last_status.elapsed() >= Duration::from_millis(200) {
                    let st = recorder.status();
                    self.state.lock().unwrap().record_status = st;
                    last_status = Instant::now();
                }
            }
            Ok(())
        })();

        let (cancelled, prev_err) = {
            let mut q = self.record_queue.lock().unwrap();
            if let Err(err) = run_err {
                q.error = Some(err);
            }
            q.worker_active = false;
            (q.cancelled, q.error.clone())
        };

        let final_res = if cancelled {
            recorder.cancel()
        } else {
            recorder.stop()
        };
        let mut final_status = final_res.unwrap_or_else(|_| recorder.status());
        if let Some(obj) = final_status.as_object_mut() {
            obj.insert(
                "error".to_string(),
                prev_err.map(Value::String).unwrap_or(Value::Null),
            );
        }
        {
            let mut state = self.state.lock().unwrap();
            state.record_status = final_status;
            state.record_wants_stock = false;
        }
        self.sync_psl_worker();
    }

    fn finish_recording(self: &Arc<Self>, cancel: bool) {
        let _ctrl = self.record_control.lock().unwrap();
        self.finish_recording_locked(cancel);
    }

    fn finish_recording_locked(self: &Arc<Self>, cancel: bool) {
        {
            let mut q = self.record_queue.lock().unwrap();
            q.cancelled = cancel;
            q.stopping = true;
            self.record_cond.notify_all();
        }
        {
            self.state.lock().unwrap().record_wants_stock = false;
        }
        self.sync_psl_worker();

        let handle = self.record_thread.lock().unwrap().take();
        if let Some(h) = handle {
            if h.thread().id() != thread::current().id() {
                let _ = h.join();
            }
        }
        let recorder = self.recorder.lock().unwrap().clone();
        if let Some(rec) = recorder {
            let err = self.record_queue.lock().unwrap().error.clone();
            let mut st = rec.status();
            if let Some(obj) = st.as_object_mut() {
                obj.insert(
                    "error".to_string(),
                    err.map(Value::String).unwrap_or(Value::Null),
                );
            }
            self.state.lock().unwrap().record_status = st;
        }
    }

    fn bind_capture_inner(
        &self,
        capture_session_id: u64,
        service_generation: Value,
        evidence: Value,
        session_guard: Option<(u64, &AtomicBool)>,
    ) -> Result<bool, String> {
        if service_generation.is_null() || evidence.is_null() {
            return Err("Capture binding requires explicit trusted transport metadata".to_string());
        }
        let binding = TransportBinding {
            service_generation: service_generation.clone(),
            evidence: evidence.clone(),
        };
        {
            let mut state = self.state.lock().unwrap();
            if let Some((lifecycle, stop_flag)) = session_guard {
                if state.lifecycle != lifecycle || stop_flag.load(Ordering::Acquire) {
                    return Ok(false);
                }
            }
            if let Some(prev) = state.transport_bindings.get(&capture_session_id) {
                if *prev != binding {
                    return Err("Capture session cannot change service generation".to_string());
                }
            }
            state.transport_bindings.insert(capture_session_id, binding);
        }
        let worker_active = self.record_queue.lock().unwrap().worker_active;
        if worker_active {
            if let Some(rec) = self.recorder.lock().unwrap().clone() {
                if let Some((lifecycle, stop_flag)) = session_guard {
                    let state = self.state.lock().unwrap();
                    if state.lifecycle != lifecycle || stop_flag.load(Ordering::Acquire) {
                        return Ok(false);
                    }
                }
                rec.bind_capture(capture_session_id, service_generation, evidence)?;
            }
        }
        Ok(true)
    }

    fn receive_loop(self: Arc<Self>, lifecycle: u64, stop_flag: Arc<AtomicBool>) {
        if !self.wait_inference_ready(&stop_flag) {
            return;
        }
        let run_result = self.run_receive(lifecycle, &stop_flag);
        if let Err(err) = run_result {
            if !stop_flag.load(Ordering::Acquire) {
                let mut state = self.state.lock().unwrap();
                if state.lifecycle == lifecycle && !stop_flag.load(Ordering::Acquire) {
                    state.last_error = Some(err);
                }
            }
        }

        stop_flag.store(true, Ordering::SeqCst);
        self.wake_clock_thread();
        let (should_stop_recording, udp_pkt) = {
            let mut state = self.state.lock().unwrap();
            if state.lifecycle == lifecycle {
                if let Some(mut stream) = state.input_stream.take() {
                    let _ = stream.flush();
                }
                state.running = false;
                state.connected = false;
                state.mode = "idle".to_string();
                state.config.enabled = false;
                state.source_closed = true;
                state.active_socket = None;
                state.model_version += 1;
                state.pending = None;
                let kind = state.config.model_kind.clone();
                let inv = invalid_prediction(&kind, &mut state.face_filter, "disconnected");
                state.prediction = inv.clone();
                state.preview = None;
                let pkt = state
                    .last_header
                    .take()
                    .and_then(|last_hdr| Self::build_result_packet(&state, &last_hdr, &inv, 0.0));
                self.frame_cond.notify_all();
                (true, pkt)
            } else {
                (false, None)
            }
        };
        self.send_udp_packet(udp_pkt);
        if should_stop_recording {
            self.finish_recording(false);
        }
    }

    fn run_receive(&self, lifecycle: u64, stop_flag: &AtomicBool) -> Result<(), String> {
        let (host, port, model_kind) = {
            let state = self.state.lock().unwrap();
            if state.lifecycle != lifecycle || stop_flag.load(Ordering::Acquire) {
                return Ok(());
            }
            (
                state.config.headset_host.trim().to_string(),
                state.config.headset_port,
                state.config.model_kind.clone(),
            )
        };
        if stop_flag.load(Ordering::Acquire) {
            return Ok(());
        }

        match wire::sync_clock(&host, port, 12, Some(stop_flag)) {
            Ok(calibrated) => self.set_clock(lifecycle, stop_flag, calibrated, None),
            Err(WireError::Cancelled) => return Ok(()),
            Err(err) => return Err(err.to_string()),
        }
        if stop_flag.load(Ordering::Acquire) {
            return Ok(());
        }

        let mut context: Option<CaptureContext> = None;
        if model_kind == "face50" {
            let mut control = match wire::connect_cancellable(
                &host,
                port,
                Duration::from_secs(3),
                Some(stop_flag),
            ) {
                Ok(s) => s,
                Err(WireError::Cancelled) => return Ok(()),
                Err(err) => return Err(err.to_string()),
            };
            let ctx = match read_capture_context(&mut control, Duration::from_secs(3), Some(stop_flag)) {
                Ok(c) => c,
                Err(WireError::Cancelled) => return Ok(()),
                Err(err) => return Err(err.to_string()),
            };
            if stop_flag.load(Ordering::Acquire) {
                return Ok(());
            }
            let evidence = ctx.evidence();
            let evidence_val = serde_json::to_value(&evidence).unwrap_or(Value::Null);
            let bound = self.bind_capture_inner(
                ctx.capture_session_id,
                json!(ctx.service_generation),
                evidence_val,
                Some((lifecycle, stop_flag)),
            )?;
            if !bound {
                return Ok(());
            }
            {
                let mut state = self.state.lock().unwrap();
                if state.lifecycle != lifecycle || stop_flag.load(Ordering::Acquire) {
                    return Ok(());
                }
                state.capture_context = Some(evidence);
            }
            context = Some(ctx);
        }
        if stop_flag.load(Ordering::Acquire) {
            return Ok(());
        }

        let mut sock = match wire::connect_cancellable(
            &host,
            port,
            Duration::from_secs(3),
            Some(stop_flag),
        ) {
            Ok(s) => s,
            Err(WireError::Cancelled) => return Ok(()),
            Err(err) => return Err(err.to_string()),
        };

        wire::enable_keepalive(&sock, 5000, 1000);
        let _ = sock.set_nodelay(true);
        let _ = sock.set_write_timeout(Some(Duration::from_secs(3)));

        {
            let mut state = self.state.lock().unwrap();
            if state.lifecycle != lifecycle || stop_flag.load(Ordering::Acquire) {
                return Ok(());
            }
            state.active_socket = sock.try_clone().ok();
            state.connected = true;
        }

        sock.write_all(b"JPEG\n").map_err(|e| e.to_string())?;

        let mut first_frame = true;
        let mut was_idle = false;
        while !stop_flag.load(Ordering::Acquire) {
            let timeout = if first_frame {
                Duration::from_secs(10)
            } else {
                Duration::from_secs(1)
            };
            let (header, payload) = match wire::read_frame(&mut sock, timeout, Some(stop_flag)) {
                Ok(frame) => frame,
                Err(WireError::IdleTimeout(_)) => {
                    was_idle = true;
                    first_frame = false;
                    continue;
                }
                Err(WireError::Cancelled) => break,
                Err(err) => return Err(err.to_string()),
            };
            if first_frame {
                first_frame = false;
            }
            if was_idle {
                was_idle = false;
                self.request_clock_resync();
            }
            let received_ns = wire::monotonic_ns();
            if let Some(ref ctx) = context {
                ctx.require_session(header.session_id)
                    .map_err(|e| e.to_string())?;
            }
            if (header.flags & END) != 0 {
                let mut state = self.state.lock().unwrap();
                if state.lifecycle == lifecycle {
                    state.replay.finished = true;
                }
                break;
            }
            if (header.flags & (JPEG | RAW)) == 0 {
                return Err("JPEG subscription returned a non-JPEG frame".to_string());
            }
            {
                let mut state = self.state.lock().unwrap();
                if state.lifecycle != lifecycle || stop_flag.load(Ordering::Acquire) {
                    break;
                }
                if let Some(ref mut stream) = state.input_stream {
                    let _ = stream.write_all(&header.pack());
                    let _ = stream.write_all(&payload);
                }
            }
            self.publish(lifecycle, stop_flag, header, payload, received_ns);
        }
        Ok(())
    }

    fn clock_refresh_loop(self: Arc<Self>, lifecycle: u64, stop_flag: Arc<AtomicBool>) {
        let mut last_sync_mono: Option<Instant> = None;
        while !stop_flag.load(Ordering::Acquire) {
            let forced = {
                let guard = self.clock_state.lock().unwrap();
                let (mut guard, _) = self
                    .clock_cond
                    .wait_timeout_while(guard, Duration::from_secs(10), |requested| {
                        !*requested && !stop_flag.load(Ordering::Acquire)
                    })
                    .unwrap();
                if stop_flag.load(Ordering::Acquire) {
                    break;
                }
                let was_forced = *guard;
                *guard = false;
                was_forced
            };
            if stop_flag.load(Ordering::Acquire) {
                break;
            }
            if forced {
                if let Some(prev) = last_sync_mono {
                    let elapsed = prev.elapsed();
                    if elapsed < Duration::from_millis(500) {
                        let wait = Duration::from_millis(500) - elapsed;
                        let guard = self.clock_state.lock().unwrap();
                        let _ = self
                            .clock_cond
                            .wait_timeout_while(guard, wait, |_| {
                                !stop_flag.load(Ordering::Acquire)
                            })
                            .unwrap();
                        if stop_flag.load(Ordering::Acquire) {
                            break;
                        }
                    }
                }
            }
            last_sync_mono = Some(Instant::now());

            let (host, port) = {
                let state = self.state.lock().unwrap();
                if state.lifecycle != lifecycle || stop_flag.load(Ordering::Acquire) {
                    break;
                }
                (
                    state.config.headset_host.trim().to_string(),
                    state.config.headset_port,
                )
            };
            if host.is_empty() {
                continue;
            }
            match wire::sync_clock(&host, port, 3, Some(&stop_flag)) {
                Ok(calibrated) => self.set_clock(lifecycle, &stop_flag, calibrated, None),
                Err(WireError::Cancelled) => break,
                Err(err) => {
                    let mut state = self.state.lock().unwrap();
                    if state.lifecycle == lifecycle && !stop_flag.load(Ordering::Acquire) {
                        if let Some(obj) = state.description.as_object_mut() {
                            obj.insert("clock_error".to_string(), json!(err.to_string()));
                        }
                    }
                }
            }
        }
        let mut state = self.state.lock().unwrap();
        if state.lifecycle == lifecycle && stop_flag.load(Ordering::Acquire) {
            state.clock_log = None;
        }
    }

    fn replay_loop(self: Arc<Self>, path: PathBuf, lifecycle: u64, stop_flag: Arc<AtomicBool>) {
        if !self.wait_inference_ready(&stop_flag) {
            return;
        }
        let res: Result<(), String> = (|| {
            let dataset = load_npz_replay(&path)?;
            let count = dataset.len();
            let epoch = wire::monotonic_ns() + 100_000_000;
            let session = unix_time_ns();
            let replay_clock = ClockCalibration {
                offset_ns: 0.0,
                uncertainty_ms: 0.0,
                calibrated_pc_ns: wire::monotonic_ns(),
                samples: Vec::new(),
            };
            self.set_clock(lifecycle, &stop_flag, replay_clock, Some("pc-replay"));
            {
                let mut state = self.state.lock().unwrap();
                if state.lifecycle != lifecycle || stop_flag.load(Ordering::Acquire) {
                    return Ok(());
                }
                state.connected = true;
                state.replay.total = Some(count);
            }

            let t0 = if count > 0 { dataset.times[0][2] } else { 0 };
            for (index, frame) in dataset.iter().enumerate() {
                let offset_ns = (frame.times[2] as i128 - t0 as i128) as i64;
                let due = epoch + offset_ns;
                loop {
                    if stop_flag.load(Ordering::Acquire) {
                        return Ok(());
                    }
                    let now = wire::monotonic_ns();
                    if now >= due {
                        break;
                    }
                    let rem_ns = (due - now) as u64;
                    let sleep_dur = Duration::from_nanos(rem_ns).min(Duration::from_millis(10));
                    thread::sleep(sleep_dur);
                }
                if stop_flag.load(Ordering::Acquire) {
                    break;
                }
                let copied = wire::monotonic_ns();
                if copied - due > 11_111_111 {
                    let mut state = self.state.lock().unwrap();
                    if state.lifecycle != lifecycle {
                        break;
                    }
                    *state.counts.replay_late_skipped.get_or_insert(0) += 1;
                    continue;
                }
                let begin = wire::monotonic_ns();
                let payload = encode_gray_jpeg(frame.image, 95)?;
                let encoded = wire::monotonic_ns();
                let header = FrameHeader {
                    payload_bytes: payload.len() as u32,
                    flags: JPEG | REPLAY,
                    session_id: session,
                    source_seq: frame.seq,
                    sensor_ns: frame.times[2],
                    vendor_ns: frame.vendor_ns,
                    copy_mono_ns: copied.max(0) as u64,
                    encode_begin_ns: begin.max(0) as u64,
                    encode_end_ns: encoded.max(0) as u64,
                    emit_mono_ns: encoded.max(0) as u64,
                    eye0_sensor_ns: frame.times[0],
                    eye1_sensor_ns: frame.times[1],
                    mouth_sensor_ns: frame.times[2],
                    raw_identity: frame.raw_identity,
                    width: 400,
                    height: 400,
                    raw_crc32: crc32fast::hash(frame.image),
                    quality: 95,
                };
                if stop_flag.load(Ordering::Acquire) {
                    break;
                }
                self.publish(lifecycle, &stop_flag, header, payload, encoded);
                {
                    let mut state = self.state.lock().unwrap();
                    if state.lifecycle == lifecycle {
                        state.replay.position = index + 1;
                    }
                }
            }
            let mut state = self.state.lock().unwrap();
            if state.lifecycle == lifecycle {
                state.replay.finished = !stop_flag.load(Ordering::Acquire);
            }
            Ok(())
        })();

        if let Err(err) = res {
            let mut state = self.state.lock().unwrap();
            if state.lifecycle == lifecycle {
                state.last_error = Some(err);
            }
        }
        let mut state = self.state.lock().unwrap();
        if state.lifecycle == lifecycle {
            state.connected = false;
            self.frame_cond.notify_all();
        }
    }

    fn activate_personal_profile(
        &self,
        lifecycle: u64,
        stop_flag: &AtomicBool,
    ) -> Result<(), String> {
        let request = self.personal.pending();
        let (profile, version, mut config) = {
            let state = self.state.lock().unwrap();
            if stop_flag.load(Ordering::Acquire) || state.lifecycle != lifecycle {
                return Ok(());
            }
            if request.is_none() && !state.profile_reload {
                return Ok(());
            }
            let profile = match &request {
                Some(req) => req.profile.clone(),
                None => self.personal.profile(),
            };
            (profile, state.model_version, state.config.clone())
        };
        if let Some(ref head) = profile.head {
            config.model_path = head.clone();
        }
        let loaded = (|| -> Result<(Box<dyn MouthModel>, Value), String> {
            let mut model = (self.model_factory)(&config)?;
            let zeros = vec![0u8; 400 * 400];
            let ctx = PredictContext {
                stale_ms: config.stale_ms,
                session_id: None,
                source_ns: None,
            };
            for _ in 0..2 {
                let _ = model.predict(&zeros, ctx)?;
            }
            let desc = model.describe();
            Ok((model, desc))
        })();

        let (model, mut desc) = match loaded {
            Ok(pair) => pair,
            Err(err) => {
                if let Some(req) = request {
                    self.personal.fail_pending(&req, &err);
                    return Ok(());
                }
                return Err(err);
            }
        };

        if stop_flag.load(Ordering::Acquire) {
            return Ok(());
        }
        let standard_ckpt = {
            let state = self.state.lock().unwrap();
            if state.lifecycle != lifecycle || state.model_version != version {
                return Ok(());
            }
            state.config.model_path.clone()
        };
        let mut expected_version = version;
        if let Some(ref req) = request {
            match self.personal.commit_pending(req) {
                Ok(true) => {
                    // commit_pending invokes on_changed(true), incrementing model_version by 1.
                    expected_version = version.wrapping_add(1);
                }
                Ok(false) => return Ok(()),
                Err(err) => {
                    self.personal.fail_pending(req, &err);
                    return Ok(());
                }
            }
        }
        if let Some(obj) = desc.as_object_mut() {
            obj.insert("standard_checkpoint".to_string(), json!(standard_ckpt));
            obj.insert("personal_head".to_string(), json!(profile.head.is_some()));
        }
        let mut state = self.state.lock().unwrap();
        if stop_flag.load(Ordering::Acquire)
            || state.lifecycle != lifecycle
            || state.model_version != expected_version
        {
            return Ok(());
        }
        {
            let mut model_guard = self.model.lock().unwrap();
            *model_guard = Some(model);
        }
        state.model_key = Some((config.model_path, config.device, config.model_kind));
        state.description = desc;
        state.profile_reload = false;
        Ok(())
    }

    fn expire_locked(&self, state: &mut RuntimeState) -> Option<PendingUdpPacket> {
        let fresh = state
            .prediction
            .get("fresh")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let last_hdr = state.last_header.clone()?;
        if !fresh {
            return None;
        }
        let now = wire::monotonic_ns();
        let (_, upper) = calc_age(state.clock.as_ref(), &state.mode, &last_hdr, now);
        if upper > state.config.stale_ms {
            let kind = state.config.model_kind.clone();
            let inv = invalid_prediction(&kind, &mut state.face_filter, "stale");
            state.prediction = inv.clone();
            state.tongue.clear();
            state.model_reset_pending = true;
            return Self::build_result_packet(state, &last_hdr, &inv, upper);
        }
        None
    }

    fn inference_loop(self: Arc<Self>, lifecycle: u64, stop_flag: Arc<AtomicBool>) {
        let result: Result<(), String> = (|| {
            let (capture_only, init_stale_ms) = {
                let state = self.state.lock().unwrap();
                (state.capture_only, state.config.stale_ms)
            };
            if !capture_only {
                let zeros = vec![0u8; 400 * 400];
                let ctx = PredictContext {
                    stale_ms: init_stale_ms,
                    session_id: None,
                    source_ns: None,
                };
                let mut model_guard = self.model.lock().unwrap();
                let model = model_guard
                    .as_mut()
                    .ok_or_else(|| "Model is not prepared".to_string())?;
                for _ in 0..2 {
                    let _ = model.predict(&zeros, ctx)?;
                }
                model.reset_state();
            }
            self.signal_inference_ready();

            while !stop_flag.load(Ordering::Acquire) {
                let is_seg = self.state.lock().unwrap().config.model_kind == "segmentation";
                if is_seg {
                    self.activate_personal_profile(lifecycle, &stop_flag)?;
                }

                let (expired_pkt, pending_item) = {
                    let guard = self.state.lock().unwrap();
                    let (mut guard, _) = self
                        .frame_cond
                        .wait_timeout_while(guard, Duration::from_millis(50), |st| {
                            st.lifecycle == lifecycle
                                && st.pending.is_none()
                                && !stop_flag.load(Ordering::Acquire)
                        })
                        .unwrap();
                    if guard.lifecycle != lifecycle || stop_flag.load(Ordering::Acquire) {
                        break;
                    }
                    let expired_pkt = self.expire_locked(&mut guard);
                    let seg_profile_pending = guard.config.model_kind == "segmentation"
                        && (guard.profile_reload || self.personal.pending().is_some());
                    if guard.pending.is_none() || seg_profile_pending {
                        (expired_pkt, None)
                    } else {
                        let pending = guard.pending.take().unwrap();
                        let reset = guard.model_reset_pending;
                        guard.model_reset_pending = false;
                        (
                            expired_pkt,
                            Some((
                                pending,
                                guard.config.clone(),
                                guard.model_version,
                                guard.mode.clone(),
                                guard.clock.clone(),
                                reset,
                            )),
                        )
                    }
                };
                self.send_udp_packet(expired_pkt);
                let Some((pending, config, model_version, mode, clock, reset_model)) = pending_item
                else {
                    continue;
                };

                let begin = wire::monotonic_ns();
                let (_age0, upper0) = calc_age(clock.as_ref(), &mode, &pending.header, begin);
                if upper0 > config.stale_ms {
                    if mode == "live" && ((begin - pending.received_ns) as f64) / 1e6 <= config.stale_ms {
                        self.request_clock_resync();
                    }
                    let mut state = self.state.lock().unwrap();
                    if state.lifecycle == lifecycle {
                        state.counts.stale += 1;
                    }
                    continue;
                }

                let decode_and_predict: Result<(Vec<u8>, i64, Option<ModelOutput>), String> = (|| {
                    let gray400 = decode_frame_gray400(&pending.header, &pending.payload)?;
                    let decoded = wire::monotonic_ns();
                    if capture_only {
                        Ok((gray400, decoded, None))
                    } else {
                        let ctx = PredictContext {
                            stale_ms: config.stale_ms,
                            session_id: Some(pending.header.session_id),
                            source_ns: Some(pending.header.mouth_sensor_ns),
                        };
                        let mut model_guard = self.model.lock().unwrap();
                        let model = model_guard
                            .as_mut()
                            .ok_or_else(|| "Model is not loaded".to_string())?;
                        if reset_model {
                            model.reset_state();
                        }
                        let out = model.predict(&gray400, ctx)?;
                        Ok((gray400, decoded, Some(out)))
                    }
                })();

                let (gray400, decoded, model_output) = match decode_and_predict {
                    Ok(v) => v,
                    Err(err) => {
                        let mut state = self.state.lock().unwrap();
                        if state.lifecycle != lifecycle
                            || stop_flag.load(Ordering::Acquire)
                            || state.order.session != Some(pending.header.session_id)
                            || model_version != state.model_version
                        {
                            if state.lifecycle == lifecycle {
                                state.counts.superseded += 1;
                            }
                            continue;
                        }
                        state.counts.decode_errors += 1;
                        return Err(err);
                    }
                };

                let finished = wire::monotonic_ns();
                let cal = self.personal.profile().calibration;

                // Feed personalization wizard outside state lock for segmentation if fresh.
                if config.model_kind == "segmentation" {
                    let (_, upper_chk) = {
                        let state = self.state.lock().unwrap();
                        calc_age(state.clock.as_ref(), &state.mode, &pending.header, finished)
                    };
                    if upper_chk <= config.stale_ms {
                        if let Some(ModelOutput::Segmentation(ref seg)) = model_output {
                            let _ = self.personal.feed(&gray400, &seg.to_wizard_raw(), &pending.header);
                        }
                    }
                }

                let (
                    frame_prediction,
                    output_session_id,
                    preview_version,
                    should_preview,
                    age,
                    upper,
                    published,
                    udp_pkt,
                ) = {
                    let mut state = self.state.lock().unwrap();
                    if state.lifecycle != lifecycle
                        || stop_flag.load(Ordering::Acquire)
                        || state.order.session != Some(pending.header.session_id)
                        || model_version != state.model_version
                    {
                        if state.lifecycle == lifecycle {
                            state.counts.superseded += 1;
                        }
                        continue;
                    }

                    let (_age1, upper1) =
                        calc_age(state.clock.as_ref(), &state.mode, &pending.header, finished);
                    state.counts.decoded += 1;
                    if !state.capture_only {
                        state.counts.inferred += 1;
                        if state.inferred_times.len() >= 900 {
                            state.inferred_times.pop_front();
                        }
                        state.inferred_times.push_back(finished);
                    }
                    if upper1 > config.stale_ms {
                        state.tongue.clear();
                    }

                    let (raw_meta_obj, mut prediction) = if config.model_kind == "face50" {
                        let (vals, mask, meta) = match &model_output {
                            Some(ModelOutput::Face50(fp)) => (
                                fp.values,
                                fp.valid_mask,
                                serde_json::to_value(fp).unwrap_or_else(|_| json!({})),
                            ),
                            None => ([0.0f32; 50], 0u64, json!({})),
                            Some(_) => {
                                return Err("Model output kind does not match face50".to_string());
                            }
                        };
                        let base_pred = if upper1 <= config.stale_ms {
                            validate_face_prediction(&vals, mask, &state.description)?
                        } else {
                            invalid_prediction("face50", &mut state.face_filter, "stale")
                        };
                        let filtered = state.face_filter.apply_json(
                            &base_pred,
                            &pending.header,
                            &config.filter_config(),
                        );
                        let merged = if !state.capture_only && upper1 <= config.stale_ms {
                            merge_mouth_prediction(
                                &filtered,
                                &pending.header,
                                &config,
                                state.latest_stock.as_ref(),
                                state.clock.as_ref(),
                                wire::monotonic_ns(),
                            )
                        } else {
                            filtered
                        };
                        (meta, merged)
                    } else {
                        let (mut raw_json, raw_input) = match &model_output {
                            Some(ModelOutput::MouthLink(ml)) => (
                                serde_json::to_value(ml).unwrap_or_else(|_| json!({})),
                                ml.to_raw_tongue_input(),
                            ),
                            Some(ModelOutput::Segmentation(seg)) => {
                                let applied = apply_calibration(
                                    seg.geometry.raw_angle,
                                    seg.geometry.mask_area,
                                    &cal,
                                    state.tongue.is_visual_active(),
                                    config.size_hysteresis,
                                );
                                let mut meta =
                                    serde_json::to_value(seg).unwrap_or_else(|_| json!({}));
                                if let Some(obj) = meta.as_object_mut() {
                                    obj.insert("present".to_string(), json!(applied.present));
                                    obj.insert("angle".to_string(), json!(applied.angle));
                                    obj.insert("position".to_string(), json!(applied.position));
                                    obj.insert("left".to_string(), json!(applied.left));
                                    obj.insert("right".to_string(), json!(applied.right));
                                    obj.insert("out".to_string(), json!(applied.out));
                                    obj.insert(
                                        "area_threshold".to_string(),
                                        json!(applied.area_threshold),
                                    );
                                    obj.insert("size_held".to_string(), json!(applied.size_held));
                                }
                                let inp = RawTongueInput {
                                    state: None,
                                    present: applied.present,
                                    left: applied.left,
                                    right: applied.right,
                                    out: applied.out,
                                    extended: false,
                                    up: None,
                                    down: None,
                                    roll: None,
                                    out_hysteresis: None,
                                };
                                (meta, inp)
                            }
                            _ => (json!({}), RawTongueInput::default()),
                        };
                        if let Some(obj) = raw_json.as_object_mut() {
                            obj.remove("mask");
                        }
                        let tongue_pred: TonguePrediction = if upper1 <= config.stale_ms {
                            state
                                .tongue
                                .update(&raw_input, &pending.header, &config.tongue_config())
                        } else {
                            TongueState::invalid("stale")
                        };
                        (
                            raw_json,
                            serde_json::to_value(tongue_pred).unwrap_or_else(|_| json!({})),
                        )
                    };

                    let published = wire::monotonic_ns();
                    let (age2, upper2) =
                        calc_age(state.clock.as_ref(), &state.mode, &pending.header, published);
                    if upper2 > config.stale_ms {
                        let kind = config.model_kind.clone();
                        state.tongue.clear();
                        state.model_reset_pending = true;
                        prediction = invalid_prediction(&kind, &mut state.face_filter, "stale");
                    }

                    let mut combined = raw_meta_obj.as_object().cloned().unwrap_or_default();
                    combined.remove("mask");
                    if let Some(pred_obj) = prediction.as_object() {
                        for (k, v) in pred_obj {
                            combined.insert(k.clone(), v.clone());
                        }
                    }
                    let frame_pred = Value::Object(combined);
                    state.prediction = frame_pred.clone();
                    let out_sid = state.output_session_id;
                    let pv = state.model_version;
                    state.prediction_source = Some(pending.header.clone());
                    state.last_header = Some(pending.header.clone());
                    state.last_result_ns = published;
                    if state.latencies.len() >= 2700 {
                        state.latencies.pop_front();
                    }
                    state.latencies.push_back(LatencySample {
                        source_copy_to_result_ms: age2,
                        queue_ms: ((begin - pending.received_ns) as f64) / 1e6,
                        pc_total_ms: ((published - pending.received_ns) as f64) / 1e6,
                        filter_ms: ((published - finished) as f64) / 1e6,
                        decode_ms: ((decoded - begin) as f64) / 1e6,
                        predict_ms: ((finished - decoded) as f64) / 1e6,
                    });
                    let udp_pkt =
                        Self::build_result_packet(&state, &pending.header, &prediction, upper2);

                    let min_interval_ns = 1e9 / (config.preview_fps.max(1) as f64);
                    let due = state.last_preview_ns == 0
                        || ((finished - state.last_preview_ns) as f64) >= min_interval_ns;
                    (frame_pred, out_sid, pv, due, age2, upper2, published, udp_pkt)
                };
                self.send_udp_packet(udp_pkt);

                if should_preview {
                    let seg_mask = match &model_output {
                        Some(ModelOutput::Segmentation(s)) => Some(s.geometry.mask.as_slice()),
                        _ => None,
                    };
                    if let Ok(preview_bytes) =
                        build_preview_jpeg(&gray400, &config.model_kind, seg_mask)
                    {
                        let mut state = self.state.lock().unwrap();
                        if state.lifecycle == lifecycle
                            && !stop_flag.load(Ordering::Acquire)
                            && state.order.session == Some(pending.header.session_id)
                            && preview_version == state.model_version
                        {
                            state.preview = Some(preview_bytes);
                            state.last_preview_ns = finished;
                        }
                    }
                }

                let entry = json!({
                    "payload_bytes": pending.header.payload_bytes,
                    "flags": pending.header.flags,
                    "session_id": pending.header.session_id,
                    "source_seq": pending.header.source_seq,
                    "sensor_ns": pending.header.sensor_ns,
                    "vendor_ns": pending.header.vendor_ns,
                    "copy_mono_ns": pending.header.copy_mono_ns,
                    "encode_begin_ns": pending.header.encode_begin_ns,
                    "encode_end_ns": pending.header.encode_end_ns,
                    "emit_mono_ns": pending.header.emit_mono_ns,
                    "eye0_sensor_ns": pending.header.eye0_sensor_ns,
                    "eye1_sensor_ns": pending.header.eye1_sensor_ns,
                    "mouth_sensor_ns": pending.header.mouth_sensor_ns,
                    "raw_identity": pending.header.raw_identity,
                    "width": pending.header.width,
                    "height": pending.header.height,
                    "raw_crc32": pending.header.raw_crc32,
                    "quality": pending.header.quality,
                    "received_pc_ns": pending.received_ns,
                    "decode_begin_pc_ns": begin,
                    "decoded_pc_ns": decoded,
                    "inferred_pc_ns": finished,
                    "published_pc_ns": published,
                    "source_age_ms": if age.is_finite() { f64_to_json(age) } else { Value::Null },
                    "source_age_upper_ms": if upper.is_finite() { f64_to_json(upper) } else { Value::Null },
                    "output_session_id": output_session_id,
                    "prediction": dataset_json_value(&frame_prediction),
                });
                {
                    let mut state = self.state.lock().unwrap();
                    if state.lifecycle == lifecycle {
                        if let Some(ref mut writer) = state.log_writer {
                            if let Ok(line) = serde_json::to_string(&entry) {
                                let _ = writeln!(writer, "{line}");
                            }
                        }
                    }
                }
            }
            Ok(())
        })();

        if let Err(err) = result {
            self.signal_inference_ready();
            stop_flag.store(true, Ordering::SeqCst);
            self.wake_clock_thread();
            let should_sync = {
                let mut state = self.state.lock().unwrap();
                if state.lifecycle == lifecycle {
                    state.last_error = Some(format!("RuntimeError: {err}"));
                    true
                } else {
                    false
                }
            };
            if should_sync {
                self.interrupt_active_socket_for_session(lifecycle);
                let err_pkt = {
                    let mut state = self.state.lock().unwrap();
                    if state.lifecycle == lifecycle {
                        let kind = state.config.model_kind.clone();
                        let inv = invalid_prediction(&kind, &mut state.face_filter, "error");
                        state.prediction = inv.clone();
                        state.running = false;
                        state.connected = false;
                        state.mode = "idle".to_string();
                        state.config.enabled = false;
                        state
                            .last_header
                            .take()
                            .and_then(|last_hdr| Self::build_result_packet(&state, &last_hdr, &inv, 0.0))
                    } else {
                        None
                    }
                };
                self.send_udp_packet(err_pkt);
                self.sync_psl_worker();
            }
        }

        let mut state = self.state.lock().unwrap();
        if state.lifecycle == lifecycle && stop_flag.load(Ordering::Acquire) {
            if let Some(mut writer) = state.log_writer.take() {
                let _ = writer.flush();
            }
        }
    }

    fn status_snapshot(&self) -> Value {
        let wants_psl = self.wants_psl();
        let personal_status = serde_json::to_value(self.personal.status()).unwrap_or_else(|_| json!({}));
        let training_status = self.training.status();
        let state = self.state.lock().unwrap();
        let now = wire::monotonic_ns();

        let (age_opt, upper_opt) = match &state.last_header {
            Some(hdr) => {
                let (a, u) = calc_age(state.clock.as_ref(), &state.mode, hdr, now);
                (Some(a), Some(u))
            }
            None => (None, None),
        };
        let source_age_ms = age_opt.filter(|v| v.is_finite());
        let receive_age_ms = state
            .received_times
            .back()
            .map(|&last| ((now - last) as f64) / 1e6);
        let pred_fresh = state
            .prediction
            .get("fresh")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let video_fresh =
            upper_opt.is_some_and(|u| u <= state.config.stale_ms) && state.connected;
        let source_fresh = pred_fresh && video_fresh;

        let (stock_fresh, stock_age_ms) = match &state.latest_stock {
            Some((rec, recv_ns)) => {
                let age_ms = round2((((now - *recv_ns) as f64) / 1e6).max(0.0));
                let sid = state.source.as_ref().map(|h| h.session_id).unwrap_or(0);
                let fresh = stock_is_fresh(
                    Some(rec),
                    Some(*recv_ns),
                    sid,
                    &state.config,
                    state.clock.as_ref(),
                    now,
                );
                (fresh, Some(age_ms))
            }
            None => (false, None),
        };

        let mut metrics = state.counts.to_map();
        metrics.insert(
            "received_hz".to_string(),
            json!(calc_hz(&state.received_times)),
        );
        metrics.insert(
            "inference_hz".to_string(),
            json!(calc_hz(&state.inferred_times)),
        );
        metrics.insert("source_age_ms".to_string(), json!(source_age_ms));
        metrics.insert("receive_age_ms".to_string(), json!(receive_age_ms));
        metrics.insert("source_fresh".to_string(), json!(source_fresh));
        metrics.insert("video_fresh".to_string(), json!(video_fresh));
        metrics.insert(
            "queue_depth".to_string(),
            json!(if state.pending.is_some() { 1 } else { 0 }),
        );
        metrics.insert(
            "queue_ms".to_string(),
            json!(state.latencies.back().map(|l| l.queue_ms)),
        );
        metrics.insert(
            "pc_total_ms".to_string(),
            json!(state.latencies.back().map(|l| l.pc_total_ms)),
        );
        metrics.insert(
            "latency_ms".to_string(),
            json!({
                "source_copy_to_result_ms": latency_percentiles(&state.latencies, |l| l.source_copy_to_result_ms),
                "decode_ms": latency_percentiles(&state.latencies, |l| l.decode_ms),
                "predict_ms": latency_percentiles(&state.latencies, |l| l.predict_ms),
                "queue_ms": latency_percentiles(&state.latencies, |l| l.queue_ms),
                "pc_total_ms": latency_percentiles(&state.latencies, |l| l.pc_total_ms),
            }),
        );
        metrics.insert(
            "clock_uncertainty_ms".to_string(),
            json!(state
                .clock
                .as_ref()
                .map(|c| c.uncertainty_ms)
                .filter(|v| v.is_finite())),
        );

        let engine = state.description.get("engine").cloned().unwrap_or(Value::Null);
        let device = state.description.get("device").cloned().unwrap_or(Value::Null);

        json!({
            "version": APP_VERSION,
            "app_version": APP_VERSION,
            "server_pid": std::process::id(),
            "running": state.running,
            "source_mode": state.mode,
            "connected": state.connected,
            "engine": engine,
            "device": device,
            "model": state.description,
            "source": state.source.as_ref().map(|h| h.to_identity_json()),
            "prediction_source": state.prediction_source.as_ref().map(|h| h.to_identity_json()),
            "output_session_id": state.output_session_id.to_string(),
            "metrics": Value::Object(metrics),
            "prediction": state.prediction,
            "config": state.config,
            "contract": {
                "schema": SCHEMA,
                "channels": CHANNELS.as_slice(),
                "stock_channels": STOCK_CHANNELS.as_slice(),
                "routable_channels": ROUTABLE_CHANNELS.as_slice(),
                "presets": mouth_presets_json(),
            },
            "live_stock": {
                "wanted": wants_psl,
                "active": state.stock_status.active,
                "fresh": stock_fresh,
                "age_ms": stock_age_ms,
                "error": state.stock_status.error,
            },
            "recording": state.record_status,
            "stock": state.stock_status.to_json(),
            "capture_context": state.capture_context,
            "training": training_status,
            "last_error": state.last_error,
            "replay": state.replay.to_json(),
            "capture_only": state.capture_only,
            "legacy_configuration_available": state.legacy_configuration.is_some(),
            "personalization": personal_status,
            "run_directory": state.run_dir.as_ref().map(|p| p.to_string_lossy().into_owned()),
            "source_directory": self.source_directory,
            "fixed": {
                "width": 400,
                "height": 400,
                "fps": 90,
                "codec": "JPEG",
                "quality": 95,
            },
        })
    }
}

fn load_config_from_disk(
    config_path: &Path,
) -> (RuntimeConfig, Map<String, Value>, Option<Value>) {
    let mut config = RuntimeConfig::default();
    let mut preserved_extra = Map::new();
    let mut legacy_configuration: Option<Value> = None;

    if let Ok(raw) = fs::read_to_string(config_path) {
        if let Ok(Value::Object(mut saved)) = serde_json::from_str::<Value>(&raw) {
            legacy_configuration = saved.remove("legacy_configuration");
            let saved_kind = saved
                .get("model_kind")
                .and_then(|v| v.as_str())
                .unwrap_or("segmentation");
            if saved_kind != "face50" {
                legacy_configuration = Some(Value::Object(saved.clone()));
                saved.remove("model_kind");
                saved.remove("model_path");
            }

            let had_filter_enabled = saved.contains_key("filter_enabled");
            let had_filter_min_cutoff = saved.contains_key("filter_min_cutoff");
            let legacy_smoothing_raw = saved.get("smoothing_ms").cloned();

            // Drop obsolete PC-side override; VRCFT owns source selection.
            saved.remove("face_source");

            for (key, value) in &saved {
                match key.as_str() {
                    "enabled" => config.enabled = parse_bool_loose(value),
                    "headset_host" => config.headset_host = parse_host_string(value),
                    "headset_port" => {
                        if let Ok(p) = parse_port_value(value, "headset_port") {
                            config.headset_port = p;
                        }
                    }
                    "model_path" => config.model_path = parse_string_field(value),
                    "model_kind" => {
                        if let Some(s) = value.as_str() {
                            if matches!(s, "face50" | "segmentation" | "mouthlink") {
                                config.model_kind = s.to_string();
                            }
                        }
                    }
                    "device" => config.device = parse_string_field(value),
                    "smoothing_ms" => {
                        if let Ok(v) = parse_nonnegative_f64(value, "smoothing_ms") {
                            config.smoothing_ms = v;
                        }
                    }
                    "filter_enabled" => config.filter_enabled = parse_bool_loose(value),
                    "filter_min_cutoff" => {
                        if let Ok(v) = parse_positive_f64(value, "filter_min_cutoff") {
                            config.filter_min_cutoff = v;
                        }
                    }
                    "filter_beta" => {
                        if let Ok(v) = parse_nonnegative_f64(value, "filter_beta") {
                            config.filter_beta = v;
                        }
                    }
                    "filter_d_cutoff" => {
                        if let Ok(v) = parse_positive_f64(value, "filter_d_cutoff") {
                            config.filter_d_cutoff = v;
                        }
                    }
                    "uncertain_hold_ms" => {
                        if let Ok(v) = parse_nonnegative_f64(value, "uncertain_hold_ms") {
                            config.uncertain_hold_ms = v;
                        }
                    }
                    "stale_ms" => {
                        if let Ok(v) = parse_nonnegative_f64(value, "stale_ms") {
                            config.stale_ms = v;
                        }
                    }
                    "preview_fps" => {
                        if let Ok(v) = parse_preview_fps(value) {
                            config.preview_fps = v;
                        }
                    }
                    "size_hysteresis" => config.size_hysteresis = parse_bool_loose(value),
                    "replay_path" => config.replay_path = parse_string_field(value),
                    "mouth_mode" => {
                        if let Some(s) = value.as_str() {
                            if MOUTH_MODES.contains(&s) {
                                config.mouth_mode = s.to_string();
                            }
                        }
                    }
                    "hybrid_channels" => {
                        if let Some(arr) = value.as_array() {
                            if arr.iter().all(|v| v.is_string()) {
                                let allowed: HashSet<&str> =
                                    arr.iter().filter_map(|v| v.as_str()).collect();
                                config.hybrid_channels = Some(
                                    STOCK_CHANNELS
                                        .iter()
                                        .filter(|&&n| allowed.contains(n))
                                        .map(|&s| s.to_string())
                                        .collect(),
                                );
                            }
                        }
                    }
                    "channel_sources" => {
                        if let Ok(Some(validated)) = validate_channel_sources(Some(value)) {
                            config.channel_sources = Some(validated);
                        }
                    }
                    "output_enabled" => config.output_enabled = parse_bool_loose(value),
                    "output_host" => config.output_host = parse_host_string(value),
                    "output_port" => {
                        if let Ok(p) = parse_port_value(value, "output_port") {
                            config.output_port = p;
                        }
                    }
                    _ => {
                        preserved_extra.insert(key.clone(), value.clone());
                    }
                }
            }

            if !had_filter_enabled && !had_filter_min_cutoff {
                if let Some(ref sm_val) = legacy_smoothing_raw {
                    if let Ok(legacy_ms) = parse_nonnegative_f64(sm_val, "smoothing_ms") {
                        config.filter_enabled = legacy_ms > 0.0;
                        if legacy_ms > 0.0 {
                            config.filter_min_cutoff = round2(1000.0 / (2.0 * PI * legacy_ms));
                        }
                    }
                }
            }
        }
    }

    if !MOUTH_MODES.contains(&config.mouth_mode.as_str()) {
        config.mouth_mode = "curated".to_string();
    }
    if let Some(ref validated) = config.channel_sources {
        let full = resolve_channel_sources(
            &config.mouth_mode,
            config.hybrid_channels.as_deref(),
            Some(validated),
        );
        let detected = matching_preset(&full);
        config.mouth_mode = detected.to_string();
        config.channel_sources = if PRESET_MODES.contains(&detected) {
            None
        } else {
            Some(full)
        };
        config.hybrid_channels = None;
    } else {
        config.channel_sources = None;
        if config.mouth_mode == "custom" {
            config.mouth_mode = "curated".to_string();
        }
    }
    config.enabled = false;
    (config, preserved_extra, legacy_configuration)
}

impl Runtime {
    pub fn new(config_path: PathBuf) -> Self {
        Self::with_options(config_path, None, None, false)
    }

    pub fn with_options(
        config_path: PathBuf,
        model_factory: Option<ModelFactory>,
        run_root: Option<PathBuf>,
        record_input: bool,
    ) -> Self {
        let (config, preserved_extra, legacy_configuration) = load_config_from_disk(&config_path);
        let parent_dir = config_path
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."));
        let run_root = run_root.unwrap_or_else(|| parent_dir.join("runs"));
        let script_root = default_script_root();
        let source_directory = script_root.to_string_lossy().into_owned();

        let mut face_filter = FaceFilter::new();
        let init_pred = invalid_prediction(&config.model_kind, &mut face_filter, "stopped");

        // Personalization on_changed callback wired via Weak<RuntimeInner>.
        let inner_weak_holder: Arc<Mutex<Option<std::sync::Weak<RuntimeInner>>>> =
            Arc::new(Mutex::new(None));
        let holder_for_cb = Arc::clone(&inner_weak_holder);
        let on_changed: Arc<dyn Fn(bool) + Send + Sync> = Arc::new(move |head_changed: bool| {
            let weak_opt = holder_for_cb.lock().unwrap().clone();
            if let Some(inner) = weak_opt.and_then(|w| w.upgrade()) {
                let udp_pkt = {
                    let mut state = inner.state.lock().unwrap();
                    state.model_version += 1;
                    if state.config.model_kind == "segmentation" {
                        state.profile_reload = state.profile_reload || head_changed;
                    }
                    let inv = serde_json::to_value(TongueState::invalid("profile_changed"))
                        .unwrap_or_else(|_| json!({}));
                    let udp_pkt = state.last_header.as_ref().and_then(|last_hdr| {
                        RuntimeInner::build_result_packet(&state, last_hdr, &inv, 0.0)
                    });
                    state.output_session_id = unix_time_ns();
                    state.tongue.clear();
                    state.prediction = inv;
                    state.preview = None;
                    inner.frame_cond.notify_all();
                    udp_pkt
                };
                inner.send_udp_packet(udp_pkt);
            }
        });

        let personal = Personalization::new(
            parent_dir.join("personalization"),
            Some(on_changed),
            None,
        );
        personal.set_checkpoint(&config.model_path);
        let init_udp_target = if config.output_enabled {
            resolve_udp_target(&config.output_host, config.output_port)
        } else {
            None
        };

        let state = RuntimeState {
            config,
            preserved_extra,
            legacy_configuration,
            running: false,
            connected: false,
            capture_only: false,
            mode: "idle".to_string(),
            last_error: None,
            pending: None,
            source: None,
            prediction_source: None,
            face_filter,
            prediction: init_pred,
            model_key: None,
            description: json!({}),
            order: SourceOrder::new(),
            tongue: TongueState::new(),
            preview: None,
            clock: None,
            clock_domain: None,
            counts: MetricsCounts::default(),
            latencies: VecDeque::with_capacity(2700),
            received_times: VecDeque::with_capacity(900),
            inferred_times: VecDeque::with_capacity(900),
            last_header: None,
            last_result_ns: 0,
            last_preview_ns: 0,
            replay: ReplayState::default(),
            lifecycle: 0,
            psl_gen: 0,
            stop_flag: Arc::new(AtomicBool::new(true)),
            udp_target: init_udp_target,
            output_session_id: unix_time_ns(),
            model_version: 0,
            model_reset_pending: false,
            profile_reload: false,
            capture_context: None,
            transport_bindings: HashMap::new(),
            active_socket: None,
            stock_socket: None,
            latest_stock: None,
            record_wants_stock: false,
            source_closed: false,
            stock_status: StockStatus::default(),
            record_status: json!({ "state": "idle" }),
            run_dir: None,
            log_writer: None,
            clock_log: None,
            input_stream: None,
        };

        let inner = Arc::new(RuntimeInner {
            config_path,
            run_root,
            record_input,
            script_root: Mutex::new(script_root),
            source_directory,
            model_factory: model_factory.unwrap_or_else(default_model_factory),
            config_write_lock: Mutex::new(()),
            state: Mutex::new(state),
            model: Mutex::new(None),
            frame_cond: Condvar::new(),
            inference_ready_state: Mutex::new(false),
            inference_ready_cond: Condvar::new(),
            clock_state: Mutex::new(false),
            clock_cond: Condvar::new(),
            threads: Mutex::new(Vec::new()),
            udp: Mutex::new(UdpSockets {
                v4: UdpSocket::bind("0.0.0.0:0").ok(),
                v6: None,
            }),
            personal,
            recorder: Mutex::new(None),
            record_control: Mutex::new(()),
            record_queue: Mutex::new(RecordQueueState::default()),
            record_cond: Condvar::new(),
            record_thread: Mutex::new(None),
            psl_lock: Mutex::new(PslWorkerState {
                stop_flag: Arc::new(AtomicBool::new(true)),
                finished: Arc::new(AtomicBool::new(true)),
                thread: None,
            }),
            training: TrainingRunner::new(),
        });

        *inner_weak_holder.lock().unwrap() = Some(Arc::downgrade(&inner));
        Self { inner }
    }

    pub fn config_path(&self) -> &Path {
        &self.inner.config_path
    }

    pub fn set_script_root(&self, root: PathBuf) {
        *self.inner.script_root.lock().unwrap() = root;
    }

    pub fn config(&self) -> RuntimeConfig {
        self.inner.state.lock().unwrap().config.clone()
    }

    pub fn current_headset_host(&self) -> String {
        self.inner.state.lock().unwrap().config.headset_host.clone()
    }

    pub fn is_running(&self) -> bool {
        self.inner.state.lock().unwrap().running
    }

    pub fn is_connected(&self) -> bool {
        self.inner.state.lock().unwrap().connected
    }

    pub fn source_mode(&self) -> String {
        self.inner.state.lock().unwrap().mode.clone()
    }

    pub fn last_error(&self) -> Option<String> {
        self.inner.state.lock().unwrap().last_error.clone()
    }

    pub fn record_status(&self) -> Value {
        self.inner.state.lock().unwrap().record_status.clone()
    }

    pub fn configure(&self, changes: Value) -> Result<RuntimeConfig, String> {
        let Some(map) = changes.as_object() else {
            return Err("Configuration payload must be a JSON object".to_string());
        };

        let _cfg_guard = self.inner.config_write_lock.lock().unwrap();

        let needs_stop = {
            let state = self.inner.state.lock().unwrap();
            let preview = build_updated_config(&state.config, map)?;
            let restart_changed = state.running && preview.restart_key_differs(&state.config);
            (!preview.enabled) || restart_changed
        };
        let _ = RESTART_KEYS;

        if needs_stop {
            self.stop(false)?;
        }

        let (updated_cfg, saved_map) = {
            let state = self.inner.state.lock().unwrap();
            let mut updated = build_updated_config(&state.config, map)?;
            if needs_stop {
                updated.enabled = false;
            } else {
                updated.enabled = updated.enabled && state.running;
            }
            let mut saved_map = state.preserved_extra.clone();
            if let Value::Object(cfg_obj) =
                serde_json::to_value(&updated).map_err(|e| e.to_string())?
            {
                for (k, v) in cfg_obj {
                    saved_map.insert(k, v);
                }
            }
            if let Some(ref legacy) = state.legacy_configuration {
                saved_map.insert("legacy_configuration".to_string(), legacy.clone());
            }
            (updated, saved_map)
        };

        let serialized =
            serde_json::to_string_pretty(&Value::Object(saved_map)).map_err(|e| e.to_string())?;
        write_text_atomic(&self.inner.config_path, &serialized).map_err(|e| e.to_string())?;

        let resolved_udp_target = if updated_cfg.output_enabled {
            resolve_udp_target(&updated_cfg.output_host, updated_cfg.output_port)
        } else {
            None
        };

        let (result_cfg, sync_psl, new_model_path) = {
            let mut state = self.inner.state.lock().unwrap();
            let mut committed = updated_cfg;
            if needs_stop {
                committed.enabled = false;
            } else {
                committed.enabled = committed.enabled && state.running;
            }
            let sync_psl = committed.enabled && state.running;
            let mp = committed.model_path.clone();
            state.config = committed.clone();
            state.udp_target = resolved_udp_target;
            (committed, sync_psl, mp)
        };

        self.inner.personal.set_checkpoint(&new_model_path);

        if sync_psl {
            self.inner.sync_psl_worker();
        }
        Ok(result_cfg)
    }

    pub fn prepare(&self) -> Result<Value, String> {
        let (running, existing_desc, config) = {
            let state = self.inner.state.lock().unwrap();
            (state.running, state.description.clone(), state.config.clone())
        };
        if running {
            return Ok(existing_desc);
        }
        self.inner.personal.set_checkpoint(&config.model_path);
        let personal_head = self.inner.personal.profile().head;
        let checkpoint = if config.model_kind == "segmentation" {
            personal_head.clone().unwrap_or_else(|| config.model_path.clone())
        } else {
            config.model_path.clone()
        };
        let key = (
            checkpoint.clone(),
            config.device.clone(),
            config.model_kind.clone(),
        );
        let need_load = {
            let state = self.inner.state.lock().unwrap();
            let has_model = self.inner.model.lock().unwrap().is_some();
            !has_model || state.model_key.as_ref() != Some(&key)
        };
        if need_load {
            if checkpoint.trim().is_empty() {
                return Err("Configure a model checkpoint path before starting".to_string());
            }
            let mut load_cfg = config.clone();
            load_cfg.model_path = checkpoint;
            let loaded = (self.inner.model_factory)(&load_cfg)?;
            let mut desc = loaded.describe();
            if let Some(obj) = desc.as_object_mut() {
                obj.insert(
                    "standard_checkpoint".to_string(),
                    json!(config.model_path),
                );
                obj.insert(
                    "personal_head".to_string(),
                    json!(config.model_kind == "segmentation" && personal_head.is_some()),
                );
            }
            {
                *self.inner.model.lock().unwrap() = Some(loaded);
            }
            let mut state = self.inner.state.lock().unwrap();
            state.model_key = Some(key);
            state.description = desc;
            state.profile_reload = false;
            return Ok(state.description.clone());
        }
        let mut state = self.inner.state.lock().unwrap();
        state.profile_reload = false;
        Ok(state.description.clone())
    }

    pub fn personalize(&self, action: &str, confirmed: bool) -> Result<Value, String> {
        let (model_kind, model_path, running, connected, mode, stale_ms, fresh_source) = {
            let state = self.inner.state.lock().unwrap();
            let now = wire::monotonic_ns();
            let fresh = state.source.as_ref().is_some_and(|hdr| {
                calc_age(state.clock.as_ref(), &state.mode, hdr, now).1 <= state.config.stale_ms
            });
            (
                state.config.model_kind.clone(),
                state.config.model_path.clone(),
                state.running,
                state.connected,
                state.mode.clone(),
                state.config.stale_ms,
                fresh,
            )
        };
        let _ = stale_ms;
        if model_kind == "face50" {
            return Err(
                "Whole-mouth models use local whole-mouth recording and training, not mask calibration"
                    .to_string(),
            );
        }
        self.inner.personal.set_checkpoint(&model_path);
        match action {
            "calibrate" | "retrain" => {
                if model_kind != "segmentation" {
                    return Err(
                        "MouthLink models use the MouthLink recording and training tools"
                            .to_string(),
                    );
                }
                if !running || !connected || mode != "live" || !fresh_source {
                    return Err(
                        "Start live input and wait for fresh camera images before calibration"
                            .to_string(),
                    );
                }
                self.inner.personal.begin(action, confirmed)?;
            }
            "cancel" => self.inner.personal.cancel(),
            "restore" => {
                if model_kind != "segmentation" {
                    return Err(
                        "MouthLink models use the MouthLink recording and training tools"
                            .to_string(),
                    );
                }
                self.inner.personal.restore()?;
            }
            _ => return Err("Unknown personalization action".to_string()),
        }
        serde_json::to_value(self.inner.personal.status()).map_err(|e| e.to_string())
    }

    pub fn start(&self, req: StartRequest) -> Result<Value, String> {
        let mode = req.mode.as_deref().unwrap_or("live");
        let replay_path = req.path.as_deref().or(req.replay_path.as_deref());
        self.start_with_mode(mode, replay_path, req.capture_only.unwrap_or(false))
    }

    pub fn start_with_mode(
        &self,
        mode: &str,
        replay_path: Option<&str>,
        mut capture_only: bool,
    ) -> Result<Value, String> {
        let actual_mode = if mode == "capture" {
            capture_only = true;
            "live"
        } else {
            mode
        };
        {
            let state = self.inner.state.lock().unwrap();
            if state.running {
                return Err("Runtime is already running".to_string());
            }
            if actual_mode != "live" && actual_mode != "replay" {
                return Err("mode must be live or replay".to_string());
            }
            if actual_mode == "live" && state.config.headset_host.trim().is_empty() {
                return Err("Enter the headset address before starting live input".to_string());
            }
        }

        self.stop(false)?;
        let generation = self.inner.state.lock().unwrap().lifecycle;

        if capture_only {
            *self.inner.model.lock().unwrap() = None;
            let mut state = self.inner.state.lock().unwrap();
            state.model_key = None;
            state.description = json!({
                "model_kind": "face50",
                "schema": SCHEMA,
                "trained_channels": [],
                "capture_only": true,
            });
        } else {
            self.prepare()?;
        }

        let replay_file: Option<PathBuf> = if actual_mode == "replay" {
            let raw_path = replay_path
                .filter(|s| !s.trim().is_empty())
                .map(str::to_string)
                .unwrap_or_else(|| self.inner.state.lock().unwrap().config.replay_path.clone());
            let pb = PathBuf::from(&raw_path);
            if raw_path.trim().is_empty() || !pb.is_file() {
                return Err(format!("Replay dataset does not exist: {raw_path}"));
            }
            Some(pb)
        } else {
            None
        };

        let seq = RUN_DIR_SEQ.fetch_add(1, Ordering::Relaxed);
        let run_dir = self
            .inner
            .run_root
            .join(format!("{}-{seq}", unix_time_ns()));
        fs::create_dir_all(&run_dir).map_err(|e| format!("Failed to create run directory: {e}"))?;
        let log_file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(run_dir.join("frames.jsonl"))
            .map_err(|e| format!("Failed to create frames.jsonl: {e}"))?;
        let clock_file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(run_dir.join("clock.jsonl"))
            .map_err(|e| format!("Failed to create clock.jsonl: {e}"))?;
        let input_stream = if self.inner.record_input && actual_mode == "live" {
            let f = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(run_dir.join("input.ptj"))
                .map_err(|e| format!("Failed to create input.ptj: {e}"))?;
            Some(BufWriter::with_capacity(262_144, f))
        } else {
            None
        };

        let resolved_udp_target = {
            let (enabled, host, port) = {
                let state = self.inner.state.lock().unwrap();
                (
                    state.config.output_enabled,
                    state.config.output_host.clone(),
                    state.config.output_port,
                )
            };
            if enabled {
                resolve_udp_target(&host, port)
            } else {
                None
            }
        };

        {
            let mut threads = self.inner.threads.lock().unwrap();
            {
                let mut clock_req = self.inner.clock_state.lock().unwrap();
                *clock_req = false;
            }
            self.inner.reset_inference_ready();

            let (session_lifecycle, session_stop, cfg_snapshot) = {
                let mut state = self.inner.state.lock().unwrap();
                if generation != state.lifecycle {
                    return Err("Startup was cancelled by stop/shutdown".to_string());
                }
                if state.running {
                    return Err("Runtime is already running".to_string());
                }
                state.lifecycle += 1;
                state.psl_gen = state.psl_gen.wrapping_add(1);
                state.udp_target = resolved_udp_target;
                let session_lifecycle = state.lifecycle;
                let session_stop = Arc::new(AtomicBool::new(false));
                state.stop_flag = Arc::clone(&session_stop);
                state.running = true;
                state.capture_only = capture_only;
                state.connected = false;
                state.mode = actual_mode.to_string();
                state.config.enabled = true;
                state.last_error = None;
                state.pending = None;
                state.source = None;
                state.prediction_source = None;
                let kind = state.config.model_kind.clone();
                state.prediction = invalid_prediction(&kind, &mut state.face_filter, "waiting");
                state.order = SourceOrder::new();
                state.tongue.clear();
                state.preview = None;
                state.clock = None;
                state.clock_domain = None;
                state.latencies.clear();
                state.received_times.clear();
                state.inferred_times.clear();
                state.last_header = None;
                state.latest_stock = None;
                state.record_wants_stock = false;
                state.source_closed = false;
                state.stock_status = StockStatus::default();
                state.transport_bindings.clear();
                state.capture_context = None;
                state.output_session_id = unix_time_ns();
                state.last_result_ns = 0;
                state.last_preview_ns = 0;
                state.counts = MetricsCounts::default();
                state.run_dir = Some(run_dir.clone());
                state.log_writer = Some(BufWriter::with_capacity(65_536, log_file));
                state.clock_log = Some(clock_file);
                state.input_stream = input_stream;
                state.replay = ReplayState {
                    active: actual_mode == "replay",
                    path: replay_file
                        .as_ref()
                        .map(|p| p.to_string_lossy().into_owned()),
                    position: 0,
                    total: None,
                    finished: false,
                };
                state.active_socket = None;
                (session_lifecycle, session_stop, state.config.clone())
            };

            let cfg_json =
                serde_json::to_string_pretty(&cfg_snapshot).map_err(|e| e.to_string())?;
            let _ = fs::write(run_dir.join("config.json"), cfg_json);

            let mut spawned = Vec::with_capacity(3);
            let spawn_res: Result<(), String> = (|| {
                let inner_inf = Arc::clone(&self.inner);
                let stop_inf = Arc::clone(&session_stop);
                spawned.push(
                    wire::spawn_bounded("tongue-inference", move || {
                        inner_inf.inference_loop(session_lifecycle, stop_inf)
                    })
                    .map_err(|e| e.to_string())?,
                );

                if let Some(rpath) = replay_file {
                    let inner_rep = Arc::clone(&self.inner);
                    let stop_rep = Arc::clone(&session_stop);
                    spawned.push(
                        wire::spawn_bounded("camera-source", move || {
                            inner_rep.replay_loop(rpath, session_lifecycle, stop_rep)
                        })
                        .map_err(|e| e.to_string())?,
                    );
                } else {
                    let inner_recv = Arc::clone(&self.inner);
                    let stop_recv = Arc::clone(&session_stop);
                    spawned.push(
                        wire::spawn_bounded("camera-source", move || {
                            inner_recv.receive_loop(session_lifecycle, stop_recv)
                        })
                        .map_err(|e| e.to_string())?,
                    );

                    let inner_clock = Arc::clone(&self.inner);
                    let stop_clock = Arc::clone(&session_stop);
                    spawned.push(
                        wire::spawn_bounded("tongue-clock", move || {
                            inner_clock.clock_refresh_loop(session_lifecycle, stop_clock)
                        })
                        .map_err(|e| e.to_string())?,
                    );
                }
                Ok(())
            })();

            if let Err(err) = spawn_res {
                session_stop.store(true, Ordering::SeqCst);
                self.inner.signal_inference_ready();
                self.inner.wake_clock_thread();
                self.inner.frame_cond.notify_all();
                wire::join_all_bounded(spawned, Duration::from_secs(5));
                let mut state = self.inner.state.lock().unwrap();
                if state.lifecycle == session_lifecycle {
                    state.running = false;
                    state.connected = false;
                    state.mode = "idle".to_string();
                    state.config.enabled = false;
                }
                return Err(err);
            }

            threads.extend(spawned);
        }

        self.inner.sync_psl_worker();
        Ok(self.status())
    }

    pub fn bind_capture(
        &self,
        capture_session_id: u64,
        service_generation: Value,
        evidence: Value,
    ) -> Result<(), String> {
        let _ = self
            .inner
            .bind_capture_inner(capture_session_id, service_generation, evidence, None)?;
        Ok(())
    }

    pub fn recording(
        &self,
        action: &str,
        options: RecordingActionOptions,
    ) -> Result<Value, String> {
        match action {
            "begin" => {
                if !options.confirmed {
                    return Err(
                        "Confirm local image/stock recording and local training first".to_string(),
                    );
                }
                if options.person_id.trim().is_empty() {
                    return Err("person_id must be a nonempty anonymous string".to_string());
                }
                if options.stage == "instant" && !options.receive_results {
                    return Err("Instant labels require receiving mouth results".to_string());
                }
                let _rec_ctrl = self.inner.record_control.lock().unwrap();
                {
                    let is_active = self.inner.record_queue.lock().unwrap().worker_active;
                    if !is_active && self.inner.record_thread.lock().unwrap().is_some() {
                        self.inner.finish_recording_locked(false);
                    }
                    if self.inner.record_thread.lock().unwrap().is_some() {
                        return Err(
                            "Stop the active recording before beginning another".to_string()
                        );
                    }
                }
                let bindings = {
                    let state = self.inner.state.lock().unwrap();
                    if !state.running
                        || !state.connected
                        || state.source_closed
                        || state.mode != "live"
                        || state.config.model_kind != "face50"
                    {
                        return Err(
                            "Start capture-only or live whole-mouth input explicitly before recording"
                                .to_string(),
                        );
                    }
                    state.transport_bindings.clone()
                };

                let rec_root = self
                    .inner
                    .config_path
                    .parent()
                    .unwrap_or_else(|| Path::new("."))
                    .join("recordings");
                let recorder = Arc::new(WholeMouthRecording::new(rec_root));
                recorder.set_label_stage(&options.stage)?;
                let rec_status = recorder.begin(BeginOptions {
                    person_id: options.person_id,
                    confirmed: true,
                    guide: options.guide,
                    role: options.role,
                    evaluation_only: options.evaluation_only,
                    preset_id: options.preset_id,
                    automatic: options.automatic,
                    continuation: options.continuation,
                    step_index: options.step_index.unwrap_or(0),
                    prepare_seconds: options.prepare_seconds,
                })?;
                for (cid, binding) in bindings {
                    recorder.bind_capture(cid, binding.service_generation, binding.evidence)?;
                }

                *self.inner.recorder.lock().unwrap() = Some(Arc::clone(&recorder));
                {
                    let mut q = self.inner.record_queue.lock().unwrap();
                    q.queue.clear();
                    q.stopping = false;
                    q.cancelled = false;
                    q.worker_active = true;
                    q.error = None;
                }
                {
                    let psl_active = self.inner.state.lock().unwrap().stock_status.active;
                    let mut state = self.inner.state.lock().unwrap();
                    state.record_status = rec_status.clone();
                    state.record_wants_stock = options.receive_results;
                    state.stock_status = StockStatus {
                        active: psl_active,
                        records: 0,
                        associated: 0,
                        unassociated: 0,
                        error: None,
                    };
                }

                let inner_rec = Arc::clone(&self.inner);
                let rec_for_worker = Arc::clone(&recorder);
                let handle = thread::Builder::new()
                    .name("mouth-recording".to_string())
                    .spawn(move || inner_rec.record_worker_loop(rec_for_worker))
                    .map_err(|e| format!("Failed to start recording worker: {e}"))?;
                *self.inner.record_thread.lock().unwrap() = Some(handle);
                self.inner.sync_psl_worker();
                Ok(rec_status)
            }
            "ready" | "finish" | "confirm" | "skip" | "pause" | "resume" | "select_step" => {
                let recorder = self
                    .inner
                    .recorder
                    .lock()
                    .unwrap()
                    .clone()
                    .ok_or_else(|| "Begin a recording first".to_string())?;
                recorder.advance(action, options.step_index, options.guide_frame.as_deref())?;
                let st = recorder.status();
                self.inner.state.lock().unwrap().record_status = st.clone();
                Ok(st)
            }
            "stop" | "cancel" => {
                self.stop(action == "cancel")?;
                Ok(self.record_status())
            }
            _ => Err("Unknown whole-mouth recording action".to_string()),
        }
    }

    pub fn recorder_display(
        &self,
        session_id: &str,
        frame: &str,
        phase: &str,
        visible: bool,
        media_ready: bool,
    ) -> Value {
        let Some(recorder) = self.inner.recorder.lock().unwrap().clone() else {
            return json!({ "accepted": false });
        };
        recorder.display(session_id, frame, phase, visible, media_ready)
    }

    pub fn review_samples(
        &self,
        offset: usize,
        limit: usize,
        all_frames: bool,
    ) -> Result<Value, String> {
        let Some(recorder) = self.inner.recorder.lock().unwrap().clone() else {
            return Ok(json!({
                "total": 0,
                "offset": 0,
                "samples": [],
                "session_id": Value::Null,
            }));
        };
        recorder.review_samples(offset, limit, all_frames)
    }

    pub fn review_image(&self, session_id: &str, sample_id: &str) -> Result<Vec<u8>, String> {
        let Some(recorder) = self.inner.recorder.lock().unwrap().clone() else {
            return Err("Image does not belong to the current local recording".to_string());
        };
        recorder.review_image(session_id, sample_id)
    }

    pub fn review(
        &self,
        session_id: &str,
        sample_ids: &[String],
        targets: &Map<String, Value>,
        confirmed: bool,
        reviewer: &str,
        observation: &str,
    ) -> Result<Value, String> {
        let Some(recorder) = self.inner.recorder.lock().unwrap().clone() else {
            return Err("Review does not belong to the current local recording".to_string());
        };
        if recorder.session_id().as_deref() != Some(session_id) {
            return Err("Review does not belong to the current local recording".to_string());
        }
        let res = recorder.review(sample_ids, targets, confirmed, reviewer, observation)?;
        self.inner.state.lock().unwrap().record_status = recorder.status();
        Ok(res)
    }

    pub fn cancel_training(&self) -> Result<Value, String> {
        self.inner.training.cancel()
    }

    pub fn train(
        &self,
        action: &str,
        operation: Option<&str>,
        args: Option<&Value>,
    ) -> Result<Value, String> {
        match action {
            "cancel" => {
                let status = self.inner.training.cancel()?;
                self.stop(false)?;
                Ok(status)
            }
            "begin" => {
                if self.inner.training.is_running() {
                    return Err("A local training job is already running".to_string());
                }
                let op = operation.unwrap_or("");
                let default_args = json!({});
                let args_ref = args.unwrap_or(&default_args);
                // Validate before stopping runtime so bad requests never disrupt live tracking.
                let _ = training::validate_operation_args(op, args_ref)?;
                let script_root = self.inner.script_root.lock().unwrap().clone();
                let script_path = script_root.join("face_train.py");
                if !script_path.is_file() {
                    return Err(
                        "Local training script face_train.py was not found; local training is only available in a source checkout"
                            .to_string(),
                    );
                }
                self.stop(false)?;
                self.inner.training.begin(op, args_ref, &script_root)
            }
            _ => Err("Unknown local training action".to_string()),
        }
    }

    pub fn stop(&self, cancel_recording: bool) -> Result<(), String> {
        let (stop_lifecycle, sock, stop_pkt) = {
            let mut state = self.inner.state.lock().unwrap();
            state.stop_flag.store(true, Ordering::SeqCst);
            state.model_version += 1;
            state.pending = None;
            let kind = state.config.model_kind.clone();
            let inv = invalid_prediction(&kind, &mut state.face_filter, "stopped");
            state.prediction = inv.clone();
            state.preview = None;
            let stop_pkt = state
                .last_header
                .take()
                .and_then(|last_hdr| RuntimeInner::build_result_packet(&state, &last_hdr, &inv, 0.0));
            state.lifecycle += 1;
            let stop_lifecycle = state.lifecycle;
            let sock = state.active_socket.take();
            self.inner.frame_cond.notify_all();
            (stop_lifecycle, sock, stop_pkt)
        };
        self.inner.send_udp_packet(stop_pkt);
        self.inner.signal_inference_ready();
        self.inner.wake_clock_thread();

        self.inner.finish_recording(cancel_recording);
        self.inner.personal.cancel();
        self.inner.personal.join_worker();

        if let Some(s) = sock {
            let _ = s.shutdown(Shutdown::Both);
        }

        let handles = {
            let mut guard = self.inner.threads.lock().unwrap();
            std::mem::take(&mut *guard)
        };
        wire::join_all_bounded(handles, Duration::from_secs(5));

        {
            let mut psl = self.inner.psl_lock.lock().unwrap();
            self.inner.stop_psl_worker_locked(&mut psl);
        }

        {
            if let Some(ref mut m) = *self.inner.model.lock().unwrap() {
                m.reset_state();
            }
        }

        let summary_dir = {
            let mut state = self.inner.state.lock().unwrap();
            if state.lifecycle == stop_lifecycle {
                if let Some(mut w) = state.log_writer.take() {
                    let _ = w.flush();
                }
                if let Some(mut s) = state.input_stream.take() {
                    let _ = s.flush();
                }
                state.clock_log = None;
                state.active_socket = None;
                state.stock_socket = None;
                state.running = false;
                state.connected = false;
                state.mode = "idle".to_string();
                state.config.enabled = false;
                state.pending = None;
                state.last_header = None;
                let kind = state.config.model_kind.clone();
                state.prediction = invalid_prediction(&kind, &mut state.face_filter, "stopped");
                state.tongue.clear();
                state.preview = None;
                state.run_dir.clone()
            } else {
                None
            }
        };

        if let Some(dir) = summary_dir {
            let summary = self.status();
            if let Ok(text) = serde_json::to_string_pretty(&summary) {
                let _ = fs::write(dir.join("summary.json"), text);
            }
            let mut state = self.inner.state.lock().unwrap();
            if state.lifecycle == stop_lifecycle {
                state.run_dir = None;
            }
        }
        Ok(())
    }

    pub fn close(&self) {
        let _ = self.inner.training.cancel();
        let _ = self.stop(false);
    }

    pub fn preview_jpeg(&self) -> Option<Vec<u8>> {
        self.inner.state.lock().unwrap().preview.clone()
    }

    pub fn expression_snapshot(&self) -> Value {
        let wants_psl = self.inner.wants_psl();
        let state = self.inner.state.lock().unwrap();
        let now = wire::monotonic_ns();
        let (age_opt, upper_opt) = match &state.last_header {
            Some(hdr) => {
                let (a, u) = calc_age(state.clock.as_ref(), &state.mode, hdr, now);
                (Some(a), Some(u))
            }
            None => (None, None),
        };
        let source_age_ms = age_opt.filter(|v| v.is_finite());
        let pred_fresh = state
            .prediction
            .get("fresh")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let video_fresh =
            upper_opt.is_some_and(|u| u <= state.config.stale_ms) && state.connected;
        let source_fresh = pred_fresh && video_fresh;

        let (stock_fresh, stock_age_ms) = match &state.latest_stock {
            Some((rec, recv_ns)) => {
                let age_ms = round2((((now - *recv_ns) as f64) / 1e6).max(0.0));
                let sid = state.source.as_ref().map(|h| h.session_id).unwrap_or(0);
                let fresh = stock_is_fresh(
                    Some(rec),
                    Some(*recv_ns),
                    sid,
                    &state.config,
                    state.clock.as_ref(),
                    now,
                );
                (fresh, Some(age_ms))
            }
            None => (false, None),
        };

        json!({
            "running": state.running,
            "connected": state.connected,
            "source_mode": state.mode,
            "capture_only": state.capture_only,
            "metrics": {
                "source_age_ms": source_age_ms,
                "source_fresh": source_fresh,
                "video_fresh": video_fresh,
            },
            "prediction": state.prediction,
            "live_stock": {
                "wanted": wants_psl,
                "active": state.stock_status.active,
                "fresh": stock_fresh,
                "age_ms": stock_age_ms,
                "error": state.stock_status.error,
            },
        })
    }

    pub fn status(&self) -> Value {
        self.inner.status_snapshot()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::face_model::{FacePrediction, TimingMs};
    use crate::wire::{unpack_face_result_packet, FACE_RESULT_BYTES};
    use std::net::TcpListener;
    use zip::write::SimpleFileOptions;
    use zip::ZipWriter;

    fn make_header(seq: u64, session_id: u64, flags: u32, payload_bytes: u32) -> FrameHeader {
        let now = wire::monotonic_ns().max(0) as u64;
        FrameHeader {
            payload_bytes,
            flags,
            session_id,
            source_seq: seq,
            sensor_ns: seq * 11_111_111,
            vendor_ns: seq * 11_111_111 + 4,
            copy_mono_ns: now,
            encode_begin_ns: now,
            encode_end_ns: now,
            emit_mono_ns: now,
            eye0_sensor_ns: seq * 11_111_111 - 2,
            eye1_sensor_ns: seq * 11_111_111 - 1,
            mouth_sensor_ns: seq * 11_111_111,
            raw_identity: seq,
            width: 400,
            height: 400,
            raw_crc32: 42,
            quality: 95,
        }
    }

    fn make_test_jpeg(fill: u8) -> Vec<u8> {
        encode_gray_jpeg(&vec![fill; 400 * 400], 95).unwrap()
    }

    fn wait_for<F: Fn() -> bool>(pred: F, timeout: Duration) {
        let deadline = Instant::now() + timeout;
        while !pred() {
            assert!(Instant::now() < deadline, "Timed out waiting for condition");
            thread::sleep(Duration::from_millis(15));
        }
    }

    struct FakeFaceModel {
        valid_mask: u64,
        values: [f32; 50],
    }

    impl FakeFaceModel {
        fn new(values: [f32; 50], valid_mask: u64) -> Self {
            Self { valid_mask, values }
        }
    }

    impl MouthModel for FakeFaceModel {
        fn model_kind(&self) -> &'static str {
            "face50"
        }

        fn describe(&self) -> Value {
            let trained: Vec<&str> = CHANNELS
                .iter()
                .enumerate()
                .filter(|&(i, _)| (self.valid_mask & (1u64 << i)) != 0)
                .map(|(_, &name)| name)
                .collect();
            json!({
                "engine": "mock",
                "device": "cpu",
                "model_kind": "face50",
                "schema": SCHEMA,
                "channels": CHANNELS.as_slice(),
                "trained_channels": trained,
                "validated_channels": trained,
                "output_channels": trained,
                "valid_mask": self.valid_mask.to_string(),
            })
        }

        fn predict(&mut self, gray400: &[u8], _ctx: PredictContext) -> Result<ModelOutput, String> {
            assert_eq!(gray400.len(), 400 * 400);
            let mut ch = BTreeMap::new();
            for (i, &name) in CHANNELS.iter().enumerate() {
                if (self.valid_mask & (1u64 << i)) != 0 {
                    ch.insert(name.to_string(), self.values[i]);
                }
            }
            Ok(ModelOutput::Face50(FacePrediction {
                values: self.values,
                channels: ch,
                valid_mask: self.valid_mask,
                schema: SCHEMA,
                timing_ms: TimingMs::default(),
            }))
        }

        fn reset_state(&mut self) {}
        fn is_temporal(&self) -> bool {
            false
        }
    }

    struct FakeHeadsetServer {
        port: u16,
        stop: Arc<AtomicBool>,
        handle: Option<JoinHandle<()>>,
    }

    impl FakeHeadsetServer {
        fn start(session_id: u64, generation: u64, stock_final33: Option<[f32; 33]>) -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let port = listener.local_addr().unwrap().port();
            listener.set_nonblocking(true).unwrap();

            let payload = make_test_jpeg(90);
            let stop = Arc::new(AtomicBool::new(false));
            let stop_srv = Arc::clone(&stop);

            let handle = thread::spawn(move || {
                while !stop_srv.load(Ordering::Relaxed) {
                    match listener.accept() {
                        Ok((mut stream, _)) => {
                            let _ = stream.set_nonblocking(false);
                            let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
                            let payload = payload.clone();
                            let stop_conn = Arc::clone(&stop_srv);
                            thread::spawn(move || {
                                let mut cmd = Vec::new();
                                let mut byte = [0u8; 1];
                                while !cmd.ends_with(b"\n") {
                                    match stream.read(&mut byte) {
                                        Ok(1) => cmd.push(byte[0]),
                                        _ => return,
                                    }
                                }
                                if cmd == b"PING\n" {
                                    let a = wire::monotonic_ns().max(0) as u64;
                                    let b = wire::monotonic_ns().max(0) as u64;
                                    let mut resp = [0u8; 16];
                                    resp[0..8].copy_from_slice(&a.to_le_bytes());
                                    resp[8..16].copy_from_slice(&b.to_le_bytes());
                                    let _ = stream.write_all(&resp);
                                } else if cmd == b"CONTEXT\n" {
                                    let ctx = CaptureContext {
                                        capture_session_id: session_id,
                                        service_generation: generation,
                                    };
                                    if let Ok(packed) = ctx.pack() {
                                        let _ = stream.write_all(&packed);
                                    }
                                } else if cmd == b"JPEG\n" {
                                    let mut seq = 0u64;
                                    while !stop_conn.load(Ordering::Relaxed) {
                                        seq += 1;
                                        let hdr = make_header(
                                            seq,
                                            session_id,
                                            JPEG,
                                            payload.len() as u32,
                                        );
                                        if stream.write_all(&hdr.pack()).is_err() {
                                            break;
                                        }
                                        if stream.write_all(&payload).is_err() {
                                            break;
                                        }
                                        thread::sleep(Duration::from_millis(15));
                                    }
                                } else if cmd == b"LABELS\n" {
                                    if let Some(final33) = stock_final33 {
                                        let mut seq = 0u64;
                                        while !stop_conn.load(Ordering::Relaxed) {
                                            seq += 1;
                                            let now = wire::monotonic_ns().max(0) as u64;
                                            let rec = StockRecord {
                                                capture_session_id: session_id,
                                                service_generation: generation,
                                                source_seq: seq,
                                                mouth_sensor_ns: seq * 11_111_111,
                                                stock_input_ns: now,
                                                vendor_ns: seq * 11_111_111 + 4,
                                                raw_identity: seq,
                                                produced_mono_ns: now,
                                                label_seq: seq,
                                                classification: 0,
                                                raw33: final33,
                                                instant33: final33,
                                                final33,
                                                quality2: [0.95, 0.05],
                                                instant_preclamp33: final33,
                                                valid: true,
                                                association_valid: true,
                                                warmup: false,
                                            };
                                            if let Ok(packed) = rec.pack() {
                                                if stream.write_all(&packed).is_err() {
                                                    break;
                                                }
                                            }
                                            thread::sleep(Duration::from_millis(20));
                                        }
                                    }
                                }
                            });
                        }
                        Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(10));
                        }
                        Err(_) => break,
                    }
                }
            });

            Self {
                port,
                stop,
                handle: Some(handle),
            }
        }
    }

    impl Drop for FakeHeadsetServer {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::SeqCst);
            if let Some(h) = self.handle.take() {
                let _ = h.join();
            }
        }
    }

    fn write_npy_bytes(descr: &str, shape: &[usize], raw: &[u8]) -> Vec<u8> {
        let shape_str = if shape.len() == 1 {
            format!("({},)", shape[0])
        } else {
            format!(
                "({})",
                shape
                    .iter()
                    .map(|d| d.to_string())
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        };
        let mut dict = format!(
            "{{'descr': '{descr}', 'fortran_order': False, 'shape': {shape_str}, }}"
        );
        while (10 + dict.len() + 1) % 64 != 0 {
            dict.push(' ');
        }
        dict.push('\n');
        let mut out = Vec::new();
        out.extend_from_slice(b"\x93NUMPY");
        out.push(1);
        out.push(0);
        out.extend_from_slice(&(dict.len() as u16).to_le_bytes());
        out.extend_from_slice(dict.as_bytes());
        out.extend_from_slice(raw);
        out
    }

    fn write_test_replay_npz(path: &Path, count: usize) {
        let file = File::create(path).unwrap();
        let mut zip = ZipWriter::new(file);
        let opts = SimpleFileOptions::default();

        let images = vec![120u8; count * 400 * 400];
        let mut seqs = Vec::with_capacity(count * 8);
        let mut times = Vec::with_capacity(count * 3 * 8);
        for i in 0..count {
            let s = (i + 1) as u64;
            seqs.extend_from_slice(&s.to_le_bytes());
            let t = s * 11_111_111;
            times.extend_from_slice(&t.to_le_bytes());
            times.extend_from_slice(&t.to_le_bytes());
            times.extend_from_slice(&t.to_le_bytes());
        }

        zip.start_file("images.npy", opts).unwrap();
        zip.write_all(&write_npy_bytes("|u1", &[count, 400, 400], &images))
            .unwrap();
        zip.start_file("seq.npy", opts).unwrap();
        zip.write_all(&write_npy_bytes("<u8", &[count], &seqs))
            .unwrap();
        zip.start_file("times.npy", opts).unwrap();
        zip.write_all(&write_npy_bytes("<u8", &[count, 3], &times))
            .unwrap();
        zip.finish().unwrap();
    }

    #[test]
    fn config_migration_validation_and_unknown_key_preservation() {
        let dir = tempfile::tempdir().unwrap();
        let cfg_path = dir.path().join("config.json");

        // Legacy segmentation config with smoothing_ms, obsolete face_source, and extra v2.7.0 eye ports.
        fs::write(
            &cfg_path,
            serde_json::to_string_pretty(&json!({
                "enabled": true,
                "model_kind": "segmentation",
                "model_path": "D:/old-seg.onnx",
                "smoothing_ms": 20.0,
                "face_source": "pc",
                "eye_left_port": 45682,
                "eye_right_port": 45683,
                "preview_camera": "mouth"
            }))
            .unwrap(),
        )
        .unwrap();

        let rt = Runtime::new(cfg_path.clone());
        let cfg = rt.config();
        assert!(!cfg.enabled, "enabled must always reset to false on startup");
        assert_eq!(cfg.model_kind, "face50", "legacy model_kind must reset to face50");
        assert_eq!(cfg.model_path, "", "legacy model_path must reset to empty");
        assert!(cfg.filter_enabled);
        assert!((cfg.filter_min_cutoff - 7.96).abs() < 0.02);
        assert_eq!(rt.status()["legacy_configuration_available"], true);

        // Configure valid fields and check extra keys + legacy_configuration preserved on disk, face_source dropped.
        let updated = rt
            .configure(json!({
                "headset_host": " 198.51.100.10 ",
                "mouth_mode": "model_only",
                "preview_fps": 20
            }))
            .unwrap();
        assert_eq!(updated.headset_host, "198.51.100.10");
        assert_eq!(updated.mouth_mode, "model_only");
        assert_eq!(updated.preview_fps, 20);

        let disk: Value = serde_json::from_str(&fs::read_to_string(&cfg_path).unwrap()).unwrap();
        assert_eq!(disk["eye_left_port"], 45682);
        assert_eq!(disk["eye_right_port"], 45683);
        assert_eq!(disk["preview_camera"], "mouth");
        assert!(disk.get("face_source").is_none());
        assert_eq!(disk["legacy_configuration"]["model_kind"], "segmentation");

        // Invalid config rejections
        assert!(rt.configure(json!({"unknown_key": 1})).is_err());
        assert!(rt.configure(json!({"preview_fps": 0})).is_err());
        assert!(rt.configure(json!({"filter_min_cutoff": 0.0})).is_err());
        assert!(rt.configure(json!({"mouth_mode": "bogus"})).is_err());
        assert!(rt
            .configure(json!({"mouth_mode": "custom", "channel_sources": null}))
            .is_err());
    }

    #[test]
    fn merge_mouth_handles_stock_model_off_and_aggregates() {
        let mut model_vals = [0.0f64; 50];
        let jaw_i = channel_index("JawOpen").unwrap();
        let tongue_l_i = channel_index("TongueLeft").unwrap();
        let cheek_puff_i = channel_index("CheekPuff").unwrap();
        let cheek_l_i = channel_index("CheekPuffLeft").unwrap();
        let cheek_r_i = channel_index("CheekPuffRight").unwrap();
        let funnel_i = channel_index("MouthFunnel").unwrap();
        let funnel_u_i = channel_index("LipFunnelUpper").unwrap();
        let funnel_l_i = channel_index("LipFunnelLower").unwrap();

        model_vals[jaw_i] = 0.22;
        model_vals[tongue_l_i] = 0.77;
        model_vals[cheek_puff_i] = 0.44;
        model_vals[cheek_l_i] = 0.41;
        model_vals[cheek_r_i] = 0.49;
        model_vals[funnel_i] = 0.33;
        model_vals[funnel_u_i] = 0.31;
        model_vals[funnel_l_i] = 0.35;

        let mask = ALL_VALID;
        let pred = json!({
            "schema": SCHEMA,
            "values": model_vals.to_vec(),
            "channels": {
                "JawOpen": 0.22,
                "TongueLeft": 0.77,
                "CheekPuff": 0.44,
                "CheekPuffLeft": 0.41,
                "CheekPuffRight": 0.49,
                "MouthFunnel": 0.33,
                "LipFunnelUpper": 0.31,
                "LipFunnelLower": 0.35,
            },
            "valid_mask": mask,
            "state": "valid",
            "fresh": true,
            "override": true,
        });

        let mut final33 = [0.0f32; 33];
        final33[jaw_i] = 0.85;
        final33[funnel_i] = 0.60;
        let now = wire::monotonic_ns();
        let rec = StockRecord {
            capture_session_id: 77,
            service_generation: 1,
            source_seq: 1,
            mouth_sensor_ns: 1000,
            stock_input_ns: 1000,
            vendor_ns: 1000,
            raw_identity: 1,
            produced_mono_ns: 0,
            label_seq: 1,
            classification: 0,
            raw33: final33,
            instant33: final33,
            final33,
            quality2: [0.9, 0.1],
            instant_preclamp33: final33,
            valid: true,
            association_valid: true,
            warmup: false,
        };
        let hdr = make_header(1, 77, JPEG, 100);
        let mut cfg = RuntimeConfig::default();
        // curated mode: JawOpen=stock, TongueLeft=model, CheekPuffLeft/Right=model, LipFunnelUpper/Lower=stock
        cfg.mouth_mode = "curated".to_string();

        let merged = merge_mouth_prediction(&pred, &hdr, &cfg, Some(&(rec.clone(), now)), None, now);
        assert!((merged["channels"]["JawOpen"].as_f64().unwrap() - 0.85).abs() < 1e-5);
        assert!((merged["channels"]["TongueLeft"].as_f64().unwrap() - 0.77).abs() < 1e-5);
        // CheekPuff children are both model -> aggregate parent CheekPuff comes from model
        assert!((merged["channels"]["CheekPuff"].as_f64().unwrap() - 0.44).abs() < 1e-5);
        // MouthFunnel children are both stock -> LipFunnelUpper/Lower and MouthFunnel come from stock final33[MouthFunnel]
        assert!((merged["channels"]["LipFunnelUpper"].as_f64().unwrap() - 0.60).abs() < 1e-5);
        assert!((merged["channels"]["LipFunnelLower"].as_f64().unwrap() - 0.60).abs() < 1e-5);
        assert!((merged["channels"]["MouthFunnel"].as_f64().unwrap() - 0.60).abs() < 1e-5);

        // Now test "off" channel sets valid_mask=1 and value=0.0
        let mut custom = resolve_channel_sources("curated", None, None);
        custom.insert("TongueLeft".to_string(), "off".to_string());
        cfg.mouth_mode = "custom".to_string();
        cfg.channel_sources = Some(custom);
        let merged_off = merge_mouth_prediction(&pred, &hdr, &cfg, Some(&(rec, now)), None, now);
        assert_eq!(merged_off["channels"]["TongueLeft"].as_f64().unwrap(), 0.0);
        assert_ne!(
            merged_off["valid_mask"].as_u64().unwrap() & (1u64 << tongue_l_i),
            0
        );
    }

    #[test]
    fn live_inference_merges_psl1_and_sends_mfr1_udp_with_hot_reload() {
        let mut stock_final = [0.0f32; 33];
        let jaw_i = channel_index("JawOpen").unwrap();
        let tongue_out_i = channel_index("TongueOut").unwrap();
        stock_final[jaw_i] = 0.75;

        let server = FakeHeadsetServer::start(9001, 42, Some(stock_final));

        let udp_rx = UdpSocket::bind("127.0.0.1:0").unwrap();
        let udp_port = udp_rx.local_addr().unwrap().port();
        udp_rx
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();

        let mut model_vals = [0.0f32; 50];
        model_vals[jaw_i] = 0.25;
        model_vals[tongue_out_i] = 0.65;
        let mask = (1u64 << jaw_i) | (1u64 << tongue_out_i);
        let factory: ModelFactory = Arc::new(move |_| Ok(Box::new(FakeFaceModel::new(model_vals, mask))));

        let dir = tempfile::tempdir().unwrap();
        let rt = Runtime::with_options(
            dir.path().join("config.json"),
            Some(factory),
            Some(dir.path().join("runs")),
            false,
        );

        rt.configure(json!({
            "headset_host": "127.0.0.1",
            "headset_port": server.port,
            "model_path": "fake.onnx",
            "mouth_mode": "curated",
            "filter_enabled": false,
            "output_enabled": true,
            "output_host": "127.0.0.1",
            "output_port": udp_port,
        }))
        .unwrap();

        rt.start(StartRequest {
            mode: Some("live".into()),
            ..Default::default()
        })
        .unwrap();

        // Wait until PSL1 stock is fresh and merged into prediction!
        wait_for(
            || {
                let snap = rt.expression_snapshot();
                snap["live_stock"]["fresh"] == true
                    && snap["prediction"]["channels"]["JawOpen"]
                        .as_f64()
                        .is_some_and(|v| (v - 0.75).abs() < 0.01)
            },
            Duration::from_secs(5),
        );

        let snap = rt.expression_snapshot();
        assert!((snap["prediction"]["channels"]["JawOpen"].as_f64().unwrap() - 0.75).abs() < 0.01);
        assert!((snap["prediction"]["channels"]["TongueOut"].as_f64().unwrap() - 0.65).abs() < 0.01);

        // Verify MFR1 UDP packet received
        let mut buf = [0u8; FACE_RESULT_BYTES];
        let mut got_merged = false;
        for _ in 0..30 {
            if let Ok((n, _)) = udp_rx.recv_from(&mut buf) {
                if n == FACE_RESULT_BYTES {
                    let pkt = unpack_face_result_packet(&buf).unwrap();
                    if pkt.state == 0 && (pkt.values[jaw_i] - 0.75).abs() < 0.01 {
                        assert!((pkt.values[tongue_out_i] - 0.65).abs() < 0.01);
                        got_merged = true;
                        break;
                    }
                }
            }
        }
        assert!(got_merged, "Expected MFR1 UDP packet with merged stock JawOpen + model TongueOut");

        // Hot-reload mouth_mode -> model_only must NOT stop tracking and must stop PSL worker!
        let cfg_after = rt.configure(json!({"mouth_mode": "model_only"})).unwrap();
        assert!(cfg_after.enabled);
        assert!(rt.is_running());
        wait_for(
            || {
                let s = rt.expression_snapshot();
                s["live_stock"]["wanted"] == false
                    && s["prediction"]["channels"]["JawOpen"]
                        .as_f64()
                        .is_some_and(|v| (v - 0.25).abs() < 0.01)
            },
            Duration::from_secs(4),
        );

        let st = rt.status();
        assert_eq!(st["version"], APP_VERSION);
        assert_eq!(st["app_version"], APP_VERSION);
        assert!(st["run_directory"].as_str().is_some());
        let run_dir = PathBuf::from(st["run_directory"].as_str().unwrap());

        rt.stop(false).unwrap();
        assert!(!rt.is_running());
        let summary_path = run_dir.join("summary.json");
        assert!(summary_path.is_file());
        assert!(run_dir.join("frames.jsonl").is_file());

        // Drain any final stopped UDP packet emitted by the first stop().
        udp_rx
            .set_read_timeout(Some(Duration::from_millis(120)))
            .unwrap();
        while udp_rx.recv_from(&mut buf).is_ok() {}

        // Repeated stop() / configure() while stopped must NOT overwrite summary.json or re-emit UDP.
        let sentinel = "{\"sentinel\":true}";
        fs::write(&summary_path, sentinel).unwrap();
        rt.configure(json!({"headset_host": "127.0.0.1"})).unwrap();
        rt.stop(false).unwrap();
        assert_eq!(fs::read_to_string(&summary_path).unwrap(), sentinel);
        assert!(
            udp_rx.recv_from(&mut buf).is_err(),
            "Repeated stop() while stopped must not re-emit UDP"
        );
    }

    #[test]
    fn npz_replay_runs_inference_and_finishes() {
        let dir = tempfile::tempdir().unwrap();
        let npz_path = dir.path().join("sample.npz");
        write_test_replay_npz(&npz_path, 4);

        let jaw_i = channel_index("JawOpen").unwrap();
        let mut model_vals = [0.0f32; 50];
        model_vals[jaw_i] = 0.55;
        let mask = 1u64 << jaw_i;
        let factory: ModelFactory =
            Arc::new(move |_| Ok(Box::new(FakeFaceModel::new(model_vals, mask))));

        let rt = Runtime::with_options(
            dir.path().join("config.json"),
            Some(factory),
            Some(dir.path().join("runs")),
            false,
        );
        rt.configure(json!({
            "model_path": "fake.onnx",
            "mouth_mode": "model_only",
            "filter_enabled": false,
            "replay_path": npz_path.to_string_lossy(),
        }))
        .unwrap();

        rt.start(StartRequest {
            mode: Some("replay".into()),
            ..Default::default()
        })
        .unwrap();

        wait_for(
            || {
                let s = rt.status();
                s["replay"]["finished"] == true && s["metrics"]["inferred"].as_u64().unwrap_or(0) >= 1
            },
            Duration::from_secs(5),
        );
        let st = rt.status();
        assert_eq!(st["replay"]["total"], 4);
        assert!(rt.preview_jpeg().is_some());
        rt.stop(false).unwrap();
    }

    #[test]
    fn recording_and_review_lifecycle_over_live_capture() {
        let mut stock_final = [0.0f32; 33];
        stock_final[0] = 0.4;
        let server = FakeHeadsetServer::start(5555, 99, Some(stock_final));

        let dir = tempfile::tempdir().unwrap();
        let rt = Runtime::new(dir.path().join("config.json"));
        rt.configure(json!({
            "headset_host": "127.0.0.1",
            "headset_port": server.port,
        }))
        .unwrap();

        rt.start(StartRequest {
            mode: Some("capture".into()),
            ..Default::default()
        })
        .unwrap();
        wait_for(|| rt.is_connected(), Duration::from_secs(4));

        let rec_st = rt
            .recording(
                "begin",
                RecordingActionOptions {
                    person_id: "p01".to_string(),
                    confirmed: true,
                    guide: false,
                    stage: "raw_only".to_string(),
                    receive_results: true,
                    ..Default::default()
                },
            )
            .unwrap();
        assert_eq!(rec_st["state"], "recording");
        let session_id = rec_st["session_id"].as_str().unwrap().to_string();

        wait_for(
            || {
                let s = rt.status();
                s["recording"]["images"].as_u64().unwrap_or(0) >= 2
                    && s["stock"]["records"].as_u64().unwrap_or(0) >= 1
            },
            Duration::from_secs(5),
        );

        let stopped = rt
            .recording("stop", RecordingActionOptions::default())
            .unwrap();
        assert_eq!(stopped["state"], "stopped");
        assert!(!rt.is_running());

        let samples_page = rt.review_samples(0, 24, true).unwrap();
        assert_eq!(samples_page["session_id"], session_id);
        let first_sample_id = samples_page["samples"][0]["sample_id"]
            .as_str()
            .unwrap()
            .to_string();
        let img_bytes = rt.review_image(&session_id, &first_sample_id).unwrap();
        assert!(img_bytes.starts_with(&[0xFF, 0xD8]));

        let mut targets = Map::new();
        targets.insert("JawOpen".to_string(), json!(0.5));
        let rev = rt
            .review(
                &session_id,
                &[first_sample_id],
                &targets,
                true,
                "tester",
                "visible",
            )
            .unwrap();
        assert_eq!(rev["samples"], 1);
    }

    #[test]
    fn localhost_udp_output_delivers_mfr1_packet_on_loopback() {
        let server = FakeHeadsetServer::start(7001, 11, None);
        let udp_rx = UdpSocket::bind("127.0.0.1:0").unwrap();
        let udp_port = udp_rx.local_addr().unwrap().port();
        udp_rx
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();

        let jaw_i = channel_index("JawOpen").unwrap();
        let mut model_vals = [0.0f32; 50];
        model_vals[jaw_i] = 0.62;
        let mask = 1u64 << jaw_i;
        let factory: ModelFactory =
            Arc::new(move |_| Ok(Box::new(FakeFaceModel::new(model_vals, mask))));

        let dir = tempfile::tempdir().unwrap();
        let rt = Runtime::with_options(
            dir.path().join("config.json"),
            Some(factory),
            Some(dir.path().join("runs")),
            false,
        );
        rt.configure(json!({
            "headset_host": "127.0.0.1",
            "headset_port": server.port,
            "model_path": "fake.onnx",
            "mouth_mode": "model_only",
            "filter_enabled": false,
            "output_enabled": true,
            "output_host": "localhost",
            "output_port": udp_port,
        }))
        .unwrap();

        rt.start(StartRequest {
            mode: Some("live".into()),
            ..Default::default()
        })
        .unwrap();

        let mut buf = [0u8; FACE_RESULT_BYTES];
        let mut received_valid = false;
        for _ in 0..30 {
            if let Ok((n, _)) = udp_rx.recv_from(&mut buf) {
                if n == FACE_RESULT_BYTES {
                    let pkt = unpack_face_result_packet(&buf).unwrap();
                    if pkt.state == 0 && (pkt.values[jaw_i] - 0.62).abs() < 0.01 {
                        received_valid = true;
                        break;
                    }
                }
            }
        }
        rt.stop(false).unwrap();
        assert!(
            received_valid,
            "Expected MFR1 UDP packet on IPv4 loopback when output_host is \"localhost\""
        );
    }

    #[test]
    fn sync_psl_worker_reaps_naturally_exited_thread_and_respawns() {
        let mut stock_final = [0.0f32; 33];
        let jaw_i = channel_index("JawOpen").unwrap();
        stock_final[jaw_i] = 0.81;
        let server = FakeHeadsetServer::start(7002, 12, Some(stock_final));

        let mut model_vals = [0.0f32; 50];
        model_vals[jaw_i] = 0.19;
        let mask = 1u64 << jaw_i;
        let factory: ModelFactory =
            Arc::new(move |_| Ok(Box::new(FakeFaceModel::new(model_vals, mask))));

        let dir = tempfile::tempdir().unwrap();
        let rt = Runtime::with_options(
            dir.path().join("config.json"),
            Some(factory),
            Some(dir.path().join("runs")),
            false,
        );
        rt.configure(json!({
            "headset_host": "127.0.0.1",
            "headset_port": server.port,
            "model_path": "fake.onnx",
            "mouth_mode": "curated",
            "filter_enabled": false,
        }))
        .unwrap();
        rt.start(StartRequest {
            mode: Some("live".into()),
            ..Default::default()
        })
        .unwrap();

        wait_for(
            || rt.expression_snapshot()["live_stock"]["fresh"] == true,
            Duration::from_secs(5),
        );

        // Simulate wants_psl() transiently flipping false so psl_worker_loop exits on its own
        // without stop_psl_worker_locked() setting stop_flag or taking psl.thread.
        {
            let mut state = rt.inner.state.lock().unwrap();
            state.config.mouth_mode = "model_only".to_string();
            if let Some(sock) = state.stock_socket.take() {
                let _ = sock.shutdown(Shutdown::Both);
            }
        }
        wait_for(
            || {
                let psl = rt.inner.psl_lock.lock().unwrap();
                psl.thread.is_some()
                    && !psl.stop_flag.load(Ordering::Acquire)
                    && psl.finished.load(Ordering::Acquire)
            },
            Duration::from_secs(5),
        );

        // Flip wants_psl() back to true and call sync_psl_worker(); it must reap the dead thread
        // and spawn a live PSL1 worker that delivers fresh stock again.
        {
            let mut state = rt.inner.state.lock().unwrap();
            state.config.mouth_mode = "curated".to_string();
            state.latest_stock = None;
            state.stock_status.records = 0;
        }
        rt.inner.sync_psl_worker();

        wait_for(
            || {
                let s = rt.status();
                s["live_stock"]["fresh"] == true && s["stock"]["records"].as_u64().unwrap_or(0) >= 1
            },
            Duration::from_secs(5),
        );
        rt.stop(false).unwrap();
    }

    #[test]
    fn train_begin_checks_face_train_script_before_stopping_live_tracking() {
        let server = FakeHeadsetServer::start(7003, 13, None);
        let dir = tempfile::tempdir().unwrap();
        let empty_scripts = dir.path().join("no-scripts");
        fs::create_dir(&empty_scripts).unwrap();

        let rt = Runtime::new(dir.path().join("config.json"));
        rt.set_script_root(empty_scripts);
        rt.configure(json!({
            "headset_host": "127.0.0.1",
            "headset_port": server.port,
        }))
        .unwrap();
        rt.start(StartRequest {
            mode: Some("capture".into()),
            ..Default::default()
        })
        .unwrap();
        wait_for(|| rt.is_connected(), Duration::from_secs(4));

        let err = rt
            .train(
                "begin",
                Some("split"),
                Some(&json!({"dataset": "d", "split": "s.json"})),
            )
            .unwrap_err();
        assert!(err.contains("face_train.py"), "unexpected error: {err}");
        assert!(
            rt.is_running() && rt.is_connected(),
            "missing face_train.py must fail before stopping live tracking"
        );
        rt.stop(false).unwrap();
    }

    #[test]
    fn configure_disk_write_failure_preserves_in_memory_config() {
        let dir = tempfile::tempdir().unwrap();
        let blocked_cfg_path = dir.path().join("config_is_a_dir");
        fs::create_dir(&blocked_cfg_path).unwrap();

        let rt = Runtime::new(blocked_cfg_path);
        let before = rt.config();
        let res = rt.configure(json!({
            "preview_fps": 25,
            "mouth_mode": "model_only",
        }));
        assert!(res.is_err(), "writing config.json onto a directory must fail");
        assert_eq!(
            rt.config(),
            before,
            "in-memory config must remain unchanged when disk write fails"
        );
    }

    #[test]
    fn personalize_restore_rejected_for_non_segmentation_models() {
        let dir = tempfile::tempdir().unwrap();
        let rt = Runtime::new(dir.path().join("config.json"));

        // Default face50 rejects restore
        assert!(rt.personalize("restore", false).is_err());

        // MouthLink must also reject restore so profile_reload cannot busy-spin
        rt.configure(json!({"model_kind": "mouthlink"})).unwrap();
        let err = rt.personalize("restore", false).unwrap_err();
        assert!(err.contains("MouthLink"), "unexpected error: {err}");
    }
}
