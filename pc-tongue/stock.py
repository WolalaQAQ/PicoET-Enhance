"""PSL1 stock-label records. Send b'LABELS\n' on a second PTJ TCP connection.

instant33 is action-postprocessed, clamped, before temporal filtering;
instant_preclamp33 is the same action-postprocessed vector before clamping.
Neither is SNPE raw. A zero TongueOut is not a reviewed tongue negative.
"""
from dataclasses import dataclass, field
import math
import struct

STOCK_BYTES = 640
STOCK_WIRE = struct.Struct('<4sHHII9QiI134f2I')
CONTEXT_WIRE = struct.Struct('<4sHHQQ')
EYE_WIRE = struct.Struct('<4sHHII6Q64f')
VALID, ASSOCIATED, WARMUP = 1, 2, 4
STAGES = 15  # raw, postprocess-before-clamp, post-clamp instant, temporal final


class CaptureContextUnavailable(ConnectionError):
    """The daemon did not provide an attached, verified capture owner."""


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


def _read_exact(sock, count, name):
    data = bytearray()
    while len(data) < count:
        part = sock.recv(count - len(data))
        if not part:
            raise EOFError(f'partial {name}: {len(data)}/{count}')
        data.extend(part)
    return bytes(data)


def read_stock(sock):
    return StockRecord.unpack(_read_exact(sock, STOCK_BYTES, 'PSL1 record'))


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
    return EyeRecord.unpack(_read_exact(sock, EYE_WIRE.size, 'PSE1 record'))
