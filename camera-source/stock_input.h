#pragma once
#include "stock_record.h"
#include "../hook/src/expression/source_identity.h"

// The callback record is not wire-ready until CameraBridge has matched and
// normalized it. reserved[0] is internal and must be zero before IPC submission.
inline void prepare_stock_input_record(PTJStockRecord& record,
        const expression_independent::MouthInputIdentity& input) {
    record.vendor_ns = record.raw_identity = 0;
    record.reserved[0] = 0;
    if (!input.captured || input.sensor_ns != record.stock_input_ns) return;
    record.vendor_ns = input.vendor_ns;
    record.raw_identity = input.frame_identity;
    record.reserved[0] = PTJ_STOCK_INPUT_IDENTITY_V1;
}
