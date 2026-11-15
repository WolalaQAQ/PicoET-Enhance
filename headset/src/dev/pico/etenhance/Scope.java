package dev.pico.etenhance;

/**
 * Synthetic movements run through the native eye and mouth processing, one update every 1/90 s.
 * The module converts every coefficient with the measured interval between source timestamps;
 * the fixed interval here only makes settings comparable. Each run settles for 1.5 s on the
 * movement's starting pose before the part that is shown.
 */
final class Scope {
    static final double HZ = 90, RATIO = 24 / HZ, FOLLOWED = .9;
    static final int SETTLE = 135, WARMUP = 8, MOTION_BACK = 7;

    static final int DOWN = 0, INNER = 1, OUTER = 2, BLINK = 3, LOOK_DOWN = 4, LOOK_IN = 5, LOOK_OUT = 6, LOOK_UP = 7, SQUINT = 8, WIDE = 9;
    static final int CHEEK_SQUINT_L = 1, JAW = 5, CLOSE = 7, DIMPLE_L = 8, FUNNEL = 12, MOUTH_LEFT = 13,
            MOUTH_RIGHT = 19, SMILE_L = 24, SMILE_R = 25, TONGUE = 32;

    static final class Scenario {
        final String id, page;
        /** The plotted channel, and a raw input channel drawn for context (-1 for none). */
        final int channel, context;
        final double start, release, hold, seconds;
        final String[] metrics;
        Scenario(String id, String page, int channel, int context, double start, double release, double hold, String... metrics) {
            this(id, page, channel, context, start, release, hold, 2, metrics);
        }
        Scenario(String id, String page, int channel, int context, double start, double release, double hold, double seconds, String... metrics) {
            this.id = id; this.page = page; this.channel = channel; this.context = context;
            this.start = start; this.release = release; this.hold = hold; this.seconds = seconds; this.metrics = metrics;
        }
        int frames() { return (int)Math.round(seconds * HZ) + 1; }
        boolean eye() { return page.equals("eye") || page.equals("brow"); }
    }

    static final Scenario[] SCENARIOS = {
        new Scenario("blink", "eye", BLINK, -1, .3, 0, .4, "peak", "end"),
        new Scenario("gaze", "eye", LOOK_IN, -1, .3, 0, 1, "rise", "jitter"),
        new Scenario("raise", "brow", INNER, -1, .3, 1.5, 1.45, 3, "rise", "fall"),
        new Scenario("blink_raise", "brow", INNER, BLINK, .3, 0, 1.5, "rise"),
        new Scenario("mixed", "brow", DOWN, INNER, .3, 0, 1.5, "hold"),
        new Scenario("wide", "brow", INNER, WIDE, .3, 0, 1.5, "hold"),
        new Scenario("brow_blink", "brow", DOWN, BLINK, .5, 0, 1.5, "peak"),
        new Scenario("outer", "brow", OUTER, INNER, .3, 0, 1.5, "hold"),
        new Scenario("talk", "mouth", JAW, -1, .25, 0, 0, "peak", "lag"),
        new Scenario("rest", "mouth", JAW, -1, .3, 0, 0, 3, "end"),
        new Scenario("corners", "mouth", SMILE_L, -1, .3, 0, 0, 3, "end"),
        new Scenario("lips", "mouth", CLOSE, -1, .3, 0, 0, 3, "end"),
        new Scenario("subtle", "mouth", DIMPLE_L, SMILE_L, .3, 0, 1.5, "hold"),
        new Scenario("tongue_out", "tongue", TONGUE, -1, .3, 1.32, 1.2, "rise", "fall"),
        new Scenario("tongue_slight", "tongue", TONGUE, -1, .3, 0, 1.5, "hold"),
    };

    static Scenario scenario(String id) {
        for (Scenario s : SCENARIOS) if (s.id.equals(id)) return s;
        throw new IllegalArgumentException(id);
    }

    static final class Trace {
        final double[] input, output, context;
        Trace(int frames) { input = new double[frames]; output = new double[frames]; context = new double[frames]; }
    }

    private Scope() { }

    static double clamp(double value) { return value > 1 ? 1 : value < 0 ? 0 : value; }
    static double mix(double current, double processed, double strength) {
        if (strength == 0) return current;
        if (strength == 1) return processed;
        return current + strength * (processed - current);
    }
    /** Share per update at the example interval for a share given at the 24 Hz reference interval. */
    static double perUpdate(double reference) { return -StrictMath.expm1(StrictMath.log1p(-reference) * RATIO); }

