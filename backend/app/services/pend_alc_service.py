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
from typing import Dict, List, Optional

from loguru import logger
from sqlalchemy import text


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
             "old_last_bdc_at": None, "new_bdc_qty": float(p[1] or 0)}
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
    """Subtract DO_QTY from PEND rows + restore IS_CLOSED + roll back history."""
    pend_updates = payload.get("pend_updates") or []
    history_updates = payload.get("history_updates") or []

    pend_rows = 0
    for u in pend_updates:
        conn.execute(text(f"""
            UPDATE {PEND_ALC_TABLE}
               SET DO_QTY    = DO_QTY - :q,
                   IS_CLOSED = CASE WHEN :was_closed = 1 THEN 0 ELSE IS_CLOSED END,
                   LAST_DO_AT = :prev_do_at
             WHERE ID = :id
        """), {
            "q":           float(u.get("qty_added") or 0),
            "was_closed":  1 if u.get("was_just_closed") else 0,
            "prev_do_at":  u.get("prev_last_do_at"),
            "id":          int(u["pend_alc_id"]),
        })
        pend_rows += 1

    history_rows = 0
    for h in history_updates:
        conn.execute(text(f"""
            UPDATE {BDC_HISTORY_TABLE}
               SET DO_RECEIVED = :got,
                   STATUS      = :s,
                   LAST_DO_AT  = :t
             WHERE ID = :id
        """), {
            "got": float(h.get("old_do_received") or 0),
            "s":   h.get("old_status") or "OPEN",
            "t":   h.get("old_last_do_at"),
            "id":  int(h["history_id"]),
        })
        history_rows += 1

    return {"pend_alc_rows_reverted": pend_rows,
            "bdc_history_rows_reverted": history_rows}


