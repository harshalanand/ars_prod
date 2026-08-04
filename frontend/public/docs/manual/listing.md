# Listing — ARS Manual

## BRD — Why this exists

The Listing module is where ARS decides **what each store should be offered and how much**, before a single unit is allocated. It fuses three worlds — MSA warehouse availability, the grid budgets (MBQ/REQ at every hierarchy level), and the store-to-RDC map — into one flat master table, `ARS_LISTING`. Every row is one option at one store: `(WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR)`.

Planners use it to review and tune a run before committing stock: they set the OPT_TYPE thresholds, growth headroom, per-type caps, and dispatch behavior, then read back how many options landed as RL / TBC / TBL / MIX and why. Developers use it as the contract that feeds the rule engine — the working table `ARS_LISTING_WORKING` is exactly what the allocator consumes.

Business outcome: a store gets fresh supply only where it is genuinely listed, has warehouse demand, has display capacity, and (for new listings) has enough size coverage — with over-shipping bounded at the MAJ_CAT+grid budget. This replaces the 20-machine Excel process with a single auditable pipeline.

Pipeline position: **MSA Stock Calc → Grid Builder → Merge Rules → LISTING (build + run cockpit) → rule engine allocation → Hold / Pending Allocation.** Listing is the staging + hand-off layer that turns MSA + grid outputs into per-option allocation instructions.

## FSD — How it works

The `/listing/generate` endpoint runs one long transaction in numbered Parts. It builds `ARS_LISTING` (identity + calculated columns), materializes eligibility into `ELIG_FLAG`, projects eligible rows into `ARS_LISTING_WORKING`, applies growth, stamps the grid-coverage flags, then hands `ARS_LISTING_WORKING` to the rule engine which writes `ARS_ALLOC_WORKING`.

### Inputs & outputs

| Direction | Table | Grain |
|---|---|---|
| Consumed | `ARS_MSA_GEN_ART` (`msa_table`) | RDC × MAJ_CAT × GEN_ART × CLR — MSA warehouse availability (`MSA_FNL_Q`) |
| Consumed | `ARS_GRID_MJ_GEN_ART` (`grid_table`) | grid stock/budget per option |
| Consumed | `Master_ALC_INPUT_ST_MASTER` | store → RDC mapping, store status |
| Consumed | `ARS_GRID_BUILDER`, `ARS_GRID_HIERARCHY` | active grids, `grid_group` (Primary/Secondary), `sec_cap_applicable`, `sec_cap_pct`, `pivot_only` |
| Consumed | `ARS_NL_TBL_HOLD_TRACKING` | prior-run NL hold → `RL_HOLD_QTY` (pool-scoped, FS-09) |
| Consumed | `ARS_SLOC_SETTINGS` | SLOC → FRESH/GRT pool membership (recompute `FNL_Q_EFF`) |
| Produced | `ARS_LISTING` | one row per option — full build with all calc columns |
| Produced | `ARS_LISTING_WORKING` (`FINAL_TABLE`) | eligible rows only (`ELIG_FLAG=1`) + growth + GH_/H_/PRI_CT% flags |
| Produced | `ARS_STORE_RANKING` | one row per (MAJ_CAT, WERKS) — store priority |
| Produced | `ARS_LISTED_OPT`, `ARS_ALLOC_WORKING` | rule-engine outputs (listed OPTs, per-VAR_ART×SZ allocation) |
| Produced | `ARS_LISTING_SESSIONS` + `logs/listing_sessions/<sid>.log` | run cockpit metadata + full log |

### Rules & invariants

