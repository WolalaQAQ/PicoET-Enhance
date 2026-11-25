#include "tracking_demand.h"
#include <cerrno>
#include <chrono>
#include <memory>
#include <utility>
#if defined(__ANDROID__)
#include <android/binder_ibinder.h>
#include <android/binder_parcel.h>
#include <dlfcn.h>

namespace {
void* on_create(void* args) { return args; }
void on_destroy(void*) {}
binder_status_t on_transact(AIBinder*, transaction_code_t, const AParcel*, AParcel*) {
    return STATUS_UNKNOWN_TRANSACTION;
}
struct NativeTracking {
    void* library = nullptr;
    AIBinder* binder = nullptr;
    ~NativeTracking() {
        if (binder) AIBinder_decStrong(binder);
        if (library) dlclose(library);
    }
    TrackingResult apply(bool start) {
        TrackingResult result{STATUS_BAD_VALUE, STATUS_BAD_VALUE, STATUS_BAD_VALUE};
        if (!binder) {
            // Same Android 10 service lookup and Binder ABI used by Facial Bridge.
            if (!library) library = dlopen("libbinder_ndk.so", RTLD_NOW | RTLD_LOCAL);
            using Check = AIBinder* (*)(const char*);
            auto check = library ? reinterpret_cast<Check>(dlsym(library, "AServiceManager_checkService")) : nullptr;
            if (!check) return result;
            binder = check("pxreyetrackingservice");
            static AIBinder_Class* type = AIBinder_Class_define("pvr.IEyeTrackingService",
                on_create, on_destroy, on_transact);
            if (!binder) return result;
            if (!AIBinder_associateClass(binder, type)) {
                AIBinder_decStrong(binder); binder = nullptr; return result;
            }
        }
        AParcel* request = nullptr;
        AParcel* reply = nullptr;
        result.transport = AIBinder_prepareTransaction(binder, &request);
        if (!result.transport) result.transport = AParcel_writeInt32(request, 5);
        if (!result.transport)
            result.transport = start ? AParcel_writeString(request, "12", 2) : AParcel_writeInt32(request, 12);
        if (!result.transport && start) result.transport = AParcel_writeInt32(request, 1000);
        if (!result.transport) result.transport = AIBinder_transact(binder, start ? 6 : 9, &request, &reply, 0);
        if (!result.transport) {
            result.transport = AParcel_readInt32(reply, &result.binder);
            if (!result.transport && !result.binder)
                result.transport = AParcel_readInt32(reply, &result.service);
        }
        if (result.transport < 0 && binder) {
            AIBinder_decStrong(binder);
            binder = nullptr;
        }
        if (request) AParcel_delete(request);
        if (reply) AParcel_delete(reply);
        return result;
    }
};
}
#endif

TrackingOperation tracking_operation() {
#if defined(__ANDROID__)
    auto owner = std::make_shared<NativeTracking>();
    return [owner](bool start) { return owner->apply(start); };
#else
    return [](bool) { return TrackingResult{ENOTSUP, 0, 0}; };
#endif
}
TrackingDemand::TrackingDemand(TrackingOperation action) : operation(std::move(action)) {
    worker = std::thread(&TrackingDemand::run, this);
}
void TrackingDemand::want(bool value) {
    { std::lock_guard<std::mutex> lock(mutex);
      if (desired == value) return;
      desired = value; state.wanted = value; ++revision; }
    changed.notify_one();
}
void TrackingDemand::run() {
    std::unique_lock<std::mutex> lock(mutex);
    for (;;) {
        if (!closing && (bool(state.active) != desired) && revision == observed_revision)
            changed.wait_for(lock, std::chrono::seconds(2), [&] { return closing || revision != observed_revision; });
        else
            changed.wait(lock, [&] { return closing || revision != observed_revision; });
        const bool next = !closing && desired;
        observed_revision = revision;
        if (bool(state.active) != next) {
            state.busy = 1;
            lock.unlock();
            const auto result = operation(next);
            lock.lock();
            state.result = result; ++state.transitions;
            if (result.ok()) state.active = next;
            state.busy = 0;
        }
        if (closing) break;
    }
}
void TrackingDemand::stop() {
    { std::lock_guard<std::mutex> lock(mutex); closing = true; desired = false; state.wanted = 0; }
    changed.notify_one();
    if (worker.joinable()) worker.join();
}
TrackingSnapshot TrackingDemand::snapshot() const {
    std::lock_guard<std::mutex> lock(mutex); return state;
}
