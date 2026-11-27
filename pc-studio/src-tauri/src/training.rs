//! Local offline training schema and subprocess runner matching `runtime.py:1031-1115`
//! and `face_train.py`.

use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{mpsc, Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const LOG_TAIL_LIMIT: usize = 30;
const CANCEL_JOIN_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FieldType {
    Text,
    Int,
}

#[derive(Debug, Clone, Copy)]
enum FieldDefault {
    Str(&'static str),
    Int(i64),
}

#[derive(Debug, Clone, Copy)]
struct FieldSpec {
    name: &'static str,
    required: bool,
    kind: FieldType,
    default: FieldDefault,
    choices: Option<&'static [&'static str]>,
}

impl FieldSpec {
    const fn req_text(name: &'static str) -> Self {
        Self {
            name,
            required: true,
            kind: FieldType::Text,
            default: FieldDefault::Str(""),
            choices: None,
        }
    }

    const fn opt_text(name: &'static str, default: &'static str) -> Self {
        Self {
            name,
            required: false,
            kind: FieldType::Text,
            default: FieldDefault::Str(default),
            choices: None,
        }
    }

    const fn opt_choice(
        name: &'static str,
        default: &'static str,
        choices: &'static [&'static str],
    ) -> Self {
        Self {
            name,
            required: false,
            kind: FieldType::Text,
            default: FieldDefault::Str(default),
            choices: Some(choices),
        }
    }

    const fn opt_int(name: &'static str, default: i64) -> Self {
        Self {
            name,
            required: false,
            kind: FieldType::Int,
            default: FieldDefault::Int(default),
            choices: None,
        }
    }

    fn to_json(&self) -> Value {
        let type_str = match self.kind {
            FieldType::Text => "text",
            FieldType::Int => "number",
        };
        let default_val = match self.default {
            FieldDefault::Str(s) => Value::String(s.to_string()),
            FieldDefault::Int(n) => json!(n),
        };
        let choices_val = match self.choices {
            Some(list) => Value::Array(list.iter().map(|s| Value::String((*s).to_string())).collect()),
            None => Value::Null,
        };
        json!({
            "name": self.name,
            "required": self.required,
            "type": type_str,
            "default": default_val,
            "choices": choices_val,
        })
    }

    fn default_string(&self) -> String {
        match self.default {
            FieldDefault::Str(s) => s.to_string(),
            FieldDefault::Int(n) => n.to_string(),
        }
    }
}

const GROUP_BY_CHOICES: &[&str] = &["person", "session"];
const TEACHER_TUNE_CHOICES: &[&str] = &["frozen", "last-two"];
const LOCAL_ADAPT_TUNE_CHOICES: &[&str] = &["head", "all"];

const SPLIT_FIELDS: &[FieldSpec] = &[
    FieldSpec::req_text("dataset"),
    FieldSpec::req_text("split"),
    FieldSpec::opt_choice("group_by", "person", GROUP_BY_CHOICES),
];

const TEACHER_TRAIN_FIELDS: &[FieldSpec] = &[
    FieldSpec::req_text("dataset"),
    FieldSpec::req_text("split"),
    FieldSpec::req_text("output"),
    FieldSpec::opt_text("dino_repo", ""),
    FieldSpec::opt_text("initial", ""),
    FieldSpec::opt_int("epochs", 10),
    FieldSpec::opt_text("device", "cpu"),
    FieldSpec::opt_text("backbone_checkpoint", ""),
    FieldSpec::opt_choice("tune", "frozen", TEACHER_TUNE_CHOICES),
];

const STUDENT_BASELINE_FIELDS: &[FieldSpec] = &[
    FieldSpec::req_text("dataset"),
    FieldSpec::req_text("split"),
    FieldSpec::req_text("output"),
    FieldSpec::opt_text("dino_repo", ""),
    FieldSpec::opt_text("initial", ""),
    FieldSpec::opt_int("epochs", 10),
    FieldSpec::opt_text("device", "cpu"),
    FieldSpec::opt_text("pretrained", ""),
];

const STUDENT_DISTILL_FIELDS: &[FieldSpec] = &[
    FieldSpec::req_text("dataset"),
    FieldSpec::req_text("split"),
    FieldSpec::req_text("output"),
    FieldSpec::opt_text("dino_repo", ""),
    FieldSpec::opt_text("initial", ""),
    FieldSpec::opt_int("epochs", 10),
    FieldSpec::opt_text("device", "cpu"),
    FieldSpec::opt_text("pretrained", ""),
    FieldSpec::opt_text("teacher", ""),
    FieldSpec::opt_text("teacher_approval", ""),
];

