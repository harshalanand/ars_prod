# Variables Glossary

> Every knob, every column, every default. Use Ctrl+F to find a name.

All defaults below are pulled from code:
- API defaults: `backend/app/api/v1/endpoints/listing.py:67-135` (`GenerateRequest`)
- UI defaults: `frontend/src/pages/ListingPage.jsx:576-602`
- Setting fallbacks: `listing.py:_SETTING_DEFAULTS` (lines 288-308)
- Engine constants: `rule_engine_new.py:48` (`ACS_SKIP_FACTOR`), `rule_engine_new.py:3204` (`SEC_CAP_DEFAULT_PCT`)

---

## 1. UI knobs (Listing page top toolbar)

| Setting | UI default | API default | Effect when **increased** | Effect when **decreased** |
|---|---|---|---|---|
| `stock_threshold_pct` | `0.6` (60%) | `0.6` | More OPTs become **MIX** (harder to clear stock bar) → fewer RL. TBL size-ratio skip fires more often. | More OPTs become RL/TBC; size-coverage skips loosen. |
| `excess_multiplier` | `2.0` | `2.0` | Fewer rows flagged as `EXCESS_STK`. | More rows flagged as excess — more aggressive cap on over-stocked OPTs. |
| `hold_days` | `0` | `0` | Bigger warehouse buffer for TBL (`OPT_MBQ_WH` grows). | TBL ships exactly to plan with no buffer. |
| `age_threshold` | `15` (days) | `15` | More articles classed as "new" → uses `PER_OPT_SALE` (actual recent sales). | Fewer "new" articles; defaults to plan-based `AUTO_GEN_ART_SALE`. |
| `req_weight` | `0.4` | `0.4` | Store rank tilts toward requirement (high-need stores rank higher). | Tilts toward fill rate (high-performing stores rank higher). |
| `fill_weight` | `0.6` | `0.6` | Tilts toward fill rate. | Tilts toward requirement. |
| `apply_sec_cap_in_normal` | **true** | `true` | (default — already on). | Sec-cap disabled. Operators may see big over-allocations on `MJ_FAB`/`MJ_MICRO_MVGR`. |
| `default_acs_d` | `18` | `18` | OPTs with null `ACS_D` treated as higher density → harder to clear stock bar → more MIX. | More null-`ACS_D` rows become RL. |
| `min_size_count` | UI toggle `enableMinSize` defaults **off** → effective `0`; slider value `3` if enabled | `3` | Stricter TBL listing — variants need more sizes in stock. More TBL→MIX. | Loosens TBL listing. |
| `pri_ct_check_rl` | **false** | `false` | Strict 100% primary coverage gate on RL; cap pinned at 100% × `MJ_REQ`. Fewer RL OPTs ship. | RL gets headroom up to growth %. |
| `pri_ct_check_tbc` | **false** | `false` | Same for TBC. **TBL always enforces.** | TBC gets growth %. |
| `mj_req_growth_pct` | UI slider `110`; checkbox `mbqGrowthUseDefault` defaults **on** → effective `100` | `100.0` | All three per-OPT_TYPE MBQ caps raised together. Each store can ship `(growth−100)% × MJ_MBQ` above plan. | Closer to strict cap of `MJ_REQ`. |
| `rl_mj_req_cap_pct`  | `100.0` | `100.0` | RL can exceed `MJ_REQ` post-waterfall. `0` disables the gate. | Stricter — RL never ships beyond `cap_pct% × MJ_REQ`. |
| `tbc_mj_req_cap_pct` | `100.0` | `100.0` | Same for TBC. | Same. |
| `tbl_mj_req_cap_pct` | `100.0` | `100.0` | Same for TBL. | Same. |
| `allocation_mode` | `pandas` | `pandas` | n/a (production default). | `sequential` is the single-thread reference — slower, used to reproduce a bug. |
| `parallel_workers` | UI `4` (max `8`) | `DEFAULT_WORKERS=4`, `MAX_WORKERS=8` | Faster — but past 8 the GIL/tempdb contention dominates. | Slower, lower CPU, lower deadlock risk. |
| `use_writer_queue` | `true` | `true` | Single dedicated writer thread serialises DB writes → zero writer-writer deadlocks. | Each worker writes its own slice — faster but can deadlock at 8 workers. |
| `allow_multi_parked` | `false` | `false` | Multiple parked sessions coexist. | One-parked-at-a-time. |
| `mix_mode` | `st_maj_rng` | `st_maj_rng` | `st_maj_rng` = one MIX line per (WERKS, MAJ_CAT, RNG_SEG). `st_maj` collapses further. | `each` = keep every MIX row. |

