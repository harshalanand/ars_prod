# Upload Data — Deep Dive

The Upload Data screen is the most-used and most-critical page in ARS.
It moves CSV/XLSX files into ARS tables. Every other module's quality
depends on uploads being correct and fast.

This is the deepest note in the developer guide because **most ARS
performance issues trace back to this code path**.

---

## What it does, in one sentence

Reads a CSV/XLSX file, cleans the cells, validates against the target
schema, and runs an upsert (or delete) against the chosen table —
foreground for small files, background for big ones.

---

## File map

```
Browser
  └─ frontend/src/pages/UploadPage.jsx          (UI: stepper, preview, result)
              │
              ▼ multipart POST
Backend
  ├─ app/api/v1/endpoints/upload.py             (routes)
  │     ├─ POST /upload/        ─────► foreground
  │     └─ POST /upload/async   ─────► background (queues job)
  │
  ├─ app/services/file_upload_service.py        (parse + clean + validate)
  │     ├─ FileUploadService.process_upload     (foreground)
  │     └─ FileUploadService.process_delete     (delete mode)
  │
  ├─ app/services/upload_job_service.py         (background queue + worker)
  │     ├─ create_upload_job(...)
  │     ├─ _worker_loop()
  │     ├─ _run_upload_job(job_id)
  │     └─ cancel_job(job_id)
  │
  └─ app/services/upsert_engine.py              (the actual SQL)
        ├─ UpsertEngine.upsert(...)             ◄── public entry
        ├─ UpsertEngine._bulk_upsert(...)       ◄── fast path (>1000 rows)
        ├─ UpsertEngine._process_chunk(...)     ◄── chunked MERGE fallback
        └─ DirectUpdateEngine                    ◄── inline grid edits
```

---

## The TWO paths

| Path | When | Behaviour |
|---|---|---|
| **Foreground** `/upload/` | Files with < ~50k rows | User waits with browser open; sync |
| **Background** `/upload/async` | Anything bigger | Returns `job_id` instantly; worker thread does work |

### Why this matters

A foreground upload of 1M rows takes ~5 minutes. If the user closes the
browser, you lose the result page (the upload still completes — it
finishes server-side). Background gives them a job_id and a Jobs
Dashboard entry instead.

---

## End-to-end logic walk

### Step 1 — File arrives (any path)

```python
# upload.py
@router.post("/")
async def upload_file(file: UploadFile, table_name: str, primary_key_columns: str, mode: str, ...):
    content = await file.read()                # async read of body
    pk_cols = primary_key_columns.split(",")
    service = FileUploadService(db)
    result = await service.process_upload(...)
    return APIResponse(data=result)
```

The `await file.read()` is async-friendly — doesn't block the event
loop. The big work happens after this.

### Step 2 — File parsing (off the event loop)

```python
# file_upload_service.py
df = await asyncio.to_thread(self._read_file, content, ext, skip_rows, sheet_name)
```

`_read_file` is sync pandas + openpyxl. Wrapping it in `asyncio.to_thread`
runs it on a worker thread so other API requests keep flowing.

| Format | Reader | Speed |
|---|---|---|
| `.csv` | `pd.read_csv(..., dtype=str)` | Fast — ~10MB/sec |
| `.xlsx` | `pd.read_excel(..., engine='openpyxl', dtype=str)` | Slow — ~1MB/sec |
| `.xls` | `engine='xlrd'` | Even slower; legacy |

Why `dtype=str`: we don't want pandas guessing types. Numeric-looking
codes like article numbers should stay strings (otherwise leading zeros
disappear). The upsert engine casts to the target type later via
`TRY_CAST`.

Why `keep_default_na=False`: pandas would otherwise turn `'NA'` (the
string) into `NaN`. We want literal `'NA'` preserved.

### Step 3 — Cell cleaning

```python
# file_upload_service.py → _clean_dataframe
for col in df.columns:
    raw = df[col].astype(str)
    stripped = raw.str.strip()
    result[stripped.isin(["", "nan", "None", "NaT"])] = "__SKIP__"
    result[stripped.isin(["|", "-"])] = "__NULL__"
    df[col] = result
```

Every cell value gets one of three roles:

