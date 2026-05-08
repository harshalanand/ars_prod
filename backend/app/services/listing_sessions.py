"""
listing_sessions.py
Per-session capture for /listing/generate runs. Each call writes:
  1. A header row in ARS_LISTING_SESSIONS (DB) — for searchability and the UI list
  2. A text log file at backend/logs/listing_sessions/<session_id>.log — every
     loguru event raised during the request lands here via a temporary sink

Lifecycle:
    sid = make_session_id()
    start_session(sid, user, request_dict)
    try:
        ...do work, log freely with loguru.logger.info(...) ...
    finally:
        end_session(sid, status, summary)

Reading:
    list_sessions(limit=50)     -> rows for the UI table
    get_session(sid)            -> single row metadata
    get_session_log(sid)        -> log file contents as a string
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine


# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------
SESSIONS_TABLE = "ARS_LISTING_SESSIONS"

# logs/listing_sessions/<session_id>.log under backend/
_BACKEND_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
LOG_DIR = os.path.join(_BACKEND_DIR, "logs", "listing_sessions")
os.makedirs(LOG_DIR, exist_ok=True)


# Map session_id -> loguru sink id (so end_session can remove the right one).
_ACTIVE_SINKS: Dict[str, int] = {}
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Schema bootstrap (idempotent — safe to call before each insert)
# ---------------------------------------------------------------------------
_SCHEMA_DDL = f"""
IF OBJECT_ID('dbo.{SESSIONS_TABLE}','U') IS NULL
CREATE TABLE dbo.{SESSIONS_TABLE} (
    SESSION_ID        NVARCHAR(50)   NOT NULL,
    USER_NAME         NVARCHAR(200)  NULL,
    STARTED_AT        DATETIME       NOT NULL DEFAULT GETDATE(),
    COMPLETED_AT      DATETIME       NULL,
    DURATION_SEC      FLOAT          NULL,
    STATUS            NVARCHAR(20)   NOT NULL DEFAULT 'RUNNING',
    ALLOCATION_MODE   NVARCHAR(20)   NULL,
    PARALLEL_WORKERS  INT            NULL,
    RDC_MODE          NVARCHAR(20)   NULL,
    STORE_COUNT       INT            NULL,
    MAJCAT_COUNT      INT            NULL,
    LISTED_OPTS       INT            NULL,
    ALLOC_ROWS        INT            NULL,
    SHIP_QTY_TOTAL    FLOAT          NULL,
    HOLD_QTY_TOTAL    FLOAT          NULL,
    FAILED_MAJCATS    INT            NULL,
    ERROR_MSG         NVARCHAR(2000) NULL,
    STEP_TIMINGS      NVARCHAR(MAX)  NULL,
    REQUEST_JSON      NVARCHAR(MAX)  NULL,
    LOG_FILE_PATH     NVARCHAR(500)  NULL,
    TABLES_AFFECTED   NVARCHAR(MAX)  NULL,
    PARKED_STATUS     NVARCHAR(20)   NULL,
    CONSTRAINT PK_{SESSIONS_TABLE} PRIMARY KEY (SESSION_ID)
);
"""

# Idempotent column-add for existing deployments (table already created
# without the new fields). Runs after table-create so the columns exist on
# both fresh and upgraded DBs.
_COLUMN_RECONCILE_DDL = [
    (
        "TABLES_AFFECTED",
        f"IF COL_LENGTH('dbo.{SESSIONS_TABLE}','TABLES_AFFECTED') IS NULL "
        f"ALTER TABLE dbo.{SESSIONS_TABLE} ADD TABLES_AFFECTED NVARCHAR(MAX) NULL",
    ),
    (
        "PARKED_STATUS",
        f"IF COL_LENGTH('dbo.{SESSIONS_TABLE}','PARKED_STATUS') IS NULL "
        f"ALTER TABLE dbo.{SESSIONS_TABLE} ADD PARKED_STATUS NVARCHAR(20) NULL",
    ),
]

_INDEX_DDL = (
    f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_{SESSIONS_TABLE}_started') "
    f"CREATE INDEX IX_{SESSIONS_TABLE}_started "
    f"ON dbo.{SESSIONS_TABLE} (STARTED_AT DESC) "
    f"INCLUDE (STATUS, ALLOCATION_MODE, USER_NAME)"
)


def ensure_sessions_table(conn) -> None:
    """Idempotent — ensures the sessions table + index exist."""
    conn.execute(text(_SCHEMA_DDL))
    # Add columns introduced after the table was first created.
    for col_name, ddl in _COLUMN_RECONCILE_DDL:
        try:
            conn.execute(text(ddl))
        except Exception as e:
            logger.warning(f"[sessions] add column {col_name} failed: {e}")
    try:
        conn.execute(text(_INDEX_DDL))
    except Exception as e:
        logger.warning(f"[sessions] index ensure failed: {e}")
    conn.commit()


# ---------------------------------------------------------------------------
# Session id
# ---------------------------------------------------------------------------
def make_session_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


# ---------------------------------------------------------------------------
# Session start / end
# ---------------------------------------------------------------------------
def start_session(
    session_id: str,
    user_name: Optional[str],
    request_dict: Dict[str, Any],
) -> str:
    """
    Begin a session: insert the DB row and attach a loguru sink that
    captures every log line into logs/listing_sessions/<session_id>.log.

    Returns the absolute path of the log file (also stored in the DB row).
    """
    log_path = os.path.join(LOG_DIR, f"{session_id}.log")

    # Loguru sink — writes everything from this point until end_session()
    # removes the sink. Filter by record["extra"]["session_id"] == session_id
    # so concurrent sessions don't bleed into each other.
    sink_id = logger.add(
        log_path,
        level="DEBUG",
        enqueue=False,
        backtrace=False,
        diagnose=False,
        format=("{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
                "{name}:{function}:{line} | {message}"),
        filter=lambda record: record["extra"].get("session_id") == session_id,
    )
    with _LOCK:
        _ACTIVE_SINKS[session_id] = sink_id

    # DB header row
    engine = get_data_engine()
    with engine.connect() as conn:
        ensure_sessions_table(conn)
        try:
            conn.execute(text(f"""
                INSERT INTO {SESSIONS_TABLE}
                    (SESSION_ID, USER_NAME, STARTED_AT, STATUS,
                     ALLOCATION_MODE, PARALLEL_WORKERS, RDC_MODE,
                     STORE_COUNT, MAJCAT_COUNT,
                     REQUEST_JSON, LOG_FILE_PATH)
                VALUES
                    (:sid, :user, GETDATE(), 'RUNNING',
                     :mode, :workers, :rdc_mode,
                     :store_count, :majcat_count,
                     :req_json, :log_path)
            """), {
                "sid":          session_id,
                "user":         (user_name or "")[:200],
                "mode":         (request_dict.get("allocation_mode") or "")[:20],
                "workers":      int(request_dict.get("parallel_workers") or 0) or None,
                "rdc_mode":     (request_dict.get("rdc_mode") or "")[:20],
                "store_count":  len(request_dict.get("store_codes") or []) or None,
                "majcat_count": len(request_dict.get("maj_cat_values") or []) or None,
                "req_json":     json.dumps(request_dict, default=str)[:8000],
                "log_path":     log_path,
            })
            conn.commit()
        except Exception as e:
            # Logging the session itself must never crash the request.
            logger.warning(f"[sessions] insert failed for {session_id}: {e}")

    # Mark the very first log line so users see the boundary in the file.
    with logger.contextualize(session_id=session_id):
        logger.info(
            f"=== SESSION START id={session_id} user={user_name} "
            f"mode={request_dict.get('allocation_mode')} "
            f"workers={request_dict.get('parallel_workers')} ==="
        )
    return log_path


def end_session(
    session_id: str,
    status: str,                 # 'SUCCESS' | 'FAILED' | 'CANCELLED'
    summary: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Close a session: write summary metrics to the DB row and detach the
    loguru sink. Always call this from a finally: even on exception.

    **Cancel-safe**: refuses to overwrite a session row whose STATUS is
    already 'CANCELLED'. The cancel pathway (cancel_batch / kill_session)
    flips STATUS to 'CANCELLED' the moment the user clicks Cancel; if the
    background daemon thread later finishes its remaining work and tries
    to call end_session('SUCCESS', ...), this guard preserves the cancel.
    """
    summary = summary or {}
    with logger.contextualize(session_id=session_id):
        logger.info(
            f"=== SESSION END id={session_id} status={status} "
            f"duration={summary.get('duration_sec')}s "
            f"alloc_rows={summary.get('alloc_rows')} "
            f"failed={summary.get('failed_majcats')} ==="
        )

    # Update DB row
    engine = get_data_engine()
    tables_affected = summary.get("tables_affected")
    parked_status   = summary.get("parked_status")
    try:
        with engine.connect() as conn:
            ensure_sessions_table(conn)  # add new cols on legacy DBs
            conn.execute(text(f"""
                UPDATE {SESSIONS_TABLE} SET
                    COMPLETED_AT     = GETDATE(),
                    DURATION_SEC     = :dur,
                    STATUS           = CASE WHEN STATUS = 'CANCELLED'
                                            THEN STATUS
                                            ELSE :status END,
                    LISTED_OPTS      = :listed,
                    ALLOC_ROWS       = :alloc_rows,
                    SHIP_QTY_TOTAL   = :ship,
                    HOLD_QTY_TOTAL   = :hold,
                    FAILED_MAJCATS   = :failed,
                    ERROR_MSG        = CASE WHEN STATUS = 'CANCELLED'
                                            THEN ERROR_MSG
                                            ELSE :err END,
                    STEP_TIMINGS     = :timings,
                    TABLES_AFFECTED  = :tables_affected,
                    PARKED_STATUS    = :parked_status
                WHERE SESSION_ID = :sid
            """), {
                "sid":        session_id,
                "dur":        float(summary.get("duration_sec") or 0) or None,
                "status":     status[:20],
                "listed":     int(summary.get("listed_opts") or 0) or None,
                "alloc_rows": int(summary.get("alloc_rows")  or 0) or None,
                "ship":       float(summary.get("ship_qty_total") or 0) or None,
                "hold":       float(summary.get("hold_qty_total") or 0) or None,
                "failed":     int(summary.get("failed_majcats") or 0) or None,
                "err":        (summary.get("error") or "")[:2000] or None,
                "timings":    json.dumps(summary.get("step_timings") or [],
                                         default=str)[:30000],
                "tables_affected": (
                    json.dumps(tables_affected, default=str)
                    if tables_affected is not None else None
                ),
                "parked_status":   (parked_status[:20]
                                     if parked_status else None),
            })
            conn.commit()
    except Exception as e:
        logger.warning(f"[sessions] update failed for {session_id}: {e}")

    # Detach loguru sink
    with _LOCK:
        sink_id = _ACTIVE_SINKS.pop(session_id, None)
    if sink_id is not None:
        try:
            logger.remove(sink_id)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Read-side (UI)
