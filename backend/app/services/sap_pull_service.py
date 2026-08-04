"""
SAP pull service — the heart of the SAP module.

A "pull definition" says: WHAT to fetch from SAP (an RFC table or an OData
entity) → WHICH SQL table to land it in (SAP_ prefixed) → on WHAT schedule.
This service owns the CRUD for those definitions, executing a pull (fetch via
sap_client, land rows into the target table), and the run history.

Read-only w.r.t. SAP. The only writes are into local SAP_* staging tables.

Storage: Rep_data (data engine), self-healing DDL. Target tables are created
lazily on first run — all columns NVARCHAR (SAP RFC returns strings) plus the
audit columns _RUN_ID / _PULLED_AT.
"""
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine
from app.services import sap_client
from app.services.sap_client import SapError
# Reuse the report scheduler's schedule math so both modules behave identically.
from app.services.report_scheduler_service import compute_next_run

DEF_TABLE = "SAP_PULL_DEF"
RUN_TABLE = "SAP_PULL_RUN"

_RFC_PAGE = 5000       # RFC_READ_TABLE page size
_ODATA_PAGE = 2000     # OData page size (Gateway caps at 5000)
_COL_MAXLEN = 1000     # NVARCHAR width / value truncation for landed columns
_PYODBC_PARAM_CAP = 2000  # stay under pyodbc's 2100 bound-parameter limit

VALID_DOORS = ("rfc_table", "odata", "snowflake")
VALID_MODES = ("replace", "append")


