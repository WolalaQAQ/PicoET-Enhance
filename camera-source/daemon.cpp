#include "channel.h"
#include "platform.h"
#include "core_api.h"
#include "tracking_demand.h"
#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <new>
#include <sstream>
using namespace ipc;
namespace {
CoreBorrowStats borrowed[PTJ_STREAMS];
uint64_t slot_addresses[PTJ_STREAMS]{}, slot_offsets[PTJ_STREAMS]{};
volatile std::sig_atomic_t interrupted = 0;
TrackingDemand* tracking = nullptr;
void signal_stop(int) { interrupted = 1; }
void failure(Channel& c, uint32_t stage, int error) {
    c.error_stage = stage; c.error_code = uint32_t(error);
    for (auto& image : c.images) image.subscription.store(0, std::memory_order_release);
    c.stock_subscription.store(0, std::memory_order_release);
    c.eye_subscription.store(0, std::memory_order_release);
    c.state.store(Failed, std::memory_order_release);
}
void release_slot(ImageChannel& image, int index) {
    --image.in_use; image.slots[index].state.store(Free, std::memory_order_release);
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
    const auto& m = c.images[PTJ_MOUTH];
    const auto& borrow = borrowed[PTJ_MOUTH];
    const auto last_slot_address = slot_addresses[PTJ_MOUTH], last_slot_offset = slot_offsets[PTJ_MOUTH];
    std::ostringstream j;
    j << "{\"schema_version\":4,\"state\":" << c.state.load() << ",\"session\":" << c.session
      << ",\"daemon_pid\":" << c.daemon_pid << ",\"daemon_birth\":" << c.daemon_birth
      << ",\"heartbeat_ns\":" << c.daemon_heartbeat.load() << ",\"capture_epoch\":" << c.capture_epoch.load()
      << ",\"client_pid\":" << c.client_pid.load()
      << ",\"client_birth\":" << c.client_birth.load() << ",\"client_heartbeat_ns\":" << c.client_heartbeat.load()
      << ",\"subscription\":" << m.subscription.load() << ",\"stop_ack\":" << c.stop_ack.load()
      << ",\"producer_lock\":" << m.producer_lock.load() << ",\"client_ready\":" << c.client_ready.load()
      << ",\"request_session\":" << c.request_session.load() << ",\"attach_ack\":" << c.attach_ack.load()
      << ",\"attach_ack_session\":" << c.attach_ack_session.load() << ",\"stop_ack_session\":" << c.stop_ack_session.load()
      << ",\"root_nonce\":[" << c.daemon_nonce[0] << ',' << c.daemon_nonce[1]
      << "],\"client_nonce\":[" << c.client_nonce[0].load() << ',' << c.client_nonce[1].load()
      << "],\"attach_client_nonce\":[" << c.attach_client_nonce[0].load() << ',' << c.attach_client_nonce[1].load()
      << "],\"attach_root_nonce\":[" << c.attach_root_nonce[0].load() << ',' << c.attach_root_nonce[1].load()
      << "],\"stop_client_nonce\":[" << c.stop_client_nonce[0].load() << ',' << c.stop_client_nonce[1].load() << ']'
      << ",\"stage\":\"" << stage_name(c.error_stage.load()) << "\",\"errno\":" << c.error_code.load()
      << ",\"in_use\":" << m.in_use.load() << ",\"peak\":" << m.peak.load()
      << ",\"pending\":" << m.pending.load() << ",\"slot_states\":[" << m.slots[0].state.load()
      << ',' << m.slots[1].state.load() << "],\"dispatched\":" << m.dispatched.load()
      << ",\"completed\":" << m.completed.load() << ",\"cancel_disconnect\":" << m.cancel_disconnect.load()
      << ",\"cancel_stop\":" << m.cancel_stop.load() << ",\"dispatch_dropped\":" << m.dispatch_dropped.load()
      << ",\"last_capture_copy_ns\":" << m.last_capture_copy_ns.load()
      << ",\"last_dispatch_ns\":" << m.last_dispatch_ns.load()
      << ",\"borrowed\":{\"admissions\":" << borrow.admissions << ",\"completions\":" << borrow.completions
      << ",\"active\":" << borrow.active << ",\"last_admitted_address\":" << borrow.last_admitted_address
      << ",\"last_completed_address\":" << borrow.last_completed_address
      << ",\"last_admitted_seq\":" << borrow.last_admitted_seq << ",\"last_completed_seq\":" << borrow.last_completed_seq
      << ",\"last_subscription\":" << borrow.last_subscription << ",\"last_slot_address\":" << last_slot_address
      << ",\"last_slot_offset\":" << last_slot_offset << "},\"producer\":{";
    PTJStats producer = load_stats(m.producer), core = load_stats(m.core);
    bool comma = false;
#define P(n) j << (comma ? "," : "") << "\"" #n "\":" << producer.n; comma = true;
    PTJ_COUNTER_FIELDS(P)
#undef P
    j << "},\"stock\":{\"subscription\":" << c.stock_subscription.load()
      << ",\"calls\":" << c.stock_calls.load() << ",\"enqueued\":" << c.stock_enqueued.load()
      << ",\"full\":" << c.stock_full.load() << ",\"unsubscribed\":" << c.stock_unsubscribed.load()
      << ",\"dispatched\":" << c.stock_dispatched.load() << ",\"cancelled\":" << c.stock_cancelled.load()
      << "},\"core\":{\"queue_in_use\":" << core.queue_in_use << ",\"connected\":" << core.connected;
#define P(n) j << ",\"" #n "\":" << core.n;
    PTJ_COUNTER_FIELDS(P)
#undef P
    j << "},\"streams\":[";
    static const char* names[] = {"mouth", "eye_left", "eye_right"};
    for (uint32_t i = 0; i < PTJ_STREAMS; ++i) {
        const auto& image = c.images[i];
        const auto stats = load_stats(image.core), copies = load_stats(image.producer);
        if (i) j << ',';
        j << "{\"id\":\"" << names[i] << "\",\"subscription\":" << image.subscription.load()
          << ",\"connected\":" << stats.connected << ",\"in_use\":" << image.in_use.load()
          << ",\"peak\":" << image.peak.load() << ",\"dispatched\":" << image.dispatched.load()
          << ",\"completed\":" << image.completed.load() << ",\"cancel_disconnect\":" << image.cancel_disconnect.load()
          << ",\"cancel_stop\":" << image.cancel_stop.load() << ",\"dispatch_dropped\":" << image.dispatch_dropped.load()
          << ",\"copy_count\":" << copies.copy_count << ",\"encoded\":" << stats.encoded
          << ",\"sent_sources\":" << stats.sent_sources << ",\"send_errors\":" << stats.send_errors
          << ",\"borrow_active\":" << borrowed[i].active << '}';
    }
    j << ']';
    j << ",\"eye_sharing\":" << (c.eye_applied.load() == 2)
      << ",\"eye_subscription\":" << c.eye_subscription.load();
    if (tracking) {
        const auto demand = tracking->snapshot();
        j << ",\"tracking\":{\"wanted\":" << demand.wanted << ",\"active\":" << demand.active
          << ",\"busy\":" << demand.busy << ",\"transitions\":" << demand.transitions
          << ",\"transport\":" << demand.result.transport << ",\"binder\":" << demand.result.binder
          << ",\"service\":" << demand.result.service << '}';
    }
    j << "}\n"; return j.str();
}
void status(const std::string& root, Channel& c) {
    int error = 0;
    publish(root, "ipc-daemon-status.json", status_json(c), error);
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
    if (argc < 2 || argc > 5 || (argc >= 4 && std::strcmp(argv[3], "results-off") && std::strcmp(argv[3], "results-on")) ||
        (argc == 5 && std::strcmp(argv[4], "eyes-off") && std::strcmp(argv[4], "eyes-on"))) {
        std::fprintf(stderr, "Usage: ptj_ipc_daemon <private-root0700> [port=45681 [results-off|results-on]]\n"); return 2;
    }
    const bool managed = argc >= 4;
    const bool allow_results = !managed || !std::strcmp(argv[3], "results-on");
    const bool allow_eyes = argc == 5 && !std::strcmp(argv[4], "eyes-on");
    const std::string root = argv[1];
    int error = private_directory(root);
    if (error) { std::fprintf(stderr, "FAIL stage=private_directory errno=%d\n", error); return 1; }
    Mapping mapping;
    error = map_file(mapping, root + "/video-channel.bin", sizeof(Channel), true);
    if (error) { std::fprintf(stderr, "FAIL stage=channel_create errno=%d operation=%s\n", error, map_operation()); return 1; }
    Channel& c = *new(mapping.address) Channel;
    std::memcpy(c.magic, "PTJIPC5", 8);
    c.daemon_pid = pid(); c.daemon_birth = self_birth(error);
    if (!c.daemon_birth) failure(c, SelfIdentity, error ? error : ESRCH);
    else if ((error = random_nonce(c.daemon_nonce))) failure(c, NonceRandom, error);
    c.session = c.daemon_nonce[0] ? c.daemon_nonce[0] : c.daemon_nonce[1];
    c.daemon_heartbeat = mono_ns();
    for (auto& image : c.images) { image.producer.session_id = c.session; image.producer.running = 1; }
    PTJConfig config{}; config.struct_bytes = sizeof(config); config.session_id = c.session;
    config.port = argc >= 3 ? uint16_t(std::atoi(argv[2])) : 45681;
    PTJSource* cores[PTJ_STREAMS]{};
    if (!config.port || config.port > 65533) failure(c, CoreBind, EINVAL);
    for (uint32_t stream = 0; stream < PTJ_STREAMS && c.state != Failed; ++stream) {
        PTJConfig stream_config = config;
        stream_config.port = uint16_t(config.port + stream); stream_config.reserved = uint16_t(stream);
        if (core_ptj_start_with_results(&stream_config, &cores[stream],
                                       stream == PTJ_MOUTH ? allow_results : stream == PTJ_EYE_LEFT && allow_eyes))
            failure(c, core_failure_stage(), core_start_errno());
    }
    if (c.state != Failed) {
        for (auto* source : cores) {
            // Multicast membership is best-effort; broadcast, HTTP /cameras and direct TCP do not depend on it.
            (void)core_ptj_bind_camera_catalog(source, config.port,
                uint16_t(config.port + 1), uint16_t(config.port + 2));
        }
    }
    if (c.state == Failed) {
        for (auto* core : cores) core_ptj_destroy(core);
        status(root, c);
        std::fprintf(stderr, "FAIL stage=%s errno=%u\n", stage_name(c.error_stage), c.error_code.load());
        unmap_file(mapping); return 1;
    }
    PTJSource* core = cores[PTJ_MOUTH]; // Labels and the legacy control endpoint remain on mouth.
    PTJSource* eye = cores[PTJ_EYE_LEFT];
    auto native_operation = tracking_operation();
    TrackingDemand demand([&c, native_operation](bool start) {
        if (start) c.capture_epoch.fetch_add(1, std::memory_order_release);
        return native_operation(start);
    });
    tracking = &demand;
    std::signal(SIGINT, signal_stop); std::signal(SIGTERM, signal_stop);
    if (managed) {
        c.results_request = allow_results ? 2 : 1;
        c.results_applied = c.results_request.load();
    }
    c.eye_request = allow_eyes ? 2 : 1;
    c.eye_applied = c.eye_request.load();
    c.state.store(Ready, std::memory_order_release);
    std::printf("READY session=%llu pid=%llu birth=%llu channel_bytes=%zu port=%u\n",
                (unsigned long long)c.session, (unsigned long long)c.daemon_pid,
                (unsigned long long)c.daemon_birth, sizeof(Channel), unsigned(config.port));
    std::fflush(stdout); status(root, c);
    uint64_t last_status = 0, last_identity_check = 0;
    Request request;
    bool registered = false, validated = false, prev_busy = true;
    int current[PTJ_STREAMS]{-1, -1, -1};
    for (;;) {
        const uint64_t now = mono_ns();
        c.daemon_heartbeat.store(now, std::memory_order_release);
        bool just_validated = false;
        if (!registered && c.client_ready.load(std::memory_order_acquire)) {
            request = Request(c); registered = true;
            if (!validate_request(c, request)) break;
            validated = true; just_validated = true; last_identity_check = now;
            if (!c.stop_request && !interrupted) {
                // The same verified Request remains immutable for this random session.
                for (auto* source : cores)
                    if (core_ptj_bind_capture_context(source, c.session, request.birth) != PTJ_ACCEPTED)
                        failure(c, OwnerEcho, EPROTO);
                if (c.state == Failed) break;
                attach_ack(c, request);
            }
        }
        if (registered && !request.matches(c)) { failure(c, OwnerEcho, EPROTO); break; }
        const bool stopping = c.stop_request || interrupted || c.state == Failed;
        const uint32_t results_request = c.results_request.load();
        const bool change_results = managed && !stopping && results_request != c.results_applied;
        if (change_results) core_ptj_allow_results(core, results_request == 2);
        const uint32_t eye_request = c.eye_request.load();
        const bool change_eyes = managed && !stopping && eye_request != c.eye_applied;
        if (change_eyes) core_ptj_allow_results(eye, eye_request == 2);
        for (uint32_t stream = 0; stream < PTJ_STREAMS; ++stream)
            c.images[stream].subscription.store(!stopping && validated
                ? core_ptj_image_subscription(cores[stream]) : 0, std::memory_order_release);
        const uint64_t stock_subscription = !stopping && validated ? core_ptj_stock_subscription(core) : 0;
        c.stock_subscription.store(stock_subscription, std::memory_order_release);
        const uint64_t eye_subscription = !stopping && validated ? core_ptj_stock_subscription(eye) : 0;
        c.eye_subscription.store(eye_subscription, std::memory_order_release);
        if (change_results) c.results_applied.store(results_request, std::memory_order_release);
        if (change_eyes) c.eye_applied.store(eye_request, std::memory_order_release);
        bool wanted = stock_subscription != 0 || eye_subscription != 0;
        for (const auto& image : c.images) wanted |= image.subscription.load() != 0;
#if defined(__ANDROID__)
        demand.want(!stopping && wanted);
#endif
        if (stopping) break;
        bool busy = !validated || wanted || change_results || change_eyes;
        for (uint32_t stream = 0; stream < PTJ_STREAMS; ++stream) {
            if (current[stream] >= 0 || c.images[stream].pending.load(std::memory_order_relaxed)) busy = true;
        }
        const bool status_due = just_validated || change_results || change_eyes || busy != prev_busy ||
            now - last_status >= (busy ? 100000000ULL : 5000000000ULL);
        PTJStats stats[PTJ_STREAMS]{};
        if (busy || status_due) {
            for (uint32_t stream = 0; stream < PTJ_STREAMS; ++stream) {
                core_ptj_get_stats(cores[stream], &stats[stream]);
                store_stats(c.images[stream].core, stats[stream]);
                core_ptj_get_borrow_stats(cores[stream], &borrowed[stream]);
            }
        }
        for (auto& slot : c.labels) {
            uint32_t pending = Pending;
            if (!slot.state.compare_exchange_strong(pending, Processing, std::memory_order_acq_rel)) continue;
            busy = true;
            const int result = stock_subscription && stock_subscription == slot.subscription
                ? core_ptj_submit_stock(core, &slot.record, stock_subscription) : PTJ_NO_SUBSCRIBER;
            if (result == PTJ_ACCEPTED) ++c.stock_dispatched;
            else ++c.stock_cancelled;
            slot.state.store(Free, std::memory_order_release);
        }
        for (auto& slot : c.eyes) {
            uint32_t pending = Pending;
            if (!slot.state.compare_exchange_strong(pending, Processing, std::memory_order_acq_rel)) continue;
            busy = true;
            if (eye_subscription && eye_subscription == slot.subscription)
                core_ptj_submit_eye(eye, &slot.record, eye_subscription);
            slot.state.store(Free, std::memory_order_release);
        }
        const uint64_t identity_interval = busy ? 100000000ULL : 500000000ULL;
        if (validated && now - last_identity_check >= identity_interval) {
            last_identity_check = now;
            int e = 0;
            if (birth(request.pid, e) != request.birth) {
                failure(c, ClientIdentity, e ? e : ESRCH); break;
            }
        }
        for (uint32_t stream = 0; stream < PTJ_STREAMS; ++stream) {
          auto& image = c.images[stream];
          int& active = current[stream];
          if (active >= 0 && stats[stream].queue_in_use == 0) {
            ++image.completed; release_slot(image, active); active = -1;
          }
          if (active < 0) {
            uint32_t pending = image.pending.load(std::memory_order_acquire);
            if (pending == 1 || pending == 2) {
                if (image.pending.compare_exchange_strong(pending, 0, std::memory_order_acq_rel)) {
                    busy = true;
                    const int index = int(pending - 1); Slot& slot = image.slots[index];
                    uint32_t expected = Pending;
                    if (!slot.state.compare_exchange_strong(expected, Processing, std::memory_order_acq_rel)) {
                        failure(c, CoreSubmit, EPROTO); break;
                    }
                    const uint64_t subscription = image.subscription.load(std::memory_order_acquire);
                    if (!subscription || subscription != slot.subscription) {
                        ++image.cancel_disconnect; release_slot(image, index);
                    } else {
                        image.last_capture_copy_ns = slot.copy_mono_ns; image.last_dispatch_ns = mono_ns();
                        int result = core_ptj_submit_borrowed(cores[stream], slot.pixels, &slot.meta,
                                                              slot.copy_mono_ns, slot.subscription);
                        if (result == PTJ_ACCEPTED) {
                            ++image.dispatched; active = index;
                            slot_addresses[stream] = uint64_t(reinterpret_cast<uintptr_t>(slot.pixels));
                            slot_offsets[stream] = slot_addresses[stream] - uint64_t(reinterpret_cast<uintptr_t>(&c));
                        }
                        else {
                            if (result == PTJ_NO_SUBSCRIBER) ++image.cancel_disconnect;
                            else ++image.dispatch_dropped;
                            release_slot(image, index);
                        }
                    }
                }
            }
          }
          if (active >= 0) busy = true;
        }
        if (c.state == Failed) break;
        if (status_due || busy != prev_busy) {
            status(root, c); last_status = now;
        }
        prev_busy = busy;
        pause_us(busy ? 250 : 100000);
    }
    for (auto& image : c.images) image.subscription.store(0, std::memory_order_release);
    c.stock_subscription.store(0, std::memory_order_release);
    c.eye_subscription.store(0, std::memory_order_release);
    demand.want(false);
    for (;;) {
        const auto state = demand.snapshot();
        if (!state.busy && (!state.active || !state.result.ok())) break;
        c.daemon_heartbeat = mono_ns(); pause_us(1000);
    }
    demand.stop(); // Only this daemon's lease; other Binder consumers retain theirs.
    const bool failed = c.state == Failed;
    c.state.store(Stopping, std::memory_order_release);
    // Native join precedes freeing the IPC CURRENT slot and every stop ACK.
    for (uint32_t stream = 0; stream < PTJ_STREAMS; ++stream) {
        core_ptj_stop(cores[stream]);
        PTJStats final{}; core_ptj_get_stats(cores[stream], &final); store_stats(c.images[stream].core, final);
        core_ptj_get_borrow_stats(cores[stream], &borrowed[stream]);
        core_ptj_destroy(cores[stream]);
        if (current[stream] >= 0) {
            ++c.images[stream].completed; release_slot(c.images[stream], current[stream]);
        }
    }
    const uint64_t drain_deadline = mono_ns() + 500000000ULL;
    auto producers_busy = [&] {
        for (const auto& image : c.images) if (image.producer_lock.load()) return true;
        for (const auto& slot : c.labels) if (slot.state.load() == Writing) return true;
        for (const auto& slot : c.eyes) if (slot.state.load() == Writing) return true;
        return false;
    };
    while (producers_busy() && mono_ns() < drain_deadline) {
        c.daemon_heartbeat = mono_ns(); pause_us(1000);
    }
    if (producers_busy()) { c.error_stage = LocalCalls; c.error_code = EBUSY; }
    else {
        for (auto& slot : c.labels) slot.state.store(Free, std::memory_order_release);
        for (auto& slot : c.eyes) slot.state.store(Free, std::memory_order_release);
        for (auto& image : c.images) {
            uint32_t p = image.pending.exchange(0, std::memory_order_acq_rel);
            if (p == 1 || p == 2) { ++image.cancel_stop; release_slot(image, int(p - 1)); }
        }
    }
    for (auto& image : c.images) image.producer.running = 0;
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
