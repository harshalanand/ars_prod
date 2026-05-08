"""
alloc_queue.py
DB-backed work queue for parallel MAJ_CAT allocation. Used by both
rule_engine_parallel_python.py and rule_engine_parallel_sql.py.

Backing table: ARS_ALLOC_MAJCAT_QUEUE (created by migration 011).

Lifecycle of a row:
    PENDING   -> claim_next   -> IN_PROGRESS  -> mark_done   -> DONE
                                              -> mark_failed -> FAILED
    FAILED    -> reset_failed_for_retry       -> PENDING       (manual retry)
    FAILED    -> claim_next (ATTEMPTS<MAX)    -> IN_PROGRESS   (auto retry)

    PENDING / IN_PROGRESS / FAILED -> /listing/cancel-batch -> CANCELLED
                                                              (terminal; never retried)

CANCELLED is a TERMINAL state. claim_next won't pick it (its WHERE filter
is PENDING-or-FAILED-with-budget, so CANCELLED is implicitly excluded).
mark_in_progress / mark_done / mark_failed all refuse to overwrite a row
that is already CANCELLED — so a subprocess worker that finishes its
in-flight MAJ_CAT after the user clicked Cancel cannot resurrect the row.
reset_failed_for_retry filters STATUS='FAILED' so manual retry never
revives a user-cancelled row.
"""
from datetime import datetime
from typing import Dict, List, Optional

from loguru import logger
from sqlalchemy import text


QUEUE_TABLE  = "ARS_ALLOC_MAJCAT_QUEUE"
MAX_ATTEMPTS = 2  # in-run auto retry budget per MAJ_CAT


# DDL kept in sync with scripts/run_011_alloc_majcat_queue.py — used by
# ensure_queue_table() below to self-heal if the migration wasn't run.
_QUEUE_DDL = f"""
IF OBJECT_ID('dbo.{QUEUE_TABLE}','U') IS NULL
CREATE TABLE dbo.{QUEUE_TABLE} (
    BATCH_ID         NVARCHAR(50)   NOT NULL,
    MAJ_CAT          NVARCHAR(50)   NOT NULL,
    OPT_COUNT        INT            NOT NULL,
    STATUS           NVARCHAR(20)   NOT NULL DEFAULT 'PENDING',
    WORKER_ID        INT            NULL,
    ATTEMPTS         INT            NOT NULL DEFAULT 0,
    PICKED_AT        DATETIME       NULL,
    COMPLETED_AT     DATETIME       NULL,
    SHIP_QTY         FLOAT          NULL,
    HOLD_QTY         FLOAT          NULL,
    ROWS_AFFECTED    INT            NULL,
    DURATION_SEC     FLOAT          NULL,
    ERROR_MSG        NVARCHAR(2000) NULL,
    ALLOCATION_MODE  NVARCHAR(20)   NULL,
    CREATED_AT       DATETIME       NOT NULL DEFAULT GETDATE(),
    CONSTRAINT PK_{QUEUE_TABLE} PRIMARY KEY (BATCH_ID, MAJ_CAT)
);
"""

