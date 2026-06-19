"""
Hold Dashboard endpoints — review HOLD_QTY across multiple angles.

Reads from `ARS_NL_TBL_HOLD_TRACKING` (Part 8.6 hold tracker, grain
WERKS x VAR_ART x SZ) and joins where useful to surface store, RDC, article,
status, age, and timeline views. Mostly read-only; the adhoc clear-hold
endpoints (POST /clear-hold and /clear-hold-file) mutate the tracker and
re-sync MSA HOLD_QTY/FNL_Q so released qty is immediately allocatable.
"""
import csv
import io
import uuid as _uuid
from datetime import datetime, timedelta
from typing import List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text
from loguru import logger

from app.database.session import get_data_db, get_data_engine
from app.security.dependencies import get_current_user
from app.models.rbac import User
from app.schemas.common import APIResponse
from app.services.pend_alc_service import apply_hold_clear, apply_hold_revise, log_operation


router = APIRouter(prefix="/hold-dashboard", tags=["Hold Dashboard"])


HOLD_TABLE = "ARS_NL_TBL_HOLD_TRACKING"
ST_MASTER = "Master_ALC_INPUT_ST_MASTER"
ALLOC_TABLE = "ARS_ALLOC_WORKING"
MSA_VAR_TABLE = "ARS_MSA_VAR_ART"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _table_exists(db: Session, name: str) -> bool:
    try:
        n = db.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :t"
        ), {"t": name}).scalar()
        return bool(n)
    except Exception:
        return False


def _resolve_rdc_col(db: Session) -> Optional[str]:
    """Probe the store-master for the RDC column (varies across envs)."""
    if not _table_exists(db, ST_MASTER):
        return None
    rows = db.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"
    ), {"t": ST_MASTER}).fetchall()
    cols_upper = {str(r[0]).upper() for r in rows}
    for c in ("RDC", "WAREHOUSE", "HUB", "WH_CD"):
        if c in cols_upper:
            return c
    return None


def _empty(reason: str = "ARS_NL_TBL_HOLD_TRACKING is empty or missing"):
    return APIResponse(data={"items": [], "totals": {}, "note": reason})


# ---------------------------------------------------------------------------
# 1. Summary KPIs
# ---------------------------------------------------------------------------
@router.get("/summary", response_model=APIResponse)
def hold_summary(
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user),
):
    """Top-of-page KPI cards.

    Returns counts and totals split by IS_CLOSED, plus the count of distinct
    stores, articles, and the oldest open-hold age in days.
    """
    if not _table_exists(db, HOLD_TABLE):
        return _empty()

    row = db.execute(text(f"""
        SELECT
            SUM(CASE WHEN ISNULL(IS_CLOSED,0)=0 THEN 1 ELSE 0 END)             AS open_rows,
            SUM(CASE WHEN ISNULL(IS_CLOSED,0)=1 THEN 1 ELSE 0 END)             AS closed_rows,
            ISNULL(SUM(CASE WHEN ISNULL(IS_CLOSED,0)=0 THEN HOLD_REM ELSE 0 END), 0)         AS open_qty,
            ISNULL(SUM(CASE WHEN ISNULL(IS_CLOSED,0)=0 THEN HOLD_QTY_INITIAL ELSE 0 END), 0) AS open_initial,
            ISNULL(SUM(CASE WHEN ISNULL(IS_CLOSED,0)=1 THEN HOLD_QTY_INITIAL ELSE 0 END), 0) AS closed_initial,
            COUNT(DISTINCT CASE WHEN ISNULL(IS_CLOSED,0)=0 THEN WERKS END)         AS distinct_stores,
            COUNT(DISTINCT CASE WHEN ISNULL(IS_CLOSED,0)=0 THEN GEN_ART_NUMBER END) AS distinct_articles,
            COUNT(DISTINCT CASE WHEN ISNULL(IS_CLOSED,0)=0
                                THEN CAST(WERKS AS NVARCHAR(50)) + '|' +
                                     CAST(VAR_ART AS NVARCHAR(50)) + '|' +
                                     ISNULL(SZ,'') END)                          AS distinct_skus,
            MAX(CASE WHEN ISNULL(IS_CLOSED,0)=0
                     THEN DATEDIFF(DAY, LISTED_DATE, GETDATE()) END)             AS oldest_open_days,
            MAX(LAST_UPDATED)                                                    AS last_updated
        FROM [{HOLD_TABLE}]
    """)).fetchone()

    consumed_initial = float(row.closed_initial or 0)
    open_initial = float(row.open_initial or 0)
    open_qty = float(row.open_qty or 0)
    consumed_open = max(open_initial - open_qty, 0.0)

    return APIResponse(data={
        "open_rows":          int(row.open_rows or 0),
        "closed_rows":        int(row.closed_rows or 0),
        "open_qty":           open_qty,
        "open_initial":       open_initial,
        "closed_initial":     consumed_initial,
        "consumed_qty":       consumed_initial + consumed_open,  # total ever shipped from holds
        "distinct_stores":    int(row.distinct_stores or 0),
        "distinct_articles":  int(row.distinct_articles or 0),
        "distinct_skus":      int(row.distinct_skus or 0),
        "oldest_open_days":   int(row.oldest_open_days) if row.oldest_open_days is not None else 0,
        "last_updated":       row.last_updated.isoformat() if row.last_updated else None,
    })


