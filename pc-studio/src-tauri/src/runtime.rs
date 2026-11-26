use crate::config::write_text_atomic;
use crate::stock::{read_capture_context, CaptureContext, CaptureContextEvidence};
use crate::updater::APP_VERSION;
use crate::wire::{
    self, AcceptOutcome, ClockCalibration, FrameHeader, SourceOrder, WireError, END, JPEG, RAW,
};
use image::codecs::jpeg::JpegEncoder;
use image::{ExtendedColorType, ImageFormat};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::collections::VecDeque;
use std::io::Write;
use std::net::{Shutdown, TcpStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const CAMERAS: [&str; 3] = ["mouth", "eye_left", "eye_right"];
pub const DEFAULT_MOUTH_PORT: u16 = 45681;
pub const DEFAULT_EYE_LEFT_PORT: u16 = 45682;
pub const DEFAULT_EYE_RIGHT_PORT: u16 = 45683;

const KNOWN_CONFIG_KEYS: [&str; 8] = [
    "enabled",
    "headset_host",
    "headset_port",
    "eye_left_port",
    "eye_right_port",
    "preview_camera",
    "stale_ms",
    "preview_fps",
];

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RuntimeConfig {
    pub enabled: bool,
    pub headset_host: String,
    pub headset_port: u16,
    pub eye_left_port: u16,
    pub eye_right_port: u16,
    pub preview_camera: String,
    pub stale_ms: f64,
    pub preview_fps: u32,
}

impl Default for RuntimeConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            headset_host: String::new(),
            headset_port: DEFAULT_MOUTH_PORT,
            eye_left_port: DEFAULT_EYE_LEFT_PORT,
            eye_right_port: DEFAULT_EYE_RIGHT_PORT,
            preview_camera: "mouth".to_string(),
            stale_ms: 1000.0,
            preview_fps: 10,
        }
    }
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct StartRequest {
    pub mode: Option<String>,
    pub camera: Option<String>,
    pub headset_host: Option<String>,
    pub headset_port: Option<u16>,
    pub eye_left_port: Option<u16>,
    pub eye_right_port: Option<u16>,
}

#[derive(Debug, Clone)]
struct SessionTarget {
    headset_host: String,
    headset_port: u16,
    eye_left_port: u16,
    eye_right_port: u16,
    preview_camera: String,
}

impl SessionTarget {
    fn active_port(&self) -> u16 {
        match self.preview_camera.as_str() {
            "eye_left" => self.eye_left_port,
            "eye_right" => self.eye_right_port,
            _ => self.headset_port,
        }
    }
}

#[derive(Debug, Clone, Default)]
struct MetricsCounts {
    received: u64,
    decoded: u64,
    pending_dropped: u64,
    source_gaps: u64,
    duplicates: u64,
    out_of_order: u64,
    retired_session: u64,
    non_advancing_time: u64,
    superseded: u64,
    stale: u64,
    decode_errors: u64,
}

struct PendingFrame {
    header: FrameHeader,
    payload: Vec<u8>,
    received_ns: i64,
}

struct RuntimeState {
    config: RuntimeConfig,
    preserved_extra: Map<String, Value>,
    running: bool,
    connected: bool,
    mode: String,
    last_error: Option<String>,
    pending: Option<PendingFrame>,
    source: Option<FrameHeader>,
    order: SourceOrder,
    preview: Option<Vec<u8>>,
    clock: Option<ClockCalibration>,
    counts: MetricsCounts,
    received_times: VecDeque<i64>,
    decoded_times: VecDeque<i64>,
    last_header: Option<FrameHeader>,
    last_preview_ns: i64,
    lifecycle: u64,
    output_session_id: u128,
    model_version: u64,
    capture_context: Option<CaptureContextEvidence>,
    session_target: Option<SessionTarget>,
    active_socket: Option<TcpStream>,
}

struct RuntimeInner {
    config_path: PathBuf,
    source_directory: String,
    state: Mutex<RuntimeState>,
    frame_cond: Condvar,
    clock_state: Mutex<bool>,
    clock_cond: Condvar,
    stop_flag: AtomicBool,
    threads: Mutex<Vec<JoinHandle<()>>>,
}

pub struct Runtime {
    inner: Arc<RuntimeInner>,
}

impl Drop for Runtime {
    fn drop(&mut self) {
        self.close();
    }
}

fn unix_time_ns() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0)
}

fn is_valid_camera(camera: &str) -> bool {
    CAMERAS.contains(&camera)
}