> **Fix shipped 2026-06:** The previous discrepancy where `rl_mbq_cap_pct` / `tbc_mbq_cap_pct` / `tbl_mbq_cap_pct` were never sent in the payload and silently hard-coded to `mj_req_growth_pct` is **resolved**.  The three cap fields are still in the payload, but the UI no longer exposes three separate sliders — caps default to `mj_req_growth_pct` (Grid MBQ Growth %).  RL and TBC can be overridden per-OPT_TYPE via an inline "Dispatch Cap %" input that surfaces next to the `PRI ≥ 100%` toggle when that toggle is ON.  TBL always tracks growth.  All formulas anchor to `MJ_MBQ_ORIG` (Decision 4-B).
>
> **PRI ≥ 100% toggle action (per OPT_TYPE):**
>
> | Knob | OFF | ON (default = strict) |
> |---|---|---|
> | PRI ≥ 100% (RL)  | RL cap  = user-set "RL Dispatch Cap %" (50–200, default 110) | RL cap  = Grid MBQ Growth % |
> | PRI ≥ 100% (TBC) | TBC cap = user-set "TBC Dispatch Cap %" | TBC cap = Grid MBQ Growth % |
> | (no TBL toggle)  | — | TBL cap always = Grid MBQ Growth % |
>
> When the PRI toggle is ON, the OPT_TYPE also gets the strict listing gate (`PRI_CT% ≥ 100` required for the option to be listed) and the cap follows the default growth.  When OFF, the user explicitly sets the cap via the inline Dispatch Cap slider.

---

## 2. Engine constants (hard-coded)

| Constant | Value | File | Effect |
|---|---|---|---|
| `ACS_SKIP_FACTOR` | `0.5` | `rule_engine_new.py:48` | Used in store-broken gate (`MJ_REQ_REM < 0.5 × ACS_D`) and grid eligibility. |
| `SEC_CAP_DEFAULT_PCT` | `130.0` | `rule_engine_new.py:3204` | Secondary grid cap baseline.  Effective cap = `max(SEC_CAP_DEFAULT_PCT, mj_req_growth_pct)` (Decision 2 — replace, not stack).  Budget anchors to `*_MBQ_ORIG`.  When sec-cap toggle is ON, also applies to MJ (Primary). |
| `DEFAULT_WORKERS` | `4` | `rule_engine_pandas.py:63` | Default parallel pool size. |
| `MAX_WORKERS` | `8` | `rule_engine_pandas.py:65` | Hard ceiling on parallel workers. |

---

## 3. OPT-level columns (`ARS_LISTING_WORKING`)

