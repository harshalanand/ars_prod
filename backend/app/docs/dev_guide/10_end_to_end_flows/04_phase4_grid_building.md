# Phase 4 — Grid Building

MSA gives you stock at three grains. Grid Builder lets planners ask
"how does stock break down across SLOCs for these dimensions?" by
producing custom pivot tables (one per grid definition).

---

## Goal

For every grid in `ARS_GRID_BUILDER` with `status='Active'`, produce
the materialised output table the grid defines, with current SLOC
columns and accurate row count.

## Inputs

- Phase 3 complete (MSA outputs fresh).
- `ET_STORE_STOCK` fresh (Phase 1).
- `ARS_STORE_SLOC_SETTINGS` reflects current active SLOCs.
- `ARS_GRID_BUILDER` rows define what to build.

## Outputs

| Table | Created by | Used by |
|---|---|---|
| `ARS_GRID_<name>` | One per grid row | Listing (Phase 5), allocation rules |
| `ARS_GRID_HIERARCHY` | Auto-managed | Cross-grid joins |

---

## Master flowchart

```
┌────────────────────────────────────────┐
│   user clicks Run All Active           │
│   POST /grid-builder/run-all           │
└──────────────────┬─────────────────────┘
                   │
                   ▼
   ┌──────────────────────────────────┐
   │ Read ARS_GRID_BUILDER WHERE      │
   │ status='Active' ORDER BY seq, id │
   └──────────────────┬───────────────┘
                      │
                      ▼
   ┌──────────────────────────────────┐
   │ Build pre-grid calc table once    │
   │ (calculate_per_day_sale)         │
   └──────────────────┬───────────────┘
                      │
                      │ ThreadPoolExecutor with workers=
                      │ settings.GRID_RUN_PARALLELISM (4 default)
                      ▼
   ┌──────────────────────────────────┐
   │ For each grid (parallel):        │
   │                                   │
   │   _run_single_grid(grid):        │
   │     ┌─────────────────────────┐  │
   │     │ A. mark Running          │  │
   │     │    (UPDATE with retry)   │  │
   │     ├─────────────────────────┤  │
   │     │ B. _build_and_run_grid:  │  │
   │     │    1) discover active     │  │
   │     │       SLOC columns        │  │
   │     │    2) CREATE TABLE IF    │  │
   │     │       NOT EXISTS          │  │
   │     │    3) ALTER add/drop     │  │
   │     │       SLOC columns        │  │
   │     │    4) TRUNCATE TABLE     │  │
   │     │    5) SELECT…PIVOT INTO  │  │
   │     │       #grid_stage_pivot   │  │
   │     │    6) Chunked INSERT     │  │
   │     │       (250k/chunk)        │  │
   │     │    7) Post-pivot lookups │  │
   │     │       (LISTING + CONT)    │  │
   │     │    8) Calc columns        │  │
   │     │       (MBQ, OPT_CNT)      │  │
   │     │    9) DROP + CREATE PK   │  │
   │     ├─────────────────────────┤  │
   │     │ C. mark Success/Failed   │  │
   │     │    (UPDATE with retry)   │  │
   │     └─────────────────────────┘  │
   └──────────────────────────────────┘
                      │
                      ▼
        Per-grid output tables ready (1k - 10M rows each)
```

---

## Step-by-step (one grid in detail)

### Step 1 — Discover active SLOCs

```sql
SELECT SLOC FROM ARS_STORE_SLOC_SETTINGS
WHERE UPPER(STATUS) = 'ACTIVE'
ORDER BY SLOC;
-- Returns: V01, V02_FRESH, V04, V06, V11, V13, V21, ...
```

These become the columns the pivot produces. Add a SLOC tomorrow → it
gets a column on the next grid run.

### Step 2 — Reconcile output table schema

`ALTER TABLE ADD` for any new SLOC. `ALTER TABLE DROP COLUMN` for
SLOCs that became inactive since last run. This keeps the output
column set in sync with current configuration.

```python
# grid_builder.py:_build_and_run_grid (around line 943-956)
for s in active_slocs:
    if s.upper() not in existing_cols:
        run(conn, f"ALTER TABLE [{out_table}] ADD [{s}] FLOAT NULL")
for col_upper, col_actual in existing_cols.items():
    if col_upper not in expected_cols_upper:
        run(conn, f"ALTER TABLE [{out_table}] DROP COLUMN [{col_actual}]")
```

### Step 3 — Stage pivot result into temp

The single big SELECT…PIVOT writes to a `#temp` table on tempdb (whose
log doesn't count against Rep_Data's log cap). One ROW_NUMBER ordering
column drives later chunked INSERTs.

```sql
;WITH Stock_CTE AS (
    SELECT <hierarchy_cols>, STK.SLOC, STK.PARTICULARS_VALUE
    FROM dbo.ET_STORE_STOCK STK WITH (NOLOCK)
    LEFT JOIN dbo.vw_master_product MP WITH (NOLOCK)
        ON STK.MATNR = MP.ARTICLE_NUMBER
    INNER JOIN ARS_STORE_SLOC_SETTINGS S WITH (NOLOCK)
        ON STK.SLOC = S.SLOC
    WHERE UPPER(S.STATUS) = 'ACTIVE'
      AND STK.WERKS IS NOT NULL AND STK.WERKS <> ''
)
SELECT
    ROW_NUMBER() OVER (ORDER BY <hierarchy_cols>) AS __rn,
    <hierarchy_cols>,
    [V01], [V02_FRESH], [V04], ...,
    <sum_of_all_slocs> AS STK_TTL
INTO #grid_stage_pivot
FROM Stock_CTE
PIVOT (SUM(PARTICULARS_VALUE) FOR SLOC IN ([V01],[V02_FRESH],[V04],...)) AS P;
```

