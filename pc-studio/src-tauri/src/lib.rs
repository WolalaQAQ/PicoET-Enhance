pub mod capture_control;
pub mod config;
pub mod cymple_relay;
pub mod discovery;
pub mod face_capture;
pub mod face_dataset;
pub mod face_filter;
pub mod face_model;
pub mod face_presets;
pub mod face_prompts;
pub mod face_schema;
pub mod npz_replay;
pub mod personalization;
pub mod runtime;
pub mod state;
pub mod stock;
pub mod training;
pub mod updater;
pub mod visual_guide;
pub mod wire;

use crate::capture_control::{CaptureControl, LANCameraSource};
use crate::cymple_relay::CympleEyeRelay;
use crate::discovery::DiscoveryResult;
use crate::face_presets::{capture_continuation, SEALED_PROFILES};
use crate::runtime::{RecordingActionOptions, Runtime, RuntimeConfig, StartRequest};
use crate::updater::{UpdateChecker, UpdateInfo};
use serde_json::{json, Map, Value};
use std::fs;
use std::io::{ErrorKind, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::image::Image;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, State, WindowEvent};

pub const INSTANCE_MUTEX_NAME: &str = "Local\\PicoET-PC-Studio-Instance-Lock";
pub const STOP_EVENT_NAME: &str = "Local\\PicoET-PC-Studio-Stop-Event";

const LEGACY_HTTP_PORT: u16 = 8787;
const DISCOVERY_TIMEOUT: Duration = Duration::from_millis(1200);
const LEGACY_STATUS_MAX_BYTES: usize = 64 * 1024;

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct StudioLaunchOptions {
    pub config: Option<PathBuf>,
    pub model: Option<String>,
    pub device: String,
    pub runs: Option<PathBuf>,
    pub record_input: bool,
    pub continue_recording: Option<String>,
    pub sealed_c: Option<PathBuf>,
    pub sealed_c_profile: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CliAction {
    Version,
    Stop,
    Run(StudioLaunchOptions),
}

pub fn parse_cli_args(args: &[String]) -> Result<CliAction, String> {
    if args.iter().any(|a| a == "--version" || a == "-V") {
        return Ok(CliAction::Version);
    }
    if args.iter().any(|a| a == "--stop") {
        return Ok(CliAction::Stop);
    }

    let mut opts = StudioLaunchOptions {
        device: "auto".to_string(),
        ..Default::default()
    };

    let mut i = 0;
    while i < args.len() {
        let arg = args[i].as_str();
        let next_val = |idx: &mut usize, flag: &str| -> Result<String, String> {
            if *idx + 1 >= args.len() {
                return Err(format!("{flag} requires a value"));
            }
            *idx += 1;
            Ok(args[*idx].clone())
        };
        match arg {
            "--config" => opts.config = Some(PathBuf::from(next_val(&mut i, "--config")?)),
            "--model" => opts.model = Some(next_val(&mut i, "--model")?),
            "--device" => opts.device = next_val(&mut i, "--device")?,
            "--runs" => opts.runs = Some(PathBuf::from(next_val(&mut i, "--runs")?)),
            "--record-input" => opts.record_input = true,
            "--continue-recording" => {
                opts.continue_recording = Some(next_val(&mut i, "--continue-recording")?);
            }
            "--sealed-c" => opts.sealed_c = Some(PathBuf::from(next_val(&mut i, "--sealed-c")?)),
            "--sealed-c-profile" => {
                let p = next_val(&mut i, "--sealed-c-profile")?;
                if !SEALED_PROFILES.iter().any(|&(k, _)| k == p) {
                    return Err("--sealed-c-profile must be mouth or jaw".to_string());
                }
                opts.sealed_c_profile = Some(p);
            }
            other => {
                // Ignore unknown launcher/OS args cleanly only if empty; reject invalid flags.
                if other.starts_with('-') {
                    return Err(format!("Unknown CLI option: {other}"));
                }
            }
        }
        i += 1;
    }

    if opts.sealed_c_profile.is_some() && opts.sealed_c.is_none() {
        return Err("--sealed-c-profile requires --sealed-c".to_string());
    }
    if opts.sealed_c.is_some()
        && (opts.config.is_some()
            || opts.runs.is_some()
            || opts.model.is_some()
            || opts.record_input
            || opts.continue_recording.is_some())
    {
        return Err(
            "--sealed-c owns its config, runs and recordings and cannot be combined with other data options"
                .to_string(),
        );
    }

    Ok(CliAction::Run(opts))
}

#[cfg(not(test))]
struct TrayMenuItems {
    show: MenuItem<tauri::Wry>,
    stop_relay: MenuItem<tauri::Wry>,
    quit: MenuItem<tauri::Wry>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct TrayLabels {
    show: &'static str,
    stop_relay: &'static str,
    quit: &'static str,
}

impl TrayLabels {
    fn for_language(language: &str) -> Self {
        if language.trim().to_ascii_lowercase().starts_with("zh") {
            Self {
                show: "显示主窗口",
                stop_relay: "停止眼追转发",
                quit: "退出 PicoET PC Studio",
            }
        } else {
            Self {
                show: "Show Window",
                stop_relay: "Stop Eye Relay",
                quit: "Quit PicoET PC Studio",
            }
        }
    }
}

fn language_from_win32_lang_id(lang_id: u16) -> &'static str {
    // Primary language ID 0x04 is LANG_CHINESE (covers zh-CN/zh-TW/zh-HK/zh-SG).
    if (lang_id & 0x03ff) == 0x04 {
        "zh-CN"
    } else {
        "en"
    }
}

#[cfg(windows)]
fn default_tray_language() -> &'static str {
    use windows_sys::Win32::Globalization::GetUserDefaultUILanguage;
    let lang_id = unsafe { GetUserDefaultUILanguage() };
    language_from_win32_lang_id(lang_id)
}

#[cfg(not(windows))]
fn default_tray_language() -> &'static str {
    "en"
}

struct AtomicFlagReset<'a>(&'a AtomicBool);

impl Drop for AtomicFlagReset<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

#[cfg(windows)]
#[allow(dead_code)]
struct SyncHandles {
    mutex: usize,
    event: usize,
}

#[cfg(not(windows))]
struct SyncHandles;

pub struct AppState {
    runtime: Arc<Runtime>,
    capture: Arc<CaptureControl>,
    relay: CympleEyeRelay,
    updater: UpdateChecker,
    runtime_control: Mutex<()>,
    exiting: AtomicBool,
    allow_exit: AtomicBool,
    relay_stopping: AtomicBool,
    #[cfg(not(test))]
    tray_items: Mutex<Option<TrayMenuItems>>,
    _sync_handles: Mutex<Option<SyncHandles>>,
}