| Cell content | Marker | Effect during upsert |
|---|---|---|
| empty / `nan` / `None` / `NaT` | `__SKIP__` | Keep existing DB value (no change) |
| `'\|'` or `'-'` | `__NULL__` | Set the DB cell to NULL |
| `'NA'` (literal) | preserved | Stored as the literal string `'NA'` |
| anything else | passes through | Standard upsert |

### Step 4 — PK validation + drop blank PKs

Rows where any PK column is null/blank/SKIP/NULL are silently dropped
with a warning log. The reasoning: PK columns must be present for
MERGE to work, and a blank PK usually means a malformed row.

### Step 5 — Type pre-validation

```python
validation_errors = await asyncio.to_thread(
    self.upsert_engine.validate_data_types, table_name, df, 200,
)
```

`validate_data_types` runs vectorised checks per column:

- For `INT/BIGINT/FLOAT/DECIMAL` columns: `pd.to_numeric(series, errors='coerce')` then count NaNs.
- For `DATE/DATETIME` columns: `pd.to_datetime(series, errors='coerce')`.
- For string columns with bounded length: `series.str.len() > max_len`.

Returns up to 200 row-level errors with `{row, column, value, expected, target_type}` so the UI can show "Row 47, column QTY: '12abc' isn't a number."

These errors **don't block the upload** — `TRY_CAST` in the engine handles bad values gracefully (NULLs them out). But they let the user know what'll be lost.

### Step 6 — Hand off to the upsert engine

```python
result = await asyncio.to_thread(
    self.upsert_engine.upsert,
    table_name=table_name,
    df=df,
    primary_key_columns=primary_key_columns,
    changed_by=changed_by,
    source="UPLOAD",
    ip_address=ip_address,
    chunk_size=settings.UPLOAD_CHUNK_SIZE,
    enable_row_audit=True,
    collect_sample_changes=False,
)
```

This is the heavy call. Wrapped in `asyncio.to_thread` for the same
event-loop reason.

---

## The upsert engine — the most important code in ARS

### Two paths inside the engine

```python
# upsert_engine.py:upsert
if total_rows > 1000:
    # FAST BULK PATH
    ins, upd = self._bulk_upsert(...)
    if bulk_ok: return ...    # done in 30-60s for 165k rows
    # else fall through to chunked MERGE
# CHUNKED MERGE PATH (10k rows per chunk)
for chunk in chunks: self._process_chunk(...)
```

### Fast path step-by-step (`_bulk_upsert`)

```
1. Open raw connection (one connection for the whole operation)
2. CREATE TABLE #bulk_stage_<batch_id> ([col] NVARCHAR(4000) NULL, ...)
   - NVARCHAR(4000), not MAX. MAX = LOB = pyodbc memory blow-up.
3. fast_executemany = True
   cursor.setinputsizes([(SQL_WVARCHAR, 4000, 0)] * cols)
   - pinned input sizes prevent pyodbc from re-guessing
4. Stage in batches of 20,000:
     for i in range(0, total_rows, 20000):
         cursor.executemany(insert_sql, all_rows[i:i+20000])
         conn.commit()
   - Each batch = one TDS round-trip
   - 165k rows → 9 round-trips total
5. ONE big UPDATE — change-detected, ROWLOCK hint:
     UPDATE t WITH (ROWLOCK)
     SET t.col = CASE
                   WHEN s.col = '__SKIP__' THEN t.col
                   WHEN s.col = '__NULL__' THEN NULL
                   ELSE TRY_CAST(s.col AS <target_type>) END
     FROM target t INNER JOIN #stage s ON t.pk = s.pk
     WHERE (any column changed)
6. ONE big INSERT — ROWLOCK, NOT EXISTS check:
     INSERT INTO target WITH (ROWLOCK) (cols)
     SELECT cols FROM #stage s
     WHERE NOT EXISTS (SELECT 1 FROM target t WITH (NOLOCK) WHERE t.pk = s.pk)
7. DROP TABLE #stage
8. Audit row → audit_log (best-effort; failure must NOT trigger fallback)
```

#### Why ROWLOCK on UPDATE/INSERT?

