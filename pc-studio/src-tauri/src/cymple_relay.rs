use crate::config::write_text_atomic;
use crate::discovery::{discover_cameras, DiscoveryResult};
use crate::wire::{self, WireError, END, JPEG};
use image::codecs::jpeg::JpegEncoder;
use image::{imageops, ExtendedColorType, ImageFormat};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::io::Write;
use std::net::{Shutdown, TcpStream, ToSocketAddrs, UdpSocket};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

pub const CYMPLE_UDP_HOST: &str = "127.0.0.1";
pub const CYMPLE_UDP_PORT: u16 = 23303;
pub const DEFAULT_LEFT_PORT: u16 = 45682;
pub const DEFAULT_RIGHT_PORT: u16 = 45683;
pub const MSG_TYPE_NATIVE_FRAME: u16 = 1;
pub const MAX_CYMPLE_FRAME_BYTES: usize = 0xFFFF;
pub const CHUNK_BYTES: usize = 1400;
pub const PKT_HEADER_SIZE: usize = 16;

const VALID_ROTATIONS: [u16; 4] = [0, 90, 180, 270];
const FPS_WINDOW_NS: i64 = 1_500_000_000;
const RELAY_CONFIG_KEYS: [&str; 10] = [
    "cymple_host",
    "cymple_port",
    "left_port",
    "right_port",
    "left_rotate",
    "left_hflip",
    "left_vflip",
    "right_rotate",
    "right_hflip",
    "right_vflip",
];

pub type DiscoverFn = Box<dyn Fn(Duration) -> DiscoveryResult + Send + Sync>;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RelayConfig {
    pub cymple_host: String,
    pub cymple_port: u16,
    pub left_port: u16,
    pub right_port: u16,
    pub left_rotate: u16,
    pub left_hflip: bool,
    pub left_vflip: bool,
    pub right_rotate: u16,
    pub right_hflip: bool,
    pub right_vflip: bool,
}

impl Default for RelayConfig {
    fn default() -> Self {
        Self {
            cymple_host: CYMPLE_UDP_HOST.to_string(),
            cymple_port: CYMPLE_UDP_PORT,
            left_port: DEFAULT_LEFT_PORT,
            right_port: DEFAULT_RIGHT_PORT,
            left_rotate: 0,
            left_hflip: false,
            left_vflip: false,
            right_rotate: 0,
            right_hflip: false,
            right_vflip: false,
        }
    }
}

/// Pack one JPEG frame into Cymple native type-1 UDP chunk datagrams (`<HHIIBBH` + chunk).
pub fn encode_cymple_packets(
    jpeg: &[u8],
    channel: u8,
    frame_seq: u8,
) -> Result<Vec<Vec<u8>>, String> {
    let total = jpeg.len();
    if total == 0 || total > MAX_CYMPLE_FRAME_BYTES {
        return Err(format!(
            "JPEG size {total} outside [1, {MAX_CYMPLE_FRAME_BYTES}]"
        ));
    }

    let mut packets = Vec::with_capacity(total.div_ceil(CHUNK_BYTES));
    let mut offset = 0usize;
    while offset < total {
        let end = (offset + CHUNK_BYTES).min(total);
        let chunk = &jpeg[offset..end];
        let payload_len = (12 + chunk.len()) as u16;
        let mut pkt = Vec::with_capacity(PKT_HEADER_SIZE + chunk.len());
        pkt.extend_from_slice(&MSG_TYPE_NATIVE_FRAME.to_le_bytes());
        pkt.extend_from_slice(&payload_len.to_le_bytes());
        pkt.extend_from_slice(&(offset as u32).to_le_bytes());
        pkt.extend_from_slice(&(total as u32).to_le_bytes());
        pkt.push(channel);
        pkt.push(frame_seq);
        pkt.extend_from_slice(&0u16.to_le_bytes());
        pkt.extend_from_slice(chunk);
        packets.push(pkt);
        offset = end;
    }
    Ok(packets)
}

/// Apply optional orientation transforms and enforce Cymple's 65535-byte frame ceiling.
pub fn prepare_jpeg(
    jpeg: &[u8],
    rotate_cw: u16,
    hflip: bool,
    vflip: bool,
) -> Result<Vec<u8>, String> {
    if !VALID_ROTATIONS.contains(&rotate_cw) {
        return Err(format!(
            "Rotation must be one of (0, 90, 180, 270), got {rotate_cw}"
        ));
    }
    let needs_edit = rotate_cw != 0 || hflip || vflip;
    if !needs_edit && jpeg.len() <= MAX_CYMPLE_FRAME_BYTES {
        return Ok(jpeg.to_vec());
    }

    let img =
        image::load_from_memory_with_format(jpeg, ImageFormat::Jpeg).map_err(|e| e.to_string())?;
    let mut gray = img.to_luma8();
    match rotate_cw {
        90 => gray = imageops::rotate90(&gray),
        180 => gray = imageops::rotate180(&gray),
        270 => gray = imageops::rotate270(&gray),
        _ => {}
    }
    if hflip {
        gray = imageops::flip_horizontal(&gray);
    }
    if vflip {
        gray = imageops::flip_vertical(&gray);
    }

    for quality in [92u8, 85, 75, 65, 50, 35] {
        let mut out = Vec::with_capacity(jpeg.len().min(MAX_CYMPLE_FRAME_BYTES));
        let mut enc = JpegEncoder::new_with_quality(&mut out, quality);
        enc.encode(
            gray.as_raw(),
            gray.width(),
            gray.height(),
            ExtendedColorType::L8,
        )
        .map_err(|e| e.to_string())?;
        if out.len() <= MAX_CYMPLE_FRAME_BYTES {
            return Ok(out);
        }
    }

    Err(format!(
        "Re-encoded JPEG still exceeds {MAX_CYMPLE_FRAME_BYTES} bytes"
    ))
}