- **OPT uniqueness (inv 1).** One option = `(WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR)` = exactly one `OPT_TYPE`. RL / TBC / TBL / L / MIX are mutually exclusive; the classification CASE is first-match-wins. Only **RL / TBC / TBL** are processed by the allocation waterfall — **L and MIX** fail the ELIG stock-gate and are never allocated (L = adequately stocked/no supply, report-only; MIX = nothing to send).
- **Growth at MJ+grid only (inv 2).** `mj_req_growth_pct` scales each grid's MBQ into `<prefix>_MBQ_REV`, promoted into the live `<prefix>_MBQ`/`<prefix>_REQ` only when ≠100. Applied at MJ and every non-pivot-only grid prefix — **never per OPT_TYPE**. The per-OPT_TYPE sliders (`rl/tbc/tbl_mbq_cap_pct`, `rl/tbc/tbl_mj_req_cap_pct`) are independent *downward* caps, not growth.
- **MBQ empty-data hard-block (inv 3, per-OPT mode).** Superseded the old "MBQ=0 = no constraint". For a sec-cap-applicable grid with `GH_<grid>=1`, an empty grid value (`NULL`/`'NA'`/`'NONE'`/empty) or `<grid>_MBQ_ORIG = 0`/`NULL` now BLOCKS dispatch (rule engine, `rule_engine_per_opt`). Owned by `rule_ars`.
- **Sec-cap propagation (inv 4).** `FAB`, `MACRO_MVGR`, `MICRO_MVGR`, `M_VND_CD`, `RNG_SEG` are in `_FINAL_KEEP_COLS` and must survive `listing → working → alloc`. Dropping any silently loses sec-cap grids.
- **ACS_D ≠ daily sale (inv 5).** `ACS_D` is accessories density (one OPT's display quantity) and is the base term in `OPT_MBQ`. Velocity uses `MAX_DAILY_SALE` / the `rate_expr` (L-7 daily, AUTO, PER_OPT), never `ACS_D`.
- **RNG_SEG = MRP tier (inv 6).** `RNG_SEG` ∈ {E, V, P, SP}. `MJ_RNG_SEG` grid is Primary; `MJ_FAB` / `MJ_MICRO_MVGR` are sec-cap examples.

### Formulas

OPT_TYPE classification (Part 3.6, first match wins; `threshold`=`stock_threshold_pct`, `default_acs`=`default_acs_d`):
```
g = threshold × NULLIF(ACS_D,0)|default_acs
MIX(a): STK_TTL < g          AND MSA_FNL_Q=0 AND RL_HOLD_QTY=0
L     : STK_TTL >= g         AND MSA_FNL_Q=0 AND RL_HOLD_QTY=0
RL    : (STK_TTL >= g        AND (MSA_FNL_Q>0 OR RL_HOLD_QTY>0))
        OR (STK_TTL <= 0     AND MSA_FNL_Q=0 AND RL_HOLD_QTY>0)   # sold-out, hold-only → release hold
TBC   : 0 < STK_TTL < g      AND (MSA_FNL_Q>0 OR RL_HOLD_QTY>0)
TBL   : STK_TTL <= 0         AND MSA_FNL_Q>0
ELSE  : MIX   (unreachable safety net — every combo above is covered)
# MIX(b) (poor color/size fill) REMOVED 2026-07-30 — size_threshold / min_size_count
#   now only drive the R07 TBL size-coverage gate, not classification.
# L added 2026-07-30: adequately-stocked option with no MSA and no hold. Report-only —
#   fails the ELIG stock-gate (MSA=0 AND HOLD=0) so it never enters ARS_ALLOC_WORKING;
#   NOT processed by the RL/TBC/TBL waterfall, same as MIX. Excluded from store ranking
#   (ARS_STORE_RANKING WHERE OPT_TYPE NOT IN ('MIX','L')). Kept as individual listing
#   lines (NOT aggregated like MIX in Part 3.7) for per-option visibility.
# TBL tightened to MSA-only 2026-07-30: a sold-out option carrying only a prior-run NL
#   hold (no MSA) now classifies RL (release the hold), not TBL.
```

OPT_STATUS — post-allocation verdict (Part 8.5, on `ARS_LISTING_WORKING`; reworked 2026-07-30).
Complements OPT_TYPE (pre-alloc decision) and ALLOC_STATUS (mechanical row result).
`post = STK_TTL + ALLOC_QTY`, same `g` as classification; first match wins:
```
1. OPT_TYPE=L    → L      (passthrough, never allocated)
2. OPT_TYPE=MIX  → MIX    (passthrough)
3. RL,  alloc=0  → WRL    (not allocated this run — Waiting RL; unconditional,
                           the old OP_MSA>50 test is dropped)
4. TBL, alloc=0  → UNQ    (not allocated this run — UNQualified; symmetric with
                           RL→WRL. Renamed from 'TBL' 2026-07-31 so the status
                           never collides with the OPT_TYPE value)
5. post < g      → MIX    (shipped but still under the display threshold;
                           also catches TBC that got 0)
6. TBL           → NL     (shipped and covered — Newly Listed)
7. TBC           → RL     (shipped to full cover — graduated live)
8. RL            → RL     (refilled)
9. else          → covered leftovers: alloc>0 → RL, untouched → L
```
Vocabulary: **NL / RL / WRL / UNQ / L / MIX** — there is NO 'NA'; every option gets a real status.
Reading: WRL and UNQ are the two "not allocated this run" states (live vs new);
MIX after alloc means "shipped something but the display is still under threshold".

Hold release for not-covered options (Part 8.55, runs right after the OPT_STATUS stamp):
an option that ends MIX after alloc keeps NO warehouse hold — reserving RDC stock for a
non-viable display strands it. `HOLD_QTY → 0` on **three copies**: `ARS_LISTING_WORKING`
(option grain, released qty audited in `HOLD_RELEASED_QTY`), `ARS_ALLOC_WORKING` +
this session's `ARS_ALLOC_PARKED` (size grain, `ROUND_HOLD` zeroed too,
`;HOLD_RELEASED_NOT_COVERED(n)` appended to `ALLOC_REMARKS`). The PARKED copy matters:
hold-tracking Step B reads `HOLD_QTY` from `ARS_ALLOC_HISTORY` (copied from PARKED at
Approve, and parking runs in Part 8.4 BEFORE 8.5) — zeroing only the working copies would
still commit the hold. Released pieces need no counter-booking: holds only materialise in
`ARS_NL_TBL_HOLD_TRACKING` at Approve, so the stock simply stays in the free pool for the
next run. Covered (NL) options keep their holds unchanged.
TBL_LISTED_DATE stamps GETDATE() on first TBL allocation (unchanged).

Demand (Part 4c). `rate_expr` = MAX(PER_OPT_SALE, L-7/7, AUTO_GEN_ART_SALE) when effective AGE < `age_threshold`, else MAX(L-7/7, AUTO). Effective AGE = 0 when `STK_TTL<=0 AND L-7<=0`.
```
OPT_MBQ    = ROUND(ACS_D + rate_expr × ALC_D, 0)
OPT_REQ    = MAX(0, OPT_MBQ − STK_TTL)
OPT_MBQ_WH = ROUND(ACS_D + rate_expr × (ALC_D + hold_days_if_TBL), 0)   # hold_days ONLY for OPT_TYPE='TBL'
OPT_REQ_WH = MAX(0, OPT_MBQ_WH − STK_TTL)
ART_EXCESS = MAX(0, STK_TTL − excess_multiplier × OPT_MBQ)              # 0 for MIX
```

