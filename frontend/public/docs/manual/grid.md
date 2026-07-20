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
| `Master_ALC_INPUT_ST_MAJ_CAT` | ST_CD, MAJ_CAT | Store-level overrides (overlay). |
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
