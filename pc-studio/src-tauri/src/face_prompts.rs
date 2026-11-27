//! Bilingual whole-mouth prompts. Instructions are proposals, never observations.

use crate::face_schema::{CHANNELS, STOCK_CHANNELS};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BilingualText {
    pub zh: String,
    pub en: String,
}

impl BilingualText {
    pub fn new(zh: impl Into<String>, en: impl Into<String>) -> Self {
        Self {
            zh: zh.into(),
            en: en.into(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PromptStep {
    pub id: String,
    pub title: BilingualText,
    pub kind: String,
    pub labels: BTreeMap<String, f64>,
    pub hold_s: f64,
    pub reaction_s: f64,
    pub tail_s: f64,
    pub skippable: bool,
    pub prepare: BilingualText,
    pub hold: BilingualText,
    pub confirm: BilingualText,
    pub skip: BilingualText,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub intensity_rank: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub demo_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PromptCoverage {
    pub channels: Vec<String>,
    pub stock_channels: Vec<String>,
    pub covered: Vec<String>,
}

/// All directions are the wearer's own, not a mirrored preview's.
pub const POSES: [(&str, (&str, &str)); 50] = [
    ("CheekPuff", ("双侧轻轻鼓腮", "Gently puff both cheeks")),
    (
        "CheekSquintL",
        ("抬起左侧面颊，轻轻眯左眼", "Raise your left cheek with a gentle left squint"),
    ),
    (
        "CheekSquintR",
        ("抬起右侧面颊，轻轻眯右眼", "Raise your right cheek with a gentle right squint"),
    ),
    (
        "JawForward",
        ("下颌轻轻前伸，牙齿保持放松", "Gently move your jaw forward, keeping your teeth relaxed"),
    ),
    ("JawLeft", ("下颌向自身左侧移动", "Move your jaw to your own left")),
    (
        "JawOpen",
        ("自然张开下颌，舌头留在口内", "Open your jaw naturally, tongue inside your mouth"),
    ),
    ("JawRight", ("下颌向自身右侧移动", "Move your jaw to your own right")),
    (
        "MouthClose",
        ("下颌稍张开但双唇保持闭合", "Lower your jaw slightly while keeping your lips together"),
    ),
    (
        "MouthDimpleL",
        ("左嘴角向内收紧，做左侧酒窝", "Draw your left mouth corner inward to make a dimple"),
    ),
    (
        "MouthDimpleR",
        ("右嘴角向内收紧，做右侧酒窝", "Draw your right mouth corner inward to make a dimple"),
    ),
    ("MouthFrownL", ("左嘴角向下压", "Lower your left mouth corner")),
    ("MouthFrownR", ("右嘴角向下压", "Lower your right mouth corner")),
    (
        "MouthFunnel",
        ("双唇松开，做圆形漏斗口型", "Part your lips into a rounded funnel shape"),
    ),
    ("MouthLeft", ("整张嘴向自身左侧移", "Move your whole mouth to your own left")),
    (
        "MouthLowerDownL",
        ("左半下唇向下拉，露出下齿", "Lower the left half of your lower lip to show lower teeth"),
    ),
    (
        "MouthLowerDownR",
        ("右半下唇向下拉，露出下齿", "Lower the right half of your lower lip to show lower teeth"),
    ),
    (
        "MouthPressL",
        ("轻压左侧上下唇", "Gently press the left sides of your lips together"),
    ),
    (
        "MouthPressR",
        ("轻压右侧上下唇", "Gently press the right sides of your lips together"),
    ),
    ("MouthPucker", ("双唇向前噘起，做亲吻口型", "Pucker both lips forward as for a kiss")),
    ("MouthRight", ("整张嘴向自身右侧移", "Move your whole mouth to your own right")),
    ("MouthRollLower", ("只把下唇向口内卷", "Roll only your lower lip inward")),
    ("MouthRollUpper", ("只把上唇向口内卷", "Roll only your upper lip inward")),
    (
        "MouthShrugLower",
        ("下唇向上推，轻轻覆盖上唇", "Push your lower lip upward toward and slightly over the upper lip"),
    ),
    (
        "MouthShrugUpper",
        ("只把上唇向上耸起，上唇不内卷", "Raise only your upper lip, keeping it from rolling inward"),
    ),
    ("MouthSmileL", ("仅左嘴角轻轻微笑", "Smile gently with only your left mouth corner")),
    ("MouthSmileR", ("仅右嘴角轻轻微笑", "Smile gently with only your right mouth corner")),
    (
        "MouthStretchL",
        ("左嘴角水平向外拉开，嘴角不上扬", "Stretch your left mouth corner straight outward, keeping it level"),
    ),
    (
        "MouthStretchR",
        ("右嘴角水平向外拉开，嘴角不上扬", "Stretch your right mouth corner straight outward, keeping it level"),
    ),
    (
        "MouthUpperUpL",
        ("只抬起左半上唇，露出上齿", "Raise only the left half of your upper lip to show upper teeth"),
    ),
    (
        "MouthUpperUpR",
        ("只抬起右半上唇，露出上齿", "Raise only the right half of your upper lip to show upper teeth"),
    ),
    ("NoseSneerL", ("轻皱左鼻翼", "Gently wrinkle the left side of your nose")),
    ("NoseSneerR", ("轻皱右鼻翼", "Gently wrinkle the right side of your nose")),
    (
        "TongueOut",
        ("舌头直向前伸到舒适长度", "Extend your tongue straight forward to a comfortable length"),
    ),
    ("TongueLeft", ("舌头向自身左侧伸出", "Extend your tongue to your own left")),
    ("TongueRight", ("舌头向自身右侧伸出", "Extend your tongue to your own right")),
    (
        "TongueUp",
        ("整条舌头朝上伸出，不只是弯起舌尖", "Extend your whole tongue upward, not just the tip"),
    ),
    ("TongueDown", ("舌头朝下伸出", "Extend your tongue downward")),
    (
        "CheekPuffLeft",
        ("只鼓左腮，右侧尽量放松", "Puff only your left cheek and relax the right"),
    ),
    (
        "CheekPuffRight",
        ("只鼓右腮，左侧尽量放松", "Puff only your right cheek and relax the left"),
    ),
    (
        "CheekSuckLeft",
        ("只轻吸左腮，不内卷嘴唇", "Gently suck in only your left cheek without rolling your lips"),
    ),
    (
        "CheekSuckRight",
        ("只轻吸右腮，不内卷嘴唇", "Gently suck in only your right cheek without rolling your lips"),
    ),
    (
        "MouthTightenerLeft",
        (
            "左嘴角水平收紧，区别于压唇或微笑",
            "Tighten your left mouth corner horizontally, distinct from pressing or smiling",
        ),
    ),
    (
        "MouthTightenerRight",
        (
            "右嘴角水平收紧，区别于压唇或微笑",
            "Tighten your right mouth corner horizontally, distinct from pressing or smiling",
        ),
    ),
    (
        "TongueRoll",
        (
            "舌头两侧向上卷成筒形，不只是舌尖上弯；做不到可以跳过",
            "Roll the sides of your tongue into a tube, not just the tip; skip if you cannot",
        ),
    ),
    (
        "MouthUpperLeft",
        ("只让上唇向自身左侧横移，下唇放松", "Move only your upper lip to your own left; relax the lower lip"),
    ),
    (
        "MouthUpperRight",
        ("只让上唇向自身右侧横移，下唇放松", "Move only your upper lip to your own right; relax the lower lip"),
    ),
    (
        "MouthLowerLeft",
        (
            "只让下唇向自身左侧横移，尽量不移动下颌",
            "Move only your lower lip to your own left without moving your jaw",
        ),
    ),
    (
        "MouthLowerRight",
        (
            "只让下唇向自身右侧横移，尽量不移动下颌",
            "Move only your lower lip to your own right without moving your jaw",
        ),
    ),
    (
        "LipFunnelUpper",
        ("只把上唇向外翻成漏斗，下唇放松", "Turn only your upper lip outward into a funnel, lower lip relaxed"),
    ),
    (
        "LipFunnelLower",
        ("只把下唇向外翻成漏斗，上唇放松", "Turn only your lower lip outward into a funnel, upper lip relaxed"),
    ),
];

pub fn pose_text(channel: &str) -> Option<(&'static str, &'static str)> {
    POSES
        .iter()
        .find_map(|&(name, text)| if name == channel { Some(text) } else { None })
}

pub fn make_step(
    key: &str,
    zh: &str,
    en: &str,
    labels: BTreeMap<String, f64>,
    kind: &str,
    hold_s: f64,
    intensity_rank: Option<f64>,
    demo_id: Option<&str>,
) -> PromptStep {
    PromptStep {
        id: key.to_string(),
        title: BilingualText::new(zh, en),
        kind: kind.to_string(),
        labels,
        hold_s,
        reaction_s: 1.0,
        tail_s: 0.3,
        skippable: true,
        prepare: BilingualText::new(
            format!("下一个动作：{zh}。准备好后点「开始保持」。"),
            format!("Next: {en}. Click \u{201c}Start holding\u{201d} when you are ready."),
        ),
        hold: BilingualText::new(
            format!("保持：{zh}。「结束保持」亮起后点击它。"),
            format!("Hold: {en}. Click \u{201c}Finish holding\u{201d} when it becomes available."),
        ),
        confirm: BilingualText::new(
            "点「确认已完成动作」进入下一步。",
            "Click \u{201c}Confirm action done\u{201d} to move on.",
        ),
        skip: BilingualText::new("跳过这一步。", "Skip this step."),
        intensity_rank,
        demo_id: demo_id.map(|s| s.to_string()),
    }
}

fn labels_from(pairs: &[(&str, f64)]) -> BTreeMap<String, f64> {
    pairs.iter().map(|&(k, v)| (k.to_string(), v)).collect()
}

/// JSON-serializable plan for optional browser speech; no audio/device calls.
pub fn prompt_plan() -> Vec<PromptStep> {
    let mut steps = Vec::with_capacity(71);

    steps.push(make_step(
        "neutral",
        "自然放松嘴部，舌头收在口内",
        "Relax your mouth naturally with your tongue inside",
        labels_from(&[("TongueOut", 0.0)]),
        "negative",
        3.0,
        None,
        None,
    ));

    for &name in &CHANNELS {
        let (zh, en) = pose_text(name).expect("every schema channel has a pose");
        steps.push(make_step(
            name,
            zh,
            en,
            labels_from(&[(name, 1.0)]),
            "pose",
            3.0,
            None,
            None,
        ));
    }

    for &(name, value, zh, en) in &[
        (
            "short",
            0.25,
            "舌尖刚超过唇缘，短伸舌",
            "Extend your tongue just past your lips",
        ),
        (
            "medium",
            0.5,
            "舌头向前伸到舒适长度的一半左右",
            "Extend your tongue to about half your comfortable length",
        ),
        (
            "long",
            1.0,
            "舌头充分但舒适地向前伸",
            "Extend your tongue fully but comfortably",
        ),
    ] {
        steps.push(make_step(
            &format!("tongue_{name}"),
            zh,
            en,
            labels_from(&[("TongueOut", value)]),
            "pose",
            3.0,
            Some(value),
            None,
        ));
    }

    steps.push(make_step(
        "tongue_sweep",
        "缓慢短、中、长伸舌，再缓慢收回，重复一次",
        "Slowly extend short, medium, long, then retract; repeat once",
        BTreeMap::new(),
        "continuous",
        6.0,
        None,
        None,
    ));

    for &(side, vertical, zh, en) in &[
        ("Left", "Up", "左上", "upper left"),
        ("Right", "Up", "右上", "upper right"),
        ("Left", "Down", "左下", "lower left"),
        ("Right", "Down", "右下", "lower right"),
    ] {
        let key = format!(
            "tongue_{}_{}",
            side.to_ascii_lowercase(),
            vertical.to_ascii_lowercase()
        );
        let mut labels = BTreeMap::new();
        labels.insert(format!("Tongue{side}"), 0.7);
        labels.insert(format!("Tongue{vertical}"), 0.7);
        steps.push(make_step(
            &key,
            &format!("舌头向自身{zh}方伸出"),
            &format!("Extend your tongue toward your own {en}"),
            labels,
            "combination",
            3.0,
            None,
            None,
        ));
    }

    let combos: [(&str, &str, &str, &[(&str, f64)]); 7] = [
        (
            "lips_opposed_a",
            "上唇左移、下唇右移",
            "Move upper lip left and lower lip right",
            &[("MouthUpperLeft", 1.0), ("MouthLowerRight", 1.0)],
        ),
        (
            "lips_opposed_b",
            "上唇右移、下唇左移",
            "Move upper lip right and lower lip left",
            &[("MouthUpperRight", 1.0), ("MouthLowerLeft", 1.0)],
        ),
        (
            "funnel_both",
            "上下唇同时外翻成漏斗",
            "Funnel upper and lower lips outward together",
            &[("LipFunnelUpper", 1.0), ("LipFunnelLower", 1.0)],
        ),
        (
            "suck_both",
            "双侧轻吸腮，不内卷唇",
            "Gently suck both cheeks inward without rolling lips",
            &[("CheekSuckLeft", 1.0), ("CheekSuckRight", 1.0)],
        ),
        (
            "jaw_tongue_small",
            "小幅张嘴并中等伸舌",
            "Open your jaw slightly and extend your tongue halfway",
            &[("TongueOut", 0.5)],
        ),
        (
            "jaw_tongue_large",
            "大幅张嘴并保持相同中等伸舌长度",
            "Open your jaw widely with the same halfway tongue extension",
            &[("TongueOut", 0.5)],
        ),
        (
            "smile_puff",
            "微笑并轻鼓左腮",
            "Smile and gently puff your left cheek",
            &[("CheekPuffLeft", 0.5)],
        ),
    ];

    for (name, zh, en, labels) in combos {
        steps.push(make_step(
            name,
            zh,
            en,
            labels_from(labels),
            "combination",
            3.0,
            None,
            None,
        ));
    }

    for &(name, zh, en) in &[
        (
            "teeth",
            "露出牙齿微笑，舌头留在口内",
            "Smile showing your teeth, tongue inside your mouth",
        ),
        (
            "wide_open",
            "大张嘴，舌头留在口内",
            "Open your mouth wide, tongue inside your mouth",
        ),
        (
            "pucker_release",
            "噘嘴再放松，舌头留在口内",
            "Pucker, then relax, tongue inside your mouth",
        ),
        (
            "speech",
            "自然说一小段话，不用刻意伸舌",
            "Speak a short passage naturally, with no need to stick out your tongue",
        ),
        (
            "tip_curl_not_roll",
            "只弯起舌尖，舌头两侧保持平展",
            "Curl only your tongue tip, keeping the sides of the tongue flat",
        ),
    ] {
        let labels = match name {
            "tip_curl_not_roll" => labels_from(&[("TongueRoll", 0.0)]),
            "teeth" | "wide_open" | "pucker_release" => labels_from(&[("TongueOut", 0.0)]),
            _ => BTreeMap::new(),
        };
        steps.push(make_step(
            &format!("negative_{name}"),
            zh,
            en,
            labels,
            "negative",
            5.0,
            None,
            None,
        ));
    }

    steps
}

pub fn coverage() -> PromptCoverage {
    let covered: BTreeSet<String> = prompt_plan()
        .into_iter()
        .flat_map(|step| step.labels.into_keys())
        .collect();
    PromptCoverage {
        channels: CHANNELS.iter().map(|s| s.to_string()).collect(),
        stock_channels: STOCK_CHANNELS.iter().map(|s| s.to_string()).collect(),
        covered: covered.into_iter().collect(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::{HashMap, HashSet};

    #[test]
    fn complete_named_channel_coverage_without_reordering() {
        let pose_keys: HashSet<&str> = POSES.iter().map(|&(k, _)| k).collect();
        let channel_set: HashSet<&str> = CHANNELS.iter().copied().collect();
        assert_eq!(pose_keys, channel_set);

        let cov = coverage();
        let covered_set: HashSet<&str> = cov.covered.iter().map(|s| s.as_str()).collect();
        assert_eq!(covered_set, channel_set);
        assert_eq!(
            cov.stock_channels,
            STOCK_CHANNELS.iter().map(|s| s.to_string()).collect::<Vec<_>>()
        );

        let plan = prompt_plan();
        assert_eq!(plan.len(), 71);
        let channel_ids: Vec<&str> = plan[1..51].iter().map(|s| s.id.as_str()).collect();
        assert_eq!(channel_ids, CHANNELS.to_vec());
    }

    #[test]
    fn every_step_has_bilingual_phases_and_skip() {
        let steps = prompt_plan();
        let unique_ids: HashSet<&str> = steps.iter().map(|s| s.id.as_str()).collect();
        assert_eq!(unique_ids.len(), steps.len());
        let channel_set: HashSet<&str> = CHANNELS.iter().copied().collect();

        for step in &steps {
            for text in [&step.prepare, &step.hold, &step.confirm, &step.skip] {
                assert!(!text.zh.is_empty());
                assert!(!text.en.is_empty());
            }
            assert!(step.skippable);
            for label in step.labels.keys() {
                assert!(channel_set.contains(label.as_str()));
            }
        }
    }

    #[test]
    fn all_diagonals_and_length_order() {
        let steps: HashMap<String, PromptStep> = prompt_plan()
            .into_iter()
            .map(|s| (s.id.clone(), s))
            .collect();
        for &(side, side_cap) in &[("left", "Left"), ("right", "Right")] {
            for &(vert, vert_cap) in &[("up", "Up"), ("down", "Down")] {
                let labels = &steps[&format!("tongue_{side}_{vert}")].labels;
                let keys: HashSet<&str> = labels.keys().map(|s| s.as_str()).collect();
                let expected: HashSet<String> =
                    [format!("Tongue{side_cap}"), format!("Tongue{vert_cap}")]
                        .into_iter()
                        .collect();
                let expected_ref: HashSet<&str> = expected.iter().map(|s| s.as_str()).collect();
                assert_eq!(keys, expected_ref);
            }
        }

        let lengths: Vec<f64> = ["short", "medium", "long"]
            .iter()
            .map(|name| steps[&format!("tongue_{name}")].labels["TongueOut"])
            .collect();
        assert_eq!(lengths, vec![0.25, 0.5, 1.0]);
        assert!(steps["tongue_sweep"].labels.is_empty());
    }

    #[test]
    fn unknown_not_unspecified_zero() {
        for step in prompt_plan() {
            if step.kind == "pose" {
                assert_eq!(step.labels.len(), 1);
            }
        }
        assert!(pose_text("TongueRoll").unwrap().1.contains("skip"));
        let last_json = serde_json::to_value(prompt_plan().last().unwrap()).unwrap();
        assert!(last_json.get("requires_new_session").is_none());
    }

    #[test]
    fn timed_steps_are_wearer_actions_without_reviewer_rules() {
        let steps = prompt_plan();
        let kinds: HashSet<&str> = steps.iter().map(|s| s.kind.as_str()).collect();
        let expected: HashSet<&str> = ["pose", "negative", "continuous", "combination"]
            .into_iter()
            .collect();
        assert_eq!(kinds, expected);
        assert_eq!(steps.last().unwrap().id, "negative_tip_curl_not_roll");

        let banned = [
            "未知",
            "遮挡",
            "画外",
            "画面内",
            "审核",
            "标签",
            "重新佩戴",
            "取下",
            "unknown",
            "covered",
            "out of view",
            "review",
            "label",
            "refit",
            "take off",
        ];
        for step in &steps {
            for text in [
                &step.prepare,
                &step.hold,
                &step.confirm,
                &step.skip,
                &step.title,
            ] {
                for s in [&text.zh, &text.en] {
                    let lower = s.to_lowercase();
                    for w in banned {
                        assert!(
                            !lower.contains(w),
                            "step {} contained banned word {w} in {s}",
                            step.id
                        );
                    }
                }
            }
        }
    }

    #[test]
    fn plans_are_independent() {
        let mut first = prompt_plan();
        first[0].labels.insert("TongueLeft".to_string(), 0.0);
        assert!(!prompt_plan()[0].labels.contains_key("TongueLeft"));
    }
}