def _revert_manual(conn, payload: Dict) -> Dict:
    """Delete inserted PEND_ALC rows + apply symmetric -1 delta to MSA/Grid.

    Reads the rows BEFORE deleting so we have the qty + grain info needed to
    reverse the original +1 delta exactly. The delta function is symmetric —
    same rows × sign=-1 produces byte-for-byte the inverse of the +1 call
    that was made when the rows were originally inserted.
    """
    inserted_ids = payload.get("inserted_ids") or []
    if not inserted_ids:
        return {"pend_alc_rows_deleted": 0}
    placeholders = ",".join(str(int(x)) for x in inserted_ids)

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
            WHERE ID IN ({placeholders})
        """)).fetchall()
    ]

    res = conn.execute(text(
        f"DELETE FROM {PEND_ALC_TABLE} WHERE ID IN ({placeholders})"
    ))
    deleted = int(res.rowcount or 0)

    if rows_to_revert:
        try:
            apply_pend_alc_delta(conn, rows_to_revert, sign=-1)
        except Exception as e:
            logger.warning(f"[revert] -1 delta skipped: {e}")

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


def write_manual_pend_alc(conn, rows: List[Dict]) -> Dict:
    """Insert manually-uploaded allocation rows into ARS_PEND_ALC.

    Returns: {
        "inserted":     int,
        "session_id":   str,         # unique per upload
        "inserted_ids": List[int],   # for revert
    }
    The caller writes session_id + inserted_ids into the operations log so a
    future revert can DELETE just the rows from this upload.

    Each dict must have: rdc, article_number, alloc_qty.
    Optional: st_cd, maj_cat, gen_art_number, clr, remarks.
    SOURCE='MANUAL', ALLOC_MODE='MANUAL'.
    SESSION_ID='MANUAL-YYYYMMDD-<6char-hex>' so each upload is uniquely
    identifiable (multiple uploads same day used to collide).
    """
    ensure_pend_alc_table(conn)
    valid = [r for r in rows if float(r.get("alloc_qty", 0) or 0) > 0]
    if not valid:
        return {"inserted": 0, "session_id": "", "inserted_ids": []}

    import datetime
    session_id = (
        f"MANUAL-{datetime.date.today().strftime('%Y%m%d')}-"
        f"{uuid.uuid4().hex[:6]}"
    )

    # Build the parameter tuples once, in a fixed column order. Values that
    # land in the staging NVARCHAR columns are converted to str (or kept as
    # None) — fast_executemany needs consistent types per column.
    params = [
        (
            session_id,
            str(r["rdc"]),
            (str(r.get("st_cd")).strip() if r.get("st_cd") else None) or None,
            str(r["article_number"]),
            r.get("maj_cat") or None,
            r.get("gen_art_number") or None,
            r.get("clr") or None,
            str(float(r["alloc_qty"])),  # numeric stored as str in staging
            r.get("remarks") or None,
        )
        for r in valid
    ]

    # ── Architecture mirrors UpsertEngine._bulk_upsert (Upload's fast path) ─
    # 1. CREATE TABLE #stage  — NVARCHAR(4000) per column to avoid the
    #    fast_executemany NVARCHAR(MAX) memory blow-up.
    # 2. fast_executemany INSERT into #stage in chunks of 10K rows.
    # 3. ONE MERGE from #stage → ARS_PEND_ALC with ROWLOCK so the target
    #    table is locked for milliseconds, not the duration of every chunk.
    # 4. DROP #stage.
    #
    # MERGE uses ON 1=0 because manual entries are append-only — every row is
    # always a brand-new ARS_PEND_ALC record (each session_id is unique to
    # this call). Matching on PK would silently UPDATE prior sessions and
    # break the operations-log revert flow.
    staging = f"#stage_pend_alc_{uuid.uuid4().hex[:8]}"

    raw = conn.connection
    cursor = raw.cursor()
    try:
        try:
            cursor.fast_executemany = True
        except Exception:
            pass

        cursor.execute(f"""
            CREATE TABLE {staging} (
                SESSION_ID      NVARCHAR(4000) NULL,
                RDC             NVARCHAR(4000) NULL,
                ST_CD           NVARCHAR(4000) NULL,
                ARTICLE_NUMBER  NVARCHAR(4000) NULL,
                MAJ_CAT         NVARCHAR(4000) NULL,
                GEN_ART_NUMBER  NVARCHAR(4000) NULL,
                CLR             NVARCHAR(4000) NULL,
                ALLOC_QTY       NVARCHAR(4000) NULL,
                REMARKS         NVARCHAR(4000) NULL
            )
        """)

        stage_sql = (
            f"INSERT INTO {staging} "
            "(SESSION_ID, RDC, ST_CD, ARTICLE_NUMBER, MAJ_CAT, GEN_ART_NUMBER, "
            " CLR, ALLOC_QTY, REMARKS) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )

        CHUNK = 10000
        staged = 0
        for i in range(0, len(params), CHUNK):
            batch = params[i:i + CHUNK]
            cursor.executemany(stage_sql, batch)
            staged += len(batch)
            logger.info(
                f"[pend_alc] manual upload (stage): {staged}/{len(params)} rows staged"
            )

        # MERGE staging → target. ROWLOCK prevents lock escalation. ON 1=0
        # forces every staged row into WHEN NOT MATCHED → INSERT (append-only).
        # TRY_CAST guards against any non-numeric ALLOC_QTY slipping through;
        # invalid values land as NULL instead of failing the entire batch.
        merge_sql = f"""
            MERGE {PEND_ALC_TABLE} WITH (ROWLOCK) AS target
            USING {staging} AS source
            ON 1 = 0
            WHEN NOT MATCHED BY TARGET THEN
                INSERT (
                    SESSION_ID, RDC, ST_CD, ARTICLE_NUMBER, MAJ_CAT,
                    GEN_ART_NUMBER, CLR, ALLOC_QTY, ALLOC_MODE, SOURCE, REMARKS
                )
                VALUES (
                    source.SESSION_ID, source.RDC, source.ST_CD, source.ARTICLE_NUMBER,
                    source.MAJ_CAT, source.GEN_ART_NUMBER, source.CLR,
                    TRY_CAST(source.ALLOC_QTY AS DECIMAL(18,4)),
                    'MANUAL', 'MANUAL', source.REMARKS
                );
        """
        cursor.execute(merge_sql)
    finally:
        try:
            cursor.execute(
                f"IF OBJECT_ID('tempdb..{staging}') IS NOT NULL DROP TABLE {staging}"
            )
        except Exception:
            pass
        cursor.close()

    conn.commit()

    # Read back the inserted IDs by session_id (unique per upload).
    inserted_ids = [
        int(r[0]) for r in conn.execute(text(f"""
            SELECT ID FROM {PEND_ALC_TABLE} WHERE SESSION_ID = :sid ORDER BY ID
        """), {"sid": session_id}).fetchall()
    ]
    inserted = len(inserted_ids)

    logger.info(
        f"[pend_alc] manual upload complete: {inserted} rows inserted via MERGE, "
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

    Matching strategy per row:
      1. If `allocation_number` is provided in the DO row → match that exact
         BDC (the SAP DO file references the BDC it satisfies).
      2. Otherwise → FIFO across all open history rows for the same
         (RDC, ST_CD, ARTICLE). Oldest BDC absorbs first.

    Updates DO_RECEIVED and flips STATUS to PARTIAL or CONFIRMED.

    do_rows: dicts with rdc, article_number, do_qty,
             optional st_cd, optional allocation_number.
    Returns count of history rows touched.
    """
    if not do_rows:
        return {"touched": 0, "history_updates": []}
    ensure_bdc_history_table(conn)
    touched = 0
    history_updates: List[Dict] = []
    for r in do_rows:
        rdc       = str(r.get("rdc") or "").strip()
        art       = str(r.get("article_number") or "").strip()
        st_cd     = (str(r.get("st_cd") or "").strip() or None)
        alloc_no  = (str(r.get("allocation_number") or "").strip() or None)
        remaining = float(r.get("do_qty") or 0)
        if not rdc or not art or remaining <= 0:
            continue

        if alloc_no:
            open_rows = conn.execute(text(f"""
                SELECT ID, BDC_QTY, DO_RECEIVED, STATUS, LAST_DO_AT
                FROM {BDC_HISTORY_TABLE}
                WHERE ALLOCATION_NUMBER = :alloc
                  AND RDC = :rdc AND ARTICLE_NUMBER = :art
                  AND STATUS <> 'CONFIRMED'
                ORDER BY BDC_DATE ASC, ID ASC
            """), {"alloc": alloc_no, "rdc": rdc, "art": art}).fetchall()
        elif st_cd:
            open_rows = conn.execute(text(f"""
                SELECT ID, BDC_QTY, DO_RECEIVED, STATUS, LAST_DO_AT
                FROM {BDC_HISTORY_TABLE}
                WHERE RDC = :rdc AND ARTICLE_NUMBER = :art
                  AND ISNULL(ST_CD,'') = :st AND STATUS <> 'CONFIRMED'
                ORDER BY BDC_DATE ASC, ID ASC
            """), {"rdc": rdc, "art": art, "st": st_cd}).fetchall()
        else:
            open_rows = conn.execute(text(f"""
                SELECT ID, BDC_QTY, DO_RECEIVED, STATUS, LAST_DO_AT
                FROM {BDC_HISTORY_TABLE}
                WHERE RDC = :rdc AND ARTICLE_NUMBER = :art
                  AND STATUS <> 'CONFIRMED'
                ORDER BY BDC_DATE ASC, ID ASC
            """), {"rdc": rdc, "art": art}).fetchall()

        for hist in open_rows:
            if remaining <= 0:
                break
            hid          = int(hist[0])
            bdc_qty      = float(hist[1] or 0)
            already      = float(hist[2] or 0)
            old_status   = hist[3] or "OPEN"
            old_last_do  = hist[4]
            need         = max(bdc_qty - already, 0)
            if need <= 0:
                continue
            apply_qty    = min(need, remaining)
            new_total    = already + apply_qty
            new_status   = "CONFIRMED" if new_total >= bdc_qty else "PARTIAL"
            conn.execute(text(f"""
                UPDATE {BDC_HISTORY_TABLE}
                   SET DO_RECEIVED = :got,
                       STATUS      = :st,
                       LAST_DO_AT  = GETDATE()
                 WHERE ID = :id
            """), {"got": new_total, "st": new_status, "id": hid})
            touched += 1
            remaining -= apply_qty
            history_updates.append({
                "history_id":      hid,
                "qty_added":       apply_qty,
                "old_do_received": already,
                "new_do_received": new_total,
                "old_status":      old_status,
                "new_status":      new_status,
                "old_last_do_at":  old_last_do.isoformat() if old_last_do else None,
            })
    conn.commit()
    return {"touched": touched, "history_updates": history_updates}


