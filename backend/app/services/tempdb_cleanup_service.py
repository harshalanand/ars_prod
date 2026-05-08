"""
TempDB Cleanup Service
======================
Background daemon thread that periodically drops orphaned ARS global (##) temp
tables from SQL Server tempdb, shrinks tempdb data files, and (when size
exceeds a configured threshold) runs an aggressive reclaim: cache flush +
hard SHRINKFILE to a target size.

Mirrors the AuditQueue threading pattern in audit_service.py.

Usage (called automatically from main.py lifespan):
    from app.services.tempdb_cleanup_service import tempdb_cleaner
    tempdb_cleaner.start()   # startup
    tempdb_cleaner.stop()    # shutdown
"""
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

from loguru import logger

from app.core.config import get_settings
from app.database.session import get_data_engine

settings = get_settings()

# ── Queries ───────────────────────────────────────────────────────────────────

_FIND_ORPHANS_SQL = """
    SELECT name, create_date
    FROM   tempdb.sys.tables
    WHERE (
           name LIKE '##upsert_temp[_]%'
        OR name LIKE '##merge_output[_]%'
        OR name LIKE '##bulk_stage[_]%'
        OR name LIKE '##do_update[_]%'
        OR name LIKE '##do_qty_tmp[_]%'
        OR name LIKE '#do_update[_]%'
        OR name LIKE '#do_qty_tmp[_]%'
        OR name LIKE '#bulk_stage[_]%'
        OR name LIKE '#upsert_temp[_]%'
        OR name LIKE '#merge_output[_]%'
        OR name LIKE '#temp[_]%'
        OR name IN ('##do_update', '##do_qty_tmp')
    )
    AND DATEDIFF(MINUTE, create_date, GETDATE()) >= ?
    ORDER BY create_date;
"""

_TEMPDB_FILES_SQL = """
    SELECT name FROM tempdb.sys.database_files WHERE type_desc = 'ROWS';
"""

_TEMPDB_SIZE_SQL = """
    SELECT
        SUM(size * 8.0 / 1024)                                  AS allocated_mb,
        SUM(FILEPROPERTY(name, 'SpaceUsed') * 8.0 / 1024)       AS used_mb
    FROM tempdb.sys.database_files
    WHERE type_desc = 'ROWS';
"""

_TOP_SESSIONS_SQL = """
    SELECT TOP 10
        s.session_id,
        ISNULL(r.status, 'sleeping')                              AS status,
        ISNULL(s.login_name, '')                                  AS login_name,
        ISNULL(s.host_name, '')                                   AS host_name,
        ISNULL(s.program_name, '')                                AS program_name,
        (ssu.user_objects_alloc_page_count
           + ssu.internal_objects_alloc_page_count) * 8 / 1024    AS mb_used,
        ssu.user_objects_alloc_page_count  * 8 / 1024             AS user_mb,
        ssu.internal_objects_alloc_page_count * 8 / 1024          AS internal_mb,
        s.last_request_start_time                                 AS last_request_start
    FROM sys.dm_db_session_space_usage ssu
    INNER JOIN sys.dm_exec_sessions s ON ssu.session_id = s.session_id
    LEFT  JOIN sys.dm_exec_requests  r ON s.session_id  = r.session_id
    WHERE ssu.session_id > 50
      AND (ssu.user_objects_alloc_page_count + ssu.internal_objects_alloc_page_count) > 0
    ORDER BY mb_used DESC;
"""


