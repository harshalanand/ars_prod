"""
pend_alc.py
Endpoints for managing ARS_PEND_ALC — the ARS-sourced pending allocation table.

Routes:
    GET  /pend-alc/summary          — totals + breakdown by MODE and SOURCE
    GET  /pend-alc/sessions         — sessions with open pending qty
    GET  /pend-alc/detail           — row-level data (paginated)
    GET  /pend-alc/do-history       — recent DO deduction events
    POST /pend-alc/do-update        — record DO qty from SAP (with optional DO_NUMBER)
    GET  /pend-alc/bdc-preview      — preview open rows to be included in next BDC
    POST /pend-alc/bdc-generate     — stamp BDC_QTY on open rows + return Excel
    POST /pend-alc/manual-upload    — insert manually allocated rows (SOURCE=MANUAL)
    GET  /pend-alc/reco             — full reco view with aging
    GET  /pend-alc/reco-summary     — aggregated tiles (by mode, source, aging)

MSA adjustment is automatic — fires after every PEND_ALC INSERT (manual upload
or approve_parked). NOT after DO updates (STK_QTY in MSA is daily snapshot;
patching FNL_Q after DO would over-state the pool).
"""
from __future__ import annotations

import io
from typing import List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from loguru import logger

from app.database.session import get_data_engine
from app.security.dependencies import get_current_user
from app.models.rbac import User
from app.services.pend_alc_service import (
    adjust_msa_after_pend_insert,
    apply_pend_alc_delta,
    apply_do_deductions,
    backfill_bdc_operations,
    delete_schedule,
    ensure_bdc_history_table,
    ensure_pend_alc_table,
    get_stores_for_date,
    insert_bdc_history,
    list_operations,
    list_schedule_audit,
    list_schedules,
    log_operation,
    preview_revert,
    recover_orphan_bdc_stamps,
    revert_operation,
    stamp_bdc_qty,
    update_bdc_history_with_do,
    upsert_schedules,
    write_manual_pend_alc,
    BDC_HISTORY_TABLE,
    OPERATIONS_TABLE,
    PEND_ALC_TABLE,
    SCHEDULE_AUDIT_TABLE,
    SCHEDULE_TABLE,
)

router = APIRouter(prefix="/pend-alc", tags=["Pending Allocation"])


def _engine():
    return get_data_engine()