# ── Self-healing DDL ─────────────────────────────────────────────────────────
def ensure_pull_tables() -> None:
    eng = get_data_engine()
    with eng.begin() as c:
        c.execute(text(f"""
            IF OBJECT_ID('dbo.{DEF_TABLE}', 'U') IS NULL
            CREATE TABLE dbo.{DEF_TABLE} (
                PULL_ID       INT IDENTITY(1,1) PRIMARY KEY,
                NAME          NVARCHAR(200)  NOT NULL,
                DESCRIPTION   NVARCHAR(500)  NULL,
                DOOR          NVARCHAR(20)   NOT NULL DEFAULT 'rfc_table',
                SAP_TABLE     NVARCHAR(64)   NULL,
                FIELDS        NVARCHAR(MAX)  NULL,
                WHERE_CLAUSE  NVARCHAR(MAX)  NULL,
                WHERE_JSON    NVARCHAR(MAX)  NULL,
                ODATA_SERVICE NVARCHAR(128)  NULL,
                ODATA_ENTITY  NVARCHAR(128)  NULL,
                ODATA_FILTER  NVARCHAR(MAX)  NULL,
                ODATA_SELECT  NVARCHAR(MAX)  NULL,
                SF_QUERY      NVARCHAR(MAX)  NULL,
                SF_DATABASE   NVARCHAR(128)  NULL,
                SF_SCHEMA     NVARCHAR(128)  NULL,
                ENV           NVARCHAR(10)   NULL,
                ROW_LIMIT     INT            NOT NULL DEFAULT 50000,
                TARGET_TABLE  NVARCHAR(128)  NOT NULL,
                WRITE_MODE    NVARCHAR(10)   NOT NULL DEFAULT 'replace',
                TRIGGER_TYPE  NVARCHAR(20)   NOT NULL DEFAULT 'manual',
                SCHEDULE_CONFIG NVARCHAR(MAX) NULL,
                ENABLED       BIT            NOT NULL DEFAULT 1,
                NEXT_RUN_AT   DATETIME       NULL,
                LAST_RUN_AT   DATETIME       NULL,
                LAST_STATUS   NVARCHAR(20)   NULL,
                LAST_ROWS     INT            NULL,
                CREATED_BY    NVARCHAR(128)  NULL,
                CREATED_AT    DATETIME       NOT NULL DEFAULT SYSUTCDATETIME(),
                UPDATED_AT    DATETIME       NOT NULL DEFAULT SYSUTCDATETIME()
            )
        """))
        c.execute(text(f"""
            IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_{DEF_TABLE}_due')
            CREATE INDEX IX_{DEF_TABLE}_due
                ON dbo.{DEF_TABLE} (TRIGGER_TYPE, ENABLED, NEXT_RUN_AT)
        """))
        # Self-heal: add WHERE_JSON to tables created before the condition builder.
        c.execute(text(f"""
            IF COL_LENGTH('dbo.{DEF_TABLE}','WHERE_JSON') IS NULL
            ALTER TABLE dbo.{DEF_TABLE} ADD WHERE_JSON NVARCHAR(MAX) NULL
        """))
        # Self-heal: add Snowflake-door columns to pre-existing tables.
        for col, ddl in (("SF_QUERY", "NVARCHAR(MAX) NULL"),
                         ("SF_DATABASE", "NVARCHAR(128) NULL"),
                         ("SF_SCHEMA", "NVARCHAR(128) NULL")):
            c.execute(text(f"""
                IF COL_LENGTH('dbo.{DEF_TABLE}','{col}') IS NULL
                ALTER TABLE dbo.{DEF_TABLE} ADD {col} {ddl}
            """))
        c.execute(text(f"""
            IF OBJECT_ID('dbo.{RUN_TABLE}', 'U') IS NULL
            CREATE TABLE dbo.{RUN_TABLE} (
                RUN_ID        BIGINT IDENTITY(1,1) PRIMARY KEY,
                PULL_ID       INT            NOT NULL,
                TRIGGER_SOURCE NVARCHAR(40)  NOT NULL,
                STATUS        NVARCHAR(20)   NOT NULL DEFAULT 'running',
                ROWS_FETCHED  INT            NULL,
                TARGET_TABLE  NVARCHAR(128)  NULL,
                MESSAGE       NVARCHAR(MAX)  NULL,
                STARTED_AT    DATETIME       NULL,
                COMPLETED_AT  DATETIME       NULL,
                DURATION_MS   INT            NULL,
                CREATED_BY    NVARCHAR(128)  NULL,
                CREATED_AT    DATETIME       NOT NULL DEFAULT SYSUTCDATETIME()
            )
        """))
        c.execute(text(f"""
            IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_{RUN_TABLE}_pull')
            CREATE INDEX IX_{RUN_TABLE}_pull ON dbo.{RUN_TABLE} (PULL_ID, CREATED_AT DESC)
        """))


