use crate::stock::{read_capture_context, CaptureContext};
use crate::wire::connect_cancellable;
use serde::{Deserialize, Serialize};
use socket2::{Domain, Protocol, Socket, Type};
use std::collections::{BTreeMap, HashMap};
use std::io::{Read, Write};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, SocketAddrV4, UdpSocket};
use std::time::{Duration, Instant};

pub const DISCOVER_MSG: &[u8] = b"PICO_MOUTH_DISCOVER_V1\n";
pub const CAMERA_DISCOVER_MSG: &[u8] = b"PICO_CAMERA_DISCOVER_V1\n";
pub const DEFAULT_PORT: u16 = 45681;
pub const MULTICAST_ADDR: Ipv4Addr = Ipv4Addr::new(239, 255, 255, 250);

fn deserialize_u64_or_string<'de, D>(deserializer: D) -> Result<u64, D::Error>
where
    D: serde::Deserializer<'de>,
{
    struct U64OrStringVisitor;

    impl<'de> serde::de::Visitor<'de> for U64OrStringVisitor {
        type Value = u64;

        fn expecting(&self, formatter: &mut std::fmt::Formatter) -> std::fmt::Result {
            formatter.write_str("a u64 integer or decimal string")
        }

        fn visit_u64<E>(self, v: u64) -> Result<Self::Value, E>
        where
            E: serde::de::Error,
        {
            Ok(v)
        }

        fn visit_i64<E>(self, v: i64) -> Result<Self::Value, E>
        where
            E: serde::de::Error,
        {
            u64::try_from(v).map_err(|_| E::custom("negative integer not allowed"))
        }

        fn visit_str<E>(self, v: &str) -> Result<Self::Value, E>
        where
            E: serde::de::Error,
        {
            v.trim()
                .parse::<u64>()
                .map_err(|_| E::custom("invalid decimal u64 string"))
        }
    }

    deserializer.deserialize_any(U64OrStringVisitor)
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct CatalogStream {
    pub id: String,
    pub port: u16,
    pub path: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    #[serde(flatten, default)]
    pub extra: BTreeMap<String, serde_json::Value>,
}

#[derive(Debug, Clone, Deserialize, PartialEq)]
struct CameraCatalog {
    service: String,
    version: u32,
    #[serde(deserialize_with = "deserialize_u64_or_string")]
    session_id: u64,
    #[serde(deserialize_with = "deserialize_u64_or_string")]
    service_generation: u64,
    streams: Vec<CatalogStream>,
}

#[derive(Debug, Clone, Serialize)]
pub struct DiscoveredDevice {
    pub host: String,
    pub port: u16,
    pub name: String,
    pub session_id: String,
    pub service_generation: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub streams: Option<Vec<CatalogStream>>,
}

#[derive(Debug, Clone, Serialize)]
pub struct DiscoveryResult {
    pub devices: Vec<DiscoveredDevice>,
}

fn parse_catalog(data: &[u8]) -> Option<CameraCatalog> {
    let value: CameraCatalog = serde_json::from_slice(data).ok()?;
    if value.service != "pico-camera" || value.version != 1 {
        return None;
    }
    CaptureContext {
        capture_session_id: value.session_id,
        service_generation: value.service_generation,
    }
    .pack()
    .ok()?;
    let ids: Vec<&str> = value.streams.iter().map(|s| s.id.as_str()).collect();
    if ids != ["mouth", "eye_left", "eye_right"] {
        return None;
    }
    if value
        .streams
        .iter()
        .any(|s| s.port == 0 || s.path != "/stream.mjpg")
    {
        return None;
    }
    Some(value)
}

fn local_ipv4_interfaces() -> Vec<Ipv4Addr> {
    let mut found: Vec<Ipv4Addr> = if_addrs::get_if_addrs()
        .unwrap_or_default()
        .into_iter()
        .filter_map(|iface| match iface.addr.ip() {
            IpAddr::V4(v4) if !v4.is_loopback() && !v4.is_unspecified() => Some(v4),
            _ => None,
        })
        .collect();
    found.sort();
    found.dedup();
    found.push(Ipv4Addr::UNSPECIFIED);
    found
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum ChunkedStatus {
    Complete(Vec<u8>),
    Incomplete,
    Invalid,
}

pub(crate) fn decode_chunked_body(raw: &[u8], max_bytes: usize) -> ChunkedStatus {
    let mut out = Vec::new();
    let mut pos = 0usize;

    loop {
        let Some(rel_crlf) = raw[pos..].windows(2).position(|w| w == b"\r\n") else {
            return ChunkedStatus::Incomplete;
        };
        let line_bytes = &raw[pos..pos + rel_crlf];
        let Ok(line) = std::str::from_utf8(line_bytes) else {
            return ChunkedStatus::Invalid;
        };
        let size_hex = line.split(';').next().unwrap_or("").trim();
        if size_hex.is_empty() || !size_hex.bytes().all(|b| b.is_ascii_hexdigit()) {
            return ChunkedStatus::Invalid;
        }
        let Ok(chunk_len) = usize::from_str_radix(size_hex, 16) else {
            return ChunkedStatus::Invalid;
        };
        pos += rel_crlf + 2;

        if chunk_len == 0 {
            if raw[pos..].starts_with(b"\r\n") {
                return ChunkedStatus::Complete(out);
            }
            if raw[pos..].windows(4).any(|w| w == b"\r\n\r\n") {
                return ChunkedStatus::Complete(out);
            }
            return ChunkedStatus::Incomplete;
        }

        if out.len().saturating_add(chunk_len) > max_bytes {
            return ChunkedStatus::Invalid;
        }
        let Some(needed) = pos
            .checked_add(chunk_len)
            .and_then(|end| end.checked_add(2))
        else {
            return ChunkedStatus::Invalid;
        };
        if raw.len() < needed {
            return ChunkedStatus::Incomplete;
        }
        if &raw[pos + chunk_len..pos + chunk_len + 2] != b"\r\n" {
            return ChunkedStatus::Invalid;
        }
        out.extend_from_slice(&raw[pos..pos + chunk_len]);
        pos = needed;
    }
}

fn fetch_http_cameras_catalog(host: &str, port: u16, timeout: Duration) -> Option<CameraCatalog> {
    const MAX_HTTP_BYTES: usize = 16384;
    const MAX_BODY_BYTES: usize = 8192;

    let deadline = Instant::now() + timeout;
    let mut stream = connect_cancellable(host, port, timeout, None).ok()?;
    let remaining = deadline.saturating_duration_since(Instant::now());
    if remaining.is_zero() {
        return None;
    }
    let _ = stream.set_write_timeout(Some(remaining));
    let req = format!("GET /cameras HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n");
    stream.write_all(req.as_bytes()).ok()?;

    let mut raw = Vec::new();
    let mut buf = [0u8; 2048];
    let mut header_end: Option<usize> = None;
    let mut content_length: Option<usize> = None;
    let mut is_chunked = false;

    while raw.len() < MAX_HTTP_BYTES {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        let _ = stream.set_read_timeout(Some(remaining));
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                let take = n.min(MAX_HTTP_BYTES - raw.len());
                raw.extend_from_slice(&buf[..take]);
            }
            Err(err) if err.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        }

        if header_end.is_none() {
            if let Some(sep) = raw.windows(4).position(|w| w == b"\r\n\r\n") {
                let header_str = std::str::from_utf8(&raw[..sep]).ok()?;
                let mut lines = header_str.lines();
                let status_line = lines.next()?;
                let status_code: u16 = status_line.split_whitespace().nth(1)?.parse().ok()?;
                if status_code != 200 {
                    return None;
                }
                for line in lines {
                    let Some((name, value)) = line.split_once(':') else {
                        continue;
                    };
                    let name = name.trim();
                    let value = value.trim();
                    if name.eq_ignore_ascii_case("transfer-encoding") {
                        if value
                            .split(',')
                            .any(|tok| tok.trim().eq_ignore_ascii_case("chunked"))
                        {
                            is_chunked = true;
                        }
                    } else if name.eq_ignore_ascii_case("content-length") {
                        let parsed = value.parse::<usize>().ok()?;
                        if parsed > MAX_BODY_BYTES {
                            return None;
                        }
                        content_length = Some(parsed);
                    }
                }
                if is_chunked {
                    content_length = None;
                }
                header_end = Some(sep);
            }
        }

        if let Some(sep) = header_end {
            let body = &raw[sep + 4..];
            if is_chunked {
                match decode_chunked_body(body, MAX_BODY_BYTES) {
                    ChunkedStatus::Complete(decoded) => return parse_catalog(&decoded),
                    ChunkedStatus::Invalid => return None,
                    ChunkedStatus::Incomplete => {}
                }
            } else if let Some(cl) = content_length {
                if body.len() >= cl {
                    return parse_catalog(&body[..cl]);
                }
            } else if body.len() > MAX_BODY_BYTES {
                return None;
            }
        }
    }

    let sep = header_end?;
    let body = &raw[sep + 4..];
    if is_chunked {
        match decode_chunked_body(body, MAX_BODY_BYTES) {
            ChunkedStatus::Complete(decoded) => parse_catalog(&decoded),
            ChunkedStatus::Incomplete | ChunkedStatus::Invalid => None,
        }
    } else if let Some(cl) = content_length {
        if body.len() < cl {
            return None;
        }
        parse_catalog(&body[..cl])
    } else {
        if body.len() > MAX_BODY_BYTES {
            return None;
        }
        parse_catalog(body)
    }
}

