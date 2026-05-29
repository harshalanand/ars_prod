# Jobs Dashboard

Real-time view of every background job in ARS — uploads, exports, MSA
calculations, contribution executions, grid runs, listing generates.

---

## What it does

Reads `upload_jobs` (the universal job table) plus any module-specific
job tables (`ARS_CONTRIB_JOBS`, etc.), shows them in a unified grid,
lets you filter by status / table / user, and offers per-job actions:
view logs, view sample changes, cancel, delete.

## Files

| Layer | File |
|---|---|
| Page | `frontend/src/pages/JobsPage.jsx` |
| Routes | `app/api/v1/endpoints/upload.py` (jobs/*), `app/api/v1/endpoints/contrib.py` (job-specific actions) |
| Services | `app/services/upload_job_service.py` (universal queue), per-domain services |
| Permission | `JOBS_VIEW` |

## Job lifecycle (states)

```
queued → running → (completed | failed | cancelled)
```

| State | What it means | When it changes |
|---|---|---|
| **queued** | In the queue, no worker has picked it up | Set on POST to `/upload/async` etc. |
| **running** | Worker is actively processing | Set when worker starts; updates `started_at` |
| **completed** | Finished successfully | Final status; sets `completed_at`, `duration_ms`, row counts |
| **failed** | Worker raised an exception | Final; `error_message` populated |
| **cancelled** | User clicked Cancel before completion | Final; `error_message='Cancelled by user'` |

## Schema (universal `upload_jobs` row)

| Column | Type | Purpose |
|---|---|---|
| `job_id` | NVARCHAR(20) PK | Stable identifier (`UPL_<random>`, `CTR_<random>`, etc.) |
| `table_name` | NVARCHAR(200) | Target table |
| `file_name` | NVARCHAR(500) | Original file name (or null for non-file jobs) |
| `mode` | NVARCHAR(20) | `upsert`, `delete`, `custom` |
| `status` | NVARCHAR(20) | One of the lifecycle states above |
| `total_rows`, `processed_rows`, `inserted_rows`, `updated_rows`, `deleted_rows`, `error_rows` | BIGINT | Progress counters |
| `started_at`, `completed_at` | DATETIME | Wall-clock timestamps |
| `duration_ms` | INT | End-to-end ms |
| `error_message` | NVARCHAR(MAX) | Stack trace / user-friendly error |
| `changed_columns_summary` | NVARCHAR(MAX) | JSON `{col: count_of_changes}` |
| `sample_changes` | NVARCHAR(MAX) | JSON, first 100 row changes |
| `validation_errors` | NVARCHAR(MAX) | JSON, type-validation errors from `validate_data_types` |
| `created_by`, `ip_address`, `created_at` | | Audit |

## How to add a new job type so it appears here

The Jobs Dashboard reads from `upload_jobs`. Any service that wants to
surface a job here needs to write a row to `upload_jobs` with
`mode='custom'` (or any new value).

```python
# In your service
from app.models.audit import UploadJob
job = UploadJob(
    job_id=f"MYJOB_{uuid.uuid4().hex[:10]}",
    table_name=spec.target_table,
    file_name="(scheduled)",
    file_path=None,
    file_size=None,
    status='queued',
    mode='custom',
    primary_key_columns='',
    created_by=current_user.username,
    ip_address=request.client.host,
)
db.add(job); db.commit()
```

## Cancel mechanism

```python
# upload_job_service.py
_cancel_requested: Dict[str, bool] = {}

def cancel_job(job_id: str, force: bool = False):
    _cancel_requested[job_id] = True
    if force:
        # mark cancelled immediately, even though the worker may still be running
        UPDATE upload_jobs SET status='cancelled' WHERE job_id=:id
```

The worker checks `cancel_check()` periodically (chunk boundary).
Limitation: a single long SQL statement can't be interrupted this way.

### Implementing **hard** cancel via `KILL`

```python
# When worker checks out a connection:
spid = conn.execute(text("SELECT @@SPID")).scalar()
job.spid = spid; db.commit()

# When user clicks Force Stop:
def cancel_job_force(job_id):
    job = db.query(UploadJob).filter_by(job_id=job_id).first()
    if not job.spid: return
    with system_engine.connect() as c:
        c.execute(text(f"KILL {int(job.spid)}"))
        c.commit()
```

Add `spid INT NULL` column to `upload_jobs` and a force-stop button in
the dashboard.

---

## Common changes

### Recipe: surface cron-scheduled jobs

If you add a cron-scheduled task (e.g. nightly summary refresh), write
a `upload_jobs` row at the start and update on completion. The
dashboard shows it without any UI change.

### Recipe: filter by job_id prefix

The dashboard could group by prefix: `UPL_*` = uploads, `CTR_*` =
contrib, `MYJOB_*` = your new type. In `JobsPage.jsx`, derive the
"type" client-side:

```js
const type = job.job_id.split('_')[0]
```

Then add a filter chip. No backend change needed.

### Recipe: live-tail the log of a running job

Add a `job_logs` table:

```sql
CREATE TABLE job_logs (
    id BIGINT IDENTITY PRIMARY KEY,
    job_id NVARCHAR(20),
    ts DATETIME2 DEFAULT SYSUTCDATETIME(),
    level NVARCHAR(20),
    message NVARCHAR(MAX)
);
CREATE INDEX IX_job_logs_job_id_ts ON job_logs(job_id, ts);
```

Worker calls `_log(job_id, "INFO", "stage 1 complete")`. Frontend
polls `GET /jobs/<id>/logs?since=<last_ts>` every 2s. Better: SSE.

---

## Performance

- **Polling load:** if 20 users open the dashboard with 5s polling, that's 4 calls/sec to `/jobs`. Switch to server-sent events to reduce DB load.
- **Pagination:** for >500 jobs, paginate. The `upload_jobs` table grows fast; archive completed/failed older than 90 days to `upload_jobs_history`.
- **Indexing:** `upload_jobs(status, created_at DESC)` is the index that powers the default "active jobs first" view.

---

## Diagnostic tips

When jobs appear stuck:

```sql
-- Are they actually running?
SELECT j.job_id, j.status, j.started_at,
       DATEDIFF(SECOND, j.started_at, GETDATE()) AS age_sec
FROM upload_jobs j
WHERE j.status = 'running';

-- Cross-reference with active SQL sessions
SELECT TOP 5 r.session_id, s.host_name, s.program_name, r.command,
       r.wait_type, r.wait_time, SUBSTRING(t.text, 1, 200) AS sql_text
FROM sys.dm_exec_requests r
JOIN sys.dm_exec_sessions s ON r.session_id = s.session_id
OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t
WHERE s.is_user_process = 1
  AND s.program_name LIKE '%uvicorn%'
ORDER BY r.total_elapsed_time DESC;
```

If the app DB says "running" but no SQL session exists for that job →
the worker died. Mark stale jobs failed:

```sql
UPDATE upload_jobs
SET status='failed', completed_at=GETDATE(),
    error_message='Worker died — auto-cleared'
WHERE status='running'
  AND started_at < DATEADD(minute, -60, GETDATE());
```
