#include "stock_association.h"
#include "camera/stock_status.h"
#include "../../hook/src/expression/mouth.h"
#include <cstring>
#define EXPORT extern "C" __declspec(dllexport)

EXPORT const char* stock_capability(int video, int registered, int armed, int running, int identity) {
    return stock_capability_state(video != 0, registered != 0, armed != 0, running != 0, identity != 0);
}

EXPORT int stock_associate(const PTJMeta* metas, size_t count, PTJStockRecord* record, int proven) {
    StockAssociation history;
    history.reset_context(count ? metas[0].session_id : 0, 99);
    for (size_t i = 0; i < count; ++i) history.observe(metas[i]);
    const uint64_t vendor = record->vendor_ns, identity = record->raw_identity;
    return history.associate(*record, proven ? StockAssociation::InputClock::Camera5ParentSensor
                                            : StockAssociation::InputClock::Unproven, vendor, identity);
}
EXPORT void stock_mouth(const float* raw35, size_t count, PTJStockRecord* records, float* output52) {
    using namespace expression_independent;
    MouthState state;
    for (size_t i = 0; i < count; ++i) {
        float* output = output52 + 52 * i;
        const float* raw = raw35 + 35 * i;
        const float valid = process_mouth_step(state, raw, 1000000000ULL + i * 41666667ULL, output);
        auto& record = records[i];
        record = {};
        std::memcpy(record.raw33, raw, sizeof(record.raw33));
        std::memcpy(record.quality2, raw + 33, sizeof(record.quality2));
        std::memcpy(record.instant_preclamp33, state.instant_preclamp, sizeof(record.instant_preclamp33));
        std::memcpy(record.instant33, state.instant, sizeof(record.instant33));
        std::memcpy(record.final33, state.previous, sizeof(record.final33));
        record.flags = (valid ? PTJ_STOCK_VALID : 0) | (state.diagnostics.warmup ? PTJ_STOCK_WARMUP : 0);
        record.classification = state.diagnostics.classification;
    }
}