def stamp_bdc_qty(
    conn, article_rdc_pairs: Optional[List[Dict]] = None,
) -> List[Dict]:
    """Set BDC_QTY = current PEND_QTY and LAST_BDC_AT = now for open rows.

    Returns a list of {pend_alc_id, old_bdc_qty, old_last_bdc_at, new_bdc_qty}
    for every row touched — the caller persists this in the operations log
    so the BDC stamp can be reverted later.

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
            OUTPUT INSERTED.ID, DELETED.BDC_QTY, DELETED.LAST_BDC_AT,
                   INSERTED.BDC_QTY
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
            OUTPUT INSERTED.ID, DELETED.BDC_QTY, DELETED.LAST_BDC_AT,
                   INSERTED.BDC_QTY
            WHERE IS_CLOSED = 0 AND PEND_QTY > 0
        """)).fetchall()

    conn.commit()
    return [
        {
            "pend_alc_id":     int(r[0]),
            "old_bdc_qty":     float(r[1] or 0),
            "old_last_bdc_at": r[2].isoformat() if r[2] else None,
            "new_bdc_qty":     float(r[3] or 0),
        }
        for r in rows
    ]


def apply_do_deductions(conn, rows: List[Dict]) -> Dict:
    """Increment DO_QTY in ARS_PEND_ALC for each DO row using FIFO across
    multiple open session rows for the same (RDC, ST_CD, ARTICLE).

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
    """
    valid = [r for r in rows if float(r.get("do_qty", 0) or 0) > 0]
    if not valid:
        return {"touched": 0, "pend_updates": []}

    ensure_pend_alc_table(conn)
    touched = 0
    pend_updates: List[Dict] = []

    for r in valid:
        rdc       = str(r["rdc"]).strip()
        art       = str(r["article_number"]).strip()
        st_cd     = (str(r.get("st_cd") or "").strip() or None)
        do_num    = (str(r.get("do_number") or "").strip() or None)
        remaining = float(r["do_qty"])

        if st_cd:
            open_rows = conn.execute(text(f"""
                SELECT ID, ALLOC_QTY, DO_QTY, ISNULL(DO_NUMBER,'') AS DO_NUMBER, LAST_DO_AT
                FROM {PEND_ALC_TABLE}
                WHERE RDC = :rdc AND ARTICLE_NUMBER = :art
                  AND ISNULL(ST_CD,'') = :st AND IS_CLOSED = 0
                ORDER BY APPROVED_AT ASC, ID ASC
            """), {"rdc": rdc, "art": art, "st": st_cd}).fetchall()
        else:
            open_rows = conn.execute(text(f"""
                SELECT ID, ALLOC_QTY, DO_QTY, ISNULL(DO_NUMBER,'') AS DO_NUMBER, LAST_DO_AT
                FROM {PEND_ALC_TABLE}
                WHERE RDC = :rdc AND ARTICLE_NUMBER = :art
                  AND IS_CLOSED = 0
                ORDER BY APPROVED_AT ASC, ID ASC
            """), {"rdc": rdc, "art": art}).fetchall()

        for row in open_rows:
            if remaining <= 0:
                break
            row_id     = int(row[0])
            alloc_qty  = float(row[1] or 0)
            do_already = float(row[2] or 0)
            prev_do_at = row[4]
            need       = max(alloc_qty - do_already, 0)
            if need <= 0:
                continue
            apply_qty  = min(need, remaining)
            new_do     = do_already + apply_qty
            is_closed  = 1 if new_do >= alloc_qty else 0
            new_do_num = (
                ((row[3] + ", ") if row[3] else "") + do_num
                if do_num else (row[3] or None)
            )
            conn.execute(text(f"""
                UPDATE {PEND_ALC_TABLE}
                   SET DO_QTY         = :got,
                       LAST_DO_AT     = GETDATE(),
                       DO_UPLOADED_AT = GETDATE(),
                       DO_NUMBER      = :dn,
                       IS_CLOSED      = :cl
                 WHERE ID = :id
            """), {"got": new_do, "dn": new_do_num, "cl": is_closed, "id": row_id})
            touched   += 1
            remaining -= apply_qty
            pend_updates.append({
                "pend_alc_id":      row_id,
                "qty_added":        apply_qty,
                "was_just_closed":  bool(is_closed),
                "prev_last_do_at":  prev_do_at.isoformat() if prev_do_at else None,
            })

    # Hold tracking — single bulk update by (RDC, ARTICLE), unchanged from
    # before. Each (rdc, art) pair updates at most one HOLD row, so over-
    # counting isn't an issue here.
    tmp = f"#pa_do_{uuid.uuid4().hex[:8]}"
    try:
        conn.execute(text(
            f"CREATE TABLE {tmp} (rdc NVARCHAR(20), art NVARCHAR(30), qty FLOAT)"
        ))
        conn.execute(
            text(f"INSERT INTO {tmp} VALUES (:r, :a, :q)"),
            [{"r": str(r["rdc"]),
              "a": str(r["article_number"]),
              "q": float(r["do_qty"])} for r in valid]
        )
        conn.execute(text(f"""
            UPDATE H
               SET H.HOLD_REM  = CASE WHEN H.HOLD_REM - u.qty < 0 THEN 0
                                       ELSE H.HOLD_REM - u.qty END,
                   H.IS_CLOSED = CASE WHEN H.HOLD_REM - u.qty <= 0 THEN 1 ELSE 0 END
            FROM ARS_NL_TBL_HOLD_TRACKING H
            JOIN {tmp} u ON H.WERKS = u.rdc AND H.VAR_ART = u.art
            WHERE H.IS_CLOSED = 0
        """))
    except Exception as he:
        logger.warning(f"[pend_alc] hold tracking update skipped: {he}")
    finally:
        try:
            conn.execute(text(
                f"IF OBJECT_ID('tempdb..{tmp}') IS NOT NULL DROP TABLE {tmp}"
            ))
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


