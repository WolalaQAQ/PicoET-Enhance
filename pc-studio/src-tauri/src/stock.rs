use crate::wire::{read_exact_stream, WireError};
use serde::Serialize;
use std::io::Write;
use std::net::TcpStream;
use std::sync::atomic::AtomicBool;
use std::time::Duration;

pub const CONTEXT_BYTES: usize = 24;
pub const STOCK_BYTES: usize = 640;
pub const EYE_BYTES: usize = 320;
pub const VALID: u32 = 1;
pub const ASSOCIATED: u32 = 2;
pub const WARMUP: u32 = 4;
pub const STAGES: u32 = 15;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CaptureContext {
    pub capture_session_id: u64,
    pub service_generation: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct CaptureContextEvidence {
    pub protocol: &'static str,
    pub version: u16,
    pub capture_session_id: u64,
    pub service_generation: u64,
    pub wire_hex: String,
    pub owner_source: &'static str,
}

impl CaptureContext {
    pub fn pack(&self) -> Result<[u8; CONTEXT_BYTES], WireError> {
        if self.capture_session_id == 0 || self.service_generation == 0 {
            return Err(WireError::Invalid(
                "PTC1 needs a nonzero verified session and generation".into(),
            ));
        }
        let mut out = [0u8; CONTEXT_BYTES];
        out[0..4].copy_from_slice(b"PTC1");
        out[4..6].copy_from_slice(&1u16.to_le_bytes());
        out[6..8].copy_from_slice(&(CONTEXT_BYTES as u16).to_le_bytes());
        out[8..16].copy_from_slice(&self.capture_session_id.to_le_bytes());
        out[16..24].copy_from_slice(&self.service_generation.to_le_bytes());
        Ok(out)
    }

    pub fn unpack(data: &[u8]) -> Result<Self, WireError> {
        if data.len() != CONTEXT_BYTES {
            return Err(WireError::Invalid("PTC1 requires 24 bytes".into()));
        }
        let magic = &data[0..4];
        let version = u16::from_le_bytes([data[4], data[5]]);
        let size = u16::from_le_bytes([data[6], data[7]]);
        if magic != b"PTC1" || version != 1 || size != CONTEXT_BYTES as u16 {
            return Err(WireError::Invalid("unsupported PTC1 context".into()));
        }
        let session = u64::from_le_bytes(data[8..16].try_into().unwrap());
        let generation = u64::from_le_bytes(data[16..24].try_into().unwrap());
        let result = Self {
            capture_session_id: session,
            service_generation: generation,
        };
        result.pack()?;
        Ok(result)
    }

    pub fn evidence(&self) -> CaptureContextEvidence {
        let bytes = self.pack().unwrap_or([0u8; CONTEXT_BYTES]);
        let mut wire_hex = String::with_capacity(bytes.len() * 2);
        for b in bytes {
            use std::fmt::Write as _;
            let _ = write!(wire_hex, "{b:02x}");
        }
        CaptureContextEvidence {
            protocol: "PTC1",
            version: 1,
            capture_session_id: self.capture_session_id,
            service_generation: self.service_generation,
            wire_hex,
            owner_source: "root-daemon nonce/PID-birth verified immutable IPC owner",
        }
    }

    pub fn require_session(&self, session_id: u64) -> Result<(), WireError> {
        if session_id != self.capture_session_id {
            return Err(WireError::Invalid(
                "PTJ1 session contradicts capture context; bind a new context before frames".into(),
            ));
        }
        Ok(())
    }
}

pub fn read_capture_context(
    stream: &mut TcpStream,
    timeout: Duration,
    stop_flag: Option<&AtomicBool>,
) -> Result<CaptureContext, WireError> {
    let _ = stream.set_write_timeout(Some(timeout));
    stream.write_all(b"CONTEXT\n")?;
    let data = read_exact_stream(stream, CONTEXT_BYTES, timeout, None, stop_flag).map_err(
        |err| match err {
            WireError::Eof(_) => WireError::Eof(
                "CONTEXT refused/unavailable or truncated; no verified capture owner".into(),
            ),
            other => other,
        },
    )?;
    CaptureContext::unpack(&data)
}

#[derive(Debug, Clone, PartialEq)]
pub struct StockRecord {
    pub capture_session_id: u64,
    pub service_generation: u64,
    pub source_seq: u64,
    pub mouth_sensor_ns: u64,
    pub stock_input_ns: u64,
    pub vendor_ns: u64,
    pub raw_identity: u64,
    pub produced_mono_ns: u64,
    pub label_seq: u64,
    pub classification: i32,
    pub raw33: [f32; 33],
    pub instant33: [f32; 33],
    pub final33: [f32; 33],
    pub quality2: [f32; 2],
    pub instant_preclamp33: [f32; 33],
    pub valid: bool,
    pub association_valid: bool,
    pub warmup: bool,
}

impl StockRecord {
    pub fn pack(&self) -> Result<[u8; STOCK_BYTES], WireError> {
        let all_finite = self.raw33.iter().all(|v| v.is_finite())
            && self.instant33.iter().all(|v| v.is_finite())
            && self.final33.iter().all(|v| v.is_finite())
            && self.quality2.iter().all(|v| v.is_finite())
            && self.instant_preclamp33.iter().all(|v| v.is_finite());
        if !all_finite {
            return Err(WireError::Invalid("nonfinite stock label".into()));
        }
        if self.association_valid
            && (self.capture_session_id == 0
                || self.service_generation == 0
                || self.source_seq == 0
                || self.mouth_sensor_ns == 0
                || self.raw_identity == 0)
        {
            return Err(WireError::Invalid(
                "associated stock label has no exact source identity".into(),
            ));
        }
        if !self.association_valid
            && (self.source_seq != 0
                || self.mouth_sensor_ns != 0
                || self.vendor_ns != 0
                || self.raw_identity != 0)
        {
            return Err(WireError::Invalid(
                "unassociated stock label must not invent camera metadata".into(),
            ));
        }

        let mut out = [0u8; STOCK_BYTES];
        out[0..4].copy_from_slice(b"PSL1");
        out[4..6].copy_from_slice(&1u16.to_le_bytes());
        out[6..8].copy_from_slice(&96u16.to_le_bytes());
        out[8..12].copy_from_slice(&(STOCK_BYTES as u32).to_le_bytes());
        let mut flags = 0u32;
        if self.valid {
            flags |= VALID;
        }
        if self.association_valid {
            flags |= ASSOCIATED;
        }
        if self.warmup {
            flags |= WARMUP;
        }
        out[12..16].copy_from_slice(&flags.to_le_bytes());

        let u64s = [
            self.capture_session_id,
            self.service_generation,
            self.source_seq,
            self.mouth_sensor_ns,
            self.stock_input_ns,
            self.vendor_ns,
            self.raw_identity,
            self.produced_mono_ns,
            self.label_seq,
        ];
        for (i, v) in u64s.iter().enumerate() {
            let off = 16 + i * 8;
            out[off..off + 8].copy_from_slice(&v.to_le_bytes());
        }
        out[88..92].copy_from_slice(&self.classification.to_le_bytes());
        out[92..96].copy_from_slice(&STAGES.to_le_bytes());

        let mut off = 96;
        for slice in [
            self.raw33.as_slice(),
            self.instant33.as_slice(),
            self.final33.as_slice(),
            self.quality2.as_slice(),
            self.instant_preclamp33.as_slice(),
        ] {
            for v in slice {
                out[off..off + 4].copy_from_slice(&v.to_le_bytes());
                off += 4;
            }
        }
        // Final 8 bytes reserved = 0.
        Ok(out)
    }

    pub fn unpack(data: &[u8]) -> Result<Self, WireError> {
        if data.len() != STOCK_BYTES {
            return Err(WireError::Invalid("PSL1 requires 640 bytes".into()));
        }
        let magic = &data[0..4];
        let version = u16::from_le_bytes([data[4], data[5]]);
        let header = u16::from_le_bytes([data[6], data[7]]);
        let size = u32::from_le_bytes(data[8..12].try_into().unwrap());
        let flags = u32::from_le_bytes(data[12..16].try_into().unwrap());
        let stages = u32::from_le_bytes(data[92..96].try_into().unwrap());
        if magic != b"PSL1"
            || version != 1
            || header != 96
            || size != STOCK_BYTES as u32
            || (flags & !7) != 0
            || stages != STAGES
        {
            return Err(WireError::Invalid("unsupported PSL1 header/stages".into()));
        }

        let u64_at = |off: usize| -> u64 { u64::from_le_bytes(data[off..off + 8].try_into().unwrap()) };
        let f32_at = |off: usize| -> f32 { f32::from_le_bytes(data[off..off + 4].try_into().unwrap()) };

        let read_33 = |start: usize| -> [f32; 33] {
            let mut arr = [0.0f32; 33];
            for (i, slot) in arr.iter_mut().enumerate() {
                *slot = f32_at(start + i * 4);
            }
            arr
        };

        let result = Self {
            capture_session_id: u64_at(16),
            service_generation: u64_at(24),
            source_seq: u64_at(32),
            mouth_sensor_ns: u64_at(40),
            stock_input_ns: u64_at(48),
            vendor_ns: u64_at(56),
            raw_identity: u64_at(64),
            produced_mono_ns: u64_at(72),
            label_seq: u64_at(80),
            classification: i32::from_le_bytes(data[88..92].try_into().unwrap()),
            raw33: read_33(96),
            instant33: read_33(96 + 33 * 4),
            final33: read_33(96 + 66 * 4),
            quality2: [f32_at(96 + 99 * 4), f32_at(96 + 100 * 4)],
            instant_preclamp33: read_33(96 + 101 * 4),
            valid: (flags & VALID) != 0,
            association_valid: (flags & ASSOCIATED) != 0,
            warmup: (flags & WARMUP) != 0,
        };
        result.pack()?;
        Ok(result)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct EyeRecord {
    pub capture_session_id: u64,
    pub service_generation: u64,
    pub sequence: u64,
    pub source_ns: (u64, u64),
    pub produced_mono_ns: u64,
    pub updated_mask: u32,
    pub valid_mask: u32,
    pub raw24: [f32; 24],
    pub post20: [f32; 20],
    pub final20: [f32; 20],
}

impl EyeRecord {
    pub fn unpack(data: &[u8]) -> Result<Self, WireError> {
        if data.len() != EYE_BYTES {
            return Err(WireError::Invalid("PSE1 requires 320 bytes".into()));
        }
        let magic = &data[0..4];
        let version = u16::from_le_bytes([data[4], data[5]]);
        let header = u16::from_le_bytes([data[6], data[7]]);
        let size = u32::from_le_bytes(data[8..12].try_into().unwrap());
        let flags = u32::from_le_bytes(data[12..16].try_into().unwrap());
        let updated = flags & 3;
        let valid = flags >> 2;

        let u64_at = |off: usize| -> u64 { u64::from_le_bytes(data[off..off + 8].try_into().unwrap()) };
        let session = u64_at(16);
        let generation = u64_at(24);
        let seq = u64_at(32);
        let left = u64_at(40);
        let right = u64_at(48);
        let produced = u64_at(56);

        let expected_updated = u32::from(left != 0) | (u32::from(right != 0) << 1);
        if magic != b"PSE1"
            || version != 1
            || header != 64
            || size != EYE_BYTES as u32
            || session == 0
            || generation == 0
            || seq == 0
            || produced == 0
            || updated == 0
            || (valid & !updated) != 0
            || updated != expected_updated
        {
            return Err(WireError::Invalid(
                "invalid PSE1 header, source clocks or values".into(),
            ));
        }

        let mut values = [0.0f32; 64];
        for (i, slot) in values.iter_mut().enumerate() {
            let off = 64 + i * 4;
            let v = f32::from_le_bytes(data[off..off + 4].try_into().unwrap());
            if !v.is_finite() {
                return Err(WireError::Invalid(
                    "invalid PSE1 header, source clocks or values".into(),
                ));
            }
            *slot = v;
        }

        let mut raw24 = [0.0f32; 24];
        let mut post20 = [0.0f32; 20];
        let mut final20 = [0.0f32; 20];
        raw24.copy_from_slice(&values[0..24]);
        post20.copy_from_slice(&values[24..44]);
        final20.copy_from_slice(&values[44..64]);

        Ok(Self {
            capture_session_id: session,
            service_generation: generation,
            sequence: seq,
            source_ns: (left, right),
            produced_mono_ns: produced,
            updated_mask: updated,
            valid_mask: valid,
            raw24,
            post20,
            final20,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ptc1_roundtrip_and_rejection() {
        let ctx = CaptureContext {
            capture_session_id: 10,
            service_generation: 20,
        };
        let packed = ctx.pack().unwrap();
        assert_eq!(packed.len(), 24);
        assert_eq!(CaptureContext::unpack(&packed).unwrap(), ctx);
        assert!(ctx.require_session(10).is_ok());
        assert!(ctx.require_session(11).is_err());

        let zero = CaptureContext {
            capture_session_id: 0,
            service_generation: 20,
        };
        assert!(zero.pack().is_err());
    }

    #[test]
    fn psl1_roundtrip_and_contract() {
        let rec = StockRecord {
            capture_session_id: 10,
            service_generation: 20,
            source_seq: 0,
            mouth_sensor_ns: 0,
            stock_input_ns: 12345,
            vendor_ns: 0,
            raw_identity: 0,
            produced_mono_ns: 0,
            label_seq: 0,
            classification: -1,
            raw33: [-0.125; 33],
            instant33: [0.0; 33],
            final33: [0.25; 33],
            quality2: [0.25, 0.75],
            instant_preclamp33: [-0.0625; 33],
            valid: true,
            association_valid: false,
            warmup: true,
        };
        let packed = rec.pack().unwrap();
        assert_eq!(packed.len(), STOCK_BYTES);
        assert_eq!(StockRecord::unpack(&packed).unwrap(), rec);

        let mut bad = rec.clone();
        bad.raw33[0] = f32::NAN;
        assert!(bad.pack().is_err());

        let mut bad_assoc = rec.clone();
        bad_assoc.association_valid = true;
        assert!(bad_assoc.pack().is_err());
    }
}
