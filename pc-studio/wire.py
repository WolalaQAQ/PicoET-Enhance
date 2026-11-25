"""PTJ1 source-preserving JPEG framing and clock synchronization."""
from dataclasses import dataclass
import socket
import struct
import time

HEADER = struct.Struct('<4sHHII12Q4I')
JPEG, RAW, END, REPLAY = 1, 2, 4, 8
assert HEADER.size == 128


@dataclass(frozen=True)
class FrameHeader:
    payload_bytes: int
    flags: int
    session_id: int
    source_seq: int
    sensor_ns: int
    vendor_ns: int
    copy_mono_ns: int
    encode_begin_ns: int
    encode_end_ns: int
    emit_mono_ns: int
    eye0_sensor_ns: int
    eye1_sensor_ns: int
    mouth_sensor_ns: int
    raw_identity: int
    width: int = 400
    height: int = 400
    raw_crc32: int = 0
    quality: int = 95

    def pack(self):
        return HEADER.pack(b'PTJ1', 1, 128, *self.__dict__.values())

    @classmethod
    def unpack(cls, data):
        magic, version, size, *fields = HEADER.unpack(data)
        if (magic, version, size) != (b'PTJ1', 1, 128):
            raise ValueError('Unsupported PTJ1 frame header')
        result = cls(*fields)
        if result.flags & END:
            if result.payload_bytes != 0:
                raise ValueError('END must not contain an image payload')
        else:
            if result.flags & (JPEG | RAW) not in (JPEG, RAW):
                raise ValueError('Expected one JPEG or RAW payload type')
            if (result.width, result.height, result.quality) != (400, 400, 95):
                raise ValueError('Expected 400x400 JPEG95 source')
            if not 0 < result.payload_bytes <= 2_000_000:
                raise ValueError('Invalid image payload length')
        return result

    def identity(self):
        return (self.session_id, self.source_seq, self.sensor_ns, self.vendor_ns,
                self.eye0_sensor_ns, self.eye1_sensor_ns, self.mouth_sensor_ns,
                self.raw_identity, self.raw_crc32)


class IdleStreamTimeout(TimeoutError):
    """No new PTJ1 frame started before the socket read timeout."""


def _is_poll_timeout(exc):
    """True only for Python socket.settimeout() expirations, never OS WSAETIMEDOUT/ETIMEDOUT."""
    return exc.errno is None and getattr(exc, 'winerror', None) is None


