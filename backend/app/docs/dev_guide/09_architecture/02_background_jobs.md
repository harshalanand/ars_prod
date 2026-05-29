# Background Jobs Framework — Developer Reference

Anything that takes more than ~5 seconds should run in the background.
ARS has a single shared job framework used by Upload Data, Export Data,
Contribution % Execute, MSA Pipeline, and Listing Generate.

## The shared pattern

```
HTTP request  ─►  create job row in upload_jobs (status='queued')
                   ↓
              put (job_id, metadata) onto an in-memory queue.Queue
                   ↓
              return { job_id, status='queued' } to caller
                   ↓
   Background thread reads queue → marks running → does work
                                                ↓
                                  emits progress callbacks
                                                ↓
                                  on success / failure / cancel:
                                       update job row,
                                       write audit summary,
                                       clean up uploaded file
```

## Files

| File | Role |
|---|---|
| `app/services/upload_job_service.py` | The framework — queue, worker, status, cancel |
| `app/models/audit.py` | `UploadJob` ORM model |
| `app/api/v1/endpoints/upload.py` | Upload-specific entry points |
| `frontend/src/pages/JobsPage.jsx` | Universal jobs UI |

## The `UploadJob` row

```sql
upload_jobs:
  job_id            NVARCHAR(20)  PK
  table_name        NVARCHAR(200)
  file_name         NVARCHAR(500)
  file_path         NVARCHAR(1000)
  file_size         BIGINT
  status            NVARCHAR(20)   -- queued | running | completed | failed | cancelled
  mode              NVARCHAR(20)   -- upsert | delete
  primary_key_columns NVARCHAR(500)
  total_rows        BIGINT
  processed_rows    BIGINT
  inserted_rows     BIGINT
  updated_rows      BIGINT
  deleted_rows      BIGINT
  error_rows        BIGINT
  duration_ms       INT
  error_message     NVARCHAR(MAX)
  changed_columns_summary NVARCHAR(MAX)  -- JSON
  sample_changes    NVARCHAR(MAX)        -- JSON
  validation_errors NVARCHAR(MAX)        -- JSON
  created_by        NVARCHAR(100)
  ip_address        NVARCHAR(50)
  created_at        DATETIME
  started_at        DATETIME
  completed_at      DATETIME
```

## Concurrency model

**Single worker thread.** One job runs at a time; the rest wait their
turn in the queue. Pros: no concurrent writes to the same target table.
Cons: backlog when uploads pile up.

To change this: `_start_worker` in `upload_job_service.py` — currently
spawns one thread. Could become a small pool (e.g. 3 workers) with
**target-table locking** so two threads never touch the same table
at once.

## Cancellation — how it actually works

```python
# In upload_job_service.py
_cancel_requested: Dict[str, bool] = {}

# When user clicks Cancel:
_cancel_requested[job_id] = True

# The engine periodically checks:
def cancel_check():
    return bool(_cancel_requested.get(job_id))

# Inside the engine:
for chunk in chunks:
    if cancel_check():
        raise InterruptedError("Job cancelled by user")
    process(chunk)
```

### Limitation

The check fires **between** chunks. If the engine is mid-way through a
single multi-million-row INSERT or UPDATE, no Python flag will stop it
— pyodbc is blocked on the socket waiting for SQL Server.

### True hard-stop (recommended future change)

```python
# 1. When the worker checks out a connection, capture its SPID:
spid = conn.execute(text("SELECT @@SPID")).scalar()
job.spid = spid; db.commit()

# 2. On cancel-force, run KILL from a separate connection:
def cancel_job_force(job_id):
    job = db.query(UploadJob).filter_by(job_id=job_id).first()
    if job.spid:
        with system_engine.connect() as c:
            c.execute(text(f"KILL {int(job.spid)}"))
```

## Heartbeats — recommended addition

Currently a job that crashes mid-flight stays "running" forever until
manually cleared. Add a heartbeat:

