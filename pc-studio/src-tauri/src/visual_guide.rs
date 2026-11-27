//! File bridge for the worn runner and a camera-free, display-only browser.

use serde_json::{json, Map, Value};
use std::fs;
use std::io;
use std::path::{Component, Path, PathBuf};
use std::sync::{Arc, OnceLock};
use std::thread;
use std::time::{Duration, Instant};
use uuid::Uuid;

pub type ClockFn = Arc<dyn Fn() -> f64 + Send + Sync>;

static GUIDE_EPOCH: OnceLock<Instant> = OnceLock::new();

pub fn default_clock() -> ClockFn {
    Arc::new(|| {
        let epoch = GUIDE_EPOCH.get_or_init(Instant::now);
        Instant::now().duration_since(*epoch).as_secs_f64()
    })
}

/// RFC 4122 `uuid.NAMESPACE_URL` bytes (`6ba7b811-9dad-11d1-80b4-00c04fd430c8`).
const NAMESPACE_URL: [u8; 16] = [
    0x6b, 0xa7, 0xb8, 0x11, 0x9d, 0xad, 0x11, 0xd1, 0x80, 0xb4, 0x00, 0xc0, 0x4f, 0xd4, 0x30, 0xc8,
];

fn sha1_digest(data: &[u8]) -> [u8; 20] {
    let mut h: [u32; 5] = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0];
    let bit_len = (data.len() as u64).wrapping_mul(8);
    let mut msg = data.to_vec();
    msg.push(0x80);
    while (msg.len() % 64) != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());

    let mut w = [0u32; 80];
    for chunk in msg.chunks_exact(64) {
        for i in 0..16 {
            w[i] = u32::from_be_bytes([
                chunk[i * 4],
                chunk[i * 4 + 1],
                chunk[i * 4 + 2],
                chunk[i * 4 + 3],
            ]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let (mut a, mut b, mut c, mut d, mut e) = (h[0], h[1], h[2], h[3], h[4]);
        for (i, &wi) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | ((!b) & d), 0x5A827999u32),
                20..=39 => (b ^ c ^ d, 0x6ED9EBA1u32),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1BBCDCu32),
                _ => (b ^ c ^ d, 0xCA62C1D6u32),
            };
            let temp = a
                .rotate_left(5)
                .wrapping_add(f)
                .wrapping_add(e)
                .wrapping_add(k)
                .wrapping_add(wi);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = temp;
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
    }

    let mut out = [0u8; 20];
    for (i, word) in h.iter().enumerate() {
        out[i * 4..i * 4 + 4].copy_from_slice(&word.to_be_bytes());
    }
    out
}

fn uuid5_url_hex(name: &str) -> String {
    let mut input = Vec::with_capacity(NAMESPACE_URL.len() + name.len());
    input.extend_from_slice(&NAMESPACE_URL);
    input.extend_from_slice(name.as_bytes());
    let mut hash = sha1_digest(&input);
    hash[6] = (hash[6] & 0x0f) | 0x50; // version 5
    hash[8] = (hash[8] & 0x3f) | 0x80; // RFC 4122 variant
    let mut hex = String::with_capacity(32);
    for b in &hash[..16] {
        use std::fmt::Write as _;
        let _ = write!(hex, "{b:02x}");
    }
    hex
}

