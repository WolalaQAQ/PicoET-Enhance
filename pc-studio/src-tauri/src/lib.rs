pub mod config;
pub mod cymple_relay;
pub mod discovery;
pub mod runtime;
pub mod stock;
pub mod updater;
pub mod wire;

use crate::cymple_relay::CympleEyeRelay;
use crate::discovery::DiscoveryResult;
use crate::runtime::{Runtime, RuntimeConfig, StartRequest};
use crate::updater::{UpdateChecker, UpdateInfo};
use serde_json::{json, Value};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
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

#[cfg(windows)]
#[allow(dead_code)]
struct SyncHandles {
    mutex: usize,
    event: usize,
}

#[cfg(not(windows))]
struct SyncHandles;

pub struct AppState {
    runtime: Runtime,
    relay: CympleEyeRelay,
    updater: UpdateChecker,
    control: Mutex<()>,
    exiting: AtomicBool,
    tray_items: Mutex<Option<TrayMenuItems>>,
    _sync_handles: Mutex<Option<SyncHandles>>,
}

impl AppState {
    fn new() -> Self {
        let data_dir = config::resolve_data_dir();
        let runtime_config = data_dir.join("config.json");
        let relay_config = data_dir.join("cymple_relay.json");
        Self {
            runtime: Runtime::new(runtime_config),
            relay: CympleEyeRelay::new(Some(relay_config)),
            updater: UpdateChecker::new(),
            control: Mutex::new(()),
            exiting: AtomicBool::new(false),
            tray_items: Mutex::new(None),
            _sync_handles: Mutex::new(None),
        }
    }

    fn combined_status(&self) -> Value {
        let mut status = self.runtime.status();
        if let Some(obj) = status.as_object_mut() {
            obj.insert("cymple_relay".to_string(), self.relay.status());
        }
        status
    }
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
fn signal_and_wait_named_instance(event_name: &str, mutex_name: &str, wait_ms: u32) -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, WAIT_ABANDONED, WAIT_OBJECT_0};
    use windows_sys::Win32::Storage::FileSystem::SYNCHRONIZE;
    use windows_sys::Win32::System::Threading::{
        OpenEventW, OpenMutexW, ReleaseMutex, SetEvent, WaitForSingleObject, EVENT_MODIFY_STATE,
    };

    let event_w = to_wide_null(event_name);
    let mutex_w = to_wide_null(mutex_name);

    unsafe {
        let event = OpenEventW(EVENT_MODIFY_STATE, 0, event_w.as_ptr());
        if event.is_null() {
            return false;
        }
        let mutex = OpenMutexW(SYNCHRONIZE, 0, mutex_w.as_ptr());
        let _ = SetEvent(event);
        let _ = CloseHandle(event);

        if !mutex.is_null() {
            let rc = WaitForSingleObject(mutex, wait_ms);
            if rc == WAIT_OBJECT_0 || rc == WAIT_ABANDONED {
                let _ = ReleaseMutex(mutex);
            }
            let _ = CloseHandle(mutex);
        }
        // Brief grace period so Windows finishes unmapping the exiting executable image
        // before an Inno Setup installer overwrites or deletes it.
        std::thread::sleep(Duration::from_millis(150));
        true
    }
}

