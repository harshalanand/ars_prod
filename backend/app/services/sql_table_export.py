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

* **Append or replace.** In APPEND mode every row is stamped with the run's
  session code and RUN_AT, so history accumulates and can be filtered per run.
  In REPLACE mode the table is cleared once at the start of the run and holds
  only the latest result; retention does not apply.

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
_NVARCHAR_FLOOR = 255

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
    # str and anything unrecognised -> unicode text.
    # Floor at NVARCHAR(255): the driver's reported width comes from the literal
    # widths inside a dynamic-SQL proc, so it under-reports badly. A fan-out step
    # created LEVEL as 6 wide from the 'RDC' branch, then truncated on 'MAJ_CAT'.
    # Widening is handled in _ensure_table for tables that already exist.
    if internal_size and 0 < internal_size <= 4000:
        return f"NVARCHAR({max(int(internal_size), _NVARCHAR_FLOOR)})"
    return "NVARCHAR(MAX)"


def _existing_columns(conn, schema: str, table: str) -> Dict[str, tuple]:
    """{name: (data_type, char_max_len)}; char_max_len is -1 for MAX, None if n/a."""
    rows = conn.execute(
        "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
        (schema, table)).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def _needed_width(sql_type: str):
    """Width wanted by a NVARCHAR(n)/NVARCHAR(MAX) type string, else None."""
    m = re.match(r"(?i)NVARCHAR\((MAX|\d+)\)", sql_type or "")
    if not m:
        return None
    return -1 if m.group(1).upper() == "MAX" else int(m.group(1))


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
        # The stamp columns are created ONLY when stamping is on. Creating them
        # unconditionally collided with a result set that has its own SESSION_ID
        # (usp_ars_grid_report does), because resolve_data_columns only reserves
        # those names when stamp is True.
        head = (f"{_qname(SESSION_COL)} NVARCHAR(100) NULL, "
                f"{_qname(RUNAT_COL)} DATETIME2 NULL, ") if stamp else ""
        conn.execute(
            f"CREATE TABLE {_qname(schema)}.{_qname(table)} ({head}{defs})")
        if stamp:
            conn.execute(
                f"CREATE INDEX {_qname('IX_' + table + '_SESSION')} "
                f"ON {_qname(schema)}.{_qname(table)} ({_qname(SESSION_COL)}, {_qname(RUNAT_COL)})")
        logger.info(f"[sql_table] created {schema}.{table} with {len(uniq)} data column(s)")
    else:
        for n, d in zip(uniq, description):
            want = _sql_type(d)
            if n not in existing:
                conn.execute(
                    f"ALTER TABLE {_qname(schema)}.{_qname(table)} "
                    f"ADD {_qname(n)} {want} NULL")
                logger.info(f"[sql_table] {schema}.{table}: added column {n}")
                continue
            # WIDEN a text column that is now too narrow. Different runs of the
            # same dynamic proc report different widths (a fan-out branch with a
            # longer literal), and without this the insert dies with
            # "String data, right truncation".
            cur_type, cur_len = existing[n]
            need = _needed_width(want)
            if need is not None and str(cur_type).lower() in ("nvarchar", "varchar", "nchar", "char"):
                if cur_len is not None and cur_len != -1 and (need == -1 or need > cur_len):
                    newt = "NVARCHAR(MAX)" if need == -1 else f"NVARCHAR({need})"
                    conn.execute(
                        f"ALTER TABLE {_qname(schema)}.{_qname(table)} "
                        f"ALTER COLUMN {_qname(n)} {newt} NULL")
                    logger.info(f"[sql_table] {schema}.{table}: widened {n} "
                                f"{cur_type}({cur_len}) -> {newt}")
        # Add the stamp columns to a pre-existing table only when stamping is on.
        # (The loop variable used to be called `stamp`, shadowing the parameter.)
        if stamp:
            for scol, ddl in ((SESSION_COL, "NVARCHAR(100)"), (RUNAT_COL, "DATETIME2")):
                if scol not in existing:
                    conn.execute(
                        f"ALTER TABLE {_qname(schema)}.{_qname(table)} ADD {_qname(scol)} {ddl} NULL")
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


def _clear(conn, schema: str, table: str) -> None:
    """Empty the table for REPLACE mode. TRUNCATE is minimally logged, but it
    fails if anything references the table, so fall back to DELETE."""
    try:
        conn.execute(f"TRUNCATE TABLE {_qname(schema)}.{_qname(table)}")
    except Exception:
        conn.execute(f"DELETE FROM {_qname(schema)}.{_qname(table)}")
    logger.info(f"[sql_table] {schema}.{table}: cleared for replace")