| Column | Source | Plain English |
|---|---|---|
| `WERKS` | input | Store code. |
| `MAJ_CAT` | input | Major category. |
| `GEN_ART` | input | Generic article. |
| `CLR` | input | Colour code. |
| `OPT_TYPE` | computed | `RL` / `TBC` / `TBL` / `MIX`. |
| `OPT_PRIORITY_TIER` | computed | Tier within OPT_TYPE — focus stores first. |
| `OPT_PRIORITY_RANK` | computed | Final rank used for waterfall sort. |
| `ST_RANK` | computed | Store rank within MAJ_CAT (driven by `req_weight` + `fill_weight`). |
| `I_ROD` | from `ARS_CALC_ST_MAJ_CAT` | **Inventory Replenishment Over Days** — number of rounds in waterfall. |
| `IS_NEW` | computed | `1` if MSA-only (no existing stock at store). |
| `STK_TTL` | computed | Variant stock total at the store — negatives clamped to 0. |
| `ACS_D` | from `ARS_CALC_ST_MAJ_CAT.ACS_D` or `default_acs_d=18` | **Accessories Density** — one OPT display quantity. *NOT* a daily sale rate. |
| `MAX_DAILY_SALE` | computed | True daily velocity. |
| `ALC_D` | from `ARS_CALC_ST_MAJ_CAT.ALC_D` | Allocation cycle days. |
| `AGE` | from `MASTER_GEN_ART_AGE` | Days since article first listed. |
| `AUTO_GEN_ART_SALE` | from `MASTER_GEN_ART_SALE.SAL_PD` | Plan-of-record sales per day. |
| `PER_OPT_SALE` | computed | Actual sales per day (used when `AGE < age_threshold`). |
| `OPT_MBQ` | computed | Per-OPT MBQ ceiling: `(ACS_D or AUTO_GEN_ART_SALE × CLR_factor) × OPT_MBQ_FORMULA`. |
| `OPT_REQ` | `max(0, OPT_MBQ − STK_TTL)` | What's still needed to top up to OPT_MBQ. |
| `OPT_MBQ_WH` | `OPT_MBQ + (hold_days × ALC_D)` for TBL; else `= OPT_MBQ` | MBQ including warehouse hold buffer. |
| `OPT_REQ_WH` | `max(0, OPT_MBQ_WH − STK_TTL)` | "With hold" requirement. |
| `EXCESS_STK` | `max(0, STK_TTL − excess_multiplier × OPT_MBQ)` | Stock exceeding cap — flagged for redistribution. |
| `MJ_MBQ` | `SUM(OPT_MBQ)` per (WERKS, MAJ_CAT). Mutated to `MJ_MBQ_REV` when growth ≠ 100. | MAJ_CAT-level MBQ (live, post-growth). |
| `MJ_MBQ_ORIG` | snapshot of `MJ_MBQ` before any growth lift (first run wins) | Pre-growth MBQ. Anchor for `rl/tbc/tbl_mbq_cap_pct` formulas (Decision 4-B) and for sec-cap budget. |
| `MJ_MBQ_REV` | `ROUND(MJ_MBQ_ORIG × mj_req_growth_pct / 100, 0)` | Lifted MBQ.  Promoted into `MJ_MBQ` when growth ≠ 100. |
| `MJ_STK_TTL` | `SUM(STK_TTL)` per (WERKS, MAJ_CAT) | MAJ_CAT-level stock. |
| `MJ_REQ` | `max(0, MJ_MBQ − MJ_STK_TTL)`. Promoted from `MJ_REQ_REV` when growth ≠ 100. | Net MAJ_CAT requirement (live). |
| `MJ_REQ_ORIG` | snapshot of `MJ_REQ` pre-growth | Audit only. |
| `MJ_REQ_REV` | `max(0, MJ_MBQ_REV − MJ_STK_TTL)` | Lifted requirement. |
| `MJ_REQ_REM` | initialised to `MJ_REQ` | Live remaining budget — decremented after every band. |
| `{prefix}_MBQ_ORIG`, `{prefix}_MBQ_REV`, `{prefix}_REQ_ORIG`, `{prefix}_REQ_REV` | analogous to MJ_*, one set per non-pivot grid (FAB, MICRO_MVGR, MACRO_MVGR, M_VND_CD, RNG_SEG …) | Sec-grid lift columns (2026-06). |
| `RL_HOLD_QTY` | from `ARS_NL_TBL_HOLD_TRACKING` | Warehouse hold reserved for this OPT (for RL/TBC draw). |

---

## 4. MP / hierarchy columns

Must propagate `listing → listed → alloc`. Used as Secondary grid bucket keys.

| Column | Source | Used by grid |
|---|---|---|
| `FAB`        | product master | `MJ_FAB` |
| `MACRO_MVGR` | product master | `MJ_MACRO_MVGR` |
| `MICRO_MVGR` | product master | `MJ_MICRO_MVGR` |
| `M_VND_CD`   | product master | `MJ_M_VND_CD` |
| `RNG_SEG`    | product master | `MJ_RNG_SEG` (this one is **Primary**, not Secondary — MRP tier `E`/`V`/`P`/`SP`) |

---

## 5. Variant-level columns (`ARS_ALLOC_WORKING`)

