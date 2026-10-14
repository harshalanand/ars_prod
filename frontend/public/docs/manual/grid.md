# Grid Builder — ARS Manual

## BRD — Why this exists

Grid Builder turns per-store, per-category planning parameters into the
**allocation grids** the rule engine ships against. A grid is a pivot of stock
(`STK_TTL`) plus a computed minimum-buy quantity (`MBQ`), an option count
(`OPT_CNT`), and an effective display quantity (`DISP_Q`) at a chosen grouping
grain. The engine reads these grids to decide how much of each option each
store should receive.

The planner/admin defines grids in the Grid Builder UI. There is one **Primary
grid** — `MJ_RNG_SEG` (MAJ_CAT × MRP tier) — which sets the baseline budget per
category-tier, and any number of **secondary "sec-cap" grids** (e.g. `MJ_FAB`,
`MJ_MICRO_MVGR`) that impose additional ceilings along a second dimension so no
single fabric/vendor/micro-segment floods a store.

Before grids can be built, a pre-grid cascade computes the sale-cover and
per-day-sale inputs from the master tables. Grid Builder never mutates the
master input tables — all work lands in `ARS_CALC_ST_MAJ_CAT` /
`ARS_CALC_ST_ART` (calc tables) and the per-grid output tables. This is where
growth factors, contribution scaling, and the stock clamp are applied, so
accuracy here directly sizes every store order.

It sits between MSA (which produced free stock) and Listing/Allocation (which
consumes the grids). A grid with a dropped dimension or a mis-scaled MBQ
silently distorts every allocation that reads it.

## FSD — How it works

### Inputs & outputs

**Consumed (Rep_data DB):**

| Source | Grain | Role |
|---|---|---|
| `Master_ALC_INPUT_CO_MAJ_CAT` | MAJ_CAT | Company-level category params (base layer). |
| `Master_ALC_INPUT_ST_MAJ_CAT` | ST_CD, MAJ_CAT | Store-level overrides (overlay). The **only** source of `DISP_Q` (display) and CM/NM sale (`SAL_PD`) — the CO table has neither. |
| `Master_ALC_INPUT_ST_MASTER` | ST_CD | INT_DAYS, PRD_DAYS, SL_CVR; store universe. |
| `Master_ALC_INPUT_CO_ART` / `Master_ALC_INPUT_ST_ART` | article | Article-level params (mirror cascade). |
| `MASTER_GEN_ART_SALE` | ST_CD, GEN_ART | CM/NM sale quantities; in-place SAL_PD. |
| `Master_CONT_<dim>` | dim | Contribution (`CONT`) share per grain. |
| stock source (via `_build_and_run_grid`) | SLOC-level | Pivoted into SLOC columns + `STK_TTL`. |

**Produced (Rep_data DB):**

| Table | Grain | Notes |
|---|---|---|
| `ARS_CALC_ST_MAJ_CAT` | ST_CD × MAJ_CAT | Cascaded params + `ALC_D`, `SAL_PD`, `SALE_COVER_SRC`. |
| `ARS_CALC_ST_ART` | ST_CD × GEN_ART [× CLR] | Article-level mirror. |
| per-grid output tables (e.g. `ARS_GRID_MJ_RNG_SEG`) | hierarchy cols + SLOC cols + STK_TTL | Plus `MBQ`, `OPT_CNT`, `DISP_Q`, `STR`. |
| `ARS_GRID_HIERARCHY` | column registry | ADD-ONLY registry of grid columns. |
| `ARS_SEC_CAP_GROWTH_MATRIX` / `_CFG` | cont% band | Optional cont%-driven growth override. |

### Rules & invariants

- **Primary vs sec-cap.** `MJ_RNG_SEG` is the Primary grid; `MJ_FAB` /
  `MJ_MICRO_MVGR` (any `MJ_<dim>`) are sec-cap examples. `RNG_SEG` = MRP tier
  (`E`/`V`/`P`/`SP`) — invariant 6.
- **Sec-cap dimensions MUST propagate.** `FAB`, `MACRO_MVGR`, `MICRO_MVGR`,
  `M_VND_CD`, `RNG_SEG` must survive `listing → listed → alloc`; dropping any
  silently loses sec-cap grids (invariant 4).
