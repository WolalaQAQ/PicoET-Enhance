#include "../src/expression/postprocessing.h"
#include "../src/expression/numerical.h"
#include "../src/expression/mouth.h"
#ifdef PICOET_TEST_BASELINE
#include "baseline/numerical.h"
#include "baseline/mouth.h"
#endif
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <fstream>
#include <sstream>

using namespace expression_independent;
unsigned checks = 0;
#define CHECK(condition) do { ++checks; if (!(condition)) { std::fprintf(stderr, "line %d: %s\n", __LINE__, #condition); std::exit(1); } } while (0)
struct Predictor { float previous[2] {}; unsigned calls = 0; };
void prediction(Predictor& state, const float* measurement, float* result) {
    ++state.calls;
    for (unsigned i = 0; i < 2; ++i) result[i] = state.previous[i] = state.previous[i] * 0.4f + measurement[i] * 0.6f;
}
void predict(void* context, const float* measurement, float* result, double, BrowStepDiagnostics&) {
    prediction(*static_cast<Predictor*>(context), measurement, result);
}
float unit(float x) { return std::clamp(x, 0.f, 1.f); }
constexpr unsigned mouth_slots[33] = {43,29,37,25,32,7,15,5,49,23,27,40,10,24,50,6,20,
    22,34,48,18,14,17,9,19,21,33,42,39,8,1,13,51};

// Every temporal filter off; the size changes stay.
PostProcessingConfig smoothing_off() {
    PostProcessingConfig config;
    config.eye_adaptive = config.brow_composite = config.brow_kalman = 0;
    config.mouth_adaptive = config.mouth_double = config.tongue_ema = 0;
    return config;
}

void parser_tests() {
    std::string error;
    PostProcessingConfig config;
    const auto defaults = format_postprocessing(config);
    CHECK(parse_postprocessing(defaults, config, error));
    CHECK(format_postprocessing(config) == defaults);
    CHECK(parse_postprocessing("# comment\r\nversion=1\r\neye.adaptive.strength=0.25 # tune\r\n", config, error));
    CHECK(config.eye_adaptive == 0.25);
    for (const char* bad : {"", "version=2", "version=1\nversion=1", "version=1\nunknown=0",
            "version=1\neye.enabled=0.5", "version=1\neye.adaptive.strength=NaN",
            "version=1\neye.adaptive.strength=inf", "version=1\neye.adaptive.strength=-1",
            "version=1\neye.adaptive.strength=2", "version=1\neye.adaptive.k=0",
            "version=1\neye.enabled=0\neye.enabled=1", "version=1\neye.enabled=0;reboot",
            "version=1\neye.blink_endpoints.low=0.9\neye.blink_endpoints.high=0.2"}) {
        const auto before = format_postprocessing(config);
        CHECK(!parse_postprocessing(bad, config, error));
        CHECK(!error.empty());
        CHECK(format_postprocessing(config) == before);
    }
    for (auto preset : {smoothing_off(), raw_postprocessing(), balanced_postprocessing(), responsive_postprocessing()}) {
        const auto text = format_postprocessing(preset);
        CHECK(parse_postprocessing(text, config, error));
        CHECK(format_postprocessing(config) == text);
    }
#define FIELD(member, key, value, low, high, integer) \
    CHECK(parse_postprocessing(std::string("version=1\n") + key + "=" + std::to_string(value), config, error));
    PICOET_PROCESSING_FIELDS(FIELD)
#undef FIELD
}