const LOCAL_ADAPT_FIELDS: &[FieldSpec] = &[
    FieldSpec::req_text("dataset"),
    FieldSpec::req_text("split"),
    FieldSpec::req_text("output"),
    FieldSpec::opt_text("dino_repo", ""),
    FieldSpec::opt_text("initial", ""),
    FieldSpec::opt_int("epochs", 10),
    FieldSpec::opt_text("device", "cpu"),
    FieldSpec::opt_text("pretrained", ""),
    FieldSpec::opt_text("teacher", ""),
    FieldSpec::opt_text("teacher_approval", ""),
    FieldSpec::opt_choice("tune", "head", LOCAL_ADAPT_TUNE_CHOICES),
];

const PREDICT_PSEUDO_FIELDS: &[FieldSpec] = &[
    FieldSpec::req_text("dataset"),
    FieldSpec::req_text("split"),
    FieldSpec::req_text("teacher"),
    FieldSpec::req_text("teacher_approval"),
    FieldSpec::opt_text("dino_repo", ""),
    FieldSpec::req_text("output"),
    FieldSpec::opt_text("device", "cpu"),
];

const EVALUATE_FIELDS: &[FieldSpec] = &[
    FieldSpec::req_text("dataset"),
    FieldSpec::req_text("split"),
    FieldSpec::req_text("checkpoint"),
    FieldSpec::opt_text("dino_repo", ""),
    FieldSpec::req_text("output"),
];

const VALIDATE_CANDIDATE_FIELDS: &[FieldSpec] = &[
    FieldSpec::req_text("dataset"),
    FieldSpec::req_text("split"),
    FieldSpec::req_text("candidate"),
    FieldSpec::req_text("baseline"),
    FieldSpec::opt_text("dino_repo", ""),
    FieldSpec::req_text("output"),
];

const EXPORT_FIELDS: &[FieldSpec] = &[
    FieldSpec::req_text("checkpoint"),
    FieldSpec::opt_text("dino_repo", ""),
    FieldSpec::opt_text("approval", ""),
    FieldSpec::req_text("output"),
];

const OPERATIONS: &[(&str, &[FieldSpec])] = &[
    ("split", SPLIT_FIELDS),
    ("teacher-train", TEACHER_TRAIN_FIELDS),
    ("student-baseline", STUDENT_BASELINE_FIELDS),
    ("student-distill", STUDENT_DISTILL_FIELDS),
    ("local-adapt", LOCAL_ADAPT_FIELDS),
    ("predict-pseudo", PREDICT_PSEUDO_FIELDS),
    ("evaluate", EVALUATE_FIELDS),
    ("validate-candidate", VALIDATE_CANDIDATE_FIELDS),
    ("export", EXPORT_FIELDS),
];

fn lookup_operation(operation: &str) -> Option<&'static [FieldSpec]> {
    OPERATIONS
        .iter()
        .find_map(|&(name, fields)| if name == operation { Some(fields) } else { None })
}

/// Returns the exact operations schema derived by `Runtime.training_operations()` from `face_train.py`.
pub fn training_operations() -> Value {
    let mut map = Map::new();
    for &(name, fields) in OPERATIONS {
        map.insert(
            name.to_string(),
            Value::Array(fields.iter().map(FieldSpec::to_json).collect()),
        );
    }
    Value::Object(map)
}

fn json_arg_to_string(value: &Value, field: &FieldSpec) -> Result<Option<String>, String> {
    match value {
        Value::Null => Ok(None),
        Value::String(s) => Ok(Some(s.clone())),
        Value::Number(n) => {
            if field.kind == FieldType::Int {
                if let Some(i) = n.as_i64() {
                    return Ok(Some(i.to_string()));
                }
                if let Some(f) = n.as_f64() {
                    if f.is_finite() && f.fract() == 0.0 && f >= i64::MIN as f64 && f <= i64::MAX as f64 {
                        return Ok(Some((f as i64).to_string()));
                    }
                }
                return Err(format!("Invalid local training arguments: {}", field.name));
            }
            Ok(Some(n.to_string()))
        }
        Value::Bool(_) | Value::Array(_) | Value::Object(_) => {
            Err(format!("Invalid local training arguments: {}", field.name))
        }
    }
}