Store ranking (`ARS_STORE_RANKING`, per MAJ_CAT, excludes MIX):
```
FILL_RATE = MJ_STK_TTL / MJ_MBQ          (0 when MJ_MBQ=0)
REQ_RANK  = DENSE_RANK by MJ_REQ ASC
FILL_RANK = DENSE_RANK by FILL_RATE DESC
W_SCORE   = ROUND(REQ_RANK × req_weight + FILL_RANK × fill_weight, 2)
ST_RANK   = ROW_NUMBER by W_SCORE DESC, WERKS ASC     (per MAJ_CAT — score order)
```
**Manual priority override** — `MANUAL_ST_PRIORITY` on `Master_ALC_INPUT_ST_MASTER` (keyed by `ST_CD` = `WERKS`) pins a store's `ST_RANK`. A positive integer `P` pins that store to `ST_RANK = P` inside **every** MAJ_CAT it is listed in; `NULL`/`0`/negative → ranked by `W_SCORE` as above. Non-manual stores keep their score order but are shifted into the smallest rank numbers **not** occupied by a pinned store (literal number + skip-used-slots, per MAJ_CAT). A store not listed in a given MAJ_CAT leaves its number free there. Two stores with the same `P` in one MAJ_CAT: the higher `W_SCORE` holds slot `P`, the other shifts to the next free slot (logged warning). `ARS_STORE_RANKING` carries `MANUAL_PRI` and a `MANUAL` bit for transparency, plus `AUTO_ST_RANK` (the score-only rank a store would get with **no** pins) and `RANK_DELTA = AUTO_ST_RANK − ST_RANK` (positive = pin promoted the store, negative = pushed down by another store's pin, 0 = unaffected) — audit columns only, never read by the engine. The column self-heals on each Listing run (also see migration `021_add_manual_st_priority.sql`).

`ST_RANK` is stamped back onto `ARS_LISTING`; the allocator uses it as a cross-MAJ_CAT tiebreaker on equal `OPT_PRIORITY_RANK` — so pinning a store also makes it win allocation ties (focus stores fill first).

Grid coverage flags (Part 7, on `ARS_LISTING_WORKING`, joined to `ARS_GRID_HIERARCHY` on MAJ_CAT):
```
GH_<grid> = raw hierarchy 0/1 (does this grid apply to the MAJ_CAT; 1 if MAJ_CAT absent)
GH_MJ     = 1 always (base grid)
H_<grid>  = (1 if <grid>_REQ > 0 else 0) × GH-value    (grid applies AND has requirement)
H_MJ      = 1 if MJ_REQ > 0 else 0
PRI_CT%   = ROUND(Σ H_<primary> / Σ GH_<primary> × 100, 1)   (0 when denom 0)
SEC_CT%   = same for Secondary grids
ALLOC_FLAG= 1 if PRI_CT% >= 100 else 0
```

Growth (Part 7, per grid prefix incl. MJ, skips pivot-only):
```
<prefix>_MBQ_ORIG = <prefix>_MBQ                       (snapshot once, first run wins)
<prefix>_MBQ_REV  = ROUND(<prefix>_MBQ_ORIG × growth_pct/100, 0)   (always reads ORIG → idempotent)
<prefix>_REQ_REV  = MAX(0, <prefix>_MBQ_REV − <prefix>_STK_TTL)
  (MJ uses MIN(WITH_EXC vs NO_EXC) against MJ_STK_TTL_ORIG / STK_TTL)
if growth_pct != 100:  <prefix>_MBQ ← _MBQ_REV ; <prefix>_REQ ← _REQ_REV   (promote to live)
```

### Validation gates — validate first, then create

1. **Validate listing before creating a working row (ELIG_FLAG, Part 6.6).** `ELIG_FLAG=1` is the conjunctive AND of all applicable gates; only `ELIG_FLAG=1` rows are projected into `ARS_LISTING_WORKING`. `ELIG_REASON` names the first failing gate (priority order):
   - `NOT_LISTED` — `LISTING ≠ 1`
   - `NO_STOCK` — `MSA_FNL_Q=0 AND RL_HOLD_QTY=0`
   - `NO_DEMAND` — `OPT_REQ_WH < 1`
   - `NO_DISPLAY` — `MJ_DISP_Q <= 0`
   - `TBL_SIZE_LT_60` — TBL only: `VAR_FNL_COUNT/VAR_COUNT < stock_threshold_pct` and no `min_size_count` rescue
2. **Validate primary-grid coverage before allocating (PRI_CT% gate).** `ALLOC_FLAG=1` requires `PRI_CT% >= 100`. TBL always enforces. RL/TBC enforce only when `pri_ct_check_rl` / `pri_ct_check_tbc` are true; when false, the option is admitted and the boosted MBQ-cap fallback path activates instead. (E2 in the allocator.)
3. **Validate MSA + demand + pool per OPT before shipping (allocator E1–E7).** Before each OPT: E1 `LISTING=1`, E2 `ALLOC_FLAG=1`, E3 `OPT_TYPE≠MIX`, E4 `MSA_FNL_Q>0`, E5 `OPT_REQ_WH>=1`, E6 pool `FNL_Q_REM>0`, E7 size availability ≥ threshold. Post-alloc: deduct `MSA_FNL_Q`, recalc `OPT_REQ_WH`, break the store when size coverage drops below threshold.

### Allocation hand-off (planner + developer view)

The rule engine consumes `ARS_LISTING_WORKING` and writes `ARS_ALLOC_WORKING`. `allocation_mode='per_opt'` is the **production default** (Jul 2026): it sets `ARS_PER_OPT_MODE=1` + `ARS_EXEC_ORDER` and dispatches through the pandas engine, which swaps in `rule_engine_per_opt._run_band_per_opt`. `sequential` is the single-thread SQL reference path. Deep engine internals are owned by **`rule_ars`** (`rule_engine_*.py`).