# ---------------------------------------------------------------------------
# 2. By Store
# ---------------------------------------------------------------------------
@router.get("/by-store", response_model=APIResponse)
def hold_by_store(
    limit: int = Query(20, ge=1, le=200),
    only_open: bool = Query(True),
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user),
):
    """Top stores by open hold qty (or include closed if only_open=false)."""
    if not _table_exists(db, HOLD_TABLE):
        return _empty()
    where = "WHERE ISNULL(IS_CLOSED,0)=0" if only_open else ""
    rows = db.execute(text(f"""
        SELECT TOP (:lim)
            WERKS,
            COUNT(*)                              AS skus,
            ISNULL(SUM(HOLD_REM), 0)              AS open_qty,
            ISNULL(SUM(HOLD_QTY_INITIAL), 0)      AS initial_qty,
            MAX(DATEDIFF(DAY, LISTED_DATE, GETDATE())) AS oldest_days
        FROM [{HOLD_TABLE}]
        {where}
        GROUP BY WERKS
        ORDER BY open_qty DESC, skus DESC
    """), {"lim": limit}).fetchall()
    return APIResponse(data={"items": [
        {
            "werks":       r.WERKS,
            "skus":        int(r.skus or 0),
            "open_qty":    float(r.open_qty or 0),
            "initial_qty": float(r.initial_qty or 0),
            "oldest_days": int(r.oldest_days or 0),
        } for r in rows
    ]})


# ---------------------------------------------------------------------------
# 3. By RDC (warehouse)
# ---------------------------------------------------------------------------
@router.get("/by-rdc", response_model=APIResponse)
def hold_by_rdc(
    only_open: bool = Query(True),
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user),
):
    """Hold qty grouped by RDC (warehouse). Joins store master to map WERKS→RDC."""
    if not _table_exists(db, HOLD_TABLE):
        return _empty()
    rdc_col = _resolve_rdc_col(db)
    if not rdc_col:
        return APIResponse(data={"items": [], "note":
            f"No RDC column found on {ST_MASTER}; cannot map WERKS to RDC"})
    where = "WHERE ISNULL(H.IS_CLOSED,0)=0" if only_open else ""
    rows = db.execute(text(f"""
        SELECT
            S.[{rdc_col}]                          AS rdc,
            COUNT(DISTINCT H.WERKS)                AS stores,
            COUNT(*)                               AS skus,
            ISNULL(SUM(H.HOLD_REM), 0)             AS open_qty,
            ISNULL(SUM(H.HOLD_QTY_INITIAL), 0)     AS initial_qty
        FROM [{HOLD_TABLE}] H
        LEFT JOIN [{ST_MASTER}] S ON S.[ST_CD] = H.WERKS
        {where}
        GROUP BY S.[{rdc_col}]
        ORDER BY open_qty DESC
    """)).fetchall()
    return APIResponse(data={"items": [
        {
            "rdc":         r.rdc or "(unmapped)",
            "stores":      int(r.stores or 0),
            "skus":        int(r.skus or 0),
            "open_qty":    float(r.open_qty or 0),
            "initial_qty": float(r.initial_qty or 0),
        } for r in rows
    ]})


# ---------------------------------------------------------------------------
# 4. By Article (GEN_ART)
# ---------------------------------------------------------------------------
@router.get("/by-article", response_model=APIResponse)
def hold_by_article(
    limit: int = Query(20, ge=1, le=200),
    only_open: bool = Query(True),
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user),
):
    """Top articles by open hold qty."""
    if not _table_exists(db, HOLD_TABLE):
        return _empty()
    where = "WHERE ISNULL(IS_CLOSED,0)=0" if only_open else ""
    rows = db.execute(text(f"""
        SELECT TOP (:lim)
            GEN_ART_NUMBER,
            MAJ_CAT,
            COUNT(DISTINCT WERKS)                 AS stores,
            COUNT(DISTINCT VAR_ART)               AS variants,
            ISNULL(SUM(HOLD_REM), 0)              AS open_qty,
            ISNULL(SUM(HOLD_QTY_INITIAL), 0)      AS initial_qty
        FROM [{HOLD_TABLE}]
        {where}
        GROUP BY GEN_ART_NUMBER, MAJ_CAT
        ORDER BY open_qty DESC, stores DESC
    """), {"lim": limit}).fetchall()
    return APIResponse(data={"items": [
        {
            "gen_art_number": int(r.GEN_ART_NUMBER) if r.GEN_ART_NUMBER is not None else None,
            "maj_cat":        r.MAJ_CAT,
            "stores":         int(r.stores or 0),
            "variants":       int(r.variants or 0),
            "open_qty":       float(r.open_qty or 0),
            "initial_qty":    float(r.initial_qty or 0),
        } for r in rows
    ]})


