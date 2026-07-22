---
title: Reports
tags: [ars, report, moc, index]
updated: 2026-07-18
---

# ARS Reports (SQL / stored procs)

Catalogue of hand-built analytics reports — ad-hoc queries and stored procs that read the ARS tables for review/ops. Distinct from [[Report Generation Hub]] (the scheduled/triggered app feature). Source SQL lives under `backend/sql/`.

> [!tip] Convention
> **Whenever a new report is built, add it here** as a new `## <Report name>` section (params, sources, grains, validation, source file). Keep one section per report; update the index table below.

## Index
| Report | Proc / file | What it does |
|--------|-------------|--------------|
| Grid Report | `dbo.usp_ars_grid_report` · `backend/sql/usp_ars_grid_report.sql` | Grid table enriched with store / product / listing / MSA / alloc / hold; any `ARS_GRID_MJ[_dim]` |

---

## Grid Report — `dbo.usp_ars_grid_report`

A parameterized analytics report over the grid tables. Source: `backend/sql/usp_ars_grid_report.sql` (built & live-validated on `HOPC866`, 2026-07-18). Enriches a grid table with store, product, listing, MSA, allocation and hold context in one wide result set. Related: [[Grid Builder]] · [[Pending Allocation and Hold]] · [[MSA Stock Calculation]] · [[Data Model]].

### Parameters
| Param | Default | Meaning |
|-------|---------|---------|
| `@GridTable` | `ARS_GRID_MJ` | any grid table — `ARS_GRID_MJ_RNG_SEG`, `_MERGE_RNG_SEG`, `_M_YARN_02`, `_FAB`, `_CLR`, … |
| `@WERKS` | `NULL` | optional single-store filter (bound param) |
| `@MAJ_CAT` | `NULL` | optional single-category filter (bound param) |

```sql
EXEC dbo.usp_ars_grid_report;
EXEC dbo.usp_ars_grid_report @GridTable = N'ARS_GRID_MJ_RNG_SEG';
EXEC dbo.usp_ars_grid_report @GridTable = N'ARS_GRID_MJ_M_YARN_02', @WERKS = N'HB05', @MAJ_CAT = N'M_JEANS';
```

### How it's built
- **`@DimCol` auto-detected** = the one column `@GridTable` has that `ARS_GRID_MJ` does not (`NULL` for the base grid). No hard-coding per table.
- **Grid columns are dynamic** (`STRING_AGG` over `INFORMATION_SCHEMA.COLUMNS`) with **type-aware `ISNULL`**: numeric→`0`, text→`''`, else left as-is; original names kept via `AS`. So `[L-7 DAYS SALE-Q]`, `[0001]`, text `CONT` (RNG_SEG grid) all handled.
- All **other** tables contribute a hand-picked column list.
- Body runs via `sp_executesql` (filters passed as `@pW`/`@pMC` — injection-safe). Guards unknown `@GridTable` with `RAISERROR`.

### CTEs & sources
| CTE | Source | Grain | Gives |
|-----|--------|-------|-------|
| `sid` | `ARS_LISTING_HISTORY` | — | latest `SESSION_ID` (referenced once) |
| `lst` | `ARS_LISTING_WORKING_HISTORY` | WERKS,MAJ_CAT | ALC_Q, HOLD_ALC_Q, ALC_FROM_HOLD_Q, ART_EXCESS_QTY |
| `msa` | `ARS_MSA_TOTAL_HISTORY` | RDC,MAJ_CAT | MSA_OP_Q (`SUM(FNL_Q)`) |
| `opt_gt50_op` | `ARS_MSA_TOTAL_HISTORY` (session-filtered) | RDC,MAJ_CAT | **OPENING** `OP_OPT_CNT_>50_PCS` = # of (GEN_ART,CLR) with `SUM(FNL_Q)>50` |
| `opt_gt50_cl` | `ARS_MSA_TOTAL` (live, no SESSION_ID) | RDC,MAJ_CAT | **CLOSING** `CL_OPT_CNT_>50_PCS` = same count on the live MSA table |
| `alloc` | `ARS_ALLOC_HISTORY` | RDC,MAJ_CAT | consumed → `MSA_CL_Q = MSA_OP_Q − consumed` |
| `pdim`* | `vw_master_product` | ARTICLE_NUMBER | one dim value per variant (`MAX`, deduped) |
| `hmov` | `ARS_ALLOC_HISTORY` | WERKS,MAJ_CAT[,dim] | added/consumed hold today |
| `hold` | `ARS_NL_TBL_HOLD_TRACKING` | WERKS,MAJ_CAT[,dim] | CLOSE_REM (`SUM(HOLD_REM)`) |
| `prod` | `vw_master_product` | MAJ_CAT | SEG, DIV, SUB_DIV, SSN (`MAX`) |

\* `pdim` only present on secondary grids. Store details join `MASTER_ALC_INPUT_ST_MASTER` on `ST_CD = WERKS`.

### OPT>50 counts (opening vs closing)
Two columns, both = count of `(GEN_ART, CLR)` OPTs whose `SUM(FNL_Q) > 50`, per RDC+MAJ_CAT:
- **`OP_OPT_CNT_>50_PCS`** — from `ARS_MSA_TOTAL_HISTORY`, filtered to the latest session (**opening** MSA).
- **`CL_OPT_CNT_>50_PCS`** — from the live `ARS_MSA_TOTAL` table (no `SESSION_ID` column) = **closing** MSA after consumption.
- Verified DW01/M_JEANS: OP=156, CL=155.

### Grain rules (important)
- `MSA_OP_Q` / `MSA_CL_Q` / `OP_OPT_CNT_>50_PCS` / `CL_OPT_CNT_>50_PCS` are **RDC+MAJ_CAT totals** repeated on every store (and every dim) row — **do NOT SUM** across stores/dim. (Not dimension-split; no FRESH/GRT filter.)
- `HOLD_*` are **store-level**. On a **secondary grid** they are **split by the grid dimension** and reconcile to the MAJ_CAT total.

### Hold movement
`opening = closing − added_today + consumed_today`, where
- closing = `SUM(HOLD_REM)` (`ARS_NL_TBL_HOLD_TRACKING` snapshot),
- added = `SUM(HOLD_QTY)`, consumed = `SUM(FROM_HOLD_QTY)` (this session's `ARS_ALLOC_HISTORY`).

The hold table has no dimension column, so on secondary grids `pdim` maps `VAR_ART → ARTICLE_NUMBER → dim` (deduped, no fan-out); `hold`/`hmov` then group by `(WERKS, MAJ_CAT, dim)`. Verified HB05/M_JEANS on RNG_SEG: V41 + P14 + SP0 + E0 = **55** = the base-grid MAJ_CAT total.

### Known join hazard
Some categories are `[A-Z]-`-prefixed in grid/MSA/listing (`B-M_TEES_HS`) but never in alloc/hold — see [[Known Risks and Doc Drift]]. Those rows show 0 for RDC/hold columns; not normalized because the prefixed and unprefixed forms coexist as distinct grid rows.

---

---

## App modules (not stored procs)
- [[UPC Store Tracking]] — store-opening lifecycle tracker (`/reports/upc-tracking`): opening-date & remarks/status history, live MBQ/stock/SLOC/FR by segment, dispatch-lead-based Bal Days / Repl Days / **D.GAP**, priority synced to the store master.

## Cross-links
[[ARS Work Log]] · [[Report Generation Hub]] · [[UPC Store Tracking]] · [[Grid Builder]] · [[Pending Allocation and Hold]] · [[Data Model]] · [[2026-07-18]]