| Column | Source | Plain English |
|---|---|---|
| `RDC` | from MSA | Warehouse / hub serving this store. |
| `VAR_ART` | from MSA | Variant article (specific size+colour SKU). |
| `SZ` | from MSA | Size. |
| `CONT` | computed | Size-mix proportion. Priority: store-level → CO-level → FNL_Q ratio → `1/var_count`. |
| `SZ_MBQ` | `ROUND(OPT_MBQ × CONT, 0)`, floor of 1 when both > 0 | Per-size MBQ. |
| `SZ_MBQ_WH` | `ROUND(OPT_MBQ_WH × CONT, 0)` | With hold buffer. |
| `SZ_STK` | from input | Variant stock at the store. |
| `SZ_REQ` | `max(0, SZ_MBQ − SZ_STK)` | Per-size requirement. |
| `SZ_REQ_WH` | `max(0, SZ_MBQ_WH − SZ_STK)` | With hold. |
| `STK_QTY` | from `ARS_MSA_VAR_ART` | Pool stock at the RDC. |
| `PEND_QTY` | from `ARS_MSA_VAR_ART` | Already-pending allocations against this VAR. |
| `FNL_Q` | `max(STK_QTY − PEND_QTY, 0)` | **The shared pool** — what's actually allocatable. |
| `FNL_Q_REM` | live | Pool remaining, snapshotted before every band. |
| `POOL_CONSUMED` | live | Cumulative pool draw by this row. |
| `SHIP_QTY` | computed | Final ship quantity. |
| `HOLD_QTY` | computed | Warehouse-side hold buffer (TBL only). |
| `FROM_HOLD_QTY` | computed | RL/TBC draws taken from `ARS_NL_TBL_HOLD_TRACKING`. |
| `ALLOC_QTY` | `= SHIP_QTY` (after PAK_SZ + caps) | The dispatch column. |
| `PAK_SZ` | from master | Carton/pack size. `SHIP_QTY` rounded to multiples. |
| `ALLOC_STATUS` | classifier | `ALLOCATED` / `PARTIAL` / `SKIPPED` / `INELIGIBLE` / `PENDING`. |
| `SKIP_REASON` | classifier | See catalogue below. |
| `ALLOC_WAVE` | trace | `<OPT>_R<round>` token. |
| `ALLOC_ROUND` | trace | Round number that shipped this row. |
| `ALLOC_REMARKS` | trace | Audit trail — every band-trace token. |
| `ALLOC_BATCH_ID` | session | Run / batch ID — matches `session_id`. |

---

## 6. `SKIP_REASON` catalogue

| Reason | When it fires |
|---|---|
| `NO_POOL_MSA`              | Pool was 0 — MSA had no available stock. |
| `NO_REQ`                   | `SZ_REQ = 0` — store already at target. |
| `ALREADY_STOCKED`          | `STK_TTL ≥ SZ_MBQ`. |
| `MJ_REQ_GATE_FAIL`         | Stage D MJ_REQ cap trimmed this row to 0. |
| `SEC_CAP_<grid>`           | 130% Secondary cap trimmed (e.g. `SEC_CAP_MJ_FAB`). |
| `PAK_SZ_BELOW_HALF`        | Rounded qty was less than half a pack. |
| `SKIP_PRI_BROKEN(pri=…)`   | Primary-grid coverage `PRI_CT% < 100`. |
| `SKIP_STORE_BROKEN(mj_rem=…)` | `MJ_REQ_REM < 0.5 × ACS_D`. |
| `SKIP_MSA_EXHAUSTED`       | Pool drained mid-waterfall. |
| `R07_SIZE_RATIO_LIVE`      | TBL size-ratio rule — too few sizes available. |

---

## 7. OLTP tables (approval workflow)

### `alloc_header`

One row per allocation session.

| Column | Purpose |
|---|---|
| `id`, `allocation_code` (unique) | Identity |
| `allocation_name` | Operator-friendly label |
| `allocation_type` | `INITIAL` / `REPLENISHMENT` / `TRANSFER` |
| `division_id` | FK to `retail_division` |
| `season` | Season label |
| `status` | `DRAFT` → `IN_PROGRESS` → `APPROVED` → `EXECUTED` / `CANCELLED` |
| `total_qty`, `total_stores`, `total_options` | Roll-up totals |
| `created_by`, `approved_by`, `executed_at` | Audit |

### `alloc_detail`

One row per `(store × variant × size)`.

| Column | Purpose |
|---|---|
| `allocation_id` (FK) | Links to header |
| `store_code` | Destination WERKS |
| `gen_article_id`, `variant_id`, `size_code`, `color_code` | What is being shipped |
| `allocated_qty` | Engine output |
| `override_qty` | Operator override |
| `final_qty` | `= override_qty if not null, else allocated_qty` |
| `store_grade` | Store tier letter |
| `allocation_basis` | `STOCK` / `SALES` / `RATIO` / `MANUAL` |

---

## 8. MSA tables

| Table | Grain | Key columns |
|---|---|---|
| `ARS_MSA_TOTAL`   | RDC × MAJ_CAT | `STK_TTL`, `PEND_TTL`, `FNL_TTL` |
| `ARS_MSA_GEN_ART` | RDC × MAJ_CAT × GEN_ART | `STK_QTY`, `PEND_QTY`, `FNL_Q` |
| `ARS_MSA_VAR_ART` | RDC × MAJ_CAT × GEN_ART × CLR × VAR_ART × SZ | `STK_QTY`, `PEND_QTY`, `FNL_Q` |