- **Growth lives at MAJ_CAT + grid level only** (invariant 2). The growth
  factors baked into MBQ (`BGT_SL_GR_DGR`, `DISP_GR_DGR`) are applied at the
  grid grain, never per OPT_TYPE. Same holds on any fallback path.
- **MBQ sparseness.** A grid MBQ of 0 means "no minimum buy at this grain"
  (e.g. no display fixture) — not a hard zero-budget block by itself (invariant
  3). But note the per-OPT-mode hard-block: empty `MBQ_ORIG` + empty grid value
  hard-block downstream (see `merge_rules.md`).
- **ACS_D ≠ daily sale** (invariant 5). `ACS_D` is accessories density (one OPT
  display quantity); OPT_CNT divides by `ACS_D`. Velocity uses `MAX_DAILY_SALE`.
- **CO → ST cascade.** CO provides the base for every store; ST overrides
  per-store where ST has a non-blank, non-`'0'` value. For `ALC_D`, priority is
  ST_MAJ_CAT > CO_MAJ_CAT > ST_MASTER.
- **`ARS_GRID_HIERARCHY` is ADD-ONLY.** Deleting/deactivating a grid does not
  drop its column; orphan columns are removed only via the explicit
  `POST /grid-builder/hierarchy/compact` admin action.
- **`STK_TTL` is clamped ≥ 0 at article grain** inside the pivot CTE, before
  rollup, so `SUM(STK_TTL)` reconciles across grids regardless of grouping.
  Individual SLOC columns keep their raw signed sums.
- **Row universe — company level (Business Rule `GRID_ROW_SCOPE`, 2026-10-12).**
  A non-pivot grid row exists for a store × key when (a) the store holds
  stock / pending of it (`ET_STORE_STOCK` + `ARS_PEND_ALC` pivot), or (b) the
  key is in the S5 universe, inserted with stock 0:
  - `COMPANY` (default) — every `Master_ALC_INPUT_ST_MASTER` store
    (`ST_STATUS` OLD/NEW/UPC) × every DISTINCT key in the latest
    `ARS_MSA_TOTAL` at **any** RDC. A DW01 store gets a row for a category
    only DH24 holds.
  - `RDC` — `ARS_MSA_TOTAL ⋈ ST_MASTER ON RDC`: only keys the store's **own**
    RDC carries (behaviour until 2026-10-12).
  The key is the grid's full hierarchy (`MAJ_CAT`, plus `MERGE_RNG_SEG` /
  `RNG_SEG` / `FAB` / `CLR` / … for merge and sec-cap grids), so every
  non-pivot grid shares one universe; a Run All pins the value once for all
  grids. Pivot-only grids (`MJ_GEN_ART`, `MJ_VAR_ART`, `MJ_SZ`) are stock-only
  in both modes. The universe is DISTINCT stores × DISTINCT keys and S5 inserts
  only keys with no grid row, so it can never duplicate a row (step 8 also
  dedupes on the PK). The `LISTING=1` filter still runs afterwards.
- **A universe row is not a budget.** S5 rows go through the same lookups as
  stock rows: `DISP_Q`, `SAL_PD`, `ACS_D`, `ALC_D` from `ARS_CALC_ST_MAJ_CAT`,
  whose `DISP_Q` and CM/NM sale come **only** from the store master
  `Master_ALC_INPUT_ST_MAJ_CAT` (`Master_ALC_INPUT_CO_MAJ_CAT` has no
  `DISP_Q`). A store with no display maintained for the category gets
  `DISP_Q 0 → MBQ 0, OPT_CNT 0`, and listing marks its options `NO_DISPLAY`
  (`MJ_DISP_Q = 0`), so it receives nothing until the display master is
  maintained.

### Formulas

Pre-grid (in `ARS_CALC_ST_MAJ_CAT`):

```
ALC_D  = ISNULL(INT_DAYS,0) + ISNULL(PRD_DAYS,0) + ISNULL(SL_CVR,0)
         (SL_CVR source priority: ST_MAJ_CAT > CO_MAJ_CAT > ST_MASTER)

SAL_PD = piecewise per-day sale blending current (CM) and next (NM) month:
   if CM_REM_D = 0                    → 0
   elif CM_REM_D >= ALC_D             → CM_SAL_Q / CM_REM_D
   elif ALC_D = 0                     → 0
   elif NM_REM_D = 0                  → CM_SAL_Q / CM_REM_D
   else → ( CM_SAL_Q
            + (NM_SAL_Q / NM_REM_D) * (ALC_D − CM_REM_D)
          ) / ALC_D
   → the whole result is ROUND(…, 2)   # 2 decimals (2026-07-18)
```

