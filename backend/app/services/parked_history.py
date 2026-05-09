"""
parked_history.py

Park-then-promote history for /listing/generate runs.

After every successful listing run, the contents of the live working tables
are snapshotted into matching `*_PARKED` tables tagged with the run's
SESSION_ID. The user reviews those rows in the UI; on Approve they are
promoted to matching `*_HISTORY` tables (the permanent record); on Reject
they stay in `*_PARKED` with PARK_STATUS='REJECTED' for audit.

Five source tables are parked in lock-step:

    ARS_ALLOC_WORKING    →  ARS_ALLOC_PARKED            →  ARS_ALLOC_HISTORY
    ARS_LISTING_WORKING  →  ARS_LISTING_WORKING_PARKED  →  ARS_LISTING_WORKING_HISTORY
    ARS_LISTING          →  ARS_LISTING_PARKED          →  ARS_LISTING_HISTORY
    ARS_MSA_GEN_ART      →  ARS_MSA_GEN_ART_PARKED      →  ARS_MSA_GEN_ART_HISTORY
    ARS_MSA_VAR_ART      →  ARS_MSA_VAR_ART_PARKED      →  ARS_MSA_VAR_ART_HISTORY

Both tables drift their schema between runs (dynamic H_*, GH_*, ALLOC_FLAG,
PRI_CT%, SEC_CT%, … columns are added by the orchestrator). The parked /
history tables are auto-reconciled at every snapshot / approve via
INFORMATION_SCHEMA introspection — `ALTER TABLE … ADD <new col> NULL` is
issued for any column that's on the source but not yet on the target.

Approve and Reject are atomic across the two source-table-pairs: a single
SQLAlchemy connection is opened and the data movement for both targets is
committed once. Idempotency: a second Approve on the same SESSION_ID
returns {already_approved: True} without inserting duplicates (early
SELECT COUNT against each history table + a NOT EXISTS guard inside the
INSERT).
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine
from app.services.pend_alc_service import (
    write_pend_alc,
    adjust_msa_after_pend_insert,
    apply_pend_alc_delta_by_session,
    bootstrap_msa_hold_sync,
)


# ---------------------------------------------------------------------------
# Snapshot configuration — list of (source, parked, history) triples
# ---------------------------------------------------------------------------
# Each triple defines a source table whose contents get parked + promoted on
# Approve. Adding a third triple here is enough to make the whole pipeline
# (snapshot, approve, reject, purge, list, detail) cover it too.
_SNAPSHOT_TARGETS: List[Dict[str, str]] = [
    {
        "label":   "alloc",
        "source":  "ARS_ALLOC_WORKING",
        "parked":  "ARS_ALLOC_PARKED",
        "history": "ARS_ALLOC_HISTORY",
    },
    {
        "label":   "listing_working",
        "source":  "ARS_LISTING_WORKING",
        "parked":  "ARS_LISTING_WORKING_PARKED",
        "history": "ARS_LISTING_WORKING_HISTORY",
    },
    {
        "label":   "listing",
        "source":  "ARS_LISTING",
        "parked":  "ARS_LISTING_PARKED",
        "history": "ARS_LISTING_HISTORY",
    },
    {
        "label":   "msa_gen_art",
        "source":  "ARS_MSA_GEN_ART",
        "parked":  "ARS_MSA_GEN_ART_PARKED",
        "history": "ARS_MSA_GEN_ART_HISTORY",
    },
    {
        "label":   "msa_var_art",
        "source":  "ARS_MSA_VAR_ART",
        "parked":  "ARS_MSA_VAR_ART_PARKED",
        "history": "ARS_MSA_VAR_ART_HISTORY",
    },
]

SESSIONS_TABLE = "ARS_LISTING_SESSIONS"

# Parking-flow control columns added on top of the source-table columns.
# Order matters for the explicit INSERT column list.
_PARKED_CONTROL_COLS:  List[str] = ["SESSION_ID", "PARKED_AT", "PARK_STATUS"]
_HISTORY_CONTROL_COLS: List[str] = [
    "SESSION_ID", "PARKED_AT", "PARK_STATUS", "APPROVED_AT", "APPROVED_BY",
]

# Tables touched by /listing/generate (for the post-run sweep).
_AFFECTED_TABLES: List[str] = [
    "ARS_LISTING",
    "ARS_LISTING_WORKING",
    "ARS_LISTED_OPT",
    "ARS_ALLOC_WORKING",
    "ARS_MSA_TOTAL",
    "ARS_MSA_GEN_ART",
    "ARS_MSA_VAR_ART",
]

# Default TTL (days) — overridden by app_settings.json "allocation.history_retention_days".
TTL_PARKED_DAYS            = 14   # stale PARKED rows (never approved/rejected)
TTL_REJECTED_DAYS          = 30   # REJECTED parked rows
TTL_HISTORY_DAYS_DEFAULT   = 30   # approved history rows (0 = keep forever)


def _get_history_retention_days() -> int:
    """Return the approved-history retention window from app_settings.json.
    Falls back to TTL_HISTORY_DAYS_DEFAULT if the file is missing or the key
    is absent. 0 means keep forever."""
    try:
        from app.core.config import APP_SETTINGS_FILE
        if os.path.exists(APP_SETTINGS_FILE):
            with open(APP_SETTINGS_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            val = (cfg.get("allocation") or {}).get("history_retention_days")
            if val is not None:
                return max(0, int(val))
    except Exception:
        pass
    return TTL_HISTORY_DAYS_DEFAULT


def _get_target(label: str) -> Optional[Dict[str, str]]:
    for t in _SNAPSHOT_TARGETS:
        if t["label"] == label:
            return t
    return None


# ---------------------------------------------------------------------------
# DDL helpers (idempotent) — operate on a single (source, parked, history)
# triple so we can loop over _SNAPSHOT_TARGETS for everything below.
# ---------------------------------------------------------------------------
def _ensure_parked_table(conn, tgt: Dict[str, str]) -> None:
    """Create <tgt.parked> with the three control columns if it does not
    exist yet. Source-table columns are added by `_reconcile_parked_columns`
    just before each snapshot."""
    parked = tgt["parked"]
    conn.execute(text(f"""
        IF OBJECT_ID('dbo.{parked}','U') IS NULL
        CREATE TABLE dbo.{parked} (
            SESSION_ID   NVARCHAR(50)  NOT NULL,
            PARKED_AT    DATETIME      NOT NULL DEFAULT GETDATE(),
            PARK_STATUS  NVARCHAR(20)  NOT NULL DEFAULT 'PARKED'
        )
    """))
    conn.execute(text(f"""
        IF NOT EXISTS (SELECT 1 FROM sys.indexes
                       WHERE name='IX_{parked}_session_status'
                         AND object_id=OBJECT_ID('dbo.{parked}'))
        CREATE INDEX IX_{parked}_session_status
            ON dbo.{parked} (SESSION_ID, PARK_STATUS)
    """))
    conn.commit()


def _ensure_history_table(conn, tgt: Dict[str, str]) -> None:
    """Create <tgt.history> with the five control columns + indexes.

    NOTE: SESSION_ID is **not** unique here. Each history table is row-grain
    (one row per allocation / one row per working row), so an approved
    session contributes thousands of rows that all share the same
    SESSION_ID. A UNIQUE constraint on SESSION_ID alone would reject every
    row past the first, breaking the very first approval. Duplicate-approval
    protection lives in `approve_parked` (early SELECT COUNT + NOT EXISTS
    guard inside the INSERT).
    """
    history = tgt["history"]
    conn.execute(text(f"""
        IF OBJECT_ID('dbo.{history}','U') IS NULL
        CREATE TABLE dbo.{history} (
            SESSION_ID   NVARCHAR(50)  NOT NULL,
            PARKED_AT    DATETIME      NOT NULL,
            PARK_STATUS  NVARCHAR(20)  NOT NULL,
            APPROVED_AT  DATETIME      NOT NULL,
            APPROVED_BY  NVARCHAR(200) NOT NULL
        )
    """))
    # Migration: drop the broken unique index from older deployments before
    # creating its non-unique replacement. Idempotent — no-op when absent.
    conn.execute(text(f"""
        IF EXISTS (SELECT 1 FROM sys.indexes
                   WHERE name='UX_{history}_session'
                     AND object_id=OBJECT_ID('dbo.{history}'))
        DROP INDEX UX_{history}_session ON dbo.{history}
    """))
    conn.execute(text(f"""
        IF NOT EXISTS (SELECT 1 FROM sys.indexes
                       WHERE name='IX_{history}_session'
                         AND object_id=OBJECT_ID('dbo.{history}'))
        CREATE INDEX IX_{history}_session
            ON dbo.{history} (SESSION_ID)
    """))
    conn.execute(text(f"""
        IF NOT EXISTS (SELECT 1 FROM sys.indexes
                       WHERE name='IX_{history}_approved_at'
                         AND object_id=OBJECT_ID('dbo.{history}'))
        CREATE INDEX IX_{history}_approved_at
            ON dbo.{history} (APPROVED_AT DESC)
    """))
    conn.commit()


def _get_columns_with_types(conn, table_name: str) -> List[Dict[str, Any]]:
    """Return [{name, dtype_sql}, …] for every column in `table_name`.
    Empty list if the table doesn't exist."""
    rows = conn.execute(text("""
        SELECT COLUMN_NAME,
               DATA_TYPE,
               CHARACTER_MAXIMUM_LENGTH,
               NUMERIC_PRECISION,
               NUMERIC_SCALE
          FROM INFORMATION_SCHEMA.COLUMNS
         WHERE TABLE_NAME = :t
         ORDER BY ORDINAL_POSITION
    """), {"t": table_name}).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        name, dt, ml, prec, scale = r[0], (r[1] or "").upper(), r[2], r[3], r[4]
        if dt in ("NVARCHAR", "VARCHAR", "NCHAR", "CHAR"):
            length = "MAX" if (ml is None or int(ml) < 0) else str(int(ml))
            dtype_sql = f"{dt}({length})"
        elif dt in ("DECIMAL", "NUMERIC"):
            dtype_sql = f"{dt}({prec or 18},{scale or 2})"
        else:
            dtype_sql = dt
        out.append({"name": name, "dtype_sql": dtype_sql})
    return out


