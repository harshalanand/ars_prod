# ARS Changelog

## 2026-06-01 — Grid-wide MBQ growth, cap pinning, sec-cap on Primary, audit log

### Allocation gate (multi-grid MBQ lift)
- **`listing.py`** — growth no longer scales only `MJ_MBQ`.  The "Use Default 100% (MBQ)" toggle, when OFF, now lifts every non-pivot grid's `_MBQ` (MJ + FAB + MICRO_MVGR + MACRO_MVGR + M_VND_CD + RNG_SEG + any custom grid where `ARS_GRID_BUILDER.pivot_only = 0`).  Multiplier reads `*_MBQ_ORIG` (snapshot taken first-run-wins) so re-runs never compound.  Lifted values are promoted into the live `*_MBQ` / `*_REQ` columns when growth ≠ 100; ORIG columns remain queryable for audit and for the MBQ-cap formulas.
- **Per-MAJ_CAT grain is automatic** — each grid's hierarchy already includes `WERKS, MAJ_CAT` (+ extras like FAB/MICRO_MVGR), so the lift partitions naturally by MAJ_CAT.

### MBQ-cap pinning (Decision 4-B)
- **`rule_engine_new.py :: _stage_c_apply_mbq_cap`** — anchors to `MJ_MBQ_ORIG` (the pre-growth budget) via `COALESCE(W.MJ_MBQ_ORIG, W.MJ_MBQ, 0)`.  Legacy deployments without the snapshot fall back to live `MJ_MBQ`.
- **`rule_engine_pandas.py :: _build_mbq_budget` / `_live_mbq_budget`** — same anchor, with column-presence fallback.
- **`listing.py` request schema** — added `rl_mbq_cap_pct`, `tbc_mbq_cap_pct`, `tbl_mbq_cap_pct` (default 100).  The orchestrator no longer hard-codes these to `mj_req_growth_pct` — they flow through from the UI.
- **`ListingPage.jsx`** — per-OPT_TYPE caps follow the PRI ≥ 100% toggle state.  When PRI is ON (strict listing gate), the cap = `mj_req_growth_pct` (default).  When PRI is OFF, an inline "Dispatch Cap %" slider surfaces next to the toggle and the user-set value becomes the cap.  TBL has no PRI toggle and always tracks growth.  Payload: `rl_mbq_cap_pct = priCheckRL ? mj_req_growth_pct : rlMbqCapPct`; same shape for TBC; TBL inherits growth unconditionally.

### Sec-cap (Decision 2 + Primary inclusion)
- **`rule_engine_new.py :: _apply_sec_grid_cap_pre_gate`** — accepts `growth_pct` and `include_primary`.  Effective cap = `max(SEC_CAP_DEFAULT_PCT, growth_pct)` — replace, not stack.  Budget reads `{prefix}_MBQ_ORIG` so growth doesn't compound through the cap.  When `include_primary=True` (callers set this when `apply_sec_cap_in_normal=True`), the MJ Primary grid is also gated.
- **`rule_engine_pandas.py`** — pre-gate call passes the two new arguments.

### Remarks clarity (why + numbers)
- **`SEC_CAP_PRE_BLOCK`** — `ALLOC_REMARKS` now includes a plain-English `why="..."` field plus `cap=NN%`, `before_ship`, `opt_ship`, `would_total`, `budget`, `exceeded_by`, `no_override=true`.  `SKIP_REASON` carries the cap %: `SEC_CAP_PRE_<grid>(cap=130%)`.
- **`SEC_CAP_PRE_OVERRIDE`** — the high-demand bypass remark is now self-documenting: `why="high-demand OPT bypassed sec-cap (OPT_REQ >= 100% x OPT_MBQ)"`.
- **`MBQ_CAP_HIT` / `MBQ_CAP_PARTIAL`** — added `why=...`, `anchor=MJ_MBQ_ORIG`, `cap=NN%`; renamed `cap_pct=` → `cap=NN%` for brevity.
- **`*_MJ_REQ_CAP_HIT` / `*_MJ_REQ_CAP_PARTIAL`** — same enrichment.

