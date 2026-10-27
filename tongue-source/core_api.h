#pragma once
#include "ptj_source.h"
#include "borrow_api.h"
extern "C" {
int core_ptj_start(const PTJConfig*, PTJSource**);
uint64_t core_ptj_session_id(const PTJSource*);
int core_ptj_submit_capture(PTJSource*, const uint8_t*, uint32_t, const PTJMeta*, uint64_t);
int core_ptj_get_stats(const PTJSource*, PTJStats*);
void core_ptj_stop(PTJSource*);
void core_ptj_destroy(PTJSource*);
const char* core_start_stage();
int core_start_errno();
}
