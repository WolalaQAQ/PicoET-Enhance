use serde::{Deserialize, Serialize};
use std::io::Read;
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const APP_VERSION: &str = env!("PICOET_STUDIO_VERSION");
pub const GITHUB_REPO: &str = "WolalaQAQ/PicoET-Enhance";
pub const RELEASES_LATEST_API: &str =
    "https://api.github.com/repos/WolalaQAQ/PicoET-Enhance/releases/latest";
pub const RELEASES_PAGE_URL: &str = "https://github.com/WolalaQAQ/PicoET-Enhance/releases";
const MAX_RELEASE_BODY_BYTES: u64 = 2 * 1024 * 1024;

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
    let without_build = match stripped.split_once('+') {
        Some((before, build)) => {
            if build.trim().is_empty() {
                return None;
            }
            before
        }
        None => stripped,
    };
    let (core, suffix) = match without_build.split_once('-') {
        Some((c, s)) => {
            let trimmed_suffix = s.trim();
            if trimmed_suffix.is_empty() {
                return None;
            }
            (c, trimmed_suffix.to_string())
        }
        None => (without_build, String::new()),
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

fn parse_numeric_ident(part: &str) -> Option<u64> {
    if !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit()) {
        part.parse::<u64>().ok()
    } else {
        None
    }
}