# ---------------------------------------------------------------------------
# 5. By Status (NL / TBL / other / NULL)
# ---------------------------------------------------------------------------
@router.get("/by-status", response_model=APIResponse)
def hold_by_status(
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user),
):
    if not _table_exists(db, HOLD_TABLE):
        return _empty()
    rows = db.execute(text(f"""
        SELECT
            ISNULL(NULLIF(LTRIM(RTRIM(OPT_STATUS)),''), '(unset)') AS status,
            SUM(CASE WHEN ISNULL(IS_CLOSED,0)=0 THEN 1 ELSE 0 END)             AS open_rows,
            SUM(CASE WHEN ISNULL(IS_CLOSED,0)=1 THEN 1 ELSE 0 END)             AS closed_rows,
            ISNULL(SUM(CASE WHEN ISNULL(IS_CLOSED,0)=0 THEN HOLD_REM ELSE 0 END), 0)
                                                                               AS open_qty
        FROM [{HOLD_TABLE}]
        GROUP BY ISNULL(NULLIF(LTRIM(RTRIM(OPT_STATUS)),''), '(unset)')
        ORDER BY open_qty DESC
    """)).fetchall()
    return APIResponse(data={"items": [
        {
            "status":      r.status,
            "open_rows":   int(r.open_rows or 0),
            "closed_rows": int(r.closed_rows or 0),
            "open_qty":    float(r.open_qty or 0),
        } for r in rows
    ]})


# ---------------------------------------------------------------------------
# 6. By Age buckets
# ---------------------------------------------------------------------------
AGE_BUCKETS = [
    ("0-7 days",   0,   7),
    ("8-14 days",  8,   14),
    ("15-30 days", 15,  30),
    ("31-60 days", 31,  60),
    ("61-90 days", 61,  90),
    ("90+ days",   91,  None),
]


@router.get("/by-age", response_model=APIResponse)
def hold_by_age(
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user),
):
    """Open holds bucketed by age (days since LISTED_DATE)."""
    if not _table_exists(db, HOLD_TABLE):
        return _empty()
    items = []
    for label, lo, hi in AGE_BUCKETS:
        if hi is None:
            cond = f"DATEDIFF(DAY, LISTED_DATE, GETDATE()) >= {lo}"
        else:
            cond = f"DATEDIFF(DAY, LISTED_DATE, GETDATE()) BETWEEN {lo} AND {hi}"
        row = db.execute(text(f"""
            SELECT
                COUNT(*)                       AS skus,
                ISNULL(SUM(HOLD_REM), 0)       AS open_qty
            FROM [{HOLD_TABLE}]
            WHERE ISNULL(IS_CLOSED,0)=0 AND {cond}
        """)).fetchone()
        items.append({
            "bucket":   label,
            "skus":     int(row.skus or 0),
            "open_qty": float(row.open_qty or 0),
        })
    return APIResponse(data={"items": items})


# ---------------------------------------------------------------------------
# 7. Timeline (last N days)
# ---------------------------------------------------------------------------
@router.get("/timeline", response_model=APIResponse)
def hold_timeline(
    days: int = Query(60, ge=7, le=365),
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user),
):
    """Daily creation (LISTED_DATE) and closure (CLOSED_DATE) counts."""
    if not _table_exists(db, HOLD_TABLE):
        return _empty()
    since = (datetime.utcnow() - timedelta(days=days)).date().isoformat()
    rows = db.execute(text(f"""
        SELECT day, SUM(created) AS created, SUM(closed) AS closed,
               SUM(created_qty) AS created_qty, SUM(closed_qty) AS closed_qty
        FROM (
            SELECT CAST(LISTED_DATE AS DATE) AS day,
                   1 AS created, 0 AS closed,
                   ISNULL(HOLD_QTY_INITIAL, 0) AS created_qty,
                   0 AS closed_qty
            FROM [{HOLD_TABLE}]
            WHERE LISTED_DATE >= :since
            UNION ALL
            SELECT CAST(CLOSED_DATE AS DATE) AS day,
                   0 AS created, 1 AS closed,
                   0 AS created_qty,
                   ISNULL(HOLD_QTY_INITIAL, 0) AS closed_qty
            FROM [{HOLD_TABLE}]
            WHERE CLOSED_DATE IS NOT NULL AND CLOSED_DATE >= :since
        ) t
        WHERE day IS NOT NULL
        GROUP BY day
        ORDER BY day
    """), {"since": since}).fetchall()
    return APIResponse(data={"items": [
        {
            "day":         r.day.isoformat() if hasattr(r.day, "isoformat") else str(r.day),
            "created":     int(r.created or 0),
            "closed":      int(r.closed or 0),
            "created_qty": float(r.created_qty or 0),
            "closed_qty":  float(r.closed_qty or 0),
        } for r in rows
    ]})


