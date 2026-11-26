use serde::{Deserialize, Serialize};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const APP_VERSION: &str = env!("PICOET_STUDIO_VERSION");
pub const GITHUB_REPO: &str = "WolalaQAQ/PicoET-Enhance";
pub const RELEASES_LATEST_API: &str =
    "https://api.github.com/repos/WolalaQAQ/PicoET-Enhance/releases/latest";
pub const RELEASES_PAGE_URL: &str = "https://github.com/WolalaQAQ/PicoET-Enhance/releases";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedVersion {
    pub nums: (u64, u64, u64),
    pub is_stable: bool,
    pub suffix: String,
}

pub fn parse_version(text: &str) -> Option<ParsedVersion> {
    let trimmed = text.trim();
    let stripped = trimmed
        .strip_prefix('v')
        .or_else(|| trimmed.strip_prefix('V'))
        .unwrap_or(trimmed);
    let (core, suffix) = match stripped.split_once('-') {
        Some((c, s)) => {
            let trimmed_suffix = s.trim();
            if trimmed_suffix.is_empty() {
                return None;
            }
            (c, trimmed_suffix.to_string())
        }
        None => (stripped, String::new()),
    };
    let parse_digits = |part: &str| -> Option<u64> {
        if part.is_empty() || !part.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        part.parse::<u64>().ok()
    };
    let mut parts = core.split('.');
    let major = parse_digits(parts.next()?)?;
    let minor = parse_digits(parts.next()?)?;
    let patch = parse_digits(parts.next()?)?;
    if parts.next().is_some() {
        return None;
    }
    Some(ParsedVersion {
        nums: (major, minor, patch),
        is_stable: suffix.is_empty(),
        suffix,
    })
}

pub fn is_newer_version(candidate: &str, current: &str) -> bool {
    let (Some(cand), Some(curr)) = (parse_version(candidate), parse_version(current)) else {
        return false;
    };
    if cand.nums != curr.nums {
        return cand.nums > curr.nums;
    }
    if cand.is_stable != curr.is_stable {
        return cand.is_stable && !curr.is_stable;
    }
    cand.suffix > curr.suffix
}

pub fn summarize_notes(body: &str, max_lines: usize) -> Vec<String> {
    let mut lines = Vec::new();
    for raw in body.lines() {
        let mut line = raw.trim();
        if line.is_empty() || line.starts_with('#') || line == "---" {
            continue;
        }
        if let Some(rest) = line.strip_prefix("- ").or_else(|| line.strip_prefix("* ")) {
            line = rest.trim();
        }
        lines.push(line.to_string());
        if lines.len() >= max_lines {
            break;
        }
    }
    lines
}

#[derive(Debug, Clone, Default)]
pub struct ClassifiedAssets {
    pub installer_name: String,
    pub installer_url: String,
    pub portable_name: String,
    pub portable_url: String,
    pub module_name: String,
    pub module_url: String,
}

fn deserialize_null_default<'de, D, T>(deserializer: D) -> Result<T, D::Error>
where
    T: Default + Deserialize<'de>,
    D: serde::Deserializer<'de>,
{
    let opt = Option::deserialize(deserializer)?;
    Ok(opt.unwrap_or_default())
}

#[derive(Debug, Deserialize)]
pub struct ReleaseAsset {
    #[serde(default, deserialize_with = "deserialize_null_default")]
    pub name: String,
    #[serde(default, deserialize_with = "deserialize_null_default")]
    pub browser_download_url: String,
}

#[derive(Debug, Deserialize)]
struct GithubReleasePayload {
    #[serde(default, deserialize_with = "deserialize_null_default")]
    tag_name: String,
    #[serde(default, deserialize_with = "deserialize_null_default")]
    name: String,
    #[serde(default, deserialize_with = "deserialize_null_default")]
    html_url: String,
    #[serde(default, deserialize_with = "deserialize_null_default")]
    published_at: String,
    #[serde(default, deserialize_with = "deserialize_null_default")]
    body: String,
    #[serde(default, deserialize_with = "deserialize_null_default")]
    assets: Vec<ReleaseAsset>,
}

