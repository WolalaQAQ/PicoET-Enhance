#pragma once
#include "ptj_source.h"
#include "errno_compat.h"
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
namespace ipc {
static_assert(std::atomic<uint32_t>::is_always_lock_free && std::atomic<uint64_t>::is_always_lock_free,
              "Cross-process channel requires hardware lock-free atomics");
static_assert(sizeof(std::atomic<uint32_t>) == 4 && sizeof(std::atomic<uint64_t>) == 8, "Atomic ABI");
constexpr uint64_t HEARTBEAT_NS = 500000000ULL;
constexpr uint64_t CLIENT_HEARTBEAT_NS = 2000000000ULL;
enum State : uint32_t { Initializing, Ready, Stopping, Stopped, Failed };
enum SlotState : uint32_t { Free, Writing, Pending, Processing };
enum Stage : uint32_t { None, Directory, ChannelCreate, ChannelOpen, Header, DaemonIdentity,
    Heartbeat, Claim, CoreSocket, CoreSetopt, CoreBind, CoreListen, CoreThreads,
    StopAck, ClientIdentity, ClientHeartbeat, StatusWrite, CoreSubmit, LocalCalls,
    SelfIdentity, NonceRandom, AttachAck, AttachNonce, OwnerEcho, StopAckNonce };
inline const char* stage_name(uint32_t stage) {
    static const char* names[] = {"none", "private_directory", "channel_create", "channel_open_map",
        "channel_header", "daemon_pid_birth", "daemon_heartbeat", "client_claim", "core_socket",
        "core_setsockopt", "core_bind", "core_listen", "core_threads", "stop_ack", "client_pid_birth",
        "client_heartbeat", "status_write", "core_submit", "local_inflight", "self_pid_birth",
        "nonce_random", "attach_ack", "attach_nonce", "owner_echo", "stop_ack_nonce"};
    return stage < sizeof(names) / sizeof(names[0]) ? names[stage] : "unknown";
}
struct AtomicStats {
    std::atomic<uint64_t> struct_bytes{sizeof(PTJStats)}, session_id{0}, running{0}, connected{0}, queue_in_use{0};
#define A(n) std::atomic<uint64_t> n{0};
    PTJ_COUNTER_FIELDS(A)
#undef A
};
inline PTJStats load_stats(const AtomicStats& a) {
    PTJStats s{};
    s.struct_bytes = a.struct_bytes; s.session_id = a.session_id; s.running = a.running;
    s.connected = a.connected; s.queue_in_use = a.queue_in_use;
#define A(n) s.n = a.n.load(std::memory_order_relaxed);
    PTJ_COUNTER_FIELDS(A)
#undef A
    return s;
}
inline void store_stats(AtomicStats& a, const PTJStats& s) {
    a.struct_bytes = s.struct_bytes; a.session_id = s.session_id; a.running = s.running;
    a.connected = s.connected; a.queue_in_use = s.queue_in_use;
#define A(n) a.n.store(s.n, std::memory_order_relaxed);
    PTJ_COUNTER_FIELDS(A)
#undef A
}
inline void maximum(std::atomic<uint64_t>& value, uint64_t n) {
    uint64_t previous = value.load(std::memory_order_relaxed);
    while (previous < n && !value.compare_exchange_weak(previous, n, std::memory_order_relaxed)) {}
}
struct alignas(64) Slot {
    std::atomic<uint32_t> state{Free};
    uint32_t reserved = 0;
    PTJMeta meta{};
    uint64_t copy_mono_ns = 0, subscription = 0;
    uint8_t pixels[PTJ_PIXELS]{};
};
struct alignas(64) Channel {
    char magic[8]{};
    uint32_t version = 2, bytes = sizeof(Channel);
    uint64_t session = 0, daemon_pid = 0, daemon_birth = 0;
    uint64_t daemon_nonce[2]{}; // Immutable root challenge, published with READY.
    alignas(64) std::atomic<uint32_t> state{Initializing}, stop_request{0}, stop_ack{0}, producer_lock{0};
    std::atomic<uint32_t> pending{0}; // 0=none, 1/2=slot+1, 3=producer's reserved pending position.
    std::atomic<uint32_t> client_ready{0};
    std::atomic<uint64_t> daemon_heartbeat{0}, subscription{0}, client_pid{0}, client_birth{0}, client_heartbeat{0};
    std::atomic<uint64_t> request_session{0}, client_nonce[2]{}, client_root_echo[2]{};
    std::atomic<uint32_t> attach_ack{0};
    std::atomic<uint64_t> attach_ack_session{0}, attach_client_nonce[2]{}, attach_root_nonce[2]{};
    std::atomic<uint64_t> stop_ack_session{0}, stop_client_nonce[2]{};
    std::atomic<uint32_t> error_stage{None}, error_code{0};
    std::atomic<uint64_t> in_use{0}, peak{0}, last_seen_seq{0}, has_seq{0};
    std::atomic<uint64_t> dispatched{0}, completed{0}, cancel_disconnect{0}, cancel_stop{0}, dispatch_dropped{0};
    std::atomic<uint64_t> peer_rejected{0}, last_capture_copy_ns{0}, last_dispatch_ns{0};
    alignas(64) AtomicStats producer, core;
    Slot slots[2];
};
static_assert(alignof(Channel) == 64 && alignof(Slot) == 64, "Channel alignment");
inline bool valid_header(const Channel& c) {
    return !std::memcmp(c.magic, "PTJIPC2", 8) && c.version == 2 && c.bytes == sizeof(Channel) &&
           c.session && c.daemon_pid && c.daemon_birth && (c.daemon_nonce[0] || c.daemon_nonce[1]);
}
}
