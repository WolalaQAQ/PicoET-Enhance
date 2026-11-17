#include "camera_bridge.h"
#include "guard_spec.h"
#include "control_message.h"
#include "stock_input.h"
#include "tracking_demand.h"
#define EXPORT extern "C" __declspec(dllexport)
EXPORT CameraBridge* camtest_create() { return new CameraBridge; }
EXPORT void camtest_destroy(CameraBridge* b) { b->detach_and_drain(); delete b; }
EXPORT void camtest_arm(CameraBridge* b, PTJSource* s, uint64_t session, CameraSubmit fn) { b->arm(s, session, fn); }
EXPORT void camtest_arm_streams(CameraBridge* b, PTJSource* s, uint64_t session, CameraSubmitStream fn) {
    b->arm(s, session, nullptr, nullptr, 0, false, fn);
}
using TestTracking = int (*)(uint32_t);
EXPORT TrackingDemand* demandtest_create(TestTracking fn) {
    return new TrackingDemand([fn](bool start) { return TrackingResult{fn(start), 0, 0}; });
}
EXPORT void demandtest_want(TrackingDemand* d, uint32_t wanted) { d->want(wanted != 0); }
EXPORT void demandtest_snapshot(TrackingDemand* d, TrackingSnapshot* s) { *s = d->snapshot(); }
EXPORT void demandtest_stop(TrackingDemand* d) { d->stop(); }
EXPORT void demandtest_destroy(TrackingDemand* d) { delete d; }
EXPORT void camtest_arm_stock(CameraBridge* b, PTJSource* s, uint64_t session, uint64_t generation,
                               CameraSubmit images, StockSubmit labels, int audited) {
    b->arm(s, session, images, labels, generation, audited != 0);
}
EXPORT void camtest_stock(CameraBridge* b, const PTJStockRecord* record) { b->on_stock(*record); }
EXPORT void camtest_pipeline_stock(CameraBridge* b, const void* execution_input, const PTJStockRecord* record) {
    PTJStockRecord input = *record;
    auto identity = expression_independent::capture_mouth_input_identity(execution_input);
    input.stock_input_ns = identity.sensor_ns;
    prepare_stock_input_record(input, identity);
    b->on_stock(input);
}
EXPORT uint64_t camtest_ticket(CameraBridge* b) { return b->ticket(); }
EXPORT void camtest_frame(CameraBridge* b, uint64_t epoch, const void* listener, const void* frame) {
    b->on_frame(epoch, listener, frame);
}
EXPORT void camtest_detach(CameraBridge* b) { b->detach_and_drain(); }
EXPORT void camtest_stats(CameraBridge* b, CameraStats* stats) { *stats = b->snapshot(); }
EXPORT int camtest_guard(const uint8_t* window, uint32_t patched) {
    return patched ? CameraGuard::patched(window) : CameraGuard::original(window);
}
EXPORT int camtest_command(const char* line, uint64_t owner, uint64_t previous, uint64_t* id, int* operation) {
    CameraCommand cmd{};
    int result = parse_camera_control(line, owner, previous, *id, cmd);
    *operation = cmd == CameraCommand::Start ? 1 : 2;
    return result;
}