pub fn classify_assets(assets: &[ReleaseAsset]) -> ClassifiedAssets {
    let mut result = ClassifiedAssets::default();
    for item in assets {
        let name = item.name.trim();
        let url = item.browser_download_url.trim();
        let lower = name.to_ascii_lowercase();
        if name.is_empty() || url.is_empty() || lower.ends_with(".sha256") {
            continue;
        }
        if result.installer_url.is_empty()
            && lower.contains("pc-studio")
            && lower.ends_with(".exe")
        {
            result.installer_name = name.to_string();
            result.installer_url = url.to_string();
        } else if result.portable_url.is_empty()
            && lower.contains("pc-studio")
            && lower.ends_with(".zip")
        {
            result.portable_name = name.to_string();
            result.portable_url = url.to_string();
        } else if result.module_url.is_empty()
            && lower.contains("magisk-picoet-enhance")
            && lower.ends_with(".zip")
        {
            result.module_name = name.to_string();
            result.module_url = url.to_string();
        }
    }
    result
}

#[derive(Debug, Clone, Serialize)]
pub struct UpdateInfo {
    pub current_version: String,
    pub latest_version: Option<String>,
    pub tag_name: String,
    pub update_available: bool,
    pub release_url: String,
    pub installer_name: String,
    pub installer_url: String,
    pub portable_name: String,
    pub portable_url: String,
    pub module_name: String,
    pub module_url: String,
    pub published_at: String,
    pub notes: Vec<String>,
    pub checked_at: u64,
    pub error: Option<String>,
}

struct CachedUpdate {
    info: UpdateInfo,
    at: Instant,
}

pub struct UpdateChecker {
    current_version: String,
    api_url: String,
    cache_ttl: Duration,
    cache: Mutex<Option<CachedUpdate>>,
}

impl UpdateChecker {
    pub fn new() -> Self {
        Self::with_config(
            APP_VERSION.to_string(),
            RELEASES_LATEST_API.to_string(),
            Duration::from_secs(300),
        )
    }

    pub fn with_config(current_version: String, api_url: String, cache_ttl: Duration) -> Self {
        Self {
            current_version,
            api_url,
            cache_ttl,
            cache: Mutex::new(None),
        }
    }

    fn base_response(&self) -> UpdateInfo {
        let checked_at = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        UpdateInfo {
            current_version: self.current_version.clone(),
            latest_version: None,
            tag_name: String::new(),
            update_available: false,
            release_url: RELEASES_PAGE_URL.to_string(),
            installer_name: String::new(),
            installer_url: String::new(),
            portable_name: String::new(),
            portable_url: String::new(),
            module_name: String::new(),
            module_url: String::new(),
            published_at: String::new(),
            notes: Vec::new(),
            checked_at,
            error: None,
        }
    }

    pub fn check(&self, force: bool) -> UpdateInfo {
        if !force {
            if let Ok(guard) = self.cache.lock() {
                if let Some(entry) = guard.as_ref() {
                    if entry.info.error.is_none() && entry.at.elapsed() < self.cache_ttl {
                        return entry.info.clone();
                    }
                }
            }
        }

        let mut res = self.base_response();
        let client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(5))
            .build();

        match client {
            Ok(cli) => {
                let req = cli
                    .get(&self.api_url)
                    .header("Accept", "application/vnd.github+json")
                    .header(
                        "User-Agent",
                        format!("PicoET-PC-Studio/{}", self.current_version),
                    );
                match req.send() {
                    Ok(resp) => {
                        let status = resp.status();
                        if status.as_u16() == 404 {
                            res.latest_version = Some(self.current_version.clone());
                            res.tag_name = format!("v{}", self.current_version);
                        } else if !status.is_success() {
                            res.error = Some(format!("HTTP {}", status.as_u16()));
                        } else {
                            match resp.json::<GithubReleasePayload>() {
                                Ok(payload) => {
                                    let tag = payload.tag_name.trim().to_string();
                                    let name_ver = payload
                                        .name
                                        .trim()
                                        .trim_start_matches(['v', 'V'])
                                        .to_string();
                                    let tag_ver =
                                        tag.trim_start_matches(['v', 'V']).to_string();
                                    let latest_ver = if !tag_ver.is_empty() {
                                        tag_ver
                                    } else if !name_ver.is_empty() {
                                        name_ver
                                    } else {
                                        self.current_version.clone()
                                    };
                                    res.tag_name = if !tag.is_empty() {
                                        tag
                                    } else {
                                        format!("v{latest_ver}")
                                    };
                                    res.update_available =
                                        is_newer_version(&latest_ver, &self.current_version);
                                    res.latest_version = Some(latest_ver);
                                    if !payload.html_url.trim().is_empty() {
                                        res.release_url = payload.html_url.trim().to_string();
                                    }
                                    res.published_at = payload.published_at.trim().to_string();
                                    res.notes = summarize_notes(&payload.body, 6);
                                    let assets = classify_assets(&payload.assets);
                                    res.installer_name = assets.installer_name;
                                    res.installer_url = assets.installer_url;
                                    res.portable_name = assets.portable_name;
                                    res.portable_url = assets.portable_url;
                                    res.module_name = assets.module_name;
                                    res.module_url = assets.module_url;
                                }
                                Err(err) => {
                                    res.error = Some(err.to_string());
                                }
                            }
                        }
                    }
                    Err(err) => {
                        res.error = Some(err.to_string());
                    }
                }
            }
            Err(err) => {
                res.error = Some(err.to_string());
            }
        }

