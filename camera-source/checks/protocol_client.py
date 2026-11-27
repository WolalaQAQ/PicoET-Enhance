"""Minimal LAN camera discovery and PTC1/PSL1/PSE1 protocol client helpers.

Used by camera-source host checks (test_stock_native.py) and standalone CLI
discovery (`python camera-source/checks/protocol_client.py --cameras`).

instant33 is action-postprocessed, clamped, before temporal filtering;
instant_preclamp33 is the same action-postprocessed vector before clamping.
Neither is SNPE raw. A zero TongueOut is not a reviewed tongue negative.
"""
import argparse
from contextlib import ExitStack
from dataclasses import dataclass, field
import http.client
import json
import math
import select
import socket
import struct
import time

STOCK_BYTES = 640
STOCK_WIRE = struct.Struct('<4sHHII9QiI134f2I')
CONTEXT_WIRE = struct.Struct('<4sHHQQ')
EYE_WIRE = struct.Struct('<4sHHII6Q64f')
VALID, ASSOCIATED, WARMUP = 1, 2, 4
STAGES = 15  # raw, postprocess-before-clamp, post-clamp instant, temporal final

DISCOVER = b'PICO_MOUTH_DISCOVER_V1\n'
CAMERA_DISCOVER = b'PICO_CAMERA_DISCOVER_V1\n'
PORT = 45681
MULTICAST = '239.255.255.250'


class CaptureContextUnavailable(ConnectionError):
    """The daemon did not provide an attached, verified capture owner."""


class IdleStockTimeout(TimeoutError):
    """No new PSL1/PSE1 record started before the socket read timeout."""


@dataclass(frozen=True)
class CaptureContext:
    capture_session_id: int
    service_generation: int

    def pack(self):
        if not self.capture_session_id or not self.service_generation:
            raise ValueError('PTC1 needs a nonzero verified session and generation')
        return CONTEXT_WIRE.pack(b'PTC1', 1, CONTEXT_WIRE.size,
                                 self.capture_session_id, self.service_generation)

    @classmethod
    def unpack(cls, data):
        if len(data) != CONTEXT_WIRE.size:
            raise ValueError('PTC1 requires 24 bytes')
        magic, version, size, session, generation = CONTEXT_WIRE.unpack(data)
        if (magic, version, size) != (b'PTC1', 1, CONTEXT_WIRE.size):
            raise ValueError('unsupported PTC1 context')
        result = cls(session, generation)
        result.pack()
        return result

    @property
    def evidence(self):
        """JSON-safe binding evidence received from the same root daemon as PTJ1."""
        return dict(protocol='PTC1', version=1, capture_session_id=self.capture_session_id,
                    service_generation=self.service_generation, wire_hex=self.pack().hex(),
                    owner_source='root-daemon nonce/PID-birth verified immutable IPC owner')

    def require_session(self, session_id):
        """Call for every PTJ1 frame, including the first; re-handshake on restart."""
        if session_id != self.capture_session_id:
            raise ValueError('PTJ1 session contradicts capture context; bind a new context before frames')


def _is_poll_timeout(error):
    """True only for Python socket.settimeout() expirations, never OS WSAETIMEDOUT/ETIMEDOUT."""
    return error.errno is None and getattr(error, 'winerror', None) is None


def _read_exact(sock, count, name, *, allow_idle=False):
    data = bytearray()
    while len(data) < count:
        try:
            part = sock.recv(count - len(data))
        except TimeoutError as error:
            if allow_idle and not data and _is_poll_timeout(error):
                raise IdleStockTimeout(f'Waiting for next {name}') from error
            raise
        if not part:
            raise EOFError(f'partial {name}: {len(data)}/{count}')
        data.extend(part)
    return bytes(data)


def read_capture_context(sock):
    """Use a fresh connected control socket to the JPEG endpoint, before JPEG.

    Sends CONTEXT itself. The daemon returns PTC1 then closes this control
    connection; caller owns the socket. No labels/latest-record fallback.
    """
    sock.sendall(b'CONTEXT\n')
    try:
        data = _read_exact(sock, CONTEXT_WIRE.size, 'PTC1 context')
    except EOFError as error:
        raise CaptureContextUnavailable('CONTEXT refused/unavailable or truncated; no verified capture owner') from error
    return CaptureContext.unpack(data)


