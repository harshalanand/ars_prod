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
