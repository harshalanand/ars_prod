# How to Add or Change a Grid (Grid Builder)

Grids are dynamic pivot definitions stored in `ARS_GRID_BUILDER`. The
runtime turns each grid into one materialised output table.

## Lifecycle

```
User clicks "New Grid"  →  POST /api/v1/grid-builder/grids
                                ↓
                          row in ARS_GRID_BUILDER
                                ↓
User clicks Run         →  POST /api/v1/grid-builder/grids/{id}/run
                                ↓
                          _build_and_run_grid:
                          1. CREATE TABLE IF NOT EXISTS
                          2. ALTER TABLE — add/drop columns for active SLOCs
                          3. TRUNCATE
                          4. SELECT ... PIVOT INTO #grid_stage_pivot
                          5. INSERT INTO target  (chunked, 250k/chunk)
                          6. Post-pivot lookups, calc columns
                          7. CREATE PRIMARY KEY
```

## Where the code lives

- **Routes:** `backend/app/api/v1/endpoints/grid_builder.py`
- **Output table:** named in the grid row's `output_table` field
- **Source data:** `dbo.ET_STORE_STOCK` × `dbo.vw_master_product`
- **SLOC config:** `ARS_STORE_SLOC_SETTINGS` (only Active SLOCs become
  pivot columns)

## To add a new grid

Use the UI (Grid Builder → New Grid). Fields:

- **Grid name** — must be unique
- **Hierarchy columns** — pivot row dimensions (e.g. `WERKS`, `MAJ_CAT`,
  `GEN_ART`)
- **KPI filter** — optional, restricts which `PARTICULARS` are pulled
- **Output table** — letters/digits/underscores only, becomes the SQL
  table name
- **pivot_only** — skip the post-pivot lookup + MBQ/OPT_CNT step

## To change pivot or aggregation logic

`grid_builder.py` → `_build_and_run_grid` → look for the staging SQL
(currently a `SELECT ... PIVOT INTO #grid_stage_pivot`). The PIVOT
clause is built dynamically from active SLOCs. The aggregation function
is `SUM(PARTICULARS_VALUE)` — change at your peril, downstream calc
tables assume sum semantics.

## To add a post-pivot enrichment (e.g. join to a new master)

`grid_builder.py` → `_apply_post_lookups`. Add another `LEFT JOIN +
UPDATE` block. Skip it for article-level grids by passing
`skip_cont=True`.

## Performance: log pressure

Each grid produces millions of rows. Azure SQL has a per-tier transaction
log cap. Controls in `app/core/config.py`:

| Setting | Purpose |
|---|---|
| `GRID_RUN_PARALLELISM` | How many grids run in parallel in "Run All Active" |
| `GRID_INSERT_CHUNK_SIZE` | Rows per chunked INSERT (250k default) |
| `GRID_LOG_FULL_RETRY_DELAY_SEC` | Sleep before retrying after 9002 |
| `AUTO_RESOLVE_LOG_BACKUP_WAIT` | If True, auto-flip FULL→SIMPLE when log truncation is blocked |

If you see error 9002 ("transaction log full"), check the **holdup LSN**
in the error message — same LSN across multiple errors = a stuck
transaction. Find and KILL it:

```sql
SELECT TOP 5 s.session_id, s.host_name, s.program_name,
             at.transaction_begin_time,
             DATEDIFF(SECOND, at.transaction_begin_time, GETDATE()) AS age_sec
FROM sys.dm_tran_active_transactions at
JOIN sys.dm_tran_session_transactions st ON at.transaction_id = st.transaction_id
JOIN sys.dm_exec_sessions s ON st.session_id = s.session_id
WHERE s.is_user_process = 1
ORDER BY at.transaction_begin_time;

KILL <session_id>;
```

## Verification

1. Click Run on the grid; watch the Logs panel for the chunked-INSERT
   progress lines.
2. After completion, run `SELECT COUNT(*) FROM <output_table>` — should
   match the `last_run_rows` shown in the grid list.
3. Confirm the output table has a primary key (post-build step).
