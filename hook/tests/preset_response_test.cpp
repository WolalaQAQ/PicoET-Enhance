#include "../src/expression/numerical.h"
#include "../src/expression/mouth.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <vector>

using namespace expression_independent;
namespace {
unsigned checks = 0, cases = 0, faster = 0;
#define CHECK(x) do { ++checks; if (!(x)) { std::fprintf(stderr, "line %d: %s\n", __LINE__, #x); std::exit(1); } } while (0)
constexpr unsigned mouth_slots[33] = {43,29,37,25,32,7,15,5,49,23,27,40,10,24,50,6,20,
    22,34,48,18,14,17,9,19,21,33,42,39,8,1,13,51};

// Scalar model of the stock identity-transition Kalman, not a native-library test.
// Each eye still uses separate state; the actual processing functions are production C++.
struct Brow {
    double q, r;
    float value[2] {}, posterior = 0;
    bool started = false;
    static void predict(void* context, const float* measurement, float* result,
                        double ratio, BrowStepDiagnostics&) {
        auto& s = *static_cast<Brow*>(context);
        const double relative = s.q / s.r;
        const double k0 = 2 * relative / (std::sqrt(relative * relative + 4 * relative) + relative);
        const double pole = std::exp(std::log1p(-k0) * ratio);
        const double k = 1 - pole;
        const float q = static_cast<float>(s.r * k * k / pole);
        const float prior = s.started ? s.posterior + q : 0;
        const float gain = prior / (prior + static_cast<float>(s.r));
        for (unsigned i = 0; i < 2; ++i) {
            s.value[i] += gain * (measurement[i] - s.value[i]);
            result[i] = s.value[i];
        }
        s.posterior = (1 - gain) * prior;
        s.started = true;
    }
};

struct Runner {
    PostProcessingConfig config;
    unsigned channel;
    bool mouth;
    SourceDtState eyes;
    MouthState lips;
    Brow brows[2];
    uint64_t stamp = 1;
    explicit Runner(PostProcessingConfig c, unsigned ch, bool m)
        : config(c), channel(ch), mouth(m), brows{{c.brow_q, c.brow_r}, {c.brow_q, c.brow_r}} {}
    float update(float value, uint64_t dt) {
        stamp += dt;
        if (mouth) {
            float raw[35] {}, output[52] {};
            raw[channel == 0 ? 2 : 0] = 1; // A second channel anchors the log-scale range.
            raw[channel] = value;
            raw[33] = 1;
            process_mouth_dt(lips, raw, stamp, output, config);
            return output[mouth_slots[channel]];
        }
        float raw[20] {}, post[20], output[20];
        raw[channel] = value;
        const uint64_t stamps[] {stamp, stamp};
        void* contexts[] {&brows[0], &brows[1]};
        postprocess_eye20(raw, post, config);
        filter_eye20_dt(eyes, raw, post, output, stamps, Brow::predict, contexts, config);
        return output[channel];
    }
};

float instant(PostProcessingConfig config, unsigned channel, bool mouth, float value) {
    config.eye_adaptive = config.brow_composite = config.brow_kalman = 0;
    config.mouth_adaptive = config.mouth_double = config.tongue_ema = 0;
    Runner runner(config, channel, mouth);
    return runner.update(value, kSourceDtBaseNs);
}

struct Response {
    double rise, fall, pulse, noise;
    float low, high;
};
Response measure(PostProcessingConfig config, unsigned channel, bool mouth,
                 unsigned hz, float low, float high) {
    const uint64_t dt = uint64_t(std::llround(1e9 / hz));
    const float a = instant(config, channel, mouth, low);
    const float b = instant(config, channel, mouth, high);
    Runner runner(config, channel, mouth);
    for (unsigned n = 0; n < 2 * hz; ++n) runner.update(low, dt);
    const unsigned steps = 5 * hz, pulse_steps = unsigned(std::ceil(.1 * hz));
    unsigned rise = steps + 1, fall = steps + 1;
    double pulse = 0;
    for (unsigned n = 1; n <= steps; ++n) {
        const float y = runner.update(high, dt);
        CHECK(std::isfinite(y) && y >= 0 && y <= 1);
        if (b - a > 1e-6f) {
            const double fraction = (y - a) / (b - a);
            if (rise > steps && fraction >= .9) rise = n;
            if (n <= pulse_steps) pulse = std::max(pulse, fraction);
        }
    }
    Runner release(config, channel, mouth);
    for (unsigned n = 0; n < 2 * hz; ++n) release.update(high, dt);
    for (unsigned n = 1; n <= steps; ++n) {
        const float y = release.update(low, dt);
        CHECK(std::isfinite(y) && y >= 0 && y <= 1);
        if (b - a > 1e-6f && fall > steps && (y - a) / (b - a) <= .1) fall = n;
    }
    // Held-pose noise is measured separately from the response target.
    float minimum = 1, maximum = 0;
    for (unsigned n = 0; n < 2 * hz; ++n) {
        const float noise = .002f * std::sin(float(n) * 2 * 3.14159265f * 7.3f / hz);
        const float y = release.update(low + noise, dt);
        if (n >= hz) { minimum = std::min(minimum, y); maximum = std::max(maximum, y); }
    }
    return {rise * 1000. / hz, fall * 1000. / hz, pulse, maximum - minimum, a, b};
}

void weak_wide() {
    const auto config = balanced_postprocessing();
    const auto response = measure(config, Wide, false, 90, .01f, .03f);
    CHECK(response.pulse >= .9); // The old Balanced preset retained only 40.9%.
    CHECK(response.rise <= 100 && response.fall <= 100);
    std::printf("Weak Wide 0.01->0.03: rise %.1f ms, fall %.1f ms, 100ms retained %.2f%%\n",
                response.rise, response.fall, response.pulse * 100);
}
}

