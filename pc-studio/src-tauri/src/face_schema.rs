use serde_json::{Map, Value};
use std::collections::{BTreeMap, HashSet};

pub const SCHEMA: &str = "pico-mouth50-v1";

pub const STOCK_CHANNELS: [&str; 33] = [
    "CheekPuff",
    "CheekSquintL",
    "CheekSquintR",
    "JawForward",
    "JawLeft",
    "JawOpen",
    "JawRight",
    "MouthClose",
    "MouthDimpleL",
    "MouthDimpleR",
    "MouthFrownL",
    "MouthFrownR",
    "MouthFunnel",
    "MouthLeft",
    "MouthLowerDownL",
    "MouthLowerDownR",
    "MouthPressL",
    "MouthPressR",
    "MouthPucker",
    "MouthRight",
    "MouthRollLower",
    "MouthRollUpper",
    "MouthShrugLower",
    "MouthShrugUpper",
    "MouthSmileL",
    "MouthSmileR",
    "MouthStretchL",
    "MouthStretchR",
    "MouthUpperUpL",
    "MouthUpperUpR",
    "NoseSneerL",
    "NoseSneerR",
    "TongueOut",
];

pub const EXTENDED_CHANNELS: [&str; 17] = [
    "TongueLeft",
    "TongueRight",
    "TongueUp",
    "TongueDown",
    "CheekPuffLeft",
    "CheekPuffRight",
    "CheekSuckLeft",
    "CheekSuckRight",
    "MouthTightenerLeft",
    "MouthTightenerRight",
    "TongueRoll",
    "MouthUpperLeft",
    "MouthUpperRight",
    "MouthLowerLeft",
    "MouthLowerRight",
    "LipFunnelUpper",
    "LipFunnelLower",
];

pub const CHANNELS: [&str; 50] = [
    "CheekPuff",
    "CheekSquintL",
    "CheekSquintR",
    "JawForward",
    "JawLeft",
    "JawOpen",
    "JawRight",
    "MouthClose",
    "MouthDimpleL",
    "MouthDimpleR",
    "MouthFrownL",
    "MouthFrownR",
    "MouthFunnel",
    "MouthLeft",
    "MouthLowerDownL",
    "MouthLowerDownR",
    "MouthPressL",
    "MouthPressR",
    "MouthPucker",
    "MouthRight",
    "MouthRollLower",
    "MouthRollUpper",
    "MouthShrugLower",
    "MouthShrugUpper",
    "MouthSmileL",
    "MouthSmileR",
    "MouthStretchL",
    "MouthStretchR",
    "MouthUpperUpL",
    "MouthUpperUpR",
    "NoseSneerL",
    "NoseSneerR",
    "TongueOut",
    "TongueLeft",
    "TongueRight",
    "TongueUp",
    "TongueDown",
    "CheekPuffLeft",
    "CheekPuffRight",
    "CheekSuckLeft",
    "CheekSuckRight",
    "MouthTightenerLeft",
    "MouthTightenerRight",
    "TongueRoll",
    "MouthUpperLeft",
    "MouthUpperRight",
    "MouthLowerLeft",
    "MouthLowerRight",
    "LipFunnelUpper",
    "LipFunnelLower",
];

pub const STOCK_SLOTS: [usize; 33] = [
    43, 29, 37, 25, 32, 7, 15, 5, 49, 23, 27, 40, 10, 24, 50, 6, 20, 22, 34, 48, 18, 14, 17, 9,
    19, 21, 33, 42, 39, 8, 1, 13, 51,
];

pub const ALL_VALID: u64 = (1u64 << 50) - 1;

pub const AGGREGATE_STOCK_CHANNELS: [&str; 4] = [
    "CheekPuff",
    "MouthFunnel",
    "MouthLeft",
    "MouthRight",
];

pub const AGGREGATE_CHILDREN: [(&str, [&str; 2]); 4] = [
    ("CheekPuff", ["CheekPuffLeft", "CheekPuffRight"]),
    ("MouthFunnel", ["LipFunnelUpper", "LipFunnelLower"]),
    ("MouthLeft", ["MouthUpperLeft", "MouthLowerLeft"]),
    ("MouthRight", ["MouthUpperRight", "MouthLowerRight"]),
];

pub const STOCK_AGGREGATE_PARENTS: [(&str, &str); 8] = [
    ("CheekPuffLeft", "CheekPuff"),
    ("CheekPuffRight", "CheekPuff"),
    ("LipFunnelUpper", "MouthFunnel"),
    ("LipFunnelLower", "MouthFunnel"),
    ("MouthUpperLeft", "MouthLeft"),
    ("MouthLowerLeft", "MouthLeft"),
    ("MouthUpperRight", "MouthRight"),
    ("MouthLowerRight", "MouthRight"),
];

