//! Per-valid-channel One-Euro filtering on the image sensor clock.
//!
//! Exact Rust port of `pc-studio/face_filter.py`.

use crate::face_schema::CHANNELS;
use crate::wire::FrameHeader;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::BTreeMap;
use std::f64::consts::PI;

/// Filter configuration parameters matching `pc-studio/face_filter.py`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FilterConfig {
    pub filter_enabled: Option<bool>,
    pub filter_min_cutoff: Option<f64>,
    pub filter_beta: f64,
    pub filter_d_cutoff: f64,
    pub smoothing_ms: f64,
    pub stale_ms: f64,
}

impl Default for FilterConfig {
    fn default() -> Self {
        Self {
            filter_enabled: Some(true),
            filter_min_cutoff: Some(8.0),
            filter_beta: 4.0,
            filter_d_cutoff: 1.0,
            smoothing_ms: 20.0,
            stale_ms: 350.0,
        }
    }
}

impl FilterConfig {
    /// Legacy smoothing-ms-only configuration (without explicit `filter_min_cutoff`).
    pub fn from_smoothing_ms(smoothing_ms: f64, stale_ms: f64) -> Self {
        Self {
            filter_enabled: None,
            filter_min_cutoff: None,
            filter_beta: 4.0,
            filter_d_cutoff: 1.0,
            smoothing_ms,
            stale_ms,
        }
    }

    /// Construct from a JSON config object matching Python dict lookup semantics.
    pub fn from_json(config: &Value) -> Self {
        let obj = config.as_object();
        let filter_enabled = obj
            .and_then(|m| m.get("filter_enabled"))
            .and_then(|v| v.as_bool());
        let filter_min_cutoff = obj
            .and_then(|m| m.get("filter_min_cutoff"))
            .and_then(|v| v.as_f64());
        let filter_beta = obj
            .and_then(|m| m.get("filter_beta"))
            .and_then(|v| v.as_f64())
            .unwrap_or(4.0);
        let filter_d_cutoff = obj
            .and_then(|m| m.get("filter_d_cutoff"))
            .and_then(|v| v.as_f64())
            .unwrap_or(1.0);
        let smoothing_ms = obj
            .and_then(|m| m.get("smoothing_ms"))
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0);
        let stale_ms = obj
            .and_then(|m| m.get("stale_ms"))
            .and_then(|v| v.as_f64())
            .unwrap_or(350.0);
        Self {
            filter_enabled,
            filter_min_cutoff,
            filter_beta,
            filter_d_cutoff,
            smoothing_ms,
            stale_ms,
        }
    }
}

/// Resolved One-Euro filter parameters `(enabled, min_cutoff, beta, d_cutoff)`.
pub fn resolve_filter_params(config: &FilterConfig) -> (bool, f64, f64, f64) {
    if config.filter_enabled == Some(false) {
        return (false, 0.0, 0.0, 1.0);
    }
    if let Some(min_cutoff) = config.filter_min_cutoff {
        let beta = config.filter_beta;
        let d_cutoff = config.filter_d_cutoff;
        if min_cutoff <= 0.0 {
            return (false, 0.0, beta, d_cutoff);
        }
        return (true, min_cutoff, beta, d_cutoff);
    }
    if config.smoothing_ms <= 0.0 {
        return (false, 0.0, 4.0, 1.0);
    }
    (true, 1000.0 / (2.0 * PI * config.smoothing_ms), 4.0, 1.0)
}