    static double ease(double t, double t0, double length) {
        if (t <= t0) return 0;
        if (t >= t0 + length) return 1;
        double x = (t - t0) / length;
        return x * x * (3 - 2 * x);
    }
    static double pulse(double t, double t0, double rise, double hold, double fall) {
        return ease(t, t0, rise) - ease(t, t0 + rise + hold, fall);
    }
    static double jitter(double t, double amplitude) {
        double turn = 2 * StrictMath.PI * t;
        return amplitude * (StrictMath.sin(7.3 * turn) + .6 * StrictMath.sin(13.1 * turn + 1) + .4 * StrictMath.sin(23.7 * turn + 2)) / 2;
    }
    static double talk(double t) {
        return .42 * pulse(t, .25, .07, .03, .09) + .30 * pulse(t, .55, .07, .03, .09)
                + .48 * pulse(t, .85, .07, .03, .09) + .35 * pulse(t, 1.2, .07, .03, .09);
    }

    /** One eye: BrowDown, BrowInnerUp, BrowOuterUp, Blink, LookDown, LookIn, LookOut, LookUp, Squint, Wide. */
    static double[] eyeFrame(String id, double t) {
        double[] v = new double[10];
        switch (id) {
            case "blink": v[BLINK] = .03 + .83 * pulse(t, .3, .07, .06, .12) + jitter(t, .005); break;
            case "gaze": v[LOOK_IN] = .5 * ease(t, .3, .03) + jitter(t, .012); break;
            case "raise": {
                double p = pulse(t, .3, .1, 1.1, .15);
                v[INNER] = .6 * p + jitter(t, .015); v[OUTER] = .45 * p; v[DOWN] = .08 + jitter(t + .4, .02); v[BLINK] = .03;
                break;
            }
            case "blink_raise":
                v[INNER] = .6 * ease(t, .3, .1) + jitter(t, .015); v[OUTER] = .45 * ease(t, .3, .1);
                v[BLINK] = .03 + .85 * pulse(t, .28, .07, .06, .12);
                break;
            case "mixed": v[INNER] = .5 * ease(t, .3, .1); v[DOWN] = .25 * ease(t, .3, .1) + jitter(t, .01); break;
            case "wide": v[INNER] = .1 + jitter(t, .01); v[WIDE] = .8 * ease(t, .3, .1); break;
            case "brow_blink":
                v[INNER] = .3; v[OUTER] = .25; v[DOWN] = .05 + jitter(t, .01);
                v[BLINK] = .03 + .85 * pulse(t, .5, .07, .06, .12);
                break;
            case "outer": v[INNER] = .1; v[OUTER] = .1 + .4 * ease(t, .3, .1) + jitter(t, .01); break;
            default: throw new IllegalArgumentException(id);
        }
        for (int i = 0; i < v.length; ++i) v[i] = clamp(v[i]);
        return v;
    }

    /** The 33 mouth channels in the module's mouth order; index 32 is TongueOut. */
    static double[] mouthFrame(String id, double t) {
        double[] v = new double[33];
        switch (id) {
            case "talk":
                v[JAW] = .03 + talk(t) + jitter(t, .006); v[CLOSE] = .03; v[FUNNEL] = .15 * pulse(t, .55, .07, .03, .09);
                break;
            case "rest": case "corners": case "lips": {
                double relax = 1 - ease(t, .3, .15);
                v[JAW] = .05 + .25 * relax + jitter(t, .004); v[CLOSE] = .015 + jitter(t + .3, .003);
                v[SMILE_L] = .025 + .2 * relax + jitter(t + .6, .002); v[SMILE_R] = .022 + .18 * relax;
                break;
            }
            case "subtle": {
                double e = ease(t, .3, .12);
                v[SMILE_L] = .6 * e + jitter(t, .008); v[SMILE_R] = .55 * e; v[CHEEK_SQUINT_L] = .18 * e;
                v[DIMPLE_L] = .12 * e + jitter(t + .2, .004); v[JAW] = .04;
                break;
            }
            case "tongue_out": v[TONGUE] = .75 * pulse(t, .3, .12, .9, .15) + jitter(t, .01); v[JAW] = .35 * ease(t, .2, .1); break;
            case "tongue_slight": v[TONGUE] = .45 * ease(t, .3, .12) + jitter(t, .01); v[JAW] = .25 * ease(t, .2, .1); break;
            default: throw new IllegalArgumentException(id);
        }
        for (int i = 0; i < v.length; ++i) v[i] = clamp(v[i]);
        return v;
    }

