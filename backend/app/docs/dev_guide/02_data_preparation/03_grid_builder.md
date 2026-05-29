# Grid Builder — Deep Dive

Dynamic pivot grids built from `ET_STORE_STOCK` × `vw_master_product`.
Each grid definition becomes one materialised output table. Multiple
grids run in batch via "Run All Active".

---

## What it does

A "grid" in this context is a pivot definition: hierarchy columns +
KPI filter + output table. The runner reads the source, pivots on
`ARS_STORE_SLOC_SETTINGS.SLOC` columns where `STATUS='ACTIVE'`, applies
post-pivot lookups, and writes the result.

## Files

| Layer | File |
|---|---|
| Page | `frontend/src/pages/GridBuilderPage.jsx` |
| API + service | `app/api/v1/endpoints/grid_builder.py` (endpoint *and* service in one file) |
| Calc helpers | `app/services/grid_calculations.py` (`calculate_per_day_sale`, etc.) |
| Permission | `GRID_VIEW` (read), `GRID_RUN` (run), `GRID_EDIT` (CRUD) |

## Lifecycle

```
1. Create grid (CRUD on ARS_GRID_BUILDER)
   ┌──────────────────────────────────────┐
   │  grid_name:       MJ                 │
   │  hierarchy_cols:  WERKS, MAJ_CAT     │
   │  kpi_filter:      STK                │
   │  output_table:    ARS_GRID_MJ        │
   │  weightage:       1.0                │
   │  grid_group:      Primary            │
   └──────────────────────────────────────┘

2. Run grid (POST /grids/<id>/run)
   ┌── _run_single_grid(grid) ────────────┐
   │  - mark Running (with retry)         │
   │  - _build_and_run_grid(de, grid):    │
   │      a. Discover active SLOCs        │
   │      b. CREATE TABLE IF NOT EXISTS   │
   │      c. ALTER add/drop SLOC cols     │
   │      d. TRUNCATE                     │
   │      e. SELECT…PIVOT INTO #stage     │
   │      f. Chunked INSERT (250k/chunk)  │
   │      g. Post-pivot lookups (LISTING) │
   │      h. Calc columns (MBQ / OPT_CNT) │
   │      i. CREATE PRIMARY KEY           │
   │  - mark Success/Failed (with retry)  │
   └──────────────────────────────────────┘
```

## Why chunked INSERT?

Single grids produce up to 9.85M rows. Logging that as one INSERT on
Azure SQL's FULL recovery model can fill the per-tier transaction log
cap → error 9002.

Solution (already implemented):

