#include "postprocessing.h"

#include <cerrno>
#include <cmath>
#include <cstdlib>
#include <iomanip>
#include <sstream>
#include <locale>
#include <set>

namespace expression_independent {
namespace {
struct Field { const char* key; double PostProcessingConfig::*member; double low, high; bool integer; };
constexpr Field fields[] = {
#define FIELD(member, key, value, low, high, integer) {key, &PostProcessingConfig::member, low, high, integer},
    PICOET_PROCESSING_FIELDS(FIELD)
#undef FIELD
};
std::string trim(const std::string& value) {
    const auto begin = value.find_first_not_of(" \t\r");
    return begin == std::string::npos ? "" : value.substr(begin, value.find_last_not_of(" \t\r") - begin + 1);
}
PostProcessingConfig& runtime() { static PostProcessingConfig value; return value; }
}

bool parse_postprocessing(const std::string& text, PostProcessingConfig& result, std::string& error) {
    PostProcessingConfig candidate;
    std::set<std::string> seen;
    std::istringstream input(text);
    std::string line;
    unsigned number = 0;
    bool version = false;
    error.clear();
    while (std::getline(input, line)) {
        ++number;
        line = trim(line.substr(0, line.find('#')));
        if (line.empty()) continue;
        const auto split = line.find('=');
        const std::string key = trim(line.substr(0, split));
        const std::string value = split == std::string::npos ? "" : trim(line.substr(split + 1));
        auto fail = [&]() { error = "line " + std::to_string(number) + ": " + key; return false; };
        if (split == std::string::npos || !seen.insert(key).second) return fail();
        if (key == "version") {
            if (value != "1") return fail();
            version = true;
            continue;
        }
        const Field* field = nullptr;
        for (const auto& item : fields) if (key == item.key) { field = &item; break; }
        if (!field) return fail();
        char* end;
        errno = 0;
        const double parsed = std::strtod(value.c_str(), &end);
        if (end == value.c_str() || *end || errno || !std::isfinite(parsed) ||
            parsed < field->low || parsed > field->high || (field->integer && std::floor(parsed) != parsed)) return fail();
        candidate.*(field->member) = parsed;
    }
    if (!version) { error = "version=1 is required"; return false; }
    if (candidate.blink_low >= candidate.blink_high) { error = "blink low must be below high"; return false; }
    result = candidate;
    return true;
}

std::string format_postprocessing(const PostProcessingConfig& config) {
    std::ostringstream output;
    output.imbue(std::locale::classic());
    output << "version=1\n" << std::setprecision(17);
    for (const auto& field : fields) output << field.key << '=' << config.*(field.member) << '\n';
    return output.str();
}
PostProcessingConfig balanced_postprocessing() {
    PostProcessingConfig config;
    // Brows settle in about 0.2 s instead of 1 s; brow links, mouth compression and
    // the tongue threshold are relaxed; rest removal and the lip closure addition stay.
    config.brow_curve = 0.7;
    config.brow_power = 2;
    config.inner_outer = 0.4;
    config.wide_brow_gain = 0.15;
    config.blink_brow = 0.2;
    config.eye_k = 14;
    config.brow_current = 0.85;
    config.brow_motion = 0.3;
    config.brow_kalman = 0.6;
    config.brow_q = 1;
    config.log_strength = 0.5;
    config.tongue_threshold = 0.35;
    config.tongue_gain = 1.25;
    config.mouth_adaptive = 0.8;
    config.mouth_k = 9;
    config.mouth_double = 0;
    config.tongue_current = 0.75;
    return config;
}
PostProcessingConfig responsive_postprocessing() {
    PostProcessingConfig config;
    // Every movement follows the model within about 0.1 s with half of the smoothing
    // and no brow links; blink snapping, rest removal and lip closure stay.
    config.brow_curve = 0.3;
    config.brow_power = 2;
    config.inner_outer = 0;
    config.wide_brow = 0;
    config.blink_brow = 0;
    config.eye_adaptive = 0.5;
    config.eye_k = 24;
    config.brow_current = 1;
    config.brow_motion = 0;
    config.brow_kalman = 0.3;
    config.brow_q = 1;
    config.brow_r = 0.05;
    config.log_strength = 0.25;
    config.tongue_threshold = 0.3;
    config.tongue_gain = 1.2;
    config.mouth_adaptive = 0.5;
    config.mouth_k = 16;
    config.mouth_double = 0;
    config.tongue_ema = 0.5;
    config.tongue_current = 0.9;
    return config;
}
PostProcessingConfig raw_postprocessing() {
    PostProcessingConfig config;
    config.eye_enabled = config.mouth_enabled = 0;
    return config;
}
const PostProcessingConfig& runtime_postprocessing() { return runtime(); }
void configure_postprocessing(const PostProcessingConfig& config) { runtime() = config; }
} // namespace expression_independent
