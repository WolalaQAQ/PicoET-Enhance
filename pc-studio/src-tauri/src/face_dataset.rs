//! Local mouth50 dataset contract shared by capture and offline consumers.

use crate::config::write_text_atomic;
use crate::face_schema::{CHANNELS, SCHEMA};
use serde_json::{Map, Number, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Component, Path};

/// Encode an `f64` losslessly: finite numbers become JSON numbers, while
/// non-finite values become `"NaN"`, `"Infinity"`, or `"-Infinity"`.
pub fn f64_to_json(value: f64) -> Value {
    if value.is_nan() {
        Value::String("NaN".to_string())
    } else if value.is_infinite() {
        Value::String(if value.is_sign_positive() {
            "Infinity".to_string()
        } else {
            "-Infinity".to_string()
        })
    } else {
        Number::from_f64(value)
            .map(Value::Number)
            .unwrap_or_else(|| Value::String("NaN".to_string()))
    }
}

pub fn f32_to_json(value: f32) -> Value {
    f64_to_json(value as f64)
}

pub fn f64_slice_to_json(values: &[f64]) -> Value {
    Value::Array(values.iter().map(|&v| f64_to_json(v)).collect())
}

pub fn f32_slice_to_json(values: &[f32]) -> Value {
    Value::Array(values.iter().map(|&v| f32_to_json(v)).collect())
}

/// Normalize a JSON value recursively. Since `serde_json::Value` cannot carry raw
/// non-finite floats, this ensures any nested containers remain valid standard JSON.
pub fn json_value(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = Map::new();
            for (k, v) in map {
                out.insert(k.clone(), json_value(v));
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(items.iter().map(json_value).collect()),
        other => other.clone(),
    }
}

pub fn append_json(path: &Path, row: &Value) -> Result<(), String> {
    let encoded = serde_json::to_string(&json_value(row))
        .map_err(|e| format!("Failed to serialize JSON row: {e}"))?;
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|e| format!("Failed to open {} for append: {e}", path.display()))?;
    file.write_all(encoded.as_bytes())
        .and_then(|()| file.write_all(b"\n"))
        .map_err(|e| format!("Failed to append to {}: {e}", path.display()))
}

pub fn write_json(path: &Path, value: &Value) -> Result<(), String> {
    let encoded = serde_json::to_string_pretty(&json_value(value))
        .map_err(|e| format!("Failed to serialize JSON: {e}"))?;
    write_text_atomic(path, &encoded)
        .map_err(|e| format!("Failed to write {}: {e}", path.display()))
}

pub fn write_samples(path: &Path, samples: &[Value]) -> Result<(), String> {
    let mut buffer = String::new();
    for row in samples {
        validate_sample(row)?;
        let line = serde_json::to_string(&json_value(row))
            .map_err(|e| format!("Failed to serialize sample row: {e}"))?;
        buffer.push_str(&line);
        buffer.push('\n');
    }
    write_text_atomic(path, &buffer)
        .map_err(|e| format!("Failed to write {}: {e}", path.display()))
}

fn is_json_integer(v: &Value) -> bool {
    match v {
        Value::Number(n) => n.is_i64() || n.is_u64(),
        _ => false,
    }
}

fn finite_json_f64(v: &Value) -> Option<f64> {
    let Value::Number(n) = v else {
        return None;
    };
    let f = n.as_f64()?;
    if f.is_finite() {
        Some(f)
    } else {
        None
    }
}

fn is_safe_relative_image_path(image: &str) -> bool {
    if image.is_empty() || image.starts_with('/') || image.starts_with('\\') {
        return false;
    }
    let path = Path::new(image);
    if path.is_absolute() {
        return false;
    }
    for comp in path.components() {
        match comp {
            Component::Prefix(_) | Component::RootDir | Component::ParentDir => return false,
            Component::CurDir | Component::Normal(_) => {}
        }
    }
    true
}

