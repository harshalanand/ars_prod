"""
SAP pull scheduler — a small background daemon that fires scheduled pulls.

Mirrors the report scheduler's claim-and-run model (see report_scheduler_service):
one thread ticks every `interval` seconds, atomically claims DUE pulls
(NEXT_RUN_AT <= now) so no pull double-fires across uvicorn workers, advances
NEXT_RUN_AT, and runs each on a small ThreadPoolExecutor. A pull already running
is skipped rather than stacked.

Manual "Run now" does NOT go through here — the endpoint runs it inline.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine
from app.services.report_scheduler_service import compute_next_run
from app.services import sap_pull_service as pulls
from app.services.sap_pull_service import DEF_TABLE, RUN_TABLE


def _reconcile_orphaned_runs() -> int:
    """Any run left 'running' by a restart is marked failed. Called at startup."""
    try:
        eng = get_data_engine()
        with eng.begin() as c:
            res = c.execute(text(f"""
                UPDATE {RUN_TABLE}
                SET STATUS='failed', MESSAGE='run interrupted — server restarted',
                    COMPLETED_AT=SYSUTCDATETIME()
                WHERE STATUS='running'
            """))
        n = res.rowcount or 0
        if n:
            logger.warning(f"[sap-sched] reconciled {n} orphaned run(s) → failed")
        return n
    except Exception as e:
        logger.warning(f"[sap-sched] reconcile skipped: {e}")
        return 0


class SapSchedulerService:
    def __init__(self, interval_seconds: int = 30, max_parallel: int = 1) -> None:
        self._interval = interval_seconds
        self._max_parallel = max_parallel
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._active: set = set()
        self._active_lock = threading.Lock()
        self._last_tick: Optional[datetime] = None

    # ── Lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            try:
                pulls.ensure_pull_tables()
                _reconcile_orphaned_runs()
            except Exception as e:
                logger.warning(f"[sap-sched] ensure tables failed: {e}")
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_parallel, thread_name_prefix="SapPull")
            self._running = True
            self._thread = threading.Thread(
                target=self._loop, name="SapScheduler", daemon=True)
            self._thread.start()
        logger.info(f"[sap-sched] started — interval={self._interval}s, "
                    f"max_parallel={self._max_parallel}")

    def stop(self) -> None:
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10.0)
        if self._executor:
            self._executor.shutdown(wait=False)
        logger.info("[sap-sched] stopped")

    @property
    def status(self) -> Dict[str, Any]:
        with self._active_lock:
            active = list(self._active)
        return {
            "running": self._running,
            "interval_seconds": self._interval,
            "max_parallel": self._max_parallel,
            "active_pull_ids": active,
            "last_tick": self._last_tick.isoformat() if self._last_tick else None,
        }

    # ── Internals ────────────────────────────────────────────────────────────
    def _loop(self) -> None:
        self._sleep_interruptible(self._interval)
        while self._running:
            try:
                self._tick()
            except Exception as e:
                logger.warning(f"[sap-sched] tick error: {e}")
            self._sleep_interruptible(self._interval)

    def _sleep_interruptible(self, seconds: int) -> None:
        elapsed = 0
        while self._running and elapsed < seconds:
            time.sleep(min(5, seconds - elapsed))
            elapsed += 5

    def _tick(self) -> None:
        self._last_tick = datetime.utcnow()
        for pull in self._claim_due():
            self._submit(pull)

    def _claim_due(self) -> List[Dict[str, Any]]:
        eng = get_data_engine()
        claimed: List[Dict[str, Any]] = []
        with eng.connect() as conn:
            candidates = conn.execute(text(f"""
                SELECT PULL_ID, SCHEDULE_CONFIG FROM {DEF_TABLE}
                WHERE TRIGGER_TYPE='schedule' AND ENABLED=1
                  AND NEXT_RUN_AT IS NOT NULL AND NEXT_RUN_AT <= SYSUTCDATETIME()
            """)).fetchall()
        for pid, sched_json in candidates:
            try:
                cfg = json.loads(sched_json or "{}")
            except Exception:
                cfg = {}
            next_run = compute_next_run(cfg)
            with eng.begin() as conn:
                res = conn.execute(text(f"""
                    UPDATE {DEF_TABLE}
                    SET NEXT_RUN_AT=:nr, UPDATED_AT=SYSUTCDATETIME()
                    WHERE PULL_ID=:pid AND TRIGGER_TYPE='schedule' AND ENABLED=1
                      AND NEXT_RUN_AT <= SYSUTCDATETIME()
                """), {"nr": next_run, "pid": pid})
            if res.rowcount == 1:
                row = pulls.get_pull(pid)
                if row:
                    claimed.append(row)
        return claimed

    def _submit(self, pull: Dict[str, Any]) -> None:
        if not self._executor:
            self._run_guarded(pull)
            return
        self._executor.submit(self._run_guarded, pull)

    def _run_guarded(self, pull: Dict[str, Any]) -> None:
        pid = int(pull["PULL_ID"])
        with self._active_lock:
            if pid in self._active:
                logger.info(f"[sap-sched] pull {pid} already running — skipped")
                return
            self._active.add(pid)
        try:
            pulls.run_pull(pull, "schedule", pull.get("CREATED_BY"))
        except Exception as e:
            logger.error(f"[sap-sched] pull {pid} crashed: {e}")
        finally:
            with self._active_lock:
                self._active.discard(pid)


# Module-level singleton.
sap_scheduler = SapSchedulerService()
