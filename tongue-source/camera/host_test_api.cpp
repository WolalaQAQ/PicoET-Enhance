#include "camera_bridge.h"
#include "guard_spec.h"
#include "control_message.h"
#define EXPORT extern "C" __declspec(dllexport)
EXPORT CameraBridge* camtest_create() { return new CameraBridge; }
EXPORT void camtest_destroy(CameraBridge* b) { b->detach_and_drain(); delete b; }
EXPORT void camtest_arm(CameraBridge* b, PTJSource* s, uint64_t session, CameraSubmit fn) { b->arm(s, session, fn); }
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