    static double power(double value, double p) {
        if (p == 4) return value * value * value * value;
        double magnitude = Math.abs(value);
        return magnitude == 0 ? 0 : StrictMath.exp(p * StrictMath.log(magnitude));
    }

    static double[] postprocessEye(double[] in, ProcessingSettings c) {
        double[] out = in.clone();
        if (c.get("eye.enabled") == 0) return out;
        double down = in[DOWN], up = in[INNER], p = c.get("eye.brow_curve.power");
        double down4 = power(down, p), up4 = power(up, p), denominator = down4 + up4 + 1e-10;
        double processedDown = down4 / denominator * (down + up), processedUp = up4 / denominator * (up + processedDown);
        double linkedUp = mix(up, processedUp, c.get("eye.brow_curve.strength"));
        if (linkedUp < c.get("eye.wide_brow.threshold"))
            linkedUp = mix(linkedUp, in[WIDE] * c.get("eye.wide_brow.gain") + linkedUp, c.get("eye.wide_brow.strength"));
        double linkedDown = mix(down, processedDown, c.get("eye.brow_curve.strength"));
        if (linkedDown < c.get("eye.blink_brow.threshold"))
            linkedDown = mix(linkedDown, in[BLINK] * c.get("eye.blink_brow.gain") + linkedDown, c.get("eye.blink_brow.strength"));
        out[DOWN] = linkedDown;
        out[INNER] = linkedUp;
        out[OUTER] = mix(in[OUTER], linkedUp, c.get("eye.inner_outer.strength"));
        double blink = in[BLINK];
        double snapped = blink < c.get("eye.blink_endpoints.low") ? 0 : blink > c.get("eye.blink_endpoints.high") ? 1 : blink;
        out[BLINK] = mix(blink, snapped, c.get("eye.blink_endpoints.strength"));
        return out;
    }

    static double motionWeight(double[] raw, double[] earlier) {
        double blink = Math.abs(raw[BLINK] - earlier[BLINK]);
        double downUp = Math.abs(raw[LOOK_DOWN] - earlier[LOOK_DOWN]) + Math.abs(raw[LOOK_UP] - earlier[LOOK_UP]);
        double inOut = Math.abs(raw[LOOK_IN] - earlier[LOOK_IN]) + Math.abs(raw[LOOK_OUT] - earlier[LOOK_OUT]);
        return clamp(blink + (downUp + inOut) * .5);
    }

    static double composite(ProcessingSettings c, double previous, double current, double motion) {
        double strength = c.get("eye.brow_composite.strength");
        if (strength == 0) return current;
        double w = c.get("eye.brow_composite.current_weight");
        double alpha = perUpdate(w * w * (1 - motion * c.get("eye.brow_motion.strength")));
        return mix(current, current * alpha + previous * (1 - alpha), strength);
    }

    static double adaptiveEye(ProcessingSettings c, double previous, double current) {
        double strength = c.get("eye.adaptive.strength");
        if (strength == 0) return current;
        double weight = StrictMath.exp(-c.get("eye.adaptive.k") * Math.abs(previous - current) * RATIO);
        return mix(current, previous * weight + current * (1 - weight), strength);
    }

    /** Per-update gain of the brow Kalman filter at the example interval, matched to its 24 Hz steady-state gain. */
    static double kalmanGain(ProcessingSettings c) {
        double relative = c.get("eye.brow_kalman.process_noise") / c.get("eye.brow_kalman.measurement_noise");
        double reference = 2 * relative / (StrictMath.sqrt(relative * relative + 4 * relative) + relative);
        return perUpdate(reference);
    }

    static Trace run(Scenario s, ProcessingSettings c) {
        Trace trace = new Trace(s.frames());
        if (s.eye()) runEye(s, c, trace); else runMouth(s, c, trace);
        return trace;
    }

