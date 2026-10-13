# Incident — SQLAlchemy pool exhaustion blocked all logins

**Date**: 2026-10-10
**Host**: LENOVO-LA0X1245 (backend / FastAPI)
**SQL Server**: HOPC866
**Symptom reported by user**: "SQL hanged. spid 108 running." → then "UNABLE TO LOGIN"
**Resolution**: Backend process (uvicorn PID tree 15036→27056→15592→36316) restarted. Fresh pool reclaimed; logins work again.

---

## 1. What the user saw

- Logins to the web app stopped responding.
- Blamed SSMS SPID 108 on HOPC866, which was in fact an idle SSMS window with no open transaction and no activity (last request ended 63 ms after it started). SPID 108 was innocent.

## 2. What was actually wrong

The backend's SQLAlchemy `QueuePool` for the **Claude** database was 100 % saturated and could not hand out a connection to new requests.

Pool config (`app/core/config.py:57-59`):
- `DB_POOL_SIZE = 15`
- `DB_MAX_OVERFLOW = 25`
- `DB_POOL_TIMEOUT = 60`
- → Max 40 concurrent connections per engine; `get_db()` blocks up to 60 s then raises `TimeoutError: QueuePool limit of size 15 overflow 25 reached`.

Observed on HOPC866 at ~14:42 local:

| host | db | sessions | with open txn |
|---|---|---:|---:|
| **LENOVO-LA0X1245** | **Claude** | **40** | **40** |
| LENOVO-LA0X1245 | Rep_Data | 16 | 15 |
| POWERBI-PRD | Claude | 15 | 15 |
| POWERBI-PRD | Rep_Data | 15 | 15 |
| HOPC575 | Rep_Data | 23 | 12 |
| HOPC575 | Claude | 10 | 9 |
| HOPC341 | Rep_Data | 3 | 3 |

**Every one of the 40 Claude-DB connections from the backend host was in an open transaction.** The pool was fully booked, so each new login request waited at `SystemSessionLocal()` for 60 s and then timed out.

Representative last-run statements on leaked SPIDs:
- SPID 158, 159 (Claude): `SELECT TOP 1 ... FROM rbac_user_sessions WHERE jti=?` — the exact query that runs on every authenticated hit; sessions never committed/closed.
- SPID 51 (Rep_Data): `SELECT TOP (200) * FROM dbo.SZ1_MSA_SZ_BASE ORDER BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, SZ`
- SPID 56, 109, 115, 164, 180, 181, 182 (Claude): large parametrized `(@P1 int, ..., @P30+ int)` reads
- SPID 183 (Rep_Data): `SELECT ... FROM alloc_session table` — **leaked since 2026-10-08 16:26 (≈46 h old)**

The ≈46 h-old leftovers from HOPC575 are from a prior backend process that was killed without graceful shutdown; TCP didn't tear the sessions down, so SQL Server still sees them as live with their last implicit transaction open.

## 3. Why this is a leak and not normal pool behaviour

With `autocommit=False` (SQLAlchemy default) + pyodbc, **every `SELECT` opens an implicit transaction on the server**. SQLAlchemy returns a connection to the pool with `reset_on_return='rollback'`, which *should* clear it. The fact that all 40 pooled connections showed `open_transaction_count = 1` simultaneously means one of:

1. The connection was never returned to the pool (code path uses `get_raw_connection()` / ad-hoc `create_engine(...)` and forgets `conn.close()`).
2. The connection was handed out via `.raw_connection()` and the rollback-on-return path was bypassed.
3. 40 real requests were all in-flight at the same instant — but that would normally drain in seconds, not sit indefinitely.

Suspect code paths (grep candidates to audit — do not treat as a conclusion, verify each):
- `app/api/v1/endpoints/allocation_engine.py:49-54` — builds a new engine inline per call: `sys_engine = create_engine(system_db_url, pool_pre_ping=True)` / `data_engine = create_engine(data_db_url, pool_pre_ping=True)`. Each call creates a fresh pool that is never disposed; its connections leak out of the main pool-management story.
- `app/services/export_job_service.py:148` — `db = SessionLocal()` outside the FastAPI `get_db` dependency, so there's no framework-managed `finally: db.close()`.
- `app/services/msa_job_service.py:180-181` — same pattern: `system_db = SessionLocal(); data_db = DataSessionLocal()` in background job code.
- `app/database/session.py:118-125` — `get_raw_connection()` / `get_data_raw_connection()` return raw DBAPI connections; any caller that doesn't wrap them in `try/finally: conn.close()` leaks the slot and the transaction.
- `app/security/dependencies.py:86` — `db = SystemSessionLocal()` opened manually in auth middleware; needs an audit for `.close()` on every exit path (including early returns and raised exceptions).

## 4. Immediate action taken (2026-10-10)

**Chose option B — full backend restart** (clean, forces a fresh pool, drops any in-flight requests but those were already timing out).

- Killed uvicorn process tree rooted at PID 15036 (child chain 27056 → 15592 → fork-worker 36316).
- Relaunched: `uvicorn main:app --reload --port 8080` from `E:\ars_prod\backend` using the venv Python.
- Verified new pool is fresh and health endpoint responds.

The ≈46 h-old orphan sessions from HOPC575 and the stale POWERBI-PRD sessions were **not** killed as part of this restart (they belong to other hosts). They continue to hold their slots on SQL Server. Separate cleanup needed — see §5.

## 5. For the next version — must-fix items

### 5.1 Stop creating ad-hoc engines in request handlers
Every `create_engine(...)` inside an endpoint builds a new, un-tracked pool. Replace all such call sites with the shared `system_engine` / `data_engine` from `app.database.session`.

