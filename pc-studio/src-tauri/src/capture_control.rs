//! User-operated capture lifecycle, with an explicitly supplied headset source.
//!
//! Exact Rust port of `pc-studio/capture_control.py`.

use crate::discovery;
use crate::face_capture::MouthGuide;
use crate::face_dataset::write_json;
use crate::face_presets::{
    capture_preset, capture_presets, latest_capture_continuation, CaptureContinuation,
    SEALED_PROFILES,
};
use crate::runtime::{RecordingActionOptions, Runtime};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CameraEndpoint {
    pub host: String,
    pub port: u16,
}

pub trait CameraSource: Send + Sync {
    fn start(&self) -> Result<CameraEndpoint, String>;
    fn stop(&self) -> Result<(), String>;
}

/// Select an existing camera service; Runtime owns only this client's socket.
pub struct LANCameraSource {
    runtime: Arc<Runtime>,
}

impl LANCameraSource {
    pub fn new(runtime: Arc<Runtime>) -> Self {
        Self { runtime }
    }
}

impl CameraSource for LANCameraSource {
    fn start(&self) -> Result<CameraEndpoint, String> {
        let cfg = self.runtime.config();
        let host = cfg.headset_host.trim().to_string();
        let port = cfg.headset_port;
        let targets = if host.is_empty() {
            None
        } else {
            Some(vec![(host, port)])
        };
        let found = discovery::discover_sources(Duration::from_millis(1200), targets);
        if found.devices.len() != 1 {
            return Err("Select one available headset in connection settings".to_string());
        }
        let dev = &found.devices[0];
        Ok(CameraEndpoint {
            host: dev.host.clone(),
            port: dev.port,
        })
    }

    fn stop(&self) -> Result<(), String> {
        // Runtime.stop closes this subscription. The shared headset service stays available.
        Ok(())
    }
}

struct CaptureControlState {
    state: String,
    error: String,
    cancelled: bool,
    worker_alive: bool,
    stop_flag: Arc<AtomicBool>,
}

struct CaptureControlInner {
    runtime: Arc<Runtime>,
    source: Arc<dyn CameraSource>,
    display_viewer: Option<String>,
    continuation: Option<CaptureContinuation>,
    sealed_preset: Option<String>,
    state: Mutex<CaptureControlState>,
    stop_cond: Condvar,
    thread: Mutex<Option<JoinHandle<()>>>,
}

pub struct CaptureControl {
    inner: Arc<CaptureControlInner>,
}

impl Drop for CaptureControl {
    fn drop(&mut self) {
        self.close();
    }
}

impl CaptureControlInner {
    fn wait_stop(&self, stop_flag: &AtomicBool, dur: Duration) -> bool {
        let guard = self.state.lock().unwrap();
        let (_guard, _) = self
            .stop_cond
            .wait_timeout_while(guard, dur, |_| !stop_flag.load(Ordering::Acquire))
            .unwrap();
        stop_flag.load(Ordering::Acquire)
    }

    fn run_worker(
        self: Arc<Self>,
        options: RecordingActionOptions,
        reuse_input: bool,
        stop_flag: Arc<AtomicBool>,
    ) {
        let mut run_error = String::new();

        let res: Result<(), String> = (|| {
            if !reuse_input {
                let endpoint = self.source.start()?;
                if stop_flag.load(Ordering::Acquire) {
                    return Ok(());
                }
                self.runtime.configure(json!({
                    "headset_host": endpoint.host,
                    "headset_port": endpoint.port,
                    "model_kind": "face50",
                }))?;
                self.runtime.start_with_mode("capture", None, false)?;
            }

            let deadline = Instant::now() + Duration::from_secs(10);
            while !self.runtime.is_connected() {
                if self.wait_stop(&stop_flag, Duration::from_millis(20)) {
                    return Ok(());
                }
                if let Some(err) = self.runtime.last_error() {
                    return Err(err);
                }
                if Instant::now() >= deadline {
                    return Err("Capture input did not connect".to_string());
                }
            }

            self.runtime.recording("begin", options)?;
            {
                self.state.lock().unwrap().state = "recording".to_string();
            }

            while !self.wait_stop(&stop_flag, Duration::from_millis(100)) {
                let record = self.runtime.record_status();
                if let Some(err) = record
                    .get("error")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty())
                {
                    return Err(err.to_string());
                }
                if let Some(err) = self.runtime.last_error() {
                    return Err(err);
                }
                let guide_phase = record
                    .get("guide")
                    .and_then(|g| g.get("phase"))
                    .and_then(|v| v.as_str());
                if self.sealed_preset.is_some()
                    && !self.runtime.is_connected()
                    && guide_phase != Some("finished")
                {
                    return Err("Camera stream ended before the sealed guide finished".to_string());
                }
                let rec_state = record.get("state").and_then(|v| v.as_str());
                if guide_phase == Some("finished")
                    || matches!(rec_state, Some("stopped") | Some("cancelled"))
                {
                    break;
                }
            }
            Ok(())
        })();