    private static void runEye(Scenario s, ProcessingSettings c, Trace trace) {
        boolean on = c.get("eye.enabled") != 0;
        double kalmanStrength = c.get("eye.brow_kalman.strength"), gain = kalmanGain(c);
        double noise = c.get("eye.brow_kalman.measurement_noise"), process = noise * gain * gain / (1 - gain);
        int total = SETTLE + s.frames();
        double[][] outputs = new double[total][];
        double[] state = new double[2], posterior = new double[2];
        boolean started = false;
        for (int n = 0; n < total; ++n) {
            double t = (n - SETTLE) / HZ;
            double[] raw = eyeFrame(s.id, t), current = postprocessEye(raw, c), out = new double[10];
            for (int i = 0; i < 10; ++i) current[i] = clamp(current[i]);
            if (n < WARMUP || !on) out = current;
            else {
                double motion = motionWeight(raw, outputs[n - MOTION_BACK]);
                double[] previous = outputs[n - 1];
                for (int i = DOWN; i <= OUTER; ++i) out[i] = composite(c, previous[i], current[i], motion);
                for (int i = BLINK; i < 10; ++i) out[i] = adaptiveEye(c, previous[i], current[i]);
                if (kalmanStrength != 0) {
                    for (int d = 0; d < 2; ++d) {
                        double measured = out[INNER + d];
                        // The filter starts in the state it reaches after running for a long time.
                        if (!started) { state[d] = measured; posterior[d] = noise * gain; }
                        double prior = posterior[d] + process, k = prior / (prior + noise);
                        state[d] += k * (measured - state[d]);
                        posterior[d] = (1 - k) * prior;
                        out[INNER + d] = mix(measured, state[d], kalmanStrength);
                    }
                    started = true;
                }
            }
            for (int i = 0; i < 10; ++i) out[i] = clamp(out[i]);
            outputs[n] = out;
            if (n >= SETTLE) {
                trace.input[n - SETTLE] = raw[s.channel]; trace.output[n - SETTLE] = out[s.channel];
                if (s.context >= 0) trace.context[n - SETTLE] = raw[s.context];
            }
        }
    }

    static boolean doublePass(int index) {
        return index == JAW || index == SMILE_L || index == SMILE_R || index == MOUTH_LEFT || index == MOUTH_RIGHT;
    }

    static void postprocessMouth(double[] m, double[] baseline, ProcessingSettings c) {
        double alpha = perUpdate(c.get("mouth.baseline.current_weight")), window = c.get("mouth.baseline.window");
        double limit = c.get("mouth.baseline.max_abs"), strength = c.get("mouth.baseline.strength");
        for (int i = 0; i < 32; ++i) {
            double center = c.get(i == JAW ? "mouth.baseline.channel5_center" : "mouth.baseline.center");
            if (Math.abs(m[i] - center) < window) baseline[i] = m[i] * alpha + baseline[i] * (1 - alpha);
            if (Math.abs(baseline[i]) < limit) m[i] = mix(m[i], m[i] - baseline[i], strength);
            if (i == CLOSE) {
                double correction = Math.min(baseline[i] / c.get("mouth.channel7_correction.divisor") * c.get("mouth.channel7_correction.scale")
                        + c.get("mouth.channel7_correction.bias"), c.get("mouth.channel7_correction.ceiling"));
                m[i] = mix(m[i], m[i] + correction, c.get("mouth.channel7_correction.strength"));
            }
        }
        double low = 100000, high = -100000;
        for (double value : m) { low = Math.min(low, value); high = Math.max(high, value); }
        double denominator = high - low + 1e-10;
        for (int i = 1; i < 33; ++i) {
            double scale = 1 + c.get("mouth.log_scale.coefficient") * StrictMath.log10((m[i] - low) / denominator + c.get("mouth.log_scale.offset"));
            m[i] = mix(m[i], m[i] * scale, c.get("mouth.log_scale.strength"));
        }
        double tongue = m[TONGUE], threshold = c.get("mouth.tongue_gate.threshold");
        double gated = tongue < threshold ? 0 : tongue > threshold ? tongue * c.get("mouth.tongue_gate.gain") : tongue;
        m[TONGUE] = mix(tongue, gated, c.get("mouth.tongue_gate.strength"));
    }

