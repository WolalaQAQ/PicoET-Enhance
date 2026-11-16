#pragma once
#include "ptj_source.h"
#include "borrow_api.h"
#include "stock_record.h"
#include "capture_context.h"
extern "C" {
int core_ptj_start(const PTJConfig*, PTJSource**);
uint64_t core_ptj_session_id(const PTJSource*);
int core_ptj_submit_capture(PTJSource*, const uint8_t*, uint32_t, const PTJMeta*, uint64_t);
int core_ptj_get_stats(const PTJSource*, PTJStats*);
void core_ptj_stop(PTJSource*);
void core_ptj_destroy(PTJSource*);
const char* core_start_stage();
int core_start_errno();
uint64_t core_ptj_stock_subscription(const PTJSource*);
int core_ptj_submit_stock(PTJSource*, const PTJStockRecord*, uint64_t subscription);
int core_ptj_bind_capture_context(PTJSource*, uint64_t session, uint64_t service_generation);
}
