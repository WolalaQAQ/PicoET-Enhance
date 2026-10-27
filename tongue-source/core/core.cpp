#include "ptj_source.h"
#include "borrow_api.h"
#include <cerrno>
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <mutex>
#include <setjmp.h>
#include <thread>
#include <jpeglib.h>
#if defined(_WIN32)
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
using Socket = SOCKET;
using PollFD = WSAPOLLFD;
static constexpr Socket bad_socket = INVALID_SOCKET;
static int socket_error() { return WSAGetLastError(); }
static bool would_block(int e) { return e == WSAEWOULDBLOCK; }
static void close_socket(Socket s) { closesocket(s); }
static void nonblocking(Socket s) { u_long one = 1; ioctlsocket(s, FIONBIO, &one); }
static int poll_sockets(PollFD* p, unsigned n, int ms) { return WSAPoll(p, n, ms); }
static uint64_t mono_ns() {
    LARGE_INTEGER now, freq; QueryPerformanceCounter(&now); QueryPerformanceFrequency(&freq);
    return uint64_t(now.QuadPart / freq.QuadPart) * 1000000000ULL +
           uint64_t(now.QuadPart % freq.QuadPart) * 1000000000ULL / uint64_t(freq.QuadPart);
}
static uint64_t process_id() { return GetCurrentProcessId(); }
#else
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
using Socket = int;
using PollFD = pollfd;
static constexpr Socket bad_socket = -1;
static int socket_error() { return errno; }
static bool would_block(int e) { return e == EAGAIN || e == EWOULDBLOCK || e == EINTR; }
static void close_socket(Socket s) { close(s); }
static void nonblocking(Socket s) { fcntl(s, F_SETFL, fcntl(s, F_GETFL, 0) | O_NONBLOCK); }
static int poll_sockets(PollFD* p, unsigned n, int ms) { return poll(p, n, ms); }
static uint64_t mono_ns() {
    timespec t{}; clock_gettime(CLOCK_MONOTONIC, &t);
    return uint64_t(t.tv_sec) * 1000000000ULL + uint64_t(t.tv_nsec);
}
static uint64_t process_id() { return uint64_t(getpid()); }
#endif

static void atomic_max(std::atomic<uint64_t>& value, uint64_t n) {
    uint64_t old = value.load(std::memory_order_relaxed);
    while (old < n && !value.compare_exchange_weak(old, n, std::memory_order_relaxed)) {}
}
struct Counters {
#define PTJ_ATOMIC(n) std::atomic<uint64_t> n{0};
    PTJ_COUNTER_FIELDS(PTJ_ATOMIC)
#undef PTJ_ATOMIC
};
struct Client {
    Socket fd;
    bool pair;
    explicit Client(Socket s, bool p) : fd(s), pair(p) {}
    ~Client() { close_socket(fd); }
};
struct Frame {
    std::array<uint8_t, PTJ_PIXELS> copy_pixels{}; // Compatibility submit only, never the daemon input.
    const uint8_t* pixels = nullptr;
    bool borrowed = false;
    PTJMeta meta{};
    uint64_t copy_ns = 0, subscription = 0;
    std::shared_ptr<Client> client;
};
struct PTJSource {
    uint64_t session;
    uint32_t timeout_ms;
    Socket listener = bad_socket;
    std::atomic<bool> running{true};
    mutable std::mutex mutex;
    std::mutex stop_mutex;
    std::condition_variable changed;
    std::array<Frame, 2> frames;
    int pending = -1, processing = -1;
    bool has_seq = false;
    uint64_t last_seen_seq = 0;
    std::shared_ptr<Client> client;
    Counters stats;
    CoreBorrowStats borrow;
    std::thread server, worker;
#if defined(_WIN32)
    bool winsock = false;
#endif
};