pub fn validate_sample(row: &Value) -> Result<(), String> {
    let Some(obj) = row.as_object() else {
        return Err("Sample row must be a JSON object".to_string());
    };
    for name in ["person_id", "session_id", "fit_id", "source_group_id"] {
        match obj.get(name).and_then(|v| v.as_str()) {
            Some(s) if !s.is_empty() => {}
            _ => return Err(format!("{name} must be a nonempty string")),
        }
    }
    for name in ["capture_session_id", "source_seq", "mouth_sensor_ns"] {
        match obj.get(name) {
            Some(v) if is_json_integer(v) => {}
            _ => return Err(format!("{name} must be an integer")),
        }
    }
    let image = obj
        .get("image")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "image must be relative to the dataset directory".to_string())?;
    if !is_safe_relative_image_path(image) {
        return Err("image must be relative to the dataset directory".to_string());
    }

    let targets = obj
        .get("targets")
        .and_then(|v| v.as_array())
        .ok_or_else(|| "Expected 50 targets and 50 target_sources".to_string())?;
    let sources = obj
        .get("target_sources")
        .and_then(|v| v.as_array())
        .ok_or_else(|| "Expected 50 targets and 50 target_sources".to_string())?;
    if targets.len() != CHANNELS.len() || sources.len() != CHANNELS.len() {
        return Err("Expected 50 targets and 50 target_sources".to_string());
    }

    for (value, source) in targets.iter().zip(sources.iter()) {
        if value.is_null() {
            if !source.is_null() {
                return Err("Unknown target must have null source".to_string());
            }
        } else {
            let Some(num) = finite_json_f64(value) else {
                return Err("Known targets must be finite [0,1] values with a source".to_string());
            };
            let valid_source = source.as_str().is_some_and(|s| !s.is_empty());
            if !(0.0..=1.0).contains(&num) || !valid_source {
                return Err("Known targets must be finite [0,1] values with a source".to_string());
            }
        }
    }

    if let Some(weights_val) = obj.get("target_weights") {
        let Some(weights) = weights_val.as_array() else {
            return Err("target_weights must have 50 finite nonnegative values".to_string());
        };
        if weights.len() != CHANNELS.len()
            || weights
                .iter()
                .any(|w| finite_json_f64(w).is_none_or(|f| f < 0.0))
        {
            return Err("target_weights must have 50 finite nonnegative values".to_string());
        }
    }

    Ok(())
}

pub fn load_dataset(root: &Path) -> Result<(Value, Vec<Value>), String> {
    let manifest_text = fs::read_to_string(root.join("manifest.json"))
        .map_err(|e| format!("Failed to read manifest.json: {e}"))?;
    let manifest: Value = serde_json::from_str(&manifest_text)
        .map_err(|e| format!("Invalid manifest.json: {e}"))?;

    let expected_channels: Vec<Value> = CHANNELS
        .iter()
        .map(|s| Value::String((*s).to_string()))
        .collect();
    if manifest.get("schema").and_then(|v| v.as_str()) != Some(SCHEMA)
        || manifest.get("channels").and_then(|v| v.as_array()) != Some(&expected_channels)
    {
        return Err("Dataset schema/channel order mismatch".to_string());
    }
    if manifest.get("samples_complete") != Some(&Value::Bool(true)) {
        return Err("Stop recording before loading its training manifest".to_string());
    }

    let samples_text = fs::read_to_string(root.join("samples.jsonl"))
        .map_err(|e| format!("Failed to read samples.jsonl: {e}"))?;
    let mut samples = Vec::new();
    for line in samples_text.lines() {
        if line.is_empty() {
            continue;
        }
        let row: Value =
            serde_json::from_str(line).map_err(|e| format!("Invalid samples.jsonl row: {e}"))?;
        validate_sample(&row)?;
        samples.push(row);
    }
    Ok((manifest, samples))
}

