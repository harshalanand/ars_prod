r"""
Data Export Service — export stored-procedure results to a session folder.

Given a session code and a list of stored procedures, this service:
  1. Creates a `data` folder inside the desired base folder (default:
     backend/exports).
  2. Creates a sub-folder named after the session code:
         <base_dir>/data/<session_code>/
  3. Executes each stored procedure against the Data DB (Rep_data) and
     writes every result set it returns to a CSV file inside that folder:
         <proc_name>.csv            (single result set)
         <proc_name>_rs2.csv, ...   (additional result sets)

Usage:
    from app.services.data_export_service import export_session_data

    result = export_session_data(
        session_code="20260709_141530_123",
        procedures=[
            "dbo.sp_AutoContCompute",                               # no params
            {"name": "dbo.usp_ars_allocate_majcat",                 # with params
             "params": {"WERKS": "V201", "MAJ_CAT": "SHIRTS"}},
        ],
        base_dir=r"D:\exports",       # optional — the "desired folder"
    )
"""
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

import pandas as pd
from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine

# Default base folder mirrors export_job_service: backend/exports
DEFAULT_BASE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "exports"
)

# Stored-proc identifiers: optional schema prefix, then a normal SQL identifier.
_PROC_NAME_RE = re.compile(r"^(?:\[?[A-Za-z0-9_]+\]?\.)?\[?[A-Za-z0-9_]+\]?$")
_PARAM_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _safe_folder_name(value: str, max_length: int = 80) -> str:
    """Make a session code safe to use as a folder name."""
    cleaned = re.sub(r'[<>:"/\\|?*]', "-", str(value).strip())
    cleaned = "_".join(cleaned.split()) or "UNNAMED"
    return cleaned[:max_length]


def _proc_base_name(proc_name: str) -> str:
    """dbo.usp_ars_allocate_majcat -> usp_ars_allocate_majcat"""
    return proc_name.split(".")[-1].strip("[]")