### Step 4 — Chunked INSERT into final table

Each chunk = 250k rows, committed independently:

```sql
-- 250k row 'window' per loop iteration
INSERT INTO [ARS_GRID_<name>] (<all_cols>)
SELECT <all_cols>
FROM #grid_stage_pivot
WHERE __rn BETWEEN :lo AND :hi;
COMMIT;
```

For a 9.85M-row grid, 40 chunks. Each commit lets Azure SQL's auto-log-backup
clear the transaction log between chunks.

### Step 5 — Post-pivot lookups

LEFT JOIN against `LISTING_MASTER` (for eligibility flag) and other
masters, then UPDATE the grid output. Done in-place.

```sql
UPDATE g
SET g.LISTED = ISNULL(L.IS_LISTED, 0)
FROM [ARS_GRID_<name>] g
LEFT JOIN ARS_LISTING_MASTER L
    ON g.WERKS = L.STORE_CODE AND g.MATNR = L.ARTICLE_NUMBER;
```

### Step 6 — Calc columns

For grids with `use_for_opt_sale=true`, compute MBQ (minimum buy
quantity) and OPT_CNT (optimal count). These flow back into Listing
Phase 5.

```python
# grid_calculations.py
df['PER_DAY_SALE'] = df['SALES_LAST_30D'] / 30.0
df['OPT_CNT'] = (df['PER_DAY_SALE'] * 7).round()
df['MBQ'] = df[['MIN_BUY_QTY','OPT_CNT']].min(axis=1)
```

### Step 7 — Build primary key

Adding the PK at the END (not at table-create time) is intentional —
inserting into a heap is faster than maintaining a clustered index
during bulk INSERT.

```sql
ALTER TABLE [ARS_GRID_<name>]
ADD CONSTRAINT [PK_ARS_GRID_<name>] PRIMARY KEY (<hierarchy_cols>);
```

If the build had duplicates, this fails. The cleanup-dedupe step
(implemented in `_build_and_run_grid` lines ~1062-1073) keeps the
highest-STK_TTL duplicate.

---

## Examples

### Run a single grid via API

```bash
GRID_ID=42
curl -X POST "$API/api/v1/grid-builder/grids/$GRID_ID/run" \
  -H "Authorization: Bearer $TOKEN"
```

Response:
```json
{
  "success": true,
  "data": { "grid_name":"MJ", "status":"Success", "rows":117615, "duration":41.2 }
}
```

### Run all active

```bash
curl -X POST "$API/api/v1/grid-builder/run-all" \
  -H "Authorization: Bearer $TOKEN"
```

Watch progress in Jobs Dashboard or:

```sql
SELECT grid_name, last_run_status, last_run_rows,
       duration_sec, last_run_at
FROM ARS_GRID_BUILDER
ORDER BY seq;
```

### Inspect output

```sql
-- Which SLOCs ended up as columns?
SELECT name FROM sys.columns
WHERE object_id = OBJECT_ID('ARS_GRID_MJ')
ORDER BY column_id;

-- Top 10 by total stock
SELECT TOP 10 WERKS, MAJ_CAT, V01, V02_FRESH, V04, STK_TTL
FROM ARS_GRID_MJ
ORDER BY STK_TTL DESC;
```

---

## What can go wrong

### Failure A — Error 9002 mid-run

See Phase 4 history. Caused by Azure SQL log cap exceeded. The chunked
INSERT + retry handles transient cases; persistent ones mean a stuck
external transaction holding the log truncation point.

### Failure B — Duplicate PK error after build

Cause: source data had two stock rows with same `(WERKS,MATNR,SLOC)` —
should never happen but does. The dedupe step keeps highest STK_TTL,
but if you've disabled that step, the PK creation fails.

### Failure C — Run All takes hours

Causes (in priority):
1. Parallelism too high → log pressure → 9002s eat retry budgets.
2. Source view (`vw_master_product`) lacks indexes.
3. tempdb is full — check `dm_db_session_space_usage`.
4. One grid is hung; the rest wait their turn.

---

## Performance benchmarks

| Grid type | Rows | Wall time |
|---|---|---|
| Top-level (e.g. MJ) | ~117k | 30-50 s |
| Range × Segment | ~275k | 60-90 s |
| Macro vendor | ~460k | 30-50 s |
| Micro vendor | ~560k | 90-120 s |
| Fabric | ~457k | 35-50 s |
| Color | ~1.3M | 150-220 s |
| Vendor code | ~822k | 70-100 s |
| GEN_ART level | ~4.4M | 130-160 s |
| VAR_ART level | ~9.85M | 130-180 s |

---

## When this phase is healthy

- Every active grid shows `last_run_status='Success'`.
- `last_run_rows > 0` and within ±10% of yesterday.
- Total Run All wall time < ~25 min for default 9 grids.