fn strip_verbatim_prefix(s: &str) -> String {
    if let Some(rest) = s
        .strip_prefix(r"\\?\UNC\")
        .or_else(|| s.strip_prefix(r"\\?\unc\"))
    {
        format!(r"\\{rest}")
    } else if let Some(rest) = s.strip_prefix(r"\\?\") {
        rest.to_string()
    } else {
        s.to_string()
    }
}

/// Resolve `path` even if the leaf does not yet exist, stripping Windows verbatim prefix
/// so the mutex name matches Python's `str(Path(path).resolve()).lower()`.
fn canonical_mutex_key(path: &Path) -> String {
    let abs = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    };
    let resolved = if let Ok(c) = fs::canonicalize(&abs) {
        c
    } else {
        let mut comps = abs.components();
        let mut tail = Vec::new();
        let base = loop {
            let candidate = comps.as_path();
            if !candidate.as_os_str().is_empty() {
                if let Ok(c) = fs::canonicalize(candidate) {
                    break c;
                }
            }
            match comps.clone().next_back() {
                Some(Component::Normal(_) | Component::CurDir | Component::ParentDir) => {
                    if let Some(comp) = comps.next_back() {
                        tail.push(comp);
                    }
                }
                Some(_) => break comps.as_path().to_path_buf(),
                None => break PathBuf::new(),
            }
        };
        let mut rebuilt = base;
        for comp in tail.into_iter().rev() {
            match comp {
                Component::CurDir => {}
                Component::ParentDir => {
                    rebuilt.pop();
                }
                Component::Normal(part) if part == "." => {}
                Component::Normal(part) if part == ".." => {
                    rebuilt.pop();
                }
                Component::Normal(part) => {
                    rebuilt.push(part);
                }
                _ => {}
            }
        }
        rebuilt
    };
    strip_verbatim_prefix(&resolved.to_string_lossy()).to_lowercase()
}

pub fn mutex_name_for_path(path: &Path) -> String {
    format!(
        "Local\\PicoMouthGuide-{}",
        uuid5_url_hex(&canonical_mutex_key(path))
    )
}

/// RAII guard for the Windows named mutex serializing readers and atomic replacers.
pub struct FileTransaction {
    #[cfg(windows)]
    handle: windows_sys::Win32::Foundation::HANDLE,
    #[cfg(windows)]
    acquired: bool,
}

#[cfg(windows)]
impl FileTransaction {
    pub fn lock(path: &Path) -> io::Result<Self> {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Foundation::{CloseHandle, WAIT_ABANDONED, WAIT_OBJECT_0};
        use windows_sys::Win32::System::Threading::{
            CreateMutexW, WaitForSingleObject, INFINITE,
        };

        let name = mutex_name_for_path(path);
        let wide: Vec<u16> = std::ffi::OsStr::new(&name)
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();
        let handle = unsafe { CreateMutexW(std::ptr::null(), 0, wide.as_ptr()) };
        if handle.is_null() {
            return Err(io::Error::last_os_error());
        }
        let rc = unsafe { WaitForSingleObject(handle, INFINITE) };
        // WAIT_ABANDONED is acceptable because the previous owner published atomically.
        if rc != WAIT_OBJECT_0 && rc != WAIT_ABANDONED {
            let err = io::Error::last_os_error();
            unsafe {
                let _ = CloseHandle(handle);
            }
            return Err(err);
        }
        Ok(Self {
            handle,
            acquired: true,
        })
    }
}

#[cfg(windows)]
impl Drop for FileTransaction {
    fn drop(&mut self) {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::ReleaseMutex;
        unsafe {
            if self.acquired {
                let _ = ReleaseMutex(self.handle);
            }
            let _ = CloseHandle(self.handle);
        }
    }
}

#[cfg(not(windows))]
impl FileTransaction {
    pub fn lock(_path: &Path) -> io::Result<Self> {
        Ok(Self {})
    }
}

pub fn read_json(path: &Path) -> Result<Value, String> {
    let _tx = FileTransaction::lock(path).map_err(|e| format!("File lock failed: {e}"))?;
    let text = fs::read_to_string(path)
        .map_err(|e| format!("Failed to read {}: {e}", path.display()))?;
    serde_json::from_str(&text).map_err(|e| format!("Invalid JSON in {}: {e}", path.display()))
}

#[cfg(windows)]
fn os_replace_file(src: &Path, dst: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };
    let src_w: Vec<u16> = src.as_os_str().encode_wide().chain(Some(0)).collect();
    let dst_w: Vec<u16> = dst.as_os_str().encode_wide().chain(Some(0)).collect();
    let ok = unsafe {
        MoveFileExW(
            src_w.as_ptr(),
            dst_w.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if ok == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(not(windows))]
fn os_replace_file(src: &Path, dst: &Path) -> io::Result<()> {
    fs::rename(src, dst)
}

fn atomic_json_with<R, C>(
    path: &Path,
    value: &Value,
    mut replacer: R,
    mut clock_s: C,
) -> Result<(), String>
where
    R: FnMut(&Path, &Path) -> io::Result<()>,
    C: FnMut() -> f64,
{
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)
                .map_err(|e| format!("Failed to create directory {}: {e}", parent.display()))?;
        }
    }
    let encoded =
        serde_json::to_string(value).map_err(|e| format!("Failed to serialize JSON: {e}"))?;
    let _tx = FileTransaction::lock(path).map_err(|e| format!("File lock failed: {e}"))?;
    let file_name = path
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("state.json");
    let temp = path.with_file_name(format!("{file_name}.{}.tmp", Uuid::new_v4().simple()));
    if let Err(err) = fs::write(&temp, encoded.as_bytes()) {
        let _ = fs::remove_file(&temp);
        return Err(format!("Failed to write temp file: {err}"));
    }

    let deadline = clock_s() + 0.05;
    loop {
        match replacer(&temp, path) {
            Ok(()) => return Ok(()),
            Err(err) => {
                let is_win_conflict = cfg!(windows)
                    && (matches!(err.raw_os_error(), Some(5) | Some(32))
                        || err.kind() == io::ErrorKind::PermissionDenied);
                if !is_win_conflict || clock_s() >= deadline {
                    let _ = fs::remove_file(&temp);
                    return Err(format!(
                        "Failed to atomically replace {}: {err}",
                        path.display()
                    ));
                }
                thread::sleep(Duration::from_millis(5));
            }
        }
    }
}

