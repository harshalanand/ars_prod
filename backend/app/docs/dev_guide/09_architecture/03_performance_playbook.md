# Performance Playbook — Developer Reference

Single source of truth for "the app feels slow — what do I check?"
Diagnose top-down: **Browser → API → Database → Platform.** Most issues
resolve at the API or DB layer; rarely is it the browser.

## Step 0 — Where is the time going?

Open **Chrome DevTools → Network** tab. Reload the page. Look for the
slowest red bar.

| Symptom | Likely culprit |
|---|---|
| One request takes 30s+ | Backend or DB query |
| Many small requests piling up | Frontend issuing waterfall calls — combine on backend |
| Request returns fast but UI is slow | React rendering — check React Profiler |
| Request hangs forever | Blocked event loop or stuck DB session |

---

## Step 1 — Browser fixes

### Pause polling when tab is hidden

```jsx
useEffect(() => {
  const tick = () => { if (!document.hidden) refetch() }
  const id = setInterval(tick, 2000)
  return () => clearInterval(id)
}, [refetch])
```

Saves DB load when 20 users have ARS open in background tabs.

### Server-side row model for big grids

Anything > 100k rows needs ag-Grid `rowModelType: 'serverSide'` or
React Virtual. Client-side row models choke past that.

### Combine N small calls into one

If a page fires 8 calls on mount, build a single `/dashboard-summary`
endpoint that returns all 8 results in one payload.

---

## Step 2 — API fixes

### 2.1 Don't block the event loop

The single most common cause of "the whole app freezes."

```python
# ❌ Wrong — sync DB call inside async route blocks every other request
@router.post("/heavy")
async def heavy(...):
    result = engine.upsert(...)   # blocks event loop for minutes

# ✅ Right — offload to thread pool
@router.post("/heavy")
async def heavy(...):
    result = await asyncio.to_thread(engine.upsert, ...)
```

### 2.2 Cache discovery queries

Page-load queries like "give me distinct values for column X" repeat
on every visit. Cache for 5 minutes:

```python
from functools import lru_cache
import time

_distinct_cache = {}

def get_distinct(table, col, ttl=300):
    now = time.time()
    key = (table, col)
    if key in _distinct_cache and now - _distinct_cache[key][0] < ttl:
        return _distinct_cache[key][1]
    values = run_query(...)
    _distinct_cache[key] = (now, values)
    return values
```

### 2.3 Run sub-queries in parallel

```python
# ❌ Sequential — 3× the latency
columns = service.get_columns()
dates   = service.get_dates()
configs = service.get_configs()

# ✅ Parallel via asyncio
columns, dates, configs = await asyncio.gather(
    asyncio.to_thread(service.get_columns),
    asyncio.to_thread(service.get_dates),
    asyncio.to_thread(service.get_configs),
)
```

### 2.4 Pool sizing

`config.py`:
- `DB_POOL_SIZE=15` baseline checked-out connections
- `DB_MAX_OVERFLOW=25` extra under load
- `DB_POOL_RECYCLE=300` Azure recommends 5-min recycle

Total = 40 max. If your peak concurrency is higher, bump these.
Symptom of too-small pool: requests timeout waiting for a connection
even when DB CPU is low.

---

## Step 3 — Database fixes

### 3.1 Make queries sargable (use indexes)

```sql
-- ❌ Non-sargable — full scan
WHERE CAST(created_at AS DATE) = '2026-04-29'
WHERE YEAR(created_at) = 2026
WHERE LEFT(name, 3) = 'ABC'
WHERE col1 + col2 > 100

-- ✅ Sargable — index seek
WHERE created_at >= '2026-04-29' AND created_at < '2026-04-30'
WHERE created_at >= '2026-01-01' AND created_at < '2027-01-01'
WHERE name LIKE 'ABC%'
WHERE col1 > 100 - col2  -- isolate the indexed col
```

### 3.2 Add the missing index

Find expensive queries:

```sql
SELECT TOP 10
  qs.execution_count,
  qs.total_elapsed_time / qs.execution_count AS avg_us,
  SUBSTRING(t.text, qs.statement_start_offset/2 + 1,
            (CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(t.text)
             ELSE qs.statement_end_offset END - qs.statement_start_offset)/2 + 1) AS query
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) t
WHERE qs.execution_count > 5
ORDER BY avg_us DESC;
```

Enable Actual Execution Plan in SSMS for slow queries; SQL Server will
suggest missing indexes. Take with a grain of salt — review before
applying.

### 3.3 Use approximate row counts

```sql
-- ❌ Full scan on a 50M row table
SELECT COUNT(*) FROM Trend_Sales_Daily;

-- ✅ Instant
SELECT SUM(p.rows) FROM sys.partitions p
JOIN sys.tables t ON p.object_id = t.object_id
WHERE t.name = 'Trend_Sales_Daily' AND p.index_id IN (0, 1);
```

### 3.4 NOLOCK on read-only views

```sql
SELECT ... FROM vw_master_product MP WITH (NOLOCK)
JOIN ET_STORE_STOCK STK WITH (NOLOCK) ON ...
```

ARS already does this for master views that rarely change during the
day. Don't use NOLOCK on transactional tables you also read with
RCSI — RCSI gives you consistent snapshot reads without explicit hints.

### 3.5 Pre-aggregate

If you compute the same SUM on the same data every day, pre-compute
it once a day into a summary table:

```sql
CREATE TABLE ARS_SALES_SUMMARY (
    REPORT_MONTH DATE,
    WERKS NVARCHAR(10),
    GEN_ART NVARCHAR(50),
    SALES_AMT FLOAT,
    QTY FLOAT,
    PRIMARY KEY (REPORT_MONTH, WERKS, GEN_ART)
);

-- Refresh nightly:
TRUNCATE TABLE ARS_SALES_SUMMARY;
INSERT INTO ARS_SALES_SUMMARY
SELECT DATEFROMPARTS(YEAR(REPORT_DATE), MONTH(REPORT_DATE), 1),
       WERKS, GEN_ART, SUM(SALES_AMT), SUM(QTY)
FROM Trend_Sales_Daily
WHERE REPORT_DATE >= DATEADD(year, -2, GETDATE())
GROUP BY DATEFROMPARTS(YEAR(REPORT_DATE), MONTH(REPORT_DATE), 1),
         WERKS, GEN_ART;
```

Reads against the summary are 100× faster than scanning the raw table.

### 3.6 Materialise heavy views

`VW_ET_MSA_STK_WITH_MASTER` is queried in 3+ places per MSA page load.
On Azure SQL DB, you can't use indexed views, but you CAN materialise
manually:

```sql
-- Run after every nightly MSA ingest
DROP TABLE IF EXISTS T_ET_MSA_STK_WITH_MASTER;
SELECT * INTO T_ET_MSA_STK_WITH_MASTER FROM VW_ET_MSA_STK_WITH_MASTER;
CREATE INDEX IX_T_MSA_DATE ON T_ET_MSA_STK_WITH_MASTER([DATE]);
```

Then change the consuming code to use the table name. Page-load times
drop from 30s to <1s.

---

## Step 4 — Platform fixes (Azure SQL)

### 4.1 RCSI is on

```sql
SELECT name, is_read_committed_snapshot_on
FROM sys.databases WHERE name IN ('Claude','Rep_Data');
```

Both should show `1`. If `0`, readers and writers will block each other
horribly.

### 4.2 Transient errors auto-recover

`config.py` adds these to every connection string:

```
ConnectRetryCount=5;
ConnectRetryInterval=10;
Connection Timeout=60;
```

Driver auto-retries 5× over ~50s on Azure transient errors (40613,
40501, 49918, serverless wakeup).

### 4.3 9002 — log full

Already detailed in `03_how_to_add_a_grid.md` and the post-mortem
notes. Quick recovery:

```sql
-- Find the culprit
SELECT TOP 5 s.session_id, s.host_name, s.program_name,
             at.transaction_begin_time
FROM sys.dm_tran_active_transactions at
JOIN sys.dm_tran_session_transactions st ON at.transaction_id = st.transaction_id
JOIN sys.dm_exec_sessions s ON st.session_id = s.session_id
WHERE s.is_user_process = 1
ORDER BY at.transaction_begin_time;

KILL <session_id>;
```

Auto-resolve setting `AUTO_RESOLVE_LOG_BACKUP_WAIT=true` flips a DB to
SIMPLE recovery if log truncation gets stuck.

### 4.4 tempdb pressure

`/api/v1/tempdb/top-sessions` shows who's burning tempdb.

```sql
SELECT TOP 10 r.session_id,
       (s.user_objects_alloc_page_count - s.user_objects_dealloc_page_count) * 8/1024 AS alloc_mb,
       SUBSTRING(t.text, 1, 200) AS sql_text
FROM sys.dm_db_session_space_usage s
JOIN sys.dm_exec_sessions ses ON s.session_id = ses.session_id
LEFT JOIN sys.dm_exec_requests r ON s.session_id = r.session_id
OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t
WHERE ses.is_user_process = 1
ORDER BY alloc_mb DESC;
```

If a single session is holding 10+ GB, kill it.

---

## Common slow-query patterns to watch for

| Pattern | Why it's slow | Fix |
|---|---|---|
| `SELECT *` | Returns more data than needed; pollutes plan cache | Select only the columns you need |
| `WHERE date_col = CAST(... AS DATE)` | Non-sargable | Use range comparison |
| `LEFT JOIN large_table` you only need rows from | Kicks plan into hash join | Use `INNER JOIN` or `EXISTS` |
| `OR` on different indexed columns | Forces scan | Rewrite as `UNION ALL` |
| `TOP N ... ORDER BY non_indexed_col` | Sort blows out memory | Add covering index on the sort col |
| `DISTINCT` on wide rows | Distinct on every column | `GROUP BY` only the cols that need to be distinct |
| Implicit type conversion (`WHERE int_col = '5'`) | Often forces scan | Match types in your params |

---

## When all else fails

1. **Restart the backend.** Clears any stuck connections.
2. **Check Azure SQL portal** for DTU / vCore saturation. If pinned,
   tune queries or upgrade tier.
3. **Look at the audit_log size.** A 50M-row audit table makes every
   write slower. Archive.
4. **Check tempdb.** If it's hit `DB_TEMPDB_AGGRESSIVE_THRESHOLD_MB`,
   the cleanup daemon will fire — but a too-fast-growing tempdb
   indicates a job using too many temp tables; review `_bulk_upsert`
   batch sizes and grid chunk sizes.
5. **Profile the slowest endpoint with cProfile** in a dev environment.
   Almost always the answer is a single missing index.
