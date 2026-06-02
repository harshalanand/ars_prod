"""
pend_alc_service.py
Manages the ARS_PEND_ALC table — tracks allocation quantities that are
approved but not yet Delivery-Order'd in SAP.

Lifecycle:
    approve_parked         → write_pend_alc         → PENDING rows (IS_CLOSED=0)
    Manual upload          → write_manual_pend_alc  → PENDING rows (SOURCE=MANUAL)
    BDC scheduled export   → stamp_bdc_qty          → BDC_QTY updated per row
    SAP DO upload back     → apply_do_deductions    → DO_QTY incremented; IS_CLOSED=1 when fully covered
    After PEND_ALC INSERTs → adjust_msa_after_pend_insert  → MSA FNL_Q/PEND_QTY refreshed for the affected keys
                              (called from manual upload + approve_parked. NOT called after DO updates —
                              STK_QTY in MSA is daily-snapshot only, refreshed by next full MSA run.)
    MSA run                → _load_ars_pending      → PEND_QTY deducted from available stock

Table grain: (SESSION_ID, RDC, ST_CD, ARTICLE_NUMBER, ALLOC_MODE)
  — one row per approved-session × source-warehouse × destination-store × variant-article × alloc-mode
  RDC   = source warehouse (where stock ships from); used by MSA for FNL_Q deduction
  ST_CD = destination store (WERKS in ARS_ALLOC_HISTORY); stored for traceability

PEND_QTY = ALLOC_QTY − DO_QTY  (persisted computed column)
BDC_QTY  = cumulative qty included in BDC files sent to SAP (audit only, not used by MSA)
"""
from __future__ import annotations

import uuid
from typing import Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import text


# Module-level latch flipped on by prewarm_pend_alc_tables(engine) at startup.
# Once True, ensure_*_table() short-circuits — schema/DDL work no longer runs
# inside user transactions, so /pend-alc/bdc-generate never takes a Sch-M
# lock during the hot path (which is what was blocking other sessions during
# "Generate BDC").
_TABLES_PREWARMED = False


def prewarm_pend_alc_tables(engine) -> None:
    """Run every ensure_*_table once at startup on a dedicated connection, then
    flip _TABLES_PREWARMED so per-request callers skip the DDL/INFORMATION_SCHEMA
    round-trips entirely.

    Call this from main.py's lifespan startup after enable_rcsi().
    """
    global _TABLES_PREWARMED
    # Reset latch so the ensure_* calls below actually run (they early-return
    # when _TABLES_PREWARMED is True).
    was_warm = _TABLES_PREWARMED
    _TABLES_PREWARMED = False
    try:
        with engine.connect() as conn:
            ensure_pend_alc_table(conn)
            ensure_bdc_history_table(conn)
            ensure_operations_table(conn)
        _TABLES_PREWARMED = True
        logger.info("[pend_alc] tables prewarmed — hot-path DDL disabled")
    except Exception as e:
        _TABLES_PREWARMED = was_warm
        logger.warning(f"[pend_alc] prewarm failed (will fall back to per-request ensure): {e}")


PEND_ALC_TABLE = "ARS_PEND_ALC"

_DDL = f"""
IF OBJECT_ID('dbo.{PEND_ALC_TABLE}','U') IS NULL
CREATE TABLE dbo.{PEND_ALC_TABLE} (
    ID             BIGINT IDENTITY(1,1),
    SESSION_ID     NVARCHAR(50)   NOT NULL,
    RDC            NVARCHAR(20)   NOT NULL,
    ST_CD          NVARCHAR(20)   NULL,
    ARTICLE_NUMBER NVARCHAR(30)   NOT NULL,
    MAJ_CAT        NVARCHAR(50)   NULL,
    GEN_ART_NUMBER NVARCHAR(30)   NULL,
    CLR            NVARCHAR(20)   NULL,
    ALLOC_MODE     NVARCHAR(10)   NOT NULL DEFAULT 'AUTO',
    SOURCE         NVARCHAR(20)   NOT NULL DEFAULT 'AUTO',
    ALLOC_QTY      FLOAT          NOT NULL DEFAULT 0,
    BDC_QTY        FLOAT          NOT NULL DEFAULT 0,
    DO_QTY         FLOAT          NOT NULL DEFAULT 0,
    PEND_QTY       AS (ALLOC_QTY - DO_QTY) PERSISTED,
    APPROVED_AT    DATETIME       NOT NULL DEFAULT GETDATE(),
    LAST_BDC_AT    DATETIME       NULL,
    DO_NUMBER      NVARCHAR(100)  NULL,
    DO_UPLOADED_AT DATETIME       NULL,
    LAST_DO_AT     DATETIME       NULL,
    IS_CLOSED      BIT            NOT NULL DEFAULT 0,
    REMARKS        NVARCHAR(500)  NULL,
    CONSTRAINT PK_ARS_PEND_ALC PRIMARY KEY (ID)
)
"""

_INDEXES = [
    ("IX_ARS_PEND_ALC_lookup",
     f"ON dbo.{PEND_ALC_TABLE} (RDC, ARTICLE_NUMBER, IS_CLOSED)"),
    ("IX_ARS_PEND_ALC_session",
     f"ON dbo.{PEND_ALC_TABLE} (SESSION_ID)"),
    ("IX_ARS_PEND_ALC_mode",
     f"ON dbo.{PEND_ALC_TABLE} (ALLOC_MODE, IS_CLOSED)"),
    ("IX_ARS_PEND_ALC_source",
     f"ON dbo.{PEND_ALC_TABLE} (SOURCE, IS_CLOSED)"),
    # Covering index for /pend-alc/bdc-generate aggregation + stamp_bdc_qty
    # JOIN. The aggregation walks (RDC, ST_CD, ARTICLE) for IS_CLOSED=0 and
    # only needs PEND_QTY, BDC_QTY, MAJ_CAT — so a covering index turns the
    # full scan into a seek and removes the table-lock escalation risk.
    ("IX_ARS_PEND_ALC_bdc_lookup",
     f"ON dbo.{PEND_ALC_TABLE} (IS_CLOSED, RDC, ST_CD, ARTICLE_NUMBER) "
     f"INCLUDE (PEND_QTY, BDC_QTY, MAJ_CAT, LAST_BDC_AT)"),
]

# ---------------------------------------------------------------------------
# ARS_BDC_HISTORY — audit trail of every BDC generation event
# Each row = one (RDC, ST_CD, ARTICLE) line within a BDC file send.
# Tracks: qty sent, allocation no, qty received via DO, current status.
# Used to answer "show me every BDC ever sent for this store/article and
# how much SAP confirmed".
# ---------------------------------------------------------------------------
BDC_HISTORY_TABLE = "ARS_BDC_HISTORY"

_BDC_HISTORY_DDL = f"""
IF OBJECT_ID('dbo.{BDC_HISTORY_TABLE}','U') IS NULL
CREATE TABLE dbo.{BDC_HISTORY_TABLE} (
    ID                BIGINT IDENTITY(1,1),
    BDC_DATE          DATETIME       NOT NULL DEFAULT GETDATE(),
    ALLOCATION_NUMBER NVARCHAR(50)   NOT NULL,
    RDC               NVARCHAR(20)   NOT NULL,
    ST_CD             NVARCHAR(20)   NULL,
    ARTICLE_NUMBER    NVARCHAR(30)   NOT NULL,
    MAJ_CAT           NVARCHAR(50)   NULL,
    BDC_QTY           FLOAT          NOT NULL DEFAULT 0,
    DO_RECEIVED       FLOAT          NOT NULL DEFAULT 0,
    STATUS            NVARCHAR(20)   NOT NULL DEFAULT 'OPEN',
    LAST_DO_AT        DATETIME       NULL,
    CREATED_BY        NVARCHAR(100)  NULL,
    CONSTRAINT PK_ARS_BDC_HISTORY PRIMARY KEY (ID)
)
"""

_BDC_HISTORY_INDEXES = [
    ("IX_ARS_BDC_HISTORY_alloc",
     f"ON dbo.{BDC_HISTORY_TABLE} (ALLOCATION_NUMBER)"),
    ("IX_ARS_BDC_HISTORY_lookup",
     f"ON dbo.{BDC_HISTORY_TABLE} (RDC, ST_CD, ARTICLE_NUMBER)"),
    ("IX_ARS_BDC_HISTORY_status",
     f"ON dbo.{BDC_HISTORY_TABLE} (STATUS, BDC_DATE)"),
]


def ensure_bdc_history_table(conn) -> None:
    """Idempotent: create ARS_BDC_HISTORY if missing + ensure indexes."""
    if _TABLES_PREWARMED:
        return  # hot-path skip — startup already verified the schema
    conn.execute(text(_BDC_HISTORY_DDL))
    for idx_name, idx_def in _BDC_HISTORY_INDEXES:
        conn.execute(text(f"""
            IF NOT EXISTS (
                SELECT 1 FROM sys.indexes
                WHERE name = '{idx_name}'
                  AND object_id = OBJECT_ID('dbo.{BDC_HISTORY_TABLE}')
            )
            CREATE INDEX {idx_name} {idx_def}
        """))
    conn.commit()


# ---------------------------------------------------------------------------
# ARS_PEND_ALC_OPERATIONS — audit log for soft-revert support.
# Every write to ARS_PEND_ALC (BDC stamp, DO upload, manual upload) creates
# one row here with a JSON payload describing the exact deltas. A revert
# replays the deltas in reverse without touching the operation row itself —
# the row's REVERTED_AT field is set so the audit trail is preserved.
# ---------------------------------------------------------------------------
OPERATIONS_TABLE = "ARS_PEND_ALC_OPERATIONS"

_OPERATIONS_DDL = f"""
IF OBJECT_ID('dbo.{OPERATIONS_TABLE}','U') IS NULL
CREATE TABLE dbo.{OPERATIONS_TABLE} (
    OP_ID         BIGINT IDENTITY(1,1),
    OP_TYPE       NVARCHAR(20)   NOT NULL,   -- 'BDC' / 'DO' / 'MANUAL'
    OP_KEY        NVARCHAR(100)  NOT NULL,   -- ALLOCATION_NUMBER / SESSION_ID / DO batch UUID
    OP_DATE       DATETIME       NOT NULL DEFAULT GETDATE(),
    CREATED_BY    NVARCHAR(100)  NULL,
    SUMMARY       NVARCHAR(500)  NULL,       -- one-line human description for the UI
    ROWS_AFFECTED INT            NOT NULL DEFAULT 0,
    QTY_TOTAL     FLOAT          NOT NULL DEFAULT 0,
    PAYLOAD       NVARCHAR(MAX)  NOT NULL,   -- JSON deltas needed to revert
    REVERTED_AT   DATETIME       NULL,
    REVERTED_BY   NVARCHAR(100)  NULL,
    REVERT_NOTE   NVARCHAR(500)  NULL,
    CONSTRAINT PK_ARS_PEND_ALC_OPERATIONS PRIMARY KEY (OP_ID)
)
"""

_OPERATIONS_INDEXES = [
    ("IX_ARS_PEND_ALC_OPS_type_date",
     f"ON dbo.{OPERATIONS_TABLE} (OP_TYPE, OP_DATE DESC)"),
    ("IX_ARS_PEND_ALC_OPS_key",
     f"ON dbo.{OPERATIONS_TABLE} (OP_KEY)"),
    ("IX_ARS_PEND_ALC_OPS_active",
     f"ON dbo.{OPERATIONS_TABLE} (REVERTED_AT) WHERE REVERTED_AT IS NULL"),
]


def ensure_operations_table(conn) -> None:
    """Idempotent: create ARS_PEND_ALC_OPERATIONS + indexes."""
    if _TABLES_PREWARMED:
        return  # hot-path skip — startup already verified the schema
    conn.execute(text(_OPERATIONS_DDL))
    for idx_name, idx_def in _OPERATIONS_INDEXES:
        conn.execute(text(f"""
            IF NOT EXISTS (
                SELECT 1 FROM sys.indexes
                WHERE name = '{idx_name}'
                  AND object_id = OBJECT_ID('dbo.{OPERATIONS_TABLE}')
            )
            CREATE INDEX {idx_name} {idx_def}
        """))
    conn.commit()


def log_operation(
    conn, op_type: str, op_key: str, payload: Dict,
    summary: str = "", rows_affected: int = 0, qty_total: float = 0,
    created_by: Optional[str] = None,
) -> int:
    """Insert one operation row + return OP_ID. Caller must commit."""
    import json as _json
    ensure_operations_table(conn)
    res = conn.execute(text(f"""
        INSERT INTO {OPERATIONS_TABLE}
            (OP_TYPE, OP_KEY, CREATED_BY, SUMMARY,
             ROWS_AFFECTED, QTY_TOTAL, PAYLOAD)
        OUTPUT INSERTED.OP_ID
        VALUES (:t, :k, :by, :s, :n, :q, :p)
    """), {
        "t":  op_type, "k": str(op_key)[:100], "by": created_by,
        "s":  (summary or "")[:500],
        "n":  int(rows_affected or 0),
        "q":  float(qty_total or 0),
        "p":  _json.dumps(payload, default=str),
    })
    op_id = res.scalar()
    conn.commit()
    return int(op_id)


def log_operation_upsert(
    conn, op_type: str, op_key: str, payload: Dict,
    summary: str = "", rows_affected: int = 0, qty_total: float = 0,
    created_by: Optional[str] = None, is_first: bool = True,
    merge_payload_lists: Optional[List[str]] = None,
) -> int:
    """Multi-chunk-aware variant of log_operation.

    - is_first=True  → INSERT a brand-new ops_log row (chunk 1 of an upload).
    - is_first=False → UPDATE the existing row identified by (op_type, op_key),
      accumulating rows_affected and qty_total. Used for chunks 2..N so the
      whole upload appears as ONE entry in the ops log.

    merge_payload_lists: list of payload field names whose values are lists
      that should be APPENDED to the chunk-1 payload (read-modify-write).
      Used for DO uploads where pend_updates / history_updates accumulate
      across chunks and must all be present for revert to work end-to-end.
      Omit (or pass None) for callers like manual-upload that revert via
      a SESSION_ID query and don't need the per-chunk payload preserved.

    If is_first=False but no existing row is found (caller error or upstream
    failure on chunk 1), falls through to a regular INSERT so we never lose
    the audit trail.

    Returns the OP_ID of the row (new or existing).
    """
    import json as _json
    ensure_operations_table(conn)

    if is_first:
        return log_operation(conn, op_type, op_key, payload, summary,
                             rows_affected, qty_total, created_by)

    if merge_payload_lists:
        # Read-modify-write so chunks 2..N append their list deltas to the
        # existing chunk-1 payload. Single ops_log row, complete revert.
        existing = conn.execute(text(f"""
            SELECT OP_ID, PAYLOAD FROM {OPERATIONS_TABLE}
             WHERE OP_TYPE = :t AND OP_KEY = :k
               AND ISNULL(REVERTED_AT, '') = ''
        """), {"t": op_type, "k": str(op_key)[:100]}).fetchone()

        if existing:
            op_id = int(existing[0])
            try:
                doc = _json.loads(existing[1]) if existing[1] else {}
            except Exception:
                doc = {}
            for field in merge_payload_lists:
                incoming = payload.get(field) or []
                if not incoming:
                    continue
                doc.setdefault(field, []).extend(incoming)
            conn.execute(text(f"""
                UPDATE {OPERATIONS_TABLE}
                   SET ROWS_AFFECTED = ISNULL(ROWS_AFFECTED, 0) + :n,
                       QTY_TOTAL     = ISNULL(QTY_TOTAL, 0)     + :q,
                       SUMMARY       = :s,
                       PAYLOAD       = :p
                 WHERE OP_ID = :id
            """), {
                "n":  int(rows_affected or 0),
                "q":  float(qty_total or 0),
                "s":  (summary or "")[:500],
                "p":  _json.dumps(doc, default=str),
                "id": op_id,
            })
            conn.commit()
            return op_id

        # Fallthrough to INSERT below
        logger.warning(
            f"[ops_log] upsert: no existing row for {op_type}/{op_key} on chunk N "
            f"(merge_payload mode); falling back to INSERT"
        )
        return log_operation(conn, op_type, op_key, payload, summary,
                             rows_affected, qty_total, created_by)

    res = conn.execute(text(f"""
        UPDATE {OPERATIONS_TABLE}
           SET ROWS_AFFECTED = ISNULL(ROWS_AFFECTED, 0) + :n,
               QTY_TOTAL     = ISNULL(QTY_TOTAL, 0)     + :q,
               SUMMARY       = :s
         OUTPUT INSERTED.OP_ID
         WHERE OP_TYPE = :t AND OP_KEY = :k AND ISNULL(REVERTED_AT, '') = ''
    """), {
        "n": int(rows_affected or 0),
        "q": float(qty_total or 0),
        "s": (summary or "")[:500],
        "t": op_type, "k": str(op_key)[:100],
    })
    rows = res.fetchall()
    conn.commit()
    if rows:
        return int(rows[0][0])

    # Fallback: existing row not found — recover by inserting a fresh row so
    # we don't lose the audit trail for this chunk.
    logger.warning(
        f"[ops_log] upsert: no existing row for {op_type}/{op_key} on chunk N; "
        f"falling back to INSERT"
    )
    return log_operation(conn, op_type, op_key, payload, summary,
                         rows_affected, qty_total, created_by)


def list_operations(
    conn, op_type: Optional[str] = None,
    include_reverted: bool = True, limit: int = 200,
) -> List[Dict]:
    """List recent operations for the UI."""
    ensure_operations_table(conn)
    where = []
    params = {"lim": limit}
    if op_type:
        where.append("OP_TYPE = :t"); params["t"] = op_type
    if not include_reverted:
        where.append("REVERTED_AT IS NULL")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(text(f"""
        SELECT TOP (:lim)
            OP_ID, OP_TYPE, OP_KEY, OP_DATE, CREATED_BY,
            SUMMARY, ROWS_AFFECTED, QTY_TOTAL,
            REVERTED_AT, REVERTED_BY, REVERT_NOTE
        FROM {OPERATIONS_TABLE} WITH (NOLOCK)
        {where_sql}
        ORDER BY OP_DATE DESC, OP_ID DESC
    """), params).fetchall()
    return [
        {
            "op_id":         int(r[0]),
            "op_type":       r[1],
            "op_key":        r[2],
            "op_date":       r[3].isoformat() if r[3] else None,
            "created_by":    r[4],
            "summary":       r[5],
            "rows_affected": int(r[6] or 0),
            "qty_total":     float(r[7] or 0),
            "reverted_at":   r[8].isoformat() if r[8] else None,
            "reverted_by":   r[9],
            "revert_note":   r[10],
        }
        for r in rows
    ]


def _load_operation(conn, op_id: int) -> Optional[Dict]:
    """Fetch one operation including its PAYLOAD as parsed JSON."""
    import json as _json
    row = conn.execute(text(f"""
        SELECT OP_ID, OP_TYPE, OP_KEY, OP_DATE, CREATED_BY,
               SUMMARY, ROWS_AFFECTED, QTY_TOTAL, PAYLOAD,
               REVERTED_AT, REVERTED_BY
        FROM {OPERATIONS_TABLE} WHERE OP_ID = :id
    """), {"id": op_id}).fetchone()
    if not row:
        return None
    return {
        "op_id":         int(row[0]),
        "op_type":       row[1],
        "op_key":        row[2],
        "op_date":       row[3],
        "created_by":    row[4],
        "summary":       row[5],
        "rows_affected": int(row[6] or 0),
        "qty_total":     float(row[7] or 0),
        "payload":       _json.loads(row[8]) if row[8] else {},
        "reverted_at":   row[9],
        "reverted_by":   row[10],
    }


# --- Revert dispatchers ----------------------------------------------------