#if defined(PTJ_CORE_TEST)
static CoreTestHook test_hook = nullptr;
static void* test_context = nullptr;
extern "C" void core_test_set_hook(CoreTestHook hook, void* context) {
    // Tests set once before creating threads and clear only after join.
    test_hook = hook; test_context = context;
}
static void test_checkpoint(uint32_t stage, const uint8_t* p) {
    if (test_hook) test_hook(stage, p, test_context);
}
#else
static inline void test_checkpoint(uint32_t, const uint8_t*) {}
#endif
/* Bytes are only read under exclusive Processing ownership. IEEE CRC32. */
static uint32_t raw_crc32(const uint8_t* p) {
    test_checkpoint(2, p);
    static const std::array<uint32_t, 256> table = [] {
        std::array<uint32_t, 256> t{};
        for (uint32_t i = 0; i < 256; ++i) {
            uint32_t n = i;
            for (int k = 0; k < 8; ++k) n = (n >> 1) ^ ((n & 1) ? 0xedb88320U : 0);
            t[i] = n;
        }
        return t;
    }();
    uint32_t crc = 0xffffffffU;
    for (size_t i = 0; i < PTJ_PIXELS; ++i) crc = table[(crc ^ p[i]) & 255] ^ (crc >> 8);
    return crc ^ 0xffffffffU;
}

