#pragma once
#include "stock_record.h"
#include <array>

// Caller serializes this small metadata history; no image pointers/slot leases.
class StockAssociation {
    std::array<PTJMeta, 64> history{};
    size_t next = 0;
    uint64_t session = 0, generation = 0;
public:
    enum class InputClock { Unproven, Camera5ParentSensor };
    void clear() { history = {}; next = 0; session = generation = 0; }
    void reset_context(uint64_t capture_session, uint64_t service_generation) {
        clear(); session = capture_session; generation = service_generation;
    }
    void observe(const PTJMeta& meta) { history[next++ % history.size()] = meta; }
    void invalidate_sensor(uint64_t sensor) {
        for (auto& meta : history) if (meta.mouth_sensor_ns == sensor) meta.raw_identity = 0;
    }
    void invalidate_identity(uint64_t identity) {
        for (auto& meta : history) if (meta.raw_identity == identity) meta.raw_identity = 0;
    }
    bool associate(PTJStockRecord& label, InputClock clock,
                   uint64_t input_vendor, uint64_t input_identity) const {
        label.flags &= ~uint32_t(PTJ_STOCK_ASSOCIATED);
        label.source_seq = label.mouth_sensor_ns = label.vendor_ns = label.raw_identity = 0;
        if (clock != InputClock::Camera5ParentSensor || !label.stock_input_ns || !input_identity ||
            !session || !generation || label.capture_session_id != session ||
            label.service_generation != generation) return false;
        const PTJMeta* found = nullptr;
        for (const auto& meta : history) {
            if (meta.session_id != label.capture_session_id ||
                meta.mouth_sensor_ns != label.stock_input_ns) continue;
            if (found) return false; // Ambiguous identity is not an association.
            found = &meta;
        }
        if (!found || !found->raw_identity || found->flags || found->vendor_ns != input_vendor ||
            found->raw_identity != input_identity) return false;
        label.source_seq = found->source_seq; label.mouth_sensor_ns = found->mouth_sensor_ns;
        label.vendor_ns = found->vendor_ns; label.raw_identity = found->raw_identity;
        label.flags |= PTJ_STOCK_ASSOCIATED;
        return true;
    }
};