Without it, SQL Server may escalate to a table lock for the duration of
the operation, blocking every reader. ROWLOCK keeps the lock granular,
RCSI keeps readers happy.

#### Why TRY_CAST and not CAST?

`CAST('abc' AS INT)` raises an error and aborts the whole INSERT.
`TRY_CAST('abc' AS INT)` returns NULL and continues. Bad values become
NULL, the upload finishes; the validation_errors panel tells the user
which rows had issues.

### What goes wrong on the fast path

| Symptom | Cause | Fix |
|---|---|---|
| "Total Records: 0, Errors = full file" | Positional args bug in `_build_result` | Already fixed — verify args order |
| Hour-long fallback to chunked MERGE | `AuditService.log_data_change` AttributeError | Already fixed — uses log_bulk_upsert |
| 9002 ("transaction log full") | Azure SQL log cap exceeded | Already-stuck transaction; KILL it |
| 40613 ("DB not currently available") | Azure SQL transient | Driver auto-retries with ConnectRetryCount=5 |

### Chunked path (fallback)

When the fast path raises (rare, but possible if the staging table
fails to create or schema-mismatch), the engine falls through to the
chunked MERGE path. It does the same logical work but per chunk:

```python
for chunk_idx, chunk_df in enumerate(chunks):
    cursor.execute("CREATE TABLE #upsert_temp_<id> (...)")
    cursor.executemany(insert_sql, chunk_rows)
    cursor.execute(merge_sql)             # MERGE with OUTPUT clause
    inserted, updated = parse_output_table(cursor)
    cursor.execute("DROP TABLE #upsert_temp_<id>")
    conn.commit()
```

Slower (~10× the fast path) but safer — each chunk is an independent
transaction so a failure mid-way doesn't roll back the entire upload.

---

## Background mode (`/upload/async`)

### What changes

```python
# upload.py
@router.post("/async")
async def upload_file_async(...):
    content = await file.read()
    result = create_upload_job(db=db, table_name=..., file_content=content, ...)
    return APIResponse(data=result)        # returns instantly with job_id
```

`create_upload_job` writes a row to `upload_jobs`, saves the file to
disk, queues `(job_id, metadata)` on an in-memory `queue.Queue`, and
ensures the single worker thread is running.

### The worker loop

```python
# upload_job_service.py
def _worker_loop():
    while True:
        job_id, metadata = _job_queue.get(timeout=60)   # blocks
        _run_upload_job(job_id, metadata)
        _job_queue.task_done()
```

`_run_upload_job` does the same thing `process_upload` does, but adds:
- progress callbacks that update `upload_jobs.processed_rows`
- cancellation checks (chunk-boundary)
- final status update (success / failed / cancelled)

### Why a single worker?

Conservative concurrency. Two workers writing to the same target table
can collide on the staging table name (`#bulk_stage_<batch_id>`) — same
session can't have two `#stage` tables of the same name. We use unique
batch IDs so the names don't collide, BUT two workers also fight for
DB log space and connection-pool slots. One worker = simple, predictable.

### To support multiple workers

`upload_job_service._start_worker_pool(n=3)` — spawn N threads. Add a
**per-target-table lock** so two workers never write to the same table
simultaneously:

```python
_table_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)
def _run_upload_job(job_id, metadata):
    job = ...
    with _table_locks[job.table_name]:
        ...
```

---

## Cancel flow

```
User clicks Cancel in Jobs Dashboard
    │
    ▼
POST /upload/jobs/<job_id>/cancel
    │
    ▼
upload_job_service.cancel_job(job_id):
    _cancel_requested[job_id] = True       # in-memory dict
    return immediately
    │
    ▼
Worker thread (still running) periodically checks:
    cancel_check = lambda: bool(_cancel_requested.get(job_id))
    if cancel_check(): raise InterruptedError
    │
    ▼
InterruptedError caught by _run_upload_job:
    job.status = 'cancelled'
    job.completed_at = now
    db.commit()
```

### Limitation

Cancel is a Python-level flag. If the worker is mid-way through a
`cursor.execute(big_merge_sql)`, it's blocked on the socket and won't
notice the flag. Pyodbc has no way to interrupt mid-statement.