fn canonical_f64_bits(v: f64) -> u64 {
    if v == 0.0 {
        0.0f64.to_bits()
    } else {
        v.to_bits()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct FilterKey {
    session_id: u64,
    valid_mask: u64,
    enabled: bool,
    min_cutoff_bits: u64,
    beta_bits: u64,
    d_cutoff_bits: u64,
}

/// Filtered output in `f64` precision (exact match to Python floats).
#[derive(Debug, Clone, PartialEq)]
pub struct FilteredFaceF64 {
    pub raw_values: [f64; 50],
    pub raw_channels: BTreeMap<String, f64>,
    pub values: [f64; 50],
    pub channels: BTreeMap<String, f64>,
    pub valid_mask: u64,
}

/// Filtered output in `f32` precision (ready for `FacePrediction` / UDP packets).
#[derive(Debug, Clone, PartialEq)]
pub struct FilteredFace {
    pub raw_values: [f32; 50],
    pub raw_channels: BTreeMap<String, f32>,
    pub values: [f32; 50],
    pub channels: BTreeMap<String, f32>,
    pub valid_mask: u64,
}

/// Per-valid-channel One-Euro filter operating on the mouth image sensor clock.
#[derive(Debug, Default, Clone)]
pub struct FaceFilter {
    key: Option<FilterKey>,
    source_ns: Option<u64>,
    values: Option<[f64; 50]>,
    raw: Option<[f64; 50]>,
    derivative: Option<[f64; 50]>,
}

impl FaceFilter {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn clear(&mut self) {
        self.key = None;
        self.source_ns = None;
        self.values = None;
        self.raw = None;
        self.derivative = None;
    }

    pub fn has_state(&self) -> bool {
        self.values.is_some()
    }

    pub fn alpha(cutoff: f64, dt: f64) -> f64 {
        1.0 / (1.0 + 1.0 / (2.0 * PI * cutoff * dt))
    }

    /// Apply One-Euro filtering in `f64` precision (exact match to `face_filter.py`).
    pub fn apply_f64(
        &mut self,
        raw: &[f64; 50],
        valid_mask: u64,
        header: &FrameHeader,
        config: &FilterConfig,
    ) -> FilteredFaceF64 {
        let (enabled, min_cutoff, beta, d_cutoff) = resolve_filter_params(config);
        let key = FilterKey {
            session_id: header.session_id,
            valid_mask,
            enabled,
            min_cutoff_bits: canonical_f64_bits(min_cutoff),
            beta_bits: canonical_f64_bits(beta),
            d_cutoff_bits: canonical_f64_bits(d_cutoff),
        };
        let source = header.mouth_sensor_ns;
        let dt = match self.source_ns {
            Some(prev) => (source as i128 - prev as i128) as f64 / 1e9,
            None => 0.0,
        };
        let reset = self.key != Some(key) || dt <= 0.0 || dt * 1000.0 > config.stale_ms;

        if valid_mask == 0 {
            self.clear();
        } else if !enabled || reset {
            self.values = Some(*raw);
            self.raw = Some(*raw);
            self.derivative = Some([0.0; 50]);
        } else {
            let derivative_alpha = Self::alpha(d_cutoff, dt);
            let prev_raw = self.raw.as_ref().expect("filter state initialized");
            let deriv = self.derivative.as_mut().expect("filter state initialized");
            let vals = self.values.as_mut().expect("filter state initialized");
            for i in 0..50 {
                if (valid_mask & (1u64 << i)) == 0 {
                    continue;
                }
                let value = raw[i];
                let velocity = (value - prev_raw[i]) / dt;
                deriv[i] += derivative_alpha * (velocity - deriv[i]);
                let cutoff = min_cutoff + beta * deriv[i].abs();
                vals[i] += Self::alpha(cutoff, dt) * (value - vals[i]);
            }
            self.raw = Some(*raw);
        }

        let values = if valid_mask != 0 {
            self.values.unwrap_or(*raw)
        } else {
            *raw
        };
        if valid_mask != 0 {
            self.key = Some(key);
            self.source_ns = Some(source);
        }

        let mut raw_channels = BTreeMap::new();
        let mut channels = BTreeMap::new();
        for (i, &name) in CHANNELS.iter().enumerate() {
            if (valid_mask & (1u64 << i)) != 0 {
                raw_channels.insert(name.to_string(), raw[i]);
                channels.insert(name.to_string(), values[i]);
            }
        }

        FilteredFaceF64 {
            raw_values: *raw,
            raw_channels,
            values,
            channels,
            valid_mask,
        }
    }

    /// Apply One-Euro filtering to `[f32; 50]` values (computing internally in `f64`).
    pub fn apply(
        &mut self,
        raw: &[f32; 50],
        valid_mask: u64,
        header: &FrameHeader,
        config: &FilterConfig,
    ) -> FilteredFace {
        let mut raw64 = [0.0f64; 50];
        for i in 0..50 {
            raw64[i] = raw[i] as f64;
        }
        let out64 = self.apply_f64(&raw64, valid_mask, header, config);
        let mut values = [0.0f32; 50];
        for i in 0..50 {
            values[i] = out64.values[i] as f32;
        }
        let mut raw_channels = BTreeMap::new();
        let mut channels = BTreeMap::new();
        for (i, &name) in CHANNELS.iter().enumerate() {
            if (valid_mask & (1u64 << i)) != 0 {
                raw_channels.insert(name.to_string(), raw[i]);
                channels.insert(name.to_string(), values[i]);
            }
        }
        FilteredFace {
            raw_values: *raw,
            raw_channels,
            values,
            channels,
            valid_mask,
        }
    }

    /// Apply filtering onto a JSON prediction dictionary matching `face_filter.py`'s dict return.
    pub fn apply_json(
        &mut self,
        prediction: &Value,
        header: &FrameHeader,
        config: &FilterConfig,
    ) -> Value {
        let mut out = prediction.clone();
        let mut raw64 = [0.0f64; 50];
        if let Some(arr) = prediction.get("values").and_then(|v| v.as_array()) {
            for (i, v) in arr.iter().take(50).enumerate() {
                raw64[i] = v.as_f64().unwrap_or(0.0);
            }
        }
        let valid_mask = prediction
            .get("valid_mask")
            .and_then(|v| v.as_u64())
            .unwrap_or(0);
        let filtered = self.apply_f64(&raw64, valid_mask, header, config);
        if let Some(obj) = out.as_object_mut() {
            let raw_channels = prediction
                .get("channels")
                .cloned()
                .unwrap_or_else(|| Value::Object(Map::new()));
            let mut ch_map = Map::new();
            for (k, v) in filtered.channels {
                ch_map.insert(k, Value::from(v));
            }
            obj.insert(
                "raw_values".to_string(),
                Value::Array(filtered.raw_values.iter().map(|&v| Value::from(v)).collect()),
            );
            obj.insert("raw_channels".to_string(), raw_channels);
            obj.insert(
                "values".to_string(),
                Value::Array(filtered.values.iter().map(|&v| Value::from(v)).collect()),
            );
            obj.insert("channels".to_string(), Value::Object(ch_map));
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::face_schema::channel_index;

    fn make_header(session_id: u64, ms: f64) -> FrameHeader {
        let ns = (ms * 1e6) as u64;
        FrameHeader {
            payload_bytes: 100,
            flags: 1,
            session_id,
            source_seq: 1,
            sensor_ns: ns,
            vendor_ns: ns,
            copy_mono_ns: ns,
            encode_begin_ns: ns,
            encode_end_ns: ns,
            emit_mono_ns: ns,
            eye0_sensor_ns: ns,
            eye1_sensor_ns: ns,
            mouth_sensor_ns: ns,
            raw_identity: 1,
            width: 400,
            height: 400,
            raw_crc32: 0,
            quality: 95,
        }
    }

    fn apply_frame(
        filter: &mut FaceFilter,
        config: &FilterConfig,
        value: f64,
        ms: f64,
        mask: u64,
        session: u64,
    ) -> FilteredFaceF64 {
        let mut values = [0.0f64; 50];
        for i in 0..50 {
            if (mask & (1u64 << i)) != 0 {
                values[i] = value;
            }
        }
        let header = make_header(session, ms);
        filter.apply_f64(&values, mask, &header, config)
    }

    #[test]
    fn zero_smoothing_is_exact_passthrough_including_valid_zero() {
        let mut filter = FaceFilter::new();
        let config = FilterConfig::from_smoothing_ms(0.0, 350.0);
        for (idx, &value) in [0.0, 0.123456789, 1.0, 0.01, 0.0].iter().enumerate() {
            let result = apply_frame(&mut filter, &config, value, (idx * 11) as f64, 1, 1);
            assert_eq!(result.values, result.raw_values);
            assert_eq!(result.values[0], value);
        }
    }

    #[test]
    fn actual_sensor_dt_not_assumed_90hz() {
        let mut filter = FaceFilter::new();
        let config = FilterConfig::from_smoothing_ms(20.0, 350.0);
        apply_frame(&mut filter, &config, 0.0, 1000.0, 1, 1);
        let short = apply_frame(&mut filter, &config, 1.0, 1010.0, 1, 1).values[0];
        filter.clear();
        apply_frame(&mut filter, &config, 0.0, 1000.0, 1, 1);
        let long = apply_frame(&mut filter, &config, 1.0, 1040.0, 1, 1).values[0];
        assert!(long > short);
        assert!(long < 1.0);
    }

    #[test]
    fn mask_session_gap_clock_reset_and_settings_change_restart_at_raw() {
        let mut filter = FaceFilter::new();
        for change in [
            "mask",
            "session",
            "gap",
            "backwards",
            "duplicate",
            "config",
            "clear",
        ] {
            let mut config = FilterConfig::from_smoothing_ms(20.0, 350.0);
            filter.clear();
            apply_frame(&mut filter, &config, 0.0, 1000.0, 1, 1);
            assert!(apply_frame(&mut filter, &config, 1.0, 1011.0, 1, 1).values[0] < 1.0);
            let mut ms = 1022.0;
            let mut mask = 1u64;
            let mut session = 1u64;
            match change {
                "mask" => mask = 3,
                "session" => session = 2,
                "gap" => ms = 1500.0,
                "backwards" => ms = 1000.0,
                "duplicate" => ms = 1011.0,
                "config" => config.smoothing_ms += 1.0,
                "clear" => filter.clear(),
                _ => unreachable!(),
            }
            let result = apply_frame(&mut filter, &config, 0.4, ms, mask, session);
            assert_eq!(result.values[0], 0.4, "failed reset case: {change}");
        }
    }

    #[test]
    fn unknown_channels_and_mask_zero_cannot_keep_state() {
        let mut filter = FaceFilter::new();
        let config = FilterConfig::from_smoothing_ms(20.0, 350.0);
        let jaw = channel_index("JawOpen").unwrap();
        let tongue = channel_index("TongueOut").unwrap();
        apply_frame(&mut filter, &config, 0.7, 1000.0, 1u64 << jaw, 1);
        let result = apply_frame(&mut filter, &config, 0.0, 1011.0, 0, 1);
        assert_eq!(result.values, [0.0; 50]);
        assert!(!filter.has_state());
        let result = apply_frame(&mut filter, &config, 0.2, 1022.0, 1u64 << jaw, 1);
        assert_eq!(result.values[jaw], 0.2);
        assert_eq!(result.values[tongue], 0.0);
    }

    #[test]
    fn rest_noise_reduced_and_real_step_not_discarded() {
        let mut filter = FaceFilter::new();
        let config = FilterConfig::from_smoothing_ms(20.0, 350.0);
        let mut raw = Vec::new();
        let mut filtered = Vec::new();
        for i in 0..120 {
            let value = 0.5 + if i % 2 == 1 { 0.02 } else { -0.02 };
            let result = apply_frame(
                &mut filter,
                &config,
                value,
                1000.0 + (i as f64) * 1000.0 / 90.0,
                1,
                1,
            );
            if i > 30 {
                raw.push(value);
                filtered.push(result.values[0]);
            }
        }
        let raw_span = raw.iter().copied().fold(f64::NEG_INFINITY, f64::max)
            - raw.iter().copied().fold(f64::INFINITY, f64::min);
        let filt_span = filtered.iter().copied().fold(f64::NEG_INFINITY, f64::max)
            - filtered.iter().copied().fold(f64::INFINITY, f64::min);
        assert!(filt_span < 0.5 * raw_span);
        let step = apply_frame(
            &mut filter,
            &config,
            1.0,
            1000.0 + 120.0 * 1000.0 / 90.0,
            1,
            1,
        );
        assert!(step.values[0] > 0.75);
        assert_eq!(step.raw_values[0], 1.0);
    }

    #[test]
    fn explicit_one_euro_params_and_toggle() {
        let mut filter = FaceFilter::new();
        let mut config = FilterConfig {
            filter_enabled: Some(false),
            filter_min_cutoff: Some(1.0),
            filter_beta: 0.0,
            filter_d_cutoff: 1.0,
            smoothing_ms: 20.0,
            stale_ms: 350.0,
        };
        apply_frame(&mut filter, &config, 0.0, 1000.0, 1, 1);
        assert_eq!(apply_frame(&mut filter, &config, 0.9, 1011.0, 1, 1).values[0], 0.9);

        filter.clear();
        config.filter_enabled = Some(true);
        apply_frame(&mut filter, &config, 0.0, 1000.0, 1, 1);
        let heavy = apply_frame(&mut filter, &config, 1.0, 1011.0, 1, 1).values[0];

        filter.clear();
        config.filter_min_cutoff = Some(8.0);
        config.filter_beta = 10.0;
        config.filter_d_cutoff = 2.0;
        apply_frame(&mut filter, &config, 0.0, 1000.0, 1, 1);
        let snappy = apply_frame(&mut filter, &config, 1.0, 1011.0, 1, 1).values[0];
        assert!(heavy < snappy);
        assert!(snappy < 1.0);

        config.filter_beta = 2.0;
        assert_eq!(apply_frame(&mut filter, &config, 0.3, 1022.0, 1, 1).values[0], 0.3);
        config.filter_d_cutoff = 0.5;
        assert_eq!(apply_frame(&mut filter, &config, 0.6, 1033.0, 1, 1).values[0], 0.6);
    }
}
