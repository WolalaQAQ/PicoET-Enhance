#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>
namespace CameraGuard {
inline constexpr const char* SERVICE = "/system/lib64/libpxreyetrackingservice.so";
inline constexpr const char* SYMBOL =
    "_ZN3pvr13CameraManager19CameraImageListener16onFrameAvailableEPNS_16pvr_frame_item_tE";
inline constexpr uint64_t FILE_FNV = 0xC51A2999AF634D51ULL;
inline constexpr size_t ENTRY = 0x36c1c;
inline constexpr uint8_t BYTES[32] = {
    0xff,0x43,0x03,0xd1,0xf9,0x43,0x00,0xf9,
    0xf8,0x5f,0x09,0xa9,0xf6,0x57,0x0a,0xa9,
    0xf4,0x4f,0x0b,0xa9,0xfd,0x7b,0x0c,0xa9,
    0xfd,0x03,0x03,0x91,0x57,0xd0,0x3b,0xd5
};
inline bool original(const uint8_t* p) { return !std::memcmp(p, BYTES, sizeof(BYTES)); }
inline bool patched(const uint8_t* p) {
    uint32_t instruction; std::memcpy(&instruction, p, 4);
    return (instruction & 0xfc000000U) == 0x14000000U && !std::memcmp(p + 4, BYTES + 4, 28);
}
}
