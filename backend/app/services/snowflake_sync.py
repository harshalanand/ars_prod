"""
Snowflake incremental sync for the report engine.

Given a report whose OUTPUT_TYPE includes 'snowflake', this pushes the files
produced by the report's SQL steps into Snowflake tables incrementally:

    filter rows newer than the saved watermark
      -> write_pandas() into a transient stage table
      -> MERGE INTO the target on key columns (upsert)
      -> save the new watermark

Watermarks are stored per (report_id, target_table) in ARS_SF_WATERMARKS in the
Data DB, so the next run resumes exactly where this one ended.

SNOWFLAKE_CONFIG (JSON on ARS_REPORTS) shape:
    {
      "database": "ANALYTICS", "schema": "ARS", "warehouse": "WH",
      "targets": [
        {"source_file": "usp_ars_allocate_majcat",   # proc base name -> its CSV
         "table": "ALLOCATION",
         "key_cols": ["SESSION_ID","RDC","ARTICLE_NUMBER"],
         "watermark_col": "MODIFIED_AT"}              # omit for append-only
      ]
    }

Connection credentials come from settings (SF_ACCOUNT/SF_USER/SF_PASSWORD),
falling back to the account in the project handover. This module is import-safe
even when snowflake-connector-python is not installed — it raises a clear error
only when a sync is actually attempted.
"""
import glob
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger
from sqlalchemy import text

from app.core.config import get_settings
from app.database.session import get_data_engine

settings = get_settings()

WATERMARK_TABLE = "ARS_SF_WATERMARKS"

# Audit columns stamped on APPEND loads so each run's batch is identifiable.
STAMP_SESSION = "_SESSION_CODE"
STAMP_LOADED = "_LOADED_AT"

# Rows read+loaded per batch, so a multi-million-row source never fully
# materialises in memory while syncing to Snowflake.
_SF_CHUNK = 100_000

# Per-target load retries — a transient TLS/network drop (SSL "wrong version
# number", connection reset, timeout) fails one target while others in the same
# run succeed. Retrying the whole target is safe: the stage is CREATE OR REPLACE'd
# at the top and the target table is only written by the final MERGE/INSERT after
# ALL chunks load, so a mid-load failure leaves the target untouched.
_SF_MAX_ATTEMPTS = 3


def _is_retryable_sf_error(e) -> bool:
    """True for transient connectivity/TLS errors worth a reconnect+retry
    (as opposed to schema/SQL errors, which will just fail again)."""
    s = str(e).lower()
    return any(k in s for k in (
        "ssl", "handshake", "wrong version number", "max retries exceeded",
        "connection aborted", "connection reset", "connection is closed",
        "timed out", "timeout", "econnreset", "eof occurred",
        "250003", "254007", "operationaltimeout", "read timed out"))


# Session codes are internally generated (e.g. 20260908_101150_878); validate
# before inlining into a DELETE so the scoped cleanup is injection-safe.
_SESSION_CODE_RE = re.compile(r"^[0-9A-Za-z_\-]{1,64}$")


def _delete_session_rows(cur, table: str, session_code: Optional[str]) -> int:
    """Append idempotency — delete rows a PRIOR run of THIS SAME session loaded into
    `table`, so a re-run replaces its own batch instead of duplicating it. Scoped to
    _SESSION_CODE only (never a full TRUNCATE, so other sessions' history survives).
    No-op when session_code is empty/invalid, the stamp column is absent, or nothing
    matches. Returns rows deleted."""
    if not session_code or not _SESSION_CODE_RE.match(str(session_code)):
        return 0
    try:
        cur.execute(f"DESC TABLE {table}")
        if STAMP_SESSION.upper() not in {str(r[0]).upper() for r in cur.fetchall()}:
            return 0  # table has no session stamp → nothing to scope-delete
    except Exception:
        return 0  # table absent (fresh create) → nothing to delete
    try:
        cur.execute(f"DELETE FROM {table} WHERE {STAMP_SESSION} = '{session_code}'")
        return int(cur.rowcount or 0)
    except Exception as e:
        logger.warning(f"[snowflake] {table}: session cleanup delete failed: {e}")
        return 0


