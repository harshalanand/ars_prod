# Listing — rules & details

## Source files
- `backend/app/services/listing_allocator.py`
- `backend/app/services/listing_sessions.py`
- `backend/app/services/listing_job_manager.py`
- `backend/app/api/v1/endpoints/listing.py`

## Pipeline stages
`listing → listed → alloc`

All sec-cap grid columns (`FAB`, `MACRO_MVGR`, `MICRO_MVGR`, `M_VND_CD`, `RNG_SEG`) must survive every stage. Verify with a SELECT at each stage when debugging.

## Sessions
- `listing_sessions.py` tracks per-session state; check session lifecycle before assuming stale data is a bug.
- `listing_job_manager.py` handles async job orchestration.

## Invariants relevant here
- OPT uniqueness (invariant 1) — RL/TBC/TBL exclusive at OPT grain after listing
- Sec-cap grid extras must propagate (invariant 4) — most common bug surface

## Known import gotcha
- `listing.py` imports from `app.database.session` (NOT `app.core.database` — that was the old path).

## Recorded rules
<!-- ars_flow appends dated bullets below. One rule per line. -->
- 2026-06-17 — `FNL_Q_REM` in saved sessions (working/parked/history) is the **live pool AFTER each OPT's pool draw** in per-OPT mode. To audit a row's outcome: combine `FNL_Q_REM` with `ALLOC_REMARKS` — pool-exhausted shows `FNL_Q_REM=0` + `PAK_SZ_ROUND(...,short=stock=...)`; pak-gated shows `FNL_Q_REM>0` + `PAK_SZ_GATE(req=R,pak=P)`. See `merge_rules.md` for the full table and code touch-points.
