//! Ordered capture plans referencing the existing prompts and demonstration assets.

use crate::face_prompts::{make_step, prompt_plan, BilingualText, PromptStep};
use crate::face_schema::SCHEMA;
use chrono::DateTime;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::Path;

pub const DEFAULT_PRESET: &str = "complete";
pub const SEALED_PRESET: &str = "sealed-c";
pub const SEALED_JAW_PRESET: &str = "sealed-c-jaw";
pub const SEALED_PROFILES: [(&str, &str); 2] = [
    ("mouth", SEALED_PRESET),
    ("jaw", SEALED_JAW_PRESET),
];

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CapturePreset {
    pub id: String,
    pub name: BilingualText,
    pub description: BilingualText,
    pub steps: Vec<PromptStep>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CaptureContinuation {
    pub preset: CapturePreset,
    pub session_id: String,
    pub person_id: String,
    pub fit_id: String,
    pub source_group_id: String,
    pub label_stage: String,
    pub role: Option<String>,
}

struct PresetDef {
    id: &'static str,
    name_zh: &'static str,
    name_en: &'static str,
    desc_zh: &'static str,
    desc_en: &'static str,
    actions: Option<&'static [&'static str]>,
}

const BASICS_ACTIONS: &[&str] = &[
    "neutral",
    "JawOpen",
    "MouthClose",
    "MouthSmileL",
    "MouthSmileR",
    "MouthPucker",
    "MouthFunnel",
    "CheekPuff",
    "JawLeft",
    "JawRight",
    "TongueOut",
    "neutral",
];

const TONGUE_ACTIONS: &[&str] = &[
    "neutral",
    "tongue_short",
    "tongue_medium",
    "tongue_long",
    "TongueLeft",
    "TongueRight",
    "TongueUp",
    "TongueDown",
    "TongueRoll",
    "negative_tip_curl_not_roll",
    "neutral",
];

const LIPS_ACTIONS: &[&str] = &[
    "neutral",
    "MouthUpperLeft",
    "MouthUpperRight",
    "MouthLowerLeft",
    "MouthLowerRight",
    "MouthRollUpper",
    "MouthRollLower",
    "LipFunnelUpper",
    "LipFunnelLower",
    "MouthPucker",
    "MouthPressL",
    "MouthPressR",
    "neutral",
];

const PRESETS: [PresetDef; 4] = [
    PresetDef {
        id: "basics",
        name_zh: "基础嘴部",
        name_en: "Mouth basics",
        desc_zh: "放松、张嘴、闭唇、左右微笑、噘嘴、漏斗口型、鼓腮、下颌左右移和伸舌。",
        desc_en: "Relax, open your jaw, close your lips, smile on each side, pucker, funnel, puff your cheeks, shift your jaw left and right, and extend your tongue.",
        actions: Some(BASICS_ACTIONS),
    },
    PresetDef {
        id: "tongue",
        name_zh: "舌头专项",
        name_en: "Tongue movements",
        desc_zh: "短、中、长三档伸舌，向左右上下伸舌，卷舌，以及只弯起舌尖的动作。",
        desc_en: "Short, medium and long extension, the four directions, tongue roll, and a tip-only curl.",
        actions: Some(TONGUE_ACTIONS),
    },
    PresetDef {
        id: "lips",
        name_zh: "独立唇形",
        name_en: "Independent lip movements",
        desc_zh: "上唇和下唇分别左右横移，分别内卷，分别做漏斗口型，再加噘嘴和左右压唇。",
        desc_en: "Move the upper and lower lips sideways separately, roll each lip inward, funnel each lip, then pucker and press each side.",
        actions: Some(LIPS_ACTIONS),
    },
    PresetDef {
        id: "complete",
        name_zh: "完整引导",
        name_en: "Complete guide",
        desc_zh: "全部命名控制、组合动作、三档伸舌，以及舌头留在口内的口型、说话和舌尖上弯。",
        desc_en: "Every named control, combined movements, three tongue lengths, and mouth shapes with the tongue inside, speaking and a tongue-tip curl.",
        actions: None,
    },
];