# ---------------------------------------------------------------------------
# 8. Detail list (filterable, paginated)
# ---------------------------------------------------------------------------
@router.get("/detail", response_model=APIResponse)
def hold_detail(
    werks: Optional[str] = None,
    rdc: Optional[str] = None,
    gen_art: Optional[int] = None,
    status: Optional[str] = None,
    only_open: bool = Query(True),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user),
):
    """Paginated drill-down list. RDC filter resolves via store master."""
    if not _table_exists(db, HOLD_TABLE):
        return _empty()
    rdc_col = _resolve_rdc_col(db)
    where = []
    params = {}
    if only_open:
        where.append("ISNULL(H.IS_CLOSED, 0) = 0")
    if werks:
        where.append("H.WERKS = :werks")
        params["werks"] = werks
    if gen_art is not None:
        where.append("H.GEN_ART_NUMBER = :gen_art")
        params["gen_art"] = int(gen_art)
    if status:
        where.append("H.OPT_STATUS = :status")
        params["status"] = status
    join = ""
    if rdc and rdc_col:
        join = f"INNER JOIN [{ST_MASTER}] S ON S.[ST_CD] = H.WERKS"
        where.append(f"S.[{rdc_col}] = :rdc")
        params["rdc"] = rdc
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    total = db.execute(text(f"""
        SELECT COUNT(*) FROM [{HOLD_TABLE}] H {join} {where_clause}
    """), params).scalar() or 0

    offset = (page - 1) * page_size
    params_pg = {**params, "offset": offset, "page_size": page_size}
    # LAST_REMARKS / LAST_UPDATED_BY may be missing on older deployments —
    # the adhoc clear/revise endpoints add them lazily. Guard the SELECT
    # so the dashboard keeps working before either endpoint has been hit.
    has_audit_cols = bool(db.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME = :t AND COLUMN_NAME = 'LAST_REMARKS'"
    ), {"t": HOLD_TABLE}).scalar() or 0)
    audit_select = (
        ", H.LAST_REMARKS, H.LAST_UPDATED_BY"
        if has_audit_cols
        else ", CAST(NULL AS NVARCHAR(500)) AS LAST_REMARKS, "
             "  CAST(NULL AS NVARCHAR(100)) AS LAST_UPDATED_BY"
    )
    rows = db.execute(text(f"""
        SELECT
            H.WERKS, H.MAJ_CAT, H.GEN_ART_NUMBER, H.CLR, H.VAR_ART, H.SZ,
            H.OPT_STATUS, H.LISTED_DATE, H.HOLD_QTY_INITIAL, H.HOLD_REM,
            H.LAST_UPDATED, H.IS_CLOSED, H.CLOSED_DATE,
            DATEDIFF(DAY, H.LISTED_DATE, GETDATE()) AS age_days
            {audit_select}
        FROM [{HOLD_TABLE}] H
        {join}
        {where_clause}
        ORDER BY H.IS_CLOSED ASC, H.HOLD_REM DESC, H.LISTED_DATE DESC
        OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY
    """), params_pg).fetchall()

    return APIResponse(data={
        "total":     int(total),
        "page":      page,
        "page_size": page_size,
        "items": [
            {
                "werks":            r.WERKS,
                "maj_cat":          r.MAJ_CAT,
                "gen_art_number":   int(r.GEN_ART_NUMBER) if r.GEN_ART_NUMBER is not None else None,
                "clr":              r.CLR,
                "var_art":          int(r.VAR_ART) if r.VAR_ART is not None else None,
                "sz":               r.SZ,
                "opt_status":       r.OPT_STATUS,
                "listed_date":      r.LISTED_DATE.isoformat() if r.LISTED_DATE else None,
                "hold_qty_initial": float(r.HOLD_QTY_INITIAL or 0),
                "hold_rem":         float(r.HOLD_REM or 0),
                "last_updated":     r.LAST_UPDATED.isoformat() if r.LAST_UPDATED else None,
                "is_closed":        bool(r.IS_CLOSED),
                "closed_date":      r.CLOSED_DATE.isoformat() if r.CLOSED_DATE else None,
                "age_days":         int(r.age_days or 0),
                "last_remarks":     r.LAST_REMARKS,
                "last_updated_by":  r.LAST_UPDATED_BY,
            } for r in rows
        ]
    })


# ---------------------------------------------------------------------------
# 8b. Detail export — streamed CSV of the same filtered result set.
# ---------------------------------------------------------------------------
EXPORT_MAX_ROWS = 500_000  # safety cap


