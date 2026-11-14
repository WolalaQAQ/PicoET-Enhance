package dev.pico.etenhance;

import java.math.BigDecimal;
import java.math.MathContext;
import java.util.*;

/** Typed form values. The configuration text is only a transport format. */
final class ProcessingSettings {
    static final class Field {
        final String key, section, group;
        final double initial, min, max, step;
        final boolean toggle, percent, logarithmic;
        Field(String key, String section, String group, double initial, double min, double max, double step) {
            this.key = key; this.section = section; this.group = group;
            this.initial = initial; this.min = min; this.max = max; this.step = step;
            toggle = key.endsWith(".enabled");
            percent = key.endsWith(".strength") || key.endsWith(".current_weight");
            logarithmic = min > 0 && max / min >= 10000;
        }
        boolean strength() { return key.endsWith(".strength"); }
        String resource() { return "p_" + key.replace('.', '_'); }
        int progress(double value) {
            double fraction = logarithmic ? Math.log(value / min) / Math.log(max / min) : (value - min) / (max - min);
            return (int)Math.round(fraction * 1000);
        }
        double value(int progress) {
            double fraction = progress / 1000.0;
            double value = logarithmic ? min * Math.pow(max / min, fraction) : min + (max - min) * fraction;
            value = logarithmic ? new BigDecimal(value, new MathContext(3)).doubleValue() : Math.round(value / step) * step;
            return Math.max(min, Math.min(max, value));
        }
        String display(double value) {
            return percent ? number(Math.round(value * 10000) / 100.0) + "%" : number(value);
        }
        /** Six significant digits for labels; precise input still shows the stored double. */
        String brief(double value) {
            return percent ? display(value) : new BigDecimal(value).round(new MathContext(6)).stripTrailingZeros().toPlainString();
        }
    }

    static final Field[] FIELDS = {
        new Field("eye.enabled", "eye", "eye_master", 1, 0, 1, 1),
        new Field("eye.brow_curve.strength", "brow", "brow_curve", 1, 0, 1, .01),
        new Field("eye.brow_curve.power", "brow", "brow_curve", 4, .1, 12, .1),
        new Field("eye.inner_outer.strength", "brow", "inner_outer", 1, 0, 1, .01),
        new Field("eye.wide_brow.strength", "brow", "wide_brow", 1, 0, 1, .01),
        new Field("eye.wide_brow.gain", "brow", "wide_brow", .5, 0, 4, .01),
        new Field("eye.wide_brow.threshold", "brow", "wide_brow", .2, 0, 1, .001),
        new Field("eye.blink_brow.strength", "brow", "blink_brow", 1, 0, 1, .01),
        new Field("eye.blink_brow.gain", "brow", "blink_brow", .5, 0, 4, .01),
        new Field("eye.blink_brow.threshold", "brow", "blink_brow", .8, 0, 1, .001),
        new Field("eye.blink_endpoints.strength", "eye", "blink_endpoints", 1, 0, 1, .01),
        new Field("eye.blink_endpoints.low", "eye", "blink_endpoints", .04, 0, 1, .001),
        new Field("eye.blink_endpoints.high", "eye", "blink_endpoints", .8, 0, 1, .001),
        new Field("eye.adaptive.strength", "eye", "eye_adaptive", 1, 0, 1, .01),
        new Field("eye.adaptive.k", "eye", "eye_adaptive", 9, .01, 1000, .1),
        new Field("eye.brow_composite.strength", "brow", "brow_composite", 1, 0, 1, .01),
        new Field("eye.brow_composite.current_weight", "brow", "brow_composite", 0x1.333334p-1, .01, 1, .001),
        new Field("eye.brow_motion.strength", "brow", "brow_motion", 1, 0, 1, .01),
        new Field("eye.brow_kalman.strength", "brow", "brow_kalman", 1, 0, 1, .01),
        new Field("eye.brow_kalman.process_noise", "brow", "brow_kalman", .1, .000001, 100, .001),
        new Field("eye.brow_kalman.measurement_noise", "brow", "brow_kalman", .1, .000001, 100, .001),
        new Field("mouth.enabled", "mouth", "mouth_master", 1, 0, 1, 1),
        new Field("mouth.baseline.strength", "mouth", "baseline", 1, 0, 1, .01),
        new Field("mouth.baseline.current_weight", "mouth", "baseline", .5, .001, 1, .001),
        new Field("mouth.baseline.center", "mouth", "baseline", .02, 0, 1, .001),
        new Field("mouth.baseline.channel5_center", "mouth", "baseline", .04, 0, 1, .001),
        new Field("mouth.baseline.window", "mouth", "baseline", .02, 0, 1, .001),
        new Field("mouth.baseline.max_abs", "mouth", "baseline", .1, 0, 1, .001),
        new Field("mouth.channel7_correction.strength", "mouth", "channel7", 1, 0, 1, .01),
        new Field("mouth.channel7_correction.divisor", "mouth", "channel7", .04, .001, 1, .001),
        new Field("mouth.channel7_correction.scale", "mouth", "channel7", .02, 0, 1, .001),
        new Field("mouth.channel7_correction.bias", "mouth", "channel7", .08, 0, 1, .001),
        new Field("mouth.channel7_correction.ceiling", "mouth", "channel7", .1, 0, 1, .001),
        new Field("mouth.log_scale.strength", "mouth", "log_scale", 1, 0, 1, .01),
        new Field("mouth.log_scale.coefficient", "mouth", "log_scale", .6, 0, 4, .01),
        new Field("mouth.log_scale.offset", "mouth", "log_scale", .1, .001, 1, .001),
        new Field("mouth.tongue_gate.strength", "tongue", "tongue_gate", 1, 0, 1, .01),
        new Field("mouth.tongue_gate.threshold", "tongue", "tongue_gate", .6, 0, 1, .001),
        new Field("mouth.tongue_gate.gain", "tongue", "tongue_gate", 1.5, 0, 10, .01),
        new Field("mouth.adaptive.strength", "mouth", "mouth_adaptive", 1, 0, 1, .01),
        new Field("mouth.adaptive.k", "mouth", "mouth_adaptive", 6, .01, 1000, .1),
        new Field("mouth.double_pass.strength", "mouth", "double_pass", 1, 0, 1, .01),
        new Field("mouth.tongue_ema.strength", "tongue", "tongue_ema", 1, 0, 1, .01),
        new Field("mouth.tongue_ema.current_weight", "tongue", "tongue_ema", .6, .001, 1, .001)
    };

