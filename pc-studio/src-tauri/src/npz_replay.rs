//! Pure-Rust `.npz` / `.npy` replay loader replacing `numpy.load(..., allow_pickle=False)`
//! in `runtime.py:_replay`.

use std::fs::File;
use std::io::{Read, Seek};
use std::path::Path;
use zip::ZipArchive;

pub const FRAME_WIDTH: usize = 400;
pub const FRAME_HEIGHT: usize = 400;
pub const FRAME_PIXELS: usize = FRAME_WIDTH * FRAME_HEIGHT;

const NPY_MAGIC: &[u8; 6] = b"\x93NUMPY";
const MAX_NPY_HEADER_BYTES: usize = 1024 * 1024;
const SHAPE_ERR: &str = "Expected replay images[N,400,400], seq[N], times[N,3]";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NpyDtype {
    U8,
    U32,
    I32,
    U64,
    I64,
}

impl NpyDtype {
    pub fn from_descr(descr: &str) -> Result<Self, String> {
        let trimmed = descr.trim();
        if trimmed == "|O" || trimmed == "O" || trimmed.ends_with('O') {
            return Err("Object/pickle NPY dtypes are not allowed".to_string());
        }
        if trimmed.starts_with('>') {
            return Err(format!(
                "Big-endian NPY dtype '{trimmed}' is not supported (expected little-endian)"
            ));
        }
        match trimmed {
            "|u1" | "<u1" | "=u1" | "u1" | "B" => Ok(Self::U8),
            "<u4" | "=u4" => Ok(Self::U32),
            "<i4" | "=i4" => Ok(Self::I32),
            "<u8" | "=u8" => Ok(Self::U64),
            "<i8" | "=i8" => Ok(Self::I64),
            other => Err(format!("Unsupported NPY dtype: {other}")),
        }
    }

