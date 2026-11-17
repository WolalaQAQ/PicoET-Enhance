#pragma once
#include "channel.h"
#include <cstdio>
#include <string>

namespace camera_service {
struct Owner {
    unsigned long long pid=0, birth=0, timestamp=0, command=0;
    char version[128]{}, owner[17]{}, state[32]{};
    int result=0;
};
inline bool idle_tracking(const std::string& dump) {
    if(dump.find("FT algorithm is not started")==std::string::npos ||
       dump.find("ET algorithm is not started")==std::string::npos) return false;
    size_t at=dump.find("There is ");
    while(at!=std::string::npos) {
        unsigned count=0;
        if(std::sscanf(dump.c_str()+at,"There is %u clients",&count)!=1 || count) return false;
        at=dump.find("There is ",at+9);
    }
    return true;
}
inline bool parse_owner(const std::string& text, Owner& out) {
    return std::sscanf(text.c_str(),
        "{\"schema_version\":1,\"version\":\"%127[^\"]\",\"pid\":%llu,\"start_ticks\":%llu,"
        "\"owner_id\":\"%16[0-9a-f]\",\"monotonic_ns\":%llu,\"state\":\"%31[^\"]\","
        "\"command_id\":%llu,\"command_result\":%d",
        out.version, &out.pid, &out.birth, out.owner, &out.timestamp, out.state,
        &out.command, &out.result)==8 && out.pid && out.birth && std::strlen(out.owner)==16;
}
inline bool attached(const ipc::Channel& c, uint64_t pid, uint64_t birth) {
    if (!ipc::valid_header(c) || c.client_pid!=pid || c.client_birth!=birth || c.request_session!=c.session ||
        !c.attach_ack || c.attach_ack_session!=c.session || !(c.client_nonce[0] || c.client_nonce[1])) return false;
    for (unsigned i=0;i<2;++i)
        if (c.attach_client_nonce[i]!=c.client_nonce[i] || c.attach_root_nonce[i]!=c.daemon_nonce[i] ||
            c.client_root_echo[i]!=c.daemon_nonce[i]) return false;
    return true;
}
inline bool stopped(const ipc::Channel& c, uint64_t pid, uint64_t birth) {
    if (!attached(c,pid,birth) || c.state!=ipc::Stopped || !c.stop_ack || c.stop_ack_session!=c.session) return false;
    for (unsigned i=0;i<2;++i) if(c.stop_client_nonce[i]!=c.client_nonce[i]) return false;
    for (const auto& image:c.images) {
        if(image.in_use || image.subscription || image.producer_lock || image.core.queue_in_use || image.core.running) return false;
        for(const auto& slot:image.slots) if(slot.state!=ipc::Free) return false;
    }
    for(const auto& label:c.labels) if(label.state!=ipc::Free) return false;
    for(const auto& record:c.eyes) if(record.state!=ipc::Free) return false;
    return !c.stock_subscription && !c.eye_subscription;
}
inline bool unattached_stopped(const ipc::Channel& c) {
    return ipc::valid_header(c) && !c.client_ready && !c.client_pid && c.stop_ack &&
        c.stop_ack_session==c.session && (c.state==ipc::Stopped || c.state==ipc::Failed);
}
}
