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
    # Multi-chunk upload coordination — mirror of manual-upload's pattern.
    # The frontend generates ONE session_id for the whole upload and sends
    # it with every chunk so all chunks roll up to a SINGLE operations_log
    # entry (revert covers the whole upload, not just chunk 1).
    # All three fields are optional; legacy single-shot clients still work.
    session_id:     Optional[str] = None
    is_first_chunk: bool          = True
    is_last_chunk:  bool          = True


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
    If omitted, falls back to legacy behavior (deduct from any matching row).

    Multi-chunk uploads: the frontend slices large uploads into chunks and
    sends each chunk with the same session_id. Chunk 1 (is_first_chunk=True)
    creates the operations_log row; chunks 2..N append their pend_updates /
    history_updates into the same row's payload so revert can undo every
    chunk's effect from a single click."""
    if not body.rows:
        raise HTTPException(400, "No rows provided")
    try:
        import uuid as _uuid
        from app.services.pend_alc_service import log_operation_upsert
        # session_id doubles as the OP_KEY for chunked uploads. Legacy
        # single-shot callers get a fresh UUID — same behavior as before.
        session_id = body.session_id or _uuid.uuid4().hex[:12]
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

            total_qty = sum(float(r.get("do_qty") or 0) for r in rows)
            # Chunk 1 INSERTs the audit row; chunks 2..N MERGE pend_updates /
            # history_updates into the existing row so the entire upload
            # remains revertable as one unit.
            log_operation_upsert(
                conn,
                op_type="DO",
                op_key=session_id,
                payload={
                    "session_id":      session_id,
                    "pend_updates":    do_result["pend_updates"],
                    "history_updates": hist_result["history_updates"],
                },
                summary=f"DO upload {session_id}: {len(rows)} input lines, "
                        f"{int(total_qty)} units, "
                        f"{do_result['touched']} pend_alc rows updated",
                rows_affected=do_result["touched"],
                qty_total=total_qty,
                created_by=getattr(current_user, "username", None),
                is_first=body.is_first_chunk,
                merge_payload_lists=["pend_updates", "history_updates"],
            )
        logger.info(
            f"[pend_alc] do-update by {getattr(current_user,'username','?')}: "
            f"{len(rows)} input → {do_result['touched']} pend_alc rows updated, "
            f"{hist_result['touched']} bdc_history rows updated, "
            f"session_id={session_id}, "
            f"chunk(first={body.is_first_chunk}, last={body.is_last_chunk})"
        )
        return {
            "success":              True,
            "updated_rows":         do_result["touched"],
            "bdc_history_updated":  hist_result["touched"],
            "session_id":           session_id,
            # Keep do_batch_id for backwards compatibility with any caller
            # that reads it (frontend currently ignores the value).
            "do_batch_id":          session_id,
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
        import time as _t
        _t0 = _t.perf_counter()

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

        # ----- PHASE 1: READ-ONLY aggregation -----
        # Done OUTSIDE the write transaction with WITH (NOLOCK) so concurrent
        # /pend-alc/* readers (Reconciliation page, MSA sync, dashboard tiles)
        # never block on Generate BDC. RCSI + the new covering index
        # IX_ARS_PEND_ALC_bdc_lookup turn this into a seek.
        with _engine().connect() as conn:
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
        _t_read = _t.perf_counter()

        # ----- PHASE 2: SHORT write transaction -----
        # Stamp + insert history + log operation in one quick connection;
        # release the connection before Excel build to free up the pool and
        # release any IX/X locks on ARS_PEND_ALC as soon as possible.
        article_rdc = [
            {"rdc": r[0], "st_cd": r[1], "article_number": r[2]}
            for r in rows
        ]
        history_rows_input = [
            {"rdc": r[0], "st_cd": r[1], "article_number": r[2],
             "maj_cat": r[3], "bdc_qty": float(r[4] or 0)}
            for r in rows
        ]
        total_qty = sum(float(r[4] or 0) for r in rows)
        username = getattr(current_user, "username", None)

        with _engine().connect() as conn:
            # Stamp BDC_QTY ONLY on the exact (RDC, ST_CD, ARTICLE) rows that
            # went into this BDC file. Scoping by all 3 keys is critical when
            # the user picks a date/store subset — otherwise BDC_QTY would
            # also get stamped on rows for stores that were NOT in the file.
            stamped_deltas = stamp_bdc_qty(conn, article_rdc)

            # Append one history row per (RDC, ST_CD, ARTICLE) line in this BDC.
            history_ids = insert_bdc_history(
                conn,
                allocation_number=allocation_no,
                rows=history_rows_input,
                created_by=username,
            )

            # Log the operation so it can be reverted later.
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
                created_by=username,
            )
        _t_write = _t.perf_counter()

        # ----- PHASE 3: Build the Excel AFTER the DB connection is closed -----
        # xlsxwriter in constant_memory mode streams rows to disk instead of
        # holding the whole workbook in RAM (the openpyxl default) — 5-10x
        # faster for ~100k-row BDC files.
        buf = io.BytesIO()
        try:
            import xlsxwriter
            wb = xlsxwriter.Workbook(buf, {"in_memory": True, "constant_memory": True})
            ws = wb.add_worksheet("BDC")
            headers = ["Serial No", "Allocation Date", "Allocation Number",
                       "VENDOR", "MATERIAL NO", "BDC-QTY", "RECEIVING STORE",
                       "Picking Date", "Remark"]
            for c, h in enumerate(headers):
                ws.write(0, c, h)
            for i, r in enumerate(rows, start=1):
                ws.write(i, 0, i)
                ws.write(i, 1, today_str)
                ws.write(i, 2, allocation_no)
                ws.write(i, 3, str(r[0] or "").strip())
                ws.write(i, 4, str(r[2] or "").strip().lstrip("0"))
                ws.write(i, 5, int(r[4] or 0))
                ws.write(i, 6, str(r[1] or "").strip())
                ws.write(i, 7, today_str)
                ws.write(i, 8, "")
            wb.close()
        except ImportError:
            # Fallback to openpyxl if xlsxwriter ever goes missing
            df = pd.DataFrame([
                {"Serial No": i + 1, "Allocation Date": today_str,
                 "Allocation Number": allocation_no,
                 "VENDOR": str(r[0] or "").strip(),
                 "MATERIAL NO": str(r[2] or "").strip().lstrip("0"),
                 "BDC-QTY": int(r[4] or 0),
                 "RECEIVING STORE": str(r[1] or "").strip(),
                 "Picking Date": today_str, "Remark": ""}
                for i, r in enumerate(rows)
            ])
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="BDC")
        buf.seek(0)
        _t_xls = _t.perf_counter()

        logger.info(
            f"[bdc-generate] {allocation_no}: rows={len(rows)} "
            f"read={_t_read - _t0:.2f}s "
            f"write={_t_write - _t_read:.2f}s "
            f"xlsx={_t_xls - _t_write:.2f}s "
            f"total={_t_xls - _t0:.2f}s"
        )

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
# Generic background-job registry — used by every long-running endpoint
# (BDC generate, DO upload, operation revert). Each job runs in a daemon
# thread so Cloudflare's 100s edge timeout never kills it; the UI polls
# /pend-alc/async-jobs/{job_id} and shows a completion modal when done.
#
# Job dict shape:
#   id, type ('bdc'|'do'|'revert'), status, progress, created_at,
#   started_at, finished_at, duration, error,
#   result (dict, type-specific),
#   zip_path (BDC only — path to the streamable ZIP)
# ---------------------------------------------------------------------------
import os as _os
import csv as _csv
import io as _io
import zipfile as _zipfile
import tempfile as _tempfile
import threading as _threading
import uuid as _uuid
from datetime import datetime as _dt

