#include "channel.h"
#include "platform.h"
#include <cerrno>
#include <cstdio>
#include <memory>
#include <mutex>
#include <sstream>
using namespace ipc;
struct PTJSource {
    Mapping mapping;
    Channel* channel = nullptr;
    std::string root;
    uint64_t session = 0, daemon_pid = 0, daemon_birth = 0;
    uint64_t own_pid = 0, own_birth = 0, root_nonce[2]{}, nonce[2]{};
    std::atomic<bool> active{true}, peer_alive{true};
    std::atomic<uint64_t> calls{0};
    std::atomic<uint32_t> error_stage{None}, error_code{0};
    uint32_t cleanup_stage = None;
    int cleanup_code = 0;
    AtomicStats local;
    std::mutex stop_mutex;
    bool stopped = false;
};
namespace {
std::atomic<bool> failed_unacked{false};
struct Lease {
    PTJSource& s;
    bool valid;
    explicit Lease(PTJSource& source) : s(source) {
        ++s.calls; valid = s.active.load();
    }
    ~Lease() { --s.calls; }
};
void report(const std::string& root, const char* state, uint32_t stage, int code,
            const PTJSource* s = nullptr, bool retained = false) {
    std::ostringstream j;
    j << "{\"state\":\"" << state << "\",\"stage\":\"" << stage_name(stage)
      << "\",\"stage_id\":" << stage << ",\"errno\":" << code
      << ",\"operation\":\"" << (stage == ChannelOpen ? map_operation() : stage_name(stage)) << '"'
      << ",\"pid\":" << pid() << ",\"monotonic_ns\":" << mono_ns()
      << ",\"retained_mapping\":" << (retained ? "true" : "false");
    if (s) {
        char nonce[33], root_nonce[33];
        std::snprintf(nonce, sizeof(nonce), "%016llx%016llx", (unsigned long long)s->nonce[0], (unsigned long long)s->nonce[1]);
        std::snprintf(root_nonce, sizeof(root_nonce), "%016llx%016llx", (unsigned long long)s->root_nonce[0], (unsigned long long)s->root_nonce[1]);
        j << ",\"session\":" << s->session << ",\"daemon_pid\":" << s->daemon_pid
          << ",\"daemon_birth\":" << s->daemon_birth << ",\"client_birth\":" << s->own_birth
          << ",\"client_nonce\":\"" << nonce << "\",\"root_nonce\":\"" << root_nonce
          << "\",\"cleanup_stage\":\"" << stage_name(s->cleanup_stage) << "\",\"cleanup_errno\":" << s->cleanup_code;
    }
    j << "}\n";
    int ignored = 0; publish(root, "ipc-shim-status.json", j.str(), ignored);
}
int start_error(const std::string& root, uint32_t stage, int code, bool retained = false) {
    report(root, "start_failed", stage, code, nullptr, retained);
    return -int(stage * 100000U + uint32_t(code));
}
void peer_error(PTJSource& s, uint32_t stage, int code) {
    s.peer_alive = false; s.error_stage = stage; s.error_code = uint32_t(code);
}
bool heartbeat(const Channel& c) {
    const uint64_t beat = c.daemon_heartbeat.load(std::memory_order_acquire), now = mono_ns();
    return beat && now >= beat && now - beat <= HEARTBEAT_NS;
}
bool owner_matches(const PTJSource& s) {
    const Channel& c = *s.channel;
    return valid_header(c) && c.session == s.session && c.daemon_pid == s.daemon_pid &&
           c.daemon_birth == s.daemon_birth && c.daemon_nonce[0] == s.root_nonce[0] && c.daemon_nonce[1] == s.root_nonce[1];
}
bool request_matches(const PTJSource& s) {
    const Channel& c = *s.channel;
    return c.client_pid == s.own_pid && c.client_birth == s.own_birth && c.request_session == s.session &&
           c.client_nonce[0] == s.nonce[0] && c.client_nonce[1] == s.nonce[1] &&
           c.client_root_echo[0] == s.root_nonce[0] && c.client_root_echo[1] == s.root_nonce[1];
}
bool attach_matches(const PTJSource& s) {
    const Channel& c = *s.channel;
    return c.attach_ack.load(std::memory_order_acquire) == 1 && c.attach_ack_session == s.session &&
           c.attach_client_nonce[0] == s.nonce[0] && c.attach_client_nonce[1] == s.nonce[1] &&
           c.attach_root_nonce[0] == s.root_nonce[0] && c.attach_root_nonce[1] == s.root_nonce[1];
}
bool wait_stop_ack(PTJSource& s, uint64_t deadline, uint32_t& stage, int& error) {
    Channel& c = *s.channel;
    while (mono_ns() < deadline) {
        if (!owner_matches(s) || !request_matches(s)) { stage = OwnerEcho; error = EPROTO; return false; }
        if (c.stop_ack.load(std::memory_order_acquire)) {
            if (c.stop_ack_session != s.session || c.stop_client_nonce[0] != s.nonce[0] ||
                c.stop_client_nonce[1] != s.nonce[1] || (c.state != Stopped && c.state != Failed)) {
                stage = StopAckNonce; error = EPROTO; return false;
            }
            stage = c.error_stage; error = int(c.error_code.load()); return true;
        }
        c.client_heartbeat = mono_ns(); pause_us(1000);
    }
    stage = StopAck; error = ETIMEDOUT; return false;
}
bool peer(PTJSource& s) {
    Channel& c = *s.channel;
    if (!s.peer_alive) return false;
    if (!owner_matches(s) || !request_matches(s)) { peer_error(s, OwnerEcho, EPROTO); return false; }
    if (!attach_matches(s)) { peer_error(s, AttachNonce, EPROTO); return false; }
    if (!heartbeat(c)) { peer_error(s, Heartbeat, ETIMEDOUT); return false; }
    return true;
}
void transport_snapshot(PTJSource& s) {
    const Channel& c = *s.channel;
#define TRANSPORT(n) s.local.n = c.core.n.load(std::memory_order_relaxed)
    TRANSPORT(encoded); TRANSPORT(encode_errors); TRANSPORT(sent_sources); TRANSPORT(sent_packets);
    TRANSPORT(sent_payload_bytes); TRANSPORT(send_errors); TRANSPORT(send_timeouts);
    TRANSPORT(connections); TRANSPORT(pings); TRANSPORT(encode_total_ns); TRANSPORT(encode_max_ns);
    TRANSPORT(last_sent_seq);
#undef TRANSPORT
    s.local.cancel_disconnect = c.cancel_disconnect.load() + c.core.cancel_disconnect.load();
    s.local.cancel_stop = c.cancel_stop.load() + c.core.cancel_stop.load();
}
}
extern "C" int ptj_start(const PTJConfig* config, PTJSource** out) {
    if (!out) return -1;
    *out = nullptr;
    const std::string root = module_directory(reinterpret_cast<const void*>(ptj_start));
    if (config && config->struct_bytes != sizeof(PTJConfig)) return start_error(root, Header, EINVAL);
    if (failed_unacked) return start_error(root, StopAck, ESHUTDOWN, true);
    int error = private_directory(root);
    if (error) return start_error(root, Directory, error);
    auto s = std::make_unique<PTJSource>();
    s->root = root;
    error = map_file(s->mapping, root + "/video-channel.bin", sizeof(Channel), false);
    if (error) return start_error(root, ChannelOpen, error);
    s->channel = static_cast<Channel*>(s->mapping.address);
    Channel& c = *s->channel;
    auto fail = [&](uint32_t stage, int code) {
        unmap_file(s->mapping); return start_error(root, stage, code);
    };
    if (c.state.load(std::memory_order_acquire) != Ready || !valid_header(c) || c.stop_request || c.stop_ack)
        return fail(Header, ESHUTDOWN);
    if (!heartbeat(c)) return fail(Heartbeat, ETIMEDOUT);
    if (config && config->session_id && config->session_id != c.session) return fail(Header, EINVAL);
    s->session = c.session; s->daemon_pid = c.daemon_pid; s->daemon_birth = c.daemon_birth;
    s->root_nonce[0] = c.daemon_nonce[0]; s->root_nonce[1] = c.daemon_nonce[1];
    int identity_error = 0;
    s->own_pid = pid(); s->own_birth = self_birth(identity_error);
    if (!s->own_birth) return fail(SelfIdentity, identity_error ? identity_error : ESRCH);
    int random_error = random_nonce(s->nonce);
    if (random_error) return fail(NonceRandom, random_error);
    uint64_t expected = 0;
    if (!c.client_pid.compare_exchange_strong(expected, pid(), std::memory_order_acq_rel))
        return fail(Claim, EALREADY);
    c.client_birth = s->own_birth; c.request_session = s->session;
    for (unsigned i = 0; i < 2; ++i) { c.client_nonce[i] = s->nonce[i]; c.client_root_echo[i] = s->root_nonce[i]; }
    c.client_heartbeat = mono_ns();
    c.client_ready.store(1, std::memory_order_release);
    auto registered_fail = [&](uint32_t stage, int code) {
        s->active = false; c.stop_request.store(1, std::memory_order_release);
        bool ack = wait_stop_ack(*s, mono_ns() + 2000000000ULL, s->cleanup_stage, s->cleanup_code);
        if (!ack) failed_unacked = true;
        report(root, "attach_failed", stage, code, s.get(), !ack);
        if (ack) unmap_file(s->mapping); // Otherwise this sole retained mapping lasts until service recovery.
        return -int(stage * 100000U + uint32_t(code));
    };
    const uint64_t attach_deadline = mono_ns() + 2000000000ULL;
    bool attached = false;
    while (mono_ns() < attach_deadline) {
        if (!owner_matches(*s) || !request_matches(*s)) return registered_fail(OwnerEcho, EPROTO);
        if (c.state != Ready) return registered_fail(c.error_stage ? c.error_stage.load() : Header,
                                                     c.error_code ? int(c.error_code.load()) : ESHUTDOWN);
        if (!heartbeat(c)) return registered_fail(Heartbeat, ETIMEDOUT);
        if (c.attach_ack.load(std::memory_order_acquire)) {
            if (!attach_matches(*s)) return registered_fail(AttachNonce, EPROTO);
            attached = true; break;
        }
        c.client_heartbeat = mono_ns(); pause_us(1000);
    }
    if (!attached) return registered_fail(AttachAck, ETIMEDOUT);
    s->local.session_id = c.session; s->local.running = 1;
    report(root, "attached", None, 0, s.get());
    *out = s.release(); return 0;
}
extern "C" uint64_t ptj_session_id(const PTJSource* s) { return s ? s->session : 0; }
extern "C" int ptj_submit(PTJSource* source, const uint8_t* pixels, uint32_t stride, const PTJMeta* meta) {
    if (!source) return PTJ_INVALID;
    PTJSource& s = *source;
    ++s.local.submit_calls;
    Lease lease(s);
    if (!lease.valid) { ++s.local.drop_stopped; return PTJ_STOPPED; }
    Channel& c = *s.channel; ++c.producer.submit_calls;
#define DROP(counter, result) do { ++s.local.counter; ++c.producer.counter; return result; } while (0)
    if (!pixels || !meta || stride < PTJ_WIDTH || (meta->flags & ~uint32_t(PTJ_REPLAY))) DROP(drop_invalid, PTJ_INVALID);
    if (!peer(s) || c.state.load(std::memory_order_acquire) != Ready || c.stop_request) {
        ++c.peer_rejected; DROP(drop_stopped, PTJ_STOPPED);
    }
    c.client_heartbeat = mono_ns();
    if (meta->session_id != s.session) DROP(drop_session, PTJ_WRONG_SESSION);
    uint32_t unlocked = 0;
    if (!c.producer_lock.compare_exchange_strong(unlocked, 1, std::memory_order_acquire)) DROP(drop_busy, PTJ_BUSY);
    struct Unlock { Channel& c; ~Unlock() { c.producer_lock.store(0, std::memory_order_release); } } unlock{c};
    if (c.has_seq && meta->source_seq <= c.last_seen_seq.load()) DROP(drop_duplicate, PTJ_DUPLICATE);
    c.last_seen_seq = meta->source_seq; c.has_seq = 1;
    const uint64_t subscription = c.subscription.load(std::memory_order_acquire);
    if (!subscription) DROP(drop_no_subscriber, PTJ_NO_SUBSCRIBER);
    uint32_t none = 0;
    if (!c.pending.compare_exchange_strong(none, 3, std::memory_order_acq_rel)) DROP(drop_full, PTJ_FULL);
    int chosen = -1;
    for (int i = 0; i < 2; ++i) {
        uint32_t free = Free;
        if (c.slots[i].state.compare_exchange_strong(free, Writing, std::memory_order_acq_rel)) { chosen = i; break; }
    }
    if (chosen < 0) { c.pending.store(0, std::memory_order_release); DROP(drop_full, PTJ_FULL); }
    Slot& slot = c.slots[chosen];
    const uint64_t used = c.in_use.fetch_add(1) + 1;
    maximum(c.peak, used); maximum(s.local.queue_peak, used); maximum(c.producer.queue_peak, used);
    const uint64_t begin = mono_ns();
    if (stride == PTJ_WIDTH) std::memcpy(slot.pixels, pixels, PTJ_PIXELS);
    else for (size_t row = 0; row < PTJ_HEIGHT; ++row)
        std::memcpy(slot.pixels + row * PTJ_WIDTH, pixels + row * stride, PTJ_WIDTH);
    slot.meta = *meta; slot.subscription = subscription;
    slot.copy_mono_ns = mono_ns(); // CAMERA-domain completed ownership-protected copy, before IPC publish.
    uint64_t elapsed = slot.copy_mono_ns - begin;
    ++s.local.copy_count; ++c.producer.copy_count;
    s.local.copy_total_ns += elapsed; c.producer.copy_total_ns += elapsed;
    s.local.copy_last_ns = elapsed; c.producer.copy_last_ns = elapsed;
    maximum(s.local.copy_max_ns, elapsed); maximum(c.producer.copy_max_ns, elapsed);
    c.last_capture_copy_ns = slot.copy_mono_ns;
    if (!peer(s) || c.state.load(std::memory_order_acquire) != Ready || c.stop_request) {
        --c.in_use; slot.state.store(Free, std::memory_order_release);
        c.pending.store(0, std::memory_order_release); DROP(drop_stopped, PTJ_STOPPED);
    }
    ++s.local.enqueued; ++c.producer.enqueued;
    slot.state.store(Pending, std::memory_order_release);
    c.pending.store(uint32_t(chosen + 1), std::memory_order_release);
    return PTJ_ACCEPTED;
#undef DROP
}
extern "C" int ptj_get_stats(const PTJSource* source, PTJStats* out) {
    if (!source || !out) return -1;
    PTJSource& s = *const_cast<PTJSource*>(source);
    Lease lease(s);
    if (lease.valid) {
        Channel& c = *s.channel;
        if (c.state == Failed && c.error_stage) peer_error(s, c.error_stage, int(c.error_code.load()));
        const bool alive = peer(s);
        c.client_heartbeat = mono_ns(); transport_snapshot(s);
        s.local.running = alive && c.state == Ready && !c.stop_request;
        s.local.connected = s.local.running && c.subscription.load(std::memory_order_acquire) != 0;
        s.local.queue_in_use = c.in_use.load();
        report(s.root, alive ? "attached" : "peer_failed", s.error_stage, int(s.error_code), &s);
    }
    *out = load_stats(s.local); return 0;
}
extern "C" void ptj_stop(PTJSource* source) {
    if (!source) return;
    PTJSource& s = *source;
    std::lock_guard<std::mutex> lock(s.stop_mutex);
    if (s.stopped) return;
    s.active = false;
    const uint64_t deadline = mono_ns() + 2000000000ULL;
    while (s.calls && mono_ns() < deadline) pause_us(1000);
    bool retained = false; uint32_t stage = None; int error = 0;
    Channel& c = *s.channel;
    if (s.calls) { stage = LocalCalls; error = ETIMEDOUT; retained = true; }
    else {
        c.stop_request.store(1, std::memory_order_release);
        retained = !wait_stop_ack(s, deadline, stage, error);
        transport_snapshot(s);
        s.local.queue_in_use = c.in_use.load();
    }
    s.local.running = 0; s.local.connected = 0; s.peer_alive = false;
    s.error_stage = stage; s.error_code = uint32_t(error);
    report(s.root, stage ? "stop_failed" : "stopped_acknowledged", stage, error, &s, retained);
    if (!retained) unmap_file(s.mapping);
    else failed_unacked = true; // A live unacknowledged peer requires manual service recovery, not reattach.
    s.stopped = true;
}
extern "C" void ptj_destroy(PTJSource* s) { if (s) { ptj_stop(s); delete s; } }