def _reconcile_parked_columns(conn, tgt: Dict[str, str],
                              source_cols: List[Dict[str, Any]]
                              ) -> List[str]:
    """Make sure <tgt.parked> has every column in `source_cols`. Adds
    missing ones as NULLABLE. Returns the list of source column names (in
    source order) for the explicit INSERT column list."""
    parked = tgt["parked"]
    parked_existing = {c["name"].upper(): c["name"]
                       for c in _get_columns_with_types(conn, parked)}
    control_upper = {x.upper() for x in _PARKED_CONTROL_COLS}
    for c in source_cols:
        if c["name"].upper() in control_upper:
            # Don't let a source column collide with our control columns.
            continue
        if c["name"].upper() not in parked_existing:
            try:
                conn.execute(text(
                    f"ALTER TABLE [{parked}] "
                    f"ADD [{c['name']}] {c['dtype_sql']} NULL"
                ))
                conn.commit()
                logger.debug(
                    f"[parked_history] reconcile: added "
                    f"{parked}.{c['name']} ({c['dtype_sql']})"
                )
            except Exception as e:
                # Race-safe: column added concurrently between check and ALTER.
                logger.debug(
                    f"[parked_history] reconcile add skipped for "
                    f"{c['name']} on {parked}: {e}"
                )
    return [c["name"] for c in source_cols
            if c["name"].upper() not in control_upper]


def _reconcile_history_columns(conn, tgt: Dict[str, str],
                               parked_col_names: List[str]) -> List[str]:
    """Mirror parked-table columns into <tgt.history>. Returns the list of
    column names to copy on Approve (excluding control cols that the SELECT
    supplies explicitly)."""
    parked  = tgt["parked"]
    history = tgt["history"]
    parked_types = {c["name"].upper(): c
                    for c in _get_columns_with_types(conn, parked)}
    history_existing = {c["name"].upper(): c["name"]
                        for c in _get_columns_with_types(conn, history)}
    control_upper = {x.upper() for x in _HISTORY_CONTROL_COLS}
    payload_cols: List[str] = []
    for name in parked_col_names:
        if name.upper() in control_upper:
            continue
        payload_cols.append(name)
        if name.upper() not in history_existing:
            src = parked_types.get(name.upper())
            if not src:
                continue
            try:
                conn.execute(text(
                    f"ALTER TABLE [{history}] "
                    f"ADD [{name}] {src['dtype_sql']} NULL"
                ))
                conn.commit()
            except Exception as e:
                logger.debug(
                    f"[parked_history] history reconcile skipped for "
                    f"{name} on {history}: {e}"
                )
    return payload_cols


# ---------------------------------------------------------------------------
# Snapshot — called from listing.py right after Part 8 finishes
# ---------------------------------------------------------------------------
def _snapshot_one_target(session_id: str, tgt: Dict[str, str]
                         ) -> Dict[str, Any]:
    """Park one source table for this session. Returns
    {parked_rows, skipped, error}. Never raises."""
    label  = tgt["label"]
    source = tgt["source"]
    parked = tgt["parked"]
    engine = get_data_engine()
    try:
        with engine.connect() as conn:
            _ensure_parked_table(conn, tgt)

            # Idempotency: rows already parked for this session?
            already = conn.execute(text(
                f"SELECT COUNT(*) FROM {parked} WHERE SESSION_ID = :sid"
            ), {"sid": session_id}).scalar() or 0
            if already > 0:
                logger.info(
                    f"[parked_history:{label}] session {session_id} "
                    f"already parked ({already} rows) — skip"
                )
                return {"parked_rows": int(already), "skipped": True,
                        "error": None}

            source_cols = _get_columns_with_types(conn, source)
            if not source_cols:
                msg = f"{source} does not exist — nothing to park"
                logger.warning(f"[parked_history:{label}] {msg}")
                return {"parked_rows": 0, "skipped": True, "error": msg}

            source_count = conn.execute(text(
                f"SELECT COUNT(*) FROM [{source}]"
            )).scalar() or 0
            if source_count == 0:
                logger.info(
                    f"[parked_history:{label}] {source} is empty for "
                    f"session {session_id} — nothing to park"
                )
                return {"parked_rows": 0, "skipped": False, "error": None}

            payload_cols = _reconcile_parked_columns(conn, tgt, source_cols)
            cols_sql = ", ".join(f"[{c}]" for c in payload_cols)
            sql = (
                f"INSERT INTO [{parked}] "
                f"({cols_sql}, [SESSION_ID], [PARKED_AT], [PARK_STATUS]) "
                f"SELECT {cols_sql}, :sid, GETDATE(), 'PARKED' "
                f"FROM [{source}]"
            )
            res = conn.execute(text(sql), {"sid": session_id})
            conn.commit()
            inserted = int(res.rowcount or 0)
            logger.info(
                f"[parked_history:{label}] parked {inserted} rows from "
                f"{source} for session {session_id}"
            )
            return {"parked_rows": inserted, "skipped": False, "error": None}
    except Exception as e:
        logger.exception(
            f"[parked_history:{label}] snapshot failed for {session_id}"
        )
        return {"parked_rows": 0, "skipped": False, "error": str(e)}