/// Return indivisible groups, not a random frame split or automatic allocation.
pub fn split_groups(samples: &[Value], by: &str) -> Result<BTreeMap<String, Vec<Value>>, String> {
    if !matches!(by, "person_id" | "session_id" | "fit_id" | "source_group_id") {
        return Err(
            "Split by person, recording session, fit or original source group".to_string(),
        );
    }
    let mut result: BTreeMap<String, Vec<Value>> = BTreeMap::new();
    for row in samples {
        let key = row
            .get(by)
            .and_then(|v| v.as_str())
            .ok_or_else(|| format!("Sample missing string group key {by}"))?;
        result.entry(key.to_string()).or_default().push(row.clone());
    }
    Ok(result)
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum KeyAtom {
    Null,
    Bool(bool),
    Int(i128),
    Str(String),
    Other(String),
}

impl KeyAtom {
    pub fn from_value(value: &Value) -> Self {
        match value {
            Value::Null => KeyAtom::Null,
            Value::Bool(b) => KeyAtom::Bool(*b),
            Value::Number(n) => {
                if let Some(i) = n.as_i64() {
                    KeyAtom::Int(i as i128)
                } else if let Some(u) = n.as_u64() {
                    KeyAtom::Int(u as i128)
                } else {
                    KeyAtom::Other(n.to_string())
                }
            }
            Value::String(s) => KeyAtom::Str(s.clone()),
            other => KeyAtom::Other(serde_json::to_string(other).unwrap_or_default()),
        }
    }
}

impl std::fmt::Display for KeyAtom {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            KeyAtom::Null => write!(f, "None"),
            KeyAtom::Bool(b) => write!(f, "{b}"),
            KeyAtom::Int(i) => write!(f, "{i}"),
            KeyAtom::Str(s) => write!(f, "{s}"),
            KeyAtom::Other(s) => write!(f, "{s}"),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct OriginalFrameIdentity {
    pub capture_session_id: KeyAtom,
    pub service_generation: KeyAtom,
    pub source_seq: KeyAtom,
    pub mouth_sensor_ns: KeyAtom,
    pub source_identity: KeyAtom,
}

impl OriginalFrameIdentity {
    pub fn source_key(&self) -> (KeyAtom, KeyAtom, KeyAtom, KeyAtom) {
        (
            self.capture_session_id.clone(),
            self.service_generation.clone(),
            self.source_seq.clone(),
            self.mouth_sensor_ns.clone(),
        )
    }
}

fn get_with_header_fallback<'a>(
    row: &'a Map<String, Value>,
    header: Option<&'a Map<String, Value>>,
    row_key: &str,
    header_key: &str,
) -> Option<&'a Value> {
    if let Some(v) = row.get(row_key) {
        Some(v)
    } else {
        header.and_then(|h| h.get(header_key))
    }
}