```python
# In _run_upload_job, in the progress_callback:
job.heartbeat_at = datetime.utcnow()
db.commit()

# Reaper, runs every minute:
db.query(UploadJob).filter(
    UploadJob.status == 'running',
    UploadJob.heartbeat_at < datetime.utcnow() - timedelta(minutes=5)
).update({
    'status': 'failed',
    'error_message': 'Worker died — auto-failed by reaper',
    'completed_at': datetime.utcnow(),
})
db.commit()
```

Add `heartbeat_at DATETIME NULL` column to `upload_jobs`.

## Stale-job cleanup on startup

Add to `main.py` `startup_event`:

```python
@app.on_event("startup")
async def cleanup_stale_jobs():
    with SystemSessionLocal() as db:
        n = db.execute(text("""
            UPDATE upload_jobs
            SET status='failed',
                completed_at=GETDATE(),
                duration_ms=DATEDIFF(ms, started_at, GETDATE()),
                error_message='Backend restarted while running — auto-cleared'
            WHERE status IN ('running','queued')
        """)).rowcount
        db.commit()
        logger.info(f"Cleared {n} stale jobs on startup")
```

This makes a backend restart a clean slate for jobs.

---

## How to add a new background job type

### Step 1 — Define the work

```python
# app/services/my_new_job_service.py
def run_my_job(job_id: str, params: dict, progress_callback, cancel_check):
    """Heavy work goes here. Periodically:
       - call progress_callback(processed, total)
       - check cancel_check() and raise InterruptedError if True
    """
    ...
```

### Step 2 — Submit endpoint

```python
# app/api/v1/endpoints/my_new.py
@router.post("/my-new/run")
def run_my_new(payload: MyNewPayload, db = Depends(get_db),
               current_user = Depends(get_current_user)):
    job_id = f"MYN_{uuid.uuid4().hex[:10]}"
    job = UploadJob(
        job_id=job_id,
        table_name=payload.target_table,
        file_name="(scheduled job)",
        status='queued',
        mode='custom',
        created_by=current_user.username,
    )
    db.add(job); db.commit()

    # Reuse the upload worker queue
    from app.services.upload_job_service import _job_queue, _start_worker_if_needed
    _job_queue.put((job_id, payload.dict()))
    _start_worker_if_needed()

    return APIResponse(success=True, data={'job_id': job_id, 'status': 'queued'})
```

### Step 3 — Wire into the worker

`upload_job_service._run_upload_job` dispatches by `job.mode`. Add a
branch:

```python
if job.mode == 'custom':
    from app.services.my_new_job_service import run_my_job
    result = run_my_job(job.job_id, metadata, progress_callback, cancel_check)
```

### Step 4 — Done

- Job appears in Jobs Dashboard automatically.
- Progress / cancel / status update for free.
- Audit log entry on completion for free.

---

## Auto-cleanup middleware

`app/middleware/auto_free_space.py` (if your tree has it; otherwise see
`config.py:AUTO_FREE_*`):

After any successful POST/PUT to a path matching `AUTO_FREE_PATHS`, the
middleware schedules a cleanup pass:

1. `CHECKPOINT` the data DB.
2. Conditionally shrink the log if it's >`AUTO_FREE_LOG_MAX_MB`.
3. Drop any orphaned `##temp` global temp tables.
4. Light SHRINKFILE on tempdb data files.

Cooldown of `AUTO_FREE_COOLDOWN_SEC` (60s) prevents thrashing under
concurrent calls.

### To extend it to a new heavy endpoint

`config.py` → `AUTO_FREE_PATHS` — add a substring of your route. No
code change.

---

## Performance summary

- One worker = no concurrency conflicts. Don't change without adding
  per-target-table locking.
- Cancel = chunk-boundary only. Add SPID capture for true stop.
- Heartbeats prevent zombie jobs but require a small schema change.
- Startup cleanup is the **simplest** win — ten lines of code that
  permanently kills the "running forever" class of bugs.
- Auto-cleanup middleware is your safety net against tempdb / log
  bloat from large jobs.