        if let Err(err) = res {
            run_error = err;
        }

        {
            self.state.lock().unwrap().state = "stopping".to_string();
        }
        if let Err(err) = self.source.stop() {
            if run_error.is_empty() {
                run_error = err;
            } else {
                run_error.push_str("; ");
                run_error.push_str(&err);
            }
        }
        let cancelled = self.state.lock().unwrap().cancelled;
        if let Err(err) = self.runtime.stop(cancelled) {
            if run_error.is_empty() {
                run_error = err;
            } else {
                run_error.push_str("; ");
                run_error.push_str(&err);
            }
        }

        if self.sealed_preset.is_some() {
            let record = self.runtime.record_status();
            let guide = record.get("guide").cloned().unwrap_or_else(|| json!({}));
            let dir = record
                .get("path")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
                .map(PathBuf::from)
                .unwrap_or_else(|| {
                    self.runtime
                        .config_path()
                        .parent()
                        .unwrap_or_else(|| Path::new("."))
                        .to_path_buf()
                });
            let skipped = guide
                .get("skipped_indices")
                .and_then(|v| v.as_array())
                .cloned()
                .unwrap_or_default();
            let complete = run_error.is_empty()
                && record.get("state").and_then(|v| v.as_str()) == Some("stopped")
                && guide.get("phase").and_then(|v| v.as_str()) == Some("finished")
                && skipped.is_empty();
            let outcome = json!({
                "session_id": record.get("session_id").cloned().unwrap_or(Value::Null),
                "archive_state": record.get("state").cloned().unwrap_or(Value::Null),
                "complete": complete,
                "error": if run_error.is_empty() { Value::Null } else { json!(run_error) },
                "step_index": guide.get("index").cloned().unwrap_or(Value::Null),
                "skipped_indices": skipped,
            });
            if let Err(err) = write_json(&dir.join("capture-outcome.json"), &outcome) {
                let msg = format!("Could not save sealed outcome: {err}");
                if run_error.is_empty() {
                    run_error = msg;
                } else {
                    run_error.push_str("; ");
                    run_error.push_str(&msg);
                }
            }
        }

        let mut st = self.state.lock().unwrap();
        st.error = run_error;
        st.state = if !st.error.is_empty() {
            "error".to_string()
        } else if st.cancelled {
            "cancelled".to_string()
        } else {
            "completed".to_string()
        };
        st.worker_alive = false;
    }
}

impl CaptureControl {
    pub fn new(
        runtime: Arc<Runtime>,
        source: Arc<dyn CameraSource>,
        display_viewer: Option<String>,
        continuation: Option<CaptureContinuation>,
        sealed_preset: Option<String>,
    ) -> Self {
        Self {
            inner: Arc::new(CaptureControlInner {
                runtime,
                source,
                display_viewer,
                continuation,
                sealed_preset,
                state: Mutex::new(CaptureControlState {
                    state: "idle".to_string(),
                    error: String::new(),
                    cancelled: false,
                    worker_alive: false,
                    stop_flag: Arc::new(AtomicBool::new(false)),
                }),
                stop_cond: Condvar::new(),
                thread: Mutex::new(None),
            }),
        }
    }

    pub fn is_sealed_c(&self) -> bool {
        self.inner.sealed_preset.is_some()
    }