pub const ROUTABLE_CHANNELS: [&str; 46] = [
    "CheekSquintL",
    "CheekSquintR",
    "JawForward",
    "JawLeft",
    "JawOpen",
    "JawRight",
    "MouthClose",
    "MouthDimpleL",
    "MouthDimpleR",
    "MouthFrownL",
    "MouthFrownR",
    "MouthLowerDownL",
    "MouthLowerDownR",
    "MouthPressL",
    "MouthPressR",
    "MouthPucker",
    "MouthRollLower",
    "MouthRollUpper",
    "MouthShrugLower",
    "MouthShrugUpper",
    "MouthSmileL",
    "MouthSmileR",
    "MouthStretchL",
    "MouthStretchR",
    "MouthUpperUpL",
    "MouthUpperUpR",
    "NoseSneerL",
    "NoseSneerR",
    "TongueOut",
    "TongueLeft",
    "TongueRight",
    "TongueUp",
    "TongueDown",
    "CheekPuffLeft",
    "CheekPuffRight",
    "CheekSuckLeft",
    "CheekSuckRight",
    "MouthTightenerLeft",
    "MouthTightenerRight",
    "TongueRoll",
    "MouthUpperLeft",
    "MouthUpperRight",
    "MouthLowerLeft",
    "MouthLowerRight",
    "LipFunnelUpper",
    "LipFunnelLower",
];

pub const PURE_EXTENDED_CHANNELS: [&str; 9] = [
    "TongueLeft",
    "TongueRight",
    "TongueUp",
    "TongueDown",
    "CheekSuckLeft",
    "CheekSuckRight",
    "MouthTightenerLeft",
    "MouthTightenerRight",
    "TongueRoll",
];

pub const STOCK_CAPABLE_CHANNELS: [&str; 37] = [
    "CheekSquintL",
    "CheekSquintR",
    "JawForward",
    "JawLeft",
    "JawOpen",
    "JawRight",
    "MouthClose",
    "MouthDimpleL",
    "MouthDimpleR",
    "MouthFrownL",
    "MouthFrownR",
    "MouthLowerDownL",
    "MouthLowerDownR",
    "MouthPressL",
    "MouthPressR",
    "MouthPucker",
    "MouthRollLower",
    "MouthRollUpper",
    "MouthShrugLower",
    "MouthShrugUpper",
    "MouthSmileL",
    "MouthSmileR",
    "MouthStretchL",
    "MouthStretchR",
    "MouthUpperUpL",
    "MouthUpperUpR",
    "NoseSneerL",
    "NoseSneerR",
    "TongueOut",
    "CheekPuffLeft",
    "CheekPuffRight",
    "MouthUpperLeft",
    "MouthUpperRight",
    "MouthLowerLeft",
    "MouthLowerRight",
    "LipFunnelUpper",
    "LipFunnelLower",
];

pub const CURATED_MODEL_TARGETS: [&str; 18] = [
    "TongueLeft",
    "TongueRight",
    "TongueUp",
    "TongueDown",
    "TongueRoll",
    "CheekSuckLeft",
    "CheekSuckRight",
    "MouthTightenerLeft",
    "MouthTightenerRight",
    "TongueOut",
    "CheekPuffLeft",
    "CheekPuffRight",
    "MouthSmileL",
    "MouthSmileR",
    "MouthFrownL",
    "MouthFrownR",
    "MouthUpperUpL",
    "MouthUpperUpR",
];

pub const MOUTH_MODES: [&str; 4] = ["curated", "hybrid", "model_only", "custom"];
pub const PRESET_MODES: [&str; 3] = ["curated", "hybrid", "model_only"];

pub fn channel_index(name: &str) -> Option<usize> {
    CHANNELS.iter().position(|&c| c == name)
}

pub fn is_aggregate_stock_channel(name: &str) -> bool {
    AGGREGATE_STOCK_CHANNELS.contains(&name)
}

pub fn is_pure_extended_channel(name: &str) -> bool {
    PURE_EXTENDED_CHANNELS.contains(&name)
}

pub fn aggregate_parent(name: &str) -> Option<&'static str> {
    STOCK_AGGREGATE_PARENTS
        .iter()
        .find_map(|&(child, parent)| if child == name { Some(parent) } else { None })
}

pub fn mouth_preset(mode: &str) -> BTreeMap<String, String> {
    let mut map = BTreeMap::new();
    for &name in &ROUTABLE_CHANNELS {
        let src = match mode {
            "hybrid" => {
                if is_pure_extended_channel(name) {
                    "model"
                } else {
                    "stock"
                }
            }
            "model_only" => "model",
            _ => {
                if CURATED_MODEL_TARGETS.contains(&name) {
                    "model"
                } else {
                    "stock"
                }
            }
        };
        map.insert(name.to_string(), src.to_string());
    }
    map
}

pub fn mouth_presets_json() -> Value {
    let mut root = Map::new();
    for mode in PRESET_MODES {
        let mut obj = Map::new();
        for &name in &ROUTABLE_CHANNELS {
            let src = match mode {
                "hybrid" => {
                    if is_pure_extended_channel(name) {
                        "model"
                    } else {
                        "stock"
                    }
                }
                "model_only" => "model",
                _ => {
                    if CURATED_MODEL_TARGETS.contains(&name) {
                        "model"
                    } else {
                        "stock"
                    }
                }
            };
            obj.insert(name.to_string(), Value::String(src.to_string()));
        }
        root.insert(mode.to_string(), Value::Object(obj));
    }
    Value::Object(root)
}

