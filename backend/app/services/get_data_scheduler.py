"""
Get Data scheduler — fires scheduled sync jobs and runs manual "Run now" requests.

Same claim-and-run model as the SAP / report schedulers: every `interval`
seconds one thread
  1. marks runs whose heartbeat stopped as failed (crashed/restarted worker),
  2. atomically claims DUE jobs — NEXT_RUN_AT <= now (advanced to the next IST
     slot in the same UPDATE) or RETRY_AT <= now — so with 4 uvicorn workers a
     job still fires once,
  3. starts each claimed job as an AUTO run.

Every run executes on its own thread; at most `max_parallel` load at once, the
rest wait (their heartbeat keeps them alive while queued). A job whose previous
run is still going is recorded as 'skipped', never stacked.
A run missed while the server was down fires once at the next tick.
"""
import json
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine
from app.services import get_data_sync_service as svc
from app.services.get_data_schema import ensure_tables, JOB_TABLE


class GetDataScheduler:
    def __init__(self, interval_seconds: int = 30, max_parallel: int = 3) -> None:
        self._interval = interval_seconds
        self._slots = threading.BoundedSemaphore(max_parallel)
        self._max_parallel = max_parallel
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._active: set = set()
        self._active_lock = threading.Lock()
        self._last_tick: Optional[datetime] = None

    # ── Lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            try:
                ensure_tables()
                svc.reconcile_stale()
            except Exception as e:
                logger.warning(f"[get-data-sched] table check failed: {e}")
            self._running = True
            self._thread = threading.Thread(target=self._loop, name="GetDataScheduler", daemon=True)
            self._thread.start()
        logger.info(f"[get-data-sched] started — interval={self._interval}s, "
                    f"max_parallel={self._max_parallel}")

    def stop(self) -> None:
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10.0)
        logger.info("[get-data-sched] stopped")

    @property
    def status(self) -> Dict[str, Any]:
        with self._active_lock:
            active = sorted(self._active)
        return {"running": self._running, "interval_seconds": self._interval,
                "max_parallel": self._max_parallel, "active_run_ids": active,
                "last_tick": self._last_tick.isoformat() + "Z" if self._last_tick else None}

    # ── Runs ─────────────────────────────────────────────────────────────────
    def submit(self, run_id: int) -> None:
        """Execute a started run in the background (used by Run now too)."""
        threading.Thread(target=self._execute, args=(run_id,),
                         name=f"GetDataRun-{run_id}", daemon=True).start()

    def _execute(self, run_id: int) -> None:
        with self._active_lock:
            self._active.add(run_id)
        try:
            svc.execute_run(run_id, slot=self._slots)
        except Exception as e:
            logger.error(f"[get-data-sched] run {run_id} crashed outside the engine: {e}")
        finally:
            with self._active_lock:
                self._active.discard(run_id)

    # ── Loop ─────────────────────────────────────────────────────────────────
    def _loop(self) -> None:
        self._sleep(10)
        while self._running:
            try:
                self._tick()
            except Exception as e:
                logger.warning(f"[get-data-sched] tick error: {e}")
            self._sleep(self._interval)

    def _sleep(self, seconds: int) -> None:
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(2, max(0.0, end - time.time())))

    def _tick(self) -> None:
        self._last_tick = datetime.utcnow()
        svc.reconcile_stale()
        for job_id, name, attempt in self._claim_due():
            try:
                run_id = svc.start_run(job_id, "AUTO", "scheduler", attempt=attempt)
            except svc.AlreadyRunning as e:
                svc.record_skipped(job_id, name, f"Skipped — {e}")
                continue
            except Exception as e:
                logger.error(f"[get-data-sched] could not start job {job_id}: {e}")
                continue
            logger.info(f"[get-data-sched] job {job_id} '{name}' → run {run_id} (attempt {attempt})")
            self.submit(run_id)

    def _claim_due(self) -> List[Tuple[int, str, int]]:
        eng = get_data_engine()
        with eng.connect() as c:
            due = c.execute(text(f"""
                SELECT JOB_ID, JOB_NAME, TRIGGER_TYPE, SCHEDULE_CONFIG,
                       CASE WHEN TRIGGER_TYPE='schedule' AND NEXT_RUN_AT <= SYSUTCDATETIME()
                            THEN 1 ELSE 0 END AS SCHED_DUE
                FROM {JOB_TABLE}
                WHERE ENABLED = 1
                  AND ((TRIGGER_TYPE = 'schedule' AND NEXT_RUN_AT <= SYSUTCDATETIME())
                       OR RETRY_AT <= SYSUTCDATETIME())
            """)).mappings().fetchall()
        claimed: List[Tuple[int, str, int]] = []
        for j in due:
            jid = int(j["JOB_ID"])
            with eng.begin() as c:
                if j["SCHED_DUE"]:
                    try:
                        cfg = json.loads(j["SCHEDULE_CONFIG"] or "{}")
                    except Exception:
                        cfg = {}
                    nxt = svc.next_run_utc("schedule", cfg)
                    # OUTPUT, not rowcount: rowcount reads -1 on a pooled
                    # connection left with SET NOCOUNT ON, which would silently
                    # advance NEXT_RUN_AT without ever running the job.
                    got = c.execute(text(f"""
                        UPDATE {JOB_TABLE} SET NEXT_RUN_AT = :nr, RETRY_AT = NULL
                        OUTPUT INSERTED.JOB_ID
                        WHERE JOB_ID = :id AND ENABLED = 1 AND NEXT_RUN_AT <= SYSUTCDATETIME()
                    """), {"nr": nxt, "id": jid}).fetchone()
                    attempt = 1
                else:
                    got = c.execute(text(f"""
                        UPDATE {JOB_TABLE} SET RETRY_AT = NULL
                        OUTPUT INSERTED.JOB_ID
                        WHERE JOB_ID = :id AND ENABLED = 1 AND RETRY_AT <= SYSUTCDATETIME()
                    """), {"id": jid}).fetchone()
                    attempt = 2
            if got:
                claimed.append((jid, j["JOB_NAME"], attempt))
        return claimed


# Module-level singleton.
get_data_scheduler = GetDataScheduler()
