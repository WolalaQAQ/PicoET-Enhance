#pragma once
#include <cstddef>
#include <cstdint>
#include <string>
namespace ipc {
uint64_t mono_ns();
uint64_t pid();
uint64_t self_birth(int& error);
#if !defined(PTJ_IPC_SHIM)
uint64_t birth(uint64_t process, int& error); // Root-only cross-PID validation, never linked into shim.
#endif
int random_nonce(uint64_t (&words)[2]);
void pause_us(unsigned us);
std::string module_directory(const void* address);
int private_directory(const std::string& root);
struct Mapping {
    void* address = nullptr;
    size_t size = 0;
#if defined(_WIN32)
    void* file = nullptr;
    void* mapping = nullptr;
#else
    int file = -1;
#endif
};
int map_file(Mapping& m, const std::string& path, size_t bytes, bool create);
const char* map_operation();
void unmap_file(Mapping& m);
bool publish(const std::string& root, const char* name, const std::string& content, int& error);
}