impl AppState {
    pub fn from_options(opts: StudioLaunchOptions) -> Result<Self, String> {
        let mut sealed_preset: Option<String> = None;
        let config_path = if let Some(ref sealed_dir) = opts.sealed_c {
            fs::create_dir(sealed_dir).map_err(|e| {
                format!("--sealed-c requires a new directory with an existing parent: {e}")
            })?;
            let resolved = fs::canonicalize(sealed_dir).unwrap_or_else(|_| sealed_dir.clone());
            let profile = opts.sealed_c_profile.as_deref().unwrap_or("mouth");
            sealed_preset = SEALED_PROFILES
                .iter()
                .find_map(|&(k, v)| if k == profile { Some(v.to_string()) } else { None });
            resolved.join("config.json")
        } else if let Some(cfg) = opts.config {
            cfg
        } else {
            config::resolve_data_dir().join("config.json")
        };

        let runtime = Arc::new(Runtime::with_options(
            config_path.clone(),
            None,
            opts.runs,
            opts.record_input,
        ));
        if let Some(ref model_path) = opts.model {
            runtime.configure(json!({
                "model_path": model_path,
                "device": if opts.device.is_empty() { "auto" } else { opts.device.as_str() },
            }))?;
        }

        let recordings_dir = config_path
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join("recordings");
        let continuation = if let Some(ref sid) = opts.continue_recording {
            capture_continuation(&recordings_dir, sid, false)?
        } else {
            None
        };

        let source = Arc::new(LANCameraSource::new(Arc::clone(&runtime)));
        let capture = Arc::new(CaptureControl::new(
            Arc::clone(&runtime),
            source,
            None,
            continuation,
            sealed_preset,
        ));
        let relay_config = config_path
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join("cymple_relay.json");
        let relay = CympleEyeRelay::new(Some(relay_config));

        Ok(Self {
            runtime,
            capture,
            relay,
            updater: UpdateChecker::new(),
            runtime_control: Mutex::new(()),
            exiting: AtomicBool::new(false),
            allow_exit: AtomicBool::new(false),
            relay_stopping: AtomicBool::new(false),
            #[cfg(not(test))]
            tray_items: Mutex::new(None),
            _sync_handles: Mutex::new(None),
        })
    }

    fn is_sealed_c(&self) -> bool {
        self.capture.is_sealed_c()
    }

    fn combined_status(&self) -> Value {
        let mut status = self.capture.status();
        if let Some(obj) = status.as_object_mut() {
            obj.insert("cymple_relay".to_string(), self.relay.status());
        }
        status
    }

    fn check_not_exiting(&self) -> Result<(), String> {
        if self.exiting.load(Ordering::SeqCst) {
            return Err("Application is shutting down".to_string());
        }
        Ok(())
    }

    fn check_capture_not_active(&self) -> Result<(), String> {
        if self.capture.is_active() {
            return Err(
                "Finish the capture session before changing input or training".to_string(),
            );
        }
        Ok(())
    }

    fn check_not_sealed_c(&self, msg: &str) -> Result<(), String> {
        if self.is_sealed_c() {
            return Err(msg.to_string());
        }
        Ok(())
    }

    fn handle_set_config(&self, mut changes: Value) -> Result<RuntimeConfig, String> {
        self.check_not_exiting()?;
        self.check_capture_not_active()?;
        let _guard = self.runtime_control.lock().map_err(|e| e.to_string())?;
        self.check_not_exiting()?;
        self.check_capture_not_active()?;
        if self.is_sealed_c() {
            if self.capture.state() != "idle" {
                return Err("This sealed C attempt has already started".to_string());
            }
            let Some(obj) = changes.as_object_mut() else {
                return Err("Configuration payload must be a JSON object".to_string());
            };
            obj.insert("model_path".to_string(), json!(""));
            obj.insert("enabled".to_string(), json!(false));
            obj.insert("model_kind".to_string(), json!("face50"));
            obj.insert("output_enabled".to_string(), json!(false));
        }
        self.runtime.configure(changes)
    }

    fn handle_start_runtime(&self, payload: Value) -> Result<Value, String> {
        self.check_not_exiting()?;
        self.check_capture_not_active()?;
        let req: StartRequest = if payload.is_null() {
            StartRequest::default()
        } else {
            serde_json::from_value(payload).map_err(|e| e.to_string())?
        };
        let _guard = self.runtime_control.lock().map_err(|e| e.to_string())?;
        self.check_not_exiting()?;
        self.check_capture_not_active()?;
        if self.is_sealed_c()
            && (self.capture.state() != "idle" || req.mode.as_deref() != Some("capture"))
        {
            return Err(
                "Only camera preview is available before starting sealed C".to_string(),
            );
        }
        self.runtime.start(req)?;
        Ok(self.combined_status())
    }

    fn handle_prepare_model(&self) -> Result<Value, String> {
        self.check_not_exiting()?;
        self.check_not_sealed_c("This workbench is only for sealed C capture")?;
        self.check_capture_not_active()?;
        let _guard = self.runtime_control.lock().map_err(|e| e.to_string())?;
        self.check_not_exiting()?;
        self.check_capture_not_active()?;
        self.runtime.prepare()
    }

    fn handle_personalization_action(&self, payload: Value) -> Result<Value, String> {
        self.check_not_exiting()?;
        self.check_not_sealed_c("This workbench is only for sealed C capture")?;
        self.check_capture_not_active()?;
        let obj = payload
            .as_object()
            .ok_or_else(|| "JSON object required".to_string())?;
        let action = obj.get("action").and_then(|v| v.as_str()).unwrap_or("");
        let confirmed = obj
            .get("confirmed")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let _guard = self.runtime_control.lock().map_err(|e| e.to_string())?;
        self.check_not_exiting()?;
        self.check_capture_not_active()?;
        self.runtime.personalize(action, confirmed)
    }