pub fn atomic_json(path: &Path, value: &Value) -> Result<(), String> {
    let clk = default_clock();
    atomic_json_with(path, value, os_replace_file, move || clk())
}

pub struct GuideStore {
    pub directory: PathBuf,
    pub path: PathBuf,
    clock: ClockFn,
}

impl GuideStore {
    pub fn new(directory: impl Into<PathBuf>) -> Self {
        Self::with_clock(directory, default_clock())
    }

    pub fn with_clock(directory: impl Into<PathBuf>, clock: ClockFn) -> Self {
        let directory = directory.into();
        let path = directory.join("state.json");
        Self {
            directory,
            path,
            clock,
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn initialize(&self, case_dir: &Path) -> Result<Value, String> {
        if self.path.exists() {
            return Err("Keep an existing viewer session; do not overwrite it".to_string());
        }
        fs::create_dir_all(&self.directory)
            .map_err(|e| format!("Failed to create viewer directory: {e}"))?;
        let config = read_json(&case_dir.join("round.json"))?;
        let steps = read_json(&case_dir.join("actions.json"))?;
        let case_name = case_dir
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_string();
        let state = json!({
            "case": case_name,
            "role": config.get("role").cloned().unwrap_or(Value::Null),
            "steps": steps,
            "index": 0,
            "phase": "practice",
            "running": false,
            "frame": Uuid::new_v4().simple().to_string(),
            "updated_pc_s": (self.clock)(),
            "training_label": false,
            "camera_started": false,
        });
        atomic_json(&self.path, &state)?;
        Ok(state)
    }

    pub fn status(&self) -> Result<Value, String> {
        let mut state = read_json(&self.path)?;
        let running = state
            .get("running")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let updated = state
            .get("updated_pc_s")
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0);
        if running && (self.clock)() - updated > 3.0 {
            state["phase"] = json!("interrupted");
        }
        Ok(state)
    }

    pub fn publish(&self, changes: Map<String, Value>) -> Result<Value, String> {
        let mut state = read_json(&self.path)?;
        let obj = state
            .as_object_mut()
            .ok_or_else(|| "Viewer state must be a JSON object".to_string())?;
        for (k, v) in changes {
            obj.insert(k, v);
        }
        obj.insert("updated_pc_s".to_string(), json!((self.clock)()));
        atomic_json(&self.path, &state)?;
        Ok(state)
    }

    pub fn receipt(&self, body: &Value) -> Result<Value, String> {
        let viewer = body
            .get("viewer")
            .and_then(|v| v.as_str())
            .filter(|v| matches!(*v, "headset" | "desktop" | "test"))
            .ok_or_else(|| "Unknown viewer".to_string())?;
        let state = self.status()?;
        if body.get("frame") != state.get("frame") || body.get("index") != state.get("index") {
            return Err("Obsolete display receipt".to_string());
        }
        let value = json!({
            "viewer": viewer,
            "frame": state["frame"],
            "index": state["index"],
            "visible": body.get("visible") == Some(&Value::Bool(true)),
            "media_ready": body.get("media_ready") == Some(&Value::Bool(true)),
            "received_pc_s": (self.clock)(),
        });
        atomic_json(&self.directory.join(format!("receipt-{viewer}.json")), &value)?;
        Ok(value)
    }

    pub fn ready(&self, viewer: &str, max_age: f64) -> Result<bool, String> {
        let path = self.directory.join(format!("receipt-{viewer}.json"));
        if !path.exists() {
            return Ok(false);
        }
        let state = self.status()?;
        let receipt = read_json(&path)?;
        let phase_ok = state.get("phase").and_then(|v| v.as_str()) != Some("interrupted");
        let frame_ok = receipt.get("frame") == state.get("frame");
        let index_ok = receipt.get("index") == state.get("index");
        let visible_ok = receipt.get("visible") == Some(&Value::Bool(true));
        let media_ok = receipt.get("media_ready") == Some(&Value::Bool(true));
        let age_ok = receipt
            .get("received_pc_s")
            .and_then(|v| v.as_f64())
            .is_some_and(|t| {
                let dt = (self.clock)() - t;
                // Allow sub-nanosecond tolerance for serde_json f64 parse rounding without float_roundtrip.
                dt >= -1e-9 && dt < max_age
            });
        Ok(phase_ok && frame_ok && index_ok && visible_ok && media_ok && age_ok)
    }
}

#[derive(Debug, Clone)]
pub struct VisualSequenceConfig {
    pub state: PathBuf,
    pub viewer: String,
    pub imitate_seconds: f64,
    pub display_timeout_seconds: f64,
    pub heartbeat_seconds: f64,
}

/// Nonblocking display/mimic stages; speech and original holds remain in StepGuide.
pub struct VisualSequence {
    pub config: VisualSequenceConfig,
    pub store: GuideStore,
    clock: ClockFn,
    pub phase: String,
    pub deadline: f64,
    pub displayed_at: Option<f64>,
    pub events: Vec<Value>,
}

impl VisualSequence {
    pub fn new(config: VisualSequenceConfig) -> Self {
        Self::with_clock(config, default_clock())
    }

