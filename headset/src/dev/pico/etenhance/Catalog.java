package dev.pico.etenhance;

import java.util.*;

/**
 * The panel is organised around movements the user makes in front of a mirror. Each movement
 * lists the settings that shape it; a setting may appear under several movements, and every
 * non-switch setting appears under at least one. "All settings" lists every field by processing step.
 */
final class Catalog {
    static final String[] PARTS = {"eye", "brow", "mouth", "tongue"};

    static final class Moment {
        final String id;
        final String[] fields;
        Moment(String id, String... fields) { this.id = id; this.fields = fields; }
        Scope.Scenario scenario() { return Scope.scenario(id); }
        String part() { return scenario().page; }
        String master() { return scenario().eye() ? "eye.enabled" : "mouth.enabled"; }
    }

    static final Moment[] MOMENTS = {
        new Moment("blink", "eye.blink_endpoints.high", "eye.blink_endpoints.low", "eye.blink_endpoints.strength",
                "eye.adaptive.strength", "eye.adaptive.k"),
        new Moment("gaze", "eye.adaptive.strength", "eye.adaptive.k"),
        new Moment("raise", "eye.brow_kalman.strength", "eye.brow_kalman.process_noise", "eye.brow_kalman.measurement_noise",
                "eye.brow_composite.strength", "eye.brow_composite.current_weight"),
        new Moment("blink_raise", "eye.brow_motion.strength", "eye.brow_composite.strength", "eye.brow_composite.current_weight"),
        new Moment("mixed", "eye.brow_curve.strength", "eye.brow_curve.power"),
        new Moment("wide", "eye.wide_brow.strength", "eye.wide_brow.gain", "eye.wide_brow.threshold"),
        new Moment("brow_blink", "eye.blink_brow.strength", "eye.blink_brow.gain", "eye.blink_brow.threshold"),
        new Moment("outer", "eye.inner_outer.strength"),
        new Moment("talk", "mouth.double_pass.strength", "mouth.adaptive.strength", "mouth.adaptive.k"),
        new Moment("rest", "mouth.baseline.strength", "mouth.baseline.channel5_center", "mouth.baseline.window",
                "mouth.baseline.current_weight", "mouth.baseline.max_abs", "mouth.adaptive.k", "mouth.double_pass.strength"),
        new Moment("corners", "mouth.baseline.strength", "mouth.baseline.center", "mouth.baseline.window",
                "mouth.baseline.current_weight", "mouth.baseline.max_abs", "mouth.adaptive.k", "mouth.double_pass.strength"),
        new Moment("lips", "mouth.channel7_correction.strength", "mouth.channel7_correction.bias", "mouth.channel7_correction.ceiling",
                "mouth.channel7_correction.scale", "mouth.channel7_correction.divisor"),
        new Moment("subtle", "mouth.log_scale.strength", "mouth.log_scale.coefficient", "mouth.log_scale.offset"),
        new Moment("tongue_out", "mouth.tongue_ema.strength", "mouth.tongue_ema.current_weight"),
        new Moment("tongue_slight", "mouth.tongue_gate.threshold", "mouth.tongue_gate.strength", "mouth.tongue_gate.gain"),
    };

    /** Native processing steps, in order, for the complete list. */
    static final String[] STEPS = {"eye_master", "brow_curve", "wide_brow", "blink_brow", "inner_outer", "blink_endpoints",
            "eye_adaptive", "brow_composite", "brow_motion", "brow_kalman", "mouth_master", "baseline", "channel7", "log_scale",
            "tongue_gate", "mouth_adaptive", "double_pass", "tongue_ema"};

    private Catalog() { }

    static Moment moment(String id) {
        for (Moment moment : MOMENTS) if (moment.id.equals(id)) return moment;
        return null;
    }

    static List<Moment> moments(String part) {
        List<Moment> result = new ArrayList<>();
        for (Moment moment : MOMENTS) if (moment.part().equals(part)) result.add(moment);
        return result;
    }

    static List<ProcessingSettings.Field> step(String group) {
        List<ProcessingSettings.Field> result = new ArrayList<>();
        for (ProcessingSettings.Field field : ProcessingSettings.FIELDS) if (field.group.equals(group)) result.add(field);
        return result;
    }

    static String master(ProcessingSettings.Field field) {
        return field.section.equals("eye") || field.section.equals("brow") ? "eye.enabled" : "mouth.enabled";
    }

    static String strengthOf(String group) {
        for (ProcessingSettings.Field field : ProcessingSettings.FIELDS)
            if (field.group.equals(group) && field.strength()) return field.key;
        return null;
    }

    /**
     * Why a control currently has no effect: {reason resource, key of the field that switches it off},
     * or null when it acts on the output.
     */
    static String[] inactive(ProcessingSettings draft, ProcessingSettings.Field field) {
        if (field.toggle) return null;
        String master = master(field);
        if (draft.get(master) == 0) return new String[]{"inactive_region", master};
        if (field.group.equals("double_pass") && draft.get("mouth.adaptive.strength") == 0)
            return new String[]{"inactive_follows", "mouth.adaptive.strength"};
        if (field.group.equals("brow_motion") && draft.get("eye.brow_composite.strength") == 0)
            return new String[]{"inactive_follows", "eye.brow_composite.strength"};
        // Rest learning continues at zero subtraction because the MouthClose addition uses it.
        String strength = strengthOf(field.group);
        if (strength != null && !field.key.equals(strength) && draft.get(strength) == 0 && !field.group.equals("baseline"))
            return new String[]{"inactive_stage", strength};
        return null;
    }
}
