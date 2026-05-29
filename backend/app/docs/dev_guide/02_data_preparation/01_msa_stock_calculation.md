# MSA Stock Calculation

The engine room of ARS. Turns raw stock + pending allocations into the
three "ready" tables that drive every downstream allocation decision:
**ARS_MSA_TOTAL**, **ARS_MSA_GEN_ART**, **ARS_MSA_VAR_ART**.

If MSA is wrong or stale, every allocation is wrong.

---

## The 9-step algorithm (logic explained)

```
SOURCE: ET_MSA_STK + MASTER_ALC_PEND + vw_master_product
                          │
                          ▼
   1. FILTER SLOC          Keep only configured + active SLOCs
                          (ARS_STORE_SLOC_SETTINGS.STATUS='ACTIVE')
                          │
                          ▼
   2. NORMALIZE            Trim, uppercase identifiers; coalesce
                          quantity columns to 0; fill text NULLs to 'NA'.
                          │
                          ▼
   3. FILL DIMS            Every row has a complete (RDC, MAJ_CAT, GEN_ART,
                          VAR_ART, CLR) tuple. ISNULL(MP.col, fallback).
                          │
                          ▼
   4. SEG = [APP, GM]      Apparel + General Merchandise are the only two
                          tracked. Others (HOME, FOOD) are ignored.
                          │
                          ▼
   5. PIVOT BY SLOC        Pivot the (RDC, MAJ_CAT, ..., SLOC, QTY) into
                          one column per SLOC. STK_TTL = sum across SLOCs.
                          │
                          ▼
   6. MERGE PEND_ALC       LEFT JOIN MASTER_ALC_PEND on (RDC, MATNR) so
                          we know what's already promised.
                          │
                          ▼
   7. FNL_Q = max(STK-PEND, 0)   "Available to allocate" = stock minus
                                  pending; never negative.
                          │
                          ▼
   8. GEN COLOR VARIANTS   Explode VAR_ART rows per CLR. Each VAR_ART
                          becomes (VAR_ART, CLR_RED), (VAR_ART, CLR_BLUE), …
                          │
                          ▼
   9. AGGREGATE            Roll up to three grains:
                          - ARS_MSA_TOTAL    by (RDC, MAJ_CAT)
                          - ARS_MSA_GEN_ART  by (RDC, MAJ_CAT, GEN_ART)
                          - ARS_MSA_VAR_ART  by (RDC, MAJ_CAT, GEN_ART, VAR_ART, CLR)
```

`ST_CD` was renamed to `RDC` (Regional Distribution Centre) across all
three output tables in April 2026.

## Files

| Layer | File |
|---|---|
| Page | `frontend/src/pages/MSAStockCalculationPage.jsx` |
| Service | `app/services/msa_service.py` |
| API | `app/api/v1/endpoints/msa_stock.py` |
| Pipeline (parallel) | `app/api/v1/endpoints/pipeline.py` (replaces 20-machine Excel run) |

## End-to-end flow

```
Browser
  │ on mount: GET /msa-stock/columns
  │  ── returns: { columns, dates, filter_configs, data_date }
  │
  │ user picks filters (cascading: ST_CD → SLOC → DIV)
  │ each cascade: GET /msa-stock/distinct?col=...&date=...&filters=...
  │
  │ user clicks Calculate MSA
  │ POST /msa-stock/calculate { date, filters, threshold }
  ▼
Backend
  │ MSAService.run_msa(date, filters, threshold):
  │   step 1: SLOC filter
  │   step 2: normalize
  │   ...
  │   step 9: aggregate + write 3 output tables
  │
  ▼
Three output tables ready for Allocation
```

## Files = where each step lives

| Step | Function in msa_service.py |
|---|---|
| 1 — filter SLOC | `_filter_active_slocs(df)` |
| 2 — normalize | `_normalize(df)` |
| 3 — fill dims | `_fill_master_dims(df)` |
| 4 — SEG = [APP,GM] | `_seg_filter(df)` |
| 5 — pivot by SLOC | `_pivot_by_sloc(df)` |
| 6 — merge PEND_ALC | `_merge_pending(df)` |
| 7 — FNL_Q | `_compute_fnl_q(df)` |
| 8 — color variants | `_explode_color_variants(df)` |
| 9 — aggregate | `_aggregate(df)` |

(Names approximate — search the file for the actual symbols.)

## Example: trigger MSA from CLI

```bash
curl -X POST "$API/api/v1/msa-stock/calculate" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "date": "2026-04-29",
    "filters": {
      "ST_CD": ["DH24","DH25","DH26"],
      "SLOC":  ["V01","V02_FRESH"],
      "SEG":   ["APP","GM"]
    },
    "threshold": 1
  }'
```

Returns a job_id you can poll. Output tables are populated when status='completed'.

## Page-load performance — the 30-seconds-blank problem

The original code does FOUR sequential queries on page open:

```sql
-- 1. column list — uses SELECT TOP 1 * which forces full view materialization
SELECT TOP 1 * FROM VW_ET_MSA_STK_WITH_MASTER

-- 2. distinct dates — non-sargable CAST = full scan over the view
SELECT DISTINCT CAST([DATE] AS DATE) AS d
FROM VW_ET_MSA_STK_WITH_MASTER
WHERE [DATE] IS NOT NULL ORDER BY d DESC

-- 3. max date — same CAST problem
SELECT MAX(CAST([DATE] AS DATE)) FROM ET_MSA_STK WHERE [DATE] IS NOT NULL

-- 4. filter configs — fast
SELECT * FROM dbo.MSA_Filter_Config
```

Add the auto-load of the first preset → +3 parallel `SELECT DISTINCT
[col]` queries, again over the heavy view.

### Fixes (in priority order)

1. **Drop CAST in date queries** — query `[DATE]` directly. From 30s → <1s.
2. **Use INFORMATION_SCHEMA.COLUMNS** for column discovery — no view materialization.
3. **Cache** columns + dates + configs for 5 min in process.
4. **Don't auto-load the first preset** — only auto-load if `is_last_used=1`.
5. **Parallelize** the 3 sub-queries via `asyncio.gather(to_thread(...))`.
6. **Materialize** `VW_ET_MSA_STK_WITH_MASTER` as a refreshed table —
   the killer fix, turns minutes into ms across many endpoints.

### Verification SQL

```sql
SET STATISTICS TIME ON;
-- BEFORE
SELECT DISTINCT CAST([DATE] AS DATE) FROM VW_ET_MSA_STK_WITH_MASTER WHERE [DATE] IS NOT NULL;
-- AFTER (drop the cast)
SELECT DISTINCT [DATE]                FROM VW_ET_MSA_STK_WITH_MASTER WHERE [DATE] IS NOT NULL;
```

A 10–50× speedup on the second query proves the cast was the killer.

## Common changes

### Recipe: add a new SEG (e.g. NB = Non-Branded)

`msa_service.py` → `_seg_filter` → change:
```python
SEGS = ['APP', 'GM']
```
to:
```python
SEGS = ['APP', 'GM', 'NB']
```

Make sure source data has SEG=NB rows; otherwise pivot has empty
columns.

### Recipe: change the threshold semantics

The `threshold` param currently filters out rows with FNL_Q < threshold.
To switch to "rows where contribution share < threshold":

`_aggregate` → after computing share per article:
```python
mask = shares < threshold_pct
df = df.loc[~mask]
```

Document the change in the API param.

### Recipe: support DIV-level output

Add `DIV` to the aggregation grain:
```python
ARS_MSA_DIV = aggregate(by=['RDC','MAJ_CAT','DIV'])
```
Add a fourth output table. Update the page to show it. Update
allocations to consume it if useful.

### Recipe: parallelize across MAJ_CATs

`pipeline.py` already does this — splits the MAJ_CATs into N chunks
and runs MSA per chunk on separate workers. Replaces the 20-machine
Excel process. Look at `pipeline.py:run_pipeline` for the orchestration.

## What MSA touches in the DB

| Source | Read | Notes |
|---|---|---|
| `ET_MSA_STK` | yes | Main fact table — stock per (store, sloc, article) |
| `MASTER_ALC_PEND` | yes | Pending allocations |
| `ARS_STORE_SLOC_SETTINGS` | yes | Active-SLOC filter |
| `vw_master_product` | yes | Article master enrichment |
| `VW_ET_MSA_STK_WITH_MASTER` | yes | Pre-joined view (for page-load discovery) |
| `MSA_Filter_Config` | yes | Saved filter presets |
| **Output:** `ARS_MSA_TOTAL` | **write** | Truncate + insert each run |
| **Output:** `ARS_MSA_GEN_ART` | **write** | |
| **Output:** `ARS_MSA_VAR_ART` | **write** | |

## When MSA produces unexpected results

| Symptom | First place to check |
|---|---|
| Empty output table | Was the SLOC filter too narrow? Was SEG=[APP,GM] missing in source? |
| FNL_Q negative for some rows | Step 7 should clamp to 0 — check `_compute_fnl_q` math |
| Same article, two output rows | Step 8 (color variants) duplicating? Or PK missing on output? |
| Numbers don't match Excel | Step ordering changed — `MSA_Calculation_Sequence` table can override |

## Index recommendations

```sql
CREATE INDEX IX_ET_MSA_STK_DATE ON ET_MSA_STK([DATE]);
CREATE INDEX IX_ET_MSA_STK_RDC_MATNR ON ET_MSA_STK([RDC],[MATNR]);
CREATE INDEX IX_MASTER_ALC_PEND_PK ON MASTER_ALC_PEND([RDC],[MATNR]);
```

## Saved filter presets

`MSA_Filter_Config` table stores named preset (e.g. `MENS_DH24_apparel`).
Loading a preset auto-populates the page filters. The `is_last_used`
flag identifies the user's most-recent preset (for auto-load).

```sql
-- Save a preset programmatically:
INSERT INTO MSA_Filter_Config (config_name, filter_columns, filter_values, sql_agg, created_at, is_last_used)
VALUES ('MENS_FRESH', '["ST_CD","SLOC","SEG"]', '{"ST_CD":["DH24"],"SLOC":["V02_FRESH"],"SEG":["APP"]}', 1, GETDATE(), 1);
```