fn parse_port_value(value: &Value, name: &str) -> Result<u16, String> {
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

fn validate_port_u16(value: u16, name: &str) -> Result<u16, String> {
    if value == 0 {
        Err(format!("{name} must be an integer in 1..65535"))
    } else {
        Ok(value)
    }
}

fn parse_stale_ms(value: &Value) -> Result<f64, String> {
    let err = || "stale_ms must be a finite positive number".to_string();
    let v = match value {
        Value::Bool(_) => return Err(err()),
        Value::Number(n) => n.as_f64().ok_or_else(err)?,
        Value::String(s) => s.trim().parse::<f64>().map_err(|_| err())?,
        _ => return Err(err()),
    };
    if !v.is_finite() || v <= 0.0 {
        return Err(err());
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

fn parse_host_string(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(s) => s.trim().to_string(),
        other => other.to_string().trim().to_string(),
    }
}

fn calc_age(clock: Option<&ClockCalibration>, header: &FrameHeader, pc_ns: i64) -> (f64, f64) {
    let Some(clock) = clock else {
        return (f64::INFINITY, f64::INFINITY);
    };
    let age = ((pc_ns as f64) + clock.offset_ns - (header.copy_mono_ns as f64)) / 1e6;
    if pc_ns - clock.calibrated_pc_ns > 60_000_000_000 {
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

fn encode_raw_gray_jpeg90(raw_gray: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = Vec::with_capacity(32 * 1024);
    let mut encoder = JpegEncoder::new_with_quality(&mut out, 90);
    encoder
        .encode(raw_gray, 400, 400, ExtendedColorType::L8)
        .map_err(|e| e.to_string())?;
    Ok(out)
}

impl RuntimeInner {
    fn request_clock_resync(&self) {
        let mut req = self.clock_state.lock().unwrap();
        *req = true;
        self.clock_cond.notify_all();
    }

    fn wake_clock_thread(&self) {
        let _guard = self.clock_state.lock().unwrap();
        self.clock_cond.notify_all();
    }

    fn set_clock(&self, calibrated: ClockCalibration) {
        let mut state = self.state.lock().unwrap();
        state.clock = Some(calibrated);
    }

    fn publish(&self, header: FrameHeader, payload: Vec<u8>, received_ns: i64) -> bool {
        let mut state = self.state.lock().unwrap();
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
            state.output_session_id = unix_time_ns();
            state.preview = None;
            state.last_header = None;
        }
        state.counts.received += 1;
        state.counts.source_gaps += gaps;
        if state.received_times.len() >= 900 {
            state.received_times.pop_front();
        }
        state.received_times.push_back(received_ns);
        state.source = Some(header.clone());
        if state.pending.is_some() {
            state.counts.pending_dropped += 1;
        }
        state.pending = Some(PendingFrame {
            header,
            payload,
            received_ns,
        });
        self.frame_cond.notify_one();
        true
    }

    fn interrupt_active_socket(&self) {
        let sock = {
            let mut state = self.state.lock().unwrap();
            state.active_socket.take()
        };
        if let Some(s) = sock {
            let _ = s.shutdown(Shutdown::Both);
        }
    }

    fn receive_loop(self: Arc<Self>) {
        let target = {
            let state = self.state.lock().unwrap();
            state
                .session_target
                .clone()
                .unwrap_or_else(|| SessionTarget {
                    headset_host: state.config.headset_host.clone(),
                    headset_port: state.config.headset_port,
                    eye_left_port: state.config.eye_left_port,
                    eye_right_port: state.config.eye_right_port,
                    preview_camera: state.config.preview_camera.clone(),
                })
        };

        let run_result = self.run_receive(&target);
        if let Err(err) = run_result {
            if !self.stop_flag.load(Ordering::Relaxed) {
                let mut state = self.state.lock().unwrap();
                state.last_error = Some(err);
            }
        }

        self.stop_flag.store(true, Ordering::SeqCst);
        self.wake_clock_thread();
        let mut state = self.state.lock().unwrap();
        state.running = false;
        state.connected = false;
        state.mode = "idle".to_string();
        state.config.enabled = false;
        state.active_socket = None;
        state.session_target = None;
        state.model_version += 1;
        state.pending = None;
        state.preview = None;
        self.frame_cond.notify_all();
    }

    fn run_receive(&self, target: &SessionTarget) -> Result<(), String> {
        let host = target.headset_host.trim();
        let port = target.active_port();
        if self.stop_flag.load(Ordering::Relaxed) {
            return Ok(());
        }

        if let Ok(calibrated) = wire::sync_clock(host, port, 12, Some(&self.stop_flag)) {
            self.set_clock(calibrated);
        }
        if self.stop_flag.load(Ordering::Relaxed) {
            return Ok(());
        }

        let mut context: Option<CaptureContext> = None;
        if let Ok(mut control) =
            wire::connect_cancellable(host, port, Duration::from_secs(3), Some(&self.stop_flag))
        {
            if let Ok(ctx) =
                read_capture_context(&mut control, Duration::from_secs(3), Some(&self.stop_flag))
            {
                let evidence = ctx.evidence();
                {
                    let mut state = self.state.lock().unwrap();
                    state.capture_context = Some(evidence);
                }
                context = Some(ctx);
            }
        }
        if self.stop_flag.load(Ordering::Relaxed) {
            return Ok(());
        }

        let mut sock = match wire::connect_cancellable(
            host,
            port,
            Duration::from_secs(3),
            Some(&self.stop_flag),
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
            if self.stop_flag.load(Ordering::Relaxed) {
                return Ok(());
            }
            state.active_socket = sock.try_clone().ok();
            state.connected = true;
        }

        sock.write_all(b"JPEG\n").map_err(|e| e.to_string())?;

        // Cold-start may take up to 10s for the first frame; subsequent idle timeouts use 1s.
        let mut first_frame = true;
        let mut was_idle = false;
        while !self.stop_flag.load(Ordering::Relaxed) {
            let timeout = if first_frame {
                Duration::from_secs(10)
            } else {
                Duration::from_secs(1)
            };
            let (header, payload) = match wire::read_frame(&mut sock, timeout, Some(&self.stop_flag))
            {
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
                {
                    let mut state = self.state.lock().unwrap();
                    state.clock = None;
                }
                self.request_clock_resync();
            }
            let received_ns = wire::monotonic_ns();
            if let Some(ref ctx) = context {
                ctx.require_session(header.session_id)
                    .map_err(|e| e.to_string())?;
            }
            if (header.flags & END) != 0 {
                break;
            }
            if (header.flags & (JPEG | RAW)) == 0 {
                return Err("Subscription returned a non-image frame".to_string());
            }
            self.publish(header, payload, received_ns);
        }
        Ok(())
    }

    fn clock_refresh_loop(self: Arc<Self>) {
        let mut last_sync_mono: Option<Instant> = None;
        while !self.stop_flag.load(Ordering::Relaxed) {
            let forced = {
                let guard = self.clock_state.lock().unwrap();
                let (mut guard, _) = self
                    .clock_cond
                    .wait_timeout_while(guard, Duration::from_secs(10), |requested| {
                        !*requested && !self.stop_flag.load(Ordering::Relaxed)
                    })
                    .unwrap();
                let was_forced = *guard;
                *guard = false;
                was_forced
            };
            if self.stop_flag.load(Ordering::Relaxed) {
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
                                !self.stop_flag.load(Ordering::Relaxed)
                            })
                            .unwrap();
                        if self.stop_flag.load(Ordering::Relaxed) {
                            break;
                        }
                    }
                }
            }
            last_sync_mono = Some(Instant::now());

            let (host, port) = {
                let state = self.state.lock().unwrap();
                if let Some(ref target) = state.session_target {
                    (target.headset_host.trim().to_string(), target.active_port())
                } else {
                    let port = match state.config.preview_camera.as_str() {
                        "eye_left" => state.config.eye_left_port,
                        "eye_right" => state.config.eye_right_port,
                        _ => state.config.headset_port,
                    };
                    (state.config.headset_host.trim().to_string(), port)
                }
            };
            if host.is_empty() {
                continue;
            }
            if let Ok(calibrated) = wire::sync_clock(&host, port, 3, Some(&self.stop_flag)) {
                self.set_clock(calibrated);
            }
        }
    }

    fn decode_worker_loop(self: Arc<Self>) {
        while !self.stop_flag.load(Ordering::Relaxed) {
            let (pending, stale_ms, preview_fps, model_version, clock) = {
                let guard = self.state.lock().unwrap();
                let (mut guard, _) = self
                    .frame_cond
                    .wait_timeout_while(guard, Duration::from_millis(50), |st| {
                        st.pending.is_none() && !self.stop_flag.load(Ordering::Relaxed)
                    })
                    .unwrap();
                if self.stop_flag.load(Ordering::Relaxed) {
                    break;
                }
                let Some(pending) = guard.pending.take() else {
                    continue;
                };
                (
                    pending,
                    guard.config.stale_ms,
                    guard.config.preview_fps.max(1),
                    guard.model_version,
                    guard.clock.clone(),
                )
            };

            let begin = wire::monotonic_ns();
            let has_clock = clock.is_some();
            let local_age_ms = (begin - pending.received_ns) as f64 / 1e6;
            let (_age, mut upper) = if has_clock {
                calc_age(clock.as_ref(), &pending.header, begin)
            } else {
                (local_age_ms, local_age_ms)
            };

            if upper.is_finite() && upper > stale_ms {
                if has_clock && local_age_ms <= stale_ms {
                    self.request_clock_resync();
                    if upper > (stale_ms * 2.0).max(2000.0) {
                        let mut state = self.state.lock().unwrap();
                        state.clock = None;
                        upper = local_age_ms;
                    }
                }
                if upper.is_finite() && upper > stale_ms {
                    let mut state = self.state.lock().unwrap();
                    state.counts.stale += 1;
                    continue;
                }
            }

            let is_raw = (pending.header.flags & RAW) != 0;
            let validate_res: Result<(), String> = if is_raw {
                if pending.payload.len() != 400 * 400 {
                    Err("Decoded source dimensions changed".to_string())
                } else {
                    Ok(())
                }
            } else {
                match image::load_from_memory_with_format(&pending.payload, ImageFormat::Jpeg) {
                    Ok(img) => {
                        if img.width() == 400 && img.height() == 400 {
                            Ok(())
                        } else {
                            Err("Decoded source dimensions changed".to_string())
                        }
                    }
                    Err(err) => Err(err.to_string()),
                }
            };

            if let Err(err_msg) = validate_res {
                let mut fatal = false;
                {
                    let mut state = self.state.lock().unwrap();
                    if self.stop_flag.load(Ordering::Relaxed)
                        || state.order.session != Some(pending.header.session_id)
                        || model_version != state.model_version
                    {
                        state.counts.superseded += 1;
                    } else {
                        state.counts.decode_errors += 1;
                        state.last_error = Some(format!("DecodeError: {err_msg}"));
                        state.running = false;
                        state.connected = false;
                        state.mode = "idle".to_string();
                        state.config.enabled = false;
                        fatal = true;
                    }
                }
                if fatal {
                    self.stop_flag.store(true, Ordering::SeqCst);
                    self.wake_clock_thread();
                    self.interrupt_active_socket();
                    self.frame_cond.notify_all();
                    break;
                }
                continue;
            }

            let decoded = wire::monotonic_ns();
            let (preview_version, should_preview) = {
                let mut state = self.state.lock().unwrap();
                if self.stop_flag.load(Ordering::Relaxed)
                    || state.order.session != Some(pending.header.session_id)
                    || model_version != state.model_version
                {
                    state.counts.superseded += 1;
                    continue;
                }
                state.counts.decoded += 1;
                if state.decoded_times.len() >= 900 {
                    state.decoded_times.pop_front();
                }
                state.decoded_times.push_back(decoded);
                state.last_header = Some(pending.header.clone());
                let min_interval_ns = 1e9 / (preview_fps as f64);
                let due =
                    state.last_preview_ns == 0 || (decoded - state.last_preview_ns) as f64 >= min_interval_ns;
                (state.model_version, due)
            };

            if should_preview {
                let preview_bytes = if is_raw {
                    match encode_raw_gray_jpeg90(&pending.payload) {
                        Ok(bytes) => bytes,
                        Err(err_msg) => {
                            let mut state = self.state.lock().unwrap();
                            state.counts.decode_errors += 1;
                            state.last_error = Some(format!("DecodeError: {err_msg}"));
                            state.running = false;
                            state.connected = false;
                            state.mode = "idle".to_string();
                            state.config.enabled = false;
                            self.stop_flag.store(true, Ordering::SeqCst);
                            drop(state);
                            self.wake_clock_thread();
                            self.interrupt_active_socket();
                            self.frame_cond.notify_all();
                            break;
                        }
                    }
                } else {
                    // Validated 400x400 JPEG frame: pass original bytes straight through without re-encoding.
                    pending.payload
                };

                let mut state = self.state.lock().unwrap();
                if state.order.session == Some(pending.header.session_id)
                    && !self.stop_flag.load(Ordering::Relaxed)
                    && preview_version == state.model_version
                {
                    state.preview = Some(preview_bytes);
                    state.last_preview_ns = decoded;
                }
            }
        }
    }
}

