#pragma once
#include "core/ptj_source.h"

// One-shot CONTEXT response. Published only by root after verified IPC attach.
struct PTJCaptureContext {
    char magic[4] {'P','T','C','1'};
    uint16_t version = 1, bytes = 24;
    uint64_t capture_session_id = 0, service_generation = 0;
};
static_assert(sizeof(PTJCaptureContext) == 24 &&
              offsetof(PTJCaptureContext, capture_session_id) == 8, "PTC1 ABI");
