"""
Derived Masters Service
=======================
Auto-derives `Master_CONT_MERGE_<col>` tables from their parent `Master_CONT_<col>`
using the mapping rules stored in `ARS_MERGE_RULES`.

Rule (Possibility 1 — flat, global, no company scope):
- ARS_MERGE_RULES rows: (source_col, source_value, target_value, agg, active)
- One row per source value. UNIQUE on (source_col, source_value).
- All active rows for a source_col MUST agree on `agg` (enforced in Python).

Triggers:
1. Upload hook  — when Master_CONT_<col> is uploaded, refresh Master_CONT_MERGE_<col>.
2. Grid hook    — when a grid joins Master_CONT_MERGE_<col> and it is missing/stale,
                   rebuild before the join.

Derived table shape (matches the post-pivot lookup join_on contract):
    Master_CONT_MERGE_<col>(
        ST_CD          NVARCHAR(50)  NOT NULL,
        MAJ_CAT        NVARCHAR(100) NOT NULL,
        MERGE_<col>    NVARCHAR(200) NOT NULL,
        CONT           FLOAT         NULL,
        derived_at     DATETIME      NOT NULL
    )
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set

from sqlalchemy import text
from loguru import logger


RULES_TABLE = "ARS_MERGE_RULES"
PARENT_PREFIX = "Master_CONT_"
DERIVED_PREFIX = "Master_CONT_MERGE_"
MERGE_COL_PREFIX = "MERGE_"
VALID_AGG = {"SUM", "AVG", "MAX", "MIN"}


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------
def is_merge_col(col: str) -> bool:
    """True if the column name follows the MERGE_<parent> convention."""
    return bool(col) and col.upper().startswith(MERGE_COL_PREFIX)


def parent_col(col: str) -> Optional[str]:
    """MERGE_RNG_SEG -> RNG_SEG. Returns None if not a MERGE_ column."""
    if not is_merge_col(col):
        return None
    return col[len(MERGE_COL_PREFIX):]


def derived_table_for(source_col: str) -> str:
    return f"{DERIVED_PREFIX}{source_col}"


def parent_table_for(source_col: str) -> str:
    return f"{PARENT_PREFIX}{source_col}"


def is_derived_master_table(table_name: str) -> bool:
    return bool(table_name) and table_name.upper().startswith(DERIVED_PREFIX.upper())


def source_col_from_derived_table(table_name: str) -> Optional[str]:
    """Master_CONT_MERGE_RNG_SEG -> RNG_SEG."""
    if not is_derived_master_table(table_name):
        return None
    return table_name[len(DERIVED_PREFIX):]


def source_col_from_parent_table(table_name: str) -> Optional[str]:
    """Master_CONT_RNG_SEG -> RNG_SEG (returns None for MERGE_* tables)."""
    if not table_name or not table_name.upper().startswith(PARENT_PREFIX.upper()):
        return None
    if is_derived_master_table(table_name):
        return None
    return table_name[len(PARENT_PREFIX):]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def ensure_rules_table(conn) -> None:
    """Idempotent CREATE TABLE + indexes for ARS_MERGE_RULES."""
    exists = conn.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :t"
    ), {"t": RULES_TABLE}).scalar() > 0
    if exists:
        return
    conn.execute(text(f"""
        CREATE TABLE {RULES_TABLE} (
            rule_id      INT IDENTITY(1,1) PRIMARY KEY,
            source_col   NVARCHAR(100) NOT NULL,
            source_value NVARCHAR(200) NOT NULL,
            target_value NVARCHAR(200) NOT NULL,
            agg          NVARCHAR(20)  NOT NULL DEFAULT 'SUM',
            active       BIT           NOT NULL DEFAULT 1,
            created_at   DATETIME      NOT NULL DEFAULT GETDATE(),
            modified_at  DATETIME      NOT NULL DEFAULT GETDATE(),
            modified_by  NVARCHAR(100) NULL,
            CONSTRAINT UQ_{RULES_TABLE} UNIQUE (source_col, source_value)
        )
    """))
    conn.execute(text(
        f"CREATE INDEX IX_{RULES_TABLE}_source_col ON {RULES_TABLE}(source_col) "
        f"WHERE active = 1"
    ))
    conn.commit()
    logger.info(f"Created {RULES_TABLE}")


# ---------------------------------------------------------------------------
# Rules access
# ---------------------------------------------------------------------------
def list_active_source_cols(conn) -> List[str]:
    """Distinct source_col values that have ≥ 1 active rule."""
    ensure_rules_table(conn)
    rows = conn.execute(text(
        f"SELECT DISTINCT source_col FROM {RULES_TABLE} WHERE active = 1"
    )).fetchall()
    return [r[0] for r in rows]


def get_mapping(conn, source_col: str) -> Dict[str, str]:
    """Returns {source_value: target_value} for active rules of source_col."""
    ensure_rules_table(conn)
    rows = conn.execute(text(
        f"SELECT source_value, target_value FROM {RULES_TABLE} "
        f"WHERE source_col = :c AND active = 1"
    ), {"c": source_col}).fetchall()
    return {r[0]: r[1] for r in rows}


def get_agg(conn, source_col: str) -> str:
    """Returns the agg function for source_col. SUM if no rules or mixed/invalid."""
    ensure_rules_table(conn)
    rows = conn.execute(text(
        f"SELECT DISTINCT agg FROM {RULES_TABLE} "
        f"WHERE source_col = :c AND active = 1"
    ), {"c": source_col}).fetchall()
    aggs = {(r[0] or "SUM").upper() for r in rows}
    if not aggs:
        return "SUM"
    if len(aggs) > 1:
        logger.warning(
            f"Mixed agg values for source_col={source_col}: {aggs}. Falling back to SUM."
        )
        return "SUM"
    agg = next(iter(aggs))
    return agg if agg in VALID_AGG else "SUM"


# ---------------------------------------------------------------------------
# SQL expression builder (used by grid pivot to materialise MERGE_<col>)
# ---------------------------------------------------------------------------
def build_case_expr(conn, merge_col: str, table_alias: str = "MP") -> Optional[str]:
    """
    For a hierarchy column like MERGE_RNG_SEG, returns:
        CASE [MP].[RNG_SEG] WHEN 'E' THEN 'EV' WHEN 'V' THEN 'EV' ... ELSE [MP].[RNG_SEG] END
    Returns None if the column is not MERGE_* or has no active rules.
    """
    src = parent_col(merge_col)
    if not src:
        return None
    mapping = get_mapping(conn, src)
    if not mapping:
        return None

    def _q(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"

    parts = [f"WHEN {_q(sv)} THEN {_q(tv)}" for sv, tv in mapping.items()]
    src_ref = f"[{table_alias}].[{src}]" if table_alias else f"[{src}]"
    return f"CASE {src_ref} " + " ".join(parts) + f" ELSE {src_ref} END"


# ---------------------------------------------------------------------------
# Derivation: TRUNCATE + INSERT Master_CONT_MERGE_<col>
# ---------------------------------------------------------------------------
def _table_exists(conn, table: str) -> bool:
    return conn.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :t"
    ), {"t": table}).scalar() > 0


def _parent_columns(conn, table: str) -> Dict[str, str]:
    """Upper col name -> actual col name."""
    rows = conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"
    ), {"t": table}).fetchall()
    return {r[0].upper(): r[0] for r in rows}


def _ensure_derived_table(conn, derived: str, merge_col_name: str) -> None:
    """Create the derived table if missing; never alter an existing one."""
    if _table_exists(conn, derived):
        return
    conn.execute(text(f"""
        CREATE TABLE [{derived}] (
            ST_CD        NVARCHAR(50)  NOT NULL,
            MAJ_CAT      NVARCHAR(100) NOT NULL,
            [{merge_col_name}] NVARCHAR(200) NOT NULL,
            CONT         FLOAT         NULL,
            derived_at   DATETIME      NOT NULL DEFAULT GETDATE(),
            CONSTRAINT PK_{derived} PRIMARY KEY (ST_CD, MAJ_CAT, [{merge_col_name}])
        )
    """))
    conn.commit()
    logger.info(f"Created derived master {derived}")


def refresh_derived_for_source_col(conn, source_col: str) -> Dict[str, object]:
    """
    Rebuild Master_CONT_MERGE_<source_col> from Master_CONT_<source_col>.
    Single transaction: TRUNCATE + INSERT, validate CONT total parity, commit.
    Returns {"status", "rows", "parent_rows", "warning"}.
    """
    ensure_rules_table(conn)
    parent = parent_table_for(source_col)
    derived = derived_table_for(source_col)
    merge_col = f"{MERGE_COL_PREFIX}{source_col}"

    if not _table_exists(conn, parent):
        return {"status": "skipped", "reason": f"parent table {parent} does not exist"}

    mapping = get_mapping(conn, source_col)
    if not mapping:
        return {"status": "skipped", "reason": f"no active rules for {source_col}"}

    parent_cols = _parent_columns(conn, parent)
    required = {"ST_CD", "MAJ_CAT", source_col.upper(), "CONT"}
    missing = required - set(parent_cols.keys())
    if missing:
        return {"status": "error", "reason": f"parent {parent} missing columns: {missing}"}

    st_cd = parent_cols["ST_CD"]
    maj_cat = parent_cols["MAJ_CAT"]
    src = parent_cols[source_col.upper()]
    cont = parent_cols["CONT"]

    agg = get_agg(conn, source_col)

    def _q(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"
    case_parts = " ".join(f"WHEN {_q(sv)} THEN {_q(tv)}" for sv, tv in mapping.items())
    merge_case = f"CASE [{src}] {case_parts} ELSE [{src}] END"

    _ensure_derived_table(conn, derived, merge_col)

    # Parent total CONT, for reconciliation
    parent_total = conn.execute(text(
        f"SELECT ISNULL(SUM(TRY_CAST([{cont}] AS FLOAT)), 0) FROM [{parent}]"
    )).scalar() or 0.0

    # Single txn: TRUNCATE + INSERT
    conn.execute(text(f"TRUNCATE TABLE [{derived}]"))
    conn.execute(text(f"""
        INSERT INTO [{derived}] (ST_CD, MAJ_CAT, [{merge_col}], CONT, derived_at)
        SELECT
            [{st_cd}]   AS ST_CD,
            [{maj_cat}] AS MAJ_CAT,
            {merge_case} AS [{merge_col}],
            {agg}(TRY_CAST([{cont}] AS FLOAT)) AS CONT,
            GETDATE()   AS derived_at
        FROM [{parent}]
        WHERE [{st_cd}] IS NOT NULL AND [{maj_cat}] IS NOT NULL
          AND [{src}] IS NOT NULL
        GROUP BY [{st_cd}], [{maj_cat}], {merge_case}
    """))

    derived_total = conn.execute(text(
        f"SELECT ISNULL(SUM(CONT), 0) FROM [{derived}]"
    )).scalar() or 0.0
    rows_out = conn.execute(text(f"SELECT COUNT(*) FROM [{derived}]")).scalar() or 0
    rows_in = conn.execute(text(f"SELECT COUNT(*) FROM [{parent}]")).scalar() or 0

    warning = None
    # SUM-of-CONT reconciliation only meaningful when agg=SUM
    if agg == "SUM" and abs(float(parent_total) - float(derived_total)) > 0.01:
        warning = (
            f"CONT total drift: parent={parent_total:.4f} derived={derived_total:.4f}. "
            f"Likely a source_value missing from ARS_MERGE_RULES (passed through ELSE)."
        )
        logger.warning(f"[derived_masters] {derived}: {warning}")

    conn.commit()
    logger.info(
        f"[derived_masters] Refreshed {derived}: {rows_out} rows from {rows_in} parent rows "
        f"(agg={agg}, parent_CONT={parent_total:.2f}, derived_CONT={derived_total:.2f})"
    )
    return {
        "status": "ok",
        "rows": int(rows_out),
        "parent_rows": int(rows_in),
        "parent_total": float(parent_total),
        "derived_total": float(derived_total),
        "agg": agg,
        "warning": warning,
    }


def refresh_for_parent_table(conn, parent_table: str) -> Optional[Dict[str, object]]:
    """Upload-hook entry: refresh the derived table that belongs to this parent."""
    src = source_col_from_parent_table(parent_table)
    if not src:
        return None
    if src not in list_active_source_cols(conn):
        return None
    return refresh_derived_for_source_col(conn, src)


def ensure_derived_master(conn, derived_table: str) -> Optional[Dict[str, object]]:
    """Grid-hook entry: rebuild derived_table if missing or stale vs parent."""
    src = source_col_from_derived_table(derived_table)
    if not src:
        return None
    parent = parent_table_for(src)
    if not _table_exists(conn, parent):
        return None
    if src not in list_active_source_cols(conn):
        return None

    needs_refresh = not _table_exists(conn, derived_table)
    if not needs_refresh:
        # stale if derived has no rows or any parent row is newer (best-effort:
        # we don't track parent mtime, so just refresh if empty).
        cnt = conn.execute(text(
            f"SELECT COUNT(*) FROM [{derived_table}]"
        )).scalar() or 0
        needs_refresh = cnt == 0

    if not needs_refresh:
        return {"status": "skipped", "reason": "fresh"}
    return refresh_derived_for_source_col(conn, src)
