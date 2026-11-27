//! Source-time tongue smoothing; absent and invalid end visual ownership.
//!
//! Exact Rust port of `pc-studio/state.py`.

use crate::wire::FrameHeader;
use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Runtime smoothing and uncertain-hold configuration for `TongueState`.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct TongueStateConfig {
    pub smoothing_ms: f64,
    pub uncertain_hold_ms: f64,
}

impl Default for TongueStateConfig {
    fn default() -> Self {
        Self {
            smoothing_ms: 20.0,
            uncertain_hold_ms: 100.0,
        }
    }
}

impl TongueStateConfig {
    pub fn from_json(config: &Value) -> Self {
        let obj = config.as_object();
        Self {
            smoothing_ms: obj
                .and_then(|m| m.get("smoothing_ms"))
                .and_then(|v| v.as_f64())
                .unwrap_or(20.0),
            uncertain_hold_ms: obj
                .and_then(|m| m.get("uncertain_hold_ms"))
                .and_then(|v| v.as_f64())
                .unwrap_or(100.0),
        }
    }
}

/// Raw tongue prediction fed into `TongueState::update`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RawTongueInput {
    pub state: Option<String>,
    pub present: bool,
    pub left: f64,
    pub right: f64,
    pub out: f64,
    pub extended: bool,
    pub up: Option<f64>,
    pub down: Option<f64>,
    pub roll: Option<f64>,
    pub out_hysteresis: Option<[f64; 2]>,
}

impl Default for RawTongueInput {
    fn default() -> Self {
        Self {
            state: None,
            present: false,
            left: 0.0,
            right: 0.0,
            out: 0.0,
            extended: false,
            up: None,
            down: None,
            roll: None,
            out_hysteresis: None,
        }
    }
}

/// Smoothed tongue prediction output matching `pc-studio/state.py`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TonguePrediction {
    pub state: String,
    pub fresh: bool,
    pub present: bool,
    pub left: f64,
    pub right: f64,
    pub out: Option<f64>,
    #[serde(rename = "override")]
    pub override_active: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub up: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub down: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub roll: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub held: Option<bool>,
}

/// Source-time tongue smoother and presence hysteresis tracker.
#[derive(Debug, Default, Clone)]
pub struct TongueState {
    pub session: Option<u64>,
    pub source_ns: Option<u64>,
    pub visual: Option<TonguePrediction>,
    pub visual_ns: Option<u64>,
}

impl TongueState {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn clear(&mut self) {
        self.session = None;
        self.source_ns = None;
        self.visual = None;
        self.visual_ns = None;
    }

    pub fn is_visual_active(&self) -> bool {
        self.visual.is_some()
    }

    pub fn invalid(reason: &str) -> TonguePrediction {
        TonguePrediction {
            state: reason.to_string(),
            fresh: false,
            present: false,
            left: 0.0,
            right: 0.0,
            out: None,
            override_active: false,
            up: None,
            down: None,
            roll: None,
            held: None,
        }
    }