fn validate_port_field(value: &Value, key: &str) -> Result<u16, String> {
    let err = || format!("Invalid Cymple relay settings: {key} must be an integer in 1..65535");
    match value {
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                if (1..=65535).contains(&i) {
                    return Ok(i as u16);
                }
            }
            Err(err())
        }
        _ => Err(err()),
    }
}

fn validate_rotate_field(value: &Value, key: &str) -> Result<u16, String> {
    let err = || format!("Invalid Cymple relay settings: {key} must be one of (0, 90, 180, 270)");
    match value {
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                if let Ok(rot) = u16::try_from(i) {
                    if VALID_ROTATIONS.contains(&rot) {
                        return Ok(rot);
                    }
                }
            }
            Err(err())
        }
        _ => Err(err()),
    }
}

fn validate_bool_field(value: &Value, key: &str) -> Result<bool, String> {
    match value {
        Value::Bool(b) => Ok(*b),
        _ => Err(format!(
            "Invalid Cymple relay settings: {key} must be a boolean"
        )),
    }
}

pub fn validate_relay_config(base: &RelayConfig, changes: &Value) -> Result<RelayConfig, String> {
    let Some(map) = changes.as_object() else {
        return Err("Invalid Cymple relay settings: payload must be a JSON object".to_string());
    };
    let mut unknown: Vec<String> = map
        .keys()
        .filter(|k| !RELAY_CONFIG_KEYS.contains(&k.as_str()))
        .cloned()
        .collect();
    if !unknown.is_empty() {
        unknown.sort();
        return Err(format!(
            "Invalid Cymple relay settings: unknown field {}",
            unknown.join(", ")
        ));
    }

    let mut updated = base.clone();
    for (key, value) in map {
        match key.as_str() {
            "cymple_host" => {
                let Some(s) = value.as_str() else {
                    return Err(
                        "Invalid Cymple relay settings: cymple_host must be a non-empty string"
                            .to_string(),
                    );
                };
                let trimmed = s.trim();
                if trimmed.is_empty() {
                    return Err(
                        "Invalid Cymple relay settings: cymple_host must be a non-empty string"
                            .to_string(),
                    );
                }
                updated.cymple_host = trimmed.to_string();
            }
            "cymple_port" => updated.cymple_port = validate_port_field(value, "cymple_port")?,
            "left_port" => updated.left_port = validate_port_field(value, "left_port")?,
            "right_port" => updated.right_port = validate_port_field(value, "right_port")?,
            "left_rotate" => updated.left_rotate = validate_rotate_field(value, "left_rotate")?,
            "right_rotate" => {
                updated.right_rotate = validate_rotate_field(value, "right_rotate")?
            }
            "left_hflip" => updated.left_hflip = validate_bool_field(value, "left_hflip")?,
            "left_vflip" => updated.left_vflip = validate_bool_field(value, "left_vflip")?,
            "right_hflip" => updated.right_hflip = validate_bool_field(value, "right_hflip")?,
            "right_vflip" => updated.right_vflip = validate_bool_field(value, "right_vflip")?,
            _ => {}
        }
    }
    Ok(updated)
}

#[derive(Debug, Clone)]
struct EyeMetrics {
    channel: u8,
    port: u16,
    state: &'static str,
    connected: bool,
    frames: u64,
    timestamps: VecDeque<i64>,
    sizes: VecDeque<usize>,
    error: Option<String>,
}

impl EyeMetrics {
    fn new(channel: u8, port: u16, initial_state: &'static str) -> Self {
        Self {
            channel,
            port,
            state: initial_state,
            connected: false,
            frames: 0,
            timestamps: VecDeque::with_capacity(180),
            sizes: VecDeque::with_capacity(180),
            error: None,
        }
    }
}

struct RelayState {
    config: RelayConfig,
    running: bool,
    starting: bool,
    generation: u64,
    error: String,
    source_host: String,
    active_left_port: Option<u16>,
    active_right_port: Option<u16>,
    eyes: [EyeMetrics; 2],
    sockets: [Option<TcpStream>; 2],
}

impl RelayState {
    fn status_snapshot(&self) -> Value {
        let now_ns = wire::monotonic_ns();
        let cutoff_ns = now_ns - FPS_WINDOW_NS;

        let eye_json = |eye: &EyeMetrics| -> Value {
            let mut recent_count = 0usize;
            let mut first_ts = 0i64;
            let mut last_ts = 0i64;
            for &ts in &eye.timestamps {
                if ts >= cutoff_ns {
                    if recent_count == 0 {
                        first_ts = ts;
                    }
                    last_ts = ts;
                    recent_count += 1;
                }
            }
            let fps = if recent_count >= 2 && last_ts > first_ts {
                ((recent_count - 1) as f64) * 1e9 / ((last_ts - first_ts) as f64)
            } else {
                0.0
            };
            let avg_kb = if !eye.sizes.is_empty() {
                let total: usize = eye.sizes.iter().sum();
                (total as f64 / eye.sizes.len() as f64) / 1024.0
            } else {
                0.0
            };
            json!({
                "channel": eye.channel,
                "port": eye.port,
                "state": eye.state,
                "connected": eye.connected,
                "fps": fps,
                "frames": eye.frames,
                "avg_kb": avg_kb,
                "error": eye.error,
            })
        };

        let left_state = self.eyes[0].state;
        let right_state = self.eyes[1].state;
        let overall_state = if !self.running {
            "idle"
        } else if left_state == "reconnecting" || right_state == "reconnecting" {
            "reconnecting"
        } else if left_state == "streaming" && right_state == "streaming" {
            "running"
        } else {
            "connecting"
        };

        json!({
            "running": self.running,
            "state": overall_state,
            "source_host": self.source_host,
            "left_port": self.active_left_port.unwrap_or(self.config.left_port),
            "right_port": self.active_right_port.unwrap_or(self.config.right_port),
            "cymple_host": self.config.cymple_host,
            "cymple_port": self.config.cymple_port,
            "config": self.config,
            "error": self.error,
            "eyes": {
                "eye_left": eye_json(&self.eyes[0]),
                "eye_right": eye_json(&self.eyes[1]),
            },
        })
    }
}

