// Module-owned mailbox worker. The APK has no root or network permission.
#include "panel_protocol.h"
#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <fcntl.h>
#include <linux/memfd.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/xattr.h>
#include <unistd.h>

namespace {
struct Fd {
    int value;
    explicit Fd(int fd = -1) : value(fd) {}
    ~Fd() { if (value >= 0) close(value); }
    Fd(const Fd&) = delete;
    Fd& operator=(const Fd&) = delete;
};
bool owned(int fd, uid_t uid, bool directory) {
    struct stat st {};
    return fd >= 0 && fstat(fd, &st) == 0 && st.st_uid == uid &&
        (directory ? S_ISDIR(st.st_mode) : S_ISREG(st.st_mode) && st.st_nlink == 1);
}
bool write_all(int fd, const std::string& text) {
    size_t done = 0;
    while (done < text.size()) {
        const auto count = write(fd, text.data() + done, text.size() - done);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) return false;
        done += static_cast<size_t>(count);
    }
    return true;
}
int open_channel(uid_t uid) {
    Fd users(open("/data/user_de/0", O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    Fd app(openat(users.value, picoet_panel::package_name, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (!owned(app.value, uid, true)) return -1;
    Fd files(openat(app.value, "files", O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (!owned(files.value, uid, true)) return -1;
    const int channel = openat(files.value, "control", O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (owned(channel, uid, true)) return channel;
    if (channel >= 0) close(channel);
    return -1;
}
bool receive(int channel, uid_t uid, picoet_panel::Request& request) {
    Fd input(openat(channel, "request.txt", O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK));
    if (!owned(input.value, uid, false)) return false;
    std::string text;
    char buffer[1024];
    ssize_t count;
    while ((count = read(input.value, buffer, sizeof(buffer))) > 0) {
        text.append(buffer, static_cast<size_t>(count));
        if (text.size() > picoet_panel::max_request) break;
    }
    if (unlinkat(channel, "request.txt", 0) != 0) return false;
    return count == 0 && picoet_panel::parse(text, request);
}
bool publish(int channel, uid_t uid, const std::string& text) {
    const std::string name = "reply." + std::to_string(getpid()) + ".new";
    Fd out(openat(channel, name.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600));
    if (out.value < 0) return false;
    char label[256];
    const auto length = fgetxattr(channel, "security.selinux", label, sizeof(label));
    const bool ok = write_all(out.value, text) && fchown(out.value, uid, uid) == 0 &&
        fchmod(out.value, 0600) == 0 && length > 0 &&
        fsetxattr(out.value, "security.selinux", label, static_cast<size_t>(length), 0) == 0 &&
        renameat(channel, name.c_str(), channel, "reply.txt") == 0;
    if (!ok) unlinkat(channel, name.c_str(), 0);
    return ok;
}
bool execute(const std::string& module, const picoet_panel::Request& request, std::string& output) {
    Fd input(static_cast<int>(syscall(__NR_memfd_create, "picoet-panel-input", MFD_CLOEXEC)));
    int pipefd[2];
    if (input.value < 0 || !write_all(input.value, request.configuration) ||
        lseek(input.value, 0, SEEK_SET) < 0 || pipe2(pipefd, O_CLOEXEC) != 0) {
        output = "Cannot prepare module command"; return false;
    }
    Fd reader(pipefd[0]), writer(pipefd[1]);
    const pid_t child = fork();
    if (child == 0) {
        if (dup2(input.value, STDIN_FILENO) < 0 || dup2(writer.value, STDOUT_FILENO) < 0 || dup2(writer.value, STDERR_FILENO) < 0) _exit(126);
        unsetenv("ASH_STANDALONE");
        unsetenv("PICOET_CONTROL_LOCKED");
        if (request.action == "apply") {
            const std::string script = module + "/picoet.sh";
            execl("/system/bin/sh", "sh", script.c_str(), "reload-processing", nullptr);
        } else {
            const std::string helper = module + "/bin/picoet-config";
            execl(helper.c_str(), helper.c_str(), request.action.c_str(), nullptr);
        }
        _exit(127);
    }
    close(writer.value); writer.value = -1;
    if (child < 0) { output = "Cannot start module command"; return false; }
    char buffer[1024];
    ssize_t count;
    while ((count = read(reader.value, buffer, sizeof(buffer))) != 0) {
        if (count < 0) { if (errno == EINTR) continue; break; }
        const size_t available = 32768 - output.size();
        output.append(buffer, std::min(available, static_cast<size_t>(count)));
    }
    int status;
    while (waitpid(child, &status, 0) < 0) { if (errno != EINTR) { output += "\nCannot wait for module command"; return false; } }
    // Applying uses the existing bounded control/injection transaction. Closing
    // the panel never kills a privileged restart/injection halfway through.
    return WIFEXITED(status) && WEXITSTATUS(status) == 0;
}
}
int main(int argc, char** argv) {
    if (getuid() != 0 || argc != 3) return 2;
    const std::string module(argv[1]);
    if (module != "/data/adb/modules/picoet-enhance") return 2;
    char* end = nullptr;
    const unsigned long value = std::strtoul(argv[2], &end, 10);
    if (!end || *end || value < 10000 || value > 19999) return 2;
    const uid_t uid = static_cast<uid_t>(value);
    Fd lock(open("/data/adb/picoet-enhance/panel.lock", O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600));
    if (!owned(lock.value, 0, false) || flock(lock.value, LOCK_EX | LOCK_NB) != 0) return 6;
    while (access(module.c_str(), F_OK) == 0 && access((module + "/disable").c_str(), F_OK) != 0 && access((module + "/remove").c_str(), F_OK) != 0) {
        Fd channel(open_channel(uid));
        picoet_panel::Request request;
        if (channel.value >= 0 && receive(channel.value, uid, request)) {
            std::string output;
            const bool success = execute(module, request, output);
            if (!publish(channel.value, uid, picoet_panel::reply(request, success, output)))
                std::fprintf(stderr, "Cannot publish panel response: %s\n", std::strerror(errno));
        }
        usleep(500000);
    }
}
