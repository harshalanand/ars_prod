"""
Get Data — sync jobs, the run engine, and run history.

A sync job copies one Snowflake view or table into a local GD_SF_* table in
Rep_data, manually or on a schedule (times in IST). Every run is recorded in
GD_RUN as AUTO or MANUAL with the Snowflake row count and the rows loaded.

One run:
  1. read the source's columns and map Snowflake types to SQL Server types
     (wide VARCHARs and NUMBER(38,0) are measured first so widths are real)
  2. run the SELECT; Snowflake's row count for THAT result is SOURCE_ROWS
  3. stream it in batches into GD_SF_<X>__STG
  4. verify: rows in the stage table must equal SOURCE_ROWS, else the run
     fails and the live table is left untouched
  5. apply — replace: swap the stage table in (one transaction, readers never
     see a half-loaded table) · incremental: MERGE on the key columns ·
     append: INSERT the batch
  6. record counts, the Snowflake query id, and the new watermark

A job runs at most once at a time (GD_JOB.LOCK_RUN_ID + heartbeat), across all
uvicorn workers. Spec: frontend/public/docs/manual/get_data.md.
"""
import json
import math
import re
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine
from app.services import get_data_bulk as gdb
from app.services import get_data_snowflake as gsf
from app.services.get_data_schema import ensure_tables, JOB_TABLE, RUN_TABLE
from app.services.get_data_snowflake import GetDataError
from app.services.report_scheduler_service import compute_next_run

IST = timedelta(hours=5, minutes=30)
TARGET_PREFIX = "GD_SF_"
VALID_MODES = ("replace", "incremental", "append")
VALID_LOADERS = ("bulk", "classic")
VALID_TRIGGERS = ("manual", "schedule")
VALID_FREQ = ("daily", "weekly", "monthly", "every_n_hours")
FETCH_BATCH = 20000
LOCK_STALE_MIN = 10
HEARTBEAT_SEC = 30
MODE_LABEL = {"replace": "full replace", "incremental": "incremental", "append": "append"}


class AlreadyRunning(GetDataError):
    def __init__(self, message: str, run_id: Optional[int] = None):
        super().__init__(message)
        self.run_id = run_id


# ── Small helpers ────────────────────────────────────────────────────────────
def _now() -> datetime:
    """SQL Server's clock, in UTC. Every due / stale check runs in SQL against
    SYSUTCDATETIME(), so times computed here must come from the same clock. The
    app server (HOPC575) and SQL Server (HOPC866) were 295 s apart on
    2026-09-30, which fired schedules early and re-fired them every tick."""
    with get_data_engine().connect() as c:
        return c.execute(text("SELECT SYSUTCDATETIME()")).scalar().replace(microsecond=0)


def _iso(v: Any) -> Any:
    """UTC-naive datetimes go out as ISO with 'Z' so the browser converts to IST."""
    if isinstance(v, datetime):
        return v.replace(microsecond=0).isoformat() + "Z"
    return v


def _row(r) -> Dict[str, Any]:
    d = {k: _iso(v) for k, v in dict(r).items()}
    for jk in ("KEY_COLS", "SCHEDULE_CONFIG"):
        if isinstance(d.get(jk), str) and d[jk]:
            try:
                d[jk] = json.loads(d[jk])
            except Exception:
                pass
    return d


