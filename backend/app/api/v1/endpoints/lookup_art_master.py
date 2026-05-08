"""
Lookup Art Master API
=====================
Upload an Excel file, select a join key column, pick columns from VW_MASTER_PRODUCT,
and get the LEFT JOIN result back.

Performance: only fetches rows from VW_MASTER_PRODUCT that match the uploaded keys
using SQL WHERE … IN (…) — avoids loading the entire view.
"""

import io
import json
from typing import Optional, List

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from loguru import logger

from app.database.session import get_data_engine
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.models.rbac import User

router = APIRouter(prefix="/lookup-art-master", tags=["Lookup Art Master"])

# Hard row cap for /run and /download. Files larger than this are rejected up
# front instead of allowed to run for many minutes and possibly OOM the worker.
MAX_UPLOAD_ROWS = 500_000


# ── Helpers ──────────────────────────────────────────────────────────────────

def _read_csv_robust(content: bytes) -> pd.DataFrame:
    """Try multiple encodings + separators so Excel-saved CSVs (BOM, UTF-16,
    cp1252, semicolon-separated, tab-separated) parse correctly instead of
    silently returning an empty frame."""
    size = len(content)
    head_preview = content[:200].decode("utf-8", errors="replace")
    logger.info(f"[lookup] CSV upload: {size} bytes, head={head_preview!r}")

    if size == 0:
        raise ValueError("Uploaded file is empty (0 bytes)")

    last_err: Optional[Exception] = None
    attempts = [
        {"encoding": "utf-8-sig", "sep": None, "engine": "python"},
        {"encoding": "utf-8",     "sep": None, "engine": "python"},
        {"encoding": "utf-16",    "sep": None, "engine": "python"},
        {"encoding": "cp1252",    "sep": None, "engine": "python"},
        {"encoding": "utf-8-sig", "sep": ","},
        {"encoding": "utf-8-sig", "sep": ";"},
        {"encoding": "utf-8-sig", "sep": "\t"},
    ]
    best: Optional[pd.DataFrame] = None
    best_opts = None
    for opts in attempts:
        try:
            df = pd.read_csv(io.BytesIO(content), low_memory=False, **opts)
            # A "good" parse has more than 1 column. A single column likely means
            # the wrong separator collapsed the row into one cell — keep trying.
            if len(df.columns) > 1:
                logger.info(f"[lookup] CSV parsed: {len(df)} rows, {len(df.columns)} cols, opts={opts}")
                return df
            if best is None and len(df.columns) >= 1:
                best, best_opts = df, opts
        except Exception as e:
            last_err = e
            continue
    if best is not None:
        logger.info(f"[lookup] CSV parsed (single-col): {len(best)} rows, opts={best_opts}")
        return best
    if last_err:
        raise ValueError(f"CSV could not be parsed. Last error: {last_err}. "
                          f"File starts with: {head_preview!r}")
    raise ValueError(f"CSV parsed to empty frame. File starts with: {head_preview!r}")


def _read_upload(content: bytes, filename: str, sheet_name: Optional[str] = None) -> pd.DataFrame:
    lower = filename.lower()
    if lower.endswith(".csv"):
        return _read_csv_robust(content)
    elif lower.endswith((".xlsx", ".xls")):
        kw = {"sheet_name": sheet_name} if sheet_name else {}
        return pd.read_excel(io.BytesIO(content), **kw)
    raise ValueError("Unsupported file type. Use .csv, .xlsx, or .xls")