- **Priority + rounds.** OPTs ship in order RL → TBC → TBL; within an OPT, I_ROD rounds scale demand (`OPT_MBQ × N`). `ST_RANK` breaks cross-MAJ_CAT ties.
- **SHIP_QTY / HOLD_QTY.** SHIP_QTY is what dispatches now; HOLD_QTY is the current-run warehouse hold (distinct from `RL_HOLD_QTY`, the *prior-run* NL hold baked in at Part 3.54). `hold_days` (TBL-only) inflates `OPT_MBQ_WH` to fund that buffer.
- **werks_cap / dispatch mode.** Each OPT's need is clamped to the live `werks_cap` (`_live_mbq_budget`). When need exceeds cap, `COMPLETE` (the **only** RL/TBC mode since 2026-07-17) = all-or-skip: skip the whole OPT and leave the cap for the next OPT, **or** admit a bounded overshoot (ship the full need) when `werks_cap >= 0.5 × total_need`, which then drives the budget negative so later OPTs hard-skip. The former `SCALED` (proportional round-then-shave) mode is **disabled** — see Recorded rules (engine) 2026-07-17.
- **Sec-cap.** With `apply_sec_cap_in_normal=True` (only mode since 2026-06-16), the main pass enforces per grid where `sec_cap_applicable=1`: `budget = max(0, MBQ_ORIG × sec_cap_pct% − STK_TTL)`; breach blocks. Empty grid data hard-blocks (inv 3).
- **Pak rounding.** Ship quantities round to pack size; audit via `ALLOC_REMARKS`: `PAK_SZ_ROUND(...,short=stock=N)` with `FNL_Q_REM=0` = pool exhausted; `PAK_SZ_GATE(req=R,pak=P)` with `FNL_Q_REM>0` = pak rule fired.
- **Skip-reason taxonomy.** `ALLOC_STATUS` + `SKIP_REASON` on alloc rows; `ALLOC_REMARKS` on working rows. Sec-cap skips: `SEC_CAP_GRID_NULL[<grid>]`, `SEC_CAP_MBQ_ZERO[<grid>]`, `SEC_CAP_NULL[<grid>]` (comma-joined). `SKIP_PRI_BROKEN` = PRI_CT% gate. `FNL_Q_REM` on a row is the live pool **after** that OPT's draw (per-OPT mode).

### Run cockpit → engine param map

The screen tunables are `GenerateRequest` fields; they flow straight into the engine call:

| Screen tunable | Field | Effect |
|---|---|---|
| Stock % (OPT_TYPE) | `stock_threshold_pct` (0.6) | RL vs TBC/TBL STK-vs-ACS_D gate |
| Size Cov % | `size_threshold` (0.6) | R07 TBL size-coverage gate (MIX(b) removed 2026-07-30) |
| Min sizes | `min_size_count` (3) | R07 TBL size rescue |
| Excess × | `excess_multiplier` (2.0) | `ART_EXCESS` |
| Hold days | `hold_days` (0) | `OPT_MBQ_WH` for TBL only |
| Age threshold | `age_threshold` (15) | new-article rate branch |
| Req / Fill weight | `req_weight` 0.4 / `fill_weight` 0.6 | `W_SCORE` → `ST_RANK` |
| MJ growth % | `mj_req_growth_pct` (100) | grid MBQ/REQ lift |
| RL/TBC/TBL MBQ cap % | `rl/tbc/tbl_mbq_cap_pct` (100) | downward cap vs `MJ_MBQ_ORIG` |
| RL/TBC/TBL MJ_REQ cap % | `rl/tbc/tbl_mj_req_cap_pct` (100) | Σ SHIP_QTY cap vs MJ_REQ |
| PRI_CT gate RL/TBC | `pri_ct_check_rl/tbc` (false) | strict-gate vs fallback |
| Dispatch mode | `rl/tbc_dispatch_mode` (COMPLETE, **locked**) | over-cap behavior — SCALED disabled 2026-07-17 |
| Sec-cap | `apply_sec_cap_in_normal` (true) | main-pass sec-cap gate |
| Alloc mode | `allocation_mode` (per_opt) | engine path |
| Pool | `alloc_type` (FRESH\|GRT, **required**) | typed-pool `FNL_Q_EFF` recompute |

### Key columns

| Column | Meaning | Formula / source |
|---|---|---|
| `OPT_TYPE` | RL / TBC / TBL / MIX classification | Part 3.6 CASE |
| `ACS_D` | Accessories density (display qty) — **not** daily sale | MSA / `MANUAL_DENSITY` override |
| `MAX_DAILY_SALE` | Velocity for demand math | `rate_expr` (L-7, AUTO, PER_OPT) |
| `OPT_MBQ` / `OPT_REQ` | Option target / requirement | `ACS_D + rate×ALC_D` ; `MAX(0, MBQ−STK)` |
| `OPT_MBQ_WH` / `OPT_REQ_WH` | With-hold target / requirement | `+hold_days` on ALC_D for TBL |
| `RL_HOLD_QTY` | Prior-run NL hold (pool-scoped) | `ARS_NL_TBL_HOLD_TRACKING`, Part 3.54 |
| `ELIG_FLAG` / `ELIG_REASON` | Eligibility AND-gate + first failure | Part 6.6 |
| `ST_RANK` / `W_SCORE` | Store priority per MAJ_CAT | `ARS_STORE_RANKING` |
| `MANUAL_ST_PRIORITY` | Manual pin for `ST_RANK` (positive int) | `Master_ALC_INPUT_ST_MASTER` (per store) |
| `MANUAL_PRI` / `MANUAL` | Pin value / pinned-row flag | `ARS_STORE_RANKING` |
| `AUTO_ST_RANK` / `RANK_DELTA` | Score-only rank / `AUTO_ST_RANK − ST_RANK` (audit) | `ARS_STORE_RANKING` |
| `GH_<grid>` / `H_<grid>` | Grid applies / grid applies + has REQ | Part 7, joined on MAJ_CAT |
| `PRI_CT%` / `SEC_CT%` | Primary / secondary grid coverage % | Σ H / Σ GH × 100 |
| `ALLOC_FLAG` | Allocation-eligible | `PRI_CT% >= 100` |
| `<prefix>_MBQ_ORIG/_REV` | Pre/post-growth grid budget | growth lift, Part 7 |
| `FAB` `MACRO_MVGR` `MICRO_MVGR` `M_VND_CD` `RNG_SEG` | Sec-cap grid keys | `_FINAL_KEEP_COLS` — must propagate |