_jobs: dict = {}
_jobs_lock = _threading.Lock()


def _new_job(job_type: str, label: str = "") -> str:
    job_id = _uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id":         job_id,
            "type":       job_type,
            "label":      label,
            "status":     "pending",
            "progress":   "queued",
            "created_at": _dt.now().isoformat(),
            "started_at": None,
            "finished_at": None,
            "duration":   None,
            "error":      None,
            "result":     None,
            "zip_path":   None,
        }
    return job_id


def _job_update(job_id: str, **kwargs):
    with _jobs_lock:
        j = _jobs.get(job_id)
        if j:
            j.update(kwargs)


# Backwards-compat shim — older block referenced _bdc_job_update.
_bdc_job_update = _job_update


def _bdc_run_job(job_id: str, params: dict, store_filter, file_date_str: str,
                 today_str: str, username):
    """Worker: read rows → stamp → log → build ZIP-of-CSVs → mark complete."""
    import time as _t
    t0 = _t.perf_counter()
    try:
        _job_update(job_id, status="running", started_at=_dt.now().isoformat(),
                    progress="reading rows")

        filters = ["IS_CLOSED = 0", "PEND_QTY > 0"]
        if params.get("rdc"):
            filters.append("RDC = :rdc")
        if params.get("maj_cat"):
            filters.append("MAJ_CAT = :mc")
        if store_filter:
            placeholders = ",".join(f":st{i}" for i in range(len(store_filter)))
            filters.append(f"ISNULL(ST_CD,'') IN ({placeholders})")
        where = "WHERE " + " AND ".join(filters)

        sql_params = {}
        if params.get("rdc"):     sql_params["rdc"] = params["rdc"]
        if params.get("maj_cat"): sql_params["mc"]  = params["maj_cat"]
        if store_filter:
            for i, v in enumerate(store_filter):
                sql_params[f"st{i}"] = v

        from app.api.v1.endpoints.bdc import _get_next_allocation_no
        allocation_no = _get_next_allocation_no(_engine())

        with _engine().connect() as conn:
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
            """), sql_params).fetchall()

        if not rows:
            _job_update(job_id, status="failed", error="No open pending rows found for BDC",
                        finished_at=_dt.now().isoformat())
            return

        t_read = _t.perf_counter()
        _job_update(job_id, progress=f"stamping {len(rows):,} rows")

        article_rdc = [{"rdc": r[0], "st_cd": r[1], "article_number": r[2]} for r in rows]
        history_rows_input = [
            {"rdc": r[0], "st_cd": r[1], "article_number": r[2],
             "maj_cat": r[3], "bdc_qty": float(r[4] or 0)}
            for r in rows
        ]
        total_qty = sum(float(r[4] or 0) for r in rows)

        with _engine().connect() as conn:
            stamped_deltas = stamp_bdc_qty(conn, article_rdc)
            history_ids = insert_bdc_history(
                conn, allocation_number=allocation_no,
                rows=history_rows_input, created_by=username,
            )
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
                        f"{int(total_qty)} units, date={file_date_str}",
                rows_affected=len(rows),
                qty_total=total_qty,
                created_by=username,
            )

        t_write = _t.perf_counter()
        _job_update(job_id, progress="building per-RDC CSVs")

        # Group by RDC → one CSV each, bundled into a ZIP.
        per_rdc: dict = {}
        for r in rows:
            per_rdc.setdefault(str(r[0] or "").strip() or "UNKNOWN", []).append(r)

        headers = ["Serial No", "Allocation Date", "Allocation Number",
                   "VENDOR", "MATERIAL NO", "BDC-QTY", "RECEIVING STORE",
                   "Picking Date", "Remark"]
        tmp_dir = _os.path.join(_tempfile.gettempdir(), "bdc_jobs")
        _os.makedirs(tmp_dir, exist_ok=True)
        zip_path = _os.path.join(tmp_dir, f"{job_id}.zip")

        with _zipfile.ZipFile(zip_path, "w", _zipfile.ZIP_DEFLATED) as zf:
            for rdc_name in sorted(per_rdc.keys()):
                rdc_rows = per_rdc[rdc_name]
                buf = _io.StringIO()
                w = _csv.writer(buf, lineterminator="\n")
                w.writerow(headers)
                for i, r in enumerate(rdc_rows, start=1):
                    w.writerow([
                        i, today_str, allocation_no,
                        str(r[0] or "").strip(),
                        str(r[2] or "").strip().lstrip("0"),
                        int(r[4] or 0),
                        str(r[1] or "").strip(),
                        today_str, "",
                    ])
                safe_rdc = "".join(c if c.isalnum() or c in "_-" else "_" for c in rdc_name)
                csv_name = f"ARS_BDC_{safe_rdc}_{today_str.replace('-','')}_{allocation_no}.csv"
                zf.writestr(csv_name, buf.getvalue())

        t_zip = _t.perf_counter()
        logger.info(
            f"[bdc-generate-async] {allocation_no}: rows={len(rows):,} "
            f"rdcs={len(per_rdc)} "
            f"read={t_read-t0:.2f}s write={t_write-t_read:.2f}s "
            f"zip={t_zip-t_write:.2f}s total={t_zip-t0:.2f}s"
        )

        _job_update(
            job_id,
            status="completed",
            progress="done",
            finished_at=_dt.now().isoformat(),
            duration=round(t_zip - t0, 2),
            zip_path=zip_path,
            result={
                "allocation_no": allocation_no,
                "rdc_count":     len(per_rdc),
                "rdc_list":      sorted(per_rdc.keys()),
                "row_count":     len(rows),
                "total_qty":     int(total_qty),
                "file_date":     file_date_str,
                "download_url":  f"/api/v1/pend-alc/async-jobs/{job_id}/download",
            },
        )
    except Exception as e:
        logger.exception(f"[bdc-generate-async] job {job_id} failed: {e}")
        _job_update(job_id, status="failed", error=str(e)[:1000],
                    finished_at=_dt.now().isoformat())


@router.post("/bdc-generate-async")
def pend_alc_bdc_generate_async(
    rdc:         Optional[str]       = Query(None),
    maj_cat:     Optional[str]       = Query(None),
    target_date: Optional[str]       = Query(None),
    st_cd_list:  Optional[List[str]] = Query(None),
    current_user: User = Depends(get_current_user),
):
    """Async variant of /bdc-generate. Returns {job_id} immediately; the
    background worker stamps + builds a ZIP of per-RDC CSVs. Poll
    /async-jobs/{job_id} and GET /async-jobs/{job_id}/download when
    status='completed'.
    """
    import datetime as _datetime

    store_filter = None
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

    if target_date:
        try:
            file_date_str = _datetime.date.fromisoformat(target_date).strftime("%Y-%m-%d")
        except Exception:
            file_date_str = _datetime.date.today().strftime("%Y-%m-%d")
    else:
        file_date_str = _datetime.date.today().strftime("%Y-%m-%d")

    username = getattr(current_user, "username", None)
    label = f"BDC {file_date_str}" + (f" — {len(store_filter)} stores" if store_filter else "")
    job_id = _new_job("bdc", label=label)

    params = {"rdc": rdc, "maj_cat": maj_cat}
    _threading.Thread(
        target=_bdc_run_job,
        args=(job_id, params, store_filter, file_date_str, file_date_str, username),
        daemon=True,
    ).start()
    return {"success": True, "job_id": job_id}


# ---------------------------------------------------------------------------
# DO upload — async wrapper
# ---------------------------------------------------------------------------
def _do_run_job(job_id: str, rows: list, session_id: str, is_first: bool,
                username):
    """Worker: apply_do_deductions + history update + log_operation_upsert."""
    import time as _t
    t0 = _t.perf_counter()
    try:
        _job_update(job_id, status="running", started_at=_dt.now().isoformat(),
                    progress=f"applying {len(rows):,} input lines")
        from app.services.pend_alc_service import log_operation_upsert
        with _engine().connect() as conn:
            do_result   = apply_do_deductions(conn, rows)
            hist_result = update_bdc_history_with_do(conn, rows)
            total_qty = sum(float(r.get("do_qty") or 0) for r in rows)
            log_operation_upsert(
                conn,
                op_type="DO",
                op_key=session_id,
                payload={
                    "session_id":      session_id,
                    "pend_updates":    do_result["pend_updates"],
                    "history_updates": hist_result["history_updates"],
                },
                summary=f"DO upload {session_id}: {len(rows)} input lines, "
                        f"{int(total_qty)} units, "
                        f"{do_result['touched']} pend_alc rows updated",
                rows_affected=do_result["touched"],
                qty_total=total_qty,
                created_by=username,
                is_first=is_first,
                merge_payload_lists=["pend_updates", "history_updates"],
            )
        logger.info(
            f"[do-update-async] {session_id}: input={len(rows)} "
            f"touched={do_result['touched']} hist={hist_result['touched']} "
            f"first={is_first} total={_t.perf_counter()-t0:.2f}s"
        )
        _job_update(
            job_id,
            status="completed",
            progress="done",
            finished_at=_dt.now().isoformat(),
            duration=round(_t.perf_counter() - t0, 2),
            result={
                "session_id":          session_id,
                "do_batch_id":         session_id,
                "updated_rows":        do_result["touched"],
                "bdc_history_updated": hist_result["touched"],
                "input_lines":         len(rows),
                "total_qty":           int(total_qty),
            },
        )
    except Exception as e:
        logger.exception(f"[do-update-async] job {job_id} failed: {e}")
        _job_update(job_id, status="failed", error=str(e)[:1000],
                    finished_at=_dt.now().isoformat())


@router.post("/do-update-async")
def pend_alc_do_update_async(
    body: DoUpdateRequest,
    current_user: User = Depends(get_current_user),
):
    """Async variant of /do-update. Same body, returns {job_id} immediately.
    Poll /async-jobs/{job_id}; result carries session_id + counts."""
    if not body.rows:
        raise HTTPException(400, "No rows provided")
    import uuid as _uu
    session_id = body.session_id or _uu.uuid4().hex[:12]
    rows = [
        {"rdc": r.rdc, "article_number": r.article_number,
         "do_qty": r.do_qty, "do_number": r.do_number,
         "st_cd": r.st_cd, "allocation_number": r.allocation_number}
        for r in body.rows
    ]
    label = f"DO upload {session_id} ({len(rows)} lines)"
    job_id = _new_job("do", label=label)
    username = getattr(current_user, "username", None)
    _threading.Thread(
        target=_do_run_job,
        args=(job_id, rows, session_id, bool(body.is_first_chunk), username),
        daemon=True,
    ).start()
    # Return session_id eagerly so multi-chunk callers can reuse it on
    # subsequent chunks without waiting for the first chunk's job to finish.
    return {"success": True, "job_id": job_id, "session_id": session_id}


# ---------------------------------------------------------------------------
# Revert operation — async wrapper
# ---------------------------------------------------------------------------
def _revert_run_job(job_id: str, op_id: int, note: Optional[str], username):
    import time as _t
    t0 = _t.perf_counter()
    try:
        _job_update(job_id, status="running", started_at=_dt.now().isoformat(),
                    progress=f"reverting op #{op_id}")
        with _engine().connect() as conn:
            res = revert_operation(conn, op_id, reverted_by=username, note=note)
        if not res.get("success"):
            _job_update(job_id, status="failed",
                        error=res.get("error") or "Revert failed",
                        finished_at=_dt.now().isoformat())
            return
        logger.info(
            f"[revert-async] op_id={op_id} by {username}: {res} "
            f"total={_t.perf_counter()-t0:.2f}s"
        )
        _job_update(
            job_id,
            status="completed",
            progress="done",
            finished_at=_dt.now().isoformat(),
            duration=round(_t.perf_counter() - t0, 2),
            result={"op_id": op_id, **{k: v for k, v in res.items() if k != "success"}},
        )
    except Exception as e:
        logger.exception(f"[revert-async] job {job_id} failed: {e}")
        _job_update(job_id, status="failed", error=str(e)[:1000],
                    finished_at=_dt.now().isoformat())


class _RevertAsyncBody(BaseModel):
    note: Optional[str] = None


@router.post("/operations/{op_id}/revert-async")
def pend_alc_operations_revert_async(
    op_id: int,
    body:    Optional[_RevertAsyncBody] = None,
    confirm: bool = Query(False),
    current_user: User = Depends(get_current_user),
):
    """Async variant of /operations/{op_id}/revert. Returns {job_id} immediately."""
    if not confirm:
        raise HTTPException(400, "confirm=true is required to revert")
    note = (body.note if body else None)
    label = f"Revert op #{op_id}"
    job_id = _new_job("revert", label=label)
    username = getattr(current_user, "username", None)
    _threading.Thread(
        target=_revert_run_job,
        args=(job_id, op_id, note, username),
        daemon=True,
    ).start()
    return {"success": True, "job_id": job_id}


# ---------------------------------------------------------------------------
# Generic poll + download endpoints used by every async job above.
# ---------------------------------------------------------------------------
@router.get("/async-jobs/{job_id}")
def pend_alc_async_job_status(job_id: str,
                              current_user: User = Depends(get_current_user)):
    with _jobs_lock:
        j = _jobs.get(job_id)
        if not j:
            raise HTTPException(404, f"Job {job_id} not found")
        out = {k: v for k, v in j.items() if k != "zip_path"}
    return {"success": True, "data": out}


@router.get("/async-jobs/{job_id}/download")
def pend_alc_async_job_download(job_id: str,
                                current_user: User = Depends(get_current_user)):
    """Stream the BDC ZIP (only valid for type='bdc' jobs once complete)."""
    with _jobs_lock:
        j = _jobs.get(job_id)
        if not j:
            raise HTTPException(404, f"Job {job_id} not found")
        if j.get("status") != "completed":
            raise HTTPException(409, f"Job {job_id} status={j.get('status')}")
        zip_path = j.get("zip_path")
        res = j.get("result") or {}
        allocation_no = res.get("allocation_no") or "BDC"
        file_date     = res.get("file_date") or _dt.now().strftime("%Y-%m-%d")
    if not zip_path or not _os.path.exists(zip_path):
        raise HTTPException(410, "Download no longer available (server restart or cleanup)")

    fname = f"ARS_BDC_{file_date.replace('-','')}_{allocation_no}.zip"

    def _stream():
        with open(zip_path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)  # 1 MiB
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        _stream(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# Back-compat — keep the old /bdc-jobs/* paths working in case any caller
# (or earlier UI build) is still polling them.  Both forward to the generic
# handlers above.
@router.get("/bdc-jobs/{job_id}")
def pend_alc_bdc_job_status_legacy(job_id: str,
                                   current_user: User = Depends(get_current_user)):
    return pend_alc_async_job_status(job_id, current_user)


@router.get("/bdc-jobs/{job_id}/download")
def pend_alc_bdc_job_download_legacy(job_id: str,
                                     current_user: User = Depends(get_current_user)):
    return pend_alc_async_job_download(job_id, current_user)


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
# Operations log + revert (BDC / DO / MANUAL / APPROVE)
# ---------------------------------------------------------------------------
@router.get("/operations")
def pend_alc_operations_list(
    op_type:           Optional[str] = Query(None, description="BDC / DO / MANUAL / APPROVE"),
    include_reverted:  bool          = Query(True),
    limit:             int           = Query(200, ge=1, le=2000),
    current_user: User = Depends(get_current_user),
):
    """List recent BDC / DO / MANUAL / APPROVE operations for the audit + undo UI."""
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
    # Multi-chunk upload coordination. The frontend generates ONE session_id
    # for the whole upload and sends it with every chunk so all chunks land
    # under the same SESSION_ID and roll up to a SINGLE operations_log entry
    # — making the upload revertable as a single unit. All three fields are
    # optional for backwards compatibility (legacy clients still work).
    session_id:     Optional[str]  = None
    is_first_chunk: bool           = True
    is_last_chunk:  bool           = True


@router.post("/manual-upload")
def pend_alc_manual_upload(
    body: ManualUploadRequest,
    current_user: User = Depends(get_current_user),
):
    """Insert manually-allocated rows (SOURCE=MANUAL) into ARS_PEND_ALC.

    Apply +1 delta to ARS_MSA_TOTAL/GEN_ART/VAR_ART and every active
    ARS_GRID_MJ* table for the affected (RDC, ARTICLE_NUMBER) keys —
    PEND_QTY goes up, FNL_Q goes down, GRID.PEND_ALC goes up. Same
    treatment whether rows came from the manual entry table or a bulk
    CSV upload.

    Multi-chunk uploads: the frontend slices large uploads into chunks and
    sends each chunk with the same session_id. Chunk 1 (is_first_chunk=True)
    creates the operations_log row; later chunks UPDATE the same row,
    accumulating rows_affected and qty_total. Result: one log entry per
    upload, one revert click to undo the whole thing.
    """
    if not body.rows:
        raise HTTPException(400, "No rows provided")
    try:
        rows = [r.model_dump() for r in body.rows]
        article_rdc_pairs = [
            {"rdc": r["rdc"], "article_number": r["article_number"]}
            for r in rows
        ]
        from app.services.pend_alc_service import log_operation_upsert
        with _engine().connect() as conn:
            res = write_manual_pend_alc(conn, rows, session_id=body.session_id)

            # ── Deferred delta: per-chunk MSA+grid sync was making each
            # chunk take ~3-4 seconds, which on a 40-chunk upload meant 2+
            # minutes of waiting and frequent mid-stream interruptions. We
            # now skip the delta on chunks 1..N-1 (each chunk just runs
            # fast_executemany INSERT, ~1 sec) and run ONE delta covering
            # every row from this session_id when the last chunk arrives.
            # Net effect: same final state, ~3× faster, lock contention drops
            # because there's only one big UPDATE pass on MSA/grid.
            msa_adjusted = None
            if body.is_last_chunk:
                from sqlalchemy import text as _text
                all_rows = [
                    {
                        "rdc":            r[0],
                        "st_cd":          r[1],
                        "article_number": r[2],
                        "maj_cat":        r[3],
                        "gen_art_number": r[4],
                        "clr":            r[5],
                        "alloc_qty":      float(r[6] or 0),
                        "do_qty":         float(r[7] or 0),
                    }
                    for r in conn.execute(_text(f"""
                        SELECT RDC, ST_CD, ARTICLE_NUMBER, MAJ_CAT, GEN_ART_NUMBER, CLR,
                               ALLOC_QTY, ISNULL(DO_QTY, 0)
                        FROM ARS_PEND_ALC
                        WHERE SESSION_ID = :sid
                    """), {"sid": res["session_id"]}).fetchall()
                ]
                if all_rows:
                    msa_adjusted = apply_pend_alc_delta(conn, all_rows, sign=+1)
                    logger.info(
                        f"[pend_alc] deferred delta applied for session {res['session_id']}: "
                        f"{len(all_rows)} total rows synced to MSA + grids"
                    )

            # Log per-chunk: chunk 1 INSERTs the ops_log row, chunks 2..N
            # UPDATE it (accumulating rows_affected + qty_total).
            total_alloc = sum(float(r.get("alloc_qty") or 0) for r in rows)
            log_operation_upsert(
                conn,
                op_type="MANUAL",
                op_key=res["session_id"],
                payload={
                    "session_id":        res["session_id"],
                    "inserted_ids":      res["inserted_ids"],
                    "article_rdc_pairs": article_rdc_pairs,
                },
                summary=f"Manual upload {res['session_id']}",
                rows_affected=res["inserted"],
                qty_total=total_alloc,
                created_by=getattr(current_user, "username", None),
                is_first=body.is_first_chunk,
            )
        logger.info(
            f"[pend_alc] manual-upload by {getattr(current_user,'username','?')}: "
            f"{res['inserted']} rows inserted, session_id={res['session_id']}, "
            f"chunk(first={body.is_first_chunk}, last={body.is_last_chunk}), "
            f"delta_applied={msa_adjusted is not None}"
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