# ---------------------------------------------------------------------------
def list_sessions(limit: int = 100,
                  status: Optional[str] = None,
                  mode:   Optional[str] = None,
                  user:   Optional[str] = None) -> List[Dict[str, Any]]:
    where = []
    params: Dict[str, Any] = {"lim": int(limit)}
    if status:
        where.append("STATUS = :status"); params["status"] = status
    if mode:
        where.append("ALLOCATION_MODE = :mode"); params["mode"] = mode
    if user:
        where.append("USER_NAME = :user"); params["user"] = user
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    engine = get_data_engine()
    with engine.connect() as conn:
        ensure_sessions_table(conn)
        rows = conn.execute(text(f"""
            SELECT TOP (:lim)
                SESSION_ID, USER_NAME, STARTED_AT, COMPLETED_AT, DURATION_SEC,
                STATUS, ALLOCATION_MODE, PARALLEL_WORKERS,
                RDC_MODE, STORE_COUNT, MAJCAT_COUNT,
                LISTED_OPTS, ALLOC_ROWS, SHIP_QTY_TOTAL, HOLD_QTY_TOTAL,
                FAILED_MAJCATS, ERROR_MSG
            FROM {SESSIONS_TABLE}
            {where_sql}
            ORDER BY STARTED_AT DESC
        """), params).fetchall()
    return [
        {
            "session_id":     r[0],
            "user":           r[1],
            "started_at":     r[2].isoformat() if r[2] else None,
            "completed_at":   r[3].isoformat() if r[3] else None,
            "duration_sec":   float(r[4]) if r[4] is not None else None,
            "status":         r[5],
            "allocation_mode": r[6],
            "workers":        int(r[7]) if r[7] is not None else None,
            "rdc_mode":       r[8],
            "store_count":    int(r[9])  if r[9]  is not None else None,
            "majcat_count":   int(r[10]) if r[10] is not None else None,
            "listed_opts":    int(r[11]) if r[11] is not None else None,
            "alloc_rows":     int(r[12]) if r[12] is not None else None,
            "ship_qty_total": float(r[13]) if r[13] is not None else None,
            "hold_qty_total": float(r[14]) if r[14] is not None else None,
            "failed_majcats": int(r[15]) if r[15] is not None else None,
            "error_msg":      r[16],
        }
        for r in rows
    ]


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    engine = get_data_engine()
    with engine.connect() as conn:
        ensure_sessions_table(conn)
        row = conn.execute(text(f"""
            SELECT SESSION_ID, USER_NAME, STARTED_AT, COMPLETED_AT, DURATION_SEC,
                   STATUS, ALLOCATION_MODE, PARALLEL_WORKERS,
                   RDC_MODE, STORE_COUNT, MAJCAT_COUNT,
                   LISTED_OPTS, ALLOC_ROWS, SHIP_QTY_TOTAL, HOLD_QTY_TOTAL,
                   FAILED_MAJCATS, ERROR_MSG, STEP_TIMINGS, REQUEST_JSON,
                   LOG_FILE_PATH, TABLES_AFFECTED, PARKED_STATUS
            FROM {SESSIONS_TABLE} WHERE SESSION_ID = :sid
        """), {"sid": session_id}).fetchone()
    if not row:
        return None

    def _maybe_json(s: Optional[str]):
        if not s: return None
        try: return json.loads(s)
        except Exception: return s

    return {
        "session_id":     row[0],
        "user":           row[1],
        "started_at":     row[2].isoformat() if row[2] else None,
        "completed_at":   row[3].isoformat() if row[3] else None,
        "duration_sec":   float(row[4]) if row[4] is not None else None,
        "status":         row[5],
        "allocation_mode": row[6],
        "workers":        int(row[7]) if row[7] is not None else None,
        "rdc_mode":       row[8],
        "store_count":    int(row[9])  if row[9]  is not None else None,
        "majcat_count":   int(row[10]) if row[10] is not None else None,
        "listed_opts":    int(row[11]) if row[11] is not None else None,
        "alloc_rows":     int(row[12]) if row[12] is not None else None,
        "ship_qty_total": float(row[13]) if row[13] is not None else None,
        "hold_qty_total": float(row[14]) if row[14] is not None else None,
        "failed_majcats": int(row[15]) if row[15] is not None else None,
        "error_msg":      row[16],
        "step_timings":   _maybe_json(row[17]),
        "request":        _maybe_json(row[18]),
        "log_file_path":  row[19],
        "tables_affected": _maybe_json(row[20]),
        "parked_status":   row[21],
    }


