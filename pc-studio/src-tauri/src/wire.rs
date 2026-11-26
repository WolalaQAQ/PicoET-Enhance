use serde::Serialize;
use socket2::{Domain, Protocol, Socket, TcpKeepalive, Type};
use std::collections::HashSet;
use std::fmt;
use std::io::{self, Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

pub const HEADER_SIZE: usize = 128;
pub const JPEG: u32 = 1;
pub const RAW: u32 = 2;
pub const END: u32 = 4;
pub const REPLAY: u32 = 8;

static MONO_EPOCH: OnceLock<Instant> = OnceLock::new();

pub fn monotonic_ns() -> i64 {
    let epoch = MONO_EPOCH.get_or_init(Instant::now);
    Instant::now().duration_since(*epoch).as_nanos() as i64
}

#[derive(Debug)]
pub enum WireError {
    IdleTimeout(String),
    Eof(String),
    Invalid(String),
    Io(io::Error),
    Cancelled,
}

impl fmt::Display for WireError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            WireError::IdleTimeout(msg)
            | WireError::Eof(msg)
            | WireError::Invalid(msg) => write!(f, "{msg}"),
            WireError::Io(err) => write!(f, "{err}"),
            WireError::Cancelled => write!(f, "Connection cancelled"),
        }
    }
}

impl std::error::Error for WireError {}