@dataclass
class StockRecord:
    capture_session_id: int
    service_generation: int
    source_seq: int
    mouth_sensor_ns: int
    raw33: list
    instant33: list
    final33: list
    quality2: list
    valid: bool
    association_valid: bool = False
    stock_input_ns: int = 0
    vendor_ns: int = 0
    raw_identity: int = 0
    produced_mono_ns: int = 0
    label_seq: int = 0
    classification: int = -1
    warmup: bool = True
    instant_preclamp33: list = field(default_factory=lambda: [0.0] * 33)

    @property
    def source_key(self):
        """None until the input clock's provenance and exact association are proven."""
        if not self.association_valid:
            return None
        return (self.capture_session_id, self.service_generation,
                self.source_seq, self.mouth_sensor_ns)

    @property
    def image_key(self):
        """PTJ1's key; one capture session binds exactly one service generation."""
        if self.source_key is None:
            return None
        return (self.capture_session_id, self.source_seq, self.mouth_sensor_ns)

    def pack(self):
        arrays = (self.raw33, self.instant33, self.final33, self.quality2,
                  self.instant_preclamp33)
        if tuple(map(len, arrays)) != (33, 33, 33, 2, 33):
            raise ValueError('stock vector lengths must be 33/33/33/2/33')
        values = [float(x) for group in arrays for x in group]
        if not all(map(math.isfinite, values)):
            raise ValueError('nonfinite stock label')
        if self.association_valid and not all((self.capture_session_id, self.service_generation,
                                               self.source_seq, self.mouth_sensor_ns,
                                               self.raw_identity)):
            raise ValueError('associated stock label has no exact source identity')
        if not self.association_valid and any((self.source_seq, self.mouth_sensor_ns,
                                               self.vendor_ns, self.raw_identity)):
            raise ValueError('unassociated stock label must not invent camera metadata')
        flags = (VALID if self.valid else 0) | (ASSOCIATED if self.association_valid else 0)
        flags |= WARMUP if self.warmup else 0
        return STOCK_WIRE.pack(b'PSL1', 1, 96, STOCK_BYTES, flags,
                               self.capture_session_id, self.service_generation, self.source_seq,
                               self.mouth_sensor_ns, self.stock_input_ns, self.vendor_ns,
                               self.raw_identity, self.produced_mono_ns, self.label_seq,
                               self.classification, STAGES, *values, 0, 0)

    @classmethod
    def unpack(cls, data):
        if len(data) != STOCK_BYTES:
            raise ValueError('PSL1 requires 640 bytes')
        v = STOCK_WIRE.unpack(data)
        if v[:4] != (b'PSL1', 1, 96, STOCK_BYTES) or v[4] & ~7 or v[15] != STAGES:
            raise ValueError('unsupported PSL1 header/stages')
        result = cls(v[5], v[6], v[7], v[8], list(v[16:49]), list(v[49:82]),
                     list(v[82:115]), list(v[115:117]), bool(v[4] & VALID),
                     bool(v[4] & ASSOCIATED), v[9], v[10], v[11], v[12], v[13],
                     v[14], bool(v[4] & WARMUP), list(v[117:150]))
        result.pack()  # The decoder applies the same identity/finite-value contract.
        return result


def read_stock(sock):
    """Read one PSL1 observation after sending LABELS on the mouth endpoint."""
    return StockRecord.unpack(_read_exact(sock, STOCK_BYTES, 'PSL1 record', allow_idle=True))


@dataclass(frozen=True)
class EyeRecord:
    capture_session_id: int
    service_generation: int
    sequence: int
    source_ns: tuple
    produced_mono_ns: int
    updated_mask: int
    valid_mask: int
    raw24: tuple
    post20: tuple
    final20: tuple

    @classmethod
    def unpack(cls, data):
        magic, version, header, size, flags, session, generation, seq, left, right, produced, *values = EYE_WIRE.unpack(data)
        updated, valid = flags & 3, flags >> 2
        if ((magic, version, header, size) != (b'PSE1', 1, 64, EYE_WIRE.size)
                or not all((session, generation, seq, produced, updated))
                or valid & ~updated or updated != (bool(left) | (bool(right) << 1))
                or not all(map(math.isfinite, values))):
            raise ValueError('invalid PSE1 header, source clocks or values')
        return cls(session, generation, seq, (left, right), produced, updated, valid,
                   tuple(values[:24]), tuple(values[24:44]), tuple(values[44:]))


def read_eye(sock):
    """Read one PSE1 observation after sending EYES on the left-camera endpoint."""
    return EyeRecord.unpack(_read_exact(sock, EYE_WIRE.size, 'PSE1 record', allow_idle=True))


def _interfaces():
    try:
        found = sorted({row[4][0] for row in socket.getaddrinfo(socket.gethostname(), None,
                        socket.AF_INET, socket.SOCK_DGRAM) if not row[4][0].startswith('127.')})
    except OSError:
        found = []
    return found or ['']


def _is_valid_port(port):
    return isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535


def _parse_u64_field(raw, field):
    if isinstance(raw, bool):
        raise ValueError(f'Invalid {field}')
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if text.isdigit():
            return int(text)
    raise ValueError(f'Invalid {field}')