/// Validate `operation` and `args` against `face_train.py`'s CLI schema and return the
/// subcommand + `--flag value` argument list.
pub fn validate_operation_args(operation: &str, args: &Value) -> Result<Vec<String>, String> {
    let fields = lookup_operation(operation)
        .ok_or_else(|| "Unknown local training operation".to_string())?;

    let arg_map: HashMap<&str, &Value> = match args {
        Value::Null => HashMap::new(),
        Value::Object(obj) => obj.iter().map(|(k, v)| (k.as_str(), v)).collect(),
        _ => return Err("Training arguments must be a JSON object".to_string()),
    };

    let known: HashSet<&str> = fields.iter().map(|f| f.name).collect();
    if arg_map.keys().any(|k| !known.contains(k)) {
        return Err("Unknown local training arguments".to_string());
    }

    let mut command = vec![operation.to_string()];
    for field in fields {
        let raw_value: Option<String> = match arg_map.get(field.name) {
            Some(v) => json_arg_to_string(v, field)?,
            None => Some(field.default_string()),
        };

        let trimmed = raw_value.as_deref().map(str::trim).unwrap_or("");
        if field.required && trimmed.is_empty() {
            return Err(format!("Required local training argument: {}", field.name));
        }

        if let Some(val) = raw_value {
            if !val.trim().is_empty() {
                match field.kind {
                    FieldType::Int => {
                        let parsed = val.trim().parse::<i64>().map_err(|_| {
                            format!("Invalid local training arguments: {}", field.name)
                        })?;
                        let normalized = parsed.to_string();
                        if let Some(choices) = field.choices {
                            if !choices.contains(&normalized.as_str()) {
                                return Err(format!(
                                    "Invalid local training arguments: {}",
                                    field.name
                                ));
                            }
                        }
                        command.push(format!("--{}", field.name.replace('_', "-")));
                        command.push(normalized);
                    }
                    FieldType::Text => {
                        if let Some(choices) = field.choices {
                            if !choices.contains(&val.as_str()) {
                                return Err(format!(
                                    "Invalid local training arguments: {}",
                                    field.name
                                ));
                            }
                        }
                        command.push(format!("--{}", field.name.replace('_', "-")));
                        command.push(val);
                    }
                }
            }
        }
    }

    Ok(command)
}

