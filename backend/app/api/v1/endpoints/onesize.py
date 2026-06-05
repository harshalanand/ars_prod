"""
OneSize Calculation API Endpoints
=================================
Read-only post-MSA analysis:
  1. Latest sequence in ARS_MSA_TOTAL
  2. Keep MAJCATs marked APPLICABLE='Y' in MASTER_SZ_APPLICABLE
  3. Group by (MAJCAT, GEN_ART, CLR) and broadcast count back to each row
  4. Keep rows where count <= threshold (default 2) and tag status='1sz'
  5. CROSS JOIN with Master_ALC_INPUT_ST_MASTER (ST_CD, ST_NM)

No database writes. Result is returned in-line for preview and as a CSV stream
for full export.
"""
import csv
import io
import math
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.session import get_data_db
from app.models.rbac import User
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user

router = APIRouter(prefix="/onesize", tags=["OneSize"])


# ---------------------------------------------------------------------------
# In-memory LRU result cache so Export doesn't have to re-run the pipeline.
# /run stores the computed dataframe here keyed by a UUID; /export?cache_key=X
# reads it back and streams CSV directly. LRU bounds memory, TTL handles
# stale entries from forgotten runs.
# ---------------------------------------------------------------------------
_RESULT_CACHE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 1800   # 30 minutes — drops after this regardless of access
_CACHE_MAX_ENTRIES = 3       # keep at most this many runs in memory


# ---------------------------------------------------------------------------
# Job registry — one entry per OneSize run kicked off via POST /jobs.
# Soft-cancel: cancel_event is checked at each stage boundary via _StagesList
# below. We can't kill a mid-stage SQL query, but the worker exits at the
# next .append() call.
# ---------------------------------------------------------------------------
class OneSizeCancelled(Exception):
    """Raised inside the worker thread when the job's cancel_event fires."""


class _StagesList(list):
    """Drop-in replacement for the stages list used by _compute_onesize.

    Every .append() first checks the bound cancel_event; if it's set, we
    raise OneSizeCancelled so the pipeline aborts at the next stage boundary
    without scattering checks through 2500 lines of code.
    """
    def __init__(self, cancel_event: Optional[threading.Event] = None) -> None:
        super().__init__()
        self._cancel = cancel_event

    def append(self, item: Any) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise OneSizeCancelled()
        super().append(item)


_JOBS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_JOBS_LOCK = threading.Lock()
_JOBS_MAX_ENTRIES = 10  # oldest evicted past this; user can explicitly delete


def _job_snapshot(job: Dict[str, Any], include_preview: bool = False) -> Dict[str, Any]:
    """Build a JSON-safe snapshot of a job (no df, no Event, no Thread)."""
    snap = {
        "job_id":       job["id"],
        "status":       job["status"],
        "stages":       list(job["stages"]),
        "total_rows":   job.get("total_rows", 0),
        "stores":       job.get("stores", 0),
        "sequence_id":  job.get("sequence_id"),
        "columns":      job.get("columns", []),
        "cache_key":    job.get("cache_key", ""),
        "error":        job.get("error"),
        "persist_error": job.get("persist_error"),
        "persisted_rows": job.get("persisted_rows"),
        "started_at":   job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "params":       job.get("params", {}),
        "preview_limit": job.get("preview_limit", 0),
    }
    if include_preview:
        snap["preview_rows"] = job.get("preview_rows", []) or []
    return snap


# ---------------------------------------------------------------------------
# Persistence — ARS_ONESIZE_ALLOCATION
# ---------------------------------------------------------------------------
# Auto-created on first successful run. Replace-per-sequence_id: prior rows
# for the same sequence_id are DELETEd before the new set is INSERTed. Both
# happen inside a single transaction so a half-failed insert can't leave the
# table empty.
# ---------------------------------------------------------------------------
ALLOCATION_TABLE = "ARS_ONESIZE_ALLOCATION"

_ALLOCATION_DDL = f"""
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_NAME = '{ALLOCATION_TABLE}'
)
BEGIN
    CREATE TABLE [{ALLOCATION_TABLE}] (
        id              BIGINT IDENTITY(1,1) PRIMARY KEY,
        sequence_id     BIGINT       NOT NULL,
        job_id          NVARCHAR(64) NOT NULL,
        run_at          DATETIME2    NOT NULL CONSTRAINT DF_OneSizeAlloc_RunAt DEFAULT SYSUTCDATETIME(),
        run_by          NVARCHAR(100) NULL,

        -- Article identity
        DIV             NVARCHAR(50)  NULL,
        SUB_DIV         NVARCHAR(50)  NULL,
        MAJCAT          NVARCHAR(50)  NULL,
        SSN             NVARCHAR(50)  NULL,
        SZ              NVARCHAR(50)  NULL,
        MACRO_MVGR      NVARCHAR(100) NULL,
        MICRO_MVGR      NVARCHAR(100) NULL,
        CLR             NVARCHAR(100) NULL,
        GEN_ART         NVARCHAR(50)  NULL,
        GEN_ART_NUMBER  NVARCHAR(50)  NULL,
        GEN_ART_DESC    NVARCHAR(255) NULL,
        ARTICLE_NUMBER  NVARCHAR(50)  NULL,
        ARTICLE_DESC    NVARCHAR(255) NULL,
        M_VND_CD        NVARCHAR(50)  NULL,
        M_VND_NM        NVARCHAR(100) NULL,
        MC_DESC         NVARCHAR(255) NULL,
        FAB             NVARCHAR(50)  NULL,
        RNG_SEG         NVARCHAR(50)  NULL,
        SEG             NVARCHAR(50)  NULL,
        MRP             FLOAT         NULL,
        [DATE]          NVARCHAR(50)  NULL,
        V02_FRESH       NVARCHAR(20)  NULL,

        -- MSA opening qty
        FNL_Q           FLOAT NULL,
        STK_QTY         FLOAT NULL,
        PEND_QTY        FLOAT NULL,
        HOLD_QTY        FLOAT NULL,
        ARS_PEND        FLOAT NULL,
        [list]          NVARCHAR(50) NULL,

        -- Store
        ST_CD           NVARCHAR(50)  NULL,
        ST_NM           NVARCHAR(255) NULL,
        RDC             NVARCHAR(50)  NULL,
        ST_STATUS       NVARCHAR(20)  NULL,
        ST_RANK         FLOAT NULL,
        ST_RANK_STORE   FLOAT NULL,
        days            FLOAT NULL,
        PAK_SZ          FLOAT NULL,
        [status]        NVARCHAR(20) NULL,

        -- Counts / density / contribution
        applicable_sz   NVARCHAR(2) NULL,
        [count]         FLOAT NULL,
        fnl_q_sum       FLOAT NULL,
        maj_cat_q       FLOAT NULL,
        final_msa       NVARCHAR(2) NULL,
        AVG_DENSITY     FLOAT NULL,
        cont            FLOAT NULL,

        -- MAJ-SZ block
        DISP_Q          FLOAT NULL,
        SAL_PD_SZ       FLOAT NULL,
        SAL_PD          FLOAT NULL,
        ACS_D           FLOAT NULL,
        ALC_D           FLOAT NULL,
        MBQ_SZ          FLOAT NULL,
        STK_TTL_SZ      FLOAT NULL,
        REQ             FLOAT NULL,

        -- Var-art block
        OPT_GRID_MBQ      FLOAT NULL,
        OPT_GRID_DISP_Q   FLOAT NULL,
        var_art_disp      FLOAT NULL,
        L7_DAILY          FLOAT NULL,
        AUTO_GEN_ART_SALE FLOAT NULL,
        PER_OPT_SALE      FLOAT NULL,
        AGE               FLOAT NULL,
        MAX_DAILY_SALE    FLOAT NULL,
        SALE_VAR_ART      FLOAT NULL,
        MBQ_VAR           FLOAT NULL,
        STK_TTL           FLOAT NULL,
        VAR_REQ           FLOAT NULL,

        -- Allocation outputs
        MSA_REMAIN       FLOAT NULL,
        POOL_POS         BIGINT NULL,
        ALLOC            FLOAT NULL,
        FINAL_ALLOCATION FLOAT NULL,
        REMAIN_AFTER     FLOAT NULL,
        DEMAND_SRC       NVARCHAR(20) NULL,

        INDEX IX_OneSizeAlloc_Seq NONCLUSTERED (sequence_id),
        INDEX IX_OneSizeAlloc_Job NONCLUSTERED (job_id)
    )
END
"""


def _ensure_allocation_table(engine) -> None:
    """Create ARS_ONESIZE_ALLOCATION if missing. Idempotent."""
    with engine.begin() as conn:
        conn.execute(text(_ALLOCATION_DDL))


def _persist_onesize_result(
    engine,
    df: pd.DataFrame,
    sequence_id: int,
    job_id: str,
    run_by: Optional[str],
) -> int:
    """Replace-per-sequence_id write of `df` into ARS_ONESIZE_ALLOCATION.

    Atomic: DELETE for this sequence_id and INSERTs of the new rows happen
    inside one transaction so a partial chunk-insert failure rolls back the
    DELETE and leaves the prior data intact.

    Returns the number of rows inserted.
    """
    if df is None or df.empty:
        return 0
    if sequence_id is None:
        raise ValueError("sequence_id is required for persistence")

    _ensure_allocation_table(engine)

    # Discover the table's actual column names so we can align df → table.
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION"
            ),
            {"t": ALLOCATION_TABLE},
        ).fetchall()
    table_cols = [r[0] for r in rows]
    # Audit cols + identity are written separately or by default — drop from match set.
    excluded = {"id", "run_at"}
    table_col_lower = {c.lower(): c for c in table_cols if c.lower() not in excluded}

    # Build the insert frame.
    insert_df = df.copy()
    insert_df["sequence_id"] = int(sequence_id)
    insert_df["job_id"]      = str(job_id)
    insert_df["run_by"]      = run_by

    # Keep only columns the table knows about (case-insensitive). Rename to
    # the table's exact casing so pandas writes match the columns.
    keep_lower = [c for c in insert_df.columns if c.lower() in table_col_lower]
    insert_df = insert_df[keep_lower]
    insert_df.columns = [table_col_lower[c.lower()] for c in insert_df.columns]

    # NaN / pd.NA → None so SQL Server gets proper NULLs.
    insert_df = insert_df.astype(object).where(pd.notnull(insert_df), None)

    # Both engines are created with fast_executemany=True (pyodbc), so a plain
    # executemany (no method='multi') sends each chunk as one bulk parameter
    # array — not subject to SQL Server's 2100-parameter-per-statement cap.
    # method='multi' would build a single giant INSERT VALUES(...) statement,
    # bypassing fast_executemany and forcing ~28-row chunks. Don't use it.
    with engine.begin() as conn:
        conn.execute(
            text(f"DELETE FROM [{ALLOCATION_TABLE}] WHERE sequence_id = :sid"),
            {"sid": int(sequence_id)},
        )
        insert_df.to_sql(
            ALLOCATION_TABLE,
            conn,
            if_exists="append",
            index=False,
            chunksize=20_000,
        )
    return len(insert_df)


def _cache_store(df: "pd.DataFrame", meta: Dict[str, Any]) -> str:
    """Stash df+meta in the cache, return a fresh UUID handle."""
    key = uuid.uuid4().hex
    with _CACHE_LOCK:
        _RESULT_CACHE[key] = {"df": df, "meta": meta, "ts": time.time()}
        while len(_RESULT_CACHE) > _CACHE_MAX_ENTRIES:
            evicted_key, _ = _RESULT_CACHE.popitem(last=False)
            logger.info(f"[onesize][cache] LRU-evicted {evicted_key}")
    logger.info(
        f"[onesize][cache] stored key={key} rows={len(df)} "
        f"(total entries={len(_RESULT_CACHE)})"
    )
    return key


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    """Return cache entry if present and not expired, else None."""
    with _CACHE_LOCK:
        entry = _RESULT_CACHE.get(key)
        if not entry:
            return None
        if time.time() - entry["ts"] > _CACHE_TTL_SECONDS:
            _RESULT_CACHE.pop(key, None)
            logger.info(f"[onesize][cache] expired {key}")
            return None
        _RESULT_CACHE.move_to_end(key)  # mark as recently used
        return entry


# ---------------------------------------------------------------------------
# Table / column names — keep in one place so renames stay localized
# ---------------------------------------------------------------------------
MSA_TOTAL_TABLE = "ARS_MSA_TOTAL"
SZ_APPLICABLE_TABLE = "MASTER_SZ_APPLICABLE"
ST_MASTER_TABLE = "Master_ALC_INPUT_ST_MASTER"
CONT_SZ_TABLE = "Master_CONT_SZ"
CALC_ST_MAJ_CAT_TABLE = "ARS_CALC_ST_MAJ_CAT"
GRID_MJ_TABLE = "ARS_GRID_MJ_VAR_ART"       # size-grain — used for STK_TTL
GRID_MJ_BASE_TABLE = "ARS_GRID_MJ"           # maj_cat-grain — used for ACS_D
# Sources for MAX_DAILY_SALE = MAX(L-7/7, AUTO_GEN_ART_SALE):
#   - AUTO_GEN_ART_SALE  ← MASTER_GEN_ART_SALE.SAL_PD  (option-grain baseline)
#   - L-7/7              ← ARS_GRID_MJ_GEN_ART.<L-7 col> ÷ 7  (option-grain recent)
# We read both upstream sources directly so OneSize doesn't depend on Listing
# Generation Parts 3.5b or 4c having been run.
MGAS_TABLE = "MASTER_GEN_ART_SALE"
MGAA_TABLE = "MASTER_GEN_ART_AGE"            # option-grain AGE for new/old-article switch
GEN_ART_GRID_TABLE = "ARS_GRID_MJ_GEN_ART"
STORE_RANKING_TABLE = "ARS_STORE_RANKING"   # (ST_CD, MAJ_CAT) ranking for allocation order
AGE_THRESHOLD_DAYS = 15                      # < 15 → new article (include PER_OPT_SALE in MAX)

# Columns the result must surface first (rest of the MSA cols follow).
PREFERRED_COLS = [
    # ── A. ARTICLE IDENTITY ────────────────────────────────────────────────
    "DIV", "SUB_DIV", "MAJCAT", "SSN",
    "SZ",
    "MACRO_MVGR", "MICRO_MVGR", "CLR",
    "GEN_ART", "GEN_ART_NUMBER", "GEN_ART_DESC",
    "ARTICLE_NUMBER", "ARTICLE_DESC",
    "M_VND_CD", "M_VND_NM",
    "MC_DESC", "FAB", "RNG_SEG", "SEG", "MRP", "DATE", "V02_FRESH",

    # ── B. MSA OPENING QTY ─────────────────────────────────────────────────
    "FNL_Q",
    "STK_QTY", "PEND_QTY", "HOLD_QTY", "ARS_PEND", "list",

    # ── C. STORE INFO ──────────────────────────────────────────────────────
    "ST_CD", "ST_NM", "RDC", "ST_STATUS", "ST_RANK", "ST_RANK_STORE", "days",
    "PAK_SZ",
    "status",                          # SZ_APPLICABLE

    # ── E. COUNTS / DENSITY ────────────────────────────────────────────────
    "applicable_sz",                   # Y/N from MASTER_SZ_APPLICABLE
    "count",                           # ART CNT (MSA-STK) — sizes per MAJ+GEN+CLR
    "fnl_q_sum",                       # maj_gen_cl_q — sum FNL_Q per MAJ+GEN+CLR
    "maj_cat_q",                       # sum FNL_Q per MAJ_CAT
    "final_msa",                       # Y/N — 4-condition AND gate
    "AVG_DENSITY",                     # ACC DENSITY

    # ── F. SIZE CONTRIBUTION % ─────────────────────────────────────────────
    "cont",                            # SZ CONT%

    # ── G. MAJ-SZ BLOCK ────────────────────────────────────────────────────
    "DISP_Q",                          # DISP
    "SAL_PD_SZ",                       # PER DAY SALE (maj_cat grain, from ARS_CALC_ST_MAJ_CAT)
    "SAL_PD",                          # PER DAY SALE (option grain, from MASTER_GEN_ART_SALE)
    "ACS_D", "ALC_D",
    "MBQ_SZ",                          # MAJ MBQ
    "STK_TTL_SZ",                      # ST_MAJ STK
    "REQ",                             # SHORT (size-grain)

    # ── H. VAR-ART BLOCK ───────────────────────────────────────────────────
    "OPT_GRID_MBQ", "OPT_GRID_DISP_Q",
    "var_art_disp",                    # ART DISP
    "L7_DAILY", "AUTO_GEN_ART_SALE", "PER_OPT_SALE",
    "AGE",
    "MAX_DAILY_SALE",                  # PER DAY (variant level)
    "SALE_VAR_ART",
    "MBQ_VAR",                         # VAR-MBQ
    "STK_TTL",                         # VAR ART-STK
    "VAR_REQ",                         # SHORT (IN PK-SZ)

    # ── I. ALGO-1 (allocation) ─────────────────────────────────────────────
    "MSA_REMAIN",                      # initial pool
    "POOL_POS",
    "ALLOC",                           # BTM-UP-ALC + FINAL ALC-Q
    "FINAL_ALLOCATION",                # MROUND(ALLOC, PAK_SZ) — whole-carton qty
    "REMAIN_AFTER",                    # CHECK REM MSA (after this row)
    "DEMAND_SRC",
]


def _table_columns(db: Session, table: str) -> List[str]:
    rows = db.execute(
        text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION"
        ),
        {"t": table},
    ).fetchall()
    return [r[0] for r in rows]


def _resolve_col(cols: List[str], candidates: List[str]) -> Optional[str]:
    """Find the first matching column name (case-insensitive)."""
    lower = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def _norm_str(s: pd.Series) -> pd.Series:
    """Trim and upper-case a Series — canonical join-key form."""
    return s.astype(str).str.strip().str.upper()


