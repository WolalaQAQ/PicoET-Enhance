#pragma once

inline const char* stock_capability_state(bool video, bool registered, bool armed, bool running,
                                         bool input_identity) {
    if (!video) return "inactive";
    if (!registered) return "jpeg_only_missing_main_payload_sink";
    if (!armed || !running) return "transport_unavailable";
    if (!input_identity) return "available_unassociated_missing_input_identity";
    return "association_ready_not_device_verified";
}