def snapshot_session_to_parked(session_id: str) -> Dict[str, Any]:
    """Park every configured source table for this session. Returns
    {by_table: {label: {parked_rows, skipped, error}},
     total_parked_rows, any_error, any_parked}.

    Never raises — caller treats parking as bookkeeping."""
    by_table: Dict[str, Dict[str, Any]] = {}
    total = 0
    any_error = False
    any_parked = False
    for tgt in _SNAPSHOT_TARGETS:
        r = _snapshot_one_target(session_id, tgt)
        by_table[tgt["label"]] = r
        total += int(r.get("parked_rows", 0) or 0)
        if r.get("error"):
            any_error = True
        if (not r.get("skipped")) and int(r.get("parked_rows", 0) or 0) > 0:
            any_parked = True
    return {"by_table": by_table,
            "total_parked_rows": total,
            "any_error": any_error,
            "any_parked": any_parked}


# Backward-compatible wrapper for the old single-target call site.
def snapshot_alloc_to_parked(session_id: str) -> Dict[str, Any]:
    """DEPRECATED — kept for callers that haven't migrated to
    snapshot_session_to_parked yet. Returns the alloc subset of the new
    aggregate result."""
    aggregate = snapshot_session_to_parked(session_id)
    alloc = aggregate["by_table"].get("alloc", {})
    return {
        "parked_rows": alloc.get("parked_rows", 0),
        "skipped":     alloc.get("skipped", False),
        "error":       alloc.get("error"),
    }


# ---------------------------------------------------------------------------
# Approve / Reject — atomic across all configured targets
# ---------------------------------------------------------------------------
def _promote_one_within_conn(conn, tgt: Dict[str, str],
                             session_id: str, user: str) -> int:
    """Inside an existing connection: INSERT parked → history + DELETE from
    parked for one target. Returns the row count promoted. Caller is
    responsible for committing."""
    parked  = tgt["parked"]
    history = tgt["history"]
    label   = tgt["label"]

    # Anything to promote?
    parked_count = conn.execute(text(
        f"SELECT COUNT(*) FROM {parked} "
        f"WHERE SESSION_ID = :sid AND PARK_STATUS = 'PARKED'"
    ), {"sid": session_id}).scalar() or 0
    if parked_count == 0:
        return 0

    parked_cols = [
        c["name"]
        for c in _get_columns_with_types(conn, parked)
        if c["name"].upper() not in
            {x.upper() for x in _HISTORY_CONTROL_COLS}
    ]
    payload = _reconcile_history_columns(conn, tgt, parked_cols)
    cols_sql = ", ".join(f"[{c}]" for c in payload)

    # NOT EXISTS guard against races with another concurrent Approve.
    sql = (
        f"INSERT INTO [{history}] "
        f"({cols_sql}, [SESSION_ID], [PARKED_AT], [PARK_STATUS], "
        f" [APPROVED_AT], [APPROVED_BY]) "
        f"SELECT {cols_sql}, [SESSION_ID], [PARKED_AT], 'PARKED', "
        f"       GETDATE(), :who "
        f"FROM [{parked}] P "
        f"WHERE P.[SESSION_ID] = :sid AND P.[PARK_STATUS] = 'PARKED' "
        f"  AND NOT EXISTS ("
        f"    SELECT 1 FROM [{history}] H WHERE H.[SESSION_ID] = :sid"
        f"  )"
    )
    res = conn.execute(
        text(sql), {"sid": session_id, "who": (user or "")[:200]}
    )
    inserted = int(res.rowcount or 0)
    conn.execute(text(
        f"DELETE FROM [{parked}] "
        f"WHERE SESSION_ID = :sid AND PARK_STATUS = 'PARKED'"
    ), {"sid": session_id})
    logger.info(
        f"[parked_history:{label}] approved {inserted} rows for "
        f"session {session_id}"
    )
    return inserted