    pub fn with_clock(config: VisualSequenceConfig, clock: ClockFn) -> Self {
        let store = GuideStore::with_clock(config.state.clone(), clock.clone());
        Self {
            config,
            store,
            clock,
            phase: "next".to_string(),
            deadline: 0.0,
            displayed_at: None,
            events: Vec::new(),
        }
    }

    pub fn preflight(&self, case_dir: &Path) -> Result<(), String> {
        let state = self.store.status()?;
        let expected_case = case_dir.file_name().and_then(|s| s.to_str());
        if state.get("case").and_then(|v| v.as_str()) != expected_case
            || state.get("running").and_then(|v| v.as_bool()).unwrap_or(false)
            || !self.store.ready(&self.config.viewer, 1.0)?
        {
            return Err(
                "Headset viewer must visibly show the first practice step before capture"
                    .to_string(),
            );
        }
        if state.get("index").and_then(|v| v.as_u64()) != Some(0) {
            return Err("Return to the first practice step".to_string());
        }
        Ok(())
    }

    pub fn tick(&mut self, index: usize) -> Result<bool, String> {
        let now = (self.clock)();
        if self.phase == "next" {
            let mut changes = Map::new();
            changes.insert("index".into(), json!(index));
            changes.insert("phase".into(), json!("display"));
            changes.insert("running".into(), json!(true));
            changes.insert("frame".into(), json!(Uuid::new_v4().simple().to_string()));
            self.store.publish(changes)?;
            self.deadline = now + self.config.display_timeout_seconds;
            self.phase = "display".to_string();
            self.events.push(json!({
                "event": "visual_display_requested",
                "index": index,
                "pc_s": now,
                "training_label": false,
            }));
        } else {
            self.store.publish(Map::new())?;
        }
        if self.phase == "display" {
            if self
                .store
                .ready(&self.config.viewer, self.config.heartbeat_seconds)?
            {
                self.displayed_at = Some(now);
                self.deadline = now + self.config.imitate_seconds;
                self.phase = "imitate".to_string();
                let mut changes = Map::new();
                changes.insert("phase".into(), json!("imitate"));
                self.store.publish(changes)?;
                self.events.push(json!({
                    "event": "visual_display_ready",
                    "index": index,
                    "pc_s": now,
                    "training_label": false,
                }));
            } else if now >= self.deadline {
                return Err(
                    "Current demonstration was not displayed before its deadline".to_string(),
                );
            }
            return Ok(false);
        }
        if !self
            .store
            .ready(&self.config.viewer, self.config.heartbeat_seconds)?
        {
            return Err("Headset viewer lost visibility, media or heartbeat".to_string());
        }
        Ok(self.phase == "imitate" && now >= self.deadline)
    }