# ── Helpers ──────────────────────────────────────────────────────────────────
def _safe_ident(name: str, fallback: str = "COL") -> str:
    """A SQL-safe identifier from an arbitrary SAP/OData column name."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", str(name or "").strip())
    if not s:
        s = fallback
    if s[0].isdigit():
        s = "C_" + s
    return s[:120]


def _jarr(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        v = [x.strip() for x in v.split(",") if x.strip()] if "," in v else ([v] if v.strip() else [])
    return json.dumps(list(v)) if v else None


def _row_to_dict(r) -> Dict[str, Any]:
    d = dict(r)
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    # Parse JSON columns back to lists for the UI.
    for jk in ("FIELDS", "WHERE_JSON"):
        if d.get(jk):
            try:
                d[jk] = json.loads(d[jk])
            except Exception:
                pass
    return d


_NO_VALUE_OPS = {"IS NULL", "IS NOT NULL"}
_VALID_OPS = {"=", "<>", "<", "<=", ">", ">=", "LIKE", "NOT LIKE",
              "IN", "NOT IN", "IS NULL", "IS NOT NULL"}


def _q(v: Any) -> str:
    """Quote a value for an RFC WHERE clause — numbers bare, everything else 'x'."""
    s = str("" if v is None else v).strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return s
    return "'" + s.replace("'", "''") + "'"


def build_where_clause(conds: List[Dict[str, Any]]) -> str:
    """Build an RFC_READ_TABLE WHERE string from structured conditions.
    Mirrors buildWhere() in frontend/src/utils/sapWhere.js — keep in sync."""
    parts: List[str] = []
    for i, c in enumerate(conds or []):
        field = str(c.get("field") or "").strip()
        if not field:
            continue
        op = str(c.get("op") or "=").strip().upper()
        if op not in _VALID_OPS:
            op = "="
        if op in _NO_VALUE_OPS:
            expr = f"{field} {op}"
        elif op in ("IN", "NOT IN"):
            raw = c.get("value")
            items = raw if isinstance(raw, list) else [
                x.strip() for x in str(raw or "").split(",") if x.strip()]
            if not items:
                continue
            expr = f"{field} {op} ({', '.join(_q(x) for x in items)})"
        else:
            val = c.get("value")
            if val is None or str(val).strip() == "":
                continue
            expr = f"{field} {op} {_q(val)}"
        conn = (str(c.get("conn") or "AND").strip().upper() + " ") if i > 0 and parts else ""
        parts.append(conn + expr)
    return " ".join(parts).strip()


def _sanitize_target(name: str) -> str:
    """Force the target table to a safe SAP_-prefixed identifier."""
    s = _safe_ident(name, "SAP_TARGET")
    if not s.upper().startswith("SAP_"):
        s = "SAP_" + s
    return s


# ── CRUD ─────────────────────────────────────────────────────────────────────
def list_pulls() -> List[Dict[str, Any]]:
    ensure_pull_tables()
    eng = get_data_engine()
    with eng.connect() as c:
        rows = c.execute(text(f"""
            SELECT PULL_ID, NAME, DESCRIPTION, DOOR, SAP_TABLE, FIELDS, WHERE_CLAUSE, WHERE_JSON,
                   ODATA_SERVICE, ODATA_ENTITY, ODATA_FILTER, ODATA_SELECT,
                   SF_QUERY, SF_DATABASE, SF_SCHEMA, ENV,
                   ROW_LIMIT, TARGET_TABLE, WRITE_MODE, TRIGGER_TYPE, SCHEDULE_CONFIG,
                   ENABLED, NEXT_RUN_AT, LAST_RUN_AT, LAST_STATUS, LAST_ROWS, UPDATED_AT
            FROM {DEF_TABLE} ORDER BY NAME
        """)).mappings().fetchall()
    return [_row_to_dict(r) for r in rows]


def get_pull(pull_id: int) -> Optional[Dict[str, Any]]:
    ensure_pull_tables()
    eng = get_data_engine()
    with eng.connect() as c:
        row = c.execute(text(f"""
            SELECT * FROM {DEF_TABLE} WHERE PULL_ID = :id
        """), {"id": pull_id}).mappings().fetchone()
    return _row_to_dict(row) if row else None


def _validate(p: Dict[str, Any]) -> None:
    door = (p.get("door") or "rfc_table").lower()
    if door not in VALID_DOORS:
        raise ValueError(f"Invalid door '{door}'")
    if (p.get("write_mode") or "replace").lower() not in VALID_MODES:
        raise ValueError("write_mode must be 'replace' or 'append'")
    if not (p.get("name") or "").strip():
        raise ValueError("Name is required")
    if not (p.get("target_table") or "").strip():
        raise ValueError("Target table is required")
    if door == "rfc_table" and not (p.get("sap_table") or "").strip():
        raise ValueError("SAP table is required for the RFC door")
    if door == "odata" and not ((p.get("odata_service") or "").strip()
                                and (p.get("odata_entity") or "").strip()):
        raise ValueError("OData service and entity are required for the OData door")
    if door == "snowflake" and not (p.get("sf_query") or "").strip():
        raise ValueError("A SELECT query is required for the Snowflake door")


def _payload_cols(p: Dict[str, Any]) -> Dict[str, Any]:
    door = (p.get("door") or "rfc_table").lower()
    trig = (p.get("trigger_type") or "manual").lower()
    sched = p.get("schedule_config")
    sched_json = json.dumps(sched) if isinstance(sched, (dict, list)) else (sched or None)
    next_run = None
    if trig == "schedule" and isinstance(sched, dict):
        next_run = compute_next_run(sched)
    # Structured conditions (the WHERE builder) are the source of truth when
    # present — the executed clause is rebuilt from them here. Otherwise fall
    # back to a raw where_clause typed by an advanced user.
    conds = p.get("where_json")
    where_json_str = None
    where_clause = (p.get("where_clause") or "").strip() or None
    if isinstance(conds, list):
        clean = [c for c in conds if isinstance(c, dict) and str(c.get("field") or "").strip()]
        if clean:
            where_json_str = json.dumps(clean)
            where_clause = build_where_clause(clean) or None
    return {
        "name": (p.get("name") or "").strip(),
        "desc": (p.get("description") or "").strip() or None,
        "door": door,
        "sap_table": (p.get("sap_table") or "").strip().upper() or None,
        "fields": _jarr(p.get("fields")),
        "where": where_clause,
        "wjson": where_json_str,
        "osvc": (p.get("odata_service") or "").strip() or None,
        "oent": (p.get("odata_entity") or "").strip() or None,
        "ofil": (p.get("odata_filter") or "").strip() or None,
        "osel": (p.get("odata_select") or "").strip() or None,
        "sfq": (p.get("sf_query") or "").strip() or None,
        "sfdb": (p.get("sf_database") or "").strip() or None,
        "sfsc": (p.get("sf_schema") or "").strip() or None,
        "env": (p.get("env") or "").strip().lower() or None,
        "rowlimit": int(p.get("row_limit") or 50000),
        "target": _sanitize_target(p.get("target_table")),
        "mode": (p.get("write_mode") or "replace").lower(),
        "trig": trig,
        "sched": sched_json,
        "en": 1 if p.get("enabled", True) else 0,
        "next_run": next_run,
    }


def create_pull(p: Dict[str, Any], user: str) -> Dict[str, Any]:
    _validate(p)
    ensure_pull_tables()
    v = _payload_cols(p)
    v["user"] = user
    eng = get_data_engine()
    with eng.begin() as c:
        new_id = c.execute(text(f"""
            INSERT INTO {DEF_TABLE}
                (NAME, DESCRIPTION, DOOR, SAP_TABLE, FIELDS, WHERE_CLAUSE, WHERE_JSON,
                 ODATA_SERVICE, ODATA_ENTITY, ODATA_FILTER, ODATA_SELECT,
                 SF_QUERY, SF_DATABASE, SF_SCHEMA, ENV,
                 ROW_LIMIT, TARGET_TABLE, WRITE_MODE, TRIGGER_TYPE, SCHEDULE_CONFIG,
                 ENABLED, NEXT_RUN_AT, CREATED_BY)
            OUTPUT INSERTED.PULL_ID
            VALUES (:name, :desc, :door, :sap_table, :fields, :where, :wjson,
                    :osvc, :oent, :ofil, :osel,
                    :sfq, :sfdb, :sfsc, :env,
                    :rowlimit, :target, :mode, :trig, :sched,
                    :en, :next_run, :user)
        """), v).scalar()
    return get_pull(int(new_id))


def update_pull(pull_id: int, p: Dict[str, Any], user: str) -> Dict[str, Any]:
    if not get_pull(pull_id):
        raise KeyError(pull_id)
    _validate(p)
    v = _payload_cols(p)
    v["id"] = pull_id
    v["user"] = user
    eng = get_data_engine()
    with eng.begin() as c:
        c.execute(text(f"""
            UPDATE {DEF_TABLE} SET
                NAME=:name, DESCRIPTION=:desc, DOOR=:door, SAP_TABLE=:sap_table,
                FIELDS=:fields, WHERE_CLAUSE=:where, WHERE_JSON=:wjson, ODATA_SERVICE=:osvc,
                ODATA_ENTITY=:oent, ODATA_FILTER=:ofil, ODATA_SELECT=:osel,
                SF_QUERY=:sfq, SF_DATABASE=:sfdb, SF_SCHEMA=:sfsc, ENV=:env,
                ROW_LIMIT=:rowlimit, TARGET_TABLE=:target, WRITE_MODE=:mode,
                TRIGGER_TYPE=:trig, SCHEDULE_CONFIG=:sched, ENABLED=:en,
                NEXT_RUN_AT=:next_run, UPDATED_AT=SYSUTCDATETIME()
            WHERE PULL_ID=:id
        """), v)
    return get_pull(pull_id)


def set_enabled(pull_id: int, enabled: bool) -> Dict[str, Any]:
    if not get_pull(pull_id):
        raise KeyError(pull_id)
    eng = get_data_engine()
    with eng.begin() as c:
        c.execute(text(f"""
            UPDATE {DEF_TABLE} SET ENABLED=:en, UPDATED_AT=SYSUTCDATETIME()
            WHERE PULL_ID=:id
        """), {"en": 1 if enabled else 0, "id": pull_id})
    return get_pull(pull_id)


def delete_pull(pull_id: int) -> None:
    eng = get_data_engine()
    with eng.begin() as c:
        c.execute(text(f"DELETE FROM {DEF_TABLE} WHERE PULL_ID=:id"), {"id": pull_id})


# ── Run history ──────────────────────────────────────────────────────────────
def list_runs(pull_id: Optional[int] = None, limit: int = 50) -> List[Dict[str, Any]]:
    ensure_pull_tables()
    eng = get_data_engine()
    where = "WHERE PULL_ID = :pid" if pull_id else ""
    params: Dict[str, Any] = {"lim": int(limit)}
    if pull_id:
        params["pid"] = pull_id
    with eng.connect() as c:
        rows = c.execute(text(f"""
            SELECT TOP (:lim) r.RUN_ID, r.PULL_ID, d.NAME AS PULL_NAME,
                   r.TRIGGER_SOURCE, r.STATUS, r.ROWS_FETCHED, r.TARGET_TABLE,
                   r.MESSAGE, r.STARTED_AT, r.COMPLETED_AT, r.DURATION_MS, r.CREATED_BY
            FROM {RUN_TABLE} r
            LEFT JOIN {DEF_TABLE} d ON d.PULL_ID = r.PULL_ID
            {where}
            ORDER BY r.RUN_ID DESC
        """), params).mappings().fetchall()
    return [_row_to_dict(r) for r in rows]


def _open_run(pull_id: int, trigger_source: str, user: Optional[str],
              target: str) -> int:
    eng = get_data_engine()
    with eng.begin() as c:
        return int(c.execute(text(f"""
            INSERT INTO {RUN_TABLE}
                (PULL_ID, TRIGGER_SOURCE, STATUS, TARGET_TABLE, STARTED_AT, CREATED_BY)
            OUTPUT INSERTED.RUN_ID
            VALUES (:pid, :ts, 'running', :tgt, SYSUTCDATETIME(), :cb)
        """), {"pid": pull_id, "ts": trigger_source, "tgt": target, "cb": user}).scalar())


def _close_run(run_id: int, status: str, rows: int, message: str,
               started: datetime) -> None:
    dur = int((datetime.utcnow() - started).total_seconds() * 1000)
    eng = get_data_engine()
    with eng.begin() as c:
        c.execute(text(f"""
            UPDATE {RUN_TABLE}
            SET STATUS=:s, ROWS_FETCHED=:r, MESSAGE=:m,
                COMPLETED_AT=SYSUTCDATETIME(), DURATION_MS=:d
            WHERE RUN_ID=:id
        """), {"s": status, "r": rows, "m": (message or "")[:4000],
               "d": dur, "id": run_id})


def _update_pull_after(pull_id: int, status: str, rows: Optional[int]) -> None:
    eng = get_data_engine()
    with eng.begin() as c:
        c.execute(text(f"""
            UPDATE {DEF_TABLE}
            SET LAST_RUN_AT=SYSUTCDATETIME(), LAST_STATUS=:s, LAST_ROWS=:r,
                UPDATED_AT=SYSUTCDATETIME()
            WHERE PULL_ID=:id
        """), {"s": status, "r": rows, "id": pull_id})


# ── Fetch from SAP (paged) ───────────────────────────────────────────────────
def _fetch(pull: Dict[str, Any]) -> Tuple[List[str], List[Dict[str, Any]]]:
    door = (pull.get("DOOR") or "rfc_table").lower()
    row_limit = int(pull.get("ROW_LIMIT") or 50000)
    env = (pull.get("ENV") or None)
    columns: List[str] = []
    all_rows: List[Dict[str, Any]] = []

    if door == "rfc_table":
        fields = pull.get("FIELDS")
        if isinstance(fields, str) and fields:
            try:
                fields = json.loads(fields)
            except Exception:
                fields = [f.strip() for f in fields.split(",") if f.strip()]
        offset = 0
        while len(all_rows) < row_limit:
            page = min(_RFC_PAGE, row_limit - len(all_rows))
            res = sap_client.read_table(
                table=pull.get("SAP_TABLE"), fields=fields or None,
                where=pull.get("WHERE_CLAUSE") or None,
                limit=page, offset=offset, env=env)
            batch = res["rows"]
            columns = columns or res["columns"]
            all_rows.extend(batch)
            if len(batch) < page:
                break
            offset += len(batch)
    elif door == "snowflake":
        # Direct Snowflake read (e.g. V2RETAIL.BRONZE.SAP_*) — no gateway relay.
        from app.services import sap_snowflake_client as sf
        res = sf.run_query(
            pull.get("SF_QUERY") or "", row_limit=row_limit,
            database=pull.get("SF_DATABASE") or None,
            schema=pull.get("SF_SCHEMA") or None)
        columns = res["columns"]
        all_rows = res["rows"]
    else:  # odata
        skip = 0
        while len(all_rows) < row_limit:
            top = min(_ODATA_PAGE, row_limit - len(all_rows))
            res = sap_client.odata_pull(
                service=pull.get("ODATA_SERVICE"), entity=pull.get("ODATA_ENTITY"),
                top=top, skip=skip, filter=pull.get("ODATA_FILTER") or None,
                select=pull.get("ODATA_SELECT") or None, env=env)
            batch = res["rows"]
            columns = columns or res["columns"]
            all_rows.extend(batch)
            if len(batch) < top:
                break
            skip += len(batch)

    if not columns and all_rows:
        columns = list(all_rows[0].keys())
    return columns, all_rows


# ── Land rows into the target SQL table ──────────────────────────────────────
def _land_rows(target: str, columns: List[str], rows: List[Dict[str, Any]],
               write_mode: str, run_id: int) -> int:
    target = _sanitize_target(target)
    # original SAP column -> safe SQL column (dedup collisions)
    colmap: Dict[str, str] = {}
    used = set()
    for orig in columns:
        safe = _safe_ident(orig)
        base, i = safe, 2
        while safe in used:
            safe = f"{base}_{i}"; i += 1
        used.add(safe)
        colmap[orig] = safe
    safe_cols = list(colmap.values())

    eng = get_data_engine()
    with eng.begin() as c:
        # Create the target if missing, else self-heal missing columns.
        exists = c.execute(text(
            "SELECT OBJECT_ID('dbo.' + :t, 'U')"), {"t": target}).scalar()
        if not exists:
            col_defs = ",\n    ".join(f"[{sc}] NVARCHAR({_COL_MAXLEN}) NULL"
                                      for sc in safe_cols)
            col_defs = (col_defs + ",\n    ") if col_defs else ""
            c.execute(text(f"""
                CREATE TABLE dbo.[{target}] (
                    _SEQ BIGINT IDENTITY(1,1) PRIMARY KEY,
                    {col_defs}_RUN_ID BIGINT NULL,
                    _PULLED_AT DATETIME2 NULL DEFAULT SYSUTCDATETIME()
                )
            """))
        else:
            for sc in safe_cols:
                c.execute(text(f"""
                    IF COL_LENGTH('dbo.{target}', :col) IS NULL
                    ALTER TABLE dbo.[{target}] ADD [{sc}] NVARCHAR({_COL_MAXLEN}) NULL
                """), {"col": sc})

        if (write_mode or "replace").lower() == "replace":
            c.execute(text(f"TRUNCATE TABLE dbo.[{target}]"))

        if not rows:
            return 0

        insert_cols = safe_cols + ["_RUN_ID"]
        placeholders = ", ".join(f":{sc}" for sc in insert_cols)
        col_list = ", ".join(f"[{sc}]" for sc in insert_cols)
        insert_sql = text(f"INSERT INTO dbo.[{target}] ({col_list}) VALUES ({placeholders})")

        # Chunk to stay under pyodbc's bound-parameter cap.
        per_batch = max(1, _PYODBC_PARAM_CAP // max(1, len(insert_cols)))
        total = 0
        for start in range(0, len(rows), per_batch):
            chunk = rows[start:start + per_batch]
            params = []
            for r in chunk:
                p = {}
                for orig, sc in colmap.items():
                    val = r.get(orig)
                    if val is not None and not isinstance(val, str):
                        val = str(val)
                    if isinstance(val, str) and len(val) > _COL_MAXLEN:
                        val = val[:_COL_MAXLEN]
                    p[sc] = val
                p["_RUN_ID"] = run_id
                params.append(p)
            c.execute(insert_sql, params)
            total += len(chunk)
    return total


# ── The engine ───────────────────────────────────────────────────────────────
def run_pull(pull: Any, trigger_source: str = "manual",
             user: Optional[str] = None) -> Dict[str, Any]:
    """Execute one pull end-to-end. `pull` may be a pull_id or a loaded row.
    Returns a result summary; never raises (records the failure on the run)."""
    if isinstance(pull, int):
        pull = get_pull(pull)
    if not pull:
        return {"success": False, "error": "pull definition not found"}

    pull_id = int(pull["PULL_ID"])
    target = _sanitize_target(pull["TARGET_TABLE"])
    started = datetime.utcnow()
    run_id = _open_run(pull_id, trigger_source, user, target)
    try:
        columns, rows = _fetch(pull)
        landed = _land_rows(target, columns, rows,
                            pull.get("WRITE_MODE") or "replace", run_id)
        msg = f"Landed {landed} row(s) into {target} ({len(columns)} column(s))."
        _close_run(run_id, "success", landed, msg, started)
        _update_pull_after(pull_id, "success", landed)
        logger.info(f"[sap-pull] {pull.get('NAME')} → {msg}")
        return {"success": True, "run_id": run_id, "rows": landed,
                "target": target, "message": msg}
    except SapError as e:
        _close_run(run_id, "failed", 0, str(e), started)
        _update_pull_after(pull_id, "failed", None)
        logger.warning(f"[sap-pull] {pull.get('NAME')} failed: {e}")
        return {"success": False, "run_id": run_id, "error": str(e)}
    except Exception as e:
        _close_run(run_id, "failed", 0, f"{type(e).__name__}: {e}", started)
        _update_pull_after(pull_id, "failed", None)
        logger.exception(f"[sap-pull] {pull.get('NAME')} crashed")
        return {"success": False, "run_id": run_id, "error": str(e)}


def run_now(pull_id: int, user: Optional[str] = None) -> Dict[str, Any]:
    return run_pull(pull_id, "manual", user)