def approve_parked(session_id: str, user: str) -> Dict[str, Any]:
    """Promote a parked session into history for every configured target.

    Atomic: alloc and listing snapshots either both move to history or
    neither does. Idempotent: a duplicate call returns
    {already_approved: True} without inserting duplicates.

    Returns {approved_rows, by_table, already_approved, error}.
    """
    engine = get_data_engine()
    try:
        # 1. Ensure all schemas exist (each helper commits internally).
        with engine.connect() as conn:
            for tgt in _SNAPSHOT_TARGETS:
                _ensure_parked_table(conn, tgt)
                _ensure_history_table(conn, tgt)

        # 2. Idempotency check across every target.
        already_in_history: Dict[str, int] = {}
        with engine.connect() as conn:
            for tgt in _SNAPSHOT_TARGETS:
                cnt = conn.execute(text(
                    f"SELECT COUNT(*) FROM {tgt['history']} "
                    f"WHERE SESSION_ID = :sid"
                ), {"sid": session_id}).scalar() or 0
                if cnt > 0:
                    already_in_history[tgt["label"]] = int(cnt)

        # If every configured target already has rows in history → fully
        # approved; return the existing counts.
        if len(already_in_history) == len(_SNAPSHOT_TARGETS):
            total = sum(already_in_history.values())
            return {
                "approved_rows":    total,
                "by_table":         already_in_history,
                "already_approved": True,
                "error":            None,
            }

        # 3. Promote any target that doesn't yet have history rows. Single
        #    connection + single commit for atomicity. Targets already in
        #    history are skipped (this handles the upgrade case where an
        #    older deployment had only alloc parking).
        approved_by_table: Dict[str, int] = {}
        with engine.connect() as conn:
            for tgt in _SNAPSHOT_TARGETS:
                if tgt["label"] in already_in_history:
                    approved_by_table[tgt["label"]] = 0
                    continue
                approved_by_table[tgt["label"]] = _promote_one_within_conn(
                    conn, tgt, session_id, user
                )
            conn.commit()

            # Write approved ALLOC_QTY to ARS_PEND_ALC so MSA can deduct
            # pending-but-not-yet-DO'd units from available stock.
            try:
                pend_rows = write_pend_alc(conn, session_id)
                logger.info(
                    f"[pend_alc] session={session_id}: "
                    f"{pend_rows} rows written to ARS_PEND_ALC"
                )
                approved_by_table["pend_alc_rows"] = pend_rows
            except Exception as pe:
                logger.warning(f"[pend_alc] write failed for {session_id}: {pe}")
                approved_by_table["pend_alc_rows"] = 0

            # Snapshot ARS_NL_TBL_HOLD_TRACKING BEFORE we modify it so any
            # future "undo approve" tooling can restore the pre-approval
            # state. Idempotent — second call for the same session is a no-op.
            try:
                _ensure_hold_snapshot_tables(conn)
                already = conn.execute(text(
                    f"SELECT COUNT(*) FROM [{_HOLD_SNAPSHOT_SESSIONS}] "
                    f"WHERE SESSION_ID = :sid"
                ), {"sid": session_id}).scalar() or 0
                if already == 0:
                    res = conn.execute(text(f"""
                        INSERT INTO [{_HOLD_SNAPSHOT_TABLE}]
                            (SESSION_ID, WERKS, RDC, MAJ_CAT, GEN_ART_NUMBER, CLR,
                             VAR_ART, SZ, OPT_STATUS, LISTED_DATE,
                             HOLD_QTY_INITIAL, HOLD_REM,
                             LAST_UPDATED, IS_CLOSED, CLOSED_DATE)
                        SELECT :sid, WERKS, RDC, MAJ_CAT, GEN_ART_NUMBER, CLR,
                               VAR_ART, SZ, OPT_STATUS, LISTED_DATE,
                               HOLD_QTY_INITIAL, HOLD_REM,
                               LAST_UPDATED, IS_CLOSED, CLOSED_DATE
                        FROM [ARS_NL_TBL_HOLD_TRACKING]
                    """), {"sid": session_id})
                    conn.execute(text(f"""
                        INSERT INTO [{_HOLD_SNAPSHOT_SESSIONS}]
                            (SESSION_ID, SNAPSHOTTED_AT, ROW_COUNT)
                        VALUES (:sid, GETDATE(), :rc)
                    """), {"sid": session_id, "rc": int(res.rowcount or 0)})
                    conn.commit()
            except Exception as _se:
                logger.warning(f"[hold] pre-approve snapshot failed: {_se}")

            # Apply the hold-tracking changes (Step A: decrement consumed
            # by RL/TBC, Step B: MERGE new TBL holds). Reads from
            # ARS_ALLOC_HISTORY for this session — symmetric with how
            # write_pend_alc reads ALLOC_QTY from history. Used to live in
            # listing.py Part 8.6 (during generate); moved here so HOLD
            # commits at the same lifecycle event as PEND.
            try:
                hold_applied = _apply_hold_tracking_from_history(conn, session_id)
                logger.info(
                    f"[hold] tracking applied session={session_id}: "
                    f"step_a={hold_applied['step_a_rows']} "
                    f"step_b={hold_applied['step_b_rows']}"
                )
                approved_by_table["hold_tracking_applied"] = hold_applied
            except Exception as he:
                logger.warning(f"[hold] tracking apply failed (non-fatal): {he}")
                approved_by_table["hold_tracking_applied"] = {"error": str(he)}

            # Immediately adjust MSA + Grid so the next alloc run sees the
            # updated available stock without requiring a full MSA / grid
            # rebuild. Symmetric +1 delta — the revert path passes -1.
            try:
                msa_adjusted = apply_pend_alc_delta_by_session(
                    conn, session_id, sign=+1,
                )
                logger.info(
                    f"[pend_alc] msa_adjusted session={session_id}: "
                    f"total={msa_adjusted['msa_total']} "
                    f"var={msa_adjusted['msa_var_art']} gen={msa_adjusted['msa_gen_art']}"
                )
                approved_by_table["msa_adjusted"] = msa_adjusted
            except Exception as mp:
                logger.warning(f"[pend_alc] msa_adjusted failed (non-fatal): {mp}")
                approved_by_table["msa_adjusted"] = {"error": str(mp)}

            # Sync MSA HOLD_QTY/FNL_Q to current ARS_NL_TBL_HOLD_TRACKING.
            # Hold tracking was already updated during listing generate
            # (Part 8.6); this defensive call ensures MSA reflects it even
            # if the listing-side hook was skipped or this is a re-approval.
            # Idempotent — same call with no changes is a no-op.
            try:
                hold_synced = bootstrap_msa_hold_sync(conn)
                logger.info(
                    f"[hold] msa hold synced session={session_id}: "
                    f"total={hold_synced['msa_total']} "
                    f"var={hold_synced['msa_var_art']} gen={hold_synced['msa_gen_art']}"
                )
                approved_by_table["msa_hold_synced"] = hold_synced
            except Exception as he:
                logger.warning(f"[hold] msa hold sync failed (non-fatal): {he}")
                approved_by_table["msa_hold_synced"] = {"error": str(he)}

        # Sum new + pre-existing for the headline number.
        # Skip non-int values (e.g. msa_patch dict, pend_alc_rows dict).
        total = (
            sum(v for v in approved_by_table.values() if isinstance(v, int)) +
            sum(already_in_history.values())
        )
        # Merge for the by_table response so the caller always sees both.
        merged: Dict[str, int] = {**already_in_history, **approved_by_table}
        return {
            "approved_rows":    total,
            "by_table":         merged,
            "already_approved": False,
            "error":            None,
        }
    except Exception as e:
        logger.exception(f"[parked_history] approve failed for {session_id}")
        return {
            "approved_rows":    0,
            "by_table":         {},
            "already_approved": False,
            "error":            str(e),
        }


def reject_parked(session_id: str, user: str,
                  note: Optional[str] = None) -> Dict[str, Any]:
    """Delete every parked target row for this session AND revert
    ARS_NL_TBL_HOLD_TRACKING to its pre-run state. Atomic across
    targets. The audit_log entry preserves the reject event.

    Returns {rejected_rows, by_table, hold_revert, error}.
    """
    engine = get_data_engine()
    rejected_by_table: Dict[str, Any] = {}
    try:
        with engine.connect() as conn:
            # 1. Delete parked snapshot rows for all configured targets.
            for tgt in _SNAPSHOT_TARGETS:
                res = conn.execute(text(
                    f"DELETE FROM [{tgt['parked']}] "
                    f" WHERE SESSION_ID = :sid AND PARK_STATUS = 'PARKED'"
                ), {"sid": session_id})
                rejected_by_table[tgt["label"]] = int(res.rowcount or 0)

            # 2. Revert ARS_NL_TBL_HOLD_TRACKING to its pre-run snapshot.
            _ensure_hold_snapshot_tables(conn)
            hold_revert = _revert_hold_tracking(conn, session_id)
            rejected_by_table["hold_revert"] = hold_revert

            conn.commit()

        # Best-effort audit_log row in the System DB.
        try:
            from app.database.session import get_system_engine
            with get_system_engine().connect() as sysc:
                sysc.execute(text("""
                    INSERT INTO audit_log
                        (username, action, resource_type, resource_id,
                         notes, created_at)
                    VALUES (:u, 'REJECT_PARKED_ALLOC', 'parked_session',
                            :sid, :note, GETDATE())
                """), {"u": (user or "")[:200], "sid": session_id,
                       "note": (note or "")[:1000]})
                sysc.commit()
        except Exception:
            pass

        total = sum(v for k, v in rejected_by_table.items()
                    if k != "hold_revert" and isinstance(v, int))
        logger.info(
            f"[parked_history] rejected session {session_id} "
            f"({total} rows total, hold_revert={hold_revert}, by {user})"
        )
        return {
            "rejected_rows": total,
            "by_table":      rejected_by_table,
            "hold_revert":   hold_revert,
            "error":         None,
        }
    except Exception as e:
        logger.exception(f"[parked_history] reject failed for {session_id}")
        return {
            "rejected_rows": 0,
            "by_table":      rejected_by_table,
            "hold_revert":   {},
            "error":         str(e),
        }