        if let Ok(mut guard) = self.cache.lock() {
            *guard = Some(CachedUpdate {
                info: res.clone(),
                at: Instant::now(),
            });
        }
        res
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_comparison_matches_semver_and_prerelease() {
        assert!(is_newer_version("2.6.3", "2.6.2"));
        assert!(is_newer_version("v2.7.0", "2.6.9"));
        assert!(is_newer_version("2.6.2", "2.6.2-dev.1"));
        assert!(!is_newer_version("2.6.2-dev.1", "2.6.2"));
        assert!(!is_newer_version("2.6.2", "2.6.2"));
        assert!(!is_newer_version("invalid", "2.6.2"));
    }

    #[test]
    fn classify_assets_ignores_sha256() {
        let assets = vec![
            ReleaseAsset {
                name: "PicoET-PC-Studio-Setup-v2.6.2.exe.sha256".into(),
                browser_download_url: "https://example.com/setup.sha256".into(),
            },
            ReleaseAsset {
                name: "PicoET-PC-Studio-Setup-v2.6.2.exe".into(),
                browser_download_url: "https://example.com/setup.exe".into(),
            },
            ReleaseAsset {
                name: "picoet-pc-studio-v2.6.2.zip".into(),
                browser_download_url: "https://example.com/portable.zip".into(),
            },
            ReleaseAsset {
                name: "magisk-picoet-enhance-v2.6.2.zip".into(),
                browser_download_url: "https://example.com/module.zip".into(),
            },
        ];
        let got = classify_assets(&assets);
        assert_eq!(got.installer_url, "https://example.com/setup.exe");
        assert_eq!(got.portable_url, "https://example.com/portable.zip");
        assert_eq!(got.module_url, "https://example.com/module.zip");
    }

    #[test]
    fn parse_version_rejects_empty_suffix_and_signed_digits() {
        assert!(parse_version("2.6.2-").is_none());
        assert!(parse_version("2.6.2-   ").is_none());
        assert!(parse_version("+2.6.2").is_none());
        assert!(parse_version("v+2.6.2").is_none());
        assert!(parse_version("2.+6.2").is_none());
        assert!(parse_version("2.6.+2").is_none());
        assert!(parse_version("-2.6.2").is_none());
        assert!(parse_version("2.6").is_none());
        assert!(parse_version("2.6.2.0").is_none());

        let parsed = parse_version("V2.6.2-rc.1").unwrap();
        assert_eq!(parsed.nums, (2, 6, 2));
        assert!(!parsed.is_stable);
        assert_eq!(parsed.suffix, "rc.1");
    }

    #[test]
    fn github_release_payload_accepts_null_fields() {
        let all_null = r#"{
            "tag_name": null,
            "name": null,
            "html_url": null,
            "published_at": null,
            "body": null,
            "assets": null
        }"#;
        let payload: GithubReleasePayload = serde_json::from_str(all_null).unwrap();
        assert_eq!(payload.tag_name, "");
        assert_eq!(payload.name, "");
        assert_eq!(payload.html_url, "");
        assert_eq!(payload.published_at, "");
        assert_eq!(payload.body, "");
        assert!(payload.assets.is_empty());

        let mixed = r#"{
            "tag_name": "v2.7.0",
            "name": null,
            "html_url": "https://github.com/WolalaQAQ/PicoET-Enhance/releases/tag/v2.7.0",
            "published_at": null,
            "body": null,
            "assets": [
                {"name": null, "browser_download_url": null},
                {"name": "PicoET-PC-Studio-Setup-v2.7.0.exe", "browser_download_url": "https://example.com/setup.exe"}
            ]
        }"#;
        let payload: GithubReleasePayload = serde_json::from_str(mixed).unwrap();
        assert_eq!(payload.tag_name, "v2.7.0");
        assert_eq!(payload.body, "");
        assert_eq!(payload.assets.len(), 2);
        let classified = classify_assets(&payload.assets);
        assert_eq!(classified.installer_url, "https://example.com/setup.exe");
    }
}