    fn handle_recording_action(&self, payload: Value) -> Result<Value, String> {
        self.check_not_exiting()?;
        let obj = payload
            .as_object()
            .ok_or_else(|| "JSON object required".to_string())?;
        let action = obj.get("action").and_then(|v| v.as_str()).unwrap_or("");
        let receive_results = match obj.get("receive_results") {
            None => true,
            Some(Value::Bool(b)) => *b,
            Some(_) => {
                return Err("Instant labels require receiving mouth results".to_string());
            }
        };
        let step_index = match obj.get("step_index") {
            None | Some(Value::Null) => None,
            Some(v) => Some(
                v.as_u64()
                    .ok_or_else(|| "Choose a step in the selected capture plan".to_string())?
                    as usize,
            ),
        };
        let prepare_seconds = match obj.get("prepare_seconds") {
            None | Some(Value::Null) => 3.0,
            Some(v) => v
                .as_f64()
                .ok_or_else(|| "Preparation time must be 3–30 seconds".to_string())?,
        };
        let opts = RecordingActionOptions {
            person_id: obj
                .get("person_id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            confirmed: obj
                .get("confirmed")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            guide: obj.get("guide").and_then(|v| v.as_bool()).unwrap_or(true),
            stage: obj
                .get("stage")
                .and_then(|v| v.as_str())
                .unwrap_or("raw_only")
                .to_string(),
            role: obj.get("role").and_then(|v| v.as_str()).map(String::from),
            evaluation_only: obj.get("evaluation_only").and_then(|v| v.as_bool()),
            preset_id: obj
                .get("preset_id")
                .and_then(|v| v.as_str())
                .unwrap_or("complete")
                .to_string(),
            automatic: obj
                .get("automatic")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            receive_results,
            continuation: None,
            continuation_session_id: obj
                .get("continuation_session_id")
                .and_then(|v| v.as_str())
                .map(String::from),
            step_index,
            prepare_seconds,
            guide_frame: obj
                .get("guide_frame")
                .and_then(|v| v.as_str())
                .map(String::from),
        };
        let _guard = self.runtime_control.lock().map_err(|e| e.to_string())?;
        self.check_not_exiting()?;
        self.capture.recording(action, opts)
    }

    fn handle_recording_display(&self, payload: Value) -> Result<Value, String> {
        let obj = payload
            .as_object()
            .ok_or_else(|| "JSON object required".to_string())?;
        if let Some(expected_viewer) = self.capture.display_viewer() {
            if obj.get("viewer").and_then(|v| v.as_str()) != Some(expected_viewer) {
                return Ok(json!({ "accepted": false }));
            }
        }
        let session_id = obj
            .get("session_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let frame = obj.get("frame").and_then(|v| v.as_str()).unwrap_or("");
        let phase = obj.get("phase").and_then(|v| v.as_str()).unwrap_or("");
        let visible = obj.get("visible") == Some(&Value::Bool(true));
        let media_ready = obj.get("media_ready") == Some(&Value::Bool(true));
        Ok(self
            .runtime
            .recorder_display(session_id, frame, phase, visible, media_ready))
    }

    fn handle_get_review_samples(
        &self,
        offset: Option<usize>,
        limit: Option<usize>,
        all: Option<bool>,
    ) -> Result<Value, String> {
        self.check_not_sealed_c("Sealed C archives are not reviewed in the capture workbench")?;
        self.runtime
            .review_samples(offset.unwrap_or(0), limit.unwrap_or(24), all.unwrap_or(false))
    }

    fn handle_get_review_image(&self, session: &str, sample: &str) -> Result<Vec<u8>, String> {
        self.check_not_sealed_c("Sealed C archives are not reviewed in the capture workbench")?;
        self.runtime.review_image(session, sample)
    }

    fn handle_recording_review(&self, payload: Value) -> Result<Value, String> {
        self.check_not_exiting()?;
        self.check_not_sealed_c("This workbench is only for sealed C capture")?;
        self.check_capture_not_active()?;
        let obj = payload
            .as_object()
            .ok_or_else(|| "JSON object required".to_string())?;
        let session_id = obj
            .get("session_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let sample_ids: Vec<String> = obj
            .get("sample_ids")
            .and_then(|v| v.as_array())
            .ok_or_else(|| {
                "Review requires a target dictionary and selected sample IDs".to_string()
            })?
            .iter()
            .map(|v| {
                v.as_str()
                    .map(String::from)
                    .ok_or_else(|| {
                        "Review requires a target dictionary and selected sample IDs".to_string()
                    })
            })
            .collect::<Result<Vec<_>, _>>()?;
        let targets: &Map<String, Value> = obj
            .get("targets")
            .and_then(|v| v.as_object())
            .ok_or_else(|| {
                "Review requires a target dictionary and selected sample IDs".to_string()
            })?;
        let confirmed = obj
            .get("confirmed")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let reviewer = obj.get("reviewer").and_then(|v| v.as_str()).unwrap_or("");
        let observation = obj
            .get("observation")
            .and_then(|v| v.as_str())
            .unwrap_or("visible");
        let _guard = self.runtime_control.lock().map_err(|e| e.to_string())?;
        self.check_not_exiting()?;
        self.check_capture_not_active()?;
        self.runtime
            .review(session_id, &sample_ids, targets, confirmed, reviewer, observation)
    }

    fn handle_training_action(&self, payload: Value) -> Result<Value, String> {
        self.check_not_exiting()?;
        self.check_not_sealed_c("This workbench is only for sealed C capture")?;
        self.check_capture_not_active()?;
        let obj = payload
            .as_object()
            .ok_or_else(|| "JSON object required".to_string())?;
        let action = obj.get("action").and_then(|v| v.as_str()).unwrap_or("");
        let operation = obj.get("operation").and_then(|v| v.as_str());
        let args = obj.get("args");
        let _guard = self.runtime_control.lock().map_err(|e| e.to_string())?;
        self.check_not_exiting()?;
        self.check_capture_not_active()?;
        self.runtime.train(action, operation, args)
    }

    fn handle_cymple_relay_action(&self, payload: Value) -> Result<Value, String> {
        self.check_not_exiting()?;
        self.check_not_sealed_c("This workbench is only for sealed C capture")?;
        self.check_capture_not_active()?;
        let obj = payload
            .as_object()
            .ok_or_else(|| "JSON object required".to_string())?;
        let action = obj
            .get("action")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let mut rest = Map::new();
        for (k, v) in obj {
            if k != "action" {
                rest.insert(k.clone(), v.clone());
            }
        }
        let body = Value::Object(rest);
        match action.as_str() {
            "start" => {
                let fallback_host = self.runtime.current_headset_host();
                self.relay.start(body, &fallback_host)
            }
            "stop" => Ok(self.relay.stop()),
            "configure" => self.relay.configure(body),
            _ => Err("Unknown Cymple relay action".to_string()),
        }
    }

    fn handle_stop_runtime(&self) -> Result<Value, String> {
        let _guard = self.runtime_control.lock().map_err(|e| e.to_string())?;
        let _ = self.capture.stop();
        Ok(self.combined_status())
    }

    fn shutdown_all(&self) {
        let _ = self.runtime.train("cancel", None, None);
        self.capture.close();
        self.relay.close();
        let _guard = self.runtime_control.lock();
        self.runtime.close();
    }
}

pub fn validate_selected_model_path(selected: Option<PathBuf>) -> Result<Value, String> {
    let Some(path) = selected else {
        return Ok(json!({ "cancelled": true }));
    };
    let resolved = fs::canonicalize(&path).unwrap_or(path);
    let resolved_str = strip_win_verbatim_prefix(&resolved);
    let normalized = PathBuf::from(&resolved_str);
    let is_onnx = normalized
        .extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| e.eq_ignore_ascii_case("onnx"));
    if !is_onnx || !normalized.is_file() {
        return Err("Whole-mouth inference requires an ONNX model".to_string());
    }
    if !normalized.with_extension("json").is_file() {
        return Err("The adjacent JSON model manifest was not found".to_string());
    }
    let name = normalized
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("")
        .to_string();
    Ok(json!({
        "cancelled": false,
        "path": resolved_str,
        "name": name,
    }))
}

fn strip_win_verbatim_prefix(path: &Path) -> String {
    let s = path.to_string_lossy();
    #[cfg(windows)]
    if let Some(stripped) = s.strip_prefix(r"\\?\") {
        return stripped.to_string();
    }
    s.into_owned()
}

#[cfg(windows)]
fn to_wide_null(text: &str) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    std::ffi::OsStr::new(text)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}

/// Attach to parent console when invoked with `--version` from an interactive terminal
/// while preserving redirected stdout pipes (e.g., `subprocess.run(..., capture_output=True)`).
#[cfg(windows)]
pub fn attach_parent_console_if_needed() {
    use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
    use windows_sys::Win32::System::Console::{
        AttachConsole, GetStdHandle, ATTACH_PARENT_PROCESS, STD_OUTPUT_HANDLE,
    };
    unsafe {
        let stdout = GetStdHandle(STD_OUTPUT_HANDLE);
        if stdout.is_null() || stdout == INVALID_HANDLE_VALUE {
            let _ = AttachConsole(ATTACH_PARENT_PROCESS);
        }
    }
}

#[cfg(not(windows))]
pub fn attach_parent_console_if_needed() {}

#[cfg(windows)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum NamedStopStatus {
    NotRunning,
    Stopped,
    TimedOut,
}

