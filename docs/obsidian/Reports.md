---
title: Reports
tags: [ars, report, moc, index]
updated: 2026-08-04
---

# ARS Reports (SQL / stored procs)

Catalogue of hand-built analytics reports — ad-hoc queries and stored procs that read the ARS tables for review/ops. Distinct from [[Report Generation Hub]] (the scheduled/triggered app feature). Source SQL lives under `backend/sql/`.

> [!tip] Convention
> **Whenever a new report is built, add it here** as a new `## <Report name>` section (params, sources, grains, validation, source file). Keep one section per report; update the index table below.

## Index
| Report | Proc / file | What it does |
|--------|-------------|--------------|
| Grid Report | `dbo.usp_ars_grid_report` · `backend/sql/usp_ars_grid_report.sql` | Grid table enriched with store / product / listing / MSA / alloc / hold; any `ARS_GRID_MJ[_dim]` |
| Grid Report (All-RDC) | `dbo.ALLRDC_usp_ars_grid_report` · `backend/sql/ALLRDC_usp_ars_grid_report.sql` | Per-RDC variant: national pool view + 18 RDC-specific columns (ALC_Q via SRC_RDC, supply via RDC, hold via RDC) |
| MSA Master | `dbo.usp_ars_msa_master` · `backend/sql/usp_ars_msa_master.sql` | Opening-vs-closing MSA reconciliation; multi-level rollup + ordered steps |
| MSA Master (All-RDC) | `dbo.ALLRDC_usp_ars_msa_master` · `backend/sql/ALLRDC_usp_ars_msa_master.sql` | Per-RDC variant: fixes 39% under-count in base via SRC_RDC (source RDC, not store RDC) |
| Fresh Lorry | `dbo.usp_ars_fresh_lorry` · `backend/sql/usp_ars_fresh_lorry.sql` | Fresh pool dispatch with per-option HOLD/RELEASE verdict (TBL <30% size coverage → HOLD) |
| Alloc Batch Summary | `dbo.usp_ars_alloc_batch_summary` · `backend/sql/usp_ars_alloc_batch_summary.sql` | Batch summary at (DIV, SUB_DIV, STATUS, MAJ_CAT): shipped quantity, MSA available, store-level unmet requirement |

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

### OPT MBQ per category (2026-08-01)
Column `MJ_PER_OPT_MBQ` = **per-OPT minimum purchase quantity** from grid:
```
MJ_PER_OPT_MBQ = ACS_D + (SAL_PD × ALC_D)
```
where **ACS_D** (accessories density = one OPT display qty) and **ALC_D** are MAJ_CAT-constant from the grid row; **SAL_PD** (per-day sales, 2dp) is the grid's daily velocity metric, not the per-OPT SALE (which was not category-constant). On secondary grids, MJ_PER_OPT_MBQ is **dim-level** (one row per category+dimension, not per individual OPT). Used by allocation to size each option's minimum take.

### OPT Status pivot (2026-08-01 — dynamic columns; 2026-08-04 architecture)
**NEW:** Runtime-built pivot columns from `ARS_LISTING_WORKING_HISTORY` distinct `OPT_STATUS` values by SESSION_ID (grow/shrink automatically):
- `CNT_<STATUS>` — count of distinct (GEN_ART, CLR) in that status; grain = (WERKS, MAJ_CAT[, dim])
- `ALCQ_<STATUS>` — sum of ALLOC_QTY by status
- Status vocabulary: **NL** (not listed), **RL** (regular live), **WRL** (watch-list live), **TBL** (table live), **L** (new/report-only), **MIX** (mixed post-alloc)

**Pipeline reorder (2026-08-04):** `listing.py` moved the parking block (snapshot_session_to_parked of ARS_LISTING_WORKING) from Part 8.4 to Part 8.56 — **after** OPT_STATUS stamp (Part 8.5) and hold release (Part 8.55). This means the PARKED snapshot now captures the **final working state** with OPT_STATUS + released holds populated. The pivot reads `ARS_LISTING_WORKING_HISTORY` by SESSION_ID; OPT_STATUS is now populated going forward by the reorder (no backfill needed). The current session was one-time backfilled from live working; future runs will fill it naturally via the normal snapshot. See [[Listing parking reorder (2026-08-04)]].