pub fn discover_sources(
    timeout: Duration,
    explicit_targets: Option<Vec<(String, u16)>>,
) -> DiscoveryResult {
    let interfaces = if explicit_targets.is_some() {
        vec![Ipv4Addr::UNSPECIFIED]
    } else {
        local_ipv4_interfaces()
    };

    let targets: Vec<SocketAddr> = match explicit_targets {
        Some(list) => list
            .into_iter()
            .filter_map(|(h, p)| {
                use std::net::ToSocketAddrs;
                (h.as_str(), p).to_socket_addrs().ok()?.find(|a| a.is_ipv4())
            })
            .collect(),
        None => vec![
            SocketAddr::V4(SocketAddrV4::new(Ipv4Addr::BROADCAST, DEFAULT_PORT)),
            SocketAddr::V4(SocketAddrV4::new(MULTICAST_ADDR, DEFAULT_PORT)),
        ],
    };

    let mut sockets: Vec<UdpSocket> = Vec::new();
    for iface in interfaces {
        let Ok(sock) = Socket::new(Domain::IPV4, Type::DGRAM, Some(Protocol::UDP)) else {
            continue;
        };
        let _ = sock.set_broadcast(true);
        let _ = sock.set_multicast_ttl_v4(1);
        let bind_addr = SocketAddrV4::new(iface, 0);
        if sock.bind(&bind_addr.into()).is_err() {
            continue;
        }
        if !iface.is_unspecified() {
            let _ = sock.set_multicast_if_v4(&iface);
        }
        let udp: UdpSocket = sock.into();
        let _ = udp.set_nonblocking(true);
        let mut sent = 0usize;
        for target in &targets {
            if udp.send_to(DISCOVER_MSG, target).is_ok() {
                let _ = udp.send_to(CAMERA_DISCOVER_MSG, target);
                sent += 1;
            }
        }
        if sent > 0 {
            sockets.push(udp);
        }
    }

    let mut candidates: BTreeMap<(String, u16), CaptureContext> = BTreeMap::new();
    let mut catalogs: HashMap<(String, u16), CameraCatalog> = HashMap::new();

    let deadline = Instant::now() + timeout;
    let mut buf = [0u8; 8192];
    while !sockets.is_empty() && Instant::now() < deadline {
        let mut got_any = false;
        for sock in &sockets {
            loop {
                match sock.recv_from(&mut buf) {
                    Ok((n, endpoint)) => {
                        got_any = true;
                        let data = &buf[..n];
                        let key = (endpoint.ip().to_string(), endpoint.port());
                        if data.starts_with(b"{") {
                            if let Some(cat) = parse_catalog(data) {
                                catalogs.insert(key, cat);
                            }
                        } else if let Ok(ctx) = CaptureContext::unpack(data) {
                            candidates.insert(key, ctx);
                        }
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                    Err(_) => break,
                }
            }
        }
        if !got_any {
            let remain = deadline.saturating_duration_since(Instant::now());
            std::thread::sleep(remain.min(Duration::from_millis(15)));
        }
    }

    let mut verified_catalogs: HashMap<(String, u16), CameraCatalog> = HashMap::new();
    for ((host, port), advertised) in catalogs {
        if let Some(fetched) = fetch_http_cameras_catalog(&host, port, Duration::from_secs(1)) {
            if fetched == advertised {
                let mouth_port = advertised.streams[0].port;
                let key = (host, mouth_port);
                candidates.insert(
                    key.clone(),
                    CaptureContext {
                        capture_session_id: advertised.session_id,
                        service_generation: advertised.service_generation,
                    },
                );
                verified_catalogs.insert(key, advertised);
            }
        }
    }

    let mut devices = Vec::new();
    for ((host, port), advertised) in candidates {
        let Ok(mut sock) = connect_cancellable(&host, port, Duration::from_secs(1), None) else {
            continue;
        };
        let Ok(verified) = read_capture_context(&mut sock, Duration::from_secs(1), None) else {
            continue;
        };
        if verified != advertised {
            continue;
        }
        let mut device = DiscoveredDevice {
            host: host.clone(),
            port,
            name: "PICO Mouth".to_string(),
            session_id: verified.capture_session_id.to_string(),
            service_generation: verified.service_generation.to_string(),
            streams: None,
        };
        if let Some(cat) = verified_catalogs.get(&(host.clone(), port)) {
            device.name = "PICO Cameras".to_string();
            device.streams = Some(
                cat.streams
                    .iter()
                    .map(|s| {
                        let mut cloned = s.clone();
                        cloned.url = Some(format!("http://{host}:{}{}", s.port, s.path));
                        cloned
                    })
                    .collect(),
            );
        }
        devices.push(device);
    }

    DiscoveryResult { devices }
}

pub fn discover_cameras(
    timeout: Duration,
    explicit_targets: Option<Vec<(String, u16)>>,
) -> DiscoveryResult {
    let all = discover_sources(timeout, explicit_targets);
    DiscoveryResult {
        devices: all
            .devices
            .into_iter()
            .filter(|d| d.streams.is_some())
            .collect(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_catalog_validates_schema() {
        let valid = br#"{
            "service":"pico-camera","version":1,
            "session_id":10,"service_generation":20,
            "streams":[
                {"id":"mouth","port":45681,"path":"/stream.mjpg"},
                {"id":"eye_left","port":45682,"path":"/stream.mjpg"},
                {"id":"eye_right","port":45683,"path":"/stream.mjpg"}
            ]
        }"#;
        assert!(parse_catalog(valid).is_some());

        let bad_order = br#"{
            "service":"pico-camera","version":1,
            "session_id":10,"service_generation":20,
            "streams":[
                {"id":"eye_left","port":45682,"path":"/stream.mjpg"},
                {"id":"mouth","port":45681,"path":"/stream.mjpg"},
                {"id":"eye_right","port":45683,"path":"/stream.mjpg"}
            ]
        }"#;
        assert!(parse_catalog(bad_order).is_none());
    }

    #[test]
    fn parse_catalog_accepts_core_cpp_string_ids_and_preserves_extra_stream_fields() {
        let core_cpp_payload = br#"{"service":"pico-camera","version":1,"session_id":"18446744073709551000","service_generation":"789","timestamp_semantics":"camera5_parent_sensor_ns","independent_eye_exposures":false,"protocols":["ptj1","http-mjpeg"],"max_clients_per_stream":8,"streams":[{"id":"mouth","source_lane":2,"port":45681,"path":"/stream.mjpg","width":400,"height":400,"pixel_format":"gray8","encoding":"jpeg","quality":95},{"id":"eye_left","source_lane":0,"port":45682,"path":"/stream.mjpg","width":400,"height":400,"pixel_format":"gray8","encoding":"jpeg","quality":95},{"id":"eye_right","source_lane":1,"port":45683,"path":"/stream.mjpg","width":400,"height":400,"pixel_format":"gray8","encoding":"jpeg","quality":95}]}"#;
        let parsed = parse_catalog(core_cpp_payload).expect("core.cpp catalog must parse");
        assert_eq!(parsed.session_id, 18446744073709551000);
        assert_eq!(parsed.service_generation, 789);
        assert_eq!(parsed.streams.len(), 3);
        assert_eq!(parsed.streams[0].extra["source_lane"], 2);
        assert_eq!(parsed.streams[0].extra["width"], 400);
        assert_eq!(parsed.streams[0].extra["height"], 400);
        assert_eq!(parsed.streams[0].extra["pixel_format"], "gray8");
        assert_eq!(parsed.streams[1].extra["source_lane"], 0);
        assert_eq!(parsed.streams[2].extra["source_lane"], 1);

        // Verify extra fields survive serialization when url is attached.
        let mut with_url = parsed.streams[0].clone();
        with_url.url = Some("http://198.51.100.2:45681/stream.mjpg".to_string());
        let serialized = serde_json::to_value(&with_url).unwrap();
        assert_eq!(serialized["id"], "mouth");
        assert_eq!(serialized["port"], 45681);
        assert_eq!(serialized["source_lane"], 2);
        assert_eq!(serialized["pixel_format"], "gray8");
        assert_eq!(serialized["url"], "http://198.51.100.2:45681/stream.mjpg");

        // Zero or non-numeric string session_id must still be rejected.
        let zero_str = br#"{"service":"pico-camera","version":1,"session_id":"0","service_generation":"789","streams":[{"id":"mouth","port":45681,"path":"/stream.mjpg"},{"id":"eye_left","port":45682,"path":"/stream.mjpg"},{"id":"eye_right","port":45683,"path":"/stream.mjpg"}]}"#;
        assert!(parse_catalog(zero_str).is_none());

        let bad_str = br#"{"service":"pico-camera","version":1,"session_id":"not-a-num","service_generation":"789","streams":[{"id":"mouth","port":45681,"path":"/stream.mjpg"},{"id":"eye_left","port":45682,"path":"/stream.mjpg"},{"id":"eye_right","port":45683,"path":"/stream.mjpg"}]}"#;
        assert!(parse_catalog(bad_str).is_none());
    }

    #[test]
    fn fetch_http_catalog_honors_content_length_without_waiting_for_close() {
        use std::net::TcpListener;
        use std::thread;

        let body = r#"{"service":"pico-camera","version":1,"session_id":10,"service_generation":20,"streams":[{"id":"mouth","port":45681,"path":"/stream.mjpg"},{"id":"eye_left","port":45682,"path":"/stream.mjpg"},{"id":"eye_right","port":45683,"path":"/stream.mjpg"}]}"#;
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let resp = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
            body.len(),
            body
        );
        let srv = thread::spawn(move || {
            let (mut conn, _) = listener.accept().unwrap();
            let mut req_buf = [0u8; 512];
            let _ = conn.read(&mut req_buf);
            let _ = conn.write_all(resp.as_bytes());
            // Intentionally keep socket open to verify client stops as soon as Content-Length is met.
            thread::sleep(Duration::from_millis(800));
        });

        let t0 = Instant::now();
        let cat = fetch_http_cameras_catalog("127.0.0.1", port, Duration::from_secs(2))
            .expect("catalog should parse");
        assert!(
            t0.elapsed() < Duration::from_millis(500),
            "should not stall waiting for EOF when Content-Length is satisfied"
        );
        assert_eq!(cat.session_id, 10);
        assert_eq!(cat.service_generation, 20);
        let _ = srv.join();
    }

    #[test]
    fn fetch_http_catalog_decodes_chunked_transfer_encoding() {
        use std::net::TcpListener;
        use std::thread;

        let body = br#"{"service":"pico-camera","version":1,"session_id":"100","service_generation":"200","streams":[{"id":"mouth","port":45681,"path":"/stream.mjpg"},{"id":"eye_left","port":45682,"path":"/stream.mjpg"},{"id":"eye_right","port":45683,"path":"/stream.mjpg"}]}"#;
        let split = body.len() / 2;
        let mut resp = Vec::new();
        resp.extend_from_slice(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n",
        );
        resp.extend_from_slice(format!("{:x};ext=1\r\n", split).as_bytes());
        resp.extend_from_slice(&body[..split]);
        resp.extend_from_slice(b"\r\n");
        resp.extend_from_slice(format!("{:x}\r\n", body.len() - split).as_bytes());
        resp.extend_from_slice(&body[split..]);
        resp.extend_from_slice(b"\r\n0\r\n\r\n");

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let srv = thread::spawn(move || {
            let (mut conn, _) = listener.accept().unwrap();
            let mut req_buf = [0u8; 512];
            let _ = conn.read(&mut req_buf);
            for part in resp.chunks(17) {
                let _ = conn.write_all(part);
            }
            thread::sleep(Duration::from_millis(800));
        });

        let t0 = Instant::now();
        let cat = fetch_http_cameras_catalog("127.0.0.1", port, Duration::from_secs(2))
            .expect("chunked catalog should parse");
        assert!(
            t0.elapsed() < Duration::from_millis(500),
            "should return immediately after terminal zero chunk"
        );
        assert_eq!(cat.session_id, 100);
        assert_eq!(cat.service_generation, 200);
        let _ = srv.join();
    }

    #[test]
    fn decode_chunked_body_rejects_overflow_and_malformed_chunks() {
        assert_eq!(
            decode_chunked_body(b"ffffffffffffffff\r\n", 64 * 1024),
            ChunkedStatus::Invalid
        );
        assert_eq!(
            decode_chunked_body(b"ffffffffffffffff\r\n", usize::MAX),
            ChunkedStatus::Invalid
        );
        assert_eq!(
            decode_chunked_body(b"+1\r\na\r\n0\r\n\r\n", 64 * 1024),
            ChunkedStatus::Invalid
        );
        assert_eq!(
            decode_chunked_body(b"1\r\naXX0\r\n\r\n", 64 * 1024),
            ChunkedStatus::Invalid
        );
        assert_eq!(
            decode_chunked_body(b"5;ext=1\r\nhello\r\n0\r\nX-Trailer: 1\r\n\r\n", 64 * 1024),
            ChunkedStatus::Complete(b"hello".to_vec())
        );
    }
}
