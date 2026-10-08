"""
SQL-table delivery for Report Generation
========================================
Writes a report step's result set straight into a SQL Server table instead of
(or as well as) exporting it to a file.

Why it exists: for event-triggered reports — "when a process completes" — the
CSV round trip is wasted work if the data is only going to be read back into
SQL. This streams the result set into a real table in one pass.

Design notes
------------
* **The table is engine-managed.** It is CREATEd on first run from the live
  cursor description and ALTER…ADDed when new columns appear. That matters
  because the important procs here build their SELECT list at run time
  (ALLRDC_usp_ars_grid_report grows a column set per RDC/SLOC), so a
  hand-maintained table would break exactly when the schema shifts.
  `sp_describe_first_result_set` cannot be used for the same reason — it
  returns "metadata could not be determined" for every dynamic-SQL proc.

* **One table per step**, named `<prefix><step label>`, so a multi-step report
  does not collide with itself.

* **Append with SESSION_ID.** Every row is stamped with the run's session code
  and RUN_AT, so history accumulates and can be filtered per run.

* **Retention.** After appending, rows older than `retention_days` are deleted
  so the table does not grow without bound. Deleted in batches to keep the
  transaction log small.

Columns are never dropped and existing ones are never retyped — only added —
so an older row keeps whatever it had when it was written.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import logging
import re
from typing import Any, Dict, List, Optional

from app.database.session import get_data_engine

logger = logging.getLogger(__name__)

_STREAM_CHUNK = 5000
_INSERT_BATCH = 1000
_DELETE_BATCH = 50000

# Columns the engine stamps on every row.
SESSION_COL = "SESSION_ID"
RUNAT_COL = "RUN_AT"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_]+")


def _qname(name: str) -> str:
    """Bracket-quote an identifier (QUOTENAME equivalent)."""
    return "[" + str(name).replace("]", "]]") + "]"


def safe_table_name(raw: str, prefix: str = "") -> str:
    """Turn a step label into a legal, predictable table name."""
    cleaned = _SAFE_NAME.sub("_", str(raw or "").strip()).strip("_")
    if not cleaned:
        cleaned = "REPORT"
    if cleaned[0].isdigit():
        cleaned = "T_" + cleaned
    return (prefix or "") + cleaned[:100]


def _sql_type(desc) -> str:
    """Map one pyodbc cursor.description entry to a T-SQL column type.

    desc = (name, type_code, display_size, internal_size, precision, scale, null_ok)
    """
    type_code, internal_size, precision, scale = desc[1], desc[3], desc[4], desc[5]
    if type_code is bool:
        return "BIT"
    if type_code is int:
        return "BIGINT"
    if type_code is float:
        return "FLOAT"
    if type_code is decimal.Decimal:
        p = precision if (precision and 1 <= precision <= 38) else 38
        s = scale if (scale is not None and 0 <= scale <= p) else 6
        return f"DECIMAL({p},{s})"
    if type_code is _dt.datetime:
        return "DATETIME2"
    if type_code is _dt.date:
        return "DATE"
    if type_code is _dt.time:
        return "TIME"
    if type_code in (bytes, bytearray, memoryview):
        return "VARBINARY(MAX)"
    # str and anything unrecognised -> unicode text, sized from the driver when sane
    if internal_size and 0 < internal_size <= 4000:
        return f"NVARCHAR({int(internal_size)})"
    return "NVARCHAR(MAX)"


def _existing_columns(conn, schema: str, table: str) -> Dict[str, str]:
    rows = conn.execute(
        "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?", (schema, table)).fetchall()
    return {r[0]: r[1] for r in rows}


def resolve_data_columns(description, stamp: bool) -> List[str]:
    """Map the result set's columns to unique table column names.

    Two collisions to handle:
      * a result set may legally repeat a column name;
      * a report may output its OWN column called SESSION_ID — usp_ars_grid_report
        does, carrying the LISTING session — which would clash with the stamp.
    In both cases the DATA column is suffixed (`SESSION_ID_1`); the stamp keeps
    the plain name, so SESSION_ID always means "the report run that wrote this
    row" and SESSION_ID_1 is the report's own value.
    """
    seen: Dict[str, int] = {}
    if stamp:
        seen[SESSION_COL] = 0
        seen[RUNAT_COL] = 0
    uniq: List[str] = []
    for c in (d[0] for d in description):
        if c in seen:
            seen[c] += 1
            uniq.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 0
            uniq.append(c)
    return uniq


def _ensure_table(conn, schema: str, table: str, description,
                  stamp: bool = True) -> List[str]:
    """Create the table if absent, add any columns that are new. Returns the
    ordered data column names (excluding the stamped ones)."""
    uniq = resolve_data_columns(description, stamp)

    existing = _existing_columns(conn, schema, table)
    if not existing:
        defs = ", ".join(
            f"{_qname(n)} {_sql_type(d)} NULL" for n, d in zip(uniq, description))
        conn.execute(
            f"CREATE TABLE {_qname(schema)}.{_qname(table)} ("
            f"{_qname(SESSION_COL)} NVARCHAR(100) NULL, "
            f"{_qname(RUNAT_COL)} DATETIME2 NULL, {defs})")
        conn.execute(
            f"CREATE INDEX {_qname('IX_' + table + '_SESSION')} "
            f"ON {_qname(schema)}.{_qname(table)} ({_qname(SESSION_COL)}, {_qname(RUNAT_COL)})")
        logger.info(f"[sql_table] created {schema}.{table} with {len(uniq)} data column(s)")
    else:
        for n, d in zip(uniq, description):
            if n not in existing:
                conn.execute(
                    f"ALTER TABLE {_qname(schema)}.{_qname(table)} "
                    f"ADD {_qname(n)} {_sql_type(d)} NULL")
                logger.info(f"[sql_table] {schema}.{table}: added column {n}")
        for stamp, ddl in ((SESSION_COL, "NVARCHAR(100)"), (RUNAT_COL, "DATETIME2")):
            if stamp not in existing:
                conn.execute(
                    f"ALTER TABLE {_qname(schema)}.{_qname(table)} ADD {_qname(stamp)} {ddl} NULL")
    return uniq


def _purge(conn, schema: str, table: str, retention_days: int) -> int:
    """Delete rows older than retention_days, in batches. Returns rows removed."""
    if not retention_days or retention_days <= 0:
        return 0
    total = 0
    while True:
        cur = conn.execute(
            f"DELETE TOP ({_DELETE_BATCH}) FROM {_qname(schema)}.{_qname(table)} "
            f"WHERE {_qname(RUNAT_COL)} IS NOT NULL "
            f"AND {_qname(RUNAT_COL)} < DATEADD(DAY, -?, SYSDATETIME())",
            (int(retention_days),))
        n = cur.rowcount or 0
        total += n
        if n < _DELETE_BATCH:
            break
    if total:
        logger.info(f"[sql_table] {schema}.{table}: purged {total} row(s) older "
                    f"than {retention_days} day(s)")
    return total


def write_step_to_table(sql: str, values, cfg: Dict[str, Any], session_code: str,
                        step_label: str, cancel_token=None) -> Dict[str, Any]:
    """Execute `sql` and append its FIRST result set to a managed SQL table.

    cfg keys: schema (default dbo), table_prefix, table (overrides the derived
    name), retention_days, add_session_col.
    Returns {"table": "schema.table", "rows": n, "purged": n}.
    """
    schema = (cfg.get("schema") or "dbo").strip() or "dbo"
    table = (cfg.get("table") or "").strip() or safe_table_name(
        step_label, cfg.get("table_prefix") or "ARS_RPT_")
    retention = int(cfg.get("retention_days") or 0)
    stamp = cfg.get("add_session_col", True)

    engine = get_data_engine()
    # TWO connections on purpose: pyodbc cannot run DDL/INSERT on a connection
    # that still has an open result set ("Connection is busy with results for
    # another command") unless MARS is on, and MARS is not enabled here. The
    # read connection streams the proc; the write connection owns the table.
    raw = engine.raw_connection()
    wraw = engine.raw_connection()
    rows_written = 0
    purged = 0
    try:
        cursor = raw.cursor()
        if cancel_token is not None:
            cancel_token.bind_cursor(cursor)
        try:
            cursor.execute(sql, values) if values else cursor.execute(sql)
            # First result set only — reports deliver one grid per step.
            while cursor.description is None:
                if not cursor.nextset():
                    return {"table": f"{schema}.{table}", "rows": 0, "purged": 0}

            ddl = wraw.cursor()
            try:
                data_cols = _ensure_table(ddl, schema, table, cursor.description, stamp)
                wraw.commit()
            finally:
                ddl.close()

            cols = ([SESSION_COL, RUNAT_COL] if stamp else []) + data_cols
            placeholders = ", ".join("?" * len(cols))
            ins = (f"INSERT INTO {_qname(schema)}.{_qname(table)} "
                   f"({', '.join(_qname(c) for c in cols)}) VALUES ({placeholders})")
            wcur = wraw.cursor()
            try:
                try:
                    wcur.fast_executemany = True
                except Exception:
                    pass
                now = _dt.datetime.now()
                while True:
                    batch = cursor.fetchmany(_STREAM_CHUNK)
                    if not batch:
                        break
                    payload = ([[session_code, now, *r] for r in batch] if stamp
                               else [list(r) for r in batch])
                    for i in range(0, len(payload), _INSERT_BATCH):
                        wcur.executemany(ins, payload[i:i + _INSERT_BATCH])
                    rows_written += len(payload)
                wraw.commit()
            finally:
                wcur.close()
        finally:
            if cancel_token is not None:
                cancel_token.unbind_cursor()
            cursor.close()

        if retention:
            pcur = wraw.cursor()
            try:
                purged = _purge(pcur, schema, table, retention)
                wraw.commit()
            finally:
                pcur.close()
    finally:
        raw.close()
        wraw.close()

    logger.info(f"[sql_table] {step_label} -> {schema}.{table}: "
                f"{rows_written} row(s) appended, {purged} purged")
    return {"table": f"{schema}.{table}", "rows": rows_written, "purged": purged}