def backfill_bdc_operations(conn, dry_run: bool = True) -> Dict:
    """Create ARS_PEND_ALC_OPERATIONS rows for BDCs generated before the
    operations-log feature existed.

    For every distinct ALLOCATION_NUMBER in ARS_BDC_HISTORY that has no
    matching op row (OP_TYPE='BDC'), build a synthetic op:
      - history_ids = all history rows for that allocation_number
      - stamped_rows = current PEND_ALC rows matching those (rdc, st_cd, art)
        that have BDC_QTY > 0 today. We assume `old_bdc_qty=0` and
        `old_last_bdc_at=null` (the legitimate pre-stamp state — anything
        else would have been a recursive event already in history).

    The synthetic op carries `"_backfilled": true` in its payload so audits
    can tell it apart from natively-logged ops.

    dry_run=True → only reports the count.
    """
    ensure_operations_table(conn)
    ensure_bdc_history_table(conn)

    # Allocation numbers needing backfill
    missing = conn.execute(text(f"""
        SELECT DISTINCT H.ALLOCATION_NUMBER
        FROM {BDC_HISTORY_TABLE} H
        WHERE NOT EXISTS (
            SELECT 1 FROM {OPERATIONS_TABLE} O
            WHERE O.OP_TYPE = 'BDC' AND O.OP_KEY = H.ALLOCATION_NUMBER
        )
        ORDER BY H.ALLOCATION_NUMBER
    """)).fetchall()
    found = len(missing)

    if dry_run or found == 0:
        return {"found": found, "ops_created": 0, "applied": False}

    import json as _json
    ops_created = 0

    # For each allocation_number needing backfill, do TWO bulk queries:
    #   1) all ARS_BDC_HISTORY rows (for history_ids + meta)
    #   2) all currently-stamped ARS_PEND_ALC rows joined to those keys
    # This replaces the previous N×row loop that ran 100k+ SELECTs serially.
    for (alloc_no,) in missing:
        # 1) History rows in one shot
        h_rows = conn.execute(text(f"""
            SELECT ID, RDC, ISNULL(ST_CD,'') AS ST_CD, ARTICLE_NUMBER,
                   BDC_QTY, BDC_DATE, CREATED_BY, DO_RECEIVED
            FROM {BDC_HISTORY_TABLE}
            WHERE ALLOCATION_NUMBER = :a
        """), {"a": alloc_no}).fetchall()
        if not h_rows:
            continue

        history_ids = [int(h[0]) for h in h_rows]
        bdc_date    = h_rows[0][5]
        created_by  = h_rows[0][6]
        total_qty   = sum(float(h[4] or 0) for h in h_rows)
        any_do      = any(float(h[7] or 0) > 0 for h in h_rows)

        # 2) Currently-stamped PEND_ALC rows that match the (rdc, st_cd, art)
        #    keys from this allocation. Done in ONE query via a temp-table
        #    JOIN — handles 100k+ keys efficiently.
        tmp = f"#bf_keys_{uuid.uuid4().hex[:8]}"
        conn.execute(text(
            f"CREATE TABLE {tmp} (rdc NVARCHAR(20), st_cd NVARCHAR(20), art NVARCHAR(30))"
        ))
        # Bulk insert keys
        conn.execute(
            text(f"INSERT INTO {tmp} VALUES (:r, :s, :a)"),
            [{"r": h[1], "s": h[2] or "", "a": h[3]} for h in h_rows]
        )
        p_rows = conn.execute(text(f"""
            SELECT P.ID, P.BDC_QTY
            FROM {PEND_ALC_TABLE} P
            JOIN {tmp} k
              ON P.RDC = k.rdc
             AND P.ARTICLE_NUMBER = k.art
             AND ISNULL(P.ST_CD,'') = k.st_cd
            WHERE P.BDC_QTY > 0
        """)).fetchall()
        try:
            conn.execute(text(
                f"IF OBJECT_ID('tempdb..{tmp}') IS NOT NULL DROP TABLE {tmp}"
            ))
        except Exception:
            pass

        stamped_rows = [
            {"pend_alc_id": int(p[0]), "old_bdc_qty": 0,
             "old_last_bdc_at": None}
            for p in p_rows
        ]

        marker = " (DO already received)" if any_do else ""
        conn.execute(text(f"""
            INSERT INTO {OPERATIONS_TABLE}
                (OP_TYPE, OP_KEY, OP_DATE, CREATED_BY, SUMMARY,
                 ROWS_AFFECTED, QTY_TOTAL, PAYLOAD)
            VALUES ('BDC', :k, :d, :by, :s, :n, :q, :p)
        """), {
            "k":  str(alloc_no)[:100],
            "d":  bdc_date,
            "by": created_by,
            "s":  (f"BDC {alloc_no}: {len(h_rows)} lines, "
                   f"{int(total_qty)} units (backfilled){marker}")[:500],
            "n":  len(h_rows),
            "q":  total_qty,
            "p":  _json.dumps({
                "allocation_number":     alloc_no,
                "history_ids":           history_ids,
                "stamped_rows":          stamped_rows,
                "_backfilled":           True,
                "_had_do_at_backfill":   any_do,
            }),
        })
        ops_created += 1

    conn.commit()
    return {"found": found, "ops_created": ops_created, "applied": True}


def _check_bdc_revert(conn, op: Dict) -> List[str]:
    """Block BDC revert if any history row has DO_RECEIVED > 0."""
    errors = []
    history_ids = op["payload"].get("history_ids") or []
    if history_ids:
        rows = conn.execute(text(f"""
            SELECT ID, ALLOCATION_NUMBER, DO_RECEIVED, STATUS
            FROM {BDC_HISTORY_TABLE}
            WHERE ID IN ({','.join(str(int(x)) for x in history_ids)})
              AND DO_RECEIVED > 0
        """)).fetchall()
        for r in rows:
            errors.append(
                f"BDC history #{r[0]} (alloc {r[1]}) already has DO={r[2]:.0f} — "
                f"cannot revert without first reversing the DO upload"
            )
    return errors


def _check_do_revert(conn, op: Dict) -> List[str]:
    """Block DO revert if any affected PEND row has been touched by a later DO."""
    errors = []
    op_date = op["op_date"]
    pend_updates = op["payload"].get("pend_updates") or []
    pend_ids = [u["pend_alc_id"] for u in pend_updates]
    if not pend_ids:
        return errors
    placeholders = ",".join(str(int(x)) for x in pend_ids)
    rows = conn.execute(text(f"""
        SELECT ID, LAST_DO_AT
        FROM {PEND_ALC_TABLE}
        WHERE ID IN ({placeholders}) AND LAST_DO_AT > :d
    """), {"d": op_date}).fetchall()
    if rows:
        errors.append(
            f"{len(rows)} row(s) have a newer DO event after this upload — "
            f"revert the newer DO upload first to keep FIFO integrity"
        )
    return errors


def _check_manual_revert(conn, op: Dict) -> List[str]:
    """Block MANUAL revert if any inserted row already has BDC_QTY or DO_QTY."""
    errors = []
    inserted_ids = op["payload"].get("inserted_ids") or []
    if not inserted_ids:
        return errors
    placeholders = ",".join(str(int(x)) for x in inserted_ids)
    rows = conn.execute(text(f"""
        SELECT ID, BDC_QTY, DO_QTY
        FROM {PEND_ALC_TABLE}
        WHERE ID IN ({placeholders})
          AND (BDC_QTY > 0 OR DO_QTY > 0)
    """)).fetchall()
    for r in rows:
        errors.append(
            f"Row #{r[0]} has BDC_QTY={r[1]:.0f} DO_QTY={r[2]:.0f} — "
            f"this manual upload was already actioned, revert BDC/DO first"
        )
    return errors


def preview_revert(conn, op_id: int) -> Dict:
    """Dry-run: returns what would change + any safety errors."""
    op = _load_operation(conn, op_id)
    if not op:
        return {"error": f"Operation {op_id} not found"}
    if op["reverted_at"]:
        return {"error": f"Already reverted at {op['reverted_at'].isoformat()}"}

    errors: List[str] = []
    if op["op_type"] == "BDC":
        errors = _check_bdc_revert(conn, op)
    elif op["op_type"] == "DO":
        errors = _check_do_revert(conn, op)
    elif op["op_type"] == "MANUAL":
        errors = _check_manual_revert(conn, op)
    else:
        errors = [f"Unknown op_type: {op['op_type']}"]

    return {
        "op_id":         op_id,
        "op_type":       op["op_type"],
        "op_key":        op["op_key"],
        "op_date":       op["op_date"].isoformat() if op["op_date"] else None,
        "summary":       op["summary"],
        "rows_affected": op["rows_affected"],
        "qty_total":     op["qty_total"],
        "blockers":      errors,
        "can_revert":    len(errors) == 0,
    }