#[cfg(windows)]
fn signal_and_wait_named_instance(
    event_name: &str,
    mutex_name: &str,
    wait_ms: u32,
) -> NamedStopStatus {
    use windows_sys::Win32::Foundation::{CloseHandle, WAIT_ABANDONED, WAIT_OBJECT_0};
    use windows_sys::Win32::Storage::FileSystem::SYNCHRONIZE;
    use windows_sys::Win32::System::Threading::{
        OpenEventW, OpenMutexW, ReleaseMutex, SetEvent, WaitForSingleObject, EVENT_MODIFY_STATE,
        MUTEX_MODIFY_STATE,
    };

    let event_w = to_wide_null(event_name);
    let mutex_w = to_wide_null(mutex_name);

    unsafe {
        let event = OpenEventW(EVENT_MODIFY_STATE, 0, event_w.as_ptr());
        if event.is_null() {
            return NamedStopStatus::NotRunning;
        }
        let mutex = OpenMutexW(SYNCHRONIZE | MUTEX_MODIFY_STATE, 0, mutex_w.as_ptr());
        let _ = SetEvent(event);
        let _ = CloseHandle(event);

        if !mutex.is_null() {
            let rc = WaitForSingleObject(mutex, wait_ms);
            if rc == WAIT_OBJECT_0 || rc == WAIT_ABANDONED {
                let _ = ReleaseMutex(mutex);
                let _ = CloseHandle(mutex);
                std::thread::sleep(Duration::from_millis(150));
                return NamedStopStatus::Stopped;
            }
            let _ = CloseHandle(mutex);
            return NamedStopStatus::TimedOut;
        }
        std::thread::sleep(Duration::from_millis(150));
        NamedStopStatus::Stopped
    }
}

fn decode_legacy_chunked_body(raw: &[u8]) -> Option<Vec<u8>> {
    match discovery::decode_chunked_body(raw, LEGACY_STATUS_MAX_BYTES) {
        discovery::ChunkedStatus::Complete(decoded) => Some(decoded),
        discovery::ChunkedStatus::Incomplete | discovery::ChunkedStatus::Invalid => None,
    }
}

fn probe_legacy_picoet_status(addr: SocketAddr, port: u16) -> bool {
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(300)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(500)));
    let req = format!(
        "GET /api/status HTTP/1.1\r\n\
         Host: 127.0.0.1:{port}\r\n\
         Connection: close\r\n\
         \r\n"
    );
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }

    let deadline = Instant::now() + Duration::from_millis(1500);
    let mut buf = Vec::new();
    let mut chunk = [0u8; 2048];
    while buf.len() < LEGACY_STATUS_MAX_BYTES && Instant::now() < deadline {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                let take = n.min(LEGACY_STATUS_MAX_BYTES - buf.len());
                buf.extend_from_slice(&chunk[..take]);
            }
            Err(err) if err.kind() == ErrorKind::Interrupted => continue,
            Err(_) => break,
        }
    }

    let Some(header_end) = buf.windows(4).position(|w| w == b"\r\n\r\n") else {
        return false;
    };
    let Ok(header_text) = std::str::from_utf8(&buf[..header_end]) else {
        return false;
    };
    let mut lines = header_text.lines();
    let Some(status_line) = lines.next() else {
        return false;
    };
    let mut parts = status_line.split_whitespace();
    let Some(http_ver) = parts.next() else {
        return false;
    };
    let Some(status_code) = parts.next() else {
        return false;
    };
    if !http_ver.starts_with("HTTP/1.") || status_code != "200" {
        return false;
    }

    let is_chunked = lines.any(|line| {
        if let Some((name, val)) = line.split_once(':') {
            name.trim().eq_ignore_ascii_case("transfer-encoding")
                && val
                    .split(',')
                    .any(|tok| tok.trim().eq_ignore_ascii_case("chunked"))
        } else {
            false
        }
    });

    let body_raw = &buf[header_end + 4..];
    let body_bytes = if is_chunked {
        let Some(decoded) = decode_legacy_chunked_body(body_raw) else {
            return false;
        };
        decoded
    } else {
        body_raw.to_vec()
    };

    let Ok(json) = serde_json::from_slice::<Value>(&body_bytes) else {
        return false;
    };
    json.as_object()
        .is_some_and(|obj| obj.contains_key("cymple_relay") && obj.contains_key("version"))
}