struct RelayInner {
    config_path: Option<PathBuf>,
    discover: DiscoverFn,
    state: Mutex<RelayState>,
    stop_flag: AtomicBool,
    backoff_lock: Mutex<()>,
    backoff_cond: Condvar,
    threads: Mutex<Vec<JoinHandle<()>>>,
}

impl RelayInner {
    fn save_config(&self, cfg: &RelayConfig) -> Result<(), String> {
        let Some(ref path) = self.config_path else {
            return Ok(());
        };
        let text = serde_json::to_string_pretty(cfg).map_err(|e| e.to_string())?;
        write_text_atomic(path, &text).map_err(|e| e.to_string())
    }

    fn resolve_endpoint(
        &self,
        explicit_host: &str,
        cfg: &RelayConfig,
    ) -> Result<(String, u16, u16), String> {
        if !explicit_host.is_empty() {
            return Ok((explicit_host.to_string(), cfg.left_port, cfg.right_port));
        }
        let found = (self.discover)(Duration::from_millis(1500));
        if found.devices.is_empty() {
            return Err(
                "No PICO camera service found; enable LAN cameras on the headset or select a headset first"
                    .to_string(),
            );
        }
        if found.devices.len() > 1 {
            return Err(
                "Multiple PICO camera services found; select one headset first".to_string(),
            );
        }
        let device = &found.devices[0];
        let streams = device.streams.as_deref().unwrap_or(&[]);
        let left_port = streams
            .iter()
            .find(|s| s.id == "eye_left")
            .map(|s| s.port);
        let right_port = streams
            .iter()
            .find(|s| s.id == "eye_right")
            .map(|s| s.port);
        match (left_port, right_port) {
            (Some(lp), Some(rp)) => Ok((device.host.clone(), lp, rp)),
            _ => Err(
                "No PICO camera service found; enable LAN cameras on the headset or select a headset first"
                    .to_string(),
            ),
        }
    }

    fn wait_backoff(&self, duration: Duration) {
        let guard = self.backoff_lock.lock().unwrap();
        let _ = self
            .backoff_cond
            .wait_timeout_while(guard, duration, |_| {
                !self.stop_flag.load(Ordering::Relaxed)
            })
            .unwrap();
    }