void bypass_tests() {
    std::mt19937 random(879);
    std::uniform_real_distribution<float> value(-0.2f, 1.2f);
    for (auto config : {raw_postprocessing(), smoothing_off()}) {
        SourceDtState eyes;
        MouthState mouth;
        Predictor predictors[2];
        void* contexts[] = {&predictors[0], &predictors[1]};
        for (unsigned frame = 0; frame < 500; ++frame) {
            float raw[20], post[20], filtered[20], raw_mouth[35], output[54];
            for (float& x : raw) x = value(random);
            // Raw model values are finite; curve power experiments also cover negatives.
            for (float& x : raw_mouth) x = value(random);
            raw_mouth[33] = frame < 100 ? 1.f : 0.f;
            std::fill(output, output + 54, -7.f);
            uint64_t stamps[2] = {1 + frame * uint64_t(11111111), 2 + frame * uint64_t(11111111)};
            postprocess_eye20(raw, post, config);
            filter_eye20_dt(eyes, raw, post, filtered, stamps, predict, contexts, config);
            for (unsigned i = 0; i < 20; ++i) {
                CHECK(filtered[i] == unit(post[i]));
                if (!config.eye_enabled) CHECK(filtered[i] == unit(raw[i]));
            }
            process_mouth_dt(mouth, raw_mouth, stamps[0], output, config);
            if (!config.mouth_enabled) {
                for (unsigned i = 0; i < 33; ++i) CHECK(output[mouth_slots[i]] == unit(raw_mouth[i]));
            } else {
                // Same postprocessing, no temporal state: even after warmup output
                // equals a fresh owner's first (unfiltered) result at this input.
                auto fresh = mouth;
                fresh.diagnostics.updates = 0;
                float instant[52] {};
                // Keep the SAME already-updated baseline by disabling its next update.
                auto instant_config = config;
                instant_config.baseline_window = 0;
                process_mouth_dt(fresh, raw_mouth, stamps[0], instant, instant_config);
                for (unsigned slot : mouth_slots) CHECK(output[slot] == instant[slot]);
            }
            CHECK(output[52] == -7.f && output[53] == -7.f);
            float held[20]; std::memcpy(held, filtered, sizeof(held));
            const auto count = eyes.eye[0].diagnostics.filter_steps;
            raw[0] = 0.99f;
            filter_eye20_dt(eyes, raw, post, filtered, stamps, predict, contexts, config);
            CHECK(std::memcmp(held, filtered, sizeof(held)) == 0);
            CHECK(eyes.eye[0].diagnostics.filter_steps == count);
        }
        CHECK(predictors[0].calls == 0 && predictors[1].calls == 0);
        CHECK(mouth.diagnostics.classification == 0); // Bypass never fabricates valid quality.
    }
    // Blink endpoints can be disabled without disabling other eye processing.
    PostProcessingConfig config;
    config.blink_endpoints = 0;
    float raw[20] {}, post[20]; raw[Blink] = 0.9f;
    postprocess_eye20(raw, post, config); CHECK(post[Blink] == raw[Blink]);
    config.blink_endpoints = 1;
    postprocess_eye20(raw, post, config); CHECK(post[Blink] == 1);
    config.inner_outer = 0; raw[BrowOuterUp] = 0.7f;
    postprocess_eye20(raw, post, config); CHECK(post[BrowOuterUp] == 0.7f);
    config.brow_power = 2;
    raw[BrowDown] = 0.3f; raw[BrowInnerUp] = 0.6f; raw[Blink] = 0;
    config.blink_brow = config.wide_brow = 0;
    postprocess_eye20(raw, post, config);
    CHECK(std::fabs(post[BrowDown] - 0.18f) < 0.000001f);
}

void preset_tests(const PostProcessingConfig& preset) {
    CHECK(preset.eye_enabled == 1 && preset.mouth_enabled == 1);
    CHECK(preset.eye_adaptive > 0 && preset.brow_composite > 0 && preset.brow_kalman > 0);
    CHECK(preset.mouth_adaptive > 0 && preset.tongue_ema > 0);
    CHECK(preset.blink_endpoints == 1 && preset.baseline == 1 && preset.channel7 == 1);
    float raw[20] {}, original[20], tuned[20];
    raw[Blink] = 0.02f;
    raw[BrowInnerUp] = 0.1f;
    raw[BrowOuterUp] = 0.45f;
    postprocess_eye20(raw, original);
    postprocess_eye20(raw, tuned, preset);
    CHECK(original[Blink] == 0 && tuned[Blink] == 0);
    CHECK(tuned[BrowOuterUp] > original[BrowOuterUp]);
    // A small one-frame EyeLook step keeps more of its amplitude at 90Hz.
    SourceDtState histories[2];
    Predictor predictors[2][2];
    float out[2][20];
    for (unsigned frame = 0; frame < 100; ++frame) {
        std::fill(raw, raw + 20, 0.f);
        if (frame == 99) raw[LookIn] = 0.15f;
        uint64_t stamps[] = {1 + uint64_t(frame) * 11111111, 1 + uint64_t(frame) * 11111111};
        for (unsigned profile = 0; profile < 2; ++profile) {
            const auto config = profile ? preset : PostProcessingConfig{};
            float post[20];
            postprocess_eye20(raw, post, config);
            void* contexts[] = {&predictors[profile][0], &predictors[profile][1]};
            filter_eye20_dt(histories[profile], raw, post, out[profile], stamps, predict, contexts, config);
        }
    }
    CHECK(out[1][LookIn] > out[0][LookIn] && out[1][LookIn] < 0.15f);
    // Weak mouth shapes and a half-out tongue are kept.
    MouthState states[2];
    float mouth_raw[35] {}, mouth_out[2][52] {};
    mouth_raw[0] = 1;
    mouth_raw[1] = 0.15f;
    mouth_raw[32] = 0.5f;
    mouth_raw[33] = 1;
    process_mouth_dt(states[0], mouth_raw, 1, mouth_out[0]);
    process_mouth_dt(states[1], mouth_raw, 1, mouth_out[1], preset);
    CHECK(mouth_out[1][mouth_slots[1]] > mouth_out[0][mouth_slots[1]]);
    CHECK(mouth_out[0][51] == 0 && mouth_out[1][51] > 0);
}

