#pragma once
#include <string>

namespace picoet_panel {
constexpr const char* package_name = "dev.pico.etenhance";
constexpr size_t max_request = 16 * 1024 + 128;
struct Request { std::string id, action, configuration; };
inline bool action_allowed(const std::string& action) {
    return action == "read" || action == "defaults" || action == "balanced" ||
        action == "responsive" || action == "raw" || action == "check" ||
        action == "write" || action == "apply";
}
inline bool parse(const std::string& text, Request& out) {
    if (text.size() > max_request || text.compare(0, 5, "PPC1\n") != 0) return false;
    const auto end_id = text.find('\n', 5);
    if (end_id != 41) return false;
    const std::string id = text.substr(5, 36);
    for (size_t i = 0; i < id.size(); ++i) {
        const bool hyphen = i == 8 || i == 13 || i == 18 || i == 23;
        if (hyphen ? id[i] != '-' : !((id[i] >= '0' && id[i] <= '9') || (id[i] >= 'a' && id[i] <= 'f'))) return false;
    }
    const auto end_action = text.find('\n', end_id + 1);
    if (end_action == std::string::npos) return false;
    const std::string action = text.substr(end_id + 1, end_action - end_id - 1);
    const std::string input = text.substr(end_action + 1);
    if (!action_allowed(action) || input.find('\0') != std::string::npos || input.size() > 16384) return false;
    if (action != "write" && action != "check" && !input.empty()) return false;
    out = {id, action, input};
    return true;
}
inline std::string reply(const Request& request, bool success, const std::string& output) {
    return "PPR1\n" + request.id + (success ? "\n0\n" : "\n1\n") + output;
}
}