# ---------------------------------------------------------------------------
# GET /pend-alc/summary
# ---------------------------------------------------------------------------
@router.get("/summary")
def pend_alc_summary(current_user: User = Depends(get_current_user)):
    """Totals + breakdown by ALLOC_MODE and SOURCE."""
    try:
        with _engine().connect() as conn:
            ensure_pend_alc_table(conn)
            row = conn.execute(text(f"""
                SELECT COUNT(*)                                         AS total_rows,
                       SUM(ALLOC_QTY)                                   AS total_alloc,
                       SUM(BDC_QTY)                                     AS total_bdc,
                       SUM(DO_QTY)                                      AS total_do,
                       SUM(PEND_QTY)                                    AS total_pend,
                       SUM(CASE WHEN IS_CLOSED=1 THEN 1 ELSE 0 END)     AS closed_rows,
                       SUM(CASE WHEN IS_CLOSED=0 THEN 1 ELSE 0 END)     AS open_rows
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
            """)).fetchone()

            by_mode = conn.execute(text(f"""
                SELECT ISNULL(ALLOC_MODE,'AUTO')     AS mode,
                       SUM(ALLOC_QTY) AS alloc_qty,
                       SUM(BDC_QTY)  AS bdc_qty,
                       SUM(DO_QTY)   AS do_qty,
                       SUM(PEND_QTY) AS pend_qty,
                       COUNT(*)      AS rows
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                WHERE IS_CLOSED = 0
                GROUP BY ISNULL(ALLOC_MODE,'AUTO')
                ORDER BY SUM(PEND_QTY) DESC
            """)).fetchall()

            by_source = conn.execute(text(f"""
                SELECT ISNULL(SOURCE,'AUTO')          AS source,
                       SUM(ALLOC_QTY) AS alloc_qty,
                       SUM(BDC_QTY)   AS bdc_qty,
                       SUM(DO_QTY)    AS do_qty,
                       SUM(PEND_QTY)  AS pend_qty,
                       COUNT(*)       AS rows
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                WHERE IS_CLOSED = 0
                GROUP BY ISNULL(SOURCE,'AUTO')
                ORDER BY SUM(PEND_QTY) DESC
            """)).fetchall()

            by_majcat = conn.execute(text(f"""
                SELECT MAJ_CAT,
                       SUM(ALLOC_QTY) AS alloc_qty,
                       SUM(BDC_QTY)   AS bdc_qty,
                       SUM(DO_QTY)    AS do_qty,
                       SUM(PEND_QTY)  AS pend_qty,
                       COUNT(*)       AS rows
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                WHERE IS_CLOSED = 0
                GROUP BY MAJ_CAT
                ORDER BY SUM(PEND_QTY) DESC
            """)).fetchall()

        return {
            "success": True,
            "data": {
                "totals": {
                    "total_rows":  int(row[0] or 0),
                    "total_alloc": float(row[1] or 0),
                    "total_bdc":   float(row[2] or 0),
                    "total_do":    float(row[3] or 0),
                    "total_pend":  float(row[4] or 0),
                    "closed_rows": int(row[5] or 0),
                    "open_rows":   int(row[6] or 0),
                    "pct_bdc_covered": round(
                        100 * float(row[3] or 0) / max(float(row[2] or 1), 1), 1
                    ),
                },
                "by_mode": [
                    {"mode": r[0], "alloc_qty": float(r[1] or 0),
                     "bdc_qty": float(r[2] or 0), "do_qty": float(r[3] or 0),
                     "pend_qty": float(r[4] or 0), "rows": int(r[5] or 0)}
                    for r in by_mode
                ],
                "by_source": [
                    {"source": r[0], "alloc_qty": float(r[1] or 0),
                     "bdc_qty": float(r[2] or 0), "do_qty": float(r[3] or 0),
                     "pend_qty": float(r[4] or 0), "rows": int(r[5] or 0)}
                    for r in by_source
                ],
                "by_majcat": [
                    {"maj_cat": r[0], "alloc_qty": float(r[1] or 0),
                     "bdc_qty": float(r[2] or 0), "do_qty": float(r[3] or 0),
                     "pend_qty": float(r[4] or 0), "rows": int(r[5] or 0)}
                    for r in by_majcat
                ],
            },
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# GET /pend-alc/sessions
# ---------------------------------------------------------------------------
@router.get("/sessions")
def pend_alc_sessions(current_user: User = Depends(get_current_user)):
    """Sessions that still have open pending quantities, with mode breakdown."""
    try:
        with _engine().connect() as conn:
            ensure_pend_alc_table(conn)
            rows = conn.execute(text(f"""
                SELECT SESSION_ID,
                       MIN(APPROVED_AT)                   AS approved_at,
                       ISNULL(SOURCE,'AUTO')               AS source,
                       SUM(ALLOC_QTY)                     AS alloc_qty,
                       SUM(BDC_QTY)                       AS bdc_qty,
                       SUM(DO_QTY)                        AS do_qty,
                       SUM(PEND_QTY)                      AS pend_qty,
                       COUNT(*)                           AS article_count
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                WHERE IS_CLOSED = 0
                GROUP BY SESSION_ID, ISNULL(SOURCE,'AUTO')
                ORDER BY MIN(APPROVED_AT) DESC
            """)).fetchall()
        return {
            "success": True,
            "data": [
                {
                    "session_id":    r[0],
                    "approved_at":   r[1].isoformat() if r[1] else None,
                    "source":        r[2],
                    "alloc_qty":     float(r[3] or 0),
                    "bdc_qty":       float(r[4] or 0),
                    "do_qty":        float(r[5] or 0),
                    "pend_qty":      float(r[6] or 0),
                    "article_count": int(r[7] or 0),
                }
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# GET /pend-alc/detail
# ---------------------------------------------------------------------------
_DETAIL_SORTABLE = {
    "id":             "P.ID",
    "session_id":     "P.SESSION_ID",
    "rdc":            "P.RDC",
    "st_cd":          "P.ST_CD",
    "article_number": "P.ARTICLE_NUMBER",
    "maj_cat":        "P.MAJ_CAT",
    "gen_art_number": "P.GEN_ART_NUMBER",
    "clr":            "P.CLR",
    "alloc_mode":     "P.ALLOC_MODE",
    "source":         "P.SOURCE",
    "alloc_qty":      "P.ALLOC_QTY",
    "bdc_qty":        "P.BDC_QTY",
    "do_qty":         "P.DO_QTY",
    "pend_qty":       "P.PEND_QTY",
    "approved_at":    "P.APPROVED_AT",
    "last_bdc_at":    "P.LAST_BDC_AT",
    "is_closed":      "P.IS_CLOSED",
    "bdc_alloc_no":   "B.ALLOCATION_NUMBER",
    "bdc_status":     "B.STATUS",
}


@router.get("/detail")
def pend_alc_detail(
    session_id:   Optional[str]  = Query(None),
    maj_cat:      Optional[str]  = Query(None),
    alloc_mode:   Optional[str]  = Query(None),
    source:       Optional[str]  = Query(None),
    closed:       Optional[bool] = Query(None),
    # Pagination
    page:         int            = Query(1,   ge=1),
    page_size:    int            = Query(100, ge=1, le=10000),
    # Sort
    sort_by:      Optional[str]  = Query(None),
    sort_dir:     str            = Query("desc", regex="^(asc|desc)$"),
    # Per-column multi-value filters
    f_rdc:        Optional[str] = Query(None),
    f_st_cd:      Optional[str] = Query(None),
    f_maj_cat:    Optional[str] = Query(None),
    f_alloc_mode: Optional[str] = Query(None),
    f_source:     Optional[str] = Query(None),
    f_bdc_status: Optional[str] = Query(None),
    q_article:    Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
):
    """Row-level ARS_PEND_ALC data, paged + sortable + filterable, joined
    with the latest BDC history row per (RDC, ST_CD, ARTICLE)."""
    try:
        filters: List[str] = ["1=1"]
        params: dict = {}
        if session_id:
            filters.append("P.SESSION_ID = :sid"); params["sid"] = session_id
        if maj_cat:
            filters.append("P.MAJ_CAT = :mc");     params["mc"]  = maj_cat
        if alloc_mode:
            filters.append("P.ALLOC_MODE = :am");  params["am"]  = alloc_mode
        if source:
            filters.append("P.SOURCE = :src");     params["src"] = source
        if closed is not None:
            filters.append("P.IS_CLOSED = :cl");   params["cl"]  = 1 if closed else 0

        def _multi(col, csv, prefix):
            vals = _parse_csv_filter(csv)
            if not vals: return
            phs = ",".join(f":{prefix}{i}" for i in range(len(vals)))
            filters.append(f"{col} IN ({phs})")
            for i, v in enumerate(vals):
                params[f"{prefix}{i}"] = v
        _multi("P.RDC",                 f_rdc,        "frdc")
        _multi("ISNULL(P.ST_CD,'')",    f_st_cd,      "fst")
        _multi("P.MAJ_CAT",             f_maj_cat,    "fmc")
        _multi("P.ALLOC_MODE",          f_alloc_mode, "fam")
        _multi("P.SOURCE",              f_source,     "fsrc")

        if q_article:
            filters.append("P.ARTICLE_NUMBER LIKE :qart")
            params["qart"] = f"%{q_article}%"

        bdc_status_vals = _parse_csv_filter(f_bdc_status)
        if bdc_status_vals:
            real_statuses = [v for v in bdc_status_vals if v != "NEVER_SENT"]
            include_never = "NEVER_SENT" in bdc_status_vals
            cond_parts = []
            if real_statuses:
                phs = ",".join(f":fbst{i}" for i in range(len(real_statuses)))
                cond_parts.append(f"B.STATUS IN ({phs})")
                for i, v in enumerate(real_statuses):
                    params[f"fbst{i}"] = v
            if include_never:
                cond_parts.append("B.STATUS IS NULL")
            filters.append("(" + " OR ".join(cond_parts) + ")")

        where = "WHERE " + " AND ".join(filters)

        sort_col = _DETAIL_SORTABLE.get(sort_by or "", "P.APPROVED_AT")
        sort_sql = f"ORDER BY {sort_col} {'ASC' if sort_dir == 'asc' else 'DESC'}, P.ID"

        bdc_join = f"""
        OUTER APPLY (
            SELECT TOP 1 H.ALLOCATION_NUMBER, H.STATUS, H.DO_RECEIVED, H.BDC_DATE
            FROM {BDC_HISTORY_TABLE} H
            WHERE H.RDC = P.RDC
              AND ISNULL(H.ST_CD,'') = ISNULL(P.ST_CD,'')
              AND H.ARTICLE_NUMBER = P.ARTICLE_NUMBER
            ORDER BY H.BDC_DATE DESC, H.ID DESC
        ) B
        """

        offset = (page - 1) * page_size
        params["offset"] = offset
        params["psize"]  = page_size

        with _engine().connect() as conn:
            ensure_pend_alc_table(conn)
            ensure_bdc_history_table(conn)

            total_rows = conn.execute(text(f"""
                SELECT COUNT(*)
                FROM {PEND_ALC_TABLE} P WITH (NOLOCK)
                {bdc_join}
                {where}
            """), params).scalar() or 0

            rows = conn.execute(text(f"""
                SELECT
                    P.ID, P.SESSION_ID, P.RDC, P.ST_CD, P.ARTICLE_NUMBER, P.MAJ_CAT,
                    P.GEN_ART_NUMBER, P.CLR, P.ALLOC_MODE, P.SOURCE,
                    P.ALLOC_QTY, P.BDC_QTY, P.DO_QTY, P.PEND_QTY,
                    P.APPROVED_AT, P.LAST_BDC_AT, P.DO_NUMBER, P.DO_UPLOADED_AT,
                    P.LAST_DO_AT, P.IS_CLOSED, P.REMARKS,
                    B.ALLOCATION_NUMBER, B.STATUS, B.DO_RECEIVED, B.BDC_DATE
                FROM {PEND_ALC_TABLE} P WITH (NOLOCK)
                {bdc_join}
                {where}
                {sort_sql}
                OFFSET :offset ROWS FETCH NEXT :psize ROWS ONLY
            """), params).fetchall()

        total_pages = max(1, (int(total_rows) + page_size - 1) // page_size)

        return {
            "success":     True,
            "count":       len(rows),
            "total_rows":  int(total_rows),
            "page":        page,
            "page_size":   page_size,
            "total_pages": total_pages,
            "data": [
                {
                    "id":             int(r[0]),
                    "session_id":     r[1],
                    "rdc":            r[2],
                    "st_cd":          r[3],
                    "article_number": r[4],
                    "maj_cat":        r[5],
                    "gen_art_number": r[6],
                    "clr":            r[7],
                    "alloc_mode":     r[8],
                    "source":         r[9],
                    "alloc_qty":      float(r[10] or 0),
                    "bdc_qty":        float(r[11] or 0),
                    "do_qty":         float(r[12] or 0),
                    "pend_qty":       float(r[13] or 0),
                    "approved_at":    r[14].isoformat() if r[14] else None,
                    "last_bdc_at":    r[15].isoformat() if r[15] else None,
                    "do_number":      r[16],
                    "do_uploaded_at": r[17].isoformat() if r[17] else None,
                    "last_do_at":     r[18].isoformat() if r[18] else None,
                    "is_closed":      bool(r[19]),
                    "remarks":        r[20],
                    "bdc_alloc_no":   r[21],
                    "bdc_status":     r[22] or ("NEVER_SENT" if not r[15] else None),
                    "do_received":    float(r[23] or 0) if r[23] is not None else None,
                    "bdc_date":       r[24].isoformat() if r[24] else None,
                }
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# GET /pend-alc/do-history
# ---------------------------------------------------------------------------
@router.get("/do-history")
def pend_alc_do_history(
    limit: int = Query(100, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
):
    """Recent DO deduction events."""
    try:
        with _engine().connect() as conn:
            ensure_pend_alc_table(conn)
            rows = conn.execute(text(f"""
                SELECT TOP (:lim)
                    SESSION_ID, RDC, ARTICLE_NUMBER, MAJ_CAT, ALLOC_MODE, SOURCE,
                    ALLOC_QTY, BDC_QTY, DO_QTY, PEND_QTY,
                    IS_CLOSED, DO_NUMBER, DO_UPLOADED_AT, LAST_DO_AT
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                WHERE LAST_DO_AT IS NOT NULL
                ORDER BY LAST_DO_AT DESC
            """), {"lim": limit}).fetchall()
        return {
            "success": True,
            "data": [
                {
                    "session_id":     r[0],
                    "rdc":            r[1],
                    "article_number": r[2],
                    "maj_cat":        r[3],
                    "alloc_mode":     r[4],
                    "source":         r[5],
                    "alloc_qty":      float(r[6] or 0),
                    "bdc_qty":        float(r[7] or 0),
                    "do_qty":         float(r[8] or 0),
                    "pend_qty":       float(r[9] or 0),
                    "is_closed":      bool(r[10]),
                    "do_number":      r[11],
                    "do_uploaded_at": r[12].isoformat() if r[12] else None,
                    "last_do_at":     r[13].isoformat() if r[13] else None,
                }
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# POST /pend-alc/do-update
# ---------------------------------------------------------------------------
class DoUpdateRow(BaseModel):
    rdc:               str
    article_number:    str
    do_qty:            float
    st_cd:             Optional[str] = None
    do_number:         Optional[str] = None
    allocation_number: Optional[str] = None  # if SAP DO references the BDC


class DoUpdateRequest(BaseModel):
    rows: List[DoUpdateRow]


@router.post("/do-update")
def pend_alc_do_update(
    body: DoUpdateRequest,
    current_user: User = Depends(get_current_user),
):
    """Record DO quantities issued by SAP. Reduces PEND_QTY; closes rows
    fully covered.

    Does NOT touch MSA tables. STK_QTY in ARS_MSA_TOTAL is a daily snapshot
    refreshed by the next full MSA run; touching FNL_Q here would over-state
    the available pool because shipped stock has physically left the WH but
    STK_QTY hasn't been re-read yet. The next full MSA reconciles.

    If st_cd is provided, deduction is scoped to that destination store —
    critical when the same RDC ships the same article to multiple stores.
    If omitted, falls back to legacy behavior (deduct from any matching row)."""
    if not body.rows:
        raise HTTPException(400, "No rows provided")
    try:
        import uuid as _uuid
        do_batch_id = _uuid.uuid4().hex[:12]  # one batch UUID per upload
        rows = [
            {"rdc": r.rdc, "article_number": r.article_number,
             "do_qty": r.do_qty, "do_number": r.do_number,
             "st_cd": r.st_cd,
             "allocation_number": r.allocation_number}
            for r in body.rows
        ]
        with _engine().connect() as conn:
            do_result   = apply_do_deductions(conn, rows)
            hist_result = update_bdc_history_with_do(conn, rows)

            # Log the operation for revert support
            total_qty = sum(float(r.get("do_qty") or 0) for r in rows)
            log_operation(
                conn,
                op_type="DO",
                op_key=do_batch_id,
                payload={
                    "input_rows":      rows,
                    "pend_updates":    do_result["pend_updates"],
                    "history_updates": hist_result["history_updates"],
                },
                summary=f"DO upload {do_batch_id}: {len(rows)} input lines, "
                        f"{int(total_qty)} units, "
                        f"{do_result['touched']} pend_alc rows updated",
                rows_affected=do_result["touched"],
                qty_total=total_qty,
                created_by=getattr(current_user, "username", None),
            )
        logger.info(
            f"[pend_alc] do-update by {getattr(current_user,'username','?')}: "
            f"{len(rows)} input → {do_result['touched']} pend_alc rows updated, "
            f"{hist_result['touched']} bdc_history rows updated, "
            f"batch={do_batch_id}"
        )
        return {
            "success":              True,
            "updated_rows":         do_result["touched"],
            "bdc_history_updated":  hist_result["touched"],
            "do_batch_id":          do_batch_id,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# GET /pend-alc/bdc-preview
# ---------------------------------------------------------------------------
@router.get("/bdc-preview")
def pend_alc_bdc_preview(
    rdc:     Optional[str] = Query(None),
    maj_cat: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
):
    """Preview open PEND rows that would be included in the next BDC file.
    Groups by RDC × ST_CD × ARTICLE × MAJ_CAT and returns PEND_QTY
    (what will be sent to SAP)."""
    try:
        filters, params = ["IS_CLOSED = 0", "PEND_QTY > 0"], {}
        if rdc:
            filters.append("RDC = :rdc"); params["rdc"] = rdc
        if maj_cat:
            filters.append("MAJ_CAT = :mc"); params["mc"] = maj_cat
        where = "WHERE " + " AND ".join(filters)

        with _engine().connect() as conn:
            ensure_pend_alc_table(conn)
            rows = conn.execute(text(f"""
                SELECT RDC,
                       ISNULL(ST_CD,'')           AS ST_CD,
                       ARTICLE_NUMBER, MAJ_CAT,
                       ISNULL(GEN_ART_NUMBER,'') AS GEN_ART_NUMBER,
                       ISNULL(CLR,'')             AS CLR,
                       SUM(ALLOC_QTY)             AS alloc_qty,
                       SUM(BDC_QTY)               AS bdc_qty_prev,
                       SUM(DO_QTY)                AS do_qty,
                       SUM(PEND_QTY)              AS pend_qty,
                       MAX(LAST_BDC_AT)           AS last_bdc_at,
                       COUNT(DISTINCT SESSION_ID) AS session_count
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                {where}
                GROUP BY RDC, ISNULL(ST_CD,''), ARTICLE_NUMBER, MAJ_CAT,
                         ISNULL(GEN_ART_NUMBER,''), ISNULL(CLR,'')
                ORDER BY RDC, ST_CD, MAJ_CAT, ARTICLE_NUMBER
            """), params).fetchall()

        return {
            "success": True,
            "count": len(rows),
            "total_pend_qty": sum(float(r[9] or 0) for r in rows),
            "data": [
                {
                    "rdc":            r[0],
                    "st_cd":          r[1],
                    "article_number": r[2],
                    "maj_cat":        r[3],
                    "gen_art_number": r[4],
                    "clr":            r[5],
                    "alloc_qty":      float(r[6] or 0),
                    "bdc_qty_prev":   float(r[7] or 0),
                    "do_qty":         float(r[8] or 0),
                    "pend_qty":       float(r[9] or 0),
                    "last_bdc_at":    r[10].isoformat() if r[10] else None,
                    "session_count":  int(r[11] or 0),
                }
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# POST /pend-alc/bdc-generate
# ---------------------------------------------------------------------------
@router.post("/bdc-generate")
def pend_alc_bdc_generate(
    rdc:         Optional[str]      = Query(None),
    maj_cat:     Optional[str]      = Query(None),
    target_date: Optional[str]      = Query(
        None,
        description="YYYY-MM-DD. If set, picks stores from "
                    "ARS_STORE_BDC_SCHEDULE for that day-of-week. "
                    "Used for Allocation Date + Picking Date in the file."),
    st_cd_list:  Optional[List[str]] = Query(
        None,
        description="Override: explicit list of stores to include. Takes "
                    "precedence over target_date schedule lookup."),
    current_user: User = Depends(get_current_user),
):
    """Stamp BDC_QTY on open PEND rows and return an Excel file in the
    BDC Creation 9-column SAP-ready format.

    Store selection priority:
      1. `st_cd_list` (explicit override) — wins if provided
      2. `target_date` → schedule lookup (Mon-Sat from ARS_STORE_BDC_SCHEDULE)
      3. None of above → all stores with open pending qty

    Output columns (identical to BDC Creation menu):
        Serial No, Allocation Date, Allocation Number, VENDOR, MATERIAL NO,
        BDC-QTY, RECEIVING STORE, Picking Date, Remark
    """
    try:
        # Resolve store list — explicit override > schedule > all
        import datetime
        store_filter: Optional[List[str]] = None
        if st_cd_list:
            store_filter = [s.strip() for s in st_cd_list if s and s.strip()]
        elif target_date:
            with _engine().connect() as _sc:
                store_filter = get_stores_for_date(_sc, target_date)
            if not store_filter:
                raise HTTPException(
                    400,
                    f"No stores scheduled for {target_date}. "
                    f"Either Sunday or no schedule rows match."
                )

        # Date used for "Allocation Date" + "Picking Date" inside the file
        if target_date:
            try:
                file_date_str = datetime.date.fromisoformat(target_date).strftime("%Y-%m-%d")
            except Exception:
                file_date_str = datetime.date.today().strftime("%Y-%m-%d")
        else:
            file_date_str = datetime.date.today().strftime("%Y-%m-%d")
        today_str = file_date_str

        filters, params = ["IS_CLOSED = 0", "PEND_QTY > 0"], {}
        if rdc:
            filters.append("RDC = :rdc"); params["rdc"] = rdc
        if maj_cat:
            filters.append("MAJ_CAT = :mc"); params["mc"] = maj_cat
        if store_filter:
            placeholders = ",".join(f":st{i}" for i in range(len(store_filter)))
            filters.append(f"ISNULL(ST_CD,'') IN ({placeholders})")
            for i, v in enumerate(store_filter):
                params[f"st{i}"] = v
        where = "WHERE " + " AND ".join(filters)

        # Allocation number from the same FY-NNN pool used by BDC Creation
        from app.api.v1.endpoints.bdc import _get_next_allocation_no
        allocation_no = _get_next_allocation_no(_engine())

        with _engine().connect() as conn:
            ensure_pend_alc_table(conn)
            rows = conn.execute(text(f"""
                SELECT RDC,
                       ISNULL(ST_CD,'')          AS ST_CD,
                       ARTICLE_NUMBER,
                       MAJ_CAT,
                       SUM(PEND_QTY)             AS PEND_QTY
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                {where}
                GROUP BY RDC, ISNULL(ST_CD,''), ARTICLE_NUMBER, MAJ_CAT
                HAVING SUM(PEND_QTY) > 0
                ORDER BY RDC, ST_CD, MAJ_CAT, ARTICLE_NUMBER
            """), params).fetchall()

            if not rows:
                raise HTTPException(404, "No open pending rows found for BDC")

            # Stamp BDC_QTY ONLY on the exact (RDC, ST_CD, ARTICLE) rows that
            # went into this BDC file. Scoping by all 3 keys is critical when
            # the user picks a date/store subset — otherwise BDC_QTY would
            # also get stamped on rows for stores that were NOT in the file.
            article_rdc = [
                {"rdc": r[0], "st_cd": r[1], "article_number": r[2]}
                for r in rows
            ]
            stamped_deltas = stamp_bdc_qty(conn, article_rdc)

            # Append one history row per (RDC, ST_CD, ARTICLE) line in this BDC.
            history_ids = insert_bdc_history(
                conn,
                allocation_number=allocation_no,
                rows=[
                    {"rdc": r[0], "st_cd": r[1], "article_number": r[2],
                     "maj_cat": r[3], "bdc_qty": float(r[4] or 0)}
                    for r in rows
                ],
                created_by=getattr(current_user, "username", None),
            )

            # Log the operation so it can be reverted later.
            total_qty = sum(float(r[4] or 0) for r in rows)
            log_operation(
                conn,
                op_type="BDC",
                op_key=allocation_no,
                payload={
                    "allocation_number": allocation_no,
                    "history_ids":       history_ids,
                    "stamped_rows":      stamped_deltas,
                },
                summary=f"BDC {allocation_no}: {len(rows)} lines, "
                        f"{int(total_qty)} units, "
                        f"date={file_date_str}",
                rows_affected=len(rows),
                qty_total=total_qty,
                created_by=getattr(current_user, "username", None),
            )

        # Build Excel in BDC Creation 9-column format
        df = pd.DataFrame([
            {
                "Serial No":         i + 1,
                "Allocation Date":   today_str,
                "Allocation Number": allocation_no,
                "VENDOR":            str(r[0] or "").strip(),
                "MATERIAL NO":       str(r[2] or "").strip().lstrip("0"),
                "BDC-QTY":           int(r[4] or 0),
                "RECEIVING STORE":   str(r[1] or "").strip(),
                "Picking Date":      today_str,
                "Remark":            "",
            }
            for i, r in enumerate(rows)
        ])

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="BDC")
        buf.seek(0)

        fname = f"ARS_BDC_{datetime.date.today().strftime('%Y%m%d')}_{allocation_no}.xlsx"
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Store BDC schedule — Mon-Sat schedule per store
# ---------------------------------------------------------------------------
@router.get("/schedule")
def pend_alc_schedule_list(current_user: User = Depends(get_current_user)):
    """List every store's BDC schedule (Mon-Sat flags + active flag)."""
    try:
        with _engine().connect() as conn:
            data = list_schedules(conn)
        return {"success": True, "count": len(data), "data": data}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/schedule/stores-for-date")
def pend_alc_schedule_stores_for_date(
    date: str = Query(..., description="YYYY-MM-DD"),
    current_user: User = Depends(get_current_user),
):
    """Return list of stores scheduled to receive a BDC on the given date.
    Sunday returns empty list (BDC not generated on Sundays).
    """
    try:
        with _engine().connect() as conn:
            stores = get_stores_for_date(conn, date)

        # Day-of-week label for the UI
        import datetime as _dt
        try:
            d = _dt.date.fromisoformat(date)
            weekday = d.strftime("%A")  # Monday, Tuesday, ...
        except Exception:
            weekday = ""
        return {
            "success": True,
            "date":    date,
            "weekday": weekday,
            "count":   len(stores),
            "stores":  stores,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


class ScheduleRow(BaseModel):
    st_cd:     str
    st_name:   Optional[str] = None
    mon:       bool = False
    tue:       bool = False
    wed:       bool = False
    thu:       bool = False
    fri:       bool = False
    sat:       bool = False
    is_active: bool = True


class ScheduleUpsertRequest(BaseModel):
    rows:    List[ScheduleRow]
    source:  Optional[str] = "API"   # 'UI' / 'CSV_IMPORT' / 'API'
    note:    Optional[str] = None    # e.g. "CSV: BDC_Schedule_2026-05-07.csv"


@router.post("/schedule")
def pend_alc_schedule_upsert(
    body: ScheduleUpsertRequest,
    current_user: User = Depends(get_current_user),
):
    """Bulk upsert one or more store schedules.

    Every changed field on every row is logged to ARS_STORE_BDC_SCHEDULE_AUDIT
    with a shared BATCH_ID for this call. The optional `source` and `note`
    are stored verbatim in the audit log."""
    if not body.rows:
        raise HTTPException(400, "No rows provided")
    try:
        with _engine().connect() as conn:
            res = upsert_schedules(
                conn,
                [r.model_dump() for r in body.rows],
                updated_by=getattr(current_user, "username", None),
                source=(body.source or "API"),
                note=body.note,
            )
        logger.info(
            f"[schedule] upsert by {getattr(current_user,'username','?')} "
            f"source={body.source}: {res}"
        )
        return {"success": True, **res}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.delete("/schedule/{st_cd}")
def pend_alc_schedule_delete(
    st_cd:  str,
    source: Optional[str] = Query("UI"),
    note:   Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
):
    """Hard-delete a store's schedule row + write per-field audit rows so the
    deleted state can be reconstructed later."""
    try:
        with _engine().connect() as conn:
            res = delete_schedule(
                conn, st_cd,
                user=getattr(current_user, "username", None),
                source=(source or "UI"),
                note=note,
            )
        return {"success": True, **res}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/schedule/audit")
def pend_alc_schedule_audit(
    st_cd:     Optional[str] = Query(None),
    user:      Optional[str] = Query(None),
    source:    Optional[str] = Query(None, description="CSV: 'UI,CSV_IMPORT'"),
    action:    Optional[str] = Query(None, description="CSV"),
    field:     Optional[str] = Query(None, description="CSV"),
    batch_id:  Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    page:      int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=10000),
    sort_by:   str = Query("change_time"),
    sort_dir:  str = Query("desc", pattern="^(asc|desc)$"),
    current_user: User = Depends(get_current_user),
):
    """Paged audit log of every schedule field change."""
    try:
        with _engine().connect() as conn:
            res = list_schedule_audit(
                conn,
                st_cd=st_cd, user=user, source=source, action=action,
                field=field, batch_id=batch_id,
                date_from=date_from, date_to=date_to,
                page=page, page_size=page_size,
                sort_by=sort_by, sort_dir=sort_dir,
            )
        return {"success": True, **res}
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# GET /pend-alc/bdc-history
# ---------------------------------------------------------------------------
@router.get("/bdc-history")
def pend_alc_bdc_history(
    allocation_no: Optional[str] = Query(None),
    rdc:           Optional[str] = Query(None),
    st_cd:         Optional[str] = Query(None),
    article:       Optional[str] = Query(None),
    status:        Optional[str] = Query(None,
        description="Filter by STATUS: OPEN / PARTIAL / CONFIRMED"),
    limit:         int = Query(2000, ge=1, le=20000),
    current_user: User = Depends(get_current_user),
):
    """Audit trail of every BDC ever generated, with how much SAP confirmed.

    Each row = one (RDC, ST_CD, ARTICLE) line in one BDC file.
    BDC_QTY      = qty asked for in that BDC
    DO_RECEIVED  = qty SAP returned via DO
    STATUS       = OPEN (no DO yet) / PARTIAL (some DO, short) / CONFIRMED (full)

    Multiple BDCs for the same store/article appear as separate rows so you can
    see retry attempts.
    """
    try:
        filters = ["1=1"]
        params: dict = {"lim": limit}
        if allocation_no:
            filters.append("ALLOCATION_NUMBER = :alloc"); params["alloc"] = allocation_no
        if rdc:
            filters.append("RDC = :rdc"); params["rdc"] = rdc
        if st_cd:
            filters.append("ISNULL(ST_CD,'') = :st"); params["st"] = st_cd
        if article:
            filters.append("ARTICLE_NUMBER = :art"); params["art"] = article
        if status:
            filters.append("STATUS = :stat"); params["stat"] = status.upper()
        where = "WHERE " + " AND ".join(filters)

        with _engine().connect() as conn:
            # Make sure table exists even if no BDC has been generated yet
            conn.execute(text(
                f"IF OBJECT_ID('dbo.{BDC_HISTORY_TABLE}','U') IS NULL "
                f"SELECT 1"
            ))
            tbl_exists = conn.execute(text(
                f"SELECT CASE WHEN OBJECT_ID('dbo.{BDC_HISTORY_TABLE}','U') "
                f"IS NULL THEN 0 ELSE 1 END"
            )).scalar()
            if not tbl_exists:
                return {"success": True, "count": 0, "data": []}

            rows = conn.execute(text(f"""
                SELECT TOP (:lim)
                    ID, BDC_DATE, ALLOCATION_NUMBER,
                    RDC, ST_CD, ARTICLE_NUMBER, MAJ_CAT,
                    BDC_QTY, DO_RECEIVED,
                    (BDC_QTY - DO_RECEIVED) AS SHORT_QTY,
                    STATUS, LAST_DO_AT, CREATED_BY
                FROM {BDC_HISTORY_TABLE} WITH (NOLOCK)
                {where}
                ORDER BY BDC_DATE DESC, ID DESC
            """), params).fetchall()

        return {
            "success": True,
            "count":   len(rows),
            "data": [
                {
                    "id":                int(r[0]),
                    "bdc_date":          r[1].isoformat() if r[1] else None,
                    "allocation_number": r[2],
                    "rdc":               r[3],
                    "st_cd":             r[4],
                    "article_number":    r[5],
                    "maj_cat":           r[6],
                    "bdc_qty":           float(r[7] or 0),
                    "do_received":       float(r[8] or 0),
                    "short_qty":         float(r[9] or 0),
                    "status":            r[10],
                    "last_do_at":        r[11].isoformat() if r[11] else None,
                    "created_by":        r[12],
                }
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# POST /pend-alc/bdc-recover-orphans  — clean up rows over-stamped by the
# pre-fix Generate BDC bug. An orphan = BDC_QTY>0 but no matching row in
# ARS_BDC_HISTORY for (RDC, ST_CD, ARTICLE).  Run with confirm=false first
# to preview, then confirm=true to apply.
# ---------------------------------------------------------------------------
@router.post("/bdc-recover-orphans")
def pend_alc_bdc_recover_orphans(
    confirm: bool = Query(False, description="false = preview only, true = apply"),
    current_user: User = Depends(get_current_user),
):
    """One-shot cleanup for the over-stamp bug.

    Resets BDC_QTY=0 and LAST_BDC_AT=NULL on rows that have BDC_QTY>0 but no
    corresponding entry in ARS_BDC_HISTORY (so they were stamped without
    actually being included in any BDC file).

    The Reconciliation status tiles will then correctly show those rows as
    "Awaiting BDC" again.
    """
    try:
        with _engine().connect() as conn:
            result = recover_orphan_bdc_stamps(conn, dry_run=not confirm)
        logger.info(
            f"[pend_alc] bdc-recover-orphans by {getattr(current_user,'username','?')}: "
            f"{result}"
        )
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Operations log + revert (BDC / DO / MANUAL)
# ---------------------------------------------------------------------------
@router.get("/operations")
def pend_alc_operations_list(
    op_type:           Optional[str] = Query(None, description="BDC / DO / MANUAL"),
    include_reverted:  bool          = Query(True),
    limit:             int           = Query(200, ge=1, le=2000),
    current_user: User = Depends(get_current_user),
):
    """List recent BDC / DO / MANUAL operations for the audit + undo UI."""
    try:
        with _engine().connect() as conn:
            data = list_operations(conn, op_type=op_type,
                                   include_reverted=include_reverted, limit=limit)
        return {"success": True, "count": len(data), "data": data}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/operations/{op_id}/preview-revert")
def pend_alc_operations_preview(
    op_id: int,
    current_user: User = Depends(get_current_user),
):
    """Dry-run: returns what would be reverted + any safety blockers."""
    try:
        with _engine().connect() as conn:
            res = preview_revert(conn, op_id)
        if res.get("error"):
            raise HTTPException(404, res["error"])
        return {"success": True, **res}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


class RevertRequest(BaseModel):
    note: Optional[str] = None


@router.post("/operations/{op_id}/revert")
def pend_alc_operations_revert(
    op_id: int,
    body:  Optional[RevertRequest] = None,
    confirm: bool = Query(False, description="Must be true to apply"),
    current_user: User = Depends(get_current_user),
):
    """Apply the reverse of an operation. Requires `confirm=true`."""
    if not confirm:
        raise HTTPException(400, "confirm=true is required to revert")
    try:
        note = (body.note if body else None)
        with _engine().connect() as conn:
            res = revert_operation(
                conn, op_id,
                reverted_by=getattr(current_user, "username", None),
                note=note,
            )
        if not res.get("success"):
            raise HTTPException(400, res.get("error") or "Revert failed")
        logger.info(
            f"[pend_alc] revert op_id={op_id} by "
            f"{getattr(current_user,'username','?')}: {res}"
        )
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/operations/backfill-bdc")
def pend_alc_operations_backfill_bdc(
    confirm: bool = Query(False, description="false = preview, true = apply"),
    current_user: User = Depends(get_current_user),
):
    """One-shot: scan ARS_BDC_HISTORY and create operations log entries for
    BDCs that were generated BEFORE the operations-log feature shipped, so
    they become revertable from the UI.

    Run with confirm=false first to see the count, then confirm=true to apply.
    Idempotent — re-running only adds rows for allocation_numbers still missing.
    """
    try:
        with _engine().connect() as conn:
            res = backfill_bdc_operations(conn, dry_run=not confirm)
        logger.info(
            f"[pend_alc] backfill-bdc by {getattr(current_user,'username','?')}: {res}"
        )
        return {"success": True, **res}
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# POST /pend-alc/manual-upload
# ---------------------------------------------------------------------------
class ManualRow(BaseModel):
    rdc:            str
    article_number: str
    alloc_qty:      float
    st_cd:          Optional[str] = None
    maj_cat:        Optional[str] = None
    gen_art_number: Optional[str] = None
    clr:            Optional[str] = None
    remarks:        Optional[str] = None


class ManualUploadRequest(BaseModel):
    rows: List[ManualRow]


@router.post("/manual-upload")
def pend_alc_manual_upload(
    body: ManualUploadRequest,
    current_user: User = Depends(get_current_user),
):
    """Insert manually-allocated rows (SOURCE=MANUAL) into ARS_PEND_ALC.

    Immediately adjusts ARS_MSA_TOTAL/GEN_ART/VAR_ART for the affected
    (RDC, ARTICLE_NUMBER) keys: PEND_QTY goes up, FNL_Q goes down. Same
    treatment whether rows came from the manual entry table or from a bulk
    CSV upload.
    """
    if not body.rows:
        raise HTTPException(400, "No rows provided")
    try:
        rows = [r.model_dump() for r in body.rows]
        article_rdc_pairs = [
            {"rdc": r["rdc"], "article_number": r["article_number"]}
            for r in rows
        ]
        with _engine().connect() as conn:
            res = write_manual_pend_alc(conn, rows)
            # Apply +1 delta to MSA + Grid in one call. Replaces the old
            # adjust_msa_after_pend_insert (which only patched MSA, not Grid).
            msa_adjusted = apply_pend_alc_delta(conn, rows, sign=+1)
            # Log the manual upload for revert support
            total_alloc = sum(float(r.get("alloc_qty") or 0) for r in rows)
            log_operation(
                conn,
                op_type="MANUAL",
                op_key=res["session_id"],
                payload={
                    "session_id":        res["session_id"],
                    "inserted_ids":      res["inserted_ids"],
                    "article_rdc_pairs": article_rdc_pairs,
                },
                summary=f"Manual upload {res['session_id']}: "
                        f"{res['inserted']} rows, {int(total_alloc)} units",
                rows_affected=res["inserted"],
                qty_total=total_alloc,
                created_by=getattr(current_user, "username", None),
            )
        logger.info(
            f"[pend_alc] manual-upload by {getattr(current_user,'username','?')}: "
            f"{res['inserted']} rows inserted, session_id={res['session_id']}"
        )
        return {
            "success":       True,
            "inserted_rows": res["inserted"],
            "session_id":    res["session_id"],
            "msa_adjusted":  msa_adjusted,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# GET /pend-alc/reco
# ---------------------------------------------------------------------------
# Sortable + filterable columns. Maps frontend column key → SQL column.
# Anything not in this map is rejected — keeps the query safe.
_RECO_SORTABLE = {
    "id":              "P.ID",
    "session_id":      "P.SESSION_ID",
    "rdc":             "P.RDC",
    "st_cd":           "P.ST_CD",
    "article_number":  "P.ARTICLE_NUMBER",
    "maj_cat":         "P.MAJ_CAT",
    "gen_art_number":  "P.GEN_ART_NUMBER",
    "clr":             "P.CLR",
    "alloc_mode":      "P.ALLOC_MODE",
    "source":          "P.SOURCE",
    "alloc_qty":       "P.ALLOC_QTY",
    "bdc_qty":         "P.BDC_QTY",
    "do_qty":          "P.DO_QTY",
    "pend_qty":        "P.PEND_QTY",
    "bdc_unconfirmed": "(CASE WHEN P.BDC_QTY - P.DO_QTY > 0 THEN P.BDC_QTY - P.DO_QTY ELSE 0 END)",
    "approved_at":     "P.APPROVED_AT",
    "last_bdc_at":     "P.LAST_BDC_AT",
    "do_number":       "P.DO_NUMBER",
    "is_closed":       "P.IS_CLOSED",
    "aging_days":      "DATEDIFF(day, P.APPROVED_AT, GETDATE())",
    "bdc_alloc_no":    "B.ALLOCATION_NUMBER",
    "bdc_status":      "B.STATUS",
    "do_received":     "B.DO_RECEIVED",
}


def _parse_csv_filter(s: Optional[str]) -> List[str]:
    if not s: return []
    return [v.strip() for v in s.split(",") if v.strip()]


@router.get("/reco")
def pend_alc_reco(
    date_from:   Optional[str]  = Query(None),
    date_to:     Optional[str]  = Query(None),
    rdc:         Optional[str]  = Query(None),
    maj_cat:     Optional[str]  = Query(None),
    alloc_mode:  Optional[str]  = Query(None),
    source:      Optional[str]  = Query(None),
    closed:      Optional[bool] = Query(None),
    session_id:  Optional[str]  = Query(None),
    # Pagination
    page:        int            = Query(1,   ge=1),
    page_size:   int            = Query(100, ge=1, le=10000),
    # Sort
    sort_by:     Optional[str]  = Query(None,
        description="Column key from _RECO_SORTABLE (e.g. 'pend_qty')"),
    sort_dir:    str            = Query("desc", regex="^(asc|desc)$"),
    # Per-column multi-value filters (CSV: 'DH24,DW01')
    f_rdc:        Optional[str] = Query(None),
    f_st_cd:      Optional[str] = Query(None),
    f_maj_cat:    Optional[str] = Query(None),
    f_alloc_mode: Optional[str] = Query(None),
    f_source:     Optional[str] = Query(None),
    f_bdc_status: Optional[str] = Query(None,
        description="OPEN, PARTIAL, CONFIRMED, NEVER_SENT (csv)"),
    f_aging_band: Optional[str] = Query(None),
    # Free-text contains-match
    q_article:    Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
):
    """Paged reco view with sort, per-column filter, and BDC history join.

    Each row is enriched with the latest open BDC's ALLOCATION_NUMBER, STATUS,
    and DO_RECEIVED. If no BDC exists for the (RDC, ST_CD, ARTICLE) those
    columns are null and BDC_STATUS reads 'NEVER_SENT'.
    """
    try:
        # Build WHERE
        filters = ["1=1"]
        params: dict = {}
        if date_from:
            filters.append("P.APPROVED_AT >= :df"); params["df"] = date_from
        if date_to:
            filters.append("P.APPROVED_AT < DATEADD(day,1,:dt)"); params["dt"] = date_to
        if rdc:
            filters.append("P.RDC = :rdc"); params["rdc"] = rdc
        if maj_cat:
            filters.append("P.MAJ_CAT = :mc"); params["mc"] = maj_cat
        if alloc_mode:
            filters.append("P.ALLOC_MODE = :am"); params["am"] = alloc_mode
        if source:
            filters.append("P.SOURCE = :src"); params["src"] = source
        if closed is not None:
            filters.append("P.IS_CLOSED = :cl"); params["cl"] = 1 if closed else 0
        if session_id:
            filters.append("P.SESSION_ID = :sid"); params["sid"] = session_id

        # Multi-value column filters
        def _multi(col: str, csv: Optional[str], prefix: str):
            vals = _parse_csv_filter(csv)
            if not vals: return
            placeholders = ",".join(f":{prefix}{i}" for i in range(len(vals)))
            filters.append(f"{col} IN ({placeholders})")
            for i, v in enumerate(vals):
                params[f"{prefix}{i}"] = v
        _multi("P.RDC",         f_rdc,        "frdc")
        _multi("ISNULL(P.ST_CD,'')", f_st_cd,  "fst")
        _multi("P.MAJ_CAT",     f_maj_cat,    "fmc")
        _multi("P.ALLOC_MODE",  f_alloc_mode, "fam")
        _multi("P.SOURCE",      f_source,     "fsrc")

        if q_article:
            filters.append("P.ARTICLE_NUMBER LIKE :qart")
            params["qart"] = f"%{q_article}%"

        # Aging band filter
        aging_vals = _parse_csv_filter(f_aging_band)
        if aging_vals:
            cases = []
            for i, v in enumerate(aging_vals):
                cases.append(f":fage{i}")
                params[f"fage{i}"] = v
            filters.append(
                f"(CASE "
                f"  WHEN DATEDIFF(day,P.APPROVED_AT,GETDATE())<=7 THEN '0-7d' "
                f"  WHEN DATEDIFF(day,P.APPROVED_AT,GETDATE())<=30 THEN '8-30d' "
                f"  WHEN DATEDIFF(day,P.APPROVED_AT,GETDATE())<=60 THEN '31-60d' "
                f"  ELSE '60d+' END) IN ({','.join(cases)})"
            )

        # BDC status filter (NEVER_SENT = no matching B row)
        bdc_status_vals = _parse_csv_filter(f_bdc_status)
        if bdc_status_vals:
            real_statuses = [v for v in bdc_status_vals if v != "NEVER_SENT"]
            include_never = "NEVER_SENT" in bdc_status_vals
            cond_parts = []
            if real_statuses:
                phs = ",".join(f":fbst{i}" for i in range(len(real_statuses)))
                cond_parts.append(f"B.STATUS IN ({phs})")
                for i, v in enumerate(real_statuses):
                    params[f"fbst{i}"] = v
            if include_never:
                cond_parts.append("B.STATUS IS NULL")
            filters.append("(" + " OR ".join(cond_parts) + ")")

        where = "WHERE " + " AND ".join(filters)

        # Sort — pick a safe column from the allow-list
        sort_col = _RECO_SORTABLE.get(sort_by or "", "P.APPROVED_AT")
        sort_sql = f"ORDER BY {sort_col} {'ASC' if sort_dir == 'asc' else 'DESC'}, P.ID"

        # The latest BDC history row per (RDC, ST_CD, ARTICLE) — using a
        # window function so we get one row per pend_alc match.
        bdc_join = f"""
        OUTER APPLY (
            SELECT TOP 1 H.ALLOCATION_NUMBER, H.STATUS, H.DO_RECEIVED, H.BDC_DATE
            FROM {BDC_HISTORY_TABLE} H
            WHERE H.RDC = P.RDC
              AND ISNULL(H.ST_CD,'') = ISNULL(P.ST_CD,'')
              AND H.ARTICLE_NUMBER = P.ARTICLE_NUMBER
            ORDER BY H.BDC_DATE DESC, H.ID DESC
        ) B
        """

        # Pagination math
        offset = (page - 1) * page_size
        params["offset"] = offset
        params["psize"]  = page_size

        with _engine().connect() as conn:
            ensure_pend_alc_table(conn)
            ensure_bdc_history_table(conn)

            # Count first (for total_pages)
            total_rows = conn.execute(text(f"""
                SELECT COUNT(*)
                FROM {PEND_ALC_TABLE} P WITH (NOLOCK)
                {bdc_join}
                {where}
            """), params).scalar() or 0

            rows = conn.execute(text(f"""
                SELECT
                    P.ID, P.SESSION_ID, P.RDC, P.ST_CD, P.ARTICLE_NUMBER, P.MAJ_CAT,
                    P.GEN_ART_NUMBER, P.CLR, P.ALLOC_MODE, P.SOURCE,
                    P.ALLOC_QTY, P.BDC_QTY, P.DO_QTY, P.PEND_QTY,
                    CASE WHEN P.BDC_QTY - P.DO_QTY > 0 THEN P.BDC_QTY - P.DO_QTY ELSE 0 END
                        AS BDC_UNCONFIRMED,
                    P.APPROVED_AT, P.LAST_BDC_AT, P.DO_NUMBER, P.DO_UPLOADED_AT,
                    P.IS_CLOSED, P.REMARKS,
                    DATEDIFF(day, P.APPROVED_AT, GETDATE()) AS AGING_DAYS,
                    CASE
                        WHEN DATEDIFF(day, P.APPROVED_AT, GETDATE()) <= 7   THEN '0-7d'
                        WHEN DATEDIFF(day, P.APPROVED_AT, GETDATE()) <= 30  THEN '8-30d'
                        WHEN DATEDIFF(day, P.APPROVED_AT, GETDATE()) <= 60  THEN '31-60d'
                        ELSE '60d+'
                    END AS AGING_BAND,
                    B.ALLOCATION_NUMBER AS BDC_ALLOC_NO,
                    B.STATUS            AS BDC_STATUS,
                    B.DO_RECEIVED       AS BDC_DO_RECEIVED,
                    B.BDC_DATE          AS BDC_DATE
                FROM {PEND_ALC_TABLE} P WITH (NOLOCK)
                {bdc_join}
                {where}
                {sort_sql}
                OFFSET :offset ROWS FETCH NEXT :psize ROWS ONLY
            """), params).fetchall()

        total_pages = max(1, (int(total_rows) + page_size - 1) // page_size)

        return {
            "success":     True,
            "count":       len(rows),
            "total_rows":  int(total_rows),
            "page":        page,
            "page_size":   page_size,
            "total_pages": total_pages,
            "data": [
                {
                    "id":              int(r[0]),
                    "session_id":      r[1],
                    "rdc":             r[2],
                    "st_cd":           r[3],
                    "article_number":  r[4],
                    "maj_cat":         r[5],
                    "gen_art_number":  r[6],
                    "clr":             r[7],
                    "alloc_mode":      r[8],
                    "source":          r[9],
                    "alloc_qty":       float(r[10] or 0),
                    "bdc_qty":         float(r[11] or 0),
                    "do_qty":          float(r[12] or 0),
                    "pend_qty":        float(r[13] or 0),
                    "bdc_unconfirmed": float(r[14] or 0),
                    "approved_at":     r[15].isoformat() if r[15] else None,
                    "last_bdc_at":     r[16].isoformat() if r[16] else None,
                    "do_number":       r[17],
                    "do_uploaded_at":  r[18].isoformat() if r[18] else None,
                    "is_closed":       bool(r[19]),
                    "remarks":         r[20],
                    "aging_days":      int(r[21] or 0),
                    "aging_band":      r[22],
                    "bdc_alloc_no":    r[23],
                    "bdc_status":      r[24] or ("NEVER_SENT" if not r[16] else None),
                    "do_received":     float(r[25] or 0) if r[25] is not None else None,
                    "bdc_date":        r[26].isoformat() if r[26] else None,
                }
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# GET /pend-alc/reco-summary
# ---------------------------------------------------------------------------
@router.get("/reco-summary")
def pend_alc_reco_summary(current_user: User = Depends(get_current_user)):
    """Aggregated reco tiles: by mode, source, aging band, and RDC."""
    try:
        with _engine().connect() as conn:
            ensure_pend_alc_table(conn)

            by_mode = conn.execute(text(f"""
                SELECT ISNULL(ALLOC_MODE,'AUTO') AS mode,
                       SUM(ALLOC_QTY) AS alloc_qty,
                       SUM(BDC_QTY)  AS bdc_qty,
                       SUM(DO_QTY)   AS do_qty,
                       SUM(PEND_QTY) AS pend_qty,
                       COUNT(*)      AS rows
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                WHERE IS_CLOSED = 0
                GROUP BY ISNULL(ALLOC_MODE,'AUTO')
            """)).fetchall()

            by_aging = conn.execute(text(f"""
                SELECT
                    CASE
                        WHEN DATEDIFF(day, APPROVED_AT, GETDATE()) <= 7   THEN '0-7d'
                        WHEN DATEDIFF(day, APPROVED_AT, GETDATE()) <= 30  THEN '8-30d'
                        WHEN DATEDIFF(day, APPROVED_AT, GETDATE()) <= 60  THEN '31-60d'
                        ELSE '60d+'
                    END                AS aging_band,
                    COUNT(*)           AS rows,
                    SUM(PEND_QTY)      AS pend_qty,
                    SUM(ALLOC_QTY)     AS alloc_qty
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                WHERE IS_CLOSED = 0
                GROUP BY
                    CASE
                        WHEN DATEDIFF(day, APPROVED_AT, GETDATE()) <= 7   THEN '0-7d'
                        WHEN DATEDIFF(day, APPROVED_AT, GETDATE()) <= 30  THEN '8-30d'
                        WHEN DATEDIFF(day, APPROVED_AT, GETDATE()) <= 60  THEN '31-60d'
                        ELSE '60d+'
                    END
                ORDER BY MIN(DATEDIFF(day, APPROVED_AT, GETDATE()))
            """)).fetchall()

            by_rdc = conn.execute(text(f"""
                SELECT RDC,
                       SUM(ALLOC_QTY) AS alloc_qty,
                       SUM(BDC_QTY)  AS bdc_qty,
                       SUM(DO_QTY)   AS do_qty,
                       SUM(PEND_QTY) AS pend_qty,
                       COUNT(*)      AS rows
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                WHERE IS_CLOSED = 0
                GROUP BY RDC
                ORDER BY SUM(PEND_QTY) DESC
            """)).fetchall()

            # Status buckets: where each open row is in its lifecycle.
            #   awaiting_bdc  = no BDC sent yet                (BDC_QTY = 0)
            #   awaiting_do   = BDC sent, SAP hasn't acked all (BDC_QTY > DO_QTY)
            #   partial       = some DO came, still short      (DO_QTY > 0 AND DO_QTY < ALLOC_QTY AND BDC_QTY=DO_QTY)
            #   closed        = fully covered                  (IS_CLOSED = 1)
            by_status = conn.execute(text(f"""
                SELECT
                    SUM(CASE WHEN IS_CLOSED=0 AND BDC_QTY = 0
                             THEN 1 ELSE 0 END)              AS awaiting_bdc_rows,
                    SUM(CASE WHEN IS_CLOSED=0 AND BDC_QTY = 0
                             THEN PEND_QTY ELSE 0 END)       AS awaiting_bdc_qty,

                    SUM(CASE WHEN IS_CLOSED=0 AND BDC_QTY > DO_QTY
                             THEN 1 ELSE 0 END)              AS awaiting_do_rows,
                    SUM(CASE WHEN IS_CLOSED=0 AND BDC_QTY > DO_QTY
                             THEN BDC_QTY - DO_QTY ELSE 0 END) AS awaiting_do_qty,

                    SUM(CASE WHEN IS_CLOSED=0 AND DO_QTY > 0
                                  AND DO_QTY < ALLOC_QTY AND BDC_QTY = DO_QTY
                             THEN 1 ELSE 0 END)              AS partial_rows,
                    SUM(CASE WHEN IS_CLOSED=0 AND DO_QTY > 0
                                  AND DO_QTY < ALLOC_QTY AND BDC_QTY = DO_QTY
                             THEN PEND_QTY ELSE 0 END)       AS partial_qty,

                    SUM(CASE WHEN IS_CLOSED=1 THEN 1 ELSE 0 END) AS closed_rows,
                    SUM(CASE WHEN IS_CLOSED=1 THEN ALLOC_QTY ELSE 0 END) AS closed_qty
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
            """)).fetchone()

        return {
            "success": True,
            "data": {
                "by_mode": [
                    {"mode": r[0], "alloc_qty": float(r[1] or 0),
                     "bdc_qty": float(r[2] or 0), "do_qty": float(r[3] or 0),
                     "pend_qty": float(r[4] or 0), "rows": int(r[5] or 0)}
                    for r in by_mode
                ],
                "by_aging": [
                    {"aging_band": r[0], "rows": int(r[1] or 0),
                     "pend_qty": float(r[2] or 0), "alloc_qty": float(r[3] or 0)}
                    for r in by_aging
                ],
                "by_rdc": [
                    {"rdc": r[0], "alloc_qty": float(r[1] or 0),
                     "bdc_qty": float(r[2] or 0), "do_qty": float(r[3] or 0),
                     "pend_qty": float(r[4] or 0), "rows": int(r[5] or 0)}
                    for r in by_rdc
                ],
                "by_status": {
                    "awaiting_bdc": {
                        "rows": int(by_status[0] or 0),
                        "qty":  float(by_status[1] or 0),
                    },
                    "awaiting_do": {
                        "rows": int(by_status[2] or 0),
                        "qty":  float(by_status[3] or 0),
                    },
                    "partial": {
                        "rows": int(by_status[4] or 0),
                        "qty":  float(by_status[5] or 0),
                    },
                    "closed": {
                        "rows": int(by_status[6] or 0),
                        "qty":  float(by_status[7] or 0),
                    },
                },
            },
        }
    except Exception as e:
        raise HTTPException(500, str(e))