    pub fn update(
        &mut self,
        raw: &RawTongueInput,
        header: &FrameHeader,
        config: &TongueStateConfig,
    ) -> TonguePrediction {
        if self.session != Some(header.session_id) {
            self.clear();
            self.session = Some(header.session_id);
        }
        let source_ns = header.mouth_sensor_ns;
        let explicit_state = raw.state.as_deref().filter(|s| !s.is_empty());
        let state = if let Some(s) = explicit_state {
            s
        } else if let Some([appear, hold]) = raw.out_hysteresis {
            let threshold = if self.visual.is_some() { hold } else { appear };
            if raw.out >= threshold {
                "present"
            } else {
                "absent"
            }
        } else if raw.present {
            "present"
        } else {
            "absent"
        };

        let old_ns = self.source_ns;
        self.source_ns = Some(source_ns);

        if state == "invalid" || state == "stale" {
            self.visual = None;
            self.visual_ns = None;
            return Self::invalid(state);
        }
        if state == "absent" {
            self.visual = None;
            self.visual_ns = None;
            return TonguePrediction {
                state: "absent".to_string(),
                fresh: true,
                present: false,
                left: 0.0,
                right: 0.0,
                out: Some(0.0),
                override_active: true,
                up: if raw.extended { Some(0.0) } else { None },
                down: if raw.extended { Some(0.0) } else { None },
                roll: if raw.extended { Some(0.0) } else { None },
                held: None,
            };
        }
        if state == "uncertain" {
            let hold_ns = (config.uncertain_hold_ms * 1e6) as i128;
            if let (Some(visual), Some(visual_ns)) = (&self.visual, self.visual_ns) {
                if (source_ns as i128 - visual_ns as i128) <= hold_ns {
                    let mut held = visual.clone();
                    held.state = "uncertain".to_string();
                    held.held = Some(true);
                    return held;
                }
            }
            return Self::invalid("uncertain");
        }

        let mut left = raw.left;
        let mut right = raw.right;
        let out = raw.out;
        let mut up = if raw.extended {
            Some(raw.up.unwrap_or(0.0))
        } else {
            None
        };
        let mut down = if raw.extended {
            Some(raw.down.unwrap_or(0.0))
        } else {
            None
        };
        let mut roll = if raw.extended {
            Some(raw.roll.unwrap_or(0.0))
        } else {
            None
        };

        let smoothing = config.smoothing_ms;
        if let (Some(prev), Some(prev_ns)) = (&self.visual, old_ns) {
            if source_ns > prev_ns && smoothing > 0.0 {
                let alpha = 1.0 - (-((source_ns - prev_ns) as f64) / (smoothing * 1e6)).exp();
                left = prev.left + alpha * (left - prev.left);
                right = prev.right + alpha * (right - prev.right);
                if raw.extended {
                    if let (Some(cur_up), Some(prev_up)) = (up, prev.up) {
                        up = Some(prev_up + alpha * (cur_up - prev_up));
                    }
                    if let (Some(cur_down), Some(prev_down)) = (down, prev.down) {
                        down = Some(prev_down + alpha * (cur_down - prev_down));
                    }
                    if let (Some(cur_roll), Some(prev_roll)) = (roll, prev.roll) {
                        roll = Some(prev_roll + alpha * (cur_roll - prev_roll));
                    }
                }
            }
        }

        let current = TonguePrediction {
            state: "present".to_string(),
            fresh: true,
            present: true,
            left,
            right,
            out: Some(out),
            override_active: true,
            up,
            down,
            roll,
            held: None,
        };
        self.visual = Some(current.clone());
        self.visual_ns = Some(source_ns);
        current
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_header(seq: u64, session_id: u64) -> FrameHeader {
        let ns = seq * 11_111_111;
        FrameHeader {
            payload_bytes: 100,
            flags: 1,
            session_id,
            source_seq: seq,
            sensor_ns: ns,
            vendor_ns: ns,
            copy_mono_ns: ns,
            encode_begin_ns: ns,
            encode_end_ns: ns,
            emit_mono_ns: ns,
            eye0_sensor_ns: ns,
            eye1_sensor_ns: ns,
            mouth_sensor_ns: ns,
            raw_identity: seq,
            width: 400,
            height: 400,
            raw_crc32: 0,
            quality: 95,
        }
    }

    #[test]
    fn out_hysteresis_and_explicit_negatives_gate_all_directions() {
        let mut state = TongueState::new();
        let raw = RawTongueInput {
            state: None,
            present: true,
            left: 0.7,
            right: 0.0,
            out: 0.4,
            extended: true,
            up: Some(0.8),
            down: Some(0.0),
            roll: Some(0.6),
            out_hysteresis: Some([0.5, 0.3]),
        };
        let config = TongueStateConfig {
            smoothing_ms: 0.0,
            uncertain_hold_ms: 100.0,
        };

        assert_eq!(state.update(&raw, &make_header(10, 1), &config).state, "absent");

        let mut high = raw.clone();
        high.out = 0.6;
        let first = state.update(&high, &make_header(11, 1), &config);
        assert_eq!(first.state, "present");
        assert_eq!(first.up, Some(0.8));
        assert_eq!(first.roll, Some(0.6));

        assert_eq!(state.update(&raw, &make_header(12, 1), &config).state, "present");

        let mut low = raw.clone();
        low.out = 0.2;
        let negative = state.update(&low, &make_header(13, 1), &config);
        assert_eq!(negative.state, "absent");
        assert_eq!(negative.out, Some(0.0));
        assert_eq!(negative.left, 0.0);
        assert_eq!(negative.right, 0.0);
        assert_eq!(negative.up, Some(0.0));
        assert_eq!(negative.down, Some(0.0));
        assert_eq!(negative.roll, Some(0.0));

        assert_eq!(state.update(&raw, &make_header(14, 1), &config).state, "absent");

        let mut very_high = raw.clone();
        very_high.out = 0.8;
        state.update(&very_high, &make_header(15, 1), &config);
        assert_eq!(state.update(&raw, &make_header(1, 2), &config).state, "absent");
    }

    #[test]
    fn source_time_smoothing_applies_to_vertical_and_roll_and_uncertain_holds() {
        let mut state = TongueState::new();
        let config = TongueStateConfig::default();
        let raw0 = RawTongueInput {
            state: None,
            present: true,
            left: 0.0,
            right: 0.0,
            out: 1.0,
            extended: true,
            up: Some(0.0),
            down: Some(0.0),
            roll: Some(0.0),
            out_hysteresis: None,
        };
        state.update(&raw0, &make_header(10, 1), &config);

        let mut raw1 = raw0.clone();
        raw1.up = Some(1.0);
        raw1.roll = Some(1.0);
        let result = state.update(&raw1, &make_header(11, 1), &config);
        assert!(result.up.unwrap() > 0.0 && result.up.unwrap() < 1.0);
        assert_eq!(result.up, result.roll);

        let mut unc = raw0.clone();
        unc.state = Some("uncertain".to_string());
        let uncertain = state.update(&unc, &make_header(12, 1), &config);
        assert_eq!(uncertain.state, "uncertain");
        assert_eq!(uncertain.held, Some(true));
        assert_eq!(uncertain.up, result.up);

        // Beyond uncertain_hold_ms (100ms) -> invalid('uncertain')
        let expired = state.update(&unc, &make_header(30, 1), &config);
        assert_eq!(expired.state, "uncertain");
        assert!(!expired.fresh);
        assert!(!expired.override_active);
        assert_eq!(expired.out, None);
    }

    #[test]
    fn invalid_and_stale_clear_visual_state_and_serialize_override_key() {
        let mut state = TongueState::new();
        let config = TongueStateConfig::default();
        let raw = RawTongueInput {
            present: true,
            left: 0.5,
            right: 0.0,
            out: 1.0,
            ..Default::default()
        };
        state.update(&raw, &make_header(1, 1), &config);
        assert!(state.is_visual_active());

        let stale = RawTongueInput {
            state: Some("stale".to_string()),
            ..Default::default()
        };
        let out = state.update(&stale, &make_header(2, 1), &config);
        assert!(!state.is_visual_active());
        assert_eq!(out.state, "stale");
        let val = serde_json::to_value(&out).unwrap();
        assert_eq!(val["override"], Value::Bool(false));
        assert!(val["out"].is_null());
    }
}