def revert_operation(
    conn, op_id: int, reverted_by: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict:
    """Apply the reverse, mark op as reverted. Idempotent — already-reverted
    ops return an error instead of being applied twice."""
    op = _load_operation(conn, op_id)
    if not op:
        return {"success": False, "error": f"Operation {op_id} not found"}
    if op["reverted_at"]:
        return {"success": False, "error": "Already reverted"}

    # Re-check safety
    if op["op_type"] == "BDC":
        errors = _check_bdc_revert(conn, op)
    elif op["op_type"] == "DO":
        errors = _check_do_revert(conn, op)
    elif op["op_type"] == "MANUAL":
        errors = _check_manual_revert(conn, op)
    else:
        return {"success": False, "error": f"Unknown op_type: {op['op_type']}"}
    if errors:
        return {"success": False, "error": "; ".join(errors)}

    # Apply the reverse
    payload = op["payload"]
    if op["op_type"] == "BDC":
        result = _revert_bdc(conn, payload)
    elif op["op_type"] == "DO":
        result = _revert_do(conn, payload)
    elif op["op_type"] == "MANUAL":
        result = _revert_manual(conn, payload)

    # Stamp the audit fields on the op row
    conn.execute(text(f"""
        UPDATE {OPERATIONS_TABLE}
           SET REVERTED_AT = GETDATE(),
               REVERTED_BY = :by,
               REVERT_NOTE = :note
         WHERE OP_ID = :id
    """), {"by": reverted_by, "note": (note or "")[:500], "id": op_id})
    conn.commit()

    # Re-sync grid + MSA PEND_ALC/PEND_QTY from current ARS_PEND_ALC.
    # This corrects any residual mismatch in grid rows whose +1 was applied
    # by an older code revision (with a narrower hierarchy resolver) and
    # whose -1 with the current resolver can't perfectly undo. The bootstrap
    # rewrites PEND_ALC = SUM(open PA.PEND_QTY) per grain, which is the
    # canonical state regardless of upload/revert history.
    try:
        bg = bootstrap_grid_pend_sync(conn)
        result["grid_resync"] = bg
        logger.info(
            f"[revert] post-sync grids: var={bg.get('grid_var',0)} "
            f"gen={bg.get('grid_gen',0)} rollups={bg.get('grid_rollup',0)}"
        )
    except Exception as e:
        logger.warning(f"[revert] post-sync bootstrap_grid_pend_sync failed: {e}")
        result["grid_resync_error"] = str(e)

    try:
        bm = bootstrap_msa_pend_sync(conn)
        result["msa_resync"] = bm
    except Exception as e:
        logger.warning(f"[revert] post-sync bootstrap_msa_pend_sync failed: {e}")
        result["msa_resync_error"] = str(e)

    return {"success": True, **result}


def _revert_bdc(conn, payload: Dict) -> Dict:
    """Restore BDC_QTY/LAST_BDC_AT on stamped rows + delete history rows."""
    stamped = payload.get("stamped_rows") or []
    history_ids = payload.get("history_ids") or []

    rows_restored = 0
    for s in stamped:
        conn.execute(text(f"""
            UPDATE {PEND_ALC_TABLE}
               SET BDC_QTY     = :q,
                   LAST_BDC_AT = :t
             WHERE ID = :id
        """), {
            "q":  float(s.get("old_bdc_qty") or 0),
            "t":  s.get("old_last_bdc_at"),
            "id": int(s["pend_alc_id"]),
        })
        rows_restored += 1

    history_deleted = 0
    if history_ids:
        placeholders = ",".join(str(int(x)) for x in history_ids)
        res = conn.execute(text(
            f"DELETE FROM {BDC_HISTORY_TABLE} WHERE ID IN ({placeholders})"
        ))
        history_deleted = int(res.rowcount or 0)

    return {"pend_alc_rows_restored": rows_restored,
            "bdc_history_rows_deleted": history_deleted}


def _revert_do(conn, payload: Dict) -> Dict:
    """Subtract DO_QTY from PEND rows + restore IS_CLOSED + roll back history.

    Set-based: bulk-loads pend_updates / history_updates into temp tables,
    then runs one UPDATE..JOIN per side. Replaces the prior per-row loop
    (5K updates = 5K SQL round-trips → minutes on the same upload size as
    the apply path) with two bulk passes (sub-second).

    Restore semantics are IDENTICAL to the prior loop:
      • DO_QTY decremented by recorded qty_added.
      • IS_CLOSED reset to 0 only if the original apply just-closed the row.
      • LAST_DO_AT restored to the pre-apply value (NULL if it was NULL).
      • BDC_HISTORY.DO_RECEIVED / STATUS / LAST_DO_AT restored to the
        pre-apply values captured at apply time.
    """
    pend_updates = payload.get("pend_updates") or []
    history_updates = payload.get("history_updates") or []

    pend_rows = 0
    history_rows = 0

    if pend_updates:
        tmp_pa = f"#do_rev_pa_{uuid.uuid4().hex[:8]}"
        try:
            conn.execute(text(
                f"CREATE TABLE {tmp_pa} ("
                "  id          BIGINT  NOT NULL,"
                "  qty_added   FLOAT   NOT NULL,"
                "  was_closed  BIT     NOT NULL,"
                "  prev_do_at  DATETIME NULL"
                ")"
            ))
            raw = conn.connection
            cur = raw.cursor()
            try:
                try:
                    cur.fast_executemany = True
                except Exception:
                    pass
                cur.executemany(
                    f"INSERT INTO {tmp_pa} (id, qty_added, was_closed, prev_do_at) "
                    f"VALUES (?, ?, ?, ?)",
                    [(int(u["pend_alc_id"]),
                      float(u.get("qty_added") or 0),
                      1 if u.get("was_just_closed") else 0,
                      u.get("prev_last_do_at"))
                     for u in pend_updates],
                )
            finally:
                cur.close()

            conn.execute(text(f"""
                UPDATE P
                   SET P.DO_QTY     = P.DO_QTY - u.qty_added,
                       P.IS_CLOSED  = CASE WHEN u.was_closed = 1 THEN 0 ELSE P.IS_CLOSED END,
                       P.LAST_DO_AT = u.prev_do_at
                FROM {PEND_ALC_TABLE} P
                JOIN {tmp_pa} u ON P.ID = u.id
            """))
            pend_rows = len(pend_updates)
        finally:
            try:
                conn.execute(text(f"IF OBJECT_ID('tempdb..{tmp_pa}') IS NOT NULL DROP TABLE {tmp_pa}"))
            except Exception:
                pass

    if history_updates:
        tmp_h = f"#do_rev_h_{uuid.uuid4().hex[:8]}"
        try:
            conn.execute(text(
                f"CREATE TABLE {tmp_h} ("
                "  id      BIGINT       NOT NULL,"
                "  got     FLOAT        NOT NULL,"
                "  status  NVARCHAR(20) NOT NULL,"
                "  last_at DATETIME     NULL"
                ")"
            ))
            raw = conn.connection
            cur = raw.cursor()
            try:
                try:
                    cur.fast_executemany = True
                except Exception:
                    pass
                cur.executemany(
                    f"INSERT INTO {tmp_h} (id, got, status, last_at) "
                    f"VALUES (?, ?, ?, ?)",
                    [(int(h["history_id"]),
                      float(h.get("old_do_received") or 0),
                      (h.get("old_status") or "OPEN")[:20],
                      h.get("old_last_do_at"))
                     for h in history_updates],
                )
            finally:
                cur.close()

            conn.execute(text(f"""
                UPDATE H
                   SET H.DO_RECEIVED = u.got,
                       H.STATUS      = u.status,
                       H.LAST_DO_AT  = u.last_at
                FROM {BDC_HISTORY_TABLE} H
                JOIN {tmp_h} u ON H.ID = u.id
            """))
            history_rows = len(history_updates)
        finally:
            try:
                conn.execute(text(f"IF OBJECT_ID('tempdb..{tmp_h}') IS NOT NULL DROP TABLE {tmp_h}"))
            except Exception:
                pass

    return {"pend_alc_rows_reverted": pend_rows,
            "bdc_history_rows_reverted": history_rows}


def _revert_manual(conn, payload: Dict) -> Dict:
    """Delete every PEND_ALC row from the upload + apply symmetric -1 delta
    to MSA/Grid.

    Reverts by SESSION_ID (preferred) so multi-chunk uploads are undone in
    full — even though the ops_log payload only carries chunk 1's
    inserted_ids, the session_id covers every row from every chunk. Falls
    back to inserted_ids for legacy single-chunk entries that pre-date the
    session_id field.

    The delta function is symmetric — same rows × sign=-1 produces byte-for-
    byte the inverse of the +1 call that was made when the rows were
    originally inserted.
    """
    session_id = payload.get("session_id")
    inserted_ids = payload.get("inserted_ids") or []

    # Build the WHERE clause: prefer session_id (covers all chunks), fall
    # back to inserted_ids for older log entries that didn't carry session_id.
    if session_id:
        where_sql = "SESSION_ID = :sid"
        where_params = {"sid": session_id}
    elif inserted_ids:
        placeholders = ",".join(str(int(x)) for x in inserted_ids)
        where_sql = f"ID IN ({placeholders})"
        where_params = {}
    else:
        return {"pend_alc_rows_deleted": 0}

    # Read the rows BEFORE deleting so we can apply the -1 delta against
    # the same grain (RDC, ST_CD, ARTICLE, MAJ_CAT, GEN_ART, CLR, qty).
    rows_to_revert = [
        {
            "rdc":            r[0],
            "st_cd":          r[1],
            "article_number": r[2],
            "maj_cat":        r[3],
            "gen_art_number": r[4],
            "clr":            r[5],
            "alloc_qty":      float(r[6] or 0),
            "do_qty":         float(r[7] or 0),
        }
        for r in conn.execute(text(f"""
            SELECT RDC, ST_CD, ARTICLE_NUMBER, MAJ_CAT, GEN_ART_NUMBER, CLR,
                   ALLOC_QTY, ISNULL(DO_QTY, 0)
            FROM {PEND_ALC_TABLE}
            WHERE {where_sql}
        """), where_params).fetchall()
    ]

    res = conn.execute(text(
        f"DELETE FROM {PEND_ALC_TABLE} WHERE {where_sql}"
    ), where_params)
    deleted = int(res.rowcount or 0)

    if rows_to_revert:
        try:
            apply_pend_alc_delta(conn, rows_to_revert, sign=-1)
        except Exception as e:
            logger.warning(f"[revert] -1 delta skipped: {e}")

    logger.info(
        f"[revert] manual: deleted {deleted} rows "
        f"(by {'session_id=' + session_id if session_id else 'inserted_ids'})"
    )
    return {"pend_alc_rows_deleted": deleted}


# ---------------------------------------------------------------------------
# ARS_STORE_BDC_SCHEDULE — Mon-Sat schedule for BDC generation per store.
# Each store has 6 boolean columns: stores marked 1 for a given day will be
# picked up by the schedule-aware /bdc-generate endpoint when the user picks
# that day.  Sunday is intentionally absent.
# ---------------------------------------------------------------------------
SCHEDULE_TABLE = "ARS_STORE_BDC_SCHEDULE"

_SCHEDULE_DDL = f"""
IF OBJECT_ID('dbo.{SCHEDULE_TABLE}','U') IS NULL
CREATE TABLE dbo.{SCHEDULE_TABLE} (
    ST_CD       NVARCHAR(20)  NOT NULL,
    ST_NAME     NVARCHAR(100) NULL,
    MON         BIT           NOT NULL DEFAULT 0,
    TUE         BIT           NOT NULL DEFAULT 0,
    WED         BIT           NOT NULL DEFAULT 0,
    THU         BIT           NOT NULL DEFAULT 0,
    FRI         BIT           NOT NULL DEFAULT 0,
    SAT         BIT           NOT NULL DEFAULT 0,
    IS_ACTIVE   BIT           NOT NULL DEFAULT 1,
    UPDATED_AT  DATETIME      NOT NULL DEFAULT GETDATE(),
    UPDATED_BY  NVARCHAR(100) NULL,
    CONSTRAINT PK_ARS_STORE_BDC_SCHEDULE PRIMARY KEY (ST_CD)
)
"""

# weekday() returns 0=Mon ... 6=Sun. Use this to pick the column.
DOW_COLUMNS = ["MON", "TUE", "WED", "THU", "FRI", "SAT"]


def ensure_schedule_table(conn) -> None:
    """Idempotent: create ARS_STORE_BDC_SCHEDULE if missing."""
    conn.execute(text(_SCHEDULE_DDL))
    conn.commit()


def get_stores_for_date(conn, date_str: str) -> List[str]:
    """Return list of ST_CD scheduled to receive BDC for the given date.

    date_str: 'YYYY-MM-DD'. Sunday returns empty list (we don't run BDC on Sun).
    """
    import datetime as _dt
    try:
        d = _dt.date.fromisoformat(date_str)
    except Exception:
        return []
    dow = d.weekday()  # 0=Mon, 6=Sun
    if dow >= 6:
        return []
    col = DOW_COLUMNS[dow]
    ensure_schedule_table(conn)
    rows = conn.execute(text(f"""
        SELECT ST_CD FROM {SCHEDULE_TABLE}
        WHERE IS_ACTIVE = 1 AND [{col}] = 1
        ORDER BY ST_CD
    """)).fetchall()
    return [r[0] for r in rows]


STORE_MASTER_TABLE = "Master_ALC_INPUT_ST_MASTER"


def list_schedules(conn) -> List[Dict]:
    """Return all schedule rows joined with Master_ALC_INPUT_ST_MASTER.

    Each row carries a `master_status`:
      - 'OK'      : exists in both schedule + master
      - 'EXTRA'   : in schedule but not in master (manual entry / typo)
      - 'MISSING' : in master but no schedule row yet (needs config)

    Master fields (RDC, HUB, ST_STATUS, ST_NM) are also included when present.
    Sorting: HUB asc, then ST_CD asc.  Missing-from-master rows show last
    within each HUB group.
    """
    ensure_schedule_table(conn)

    # Detect master table; if absent fall back to schedule-only listing.
    has_master = conn.execute(text(f"""
        SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_NAME = '{STORE_MASTER_TABLE}'
    """)).scalar()

    if not has_master:
        rows = conn.execute(text(f"""
            SELECT ST_CD, ST_NAME, MON, TUE, WED, THU, FRI, SAT,
                   IS_ACTIVE, UPDATED_AT, UPDATED_BY,
                   NULL AS RDC, NULL AS HUB, NULL AS ST_STATUS,
                   NULL AS MASTER_NAME, 'OK' AS STATUS_FLAG
            FROM {SCHEDULE_TABLE}
            ORDER BY ST_CD
        """)).fetchall()
    else:
        # FULL OUTER JOIN — covers MISSING (in master, not in schedule),
        # EXTRA (in schedule, not in master), and OK (both).
        rows = conn.execute(text(f"""
            SELECT
                ISNULL(S.ST_CD, M.ST_CD)       AS ST_CD,
                S.ST_NAME, S.MON, S.TUE, S.WED, S.THU, S.FRI, S.SAT,
                S.IS_ACTIVE, S.UPDATED_AT, S.UPDATED_BY,
                M.RDC, M.HUB, M.ST_STATUS, M.ST_NM        AS MASTER_NAME,
                CASE
                    WHEN M.ST_CD IS NULL THEN 'EXTRA'
                    WHEN S.ST_CD IS NULL THEN 'MISSING'
                    ELSE 'OK'
                END                              AS STATUS_FLAG
            FROM {SCHEDULE_TABLE} S
            FULL OUTER JOIN dbo.{STORE_MASTER_TABLE} M
                ON S.ST_CD = M.ST_CD
            ORDER BY ISNULL(M.HUB, 'zzz_NO_HUB'),
                     ISNULL(S.ST_CD, M.ST_CD)
        """)).fetchall()

    return [
        {
            "st_cd":         r[0],
            "st_name":       r[1] or r[14],   # prefer schedule's name, fall back to master
            "mon":           bool(r[2]) if r[2] is not None else False,
            "tue":           bool(r[3]) if r[3] is not None else False,
            "wed":           bool(r[4]) if r[4] is not None else False,
            "thu":           bool(r[5]) if r[5] is not None else False,
            "fri":           bool(r[6]) if r[6] is not None else False,
            "sat":           bool(r[7]) if r[7] is not None else False,
            "is_active":     bool(r[8]) if r[8] is not None else True,
            "updated_at":    r[9].isoformat() if r[9] else None,
            "updated_by":    r[10],
            "rdc":           r[11],
            "hub":           r[12],
            "st_status":     r[13],
            "master_status": r[15],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# ARS_STORE_BDC_SCHEDULE_AUDIT — field-level audit log for the schedule.
# One row per (FIELD changed) per (store touched in a save). A single Save
# button click might write 24 stores × 4 changed fields = 96 audit rows,
# all sharing the same BATCH_ID so the UI can group them as one event.
# ---------------------------------------------------------------------------
SCHEDULE_AUDIT_TABLE = "ARS_STORE_BDC_SCHEDULE_AUDIT"

_SCHEDULE_AUDIT_DDL = f"""
IF OBJECT_ID('dbo.{SCHEDULE_AUDIT_TABLE}','U') IS NULL
CREATE TABLE dbo.{SCHEDULE_AUDIT_TABLE} (
    LOG_ID        BIGINT IDENTITY(1,1),
    CHANGE_TIME   DATETIME       NOT NULL DEFAULT GETDATE(),
    ST_CD         NVARCHAR(20)   NOT NULL,
    ACTION        NVARCHAR(10)   NOT NULL,   -- INSERT / UPDATE / DELETE
    SOURCE        NVARCHAR(20)   NOT NULL DEFAULT 'API',
    BATCH_ID      NVARCHAR(50)   NULL,
    USER_NAME     NVARCHAR(100)  NULL,
    FIELD         NVARCHAR(50)   NOT NULL,
    OLD_VALUE     NVARCHAR(100)  NULL,
    NEW_VALUE     NVARCHAR(100)  NULL,
    NOTE          NVARCHAR(500)  NULL,
    CONSTRAINT PK_ARS_STORE_BDC_SCHEDULE_AUDIT PRIMARY KEY (LOG_ID)
)
"""

_SCHEDULE_AUDIT_INDEXES = [
    ("IX_ARS_SCHED_AUDIT_time",
     f"ON dbo.{SCHEDULE_AUDIT_TABLE} (CHANGE_TIME DESC)"),
    ("IX_ARS_SCHED_AUDIT_st_cd",
     f"ON dbo.{SCHEDULE_AUDIT_TABLE} (ST_CD, CHANGE_TIME DESC)"),
    ("IX_ARS_SCHED_AUDIT_batch",
     f"ON dbo.{SCHEDULE_AUDIT_TABLE} (BATCH_ID)"),
    ("IX_ARS_SCHED_AUDIT_user",
     f"ON dbo.{SCHEDULE_AUDIT_TABLE} (USER_NAME, CHANGE_TIME DESC)"),
]


def ensure_schedule_audit_table(conn) -> None:
    """Idempotent: create ARS_STORE_BDC_SCHEDULE_AUDIT + indexes."""
    conn.execute(text(_SCHEDULE_AUDIT_DDL))
    for idx_name, idx_def in _SCHEDULE_AUDIT_INDEXES:
        conn.execute(text(f"""
            IF NOT EXISTS (
                SELECT 1 FROM sys.indexes
                WHERE name = '{idx_name}'
                  AND object_id = OBJECT_ID('dbo.{SCHEDULE_AUDIT_TABLE}')
            )
            CREATE INDEX {idx_name} {idx_def}
        """))
    conn.commit()


# Fields tracked by the audit log. Order matches the table.
_SCHED_AUDIT_FIELDS = [
    "ST_NAME", "MON", "TUE", "WED", "THU", "FRI", "SAT", "IS_ACTIVE",
]

def _sched_audit_value(v) -> Optional[str]:
    """Stringify a value for OLD_VALUE / NEW_VALUE storage."""
    if v is None: return None
    if isinstance(v, bool): return "1" if v else "0"
    return str(v)

def _insert_audit_rows(conn, audit_rows: List[Dict]) -> None:
    """Bulk-insert audit rows in one round-trip."""
    if not audit_rows:
        return
    conn.execute(text(f"""
        INSERT INTO {SCHEDULE_AUDIT_TABLE}
            (ST_CD, ACTION, SOURCE, BATCH_ID, USER_NAME,
             FIELD, OLD_VALUE, NEW_VALUE, NOTE)
        VALUES
            (:st_cd, :action, :source, :batch, :user,
             :field, :old, :new, :note)
    """), audit_rows)


def upsert_schedules(
    conn, rows: List[Dict],
    updated_by: Optional[str] = None,
    source: str = "API",
    note: Optional[str] = None,
) -> Dict:
    """Bulk upsert store schedules + write per-field audit rows.

    For each input row we SELECT the existing values, diff per field, and
    write one audit row per changed field. A single BATCH_ID groups all
    audit rows from one call so the UI can show "Bulk save: 96 changes".

    Returns:
        { touched: int, inserted: int, updated: int,
          batch_id: str, audit_rows_written: int }
    """
    if not rows:
        return {"touched": 0, "inserted": 0, "updated": 0,
                "batch_id": None, "audit_rows_written": 0}
    ensure_schedule_table(conn)
    ensure_schedule_audit_table(conn)

    batch_id = uuid.uuid4().hex[:12]
    audit_rows: List[Dict] = []
    touched = inserted = updated = 0

    for r in rows:
        st_cd = str(r.get("st_cd") or "").strip()
        if not st_cd:
            continue

        # Normalize incoming values
        new_vals = {
            "ST_NAME":   (r.get("st_name") or None),
            "MON":       1 if r.get("mon") else 0,
            "TUE":       1 if r.get("tue") else 0,
            "WED":       1 if r.get("wed") else 0,
            "THU":       1 if r.get("thu") else 0,
            "FRI":       1 if r.get("fri") else 0,
            "SAT":       1 if r.get("sat") else 0,
            "IS_ACTIVE": 0 if r.get("is_active") is False else 1,
        }

        # Read current state
        existing = conn.execute(text(f"""
            SELECT ST_NAME, MON, TUE, WED, THU, FRI, SAT, IS_ACTIVE
            FROM {SCHEDULE_TABLE} WHERE ST_CD = :st
        """), {"st": st_cd}).fetchone()

        action = "INSERT" if existing is None else "UPDATE"

        # Diff per field
        old_vals = {}
        if existing is not None:
            for i, f in enumerate(_SCHED_AUDIT_FIELDS):
                old_vals[f] = existing[i]
        for f in _SCHED_AUDIT_FIELDS:
            new_v = new_vals[f]
            old_v = old_vals.get(f)
            # Coerce bit columns from DB to int for compare
            if isinstance(old_v, bool):
                old_v = 1 if old_v else 0
            if action == "INSERT":
                # Skip logging "False/empty" defaults on insert — only log
                # actually-set fields so the audit trail isn't noisy.
                if new_v in (None, 0, "", False):
                    continue
            else:
                if old_v == new_v:
                    continue
            audit_rows.append({
                "st_cd":  st_cd,
                "action": action,
                "source": source,
                "batch":  batch_id,
                "user":   updated_by,
                "field":  f,
                "old":    _sched_audit_value(old_v),
                "new":    _sched_audit_value(new_v),
                "note":   (note or "")[:500] or None,
            })

        # Apply the upsert
        params = {
            "st_cd":   st_cd,
            "name":    new_vals["ST_NAME"],
            "mon":     new_vals["MON"],   "tue": new_vals["TUE"],
            "wed":     new_vals["WED"],   "thu": new_vals["THU"],
            "fri":     new_vals["FRI"],   "sat": new_vals["SAT"],
            "active":  new_vals["IS_ACTIVE"],
            "by":      updated_by,
        }
        conn.execute(text(f"""
            MERGE {SCHEDULE_TABLE} AS T
            USING (SELECT :st_cd AS ST_CD) AS S ON T.ST_CD = S.ST_CD
            WHEN MATCHED THEN UPDATE SET
                ST_NAME=:name, MON=:mon, TUE=:tue, WED=:wed,
                THU=:thu, FRI=:fri, SAT=:sat, IS_ACTIVE=:active,
                UPDATED_AT=GETDATE(), UPDATED_BY=:by
            WHEN NOT MATCHED THEN INSERT
                (ST_CD, ST_NAME, MON, TUE, WED, THU, FRI, SAT, IS_ACTIVE, UPDATED_BY)
                VALUES (:st_cd, :name, :mon, :tue, :wed, :thu, :fri, :sat, :active, :by);
        """), params)
        touched += 1
        if action == "INSERT": inserted += 1
        else:                  updated  += 1

    _insert_audit_rows(conn, audit_rows)
    conn.commit()

    return {
        "touched":            touched,
        "inserted":           inserted,
        "updated":            updated,
        "batch_id":           batch_id,
        "audit_rows_written": len(audit_rows),
    }


def delete_schedule(
    conn, st_cd: str,
    user: Optional[str] = None,
    source: str = "API",
    note: Optional[str] = None,
) -> Dict:
    """Hard-delete a schedule row + write per-field audit rows so the
    deleted state can be reconstructed later from the audit log."""
    ensure_schedule_table(conn)
    ensure_schedule_audit_table(conn)

    existing = conn.execute(text(f"""
        SELECT ST_NAME, MON, TUE, WED, THU, FRI, SAT, IS_ACTIVE
        FROM {SCHEDULE_TABLE} WHERE ST_CD = :s
    """), {"s": st_cd}).fetchone()

    if existing is None:
        return {"deleted": 0, "batch_id": None, "audit_rows_written": 0}

    batch_id = uuid.uuid4().hex[:12]
    audit_rows = []
    for i, f in enumerate(_SCHED_AUDIT_FIELDS):
        v = existing[i]
        if isinstance(v, bool): v = 1 if v else 0
        if v in (None, 0, ""):
            continue  # skip logging fields that were already at default
        audit_rows.append({
            "st_cd":  st_cd,
            "action": "DELETE",
            "source": source,
            "batch":  batch_id,
            "user":   user,
            "field":  f,
            "old":    _sched_audit_value(v),
            "new":    None,
            "note":   (note or "")[:500] or None,
        })

    res = conn.execute(text(
        f"DELETE FROM {SCHEDULE_TABLE} WHERE ST_CD = :s"
    ), {"s": st_cd})
    _insert_audit_rows(conn, audit_rows)
    conn.commit()
    return {
        "deleted":            int(res.rowcount or 0),
        "batch_id":           batch_id,
        "audit_rows_written": len(audit_rows),
    }


def list_schedule_audit(
    conn, st_cd: Optional[str] = None,
    user: Optional[str] = None,
    source: Optional[str] = None,    # CSV: 'UI,CSV_IMPORT'
    action: Optional[str] = None,    # CSV
    field:  Optional[str] = None,    # CSV
    batch_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    page: int = 1, page_size: int = 100,
    sort_by: str = "change_time", sort_dir: str = "desc",
) -> Dict:
    """Paged audit log query."""
    ensure_schedule_audit_table(conn)

    sortable = {
        "change_time": "CHANGE_TIME",
        "st_cd":       "ST_CD",
        "user":        "USER_NAME",
        "source":      "SOURCE",
        "action":      "ACTION",
        "field":       "FIELD",
        "batch_id":    "BATCH_ID",
    }
    order_col = sortable.get(sort_by, "CHANGE_TIME")
    order_dir = "ASC" if sort_dir == "asc" else "DESC"

    where = ["1=1"]
    params: dict = {}

    def _multi(col, csv, prefix):
        if not csv: return
        vals = [v.strip() for v in csv.split(",") if v.strip()]
        if not vals: return
        phs = ",".join(f":{prefix}{i}" for i in range(len(vals)))
        where.append(f"{col} IN ({phs})")
        for i, v in enumerate(vals):
            params[f"{prefix}{i}"] = v

    if st_cd:
        where.append("ST_CD = :st"); params["st"] = st_cd
    if user:
        where.append("USER_NAME LIKE :u"); params["u"] = f"%{user}%"
    if batch_id:
        where.append("BATCH_ID = :b"); params["b"] = batch_id
    if date_from:
        where.append("CHANGE_TIME >= :df"); params["df"] = date_from
    if date_to:
        where.append("CHANGE_TIME < DATEADD(day,1,:dt)"); params["dt"] = date_to
    _multi("SOURCE", source, "fsrc")
    _multi("ACTION", action, "fact")
    _multi("FIELD",  field,  "ffld")

    where_sql = "WHERE " + " AND ".join(where)
    offset = (page - 1) * page_size
    params["offset"] = offset
    params["psize"]  = page_size

    total = conn.execute(text(f"""
        SELECT COUNT(*) FROM {SCHEDULE_AUDIT_TABLE} {where_sql}
    """), params).scalar() or 0

    rows = conn.execute(text(f"""
        SELECT LOG_ID, CHANGE_TIME, ST_CD, ACTION, SOURCE, BATCH_ID,
               USER_NAME, FIELD, OLD_VALUE, NEW_VALUE, NOTE
        FROM {SCHEDULE_AUDIT_TABLE}
        {where_sql}
        ORDER BY {order_col} {order_dir}, LOG_ID DESC
        OFFSET :offset ROWS FETCH NEXT :psize ROWS ONLY
    """), params).fetchall()

    total_pages = max(1, (int(total) + page_size - 1) // page_size)

    return {
        "total_rows":  int(total),
        "page":        page,
        "page_size":   page_size,
        "total_pages": total_pages,
        "data": [
            {
                "log_id":      int(r[0]),
                "change_time": r[1].isoformat() if r[1] else None,
                "st_cd":       r[2],
                "action":      r[3],
                "source":      r[4],
                "batch_id":    r[5],
                "user":        r[6],
                "field":       r[7],
                "old_value":   r[8],
                "new_value":   r[9],
                "note":        r[10],
            }
            for r in rows
        ],
    }

# All non-computed columns that must exist; order matters for NOT NULL + DEFAULT
_ENSURE_COLS = [
    ("SESSION_ID",     "NVARCHAR(50)   NOT NULL DEFAULT ''"),
    ("RDC",            "NVARCHAR(20)   NOT NULL DEFAULT ''"),
    ("ST_CD",          "NVARCHAR(20)   NULL"),
    ("ARTICLE_NUMBER", "NVARCHAR(30)   NOT NULL DEFAULT ''"),
    ("MAJ_CAT",        "NVARCHAR(50)   NULL"),
    ("GEN_ART_NUMBER", "NVARCHAR(30)   NULL"),
    ("CLR",            "NVARCHAR(20)   NULL"),
    ("ALLOC_MODE",     "NVARCHAR(10)   NOT NULL DEFAULT 'AUTO'"),
    ("SOURCE",         "NVARCHAR(20)   NOT NULL DEFAULT 'AUTO'"),
    ("ALLOC_QTY",      "FLOAT          NOT NULL DEFAULT 0"),
    ("BDC_QTY",        "FLOAT          NOT NULL DEFAULT 0"),
    ("DO_QTY",         "FLOAT          NOT NULL DEFAULT 0"),
    ("APPROVED_AT",    "DATETIME       NOT NULL DEFAULT GETDATE()"),
    ("LAST_BDC_AT",    "DATETIME       NULL"),
    ("DO_NUMBER",      "NVARCHAR(100)  NULL"),
    ("DO_UPLOADED_AT", "DATETIME       NULL"),
    ("LAST_DO_AT",     "DATETIME       NULL"),
    ("IS_CLOSED",      "BIT            NOT NULL DEFAULT 0"),
    ("REMARKS",        "NVARCHAR(500)  NULL"),
]


def ensure_pend_alc_table(conn) -> None:
    """Idempotent: create ARS_PEND_ALC with full schema if missing, or add any
    missing columns to an existing table (handles old-schema upgrades)."""
    if _TABLES_PREWARMED:
        return  # hot-path skip — startup already verified the schema
    # 1. Create table if it doesn't exist at all
    conn.execute(text(_DDL))

    # 2. Add any missing non-computed columns
    existing = {
        str(r[0]).upper()
        for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            f"WHERE TABLE_NAME = '{PEND_ALC_TABLE}'"
        )).fetchall()
    }

    # 2a. Add ID IDENTITY column + PK if missing (legacy tables predate it)
    if "ID" not in existing:
        try:
            conn.execute(text(
                f"ALTER TABLE dbo.{PEND_ALC_TABLE} "
                f"ADD [ID] BIGINT IDENTITY(1,1) NOT NULL"
            ))
            logger.info("[pend_alc] ensure_table: added IDENTITY column ID")
            # Add PK only if no PK exists yet
            has_pk = conn.execute(text(f"""
                SELECT COUNT(*) FROM sys.key_constraints
                WHERE parent_object_id = OBJECT_ID('dbo.{PEND_ALC_TABLE}')
                  AND type = 'PK'
            """)).scalar() or 0
            if not has_pk:
                conn.execute(text(
                    f"ALTER TABLE dbo.{PEND_ALC_TABLE} "
                    f"ADD CONSTRAINT PK_{PEND_ALC_TABLE} PRIMARY KEY (ID)"
                ))
                logger.info(f"[pend_alc] ensure_table: added PK_{PEND_ALC_TABLE}")
            existing.add("ID")
        except Exception as e:
            logger.warning(f"[pend_alc] ensure_table: add ID failed: {e}")

    for col_name, col_def in _ENSURE_COLS:
        if col_name.upper() not in existing:
            try:
                conn.execute(text(
                    f"ALTER TABLE dbo.{PEND_ALC_TABLE} ADD [{col_name}] {col_def}"
                ))
                logger.info(f"[pend_alc] ensure_table: added column {col_name}")
            except Exception as e:
                logger.warning(f"[pend_alc] ensure_table: add {col_name} failed: {e}")

    # 3. Add PEND_QTY computed column only after ALLOC_QTY and DO_QTY exist
    if "PEND_QTY" not in existing:
        # Re-check existing after step 2 additions
        existing2 = {
            str(r[0]).upper()
            for r in conn.execute(text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                f"WHERE TABLE_NAME = '{PEND_ALC_TABLE}'"
            )).fetchall()
        }
        if "ALLOC_QTY" in existing2 and "DO_QTY" in existing2:
            try:
                conn.execute(text(
                    f"ALTER TABLE dbo.{PEND_ALC_TABLE} "
                    f"ADD [PEND_QTY] AS (ALLOC_QTY - DO_QTY) PERSISTED"
                ))
                logger.info("[pend_alc] ensure_table: added computed column PEND_QTY")
            except Exception as e:
                logger.warning(f"[pend_alc] ensure_table: add PEND_QTY failed: {e}")

    conn.commit()

    # 4. Create missing indexes
    for idx_name, idx_def in _INDEXES:
        try:
            conn.execute(text(
                f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='{idx_name}') "
                f"CREATE INDEX {idx_name} {idx_def}"
            ))
        except Exception as e:
            logger.warning(f"[pend_alc] index {idx_name}: {e}")
    conn.commit()