    pub fn item_size(self) -> usize {
        match self {
            Self::U8 => 1,
            Self::U32 | Self::I32 => 4,
            Self::U64 | Self::I64 => 8,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NpyHeader {
    pub version: (u8, u8),
    pub dtype: NpyDtype,
    pub descr: String,
    pub fortran_order: bool,
    pub shape: Vec<usize>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplayDataset {
    /// Contiguous row-major grayscale frames: `len == count * 400 * 400`.
    pub images: Vec<u8>,
    pub seq: Vec<u64>,
    pub times: Vec<[u64; 3]>,
    pub vendor_ns: Vec<u64>,
    pub raw_identity: Vec<u64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReplayFrame<'a> {
    pub image: &'a [u8],
    pub seq: u64,
    pub times: [u64; 3],
    pub vendor_ns: u64,
    pub raw_identity: u64,
}

impl ReplayDataset {
    pub fn len(&self) -> usize {
        self.seq.len()
    }

    pub fn is_empty(&self) -> bool {
        self.seq.is_empty()
    }

    pub fn image_slice(&self, index: usize) -> Option<&[u8]> {
        if index >= self.len() {
            return None;
        }
        let start = index * FRAME_PIXELS;
        self.images.get(start..start + FRAME_PIXELS)
    }

    pub fn frame(&self, index: usize) -> Option<ReplayFrame<'_>> {
        let image = self.image_slice(index)?;
        Some(ReplayFrame {
            image,
            seq: self.seq[index],
            times: self.times[index],
            vendor_ns: self.vendor_ns[index],
            raw_identity: self.raw_identity[index],
        })
    }

    pub fn iter(&self) -> impl Iterator<Item = ReplayFrame<'_>> {
        (0..self.len()).filter_map(move |i| self.frame(i))
    }
}

struct DictParser<'a> {
    input: &'a str,
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> DictParser<'a> {
    fn new(input: &'a str) -> Self {
        Self {
            input,
            bytes: input.as_bytes(),
            pos: 0,
        }
    }

    fn skip_ws(&mut self) {
        while self.pos < self.bytes.len() && self.bytes[self.pos].is_ascii_whitespace() {
            self.pos += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn expect_byte(&mut self, expected: u8) -> Result<(), String> {
        self.skip_ws();
        match self.peek() {
            Some(b) if b == expected => {
                self.pos += 1;
                Ok(())
            }
            Some(b) => Err(format!(
                "Invalid NPY header: expected '{}', got '{}'",
                expected as char, b as char
            )),
            None => Err(format!(
                "Invalid NPY header: unexpected end of header expecting '{}'",
                expected as char
            )),
        }
    }

    fn parse_string(&mut self) -> Result<String, String> {
        self.skip_ws();
        let quote = match self.peek() {
            Some(b'\'') | Some(b'"') => {
                let q = self.bytes[self.pos] as char;
                self.pos += 1;
                q
            }
            _ => return Err("Invalid NPY header: expected string literal".to_string()),
        };
        let mut out = String::new();
        while let Some(ch) = self.input[self.pos..].chars().next() {
            self.pos += ch.len_utf8();
            if ch == '\\' {
                let Some(esc) = self.input[self.pos..].chars().next() else {
                    return Err("Invalid NPY header: unterminated string escape".to_string());
                };
                self.pos += esc.len_utf8();
                match esc {
                    '\\' => out.push('\\'),
                    '\'' => out.push('\''),
                    '"' => out.push('"'),
                    'n' => out.push('\n'),
                    'r' => out.push('\r'),
                    't' => out.push('\t'),
                    other => out.push(other),
                }
            } else if ch == quote {
                return Ok(out);
            } else {
                out.push(ch);
            }
        }
        Err("Invalid NPY header: unterminated string literal".to_string())
    }

    fn parse_bool(&mut self) -> Result<bool, String> {
        self.skip_ws();
        let rest = &self.bytes[self.pos..];
        if rest.starts_with(b"True")
            && !rest.get(4).is_some_and(|b| b.is_ascii_alphanumeric() || *b == b'_')
        {
            self.pos += 4;
            Ok(true)
        } else if rest.starts_with(b"False")
            && !rest.get(5).is_some_and(|b| b.is_ascii_alphanumeric() || *b == b'_')
        {
            self.pos += 5;
            Ok(false)
        } else {
            Err("Invalid NPY header: expected boolean for fortran_order".to_string())
        }
    }

    fn parse_usize(&mut self) -> Result<usize, String> {
        self.skip_ws();
        let start = self.pos;
        while self.pos < self.bytes.len() && self.bytes[self.pos].is_ascii_digit() {
            self.pos += 1;
        }
        if self.pos == start {
            return Err("Invalid NPY header: expected unsigned integer in shape".to_string());
        }
        // Python 2/legacy numpy may append 'L' to long integers.
        let num_end = self.pos;
        if self.peek() == Some(b'L') {
            self.pos += 1;
        }
        let text = std::str::from_utf8(&self.bytes[start..num_end])
            .map_err(|_| "Invalid NPY header: non-UTF8 shape digit".to_string())?;
        text.parse::<usize>()
            .map_err(|_| format!("Invalid NPY header: shape dimension overflow ({text})"))
    }

    fn parse_shape_tuple(&mut self) -> Result<Vec<usize>, String> {
        self.expect_byte(b'(')?;
        let mut dims = Vec::new();
        loop {
            self.skip_ws();
            if self.peek() == Some(b')') {
                self.pos += 1;
                return Ok(dims);
            }
            dims.push(self.parse_usize()?);
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b')') => {
                    self.pos += 1;
                    return Ok(dims);
                }
                _ => {
                    return Err(
                        "Invalid NPY header: expected ',' or ')' in shape tuple".to_string()
                    )
                }
            }
        }
    }

    fn parse_header_dict(&mut self) -> Result<(String, bool, Vec<usize>), String> {
        self.expect_byte(b'{')?;
        let mut descr: Option<String> = None;
        let mut fortran_order: Option<bool> = None;
        let mut shape: Option<Vec<usize>> = None;

        loop {
            self.skip_ws();
            if self.peek() == Some(b'}') {
                self.pos += 1;
                break;
            }
            let key = self.parse_string()?;
            self.expect_byte(b':')?;
            match key.as_str() {
                "descr" => {
                    if descr.is_some() {
                        return Err("Invalid NPY header: duplicate 'descr' key".to_string());
                    }
                    descr = Some(self.parse_string()?);
                }
                "fortran_order" => {
                    if fortran_order.is_some() {
                        return Err(
                            "Invalid NPY header: duplicate 'fortran_order' key".to_string()
                        );
                    }
                    fortran_order = Some(self.parse_bool()?);
                }
                "shape" => {
                    if shape.is_some() {
                        return Err("Invalid NPY header: duplicate 'shape' key".to_string());
                    }
                    shape = Some(self.parse_shape_tuple()?);
                }
                other => {
                    return Err(format!("Invalid NPY header: unexpected key '{other}'"));
                }
            }
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b'}') => {
                    self.pos += 1;
                    break;
                }
                _ => {
                    return Err(
                        "Invalid NPY header: expected ',' or '}' after dict entry".to_string()
                    )
                }
            }
        }

        self.skip_ws();
        if self.pos != self.bytes.len() {
            return Err("Invalid NPY header: unexpected trailing tokens".to_string());
        }

        let descr = descr.ok_or_else(|| "Invalid NPY header: missing 'descr'".to_string())?;
        let fortran_order = fortran_order
            .ok_or_else(|| "Invalid NPY header: missing 'fortran_order'".to_string())?;
        let shape = shape.ok_or_else(|| "Invalid NPY header: missing 'shape'".to_string())?;
        Ok((descr, fortran_order, shape))
    }
}