impl From<io::Error> for WireError {
    fn from(value: io::Error) -> Self {
        WireError::Io(value)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrameHeader {
    pub payload_bytes: u32,
    pub flags: u32,
    pub session_id: u64,
    pub source_seq: u64,
    pub sensor_ns: u64,
    pub vendor_ns: u64,
    pub copy_mono_ns: u64,
    pub encode_begin_ns: u64,
    pub encode_end_ns: u64,
    pub emit_mono_ns: u64,
    pub eye0_sensor_ns: u64,
    pub eye1_sensor_ns: u64,
    pub mouth_sensor_ns: u64,
    pub raw_identity: u64,
    pub width: u32,
    pub height: u32,
    pub raw_crc32: u32,
    pub quality: u32,
}

#[derive(Debug, Clone, Serialize)]
pub struct FrameHeaderIdentityJson {
    pub payload_bytes: u32,
    pub flags: u32,
    pub session_id: String,
    pub source_seq: String,
    pub sensor_ns: String,
    pub vendor_ns: String,
    pub copy_mono_ns: String,
    pub encode_begin_ns: String,
    pub encode_end_ns: String,
    pub emit_mono_ns: String,
    pub eye0_sensor_ns: String,
    pub eye1_sensor_ns: String,
    pub mouth_sensor_ns: String,
    pub raw_identity: String,
    pub width: u32,
    pub height: u32,
    pub raw_crc32: u32,
    pub quality: u32,
}

impl FrameHeader {
    pub fn pack(&self) -> [u8; HEADER_SIZE] {
        let mut out = [0u8; HEADER_SIZE];
        out[0..4].copy_from_slice(b"PTJ1");
        out[4..6].copy_from_slice(&1u16.to_le_bytes());
        out[6..8].copy_from_slice(&(HEADER_SIZE as u16).to_le_bytes());
        out[8..12].copy_from_slice(&self.payload_bytes.to_le_bytes());
        out[12..16].copy_from_slice(&self.flags.to_le_bytes());

        let u64s = [
            self.session_id,
            self.source_seq,
            self.sensor_ns,
            self.vendor_ns,
            self.copy_mono_ns,
            self.encode_begin_ns,
            self.encode_end_ns,
            self.emit_mono_ns,
            self.eye0_sensor_ns,
            self.eye1_sensor_ns,
            self.mouth_sensor_ns,
            self.raw_identity,
        ];
        for (i, v) in u64s.iter().enumerate() {
            let off = 16 + i * 8;
            out[off..off + 8].copy_from_slice(&v.to_le_bytes());
        }
        let u32s = [self.width, self.height, self.raw_crc32, self.quality];
        for (i, v) in u32s.iter().enumerate() {
            let off = 112 + i * 4;
            out[off..off + 4].copy_from_slice(&v.to_le_bytes());
        }
        out
    }

    pub fn unpack(data: &[u8]) -> Result<Self, WireError> {
        if data.len() != HEADER_SIZE {
            return Err(WireError::Invalid("Unsupported PTJ1 frame header".into()));
        }
        let magic = &data[0..4];
        let version = u16::from_le_bytes([data[4], data[5]]);
        let size = u16::from_le_bytes([data[6], data[7]]);
        if magic != b"PTJ1" || version != 1 || size != HEADER_SIZE as u16 {
            return Err(WireError::Invalid("Unsupported PTJ1 frame header".into()));
        }

        let u32_at = |off: usize| -> u32 {
            u32::from_le_bytes([data[off], data[off + 1], data[off + 2], data[off + 3]])
        };
        let u64_at = |off: usize| -> u64 {
            u64::from_le_bytes([
                data[off],
                data[off + 1],
                data[off + 2],
                data[off + 3],
                data[off + 4],
                data[off + 5],
                data[off + 6],
                data[off + 7],
            ])
        };

        let result = Self {
            payload_bytes: u32_at(8),
            flags: u32_at(12),
            session_id: u64_at(16),
            source_seq: u64_at(24),
            sensor_ns: u64_at(32),
            vendor_ns: u64_at(40),
            copy_mono_ns: u64_at(48),
            encode_begin_ns: u64_at(56),
            encode_end_ns: u64_at(64),
            emit_mono_ns: u64_at(72),
            eye0_sensor_ns: u64_at(80),
            eye1_sensor_ns: u64_at(88),
            mouth_sensor_ns: u64_at(96),
            raw_identity: u64_at(104),
            width: u32_at(112),
            height: u32_at(116),
            raw_crc32: u32_at(120),
            quality: u32_at(124),
        };

        if (result.flags & END) != 0 {
            if result.payload_bytes != 0 {
                return Err(WireError::Invalid(
                    "END must not contain an image payload".into(),
                ));
            }
        } else {
            let kind = result.flags & (JPEG | RAW);
            if kind != JPEG && kind != RAW {
                return Err(WireError::Invalid(
                    "Expected one JPEG or RAW payload type".into(),
                ));
            }
            if (result.width, result.height, result.quality) != (400, 400, 95) {
                return Err(WireError::Invalid(
                    "Expected 400x400 JPEG95 source".into(),
                ));
            }
            if result.payload_bytes == 0 || result.payload_bytes > 2_000_000 {
                return Err(WireError::Invalid("Invalid image payload length".into()));
            }
        }

        Ok(result)
    }

    pub fn to_identity_json(&self) -> FrameHeaderIdentityJson {
        FrameHeaderIdentityJson {
            payload_bytes: self.payload_bytes,
            flags: self.flags,
            session_id: self.session_id.to_string(),
            source_seq: self.source_seq.to_string(),
            sensor_ns: self.sensor_ns.to_string(),
            vendor_ns: self.vendor_ns.to_string(),
            copy_mono_ns: self.copy_mono_ns.to_string(),
            encode_begin_ns: self.encode_begin_ns.to_string(),
            encode_end_ns: self.encode_end_ns.to_string(),
            emit_mono_ns: self.emit_mono_ns.to_string(),
            eye0_sensor_ns: self.eye0_sensor_ns.to_string(),
            eye1_sensor_ns: self.eye1_sensor_ns.to_string(),
            mouth_sensor_ns: self.mouth_sensor_ns.to_string(),
            raw_identity: self.raw_identity.to_string(),
            width: self.width,
            height: self.height,
            raw_crc32: self.raw_crc32,
            quality: self.quality,
        }
    }
}

pub fn enable_keepalive(stream: &TcpStream, idle_ms: u64, interval_ms: u64) {
    let sock_ref = socket2::SockRef::from(stream);
    let _ = sock_ref.set_keepalive(true);
    let keepalive = TcpKeepalive::new()
        .with_time(Duration::from_millis(idle_ms))
        .with_interval(Duration::from_millis(interval_ms));
    let _ = sock_ref.set_tcp_keepalive(&keepalive);
}

/// Wait for a TCP stream to become readable (or error/hangup) up to `timeout`,
/// waking periodically to check `stop_flag` so stop() returns promptly.
/// Distinguishes genuine idle poll timeouts (`Ok(false)`) from OS keepalive/reset errors
/// (which make the socket readable/error so subsequent `read` surfaces the exact OS error).
#[cfg(windows)]
pub fn poll_readable(
    stream: &TcpStream,
    timeout: Duration,
    stop_flag: Option<&AtomicBool>,
) -> Result<bool, WireError> {
    use std::os::windows::io::AsRawSocket;
    use windows_sys::Win32::Networking::WinSock::{
        WSAPoll, POLLERR, POLLHUP, POLLRDNORM, SOCKET, WSAPOLLFD,
    };

    let raw = stream.as_raw_socket() as SOCKET;
    let deadline = Instant::now() + timeout;
    loop {
        if stop_flag.is_some_and(|s| s.load(Ordering::Relaxed)) {
            return Err(WireError::Cancelled);
        }
        let now = Instant::now();
        if now >= deadline {
            return Ok(false);
        }
        let slice_ms = (deadline - now).as_millis().min(100) as i32;
        let mut pfd = WSAPOLLFD {
            fd: raw,
            events: POLLRDNORM as i16,
            revents: 0,
        };
        let rc = unsafe { WSAPoll(&mut pfd, 1, slice_ms) };
        if rc > 0 {
            if (pfd.revents & (POLLRDNORM as i16 | POLLERR as i16 | POLLHUP as i16)) != 0 {
                return Ok(true);
            }
            return Ok(true);
        } else if rc < 0 {
            return Err(WireError::Io(io::Error::last_os_error()));
        }
    }
}

#[cfg(not(windows))]
pub fn poll_readable(
    stream: &TcpStream,
    timeout: Duration,
    stop_flag: Option<&AtomicBool>,
) -> Result<bool, WireError> {
    if stop_flag.is_some_and(|s| s.load(Ordering::Relaxed)) {
        return Err(WireError::Cancelled);
    }
    let _ = stream.set_read_timeout(Some(timeout));
    Ok(true)
}

pub fn read_exact_stream(
    stream: &mut TcpStream,
    size: usize,
    timeout: Duration,
    part: Option<&str>,
    stop_flag: Option<&AtomicBool>,
) -> Result<Vec<u8>, WireError> {
    let mut buf = vec![0u8; size];
    let mut filled = 0usize;
    let deadline = Instant::now() + timeout;

    while filled < size {
        if stop_flag.is_some_and(|s| s.load(Ordering::Relaxed)) {
            return Err(WireError::Cancelled);
        }
        let now = Instant::now();
        let remaining = deadline.saturating_duration_since(now);
        if remaining.is_zero() {
            if part == Some("header") && filled == 0 {
                return Err(WireError::IdleTimeout(
                    "Waiting for next PTJ1 frame".into(),
                ));
            }
            return Err(WireError::Io(io::Error::new(
                io::ErrorKind::TimedOut,
                "timed out",
            )));
        }

        let ready = poll_readable(stream, remaining, stop_flag)?;
        if !ready {
            if part == Some("header") && filled == 0 {
                return Err(WireError::IdleTimeout(
                    "Waiting for next PTJ1 frame".into(),
                ));
            }
            return Err(WireError::Io(io::Error::new(
                io::ErrorKind::TimedOut,
                "timed out",
            )));
        }

        // Socket is ready (or errored/EOF); perform read without SO_RCVTIMEO ambiguity.
        let _ = stream.set_read_timeout(Some(remaining.max(Duration::from_millis(50))));
        match stream.read(&mut buf[filled..]) {
            Ok(0) => {
                if part == Some("header") && filled == 0 {
                    return Err(WireError::Eof("PTJ1 stream closed between frames".into()));
                }
                if let Some(p) = part {
                    return Err(WireError::Eof(format!(
                        "PTJ1 stream closed during {p}: received {filled}/{size} bytes"
                    )));
                }
                return Err(WireError::Eof(
                    "PTJ1 connection ended within a frame".into(),
                ));
            }
            Ok(n) => {
                filled += n;
            }
            Err(err) if err.kind() == io::ErrorKind::Interrupted => continue,
            Err(err) => return Err(WireError::Io(err)),
        }
    }

    Ok(buf)
}

pub fn read_frame(
    stream: &mut TcpStream,
    timeout: Duration,
    stop_flag: Option<&AtomicBool>,
) -> Result<(FrameHeader, Vec<u8>), WireError> {
    let header_bytes = read_exact_stream(stream, HEADER_SIZE, timeout, Some("header"), stop_flag)?;
    let header = FrameHeader::unpack(&header_bytes)?;
    let payload = if header.payload_bytes == 0 {
        Vec::new()
    } else {
        read_exact_stream(
            stream,
            header.payload_bytes as usize,
            timeout.max(Duration::from_secs(2)),
            Some("payload"),
            stop_flag,
        )?
    };
    Ok((header, payload))
}

#[derive(Debug, Default)]
pub struct SourceOrder {
    pub session: Option<u64>,
    pub last_seq: Option<u64>,
    pub last_mouth_ns: Option<u64>,
    retired: HashSet<u64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AcceptOutcome {
    NewSession,
    NewSource,
    RetiredSession,
    Duplicate,
    OutOfOrder,
    NonAdvancingTime,
}

impl SourceOrder {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn accept(&mut self, header: &FrameHeader) -> (bool, AcceptOutcome, u64) {
        if self.retired.contains(&header.session_id) {
            return (false, AcceptOutcome::RetiredSession, 0);
        }
        let changed = self.session != Some(header.session_id);
        if changed {
            if let Some(prev) = self.session {
                self.retired.insert(prev);
            }
            self.session = Some(header.session_id);
            self.last_seq = None;
            self.last_mouth_ns = None;
        }
        if let Some(last_seq) = self.last_seq {
            if header.source_seq <= last_seq {
                let reason = if header.source_seq == last_seq {
                    AcceptOutcome::Duplicate
                } else {
                    AcceptOutcome::OutOfOrder
                };
                return (false, reason, 0);
            }
        }
        if let Some(last_ns) = self.last_mouth_ns {
            if header.mouth_sensor_ns <= last_ns {
                return (false, AcceptOutcome::NonAdvancingTime, 0);
            }
        }
        let gap = match self.last_seq {
            None => 0,
            Some(prev) => header.source_seq.saturating_sub(prev).saturating_sub(1),
        };
        self.last_seq = Some(header.source_seq);
        self.last_mouth_ns = Some(header.mouth_sensor_ns);
        (
            true,
            if changed {
                AcceptOutcome::NewSession
            } else {
                AcceptOutcome::NewSource
            },
            gap,
        )
    }
}

/// Connect to `host:port` with a deadline while polling `stop_flag` so unreachable hosts
/// can be cancelled within ~50ms.
pub fn connect_cancellable(
    host: &str,
    port: u16,
    timeout: Duration,
    stop_flag: Option<&AtomicBool>,
) -> Result<TcpStream, WireError> {
    if stop_flag.is_some_and(|s| s.load(Ordering::Relaxed)) {
        return Err(WireError::Cancelled);
    }
    let addrs: Vec<SocketAddr> = (host, port).to_socket_addrs()?.collect();
    if addrs.is_empty() {
        return Err(WireError::Io(io::Error::new(
            io::ErrorKind::AddrNotAvailable,
            format!("Unable to connect to {host}:{port}"),
        )));
    }
    let mut last_err: Option<io::Error> = None;
    for addr in addrs {
        if stop_flag.is_some_and(|s| s.load(Ordering::Relaxed)) {
            return Err(WireError::Cancelled);
        }
        match connect_one_cancellable(addr, timeout, stop_flag) {
            Ok(stream) => return Ok(stream),
            Err(WireError::Cancelled) => return Err(WireError::Cancelled),
            Err(WireError::Io(err)) => last_err = Some(err),
            Err(other) => return Err(other),
        }
    }
    Err(WireError::Io(last_err.unwrap_or_else(|| {
        io::Error::other(format!("Unable to connect to {host}:{port}"))
    })))
}

fn connect_one_cancellable(
    addr: SocketAddr,
    timeout: Duration,
    stop_flag: Option<&AtomicBool>,
) -> Result<TcpStream, WireError> {
    let domain = if addr.is_ipv4() {
        Domain::IPV4
    } else {
        Domain::IPV6
    };
    let sock = Socket::new(domain, Type::STREAM, Some(Protocol::TCP))?;
    sock.set_nonblocking(true)?;

    match sock.connect(&addr.into()) {
        Ok(()) => {
            sock.set_nonblocking(false)?;
            return Ok(TcpStream::from(sock));
        }
        Err(err) => {
            let in_progress = err.kind() == io::ErrorKind::WouldBlock
                || matches!(err.raw_os_error(), Some(10035) | Some(10036) | Some(115));
            if !in_progress {
                return Err(WireError::Io(err));
            }
        }
    }

    let deadline = Instant::now() + timeout;
    loop {
        if stop_flag.is_some_and(|s| s.load(Ordering::Relaxed)) {
            return Err(WireError::Cancelled);
        }
        let now = Instant::now();
        if now >= deadline {
            return Err(WireError::Io(io::Error::new(
                io::ErrorKind::TimedOut,
                "timed out",
            )));
        }
        let slice = (deadline - now).min(Duration::from_millis(50));
        if poll_connect_complete(&sock, slice)? {
            if let Some(err) = sock.take_error()? {
                return Err(WireError::Io(err));
            }
            sock.set_nonblocking(false)?;
            return Ok(TcpStream::from(sock));
        }
    }
}

#[cfg(windows)]
fn poll_connect_complete(sock: &Socket, timeout: Duration) -> Result<bool, WireError> {
    use std::os::windows::io::AsRawSocket;
    use windows_sys::Win32::Networking::WinSock::{
        WSAPoll, POLLERR, POLLHUP, POLLWRNORM, SOCKET, WSAPOLLFD,
    };

    let mut pfd = WSAPOLLFD {
        fd: sock.as_raw_socket() as SOCKET,
        events: POLLWRNORM as i16,
        revents: 0,
    };
    let rc = unsafe { WSAPoll(&mut pfd, 1, timeout.as_millis().max(1) as i32) };
    if rc > 0 {
        Ok((pfd.revents & (POLLWRNORM as i16 | POLLERR as i16 | POLLHUP as i16)) != 0)
    } else if rc == 0 {
        Ok(false)
    } else {
        Err(WireError::Io(io::Error::last_os_error()))
    }
}

#[cfg(not(windows))]
fn poll_connect_complete(_sock: &Socket, timeout: Duration) -> Result<bool, WireError> {
    std::thread::sleep(timeout);
    Ok(true)
}

#[derive(Debug, Clone, Serialize)]
pub struct ClockSample {
    pub pc_send_ns: i64,
    pub device_recv_ns: u64,
    pub device_send_ns: u64,
    pub pc_recv_ns: i64,
    pub low_ns: i128,
    pub high_ns: i128,
    pub rtt_ns: i128,
}

#[derive(Debug, Clone, Serialize)]
pub struct ClockCalibration {
    pub offset_ns: f64,
    pub uncertainty_ms: f64,
    pub calibrated_pc_ns: i64,
    pub samples: Vec<ClockSample>,
}

pub fn sync_clock(
    host: &str,
    port: u16,
    count: usize,
    stop_flag: Option<&AtomicBool>,
) -> Result<ClockCalibration, WireError> {
    let mut samples = Vec::with_capacity(count);
    for _ in 0..count {
        if stop_flag.is_some_and(|s| s.load(Ordering::Relaxed)) {
            return Err(WireError::Cancelled);
        }
        let mut sock = connect_cancellable(host, port, Duration::from_secs(3), stop_flag)?;
        let _ = sock.set_nodelay(true);
        let _ = sock.set_write_timeout(Some(Duration::from_secs(3)));
        let start = monotonic_ns();
        sock.write_all(b"PING\n")?;
        let raw = read_exact_stream(&mut sock, 16, Duration::from_secs(3), None, stop_flag)?;
        let end = monotonic_ns();
        let received = u64::from_le_bytes(raw[0..8].try_into().unwrap());
        let sent = u64::from_le_bytes(raw[8..16].try_into().unwrap());
        let low_ns = (sent as i128) - (end as i128);
        let high_ns = (received as i128) - (start as i128);
        let rtt_ns = ((end - start) as i128) - ((sent as i128) - (received as i128));
        samples.push(ClockSample {
            pc_send_ns: start,
            device_recv_ns: received,
            device_send_ns: sent,
            pc_recv_ns: end,
            low_ns,
            high_ns,
            rtt_ns,
        });
    }
    let best = samples
        .iter()
        .min_by_key(|s| s.rtt_ns)
        .ok_or_else(|| WireError::Invalid("No clock sync samples collected".into()))?;
    Ok(ClockCalibration {
        offset_ns: ((best.low_ns + best.high_ns) as f64) / 2.0,
        uncertainty_ms: ((best.high_ns - best.low_ns) as f64) / 2e6,
        calibrated_pc_ns: best.pc_recv_ns,
        samples,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::thread;

    fn sample_header(seq: u64, session: u64, flags: u32, payload_bytes: u32) -> FrameHeader {
        let now = 1_000_000_000u64 + seq * 11_111_111;
        FrameHeader {
            payload_bytes,
            flags,
            session_id: session,
            source_seq: seq,
            sensor_ns: seq * 11_111_111,
            vendor_ns: seq * 11_111_111 + 4,
            copy_mono_ns: now,
            encode_begin_ns: now,
            encode_end_ns: now,
            emit_mono_ns: now,
            eye0_sensor_ns: seq * 11_111_111 - 2,
            eye1_sensor_ns: seq * 11_111_111 - 1,
            mouth_sensor_ns: seq * 11_111_111,
            raw_identity: seq,
            width: 400,
            height: 400,
            raw_crc32: 42,
            quality: 95,
        }
    }

    #[test]
    fn frame_header_roundtrip_and_validation() {
        let h = sample_header(10, 1, JPEG, 1234);
        let packed = h.pack();
        assert_eq!(packed.len(), 128);
        assert_eq!(FrameHeader::unpack(&packed).unwrap(), h);

        let end = sample_header(11, 1, END, 0);
        assert_eq!(FrameHeader::unpack(&end.pack()).unwrap(), end);

        let mut bad_end = end.clone();
        bad_end.payload_bytes = 1;
        assert!(FrameHeader::unpack(&bad_end.pack()).is_err());

        let mut bad_size = h.clone();
        bad_size.width = 320;
        assert!(FrameHeader::unpack(&bad_size.pack()).is_err());
    }

    #[test]
    fn fragmented_frame_read_and_eof() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let h = sample_header(10, 1, JPEG, 17);
        let mut data = h.pack().to_vec();
        data.extend_from_slice(&[b'x'; 17]);
        let expected = h.clone();

        let server = thread::spawn(move || {
            let (mut conn, _) = listener.accept().unwrap();
            for chunk in data.chunks(3) {
                conn.write_all(chunk).unwrap();
            }
        });

        let mut client = TcpStream::connect(addr).unwrap();
        let (got_h, got_payload) = read_frame(&mut client, Duration::from_secs(2), None).unwrap();
        assert_eq!(got_h, expected);
        assert_eq!(got_payload, vec![b'x'; 17]);

        let err = read_frame(&mut client, Duration::from_secs(2), None).unwrap_err();
        assert!(matches!(err, WireError::Eof(_)));
        server.join().unwrap();
    }

    #[test]
    fn source_order_rejects_dups_ooo_non_advancing_and_retired() {
        let mut order = SourceOrder::new();
        assert_eq!(
            order.accept(&sample_header(10, 1, JPEG, 10)),
            (true, AcceptOutcome::NewSession, 0)
        );
        assert_eq!(
            order.accept(&sample_header(10, 1, JPEG, 10)),
            (false, AcceptOutcome::Duplicate, 0)
        );
        assert_eq!(
            order.accept(&sample_header(9, 1, JPEG, 10)),
            (false, AcceptOutcome::OutOfOrder, 0)
        );
        assert_eq!(
            order.accept(&sample_header(13, 1, JPEG, 10)),
            (true, AcceptOutcome::NewSource, 2)
        );
        let mut non_adv = sample_header(14, 1, JPEG, 10);
        non_adv.mouth_sensor_ns = 1;
        assert_eq!(
            order.accept(&non_adv),
            (false, AcceptOutcome::NonAdvancingTime, 0)
        );
        assert_eq!(
            order.accept(&sample_header(1, 2, JPEG, 10)),
            (true, AcceptOutcome::NewSession, 0)
        );
        assert_eq!(
            order.accept(&sample_header(20, 1, JPEG, 10)),
            (false, AcceptOutcome::RetiredSession, 0)
        );
    }

    #[test]
    fn idle_timeout_on_silent_stream() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let _server = thread::spawn(move || {
            let (_conn, _) = listener.accept().unwrap();
            thread::sleep(Duration::from_millis(600));
        });
        let mut client = TcpStream::connect(addr).unwrap();
        let err = read_frame(&mut client, Duration::from_millis(120), None).unwrap_err();
        assert!(matches!(err, WireError::IdleTimeout(_)));
    }
}