_QUEUE_INDEXES = [
    (f"IX_{QUEUE_TABLE}_status",
     f"ON dbo.{QUEUE_TABLE} (BATCH_ID, STATUS, OPT_COUNT DESC)"),
    (f"IX_{QUEUE_TABLE}_created",
     f"ON dbo.{QUEUE_TABLE} (CREATED_AT DESC) "
     f"INCLUDE (STATUS, ALLOCATION_MODE)"),
]


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------
def ensure_queue_table(conn) -> None:
    """
    Idempotent: create ARS_ALLOC_MAJCAT_QUEUE (and its indexes) if missing.
    Lets the parallel allocator self-heal when migration 011 hasn't been
    run on a given environment. Cheap — both checks are metadata-only when
    the objects already exist.
    """
    conn.execute(text(_QUEUE_DDL))
    for idx_name, idx_def in _QUEUE_INDEXES:
        try:
            conn.execute(text(
                f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='{idx_name}') "
                f"CREATE INDEX {idx_name} {idx_def}"
            ))
        except Exception as e:
            logger.warning(f"[queue] index {idx_name} ensure failed: {e}")
    conn.commit()


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------
def make_batch_id() -> str:
    """Generate a unique batch id like '20260426_151220_123'."""
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
def seed_queue(conn, batch_id: str, alloc_table: str,
               allocation_mode: str,
               only_majcats: Optional[List[str]] = None) -> int:
    """
    Insert one PENDING row per MAJ_CAT for this batch.

    only_majcats=None     -> seed every MAJ_CAT present in alloc_table.
    only_majcats=[...]    -> seed only the given list (used by retry endpoint).

    Idempotent: rows already in the queue for (BATCH_ID, MAJ_CAT) are
    skipped via NOT EXISTS, so the manual retry path (which leaves the
    queue rows in place and just flips them PENDING via
    reset_failed_for_retry) does not collide with the PK.

    Returns the count of PENDING rows in the queue for this batch *after*
    the upsert — not just the number of new rows inserted. Retry callers
    rely on this so the "queue empty → bail" guard in the engines doesn't
    fire when nothing needed to be inserted.

    Performance notes:
    - GROUP BY uses the narrow IX_<alloc_table>_majcat index (added in
      _stage_b_indexes), so this is a stream aggregate over a slim scan.
    - NOLOCK on the source: seed runs right after Stage B finished writing
      to alloc_table on the same connection, so there's nothing live to
      block against — but a future caller might run it cross-session, and
      NOLOCK keeps us off any leftover IX/SCH-S waits either way.
    - OPTION (MAXDOP 4) lets SQL Server parallelise the aggregate when the
      table is large; it's a no-op on small/medium tables.
    """
    import time as _time
    ensure_queue_table(conn)

    where_mc = ""
    params: Dict[str, object] = {"batch_id": batch_id, "mode": allocation_mode}
    if only_majcats:
        keys = ", ".join(f":mc_{i}" for i in range(len(only_majcats)))
        where_mc = f"AND MAJ_CAT IN ({keys})"
        for i, mc in enumerate(only_majcats):
            params[f"mc_{i}"] = mc

    t0 = _time.time()
    res = conn.execute(text(f"""
        INSERT INTO {QUEUE_TABLE}
            (BATCH_ID, MAJ_CAT, OPT_COUNT, STATUS, ALLOCATION_MODE)
        SELECT :batch_id, src.MAJ_CAT, src.cnt, 'PENDING', :mode
        FROM (
            SELECT MAJ_CAT, COUNT_BIG(*) AS cnt
            FROM [{alloc_table}] WITH (NOLOCK)
            WHERE MAJ_CAT IS NOT NULL {where_mc}
            GROUP BY MAJ_CAT
        ) src
        WHERE NOT EXISTS (
            SELECT 1 FROM {QUEUE_TABLE} q
            WHERE q.BATCH_ID = :batch_id AND q.MAJ_CAT = src.MAJ_CAT
        )
        OPTION (MAXDOP 4)
    """), params)
    conn.commit()
    inserted = int(res.rowcount or 0)

    # Total queue rows for this batch — what callers need for the
    # "anything to do?" check after a no-op retry seed.
    total = int(conn.execute(text(
        f"SELECT COUNT(*) FROM {QUEUE_TABLE} WHERE BATCH_ID = :b"
    ), {"b": batch_id}).scalar() or 0)

    logger.info(
        f"[queue] seeded batch_id={batch_id}: inserted={inserted} "
        f"total={total} in {_time.time() - t0:.1f}s"
    )
    return total


# ---------------------------------------------------------------------------
# Worker claim / report
# ---------------------------------------------------------------------------
def claim_next(conn, batch_id: str, worker_id: int) -> Optional[str]:
    """
    Atomically claim the next PENDING (or retry-eligible FAILED) MAJ_CAT
    in this batch. Returns the MAJ_CAT name, or None if the queue is
    exhausted. Concurrency-safe: UPDLOCK + READPAST guarantees no two
    workers see the same row.
    """
    row = conn.execute(text(f"""
        ;WITH NextItem AS (
            SELECT TOP 1 BATCH_ID, MAJ_CAT, STATUS, ATTEMPTS,
                         WORKER_ID, PICKED_AT
            FROM {QUEUE_TABLE} WITH (UPDLOCK, READPAST)
            WHERE BATCH_ID = :batch_id
              AND ( STATUS = 'PENDING'
                    OR (STATUS = 'FAILED' AND ATTEMPTS < :max_a) )
            ORDER BY
                CASE STATUS WHEN 'PENDING' THEN 0 ELSE 1 END,
                OPT_COUNT DESC
        )
        UPDATE NextItem
           SET STATUS    = 'IN_PROGRESS',
               WORKER_ID = :wid,
               PICKED_AT = GETDATE(),
               ATTEMPTS  = ATTEMPTS + 1
        OUTPUT inserted.MAJ_CAT
    """), {
        "batch_id": batch_id,
        "wid":      worker_id,
        "max_a":    MAX_ATTEMPTS,
    }).fetchone()
    conn.commit()
    return row[0] if row else None