Grid columns (in each per-grid output table, order matters):

```
-- 1. MBQ (raw), then scaled by contribution
MBQ = CASE WHEN DISP_Q = 0/NULL THEN 0            -- no fixture ⇒ no minimum buy
           ELSE (SAL_PD * BGT_SL_GR_DGR) * ALC_D
                + (DISP_Q * DISP_GR_DGR)
      END
MBQ = CASE WHEN CONT = 0/NULL THEN 0
           ELSE ROUND(MBQ * CONT, 0) END          -- contribution scaling

-- 2. OPT_CNT  (uses RAW DISP_Q — computed before DISP_Q is rescaled)
OPT_CNT = CASE WHEN CONT = 0/NULL OR ACS_D = 0/NULL THEN 0
               ELSE ROUND(DISP_Q * DISP_GR_DGR * CONT / ACS_D, 0) END

-- 3. STR  (days of stock cover)
STR = STK_TTL / ( [L-7 DAYS SALE-Q] / 7 )

-- 4. DISP_Q rescale (LAST — after MBQ & OPT_CNT consumed raw DISP_Q)
DISP_Q = CASE WHEN CONT = 0/NULL THEN 0
              ELSE ROUND(DISP_Q * CONT, 0) END
```

Sec-cap growth matrix (optional, `ARS_SEC_CAP_GROWTH_MATRIX`, off by default):
replaces the flat per-grid `sec_cap_pct` with a growth% resolved from a
cont%-banded matrix at allocation time. Small contributors get a bigger stretch:

```
cont% band  →  growth%     (default bands)
 [0, 5)     →  300
 [5, 10)    →  250
 [10, 15)   →  200
 [15, 30)   →  150
 [30, ∞)    →  120
resolve_growth(cont_pct): first band with lo ≤ cont% < hi (last band open-ended)
```

### Validation gates — validate first, then create

Validate before committing grid results:

- **Validate the cascade before computing grids** — CO base created, CO gaps
  filled (`store × MAJ_CAT` complete), ST overlaid; each step returns
  `ok/skip/error` in the steps log. A missing PK or source table must surface as
  `skip`, not a silent pass.
- **Validate SLOC coverage before pivot** — expected columns = hierarchy cols +
  active SLOC cols + `STK_TTL`; add columns for newly-active SLOCs before
  writing.
- **Clamp `STK_TTL ≥ 0` at article grain before rollup**, so
  `SUM(STK_TTL)` parity holds across every grid grouping (validate parity, then
  publish).
- **Growth-matrix band validation** (`validate_bands`) before saving: bands
  non-empty, `lo ≥ 0`, sorted ascending, contiguous (`hi[i] == lo[i+1]`),
  exactly one open-ended (`hi=null`) last row, every `growth ≥ 100` (matrix
  relaxes, never tightens). Non-monotonic growth is a warning, not an error.
- **pivot_only / article-grain grids** skip CONT/MBQ/OPT_CNT and skip synthetic
  MSA-row injection (`_insert_missing_msa_rows`) — verify the `pivot_only` flag
  before running post-lookups so `(GEN_ART='NA', CLR='NA')` placeholders don't
  pollute consumers.
- **Row universe** — the S5 universe is resolved from `GRID_ROW_SCOPE`
  (`_grid_row_scope`; inactive / missing / unknown value → `COMPANY`) and is
  logged per grid as `post-pivot synthetic rows (GRID_ROW_SCOPE=…)`. Read-only
  check before a build: `_missing_rows_cte(...)` + `SELECT COUNT(*) FROM
  Missing` gives the rows S5 would add; under `RDC` it must be 0 against a
  grid last built under `RDC`.

### Key columns

