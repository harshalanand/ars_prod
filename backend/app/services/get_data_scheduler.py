"""
Get Data scheduler — fires scheduled sync jobs and runs manual "Run now" requests.

Same claim-and-run model as the SAP / report schedulers: every `interval`
seconds one thread
  1. marks runs whose heartbeat stopped as failed (crashed/restarted worker),
  2. atomically claims DUE jobs — NEXT_RUN_AT <= now (advanced to the next IST
     slot in the same UPDATE) or RETRY_AT <= now — so with 4 uvicorn workers a
     job still fires once,
  3. starts each claimed job as an AUTO run,
  4. checks "When the source changes" jobs whose check is due (claimed the
     same way on NEXT_CHECK_AT) and starts the ones get_data_watch.decide()
     clears. Their daily safety run is a NEXT_RUN_AT slot (step 2), skipped
     when the job loaded in the last 23 hours.

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
from app.services import get_data_watch as gw
from app.services.get_data_schema import ensure_tables, JOB_TABLE
from app.services.get_data_snowflake import sf_message


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
        for job_id, name, attempt, trigger in self._claim_due():
            by = "scheduler"
            if trigger == "on_change" and attempt == 1:
                # The daily safety run of an on-change job: only if nothing loaded lately.
                go, why = gw.safety_due(gw.last_ok_start(job_id), svc._now())
                if not go:
                    logger.info(f"[get-data-sched] job {job_id} '{name}': {why}")
                    continue
                by = "safety run"
            try:
                run_id = svc.start_run(job_id, "AUTO", by, attempt=attempt)
            except svc.AlreadyRunning as e:
                svc.record_skipped(job_id, name, f"Skipped — {e}")
                continue
            except Exception as e:
                logger.error(f"[get-data-sched] could not start job {job_id}: {e}")
                continue
            logger.info(f"[get-data-sched] job {job_id} '{name}' → run {run_id} (attempt {attempt}, {by})")
            self.submit(run_id)
        self._check_changes()

    # ── When the source changes ─────────────────────────────────────────────
    def _check_changes(self) -> None:
        """One Snowflake round trip for every watched job whose check is due
        (metadata only — no warehouse), then run or record why it waits."""
        jobs = gw.claim_checks()
        if not jobs:
            return
        tables = sorted({t for j in jobs for t in (j["cfg"].get("tables") or [])})
        try:
            toks, errs, sf_now = gw.poll(tables)
        except Exception as e:
            msg = f"Couldn't check Snowflake: {sf_message(e)[:300]}"
            logger.warning(f"[get-data-sched] change check failed: {msg}")
            for j in jobs:
                gw.save_check(int(j["JOB_ID"]), "error", msg)
            return
        now = svc._now()
        for j in jobs:
            try:
                self._check_one(j, toks, errs, sf_now, now)
            except Exception as e:
                logger.warning(f"[get-data-sched] change check of job {j['JOB_ID']} failed: {e}")
                gw.save_check(int(j["JOB_ID"]), "error", f"Check failed: {e}"[:400])

    def _check_one(self, j: Dict[str, Any], toks, errs, sf_now: datetime, now: datetime) -> None:
        jid, cfg = int(j["JOB_ID"]), j["cfg"]
        tok, table, err = gw.job_token(cfg.get("tables") or [], toks, errs)
        if tok is None:
            gw.save_check(jid, "error", err or "No tables to watch")
            return
        change_at = gw.token_time(tok)
        fire, state, msg, init = gw.decide(
            cfg, token=str(tok), change_at=change_at, change_table=table,
            synced_token=j["SYNCED_TOKEN"], last_ok_start=j["LAST_OK_START"],
            last_run_start=j["LAST_RUN_START"], running=j["LOCK_RUN_ID"] is not None,
            now=now, sf_now=sf_now)
        if fire:
            try:
                run_id = svc.start_run(jid, "AUTO", gw.trigger_label(table, change_at))
                self.submit(run_id)
                msg = msg.replace(gw.STARTING_LEAD, f"Run #{run_id} started")
                logger.info(f"[get-data-sched] job {jid} '{j['JOB_NAME']}': {msg}")
            except svc.AlreadyRunning:
                state, msg = "running", msg.replace(gw.STARTING_LEAD, gw.RUNNING_LEAD)
        gw.save_check(jid, state, msg, token=str(tok), change_at=change_at,
                      change_table=table, synced_init=init)

    def _claim_due(self) -> List[Tuple[int, str, int, str]]:
        eng = get_data_engine()
        with eng.connect() as c:
            due = c.execute(text(f"""
                SELECT JOB_ID, JOB_NAME, TRIGGER_TYPE, SCHEDULE_CONFIG,
                       CASE WHEN TRIGGER_TYPE IN ('schedule', 'on_change')
                                 AND NEXT_RUN_AT <= SYSUTCDATETIME()
                            THEN 1 ELSE 0 END AS SCHED_DUE
                FROM {JOB_TABLE}
                WHERE ENABLED = 1
                  AND ((TRIGGER_TYPE IN ('schedule', 'on_change') AND NEXT_RUN_AT <= SYSUTCDATETIME())
                       OR RETRY_AT <= SYSUTCDATETIME())
            """)).mappings().fetchall()
        claimed: List[Tuple[int, str, int, str]] = []
        for j in due:
            jid = int(j["JOB_ID"])
            with eng.begin() as c:
                if j["SCHED_DUE"]:
                    try:
                        cfg = json.loads(j["SCHEDULE_CONFIG"] or "{}")
                    except Exception:
                        cfg = {}
                    nxt = svc.next_run_utc(j["TRIGGER_TYPE"], cfg)
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
                claimed.append((jid, j["JOB_NAME"], attempt, j["TRIGGER_TYPE"]))
        return claimed


# Module-level singleton.
get_data_scheduler = GetDataScheduler()