_ST_MASTER = "Master_ALC_INPUT_ST_MASTER"


def _probe_rdc_col(conn) -> Optional[str]:
    """Return the source-RDC column name on the store master, or None.

    The store master maps destination stores (ST_CD / WERKS) to their source
    warehouse (RDC).  The column name varies by deployment.
    """
    for candidate in ("RDC", "WAREHOUSE", "HUB", "WH_CD"):
        found = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = :t AND COLUMN_NAME = :c"
        ), {"t": _ST_MASTER, "c": candidate}).scalar() or 0
        if found:
            return candidate
    return None


def write_pend_alc(conn, session_id: str) -> int:
    """Insert approved ALLOC_QTY into ARS_PEND_ALC at grain
    (SESSION_ID, RDC, ST_CD, ARTICLE_NUMBER, ALLOC_MODE).

    WERKS in ARS_ALLOC_HISTORY is the DESTINATION store (ST_CD).  We join
    with the store master to resolve the SOURCE warehouse (RDC).  Both are
    stored so that:
      - ST_CD lets users see which destination store received the allocation.
      - RDC is used by MSA to aggregate PEND_QTY per source warehouse.

    One row is written per (SESSION, RDC, ST_CD, ARTICLE, ALLOC_MODE).
    Falls back to using WERKS as RDC if no store master RDC column is found.
    Idempotent via NOT EXISTS guard on (SESSION, RDC, ST_CD, ARTICLE, MODE).
    Returns count of rows inserted.
    """
    ensure_pend_alc_table(conn)

    # Probe whether ARS_LISTING_WORKING_HISTORY exists
    has_lwh = conn.execute(text(
        "SELECT CASE WHEN OBJECT_ID('dbo.ARS_LISTING_WORKING_HISTORY','U') IS NULL "
        "THEN 0 ELSE 1 END"
    )).scalar() or 0

    # Probe store master for source-RDC column (maps destination WERKS → source RDC)
    rdc_col = _probe_rdc_col(conn)
    if rdc_col:
        rdc_expr = f"ISNULL(M.[{rdc_col}], H.[WERKS])"
        st_join  = f"LEFT JOIN [{_ST_MASTER}] M ON M.[ST_CD] = H.[WERKS]"
        logger.info(f"[pend_alc] write_pend_alc: mapping WERKS → RDC via {_ST_MASTER}.{rdc_col}")
    else:
        rdc_expr = "H.[WERKS]"
        st_join  = ""
        logger.warning("[pend_alc] write_pend_alc: store master RDC col not found — "
                       "WERKS stored as RDC (MSA deduction may be inaccurate)")

    if has_lwh:
        sql = f"""
            INSERT INTO {PEND_ALC_TABLE}
                (SESSION_ID, RDC, ST_CD, ARTICLE_NUMBER, MAJ_CAT, GEN_ART_NUMBER, CLR,
                 ALLOC_QTY, ALLOC_MODE, SOURCE)
            SELECT :sid, src.RDC, src.ST_CD, src.VAR_ART, src.MAJ_CAT,
                   src.GEN_ART_NUMBER, src.CLR, src.ALLOC_QTY, src.ALLOC_MODE, 'AUTO'
            FROM (
                SELECT {rdc_expr}                         AS RDC,
                       H.[WERKS]                          AS ST_CD,
                       H.[VAR_ART],
                       MAX(H.[MAJ_CAT])                   AS MAJ_CAT,
                       H.[GEN_ART_NUMBER],
                       MAX(H.[CLR])                       AS CLR,
                       SUM(ISNULL(TRY_CAST(H.[ALLOC_QTY] AS FLOAT), 0)) AS ALLOC_QTY,
                       ISNULL(MAX(W.[OPT_TYPE]), 'AUTO')  AS ALLOC_MODE
                FROM [ARS_ALLOC_HISTORY] H
                {st_join}
                LEFT JOIN [ARS_LISTING_WORKING_HISTORY] W
                    ON  W.[SESSION_ID]                = H.[SESSION_ID]
                    AND W.[WERKS]                     = H.[WERKS]
                    AND ISNULL(W.[GEN_ART_NUMBER],'') = ISNULL(H.[GEN_ART_NUMBER],'')
                    AND ISNULL(W.[CLR],'')             = ISNULL(H.[CLR],'')
                WHERE H.[SESSION_ID] = :sid
                  AND ISNULL(TRY_CAST(H.[ALLOC_QTY] AS FLOAT), 0) > 0
                GROUP BY {rdc_expr}, H.[WERKS], H.[VAR_ART], H.[GEN_ART_NUMBER],
                         ISNULL(W.[OPT_TYPE], 'AUTO')
            ) src
            WHERE NOT EXISTS (
                SELECT 1 FROM {PEND_ALC_TABLE} P
                WHERE P.SESSION_ID     = :sid
                  AND P.RDC            = src.RDC
                  AND ISNULL(P.ST_CD,'') = ISNULL(src.ST_CD,'')
                  AND P.ARTICLE_NUMBER = src.VAR_ART
                  AND P.ALLOC_MODE     = src.ALLOC_MODE
            )
        """
    else:
        # Fallback: no working history — one row per (RDC, ST_CD, ARTICLE)
        sql = f"""
            INSERT INTO {PEND_ALC_TABLE}
                (SESSION_ID, RDC, ST_CD, ARTICLE_NUMBER, MAJ_CAT, GEN_ART_NUMBER, CLR,
                 ALLOC_QTY, ALLOC_MODE, SOURCE)
            SELECT :sid,
                   {rdc_expr}, H.[WERKS], H.[VAR_ART],
                   MAX(H.[MAJ_CAT]), MAX(H.[GEN_ART_NUMBER]), MAX(H.[CLR]),
                   SUM(ISNULL(TRY_CAST(H.[ALLOC_QTY] AS FLOAT), 0)),
                   'AUTO', 'AUTO'
            FROM [ARS_ALLOC_HISTORY] H
            {st_join}
            WHERE H.[SESSION_ID] = :sid
              AND ISNULL(TRY_CAST(H.[ALLOC_QTY] AS FLOAT), 0) > 0
              AND NOT EXISTS (
                  SELECT 1 FROM {PEND_ALC_TABLE} P
                  WHERE P.SESSION_ID       = :sid
                    AND P.RDC              = {rdc_expr}
                    AND ISNULL(P.ST_CD,'') = ISNULL(H.[WERKS],'')
                    AND P.ARTICLE_NUMBER   = H.[VAR_ART]
                    AND P.ALLOC_MODE       = 'AUTO'
              )
            GROUP BY {rdc_expr}, H.[WERKS], H.[VAR_ART]
        """

    res = conn.execute(text(sql), {"sid": session_id})
    conn.commit()
    inserted = int(res.rowcount or 0)
    logger.info(f"[pend_alc] write_pend_alc: {inserted} rows for session {session_id}")
    return inserted


def write_manual_pend_alc(
    conn,
    rows: List[Dict],
    session_id: Optional[str] = None,
) -> Dict:
    """Insert manually-uploaded allocation rows into ARS_PEND_ALC.

    Direct INSERT via fast_executemany — simple, fast, lock-light because
    pyodbc's fast_executemany binds the row array as one parameterised batch
    and the target lock is only held during the executemany call (~100-500 ms
    for 10K rows). No staging table, no MERGE.

    Args:
      rows:        list of row dicts (rdc, article_number, alloc_qty, ...).
      session_id:  if provided, all rows are tagged with this session_id
                   (multi-chunk uploads share one session_id and roll up to
                   one operations_log entry, which makes revert atomic).
                   If omitted, a fresh MANUAL-YYYYMMDD-<6hex> id is generated.

    Returns: {
        "inserted":     int,
        "session_id":   str,
        "inserted_ids": List[int],   # ids inserted by THIS call
    }
    """
    ensure_pend_alc_table(conn)
    valid = [r for r in rows if float(r.get("alloc_qty", 0) or 0) > 0]
    if not valid:
        return {"inserted": 0, "session_id": session_id or "", "inserted_ids": []}

    if not session_id:
        import datetime
        session_id = (
            f"MANUAL-{datetime.date.today().strftime('%Y%m%d')}-"
            f"{uuid.uuid4().hex[:6]}"
        )

    # Snapshot the highest existing ID for this session BEFORE insert so the
    # post-insert SELECT picks up only rows from this call (multi-chunk uploads
    # reuse the same session_id, and we don't want chunk N to claim chunk N-1's
    # ids).
    prev_max_id = conn.execute(text(
        f"SELECT ISNULL(MAX(ID), 0) FROM {PEND_ALC_TABLE} WHERE SESSION_ID = :sid"
    ), {"sid": session_id}).scalar() or 0

    # Build parameter tuples in fixed column order.
    params = [
        (
            session_id,
            str(r["rdc"]),
            (str(r.get("st_cd")).strip() if r.get("st_cd") else None) or None,
            str(r["article_number"]),
            r.get("maj_cat") or None,
            r.get("gen_art_number") or None,
            r.get("clr") or None,
            float(r["alloc_qty"]),
            r.get("remarks") or None,
        )
        for r in valid
    ]

    # ── Direct INSERT via fast_executemany.  Drop down to the raw pyodbc
    # cursor so we can flip on fast_executemany — SQLAlchemy executemany
    # without it sends one round-trip per row, which is 100x slower.
    raw = conn.connection
    cursor = raw.cursor()
    try:
        try:
            cursor.fast_executemany = True
        except Exception:
            pass

        sql = (
            f"INSERT INTO {PEND_ALC_TABLE} "
            "(SESSION_ID, RDC, ST_CD, ARTICLE_NUMBER, MAJ_CAT, GEN_ART_NUMBER, CLR,"
            " ALLOC_QTY, ALLOC_MODE, SOURCE, REMARKS) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'MANUAL', 'MANUAL', ?)"
        )

        # Internal chunk to keep the parameter array manageable in memory and
        # to avoid hitting any single-statement parameter cap. With
        # fast_executemany the lock on ARS_PEND_ALC is held only while one
        # batch flushes — sub-second for 10K rows.
        CHUNK = 10000
        inserted = 0
        for i in range(0, len(params), CHUNK):
            batch = params[i:i + CHUNK]
            cursor.executemany(sql, batch)
            inserted += len(batch)
            logger.info(
                f"[pend_alc] manual upload: {inserted}/{len(params)} rows inserted "
                f"(session_id={session_id})"
            )
    finally:
        cursor.close()

    conn.commit()

    # Read back ONLY the IDs inserted by this call (ID > prev_max_id).
    inserted_ids = [
        int(r[0]) for r in conn.execute(text(f"""
            SELECT ID FROM {PEND_ALC_TABLE}
            WHERE SESSION_ID = :sid AND ID > :prev
            ORDER BY ID
        """), {"sid": session_id, "prev": int(prev_max_id)}).fetchall()
    ]
    inserted = len(inserted_ids)

    logger.info(
        f"[pend_alc] manual upload complete: {inserted} rows inserted, "
        f"session_id={session_id}"
    )
    return {
        "inserted":     inserted,
        "session_id":   session_id,
        "inserted_ids": inserted_ids,
    }


def recover_orphan_bdc_stamps(conn, dry_run: bool = True) -> Dict:
    """Reset BDC_QTY=0 on rows that were stamped but have no matching
    ARS_BDC_HISTORY entry (orphans). These were stamped by the pre-fix
    `stamp_bdc_qty(...)` call when the code passed `None` (stamp-all)
    instead of scoped pairs.

    A row is an orphan if:
        BDC_QTY > 0
        AND IS_CLOSED = 0
        AND no ARS_BDC_HISTORY row exists for (RDC, ST_CD, ARTICLE_NUMBER)

    dry_run=True → returns {found, total_qty} without changes.
    dry_run=False → also performs the UPDATE.
    """
    ensure_pend_alc_table(conn)
    ensure_bdc_history_table(conn)

    summary = conn.execute(text(f"""
        SELECT COUNT(*), ISNULL(SUM(BDC_QTY), 0)
        FROM {PEND_ALC_TABLE} P
        WHERE P.BDC_QTY > 0 AND P.IS_CLOSED = 0
          AND NOT EXISTS (
              SELECT 1 FROM {BDC_HISTORY_TABLE} H
              WHERE H.RDC = P.RDC
                AND ISNULL(H.ST_CD,'') = ISNULL(P.ST_CD,'')
                AND H.ARTICLE_NUMBER = P.ARTICLE_NUMBER
          )
    """)).fetchone()
    found = int(summary[0] or 0)
    qty   = float(summary[1] or 0)

    out = {"found": found, "qty_to_clear": qty, "applied": False}
    if not dry_run and found > 0:
        res = conn.execute(text(f"""
            UPDATE P
               SET P.BDC_QTY    = 0,
                   P.LAST_BDC_AT = NULL
            FROM {PEND_ALC_TABLE} P
            WHERE P.BDC_QTY > 0 AND P.IS_CLOSED = 0
              AND NOT EXISTS (
                  SELECT 1 FROM {BDC_HISTORY_TABLE} H
                  WHERE H.RDC = P.RDC
                    AND ISNULL(H.ST_CD,'') = ISNULL(P.ST_CD,'')
                    AND H.ARTICLE_NUMBER = P.ARTICLE_NUMBER
              )
        """))
        conn.commit()
        out["applied"] = True
        out["rows_updated"] = int(res.rowcount or 0)
    return out


def insert_bdc_history(
    conn,
    allocation_number: str,
    rows: List[Dict],
    created_by: Optional[str] = None,
) -> List[int]:
    """Append one row per (RDC, ST_CD, ARTICLE) line in a BDC file to
    ARS_BDC_HISTORY. Each call = one BDC generation event.

    rows: list of dicts with keys rdc, st_cd, article_number, maj_cat, bdc_qty.
    Returns list of inserted IDs (used by the operations log to revert later).
    """
    if not rows:
        return []
    ensure_bdc_history_table(conn)
    alloc = str(allocation_number or "").strip()
    payload = [
        {
            "alloc": alloc,
            "rdc":   str(r.get("rdc") or "").strip(),
            "st_cd": str(r.get("st_cd") or "").strip() or None,
            "art":   str(r.get("article_number") or "").strip(),
            "mc":    (r.get("maj_cat") or None),
            "qty":   float(r.get("bdc_qty") or 0),
            "by":    created_by,
        }
        for r in rows
    ]
    conn.execute(text(f"""
        INSERT INTO {BDC_HISTORY_TABLE}
            (ALLOCATION_NUMBER, RDC, ST_CD, ARTICLE_NUMBER, MAJ_CAT,
             BDC_QTY, DO_RECEIVED, STATUS, CREATED_BY)
        VALUES (:alloc, :rdc, :st_cd, :art, :mc, :qty, 0, 'OPEN', :by)
    """), payload)
    conn.commit()
    # Read back the IDs we just inserted — allocation_number is unique per
    # BDC generation so this is safe.
    ids = conn.execute(text(f"""
        SELECT ID FROM {BDC_HISTORY_TABLE}
        WHERE ALLOCATION_NUMBER = :a ORDER BY ID
    """), {"a": alloc}).fetchall()
    return [int(r[0]) for r in ids]