| Column | Meaning | Formula / source |
|---|---|---|
| `ALC_D` | Total sale-cover days | `INT_DAYS + PRD_DAYS + SL_CVR` (cascaded) |
| `SAL_PD` | Per-day sale (blended CM/NM) | piecewise formula above |
| `CONT` | Contribution share of the grain | `Master_CONT_<dim>`; CO fallback; `1/COUNT` fallback |
| `DISP_Q` | Effective display quantity | raw from calc, then `ROUND(DISP_Q × CONT, 0)` |
| `ACS_D` | Accessories density (one OPT display qty) | calc; `MANUAL_DENSITY` overrides at article level. **NOT daily sale** |
| `DISP_GR_DGR` | Display growth factor | default 1 (null/0 → 1) |
| `BGT_SL_GR_DGR` | Budget-sale growth factor | default 1 (null/0 → 1) |
| `MBQ` | Minimum buy quantity | `((SAL_PD×BGT_SL_GR_DGR)×ALC_D + DISP_Q×DISP_GR_DGR) × CONT`, rounded; 0 if `DISP_Q=0` or `CONT=0` |
| `OPT_CNT` | Option count | `ROUND(DISP_Q × DISP_GR_DGR × CONT / ACS_D, 0)` (raw DISP_Q) |
| `STK_TTL` | Total stock at grain | `SUM(STK` SLOCs`)`, clamped ≥ 0 at article grain |
| `STR` | Days of stock cover | `STK_TTL / (L-7 sale / 7)` |
| `RNG_SEG` | MRP tier | `E`/`V`/`P`/`SP` — Primary grid axis |
| `sec_cap_pct` | Per-grid secondary cap % | grid config; `None` → global default; overridable by growth matrix |

## Recorded rules
<!-- dated appendable bullets; leave this comment and add any dated rules already known -->

