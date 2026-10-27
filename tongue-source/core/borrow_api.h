#pragma once
#include "ptj_source.h"
// Internal core API only. No shared-channel, PTJStats or wire layout changes.
struct CoreBorrowStats {
    uint64_t admissions = 0, completions = 0, active = 0;
    uint64_t last_admitted_address = 0, last_completed_address = 0;
    uint64_t last_admitted_seq = 0, last_completed_seq = 0, last_subscription = 0;
};
#if defined(PTJ_CORE_TEST) && defined(_WIN32)
#define CORE_INTERNAL __declspec(dllexport)
#else
#define CORE_INTERNAL
#endif
extern "C" {
// Contiguous 400x400 input, immutable until queue_in_use==0 or stop/join returns.
// The caller holds the IPC PROCESSING slot throughout this lease. No pixel copy.
CORE_INTERNAL int core_ptj_submit_borrowed(PTJSource*, const uint8_t*, const PTJMeta*,
                                         uint64_t capture_copy_ns, uint64_t subscription);
CORE_INTERNAL int core_ptj_get_borrow_stats(const PTJSource*, CoreBorrowStats*);
#if defined(PTJ_CORE_TEST)
// Test DLL only: 1=worker acquired, 2=CRC input, 3=JPEG input, 4=RAW emit, 5=last read finished.
typedef void (*CoreTestHook)(uint32_t, const uint8_t*, void*);
CORE_INTERNAL void core_test_set_hook(CoreTestHook, void*);
#endif
}
