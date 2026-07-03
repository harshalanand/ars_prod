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
- ~~MBQ = 0 means no constraint at that grain (invariant 3). Do NOT apply 1.30× breach when MBQ=0.~~ **Superseded 2026-06-30** — see Recorded rules below.
- Hard-block (per-OPT mode, `rule_engine_per_opt.py`): for any sec-cap-applicable grid where `GH_<grid>=1`, an OPT is BLOCKED (status=SKIPPED) when ANY of the three empty-data triggers fires at its grain:
  1. grid extra value is NULL / empty / `'NA'` / `'NONE'` → SKIP_REASON `SEC_CAP_GRID_NULL[<grid>]`
  2. `<grid>_MBQ_ORIG = 0` (explicit zero) → SKIP_REASON `SEC_CAP_MBQ_ZERO[<grid>]`
  3. `<grid>_MBQ_ORIG IS NULL` → SKIP_REASON `SEC_CAP_NULL[<grid>]`
  Multiple triggers concatenate (e.g., `SEC_CAP_GRID_NULL[X],SEC_CAP_MBQ_ZERO[X]`).

## Invariants relevant here
- All six are relevant; this is the heart of the engine.

## Recorded rules
<!-- ars_flow appends dated bullets below. One rule per line. -->
- 2026-06-17 — `FNL_Q_REM` on alloc rows is the **live pool AFTER that OPT's draw** in per-OPT mode (`rule_engine_per_opt._run_band_per_opt` step 5f.1). Pre-band snapshot in `rule_engine_pandas` and the post-loop SQL recompute are both gated off when per-OPT mode is on. Read with `ALLOC_REMARKS`: `FNL_Q_REM==0` + `PAK_SZ_ROUND(...,short=stock=N)` = pool exhausted; `FNL_Q_REM>0` + `PAK_SZ_GATE(req=R,pak=P)` = pak rule fired. **Why:** Santosh debugging VAR_ART 1240059334001 — the legacy aggregate `FNL_Q − SUM(POOL_CONSUMED)` value could not distinguish those two cases. See also `listing.md`.
- 2026-06-30 — **Invariant 3 superseded for per-OPT mode.** Hard-block when the grid's empty-data triggers fire (any of: grid extra value is NULL/'NA'/'NONE'/empty, `<grid>_MBQ_ORIG = 0`, `<grid>_MBQ_ORIG IS NULL`) AND `sec_cap_applicable=True` AND `GH_<grid>=1`. SKIP_REASON tags: `SEC_CAP_GRID_NULL[<grid>]`, `SEC_CAP_MBQ_ZERO[<grid>]`, `SEC_CAP_NULL[<grid>]` (comma-joined when multiple trigger). Code: `rule_engine_per_opt.build_sec_cap_state` populates `state["hard_block"]`, `_evaluate_sec_cap_per_opt` checks it before the legacy `ceiling<=0` skip. **Why:** Santosh — OPTs at HB11/M_BOXER were shipping through MJ_M_YARN_02 despite sec_cap_applicable=True because `M_YARN_02='NA'` + `M_YARN_02_MBQ_ORIG=0` was being treated as "no constraint" (old invariant 3). Business intent: empty data = hold dispatch until upstream Grid Builder classifies. Spec: `docs/superpowers/specs/2026-06-30-sec-cap-empty-grid-hard-block-design.md`. Expected blast radius (session 20260629_154555_543): ~62k OPTs / ~669k SHIP units flip ALLOCATED → SKIPPED. Downstream consumers (pak align, HOLD, PEND_ALC, listing rebuild, MSA) are quantity-driven and benign.