fn stop_legacy_python_instance(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    if !probe_legacy_picoet_status(addr, port) {
        return true;
    }
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(300)) else {
        return true;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(1)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
    let req = format!(
        "POST /api/shutdown HTTP/1.1\r\n\
         Host: 127.0.0.1:{port}\r\n\
         Origin: http://127.0.0.1:{port}\r\n\
         Content-Type: application/json\r\n\
         Content-Length: 2\r\n\
         Connection: close\r\n\
         \r\n\
         {{}}"
    );
    if stream.write_all(req.as_bytes()).is_err() {
        return true;
    }
    let mut buf = [0u8; 512];
    let _ = stream.read(&mut buf);
    drop(stream);

    let deadline = Instant::now() + Duration::from_millis(3000);
    while Instant::now() < deadline {
        if TcpStream::connect_timeout(&addr, Duration::from_millis(200)).is_err() {
            std::thread::sleep(Duration::from_millis(250));
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    false
}

pub fn stop_running_instance() -> i32 {
    let mut timed_out = false;
    #[cfg(windows)]
    {
        if signal_and_wait_named_instance(STOP_EVENT_NAME, INSTANCE_MUTEX_NAME, 7000)
            == NamedStopStatus::TimedOut
        {
            timed_out = true;
        }
    }
    if !stop_legacy_python_instance(LEGACY_HTTP_PORT) {
        timed_out = true;
    }
    if timed_out {
        1
    } else {
        0
    }
}

#[cfg(windows)]
fn setup_instance_sync(app: &AppHandle, state: &Arc<AppState>) {
    use windows_sys::Win32::Foundation::{
        GetLastError, ERROR_ALREADY_EXISTS, HANDLE, WAIT_OBJECT_0,
    };
    use windows_sys::Win32::System::Threading::{
        CreateEventW, CreateMutexW, ResetEvent, WaitForSingleObject, INFINITE,
    };

    let mutex_w = to_wide_null(INSTANCE_MUTEX_NAME);
    let event_w = to_wide_null(STOP_EVENT_NAME);

    unsafe {
        let mutex = CreateMutexW(std::ptr::null(), 1, mutex_w.as_ptr());
        if !mutex.is_null() && GetLastError() == ERROR_ALREADY_EXISTS {
            let _ = WaitForSingleObject(mutex, 3000);
        }
        let event = CreateEventW(std::ptr::null(), 1, 0, event_w.as_ptr());
        if !event.is_null() && GetLastError() == ERROR_ALREADY_EXISTS {
            let _ = ResetEvent(event);
        }

        if let Ok(mut guard) = state._sync_handles.lock() {
            *guard = Some(SyncHandles {
                mutex: mutex as usize,
                event: event as usize,
            });
        }

        if !event.is_null() {
            let event_raw = event as usize;
            let app_handle = app.clone();
            let _ = std::thread::Builder::new()
                .name("stop-event-watcher".to_string())
                .spawn(move || {
                    let rc = WaitForSingleObject(event_raw as HANDLE, INFINITE);
                    if rc == WAIT_OBJECT_0 {
                        initiate_shutdown(&app_handle);
                    }
                });
        }
    }
}

#[cfg(not(windows))]
fn setup_instance_sync(_app: &AppHandle, _state: &Arc<AppState>) {}

fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn initiate_shutdown(app: &AppHandle) {
    let state = app.state::<Arc<AppState>>().inner().clone();
    if state.exiting.swap(true, Ordering::SeqCst) {
        return;
    }
    state.shutdown_all();
    state.allow_exit.store(true, Ordering::SeqCst);
    app.exit(0);
}

fn validate_external_url(url: &str) -> Result<String, String> {
    let trimmed = url.trim();
    if trimmed.is_empty()
        || trimmed
            .chars()
            .any(|c| c.is_control() || c.is_whitespace() || c == '"' || c == '\\')
    {
        return Err("Invalid external URL".to_string());
    }
    if !trimmed
        .as_bytes()
        .get(..8)
        .is_some_and(|prefix| prefix.eq_ignore_ascii_case(b"https://"))
        || trimmed.as_bytes().get(8) == Some(&b'/')
    {
        return Err("Only https:// URLs are allowed".to_string());
    }
    let parsed =
        reqwest::Url::parse(trimmed).map_err(|_| "Only https:// URLs are allowed".to_string())?;
    if parsed.scheme() != "https" || !parsed.has_host() {
        return Err("Only https:// URLs are allowed".to_string());
    }
    Ok(trimmed.to_string())
}

fn is_allowed_webview_url(url: &tauri::Url) -> bool {
    match url.scheme() {
        "tauri" => url.host_str() == Some("localhost"),
        "http" | "https" => url.host_str() == Some("tauri.localhost") && url.port().is_none(),
        _ => false,
    }
}

#[cfg(windows)]
struct ComApartmentGuard {
    should_uninit: bool,
}

#[cfg(windows)]
impl ComApartmentGuard {
    fn init() -> Self {
        use windows_sys::Win32::Foundation::{S_FALSE, S_OK};
        use windows_sys::Win32::System::Com::{
            CoInitializeEx, COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE,
        };
        let hr = unsafe {
            CoInitializeEx(
                std::ptr::null(),
                (COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE) as u32,
            )
        };
        Self {
            should_uninit: hr == S_OK || hr == S_FALSE,
        }
    }
}

#[cfg(windows)]
impl Drop for ComApartmentGuard {
    fn drop(&mut self) {
        if self.should_uninit {
            unsafe {
                windows_sys::Win32::System::Com::CoUninitialize();
            }
        }
    }
}

#[cfg(windows)]
fn open_url_os(url: &str) -> Result<(), String> {
    use windows_sys::Win32::UI::Shell::ShellExecuteW;
    use windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;

    let url_owned = url.to_string();
    let handle = std::thread::Builder::new()
        .name("open-external-url".to_string())
        .spawn(move || {
            let _com = ComApartmentGuard::init();
            let op_w = to_wide_null("open");
            let url_w = to_wide_null(&url_owned);
            let rc = unsafe {
                ShellExecuteW(
                    std::ptr::null_mut(),
                    op_w.as_ptr(),
                    url_w.as_ptr(),
                    std::ptr::null(),
                    std::ptr::null(),
                    SW_SHOWNORMAL,
                )
            };
            if (rc as isize) <= 32 {
                return Err(format!("Failed to open URL (code {})", rc as isize));
            }
            Ok(())
        })
        .map_err(|e| e.to_string())?;
    handle
        .join()
        .map_err(|_| "External URL helper thread panicked".to_string())?
}

#[cfg(not(windows))]
fn open_url_os(_url: &str) -> Result<(), String> {
    Err("Opening external URLs is only supported on Windows".to_string())
}

fn resolve_discover_targets(
    host: Option<String>,
    port: Option<Value>,
) -> Result<Option<Vec<(String, u16)>>, String> {
    let parsed_port = match port {
        None | Some(Value::Null) => discovery::DEFAULT_PORT,
        Some(ref v) => runtime::parse_port_value(v, "headset_port")?,
    };
    Ok(host
        .map(|h| h.trim().to_string())
        .filter(|h| !h.is_empty())
        .map(|h| vec![(h, parsed_port)]))
}

// ---------------------------------------------------------------------------
// Tauri IPC commands matching pc-studio/web/app.mjs createTauriFetcher
// ---------------------------------------------------------------------------

#[tauri::command]
fn get_status(state: State<'_, Arc<AppState>>) -> Value {
    state.combined_status()
}

#[tauri::command]
fn get_expressions(state: State<'_, Arc<AppState>>) -> Value {
    state.runtime.expression_snapshot()
}

#[tauri::command]
fn get_config(state: State<'_, Arc<AppState>>) -> RuntimeConfig {
    state.runtime.config()
}

#[tauri::command]
fn get_presets(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    state.capture.presets()
}

#[tauri::command]
async fn discover_sources(
    host: Option<String>,
    port: Option<Value>,
) -> Result<DiscoveryResult, String> {
    let explicit_targets = resolve_discover_targets(host, port)?;
    tauri::async_runtime::spawn_blocking(move || {
        discovery::discover_sources(DISCOVERY_TIMEOUT, explicit_targets)
    })
    .await
    .map_err(|e| e.to_string())
}

#[tauri::command]
async fn check_updates(
    force: Option<bool>,
    state: State<'_, Arc<AppState>>,
) -> Result<UpdateInfo, String> {
    let force = force.unwrap_or(false);
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || state.updater.check(force))
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn get_preview_frame(state: State<'_, Arc<AppState>>) -> tauri::ipc::Response {
    tauri::ipc::Response::new(state.runtime.preview_jpeg().unwrap_or_default())
}

#[tauri::command]
fn get_review_samples(
    offset: Option<usize>,
    limit: Option<usize>,
    all: Option<bool>,
    state: State<'_, Arc<AppState>>,
) -> Result<Value, String> {
    state.handle_get_review_samples(offset, limit, all)
}

#[tauri::command]
fn get_review_image(
    session: String,
    sample: String,
    state: State<'_, Arc<AppState>>,
) -> Result<tauri::ipc::Response, String> {
    let bytes = state.handle_get_review_image(&session, &sample)?;
    Ok(tauri::ipc::Response::new(bytes))
}

#[cfg(not(test))]
fn pick_model_file_dialog(initial_str: &str) -> Option<PathBuf> {
    let mut dialog = rfd::FileDialog::new()
        .set_title("Choose the whole-mouth model (.onnx)")
        .add_filter("ONNX model (*.onnx)", &["onnx"]);
    let trimmed = initial_str.trim();
    if !trimmed.is_empty() {
        let p = Path::new(trimmed);
        if let Some(parent) = p.parent() {
            if parent.is_dir() {
                dialog = dialog.set_directory(parent);
            }
        }
    }
    dialog.pick_file()
}

#[cfg(test)]
fn pick_model_file_dialog(_initial_str: &str) -> Option<PathBuf> {
    // Avoid pulling rfd's ComCtl32 v6 TaskDialogIndirect import into unmanifested cargo test harnesses.
    None
}

#[tauri::command]
async fn select_model(
    initial: Option<String>,
    state: State<'_, Arc<AppState>>,
) -> Result<Value, String> {
    state.check_not_exiting()?;
    state.check_not_sealed_c("This workbench is only for sealed C capture")?;
    state.check_capture_not_active()?;
    let initial_str = initial.unwrap_or_default();
    // Run rfd::FileDialog on a fresh dedicated OS thread so Windows COM STA initialization
    // cannot collide with MTA threads in Tokio's spawn_blocking pool (e.g. ORT/DirectML).
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::Builder::new()
        .name("select-model-dialog".to_string())
        .spawn(move || {
            let picked = pick_model_file_dialog(&initial_str);
            let _ = tx.send(validate_selected_model_path(picked));
        })
        .map_err(|e| format!("Failed to launch file picker thread: {e}"))?;
    tauri::async_runtime::spawn_blocking(move || {
        rx.recv()
            .map_err(|_| "File picker thread exited unexpectedly".to_string())?
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn set_config(
    changes: Value,
    state: State<'_, Arc<AppState>>,
) -> Result<RuntimeConfig, String> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || state.handle_set_config(changes))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn start_runtime(
    payload: Value,
    state: State<'_, Arc<AppState>>,
) -> Result<Value, String> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || state.handle_start_runtime(payload))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn prepare_model(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || state.handle_prepare_model())
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn personalization_action(
    payload: Value,
    state: State<'_, Arc<AppState>>,
) -> Result<Value, String> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || state.handle_personalization_action(payload))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn recording_action(
    payload: Value,
    state: State<'_, Arc<AppState>>,
) -> Result<Value, String> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || state.handle_recording_action(payload))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
fn recording_display(payload: Value, state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    state.handle_recording_display(payload)
}

#[tauri::command]
async fn recording_review(
    payload: Value,
    state: State<'_, Arc<AppState>>,
) -> Result<Value, String> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || state.handle_recording_review(payload))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn training_action(
    payload: Value,
    state: State<'_, Arc<AppState>>,
) -> Result<Value, String> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || state.handle_training_action(payload))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn cymple_relay_action(
    payload: Value,
    state: State<'_, Arc<AppState>>,
) -> Result<Value, String> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || state.handle_cymple_relay_action(payload))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn stop_runtime(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || state.handle_stop_runtime())
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
fn set_tray_language(language: String, state: State<'_, Arc<AppState>>) -> Result<(), String> {
    let labels = TrayLabels::for_language(&language);
    #[cfg(not(test))]
    {
        let guard = state.tray_items.lock().map_err(|e| e.to_string())?;
        if let Some(items) = guard.as_ref() {
            items.show.set_text(labels.show).map_err(|e| e.to_string())?;
            items
                .stop_relay
                .set_text(labels.stop_relay)
                .map_err(|e| e.to_string())?;
            items.quit.set_text(labels.quit).map_err(|e| e.to_string())?;
        }
    }
    #[cfg(test)]
    {
        let _ = (labels, state);
    }
    Ok(())
}

