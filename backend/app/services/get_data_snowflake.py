"""
Get Data — Snowflake side: the view builder, the source browser, and the
Snowflake → SQL Server type mapping used by the sync engine.

Views the module creates live ONLY in V2RETAIL.ARS_GETDATA and are named
V_GD_*. The user supplies a SELECT/WITH body; the module adds
CREATE OR REPLACE VIEW itself, and always validates before it creates
(read-only → compiles → unique column names → row count). Views anywhere else
(ARS_GOLD, GOLD, SILVER, …) are read-only sources.

Uses the single app-wide Snowflake connection (snowflake_config_service).
Spec: frontend/public/docs/manual/get_data.md.
"""
import json
import math
import re
import time
from datetime import date, datetime, time as dtime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine
from app.services import snowflake_config_service as sfc
from app.services.get_data_schema import (
    ensure_tables, VIEW_TABLE, VIEW_VERSION_TABLE, JOB_TABLE,
)

GD_DATABASE = "V2RETAIL"
GD_SCHEMA = "ARS_GETDATA"
VIEW_PREFIX = "V_GD_"
QUERY_TAG = "ARS_GET_DATA"
PREVIEW_MAX = 500
AUDIT_COLS = ("_GD_RUN_ID", "_GD_LOADED_AT")

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,254}$")

try:  # id → name ('FIXED', 'TEXT', …); import-safe without the connector
    from snowflake.connector.constants import FIELD_ID_TO_NAME
except Exception:  # pragma: no cover
    FIELD_ID_TO_NAME = dict(enumerate([
        "FIXED", "REAL", "TEXT", "DATE", "TIMESTAMP", "VARIANT", "TIMESTAMP_LTZ",
        "TIMESTAMP_TZ", "TIMESTAMP_NTZ", "OBJECT", "ARRAY", "BINARY", "TIME",
        "BOOLEAN", "GEOGRAPHY", "GEOMETRY", "VECTOR"]))


class GetDataError(Exception):
    """A user-facing Get Data failure with an actionable message."""


# ── Connection ───────────────────────────────────────────────────────────────
def connect(long_running: bool = False):
    """Snowflake connection tagged ARS_GET_DATA (visible in Snowflake query
    history). Sync runs get a 1 h statement timeout; UI calls 5 min.
    The session runs in UTC — the account default is US Pacific — so TIMESTAMP
    watermarks, which are stored in UTC locally, compare exactly."""
    # Sync runs read Arrow batches (bulk loader): ask for exact decimals, or the
    # connector hands NUMBER(p<=18, s>0) over as float64.
    kw = {"network_timeout": 1800, "arrow_number_to_decimal": True} if long_running else {}
    conn = sfc.connect(require_enabled=True, **kw)
    cur = conn.cursor()
    try:
        cur.execute(f"ALTER SESSION SET QUERY_TAG = '{QUERY_TAG}', TIMEZONE = 'UTC', "
                    f"STATEMENT_TIMEOUT_IN_SECONDS = {3600 if long_running else 300}")
    finally:
        cur.close()
    return conn


def sf_message(e: Exception) -> str:
    """Snowflake errors carry a code prefix and newlines — keep them readable."""
    return re.sub(r"\s+", " ", str(e)).strip()[:1500]


# ── Identifiers and SQL bodies ───────────────────────────────────────────────
def ident(name: Any, what: str = "name") -> str:
    s = str(name or "").strip().strip('"').upper()
    if not _IDENT.match(s):
        raise GetDataError(f"Invalid Snowflake {what} '{name}'. Use letters, digits and _ only.")
    return s


def split_object(fq: Any) -> Tuple[str, str, str]:
    parts = [p for p in str(fq or "").strip().split(".")]
    if len(parts) != 3:
        raise GetDataError("Source must be DATABASE.SCHEMA.NAME, "
                           "e.g. V2RETAIL.ARS_GETDATA.V_GD_STORE_STOCK")
    return (ident(parts[0], "database"), ident(parts[1], "schema"),
            ident(parts[2], "object name"))


def normalize_object(fq: Any) -> str:
    return ".".join(split_object(fq))


def quote_object(fq: Any) -> str:
    db, sch, name = split_object(fq)
    return f'"{db}"."{sch}"."{name}"'