**Gotcha:** On dim-split grids, an OPT with NULL/unmapped dim (e.g., NULL RNG_SEG) counts at base MAJ_CAT but not in the dim view (e.g., base CNT_MIX=75, sum of dims=74 → 1 OPT with NULL RNG_SEG). Expected, not a bug — **must be explained to users**. Related: [[Listing OPT_TYPE + OPT_STATUS classification]].

### Grain rules (important)
- `MSA_OP_Q` / `MSA_CL_Q` / `OP_OPT_CNT_>50_PCS` / `CL_OPT_CNT_>50_PCS` are **RDC+MAJ_CAT totals** (repeated per store/dim — **do NOT SUM** across stores/dim) and are **dimension-split on secondary grids** via `pdim` (article→dim mapping). No FRESH/GRT filter.
- `ALC_Q` / `HOLD_ALC_Q` / `ALC_FROM_HOLD_Q` / `ART_EXCESS_QTY` (from listing) are **store-level**, **dimension-split** on secondary grids and reconcile to the base-grid MAJ_CAT total.
- `HOLD_*` are **store-level**. On a **secondary grid** they are **split by the grid dimension** and reconcile to the MAJ_CAT total.
- `MJ_PER_OPT_MBQ` and `CNT_*/ALCQ_*` (OPT Status) are **dimension-level on secondary grids**, NOT summed across dims — each row is one dim's view.

