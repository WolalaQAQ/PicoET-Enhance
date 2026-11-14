#include "postprocessing_file.h"
#include "../root_file.h"
#include <cstdio>
#include <cstring>
#include <cstdlib>

namespace expression_independent {
bool read_postprocessing_file(PostProcessingConfig& config, std::string& error) {
    int fd = picoet_open_root_file(kPostProcessingFile, O_RDONLY, 0);
    if (fd < 0) {
        if (errno == ENOENT) { config = {}; error.clear(); return true; }
        error = std::strerror(errno); return false;
    }
    std::string text;
    char buffer[1024];
    ssize_t count;
    while ((count = read(fd, buffer, sizeof(buffer))) > 0) {
        text.append(buffer, static_cast<size_t>(count));
        if (text.size() > kPostProcessingMaxBytes) { close(fd); error = "configuration exceeds 16 KiB"; return false; }
    }
    const int saved = errno;
    close(fd);
    if (count < 0) { error = std::strerror(saved); return false; }
    return parse_postprocessing(text, config, error);
}

bool write_postprocessing_file(const PostProcessingConfig& config, std::string& error) {
    // Check the existing leaf without following symlinks or opening FIFOs.
    int old = picoet_open_root_file(kPostProcessingFile, O_RDONLY, 0);
    if (old < 0 && errno != ENOENT) { error = std::strerror(errno); return false; }
    if (old >= 0) close(old);
    char temporary[] = "/data/local/tmp/.picoet-postprocessing.XXXXXX";
    const int fd = mkstemp(temporary);
    if (fd < 0) { error = std::strerror(errno); return false; }
    const std::string text = format_postprocessing(config);
    size_t offset = 0;
    bool ok = fchmod(fd, 0644) == 0;
    while (ok && offset < text.size()) {
        const ssize_t count = write(fd, text.data() + offset, text.size() - offset);
        if (count <= 0) ok = false;
        else offset += static_cast<size_t>(count);
    }
    if (ok) ok = fsync(fd) == 0;
    int saved = errno;
    if (close(fd) != 0 && ok) { ok = false; saved = errno; }
    if (ok && rename(temporary, kPostProcessingFile) != 0) { ok = false; saved = errno; }
    if (!ok) { unlink(temporary); error = std::strerror(saved); }
    return ok;
}
}