int main(int argc, char** argv) {
    CHECK(argc <= 2);
    std::ofstream report;
    if (argc == 2) {
        report.open(argv[1]);
        CHECK(report.good());
        report << std::fixed << std::setprecision(6)
            << "part,channel,hz,delta,stock_rise_ms,balanced_rise_ms,stock_fall_ms,"
               "balanced_fall_ms,stock_pulse,balanced_pulse,stock_noise,balanced_noise,stock_span,balanced_span\n";
    }
    const auto balanced = balanced_postprocessing();
    weak_wide();
    for (bool mouth : {false, true}) {
        for (unsigned channel = 0; channel < (mouth ? 33u : 20u); ++channel) {
            for (unsigned hz : {24u, 45u, 90u}) {
                for (float delta : {.02f, .2f, .55f}) {
                    const bool tongue = mouth && channel == 32;
                    const float low = tongue ? .65f : .1f;
                    const float high = tongue ? std::min(.95f, low + delta) : low + delta;
                    const auto stock = measure({}, channel, mouth, hz, low, high);
                    const auto tuned = measure(balanced, channel, mouth, hz, low, high);
                    ++cases;
                    // Partial smoothing mixes once per observation; allow eight updates
                    // at 24 Hz while keeping the 90 Hz response under 200 ms.
                    const double limit_ms = std::max(200., 8 * 1000. / hz);
                    if (tuned.high - tuned.low <= 1e-5f || tuned.rise > limit_ms || tuned.fall > limit_ms)
                        std::fprintf(stderr, "%s ch%u hz%u delta%.2f tuned rise %.1f fall %.1f span %.6f\n",
                            mouth ? "mouth" : "eye", channel, hz, delta, tuned.rise, tuned.fall, tuned.high - tuned.low);
                    CHECK(tuned.high - tuned.low > 1e-5f);
                    CHECK(tuned.rise <= limit_ms && tuned.fall <= limit_ms);
                    if (stock.high - stock.low > 1e-5f) {
                        if (!(tuned.rise <= stock.rise && tuned.fall <= stock.fall &&
                              tuned.pulse + 1e-5 >= stock.pulse)) {
                            std::fprintf(stderr, "%s ch%u hz%u delta%.2f rise %.1f/%.1f fall %.1f/%.1f pulse %.4f/%.4f\n",
                                mouth ? "mouth" : "eye", channel, hz, delta, stock.rise, tuned.rise,
                                stock.fall, tuned.fall, stock.pulse, tuned.pulse);
                        }
                        CHECK(tuned.rise <= stock.rise && tuned.fall <= stock.fall);
                        CHECK(tuned.pulse + 1e-5 >= stock.pulse);
                        if (tuned.rise < stock.rise || tuned.fall < stock.fall) ++faster;
                    } else {
                        // Stock overwrites raw OuterUp; Balanced must preserve this independent action.
                        CHECK(!mouth && channel % 10 == BrowOuterUp);
                    }
                    if (report.is_open()) report << (mouth ? "mouth" : "eye") << ',' << channel << ',' << hz
                        << ',' << high - low << ',' << stock.rise << ',' << tuned.rise
                        << ',' << stock.fall << ',' << tuned.fall << ',' << stock.pulse << ',' << tuned.pulse
                        << ',' << stock.noise << ',' << tuned.noise
                        << ',' << stock.high - stock.low << ',' << tuned.high - tuned.low << '\n';
                }
            }
        }
    }
    std::printf("%u response cases, %u strictly faster, %u checks passed; production C++, scalar brow Kalman model.\n",
                cases, faster, checks);
}
