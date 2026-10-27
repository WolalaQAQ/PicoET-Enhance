// Windows host fixtures only. Not part of either Android artifact.
#include "channel.h"
#include "platform.h"
#include <new>
using namespace ipc;
struct Probe {
    uint64_t bytes, session, daemon_pid, daemon_birth, heartbeat, subscription, state, pending;
    uint64_t slot0, slot1, in_use, peak, core_queue, completed, dispatched, cancel_disconnect, cancel_stop;
    uint64_t dispatch_dropped, stop_ack, capture_ns, dispatch_ns, producer_enqueued;
    uint64_t version, client_ready, attach_ack, error_stage, client_pid, client_birth, request_session;
    uint64_t attach_session, stop_session, root0, root1, nonce0, nonce1, attach0, attach1, attach_root0, attach_root1;
    uint64_t stop0, stop1;
};
void stop_ack(Channel& c) {
    c.stop_ack_session = c.session;
    for (unsigned i = 0; i < 2; ++i) c.stop_client_nonce[i] = c.client_nonce[i].load();
    c.state = Stopped; c.stop_ack.store(1, std::memory_order_release);
}
#define EXPORT extern "C" __declspec(dllexport)
EXPORT int ipc_test_probe(const char* root, Probe* out) {
    Mapping m; int error = map_file(m, std::string(root) + "/video-channel.bin", sizeof(Channel), false);
    if (error) return error;
    const Channel& c = *static_cast<const Channel*>(m.address);
    *out = {sizeof(Channel), c.session, c.daemon_pid, c.daemon_birth, c.daemon_heartbeat.load(),
        c.subscription.load(), c.state.load(), c.pending.load(), c.slots[0].state.load(), c.slots[1].state.load(),
        c.in_use.load(), c.peak.load(), c.core.queue_in_use.load(), c.completed.load(), c.dispatched.load(),
        c.cancel_disconnect.load(), c.cancel_stop.load(), c.dispatch_dropped.load(), c.stop_ack.load(),
        c.last_capture_copy_ns.load(), c.last_dispatch_ns.load(), c.producer.enqueued.load(),
        c.version, c.client_ready.load(), c.attach_ack.load(), c.error_stage.load(), c.client_pid.load(),
        c.client_birth.load(), c.request_session.load(), c.attach_ack_session.load(), c.stop_ack_session.load(),
        c.daemon_nonce[0], c.daemon_nonce[1], c.client_nonce[0].load(), c.client_nonce[1].load(),
        c.attach_client_nonce[0].load(), c.attach_client_nonce[1].load(),
        c.attach_root_nonce[0].load(), c.attach_root_nonce[1].load(), c.stop_client_nonce[0].load(), c.stop_client_nonce[1].load()};
    unmap_file(m); return 0;
}
EXPORT int ipc_test_fixture(const char* root, uint32_t action) {
    Mapping m; int error = map_file(m, std::string(root) + "/video-channel.bin", sizeof(Channel), action == 0);
    if (error) return error;
    auto* c = action == 0 ? new(m.address) Channel : static_cast<Channel*>(m.address);
    if (action == 0) {
        std::memcpy(c->magic, "PTJIPC2", 8); c->session = mono_ns();
        c->daemon_pid = pid(); c->daemon_birth = self_birth(error);
        if (!error) error = random_nonce(c->daemon_nonce);
        c->daemon_heartbeat = mono_ns(); c->state = Ready;
    } else if (action == 1) c->daemon_heartbeat = 0;
    else if (action == 2) { c->version = 1; std::memcpy(c->magic, "PTJIPC1", 8); }
    else if (action == 3) c->subscription = 123;
    else if (action == 4) c->producer_lock = 1;
    else if (action == 5) c->producer_lock = 0;
    else if (action == 6) stop_ack(*c);
    else if (action == 7) { stop_ack(*c); c->stop_client_nonce[0] ^= 1; }
    else if (action == 8 || action == 9 || action == 19 || action == 20) {
        c->client_pid = pid(); c->client_birth = self_birth(error) + (action == 8 ? 1 : 0);
        c->request_session = c->session ^ (action == 9 ? 1 : 0);
        uint64_t nonce[2]{};
        if (action != 20 && !error) error = random_nonce(nonce);
        for (unsigned i = 0; i < 2; ++i) {
            c->client_nonce[i] = nonce[i]; c->client_root_echo[i] = c->daemon_nonce[i];
        }
        if (action == 19) c->client_root_echo[0] ^= 1;
        c->client_heartbeat = mono_ns(); c->client_ready.store(1, std::memory_order_release);
    } else if ((action >= 10 && action <= 14) || action == 21 || action == 22) {
        c->daemon_heartbeat = mono_ns();
        if (c->client_ready.load(std::memory_order_acquire) && !c->attach_ack && action != 14 && action != 21) {
            c->attach_ack_session = c->session ^ (action == 13 ? 1 : 0);
            for (unsigned i = 0; i < 2; ++i) {
                c->attach_client_nonce[i] = c->client_nonce[i].load(); c->attach_root_nonce[i] = c->daemon_nonce[i];
            }
            if (action == 11) c->attach_client_nonce[0] ^= 1;
            if (action == 12) c->attach_root_nonce[0] ^= 1;
            c->attach_ack.store(1, std::memory_order_release);
        }
        if (c->stop_request && action != 21 && action != 22) stop_ack(*c);
    } else if (action == 16) c->client_nonce[0] ^= 1;
    else if (action == 17) c->request_session ^= 1;
    else if (action == 18) c->attach_client_nonce[0] ^= 1;
    unmap_file(m); return error;
}