/// Parse an `.npy` v1.0, v2.0, or v3.0 buffer, returning its validated header and raw data slice.
/// Rejects Fortran-order arrays, object/pickle dtypes, unsupported versions/dtypes, and length mismatches.
pub fn parse_npy(data: &[u8]) -> Result<(NpyHeader, &[u8]), String> {
    if data.len() < 10 || &data[0..6] != NPY_MAGIC {
        return Err("Invalid NPY file: missing \\x93NUMPY magic".to_string());
    }
    let major = data[6];
    let minor = data[7];
    let (header_len, header_start) = match (major, minor) {
        (1, 0) => {
            let len = u16::from_le_bytes([data[8], data[9]]) as usize;
            (len, 10usize)
        }
        (2, 0) | (3, 0) => {
            if data.len() < 12 {
                return Err("Invalid NPY v2/v3 file: truncated header length".to_string());
            }
            let len = u32::from_le_bytes([data[8], data[9], data[10], data[11]]) as usize;
            (len, 12usize)
        }
        _ => {
            return Err(format!(
                "Unsupported NPY version {major}.{minor} (expected 1.0, 2.0, or 3.0)"
            ))
        }
    };
    if header_len == 0 || header_len > MAX_NPY_HEADER_BYTES {
        return Err(format!("Invalid NPY header length: {header_len}"));
    }
    let payload_start = header_start
        .checked_add(header_len)
        .ok_or_else(|| "Invalid NPY header length overflow".to_string())?;
    if payload_start > data.len() {
        return Err("Invalid NPY file: header exceeds file length".to_string());
    }
    let header_bytes = &data[header_start..payload_start];
    let header_str = if major == 3 {
        std::str::from_utf8(header_bytes)
            .map_err(|_| "Invalid NPY v3.0 header: not valid UTF-8".to_string())?
            .to_string()
    } else {
        // NPY v1.0 / v2.0 specify ASCII/Latin-1 header strings.
        header_bytes.iter().map(|&b| b as char).collect()
    };

    let (descr, fortran_order, shape) = DictParser::new(&header_str).parse_header_dict()?;
    if fortran_order {
        return Err("Fortran-order NPY arrays are not supported (expected C-order)".to_string());
    }
    let dtype = NpyDtype::from_descr(&descr)?;

    let mut elem_count: usize = 1;
    for &dim in &shape {
        elem_count = elem_count
            .checked_mul(dim)
            .ok_or_else(|| "NPY shape element count overflow".to_string())?;
    }
    let expected_bytes = elem_count
        .checked_mul(dtype.item_size())
        .ok_or_else(|| "NPY payload byte size overflow".to_string())?;
    let payload = &data[payload_start..];
    if payload.len() != expected_bytes {
        return Err(format!(
            "Invalid NPY payload size: got {} bytes, expected {} bytes for shape {:?}",
            payload.len(),
            expected_bytes,
            shape
        ));
    }

    Ok((
        NpyHeader {
            version: (major, minor),
            dtype,
            descr,
            fortran_order,
            shape,
        },
        payload,
    ))
}

fn decode_u64_elements(
    dtype: NpyDtype,
    payload: &[u8],
    field_name: &str,
    allow_u32_i32: bool,
) -> Result<Vec<u64>, String> {
    let item = dtype.item_size();
    let count = payload.len() / item;
    let mut out = Vec::with_capacity(count);
    match dtype {
        NpyDtype::U64 => {
            for chunk in payload.chunks_exact(8) {
                out.push(u64::from_le_bytes(chunk.try_into().unwrap()));
            }
        }
        NpyDtype::I64 => {
            for chunk in payload.chunks_exact(8) {
                let v = i64::from_le_bytes(chunk.try_into().unwrap());
                let u = u64::try_from(v).map_err(|_| {
                    format!("Replay array '{field_name}' contains negative value {v}")
                })?;
                out.push(u);
            }
        }
        NpyDtype::U32 if allow_u32_i32 => {
            for chunk in payload.chunks_exact(4) {
                out.push(u32::from_le_bytes(chunk.try_into().unwrap()) as u64);
            }
        }
        NpyDtype::I32 if allow_u32_i32 => {
            for chunk in payload.chunks_exact(4) {
                let v = i32::from_le_bytes(chunk.try_into().unwrap());
                let u = u64::try_from(v).map_err(|_| {
                    format!("Replay array '{field_name}' contains negative value {v}")
                })?;
                out.push(u);
            }
        }
        other => {
            return Err(format!(
                "Unsupported dtype {other:?} for replay array '{field_name}'"
            ));
        }
    }
    Ok(out)
}