#[tauri::command]
async fn open_external_url(url: String) -> Result<(), String> {
    let safe_url = validate_external_url(&url)?;
    tauri::async_runtime::spawn_blocking(move || open_url_os(&safe_url))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
fn quit_app(app: AppHandle) -> Value {
    std::thread::spawn(move || {
        // Let the IPC response flush back to the WebView before tearing down the event loop.
        std::thread::sleep(Duration::from_millis(25));
        initiate_shutdown(&app);
    });
    json!({ "stopping": true })
}

fn setup_tray(app: &AppHandle, state: &Arc<AppState>) -> tauri::Result<()> {
    let labels = TrayLabels::for_language(default_tray_language());
    let show = MenuItem::with_id(app, "show", labels.show, true, None::<&str>)?;
    let stop_relay = MenuItem::with_id(app, "stop_relay", labels.stop_relay, true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let quit = MenuItem::with_id(app, "quit", labels.quit, true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &stop_relay, &separator, &quit])?;

    #[cfg(not(test))]
    if let Ok(mut guard) = state.tray_items.lock() {
        *guard = Some(TrayMenuItems {
            show,
            stop_relay,
            quit,
        });
    }
    #[cfg(test)]
    let _ = state;

    let icon = Image::from_bytes(include_bytes!("../icons/icon.png"))?;
    TrayIconBuilder::with_id("main-tray")
        .icon(icon)
        .tooltip("PicoET PC Studio")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => show_main_window(app),
            "stop_relay" => {
                let state = app.state::<Arc<AppState>>().inner().clone();
                if !state.relay_stopping.swap(true, Ordering::SeqCst) {
                    let _ = std::thread::Builder::new()
                        .name("tray-stop-relay".to_string())
                        .spawn(move || {
                            let _reset = AtomicFlagReset(&state.relay_stopping);
                            let _ = state.relay.stop();
                        });
                }
            }
            "quit" => {
                let app_handle = app.clone();
                std::thread::spawn(move || {
                    initiate_shutdown(&app_handle);
                });
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| match event {
            TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            }
            | TrayIconEvent::DoubleClick {
                button: MouseButton::Left,
                ..
            } => {
                show_main_window(tray.app_handle());
            }
            _ => {}
        })
        .build(app)?;

    Ok(())
}

pub fn run() {
    run_with_options(StudioLaunchOptions {
        device: "auto".to_string(),
        ..Default::default()
    });
}