    pub fn sealed_preset(&self) -> Option<&str> {
        self.inner.sealed_preset.as_deref()
    }

    pub fn display_viewer(&self) -> Option<&str> {
        self.inner.display_viewer.as_deref()
    }

    pub fn state(&self) -> String {
        self.inner.state.lock().unwrap().state.clone()
    }

    pub fn is_active(&self) -> bool {
        let st = self.inner.state.lock().unwrap();
        matches!(st.state.as_str(), "starting" | "recording" | "stopping")
    }

    pub fn status(&self) -> Value {
        let (state_str, error_str) = {
            let st = self.inner.state.lock().unwrap();
            (st.state.clone(), st.error.clone())
        };
        let mut control = json!({
            "state": state_str,
            "error": error_str,
            "viewer": self.inner.display_viewer,
        });
        if let Some(ref preset) = self.inner.sealed_preset {
            if let Some(obj) = control.as_object_mut() {
                obj.insert("sealed_c".to_string(), json!(true));
                obj.insert("preset_id".to_string(), json!(preset));
            }
        }
        let mut status = self.inner.runtime.status();
        if let Some(obj) = status.as_object_mut() {
            obj.insert("managed_capture".to_string(), json!(true));
            obj.insert("capture_control".to_string(), control);
        }
        status
    }

    fn recordings_root(&self) -> PathBuf {
        self.inner
            .runtime
            .config_path()
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join("recordings")
    }

    fn current_continuation(&self) -> Result<Option<CaptureContinuation>, String> {
        let is_idle = self.inner.state.lock().unwrap().state == "idle";
        if is_idle {
            if let Some(ref cont) = self.inner.continuation {
                return Ok(Some(cont.clone()));
            }
        }
        latest_capture_continuation(&self.recordings_root())
    }

    pub fn presets(&self) -> Result<Value, String> {
        if let Some(ref sealed_id) = self.inner.sealed_preset {
            let preset = capture_preset(sealed_id)?;
            return Ok(json!({
                "presets": [preset],
                "default": sealed_id,
                "sealed_c": true,
            }));
        }
        let mut presets = capture_presets();
        let is_idle = self.inner.state.lock().unwrap().state == "idle";
        let has_cli_continuation = is_idle && self.inner.continuation.is_some();
        let continuation = self.current_continuation()?;
        if let Some(cont) = continuation {
            presets.insert(0, cont.preset);
            return Ok(json!({
                "presets": presets,
                "default": if has_cli_continuation { "continuation" } else { "complete" },
                "continuation": {
                    "person_id": cont.person_id,
                    "label_stage": cont.label_stage,
                    "session_id": cont.session_id,
                },
            }));
        }
        Ok(json!({
            "presets": presets,
            "default": "complete",
        }))
    }