def kill_session(session_id: str, reason: str = "killed by user") -> Dict[str, Any]:
    """
    Force-terminate a RUNNING session: sets the cooperative cancel event so
    worker threads exit, KILLs every SQL Server SPID registered with this
    batch_id (so in-flight queries die immediately), marks the session row
    FAILED, and cancels its alloc-queue rows.

    `session_id` IS the `batch_id` for parallel modes, so this drives the
    same cancel infrastructure as POST /cancel-batch.
    """
    from app.services.alloc_queue import QUEUE_TABLE
    from app.services import alloc_cancellation as ac

    # Step 0: signal the workers + KILL their SPIDs. No-op if nobody
    # registered (e.g. sequential mode mid-Stage-A) — the bookkeeping
    # update below still closes the session.
    cancel_info = ac.hard_cancel(session_id)

    engine = get_data_engine()
    cancelled_queue_rows = 0
    sess_row_updated = False
    with engine.connect() as conn:
        ensure_sessions_table(conn)
        # 1) Mark the session row CANCELLED so the orchestrator's post-Part-8
        #    cancel check sees it and short-circuits before Part 8.4 / 8.5
        #    / 8.6 / parking. (Only if still RUNNING — no-op otherwise.)
        res = conn.execute(text(f"""
            UPDATE {SESSIONS_TABLE}
               SET STATUS       = 'CANCELLED',
                   COMPLETED_AT = GETDATE(),
                   ERROR_MSG    = LEFT(ISNULL(ERROR_MSG,'') + :why, 2000)
             WHERE SESSION_ID = :sid AND STATUS = 'RUNNING'
        """), {"sid": session_id, "why": f" [{reason}]"})
        sess_row_updated = bool(res.rowcount)
        # 2) Mark the linked alloc-queue rows CANCELLED (terminal — never
        #    re-claimed by claim_next, never resurrected by mark_in_progress).
        #    Also freezes any deadlock-FAILED rows that would otherwise be
        #    auto-retried by the next claim_next call.
        try:
            res2 = conn.execute(text(f"""
                UPDATE {QUEUE_TABLE}
                   SET STATUS       = 'CANCELLED',
                       COMPLETED_AT = GETDATE(),
                       ERROR_MSG    = LEFT(ISNULL(ERROR_MSG,'') + :why, 2000)
                 WHERE BATCH_ID = :sid
                   AND STATUS IN ('PENDING','IN_PROGRESS','FAILED')
            """), {"sid": session_id, "why": f" [{reason}]"})
            cancelled_queue_rows = int(res2.rowcount or 0)
        except Exception as e:
            logger.warning(f"[sessions] queue cancel failed for {session_id}: {e}")
        conn.commit()
    # Detach the loguru sink if it's still attached
    with _LOCK:
        sink_id = _ACTIVE_SINKS.pop(session_id, None)
    if sink_id is not None:
        try: logger.remove(sink_id)
        except Exception: pass
    return {
        "session_id": session_id,
        "session_row_updated": sess_row_updated,
        "queue_rows_cancelled": cancelled_queue_rows,
        "kill_attempted": cancel_info.get("kill_attempted", 0),
        "killed":         cancel_info.get("killed", []),
        "kill_failed":    cancel_info.get("kill_failed", []),
        "event_set":      cancel_info.get("event_set", False),
    }