def _fmt_dur(ms: int) -> str:
    s = int(ms // 1000)
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def _ist_label(dt_utc: Optional[datetime]) -> str:
    return (dt_utc + IST).strftime("%d %b %H:%M IST") if dt_utc else "—"


def sanitize_target(name: Any) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", str(name or "").strip()).upper().strip("_")
    if not s:
        raise GetDataError("Local table name is required.")
    if not s.startswith(TARGET_PREFIX):
        s = TARGET_PREFIX + s
    if s.endswith("__STG") or s.endswith("__OLD"):
        raise GetDataError("Local table names may not end in __STG or __OLD (used during loads).")
    if len(s) > 100:
        raise GetDataError("Local table name is too long (100 characters at most).")
    return s


def next_run_utc(trigger: str, cfg: Optional[Dict[str, Any]],
                 now_utc: Optional[datetime] = None) -> Optional[datetime]:
    """Schedule times are IST wall-clock; NEXT_RUN_AT is stored in UTC.
    IST has no daylight saving, so a fixed +05:30 is exact."""
    if trigger != "schedule" or not isinstance(cfg, dict):
        return None
    now_ist = (now_utc or _now()) + IST
    nxt = compute_next_run(cfg, now=now_ist)
    return (nxt - IST).replace(microsecond=0) if nxt else None


def _json_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [x for x in re.split(r"[,\s]+", v) if x]
    return [str(x).strip().strip('"').upper() for x in v if str(x).strip()]


def _validate_schedule(cfg: Any) -> Dict[str, Any]:
    if not isinstance(cfg, dict):
        raise GetDataError("A scheduled job needs a schedule.")
    freq = str(cfg.get("freq") or "daily").lower()
    if freq not in VALID_FREQ:
        raise GetDataError(f"Unknown frequency '{freq}'.")
    out: Dict[str, Any] = {"freq": freq}
    if freq == "every_n_hours":
        n = int(cfg.get("every_n_hours") or 0)
        if not 1 <= n <= 24:
            raise GetDataError("Every N hours must be between 1 and 24.")
        out["every_n_hours"] = n
        if cfg.get("start") and cfg.get("end"):
            out["start"], out["end"] = str(cfg["start"]), str(cfg["end"])
        return out
    times = cfg.get("times") or []
    if isinstance(times, str):
        times = [t for t in re.split(r"[,\s]+", times) if t]
    clean = []
    for t in times:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(t).strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise GetDataError(f"'{t}' is not a valid time — use HH:MM (24-hour, IST).")
        clean.append(f"{int(m.group(1)):02d}:{m.group(2)}")
    if not clean:
        raise GetDataError("Add at least one run time (HH:MM, IST).")
    out["times"] = sorted(set(clean))
    if freq == "weekly":
        wds = sorted({int(w) % 7 for w in (cfg.get("weekdays") or [])})
        if not wds:
            raise GetDataError("Pick at least one weekday.")
        out["weekdays"] = wds
    if freq == "monthly":
        days = sorted({int(d) for d in (cfg.get("days") or []) if 1 <= int(d) <= 31})
        if not days:
            raise GetDataError("Pick at least one day of the month (1–31).")
        out["days"] = days
    return out


# ── Job CRUD ─────────────────────────────────────────────────────────────────
_JOB_COLS = """j.JOB_ID, j.JOB_NAME, j.DESCRIPTION, j.SOURCE_TYPE, j.SOURCE_OBJECT, j.TARGET_TABLE,
    j.LOAD_MODE, j.LOADER_PREF, j.ALLOW_EMPTY, j.KEY_COLS, j.WATERMARK_COL, j.WATERMARK_VALUE, j.TRIGGER_TYPE,
    j.SCHEDULE_CONFIG, j.RETRY_ON_FAIL, j.RETRY_DELAY_MIN, j.ENABLED, j.NEXT_RUN_AT,
    j.RETRY_AT, j.LOCK_RUN_ID, j.LOCK_HEARTBEAT, j.LAST_RUN_ID, j.LAST_RUN_AT,
    j.LAST_STATUS, j.LAST_ROWS, j.LAST_MESSAGE, j.CREATED_BY, j.CREATED_AT,
    j.UPDATED_BY, j.UPDATED_AT,
    r.STEP AS RUN_STEP, r.SOURCE_ROWS AS RUN_SOURCE_ROWS, r.ROWS_LOADED AS RUN_ROWS_LOADED,
    r.STARTED_AT AS RUN_STARTED_AT, r.RUN_TYPE AS RUN_RUN_TYPE"""


def list_jobs() -> List[Dict[str, Any]]:
    ensure_tables()
    with get_data_engine().connect() as c:
        rows = c.execute(text(f"""
            SELECT {_JOB_COLS} FROM {JOB_TABLE} j
            LEFT JOIN {RUN_TABLE} r ON r.RUN_ID = j.LOCK_RUN_ID AND r.STATUS = 'running'
            ORDER BY j.JOB_NAME
        """)).mappings().fetchall()
    return [_row(r) for r in rows]


def get_job(job_id: int, raw: bool = False) -> Optional[Dict[str, Any]]:
    ensure_tables()
    with get_data_engine().connect() as c:
        r = c.execute(text(f"""
            SELECT {_JOB_COLS} FROM {JOB_TABLE} j
            LEFT JOIN {RUN_TABLE} r ON r.RUN_ID = j.LOCK_RUN_ID AND r.STATUS = 'running'
            WHERE j.JOB_ID = :id
        """), {"id": job_id}).mappings().fetchone()
    if not r:
        return None
    if raw:
        d = dict(r)
        d["KEY_COLS"] = json.loads(d["KEY_COLS"]) if d.get("KEY_COLS") else []
        d["SCHEDULE_CONFIG"] = json.loads(d["SCHEDULE_CONFIG"]) if d.get("SCHEDULE_CONFIG") else None
        return d
    return _row(r)


def _clean_payload(p: Dict[str, Any], job_id: Optional[int]) -> Dict[str, Any]:
    name = str(p.get("job_name") or "").strip()
    if not name:
        raise GetDataError("Job name is required.")
    if len(name) > 200:
        raise GetDataError("Job name is too long (200 characters at most).")
    source = gsf.normalize_object(p.get("source_object"))
    target = sanitize_target(p.get("target_table"))
    mode = str(p.get("load_mode") or "replace").lower()
    if mode not in VALID_MODES:
        raise GetDataError(f"Load mode must be one of {', '.join(VALID_MODES)}.")
    keys = _json_list(p.get("key_cols"))
    wm = str(p.get("watermark_col") or "").strip().strip('"').upper() or None
    if mode == "incremental" and (not keys or not wm):
        raise GetDataError("Incremental loads need key column(s) and a watermark column.")
    if mode != "incremental":
        wm = None
    trig = str(p.get("trigger_type") or "manual").lower()
    if trig not in VALID_TRIGGERS:
        raise GetDataError("Trigger must be manual or schedule.")
    sched = _validate_schedule(p.get("schedule_config")) if trig == "schedule" else None
    delay = int(p.get("retry_delay_min") or 15)
    if not 5 <= delay <= 240:
        raise GetDataError("Retry delay must be between 5 and 240 minutes.")
    loader = str(p.get("loader") or "bulk").lower()
    if loader not in VALID_LOADERS:
        raise GetDataError("Loader must be bulk or classic.")

    with get_data_engine().connect() as c:
        clash = c.execute(text(f"""
            SELECT JOB_NAME FROM {JOB_TABLE}
            WHERE TARGET_TABLE = :t AND (:id IS NULL OR JOB_ID <> :id)
        """), {"t": target, "id": job_id}).scalar()
    if clash:
        raise GetDataError(f"{target} is already loaded by job '{clash}'. Each local table has one job.")

    enabled = bool(p.get("enabled", True))
    return {
        "name": name, "desc": (str(p.get("description") or "").strip() or None),
        "src": source, "tgt": target, "mode": mode,
        "keys": json.dumps(keys) if keys else None, "wm": wm,
        "trig": trig, "sched": json.dumps(sched) if sched else None,
        "retry": 1 if p.get("retry_on_fail", True) else 0, "delay": delay,
        "en": 1 if enabled else 0,
        "next": next_run_utc(trig, sched) if enabled else None,
        "loader": loader, "allow_empty": 1 if p.get("allow_empty") else 0,
    }


def create_job(p: Dict[str, Any], user: str) -> Dict[str, Any]:
    ensure_tables()
    v = _clean_payload(p, None)
    v["user"] = user
    with get_data_engine().begin() as c:
        new_id = c.execute(text(f"""
            INSERT INTO {JOB_TABLE} (JOB_NAME, DESCRIPTION, SOURCE_TYPE, SOURCE_OBJECT,
                TARGET_TABLE, LOAD_MODE, LOADER_PREF, ALLOW_EMPTY, KEY_COLS, WATERMARK_COL,
                TRIGGER_TYPE, SCHEDULE_CONFIG, RETRY_ON_FAIL, RETRY_DELAY_MIN, ENABLED,
                NEXT_RUN_AT, CREATED_BY, UPDATED_BY)
            OUTPUT INSERTED.JOB_ID
            VALUES (:name, :desc, 'SNOWFLAKE', :src, :tgt, :mode, :loader, :allow_empty, :keys,
                :wm, :trig, :sched, :retry, :delay, :en, :next, :user, :user)
        """), v).scalar()
    logger.info(f"[get-data] job {new_id} '{v['name']}' created by {user}")
    return get_job(int(new_id))


def update_job(job_id: int, p: Dict[str, Any], user: str) -> Dict[str, Any]:
    cur = get_job(job_id, raw=True)
    if not cur:
        raise KeyError(job_id)
    v = _clean_payload(p, job_id)
    # A different source, table, mode or watermark column invalidates the
    # stored watermark — the next incremental run starts with a full load.
    reset_wm = (v["src"] != cur["SOURCE_OBJECT"] or v["tgt"] != cur["TARGET_TABLE"]
                or v["mode"] != cur["LOAD_MODE"] or v["wm"] != cur["WATERMARK_COL"])
    v.update({"id": job_id, "user": user, "reset": 1 if reset_wm else 0})
    with get_data_engine().begin() as c:
        c.execute(text(f"""
            UPDATE {JOB_TABLE} SET JOB_NAME=:name, DESCRIPTION=:desc, SOURCE_OBJECT=:src,
                TARGET_TABLE=:tgt, LOAD_MODE=:mode, LOADER_PREF=:loader, ALLOW_EMPTY=:allow_empty,
                KEY_COLS=:keys, WATERMARK_COL=:wm,
                WATERMARK_VALUE = CASE WHEN :reset = 1 THEN NULL ELSE WATERMARK_VALUE END,
                TRIGGER_TYPE=:trig, SCHEDULE_CONFIG=:sched, RETRY_ON_FAIL=:retry,
                RETRY_DELAY_MIN=:delay, ENABLED=:en, NEXT_RUN_AT=:next,
                RETRY_AT = CASE WHEN :en = 1 THEN RETRY_AT ELSE NULL END,
                UPDATED_BY=:user, UPDATED_AT=SYSUTCDATETIME()
            WHERE JOB_ID=:id
        """), v)
    return get_job(job_id)


def set_enabled(job_id: int, enabled: bool, user: str) -> Dict[str, Any]:
    job = get_job(job_id, raw=True)
    if not job:
        raise KeyError(job_id)
    nxt = next_run_utc(job["TRIGGER_TYPE"], job["SCHEDULE_CONFIG"]) if enabled else None
    with get_data_engine().begin() as c:
        c.execute(text(f"""
            UPDATE {JOB_TABLE} SET ENABLED=:en, NEXT_RUN_AT=:nr,
                RETRY_AT = CASE WHEN :en = 1 THEN RETRY_AT ELSE NULL END,
                UPDATED_BY=:u, UPDATED_AT=SYSUTCDATETIME()
            WHERE JOB_ID=:id
        """), {"en": 1 if enabled else 0, "nr": nxt, "u": user, "id": job_id})
    return get_job(job_id)


def delete_job(job_id: int, drop_table: bool, user: str) -> Dict[str, Any]:
    job = get_job(job_id, raw=True)
    if not job:
        raise KeyError(job_id)
    if job.get("LOCK_RUN_ID") and not _lock_is_stale(job.get("LOCK_HEARTBEAT")):
        raise GetDataError("The job is running — wait for the run to finish, then delete it.")
    with get_data_engine().begin() as c:
        c.execute(text(f"DELETE FROM {JOB_TABLE} WHERE JOB_ID=:id"), {"id": job_id})
        if drop_table:
            t = job["TARGET_TABLE"]
            c.execute(text(f"IF OBJECT_ID('dbo.[{t}]','U') IS NOT NULL DROP TABLE dbo.[{t}]"))
    logger.info(f"[get-data] job {job_id} '{job['JOB_NAME']}' deleted by {user}"
                f"{' (table dropped)' if drop_table else ''}")
    return {"deleted": True, "table_dropped": bool(drop_table), "target_table": job["TARGET_TABLE"]}


# ── Test (count only, nothing loaded) ────────────────────────────────────────
def _resolve_col(names: List[str], wanted: str) -> Optional[str]:
    for n in names:
        if n.upper() == wanted.upper():
            return n
    return None


def test_job(p: Dict[str, Any]) -> Dict[str, Any]:
    fq = gsf.normalize_object(p.get("source_object"))
    mode = str(p.get("load_mode") or "replace").lower()
    keys = _json_list(p.get("key_cols"))
    wm = str(p.get("watermark_col") or "").strip().strip('"').upper() or None
    checks: List[Dict[str, Any]] = []
    conn = gsf.connect()
    try:
        cur = conn.cursor()
        t0 = time.time()
        try:
            metas = gsf.describe(cur, f"SELECT * FROM {gsf.quote_object(fq)}")
        except Exception as e:
            checks.append({"label": "Source readable", "ok": False, "detail": gsf.sf_message(e)})
            return {"ok": False, "checks": checks, "columns": [], "row_count": None}
        checks.append({"label": "Source readable", "ok": True, "detail": f"{len(metas)} columns"})
        names = [m["name"] for m in metas]
        dups = gsf._dup_names(metas)
        reserved = [n for n in names if n.upper() in gsf.AUDIT_COLS]
        if dups or reserved:
            checks.append({"label": "Column names", "ok": False,
                           "detail": "Duplicate (case-insensitive): " + ", ".join(dups) if dups
                           else f"{', '.join(reserved)} is reserved for load audit columns"})
        if mode == "incremental" or keys:
            missing = [k for k in keys if not _resolve_col(names, k)]
            checks.append({"label": "Key columns", "ok": bool(keys) and not missing,
                           "detail": ("not in source: " + ", ".join(missing)) if missing
                           else (", ".join(keys) if keys else "none given")})
        if mode == "incremental":
            wm_real = _resolve_col(names, wm) if wm else None
            wm_meta = next((m for m in metas if m["name"] == wm_real), None)
            ok = bool(wm_meta) and wm_meta["sf_type"] in ("FIXED", "DATE", "TIMESTAMP",
                                                         "TIMESTAMP_NTZ", "TIMESTAMP_LTZ",
                                                         "TIMESTAMP_TZ", "TEXT")
            checks.append({"label": "Watermark column", "ok": ok,
                           "detail": (f"{wm_real} · {gsf.sf_type_label(wm_meta)}" if wm_meta
                                      else f"{wm or '—'} not in source")})
        cur.execute(f"SELECT COUNT(*) FROM {gsf.quote_object(fq)}")
        rows = int(cur.fetchone()[0])
        checks.append({"label": "Row count", "ok": True,
                       "detail": f"{rows:,} rows · {time.time() - t0:.1f}s"})
        for m in metas:
            m["sf_type_label"] = gsf.sf_type_label(m)
            m["sql_type"] = gsf.preview_sql_type(m)
        return {"ok": all(c["ok"] for c in checks), "checks": checks,
                "columns": metas, "row_count": rows}
    finally:
        conn.close()


# ── Locking, heartbeat, run rows ─────────────────────────────────────────────
def _lock_is_stale(hb: Optional[datetime]) -> bool:
    if isinstance(hb, str):
        hb = datetime.fromisoformat(hb.rstrip("Z"))
    return hb is None or hb < _now() - timedelta(minutes=LOCK_STALE_MIN)


def start_run(job_id: int, run_type: str, user: Optional[str], attempt: int = 1,
              full_reload: bool = False) -> int:
    """Take the job's lock and open a run row. Raises AlreadyRunning when a
    live run holds the lock; a lock whose heartbeat stopped is taken over and
    that run is marked failed."""
    ensure_tables()
    with get_data_engine().begin() as c:
        job = c.execute(text(f"""
            SELECT JOB_ID, JOB_NAME, SOURCE_TYPE, SOURCE_OBJECT, TARGET_TABLE, LOAD_MODE,
                   LOCK_RUN_ID, LOCK_HEARTBEAT
            FROM {JOB_TABLE} WITH (UPDLOCK, HOLDLOCK) WHERE JOB_ID = :id
        """), {"id": job_id}).mappings().fetchone()
        if not job:
            raise GetDataError(f"Job {job_id} not found.")
        if job["LOCK_RUN_ID"] is not None:
            if not _lock_is_stale(job["LOCK_HEARTBEAT"]):
                raise AlreadyRunning(f"'{job['JOB_NAME']}' is already running "
                                     f"(run #{job['LOCK_RUN_ID']}).", int(job["LOCK_RUN_ID"]))
            c.execute(text(f"""
                UPDATE {RUN_TABLE} SET STATUS='failed', COMPLETED_AT=SYSUTCDATETIME(),
                    MESSAGE='Stopped responding (server restart or crash) — lock taken over by a new run.'
                WHERE RUN_ID=:rid AND STATUS='running'
            """), {"rid": job["LOCK_RUN_ID"]})
        run_id = int(c.execute(text(f"""
            INSERT INTO {RUN_TABLE} (JOB_ID, JOB_NAME, SOURCE_TYPE, SOURCE_OBJECT, TARGET_TABLE,
                LOAD_MODE, RUN_TYPE, TRIGGERED_BY, ATTEMPT, FULL_RELOAD, STATUS, STEP, HEARTBEAT_AT)
            OUTPUT INSERTED.RUN_ID
            VALUES (:jid, :jn, :st, :src, :tgt, :mode, :rt, :by, :att, :full, 'running',
                'Queued', SYSUTCDATETIME())
        """), {"jid": job_id, "jn": job["JOB_NAME"], "st": job["SOURCE_TYPE"],
               "src": job["SOURCE_OBJECT"], "tgt": job["TARGET_TABLE"], "mode": job["LOAD_MODE"],
               "rt": run_type, "by": user, "att": attempt, "full": 1 if full_reload else 0}).scalar())
        c.execute(text(f"""
            UPDATE {JOB_TABLE} SET LOCK_RUN_ID=:rid, LOCK_HEARTBEAT=SYSUTCDATETIME(),
                LAST_RUN_ID=:rid WHERE JOB_ID=:id
        """), {"rid": run_id, "id": job_id})
    return run_id


def record_skipped(job_id: int, job_name: str, reason: str, run_type: str = "AUTO",
                   user: Optional[str] = "scheduler") -> None:
    with get_data_engine().begin() as c:
        c.execute(text(f"""
            INSERT INTO {RUN_TABLE} (JOB_ID, JOB_NAME, RUN_TYPE, TRIGGERED_BY, STATUS,
                COMPLETED_AT, DURATION_MS, MESSAGE)
            VALUES (:jid, :jn, :rt, :by, 'skipped', SYSUTCDATETIME(), 0, :m)
        """), {"jid": job_id, "jn": job_name, "rt": run_type, "by": user, "m": reason[:4000]})


class _Heartbeat:
    """Keeps the job lock and the run row alive while a run works, and
    publishes its step and row progress for the UI."""

    def __init__(self, run_id: int, job_id: int):
        self.run_id, self.job_id = run_id, job_id
        self.step_text = "Queued"
        self.source_rows: Optional[int] = None
        self.rows_loaded: Optional[int] = None
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, name=f"GdHeartbeat-{run_id}", daemon=True)

    def start(self) -> "_Heartbeat":
        self._t.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def step(self, s: str) -> None:
        self.step_text = s
        self.flush()

    def flush(self) -> None:
        try:
            with get_data_engine().begin() as c:
                c.execute(text(f"""
                    UPDATE {RUN_TABLE} SET HEARTBEAT_AT=SYSUTCDATETIME(), STEP=:s,
                        SOURCE_ROWS=COALESCE(:src, SOURCE_ROWS),
                        ROWS_LOADED=COALESCE(:n, ROWS_LOADED)
                    WHERE RUN_ID=:rid AND STATUS='running'
                """), {"s": self.step_text[:200], "src": self.source_rows,
                       "n": self.rows_loaded, "rid": self.run_id})
                c.execute(text(f"""
                    UPDATE {JOB_TABLE} SET LOCK_HEARTBEAT=SYSUTCDATETIME()
                    WHERE JOB_ID=:jid AND LOCK_RUN_ID=:rid
                """), {"jid": self.job_id, "rid": self.run_id})
        except Exception as e:
            logger.warning(f"[get-data] heartbeat for run {self.run_id} failed: {e}")

    def _loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_SEC):
            self.flush()