    pub fn hold(&mut self, until: f64) -> Result<(), String> {
        self.phase = "hold".to_string();
        let mut changes = Map::new();
        changes.insert("phase".into(), json!("hold"));
        changes.insert("hold_until_pc_s".into(), json!(until));
        self.store.publish(changes)?;
        Ok(())
    }

    pub fn next(&mut self) {
        self.phase = "next".to_string();
    }

    pub fn stop(&mut self, complete: bool) -> Result<(), String> {
        let phase = if complete { "done" } else { "paused" };
        self.phase = phase.to_string();
        let mut changes = Map::new();
        changes.insert("phase".into(), json!(phase));
        changes.insert("running".into(), json!(false));
        self.store.publish(changes)?;
        Ok(())
    }
}

pub fn load_visual(case_dir: &Path) -> Result<Option<Value>, String> {
    let path = case_dir.join("round.json");
    if !path.is_file() {
        return Ok(None);
    }
    let data = read_json(&path)?;
    Ok(data.get("visual_guidance").cloned())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Mutex;

    struct GuideFixture {
        _tmp: tempfile::TempDir,
        case_dir: PathBuf,
        now: Arc<Mutex<f64>>,
        store: GuideStore,
        sequence: VisualSequence,
    }

    impl GuideFixture {
        fn new() -> Self {
            let tmp = tempfile::tempdir().unwrap();
            let case_dir = tmp.path().join("round-A");
            fs::create_dir_all(&case_dir).unwrap();
            let steps = json!([
                {
                    "action": "neutral",
                    "phrase": "neutral",
                    "text": "Relax",
                    "hold_seconds": 3,
                    "visual": { "demo_prompt_ids": ["neutral"], "note": "" }
                },
                {
                    "action": "roll",
                    "phrase": "roll",
                    "text": "Roll",
                    "hold_seconds": 3,
                    "visual": { "demo_prompt_ids": ["TongueRoll"], "note": "" }
                }
            ]);
            fs::write(
                case_dir.join("actions.json"),
                serde_json::to_string(&steps).unwrap(),
            )
            .unwrap();
            fs::write(
                case_dir.join("round.json"),
                serde_json::to_string(&json!({ "role": "A" })).unwrap(),
            )
            .unwrap();

            let now = Arc::new(Mutex::new(100.0f64));
            let c1 = now.clone();
            let clock: ClockFn = Arc::new(move || *c1.lock().unwrap());
            let viewer_dir = tmp.path().join("viewer");
            let store = GuideStore::with_clock(&viewer_dir, clock.clone());
            store.initialize(&case_dir).unwrap();
            let sequence = VisualSequence::with_clock(
                VisualSequenceConfig {
                    state: viewer_dir,
                    viewer: "headset".into(),
                    imitate_seconds: 2.5,
                    display_timeout_seconds: 1.5,
                    heartbeat_seconds: 1.0,
                },
                clock,
            );
            Self {
                _tmp: tmp,
                case_dir,
                now,
                store,
                sequence,
            }
        }

        fn send_receipt(&self, visible: bool, media_ready: bool) -> Value {
            let st = self.store.status().unwrap();
            self.store
                .receipt(&json!({
                    "viewer": "headset",
                    "frame": st["frame"],
                    "index": st["index"],
                    "visible": visible,
                    "media_ready": media_ready,
                }))
                .unwrap()
        }
    }

    #[test]
    fn no_headset_receipt_no_preflight_and_desktop_isolated() {
        let f = GuideFixture::new();
        assert!(f.sequence.preflight(&f.case_dir).is_err());

        // Desktop receipt never unlocks headset.
        let st = f.store.status().unwrap();
        f.store
            .receipt(&json!({
                "viewer": "desktop",
                "frame": st["frame"],
                "index": 0,
                "visible": true,
                "media_ready": true,
            }))
            .unwrap();
        assert!(!f.store.ready("headset", 1.0).unwrap());

        f.send_receipt(true, true);
        f.sequence.preflight(&f.case_dir).unwrap();
    }

    #[test]
    fn stale_hidden_failed_media_and_old_frame_rejected() {
        let f = GuideFixture::new();
        for (vis, media) in [(false, true), (true, false)] {
            f.send_receipt(vis, media);
            assert!(!f.store.ready("headset", 1.0).unwrap());
        }
        f.send_receipt(true, true);
        *f.now.lock().unwrap() += 1.0;
        assert!(!f.store.ready("headset", 1.0).unwrap());

        let st = f.store.status().unwrap();
        assert!(f
            .store
            .receipt(&json!({ "viewer": "headset", "frame": "old", "index": 0 }))
            .is_err());
        assert!(f
            .store
            .receipt(&json!({ "viewer": "headset", "frame": st["frame"], "index": 1 }))
            .is_err());
    }

    #[test]
    fn show_receipt_mimic_then_caller_supplied_hold() {
        let mut f = GuideFixture::new();
        assert!(!f.sequence.tick(0).unwrap());
        assert_eq!(f.store.status().unwrap()["phase"], json!("display"));
        f.send_receipt(true, true);
        assert!(!f.sequence.tick(0).unwrap());
        let displayed = *f.now.lock().unwrap();
        assert_eq!(f.store.status().unwrap()["phase"], json!("imitate"));
        *f.now.lock().unwrap() += 2.49;
        f.send_receipt(true, true);
        assert!(!f.sequence.tick(0).unwrap());
        *f.now.lock().unwrap() += 0.02;
        assert!(f.sequence.tick(0).unwrap());
        assert!(*f.now.lock().unwrap() - displayed >= 2.5);
        let until = *f.now.lock().unwrap() + 3.0;
        f.sequence.hold(until).unwrap();
        let stored_until = f.store.status().unwrap()["hold_until_pc_s"]
            .as_f64()
            .unwrap();
        assert!((stored_until - until).abs() < 1e-12);
        *f.now.lock().unwrap() = until - 0.01;
        f.send_receipt(true, true);
        assert!(!f.sequence.tick(0).unwrap());
        assert_eq!(f.store.status().unwrap()["index"], json!(0));
        f.sequence.next();
        assert!(!f.sequence.tick(1).unwrap());
        assert_eq!(f.store.status().unwrap()["index"], json!(1));
        assert!(!f.store.ready("headset", 1.0).unwrap());
        let ready_events = f
            .sequence
            .events
            .iter()
            .filter(|e| e["event"] == json!("visual_display_ready"))
            .count();
        assert_eq!(ready_events, 1);
    }

    #[test]
    fn missing_display_lost_viewer_runner_loss_complete_and_no_overwrite() {
        let mut f = GuideFixture::new();
        f.sequence.tick(0).unwrap();
        *f.now.lock().unwrap() += 1.51;
        assert!(f.sequence.tick(0).is_err());
        f.sequence.stop(false).unwrap();
        assert!(!f
            .sequence
            .events
            .iter()
            .any(|e| e["event"] == json!("visual_display_ready")));
        assert_eq!(f.store.status().unwrap()["phase"], json!("paused"));

        // Lost viewer during mimic.
        let mut f2 = GuideFixture::new();
        f2.sequence.tick(0).unwrap();
        f2.send_receipt(true, true);
        f2.sequence.tick(0).unwrap();
        *f2.now.lock().unwrap() += 1.01;
        assert!(f2.sequence.tick(0).is_err());
        assert_eq!(f2.store.status().unwrap()["phase"], json!("imitate"));

        // Runner loss becomes interrupted.
        let mut changes = Map::new();
        changes.insert("running".into(), json!(true));
        changes.insert("phase".into(), json!("hold"));
        f2.store.publish(changes).unwrap();
        *f2.now.lock().unwrap() += 3.01;
        assert_eq!(f2.store.status().unwrap()["phase"], json!("interrupted"));

        // Complete sequence.
        let mut f3 = GuideFixture::new();
        for index in 0..2 {
            f3.sequence.tick(index).unwrap();
            f3.send_receipt(true, true);
            f3.sequence.tick(index).unwrap();
            *f3.now.lock().unwrap() += 2.51;
            f3.send_receipt(true, true);
            assert!(f3.sequence.tick(index).unwrap());
            let until = *f3.now.lock().unwrap() + 3.0;
            f3.sequence.hold(until).unwrap();
            assert_eq!(f3.store.status().unwrap()["running"], json!(true));
            f3.sequence.next();
        }
        f3.sequence.stop(true).unwrap();
        assert_eq!(f3.store.status().unwrap()["phase"], json!("done"));
        assert_eq!(f3.store.status().unwrap()["running"], json!(false));
        assert_eq!(
            f3.sequence
                .events
                .iter()
                .filter(|e| e["event"] == json!("visual_display_ready"))
                .count(),
            2
        );

        // Cannot overwrite existing viewer session.
        assert!(f3.store.initialize(&f3.case_dir).is_err());
    }

    #[test]
    fn transient_and_persistent_replace_conflicts() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("state.json");

        // Transient conflict retried and recovered.
        let mut calls = 0usize;
        atomic_json_with(
            &path,
            &json!({ "revision": 1 }),
            |src, dst| {
                calls += 1;
                if calls <= 2 {
                    return Err(io::Error::from_raw_os_error(5));
                }
                os_replace_file(src, dst)
            },
            || 100.0,
        )
        .unwrap();
        assert_eq!(calls, 3);
        assert_eq!(read_json(&path).unwrap(), json!({ "revision": 1 }));

        // Persistent conflict bounded and remains an error.
        let path2 = dir.path().join("state2.json");
        let mut ticks = [100.0, 101.0].into_iter();
        let mut clock_calls = 0usize;
        let err = atomic_json_with(
            &path2,
            &json!({ "revision": 1 }),
            |_src, _dst| Err(io::Error::from_raw_os_error(5)),
            || {
                clock_calls += 1;
                ticks.next().unwrap_or(101.0)
            },
        );
        assert!(err.is_err());
        assert_eq!(clock_calls, 2);
        assert!(!path2.exists());
    }

