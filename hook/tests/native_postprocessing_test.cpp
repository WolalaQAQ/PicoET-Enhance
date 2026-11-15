// Isolated numerical test with the headset's native Mat/Kalman implementation.
// No inference owner, tracking client, camera, hook or system setting is created.
#include "../src/expression/numerical.h"
#include "../src/expression/mouth.h"
#include <dlfcn.h>
#include <link.h>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <cstdlib>

using namespace expression_independent;
uintptr_t plugin_base;
int locate(dl_phdr_info* info, size_t, void*) {
    if (info->dlpi_name && std::strstr(info->dlpi_name, "/libpxreyetracking.phoenix.so"))
        plugin_base = static_cast<uintptr_t>(info->dlpi_addr);
    return 0;
}
unsigned checks;
void check(bool condition, const char* description) {
    ++checks;
    if (!condition) { std::fprintf(stderr, "FAIL: %s\n", description); std::exit(1); }
}
struct Metrics { int look90 = -1, brow90 = -1, mouth90 = -1; float first_look = 0, first_brow = 0, first_mouth = 0; };
Metrics run(const PostProcessingConfig& config) {
    NativeBrowFilter left(plugin_base, config.brow_q, config.brow_r);
    NativeBrowFilter right(plugin_base, config.brow_q, config.brow_r);
    void* contexts[] = {&left, &right};
    SourceDtState state;
    MouthState mouth;
    Metrics metrics;
    for (unsigned frame = 0; frame < 360; ++frame) {
        float raw[20] {}, post[20], filtered[20], raw_mouth[35] {}, out[52] {};
        if (frame >= 180) {
            raw[LookIn] = 0.15f;
            raw[BrowInnerUp] = 0.35f;
            raw[BrowOuterUp] = 0.35f;
            raw_mouth[1] = 0.2f;
        }
        raw_mouth[0] = raw_mouth[33] = 1;
        const uint64_t stamps[] = {1 + uint64_t(frame) * 11111111, 2 + uint64_t(frame) * 11111111};
        postprocess_eye20(raw, post, config);
        filter_eye20_dt(state, raw, post, filtered, stamps, NativeBrowFilter::predict_dt, contexts, config);
        process_mouth_dt(mouth, raw_mouth, stamps[0], out, config);
        for (float value : filtered) check(std::isfinite(value) && value >= 0 && value <= 1, "bounded native eye output");
        for (float value : out) check(std::isfinite(value) && value >= 0 && value <= 1, "bounded mouth output");
        if (frame >= 180) {
            if (frame == 180) {
                metrics.first_look = filtered[LookIn];
                metrics.first_brow = filtered[BrowInnerUp];
                metrics.first_mouth = out[29];
            }
            if (metrics.look90 < 0 && filtered[LookIn] >= post[LookIn] * 0.9f) metrics.look90 = int(frame - 180 + 1);
            if (metrics.brow90 < 0 && filtered[BrowInnerUp] >= post[BrowInnerUp] * 0.9f) metrics.brow90 = int(frame - 180 + 1);
            auto instant_config = config;
            instant_config.mouth_adaptive = instant_config.tongue_ema = 0;
            MouthState instant;
            float target[52] {};
            process_mouth_dt(instant, raw_mouth, 1, target, instant_config);
            if (metrics.mouth90 < 0 && out[29] >= target[29] * 0.9f) metrics.mouth90 = int(frame - 180 + 1);
        }
    }
    check(state.eye[0].diagnostics.kalman_steps > 0 && state.eye[1].diagnostics.kalman_steps > 0, "both native Kalman objects executed");
    return metrics;
}
int main() {
    void* library = dlopen("/system/lib64/libpxreyetracking.phoenix.so", RTLD_NOW | RTLD_LOCAL);
    if (!library) { std::fprintf(stderr, "dlopen: %s\n", dlerror()); return 2; }
    dl_iterate_phdr(locate, nullptr);
    check(plugin_base != 0, "native plugin mapping");
    const auto original = run({});
    const auto fast = run(responsive_postprocessing());
    check(fast.look90 > 0 && fast.look90 < original.look90, "faster EyeLook step");
    check(fast.brow90 > 0 && fast.brow90 < original.brow90, "faster native Kalman brow step");
    check(fast.mouth90 > 0 && fast.mouth90 < original.mouth90, "faster mouth step");
    std::printf("{\"checks\":%u,\"input\":\"synthetic-step-90Hz\",\"camera_started\":false,"
        "\"original_samples_to_90pct\":[%d,%d,%d],\"fast_samples_to_90pct\":[%d,%d,%d],"
        "\"original_first\":[%.9g,%.9g,%.9g],\"fast_first\":[%.9g,%.9g,%.9g]}\n",
        checks, original.look90, original.brow90, original.mouth90, fast.look90, fast.brow90, fast.mouth90,
        original.first_look, original.first_brow, original.first_mouth, fast.first_look, fast.first_brow, fast.first_mouth);
    dlclose(library);
}
