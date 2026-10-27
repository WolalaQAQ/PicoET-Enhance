#ifndef PTJ_SOURCE_H
#define PTJ_SOURCE_H
#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#if defined(PTJ_BUILDING_LIBRARY)
#define PTJ_API __declspec(dllexport)
#else
#define PTJ_API __declspec(dllimport)
#endif
#else
#define PTJ_API __attribute__((visibility("default")))
#endif
#ifdef __cplusplus
extern "C" {
#endif

enum { PTJ_WIDTH = 400, PTJ_HEIGHT = 400, PTJ_PIXELS = 160000,
       PTJ_JPEG = 1, PTJ_RAW = 2, PTJ_END = 4, PTJ_REPLAY = 8 };
typedef struct PTJHeader {
    char magic[4];
    uint16_t version, header_bytes;
    uint32_t payload_bytes, flags;
    uint64_t session_id, source_seq, sensor_ns, vendor_ns;
    uint64_t copy_mono_ns, encode_begin_ns, encode_end_ns, emit_mono_ns;
    uint64_t eye0_sensor_ns, eye1_sensor_ns, mouth_sensor_ns, raw_identity;
    uint32_t width, height, raw_crc32, quality;
} PTJHeader;

typedef struct PTJMeta {
    uint64_t session_id, source_seq, sensor_ns, vendor_ns;
    uint64_t eye0_sensor_ns, eye1_sensor_ns, mouth_sensor_ns, raw_identity;
    uint32_t flags; /* Only PTJ_REPLAY is accepted here. */
    uint32_t reserved;
} PTJMeta;

typedef struct PTJConfig {
    uint32_t struct_bytes;
    uint16_t port; /* 0 selects 45681. Bound to INADDR_ANY. */
    uint16_t reserved;
    uint32_t send_timeout_ms; /* 0 selects 100 ms, deadline per source/pair. */
    uint32_t reserved2;
    uint64_t session_id; /* 0 generates a new stream owner session. */
} PTJConfig;

/* Counters are monotonic, snapshots during activity are not transactional. */
#define PTJ_COUNTER_FIELDS(X) \
    X(submit_calls) X(enqueued) X(drop_no_subscriber) X(drop_duplicate) \
    X(drop_busy) X(drop_full) X(drop_stopped) X(drop_session) X(drop_invalid) \
    X(cancel_disconnect) X(cancel_stop) X(encoded) X(encode_errors) \
    X(sent_sources) X(sent_packets) X(sent_payload_bytes) X(send_errors) \
    X(send_timeouts) X(connections) X(pings) X(queue_peak) \
    X(copy_count) X(copy_total_ns) X(copy_max_ns) X(copy_last_ns) \
    X(encode_total_ns) X(encode_max_ns) X(last_sent_seq)
typedef struct PTJStats {
    uint64_t struct_bytes, session_id, running, connected, queue_in_use;
#define PTJ_DECLARE_FIELD(n) uint64_t n;
    PTJ_COUNTER_FIELDS(PTJ_DECLARE_FIELD)
#undef PTJ_DECLARE_FIELD
} PTJStats;

typedef struct PTJSource PTJSource;
enum PTJSubmitResult {
    PTJ_ACCEPTED = 0, PTJ_NO_SUBSCRIBER = 1, PTJ_DUPLICATE = 2,
    PTJ_BUSY = 3, PTJ_FULL = 4, PTJ_STOPPED = 5, PTJ_WRONG_SESSION = 6,
    PTJ_INVALID = 7
};
PTJ_API int ptj_start(const PTJConfig* config, PTJSource** out_source);
PTJ_API uint64_t ptj_session_id(const PTJSource* source);
/* pixels remain caller-owned; return releases them. Single/multiple producer safe.
 * stride >= 400, 400 rows. No allocation, encode, CRC or network in this call.
 * Source seq must increase within a session; attempted duplicate/older seq is dropped.
 */
PTJ_API int ptj_submit(PTJSource* source, const uint8_t* pixels,
                       uint32_t stride, const PTJMeta* meta);
PTJ_API int ptj_get_stats(const PTJSource* source, PTJStats* stats);
/* Idempotent, control-thread only. Joins both owned threads before returning.
 * May overlap submit; the handle remains valid until destroy. */
PTJ_API void ptj_stop(PTJSource* source);
/* Caller must first detach/finish all submit and stats callers. */
PTJ_API void ptj_destroy(PTJSource* source);

#ifdef __cplusplus
}
static_assert(sizeof(PTJHeader) == 128, "PTJ wire header must be 128 bytes");
static_assert(offsetof(PTJHeader, session_id) == 16, "wire alignment");
static_assert(offsetof(PTJHeader, width) == 112, "wire fields");
static_assert(sizeof(PTJMeta) == 72, "PTJMeta ABI");
static_assert(sizeof(PTJConfig) == 24, "PTJConfig ABI");
static_assert(sizeof(PTJStats) == 264, "PTJStats ABI");
#endif
#endif