def _normalize_procedures(
    procedures: List[Union[str, Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """Accept 'dbo.proc' strings or {'name':..., 'params': {...}} dicts."""
    normalized = []
    for entry in procedures:
        if isinstance(entry, str):
            entry = {"name": entry, "params": {}}
        name = str(entry.get("name", "")).strip()
        params = entry.get("params") or {}
        if not _PROC_NAME_RE.match(name):
            raise ValueError(f"Invalid stored procedure name: {name!r}")
        for pname in params:
            if not _PARAM_NAME_RE.match(str(pname)):
                raise ValueError(f"Invalid parameter name: {pname!r} for {name}")
        normalized.append({"name": name, "params": params})
    return normalized


def _run_sql_all_resultsets(
    sql: str, values=None, cancel_token=None
) -> List[pd.DataFrame]:
    """Execute arbitrary SQL and collect EVERY result set it returns.

    Uses a raw DBAPI connection + pyodbc nextset() so it works for multi-
    statement batches (DECLARE / SET / assignment-SELECT / EXEC sp_executesql)
    where the real SELECT is not the first thing the batch does. Result sets
    with no columns (DML counts, variable assignments) are skipped. When a
    cancel_token is given, its cursor is bound so a cancel interrupts the query.
    """
    engine = get_data_engine()
    raw = engine.raw_connection()
    frames: List[pd.DataFrame] = []
    try:
        cursor = raw.cursor()
        if cancel_token is not None:
            cancel_token.bind_cursor(cursor)
        try:
            if values:
                cursor.execute(sql, values)
            else:
                cursor.execute(sql)
            while True:
                if cursor.description:  # None for DML / assignment statements
                    columns = [col[0] for col in cursor.description]
                    rows = cursor.fetchall()
                    frames.append(
                        pd.DataFrame([tuple(r) for r in rows], columns=columns)
                    )
                if not cursor.nextset():
                    break
        finally:
            if cancel_token is not None:
                cancel_token.unbind_cursor()
            cursor.close()
        raw.commit()
    finally:
        raw.close()
    return frames


def _run_proc_all_resultsets(
    proc_name: str, params: Dict[str, Any], cancel_token=None
) -> List[pd.DataFrame]:
    """EXEC a stored procedure and collect every result set it returns."""
    if params:
        placeholders = ", ".join(f"@{p} = ?" for p in params)
        sql = f"EXEC {proc_name} {placeholders}"
        values = list(params.values())
    else:
        sql = f"EXEC {proc_name}"
        values = None
    return _run_sql_all_resultsets(sql, values, cancel_token)


def make_session_dir(session_code: str, base_dir: Optional[str] = None,
                     per_run: bool = True) -> str:
    """Return the export folder, creating it (and its parents) if missing.

    Always nests under a date folder (YYYYMMDD, today's run date):
      per_run=True  -> <base_dir>/data/<YYYYMMDD>/<session_code>/  (fresh each run)
      per_run=False -> <base_dir>/data/<YYYYMMDD>/                 (one folder per
                       day, reused; files land straight in it)
    """
    date_str = datetime.now().strftime("%Y%m%d")
    date_dir = os.path.join(base_dir or DEFAULT_BASE_DIR, "data", date_str)
    if per_run:
        if not session_code or not str(session_code).strip():
            raise ValueError("session_code is required")
        export_dir = os.path.join(date_dir, _safe_folder_name(session_code))
    else:
        export_dir = date_dir
    os.makedirs(export_dir, exist_ok=True)
    return export_dir


# Split defaults mirror the existing Export page (export_job_service).
_PRODUCT_HIERARCHY = ["SEG", "DIV", "SUB_DIV", "MAJ_CAT"]
_STORE_HIERARCHY = ["ZONE", "REG", "STORE"]


def _resolve_split_columns(method: str, product_hier, store_hier,
                           df_cols: List[str]) -> List[str]:
    """Which columns to group by, keeping only those present in the data."""
    if str(method).lower() == "store":
        preferred = store_hier or _STORE_HIERARCHY
    else:
        preferred = product_hier or _PRODUCT_HIERARCHY
    available = set(df_cols)
    return [c for c in preferred if c in available]


def _write_frame(df, export_dir: str, base_name: str, file_format: str,
                 procedure: str) -> Dict[str, Any]:
    file_path = os.path.join(export_dir, f"{base_name}.{file_format}")
    if file_format == "xlsx":
        df.to_excel(file_path, index=False)
    else:
        df.to_csv(file_path, index=False)
    logger.info(f"[data_export] {procedure} -> {file_path} ({len(df)} rows)")
    return {"procedure": procedure, "file": file_path, "rows": len(df)}


def write_frame_split(df, export_dir: str, base_name: str, file_format: str,
                      procedure: str,
                      split_config: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Write a DataFrame to one or many files per split_config.

    split_config = {enabled, method('product'|'store'), max_rows,
                    product_hierarchy[], store_hierarchy[]}. Same behavior as the
    Export page: splitting only kicks in when the data EXCEEDS max_rows. At or
    under the limit it's always a single file. Above it, group by the hierarchy
    columns (one file per group), and add _partN when a group still exceeds
    max_rows. Method 'none' (or disabled) splits by size only.
    """
    sc = split_config or {}
    enabled = bool(sc.get("enabled"))
    method = str(sc.get("method", "product")).lower()
    try:
        max_rows = int(sc.get("max_rows") or 1000000)
    except (TypeError, ValueError):
        max_rows = 1000000
    max_rows = max(1, max_rows)

    written: List[Dict[str, Any]] = []

    def size_split(frame, label_base):
        rows = len(frame)
        chunks = max(1, (rows + max_rows - 1) // max_rows)
        frame = frame.reset_index(drop=True)
        for ci in range(chunks):
            part = frame.iloc[ci * max_rows: min((ci + 1) * max_rows, rows)]
            name = label_base if chunks == 1 else f"{label_base}_part{ci + 1}"
            written.append(_write_frame(part, export_dir,
                                        _safe_folder_name(name, 180),
                                        file_format, procedure))

    # Only split when it's enabled AND the data actually exceeds max_rows.
    # Otherwise (disabled, or fits within the limit) write a single file.
    if not enabled or len(df) <= max_rows or method not in ("product", "store"):
        size_split(df, base_name)
        return written

    hierarchy = (sc.get("product_hierarchy") if method == "product"
                 else sc.get("store_hierarchy")) or (
                 _PRODUCT_HIERARCHY if method == "product" else _STORE_HIERARCHY)

    # Progressive (adaptive) hierarchy split: use the COARSEST level that keeps
    # each file within max_rows. The whole frame is already over the limit here,
    # so split by the first level; any group STILL over the limit is split by
    # the next level down, and so on. A group that fits becomes one file at that
    # level. If the deepest level is still too big, size-chunk it into _partN.
    def _rec(frame, levels, label):
        if len(frame) <= max_rows or not levels:
            size_split(frame, f"{base_name}_{label}" if label else base_name)
            return
        col = levels[0]
        if col not in frame.columns:
            _rec(frame, levels[1:], label)          # level absent — try the next
            return
        for val, grp in frame.groupby(col, dropna=False, sort=False):
            piece = f"{col}-{_safe_folder_name(str(val), 40)}"
            _rec(grp, levels[1:], f"{label}_{piece}" if label else piece)

    _rec(df, list(hierarchy), "")
    return written


def fanout_param_runs(
    proc_name: str, params: Dict[str, Any]
) -> List[tuple]:
    """Expand a proc's params for FANOUT parameters.

    If the proc has a parameter registered with FANOUT=1 in ARS_PROC_PARAM_VALUES
    and its value is a comma list, return one (params, suffix) run per value so
    the caller writes a SEPARATE output file per value (suffixed with the value).
    Otherwise returns a single [(params, None)]. Only the first matching fan-out
    param is expanded (no cross-product). Registry is optional — any error or a
    single value falls back to one run.
    """
    params = dict(params or {})
    pname = str(proc_name or "").strip().strip("[]").split(".")[-1].strip("[]")
    try:
        eng = get_data_engine()
        with eng.connect() as conn:
            if not conn.execute(text("SELECT OBJECT_ID('dbo.ARS_PROC_PARAM_VALUES')")).scalar():
                return [(params, None)]
            if not conn.execute(text("SELECT COL_LENGTH('dbo.ARS_PROC_PARAM_VALUES','FANOUT')")).scalar():
                return [(params, None)]
            fo = [r[0] for r in conn.execute(text("""
                SELECT DISTINCT PARAM_NAME FROM dbo.ARS_PROC_PARAM_VALUES
                WHERE LOWER(PROC_NAME) = LOWER(:p) AND ISNULL(FANOUT, 0) = 1
            """), {"p": pname})]
    except Exception:
        return [(params, None)]

    for fp in fo:
        key = next((k for k in params if k.lower() == str(fp).lower()), None)
        if not key or params.get(key) in (None, ""):
            continue
        vals = [v.strip() for v in str(params[key]).split(",") if v.strip()]
        if len(vals) <= 1:
            continue
        runs = []
        for v in vals:
            p2 = dict(params)
            p2[key] = v
            runs.append((p2, v))
        return runs
    return [(params, None)]


def run_procedure_to_files(
    proc: Union[str, Dict[str, Any]],
    export_dir: str,
    file_format: str = "csv",
    split_config: Optional[Dict[str, Any]] = None,
    cancel_token=None,
    output_name: Optional[str] = None,
    name_suffix: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Run one stored procedure and write every result set into export_dir.

    output_name overrides the file base name (defaults to the procedure name);
    name_suffix (e.g. the triggering session id) is appended to it.
    If split_config is given, each result set is split into multiple files per
    that config (same logic as the Export page). Returns a list of file dicts
    ({"procedure", "file", "rows"}). Raises if the proc name/params are invalid
    or the proc returns no result set. A cancel_token lets a caller kill the
    running query.
    """
    if file_format not in ("csv", "xlsx"):
        raise ValueError(f"Unsupported file_format: {file_format!r}")
    normalized = _normalize_procedures([proc])[0]
    name, params = normalized["name"], normalized["params"]

    frames = _run_proc_all_resultsets(name, params, cancel_token=cancel_token)
    if not frames:
        raise RuntimeError(f"{name} returned no result set")

    base = _safe_folder_name(output_name, 120) if output_name else _proc_base_name(name)
    if name_suffix:
        base = f"{base}_{_safe_folder_name(str(name_suffix), 80)}"
    written: List[Dict[str, Any]] = []
    for i, df in enumerate(frames, start=1):
        rs_suffix = "" if len(frames) == 1 else f"_rs{i}"
        written.extend(write_frame_split(
            df, export_dir, f"{base}{rs_suffix}", file_format, name, split_config))
    return written


# A report "query" step runs read-only SQL. Block data-modifying / DDL keywords
# so a pasted query can never mutate the database by accident. EXEC is allowed
# ONLY for sp_executesql (the read-only dynamic-SQL pattern many report scripts
# use); any other EXEC belongs in a stored-procedure step.
_DESTRUCTIVE_RE = re.compile(
    r"\b(INSERT\s+INTO|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|MERGE\s+INTO|"
    r"GRANT|REVOKE|BACKUP|RESTORE)\b", re.IGNORECASE)
_BAD_EXEC_RE = re.compile(r"\bEXEC(UTE)?\b\s+(?!sp_executesql)", re.IGNORECASE)


def run_query_to_files(
    sql: str,
    base_name: str,
    export_dir: str,
    file_format: str = "csv",
    split_config: Optional[Dict[str, Any]] = None,
    cancel_token=None,
    name_suffix: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Run a read-only SQL query and write its result into export_dir.

    Only SELECT-style queries are allowed — any data-modifying / DDL keyword
    raises. Honors split_config exactly like a stored-procedure step. A raw
    cursor is used (not pd.read_sql) so a cancel_token can interrupt it.
    """
    if file_format not in ("csv", "xlsx"):
        raise ValueError(f"Unsupported file_format: {file_format!r}")
    if not sql or not str(sql).strip():
        raise ValueError("query step has empty SQL")
    if _DESTRUCTIVE_RE.search(sql):
        raise ValueError(
            "query step must be read-only — remove data-modifying/DDL keywords "
            "(INSERT INTO / UPDATE / DELETE / DROP / ALTER / CREATE / MERGE INTO). "
            "Use a stored-procedure step for anything that writes.")
    if _BAD_EXEC_RE.search(sql):
        raise ValueError(
            "query step may only EXEC sp_executesql (read-only dynamic SQL). "
            "To call another stored procedure, use a stored-procedure step.")

    # Walk every result set — multi-statement batches (DECLARE / SET /
    # SELECT-assignment / EXEC sp_executesql) return their SELECT after the
    # non-result statements, which a single fetch would miss.
    frames = _run_sql_all_resultsets(sql, None, cancel_token)
    if not frames:
        raise RuntimeError("query returned no result set (nothing to export)")

    label = _safe_folder_name(base_name or "query", 120) or "query"
    if name_suffix:
        label = f"{label}_{_safe_folder_name(str(name_suffix), 80)}"

    written: List[Dict[str, Any]] = []
    for i, df in enumerate(frames, start=1):
        rs_suffix = "" if len(frames) == 1 else f"_rs{i}"
        written.extend(write_frame_split(
            df, export_dir, f"{label}{rs_suffix}", file_format,
            base_name or "query", split_config))
    return written


def export_session_data(
    session_code: str,
    procedures: List[Union[str, Dict[str, Any]]],
    base_dir: Optional[str] = None,
    file_format: str = "csv",
) -> Dict[str, Any]:
    """Export stored-procedure results into <base_dir>/data/<session_code>/.

    Args:
        session_code: session id used as the folder name
                      (e.g. from listing_sessions.make_session_id()).
        procedures:   list of proc names or {"name", "params"} dicts.
        base_dir:     desired folder; defaults to backend/exports.
        file_format:  "csv" (default) or "xlsx".

    Returns a manifest dict:
        {"session_code", "export_dir", "files": [...], "errors": [...]}
    """
    procs = _normalize_procedures(procedures)
    export_dir = make_session_dir(session_code, base_dir)

    manifest: Dict[str, Any] = {
        "session_code": session_code,
        "export_dir": export_dir,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "files": [],
        "errors": [],
    }

    for proc in procs:
        try:
            manifest["files"].extend(
                run_procedure_to_files(proc, export_dir, file_format)
            )
        except Exception as e:
            logger.error(f"[data_export] {proc} failed: {e}")
            manifest["errors"].append(
                {"procedure": proc if isinstance(proc, str) else proc.get("name"),
                 "error": str(e)}
            )

    manifest["completed_at"] = datetime.now().isoformat(timespec="seconds")
    return manifest
