#pragma once
#include <cstdint>
#include <cstdio>
#include <cstring>
enum class CameraCommand { Start, Stop };
// 0=new, 1=old ID, -2=syntax/operation error, -3=foreign owner. No side effects.
inline int parse_camera_control(const char* text, uint64_t expected_owner, uint64_t previous_id,
                                uint64_t& id, CameraCommand& command) {
    unsigned long long owner = 0, number = 0;
    char operation[16]{}, extra[2]{};
    if (std::sscanf(text, "%llx %llu %15s %1s", &owner, &number, operation, extra) != 3) return -2;
    if (owner != expected_owner) return -3;
    if (number <= previous_id) return 1;
    if (!std::strcmp(operation, "start")) command = CameraCommand::Start;
    else if (!std::strcmp(operation, "stop")) command = CameraCommand::Stop;
    else return -2;
    id = number;
    return 0;
}