def update_bdc_history_with_do(conn, do_rows: List[Dict]) -> int:
    """When a DO is uploaded, credit each DO line against the matching
    open BDC history row(s).

    Matching strategy per input row (UNCHANGED from prior behavior — same
    three-way routing, same FIFO ordering):
      1. If `allocation_number` is provided → match exact ALLOCATION_NUMBER
         + RDC + ART.
      2. Else if `st_cd` is provided → FIFO across open history rows for
         (RDC, ST_CD, ARTICLE).
      3. Else → FIFO across open history rows for (RDC, ARTICLE).

    Set-based implementation: input is bucketed by route, each bucket gets
    one windowed-FIFO UPDATE pass. Replaces the per-row Python loop
    (1 SELECT + N UPDATEs per input row → minutes on 5K rows) with three
    bulk passes (sub-second total).

    Updates DO_RECEIVED and flips STATUS to PARTIAL or CONFIRMED.

    do_rows: dicts with rdc, article_number, do_qty,
             optional st_cd, optional allocation_number.
    Returns: {touched, history_updates} — history_updates payload format
    is byte-for-byte identical to the prior implementation so _revert_do
    needs no changes.
    """
    if not do_rows:
        return {"touched": 0, "history_updates": []}
    ensure_bdc_history_table(conn)

    # Bucket input rows by routing path (alloc_no > st_cd > global).
    # Each bucket runs in its own set-based pass so the FIFO and matching
    # rules of the original per-row Python loop are preserved exactly.
    by_alloc: List[Dict]  = []  # alloc_no path
    by_store: List[Dict]  = []  # st_cd FIFO path
    by_global: List[Dict] = []  # global FIFO path

    for r in do_rows:
        rdc       = str(r.get("rdc") or "").strip()
        art       = str(r.get("article_number") or "").strip()
        st_cd     = (str(r.get("st_cd") or "").strip() or "")
        alloc_no  = (str(r.get("allocation_number") or "").strip() or "")
        qty       = float(r.get("do_qty") or 0)
        if not rdc or not art or qty <= 0:
            continue
        rec = {"rdc": rdc, "art": art, "st_cd": st_cd, "alloc_no": alloc_no, "qty": qty}
        if alloc_no:
            by_alloc.append(rec)
        elif st_cd:
            by_store.append(rec)
        else:
            by_global.append(rec)

    if not (by_alloc or by_store or by_global):
        return {"touched": 0, "history_updates": []}

    tmp_in  = f"#bdc_in_{uuid.uuid4().hex[:8]}"
    tmp_out = f"#bdc_out_{uuid.uuid4().hex[:8]}"
    history_updates: List[Dict] = []

    try:
        conn.execute(text(
            f"CREATE TABLE {tmp_in} ("
            "  bucket   NVARCHAR(10) NOT NULL,"   # 'alloc' / 'store' / 'global'
            "  rdc      NVARCHAR(20) NOT NULL,"
            "  st_cd    NVARCHAR(20) NOT NULL,"
            "  art      NVARCHAR(30) NOT NULL,"
            "  alloc_no NVARCHAR(50) NOT NULL,"
            "  qty      FLOAT        NOT NULL"
            ")"
        ))
        raw = conn.connection
        cur = raw.cursor()
        try:
            try:
                cur.fast_executemany = True
            except Exception:
                pass
            all_rows = (
                [("alloc",  r["rdc"], r["st_cd"], r["art"], r["alloc_no"], r["qty"]) for r in by_alloc]
              + [("store",  r["rdc"], r["st_cd"], r["art"], "",            r["qty"]) for r in by_store]
              + [("global", r["rdc"], "",         r["art"], "",            r["qty"]) for r in by_global]
            )
            cur.executemany(
                f"INSERT INTO {tmp_in} (bucket, rdc, st_cd, art, alloc_no, qty) "
                f"VALUES (?, ?, ?, ?, ?, ?)",
                all_rows,
            )
        finally:
            cur.close()

        conn.execute(text(
            f"CREATE TABLE {tmp_out} ("
            "  history_id      BIGINT       NOT NULL,"
            "  qty_added       FLOAT        NOT NULL,"
            "  old_do_received FLOAT        NOT NULL,"
            "  new_do_received FLOAT        NOT NULL,"
            "  old_status      NVARCHAR(20) NOT NULL,"
            "  new_status      NVARCHAR(20) NOT NULL,"
            "  old_last_do_at  DATETIME     NULL"
            ")"
        ))

        # Three passes — one per route. Each uses the same windowed
        # running-sum CTE to assign FIFO apply-qty across matching history
        # rows in a single UPDATE..JOIN.
        #
        # group_cols and join_pred MUST share the same key columns so each
        # open history row matches at most one agg row (otherwise an
        # UPDATE..JOIN can drop apply_qty silently when two agg rows hit
        # the same target).
        passes = [
            # (bucket_name, partition_cols, group_cols, join_pred)
            ("alloc",
             "H.ALLOCATION_NUMBER, H.RDC, H.ARTICLE_NUMBER",
             "rdc, art, alloc_no",
             "agg.alloc_no = o.ALLOCATION_NUMBER AND agg.rdc = o.RDC AND agg.art = o.ARTICLE_NUMBER"),
            ("store",
             "H.RDC, ISNULL(H.ST_CD,''), H.ARTICLE_NUMBER",
             "rdc, st_cd, art",
             "agg.rdc = o.RDC AND agg.art = o.ARTICLE_NUMBER AND agg.st_cd = ISNULL(o.ST_CD,'')"),
            ("global",
             "H.RDC, H.ARTICLE_NUMBER",
             "rdc, art",
             "agg.rdc = o.RDC AND agg.art = o.ARTICLE_NUMBER"),
        ]

        for bucket, partition_cols, group_cols, join_pred in passes:
            sql = f"""
            ;WITH agg AS (
                SELECT {group_cols}, SUM(qty) AS qty
                FROM {tmp_in}
                WHERE bucket = :bucket AND qty > 0
                GROUP BY {group_cols}
            ),
            open_ranked AS (
                SELECT H.ID,
                       H.BDC_QTY,
                       H.DO_RECEIVED,
                       H.STATUS,
                       H.LAST_DO_AT,
                       H.RDC, H.ST_CD, H.ARTICLE_NUMBER, H.ALLOCATION_NUMBER,
                       (H.BDC_QTY - H.DO_RECEIVED) AS need,
                       SUM(H.BDC_QTY - H.DO_RECEIVED) OVER (
                           PARTITION BY {partition_cols}
                           ORDER BY H.BDC_DATE, H.ID
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) AS cum_after,
                       ISNULL(SUM(H.BDC_QTY - H.DO_RECEIVED) OVER (
                           PARTITION BY {partition_cols}
                           ORDER BY H.BDC_DATE, H.ID
                           ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                       ), 0) AS cum_before
                FROM {BDC_HISTORY_TABLE} H
                WHERE H.STATUS <> 'CONFIRMED' AND (H.BDC_QTY - H.DO_RECEIVED) > 0
            ),
            plan AS (
                SELECT o.ID,
                       CASE
                           WHEN agg.qty <= o.cum_before THEN 0
                           WHEN agg.qty >= o.cum_after  THEN o.need
                           ELSE agg.qty - o.cum_before
                       END AS apply_qty,
                       o.BDC_QTY, o.DO_RECEIVED, o.STATUS, o.LAST_DO_AT
                FROM open_ranked o
                JOIN agg ON {join_pred}
            )
            UPDATE H
               SET H.DO_RECEIVED = H.DO_RECEIVED + plan.apply_qty,
                   H.STATUS      = CASE WHEN H.DO_RECEIVED + plan.apply_qty >= H.BDC_QTY
                                        THEN 'CONFIRMED' ELSE 'PARTIAL' END,
                   H.LAST_DO_AT  = GETDATE()
            OUTPUT INSERTED.ID,
                   INSERTED.DO_RECEIVED - DELETED.DO_RECEIVED AS qty_added,
                   DELETED.DO_RECEIVED  AS old_do_received,
                   INSERTED.DO_RECEIVED AS new_do_received,
                   ISNULL(DELETED.STATUS, 'OPEN')  AS old_status,
                   INSERTED.STATUS                 AS new_status,
                   DELETED.LAST_DO_AT              AS old_last_do_at
              INTO {tmp_out} (history_id, qty_added, old_do_received,
                              new_do_received, old_status, new_status,
                              old_last_do_at)
            FROM {BDC_HISTORY_TABLE} H
            JOIN plan ON H.ID = plan.ID
            WHERE plan.apply_qty > 0
            """
            conn.execute(text(sql), {"bucket": bucket})

        for r in conn.execute(text(
            f"SELECT history_id, qty_added, old_do_received, new_do_received, "
            f"old_status, new_status, old_last_do_at FROM {tmp_out}"
        )).fetchall():
            history_updates.append({
                "history_id":      int(r[0]),
                "qty_added":       float(r[1] or 0),
                "old_do_received": float(r[2] or 0),
                "new_do_received": float(r[3] or 0),
                "old_status":      r[4] or "OPEN",
                "new_status":      r[5] or "OPEN",
                "old_last_do_at":  r[6].isoformat() if r[6] else None,
            })
    except Exception:
        try:
            conn.execute(text(f"IF OBJECT_ID('tempdb..{tmp_in}')  IS NOT NULL DROP TABLE {tmp_in}"))
            conn.execute(text(f"IF OBJECT_ID('tempdb..{tmp_out}') IS NOT NULL DROP TABLE {tmp_out}"))
        except Exception:
            pass
        raise
    else:
        try:
            conn.execute(text(f"IF OBJECT_ID('tempdb..{tmp_in}')  IS NOT NULL DROP TABLE {tmp_in}"))
            conn.execute(text(f"IF OBJECT_ID('tempdb..{tmp_out}') IS NOT NULL DROP TABLE {tmp_out}"))
        except Exception:
            pass

    conn.commit()
    return {"touched": len(history_updates), "history_updates": history_updates}


def stamp_bdc_qty(
    conn, article_rdc_pairs: Optional[List[Dict]] = None,
) -> List[Dict]:
    """Set BDC_QTY = current PEND_QTY and LAST_BDC_AT = now for open rows.

    Returns a list of {pend_alc_id, old_bdc_qty, old_last_bdc_at} for every row
    touched — the caller persists this in the operations log so the BDC stamp
    can be reverted later. `new_bdc_qty` is not stored (it's recomputed at
    revert time from the live PEND_QTY) — that alone cuts the JSON payload by
    ~25 % when stamping hundreds of thousands of rows.

    article_rdc_pairs: list of dicts with rdc, article_number, optional st_cd.
        - If `st_cd` is provided, scoped to that exact destination store row.
        - If `st_cd` omitted, falls back to (RDC, ARTICLE) matching.
    If article_rdc_pairs is None, stamps ALL open rows globally.
    """
    ensure_pend_alc_table(conn)

    # First, capture current state of rows that WILL be stamped.  We use
    # OUTPUT INSERTED.* on the UPDATE so we get pre-image and post-image in
    # one round-trip.
    if article_rdc_pairs:
        tmp = f"#bdc_stamp_{uuid.uuid4().hex[:8]}"
        conn.execute(text(
            f"CREATE TABLE {tmp} "
            f"(rdc NVARCHAR(20), st_cd NVARCHAR(20), art NVARCHAR(30))"
        ))
        conn.execute(
            text(f"INSERT INTO {tmp} VALUES (:r, :s, :a)"),
            [{
                "r": str(p["rdc"]),
                "s": (str(p.get("st_cd") or "").strip() or ""),
                "a": str(p["article_number"]),
            } for p in article_rdc_pairs]
        )
        rows = conn.execute(text(f"""
            UPDATE P
               SET P.BDC_QTY    = P.PEND_QTY,
                   P.LAST_BDC_AT = GETDATE()
            OUTPUT INSERTED.ID, DELETED.BDC_QTY, DELETED.LAST_BDC_AT
            FROM {PEND_ALC_TABLE} P
            JOIN {tmp} u
              ON P.RDC = u.rdc
             AND P.ARTICLE_NUMBER = u.art
             AND (u.st_cd = '' OR ISNULL(P.ST_CD,'') = u.st_cd)
            WHERE P.IS_CLOSED = 0 AND P.PEND_QTY > 0
        """)).fetchall()
        try:
            conn.execute(text(
                f"IF OBJECT_ID('tempdb..{tmp}') IS NOT NULL DROP TABLE {tmp}"
            ))
        except Exception:
            pass
    else:
        rows = conn.execute(text(f"""
            UPDATE {PEND_ALC_TABLE}
               SET BDC_QTY     = PEND_QTY,
                   LAST_BDC_AT  = GETDATE()
            OUTPUT INSERTED.ID, DELETED.BDC_QTY, DELETED.LAST_BDC_AT
            WHERE IS_CLOSED = 0 AND PEND_QTY > 0
        """)).fetchall()

    conn.commit()
    return [
        {
            "pend_alc_id":     int(r[0]),
            "old_bdc_qty":     float(r[1] or 0),
            "old_last_bdc_at": r[2].isoformat() if r[2] else None,
        }
        for r in rows
    ]


def apply_do_deductions(conn, rows: List[Dict]) -> Dict:
    """Increment DO_QTY in ARS_PEND_ALC for each DO row using FIFO across
    multiple open session rows for the same (RDC, ST_CD, ARTICLE).

    Set-based implementation: input rows are bulk-loaded into a temp table,
    aggregated by (RDC, ST_CD, ARTICLE), then one UPDATE..JOIN uses a windowed
    running-sum CTE to assign the FIFO apply-qty across all open PEND_ALC
    rows in a single statement. Replaces the prior per-row Python loop
    (5K input rows = 10K+ round-trips → minutes) with one bulk pass
    (5K input rows = sub-second).

    Returns: {
        "touched":      int,         # rows actually updated
        "pend_updates": [             # per-row deltas for the operations log
            {"pend_alc_id", "qty_added", "was_just_closed",
             "prev_last_do_at"}
        ],
    }
    The caller writes `pend_updates` into the audit payload so a future
    revert can subtract those exact qtys from those exact rows.

    rows: list of dicts with keys rdc, article_number, do_qty.
          Optional: st_cd, do_number.

    FIFO and matching semantics are IDENTICAL to the prior per-row loop:
      • Open rows ordered by APPROVED_AT ASC, ID ASC.
      • st_cd-scoped input matches only rows with that ST_CD;
        empty-st_cd input matches any row regardless of ST_CD.
      • st_cd-scoped input is applied first (in its own pass), then
        empty-st_cd input absorbs residual capacity. This matches the
        original input-order behavior in every realistic CSV (templates
        always include st_cd uniformly).
    """
    valid = [r for r in rows if float(r.get("do_qty", 0) or 0) > 0]
    if not valid:
        return {"touched": 0, "pend_updates": []}

    ensure_pend_alc_table(conn)

    # Build input tuples with a stable sequence index so STRING_AGG can
    # rebuild DO_NUMBER concatenation in the original input order.
    input_rows = []
    for seq, r in enumerate(valid):
        input_rows.append({
            "seq":    seq,
            "rdc":    str(r["rdc"]).strip(),
            "st_cd":  (str(r.get("st_cd") or "").strip() or ""),
            "art":    str(r["article_number"]).strip(),
            "qty":    float(r["do_qty"]),
            "do_num": (str(r.get("do_number") or "").strip() or None),
        })

    tmp_in   = f"#do_in_{uuid.uuid4().hex[:8]}"
    tmp_out  = f"#do_out_{uuid.uuid4().hex[:8]}"
    pend_updates: List[Dict] = []
    touched = 0

    try:
        # Stage input rows. Use fast_executemany for the bulk load.
        conn.execute(text(
            f"CREATE TABLE {tmp_in} ("
            "  seq    INT          NOT NULL,"
            "  rdc    NVARCHAR(20) NOT NULL,"
            "  st_cd  NVARCHAR(20) NOT NULL,"   # '' means no-store-scope
            "  art    NVARCHAR(30) NOT NULL,"
            "  qty    FLOAT        NOT NULL,"
            "  do_num NVARCHAR(50) NULL"
            ")"
        ))
        raw = conn.connection
        cur = raw.cursor()
        try:
            try:
                cur.fast_executemany = True
            except Exception:
                pass
            cur.executemany(
                f"INSERT INTO {tmp_in} (seq, rdc, st_cd, art, qty, do_num) "
                f"VALUES (?, ?, ?, ?, ?, ?)",
                [(r["seq"], r["rdc"], r["st_cd"], r["art"], r["qty"], r["do_num"])
                 for r in input_rows],
            )
        finally:
            cur.close()

        # Output capture table for OUTPUT INSERTED/DELETED → pend_updates.
        conn.execute(text(
            f"CREATE TABLE {tmp_out} ("
            "  pend_alc_id     BIGINT  NOT NULL,"
            "  qty_added       FLOAT   NOT NULL,"
            "  was_just_closed BIT     NOT NULL,"
            "  prev_last_do_at DATETIME NULL"
            ")"
        ))

        # One pass per scope-bucket. Scoped (st_cd != '') first so it claims
        # its targeted PEND_ALC rows before empty-st_cd input can absorb them
        # — preserves the original input-order behavior in mixed CSVs.
        for scope in ("scoped", "global"):
            if scope == "scoped":
                scope_pred = "agg.st_cd <> ''"
                join_pred  = "agg.st_cd = ISNULL(P.ST_CD,'')"
            else:
                scope_pred = "agg.st_cd = ''"
                join_pred  = "1 = 1"

            # Aggregate input within the current scope by (rdc, st_cd, art).
            # do_numbers preserves input order via STRING_AGG WITHIN GROUP.
            sql = f"""
            ;WITH agg AS (
                SELECT rdc, st_cd, art,
                       SUM(qty) AS qty,
                       STRING_AGG(do_num, ', ') WITHIN GROUP (ORDER BY seq) AS do_numbers
                FROM {tmp_in}
                WHERE qty > 0
                GROUP BY rdc, st_cd, art
            ),
            open_ranked AS (
                SELECT P.ID,
                       P.ALLOC_QTY,
                       P.DO_QTY,
                       (P.ALLOC_QTY - P.DO_QTY) AS need,
                       SUM(P.ALLOC_QTY - P.DO_QTY) OVER (
                           PARTITION BY P.RDC, ISNULL(P.ST_CD,''), P.ARTICLE_NUMBER
                           ORDER BY P.APPROVED_AT, P.ID
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) AS cum_after,
                       ISNULL(SUM(P.ALLOC_QTY - P.DO_QTY) OVER (
                           PARTITION BY P.RDC, ISNULL(P.ST_CD,''), P.ARTICLE_NUMBER
                           ORDER BY P.APPROVED_AT, P.ID
                           ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                       ), 0) AS cum_before,
                       P.RDC, P.ST_CD, P.ARTICLE_NUMBER
                FROM {PEND_ALC_TABLE} P
                WHERE P.IS_CLOSED = 0 AND (P.ALLOC_QTY - P.DO_QTY) > 0
            ),
            plan AS (
                SELECT o.ID,
                       agg.do_numbers,
                       CASE
                           WHEN agg.qty <= o.cum_before THEN 0
                           WHEN agg.qty >= o.cum_after  THEN o.need
                           ELSE agg.qty - o.cum_before
                       END AS apply_qty
                FROM open_ranked o
                JOIN agg
                  ON agg.rdc = o.RDC
                 AND agg.art = o.ARTICLE_NUMBER
                 AND {join_pred}
                WHERE {scope_pred}
            )
            UPDATE P
               SET P.DO_QTY         = P.DO_QTY + plan.apply_qty,
                   P.IS_CLOSED      = CASE WHEN P.DO_QTY + plan.apply_qty >= P.ALLOC_QTY THEN 1 ELSE 0 END,
                   P.LAST_DO_AT     = GETDATE(),
                   P.DO_UPLOADED_AT = GETDATE(),
                   P.DO_NUMBER      = CASE
                       WHEN plan.do_numbers IS NULL OR plan.do_numbers = '' THEN P.DO_NUMBER
                       WHEN P.DO_NUMBER IS NULL OR P.DO_NUMBER = ''         THEN plan.do_numbers
                       ELSE P.DO_NUMBER + ', ' + plan.do_numbers
                   END
            OUTPUT INSERTED.ID,
                   INSERTED.DO_QTY - DELETED.DO_QTY AS qty_added,
                   CASE WHEN INSERTED.IS_CLOSED = 1 AND DELETED.IS_CLOSED = 0 THEN 1 ELSE 0 END AS was_just_closed,
                   DELETED.LAST_DO_AT AS prev_last_do_at
              INTO {tmp_out} (pend_alc_id, qty_added, was_just_closed, prev_last_do_at)
            FROM {PEND_ALC_TABLE} P
            JOIN plan ON P.ID = plan.ID
            WHERE plan.apply_qty > 0
            """
            conn.execute(text(sql))

        # Pull captured deltas for the audit payload.
        for r in conn.execute(text(
            f"SELECT pend_alc_id, qty_added, was_just_closed, prev_last_do_at "
            f"FROM {tmp_out}"
        )).fetchall():
            pend_updates.append({
                "pend_alc_id":     int(r[0]),
                "qty_added":       float(r[1] or 0),
                "was_just_closed": bool(r[2]),
                "prev_last_do_at": r[3].isoformat() if r[3] else None,
            })
        touched = len(pend_updates)

        # Hold tracking — set-based, same as before. Aggregate by (rdc, art)
        # so two CSV lines for the same article don't get double-applied to
        # the same HOLD row.
        conn.execute(text(f"""
            ;WITH hold_agg AS (
                SELECT rdc, art, SUM(qty) AS qty
                FROM {tmp_in}
                WHERE qty > 0
                GROUP BY rdc, art
            )
            UPDATE H
               SET H.HOLD_REM  = CASE WHEN H.HOLD_REM - u.qty < 0 THEN 0
                                       ELSE H.HOLD_REM - u.qty END,
                   H.IS_CLOSED = CASE WHEN H.HOLD_REM - u.qty <= 0 THEN 1 ELSE 0 END
            FROM ARS_NL_TBL_HOLD_TRACKING H
            JOIN hold_agg u ON H.WERKS = u.rdc AND H.VAR_ART = u.art
            WHERE H.IS_CLOSED = 0
        """))
    except Exception:
        # Drop temp tables, re-raise to bubble up to endpoint
        try:
            conn.execute(text(f"IF OBJECT_ID('tempdb..{tmp_in}')  IS NOT NULL DROP TABLE {tmp_in}"))
            conn.execute(text(f"IF OBJECT_ID('tempdb..{tmp_out}') IS NOT NULL DROP TABLE {tmp_out}"))
        except Exception:
            pass
        raise
    else:
        try:
            conn.execute(text(f"IF OBJECT_ID('tempdb..{tmp_in}')  IS NOT NULL DROP TABLE {tmp_in}"))
            conn.execute(text(f"IF OBJECT_ID('tempdb..{tmp_out}') IS NOT NULL DROP TABLE {tmp_out}"))
        except Exception:
            pass

    conn.commit()
    return {"touched": touched, "pend_updates": pend_updates}