def reconcile_stale() -> int:
    """Runs whose heartbeat stopped (worker crashed or restarted) → failed,
    and their job locks released. Safe with several workers: a live run keeps
    its heartbeat fresh, so only dead runs match."""
    try:
        ensure_tables()
        # OUTPUT, not rowcount: rowcount is -1 on a pooled connection that some
        # other code left with SET NOCOUNT ON.
        with get_data_engine().begin() as c:
            n = len(c.execute(text(f"""
                UPDATE {RUN_TABLE} SET STATUS='failed', COMPLETED_AT=SYSUTCDATETIME(),
                    STEP=NULL,
                    MESSAGE='Stopped responding (server restart or crash). The live table was not changed unless the swap had already finished.'
                OUTPUT INSERTED.RUN_ID
                WHERE STATUS='running'
                  AND COALESCE(HEARTBEAT_AT, STARTED_AT) < DATEADD(MINUTE, -{LOCK_STALE_MIN}, SYSUTCDATETIME())
            """)).fetchall())
            c.execute(text(f"""
                UPDATE {JOB_TABLE} SET LOCK_RUN_ID=NULL, LOCK_HEARTBEAT=NULL,
                    LAST_STATUS = CASE WHEN LAST_STATUS='running' THEN 'failed' ELSE LAST_STATUS END
                WHERE LOCK_RUN_ID IS NOT NULL
                  AND (LOCK_HEARTBEAT IS NULL
                       OR LOCK_HEARTBEAT < DATEADD(MINUTE, -{LOCK_STALE_MIN}, SYSUTCDATETIME()))
            """))
        if n:
            logger.warning(f"[get-data] reconciled {n} stale run(s) → failed")
        return n
    except Exception as e:
        logger.warning(f"[get-data] reconcile skipped: {e}")
        return 0