class TempDBCleanupService:
    """
    Daemon thread that wakes every `interval_minutes` and:
      1. Drops orphaned ARS ## global temp tables older than `orphan_age_minutes`.
      2. Runs DBCC SHRINKFILE TRUNCATEONLY on every tempdb data file.
      3. If total size exceeds `aggressive_threshold_mb`, runs aggressive reclaim
         (FREEPROCCACHE + FREESYSTEMCACHE + SHRINKFILE to `aggressive_target_mb`).
      4. If total size exceeds `alert_threshold_mb`, raises an ALERT (ERROR log
         + stored in `self._last_alert` so the UI can surface it).
      5. Keeps a short in-memory history of recent runs for the UI trend chart.

    Thread-safe. Exposes run_now() and aggressive_shrink_now() for API endpoints.
    """

    def __init__(
        self,
        interval_minutes: int = 5,
        orphan_age_minutes: int = 10,
        shrink_after_cleanup: bool = True,
        aggressive_threshold_mb: int = 20480,
        alert_threshold_mb: int = 40960,
        aggressive_target_mb: int = 4096,
        history_size: int = 96,
    ) -> None:
        self._interval = interval_minutes * 60   # stored as seconds
        self._orphan_age = orphan_age_minutes
        self._shrink = shrink_after_cleanup
        self._aggressive_threshold_mb = aggressive_threshold_mb
        self._alert_threshold_mb = alert_threshold_mb
        self._aggressive_target_mb = aggressive_target_mb

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_run: Optional[datetime] = None
        self._last_stats: Dict[str, Any] = {}
        self._last_alert: Optional[Dict[str, Any]] = None
        self._history: Deque[Dict[str, Any]] = deque(maxlen=history_size)
        # Azure SQL DB detection — sticky once detected so we don't re-probe
        # every cycle. None = unknown / not yet probed.
        self._is_azure_sql_db: Optional[bool] = None
        self._engine_edition: Optional[int] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background cleanup thread. Safe to call multiple times."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._loop, name="TempDBCleanup", daemon=True
            )
            self._thread.start()
        logger.info(
            f"TempDB cleanup service started — "
            f"interval={self._interval // 60} min, "
            f"orphan_age={self._orphan_age} min, "
            f"shrink={self._shrink}, "
            f"aggressive_threshold={self._aggressive_threshold_mb} MB, "
            f"alert_threshold={self._alert_threshold_mb} MB"
        )

    def stop(self) -> None:
        """Signal the thread to stop and wait up to 10 s for it to finish."""
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10.0)
        logger.info("TempDB cleanup service stopped")

    def run_now(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Blocking manual trigger — standard cleanup cycle.
        Returns the stats dict from _do_cleanup().
        """
        return self._do_cleanup(dry_run=dry_run)

    def aggressive_shrink_now(self) -> Dict[str, Any]:
        """
        Blocking manual trigger — force an aggressive shrink regardless of size.
        Drops orphans, flushes caches, hard-shrinks every data file to the
        configured target size.
        """
        return self._do_cleanup(dry_run=False, force_aggressive=True)

    def top_sessions(self) -> List[Dict[str, Any]]:
        """Return the top tempdb-consuming sessions for diagnostics.
        Returns [] on Azure SQL DB — sys.dm_db_session_space_usage is per-DB
        on Azure and the cross-tempdb view does not exist."""
        if self._detect_azure_sql_db():
            return []
        engine = get_data_engine()
        raw_conn = engine.raw_connection()
        try:
            cursor = raw_conn.cursor()
            cursor.execute(_TOP_SESSIONS_SQL)
            cols = [c[0] for c in cursor.description]
            rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
            for r in rows:
                if r.get("last_request_start"):
                    r["last_request_start"] = r["last_request_start"].isoformat()
            return rows
        finally:
            raw_conn.close()

    def clear_alert(self) -> None:
        """Dismiss the current alert (called from the UI)."""
        self._last_alert = None

    @property
    def status(self) -> Dict[str, Any]:
        """Service state snapshot for the /maintenance/tempdb/status endpoint."""
        return {
            "running": self._running,
            "interval_minutes": self._interval // 60,
            "orphan_age_minutes": self._orphan_age,
            "shrink_enabled": self._shrink,
            "aggressive_threshold_mb": self._aggressive_threshold_mb,
            "alert_threshold_mb": self._alert_threshold_mb,
            "aggressive_target_mb": self._aggressive_target_mb,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "last_stats": self._last_stats,
            "last_alert": self._last_alert,
            "history_points": len(self._history),
            "last_post_job": (
                self._last_post_job.isoformat()
                if getattr(self, "_last_post_job", None) else None
            ),
            "last_post_job_stats": getattr(self, "_last_post_job_stats", None),
        }

    @property
    def history(self) -> List[Dict[str, Any]]:
        """Recent runs (for the UI trend chart)."""
        return list(self._history)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        """Thread body: wait one interval, then clean on each tick."""
        # Initial delay — let the app finish starting up before first run
        self._sleep_interruptible(self._interval)

        while self._running:
            try:
                self._do_cleanup()
            except Exception as exc:
                logger.warning(f"TempDB cleanup cycle error: {exc}")
            self._sleep_interruptible(self._interval)

    def _sleep_interruptible(self, seconds: int) -> None:
        """Sleep in 5-second slices so stop() is responsive."""
        elapsed = 0
        while self._running and elapsed < seconds:
            time.sleep(min(5, seconds - elapsed))
            elapsed += 5

    def _read_size(self, cursor) -> Optional[float]:
        """Fetch current tempdb allocated MB, or None on failure."""
        try:
            cursor.execute(_TEMPDB_SIZE_SQL)
            row = cursor.fetchone()
            if row and row[0] is not None:
                return round(float(row[0]), 2)
        except Exception as exc:
            logger.debug(f"TempDB size read failed: {exc}")
        return None

    def _detect_azure_sql_db(self) -> bool:
        """One-shot probe of SERVERPROPERTY('EngineEdition'). Cached.

        Engine edition values:
          1 = Personal/Desktop, 2 = Standard, 3 = Enterprise, 4 = Express,
          5 = Azure SQL Database (single DB / elastic pool — NO USE statement,
              NO cross-DB queries, NO tempdb shrink — Azure manages it)
          6 = Azure SQL Data Warehouse (Synapse)
          8 = Azure SQL Managed Instance (supports USE, has tempdb)
          9 = Azure SQL Edge / 11 = Fabric SQL DB

        Only edition 5 (and 6) need to be skipped — local SQL Server,
        Express, and Managed Instance all support the cleanup operations.
        """
        if self._is_azure_sql_db is not None:
            return self._is_azure_sql_db
        try:
            engine = get_data_engine()
            with engine.connect() as conn:
                from sqlalchemy import text as _text
                edition = conn.execute(
                    _text("SELECT CAST(SERVERPROPERTY('EngineEdition') AS INT)")
                ).scalar()
                self._engine_edition = int(edition) if edition is not None else None
                self._is_azure_sql_db = self._engine_edition in (5, 6)
                if self._is_azure_sql_db:
                    logger.info(
                        f"TempDB cleanup: detected Azure SQL Database "
                        f"(EngineEdition={self._engine_edition}). "
                        f"Skipping cleanup — Azure manages tempdb automatically "
                        f"and USE statement is not supported."
                    )
                else:
                    logger.info(
                        f"TempDB cleanup: SQL Server EngineEdition="
                        f"{self._engine_edition} (cleanup enabled)"
                    )
        except Exception as exc:
            logger.debug(f"EngineEdition probe failed: {exc}")
            # Don't cache on failure — try again next cycle
            return False
        return self._is_azure_sql_db

    def _do_cleanup(
        self,
        dry_run: bool = False,
        force_aggressive: bool = False,
    ) -> Dict[str, Any]:
        """
        Core logic:
          1. Snapshot tempdb size.
          2. Find + drop orphaned ARS ## tables older than orphan_age_minutes.
          3. SHRINKFILE TRUNCATEONLY on every tempdb data file.
          4. If size > aggressive threshold (or force_aggressive): flush caches
             and hard-shrink every data file to aggressive_target_mb.
          5. Snapshot tempdb size again and record history + optional alert.

        On Azure SQL Database (EngineEdition=5/6) the entire body is skipped:
        Azure manages tempdb automatically, and `USE`, `DBCC SHRINKFILE`, and
        cross-database `tempdb.sys.tables` queries are all unsupported.
        """
        # Short-circuit on Azure SQL Database — nothing here works there.
        if self._detect_azure_sql_db():
            stats = {
                "run_at":      datetime.utcnow().isoformat(),
                "skipped":     True,
                "reason":      "Azure SQL Database — tempdb is managed by Azure",
                "edition":     self._engine_edition,
                "mb_before":   None,
                "mb_after":    None,
                "mb_freed":    0.0,
                "mode":        "skipped_azure",
                "dropped":     [],
                "shrunk":      [],
                "errors":      [],
            }
            self._last_run = datetime.utcnow()
            self._last_stats = stats
            self._history.append(stats)
            return stats

        dropped: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        shrunk:  List[str] = []
        errors:  List[Dict[str, Any]] = []
        mb_before: Optional[float] = None
        mb_after:  Optional[float] = None
        mode = "light"   # light | aggressive | dry_run

        engine = get_data_engine()
        raw_conn = engine.raw_connection()
        try:
            cursor = raw_conn.cursor()

            # 1. Size before
            mb_before = self._read_size(cursor)

            # 2. Find orphans
            cursor.execute(_FIND_ORPHANS_SQL, self._orphan_age)
            orphans = cursor.fetchall()   # list of (name, create_date)

            for tbl_name, create_date in orphans:
                age_min = 0
                if create_date:
                    age_min = int(
                        (datetime.utcnow() - create_date).total_seconds() / 60
                    )

                if dry_run:
                    skipped.append({"table": tbl_name, "age_minutes": age_min, "reason": "dry_run"})
                    continue

                drop_sql = (
                    f"IF OBJECT_ID('tempdb..[{tbl_name}]') IS NOT NULL "
                    f"DROP TABLE [{tbl_name}]"
                )
                try:
                    cursor.execute(drop_sql)
                    raw_conn.commit()
                    dropped.append({"table": tbl_name, "age_minutes": age_min})
                    logger.info(f"TempDB cleanup: dropped [{tbl_name}] (age {age_min} min)")
                except Exception as exc:
                    errors.append({"table": tbl_name, "error": str(exc)})
                    logger.warning(f"TempDB cleanup: failed to drop [{tbl_name}]: {exc}")

            # Decide shrink mode
            size_for_decision = mb_before if mb_before is not None else 0.0
            aggressive = force_aggressive or (
                size_for_decision >= self._aggressive_threshold_mb
            )
            if dry_run:
                mode = "dry_run"
            else:
                mode = "aggressive" if aggressive else "light"

            # 3 + 4. Shrink
            # DBCC SHRINKFILE / FREEPROCCACHE require:
            #   a) autocommit mode (cannot run inside a user transaction)
            #   b) tempdb as current database context for SHRINKFILE
            # So we use a separate pyodbc connection with autocommit + USE tempdb.
            if self._shrink and not dry_run:
                shrink_fairy = None
                try:
                    cursor.execute(_TEMPDB_FILES_SQL)
                    file_names = [r[0] for r in cursor.fetchall()]

                    shrink_fairy = engine.raw_connection()
                    pyodbc_conn = shrink_fairy.driver_connection
                    pyodbc_conn.autocommit = True
                    shrink_cursor = pyodbc_conn.cursor()

                    if aggressive:
                        # Flush caches so SHRINKFILE has room to reclaim.
                        # These run in the master context; SHRINKFILE needs tempdb.
                        for stmt in (
                            "DBCC FREEPROCCACHE WITH NO_INFOMSGS",
                            "DBCC FREESYSTEMCACHE ('ALL') WITH NO_INFOMSGS",
                            "DBCC FREESESSIONCACHE WITH NO_INFOMSGS",
                        ):
                            try:
                                shrink_cursor.execute(stmt)
                            except Exception as exc:
                                errors.append({"step": stmt, "error": str(exc)})
                                logger.warning(f"TempDB aggressive cache flush failed [{stmt}]: {exc}")

                    shrink_cursor.execute("USE tempdb")
                    for fname in file_names:
                        try:
                            if aggressive:
                                stmt = (
                                    f"DBCC SHRINKFILE ([{fname}], "
                                    f"{int(self._aggressive_target_mb)}) WITH NO_INFOMSGS"
                                )
                            else:
                                stmt = (
                                    f"DBCC SHRINKFILE ([{fname}], TRUNCATEONLY) "
                                    f"WITH NO_INFOMSGS"
                                )
                            shrink_cursor.execute(stmt)
                            shrunk.append(fname)
                        except Exception as exc:
                            errors.append({"file": fname, "error": str(exc)})
                            logger.warning(f"TempDB shrink [{fname}] failed: {exc}")

                    shrink_cursor.close()
                except Exception as exc:
                    logger.warning(f"TempDB shrink file enumeration failed: {exc}")
                finally:
                    # CRITICAL: invalidate this connection so it is NOT returned
                    # to the pool with "USE tempdb" context — that would poison
                    # other queries into looking for tables in tempdb instead of Rep_data.
                    if shrink_fairy:
                        try:
                            shrink_fairy.invalidate()
                        except Exception:
                            pass

            # 5. Size after
            mb_after = self._read_size(cursor)

        finally:
            raw_conn.close()

        mb_freed = round(
            (mb_before or 0.0) - (mb_after if mb_after is not None else (mb_before or 0.0)),
            2,
        )

        stats: Dict[str, Any] = {
            "run_at":            datetime.utcnow().isoformat(),
            "mode":              mode,
            "dry_run":           dry_run,
            "dropped_count":     len(dropped),
            "dropped":           dropped,
            "skipped":           skipped,
            "shrunk_files":      shrunk,
            "errors":            errors,
            "tempdb_mb_before":  mb_before,
            "tempdb_mb_after":   mb_after,
            "mb_freed":          mb_freed,
        }

        self._last_run = datetime.utcnow()
        self._last_stats = stats

        # Append a compact point to history (enough for the trend chart).
        self._history.append({
            "ts":       stats["run_at"],
            "mode":     mode,
            "mb_before": mb_before,
            "mb_after":  mb_after,
            "mb_freed":  mb_freed,
            "dropped":   len(dropped),
        })

        # Alert: size still above the alert threshold after cleanup.
        current = mb_after if mb_after is not None else mb_before
        if current is not None and current >= self._alert_threshold_mb:
            self._last_alert = {
                "raised_at": stats["run_at"],
                "mb_current": current,
                "threshold_mb": self._alert_threshold_mb,
                "message": (
                    f"TempDB is {current:.0f} MB — above alert threshold "
                    f"{self._alert_threshold_mb} MB. "
                    f"Last cleanup mode={mode}, freed={mb_freed} MB."
                ),
            }
            logger.error(f"ALERT: {self._last_alert['message']}")
        elif self._last_alert and current is not None and current < self._alert_threshold_mb:
            # Auto-clear once we drop back below threshold
            self._last_alert = None

        if dropped or errors or mode == "aggressive":
            logger.info(
                f"TempDB cleanup done — mode={mode}, dropped={len(dropped)}, "
                f"errors={len(errors)}, freed={mb_freed} MB, "
                f"size {mb_before} → {mb_after} MB"
            )

        return stats

    # ── Post-job cleanup (called after every heavy endpoint completes) ─────────

    def schedule_post_job_cleanup(self, reason: str = "") -> bool:
        """
        Fire-and-forget. Spawns a daemon thread that runs post_job_cleanup()
        and returns immediately so the HTTP response is never blocked.
        Returns False if a cleanup ran within the cooldown window.
        """
        cooldown = getattr(settings, "AUTO_FREE_COOLDOWN_SEC", 60)
        last = getattr(self, "_last_post_job", None)
        if last and (datetime.utcnow() - last).total_seconds() < cooldown:
            return False

        # Pre-claim the cooldown slot so concurrent calls collapse to one run
        self._last_post_job = datetime.utcnow()
        threading.Thread(
            target=self._run_post_job_safe,
            args=(reason,),
            name=f"PostJobCleanup-{reason[:30]}",
            daemon=True,
        ).start()
        return True

    def _run_post_job_safe(self, reason: str) -> None:
        try:
            self.post_job_cleanup(reason=reason)
        except Exception as exc:
            logger.warning(f"post_job_cleanup({reason}) failed: {exc}")

    def post_job_cleanup(self, reason: str = "", target_db: Optional[str] = None) -> Dict[str, Any]:
        """
        Run after a heavy job (allocation / listing / contrib / grid / bdc):
          1. CHECKPOINT on `target_db` — in SIMPLE recovery this also frees log.
          2. If log > AUTO_FREE_LOG_MAX_MB and recovery=SIMPLE,
             SHRINKFILE the log to AUTO_FREE_LOG_TARGET_MB.
          3. Drop orphaned ## global temp tables (no age threshold — they're
             from the job that just finished).
          4. Light SHRINKFILE TRUNCATEONLY on every tempdb data file.

        Cheap and idempotent. Designed to be called dozens of times per hour
        (with cooldown via schedule_post_job_cleanup).
        """
        if target_db is None:
            target_db = settings.DATA_DB_NAME
        ts = datetime.utcnow().isoformat()
        result: Dict[str, Any] = {
            "ts": ts, "reason": reason, "target_db": target_db,
            "checkpointed": False, "log_shrunk": False,
            "log_before_mb": None, "log_after_mb": None,
            "orphans_dropped": 0, "tempdb_files_shrunk": 0,
            "errors": [],
        }

        log_max    = getattr(settings, "AUTO_FREE_LOG_MAX_MB", 8192)
        log_target = getattr(settings, "AUTO_FREE_LOG_TARGET_MB", 4096)

        engine = get_data_engine()
        shrink_fairy = None
        try:
            shrink_fairy = engine.raw_connection()
            pyodbc_conn = shrink_fairy.driver_connection
            pyodbc_conn.autocommit = True
            cur = pyodbc_conn.cursor()

            # 1) CHECKPOINT on the target DB
            try:
                cur.execute(f"USE [{target_db}]")
                cur.execute("CHECKPOINT")
                result["checkpointed"] = True
            except Exception as exc:
                result["errors"].append({"step": "checkpoint", "error": str(exc)})

            # 2) Conditional log shrink (only if SIMPLE — won't waste effort on FULL+LOG_BACKUP wait)
            try:
                cur.execute(
                    "SELECT recovery_model_desc, log_reuse_wait_desc "
                    "FROM sys.databases WHERE name = ?", target_db,
                )
                row = cur.fetchone() or (None, None)
                recovery, wait = row[0], row[1]

                cur.execute("""
                    SELECT name, size * 8.0 / 1024
                    FROM sys.database_files WHERE type_desc = 'LOG'
                """)
                logs = cur.fetchall()
                if logs:
                    log_name, log_mb = logs[0][0], float(logs[0][1] or 0)
                    result["log_before_mb"] = log_mb
                    auto_resolve = getattr(settings, "AUTO_RESOLVE_LOG_BACKUP_WAIT", True)

                    # Auto-flip FULL→SIMPLE when log is large AND held by LOG_BACKUP.
                    # Eliminates the recurring "log fills the disk" failure mode
                    # without anyone having to schedule log backups.
                    if (auto_resolve and recovery != "SIMPLE"
                            and wait == "LOG_BACKUP" and log_mb > log_max):
                        try:
                            cur.execute(f"ALTER DATABASE [{target_db}] SET RECOVERY SIMPLE")
                            cur.execute(f"USE [{target_db}]")
                            cur.execute("CHECKPOINT")
                            logger.warning(
                                f"post_job_cleanup({reason}): {target_db} switched "
                                f"{recovery}→SIMPLE to clear LOG_BACKUP wait"
                            )
                            recovery = "SIMPLE"
                            result["auto_simpled"] = True
                        except Exception as exc:
                            result["errors"].append({
                                "step": "auto_simple", "error": str(exc),
                            })

                    should_shrink = (
                        recovery == "SIMPLE"
                        and log_mb > log_max
                    )
                    if should_shrink:
                        cur.execute(
                            f"DBCC SHRINKFILE (N'{log_name}', {int(log_target)}) WITH NO_INFOMSGS"
                        )
                        cur.execute(
                            "SELECT size * 8.0 / 1024 FROM sys.database_files WHERE name = ?",
                            log_name,
                        )
                        result["log_after_mb"] = float(cur.fetchone()[0] or 0)
                        result["log_shrunk"] = True
                        logger.info(
                            f"post_job_cleanup({reason}): {target_db} log "
                            f"{log_mb:.0f}→{result['log_after_mb']:.0f} MB"
                        )
                    elif recovery != "SIMPLE" and log_mb > log_max:
                        # Surface this once — admin needs to act (auto-resolve disabled)
                        result["errors"].append({
                            "step": "log_shrink_skipped",
                            "error": f"log is {log_mb:.0f} MB but recovery={recovery}, "
                                     f"reuse_wait={wait}. Switch to SIMPLE or back up the log.",
                        })
            except Exception as exc:
                result["errors"].append({"step": "log_shrink", "error": str(exc)})

            # 3) Orphan ## tables — no age filter, this is end-of-job cleanup
            try:
                cur.execute("USE tempdb")
                cur.execute("""
                    SELECT name FROM tempdb.sys.tables
                    WHERE name LIKE '##upsert_temp[_]%'
                       OR name LIKE '##merge_output[_]%'
                       OR name LIKE '##bulk_stage[_]%'
                       OR name LIKE '##do_update[_]%'
                       OR name LIKE '##do_qty_tmp[_]%'
                       OR name IN ('##do_update', '##do_qty_tmp')
                """)
                names = [r[0] for r in cur.fetchall()]
                for n in names:
                    try:
                        cur.execute(f"IF OBJECT_ID('tempdb..[{n}]') IS NOT NULL DROP TABLE [{n}]")
                        result["orphans_dropped"] += 1
                    except Exception as exc:
                        result["errors"].append({"step": f"drop_{n}", "error": str(exc)})
            except Exception as exc:
                result["errors"].append({"step": "orphan_drop", "error": str(exc)})

            # 4) Light tempdb SHRINKFILE TRUNCATEONLY (gives back free pages,
            #    no lock storm, fast, and idempotent)
            try:
                cur.execute(_TEMPDB_FILES_SQL)
                tempdb_files = [r[0] for r in cur.fetchall()]
                for fname in tempdb_files:
                    try:
                        cur.execute(
                            f"DBCC SHRINKFILE ([{fname}], TRUNCATEONLY) WITH NO_INFOMSGS"
                        )
                        result["tempdb_files_shrunk"] += 1
                    except Exception as exc:
                        result["errors"].append({"step": f"shrink_{fname}", "error": str(exc)})
            except Exception as exc:
                result["errors"].append({"step": "tempdb_shrink", "error": str(exc)})

        finally:
            if shrink_fairy:
                try:
                    shrink_fairy.invalidate()
                except Exception:
                    pass

        # Bookkeeping for the UI
        self._last_post_job = datetime.utcnow()
        self._last_post_job_stats = result
        # Reuse history deque so the trend chart shows post-job cleanups too
        self._history.append({
            "ts":        ts,
            "mode":      f"post_job:{reason[:24]}" if reason else "post_job",
            "mb_before": result.get("log_before_mb"),
            "mb_after":  result.get("log_after_mb") or result.get("log_before_mb"),
            "mb_freed":  ((result.get("log_before_mb") or 0)
                          - (result.get("log_after_mb") or result.get("log_before_mb") or 0)),
            "dropped":   result["orphans_dropped"],
        })

        return result


# ── Module-level singleton ────────────────────────────────────────────────────
tempdb_cleaner = TempDBCleanupService(
    interval_minutes        = settings.DB_TEMPDB_CLEANUP_INTERVAL_MINUTES,
    orphan_age_minutes      = settings.DB_TEMPDB_ORPHAN_AGE_MINUTES,
    shrink_after_cleanup    = True,
    aggressive_threshold_mb = settings.DB_TEMPDB_AGGRESSIVE_THRESHOLD_MB,
    alert_threshold_mb      = settings.DB_TEMPDB_ALERT_THRESHOLD_MB,
    aggressive_target_mb    = settings.DB_TEMPDB_AGGRESSIVE_TARGET_MB,
    history_size            = settings.DB_TEMPDB_HISTORY_SIZE,
)