def delete_session(session_id: str) -> Dict[str, Any]:
    """
    Permanently remove a session header row + its log file. Allowed only
    for sessions that have already ended (SUCCESS/FAILED) — never for a
    RUNNING session, since deleting that would orphan its log sink.
    """
    engine = get_data_engine()
    deleted_db = 0
    deleted_log = False
    with engine.connect() as conn:
        ensure_sessions_table(conn)
        # Refuse to delete a still-running session — caller should kill first.
        running = conn.execute(text(
            f"SELECT 1 FROM {SESSIONS_TABLE} "
            f"WHERE SESSION_ID = :sid AND STATUS = 'RUNNING'"
        ), {"sid": session_id}).fetchone()
        if running:
            raise RuntimeError(
                f"session {session_id} is still RUNNING — kill it first")
        res = conn.execute(text(
            f"DELETE FROM {SESSIONS_TABLE} WHERE SESSION_ID = :sid"
        ), {"sid": session_id})
        deleted_db = int(res.rowcount or 0)
        conn.commit()
    # Best-effort log file cleanup — never fatal.
    log_path = os.path.join(LOG_DIR, f"{session_id}.log")
    if os.path.exists(log_path):
        try:
            os.remove(log_path)
            deleted_log = True
        except Exception as e:
            logger.warning(f"[sessions] log file delete failed for {session_id}: {e}")
    return {
        "session_id": session_id,
        "deleted_db_row": deleted_db,
        "deleted_log_file": deleted_log,
    }


def get_session_log(session_id: str,
                    tail_lines: Optional[int] = None) -> Optional[str]:
    """
    Read the per-session log file. Returns None if it doesn't exist.
    Pass tail_lines=N to return only the last N lines (cheaper for very
    long runs).
    """
    log_path = os.path.join(LOG_DIR, f"{session_id}.log")
    if not os.path.exists(log_path):
        return None
    with open(log_path, encoding="utf-8", errors="replace") as f:
        if tail_lines is None:
            return f.read()
        # Cheap tail: read the whole file then slice. For our log volumes
        # (~MB per session), this is fine. If logs ever grow huge we can
        # switch to a reverse-read implementation.
        lines = f.readlines()
        return "".join(lines[-tail_lines:])