#ifdef PICOET_TEST_BASELINE
void stock_predict(void* context, const float* measurement, float* result, double,
                    stock_baseline::BrowStepDiagnostics&) {
    prediction(*static_cast<Predictor*>(context), measurement, result);
}
void baseline_tests() {
    std::mt19937 random(2901);
    std::uniform_real_distribution<float> value(0.f, 1.f);
    SourceDtState state;
    stock_baseline::SourceDtState before;
    MouthState mouth;
    stock_baseline::MouthState mouth_before;
    Predictor now_predictors[2], old_predictors[2];
    void* now_contexts[] = {&now_predictors[0], &now_predictors[1]};
    void* old_contexts[] = {&old_predictors[0], &old_predictors[1]};
    uint64_t stamps[2] {100, 200};
    for (unsigned frame = 0; frame < 10000; ++frame) {
        float raw[20], post[20], old_post[20], output[20], old_output[20], raw_mouth[35];
        for (float& x : raw) x = value(random);
        for (float& x : raw_mouth) x = value(random);
        const uint64_t dt = frame % 3 == 0 ? kSourceDtBaseNs : frame % 3 == 1 ? 11111111 : 77777777;
        for (unsigned eye = 0; eye < 2; ++eye) stamps[eye] += dt;
        postprocess_eye20(raw, post);
        stock_baseline::postprocess_eye20(raw, old_post);
        CHECK(std::memcmp(post, old_post, sizeof(post)) == 0);
        filter_eye20_dt(state, raw, post, output, stamps, predict, now_contexts);
        stock_baseline::filter_eye20_dt(before, raw, old_post, old_output, stamps, stock_predict, old_contexts);
        CHECK(std::memcmp(output, old_output, sizeof(output)) == 0);
        float mout[52] {}, old_mout[52] {};
        CHECK(process_mouth_dt(mouth, raw_mouth, stamps[0], mout) ==
            stock_baseline::process_mouth_dt(mouth_before, raw_mouth, stamps[0], old_mout));
        CHECK(std::memcmp(mout, old_mout, sizeof(mout)) == 0);
    }
}
#endif
int main(int argc, char** argv) {
    for (int i = 1; i < argc; ++i) {
        std::ifstream file(argv[i]);
        CHECK(file.good());
        std::ostringstream text; text << file.rdbuf();
        PostProcessingConfig config;
        std::string error;
        CHECK(parse_postprocessing(text.str(), config, error));
        const std::string name(argv[i]);
        if (name.find("balanced") != std::string::npos)
            CHECK(format_postprocessing(config) == format_postprocessing(balanced_postprocessing()));
        if (name.find("responsive") != std::string::npos)
            CHECK(format_postprocessing(config) == format_postprocessing(responsive_postprocessing()));
        if (name.find("raw") != std::string::npos)
            CHECK(format_postprocessing(config) == format_postprocessing(raw_postprocessing()));
    }
    parser_tests(); bypass_tests();
    preset_tests(balanced_postprocessing()); preset_tests(responsive_postprocessing());
#ifdef PICOET_TEST_BASELINE
    baseline_tests();
#endif
    std::printf("%u postprocessing checks passed\n", checks);
}
