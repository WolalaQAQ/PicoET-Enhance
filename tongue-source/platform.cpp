#include "platform.h"
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <bcrypt.h>
#else
#include <dlfcn.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/random.h>
#include <time.h>
#include <unistd.h>
#endif
namespace ipc {
static thread_local const char* mapping_operation = "none";
const char* map_operation() { return mapping_operation; }
#if defined(_WIN32)
uint64_t mono_ns() {
    LARGE_INTEGER n, f; QueryPerformanceCounter(&n); QueryPerformanceFrequency(&f);
    return uint64_t(n.QuadPart / f.QuadPart) * 1000000000ULL +
           uint64_t(n.QuadPart % f.QuadPart) * 1000000000ULL / uint64_t(f.QuadPart);
}
uint64_t pid() { return GetCurrentProcessId(); }
static uint64_t handle_birth(HANDLE h, int& error) {
    FILETIME created{}, exited{}, kernel{}, user{}; DWORD code = 0;
    bool queried = GetProcessTimes(h, &created, &exited, &kernel, &user) && GetExitCodeProcess(h, &code);
    bool ok = queried && code == STILL_ACTIVE;
    error = queried ? (ok ? 0 : ERROR_PROCESS_ABORTED) : int(GetLastError());
    return ok ? (uint64_t(created.dwHighDateTime) << 32) | created.dwLowDateTime : 0;
}
uint64_t self_birth(int& error) { return handle_birth(GetCurrentProcess(), error); }
#if !defined(PTJ_IPC_SHIM)
uint64_t birth(uint64_t process, int& error) {
    HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, DWORD(process));
    if (!h) { error = int(GetLastError()); return 0; }
    uint64_t result = handle_birth(h, error); CloseHandle(h); return result;
}
#endif
int random_nonce(uint64_t (&words)[2]) {
    NTSTATUS status = BCryptGenRandom(nullptr, reinterpret_cast<PUCHAR>(words), sizeof(words),
                                     BCRYPT_USE_SYSTEM_PREFERRED_RNG);
    return status ? int(status) : (words[0] || words[1] ? 0 : EIO);
}
void pause_us(unsigned us) {
    if (us >= 1000) Sleep((us + 999) / 1000); else SwitchToThread();
}
std::string module_directory(const void* address) {
    HMODULE module = nullptr; char path[2048]{};
    if (!GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                           reinterpret_cast<LPCSTR>(address), &module) ||
        !GetModuleFileNameA(module, path, DWORD(sizeof(path)))) return {};
    std::string s(path); return s.substr(0, s.find_last_of("/\\"));
}
int private_directory(const std::string&) { return 0; }
int map_file(Mapping& m, const std::string& path, size_t bytes, bool create) {
    mapping_operation = "CreateFileA";
    HANDLE file = CreateFileA(path.c_str(), GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
        create ? CREATE_NEW : OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return int(GetLastError());
    LARGE_INTEGER length{}; length.QuadPart = LONGLONG(bytes);
    mapping_operation = "SetEndOfFile";
    if (create && (!SetFilePointerEx(file, length, nullptr, FILE_BEGIN) || !SetEndOfFile(file))) {
        int e = int(GetLastError()); CloseHandle(file); return e;
    }
    mapping_operation = "GetFileSizeEx/check_size";
    if (!GetFileSizeEx(file, &length)) {
        int e = int(GetLastError()); CloseHandle(file); return e;
    }
    if (length.QuadPart != LONGLONG(bytes)) {
        CloseHandle(file); return ERROR_BAD_LENGTH;
    }
    mapping_operation = "CreateFileMappingA";
    HANDLE mapping = CreateFileMappingA(file, nullptr, PAGE_READWRITE, 0, 0, nullptr);
    if (!mapping) { int e = int(GetLastError()); CloseHandle(file); return e; }
    mapping_operation = "MapViewOfFile";
    void* p = MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, bytes);
    if (!p) { int e = int(GetLastError()); CloseHandle(mapping); CloseHandle(file); return e; }
    m.address = p; m.size = bytes; m.file = file; m.mapping = mapping; mapping_operation = "none"; return 0;
}
void unmap_file(Mapping& m) {
    if (m.address) UnmapViewOfFile(m.address);
    if (m.mapping) CloseHandle(m.mapping);
    if (m.file) CloseHandle(m.file);
    m = {};
}
#else
uint64_t mono_ns() {
    timespec t{}; clock_gettime(CLOCK_MONOTONIC, &t);
    return uint64_t(t.tv_sec) * 1000000000ULL + uint64_t(t.tv_nsec);
}
uint64_t pid() { return uint64_t(getpid()); }
static uint64_t path_birth(const char* path, int& error) {
    char text[2048];
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) { error = errno; return 0; }
    ssize_t n = read(fd, text, sizeof(text) - 1); int e = errno; close(fd);
    if (n <= 0) { error = n < 0 ? e : EIO; return 0; }
    text[n] = 0; char* p = strrchr(text, ')');
    if (!p || p[1] != ' ' || p[2] == 'Z' || p[2] == 'X') { error = ESRCH; return 0; }
    p += 2;
    for (int field = 3; field < 22; ++field) {
        p = strchr(p, ' '); if (!p) { error = EINVAL; return 0; }
        while (*p == ' ') ++p;
    }
    error = 0; return strtoull(p, nullptr, 10);
}
uint64_t self_birth(int& error) { return path_birth("/proc/self/stat", error); }
#if !defined(PTJ_IPC_SHIM)
uint64_t birth(uint64_t process, int& error) {
    char path[96]; snprintf(path, sizeof(path), "/proc/%llu/stat", (unsigned long long)process);
    return path_birth(path, error);
}
#endif
int random_nonce(uint64_t (&words)[2]) {
    size_t done = 0;
    while (done < sizeof(words)) {
        ssize_t n = getrandom(reinterpret_cast<char*>(words) + done, sizeof(words) - done, 0);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return n < 0 ? errno : EIO;
        done += size_t(n);
    }
    return words[0] || words[1] ? 0 : EIO;
}
void pause_us(unsigned us) { usleep(us); }
std::string module_directory(const void* address) {
    Dl_info info{};
    if (!dladdr(address, &info) || !info.dli_fname) return {};
    std::string path(info.dli_fname); return path.substr(0, path.find_last_of('/'));
}
int private_directory(const std::string& root) {
    struct stat s{};
    if (lstat(root.c_str(), &s)) return errno;
    return S_ISDIR(s.st_mode) && s.st_uid == 0 && (s.st_mode & 0777) == 0700 ? 0 : EACCES;
}
int map_file(Mapping& m, const std::string& path, size_t bytes, bool create) {
    mapping_operation = "open";
    int fd = open(path.c_str(), O_RDWR | O_CLOEXEC | O_NOFOLLOW | (create ? O_CREAT | O_EXCL : 0), 0600);
    if (fd < 0) return errno;
    mapping_operation = "ftruncate";
    if (create && ftruncate(fd, off_t(bytes))) { int e = errno; close(fd); return e; }
    struct stat s{};
    mapping_operation = "fstat";
    if (fstat(fd, &s)) { int e = errno; close(fd); return e; }
    mapping_operation = "check_owner_mode_size";
    if (!S_ISREG(s.st_mode) || s.st_uid != 0 || (s.st_mode & 0777) != 0600 || s.st_size != off_t(bytes)) {
        close(fd); return EINVAL;
    }
    mapping_operation = "mmap";
    void* p = mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (p == MAP_FAILED) { int e = errno; close(fd); return e; }
    m.address = p; m.size = bytes; m.file = fd; mapping_operation = "none"; return 0;
}
void unmap_file(Mapping& m) {
    if (m.address) munmap(m.address, m.size);
    if (m.file >= 0) close(m.file);
    m = {};
}
#endif
bool publish(const std::string& root, const char* name, const std::string& content, int& error) {
    const std::string path = root + "/" + name, temporary = path + ".tmp";
    FILE* f = fopen(temporary.c_str(), "wb");
    if (!f) { error = errno; return false; }
    bool ok = fwrite(content.data(), 1, content.size(), f) == content.size();
    error = ok ? 0 : errno;
    if (fclose(f)) { ok = false; error = errno; }
#if defined(_WIN32)
    if (ok && !MoveFileExA(temporary.c_str(), path.c_str(), MOVEFILE_REPLACE_EXISTING)) {
        error = int(GetLastError()); ok = false;
    }
#else
    if (ok && rename(temporary.c_str(), path.c_str())) { error = errno; ok = false; }
#endif
    return ok;
}
}