# ── SQL Server side of a load ────────────────────────────────────────────────
def _pyodbc():
    import pyodbc
    return pyodbc


def _input_size(spec: Dict[str, Any]):
    po = _pyodbc()
    k = spec["kind"]
    if k == "int":
        return (po.SQL_INTEGER, 0, 0)
    if k == "bigint":
        return (po.SQL_BIGINT, 0, 0)
    if k == "dec":
        return (po.SQL_DECIMAL, spec["precision"], spec["scale"])
    if k == "float":
        return (po.SQL_DOUBLE, 0, 0)
    if k in ("str", "time", "json"):
        # 0 = (N)VARCHAR(MAX); VARCHAR only when every value was plain ASCII
        return (po.SQL_VARCHAR if spec.get("varchar") else po.SQL_WVARCHAR, spec.get("width") or 0, 0)
    if k == "date":
        return (po.SQL_TYPE_DATE, 0, 0)
    if k == "datetime":
        return (po.SQL_TYPE_TIMESTAMP, 26, 6)
    if k == "bit":
        return (po.SQL_BIT, 0, 0)
    return (po.SQL_VARBINARY, 0, 0)


def _converter(spec: Dict[str, Any]) -> Callable[[Any], Any]:
    k = spec["kind"]
    if k in ("int", "bigint"):
        return lambda v: None if v is None else int(v)
    if k == "dec":
        return lambda v: None if v is None else (v if isinstance(v, Decimal) else Decimal(str(v)))
    if k == "float":
        return lambda v: None if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else float(v)
    if k == "str":
        return lambda v: None if v is None else (v if isinstance(v, str) else str(v))
    if k == "date":
        return lambda v: v.date() if isinstance(v, datetime) else v
    if k == "datetime":
        return lambda v: (v.astimezone(timezone.utc).replace(tzinfo=None)
                          if isinstance(v, datetime) and v.tzinfo else v)
    if k == "time":   # always with microseconds — the bulk loader writes the same text
        return lambda v: None if v is None else (
            v.isoformat(timespec="microseconds") if isinstance(v, dtime) else str(v))
    if k == "bit":
        return lambda v: None if v is None else bool(v)
    if k == "json":
        return lambda v: None if v is None else (v if isinstance(v, str) else json.dumps(v, default=str))
    return lambda v: None if v is None else bytes(v)


def _sql_ident(name: str) -> str:
    return "[" + str(name).replace("]", "]]") + "]"


def _table_exists(conn, table: str) -> bool:
    return bool(conn.execute(text("SELECT OBJECT_ID('dbo.' + :t, 'U')"), {"t": table}).scalar())


def _count(table: str) -> Optional[int]:
    with get_data_engine().connect() as c:
        if not _table_exists(c, table):
            return None
        return int(c.execute(text(f"SELECT COUNT_BIG(*) FROM dbo.[{table}]")).scalar())


def _drop(table: str) -> None:
    with get_data_engine().begin() as c:
        c.execute(text(f"IF OBJECT_ID('dbo.[{table}]','U') IS NOT NULL DROP TABLE dbo.[{table}]"))