@router.get("/detail/export")
def hold_detail_export(
    werks: Optional[str] = None,
    rdc: Optional[str] = None,
    gen_art: Optional[int] = None,
    status: Optional[str] = None,
    only_open: bool = Query(True),
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user),
):
    """CSV export of the drill-down, honouring the same filters as /detail.
    Defaults to only_open=True so the user grabs the open rows to work on.
    """
    if not _table_exists(db, HOLD_TABLE):
        raise HTTPException(404, "Hold tracker table is empty or missing")
    rdc_col = _resolve_rdc_col(db)
    where = []
    params = {}
    if only_open:
        where.append("ISNULL(H.IS_CLOSED, 0) = 0")
    if werks:
        where.append("H.WERKS = :werks")
        params["werks"] = werks
    if gen_art is not None:
        where.append("H.GEN_ART_NUMBER = :gen_art")
        params["gen_art"] = int(gen_art)
    if status:
        where.append("H.OPT_STATUS = :status")
        params["status"] = status
    join = ""
    if rdc and rdc_col:
        join = f"INNER JOIN [{ST_MASTER}] S ON S.[ST_CD] = H.WERKS"
        where.append(f"S.[{rdc_col}] = :rdc")
        params["rdc"] = rdc
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    has_audit_cols = bool(db.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME = :t AND COLUMN_NAME = 'LAST_REMARKS'"
    ), {"t": HOLD_TABLE}).scalar() or 0)
    audit_select = (
        ", H.LAST_REMARKS, H.LAST_UPDATED_BY"
        if has_audit_cols
        else ", CAST(NULL AS NVARCHAR(500)) AS LAST_REMARKS, "
             "  CAST(NULL AS NVARCHAR(100)) AS LAST_UPDATED_BY"
    )

    sql = text(f"""
        SELECT TOP ({EXPORT_MAX_ROWS})
            H.WERKS, H.MAJ_CAT, H.GEN_ART_NUMBER, H.CLR, H.VAR_ART, H.SZ,
            H.OPT_STATUS, H.LISTED_DATE, H.HOLD_QTY_INITIAL, H.HOLD_REM,
            DATEDIFF(DAY, H.LISTED_DATE, GETDATE()) AS AGE_DAYS,
            H.IS_CLOSED, H.CLOSED_DATE, H.LAST_UPDATED
            {audit_select}
        FROM [{HOLD_TABLE}] H
        {join}
        {where_clause}
        ORDER BY H.IS_CLOSED ASC, H.HOLD_REM DESC, H.LISTED_DATE DESC
    """)

    header = [
        "WERKS", "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "VAR_ART", "SZ",
        "OPT_STATUS", "LISTED_DATE", "HOLD_QTY_INITIAL", "HOLD_REM",
        "AGE_DAYS", "IS_CLOSED", "CLOSED_DATE", "LAST_UPDATED",
        "LAST_REMARKS", "LAST_UPDATED_BY",
    ]

    def _iter_csv():
        # Stream row-by-row so 100K+ rows don't materialise in memory at once.
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(header)
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)

        result = db.execute(sql, params)
        for r in result:
            writer.writerow([
                r.WERKS or "",
                r.MAJ_CAT or "",
                int(r.GEN_ART_NUMBER) if r.GEN_ART_NUMBER is not None else "",
                r.CLR or "",
                int(r.VAR_ART) if r.VAR_ART is not None else "",
                r.SZ or "",
                r.OPT_STATUS or "",
                r.LISTED_DATE.isoformat() if r.LISTED_DATE else "",
                float(r.HOLD_QTY_INITIAL or 0),
                float(r.HOLD_REM or 0),
                int(r.AGE_DAYS or 0),
                1 if r.IS_CLOSED else 0,
                r.CLOSED_DATE.isoformat() if r.CLOSED_DATE else "",
                r.LAST_UPDATED.isoformat() if r.LAST_UPDATED else "",
                (r.LAST_REMARKS or "") if hasattr(r, "LAST_REMARKS") else "",
                (r.LAST_UPDATED_BY or "") if hasattr(r, "LAST_UPDATED_BY") else "",
            ])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)

    suffix = "open" if only_open else "all"
    fname = f"hold_detail_{suffix}_{datetime.utcnow():%Y%m%d_%H%M%S}.csv"
    return StreamingResponse(
        _iter_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---------------------------------------------------------------------------
# 9. Reconciliation — tracker vs latest run vs MSA
# ---------------------------------------------------------------------------
@router.get("/reconciliation", response_model=APIResponse)
def hold_reconciliation(
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user),
):
    """Cross-check open HOLD_REM vs the last run's HOLD_QTY in
    ARS_ALLOC_WORKING and the HOLD_QTY column in ARS_MSA_VAR_ART (added by
    MSA Step 6.5). Highlights drift the user should investigate.
    """
    out = {}
    if _table_exists(db, HOLD_TABLE):
        out["tracker_open_qty"] = float(db.execute(text(
            f"SELECT ISNULL(SUM(HOLD_REM), 0) FROM [{HOLD_TABLE}] "
            f"WHERE ISNULL(IS_CLOSED, 0) = 0"
        )).scalar() or 0)
    else:
        out["tracker_open_qty"] = None

    if _table_exists(db, ALLOC_TABLE):
        out["latest_run_hold_qty"] = float(db.execute(text(
            f"SELECT ISNULL(SUM(HOLD_QTY), 0) FROM [{ALLOC_TABLE}] "
            f"WHERE ISNULL(HOLD_QTY, 0) > 0"
        )).scalar() or 0)
    else:
        out["latest_run_hold_qty"] = None

    if _table_exists(db, MSA_VAR_TABLE):
        # HOLD_QTY column may not yet exist on a fresh DB — guard the query
        try:
            out["msa_hold_qty"] = float(db.execute(text(
                f"SELECT ISNULL(SUM(HOLD_QTY), 0) FROM [{MSA_VAR_TABLE}]"
            )).scalar() or 0)
        except Exception as e:
            out["msa_hold_qty"] = None
            out["msa_hold_qty_error"] = str(e)[:200]
    else:
        out["msa_hold_qty"] = None

    # Drift = tracker vs MSA. Should be small once Step 6.5 has run on the
    # latest sequence and the tracker is up to date.
    if out["tracker_open_qty"] is not None and out["msa_hold_qty"] is not None:
        out["tracker_vs_msa_drift"] = round(out["tracker_open_qty"] - out["msa_hold_qty"], 2)
    return APIResponse(data=out)