enum class IOResult { Ok, Error, Timeout, Stopped };
static IOResult write_all(PTJSource& s, Socket fd, const void* bytes, size_t count,
                          uint64_t deadline) {
    const auto* p = static_cast<const char*>(bytes);
    while (count) {
        if (!s.running.load(std::memory_order_relaxed)) return IOResult::Stopped;
        const uint64_t now = mono_ns();
        if (now >= deadline) return IOResult::Timeout;
#if defined(_WIN32)
        int n = send(fd, p, int(count), 0);
#else
        ssize_t n = send(fd, p, count, MSG_NOSIGNAL);
#endif
        if (n > 0) { p += n; count -= size_t(n); continue; }
        if (n == 0 || !would_block(socket_error())) return IOResult::Error;
        PollFD pollfd{fd, POLLOUT, 0};
        int ms = int(std::min<uint64_t>(10, (deadline - now + 999999) / 1000000));
        if (poll_sockets(&pollfd, 1, ms) < 0) return IOResult::Error;
        if (pollfd.revents & (POLLERR | POLLHUP | POLLNVAL)) return IOResult::Error;
    }
    return IOResult::Ok;
}
static void finish_frame(PTJSource& s, Frame& f) { // mutex held; every pixel reader has finished
    if (f.borrowed) {
        ++s.borrow.completions; --s.borrow.active;
        s.borrow.last_completed_address = uint64_t(reinterpret_cast<uintptr_t>(f.pixels));
        s.borrow.last_completed_seq = f.meta.source_seq;
    }
    f.pixels = nullptr; f.borrowed = false; f.client.reset();
}
static void clear_pending(PTJSource& s, bool stop) { // mutex held
    if (s.pending < 0) return;
    finish_frame(s, s.frames[size_t(s.pending)]);
    s.pending = -1;
    if (stop) ++s.stats.cancel_stop; else ++s.stats.cancel_disconnect;
}
static void drop_client(PTJSource& s, const std::shared_ptr<Client>& client) {
    std::lock_guard<std::mutex> lock(s.mutex);
    if (s.client != client) return;
    s.client.reset();
    clear_pending(s, false);
}
static PTJHeader header_for(const PTJSource& s, const Frame& f, uint32_t crc) {
    PTJHeader h{};
    std::memcpy(h.magic, "PTJ1", 4); h.version = 1; h.header_bytes = 128;
    h.session_id = s.session; h.source_seq = f.meta.source_seq;
    h.sensor_ns = f.meta.sensor_ns; h.vendor_ns = f.meta.vendor_ns;
    h.copy_mono_ns = f.copy_ns;
    h.eye0_sensor_ns = f.meta.eye0_sensor_ns; h.eye1_sensor_ns = f.meta.eye1_sensor_ns;
    h.mouth_sensor_ns = f.meta.mouth_sensor_ns; h.raw_identity = f.meta.raw_identity;
    h.width = PTJ_WIDTH; h.height = PTJ_HEIGHT; h.raw_crc32 = crc; h.quality = 95;
    return h;
}
static IOResult emit(PTJSource& s, Socket fd, PTJHeader& h, const void* p,
                      uint32_t count, uint64_t deadline, bool terminal = false) {
    h.payload_bytes = count; h.emit_mono_ns = mono_ns();
    if (!terminal && (h.flags & PTJ_RAW)) test_checkpoint(4, static_cast<const uint8_t*>(p));
    IOResult result;
    if (terminal) {
        // stop has already cleared running. One nonblocking best-effort END only.
#if defined(_WIN32)
        int n = send(fd, reinterpret_cast<const char*>(&h), sizeof(h), 0);
#else
        ssize_t n = send(fd, &h, sizeof(h), MSG_NOSIGNAL);
#endif
        return n == sizeof(h) ? IOResult::Ok : IOResult::Error;
    }
    result = write_all(s, fd, &h, sizeof(h), deadline);
    if (result == IOResult::Ok && count) result = write_all(s, fd, p, count, deadline);
    if (result == IOResult::Ok) {
        ++s.stats.sent_packets; s.stats.sent_payload_bytes += count;
    }
    return result;
}
struct JPEGError {
    jpeg_error_mgr base;
    jmp_buf jump;
};
static void jpeg_failure(j_common_ptr c) {
    auto* error = reinterpret_cast<JPEGError*>(c->err);
    longjmp(error->jump, 1);
}
#if defined(_MSC_VER)
#pragma warning(push)
// The setjmp scopes below contain POD locals only; no C++ destructor is bypassed.
#pragma warning(disable: 4611)
#endif
class Encoder {
    jpeg_compress_struct jpeg{};
    JPEGError error{};
    bool ready = false;
public:
    unsigned char* data = nullptr;
    unsigned long size = 0;
    Encoder() {
        jpeg.err = jpeg_std_error(&error.base); error.base.error_exit = jpeg_failure;
        // No C++ automatic objects live across this libjpeg error jump.
        if (setjmp(error.jump)) return;
        jpeg_create_compress(&jpeg);
        ready = true;
    }
    ~Encoder() { jpeg_destroy_compress(&jpeg); std::free(data); }
    bool encode(const uint8_t* pixels) {
        test_checkpoint(3, pixels);
        if (!ready) return false;
        if (setjmp(error.jump)) { jpeg_abort_compress(&jpeg); return false; }
        std::free(data); data = nullptr; size = 0;
        jpeg_mem_dest(&jpeg, &data, &size);
        jpeg.image_width = PTJ_WIDTH; jpeg.image_height = PTJ_HEIGHT;
        jpeg.input_components = 1; jpeg.in_color_space = JCS_GRAYSCALE;
        jpeg_set_defaults(&jpeg); jpeg_set_quality(&jpeg, 95, TRUE);
        jpeg_start_compress(&jpeg, TRUE);
        while (jpeg.next_scanline < PTJ_HEIGHT) {
            JSAMPROW row = const_cast<JSAMPROW>(pixels + jpeg.next_scanline * PTJ_WIDTH);
            jpeg_write_scanlines(&jpeg, &row, 1);
        }
        jpeg_finish_compress(&jpeg);
        return true;
    }
};
#if defined(_MSC_VER)
#pragma warning(pop)
#endif
static void encode_loop(PTJSource& s) {
    Encoder encoder;
    while (true) {
        int slot;
        {
            std::unique_lock<std::mutex> lock(s.mutex);
            s.changed.wait(lock, [&] { return !s.running || s.pending >= 0; });
            if (!s.running) break;
            slot = s.pending; s.pending = -1; s.processing = slot;
        }
        Frame& f = s.frames[size_t(slot)];
        const auto client = f.client;
        test_checkpoint(1, f.pixels);
        PTJHeader h = header_for(s, f, raw_crc32(f.pixels));
        h.encode_begin_ns = mono_ns();
        const bool encoded = encoder.encode(f.pixels);
        h.encode_end_ns = mono_ns();
        uint64_t elapsed = h.encode_end_ns - h.encode_begin_ns;
        s.stats.encode_total_ns += elapsed; atomic_max(s.stats.encode_max_ns, elapsed);
        IOResult result = IOResult::Error;
        bool still_subscribed;
        { std::lock_guard<std::mutex> lock(s.mutex); still_subscribed = s.client == client; }
        if (!encoded) ++s.stats.encode_errors;
        else {
            ++s.stats.encoded;
            if (!s.running) { ++s.stats.cancel_stop; }
            else if (!still_subscribed) { ++s.stats.cancel_disconnect; }
            else {
                const uint64_t deadline = mono_ns() + uint64_t(s.timeout_ms) * 1000000ULL;
                if (client->pair) {
                    PTJHeader raw = h;
                    raw.flags = PTJ_RAW | (f.meta.flags & PTJ_REPLAY);
                    // Direct-memory reference has no encoding stage.
                    raw.encode_begin_ns = raw.encode_end_ns = 0;
                    result = emit(s, client->fd, raw, f.pixels, PTJ_PIXELS, deadline);
                } else result = IOResult::Ok;
                if (result == IOResult::Ok) {
                    h.flags = PTJ_JPEG | (f.meta.flags & PTJ_REPLAY);
                    result = emit(s, client->fd, h, encoder.data, uint32_t(encoder.size), deadline);
                }
                if (result == IOResult::Ok) {
                    ++s.stats.sent_sources; s.stats.last_sent_seq = h.source_seq;
                } else if (result == IOResult::Stopped) {
                    ++s.stats.cancel_stop; drop_client(s, client);
                }
                else {
                    ++s.stats.send_errors;
                    if (result == IOResult::Timeout) ++s.stats.send_timeouts;
                    drop_client(s, client);
                }
            }
        }
        test_checkpoint(5, f.pixels);
        {
            std::lock_guard<std::mutex> lock(s.mutex);
            finish_frame(s, f); s.processing = -1;
        }
    }
}

