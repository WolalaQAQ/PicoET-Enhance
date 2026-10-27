#include "camera_bridge.h"
#include <cstring>

namespace {
template<class T> T load(const void* p, size_t offset) {
    T value; std::memcpy(&value, static_cast<const uint8_t*>(p) + offset, sizeof(value)); return value;
}
constexpr uint32_t PREFIX_BYTES = 640000, MAX_BYTES = 2 * 1024 * 1024;
}
void CameraBridge::arm(PTJSource* owner, uint64_t owner_session, CameraSubmit function) {
    std::lock_guard<std::mutex> lock(gate);
    source = owner; session = owner_session; submit = function;
    counts.last_identity = 0; counts.last_sensor_ns = 0; counts.first_identity = 0;
    fault = false;
    accepting.store(++epoch, std::memory_order_release);
}
void CameraBridge::on_frame(uint64_t entry_epoch, const void* listener, const void* frame) {
    ++counts.callbacks;
    if (!entry_epoch) { ++counts.disarmed; return; }
    std::unique_lock<std::mutex> lock(gate, std::try_to_lock);
    if (!lock.owns_lock()) { ++counts.gate_busy; return; }
    if (accepting.load(std::memory_order_relaxed) != entry_epoch) { ++counts.stale_epoch; return; }
    if (!listener || load<uint32_t>(listener, 16) != 5) { ++counts.other_camera; return; }
    if (!frame) { ++counts.null_header; return; }
    // One immutable vendor header from this callback. Header byte zero is not a camera ID here.
    const uint64_t sensor = load<uint64_t>(frame, 24);
    const uint64_t vendor = load<uint64_t>(frame, 32);
    const uint64_t identity = load<uint64_t>(frame, 48);
    const uint32_t bytes = load<uint32_t>(frame, 60);
    const uint8_t* pixels = load<const uint8_t*>(frame, 96);
    counts.last_byte_count = bytes;
    if (!pixels) { ++counts.null_pixels; return; }
    if (bytes < PREFIX_BYTES || bytes > MAX_BYTES) { ++counts.bad_length; return; }
    if (load<uint32_t>(frame, 16) != 36) { ++counts.bad_format; return; }
    if (!identity) { ++counts.missing_identity; return; }
    if (!sensor) { ++counts.missing_sensor; return; }
    const uint64_t previous = counts.last_identity.load(std::memory_order_relaxed);
    const uint64_t previous_sensor = counts.last_sensor_ns.load(std::memory_order_relaxed);
    if (identity == previous) { ++counts.duplicate; return; }
    if (identity < previous) {
        ++counts.backwards; fault = true; accepting.store(0, std::memory_order_release); return;
    }
    if (previous_sensor && sensor == previous_sensor) { ++counts.sensor_duplicate; return; }
    if (previous_sensor && sensor < previous_sensor) {
        ++counts.sensor_backwards; fault = true; accepting.store(0, std::memory_order_release); return;
    }
    if (previous && identity - previous > 1) {
        ++counts.identity_gaps; counts.identity_missing += identity - previous - 1;
    }
    if (!previous) counts.first_identity = identity;
    counts.last_identity = identity; counts.last_sensor_ns = sensor; counts.last_vendor_ns = vendor;
    PTJMeta meta{};
    meta.session_id = session; meta.source_seq = identity; meta.raw_identity = identity;
    meta.sensor_ns = sensor; meta.vendor_ns = vendor;
    // This camera5 composite exposes ONE parent sensor timestamp, not separate eye exposures.
    meta.eye0_sensor_ns = sensor; meta.eye1_sensor_ns = sensor; meta.mouth_sensor_ns = sensor;
    PTJSource* target = source;
    CameraSubmit function = submit;
    ++flight;
    lock.unlock();
    // Lane2 == rows 2,6,...1598 of 400-byte rows. Native owns its synchronous copy.
    const int result = function(target, pixels + 800, 1600, &meta);
    ++counts.submitted;
    switch (result) {
        case PTJ_ACCEPTED: ++counts.accepted; break;
        case PTJ_NO_SUBSCRIBER: ++counts.no_subscriber; break;
        case PTJ_DUPLICATE: ++counts.native_duplicate; break;
        case PTJ_BUSY: ++counts.native_busy; break;
        case PTJ_FULL: ++counts.native_full; break;
        case PTJ_STOPPED: ++counts.native_stopped; break;
        default: ++counts.native_error; break;
    }
    // The control thread NEVER holds this lock across hook, dlopen, network, stop or destroy.
    // Predicate update under the same mutex prevents lost wakeups in detach_and_drain.
    lock.lock(); --flight; lock.unlock(); drained.notify_all();
}
void CameraBridge::detach_and_drain() {
    accepting.store(0, std::memory_order_release);
    std::unique_lock<std::mutex> lock(gate);
    drained.wait(lock, [&] { return flight.load(std::memory_order_relaxed) == 0; });
    source = nullptr; submit = nullptr; session = 0;
}
CameraStats CameraBridge::snapshot() const {
    CameraStats s{};
    s.armed = accepting.load(std::memory_order_acquire) != 0;
    s.epoch = accepting.load(std::memory_order_acquire);
    s.in_flight = flight.load(std::memory_order_relaxed);
    s.identity_fault = fault.load(std::memory_order_acquire);
#define CAMERA_LOAD(n) s.n = counts.n.load(std::memory_order_relaxed);
    CAMERA_COUNTERS(CAMERA_LOAD)
#undef CAMERA_LOAD
    return s;
}
