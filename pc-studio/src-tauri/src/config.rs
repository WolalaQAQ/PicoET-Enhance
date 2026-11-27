use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::Duration;

static TMP_COUNTER: AtomicU64 = AtomicU64::new(1);

/// Resolve user-data directory matching the Python PC Studio convention:
/// - Portable mode: `<exe_dir>/user-data` if that directory exists or `%LOCALAPPDATA%` is unset.
/// - Installed mode: `%LOCALAPPDATA%/PicoET-PC-Studio/user-data`.
pub fn resolve_data_dir() -> PathBuf {
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."));

    let local_data = exe_dir.join("user-data");
    let local_app_data = std::env::var_os("LOCALAPPDATA")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from);

    if local_data.exists() || local_app_data.is_none() {
        local_data
    } else {
        local_app_data
            .unwrap()
            .join("PicoET-PC-Studio")
            .join("user-data")
    }
}

/// Atomically write UTF-8 text to `path`, retrying transient Windows sharing/access errors (5, 32).
pub fn write_text_atomic(path: &Path, content: &str) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)?;
        }
    }
    let pid = std::process::id();
    let seq = TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let file_name = path
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("config.json");
    let tmp = path.with_file_name(format!("{file_name}.{pid}.{seq}.tmp"));

    if let Err(err) = fs::write(&tmp, content.as_bytes()) {
        let _ = fs::remove_file(&tmp);
        return Err(err);
    }

    let mut last_err = None;
    for attempt in 0..6 {
        match replace_file(&tmp, path) {
            Ok(()) => return Ok(()),
            Err(err) => {
                let retryable = matches!(err.raw_os_error(), Some(5) | Some(32))
                    || err.kind() == io::ErrorKind::PermissionDenied;
                last_err = Some(err);
                if !retryable || attempt == 5 {
                    break;
                }
                thread::sleep(Duration::from_millis(25));
            }
        }
    }

    let _ = fs::remove_file(&tmp);
    Err(last_err.unwrap_or_else(|| io::Error::other("Atomic file replace failed")))
}

#[cfg(windows)]
fn replace_file(src: &Path, dst: &Path) -> io::Result<()> {
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
fn replace_file(src: &Path, dst: &Path) -> io::Result<()> {
    fs::rename(src, dst)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn atomic_write_creates_and_replaces() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("sub").join("config.json");
        write_text_atomic(&target, "{\"a\":1}").unwrap();
        assert_eq!(fs::read_to_string(&target).unwrap(), "{\"a\":1}");
        write_text_atomic(&target, "{\"a\":2}").unwrap();
        assert_eq!(fs::read_to_string(&target).unwrap(), "{\"a\":2}");
    }

    #[test]
    fn atomic_write_supports_bare_relative_filename() {
        struct RemoveOnDrop(PathBuf);
        impl Drop for RemoveOnDrop {
            fn drop(&mut self) {
                let _ = fs::remove_file(&self.0);
            }
        }

        let seq = TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
        let rel = PathBuf::from(format!(
            "picoet-atomic-rel-test-{}-{seq}.json",
            std::process::id()
        ));
        assert_eq!(rel.parent(), Some(Path::new("")));
        let _guard = RemoveOnDrop(rel.clone());

        write_text_atomic(&rel, "{\"rel\":1}").unwrap();
        assert_eq!(fs::read_to_string(&rel).unwrap(), "{\"rel\":1}");
        write_text_atomic(&rel, "{\"rel\":2}").unwrap();
        assert_eq!(fs::read_to_string(&rel).unwrap(), "{\"rel\":2}");
    }
}
