// PICO OS5.13.7 camera5 adapter. Constructor starts ONLY idle control.
#include "camera_bridge.h"
#include "guard_spec.h"
#include "control_message.h"
#include "shadowhook.h"
#include <android/log.h>
#include <cerrno>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <fcntl.h>
#include <link.h>
#include <pthread.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

extern "C" __attribute__((visibility("default")))
const char picoet_camera_jpeg_version[] = "camera5-jpeg95-r1-20261027";

namespace {
#define U(x) static_cast<unsigned long long>(x)
CameraBridge& bridge() { static CameraBridge value; return value; }
struct API {
    int (*start)(const PTJConfig*, PTJSource**) = nullptr;
    uint64_t (*session_id)(const PTJSource*) = nullptr;
    CameraSubmit submit = nullptr;
    int (*stats)(const PTJSource*, PTJStats*) = nullptr;
    void (*stop)(PTJSource*) = nullptr;
    void (*destroy)(PTJSource*) = nullptr;
} api;
void* video_library = nullptr;
PTJSource* video = nullptr;
PTJStats video_stats{};
uintptr_t service_base = 0;
void* intercept_handle = nullptr;
uint8_t owned_window[32]{};
uint64_t file_fnv = 0, start_ticks = 0, owner_id = 0, command_id = 0, starts = 0;
int hook_status = 0, hook_errno = 0, rollback_result = 0, command_result = 0;
const char* hook_stage = "not_installed";
const char* state = "idle";
char root[768]{}, error[512]{}, hook_error[512]{}, last_command[256]{};
uint64_t rejected_owner = 0, status_failures = 0;

uint64_t now_ns() {
    timespec t{}; clock_gettime(CLOCK_MONOTONIC, &t);
    return uint64_t(t.tv_sec) * 1000000000ULL + uint64_t(t.tv_nsec);
}
void log(const char* format, ...) {
    char line[1024]; va_list args; va_start(args, format);
    vsnprintf(line, sizeof(line), format, args); va_end(args);
    __android_log_print(ANDROID_LOG_INFO, "PicoCameraJPEG", "%s", line);
}
void set_error(int result, const char* format, ...) {
    command_result = result;
    va_list args; va_start(args, format); vsnprintf(error, sizeof(error), format, args); va_end(args);
    log("%s", error);
}
struct Json {
    char text[16384]{};
    size_t used = 0;
    void add(const char* format, ...) {
        va_list args; va_start(args, format);
        int n = vsnprintf(text + used, sizeof(text) - used, format, args); va_end(args);
        if (n > 0 && size_t(n) < sizeof(text) - used) used += size_t(n);
    }
    void string(const char* s) {
        add("\"");
        for (; *s; ++s) {
            if (*s == '"' || *s == '\\') add("\\%c", *s);
            else if (static_cast<unsigned char>(*s) < 32) add("\\u%04x", unsigned(*s));
            else add("%c", *s);
        }
        add("\"");
    }
};
bool publish(const Json& json) {
    char path[1024], temporary[1024];
    snprintf(path, sizeof(path), "%s/camera-status.json", root);
    snprintf(temporary, sizeof(temporary), "%s/camera-status.tmp", root);
    int fd = open(temporary, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) return false;
    size_t done = 0;
    while (done < json.used) {
        ssize_t n = write(fd, json.text + done, json.used - done);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) break;
        done += size_t(n);
    }
    bool ok = done == json.used;
    if (close(fd)) ok = false;
    if (ok && rename(temporary, path)) ok = false;
    if (!ok) unlink(temporary);
    return ok;
}
void status() {
    if (video) api.stats(video, &video_stats);
    CameraStats camera = bridge().snapshot();
    Json j;
    j.add("{\"schema_version\":1,\"version\":\"%s\",\"pid\":%ld,\"start_ticks\":%llu,"
          "\"owner_id\":\"%016llx\",\"monotonic_ns\":%llu,\"state\":\"%s\","
          "\"command_id\":%llu,\"command_result\":%d,\"starts\":%llu,\"rejected_owner\":%llu,"
          "\"status_failures\":%llu,\"error\":",
          picoet_camera_jpeg_version, long(getpid()), U(start_ticks), U(owner_id), U(now_ns()), state,
          U(command_id), command_result, U(starts), U(rejected_owner), U(status_failures));
    j.string(error);
    j.add(",\"hook_status\":%d,\"hook_stage\":\"%s\",\"hook_errno\":%d,\"rollback_result\":%d,"
          "\"service_base\":\"0x%llx\",\"service_file_fnv1a64\":\"0x%016llx\",\"hook_error\":",
          hook_status, hook_stage, hook_errno, rollback_result, U(service_base), U(file_fnv));
    j.string(hook_error);
    j.add(",\"entry_offset\":%zu,\"camera_id\":5,\"port\":45681,\"lane\":2,\"stride\":1600,"
          "\"minimum_blob_bytes\":640000,\"maximum_blob_bytes\":2097152,"
          "\"source_seq_semantics\":\"vendor frame_identity at +48; no callback renumbering\","
          "\"image_time_semantics\":\"one camera5 composite parent sensor_ns copied to all three fields\","
          "\"independent_eye_times_available\":false,\"exposure_synchronization_proven\":false,"
          "\"inactive_until_subscribed\":%s,\"camera\":{\"armed\":%llu,\"epoch\":%llu,"
          "\"in_flight\":%llu,\"identity_fault\":%llu",
          CameraGuard::ENTRY, video && !video_stats.connected ? "true" : "false",
          U(camera.armed), U(camera.epoch), U(camera.in_flight), U(camera.identity_fault));
#define CAMERA_JSON(n) j.add(",\"" #n "\":%llu", U(camera.n));
    CAMERA_COUNTERS(CAMERA_JSON)
#undef CAMERA_JSON
    j.add("},\"video\":{\"session_id\":%llu,\"running\":%llu,\"connected\":%llu,\"queue_in_use\":%llu",
          U(video_stats.session_id), U(video_stats.running), U(video_stats.connected), U(video_stats.queue_in_use));
#define VIDEO_JSON(n) j.add(",\"" #n "\":%llu", U(video_stats.n));
    PTJ_COUNTER_FIELDS(VIDEO_JSON)
#undef VIDEO_JSON
    j.add("}}\n");
    if (!publish(j)) ++status_failures;
}
uint64_t process_start_ticks() {
    char text[2048];
    int fd = open("/proc/self/stat", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return 0;
    ssize_t n = read(fd, text, sizeof(text) - 1); close(fd);
    if (n <= 0) return 0;
    text[n] = 0;
    char* p = strrchr(text, ')');
    if (!p || p[1] != ' ') return 0;
    p += 2;
    for (int field = 3; field < 22; ++field) {
        p = strchr(p, ' '); if (!p) return 0;
        while (*p == ' ') ++p;
    }
    return strtoull(p, nullptr, 10);
}
int discover(dl_phdr_info* info, size_t, void*) {
    if (strcmp(info->dlpi_name, CameraGuard::SERVICE)) return 0;
    for (ElfW(Half) i = 0; i < info->dlpi_phnum; ++i) {
        const auto& p = info->dlpi_phdr[i];
        if (p.p_type == PT_LOAD && (p.p_flags & PF_X) && CameraGuard::ENTRY >= p.p_vaddr &&
            CameraGuard::ENTRY + sizeof(CameraGuard::BYTES) <= p.p_vaddr + p.p_memsz)
            service_base = info->dlpi_addr;
    }
    return 1;
}
bool fail_hook(const char* stage, const char* format, ...) {
    hook_status = -1; hook_stage = stage;
    va_list args; va_start(args, format);
    vsnprintf(hook_error, sizeof(hook_error), format, args); va_end(args);
    set_error(-1, "hook %s: %s", stage, hook_error);
    return false;
}
bool read_entry(uint8_t (&bytes)[32]) {
    // Original text mapping is execute-only. No mprotect or direct code dereference.
    int fd = open("/proc/self/mem", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return false;
    ssize_t n = pread(fd, bytes, sizeof(bytes), off_t(service_base + CameraGuard::ENTRY));
    int saved = n == ssize_t(sizeof(bytes)) ? 0 : (n < 0 ? errno : EIO);
    int closed = close(fd);
    if (saved) { errno = saved; return false; }
    return closed == 0;
}
void intercept(shadowhook_cpu_context_t* context, void*) {
    const uint64_t epoch = bridge().ticket();
    bridge().on_frame(epoch, reinterpret_cast<const void*>(context->regs[0]),
                    reinterpret_cast<const void*>(context->regs[1]));
    // Original instruction and listener continue normally; never modify saved registers/frame.
}
bool install() {
    if (hook_status < 0) { set_error(-1, "previous hook failure is terminal; no retry in this process"); return false; }
    if (hook_status == 1) {
        uint8_t bytes[32]{};
        if (!read_entry(bytes) || memcmp(bytes, owned_window, sizeof(bytes)))
            return fail_hook("owned_memory_guard", "installed camera hook window changed");
        return true;
    }
    dl_iterate_phdr(discover, nullptr);
    if (!service_base) return fail_hook("mapping_guard", "expected absolute service mapping unavailable");
    int fd = open(CameraGuard::SERVICE, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return fail_hook("file_guard", "open errno=%d", errno);
    uint8_t buffer[65536]; uint64_t hash = 14695981039346656037ULL; bool ok = true;
    for (;;) {
        ssize_t n = read(fd, buffer, sizeof(buffer));
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) { ok = false; break; }
        if (!n) break;
        for (ssize_t i = 0; i < n; ++i) hash = (hash ^ buffer[i]) * 1099511628211ULL;
    }
    uint8_t file_entry[32]{};
    ssize_t got = pread(fd, file_entry, sizeof(file_entry), off_t(CameraGuard::ENTRY));
    int closed = close(fd); file_fnv = hash;
    if (!ok || closed || hash != CameraGuard::FILE_FNV || got != ssize_t(sizeof(file_entry)) ||
        !CameraGuard::original(file_entry))
        return fail_hook("file_guard", "full-file FNV or entry32 mismatch got=%016llx", U(hash));
    uint8_t memory_entry[32]{};
    if (!read_entry(memory_entry)) return fail_hook("memory_read", "proc/self/mem errno=%d", errno);
    if (!CameraGuard::original(memory_entry))
        return fail_hook("memory_guard", "entry32 differs from original; same-site hook rejected");
    int rc = shadowhook_init(SHADOWHOOK_MODE_UNIQUE, false);
    hook_errno = shadowhook_get_errno();
    if (rc) return fail_hook("shadowhook_init", "rc=%d errno=%d", rc, hook_errno);
    void* library = shadowhook_dlopen(CameraGuard::SERVICE);
    void* symbol = library ? shadowhook_dlsym_dynsym(library, CameraGuard::SYMBOL) : nullptr;
    if (library) shadowhook_dlclose(library);
    void* entry = reinterpret_cast<void*>(service_base + CameraGuard::ENTRY);
    if (symbol != entry) return fail_hook("symbol_guard", "symbol=%p expected=%p", symbol, entry);
    intercept_handle = shadowhook_intercept_instr_addr(entry, intercept, nullptr,
                                                       SHADOWHOOK_INTERCEPT_WITH_FPSIMD_READ_WRITE);
    hook_errno = shadowhook_get_errno();
    if (!intercept_handle)
        return fail_hook("instruction_intercept", "errno=%d %s; no self-retry", hook_errno, shadowhook_to_errmsg(hook_errno));
    const bool post_read = read_entry(memory_entry);
    if (!post_read || !CameraGuard::patched(memory_entry)) {
        rollback_result = shadowhook_unintercept(intercept_handle);
        intercept_handle = nullptr;
        return fail_hook("rewrite_guard", "required B imm26 + unchanged28; rollback=%d", rollback_result);
    }
    memcpy(owned_window, memory_entry, sizeof(owned_window));
    hook_status = 1; hook_stage = "installed";
    log("camera5 hook installed at +0x36c1c; rewrite4 unchanged28; disarmed");
    return true;
}
bool load_video() {
    if (video_library) return true;
    char path[1024]; snprintf(path, sizeof(path), "%s/libptj_source.so", root);
    video_library = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!video_library) { set_error(-2, "video dlopen: %s", dlerror()); return false; }
#define RESOLVE(field, symbol) api.field = reinterpret_cast<decltype(api.field)>(dlsym(video_library, symbol));
    RESOLVE(start, "ptj_start") RESOLVE(session_id, "ptj_session_id") RESOLVE(submit, "ptj_submit")
    RESOLVE(stats, "ptj_get_stats") RESOLVE(stop, "ptj_stop") RESOLVE(destroy, "ptj_destroy")
#undef RESOLVE
    if (!api.start || !api.session_id || !api.submit || !api.stats || !api.stop || !api.destroy) {
        set_error(-2, "video C ABI exports missing"); dlclose(video_library); video_library = nullptr; return false;
    }
    return true;
}
void stop_video(const char* stopped_state = "stopped") {
    state = "stopping";
    bridge().detach_and_drain();
    if (video) {
        api.stop(video); api.stats(video, &video_stats); api.destroy(video); video = nullptr;
    }
    // Keep the video DSO and disabled hook resident. Never dlclose from a callback.
    state = stopped_state;
}
void start_video() {
    if (video) { set_error(-3, "already started; stop before creating another owner"); return; }
    state = "starting";
    if (!install() || !load_video()) { state = "error"; return; }
    PTJConfig config{}; config.struct_bytes = sizeof(config);
    int rc = api.start(&config, &video);
    if (rc) { set_error(-2, "ptj_start rc=%d", rc); state = "error"; return; }
    video_stats = {}; ++starts;
    bridge().arm(video, api.session_id(video), api.submit);
    state = "armed";
}
void commands() {
    char path[1024], line[256]{};
    snprintf(path, sizeof(path), "%s/camera-control.txt", root);
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return;
    ssize_t n = read(fd, line, sizeof(line) - 1); close(fd);
    if (n <= 0 || line[n - 1] != '\n' || !strcmp(line, last_command)) return;
    strcpy(last_command, line);
    uint64_t id = 0;
    CameraCommand command{};
    int parsed = parse_camera_control(line, owner_id, command_id, id, command);
    if (parsed == 1) return;
    if (parsed == -3) { ++rejected_owner; set_error(-2, "control owner mismatch"); return; }
    if (parsed) { set_error(-2, "control needs: owner_hex command_id start|stop"); return; }
    command_id = id; command_result = 0; error[0] = 0;
    if (command == CameraCommand::Start) start_video();
    else stop_video();
}
void* control(void*) {
    pthread_setname_np(pthread_self(), "camera-jpeg-ctl");
    bridge(); // Construct the callback gate here, not in a competing global initializer.
    start_ticks = process_start_ticks();
    owner_id = now_ns() ^ (uint64_t(getpid()) << 32) ^ start_ticks;
    if (!start_ticks) { set_error(-1, "cannot bind process start_ticks"); state = "error"; }
    for (;;) {
        if (start_ticks) commands();
        if (video && bridge().identity_fault()) {
            stop_video("identity_fault");
            set_error(-4, "camera identity/sensor regressed; explicit start required for new owner");
        }
        status(); usleep(100000);
    }
    return nullptr;
}
__attribute__((constructor)) void initialize() {
    Dl_info info{};
    if (!dladdr(reinterpret_cast<const void*>(initialize), &info) || !info.dli_fname ||
        strlen(info.dli_fname) >= sizeof(root)) { log("cannot locate payload directory"); return; }
    strcpy(root, info.dli_fname);
    char* slash = strrchr(root, '/');
    if (!slash) { log("absolute payload path required"); return; }
    *slash = 0;
    pthread_t thread;
    int rc = pthread_create(&thread, nullptr, control, nullptr);
    if (!rc) pthread_detach(thread);
    else log("idle control thread creation failed rc=%d", rc);
}
} // namespace