## Recorded rules
<!-- dated appendable bullets -->
- 2026-07-09 — Manual dossier created from source. OPT_TYPE CASE at `listing.py:1644`; OPT_MBQ/REQ at `2122`; store ranking at `2475`; ELIG_FLAG at `2587`; growth lift at `2748`; GH_/H_/PRI_CT% at `2864`; engine dispatch at `3031`. Allocator eligibility E1–E7 at `listing_allocator.py:16`.
- 2026-07-14 — **Manual store priority override** added to Part 6 store ranking (`listing.py`). `MANUAL_ST_PRIORITY` (int) on `Master_ALC_INPUT_ST_MASTER` pins `ST_RANK = P` per MAJ_CAT; non-manual stores keep score order and skip used slots (tally/`FreeSlots` CTE bounded by per-MAJ_CAT row count). Duplicate `P` in a MAJ_CAT → best `W_SCORE` holds the slot, other shifts + logged warning; non-positive → treated as auto. `ARS_STORE_RANKING` gains `MANUAL_PRI` + `MANUAL` bit. Column self-heals on run; migration `021_add_manual_st_priority.sql` adds it upfront so it's editable in the store-master table view before the first run.
- 2026-07-14 — `ARS_STORE_RANKING` gains **`AUTO_ST_RANK`** (score-only `ROW_NUMBER` over W_SCORE DESC, WERKS ASC per MAJ_CAT — ignores pins) and **`RANK_DELTA = AUTO_ST_RANK − ST_RANK`** so ops can review how far each pin moved a store. Display/audit only — engine still reads `ST_RANK`.
- 2026-07-18 — **Session-wise Run Parameters review + explicit sec-cap mode.** The existing per-run audit table `ARS_RUN_PARAMS_AUDIT` (one row per `PARAM_GROUP·PARAM_NAME·PARAM_VALUE`, keyed `RUN_ID`+`SESSION_ID`+`USER_ID`+`RUN_TS`) now also stamps a flat **`SEC_CAP / sec_cap_mode`** = `MATRIX` (growth-matrix enabled) or `STANDARD` (flat per-grid cap) so the sec-grid-cap fallback condition is reviewable without parsing the `growth_matrix` JSON blob (which is still stamped for band detail). New read endpoint **`GET /listing/sessions/{session_id}/run-params`** returns the latest run's params grouped (LISTING/RANKING/ALLOCATION/SEC_CAP/FLAGS) plus a `conditions` summary; for pre-2026-07-18 runs it derives `sec_cap_mode` from the `growth_matrix.enabled` flag (back-compat). New **Run Parameters** page (`RunParamsPage.jsx`, route `data-prep/listing/run-params`) — opened from a **Run Parameters** button in the Listing cockpit next to *View Logs* (passes the current session via `?session=`) — lets ops pick a session and click **Compare** to put a second run side-by-side with per-row diff highlighting, seeing every tunable + condition that produced a run. The audit now also snapshots the **grid configuration in force during the run** from `ARS_GRID_BUILDER` — a `GRIDS` group with one readable row per grid (`status · group · capping ON/off · cap% · wt · opt_sale`), plus numeric `active_grid_count`, `capping_grid_count`, `capping_grids`, and `cap_pct::<grid>` metrics, and a readable `SEC_CAP/matrix_bands` row. The Run Parameters page gained a **Trends** tab (`Compare | Trends` toggle): pick any params (grouped, `∙`=categorical) and plot them across the last N runs — numeric params overlaid as lines, categorical (sec_cap_mode, alloc_type, dispatch, grid status…) as step charts. A **View** selector switches the numeric plot between **Line / Bar / Area**, or **Table** (rows = runs, columns = each selected param — numeric and categorical together). Endpoints: `GET /listing/run-params/catalog` + `GET /listing/run-params/trend`. Read-only, no engine/math change; audit write stays best-effort (never blocks a run). **Also fixed a pre-existing bug: the audit INSERT was never committed** (`_run`/`run_sql` commits the CREATE, but the row `ac.execute(...)` had no `ac.commit()`, so SQLAlchemy rolled it back on connection close — `ARS_RUN_PARAMS_AUDIT` had been empty since inception). Only runs after this fix + a backend restart populate; historic sessions stay blank. Owned by `ars_flow`.
- 2026-07-18 — **Run Setup MAJ_CAT search: exact SSN/SEG/DIV group now surfaces.** `SearchSelect` bulk-select group matcher (`ListingPage.jsx`) used substring `.includes()` + `.slice(0,6)` over groups built in SEG→DIV→SUB_DIV→SSN order. A short query like `s` substring-matches many longer codes (`MENS`, `LADIES`, `SSNL`, …) that precede SSN groups, so the exact **SSN: S** bulk-select row was pushed past the 6-row cap and never rendered. Fix: rank matches exact → prefix → substring (shorter code wins ties) and raise the cap to 8, so an exact code match (any kind, e.g. SSN `S`) always shows first. `maj_cat_attr_map` source (`ARS_MSA_GEN_ART` SEG/DIV/SUB_DIV/SSN) was verified correct — the bug was purely UI ranking. Owned by `ars_flow`.
- 2026-07-18 — **Hold Control pre-fill driven by Run Pool** (`ListingPage.jsx`, cockpit). Selecting the pool now pre-sets the three "Skip hold" toggles so a selection is never missed: **FRESH ⇒ Skip hold UPC + GM checked, APP unchecked**; **GRT ⇒ all three checked** (UPC + APP + GM). Toggles remain fully editable after selection (pre-fill, not lock). Initial page state matches the default FRESH pool (UPC + GM pre-checked). Reminder: a checked "Skip hold — X" toggle means that segment's TBL rows ship like RL/TBC with **no** warehouse hold (`OPT_MBQ_WH = OPT_MBQ`, `HOLD_QTY = 0`); the payload still inverts these to the backend `apply_hold_seg_*` contract. Engine hold math unchanged. Owned by `ars_flow`.
- 2026-07-18 — **Run dates (information-only): `STOCK_CONSIDER_DT` + `PICKING_DT`.** Two date fields added to the Run Setup cockpit (`ListingPage.jsx`), **both mandatory** (generate guard blocks either empty): **Stock Consider Date** (UI defaults to **D-1 / yesterday**) and **Picking Date** (UI **blank by default** — no assumed date, user picks it consciously each run). `GenerateRequest` carries `stock_consider_dt: str` and `picking_dt: str` — both required, validated non-empty ISO `YYYY-MM-DD` by one shared validator (`_require_run_date`). Part 8.36 in `/generate` `ALTER`s `ARS_ALLOC_WORKING` to add both `DATE` columns (if missing) and `UPDATE`s every row with the two run constants — same pattern as the Part 8.35 ALLOC_TYPE stamp, and runs BEFORE Part 8.4 parking. The column-introspection copy in `parked_history.py` (`_reconcile_parked_columns` / `_reconcile_history_columns`) then carries both columns and values into `ARS_ALLOC_PARKED` and `ARS_ALLOC_HISTORY` with **no parking-code change**. Also written as two `LISTING` rows in `ARS_RUN_PARAMS_AUDIT`, and seeded into the data dictionary. **These dates never enter any stock/MSA/allocation math — pure traceability.** Owned by `ars_flow`.
- 2026-07-30 — **OPT_STATUS post-alloc classification reworked** (`listing.py` Part 8.5, writes `ARS_LISTING_WORKING`). Source: user's Excel formula, refined over 3 iterations. Final verdict set **NL / RL / WRL / TBL / L / MIX** (no 'NA' — CASE is exhaustive). Order: L/MIX passthrough → **not-allocated waiting states first**: RL+alloc=0 → **WRL**, TBL+alloc=0 → **TBL** (symmetric pair, per user; the Excel's `OP_MSA>50` WRL test was explicitly dropped) → under-threshold gate `STK_TTL+ALLOC_QTY < g` → MIX (now only judges rows that shipped-but-under-cover, plus TBC that got 0) → TBL→NL, TBC→RL (graduated), RL→RL → covered leftovers alloc>0?RL:L. Dry-run on live working table (18,152 opts / 24,228 qty / 1,891 hold — totals reconcile): TBL 11,367 (0 shipped), WRL 2,843 (0 shipped), MIX 1,801 (260 partial-shipped, qty 1,519, hold 133), NL 1,214 (qty 17,859, hold 1,758), RL 927 (qty 4,850). NL matches old logic exactly (cover math cross-validated). Dictionary entry `OPT_STATUS` added. **Part 8.55 added same day:** MIX-after-alloc options release their warehouse hold (HOLD_QTY→0 on working + alloc + session's PARKED; audit `HOLD_RELEASED_QTY` + `;HOLD_RELEASED_NOT_COVERED(n)` remark; released stock stays in the free pool — no counter-booking since holds commit to `ARS_NL_TBL_HOLD_TRACKING` only at Approve). Dry-run: 99 options / 133 pcs released; 784 covered NL options keep 1,758 pcs. Owned by `ars_flow`.
- 2026-07-31 — **In-run hold release + retry (Option B)** (`rule_engine_pandas.py` `_run_majcat_waterfall`, end of the RL→TBC→TBL loop). After TBL completes in a MAJ_CAT, options that shipped but did NOT reach cover (`STK_TTL + Σ SHIP < stock_threshold_pct × ACS_D` — same test as Part 8.5 MIX) release their `HOLD_QTY` **into the live `pool_dict`**, get the `;HOLD_RELEASED_NOT_COVERED(n)` remark, then **ONE extra TBL band pass** runs so still-hungry options (PARTIAL / POOL_EMPTY sizes — not SKIPPED-vetoed ones) consume the freed pieces in the SAME run, all gates re-applied (R07, MJ_REQ gate, MBQ caps, sec-cap, dispatch). Released OPTs can't re-take their own pieces (`POOL_CONSUMED` not decremented → `need_pool` stays spent; covered sizes have `need_ship=0`). Holds created BY the retry are not re-examined in-run — **Part 8.55 stays as the post-run safety net** (now catches only leftovers). Plumbing: `stock_threshold_pct`/`default_acs_d`/`tbl_hold_release_retry` params on `run_listing_and_allocation_pandas` → tuple elements 27–29 → worker → waterfall; `STK_TTL` added to `_select_working_cols` base (the `_STK_TTL` suffix scan missed the plain column); both listing.py call sites pass the request/saved values. Measured potential: 133/133 freed pcs had same-key takers (4,518 pcs unmet demand on 44 keys, 29 POOL_EMPTY rows). Owned by `ars_flow` / `rule_ars`.
- 2026-07-31 — **Parking mode + audit mode migrated into Business Rules.** `ALC_MULTI_PARKED` (was Settings → Application "Parking Mode" / AppSettings `listing.allow_multi_parked`) now drives the one-parked-at-a-time /generate guard; the legacy `/listing/parking-mode` GET/PUT endpoints read/write the rule (PUT also mirrors the old key for back-compat readers) so the old toggle stays in sync. `LST_AUDIT_ALL_TO_WORKING` (was app_settings.json `application.shift_all_to_working`) now gates Part 7 audit mode; legacy value is fallback-default only. Both seeded ACTIVE to match live values at migration (multi-parked=true, audit=true) — behavior-identical. `UPC_TRACKING_ENABLED` registered unwired (module master switch, wiring pending). Registry now 15 rules / 6 wired. **Module names follow the sidebar father menus** (Listing & Alloc 12 · Pending Allocation 1 · Data Management 1 · Reports 1). The old Settings → Application toggles (Parking Mode, audit mode) were **removed from SettingsPage.jsx** and replaced by a pointer card linking to Business Rules; parking state/handlers deleted, legacy `/listing/parking-mode` API kept (reads/writes the rule). Owned by `ars_flow`.
- 2026-08-01 — **PAK_SZ pack conversion reworked: 50% rule + MIN against pool, done per-OPT in-band** (`rule_engine_per_opt.py` 5e/5g; post-pass sweeps retired). **Rule:** `target = half-up pak conversion of the requirement` — remainder ≥ 0.5×pak rounds UP to the next whole pak, below it rounds DOWN (pak 6: 7→6, 8→6, 9→12, 3→6, 2→0); then **`SHIP = MIN(target, live pool)`** — the last partial pack drains as-is (req 3+, stock 3 → ship 3) and a round-up can never exceed stock. TBL HOLD follows the same MIN (was: full-pak-or-sliver-else-zero, which discarded mid-size remainders — target 12 with 8 left held nothing). RL/TBC lost the old `ceil` ship-ceiling + floor-to-pak pair. **Root cause fixed:** the blind post-pass nets `_apply_pak_sz_rounding_df` and `_stage_d_apply_pak_sz_rounding` rounded SHIP up with no pool reference; their only guard was "skip rows already carrying a PAK_SZ_ marker", but the engine stamps that marker only when target ≠ raw need — so rows whose need was already pak-aligned were re-rounded past the pool. Session 20260801_124142_212: 2 pool keys over-allocated / 4 phantom units (DW01·1241092244001 pool 3 → shipped 6; DH24·1240058807001 pool 3 → shipped 4). Both nets now gated off by `ENABLE_POST_PASS_PAK_ROUNDING = False` in `rule_engine_pandas.py` (flip to True only for legacy comparison). Owned by `rule_ars`.
- 2026-07-31 — **OPT_STATUS 'TBL' renamed to 'UNQ'** (Part 8.5). The not-allocated-this-run status for TBL options is now `UNQ` (UNQualified) so the post-alloc status vocabulary never collides with the pre-alloc OPT_TYPE value `TBL`. Final vocabulary: **NL / RL / WRL / UNQ / L / MIX**. Stored `ARS_LISTING_WORKING.OPT_STATUS` values updated in place; historical parked snapshots keep the old label. Dictionary + rule-algorithm texts synced. Owned by `ars_flow`.
- 2026-07-31 — **Business Rules registry (Settings → Business Rules) wired into listing.** New app-wide `ARS_BUSINESS_RULES` (+`_LOG`) table, service `app/services/business_rules.py` (`rule_flag`/`rule_value`, 30s cache, **inactive/missing = caller default = hardcoded behavior**, bounds-clamped numbers), API `/business-rules` (read any user, change SUPER_ADMIN, per-change audit log), page `BusinessRulesPage.jsx` at `/settings/business-rules`. 12 module-wise rules seeded; **4 wired in listing.py**: `LST_IROD_AMIX_FLOOR` (Part 3.5a floor value; inactive → NO floor), `LST_OPT_STATUS_STAMP` (gates Part 8.5+8.55), `ALC_HOLD_RELEASE_855` (gates Part 8.55), `ALC_TBL_HOLD_RETRY` (engine Option B master switch). Unwired rules show a "wiring pending" badge and are read-only. Seed self-heals metadata on seed-owned rows; IS_ACTIVE/RULE_VALUE are user state, never overwritten. Owned by `ars_flow`.
- 2026-07-31 — **Part 3.5a I_ROD A/A_MIX override changed to a TRUE floor (MAX semantics).** The old rule was an unconditional `SET I_ROD = 2 WHERE CLR IN ('A','A_MIX')` — it silently DOWNGRADED a maintained I_ROD of 3/4 (from `ARS_CALC_ST_ART`/`ST_MAJ_CAT`, which cascade earlier in 3.5a) back to 2, cutting the option's rounds, per-size cap (`SZ_MBQ × I_ROD`) and excess ceiling. Now `AND ISNULL(TRY_CAST(I_ROD AS FLOAT),0) < 2` — NULL/0/1 lifts to 2, maintained ≥2 is kept. Dictionary `I_ROD` entry updated. Owned by `ars_flow`.
- 2026-07-30 — **New `L` OPT_TYPE + TBL/RL rework** (`listing.py` Part 3.6 `_classify_opt_type`). The final `ELSE→MIX` region — `STK ≥ g AND MSA_FNL_Q=0` — is now split off explicitly: **`L`** = `STK≥g AND MSA=0 AND HOLD=0` (adequately stocked, no supply → nothing to replenish). **`L` is report-only and NOT allocated** — it fails the ELIG stock-gate (`MSA>0 OR HOLD>0`) so it never enters `ARS_ALLOC_WORKING`, and the RL/TBC/TBL waterfall (`OPT_TYPE_ORDER`) never touches it — same non-processing guarantee as MIX. Two supporting edits: store ranking excludes it (`ARS_STORE_RANKING` CTE `WHERE OPT_TYPE NOT IN ('MIX','L')`, `listing.py:~2586`); data dictionary OPT_TYPE description updated. `L` is **kept as individual listing lines** (not aggregated like MIX in Part 3.7) for per-option visibility. `ART_EXCESS` is computed for `L` (adequate/over-stock is meaningful). **Also reworked the sold-out region so no combo drops to MIX unintentionally:** **`TBL`** tightened to `STK≤0 AND MSA>0` (was `STK≤0 AND (MSA>0 OR HOLD>0)`), and **`RL`** extended with `OR (STK≤0 AND MSA=0 AND HOLD>0)` so a sold-out option carrying only a prior-run NL hold ships by releasing that hold instead of being orphaned. Net: all 12 (STK-region × MSA × HOLD) combos resolve to a real type; ELSE is an unreachable safety net. No rule-engine change required. Owned by `ars_flow`.
- 2026-07-30 — **MIX(b) removed from OPT_TYPE classification** (`listing.py` Part 3.6 `_classify_opt_type`). The second branch that forced `OPT_TYPE='MIX'` on poor color/size fill (`VAR_COUNT>0 AND (VAR_FNL_COUNT/VAR_COUNT < size_threshold OR VAR_FNL_COUNT < min_size_count)`) is deleted. Poor-coverage options now classify purely on stock + MSA (MIX(a) → RL → TBC → TBL → ELSE MIX), exactly like every other row. `size_threshold` / `min_size_count` are **not** orphaned — they still drive the downstream **R07 size-coverage gate** (`listing.py:~2749`) which skips poor-size **TBL** at allocation time, and the Size Cov %/Min sizes knobs still surface for that. Net effect: options that used to be parked as MIP-b now flow into the RL/TBC/TBL waterfall (and are size-gated there if TBL) instead of being suppressed at listing. No other math changed. Owned by `ars_flow`.

## Recorded rules (engine)

- **2026-07-10** — per_opt is the ONLY allocation engine. The Pandas/Sequential mode radios and `exec_order` were removed from the run cockpit; `allocation_mode != 'per_opt'` is rejected with HTTP 400. Why: identical inputs produced different allocations per engine (pandas 7,259 vs per_opt 9,824 on M_TEES_HS), and `/retry-failed` could silently inherit the wrong engine via a stale env switch. See `backend/app/docs/REMOVAL_PLAN_PER_OPT_ONLY.md`.

- **2026-07-17** — **SCALED dispatch disabled; COMPLETE-overshoot fall-through bug fixed.** In `rule_engine_per_opt._run_band_per_opt` the RL/TBC scale block (`werks_cap / total_need` round-then-shave) was an **unguarded fall-through** after the `if mode == 'COMPLETE':` block. A COMPLETE-mode overshoot admit (`werks_cap >= 0.5 × total_need`) stamps `MBQ_CAP_OVERSHOOT` and intends a FULL ship with a `# … Fall through` comment — but with no `continue`/guard, execution dropped straight into the scale block, which rescaled `opt_need` down to the cap and stamped `MBQ_CAP_SCALE`. Net effect: contradictory double-stamp (`OVERSHOOT` + `SCALE`) and the OPT shipped the **scaled** qty, not the full need. Repro: session `20260716_172414_619`, HB48 / M_W_TRSR / 1112108374 / CRM (RL) — need=15, cap=10, remark carried both stamps and shipped 10 (scaled) instead of 15 (overshoot). Fix: scale block is now `elif mode == 'SCALED':`, making COMPLETE and SCALED mutually exclusive; COMPLETE-overshoot ships the full need with a single `MBQ_CAP_OVERSHOOT` stamp. Separately, `SCALED` is now **disabled end-to-end**: the run-cockpit radio (`ListingPage.jsx`) still **shows** the SCALED option but renders it greyed-out and non-selectable (visible-but-disabled), stale saved settings normalize to COMPLETE on load, and `GenerateRequest._force_complete_dispatch` (a Pydantic `field_validator` on `rl/tbc_dispatch_mode`) coerces any incoming value — including direct-API `SCALED` — to COMPLETE. The scale block remains in the engine as dead-but-guarded code for reference. Owned by `rule_ars`.

- **2026-07-13** — Sec-cap hard-block now takes strict precedence over the Primary overshoot admit. `_evaluate_sec_cap_per_opt` was split into two passes: Pass 1 scans **every** applicable grid (same `GH_<grid>` guard — a `GH=0` grid still cannot veto) for hard-block reasons and, if any grid vetoes, blocks the OPT immediately with the combined `hard_block_reasons`; Pass 2 runs the unchanged Primary-first breach/overshoot logic only when Pass 1 finds no veto. Invariant: **veto > override**. Why: an OPT that breached a Primary grid (e.g. `MJ`) by an overshoot-eligible margin was admitted before the loop reached a downstream grid (`MJ_M_YARN_02`, `MBQ_ORIG=0` on the `OD` grain) that should have hard-blocked it — session `20260707_170050_291`, HO10/L_JEANS/1121112392/D_GRY shipped 16+3 while its RL/TBC siblings on the same grain were correctly skipped. Vetoed OPTs leave `participating` empty, so `running` is never advanced. Tests: `backend/tests/test_evaluate_sec_cap_per_opt_precedence.py`. Owned by `rule_ars`.