fn resolve_python_with_env(script_root: &Path, env_python: Option<&str>) -> PathBuf {
    if let Some(env_val) = env_python {
        let trimmed = env_val.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    let win_venv = script_root.join(".venv").join("Scripts").join("python.exe");
    if win_venv.is_file() {
        return win_venv;
    }
    let unix_venv = script_root.join(".venv").join("bin").join("python");
    if unix_venv.is_file() {
        return unix_venv;
    }
    PathBuf::from("python")
}

/// Resolve the Python interpreter for offline training:
/// `PICOET_PYTHON` env -> `<script_root>/.venv/Scripts/python.exe`
/// -> `<script_root>/.venv/bin/python` -> `"python"`.
pub fn resolve_python(script_root: &Path) -> PathBuf {
    let env_val = std::env::var("PICOET_PYTHON").ok();
    resolve_python_with_env(script_root, env_val.as_deref())
}

struct WorkerHandle {
    handle: JoinHandle<()>,
    done_rx: mpsc::Receiver<()>,
}

struct TrainingState {
    job_id: u64,
    state: String,
    running: bool,
    operation: Option<String>,
    error: Option<String>,
    result: Option<Value>,
    progress: Option<Value>,
    log_tail: Vec<String>,
    child: Option<Arc<Mutex<Option<Child>>>>,
    worker: Option<WorkerHandle>,
}

impl Default for TrainingState {
    fn default() -> Self {
        Self {
            job_id: 0,
            state: "idle".to_string(),
            running: false,
            operation: None,
            error: None,
            result: None,
            progress: None,
            log_tail: Vec::new(),
            child: None,
            worker: None,
        }
    }
}

impl TrainingState {
    fn snapshot(&self) -> Value {
        json!({
            "state": self.state,
            "running": self.running,
            "operation": self.operation,
            "error": self.error,
            "result": self.result,
            "progress": self.progress,
            "log_tail": self.log_tail,
            "operations": training_operations(),
        })
    }

    fn ingest_line(&mut self, job_id: u64, raw_line: &str) {
        if self.job_id != job_id {
            return;
        }
        let cleaned = raw_line.trim_end_matches(['\r', '\n']).trim_end().to_string();
        self.log_tail.push(cleaned);
        if self.log_tail.len() > LOG_TAIL_LIMIT {
            let drain = self.log_tail.len() - LOG_TAIL_LIMIT;
            self.log_tail.drain(0..drain);
        }
        if let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(raw_line) {
            match obj.get("event").and_then(Value::as_str) {
                Some("result") => {
                    if let Some(res) = obj.get("result") {
                        self.result = Some(res.clone());
                    }
                }
                Some("progress") => {
                    self.progress = Some(Value::Object(obj));
                }
                _ => {}
            }
        }
    }
}

pub struct TrainingRunner {
    inner: Arc<Mutex<TrainingState>>,
}

impl Default for TrainingRunner {
    fn default() -> Self {
        Self::new()
    }
}

impl Drop for TrainingRunner {
    fn drop(&mut self) {
        let _ = self.cancel();
    }
}

impl TrainingRunner {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(TrainingState::default())),
        }
    }

    pub fn is_running(&self) -> bool {
        self.inner.lock().map(|s| s.running).unwrap_or(false)
    }

    pub fn status(&self) -> Value {
        self.inner.lock().unwrap().snapshot()
    }

    /// Validate operation/args, resolve Python, and launch `face_train.py` in the background.
    pub fn begin(
        &self,
        operation: &str,
        args: &Value,
        script_root: &Path,
    ) -> Result<Value, String> {
        let python = resolve_python(script_root);
        self.begin_with_python(operation, args, script_root, &python)
    }

    /// Launch `face_train.py` using an explicit Python executable path (useful for testing
    /// or custom interpreter overrides).
    pub fn begin_with_python(
        &self,
        operation: &str,
        args: &Value,
        script_root: &Path,
        python: &Path,
    ) -> Result<Value, String> {
        // Reap any already-finished worker handle before checking running state.
        let (prev_worker, start_gen) = {
            let mut st = self.inner.lock().unwrap();
            if st.running {
                return Err("A local training job is already running".to_string());
            }
            (st.worker.take(), st.job_id)
        };
        if let Some(w) = prev_worker {
            let _ = w.handle.join();
        }

        let cli_args = validate_operation_args(operation, args)?;
        let script_path = script_root.join("face_train.py");

        let mut cmd = Command::new(python);
        cmd.arg("-B")
            .arg("-X")
            .arg("utf8")
            .arg(&script_path)
            .args(&cli_args)
            .current_dir(script_root)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }

        let mut child = cmd
            .spawn()
            .map_err(|e| format!("Failed to spawn local training process: {e}"))?;

        let stdout = child.stdout.take();
        let stderr = child.stderr.take();
        let child_arc = Arc::new(Mutex::new(Some(child)));

        let (done_tx, done_rx) = mpsc::channel();
        let inner_clone = Arc::clone(&self.inner);
        let child_for_worker = Arc::clone(&child_arc);

        // Hold state lock across job registration, monitor-thread spawn, and st.worker
        // assignment so cancel() can never observe st.child without st.worker or race
        // with thread startup.
        let mut st = self.inner.lock().unwrap();
        if st.running {
            drop(st);
            if let Ok(mut guard) = child_arc.lock() {
                if let Some(mut c) = guard.take() {
                    Self::terminate_child(&mut c);
                    let _ = c.wait();
                }
            }
            return Err("A local training job is already running".to_string());
        }
        if st.job_id != start_gen {
            let snap = st.snapshot();
            drop(st);
            if let Ok(mut guard) = child_arc.lock() {
                if let Some(mut c) = guard.take() {
                    Self::terminate_child(&mut c);
                    let _ = c.wait();
                }
            }
            return Ok(snap);
        }

        let job_id = st.job_id.wrapping_add(1);
        st.job_id = job_id;
        st.state = "training".to_string();
        st.running = true;
        st.operation = Some(operation.to_string());
        st.error = None;
        st.result = None;
        st.progress = None;
        st.log_tail.clear();
        st.child = Some(Arc::clone(&child_arc));

        let handle = match thread::Builder::new()
            .name("mouth-local-training".to_string())
            .spawn(move || {
                struct DoneSignal(Option<mpsc::Sender<()>>);
                impl Drop for DoneSignal {
                    fn drop(&mut self) {
                        if let Some(tx) = self.0.take() {
                            let _ = tx.send(());
                        }
                    }
                }
                let _done = DoneSignal(Some(done_tx));

                let stderr_handle = stderr.map(|err_pipe| {
                    let st_ref = Arc::clone(&inner_clone);
                    thread::Builder::new()
                        .name("mouth-local-training-stderr".to_string())
                        .spawn(move || {
                            let mut reader = BufReader::new(err_pipe);
                            let mut line = String::new();
                            loop {
                                line.clear();
                                match reader.read_line(&mut line) {
                                    Ok(0) | Err(_) => break,
                                    Ok(_) => {
                                        if let Ok(mut st) = st_ref.lock() {
                                            st.ingest_line(job_id, &line);
                                        }
                                    }
                                }
                            }
                        })
                        .ok()
                });

                if let Some(out_pipe) = stdout {
                    let mut reader = BufReader::new(out_pipe);
                    let mut line = String::new();
                    loop {
                        line.clear();
                        match reader.read_line(&mut line) {
                            Ok(0) | Err(_) => break,
                            Ok(_) => {
                                if let Ok(mut st) = inner_clone.lock() {
                                    st.ingest_line(job_id, &line);
                                }
                            }
                        }
                    }
                }

                if let Some(Some(h)) = stderr_handle {
                    let _ = h.join();
                }

                // Wait for child exit without holding the child mutex across blocking wait,
                // so cancel() can still kill the process if pipes closed early.
                let exit_status = loop {
                    let status_opt = {
                        let mut guard = child_for_worker.lock().unwrap();
                        match guard.as_mut() {
                            None => break None,
                            Some(child) => match child.try_wait() {
                                Ok(Some(status)) => {
                                    *guard = None;
                                    Some(status)
                                }
                                Ok(None) => None,
                                Err(_) => {
                                    *guard = None;
                                    break None;
                                }
                            },
                        }
                    };
                    if let Some(status) = status_opt {
                        break Some(status);
                    }
                    thread::sleep(Duration::from_millis(15));
                };

                if let Ok(mut st) = inner_clone.lock() {
                    if st.job_id == job_id {
                        let cancelled = st.state == "cancelling" || st.state == "cancelled";
                        let ok = exit_status.map(|s| s.success()).unwrap_or(false);
                        st.running = false;
                        st.child = None;
                        st.state = if cancelled {
                            "cancelled".to_string()
                        } else if ok {
                            "completed".to_string()
                        } else {
                            "failed".to_string()
                        };
                        st.error = if ok || cancelled {
                            None
                        } else if st.log_tail.is_empty() {
                            let code = exit_status
                                .and_then(|s| s.code())
                                .map(|c| c.to_string())
                                .unwrap_or_else(|| "unknown".to_string());
                            Some(format!("Local training exited with code {code}"))
                        } else {
                            Some(st.log_tail.join("\n"))
                        };
                    }
                }
            }) {
            Ok(h) => h,
            Err(e) => {
                st.running = false;
                st.state = "failed".to_string();
                st.child = None;
                st.error = Some(e.to_string());
                drop(st);
                if let Ok(mut guard) = child_arc.lock() {
                    if let Some(mut c) = guard.take() {
                        Self::terminate_child(&mut c);
                        let _ = c.wait();
                    }
                }
                return Err(format!("Failed to start training monitor thread: {e}"));
            }
        };

        st.worker = Some(WorkerHandle { handle, done_rx });
        Ok(st.snapshot())
    }

    fn terminate_child(child: &mut Child) {
        if !matches!(child.try_wait(), Ok(None)) {
            return;
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            let pid = child.id().to_string();
            let _ = Command::new("taskkill")
                .args(["/PID", &pid, "/T", "/F"])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .creation_flags(CREATE_NO_WINDOW)
                .status();
        }
        let _ = child.kill();
    }

    /// Terminate any running training subprocess and join its reader thread with timeout.
    pub fn cancel(&self) -> Result<Value, String> {
        let (child_opt, worker_opt) = {
            let mut st = self.inner.lock().unwrap();
            if st.running {
                st.state = "cancelling".to_string();
            } else {
                // Bump job_id so any concurrent begin() still inside Command::spawn()
                // observes the cancellation and aborts cleanly before publishing.
                st.job_id = st.job_id.wrapping_add(1);
            }
            (st.child.clone(), st.worker.take())
        };

        if let Some(child_arc) = child_opt {
            if let Ok(mut guard) = child_arc.lock() {
                if let Some(child) = guard.as_mut() {
                    Self::terminate_child(child);
                }
            }
        }

        if let Some(worker) = worker_opt {
            let deadline = Instant::now() + CANCEL_JOIN_TIMEOUT;
            let remaining = deadline.saturating_duration_since(Instant::now());
            match worker.done_rx.recv_timeout(remaining) {
                Ok(()) | Err(mpsc::RecvTimeoutError::Disconnected) => {
                    let _ = worker.handle.join();
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    drop(worker.handle);
                    return Err("Owned whole-mouth training worker did not stop".to_string());
                }
            }
        }

        let mut st = self.inner.lock().unwrap();
        st.job_id = st.job_id.wrapping_add(1);
        st.state = "cancelled".to_string();
        st.running = false;
        st.child = None;
        st.error = None;
        Ok(st.snapshot())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn wait_for_idle(runner: &TrainingRunner, timeout: Duration) {
        let deadline = Instant::now() + timeout;
        while runner.is_running() {
            assert!(
                Instant::now() < deadline,
                "timed out waiting for training runner to finish; status={}",
                runner.status()
            );
            thread::sleep(Duration::from_millis(15));
        }
    }

    #[cfg(windows)]
    fn write_mock_python(dir: &Path, name: &str, body: &str) -> PathBuf {
        let path = dir.join(format!("{name}.cmd"));
        fs::write(&path, format!("@echo off\r\n{body}\r\n")).unwrap();
        path
    }

    #[cfg(not(windows))]
    fn write_mock_python(dir: &Path, name: &str, body: &str) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let path = dir.join(format!("{name}.sh"));
        fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
        let mut perms = fs::metadata(&path).unwrap().permissions();
        perms.set_mode(0o755);
        fs::set_permissions(&path, perms).unwrap();
        path
    }

    #[test]
    fn training_operations_matches_face_train_schema_exactly() {
        let ops = training_operations();
        let obj = ops.as_object().unwrap();
        assert_eq!(obj.len(), 9);
        for op in [
            "split",
            "teacher-train",
            "student-baseline",
            "student-distill",
            "local-adapt",
            "predict-pseudo",
            "evaluate",
            "validate-candidate",
            "export",
        ] {
            assert!(obj.contains_key(op), "missing operation {op}");
        }

        // Verify exact field sequences and properties for representative operations.
        assert_eq!(
            ops["split"],
            json!([
                {"name": "dataset", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "split", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "group_by", "required": false, "type": "text", "default": "person", "choices": ["person", "session"]}
            ])
        );
        assert_eq!(
            ops["teacher-train"],
            json!([
                {"name": "dataset", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "split", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "output", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "dino_repo", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "initial", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "epochs", "required": false, "type": "number", "default": 10, "choices": null},
                {"name": "device", "required": false, "type": "text", "default": "cpu", "choices": null},
                {"name": "backbone_checkpoint", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "tune", "required": false, "type": "text", "default": "frozen", "choices": ["frozen", "last-two"]}
            ])
        );
        assert_eq!(
            ops["local-adapt"],
            json!([
                {"name": "dataset", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "split", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "output", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "dino_repo", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "initial", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "epochs", "required": false, "type": "number", "default": 10, "choices": null},
                {"name": "device", "required": false, "type": "text", "default": "cpu", "choices": null},
                {"name": "pretrained", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "teacher", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "teacher_approval", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "tune", "required": false, "type": "text", "default": "head", "choices": ["head", "all"]}
            ])
        );
        assert_eq!(
            ops["predict-pseudo"],
            json!([
                {"name": "dataset", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "split", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "teacher", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "teacher_approval", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "dino_repo", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "output", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "device", "required": false, "type": "text", "default": "cpu", "choices": null}
            ])
        );
        assert_eq!(
            ops["student-baseline"],
            json!([
                {"name": "dataset", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "split", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "output", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "dino_repo", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "initial", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "epochs", "required": false, "type": "number", "default": 10, "choices": null},
                {"name": "device", "required": false, "type": "text", "default": "cpu", "choices": null},
                {"name": "pretrained", "required": false, "type": "text", "default": "", "choices": null}
            ])
        );
        assert_eq!(
            ops["student-distill"],
            json!([
                {"name": "dataset", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "split", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "output", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "dino_repo", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "initial", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "epochs", "required": false, "type": "number", "default": 10, "choices": null},
                {"name": "device", "required": false, "type": "text", "default": "cpu", "choices": null},
                {"name": "pretrained", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "teacher", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "teacher_approval", "required": false, "type": "text", "default": "", "choices": null}
            ])
        );
        assert_eq!(
            ops["evaluate"],
            json!([
                {"name": "dataset", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "split", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "checkpoint", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "dino_repo", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "output", "required": true, "type": "text", "default": "", "choices": null}
            ])
        );
        assert_eq!(
            ops["validate-candidate"],
            json!([
                {"name": "dataset", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "split", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "candidate", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "baseline", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "dino_repo", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "output", "required": true, "type": "text", "default": "", "choices": null}
            ])
        );
        assert_eq!(
            ops["export"],
            json!([
                {"name": "checkpoint", "required": true, "type": "text", "default": "", "choices": null},
                {"name": "dino_repo", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "approval", "required": false, "type": "text", "default": "", "choices": null},
                {"name": "output", "required": true, "type": "text", "default": "", "choices": null}
            ])
        );
    }

    #[test]
    fn validate_operation_args_checks_required_choices_numbers_and_unknowns() {
        // Unknown operation.
        assert_eq!(
            validate_operation_args("bogus", &json!({})).unwrap_err(),
            "Unknown local training operation"
        );
        // Unknown arg key.
        assert_eq!(
            validate_operation_args("export", &json!({"checkpoint": "a", "output": "b", "extra": "x"})).unwrap_err(),
            "Unknown local training arguments"
        );
        // Missing required arg.
        let err = validate_operation_args("local-adapt", &json!({})).unwrap_err();
        assert!(err.starts_with("Required local training argument:"));
        // Blank required arg.
        let err = validate_operation_args("export", &json!({"checkpoint": "   ", "output": "b"})).unwrap_err();
        assert_eq!(err, "Required local training argument: checkpoint");
        // Invalid choice.
        assert!(validate_operation_args(
            "split",
            &json!({"dataset": "d", "split": "s", "group_by": "invalid"})
        )
        .is_err());
        // Invalid numeric type for epochs.
        assert!(validate_operation_args(
            "teacher-train",
            &json!({"dataset": "d", "split": "s", "output": "o", "epochs": "ten"})
        )
        .is_err());
        assert!(validate_operation_args(
            "teacher-train",
            &json!({"dataset": "d", "split": "s", "output": "o", "epochs": 3.5})
        )
        .is_err());
        assert!(validate_operation_args(
            "teacher-train",
            &json!({"dataset": "d", "split": "s", "output": "o", "epochs": true})
        )
        .is_err());

        // Valid args fill defaults and convert underscores to hyphens.
        let cmd = validate_operation_args(
            "local-adapt",
            &json!({
                "dataset": "ds",
                "split": "sp.json",
                "output": "out.pt",
                "teacher_approval": "ok.json",
                "epochs": 5
            }),
        )
        .unwrap();
        assert_eq!(
            cmd,
            vec![
                "local-adapt",
                "--dataset",
                "ds",
                "--split",
                "sp.json",
                "--output",
                "out.pt",
                "--epochs",
                "5",
                "--device",
                "cpu",
                "--teacher-approval",
                "ok.json",
                "--tune",
                "head",
            ]
        );
    }

    #[test]
    fn resolve_python_respects_env_then_venv_then_fallback() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(
            resolve_python_with_env(dir.path(), None),
            PathBuf::from("python")
        );
        assert_eq!(
            resolve_python_with_env(dir.path(), Some("  C:/custom/python.exe  ")),
            PathBuf::from("C:/custom/python.exe")
        );

        let unix_py = dir.path().join(".venv").join("bin").join("python");
        fs::create_dir_all(unix_py.parent().unwrap()).unwrap();
        fs::write(&unix_py, b"").unwrap();
        assert_eq!(resolve_python_with_env(dir.path(), None), unix_py);

        let win_py = dir.path().join(".venv").join("Scripts").join("python.exe");
        fs::create_dir_all(win_py.parent().unwrap()).unwrap();
        fs::write(&win_py, b"").unwrap();
        assert_eq!(resolve_python_with_env(dir.path(), None), win_py);

        // Env overrides venv.
        assert_eq!(
            resolve_python_with_env(dir.path(), Some("/env/python")),
            PathBuf::from("/env/python")
        );
    }

    #[test]
    fn runner_parses_progress_result_and_caps_log_tail() {
        let dir = tempfile::tempdir().unwrap();
        #[cfg(windows)]
        let mock = write_mock_python(
            dir.path(),
            "mock_ok",
            concat!(
                "for /L %%i in (1,1,32) do echo log-line-%%i\r\n",
                "echo {\"event\":\"progress\",\"epoch\":1,\"loss\":0.25}\r\n",
                "echo {\"event\":\"result\",\"result\":{\"output\":\"candidate.onnx\"}}\r\n",
                "echo args=%*\r\n",
            ),
        );
        #[cfg(not(windows))]
        let mock = write_mock_python(
            dir.path(),
            "mock_ok",
            concat!(
                "for i in $(seq 1 32); do echo log-line-$i; done\n",
                "echo '{\"event\":\"progress\",\"epoch\":1,\"loss\":0.25}'\n",
                "echo '{\"event\":\"result\",\"result\":{\"output\":\"candidate.onnx\"}}'\n",
                "echo \"args=$*\"\n",
            ),
        );

        let runner = TrainingRunner::new();
        let initial = runner.status();
        assert_eq!(initial["state"], "idle");
        assert_eq!(initial["running"], false);
        assert!(initial["operations"]["export"].is_array());

        let started = runner
            .begin_with_python(
                "export",
                &json!({"checkpoint": "student.pt", "output": "candidate.onnx"}),
                dir.path(),
                &mock,
            )
            .unwrap();
        assert_eq!(started["operation"], "export");

        wait_for_idle(&runner, Duration::from_secs(5));
        let st = runner.status();
        assert_eq!(st["state"], "completed");
        assert_eq!(st["running"], false);
        assert!(st["error"].is_null());
        assert_eq!(st["progress"]["event"], "progress");
        assert_eq!(st["progress"]["epoch"], 1);
        assert_eq!(st["result"]["output"], "candidate.onnx");

        let tail = st["log_tail"].as_array().unwrap();
        assert_eq!(tail.len(), LOG_TAIL_LIMIT);
        let last_line = tail.last().unwrap().as_str().unwrap();
        assert!(last_line.contains("face_train.py"), "last line: {last_line}");
        assert!(last_line.contains("export"), "last line: {last_line}");
        assert!(last_line.contains("--checkpoint"), "last line: {last_line}");
        assert!(last_line.contains("student.pt"), "last line: {last_line}");
    }

    #[test]
    fn runner_captures_stderr_and_marks_failed_on_nonzero_exit() {
        let dir = tempfile::tempdir().unwrap();
        #[cfg(windows)]
        let mock = write_mock_python(
            dir.path(),
            "mock_fail",
            "echo boom-from-stderr 1>&2\r\nexit /b 3",
        );
        #[cfg(not(windows))]
        let mock = write_mock_python(dir.path(), "mock_fail", "echo boom-from-stderr >&2\nexit 3");

        let runner = TrainingRunner::new();
        runner
            .begin_with_python(
                "export",
                &json!({"checkpoint": "a.pt", "output": "b.onnx"}),
                dir.path(),
                &mock,
            )
            .unwrap();

        wait_for_idle(&runner, Duration::from_secs(5));
        let st = runner.status();
        assert_eq!(st["state"], "failed");
        assert_eq!(st["running"], false);
        let err_text = st["error"].as_str().unwrap();
        assert!(err_text.contains("boom-from-stderr"), "err={err_text}");
    }

    #[test]
    fn runner_cancel_kills_long_running_process_and_rejects_concurrent_begin() {
        let dir = tempfile::tempdir().unwrap();
        #[cfg(windows)]
        let mock = write_mock_python(
            dir.path(),
            "mock_sleep",
            "echo {\"event\":\"progress\",\"epoch\":0}\r\nping -n 30 127.0.0.1 >nul",
        );
        #[cfg(not(windows))]
        let mock = write_mock_python(
            dir.path(),
            "mock_sleep",
            "echo '{\"event\":\"progress\",\"epoch\":0}'\nsleep 30",
        );

        let runner = TrainingRunner::new();
        runner
            .begin_with_python(
                "export",
                &json!({"checkpoint": "a.pt", "output": "b.onnx"}),
                dir.path(),
                &mock,
            )
            .unwrap();

        // Wait until the subprocess has actually started and emitted its progress line.
        let ready_deadline = Instant::now() + Duration::from_secs(3);
        while runner.status()["progress"].is_null() {
            assert!(
                Instant::now() < ready_deadline,
                "timed out waiting for subprocess progress event; status={}",
                runner.status()
            );
            thread::sleep(Duration::from_millis(15));
        }

        // Concurrent begin must be rejected.
        let dup_err = runner
            .begin_with_python(
                "export",
                &json!({"checkpoint": "a.pt", "output": "b.onnx"}),
                dir.path(),
                &mock,
            )
            .unwrap_err();
        assert_eq!(dup_err, "A local training job is already running");

        let t0 = Instant::now();
        let cancelled = runner.cancel().unwrap();
        assert!(
            t0.elapsed() < Duration::from_secs(4),
            "cancel took too long: {:?}",
            t0.elapsed()
        );
        assert_eq!(cancelled["state"], "cancelled");
        assert_eq!(cancelled["running"], false);
        assert!(!runner.is_running());

        // Calling cancel() when already idle/cancelled stays clean.
        let again = runner.cancel().unwrap();
        assert_eq!(again["state"], "cancelled");
        assert_eq!(again["running"], false);
    }

    #[test]
    fn runner_cancel_during_spawn_never_overwrites_cancelled_to_failed() {
        let dir = tempfile::tempdir().unwrap();
        #[cfg(windows)]
        let mock = write_mock_python(dir.path(), "mock_race", "ping -n 10 127.0.0.1 >nul");
        #[cfg(not(windows))]
        let mock = write_mock_python(dir.path(), "mock_race", "sleep 10");

        let runner = Arc::new(TrainingRunner::new());
        for _ in 0..5 {
            let r_begin = Arc::clone(&runner);
            let root = dir.path().to_path_buf();
            let exe = mock.clone();
            let t_begin = thread::spawn(move || {
                r_begin.begin_with_python(
                    "export",
                    &json!({"checkpoint": "a.pt", "output": "b.onnx"}),
                    &root,
                    &exe,
                )
            });
            let r_cancel = Arc::clone(&runner);
            let t_cancel = thread::spawn(move || {
                thread::sleep(Duration::from_millis(2));
                r_cancel.cancel()
            });
            let _ = t_begin.join().unwrap();
            let _ = t_cancel.join().unwrap();
            // Ensure final cancel cleans up whichever thread ordered second, and state is never "failed".
            let _ = runner.cancel().unwrap();
            wait_for_idle(&runner, Duration::from_secs(3));
            let st = runner.status();
            assert_eq!(st["state"], "cancelled", "unexpected status: {st}");
            assert_eq!(st["running"], false);
            assert!(st["error"].is_null(), "unexpected error: {st}");
        }
    }
}
