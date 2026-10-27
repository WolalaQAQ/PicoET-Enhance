#include "channel.h"
#include "platform.h"
#include "core_api.h"
#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <new>
#include <sstream>
using namespace ipc;
namespace {
CoreBorrowStats borrowed;
uint64_t last_slot_address = 0, last_slot_offset = 0;
volatile std::sig_atomic_t interrupted = 0;
void signal_stop(int) { interrupted = 1; }
void failure(Channel& c, uint32_t stage, int error) {
    c.error_stage = stage; c.error_code = uint32_t(error);
    c.subscription.store(0, std::memory_order_release);
    c.state.store(Failed, std::memory_order_release);
}
void release_slot(Channel& c, int index) {
    --c.in_use; c.slots[index].state.store(Free, std::memory_order_release);
}
struct Request {
    uint64_t pid = 0, birth = 0, session = 0, nonce[2]{}, root_echo[2]{};
    explicit Request(const Channel& c) : pid(c.client_pid), birth(c.client_birth), session(c.request_session) {
        for (unsigned i = 0; i < 2; ++i) { nonce[i] = c.client_nonce[i]; root_echo[i] = c.client_root_echo[i]; }
    }
    Request() = default;
    bool matches(const Channel& c) const {
        return c.client_pid == pid && c.client_birth == birth && c.request_session == session &&
               c.client_nonce[0] == nonce[0] && c.client_nonce[1] == nonce[1] &&
               c.client_root_echo[0] == root_echo[0] && c.client_root_echo[1] == root_echo[1];
    }
};
bool validate_request(Channel& c, const Request& r) {
    if (r.session != c.session || r.root_echo[0] != c.daemon_nonce[0] || r.root_echo[1] != c.daemon_nonce[1]) {
        failure(c, OwnerEcho, EPROTO); return false;
    }
    if (!(r.nonce[0] || r.nonce[1])) { failure(c, AttachNonce, EPROTO); return false; }
    int error = 0;
    if (!r.pid || !r.birth || birth(r.pid, error) != r.birth) {
        failure(c, ClientIdentity, error ? error : ESRCH); return false;
    }
    if (!r.matches(c)) { failure(c, OwnerEcho, EPROTO); return false; }
    return true;
}
void attach_ack(Channel& c, const Request& r) {
    c.attach_ack_session = c.session;
    for (unsigned i = 0; i < 2; ++i) {
        c.attach_client_nonce[i] = r.nonce[i]; c.attach_root_nonce[i] = c.daemon_nonce[i];
    }
    c.attach_ack.store(1, std::memory_order_release);
}
std::string status_json(const Channel& c) {
    std::ostringstream j;
    j << "{\"schema_version\":2,\"state\":" << c.state.load() << ",\"session\":" << c.session
      << ",\"daemon_pid\":" << c.daemon_pid << ",\"daemon_birth\":" << c.daemon_birth
      << ",\"heartbeat_ns\":" << c.daemon_heartbeat.load() << ",\"client_pid\":" << c.client_pid.load()
      << ",\"client_birth\":" << c.client_birth.load() << ",\"client_heartbeat_ns\":" << c.client_heartbeat.load()
      << ",\"subscription\":" << c.subscription.load() << ",\"stop_ack\":" << c.stop_ack.load()
      << ",\"producer_lock\":" << c.producer_lock.load() << ",\"client_ready\":" << c.client_ready.load()
      << ",\"request_session\":" << c.request_session.load() << ",\"attach_ack\":" << c.attach_ack.load()
      << ",\"attach_ack_session\":" << c.attach_ack_session.load() << ",\"stop_ack_session\":" << c.stop_ack_session.load()
      << ",\"root_nonce\":[" << c.daemon_nonce[0] << ',' << c.daemon_nonce[1]
      << "],\"client_nonce\":[" << c.client_nonce[0].load() << ',' << c.client_nonce[1].load()
      << "],\"attach_client_nonce\":[" << c.attach_client_nonce[0].load() << ',' << c.attach_client_nonce[1].load()
      << "],\"attach_root_nonce\":[" << c.attach_root_nonce[0].load() << ',' << c.attach_root_nonce[1].load()
      << "],\"stop_client_nonce\":[" << c.stop_client_nonce[0].load() << ',' << c.stop_client_nonce[1].load() << ']'
      << ",\"stage\":\"" << stage_name(c.error_stage.load()) << "\",\"errno\":" << c.error_code.load()
      << ",\"in_use\":" << c.in_use.load() << ",\"peak\":" << c.peak.load()
      << ",\"pending\":" << c.pending.load() << ",\"slot_states\":[" << c.slots[0].state.load()
      << ',' << c.slots[1].state.load() << "],\"dispatched\":" << c.dispatched.load()
      << ",\"completed\":" << c.completed.load() << ",\"cancel_disconnect\":" << c.cancel_disconnect.load()
      << ",\"cancel_stop\":" << c.cancel_stop.load() << ",\"dispatch_dropped\":" << c.dispatch_dropped.load()
      << ",\"last_capture_copy_ns\":" << c.last_capture_copy_ns.load()
      << ",\"last_dispatch_ns\":" << c.last_dispatch_ns.load()
      << ",\"borrowed\":{\"admissions\":" << borrowed.admissions << ",\"completions\":" << borrowed.completions
      << ",\"active\":" << borrowed.active << ",\"last_admitted_address\":" << borrowed.last_admitted_address
      << ",\"last_completed_address\":" << borrowed.last_completed_address
      << ",\"last_admitted_seq\":" << borrowed.last_admitted_seq << ",\"last_completed_seq\":" << borrowed.last_completed_seq
      << ",\"last_subscription\":" << borrowed.last_subscription << ",\"last_slot_address\":" << last_slot_address
      << ",\"last_slot_offset\":" << last_slot_offset << "},\"producer\":{";
    PTJStats producer = load_stats(c.producer), core = load_stats(c.core);
    bool comma = false;
#define P(n) j << (comma ? "," : "") << "\"" #n "\":" << producer.n; comma = true;
    PTJ_COUNTER_FIELDS(P)
#undef P
    j << "},\"core\":{\"queue_in_use\":" << core.queue_in_use << ",\"connected\":" << core.connected;
#define P(n) j << ",\"" #n "\":" << core.n;
    PTJ_COUNTER_FIELDS(P)
#undef P
    j << "}}\n"; return j.str();
}
void status(const std::string& root, Channel& c) {
    int error = 0;
    if (!publish(root, "ipc-daemon-status.json", status_json(c), error) && !c.error_stage) {
        failure(c, StatusWrite, error);
    }
}
uint32_t core_failure_stage() {
    const char* name = core_start_stage();
    if (!std::strcmp(name, "socket")) return CoreSocket;
    if (!std::strcmp(name, "setsockopt")) return CoreSetopt;
    if (!std::strcmp(name, "bind")) return CoreBind;
    if (!std::strcmp(name, "listen")) return CoreListen;
    return CoreThreads;
}
}
int main(int argc, char** argv) {
    if (argc < 2 || argc > 3) { std::fprintf(stderr, "Usage: ptj_ipc_daemon <private-root0700> [port=45681]\n"); return 2; }
    const std::string root = argv[1];
    int error = private_directory(root);
    if (error) { std::fprintf(stderr, "FAIL stage=private_directory errno=%d\n", error); return 1; }
    Mapping mapping;
    error = map_file(mapping, root + "/video-channel.bin", sizeof(Channel), true);
    if (error) { std::fprintf(stderr, "FAIL stage=channel_create errno=%d operation=%s\n", error, map_operation()); return 1; }
    Channel& c = *new(mapping.address) Channel;
    std::memcpy(c.magic, "PTJIPC2", 8);
    c.daemon_pid = pid(); c.daemon_birth = self_birth(error);
    if (!c.daemon_birth) failure(c, SelfIdentity, error ? error : ESRCH);
    else if ((error = random_nonce(c.daemon_nonce))) failure(c, NonceRandom, error);
    c.session = c.daemon_nonce[0] ? c.daemon_nonce[0] : c.daemon_nonce[1];
    c.daemon_heartbeat = mono_ns();
    c.producer.session_id = c.session; c.producer.running = 1;
    PTJConfig config{}; config.struct_bytes = sizeof(config); config.session_id = c.session;
    config.port = argc == 3 ? uint16_t(std::atoi(argv[2])) : 45681;
    PTJSource* core = nullptr;
    if (c.state == Failed || core_ptj_start(&config, &core)) {
        if (c.state != Failed) failure(c, core_failure_stage(), core_start_errno());
        status(root, c);
        std::fprintf(stderr, "FAIL stage=%s errno=%u\n", stage_name(c.error_stage), c.error_code.load());
        unmap_file(mapping); return 1;
    }
    std::signal(SIGINT, signal_stop); std::signal(SIGTERM, signal_stop);
    c.state.store(Ready, std::memory_order_release);
    std::printf("READY session=%llu pid=%llu birth=%llu channel_bytes=%zu port=%u\n",
                (unsigned long long)c.session, (unsigned long long)c.daemon_pid,
                (unsigned long long)c.daemon_birth, sizeof(Channel), unsigned(config.port));
    std::fflush(stdout); status(root, c);
    uint64_t last_status = 0, last_identity_check = 0;
    Request request;
    bool registered = false, validated = false;
    int current = -1;
    for (;;) {
        const uint64_t now = mono_ns();
        c.daemon_heartbeat.store(now, std::memory_order_release);
        PTJStats stats{}; core_ptj_get_stats(core, &stats); store_stats(c.core, stats);
        core_ptj_get_borrow_stats(core, &borrowed);
        if (!registered && c.client_ready.load(std::memory_order_acquire)) {
            request = Request(c); registered = true;
            if (!validate_request(c, request)) break;
            validated = true; last_identity_check = now;
            if (!c.stop_request && !interrupted) attach_ack(c, request);
        }
        if (registered && !request.matches(c)) { failure(c, OwnerEcho, EPROTO); break; }
        const bool stopping = c.stop_request || interrupted || c.state == Failed;
        c.subscription.store(!stopping && validated && stats.connected ? stats.connections : 0, std::memory_order_release);
        if (stopping) break;
        if (validated && now - last_identity_check >= 100000000ULL) {
            last_identity_check = now;
            int e = 0;
            if (birth(request.pid, e) != request.birth) {
                failure(c, ClientIdentity, e ? e : ESRCH); break;
            }
            const uint64_t beat = c.client_heartbeat.load(), checked_now = mono_ns();
            if (!beat || checked_now < beat || checked_now - beat > CLIENT_HEARTBEAT_NS) {
                failure(c, ClientHeartbeat, ETIMEDOUT); break;
            }
        }
        if (current >= 0 && stats.queue_in_use == 0) {
            ++c.completed; release_slot(c, current); current = -1;
        }
        if (current < 0) {
            uint32_t pending = c.pending.load(std::memory_order_acquire);
            if (pending == 1 || pending == 2) {
                if (c.pending.compare_exchange_strong(pending, 0, std::memory_order_acq_rel)) {
                    const int index = int(pending - 1); Slot& slot = c.slots[index];
                    uint32_t expected = Pending;
                    if (!slot.state.compare_exchange_strong(expected, Processing, std::memory_order_acq_rel)) {
                        failure(c, CoreSubmit, EPROTO); break;
                    }
                    const uint64_t subscription = c.subscription.load(std::memory_order_acquire);
                    if (!subscription || subscription != slot.subscription) {
                        ++c.cancel_disconnect; release_slot(c, index);
                    } else {
                        c.last_capture_copy_ns = slot.copy_mono_ns; c.last_dispatch_ns = mono_ns();
                        int result = core_ptj_submit_borrowed(core, slot.pixels, &slot.meta,
                                                              slot.copy_mono_ns, slot.subscription);
                        if (result == PTJ_ACCEPTED) {
                            ++c.dispatched; current = index;
                            last_slot_address = uint64_t(reinterpret_cast<uintptr_t>(slot.pixels));
                            last_slot_offset = last_slot_address - uint64_t(reinterpret_cast<uintptr_t>(&c));
                        }
                        else {
                            if (result == PTJ_NO_SUBSCRIBER) ++c.cancel_disconnect;
                            else ++c.dispatch_dropped;
                            release_slot(c, index);
                        }
                    }
                }
            }
        }
        if (now - last_status >= 100000000ULL) { status(root, c); last_status = now; }
        pause_us(250);
    }
    c.subscription.store(0, std::memory_order_release);
    const bool failed = c.state == Failed;
    c.state.store(Stopping, std::memory_order_release);
    // Native join precedes freeing the IPC CURRENT slot and every stop ACK.
    core_ptj_stop(core);
    PTJStats final{}; core_ptj_get_stats(core, &final); store_stats(c.core, final);
    core_ptj_get_borrow_stats(core, &borrowed);
    core_ptj_destroy(core);
    if (current >= 0) { ++c.completed; release_slot(c, current); current = -1; }
    const uint64_t drain_deadline = mono_ns() + 500000000ULL;
    while (c.producer_lock && mono_ns() < drain_deadline) {
        c.daemon_heartbeat = mono_ns(); pause_us(1000);
    }
    if (c.producer_lock) { c.error_stage = LocalCalls; c.error_code = EBUSY; }
    else {
        uint32_t p = c.pending.exchange(0, std::memory_order_acq_rel);
        if (p == 1 || p == 2) { ++c.cancel_stop; release_slot(c, int(p - 1)); }
    }
    c.producer.running = 0;
    c.state.store(failed || c.error_stage ? Failed : Stopped, std::memory_order_release);
    c.daemon_heartbeat = mono_ns();
    c.stop_ack_session = c.session;
    for (unsigned i = 0; i < 2; ++i) c.stop_client_nonce[i] = request.nonce[i];
    c.stop_ack.store(1, std::memory_order_release);
    status(root, c);
    std::printf("STOPPED session=%llu ack=1 state=%u stage=%s errno=%u\n",
                (unsigned long long)c.session, c.state.load(), stage_name(c.error_stage), c.error_code.load());
    const bool success = c.state == Stopped;
    unmap_file(mapping); return success ? 0 : 1;
}
