#include "expression/postprocessing_file.h"
#include <cstdio>
#include <cstring>
#include <unistd.h>

using namespace expression_independent;
int main(int argc, char** argv) {
    if (getuid() != 0) { std::fprintf(stderr, "root permission required\n"); return 1; }
    if (argc != 2) { std::fprintf(stderr, "usage: picoet-config read|write|check|defaults|balanced|responsive|raw\n"); return 2; }
    PostProcessingConfig config;
    std::string error;
    const std::string command(argv[1]);
    bool ok = true;
    if (command == "read") ok = read_postprocessing_file(config, error);
    else if (command == "balanced") config = balanced_postprocessing();
    else if (command == "responsive") config = responsive_postprocessing();
    else if (command == "raw") config = raw_postprocessing();
    else if (command == "defaults") { }
    else if (command == "write" || command == "check") {
        std::string text;
        char buffer[1024];
        size_t count;
        while ((count = std::fread(buffer, 1, sizeof(buffer), stdin)) > 0) {
            text.append(buffer, count);
            if (text.size() > kPostProcessingMaxBytes) { std::fprintf(stderr, "configuration exceeds 16 KiB\n"); return 2; }
        }
        if (std::ferror(stdin)) { std::fprintf(stderr, "stdin read failed\n"); return 1; }
        ok = parse_postprocessing(text, config, error);
        if (ok && command == "write") ok = write_postprocessing_file(config, error);
    } else { std::fprintf(stderr, "unknown command\n"); return 2; }
    if (!ok) { std::fprintf(stderr, "%s\n", error.c_str()); return 1; }
    std::fputs(format_postprocessing(config).c_str(), stdout);
    return 0;
}