impl Runtime {
    pub fn new(config_path: PathBuf) -> Self {
        let mut config = RuntimeConfig::default();
        let mut preserved_extra = Map::new();

        if let Ok(raw) = std::fs::read_to_string(&config_path) {
            if let Ok(Value::Object(map)) = serde_json::from_str::<Value>(&raw) {
                for (key, value) in map {
                    match key.as_str() {
                        "enabled" => config.enabled = parse_bool_loose(&value),
                        "headset_host" => config.headset_host = parse_host_string(&value),
                        "headset_port" => {
                            if let Ok(p) = parse_port_value(&value, "headset_port") {
                                config.headset_port = p;
                            }
                        }
                        "eye_left_port" => {
                            if let Ok(p) = parse_port_value(&value, "eye_left_port") {
                                config.eye_left_port = p;
                            }
                        }
                        "eye_right_port" => {
                            if let Ok(p) = parse_port_value(&value, "eye_right_port") {
                                config.eye_right_port = p;
                            }
                        }
                        "preview_camera" => {
                            if let Some(s) = value.as_str() {
                                if is_valid_camera(s) {
                                    config.preview_camera = s.to_string();
                                }
                            }
                        }
                        "stale_ms" => {
                            if let Ok(v) = parse_stale_ms(&value) {
                                config.stale_ms = v;
                            }
                        }
                        "preview_fps" => {
                            if let Ok(v) = parse_preview_fps(&value) {
                                config.preview_fps = v;
                            }
                        }
                        _ => {
                            preserved_extra.insert(key, value);
                        }
                    }
                }
            }
        }

        // Opening the local service never auto-starts capture.
        config.enabled = false;
        if !is_valid_camera(&config.preview_camera) {
            config.preview_camera = "mouth".to_string();
        }

        let source_directory = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|d| d.to_string_lossy().into_owned()))
            .unwrap_or_else(|| ".".to_string());

        let state = RuntimeState {
            config,
            preserved_extra,
            running: false,
            connected: false,
            mode: "idle".to_string(),
            last_error: None,
            pending: None,
            source: None,
            order: SourceOrder::new(),
            preview: None,
            clock: None,
            counts: MetricsCounts::default(),
            received_times: VecDeque::with_capacity(900),
            decoded_times: VecDeque::with_capacity(900),
            last_header: None,
            last_preview_ns: 0,
            lifecycle: 0,
            output_session_id: unix_time_ns(),
            model_version: 0,
            capture_context: None,
            session_target: None,
            active_socket: None,
        };

        Self {
            inner: Arc::new(RuntimeInner {
                config_path,
                source_directory,
                state: Mutex::new(state),
                frame_cond: Condvar::new(),
                clock_state: Mutex::new(false),
                clock_cond: Condvar::new(),
                stop_flag: AtomicBool::new(false),
                threads: Mutex::new(Vec::new()),
            }),
        }
    }

    pub fn config(&self) -> RuntimeConfig {
        self.inner.state.lock().unwrap().config.clone()
    }

    pub fn current_headset_host(&self) -> String {
        self.inner.state.lock().unwrap().config.headset_host.clone()
    }

    pub fn configure(&self, changes: Value) -> Result<RuntimeConfig, String> {
        let Some(map) = changes.as_object() else {
            return Err("Configuration payload must be a JSON object".to_string());
        };

        let mut unknown: Vec<String> = map
            .keys()
            .filter(|k| !KNOWN_CONFIG_KEYS.contains(&k.as_str()))
            .cloned()
            .collect();
        if !unknown.is_empty() {
            unknown.sort();
            return Err(format!("Unknown configuration fields: {}", unknown.join(", ")));
        }

        let (current_cfg, was_running) = {
            let state = self.inner.state.lock().unwrap();
            (state.config.clone(), state.running)
        };

        let mut updated = current_cfg.clone();
        for (key, value) in map {
            match key.as_str() {
                "enabled" => updated.enabled = parse_bool_loose(value),
                "headset_host" => updated.headset_host = parse_host_string(value),
                "headset_port" => updated.headset_port = parse_port_value(value, "headset_port")?,
                "eye_left_port" => {
                    updated.eye_left_port = parse_port_value(value, "eye_left_port")?
                }
                "eye_right_port" => {
                    updated.eye_right_port = parse_port_value(value, "eye_right_port")?
                }
                "preview_camera" => {
                    let cam = match value {
                        Value::String(s) => s.as_str(),
                        other => {
                            return Err(format!("Unknown preview camera: {other}"));
                        }
                    };
                    if !is_valid_camera(cam) {
                        return Err(format!("Unknown preview camera: {cam}"));
                    }
                    updated.preview_camera = cam.to_string();
                }
                "stale_ms" => updated.stale_ms = parse_stale_ms(value)?,
                "preview_fps" => updated.preview_fps = parse_preview_fps(value)?,
                _ => {}
            }
        }
        updated.headset_host = updated.headset_host.trim().to_string();

        let target_changed = updated.headset_host != current_cfg.headset_host
            || updated.headset_port != current_cfg.headset_port
            || updated.eye_left_port != current_cfg.eye_left_port
            || updated.eye_right_port != current_cfg.eye_right_port
            || updated.preview_camera != current_cfg.preview_camera;

        if was_running && (target_changed || !updated.enabled) {
            self.stop()?;
            updated.enabled = false;
        }

        let mut state = self.inner.state.lock().unwrap();
        state.config = updated.clone();
        state.last_error = None;

        let mut saved_map = state.preserved_extra.clone();
        if let Value::Object(cfg_obj) = serde_json::to_value(&updated).map_err(|e| e.to_string())? {
            for (k, v) in cfg_obj {
                saved_map.insert(k, v);
            }
        }
        let serialized =
            serde_json::to_string_pretty(&Value::Object(saved_map)).map_err(|e| e.to_string())?;
        write_text_atomic(&self.inner.config_path, &serialized).map_err(|e| e.to_string())?;

        Ok(updated)
    }

    pub fn start(&self, req: StartRequest) -> Result<Value, String> {
        let mode = req.mode.as_deref().unwrap_or("capture");
        if mode != "live" && mode != "capture" {
            return Err("mode must be live or capture".to_string());
        }

        let target = {
            let mut state = self.inner.state.lock().unwrap();
            if state.running {
                return Err("Runtime is already running".to_string());
            }
            let selected_camera = match req.camera {
                Some(ref cam) => {
                    if !is_valid_camera(cam) {
                        return Err(format!("Unknown preview camera: {cam}"));
                    }
                    cam.clone()
                }
                None => state.config.preview_camera.clone(),
            };
            let host = match req.headset_host {
                Some(ref h) => h.trim().to_string(),
                None => state.config.headset_host.trim().to_string(),
            };
            if host.is_empty() {
                return Err("Enter the headset address before starting live input".to_string());
            }
            let headset_port = match req.headset_port {
                Some(p) => validate_port_u16(p, "headset_port")?,
                None => state.config.headset_port,
            };
            let eye_left_port = match req.eye_left_port {
                Some(p) => validate_port_u16(p, "eye_left_port")?,
                None => state.config.eye_left_port,
            };
            let eye_right_port = match req.eye_right_port {
                Some(p) => validate_port_u16(p, "eye_right_port")?,
                None => state.config.eye_right_port,
            };
            state.config.preview_camera = selected_camera.clone();
            SessionTarget {
                headset_host: host,
                headset_port,
                eye_left_port,
                eye_right_port,
                preview_camera: selected_camera,
            }
        };

        // Ensure any previously exited worker threads are joined cleanly.
        self.stop()?;

        {
            let mut threads = self.inner.threads.lock().unwrap();
            let generation = self.inner.state.lock().unwrap().lifecycle;
            {
                let mut clock_req = self.inner.clock_state.lock().unwrap();
                *clock_req = false;
            }
            {
                let mut state = self.inner.state.lock().unwrap();
                if generation != state.lifecycle {
                    return Err("Startup was cancelled by stop/shutdown".to_string());
                }
                self.inner.stop_flag.store(false, Ordering::SeqCst);
                state.session_target = Some(target);
                state.running = true;
                state.connected = false;
                state.mode = "live".to_string();
                state.config.enabled = true;
                state.last_error = None;
                state.pending = None;
                state.source = None;
                state.order = SourceOrder::new();
                state.preview = None;
                state.clock = None;
                state.received_times.clear();
                state.decoded_times.clear();
                state.last_header = None;
                state.capture_context = None;
                state.output_session_id = unix_time_ns();
                state.last_preview_ns = 0;
                state.counts = MetricsCounts::default();
                state.active_socket = None;
            }

            let inner_decode = Arc::clone(&self.inner);
            let inner_recv = Arc::clone(&self.inner);
            let inner_clock = Arc::clone(&self.inner);

            let t_decode = thread::Builder::new()
                .name("camera-preview".to_string())
                .spawn(move || inner_decode.decode_worker_loop())
                .map_err(|e| e.to_string())?;
            let t_recv = thread::Builder::new()
                .name("camera-source".to_string())
                .spawn(move || inner_recv.receive_loop())
                .map_err(|e| e.to_string())?;
            let t_clock = thread::Builder::new()
                .name("camera-clock".to_string())
                .spawn(move || inner_clock.clock_refresh_loop())
                .map_err(|e| e.to_string())?;

            threads.push(t_decode);
            threads.push(t_recv);
            threads.push(t_clock);
        }

        Ok(self.status())
    }

    pub fn stop(&self) -> Result<(), String> {
        self.inner.stop_flag.store(true, Ordering::SeqCst);
        self.inner.wake_clock_thread();
        let sock = {
            let mut state = self.inner.state.lock().unwrap();
            state.model_version += 1;
            state.pending = None;
            state.preview = None;
            state.lifecycle += 1;
            let sock = state.active_socket.take();
            self.inner.frame_cond.notify_all();
            sock
        };
        if let Some(s) = sock {
            let _ = s.shutdown(Shutdown::Both);
        }

        let handles = {
            let mut guard = self.inner.threads.lock().unwrap();
            std::mem::take(&mut *guard)
        };
        let current_id = thread::current().id();
        for handle in handles {
            if handle.thread().id() != current_id {
                let _ = handle.join();
            }
        }

        let mut state = self.inner.state.lock().unwrap();
        state.active_socket = None;
        state.running = false;
        state.connected = false;
        state.mode = "idle".to_string();
        state.last_error = None;
        state.config.enabled = false;
        state.session_target = None;
        state.pending = None;
        state.preview = None;
        Ok(())
    }

    pub fn close(&self) {
        let _ = self.stop();
    }

    pub fn preview_jpeg(&self) -> Option<Vec<u8>> {
        self.inner.state.lock().unwrap().preview.clone()
    }

    pub fn status(&self) -> Value {
        let state = self.inner.state.lock().unwrap();
        let now = wire::monotonic_ns();

        let source_age_ms = state.last_header.as_ref().and_then(|h| {
            let (age, _upper) = calc_age(state.clock.as_ref(), h, now);
            if age.is_finite() {
                Some(age)
            } else {
                None
            }
        });
        let receive_age_ms = state
            .received_times
            .back()
            .map(|&last| (now - last) as f64 / 1e6);
        let video_fresh = state.connected
            && receive_age_ms.is_some_and(|age| age <= state.config.stale_ms.max(1500.0));
        let clock_uncertainty_ms = state
            .clock
            .as_ref()
            .map(|c| c.uncertainty_ms)
            .filter(|v| v.is_finite());

        json!({
            "version": APP_VERSION,
            "server_pid": std::process::id(),
            "running": state.running,
            "source_mode": state.mode,
            "connected": state.connected,
            "capture_only": true,
            "preview_camera": state.config.preview_camera,
            "source": state.source.as_ref().map(|h| h.to_identity_json()),
            "output_session_id": state.output_session_id.to_string(),
            "metrics": {
                "received": state.counts.received,
                "decoded": state.counts.decoded,
                "pending_dropped": state.counts.pending_dropped,
                "source_gaps": state.counts.source_gaps,
                "duplicates": state.counts.duplicates,
                "out_of_order": state.counts.out_of_order,
                "retired_session": state.counts.retired_session,
                "non_advancing_time": state.counts.non_advancing_time,
                "superseded": state.counts.superseded,
                "stale": state.counts.stale,
                "decode_errors": state.counts.decode_errors,
                "received_hz": calc_hz(&state.received_times),
                "decoded_hz": calc_hz(&state.decoded_times),
                "source_age_ms": source_age_ms,
                "receive_age_ms": receive_age_ms,
                "video_fresh": video_fresh,
                "queue_depth": if state.pending.is_some() { 1 } else { 0 },
                "clock_uncertainty_ms": clock_uncertainty_ms,
            },
            "config": state.config,
            "capture_context": state.capture_context,
            "last_error": state.last_error,
            "run_directory": Value::Null,
            "source_directory": self.inner.source_directory,
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;
    use std::net::TcpListener;

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
        let raw = vec![fill; 400 * 400];
        let mut out = Vec::new();
        let mut enc = JpegEncoder::new_with_quality(&mut out, 95);
        enc.encode(&raw, 400, 400, ExtendedColorType::L8).unwrap();
        out
    }

    fn wait_for<F: Fn() -> bool>(pred: F, timeout: Duration) {
        let deadline = Instant::now() + timeout;
        while !pred() {
            assert!(Instant::now() < deadline, "Timed out waiting for condition");
            thread::sleep(Duration::from_millis(10));
        }
    }

    struct FakeCameraServer {
        port: u16,
        stop: Arc<AtomicBool>,
        handle: Option<JoinHandle<()>>,
    }

    impl FakeCameraServer {
        fn start(mode: &'static str, fill: u8, session_id: u64, generation: u64) -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let port = listener.local_addr().unwrap().port();
            listener.set_nonblocking(true).unwrap();

            let payload = if mode == "jpeg" {
                make_test_jpeg(fill)
            } else {
                vec![fill; 400 * 400]
            };
            let flags = if mode == "jpeg" { JPEG } else { RAW };
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
                                            flags,
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
                                }
                            });
                        }
                        Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
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

    impl Drop for FakeCameraServer {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::SeqCst);
            if let Some(h) = self.handle.take() {
                let _ = h.join();
            }
        }
    }

    #[test]
    fn config_validation_unknown_key_preservation_and_corrupt_fallback() {
        let dir = tempfile::tempdir().unwrap();
        let cfg_path = dir.path().join("config.json");
        std::fs::write(&cfg_path, "{truncated").unwrap();

        let rt_corrupt = Runtime::new(cfg_path.clone());
        assert_eq!(rt_corrupt.config().headset_port, 45681);
        assert!(!rt_corrupt.config().enabled);

        std::fs::write(
            &cfg_path,
            r#"{"enabled": true, "model_path": "D:/branch.onnx", "device": "cuda"}"#,
        )
        .unwrap();
        let rt = Runtime::new(cfg_path.clone());
        assert!(!rt.config().enabled, "enabled must always reset to false");
        assert_eq!(rt.current_headset_host(), "");

        // Start without host must match web/i18n.mjs knownErrors.
        let err = rt.start(StartRequest::default()).unwrap_err();
        assert!(err.contains("Enter the headset address"), "got: {err}");

        // Invalid configs rejected with matching messages.
        assert!(rt.configure(json!({"unknown_key": 1})).is_err());
        assert!(rt.configure(json!({"preview_fps": 0})).is_err());
        assert!(rt.configure(json!({"preview_fps": true})).is_err());
        assert!(rt.configure(json!({"preview_camera": "invalid"})).is_err());
        assert!(rt.configure(json!({"headset_port": 70000})).is_err());
        assert!(rt.configure(json!({"headset_port": true})).is_err());
        assert!(rt.configure(json!({"stale_ms": -5.0})).is_err());

        // Valid configure preserves unknown extra JSON keys on disk.
        let saved = rt
            .configure(json!({
                "headset_host": " 127.0.0.1 ",
                "preview_fps": 20,
                "preview_camera": "eye_left"
            }))
            .unwrap();
        assert_eq!(saved.headset_host, "127.0.0.1");
        assert_eq!(saved.preview_fps, 20);
        assert_eq!(saved.preview_camera, "eye_left");

        let disk: Value =
            serde_json::from_str(&std::fs::read_to_string(&cfg_path).unwrap()).unwrap();
        assert_eq!(disk["model_path"], "D:/branch.onnx");
        assert_eq!(disk["device"], "cuda");
        assert_eq!(disk["headset_host"], "127.0.0.1");
    }

    #[test]
    fn loopback_jpeg_and_raw_streaming_and_status_schema() {
        let mouth = FakeCameraServer::start("jpeg", 80, 18446744073709551000, 42);
        let eye_r = FakeCameraServer::start("raw", 190, 18446744073709551001, 43);

        let dir = tempfile::tempdir().unwrap();
        let cfg_path = dir.path().join("config.json");
        let rt = Runtime::new(cfg_path.clone());

        // Ephemeral start does not persist headset_host into config.json.
        let st_eph = rt
            .start(StartRequest {
                mode: Some("capture".into()),
                camera: Some("mouth".into()),
                headset_host: Some("127.0.0.1".into()),
                headset_port: Some(mouth.port),
                eye_left_port: None,
                eye_right_port: Some(eye_r.port),
            })
            .unwrap();
        assert_eq!(st_eph["running"], true);
        assert_eq!(st_eph["config"]["headset_host"], "");

        wait_for(
            || {
                let s = rt.status();
                rt.preview_jpeg().is_some()
                    && s["metrics"]["decoded"].as_u64().unwrap_or(0) >= 2
            },
            Duration::from_secs(4),
        );

        let st = rt.status();
        assert_eq!(st["version"], APP_VERSION);
        assert_eq!(st["capture_only"], true);
        assert_eq!(st["connected"], true);
        assert_eq!(st["metrics"]["video_fresh"], true);
        assert_eq!(
            st["source"]["session_id"],
            "18446744073709551000"
        );
        assert!(st["capture_context"].is_object());
        assert_eq!(st["fixed"]["width"], 400);
        assert_eq!(st["fixed"]["height"], 400);

        let preview = rt.preview_jpeg().unwrap();
        let decoded = image::load_from_memory_with_format(&preview, ImageFormat::Jpeg).unwrap();
        assert_eq!((decoded.width(), decoded.height()), (400, 400));

        rt.stop().unwrap();
        assert_eq!(rt.status()["running"], false);
        assert!(rt.preview_jpeg().is_none());

        // Now stream RAW frames on eye_right and verify JPEG preview encoding.
        rt.configure(json!({
            "headset_host": "127.0.0.1",
            "headset_port": mouth.port,
            "eye_right_port": eye_r.port,
            "preview_fps": 20,
        }))
        .unwrap();

        rt.start(StartRequest {
            mode: Some("capture".into()),
            camera: Some("eye_right".into()),
            ..Default::default()
        })
        .unwrap();

        wait_for(
            || {
                rt.preview_jpeg().is_some()
                    && rt.status()["metrics"]["decoded"].as_u64().unwrap_or(0) >= 2
            },
            Duration::from_secs(4),
        );
        let raw_preview = rt.preview_jpeg().unwrap();
        let decoded_raw =
            image::load_from_memory_with_format(&raw_preview, ImageFormat::Jpeg).unwrap();
        assert_eq!((decoded_raw.width(), decoded_raw.height()), (400, 400));

        rt.stop().unwrap();
        assert_eq!(rt.status()["running"], false);
    }

    #[test]
    fn corrupt_jpeg_increments_decode_errors_and_stops_cleanly() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let stop = Arc::new(AtomicBool::new(false));
        let stop_srv = Arc::clone(&stop);
        let srv = thread::spawn(move || {
            listener.set_nonblocking(true).unwrap();
            while !stop_srv.load(Ordering::Relaxed) {
                if let Ok((mut s, _)) = listener.accept() {
                    let _ = s.set_nonblocking(false);
                    let mut cmd = Vec::new();
                    let mut b = [0u8; 1];
                    while !cmd.ends_with(b"\n") {
                        if s.read(&mut b).unwrap_or(0) == 0 {
                            return;
                        }
                        cmd.push(b[0]);
                    }
                    if cmd == b"JPEG\n" {
                        let bad = b"not-a-valid-jpeg-payload".to_vec();
                        let hdr = make_header(1, 55, JPEG, bad.len() as u32);
                        let _ = s.write_all(&hdr.pack());
                        let _ = s.write_all(&bad);
                        thread::sleep(Duration::from_millis(200));
                    }
                } else {
                    thread::sleep(Duration::from_millis(10));
                }
            }
        });

        let dir = tempfile::tempdir().unwrap();
        let rt = Runtime::new(dir.path().join("config.json"));
        rt.start(StartRequest {
            mode: Some("capture".into()),
            headset_host: Some("127.0.0.1".into()),
            headset_port: Some(port),
            ..Default::default()
        })
        .unwrap();

        wait_for(|| rt.status()["running"] == false, Duration::from_secs(4));
        let st = rt.status();
        assert_eq!(st["metrics"]["decode_errors"].as_u64().unwrap_or(0), 1);
        assert!(st["last_error"].as_str().is_some());

        rt.stop().unwrap();
        stop.store(true, Ordering::SeqCst);
        let _ = srv.join();
    }

    #[test]
    fn stop_cancels_hanging_connect_promptly() {
        let dir = tempfile::tempdir().unwrap();
        let rt = Runtime::new(dir.path().join("config.json"));
        rt.start(StartRequest {
            mode: Some("capture".into()),
            headset_host: Some("198.51.100.1".into()),
            headset_port: Some(45681),
            ..Default::default()
        })
        .unwrap();
        thread::sleep(Duration::from_millis(60));
        let t0 = Instant::now();
        rt.stop().unwrap();
        assert!(t0.elapsed() < Duration::from_secs(2));
        assert_eq!(rt.status()["running"], false);
    }
}