# ---------------------------------------------------------------------------
# Read-side
# ---------------------------------------------------------------------------
def list_parked_runs(include_rejected: bool = False) -> List[Dict[str, Any]]:
    """Return one row per parked SESSION_ID with row counts from BOTH
    parked tables, joined with ARS_LISTING_SESSIONS metadata. Default
    filter: only PARK_STATUS='PARKED'."""
    status_clause = "" if include_rejected else "WHERE PARK_STATUS = 'PARKED'"
    alloc_p   = _get_target("alloc")["parked"]          # ARS_ALLOC_PARKED
    listing_p = _get_target("listing_working")["parked"] # ARS_LISTING_WORKING_PARKED

    engine = get_data_engine()
    with engine.connect() as conn:
        for tgt in _SNAPSHOT_TARGETS:
            _ensure_parked_table(conn, tgt)
        rows = conn.execute(text(f"""
            ;WITH alloc_p AS (
                SELECT SESSION_ID,
                       MAX(PARK_STATUS) AS park_status,
                       MAX(PARKED_AT)   AS parked_at,
                       COUNT(*)         AS parked_rows
                  FROM {alloc_p}
                  {status_clause}
                 GROUP BY SESSION_ID
            ),
            listing_p AS (
                SELECT SESSION_ID,
                       MAX(PARK_STATUS) AS park_status,
                       MAX(PARKED_AT)   AS parked_at,
                       COUNT(*)         AS parked_rows
                  FROM {listing_p}
                  {status_clause}
                 GROUP BY SESSION_ID
            )
            SELECT
                COALESCE(a.SESSION_ID, l.SESSION_ID)            AS session_id,
                COALESCE(a.park_status, l.park_status)          AS park_status,
                COALESCE(a.parked_at,   l.parked_at)            AS parked_at,
                ISNULL(a.parked_rows, 0)                        AS alloc_parked_rows,
                ISNULL(l.parked_rows, 0)                        AS listing_parked_rows,
                s.STARTED_AT                                    AS started_at,
                s.COMPLETED_AT                                  AS completed_at,
                s.USER_NAME                                     AS user_name,
                s.STATUS                                        AS run_status,
                s.ALLOC_ROWS                                    AS alloc_rows,
                s.SHIP_QTY_TOTAL                                AS ship_qty_total,
                s.HOLD_QTY_TOTAL                                AS hold_qty_total
            FROM alloc_p a
            FULL OUTER JOIN listing_p l ON a.SESSION_ID = l.SESSION_ID
            LEFT JOIN {SESSIONS_TABLE} s
                   ON s.SESSION_ID = COALESCE(a.SESSION_ID, l.SESSION_ID)
            ORDER BY COALESCE(a.parked_at, l.parked_at) DESC
        """)).fetchall()
    return [
        {
            "session_id":          r[0],
            "park_status":         r[1],
            "parked_at":           r[2].isoformat() if r[2] else None,
            "alloc_parked_rows":   int(r[3]) if r[3] is not None else 0,
            "listing_parked_rows": int(r[4]) if r[4] is not None else 0,
            # Back-compat: existing UI expects `parked_rows`. Surface the
            # alloc count there since alloc was the original scope.
            "parked_rows":         int(r[3]) if r[3] is not None else 0,
            "started_at":          r[5].isoformat() if r[5] else None,
            "completed_at":        r[6].isoformat() if r[6] else None,
            "user_name":           r[7],
            "run_status":          r[8],
            "alloc_rows":          int(r[9])  if r[9]  is not None else None,
            "ship_qty_total":      float(r[10]) if r[10] is not None else None,
            "hold_qty_total":      float(r[11]) if r[11] is not None else None,
        }
        for r in rows
    ]


def get_parked_detail(session_id: str, page: int = 1,
                      page_size: int = 100,
                      which: str = "alloc") -> Dict[str, Any]:
    """Paginated detail rows for one parked session. `which` selects which
    parked table to read: 'alloc' (default, ARS_ALLOC_PARKED) or 'listing'
    (ARS_LISTING_WORKING_PARKED). Column list is whatever the parked table
    has at read time."""
    page = max(1, int(page))
    page_size = max(1, min(5000, int(page_size)))
    offset = (page - 1) * page_size

    tgt = _get_target((which or "alloc").lower())
    if tgt is None:
        return {"columns": [], "rows": [], "total": 0,
                "page": page, "page_size": page_size,
                "which": which, "error": f"unknown which='{which}'"}
    parked = tgt["parked"]

    engine = get_data_engine()
    with engine.connect() as conn:
        _ensure_parked_table(conn, tgt)
        cols = [c["name"] for c in _get_columns_with_types(conn, parked)]
        if not cols:
            return {"columns": [], "rows": [], "total": 0,
                    "page": page, "page_size": page_size, "which": which}
        cols_sql = ", ".join(f"[{c}]" for c in cols)
        total = int(conn.execute(text(
            f"SELECT COUNT(*) FROM {parked} WHERE SESSION_ID = :sid"
        ), {"sid": session_id}).scalar() or 0)
        rows = conn.execute(text(f"""
            SELECT {cols_sql}
              FROM {parked}
             WHERE SESSION_ID = :sid
             ORDER BY PARKED_AT
             OFFSET :off ROWS FETCH NEXT :lim ROWS ONLY
        """), {"sid": session_id, "off": offset, "lim": page_size}).fetchall()
    data = [
        {c: (v.isoformat() if hasattr(v, "isoformat") else v)
         for c, v in zip(cols, r)}
        for r in rows
    ]
    return {"columns": cols, "rows": data, "total": total,
            "page": page, "page_size": page_size, "which": which}


def _list_history(tgt: Dict[str, str],
                  session_id: Optional[str] = None,
                  date_from: Optional[str] = None,
                  date_to:   Optional[str] = None,
                  page: int = 1, page_size: int = 100
                  ) -> Dict[str, Any]:
    """Internal: paginated read from one history table."""
    page = max(1, int(page))
    page_size = max(1, min(5000, int(page_size)))
    offset = (page - 1) * page_size
    history = tgt["history"]

    where: List[str] = []
    params: Dict[str, Any] = {"off": offset, "lim": page_size}
    if session_id:
        where.append("SESSION_ID = :sid"); params["sid"] = session_id
    if date_from:
        where.append("APPROVED_AT >= :fr"); params["fr"] = date_from
    if date_to:
        where.append("APPROVED_AT <= :to"); params["to"] = date_to
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    engine = get_data_engine()
    with engine.connect() as conn:
        _ensure_history_table(conn, tgt)
        cols = [c["name"] for c in _get_columns_with_types(conn, history)]
        if not cols:
            return {"columns": [], "rows": [], "total": 0,
                    "page": page, "page_size": page_size}
        cols_sql = ", ".join(f"[{c}]" for c in cols)
        total = int(conn.execute(text(
            f"SELECT COUNT(*) FROM {history} {where_sql}"
        ), params).scalar() or 0)
        rows = conn.execute(text(f"""
            SELECT {cols_sql}
              FROM {history}
              {where_sql}
             ORDER BY APPROVED_AT DESC
             OFFSET :off ROWS FETCH NEXT :lim ROWS ONLY
        """), params).fetchall()
    data = [
        {c: (v.isoformat() if hasattr(v, "isoformat") else v)
         for c, v in zip(cols, r)}
        for r in rows
    ]
    return {"columns": cols, "rows": data, "total": total,
            "page": page, "page_size": page_size}


def list_alloc_history(session_id: Optional[str] = None,
                       date_from: Optional[str] = None,
                       date_to:   Optional[str] = None,
                       page: int = 1, page_size: int = 100
                       ) -> Dict[str, Any]:
    """Query approved alloc history (ARS_ALLOC_HISTORY)."""
    return _list_history(_get_target("alloc"),
                         session_id=session_id,
                         date_from=date_from, date_to=date_to,
                         page=page, page_size=page_size)


def list_listing_history(session_id: Optional[str] = None,
                         date_from: Optional[str] = None,
                         date_to:   Optional[str] = None,
                         page: int = 1, page_size: int = 100
                         ) -> Dict[str, Any]:
    """Query approved listing-working history
    (ARS_LISTING_WORKING_HISTORY)."""
    return _list_history(_get_target("listing_working"),
                         session_id=session_id,
                         date_from=date_from, date_to=date_to,
                         page=page, page_size=page_size)