fn stop_legacy_python_instance(port: u16) {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(300)) else {
        return;
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
        return;
    }
    let mut buf = [0u8; 512];
    let _ = stream.read(&mut buf);
    drop(stream);

    let deadline = Instant::now() + Duration::from_millis(2500);
    while Instant::now() < deadline {
        if TcpStream::connect_timeout(&addr, Duration::from_millis(200)).is_err() {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

/// Signal any running Tauri instance (or legacy Python instance on 127.0.0.1:8787) to stop.
/// Always returns 0 so Inno Setup installers/uninstallers can proceed cleanly.
pub fn stop_running_instance() -> i32 {
    #[cfg(windows)]
    {
        if signal_and_wait_named_instance(STOP_EVENT_NAME, INSTANCE_MUTEX_NAME, 5000) {
            return 0;
        }
    }
    stop_legacy_python_instance(LEGACY_HTTP_PORT);
    0
}

#[cfg(windows)]
fn setup_instance_sync(app: &AppHandle, state: &Arc<AppState>) {
    use windows_sys::Win32::Foundation::{GetLastError, ERROR_ALREADY_EXISTS, HANDLE, WAIT_OBJECT_0};
    use windows_sys::Win32::System::Threading::{
        CreateEventW, CreateMutexW, ResetEvent, WaitForSingleObject, INFINITE,
    };

    let mutex_w = to_wide_null(INSTANCE_MUTEX_NAME);
    let event_w = to_wide_null(STOP_EVENT_NAME);

    unsafe {
        let mutex = CreateMutexW(std::ptr::null(), 1, mutex_w.as_ptr());
        if !mutex.is_null() && GetLastError() == ERROR_ALREADY_EXISTS {
            let _ = WaitForSingleObject(mutex, 0);
        }
        let event = CreateEventW(std::ptr::null(), 1, 0, event_w.as_ptr());
        if !event.is_null() {
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
    {
        let _guard = state.control.lock();
        state.relay.close();
        state.runtime.close();
    }
    app.exit(0);
}

fn validate_external_url(url: &str) -> Result<String, String> {
    let trimmed = url.trim();
    if trimmed.len() <= 8 || !trimmed[..8].eq_ignore_ascii_case("https://") {
        return Err("Only https:// URLs are allowed".to_string());
    }
    if trimmed
        .chars()
        .any(|c| c.is_control() || c.is_whitespace() || c == '"')
    {
        return Err("Invalid external URL".to_string());
    }
    Ok(trimmed.to_string())
}

#[cfg(windows)]
fn open_url_os(url: &str) -> Result<(), String> {
    use windows_sys::Win32::UI::Shell::ShellExecuteW;
    use windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;

    let op_w = to_wide_null("open");
    let url_w = to_wide_null(url);
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
}

#[cfg(not(windows))]
fn open_url_os(_url: &str) -> Result<(), String> {
    Err("Opening external URLs is only supported on Windows".to_string())
}

#[tauri::command]
fn get_status(state: State<'_, Arc<AppState>>) -> Value {
    state.combined_status()
}

#[tauri::command]
fn get_config(state: State<'_, Arc<AppState>>) -> RuntimeConfig {
    state.runtime.config()
}

#[tauri::command]
async fn set_config(
    changes: Value,
    state: State<'_, Arc<AppState>>,
) -> Result<RuntimeConfig, String> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = state.control.lock().map_err(|e| e.to_string())?;
        state.runtime.configure(changes)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn start_camera(
    payload: Value,
    state: State<'_, Arc<AppState>>,
) -> Result<Value, String> {
    let req: StartRequest = if payload.is_null() {
        StartRequest::default()
    } else {
        serde_json::from_value(payload).map_err(|e| e.to_string())?
    };
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = state.control.lock().map_err(|e| e.to_string())?;
        state.runtime.start(req)?;
        Ok(state.combined_status())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn stop_camera(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = state.control.lock().map_err(|e| e.to_string())?;
        state.runtime.stop()?;
        Ok(state.combined_status())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn discover_sources(
    host: Option<String>,
    port: Option<u16>,
) -> Result<DiscoveryResult, String> {
    let explicit_targets = match host.map(|h| h.trim().to_string()).filter(|h| !h.is_empty()) {
        Some(h) => {
            let p = match port {
                Some(0) => {
                    return Err("headset_port must be an integer in 1..65535".to_string());
                }
                Some(v) => v,
                None => discovery::DEFAULT_PORT,
            };
            Some(vec![(h, p)])
        }
        None => None,
    };
    tauri::async_runtime::spawn_blocking(move || {
        discovery::discover_sources(DISCOVERY_TIMEOUT, explicit_targets)
    })
    .await
    .map_err(|e| e.to_string())
}

#[tauri::command]
async fn cymple_relay_action(
    payload: Value,
    state: State<'_, Arc<AppState>>,
) -> Result<Value, String> {
    let Some(obj) = payload.as_object() else {
        return Err("JSON object required".to_string());
    };
    let action = obj
        .get("action")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let mut rest = serde_json::Map::new();
    for (k, v) in obj {
        if k != "action" {
            rest.insert(k.clone(), v.clone());
        }
    }
    let body = Value::Object(rest);
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = state.control.lock().map_err(|e| e.to_string())?;
        match action.as_str() {
            "start" => {
                let fallback_host = state.runtime.current_headset_host();
                state.relay.start(body, &fallback_host)
            }
            "stop" => Ok(state.relay.stop()),
            "configure" => state.relay.configure(body),
            _ => Err("Unknown Cymple relay action".to_string()),
        }
    })
    .await
    .map_err(|e| e.to_string())?
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
fn set_tray_language(language: String, state: State<'_, Arc<AppState>>) -> Result<(), String> {
    let labels = TrayLabels::for_language(&language);
    let guard = state.tray_items.lock().map_err(|e| e.to_string())?;
    if let Some(items) = guard.as_ref() {
        items.show.set_text(labels.show).map_err(|e| e.to_string())?;
        items
            .stop_relay
            .set_text(labels.stop_relay)
            .map_err(|e| e.to_string())?;
        items.quit.set_text(labels.quit).map_err(|e| e.to_string())?;
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
    let labels = TrayLabels::for_language("en");
    let show = MenuItem::with_id(app, "show", labels.show, true, None::<&str>)?;
    let stop_relay = MenuItem::with_id(app, "stop_relay", labels.stop_relay, true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let quit = MenuItem::with_id(app, "quit", labels.quit, true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &stop_relay, &separator, &quit])?;

    if let Ok(mut guard) = state.tray_items.lock() {
        *guard = Some(TrayMenuItems {
            show,
            stop_relay,
            quit,
        });
    }

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
                std::thread::spawn(move || {
                    let _guard = state.control.lock();
                    let _ = state.relay.stop();
                });
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
    let state = Arc::new(AppState::new());
    let setup_state = state.clone();

    let app = tauri::Builder::default()
        .manage(state)
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main_window(app);
        }))
        .setup(move |app| {
            let handle = app.handle();
            setup_instance_sync(handle, &setup_state);
            setup_tray(handle, &setup_state)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let state = window.state::<Arc<AppState>>();
                if !state.exiting.load(Ordering::SeqCst) {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_status,
            get_config,
            set_config,
            start_camera,
            stop_camera,
            discover_sources,
            cymple_relay_action,
            check_updates,
            get_preview_frame,
            set_tray_language,
            open_external_url,
            quit_app,
        ])
        .build(tauri::generate_context!())
        .expect("error while building PicoET PC Studio");

    app.run(|app_handle, event| match event {
        tauri::RunEvent::ExitRequested { api, code, .. } => {
            let state = app_handle.state::<Arc<AppState>>();
            if code.is_none() && !state.exiting.load(Ordering::SeqCst) {
                api.prevent_exit();
            }
        }
        tauri::RunEvent::Exit => {
            let state = app_handle.state::<Arc<AppState>>();
            if !state.exiting.swap(true, Ordering::SeqCst) {
                let _guard = state.control.lock();
                state.relay.close();
                state.runtime.close();
            }
        }
        _ => {}
    });
}

#[cfg(test)]
mod tests {
    use super::*;

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

    #[cfg(windows)]
    #[test]
    fn stop_event_signals_and_waits_for_mutex_release() {
        use std::sync::mpsc;
        use windows_sys::Win32::Foundation::{CloseHandle, WAIT_OBJECT_0};
        use windows_sys::Win32::System::Threading::{
            CreateEventW, CreateMutexW, ReleaseMutex, WaitForSingleObject, INFINITE,
        };

        let pid = std::process::id();
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
        assert!(signal_and_wait_named_instance(&event_name, &mutex_name, 3000));
        worker.join().unwrap();
    }
}
