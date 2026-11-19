#pragma once
#include "core/ptj_source.h"

enum : uint32_t { PTJ_STOCK_VALID = 1, PTJ_STOCK_ASSOCIATED = 2, PTJ_STOCK_WARMUP = 4 };
// Internal main-payload -> adapter marker, cleared before IPC/wire publication.
constexpr uint32_t PTJ_STOCK_INPUT_IDENTITY_V1 = 0x3149534d;
// Native and wire layout: little-endian IEEE754; PSL1 is always 640 bytes.
struct PTJStockRecord {
    char magic[4] {'P','S','L','1'};
    uint16_t version = 1, header_bytes = 96;
    uint32_t record_bytes = 640, flags = 0;
    uint64_t capture_session_id = 0, service_generation = 0, source_seq = 0, mouth_sensor_ns = 0;
    uint64_t stock_input_ns = 0, vendor_ns = 0, raw_identity = 0, produced_mono_ns = 0, label_seq = 0;
    int32_t classification = -1;
    uint32_t stages = 15;
    float raw33[33]{}, instant33[33]{}, final33[33]{}, quality2[2]{};
    float instant_preclamp33[33]{};
    uint32_t reserved[2]{};
};
static_assert(sizeof(PTJStockRecord) == 640 && offsetof(PTJStockRecord, raw33) == 96 &&
              offsetof(PTJStockRecord, instant_preclamp33) == 500, "PSL1 ABI");
using PTJStockSink = void (*)(void*, const PTJStockRecord*);
// PSE1 carries the two expression observations, not an asserted image identity.
struct PTJEyeRecord {
    char magic[4] {'P','S','E','1'};
    uint16_t version = 1, header_bytes = 64;
    uint32_t record_bytes = 320, flags = 0; // Updated L/R at bits 0/1, valid L/R at 2/3.
    uint64_t capture_session_id = 0, service_generation = 0, sequence = 0;
    uint64_t left_source_ns = 0, right_source_ns = 0, produced_mono_ns = 0;
    float raw24[24]{}, post20[20]{}, final20[20]{};
};
static_assert(sizeof(PTJEyeRecord) == 320 && offsetof(PTJEyeRecord, raw24) == 64, "PSE1 ABI");
using PTJEyeSink = void (*)(void*, const PTJEyeRecord*);
extern "C" {
PTJ_API int ptj_submit_stock(PTJSource*, const PTJStockRecord*);
PTJ_API int ptj_submit_eye(PTJSource*, const PTJEyeRecord*);
// Main payload callback; setter drains the previous callback before returning.
#if defined(_WIN32)
__declspec(dllexport) void picoet_set_stock_sink(PTJStockSink, void*);
__declspec(dllexport) uint32_t picoet_stock_input_identity_version();
__declspec(dllexport) void picoet_set_eye_sink(PTJEyeSink, void*);
#else
__attribute__((visibility("default"))) void picoet_set_stock_sink(PTJStockSink, void*);
__attribute__((visibility("default"))) uint32_t picoet_stock_input_identity_version();
__attribute__((visibility("default"))) void picoet_set_eye_sink(PTJEyeSink, void*);
#endif
}
