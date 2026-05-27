# Fallback Allocation — ARCHIVED REFERENCE

> **Status:** REMOVED on 2026-05-16. This document captures the design
> before deletion so the feature can be rebuilt later if needed.
>
> What stayed: `_apply_sec_grid_cap_pre_gate` (the main-pass pre-gate
> variant of the Secondary-grid cap) and its `apply_sec_cap_in_normal`
> toggle. Everything else described below was deleted.

## 1. Purpose

Fallback was an optional **post-main-pass reallocation phase** triggered
by `enable_fallback=True`. After the main RL→TBC→TBL waterfall finished
and pool was still in `#nre_pool`, it boosted MBQ budgets by
`fallback_growth_pct` (default 130 %), recomputed the REQ chain,
inserted newly-eligible OPTs into the alloc table, and ran the waterfall
again on the larger budget. F5 then demoted Primary grids one-by-one to
release further TBL stock that was being held back by tight grid caps.

The goal: surface stock that the main pass left in `FNL_Q_REM` because
of strict MBQ ceilings, without ever exceeding the physical pool.

## 2. End-to-end Phases (F0–F5)

| Phase | What it did                                                                    |
| ----- | ------------------------------------------------------------------------------ |
| **F0** | Add `*_PRE_FB` snapshot cols + `FALLBACK_LVL` on alloc, discover all grids.    |
| **F1** | Boost `OPT_MBQ`, `OPT_MBQ_WH`, and every grid `<prefix>_MBQ` by `growth_pct`. `boost_scope="exclude_mj"` left `MJ_MBQ` static; `"include_mj"` boosted it too. `OPT_MBQ_WH := OPT_MBQ` to force no-hold for the whole FB phase. |
| **F2** | Recompute `REQ → REM → H_REM → PRI_CT_REM → ALLOC_FLAG` via `_recompute_req_chain`. Boundary `REQ_REM > 0` — NO `ACS_D` threshold (different from main-pass `_revalidate_after_band`). |
| **F3** | Insert newly-eligible OPTs into alloc_table tagged `FALLBACK_LVL = 1`. |
| **F4** | Re-run RL→TBC→TBL waterfall with `fallback_active=True` (R09 uses unified `growth_pct` cap across all three OPT_TYPEs). Then `_apply_pack_round` + (if enabled) `_apply_sec_grid_cap` (post-trim variant). |
| **F5** | TBL-only grid-demotion loop: pop the highest-seq Primary grid from an *in-memory* primary set, recompute, insert newly-eligible TBL OPTs (tagged `FALLBACK_LVL = level`), re-run TBL waterfall, pack-round, sec-cap. Skip `seq=1` (MJ never demoted). Stop when no new OPTs surface. `ARS_GRID_BUILDER` on disk is NEVER mutated — crash-safe. |

## 3. Key Functions (all deleted)

All lived in `backend/app/services/rule_engine_new.py`.

| Function                             | Role |
| ------------------------------------ | ---- |
| `_run_fallback_new`                  | Orchestrator. Owns F0–F5 sequence and the audit dict. |
| `_fallback_ensure_schema`            | Idempotent DDL for `OPT_MBQ_PRE_FB`, `ST_RANK_FB`, `<prefix>_MBQ_PRE_FB`, `FALLBACK_LVL`. |
| `_discover_all_active_grids`         | Returns Primary + Secondary grids from `ARS_GRID_BUILDER` with `prefix`, `group`, `seq`, `mbq_col`, `req_col`, `stk_col` metadata. (KEPT — still used by main-pass sec-cap pre-gate.) |
| `_recompute_req_chain`               | Centralised REQ→REM→PRI_CT_REM→ALLOC_FLAG recompute after boost or demotion. |
| `_fallback_insert_eligible`          | INSERT newly-eligible OPT rows tagged with the current `FALLBACK_LVL`. |
| `_apply_sec_grid_cap` (post-trim)    | Per-grid trim of lowest-priority rows when fallback over-shipped. Cap = `max(130 %, growth_pct) × grid_MBQ`. Logs `SEC_CAP_HIT(...)` to `ALLOC_REMARKS`. |
| `_stage_c_apply_mj_req_cap`          | POST_FB MJ_REQ hard-cap. Σ SHIP per (WERKS, MAJ_CAT) may exceed original `MJ_REQ` ONLY when `boost_scope=include_mj` AND `MJ_REQ_PRE_FB ≥ 0.5 × ACS_D`. Else trim FB units lowest-priority first. |
| `_snapshot_ship_pre_fb` / `_tag_phase_for_fb_step` / `_finalize_fb_columns` | FB delta accounting: SHIP_QTY snapshots per phase + final `FB_SHIP_QTY = SHIP − main_final`. |