- [ ] `app/api/v1/endpoints/allocation_engine.py:49-54` — remove the inline `create_engine` and inject `system_engine, data_engine` instead.
- [ ] Grep the whole backend for `create_engine(` and audit every match. Only `app/database/session.py` should call it.

### 5.2 Guard every manually-opened Session
Anywhere code does `SessionLocal()` / `SystemSessionLocal()` / `DataSessionLocal()` outside a FastAPI `Depends(get_db)`, it must wrap the session in `try/except/finally` that calls `db.rollback()` on exception and `db.close()` always. Preferred form:

```python
with SystemSessionLocal() as db:
    ...
# auto-close on exit
```

- [ ] `app/services/export_job_service.py:148`
- [ ] `app/services/msa_job_service.py:180-181`
- [ ] `app/security/dependencies.py:86`
- [ ] Any other `SessionLocal(` / `*SessionLocal(` call site.

### 5.3 Make raw-connection usage safe-by-default
`get_raw_connection()` and `get_data_raw_connection()` are foot-guns: callers forget `conn.close()`. Replace the free functions with context managers:

```python
@contextmanager
def raw_system_connection():
    conn = system_engine.raw_connection()
    try:
        yield conn
    finally:
        conn.close()
```

Then grep-replace callers to `with raw_system_connection() as conn:`.

### 5.4 Add a leak canary
In `main.py` (or a scheduled task), emit pool telemetry every 60 s:

```python
from app.database.session import system_engine, data_engine
for name, eng in [("system", system_engine), ("data", data_engine)]:
    p = eng.pool
    logger.info(f"pool.{name} size={p.size()} checkedout={p.checkedout()} overflow={p.overflow()}")
```

Alert when `checkedout / (size + max_overflow) > 0.8` for more than 2 minutes — that's the window where a leak becomes a login outage.

### 5.5 Shutdown handling
Register a FastAPI `shutdown` event that calls `system_engine.dispose()` and `data_engine.dispose()`. The ≈46 h orphan from HOPC575 happened because the previous process was killed without graceful shutdown.

```python
@app.on_event("shutdown")
async def _dispose_pools():
    system_engine.dispose()
    data_engine.dispose()
```

### 5.6 Shorten `pool_recycle`
The current `DB_POOL_RECYCLE` was not inspected during this incident, but a short recycle (e.g. 1800 s) ensures connections are periodically closed and reopened, which bounds how long any single leaked connection can hog a server slot.

### 5.7 Server-side safety net
Add a nightly SQL Server job that KILLs user sessions with `open_transaction_count > 0 AND last_request_end_time < DATEADD(hour, -1, GETDATE())`. Belt-and-braces: even if the client leaks, the server stops hoarding slots overnight.

## 6. Diagnostic queries (for future incidents)

```sql
-- Zombie sessions by host
SELECT host_name, program_name, DB_NAME(database_id) AS db, COUNT(*) AS n,
       SUM(CASE WHEN open_transaction_count>0 THEN 1 ELSE 0 END) AS with_open_txn,
       MIN(last_request_start_time) AS oldest
FROM sys.dm_exec_sessions
WHERE is_user_process = 1
GROUP BY host_name, program_name, DB_NAME(database_id)
ORDER BY n DESC;

-- What the leaked session last ran
SELECT s.session_id, ib.event_info
FROM sys.dm_exec_sessions s
CROSS APPLY sys.dm_exec_input_buffer(s.session_id, NULL) ib
WHERE s.session_id = <SPID>;

-- Emergency kill of long-idle leaks (>10 min idle, user sessions, open txn)
DECLARE @sql nvarchar(max) = N'';
SELECT @sql += N'KILL ' + CAST(session_id AS varchar(5)) + N';' + CHAR(10)
FROM sys.dm_exec_sessions
WHERE is_user_process = 1
  AND open_transaction_count > 0
  AND DATEDIFF(minute, last_request_end_time, GETDATE()) > 10;
EXEC sp_executesql @sql;
```

## 7. Post-restart calibration — important for next-version diagnosis

Immediately after the clean restart, the backend showed **9 Claude + 9 Rep_Data sessions, every one with `open_transaction_count = 1`**, while the pool was clearly healthy (login works, `/health = 200`, 9/40 slots in use).

That means `open_transaction_count = 1` on an idle pooled connection is **expected** with `autocommit=False` + pyodbc: every `SELECT` opens an implicit transaction and the row stays visible on the server even after SQLAlchemy's `reset_on_return` runs — because the next statement will just open a fresh implicit txn anyway.

Diagnostic rule for next time:
- **`open_transaction_count` alone is NOT a leak signal** — it will match the pool size on a healthy, idle backend.
- The real leak signal is **`sessions_from_backend_host == pool_size + max_overflow` AND requests are timing out**. I.e., the host hit the ceiling (40) *and* the app is unhappy.
- A better leak probe is SQLAlchemy pool telemetry (`pool.checkedout()`), not server-side DMVs. §5.4 above.

So the earlier claim "100 % had open transactions, therefore leaked" was partly a red herring; the dispositive fact was "100 % of pool slots in use *while* login requests were timing out at 60 s". Keep that distinction in mind during the next incident.

## 8. One-line lesson

> **The "SQL hang" was never on SQL Server. SQL Server was idle. The hang was in the backend's SQLAlchemy pool: all 40 slots were in use (whether truly leaked or just slow-to-return), so `get_db()` blocked for `pool_timeout=60 s` and login requests failed. Fix is to add pool telemetry, convert ad-hoc engines/raw-connections to shared/context-managed form, and register a shutdown handler.**