fn compare_prerelease(a: &str, b: &str) -> std::cmp::Ordering {
    use std::cmp::Ordering;
    let mut a_parts = a.split('.');
    let mut b_parts = b.split('.');
    loop {
        match (a_parts.next(), b_parts.next()) {
            (Some(ap), Some(bp)) => {
                let ord = match (parse_numeric_ident(ap), parse_numeric_ident(bp)) {
                    (Some(an), Some(bn)) => an.cmp(&bn),
                    (Some(_), None) => Ordering::Less,
                    (None, Some(_)) => Ordering::Greater,
                    (None, None) => ap.cmp(bp),
                };
                if ord != Ordering::Equal {
                    return ord;
                }
            }
            (Some(_), None) => return Ordering::Greater,
            (None, Some(_)) => return Ordering::Less,
            (None, None) => return Ordering::Equal,
        }
    }
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
    compare_prerelease(&cand.suffix, &curr.suffix) == std::cmp::Ordering::Greater
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

fn validate_https_url(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    if trimmed.is_empty()
        || trimmed
            .chars()
            .any(|c| c.is_control() || c.is_whitespace() || c == '"' || c == '\\')
    {
        return None;
    }
    if !trimmed
        .as_bytes()
        .get(..8)
        .is_some_and(|prefix| prefix.eq_ignore_ascii_case(b"https://"))
        || trimmed.as_bytes().get(8) == Some(&b'/')
    {
        return None;
    }
    let parsed = reqwest::Url::parse(trimmed).ok()?;
    if parsed.scheme() != "https" || !parsed.has_host() {
        return None;
    }
    Some(trimmed.to_string())
}

fn read_capped_bytes<R: Read>(reader: R, max_bytes: u64) -> Result<Vec<u8>, String> {
    let mut limited = reader.take(max_bytes.saturating_add(1));
    let mut buf = Vec::new();
    limited.read_to_end(&mut buf).map_err(|e| e.to_string())?;
    if (buf.len() as u64) > max_bytes {
        return Err(format!("Response body exceeds {} bytes limit", max_bytes));
    }
    Ok(buf)
}

pub fn classify_assets(assets: &[ReleaseAsset]) -> ClassifiedAssets {
    let mut result = ClassifiedAssets::default();
    for item in assets {
        let name = item.name.trim();
        let lower = name.to_ascii_lowercase();
        if name.is_empty() || lower.ends_with(".sha256") {
            continue;
        }
        let Some(url) = validate_https_url(&item.browser_download_url) else {
            continue;
        };
        if result.installer_url.is_empty()
            && lower.contains("pc-studio")
            && lower.ends_with(".exe")
        {
            result.installer_name = name.to_string();
            result.installer_url = url;
        } else if result.portable_url.is_empty()
            && lower.contains("pc-studio")
            && lower.ends_with(".zip")
        {
            result.portable_name = name.to_string();
            result.portable_url = url;
        } else if result.module_url.is_empty()
            && lower.contains("magisk-picoet-enhance")
            && lower.ends_with(".zip")
        {
            result.module_name = name.to_string();
            result.module_url = url;
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
                            let parsed = read_capped_bytes(resp, MAX_RELEASE_BODY_BYTES).and_then(
                                |bytes| {
                                    serde_json::from_slice::<GithubReleasePayload>(&bytes)
                                        .map_err(|e| e.to_string())
                                },
                            );
                            match parsed {
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
                                    if let Some(safe_release_url) =
                                        validate_https_url(&payload.html_url)
                                    {
                                        res.release_url = safe_release_url;
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
                                    res.error = Some(err);
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
        // Numeric prerelease identifiers compare numerically, not lexicographically.
        assert!(is_newer_version("2.6.2-rc.10", "2.6.2-rc.2"));
        assert!(!is_newer_version("2.6.2-rc.2", "2.6.2-rc.10"));
        assert!(is_newer_version("2.6.2-dev.10", "2.6.2-dev.2"));
        assert!(!is_newer_version("2.6.2-rc.10", "2.6.2-rc.10"));
        assert!(is_newer_version("2.6.2-rc.1", "2.6.2-beta.9"));
        assert!(is_newer_version("2.6.2-alpha.1", "2.6.2-alpha"));
        assert!(is_newer_version("2.6.2-alpha.beta", "2.6.2-alpha.1"));
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

    #[test]
    fn parse_version_strips_semver_build_metadata() {
        let stable = parse_version("2.7.0+build.1").unwrap();
        assert_eq!(stable.nums, (2, 7, 0));
        assert!(stable.is_stable);
        assert_eq!(stable.suffix, "");

        let pre = parse_version("v2.7.0-rc.1+sha.abc123").unwrap();
        assert_eq!(pre.nums, (2, 7, 0));
        assert!(!pre.is_stable);
        assert_eq!(pre.suffix, "rc.1");

        assert!(is_newer_version("2.7.0+build.1", "2.6.9"));
        assert!(!is_newer_version("2.7.0+build.2", "2.7.0+build.1"));
        assert!(!is_newer_version("2.7.0-rc.1+build.1", "2.7.0-rc.1"));
        assert!(!is_newer_version("2.7.0-rc.1", "2.7.0-rc.1+build.1"));
        assert!(is_newer_version("2.7.0+build.1", "2.7.0-rc.1+build.9"));

        assert!(parse_version("2.7.0+").is_none());
        assert!(parse_version("2.7.0-rc.1+   ").is_none());
        assert!(parse_version("2.7.0-+build.1").is_none());
    }

    #[test]
    fn updater_enforces_https_urls_and_body_size_cap() {
        assert_eq!(
            validate_https_url("https://github.com/WolalaQAQ/PicoET-Enhance/releases/tag/v2.7.0")
                .as_deref(),
            Some("https://github.com/WolalaQAQ/PicoET-Enhance/releases/tag/v2.7.0")
        );
        assert!(validate_https_url("http://example.com/setup.exe").is_none());
        assert!(validate_https_url("javascript:alert(1)").is_none());
        assert!(validate_https_url("file:///C:/Windows/win.ini").is_none());
        assert!(validate_https_url("https://").is_none());
        assert!(validate_https_url("https:///foo").is_none());
        assert!(validate_https_url("https://example.com/a b").is_none());

        let assets = vec![
            ReleaseAsset {
                name: "PicoET-PC-Studio-Setup-v2.7.0.exe".into(),
                browser_download_url: "http://insecure.example.com/setup.exe".into(),
            },
            ReleaseAsset {
                name: "PicoET-PC-Studio-Setup-v2.7.0-good.exe".into(),
                browser_download_url: "https://example.com/setup.exe".into(),
            },
            ReleaseAsset {
                name: "picoet-pc-studio-v2.7.0.zip".into(),
                browser_download_url: "javascript:alert(1)".into(),
            },
        ];
        let got = classify_assets(&assets);
        assert_eq!(got.installer_name, "PicoET-PC-Studio-Setup-v2.7.0-good.exe");
        assert_eq!(got.installer_url, "https://example.com/setup.exe");
        assert_eq!(got.portable_url, "");

        let exact = vec![b'a'; 64];
        assert_eq!(read_capped_bytes(&exact[..], 64).unwrap().len(), 64);
        let over = vec![b'a'; 65];
        assert!(read_capped_bytes(&over[..], 64).is_err());
    }

    #[test]
    fn update_checker_rejects_oversized_response_and_unsafe_html_url() {
        use std::io::Write;
        use std::net::TcpListener;
        use std::thread;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let srv = thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let mut req = [0u8; 512];
                let _ = stream.read(&mut req);
                let body = r#"{"tag_name":"v2.8.0","html_url":"javascript:alert(1)","assets":[{"name":"PicoET-PC-Studio-Setup-v2.8.0.exe","browser_download_url":"http://evil.example/setup.exe"}]}"#;
                let resp = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                let _ = stream.write_all(resp.as_bytes());
            }
            if let Ok((mut stream, _)) = listener.accept() {
                let mut req = [0u8; 512];
                let _ = stream.read(&mut req);
                let big_len = (MAX_RELEASE_BODY_BYTES as usize) + 128;
                let header = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {big_len}\r\nConnection: close\r\n\r\n"
                );
                let _ = stream.write_all(header.as_bytes());
                let chunk = vec![b' '; 65536];
                let mut sent = 0usize;
                while sent < big_len {
                    let take = (big_len - sent).min(chunk.len());
                    if stream.write_all(&chunk[..take]).is_err() {
                        break;
                    }
                    sent += take;
                }
            }
        });

        let checker = UpdateChecker::with_config(
            "2.7.0".to_string(),
            format!("http://127.0.0.1:{port}/latest"),
            Duration::from_secs(0),
        );
        let first = checker.check(true);
        assert!(first.error.is_none());
        assert_eq!(first.release_url, RELEASES_PAGE_URL);
        assert_eq!(first.installer_url, "");
        assert!(first.update_available);

        let second = checker.check(true);
        assert!(
            second.error.as_deref().is_some_and(|e| e.contains("exceeds")),
            "expected oversized body error, got {:?}",
            second.error
        );

        let _ = srv.join();
    }
}