`_stage_c_waterfall` accepted `fallback_active: bool` and
`fallback_growth_pct: float` and `_check_r09_eligibility` consumed them
to switch R09's cap basis. Those parameters were also dropped.

## 4. Schema Columns (all dropped)

**working_table (`ARS_LISTING_WORKING`):**
- `OPT_MBQ_PRE_FB FLOAT` — `OPT_MBQ` snapshot before F1.
- `ST_RANK_FB INT` — fallback rank tiebreaker.
- `<prefix>_MBQ_PRE_FB FLOAT` — one per active grid (MJ, plus every active sub-grid).
- `MJ_REQ_PRE_FB FLOAT` — used by POST_FB MJ_REQ cap to decide if include_mj over-ship is allowed.

**alloc_table (`ARS_ALLOC_WORKING`):**
- `FALLBACK_LVL INT NULL DEFAULT 0` — 0 = main pass, 1 = F4, 2..N = F5 demotion level.

**Temp tables:**
- `#ship_pre_fb_orig` — per-row SHIP_QTY snapshot of final main-pass state. Joined back in `_finalize_fb_columns` to compute `FB_SHIP_QTY`.

## 5. Knobs (request → engine → SQL)

| Request field             | Engine arg              | Default       | Effect |
| ------------------------- | ----------------------- | ------------- | ------ |
| `enable_fallback`         | `enable_fallback`       | False         | Master gate. False = phase skipped entirely. |
| `static_growth_pct`       | `fallback_growth_pct`   | 130.0         | F1 multiplier (130 = 1.30×). Also the R09 cap during fallback. |
| `fallback_boost_scope`    | `fallback_boost_scope`  | `"exclude_mj"` | `include_mj` boosts MJ_MBQ too; `exclude_mj` leaves it. |
| `fallback_rerun_scope`    | `fallback_rerun_scope`  | `"under_allocated"` | Hint only — engine never branched on it. |
| `fallback_boost_mode`     | (frontend only)         | `"static"`    | Legacy UI; backend only uses `static_growth_pct`. |
| `str_tiers`               | (legacy)                | `"30:150,45:130,60:120,90:110"` | Legacy `listing_allocator._run_fallback`. |
| `apply_sec_cap_in_normal` | same                    | True          | **KEPT.** Toggles the main-pass sec-cap pre-gate. Inside fallback, sec-cap was always on regardless. |

## 6. Constants (dropped from `rule_engine_new.py` if fallback-only)

- `SEC_CAP_DEFAULT_PCT = 130.0` — KEPT (still used by `_apply_sec_grid_cap_pre_gate`).
- `SEC_CAP_PRE_OVERRIDE_OPT_REQ_PCT = 100.0` — KEPT (still used by pre-gate override).

## 7. Audit Dict

`_run_fallback_new` returned a dict that bubbled up to the API response
under `result["fallback_audit"]`:

```python
{
  "enabled": True,
  "growth_pct": 130.0,
  "boost_scope": "exclude_mj",
  "rerun_scope": "under_allocated",
  "apply_sec_cap": True,
  "levels_run": [1, 2, 3, ...],
  "newly_eligible_total": <int>,    # size-rows inserted across F3 + F5
  "fallback_ship_qty": <float>,     # Σ SHIP_QTY WHERE FALLBACK_LVL > 0
  "pack_round_units": <float>,      # Σ units shipped by _apply_pack_round across all FB steps
  "sec_cap_units_trimmed": <float>, # Σ units trimmed by _apply_sec_grid_cap across all FB steps
  "duration_sec": <float>,
}
```