pub fn capture_presets() -> Vec<CapturePreset> {
    let prompts = prompt_plan();
    let by_id: HashMap<&str, &PromptStep> =
        prompts.iter().map(|step| (step.id.as_str(), step)).collect();
    PRESETS
        .iter()
        .map(|def| {
            let steps = match def.actions {
                Some(ids) => ids.iter().map(|&key| by_id[key].clone()).collect(),
                None => prompts.clone(),
            };
            CapturePreset {
                id: def.id.to_string(),
                name: BilingualText::new(def.name_zh, def.name_en),
                description: BilingualText::new(def.desc_zh, def.desc_en),
                steps,
            }
        })
        .collect()
}

pub fn capture_preset(preset_id: &str) -> Result<CapturePreset, String> {
    if preset_id == SEALED_PRESET {
        return Ok(sealed_preset());
    }
    if preset_id == SEALED_JAW_PRESET {
        return Ok(sealed_jaw_preset());
    }
    for preset in capture_presets() {
        if preset.id == preset_id {
            return Ok(preset);
        }
    }
    Err(format!("Unknown capture preset: {preset_id}"))
}

/// Fixed TongueOut/JawOpen contrasts, using the existing demonstration media.
pub fn sealed_preset() -> CapturePreset {
    let prompts: HashMap<String, PromptStep> = prompt_plan()
        .into_iter()
        .map(|s| (s.id.clone(), s))
        .collect();
    let mut steps = Vec::with_capacity(12);
    let mut first = prompts["neutral"].clone();
    first.labels.clear();
    steps.push(first);

    for &(opening, zh_open, en_open) in &[
        ("small", "小幅张嘴", "Open your jaw slightly"),
        ("large", "大幅张嘴", "Open your jaw wide"),
    ] {
        let med_demo = format!("jaw_tongue_{opening}");
        let levels = [
            (
                "none",
                "舌头留在口内",
                "keep your tongue inside your mouth",
                "negative_wide_open",
                6.0,
            ),
            (
                "weak",
                "只让舌尖刚超过唇缘",
                "extend only the tip just past your lips",
                "tongue_short",
                3.0,
            ),
            (
                "medium",
                "舌头伸到舒适长度的一半左右",
                "extend your tongue about halfway",
                med_demo.as_str(),
                3.0,
            ),
            (
                "strong",
                "舌头充分但舒适地向前伸",
                "extend your tongue fully but comfortably",
                "tongue_long",
                3.0,
            ),
        ];
        for (level, zh, en, demo, hold_s) in levels {
            steps.push(make_step(
                &format!("c_{opening}_{level}"),
                &format!("{zh_open}，{zh}"),
                &format!("{en_open} and {en}"),
                BTreeMap::new(),
                "pose",
                hold_s,
                None,
                Some(demo),
            ));
        }
    }

    for &(key, hold) in &[
        ("negative_teeth", 6.0),
        ("negative_speech", 8.0),
        ("neutral", 4.0),
    ] {
        let mut step = prompts[key].clone();
        step.labels.clear();
        step.hold_s = hold;
        steps.push(step);
    }

    CapturePreset {
        id: SEALED_PRESET.to_string(),
        name: BilingualText::new("独立评估 C", "Independent evaluation C"),
        description: BilingualText::new(
            "两档开口分别做无、弱、中、强伸舌，再做露牙微笑和自然说话。开口大小以每步文字为准。",
            "At each jaw opening, hold no, weak, medium and strong extension, then smile with teeth and speak naturally. Follow the step text for how far to open your jaw.",
        ),
        steps,
    }
}

