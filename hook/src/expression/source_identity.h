#pragma once
#include <cstdint>
#include <cstring>

namespace expression_independent {
struct MouthInputIdentity {
    uint64_t vendor_ns = 0, sensor_ns = 0, frame_identity = 0;
    bool captured = false;
};
// Existing pipeline entry, before stock releases the execution slot. These are
// the third image's fields, never the first eye's final callback prefix.
inline MouthInputIdentity capture_mouth_input_identity(const void* input) {
    MouthInputIdentity result;
    const auto* bytes = static_cast<const unsigned char*>(input);
    std::memcpy(&result.vendor_ns, bytes + 0x100, 8);
    std::memcpy(&result.sensor_ns, bytes + 0x108, 8);
    std::memcpy(&result.frame_identity, bytes + 0x110, 8);
    result.captured = true;
    return result;
}
}