static bool read_command(PTJSource& s, Socket fd, char (&command)[8], uint64_t& received) {
    const uint64_t deadline = mono_ns() + 1000000000ULL;
    size_t used = 0;
    while (s.running && mono_ns() < deadline && used < sizeof(command) - 1) {
        char c;
        int n = recv(fd, &c, 1, 0);
        if (n == 1) {
            if (c == '\n') { received = mono_ns(); command[used] = 0; return true; }
            command[used++] = c; continue;
        }
        if (n == 0 || !would_block(socket_error())) return false;
        PollFD p{fd, POLLIN, 0}; poll_sockets(&p, 1, 10);
    }
    return false;
}
static void server_loop(PTJSource& s) {
    while (s.running) {
        // Detect an idle subscriber disappearing, even when there are no sources.
        std::shared_ptr<Client> current;
        { std::lock_guard<std::mutex> lock(s.mutex); current = s.client; }
        if (current) {
            char c; int n = recv(current->fd, &c, 1, MSG_PEEK);
            if (n == 0 || (n < 0 && !would_block(socket_error()))) drop_client(s, current);
            // Stream connections are receive-only after their command.
            else if (n > 0) drop_client(s, current);
        }
        PollFD p{s.listener, POLLIN, 0};
        if (poll_sockets(&p, 1, 10) <= 0 || !(p.revents & POLLIN)) continue;
        Socket fd = accept(s.listener, nullptr, nullptr);
        if (fd == bad_socket) continue;
        nonblocking(fd);
        int one = 1, buffer = 32768;
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&one), sizeof(one));
        setsockopt(fd, SOL_SOCKET, SO_SNDBUF, reinterpret_cast<const char*>(&buffer), sizeof(buffer));
        char command[8]{}; uint64_t received = 0;
        if (!read_command(s, fd, command, received)) { close_socket(fd); continue; }
        if (std::strcmp(command, "PING") == 0) {
            uint64_t response[2]{received, mono_ns()};
            if (write_all(s, fd, response, sizeof(response), mono_ns() + 100000000ULL) == IOResult::Ok)
                ++s.stats.pings;
            close_socket(fd);
        } else if (!std::strcmp(command, "JPEG") || !std::strcmp(command, "PAIR")) {
            auto client = std::make_shared<Client>(fd, !std::strcmp(command, "PAIR"));
            std::lock_guard<std::mutex> lock(s.mutex);
            if (s.running && !s.client) { s.client = std::move(client); ++s.stats.connections; }
        } else close_socket(fd);
    }
}