def adjust_msa_after_pend_insert(
    conn,
    session_id: Optional[str] = None,
    article_rdc_pairs: Optional[List[Dict]] = None,
) -> Dict:
    """Adjust ARS_MSA_TOTAL/GEN_ART/VAR_ART after rows were INSERTED into
    ARS_PEND_ALC (manual upload, bulk CSV, or approve_parked).

    Called only after PEND_ALC INSERTs — NOT after DO updates. Reason:
      • A new pending allocation reduces what's freely available in WH, so
        FNL_Q and PEND_QTY should reflect that immediately.
      • A DO arrival physically depletes WH stock, but STK_QTY in MSA tables
        is a daily snapshot and is not refreshed until the next full MSA run.
        Touching FNL_Q after a DO without refreshing STK_QTY would
        over-state the available pool — so we leave MSA alone for DO and
        let the next-day full MSA reconcile.

    Strategy:
      1. Identify affected (RDC, VAR_ART) keys.
      2. Recompute PEND_QTY per key from ARS_PEND_ALC (IS_CLOSED=0 sum).
      3. UPDATE ARS_MSA_TOTAL: FNL_Q = max(STK_QTY - new_PEND - HOLD_QTY, 0).
         HOLD_QTY is already stored in the table from the last MSA run.
      4. Roll up FNL_Q to ARS_MSA_GEN_ART and ARS_MSA_VAR_ART.

    Returns dict with updated row counts per table.
    """
    result: Dict = {"msa_total": 0, "msa_gen_art": 0, "msa_var_art": 0, "error": None}

    try:
        # --- Build affected-articles temp table ---
        tmp = f"#patch_msa_{uuid.uuid4().hex[:8]}"
        conn.execute(text(
            f"CREATE TABLE {tmp} (rdc NVARCHAR(20), art NVARCHAR(30))"
        ))

        if session_id:
            conn.execute(text(f"""
                INSERT INTO {tmp} (rdc, art)
                SELECT DISTINCT RDC, ARTICLE_NUMBER
                FROM {PEND_ALC_TABLE}
                WHERE SESSION_ID = :sid
            """), {"sid": session_id})
        elif article_rdc_pairs:
            conn.execute(
                text(f"INSERT INTO {tmp} VALUES (:r, :a)"),
                [{"r": str(p["rdc"]), "a": str(p["article_number"])}
                 for p in article_rdc_pairs]
            )
        else:
            # Patch all open PEND rows
            conn.execute(text(f"""
                INSERT INTO {tmp} (rdc, art)
                SELECT DISTINCT RDC, ARTICLE_NUMBER
                FROM {PEND_ALC_TABLE} WHERE IS_CLOSED = 0
            """))

        # --- Check MSA tables exist ---
        for tbl in ("ARS_MSA_TOTAL", "ARS_MSA_GEN_ART", "ARS_MSA_VAR_ART"):
            exists = conn.execute(text(
                f"SELECT CASE WHEN OBJECT_ID('dbo.{tbl}','U') IS NULL THEN 0 ELSE 1 END"
            )).scalar() or 0
            if not exists:
                logger.info(f"[pend_alc] adjust_msa_after_pend_insert: {tbl} not found — skip")
                conn.execute(text(
                    f"IF OBJECT_ID('tempdb..{tmp}') IS NOT NULL DROP TABLE {tmp}"
                ))
                return result

        # --- Probe article column names (vary by deployment) ---
        def _col(table, *candidates):
            for c in candidates:
                found = conn.execute(text(
                    "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_NAME=:t AND COLUMN_NAME=:c"
                ), {"t": table, "c": c}).scalar() or 0
                if found:
                    return c
            return candidates[0]  # fallback

        # ARS_MSA_TOTAL article col (MSA pivot uses ARTICLE_NUMBER)
        total_art  = _col("ARS_MSA_TOTAL",   "ARTICLE_NUMBER", "VAR_ART", "ARTICLE")
        # ARS_MSA_VAR_ART article col
        var_art_c  = _col("ARS_MSA_VAR_ART", "VAR_ART", "ARTICLE_NUMBER", "ARTICLE")

        has_hold = conn.execute(text("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_NAME='ARS_MSA_TOTAL' AND COLUMN_NAME='HOLD_QTY'
        """)).scalar() or 0
        hold_expr = "ISNULL(T.HOLD_QTY, 0)" if has_hold else "0"

        logger.info(
            f"[pend_alc] adjust_msa_after_pend_insert: MSA_TOTAL.art={total_art} "
            f"VAR_ART.art={var_art_c}"
        )

        # --- 1. Update ARS_MSA_TOTAL ---
        r1 = conn.execute(text(f"""
            UPDATE T
               SET T.PEND_QTY = ISNULL(P.PEND_TOTAL, 0),
                   T.FNL_Q    = CASE
                       WHEN T.STK_QTY - ISNULL(P.PEND_TOTAL, 0) - {hold_expr} < 0
                       THEN 0
                       ELSE T.STK_QTY - ISNULL(P.PEND_TOTAL, 0) - {hold_expr}
                   END
            FROM ARS_MSA_TOTAL T
            JOIN {tmp} x ON T.RDC = x.rdc AND T.[{total_art}] = x.art
            LEFT JOIN (
                SELECT RDC, ARTICLE_NUMBER, SUM(PEND_QTY) AS PEND_TOTAL
                FROM {PEND_ALC_TABLE} WHERE IS_CLOSED = 0
                GROUP BY RDC, ARTICLE_NUMBER
            ) P ON T.RDC = P.RDC AND T.[{total_art}] = P.ARTICLE_NUMBER
        """))
        result["msa_total"] = int(r1.rowcount or 0)
        conn.commit()  # commit TOTAL update independently

        # --- 2. Roll up to ARS_MSA_VAR_ART ---
        try:
            r2 = conn.execute(text(f"""
                UPDATE V
                   SET V.FNL_Q    = agg.FNL_Q_SUM,
                       V.PEND_QTY = agg.PEND_SUM
                FROM ARS_MSA_VAR_ART V
                JOIN (
                    SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, [{total_art}] AS ART_KEY,
                           SUM(FNL_Q)    AS FNL_Q_SUM,
                           SUM(PEND_QTY) AS PEND_SUM
                    FROM ARS_MSA_TOTAL
                    WHERE [{total_art}] IN (SELECT art FROM {tmp})
                    GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, [{total_art}]
                ) agg ON V.RDC = agg.RDC
                     AND ISNULL(V.MAJ_CAT,'')        = ISNULL(agg.MAJ_CAT,'')
                     AND ISNULL(V.GEN_ART_NUMBER,'') = ISNULL(agg.GEN_ART_NUMBER,'')
                     AND ISNULL(V.CLR,'')             = ISNULL(agg.CLR,'')
                     AND V.[{var_art_c}]              = agg.ART_KEY
            """))
            result["msa_var_art"] = int(r2.rowcount or 0)
            conn.commit()
        except Exception as e2:
            logger.warning(f"[pend_alc] adjust_msa_after_pend_insert VAR_ART rollup skipped: {e2}")
            conn.rollback()

        # --- 3. Roll up to ARS_MSA_GEN_ART ---
        try:
            r3 = conn.execute(text(f"""
                UPDATE G
                   SET G.FNL_Q    = agg.FNL_Q_SUM,
                       G.PEND_QTY = agg.PEND_SUM
                FROM ARS_MSA_GEN_ART G
                JOIN (
                    SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR,
                           SUM(FNL_Q)    AS FNL_Q_SUM,
                           SUM(PEND_QTY) AS PEND_SUM
                    FROM ARS_MSA_TOTAL
                    WHERE [{total_art}] IN (SELECT art FROM {tmp})
                    GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR
                ) agg ON G.RDC = agg.RDC
                     AND ISNULL(G.MAJ_CAT,'')        = ISNULL(agg.MAJ_CAT,'')
                     AND ISNULL(G.GEN_ART_NUMBER,'') = ISNULL(agg.GEN_ART_NUMBER,'')
                     AND ISNULL(G.CLR,'')             = ISNULL(agg.CLR,'')
            """))
            result["msa_gen_art"] = int(r3.rowcount or 0)
            conn.commit()
        except Exception as e3:
            logger.warning(f"[pend_alc] adjust_msa_after_pend_insert GEN_ART rollup skipped: {e3}")
            conn.rollback()

        try:
            conn.execute(text(
                f"IF OBJECT_ID('tempdb..{tmp}') IS NOT NULL DROP TABLE {tmp}"
            ))
            conn.commit()
        except Exception:
            pass

        logger.info(
            f"[pend_alc] adjust_msa_after_pend_insert: total={result['msa_total']} "
            f"var={result['msa_var_art']} gen={result['msa_gen_art']}"
        )

    except Exception as e:
        logger.warning(f"[pend_alc] adjust_msa_after_pend_insert failed (non-fatal): {e}")
        result["error"] = str(e)

    return result


# ---------------------------------------------------------------------------
# PEND_ALC ↔ MSA/Grid synchronisation — symmetric incremental delta + bootstrap
# ---------------------------------------------------------------------------
#
# Why this exists
# ===============
# Every INSERT into ARS_PEND_ALC must immediately reduce FNL_Q (and increase
# PEND_QTY) on ARS_MSA_TOTAL/VAR_ART/GEN_ART, and increase PEND_ALC on each
# ARS_GRID_* table. Conversely, every revert/delete must undo the same delta.
# `apply_pend_alc_delta` does both — the same call with sign=+1 or sign=-1
# produces symmetric, reversible state changes.
#
# Bootstrap functions seed the same columns from a full ARS_PEND_ALC scan; a
# correctly-built bootstrap + every incremental delta = the same column values
# you'd get by running bootstrap from scratch.
#
# Invariants (enforced by these functions):
#   MSA.FNL_Q     = max(STK_QTY − PEND_QTY − HOLD_QTY, 0)
#   MSA.PEND_QTY  = SUM(ARS_PEND_ALC.PEND_QTY where IS_CLOSED=0)  per (RDC, ARTICLE)
#   GRID.PEND_ALC = SUM(ARS_PEND_ALC.PEND_QTY where IS_CLOSED=0)  per (WERKS, MAJ_CAT, ...)
#   GRID.STK_TTL  = unchanged by pend_alc (physical stock)
# ---------------------------------------------------------------------------


def _probe_col(conn, table: str, *candidates: str) -> str:
    """Return the first candidate column that exists on `table`. Falls back
    to the first candidate so callers always get a usable name. Used because
    article-column names vary across deployments (VAR_ART vs ARTICLE_NUMBER
    vs ARTICLE — see CLAUDE.md note about prior renames)."""
    for c in candidates:
        found = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = :t AND COLUMN_NAME = :c"
        ), {"t": table, "c": c}).scalar() or 0
        if found:
            return c
    return candidates[0]


# Attribute-rollup grids that aggregate PEND_ALC at (WERKS, MAJ_CAT) level.
# Each such table has columns (WERKS, MAJ_CAT, <attribute>, ..., PEND_ALC, ...).
# The set of grid tables changes whenever the grid builder runs (new attribute
# grids can be added or old ones dropped) so the list is discovered at runtime
# from INFORMATION_SCHEMA rather than hardcoded.
#
# Variant- and gen_art-grain grids (ARS_GRID_MJ_VAR_ART, ARS_GRID_MJ_GEN_ART)
# are excluded here — they have additional join keys (article / gen_art / clr)
# and are handled by their own dedicated UPDATE blocks.
# Grids excluded from the auto-discovered rollup loop. The article-grain
# grids ARS_GRID_MJ_VAR_ART and ARS_GRID_MJ_GEN_ART now flow through the
# same rollup builder (their article column is handled via delta_native),
# making the path fully dynamic — any future grid the user creates with
# columns from vw_master_product is auto-handled without code changes.
_EXCLUDED_FROM_ROLLUP = {
    "ARS_GRID_MJ_VND_CD",   # zombie grid (legacy, superseded by M_VND_CD)
}


def _discover_grid_rollup_tables(conn) -> List[str]:
    """Return every dbo.ARS_GRID_MJ* table that has the columns we update
    (WERKS, MAJ_CAT, PEND_ALC) and isn't a variant/gen_art grain table.

    Auto-adapts to whatever the grid builder created on this deployment —
    if a new attribute grid is added (e.g. ARS_GRID_MJ_NEW_DIM) the delta
    picks it up automatically. If an old grid is dropped, it's silently
    excluded instead of raising 'table not found'.

    Grids whose registry row in ARS_GRID_BUILDER has status='Inactive' are
    excluded — neither manual upload (+1 delta), revert (-1 delta), nor the
    post-op bootstrap re-sync touches their PEND_ALC. Grids absent from the
    registry are treated as active (back-compat with hand-created tables).
    """
    rows = conn.execute(text("""
        SELECT t.TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES t
        LEFT JOIN ARS_GRID_BUILDER g
               ON g.output_table = t.TABLE_NAME
        WHERE t.TABLE_TYPE = 'BASE TABLE'
          AND t.TABLE_NAME LIKE 'ARS_GRID_MJ%'
          AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS c
                      WHERE c.TABLE_NAME = t.TABLE_NAME AND c.COLUMN_NAME = 'WERKS')
          AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS c
                      WHERE c.TABLE_NAME = t.TABLE_NAME AND c.COLUMN_NAME = 'MAJ_CAT')
          AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS c
                      WHERE c.TABLE_NAME = t.TABLE_NAME AND c.COLUMN_NAME = 'PEND_ALC')
          AND (g.status IS NULL OR UPPER(g.status) <> 'INACTIVE')
        ORDER BY t.TABLE_NAME
    """)).fetchall()
    return [r[0] for r in rows if r[0] not in _EXCLUDED_FROM_ROLLUP]


# Hierarchy columns held in ARS_PEND_ALC directly — for these, the rollup
# can read straight from the delta / pend_alc table without a MSA join.
_PEND_ALC_NATIVE = {"MAJ_CAT", "GEN_ART_NUMBER", "CLR"}

# Columns we always exclude from "hierarchy" detection.
# Note: ARTICLE_NUMBER / VAR_ART / ARTICLE are included as valid hier cols
# (they're the article axis used by VAR_ART-style grids) — resolved via the
# delta payload's `t.art` field, not via master lookup.
_NON_HIER_COLS = {
    "WERKS", "RDC", "ST_CD",
    "PEND_ALC",
    "ID", "CREATED_AT", "UPDATED_AT", "LAST_UPDATED",
}

# Article-column aliases handled directly from the delta payload (t.art).
# Any column matching one of these names in a grid's hierarchy is read
# from the delta row instead of via a vw_master_product lookup.
_ARTICLE_NATIVE_COLS = {"ARTICLE_NUMBER", "VAR_ART", "ARTICLE"}


def _discover_grid_hierarchy(conn, grid_table: str) -> List[str]:
    """Discover the hierarchy columns of a rollup grid.

    Hierarchy columns = columns that exist on the grid table AND can be
    resolved per article via vw_master_product, minus the store/article
    axes and metric columns.

    Resolution sources:
      • Direct match in vw_master_product (e.g., MAJ_CAT, FAB, MICRO_MVGR,
        WEAVE_2, M_YARN_02, or any new master attribute added later)
      • Derived MERGE_<col> whose parent (e.g., RNG_SEG for MERGE_RNG_SEG)
        exists in vw_master_product — the CASE expression from
        derived_masters is applied at delta time.

    We use vw_master_product (NOT ARS_MSA_TOTAL) as the discovery source
    because:
      1. MSA only carries the allocation-eligible universe — pend-only
         articles aren't in MSA, so MSA-side lookup would miss them.
      2. The rollup delta SQL now JOINs vw_master_product as its master
         source, so the discovered hier_cols must match what MP can
         resolve. This keeps the path fully dynamic: any new attribute
         column added to MP flows automatically.

    Examples:
      ARS_GRID_MJ                 → ['MAJ_CAT']
      ARS_GRID_MJ_FAB             → ['FAB', 'MAJ_CAT']
      ARS_GRID_MJ_WEAVE_2         → ['MAJ_CAT', 'WEAVE_2']
      ARS_GRID_MJ_M_YARN_02       → ['M_YARN_02', 'MAJ_CAT']
      ARS_GRID_MJ_MERGE_RNG_SEG   → ['MAJ_CAT', 'MERGE_RNG_SEG']
    """
    grid_cols = {r[0] for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"
    ), {"t": grid_table}).fetchall()}

    mp_cols = {r[0] for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME = 'vw_master_product'"
    )).fetchall()}

    # Native master match (direct column in vw_master_product)
    candidates = (grid_cols & mp_cols) - _NON_HIER_COLS

    # MAJ_CAT is always a valid hier col even if absent from MP (it's part
    # of every grid's grain and is carried natively by ARS_PEND_ALC).
    if "MAJ_CAT" in grid_cols:
        candidates.add("MAJ_CAT")

    # Derived MERGE_<col>: include if the parent column lives in MP so we
    # can compute the merged value via CASE at delta time.
    try:
        from app.services import derived_masters as _dm
        for col in grid_cols - _NON_HIER_COLS:
            if col in candidates:
                continue
            if _dm.is_merge_col(col):
                parent = _dm.parent_col(col) or ""
                if parent and parent in mp_cols:
                    candidates.add(col)
    except Exception as e:
        logger.warning(f"derived_masters detection failed for {grid_table}: {e}")

    return sorted(candidates)


def _build_rollup_delta_sql(
    grid_table: str,
    hier_cols: List[str],
    delta_table: str,
    msa_article_col: str,
    conn=None,
) -> str:
    """Build an UPDATE statement that rolls up qty from a delta temp table
    into a rollup grid, joined on the grid's full hierarchy.

    Hierarchy columns present in ARS_PEND_ALC (MAJ_CAT, GEN_ART_NUMBER, CLR)
    are read directly from the delta. Master-lookup attrs (FAB, RNG_SEG,
    MACRO_MVGR, MICRO_MVGR, VND_CD, M_VND_CD, ...) come from
    vw_master_product joined on ARTICLE_NUMBER. Derived MERGE_<col> attrs
    are computed via the CASE expression from derived_masters, applied to
    the MP-joined parent column (e.g., MERGE_RNG_SEG = CASE MP.RNG_SEG ... END).

    NOTE: master attributes are sourced from `vw_master_product` (not
    ARS_MSA_TOTAL) because MSA only carries the allocation-eligible
    universe — pend-only articles aren't in MSA, so a MSA-side lookup
    would return NULL for those and the rollup UPDATE would silently miss
    ~130K units of pend that landed via INSERT. Master has every article.
    """
    delta_native = {
        "MAJ_CAT":         "t.maj_cat",
        "GEN_ART_NUMBER":  "t.gen_art",
        "CLR":             "t.clr",
        # Article axis (BIGINT) — VAR_ART-style grids include this in hier
        "ARTICLE_NUMBER":  "t.art",
        "VAR_ART":         "t.art",
        "ARTICLE":         "t.art",
    }

    # Resolve any derived MERGE_<col> in hier_cols to its parent + CASE expr
    merge_resolved: Dict[str, Tuple[str, str]] = {}  # col -> (parent_col, case_expr_on_MP)
    if conn is not None:
        try:
            from app.services import derived_masters as _dm
            for col in hier_cols:
                if _dm.is_merge_col(col):
                    parent = _dm.parent_col(col)
                    if not parent:
                        continue
                    expr = _dm.build_case_expr(conn, col, table_alias="MP")
                    if expr:
                        merge_resolved[col] = (parent, expr)
        except Exception as e:
            logger.warning(f"derived_masters resolution failed for {grid_table}: {e}")

    select_parts = ["t.st_cd AS werks"]
    group_parts  = ["t.st_cd"]
    join_clauses = ["X.WERKS = d.werks"]
    mp_lookup_cols: List[str] = []

    for col in hier_cols:
        bracketed = f"[{col}]"
        if col in delta_native:
            expr = delta_native[col]
            select_parts.append(f"{expr} AS {bracketed}")
            group_parts.append(expr)
        elif col in merge_resolved:
            # Derived MERGE_<col>: ensure the PARENT col is on the MP join,
            # then apply the CASE expression (already MP-rooted from derived_masters).
            parent, case_expr = merge_resolved[col]
            if parent not in mp_lookup_cols:
                mp_lookup_cols.append(parent)
            select_parts.append(f"{case_expr} AS {bracketed}")
            group_parts.append(case_expr)
        else:
            mp_lookup_cols.append(col)
            select_parts.append(f"MP.{bracketed} AS {bracketed}")
            group_parts.append(f"MP.{bracketed}")
        join_clauses.append(
            f"ISNULL(X.{bracketed}, '') = ISNULL(d.{bracketed}, '')"
        )

    select_parts.append("SUM(t.qty) AS qty")

    msa_join_sql = ""
    if mp_lookup_cols:
        # Source attributes from vw_master_product — same source as INSERT —
        # so pend-only articles (not in MSA) still get their attrs resolved.
        msa_join_sql = "LEFT JOIN dbo.vw_master_product MP ON MP.ARTICLE_NUMBER = t.art"

    # STK_TTL = physical_stock + PEND_ALC (user-chosen contract). PEND_ALC has
    # KPI='STK' in ARS_STORE_SLOC_SETTINGS, so pending units count toward total
    # committed stock for allocation planning. The delta must keep both columns
    # in step: PEND_ALC tracks just the pending portion, STK_TTL tracks the
    # combined total. Without `STK_TTL += d.qty`, STK_TTL drifts behind pend
    # activity between rebuilds.
    return f"""
        UPDATE X SET
            X.PEND_ALC = ISNULL(X.PEND_ALC, 0) + d.qty,
            X.STK_TTL  = ISNULL(X.STK_TTL,  0) + d.qty
        FROM [{grid_table}] X
        JOIN (
            SELECT {', '.join(select_parts)}
            FROM {delta_table} t
            {msa_join_sql}
            GROUP BY {', '.join(group_parts)}
        ) d
          ON {' AND '.join(join_clauses)}
    """


def _build_rollup_insert_sql(
    grid_table: str,
    hier_cols: List[str],
    delta_table: str,
    conn=None,
) -> str:
    """Build INSERT-WHERE-NOT-EXISTS for one rollup grid.

    For a pend-only article that doesn't yet have a row in this rollup
    grid, this inserts a placeholder (WERKS, hier_cols..., STK_TTL=0,
    PEND_ALC=0). The existing UPDATE built by `_build_rollup_delta_sql`
    then lands the qty on that fresh row.

    Hierarchy columns held on ARS_PEND_ALC directly (MAJ_CAT,
    GEN_ART_NUMBER, CLR) come from the delta payload; master-lookup attrs
    (FAB, RNG_SEG, MACRO_MVGR, MICRO_MVGR, M_VND_CD, VND_CD, ...) come
    from `vw_master_product` on ARTICLE_NUMBER. Derived MERGE_<col> attrs
    are computed via the CASE expression from derived_masters applied to
    the MP-side parent column (e.g., MERGE_RNG_SEG = CASE MP.RNG_SEG ... END).
    """
    delta_native = {
        "MAJ_CAT":        "d.maj_cat",
        "GEN_ART_NUMBER": "d.gen_art",
        "CLR":            "d.clr",
        # Article axis (BIGINT) — VAR_ART-style grids include this in hier
        "ARTICLE_NUMBER": "d.art",
        "VAR_ART":        "d.art",
        "ARTICLE":        "d.art",
    }

    # Resolve any derived MERGE_<col> in hier_cols → CASE on MP-side parent
    merge_resolved: Dict[str, str] = {}  # col -> case_expr_on_MP
    if conn is not None:
        try:
            from app.services import derived_masters as _dm
            for col in hier_cols:
                if _dm.is_merge_col(col):
                    expr = _dm.build_case_expr(conn, col, table_alias="MP")
                    if expr:
                        merge_resolved[col] = expr
        except Exception as e:
            logger.warning(f"derived_masters resolution failed for insert {grid_table}: {e}")

    insert_cols  = ["WERKS"] + [f"[{c}]" for c in hier_cols] + ["STK_TTL", "PEND_ALC"]
    select_parts = ["d.st_cd"]
    where_match  = ["g.WERKS = d.st_cd"]
    needs_mp     = False

    for col in hier_cols:
        if col in delta_native:
            expr = delta_native[col]
            select_parts.append(f"{expr}")
            where_match.append(
                f"ISNULL(g.[{col}], '') = ISNULL({expr}, '')"
            )
        elif col in merge_resolved:
            needs_mp = True
            case_expr = merge_resolved[col]
            select_parts.append(case_expr)
            where_match.append(
                f"ISNULL(g.[{col}], '') = ISNULL({case_expr}, '')"
            )
        else:
            needs_mp = True
            select_parts.append(f"MP.[{col}]")
            where_match.append(
                f"ISNULL(g.[{col}], '') = ISNULL(MP.[{col}], '')"
            )

    # INSERT seeds STK_TTL=0, PEND_ALC=0. The subsequent UPDATE built by
    # _build_rollup_delta_sql bumps BOTH columns by +qty, producing the
    # final state STK_TTL=qty, PEND_ALC=qty for newly-inserted rows
    # (matches the user-chosen contract: STK_TTL = physical + PEND_ALC).
    select_parts += ["0", "0"]  # STK_TTL, PEND_ALC

    mp_join = ""
    if needs_mp:
        mp_join = (
            "LEFT JOIN dbo.vw_master_product MP "
            "ON MP.ARTICLE_NUMBER = d.art"
        )

    return f"""
        INSERT INTO [{grid_table}] ({", ".join(insert_cols)})
        SELECT DISTINCT {", ".join(select_parts)}
        FROM (
            SELECT DISTINCT st_cd, art, maj_cat, gen_art, clr
            FROM {delta_table}
        ) d
        {mp_join}
        WHERE NOT EXISTS (
            SELECT 1 FROM [{grid_table}] g
            WHERE {" AND ".join(where_match)}
        )
    """


def _build_rollup_bootstrap_sql(
    grid_table: str,
    hier_cols: List[str],
    msa_article_col: str,
    conn=None,
) -> str:
    """Build an UPDATE that reseeds a rollup grid's PEND_ALC column from a
    fresh scan of ARS_PEND_ALC. Idempotent — SETs PEND_ALC, doesn't add.

    Resolution sources (same as `_build_rollup_delta_sql`):
      • MAJ_CAT, GEN_ART_NUMBER, CLR → from ARS_PEND_ALC directly
      • Other master attrs (FAB, RNG_SEG, WEAVE_2, M_YARN_02, ...) → from
        vw_master_product joined on ARTICLE_NUMBER
      • Derived MERGE_<col> → CASE expression on MP-side parent column

    Uses vw_master_product (NOT ARS_MSA_TOTAL) because MSA only carries the
    allocation-eligible universe — pend-only articles aren't in MSA, so
    MSA-side lookup would silently miss them. Master has every article.
    """
    pend_native = {
        "MAJ_CAT":         "P.MAJ_CAT",
        "GEN_ART_NUMBER":  "P.GEN_ART_NUMBER",
        "CLR":             "P.CLR",
        # Article axis — VAR_ART-style grids include this in hier
        "ARTICLE_NUMBER":  "P.ARTICLE_NUMBER",
        "VAR_ART":         "P.ARTICLE_NUMBER",
        "ARTICLE":         "P.ARTICLE_NUMBER",
    }

    # Resolve derived MERGE_<col> to (parent_col, MP-side CASE expression).
    merge_resolved: Dict[str, Tuple[str, str]] = {}
    if conn is not None:
        try:
            from app.services import derived_masters as _dm
            for col in hier_cols:
                if _dm.is_merge_col(col):
                    parent = _dm.parent_col(col)
                    if not parent:
                        continue
                    expr = _dm.build_case_expr(conn, col, table_alias="MP")
                    if expr:
                        merge_resolved[col] = (parent, expr)
        except Exception as e:
            logger.warning(f"derived_masters resolution failed for bootstrap {grid_table}: {e}")

    select_parts = ["P.ST_CD AS werks"]
    group_parts  = ["P.ST_CD"]
    join_clauses = ["agg.werks = X.WERKS"]
    mp_lookup_cols: List[str] = []

    for col in hier_cols:
        bracketed = f"[{col}]"
        if col in pend_native:
            expr = pend_native[col]
            select_parts.append(f"{expr} AS {bracketed}")
            group_parts.append(expr)
        elif col in merge_resolved:
            parent, case_expr = merge_resolved[col]
            if parent not in mp_lookup_cols:
                mp_lookup_cols.append(parent)
            select_parts.append(f"{case_expr} AS {bracketed}")
            group_parts.append(case_expr)
        else:
            mp_lookup_cols.append(col)
            select_parts.append(f"MP.{bracketed} AS {bracketed}")
            group_parts.append(f"MP.{bracketed}")
        join_clauses.append(
            f"ISNULL(X.{bracketed}, '') = ISNULL(agg.{bracketed}, '')"
        )

    select_parts.append("SUM(CAST(P.PEND_QTY AS FLOAT)) AS qty")

    mp_join_sql = ""
    if mp_lookup_cols:
        # vw_master_product is the canonical attribute source — has every
        # article (pend-only too), unlike ARS_MSA_TOTAL.
        mp_join_sql = (
            "LEFT JOIN dbo.vw_master_product MP "
            "ON MP.ARTICLE_NUMBER = P.ARTICLE_NUMBER"
        )

    return f"""
        UPDATE X SET X.PEND_ALC = ISNULL(agg.qty, 0)
        FROM [{grid_table}] X
        LEFT JOIN (
            SELECT {', '.join(select_parts)}
            FROM {PEND_ALC_TABLE} P
            {mp_join_sql}
            WHERE P.IS_CLOSED = 0
            GROUP BY {', '.join(group_parts)}
        ) agg
          ON {' AND '.join(join_clauses)}
    """


def apply_pend_alc_delta(conn, rows: List[Dict], sign: int = +1) -> Dict:
    """Apply a +qty (insert) or -qty (revert) delta across MSA + Grid for each
    ARS_PEND_ALC row. Symmetric: the same `rows` with sign=-1 exactly reverses
    a prior sign=+1 call.

    rows[i] keys: rdc, st_cd, article_number, maj_cat, gen_art_number, clr,
                  alloc_qty, do_qty (default 0)
    Effective qty per row = (alloc_qty - do_qty) * sign

    Returns counts of rows updated per target table (best-effort — missing
    tables are skipped silently and recorded as 0).
    """
    assert sign in (+1, -1), "sign must be +1 (insert) or -1 (revert)"
    result: Dict = {
        "msa_total":   0, "msa_var_art": 0, "msa_gen_art": 0,
        "grid_var":    0, "grid_gen":    0, "grid_rollup": 0,
        "error":       None,
    }

    # Build payload (skip zero-qty rows)
    payload = []
    for r in rows:
        eff = (float(r.get("alloc_qty", 0) or 0)
               - float(r.get("do_qty", 0) or 0)) * sign
        if eff == 0:
            continue
        payload.append({
            "r": str(r["rdc"]),
            "s": str(r.get("st_cd") or ""),
            "a": str(r["article_number"]),
            "m": str(r.get("maj_cat") or ""),
            "g": str(r.get("gen_art_number") or ""),
            "c": str(r.get("clr") or ""),
            "q": eff,
        })
    if not payload:
        return result

    tmp = f"#delta_{uuid.uuid4().hex[:8]}"
    try:
        conn.execute(text(f"""
            CREATE TABLE {tmp} (
                rdc      NVARCHAR(20),
                st_cd    NVARCHAR(20),
                art      NVARCHAR(30),
                maj_cat  NVARCHAR(200),
                gen_art  NVARCHAR(30),
                clr      NVARCHAR(50),
                qty      FLOAT
            )
        """))
        conn.execute(
            text(f"INSERT INTO {tmp} VALUES (:r, :s, :a, :m, :g, :c, :q)"),
            payload,
        )

        # ── Probe deployment-specific column names. Article/gen_art column
        # names vary across deployments (VAR_ART vs ARTICLE_NUMBER vs ARTICLE,
        # and GEN_ART_NUMBER vs GEN_ART). The first existing candidate wins.
        msa_total_art   = _probe_col(conn, "ARS_MSA_TOTAL",       "ARTICLE_NUMBER", "VAR_ART", "ARTICLE")
        msa_var_art_col = _probe_col(conn, "ARS_MSA_VAR_ART",     "ARTICLE_NUMBER", "VAR_ART", "ARTICLE")
        msa_gen_genc    = _probe_col(conn, "ARS_MSA_GEN_ART",     "GEN_ART_NUMBER", "GEN_ART")
        grid_var_art    = _probe_col(conn, "ARS_GRID_MJ_VAR_ART", "ARTICLE_NUMBER", "VAR_ART", "ARTICLE")
        grid_var_gen    = _probe_col(conn, "ARS_GRID_MJ_VAR_ART", "GEN_ART_NUMBER", "GEN_ART")
        grid_gen_gen    = _probe_col(conn, "ARS_GRID_MJ_GEN_ART", "GEN_ART_NUMBER", "GEN_ART")

        # ── MSA: ARS_MSA_TOTAL — keyed on (RDC, <article>) ──────────────
        # PEND_QTY is incremented by d.qty.
        # FNL_Q is recomputed from the canonical formula
        #     FNL_Q = max(STK_QTY − new_PEND_QTY − HOLD_QTY, 0)
        # rather than incrementally subtracted, because the incremental
        # approach drifted whenever FNL_Q hit 0 — clipping at 0 LOST the
        # excess units, so subsequent reverts couldn't restore the true
        # value. Recomputing from STK/PEND/HOLD makes the operation
        # symmetric and self-healing.
        #
        # MSA tables UPDATE-only by design (user requirement): only
        # (RDC, article) keys that already exist in the MSA universe get
        # adjusted. Pend-only articles (no matching MSA row) are deliberately
        # not inserted here — MSA represents the allocation-eligible universe
        # and must not be polluted with stockless rows. Grids, by contrast,
        # do insert missing rows further below to keep stock displays current.
        try:
            r1 = conn.execute(text(f"""
                UPDATE T SET
                    T.PEND_QTY = ISNULL(T.PEND_QTY, 0) + d.qty,
                    T.FNL_Q    = CASE
                        WHEN ISNULL(T.STK_QTY, 0)
                             - (ISNULL(T.PEND_QTY, 0) + d.qty)
                             - ISNULL(T.HOLD_QTY, 0) < 0 THEN 0
                        ELSE ISNULL(T.STK_QTY, 0)
                             - (ISNULL(T.PEND_QTY, 0) + d.qty)
                             - ISNULL(T.HOLD_QTY, 0)
                    END
                FROM ARS_MSA_TOTAL T
                JOIN (
                    SELECT rdc, art, SUM(qty) AS qty
                    FROM {tmp} GROUP BY rdc, art
                ) d ON T.RDC = d.rdc AND T.[{msa_total_art}] = d.art
            """))
            result["msa_total"] = int(r1.rowcount or 0)
        except Exception as e:
            logger.warning(f"[delta] MSA_TOTAL skipped: {e}")

        # ── MSA: ARS_MSA_VAR_ART — true rollup from MSA_TOTAL ───────────
        # VAR_ART is variant-grain like TOTAL (1:1 by RDC + ARTICLE), so we
        # just copy the now-correct PEND_QTY/FNL_Q from TOTAL for every
        # article touched by this delta. Eliminates drift; matches what
        # bootstrap_msa_pend_sync does.
        # MSA_VAR_ART: UPDATE-only (same MSA rule).
        try:
            r2 = conn.execute(text(f"""
                UPDATE V SET
                    V.PEND_QTY = T.PEND_QTY,
                    V.FNL_Q    = T.FNL_Q
                FROM ARS_MSA_VAR_ART V
                JOIN ARS_MSA_TOTAL T
                  ON T.RDC = V.RDC
                 AND T.[{msa_total_art}] = V.[{msa_var_art_col}]
                JOIN (
                    SELECT DISTINCT rdc, art FROM {tmp}
                ) d
                  ON d.rdc = T.RDC
                 AND d.art = T.[{msa_total_art}]
            """))
            result["msa_var_art"] = int(r2.rowcount or 0)
        except Exception as e:
            logger.warning(f"[delta] MSA_VAR_ART skipped: {e}")

        # ── MSA: ARS_MSA_GEN_ART — true rollup from MSA_TOTAL ────────────
        # Gen_art grain coarser than TOTAL — many variants per gen_art row.
        # We must SUM(FNL_Q) from MSA_TOTAL, not max(SUM(STK)−SUM(PEND)−SUM(HOLD), 0),
        # because max() doesn't distribute over sums when one variant
        # already clipped at 0. Scoped to (RDC, MAJ_CAT, GEN_ART, CLR)
        # keys touched by this delta to avoid scanning all of MSA_TOTAL.
        # MSA_GEN_ART: UPDATE-only (same MSA rule).
        try:
            r3 = conn.execute(text(f"""
                UPDATE G SET
                    G.PEND_QTY = agg.p,
                    G.FNL_Q    = agg.f
                FROM ARS_MSA_GEN_ART G
                JOIN (
                    SELECT T.RDC, T.MAJ_CAT, T.[{msa_gen_genc}] AS gen, T.CLR,
                           SUM(CAST(T.PEND_QTY AS FLOAT)) AS p,
                           SUM(CAST(T.FNL_Q    AS FLOAT)) AS f
                    FROM ARS_MSA_TOTAL T
                    JOIN (
                        SELECT DISTINCT rdc, maj_cat, gen_art, clr FROM {tmp}
                    ) a
                      ON T.RDC = a.rdc
                     AND ISNULL(T.MAJ_CAT, '')             = ISNULL(a.maj_cat, '')
                     AND ISNULL(T.[{msa_gen_genc}], '')    = ISNULL(a.gen_art, '')
                     AND ISNULL(T.CLR, '')                 = ISNULL(a.clr, '')
                    GROUP BY T.RDC, T.MAJ_CAT, T.[{msa_gen_genc}], T.CLR
                ) agg
                  ON G.RDC = agg.RDC
                 AND ISNULL(G.MAJ_CAT, '')             = ISNULL(agg.MAJ_CAT, '')
                 AND ISNULL(G.[{msa_gen_genc}], '')    = ISNULL(agg.gen, '')
                 AND ISNULL(G.CLR, '')                 = ISNULL(agg.CLR, '')
            """))
            result["msa_gen_art"] = int(r3.rowcount or 0)
        except Exception as e:
            logger.warning(f"[delta] MSA_GEN_ART skipped: {e}")

        # NOTE: ARS_GRID_MJ_VAR_ART and ARS_GRID_MJ_GEN_ART are now handled
        # by the auto-discovered rollup loop below (no longer in
        # _EXCLUDED_FROM_ROLLUP). Their hier_cols include ARTICLE_NUMBER /
        # VAR_ART which is in delta_native (mapped to t.art) so the rollup
        # builder handles them like any other grid. SZ and other extra hier
        # cols are auto-resolved via vw_master_product.
        # Result keys grid_var / grid_gen are reported from the rollup pass.

        # ── Grid: attribute rollups — based on each grid's HIERARCHY ─────
        # Each rollup grid has its own hierarchy (MAJ_CAT only / MAJ_CAT+CLR /
        # MAJ_CAT+FAB / MAJ_CAT+RNG_SEG / etc.), discovered at runtime from
        # INFORMATION_SCHEMA. Attributes not held directly on ARS_PEND_ALC
        # (FAB, RNG_SEG, MACRO_MVGR, MICRO_MVGR, VND_CD, M_VND_CD) are
        # resolved via a per-article lookup on ARS_MSA_TOTAL.
        rollup_tables = _discover_grid_rollup_tables(conn)
        rollup_total = 0
        for tbl in rollup_tables:
            try:
                hier_cols = _discover_grid_hierarchy(conn, tbl)
                if not hier_cols:
                    logger.debug(f"[delta] {tbl}: no hierarchy columns found, skipping")
                    continue
                # On +1 deltas, INSERT placeholder rows for any (WERKS,
                # hier_cols...) tuple missing from this rollup grid — the
                # grouping attributes (FAB, M_VND_CD, MICRO_MVGR, ...) are
                # resolved per-article from vw_master_product. Without this
                # a pend-only article (no current stock) silently drops out
                # of every rollup grid until the next full rebuild.
                if sign > 0:
                    conn.execute(text(
                        _build_rollup_insert_sql(tbl, hier_cols, tmp, conn=conn)
                    ))
                sql = _build_rollup_delta_sql(tbl, hier_cols, tmp, msa_total_art, conn=conn)
                rr = conn.execute(text(sql))
                cnt = int(rr.rowcount or 0)
                rollup_total += cnt
                # Maintain back-compat result keys for the two article-grain grids
                # that used to have their own dedicated blocks.
                if tbl == "ARS_GRID_MJ_VAR_ART":
                    result["grid_var"] = cnt
                elif tbl == "ARS_GRID_MJ_GEN_ART":
                    result["grid_gen"] = cnt
                logger.debug(
                    f"[delta] {tbl}: hier={hier_cols} → {cnt} rows"
                )
            except Exception as e:
                logger.warning(f"[delta] rollup grid {tbl} skipped: {e}")
        result["grid_rollup"] = rollup_total

    except Exception as e:
        logger.warning(f"[delta] apply_pend_alc_delta failed (non-fatal): {e}")
        result["error"] = str(e)
    finally:
        try:
            conn.execute(text(
                f"IF OBJECT_ID('tempdb..{tmp}') IS NOT NULL DROP TABLE {tmp}"
            ))
        except Exception:
            pass
        try:
            conn.commit()
        except Exception:
            pass

    logger.info(
        f"[delta sign={sign}] msa_total={result['msa_total']} "
        f"var={result['msa_var_art']} gen={result['msa_gen_art']} | "
        f"grid_var={result['grid_var']} grid_gen={result['grid_gen']} "
        f"rollups={result['grid_rollup']}"
    )
    return result


def apply_pend_alc_delta_by_session(
    conn, session_id: str, sign: int = +1
) -> Dict:
    """Convenience wrapper: read PEND_ALC rows by session_id, then apply the
    delta. Used by approve_parked which only has the session_id, not the
    original row dicts.
    """
    rows = [
        {
            "rdc":            r[0],
            "st_cd":          r[1],
            "article_number": r[2],
            "maj_cat":        r[3],
            "gen_art_number": r[4],
            "clr":            r[5],
            "alloc_qty":      float(r[6] or 0),
            "do_qty":         float(r[7] or 0),
        }
        for r in conn.execute(text(f"""
            SELECT RDC, ST_CD, ARTICLE_NUMBER, MAJ_CAT, GEN_ART_NUMBER, CLR,
                   ALLOC_QTY, ISNULL(DO_QTY, 0)
            FROM {PEND_ALC_TABLE}
            WHERE SESSION_ID = :sid
        """), {"sid": session_id}).fetchall()
    ]
    return apply_pend_alc_delta(conn, rows, sign=sign)


def bootstrap_msa_pend_sync(conn) -> Dict:
    """Reseed PEND_QTY/FNL_Q in ARS_MSA_TOTAL/VAR_ART/GEN_ART from a fresh scan
    of ARS_PEND_ALC (open rows only). Safe to run repeatedly — idempotent.

    Call ONCE manually before deploying the delta function for the first time
    (existing GEN_ART totals may be stale). After that, every full MSA build
    should call this at the end of `store_results` so a freshly-rebuilt MSA
    matches the open pend_alc ledger.
    """
    result: Dict = {"msa_total": 0, "msa_var_art": 0, "msa_gen_art": 0, "error": None}
    try:
        msa_total_art   = _probe_col(conn, "ARS_MSA_TOTAL",   "ARTICLE_NUMBER", "VAR_ART", "ARTICLE")
        msa_var_art_col = _probe_col(conn, "ARS_MSA_VAR_ART", "ARTICLE_NUMBER", "VAR_ART", "ARTICLE")
        msa_gen_genc    = _probe_col(conn, "ARS_MSA_GEN_ART", "GEN_ART_NUMBER", "GEN_ART")

        # 1a. Seed PEND_QTY into ARS_MSA_TOTAL from open pend_alc rows.
        # MSA tables are UPDATE-only by design (user requirement): only
        # (RDC, article) keys already in the MSA universe get adjusted.
        # Pend-only articles (no matching MSA row) are intentionally skipped
        # — MSA represents the allocation-eligible universe and must not be
        # polluted with stockless rows.
        r1 = conn.execute(text(f"""
            ;WITH P AS (
                SELECT RDC, ARTICLE_NUMBER, SUM(PEND_QTY) AS qty
                FROM {PEND_ALC_TABLE}
                WHERE IS_CLOSED = 0
                GROUP BY RDC, ARTICLE_NUMBER
            )
            UPDATE T
               SET T.PEND_QTY = ISNULL(P.qty, 0),
                   T.FNL_Q    = CASE
                       WHEN ISNULL(T.STK_QTY, 0) - ISNULL(P.qty, 0)
                            - ISNULL(T.HOLD_QTY, 0) < 0 THEN 0
                       ELSE ISNULL(T.STK_QTY, 0) - ISNULL(P.qty, 0)
                            - ISNULL(T.HOLD_QTY, 0)
                   END
            FROM ARS_MSA_TOTAL T
            LEFT JOIN P ON P.RDC = T.RDC AND P.ARTICLE_NUMBER = T.[{msa_total_art}]
        """))
        result["msa_total"] = int(r1.rowcount or 0)

        # 1b. Roll up TOTAL → VAR_ART (UPDATE-only — same MSA rule).
        r2 = conn.execute(text(f"""
            UPDATE V
               SET V.PEND_QTY = agg.p, V.FNL_Q = agg.f
            FROM ARS_MSA_VAR_ART V
            JOIN (
                SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, [{msa_total_art}] AS art,
                       SUM(CAST(PEND_QTY AS FLOAT)) AS p,
                       SUM(CAST(FNL_Q    AS FLOAT)) AS f
                FROM ARS_MSA_TOTAL
                GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, [{msa_total_art}]
            ) agg
              ON V.RDC = agg.RDC
             AND ISNULL(V.MAJ_CAT, '')        = ISNULL(agg.MAJ_CAT, '')
             AND ISNULL(V.GEN_ART_NUMBER, '') = ISNULL(agg.GEN_ART_NUMBER, '')
             AND ISNULL(V.CLR, '')            = ISNULL(agg.CLR, '')
             AND V.[{msa_var_art_col}]        = agg.art
        """))
        result["msa_var_art"] = int(r2.rowcount or 0)

        # 1c. Roll up TOTAL → GEN_ART (UPDATE-only — same MSA rule).
        r3 = conn.execute(text(f"""
            UPDATE G
               SET G.PEND_QTY = agg.p, G.FNL_Q = agg.f
            FROM ARS_MSA_GEN_ART G
            JOIN (
                SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR,
                       SUM(CAST(PEND_QTY AS FLOAT)) AS p,
                       SUM(CAST(FNL_Q    AS FLOAT)) AS f
                FROM ARS_MSA_TOTAL
                GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR
            ) agg
              ON G.RDC = agg.RDC
             AND ISNULL(G.MAJ_CAT, '')             = ISNULL(agg.MAJ_CAT, '')
             AND ISNULL(G.[{msa_gen_genc}], '')    = ISNULL(agg.GEN_ART_NUMBER, '')
             AND ISNULL(G.CLR, '')                 = ISNULL(agg.CLR, '')
        """))
        result["msa_gen_art"] = int(r3.rowcount or 0)

        conn.commit()
    except Exception as e:
        logger.warning(f"[bootstrap_msa] failed (non-fatal): {e}")
        result["error"] = str(e)
        try: conn.rollback()
        except Exception: pass

    logger.info(
        f"[bootstrap_msa] reseeded: total={result['msa_total']} "
        f"var={result['msa_var_art']} gen={result['msa_gen_art']}"
    )
    return result


def bootstrap_msa_hold_sync(conn, session_id: Optional[str] = None) -> Dict:
    """Reseed HOLD_QTY in ARS_MSA_TOTAL/VAR_ART/GEN_ART from currently-open
    ARS_NL_TBL_HOLD_TRACKING rows, then recompute FNL_Q from the canonical
    formula max(STK_QTY − PEND_QTY − HOLD_QTY, 0).

    Mirror of bootstrap_msa_pend_sync but for the HOLD axis. Idempotent.

    Args:
        session_id: Optional. If provided, scope the reseed to ONLY the
            (RDC, ARTICLE) keys touched by this session in ARS_ALLOC_HISTORY
            — turns a 100K-row full-table update into a few-hundred-row
            scoped update. Major win on approve where the affected article
            set is small.
            Pass None to reseed every row (used by msa_result_storage,
            full MSA builds, and the diagnostic script).

    RDC resolution: prefers ARS_NL_TBL_HOLD_TRACKING.RDC if present (added
    by listing.py Part 8.6 setup). Falls back to a Master_ALC_INPUT_ST_MASTER
    join for older rows where RDC is still NULL.

    Call from:
      • approve_parked, after the PEND delta — keeps HOLD in step with the
        same lifecycle event that updates PEND. PASS session_id for speed.
      • msa_result_storage.store_results — defensive double-check after a
        full MSA build. Pass None.
    """
    result: Dict = {
        "msa_total": 0, "msa_var_art": 0, "msa_gen_art": 0,
        "scoped": session_id is not None, "error": None,
    }
    try:
        msa_total_art   = _probe_col(conn, "ARS_MSA_TOTAL",
                                     "ARTICLE_NUMBER", "VAR_ART", "ARTICLE")
        msa_var_art_col = _probe_col(conn, "ARS_MSA_VAR_ART",
                                     "ARTICLE_NUMBER", "VAR_ART", "ARTICLE")
        msa_gen_genc    = _probe_col(conn, "ARS_MSA_GEN_ART",
                                     "GEN_ART_NUMBER", "GEN_ART")

        # Build a scope-filter CTE that resolves the (RDC, ARTICLE) keys
        # touched by this session — used to limit MSA_TOTAL row updates
        # below. Empty / null when running unscoped.
        scope_cte = ""
        scope_join = ""
        sql_params: Dict = {}
        if session_id:
            # Resolve affected (RDC, ARTICLE) keys via the alloc history for
            # this session, joined through store master to map WERKS→RDC.
            scope_cte = f"""
                , ScopedKeys AS (
                    SELECT DISTINCT
                           SM.RDC AS rdc,
                           CAST(A.[VAR_ART] AS NVARCHAR(30)) AS art
                    FROM [ARS_ALLOC_HISTORY] A
                    INNER JOIN [Master_ALC_INPUT_ST_MASTER] SM
                        ON SM.[ST_CD] = A.[WERKS]
                    WHERE A.[SESSION_ID] = :sid
                      AND A.[OPT_TYPE] IN ('RL','TBC','TBL','NL')
                )
            """
            scope_join = (
                "INNER JOIN ScopedKeys SK "
                "ON SK.rdc = T.RDC AND SK.art = T.[{}]".format(msa_total_art)
            )
            sql_params["sid"] = session_id

        # Detect whether ARS_NL_TBL_HOLD_TRACKING already has the RDC column
        has_rdc_col = (conn.execute(text("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_NAME = 'ARS_NL_TBL_HOLD_TRACKING' AND COLUMN_NAME = 'RDC'
        """)).scalar() or 0) > 0

        if has_rdc_col:
            hold_cte_inner = """
                SELECT COALESCE(NULLIF(H.RDC, ''), SM.RDC) AS rdc,
                       CAST(H.VAR_ART AS NVARCHAR(30)) AS art,
                       SUM(CAST(H.HOLD_REM AS FLOAT)) AS hold_qty
                FROM ARS_NL_TBL_HOLD_TRACKING H
                LEFT JOIN Master_ALC_INPUT_ST_MASTER SM ON SM.ST_CD = H.WERKS
                WHERE ISNULL(H.IS_CLOSED, 0) = 0
                  AND ISNULL(H.HOLD_REM, 0) > 0
                GROUP BY COALESCE(NULLIF(H.RDC, ''), SM.RDC),
                         CAST(H.VAR_ART AS NVARCHAR(30))
            """
        else:
            hold_cte_inner = """
                SELECT SM.RDC AS rdc,
                       CAST(H.VAR_ART AS NVARCHAR(30)) AS art,
                       SUM(CAST(H.HOLD_REM AS FLOAT)) AS hold_qty
                FROM ARS_NL_TBL_HOLD_TRACKING H
                INNER JOIN Master_ALC_INPUT_ST_MASTER SM ON SM.ST_CD = H.WERKS
                WHERE ISNULL(H.IS_CLOSED, 0) = 0
                  AND ISNULL(H.HOLD_REM, 0) > 0
                GROUP BY SM.RDC, CAST(H.VAR_ART AS NVARCHAR(30))
            """

        # 1. Reseed MSA_TOTAL.HOLD_QTY + recompute FNL_Q.  When session_id
        # is provided, ScopedKeys filters MSA_TOTAL rows so only those
        # touched by this session get re-written.
        try:
            r1 = conn.execute(text(f"""
                ;WITH H AS ({hold_cte_inner})
                {scope_cte}
                UPDATE T SET
                    T.HOLD_QTY = ISNULL(H.hold_qty, 0),
                    T.FNL_Q = CASE
                        WHEN ISNULL(T.STK_QTY, 0) - ISNULL(T.PEND_QTY, 0)
                             - ISNULL(H.hold_qty, 0) < 0 THEN 0
                        ELSE ISNULL(T.STK_QTY, 0) - ISNULL(T.PEND_QTY, 0)
                             - ISNULL(H.hold_qty, 0)
                    END
                FROM ARS_MSA_TOTAL T
                {scope_join}
                LEFT JOIN H ON H.rdc = T.RDC AND H.art = T.[{msa_total_art}]
            """), sql_params)
            result["msa_total"] = int(r1.rowcount or 0)
        except Exception as e:
            logger.warning(f"[bootstrap_hold] MSA_TOTAL skipped: {e}")

        # 2. Mirror to MSA_VAR_ART (variant grain — 1:1 with MSA_TOTAL).
        # Same scope filter to avoid scanning the entire VAR_ART.
        var_scope_join = ""
        if session_id:
            var_scope_join = (
                "INNER JOIN [ARS_ALLOC_HISTORY] AH "
                "ON AH.[SESSION_ID] = :sid "
                "AND CAST(AH.[VAR_ART] AS NVARCHAR(30)) = "
                f"CAST(V.[{msa_var_art_col}] AS NVARCHAR(30))"
            )
        try:
            r2 = conn.execute(text(f"""
                UPDATE V SET V.HOLD_QTY = T.HOLD_QTY, V.FNL_Q = T.FNL_Q
                FROM ARS_MSA_VAR_ART V
                JOIN ARS_MSA_TOTAL T
                  ON T.RDC = V.RDC
                 AND T.[{msa_total_art}] = V.[{msa_var_art_col}]
                {var_scope_join}
            """), sql_params)
            result["msa_var_art"] = int(r2.rowcount or 0)
        except Exception as e:
            logger.warning(f"[bootstrap_hold] MSA_VAR_ART skipped: {e}")

        # 3. Roll up to MSA_GEN_ART — sum HOLD_QTY and FNL_Q from MSA_TOTAL
        # by gen_art group (sum of FNL_Q is the correct rollup, NOT
        # max(STK − PEND − HOLD) — see comment in apply_pend_alc_delta).
        # When session_id is provided, only the gen_art groups whose
        # variants appear in the session's history are recomputed.
        try:
            if session_id:
                # Scoped: derive affected (RDC, MAJ_CAT, GEN_ART, CLR) keys
                # from the session's articles via MSA_TOTAL → AH join.
                r3 = conn.execute(text(f"""
                    ;WITH ScopedGenKeys AS (
                        SELECT DISTINCT T.RDC, T.MAJ_CAT, T.[{msa_gen_genc}] AS gen, T.CLR
                        FROM ARS_MSA_TOTAL T
                        INNER JOIN [ARS_ALLOC_HISTORY] AH
                            ON AH.[SESSION_ID] = :sid
                           AND CAST(AH.[VAR_ART] AS NVARCHAR(30))
                                = CAST(T.[{msa_total_art}] AS NVARCHAR(30))
                    )
                    UPDATE G SET G.HOLD_QTY = agg.h, G.FNL_Q = agg.f
                    FROM ARS_MSA_GEN_ART G
                    INNER JOIN ScopedGenKeys SK
                       ON G.RDC = SK.RDC
                      AND ISNULL(G.MAJ_CAT, '')             = ISNULL(SK.MAJ_CAT, '')
                      AND ISNULL(G.[{msa_gen_genc}], '')    = ISNULL(SK.gen, '')
                      AND ISNULL(G.CLR, '')                 = ISNULL(SK.CLR, '')
                    JOIN (
                        SELECT T.RDC, T.MAJ_CAT, T.[{msa_gen_genc}] AS gen, T.CLR,
                               SUM(CAST(T.HOLD_QTY AS FLOAT)) AS h,
                               SUM(CAST(T.FNL_Q    AS FLOAT)) AS f
                        FROM ARS_MSA_TOTAL T
                        GROUP BY T.RDC, T.MAJ_CAT, T.[{msa_gen_genc}], T.CLR
                    ) agg
                      ON G.RDC = agg.RDC
                     AND ISNULL(G.MAJ_CAT, '')             = ISNULL(agg.MAJ_CAT, '')
                     AND ISNULL(G.[{msa_gen_genc}], '')    = ISNULL(agg.gen, '')
                     AND ISNULL(G.CLR, '')                 = ISNULL(agg.CLR, '')
                """), sql_params)
            else:
                r3 = conn.execute(text(f"""
                    UPDATE G SET G.HOLD_QTY = agg.h, G.FNL_Q = agg.f
                    FROM ARS_MSA_GEN_ART G
                    JOIN (
                        SELECT T.RDC, T.MAJ_CAT, T.[{msa_gen_genc}] AS gen, T.CLR,
                               SUM(CAST(T.HOLD_QTY AS FLOAT)) AS h,
                               SUM(CAST(T.FNL_Q    AS FLOAT)) AS f
                        FROM ARS_MSA_TOTAL T
                        GROUP BY T.RDC, T.MAJ_CAT, T.[{msa_gen_genc}], T.CLR
                    ) agg
                      ON G.RDC = agg.RDC
                     AND ISNULL(G.MAJ_CAT, '')             = ISNULL(agg.MAJ_CAT, '')
                     AND ISNULL(G.[{msa_gen_genc}], '')    = ISNULL(agg.gen, '')
                     AND ISNULL(G.CLR, '')                 = ISNULL(agg.CLR, '')
                """))
            result["msa_gen_art"] = int(r3.rowcount or 0)
        except Exception as e:
            logger.warning(f"[bootstrap_hold] MSA_GEN_ART skipped: {e}")

        conn.commit()
    except Exception as e:
        logger.warning(f"[bootstrap_hold] failed (non-fatal): {e}")
        result["error"] = str(e)
        try: conn.rollback()
        except Exception: pass

    logger.info(
        f"[bootstrap_hold] reseeded: total={result['msa_total']} "
        f"var={result['msa_var_art']} gen={result['msa_gen_art']}"
    )
    return result