def _catalog(data):
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('Invalid camera catalog')
    version = value.get('version')
    if value.get('service') != 'pico-camera' or isinstance(version, bool) or version != 1:
        raise ValueError('Unknown camera service')
    CaptureContext(
        _parse_u64_field(value['session_id'], 'session_id'),
        _parse_u64_field(value['service_generation'], 'service_generation'),
    ).pack()
    streams = value.get('streams')
    if not isinstance(streams, list) or any(not isinstance(s, dict) for s in streams):
        raise ValueError('Unknown camera streams')
    if [s.get('id') for s in streams] != ['mouth', 'eye_left', 'eye_right']:
        raise ValueError('Unknown camera streams')
    if any(not _is_valid_port(s.get('port')) or s.get('path') != '/stream.mjpg' for s in streams):
        raise ValueError('Invalid camera endpoint')
    return value


def discover_sources(timeout=1.2, targets=None):
    """Discover LAN cameras without subscribing; compatible with legacy mouth sources."""
    if targets is None:
        interfaces = _interfaces()
        if '' not in interfaces:
            interfaces = [*interfaces, '']
        targets = [('255.255.255.255', PORT), (MULTICAST, PORT)]
    else:
        interfaces = ['']
        targets = [(host, port) for host, port in targets if host and _is_valid_port(port)]
    candidates = {}
    catalogs = {}
    with ExitStack() as stack:
        sockets = []
        for interface in interfaces:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                sock.bind((interface, 0))
                if interface:
                    try:
                        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
                    except OSError:
                        pass
                sent = 0
                for target in targets:
                    try:
                        sock.sendto(DISCOVER, target)
                        sock.sendto(CAMERA_DISCOVER, target)
                        sent += 1
                    except OSError:
                        continue
                if sent:
                    stack.enter_context(sock)
                    sockets.append(sock)
                else:
                    sock.close()
            except OSError:
                sock.close()
                continue
        deadline = time.monotonic() + timeout
        while sockets and time.monotonic() < deadline:
            try:
                readable, _, _ = select.select(sockets, [], [], max(.001, deadline - time.monotonic()))
            except OSError:
                break
            for sock in readable:
                try:
                    data, endpoint = sock.recvfrom(8192)
                except OSError:
                    continue
                try:
                    if data.startswith(b'{'):
                        catalogs[endpoint] = _catalog(data)
                    else:
                        candidates[endpoint] = CaptureContext.unpack(data)
                except (ValueError, KeyError, TypeError, struct.error):
                    continue
    verified_catalogs = {}
    for (host, port), advertised in catalogs.items():
        connection = http.client.HTTPConnection(host, port, timeout=1.0)
        try:
            connection.request('GET', '/cameras')
            response = connection.getresponse()
            body = response.read(8193)
            if response.status != 200 or len(body) > 8192 or _catalog(body) != advertised:
                continue
            mouth_port = advertised['streams'][0]['port']
            key = (host, mouth_port)
            candidates[key] = CaptureContext(
                _parse_u64_field(advertised['session_id'], 'session_id'),
                _parse_u64_field(advertised['service_generation'], 'service_generation'),
            )
            verified_catalogs[key] = advertised
        except (OSError, ValueError, KeyError, TypeError, struct.error, http.client.HTTPException):
            continue
        finally:
            connection.close()
    devices = []
    for (host, port), advertised in sorted(candidates.items()):
        try:
            with socket.create_connection((host, port), timeout=1.0) as sock:
                verified = read_capture_context(sock)
            if verified != advertised:
                continue
        except (OSError, ValueError, EOFError):
            continue
        device = dict(host=host, port=port, name='PICO Mouth',
                      session_id=str(verified.capture_session_id),
                      service_generation=str(verified.service_generation))
        if catalog := verified_catalogs.get((host, port)):
            device['name'] = 'PICO Cameras'
            device['streams'] = [dict(stream, url=f"http://{host}:{stream['port']}{stream['path']}")
                                 for stream in catalog['streams']]
        devices.append(device)
    return dict(devices=devices)


def discover_cameras(timeout=1.2, targets=None):
    return dict(devices=[d for d in discover_sources(timeout, targets)['devices'] if 'streams' in d])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Discover PicoET-Enhance LAN camera services.')
    parser.add_argument('--host', help='Unicast discovery target when broadcast/multicast is unavailable')
    parser.add_argument('--port', type=int, default=PORT)
    parser.add_argument('--timeout', type=float, default=1.2)
    parser.add_argument('--cameras', action='store_true', help='Only list the three-camera service')
    args = parser.parse_args()
    discover = discover_cameras if args.cameras else discover_sources
    print(json.dumps(discover(args.timeout, [(args.host, args.port)] if args.host else None), indent=2))