    pub fn recording(
        &self,
        action: &str,
        mut options: RecordingActionOptions,
    ) -> Result<Value, String> {
        if action == "begin" {
            let continuation_session_id = options.continuation_session_id.take();
            if !options.confirmed || options.person_id.trim().is_empty() {
                return Err(
                    "Confirm local recording and enter an anonymous recording name".to_string(),
                );
            }
            if let Some(ref sealed_id) = self.inner.sealed_preset {
                if self.state() != "idle" {
                    return Err(
                        "This sealed C attempt has already started; it cannot be restarted or continued"
                            .to_string(),
                    );
                }
                if continuation_session_id.is_some()
                    || options.preset_id == "continuation"
                    || !matches!(options.step_index, None | Some(0))
                {
                    return Err(
                        "Sealed C must start at the first action of its fixed plan".to_string(),
                    );
                }
                options.role = Some("C".to_string());
                options.evaluation_only = Some(true);
                options.preset_id = sealed_id.clone();
                options.guide = true;
                options.stage = "raw_only".to_string();
                options.receive_results = true;
            } else if SEALED_PROFILES
                .iter()
                .any(|&(_, preset)| preset == options.preset_id)
            {
                return Err("Use the isolated sealed C recording mode for this plan".to_string());
            }

            if options.stage == "instant" && !options.receive_results {
                return Err("Instant labels require receiving mouth results".to_string());
            }

            {
                let st = self.inner.state.lock().unwrap();
                if st.worker_alive {
                    return Err("A capture session is already active".to_string());
                }
            }
            // Reap any previous finished thread cleanly.
            if let Some(prev) = self.inner.thread.lock().unwrap().take() {
                let _ = prev.join();
            }

            let rt_running = self.inner.runtime.is_running();
            let reuse_input = rt_running
                && self.inner.runtime.source_mode() == "live"
                && self.inner.runtime.config().model_kind == "face50";
            if rt_running && !reuse_input {
                return Err("Close the current input before starting managed capture".to_string());
            }

            if options.preset_id == "continuation" {
                let continuation = self.current_continuation()?;
                let Some(cont) = continuation else {
                    return Err("Select the available guided continuation".to_string());
                };
                if !options.guide
                    || continuation_session_id
                        .as_deref()
                        .is_some_and(|sid| sid != cont.session_id)
                {
                    return Err("Select the available guided continuation".to_string());
                }
                if options.person_id != cont.person_id
                    || options.stage != cont.label_stage
                    || options.evaluation_only.unwrap_or(false)
                {
                    return Err(
                        "Use the previous recording name and label stage for continuation"
                            .to_string(),
                    );
                }
                options.role = cont.role.clone();
                options.continuation = Some(cont);
            } else if options.guide {
                let _ = capture_preset(&options.preset_id)?;
            }

            if options.guide {
                let _ = MouthGuide::new(
                    &options.preset_id,
                    options.automatic,
                    options.continuation.as_ref(),
                    options.step_index.unwrap_or(0),
                    options.prepare_seconds,
                )?;
            }

            let stop_flag = Arc::new(AtomicBool::new(false));
            {
                let mut st = self.inner.state.lock().unwrap();
                st.stop_flag = Arc::clone(&stop_flag);
                st.cancelled = false;
                st.error.clear();
                st.state = "starting".to_string();
                st.worker_alive = true;
            }

            let inner_clone = Arc::clone(&self.inner);
            let handle = thread::Builder::new()
                .name("guided-capture".to_string())
                .spawn(move || inner_clone.run_worker(options, reuse_input, stop_flag))
                .map_err(|e| {
                    let mut st = self.inner.state.lock().unwrap();
                    st.worker_alive = false;
                    st.state = "error".to_string();
                    st.error = e.to_string();
                    format!("Failed to start guided capture worker: {e}")
                })?;
            *self.inner.thread.lock().unwrap() = Some(handle);
            return Ok(self.status());
        }

        if action == "stop" || action == "cancel" {
            {
                self.inner.state.lock().unwrap().cancelled = action == "cancel";
            }
            return Ok(self.stop());
        }

        self.inner.runtime.recording(
            action,
            RecordingActionOptions {
                step_index: options.step_index,
                guide_frame: options.guide_frame,
                ..Default::default()
            },
        )
    }

    pub fn stop(&self) -> Value {
        let alive = {
            let mut st = self.inner.state.lock().unwrap();
            if st.worker_alive {
                st.state = "stopping".to_string();
                st.stop_flag.store(true, Ordering::SeqCst);
                self.inner.stop_cond.notify_all();
                true
            } else {
                false
            }
        };
        if !alive {
            let _ = self.inner.runtime.stop(false);
        }
        self.status()
    }

