"""
Report Scheduler Service
========================
Background daemon that fires report runs three ways:
  1. schedule — time-based (daily / weekly / every N hours)
  2. event    — a process completed (listing approve, pend-alc approve, MSA calc)
  3. manual   — run_now() from the API

Mirrors the tempdb_cleaner singleton/thread pattern in tempdb_cleanup_service.py.

Concurrency model:
  * One daemon thread ticks every `interval` seconds and claims DUE schedule
    reports with an ATOMIC `UPDATE ... WHERE NEXT_RUN_AT <= now` — so even with
    4 uvicorn workers (4 threads) only one wins each schedule per tick.
  * A ThreadPoolExecutor(max_parallel) runs the claimed reports. Excess runs
    queue for a free slot (FIFO) — this throttles heavy stored procs.
  * A report already running is SKIPPED (logged as a 'skipped' run) rather than
    stacked, so a slow proc never piles up.

Event firing is background: emit_event() submits matching reports to the same
pool and returns immediately, so approving a listing is never blocked.

Times are treated as UTC (compared against SQL Server SYSUTCDATETIME()).
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.core.config import get_settings
from app.database.session import get_data_engine
from app.services.report_engine import (
    REPORTS_TABLE, RUNS_TABLE, run_report, make_session_code,
)

settings = get_settings()

EVENT_LISTING_APPROVED = "listing.approved"
EVENT_PENDALC_APPROVED = "pendalc.approved"
EVENT_MSA_COMPLETED = "msa.completed"
EVENT_AUTOCONT_COMPLETED = "autocont.completed"
KNOWN_EVENTS = {EVENT_LISTING_APPROVED, EVENT_PENDALC_APPROVED,
                EVENT_MSA_COMPLETED, EVENT_AUTOCONT_COMPLETED}


# ── Self-healing DDL ────────────────────────────────────────────────────────
def ensure_report_tables() -> None:
    """Create ARS_REPORTS + ARS_REPORT_RUNS if missing. Idempotent."""
    engine = get_data_engine()
    with engine.begin() as conn:
        conn.execute(text(f"""
            IF OBJECT_ID('dbo.{REPORTS_TABLE}','U') IS NULL
            CREATE TABLE dbo.{REPORTS_TABLE} (
                REPORT_ID        INT IDENTITY(1,1) PRIMARY KEY,
                NAME             NVARCHAR(200)  NOT NULL,
                DESCRIPTION      NVARCHAR(500)  NULL,
                STEPS            NVARCHAR(MAX)  NOT NULL,
                OUTPUT_TYPE      NVARCHAR(20)   NOT NULL DEFAULT 'folder',
                BASE_DIR         NVARCHAR(400)  NULL,
                FILE_FORMAT      NVARCHAR(10)   NOT NULL DEFAULT 'csv',
                SNOWFLAKE_CONFIG NVARCHAR(MAX)  NULL,
                SPLIT_CONFIG     NVARCHAR(MAX)  NULL,
                EMAIL_CONFIG     NVARCHAR(MAX)  NULL,
                WHATSAPP_CONFIG  NVARCHAR(MAX)  NULL,
                SMS_CONFIG       NVARCHAR(MAX)  NULL,
                FOLDER_PER_RUN   BIT            NOT NULL DEFAULT 1,
                TRIGGER_TYPE     NVARCHAR(20)   NOT NULL DEFAULT 'manual',
                SCHEDULE_CONFIG  NVARCHAR(MAX)  NULL,
                TRIGGER_EVENT    NVARCHAR(50)   NULL,
                ENABLED          BIT            NOT NULL DEFAULT 1,
                NEXT_RUN_AT      DATETIME       NULL,
                LAST_RUN_AT      DATETIME       NULL,
                LAST_STATUS      NVARCHAR(20)   NULL,
                CREATED_BY       NVARCHAR(100)  NULL,
                CREATED_AT       DATETIME       NOT NULL DEFAULT SYSUTCDATETIME(),
                UPDATED_AT       DATETIME       NOT NULL DEFAULT SYSUTCDATETIME()
            )
        """))
        # Self-heal: add columns to tables created before they existed.
        conn.execute(text(f"""
            IF COL_LENGTH('dbo.{REPORTS_TABLE}','SPLIT_CONFIG') IS NULL
            ALTER TABLE dbo.{REPORTS_TABLE} ADD SPLIT_CONFIG NVARCHAR(MAX) NULL
        """))
        conn.execute(text(f"""
            IF COL_LENGTH('dbo.{REPORTS_TABLE}','EMAIL_CONFIG') IS NULL
            ALTER TABLE dbo.{REPORTS_TABLE} ADD EMAIL_CONFIG NVARCHAR(MAX) NULL
        """))
        conn.execute(text(f"""
            IF COL_LENGTH('dbo.{REPORTS_TABLE}','FOLDER_PER_RUN') IS NULL
            ALTER TABLE dbo.{REPORTS_TABLE} ADD FOLDER_PER_RUN BIT NOT NULL DEFAULT 1
        """))
        conn.execute(text(f"""
            IF COL_LENGTH('dbo.{REPORTS_TABLE}','WHATSAPP_CONFIG') IS NULL
            ALTER TABLE dbo.{REPORTS_TABLE} ADD WHATSAPP_CONFIG NVARCHAR(MAX) NULL
        """))
        conn.execute(text(f"""
            IF COL_LENGTH('dbo.{REPORTS_TABLE}','SMS_CONFIG') IS NULL
            ALTER TABLE dbo.{REPORTS_TABLE} ADD SMS_CONFIG NVARCHAR(MAX) NULL
        """))
        conn.execute(text(f"""
            IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_{REPORTS_TABLE}_due')
            CREATE INDEX IX_{REPORTS_TABLE}_due
                ON dbo.{REPORTS_TABLE} (TRIGGER_TYPE, ENABLED, NEXT_RUN_AT)
        """))
        conn.execute(text(f"""
            IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_{REPORTS_TABLE}_event')
            CREATE INDEX IX_{REPORTS_TABLE}_event
                ON dbo.{REPORTS_TABLE} (TRIGGER_EVENT, ENABLED)
        """))
        conn.execute(text(f"""
            IF OBJECT_ID('dbo.{RUNS_TABLE}','U') IS NULL
            CREATE TABLE dbo.{RUNS_TABLE} (
                RUN_ID         BIGINT IDENTITY(1,1) PRIMARY KEY,
                REPORT_ID      INT            NOT NULL,
                SESSION_CODE   NVARCHAR(80)   NOT NULL,
                TRIGGER_SOURCE NVARCHAR(40)   NOT NULL,
                STATUS         NVARCHAR(20)   NOT NULL DEFAULT 'pending',
                EXPORT_DIR     NVARCHAR(500)  NULL,
                FILES          NVARCHAR(MAX)  NULL,
                ERRORS         NVARCHAR(MAX)  NULL,
                ROW_COUNT      INT            NULL,
                STARTED_AT     DATETIME       NULL,
                COMPLETED_AT   DATETIME       NULL,
                DURATION_MS    INT            NULL,
                CREATED_AT     DATETIME       NOT NULL DEFAULT SYSUTCDATETIME(),
                CREATED_BY     NVARCHAR(100)  NULL
            )
        """))
        conn.execute(text(f"""
            IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_{RUNS_TABLE}_report')
            CREATE INDEX IX_{RUNS_TABLE}_report
                ON dbo.{RUNS_TABLE} (REPORT_ID, CREATED_AT DESC)
        """))


def _parse_times(cfg: Dict[str, Any]) -> List[tuple]:
    """Return a list of (hour, minute) — supports multiple times per day.

    Accepts cfg['times'] = ['07:00','15:00'] or a single cfg['time'].
    """
    raw = cfg.get("times")
    if not raw:
        raw = [cfg.get("time", "07:00")]
    out = []
    for t in raw:
        try:
            hh, mm = (int(x) for x in str(t).split(":")[:2])
            out.append((hh % 24, mm % 60))
        except Exception:
            continue
    return out or [(7, 0)]


def _month_day(year: int, month0: int, day: int) -> datetime:
    """Date for `day` of the month `month0` months after (year, month0 base),
    clamping day to the month length (e.g. 31 → 30/28)."""
    import calendar
    total = month0
    y = year + total // 12
    m = total % 12 + 1
    d = min(max(1, day), calendar.monthrange(y, m)[1])
    return datetime(y, m, d)


def reconcile_orphaned_runs() -> int:
    """Mark any run still 'running' as failed — it was interrupted by a restart.

    A run is only ever 'running' while its process is executing it; if the
    server restarts mid-run, that row is orphaned and would otherwise show
    'running' forever. Called once at scheduler startup. Returns the count.
    """
    engine = get_data_engine()
    with engine.begin() as conn:
        res = conn.execute(text(f"""
            UPDATE {RUNS_TABLE}
            SET STATUS='failed',
                ERRORS='[{{"step":"server","error":"run interrupted — server restarted"}}]',
                COMPLETED_AT=SYSUTCDATETIME()
            WHERE STATUS='running'
        """))
        # A report whose LAST_STATUS got stuck at 'running' is stale too.
        conn.execute(text(f"""
            UPDATE {REPORTS_TABLE} SET LAST_STATUS='failed'
            WHERE LAST_STATUS='running'
        """))
    n = res.rowcount or 0
    if n:
        logger.warning(f"[report-sched] reconciled {n} orphaned 'running' run(s) → failed")
    return n


def compute_next_run(cfg: Dict[str, Any],
                     now: Optional[datetime] = None) -> Optional[datetime]:
    """Next fire time (UTC) from a schedule config dict.

    freq: once | daily | weekly | monthly | every_n_hours.
      once    -> cfg['datetime'] (ISO); returns None once it's in the past
                 (so a one-time report never repeats).
      daily   -> cfg['times'] (one or many HH:MM per day).
      weekly  -> cfg['weekday'] (0=Mon) + cfg['times'].
      monthly -> cfg['day'] (1-31, clamped) + cfg['times'].
      every_n_hours -> cfg['every_n_hours'].
    Returns the earliest upcoming time strictly after `now`, or None.
    """
    now = now or datetime.utcnow()
    freq = str(cfg.get("freq", "daily")).lower()

    if freq == "once":
        dt_str = cfg.get("datetime")
        if not dt_str:
            return None
        try:
            dt = datetime.fromisoformat(str(dt_str))
        except Exception:
            return None
        return dt if dt > now else None

    if freq in ("hourly", "every_n_hours") or cfg.get("every_n_hours"):
        n = max(1, int(cfg.get("every_n_hours", 1)))
        start, end = cfg.get("start"), cfg.get("end")
        if not start or not end:
            # No window → fire continuously every N hours.
            return now + timedelta(hours=n)
        # Windowed: only between start..end each day, stepping N hours from start.
        def _hm(s, dflt):
            try:
                hh, mm = (int(x) for x in str(s).split(":")[:2])
                return hh % 24, mm % 60
            except Exception:
                return dflt
        sh, sm = _hm(start, (0, 0))
        eh, em = _hm(end, (23, 59))
        win: List[datetime] = []
        for d in (0, 1):
            day = now + timedelta(days=d)
            t = day.replace(hour=sh, minute=sm, second=0, microsecond=0)
            end_dt = day.replace(hour=eh, minute=em, second=0, microsecond=0)
            guard = 0
            while t <= end_dt and guard < 1000:
                win.append(t)
                t += timedelta(hours=n)
                guard += 1
        upcoming = sorted(c for c in win if c > now)
        return upcoming[0] if upcoming else now + timedelta(hours=n)

    times = _parse_times(cfg)
    candidates: List[datetime] = []

    if freq == "weekly":
        # Support multiple weekdays: cfg['weekdays']=[0,1,2,3,4] (0=Mon). Falls
        # back to a single cfg['weekday'] for older configs.
        wds = cfg.get("weekdays")
        if not wds:
            wds = [int(cfg.get("weekday", 0))]
        wds = sorted({int(w) % 7 for w in wds})
        for wk in (0, 1):
            for wd in wds:
                base = now + timedelta(days=(wd - now.weekday()) % 7 + wk * 7)
                for hh, mm in times:
                    candidates.append(base.replace(hour=hh, minute=mm,
                                                   second=0, microsecond=0))
    elif freq == "monthly":
        # Support multiple dates: cfg['days']=[1,15]. Falls back to cfg['day'].
        days = cfg.get("days")
        if not days:
            days = [int(cfg.get("day", 1))]
        days = sorted({int(d) for d in days})
        month0 = now.month - 1
        for mo in (0, 1, 2):
            for day in days:
                base = _month_day(now.year, month0 + mo, day)
                for hh, mm in times:
                    candidates.append(base.replace(hour=hh, minute=mm,
                                                   second=0, microsecond=0))
    else:  # daily (default)
        for d in (0, 1):
            base = now + timedelta(days=d)
            for hh, mm in times:
                candidates.append(base.replace(hour=hh, minute=mm,
                                               second=0, microsecond=0))

    future = sorted(c for c in candidates if c > now)
    return future[0] if future else now + timedelta(days=1)


class ReportSchedulerService:
    def __init__(self, interval_seconds: int = 30, max_parallel: int = 2) -> None:
        self._interval = interval_seconds
        self._max_parallel = max_parallel
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._active: set = set()          # report_ids currently running
        self._queued: int = 0              # submitted, waiting for a worker slot
        self._active_lock = threading.Lock()
        self._last_tick: Optional[datetime] = None

    # ── Lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            try:
                ensure_report_tables()
                reconcile_orphaned_runs()
            except Exception as e:
                logger.warning(f"[report-sched] ensure tables failed: {e}")
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_parallel, thread_name_prefix="ReportRun")
            self._running = True
            self._thread = threading.Thread(
                target=self._loop, name="ReportScheduler", daemon=True)
            self._thread.start()
        logger.info(f"[report-sched] started — interval={self._interval}s, "
                    f"max_parallel={self._max_parallel}")

    def stop(self) -> None:
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10.0)
        if self._executor:
            self._executor.shutdown(wait=False)
        logger.info("[report-sched] stopped")

    # ── Public triggers ──────────────────────────────────────────────────────
    def run_now(self, report_id: int, user: Optional[str] = None) -> Dict[str, Any]:
        """Queue a manual run. Returns immediately; status shows in run history."""
        report = self._load_report(report_id)
        if not report:
            return {"queued": False, "error": "report not found"}
        session_code = make_session_code()
        self._submit(report, "manual", session_code, user)
        return {"queued": True, "session_code": session_code}

    def emit_event(self, event_name: str, session_id: Optional[str] = None,
                   user: Optional[str] = None) -> int:
        """Fire all enabled reports subscribed to this event. Background.

        Returns the number of reports queued. Never raises (safe to call from
        an approve endpoint's hot path).
        """
        try:
            reports = self._load_event_reports(event_name)
        except Exception as e:
            logger.warning(f"[report-sched] emit_event({event_name}) lookup failed: {e}")
            return 0
        queued = 0
        for report in reports:
            # Use the triggering session id as the folder name so the report is
            # tied to the exact process run that fired it.
            session_code = session_id or make_session_code()
            self._submit(report, f"event:{event_name}", session_code, user)
            queued += 1
        if queued:
            logger.info(f"[report-sched] event {event_name} queued {queued} report(s)")
        return queued

    @property
    def status(self) -> Dict[str, Any]:
        with self._active_lock:
            active = list(self._active)
            queued = self._queued
        return {
            "running": self._running,
            "interval_seconds": self._interval,
            "max_parallel": self._max_parallel,
            "active_report_ids": active,
            "running_count": len(active),
            "queued_count": queued,
            "last_tick": self._last_tick.isoformat() if self._last_tick else None,
        }

    # ── Internals ─────────────────────────────────────────────────────────────
    def _loop(self) -> None:
        self._sleep_interruptible(self._interval)  # let startup settle
        while self._running:
            try:
                self._tick()
            except Exception as e:
                logger.warning(f"[report-sched] tick error: {e}")
            self._sleep_interruptible(self._interval)

    def _sleep_interruptible(self, seconds: int) -> None:
        elapsed = 0
        while self._running and elapsed < seconds:
            time.sleep(min(5, seconds - elapsed))
            elapsed += 5

    def _tick(self) -> None:
        self._last_tick = datetime.utcnow()
        for report in self._claim_due_schedules():
            self._submit(report, "schedule", make_session_code(), report.get("CREATED_BY"))

    def _claim_due_schedules(self) -> List[Dict[str, Any]]:
        """Atomically claim due schedule reports and advance their NEXT_RUN_AT.

        Claiming = an UPDATE guarded on NEXT_RUN_AT <= now, so concurrent
        workers can't double-claim. Returns the reports this worker won.
        """
        engine = get_data_engine()
        claimed: List[Dict[str, Any]] = []
        import json
        with engine.connect() as conn:
            candidates = conn.execute(text(f"""
                SELECT REPORT_ID, SCHEDULE_CONFIG FROM {REPORTS_TABLE}
                WHERE TRIGGER_TYPE='schedule' AND ENABLED=1
                  AND NEXT_RUN_AT IS NOT NULL
                  AND NEXT_RUN_AT <= SYSUTCDATETIME()
            """)).fetchall()

        for rid, sched_json in candidates:
            try:
                cfg = json.loads(sched_json or "{}")
            except Exception:
                cfg = {}
            next_run = compute_next_run(cfg)
            with engine.begin() as conn:
                res = conn.execute(text(f"""
                    UPDATE {REPORTS_TABLE}
                    SET NEXT_RUN_AT = :nr, UPDATED_AT = SYSUTCDATETIME()
                    WHERE REPORT_ID = :rid
                      AND TRIGGER_TYPE='schedule' AND ENABLED=1
                      AND NEXT_RUN_AT <= SYSUTCDATETIME()
                """), {"nr": next_run, "rid": rid})
            if res.rowcount == 1:
                report = self._load_report(rid)
                if report:
                    claimed.append(report)
        return claimed

    def _submit(self, report: Dict[str, Any], trigger_source: str,
                session_code: str, user: Optional[str]) -> None:
        if not self._executor:
            # Scheduler not started (e.g. unit test) — run inline.
            self._run_guarded(report, trigger_source, session_code, user)
            return
        with self._active_lock:
            self._queued += 1
        self._executor.submit(
            self._run_guarded, report, trigger_source, session_code, user)

    def _run_guarded(self, report: Dict[str, Any], trigger_source: str,
                     session_code: str, user: Optional[str]) -> None:
        rid = int(report["REPORT_ID"])
        with self._active_lock:
            if self._queued > 0:
                self._queued -= 1   # left the queue, a worker picked it up
            if rid in self._active:
                self._record_skip(rid, session_code, trigger_source, user)
                logger.info(f"[report-sched] report {rid} already running — skipped")
                return
            self._active.add(rid)
        try:
            run_report(report, trigger_source, session_code, user)
        except Exception as e:
            logger.error(f"[report-sched] report {rid} crashed: {e}")
        finally:
            with self._active_lock:
                self._active.discard(rid)

    def _record_skip(self, report_id: int, session_code: str,
                     trigger_source: str, user: Optional[str]) -> None:
        try:
            engine = get_data_engine()
            with engine.begin() as conn:
                conn.execute(text(f"""
                    INSERT INTO {RUNS_TABLE}
                        (REPORT_ID, SESSION_CODE, TRIGGER_SOURCE, STATUS,
                         ERRORS, STARTED_AT, COMPLETED_AT, CREATED_BY)
                    VALUES (:rid, :sc, :ts, 'skipped',
                            :err, SYSUTCDATETIME(), SYSUTCDATETIME(), :cb)
                """), {"rid": report_id, "sc": session_code, "ts": trigger_source,
                       "err": '[{"error": "previous run still active"}]', "cb": user})
        except Exception as e:
            logger.warning(f"[report-sched] record_skip failed: {e}")

    def _load_report(self, report_id: int) -> Optional[Dict[str, Any]]:
        engine = get_data_engine()
        with engine.connect() as conn:
            row = conn.execute(text(f"""
                SELECT REPORT_ID, NAME, STEPS, OUTPUT_TYPE, BASE_DIR,
                       FILE_FORMAT, SNOWFLAKE_CONFIG, SPLIT_CONFIG, EMAIL_CONFIG,
                       WHATSAPP_CONFIG, SMS_CONFIG, FOLDER_PER_RUN, CREATED_BY
                FROM {REPORTS_TABLE} WHERE REPORT_ID = :rid
            """), {"rid": report_id}).mappings().fetchone()
        return dict(row) if row else None

    def _load_event_reports(self, event_name: str) -> List[Dict[str, Any]]:
        engine = get_data_engine()
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT REPORT_ID, NAME, STEPS, OUTPUT_TYPE, BASE_DIR,
                       FILE_FORMAT, SNOWFLAKE_CONFIG, SPLIT_CONFIG, EMAIL_CONFIG,
                       WHATSAPP_CONFIG, SMS_CONFIG, FOLDER_PER_RUN, CREATED_BY
                FROM {REPORTS_TABLE}
                WHERE TRIGGER_TYPE='event' AND ENABLED=1 AND TRIGGER_EVENT=:ev
            """), {"ev": event_name}).mappings().fetchall()
        return [dict(r) for r in rows]


# ── Module-level singleton + convenience wrapper ────────────────────────────
report_scheduler = ReportSchedulerService(
    interval_seconds=getattr(settings, "REPORT_SCHED_INTERVAL_SEC", 30),
    max_parallel=getattr(settings, "REPORT_MAX_PARALLEL", 2),
)


def emit_event(event_name: str, session_id: Optional[str] = None,
               user: Optional[str] = None) -> int:
    """Fire report event from anywhere (e.g. an approve endpoint). Safe/no-raise."""
    return report_scheduler.emit_event(event_name, session_id, user)