def bootstrap_grid_pend_sync(conn) -> Dict:
    """Reseed PEND_ALC on the grid family from a fresh scan of ARS_PEND_ALC.
    Skips silently if a grid table doesn't exist on this deployment. Safe to
    run repeatedly — idempotent.

    Call ONCE manually before deploying the delta function and each time the
    grid is fully rebuilt. Note: the 8 attribute-rollup grids are seeded at
    (WERKS, MAJ_CAT) precision (matches the incremental delta granularity).
    """
    result: Dict = {"grid_var": 0, "grid_gen": 0, "grid_rollup": 0, "error": None}
    try:
        # ARS_GRID_MJ_VAR_ART and _GEN_ART are now handled by the same
        # auto-discovered rollup loop below (their article column is in
        # delta_native, SZ and other extras auto-resolved via MP). Fully
        # dynamic — any future grid table flows through identically.
        msa_article_col = _probe_col(
            conn, "ARS_MSA_TOTAL", "ARTICLE_NUMBER", "VAR_ART", "ARTICLE",
        )
        rollup_tables = _discover_grid_rollup_tables(conn)
        rollup_total = 0
        for tbl in rollup_tables:
            try:
                hier_cols = _discover_grid_hierarchy(conn, tbl)
                if not hier_cols:
                    logger.debug(
                        f"[bootstrap_grid] {tbl}: no hierarchy columns found, skipping"
                    )
                    continue
                sql = _build_rollup_bootstrap_sql(tbl, hier_cols, msa_article_col, conn=conn)
                rr = conn.execute(text(sql))
                cnt = int(rr.rowcount or 0)
                rollup_total += cnt
                # Maintain back-compat result keys
                if tbl == "ARS_GRID_MJ_VAR_ART":
                    result["grid_var"] = cnt
                elif tbl == "ARS_GRID_MJ_GEN_ART":
                    result["grid_gen"] = cnt
                logger.debug(
                    f"[bootstrap_grid] {tbl}: hier={hier_cols} → {cnt} rows"
                )
            except Exception as e:
                logger.warning(f"[bootstrap_grid] {tbl} skipped: {e}")
        result["grid_rollup"] = rollup_total

        conn.commit()
    except Exception as e:
        logger.warning(f"[bootstrap_grid] failed (non-fatal): {e}")
        result["error"] = str(e)
        try: conn.rollback()
        except Exception: pass

    logger.info(
        f"[bootstrap_grid] reseeded: var={result['grid_var']} "
        f"gen={result['grid_gen']} rollups={result['grid_rollup']}"
    )
    return result