/// Repeat small and large openings with different closed-lip contrasts.
pub fn sealed_jaw_preset() -> CapturePreset {
    let actions = [
        (
            "closed_relaxed",
            "轻轻合上双唇，嘴部放松",
            "Close your lips gently and relax your mouth",
            "MouthClose",
        ),
        (
            "open_small_1",
            "小幅张嘴，保持这个开口",
            "Open your jaw slightly and hold that opening",
            "JawOpen",
        ),
        (
            "closed_pucker",
            "双唇保持贴合，向前噘嘴",
            "Pucker forward, keeping your lips together",
            "MouthPucker",
        ),
        (
            "open_large_1",
            "舒适地大幅张嘴，保持这个开口",
            "Open your jaw comfortably wide and hold that opening",
            "JawOpen",
        ),
        (
            "closed_smile_left",
            "双唇保持贴合，只抬起左嘴角微笑",
            "Lift your left mouth corner in a smile, keeping your lips together",
            "MouthSmileL",
        ),
        (
            "open_small_2",
            "再次小幅张嘴，保持这个开口",
            "Open your jaw slightly again and hold that opening",
            "JawOpen",
        ),
        (
            "closed_smile_right",
            "双唇保持贴合，只抬起右嘴角微笑",
            "Lift your right mouth corner in a smile, keeping your lips together",
            "MouthSmileR",
        ),
        (
            "open_large_2",
            "再次舒适地大幅张嘴，保持这个开口",
            "Open your jaw comfortably wide again and hold that opening",
            "JawOpen",
        ),
        (
            "closed_relaxed_end",
            "轻轻合上双唇，嘴部放松",
            "Close your lips gently and relax your mouth",
            "MouthClose",
        ),
    ];

    let steps = actions
        .iter()
        .map(|&(key, zh, en, demo)| {
            make_step(
                &format!("c_jaw_{key}"),
                zh,
                en,
                BTreeMap::new(),
                "pose",
                3.0,
                None,
                Some(demo),
            )
        })
        .collect();

    CapturePreset {
        id: SEALED_JAW_PRESET.to_string(),
        name: BilingualText::new(
            "独立评估 C · 仅开闭口",
            "Independent evaluation C · JawOpen only",
        ),
        description: BilingualText::new(
            "按文字做两次小开口和两次大开口，全程舌头留在口内。闭唇动作中双唇始终贴合。",
            "Follow the text for two small and two large openings. Keep your tongue inside throughout, and keep your lips together during closed-lip actions.",
        ),
        steps,
    }
}

pub fn is_hex_session_id(s: &str) -> bool {
    s.len() == 32 && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

fn manifest_is_continuable(manifest: &Value, expected_session_id: Option<&str>) -> bool {
    if manifest.get("schema").and_then(|v| v.as_str()) != Some(SCHEMA) {
        return false;
    }
    if let Some(sid) = expected_session_id {
        if manifest.get("session_id").and_then(|v| v.as_str()) != Some(sid) {
            return false;
        }
    }
    if manifest.get("state").and_then(|v| v.as_str()) != Some("stopped") {
        return false;
    }
    if manifest.get("samples_complete") != Some(&Value::Bool(true)) {
        return false;
    }
    let has_plan = manifest
        .get("capture_plan")
        .and_then(|v| v.as_object())
        .is_some_and(|obj| !obj.is_empty());
    if !has_plan {
        return false;
    }
    if manifest
        .get("evaluation_only")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
    {
        return false;
    }
    if manifest.get("observability_role").and_then(|v| v.as_str()) == Some("C") {
        return false;
    }
    true
}

/// Build a new plan from unfinished positions, preserving the frozen order.
pub fn capture_continuation(
    root: &Path,
    session_id: &str,
    allow_complete: bool,
) -> Result<Option<CaptureContinuation>, String> {
    if !is_hex_session_id(session_id) {
        return Err("Continuation requires a local recording session ID".to_string());
    }
    let root_canon = fs::canonicalize(root)
        .map_err(|e| format!("Continuation root is unavailable: {e}"))?;
    let expected = root_canon.join(session_id);
    let recording_canon = fs::canonicalize(&expected)
        .map_err(|e| format!("Continuation recording directory not found: {e}"))?;
    if recording_canon != expected {
        return Err("Continuation must use the original local recording directory".to_string());
    }

    let manifest_text = fs::read_to_string(recording_canon.join("manifest.json"))
        .map_err(|e| format!("Could not read manifest.json: {e}"))?;
    let manifest: Value = serde_json::from_str(&manifest_text)
        .map_err(|e| format!("Invalid manifest.json: {e}"))?;

    if !manifest_is_continuable(&manifest, Some(session_id)) {
        return Err("Continue only a stopped guided development recording".to_string());
    }

    let steps_val = manifest
        .get("capture_plan")
        .and_then(|p| p.get("steps"))
        .cloned()
        .unwrap_or(Value::Array(Vec::new()));
    let steps: Vec<PromptStep> = serde_json::from_value(steps_val)
        .map_err(|e| format!("Invalid capture_plan.steps in manifest.json: {e}"))?;

    let mut index: i64 = manifest
        .get("guide_start_step")
        .and_then(|v| v.as_i64())
        .unwrap_or(0);
    let mut resolved: HashSet<usize> = HashSet::new();

    let events_text = fs::read_to_string(recording_canon.join("events.jsonl"))
        .map_err(|e| format!("Could not read events.jsonl: {e}"))?;
    for line in events_text.lines() {
        if line.trim().is_empty() {
            continue;
        }
        let event: Value =
            serde_json::from_str(line).map_err(|e| format!("Invalid events.jsonl line: {e}"))?;
        let kind = event.get("kind").and_then(|v| v.as_str()).unwrap_or("");
        let action = event.get("action").and_then(|v| v.as_str()).unwrap_or("");
        if kind != "guide_action" || !matches!(action, "confirm" | "skip" | "select_step") {
            continue;
        }
        if index < 0 || (index as usize) >= steps.len() {
            return Err("Recording events do not match the frozen guide order".to_string());
        }
        let step_idx = event
            .get("step_index")
            .and_then(|v| v.as_i64())
            .unwrap_or(index);
        let prompt_id = event.get("prompt_id").and_then(|v| v.as_str());
        if step_idx != index || prompt_id != Some(steps[index as usize].id.as_str()) {
            return Err("Recording events do not match the frozen guide order".to_string());
        }
        if action == "select_step" {
            index = event
                .get("next_step_index")
                .and_then(|v| v.as_i64())
                .ok_or_else(|| "Recording events do not match the frozen guide order".to_string())?;
        } else {
            resolved.insert(index as usize);
            index += 1;
        }
    }

    let remaining: Vec<PromptStep> = steps
        .into_iter()
        .enumerate()
        .filter_map(|(i, step)| {
            if !resolved.contains(&i)
                && step.id != "negative_occluded"
                && step.id != "remount"
            {
                Some(step)
            } else {
                None
            }
        })
        .collect();

    if remaining.is_empty() {
        if allow_complete {
            return Ok(None);
        }
        return Err("No unfinished actions in this recording".to_string());
    }

    let first = &remaining[0].title;
    let preset = CapturePreset {
        id: "continuation".to_string(),
        name: BilingualText::new("续录剩余动作", "Continue remaining actions"),
        description: BilingualText::new(
            format!("从「{}」开始，之前的录制会保留。", first.zh),
            format!(
                "Starts at \u{201c}{}\u{201d}. The previous recording is kept.",
                first.en
            ),
        ),
        steps: remaining,
    };

    let str_field = |k: &str| -> Result<String, String> {
        manifest
            .get(k)
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .ok_or_else(|| format!("Manifest missing string field {k}"))
    };

    Ok(Some(CaptureContinuation {
        preset,
        session_id: session_id.to_string(),
        person_id: str_field("person_id")?,
        fit_id: str_field("fit_id")?,
        source_group_id: str_field("source_group_id")?,
        label_stage: str_field("label_stage")?,
        role: manifest
            .get("observability_role")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string()),
    }))
}