const MAX_IMAGES_ENTRY_BYTES: u64 = 2 * 1024 * 1024 * 1024;
const MAX_META_ENTRY_BYTES: u64 = 64 * 1024 * 1024;

fn read_bounded<R: Read>(
    reader: R,
    name: &str,
    declared_size: u64,
    max_bytes: u64,
) -> Result<Vec<u8>, String> {
    if declared_size > max_bytes {
        return Err(format!(
            "NPZ entry '{name}' uncompressed size {declared_size} exceeds limit of {max_bytes} bytes"
        ));
    }
    let cap = declared_size.min(64 * 1024 * 1024).min(max_bytes) as usize;
    let mut buf = Vec::with_capacity(cap);
    reader
        .take(max_bytes.saturating_add(1))
        .read_to_end(&mut buf)
        .map_err(|e| format!("Failed reading '{name}' from NPZ archive: {e}"))?;
    if (buf.len() as u64) > max_bytes {
        return Err(format!(
            "NPZ entry '{name}' decompressed size exceeds limit of {max_bytes} bytes"
        ));
    }
    Ok(buf)
}

fn read_zip_entry<R: Read + Seek>(
    archive: &mut ZipArchive<R>,
    name: &str,
    max_bytes: u64,
) -> Result<Option<Vec<u8>>, String> {
    match archive.by_name(name) {
        Ok(entry) => {
            let declared_size = entry.size();
            read_bounded(entry, name, declared_size, max_bytes).map(Some)
        }
        Err(zip::result::ZipError::FileNotFound) => Ok(None),
        Err(err) => Err(format!("Failed opening '{name}' in NPZ archive: {err}")),
    }
}

/// Load a replay `.npz` archive from disk without Python or pickle.
pub fn load_npz_replay(path: impl AsRef<Path>) -> Result<ReplayDataset, String> {
    let path = path.as_ref();
    let file = File::open(path)
        .map_err(|e| format!("Failed to open replay dataset {}: {e}", path.display()))?;
    load_npz_replay_from_reader(file)
}

