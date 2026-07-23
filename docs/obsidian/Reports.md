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
| MSA Master | `dbo.usp_ars_msa_master` · `backend/sql/usp_ars_msa_master.sql` | Opening-vs-closing MSA reconciliation; multi-level rollup + ordered steps |

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
- `MSA_OP_Q` / `MSA_CL_Q` / `OP_OPT_CNT_>50_PCS` / `CL_OPT_CNT_>50_PCS` are **RDC+MAJ_CAT totals** (repeated per store/dim — **do NOT SUM** across stores/dim) and are **dimension-split on secondary grids** via `pdim` (article→dim mapping). No FRESH/GRT filter.
- `ALC_Q` / `HOLD_ALC_Q` / `ALC_FROM_HOLD_Q` / `ART_EXCESS_QTY` (from listing) are **store-level**, **dimension-split** on secondary grids and reconcile to the base-grid MAJ_CAT total.
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

## MSA Master — `dbo.usp_ars_msa_master`

Opening → allocation-movement → closing reconciliation of MSA, rolled up to any grain. Source: `backend/sql/usp_ars_msa_master.sql` (built & live-validated on `HOPC866`, 2026-07-23). Related: [[MSA Stock Calculation]] · [[Fresh-GRT Allocation]] · [[Data Model]].

### Parameters
| Param | Default | Meaning |
|-------|---------|---------|
| `@Level` | `DETAIL` | grain, or `'LIST'` for the catalogue, or a **comma list run in order** (steps) |
| `@SESSION_ID` | latest `ARS_LISTING_HISTORY` | opening session |
| `@RDC` / `@MAJ_CAT` | `NULL` | optional filters |

```sql
EXEC dbo.usp_ars_msa_master @Level = 'LIST';                                   -- discover levels
EXEC dbo.usp_ars_msa_master @Level = 'GEN_CLR', @RDC = 'DW01', @MAJ_CAT = 'M_JEANS';
EXEC dbo.usp_ars_msa_master @Level = 'RDC,MAJ_CAT,GEN_CLR,DETAIL', @RDC = 'DW01';  -- ordered steps
```

### Levels (`dbo.vw_ars_msa_master_levels` — single source of truth)
| Code | Name | Grain |
|------|------|-------|
| `DETAIL` | Article + Size | …GEN_ART·CLR·ARTICLE·PAK_SZ·SZ·RNG_SEG·ALLOC_TYPE (finest) |
| `ARTICLE` | Article (sizes merged) | …ARTICLE·PAK_SZ·RNG_SEG·ALLOC_TYPE |
| `GEN_CLR` | Gen-Article + Colour | RDC·SEG·DIV·SUB_DIV·MAJ_CAT·GEN_ART·CLR (OPT grain) |
| `MAJ_CAT` | Major Category | RDC·SEG·DIV·SUB_DIV·MAJ_CAT |
| `RDC` | RDC grand total | RDC |

Add a level = add a row to the view; the proc reads `KEY_COLS`/`KEY_SEL` from it and `@Level='LIST'` shows it automatically. **No native SQL param dropdown** — a UI/report (React page, SSRS, Power BI) binds its dropdown to `SELECT LEVEL_CODE, LEVEL_NAME FROM dbo.vw_ars_msa_master_levels ORDER BY SORT_ORDER`.

### Column / source map
| Column | Source |
|--------|--------|
| RDC/SEG/DIV/SUB DIV/MAJ CAT/GEN_ART/CLR/ARTICLE/PAK_SZ/SZ/RNG_SEG/ALLOC_TYPE | opening `ARS_MSA_TOTAL_HISTORY` |
| FAB-MVGR-1 / FAB-MVGR-2 | `vw_master_product.M_YARN_02` / `WEAVE_2` (per ARTICLE_NUMBER) |
| V02 BEFORE ALC | opening `V02_FRESH` (FRESH) / `V02_GRT` (GRT), via `SUM(CASE ALLOC_TYPE…)` |
| PEND INT / HOLD INT / OP MSA-Q USE IN ALC | opening `PEND_QTY` / `HOLD_QTY` / `FNL_Q` |
| ALC-Q / TD-ALC FROM HOLD_Q / TD-HOLD ALC-Q | `ARS_ALLOC_HISTORY` `SUM(ALLOC_QTY / FROM_HOLD_QTY / HOLD_QTY)` |
| PEND AFT ALC / HOLD AFT ALC / REM MSA-Q | live `ARS_MSA_TOTAL` `PEND_QTY` / `HOLD_QTY` / `FNL_Q` |

### Report Generation — dropdown + fan-out (one file per level)
In the Report Generation hub, `@Level` shows a **multi-select dropdown** (chips, click order = run order) sourced from the registry `dbo.ARS_PROC_PARAM_VALUES` (seeded from the level catalog, `FANOUT=1`). Because `@Level` is `FANOUT=1`, selecting several levels makes the engine run the proc **once per level** and write a **separate output file suffixed with the level** — e.g. `MSA_OP_CL_DETAIL`, `MSA_OP_CL_GEN_CLR`, `MSA_OP_CL_MAJ_CAT` — instead of one combined file. Steps in a report reorder by **drag-and-drop**. Impl: `data_export_service.fanout_param_runs()` + `report_engine` sql-step loop; UI `ReportGenerationPage.jsx`. Gotcha: with **Folder-per-run off**, a stale pre-fan-out `MSA_OP_CL.csv` lingers beside the suffixed files — delete once or enable folder-per-run.

### Design & gotchas
- A `base` CTE resolves everything at finest grain; each level is a final `GROUP BY` (measures additive). FRESH/GRT are separate rows (`ALLOC_TYPE`) at DETAIL/ARTICLE; combined at higher levels.
- **Zero rows dropped** — `HAVING` keeps only rows where some measure ≠ 0.
- **Sequence/steps** — `@Level` accepts a comma list run in order (e.g. `'RDC,MAJ_CAT,GEN_CLR,DETAIL'`). Returns **one combined grid** (`UNION ALL` of the steps — *not* multiple result sets, which most clients don't show past the first), with leading `STEP` (1..n) + `LEVEL` columns; a key a level doesn't use is `NULL` on that step's rows; ordered by `STEP` then key so the steps read top-to-bottom. Parsed with `OPENJSON` (order-preserving); a superset of keys is projected per branch (real col if the level uses it, else `NULL`, all `CAST NVARCHAR` for UNION compatibility).
- **Two perf traps (both fixed, both essential):** (1) `ARTICLE_NUMBER` is `nvarchar` in the MSA tables but `bigint` in `vw_master_product` → `CAST` to `NVARCHAR(50)` in `pmap` (else nested-loop over the 3.6M-row view). (2) `(@p IS NULL OR col=@p)` filters → `OPTION (RECOMPILE)` (else full scan of the 40M-row alloc table, minutes-long hang).

## App modules (not stored procs)
- [[UPC Store Tracking]] — store-opening lifecycle tracker (`/reports/upc-tracking`): opening-date & remarks/status history, live MBQ/stock/SLOC/FR by segment, dispatch-lead-based Bal Days / Repl Days / **D.GAP**, priority synced to the store master.

## Cross-links
[[ARS Work Log]] · [[Report Generation Hub]] · [[UPC Store Tracking]] · [[Grid Builder]] · [[Pending Allocation and Hold]] · [[Data Model]] · [[2026-07-18]]