def mark_in_progress(conn, batch_id: str, mc: str, worker_id: int) -> None:
    """
    Stamp a single MAJ_CAT row as IN_PROGRESS. Used by the ProcessPool path
    where each worker already has its target MAJ_CAT (no claim race) but
    we still want the dashboard to show the live `In progress` count.
    Idempotent — safe to call again on retry.
    """
    # Explicitly exclude CANCELLED — a subprocess worker that was already
    # mid-flight when the user clicked Cancel must NOT resurrect a cancelled
    # row by flipping it back to IN_PROGRESS. PENDING / FAILED stay claimable
    # for legitimate retries.
    conn.execute(text(f"""
        UPDATE {QUEUE_TABLE}
           SET STATUS    = 'IN_PROGRESS',
               WORKER_ID = :wid,
               PICKED_AT = GETDATE(),
               ATTEMPTS  = ISNULL(ATTEMPTS, 0) + 1
         WHERE BATCH_ID = :b AND MAJ_CAT = :mc
           AND STATUS IN ('PENDING','FAILED')
    """), {"b": batch_id, "mc": mc, "wid": int(worker_id)})
    conn.commit()


def mark_done(conn, batch_id: str, mc: str,
              ship: float, hold: float,
              rows_affected: int, duration_sec: float) -> None:
    """Mark a MAJ_CAT row DONE. **Refuses to overwrite STATUS='CANCELLED'**
    so a subprocess worker that finishes its in-flight MAJ_CAT after the
    user clicked Cancel can't resurrect the row by writing DONE on top."""
    conn.execute(text(f"""
        UPDATE {QUEUE_TABLE}
           SET STATUS         = 'DONE',
               COMPLETED_AT   = GETDATE(),
               SHIP_QTY       = :sh,
               HOLD_QTY       = :ho,
               ROWS_AFFECTED  = :r,
               DURATION_SEC   = :d,
               ERROR_MSG      = NULL
         WHERE BATCH_ID = :b AND MAJ_CAT = :mc
           AND STATUS <> 'CANCELLED'
    """), {
        "b": batch_id, "mc": mc,
        "sh": float(ship), "ho": float(hold),
        "r": int(rows_affected), "d": float(duration_sec),
    })
    conn.commit()


def mark_failed(conn, batch_id: str, mc: str,
                error: str, duration_sec: float) -> None:
    """Mark a MAJ_CAT row FAILED. **Refuses to overwrite STATUS='CANCELLED'**
    so a worker that errors after a user cancel doesn't downgrade the row's
    status from CANCELLED → FAILED (which would make it eligible for
    retry-failed and resurrect cancelled work)."""
    conn.execute(text(f"""
        UPDATE {QUEUE_TABLE}
           SET STATUS       = 'FAILED',
               COMPLETED_AT = GETDATE(),
               ERROR_MSG    = :e,
               DURATION_SEC = :d
         WHERE BATCH_ID = :b AND MAJ_CAT = :mc
           AND STATUS <> 'CANCELLED'
    """), {
        "b": batch_id, "mc": mc,
        "e": (error or "")[:2000],
        "d": float(duration_sec),
    })
    conn.commit()


# ---------------------------------------------------------------------------
# Read-side (UI / orchestrator)
# ---------------------------------------------------------------------------
def get_progress(conn, batch_id: str) -> Dict:
    """Counts grouped by STATUS for the given batch — for UI live poll."""
    rows = conn.execute(text(f"""
        SELECT STATUS, COUNT(*) FROM {QUEUE_TABLE}
        WHERE BATCH_ID = :b GROUP BY STATUS
    """), {"b": batch_id}).fetchall()
    counts = {r[0]: int(r[1]) for r in rows}
    total = sum(counts.values())
    done  = counts.get("DONE", 0)
    return {
        "batch_id":     batch_id,
        "total":        total,
        "pending":      counts.get("PENDING", 0),
        "in_progress":  counts.get("IN_PROGRESS", 0),
        "done":         done,
        "failed":       counts.get("FAILED", 0),
        "pct":          round(100.0 * done / total, 1) if total else 0.0,
    }