## 8. Interactions Worth Recalling

1. **Pool sharing.** Fallback never restored blocked stock — it used
   the *same* `#nre_pool` left behind by the main waterfall. So FB only
   ever surfaced stock that physically remained, never recreated stock
   the main pass already shipped.
2. **MJ_REQ POST_FB cap.** Fixed the May-2026 HM24 over-allocation bug:
   when `include_mj` boosted `MJ_MBQ`, allowing Σ SHIP > original
   `MJ_REQ` was OK if `MJ_REQ_PRE_FB ≥ 0.5 × ACS_D` (meaningful demand
   existed); otherwise trim FB-added units from lowest-priority rows
   first. `_stage_c_apply_mj_req_cap` with `phase="POST_FB"` did this.
3. **Sec-cap variants.** Two distinct functions existed:
   - `_apply_sec_grid_cap_pre_gate` — main pass, pre-gate, **whole-OPT skip**, no pool restore. Logs `SEC_CAP_PRE_BLOCK` / `SEC_CAP_PRE_OVERRIDE`. **KEPT.**
   - `_apply_sec_grid_cap` — fallback only, **post-trim**, row-level partial trim. Logs `SEC_CAP_HIT(...)`. **DELETED.**
4. **R09 cap-basis switch.** In main pass, R09 used per-OPT-type caps
   (`rl_mbq_cap_pct`, `tbc_mbq_cap_pct`, `tbl_mbq_cap_pct` — TBL also
   removed today). In fallback it used the unified `fallback_growth_pct`
   for all three types.
5. **FALLBACK_LVL semantics.** 0 = main, 1 = F4 (first re-run on
   boosted MBQ), 2..N = F5 (one per demoted grid level). Audit reports
   show which rows came from which level.

## 9. Frontend State (deleted from `ListingPage.jsx`)

- `enableFallback` (bool)
- `boostMode` (`"str"` | `"static"`)
- `staticGrowth` (number, default 130)
- `fallbackBoostScope` (`"include_mj"` | `"exclude_mj"`)
- `fallbackRerunScope` (`"under_allocated"` | `"all"`)
- UI group "Fallback Allocation" — entire `<ParamGroup>` block removed.

`applySecCapInNormal` state + "Sec-grid Cap" toggle were KEPT and moved
under the main "Allocation Gates" group.

## 10. How to Rebuild

If you bring fallback back:

1. Reinstate the schema in `_fallback_ensure_schema` (cols above).
2. Reinstate `_run_fallback_new` and its helpers
   (`_recompute_req_chain`, `_fallback_insert_eligible`,
   `_apply_sec_grid_cap` post-trim, `_stage_c_apply_mj_req_cap`,
   `_snapshot_ship_pre_fb`, `_tag_phase_for_fb_step`,
   `_finalize_fb_columns`).
3. Restore `fallback_active` + `fallback_growth_pct` parameters on
   `_stage_c_waterfall` and `_check_r09_eligibility`.
4. Restore request fields on `AllocListingRequest` in `listing.py`
   (`enable_fallback`, `static_growth_pct`, `fallback_boost_scope`,
   `fallback_rerun_scope`, optional `fallback_boost_mode`).
5. Restore the `if enable_fallback:` branch in
   `run_listing_and_allocation` (rule_engine_new) and the equivalent
   block in `rule_engine_pandas.run_listing_and_allocation_pandas`.
6. Restore the UI controls in `ListingPage.jsx` (Fallback param group +
   payload mapping).
7. Restore the build-script sections in
   `backend/scripts/build_listing_alloc_doc.py` and
   `backend/scripts/build_part8_walkthrough.py`.

The git history before this commit contains the working implementation
verbatim — start there.