pub fn validate_channel_sources(
    channel_sources: Option<&Value>,
) -> Result<Option<BTreeMap<String, String>>, String> {
    let Some(val) = channel_sources else {
        return Ok(None);
    };
    if val.is_null() {
        return Ok(None);
    }
    let Some(obj) = val.as_object() else {
        return Err(
            "channel_sources must be null or a mapping keyed by ROUTABLE_CHANNELS".to_string(),
        );
    };
    let routable: HashSet<&str> = ROUTABLE_CHANNELS.iter().copied().collect();
    let mut unknown: Vec<&str> = obj
        .keys()
        .map(|k| k.as_str())
        .filter(|k| !routable.contains(k))
        .collect();
    if !unknown.is_empty() {
        unknown.sort_unstable();
        return Err(format!(
            "channel_sources keys must belong to ROUTABLE_CHANNELS: {}",
            unknown.join(", ")
        ));
    }
    let mut out = BTreeMap::new();
    for &name in &ROUTABLE_CHANNELS {
        if let Some(v) = obj.get(name) {
            let src = v
                .as_str()
                .ok_or_else(|| format!("Unsupported source for {name}: {v}"))?;
            if is_pure_extended_channel(name) {
                if src != "model" && src != "off" {
                    return Err(format!(
                        "{name} is missing in PICO stock and only supports model or off"
                    ));
                }
            } else if src != "stock" && src != "model" && src != "off" {
                return Err(format!("Unsupported source for {name}: {src}"));
            }
            out.insert(name.to_string(), src.to_string());
        }
    }
    Ok(Some(out))
}

pub fn matching_preset(sources: &BTreeMap<String, String>) -> &'static str {
    for mode in PRESET_MODES {
        if *sources == mouth_preset(mode) {
            return mode;
        }
    }
    "custom"
}

pub fn resolve_channel_sources(
    mouth_mode: &str,
    hybrid_channels: Option<&[String]>,
    channel_sources: Option<&BTreeMap<String, String>>,
) -> BTreeMap<String, String> {
    let base_mode = if PRESET_MODES.contains(&mouth_mode) {
        mouth_mode
    } else {
        "curated"
    };
    let mut base = mouth_preset(base_mode);
    if channel_sources.is_none() && mouth_mode == "hybrid" {
        if let Some(hc) = hybrid_channels {
            let allowed: HashSet<&str> = hc.iter().map(|s| s.as_str()).collect();
            for &name in &STOCK_CAPABLE_CHANNELS {
                let stock_key = aggregate_parent(name).unwrap_or(name);
                base.insert(
                    name.to_string(),
                    if allowed.contains(stock_key) {
                        "stock".to_string()
                    } else {
                        "model".to_string()
                    },
                );
            }
        }
    }
    if let Some(overrides) = channel_sources {
        for (k, v) in overrides {
            if base.contains_key(k) {
                base.insert(k.clone(), v.clone());
            }
        }
    }
    base
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn schema_counts_and_invariants() {
        assert_eq!(STOCK_CHANNELS.len(), 33);
        assert_eq!(EXTENDED_CHANNELS.len(), 17);
        assert_eq!(CHANNELS.len(), 50);
        assert_eq!(ROUTABLE_CHANNELS.len(), 46);
        assert_eq!(PURE_EXTENDED_CHANNELS.len(), 9);
        assert_eq!(STOCK_CAPABLE_CHANNELS.len(), 37);
        assert_eq!(CURATED_MODEL_TARGETS.len(), 18);
        assert_eq!(ALL_VALID, 0x3ffffffffffff);
        let unique: HashSet<&str> = CHANNELS.iter().copied().collect();
        assert_eq!(unique.len(), 50);
        assert_eq!(channel_index("CheekPuff"), Some(0));
        assert_eq!(channel_index("TongueOut"), Some(32));
        assert_eq!(channel_index("LipFunnelLower"), Some(49));
    }

    #[test]
    fn presets_and_validation() {
        for mode in PRESET_MODES {
            let p = mouth_preset(mode);
            assert_eq!(p.len(), 46);
            assert_eq!(matching_preset(&p), mode);
        }
        assert!(validate_channel_sources(Some(&json!({"TongueLeft": "stock"}))).is_err());
        assert!(validate_channel_sources(Some(&json!({"TongueLeft": "off"}))).is_ok());
        assert!(validate_channel_sources(Some(&json!({"CheekPuff": "stock"}))).is_err());
        let resolved = resolve_channel_sources(
            "hybrid",
            Some(&["JawOpen".to_string(), "CheekPuff".to_string()]),
            None,
        );
        assert_eq!(resolved["JawOpen"], "stock");
        assert_eq!(resolved["CheekPuffLeft"], "stock");
        assert_eq!(resolved["CheekPuffRight"], "stock");
        assert_eq!(resolved["MouthClose"], "model");
        assert_eq!(resolved["TongueLeft"], "model");
    }
}