def get_failed_list(conn, batch_id: str) -> List[Dict]:
    rows = conn.execute(text(f"""
        SELECT MAJ_CAT, ATTEMPTS, ERROR_MSG, DURATION_SEC, COMPLETED_AT
        FROM {QUEUE_TABLE}
        WHERE BATCH_ID = :b AND STATUS = 'FAILED'
        ORDER BY MAJ_CAT
    """), {"b": batch_id}).fetchall()
    return [
        {
            "maj_cat":      r[0],
            "attempts":     int(r[1]) if r[1] is not None else 0,
            "error":        r[2],
            "duration_sec": float(r[3]) if r[3] is not None else None,
            "completed_at": r[4].isoformat() if r[4] else None,
        }
        for r in rows
    ]


def get_done_summary(conn, batch_id: str) -> Dict:
    """Final totals for a batch (after run completes)."""
    row = conn.execute(text(f"""
        SELECT
            ISNULL(SUM(SHIP_QTY),0)      AS ship_total,
            ISNULL(SUM(HOLD_QTY),0)      AS hold_total,
            ISNULL(SUM(ROWS_AFFECTED),0) AS rows_total,
            ISNULL(MAX(DURATION_SEC),0)  AS max_duration,
            ISNULL(SUM(DURATION_SEC),0)  AS sum_duration
        FROM {QUEUE_TABLE}
        WHERE BATCH_ID = :b AND STATUS = 'DONE'
    """), {"b": batch_id}).fetchone()
    return {
        "ship_total":   float(row[0] or 0),
        "hold_total":   float(row[1] or 0),
        "rows_total":   int(row[2] or 0),
        "max_duration": float(row[3] or 0),
        "sum_duration": float(row[4] or 0),
    }


# ---------------------------------------------------------------------------
# Manual retry support
# ---------------------------------------------------------------------------
def reset_failed_for_retry(conn, batch_id: str) -> int:
    """
    Move every FAILED row in the batch back to PENDING and **reset
    ATTEMPTS to 0** so the auto-retry budget (MAX_ATTEMPTS) is fully
    restored. Without the ATTEMPTS reset a manual retry that hits a
    fresh deadlock would be the row's last shot — `claim_next` would
    not pick it up again because of the `ATTEMPTS < :max_a` clause.

    Called by /listing/retry-failed. Returns the number of rows reset.
    """
    res = conn.execute(text(f"""
        UPDATE {QUEUE_TABLE}
           SET STATUS       = 'PENDING',
               WORKER_ID    = NULL,
               PICKED_AT    = NULL,
               COMPLETED_AT = NULL,
               ERROR_MSG    = NULL,
               ATTEMPTS     = 0
         WHERE BATCH_ID = :b AND STATUS = 'FAILED'
    """), {"b": batch_id})
    conn.commit()
    return int(res.rowcount or 0)


def list_recent_batches(conn, limit: int = 20) -> List[Dict]:
    """Used by UI to show recent runs."""
    rows = conn.execute(text(f"""
        SELECT TOP (:lim)
            BATCH_ID,
            MIN(CREATED_AT)              AS created_at,
            MAX(ALLOCATION_MODE)         AS mode,
            COUNT(*)                     AS total,
            SUM(CASE WHEN STATUS='DONE'    THEN 1 ELSE 0 END) AS done,
            SUM(CASE WHEN STATUS='FAILED'  THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN STATUS='PENDING' OR STATUS='IN_PROGRESS' THEN 1 ELSE 0 END) AS open
        FROM {QUEUE_TABLE}
        GROUP BY BATCH_ID
        ORDER BY MIN(CREATED_AT) DESC
    """), {"lim": int(limit)}).fetchall()
    return [
        {
            "batch_id":   r[0],
            "created_at": r[1].isoformat() if r[1] else None,
            "mode":       r[2],
            "total":      int(r[3] or 0),
            "done":       int(r[4] or 0),
            "failed":     int(r[5] or 0),
            "open":       int(r[6] or 0),
        }
        for r in rows
    ]