def _ensure_table_columns(cur, table: str, cols: List[str]) -> None:
    """Schema evolution — ADD any DataFrame columns missing from an EXISTING
    target table. Auto-create only runs (CREATE TABLE IF NOT EXISTS) when the
    table is absent, so once the source SQL gains a column (e.g. a grid step
    adds ADHOC) the frozen table lacks it and the load fails with Snowflake
    'invalid identifier'. This self-heals that by widening the table."""
    try:
        cur.execute(f"DESC TABLE {table}")
        existing = {str(r[0]).upper() for r in cur.fetchall()}
    except Exception as e:
        logger.debug(f"[snowflake] DESC {table} failed (new table?): {e}")
        return
    for c in cols:
        if str(c).upper() not in existing:
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {c} VARCHAR")
                logger.info(f"[snowflake] {table}: added missing column {c}")
            except Exception as e:
                logger.warning(f"[snowflake] {table}: could not add column {c}: {e}")


def _parse_split(raw) -> Dict[str, Any]:
    """SPLIT_CONFIG (dict | JSON string | None) → dict. Used to know a report's
    hierarchy columns so all split files of a source are matched."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        import json
        return json.loads(raw) or {}
    except Exception:
        return {}


def _sf_col(name: str) -> str:
    """Snowflake-safe, unquoted (uppercase) column identifier from a CSV header."""
    s = re.sub(r"[^0-9A-Za-z_]", "_", str(name)).upper().strip("_") or "COL"
    if not (s[0].isalpha() or s[0] == "_"):
        s = "_" + s
    return s


def _resolve_location(cfg: Dict[str, Any]) -> tuple:
    """Per-report database/schema/warehouse, falling back to the shared
    Settings → Snowflake connection when a field is blank."""
    database = (cfg.get("database") or "").strip()
    schema = (cfg.get("schema") or "").strip()
    warehouse = (cfg.get("warehouse") or "").strip()
    if not (database and schema and warehouse):
        try:
            from app.services import snowflake_config_service as sfc
            sc = sfc.get_config()
            database = database or (sc.get("database") or "")
            schema = schema or (sc.get("schema") or "")
            warehouse = warehouse or (sc.get("warehouse") or "")
        except Exception as e:
            logger.debug(f"[snowflake] shared location fallback failed: {e}")
    return database, schema, warehouse


def ensure_watermark_table() -> None:
    engine = get_data_engine()
    with engine.begin() as conn:
        conn.execute(text(f"""
            IF OBJECT_ID('dbo.{WATERMARK_TABLE}','U') IS NULL
            CREATE TABLE dbo.{WATERMARK_TABLE} (
                REPORT_ID     INT           NOT NULL,
                TARGET_TABLE  NVARCHAR(200) NOT NULL,
                WATERMARK_VAL NVARCHAR(100) NULL,
                UPDATED_AT    DATETIME      NOT NULL DEFAULT SYSUTCDATETIME(),
                CONSTRAINT PK_{WATERMARK_TABLE} PRIMARY KEY (REPORT_ID, TARGET_TABLE)
            )
        """))


def _get_watermark(report_id: int, target: str) -> Optional[str]:
    engine = get_data_engine()
    with engine.connect() as conn:
        row = conn.execute(text(
            f"SELECT WATERMARK_VAL FROM {WATERMARK_TABLE} "
            f"WHERE REPORT_ID=:r AND TARGET_TABLE=:t"),
            {"r": report_id, "t": target}).fetchone()
    return row[0] if row else None


def _set_watermark(report_id: int, target: str, val: str) -> None:
    engine = get_data_engine()
    with engine.begin() as conn:
        conn.execute(text(f"""
            MERGE {WATERMARK_TABLE} AS tgt
            USING (SELECT :r AS REPORT_ID, :t AS TARGET_TABLE) AS src
            ON tgt.REPORT_ID=src.REPORT_ID AND tgt.TARGET_TABLE=src.TARGET_TABLE
            WHEN MATCHED THEN UPDATE SET WATERMARK_VAL=:v, UPDATED_AT=SYSUTCDATETIME()
            WHEN NOT MATCHED THEN INSERT (REPORT_ID, TARGET_TABLE, WATERMARK_VAL)
                 VALUES (:r, :t, :v);
        """), {"r": report_id, "t": target, "v": str(val)})


def _sf_credentials() -> Dict[str, Any]:
    """Resolve Snowflake connection creds, preferring the Settings → Snowflake
    UI (app_settings.json) and falling back to env-based settings.SF_*."""
    account = user = password = warehouse = role = ""
    try:
        from app.api.v1.endpoints.settings import load_app_settings
        sf = (load_app_settings() or {}).get("snowflake", {}) or {}
        account = sf.get("account") or ""
        user = sf.get("user") or ""
        password = sf.get("password") or ""
        warehouse = sf.get("warehouse") or ""
        role = sf.get("role") or ""
    except Exception as e:
        logger.debug(f"[snowflake] app_settings read failed: {e}")
    account = account or getattr(settings, "SF_ACCOUNT", "") or ""
    user = user or getattr(settings, "SF_USER", "") or ""
    password = password or getattr(settings, "SF_PASSWORD", "") or ""
    return {"account": account, "user": user, "password": password,
            "warehouse": warehouse, "role": role}


def _connect():
    """Open a Snowflake connection. Raises a clear error if unavailable.

    Prefers the single app-wide config (Settings → Snowflake via
    snowflake_config_service) — which supports key-pair auth and is shared with
    the SAP module — and falls back to the legacy app_settings.snowflake below.
    """
    try:
        from app.services import snowflake_config_service as sfc
        if sfc.is_enabled():
            return sfc.connect(require_enabled=True)
    except Exception as e:
        logger.warning(f"[snowflake] shared config connect failed, using legacy creds: {e}")

    try:
        import snowflake.connector  # noqa
    except ImportError as e:
        raise RuntimeError(
            "snowflake-connector-python is not installed on the server — "
            "run: pip install 'snowflake-connector-python[pandas]'"
        ) from e
    c = _sf_credentials()
    if not c["account"] or not c["user"] or not c["password"]:
        raise RuntimeError(
            "Snowflake not configured — set account, user, and password in "
            "Settings → Snowflake")
    kwargs = {"account": c["account"], "user": c["user"], "password": c["password"]}
    if c["warehouse"]:
        kwargs["warehouse"] = c["warehouse"]
    if c["role"]:
        kwargs["role"] = c["role"]
    return snowflake.connector.connect(**kwargs)


_HIER_TOKENS = ["SEG", "DIV", "SUB_DIV", "MAJ_CAT", "ZONE", "REG", "STORE"]


def _files_for_source(export_dir: str, source_file: str,
                      files: List[Dict[str, Any]],
                      split_config: Optional[Dict[str, Any]] = None) -> List[str]:
    """ALL CSVs belonging to a source — a split step produces many files
    (`<base>_SEG-APP_DIV-KIDS.csv`, `<base>_part2.csv`, …), so a single-file
    match would load the data INCOMPLETELY. Matches, case-insensitively:
        <base>.csv                         (single file)
        <base>_part<N>.csv                 (row-count split)
        <base>_<HIER>-<val>....csv         (hierarchy split)
    where <HIER> is a configured/known hierarchy column. This is precise enough
    NOT to grab a sibling whose name merely starts with <base> — e.g. source
    'ARS_GRID_MJ' does not swallow 'ARS_GRID_MJ_MERGE_RNG_SEG'."""
    sc = split_config or {}
    hiers = list(_HIER_TOKENS)
    for extra in (sc.get("product_hierarchy") or []) + (sc.get("store_hierarchy") or []):
        if extra and extra not in hiers:
            hiers.append(extra)
    hier_alt = "|".join(re.escape(h) for h in hiers)
    base = re.escape(source_file)
    pat = re.compile(
        rf"^{base}(\.csv|_part\d+\.csv|_(?:{hier_alt})-.*\.csv)$", re.IGNORECASE)

    seen, out = set(), []
    candidates = [f.get("file", "") for f in files]
    candidates += glob.glob(os.path.join(export_dir, f"{source_file}*.csv"))
    for path in candidates:
        if not path:
            continue
        name = os.path.basename(path)
        if pat.match(name) and path not in seen and os.path.exists(path):
            seen.add(path)
            out.append(path)
    return sorted(out)


def sync_report_to_snowflake(report: Dict[str, Any], export_dir: str,
                             files: List[Dict[str, Any]],
                             session_code: Optional[str] = None) -> Dict[str, Any]:
    """Push each configured target into Snowflake.

    Per target `mode`:
      - 'append' (default) → INSERT the file's rows (stamped with _SESSION_CODE /
        _LOADED_AT so each run's batch is identifiable).
      - 'upsert' → MERGE on key_cols (optionally incremental via watermark_col).
    The target table is auto-created (all-VARCHAR) if it doesn't exist.
    database/schema/warehouse fall back to the shared Settings → Snowflake conn.

    Returns {"files": [...uploaded summaries...], "errors": [...]}.
    """
    import json
    cfg_raw = report.get("SNOWFLAKE_CONFIG")
    if not cfg_raw:
        return {"files": [], "errors": [
            {"step": "snowflake", "error": "OUTPUT_TYPE includes snowflake but "
             "SNOWFLAKE_CONFIG is empty"}]}
    cfg = cfg_raw if isinstance(cfg_raw, dict) else json.loads(cfg_raw)
    targets = cfg.get("targets") or []
    if not targets:
        return {"files": [], "errors": [
            {"step": "snowflake", "error": "SNOWFLAKE_CONFIG has no targets"}]}

    database, schema, warehouse = _resolve_location(cfg)
    if not database or not schema:
        return {"files": [], "errors": [{"step": "snowflake", "error":
            "no database/schema — set them on the report or in Settings → Snowflake"}]}
    report_id = int(report["REPORT_ID"])

    ensure_watermark_table()

    from snowflake.connector.pandas_tools import write_pandas  # local import
    conn = _connect()
    uploaded: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    loaded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        cur = conn.cursor()
        if warehouse:
            cur.execute(f"USE WAREHOUSE {warehouse}")
        cur.execute(f"USE DATABASE {database}")
        cur.execute(f"USE SCHEMA {schema}")

        def _reuse_location():
            """Re-issue USE statements after a reconnect."""
            if warehouse:
                cur.execute(f"USE WAREHOUSE {warehouse}")
            cur.execute(f"USE DATABASE {database}")
            cur.execute(f"USE SCHEMA {schema}")

        split_cfg = _parse_split(report.get("SPLIT_CONFIG"))
        for tgt in targets:
            table = tgt["table"]
            key_cols = [_sf_col(c) for c in (tgt.get("key_cols") or [])]
            wm_col = tgt.get("watermark_col")
            wm_sf = _sf_col(wm_col) if wm_col else None
            source = tgt.get("source_file")
            # mode: explicit, else inferred (key_cols → upsert, none → append)
            mode = str(tgt.get("mode") or ("upsert" if key_cols else "append")).lower()
            attempt = 0
            while True:
                attempt += 1
                try:
                    # ALL files for this source (a split step produces many).
                    src_paths = _files_for_source(export_dir, source, files, split_cfg)
                    if not src_paths:
                        raise FileNotFoundError(f"no exported file for source '{source}'")

                    # Column set from the first file's header (+ stamps for append).
                    header = list(pd.read_csv(src_paths[0], nrows=0, dtype=str).columns)
                    cols = [_sf_col(c) for c in header]
                    if mode == "append":
                        cols += [STAMP_SESSION, STAMP_LOADED]

                    cols_ddl = ", ".join(f"{c} VARCHAR" for c in cols)
                    cur.execute(f"CREATE TABLE IF NOT EXISTS {table} ({cols_ddl})")
                    # Widen an existing table for any new source columns (schema drift).
                    _ensure_table_columns(cur, table, cols)
                    stage = f"STG_{table}_{report_id}"
                    cur.execute(f"CREATE OR REPLACE TEMPORARY TABLE {stage} LIKE {table}")

                    wm_before = _get_watermark(report_id, table) if (mode == "upsert" and wm_sf) else None
                    new_wm = wm_before
                    total = 0
                    # Stream each file in CHUNKS → stage, so a multi-million-row source
                    # never materialises in memory (the OOM the old whole-file read hit).
                    for path in src_paths:
                        for chunk in pd.read_csv(path, dtype=str, keep_default_na=False,
                                                 chunksize=_SF_CHUNK):
                            chunk.columns = [_sf_col(c) for c in chunk.columns]
                            if mode == "upsert" and wm_sf and wm_before is not None and wm_sf in chunk.columns:
                                chunk = chunk[chunk[wm_sf].astype(str) > str(wm_before)]
                            if chunk.empty:
                                continue
                            if wm_sf and wm_sf in chunk.columns:
                                cmax = str(chunk[wm_sf].astype(str).max())
                                new_wm = cmax if (new_wm is None or cmax > new_wm) else new_wm
                            if mode == "append":
                                chunk[STAMP_SESSION] = session_code or ""
                                chunk[STAMP_LOADED] = loaded_at
                            write_pandas(conn, chunk, stage, quote_identifiers=False)
                            total += len(chunk)

                    if total == 0:
                        uploaded.append({"target": table, "rows": 0, "mode": mode,
                                         "note": "no new rows", "files": len(src_paths)})
                        break

                    if mode == "upsert" and key_cols:
                        on = " AND ".join(f"t.{c}=s.{c}" for c in key_cols)
                        set_clause = ", ".join(f"t.{c}=s.{c}" for c in cols if c not in key_cols)
                        insert_cols = ", ".join(cols)
                        insert_vals = ", ".join(f"s.{c}" for c in cols)
                        cur.execute(f"""
                            MERGE INTO {table} t USING {stage} s ON {on}
                            WHEN MATCHED THEN UPDATE SET {set_clause}
                            WHEN NOT MATCHED THEN
                                INSERT ({insert_cols}) VALUES ({insert_vals})
                        """)
                        if wm_sf and new_wm is not None:
                            _set_watermark(report_id, table, new_wm)
                        note = f"upserted (merged on {key_cols})"
                    else:
                        # Idempotent per session: if THIS session already loaded
                        # rows into the target (e.g. a retry of a partial run), delete
                        # that session's batch first so the re-run REPLACES it instead
                        # of duplicating. Scoped by _SESSION_CODE — other sessions'
                        # history is untouched (this is NOT a full TRUNCATE).
                        removed = _delete_session_rows(cur, table, session_code)
                        if removed:
                            logger.info(f"[snowflake] {table}: removed {removed} prior row(s) "
                                        f"for session {session_code} before re-append")
                        cur.execute(f"INSERT INTO {table} SELECT * FROM {stage}")
                        note = "re-appended (replaced session)" if removed else "appended"

                    uploaded.append({"target": table, "rows": total, "mode": mode,
                                     "note": note, "files": len(src_paths)})
                    logger.info(f"[snowflake] {table}: {note} {total} rows from "
                                f"{len(src_paths)} file(s) ({database}.{schema})")
                    break
                except Exception as e:
                    if attempt < _SF_MAX_ATTEMPTS and _is_retryable_sf_error(e):
                        logger.warning(f"[snowflake] {table}: transient error on attempt "
                                       f"{attempt}/{_SF_MAX_ATTEMPTS} ({e}); reconnecting")
                        try:
                            conn.close()
                        except Exception:
                            pass
                        time.sleep(2 * attempt)
                        conn = _connect()
                        cur = conn.cursor()
                        _reuse_location()
                        continue
                    logger.error(f"[snowflake] target {tgt.get('table')} failed: {e}")
                    errors.append({"target": tgt.get("table"), "error": str(e)})
                    break
    finally:
        conn.close()

    return {"files": uploaded, "errors": errors}


# ── Direct mode: SQL Server → Snowflake with NO intermediate CSV ─────────────
def _proc_sql(name: str, params: Dict[str, Any]):
    """Build an EXEC statement (+ bound values) for a stored-procedure step."""
    if params:
        placeholders = ", ".join(f"@{p} = ?" for p in params)
        return f"EXEC {name} {placeholders}", list(params.values())
    return f"EXEC {name}", None


def _direct_sql_for_source(source: str, steps: List[Dict[str, Any]]):
    """Map a Snowflake target's source_file to the (sql, values) that produce it,
    mirroring file-mode output naming — including FANOUT (e.g. usp_ars_msa_master
    @Level → MSA_OP_CL_DETAIL / _GEN_CLR / _MAJ_CAT). Returns (sql, values) | None."""
    from app.services.data_export_service import (
        _safe_folder_name, _proc_base_name, fanout_param_runs)
    want = _safe_folder_name(source).lower()
    for s in steps:
        typ = (s.get("type") or "sql").lower()
        name = s.get("name") or ""
        label = s.get("label")
        params = s.get("params") or {}
        if typ == "query":
            if _safe_folder_name(name).lower() == want:
                return (params.get("sql", ""), None)
        elif typ == "sql":
            base0 = _safe_folder_name(label) if label else _proc_base_name(name)
            for run_params, fo in fanout_param_runs(name, params):
                full = _safe_folder_name(f"{base0}_{fo}") if fo else _safe_folder_name(base0)
                if full.lower() == want:
                    return _proc_sql(name, run_params)
    return None


def _stream_sqlserver_to_target(sf_conn, cur_sf, write_pandas, sql, values,
                                table, mode, key_cols, wm_sf, report_id,
                                session_code, loaded_at) -> int:
    """Run a SQL Server query/proc and stream its rows straight into Snowflake in
    chunks (no CSV). Auto-creates the table, batch-stamps on append, honours
    watermark on upsert. Returns rows loaded."""
    raw = get_data_engine().raw_connection()
    total, cols, stage = 0, None, None
    wm_before = _get_watermark(report_id, table) if (mode == "upsert" and wm_sf) else None
    new_wm = wm_before
    try:
        src = raw.cursor()
        if values:
            src.execute(sql, values)
        else:
            src.execute(sql)
        while src.description is None:   # skip non-result statements
            if not src.nextset():
                break
        if src.description is None:
            return 0
        columns = [_sf_col(c[0]) for c in src.description]
        while True:
            rows = src.fetchmany(_SF_CHUNK)
            if not rows:
                break
            # All-string rows (None→''), matching the all-VARCHAR target.
            df = pd.DataFrame(
                [tuple("" if v is None else str(v) for v in r) for r in rows],
                columns=columns)
            if mode == "upsert" and wm_sf and wm_before is not None and wm_sf in df.columns:
                df = df[df[wm_sf] > str(wm_before)]
            if df.empty:
                continue
            if wm_sf and wm_sf in df.columns:
                cmax = str(df[wm_sf].max())
                new_wm = cmax if (new_wm is None or cmax > new_wm) else new_wm
            if mode == "append":
                df[STAMP_SESSION] = session_code or ""
                df[STAMP_LOADED] = loaded_at
            if cols is None:
                cols = list(df.columns)
                cur_sf.execute(f"CREATE TABLE IF NOT EXISTS {table} "
                               f"({', '.join(f'{c} VARCHAR' for c in cols)})")
                # Widen an existing table for any new source columns (schema drift).
                _ensure_table_columns(cur_sf, table, cols)
                stage = f"STG_{table}_{report_id}"
                cur_sf.execute(f"CREATE OR REPLACE TEMPORARY TABLE {stage} LIKE {table}")
            write_pandas(sf_conn, df, stage, quote_identifiers=False)
            total += len(df)
        src.close()
        raw.commit()
    finally:
        raw.close()

    if total == 0:
        return 0
    if mode == "upsert" and key_cols:
        on = " AND ".join(f"t.{c}=s.{c}" for c in key_cols)
        set_clause = ", ".join(f"t.{c}=s.{c}" for c in cols if c not in key_cols)
        insert_cols = ", ".join(cols)
        insert_vals = ", ".join(f"s.{c}" for c in cols)
        cur_sf.execute(f"""
            MERGE INTO {table} t USING {stage} s ON {on}
            WHEN MATCHED THEN UPDATE SET {set_clause}
            WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})
        """)
        if wm_sf and new_wm is not None:
            _set_watermark(report_id, table, new_wm)
    else:
        # Idempotent per session (see file-mode note): replace this session's own
        # prior batch on a re-run instead of duplicating it.
        removed = _delete_session_rows(cur_sf, table, session_code)
        if removed:
            logger.info(f"[snowflake] {table}: removed {removed} prior row(s) for "
                        f"session {session_code} before re-append (direct)")
        cur_sf.execute(f"INSERT INTO {table} SELECT * FROM {stage}")
    return total


def sync_report_direct_to_snowflake(report: Dict[str, Any],
                                    session_code: Optional[str] = None) -> Dict[str, Any]:
    """DIRECT mode — run each target's source step against SQL Server and stream
    the rows straight into Snowflake, with NO intermediate CSV export. Used for
    snowflake-only reports. Returns {"files": [...], "errors": [...]}."""
    import json
    cfg_raw = report.get("SNOWFLAKE_CONFIG")
    cfg = (cfg_raw if isinstance(cfg_raw, dict) else json.loads(cfg_raw)) if cfg_raw else {}
    targets = cfg.get("targets") or []
    if not targets:
        return {"files": [], "errors": [{"step": "snowflake", "error": "no targets"}]}
    database, schema, warehouse = _resolve_location(cfg)
    if not database or not schema:
        return {"files": [], "errors": [{"step": "snowflake", "error":
            "no database/schema — set them on the report or in Settings → Snowflake"}]}
    report_id = int(report["REPORT_ID"])
    raw_steps = report.get("STEPS")
    if isinstance(raw_steps, str):
        try:
            steps = json.loads(raw_steps or "[]")
        except Exception:
            steps = []
    else:
        steps = raw_steps or []
    if not isinstance(steps, list):
        steps = []

    ensure_watermark_table()
    from snowflake.connector.pandas_tools import write_pandas
    conn = _connect()
    uploaded, errors = [], []
    loaded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        cur = conn.cursor()

        def _reuse_location():
            if warehouse:
                cur.execute(f"USE WAREHOUSE {warehouse}")
            cur.execute(f"USE DATABASE {database}")
            cur.execute(f"USE SCHEMA {schema}")

        _reuse_location()
        for tgt in targets:
            table = tgt["table"]
            key_cols = [_sf_col(c) for c in (tgt.get("key_cols") or [])]
            wm_sf = _sf_col(tgt["watermark_col"]) if tgt.get("watermark_col") else None
            source = tgt.get("source_file")
            mode = str(tgt.get("mode") or ("upsert" if key_cols else "append")).lower()
            attempt = 0
            while True:
                attempt += 1
                try:
                    pair = _direct_sql_for_source(source, steps)
                    if not pair or not pair[0]:
                        raise ValueError(f"no report step produces source '{source}'")
                    sql, values = pair
                    # Restartable: streams from a fresh SQL Server cursor and
                    # CREATE OR REPLACE's the stage; the target is only written by
                    # the final MERGE/INSERT, so a retry re-runs cleanly.
                    total = _stream_sqlserver_to_target(
                        conn, cur, write_pandas, sql, values, table, mode, key_cols,
                        wm_sf, report_id, session_code, loaded_at)
                    note = ("upserted" if (mode == "upsert" and key_cols) else "appended") + " (direct)"
                    uploaded.append({"target": table, "rows": total, "mode": mode,
                                     "note": note, "direct": True})
                    logger.info(f"[snowflake] {table}: {note} {total} rows direct "
                                f"({database}.{schema})")
                    break
                except Exception as e:
                    if attempt < _SF_MAX_ATTEMPTS and _is_retryable_sf_error(e):
                        logger.warning(f"[snowflake-direct] {table}: transient error on attempt "
                                       f"{attempt}/{_SF_MAX_ATTEMPTS} ({e}); reconnecting")
                        try:
                            conn.close()
                        except Exception:
                            pass
                        time.sleep(2 * attempt)
                        conn = _connect()
                        cur = conn.cursor()
                        _reuse_location()
                        continue
                    logger.error(f"[snowflake-direct] target {tgt.get('table')} failed: {e}")
                    errors.append({"target": tgt.get("table"), "error": str(e)})
                    break
    finally:
        conn.close()
    return {"files": uploaded, "errors": errors}