def _get_vw_columns(engine) -> List[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'VW_MASTER_PRODUCT'
            ORDER BY ORDINAL_POSITION
        """)).fetchall()
    return [r[0] for r in rows]


def _do_lookup(df_upload: pd.DataFrame, join_column: str,
               master_column: str, sel_cols: List[str], engine) -> pd.DataFrame:
    """
    Fast lookup: bulk-load unique keys into a session-scoped temp table, then
    pull matching VW_MASTER_PRODUCT rows in a single JOIN. Replaces the old
    100+ batched WHERE IN queries (which serialised against the view and
    starved other endpoints) with one round-trip on one connection.
    """
    # Unique non-null keys from the uploaded file
    keys = df_upload[join_column].dropna().astype(str).unique().tolist()
    if not keys:
        for c in sel_cols:
            if c != join_column:
                df_upload[c] = None
        return df_upload

    fetch_cols = list(dict.fromkeys([master_column] + sel_cols))
    cols_sql = ", ".join(f"v.[{c}]" for c in fetch_cols)

    # Use a single raw pyodbc connection for the whole operation — temp table
    # is session-scoped, so it MUST live on the same connection that runs the
    # JOIN. The `try/finally` guarantees the connection returns to the pool
    # even if the SELECT or pandas conversion throws.
    raw_conn = engine.raw_connection()
    try:
        cursor = raw_conn.cursor()
        cursor.fast_executemany = True

        # SQL Server temp tables (#name) are session-scoped, and SQLAlchemy's
        # connection pool keeps the underlying SQL session alive across
        # raw_conn.close() / re-checkout cycles. So a `#lookup_keys` from a
        # prior request can still be present on the same pooled session —
        # always drop first.
        cursor.execute(
            "IF OBJECT_ID('tempdb..#lookup_keys','U') IS NOT NULL "
            "DROP TABLE #lookup_keys"
        )
        # NVARCHAR(450) is the indexable max for nvarchar; covers any realistic key.
        cursor.execute(
            "CREATE TABLE #lookup_keys (k NVARCHAR(450) NOT NULL PRIMARY KEY)"
        )
        cursor.executemany(
            "INSERT INTO #lookup_keys (k) VALUES (?)",
            [(k,) for k in keys],
        )
        raw_conn.commit()

        sql = (
            f"SELECT {cols_sql} "
            f"FROM dbo.VW_MASTER_PRODUCT v WITH (NOLOCK) "
            f"INNER JOIN #lookup_keys k "
            f"  ON CAST(v.[{master_column}] AS NVARCHAR(450)) = k.k"
        )
        df_master = pd.read_sql(sql, raw_conn)

        # Best-effort cleanup so the next checkout of this pooled session
        # starts clean even if it skips the drop-if-exists above.
        try:
            cursor.execute("DROP TABLE #lookup_keys")
            raw_conn.commit()
        except Exception:
            pass
    finally:
        raw_conn.close()

    # Ensure matching dtypes for merge
    df_upload[join_column] = df_upload[join_column].astype(str)
    df_master[master_column] = df_master[master_column].astype(str)

    # When a master column collides with an upload column (e.g. user uploads a
    # file with empty MAJ_CAT/GEN_ART_NUMBER/CLR placeholders and asks the
    # lookup to fill them), prefer the master value so the result shows the
    # filled column instead of duplicated `MAJ_CAT` + `MAJ_CAT_master` pairs.
    # The join column itself is kept on the upload side.
    overlap_cols = [c for c in sel_cols
                    if c in df_upload.columns and c != join_column]
    if overlap_cols:
        df_upload = df_upload.drop(columns=overlap_cols)

    df_result = df_upload.merge(
        df_master, left_on=join_column, right_on=master_column,
        how="left", suffixes=("", "_master"),
        indicator="_lookup_match",
    )

    if master_column != join_column and master_column in df_result.columns:
        df_result.drop(columns=[master_column], inplace=True)

    return df_result


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/columns", response_model=APIResponse)
def get_master_columns(current_user: User = Depends(get_current_user)):
    """Return available columns from VW_MASTER_PRODUCT."""
    engine = get_data_engine()
    cols = _get_vw_columns(engine)
    return APIResponse(success=True,
        message=f"{len(cols)} columns available",
        data={"columns": cols})


@router.post("/preview", response_model=APIResponse)
def preview_upload(
    file: UploadFile = File(...),
    sheet_name: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
):
    """Preview uploaded file: return column names and row count.

    Sync handler so FastAPI runs it in a threadpool — pandas/openpyxl are
    blocking, and an `async def` here would freeze the event loop while a
    50 MB Excel file parses.
    """
    content = file.file.read()
    try:
        df = _read_upload(content, file.filename, sheet_name)
    except Exception as e:
        raise HTTPException(400, detail=f"Failed to read file: {e}")

    return APIResponse(success=True,
        message=f"{len(df)} rows, {len(df.columns)} columns",
        data={
            "columns": list(df.columns),
            "row_count": len(df),
            "sample": json.loads(df.head(5).to_json(orient="records", date_format="iso")),
        })


@router.post("/run", response_model=APIResponse)
def run_lookup(
    file: UploadFile = File(...),
    join_column: str = Form(...),
    master_column: str = Form(...),
    select_columns: str = Form(...),
    sheet_name: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
):
    """LEFT JOIN uploaded file with VW_MASTER_PRODUCT (filtered by uploaded keys).

    Sync handler — FastAPI dispatches to a threadpool, so the rest of the API
    stays responsive while pandas + SQL Server crunch through the join.
    """
    content = file.file.read()
    try:
        df_upload = _read_upload(content, file.filename, sheet_name)
    except Exception as e:
        raise HTTPException(400, detail=f"Failed to read file: {e}")

    if len(df_upload) > MAX_UPLOAD_ROWS:
        raise HTTPException(
            400,
            detail=f"File has {len(df_upload):,} rows (max {MAX_UPLOAD_ROWS:,}). "
                   f"Split into smaller files.",
        )

    if join_column not in df_upload.columns:
        raise HTTPException(400, detail=f"Column '{join_column}' not found in uploaded file")

    try:
        sel_cols = json.loads(select_columns)
    except Exception:
        raise HTTPException(400, detail="select_columns must be a valid JSON array")

    if not sel_cols:
        raise HTTPException(400, detail="Select at least one column from VW_MASTER_PRODUCT")

    engine = get_data_engine()
    vw_cols = _get_vw_columns(engine)
    if master_column not in vw_cols:
        raise HTTPException(400, detail=f"Master column '{master_column}' not in VW_MASTER_PRODUCT")

    try:
        df_result = _do_lookup(df_upload, join_column, master_column, sel_cols, engine)
    except Exception as e:
        logger.error(f"Lookup failed: {e}")
        raise HTTPException(500, detail=f"Lookup failed: {e}")

    total   = len(df_result)
    matched = int((df_result["_lookup_match"] == "both").sum()) \
              if "_lookup_match" in df_result.columns else 0
    if "_lookup_match" in df_result.columns:
        df_result = df_result.drop(columns=["_lookup_match"])

    preview = json.loads(
        df_result.head(500).to_json(orient="records", date_format="iso")
    )

    return APIResponse(success=True,
        message=f"Lookup complete: {matched}/{total} rows matched",
        data={
            "columns": list(df_result.columns),
            "total_rows": total,
            "matched_rows": matched,
            "preview": preview,
        })


@router.post("/download")
def download_lookup(
    file: UploadFile = File(...),
    join_column: str = Form(...),
    master_column: str = Form(...),
    select_columns: str = Form(...),
    sheet_name: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
):
    """Same as /run but returns the full result as an Excel download."""
    content = file.file.read()
    try:
        df_upload = _read_upload(content, file.filename, sheet_name)
    except Exception as e:
        raise HTTPException(400, detail=f"Failed to read file: {e}")

    if len(df_upload) > MAX_UPLOAD_ROWS:
        raise HTTPException(
            400,
            detail=f"File has {len(df_upload):,} rows (max {MAX_UPLOAD_ROWS:,}). "
                   f"Split into smaller files.",
        )

    if join_column not in df_upload.columns:
        raise HTTPException(400, detail=f"Column '{join_column}' not found in uploaded file")

    sel_cols = json.loads(select_columns)
    engine = get_data_engine()

    df_result = _do_lookup(df_upload, join_column, master_column, sel_cols, engine)
    if "_lookup_match" in df_result.columns:
        df_result = df_result.drop(columns=["_lookup_match"])

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df_result.to_excel(writer, index=False, sheet_name="Lookup_Result")
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=lookup_result.xlsx"},
    )