static const char* start_failure_stage = "none";
static int start_failure_code = 0;
extern "C" const char* core_start_stage() { return start_failure_stage; }
extern "C" int core_start_errno() { return start_failure_code; }
extern "C" int ptj_start(const PTJConfig* config, PTJSource** out) {
    start_failure_stage = "none"; start_failure_code = 0;
    if (!out) return -1;
    *out = nullptr;
    PTJConfig c{};
    if (config) { if (config->struct_bytes != sizeof(c)) return -1; c = *config; }
    auto s = std::make_unique<PTJSource>();
    s->session = c.session_id ? c.session_id : (mono_ns() ^ (process_id() << 32));
    s->timeout_ms = c.send_timeout_ms ? c.send_timeout_ms : 100;
#if defined(_WIN32)
    WSADATA wsa{};
    if (WSAStartup(MAKEWORD(2, 2), &wsa)) return -2;
    s->winsock = true;
#endif
    s->listener = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    auto start_fail = [&](const char* stage) {
        start_failure_stage = stage; start_failure_code = socket_error();
        if (s->listener != bad_socket) close_socket(s->listener);
#if defined(_WIN32)
        WSACleanup();
#endif
        return -2;
    };
    if (s->listener == bad_socket) return start_fail("socket");
    int one = 1;
#if !defined(_WIN32)
    if (setsockopt(s->listener, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one))) return start_fail("setsockopt");
#else
    if (setsockopt(s->listener, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, reinterpret_cast<const char*>(&one), sizeof(one))) return start_fail("setsockopt");
