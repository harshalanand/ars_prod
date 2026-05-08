"""
Listing Job Manager — lightweight in-memory async job tracker for listing generation.

Supports:
  - Launching listing jobs in background threads
  - Progress polling (current step, step_timings)
  - Cancel/kill (sets flag; listing code checks between parts)
  - Parallel MAJ_CAT workers (multiple threads sharing ARS_LISTING)

Usage:
    jm = job_manager
    job_id = jm.start(job_type="listing", runner=my_runner_fn, payload=req)
    status = jm.status(job_id)
    jm.cancel(job_id)
"""
import uuid
import time
import threading
import traceback
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from loguru import logger


class ListingJob:
    def __init__(self, job_id: str, job_type: str, payload: Any, user: Optional[str] = None):
        self.job_id = job_id
        self.job_type = job_type           # "listing" | "parallel_listing"
        self.payload = payload              # request dict
        self.user = user
        self.status = "pending"             # pending|running|completed|failed|cancelled
        self.created_at = datetime.utcnow()
        self.started_at: Optional[datetime] = None
        self.completed_at: Optional[datetime] = None
        self.current_step: str = ""
        self.progress_pct: int = 0          # 0..100 approximate
        self.step_timings: List[Dict] = []
        self.result: Optional[Dict] = None
        self.error: Optional[str] = None
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def request_cancel(self):
        self._cancel.set()

    def update_step(self, step: str, progress_pct: Optional[int] = None):
        with self._lock:
            self.current_step = step
            if progress_pct is not None:
                self.progress_pct = max(0, min(100, progress_pct))

    def append_timing(self, step: str, seconds: float):
        with self._lock:
            self.step_timings.append({"step": step, "seconds": round(seconds, 2)})

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            elapsed = None
            if self.started_at:
                end = self.completed_at or datetime.utcnow()
                elapsed = round((end - self.started_at).total_seconds(), 1)
            return {
                "job_id": self.job_id,
                "job_type": self.job_type,
                "status": self.status,
                "user": self.user,
                "created_at": self.created_at.isoformat(),
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "completed_at": self.completed_at.isoformat() if self.completed_at else None,
                "elapsed_sec": elapsed,
                "current_step": self.current_step,
                "progress_pct": self.progress_pct,
                "step_timings": list(self.step_timings),
                "result": self.result,
                "error": self.error,
                "cancel_requested": self.cancel_requested(),
            }


class ListingJobManager:
    """
    Thread-safe registry of listing jobs. Auto-prunes finished jobs after 2 hours
    to avoid unbounded memory growth.
    """
    RETENTION_SEC = 2 * 3600

    def __init__(self):
        self._jobs: Dict[str, ListingJob] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ basic

    def create(self, job_type: str, payload: Any, user: Optional[str] = None) -> ListingJob:
        job_id = f"LST_{uuid.uuid4().hex[:10]}"
        job = ListingJob(job_id, job_type, payload, user)
        with self._lock:
            self._jobs[job_id] = job
            self._prune_locked()
        return job

    def get(self, job_id: str) -> Optional[ListingJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        if job.status not in ("pending", "running"):
            return False
        job.request_cancel()
        logger.info(f"Cancel requested for listing job {job_id}")
        return True

    def list_active(self) -> List[Dict]:
        with self._lock:
            return [j.to_dict() for j in self._jobs.values()
                    if j.status in ("pending", "running")]

    def list_all(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [j.to_dict() for j in jobs[:limit]]

    # ------------------------------------------------------------------ runner

    def start(self, job: ListingJob, runner: Callable[[ListingJob], Dict]) -> str:
        """Start `runner(job)` in a daemon thread. Runner must handle its own errors."""
        def _target():
            job.status = "running"
            job.started_at = datetime.utcnow()
            try:
                result = runner(job)
                if job.cancel_requested():
                    job.status = "cancelled"
                    logger.info(f"Job {job.job_id} cancelled")
                else:
                    job.status = "completed"
                    job.result = result
                    job.progress_pct = 100
                    logger.info(f"Job {job.job_id} completed")
            except InterruptedError:
                job.status = "cancelled"
                logger.info(f"Job {job.job_id} cancelled via InterruptedError")
            except Exception as e:
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}"
                logger.error(f"Job {job.job_id} failed: {e}\n{traceback.format_exc()[:2000]}")
            finally:
                job.completed_at = datetime.utcnow()

        t = threading.Thread(target=_target, name=f"Listing-{job.job_id}", daemon=True)
        job._thread = t
        t.start()
        return job.job_id

    # ------------------------------------------------------------------ internal

    def _prune_locked(self):
        """Drop finished jobs older than RETENTION_SEC. Caller holds self._lock."""
        now = time.time()
        stale = []
        for jid, j in self._jobs.items():
            if j.status in ("pending", "running"):
                continue
            end = j.completed_at or j.created_at
            age = now - end.timestamp()
            if age > self.RETENTION_SEC:
                stale.append(jid)
        for jid in stale:
            self._jobs.pop(jid, None)


# ── Module-level singleton ────────────────────────────────────────────────────
job_manager = ListingJobManager()