### Run-input audit log
- **`ARS_RUN_PARAMS_AUDIT`** — new table, created on first use of `/listing/generate`.  ~28 rows per run capturing every input parameter (LISTING, RANKING, ALLOCATION, SEC_CAP, FLAGS) with `RUN_ID`, `USER_ID`, `RUN_TS`.  Sample queries in `frontend/public/docs/process/variables.md §10`.

### Docs
- `frontend/public/docs/process/sec-cap.md` — replace-not-stack formula, MJ inclusion, worked example rebuilt around the pre-gate (running totals).
- `frontend/public/docs/process/listing-build.md` — multi-grid lift block (SQL + idempotency note).
- `frontend/public/docs/process/variables.md` — `MJ_MBQ_ORIG/REV` columns documented; FAQ updated; new §10 for `ARS_RUN_PARAMS_AUDIT`.
- `frontend/public/docs/process/overview.md` — sec-cap section rewritten with the new effective-cap formula and MJ-inclusion note.

---

## 2026-05-20 — Corrections

### Rule engine (allocation)
- **`rule_engine_pandas.py`** — fixed `defer_writes` / `use_pool` coupling bug: when the writer-queue flag was on but the inline path was used (MAJ_CATs < `PROCESS_POOL_MIN_MAJCATS` or `n_workers ≤ 1`), workers returned DataFrames that nobody wrote, so `alloc_rows = 0` and the MAJ_CAT queue row stayed `IN_PROGRESS`. `defer_writes` is now tied to `use_pool`.
- Worker exception logging now includes the full traceback (`type(e).__name__: e` + `traceback.format_exc()`); bare `KeyError` keys were uninformative.
- Worker tuple unpack is now forward-compatible (`len(args) >= 15`, trailing extras absorbed) — retired the 16-tuple TBL MBQ-cap path cleanly.
- New per-OPT_TYPE downward `MJ_REQ` caps applied after waterfall, before PAK rounding: `rl_mj_req_cap_pct`, `tbc_mj_req_cap_pct`, `tbl_mj_req_cap_pct` (default 100%). `mj_req_growth_pct` is informational (audit log only — `/listing-build` does the actual scaling on `ARS_LISTING_WORKING`).
- New SQL safety-net `_stage_d_apply_pak_sz_rounding` catches rows that escape the per-MAJ_CAT pak-alignment write-back. Half-up rule: `req ≥ 0.5*pak` rounds up; below that the row is gated to 0 with `SKIP_REASON = PAK_SZ_BELOW_HALF(pak=N)` and `POOL_CONSUMED` is refunded by the delta.
- `HOLD_QTY = 0` safety-net now preserves `r=1` partial dispatches (rows with `SHIP_QTY > 0` keep their warehouse-buffer hold).
- `SKIP_REASON` finalise step preserves the specific reasons stamped earlier by `PAK_SZ_*`, `*_MJ_REQ_GATE_*`, and `SEC_CAP_*` gates (the catch-all `NO_POOL_MSA` arm was stomping them).
- Split `NO_POOL_OR_DEMAND` into `NO_REQ` (demand-side: `SZ_REQ ≤ 0`) and `NO_POOL_MSA` (supply-side: pool empty). Also applied to `rule_engine_parallel_python.py` and `rule_engine_parallel_sql.py`.
- Secondary-grid cap (130% by default) now applies in the main pass via a temp `#nre_pool` built from current `FNL_Q_REM` (toggle: `apply_sec_cap_in_normal`).
- `_ensure_phase_reason_cols` and `_ensure_alloc_remarks_col` are now called right after Stage B (table re-creation), so per-MAJ_CAT write-backs see the columns.
- `_stage_d_apply_pak_sz_rounding` added to both parallel engines (`rule_engine_parallel_python.py`, `rule_engine_parallel_sql.py`) for parity with the pandas path.

