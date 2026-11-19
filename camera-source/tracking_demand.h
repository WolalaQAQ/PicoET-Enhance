#pragma once
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <mutex>
#include <thread>

struct TrackingResult {
    int32_t transport = 0, binder = 0, service = 0;
    bool ok() const { return !transport && !binder && !service; }
};
struct TrackingSnapshot {
    uint64_t wanted = 0, active = 0, busy = 0, transitions = 0;
    TrackingResult result;
};
using TrackingOperation = std::function<TrackingResult(bool)>;
TrackingOperation tracking_operation();

// One service lease for the union of camera consumers. Binder cold starts never
// block the IPC heartbeat. A failed transition is retried only after demand changes.
class TrackingDemand {
    mutable std::mutex mutex;
    std::condition_variable changed;
    bool desired = false, closing = false;
    uint64_t revision = 0, observed_revision = 0;
    TrackingSnapshot state;
    TrackingOperation operation;
    std::thread worker;
    void run();
public:
    explicit TrackingDemand(TrackingOperation action = tracking_operation());
    ~TrackingDemand() { stop(); }
    void want(bool value);
    void stop();
    TrackingSnapshot snapshot() const;
};