### Hold movement
`opening = closing − added_today + consumed_today`, where
- closing = `SUM(HOLD_REM)` (`ARS_NL_TBL_HOLD_TRACKING` snapshot),
- added = `SUM(HOLD_QTY)`, consumed = `SUM(FROM_HOLD_QTY)` (this session's `ARS_ALLOC_HISTORY`).

The hold table has no dimension column, so on secondary grids `pdim` maps `VAR_ART → ARTICLE_NUMBER → dim` (deduped, no fan-out); `hold`/`hmov` then group by `(WERKS, MAJ_CAT, dim)`. Verified HB05/M_JEANS on RNG_SEG: V41 + P14 + SP0 + E0 = **55** = the base-grid MAJ_CAT total.

### Known join hazard
Some categories are `[A-Z]-`-prefixed in grid/MSA/listing (`B-M_TEES_HS`) but never in alloc/hold — see [[Known Risks and Doc Drift]]. Those rows show 0 for RDC/hold columns; not normalized because the prefixed and unprefixed forms coexist as distinct grid rows.

---

## Grid Report (All-RDC) — `dbo.ALLRDC_usp_ars_grid_report`

Per-RDC variant of the base Grid Report. Source: `backend/sql/ALLRDC_usp_ars_grid_report.sql` (deployed 2026-10-06, both `HOPC866` & `ARSDBPRO`, sha 1511a9e585080afd). **Use this when you need to see what stock is available FROM each RDC, and how much was actually allocated FROM each source.**

Related: [[Grid Report — dbo.usp_ars_grid_report]] · [[Fresh-GRT Allocation]] · [[Data Model]] · [[project_rdc_store_vs_src]].

### Purpose

Answers: For each store, how much MSA (supply) is available FROM each RDC, and how much allocation came FROM each RDC? Solves the reporting gap that base Grid Report doesn't expose. Critical for supply-chain ops and cross-RDC transfer reconciliation.

### Key behaviours

1. **All-RDC pool view** — Four RDC-scoped measures aggregate nationally instead of joining `msa.RDC = s.RDC`:
   - `MSA_OP_Q`, `MSA_CL_Q`, `OP_OPT_CNT_>50_PCS`, `CL_OPT_CNT_>50_PCS` show the ENTIRE national pool to every store
   - Example (M_JEANS, DH24 store): 28,964 (base proc, DH24 only) → 41,682 (all-RDC national, +63%)

2. **Dynamic per-RDC columns** (9 columns per RDC, auto-discovered at execution):
   - `<RDC>_ALC_Q` — shipped FROM this RDC (key attribute: SRC_RDC, sourced from `ARS_ALLOC_RDC_SPLIT_HISTORY`)
   - `<RDC>_MSA_OP_Q`, `<RDC>_MSA_CL_Q` — opening/closing MSA BY this RDC (RDC filter)
   - `<RDC>_OP_OPT_CNT_>50_PCS`, `<RDC>_CL_OPT_CNT_>50_PCS` — options with >50 pcs, qualified BY this RDC
   - `<RDC>_HOLD_OPEN_REM`, `<RDC>_HOLD_CONSUMED_TODAY`, `<RDC>_HOLD_ADDED_TODAY`, `<RDC>_HOLD_CLOSE_REM` — hold movement by RDC

RDC codes are discovered at runtime from `ARS_MSA_TOTAL` — no hardcoding. A new RDC automatically grows 9 columns.

### Validation (session 20261005_124520_655)
- **521 rows** (one MAJ_CAT), all dimensioned WERKS × MAJ_CAT × RDC
- **ALC_Q reconciles exactly:** 22,402 = 17,111 (DH24) + 5,291 (DW01), verified on all 521 rows
- **Hold reconciles** (values currently 0, structural check pass)
- **Cross-RDC activity:** 76,550 of 150,233 rows marked IS_CROSS=1 (51%)
- **Runtime:** 51.9s for one MAJ_CAT

### CRITICAL GOTCHAS

#### 1. >50 counts are NOT sums of the per-RDC counts
The all-RDC count (64) ≠ sum of per-RDC counts (39 + 27 = 66). An option with 30 pcs in each RDC fails the 50-pcs threshold per-RDC but passes nationally. **This is correct — do not "fix" it.** The all-RDC CTEs qualify across all RDCs; the per-RDC CTEs qualify within each RDC only. Both views are intentional.

#### 2. HOLD_REM is currently 0 on every row
While `HOLD_QTY_INITIAL ≈ 4.2M` total — flagged as a possible upstream data issue, unresolved. Structural validation only.

#### 3. Dynamic-SQL quoting and declaration order (CRITICAL)
**Quoting:** When splicing a fragment into an `N'...'` literal, reopen with `N'` at the tail, NOT `N'` + close quote. Wrong form produces "Syntax error, permission violation, or other nonspecific error."

**Declaration:** Fragment variables must be DECLAREd BEFORE the CTE-string DECLAREs that reference them, else "Must declare the scalar variable."

#### 4. Grepping for old formulas in proc definition produces false positives
Strip `/* */` and `--` comments first. Header comments mentioning a formula name match grep patterns even if the code was fixed.

#### 5. Naming convention — ALLRDC prefix enables proc grouping
`ALLRDC_usp_ars_grid_report` was chosen so "ALLRDC" searches group variants. No `usp_` filter on `/report-gen/procedures` endpoint. **More ALLRDC_ variants expected** (e.g., `ALLRDC_usp_ars_alloc_batch_summary`).

### Cross-links
[[Grid Report — dbo.usp_ars_grid_report]] · [[Reports]] · [[Data Model]] · [[project_rdc_store_vs_src]]

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

---

## MSA Master (All-RDC) — `dbo.ALLRDC_usp_ars_msa_master`

Per-RDC opening → allocation-movement → closing reconciliation. Corrects a **39% under-count** in the base `usp_ars_msa_master` caused by cross-RDC supply. Source: `backend/sql/ALLRDC_usp_ars_msa_master.sql` (deployed 2026-10-06, both HOPC866 & ARSDBPRO, sha 68e35d1fe7002500). **This proc is only correct on PROD**; on DEV it degrades gracefully to the base logic (the split table does not exist there). Related: [[MSA Master|usp_ars_msa_master]] · [[RDC store vs src]] · [[Fresh-GRT Allocation]] · [[Data Model]].

### The Under-Count Problem

The base proc groups allocations by the **store's RDC** and joins MSA grains on that RDC. Cross-RDC supply is routine — warehouses allocate to stores outside their region. When an allocation ships **from** RDC-A **to** a store in RDC-B, the base proc joins on RDC-B (store), finds no matching MSA grain at RDC-B, and **silently drops the entire allocation row** — the quantity vanishes completely from ALC-Q.

**PROD evidence (session 20261005_124520_655):**
- Base `usp_ars_msa_master`: ALC-Q = **399,553 pcs** (under-reported)
- Corrected variant: ALC-Q = **658,099 pcs** (true total)
- Missing: **258,546 pcs** (39%)
- Row count: 805 base (with empty grains) vs 834 corrected (real supply detail)

### The Fix: Group by SRC_RDC

This variant groups allocations by **SRC_RDC** (source RDC) from `ARS_ALLOC_RDC_SPLIT_HISTORY`, recovering all cross-RDC supply. The result is **complete and reconciles to the true shipped total**.

### Params, Levels, Columns (Identical to Base)

Same as `usp_ars_msa_master`:
- `@Level`, `@SESSION_ID`, `@RDC`, `@MAJ_CAT` (optional filters)
- Levels: `DETAIL`, `ARTICLE`, `GEN_CLR`, `MAJ_CAT`, `RDC`
- 28 output columns (all measures identical, same reconciliation structure)
- Ordered steps: `@Level='RDC,MAJ_CAT,GEN_CLR,DETAIL'` runs steps in sequence

### Source Map

| Column | Source |
|--------|--------|
| Opening values (PEND INT, HOLD INT, OP MSA-Q) | `ARS_MSA_TOTAL_HISTORY` (same as base) |
| Closing values (PEND AFT ALC, HOLD AFT ALC, REM MSA-Q) | live `ARS_MSA_TOTAL` (same as base) |
| **ALC-Q** | `ARS_ALLOC_RDC_SPLIT_HISTORY` · `SUM(SHIP_QTY)` grouped by **SRC_RDC** |
| **TD-HOLD ALC-Q** | `ARS_ALLOC_RDC_SPLIT_HISTORY` · `SUM(HOLD_QTY)` grouped by **SRC_RDC** |
| TD-ALC FROM HOLD_Q | `ARS_ALLOC_HISTORY` · `SUM(FROM_HOLD_QTY)` (split table lacks this column; separate CTE until FROM_HOLD_QTY is added to the split table) |

### Design & Deployment

- **One proc definition serves both servers** via `OBJECT_ID()` runtime check: if the split table exists (PROD), use it; else fall back to the base logic (DEV). Both return valid results (different behaviour, correct per server).
- Perf: PROD 18–23s (split table wider). DEV ~2.6s (fallback mode).
- Reuses shared `dbo.vw_ars_msa_master_levels` catalog view (no duplication).
- Related: [[Allocation (All-RDC) grid report|ALLRDC_usp_ars_grid_report]] (similar per-RDC design).

### Important Gotchas

1. **This proc is PROD-ONLY in correctness.** DEV environment lacks `ARS_ALLOC_RDC_SPLIT_HISTORY` (created on PROD 2026-10-03 by the `feat/central-rdc-pool` branch, not merged to `ars_v2`). The proc succeeds on both servers but returns under-counted values on DEV (graceful degradation via OBJECT_ID fallback).

2. **FROM_HOLD_QTY is incomplete.** The split table currently lacks the `FROM_HOLD_QTY` column. Until it is added (exact edit points in `rdc_split_service.py` documented), the proc reads FROM_HOLD_QTY from `ARS_ALLOC_HISTORY` via a separate CTE. When FROM_HOLD_QTY lands on the split table, fold this CTE into the main alloc logic.

3. **Deferred name resolution catch.** See [[SQL deferred name resolution]]: `CREATE OR ALTER PROCEDURE` succeeds even if the split table doesn't exist (no compile-time validation). The error only appears at EXEC. The `OBJECT_ID` fallback is the fix.

### Report Generation

Same dropdown + fan-out as the base proc: `@Level` is `FANOUT=1` in the Report Generation hub, so selecting multiple levels runs the proc once per level and writes separate files (one per RDC). Related: [[Report param registry + FANOUT]].

---

## Fresh Lorry — `dbo.usp_ars_fresh_lorry`

Fresh pool dispatch report with per-option HOLD/RELEASE verdict. Source: `backend/sql/usp_ars_fresh_lorry.sql` (deployed 2026-10-05, both HOPC866 & ARSDBPRO). Related: [[Fresh-GRT Allocation]] · [[Allocation Waterfall (Step by Step)]] · [[Data Model]].

### Purpose
Reports which options in the Fresh replenishment pool should be held (incomplete size coverage) vs released (ready for dispatch). A TBL (to-be-liquidated / slow-mover) option with **less than 30% of the size curve covered** is a dispatch stub — holds it pending more complete allocation. RL and TBC options always release.

### Output columns (26 total)
Key dispatch columns: WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR, SZ (option-size key), ALLOC_QTY. **New (2026-10-05):**
- **`OPT_TYPE`** — RL / TBC / TBL (from the allocation session)
- **`CONT`** — contribution per size (0..1 fraction, coverage of this size against the size curve)
- **`CONT_SUM`** — aggregate contribution across all sizes of the option (sum of CONT per-size)
- **`HOLD_RELEASE`** — verdict: `'HOLD'` or `'RELEASE'`

### Rule
```
HOLD_RELEASE = CASE WHEN OPT_TYPE='TBL' AND ISNULL(CONT_SUM,0) < 0.3 
               THEN 'HOLD' ELSE 'RELEASE' END
```

### Business impact (2026-10-05)
- **PROD** (session 20261004_183825_965): 52,935 lines; HOLD 6,935 (13.1%) / 13,298 pcs; RELEASE 46,000 / 211,596 pcs.
- **DEV**: 182,796 lines; HOLD 10,552 / 21,777 pcs.

### CRITICAL GOTCHAS

#### 1. CONT is repeated per alloc wave — dedup before summing
CONT is a per-size attribute that appears on **every alloc wave row** for the same (WERKS, MAJ_CAT, GEN_ART, CLR, SZ) grain. A raw `SUM(CONT)` reaches 2.0 or higher where the ceiling is 1.0, wrongly flipping HOLD verdicts to RELEASE.

**Correct approach:** `MAX(CONT)` per size-grain first (deduplicates identical values), **then** `SUM` across sizes. Verified: 447 size-grains carried 2 identical rows; zero cases where CONT differed. Same family as the existing "repeated measure, don't raw-SUM" rule (e.g., MSA_OP_Q on secondary grids).

#### 2. CONT_SUM must include WERKS grain
CONT is a 0..1 fraction; the verdict depends critically on grain. Summing by (MAJ_CAT, GEN_ART, CLR) **without WERKS** folds all 320+ stores:
- `SUM(CONT)` avg 9.11, max 164.0 — the 0..1 scale destroyed.
- Threshold 0.3 becomes meaningless; every option flips to RELEASE.

**Always** group by (WERKS, MAJ_CAT, GEN_ART, CLR) before `SUM(CONT)`. Reinforces existing [[Option grain must include WERKS]] rule.

#### 3. Source OPT_TYPE/CONT from ARS_ALLOC_HISTORY, NOT working tables
**MUST** read from `ARS_ALLOC_HISTORY` scoped to the reported SESSION_ID, **not** from `ARS_LISTING_WORKING` / `ARS_ALLOC_WORKING`.

**Why:** Working tables are overwritten by every allocation run, so they describe whatever ran LAST, not the session being reported. History tables are immutable per session.

**Hard evidence (2026-10-05):**
- **PROD**: ARS_ALLOC_WORKING held exactly the session's 358,130 rows (100% option overlap). By accident, they matched history.
- **DEV**: Latest history session 20260916_095649_565 had 985,430 rows; ARS_ALLOC_WORKING held only 27,437 from a different run (1.6% overlap). Result: **181,300 of 182,796 report rows came back with NULL OPT_TYPE**, every verdict silently fell through to RELEASE.

Switching to history columns produced **bit-identical PROD output** and **fixed DEV completely** (0 NULLs, 10,552 HOLD correct). History-sourcing is also correct for re-printing any older archived session.

### Data quality note
DEV's max CONT_SUM is 1.020 (PROD exactly 1.000) — deduplication is working, but underlying CONT values sum marginally over 100% for a few DEV options. No verdict affected (far from 0.3 threshold), but hints at a size-curve data issue in that dev session.

---

## Alloc Batch Summary — `dbo.usp_ars_alloc_batch_summary`

Allocation batch summary spanning queue dispatch, MSA availability, and store-level unmet demand. Source: `backend/sql/usp_ars_alloc_batch_summary.sql` (deployed 2026-10-06, both HOPC866 & ARSDBPRO, sha fa38511f7eecba22).

### Purpose

A single unified query combining three perspectives that previously required separate queries. Shows what a batch shipped, what MSA was available, and what store requirement remains. The key insight: **BATCH_ID is the listing history's SESSION_ID** — same run identifier — which enables the union.

### Parameters (all optional)
| Param | Meaning |
|-------|---------|
| `@BATCH_ID` | Defaults to latest by MAX(CREATED_AT) |
| `@DIV` | Filter by division |
| `@SUB_DIV` | Filter by sub-division |
| `@MAJ_CAT` | Filter by major category |
| `@STATUS` | Filter by allocation status |

### Output columns (14 total)
| Column | Source | Meaning |
|--------|--------|---------|
| BATCH_ID, DIV, SUB_DIV, STATUS, MAJ_CAT | — | Filter/group dimensions |
| CNT | ARS_ALLOC_MAJCAT_QUEUE | Count of options in batch |
| OPT_COUNT | ARS_ALLOC_MAJCAT_QUEUE | Sum of FNL_Q shipped |
| SHIP_QTY | ARS_ALLOC_MAJCAT_QUEUE | Sum of ship volume |
| HOLD_QTY | ARS_ALLOC_MAJCAT_QUEUE | Sum of hold volume |
| MSA_QTY | ARS_MSA_TOTAL | Sum(FNL_Q) available per MAJ_CAT (current, not batch-time) |
| SHIP_PCT | — | (SHIP_QTY / MSA_QTY) × 100 — soft metric, see gotcha 3c |
| MJ_REQ_REM | ARS_LISTING_WORKING_HISTORY | Per-store MIN remaining after sequential walk RL→TBC→TBL, then SUM across stores |
| STORE_CNT | — | Count of stores still unmet (MJ_REQ_REM > 0) — shows **width** of shortfall |
| STORES_SHORT | — | List of store codes still short |

### Key Semantics

**MJ_REQ_REM rule** (ties to [[Sequential MJ_REQ gate]]): During allocation, the engine walks each (WERKS, MAJ_CAT) sequentially through RL → TBC → TBL, decrementing `MJ_REQ_REM` as options ship. The per-store **minimum** is what remained unfilled at walk end. The proc takes MIN per store, then SUMs across stores for the MAJ_CAT total. Example: HAND WASH shipped only 589 pcs against 97,069 MSA with 69,939 still unmet across **299 of 457 stores** — one query surfaces width + depth that query-per-store misses.

### Validation (session 20261005_124520_655, PROD)
- **399 rows**, parameter filters verified (e.g. `@DIV=MENS` → 51 rows MENS only)
- **SHIP_QTY 658,099** / **OPT_COUNT 2,050,332** reconcile exactly to raw `ARS_ALLOC_MAJCAT_QUEUE`
- **MJ_REQ_REM 2,773,314** reconciles exactly to standalone MIN-per-store query
- 0 NULL DIV values
- Runtime: **~8–11s PROD**, 2.4s DEV

### CRITICAL GOTCHAS

#### 1. @BATCH_ID default: MAX(CREATED_AT), NOT MAX(BATCH_ID)
Batch ids are **not all sortable** as timestamps — examples in production: `'FV_B2_allON_145423'`, `'E2E_ALL_101034'`. These **sort ABOVE** a `'20261005_...'` id lexicographically. Using `MAX(BATCH_ID)` silently picks a stale test batch. **Always default to `MAX(CREATED_AT)`** to find the truly latest batch.

#### 2. vw_master_product deduplication: one (DIV, SUB_DIV) per MAJ_CAT
The view holds **2,025 distinct (MAJ_CAT, DIV, SUB_DIV)** rows against only **1,897 distinct MAJ_CAT**; CREPE, JAQUARD, IMP, NET each have 6 combinations. On this batch, measured inflation = 0 (those categories not yet in allocations), but **latent hazard**: an un-deduped DISTINCT join multiplies a multi-mapping MAJ_CAT's SHIP_QTY by the cardinality.

**Fix:** `SELECT MAX(DIV), MAX(SUB_DIV) ... GROUP BY MAJ_CAT` before joining.

#### 3. MSA_QTY is CURRENT, not batch-time availability — SHIP_PCT is soft
72 of 399 categories show MSA_QTY = 0 while shipping real volume (e.g., LS_F_AIR FRSHENR shipped 19,308 pcs vs 0 now). They have FNL_Q=0 in live `ARS_MSA_TOTAL` because `FNL_Q = MAX(STK - PEND, 0)` and the batch's allocation raised PEND afterwards.

**Interpretation**: SHIP_PCT = "shipped vs what remains now", not "vs available at batch start". A true at-batch-time denominator needs an MSA snapshot keyed to SESSION_ID (not currently available). Related: [[MSA stock source is et_msa_stk]].

#### 4. ARS_MSA_TOTAL scope: MAX(sequence_id)
The table is scoped to `sequence_id = (SELECT MAX(...) FROM ARS_MSA_SEQUENCES)` to prevent double-counting when multiple sequences coexist. Currently, exactly one sequence (376) exists, so summing all rows is correct today. The proc applies the scope to stay correct the moment a second lands.

### Cross-links
[[ARS Work Log]] · [[Sequential MJ_REQ gate]] · [[MSA stock source is et_msa_stk]] · [[Data Model]]

---

## SQL Table Delivery — App feature (2026-10-08)

A sixth **delivery option** in Report Generation (after Folder / Snowflake / Email / WhatsApp / SMS): append each step's result set into a managed SQL Server table in Rep_Data.

### Purpose & Design

Use case: event-triggered reports (e.g., "when allocation completes, append to a history table"). Saves the step of exporting CSVs just to re-import them into SQL.

**One table per step**, named per `table_prefix + step_label` (e.g., `ARS_RPT_GRID_MJ`, `ARS_RPT_MSA_MASTER`, etc.). Rows auto-stamped with `SESSION_ID` (report-run identifier) + `_LOADED_AT` timestamp. Retention window (default 5 days) auto-purges old rows in 50k batches.

### Configuration

Report form → Snowflake-like Deliver section, new **Table** chip:
- **Schema**: SQL Server schema (default `dbo`)
- **Table prefix**: appended to step labels (default `ARS_RPT_`)
- **Keep days**: retention window for auto-purge (default 5)
- **Skip file export**: checkbox. Default OFF = **both file AND SQL table** are produced (report runs once, doubles the work). Ticked = SQL table ONLY output (time-saving path for read-later workflows).

### Technical Implementation

- **NEW `backend/app/services/sql_table_export.py`**: `write_step_to_table(sql, values, cfg, session_code, step_label, cancel_token)` streams results and manages the target table.
- **Auto-create at first run** from runtime cursor description (read via pyodbc, not via `sp_describe_first_result_set` — see gotcha below).
- **Schema evolution**: subsequent runs with new columns trigger `ALTER TABLE ADD COLUMN` (type all `VARCHAR`).
- **Two connections**: separate pyodbc session for streaming (SELECT) and one for DDL/INSERT (MARS not enabled).
- **DB migration**: `ARS_REPORTS.SQLTABLE_CONFIG NVARCHAR(MAX)` added to PROD (idempotent ALTER).

### CRITICAL GOTCHAS

#### 1. sp_describe_first_result_set fails on dynamic-SQL procs

`sp_describe_first_result_set` returns "metadata could not be determined" for any proc with a dynamic SELECT (e.g., `usp_ars_grid_report`, `ALLRDC_usp_ars_msa_master`, `usp_ars_grid_var_art`). No compile-time metadata available. **Solution:** Read `cursor.description` at EXEC-time (after `cursor.execute()` but before fetch) — populated even on dynamic procs. This is why targets are engine-managed (created at first run). See [[gotcha_sp_describe_first_result_set_dynamic_sql]].

#### 2. pyodbc connection busy — MARS not enabled

Attempting DDL/INSERT while a SELECT still has an open result set raises `"Connection is busy with results for another command"`. MARS is disabled on the connection string. **Solution:** Two independent raw connections from the pool — one for streaming (SELECT), one for admin work (DDL, INSERT, purge). See [[gotcha_pyodbc_connection_busy_no_mars]].

#### 3. SESSION_ID column collision

Some output columns are legally named `SESSION_ID` (e.g., `usp_ars_grid_report.SESSION_ID` = the listing session, added 2026-08-27). The deliver engine also stamps `SESSION_ID` = report-run identifier. **Resolution:** Data column suffixed — plain `SESSION_ID` = report run, `SESSION_ID_1` = data's own column. When querying ARS_RPT_* tables, use plain `SESSION_ID` to filter by report execution, and expect data's original `SESSION_ID` as `SESSION_ID_1`. See [[gotcha_report_sql_table_session_id_collision]].

### Validation (PROD session 20261008)

- First run: created table, appended 74 rows / 28 columns
- Second run: accumulated per SESSION_ID (different report execution, same table)
- Dynamic proc (wider output): auto-widened table 28 → 90 columns, no error
- Retention: backdated 74 rows 9 days, retention_days=5 → correctly purged all 74
- Full engine run with skip_files=true: 0 files written, 74 rows in table, 1.2s end-to-end

### Cross-links
[[Report Generation Hub]] · [[ARS Work Log]] · [[Data Model]]

### Performance Results (2026-10-08) — The Headline

**Report 104** (8 steps, 2.77M rows/run): **291.2s (4m51s) writing SQL tables + zero files**, vs **7m45s** when writing CSVs. The **server-side INSERT path is 3.2x faster** than streaming. Measured **0.045 ms/row** vs streaming's **0.200 ms/row** (on 446,717 rows: 20.0s vs 89.3s write time).

**Major finding:** The 291.2s run is **86% stored-proc execution**, not delivery. Bottleneck = **395 GB ARS_LISTING_WORKING_HISTORY table (14x buffer pool)**. Page Life Expectancy = 103 seconds; nothing stays cached. **Proc tuning won't help; trimming retention** ([[project_rep_data_30day_purge]]) **is where the gains live.**

Per-step: USP_PARK_TEMP 109.6s (446k rows), ALLRDC grid RNG_SEG 103.9s (675k), MERGE_RNG_SEG 70.5s, M_YARN_02 57.6s, ALLRDC grid 29.3s, VAR_ART 19.5s, MSA 5.4s, Fresh 2.4s. Four ALLRDC calls = 261s (66% of run).

**Cache warmth lesson:** I blamed ALLRDC's CTEs for 1.6x overhead (wrong — compared COLD ALLRDC vs WARM base). Fairly measured (both warm), ALLRDC costs 1.19-1.25x only. Always warm the cache before comparing two queries on HOPC866.

---

## App modules (not stored procs)
- [[UPC Store Tracking]] — store-opening lifecycle tracker (`/reports/upc-tracking`): opening-date & remarks/status history, live MBQ/stock/SLOC/FR by segment, dispatch-lead-based Bal Days / Repl Days / **D.GAP**, priority synced to the store master.

## Cross-links
[[ARS Work Log]] · [[Report Generation Hub]] · [[UPC Store Tracking]] · [[Grid Builder]] · [[Pending Allocation and Hold]] · [[Data Model]] · [[2026-07-18]]