To force-stop, we'd need to record the SQL session SPID and run
`KILL <spid>` from a separate connection. Not implemented today;
worth adding.

---

## Audit trail

Every upload writes:

| Row | When |
|---|---|
| `audit_log` summary | After every upload, regardless of size — `action_type=BULK_UPLOAD`, contains `inserted`, `updated`, `unchanged`, `errors`, `duration_ms`, `changed_columns_summary` |
| `audit_log` per-row | Only if `enable_row_audit=True` (default for bulk uploads). One row per insert/update with `old_data`, `new_data`, `changed_columns` |
| `data_change_log` (async) | Best-effort, queued for background processing |

### Disabling per-row audit (for huge uploads)

```python
result = engine.upsert(..., enable_row_audit=False)
```

Saves N audit rows for an N-row upload. Summary row is still written.
Use for >1M-row uploads where per-row audit isn't needed.

---

## Common change recipes

### Recipe 1 — add a new "blank-marker" character

Say you want `'X'` to mean "leave alone" in addition to blank.

`file_upload_service._clean_dataframe`:

```python
result[stripped.isin(["", "nan", "None", "NaT", "X"])] = "__SKIP__"
```

The engine already knows `__SKIP__`. No engine change needed.

### Recipe 2 — change the staging batch size

`upsert_engine._bulk_upsert` → `batch_size = 20000`. We've tested
20k as the sweet spot for Azure SQL across the public internet. Bigger
= fewer round-trips but ODBC driver allocates more buffer; >50k starts
thrashing on NVARCHAR(MAX) columns (we use 4000 instead, mitigating
this — but staying at 20k for safety).

### Recipe 3 — pre-validate against a custom rule

```python
# In file_upload_service.process_upload, before engine.upsert:
errors = []
mask = df['QTY'].astype(float) < 0
for idx in df[mask].index:
    errors.append({
      'row': int(idx) + 2,
      'column': 'QTY',
      'value': df.at[idx, 'QTY'],
      'expected': 'non-negative number',
      'target_type': 'business rule',
    })
result['validation_errors'] = errors + (result.get('validation_errors') or [])
```

Frontend already shows `validation_errors` on the result panel.

### Recipe 4 — different fast-path strategy for tiny tables (<100 rows)

For tiny uploads, the staging-table dance is overhead. Use direct
parameterized INSERTs:

```python
if total_rows < 100:
    return self._tiny_upsert(...)   # one MERGE statement, no staging
```

### Recipe 5 — auto-detect the file's column mapping

Currently the user must pre-map columns if their file's headers don't
match the target. Add auto-mapping:

```python
def _auto_map(df_cols, target_cols):
    # exact match first
    mapping = {c: c for c in df_cols if c in target_cols}
    # case-insensitive
    upper_target = {t.upper(): t for t in target_cols}
    for c in df_cols:
        if c not in mapping and c.upper() in upper_target:
            mapping[c] = upper_target[c.upper()]
    return mapping
```

---

## Performance reference

| Scenario | Before fixes | After fixes |
|---|---|---|
| 165k-row XLSX upload | 90+ minutes (stuck on chunked MERGE) | ~50 seconds |
| Foreground upload of 100k rows | Blocked event loop, UI froze | UI stays responsive |
| Audit log on 1M-row upload | 1M audit rows = slow | Single summary row (with `enable_row_audit=False`) |
| pyodbc memory on big batch | GBs (NVARCHAR(MAX)) | ~250-500 MB (NVARCHAR(4000)) |
| Connection drops mid-upload | Job stuck "running" forever | Auto-retry via ConnectRetryCount=5 |

---

## Things to never do

- ❌ Call `engine.upsert(...)` from an `async def` without `asyncio.to_thread`. Blocks the entire event loop.
- ❌ Use `NVARCHAR(MAX)` in the staging table. `fast_executemany` allocates per-cell LOB buffers.
- ❌ Loop and call `engine.upsert(...)` per chunk in user code. The engine already chunks; you'll just slow it down.
- ❌ Bypass the audit log for "performance reasons." `audit_log` is the system of record. Every write must trace to a user.
- ❌ Re-implement parsing in a route. Use `FileUploadService` so PK validation, cleaning, and validation_errors are consistent.