def qcol(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def normalize_view_name(name: Any) -> str:
    s = str(name or "").strip().strip('"').upper()
    if not s:
        raise GetDataError("View name is required.")
    if not s.startswith(VIEW_PREFIX):
        s = VIEW_PREFIX + s
    s = ident(s, "view name")
    if len(s) > 120:
        raise GetDataError("View name is too long (120 characters at most).")
    return s


_LEAD_COMMENT = re.compile(r"^\s*(--[^\n]*(\n|$)|/\*.*?\*/)", re.S)


def _strip_literals(sql: str) -> str:
    s = re.sub(r"'(?:[^'\\]|\\.|'')*'", "''", sql)
    s = re.sub(r"\$\$.*?\$\$", "''", s, flags=re.S)
    s = re.sub(r"--[^\n]*", "", s)
    return re.sub(r"/\*.*?\*/", "", s, flags=re.S)


def clean_select(sql: Any) -> str:
    """The SELECT/WITH body as the user wrote it, minus a trailing ';'.
    Raises GetDataError when it is not a single read-only query."""
    s = str(sql or "").strip().rstrip(";").rstrip()
    head = s
    while True:
        m = _LEAD_COMMENT.match(head)
        if not m:
            break
        head = head[m.end():]
    head = head.lstrip().lstrip("(").lstrip().lower()
    if not head:
        raise GetDataError("The query is empty.")
    if not (head.startswith("select") or head.startswith("with")):
        raise GetDataError("Only a read-only SELECT or WITH query is allowed — "
                           "the module adds CREATE VIEW itself.")
    if ";" in _strip_literals(s):
        raise GetDataError("Only one statement is allowed — remove the ';' inside the query.")
    return s


def wrap(body: str) -> str:
    """Newlines keep a trailing '-- comment' in the body from eating the ')'."""
    return f"(\n{body}\n)"


# ── Column metadata and type mapping ─────────────────────────────────────────
def _meta_attr(d: Any, name: str, idx: int) -> Any:
    v = getattr(d, name, None)
    if v is None:
        try:
            v = d[idx]
        except Exception:
            v = None
    return v


def column_meta(description) -> List[Dict[str, Any]]:
    """cursor.description → [{name, sf_type, size, precision, scale, nullable}]."""
    out = []
    for d in description or []:
        type_code = _meta_attr(d, "type_code", 1)
        nullable = _meta_attr(d, "is_nullable", 6)
        out.append({
            "name": _meta_attr(d, "name", 0),
            "sf_type": FIELD_ID_TO_NAME.get(type_code, str(type_code)),
            "size": _meta_attr(d, "internal_size", 3),
            "precision": _meta_attr(d, "precision", 4),
            "scale": _meta_attr(d, "scale", 5),
            "nullable": True if nullable is None else bool(nullable),
        })
    return out


def sf_type_label(m: Dict[str, Any]) -> str:
    t = m["sf_type"]
    if t == "FIXED":
        return f"NUMBER({m.get('precision') or 38},{m.get('scale') or 0})"
    if t == "TEXT":
        return f"VARCHAR({m.get('size')})" if m.get("size") else "VARCHAR"
    return {"REAL": "FLOAT"}.get(t, t)


_STR_BUCKETS = (10, 20, 50, 100, 255, 500, 1000, 2000, 4000)
_BIGINT_SAFE = 9_000_000_000_000_000_000


def str_width(max_len: Optional[int]) -> Optional[int]:
    """Measured max length → an NVARCHAR width with 25% headroom, rounded up to
    a bucket so the width is stable day to day. None = NVARCHAR(MAX)."""
    if max_len is None:
        return 255
    need = max(1, int(math.ceil(int(max_len) * 1.25)))
    for b in _STR_BUCKETS:
        if need <= b:
            return b
    return None


def needs_profile(m: Dict[str, Any]) -> Optional[str]:
    """'len' for text wider than 4000 (Snowflake's default 16 MB VARCHAR),
    'abs' for NUMBER(>18,0); None when the declared type is enough."""
    if m["sf_type"] == "TEXT" and not (m.get("size") and m["size"] <= 4000):
        return "len"
    if m["sf_type"] == "FIXED" and not (m.get("scale") or 0) and int(m.get("precision") or 38) > 18:
        return "abs"
    return None


def map_column(m: Dict[str, Any], profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Snowflake column → SQL Server column spec {kind, sql_type, width, precision, scale}."""
    t = m["sf_type"]
    prof = profile or {}
    if t == "FIXED":
        s = int(m.get("scale") or 0)
        p = int(m.get("precision") or 38)
        if s > 0:
            p = min(max(p, s), 38)
            return {"kind": "dec", "sql_type": f"DECIMAL({p},{s})", "precision": p, "scale": s}
        if p <= 9:
            return {"kind": "int", "sql_type": "INT"}
        if p <= 18:
            return {"kind": "bigint", "sql_type": "BIGINT"}
        mx = prof.get("max_abs")
        if mx is None or int(mx) < _BIGINT_SAFE:
            return {"kind": "bigint", "sql_type": "BIGINT"}
        return {"kind": "dec", "sql_type": "DECIMAL(38,0)", "precision": 38, "scale": 0}
    if t == "REAL":
        return {"kind": "float", "sql_type": "FLOAT"}
    if t == "TEXT":
        size = m.get("size")
        width = int(size) if size and int(size) <= 4000 else str_width(prof.get("max_len"))
        # VARCHAR (half the bytes, faster loads) only when the profile measured
        # every value as plain printable ASCII; anything else stays NVARCHAR.
        ascii_only = prof.get("non_ascii") == 0
        base = "VARCHAR" if ascii_only else "NVARCHAR"
        return {"kind": "str", "width": width, "varchar": ascii_only,
                "sql_type": f"{base}({width})" if width else f"{base}(MAX)"}
    if t == "DATE":
        return {"kind": "date", "sql_type": "DATE"}
    if t.startswith("TIMESTAMP"):
        return {"kind": "datetime", "sql_type": "DATETIME2(6)"}
    if t == "TIME":
        return {"kind": "time", "width": 20, "sql_type": "NVARCHAR(20)"}
    if t == "BOOLEAN":
        return {"kind": "bit", "sql_type": "BIT"}
    if t == "BINARY":
        return {"kind": "bin", "sql_type": "VARBINARY(MAX)"}
    return {"kind": "json", "width": None, "sql_type": "NVARCHAR(MAX)"}


def preview_sql_type(m: Dict[str, Any]) -> str:
    """The local type shown before a load. Text is VARCHAR when every value is
    plain ASCII, else NVARCHAR — decided at load; wide text is sized then too."""
    if m["sf_type"] == "TEXT":
        size = m.get("size")
        return f"(N)VARCHAR({size})" if size and int(size) <= 4000 else "(N)VARCHAR, sized at load"
    return map_column(m)["sql_type"]


def json_safe(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (datetime, date, dtime)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).hex()
    return str(v)


def _dup_names(cols: List[Dict[str, Any]]) -> List[str]:
    seen, dups = set(), []
    for c in cols:
        k = str(c["name"]).upper()
        if k in seen and c["name"] not in dups:
            dups.append(c["name"])
        seen.add(k)
    return dups


# ── Validate / preview ───────────────────────────────────────────────────────
def describe(cur, body: str) -> List[Dict[str, Any]]:
    cur.execute(f"SELECT * FROM {wrap(body)} LIMIT 0")
    return column_meta(cur.description)


def validate_sql(sql: Any, with_count: bool = True) -> Dict[str, Any]:
    """The 'validate first' gate. Every check is reported; ok only if all pass."""
    checks: List[Dict[str, Any]] = []
    try:
        body = clean_select(sql)
    except GetDataError as e:
        checks.append({"key": "readonly", "label": "Read-only SELECT", "ok": False, "detail": str(e)})
        return {"ok": False, "checks": checks, "columns": [], "row_count": None}
    checks.append({"key": "readonly", "label": "Read-only SELECT", "ok": True, "detail": "single statement"})

    conn = connect()
    try:
        cur = conn.cursor()
        t0 = time.time()
        try:
            cols = describe(cur, body)
        except Exception as e:
            checks.append({"key": "compiles", "label": "Compiles in Snowflake", "ok": False,
                           "detail": sf_message(e)})
            return {"ok": False, "checks": checks, "columns": [], "row_count": None}
        checks.append({"key": "compiles", "label": "Compiles in Snowflake", "ok": True,
                       "detail": f"{time.time() - t0:.1f}s"})

        dups = _dup_names(cols)
        reserved = [c["name"] for c in cols if str(c["name"]).upper() in AUDIT_COLS]
        bad = dups + reserved
        checks.append({
            "key": "columns", "label": f"{len(cols)} columns", "ok": not bad,
            "detail": ("Duplicate column names (give each an alias): " + ", ".join(dups)) if dups else
                      (f"{', '.join(reserved)} is reserved for the load audit columns — rename it") if reserved else
                      "names unique, types mapped",
        })

        row_count = None
        if with_count:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {wrap(body)}")
                row_count = int(cur.fetchone()[0])
                checks.append({"key": "rows", "label": "Row count", "ok": True, "detail": f"{row_count:,}"})
            except Exception as e:
                checks.append({"key": "rows", "label": "Row count", "ok": False, "detail": sf_message(e)})

        for c in cols:
            c["sf_type_label"] = sf_type_label(c)
            c["sql_type"] = preview_sql_type(c)
        return {"ok": all(c["ok"] for c in checks), "checks": checks,
                "columns": cols, "row_count": row_count}
    finally:
        conn.close()


def preview(sql: Any = None, obj: Any = None, limit: int = 100) -> Dict[str, Any]:
    body = f"SELECT * FROM {quote_object(obj)}" if obj else clean_select(sql)
    limit = max(1, min(int(limit or 100), PREVIEW_MAX))
    conn = connect()
    try:
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT * FROM {wrap(body)} LIMIT {limit}")
        except Exception as e:
            raise GetDataError(sf_message(e))
        cols = column_meta(cur.description)
        rows = [[json_safe(v) for v in r] for r in cur.fetchall()]
        for c in cols:
            c["sf_type_label"] = sf_type_label(c)
            c["sql_type"] = preview_sql_type(c)
        return {"columns": [c["name"] for c in cols], "column_meta": cols, "rows": rows}
    finally:
        conn.close()


def describe_object(obj: Any) -> Dict[str, Any]:
    fq = normalize_object(obj)
    conn = connect()
    try:
        cur = conn.cursor()
        try:
            cols = describe(cur, f"SELECT * FROM {quote_object(fq)}")
        except Exception as e:
            raise GetDataError(sf_message(e))
        for c in cols:
            c["sf_type_label"] = sf_type_label(c)
            c["sql_type"] = preview_sql_type(c)
        return {"object": fq, "columns": cols}
    finally:
        conn.close()


# ── Browse ───────────────────────────────────────────────────────────────────
def list_schemas(database: str = GD_DATABASE) -> List[Dict[str, Any]]:
    db = ident(database, "database")
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT TABLE_SCHEMA, COUNT_IF(TABLE_TYPE = 'VIEW') AS VIEWS,
                   COUNT_IF(TABLE_TYPE <> 'VIEW') AS TABLES
            FROM "{db}".INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA <> 'INFORMATION_SCHEMA'
            GROUP BY 1 ORDER BY 1
        """)
        return [{"schema": s, "views": int(v or 0), "tables": int(t or 0)}
                for s, v, t in cur.fetchall()]
    finally:
        conn.close()


def list_objects(schema: str, like: Optional[str] = None, database: str = GD_DATABASE,
                 limit: int = 1000) -> List[Dict[str, Any]]:
    db, sch = ident(database, "database"), ident(schema, "schema")
    where, params = ["TABLE_SCHEMA = %s"], [sch]
    if like and like.strip():
        where.append("TABLE_NAME ILIKE %s")
        params.append(f"%{like.strip()}%")
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT TABLE_NAME, TABLE_TYPE, ROW_COUNT, BYTES, LAST_ALTERED, COMMENT
            FROM "{db}".INFORMATION_SCHEMA.TABLES
            WHERE {' AND '.join(where)}
            ORDER BY IFF(TABLE_TYPE = 'VIEW', 0, 1), TABLE_NAME
            LIMIT {max(1, min(int(limit), 5000))}
        """, tuple(params))
        return [{"name": n, "object": f"{db}.{sch}.{n}",
                 "type": "view" if tt == "VIEW" else "table",
                 "row_count": rc, "bytes": b, "last_altered": json_safe(la), "comment": cm}
                for n, tt, rc, b, la, cm in cur.fetchall()]
    finally:
        conn.close()


# ── Module views (V2RETAIL.ARS_GETDATA.V_GD_*) ───────────────────────────────
def _fq_view(name: str) -> str:
    return f"{GD_DATABASE}.{GD_SCHEMA}.{name}"


def _iso(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.replace(microsecond=0).isoformat() + ("Z" if v.tzinfo is None else "")
    return v


def _local_views() -> Dict[str, Dict[str, Any]]:
    ensure_tables()
    with get_data_engine().connect() as c:
        rows = c.execute(text(f"""
            SELECT VIEW_ID, VIEW_NAME, DESCRIPTION, VIEW_SQL, VERSION, COLUMN_COUNT,
                   ROW_COUNT, STATUS, CREATED_BY, CREATED_AT, UPDATED_BY, UPDATED_AT
            FROM {VIEW_TABLE} WHERE SF_DATABASE = :db AND SF_SCHEMA = :sch
        """), {"db": GD_DATABASE, "sch": GD_SCHEMA}).mappings().fetchall()
    return {r["VIEW_NAME"]: {k: _iso(v) for k, v in dict(r).items()} for r in rows}


def _jobs_by_source() -> Dict[str, List[Dict[str, Any]]]:
    ensure_tables()
    with get_data_engine().connect() as c:
        rows = c.execute(text(f"SELECT JOB_ID, JOB_NAME, SOURCE_OBJECT FROM {JOB_TABLE}")).fetchall()
    out: Dict[str, List[Dict[str, Any]]] = {}
    for jid, jname, src in rows:
        out.setdefault(str(src or "").upper(), []).append({"job_id": jid, "job_name": jname})
    return out


def list_views() -> Dict[str, Any]:
    """Snowflake is the truth for what exists; local rows add SQL, version and
    who changed it. A view dropped outside the app shows as 'missing'."""
    local = _local_views()
    jobs = _jobs_by_source()
    sf_rows, sf_error = [], None
    try:
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute(f"""
                SELECT TABLE_NAME, COMMENT, CREATED, LAST_ALTERED
                FROM "{GD_DATABASE}".INFORMATION_SCHEMA.VIEWS
                WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME
            """, (GD_SCHEMA,))
            sf_rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        sf_error = sf_message(e)

    items, seen = [], set()
    for name, comment, created, altered in sf_rows:
        seen.add(name)
        loc = local.get(name) or {}
        items.append({
            "name": name, "object": _fq_view(name), "in_snowflake": True,
            "managed": bool(loc) and loc.get("STATUS") == "active",
            "description": loc.get("DESCRIPTION") or comment,
            "version": loc.get("VERSION"), "column_count": loc.get("COLUMN_COUNT"),
            "row_count": loc.get("ROW_COUNT"),
            "updated_by": loc.get("UPDATED_BY"), "updated_at": loc.get("UPDATED_AT"),
            "sf_created": json_safe(created), "sf_last_altered": json_safe(altered),
            "jobs": jobs.get(_fq_view(name).upper(), []),
        })
    if not sf_error:
        for name, loc in local.items():
            if name not in seen and loc.get("STATUS") == "active":
                items.append({
                    "name": name, "object": _fq_view(name), "in_snowflake": False,
                    "managed": True, "missing": True, "description": loc.get("DESCRIPTION"),
                    "version": loc.get("VERSION"), "column_count": loc.get("COLUMN_COUNT"),
                    "row_count": loc.get("ROW_COUNT"), "updated_by": loc.get("UPDATED_BY"),
                    "updated_at": loc.get("UPDATED_AT"),
                    "jobs": jobs.get(_fq_view(name).upper(), []),
                })
    else:
        for name, loc in local.items():
            if loc.get("STATUS") == "active":
                items.append({"name": name, "object": _fq_view(name), "in_snowflake": None,
                              "managed": True, "description": loc.get("DESCRIPTION"),
                              "version": loc.get("VERSION"), "column_count": loc.get("COLUMN_COUNT"),
                              "row_count": loc.get("ROW_COUNT"), "updated_by": loc.get("UPDATED_BY"),
                              "updated_at": loc.get("UPDATED_AT"),
                              "jobs": jobs.get(_fq_view(name).upper(), [])})
    items.sort(key=lambda r: r["name"])
    return {"database": GD_DATABASE, "schema": GD_SCHEMA, "items": items, "snowflake_error": sf_error}


_DDL_BODY = re.compile(
    r"(?is)^\s*create\s+(?:or\s+replace\s+)?(?:secure\s+)?(?:recursive\s+)?view\s+"
    r"(?:\"[^\"]+\"|[^\s(]+)(?:\s*\([^)]*\))?"
    r"(?:\s+copy\s+grants)?"
    r"(?:\s+comment\s*=\s*'(?:[^'\\]|\\.|'')*')?\s+as\s+(.*?)\s*;?\s*$")


def get_view(name: Any) -> Dict[str, Any]:
    vname = normalize_view_name(name)
    local = _local_views().get(vname)
    out: Dict[str, Any] = {"name": vname, "object": _fq_view(vname),
                           "jobs": _jobs_by_source().get(_fq_view(vname).upper(), [])}
    if local and local.get("STATUS") == "active":
        out.update({"sql": local["VIEW_SQL"], "description": local.get("DESCRIPTION"),
                    "version": local.get("VERSION"), "managed": True,
                    "updated_by": local.get("UPDATED_BY"), "updated_at": local.get("UPDATED_AT")})
        with get_data_engine().connect() as c:
            vers = c.execute(text(f"""
                SELECT VERSION, ACTION, COLUMN_COUNT, ROW_COUNT, CHANGED_BY, CHANGED_AT
                FROM {VIEW_VERSION_TABLE} WHERE VIEW_ID = :id ORDER BY VERSION_ID DESC
            """), {"id": local["VIEW_ID"]}).mappings().fetchall()
        out["versions"] = [{k: _iso(v) for k, v in dict(r).items()} for r in vers]
        return out
    # Created outside the app — recover the body from Snowflake's DDL.
    conn = connect()
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT GET_DDL('VIEW', %s)", (f'"{GD_DATABASE}"."{GD_SCHEMA}"."{vname}"',))
        except Exception as e:
            raise GetDataError(f"View {vname} not found in {GD_DATABASE}.{GD_SCHEMA}: {sf_message(e)}")
        ddl = cur.fetchone()[0] or ""
    finally:
        conn.close()
    m = _DDL_BODY.match(ddl)
    out.update({"sql": m.group(1) if m else ddl, "managed": False, "versions": [],
                "note": "Created outside the app. Saving it here puts it under version control."})
    return out


def _record_view(vname: str, body: str, description: Optional[str], cols: int,
                 rows: Optional[int], user: str) -> None:
    ensure_tables()
    with get_data_engine().begin() as c:
        row = c.execute(text(f"""
            SELECT VIEW_ID, VERSION, STATUS FROM {VIEW_TABLE} WITH (UPDLOCK)
            WHERE SF_DATABASE = :db AND SF_SCHEMA = :sch AND VIEW_NAME = :n
        """), {"db": GD_DATABASE, "sch": GD_SCHEMA, "n": vname}).fetchone()
        if row:
            vid, ver = int(row[0]), int(row[1]) + 1
            action = "replaced" if row[2] == "active" else "created"
            c.execute(text(f"""
                UPDATE {VIEW_TABLE} SET DESCRIPTION = :d, VIEW_SQL = :sql, VERSION = :v,
                    COLUMN_COUNT = :cc, ROW_COUNT = :rc, STATUS = 'active',
                    UPDATED_BY = :u, UPDATED_AT = SYSUTCDATETIME()
                WHERE VIEW_ID = :id
            """), {"d": description, "sql": body, "v": ver, "cc": cols, "rc": rows,
                   "u": user, "id": vid})
        else:
            ver, action = 1, "created"
            vid = int(c.execute(text(f"""
                INSERT INTO {VIEW_TABLE} (VIEW_NAME, SF_DATABASE, SF_SCHEMA, DESCRIPTION,
                    VIEW_SQL, VERSION, COLUMN_COUNT, ROW_COUNT, CREATED_BY, UPDATED_BY)
                OUTPUT INSERTED.VIEW_ID
                VALUES (:n, :db, :sch, :d, :sql, 1, :cc, :rc, :u, :u)
            """), {"n": vname, "db": GD_DATABASE, "sch": GD_SCHEMA, "d": description,
                   "sql": body, "cc": cols, "rc": rows, "u": user}).scalar())
        c.execute(text(f"""
            INSERT INTO {VIEW_VERSION_TABLE} (VIEW_ID, VERSION, ACTION, VIEW_SQL,
                COLUMN_COUNT, ROW_COUNT, CHANGED_BY)
            VALUES (:id, :v, :a, :sql, :cc, :rc, :u)
        """), {"id": vid, "v": ver, "a": action, "sql": body, "cc": cols, "rc": rows, "u": user})


def create_view(name: Any, sql: Any, description: Optional[str], user: str) -> Dict[str, Any]:
    """Validate, then CREATE OR REPLACE the view, then record the version.
    Nothing is created unless every validation check passes."""
    vname = normalize_view_name(name)
    body = clean_select(sql)
    validation = validate_sql(body, with_count=True)
    if not validation["ok"]:
        return {"created": False, "name": vname, "validation": validation}

    desc = (description or "").strip() or None
    comment = (desc or "Created in ARS Get Data")[:250].replace("\\", "\\\\").replace("'", "''")
    conn = connect()
    try:
        cur = conn.cursor()
        try:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{GD_DATABASE}"."{GD_SCHEMA}" '
                        f"COMMENT = 'Views created from the ARS Get Data module'")
            cur.execute(f'CREATE OR REPLACE VIEW "{GD_DATABASE}"."{GD_SCHEMA}"."{vname}" '
                        f"COMMENT = '{comment}' AS\n{body}")
        except Exception as e:
            raise GetDataError(f"Snowflake refused the view: {sf_message(e)}")
    finally:
        conn.close()
    _record_view(vname, body, desc, len(validation["columns"]), validation.get("row_count"), user)
    logger.info(f"[get-data] view {_fq_view(vname)} saved by {user} "
                f"({len(validation['columns'])} cols, {validation.get('row_count')} rows)")
    return {"created": True, "name": vname, "object": _fq_view(vname), "validation": validation}


def drop_view(name: Any, user: str) -> Dict[str, Any]:
    vname = normalize_view_name(name)
    used = _jobs_by_source().get(_fq_view(vname).upper(), [])
    if used:
        raise GetDataError("Used by sync job(s): " + ", ".join(j["job_name"] for j in used)
                           + ". Delete or repoint those jobs first.")
    conn = connect()
    try:
        cur = conn.cursor()
        try:
            cur.execute(f'DROP VIEW IF EXISTS "{GD_DATABASE}"."{GD_SCHEMA}"."{vname}"')
        except Exception as e:
            raise GetDataError(sf_message(e))
    finally:
        conn.close()
    ensure_tables()
    with get_data_engine().begin() as c:
        row = c.execute(text(f"""
            SELECT VIEW_ID, VERSION FROM {VIEW_TABLE}
            WHERE SF_DATABASE = :db AND SF_SCHEMA = :sch AND VIEW_NAME = :n
        """), {"db": GD_DATABASE, "sch": GD_SCHEMA, "n": vname}).fetchone()
        if row:
            c.execute(text(f"""
                UPDATE {VIEW_TABLE} SET STATUS = 'dropped', UPDATED_BY = :u,
                    UPDATED_AT = SYSUTCDATETIME() WHERE VIEW_ID = :id
            """), {"u": user, "id": row[0]})
            c.execute(text(f"""
                INSERT INTO {VIEW_VERSION_TABLE} (VIEW_ID, VERSION, ACTION, CHANGED_BY)
                VALUES (:id, :v, 'dropped', :u)
            """), {"id": row[0], "v": row[1], "u": user})
    logger.info(f"[get-data] view {_fq_view(vname)} dropped by {user}")
    return {"dropped": True, "name": vname}


def status() -> Dict[str, Any]:
    cfg = sfc.get_config()
    return {"enabled": cfg.get("enabled"), "account": cfg.get("account"),
            "user": cfg.get("user"), "role": cfg.get("role"), "warehouse": cfg.get("warehouse"),
            "last_status": cfg.get("last_status"), "last_message": cfg.get("last_message"),
            "last_verified": cfg.get("last_verified"),
            "database": GD_DATABASE, "schema": GD_SCHEMA, "view_prefix": VIEW_PREFIX}