/// Load a replay `.npz` archive from any seekable reader.
pub fn load_npz_replay_from_reader<R: Read + Seek>(reader: R) -> Result<ReplayDataset, String> {
    let mut archive =
        ZipArchive::new(reader).map_err(|e| format!("Invalid NPZ zip archive: {e}"))?;

    let images_raw = read_zip_entry(&mut archive, "images.npy", MAX_IMAGES_ENTRY_BYTES)?
        .ok_or_else(|| "Replay NPZ archive is missing required 'images.npy'".to_string())?;
    let seq_raw = read_zip_entry(&mut archive, "seq.npy", MAX_META_ENTRY_BYTES)?
        .ok_or_else(|| "Replay NPZ archive is missing required 'seq.npy'".to_string())?;
    let times_raw = read_zip_entry(&mut archive, "times.npy", MAX_META_ENTRY_BYTES)?
        .ok_or_else(|| "Replay NPZ archive is missing required 'times.npy'".to_string())?;
    let vendor_raw = read_zip_entry(&mut archive, "vendor_ns.npy", MAX_META_ENTRY_BYTES)?;
    let identity_raw = read_zip_entry(&mut archive, "raw_identity.npy", MAX_META_ENTRY_BYTES)?;

    let (img_hdr, img_payload) = parse_npy(&images_raw)?;
    if img_hdr.dtype != NpyDtype::U8 {
        return Err(format!(
            "Replay 'images.npy' must have uint8 (|u1) dtype, got {}",
            img_hdr.descr
        ));
    }
    if img_hdr.shape.len() != 3
        || img_hdr.shape[1] != FRAME_HEIGHT
        || img_hdr.shape[2] != FRAME_WIDTH
    {
        return Err(SHAPE_ERR.to_string());
    }
    let count = img_hdr.shape[0];

    let (seq_hdr, seq_payload) = parse_npy(&seq_raw)?;
    if seq_hdr.shape != [count] {
        return Err(SHAPE_ERR.to_string());
    }
    let seq = decode_u64_elements(seq_hdr.dtype, seq_payload, "seq", true)?;

    let (times_hdr, times_payload) = parse_npy(&times_raw)?;
    if times_hdr.shape != [count, 3] {
        return Err(SHAPE_ERR.to_string());
    }
    let times_flat = decode_u64_elements(times_hdr.dtype, times_payload, "times", true)?;
    let mut times = Vec::with_capacity(count);
    for chunk in times_flat.chunks_exact(3) {
        times.push([chunk[0], chunk[1], chunk[2]]);
    }

    let vendor_ns = match vendor_raw {
        Some(raw) => {
            let (hdr, payload) = parse_npy(&raw)?;
            if hdr.shape != [count] {
                return Err(format!(
                    "Expected replay vendor_ns[{count}], got {:?}",
                    hdr.shape
                ));
            }
            decode_u64_elements(hdr.dtype, payload, "vendor_ns", true)?
        }
        None => times.iter().map(|t| t[2]).collect(),
    };

    let raw_identity = match identity_raw {
        Some(raw) => {
            let (hdr, payload) = parse_npy(&raw)?;
            if hdr.shape != [count] {
                return Err(format!(
                    "Expected replay raw_identity[{count}], got {:?}",
                    hdr.shape
                ));
            }
            decode_u64_elements(hdr.dtype, payload, "raw_identity", true)?
        }
        None => seq.clone(),
    };

    Ok(ReplayDataset {
        images: img_payload.to_vec(),
        seq,
        times,
        vendor_ns,
        raw_identity,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Cursor, Write};
    use zip::write::SimpleFileOptions;
    use zip::{CompressionMethod, ZipWriter};

    struct NpyWriter;

    impl NpyWriter {
        fn encode(
            version: (u8, u8),
            descr: &str,
            fortran_order: bool,
            shape: &[usize],
            payload: &[u8],
        ) -> Vec<u8> {
            let shape_str = match shape.len() {
                0 => "()".to_string(),
                1 => format!("({},)", shape[0]),
                _ => {
                    let parts: Vec<String> = shape.iter().map(|d| d.to_string()).collect();
                    format!("({})", parts.join(", "))
                }
            };
            let order_str = if fortran_order { "True" } else { "False" };
            let mut header = format!(
                "{{'descr': '{descr}', 'fortran_order': {order_str}, 'shape': {shape_str}, }}"
            );
            let prefix_len = if version.0 == 1 { 10 } else { 12 };
            // Align header end to 64-byte boundary (matching numpy.lib.format).
            while (prefix_len + header.len() + 1) % 64 != 0 {
                header.push(' ');
            }
            header.push('\n');

            let mut out = Vec::with_capacity(prefix_len + header.len() + payload.len());
            out.extend_from_slice(NPY_MAGIC);
            out.push(version.0);
            out.push(version.1);
            if version.0 == 1 {
                out.extend_from_slice(&(header.len() as u16).to_le_bytes());
            } else {
                out.extend_from_slice(&(header.len() as u32).to_le_bytes());
            }
            out.extend_from_slice(header.as_bytes());
            out.extend_from_slice(payload);
            out
        }

        fn u8_array(version: (u8, u8), shape: &[usize], data: &[u8]) -> Vec<u8> {
            Self::encode(version, "|u1", false, shape, data)
        }

        fn u64_array(version: (u8, u8), shape: &[usize], data: &[u64]) -> Vec<u8> {
            let mut bytes = Vec::with_capacity(data.len() * 8);
            for &v in data {
                bytes.extend_from_slice(&v.to_le_bytes());
            }
            Self::encode(version, "<u8", false, shape, &bytes)
        }

        fn i64_array(version: (u8, u8), shape: &[usize], data: &[i64]) -> Vec<u8> {
            let mut bytes = Vec::with_capacity(data.len() * 8);
            for &v in data {
                bytes.extend_from_slice(&v.to_le_bytes());
            }
            Self::encode(version, "<i8", false, shape, &bytes)
        }

        fn u32_array(version: (u8, u8), shape: &[usize], data: &[u32]) -> Vec<u8> {
            let mut bytes = Vec::with_capacity(data.len() * 4);
            for &v in data {
                bytes.extend_from_slice(&v.to_le_bytes());
            }
            Self::encode(version, "<u4", false, shape, &bytes)
        }

        fn i32_array(version: (u8, u8), shape: &[usize], data: &[i32]) -> Vec<u8> {
            let mut bytes = Vec::with_capacity(data.len() * 4);
            for &v in data {
                bytes.extend_from_slice(&v.to_le_bytes());
            }
            Self::encode(version, "<i4", false, shape, &bytes)
        }
    }

    fn build_npz(entries: &[(&str, Vec<u8>)], compression: CompressionMethod) -> Vec<u8> {
        let mut cursor = Cursor::new(Vec::new());
        {
            let mut writer = ZipWriter::new(&mut cursor);
            let opts = SimpleFileOptions::default().compression_method(compression);
            for (name, data) in entries {
                writer.start_file(*name, opts).unwrap();
                writer.write_all(data).unwrap();
            }
            writer.finish().unwrap();
        }
        cursor.into_inner()
    }

    #[test]
    fn parse_npy_supports_v1_v2_v3_and_arbitrary_key_order() {
        for version in [(1, 0), (2, 0), (3, 0)] {
            let npy = NpyWriter::u64_array(version, &[3], &[10, 20, 30]);
            let (hdr, payload) = parse_npy(&npy).unwrap();
            assert_eq!(hdr.version, version);
            assert_eq!(hdr.dtype, NpyDtype::U64);
            assert_eq!(hdr.shape, vec![3]);
            assert!(!hdr.fortran_order);
            assert_eq!(payload.len(), 24);
        }

        // Non-standard key order, double quotes, and no trailing comma in dict.
        let header = "{\"shape\": (2,), \"fortran_order\": False, \"descr\": \"<i4\"}\n";
        let mut raw = Vec::new();
        raw.extend_from_slice(NPY_MAGIC);
        raw.extend_from_slice(&[1, 0]);
        raw.extend_from_slice(&(header.len() as u16).to_le_bytes());
        raw.extend_from_slice(header.as_bytes());
        raw.extend_from_slice(&7i32.to_le_bytes());
        raw.extend_from_slice(&9i32.to_le_bytes());
        let (hdr, payload) = parse_npy(&raw).unwrap();
        assert_eq!(hdr.dtype, NpyDtype::I32);
        assert_eq!(hdr.shape, vec![2]);
        assert_eq!(
            decode_u64_elements(hdr.dtype, payload, "seq", true).unwrap(),
            vec![7, 9]
        );
    }

    #[test]
    fn load_npz_end_to_end_defaults_and_deflated_archive() {
        let mut imgs = vec![0u8; 2 * FRAME_PIXELS];
        imgs[0] = 42;
        imgs[FRAME_PIXELS + 123] = 200;

        let npz_bytes = build_npz(
            &[
                ("images.npy", NpyWriter::u8_array((1, 0), &[2, 400, 400], &imgs)),
                ("seq.npy", NpyWriter::i32_array((2, 0), &[2], &[100, 101])),
                (
                    "times.npy",
                    NpyWriter::i64_array(
                        (3, 0),
                        &[2, 3],
                        &[10, 20, 30_000, 40, 50, 41_111],
                    ),
                ),
            ],
            CompressionMethod::Deflated,
        );

        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("sample.npz");
        std::fs::write(&path, &npz_bytes).unwrap();

        let ds = load_npz_replay(&path).unwrap();
        assert_eq!(ds.len(), 2);
        assert!(!ds.is_empty());
        assert_eq!(ds.seq, vec![100, 101]);
        assert_eq!(ds.times, vec![[10, 20, 30_000], [40, 50, 41_111]]);
        // Optional arrays default to times[:, 2] and seq.
        assert_eq!(ds.vendor_ns, vec![30_000, 41_111]);
        assert_eq!(ds.raw_identity, vec![100, 101]);

        let f0 = ds.frame(0).unwrap();
        assert_eq!(f0.image.len(), FRAME_PIXELS);
        assert_eq!(f0.image[0], 42);
        assert_eq!(f0.vendor_ns, 30_000);

        let f1 = ds.frame(1).unwrap();
        assert_eq!(f1.image[123], 200);
        assert_eq!(f1.raw_identity, 101);
        assert!(ds.frame(2).is_none());
        assert_eq!(ds.iter().count(), 2);
    }

    #[test]
    fn load_npz_explicit_vendor_ns_and_raw_identity_and_empty_dataset() {
        let imgs = vec![7u8; FRAME_PIXELS];
        let npz_bytes = build_npz(
            &[
                ("images.npy", NpyWriter::u8_array((1, 0), &[1, 400, 400], &imgs)),
                ("seq.npy", NpyWriter::u32_array((1, 0), &[1], &[55])),
                ("times.npy", NpyWriter::u64_array((1, 0), &[1, 3], &[1, 2, 3])),
                ("vendor_ns.npy", NpyWriter::u64_array((1, 0), &[1], &[9999])),
                (
                    "raw_identity.npy",
                    NpyWriter::i64_array((1, 0), &[1], &[7777]),
                ),
            ],
            CompressionMethod::Stored,
        );
        let ds = load_npz_replay_from_reader(Cursor::new(npz_bytes)).unwrap();
        assert_eq!(ds.vendor_ns, vec![9999]);
        assert_eq!(ds.raw_identity, vec![7777]);

        // Empty (N = 0) dataset is structurally valid.
        let empty_npz = build_npz(
            &[
                ("images.npy", NpyWriter::u8_array((1, 0), &[0, 400, 400], &[])),
                ("seq.npy", NpyWriter::u64_array((1, 0), &[0], &[])),
                ("times.npy", NpyWriter::u64_array((1, 0), &[0, 3], &[])),
            ],
            CompressionMethod::Stored,
        );
        let empty = load_npz_replay_from_reader(Cursor::new(empty_npz)).unwrap();
        assert!(empty.is_empty());
        assert_eq!(empty.len(), 0);
    }

    #[test]
    fn rejects_fortran_order_object_dtype_bad_shapes_and_mismatched_lengths() {
        // 1. Fortran-order rejected.
        let fortran = NpyWriter::encode((1, 0), "<u8", true, &[1, 3], &[0u8; 24]);
        let err = parse_npy(&fortran).unwrap_err();
        assert!(err.contains("Fortran"), "unexpected error: {err}");

        // 2. Object / pickle dtype rejected.
        let obj = NpyWriter::encode((1, 0), "|O", false, &[1], &[0u8; 8]);
        let err = parse_npy(&obj).unwrap_err();
        assert!(err.contains("pickle") || err.contains("Object"), "unexpected error: {err}");

        // 3. Big-endian dtype rejected.
        let be = NpyWriter::encode((1, 0), ">u8", false, &[1], &[0u8; 8]);
        assert!(parse_npy(&be).is_err());

        // 4. Malformed images shape (not 400x400).
        let bad_img_npz = build_npz(
            &[
                (
                    "images.npy",
                    NpyWriter::u8_array((1, 0), &[1, 320, 400], &vec![0u8; 320 * 400]),
                ),
                ("seq.npy", NpyWriter::u64_array((1, 0), &[1], &[1])),
                ("times.npy", NpyWriter::u64_array((1, 0), &[1, 3], &[1, 2, 3])),
            ],
            CompressionMethod::Stored,
        );
        let err = load_npz_replay_from_reader(Cursor::new(bad_img_npz)).unwrap_err();
        assert_eq!(err, SHAPE_ERR);

        // 5. Mismatched seq length.
        let bad_len_npz = build_npz(
            &[
                (
                    "images.npy",
                    NpyWriter::u8_array((1, 0), &[1, 400, 400], &vec![0u8; FRAME_PIXELS]),
                ),
                ("seq.npy", NpyWriter::u64_array((1, 0), &[2], &[1, 2])),
                ("times.npy", NpyWriter::u64_array((1, 0), &[1, 3], &[1, 2, 3])),
            ],
            CompressionMethod::Stored,
        );
        let err = load_npz_replay_from_reader(Cursor::new(bad_len_npz)).unwrap_err();
        assert_eq!(err, SHAPE_ERR);

        // 6. Malformed times shape ([N, 2] instead of [N, 3]).
        let bad_times_npz = build_npz(
            &[
                (
                    "images.npy",
                    NpyWriter::u8_array((1, 0), &[1, 400, 400], &vec![0u8; FRAME_PIXELS]),
                ),
                ("seq.npy", NpyWriter::u64_array((1, 0), &[1], &[1])),
                ("times.npy", NpyWriter::u64_array((1, 0), &[1, 2], &[1, 2])),
            ],
            CompressionMethod::Stored,
        );
        let err = load_npz_replay_from_reader(Cursor::new(bad_times_npz)).unwrap_err();
        assert_eq!(err, SHAPE_ERR);

        // 7. Mismatched optional vendor_ns length.
        let bad_vendor_npz = build_npz(
            &[
                (
                    "images.npy",
                    NpyWriter::u8_array((1, 0), &[1, 400, 400], &vec![0u8; FRAME_PIXELS]),
                ),
                ("seq.npy", NpyWriter::u64_array((1, 0), &[1], &[1])),
                ("times.npy", NpyWriter::u64_array((1, 0), &[1, 3], &[1, 2, 3])),
                ("vendor_ns.npy", NpyWriter::u64_array((1, 0), &[2], &[1, 2])),
            ],
            CompressionMethod::Stored,
        );
        assert!(load_npz_replay_from_reader(Cursor::new(bad_vendor_npz)).is_err());

        // 8. Negative signed value rejected.
        let neg_npz = build_npz(
            &[
                (
                    "images.npy",
                    NpyWriter::u8_array((1, 0), &[1, 400, 400], &vec![0u8; FRAME_PIXELS]),
                ),
                ("seq.npy", NpyWriter::i64_array((1, 0), &[1], &[-1])),
                ("times.npy", NpyWriter::u64_array((1, 0), &[1, 3], &[1, 2, 3])),
            ],
            CompressionMethod::Stored,
        );
        assert!(load_npz_replay_from_reader(Cursor::new(neg_npz)).is_err());

        // 9. Missing required entry rejected.
        let missing_npz = build_npz(
            &[
                (
                    "images.npy",
                    NpyWriter::u8_array((1, 0), &[1, 400, 400], &vec![0u8; FRAME_PIXELS]),
                ),
                ("seq.npy", NpyWriter::u64_array((1, 0), &[1], &[1])),
            ],
            CompressionMethod::Stored,
        );
        assert!(load_npz_replay_from_reader(Cursor::new(missing_npz)).is_err());

        // 10. Truncated NPY payload rejected.
        let mut truncated = NpyWriter::u64_array((1, 0), &[2], &[1, 2]);
        truncated.pop();
        assert!(parse_npy(&truncated).is_err());
    }

    #[test]
    fn zip_decompression_cap_bounds_entry_and_stream_overflow() {
        // Compress 64 KiB of zeros (tiny compressed payload) and verify strict cap.
        let payload = vec![0u8; 64 * 1024];
        let npz_bytes = build_npz(&[("bomb.npy", payload.clone())], CompressionMethod::Deflated);
        let mut archive = ZipArchive::new(Cursor::new(npz_bytes)).unwrap();

        // Exact limit succeeds.
        let ok = read_zip_entry(&mut archive, "bomb.npy", 64 * 1024)
            .unwrap()
            .unwrap();
        assert_eq!(ok.len(), 64 * 1024);

        // One byte below uncompressed size fails cleanly.
        let err = read_zip_entry(&mut archive, "bomb.npy", 64 * 1024 - 1).unwrap_err();
        assert!(err.contains("exceeds limit"), "unexpected error: {err}");

        // Stream overflow when declared_size lies (forged small header) is caught by Read::take.
        let err = read_bounded(Cursor::new(payload), "forged.npy", 16, 1024).unwrap_err();
        assert!(
            err.contains("decompressed size exceeds limit"),
            "unexpected error: {err}"
        );

        // Exact boundary on read_bounded succeeds.
        let ok = read_bounded(Cursor::new(vec![7u8; 1024]), "exact.npy", 0, 1024).unwrap();
        assert_eq!(ok.len(), 1024);
    }

    #[test]
    fn npy_v3_utf8_string_decoding_and_32bit_times() {
        // 1. DictParser::parse_string preserves multi-byte UTF-8 without Latin-1 mojibake.
        let mut parser = DictParser::new("'模型-αβγ\\n测试'");
        assert_eq!(parser.parse_string().unwrap(), "模型-αβγ\n测试");

        // 2. NPY v3.0 header with non-ASCII UTF-8 key preserves exact UTF-8 in error message.
        let header = "{'描述': '测试', 'descr': '<u8', 'fortran_order': False, 'shape': (1,)}\n";
        let mut raw = Vec::new();
        raw.extend_from_slice(NPY_MAGIC);
        raw.extend_from_slice(&[3, 0]);
        raw.extend_from_slice(&(header.len() as u32).to_le_bytes());
        raw.extend_from_slice(header.as_bytes());
        raw.extend_from_slice(&1u64.to_le_bytes());
        let err = parse_npy(&raw).unwrap_err();
        assert!(
            err.contains("unexpected key '描述'"),
            "expected preserved UTF-8 key in error, got: {err}"
        );

        // 3. times.npy accepts u32 and i32 synthetic fixtures (and rejects negative i32).
        let imgs = vec![11u8; 2 * FRAME_PIXELS];
        let u32_npz = build_npz(
            &[
                ("images.npy", NpyWriter::u8_array((1, 0), &[2, 400, 400], &imgs)),
                ("seq.npy", NpyWriter::u32_array((1, 0), &[2], &[1, 2])),
                (
                    "times.npy",
                    NpyWriter::u32_array((3, 0), &[2, 3], &[10, 20, 30, 40, 50, 60]),
                ),
            ],
            CompressionMethod::Stored,
        );
        let ds_u32 = load_npz_replay_from_reader(Cursor::new(u32_npz)).unwrap();
        assert_eq!(ds_u32.times, vec![[10, 20, 30], [40, 50, 60]]);
        assert_eq!(ds_u32.vendor_ns, vec![30, 60]);

        let i32_npz = build_npz(
            &[
                ("images.npy", NpyWriter::u8_array((1, 0), &[1, 400, 400], &vec![0u8; FRAME_PIXELS])),
                ("seq.npy", NpyWriter::i32_array((1, 0), &[1], &[5])),
                (
                    "times.npy",
                    NpyWriter::i32_array((3, 0), &[1, 3], &[100, 200, 300]),
                ),
            ],
            CompressionMethod::Stored,
        );
        let ds_i32 = load_npz_replay_from_reader(Cursor::new(i32_npz)).unwrap();
        assert_eq!(ds_i32.times, vec![[100, 200, 300]]);

        let neg_i32_npz = build_npz(
            &[
                ("images.npy", NpyWriter::u8_array((1, 0), &[1, 400, 400], &vec![0u8; FRAME_PIXELS])),
                ("seq.npy", NpyWriter::i32_array((1, 0), &[1], &[5])),
                (
                    "times.npy",
                    NpyWriter::i32_array((3, 0), &[1, 3], &[100, -1, 300]),
                ),
            ],
            CompressionMethod::Stored,
        );
        assert!(load_npz_replay_from_reader(Cursor::new(neg_i32_npz)).is_err());
    }
}
