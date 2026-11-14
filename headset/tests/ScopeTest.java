package dev.pico.etenhance;

import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;

public final class ScopeTest {
    private static int checks;
    private static void check(boolean condition, String what) { ++checks; if (!condition) throw new AssertionError(what); }
    private static void near(double actual, double expected, double tolerance, String what) {
        check(Math.abs(actual - expected) <= tolerance, what + ": " + actual + " vs " + expected);
    }
    private static ProcessingSettings with(ProcessingSettings base, Object... pairs) {
        ProcessingSettings copy = ProcessingSettings.parse(base.format());
        for (int i = 0; i < pairs.length; i += 2) copy.set((String)pairs[i], ((Number)pairs[i + 1]).doubleValue());
        return copy;
    }
    private static double read(String id, String metric, ProcessingSettings c) {
        Scope.Scenario s = Scope.scenario(id);
        Scope.Trace t = Scope.run(s, c);
        return Scope.metric(metric, s, t.input, t.output);
    }
    private static double model(String id, String metric) {
        Scope.Scenario s = Scope.scenario(id);
        Scope.Trace t = Scope.run(s, new ProcessingSettings());
        return Scope.metric(metric, s, t.input, t.input);
    }

    public static void main(String[] args) throws Exception {
        Path directory = Paths.get(args[0]);
        Map<String, ProcessingSettings> presets = new LinkedHashMap<>();
        for (String name : new String[]{"default", "balanced", "responsive", "raw"})
            presets.put(name, ProcessingSettings.parse(new String(Files.readAllBytes(directory.resolve("postprocessing-" + name + ".conf")), StandardCharsets.UTF_8)));
        ProcessingSettings original = presets.get("default"), raw = presets.get("raw");
        ProcessingSettings smoothingOff = with(original, "eye.adaptive.strength", 0, "eye.brow_composite.strength", 0, "eye.brow_kalman.strength", 0,
                "mouth.adaptive.strength", 0, "mouth.double_pass.strength", 0, "mouth.tongue_ema.strength", 0);

        // Every non-switch field shapes at least one movement, and each movement belongs to one part.
        Set<String> covered = new HashSet<>();
        Set<String> ids = new HashSet<>();
        for (Catalog.Moment moment : Catalog.MOMENTS) {
            check(ids.add(moment.id), moment.id + " unique");
            check(Arrays.asList(Catalog.PARTS).contains(moment.part()), moment.id + " part");
            check(moment.fields.length > 0, moment.id + " has settings");
            for (String key : moment.fields) {
                ProcessingSettings.Field field = ProcessingSettings.field(key);
                check(!field.toggle, key + " toggle on a movement");
                check(Catalog.master(field).equals(moment.master()), key + " master on " + moment.id);
                covered.add(key);
            }
        }
        check(ids.size() == Scope.SCENARIOS.length, "one movement per scenario");
        for (ProcessingSettings.Field field : ProcessingSettings.FIELDS) check(field.toggle != covered.contains(field.key), field.key + " coverage");
        List<String> steps = new ArrayList<>();
        for (ProcessingSettings.Field field : ProcessingSettings.FIELDS) if (!steps.contains(field.group)) steps.add(field.group);
        check(new HashSet<>(steps).equals(new HashSet<>(Arrays.asList(Catalog.STEPS))), "all settings lists every step");
        int listed = 0;
        for (String group : Catalog.STEPS) listed += Catalog.step(group).size();
        check(listed == 44, "all settings lists 44 fields");

        // Bypass: the avatar receives the model value, apart from the 0-1 limit.
        for (Scope.Scenario s : Scope.SCENARIOS) {
            Scope.Trace t = Scope.run(s, raw);
            for (int n = 0; n < t.output.length; ++n) near(t.output[n], Scope.clamp(t.input[n]), 1e-12, s.id + " bypass " + n);
        }

        // Single updates against hand-written native formulas.
        double[] eye = Scope.eyeFrame("raise", .8);
        double[] post = Scope.postprocessEye(eye, original);
        double u = eye[Scope.INNER], d = eye[Scope.DOWN], u4 = Math.pow(u, 4), d4 = Math.pow(d, 4);
        double dd = d4 / (u4 + d4 + 1e-10) * (u + d), uu = u4 / (u4 + d4 + 1e-10) * (u + dd);
        near(post[Scope.DOWN], dd + .5 * eye[Scope.BLINK], 1e-12, "brow curve down plus the blink link");
        near(post[Scope.INNER], uu, 1e-12, "brow curve up");
        near(post[Scope.OUTER], uu, 1e-12, "outer copies inner at 100%");
        double[] wide = Scope.eyeFrame("wide", 1.5), widePost = Scope.postprocessEye(wide, with(original, "eye.brow_curve.strength", 0));
        near(widePost[Scope.INNER], wide[Scope.INNER] + .5 * wide[Scope.WIDE], 1e-12, "wide link adds g·Wide below the threshold");
        double[] blink = new double[10]; blink[Scope.BLINK] = .03;
        near(Scope.postprocessEye(blink, original)[Scope.BLINK], 0, 0, "blink below the open line snaps to 0");
        blink[Scope.BLINK] = .86;
        near(Scope.postprocessEye(blink, original)[Scope.BLINK], 1, 0, "blink above the closed line snaps to 1");
        near(Scope.perUpdate(.36), .112, .0005, "36% at 24 Hz is about 11.2% at 90 Hz");
        near(Scope.kalmanGain(original), .226, .0005, "Kalman gain at Q = R per 1/90 s");
        double[] mouth = new double[33]; mouth[Scope.TONGUE] = .7; mouth[Scope.JAW] = .7;
        ProcessingSettings gateOnly = with(original, "mouth.log_scale.strength", 0, "mouth.baseline.strength", 0, "mouth.channel7_correction.strength", 0);
        Scope.postprocessMouth(mouth, new double[32], gateOnly);
        near(mouth[Scope.TONGUE], 1.05, 1e-12, "tongue 0.7 gated to 1.05 before the limit");
        double[] lips = new double[33], learned = new double[32];
        Scope.postprocessMouth(lips, learned, with(original, "mouth.log_scale.strength", 0));
        near(lips[Scope.CLOSE], .08, 1e-12, "MouthClose gets 0.08 with no learned offset");

        // Readings per preset: what the panel shows for the stock settings and how the presets differ.
        near(read("raise", "rise", original), 1067, 12, "stock brow raise takes about a second");
        check(read("raise", "rise", with(original, "eye.brow_kalman.strength", 0)) < 400, "brows fast without the second layer");
        // What the preset descriptions promise.
        ProcessingSettings balanced = presets.get("balanced"), responsive = presets.get("responsive");
        check(read("raise", "rise", balanced) < 250 && read("raise", "fall", balanced) < 250, "balanced brows settle in about 0.2 s");
        check(read("gaze", "jitter", balanced) <= read("gaze", "jitter", original), "balanced gaze is as steady as stock");
        check(read("talk", "peak", balanced) > .43, "balanced jaw keeps most of its peak");
        check(read("rest", "end", balanced) < .01 && read("corners", "end", balanced) < .01, "balanced removes rest leftovers");
        check(read("tongue_slight", "hold", balanced) > .45, "balanced shows a slight tongue");
        check(read("outer", "hold", balanced) > .3, "balanced lets the outer brows move");
        check(read("wide", "hold", balanced) < .25 && read("brow_blink", "peak", balanced) < .12, "balanced brow links are lighter");
        near(read("blink", "peak", balanced), 1, .05, "balanced blink closes");
        near(read("blink", "end", balanced), 0, .01, "balanced blink opens");
        for (String m : new String[]{"raise", "blink_raise"}) check(read(m, "rise", responsive) < 100, "responsive " + m);
        check(read("gaze", "rise", responsive) < 50 && read("talk", "lag", responsive) < 30, "responsive eyes and jaw");
        check(read("wide", "hold", responsive) < model("wide", "hold") + .01 && read("outer", "hold", responsive) > .45, "responsive has no brow links");
        near(read("blink", "end", responsive), 0, .01, "responsive blink opens");
        check(read("tongue_slight", "hold", responsive) > .45, "responsive shows a slight tongue");
        check(read("blink_raise", "rise", original) > read("blink_raise", "rise", with(original, "eye.brow_motion.strength", 0)), "eye movement holds the brows");
        check(read("blink_raise", "rise", with(original, "eye.brow_kalman.strength", 0))
                > read("blink_raise", "rise", with(original, "eye.brow_kalman.strength", 0, "eye.brow_motion.strength", 0)) + 50, "hold is visible without Kalman");
        near(read("blink", "peak", original), 1, .05, "stock blink closes fully");
        near(read("blink", "end", original), 0, .01, "stock blink opens fully");
        near(read("blink", "peak", raw), model("blink", "peak"), 1e-12, "bypass blink peak");
        check(read("blink", "peak", with(original, "eye.blink_endpoints.high", .9)) < .95, "higher closed line stops snapping");
        check(read("gaze", "rise", original) > read("gaze", "rise", smoothingOff), "gaze smoothing adds delay");
        check(read("gaze", "jitter", original) < read("gaze", "jitter", smoothingOff) / 2, "gaze smoothing removes shake");
        check(read("gaze", "rise", with(original, "eye.adaptive.k", 40)) < read("gaze", "rise", original), "higher sensitivity follows faster");
        check(read("mixed", "hold", original) < model("mixed", "hold") / 3, "frown suppressed by default");
        check(read("mixed", "hold", with(original, "eye.brow_curve.power", 1)) > read("mixed", "hold", original), "lower power keeps the frown");
        check(read("wide", "hold", original) > model("wide", "hold") + .3, "wide eyes lift the brows by default");
        check(read("wide", "hold", with(original, "eye.wide_brow.gain", .1)) < read("wide", "hold", original), "less wide link");
        check(read("brow_blink", "peak", original) > model("brow_blink", "peak") + .1, "blink lowers the brows by default");
        check(read("brow_blink", "peak", with(original, "eye.blink_brow.strength", 0)) < read("brow_blink", "peak", original), "blink link off");
        check(read("outer", "hold", original) < model("outer", "hold") / 3, "outer brow pulled to inner");
        check(read("outer", "hold", with(original, "eye.inner_outer.strength", 0)) > .45, "outer brow free");
        check(read("talk", "peak", original) < model("talk", "peak") - .1, "talking jaw peaks lower when smoothed");
        check(read("talk", "lag", original) > read("talk", "lag", with(original, "mouth.double_pass.strength", 0)), "double pass adds lag");
        near(read("talk", "lag", smoothingOff), 0, 0, "no lag without smoothing");
        check(read("rest", "end", original) < model("rest", "end"), "rest leftover reduced");
        check(read("rest", "end", with(original, "mouth.baseline.strength", 0)) > read("rest", "end", original), "rest removal off");
        near(read("rest", "end", smoothingOff), 0, .005, "rest leftover removed without smoothing");
        check(read("corners", "end", with(original, "mouth.baseline.center", .3)) > read("corners", "end", original), "corner leftover outside the moved range stays");
        near(read("lips", "end", original), model("lips", "end") + .08, .02, "stock lip closure addition");
        check(read("lips", "end", with(original, "mouth.channel7_correction.bias", .02)) < read("lips", "end", original), "smaller fixed amount");
        check(read("subtle", "hold", original) < model("subtle", "hold"), "weak shape shrunk");
        check(read("subtle", "hold", with(original, "mouth.log_scale.strength", 0)) > read("subtle", "hold", original), "shrink off");
        near(read("tongue_slight", "hold", original), 0, 0, "stock tongue gate hides 45%");
        check(read("tongue_slight", "hold", with(original, "mouth.tongue_gate.threshold", .3)) > .45, "lower threshold shows it");
        check(read("tongue_out", "rise", original) > read("tongue_out", "rise", smoothingOff), "tongue smoothing adds delay");

        // Random drafts give finite, bounded charts and readings.
        Random random = new Random(20261113);
        for (int round = 0; round < 60; ++round) {
            ProcessingSettings c = new ProcessingSettings();
            for (ProcessingSettings.Field field : ProcessingSettings.FIELDS) {
                if (field.key.startsWith("eye.blink_endpoints.l") || field.key.startsWith("eye.blink_endpoints.h")) continue;
                c.set(field.key, field.toggle ? (random.nextDouble() < .85 ? 1 : 0) : field.value(random.nextInt(1001)));
            }
            for (Scope.Scenario s : Scope.SCENARIOS) {
                Scope.Trace t = Scope.run(s, c);
                check(t.output.length == s.frames(), s.id + " length");
                for (double v : t.output) check(v >= 0 && v <= 1, s.id + " bounded " + round);
                for (String metric : s.metrics) {
                    double value = Scope.metric(metric, s, t.input, t.output);
                    check(!Double.isNaN(value) && (Scope.timed(metric) || value >= 0 && value <= 1), s.id + " " + metric + " " + round);
                }
            }
        }
        System.out.println("SCOPE_TEST_OK checks=" + checks);
    }
}