1. **Stage** the pivot to `#grid_stage_pivot` (tempdb log doesn't count
   against Rep_Data's log cap).
2. **Chunk** INSERTs at 250k rows each, committing per chunk so the
   platform's auto-backup can clear log between batches.
3. Each chunk wrapped in `_retry_on_log_full` — single 60s retry on
   transient 9002.

## SQL pattern (the actual staging + chunked insert)

```sql
-- Stage (one statement, all logged in tempdb)
;WITH Stock_CTE AS (
    SELECT <hierarchy>, STK.SLOC, STK.PARTICULARS_VALUE
    FROM dbo.ET_STORE_STOCK STK WITH (NOLOCK)
    LEFT JOIN dbo.vw_master_product MP WITH (NOLOCK) ON STK.MATNR = MP.ARTICLE_NUMBER
    INNER JOIN ARS_STORE_SLOC_SETTINGS S WITH (NOLOCK) ON STK.SLOC = S.SLOC
    WHERE UPPER(S.STATUS) = 'ACTIVE'
      AND STK.WERKS IS NOT NULL AND STK.WERKS <> ''
)
SELECT
    ROW_NUMBER() OVER (ORDER BY <hierarchy>) AS __rn,
    <hierarchy>, [SLOC1], [SLOC2], ..., [STK_TTL]
INTO #grid_stage_pivot
FROM Stock_CTE
PIVOT (SUM(PARTICULARS_VALUE) FOR SLOC IN ([SLOC1], [SLOC2], ...)) AS P;

-- Then loop in Python
FOR lo, hi in chunks (250k each):
    INSERT INTO [ARS_GRID_MJ] (cols)
    SELECT <cols> FROM #grid_stage_pivot WHERE __rn BETWEEN :lo AND :hi
    COMMIT
```

## Why ROW_NUMBER not OFFSET/FETCH?

`OFFSET N FETCH NEXT M ROWS ONLY` re-runs the entire pivot computation
on every chunk to skip N rows. With 9.85M rows × 40 chunks, that's 40
full pivots — terrible.

`ROW_NUMBER()` materialises once into the staging table, then chunks
do a fast `WHERE __rn BETWEEN ?` index seek (or scan range).

## Run All Active

```python
# grid_builder.py
@router.post("/run-all")
def run_all_active(...):
    parallelism = settings.GRID_RUN_PARALLELISM   # default 1
    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = [pool.submit(_run_single_grid, g) for g in active_grids]
        for f in as_completed(futures):
            results.append(f.result())
```

| Setting | Default | Effect |
|---|---|---|
| `GRID_RUN_PARALLELISM` | 4 | How many grids run simultaneously |
| `GRID_RUN_PARALLELISM_MAX` | 16 | Hard cap (UI / API can't exceed) |
| `GRID_INSERT_CHUNK_SIZE` | 250000 | Rows per chunk |
| `GRID_LOG_FULL_RETRY_DELAY_SEC` | 60 | Sleep before retrying 9002 |
| `GRID_LOG_FULL_RETRY_COUNT` | 1 | Number of retries |

## Common changes

### Recipe: change pivot aggregation from SUM to MAX

`_build_and_run_grid` → look for `PIVOT (SUM(...) FOR SLOC IN ...)`.
Replace `SUM` with `MAX` (or `AVG`, etc.). Watch out: downstream calc
tables assume sum semantics — audit them.

### Recipe: add a "filter MAJ_CAT" parameter to a grid

Today filtering is by KPI (PARTICULARS) only. To add MAJ_CAT filter:

1. Add `maj_cat_filter NVARCHAR(500)` column to `ARS_GRID_BUILDER`.
2. Frontend: add a multi-select MAJ_CAT field in the grid editor.
3. Backend: in the SQL builder, append `AND MP.MAJ_CAT IN (:m1, :m2)`
   to the `WHERE` clause.

### Recipe: add a post-pivot enrichment from a new master view

`_apply_post_lookups` → add a `LEFT JOIN + UPDATE`:

```sql
UPDATE g
SET g.NEW_DIMENSION = m.new_value
FROM [<output_table>] g
LEFT JOIN dbo.vw_my_new_master m ON g.MATNR = m.ARTICLE_NUMBER
WHERE g.NEW_DIMENSION IS NULL
```

If the column doesn't exist yet, `ALTER TABLE` it on first run with
`column_exists` helper from `db_helpers.py`.

### Recipe: detect grid dependencies and order Run All

Grids that consume each other's output should run in dependency order.
Today they run in `seq` order.

1. Add `depends_on NVARCHAR(MAX) NULL` column (JSON list of grid IDs).
2. In `run_all_active`, build a dependency graph and topologically
   sort. Run independent grids in parallel; dependent ones serially.

```python
def topo_sort(grids):
    # Standard Kahn's algorithm
    in_degree = {g.id: 0 for g in grids}
    for g in grids:
        for dep_id in json.loads(g.depends_on or '[]'):
            in_degree[g.id] += 1
    ready = [g for g in grids if in_degree[g.id] == 0]
    while ready:
        yield ready.pop(0)
        ...
```

### Recipe: cache pivot result if source data hasn't changed

After a successful run, store the source data's max-modified date.
On the next run, if the source's max-modified is older than the
stored value, skip the work and reuse the existing output.

```python
src_mtime = SELECT MAX(modified_at) FROM ET_STORE_STOCK
last_run_mtime = grid.last_run_source_mtime
if src_mtime <= last_run_mtime:
    log.info("Skipping grid; source unchanged since last run")
    return cached_result
```

## Performance reference

| Grid | Output rows | Wall time (chunked) |
|---|---|---|
| MJ (top-level) | ~117k | ~40 s |
| MJ_RNG_SEG | ~275k | ~80 s |
| MJ_GEN_ART | ~4.4M | ~150 s |
| MJ_VAR_ART | ~9.85M | ~140 s |

If a grid takes much longer than these benchmarks:
1. Check the source view (`vw_master_product`) for missing indexes.
2. Check the SLOC count — more SLOCs = wider PIVOT = more work.
3. Check tempdb pressure — Aggressive shrink may be running.

## Diagnostic queries during a run

```sql
-- Is the staging table populated?
SELECT COUNT(*) FROM tempdb..#grid_stage_pivot;

-- What's the connection doing?
SELECT r.session_id, r.command, r.wait_type, r.wait_time,
       r.total_elapsed_time / 1000 AS elapsed_sec,
       SUBSTRING(t.text, 1, 300) AS sql_text
FROM sys.dm_exec_requests r
OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t
WHERE r.session_id = (SELECT session_id FROM sys.dm_exec_sessions
                       WHERE host_name = 'YOUR_HOST'
                       AND program_name LIKE '%uvicorn%');

-- Log space usage (watch it oscillate during chunked inserts)
SELECT log_reuse_wait_desc,
       used_log_space_in_bytes/1048576.0 AS used_mb
FROM sys.dm_db_log_space_usage;
```
