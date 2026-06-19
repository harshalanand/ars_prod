# Merge Rules — rules & details

## Source files
- `backend/app/api/v1/endpoints/merge_rules.py`
- Rule engine family (deep internals → defer to `rule_ars` specialist):
  - `backend/app/services/rule_engine.py` (legacy)
  - `backend/app/services/rule_engine_new.py` (current main)
  - `backend/app/services/rule_engine_pandas.py`
  - `backend/app/services/rule_engine_parallel_python.py`
  - `backend/app/services/rule_engine_parallel_sql.py`
  - `backend/app/services/rule_engine_per_opt.py`

## OPT_TYPE classification
Three mutually exclusive types at OPT grain (invariant 1):
- **RL** — Regular Listed
- **TBC** — To Be Continued
- **TBL** — To Be Listed

One OPT = `(WERKS, MAJ_CAT, GEN_ART, CLR)` = exactly one type.

## Cap / growth application
- Cap and growth % apply at `MAJ_CAT` + grid level (invariant 2).
- **Never per OPT_TYPE** — applies to both main and fallback paths.

## Sec-cap behavior
- MBQ = 0 means no constraint at that grain (invariant 3). Do NOT apply 1.30× breach when MBQ=0.

## Invariants relevant here
- All six are relevant; this is the heart of the engine.

## Recorded rules
<!-- ars_flow appends dated bullets below. One rule per line. -->
- 2026-06-17 — `FNL_Q_REM` on alloc rows is the **live pool AFTER that OPT's draw** in per-OPT mode (`rule_engine_per_opt._run_band_per_opt` step 5f.1). Pre-band snapshot in `rule_engine_pandas` and the post-loop SQL recompute are both gated off when per-OPT mode is on. Read with `ALLOC_REMARKS`: `FNL_Q_REM==0` + `PAK_SZ_ROUND(...,short=stock=N)` = pool exhausted; `FNL_Q_REM>0` + `PAK_SZ_GATE(req=R,pak=P)` = pak rule fired. **Why:** Santosh debugging VAR_ART 1240059334001 — the legacy aggregate `FNL_Q − SUM(POOL_CONSUMED)` value could not distinguish those two cases. See also `listing.md`.