    #[test]
    fn parallel_readers_and_atomic_writers_keep_complete_json() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("state.json");
        atomic_json(&path, &json!({ "revision": 0 })).unwrap();

        let done = Arc::new(AtomicBool::new(false));
        let errors = Arc::new(Mutex::new(Vec::<String>::new()));
        let mut readers = Vec::new();
        for _ in 0..3 {
            let p = path.clone();
            let d = done.clone();
            let e = errors.clone();
            readers.push(thread::spawn(move || {
                while !d.load(Ordering::Relaxed) {
                    match read_json(&p) {
                        Ok(v) if v.get("revision").and_then(|r| r.as_i64()).is_some() => {}
                        Ok(v) => {
                            e.lock().unwrap().push(format!("unexpected payload: {v}"));
                            break;
                        }
                        Err(err) => {
                            e.lock().unwrap().push(err);
                            break;
                        }
                    }
                }
            }));
        }

        for rev in 0..100 {
            atomic_json(&path, &json!({ "revision": rev })).unwrap();
        }
        done.store(true, Ordering::Relaxed);
        for t in readers {
            t.join().unwrap();
        }
        assert!(errors.lock().unwrap().is_empty());
        assert_eq!(read_json(&path).unwrap()["revision"], json!(99));
    }

    #[test]
    fn atomic_writer_waits_for_open_reader_then_publishes_complete_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("state.json");
        atomic_json(&path, &json!({ "revision": 1 })).unwrap();

        let completed = Arc::new(AtomicBool::new(false));
        let handle = {
            let _reader_tx = FileTransaction::lock(&path).unwrap();
            let p = path.clone();
            let c = completed.clone();
            let t = thread::spawn(move || {
                atomic_json(&p, &json!({ "revision": 2 })).unwrap();
                c.store(true, Ordering::SeqCst);
            });
            let text = fs::read_to_string(&path).unwrap();
            let parsed: Value = serde_json::from_str(&text).unwrap();
            assert_eq!(parsed, json!({ "revision": 1 }));
            t
        };
        handle.join().unwrap();
        assert!(completed.load(Ordering::SeqCst));
        assert_eq!(read_json(&path).unwrap(), json!({ "revision": 2 }));
    }

    #[test]
    fn uuid5_matches_python_vector() {
        // Matches Python: uuid.uuid5(uuid.NAMESPACE_URL, 'https://example.com').hex
        let hex = uuid5_url_hex("https://example.com");
        assert_eq!(hex.len(), 32);
        assert_eq!(hex, "4fd35a7171ef5a55a9d9aa75c889a6d0");
    }

    #[test]
    fn canonical_mutex_key_strips_unc_verbatim_prefix_and_normalizes_tails() {
        assert_eq!(
            strip_verbatim_prefix(r"\\?\UNC\Server\Share\Dir\state.json"),
            r"\\Server\Share\Dir\state.json"
        );
        assert_eq!(
            strip_verbatim_prefix(r"\\?\unc\Server\Share\Dir\state.json"),
            r"\\Server\Share\Dir\state.json"
        );
        assert_eq!(
            strip_verbatim_prefix(r"\\?\C:\Dir\state.json"),
            r"C:\Dir\state.json"
        );
        assert_eq!(
            strip_verbatim_prefix(r"\\Server\Share\Dir\state.json"),
            r"\\Server\Share\Dir\state.json"
        );

        let dir = tempfile::tempdir().unwrap();
        let existing_sub = dir.path().join("existing_sub");
        fs::create_dir_all(&existing_sub).unwrap();

        let direct = dir.path().join("state.json");
        let with_parent = dir.path().join("missing_a").join("..").join("state.json");
        let with_cur_and_nested = dir
            .path()
            .join("missing_a")
            .join(".")
            .join("missing_b")
            .join("..")
            .join("..")
            .join("state.json");
        let crossing_existing = existing_sub
            .join("missing_a")
            .join("..")
            .join("..")
            .join("state.json");

        let expected_key = canonical_mutex_key(&direct);
        let expected_mutex = mutex_name_for_path(&direct);
        assert!(!expected_key.contains("missing_a"));
        assert_eq!(canonical_mutex_key(&with_parent), expected_key);
        assert_eq!(canonical_mutex_key(&with_cur_and_nested), expected_key);
        assert_eq!(canonical_mutex_key(&crossing_existing), expected_key);
        assert_eq!(mutex_name_for_path(&with_parent), expected_mutex);
        assert_eq!(mutex_name_for_path(&with_cur_and_nested), expected_mutex);
        assert_eq!(mutex_name_for_path(&crossing_existing), expected_mutex);

        #[cfg(windows)]
        {
            let unc_verbatim = Path::new(r"\\?\UNC\NoSuchServer\NoSuchShare\Sub\.\Nested\..\..\State.json");
            let unc_plain = Path::new(r"\\NoSuchServer\NoSuchShare\State.json");
            assert_eq!(
                canonical_mutex_key(unc_verbatim),
                r"\\nosuchserver\nosuchshare\state.json"
            );
            assert_eq!(
                canonical_mutex_key(unc_verbatim),
                canonical_mutex_key(unc_plain)
            );
            assert_eq!(
                mutex_name_for_path(unc_verbatim),
                mutex_name_for_path(unc_plain)
            );
        }
    }
}
