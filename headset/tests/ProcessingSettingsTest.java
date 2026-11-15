package dev.pico.etenhance;

import java.nio.charset.StandardCharsets;
import java.nio.file.*;

public final class ProcessingSettingsTest {
    private static int checks;
    private static void check(boolean condition) { ++checks; if (!condition) throw new AssertionError("check " + checks); }
    private static void rejected(String value) {
        try { ProcessingSettings.parse(value); throw new AssertionError(value); }
        catch (IllegalArgumentException expected) { ++checks; }
    }
    public static void main(String[] args) throws Exception {
        Path directory = Paths.get(args[0]);
        ProcessingSettings original = new ProcessingSettings();
        check(ProcessingSettings.FIELDS.length == 44);
        check(original.format().split("\n").length == 45);
        check(original.same(ProcessingSettings.parse(original.format())));
        for (String name : new String[]{"default", "balanced", "responsive", "raw"}) {
            String text = new String(Files.readAllBytes(directory.resolve("postprocessing-"+name+".conf")), StandardCharsets.UTF_8);
            ProcessingSettings settings = ProcessingSettings.parse(text);
            check(settings.same(ProcessingSettings.parse(settings.format())));
            if (name.equals("default")) check(settings.same(original));
            for (ProcessingSettings.Field field : ProcessingSettings.FIELDS)
                check(Double.doubleToLongBits(settings.get(field.key)) == Double.doubleToLongBits(ProcessingSettings.parse(settings.format()).get(field.key)));
        }
        // Untouched float32-origin defaults must survive the form without rounding.
        check(Double.doubleToLongBits(original.get("eye.brow_composite.current_weight")) == Double.doubleToLongBits(0x1.333334p-1));
        for (ProcessingSettings.Field field : ProcessingSettings.FIELDS) {
            check(field.progress(field.min) == 0);
            check(field.progress(field.max) == 1000);
            check(field.value(0) == field.min);
            check(field.value(1000) == field.max);
            double previous = field.min;
            for (int n = 0; n <= 1000; ++n) {
                double value = field.value(n);
                check(Double.isFinite(value) && value >= field.min && value <= field.max && value >= previous);
                previous = value;
            }
        }
        // Fine arrows use the field step, not one of the slider's 1001 positions.
        for (ProcessingSettings.Field field : ProcessingSettings.FIELDS) {
            if (field.toggle) continue;
            check(field.nudge(field.min, -1) == field.min);
            check(field.nudge(field.max, 1) == field.max);
            for (int n = 0; n <= 1000; ++n) {
                double current = field.value(n);
                for (int direction : new int[]{-1, 1}) {
                    double next = field.nudge(current, direction);
                    check(Double.isFinite(next) && next >= field.min && next <= field.max);
                    check(direction < 0 ? next <= current : next >= current);
                    double expected = Math.max(field.min, Math.min(field.max, current + direction * field.step));
                    check(Math.abs(next - expected) <= (Math.ulp(current) + Math.ulp(field.step)) * 2);
                }
            }
        }
        ProcessingSettings.Field strength = ProcessingSettings.field("eye.adaptive.strength");
        check(strength.nudge(.8, 1) == .81 && strength.nudge(.8, -1) == .79);
        double stepped = 0;
        for (int n = 0; n < 100; ++n) stepped = strength.nudge(stepped, 1);
        check(stepped == 1);
        for (int n = 0; n < 100; ++n) stepped = strength.nudge(stepped, -1);
        check(stepped == 0);
        ProcessingSettings.Field currentWeight = ProcessingSettings.field("eye.brow_composite.current_weight");
        check(currentWeight.nudge(currentWeight.nudge(currentWeight.initial, 1), -1) == currentWeight.initial);
        check(ProcessingSettings.field("eye.adaptive.k").nudge(16, 1) == 16.1);
        check(ProcessingSettings.field("eye.brow_kalman.process_noise").nudge(.1, 1) == .101);
        // A precisely entered value keeps its offset from the slider grid.
        check(strength.nudge(.123456, 1) == .133456);
        for (String invalid : new String[]{"", "version=2\n", "version=1\nversion=1\n", "version=1\nunknown=1\n",
                "version=1\neye.enabled=.5\n", "version=1\neye.adaptive.k=NaN\n", "version=1\neye.adaptive.k=Infinity\n",
                "version=1\neye.blink_endpoints.low=.9\n", "version=1\nmouth.baseline.window=-1\n",
                "version=1\nmouth.tongue_gate.gain=11\n"}) rejected(invalid);
        ProcessingSettings bounds = new ProcessingSettings();
        try { bounds.set("eye.blink_endpoints.low", .9); throw new AssertionError(); }
        catch (IllegalArgumentException expected) { check(bounds.same(original)); }
        for (String key : new String[]{"eye.blink_endpoints.low", "eye.blink_endpoints.high"}) {
            ProcessingSettings blink = new ProcessingSettings();
            ProcessingSettings.Field field = ProcessingSettings.field(key);
            int direction = key.endsWith(".low") ? 1 : -1;
            blink.set(key, direction > 0 ? .799 : .041);
            String before = blink.format();
            try { blink.set(key, field.nudge(blink.get(key), direction)); throw new AssertionError(); }
            catch (IllegalArgumentException expected) {
                check(expected.getMessage().equals("blink_order"));
                check(blink.format().equals(before));
            }
        }
        bounds.set("eye.blink_endpoints.high", .95); bounds.set("eye.blink_endpoints.low", .9);
        check(bounds.get("eye.blink_endpoints.low") == .9);
        check(ProcessingSettings.field("eye.adaptive.k").logarithmic);
        check(ProcessingSettings.field("eye.brow_kalman.process_noise").logarithmic);
        check(ProcessingSettings.field("mouth.adaptive.strength").display(.25).equals("25%"));

        PanelState state = new PanelState();
        check(!state.ready() && !state.canSave() && !state.canApply());
        state.loaded(original.format());
        check(state.ready() && state.phase().equals("loaded") && !state.canSave() && state.canApply());
        state.draft.set(strength.key, strength.nudge(state.draft.get(strength.key), -1));
        check(state.draft.differences(state.saved) == 1 && state.canSave() && !state.canApply());
        check(state.saved.same(original) && state.applied == null);
        state.draft.set(strength.key, strength.nudge(state.draft.get(strength.key), 1));
        check(state.draft.same(original) && !state.canSave() && state.canApply());
        state.draft.set("eye.adaptive.strength", .25);
        check(state.phase().equals("unsaved") && state.canSave() && !state.canApply());
        check(state.saved.same(original) && state.applied == null);
        String draft = state.draft.format(); state.wrote(draft);
        check(state.phase().equals("saved") && !state.canSave() && state.canApply());
        state.applied();
        check(state.phase().equals("applied") && !state.canSave() && !state.canApply());
        state.draft.set("eye.adaptive.strength", .5);
        check(state.phase().equals("unsaved") && !state.canApply());
        check(state.applied.get("eye.adaptive.strength") == .25);
        state.draft.set("eye.adaptive.strength", .25);
        check(state.phase().equals("applied"));
        PanelState reopened = new PanelState(); reopened.loaded(state.saved.format());
        check(reopened.applied == null && reopened.phase().equals("loaded"));
        reopened.preset(original.format());
        check(reopened.phase().equals("unsaved") && reopened.saved.same(state.saved));
        System.out.println(checks + " typed settings / slider / save-apply state checks passed");
    }
}