- **2026-06-13 (from KB)** — `_ensure_hierarchy_table` / `ARS_GRID_HIERARCHY` is ADD-ONLY; deleting or deactivating a grid never drops its column. Orphan columns are removed only via the explicit `POST /grid-builder/hierarchy/compact` (default `dry_run=true`). Why: a single accidental Delete used to destroy a column and all its data.
- **2026-06-13 (from KB)** — Stage S5 `_insert_missing_msa_rows` is skipped when `grid.pivot_only=1` (article-grain grids), else it produces `(GEN_ART='NA', CLR='NA', ARTICLE='NA')` placeholder pollution. Non-pivot Primary/Secondary grids still get synthetic injection.
- **2026-06-13 (from KB)** — Routine `DBCC SHRINKFILE` after Run-All was removed (SQL Server anti-pattern: index fragmentation + regrowth). Do not reintroduce in any hot path.
- **2026-06-13 (from KB)** — `bootstrap_msa_pend_sync` at the end of Run-All is kept as a safety-net reseed of PEND_QTY/FNL_Q into the MSA tables; do not gate it behind a flag.
- **2026-07-17 (from live DB) — CORRECTION to BRD/FSD above.** The live `ARS_GRID_BUILDER` no longer treats `MJ_RNG_SEG` as the Primary grid. Live `grid_group='Primary'` = `MJ` (WERKS,MAJ_CAT) and `MJ_MERGE_RNG_SEG` (WERKS,MAJ_CAT,MERGE_RNG_SEG). `MJ_RNG_SEG` (raw tier) is now `Secondary` with `sec_cap_applicable=1, sec_cap_pct=130`, and is the single `use_for_opt_sale=1` grid (feeds listing PER_OPT_SALE). Sentences in the BRD ("one Primary grid — MJ_RNG_SEG") and FSD ("MJ_RNG_SEG is the Primary grid") are stale — read the merged-tier grid as Primary. Live sec_cap_applicable set = `MJ_RNG_SEG`, `MJ_FIT`, `MJ_M_YARN_02` (all pct=130); `MJ_FAB`/`MJ_CLR`/`MJ_M_VND_CD`/`MJ_WEAVE_2` Active-Secondary but no cap; `MJ_MACRO_MVGR`/`MJ_MICRO_MVGR` Inactive. Live `ARS_GRID_HIERARCHY` cols: MAJ_CAT, MERGE_RNG_SEG, RNG_SEG, M_YARN_02, WEAVE_2, FAB, CLR, M_VND_CD, FIT, SZ_APPLICABLE.
- **2026-07-08 (from code)** — Sec-cap growth matrix (`ARS_SEC_CAP_GROWTH_MATRIX`) is off by default; when enabled it replaces the flat `sec_cap_pct` per grain with a cont%-banded growth%. Bands must be contiguous with exactly one open-ended last row and every growth ≥ 100 (relaxes, never tightens); a stored growth < 100 is clamped to 100 on load.
- **2026-07-18 — Rounding precision.** `SAL_PD` is now `ROUND(…, 2)` at all four write sites in `grid_calculations.py` (`_step_sal_pd` MAJ_CAT, `_step_art_sal_pd` fallback + main, `_step_master_sale_sal_pd`). Contribution percentages `STOCK_CONT%` / `SALE_CONT%` / `INITIAL AUTO CONT%` (and the final `CONT`) are `ROUND(…, 4)` — set in `contrib.py` `_compute` (the KPI display columns stay at 2 dp); the Stage B CONT fallback in `rule_engine_new.py` already rounded to 4. Values only; no formula/logic change.
- **2026-07-18 — Data dictionary.** `ARS_DATA_DICTIONARY` seed audited + topped up (SAL_PD, ALC_D, DISP_Q, MBQ, OPT_CNT, growth degrees, I_ROD, STK_TTL, MSA_FNL_Q, ELIG_FLAG, RL_HOLD_QTY, VAR_COUNT/VAR_FNL_COUNT, AGE, MAX_DAILY_SALE, OPT_MBQ_WH/OPT_REQ_WH, ART_EXCESS, ST_RANK/W_SCORE, ALLOC_TYPE, OPT_PRIORITY_RANK, MJ_REQ_REM, ALLOC_REMARKS, FROM_HOLD, contribution %). `_ensure_table` now idempotently inserts missing seed columns (no wipe). New `GET /data-dictionary/export` → .xlsx, with an **Export** button on the Data Dictionary page.
- **2026-10-12 — Grid rows at COMPANY level (Business Rule `GRID_ROW_SCOPE`).** Owner ask: "grid row to be generated company level, not based on RDC availability". Until today S5 (`_insert_missing_msa_rows`) built its universe as `ARS_MSA_TOTAL ⋈ Master_ALC_INPUT_ST_MASTER ON SM.RDC = MSA.RDC`, so a store got a non-stock row only for categories its **own** RDC carried — 2,777 listed store × category pairs in 1SZ scope (303 stores, 38 categories; 2,711 DW01 + 66 DH24), e.g. HA13 Ziro (DW01) × YGW_SKEEVY (DH24-only), had no `ARS_GRID_MJ` row. Now `GRID_ROW_SCOPE` (module Grid Builder, choice `COMPANY|RDC`, seeded Active = `COMPANY`; inactive / missing → `COMPANY`) selects the universe; `COMPANY` = DISTINCT stores × DISTINCT keys of ANY RDC. Applies to every non-pivot grid, Run All pins it once per run, takes effect from the next grid build. Read-only measurement against the 2026-10-12 13:09 build (MSA seq 398): `RDC` reproduces today exactly (0 missing rows in all 9 grids); `COMPANY` adds MJ +10,921 · MERGE_RNG_SEG +20,007 · RNG_SEG +41,557 · FIT +37,086 · M_YARN_02 +41,716 · WEAVE_2 +87,412 · FAB +138,705 · CLR +650,002 · M_VND_CD +213,102 (≈ 1.24 M rows, +13 %), no duplicate keys. Of the 10,921 new MJ rows only **342** have a display (`DISP_Q > 0`, MBQ 27,092 pcs; 153 / 4,450 pcs in the 1SZ subset), 678 have `SAL_PD > 0`, 545 (10 categories) are not in the calc masters at all. All 47 new YGW_SKEEVY rows (incl. HA13) are `DISP_Q 0 / SAL_PD 0 / MBQ 0` (ACS_D 22, ALC_D 5). Why it matters: a row is not a budget — to receive stock the store × category also needs `DISP_Q` in `Master_ALC_INPUT_ST_MAJ_CAT`; without it listing blocks the options `NO_DISPLAY`.
- **2026-10-14 — Contribution % and Auto Cont % modules removed.** The `/contribution/*` and `/auto-cont/*` pages, `contrib.py`, `auto_contrib.py` and `sp_AutoContCompute` are gone (along with their `Cont_presets` / `Cont_mappings` / `Cont_Percentage_*` / `AutoCont_*` tables — dropped per `backend/scripts/040_retire_removed_modules.sql`). **No grid or allocation logic changes:** the `CONT` used by the grid and rule engines always came from the uploaded `Master_CONT_<dim>` tables (rule engine default `Master_CONT_SZ`), which those modules never wrote. The `/listing/contribution` endpoint (RDC stock-vs-alloc chart on the Listing page) is unrelated and stays.
