#pragma once
#include "ptj_source.h"
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <mutex>

using CameraSubmit = int (*)(PTJSource*, const uint8_t*, uint32_t, const PTJMeta*);
#define CAMERA_COUNTERS(X) \
    X(callbacks) X(disarmed) X(stale_epoch) X(gate_busy) X(other_camera) \
    X(null_header) X(null_pixels) X(bad_length) X(bad_format) X(missing_identity) \
    X(missing_sensor) X(duplicate) X(backwards) X(sensor_duplicate) X(sensor_backwards) \
    X(identity_gaps) X(identity_missing) X(submitted) X(accepted) X(no_subscriber) \
    X(native_duplicate) X(native_busy) X(native_full) X(native_stopped) X(native_error) \
    X(last_identity) X(last_sensor_ns) X(last_vendor_ns) X(last_byte_count) X(first_identity)
struct CameraStats {
    uint64_t armed, epoch, in_flight, identity_fault;
#define CAMERA_FIELD(n) uint64_t n;
    CAMERA_COUNTERS(CAMERA_FIELD)
#undef CAMERA_FIELD
};

class CameraBridge {
    struct Counters {
#define CAMERA_ATOMIC(n) std::atomic<uint64_t> n{0};
        CAMERA_COUNTERS(CAMERA_ATOMIC)
#undef CAMERA_ATOMIC
    } counts;
    std::atomic<uint64_t> accepting{0};
    std::atomic<uint64_t> flight{0};
    std::atomic<bool> fault{false};
    std::mutex gate;
    std::condition_variable drained;
    uint64_t epoch = 0, session = 0;
    PTJSource* source = nullptr;
    CameraSubmit submit = nullptr;
public:
    void arm(PTJSource* owner, uint64_t owner_session, CameraSubmit function);
    uint64_t ticket() const { return accepting.load(std::memory_order_acquire); }
    // All borrowed frame access and the synchronous submit finish before this returns.
    void on_frame(uint64_t entry_epoch, const void* listener, const void* frame);
    // Control thread: disarm, wait for every admitted submit, detach borrowed handle.
    void detach_and_drain();
    bool identity_fault() const { return fault.load(std::memory_order_acquire); }
    CameraStats snapshot() const;
};
