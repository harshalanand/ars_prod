---
title: Rule Engine (per_opt)
tags: [ars, rule-engine, allocation]
updated: 2026-07-17
---

# Rule Engine (per_opt)

Stage 5 of the [[Pipeline Overview|pipeline]]. Takes listed OPTs (each tagged `OPT_TYPE` + `OPT_MBQ`) from [[Listing]], explodes to size level, and runs a deterministic **per-OPT sequential waterfall** deciding ship vs hold per size per store, drawing from the finite [[MSA Stock Calculation|MSA]] pool + store hold reservations. Output → `ARS_ALLOC_WORKING` with full `ALLOC_REMARKS` + `SKIP_REASON` audit.

## per_opt is the ONLY engine (since 2026-07-10)
Three files (executed per `backend/app/docs/REMOVAL_PLAN_PER_OPT_ONLY.md`):
- **`rule_engine_per_opt.py`** — the band math (Stage C, the ONLY band).
- **`rule_engine_pandas.py`** — orchestration host only (loaders, `ProcessPoolExecutor`, writer thread, Stage D, write-back). `_run_band` deleted.
- **`rule_engine_new.py`** — shared **Stage A/B** SQL + `_stage_b_fill_cont`. Its sequential Stage C is dead pending phase-5.

`allocation_mode != 'per_opt'` → **HTTP 400** (`listing.py:598`). Env switch `ARS_PER_OPT_MODE`/`ARS_EXEC_ORDER` is gone (the per_opt module docstring still claims env activation — **stale**). Fixed order: **RL all rounds → TBC all rounds → TBL all rounds**.

> [!warning] Deleted — do not reference
> `rule_engine.py`, `rule_engine_parallel_python.py`, `rule_engine_parallel_sql.py`, `listing_allocator.py`, `usp_ars_allocate_majcat.sql`.

## Stage pipeline
| Stage             | Purpose                                                                                                                                                                  | In → Out                                     | Key fns                                  |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------- | ---------------------------------------- |
| **A** eligibility | E1–E7 gates → `LISTED_FLAG` + skip reason; assign `OPT_PRIORITY_TIER`/`_RANK`; propagate grid extras via `_collect_grid_extra_cols` (enforces invariant 4)               | `ARS_LISTING_WORKING` → `ARS_LISTED_OPT`     | `_stage_a_*`                             |
| **B** explosion   | one row per `listed OPT × VAR_ART × SZ`; join `ARS_MSA_VAR_ART`; fill `CONT`/`SZ_MBQ`/targets. Fresh/GRT: joins ONLY the matching typed MSA row, reads its baked `FNL_Q` | `ARS_LISTED_OPT` + MSA → `ARS_ALLOC_WORKING` | `_stage_b_explode`, `_stage_b_fill_cont` |
| **C** waterfall   | per-OPT band, fanned by MAJ_CAT over a process pool                                                                                                                      | mutates in-memory alloc slice                | `_run_band_per_opt`                      |
| **D** reflect     | SQL safety-net pak rounding; reflect to `ARS_LISTED_OPT`; final `ALLOC_STATUS`/`SKIP_REASON`; `*_REQ_REM` refund                                                         | `ARS_ALLOC_WORKING` finalized                | `_stage_d_*`, `_classify_alloc_reason`   |

**E1–E7 (Stage A):** E1 `LISTING=1` · E2 `ALLOC_FLAG=1` · E3 `OPT_TYPE≠MIX` · E4 `MSA_FNL_Q>0` · E5 `OPT_REQ_WH≥1` · E6 pool `FNL_Q_REM>0` · E7 size availability ≥ threshold.

> [!tip] Exhaustive walkthrough
> For the complete gate-by-gate trace (every condition, pass/fail path, per-size math, carried state, and a worked numeric example), see [[Allocation Waterfall (Step by Step)]].

## Band gate sequence (per OPT, `_run_band_per_opt`)
One OPT = `(WERKS, GEN_ART, CLR)`, all sizes together, sorted `OPT_PRIORITY_RANK → ST_RANK → WERKS`. **Every gate pre-validates before the pool is touched** — a blocked OPT consumes zero pool.
1. R07 live size-ratio [TBL] → 2. TBL MJ_REQ gate → 3. pak rounding + MJ_REQ_CAP/MBQ_CAP budget → 4. sec-cap pre-gate (`_evaluate_sec_cap_per_opt`, two-pass) → 5. **hold draw first** → 6. pool draw + live decrement → 7. SHIP/HOLD split + refund → 8. advance sec-cap `running` → 9. decrement `mbq_budget`/`mj_req_rem` → 10. write-back + remarks.