# Attribute-rollup grids that aggregate PEND_ALC at (WERKS, MAJ_CAT) level.
# Each table has columns (WERKS, MAJ_CAT, <attribute>, ..., PEND_ALC, ...).
# The incremental delta rolls up by (WERKS, MAJ_CAT) only — see implementation
# note 4 in the design doc for the precision trade-off.
_GRID_ROLLUP_TABLES = (
    "ARS_GRID_MJ",
    "ARS_GRID_MJ_CLR",
    "ARS_GRID_MJ_RNG_SEG",
    "ARS_GRID_MJ_FAB",
    "ARS_GRID_MJ_MACRO_MVGR",
    "ARS_GRID_MJ_MICRO_MVGR",
    "ARS_GRID_MJ_VND_CD",
    "ARS_GRID_MJ_M_VND_CD",
)


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

        # ── MSA: ARS_MSA_TOTAL — keyed on (RDC, ARTICLE_NUMBER) ─────────
        try:
            r1 = conn.execute(text(f"""
                UPDATE T SET
                    T.PEND_QTY = ISNULL(T.PEND_QTY, 0) + d.qty,
                    T.FNL_Q    = CASE
                        WHEN ISNULL(T.FNL_Q, 0) - d.qty < 0 THEN 0
                        ELSE ISNULL(T.FNL_Q, 0) - d.qty
                    END
                FROM ARS_MSA_TOTAL T
                JOIN (
                    SELECT rdc, art, SUM(qty) AS qty
                    FROM {tmp} GROUP BY rdc, art
                ) d ON T.RDC = d.rdc AND T.ARTICLE_NUMBER = d.art
            """))
            result["msa_total"] = int(r1.rowcount or 0)
        except Exception as e:
            logger.warning(f"[delta] MSA_TOTAL skipped: {e}")

        # ── MSA: ARS_MSA_VAR_ART — VAR_ART = ARTICLE_NUMBER ─────────────
        try:
            r2 = conn.execute(text(f"""
                UPDATE V SET
                    V.PEND_QTY = ISNULL(V.PEND_QTY, 0) + d.qty,
                    V.FNL_Q    = CASE
                        WHEN ISNULL(V.FNL_Q, 0) - d.qty < 0 THEN 0
                        ELSE ISNULL(V.FNL_Q, 0) - d.qty
                    END
                FROM ARS_MSA_VAR_ART V
                JOIN (
                    SELECT rdc, art, SUM(qty) AS qty
                    FROM {tmp} GROUP BY rdc, art
                ) d ON V.RDC = d.rdc AND V.VAR_ART = d.art
            """))
            result["msa_var_art"] = int(r2.rowcount or 0)
        except Exception as e:
            logger.warning(f"[delta] MSA_VAR_ART skipped: {e}")

        # ── MSA: ARS_MSA_GEN_ART — keyed on (RDC, MAJ_CAT, GEN_ART, CLR) ─
        try:
            r3 = conn.execute(text(f"""
                UPDATE G SET
                    G.PEND_QTY = ISNULL(G.PEND_QTY, 0) + d.qty,
                    G.FNL_Q    = CASE
                        WHEN ISNULL(G.FNL_Q, 0) - d.qty < 0 THEN 0
                        ELSE ISNULL(G.FNL_Q, 0) - d.qty
                    END
                FROM ARS_MSA_GEN_ART G
                JOIN (
                    SELECT rdc, maj_cat, gen_art, clr, SUM(qty) AS qty
                    FROM {tmp}
                    GROUP BY rdc, maj_cat, gen_art, clr
                ) d
                  ON G.RDC = d.rdc
                 AND ISNULL(G.MAJ_CAT, '')        = ISNULL(d.maj_cat, '')
                 AND ISNULL(G.GEN_ART_NUMBER, '') = ISNULL(d.gen_art, '')
                 AND ISNULL(G.CLR, '')            = ISNULL(d.clr, '')
            """))
            result["msa_gen_art"] = int(r3.rowcount or 0)
        except Exception as e:
            logger.warning(f"[delta] MSA_GEN_ART skipped: {e}")

        # ── Grid: ARS_GRID_MJ_VAR_ART — variant grain ───────────────────
        try:
            r4 = conn.execute(text(f"""
                UPDATE V SET V.PEND_ALC = ISNULL(V.PEND_ALC, 0) + d.qty
                FROM ARS_GRID_MJ_VAR_ART V
                JOIN (
                    SELECT st_cd, maj_cat, gen_art, clr, art, SUM(qty) AS qty
                    FROM {tmp}
                    GROUP BY st_cd, maj_cat, gen_art, clr, art
                ) d
                  ON V.WERKS = d.st_cd
                 AND ISNULL(V.MAJ_CAT, '')        = ISNULL(d.maj_cat, '')
                 AND ISNULL(V.GEN_ART_NUMBER, '') = ISNULL(d.gen_art, '')
                 AND ISNULL(V.CLR, '')            = ISNULL(d.clr, '')
                 AND V.ARTICLE_NUMBER             = d.art
            """))
            result["grid_var"] = int(r4.rowcount or 0)
        except Exception as e:
            logger.warning(f"[delta] GRID_VAR_ART skipped: {e}")

        # ── Grid: ARS_GRID_MJ_GEN_ART — gen_art grain ───────────────────
        try:
            r5 = conn.execute(text(f"""
                UPDATE G SET G.PEND_ALC = ISNULL(G.PEND_ALC, 0) + d.qty
                FROM ARS_GRID_MJ_GEN_ART G
                JOIN (
                    SELECT st_cd, maj_cat, gen_art, clr, SUM(qty) AS qty
                    FROM {tmp}
                    GROUP BY st_cd, maj_cat, gen_art, clr
                ) d
                  ON G.WERKS = d.st_cd
                 AND ISNULL(G.MAJ_CAT, '')        = ISNULL(d.maj_cat, '')
                 AND ISNULL(G.GEN_ART_NUMBER, '') = ISNULL(d.gen_art, '')
                 AND ISNULL(G.CLR, '')            = ISNULL(d.clr, '')
            """))
            result["grid_gen"] = int(r5.rowcount or 0)
        except Exception as e:
            logger.warning(f"[delta] GRID_GEN_ART skipped: {e}")

        # ── Grid: 8 attribute rollups — (WERKS, MAJ_CAT) only ────────────
        rollup_total = 0
        for tbl in _GRID_ROLLUP_TABLES:
            try:
                rr = conn.execute(text(f"""
                    UPDATE X SET X.PEND_ALC = ISNULL(X.PEND_ALC, 0) + d.qty
                    FROM [{tbl}] X
                    JOIN (
                        SELECT st_cd, maj_cat, SUM(qty) AS qty
                        FROM {tmp} GROUP BY st_cd, maj_cat
                    ) d
                      ON X.WERKS = d.st_cd
                     AND X.MAJ_CAT = d.maj_cat
                """))
                rollup_total += int(rr.rowcount or 0)
            except Exception as e:
                logger.debug(f"[delta] rollup grid {tbl} skipped: {e}")
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
        # 1a. Seed PEND_QTY into ARS_MSA_TOTAL from open pend_alc rows
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
            LEFT JOIN P ON P.RDC = T.RDC AND P.ARTICLE_NUMBER = T.ARTICLE_NUMBER
        """))
        result["msa_total"] = int(r1.rowcount or 0)

        # 1b. Roll up TOTAL → VAR_ART
        r2 = conn.execute(text("""
            UPDATE V
               SET V.PEND_QTY = agg.p, V.FNL_Q = agg.f
            FROM ARS_MSA_VAR_ART V
            JOIN (
                SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, ARTICLE_NUMBER,
                       SUM(CAST(PEND_QTY AS FLOAT)) AS p,
                       SUM(CAST(FNL_Q    AS FLOAT)) AS f
                FROM ARS_MSA_TOTAL
                GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, ARTICLE_NUMBER
            ) agg
              ON V.RDC = agg.RDC
             AND ISNULL(V.MAJ_CAT, '')        = ISNULL(agg.MAJ_CAT, '')
             AND ISNULL(V.GEN_ART_NUMBER, '') = ISNULL(agg.GEN_ART_NUMBER, '')
             AND ISNULL(V.CLR, '')            = ISNULL(agg.CLR, '')
             AND V.VAR_ART                    = agg.ARTICLE_NUMBER
        """))
        result["msa_var_art"] = int(r2.rowcount or 0)

        # 1c. Roll up TOTAL → GEN_ART
        r3 = conn.execute(text("""
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
             AND ISNULL(G.MAJ_CAT, '')        = ISNULL(agg.MAJ_CAT, '')
             AND ISNULL(G.GEN_ART_NUMBER, '') = ISNULL(agg.GEN_ART_NUMBER, '')
             AND ISNULL(G.CLR, '')            = ISNULL(agg.CLR, '')
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
        # 2a. ARS_GRID_MJ_VAR_ART — variant grain
        try:
            r1 = conn.execute(text(f"""
                UPDATE V
                   SET V.PEND_ALC = ISNULL(P.qty, 0)
                FROM ARS_GRID_MJ_VAR_ART V
                LEFT JOIN (
                    SELECT ST_CD, MAJ_CAT, GEN_ART_NUMBER, CLR, ARTICLE_NUMBER,
                           SUM(CAST(PEND_QTY AS FLOAT)) AS qty
                    FROM {PEND_ALC_TABLE} WHERE IS_CLOSED = 0
                    GROUP BY ST_CD, MAJ_CAT, GEN_ART_NUMBER, CLR, ARTICLE_NUMBER
                ) P
                  ON P.ST_CD = V.WERKS
                 AND ISNULL(P.MAJ_CAT, '')        = ISNULL(V.MAJ_CAT, '')
                 AND ISNULL(P.GEN_ART_NUMBER, '') = ISNULL(V.GEN_ART_NUMBER, '')
                 AND ISNULL(P.CLR, '')            = ISNULL(V.CLR, '')
                 AND P.ARTICLE_NUMBER              = V.ARTICLE_NUMBER
            """))
            result["grid_var"] = int(r1.rowcount or 0)
        except Exception as e:
            logger.debug(f"[bootstrap_grid] GRID_VAR_ART skipped: {e}")

        # 2b. ARS_GRID_MJ_GEN_ART — gen_art grain
        try:
            r2 = conn.execute(text(f"""
                UPDATE G
                   SET G.PEND_ALC = ISNULL(P.qty, 0)
                FROM ARS_GRID_MJ_GEN_ART G
                LEFT JOIN (
                    SELECT ST_CD, MAJ_CAT, GEN_ART_NUMBER, CLR,
                           SUM(CAST(PEND_QTY AS FLOAT)) AS qty
                    FROM {PEND_ALC_TABLE} WHERE IS_CLOSED = 0
                    GROUP BY ST_CD, MAJ_CAT, GEN_ART_NUMBER, CLR
                ) P
                  ON P.ST_CD = G.WERKS
                 AND ISNULL(P.MAJ_CAT, '')        = ISNULL(G.MAJ_CAT, '')
                 AND ISNULL(P.GEN_ART_NUMBER, '') = ISNULL(G.GEN_ART_NUMBER, '')
                 AND ISNULL(P.CLR, '')            = ISNULL(G.CLR, '')
            """))
            result["grid_gen"] = int(r2.rowcount or 0)
        except Exception as e:
            logger.debug(f"[bootstrap_grid] GRID_GEN_ART skipped: {e}")

        # 2c. 8 attribute-rollup grids — (WERKS, MAJ_CAT) precision
        rollup_total = 0
        for tbl in _GRID_ROLLUP_TABLES:
            try:
                rr = conn.execute(text(f"""
                    UPDATE X
                       SET X.PEND_ALC = ISNULL(P.qty, 0)
                    FROM [{tbl}] X
                    LEFT JOIN (
                        SELECT ST_CD, MAJ_CAT, SUM(CAST(PEND_QTY AS FLOAT)) AS qty
                        FROM {PEND_ALC_TABLE} WHERE IS_CLOSED = 0
                        GROUP BY ST_CD, MAJ_CAT
                    ) P
                      ON P.ST_CD = X.WERKS AND P.MAJ_CAT = X.MAJ_CAT
                """))
                rollup_total += int(rr.rowcount or 0)
            except Exception as e:
                logger.debug(f"[bootstrap_grid] {tbl} skipped: {e}")
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
