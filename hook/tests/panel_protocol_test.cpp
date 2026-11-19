#include "../src/panel_protocol.h"
#include <cstdio>
#include <cstdlib>
using namespace picoet_panel;
unsigned checks;
void check(bool value) { ++checks; if (!value) std::exit(1); }
int main() {
    const std::string prefix = "PPC1\n12345678-abcd-1234-abcd-123456789abc\n";
    Request request;
    for (const auto* action : {"read", "defaults", "balanced", "responsive", "raw", "check", "write", "apply",
                               "camera-status", "camera-on", "camera-off", "camera-results-on", "camera-results-off",
                               "camera-eyes-on", "camera-eyes-off"})
        check(parse(prefix + action + "\n", request) && request.action == action);
    for (const auto* action : {"sh", "read; id", "--force", "", "read\napply"}) check(!parse(prefix + action + "\n", request));
    check(!parse("PPC1\n../../arbitrary\nwrite\n", request));
    check(!parse(prefix + "apply\nsh arbitrary", request));
    check(!parse(prefix + "write\n" + std::string(16385, 'a'), request));
    check(!parse(prefix + "write\n" + std::string("a\0b", 3), request));
    check(parse(prefix + "write\nversion=1\neye.enabled=1\n", request));
    check(request.configuration == "version=1\neye.enabled=1\n");
    check(reply(request, true, "saved") == "PPR1\n12345678-abcd-1234-abcd-123456789abc\n0\nsaved");
    check(reply(request, false, "failed").find("\n1\nfailed") != std::string::npos);
    std::printf("%u fixed panel protocol checks passed\n", checks);
}