/// Physical source identity, independent of recording IDs or derived filenames.
pub fn original_frame_identity(row: &Value) -> Option<OriginalFrameIdentity> {
    let obj = row.as_object()?;
    let header = obj.get("header").and_then(|v| v.as_object());

    let capture = get_with_header_fallback(obj, header, "capture_session_id", "session_id");
    let seq = get_with_header_fallback(obj, header, "source_seq", "source_seq");
    let sensor = get_with_header_fallback(obj, header, "mouth_sensor_ns", "mouth_sensor_ns");

    let (capture, seq, sensor) = match (capture, seq, sensor) {
        (Some(c), Some(s), Some(t)) if !c.is_null() && !s.is_null() && !t.is_null() => (c, s, t),
        _ => return None,
    };

    let service_generation = obj
        .get("service_generation")
        .map(KeyAtom::from_value)
        .unwrap_or(KeyAtom::Null);

    let identity = if let Some(v) = obj.get("source_identity") {
        KeyAtom::from_value(v)
    } else if let Some(v) = obj.get("raw_identity") {
        KeyAtom::from_value(v)
    } else if let Some(v) = header.and_then(|h| h.get("raw_identity")) {
        KeyAtom::from_value(v)
    } else {
        KeyAtom::Null
    };

    Some(OriginalFrameIdentity {
        capture_session_id: KeyAtom::from_value(capture),
        service_generation,
        source_seq: KeyAtom::from_value(seq),
        mouth_sensor_ns: KeyAtom::from_value(sensor),
        source_identity: identity,
    })
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
enum LeakageKey {
    Field(String, KeyAtom),
    OriginalFrame(OriginalFrameIdentity),
    OriginalFrameSource(KeyAtom, KeyAtom, KeyAtom, KeyAtom),
}

impl LeakageKey {
    fn describe(&self) -> (&str, String) {
        match self {
            LeakageKey::Field(field, atom) => (field.as_str(), atom.to_string()),
            LeakageKey::OriginalFrame(f) => (
                "original_frame",
                format!(
                    "({}, {}, {}, {}, {})",
                    f.capture_session_id,
                    f.service_generation,
                    f.source_seq,
                    f.mouth_sensor_ns,
                    f.source_identity
                ),
            ),
            LeakageKey::OriginalFrameSource(c, g, s, t) => (
                "original_frame_source",
                format!("({c}, {g}, {s}, {t})"),
            ),
        }
    }
}

/// Keep recordings/fits/derivatives whole, and reject repeated physical frames.
pub fn assert_no_split_leakage(
    splits: &BTreeMap<String, Vec<Value>>,
    by: &str,
) -> Result<(), String> {
    let mut owners: HashMap<LeakageKey, String> = HashMap::new();
    let fields: BTreeSet<&str> = [by, "session_id", "fit_id", "source_group_id"]
        .into_iter()
        .collect();

    for (name, rows) in splits {
        for row in rows {
            let mut origins: Vec<&Value> = Vec::new();
            origins.push(row);
            if let Some(arr) = row.get("lineage").and_then(|v| v.as_array()) {
                origins.extend(arr.iter());
            }
            if let Some(arr) = row.get("derived_from").and_then(|v| v.as_array()) {
                origins.extend(arr.iter());
            }
            for origin in origins {
                let mut keys: Vec<LeakageKey> = Vec::new();
                if let Some(obj) = origin.as_object() {
                    for &field in &fields {
                        if let Some(val) = obj.get(field) {
                            keys.push(LeakageKey::Field(
                                field.to_string(),
                                KeyAtom::from_value(val),
                            ));
                        }
                    }
                }
                if let Some(frame) = original_frame_identity(origin) {
                    let src = frame.source_key();
                    keys.push(LeakageKey::OriginalFrame(frame));
                    keys.push(LeakageKey::OriginalFrameSource(src.0, src.1, src.2, src.3));
                }
                for key in keys {
                    if let Some(owner) = owners.get(&key) {
                        if owner != name {
                            let (kind, detail) = key.describe();
                            return Err(format!("{kind} crosses splits: {detail}"));
                        }
                    } else {
                        owners.insert(key, name.clone());
                    }
                }
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn sample(session: &str, person: &str, source: Option<&str>) -> Value {
        let seq: i64 = match session {
            "s1" => 3,
            "s2" => 13,
            "s3" => 23,
            "renamed" => 33,
            _ => 43,
        };
        let src = source
            .map(|s| s.to_string())
            .unwrap_or_else(|| format!("recording:{session}:fit:{session}"));
        json!({
            "image": "images/frame.jpg",
            "person_id": person,
            "session_id": session,
            "fit_id": session,
            "source_group_id": src,
            "capture_session_id": 2,
            "service_generation": 8,
            "source_seq": seq,
            "mouth_sensor_ns": seq * 11_111_111,
            "source_identity": seq + 100,
            "targets": vec![Value::Null; 50],
            "target_sources": vec![Value::Null; 50],
        })
    }

    #[test]
    fn unknown_valid_explicit_zero_valid() {
        let mut row = sample("s1", "p1", None);
        validate_sample(&row).unwrap();
        row["targets"][3] = json!(0.0);
        row["target_sources"][3] = json!("human_review:id");
        validate_sample(&row).unwrap();
    }

    #[test]
    fn optional_source_weights_validation() {
        let mut row = sample("s1", "p1", None);
        let mut weights: Vec<Value> = vec![json!(0.5); 33];
        weights.extend(vec![json!(0.0); 17]);
        row["target_weights"] = Value::Array(weights);
        validate_sample(&row).unwrap();

        let bad_cases = vec![
            vec![json!(0.5); 49],
            std::iter::once(json!(-0.1))
                .chain(vec![json!(1.0); 49])
                .collect(),
            std::iter::once(f64_to_json(f64::NAN))
                .chain(vec![json!(1.0); 49])
                .collect(),
        ];
        for bad in bad_cases {
            let mut r = row.clone();
            r["target_weights"] = Value::Array(bad);
            let err = validate_sample(&r).unwrap_err();
            assert!(err.contains("target_weights"));
        }
    }

    #[test]
    fn nonfinite_or_unattributed_target_rejected() {
        for value in [
            f64_to_json(f64::NAN),
            f64_to_json(f64::INFINITY),
            json!(-0.1),
            json!(1.1),
            json!(0.5),
        ] {
            let mut row = sample("s1", "p1", None);
            row["targets"][3] = value;
            assert!(validate_sample(&row).is_err());
        }
    }

    #[test]
    fn path_escape_and_wrong_order_length_rejected() {
        let mut row = sample("s1", "p1", None);
        row["image"] = json!("../outside.jpg");
        assert!(validate_sample(&row).is_err());

        let mut row = sample("s1", "p1", None);
        row["targets"] = Value::Array(vec![Value::Null; 44]);
        assert!(validate_sample(&row).is_err());
    }

    #[test]
    fn person_session_fit_and_original_source_groups() {
        let rows = vec![
            sample("s1", "p1", None),
            sample("s1", "p1", None),
            sample("s2", "p1", Some("source2")),
            sample("s3", "p2", Some("source3")),
        ];
        let by_session = split_groups(&rows, "session_id").unwrap();
        let mut counts: Vec<usize> = by_session.values().map(|v| v.len()).collect();
        counts.sort();
        assert_eq!(counts, vec![1, 1, 2]);

        let by_person = split_groups(&rows, "person_id").unwrap();
        assert_eq!(by_person["p1"].len(), 3);
        assert_eq!(by_person["p2"].len(), 1);

        let by_fit = split_groups(&rows, "fit_id").unwrap();
        let fit_keys: BTreeSet<&str> = by_fit.keys().map(|s| s.as_str()).collect();
        assert_eq!(fit_keys, ["s1", "s2", "s3"].into_iter().collect());

        assert!(split_groups(&rows, "source_seq").is_err());
    }

    #[test]
    fn neighbor_frames_cannot_cross_session_split() {
        let one = sample("s1", "p1", None);
        let mut two = one.clone();
        two["source_seq"] = json!(two["source_seq"].as_i64().unwrap() + 1);
        let splits = BTreeMap::from([("train".into(), vec![one]), ("test".into(), vec![two])]);
        assert!(assert_no_split_leakage(&splits, "session_id").is_err());
    }

    #[test]
    fn derived_recording_retains_source_group() {
        let one = sample("s1", "p1", None);
        let src = one["source_group_id"].as_str().unwrap().to_string();
        let two = sample("renamed", "p1", Some(&src));
        let splits = BTreeMap::from([("train".into(), vec![one]), ("test".into(), vec![two])]);
        assert!(assert_no_split_leakage(&splits, "session_id").is_err());
    }

    #[test]
    fn same_capture_owner_different_recording_fit_can_be_held_out() {
        let first = sample("s1", "p1", None);
        let refitted = sample("s2", "p1", None);
        assert_eq!(first["capture_session_id"], refitted["capture_session_id"]);
        assert_eq!(first["service_generation"], refitted["service_generation"]);
        let splits =
            BTreeMap::from([("train".into(), vec![first]), ("test".into(), vec![refitted])]);
        assert_no_split_leakage(&splits, "session_id").unwrap();
        assert_no_split_leakage(&splits, "fit_id").unwrap();
    }

    #[test]
    fn same_frame_cannot_be_unlocked_by_renaming_recording_and_fit() {
        let original = sample("s1", "p1", None);
        let mut renamed = original.clone();
        renamed["session_id"] = json!("new-session");
        renamed["fit_id"] = json!("new-fit");
        renamed["source_group_id"] = json!("recording:new-session:fit:new-fit");
        renamed["image"] = json!("copied.jpg");
        assert_eq!(
            original_frame_identity(&original),
            original_frame_identity(&renamed)
        );
        let splits =
            BTreeMap::from([("train".into(), vec![original]), ("test".into(), vec![renamed])]);
        assert!(assert_no_split_leakage(&splits, "session_id")
            .unwrap_err()
            .contains("original_frame"));
    }

    #[test]
    fn derived_lineage_checks_original_frame_even_if_output_ids_change() {
        let original = sample("s1", "p1", None);
        let mut derived = sample("s2", "p1", None);
        derived["derived_from"] = Value::Array(vec![original.clone()]);
        let splits = BTreeMap::from([
            ("train".into(), vec![original.clone()]),
            ("test".into(), vec![derived.clone()]),
        ]);
        assert!(assert_no_split_leakage(&splits, "session_id").is_err());

        derived["derived_from"] = json!([{
            "capture_session_id": original["capture_session_id"],
            "service_generation": original["service_generation"],
            "source_seq": original["source_seq"],
            "mouth_sensor_ns": original["mouth_sensor_ns"],
            "source_identity": original["source_identity"],
        }]);
        let splits =
            BTreeMap::from([("train".into(), vec![original]), ("test".into(), vec![derived])]);
        assert!(assert_no_split_leakage(&splits, "session_id")
            .unwrap_err()
            .contains("original_frame"));
    }

    #[test]
    fn header_raw_identity_is_same_as_top_level_source_identity() {
        let original = sample("s1", "p1", None);
        let mut derived = original.clone();
        derived["session_id"] = json!("renamed");
        derived["fit_id"] = json!("renamed");
        derived["source_group_id"] = json!("renamed");
        let raw_id = derived.as_object_mut().unwrap().remove("source_identity").unwrap();
        derived["header"] = json!({ "raw_identity": raw_id });
        assert_eq!(
            original_frame_identity(&original),
            original_frame_identity(&derived)
        );
        let splits =
            BTreeMap::from([("train".into(), vec![original]), ("test".into(), vec![derived])]);
        assert!(assert_no_split_leakage(&splits, "session_id")
            .unwrap_err()
            .contains("original_frame"));
    }

    #[test]
    fn dropping_raw_identity_does_not_unlock_the_same_source_frame() {
        let original = sample("s1", "p1", None);
        let mut derived = original.clone();
        derived["session_id"] = json!("renamed");
        derived["fit_id"] = json!("renamed");
        derived["source_group_id"] = json!("renamed");
        derived.as_object_mut().unwrap().remove("source_identity");
        let splits =
            BTreeMap::from([("train".into(), vec![original]), ("test".into(), vec![derived])]);
        assert!(assert_no_split_leakage(&splits, "session_id")
            .unwrap_err()
            .contains("original_frame_source"));
    }

    #[test]
    fn continuous_recording_and_same_fit_and_person_holdout() {
        let first = sample("s1", "p1", None);
        let mut next_frame = sample("s2", "p1", None);
        next_frame["session_id"] = first["session_id"].clone();
        let splits = BTreeMap::from([
            ("train".into(), vec![first.clone()]),
            ("test".into(), vec![next_frame]),
        ]);
        assert!(assert_no_split_leakage(&splits, "source_group_id")
            .unwrap_err()
            .contains("session_id"));

        let mut second_take = sample("s2", "p1", None);
        second_take["fit_id"] = first["fit_id"].clone();
        let splits = BTreeMap::from([
            ("train".into(), vec![first.clone()]),
            ("test".into(), vec![second_take]),
        ]);
        assert!(assert_no_split_leakage(&splits, "session_id")
            .unwrap_err()
            .contains("fit_id"));

        let splits = BTreeMap::from([
            ("train".into(), vec![sample("s1", "p1", None)]),
            ("test".into(), vec![sample("s2", "p1", Some("source2"))]),
        ]);
        assert_no_split_leakage(&splits, "session_id").unwrap();
        assert!(assert_no_split_leakage(&splits, "person_id").is_err());
    }

    #[test]
    fn lossless_nonfinite_encoding_and_atomic_write() {
        assert_eq!(f64_to_json(f64::NAN), json!("NaN"));
        assert_eq!(f64_to_json(f64::INFINITY), json!("Infinity"));
        assert_eq!(f64_to_json(f64::NEG_INFINITY), json!("-Infinity"));
        assert_eq!(f64_to_json(0.25), json!(0.25));

        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("val.json");
        write_json(&p, &json!({ "x": f64_to_json(f64::NEG_INFINITY) })).unwrap();
        let text = fs::read_to_string(&p).unwrap();
        let parsed: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(parsed["x"], json!("-Infinity"));
    }
}