# ---------------------------------------------------------------------------
# Adhoc clear-hold — release qty from the tracker + push the change into MSA
# (HOLD_QTY/FNL_Q) so it lands in the next allocation without waiting for a
# full MSA run. Symmetric with PEND_ALC's adhoc-close.
# ---------------------------------------------------------------------------
class ClearHoldRow(BaseModel):
    werks: str
    var_art: str           # accept string to preserve leading zeros / big ints
    sz: Optional[str] = "" # blank SZ is a valid key in the tracker
    release_qty: Optional[float] = None


class ClearHoldRequest(BaseModel):
    rows: List[ClearHoldRow]
    reason: Optional[str] = None


@router.post("/clear-hold")
def clear_hold(
    body: ClearHoldRequest,
    current_user: User = Depends(get_current_user),
):
    """Cancel or release stock from one or more hold rows.

    Per-row `release_qty` semantics:
      * omitted / ≥ current HOLD_REM → full close (HOLD_REM = 0, IS_CLOSED = 1).
      * 0 < release_qty < HOLD_REM    → partial release (HOLD_REM -= release_qty).

    Also re-aggregates MSA HOLD_QTY from the tracker for the affected
    (RDC, ARTICLE) keys and recomputes FNL_Q, so the released qty is
    immediately allocatable in the next run.

    Logged to ARS_PEND_ALC_OPERATIONS as OP_TYPE='HOLD_CLEAR' for audit.
    """
    rows = [r.model_dump() for r in body.rows]
    if not rows:
        raise HTTPException(400, "rows is empty")
    clean_reason = (body.reason or "").strip()
    if not clean_reason:
        raise HTTPException(400, "Reason is required")
    try:
        username = getattr(current_user, "username", None)
        op_key = _uuid.uuid4().hex[:12]
        with get_data_engine().connect() as conn:
            res = apply_hold_clear(
                conn, rows,
                reason=clean_reason,
                created_by=username,
            )
            log_operation(
                conn,
                op_type="HOLD_CLEAR",
                op_key=op_key,
                payload={
                    "reason":        clean_reason,
                    "input_rows":    rows,
                    "hold_updates":  res["hold_updates"],
                    "msa_total":     res["msa_total"],
                    "msa_var_art":   res["msa_var_art"],
                    "msa_gen_art":   res["msa_gen_art"],
                },
                summary=(
                    f"Hold clear {op_key}: {res['touched_hold']} row(s) "
                    f"updated, msa_total={res['msa_total']} — {clean_reason}"
                ),
                rows_affected=res["touched_hold"],
                qty_total=sum(
                    (u["old_hold_rem"] - u["new_hold_rem"])
                    for u in res["hold_updates"]
                ),
                created_by=username,
            )
        return {
            "success":             True,
            "op_key":               op_key,
            "input_rows":           len(rows),
            "hold_rows_updated":    res["touched_hold"],
            "msa_total_updated":    res["msa_total"],
            "msa_var_art_updated":  res["msa_var_art"],
            "msa_gen_art_updated":  res["msa_gen_art"],
            "qty_released":         sum(
                (u["old_hold_rem"] - u["new_hold_rem"])
                for u in res["hold_updates"]
            ),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[hold-dashboard] clear-hold failed: {e}")
        raise HTTPException(500, str(e))


@router.post("/clear-hold-file")
async def clear_hold_file(
    file: UploadFile = File(..., description="CSV/Excel: WERKS, VAR_ART, [SZ], [RELEASE_QTY]"),
    reason: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Bulk adhoc clear-hold from a CSV / Excel file.

    Required columns:  WERKS, VAR_ART
    Optional columns:  SZ, RELEASE_QTY
    Top-level `reason` query param is used for the audit summary.
    """
    try:
        content = await file.read()
        if not content:
            raise HTTPException(400, "File is empty")
        lower = (file.filename or "").lower()
        if lower.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content), dtype=str)
        elif lower.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(content), dtype=str)
        else:
            raise HTTPException(400, "Unsupported file — use CSV or Excel")
        if df.empty:
            raise HTTPException(400, "File contains no data rows")
        # Empty cells in mixed columns come back as NaN; force to '' so the
        # SQL match isn't poisoned by str(NaN) == 'nan'.
        df = df.fillna("")
        df.columns = [str(c).strip().upper() for c in df.columns]
        if "WERKS" not in df.columns or "VAR_ART" not in df.columns:
            raise HTTPException(400, "Missing required columns: WERKS, VAR_ART")
        clean_reason_top = (reason or "").strip()
        if not clean_reason_top:
            raise HTTPException(400, "Reason is required")

        rows: List[dict] = []
        for _, row in df.iterrows():
            werks   = str(row.get("WERKS")   or "").strip()
            var_art = str(row.get("VAR_ART") or "").strip().split(".")[0]
            if not werks or not var_art:
                continue
            sz      = str(row.get("SZ") or "").strip() if "SZ" in df.columns else ""
            rq_raw  = str(row.get("RELEASE_QTY") or "").strip() \
                      if "RELEASE_QTY" in df.columns else ""
            try:
                rq = float(rq_raw) if rq_raw else None
            except ValueError:
                rq = None
            rows.append({"werks": werks, "var_art": var_art, "sz": sz,
                         "release_qty": rq})
        if not rows:
            raise HTTPException(400, "No valid rows after parsing")

        username = getattr(current_user, "username", None)
        op_key = _uuid.uuid4().hex[:12]
        clean_reason = clean_reason_top
        with get_data_engine().connect() as conn:
            res = apply_hold_clear(conn, rows, reason=clean_reason,
                                   created_by=username)
            log_operation(
                conn,
                op_type="HOLD_CLEAR",
                op_key=op_key,
                payload={
                    "reason":        clean_reason,
                    "filename":      file.filename,
                    "input_rows":    rows,
                    "hold_updates":  res["hold_updates"],
                    "msa_total":     res["msa_total"],
                    "msa_var_art":   res["msa_var_art"],
                    "msa_gen_art":   res["msa_gen_art"],
                },
                summary=(
                    f"Hold clear {op_key} (file {file.filename}): "
                    f"{res['touched_hold']} row(s) updated"
                    + (f" — {clean_reason}" if clean_reason else "")
                ),
                rows_affected=res["touched_hold"],
                qty_total=sum(
                    (u["old_hold_rem"] - u["new_hold_rem"])
                    for u in res["hold_updates"]
                ),
                created_by=username,
            )
        return {
            "success":             True,
            "op_key":               op_key,
            "input_rows":           len(rows),
            "hold_rows_updated":    res["touched_hold"],
            "msa_total_updated":    res["msa_total"],
            "msa_var_art_updated":  res["msa_var_art"],
            "msa_gen_art_updated":  res["msa_gen_art"],
            "qty_released":         sum(
                (u["old_hold_rem"] - u["new_hold_rem"])
                for u in res["hold_updates"]
            ),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[hold-dashboard] clear-hold-file failed: {e}")
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Adhoc revise-hold — increase HOLD_REM (and HOLD_QTY_INITIAL) on existing
# tracker rows. Re-opens a previously-closed row if matched.
# ---------------------------------------------------------------------------
class ReviseHoldRow(BaseModel):
    werks:   str
    var_art: str
    sz:      Optional[str] = ""
    add_qty: float


class ReviseHoldRequest(BaseModel):
    rows:   List[ReviseHoldRow]
    reason: Optional[str] = None


@router.post("/revise-hold")
def revise_hold(
    body: ReviseHoldRequest,
    current_user: User = Depends(get_current_user),
):
    """Increase HOLD_REM + HOLD_QTY_INITIAL on matching tracker rows.

    Blank `sz` matches every size for that (WERKS, VAR_ART) — same as
    clear-hold. A closed row is re-opened with `add_qty` as the new
    initial.

    MSA HOLD_QTY/FNL_Q is re-synced for affected (RDC, ARTICLE). Logged as
    OP_TYPE='HOLD_REVISE'.
    """
    rows = [r.model_dump() for r in body.rows]
    if not rows:
        raise HTTPException(400, "rows is empty")
    clean_reason = (body.reason or "").strip()
    if not clean_reason:
        raise HTTPException(400, "Reason is required")
    try:
        username = getattr(current_user, "username", None)
        op_key = _uuid.uuid4().hex[:12]
        with get_data_engine().connect() as conn:
            res = apply_hold_revise(
                conn, rows,
                reason=clean_reason,
                created_by=username,
            )
            qty_added = sum(
                (u["new_hold_rem"] - u["old_hold_rem"])
                for u in res["hold_updates"]
            )
            log_operation(
                conn,
                op_type="HOLD_REVISE",
                op_key=op_key,
                payload={
                    "reason":        clean_reason,
                    "input_rows":    rows,
                    "hold_updates":  res["hold_updates"],
                    "msa_total":     res["msa_total"],
                    "msa_var_art":   res["msa_var_art"],
                    "msa_gen_art":   res["msa_gen_art"],
                },
                summary=(
                    f"Hold revise {op_key}: {res['touched_hold']} row(s) "
                    f"updated, +{qty_added} units — {clean_reason}"
                ),
                rows_affected=res["touched_hold"],
                qty_total=qty_added,
                created_by=username,
            )
        return {
            "success":             True,
            "op_key":              op_key,
            "input_rows":          len(rows),
            "hold_rows_updated":   res["touched_hold"],
            "msa_total_updated":   res["msa_total"],
            "msa_var_art_updated": res["msa_var_art"],
            "msa_gen_art_updated": res["msa_gen_art"],
            "qty_added":           qty_added,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[hold-dashboard] revise-hold failed: {e}")
        raise HTTPException(500, str(e))


@router.post("/revise-hold-file")
async def revise_hold_file(
    file: UploadFile = File(..., description="CSV/Excel: WERKS, VAR_ART, ADD_QTY, [SZ]"),
    reason: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Bulk revise-hold via CSV/Excel.

    Required columns:  WERKS, VAR_ART, ADD_QTY
    Optional columns:  SZ
    Each row adds ADD_QTY to the matching tracker row(s). Blank SZ matches
    every size for that (WERKS, VAR_ART).
    """
    try:
        content = await file.read()
        if not content:
            raise HTTPException(400, "File is empty")
        lower = (file.filename or "").lower()
        if lower.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content), dtype=str)
        elif lower.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(content), dtype=str)
        else:
            raise HTTPException(400, "Unsupported file — use CSV or Excel")
        if df.empty:
            raise HTTPException(400, "File contains no data rows")
        df = df.fillna("")
        df.columns = [str(c).strip().upper() for c in df.columns]
        for required in ("WERKS", "VAR_ART", "ADD_QTY"):
            if required not in df.columns:
                raise HTTPException(400, f"Missing required column: {required}")
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise HTTPException(400, "Reason is required")

        rows: List[dict] = []
        for _, row in df.iterrows():
            werks   = str(row.get("WERKS")   or "").strip()
            var_art = str(row.get("VAR_ART") or "").strip().split(".")[0]
            add_raw = str(row.get("ADD_QTY") or "").strip()
            if not werks or not var_art or not add_raw:
                continue
            try:
                add_qty = float(add_raw)
            except ValueError:
                continue
            if add_qty <= 0:
                continue
            sz = str(row.get("SZ") or "").strip() if "SZ" in df.columns else ""
            rows.append({"werks": werks, "var_art": var_art, "sz": sz,
                         "add_qty": add_qty})
        if not rows:
            raise HTTPException(400, "No valid rows after parsing (ADD_QTY must be > 0)")

        username = getattr(current_user, "username", None)
        op_key = _uuid.uuid4().hex[:12]
        with get_data_engine().connect() as conn:
            res = apply_hold_revise(conn, rows, reason=clean_reason,
                                    created_by=username)
            qty_added = sum(
                (u["new_hold_rem"] - u["old_hold_rem"])
                for u in res["hold_updates"]
            )
            log_operation(
                conn,
                op_type="HOLD_REVISE",
                op_key=op_key,
                payload={
                    "reason":        clean_reason,
                    "filename":      file.filename,
                    "input_rows":    rows,
                    "hold_updates":  res["hold_updates"],
                    "msa_total":     res["msa_total"],
                    "msa_var_art":   res["msa_var_art"],
                    "msa_gen_art":   res["msa_gen_art"],
                },
                summary=(
                    f"Hold revise {op_key} (file {file.filename}): "
                    f"{res['touched_hold']} row(s) updated, +{qty_added} units"
                    + (f" — {clean_reason}" if clean_reason else "")
                ),
                rows_affected=res["touched_hold"],
                qty_total=qty_added,
                created_by=username,
            )
        return {
            "success":             True,
            "op_key":              op_key,
            "input_rows":          len(rows),
            "hold_rows_updated":   res["touched_hold"],
            "msa_total_updated":   res["msa_total"],
            "msa_var_art_updated": res["msa_var_art"],
            "msa_gen_art_updated": res["msa_gen_art"],
            "qty_added":           qty_added,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[hold-dashboard] revise-hold-file failed: {e}")
        raise HTTPException(500, str(e))
