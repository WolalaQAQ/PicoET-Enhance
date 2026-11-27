use std::env;
use std::fs;
use std::path::PathBuf;

fn read_module_version() -> Option<String> {
    if let Ok(explicit) = env::var("PICOET_STUDIO_VERSION") {
        let trimmed = explicit.trim().to_string();
        if !trimmed.is_empty() {
            return Some(trimmed);
        }
    }
    let manifest_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").ok()?);
    let prop_path = manifest_dir
        .parent()?
        .parent()?
        .join("magisk-picoet-enhance")
        .join("module.prop");
    println!("cargo:rerun-if-changed={}", prop_path.display());
    let text = fs::read_to_string(prop_path).ok()?;
    for line in text.lines() {
        if let Some(rest) = line.strip_prefix("version=") {
            let trimmed = rest.trim().to_string();
            if !trimmed.is_empty() {
                return Some(trimmed);
            }
        }
    }
    None
}

fn main() {
    println!("cargo:rerun-if-env-changed=PICOET_STUDIO_VERSION");
    let version = read_module_version()
        .unwrap_or_else(|| env::var("CARGO_PKG_VERSION").unwrap_or_else(|_| "2.7.0".to_string()));
    println!("cargo:rustc-env=PICOET_STUDIO_VERSION={version}");
    tauri_build::build();
}
