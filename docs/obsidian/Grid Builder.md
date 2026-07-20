---
title: Grid Builder
tags: [ars, grid]
updated: 2026-07-17
---

# Grid Builder

Stage 2 of the [[Pipeline Overview|pipeline]]. A **grid** is a dynamic pivot of store stock at a chosen hierarchy grain, materialized into its own `ARS_GRID_<name>` table with pivoted SLOC columns + `STK_TTL` and computed `MBQ`, `OPT_CNT`, `DISP_Q`, `STR`. The [[Rule Engine (per_opt)|allocator]] ships against these grids.

- **Source:** `backend/app/api/v1/endpoints/grid_builder.py`, `backend/app/services/grid_calculations.py`.
- **Primary grid** = per-category budget baseline (`grid_group='Primary'`). **Secondary / sec-cap grid** = a ceiling on a second dimension (`grid_group='Secondary'` + `sec_cap_applicable=1`) — see [[Secondary-Grid Cap]].

## Live grids (confirmed from DB, 2026-07-17)
> [!warning] Doc drift corrected
> The dossier `manual/grid.md` body says "MJ_RNG_SEG is Primary" — **stale**. Live config below.

| Grid | grain (last hier col) | status | group | sec_cap | pct | note |
|------|----------------------|--------|-------|:------:|----:|------|
| `MJ` | MAJ_CAT | Active | **Primary** | | | budget baseline |
| `MJ_MERGE_RNG_SEG` | MERGE_RNG_SEG | Active | **Primary** | | | merged-tier baseline |
| `MJ_RNG_SEG` | RNG_SEG | Active | Secondary | ✔ | 130 | **sole `use_for_opt_sale`** |
| `MJ_FIT` | FIT | Active | Secondary | ✔ | 130 | |
| `MJ_M_YARN_02` | M_YARN_02 | Active | Secondary | ✔ | 130 | |
| `MJ_WEAVE_2` | WEAVE_2 | Active | Secondary | ✘ | — | |
| `MJ_FAB` | FAB | Active | Secondary | ✘ | — | |
| `MJ_CLR` | CLR | Active | Secondary | ✘ | — | |
| `MJ_M_VND_CD` | M_VND_CD | Active | Secondary | ✘ | — | |
| `MJ_MACRO_MVGR` / `MJ_MICRO_MVGR` | MACRO/MICRO_MVGR | **Inactive** | Secondary | ✘ | — | |
| `MJ_GEN_ART` / `MJ_VAR_ART` | …GEN_ART/VAR_ART | Active | None | | | `pivot_only` |

**Only `MJ_RNG_SEG`, `MJ_FIT`, `MJ_M_YARN_02` are actually sec-cap-applicable** (all 130%). The KB's old canonical sec-cap examples (`MJ_FAB`, `MJ_MICRO_MVGR`) are NOT currently cap-applicable.

## `ARS_GRID_BUILDER` definition
Key columns: `hierarchy_columns` (ordered JSON; **last col = grain**), `seq`, `kpi_filter`, `output_table`, `grid_group`, `pivot_only` (skip CONT/MBQ + synthetic MSA injection), `use_for_opt_sale` (exactly ONE grid; feeds listing `PER_OPT_SALE`), `sec_cap_applicable`, `sec_cap_pct`, `weightage`. There is **no per-grid MBQ column here** — MBQ is computed into each grid's own output table.

## Calculations
**Pre-grid cascade** (`calculate_per_day_sale`) → `ARS_CALC_ST_MAJ_CAT` / `ARS_CALC_ST_ART` (masters never mutated). CO base → fill CO gaps → ST overlay (ST wins when non-blank) → defaults:
```
ALC_D  = INT_DAYS + PRD_DAYS + SL_CVR    (SL_CVR priority: ST_MAJ_CAT > CO_MAJ_CAT > ST_MASTER)
SAL_PD = piecewise CM/NM per-day sale blend
ACS_D  ← MANUAL_DENSITY when > 0 (article grain)
```
**Grid-level calc** (order matters):
```
MBQ     = (SAL_PD × BGT_SL_GR_DGR) × ALC_D + (DISP_Q × DISP_GR_DGR)   ; 0 if DISP_Q=0/NULL
MBQ     = ROUND(MBQ × CONT, 0)            ; 0 if CONT=0/NULL
OPT_CNT = ROUND(DISP_Q × DISP_GR_DGR × CONT / ACS_D, 0)   (raw DISP_Q, ÷ density)
STR     = STK_TTL / (L-7 sale / 7)
DISP_Q  = ROUND(DISP_Q × CONT, 0)         (rescaled LAST — after MBQ/OPT_CNT use raw DISP_Q)
```
Rollup/pivot: SLOC discovery from `ET_STORE_STOCK` × `ARS_STORE_SLOC_SETTINGS`; staged in tempdb + chunked insert (dodges Azure 9002 log-full); **`STK_TTL` clamped ≥0 at (WERKS,MATNR) grain** so `SUM(STK_TTL)` reconciles across grids; synthetic MSA-row injection for missing `(WERKS,MAJ_CAT)` pairs (skipped when `pivot_only`).

## `ARS_GRID_HIERARCHY` — applicability registry
Base col `MAJ_CAT` (PK) + one 0/1 column per active non-article grid (named after its last hierarchy col: RNG_SEG, FAB, CLR…). **ADD-ONLY** — deactivating a grid never drops its column; orphans removed only via `POST /hierarchy/compact` (default `dry_run=true`). Protected manual col `SZ_APPLICABLE` (Y/N).

## Extras propagation (sec-cap invariant 4)
The MP-resolved dimensions must survive `listing → listed → alloc`. The live sec-cap dimension set is **broader** than the KB's 5-col list — it is the `ARS_GRID_HIERARCHY` columns: `RNG_SEG, MERGE_RNG_SEG, FIT, M_YARN_02, WEAVE_2, FAB, CLR, M_VND_CD`. Drop any and [[Secondary-Grid Cap|sec-cap]] silently loses grids. Populated in [[Listing]] Part 4 pre-resolve.

## Key endpoints
`GET /columns` · `GET/POST /grids` · `PUT/DELETE /grids/{id}` · `POST /grids/{id}/run` · `POST /run-all` (async, polled) · `POST /hierarchy/compact`. Create/update enforce `_ensure_merge_parent_grid_exists` (a `MERGE_<X>` grid needs an Active parent ending in `X`) and single-`use_for_opt_sale`.

## Gotchas
- `ARS_GRID_HIERARCHY` is ADD-ONLY — never reintroduce DROP/REBUILD on CRUD.
- MBQ order dependency: DISP_Q rescale MUST be last.
- Grid-level `MBQ=0 when DISP_Q=0` is a fixture rule; invariant 3 (MBQ=0 = no constraint) is **superseded downstream** in per_opt (empty grid value / `MBQ_ORIG=0/NULL` hard-blocks).
- [[Merge Rules]] feed the merged grids; sec-cap growth matrix (`ARS_SEC_CAP_GROWTH_MATRIX`) is off by default.

## Cross-links
[[MSA Stock Calculation]] (synthetic-row join) · [[Merge Rules]] · [[Listing]] (consumes grids) · [[Secondary-Grid Cap]] · [[Contribution and CONT]].