#endif
    sockaddr_in address{}; address.sin_family = AF_INET;
    address.sin_port = htons(c.port ? c.port : 45681); address.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(s->listener, reinterpret_cast<sockaddr*>(&address), sizeof(address))) return start_fail("bind");
    if (listen(s->listener, 8)) return start_fail("listen");
    nonblocking(s->listener);
    try {
        s->worker = std::thread(encode_loop, std::ref(*s));
        s->server = std::thread(server_loop, std::ref(*s));
    } catch (...) {
        start_failure_stage = "threads"; start_failure_code = EAGAIN;
        ptj_stop(s.get()); return -3;
    }
    *out = s.release(); return 0;
}
extern "C" uint64_t ptj_session_id(const PTJSource* s) { return s ? s->session : 0; }
static int submit_frame(PTJSource* s, const uint8_t* pixels, uint32_t stride, const PTJMeta* meta,
                        uint64_t capture_copy_ns, bool borrowed, uint64_t subscription) {
    if (!s) return PTJ_INVALID;
    ++s->stats.submit_calls;
    if (!pixels || !meta || stride < PTJ_WIDTH || (meta->flags & ~uint32_t(PTJ_REPLAY)) ||
        (borrowed && (!capture_copy_ns || !subscription))) {
        ++s->stats.drop_invalid; return PTJ_INVALID;
    }
    std::unique_lock<std::mutex> lock(s->mutex, std::try_to_lock);
    if (!lock.owns_lock()) { ++s->stats.drop_busy; return PTJ_BUSY; }
    if (!s->running) { ++s->stats.drop_stopped; return PTJ_STOPPED; }
    if (meta->session_id != s->session) { ++s->stats.drop_session; return PTJ_WRONG_SESSION; }
    if (s->has_seq && meta->source_seq <= s->last_seen_seq) {
        ++s->stats.drop_duplicate; return PTJ_DUPLICATE;
    }
    s->has_seq = true; s->last_seen_seq = meta->source_seq;
    if (!s->client || (borrowed && s->stats.connections != subscription)) {
        ++s->stats.drop_no_subscriber; return PTJ_NO_SUBSCRIBER;
    }
    if (s->pending >= 0) { ++s->stats.drop_full; return PTJ_FULL; }
    const int slot = s->processing == 0 ? 1 : 0;
    Frame& f = s->frames[size_t(slot)];
    if (borrowed) {
        f.pixels = pixels; f.copy_ns = capture_copy_ns;
        ++s->borrow.admissions; ++s->borrow.active;
        s->borrow.last_admitted_address = uint64_t(reinterpret_cast<uintptr_t>(pixels));
        s->borrow.last_admitted_seq = meta->source_seq; s->borrow.last_subscription = subscription;
    } else {
        const uint64_t begin = mono_ns();
        if (stride == PTJ_WIDTH) std::memcpy(f.copy_pixels.data(), pixels, PTJ_PIXELS);
        else for (size_t row = 0; row < PTJ_HEIGHT; ++row)
            std::memcpy(f.copy_pixels.data() + row * PTJ_WIDTH, pixels + row * stride, PTJ_WIDTH);
        const uint64_t copy_done = mono_ns(), copy_ns = copy_done - begin;
        f.pixels = f.copy_pixels.data(); f.copy_ns = capture_copy_ns ? capture_copy_ns : copy_done;
        ++s->stats.copy_count; s->stats.copy_total_ns += copy_ns;
        s->stats.copy_last_ns = copy_ns; atomic_max(s->stats.copy_max_ns, copy_ns);
    }
    f.meta = *meta; f.client = s->client; f.borrowed = borrowed;
    f.subscription = borrowed ? subscription : s->stats.connections.load();
    s->pending = slot; // Mutex publishes pointer + metadata + subscription as one lease.
    ++s->stats.enqueued;
    atomic_max(s->stats.queue_peak, uint64_t(1 + (s->processing >= 0)));
    lock.unlock(); s->changed.notify_one();
    return PTJ_ACCEPTED;
}
extern "C" int ptj_submit_capture(PTJSource* s, const uint8_t* pixels, uint32_t stride,
                                 const PTJMeta* meta, uint64_t capture_copy_ns) {
    return submit_frame(s, pixels, stride, meta, capture_copy_ns, false, 0);
}
extern "C" int core_ptj_submit_borrowed(PTJSource* s, const uint8_t* pixels, const PTJMeta* meta,
                                       uint64_t capture_copy_ns, uint64_t subscription) {
    return submit_frame(s, pixels, PTJ_WIDTH, meta, capture_copy_ns, true, subscription);
}
extern "C" int core_ptj_get_borrow_stats(const PTJSource* s, CoreBorrowStats* out) {
    if (!s || !out) return -1;
    std::lock_guard<std::mutex> lock(s->mutex); *out = s->borrow; return 0;
}
extern "C" int ptj_get_stats(const PTJSource* s, PTJStats* stats) {
    if (!s || !stats) return -1;
    *stats = {}; stats->struct_bytes = sizeof(*stats); stats->session_id = s->session;
    stats->running = s->running;
    {
        std::lock_guard<std::mutex> lock(s->mutex);
        stats->connected = bool(s->client);
        stats->queue_in_use = uint64_t((s->pending >= 0) + (s->processing >= 0));
    }
#define PTJ_LOAD(n) stats->n = s->stats.n.load(std::memory_order_relaxed);
    PTJ_COUNTER_FIELDS(PTJ_LOAD)
#undef PTJ_LOAD
    return 0;
}
extern "C" void ptj_stop(PTJSource* s) {
    if (!s) return;
    std::lock_guard<std::mutex> stop_lock(s->stop_mutex);
    {
        std::lock_guard<std::mutex> lock(s->mutex);
        s->running = false; clear_pending(*s, true);
    }
    s->changed.notify_all();
    if (s->server.joinable()) s->server.join();
    if (s->worker.joinable()) s->worker.join();
    // Never append END behind a failed/partial frame; close terminates that connection.
    std::shared_ptr<Client> current;
    {
        std::lock_guard<std::mutex> lock(s->mutex);
        current = std::move(s->client);
    }
    if (current) {
        PTJHeader h{}; std::memcpy(h.magic, "PTJ1", 4);
        h.version = 1; h.header_bytes = 128; h.flags = PTJ_END; h.session_id = s->session;
        h.width = PTJ_WIDTH; h.height = PTJ_HEIGHT; h.quality = 95;
        emit(*s, current->fd, h, nullptr, 0, 0, true);
        current.reset();
    }
    if (s->listener != bad_socket) { close_socket(s->listener); s->listener = bad_socket; }
#if defined(_WIN32)
    if (s->winsock) { WSACleanup(); s->winsock = false; }
#endif
}
extern "C" void ptj_destroy(PTJSource* s) { if (s) { ptj_stop(s); delete s; } }

extern "C" int ptj_submit(PTJSource* s, const uint8_t* pixels, uint32_t stride, const PTJMeta* meta) {
    return ptj_submit_capture(s, pixels, stride, meta, 0);
}