# ---------------------------------------------------------------------------
# TTL purge — across every configured parked target
# ---------------------------------------------------------------------------
def purge_old_parked() -> Dict[str, Any]:
    """Delete PARKED rows older than TTL_PARKED_DAYS and REJECTED rows
    older than TTL_REJECTED_DAYS, across every configured parked target.
    Also purges approved history rows older than history_retention_days
    (0 = keep forever)."""
    engine = get_data_engine()
    by_table: Dict[str, Dict[str, int]] = {}
    total_parked = 0
    total_rejected = 0
    with engine.connect() as conn:
        for tgt in _SNAPSHOT_TARGETS:
            _ensure_parked_table(conn, tgt)
            r1 = conn.execute(text(f"""
                DELETE FROM {tgt['parked']}
                 WHERE PARK_STATUS = 'PARKED'
                   AND PARKED_AT < DATEADD(day, -:n, GETDATE())
            """), {"n": TTL_PARKED_DAYS})
            d_p = int(r1.rowcount or 0)
            r2 = conn.execute(text(f"""
                DELETE FROM {tgt['parked']}
                 WHERE PARK_STATUS = 'REJECTED'
                   AND PARKED_AT < DATEADD(day, -:n, GETDATE())
            """), {"n": TTL_REJECTED_DAYS})
            d_r = int(r2.rowcount or 0)
            by_table[tgt["label"]] = {
                "deleted_parked":   d_p,
                "deleted_rejected": d_r,
            }
            total_parked += d_p
            total_rejected += d_r
        conn.commit()
    logger.info(
        f"[parked_history] purge: parked={total_parked} "
        f"rejected={total_rejected} by_table={by_table}"
    )
    # Purge approved history rows beyond the retention window.
    history_result = purge_old_history()
    return {
        "deleted_parked":   total_parked,
        "deleted_rejected": total_rejected,
        "by_table":         by_table,
        "history_purge":    history_result,
    }


def purge_old_history() -> Dict[str, Any]:
    """Delete approved history rows older than history_retention_days
    (read from app_settings.json → allocation.history_retention_days).
    Returns {deleted_total, by_table, retention_days}. No-op when retention_days=0."""
    retention = _get_history_retention_days()
    if retention <= 0:
        return {"deleted_total": 0, "by_table": {}, "retention_days": 0}

    engine = get_data_engine()
    by_table: Dict[str, int] = {}
    total = 0
    try:
        with engine.connect() as conn:
            for tgt in _SNAPSHOT_TARGETS:
                hist = tgt["history"]
                # Skip if history table doesn't exist yet.
                exists = conn.execute(text(
                    f"SELECT CASE WHEN OBJECT_ID('dbo.{hist}','U') IS NULL "
                    f"THEN 0 ELSE 1 END"
                )).scalar() or 0
                if not exists:
                    by_table[tgt["label"]] = 0
                    continue
                res = conn.execute(text(f"""
                    DELETE FROM {hist}
                     WHERE APPROVED_AT < DATEADD(day, -:n, GETDATE())
                """), {"n": retention})
                d = int(res.rowcount or 0)
                by_table[tgt["label"]] = d
                total += d
            conn.commit()
        logger.info(
            f"[parked_history] history purge: retention={retention}d "
            f"deleted={total} by_table={by_table}"
        )
    except Exception as e:
        logger.warning(f"[parked_history] history purge failed: {e}")
    return {"deleted_total": total, "by_table": by_table, "retention_days": retention}


# ---------------------------------------------------------------------------
# Tables-affected sweep — single chokepoint, called once at end of run
# ---------------------------------------------------------------------------
def capture_pre_existence() -> Dict[str, bool]:
    """Snapshot whether each tracked table exists at the start of a run.
    Used by `tables_affected_summary` to classify the action as CREATED
    vs. RECREATED / TRUNCATED."""
    engine = get_data_engine()
    out: Dict[str, bool] = {}
    with engine.connect() as conn:
        for tbl in _AFFECTED_TABLES:
            try:
                v = conn.execute(text(
                    f"SELECT CASE WHEN OBJECT_ID('dbo.{tbl}','U') IS NULL "
                    f"THEN 0 ELSE 1 END"
                )).scalar()
                out[tbl] = bool(v)
            except Exception:
                out[tbl] = False
    return out


def tables_affected_summary(pre_existence: Dict[str, bool]
                            ) -> List[Dict[str, Any]]:
    """For each tracked table, count current rows and classify the action
    relative to its pre-run state.

    Action labels:
      CREATED   — table did not exist pre-run, exists now.
      RECREATED — existed before AND owned by a DROP/CREATE pattern.
      TRUNCATED — existed before AND owned by a TRUNCATE+INSERT pattern.
      MISSING   — doesn't exist post-run.
    """
    drop_create = {
        "ARS_LISTING", "ARS_LISTING_WORKING",
        "ARS_LISTED_OPT", "ARS_ALLOC_WORKING",
    }
    truncate_pattern = {
        "ARS_MSA_TOTAL", "ARS_MSA_GEN_ART", "ARS_MSA_VAR_ART",
    }

    engine = get_data_engine()
    out: List[Dict[str, Any]] = []
    with engine.connect() as conn:
        for tbl in _AFFECTED_TABLES:
            existed_before = bool(pre_existence.get(tbl, False))
            try:
                exists_now = bool(conn.execute(text(
                    f"SELECT CASE WHEN OBJECT_ID('dbo.{tbl}','U') IS NULL "
                    f"THEN 0 ELSE 1 END"
                )).scalar())
            except Exception:
                exists_now = False

            if not exists_now:
                out.append({"table": tbl, "action": "MISSING", "rows": 0})
                continue

            try:
                rows = int(conn.execute(text(
                    f"SELECT COUNT(*) FROM [{tbl}]"
                )).scalar() or 0)
            except Exception:
                rows = 0

            if not existed_before:
                action = "CREATED"
            elif tbl in drop_create:
                action = "RECREATED"
            elif tbl in truncate_pattern:
                action = "TRUNCATED"
            else:
                action = "UPSERTED"
            out.append({"table": tbl, "action": action, "rows": rows})
    return out


# ---------------------------------------------------------------------------
# Hold-tracking snapshot — snapshot ARS_NL_TBL_HOLD_TRACKING before Part 8.6
# writes it so that reject_parked() can restore the pre-run state.
# ---------------------------------------------------------------------------
_HOLD_SNAPSHOT_TABLE    = "ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT"
_HOLD_SNAPSHOT_SESSIONS = "ARS_NL_TBL_HOLD_SNAPSHOT_SESSIONS"