def enable_keepalive(sock, idle_ms=5000, interval_ms=1000, count=5):
    """Enable OS TCP keepalive so dropped wireless links still fail idle reads."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except (OSError, AttributeError):
        return
    sio_keepalive_vals = getattr(socket, 'SIO_KEEPALIVE_VALS', None)
    if sio_keepalive_vals is not None and hasattr(sock, 'ioctl'):
        try:
            sock.ioctl(sio_keepalive_vals, (1, int(idle_ms), int(interval_ms)))
        except (OSError, AttributeError, ValueError):
            pass
    else:
        idle_s = max(1, int(idle_ms) // 1000)
        interval_s = max(1, int(interval_ms) // 1000)
        for opt_name, val in (
            ('TCP_KEEPIDLE', idle_s),
            ('TCP_KEEPALIVE', idle_s),
            ('TCP_KEEPINTVL', interval_s),
        ):
            opt = getattr(socket, opt_name, None)
            if opt is not None:
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, opt, val)
                except (OSError, AttributeError):
                    pass
    tcp_keepcnt = getattr(socket, 'TCP_KEEPCNT', None)
    if tcp_keepcnt is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, tcp_keepcnt, max(1, int(count)))
        except (OSError, AttributeError):
            pass


def read_exact(sock, size, *, part=None):
    result = bytearray(size)
    view = memoryview(result)
    while view:
        try:
            count = sock.recv_into(view)
        except TimeoutError as exc:
            if part == 'header' and len(view) == size and _is_poll_timeout(exc):
                raise IdleStreamTimeout('Waiting for next PTJ1 frame') from exc
            raise
        if count == 0:
            if part == 'header' and len(view) == size:
                raise EOFError('PTJ1 stream closed between frames')
            if part:
                raise EOFError(f'PTJ1 stream closed during {part}: received {size - len(view)}/{size} bytes')
            raise EOFError('PTJ1 connection ended within a frame')
        view = view[count:]
    return bytes(result)


def read_frame(sock):
    header = FrameHeader.unpack(read_exact(sock, HEADER.size, part='header'))
    return header, read_exact(sock, header.payload_bytes, part='payload')


class SourceOrder:
    def __init__(self):
        self.session = None
        self.last_seq = None
        self.last_mouth_ns = None
        self.retired = set()

    def accept(self, header):
        if header.session_id in self.retired:
            return False, 'retired_session', 0
        changed = header.session_id != self.session
        if changed:
            if self.session is not None:
                self.retired.add(self.session)
            self.session, self.last_seq, self.last_mouth_ns = header.session_id, None, None
        if self.last_seq is not None and header.source_seq <= self.last_seq:
            return False, 'duplicate' if header.source_seq == self.last_seq else 'out_of_order', 0
        if self.last_mouth_ns is not None and header.mouth_sensor_ns <= self.last_mouth_ns:
            return False, 'non_advancing_time', 0
        gap = 0 if self.last_seq is None else header.source_seq - self.last_seq - 1
        self.last_seq = header.source_seq
        self.last_mouth_ns = header.mouth_sensor_ns
        return True, 'new_session' if changed else 'new_source', gap


_ORIG_CREATE_CONNECTION = socket.create_connection


def connect_cancellable(host, port, timeout=3, *, stop_event=None, on_socket=None, keep_tracked=False):
    """Connect TCP socket while registering it so stop() can interrupt an unreachable-host wait."""
    if stop_event is not None and stop_event.is_set():
        raise OSError('Connection cancelled')
    if socket.create_connection is not _ORIG_CREATE_CONNECTION:
        sock = socket.create_connection((host, port), timeout=timeout)
        if on_socket is not None and keep_tracked:
            on_socket(sock)
        return sock
    last_error = None
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    for family, socktype, proto, _, addr in infos:
        if stop_event is not None and stop_event.is_set():
            raise OSError('Connection cancelled')
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        if on_socket is not None:
            on_socket(sock)
        connected = False
        try:
            if stop_event is not None and stop_event.is_set():
                raise OSError('Connection cancelled')
            sock.connect(addr)
            connected = True
            return sock
        except OSError as error:
            last_error = error
            sock.close()
        finally:
            if on_socket is not None and (not connected or not keep_tracked):
                on_socket(None)
    raise last_error or OSError(f'Unable to connect to {host}:{port}')


def sync_clock(host, port, count=12, *, stop_event=None, on_socket=None):
    samples = []
    for _ in range(count):
        if stop_event is not None and stop_event.is_set():
            raise OSError('Clock sync cancelled')
        try:
            with connect_cancellable(
                host, port, timeout=3, stop_event=stop_event, on_socket=on_socket, keep_tracked=True,
            ) as sock:
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except (OSError, AttributeError):
                    pass
                start = time.perf_counter_ns()
                sock.sendall(b'PING\n')
                received, sent = struct.unpack('<QQ', read_exact(sock, 16))
                end = time.perf_counter_ns()
        finally:
            if on_socket is not None:
                on_socket(None)
        samples.append({'pc_send_ns': start, 'device_recv_ns': received,
                        'device_send_ns': sent, 'pc_recv_ns': end,
                        'low_ns': sent - end, 'high_ns': received - start,
                        'rtt_ns': end - start - (sent - received)})
    best = min(samples, key=lambda sample: sample['rtt_ns'])
    return {'offset_ns': (best['low_ns'] + best['high_ns']) / 2,
            'uncertainty_ms': (best['high_ns'] - best['low_ns']) / 2e6,
            'calibrated_pc_ns': best['pc_recv_ns'], 'samples': samples}