pub fn run_with_options(opts: StudioLaunchOptions) {
    let nav_guard = tauri::plugin::Builder::<tauri::Wry, ()>::new("navigation-guard")
        .on_navigation(|_webview, url| is_allowed_webview_url(url))
        .build();

    let app = tauri::Builder::default()
        .plugin(nav_guard)
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main_window(app);
        }))
        .setup(move |app| {
            // Defer AppState initialization (e.g. --sealed-c directory creation and --model
            // config persistence) until after single-instance verification succeeds.
            let state = Arc::new(
                AppState::from_options(opts).map_err(std::io::Error::other)?,
            );
            app.manage(state.clone());
            let handle = app.handle();
            setup_instance_sync(handle, &state);
            setup_tray(handle, &state)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if let Some(state) = window.try_state::<Arc<AppState>>() {
                    if !state.allow_exit.load(Ordering::SeqCst) {
                        api.prevent_close();
                        let _ = window.hide();
                    }
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_status,
            get_expressions,
            get_config,
            get_presets,
            discover_sources,
            check_updates,
            get_preview_frame,
            get_review_samples,
            get_review_image,
            select_model,
            set_config,
            start_runtime,
            prepare_model,
            personalization_action,
            recording_action,
            recording_display,
            recording_review,
            training_action,
            cymple_relay_action,
            stop_runtime,
            set_tray_language,
            open_external_url,
            quit_app,
        ])
        .build(tauri::generate_context!())
        .expect("error while building PicoET PC Studio");

    app.run(|app_handle, event| match event {
        tauri::RunEvent::ExitRequested { api, code, .. } => {
            if let Some(state) = app_handle.try_state::<Arc<AppState>>() {
                if code.is_none() && !state.allow_exit.load(Ordering::SeqCst) {
                    api.prevent_exit();
                }
            }
        }
        tauri::RunEvent::Exit => {
            if let Some(state) = app_handle.try_state::<Arc<AppState>>() {
                if !state.exiting.swap(true, Ordering::SeqCst) {
                    state.shutdown_all();
                    state.allow_exit.store(true, Ordering::SeqCst);
                }
            }
        }
        _ => {}
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cli_arg_parsing_handles_flags_and_sealed_c_constraints() {
        assert_eq!(
            parse_cli_args(&["--version".to_string()]).unwrap(),
            CliAction::Version
        );
        assert_eq!(
            parse_cli_args(&["-V".to_string()]).unwrap(),
            CliAction::Version
        );
        assert_eq!(
            parse_cli_args(&["--stop".to_string()]).unwrap(),
            CliAction::Stop
        );

        let parsed = parse_cli_args(&[
            "--config".into(),
            "C:/tmp/cfg.json".into(),
            "--model".into(),
            "C:/tmp/m.onnx".into(),
            "--device".into(),
            "cuda".into(),
            "--record-input".into(),
        ])
        .unwrap();
        match parsed {
            CliAction::Run(opts) => {
                assert_eq!(opts.config, Some(PathBuf::from("C:/tmp/cfg.json")));
                assert_eq!(opts.model.as_deref(), Some("C:/tmp/m.onnx"));
                assert_eq!(opts.device, "cuda");
                assert!(opts.record_input);
            }
            _ => panic!("expected Run"),
        }

        // --sealed-c-profile requires --sealed-c
        assert!(parse_cli_args(&["--sealed-c-profile".into(), "jaw".into()]).is_err());
        // --sealed-c cannot combine with --config
        assert!(parse_cli_args(&[
            "--sealed-c".into(),
            "C:/tmp/sealed".into(),
            "--config".into(),
            "C:/tmp/c.json".into(),
        ])
        .is_err());
        // Valid --sealed-c + --sealed-c-profile
        let sealed = parse_cli_args(&[
            "--sealed-c".into(),
            "C:/tmp/sealed".into(),
            "--sealed-c-profile".into(),
            "jaw".into(),
        ])
        .unwrap();
        assert!(matches!(sealed, CliAction::Run(o) if o.sealed_c_profile.as_deref() == Some("jaw")));
    }

    #[test]
    fn select_model_validation_checks_onnx_and_adjacent_manifest() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(
            validate_selected_model_path(None).unwrap(),
            json!({ "cancelled": true })
        );

        let txt = dir.path().join("wrong.txt");
        fs::write(&txt, "hi").unwrap();
        assert!(validate_selected_model_path(Some(txt))
            .unwrap_err()
            .contains("ONNX model"));

        let onnx = dir.path().join("face50.onnx");
        fs::write(&onnx, b"fake").unwrap();
        assert!(validate_selected_model_path(Some(onnx.clone()))
            .unwrap_err()
            .contains("JSON model manifest"));

        fs::write(dir.path().join("face50.json"), "{}").unwrap();
        let ok = validate_selected_model_path(Some(onnx)).unwrap();
        assert_eq!(ok["cancelled"], false);
        assert_eq!(ok["name"], "face50.onnx");
    }

    #[test]
    fn app_state_sealed_c_guards_block_forbidden_routes() {
        let dir = tempfile::tempdir().unwrap();
        let sealed_dir = dir.path().join("attempt1");
        let state = AppState::from_options(StudioLaunchOptions {
            sealed_c: Some(sealed_dir.clone()),
            sealed_c_profile: Some("jaw".to_string()),
            device: "auto".to_string(),
            ..Default::default()
        })
        .unwrap();

        assert!(sealed_dir.is_dir());
        assert!(state.is_sealed_c());
        assert!(state.handle_prepare_model().is_err());
        assert!(state.handle_get_review_samples(None, None, None).is_err());
        assert!(state.handle_get_review_image("s", "f").is_err());
        assert!(state
            .handle_training_action(json!({"action": "begin", "operation": "split"}))
            .is_err());
        assert!(state
            .handle_cymple_relay_action(json!({"action": "stop"}))
            .is_err());
        assert!(state
            .handle_start_runtime(json!({"mode": "live"}))
            .is_err());

        // set_config in sealed C forces model_path="" / output_enabled=false / model_kind="face50"
        let cfg = state
            .handle_set_config(json!({
                "headset_host": "127.0.0.1",
                "model_path": "evil.onnx",
                "output_enabled": true
            }))
            .unwrap();
        assert_eq!(cfg.headset_host, "127.0.0.1");
        assert_eq!(cfg.model_path, "");
        assert!(!cfg.output_enabled);
        assert_eq!(cfg.model_kind, "face50");

        let st = state.combined_status();
        assert_eq!(st["managed_capture"], true);
        assert_eq!(st["capture_control"]["sealed_c"], true);
        assert_eq!(st["capture_control"]["preset_id"], "sealed-c-jaw");
        assert!(st["cymple_relay"].is_object());
    }

    #[test]
    fn external_url_validation_only_allows_clean_https() {
        assert_eq!(
            validate_external_url("https://github.com/WolalaQAQ/PicoET-Enhance/releases").unwrap(),
            "https://github.com/WolalaQAQ/PicoET-Enhance/releases"
        );
        assert!(validate_external_url("http://example.com").is_err());
        assert!(validate_external_url("file:///C:/Windows/win.ini").is_err());
        assert!(validate_external_url("javascript:alert(1)").is_err());
        assert!(validate_external_url("https://").is_err());
        assert!(validate_external_url("https://example.com/a b").is_err());
        assert!(validate_external_url("https://example.com/a\nb").is_err());
        assert!(validate_external_url("https:/中").is_err());
        assert!(validate_external_url("中文中文中文").is_err());
        assert!(validate_external_url("https:///C:/Windows/win.ini").is_err());
        assert!(validate_external_url("https:///foo/bar").is_err());
        assert!(validate_external_url("https://\\\\host\\share").is_err());
        assert!(validate_external_url("https://example.com/a\\b").is_err());
    }

    #[test]
    fn webview_navigation_guard_restricts_to_tauri_origin() {
        assert!(is_allowed_webview_url(
            &tauri::Url::parse("http://tauri.localhost/").unwrap()
        ));
        assert!(is_allowed_webview_url(
            &tauri::Url::parse("http://tauri.localhost/#/preferences").unwrap()
        ));
        assert!(is_allowed_webview_url(
            &tauri::Url::parse("https://tauri.localhost/index.html").unwrap()
        ));
        assert!(is_allowed_webview_url(
            &tauri::Url::parse("tauri://localhost/").unwrap()
        ));
        assert!(!is_allowed_webview_url(
            &tauri::Url::parse("https://github.com/WolalaQAQ/PicoET-Enhance").unwrap()
        ));
        assert!(!is_allowed_webview_url(
            &tauri::Url::parse("http://tauri.localhost:8080/").unwrap()
        ));
        assert!(!is_allowed_webview_url(
            &tauri::Url::parse("http://evil.tauri.localhost/").unwrap()
        ));
        assert!(!is_allowed_webview_url(
            &tauri::Url::parse("file:///C:/Windows/win.ini").unwrap()
        ));
    }

    #[test]
    fn tray_labels_switch_between_en_and_zh() {
        let zh = TrayLabels::for_language("zh-CN");
        assert_eq!(zh.show, "显示主窗口");
        assert_eq!(zh.stop_relay, "停止眼追转发");
        let en = TrayLabels::for_language("en");
        assert_eq!(en.show, "Show Window");
        assert_eq!(en.stop_relay, "Stop Eye Relay");
    }

    #[test]
    fn win32_ui_language_maps_chinese_sublanguages_to_zh_cn() {
        assert_eq!(language_from_win32_lang_id(0x0804), "zh-CN");
        assert_eq!(language_from_win32_lang_id(0x0404), "zh-CN");
        assert_eq!(language_from_win32_lang_id(0x0c04), "zh-CN");
        assert_eq!(language_from_win32_lang_id(0x0409), "en");
        assert_eq!(language_from_win32_lang_id(0x0411), "en");
    }

    #[test]
    fn legacy_python_stop_probes_status_before_posting_shutdown() {
        use std::net::TcpListener;
        use std::sync::atomic::AtomicUsize;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let shutdown_hits = Arc::new(AtomicUsize::new(0));
        let hits_clone = shutdown_hits.clone();
        let server = std::thread::spawn(move || {
            let _ = listener.set_nonblocking(false);
            if let Ok((mut stream, _)) = listener.accept() {
                let mut req = [0u8; 512];
                let n = stream.read(&mut req).unwrap_or(0);
                let text = String::from_utf8_lossy(&req[..n]);
                if text.starts_with("POST /api/shutdown") {
                    hits_clone.fetch_add(1, Ordering::SeqCst);
                }
                let body = r#"{"ok":true}"#;
                let resp = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                let _ = stream.write_all(resp.as_bytes());
            }
        });
        assert!(stop_legacy_python_instance(port));
        server.join().unwrap();
        assert_eq!(shutdown_hits.load(Ordering::SeqCst), 0);

        let listener2 = TcpListener::bind("127.0.0.1:0").unwrap();
        let port2 = listener2.local_addr().unwrap().port();
        let shutdown_hits2 = Arc::new(AtomicUsize::new(0));
        let hits2_clone = shutdown_hits2.clone();
        let server2 = std::thread::spawn(move || {
            for _ in 0..2 {
                if let Ok((mut stream, _)) = listener2.accept() {
                    let mut req = [0u8; 512];
                    let n = stream.read(&mut req).unwrap_or(0);
                    let text = String::from_utf8_lossy(&req[..n]);
                    if text.starts_with("GET /api/status") {
                        let body = r#"{"version":"2.6.2","cymple_relay":{"running":false}}"#;
                        let resp = format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                            body.len(),
                            body
                        );
                        let _ = stream.write_all(resp.as_bytes());
                    } else if text.starts_with("POST /api/shutdown") {
                        hits2_clone.fetch_add(1, Ordering::SeqCst);
                        let body = r#"{"stopping":true}"#;
                        let resp = format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                            body.len(),
                            body
                        );
                        let _ = stream.write_all(resp.as_bytes());
                        break;
                    }
                }
            }
            drop(listener2);
        });
        assert!(stop_legacy_python_instance(port2));
        server2.join().unwrap();
        assert_eq!(shutdown_hits2.load(Ordering::SeqCst), 1);
    }

    #[cfg(windows)]
    #[test]
    fn stop_event_signals_waits_and_reports_timeout() {
        use std::sync::mpsc;
        use windows_sys::Win32::Foundation::{CloseHandle, WAIT_OBJECT_0};
        use windows_sys::Win32::System::Threading::{
            CreateEventW, CreateMutexW, ReleaseMutex, WaitForSingleObject, INFINITE,
        };

        let pid = std::process::id();

        assert_eq!(
            signal_and_wait_named_instance(
                &format!("Local\\PicoET-PC-Studio-Test-Missing-Stop-{pid}"),
                &format!("Local\\PicoET-PC-Studio-Test-Missing-Lock-{pid}"),
                200
            ),
            NamedStopStatus::NotRunning
        );

        let mutex_name = format!("Local\\PicoET-PC-Studio-Test-Lock-{pid}");
        let event_name = format!("Local\\PicoET-PC-Studio-Test-Stop-{pid}");
        let (tx, rx) = mpsc::channel();

        let m_name = mutex_name.clone();
        let e_name = event_name.clone();
        let worker = std::thread::spawn(move || {
            let mutex_w = to_wide_null(&m_name);
            let event_w = to_wide_null(&e_name);
            let mutex = unsafe { CreateMutexW(std::ptr::null(), 1, mutex_w.as_ptr()) };
            let event = unsafe { CreateEventW(std::ptr::null(), 1, 0, event_w.as_ptr()) };
            assert!(!mutex.is_null());
            assert!(!event.is_null());
            tx.send(()).unwrap();
            let rc = unsafe { WaitForSingleObject(event, INFINITE) };
            assert_eq!(rc, WAIT_OBJECT_0);
            unsafe {
                let _ = ReleaseMutex(mutex);
                let _ = CloseHandle(mutex);
                let _ = CloseHandle(event);
            }
        });

        rx.recv().unwrap();
        assert_eq!(
            signal_and_wait_named_instance(&event_name, &mutex_name, 3000),
            NamedStopStatus::Stopped
        );
        worker.join().unwrap();

        let stuck_mutex = format!("Local\\PicoET-PC-Studio-Test-Stuck-Lock-{pid}");
        let stuck_event = format!("Local\\PicoET-PC-Studio-Test-Stuck-Stop-{pid}");
        let (tx2, rx2) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let sm_name = stuck_mutex.clone();
        let se_name = stuck_event.clone();
        let stuck_worker = std::thread::spawn(move || {
            let mutex_w = to_wide_null(&sm_name);
            let event_w = to_wide_null(&se_name);
            let mutex = unsafe { CreateMutexW(std::ptr::null(), 1, mutex_w.as_ptr()) };
            let event = unsafe { CreateEventW(std::ptr::null(), 1, 0, event_w.as_ptr()) };
            assert!(!mutex.is_null());
            assert!(!event.is_null());
            tx2.send(()).unwrap();
            let _ = release_rx.recv();
            unsafe {
                let _ = ReleaseMutex(mutex);
                let _ = CloseHandle(mutex);
                let _ = CloseHandle(event);
            }
        });

        rx2.recv().unwrap();
        assert_eq!(
            signal_and_wait_named_instance(&stuck_event, &stuck_mutex, 120),
            NamedStopStatus::TimedOut
        );
        release_tx.send(()).unwrap();
        stuck_worker.join().unwrap();
    }

    #[test]
    fn legacy_chunked_decoder_rejects_overflow_without_panic() {
        use std::net::TcpListener;

        assert_eq!(decode_legacy_chunked_body(b"ffffffffffffffff\r\n"), None);
        assert_eq!(decode_legacy_chunked_body(b"+4\r\ntest\r\n0\r\n\r\n"), None);
        assert_eq!(
            decode_legacy_chunked_body(b"4\r\ntest\r\n0\r\n\r\n"),
            Some(b"test".to_vec())
        );

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let port = addr.port();
        let srv = std::thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let mut req = [0u8; 512];
                let _ = stream.read(&mut req);
                let _ = stream.write_all(
                    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\nffffffffffffffff\r\n",
                );
            }
        });
        assert!(!probe_legacy_picoet_status(addr, port));
        srv.join().unwrap();
    }

    #[test]
    fn discover_sources_validates_port_range() {
        assert_eq!(
            resolve_discover_targets(Some(" 127.0.0.1 ".into()), None).unwrap(),
            Some(vec![("127.0.0.1".to_string(), discovery::DEFAULT_PORT)])
        );
        assert_eq!(
            resolve_discover_targets(Some("127.0.0.1".into()), Some(Value::Null)).unwrap(),
            Some(vec![("127.0.0.1".to_string(), discovery::DEFAULT_PORT)])
        );
        assert_eq!(
            resolve_discover_targets(Some("127.0.0.1".into()), Some(json!(45690))).unwrap(),
            Some(vec![("127.0.0.1".to_string(), 45690)])
        );
        assert_eq!(resolve_discover_targets(None, None).unwrap(), None);

        for bad in [json!(0), json!(-1), json!(70000), json!(true), json!("abc")] {
            let err = resolve_discover_targets(Some("127.0.0.1".into()), Some(bad)).unwrap_err();
            assert_eq!(err, "headset_port must be an integer in 1..65535");
        }
    }
}
