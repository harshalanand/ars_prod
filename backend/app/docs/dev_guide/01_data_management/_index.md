# Data Management — Developer Reference

The Data Management section in the sidebar is the data-engineer's toolbox.
Six pages: **All Tables, Create Table, Upload Data, Export Data, Jobs
Dashboard, Data Editor**. They share one philosophy: every read or write
goes through a router → service → SQLAlchemy/pyodbc connection on the
data engine, with audit trail.

## Data flow at a glance

```
                    Browser (React)
                         │
                  HTTP /api/v1/...
                         ▼
   ┌──────────────────────────────────────────┐
   │  endpoints/  ── route file + permission   │
   │  services/   ── business logic            │
   │  upsert_engine / data_engine ── SQL exec  │
   │  audit_log   ── every write logged        │
   └──────────────────────────────────────────┘
                         │
                  Azure SQL (Rep_Data)
```

---

## 6.1 All Tables — `frontend/src/pages/AllTablesPage.jsx`

Lists every table the user can read with row counts and "open in" actions.

### Files

| File | Purpose |
|---|---|
| `frontend/src/pages/AllTablesPage.jsx` | The page |
| `backend/app/api/v1/endpoints/tables.py` | `GET /tables` |
| `backend/app/services/table_mgmt_service.py` | Business logic |

### How row counts are fetched

We never use `SELECT COUNT(*)` on big tables — that's a full scan. We use
`sys.partitions` for an approximate count (instant, accurate within a
heartbeat):

```sql
SELECT t.name, ISNULL(SUM(CASE WHEN p.index_id IN (0,1) THEN p.rows END), 0) AS row_count
FROM sys.tables t
LEFT JOIN sys.partitions p ON t.object_id = p.object_id
GROUP BY t.name
```

### Common change: hide system tables from the list

`tables.py` → the listing query → add a `WHERE t.name NOT LIKE 'sys%'`
filter, or drive it from a config list.

---

## 6.2 Create Table — `frontend/src/pages/CreateTablePage.jsx`

Lets an admin design a table interactively, then runs `CREATE TABLE` on
the data DB.

### Permission required

`TABLE_CREATE` — defined in `app/core/permissions.py`, granted via the
roles UI.

### Backend route

```python
# app/api/v1/endpoints/tables.py
@router.post("/tables")
def create_table(payload: CreateTablePayload, db = Depends(get_data_db)):
    ...
```

### Example: programmatically create a table

```bash
curl -X POST "$API/api/v1/tables" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "table_name": "my_test_table",
    "columns": [
      {"name":"id","type":"BIGINT","nullable":false,"primary_key":true},
      {"name":"sku","type":"NVARCHAR(50)","nullable":false},
      {"name":"qty","type":"FLOAT","nullable":true}
    ]
  }'
```

### Conventions to follow

- Default new string columns to `NVARCHAR(500)`, **never** `NVARCHAR(MAX)`
  (LOB types defeat fast_executemany during upserts).
- Always declare a primary key — the upsert engine needs it.
- Add the indexes you'll filter on **immediately** after creation.
  Heaps with no index = full scans on every query.

---

## 6.3 Upload Data — the most-used screen

Send a CSV/XLSX file, choose a target table and PK columns, click Upsert.

### Two flavours

| Flavour | Endpoint | When to use |
|---|---|---|
| **Foreground** | `POST /api/v1/upload/` | Files < ~50k rows; you watch the result live. |
| **Background** | `POST /api/v1/upload/async` | Files > 100k rows. User can close the browser. |

### Full file map

| File | Role |
|---|---|
| `app/api/v1/endpoints/upload.py` | Receives the multipart upload, dispatches to service |
| `app/services/file_upload_service.py` | Reads file, cleans data, calls engine |
| `app/services/upload_job_service.py` | Background queue + worker thread + cancel |
| `app/services/upsert_engine.py` | The actual SQL — staging, MERGE, audit |

### What "clean" means

`file_upload_service._clean_dataframe`:

| Cell value | Becomes | Effect |
|---|---|---|
| empty / blank | `__SKIP__` | Engine keeps existing DB value |
| `'-'` or `'\|'` | `__NULL__` | Engine sets the column to NULL |
| `'NA'` | preserved as `'NA'` | Stored as the literal string |
| anything else | passes through | Standard upsert |

Add a marker by editing this function — the engine's MERGE auto-handles
`__SKIP__` and `__NULL__`, so no engine change needed for synonyms.

### The fast path (for >1000 rows)

`upsert_engine._bulk_upsert`:

```python
# 1. Stage in temp table (NVARCHAR(4000) — no LOB)
CREATE TABLE #bulk_stage_<batch_id> ([col1] NVARCHAR(4000) NULL, ...)

# 2. Bulk-insert in batches of 20,000 (one TDS round-trip per batch)
cursor.fast_executemany = True
cursor.executemany(insert_sql, rows[i:i+20000])

# 3. ONE big UPDATE — change-detected with NVARCHAR comparison
UPDATE t WITH (ROWLOCK) SET ... FROM target t INNER JOIN #stage s ON t.pk = s.pk
WHERE (any column changed)

# 4. ONE big INSERT — for new rows
INSERT INTO target WITH (ROWLOCK) SELECT ... FROM #stage WHERE NOT EXISTS (...)

# 5. Drop staging
```