    pub fn close(&self) {
        let _ = self.stop();
        let handle = self.inner.thread.lock().unwrap().take();
        if let Some(h) = handle {
            if h.thread().id() != thread::current().id() {
                let _ = h.join();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::face_presets::SEALED_PRESET;
    use crate::stock::CaptureContext;
    use crate::wire::{self, FrameHeader, JPEG};
    use image::codecs::jpeg::JpegEncoder;
    use image::ExtendedColorType;
    use std::fs;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    struct FakeSource {
        endpoint: CameraEndpoint,
        starts: Arc<Mutex<usize>>,
        stops: Arc<Mutex<usize>>,
    }

    impl CameraSource for FakeSource {
        fn start(&self) -> Result<CameraEndpoint, String> {
            *self.starts.lock().unwrap() += 1;
            Ok(self.endpoint.clone())
        }

        fn stop(&self) -> Result<(), String> {
            *self.stops.lock().unwrap() += 1;
            Ok(())
        }
    }

    struct MiniCameraServer {
        port: u16,
        stop: Arc<AtomicBool>,
        handle: Option<JoinHandle<()>>,
    }

    impl MiniCameraServer {
        fn start() -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let port = listener.local_addr().unwrap().port();
            listener.set_nonblocking(true).unwrap();

            let mut jpeg = Vec::new();
            JpegEncoder::new_with_quality(&mut jpeg, 95)
                .encode(&vec![100u8; 400 * 400], 400, 400, ExtendedColorType::L8)
                .unwrap();

            let stop = Arc::new(AtomicBool::new(false));
            let stop_srv = Arc::clone(&stop);
            let handle = thread::spawn(move || {
                while !stop_srv.load(Ordering::Relaxed) {
                    if let Ok((mut s, _)) = listener.accept() {
                        let _ = s.set_nonblocking(false);
                        let _ = s.set_read_timeout(Some(Duration::from_secs(2)));
                        let payload = jpeg.clone();
                        let stop_conn = Arc::clone(&stop_srv);
                        thread::spawn(move || {
                            let mut cmd = Vec::new();
                            let mut b = [0u8; 1];
                            while !cmd.ends_with(b"\n") {
                                match s.read(&mut b) {
                                    Ok(1) => cmd.push(b[0]),
                                    _ => return,
                                }
                            }
                            if cmd == b"PING\n" {
                                let t = wire::monotonic_ns().max(0) as u64;
                                let mut resp = [0u8; 16];
                                resp[0..8].copy_from_slice(&t.to_le_bytes());
                                resp[8..16].copy_from_slice(&t.to_le_bytes());
                                let _ = s.write_all(&resp);
                            } else if cmd == b"CONTEXT\n" {
                                let ctx = CaptureContext {
                                    capture_session_id: 777,
                                    service_generation: 888,
                                };
                                let _ = s.write_all(&ctx.pack().unwrap());
                            } else if cmd == b"JPEG\n" {
                                let mut seq = 0u64;
                                while !stop_conn.load(Ordering::Relaxed) {
                                    seq += 1;
                                    let now = wire::monotonic_ns().max(0) as u64;
                                    let hdr = FrameHeader {
                                        payload_bytes: payload.len() as u32,
                                        flags: JPEG,
                                        session_id: 777,
                                        source_seq: seq,
                                        sensor_ns: seq * 11_111_111,
                                        vendor_ns: seq * 11_111_111,
                                        copy_mono_ns: now,
                                        encode_begin_ns: now,
                                        encode_end_ns: now,
                                        emit_mono_ns: now,
                                        eye0_sensor_ns: seq * 11_111_111,
                                        eye1_sensor_ns: seq * 11_111_111,
                                        mouth_sensor_ns: seq * 11_111_111,
                                        raw_identity: seq,
                                        width: 400,
                                        height: 400,
                                        raw_crc32: 0,
                                        quality: 95,
                                    };
                                    if s.write_all(&hdr.pack()).is_err()
                                        || s.write_all(&payload).is_err()
                                    {
                                        break;
                                    }
                                    thread::sleep(Duration::from_millis(15));
                                }
                            }
                        });
                    } else {
                        thread::sleep(Duration::from_millis(10));
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

    impl Drop for MiniCameraServer {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::SeqCst);
            if let Some(h) = self.handle.take() {
                let _ = h.join();
            }
        }
    }

    fn wait_for<F: Fn() -> bool>(pred: F, timeout: Duration) {
        let deadline = Instant::now() + timeout;
        while !pred() {
            assert!(Instant::now() < deadline, "Timed out waiting for condition");
            thread::sleep(Duration::from_millis(15));
        }
    }

    #[test]
    fn managed_capture_starts_source_records_and_stops_cleanly() {
        let srv = MiniCameraServer::start();
        let dir = tempfile::tempdir().unwrap();
        let rt = Arc::new(Runtime::new(dir.path().join("config.json")));
        let starts = Arc::new(Mutex::new(0usize));
        let stops = Arc::new(Mutex::new(0usize));
        let src = Arc::new(FakeSource {
            endpoint: CameraEndpoint {
                host: "127.0.0.1".to_string(),
                port: srv.port,
            },
            starts: Arc::clone(&starts),
            stops: Arc::clone(&stops),
        });
        let cc = CaptureControl::new(Arc::clone(&rt), src, None, None, None);

        // Reject missing consent or person_id.
        assert!(cc
            .recording("begin", RecordingActionOptions::default())
            .is_err());
        // Reject sealed preset outside sealed-c mode.
        assert!(cc
            .recording(
                "begin",
                RecordingActionOptions {
                    person_id: "p01".to_string(),
                    confirmed: true,
                    preset_id: SEALED_PRESET.to_string(),
                    ..Default::default()
                }
            )
            .is_err());

        let presets = cc.presets().unwrap();
        assert_eq!(presets["default"], "complete");

        let st = cc
            .recording(
                "begin",
                RecordingActionOptions {
                    person_id: "p01".to_string(),
                    confirmed: true,
                    guide: false,
                    receive_results: false,
                    ..Default::default()
                },
            )
            .unwrap();
        assert_eq!(st["managed_capture"], true);

        wait_for(|| cc.state() == "recording", Duration::from_secs(5));
        wait_for(
            || rt.record_status()["images"].as_u64().unwrap_or(0) >= 2,
            Duration::from_secs(4),
        );

        cc.recording("stop", RecordingActionOptions::default())
            .unwrap();
        wait_for(|| cc.state() == "completed", Duration::from_secs(5));
        assert_eq!(*starts.lock().unwrap(), 1);
        assert_eq!(*stops.lock().unwrap(), 1);
        cc.close();
    }

    #[test]
    fn sealed_c_enforces_single_attempt_and_writes_outcome() {
        let srv = MiniCameraServer::start();
        let dir = tempfile::tempdir().unwrap();
        let rt = Arc::new(Runtime::new(dir.path().join("config.json")));
        let src = Arc::new(FakeSource {
            endpoint: CameraEndpoint {
                host: "127.0.0.1".to_string(),
                port: srv.port,
            },
            starts: Arc::new(Mutex::new(0)),
            stops: Arc::new(Mutex::new(0)),
        });
        let cc = CaptureControl::new(
            Arc::clone(&rt),
            src,
            None,
            None,
            Some(SEALED_PRESET.to_string()),
        );

        assert!(cc.is_sealed_c());
        let presets = cc.presets().unwrap();
        assert_eq!(presets["sealed_c"], true);
        assert_eq!(presets["default"], SEALED_PRESET);

        // Nonzero step_index rejected in sealed C.
        assert!(cc
            .recording(
                "begin",
                RecordingActionOptions {
                    person_id: "p01".to_string(),
                    confirmed: true,
                    step_index: Some(1),
                    ..Default::default()
                }
            )
            .is_err());

        cc.recording(
            "begin",
            RecordingActionOptions {
                person_id: "p01".to_string(),
                confirmed: true,
                ..Default::default()
            },
        )
        .unwrap();
        wait_for(|| cc.state() == "recording", Duration::from_secs(5));
        cc.recording("cancel", RecordingActionOptions::default())
            .unwrap();
        wait_for(|| cc.state() == "cancelled", Duration::from_secs(5));

        let rec_path = PathBuf::from(rt.record_status()["path"].as_str().unwrap());
        let outcome_path = rec_path.join("capture-outcome.json");
        assert!(outcome_path.is_file());
        let outcome: Value = serde_json::from_str(&fs::read_to_string(outcome_path).unwrap()).unwrap();
        assert_eq!(outcome["complete"], false);
        assert_eq!(outcome["archive_state"], "cancelled");

        // Second attempt must be rejected.
        let err = cc
            .recording(
                "begin",
                RecordingActionOptions {
                    person_id: "p01".to_string(),
                    confirmed: true,
                    ..Default::default()
                },
            )
            .unwrap_err();
        assert!(err.contains("already started"));
        cc.close();
    }
}