> `ST_CD` was renamed to `RDC` in April 2026. Same column.

---

## 9. Frequent-question quick lookups

| Question | Answer |
|---|---|
| What does "use default 100% (MBQ)" do? | Forces `mj_req_growth_pct = 100` AND `rl/tbc/tbl_mbq_cap_pct = 100`, ignoring all four slider values.  Strict to plan, no headroom on any grid. |
| Why is my run skipping a whole MAJ_CAT? | Look at the first OPT's `SKIP_REASON`. Likely `SKIP_STORE_BROKEN(mj_rem=…)` from prior OPT_TYPE eating the budget. |
| Why are sizes uneven across colours? | `CONT` size-mix. Adjust `Master_CONT_SZ` or store-level overrides. |
| Why is the same OPT shipping different qty between two runs with same input? | Almost always `PEND_QTY` changed — reconcile or check `MASTER_ALC_PEND`. |
| Where do I change the 130% sec cap? | Globally: `SEC_CAP_DEFAULT_PCT` in `rule_engine_new.py:3204`.  Per grid: `ARS_GRID_BUILDER.sec_cap_pct`.  Per-run via growth: set `mj_req_growth_pct > 130` (effective cap becomes the growth value). |
| Why doesn't my UI growth slider above 100 do anything? | Check `mbqGrowthUseDefault` toggle — when **on**, slider is overridden to 100. |
| How do I read a SEC_CAP_PRE_BLOCK remark? | New format includes `why="..."`, `grid=`, `cap=NN%`, `before_ship`, `opt_ship`, `would_total`, `budget`, `exceeded_by`.  The `why` field is the plain-English version; the rest are the actual numbers. |
| Where can I see what parameters were used for a given run? | `SELECT * FROM ARS_RUN_PARAMS_AUDIT WHERE RUN_ID = '...'` — one row per parameter, grouped (LISTING / ALLOCATION / SEC_CAP / FLAGS) with timestamp + user. |

---

## 10. ARS_RUN_PARAMS_AUDIT — per-run parameter log (2026-06)

Every call to `/listing/generate` inserts ~28 rows into `ARS_RUN_PARAMS_AUDIT` capturing every input parameter actually used.  One row per `(RUN_ID, PARAM_NAME)`.

| Column | Type | Notes |
|---|---|---|
| `AUDIT_ID`    | BIGINT IDENTITY | PK |
| `RUN_ID`      | NVARCHAR(128)  | Session ID or batch ID. |
| `SESSION_ID`  | NVARCHAR(128)  | The /listing/sessions row this run belongs to. |
| `USER_ID`     | NVARCHAR(128)  | The user who launched the run. |
| `RUN_TS`      | DATETIME2      | UTC, inserted on the run-start. |
| `PARAM_GROUP` | NVARCHAR(32)   | `LISTING` / `RANKING` / `ALLOCATION` / `SEC_CAP` / `FLAGS`. |
| `PARAM_NAME`  | NVARCHAR(64)   | e.g. `mj_req_growth_pct`, `rl_mbq_cap_pct`, `hold_days`. |
| `PARAM_VALUE` | NVARCHAR(512)  | String form — numerics & booleans stringified; lists JSON-encoded. |
| `SOURCE`      | NVARCHAR(16)   | `UI` / `DEFAULT` / `MAJ_CAT_OVERRIDE` (future). |

**Sample query — what cap % was used for the last 5 runs?**
```sql
SELECT TOP 50 RUN_TS, USER_ID, PARAM_NAME, PARAM_VALUE
FROM ARS_RUN_PARAMS_AUDIT
WHERE PARAM_NAME IN ('mj_req_growth_pct', 'rl_mbq_cap_pct',
                     'tbc_mbq_cap_pct', 'tbl_mbq_cap_pct',
                     'apply_sec_cap_in_normal')
ORDER BY RUN_TS DESC;
```

**Sample query — every parameter for one run:**
```sql
SELECT PARAM_GROUP, PARAM_NAME, PARAM_VALUE
FROM ARS_RUN_PARAMS_AUDIT
WHERE RUN_ID = :run_id
ORDER BY PARAM_GROUP, PARAM_NAME;
```