## Key formulas
```
OPT_MBQ    = ROUND(ACS_D + rate × ALC_D, 0)         ; rate = PER_OPT_SALE for new arts (age<15) else default
OPT_MBQ_WH = ROUND(ACS_D + rate × (ALC_D + HOLD_DAYS·[TBL]), 0)
SZ_MBQ     = ROUND(OPT_MBQ × CONT, 0)   with min-1 guard only when CONT>0 AND OPT_MBQ>0

# per-size need, round r
need_ship = max(r×SZ_MBQ − SZ_STK − SHIP_sofar, 0)
from_hold = min(opt_need, RL_HOLD_QTY_remaining)     # HOLD consumed FIRST, per (WERKS,VAR_ART,SZ)
take_pool = min(opt_need − from_hold, live_pool)      # then MSA pool
ship_ceiling   = ceil(need_ship/pak)×pak
raw_ship       = min(take_pool + from_hold, ship_ceiling)
effective_ship = floor(raw_ship/pak)×pak             # (or raw when combined supply < 1 pak: loose last-sliver drain)
pool_used      = max(effective_ship − from_hold, 0)  # excess pool refunded

# TBL: need_pool = SZ_MBQ_WH + (r−1)×SZ_MBQ − SZ_STK − POOL_CONSUMED ; SHIP & HOLD pak-aligned INDEPENDENTLY
# Cap guarantee: Σ SHIP/size ≤ I_ROD×SZ_MBQ − SZ_STK (+ up to pak−1 from rounding)
```

## Caps & gates
- **Growth** lives at MJ+grid only — the engine receives an already-scaled `MJ_REQ`. `MJ_MBQ_REV = MJ_MBQ × growth%`. **R09 headroom:** after each OPT_TYPE pass, `headroom = cap_pct×MJ_MBQ − MJ_STK_TTL − Σ_ALLOC`; if `< 0.5×ACS_D` (`ACS_SKIP_FACTOR`), upcoming rows skip `R09_HEADROOM_TRIVIAL` (TBL factor hardcoded 1.0).
- **TBL MJ_REQ sequential gate:** OPT passes iff `(tbl_mj_req_cap_pct/100)×MJ_REQ_REM[WERKS] ≥ 0.5×OPT_MBQ`; `MJ_REQ_REM` decremented per shipping OPT (no floor → one overshoot then next TBL fails). See [[ARS Glossary]].
- **MBQ_CAP overshoot (COMPLETE-only since 2026-07-18):** COMPLETE mode admits full `total_need` when `werks_cap ≥ 0.5×total_need` (bounded one-OPT overshoot), stamping a single `MBQ_CAP_OVERSHOOT`; the 5h decrement then drives the budget negative so later OPTs in the WERKS hard-skip. **SCALED (proportional round-then-shave) is disabled** — the scale block is now `elif mode == 'SCALED':` (dead-but-guarded). It was previously an unguarded fall-through, so a COMPLETE overshoot admit dropped into it and got silently rescaled to the cap, emitting a contradictory `OVERSHOOT`+`SCALE` double-stamp (repro: session `20260716_172414_619`, HB48/M_W_TRSR/1112108374/CRM shipped 10 not 15). See [[dispatch-complete-only]] and [[2026-07-18]].
- **[[Secondary-Grid Cap]]** — two-pass veto>override.
- **[[Contribution and CONT|CONT fallback]]** — `_stage_b_fill_cont` ladder; per_opt has no independent CONT path.

## Config / tunables (engine ↔ UI)
`allocation_mode` (per_opt only) · `size_threshold`/`min_size_count` (R07) · `pri_ct_check_rl/tbc` · `rl/tbc/tbl_mbq_cap_pct` (vs `MJ_MBQ_ORIG`) · `rl/tbc/tbl_mj_req_cap_pct` (vs `MJ_REQ`) · `mj_req_growth_pct` · `rl/tbc_dispatch_mode` **COMPLETE-only** (SCALED disabled + backend-coerced 2026-07-18) · `apply_sec_cap_in_normal` · `cont_fallback_mode` P4_UNIFORM|P3_FNL_Q · `alloc_type` FRESH|GRT · hold-suppression toggles · `parallel_workers` (2–8) · `use_writer_queue`. Per-run snapshot → `ARS_RUN_PARAMS_AUDIT` (incl. flat `SEC_CAP/sec_cap_mode` = STANDARD|MATRIX since 2026-07-18). Review session-wise (view + compare-two) in the Listing cockpit **Run Parameters (audit)** panel via `GET /listing/sessions/{sid}/run-params`. See [[Data Model]], [[2026-07-18]].

## Gotchas & invariants
- **`FNL_Q_REM` = live pool AFTER that OPT's draw**, not a pre-band snapshot. `POOL_CONSUMED` tracks pool-take; `from_hold` (`FROM_HOLD_QTY`) is separate to avoid double-subtraction.
- **SKIP_REASON preservation:** finalize preserves any pre-stamped reason before catch-alls; `_mark_opt_skip_sec_cap` **replaces** `ALLOC_REMARKS` (pool untouched) — deliberate, surprising.
- **Post-pass SQL sec-cap gate is a safety net** — only runs when the per-OPT spec build failed; don't delete.
- `_safe_int` wraps every remark cast so a stray NaN can't crash a MAJ_CAT subprocess.
- Skip taxonomy: `SEC_CAP_GRID_NULL/MBQ_ZERO/NULL[<grid>]`, `SKIP_PRI_BROKEN`, `POOL_EMPTY`, `MBQ_CAP_*`, `R07_SIZE_RATIO_LIVE`, `R09_HEADROOM_TRIVIAL`.

## Cross-links
[[MSA Stock Calculation]] (pool source) · [[Listing]] (input + growth) · [[Grid Builder]] / [[Secondary-Grid Cap]] · [[Pending Allocation and Hold]] (`RL_HOLD_QTY` drawn first; ship → PEND ledger) · [[Fresh-GRT Allocation]] · [[Contribution and CONT]].