    #[allow(clippy::too_many_arguments)]
    fn worker_loop(
        self: Arc<Self>,
        eye_idx: usize,
        channel: u8,
        src_host: String,
        src_port: u16,
        cymple_host: String,
        cymple_port: u16,
        rotate_cw: u16,
        hflip: bool,
        vflip: bool,
    ) {
        let udp_sock = match UdpSocket::bind("0.0.0.0:0") {
            Ok(s) => s,
            Err(err) => {
                if !self.stop_flag.load(Ordering::Relaxed) {
                    let mut state = self.state.lock().unwrap();
                    state.eyes[eye_idx].connected = false;
                    state.eyes[eye_idx].state = "reconnecting";
                    state.eyes[eye_idx].error = Some(err.to_string());
                }
                return;
            }
        };

        let mut seq: u8 = 0;
        let mut backoff_ms: u64 = 500;

        while !self.stop_flag.load(Ordering::Relaxed) {
            let mut wait_after = false;
            {
                let mut state = self.state.lock().unwrap();
                let eye = &mut state.eyes[eye_idx];
                if eye.frames == 0 && eye.error.is_none() {
                    eye.state = "connecting";
                }
            }

            let step_result = self.run_eye_connection(
                eye_idx,
                channel,
                &src_host,
                src_port,
                &cymple_host,
                cymple_port,
                rotate_cw,
                hflip,
                vflip,
                &udp_sock,
                &mut seq,
                &mut backoff_ms,
                &mut wait_after,
            );

            // Always drop any tracked socket clone when this connection ends.
            {
                let mut state = self.state.lock().unwrap();
                state.sockets[eye_idx] = None;
            }

            if let Err(err_msg) = step_result {
                if self.stop_flag.load(Ordering::Relaxed) {
                    break;
                }
                {
                    let mut state = self.state.lock().unwrap();
                    let eye = &mut state.eyes[eye_idx];
                    eye.connected = false;
                    eye.state = "reconnecting";
                    eye.error = Some(err_msg);
                }
                wait_after = true;
            }

            if wait_after && !self.stop_flag.load(Ordering::Relaxed) {
                self.wait_backoff(Duration::from_millis(backoff_ms));
                backoff_ms = (backoff_ms * 2).min(5000);
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn run_eye_connection(
        &self,
        eye_idx: usize,
        channel: u8,
        src_host: &str,
        src_port: u16,
        cymple_host: &str,
        cymple_port: u16,
        rotate_cw: u16,
        hflip: bool,
        vflip: bool,
        udp_sock: &UdpSocket,
        seq: &mut u8,
        backoff_ms: &mut u64,
        wait_after: &mut bool,
    ) -> Result<(), String> {
        let dst_addr = (cymple_host, cymple_port)
            .to_socket_addrs()
            .map_err(|e| e.to_string())?
            .find(|a| a.is_ipv4())
            .ok_or_else(|| format!("Unable to resolve Cymple host {cymple_host}:{cymple_port}"))?;

        let mut tcp_sock = match wire::connect_cancellable(
            src_host,
            src_port,
            Duration::from_secs(4),
            Some(&self.stop_flag),
        ) {
            Ok(s) => s,
            Err(WireError::Cancelled) => return Ok(()),
            Err(err) => return Err(err.to_string()),
        };

        let _ = tcp_sock.set_nodelay(true);
        let _ = tcp_sock.set_write_timeout(Some(Duration::from_secs(4)));
        wire::enable_keepalive(&tcp_sock, 5000, 1000);

        {
            let mut state = self.state.lock().unwrap();
            if self.stop_flag.load(Ordering::Relaxed) {
                return Ok(());
            }
            state.sockets[eye_idx] = tcp_sock.try_clone().ok();
            let eye = &mut state.eyes[eye_idx];
            eye.connected = true;
            eye.error = None;
            if eye.state == "reconnecting" {
                eye.state = "connecting";
            }
        }

        tcp_sock.write_all(b"JPEG\n").map_err(|e| e.to_string())?;

        while !self.stop_flag.load(Ordering::Relaxed) {
            let (header, payload) = match wire::read_frame(
                &mut tcp_sock,
                Duration::from_secs(4),
                Some(&self.stop_flag),
            ) {
                Ok(frame) => frame,
                Err(WireError::IdleTimeout(_)) => continue,
                Err(WireError::Cancelled) => return Ok(()),
                Err(err) => return Err(err.to_string()),
            };

            if (header.flags & END) != 0 {
                let mut state = self.state.lock().unwrap();
                if !self.stop_flag.load(Ordering::Relaxed) {
                    let eye = &mut state.eyes[eye_idx];
                    eye.connected = false;
                    eye.state = "reconnecting";
                }
                *wait_after = true;
                return Ok(());
            }

            if (header.flags & JPEG) == 0 {
                return Err("Cymple relay received a non-JPEG frame".to_string());
            }

            let jpeg = prepare_jpeg(&payload, rotate_cw, hflip, vflip)?;
            let packets = encode_cymple_packets(&jpeg, channel, *seq)?;
            for pkt in &packets {
                udp_sock
                    .send_to(pkt, dst_addr)
                    .map_err(|e| e.to_string())?;
            }
            *seq = seq.wrapping_add(1);
            *backoff_ms = 500;
            let now_ns = wire::monotonic_ns();

            let mut state = self.state.lock().unwrap();
            let eye = &mut state.eyes[eye_idx];
            eye.state = "streaming";
            eye.connected = true;
            eye.error = None;
            eye.frames += 1;
            if eye.timestamps.len() >= 180 {
                eye.timestamps.pop_front();
            }
            eye.timestamps.push_back(now_ns);
            if eye.sizes.len() >= 180 {
                eye.sizes.pop_front();
            }
            eye.sizes.push_back(jpeg.len());
        }

        Ok(())
    }
}

pub struct CympleEyeRelay {
    inner: Arc<RelayInner>,
}

impl Drop for CympleEyeRelay {
    fn drop(&mut self) {
        self.close();
    }
}

impl CympleEyeRelay {
    pub fn new(config_path: Option<PathBuf>) -> Self {
        Self::new_with_discover(
            config_path,
            Box::new(|timeout| discover_cameras(timeout, None)),
        )
    }

    pub fn new_with_discover(config_path: Option<PathBuf>, discover: DiscoverFn) -> Self {
        let mut config = RelayConfig::default();
        if let Some(ref path) = config_path {
            if let Ok(raw) = std::fs::read_to_string(path) {
                if let Ok(Value::Object(map)) = serde_json::from_str::<Value>(&raw) {
                    let mut filtered = serde_json::Map::new();
                    for key in RELAY_CONFIG_KEYS {
                        if let Some(val) = map.get(key) {
                            filtered.insert(key.to_string(), val.clone());
                        }
                    }
                    if let Ok(loaded) =
                        validate_relay_config(&RelayConfig::default(), &Value::Object(filtered))
                    {
                        config = loaded;
                    }
                }
            }
        }

        let eyes = [
            EyeMetrics::new(0, config.left_port, "idle"),
            EyeMetrics::new(1, config.right_port, "idle"),
        ];

        Self {
            inner: Arc::new(RelayInner {
                config_path,
                discover,
                state: Mutex::new(RelayState {
                    config,
                    running: false,
                    starting: false,
                    generation: 0,
                    error: String::new(),
                    source_host: String::new(),
                    active_left_port: None,
                    active_right_port: None,
                    eyes,
                    sockets: [None, None],
                }),
                stop_flag: AtomicBool::new(false),
                backoff_lock: Mutex::new(()),
                backoff_cond: Condvar::new(),
                threads: Mutex::new(Vec::new()),
            }),
        }
    }

    pub fn configure(&self, changes: Value) -> Result<Value, String> {
        let mut state = self.inner.state.lock().unwrap();
        if state.running || state.starting {
            return Err("Stop the Cymple eye relay before changing relay settings".to_string());
        }
        let updated = validate_relay_config(&state.config, &changes)?;
        state.config = updated.clone();
        state.eyes[0].port = updated.left_port;
        state.eyes[1].port = updated.right_port;
        self.inner.save_config(&updated)?;
        Ok(state.status_snapshot())
    }

    pub fn start(&self, options: Value, fallback_headset_host: &str) -> Result<Value, String> {
        let opts_map = match options {
            Value::Null => serde_json::Map::new(),
            Value::Object(m) => m,
            _ => {
                return Err(
                    "Invalid Cymple relay settings: payload must be a JSON object".to_string(),
                )
            }
        };

        let mut relay_changes = serde_json::Map::new();
        for key in RELAY_CONFIG_KEYS {
            if let Some(v) = opts_map.get(key) {
                relay_changes.insert(key.to_string(), v.clone());
            }
        }

        let (updated_cfg, generation) = {
            let mut state = self.inner.state.lock().unwrap();
            if state.running || state.starting {
                return Err("Cymple eye relay is already running".to_string());
            }
            let updated =
                validate_relay_config(&state.config, &Value::Object(relay_changes))?;
            state.config = updated.clone();
            state.eyes[0].port = updated.left_port;
            state.eyes[1].port = updated.right_port;
            self.inner.save_config(&updated)?;
            state.starting = true;
            (updated, state.generation)
        };

        let explicit_host = match opts_map.get("headset_host") {
            Some(Value::String(s)) => s.trim().to_string(),
            Some(Value::Null) => String::new(),
            Some(other) => other.to_string().trim().to_string(),
            None => fallback_headset_host.trim().to_string(),
        };

        let resolved = self.inner.resolve_endpoint(&explicit_host, &updated_cfg);
        let (src_host, left_port, right_port) = match resolved {
            Ok(ep) => ep,
            Err(err) => {
                let mut state = self.inner.state.lock().unwrap();
                state.error = err.clone();
                state.starting = false;
                return Err(err);
            }
        };

        let mut threads = self.inner.threads.lock().unwrap();
        let mut state = self.inner.state.lock().unwrap();
        if generation != state.generation {
            let err = "Cymple eye relay startup was cancelled".to_string();
            state.error = err.clone();
            state.starting = false;
            return Err(err);
        }
        if state.running {
            let err = "Cymple eye relay is already running".to_string();
            state.error = err.clone();
            state.starting = false;
            return Err(err);
        }

        self.inner.stop_flag.store(false, Ordering::SeqCst);
        state.running = true;
        state.starting = false;
        state.error.clear();
        state.source_host = src_host.clone();
        state.active_left_port = Some(left_port);
        state.active_right_port = Some(right_port);
        state.sockets = [None, None];
        state.eyes = [
            EyeMetrics::new(0, left_port, "connecting"),
            EyeMetrics::new(1, right_port, "connecting"),
        ];

        let left_inner = Arc::clone(&self.inner);
        let right_inner = Arc::clone(&self.inner);
        let src_host_l = src_host.clone();
        let src_host_r = src_host;
        let cymple_host_l = updated_cfg.cymple_host.clone();
        let cymple_host_r = updated_cfg.cymple_host.clone();
        let cymple_port = updated_cfg.cymple_port;

        let t_left = thread::Builder::new()
            .name("cymple-relay-eye_left".to_string())
            .spawn(move || {
                left_inner.worker_loop(
                    0,
                    0,
                    src_host_l,
                    left_port,
                    cymple_host_l,
                    cymple_port,
                    updated_cfg.left_rotate,
                    updated_cfg.left_hflip,
                    updated_cfg.left_vflip,
                )
            })
            .map_err(|e| e.to_string())?;
        let t_right = thread::Builder::new()
            .name("cymple-relay-eye_right".to_string())
            .spawn(move || {
                right_inner.worker_loop(
                    1,
                    1,
                    src_host_r,
                    right_port,
                    cymple_host_r,
                    cymple_port,
                    updated_cfg.right_rotate,
                    updated_cfg.right_hflip,
                    updated_cfg.right_vflip,
                )
            })
            .map_err(|e| e.to_string())?;

        threads.push(t_left);
        threads.push(t_right);
        Ok(state.status_snapshot())
    }

    pub fn stop(&self) -> Value {
        self.inner.stop_flag.store(true, Ordering::SeqCst);
        {
            let _guard = self.inner.backoff_lock.lock().unwrap();
            self.inner.backoff_cond.notify_all();
        }

        let sockets = {
            let mut state = self.inner.state.lock().unwrap();
            state.generation += 1;
            [state.sockets[0].take(), state.sockets[1].take()]
        };
        for sock in sockets.into_iter().flatten() {
            let _ = sock.shutdown(Shutdown::Both);
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
        state.sockets = [None, None];
        state.running = false;
        state.error.clear();
        state.source_host.clear();
        state.active_left_port = None;
        state.active_right_port = None;
        let left_port = state.config.left_port;
        let right_port = state.config.right_port;
        state.eyes[0].port = left_port;
        state.eyes[0].state = "idle";
        state.eyes[0].connected = false;
        state.eyes[0].error = None;
        state.eyes[1].port = right_port;
        state.eyes[1].state = "idle";
        state.eyes[1].connected = false;
        state.eyes[1].error = None;
        state.status_snapshot()
    }

    pub fn close(&self) {
        let _ = self.stop();
    }

    pub fn status(&self) -> Value {
        self.inner.state.lock().unwrap().status_snapshot()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::discovery::{CatalogStream, DiscoveredDevice};
    use crate::wire::FrameHeader;
    use image::{GrayImage, Luma};
    use std::collections::HashMap;
    use std::io::Read;
    use std::net::TcpListener;
    use std::time::Instant;

    fn corner_jpeg(width: u32, height: u32, quality: u8) -> Vec<u8> {
        let mut img = GrayImage::from_pixel(width, height, Luma([20u8]));
        for y in 0..(height / 4) {
            for x in 0..(width / 4) {
                img.put_pixel(x, y, Luma([240u8]));
            }
        }
        let mut out = Vec::new();
        let mut enc = JpegEncoder::new_with_quality(&mut out, quality);
        enc.encode(img.as_raw(), width, height, ExtendedColorType::L8)
            .unwrap();
        out
    }

    fn make_header(seq: u64, flags: u32, payload_bytes: u32) -> FrameHeader {
        let now = wire::monotonic_ns().max(0) as u64;
        FrameHeader {
            payload_bytes,
            flags,
            session_id: 80001,
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

    fn wait_for<F: Fn() -> bool>(pred: F, timeout: Duration) {
        let deadline = Instant::now() + timeout;
        while !pred() {
            assert!(Instant::now() < deadline, "Timed out waiting for condition");
            thread::sleep(Duration::from_millis(10));
        }
    }

    struct FakeEyeStream {
        port: u16,
        stop: Arc<AtomicBool>,
        handle: Option<JoinHandle<()>>,
    }

    impl FakeEyeStream {
        fn start(payload: Vec<u8>) -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let port = listener.local_addr().unwrap().port();
            listener.set_nonblocking(true).unwrap();
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
                                let mut b = [0u8; 1];
                                while !cmd.ends_with(b"\n") {
                                    match stream.read(&mut b) {
                                        Ok(1) => cmd.push(b[0]),
                                        _ => return,
                                    }
                                }
                                if cmd != b"JPEG\n" {
                                    return;
                                }
                                let mut seq = 0u64;
                                while !stop_conn.load(Ordering::Relaxed) {
                                    seq += 1;
                                    let hdr = make_header(seq, JPEG, payload.len() as u32);
                                    if stream.write_all(&hdr.pack()).is_err() {
                                        break;
                                    }
                                    if stream.write_all(&payload).is_err() {
                                        break;
                                    }
                                    thread::sleep(Duration::from_millis(15));
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

    impl Drop for FakeEyeStream {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::SeqCst);
            if let Some(h) = self.handle.take() {
                let _ = h.join();
            }
        }
    }

    struct UdpReassembler {
        port: u16,
        stop: Arc<AtomicBool>,
        frames: Arc<Mutex<[Vec<Vec<u8>>; 2]>>,
        handle: Option<JoinHandle<()>>,
    }

    impl UdpReassembler {
        fn start() -> Self {
            let sock = UdpSocket::bind("127.0.0.1:0").unwrap();
            let port = sock.local_addr().unwrap().port();
            sock.set_read_timeout(Some(Duration::from_millis(50)))
                .unwrap();
            let stop = Arc::new(AtomicBool::new(false));
            let stop_rx = Arc::clone(&stop);
            let frames: Arc<Mutex<[Vec<Vec<u8>>; 2]>> =
                Arc::new(Mutex::new([Vec::new(), Vec::new()]));
            let frames_rx = Arc::clone(&frames);

            let handle = thread::spawn(move || {
                let mut inflight: HashMap<(u8, u8, usize), HashMap<usize, Vec<u8>>> =
                    HashMap::new();
                let mut buf = [0u8; 4096];
                while !stop_rx.load(Ordering::Relaxed) {
                    let n = match sock.recv(&mut buf) {
                        Ok(n) => n,
                        Err(e)
                            if e.kind() == std::io::ErrorKind::WouldBlock
                                || e.kind() == std::io::ErrorKind::TimedOut =>
                        {
                            continue;
                        }
                        Err(_) => break,
                    };
                    if n < PKT_HEADER_SIZE {
                        continue;
                    }
                    let msg_type = u16::from_le_bytes([buf[0], buf[1]]);
                    let payload_len = u16::from_le_bytes([buf[2], buf[3]]) as usize;
                    let offset = u32::from_le_bytes([buf[4], buf[5], buf[6], buf[7]]) as usize;
                    let total = u32::from_le_bytes([buf[8], buf[9], buf[10], buf[11]]) as usize;
                    let channel = buf[12];
                    let seq_byte = buf[13];
                    if msg_type != MSG_TYPE_NATIVE_FRAME || n != payload_len + 4 {
                        continue;
                    }
                    let chunk = buf[PKT_HEADER_SIZE..n].to_vec();
                    let key = (channel, seq_byte, total);
                    let parts = inflight.entry(key).or_default();
                    parts.insert(offset, chunk);
                    let got_bytes: usize = parts.values().map(|v| v.len()).sum();
                    if got_bytes >= total {
                        let mut ordered = Vec::with_capacity(total);
                        let mut pos = 0usize;
                        while pos < total {
                            let Some(part) = parts.get(&pos) else {
                                break;
                            };
                            ordered.extend_from_slice(part);
                            pos += part.len();
                        }
                        if ordered.len() == total {
                            inflight.remove(&key);
                            if (channel as usize) < 2 {
                                frames_rx.lock().unwrap()[channel as usize].push(ordered);
                            }
                        }
                    }
                }
            });

            Self {
                port,
                stop,
                frames,
                handle: Some(handle),
            }
        }
    }

    impl Drop for UdpReassembler {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::SeqCst);
            if let Some(h) = self.handle.take() {
                let _ = h.join();
            }
        }
    }

    #[test]
    fn packet_encoding_chunks_seq_and_bounds() {
        let payload: Vec<u8> = (0..3000).map(|i| (i & 0xFF) as u8).collect();
        let packets = encode_cymple_packets(&payload, 1, 257u16 as u8).unwrap();
        assert_eq!(packets.len(), 3);

        let mut reassembled = Vec::new();
        let mut expected_offset = 0usize;
        for pkt in &packets {
            let msg_type = u16::from_le_bytes([pkt[0], pkt[1]]);
            let payload_len = u16::from_le_bytes([pkt[2], pkt[3]]) as usize;
            let offset = u32::from_le_bytes([pkt[4], pkt[5], pkt[6], pkt[7]]) as usize;
            let total = u32::from_le_bytes([pkt[8], pkt[9], pkt[10], pkt[11]]) as usize;
            let channel = pkt[12];
            let seq_byte = pkt[13];
            let reserved = u16::from_le_bytes([pkt[14], pkt[15]]);

            assert_eq!(msg_type, MSG_TYPE_NATIVE_FRAME);
            assert_eq!(pkt.len(), payload_len + 4);
            assert_eq!(offset, expected_offset);
            assert_eq!(total, 3000);
            assert_eq!(channel, 1);
            assert_eq!(seq_byte, 1);
            assert_eq!(reserved, 0);

            let chunk = &pkt[PKT_HEADER_SIZE..];
            assert!(chunk.len() <= CHUNK_BYTES);
            reassembled.extend_from_slice(chunk);
            expected_offset += chunk.len();
        }
        assert_eq!(reassembled, payload);

        assert!(encode_cymple_packets(&[], 0, 0).is_err());
        assert!(encode_cymple_packets(&vec![b'x'; MAX_CYMPLE_FRAME_BYTES + 1], 0, 0).is_err());
    }

    #[test]
    fn prepare_jpeg_passthrough_orientation_and_oversize_reencode() {
        let raw = corner_jpeg(120, 60, 95);
        assert_eq!(prepare_jpeg(&raw, 0, false, false).unwrap(), raw);

        let r90 = image::load_from_memory(&prepare_jpeg(&raw, 90, false, false).unwrap())
            .unwrap()
            .to_luma8();
        assert_eq!((r90.width(), r90.height()), (60, 120));
        assert!(r90.get_pixel(50, 10)[0] > 180);
        assert!(r90.get_pixel(10, 10)[0] < 60);

        let r180 = image::load_from_memory(&prepare_jpeg(&raw, 180, false, false).unwrap())
            .unwrap()
            .to_luma8();
        assert_eq!((r180.width(), r180.height()), (120, 60));
        assert!(r180.get_pixel(110, 50)[0] > 180);

        let r270 = image::load_from_memory(&prepare_jpeg(&raw, 270, false, false).unwrap())
            .unwrap()
            .to_luma8();
        assert_eq!((r270.width(), r270.height()), (60, 120));
        assert!(r270.get_pixel(10, 110)[0] > 180);

        let hf = image::load_from_memory(&prepare_jpeg(&raw, 0, true, false).unwrap())
            .unwrap()
            .to_luma8();
        assert!(hf.get_pixel(110, 10)[0] > 180);

        let vf = image::load_from_memory(&prepare_jpeg(&raw, 0, false, true).unwrap())
            .unwrap()
            .to_luma8();
        assert!(vf.get_pixel(10, 50)[0] > 180);

        // High-entropy 400x400 image at Q100 exceeds 65535 bytes; prepare_jpeg shrinks via quality ladder.
        let noisy: Vec<u8> = (0..(400 * 400))
            .map(|i| (((i * 73) + (i >> 3)) & 0xFF) as u8)
            .collect();
        let mut big = Vec::new();
        JpegEncoder::new_with_quality(&mut big, 100)
            .encode(&noisy, 400, 400, ExtendedColorType::L8)
            .unwrap();
        assert!(big.len() > MAX_CYMPLE_FRAME_BYTES);
        let shrunk = prepare_jpeg(&big, 0, false, false).unwrap();
        assert!(shrunk.len() <= MAX_CYMPLE_FRAME_BYTES);
    }

    #[test]
    fn end_to_end_dual_eye_udp_relay_and_persistence() {
        let left_cam = FakeEyeStream::start(corner_jpeg(400, 400, 95));
        let right_cam = FakeEyeStream::start(corner_jpeg(400, 400, 95));
        let sink = UdpReassembler::start();

        let dir = tempfile::tempdir().unwrap();
        let cfg_path = dir.path().join("cymple_relay.json");
        let relay = CympleEyeRelay::new(Some(cfg_path.clone()));

        assert_eq!(relay.status()["state"], "idle");
        assert_eq!(relay.status()["running"], false);
        assert!(!cfg_path.exists());

        let started = relay
            .start(
                json!({
                    "headset_host": "127.0.0.1",
                    "cymple_host": "127.0.0.1",
                    "cymple_port": sink.port,
                    "left_port": left_cam.port,
                    "right_port": right_cam.port,
                    "left_rotate": 90,
                    "right_vflip": true,
                }),
                "",
            )
            .unwrap();
        assert_eq!(started["running"], true);

        wait_for(
            || {
                let st = relay.status();
                let frames = sink.frames.lock().unwrap();
                st["state"] == "running"
                    && st["eyes"]["eye_left"]["frames"].as_u64().unwrap_or(0) >= 3
                    && st["eyes"]["eye_right"]["frames"].as_u64().unwrap_or(0) >= 3
                    && st["eyes"]["eye_left"]["fps"].as_f64().unwrap_or(0.0) > 0.0
                    && frames[0].len() >= 2
                    && frames[1].len() >= 2
            },
            Duration::from_secs(5),
        );

        let (im_l, im_r) = {
            let frames = sink.frames.lock().unwrap();
            (
                image::load_from_memory(frames[0].last().unwrap())
                    .unwrap()
                    .to_luma8(),
                image::load_from_memory(frames[1].last().unwrap())
                    .unwrap()
                    .to_luma8(),
            )
        };
        assert!(im_l.get_pixel(350, 50)[0] > 180);
        assert!(im_l.get_pixel(50, 50)[0] < 60);
        assert!(im_r.get_pixel(50, 350)[0] > 180);
        assert!(im_r.get_pixel(50, 50)[0] < 60);

        // Changing config while running must fail with expected message.
        let err = relay.configure(json!({"left_rotate": 180})).unwrap_err();
        assert!(err.contains("Stop the Cymple eye relay before changing"));

        let t0 = Instant::now();
        let stopped = relay.stop();
        assert!(t0.elapsed() < Duration::from_secs(2));
        assert_eq!(stopped["running"], false);
        assert_eq!(stopped["state"], "idle");
        assert!(cfg_path.exists());

        // Reloading config restores persisted fields.
        let reloaded = CympleEyeRelay::new(Some(cfg_path));
        let st_reloaded = reloaded.status();
        assert_eq!(st_reloaded["config"]["left_rotate"], 90);
        assert_eq!(st_reloaded["config"]["right_vflip"], true);
        assert_eq!(st_reloaded["config"]["cymple_port"], sink.port);
    }

    #[test]
    fn discovery_guards_validation_and_corrupt_config_fallback() {
        let dir = tempfile::tempdir().unwrap();
        let corrupt = dir.path().join("cymple_relay.json");
        std::fs::write(&corrupt, "{truncated").unwrap();
        let r_corrupt = CympleEyeRelay::new(Some(corrupt));
        assert_eq!(r_corrupt.status()["left_port"], DEFAULT_LEFT_PORT);

        let empty_relay = CympleEyeRelay::new_with_discover(
            None,
            Box::new(|_| DiscoveryResult { devices: vec![] }),
        );
        let err_empty = empty_relay.start(json!({"headset_host": ""}), "").unwrap_err();
        assert!(err_empty.contains("No PICO camera service found"));

        let multi_relay = CympleEyeRelay::new_with_discover(
            None,
            Box::new(|_| DiscoveryResult {
                devices: vec![
                    DiscoveredDevice {
                        host: "192.0.2.10".into(),
                        port: 45681,
                        name: "PICO Cameras".into(),
                        session_id: "1".into(),
                        service_generation: "1".into(),
                        streams: None,
                    },
                    DiscoveredDevice {
                        host: "192.0.2.11".into(),
                        port: 45681,
                        name: "PICO Cameras".into(),
                        session_id: "2".into(),
                        service_generation: "1".into(),
                        streams: None,
                    },
                ],
            }),
        );
        let err_multi = multi_relay.start(json!({}), "").unwrap_err();
        assert!(err_multi.contains("Multiple PICO camera services found"));

        for bad in [
            json!({"cymple_host": ""}),
            json!({"cymple_port": 0}),
            json!({"left_port": true}),
            json!({"right_port": 70000}),
            json!({"left_rotate": 45}),
            json!({"right_hflip": "yes"}),
            json!({"unknown_field": 1}),
        ] {
            let err = empty_relay.configure(bad).unwrap_err();
            assert!(err.contains("Invalid Cymple relay settings"), "got: {err}");
        }

        // Auto-discovery picks eye ports when no explicit host is passed.
        let cam_l = FakeEyeStream::start(corner_jpeg(400, 400, 95));
        let cam_r = FakeEyeStream::start(corner_jpeg(400, 400, 95));
        let sink = UdpReassembler::start();
        let lp = cam_l.port;
        let rp = cam_r.port;
        let auto_relay = CympleEyeRelay::new_with_discover(
            None,
            Box::new(move |_| DiscoveryResult {
                devices: vec![DiscoveredDevice {
                    host: "127.0.0.1".into(),
                    port: 45681,
                    name: "PICO Cameras".into(),
                    session_id: "10".into(),
                    service_generation: "20".into(),
                    streams: Some(vec![
                        CatalogStream {
                            id: "mouth".into(),
                            port: 45681,
                            path: "/stream.mjpg".into(),
                            url: None,
                            extra: Default::default(),
                        },
                        CatalogStream {
                            id: "eye_left".into(),
                            port: lp,
                            path: "/stream.mjpg".into(),
                            url: None,
                            extra: Default::default(),
                        },
                        CatalogStream {
                            id: "eye_right".into(),
                            port: rp,
                            path: "/stream.mjpg".into(),
                            url: None,
                            extra: Default::default(),
                        },
                    ]),
                }],
            }),
        );
        let st = auto_relay
            .start(json!({"cymple_host": "127.0.0.1", "cymple_port": sink.port}), "")
            .unwrap();
        assert_eq!(st["source_host"], "127.0.0.1");
        assert_eq!(st["left_port"], lp);
        assert_eq!(st["right_port"], rp);
        wait_for(
            || auto_relay.status()["eyes"]["eye_left"]["frames"].as_u64().unwrap_or(0) >= 1,
            Duration::from_secs(3),
        );
        auto_relay.stop();
    }

    #[test]
    fn stop_cancels_startup_during_discovery_and_hanging_connect() {
        // 1. Stop cancels hanging connect promptly.
        let relay = CympleEyeRelay::new(None);
        relay
            .start(json!({"headset_host": "198.51.100.9"}), "")
            .unwrap();
        thread::sleep(Duration::from_millis(60));
        let t0 = Instant::now();
        relay.stop();
        assert!(t0.elapsed() < Duration::from_secs(2));
        assert_eq!(relay.status()["running"], false);

        // 2. Stop during discovery cancels startup cleanly.
        let entered = Arc::new(AtomicBool::new(false));
        let release = Arc::new(AtomicBool::new(false));
        let entered_d = Arc::clone(&entered);
        let release_d = Arc::clone(&release);
        let race_relay = Arc::new(CympleEyeRelay::new_with_discover(
            None,
            Box::new(move |_| {
                entered_d.store(true, Ordering::SeqCst);
                while !release_d.load(Ordering::SeqCst) {
                    thread::sleep(Duration::from_millis(10));
                }
                DiscoveryResult {
                    devices: vec![DiscoveredDevice {
                        host: "127.0.0.1".into(),
                        port: 45681,
                        name: "PICO Cameras".into(),
                        session_id: "1".into(),
                        service_generation: "1".into(),
                        streams: Some(vec![
                            CatalogStream {
                                id: "mouth".into(),
                                port: 45681,
                                path: "/stream.mjpg".into(),
                                url: None,
                                extra: Default::default(),
                            },
                            CatalogStream {
                                id: "eye_left".into(),
                                port: 45682,
                                path: "/stream.mjpg".into(),
                                url: None,
                                extra: Default::default(),
                            },
                            CatalogStream {
                                id: "eye_right".into(),
                                port: 45683,
                                path: "/stream.mjpg".into(),
                                url: None,
                                extra: Default::default(),
                            },
                        ]),
                    }],
                }
            }),
        ));

        let r_start = Arc::clone(&race_relay);
        let starter = thread::spawn(move || r_start.start(json!({"headset_host": ""}), ""));
        wait_for(|| entered.load(Ordering::SeqCst), Duration::from_secs(2));
        race_relay.stop();
        release.store(true, Ordering::SeqCst);
        let res = starter.join().unwrap();
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("cancelled"));
        assert_eq!(race_relay.status()["running"], false);
    }
}