    private final LinkedHashMap<String, Double> values = new LinkedHashMap<>();
    ProcessingSettings() { for (Field field : FIELDS) values.put(field.key, field.initial); }
    static Field field(String key) {
        for (Field field : FIELDS) if (field.key.equals(key)) return field;
        throw new IllegalArgumentException(key);
    }
    double get(String key) { return values.get(key); }
    void set(String key, double value) {
        Field field = field(key);
        if (!Double.isFinite(value) || value < field.min || value > field.max || (field.toggle && value != Math.rint(value)))
            throw new IllegalArgumentException(key);
        double previous = get(key);
        values.put(key, value);
        if (get("eye.blink_endpoints.low") >= get("eye.blink_endpoints.high")) {
            values.put(key, previous);
            throw new IllegalArgumentException("blink_order");
        }
    }
    static ProcessingSettings parse(String text) {
        ProcessingSettings result = new ProcessingSettings();
        Set<String> seen = new HashSet<>();
        for (String line : text.split("\n")) {
            line = line.split("#", 2)[0].trim();
            if (line.isEmpty()) continue;
            String[] pair = line.split("=", -1);
            if (pair.length != 2 || !seen.add(pair[0].trim())) throw new IllegalArgumentException(line);
            String key = pair[0].trim(), value = pair[1].trim();
            if (key.equals("version")) {
                if (!value.equals("1")) throw new IllegalArgumentException("version");
            } else {
                Field field = field(key);
                double number = Double.parseDouble(value);
                if (!Double.isFinite(number) || number < field.min || number > field.max || (field.toggle && number != Math.rint(number)))
                    throw new IllegalArgumentException(key);
                result.values.put(key, number);
            }
        }
        if (!seen.contains("version")) throw new IllegalArgumentException("version");
        if (result.get("eye.blink_endpoints.low") >= result.get("eye.blink_endpoints.high")) throw new IllegalArgumentException("blink_order");
        return result;
    }
    String format() {
        StringBuilder text = new StringBuilder("version=1\n");
        for (Field field : FIELDS) text.append(field.key).append('=').append(Double.toString(get(field.key))).append('\n');
        return text.toString();
    }
    boolean same(ProcessingSettings other) { return other != null && values.equals(other.values); }
    boolean differs(String key, ProcessingSettings other) { return other != null && !values.get(key).equals(other.values.get(key)); }
    int differences(ProcessingSettings other) {
        int count = 0;
        for (Field field : FIELDS) if (differs(field.key, other)) ++count;
        return count;
    }
    static String number(double value) { return BigDecimal.valueOf(value).stripTrailingZeros().toPlainString(); }
}
