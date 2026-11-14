#pragma once

#include <cstddef>
#include <string>

namespace expression_independent {

// One schema shared by the payload and the headset configuration helper.
// Strength 0 bypasses a stage; 1 retains the existing reconstruction.
#define PICOET_PROCESSING_FIELDS(X) \
    X(eye_enabled, "eye.enabled", 1, 0, 1, true) \
    X(brow_curve, "eye.brow_curve.strength", 1, 0, 1, false) \
    X(brow_power, "eye.brow_curve.power", 4, 0.1, 12, false) \
    X(inner_outer, "eye.inner_outer.strength", 1, 0, 1, false) \
    X(wide_brow, "eye.wide_brow.strength", 1, 0, 1, false) \
    X(wide_brow_gain, "eye.wide_brow.gain", 0.5, 0, 4, false) \
    X(wide_brow_threshold, "eye.wide_brow.threshold", 0.2, 0, 1, false) \
    X(blink_brow, "eye.blink_brow.strength", 1, 0, 1, false) \
    X(blink_brow_gain, "eye.blink_brow.gain", 0.5, 0, 4, false) \
    X(blink_brow_threshold, "eye.blink_brow.threshold", 0.8, 0, 1, false) \
    X(blink_endpoints, "eye.blink_endpoints.strength", 1, 0, 1, false) \
    X(blink_low, "eye.blink_endpoints.low", 0.04, 0, 1, false) \
    X(blink_high, "eye.blink_endpoints.high", 0.8, 0, 1, false) \
    X(eye_adaptive, "eye.adaptive.strength", 1, 0, 1, false) \
    X(eye_k, "eye.adaptive.k", 9, 0.01, 1000, false) \
    X(brow_composite, "eye.brow_composite.strength", 1, 0, 1, false) \
    X(brow_current, "eye.brow_composite.current_weight", 0x1.333334p-1, 0.01, 1, false) \
    X(brow_motion, "eye.brow_motion.strength", 1, 0, 1, false) \
    X(brow_kalman, "eye.brow_kalman.strength", 1, 0, 1, false) \
    X(brow_q, "eye.brow_kalman.process_noise", 0.1, 0.000001, 100, false) \
    X(brow_r, "eye.brow_kalman.measurement_noise", 0.1, 0.000001, 100, false) \
    X(mouth_enabled, "mouth.enabled", 1, 0, 1, true) \
    X(baseline, "mouth.baseline.strength", 1, 0, 1, false) \
    X(baseline_alpha, "mouth.baseline.current_weight", 0.5, 0.001, 1, false) \
    X(baseline_center, "mouth.baseline.center", 0.02, 0, 1, false) \
    X(baseline_special_center, "mouth.baseline.channel5_center", 0.04, 0, 1, false) \
    X(baseline_window, "mouth.baseline.window", 0.02, 0, 1, false) \
    X(baseline_limit, "mouth.baseline.max_abs", 0.1, 0, 1, false) \
    X(channel7, "mouth.channel7_correction.strength", 1, 0, 1, false) \
    X(channel7_divisor, "mouth.channel7_correction.divisor", 0.04, 0.001, 1, false) \
    X(channel7_scale, "mouth.channel7_correction.scale", 0.02, 0, 1, false) \
    X(channel7_bias, "mouth.channel7_correction.bias", 0.08, 0, 1, false) \
    X(channel7_ceiling, "mouth.channel7_correction.ceiling", 0.1, 0, 1, false) \
    X(log_strength, "mouth.log_scale.strength", 1, 0, 1, false) \
    X(log_coefficient, "mouth.log_scale.coefficient", 0.6, 0, 4, false) \
    X(log_offset, "mouth.log_scale.offset", 0.1, 0.001, 1, false) \
    X(tongue_gate, "mouth.tongue_gate.strength", 1, 0, 1, false) \
    X(tongue_threshold, "mouth.tongue_gate.threshold", 0.6, 0, 1, false) \
    X(tongue_gain, "mouth.tongue_gate.gain", 1.5, 0, 10, false) \
    X(mouth_adaptive, "mouth.adaptive.strength", 1, 0, 1, false) \
    X(mouth_k, "mouth.adaptive.k", 6, 0.01, 1000, false) \
    X(mouth_double, "mouth.double_pass.strength", 1, 0, 1, false) \
    X(tongue_ema, "mouth.tongue_ema.strength", 1, 0, 1, false) \
    X(tongue_current, "mouth.tongue_ema.current_weight", 0.6, 0.001, 1, false)

struct PostProcessingConfig {
#define FIELD(member, key, value, low, high, integer) double member = value;
    PICOET_PROCESSING_FIELDS(FIELD)
#undef FIELD
};

// Parses a complete candidate transactionally. Unknown/duplicate/invalid fields
// reject the candidate rather than silently re-enabling a filter.
bool parse_postprocessing(const std::string& text, PostProcessingConfig& result, std::string& error);
std::string format_postprocessing(const PostProcessingConfig& config);
PostProcessingConfig balanced_postprocessing();
PostProcessingConfig responsive_postprocessing();
PostProcessingConfig raw_postprocessing();
const PostProcessingConfig& runtime_postprocessing();
void configure_postprocessing(const PostProcessingConfig& config);

inline float processing_mix(float current, float processed, double strength) {
    if (strength == 0) return current;
    if (strength == 1) return processed;
    return current + static_cast<float>(strength) * (processed - current);
}

} // namespace expression_independent