def _try_server_side_insert(engine, sql, values, schema: str, table: str,
                            session_code: str, stamp: bool, do_clear: bool,
                            retention: int, step_label: str):
    """INSERT INTO <table> (cols) EXEC <proc> — no row ever reaches Python.

    Returns the usual result dict, or None to mean "not usable, stream instead".
    None is returned for an absent table (nothing to insert into yet) and for
    ANY error — a column-count change raises here and the streaming fallback
    then reconciles the schema. Everything is one transaction, so a failed
    attempt leaves the table exactly as it was, including a replace-mode clear.
    """
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        try:
            existing = _existing_columns(cur, schema, table)
            if not existing:
                return None                      # first run — let streaming create it
            data_cols = [c for c in existing if c not in (SESSION_COL, RUNAT_COL)]
            if not data_cols:
                return None
            cols_sql = ", ".join(_qname(c) for c in data_cols)
            if do_clear:
                _clear(cur, schema, table)
            now = _dt.datetime.now()
            cur.execute(
                f"INSERT INTO {_qname(schema)}.{_qname(table)} ({cols_sql}) {sql}",
                values if values else [])
            rows = cur.rowcount if (cur.rowcount or 0) > 0 else 0
            if stamp:
                # Rows just inserted are the ones with no stamp yet; this also
                # gives an exact count when rowcount is unreliable.
                cur.execute(
                    f"UPDATE {_qname(schema)}.{_qname(table)} "
                    f"SET {_qname(SESSION_COL)} = ?, {_qname(RUNAT_COL)} = ? "
                    f"WHERE {_qname(SESSION_COL)} IS NULL", (session_code, now))
                if (cur.rowcount or 0) > 0:
                    rows = cur.rowcount
            raw.commit()
        finally:
            cur.close()

        purged = 0
        if retention:
            pcur = raw.cursor()
            try:
                purged = _purge(pcur, schema, table, retention)
                raw.commit()
            finally:
                pcur.close()
        logger.info(f"[sql_table] {step_label} -> {schema}.{table}: {rows} row(s) "
                    f"{'replaced' if do_clear else 'appended'} server-side, {purged} purged")
        return {"table": f"{schema}.{table}", "rows": rows, "purged": purged,
                "replaced": bool(do_clear), "server_side": True}
    except Exception as e:
        try:
            raw.rollback()
        except Exception:
            pass
        logger.info(f"[sql_table] {schema}.{table}: server-side insert not usable "
                    f"({str(e)[:110]}) — falling back to streaming")
        return None
    finally:
        raw.close()


def write_step_to_table(sql: str, values, cfg: Dict[str, Any], session_code: str,
                        step_label: str, cancel_token=None,
                        cleared: Optional[set] = None) -> Dict[str, Any]:
    """Execute `sql` and write its FIRST result set to a managed SQL table.

    cfg keys: schema (default dbo), table_prefix, table (overrides the derived
    name), mode ('append'|'replace'), retention_days, add_session_col.

    `cleared` is a per-RUN set the caller owns. In REPLACE mode a table is
    emptied the FIRST time this run writes to it and appended to afterwards;
    without that, a fan-out step (several executions) would leave only the last
    value. The key is the RESOLVED table name, not the step label — fan-out
    values share one table when cfg['table'] is set, but get a table each when
    only a prefix is given. Retention does not apply in replace mode.

    Returns {"table": "schema.table", "rows": n, "purged": n, "replaced": bool}.
    """
    schema = (cfg.get("schema") or "dbo").strip() or "dbo"
    table = (cfg.get("table") or "").strip() or safe_table_name(
        step_label, cfg.get("table_prefix") or "ARS_RPT_")
    replace = str(cfg.get("mode") or "append").lower() == "replace"
    retention = 0 if replace else int(cfg.get("retention_days") or 0)
    stamp = cfg.get("add_session_col", True)
    _key = f"{schema}.{table}"
    do_clear = replace and (cleared is None or _key not in cleared)
    if cleared is not None:
        cleared.add(_key)

    engine = get_data_engine()

    # ---- FAST PATH: keep the rows inside SQL Server -------------------------
    # INSERT INTO tgt (cols) EXEC proc — the result set never crosses the wire.
    # Measured 0.045 ms/row against 0.200 ms/row for the streaming path below
    # (446,717 rows: 20.0s vs 89.3s of write time).
    #
    # Only for EXEC statements: a raw SELECT may open with a CTE, and
    # "INSERT INTO t WITH cte AS (...)" is not valid T-SQL. And only once the
    # table exists, because the streaming path is what CREATEs and widens it.
    #
    # Safety: the column list is the table's own data columns, so SQL Server
    # rejects a count mismatch and we fall back to streaming, which adds or
    # widens columns and self-heals. The residual risk is a proc returning the
    # SAME number of columns in a DIFFERENT order — only reachable if the source
    # table is rebuilt with reordered columns, since ALTER..ADD always appends.
    # Set "fast_insert": false in the config to force streaming.
    if cfg.get("fast_insert", True) and sql.lstrip().upper().startswith("EXEC"):
        fast = _try_server_side_insert(engine, sql, values, schema, table,
                                       session_code, stamp, do_clear, retention,
                                       step_label)
        if fast is not None:
            return fast

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
                    return {"table": f"{schema}.{table}", "rows": 0, "purged": 0, "replaced": False}

            ddl = wraw.cursor()
            try:
                data_cols = _ensure_table(ddl, schema, table, cursor.description, stamp)
                if do_clear:
                    _clear(ddl, schema, table)
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
                f"{rows_written} row(s) {'replaced' if do_clear else 'appended'}"
                f", {purged} purged")
    return {"table": f"{schema}.{table}", "rows": rows_written, "purged": purged,
            "replaced": bool(do_clear)}
