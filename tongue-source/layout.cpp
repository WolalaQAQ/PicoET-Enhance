// Compiled as an object only, for ABI evidence. Not linked into production artifacts.
#include "channel.h"
extern "C" const uint64_t ptj_ipc_layout[] = {
    2, sizeof(ipc::Channel), sizeof(ipc::Slot), sizeof(PTJMeta), sizeof(PTJStats),
    offsetof(ipc::Channel, state), offsetof(ipc::Channel, pending), offsetof(ipc::Channel, daemon_heartbeat),
    offsetof(ipc::Channel, client_pid), offsetof(ipc::Channel, producer), offsetof(ipc::Channel, core),
    offsetof(ipc::Channel, slots), offsetof(ipc::Slot, state), offsetof(ipc::Slot, meta),
    offsetof(ipc::Slot, copy_mono_ns), offsetof(ipc::Slot, subscription), offsetof(ipc::Slot, pixels),
    sizeof(PTJConfig), offsetof(ipc::Channel, daemon_nonce), offsetof(ipc::Channel, request_session),
    offsetof(ipc::Channel, client_nonce), offsetof(ipc::Channel, client_root_echo),
    offsetof(ipc::Channel, attach_ack), offsetof(ipc::Channel, attach_ack_session),
    offsetof(ipc::Channel, attach_client_nonce), offsetof(ipc::Channel, attach_root_nonce),
    offsetof(ipc::Channel, stop_ack_session), offsetof(ipc::Channel, stop_client_nonce)
};