    private static void runMouth(Scenario s, ProcessingSettings c, Trace trace) {
        boolean on = c.get("mouth.enabled") != 0;
        double k = c.get("mouth.adaptive.k"), adaptive = c.get("mouth.adaptive.strength"), second = c.get("mouth.double_pass.strength");
        double tongueStrength = c.get("mouth.tongue_ema.strength"), tongueAlpha = perUpdate(c.get("mouth.tongue_ema.current_weight"));
        double[] baseline = new double[32], previous = new double[33];
        for (int n = 0, total = SETTLE + s.frames(); n < total; ++n) {
            double t = (n - SETTLE) / HZ;
            double[] raw = mouthFrame(s.id, t), m = raw.clone();
            if (on) postprocessMouth(m, baseline, c);
            for (int i = 0; i < 33; ++i) {
                double value = clamp(m[i]);
                if (n >= WARMUP && on) {
                    if (i == TONGUE) {
                        if (tongueStrength != 0) value = mix(value, value * tongueAlpha + previous[i] * (1 - tongueAlpha), tongueStrength);
                    } else if (adaptive != 0) {
                        double filtered;
                        double weight1 = StrictMath.exp(-k * Math.abs(previous[i] - value)), alpha1 = 1 - weight1;
                        if (doublePass(i)) {
                            double middle = previous[i] * weight1 + value * alpha1;
                            double alpha2 = 1 - StrictMath.exp(-k * Math.abs(middle - previous[i]));
                            double alpha = perUpdate(second == 1 ? alpha1 * alpha2 : alpha1 * (1 - second + second * alpha2));
                            filtered = previous[i] * (1 - alpha) + value * alpha;
                        } else {
                            double alpha = perUpdate(alpha1);
                            filtered = previous[i] * (1 - alpha) + value * alpha;
                        }
                        value = mix(value, filtered, adaptive);
                    }
                }
                previous[i] = clamp(value);
            }
            if (n >= SETTLE) {
                trace.input[n - SETTLE] = raw[s.channel]; trace.output[n - SETTLE] = previous[s.channel];
                if (s.context >= 0) trace.context[n - SETTLE] = raw[s.context];
            }
        }
    }

    static int index(double seconds) { return (int)Math.round(seconds * HZ); }
    static double milliseconds(int frames) { return frames * 1000 / HZ; }
    static boolean timed(String metric) { return metric.equals("rise") || metric.equals("fall") || metric.equals("lag"); }

    /** Frame of a reading on the plotted channel: where a time is reached or a value is read; -1 when not reached. */
    static int frame(String kind, Scenario s, double[] input, double[] y) {
        int start = index(s.start), hold = index(s.hold), last = y.length - 1;
        double low = input[Math.max(0, start - 1)], high = input[hold];
        switch (kind) {
            case "rise":
                for (int n = start; n <= last; ++n) if (y[n] - low >= FOLLOWED * (high - low)) return n;
                return -1;
            case "fall": {
                for (int n = index(s.release); n <= last; ++n) if (y[n] - low <= (1 - FOLLOWED) * (high - low)) return n;
                return -1;
            }
            case "peak": {
                int best = start;
                for (int n = start; n <= last; ++n) if (y[n] > y[best]) best = n;
                return best;
            }
            case "hold": return hold;
            case "end": case "jitter": return last;
            case "lag": {
                int in = 0;
                for (int n = 1; n <= last; ++n) if (input[n] > input[in]) in = n;
                int out = in;
                for (int n = in; n <= Math.min(last, in + index(.3)); ++n) if (y[n] > y[out]) out = n;
                return out;
            }
            default: throw new IllegalArgumentException(kind);
        }
    }

    /**
     * A reading of the plotted channel: milliseconds for rise, fall and lag (infinity when not reached
     * within the chart), the action value for peak, hold and end, and the range for jitter.
     */
    static double metric(String kind, Scenario s, double[] input, double[] y) {
        int at = frame(kind, s, input, y);
        switch (kind) {
            case "rise": return at < 0 ? Double.POSITIVE_INFINITY : milliseconds(at - index(s.start));
            case "fall": return at < 0 ? Double.POSITIVE_INFINITY : milliseconds(at - index(s.release));
            case "lag": {
                int in = 0;
                for (int n = 1; n < input.length; ++n) if (input[n] > input[in]) in = n;
                return milliseconds(at - in);
            }
            case "jitter": {
                double min = 1, max = 0;
                for (int n = index(s.hold); n < y.length; ++n) { min = Math.min(min, y[n]); max = Math.max(max, y[n]); }
                return max - min;
            }
            default: return y[at];
        }
    }

    /** Axis tops whose quarters are whole percentages. */
    static final double[] NICE = {.04, .08, .12, .2, .4, .6, .8, 1};
    static double ceiling(double... maxima) {
        double top = 0;
        for (double value : maxima) top = Math.max(top, value);
        for (double value : NICE) if (value >= top * 1.04) return value;
        return 1;
    }
    static double max(double[] values) {
        double top = 0;
        for (double value : values) top = Math.max(top, value);
        return top;
    }
}