### Example: tune the staging batch

`app/services/upsert_engine.py` → `_bulk_upsert` → `batch_size`. We use
20,000. Sweet spot for Azure SQL across the public internet. Memory
scales `batch_size × col_count × 8 KB`. For a 30-col table that's about
~5 GB notional with NVARCHAR(MAX) — **don't** go to MAX columns.

### Recovery from a stuck job

If a background job sits "running" forever (worker crashed):

```sql
UPDATE upload_jobs
SET status='failed', completed_at=GETDATE(),
    error_message='Stale running job — auto-cleared'
WHERE status='running'
  AND started_at < DATEADD(minute, -60, GETDATE())
  AND processed_rows = 0;
```

---

## 6.4 Export Data — `frontend/src/pages/ExportPage.jsx`

Pull data back out as CSV/XLSX with filters and column selection.

### Files

| File | Role |
|---|---|
| `frontend/src/pages/ExportPage.jsx` | UI — filter builder, column picker, preview |
| `app/api/v1/endpoints/data_ops.py` (export routes) | Submit job, fetch download |
| `app/services/upload_job_service.py` (shared) | Background processing |

### Filter operators supported

`equals, notEqual, contains, startsWith, endsWith, greaterThan, lessThan,
between, in, blank, notBlank` — applied server-side with parameter
binding (no SQL injection risk).

### Example: pull all stock for store DH24

```js
// Frontend
exportAPI.submit({
  table: 'ET_STORE_STOCK',
  columns: ['WERKS','MATNR','SLOC','PARTICULARS_VALUE'],
  filters: [{ column: 'WERKS', op: 'equals', value: 'DH24' }],
  format: 'csv'
})
```

### Performance notes

- Distinct-value lookups for filter dropdowns can be slow on
  high-cardinality columns. Cache for 5 min in production.
- For exports >500k rows, default to CSV (XLSX is single-threaded and
  has hard 1M row/sheet limit).

---

## 6.5 Jobs Dashboard — `frontend/src/pages/JobsPage.jsx`

Real-time view of every background job (uploads, exports, MSA, contrib,
grid builds). Filter by status, drill in for logs, cancel/delete.

### Job lifecycle

```
queued → running → (completed | failed | cancelled)
```

### Implementation details

| Concern | Where |
|---|---|
| Job table | `upload_jobs` (system DB) |
| Worker thread | `upload_job_service._worker_loop` (single-threaded queue) |
| Cancel flag | `_cancel_requested[job_id] = True` (in-memory dict) |
| Status update | `_run_upload_job` writes status, started_at, completed_at, duration_ms |

### Why cancel sometimes "doesn't work"

The cancel flag is checked at chunk boundaries. If the engine is mid-way
through a single multi-million-row INSERT or UPDATE, Python can't
interrupt it — pyodbc is blocked on the socket. To force-stop, find the
SPID and `KILL` it on the SQL side.

### Example: query active jobs from CLI

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "$API/api/v1/upload/jobs?limit=20" | jq '.data[] | {job_id, table_name, status, total_rows}'
```

---

## 6.6 Data Editor — `frontend/src/pages/DataEditorPage.jsx`

Spreadsheet-style editor over any table. View, edit cells, add rows,
delete rows.

### Files

| File | Role |
|---|---|
| `frontend/src/pages/DataEditorPage.jsx` | Grid UI |
| `app/api/v1/endpoints/data_ops.py` | CRUD routes |
| `app/services/upsert_engine.DirectUpdateEngine` | Per-row UPDATE / DELETE with audit |

### How saves work

Each cell edit → batched on the frontend → one request with N row
updates → backend calls `DirectUpdateEngine.update_record` per row →
each compares old vs new and only writes changed columns → audit
log gets a row per change.

### Caveats

- **No optimistic locking** — if two users edit the same row, last
  writer wins silently.
- **Per-row UPDATE is slow for 200+ edits** — consider switching to a
  bulk MERGE in `DirectUpdateEngine` if you regularly batch edit.

### Example: extend the editor with a new bulk action

1. Add a button in `DataEditorPage.jsx` that collects selected row PKs.
2. Add a `POST /editor/{table}/bulk-action` route in `data_ops.py`.
3. Implement the action server-side in a new service method.
4. Always emit an audit row via `AuditService` for traceability.

---

## Performance master rules for Data Management

1. **Never** open a `SELECT COUNT(*)` on a >1M-row table. Use `sys.partitions`.
2. **Always** wrap synchronous DB calls in `asyncio.to_thread` inside `async def` routes.
3. **Always** check the user's permission (`Depends(RequirePermissions([...]))`).
4. **Always** parameterise filter values — never string-concatenate user input into SQL.
5. **Audit log** every write — `audit_log` is the system of record.