def _norm_num_or_str(s: pd.Series) -> pd.Series:
    """Normalize a join key that may be numeric in one source and string in
    another. Numeric values become integer strings (no trailing '.0'); the
    rest stays as trimmed-uppercase strings. Eliminates the 12345 vs '12345'
    vs '12345.0' vs ' 12345 ' family of silent mismatches."""
    n = pd.to_numeric(s, errors="coerce")
    is_num = n.notna()
    out = s.astype(str).str.strip().str.upper()
    out.loc[is_num] = n[is_num].astype("Int64").astype(str)
    return out


DEFAULT_SSNS: List[str] = ["A", "OC", "S"]


def _compute_onesize(
    db: Session,
    placeholder_value: str = "A",
    rdcs: Optional[List[str]] = None,
    ssns: Optional[List[str]] = None,
    job: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the full OneSize calculation. Pure read; no writes.

    When `job` is supplied, stages are appended to `job['stages']` so the
    poll endpoint reflects live progress, and any append() will raise
    OneSizeCancelled if the job's cancel_event fired.
    """
    stages: List[Dict[str, Any]] = job["stages"] if job is not None else []

    # ---- Stage 1: latest sequence_id ------------------------------------
    seq_row = db.execute(
        text(f"SELECT MAX(sequence_id) FROM [{MSA_TOTAL_TABLE}]")
    ).scalar()
    if seq_row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No data found in {MSA_TOTAL_TABLE}",
        )
    sequence_id = int(seq_row)
    stages.append({"stage": "latest_sequence", "sequence_id": sequence_id})
    logger.info(f"[onesize] latest sequence_id={sequence_id}")

    # ---- Stage 2: load MSA rows for that sequence -----------------------
    msa_cols = _table_columns(db, MSA_TOTAL_TABLE)
    if not msa_cols:
        raise HTTPException(status_code=500, detail=f"{MSA_TOTAL_TABLE} not found")

    majcat_col = _resolve_col(msa_cols, ["MAJCAT", "MAJ_CAT"])
    gen_art_col = _resolve_col(msa_cols, ["GEN_ART", "GEN_ART_NUMBER", "GEN_ART_NO"])
    clr_col = _resolve_col(msa_cols, ["CLR", "COLOR", "COLOUR"])
    sz_col = _resolve_col(msa_cols, ["SZ", "SIZE", "SZ_CD", "SZ_CODE"])
    # Sum is taken over FNL_Q (final available qty after PEND + HOLD), not STK_QTY.
    qty_col = _resolve_col(msa_cols, ["FNL_Q", "FNL_QTY"])

    missing = [n for n, v in [("MAJCAT", majcat_col), ("GEN_ART", gen_art_col), ("CLR", clr_col)] if not v]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"{MSA_TOTAL_TABLE} missing required columns: {missing}",
        )

    # SSN filter — empty selection means "no SSN restriction" (all rows).
    # Trim+upper-case to match the canonical form used on disk.
    ssn_list = [
        str(s).strip().upper()
        for s in (ssns if ssns is not None else DEFAULT_SSNS)
        if str(s).strip()
    ]
    if ssn_list:
        sql_params: Dict[str, Any] = {"sid": sequence_id}
        in_keys: List[str] = []
        for i, v in enumerate(ssn_list):
            key = f"ssn{i}"
            in_keys.append(f":{key}")
            sql_params[key] = v
        msa_sql = (
            f"SELECT * FROM [{MSA_TOTAL_TABLE}] "
            f"WHERE sequence_id = :sid "
            f"AND UPPER(LTRIM(RTRIM(CAST([SSN] AS NVARCHAR(50))))) IN ({', '.join(in_keys)})"
        )
        msa_df = pd.read_sql(text(msa_sql), db.bind, params=sql_params)
    else:
        msa_df = pd.read_sql(
            text(f"SELECT * FROM [{MSA_TOTAL_TABLE}] WHERE sequence_id = :sid"),
            db.bind,
            params={"sid": sequence_id},
        )
    stages.append({
        "stage": "load_msa",
        "rows": len(msa_df),
        "ssn_filter": ssn_list if ssn_list else "ALL",
    })
    logger.info(f"[onesize] loaded {len(msa_df)} MSA rows (ssn filter={ssn_list or 'ALL'})")
    if msa_df.empty:
        return {
            "sequence_id": sequence_id,
            "total_rows": 0,
            "columns": [],
            "rows": [],
            "stores": 0,
            "stages": stages,
        }

    # ---- Stage 3: filter to applicable MAJCATs --------------------------
    app_cols = _table_columns(db, SZ_APPLICABLE_TABLE)
    if not app_cols:
        raise HTTPException(status_code=500, detail=f"{SZ_APPLICABLE_TABLE} not found")
    app_majcat = _resolve_col(
        app_cols,
        ["MAJCAT", "MAJ_CAT", "MAJOR_CATEGORY", "MAJORCAT", "MAJ_CATEGORY"],
    )
    app_flag = _resolve_col(
        app_cols,
        ["APPLICABLE", "IS_APPLICABLE", "FLAG", "ONE_SIZE", "ONESIZE",
         "ONE_SIZE_APPLICABLE", "SZ_APPLICABLE", "ACTIVE", "STATUS", "IND", "Y_N"],
    )
    if not app_majcat or not app_flag:
        raise HTTPException(
            status_code=500,
            detail=(
                f"{SZ_APPLICABLE_TABLE} missing required columns. "
                f"Need MAJCAT-like and APPLICABLE-like columns. "
                f"Found columns: {app_cols}. "
                f"Resolved MAJCAT={app_majcat!r}, APPLICABLE={app_flag!r}."
            ),
        )

    applicable = db.execute(
        text(
            f"SELECT DISTINCT [{app_majcat}] FROM [{SZ_APPLICABLE_TABLE}] "
            f"WHERE UPPER(LTRIM(RTRIM(CAST([{app_flag}] AS NVARCHAR(10))))) = 'Y'"
        )
    ).fetchall()
    applicable_majcats = {str(r[0]).strip() for r in applicable if r[0] is not None}
    stages.append({"stage": "applicable_majcats", "count": len(applicable_majcats)})
    logger.info(f"[onesize] {len(applicable_majcats)} applicable MAJCATs")

    if not applicable_majcats:
        return {
            "sequence_id": sequence_id,
            "total_rows": 0,
            "columns": [],
            "rows": [],
            "stores": 0,
            "stages": stages,
        }

    # Tag every row with its SZ_APPLICABLE flag instead of pre-filtering.
    # The actual filter happens in Stage 5 (compute_final_msa) where it's
    # AND-combined with count, maj_gen_cl_q and maj_cat_q thresholds.
    msa_df[majcat_col] = msa_df[majcat_col].astype(str).str.strip()
    msa_df["applicable_sz"] = np.where(
        msa_df[majcat_col].isin(applicable_majcats), "Y", "N"
    )
    stages.append({
        "stage": "tag_applicable_sz",
        "rows": len(msa_df),
        "y_rows": int((msa_df["applicable_sz"] == "Y").sum()),
        "n_rows": int((msa_df["applicable_sz"] == "N").sum()),
    })

    # ---- Stage 4: group by (MAJCAT, GEN_ART, CLR), broadcast count ------
    grp_cols = [majcat_col, gen_art_col, clr_col]
    msa_df["count"] = msa_df.groupby(grp_cols)[majcat_col].transform("size")
    stages.append({"stage": "group_and_count", "rows": len(msa_df)})

    # ---- Stage 4b: broadcast SUM(FNL_Q) per combination -----------------
    # Same shape as `count` — every row in a (MAJCAT, GEN_ART, CLR) combo
    # gets the total final-available qty for that combo in `fnl_q_sum`.
    if qty_col:
        msa_df[qty_col] = pd.to_numeric(msa_df[qty_col], errors="coerce").fillna(0)
        msa_df["fnl_q_sum"] = msa_df.groupby(grp_cols)[qty_col].transform("sum")
    else:
        msa_df["fnl_q_sum"] = 0.0
    stages.append({"stage": "group_fnl_q_sum", "rows": len(msa_df), "qty_col": qty_col or "(none)"})

    # ---- Stage 4c: broadcast SUM(FNL_Q) per MAJ_CAT (new aggregation) ---
    # maj_cat_q is the total final-available qty for the whole MAJ_CAT, repeated
    # on every row in that MAJ_CAT. Used by Stage 5 in the final_msa gate.
    if qty_col:
        msa_df["maj_cat_q"] = msa_df.groupby(majcat_col)[qty_col].transform("sum")
    else:
        msa_df["maj_cat_q"] = 0.0
    stages.append({"stage": "group_maj_cat_q", "rows": len(msa_df)})

    # ---- Stage 5: pre-cross-join — drop applicable_sz='N' rows ----------
    # These fail the gate and can never pass regardless of store. Drop now to
    # keep the cross-join small.
    before_gate = len(msa_df)
    msa_df = msa_df[msa_df["applicable_sz"] == "Y"].copy()
    stages.append({
        "stage":         "drop_applicable_sz_n",
        "rows_before":   before_gate,
        "rows":          len(msa_df),
        "dropped_n":     before_gate - len(msa_df),
    })
    logger.info(
        f"[onesize] dropped {before_gate - len(msa_df)} applicable_sz='N' rows "
        f"before cross-join; {len(msa_df)} remain"
    )

    if msa_df.empty:
        return {
            "sequence_id": sequence_id,
            "total_rows": 0,
            "columns": [],
            "rows": [],
            "stores": 0,
            "stages": stages,
            "_df": pd.DataFrame(),
        }

    # ---- Stage 5a: compute row-level OR (everything except ST_STATUS) ---
    # row_or is TRUE when ANY of the three row-level OR-clauses passes:
    #   count ≤ 2, maj_gen_cl_q ≤ 50, maj_cat_q ≤ 400
    # If row_or=TRUE, the row WILL pass final_msa regardless of which store
    # it joins with. If row_or=FALSE, the row only passes if joined with an
    # OLD store. This lets us decide final_msa AT the cross-join — no
    # separate Y/N filter is needed afterwards.
    MAJ_GEN_CL_Q_LIMIT = 50
    MAJ_CAT_Q_LIMIT    = 400
    COUNT_LIMIT        = 2

    _cnt = pd.to_numeric(msa_df["count"],     errors="coerce").fillna(0)
    _mgc = pd.to_numeric(msa_df["fnl_q_sum"], errors="coerce").fillna(0)
    _maj = pd.to_numeric(msa_df["maj_cat_q"], errors="coerce").fillna(0)

    msa_df["row_or"] = (
        _cnt.le(COUNT_LIMIT) |
        _mgc.le(MAJ_GEN_CL_Q_LIMIT) |
        _maj.le(MAJ_CAT_Q_LIMIT)
    )
    stages.append({
        "stage":               "compute_row_or",
        "rows":                int(len(msa_df)),
        "row_or_true":         int(msa_df["row_or"].sum()),
        "row_or_false":        int((~msa_df["row_or"]).sum()),
        "thresholds": {
            "count":        f"<= {COUNT_LIMIT}",
            "maj_gen_cl_q": f"<= {MAJ_GEN_CL_Q_LIMIT}",
            "maj_cat_q":    f"<= {MAJ_CAT_Q_LIMIT}",
        },
        "note": ("row_or=TRUE → will pass final_msa for ANY store. "
                 "row_or=FALSE → only passes if joined with ST_STATUS='OLD'."),
    })
    logger.info(
        f"[onesize] row_or — TRUE={int(msa_df['row_or'].sum())} "
        f"FALSE={int((~msa_df['row_or']).sum())} "
        f"(only TRUE rows survive — ST_STATUS no longer in the OR-block)"
    )

    # ---- Stage 5b: drop row_or=FALSE rows BEFORE cross-join -------------
    # ST_STATUS='OLD' clause was removed from the OR-block. The filter is
    # now purely row-level, so we apply it pre-cross-join. Every surviving
    # row is final_msa='Y' by construction; no post-cross-join filter needed.
    rows_before_row_or = len(msa_df)
    msa_df = msa_df[msa_df["row_or"] == True].copy()
    msa_df = msa_df.drop(columns=["row_or"])
    stages.append({
        "stage":         "drop_row_or_false",
        "rows_before":   rows_before_row_or,
        "rows":          len(msa_df),
        "dropped":       rows_before_row_or - len(msa_df),
        "rule":          "keep iff (count<=2 OR maj_gen_cl_q<=50 OR maj_cat_q<=400)",
    })
    logger.info(
        f"[onesize] dropped {rows_before_row_or - len(msa_df)} row_or=FALSE rows "
        f"pre-cross-join ({rows_before_row_or}→{len(msa_df)})"
    )

    if msa_df.empty:
        return {
            "sequence_id": sequence_id,
            "total_rows": 0,
            "columns": [],
            "rows": [],
            "stores": 0,
            "stages": stages,
            "_df": pd.DataFrame(),
        }

    # ---- Stage 5c: drop placeholder rows (CLR='A' or SZ='A') ------------
    # 'A' is the default fill value for missing CLR/SZ in msa_service.py
    # (see fill_defaults). Treat as case-insensitive exact match after trim.
    placeholder = str(placeholder_value).strip().upper()
    before = len(msa_df)
    clr_norm = msa_df[clr_col].astype(str).str.strip().str.upper()
    mask_drop = clr_norm == placeholder
    if sz_col:
        sz_norm = msa_df[sz_col].astype(str).str.strip().str.upper()
        mask_drop = mask_drop | (sz_norm == placeholder)
    msa_df = msa_df[~mask_drop].copy()
    stages.append({
        "stage": "filter_placeholder",
        "rows": len(msa_df),
        "dropped": before - len(msa_df),
        "value": placeholder_value,
        "sz_col": sz_col or "(missing)",
    })
    logger.info(f"[onesize] {len(msa_df)} rows after CLR/SZ='{placeholder_value}' drop")

    # ---- Stage 5d: recompute aggregates on the filtered data ------------
    # count, fnl_q_sum (maj_gen_cl_q), and maj_cat_q were originally computed
    # at Stage 4-4c on the FULL post-load dataframe (so they could serve as
    # gating thresholds in Stage 5a's row_or). Now that we've dropped
    # applicable_sz='N', row_or=FALSE, and placeholder rows, recompute these
    # on the surviving subset so the displayed values match what you'd sum
    # in Excel after opening the CSV.
    if not msa_df.empty:
        grp_cols = [majcat_col, gen_art_col, clr_col]
        msa_df["count"] = msa_df.groupby(grp_cols)[majcat_col].transform("size")
        if qty_col:
            msa_df["fnl_q_sum"] = msa_df.groupby(grp_cols)[qty_col].transform("sum")
            msa_df["maj_cat_q"] = msa_df.groupby(majcat_col)[qty_col].transform("sum")
        stages.append({
            "stage": "recompute_aggregates_post_filter",
            "rows": len(msa_df),
            "note": ("count / fnl_q_sum / maj_cat_q recomputed on filtered data "
                     "— values now match what you sum in the CSV"),
        })
        logger.info(
            f"[onesize] recomputed count/fnl_q_sum/maj_cat_q on {len(msa_df)} "
            f"post-filter rows"
        )

    msa_df["status"] = "1sz"

    if msa_df.empty:
        return {
            "sequence_id": sequence_id,
            "total_rows": 0,
            "columns": [],
            "rows": [],
            "stores": 0,
            "stages": stages,
        }

    # ---- Stage 6: load stores ------------------------------------------
    st_cols = _table_columns(db, ST_MASTER_TABLE)
    if not st_cols:
        raise HTTPException(status_code=500, detail=f"{ST_MASTER_TABLE} not found")
    st_cd = _resolve_col(st_cols, ["ST_CD", "WERKS", "STORE_CODE"])
    st_nm = _resolve_col(st_cols, ["ST_NM", "STORE_NAME", "ST_NAME"])
    # Same RDC candidates listing.py uses, so this stays consistent.
    st_rdc = _resolve_col(st_cols, ["RDC", "WAREHOUSE", "HUB", "WH_CD"])
    print(f"Resolved store master columns: ST_CD={st_cd}, ST_NM={st_nm}, RDC={st_rdc}")
    if not st_cd:
        raise HTTPException(status_code=500, detail=f"{ST_MASTER_TABLE} missing ST_CD column")

    # Day components — `days` in the result is the sum of these three per store.
    c_slcvr = _resolve_col(st_cols, ["SL_CVR", "SL_COVER", "SLCVR"])
    c_intd  = _resolve_col(st_cols, ["INT_DAYS", "INT_DAY", "INT"])
    c_prdd  = _resolve_col(st_cols, ["PRD_DAYS", "PRD_DAY", "PRD"])
    # ST_STATUS — used by final_msa OR-clause (= "OLD STORE" in the Excel
    # rule; DB stores it as "OLD" / "UPC"). Joined as a per-store attribute.
    c_status = _resolve_col(st_cols, ["ST_STATUS", "STORE_STATUS", "STATUS"])
    day_cols = [c for c in (c_slcvr, c_intd, c_prdd) if c]

    select_parts: List[str] = [f"[{st_cd}] AS ST_CD"]
    group_parts:  List[str] = [f"[{st_cd}]"]
    if st_nm:
        select_parts.append(f"[{st_nm}] AS ST_NM")
        group_parts.append(f"[{st_nm}]")
    else:
        select_parts.append("CAST(NULL AS NVARCHAR(255)) AS ST_NM")
    if st_rdc:
        select_parts.append(f"[{st_rdc}] AS RDC")
        group_parts.append(f"[{st_rdc}]")
    if c_status:
        select_parts.append(f"[{c_status}] AS ST_STATUS")
        group_parts.append(f"[{c_status}]")
    else:
        select_parts.append("CAST(NULL AS NVARCHAR(50)) AS ST_STATUS")
    if day_cols:
        # SUM each component (cast to FLOAT, NULL → 0) then add them. SQL Server
        # collapses duplicate rows for the same store via the GROUP BY below.
        sum_expr = " + ".join(
            f"SUM(ISNULL(CAST([{c}] AS FLOAT), 0))" for c in day_cols
        )
        select_parts.append(f"({sum_expr}) AS days")

    if day_cols:
        # Aggregated path — one row per (ST_CD, ST_NM, RDC), days summed.
        stores_sql = (
            f"SELECT {', '.join(select_parts)} FROM [{ST_MASTER_TABLE}] "
            f"GROUP BY {', '.join(group_parts)} ORDER BY [{st_cd}]"
        )
    else:
        # No day columns to sum — keep the original DISTINCT semantics.
        stores_sql = (
            f"SELECT DISTINCT {', '.join(select_parts)} FROM [{ST_MASTER_TABLE}] "
            f"ORDER BY [{st_cd}]"
        )

    stores_df = pd.read_sql(text(stores_sql), db.bind)
    if "days" not in stores_df.columns:
        stores_df["days"] = None
    # ST_STATUS is loaded for visibility in the result but no longer used
    # for filtering — the OR-block is now purely row-level (count, maj_gen_cl_q,
    # maj_cat_q) and was applied pre-cross-join in Stage 5b.
    stages.append({
        "stage": "load_stores",
        "stores": len(stores_df),
        "rdc_col":      st_rdc   or "(missing)",
        "sl_cvr_col":   c_slcvr  or "(missing)",
        "int_days_col": c_intd   or "(missing)",
        "prd_days_col": c_prdd   or "(missing)",
        "status_col":   c_status or "(missing — ST_STATUS will be NULL)",
        "days_components": day_cols or "(none — `days` is NULL)",
    })
    logger.info(
        f"[onesize] {len(stores_df)} stores loaded "
        f"(rdc col={st_rdc}, days from {day_cols or 'none'})"
    )

    if stores_df.empty:
        return {
            "sequence_id": sequence_id,
            "total_rows": 0,
            "columns": [],
            "rows": [],
            "stores": 0,
            "stages": stages,
        }

    # ---- Stage 7: join MSA rows with stores -----------------------------
    # Per business rule: a store only receives products from its assigned RDC,
    # so we INNER JOIN on RDC instead of a full cartesian product. Without an
    # RDC column on one side we fall back to cartesian and surface a warning
    # in the stage so it's visible in the UI.
    msa_rdc = _resolve_col(list(msa_df.columns), ["RDC", "ST_CD"])

    join_mode: str
    rdc_diag: Dict[str, Any] = {}
    # Normalize the user's selection (empty = "all RDCs")
    selected_rdcs = {str(r).strip().upper() for r in (rdcs or []) if str(r).strip()}
    if msa_rdc and st_rdc and "RDC" in stores_df.columns:
        # Build the join key BEFORE dropping any source columns so we don't
        # blow away the column msa_rdc points at (especially when msa_rdc='ST_CD').
        msa_df["__RDC_KEY"] = msa_df[msa_rdc].astype(str).str.strip().str.upper()
        stores_df["__RDC_KEY"] = stores_df["RDC"].astype(str).str.strip().str.upper()

        # Drop ST_CD / ST_NM from MSA side so the store master is authoritative
        # on those columns post-join. Done after __RDC_KEY is materialised.
        drop_existing = [c for c in ("ST_CD", "ST_NM") if c in msa_df.columns and c != majcat_col]
        if drop_existing:
            msa_df = msa_df.drop(columns=drop_existing)
        # Also drop MSA's RDC column — the store-side one is the join authority
        # and will keep its name `RDC` thanks to the suffixes trick below.
        if "RDC" in msa_df.columns:
            msa_df = msa_df.drop(columns=["RDC"])

        # Apply user RDC filter (if any). Empty selection = no restriction.
        msa_before  = len(msa_df)
        stores_before = len(stores_df)
        if selected_rdcs:
            msa_df = msa_df[msa_df["__RDC_KEY"].isin(selected_rdcs)].copy()
            stores_df = stores_df[stores_df["__RDC_KEY"].isin(selected_rdcs)].copy()
        # Surface this as its own stage so the UI clearly shows the user's
        # selection narrowed BOTH sides before the join. When nothing was
        # picked we keep both sides intact and the inner join still ensures
        # an MSA row only multiplies with stores of its own RDC.
        stages.append({
            "stage": "apply_rdc_filter",
            "selected": sorted(selected_rdcs) if selected_rdcs else "ALL",
            "msa_rows_in":  msa_before,
            "msa_rows_out": len(msa_df),
            "stores_in":    stores_before,
            "stores_out":   len(stores_df),
        })
        logger.info(
            f"[onesize] RDC filter — selected={sorted(selected_rdcs) if selected_rdcs else 'ALL'} | "
            f"msa {msa_before}→{len(msa_df)} | stores {stores_before}→{len(stores_df)}"
        )

        # ---- Diagnostics: what RDCs exist on each side & how they overlap
        msa_rdcs = set(msa_df["__RDC_KEY"].dropna().unique())
        store_rdcs = set(stores_df["__RDC_KEY"].dropna().unique())
        matched = msa_rdcs & store_rdcs
        msa_only = sorted(msa_rdcs - store_rdcs)
        store_only = sorted(store_rdcs - msa_rdcs)

        # Stores per matched RDC (only those that survived the inner join)
        stores_per_rdc = (
            stores_df[stores_df["__RDC_KEY"].isin(matched)]
            .groupby("__RDC_KEY")["ST_CD"]
            .nunique()
            .sort_index()
            .to_dict()
        )

        # Log lines — visible in backend stdout/log file
        logger.info(
            f"[onesize] RDC diagnostic — MSA RDCs={len(msa_rdcs)}, "
            f"Store RDCs={len(store_rdcs)}, matched={len(matched)}, "
            f"msa_only={msa_only or '[]'}, store_only={store_only or '[]'}"
        )
        for r, n in sorted(stores_per_rdc.items()):
            logger.info(f"[onesize]   RDC={r}: {n} stores")
        # Also print to stdout for live tail visibility
        print(
            f"[onesize] RDCs matched={len(matched)}/{len(store_rdcs)} | "
            f"per-RDC stores={dict(stores_per_rdc)}"
        )

        rdc_diag = {
            "msa_rdc_count": len(msa_rdcs),
            "store_rdc_count": len(store_rdcs),
            "matched_rdc_count": len(matched),
            "matched_rdcs": sorted(matched),
            "msa_only_rdcs": msa_only,
            "store_only_rdcs": store_only,
            "stores_per_matched_rdc": {str(k): int(v) for k, v in stores_per_rdc.items()},
            "selected_rdcs": sorted(selected_rdcs) if selected_rdcs else "ALL",
        }

        # `suffixes=("", "_msa")`: any column present on both sides keeps the
        # store-side name unchanged and the MSA-side copy gets `_msa`. We then
        # drop those `_msa` columns, so `RDC` survives as a clean store-side
        # column in the result.
        cross = stores_df.merge(
            msa_df, on="__RDC_KEY", how="inner", suffixes=("", "_msa")
        )
        msa_dupes = [c for c in cross.columns if c.endswith("_msa")]
        if msa_dupes:
            cross = cross.drop(columns=msa_dupes)
        # Guarantee an RDC column on the result — if it somehow got dropped
        # upstream, reconstruct it from the canonical join key. This is the
        # final safety net so "RDC is missing from the result" can't happen.
        if "RDC" not in cross.columns:
            cross["RDC"] = cross["__RDC_KEY"]
        cross = cross.drop(columns=["__RDC_KEY"])
        join_mode = "inner_on_rdc"
    else:
        # RDC unavailable — degrade to cartesian, but tell the user.
        drop_existing = [c for c in ("ST_CD", "ST_NM") if c in msa_df.columns and c != majcat_col]
        if drop_existing:
            msa_df = msa_df.drop(columns=drop_existing)
        msa_df["_join"] = 1
        stores_df["_join"] = 1
        cross = stores_df.merge(
            msa_df, on="_join", suffixes=("", "_msa")
        ).drop(columns=["_join"])
        msa_dupes = [c for c in cross.columns if c.endswith("_msa")]
        if msa_dupes:
            cross = cross.drop(columns=msa_dupes)
        join_mode = "cartesian_fallback"
        logger.warning(
            f"[onesize] RDC column missing (msa={msa_rdc!r}, store={st_rdc!r}) "
            f"— falling back to cartesian join"
        )

    stages.append({
        "stage": "cross_join",
        "rows": len(cross),
        "stores": len(stores_df),
        "mode": join_mode,
        **rdc_diag,
    })
    logger.info(f"[onesize] joined ({join_mode}) → {len(cross)} rows")

    # ---- Stage 7b: tag final_msa='Y' (no filter needed) -----------------
    # All filtering happened pre-cross-join (Stages 5 + 5b): we dropped
    # applicable_sz='N' and row_or=FALSE rows. Every cross row here is
    # already final_msa='Y' by construction. ST_STATUS is loaded only for
    # display; it no longer drives any filter.
    cross["final_msa"] = "Y"
    stages.append({
        "stage": "tag_final_msa_y",
        "rows":  int(len(cross)),
        "note":  "filter applied pre-cross-join (Stages 5 + 5b); all rows pass",
    })

    # Early-exit if everything was filtered out — downstream stages assume rows.
    if cross.empty:
        return {
            "sequence_id": sequence_id,
            "total_rows": 0,
            "columns": list(cross.columns),
            "rows": [],
            "stores": int(len(stores_df)),
            "stages": stages,
            "_df": cross,
        }

    # ---- Stage 8: enrich with `cont` from Master_CONT_SZ ----------------
    # Match key: (ST_CD, MAJ_CAT/MAJCAT, SZ). LEFT JOIN so rows with no
    # contribution row survive — they get cont=NULL.
    try:
        cont_cols = _table_columns(db, CONT_SZ_TABLE)
    except Exception as e:
        cont_cols = []
        logger.warning(f"[onesize] could not introspect {CONT_SZ_TABLE}: {e}")

    if cont_cols:
        c_stcd = _resolve_col(cont_cols, ["ST_CD", "WERKS", "STORE_CODE"])
        c_maj  = _resolve_col(cont_cols, ["MAJ_CAT", "MAJCAT"])
        c_sz   = _resolve_col(cont_cols, ["SZ", "SIZE", "SZ_CD", "SZ_CODE"])
        c_cont = _resolve_col(cont_cols, ["CONT", "CONTRIBUTION", "CONT_PCT", "CONT_PERCENT"])

        missing_cont = [
            name for name, v in (("ST_CD", c_stcd), ("MAJ_CAT", c_maj), ("SZ", c_sz), ("CONT", c_cont))
            if not v
        ]
        if missing_cont:
            stages.append({
                "stage": "enrich_cont",
                "skipped": True,
                "reason": f"{CONT_SZ_TABLE} missing columns: {missing_cont}",
                "available_cols": cont_cols,
            })
            cross["cont"] = None
            logger.warning(
                f"[onesize] cont enrichment skipped — {CONT_SZ_TABLE} missing {missing_cont}"
            )
        else:
            cont_df = pd.read_sql(
                text(
                    f"SELECT [{c_stcd}] AS ST_CD, [{c_maj}] AS MAJ_CAT, "
                    f"[{c_sz}] AS SZ, [{c_cont}] AS cont "
                    f"FROM [{CONT_SZ_TABLE}]"
                ),
                db.bind,
            )
            # Normalize keys on both sides (trim + upper) — same approach we use
            # for the RDC join so trailing spaces / case mismatches still hit.
            cross["__ST_KEY"]  = cross["ST_CD"].astype(str).str.strip().str.upper()
            cross["__MAJ_KEY"] = cross[majcat_col].astype(str).str.strip().str.upper()
            cross["__SZ_KEY"]  = (
                cross[sz_col].astype(str).str.strip().str.upper()
                if sz_col and sz_col in cross.columns
                else cross.get("SZ", pd.Series([""] * len(cross))).astype(str).str.strip().str.upper()
            )
            cont_df["__ST_KEY"]  = cont_df["ST_CD"].astype(str).str.strip().str.upper()
            cont_df["__MAJ_KEY"] = cont_df["MAJ_CAT"].astype(str).str.strip().str.upper()
            cont_df["__SZ_KEY"]  = cont_df["SZ"].astype(str).str.strip().str.upper()
            # Keep only the join keys + cont; drop the raw renamed cols so the
            # merge doesn't pull in duplicate ST_CD / SZ / MAJ_CAT.
            cont_lookup = cont_df[["__ST_KEY", "__MAJ_KEY", "__SZ_KEY", "cont"]]

            before_cols = set(cross.columns)
            cross = cross.merge(
                cont_lookup, on=["__ST_KEY", "__MAJ_KEY", "__SZ_KEY"], how="left",
            )
            cross = cross.drop(columns=["__ST_KEY", "__MAJ_KEY", "__SZ_KEY"])

            matched = int(cross["cont"].notna().sum())
            stages.append({
                "stage": "enrich_cont",
                "lookup_rows": int(len(cont_df)),
                "matched_rows": matched,
                "unmatched_rows": int(len(cross) - matched),
                "join_keys": ["ST_CD", "MAJ_CAT", "SZ"],
            })
            logger.info(
                f"[onesize] cont enrichment — {matched}/{len(cross)} rows matched "
                f"from {len(cont_df)} contribution rows"
            )
    else:
        cross["cont"] = None
        stages.append({"stage": "enrich_cont", "skipped": True, "reason": f"{CONT_SZ_TABLE} not found"})

    # ---- Stage 9: enrich with DISP_Q, ALC_D, SAL_PD_SZ from ARS_CALC_ST_MAJ_CAT
    # Join key: (ST_CD, MAJ_CAT). SAL_PD_SZ is the maj_cat-grain per-day sale
    # rate; it's used by Stage 10 to compute MBQ_SZ. The option-grain SAL_PD
    # (from MASTER_GEN_ART_SALE) is loaded separately in Stage 9a and used
    # downstream for AUTO_GEN_ART_SALE / PER_OPT_SALE.
    #   MBQ_SZ = (DISP_Q + SAL_PD_SZ * days) * cont
    try:
        calc_cols = _table_columns(db, CALC_ST_MAJ_CAT_TABLE)
    except Exception as e:
        calc_cols = []
        logger.warning(f"[onesize] could not introspect {CALC_ST_MAJ_CAT_TABLE}: {e}")

    if calc_cols:
        k_stcd  = _resolve_col(calc_cols, ["ST_CD", "WERKS", "STORE_CODE"])
        k_maj   = _resolve_col(calc_cols, ["MAJ_CAT", "MAJCAT"])
        c_disp  = _resolve_col(calc_cols, ["DISP_Q", "DESP_Q", "DISPATCH_Q", "DISP_QTY"])
        c_alcd  = _resolve_col(calc_cols, ["ALC_D", "ALC_DAYS", "ALLOC_D"])
        c_salpd = _resolve_col(calc_cols, ["SAL_PD", "SAL_PER_DAY", "SAL_PERDAY"])

        missing = [
            n for n, v in (("ST_CD", k_stcd), ("MAJ_CAT", k_maj),
                           ("DISP_Q", c_disp)) if not v
        ]
        if missing:
            stages.append({
                "stage": "enrich_calc_maj_cat",
                "skipped": True,
                "reason": f"{CALC_ST_MAJ_CAT_TABLE} missing columns: {missing}",
                "available_cols": calc_cols,
            })
            cross["DISP_Q"]    = None
            cross["SAL_PD_SZ"] = 0.0
            cross["SAL_PD"]    = 0.0       # initialise so Stage 9a can overwrite
            cross["ALC_D"]     = None
            logger.warning(
                f"[onesize] DISP_Q/ALC_D/SAL_PD_SZ enrichment skipped — missing {missing}"
            )
        else:
            alcd_select  = f", [{c_alcd}] AS ALC_D"      if c_alcd  else ", CAST(NULL AS FLOAT) AS ALC_D"
            salpd_select = f", [{c_salpd}] AS SAL_PD_SZ" if c_salpd else ", CAST(0 AS FLOAT) AS SAL_PD_SZ"
            calc_df = pd.read_sql(
                text(
                    f"SELECT [{k_stcd}] AS ST_CD, [{k_maj}] AS MAJ_CAT, "
                    f"[{c_disp}] AS DISP_Q{alcd_select}{salpd_select} "
                    f"FROM [{CALC_ST_MAJ_CAT_TABLE}]"
                ),
                db.bind,
            )
            calc_df["__ST_KEY"]  = calc_df["ST_CD"].astype(str).str.strip().str.upper()
            calc_df["__MAJ_KEY"] = calc_df["MAJ_CAT"].astype(str).str.strip().str.upper()
            cross["__ST_KEY"]   = cross["ST_CD"].astype(str).str.strip().str.upper()
            cross["__MAJ_KEY"]  = cross[majcat_col].astype(str).str.strip().str.upper()
            calc_lookup = calc_df[["__ST_KEY", "__MAJ_KEY", "DISP_Q", "ALC_D", "SAL_PD_SZ"]]

            cross = cross.merge(
                calc_lookup, on=["__ST_KEY", "__MAJ_KEY"], how="left",
            ).drop(columns=["__ST_KEY", "__MAJ_KEY"])

            # Non-matching rows get NaN for SAL_PD_SZ; fill with 0 so the MBQ_SZ
            # arithmetic in Stage 10 doesn't propagate NaN through.
            cross["SAL_PD_SZ"] = pd.to_numeric(cross["SAL_PD_SZ"], errors="coerce").fillna(0)

            # SAL_PD (option-grain) is populated by Stage 9a from MASTER_GEN_ART_SALE.
            cross["SAL_PD"] = 0.0

            matched_q = int(cross["DISP_Q"].notna().sum())
            stages.append({
                "stage": "enrich_calc_maj_cat",
                "lookup_rows":   int(len(calc_df)),
                "matched_rows":  matched_q,
                "unmatched_rows": int(len(cross) - matched_q),
                "join_keys":     ["ST_CD", "MAJ_CAT"],
                "loads":         ["DISP_Q", "ALC_D", "SAL_PD_SZ"],
                "alc_d_col":     c_alcd  or "(missing)",
                "sal_pd_col":    c_salpd or "(missing — SAL_PD_SZ defaulted to 0)",
            })
            logger.info(
                f"[onesize] DISP_Q/ALC_D/SAL_PD_SZ enrichment — "
                f"{matched_q}/{len(cross)} rows matched from {len(calc_df)} calc rows "
                f"(alc_d col={c_alcd}; sal_pd col={c_salpd}; "
                f"option-grain SAL_PD still comes from MASTER_GEN_ART_SALE in Stage 9a)"
            )
    else:
        cross["DISP_Q"]    = None
        cross["SAL_PD_SZ"] = 0.0
        cross["SAL_PD"]    = 0.0
        cross["ALC_D"]     = None
        stages.append({
            "stage": "enrich_calc_maj_cat",
            "skipped": True,
            "reason": f"{CALC_ST_MAJ_CAT_TABLE} not found",
        })

    # ---- Stage 9a: LOAD SAL_PD strictly from MASTER_GEN_ART_SALE -------
    # MASTER_GEN_ART_SALE is the ONLY source of SAL_PD now. Stage 9 above
    # initialised cross["SAL_PD"] = 0 — this stage overwrites where the 4-key
    # match succeeds, else keeps 0. ARS_CALC_ST_MAJ_CAT.SAL_PD is not read.
    cross["SAL_PD"] = 0.0   # belt-and-braces — Stage 9 already set this
    mgas_diag: Dict[str, Any] = {"stage": "enrich_sal_pd_option_grain", "table": MGAS_TABLE}
    try:
        mgas_cols = _table_columns(db, MGAS_TABLE)
    except Exception as e:
        mgas_cols = []
        logger.warning(f"[onesize] could not introspect {MGAS_TABLE}: {e}")

    if not mgas_cols:
        mgas_diag.update({"skipped": True, "reason": f"{MGAS_TABLE} not found — all SAL_PD = 0"})
        stages.append(mgas_diag)
    else:
        m_stcd = _resolve_col(mgas_cols, ["ST_CD", "WERKS", "STORE_CODE"])
        m_maj  = _resolve_col(mgas_cols, ["MAJ_CAT", "MAJCAT"])
        m_gen  = _resolve_col(mgas_cols, ["GEN_ART_NUMBER", "GEN_ART", "GEN_ART_NO"])
        m_clr  = _resolve_col(mgas_cols, ["CLR", "COLOR", "COLOUR"])
        m_sp   = _resolve_col(mgas_cols, ["SAL_PD", "SAL_PER_DAY", "SAL_PERDAY"])

        missing_m = [
            n for n, v in (("ST_CD", m_stcd), ("MAJ_CAT", m_maj),
                           ("GEN_ART_NUMBER", m_gen), ("CLR", m_clr),
                           ("SAL_PD", m_sp)) if not v
        ]
        if missing_m:
            mgas_diag.update({
                "skipped": True,
                "reason": f"{MGAS_TABLE} missing cols: {missing_m}",
                "available_cols_sample": mgas_cols[:30],
            })
            stages.append(mgas_diag)
        else:
            try:
                # PERFORMANCE: filter by ST_CDs from cross (small, indexable set)
                # instead of MAJCATs with UPPER/TRIM (kills index usage).
                unique_stores = (
                    cross["ST_CD"].dropna().astype(str).str.strip().unique().tolist()
                )
                mgas_params: Dict[str, Any] = {}
                mgas_where = f" WHERE [{m_sp}] IS NOT NULL"
                if unique_stores and len(unique_stores) <= 1800:
                    in_keys = []
                    for i, s in enumerate(unique_stores):
                        k = f"st{i}"
                        in_keys.append(f":{k}")
                        mgas_params[k] = s
                    mgas_where += f" AND [{m_stcd}] IN ({', '.join(in_keys)})"
                mgas_df = pd.read_sql(
                    text(
                        f"SELECT [{m_stcd}] AS ST_CD, [{m_maj}] AS MAJ_CAT, "
                        f"[{m_gen}] AS GEN_ART, [{m_clr}] AS CLR, "
                        f"TRY_CAST([{m_sp}] AS FLOAT) AS SAL_PD_OPT "
                        f"FROM [{MGAS_TABLE}]{mgas_where}"
                    ),
                    db.bind, params=mgas_params,
                )
                mgas_df["__ST_KEY"]  = _norm_str(mgas_df["ST_CD"])
                mgas_df["__MAJ_KEY"] = _norm_str(mgas_df["MAJ_CAT"])
                mgas_df["__GEN_KEY"] = _norm_num_or_str(mgas_df["GEN_ART"])
                mgas_df["__CLR_KEY"] = _norm_str(mgas_df["CLR"])
                # Collapse possible duplicates with MAX.
                mgas_lookup = (
                    mgas_df.groupby(
                        ["__ST_KEY", "__MAJ_KEY", "__GEN_KEY", "__CLR_KEY"], dropna=False
                    )["SAL_PD_OPT"].max().reset_index()
                )
                cross["__ST_KEY"]  = _norm_str(cross["ST_CD"])
                cross["__MAJ_KEY"] = _norm_str(cross[majcat_col])
                cross["__GEN_KEY"] = _norm_num_or_str(cross[gen_art_col])
                cross["__CLR_KEY"] = _norm_str(cross[clr_col])
                cross = cross.merge(
                    mgas_lookup,
                    on=["__ST_KEY", "__MAJ_KEY", "__GEN_KEY", "__CLR_KEY"],
                    how="left",
                ).drop(columns=["__ST_KEY", "__MAJ_KEY", "__GEN_KEY", "__CLR_KEY"])

                # Strict option-grain — fill with 0 where no match.
                opt_sp = pd.to_numeric(cross["SAL_PD_OPT"], errors="coerce")
                replaced     = int(opt_sp.notna().sum())
                filled_zero  = int(opt_sp.isna().sum())
                cross["SAL_PD"] = opt_sp.fillna(0)
                cross = cross.drop(columns=["SAL_PD_OPT"])

                mgas_diag.update({
                    "lookup_rows":          int(len(mgas_lookup)),
                    "replaced_with_option": replaced,
                    "filled_zero_no_match": filled_zero,
                    "join_keys":            ["ST_CD", "MAJ_CAT", "GEN_ART", "CLR"],
                    "source_col":           m_sp,
                    "fallback_policy":      "no maj_cat fallback — missing → 0",
                })
                stages.append(mgas_diag)
                logger.info(
                    f"[onesize] SAL_PD set from {MGAS_TABLE}.{m_sp} — "
                    f"{replaced}/{len(cross)} rows from option-grain, "
                    f"{filled_zero} filled with 0 (no match)"
                )
            except Exception as e:
                mgas_diag.update({"error": str(e)[:500]})
                stages.append(mgas_diag)
                logger.warning(f"[onesize] {MGAS_TABLE} SAL_PD overwrite failed: {e}")

    # ---- Stage 9b: enrich with ACS_D from ARS_GRID_MJ (maj_cat-grain) ----
    # Join keys: (WERKS=ST_CD, MAJ_CAT). After the join we compute the new
    # `var_art_disp` column as ACS_D * cont (size-level contribution).
    try:
        gmj_cols = _table_columns(db, GRID_MJ_BASE_TABLE)
    except Exception as e:
        gmj_cols = []
        logger.warning(f"[onesize] could not introspect {GRID_MJ_BASE_TABLE}: {e}")

    if gmj_cols:
        gm_stcd = _resolve_col(gmj_cols, ["WERKS", "ST_CD", "STORE_CODE"])
        gm_maj  = _resolve_col(gmj_cols, ["MAJ_CAT", "MAJCAT"])
        gm_acs  = _resolve_col(gmj_cols, ["ACS_D", "MJ_ACS_D", "ACS_DAYS", "ACSD"])

        missing_gm = [
            n for n, v in (("WERKS/ST_CD", gm_stcd), ("MAJ_CAT", gm_maj), ("ACS_D", gm_acs))
            if not v
        ]
        if missing_gm:
            stages.append({
                "stage": "enrich_grid_mj_base",
                "skipped": True,
                "reason": f"{GRID_MJ_BASE_TABLE} missing columns: {missing_gm}",
                "available_cols": gmj_cols,
            })
            cross["ACS_D"] = None
            logger.warning(
                f"[onesize] ACS_D enrichment skipped — {GRID_MJ_BASE_TABLE} missing {missing_gm}"
            )
        else:
            gmj_df = pd.read_sql(
                text(
                    f"SELECT [{gm_stcd}] AS ST_CD, [{gm_maj}] AS MAJ_CAT, "
                    f"[{gm_acs}] AS ACS_D FROM [{GRID_MJ_BASE_TABLE}]"
                ),
                db.bind,
            )
            gmj_df["__ST_KEY"]  = gmj_df["ST_CD"].astype(str).str.strip().str.upper()
            gmj_df["__MAJ_KEY"] = gmj_df["MAJ_CAT"].astype(str).str.strip().str.upper()
            cross["__ST_KEY"]  = cross["ST_CD"].astype(str).str.strip().str.upper()
            cross["__MAJ_KEY"] = cross[majcat_col].astype(str).str.strip().str.upper()
            # If the source has more than one row per (store, maj_cat), take the
            # max so the join stays 1:1 with the result grain.
            gmj_lookup = (
                gmj_df.groupby(["__ST_KEY", "__MAJ_KEY"], dropna=False)["ACS_D"]
                .apply(lambda s: pd.to_numeric(s, errors="coerce").max())
                .reset_index()
            )
            cross = cross.merge(
                gmj_lookup, on=["__ST_KEY", "__MAJ_KEY"], how="left",
            ).drop(columns=["__ST_KEY", "__MAJ_KEY"])

            matched_a = int(cross["ACS_D"].notna().sum())
            stages.append({
                "stage": "enrich_grid_mj_base",
                "table":         GRID_MJ_BASE_TABLE,
                "lookup_rows":   int(len(gmj_df)),
                "unique_keys":   int(len(gmj_lookup)),
                "matched_rows":  matched_a,
                "unmatched_rows": int(len(cross) - matched_a),
                "join_keys":     ["ST_CD(=WERKS)", "MAJ_CAT"],
            })
            logger.info(
                f"[onesize] ACS_D enrichment from {GRID_MJ_BASE_TABLE} — "
                f"{matched_a}/{len(cross)} rows matched"
            )
    else:
        cross["ACS_D"] = None
        stages.append({
            "stage": "enrich_grid_mj_base",
            "skipped": True,
            "reason": f"{GRID_MJ_BASE_TABLE} not found",
        })

    # ---- Stage 9c: compute var_art_disp = ACS_D * cont ------------------
    acs_n  = pd.to_numeric(cross.get("ACS_D"), errors="coerce")
    cont_v = pd.to_numeric(cross.get("cont"),  errors="coerce")
    cross["var_art_disp"] = acs_n * cont_v   # NaN propagates if either side is NULL
    vad_computed = int(cross["var_art_disp"].notna().sum())
    stages.append({
        "stage": "compute_var_art_disp",
        "rows":          int(len(cross)),
        "computed_rows": vad_computed,
        "null_rows":     int(len(cross) - vad_computed),
        "formula":       "ACS_D * cont",
    })
    logger.info(
        f"[onesize] var_art_disp computed for {vad_computed}/{len(cross)} rows"
    )

    # ---- Stage 10: compute MBQ_SZ = (DISP_Q + SAL_PD_SZ * days) * cont -----
    # SAL_PD_SZ is the maj_cat-grain per-day sale loaded from ARS_CALC_ST_MAJ_CAT
    # in Stage 9. Treat NULLs as 0 for the arithmetic, but if cont is NULL we
    # leave MBQ_SZ NULL — there's no meaningful MBQ without a contribution %.
    disp_n  = pd.to_numeric(cross.get("DISP_Q"),    errors="coerce").fillna(0)
    sal_sz  = pd.to_numeric(cross.get("SAL_PD_SZ"), errors="coerce").fillna(0)
    days_n  = pd.to_numeric(cross.get("days"),      errors="coerce").fillna(0)
    cont_n  = pd.to_numeric(cross.get("cont"),      errors="coerce")
    mbq     = (disp_n + sal_sz * days_n) * cont_n
    cross["MBQ_SZ"] = mbq  # NaN where cont was NULL — preserved through to CSV
    computed = int(cross["MBQ_SZ"].notna().sum())
    stages.append({
        "stage": "compute_mbq_sz",
        "rows":           int(len(cross)),
        "computed_rows":  computed,
        "null_rows":      int(len(cross) - computed),
        "formula":        "(DISP_Q + SAL_PD_SZ * days) * cont",
        "sal_pd_source":  f"{CALC_ST_MAJ_CAT_TABLE}.SAL_PD (joined as SAL_PD_SZ on ST_CD+MAJ_CAT)",
    })
    logger.info(
        f"[onesize] MBQ_SZ computed for {computed}/{len(cross)} rows "
        f"(nulls where cont is missing; SAL_PD_SZ from {CALC_ST_MAJ_CAT_TABLE})"
    )

    # ---- Stage 11: enrich with STK_TTL from ARS_GRID_MJ_VAR_ART ---------
    # Join keys: (WERKS=ST_CD, ARTICLE_NUMBER) — 2-key store × article grain.
    # ARTICLE_NUMBER comes from ARS_MSA_TOTAL (already in `cross`).
    # LEFT JOIN — non-matching rows get STK_TTL = NULL (treated as 0 downstream).
    try:
        grid_cols = _table_columns(db, GRID_MJ_TABLE)
    except Exception as e:
        grid_cols = []
        logger.warning(f"[onesize] could not introspect {GRID_MJ_TABLE}: {e}")

    if grid_cols:
        g_stcd = _resolve_col(grid_cols, ["WERKS", "ST_CD", "STORE_CODE"])
        g_art  = _resolve_col(grid_cols, ["ARTICLE_NUMBER", "ARTICLE_NO", "VAR_ART", "MATNR"])
        g_maj  = _resolve_col(grid_cols, ["MAJ_CAT", "MAJCAT"])   # optional — used only for prefilter
        g_stk  = _resolve_col(grid_cols, ["STK_TTL", "MJ_STK_TTL", "STK_TOTAL", "TOTAL_STK"])

        # Resolve ARTICLE_NUMBER on the cross side too.
        cross_art_col = _resolve_col(list(cross.columns), ["ARTICLE_NUMBER", "ARTICLE_NO"])

        missing = [
            n for n, v in (
                ("WERKS/ST_CD",   g_stcd),
                ("ARTICLE_NUMBER (grid)", g_art),
                ("STK_TTL",       g_stk),
                ("ARTICLE_NUMBER (cross)", cross_art_col),
            ) if not v
        ]
        if missing:
            stages.append({
                "stage": "enrich_grid_mj",
                "skipped": True,
                "reason": f"missing columns: {missing}",
                "available_grid_cols_sample": grid_cols[:30],
            })
            cross["STK_TTL"] = None
            logger.warning(f"[onesize] STK_TTL enrichment skipped — {missing}")
        else:
            # PERFORMANCE: aggregate server-side. The variant × size grain of
            # ARS_GRID_MJ_VAR_ART can be many millions of rows — pulling the
            # raw table over the network and aggregating in pandas is what
            # was killing this stage. SUM in SQL with GROUP BY returns at most
            # (stores × articles) rows.
            unique_stores = (
                cross["ST_CD"].dropna().astype(str).str.strip().unique().tolist()
            )
            stk_params: Dict[str, Any] = {}
            stk_where = ""
            if unique_stores and len(unique_stores) <= 1800:   # SQL param limit safety
                in_keys = []
                for i, s in enumerate(unique_stores):
                    k = f"st{i}"
                    in_keys.append(f":{k}")
                    stk_params[k] = s
                stk_where = f" WHERE [{g_stcd}] IN ({', '.join(in_keys)})"
            grid_df = pd.read_sql(
                text(
                    f"SELECT [{g_stcd}] AS ST_CD, [{g_art}] AS ARTICLE_NUMBER, "
                    f"SUM(TRY_CAST([{g_stk}] AS FLOAT)) AS STK_TTL "
                    f"FROM [{GRID_MJ_TABLE}]{stk_where} "
                    f"GROUP BY [{g_stcd}], [{g_art}]"
                ),
                db.bind, params=stk_params,
            )
            # Normalize the 2 join keys (SQL already did the SUM aggregation).
            grid_df["__ST_KEY"]  = _norm_str(grid_df["ST_CD"])
            grid_df["__ART_KEY"] = _norm_num_or_str(grid_df["ARTICLE_NUMBER"])
            cross["__ST_KEY"]   = _norm_str(cross["ST_CD"])
            cross["__ART_KEY"]  = _norm_num_or_str(cross[cross_art_col])

            grid_lookup = grid_df[["__ST_KEY", "__ART_KEY", "STK_TTL"]]
            # Defensive: in case the SQL GROUP BY produced duplicate keys due
            # to whitespace/casing differences, collapse one more time.
            grid_lookup = (
                grid_lookup.groupby(["__ST_KEY", "__ART_KEY"], dropna=False)["STK_TTL"]
                .sum().reset_index()
            )

            cross = cross.merge(
                grid_lookup, on=["__ST_KEY", "__ART_KEY"], how="left",
            ).drop(columns=["__ST_KEY", "__ART_KEY"])

            matched_g = int(cross["STK_TTL"].notna().sum())
            stages.append({
                "stage": "enrich_grid_mj",
                "table":         GRID_MJ_TABLE,
                "lookup_rows":   int(len(grid_df)),
                "unique_keys":   int(len(grid_lookup)),
                "matched_rows":  matched_g,
                "unmatched_rows": int(len(cross) - matched_g),
                "join_keys":     ["ST_CD(=WERKS)", "ARTICLE_NUMBER"],
                "grid_article_col":  g_art,
                "cross_article_col": cross_art_col,
            })
            logger.info(
                f"[onesize] STK_TTL enrichment from {GRID_MJ_TABLE} — "
                f"{matched_g}/{len(cross)} rows matched "
                f"({len(grid_lookup)} unique (store,article) tuples from {len(grid_df)} grid rows; "
                f"grid_article_col={g_art})"
            )
    else:
        cross["STK_TTL"] = None
        stages.append({
            "stage": "enrich_grid_mj",
            "skipped": True,
            "reason": f"{GRID_MJ_TABLE} not found",
        })

    # ---- Stage 11b: enrich with STK_TTL_SZ from same table at size grain --
    # Same table (ARS_GRID_MJ_VAR_ART) but joined on (ST_CD, MAJ_CAT, SZ),
    # SUM-aggregated server-side. STK_TTL_SZ is used by REQ; STK_TTL (option
    # grain) is used by VAR_REQ.
    try:
        sz_grid_cols = _table_columns(db, GRID_MJ_TABLE)
    except Exception as e:
        sz_grid_cols = []
        logger.warning(f"[onesize] could not introspect {GRID_MJ_TABLE} (sz grain): {e}")

    if sz_grid_cols:
        sg_stcd = _resolve_col(sz_grid_cols, ["WERKS", "ST_CD", "STORE_CODE"])
        sg_maj  = _resolve_col(sz_grid_cols, ["MAJ_CAT", "MAJCAT"])
        sg_sz   = _resolve_col(sz_grid_cols, ["SZ", "SIZE", "SZ_CD", "SZ_CODE"])
        sg_stk  = _resolve_col(sz_grid_cols, ["STK_TTL", "MJ_STK_TTL", "STK_TOTAL", "TOTAL_STK"])

        missing_sz = [
            n for n, v in (
                ("WERKS/ST_CD", sg_stcd),
                ("MAJ_CAT",     sg_maj),
                ("SZ",          sg_sz),
                ("STK_TTL",     sg_stk),
            ) if not v
        ]
        if missing_sz:
            stages.append({
                "stage": "enrich_grid_mj_sz",
                "skipped": True,
                "reason": f"{GRID_MJ_TABLE} missing columns for size grain: {missing_sz}",
            })
            cross["STK_TTL_SZ"] = None
        else:
            unique_stores = (
                cross["ST_CD"].dropna().astype(str).str.strip().unique().tolist()
            )
            sz_params: Dict[str, Any] = {}
            sz_where = ""
            if unique_stores and len(unique_stores) <= 1800:
                in_keys = []
                for i, s in enumerate(unique_stores):
                    k = f"st{i}"
                    in_keys.append(f":{k}")
                    sz_params[k] = s
                sz_where = f" WHERE [{sg_stcd}] IN ({', '.join(in_keys)})"
            sz_grid_df = pd.read_sql(
                text(
                    f"SELECT [{sg_stcd}] AS ST_CD, [{sg_maj}] AS MAJ_CAT, "
                    f"[{sg_sz}] AS SZ, "
                    f"SUM(TRY_CAST([{sg_stk}] AS FLOAT)) AS STK_TTL_SZ "
                    f"FROM [{GRID_MJ_TABLE}]{sz_where} "
                    f"GROUP BY [{sg_stcd}], [{sg_maj}], [{sg_sz}]"
                ),
                db.bind, params=sz_params,
            )
            sz_grid_df["__ST_KEY"]  = _norm_str(sz_grid_df["ST_CD"])
            sz_grid_df["__MAJ_KEY"] = _norm_str(sz_grid_df["MAJ_CAT"])
            sz_grid_df["__SZ_KEY"]  = _norm_str(sz_grid_df["SZ"])
            cross["__ST_KEY"]   = _norm_str(cross["ST_CD"])
            cross["__MAJ_KEY"]  = _norm_str(cross[majcat_col])
            cross["__SZ_KEY"]   = (
                _norm_str(cross[sz_col])
                if sz_col and sz_col in cross.columns
                else _norm_str(cross.get("SZ", pd.Series([""] * len(cross))))
            )
            sz_keys = ["__ST_KEY", "__MAJ_KEY", "__SZ_KEY"]
            sz_lookup = sz_grid_df[sz_keys + ["STK_TTL_SZ"]]
            cross = cross.merge(sz_lookup, on=sz_keys, how="left").drop(columns=sz_keys)
            matched_sz = int(cross["STK_TTL_SZ"].notna().sum())
            stages.append({
                "stage": "enrich_grid_mj_sz",
                "table":        GRID_MJ_TABLE,
                "lookup_rows":  int(len(sz_grid_df)),
                "matched_rows": matched_sz,
                "unmatched_rows": int(len(cross) - matched_sz),
                "join_keys":    ["ST_CD(=WERKS)", "MAJ_CAT", "SZ"],
            })
            logger.info(
                f"[onesize] STK_TTL_SZ enrichment (size-grain) — "
                f"{matched_sz}/{len(cross)} rows matched from {len(sz_grid_df)} aggregated rows"
            )
    else:
        cross["STK_TTL_SZ"] = None

    # ---- Stage 12: compute REQ = MAX(MBQ_SZ - STK_TTL_SZ, 0) -------------
    # REQ uses the size-grain stock (STK_TTL_SZ) since MBQ_SZ is size-scaled.
    # VAR_REQ still uses STK_TTL (option grain). NULLs treated as 0.
    mbq_n = pd.to_numeric(cross.get("MBQ_SZ"),     errors="coerce")
    stk_n = pd.to_numeric(cross.get("STK_TTL_SZ"), errors="coerce").fillna(0)
    req   = (mbq_n - stk_n).clip(lower=0).round(0)
    cross["REQ"] = req
    req_rows = int(cross["REQ"].notna().sum())
    nonzero  = int((cross["REQ"].fillna(0) > 0).sum())
    stages.append({
        "stage": "compute_req",
        "rows":         int(len(cross)),
        "rows_with_req": req_rows,
        "nonzero_req":   nonzero,
        "formula":       "MAX(MBQ_SZ - STK_TTL_SZ, 0)",
    })
    logger.info(
        f"[onesize] REQ computed — {nonzero}/{len(cross)} rows have REQ > 0"
    )

    # ---- Stage 13: compute MAX_DAILY_SALE = MAX(L-7/7, AUTO_GEN_ART_SALE) -
    # AUTO_GEN_ART_SALE = SAL_PD (already in `cross` from Stage 9, loaded from
    # ARS_CALC_ST_MAJ_CAT). We don't re-load it from MASTER_GEN_ART_SALE.
    # We DO try to pull L-7-day sale qty from ARS_GRID_MJ_GEN_ART so the MAX
    # picks up recent-week spikes; if the L-7 column is missing we fall back
    # gracefully to SAL_PD alone.
    diag: Dict[str, Any] = {"stage": "enrich_listing", "source": "SAL_PD (from Stage 9) + ARS_GRID_MJ_GEN_ART L-7"}

    def _bp(label: str, **kv):
        """Loud breadcrumb — to stdout AND log AND stage payload."""
        msg = f"[onesize][enrich_listing][{label}] " + ", ".join(f"{k}={v}" for k, v in kv.items())
        print(msg, flush=True)
        logger.info(msg)
        diag.setdefault("checkpoints", []).append({"label": label, **kv})

    try:
        # AUTO_GEN_ART_SALE = SAL_PD × cont   (size-scaled baseline sale rate).
        # SAL_PD is in `cross` from Stage 9 (overwritten with option-grain in
        # Stage 9a). cont is the size contribution % from Stage 8.
        if "SAL_PD" in cross.columns and "cont" in cross.columns:
            _sp = pd.to_numeric(cross["SAL_PD"], errors="coerce")
            _co = pd.to_numeric(cross["cont"],   errors="coerce")
            cross["AUTO_GEN_ART_SALE"] = _sp * _co     # NaN propagates if either is NULL
        else:
            cross["AUTO_GEN_ART_SALE"] = np.nan
        auto_non_null = int(cross["AUTO_GEN_ART_SALE"].notna().sum())
        _bp("auto_from_sal_pd_x_cont", non_null_rows=auto_non_null,
            min=float(cross["AUTO_GEN_ART_SALE"].min()) if auto_non_null else None,
            max=float(cross["AUTO_GEN_ART_SALE"].max()) if auto_non_null else None)
        diag["auto_non_null"] = auto_non_null

        # ---- Pull L-7-DAYS-SALE from ARS_GRID_MJ_GEN_ART (option grain) ---
        cross["L7_DAILY"] = float("nan")
        diag["l7_source"] = None
        try:
            gen_grid_cols = _table_columns(db, GEN_ART_GRID_TABLE)
        except Exception as e:
            gen_grid_cols = []
            _bp("gen_grid_introspect_failed", error=str(e)[:200])

        if gen_grid_cols:
            gg_stcd = _resolve_col(gen_grid_cols, ["WERKS", "ST_CD", "STORE_CODE"])
            gg_maj  = _resolve_col(gen_grid_cols, ["MAJ_CAT", "MAJCAT"])
            gg_gen  = _resolve_col(gen_grid_cols, ["GEN_ART_NUMBER", "GEN_ART", "GEN_ART_NO"])
            gg_clr  = _resolve_col(gen_grid_cols, ["CLR", "COLOR", "COLOUR"])
            l7_col = None
            for c in gen_grid_cols:
                cu = c.upper()
                if ("L-7" in cu or "L_7" in cu) and "SALE" in cu and "7" in c:
                    l7_col = c
                    break
            if not l7_col:
                for c in gen_grid_cols:
                    cu = c.upper()
                    if cu.startswith("L-7") or cu.startswith("L_7"):
                        l7_col = c
                        break
            _bp("gen_grid_resolved", werks=gg_stcd, maj_cat=gg_maj, gen_art=gg_gen, clr=gg_clr, l7_col=l7_col)

            if gg_stcd and gg_maj and gg_gen and gg_clr and l7_col:
                diag["l7_source"] = f"{GEN_ART_GRID_TABLE}.{l7_col}"
                try:
                    unique_stores = (
                        cross["ST_CD"].dropna().astype(str).str.strip().unique().tolist()
                    )
                    l7_params: Dict[str, Any] = {}
                    l7_where = f" WHERE [{l7_col}] IS NOT NULL"
                    if unique_stores and len(unique_stores) <= 1800:
                        in_keys = []
                        for i, s in enumerate(unique_stores):
                            k = f"st{i}"
                            in_keys.append(f":{k}")
                            l7_params[k] = s
                        l7_where += f" AND [{gg_stcd}] IN ({', '.join(in_keys)})"
                    gen_df = pd.read_sql(
                        text(
                            f"SELECT [{gg_stcd}] AS ST_CD, [{gg_maj}] AS MAJ_CAT, "
                            f"[{gg_gen}] AS GEN_ART, [{gg_clr}] AS CLR, "
                            f"TRY_CAST([{l7_col}] AS FLOAT) AS L7_SALE "
                            f"FROM [{GEN_ART_GRID_TABLE}]{l7_where}"
                        ),
                        db.bind, params=l7_params,
                    )
                    gen_df["__ST_KEY"]  = _norm_str(gen_df["ST_CD"])
                    gen_df["__MAJ_KEY"] = _norm_str(gen_df["MAJ_CAT"])
                    gen_df["__GEN_KEY"] = _norm_num_or_str(gen_df["GEN_ART"])
                    gen_df["__CLR_KEY"] = _norm_str(gen_df["CLR"])
                    gen_lookup = (
                        gen_df.groupby(
                            ["__ST_KEY", "__MAJ_KEY", "__GEN_KEY", "__CLR_KEY"], dropna=False
                        )["L7_SALE"].max().reset_index()
                    )
                    cross["__ST_KEY"]  = _norm_str(cross["ST_CD"])
                    cross["__MAJ_KEY"] = _norm_str(cross[majcat_col])
                    cross["__GEN_KEY"] = _norm_num_or_str(cross[gen_art_col])
                    cross["__CLR_KEY"] = _norm_str(cross[clr_col])
                    cross = cross.drop(columns=["L7_DAILY"]).merge(
                        gen_lookup,
                        on=["__ST_KEY", "__MAJ_KEY", "__GEN_KEY", "__CLR_KEY"],
                        how="left",
                    )
                    cross["L7_DAILY"] = pd.to_numeric(cross["L7_SALE"], errors="coerce") / 7.0
                    cross = cross.drop(columns=["L7_SALE", "__ST_KEY", "__MAJ_KEY", "__GEN_KEY", "__CLR_KEY"])
                    matched_l7 = int(cross["L7_DAILY"].notna().sum())
                    diag["l7_loaded_rows"]  = int(len(gen_df))
                    diag["l7_matched_rows"] = matched_l7
                    _bp("l7_joined", loaded=len(gen_df), matched=matched_l7)
                except Exception as e:
                    _bp("l7_load_failed", error=str(e)[:200])
            else:
                _bp("gen_grid_missing_keys",
                    missing=[n for n, v in (
                        ("WERKS/ST_CD", gg_stcd), ("MAJ_CAT", gg_maj),
                        ("GEN_ART_NUMBER", gg_gen), ("CLR", gg_clr),
                        ("L-7 sale", l7_col),
                    ) if not v])
        else:
            _bp("gen_grid_not_found", table=GEN_ART_GRID_TABLE)

        # ---- Pull <prefix>_MBQ + <prefix>_DISP_Q from the opt-sale grid ---
        # Mirrors Listing Part 4b (listing.py:1517-1523):
        #   PER_OPT_SALE = ROUND(CASE
        #     WHEN ISNULL(opt_disp,0)=0 OR ISNULL(ALC_D,0)=0 THEN 0
        #     ELSE ((opt_mbq-opt_disp)/NULLIF(opt_disp,0) * ACS_D) / NULLIF(ALC_D,0)
        #   END, 2)
        # Default to 0.0 (not NaN) so missing-data rows match Listing's "WHEN ... THEN 0".
        # Step a: find the active opt-sale grid in ARS_GRID_BUILDER
        cross["PER_OPT_SALE"] = 0.0
        diag["opt_grid_table"] = None
        opt_grid_name = None
        try:
            row = db.execute(text(
                "SELECT TOP 1 grid_name FROM [ARS_GRID_BUILDER] "
                "WHERE ISNULL(use_for_opt_sale, 0) = 1 AND UPPER(status) = 'ACTIVE' "
                "ORDER BY seq ASC"
            )).fetchone()
            if row:
                opt_grid_name = row[0]
                _bp("opt_grid_resolved", grid_name=opt_grid_name)
            else:
                _bp("opt_grid_none_active",
                    note="no ARS_GRID_BUILDER row with use_for_opt_sale=1 AND status=ACTIVE")
        except Exception as e:
            _bp("opt_grid_lookup_failed", error=str(e)[:200])

        # Step b: load <prefix>_MBQ and <prefix>_DISP_Q from that grid table
        if opt_grid_name:
            opt_prefix = opt_grid_name.upper()
            if opt_prefix.startswith("MJ_"):
                opt_prefix = opt_prefix[3:]
            opt_mbq_col  = f"{opt_prefix}_MBQ"
            opt_disp_col = f"{opt_prefix}_DISP_Q"
            opt_table    = f"ARS_GRID_{opt_grid_name.upper()}"
            diag["opt_grid_table"]  = opt_table
            diag["opt_mbq_col"]     = opt_mbq_col
            diag["opt_disp_col"]    = opt_disp_col

            try:
                ot_cols = _table_columns(db, opt_table)
            except Exception as e:
                ot_cols = []
                _bp("opt_grid_introspect_failed", table=opt_table, error=str(e)[:200])

            if not ot_cols:
                _bp("opt_grid_table_not_found", table=opt_table)
            elif opt_mbq_col not in ot_cols or opt_disp_col not in ot_cols:
                _bp("opt_grid_cols_missing",
                    table=opt_table, mbq=opt_mbq_col, disp=opt_disp_col,
                    available=[c for c in ot_cols if "MBQ" in c.upper() or "DISP" in c.upper()][:10])
            else:
                ot_stcd = _resolve_col(ot_cols, ["WERKS", "ST_CD", "STORE_CODE"])
                ot_maj  = _resolve_col(ot_cols, ["MAJ_CAT", "MAJCAT"])
                ot_gen  = _resolve_col(ot_cols, ["GEN_ART_NUMBER", "GEN_ART", "GEN_ART_NO"])
                ot_clr  = _resolve_col(ot_cols, ["CLR", "COLOR", "COLOUR"])
                if not (ot_stcd and ot_maj and ot_gen and ot_clr):
                    _bp("opt_grid_keys_missing",
                        werks=ot_stcd, maj_cat=ot_maj, gen_art=ot_gen, clr=ot_clr)
                else:
                    try:
                        unique_stores = (
                            cross["ST_CD"].dropna().astype(str).str.strip().unique().tolist()
                        )
                        opt_params: Dict[str, Any] = {}
                        opt_where = ""
                        if unique_stores and len(unique_stores) <= 1800:
                            in_keys = []
                            for i, s in enumerate(unique_stores):
                                k = f"st{i}"
                                in_keys.append(f":{k}")
                                opt_params[k] = s
                            opt_where = f" WHERE [{ot_stcd}] IN ({', '.join(in_keys)})"
                        opt_df = pd.read_sql(
                            text(
                                f"SELECT [{ot_stcd}] AS ST_CD, [{ot_maj}] AS MAJ_CAT, "
                                f"[{ot_gen}] AS GEN_ART, [{ot_clr}] AS CLR, "
                                f"TRY_CAST([{opt_mbq_col}]  AS FLOAT) AS OPT_GRID_MBQ, "
                                f"TRY_CAST([{opt_disp_col}] AS FLOAT) AS OPT_GRID_DISP_Q "
                                f"FROM [{opt_table}]{opt_where}"
                            ),
                            db.bind, params=opt_params,
                        )
                        opt_df["__ST_KEY"]  = _norm_str(opt_df["ST_CD"])
                        opt_df["__MAJ_KEY"] = _norm_str(opt_df["MAJ_CAT"])
                        opt_df["__GEN_KEY"] = _norm_num_or_str(opt_df["GEN_ART"])
                        opt_df["__CLR_KEY"] = _norm_str(opt_df["CLR"])
                        # Collapse possible duplicates with MAX for safety.
                        opt_lookup = (
                            opt_df.groupby(
                                ["__ST_KEY", "__MAJ_KEY", "__GEN_KEY", "__CLR_KEY"], dropna=False
                            )[["OPT_GRID_MBQ", "OPT_GRID_DISP_Q"]].max().reset_index()
                        )
                        cross["__ST_KEY"]  = _norm_str(cross["ST_CD"])
                        cross["__MAJ_KEY"] = _norm_str(cross[majcat_col])
                        cross["__GEN_KEY"] = _norm_num_or_str(cross[gen_art_col])
                        cross["__CLR_KEY"] = _norm_str(cross[clr_col])
                        cross = cross.merge(
                            opt_lookup,
                            on=["__ST_KEY", "__MAJ_KEY", "__GEN_KEY", "__CLR_KEY"],
                            how="left",
                        ).drop(columns=["__ST_KEY", "__MAJ_KEY", "__GEN_KEY", "__CLR_KEY"])

                        # PER_OPT_SALE = ((MBQ-DISP_Q)/DISP_Q × ACS_D) / ALC_D
                        mbq_o  = pd.to_numeric(cross.get("OPT_GRID_MBQ"),    errors="coerce")
                        disp_o = pd.to_numeric(cross.get("OPT_GRID_DISP_Q"), errors="coerce")
                        acsd_o = pd.to_numeric(cross.get("ACS_D"),           errors="coerce")
                        alcd_o = pd.to_numeric(cross.get("ALC_D"),           errors="coerce")
                        # Guards: DISP_Q=0 or ALC_D=0 → 0 (matches Listing's CASE)
                        denom_disp  = disp_o.where(disp_o > 0)
                        denom_alc   = alcd_o.where(alcd_o > 0)
                        rel_excess  = (mbq_o - disp_o) / denom_disp
                        per_opt_raw = (rel_excess * acsd_o) / denom_alc
                        per_opt     = per_opt_raw.fillna(0)
                        # Clip negatives to 0 — if MBQ < DISP_Q (over-stocked), the
                        # formula would go negative; treat as no synthetic signal.
                        per_opt     = per_opt.clip(lower=0).round(2)
                        cross["PER_OPT_SALE"] = per_opt

                        matched_opt = int(mbq_o.notna().sum())
                        nonzero_pos = int((per_opt > 0).sum())
                        diag["per_opt_matched_rows"] = matched_opt
                        diag["per_opt_nonzero_rows"] = nonzero_pos
                        _bp("per_opt_sale_computed",
                            loaded=len(opt_df), matched=matched_opt, nonzero=nonzero_pos)
                        # Surface why rows are 0 even after the lookup
                        if nonzero_pos == 0 and matched_opt > 0:
                            zero_disp = int((disp_o.fillna(0) == 0).sum())
                            zero_alc  = int((alcd_o.fillna(0) == 0).sum())
                            neg_clip  = int((per_opt_raw < 0).sum())
                            _bp("per_opt_all_zero_after_compute",
                                rows_matched=matched_opt,
                                disp_zero_rows=zero_disp,
                                alc_d_zero_rows=zero_alc,
                                clipped_negative_rows=neg_clip)
                    except Exception as e:
                        _bp("per_opt_load_failed", error=str(e)[:200])

        # ---- Fallback: where grid formula produced 0 (or path failed), use SAL_PD --
        # User rule: PER_OPT_SALE = grid formula if valid, ELSE SAL_PD.
        # "Valid" = strictly positive after the grid math (denominator guards
        # and negative-clip already zeroed the invalid cases above).
        sal_fb   = pd.to_numeric(cross.get("SAL_PD"),       errors="coerce").fillna(0)
        per_now  = pd.to_numeric(cross.get("PER_OPT_SALE"), errors="coerce").fillna(0)
        from_grid     = int((per_now > 0).sum())
        fallback_mask = per_now <= 0
        per_now[fallback_mask] = sal_fb[fallback_mask]
        from_sal_pd   = int(((per_now > 0) & fallback_mask).sum())
        still_zero    = int((per_now <= 0).sum())
        cross["PER_OPT_SALE"] = per_now.round(2)
        diag["per_opt_from_grid"]    = from_grid
        diag["per_opt_from_sal_pd"]  = from_sal_pd
        diag["per_opt_still_zero"]   = still_zero
        _bp("per_opt_fallback_applied",
            from_grid=from_grid, from_sal_pd_fallback=from_sal_pd, still_zero=still_zero)

        # ---- Pull AGE from MASTER_GEN_ART_AGE (option grain) --------------
        # Listing logic (rate_expr at listing.py:1611-1617):
        #   AGE  <  15  →  MAX(PER_OPT_SALE, L7/7, AUTO_GEN_ART_SALE)   "new article"
        #   AGE  >= 15  →  MAX(L7/7, AUTO_GEN_ART_SALE)                  "old article"
        # AGE NULL/missing → treated as 0 → new (matches Listing's ISNULL(AGE,0))
        cross["AGE"] = float("nan")
        diag["age_source"] = None
        try:
            age_cols = _table_columns(db, MGAA_TABLE)
        except Exception as e:
            age_cols = []
            _bp("age_introspect_failed", error=str(e)[:200])
        if age_cols:
            a_stcd = _resolve_col(age_cols, ["ST_CD", "WERKS", "STORE_CODE"])
            a_maj  = _resolve_col(age_cols, ["MAJ_CAT", "MAJCAT"])
            a_gen  = _resolve_col(age_cols, ["GEN_ART_NUMBER", "GEN_ART", "GEN_ART_NO"])
            a_clr  = _resolve_col(age_cols, ["CLR", "COLOR", "COLOUR"])
            a_age  = _resolve_col(age_cols, ["AGE", "AGE_D", "AGE_DAYS", "OPT_AGE"])
            if a_stcd and a_maj and a_gen and a_clr and a_age:
                diag["age_source"] = f"{MGAA_TABLE}.{a_age}"
                try:
                    unique_stores = (
                        cross["ST_CD"].dropna().astype(str).str.strip().unique().tolist()
                    )
                    age_params: Dict[str, Any] = {}
                    age_where = f" WHERE [{a_age}] IS NOT NULL"
                    if unique_stores and len(unique_stores) <= 1800:
                        in_keys = []
                        for i, s in enumerate(unique_stores):
                            k = f"st{i}"
                            in_keys.append(f":{k}")
                            age_params[k] = s
                        age_where += f" AND [{a_stcd}] IN ({', '.join(in_keys)})"
                    age_df = pd.read_sql(
                        text(
                            f"SELECT [{a_stcd}] AS ST_CD, [{a_maj}] AS MAJ_CAT, "
                            f"[{a_gen}] AS GEN_ART, [{a_clr}] AS CLR, "
                            f"TRY_CAST([{a_age}] AS FLOAT) AS AGE "
                            f"FROM [{MGAA_TABLE}]{age_where}"
                        ),
                        db.bind, params=age_params,
                    )
                    age_df["__ST_KEY"]  = _norm_str(age_df["ST_CD"])
                    age_df["__MAJ_KEY"] = _norm_str(age_df["MAJ_CAT"])
                    age_df["__GEN_KEY"] = _norm_num_or_str(age_df["GEN_ART"])
                    age_df["__CLR_KEY"] = _norm_str(age_df["CLR"])
                    age_lookup = (
                        age_df.groupby(
                            ["__ST_KEY", "__MAJ_KEY", "__GEN_KEY", "__CLR_KEY"], dropna=False
                        )["AGE"].min().reset_index()    # MIN — youngest age wins
                    )
                    cross["__ST_KEY"]  = _norm_str(cross["ST_CD"])
                    cross["__MAJ_KEY"] = _norm_str(cross[majcat_col])
                    cross["__GEN_KEY"] = _norm_num_or_str(cross[gen_art_col])
                    cross["__CLR_KEY"] = _norm_str(cross[clr_col])
                    cross = cross.drop(columns=["AGE"]).merge(
                        age_lookup,
                        on=["__ST_KEY", "__MAJ_KEY", "__GEN_KEY", "__CLR_KEY"],
                        how="left",
                    ).drop(columns=["__ST_KEY", "__MAJ_KEY", "__GEN_KEY", "__CLR_KEY"])
                    matched_age = int(cross["AGE"].notna().sum())
                    diag["age_loaded_rows"]  = int(len(age_df))
                    diag["age_matched_rows"] = matched_age
                    _bp("age_joined", loaded=len(age_df), matched=matched_age)
                except Exception as e:
                    _bp("age_load_failed", error=str(e)[:200])
            else:
                _bp("age_keys_missing",
                    missing=[n for n, v in (
                        ("ST_CD", a_stcd), ("MAJ_CAT", a_maj),
                        ("GEN_ART_NUMBER", a_gen), ("CLR", a_clr),
                        ("AGE", a_age),
                    ) if not v])
        else:
            _bp("age_table_not_found", table=MGAA_TABLE)

        # ---- Compute MAX_DAILY_SALE with AGE switch -----------------------
        auto_n = pd.to_numeric(cross.get("AUTO_GEN_ART_SALE"), errors="coerce")
        l7_n   = pd.to_numeric(cross.get("L7_DAILY"),          errors="coerce")
        pos_n  = pd.to_numeric(cross.get("PER_OPT_SALE"),      errors="coerce")
        # Effective age — NULL → 0 (treated as new article, per Listing)
        age_n  = pd.to_numeric(cross.get("AGE"), errors="coerce").fillna(0)
        is_new = age_n < AGE_THRESHOLD_DAYS

        # NEW article rate: 3-input MAX(L7/7, AUTO, PER_OPT)
        # OLD article rate: MAX(L7/7, AUTO) − PER_OPT_SALE  (clipped to 0)
        af = auto_n.fillna(0)
        lf = l7_n.fillna(0)
        pf = pos_n.fillna(0)
        new_rate = pd.concat([af, lf, pf], axis=1).max(axis=1)
        old_rate = (pd.concat([af, lf], axis=1).max(axis=1) - pf).clip(lower=0)
        mds = pd.Series(np.where(is_new, new_rate, old_rate), index=cross.index)
        # All-null guard — preserve NaN only when every input was NaN
        all_null = auto_n.isna() & l7_n.isna() & (pos_n.isna() | ~is_new)
        mds[all_null] = pd.NA
        cross["MAX_DAILY_SALE"] = mds

        # Attribution counters
        picked_l7   = int(((lf >= af) & (~is_new | (lf >= pf))).sum())
        picked_auto = int(((af >  lf) & (~is_new | (af >= pf))).sum())
        picked_pos  = int((is_new & (pf > af) & (pf > lf)).sum())
        new_rows    = int(is_new.sum())
        old_rows    = int((~is_new).sum())
        diag["age_threshold"]      = AGE_THRESHOLD_DAYS
        diag["new_article_rows"]   = new_rows
        diag["old_article_rows"]   = old_rows
        diag["mds_picked_l7"]      = picked_l7
        diag["mds_picked_auto"]    = picked_auto
        diag["mds_picked_per_opt"] = picked_pos
        diag["mds_all_null"]       = int(all_null.sum())
        diag["formula"]            = (
            f"AGE < {AGE_THRESHOLD_DAYS} → MAX(L7/7, AUTO_GEN_ART_SALE, PER_OPT_SALE); "
            f"AGE >= {AGE_THRESHOLD_DAYS} → MAX(L7/7, AUTO_GEN_ART_SALE) − PER_OPT_SALE (≥ 0)"
        )
        stages.append(diag)
        _bp("done",
            new_rows=new_rows, old_rows=old_rows,
            picked_l7=picked_l7, picked_auto=picked_auto, picked_per_opt=picked_pos,
            all_null=int(all_null.sum()))
    except Exception as e:
        if "MAX_DAILY_SALE" not in cross.columns:
            cross["MAX_DAILY_SALE"] = None
        if "AUTO_GEN_ART_SALE" not in cross.columns:
            cross["AUTO_GEN_ART_SALE"] = None
        if "L7_DAILY" not in cross.columns:
            cross["L7_DAILY"] = None
        diag.update({"error": str(e)[:500], "skipped": True})
        stages.append(diag)
        logger.exception(f"[onesize] enrich_listing crashed: {e}")
        print(f"[onesize][enrich_listing][CRASH] {e}", flush=True)

    # ---- Stage 14: compute SALE_VAR_ART = MAX_DAILY_SALE * cont ----------
    mds_n  = pd.to_numeric(cross.get("MAX_DAILY_SALE"), errors="coerce")
    cont_n = pd.to_numeric(cross.get("cont"),           errors="coerce")
    cross["SALE_VAR_ART"] = mds_n * cont_n  # NaN propagates if either is NULL
    sv_computed = int(cross["SALE_VAR_ART"].notna().sum())
    stages.append({
        "stage": "compute_sale_var_art",
        "rows":          int(len(cross)),
        "computed_rows": sv_computed,
        "null_rows":     int(len(cross) - sv_computed),
        "formula":       "MAX_DAILY_SALE * cont",
    })
    logger.info(
        f"[onesize] SALE_VAR_ART computed for {sv_computed}/{len(cross)} rows"
    )

    # ---- Stage 15: compute MBQ_VAR = (MAX_DAILY_SALE × days) + (ACS_D × cont) ----
    # The fixture-floor component is now size-scaled by `cont` so MBQ_VAR
    # varies per size within the same option. (ACS_D × cont) is already in
    # `cross` as `var_art_disp` from Stage 9c — reuse it directly.
    mds_n  = pd.to_numeric(cross.get("MAX_DAILY_SALE"), errors="coerce")
    vad_n  = pd.to_numeric(cross.get("var_art_disp"),   errors="coerce")   # = ACS_D × cont
    days_v = pd.to_numeric(cross.get("days"),           errors="coerce").fillna(0)
    sale_part   = mds_n.fillna(0) * days_v
    floor_part  = vad_n.fillna(0)
    mbq_var = sale_part + floor_part
    cross["MBQ_VAR"] = mbq_var.round(0)
    both_null   = mds_n.isna() & vad_n.isna()
    nonzero_rows = int((mbq_var > 0).sum())
    stages.append({
        "stage": "compute_mbq_var",
        "rows":             int(len(cross)),
        "computed_rows":    nonzero_rows,
        "zero_rows":        int(len(cross) - nonzero_rows),
        "both_null_inputs": int(both_null.sum()),
        "formula":          "(MAX_DAILY_SALE × days) + (ACS_D × cont)   [= sale_part + var_art_disp]",
    })
    logger.info(
        f"[onesize] MBQ_VAR computed — nonzero={nonzero_rows}, "
        f"both_null_inputs={int(both_null.sum())}"
    )

    # ---- Stage 16: compute VAR_REQ = MAX(MBQ_VAR - STK_TTL, 0) ----------
    # MBQ_VAR is now guaranteed non-NaN (forced to 0 when inputs are 0/NULL),
    # so VAR_REQ will be 0 in those cases instead of staying NaN.
    mbq_v_n = pd.to_numeric(cross.get("MBQ_VAR"), errors="coerce")
    stk_v_n = pd.to_numeric(cross.get("STK_TTL"), errors="coerce").fillna(0)
    var_req = (mbq_v_n - stk_v_n).clip(lower=0).round(0)
    cross["VAR_REQ"] = var_req
    vr_rows = int(cross["VAR_REQ"].notna().sum())
    vr_nz   = int((cross["VAR_REQ"].fillna(0) > 0).sum())
    stages.append({
        "stage": "compute_var_req",
        "rows":           int(len(cross)),
        "rows_with_var_req": vr_rows,
        "nonzero_var_req":   vr_nz,
        "formula":           "MAX(MBQ_VAR - STK_TTL, 0)",
    })
    logger.info(
        f"[onesize] VAR_REQ computed — {vr_nz}/{len(cross)} rows have VAR_REQ > 0"
    )

    # ---- Stage 17: enrich MSA_REMAIN (warehouse pool) + ST_RANK ----------
    # MSA_REMAIN = SUM(FNL_Q) per (RDC, MAJ_CAT, GEN_ART, CLR, SZ) from
    # ARS_MSA_TOTAL for the current sequence/SSN, summed across SLOCs.
    # ST_RANK = per-store rank from ARS_STORE_RANKING at (ST_CD, MAJ_CAT) grain.
    alloc_diag: Dict[str, Any] = {"stage": "enrich_msa_remain_and_rank"}

    def _ad(label, **kv):
        msg = f"[onesize][alloc][{label}] " + ", ".join(f"{k}={v}" for k, v in kv.items())
        print(msg, flush=True)
        logger.info(msg)
        alloc_diag.setdefault("checkpoints", []).append({"label": label, **kv})

    # ---- Step A: pool MSA_REMAIN ----------------------------------------
    try:
        ssn_clause = ""
        ssn_params: Dict[str, Any] = {"sid": sequence_id}
        if ssn_list:
            in_keys = []
            for i, v in enumerate(ssn_list):
                k = f"ssn{i}"
                in_keys.append(f":{k}")
                ssn_params[k] = v
            ssn_clause = (
                f" AND UPPER(LTRIM(RTRIM(CAST([SSN] AS NVARCHAR(50))))) "
                f"IN ({', '.join(in_keys)})"
            )
        pool_sql = (
            f"SELECT [RDC] AS RDC, [{majcat_col}] AS MAJ_CAT, "
            f"[{gen_art_col}] AS GEN_ART, [{clr_col}] AS CLR, "
            f"[{sz_col if sz_col else 'SZ'}] AS SZ, "
            f"SUM(TRY_CAST([{qty_col if qty_col else 'FNL_Q'}] AS FLOAT)) AS MSA_REMAIN "
            f"FROM [{MSA_TOTAL_TABLE}] "
            f"WHERE sequence_id = :sid{ssn_clause} "
            f"GROUP BY [RDC], [{majcat_col}], [{gen_art_col}], [{clr_col}], "
            f"[{sz_col if sz_col else 'SZ'}]"
        )
        pool_df = pd.read_sql(text(pool_sql), db.bind, params=ssn_params)
        _ad("pool_loaded", rows=len(pool_df),
            total_qty=float(pd.to_numeric(pool_df["MSA_REMAIN"], errors="coerce").sum()) if len(pool_df) else 0)

        pool_df["__RDC_KEY"]  = pool_df["RDC"].astype(str).str.strip().str.upper()
        pool_df["__MAJ_KEY"]  = pool_df["MAJ_CAT"].astype(str).str.strip().str.upper()
        pool_df["__GEN_KEY"]  = _norm_num_or_str(pool_df["GEN_ART"])
        pool_df["__CLR_KEY"]  = pool_df["CLR"].astype(str).str.strip().str.upper()
        pool_df["__SZ_KEY"]   = pool_df["SZ"].astype(str).str.strip().str.upper()

        cross["__RDC_KEY"] = cross["RDC"].astype(str).str.strip().str.upper()
        cross["__MAJ_KEY"] = cross[majcat_col].astype(str).str.strip().str.upper()
        cross["__GEN_KEY"] = _norm_num_or_str(cross[gen_art_col])
        cross["__CLR_KEY"] = cross[clr_col].astype(str).str.strip().str.upper()
        if sz_col and sz_col in cross.columns:
            cross["__SZ_KEY"] = cross[sz_col].astype(str).str.strip().str.upper()
        else:
            cross["__SZ_KEY"] = cross.get("SZ", pd.Series([""] * len(cross))).astype(str).str.strip().str.upper()

        pool_keys_cols = ["__RDC_KEY", "__MAJ_KEY", "__GEN_KEY", "__CLR_KEY", "__SZ_KEY"]
        pool_lookup = pool_df[pool_keys_cols + ["MSA_REMAIN"]]
        cross = cross.merge(pool_lookup, on=pool_keys_cols, how="left")
        # Round pool to integer — keeps allocation math integer end-to-end.
        cross["MSA_REMAIN"] = pd.to_numeric(cross["MSA_REMAIN"], errors="coerce").round(0)
        matched_pool = int(cross["MSA_REMAIN"].notna().sum())
        _ad("pool_joined", matched=matched_pool, unmatched=int(len(cross) - matched_pool))
        alloc_diag["pool_rows"]    = int(len(pool_df))
        alloc_diag["pool_matched"] = matched_pool
    except Exception as e:
        cross["MSA_REMAIN"] = None
        _ad("pool_failed", error=str(e)[:200])
        alloc_diag.update({"pool_error": str(e)[:300]})

    # ---- Step B: compute store ranks fresh from ARS_LISTING -------------
    # Two ranks computed here:
    #   ST_RANK       — per-(ST_CD, MAJ_CAT) rank computed FRESH inline from
    #                    ARS_LISTING using the SAME 0.4·REQ + 0.6·FILL formula
    #                    as Listing Part 6. USED to drive allocation sort.
    #                    ARS_STORE_RANKING is NOT used — fresh compute avoids
    #                    stale data when Listing hasn't been re-run.
    #   ST_RANK_STORE — store-level rank, freshly computed from ARS_LISTING
    #                    aggregated to store grain (no MAJ_CAT partition).
    #                    Display only; NOT used for allocation.
    try:
        # Source: ARS_GRID_MJ at (WERKS, MAJ_CAT) grain. Covers ~826 MAJ_CATs
        # and 365 stores — far better coverage than ARS_LISTING (which only
        # has ~28 MAJ_CATs). MJ_REQ derived inline as MAX(MBQ - STK_TTL, 0).
        # MBQ > 0 filter drops noise rows where no target stock is defined.
        majcat_rank_sql = """
            ;WITH StoreAgg AS (
                SELECT [MAJ_CAT], [WERKS] AS ST_CD,
                       MAX(ISNULL([MBQ],     0)) AS MJ_MBQ,
                       MAX(ISNULL([STK_TTL], 0)) AS MJ_STK,
                       CASE WHEN MAX(ISNULL([MBQ], 0)) > MAX(ISNULL([STK_TTL], 0))
                            THEN MAX(ISNULL([MBQ], 0)) - MAX(ISNULL([STK_TTL], 0))
                            ELSE 0
                       END AS MJ_REQ,
                       CASE WHEN MAX(ISNULL([MBQ], 0)) = 0 THEN 0
                            ELSE ROUND(
                                MAX(ISNULL([STK_TTL], 0)) /
                                NULLIF(MAX([MBQ]), 0), 4
                            ) END AS FILL_RATE
                FROM [ARS_GRID_MJ]
                WHERE ISNULL([MBQ], 0) > 0
                GROUP BY [MAJ_CAT], [WERKS]
            ),
            Ranked AS (
                SELECT *,
                       ROW_NUMBER() OVER (PARTITION BY MAJ_CAT ORDER BY MJ_REQ ASC)    AS REQ_RANK,
                       ROW_NUMBER() OVER (PARTITION BY MAJ_CAT ORDER BY FILL_RATE DESC) AS FILL_RANK
                FROM StoreAgg
            )
            SELECT ST_CD, MAJ_CAT,
                   ROW_NUMBER() OVER (
                       PARTITION BY MAJ_CAT
                       ORDER BY ROUND(REQ_RANK * 0.4 + FILL_RANK * 0.6, 2) DESC
                   ) AS ST_RANK
            FROM Ranked
        """
        rank_df = pd.read_sql(text(majcat_rank_sql), db.bind)
        # Use rank-local key names so we don't trample the existing __ST_KEY /
        # __MAJ_KEY columns that Stage 17 Step A added to cross for the pool
        # join (those are reused by Stage 18's allocation sort).
        rank_df["__RNK_ST_KEY"]  = rank_df["ST_CD"].astype(str).str.strip().str.upper()
        rank_df["__RNK_MAJ_KEY"] = rank_df["MAJ_CAT"].astype(str).str.strip().str.upper()
        rank_df["ST_RANK"]       = pd.to_numeric(rank_df["ST_RANK"], errors="coerce")
        rank_lookup = rank_df[["__RNK_ST_KEY", "__RNK_MAJ_KEY", "ST_RANK"]]

        cross["__RNK_ST_KEY"]  = cross["ST_CD"].astype(str).str.strip().str.upper()
        cross["__RNK_MAJ_KEY"] = cross[majcat_col].astype(str).str.strip().str.upper()
        cross = cross.merge(rank_lookup, on=["__RNK_ST_KEY", "__RNK_MAJ_KEY"], how="left")
        cross = cross.drop(columns=["__RNK_ST_KEY", "__RNK_MAJ_KEY"])
        matched_rank = int(cross["ST_RANK"].notna().sum())
        _ad("rank_computed_inline",
            rank_rows=int(len(rank_df)),
            distinct_majcats=int(rank_df["__RNK_MAJ_KEY"].nunique()) if "__RNK_MAJ_KEY" in rank_df.columns else None,
            matched=matched_rank,
            unmatched=int(len(cross) - matched_rank),
            formula="ROW_NUMBER OVER (PARTITION BY MAJ_CAT ORDER BY 0.4·REQ_RANK + 0.6·FILL_RANK DESC)",
            source="ARS_GRID_MJ (MBQ > 0)",
            note="fresh — full coverage (~826 MAJ_CATs, 365 stores)")
        alloc_diag["rank_matched"] = matched_rank
    except Exception as e:
        cross["ST_RANK"] = np.nan
        _ad("rank_compute_failed", error=str(e)[:200])

    # ---- Step C: compute store-level ST_RANK_STORE (display only) -------
    # Same formula as Listing Part 6 (0.4 × REQ_RANK + 0.6 × FILL_RANK)
    # but aggregated to store grain across ALL MAJ_CATs. Fresh computation —
    # does NOT depend on ARS_STORE_RANKING. Allocation does NOT use this.
    try:
        store_rank_sql = """
            ;WITH StoreAgg AS (
                SELECT [WERKS] AS ST_CD,
                       SUM(ISNULL([MJ_REQ],     0)) AS TOTAL_REQ,
                       SUM(ISNULL([MJ_MBQ],     0)) AS TOTAL_MBQ,
                       SUM(ISNULL([MJ_STK_TTL], 0)) AS TOTAL_STK,
                       CASE WHEN SUM(ISNULL([MJ_MBQ], 0)) = 0 THEN 0
                            ELSE ROUND(
                                SUM(ISNULL([MJ_STK_TTL],0)) /
                                NULLIF(SUM([MJ_MBQ]), 0), 4
                            ) END AS FILL_RATE
                FROM [ARS_LISTING]
                WHERE ISNULL([OPT_TYPE], '') <> 'MIX'
                GROUP BY [WERKS]
            ),
            Ranked AS (
                SELECT *,
                       ROW_NUMBER() OVER (ORDER BY TOTAL_REQ ASC)  AS REQ_RANK,
                       ROW_NUMBER() OVER (ORDER BY FILL_RATE DESC) AS FILL_RANK
                FROM StoreAgg
            )
            SELECT ST_CD,
                   ROW_NUMBER() OVER (
                       ORDER BY ROUND(REQ_RANK * 0.4 + FILL_RANK * 0.6, 2) DESC
                   ) AS ST_RANK_STORE
            FROM Ranked
        """
        store_rank_df = pd.read_sql(text(store_rank_sql), db.bind)
        store_rank_df["__ST_KEY"]      = store_rank_df["ST_CD"].astype(str).str.strip().str.upper()
        store_rank_df["ST_RANK_STORE"] = pd.to_numeric(store_rank_df["ST_RANK_STORE"], errors="coerce")
        store_lookup = store_rank_df[["__ST_KEY", "ST_RANK_STORE"]]

        cross["__ST_KEY"] = cross["ST_CD"].astype(str).str.strip().str.upper()
        cross = cross.merge(store_lookup, on="__ST_KEY", how="left")
        cross = cross.drop(columns=["__ST_KEY"])
        matched_store = int(cross["ST_RANK_STORE"].notna().sum())
        _ad("rank_store_joined",
            stores_ranked=int(len(store_rank_df)),
            matched=matched_store,
            unmatched=int(len(cross) - matched_store),
            formula="ROW_NUMBER OVER (W_SCORE DESC) on ARS_LISTING-aggregated store totals",
            note="display only — allocation uses ST_RANK (per-MAJ_CAT)")
        alloc_diag["rank_store_matched"] = matched_store
    except Exception as e:
        cross["ST_RANK_STORE"] = np.nan
        _ad("rank_store_compute_failed", error=str(e)[:200])

    stages.append(alloc_diag)

    # ---- Stage 17b: drop zero-demand rows BEFORE allocation -------------
    # A row needs BOTH REQ > 0 AND VAR_REQ > 0 to be a candidate. Rows where
    # EITHER is 0/NULL can never receive stock, so drop them now. Saves work
    # in the allocation loop and shrinks the output.
    rows_before_demand_filter = len(cross)
    req_n     = pd.to_numeric(cross.get("REQ"),     errors="coerce").fillna(0)
    var_req_n = pd.to_numeric(cross.get("VAR_REQ"), errors="coerce").fillna(0)
    dropped_no_req     = int(((req_n     <= 0) & (var_req_n >  0)).sum())
    dropped_no_var_req = int(((var_req_n <= 0) & (req_n     >  0)).sum())
    dropped_both_zero  = int(((req_n     <= 0) & (var_req_n <= 0)).sum())
    keep_demand = (req_n > 0) & (var_req_n > 0)
    cross = cross[keep_demand].copy()
    stages.append({
        "stage":               "drop_zero_demand",
        "rows_before":         rows_before_demand_filter,
        "rows":                len(cross),
        "dropped_total":       rows_before_demand_filter - len(cross),
        "dropped_no_req":      dropped_no_req,
        "dropped_no_var_req":  dropped_no_var_req,
        "dropped_both_zero":   dropped_both_zero,
        "rule":                "keep iff REQ > 0 AND VAR_REQ > 0",
    })
    logger.info(
        f"[onesize] dropped {rows_before_demand_filter - len(cross)} zero-demand rows "
        f"({rows_before_demand_filter}→{len(cross)}; "
        f"no_req={dropped_no_req} no_var_req={dropped_no_var_req} "
        f"both_zero={dropped_both_zero})"
    )

    if cross.empty:
        # Nothing left to allocate — short-circuit to a clean empty result.
        cross["ALLOC"]        = 0
        cross["REMAIN_AFTER"] = 0
        cross["DEMAND_SRC"]   = ""
        cross["POOL_POS"]     = 0
        stages.append({
            "stage":   "compute_allocation",
            "rows":    0,
            "allocated_rows": 0,
            "total_allocated": 0,
            "note": "skipped — no rows had REQ>0 AND VAR_REQ>0",
        })
        return {
            "sequence_id": sequence_id,
            "total_rows": 0,
            "columns": cross.columns.tolist(),
            "stores": int(len(stores_df)),
            "stages": stages,
            "_df": cross,
        }

    # ---- Stage 18: compute_allocation ------------------------------------
    # Per pool (RDC, MAJ_CAT, GEN_ART, CLR, SZ), drain MSA_REMAIN by ranked
    # stores. STRICT RULE: BOTH `REQ > 0` AND `VAR_REQ > 0` are required for
    # allocation. If EITHER is 0/NULL, the row is skipped without depleting
    # the pool. When both are positive, cap = MIN(VAR_REQ, REQ, available).
    cross["ALLOC"]        = 0.0
    cross["REMAIN_AFTER"] = pd.to_numeric(cross.get("MSA_REMAIN"), errors="coerce").fillna(0)
    cross["DEMAND_SRC"]   = ""

    # Sort key: pool first, then rank ASC (NaN ranks last), then ST_CD ASC
    rank_for_sort = cross["ST_RANK"].fillna(np.inf)
    cross = cross.assign(__SORT_RANK=rank_for_sort)
    sort_cols = pool_keys_cols + ["__SORT_RANK", "ST_CD"]
    cross = cross.sort_values(sort_cols, kind="mergesort").reset_index(drop=True)

    var_req_arr = pd.to_numeric(cross["VAR_REQ"], errors="coerce").fillna(0).to_numpy(dtype=float)
    req_arr     = pd.to_numeric(cross["REQ"],     errors="coerce").fillna(0).to_numpy(dtype=float)
    pool_arr    = pd.to_numeric(cross["MSA_REMAIN"], errors="coerce").fillna(0).to_numpy(dtype=float)
    alloc_arr   = np.zeros(len(cross), dtype=float)
    remain_arr  = np.zeros(len(cross), dtype=float)
    src_arr     = np.empty(len(cross), dtype=object)
    src_arr[:]  = ""

    # Iterate per pool group; numpy positional indices are contiguous after sort.
    group_ids = (
        cross[pool_keys_cols].astype(str).agg("|".join, axis=1)
    )
    grp_codes = group_ids.factorize(sort=False)[0]
    # Stats
    skipped_no_demand     = 0   # both REQ and VAR_REQ ≤ 0
    skipped_no_var_req    = 0   # REQ > 0 but VAR_REQ ≤ 0
    skipped_no_req        = 0   # VAR_REQ > 0 but REQ ≤ 0  (both required now)
    skipped_pool_empty    = 0
    allocated_rows        = 0
    src_both              = 0
    pools_drained         = 0
    pools_partial         = 0

    i = 0
    n = len(cross)
    while i < n:
        # Find end of this group (same pool)
        gc = grp_codes[i]
        j = i + 1
        while j < n and grp_codes[j] == gc:
            j += 1
        # Pool's initial available is the first row's MSA_REMAIN (all same in group)
        available = pool_arr[i]
        had_alloc = False
        for k in range(i, j):
            vr = var_req_arr[k]
            rq = req_arr[k]
            has_vr = vr > 0
            has_rq = rq > 0
            # ── STRICT: BOTH REQ AND VAR_REQ must be > 0 ──
            if not has_vr and not has_rq:
                remain_arr[k] = available
                src_arr[k]    = "NONE"
                skipped_no_demand += 1
                continue
            if not has_vr:                     # VAR_REQ missing
                remain_arr[k] = available
                src_arr[k]    = "NO_VAR_REQ"
                skipped_no_var_req += 1
                continue
            if not has_rq:                     # REQ missing
                remain_arr[k] = available
                src_arr[k]    = "NO_REQ"
                skipped_no_req += 1
                continue
            # Both positive — check pool.
            if available <= 0:
                remain_arr[k] = 0.0
                src_arr[k]    = "POOL_EMPTY"
                skipped_pool_empty += 1
                continue
            demand_cap = min(vr, rq)
            cap = min(demand_cap, available)
            if cap <= 0:
                remain_arr[k] = available
                src_arr[k]    = "BOTH_CAP_0"
                continue
            alloc_arr[k]  = cap
            available    -= cap
            remain_arr[k] = available
            src_arr[k]    = "BOTH"
            src_both     += 1
            allocated_rows += 1
            had_alloc = True
        if had_alloc:
            if available <= 0:
                pools_drained += 1
            else:
                pools_partial += 1
        i = j

    cross["DEMAND_SRC"] = src_arr

    # Debug column: this row's 1-based position within its pool after the rank
    # sort. POOL_POS=1 means "first store in this pool" (highest priority).
    # If you see ALLOC=0 with POOL_POS=1 and VAR_REQ>0, that's a real bug.
    # If ALLOC=0 with POOL_POS>1, an earlier row in the same pool took the stock.
    pool_pos = np.zeros(len(cross), dtype=np.int64)
    i2 = 0
    while i2 < n:
        gc = grp_codes[i2]
        j2 = i2 + 1
        while j2 < n and grp_codes[j2] == gc:
            j2 += 1
        for offset, k in enumerate(range(i2, j2)):
            pool_pos[k] = offset + 1
        i2 = j2
    cross["POOL_POS"] = pool_pos

    # Round allocation outputs to integers — matches Listing's ROUND(...,0)
    # convention and avoids tiny float residues like 0.39999.
    cross["ALLOC"]        = np.round(alloc_arr, 0)
    cross["REMAIN_AFTER"] = np.round(remain_arr, 0)

    # FINAL_ALLOCATION = MROUND(ALLOC, PAK_SZ) — round ALLOC to the nearest
    # multiple of PAK_SZ so shipments come out in whole cartons. Matches
    # Excel's MROUND (round half AWAY from zero). When PAK_SZ is 0/NaN/<=0,
    # fall back to plain ALLOC so the column never silently zeroes out.
    pak_n   = pd.to_numeric(cross.get("PAK_SZ"), errors="coerce").fillna(0)
    alloc_n = pd.to_numeric(cross["ALLOC"],      errors="coerce").fillna(0)
    safe_pak = pak_n.where(pak_n > 0, 1)        # avoid div-by-zero
    rounded  = np.floor(alloc_n.abs() / safe_pak + 0.5) * safe_pak * np.sign(alloc_n).replace(0, 1)
    cross["FINAL_ALLOCATION"] = np.where(pak_n > 0, rounded, alloc_n)

    cross = cross.drop(columns=["__SORT_RANK"] + pool_keys_cols)

    # Log the first few pools that had positive VAR_REQ but allocated 0 — these
    # are the most diagnostic. If POOL_POS=1 on a positive-VAR_REQ skip, the
    # allocation logic is misbehaving; otherwise the pool simply drained early.
    debug_mask = (
        (cross["VAR_REQ"].fillna(0) > 0)
        & (cross["ALLOC"].fillna(0) == 0)
        & (cross["DEMAND_SRC"] != "POOL_EMPTY")
    )
    suspicious = cross[debug_mask].head(10)
    if len(suspicious):
        logger.warning(
            f"[onesize][alloc] {int(debug_mask.sum())} rows have VAR_REQ>0 + ALLOC=0 + "
            f"not POOL_EMPTY — sample:"
        )
        for _, r in suspicious.iterrows():
            logger.warning(
                f"  RDC={r.get('RDC')} MAJ={r.get(majcat_col)} GEN={r.get(gen_art_col)} "
                f"CLR={r.get(clr_col)} SZ={r.get(sz_col) if sz_col else '?'} "
                f"REQ={r.get('REQ')} VAR_REQ={r.get('VAR_REQ')} "
                f"MSA_REMAIN={r.get('MSA_REMAIN')} POOL_POS={r.get('POOL_POS')} "
                f"DEMAND_SRC={r.get('DEMAND_SRC')}"
            )

    total_alloc = float(alloc_arr.sum())
    stages.append({
        "stage": "compute_allocation",
        "rows":                int(len(cross)),
        "allocated_rows":      allocated_rows,
        "src_both":            src_both,
        "skipped_no_var_req":  skipped_no_var_req,
        "skipped_no_req":      skipped_no_req,
        "skipped_no_demand":   skipped_no_demand,
        "skipped_pool_empty":  skipped_pool_empty,
        "pools_drained":       pools_drained,
        "pools_partial":       pools_partial,
        "total_allocated":     round(total_alloc, 2),
        "formula":             "ALLOC = MIN(VAR_REQ, REQ, MSA_REMAIN); SKIP if VAR_REQ<=0 OR REQ<=0",
        "skip_rule":           "BOTH VAR_REQ > 0 AND REQ > 0 required — either zero → skip, pool untouched",
        "pool_grain":          "(RDC, MAJ_CAT, GEN_ART, CLR, SZ)",
        "unranked_stores":     "processed_last",
        "tie_breaker":         "ST_CD ASC",
    })
    logger.info(
        f"[onesize] allocation done — allocated={allocated_rows}, total_qty={total_alloc:.0f}, "
        f"src_both={src_both}, "
        f"skipped(no_var_req)={skipped_no_var_req}, skipped(no_req)={skipped_no_req}, "
        f"skipped(no_demand)={skipped_no_demand}, drained={pools_drained}, partial={pools_partial}"
    )
    print(
        f"[onesize][alloc][summary] allocated={allocated_rows} (BOTH only), "
        f"skipped(no_var_req)={skipped_no_var_req}, skipped(no_req)={skipped_no_req}, "
        f"skipped(no_demand)={skipped_no_demand}, skipped(empty_pool)={skipped_pool_empty}, "
        f"total_qty={total_alloc:.0f}",
        flush=True,
    )

    # ---- Stage 18b: drop ALLOC=0 rows (POOL_EMPTY / BOTH_CAP_0) ---------
    # After allocation, the only zero-ALLOC rows are pool-drained ones (a
    # higher-ranked store took all the stock). User chose to drop those too,
    # so the final output contains ONLY rows that actually received stock.
    rows_before_alloc_filter = len(cross)
    alloc_n = pd.to_numeric(cross["ALLOC"], errors="coerce").fillna(0)
    cross = cross[alloc_n > 0].copy()
    stages.append({
        "stage":         "drop_alloc_zero",
        "rows_before":   rows_before_alloc_filter,
        "rows":          len(cross),
        "dropped":       rows_before_alloc_filter - len(cross),
        "rule":          "keep iff ALLOC > 0 (drops POOL_EMPTY + BOTH_CAP_0)",
    })
    logger.info(
        f"[onesize] dropped {rows_before_alloc_filter - len(cross)} ALLOC=0 rows "
        f"({rows_before_alloc_filter}→{len(cross)}; pool-drained rows removed)"
    )

    # Reorder columns: preferred first, then everything else (drop internal keys)
    available = list(cross.columns)
    front = [c for c in PREFERRED_COLS if c in available]
    rest = [c for c in available if c not in front and c not in ("id", "sequence_id")]
    cross = cross[front + rest]
    logger.info(f"[onesize] final result columns ({len(cross.columns)}): {cross.columns.tolist()}")
    print(f"[onesize] FINAL COLUMNS: {cross.columns.tolist()}")
    if "RDC" not in cross.columns:
        logger.error(
            f"[onesize] RDC column missing from final result — "
            f"join_mode={join_mode}, msa_rdc={msa_rdc}, st_rdc={st_rdc}, "
            f"stores cols at load={list(stores_df.columns)}"
        )

    return {
        "sequence_id": sequence_id,
        "total_rows": int(len(cross)),
        "columns": cross.columns.tolist(),
        "stores": int(len(stores_df)),
        "stages": stages,
        "_df": cross,  # internal handoff; stripped before returning to client
    }


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------
def _run_job(
    job_id: str,
    rdcs: List[str],
    ssns: List[str],
    preview_limit: int,
    username: Optional[str],
) -> None:
    """Worker thread body. Owns its own DB session (FastAPI's request-scoped
    session can't safely cross threads). Writes status/result back into the
    job dict; never raises out of the thread."""
    from app.database.session import DataSessionLocal

    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        return  # deleted before we even started

    db: Optional[Session] = None
    try:
        db = DataSessionLocal()
        result = _compute_onesize(db, rdcs=rdcs, ssns=ssns, job=job)
        df = result.pop("_df", None)

        # Preview slice + full df cache for CSV export
        preview_rows: List[Dict[str, Any]] = []
        if df is not None and not df.empty and preview_limit > 0:
            head = df.head(preview_limit)
            preview_rows = head.where(pd.notnull(head), None).to_dict("records")

        cache_key = ""
        if df is not None and not df.empty:
            cache_key = _cache_store(
                df,
                {
                    "sequence_id": result.get("sequence_id"),
                    "rdcs": list(rdcs),
                    "ssns": list(ssns),
                    "user": username,
                    "job_id": job_id,
                },
            )

        # ---- Persist to ARS_ONESIZE_ALLOCATION (replace-per-sequence_id) ----
        # Errors here don't fail the job — the compute already succeeded and
        # the df is cached for CSV export. We record persist_error on the job
        # so the UI can surface a warning.
        persist_error: Optional[str] = None
        persisted_rows: Optional[int] = None
        seq_id_for_persist = result.get("sequence_id")
        if df is not None and not df.empty and seq_id_for_persist is not None:
            # Visible to pollers so the UI shows the job is in the save phase
            # (a cancel here raises OneSizeCancelled before we touch the table).
            job["stages"].append({
                "stage": "persist_to_db",
                "table": ALLOCATION_TABLE,
                "rows": int(len(df)),
            })
            try:
                persisted_rows = _persist_onesize_result(
                    db.bind, df,
                    sequence_id=seq_id_for_persist,
                    job_id=job_id,
                    run_by=username,
                )
                logger.info(
                    f"[onesize][job {job_id}] persisted {persisted_rows} rows to "
                    f"{ALLOCATION_TABLE} (sequence_id={seq_id_for_persist})"
                )
            except Exception as e:
                persist_error = f"DB save failed: {str(e)[:400]}"
                logger.exception(f"[onesize][job {job_id}] persistence failed: {e}")

        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if j is None:
                return  # deleted while running
            j["status"]         = "completed"
            j["total_rows"]     = result.get("total_rows", 0)
            j["stores"]         = result.get("stores", 0)
            j["sequence_id"]    = result.get("sequence_id")
            j["columns"]        = result.get("columns", [])
            j["preview_rows"]   = preview_rows
            j["preview_limit"]  = preview_limit
            j["cache_key"]      = cache_key
            j["persist_error"]  = persist_error
            j["persisted_rows"] = persisted_rows
            j["finished_at"]    = time.time()
        logger.info(f"[onesize][job {job_id}] completed — rows={result.get('total_rows', 0)}")
    except OneSizeCancelled:
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if j is not None:
                j["status"]      = "cancelled"
                j["finished_at"] = time.time()
        logger.info(f"[onesize][job {job_id}] cancelled by user")
    except Exception as e:
        logger.exception(f"[onesize][job {job_id}] failed: {e}")
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if j is not None:
                j["status"]      = "failed"
                j["error"]       = str(e)[:1000]
                j["finished_at"] = time.time()
    finally:
        if db is not None:
            try: db.close()
            except Exception: pass


# ---------------------------------------------------------------------------
# Routes — Job lifecycle
# ---------------------------------------------------------------------------
@router.post(
    "/jobs",
    response_model=APIResponse,
    summary="Start a OneSize calculation job",
)
def start_onesize_job(
    preview_limit: int = Query(1000, ge=0, le=50000, description="Rows to include in preview when job finishes"),
    rdc: List[str] = Query(default=[], description="Restrict to these RDCs (empty = all)"),
    ssn: List[str] = Query(default=DEFAULT_SSNS, description="Restrict to these SSNs (empty = all)"),
    current_user: User = Depends(get_current_user),
):
    """Kick off the OneSize pipeline in a background thread. Returns a job_id
    that the client polls via GET /jobs/{job_id}."""
    job_id = uuid.uuid4().hex
    cancel_event = threading.Event()
    job: Dict[str, Any] = {
        "id":            job_id,
        "status":        "running",
        "stages":        _StagesList(cancel_event),
        "cancel_event":  cancel_event,
        "started_at":    time.time(),
        "finished_at":   None,
        "total_rows":    0,
        "stores":        0,
        "sequence_id":   None,
        "columns":       [],
        "preview_rows":  [],
        "preview_limit": preview_limit,
        "cache_key":     "",
        "error":         None,
        "params":        {"rdcs": list(rdc), "ssns": list(ssn)},
        "user":          getattr(current_user, "username", None),
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job
        while len(_JOBS) > _JOBS_MAX_ENTRIES:
            evict_id, evicted = _JOBS.popitem(last=False)
            # Best-effort: if the evicted job is still running, cancel it.
            ev = evicted.get("cancel_event")
            if ev is not None and evicted.get("status") == "running":
                ev.set()
            logger.info(f"[onesize][jobs] LRU-evicted {evict_id} (status={evicted.get('status')})")

    t = threading.Thread(
        target=_run_job,
        name=f"onesize-job-{job_id[:8]}",
        args=(job_id, list(rdc), list(ssn), preview_limit, job["user"]),
        daemon=True,
    )
    job["thread"] = t
    t.start()
    logger.info(f"[onesize][job {job_id}] started (rdcs={list(rdc)}, ssns={list(ssn)})")
    return APIResponse(data=_job_snapshot(job), message=f"OneSize job {job_id} started")


@router.get(
    "/jobs/{job_id}",
    response_model=APIResponse,
    summary="Poll a OneSize job's status / progress / result",
)
def get_onesize_job(
    job_id: str,
    include_preview: bool = Query(True, description="Include preview_rows in the response (only when status=completed)"),
    current_user: User = Depends(get_current_user),
):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        snap = _job_snapshot(job, include_preview=include_preview and job["status"] == "completed")
    return APIResponse(data=snap, message=f"Job {job_id} status={snap['status']}")


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=APIResponse,
    summary="Request cancellation of a running OneSize job",
)
def cancel_onesize_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        if job["status"] != "running":
            return APIResponse(
                data=_job_snapshot(job),
                message=f"Job already in terminal state ({job['status']}); nothing to cancel",
            )
        ev = job.get("cancel_event")
        if ev is not None:
            ev.set()
        snap = _job_snapshot(job)
    logger.info(f"[onesize][job {job_id}] cancel requested")
    return APIResponse(data=snap, message="Cancel signal sent; job will stop at next stage boundary")


@router.delete(
    "/jobs/{job_id}",
    response_model=APIResponse,
    summary="Delete a OneSize job (cancels first if still running)",
)
def delete_onesize_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
):
    with _JOBS_LOCK:
        job = _JOBS.pop(job_id, None)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    # If it was still running, set the cancel event so the worker exits cleanly.
    if job.get("status") == "running":
        ev = job.get("cancel_event")
        if ev is not None:
            ev.set()
    # Free the cache entry too — the df is bound to this job's cache_key.
    ck = job.get("cache_key")
    if ck:
        with _CACHE_LOCK:
            _RESULT_CACHE.pop(ck, None)
    logger.info(f"[onesize][job {job_id}] deleted (was {job.get('status')})")
    return APIResponse(data={"job_id": job_id}, message=f"Job {job_id} deleted")


@router.get(
    "/jobs",
    response_model=APIResponse,
    summary="List recent OneSize jobs",
)
def list_onesize_jobs(
    current_user: User = Depends(get_current_user),
):
    with _JOBS_LOCK:
        snaps = [_job_snapshot(j) for j in _JOBS.values()]
    return APIResponse(data={"jobs": snaps}, message=f"{len(snaps)} jobs")


@router.get(
    "/export.csv",
    summary="Stream OneSize result as CSV",
)
def export_onesize_csv(
    cache_key: str = Query("", description="UUID returned by /run; if present and valid, skip recompute"),
    rdc: List[str] = Query(default=[]),
    ssn: List[str] = Query(default=DEFAULT_SSNS),
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user),
):
    """Stream the OneSize result as a CSV download.

    If `cache_key` is supplied and still valid, the cached dataframe from a
    prior `/run` is streamed directly — no recompute. Otherwise the full
    pipeline runs from scratch (same as /run does internally).
    """
    try:
        df: Optional[pd.DataFrame] = None
        sequence_id: Any = "unknown"
        source = "recomputed"

        if cache_key:
            entry = _cache_get(cache_key)
            if entry is not None:
                df = entry["df"]
                sequence_id = entry["meta"].get("sequence_id", "unknown")
                source = "cache"
                logger.info(f"[onesize][export] cache HIT key={cache_key} rows={len(df)}")
            else:
                logger.info(f"[onesize][export] cache MISS key={cache_key} — recomputing")

        if df is None:
            result = _compute_onesize(db, rdcs=rdc, ssns=ssn)
            df = result.pop("_df", None)
            sequence_id = result.get("sequence_id", "unknown")
            if df is None or df.empty:
                df = pd.DataFrame(columns=result.get("columns") or PREFERRED_COLS)

        buf = io.StringIO()
        df.to_csv(buf, index=False, quoting=csv.QUOTE_MINIMAL)
        buf.seek(0)
        filename = f"onesize_seq{sequence_id}.csv"
        logger.info(
            f"[onesize][export] streaming {len(df)} rows ({source}) → {filename}"
        )
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[onesize] csv export failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/rdcs",
    response_model=APIResponse,
    summary="List RDCs available for the OneSize filter",
)
def list_onesize_rdcs(
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user),
):
    """Return distinct RDC codes from the store master so the UI dropdown
    can offer the same set the join will use."""
    try:
        st_cols = _table_columns(db, ST_MASTER_TABLE)
        if not st_cols:
            return APIResponse(data={"rdcs": []}, message=f"{ST_MASTER_TABLE} not found")
        st_rdc = _resolve_col(st_cols, ["RDC", "WAREHOUSE", "HUB", "WH_CD"])
        if not st_rdc:
            return APIResponse(data={"rdcs": []}, message=f"{ST_MASTER_TABLE} has no RDC column")

        rows = db.execute(
            text(
                f"SELECT DISTINCT LTRIM(RTRIM(CAST([{st_rdc}] AS NVARCHAR(50)))) AS RDC "
                f"FROM [{ST_MASTER_TABLE}] WHERE [{st_rdc}] IS NOT NULL ORDER BY RDC"
            )
        ).fetchall()
        rdcs = [str(r[0]) for r in rows if r[0]]
        return APIResponse(data={"rdcs": rdcs, "rdc_col": st_rdc}, message=f"{len(rdcs)} RDCs")
    except Exception as e:
        logger.error(f"[onesize] list RDCs failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/ssns",
    response_model=APIResponse,
    summary="List SSN (season) codes available for the OneSize filter",
)
def list_onesize_ssns(
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user),
):
    """Return distinct SSN codes from the latest ARS_MSA_TOTAL sequence so the
    UI dropdown offers exactly what the filter can match against."""
    try:
        seq = db.execute(text(f"SELECT MAX(sequence_id) FROM [{MSA_TOTAL_TABLE}]")).scalar()
        if seq is None:
            return APIResponse(
                data={"ssns": [], "defaults": DEFAULT_SSNS},
                message=f"No data in {MSA_TOTAL_TABLE}",
            )

        msa_cols = _table_columns(db, MSA_TOTAL_TABLE)
        if "SSN" not in {c.upper() for c in msa_cols}:
            return APIResponse(
                data={"ssns": [], "defaults": DEFAULT_SSNS},
                message=f"{MSA_TOTAL_TABLE} has no SSN column",
            )

        rows = db.execute(
            text(
                f"SELECT DISTINCT UPPER(LTRIM(RTRIM(CAST([SSN] AS NVARCHAR(50))))) AS SSN "
                f"FROM [{MSA_TOTAL_TABLE}] "
                f"WHERE sequence_id = :sid AND [SSN] IS NOT NULL ORDER BY SSN"
            ),
            {"sid": int(seq)},
        ).fetchall()
        ssns = [str(r[0]) for r in rows if r[0]]
        return APIResponse(
            data={"ssns": ssns, "defaults": DEFAULT_SSNS, "sequence_id": int(seq)},
            message=f"{len(ssns)} SSNs in sequence {int(seq)}",
        )
    except Exception as e:
        logger.error(f"[onesize] list SSNs failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