def _ensure_hold_snapshot_tables(conn) -> None:
    """Create the two hold-snapshot helper tables if they don't exist."""
    conn.execute(text(f"""
        IF OBJECT_ID('{_HOLD_SNAPSHOT_TABLE}','U') IS NULL
        CREATE TABLE [{_HOLD_SNAPSHOT_TABLE}] (
            [SESSION_ID]       NVARCHAR(50)  NOT NULL,
            [WERKS]            NVARCHAR(50)  NOT NULL,
            [RDC]              NVARCHAR(20)  NULL,
            [MAJ_CAT]          NVARCHAR(200) NULL,
            [GEN_ART_NUMBER]   BIGINT        NULL,
            [CLR]              NVARCHAR(200) NULL,
            [VAR_ART]          BIGINT        NOT NULL,
            [SZ]               NVARCHAR(50)  NOT NULL,
            [OPT_STATUS]       NVARCHAR(10)  NULL,
            [LISTED_DATE]      DATETIME      NULL,
            [HOLD_QTY_INITIAL] FLOAT         NULL,
            [HOLD_REM]         FLOAT         NULL,
            [LAST_UPDATED]     DATETIME      NULL,
            [IS_CLOSED]        BIT           NULL,
            [CLOSED_DATE]      DATETIME      NULL,
            CONSTRAINT [PK_{_HOLD_SNAPSHOT_TABLE}]
                PRIMARY KEY CLUSTERED ([SESSION_ID], [WERKS], [VAR_ART], [SZ])
        )
    """))
    # Idempotent ALTER for older deployments that already have the snapshot
    # table without the RDC column.
    conn.execute(text(f"""
        IF NOT EXISTS (
            SELECT 1 FROM sys.columns
            WHERE object_id = OBJECT_ID('{_HOLD_SNAPSHOT_TABLE}')
              AND name = 'RDC'
        )
        ALTER TABLE [{_HOLD_SNAPSHOT_TABLE}] ADD [RDC] NVARCHAR(20) NULL
    """))
    conn.execute(text(f"""
        IF OBJECT_ID('{_HOLD_SNAPSHOT_SESSIONS}','U') IS NULL
        CREATE TABLE [{_HOLD_SNAPSHOT_SESSIONS}] (
            [SESSION_ID]     NVARCHAR(50) NOT NULL,
            [SNAPSHOTTED_AT] DATETIME     NOT NULL DEFAULT GETDATE(),
            [ROW_COUNT]      INT          NOT NULL DEFAULT 0,
            CONSTRAINT [PK_{_HOLD_SNAPSHOT_SESSIONS}] PRIMARY KEY ([SESSION_ID])
        )
    """))
    conn.commit()


def snapshot_hold_tracking(session_id: str) -> None:
    """Snapshot ARS_NL_TBL_HOLD_TRACKING BEFORE Part 8.6 modifies it.

    A lightweight session-marker row is always inserted into
    ARS_NL_TBL_HOLD_SNAPSHOT_SESSIONS even when the tracking table is
    empty — this lets reject_parked() distinguish "snapshot was taken on
    an empty table" from "snapshot was never taken" and prevents it from
    accidentally deleting everything on old sessions.
    """
    engine = get_data_engine()
    try:
        with engine.connect() as conn:
            _ensure_hold_snapshot_tables(conn)
            # Idempotency — already snapshotted for this session?
            already = conn.execute(text(
                f"SELECT COUNT(*) FROM [{_HOLD_SNAPSHOT_SESSIONS}] WHERE SESSION_ID = :sid"
            ), {"sid": session_id}).scalar() or 0
            if already > 0:
                return
            # Snapshot all current rows.
            res = conn.execute(text(f"""
                INSERT INTO [{_HOLD_SNAPSHOT_TABLE}]
                    (SESSION_ID, WERKS, RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
                     OPT_STATUS, LISTED_DATE, HOLD_QTY_INITIAL, HOLD_REM,
                     LAST_UPDATED, IS_CLOSED, CLOSED_DATE)
                SELECT :sid, WERKS, RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
                       OPT_STATUS, LISTED_DATE, HOLD_QTY_INITIAL, HOLD_REM,
                       LAST_UPDATED, IS_CLOSED, CLOSED_DATE
                FROM [ARS_NL_TBL_HOLD_TRACKING]
            """), {"sid": session_id})
            row_count = int(res.rowcount or 0)
            # Session marker — always written so reject knows a snapshot exists.
            conn.execute(text(f"""
                INSERT INTO [{_HOLD_SNAPSHOT_SESSIONS}] (SESSION_ID, SNAPSHOTTED_AT, ROW_COUNT)
                VALUES (:sid, GETDATE(), :rc)
            """), {"sid": session_id, "rc": row_count})
            conn.commit()
            logger.info(
                f"[parked_history] hold tracking snapshot: "
                f"session={session_id} rows={row_count}"
            )
    except Exception as e:
        logger.warning(f"[parked_history] hold tracking snapshot failed for {session_id}: {e}")


def _apply_hold_tracking_from_history(conn, session_id: str) -> Dict[str, Any]:
    """Apply Step A + Step B hold-tracking changes for an approved listing.

    Mirrors the SQL that used to live in listing.py Part 8.6 (during
    generate), but moved here so the writes happen at APPROVE time —
    symmetric with PEND_ALC, which is also written on approve. Reads from
    ARS_ALLOC_HISTORY (post-promotion) filtered by SESSION_ID.

    Step A: RL/TBC alloc rows consumed existing hold → decrement HOLD_REM.
    Step B: TBL alloc rows created new hold → MERGE (insert / accumulate /
            re-open) and stamp RDC from the store master.

    Returns: {step_a_rows, step_b_rows, error}.
    """
    result: Dict[str, Any] = {"step_a_rows": 0, "step_b_rows": 0, "error": None}
    try:
        # STEP A — RL/TBC consumed warehouse hold
        r_a = conn.execute(text("""
            ;WITH RunHold AS (
                SELECT A.[WERKS],
                       TRY_CAST(A.[VAR_ART] AS BIGINT) AS VAR_ART,
                       A.[SZ],
                       SUM(ISNULL(TRY_CAST(A.[FROM_HOLD_QTY] AS FLOAT), 0)) AS from_hold_qty
                FROM [ARS_ALLOC_HISTORY] A
                WHERE A.[SESSION_ID] = :sid
                  AND A.[OPT_TYPE] IN ('RL', 'TBC')
                  AND ISNULL(TRY_CAST(A.[FROM_HOLD_QTY] AS FLOAT), 0) > 0
                GROUP BY A.[WERKS], TRY_CAST(A.[VAR_ART] AS BIGINT), A.[SZ]
            )
            UPDATE T SET
                T.[HOLD_REM] = CASE
                    WHEN T.[HOLD_REM] - R.from_hold_qty <= 0 THEN 0
                    ELSE T.[HOLD_REM] - R.from_hold_qty
                END,
                T.[IS_CLOSED] = CASE
                    WHEN T.[HOLD_REM] - R.from_hold_qty <= 0 THEN 1
                    ELSE 0
                END,
                T.[CLOSED_DATE] = CASE
                    WHEN T.[HOLD_REM] - R.from_hold_qty <= 0 THEN GETDATE()
                    ELSE NULL
                END,
                T.[LAST_UPDATED] = GETDATE()
            FROM [ARS_NL_TBL_HOLD_TRACKING] T
            INNER JOIN RunHold R
                ON  T.[WERKS]   = R.[WERKS]
                AND T.[VAR_ART] = R.[VAR_ART]
                AND T.[SZ]      = R.[SZ]
            WHERE T.[IS_CLOSED] = 0
        """), {"sid": session_id})
        result["step_a_rows"] = int(r_a.rowcount or 0)

        # STEP B — TBL created new warehouse hold (with RDC populated from
        # store master so MSA hold sync can join directly later).
        r_b = conn.execute(text("""
            MERGE [ARS_NL_TBL_HOLD_TRACKING] AS T
            USING (
                SELECT A.[WERKS], MAX(SM.[RDC]) AS RDC,
                       A.[MAJ_CAT], A.[GEN_ART_NUMBER], A.[CLR],
                       TRY_CAST(A.[VAR_ART] AS BIGINT) AS VAR_ART,
                       A.[SZ],
                       SUM(ISNULL(TRY_CAST(A.[HOLD_QTY] AS FLOAT), 0)) AS hold_qty
                FROM [ARS_ALLOC_HISTORY] A
                LEFT JOIN [Master_ALC_INPUT_ST_MASTER] SM
                    ON SM.[ST_CD] = A.[WERKS]
                WHERE A.[SESSION_ID] = :sid
                  AND A.[OPT_TYPE] = 'TBL'
                  AND ISNULL(TRY_CAST(A.[HOLD_QTY] AS FLOAT), 0) > 0
                GROUP BY A.[WERKS], A.[MAJ_CAT], A.[GEN_ART_NUMBER], A.[CLR],
                         TRY_CAST(A.[VAR_ART] AS BIGINT), A.[SZ]
            ) AS R
                ON T.[WERKS]   = R.[WERKS]
               AND T.[VAR_ART] = R.[VAR_ART]
               AND T.[SZ]      = R.[SZ]
            WHEN MATCHED THEN
                UPDATE SET
                    T.[RDC] = ISNULL(T.[RDC], R.[RDC]),
                    T.[HOLD_QTY_INITIAL] = CASE
                        WHEN T.[IS_CLOSED] = 1 THEN R.hold_qty
                        ELSE T.[HOLD_QTY_INITIAL] + R.hold_qty
                    END,
                    T.[HOLD_REM] = CASE
                        WHEN T.[IS_CLOSED] = 1 THEN R.hold_qty
                        ELSE T.[HOLD_REM] + R.hold_qty
                    END,
                    T.[IS_CLOSED]        = 0,
                    T.[CLOSED_DATE]      = NULL,
                    T.[LAST_UPDATED]     = GETDATE()
            WHEN NOT MATCHED THEN
                INSERT (
                    [WERKS], [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR],
                    [VAR_ART], [SZ], [OPT_STATUS],
                    [LISTED_DATE], [HOLD_QTY_INITIAL], [HOLD_REM],
                    [LAST_UPDATED], [IS_CLOSED]
                )
                VALUES (
                    R.[WERKS], R.[RDC], R.[MAJ_CAT], R.[GEN_ART_NUMBER], R.[CLR],
                    R.[VAR_ART], R.[SZ], 'TBL',
                    GETDATE(), R.hold_qty, R.hold_qty,
                    GETDATE(), 0
                );
        """), {"sid": session_id})
        result["step_b_rows"] = int(r_b.rowcount or 0)

        conn.commit()
        logger.info(
            f"[hold] hold-tracking applied from history session={session_id}: "
            f"step_a_rows={result['step_a_rows']} step_b_rows={result['step_b_rows']}"
        )
    except Exception as e:
        result["error"] = str(e)
        logger.warning(f"[hold] _apply_hold_tracking_from_history failed: {e}")
        try: conn.rollback()
        except Exception: pass
    return result


