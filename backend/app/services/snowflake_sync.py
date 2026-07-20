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
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger
from sqlalchemy import text

from app.core.config import get_settings
from app.database.session import get_data_engine

settings = get_settings()

WATERMARK_TABLE = "ARS_SF_WATERMARKS"


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
    """Open a Snowflake connection. Raises a clear error if unavailable."""
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


def _file_for_source(export_dir: str, source_file: str,
                     files: List[Dict[str, Any]]) -> Optional[str]:
    """Locate the CSV a target should load from (proc base name)."""
    for f in files:
        path = f.get("file", "")
        if os.path.splitext(os.path.basename(path))[0].lower() == source_file.lower():
            return path
    # Fallback: glob the export dir
    matches = glob.glob(os.path.join(export_dir, f"{source_file}*.csv"))
    return matches[0] if matches else None


def sync_report_to_snowflake(report: Dict[str, Any], export_dir: str,
                             files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Push each configured target into Snowflake incrementally.

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

    database = cfg.get("database")
    schema = cfg.get("schema")
    warehouse = cfg.get("warehouse")
    report_id = int(report["REPORT_ID"])

    ensure_watermark_table()

    from snowflake.connector.pandas_tools import write_pandas  # local import
    conn = _connect()
    uploaded: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    try:
        cur = conn.cursor()
        if warehouse:
            cur.execute(f"USE WAREHOUSE {warehouse}")
        cur.execute(f"USE DATABASE {database}")
        cur.execute(f"USE SCHEMA {schema}")

        for tgt in targets:
            table = tgt["table"]
            key_cols = tgt.get("key_cols") or []
            wm_col = tgt.get("watermark_col")
            source = tgt.get("source_file")
            try:
                src_path = _file_for_source(export_dir, source, files)
                if not src_path or not os.path.exists(src_path):
                    raise FileNotFoundError(
                        f"no exported file for source '{source}'")
                df = pd.read_csv(src_path)

                # Incremental filter on the watermark column.
                wm_before = _get_watermark(report_id, table) if wm_col else None
                if wm_col and wm_before is not None and wm_col in df.columns:
                    df = df[df[wm_col].astype(str) > str(wm_before)]

                if df.empty:
                    uploaded.append({"target": table, "rows": 0,
                                     "note": "no new rows since watermark"})
                    continue

                stage = f"STG_{table}_{report_id}"
                cur.execute(
                    f"CREATE OR REPLACE TEMPORARY TABLE {stage} LIKE {table}")
                write_pandas(conn, df, stage, quote_identifiers=False)

                if key_cols:
                    on = " AND ".join(f"t.{c}=s.{c}" for c in key_cols)
                    cols = list(df.columns)
                    set_clause = ", ".join(
                        f"t.{c}=s.{c}" for c in cols if c not in key_cols)
                    insert_cols = ", ".join(cols)
                    insert_vals = ", ".join(f"s.{c}" for c in cols)
                    cur.execute(f"""
                        MERGE INTO {table} t USING {stage} s ON {on}
                        WHEN MATCHED THEN UPDATE SET {set_clause}
                        WHEN NOT MATCHED THEN
                            INSERT ({insert_cols}) VALUES ({insert_vals})
                    """)
                else:
                    cur.execute(
                        f"INSERT INTO {table} SELECT * FROM {stage}")

                if wm_col and wm_col in df.columns:
                    new_wm = str(df[wm_col].astype(str).max())
                    _set_watermark(report_id, table, new_wm)

                uploaded.append({"target": table, "rows": int(len(df)),
                                 "merged_on": key_cols})
                logger.info(f"[snowflake] {table}: upserted {len(df)} rows")
            except Exception as e:
                logger.error(f"[snowflake] target {tgt.get('table')} failed: {e}")
                errors.append({"target": tgt.get("table"), "error": str(e)})
    finally:
        conn.close()

    return {"files": uploaded, "errors": errors}