### Listing allocator (legacy reference)
- **`listing_allocator.py`** — stripped retired fallback support: removed `enable_fallback`, `fallback_boost_mode`, `static_growth_pct`, `str_tiers` params and the `_apply_fallback_boost` + `_run_fallback` helpers (see `fallback_archived.md`). This module is the legacy reference allocator and not used by the live pipeline.
- `PAK_SZ` is now sanitised at source: `NULL` / `0` / negative → `1`. Bad master data was previously only neutralised inside the PAK gate expression, leaving the column itself junk for downstream consumers.
- Variant `STK_TTL` is clamped to `≥ 0` during enrichment so a negative balance doesn't show up as extra `SZ_REQ` demand.

### Approve / revert (parked sessions)
- **`parked_history.py :: approve_parked`** — pre-approve snapshot of `ARS_NL_TBL_HOLD_TRACKING` is now **scoped** to `(WERKS, VAR_ART, SZ)` keys touched by the session (resolved from `ARS_ALLOC_HISTORY`). Full-table copy was dominating approve latency on 50k–100k-row datasets.
- **`_revert_hold_tracking`** — DELETE is symmetrically scoped to the same touched keys; without this, the scoped snapshot would cause the "rows in live, not in snapshot" predicate to wipe rows that pre-existed and were never touched by this session.

### Contribution KPIs
- **`contrib.py :: _process_single_preset`** — removed the inner-query `ROUND(AVG(...), 2)` on `OP/CL_STK_*`, `SALE_*`, `GM_V`. Per-store-per-active-month values in lakhs (often < 0.005) were rounding to 0.00 and the SUM at company level was silently zero. Now uses `AVG(NULLIF(..., 0))` for stock cols and `AVG(CASE WHEN SALE_Q <> 0 THEN ... END)` for sales/GM so months with no sale are excluded from the average instead of dragging it down. Final 2-dp rounding happens once at the end of `_compute_kpis()`.

### Grid builder
- **`grid_builder.py`** — added per-grid `sec_cap_applicable` (BIT, default 0) and `sec_cap_pct` (FLOAT, NULL) columns to the grid-master table; backfilled OFF for existing rows (opt-in). Surfaced on the `GridCreate` / `GridUpdate` schemas and `_row_to_dict`.
- New post-build step: `[L-7 DAYS SALE-Q] *= LW_ACT_SL_GR_DGR` (default 1 if NULL/0). Mutates the column in place so the downstream `STR` (days-of-cover) calculation uses the grown value.
- `STK_TTL` sum across SLOCs is clamped to `≥ 0` — negative SLOC totals from adjustments/returns no longer inflate `OPT_REQ = MAX(0, OPT_MBQ - STK_TTL)`.
- `ARS_pend_alc` → `ARS_PEND_ALC` (V2 schema). PEND_ALC now joins on `(ST_CD, MAJ_CAT, GEN_ART_NUMBER, CLR, ARTICLE_NUMBER)` from `PA` directly when available; falls back to `vw_master_product` via `ARTICLE_NUMBER`. Filter mirrors MSA's `_load_ars_pending` (`IS_CLOSED = 0 AND PEND_QTY > 0`). SLOC settings row is now optional (KPI defaults to `'PEND'` when absent so it appears as a column without rolling into `STK_TTL`).

### OPT_TYPE classification
- **`opt_type_classification.md`** + the underlying `_classify_opt_type` rules — RL now also requires `MSA_FNL_Q > 0` (adequate-stock options with no fresh supply fall through to MIX). MIX guard for "store almost out AND warehouse empty" now also requires `RL_HOLD_QTY = 0` so open TBL holds keep an option in TBL/TBC rather than being killed. Reduced from a 10-branch to a 6-branch CASE.

### Dependencies
- `backend/requirements.txt` — added `alembic>=1.13.0` (new `backend/alembic/` migrations directory).

---

_See `git diff HEAD` for the full set of unstaged changes that this entry covers._