pub fn latest_capture_continuation(root: &Path) -> Result<Option<CaptureContinuation>, String> {
    if !root.is_dir() {
        return Ok(None);
    }
    let Ok(entries) = fs::read_dir(root) else {
        return Ok(None);
    };
    let mut candidates: Vec<((i64, u32), String)> = Vec::new();
    for entry in entries.flatten() {
        let name = match entry.file_name().into_string() {
            Ok(n) => n,
            Err(_) => continue,
        };
        if !is_hex_session_id(&name) {
            continue;
        }
        let path = entry.path();
        let Ok(meta) = fs::symlink_metadata(&path) else {
            continue;
        };
        if meta.file_type().is_symlink() || !meta.is_dir() {
            continue;
        }
        let Ok(manifest_text) = fs::read_to_string(path.join("manifest.json")) else {
            continue;
        };
        let Ok(manifest) = serde_json::from_str::<Value>(&manifest_text) else {
            continue;
        };
        if manifest_is_continuable(&manifest, None) {
            let Some(created) = manifest.get("created_utc").and_then(|v| v.as_str()) else {
                continue;
            };
            let Ok(dt) = DateTime::parse_from_rfc3339(created) else {
                continue;
            };
            candidates.push(((dt.timestamp(), dt.timestamp_subsec_nanos()), name));
        }
    }
    if let Some((_, best_name)) = candidates.into_iter().max() {
        capture_continuation(root, &best_name, true)
    } else {
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::face_capture::{BeginOptions, MouthGuide, WholeMouthRecording};
    use std::collections::HashSet;

    #[test]
    fn named_presets_have_explicit_distinct_orders_and_derived_lengths() {
        let presets: HashMap<String, CapturePreset> = capture_presets()
            .into_iter()
            .map(|p| (p.id.clone(), p))
            .collect();
        let lens: HashMap<&str, usize> = presets
            .iter()
            .map(|(k, p)| (k.as_str(), p.steps.len()))
            .collect();
        assert_eq!(lens["basics"], 12);
        assert_eq!(lens["tongue"], 11);
        assert_eq!(lens["lips"], 13);
        assert_eq!(lens["complete"], 71);
        assert_eq!(presets["complete"].steps, prompt_plan());

        let complete_ids: HashSet<&str> = presets["complete"]
            .steps
            .iter()
            .map(|s| s.id.as_str())
            .collect();
        assert!(!complete_ids.contains("negative_occluded"));
        assert!(!complete_ids.contains("remount"));

        for preset in presets.values() {
            let text = serde_json::to_string(&(&preset.name, &preset.description))
                .unwrap()
                .to_lowercase();
            for word in ["重新佩戴", "遮挡", "未知", "refit", "occlu", "unknown"] {
                assert!(!text.contains(word));
            }
        }
        let basics_first3: Vec<&str> = presets["basics"].steps[..3]
            .iter()
            .map(|s| s.id.as_str())
            .collect();
        assert_eq!(basics_first3, vec!["neutral", "JawOpen", "MouthClose"]);
        let tongue_mid3: Vec<&str> = presets["tongue"].steps[1..4]
            .iter()
            .map(|s| s.id.as_str())
            .collect();
        assert_eq!(
            tongue_mid3,
            vec!["tongue_short", "tongue_medium", "tongue_long"]
        );
    }

    #[test]
    fn reference_manifest_matches_plan_and_hides_frozen_only_entries() {
        let manifest_path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../web/guide-reference/manifest.json");
        if !manifest_path.is_file() {
            return;
        }
        let manifest: Value =
            serde_json::from_str(&fs::read_to_string(&manifest_path).unwrap()).unwrap();
        let poses: HashMap<String, &Value> = manifest["poses"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| (p["id"].as_str().unwrap().to_string(), p))
            .collect();
        let steps = prompt_plan();
        for step in &steps {
            let pose = poses[&step.id];
            assert_eq!(pose["instruction"]["zh"].as_str(), Some(step.title.zh.as_str()));
            assert_eq!(pose["instruction"]["en"].as_str(), Some(step.title.en.as_str()));
            assert!(pose.get("legacy_only").is_none());
        }
        let legacy: HashSet<String> = poses
            .iter()
            .filter_map(|(k, p)| {
                if p.get("legacy_only").and_then(|v| v.as_bool()) == Some(true) {
                    Some(k.clone())
                } else {
                    None
                }
            })
            .collect();
        let step_ids: HashSet<String> = steps.iter().map(|s| s.id.clone()).collect();
        let all_pose_ids: HashSet<String> = poses.keys().cloned().collect();
        let diff: HashSet<String> = all_pose_ids.difference(&step_ids).cloned().collect();
        assert_eq!(legacy, diff);
        let expected_legacy: HashSet<String> = ["negative_occluded", "remount"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        assert_eq!(legacy, expected_legacy);
    }

    #[test]
    fn guide_uses_selected_order_and_finishes_at_its_own_total() {
        for preset in capture_presets() {
            let mut guide = MouthGuide::new(&preset.id, false, None, 0, 3.0).unwrap();
            let mut seen = Vec::new();
            while let Some(step) = guide.step() {
                seen.push(step.id.clone());
                guide.advance("skip", None).unwrap();
            }
            let expected: Vec<String> = preset.steps.iter().map(|s| s.id.clone()).collect();
            assert_eq!(seen, expected);
            let status = guide.status();
            assert_eq!(status["total"].as_u64(), Some(seen.len() as u64));
            assert_eq!(status["phase"].as_str(), Some("finished"));
            assert_eq!(status["progress"].as_f64(), Some(1.0));
        }
    }

    #[test]
    fn sealed_presets_match_expected_steps_and_empty_labels() {
        let mouth = capture_preset(SEALED_PRESET).unwrap();
        let expected_mouth: Vec<String> = std::iter::once("neutral".to_string())
            .chain(
                ["small", "large"]
                    .iter()
                    .flat_map(|o| {
                        ["none", "weak", "medium", "strong"]
                            .iter()
                            .map(move |l| format!("c_{o}_{l}"))
                    }),
            )
            .chain(
                ["negative_teeth", "negative_speech", "neutral"]
                    .iter()
                    .map(|s| s.to_string()),
            )
            .collect();
        let got_mouth: Vec<String> = mouth.steps.iter().map(|s| s.id.clone()).collect();
        assert_eq!(got_mouth, expected_mouth);
        assert!(mouth.steps.iter().all(|s| s.labels.is_empty()));

        let jaw = capture_preset(SEALED_JAW_PRESET).unwrap();
        assert_eq!(jaw.steps.len(), 9);
        assert!(jaw.steps.iter().all(|s| s.labels.is_empty()));
        assert_eq!(jaw.steps[0].id, "c_jaw_closed_relaxed");
        assert_eq!(jaw.steps[8].id, "c_jaw_closed_relaxed_end");
    }

    #[test]
    fn recording_freezes_preset_before_frames_and_never_generates_labels() {
        let dir = tempfile::tempdir().unwrap();
        let recorder = WholeMouthRecording::new(dir.path());
        let status = recorder
            .begin(BeginOptions {
                person_id: "fixture".into(),
                confirmed: true,
                preset_id: "tongue".into(),
                ..BeginOptions::default()
            })
            .unwrap();
        assert_eq!(status["guide"]["total"].as_u64(), Some(11));
        let manifest_path = Path::new(status["path"].as_str().unwrap()).join("manifest.json");
        let manifest: Value =
            serde_json::from_str(&fs::read_to_string(manifest_path).unwrap()).unwrap();
        let expected_plan = serde_json::to_value(capture_preset("tongue").unwrap()).unwrap();
        assert_eq!(manifest["capture_plan"], expected_plan);
        assert!(recorder.samples().is_empty());
        assert_eq!(status["trainable"].as_u64(), Some(0));
        recorder.stop().unwrap();
    }

    #[test]
    fn unknown_preset_is_rejected_before_recording_directory_creation() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("recordings");
        let recorder = WholeMouthRecording::new(&root);
        let err = recorder.begin(BeginOptions {
            person_id: "fixture".into(),
            confirmed: true,
            preset_id: "missing".into(),
            ..BeginOptions::default()
        });
        assert!(err.is_err());
        assert!(!root.exists());
    }

    #[test]
    fn continuation_preserves_out_of_order_progress_and_rejects_role_c() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("recordings");
        let rec = WholeMouthRecording::new(&root);

        // Role C refused before and after stop.
        rec.begin(BeginOptions {
            person_id: "fixture".into(),
            confirmed: true,
            automatic: true,
            role: Some("C".into()),
            ..BeginOptions::default()
        })
        .unwrap();
        let c_sid = rec.session_id().unwrap();
        assert!(capture_continuation(&root, &c_sid, false)
            .unwrap_err()
            .contains("stopped guided development"));
        rec.stop().unwrap();
        assert!(capture_continuation(&root, &c_sid, false)
            .unwrap_err()
            .contains("stopped guided development"));
        assert!(latest_capture_continuation(&root).unwrap().is_none());

        // Manual out-of-order development recording.
        rec.begin(BeginOptions {
            person_id: "fixture".into(),
            confirmed: true,
            preset_id: "basics".into(),
            step_index: 2,
            ..BeginOptions::default()
        })
        .unwrap();
        let dev_sid = rec.session_id().unwrap();
        let steps = capture_preset("basics").unwrap().steps;
        let first_token = rec.guide_frame().unwrap();
        rec.advance("skip", None, None).unwrap();
        rec.advance("select_step", Some(1), None).unwrap();
        assert!(rec
            .advance("skip", None, Some(&first_token))
            .unwrap_err()
            .contains("step has changed"));
        rec.advance("ready", None, None).unwrap();
        rec.stop().unwrap();

        let cont = latest_capture_continuation(&root).unwrap().unwrap();
        assert_eq!(cont.session_id, dev_sid);
        let expected: Vec<PromptStep> = steps[..2]
            .iter()
            .chain(steps[3..].iter())
            .cloned()
            .collect();
        assert_eq!(cont.preset.steps, expected);
    }
}
