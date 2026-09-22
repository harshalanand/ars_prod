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
- 2026-09-22 — **Central RDC pool (`ALC_RDC_CENTRAL_POOL`, seeded INACTIVE — default behaviour is unchanged).** `All RDCs` + switch ON = club stock across warehouses, allocate with unchanged rules, then Part 8.37 tags which warehouse ships each line. Key facts when working here: (1) **`RDC` is always the STORE's warehouse; `SRC_RDC` is the SOURCING one** — Own/Cross write no `SRC_RDC` and no split rows, and every reader uses `ISNULL(SRC_RDC, RDC)`, so there is no dual meaning and no backfill. (2) The mode flag is resolved **once** at run start and passed down — never call `rule_flag` per site, the cache TTL is 30 s and you would get a half-clubbed run. (3) In Part 3.55 the SELECT, GROUP BY and JOIN RDC fragments **must move together**; dropping only the join leaves the per-warehouse fan-out. (4) `VAR_COUNT` under clubbing needs a **two-level** aggregate or a size held at both warehouses counts twice. (5) `SRC_RDC`/`SRC_SPLIT_CNT` live in the `SELECT..INTO` in `rule_engine_new`, NOT an ALTER — `ARS_ALLOC_WORKING` is rebuilt every run. (6) `POOL_KEYS[0]` is the derived `POOL_RDC` (`'*'` when clubbing), not `RDC`. (7) **A hold is never split** (BR-RDC-12) — the hold tracker PK cannot hold two sources. (8) Approve must read `ARS_ALLOC_RDC_SPLIT`, not `ARS_ALLOC_HISTORY`, whose `SRC_RDC` is `'MULTI'` on a split line. Spec: `docs/superpowers/specs/2026-09-21-central-rdc-pool-allocation-brd-fsd-v1.5.md`. Full dossier: `frontend/public/docs/manual/listing.md`.
- 2026-09-22 — **Pre-existing, NOT fixed by the RDC work: the two hold-RDC resolvers disagree.** `alloc_pool._typed_hold_sql` prefers the hold row's own `RDC` over the store master; `msa_service._load_open_holds` / `_load_typed_hold` ignore it and read the master. For any store re-tagged after its hold was created they credit different warehouses — measured 2026-09-22 at **234,285 of 234,285 open hold rows (346,258 pcs, 412 stores)**. Deliberately left alone: aligning them would move the MSA deduction for every open hold, and check V13 exists to prove central pooling changed nothing else. Needs its own decision.