def _build_specs(metas: List[Dict[str, Any]], profiles: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    specs, used = [], set()
    for m in metas:
        base = str(m["name"])[:120]
        sql_name, i = base, 2
        while sql_name.upper() in used:   # SQL Server compares names case-insensitively
            sql_name = f"{base}_{i}"
            i += 1
        used.add(sql_name.upper())
        prof = profiles.get(m["name"]) or {}
        spec = gsf.map_column(m, prof)
        spec.update({"sf_name": m["name"], "sql_name": sql_name,
                     "nn": prof.get("nn"), "max_len": prof.get("max_len")})
        specs.append(spec)
    return specs


def _create_stage(stage: str, specs: List[Dict[str, Any]]) -> None:
    """A fresh, empty stage table (a heap — bulk loads into it take TABLOCK)."""
    cols_ddl = ",\n  ".join(f"{_sql_ident(s['sql_name'])} {s['sql_type']} NULL" for s in specs)
    with get_data_engine().begin() as c:
        c.execute(text(f"IF OBJECT_ID('dbo.[{stage}]','U') IS NOT NULL DROP TABLE dbo.[{stage}]"))
        c.execute(text(f"""CREATE TABLE dbo.[{stage}] (
  {cols_ddl},
  [_GD_RUN_ID] BIGINT NULL,
  [_GD_LOADED_AT] DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME()
)"""))


def _profile(sf_conn, qid: str, metas: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """One pass over the data query's own result (RESULT_SCAN — the view is
    not computed again). Per column: non-null count; text: max length, values
    that aren't plain printable ASCII, values with 0x1E/0x1F/NUL; numbers:
    sum (and max |x| for NUMBER(>18,0)); floats: finite count and sum.
    These size and type the stage table, pick the loader, and are the
    figures the loaded stage table must reproduce (_verify_stage)."""
    exprs, plan = [], []
    nan = "('NaN'::FLOAT, 'inf'::FLOAT, '-inf'::FLOAT)"
    for i, m in enumerate(metas):
        q, t = gsf.qcol(m["name"]), m["sf_type"]
        exprs.append(f"COUNT({q}) AS NN_{i}")
        if t == "TEXT":
            exprs += [f"MAX(LENGTH({q})) AS ML_{i}",
                      # printable ASCII plus tab / CR / LF — all safe in VARCHAR
                      f"COUNT_IF(NOT ({q} RLIKE '[ -~\\t\\r\\n]*')) AS NA_{i}",
                      # the bulk files' own separators — line breaks are fine
                      f"COUNT_IF(CONTAINS({q}, CHR(30)) OR CONTAINS({q}, CHR(31)) OR "
                      f"CONTAINS({q}, CHR(0))) AS UN_{i}"]
        elif t == "FIXED":
            exprs.append(f"SUM({q}) AS SM_{i}")
            if not (m.get("scale") or 0) and int(m.get("precision") or 38) > 18:
                exprs.append(f"MAX(ABS({q})) AS MA_{i}")
        elif t == "REAL":
            exprs += [f"COUNT_IF({q} IS NOT NULL AND {q} NOT IN {nan}) AS NF_{i}",
                      f"SUM(IFF({q} IN {nan}, NULL, {q})) AS SF_{i}",
                      f"SUM(ABS(IFF({q} IN {nan}, NULL, {q}))) AS SA_{i}"]
        plan.append((i, m))
    cur = sf_conn.cursor()
    try:
        cur.execute(f"SELECT {', '.join(exprs)} FROM TABLE(RESULT_SCAN('{qid}'))")
        row = cur.fetchone()
        names = [d[0].upper() for d in cur.description]
    finally:
        cur.close()
    v = dict(zip(names, row))
    out: Dict[str, Dict[str, Any]] = {}
    for i, m in plan:
        p = {"nn": v.get(f"NN_{i}")}
        if m["sf_type"] == "TEXT":
            p.update(max_len=v.get(f"ML_{i}"), non_ascii=int(v.get(f"NA_{i}") or 0),
                     unsafe=int(v.get(f"UN_{i}") or 0))
        elif m["sf_type"] == "FIXED":
            p["sum"] = v.get(f"SM_{i}")
            if f"MA_{i}" in v:
                p["max_abs"] = v.get(f"MA_{i}")
        elif m["sf_type"] == "REAL":
            p.update(nn=v.get(f"NF_{i}"), fsum=v.get(f"SF_{i}"), fabs=v.get(f"SA_{i}"))
        out[m["name"]] = p
    return out


def _verify_stage(stage: str, specs: List[Dict[str, Any]], profiles: Dict[str, Dict[str, Any]],
                  expected_rows: int) -> str:
    """The loaded stage table must reproduce Snowflake's own figures: row count,
    non-null count of every column, exact sum of every number column (floats
    within 1e-8 × Σ|x|). Any difference fails the run before the live table is
    touched."""
    exprs = ["COUNT_BIG(*)"]
    for s in specs:
        col = _sql_ident(s["sql_name"])
        exprs.append(f"COUNT_BIG({col})")
        if s["kind"] in ("int", "bigint"):
            exprs.append(f"SUM(CAST({col} AS DECIMAL(38,0)))")
        elif s["kind"] == "dec":
            exprs.append(f"SUM({col})")
        elif s["kind"] == "float":
            exprs.append(f"SUM({col})")
    with get_data_engine().connect() as c:
        try:
            row = list(c.execute(text(f"SELECT {', '.join(exprs)} FROM dbo.[{stage}]")).fetchone())
        except Exception as e:
            if "overflow" not in str(e).lower():
                raise
            return "checksums skipped (a column sum overflows DECIMAL(38))"
    got_rows, i, bad = int(row[0]), 1, []
    if got_rows != expected_rows:
        bad.append(f"rows {got_rows:,} vs {expected_rows:,}")
    for s in specs:
        p = profiles.get(s["sf_name"]) or {}
        nn = row[i]; i += 1
        if p.get("nn") is not None and int(nn) != int(p["nn"]):
            bad.append(f"{s['sf_name']}: {int(nn):,} values vs {int(p['nn']):,} in Snowflake")
        if s["kind"] in ("int", "bigint", "dec", "float"):
            got = row[i]; i += 1
            want = p.get("fsum") if s["kind"] == "float" else p.get("sum")
            if want is None and got is None:
                continue
            if s["kind"] == "float":
                # Float sums depend on addition order; the drift is bounded by
                # n·ε·Σ|x|, so the tolerance scales with Σ|x|, not with the total.
                a, b = float(got or 0), float(want or 0)
                tol = max(1e-6, 1e-8 * float(p.get("fabs") or abs(b)))
                if abs(a - b) > tol:
                    bad.append(f"{s['sf_name']}: sum {a!r} vs {b!r}")
            elif Decimal(str(got or 0)) != Decimal(str(want or 0)):
                bad.append(f"{s['sf_name']}: sum {got} vs {want}")
    if bad:
        raise GetDataError("Checksum mismatch — " + "; ".join(bad[:5]) +
                           ". The load was cancelled and the live table was left unchanged.")
    n_sums = sum(1 for s in specs if s["kind"] in ("int", "bigint", "dec", "float"))
    return f"verified: rows, {len(specs)} column counts, {n_sums} column sums"


def _load_stage(sf_cur, stage: str, specs: List[Dict[str, Any]], run_id: int,
                hb: _Heartbeat) -> int:
    """Classic loader: stream the open Snowflake result into a fresh stage
    table with parameterised inserts. Commits per batch — the stage table is
    private to this run. Used when the bulk path can't take the data."""
    _create_stage(stage, specs)
    raw = get_data_engine().raw_connection()
    try:
        mc = raw.cursor()
        col_list = ", ".join(_sql_ident(s["sql_name"]) for s in specs) + ", [_GD_RUN_ID]"
        insert = (f"INSERT INTO dbo.[{stage}] ({col_list}) "
                  f"VALUES ({', '.join('?' for _ in range(len(specs) + 1))})")
        sizes = [_input_size(s) for s in specs] + [(_pyodbc().SQL_BIGINT, 0, 0)]
        convs = [_converter(s) for s in specs]
        mc.fast_executemany = True
        total, last_flush = 0, time.time()
        while True:
            batch = sf_cur.fetchmany(FETCH_BATCH)
            if not batch:
                break
            rows = [tuple(f(v) for f, v in zip(convs, r)) + (run_id,) for r in batch]
            mc.setinputsizes(sizes)
            mc.executemany(insert, rows)
            raw.commit()
            total += len(rows)
            hb.rows_loaded = total
            if time.time() - last_flush > 5:
                hb.flush()
                last_flush = time.time()
        return total
    except Exception:
        try:
            raw.rollback()
        except Exception:
            pass
        raise
    finally:
        raw.close()


def _swap(stage: str, target: str) -> None:
    """Stage becomes the live table in one transaction; the old copy is dropped
    after commit. Readers see either yesterday's table or today's — never half."""
    old = f"{target}__OLD"
    with get_data_engine().begin() as c:
        c.execute(text(f"IF OBJECT_ID('dbo.[{old}]','U') IS NOT NULL DROP TABLE dbo.[{old}]"))
        if _table_exists(c, target):
            c.execute(text(f"EXEC sp_rename 'dbo.{target}', '{old}'"))
        c.execute(text(f"EXEC sp_rename 'dbo.{stage}', '{target}'"))
    _drop(old)


def _index_keys(table: str, target: str, key_sql: List[str]) -> None:
    try:
        with get_data_engine().begin() as c:
            c.execute(text(f"CREATE CLUSTERED INDEX [CX_{target}] ON dbo.[{table}] "
                           f"({', '.join(_sql_ident(k) for k in key_sql)})"))
    except Exception as e:
        logger.warning(f"[get-data] could not index {target} on {key_sql}: {e}")


def _target_columns(c, table: str) -> Dict[str, Dict[str, Any]]:
    rows = c.execute(text("""
        SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE
        FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = :t
    """), {"t": table}).fetchall()
    return {r[0].upper(): {"name": r[0], "type": r[1].lower(), "len": r[2],
                           "p": r[3], "s": r[4]} for r in rows}


def _align_schema(target: str, specs: List[Dict[str, Any]]) -> List[str]:
    """Incremental / append into an existing table: add new source columns and
    widen text and integer columns that the new batch needs. Returns changes."""
    changes = []
    with get_data_engine().begin() as c:
        have = _target_columns(c, target)
        for s in specs:
            col = have.get(s["sql_name"].upper())
            ident = _sql_ident(col["name"] if col else s["sql_name"])
            if not col:
                c.execute(text(f"ALTER TABLE dbo.[{target}] ADD {ident} {s['sql_type']} NULL"))
                changes.append(f"added {s['sql_name']}")
                continue
            if s["kind"] in ("str", "time", "json") and col["type"] in ("varchar", "nvarchar"):
                if s.get("nn") == 0:         # all NULL in this batch — nothing to fit
                    continue
                cur_w = col["len"]           # -1 = MAX
                need = s.get("width")        # None = MAX
                longest = s.get("max_len")   # measured in this batch (None = not measured)
                # A VARCHAR column must become NVARCHAR the first time a batch
                # carries non-ASCII text, or SQL Server would turn it into '?'.
                to_unicode = col["type"] == "varchar" and not s.get("varchar")
                wider = cur_w != -1 and (
                    (longest is not None and int(longest) > cur_w) or
                    (longest is None and (need is None or need > cur_w)))
                if to_unicode or wider:
                    base = "NVARCHAR" if (col["type"] == "nvarchar" or to_unicode) else "VARCHAR"
                    width = "MAX" if (cur_w == -1 or need is None) else str(max(cur_w, need))
                    c.execute(text(f"ALTER TABLE dbo.[{target}] ALTER COLUMN {ident} {base}({width}) NULL"))
                    changes.append(f"{'made Unicode' if to_unicode else 'widened'} {col['name']}")
            elif s["kind"] in ("bigint", "dec") and col["type"] in ("int", "bigint"):
                if s["kind"] == "bigint" and col["type"] == "int":
                    c.execute(text(f"ALTER TABLE dbo.[{target}] ALTER COLUMN {ident} BIGINT NULL"))
                    changes.append(f"widened {col['name']} to BIGINT")
                elif s["kind"] == "dec":
                    c.execute(text(f"ALTER TABLE dbo.[{target}] ALTER COLUMN {ident} {s['sql_type']} NULL"))
                    changes.append(f"widened {col['name']} to {s['sql_type']}")
            elif s["kind"] == "dec" and col["type"] == "decimal":
                ip = max((col["p"] or 0) - (col["s"] or 0), s["precision"] - s["scale"])
                sc = max(col["s"] or 0, s["scale"])
                if ip + sc > (col["p"] or 0) or sc > (col["s"] or 0):
                    c.execute(text(f"ALTER TABLE dbo.[{target}] ALTER COLUMN {ident} "
                                   f"DECIMAL({min(ip + sc, 38)},{sc}) NULL"))
                    changes.append(f"widened {col['name']}")
        for audit, ddl in (("_GD_RUN_ID", "BIGINT NULL"),
                           ("_GD_LOADED_AT", "DATETIME2(0) NULL")):
            if audit not in have:
                c.execute(text(f"ALTER TABLE dbo.[{target}] ADD [{audit}] {ddl}"))
    return changes


def _merge(stage: str, target: str, specs: List[Dict[str, Any]], key_sql: List[str],
           wm_sql: str) -> Tuple[int, int, int]:
    """Upsert the batch on the key columns. Duplicate keys inside the batch keep
    the row with the highest watermark. NULL keys match NULL keys."""
    cols = [s["sql_name"] for s in specs] + ["_GD_RUN_ID", "_GD_LOADED_AT"]
    keyset = {k.upper() for k in key_sql}
    on = " AND ".join(f"(t.{_sql_ident(k)} = s.{_sql_ident(k)} OR "
                      f"(t.{_sql_ident(k)} IS NULL AND s.{_sql_ident(k)} IS NULL))" for k in key_sql)
    upd = ", ".join(f"t.{_sql_ident(c)} = s.{_sql_ident(c)}" for c in cols if c.upper() not in keyset)
    ins_cols = ", ".join(_sql_ident(c) for c in cols)
    ins_vals = ", ".join(f"s.{_sql_ident(c)}" for c in cols)
    part = ", ".join(_sql_ident(k) for k in key_sql)
    # SET NOCOUNT persists on the pooled connection after the batch, which makes
    # rowcount -1 for whoever reuses it — so it is switched back off, and on any
    # error the connection is discarded rather than returned to the pool.
    with get_data_engine().begin() as c:
        dups = int(c.execute(text(f"""
            SELECT COUNT_BIG(*) FROM (SELECT ROW_NUMBER() OVER (PARTITION BY {part}
                ORDER BY {_sql_ident(wm_sql)} DESC) AS rn FROM dbo.[{stage}]) x WHERE rn > 1
        """)).scalar() or 0)
        try:
            res = _merge_batch(c, target, stage, part, wm_sql, on, upd, ins_cols, ins_vals)
            c.exec_driver_sql("SET NOCOUNT OFF")
        except Exception:
            c.invalidate()
            raise
    return int(res[0]), int(res[1]), dups


def _merge_batch(c, target: str, stage: str, part: str, wm_sql: str, on: str, upd: str,
                 ins_cols: str, ins_vals: str):
    return c.execute(text(f"""
            SET NOCOUNT ON;
            DECLARE @a TABLE (act NVARCHAR(10));
            MERGE dbo.[{target}] WITH (HOLDLOCK) AS t
            USING (SELECT * FROM (SELECT s0.*, ROW_NUMBER() OVER (PARTITION BY {part}
                        ORDER BY {_sql_ident(wm_sql)} DESC) AS [__GD_RN]
                   FROM dbo.[{stage}] s0) x WHERE [__GD_RN] = 1) AS s
            ON {on}
            WHEN MATCHED THEN UPDATE SET {upd}
            WHEN NOT MATCHED BY TARGET THEN INSERT ({ins_cols}) VALUES ({ins_vals})
            OUTPUT $action INTO @a;
            SELECT COALESCE(SUM(CASE WHEN act = 'INSERT' THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN act = 'UPDATE' THEN 1 ELSE 0 END), 0) FROM @a;
        """)).fetchone()


def _append(stage: str, target: str, specs: List[Dict[str, Any]]) -> None:
    cols = ", ".join(_sql_ident(s["sql_name"]) for s in specs) + ", [_GD_RUN_ID], [_GD_LOADED_AT]"
    with get_data_engine().begin() as c:
        c.execute(text(f"INSERT INTO dbo.[{target}] ({cols}) SELECT {cols} FROM dbo.[{stage}]"))


def _wm_string(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S.%f")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return format(v, "f")
    return str(v)


# ── The engine ───────────────────────────────────────────────────────────────
def _sync(job: Dict[str, Any], run: Dict[str, Any], hb: _Heartbeat) -> Dict[str, Any]:
    fq = job["SOURCE_OBJECT"]
    src = gsf.quote_object(fq)
    target = job["TARGET_TABLE"]
    stage = f"{target}__STG"
    mode = job["LOAD_MODE"]
    full = bool(run.get("FULL_RELOAD"))
    keys = job.get("KEY_COLS") or []
    wm_col = job.get("WATERMARK_COL")
    run_id = int(run["RUN_ID"])

    with get_data_engine().connect() as c:
        target_exists = _table_exists(c, target)
    stats: Dict[str, Any] = {"TARGET_ROWS_BEFORE": _count(target) if target_exists else None}
    as_new_table = mode == "replace" or not target_exists or full

    hb.step("Reading source columns")
    sfconn = gsf.connect(long_running=True)
    try:
        cur = sfconn.cursor()
        try:
            metas = gsf.describe(cur, f"SELECT * FROM {src}")
        except Exception as e:
            raise GetDataError(f"Cannot read {fq}: {gsf.sf_message(e)}")
        names = [m["name"] for m in metas]
        dups = gsf._dup_names(metas)
        if dups:
            raise GetDataError(f"Source has duplicate column names (case-insensitive): {', '.join(dups)}")
        reserved = [n for n in names if n.upper() in gsf.AUDIT_COLS]
        if reserved:
            raise GetDataError(f"Source column {', '.join(reserved)} clashes with a load audit column.")
        key_sf = []
        for k in keys:
            real = _resolve_col(names, k)
            if not real:
                raise GetDataError(f"Key column {k} is not in {fq}.")
            key_sf.append(real)
        wm_sf = None
        if mode == "incremental":
            wm_sf = _resolve_col(names, wm_col or "")
            if not wm_sf:
                raise GetDataError(f"Watermark column {wm_col} is not in {fq}.")

        where, params = "", None
        wm_from = job.get("WATERMARK_VALUE") if (mode == "incremental" and not as_new_table) else None
        if wm_from is not None:
            # >= re-reads the boundary value; the MERGE makes that harmless and
            # it catches rows that arrived late for the last loaded day/time.
            where, params = f" WHERE {gsf.qcol(wm_sf)} >= %s", (wm_from,)
        stats["WATERMARK_FROM"] = wm_from

        # ONE pass over the source: the data query runs first, and the profile
        # (widths, ASCII-only, unsafe characters, checksums) reads that query's
        # stored result, so a heavy view is computed once, not twice.
        hb.step("Querying Snowflake")
        cur.execute(f"SELECT {', '.join(gsf.qcol(n) for n in names)} FROM {src}{where}", params)
        stats["SF_QUERY_ID"] = qid = getattr(cur, "sfqid", None)
        rc = getattr(cur, "rowcount", None)
        source_rows = int(rc) if rc is not None and rc >= 0 else None
        stats["SOURCE_ROWS"] = source_rows
        hb.source_rows = source_rows
        # Empty-source guard: a full replace that receives nothing while the
        # local table has data almost always means the source was emptied
        # upstream (2026-10-01: AKS_GOLD.GLD_MONTH_PLAN_APPROVED was emptied at
        # 09:31 IST and the next run wiped 3.7M local rows). Keep the data.
        before = stats.get("TARGET_ROWS_BEFORE") or 0
        if (source_rows == 0 and as_new_table and target_exists and before > 0
                and not job.get("ALLOW_EMPTY")):
            raise GetDataError(
                f"Snowflake returned 0 rows from {fq}, but {target} has {before:,}. The run was "
                f"stopped and the existing rows were kept. If the source is meant to be empty, tick "
                f"'Allow an empty Snowflake result' on the job.")
        hb.step("Profiling columns")
        profiles = _profile(sfconn, qid, metas)
        specs = _build_specs(metas, profiles)
        by_sf = {s["sf_name"]: s for s in specs}

        if (job.get("LOADER_PREF") or "bulk") == "classic":
            fallback = "classic insert chosen on the job"
        else:
            fallback = gdb.fast_path_reason(specs, profiles)
        _create_stage(stage, specs)
        if fallback is None:
            hb.step(f"Bulk loading into SQL Server ({gdb.PARALLEL} parallel)")
            try:
                fetched = gdb.load_stage_bulk(cur, stage, specs, run_id,
                                              _now().strftime("%Y-%m-%d %H:%M:%S"), hb)
                stats["LOADER"] = "bulk"
            except gdb.FastPathUnsupported as e:
                # An Arrow batch the bulk path can't encode losslessly. The stage
                # is rebuilt empty, so no partial rows survive, and the classic
                # loader re-reads the same stored result.
                fallback = str(e)
                _create_stage(stage, specs)
                cur.execute(f"SELECT * FROM TABLE(RESULT_SCAN('{qid}'))")
        if fallback is not None:
            hb.step("Loading into SQL Server (classic)")
            fetched = _load_stage(cur, stage, specs, run_id, hb)
            stats["LOADER"] = "classic"
            stats["LOADER_NOTE"] = fallback
    finally:
        try:
            sfconn.close()
        except Exception:
            pass

    hb.step("Verifying row count and checksums")
    staged = _count(stage) or 0
    expected = source_rows if source_rows is not None else fetched
    stats["SOURCE_ROWS"] = expected
    stats["ROWS_LOADED"] = staged
    if staged != expected or fetched != expected:
        _drop(stage)
        raise GetDataError(
            f"Count mismatch: Snowflake returned {expected:,} rows but {staged:,} reached "
            f"SQL Server. The load was cancelled and {target} was left unchanged.")
    try:
        verified = _verify_stage(stage, specs, profiles, expected)
    except Exception:
        _drop(stage)
        raise

    wm_sql = by_sf[wm_sf]["sql_name"] if wm_sf else None
    key_sql = [by_sf[k]["sql_name"] for k in key_sf]
    new_wm = None
    if wm_sql and staged:
        with get_data_engine().connect() as c:
            new_wm = _wm_string(c.execute(text(
                f"SELECT MAX({_sql_ident(wm_sql)}) FROM dbo.[{stage}]")).scalar())
    stats["WATERMARK_TO"] = new_wm or stats.get("WATERMARK_FROM")

    changes: List[str] = []
    if as_new_table:
        if key_sql:
            hb.step("Indexing key columns")
            _index_keys(stage, target, key_sql)
        hb.step("Swapping in the new table")
        _swap(stage, target)
        stats.update(ROWS_INSERTED=staged, ROWS_UPDATED=0)
        how = "first full load" if mode != "replace" and not target_exists else (
            "full reload" if full and mode != "replace" else MODE_LABEL["replace"])
    elif mode == "incremental":
        hb.step("Merging changes")
        changes = _align_schema(target, specs)
        ins, upd, dup = _merge(stage, target, specs, key_sql, wm_sql)
        _drop(stage)
        stats.update(ROWS_INSERTED=ins, ROWS_UPDATED=upd)
        how = f"incremental: {ins:,} new, {upd:,} updated" + (
            f", {dup:,} duplicate key(s) in the batch kept the latest" if dup else "")
    else:
        hb.step("Appending rows")
        changes = _align_schema(target, specs)
        _append(stage, target, specs)
        _drop(stage)
        stats.update(ROWS_INSERTED=staged, ROWS_UPDATED=0)
        how = MODE_LABEL["append"]

    stats["TARGET_ROWS_AFTER"] = _count(target)
    stats["NEW_WATERMARK"] = new_wm
    loader = (f"bulk copy ({gdb.auth_label()})" if stats.get("LOADER") == "bulk"
              else f"classic insert ({stats.get('LOADER_NOTE')})")
    msg = f"Loaded {staged:,} row(s) into {target} ({how}) via {loader}; {verified}."
    if changes:
        msg += " Schema: " + "; ".join(changes) + "."
    stats["MESSAGE"] = msg
    return stats


def execute_run(run_id: int, slot: Optional[threading.Semaphore] = None) -> Dict[str, Any]:
    """Run a started run to completion. Never raises — failures are recorded
    on the run, and a failed AUTO run is retried once if the job allows it."""
    with get_data_engine().connect() as c:
        run = c.execute(text(f"SELECT * FROM {RUN_TABLE} WHERE RUN_ID=:id"),
                        {"id": run_id}).mappings().fetchone()
    if not run:
        return {"success": False, "error": f"run {run_id} not found"}
    run = dict(run)
    job_id = int(run["JOB_ID"])
    hb = _Heartbeat(run_id, job_id).start()
    t0 = time.time()
    stats: Dict[str, Any] = {}
    status, message = "failed", ""
    job = None
    try:
        if slot is not None:
            hb.step("Waiting for a free slot")
            slot.acquire()
        try:
            job = get_job(job_id, raw=True)
            if not job:
                raise GetDataError("The job was deleted before the run started.")
            stats = _sync(job, run, hb)
            status, message = "success", stats.pop("MESSAGE", "")
        finally:
            if slot is not None:
                slot.release()
    except Exception as e:
        if isinstance(e, GetDataError):
            message = str(e)
            logger.warning(f"[get-data] run {run_id} failed: {e}")
        else:
            message = f"{type(e).__name__}: {e}"
            logger.exception(f"[get-data] run {run_id} crashed")
        try:
            if job:
                _drop(f"{job['TARGET_TABLE']}__STG")
        except Exception:
            pass
    finally:
        hb.stop()

    dur = int((time.time() - t0) * 1000)
    retry_at = None
    if (status == "failed" and job and run["RUN_TYPE"] == "AUTO" and int(run["ATTEMPT"]) == 1
            and job.get("RETRY_ON_FAIL") and job.get("ENABLED")):
        retry_at = _now() + timedelta(minutes=int(job.get("RETRY_DELAY_MIN") or 15))
        message += f" Retrying once at {_ist_label(retry_at)}."
    if status == "success":
        message += f" Took {_fmt_dur(dur)}."

    new_wm = stats.pop("NEW_WATERMARK", None)
    with get_data_engine().begin() as c:
        c.execute(text(f"""
            UPDATE {RUN_TABLE} SET STATUS=:st, STEP=NULL, COMPLETED_AT=SYSUTCDATETIME(),
                HEARTBEAT_AT=SYSUTCDATETIME(), DURATION_MS=:d, MESSAGE=:m,
                SOURCE_ROWS=COALESCE(:src, SOURCE_ROWS), ROWS_LOADED=COALESCE(:rl, ROWS_LOADED),
                ROWS_INSERTED=:ri, ROWS_UPDATED=:ru, TARGET_ROWS_BEFORE=:tb,
                TARGET_ROWS_AFTER=:ta, WATERMARK_FROM=:wf, WATERMARK_TO=:wt, SF_QUERY_ID=:q,
                LOADER=:ld
            WHERE RUN_ID=:id
        """), {"st": status, "d": dur, "m": message[:4000],
               "src": stats.get("SOURCE_ROWS"), "rl": stats.get("ROWS_LOADED"),
               "ri": stats.get("ROWS_INSERTED"), "ru": stats.get("ROWS_UPDATED"),
               "tb": stats.get("TARGET_ROWS_BEFORE"), "ta": stats.get("TARGET_ROWS_AFTER"),
               "wf": stats.get("WATERMARK_FROM"), "wt": stats.get("WATERMARK_TO"),
               "q": stats.get("SF_QUERY_ID"), "ld": stats.get("LOADER"), "id": run_id})
        c.execute(text(f"""
            UPDATE {JOB_TABLE} SET LAST_RUN_AT=SYSUTCDATETIME(), LAST_STATUS=:st,
                LAST_ROWS=:rows, LAST_MESSAGE=:m,
                WATERMARK_VALUE = CASE WHEN :st = 'success' AND :wm IS NOT NULL THEN :wm
                                       ELSE WATERMARK_VALUE END,
                RETRY_AT = CASE WHEN :st = 'success' THEN NULL ELSE COALESCE(:retry, RETRY_AT) END
            WHERE JOB_ID=:jid
        """), {"st": status, "rows": stats.get("ROWS_LOADED") if status == "success" else None,
               "m": message[:1000], "wm": new_wm, "retry": retry_at, "jid": job_id})
        c.execute(text(f"""
            UPDATE {JOB_TABLE} SET LOCK_RUN_ID=NULL, LOCK_HEARTBEAT=NULL
            WHERE JOB_ID=:jid AND LOCK_RUN_ID=:rid
        """), {"jid": job_id, "rid": run_id})
    logger.info(f"[get-data] run {run_id} ({run['RUN_TYPE']}) {status}: {message}")
    return {"success": status == "success", "run_id": run_id, "status": status, "message": message}


# ── History ──────────────────────────────────────────────────────────────────
def _ist_day_bounds(d_from: Optional[str], d_to: Optional[str]) -> Tuple[Optional[datetime], Optional[datetime]]:
    lo = hi = None
    if d_from:
        lo = datetime.fromisoformat(str(d_from)[:10]) - IST
    if d_to:
        hi = datetime.fromisoformat(str(d_to)[:10]) + timedelta(days=1) - IST
    return lo, hi


def list_runs(date_from: Optional[str] = None, date_to: Optional[str] = None,
              job_id: Optional[int] = None, run_type: Optional[str] = None,
              status: Optional[str] = None, limit: int = 200, offset: int = 0) -> Dict[str, Any]:
    ensure_tables()
    lo, hi = _ist_day_bounds(date_from, date_to)
    where, params = ["1=1"], {}
    if lo:
        where.append("STARTED_AT >= :lo"); params["lo"] = lo
    if hi:
        where.append("STARTED_AT < :hi"); params["hi"] = hi
    if job_id:
        where.append("JOB_ID = :jid"); params["jid"] = int(job_id)
    if run_type and run_type.upper() in ("AUTO", "MANUAL"):
        where.append("RUN_TYPE = :rt"); params["rt"] = run_type.upper()
    if status and status.lower() in ("running", "success", "failed", "skipped"):
        where.append("STATUS = :st"); params["st"] = status.lower()
    w = " AND ".join(where)
    lim = max(1, min(int(limit or 200), 2000))
    off = max(0, int(offset or 0))
    with get_data_engine().connect() as c:
        rows = c.execute(text(f"""
            SELECT RUN_ID, JOB_ID, JOB_NAME, SOURCE_TYPE, SOURCE_OBJECT, TARGET_TABLE, LOAD_MODE,
                   RUN_TYPE, TRIGGERED_BY, ATTEMPT, FULL_RELOAD, STATUS, STEP, SOURCE_ROWS,
                   ROWS_LOADED, ROWS_INSERTED, ROWS_UPDATED, TARGET_ROWS_BEFORE, TARGET_ROWS_AFTER,
                   WATERMARK_FROM, WATERMARK_TO, SF_QUERY_ID, LOADER, STARTED_AT, HEARTBEAT_AT,
                   COMPLETED_AT, DURATION_MS, MESSAGE
            FROM {RUN_TABLE} WHERE {w}
            ORDER BY RUN_ID DESC OFFSET {off} ROWS FETCH NEXT {lim} ROWS ONLY
        """), params).mappings().fetchall()
        s = c.execute(text(f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN RUN_TYPE='AUTO' THEN 1 ELSE 0 END) AS auto_runs,
                   SUM(CASE WHEN RUN_TYPE='MANUAL' THEN 1 ELSE 0 END) AS manual_runs,
                   SUM(CASE WHEN STATUS='success' THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN STATUS='failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN STATUS='skipped' THEN 1 ELSE 0 END) AS skipped,
                   SUM(CASE WHEN STATUS='running' THEN 1 ELSE 0 END) AS running,
                   SUM(CASE WHEN STATUS='success' THEN ROWS_LOADED ELSE 0 END) AS rows_loaded
            FROM {RUN_TABLE} WHERE {w}
        """), params).mappings().fetchone()
    summary = {k: int(v or 0) for k, v in dict(s).items()}
    return {"items": [_row(r) for r in rows], "total": summary["total"], "summary": summary}


def get_run(run_id: int) -> Optional[Dict[str, Any]]:
    ensure_tables()
    with get_data_engine().connect() as c:
        r = c.execute(text(f"SELECT * FROM {RUN_TABLE} WHERE RUN_ID=:id"),
                      {"id": run_id}).mappings().fetchone()
    return _row(r) if r else None


def overview() -> Dict[str, Any]:
    ensure_tables()
    today_lo = datetime.combine((_now() + IST).date(), dtime()) - IST
    week_lo = today_lo - timedelta(days=6)
    with get_data_engine().connect() as c:
        jobs = c.execute(text(f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN ENABLED=1 THEN 1 ELSE 0 END) AS enabled,
                   SUM(CASE WHEN ENABLED=1 AND TRIGGER_TYPE='schedule' THEN 1 ELSE 0 END) AS scheduled,
                   SUM(CASE WHEN LOCK_RUN_ID IS NOT NULL THEN 1 ELSE 0 END) AS running
            FROM {JOB_TABLE}
        """)).mappings().fetchone()
        today = c.execute(text(f"""
            SELECT COUNT(*) AS runs,
                   SUM(CASE WHEN STATUS='success' THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN STATUS='failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN STATUS='running' THEN 1 ELSE 0 END) AS running,
                   SUM(CASE WHEN RUN_TYPE='AUTO' THEN 1 ELSE 0 END) AS auto_runs,
                   SUM(CASE WHEN RUN_TYPE='MANUAL' THEN 1 ELSE 0 END) AS manual_runs,
                   SUM(CASE WHEN STATUS='success' THEN ROWS_LOADED ELSE 0 END) AS rows_loaded
            FROM {RUN_TABLE} WHERE STARTED_AT >= :lo
        """), {"lo": today_lo}).mappings().fetchone()
        nxt = c.execute(text(f"""
            SELECT TOP 5 JOB_ID, JOB_NAME, NEXT_RUN_AT, RETRY_AT, TARGET_TABLE FROM {JOB_TABLE}
            WHERE ENABLED=1 AND (NEXT_RUN_AT IS NOT NULL OR RETRY_AT IS NOT NULL)
            ORDER BY CASE WHEN RETRY_AT IS NOT NULL AND (NEXT_RUN_AT IS NULL OR RETRY_AT < NEXT_RUN_AT)
                          THEN RETRY_AT ELSE NEXT_RUN_AT END
        """)).mappings().fetchall()
        fails = c.execute(text(f"""
            SELECT TOP 5 RUN_ID, JOB_ID, JOB_NAME, RUN_TYPE, STARTED_AT, MESSAGE FROM {RUN_TABLE}
            WHERE STATUS='failed' AND STARTED_AT >= :lo ORDER BY RUN_ID DESC
        """), {"lo": week_lo}).mappings().fetchall()
        days = c.execute(text(f"""
            SELECT CAST(DATEADD(MINUTE, 330, STARTED_AT) AS DATE) AS run_day,
                   SUM(CASE WHEN RUN_TYPE='AUTO' THEN 1 ELSE 0 END) AS auto_runs,
                   SUM(CASE WHEN RUN_TYPE='MANUAL' THEN 1 ELSE 0 END) AS manual_runs,
                   SUM(CASE WHEN STATUS='failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN STATUS='success' THEN ROWS_LOADED ELSE 0 END) AS rows_loaded
            FROM {RUN_TABLE} WHERE STARTED_AT >= :lo
            GROUP BY CAST(DATEADD(MINUTE, 330, STARTED_AT) AS DATE) ORDER BY run_day
        """), {"lo": week_lo}).mappings().fetchall()
    ints = lambda m: {k: int(v or 0) for k, v in dict(m).items()}
    return {
        "jobs": ints(jobs), "today": ints(today),
        "next_runs": [_row(r) for r in nxt],
        "recent_failures": [_row(r) for r in fails],
        "last_7_days": [{"day": str(r["run_day"]), "auto_runs": int(r["auto_runs"] or 0),
                         "manual_runs": int(r["manual_runs"] or 0), "failed": int(r["failed"] or 0),
                         "rows_loaded": int(r["rows_loaded"] or 0)} for r in days],
        "snowflake": gsf.status(),
        "sources": [
            {"key": "snowflake", "label": "Snowflake", "status": "active"},
            {"key": "excel", "label": "Excel", "status": "planned", "phase": 2},
            {"key": "datav2", "label": "DataV2", "status": "planned", "phase": 3},
            {"key": "sap", "label": "SAP", "status": "planned", "phase": 4},
        ],
    }
