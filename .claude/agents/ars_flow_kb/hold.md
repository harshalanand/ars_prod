# Hold Process & Manage — rules & details

## Source files
- `backend/app/api/v1/endpoints/hold_dashboard.py`
- `backend/app/services/parked_history.py`

## Concepts
- **Hold / Park**: rows pulled out of active allocation, retained in `parked_history` with reason + timestamp.
- **Manage**: review parked rows, optionally unpark back into the flow.

## Lifecycle
`active → parked (with reason) → unparked (back to active) | finalized`

`parked_history` is the source of truth for the lifecycle — every transition writes a row.

## Hold dashboard
- Backed by `hold_dashboard.py` endpoints.
- Pulls from `parked_history` joined with current allocation state.

## Invariants relevant here
- OPT uniqueness (invariant 1) — a parked OPT must not re-enter active flow with a different OPT_TYPE without an explicit transition row.
- Sec-cap grid extras must propagate (invariant 4) — unparked rows must carry FAB/MACRO_MVGR/MICRO_MVGR/M_VND_CD/RNG_SEG back to alloc.

## Recorded rules
<!-- ars_flow appends dated bullets below. One rule per line. -->
- 2026-07-08 — `ARS_NL_TBL_HOLD_TRACKING` carries NO session/run metadata: PK is `(WERKS, VAR_ART, SZ)` and the only non-qty columns are `RDC`, `OPT_STATUS`, dates, `IS_CLOSED` (DDL listing.py:2932-2949). Session attribution for revert lives entirely in `ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT` + `ARS_NL_TBL_HOLD_SNAPSHOT_SESSIONS` (parked_history.py:1302-1303). Why: any per-run ALLOC_TYPE tag cannot ride the tracking table without a schema change. see also: pend_alc.md
- 2026-10-14 — **Hold tracker gains `SRC_RDC`; `RDC` = store's own warehouse** (`ARS_NL_TBL_HOLD_TRACKING` + `_SNAPSHOT`; branch `for_merge`, not yet deployed/migrated). `SRC_RDC` = warehouse the hold is reserved at; readers use `COALESCE(NULLIF(SRC_RDC,''), NULLIF(RDC,''), store master)` (`hold_src_rdc_sql` / `msa_service._hold_src_expr` / `alloc_pool.hold_agg_sql`). Step B (`parked_history._apply_hold_tracking_from_history`): `RDC` = store master; `SRC_RDC` = split rows carrying the hold (history, else parked) → `ARS_ALLOC_HISTORY.SRC_RDC` unless MULTI → run RDC → store master. On MATCHED an open hold KEEPS its warehouse; a closed row being re-opened takes the session's `SRC_RDC`. Snapshot + revert carry `SRC_RDC`; clear / revise MSA re-sync key on hold `SRC_RDC`. PK is unchanged, so one hold row = one warehouse. Why: a hold is a claim on the stock of the warehouse that physically holds it, not the store's. Full rule: manual `hold.md`. see also: msa.md, pend_alc.md