def _revert_hold_tracking(conn, session_id: str) -> Dict[str, Any]:
    """Inside an existing connection: restore ARS_NL_TBL_HOLD_TRACKING to
    its pre-run state using the snapshot for `session_id`.

    Returns {reverted: bool, deleted_new: int, restored: int, error: str|None}.
    """
    result: Dict[str, Any] = {"reverted": False, "deleted_new": 0, "restored": 0, "error": None}
    try:
        # Only revert if a snapshot was actually taken for this session.
        snapped = conn.execute(text(
            f"SELECT COUNT(*) FROM [{_HOLD_SNAPSHOT_SESSIONS}] WHERE SESSION_ID = :sid"
        ), {"sid": session_id}).scalar() or 0
        if snapped == 0:
            # No snapshot — this session predates the feature; don't touch the table.
            return result

        # 1. Delete rows inserted by this run (present in live table but NOT in snapshot).
        r_del = conn.execute(text(f"""
            DELETE T FROM [ARS_NL_TBL_HOLD_TRACKING] T
            WHERE NOT EXISTS (
                SELECT 1 FROM [{_HOLD_SNAPSHOT_TABLE}] S
                WHERE S.SESSION_ID = :sid
                  AND S.WERKS   = T.WERKS
                  AND S.VAR_ART = T.VAR_ART
                  AND S.SZ      = T.SZ
            )
        """), {"sid": session_id})
        result["deleted_new"] = int(r_del.rowcount or 0)

        # 2. Restore rows that existed before the run to their snapshot values.
        r_upd = conn.execute(text(f"""
            UPDATE T SET
                T.[RDC]              = S.[RDC],
                T.[MAJ_CAT]          = S.[MAJ_CAT],
                T.[GEN_ART_NUMBER]   = S.[GEN_ART_NUMBER],
                T.[CLR]              = S.[CLR],
                T.[OPT_STATUS]       = S.[OPT_STATUS],
                T.[LISTED_DATE]      = S.[LISTED_DATE],
                T.[HOLD_QTY_INITIAL] = S.[HOLD_QTY_INITIAL],
                T.[HOLD_REM]         = S.[HOLD_REM],
                T.[LAST_UPDATED]     = S.[LAST_UPDATED],
                T.[IS_CLOSED]        = S.[IS_CLOSED],
                T.[CLOSED_DATE]      = S.[CLOSED_DATE]
            FROM [ARS_NL_TBL_HOLD_TRACKING] T
            INNER JOIN [{_HOLD_SNAPSHOT_TABLE}] S
                ON S.WERKS   = T.WERKS
               AND S.VAR_ART = T.VAR_ART
               AND S.SZ      = T.SZ
            WHERE S.SESSION_ID = :sid
        """), {"sid": session_id})
        result["restored"] = int(r_upd.rowcount or 0)

        # 3. Clean up this session's snapshot rows.
        conn.execute(text(
            f"DELETE FROM [{_HOLD_SNAPSHOT_TABLE}] WHERE SESSION_ID = :sid"
        ), {"sid": session_id})
        conn.execute(text(
            f"DELETE FROM [{_HOLD_SNAPSHOT_SESSIONS}] WHERE SESSION_ID = :sid"
        ), {"sid": session_id})

        result["reverted"] = True
        logger.info(
            f"[parked_history] hold tracking reverted: session={session_id} "
            f"deleted_new={result['deleted_new']} restored={result['restored']}"
        )
    except Exception as e:
        result["error"] = str(e)
        logger.warning(f"[parked_history] hold tracking revert failed for {session_id}: {e}")
    return result


# ---------------------------------------------------------------------------
# Helper for /listing/generate concurrency guard
# ---------------------------------------------------------------------------
def has_running_session() -> bool:
    """True if any ARS_LISTING_SESSIONS row is still STATUS='RUNNING'."""
    engine = get_data_engine()
    try:
        with engine.connect() as conn:
            v = conn.execute(text(
                f"SELECT COUNT(*) FROM {SESSIONS_TABLE} "
                f"WHERE STATUS = 'RUNNING'"
            )).scalar() or 0
            return int(v) > 0
    except Exception:
        return False


def has_pending_parked() -> bool:
    """True if any session is parked and awaiting approve/reject.

    Blocks new /listing/generate runs until the user resolves the pending
    parked session — only one parked session at a time is allowed.

    Queries ARS_ALLOC_PARKED (the actual parked-rows table) so the check
    stays consistent with list_parked_runs() and is cleared correctly after
    reject_parked() deletes those rows.
    """
    alloc_p = _get_target("alloc")["parked"]  # ARS_ALLOC_PARKED
    engine = get_data_engine()
    try:
        with engine.connect() as conn:
            v = conn.execute(text(
                f"SELECT COUNT(*) FROM {alloc_p} WHERE PARK_STATUS = 'PARKED'"
            )).scalar() or 0
            return int(v) > 0
    except Exception:
        return False
