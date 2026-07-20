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

import datetime
import io
from typing import List, Literal, Optional

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import text
from loguru import logger

from app.database.session import get_data_engine
from app.security.dependencies import get_current_user
from app.models.rbac import User
from app.services.pend_alc_service import (
    adjust_msa_after_pend_insert,
    apply_adhoc_close,
    apply_pend_alc_delta,
    apply_do_deductions,
    backfill_bdc_operations,
    backfill_approve_qty,
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
    close_orphan_open_bdc_history,
    recover_bdc_history_from_active_ops,
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


def _norm_alloc_type_field(v):
    """Pydantic normalizer for row-level alloc_type: None/'' → None,
    'fresh'/' GRT ' → 'FRESH'/'GRT', anything else → ValueError (422)."""
    if v is None:
        return None
    s = str(v).strip().upper()
    if s == "":
        return None
    if s in ("FRESH", "GRT"):
        return s
    raise ValueError("alloc_type must be FRESH, GRT, or blank")


def _alloc_type_pred(alloc_type: Optional[str], params: dict,
                     col: str = "P.ALLOC_TYPE") -> Optional[str]:
    """WHERE fragment for the optional alloc_type view filter.
    FRESH/GRT match typed rows exactly; LEGACY matches NULL/'' rows.
    Returns None when no filter is requested; raises 400 on junk."""
    if not alloc_type:
        return None
    at = alloc_type.strip().upper()
    if at == "LEGACY":
        return f"ISNULL({col},'') = ''"
    if at in ("FRESH", "GRT"):
        params["f_at"] = at
        return f"ISNULL({col},'') = :f_at"
    raise HTTPException(400, "alloc_type filter must be FRESH, GRT, or LEGACY")


# Excludes (RDC, ST_CD, ARTICLE) combos that already have an open BDC
# awaiting DO. A partial DO transitions the prior history row to
# CLOSED_PARTIAL (see apply_do_deductions), so it no longer matches here —
# the residual ALLOC_QTY-DO_QTY is free to flow into the next BDC.
_NO_OPEN_BDC_PREDICATE = (
    f"NOT EXISTS ("
    f"  SELECT 1 FROM {BDC_HISTORY_TABLE} h WITH (NOLOCK)"
    f"  WHERE h.RDC = {PEND_ALC_TABLE}.RDC"
    f"    AND ISNULL(h.ST_CD,'') = ISNULL({PEND_ALC_TABLE}.ST_CD,'')"
    f"    AND h.ARTICLE_NUMBER = {PEND_ALC_TABLE}.ARTICLE_NUMBER"
    f"    AND h.STATUS = 'OPEN'"
    f")"
)

# ── Dispatch-control exclusions (2026-07-18) ───────────────────────────────
# Three tables suppress BDC generation so controlled stock never dispatches:
#   ARS_HOLD_ARTICLE_BDC               (GEN_ART_NUMBER, CLR) — hold an article colour
#   ARS_DIVISION_DELETE_BDC            (STORE, DIV)          — no dispatch of a division to a store
#   ARS_DIVISION_DELETE_ON_MAJ_CAT_BDC (STORE, MAJ_CAT)      — no dispatch of a MAJ_CAT to a store
# Each is a correlated EXISTS against the pending row (full-table-name
# correlation, matching _NO_OPEN_BDC_PREDICATE). Generate BDC ANDs the NOT
# forms so matching lines drop out; the Gap report uses the EXISTS forms to
# surface what was held. DIV is resolved from MAJ_CAT via ARS_MSA_GEN_ART.
_MATCH_ARTICLE_HOLD = (
    f"EXISTS (SELECT 1 FROM ARS_HOLD_ARTICLE_BDC h WITH (NOLOCK) "
    f"WHERE h.GEN_ART_NUMBER = {PEND_ALC_TABLE}.GEN_ART_NUMBER "
    f"AND ISNULL(h.CLR,'') = ISNULL({PEND_ALC_TABLE}.CLR,''))"
)
_MATCH_DIV_DELETE = (
    f"EXISTS (SELECT 1 FROM ARS_DIVISION_DELETE_BDC dd WITH (NOLOCK) "
    f"JOIN (SELECT DISTINCT MAJ_CAT, DIV FROM ARS_MSA_GEN_ART) m "
    f"ON m.MAJ_CAT = {PEND_ALC_TABLE}.MAJ_CAT "
    f"WHERE dd.STORE = ISNULL({PEND_ALC_TABLE}.ST_CD,'') AND dd.DIV = m.DIV)"
)
_MATCH_STORE_MAJCAT = (
    f"EXISTS (SELECT 1 FROM ARS_DIVISION_DELETE_ON_MAJ_CAT_BDC d WITH (NOLOCK) "
    f"WHERE d.STORE = ISNULL({PEND_ALC_TABLE}.ST_CD,'') "
    f"AND d.MAJ_CAT = {PEND_ALC_TABLE}.MAJ_CAT)"
)
# (table, rule_label, match_sql). Order = precedence for display only.
_DISPATCH_CONTROL = [
    ("ARS_HOLD_ARTICLE_BDC",               "ARTICLE_HOLD", _MATCH_ARTICLE_HOLD),
    ("ARS_DIVISION_DELETE_BDC",            "DIV_DELETE",   _MATCH_DIV_DELETE),
    ("ARS_DIVISION_DELETE_ON_MAJ_CAT_BDC", "STORE_MAJCAT", _MATCH_STORE_MAJCAT),
]


def _existing_dispatch_controls(conn):
    """Subset of _DISPATCH_CONTROL whose tables exist (best-effort).

    Missing control tables must never 500 a BDC run — they simply mean
    "no exclusion for that rule" in this environment.
    """
    out = []
    for tbl, rule, match_sql in _DISPATCH_CONTROL:
        try:
            if conn.execute(text("SELECT OBJECT_ID(:t,'U')"), {"t": tbl}).scalar():
                out.append((tbl, rule, match_sql))
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# GET /pend-alc/summary
# ---------------------------------------------------------------------------
@router.get("/summary")
def pend_alc_summary(
    alloc_type: Optional[str] = Query(
        None, description="FRESH | GRT | LEGACY (NULL/'' rows)"),
    current_user: User = Depends(get_current_user),
):
    """Totals + breakdown by ALLOC_MODE, SOURCE and ALLOC_TYPE."""
    # Computed before the try so a bad filter surfaces as 400, not 500.
    params: dict = {}
    at_pred = _alloc_type_pred(alloc_type, params, col="ALLOC_TYPE")
    at_and  = f" AND {at_pred}" if at_pred else ""
    at_where = f" WHERE {at_pred}" if at_pred else ""
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
                {at_where}
            """), params).fetchone()

            by_mode = conn.execute(text(f"""
                SELECT ISNULL(ALLOC_MODE,'AUTO')     AS mode,
                       SUM(ALLOC_QTY) AS alloc_qty,
                       SUM(BDC_QTY)  AS bdc_qty,
                       SUM(DO_QTY)   AS do_qty,
                       SUM(PEND_QTY) AS pend_qty,
                       COUNT(*)      AS rows
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                WHERE IS_CLOSED = 0{at_and}
                GROUP BY ISNULL(ALLOC_MODE,'AUTO')
                ORDER BY SUM(PEND_QTY) DESC
            """), params).fetchall()

            by_source = conn.execute(text(f"""
                SELECT ISNULL(SOURCE,'AUTO')          AS source,
                       SUM(ALLOC_QTY) AS alloc_qty,
                       SUM(BDC_QTY)   AS bdc_qty,
                       SUM(DO_QTY)    AS do_qty,
                       SUM(PEND_QTY)  AS pend_qty,
                       COUNT(*)       AS rows
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                WHERE IS_CLOSED = 0{at_and}
                GROUP BY ISNULL(SOURCE,'AUTO')
                ORDER BY SUM(PEND_QTY) DESC
            """), params).fetchall()

            by_majcat = conn.execute(text(f"""
                SELECT MAJ_CAT,
                       SUM(ALLOC_QTY) AS alloc_qty,
                       SUM(BDC_QTY)   AS bdc_qty,
                       SUM(DO_QTY)    AS do_qty,
                       SUM(PEND_QTY)  AS pend_qty,
                       COUNT(*)       AS rows
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                WHERE IS_CLOSED = 0{at_and}
                GROUP BY MAJ_CAT
                ORDER BY SUM(PEND_QTY) DESC
            """), params).fetchall()

            by_alloc_type = conn.execute(text(f"""
                SELECT CASE WHEN ISNULL(ALLOC_TYPE,'') = '' THEN 'LEGACY'
                            ELSE ALLOC_TYPE END AS alloc_type,
                       SUM(ALLOC_QTY) AS alloc_qty,
                       SUM(BDC_QTY)   AS bdc_qty,
                       SUM(DO_QTY)    AS do_qty,
                       SUM(PEND_QTY)  AS pend_qty,
                       COUNT(*)       AS rows
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                WHERE IS_CLOSED = 0{at_and}
                GROUP BY CASE WHEN ISNULL(ALLOC_TYPE,'') = '' THEN 'LEGACY'
                              ELSE ALLOC_TYPE END
                ORDER BY SUM(PEND_QTY) DESC
            """), params).fetchall()

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
                "by_alloc_type": [
                    {"alloc_type": r[0], "alloc_qty": float(r[1] or 0),
                     "bdc_qty": float(r[2] or 0), "do_qty": float(r[3] or 0),
                     "pend_qty": float(r[4] or 0), "rows": int(r[5] or 0)}
                    for r in by_alloc_type
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
    """Sessions that still have open pending quantities, with mode breakdown.

    Adds `bdc_in_flight_qty` per session — qty currently in flight to SAP
    (open BDC, no DO yet) attributed by (RDC, ST_CD, ARTICLE) intersection
    with that session's PEND_ALC rows. When the same key sits in multiple
    sessions, the open BDC is split evenly across them; not a perfect
    accounting but the only deterministic answer without a FK from
    BDC_HISTORY → PEND_ALC, and stable for reconciliation.

    A session row is included in the fair-split for a key ONLY if its
    PEND_ALC row for that key has BDC_QTY > 0 (i.e., it actually
    participated in a BDC stamping). A freshly-approved session whose
    rows have never been stamped (BDC_QTY = 0 on every row for the
    matching key) should never appear in BDC IN FLIGHT — it is not in
    flight to SAP yet. This prevents bleed-through from older overlapping
    sessions onto a brand-new session that has nothing to do with that
    historical BDC."""
    try:
        with _engine().connect() as conn:
            ensure_pend_alc_table(conn)
            ensure_bdc_history_table(conn)
            rows = conn.execute(text(f"""
                ;WITH base AS (
                    SELECT SESSION_ID,
                           ISNULL(SOURCE,'AUTO')              AS source,
                           MIN(APPROVED_AT)                   AS approved_at,
                           SUM(ALLOC_QTY)                     AS alloc_qty,
                           SUM(BDC_QTY)                       AS bdc_qty,
                           SUM(DO_QTY)                        AS do_qty,
                           SUM(PEND_QTY)                      AS pend_qty,
                           COUNT(*)                           AS article_count
                    FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                    WHERE IS_CLOSED = 0
                    GROUP BY SESSION_ID, ISNULL(SOURCE,'AUTO')
                ),
                -- Only sessions whose PEND_ALC row for the key has been
                -- stamped (BDC_QTY > 0) participate in the in-flight
                -- attribution. A brand-new session that hasn't been part
                -- of any /bdc-generate run yet must not absorb open-BDC
                -- residual that belongs to an older overlapping session.
                stamped_keys AS (
                    SELECT P.SESSION_ID,
                           P.RDC, ISNULL(P.ST_CD,'') AS ST_CD, P.ARTICLE_NUMBER
                    FROM {PEND_ALC_TABLE} P WITH (NOLOCK)
                    WHERE P.IS_CLOSED = 0
                      AND ISNULL(P.BDC_QTY, 0) > 0
                    GROUP BY P.SESSION_ID, P.RDC, ISNULL(P.ST_CD,''),
                             P.ARTICLE_NUMBER
                ),
                key_session_count AS (
                    -- How many *stamped* open sessions share each
                    -- (RDC, ST_CD, ARTICLE). Used to fairly split open
                    -- BDC across overlapping participating sessions.
                    SELECT RDC, ST_CD, ARTICLE_NUMBER,
                           COUNT(DISTINCT SESSION_ID) AS n
                    FROM stamped_keys
                    GROUP BY RDC, ST_CD, ARTICLE_NUMBER
                ),
                session_keys AS (
                    SELECT DISTINCT SESSION_ID, RDC, ST_CD, ARTICLE_NUMBER
                    FROM stamped_keys
                ),
                inflight AS (
                    SELECT sk.SESSION_ID,
                           SUM((H.BDC_QTY - H.DO_RECEIVED) * 1.0 / k.n) AS qty
                    FROM {BDC_HISTORY_TABLE} H WITH (NOLOCK)
                    JOIN session_keys sk
                      ON sk.RDC = H.RDC
                     AND sk.ST_CD = ISNULL(H.ST_CD,'')
                     AND sk.ARTICLE_NUMBER = H.ARTICLE_NUMBER
                    JOIN key_session_count k
                      ON k.RDC = sk.RDC AND k.ST_CD = sk.ST_CD
                     AND k.ARTICLE_NUMBER = sk.ARTICLE_NUMBER
                    WHERE H.STATUS = 'OPEN' AND (H.BDC_QTY - H.DO_RECEIVED) > 0
                    GROUP BY sk.SESSION_ID
                )
                SELECT b.SESSION_ID, b.approved_at, b.source,
                       b.alloc_qty, b.bdc_qty, b.do_qty, b.pend_qty,
                       b.article_count,
                       ISNULL(i.qty, 0) AS bdc_in_flight_qty
                FROM base b
                LEFT JOIN inflight i ON i.SESSION_ID = b.SESSION_ID
                ORDER BY b.approved_at DESC
            """)).fetchall()
        return {
            "success": True,
            "data": [
                {
                    "session_id":        r[0],
                    "approved_at":       r[1].isoformat() if r[1] else None,
                    "source":            r[2],
                    "alloc_qty":         float(r[3] or 0),
                    "bdc_qty":           float(r[4] or 0),
                    "do_qty":            float(r[5] or 0),
                    "pend_qty":          float(r[6] or 0),
                    "article_count":     int(r[7] or 0),
                    "bdc_in_flight_qty": float(r[8] or 0),
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
    "alloc_type":     "P.ALLOC_TYPE",
    "bdc_alloc_no":   "B.ALLOCATION_NUMBER",
    "bdc_status":     "B.STATUS",
}


@router.get("/detail")
def pend_alc_detail(
    session_id:   Optional[str]  = Query(None),
    maj_cat:      Optional[str]  = Query(None),
    alloc_mode:   Optional[str]  = Query(None),
    source:       Optional[str]  = Query(None),
    alloc_type:   Optional[str]  = Query(
        None, description="FRESH | GRT | LEGACY (NULL/'' rows)"),
    closed:       Optional[bool] = Query(None),
    # Pagination
    page:         int            = Query(1,   ge=1),
    page_size:    int            = Query(100, ge=1, le=10000),
    # Sort
    sort_by:      Optional[str]  = Query(None),
    sort_dir:     str            = Query("desc", pattern="^(asc|desc)$"),
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
        at_pred = _alloc_type_pred(alloc_type, params)
        if at_pred:
            filters.append(at_pred)

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
                    B.ALLOCATION_NUMBER, B.STATUS, B.DO_RECEIVED, B.BDC_DATE,
                    P.ALLOC_TYPE
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
                    "alloc_type":     r[25] or None,
                }
                for r in rows
            ],
        }
    except HTTPException:
        raise
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
    # Optional — when provided, apply_do_deductions resolves the BDC's
    # ST_CD and pins the deduction to that destination store. When blank,
    # falls through to FIFO on (RDC, ST_CD, ART) or (RDC, ART). UI labels
    # this field "Allocation No. (OPTIONAL)".
    allocation_number: Optional[str] = None
    st_cd:             Optional[str] = None
    do_number:         Optional[str] = None
    # Optional typed scoping (Package 3) — when FRESH/GRT, the FIFO only
    # drains PEND rows whose folded ALLOC_TYPE matches (legacy NULL/'' pend
    # folds to FRESH). Blank = today's type-agnostic behaviour.
    alloc_type:        Optional[str] = None

    @field_validator("alloc_type", mode="before")
    @classmethod
    def _v_alloc_type(cls, v):
        return _norm_alloc_type_field(v)


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
    # FS-12 — DO session-wise deduction. NOTE: `session_id` above is the
    # upload/ops-log key; `target_session_id` is a PEND_ALC allocation
    # SESSION_ID and deliberately named differently. Required (400) when
    # deduction_method != FIFO — see _validate_deduction_method().
    deduction_method:  Literal["FIFO", "SESSION_FIRST", "SESSION_ONLY"] = "FIFO"
    target_session_id: Optional[str] = None


def _validate_deduction_method(body: "DoUpdateRequest") -> None:
    """FS-12: non-FIFO methods must name the PEND_ALC session to target."""
    if body.deduction_method != "FIFO" and not (body.target_session_id or "").strip():
        raise HTTPException(
            400,
            f"target_session_id is required when "
            f"deduction_method={body.deduction_method}",
        )


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
    _validate_deduction_method(body)
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
             "allocation_number": r.allocation_number,
             "alloc_type": r.alloc_type}
            for r in body.rows
        ]
        with _engine().connect() as conn:
            do_result   = apply_do_deductions(
                conn, rows,
                deduction_method=body.deduction_method,
                target_session_id=body.target_session_id,
            )
            hist_result = update_bdc_history_with_do(conn, rows)
            cancel_count = sum(
                1 for h in (hist_result.get("history_updates") or [])
                if (h.get("new_status") or "").upper() == "CANCELLED"
            )

            total_qty = sum(float(r.get("do_qty") or 0) for r in rows)
            overflow_rows = do_result.get("overflow_rows") or []
            overflow_total = float(do_result.get("overflow_total_qty") or 0)
            # Chunk 1 INSERTs the audit row; chunks 2..N MERGE pend_updates /
            # history_updates into the existing row so the entire upload
            # remains revertable as one unit.
            log_operation_upsert(
                conn,
                op_type="DO",
                op_key=session_id,
                payload={
                    "session_id":          session_id,
                    # FS-12 audit: how this upload's deduction was targeted.
                    "deduction_method":    body.deduction_method,
                    "target_session_id":   body.target_session_id,
                    "pend_updates":        do_result["pend_updates"],
                    "history_updates":     hist_result["history_updates"],
                    # auto_history_closes = OPEN→CONFIRMED side-effect
                    # writes done by apply_do_deductions when a PEND row
                    # just closed. Revert restores them to OPEN.
                    "auto_history_closes": do_result.get("auto_history_closes") or [],
                    # D-2 fix: persist DO over-ship in the audit payload so
                    # ops can review what didn't land.
                    "overflow_rows":       overflow_rows,
                },
                summary=(
                    f"DO upload {session_id}: {len(rows)} input lines, "
                    f"{int(total_qty)} units, "
                    f"{do_result['touched']} pend_alc rows updated"
                    + (f", {cancel_count} BDC cancelled (DO_QTY=0)"
                       if cancel_count else "")
                    + (f", overflow={int(overflow_total)} units"
                       if overflow_total > 0 else "")
                ),
                rows_affected=do_result["touched"],
                qty_total=total_qty,
                created_by=getattr(current_user, "username", None),
                is_first=body.is_first_chunk,
                merge_payload_lists=["pend_updates", "history_updates",
                                     "auto_history_closes", "overflow_rows"],
            )
        logger.info(
            f"[pend_alc] do-update by {getattr(current_user,'username','?')}: "
            f"{len(rows)} input → {do_result['touched']} pend_alc rows updated, "
            f"{hist_result['touched']} bdc_history rows updated, "
            f"{cancel_count} cancelled, "
            f"{len(do_result.get('auto_history_closes') or [])} bdc_history auto-closed, "
            f"overflow_rows={len(overflow_rows)} ({overflow_total} units), "
            f"session_id={session_id}, "
            f"chunk(first={body.is_first_chunk}, last={body.is_last_chunk})"
        )
        return {
            "success":              True,
            "updated_rows":         do_result["touched"],
            "bdc_history_updated":  hist_result["touched"],
            "bdc_cancelled":        cancel_count,
            "session_id":           session_id,
            # Keep do_batch_id for backwards compatibility with any caller
            # that reads it (frontend currently ignores the value).
            "do_batch_id":          session_id,
            "overflow_rows":        overflow_rows,
            "overflow_total_qty":   overflow_total,
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
        filters, params = ["IS_CLOSED = 0", "PEND_QTY > 0",
                           _NO_OPEN_BDC_PREDICATE], {}
        if rdc:
            filters.append("RDC = :rdc"); params["rdc"] = rdc
        if maj_cat:
            filters.append("MAJ_CAT = :mc"); params["mc"] = maj_cat
        where = "WHERE " + " AND ".join(filters)

        with _engine().connect() as conn:
            ensure_pend_alc_table(conn)
            # Preview must mirror Generate BDC — apply the same dispatch-control
            # exclusions so held lines don't appear as "will be sent".
            _dispatch_and = "".join(
                f" AND NOT {m}" for _, _, m in _existing_dispatch_controls(conn)
            )
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
                {where}{_dispatch_and}
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
# GET /pend-alc/dispatch-gap — pending lines HELD from BDC by dispatch controls
# ---------------------------------------------------------------------------
def _dispatch_gap_query(conn, rdc: Optional[str]):
    """Open pending lines that Generate BDC will SKIP due to a control table.

    Tags each held (RDC, ST_CD, GEN_ART, CLR, MAJ_CAT) group with which
    rule(s) matched. Returns (detail, summary, total_held_qty). Held stock
    stays pending — this is the visibility for it.
    """
    controls = _existing_dispatch_controls(conn)
    if not controls:
        return [], [], 0.0
    params: dict = {}
    base = ["IS_CLOSED = 0", "PEND_QTY > 0", _NO_OPEN_BDC_PREDICATE]
    if rdc:
        base.append("RDC = :rdc"); params["rdc"] = rdc
    where = "WHERE " + " AND ".join(base)
    rules = [rule for _, rule, _ in controls]
    # Row-level flags in the CTE (no grouping → correlated EXISTS is legal),
    # then aggregate the flags with MAX per held group.
    cte_flags = ",\n                   ".join(
        f"CASE WHEN {m} THEN 1 ELSE 0 END AS f_{rule}" for _, rule, m in controls)
    out_flags = ",\n               ".join(f"MAX(f_{rule}) AS f_{rule}" for rule in rules)
    any_flag  = " OR ".join(f"f_{rule} = 1" for rule in rules)
    sql = f"""
        WITH tagged AS (
            SELECT RDC,
                   ISNULL(ST_CD,'')          AS ST_CD,
                   ISNULL(GEN_ART_NUMBER,'') AS GEN_ART_NUMBER,
                   ISNULL(CLR,'')            AS CLR,
                   MAJ_CAT, PEND_QTY,
                   {cte_flags}
            FROM {PEND_ALC_TABLE} WITH (NOLOCK)
            {where}
        )
        SELECT RDC, ST_CD, GEN_ART_NUMBER, CLR, MAJ_CAT,
               (SELECT TOP 1 DIV FROM ARS_MSA_GEN_ART g WITH (NOLOCK)
                  WHERE g.MAJ_CAT = tagged.MAJ_CAT) AS DIV,
               SUM(PEND_QTY) AS PEND_QTY,
               {out_flags}
        FROM tagged
        WHERE {any_flag}
        GROUP BY RDC, ST_CD, GEN_ART_NUMBER, CLR, MAJ_CAT
        ORDER BY RDC, ST_CD, MAJ_CAT
    """
    rows = conn.execute(text(sql), params).fetchall()
    detail = []
    summ = {r: {"rule": r, "rows": 0, "pend_qty": 0.0} for r in rules}
    total = 0.0
    for row in rows:
        pq = float(row[6] or 0)
        flags = {rules[i]: int(row[7 + i] or 0) for i in range(len(rules))}
        blocked = [r for r in rules if flags[r]]
        detail.append({
            "rdc": row[0], "st_cd": row[1], "gen_art_number": row[2],
            "clr": row[3], "maj_cat": row[4], "div": row[5],
            "pend_qty": pq, "blocked_by": blocked,
        })
        total += pq
        for r in blocked:
            summ[r]["rows"] += 1
            summ[r]["pend_qty"] += pq
    return detail, list(summ.values()), total


@router.get("/dispatch-gap")
def pend_alc_dispatch_gap(
    rdc: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
):
    """Reconciliation Gap report — pending stock withheld from BDC by the
    three dispatch-control tables (article-hold / division-delete / store×
    maj_cat), with a per-rule summary."""
    try:
        with _engine().connect() as conn:
            detail, summary, total = _dispatch_gap_query(conn, rdc)
        return {"success": True, "count": len(detail),
                "total_held_qty": total, "summary": summary, "detail": detail}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/dispatch-gap/export")
def pend_alc_dispatch_gap_export(
    rdc: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
):
    """Excel export of the dispatch-control Gap report detail."""
    try:
        with _engine().connect() as conn:
            detail, _, _ = _dispatch_gap_query(conn, rdc)
        cols = ["RDC", "STORE", "GEN_ART_NUMBER", "CLR", "MAJ_CAT", "DIV",
                "PEND_QTY", "BLOCKED_BY"]
        df = pd.DataFrame([{
            "RDC": d["rdc"], "STORE": d["st_cd"], "GEN_ART_NUMBER": d["gen_art_number"],
            "CLR": d["clr"], "MAJ_CAT": d["maj_cat"], "DIV": d["div"],
            "PEND_QTY": d["pend_qty"], "BLOCKED_BY": ", ".join(d["blocked_by"]),
        } for d in detail], columns=cols)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            df.to_excel(xw, index=False, sheet_name="Dispatch Gap")
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=dispatch_control_gap.xlsx"},
        )
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

        filters, params = ["IS_CLOSED = 0", "PEND_QTY > 0",
                           _NO_OPEN_BDC_PREDICATE], {}
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

        from app.api.v1.endpoints.bdc import _get_next_allocation_no

        # ----- PHASE 1: READ-ONLY aggregation -----
        # Done OUTSIDE the write transaction with WITH (NOLOCK) so concurrent
        # /pend-alc/* readers (Reconciliation page, MSA sync, dashboard tiles)
        # never block on Generate BDC. RCSI + the new covering index
        # IX_ARS_PEND_ALC_bdc_lookup turn this into a seek.
        with _engine().connect() as conn:
            # Dispatch-control exclusions: drop pending lines matching any of
            # the 3 control tables so they never generate a BDC (held).
            _dispatch_and = "".join(
                f" AND NOT {m}" for _, _, m in _existing_dispatch_controls(conn)
            )
            rows = conn.execute(text(f"""
                SELECT RDC,
                       ISNULL(ST_CD,'')          AS ST_CD,
                       ARTICLE_NUMBER,
                       MAJ_CAT,
                       SUM(PEND_QTY)             AS PEND_QTY
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                {where}{_dispatch_and}
                GROUP BY RDC, ISNULL(ST_CD,''), ARTICLE_NUMBER, MAJ_CAT
                HAVING SUM(PEND_QTY) > 0
                ORDER BY RDC, ST_CD, MAJ_CAT, ARTICLE_NUMBER
            """), params).fetchall()

        if not rows:
            raise HTTPException(404, "No open pending rows found for BDC")
        _t_read = _t.perf_counter()

        # Group rows by RDC — one allocation number + one history batch +
        # one operations-log entry per (FY, RDC). Each RDC's BDC is
        # independently revertable.
        rows_by_rdc: dict = {}
        for r in rows:
            rdc_key = str(r[0] or "").strip() or "UNKNOWN"
            rows_by_rdc.setdefault(rdc_key, []).append(r)

        # Assign allocation numbers per RDC before the write transaction so
        # the Excel writer can stamp the right number on each row.
        alloc_by_rdc: dict = {}
        eng = _engine()
        for rdc_key in sorted(rows_by_rdc.keys()):
            alloc_by_rdc[rdc_key] = _get_next_allocation_no(eng, rdc=rdc_key)

        total_qty = sum(float(r[4] or 0) for r in rows)
        username = getattr(current_user, "username", None)

        # ----- PHASE 2: SHORT write transaction (per-RDC stamp + history) -----
        with _engine().connect() as conn:
            for rdc_key in sorted(rows_by_rdc.keys()):
                rdc_rows = rows_by_rdc[rdc_key]
                allocation_no = alloc_by_rdc[rdc_key]
                article_rdc = [
                    {"rdc": r[0], "st_cd": r[1], "article_number": r[2]}
                    for r in rdc_rows
                ]
                history_rows_input = [
                    {"rdc": r[0], "st_cd": r[1], "article_number": r[2],
                     "maj_cat": r[3], "bdc_qty": float(r[4] or 0)}
                    for r in rdc_rows
                ]
                rdc_total_qty = sum(float(r[4] or 0) for r in rdc_rows)

                # Stamp BDC_QTY ONLY on the exact (RDC, ST_CD, ARTICLE) rows
                # that went into this RDC's slice — scoping by all 3 keys is
                # critical when the user picks a date/store subset.
                # FS-13 fix #3: store-scoped generate drops the empty-ST_CD
                # wildcard so it can't stamp all stores for (RDC, ARTICLE).
                stamped_deltas = stamp_bdc_qty(
                    conn, article_rdc, store_scoped=bool(store_filter))
                history_ids = insert_bdc_history(
                    conn,
                    allocation_number=allocation_no,
                    rows=history_rows_input,
                    created_by=username,
                )
                log_operation(
                    conn,
                    op_type="BDC",
                    op_key=allocation_no,
                    payload={
                        "allocation_number": allocation_no,
                        "rdc":               rdc_key,
                        "history_ids":       history_ids,
                        "stamped_rows":      stamped_deltas,
                    },
                    summary=f"BDC {allocation_no}: {len(rdc_rows)} lines, "
                            f"{int(rdc_total_qty)} units, "
                            f"date={file_date_str}",
                    rows_affected=len(rdc_rows),
                    qty_total=rdc_total_qty,
                    created_by=username,
                )
        _t_write = _t.perf_counter()

        # Stable "primary" alloc number for callers that expect a single
        # string — concatenated when there's more than one RDC.
        allocation_no = ",".join(alloc_by_rdc[k] for k in sorted(alloc_by_rdc))

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
                rdc_key = str(r[0] or "").strip() or "UNKNOWN"
                ws.write(i, 0, i)
                ws.write(i, 1, today_str)
                ws.write(i, 2, alloc_by_rdc.get(rdc_key, ""))
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
                 "Allocation Number": alloc_by_rdc.get(
                     str(r[0] or "").strip() or "UNKNOWN", ""),
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
            f"[bdc-generate] allocs={allocation_no} rows={len(rows)} "
            f"rdcs={len(alloc_by_rdc)} "
            f"read={_t_read - _t0:.2f}s "
            f"write={_t_write - _t_read:.2f}s "
            f"xlsx={_t_xls - _t_write:.2f}s "
            f"total={_t_xls - _t0:.2f}s"
        )

        # Filename: when multiple RDCs share one Excel, the alloc-no list can
        # blow past Windows filename limits — use the first alloc + count.
        fname_alloc = (
            next(iter(sorted(alloc_by_rdc.values())), "")
            + (f"_plus{len(alloc_by_rdc) - 1}" if len(alloc_by_rdc) > 1 else "")
        )
        fname = f"ARS_BDC_{datetime.date.today().strftime('%Y%m%d')}_{fname_alloc}.xlsx"
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

# ---------------------------------------------------------------------------
# FS-13 fix #1 — cross-worker job persistence. `_jobs` is a per-process dict;
# under gunicorn -w N a poll/download request can land on a worker that never
# saw the job and 404 even though the job is running (or has already committed
# its stamps). Every state change is written through to ARS_ASYNC_JOBS so ANY
# worker can answer /async-jobs/{id}; the dict stays as a warm cache.
#
# Deviation from the FS-13 DDL sketch (progress INT): `progress` is stored as
# NVARCHAR(200) because job progress is a human-readable string ("applied
# 125,000 / 213,050 rows") that both frontends render verbatim — an INT would
# lose the message on every cross-worker poll (the common case with 4 workers).
# ---------------------------------------------------------------------------
import json as _json

ASYNC_JOBS_TABLE = "ARS_ASYNC_JOBS"

_ASYNC_JOBS_DDL = f"""
IF OBJECT_ID('dbo.{ASYNC_JOBS_TABLE}','U') IS NULL
CREATE TABLE dbo.{ASYNC_JOBS_TABLE} (
    job_id      NVARCHAR(50)   NOT NULL,
    kind        NVARCHAR(20)   NULL,
    status      NVARCHAR(20)   NULL,
    progress    NVARCHAR(200)  NULL,
    error       NVARCHAR(1000) NULL,
    result_json NVARCHAR(MAX)  NULL,
    zip_path    NVARCHAR(500)  NULL,
    created_at  DATETIME       NOT NULL DEFAULT GETDATE(),
    updated_at  DATETIME       NULL,
    CONSTRAINT PK_ARS_ASYNC_JOBS PRIMARY KEY (job_id)
)
"""

_async_jobs_table_ready = False


def _ensure_async_jobs_table(conn) -> None:
    """Idempotent first-use DDL guard — same pattern as ensure_*_table()."""
    global _async_jobs_table_ready
    if _async_jobs_table_ready:
        return
    conn.execute(text(_ASYNC_JOBS_DDL))
    conn.commit()
    _async_jobs_table_ready = True


def _job_db_write(job_id: str) -> None:
    """Write-through of the in-memory job dict to ARS_ASYNC_JOBS (upsert).

    Best-effort: a DB hiccup must never kill the job thread — same-worker
    polls are still served from the dict."""
    with _jobs_lock:
        j = dict(_jobs.get(job_id) or {})
    if not j:
        return
    try:
        result = j.get("result")
        params = {
            "id":       job_id,
            "kind":     (str(j.get("type") or ""))[:20],
            "status":   (str(j.get("status") or ""))[:20],
            "progress": (str(j.get("progress") or ""))[:200],
            "error":    (str(j.get("error"))[:1000] if j.get("error") else None),
            "result":   (_json.dumps(result, default=str)
                         if result is not None else None),
            "zip":      j.get("zip_path"),
        }
        with _engine().connect() as conn:
            _ensure_async_jobs_table(conn)
            conn.execute(text(f"""
                UPDATE {ASYNC_JOBS_TABLE}
                   SET kind = :kind, status = :status, progress = :progress,
                       error = :error, result_json = :result,
                       zip_path = :zip, updated_at = GETDATE()
                 WHERE job_id = :id;
                IF @@ROWCOUNT = 0
                INSERT INTO {ASYNC_JOBS_TABLE}
                       (job_id, kind, status, progress, error, result_json,
                        zip_path, updated_at)
                VALUES (:id, :kind, :status, :progress, :error, :result,
                        :zip, GETDATE())
            """), params)
            conn.commit()
    except Exception as e:
        logger.warning(f"[async-jobs] DB write-through failed for {job_id}: {e}")


def _job_db_read(job_id: str) -> Optional[dict]:
    """Rebuild a poll-shaped job dict from ARS_ASYNC_JOBS.

    Cross-worker / post-restart path: returns None when the row doesn't
    exist (true 404) or the DB is unreachable (caller falls back to 404)."""
    try:
        with _engine().connect() as conn:
            _ensure_async_jobs_table(conn)
            r = conn.execute(text(f"""
                SELECT job_id, kind, status, progress, error, result_json,
                       zip_path, created_at, updated_at
                FROM {ASYNC_JOBS_TABLE} WITH (NOLOCK)
                WHERE job_id = :id
            """), {"id": job_id}).fetchone()
    except Exception as e:
        logger.warning(f"[async-jobs] DB read failed for {job_id}: {e}")
        return None
    if not r:
        return None
    result = None
    if r[5]:
        try:
            result = _json.loads(r[5])
        except Exception:
            result = None
    return {
        "id":          r[0],
        "type":        r[1],
        "status":      r[2],
        "progress":    r[3],
        "error":       r[4],
        "result":      result,
        "zip_path":    r[6],
        "created_at":  r[7].isoformat() if r[7] else None,
        "finished_at": r[8].isoformat() if r[8] else None,
    }


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
    _job_db_write(job_id)
    return job_id


def _job_update(job_id: str, **kwargs):
    with _jobs_lock:
        j = _jobs.get(job_id)
        if j:
            j.update(kwargs)
    _job_db_write(job_id)


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

        filters = ["IS_CLOSED = 0", "PEND_QTY > 0", _NO_OPEN_BDC_PREDICATE]
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

        with _engine().connect() as conn:
            # Dispatch-control exclusions — same as the sync path so async
            # generate holds the same lines.
            _dispatch_and = "".join(
                f" AND NOT {m}" for _, _, m in _existing_dispatch_controls(conn)
            )
            rows = conn.execute(text(f"""
                SELECT RDC,
                       ISNULL(ST_CD,'')          AS ST_CD,
                       ARTICLE_NUMBER,
                       MAJ_CAT,
                       SUM(PEND_QTY)             AS PEND_QTY
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                {where}{_dispatch_and}
                GROUP BY RDC, ISNULL(ST_CD,''), ARTICLE_NUMBER, MAJ_CAT
                HAVING SUM(PEND_QTY) > 0
                ORDER BY RDC, ST_CD, MAJ_CAT, ARTICLE_NUMBER
            """), sql_params).fetchall()

        if not rows:
            # FS-13 fix #4 — self-explanatory failure. Count the rows that
            # match every filter EXCEPT _NO_OPEN_BDC_PREDICATE: those are
            # excluded solely because an open BDC already covers their
            # (RDC, ST_CD, ARTICLE) key.
            err = "No open pending rows found for BDC"
            try:
                other = [f for f in filters if f != _NO_OPEN_BDC_PREDICATE]
                blocked_where = ("WHERE " + " AND ".join(other)
                                 + f" AND NOT ({_NO_OPEN_BDC_PREDICATE})")
                with _engine().connect() as conn:
                    blocked = int(conn.execute(text(
                        f"SELECT COUNT(*) FROM {PEND_ALC_TABLE} WITH (NOLOCK) "
                        f"{blocked_where}"
                    ), sql_params).scalar() or 0)
                if blocked > 0:
                    err += (f" — {blocked} rows are blocked by open BDC "
                            f"history (revert or close them from Operations "
                            f"Log / Open BDC Report)")
            except Exception as diag_err:
                logger.warning(
                    f"[bdc-generate-async] blocked-row diagnostic failed: {diag_err}"
                )
            _job_update(job_id, status="failed", error=err,
                        finished_at=_dt.now().isoformat())
            return

        t_read = _t.perf_counter()

        # Group by RDC + assign one allocation number per RDC.
        per_rdc: dict = {}
        for r in rows:
            per_rdc.setdefault(str(r[0] or "").strip() or "UNKNOWN", []).append(r)
        alloc_by_rdc: dict = {}
        eng = _engine()
        for rdc_name in sorted(per_rdc.keys()):
            alloc_by_rdc[rdc_name] = _get_next_allocation_no(eng, rdc=rdc_name)

        total_qty = sum(float(r[4] or 0) for r in rows)
        _job_update(job_id, progress=f"stamping {len(rows):,} rows across "
                                     f"{len(per_rdc)} RDC(s)")

        # One stamp + history + log_operation per RDC so each warehouse's
        # BDC is independently revertable.
        with _engine().connect() as conn:
            for rdc_name in sorted(per_rdc.keys()):
                rdc_rows = per_rdc[rdc_name]
                allocation_no = alloc_by_rdc[rdc_name]
                article_rdc = [
                    {"rdc": r[0], "st_cd": r[1], "article_number": r[2]}
                    for r in rdc_rows
                ]
                history_rows_input = [
                    {"rdc": r[0], "st_cd": r[1], "article_number": r[2],
                     "maj_cat": r[3], "bdc_qty": float(r[4] or 0)}
                    for r in rdc_rows
                ]
                rdc_total_qty = sum(float(r[4] or 0) for r in rdc_rows)
                # FS-13 fix #3: store-scoped generate must never let an
                # empty-ST_CD pair wildcard-stamp every store's rows.
                stamped_deltas = stamp_bdc_qty(
                    conn, article_rdc, store_scoped=bool(store_filter))
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
                        "rdc":               rdc_name,
                        "history_ids":       history_ids,
                        "stamped_rows":      stamped_deltas,
                    },
                    summary=f"BDC {allocation_no}: {len(rdc_rows)} lines, "
                            f"{int(rdc_total_qty)} units, date={file_date_str}",
                    rows_affected=len(rdc_rows),
                    qty_total=rdc_total_qty,
                    created_by=username,
                )

        t_write = _t.perf_counter()
        _job_update(job_id, progress="building per-RDC CSVs")

        headers = ["Serial No", "Allocation Date", "Allocation Number",
                   "VENDOR", "MATERIAL NO", "BDC-QTY", "RECEIVING STORE",
                   "Picking Date", "Remark"]
        tmp_dir = _os.path.join(_tempfile.gettempdir(), "bdc_jobs")
        _os.makedirs(tmp_dir, exist_ok=True)
        zip_path = _os.path.join(tmp_dir, f"{job_id}.zip")

        with _zipfile.ZipFile(zip_path, "w", _zipfile.ZIP_DEFLATED) as zf:
            for rdc_name in sorted(per_rdc.keys()):
                rdc_rows = per_rdc[rdc_name]
                rdc_alloc = alloc_by_rdc[rdc_name]
                buf = _io.StringIO()
                w = _csv.writer(buf, lineterminator="\n")
                w.writerow(headers)
                for i, r in enumerate(rdc_rows, start=1):
                    w.writerow([
                        i, today_str, rdc_alloc,
                        str(r[0] or "").strip(),
                        str(r[2] or "").strip().lstrip("0"),
                        int(r[4] or 0),
                        str(r[1] or "").strip(),
                        today_str, "",
                    ])
                safe_rdc = "".join(c if c.isalnum() or c in "_-" else "_" for c in rdc_name)
                csv_name = f"ARS_BDC_{safe_rdc}_{today_str.replace('-','')}_{rdc_alloc}.csv"
                zf.writestr(csv_name, buf.getvalue())

        t_zip = _t.perf_counter()
        joined_allocs = ",".join(alloc_by_rdc[k] for k in sorted(alloc_by_rdc))
        logger.info(
            f"[bdc-generate-async] allocs={joined_allocs} rows={len(rows):,} "
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
                "allocation_no":  joined_allocs,  # legacy field
                "allocation_nos": alloc_by_rdc,    # per-RDC mapping
                "rdc_count":      len(per_rdc),
                "rdc_list":       sorted(per_rdc.keys()),
                "row_count":      len(rows),
                "total_qty":      int(total_qty),
                "file_date":      file_date_str,
                "download_url":   f"/api/v1/pend-alc/async-jobs/{job_id}/download",
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
# Server-side micro-batch size. Bigger = fewer SQL passes, smaller = more
# frequent progress updates. 25K balances ~1s of SQL work per slice (set-based
# UPDATE on both ARS_PEND_ALC and ARS_BDC_HISTORY) against UX responsiveness.
_DO_SLICE = 25_000


def _do_run_job(job_id: str, rows: list, session_id: str, is_first: bool,
                username, deduction_method: str = "FIFO",
                target_session_id: Optional[str] = None):
    """Worker: apply DO deductions to ARS_PEND_ALC + history update +
    log_operation_upsert, slicing the input internally so progress can be
    reported mid-flight.

    The old design relied on the frontend to chunk a 200K-row upload into
    8-9 sequential POSTs; every chunk paid HTTP RTT + a polling window, and
    a single transient 502 on chunk N marked that chunk as failed even
    though the backend job had succeeded. Now the entire payload arrives in
    one POST, we slice in-process, and the user sees one resilient job with
    smooth progress.

    `is_first` is accepted for backward compatibility with older callers but
    is no longer relevant — a single job owns the whole upload, so the
    ops_log row is always written fresh.
    """
    import time as _t
    t0 = _t.perf_counter()
    total = len(rows)
    try:
        _job_update(job_id, status="running", started_at=_dt.now().isoformat(),
                    progress=f"applying 0 / {total:,} rows")
        from app.services.pend_alc_service import log_operation_upsert

        agg_touched      = 0
        agg_hist_touched = 0
        agg_pend_updates: list = []
        agg_hist_updates: list = []
        agg_auto_closes:  list = []
        agg_overflow:     list = []  # D-2 fix: surface DO over-ship in async path

        with _engine().connect() as conn:
            # D-5 fix: log_operation_upsert per slice — apply_do_deductions /
            # update_bdc_history_with_do commit inside each slice, so if slice K
            # raises, slices 1..K-1 are already in the DB but had no ops_log
            # entry under the old code (one log_operation_upsert AFTER the
            # whole loop). That left the committed changes unrevertable. Now
            # slice 1 INSERTs the ops_log row and slices 2..N MERGE their
            # payload lists into it — same pattern as the sync /do-update.
            slices_total = (total + _DO_SLICE - 1) // _DO_SLICE if total else 0
            for slice_idx, i in enumerate(range(0, total, _DO_SLICE)):
                sl = rows[i:i + _DO_SLICE]
                # FS-12: same deduction args on every slice — SESSION_FIRST /
                # SESSION_ONLY target the same PEND_ALC session throughout.
                do_result   = apply_do_deductions(
                    conn, sl,
                    deduction_method=deduction_method,
                    target_session_id=target_session_id,
                )
                hist_result = update_bdc_history_with_do(conn, sl)
                agg_touched      += int(do_result.get("touched") or 0)
                agg_hist_touched += int(hist_result.get("touched") or 0)
                slice_pend_updates  = do_result.get("pend_updates") or []
                slice_hist_updates  = hist_result.get("history_updates") or []
                slice_auto_closes   = do_result.get("auto_history_closes") or []
                slice_overflow_rows = do_result.get("overflow_rows") or []
                agg_pend_updates.extend(slice_pend_updates)
                agg_hist_updates.extend(slice_hist_updates)
                agg_auto_closes.extend(slice_auto_closes)
                agg_overflow.extend(slice_overflow_rows)

                # Persist this slice's effect to ops_log BEFORE moving on, so a
                # crash on slice K+1 still leaves a revertable audit row for
                # slices 1..K. log_operation_upsert is best-effort: if the log
                # write itself fails we log CRITICAL and continue — the DB
                # changes already committed and skipping further slices on a
                # log-write hiccup would orphan more state.
                slice_qty = sum(float(r.get("do_qty") or 0) for r in sl)
                slice_cancel_count = sum(
                    1 for h in slice_hist_updates
                    if (h.get("new_status") or "").upper() == "CANCELLED"
                )
                try:
                    log_operation_upsert(
                        conn,
                        op_type="DO",
                        op_key=session_id,
                        payload={
                            "session_id":          session_id,
                            # FS-12 audit — scalar keys persist from slice 1
                            # (chunks 2..N only merge the list fields).
                            "deduction_method":    deduction_method,
                            "target_session_id":   target_session_id,
                            "pend_updates":        slice_pend_updates,
                            "history_updates":     slice_hist_updates,
                            "auto_history_closes": slice_auto_closes,
                            "overflow_rows":       slice_overflow_rows,
                        },
                        summary=(
                            f"DO upload {session_id}: applied "
                            f"{min(i + len(sl), total)}/{total} input lines"
                        ),
                        rows_affected=int(do_result.get("touched") or 0),
                        qty_total=slice_qty,
                        created_by=username,
                        is_first=(slice_idx == 0),
                        merge_payload_lists=["pend_updates", "history_updates",
                                             "auto_history_closes",
                                             "overflow_rows"],
                    )
                except Exception as log_err:
                    logger.critical(
                        f"[do-update-async] {session_id}: ops_log upsert "
                        f"FAILED on slice {slice_idx + 1}/{slices_total} — "
                        f"DB changes are committed but this slice is NOT in "
                        f"ops_log. err={log_err}"
                    )

                applied = min(i + len(sl), total)
                _job_update(
                    job_id,
                    progress=f"applied {applied:,} / {total:,} rows",
                )

            total_qty = sum(float(r.get("do_qty") or 0) for r in rows)
            # Cancellations (do_qty=0 input rows that flipped an OPEN BDC to
            # CANCELLED) surface in history_updates with new_status='CANCELLED'.
            # Count them separately so the user sees both numbers — DO
            # applications and cancellations are very different audit events.
            cancel_count = sum(
                1 for h in agg_hist_updates
                if (h.get("new_status") or "").upper() == "CANCELLED"
            )
            overflow_total = float(sum(o.get("overflow_qty") or 0 for o in agg_overflow))
            # Final summary refresh — no payload merge (slices already merged
            # their lists in). Just stamp the totals + final summary string.
            try:
                log_operation_upsert(
                    conn,
                    op_type="DO",
                    op_key=session_id,
                    payload={},  # ignored when merge_payload_lists is None and
                                 # is_first=False — only summary + totals refresh
                    summary=(
                        f"DO upload {session_id}: {total} input lines, "
                        f"{int(total_qty)} units, "
                        f"{agg_touched} pend_alc rows updated"
                        + (f", {cancel_count} BDC cancelled (DO_QTY=0)"
                           if cancel_count else "")
                        + (f", overflow={int(overflow_total)} units"
                           if overflow_total > 0 else "")
                    ),
                    # Don't double-count rows_affected / qty_total — the per-
                    # slice upserts already incremented them. Pass 0 deltas.
                    rows_affected=0,
                    qty_total=0,
                    created_by=username,
                    is_first=False,
                    merge_payload_lists=None,
                )
            except Exception as log_err:
                logger.warning(
                    f"[do-update-async] final summary refresh failed: {log_err}"
                )
        logger.info(
            f"[do-update-async] {session_id}: input={total} "
            f"touched={agg_touched} hist={agg_hist_touched} "
            f"cancelled={cancel_count} "
            f"auto-closed={len(agg_auto_closes)} "
            f"overflow_rows={len(agg_overflow)} ({overflow_total} units) "
            f"slices={slices_total} "
            f"total={_t.perf_counter()-t0:.2f}s"
        )
        _job_update(
            job_id,
            status="completed",
            progress=(
                f"done — {agg_touched:,} rows updated"
                + (f", {cancel_count:,} BDC cancelled" if cancel_count else "")
                + (f", {int(overflow_total):,} overflow" if overflow_total > 0 else "")
            ),
            finished_at=_dt.now().isoformat(),
            duration=round(_t.perf_counter() - t0, 2),
            result={
                "session_id":          session_id,
                "do_batch_id":         session_id,
                "updated_rows":        agg_touched,
                "bdc_history_updated": agg_hist_touched,
                "bdc_cancelled":       cancel_count,
                "input_lines":         total,
                "total_qty":           int(total_qty),
                "overflow_rows":       agg_overflow,
                "overflow_total_qty":  overflow_total,
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
    _validate_deduction_method(body)
    import uuid as _uu
    session_id = body.session_id or _uu.uuid4().hex[:12]
    rows = [
        {"rdc": r.rdc, "article_number": r.article_number,
         "do_qty": r.do_qty, "do_number": r.do_number,
         "st_cd": r.st_cd, "allocation_number": r.allocation_number,
         "alloc_type": r.alloc_type}
        for r in body.rows
    ]
    label = f"DO upload {session_id} ({len(rows)} lines)"
    job_id = _new_job("do", label=label)
    username = getattr(current_user, "username", None)
    _threading.Thread(
        target=_do_run_job,
        args=(job_id, rows, session_id, bool(body.is_first_chunk), username,
              body.deduction_method, body.target_session_id),
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
        j = dict(_jobs.get(job_id) or {})
    if not j:
        # FS-13 fix #1: dict miss ≠ job missing — under gunicorn -w N this
        # worker may simply not own the job. Fall back to ARS_ASYNC_JOBS.
        j = _job_db_read(job_id)
        if not j:
            raise HTTPException(404, f"Job {job_id} not found")
    out = {k: v for k, v in j.items() if k != "zip_path"}
    return {"success": True, "data": out}


@router.get("/async-jobs/{job_id}/download")
def pend_alc_async_job_download(job_id: str,
                                current_user: User = Depends(get_current_user)):
    """Stream the BDC ZIP (only valid for type='bdc' jobs once complete)."""
    with _jobs_lock:
        j = dict(_jobs.get(job_id) or {})
    if not j:
        # FS-13 fix #1: cross-worker download — the ZIP lives in the shared
        # temp dir, so any worker on this host can stream it from the
        # zip_path recorded in ARS_ASYNC_JOBS.
        j = _job_db_read(job_id)
    if not j:
        raise HTTPException(404, f"Job {job_id} not found")
    if j.get("status") != "completed":
        raise HTTPException(409, f"Job {job_id} status={j.get('status')}")
    zip_path = j.get("zip_path")
    res = j.get("result") or {}
    allocation_no = res.get("allocation_no") or "BDC"
    file_date     = res.get("file_date") or _dt.now().strftime("%Y-%m-%d")
    if not zip_path or not _os.path.exists(zip_path):
        raise HTTPException(
            410,
            "Download no longer available (server restart or cleanup). The "
            "BDC stamps and history WERE committed — if this generate should "
            "be undone, revert it from the Operations Log; the SAP file can "
            "be re-downloaded from the Open BDC Report.",
        )

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
        description="Filter by STATUS: OPEN / CLOSED_PARTIAL / CONFIRMED / CANCELLED "
                    "(legacy PARTIAL still readable for pre-cutover rows)"),
    limit:         int = Query(2000, ge=1, le=20000),
    current_user: User = Depends(get_current_user),
):
    """Audit trail of every BDC ever generated, with how much SAP confirmed.

    Each row = one (RDC, ST_CD, ARTICLE) line in one BDC file.
    BDC_QTY      = qty asked for in that BDC
    DO_RECEIVED  = qty SAP returned via DO
    STATUS:
      OPEN            — no DO yet, blocks the same (RDC, ST_CD, ART) from re-BDC
      CLOSED_PARTIAL  — DO arrived short of BDC; row is terminal, residual
                        ALLOC_QTY - DO_QTY is free for the next BDC
      CONFIRMED       — DO fully covered BDC; terminal
      CANCELLED       — adhoc close via /pend-alc/close-rows; terminal
    (Legacy STATUS='PARTIAL' rows from before this rollout remain readable.)

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
# GET /pend-alc/bdc-history-allocations
# Summary of distinct allocations in ARS_BDC_HISTORY for the Open BDC
# report's "Re-download" strip. No row cap (it's a GROUP BY, returns one
# row per allocation) so the strip never goes stale when the detail table
# is capped at N rows.
# ---------------------------------------------------------------------------
@router.get("/bdc-history-allocations")
def pend_alc_bdc_history_allocations(
    status:    Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    rdc:       Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
):
    """One row per distinct ALLOCATION_NUMBER matching the filters. Returns
    line count, total BDC qty, total DO received, status mix, and date
    range. Used to populate the per-allocation re-download chips."""
    try:
        filters = ["1=1"]
        params: dict = {}
        if status:
            filters.append("STATUS = :stat"); params["stat"] = status.upper()
        if date_from:
            filters.append("BDC_DATE >= :df"); params["df"] = date_from
        if date_to:
            filters.append("BDC_DATE < DATEADD(day, 1, :dt)"); params["dt"] = date_to
        if rdc:
            filters.append("RDC = :rdc"); params["rdc"] = rdc
        where = "WHERE " + " AND ".join(filters)

        with _engine().connect() as conn:
            ensure_bdc_history_table(conn)
            rows = conn.execute(text(f"""
                SELECT ALLOCATION_NUMBER,
                       COUNT(*)                                AS lines,
                       SUM(BDC_QTY)                            AS bdc_qty,
                       SUM(DO_RECEIVED)                        AS do_qty,
                       SUM(BDC_QTY - DO_RECEIVED)              AS short_qty,
                       MIN(BDC_DATE)                           AS first_dt,
                       MAX(BDC_DATE)                           AS last_dt,
                       MIN(STATUS)                             AS min_status,
                       MAX(STATUS)                             AS max_status,
                       COUNT(DISTINCT STATUS)                  AS status_variants
                FROM {BDC_HISTORY_TABLE} WITH (NOLOCK)
                {where}
                GROUP BY ALLOCATION_NUMBER
                ORDER BY MAX(BDC_DATE) DESC, ALLOCATION_NUMBER
            """), params).fetchall()

        return {
            "success": True,
            "count":   len(rows),
            "data": [
                {
                    "allocation_number": r[0],
                    "lines":             int(r[1] or 0),
                    "bdc_qty":           float(r[2] or 0),
                    "do_qty":            float(r[3] or 0),
                    "short_qty":         float(r[4] or 0),
                    "first_date":        r[5].isoformat() if r[5] else None,
                    "last_date":         r[6].isoformat() if r[6] else None,
                    # 'MIXED' if multiple statuses share this allocation (rare —
                    # CONFIRMED + CLOSED_PARTIAL of the same alloc-no).
                    "status":            (r[7] if r[9] == 1 else "MIXED"),
                }
                for r in rows
            ],
        }
    except Exception as e:
        logger.exception(f"[pend_alc] bdc-history-allocations failed: {e}")
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# GET /pend-alc/bdc-history-redownload  — re-download an old BDC's SAP file
#                                          from the BDC_HISTORY records.
# ---------------------------------------------------------------------------
@router.get("/bdc-history-redownload")
def pend_alc_bdc_history_redownload(
    allocation_number: str = Query(..., description="e.g. 2627-001 or 2627-DH24-001"),
    current_user: User = Depends(get_current_user),
):
    """Re-download a previously generated BDC in the same SAP-ready 9-column
    Excel format as /bdc-generate. Reads every ARS_BDC_HISTORY row tagged
    with `allocation_number` and rebuilds the file using BDC_DATE for both
    Allocation Date and Picking Date columns.

    Use when the original download was lost or the user needs a fresh copy
    of an already-generated BDC without re-stamping anything.
    """
    try:
        with _engine().connect() as conn:
            rows = conn.execute(text(f"""
                SELECT BDC_DATE, ALLOCATION_NUMBER,
                       RDC, ISNULL(ST_CD,'') AS ST_CD,
                       ARTICLE_NUMBER, BDC_QTY
                FROM {BDC_HISTORY_TABLE} WITH (NOLOCK)
                WHERE ALLOCATION_NUMBER = :a
                ORDER BY RDC, ST_CD, ARTICLE_NUMBER, ID
            """), {"a": allocation_number}).fetchall()

        if not rows:
            raise HTTPException(404,
                f"No BDC history found for allocation_number={allocation_number}")

        # Pull the BDC date once — they all share it. Format for both
        # Allocation Date and Picking Date columns the same way as the
        # original generator (YYYY-MM-DD).
        bdc_date = rows[0][0]
        date_str = bdc_date.strftime("%Y-%m-%d") if bdc_date else \
                   datetime.date.today().strftime("%Y-%m-%d")

        import io as _io
        try:
            import xlsxwriter  # noqa: F401  — preferred fast writer
            buf = _io.BytesIO()
            import xlsxwriter
            wb = xlsxwriter.Workbook(buf, {"in_memory": True})
            ws = wb.add_worksheet("BDC")
            headers = ["Serial No", "Allocation Date", "Allocation Number",
                       "VENDOR", "MATERIAL NO", "BDC-QTY", "RECEIVING STORE",
                       "Picking Date", "Remark"]
            for col, h in enumerate(headers):
                ws.write(0, col, h)
            for i, r in enumerate(rows, start=1):
                ws.write(i, 0, i)
                ws.write(i, 1, date_str)
                ws.write(i, 2, r[1])
                ws.write(i, 3, str(r[2] or "").strip())
                ws.write(i, 4, str(r[4] or "").strip().lstrip("0"))
                ws.write(i, 5, int(r[5] or 0))
                ws.write(i, 6, str(r[3] or "").strip())
                ws.write(i, 7, date_str)
                ws.write(i, 8, "")
            wb.close()
            buf.seek(0)
        except ImportError:
            # Fallback via pandas if xlsxwriter not available.
            df = pd.DataFrame([{
                "Serial No":         i,
                "Allocation Date":   date_str,
                "Allocation Number": r[1],
                "VENDOR":            str(r[2] or "").strip(),
                "MATERIAL NO":       str(r[4] or "").strip().lstrip("0"),
                "BDC-QTY":           int(r[5] or 0),
                "RECEIVING STORE":   str(r[3] or "").strip(),
                "Picking Date":      date_str,
                "Remark":            "",
            } for i, r in enumerate(rows, start=1)])
            buf = _io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="BDC")
            buf.seek(0)

        fname = (f"ARS_BDC_{date_str.replace('-','')}_"
                 f"{allocation_number}_redownload.xlsx")
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[pend_alc] bdc-history-redownload failed: {e}")
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# GET /pend-alc/bdc-history-export  — Excel of BDC_HISTORY rows with filters.
# Powers the Open BDC Report page (defaults to STATUS=OPEN — what's in
# flight to SAP and awaiting DO).
# ---------------------------------------------------------------------------
@router.get("/bdc-history-export")
def pend_alc_bdc_history_export(
    status:        Optional[str] = Query(None, description="OPEN / CLOSED_PARTIAL / CONFIRMED / CANCELLED"),
    allocation_no: Optional[str] = Query(None),
    rdc:           Optional[str] = Query(None),
    st_cd:         Optional[str] = Query(None),
    article:       Optional[str] = Query(None),
    date_from:     Optional[str] = Query(None, description="BDC_DATE >= ..."),
    date_to:       Optional[str] = Query(None, description="BDC_DATE <= ..."),
    current_user: User = Depends(get_current_user),
):
    """Export ARS_BDC_HISTORY rows (with current DO_RECEIVED and SHORT_QTY)
    as an Excel file. Default Open BDC Report view: status=OPEN.

    Excel columns:
      ID, BDC_DATE, ALLOCATION_NUMBER, RDC, ST_CD, ARTICLE_NUMBER, MAJ_CAT,
      BDC_QTY, DO_RECEIVED, SHORT_QTY, STATUS, LAST_DO_AT, CREATED_BY,
      DAYS_OPEN
    """
    try:
        filters = ["1=1"]
        params: dict = {}
        if status:
            filters.append("STATUS = :stat"); params["stat"] = status.upper()
        if allocation_no:
            filters.append("ALLOCATION_NUMBER = :a"); params["a"] = allocation_no
        if rdc:
            filters.append("RDC = :rdc"); params["rdc"] = rdc
        if st_cd:
            filters.append("ISNULL(ST_CD,'') = :st"); params["st"] = st_cd
        if article:
            filters.append("ARTICLE_NUMBER = :art"); params["art"] = article
        if date_from:
            filters.append("BDC_DATE >= :df"); params["df"] = date_from
        if date_to:
            filters.append("BDC_DATE < DATEADD(day, 1, :dt)"); params["dt"] = date_to
        where = "WHERE " + " AND ".join(filters)

        with _engine().connect() as conn:
            ensure_bdc_history_table(conn)
            rows = conn.execute(text(f"""
                SELECT ID, BDC_DATE, ALLOCATION_NUMBER,
                       RDC, ISNULL(ST_CD,'') AS ST_CD, ARTICLE_NUMBER, MAJ_CAT,
                       BDC_QTY, DO_RECEIVED,
                       (BDC_QTY - DO_RECEIVED) AS SHORT_QTY,
                       STATUS, LAST_DO_AT, CREATED_BY,
                       DATEDIFF(day, BDC_DATE, GETDATE()) AS DAYS_OPEN
                FROM {BDC_HISTORY_TABLE} WITH (NOLOCK)
                {where}
                ORDER BY BDC_DATE DESC, ID DESC
            """), params).fetchall()

        import csv as _csv
        buf = io.StringIO()
        w = _csv.writer(buf, lineterminator="\n")
        w.writerow([
            "ID", "BDC_DATE", "ALLOCATION_NUMBER", "RDC", "ST_CD",
            "ARTICLE_NUMBER", "MAJ_CAT", "BDC_QTY", "DO_RECEIVED",
            "SHORT_QTY", "STATUS", "LAST_DO_AT", "CREATED_BY", "DAYS_OPEN",
        ])
        for r in rows:
            w.writerow([
                int(r[0]),
                r[1].strftime("%Y-%m-%d") if r[1] else "",
                r[2], r[3], r[4], r[5], r[6] or "",
                float(r[7] or 0), float(r[8] or 0), float(r[9] or 0),
                r[10] or "",
                r[11].strftime("%Y-%m-%d %H:%M") if r[11] else "",
                r[12] or "",
                int(r[13] or 0),
            ])

        tag = (status or "ALL").upper()
        fname = (f"BDC_HISTORY_{tag}_"
                 f"{datetime.date.today().strftime('%Y%m%d')}.csv")
        body = "﻿" + buf.getvalue()
        return StreamingResponse(
            iter([body]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except Exception as e:
        logger.exception(f"[pend_alc] bdc-history-export failed: {e}")
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


@router.post("/operations/recover-bdc-history")
def pend_alc_operations_recover_bdc_history(
    confirm: bool = Query(False, description="false = preview, true = apply"),
    current_user: User = Depends(get_current_user),
):
    """One-shot: rebuild ARS_BDC_HISTORY rows for active BDC ops whose
    history was wiped by a colliding-allocation-number revert (legacy bug
    fixed by the prev-MAX(ID) readback in insert_bdc_history).

    Reads PEND_ALC.BDC_QTY for each (rdc, st_cd, article) in the op's
    stamped_rows and writes one BDC_HISTORY row per combo with that qty
    as the residual. Patches the op's payload history_ids so future
    reverts undo the correct rows. Restores _NO_OPEN_BDC_PREDICATE so the
    next /bdc-generate stops re-stamping in-flight units.

    Run with confirm=false first to see how many ops need rebuilding,
    then confirm=true to apply. Idempotent — only touches ops whose
    history_ids no longer exist in ARS_BDC_HISTORY.
    """
    try:
        with _engine().connect() as conn:
            res = recover_bdc_history_from_active_ops(conn, dry_run=not confirm)
        logger.info(
            f"[pend_alc] recover-bdc-history by "
            f"{getattr(current_user,'username','?')}: {res}"
        )
        return {"success": True, **res}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/operations/close-orphan-bdc-history")
def pend_alc_operations_close_orphan_bdc_history(
    confirm: bool = Query(False, description="false = preview, true = apply"),
    current_user: User = Depends(get_current_user),
):
    """One-shot: close OPEN BDC_HISTORY rows whose underlying PEND_ALC
    state says the units already shipped (or the row was reverted).

    Symptom this fixes: "Pending DO (Open BDC)" tile shows far more
    units/rows than open PEND_ALC really has — because earlier DO uploads
    closed PEND_ALC.IS_CLOSED but never updated BDC_HISTORY.STATUS.

    Buckets (CONFIRMED / CLOSED_PARTIAL / CANCELLED) chosen per
    (RDC, ST_CD, ARTICLE) by comparing live PEND_ALC sums to the OPEN
    history. Run with confirm=false first to preview; confirm=true to
    apply. The apply step logs an ADHOC_CLOSE ops_log entry for audit.
    """
    try:
        with _engine().connect() as conn:
            res = close_orphan_open_bdc_history(conn, dry_run=not confirm)
        logger.info(
            f"[pend_alc] close-orphan-bdc-history by "
            f"{getattr(current_user,'username','?')}: {res}"
        )
        return {"success": True, **res}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/operations/backfill-approve-qty")
def pend_alc_operations_backfill_approve_qty(
    confirm: bool = Query(False, description="false = preview, true = apply"),
    current_user: User = Depends(get_current_user),
):
    """One-shot: rewrite QTY_TOTAL + SUMMARY on legacy APPROVE ops that
    were logged with qty_total=0 (the field was hardcoded before the fix).
    Reads SUM(ALLOC_QTY) from ARS_PEND_ALC by SESSION_ID for each affected op.

    Run with confirm=false first to see the count, then confirm=true to apply.
    Idempotent — re-running only updates rows still at QTY_TOTAL=0.
    """
    try:
        with _engine().connect() as conn:
            res = backfill_approve_qty(conn, dry_run=not confirm)
        logger.info(
            f"[pend_alc] backfill-approve-qty by "
            f"{getattr(current_user,'username','?')}: {res}"
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
    # Typed pend (Package 3): FRESH/GRT stored on the row and deducted from
    # the matching typed MSA row; None/'' stored as NULL (legacy → folds
    # into FRESH in the MSA delta).
    alloc_type:     Optional[str] = None

    @field_validator("alloc_type", mode="before")
    @classmethod
    def _v_alloc_type(cls, v):
        return _norm_alloc_type_field(v)


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
    # Applied to rows that don't carry their own alloc_type. None keeps
    # legacy behaviour (rows insert with ALLOC_TYPE NULL → fold FRESH).
    default_alloc_type: Optional[Literal["FRESH", "GRT"]] = None


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
        # Request-level default type fills rows that didn't carry their own.
        if body.default_alloc_type:
            for r in rows:
                if not r.get("alloc_type"):
                    r["alloc_type"] = body.default_alloc_type
        article_rdc_pairs = [
            {"rdc": r["rdc"], "article_number": r["article_number"]}
            for r in rows
        ]
        from app.services.pend_alc_service import log_operation_upsert
        with _engine().connect() as conn:
            # M-2 / M-3 fix: write_manual_pend_alc now applies the MSA+grid
            # delta per-chunk, scoped to the rows it just inserted. The old
            # "deferred delta on is_last_chunk" path here is removed — it
            # mis-fired in two ways: (a) if the client crashed before sending
            # is_last_chunk=True, MSA stayed stale; (b) if the client sent
            # is_last_chunk=True twice (retry), the delta was applied over the
            # whole session each time, double-counting. is_first_chunk /
            # is_last_chunk are still accepted for backward compatibility but
            # no longer gate any MSA write.
            res = write_manual_pend_alc(conn, rows, session_id=body.session_id)
            msa_adjusted = res.get("msa_adjusted")

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
            f"delta_applied={msa_adjusted is not None and not (msa_adjusted or {}).get('error')}"
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
# POST /pend-alc/close-rows  — adhoc cancel
# ---------------------------------------------------------------------------
class CloseRow(BaseModel):
    rdc:            str
    article_number: str
    st_cd:          Optional[str] = None
    reason:         Optional[str] = None


class CloseRowsRequest(BaseModel):
    rows:   List[CloseRow]
    reason: Optional[str] = None  # global reason if not set per-row
    # A-1 fix: blank ST_CD in the input means "close every store for this
    # (RDC, ARTICLE)" — silently nukes whole articles if a user uploads a
    # spreadsheet with the ST_CD column accidentally empty. Require explicit
    # confirmation; otherwise the API rejects with 400 + the offending keys.
    confirm_close_all_stores: Optional[bool] = False


@router.post("/close-rows")
def pend_alc_close_rows(
    body: CloseRowsRequest,
    current_user: User = Depends(get_current_user),
):
    """Adhoc-close PEND_ALC rows + cancel their open BDC history.

    Use when a BDC line was generated but should not ship — typical case
    is the bot issuing a BDC for an article that has no MSA stock. Closing
    here:
      - flips IS_CLOSED=1 on every open PEND_ALC row matching the key
        (REMARKS gets `ADHOC: <reason>` prefix so it shows in reco)
      - flips STATUS='CANCELLED' on every still-OPEN BDC_HISTORY row at
        the same key, releasing the in-flight predicate so a corrected
        re-BDC can flow.
    Operation is logged to ARS_PEND_ALC_OPERATIONS (OP_TYPE='ADHOC_CLOSE')
    so it's revertable from the same UI as BDC / DO reverts.
    """
    if not body.rows:
        raise HTTPException(400, "No rows provided")
    rows = []
    for r in body.rows:
        per_row_reason = (r.reason or body.reason or "").strip()
        rows.append({
            "rdc":            r.rdc,
            "st_cd":          r.st_cd,
            "article_number": r.article_number,
            "reason":         per_row_reason,
        })

    # A-1 fix: refuse blank-ST_CD wildcard unless caller confirms. A blank
    # ST_CD on the service side closes EVERY store for the (RDC, ARTICLE),
    # which is the right behavior for a deliberate sweep but a footgun for
    # a user who left the column empty by accident.
    wildcard_rows = [
        {"rdc": r["rdc"], "article_number": r["article_number"]}
        for r in rows
        if not (r.get("st_cd") or "").strip()
    ]
    if wildcard_rows and not bool(body.confirm_close_all_stores):
        raise HTTPException(
            status_code=400,
            detail={
                "detail": (
                    "Some rows have blank ST_CD (would close ALL stores for "
                    "the RDC+ARTICLE). Set confirm_close_all_stores=true if "
                    "intentional."
                ),
                "wildcard_rows":  wildcard_rows[:50],  # cap for response size
                "wildcard_count": len(wildcard_rows),
            },
        )
    if wildcard_rows:
        logger.warning(
            f"Adhoc close: explicit wildcard for {len(wildcard_rows)} "
            f"(rdc, article) keys by reason={(body.reason or '')!r}"
        )

    # One log entry per call; use the global reason if every per-row reason
    # is blank, else first-non-blank as the audit summary.
    summary_reason = (
        (body.reason or "").strip()
        or next((r["reason"] for r in rows if r["reason"]), "")
    )
    username = getattr(current_user, "username", None)
    op_key = _uuid.uuid4().hex[:12]
    try:
        with _engine().connect() as conn:
            res = apply_adhoc_close(
                conn, rows, reason=summary_reason, created_by=username,
            )
            log_operation(
                conn,
                op_type="ADHOC_CLOSE",
                op_key=op_key,
                payload={
                    "reason":          summary_reason,
                    "input_keys":      rows,
                    "pend_updates":    res["pend_updates"],
                    "history_updates": res["history_updates"],
                },
                summary=(
                    f"Adhoc close {op_key}: {len(rows)} key(s), "
                    f"{res['touched_pend']} pend rows closed, "
                    f"{res['touched_history']} BDC history rows cancelled"
                    + (f" — {summary_reason}" if summary_reason else "")
                ),
                rows_affected=res["touched_pend"],
                qty_total=0,
                created_by=username,
            )
        return {
            "success":            True,
            "op_key":              op_key,
            "input_keys":          len(rows),
            "pend_rows_closed":    res["touched_pend"],
            "history_rows_cancelled": res["touched_history"],
        }
    except Exception as e:
        logger.exception(f"[pend_alc] close-rows failed: {e}")
        raise HTTPException(500, str(e))


@router.post("/close-rows-file")
async def pend_alc_close_rows_file(
    file: UploadFile = File(..., description="CSV/Excel with RDC, ARTICLE_NUMBER, [ST_CD], [REASON]"),
    reason: Optional[str] = None,
    confirm_close_all_stores: bool = False,
    current_user: User = Depends(get_current_user),
):
    """Bulk adhoc-close from a CSV / Excel file.

    Required columns:  RDC, ARTICLE_NUMBER
    Optional columns:  ST_CD, REASON
    Top-level `reason` query param applies to rows that don't carry their own.

    A-1 guard: rows with blank ST_CD close EVERY store for (RDC, ARTICLE).
    Set `confirm_close_all_stores=true` to opt-in; otherwise the request is
    rejected with 400.
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
        # Empty cells in mixed columns come back as NaN (a truthy float, str(NaN)=='nan')
        # which would silently poison the SQL match. Force every cell to a real string.
        df = df.fillna("")
        # Normalise column names (uppercase, strip)
        df.columns = [str(c).strip().upper() for c in df.columns]
        if "RDC" not in df.columns or "ARTICLE_NUMBER" not in df.columns:
            raise HTTPException(400, "Missing required columns: RDC, ARTICLE_NUMBER")
        global_reason = (reason or "").strip()
        rows = []
        for _, row in df.iterrows():
            rdc = str(row.get("RDC") or "").strip()
            art = str(row.get("ARTICLE_NUMBER") or "").strip().split(".")[0]
            if not rdc or not art:
                continue
            st = str(row.get("ST_CD") or "").strip() if "ST_CD" in df.columns else ""
            per_row_reason = str(row.get("REASON") or "").strip() if "REASON" in df.columns else ""
            rows.append({
                "rdc":            rdc,
                "st_cd":          st or None,
                "article_number": art,
                "reason":         per_row_reason or global_reason,
            })
        if not rows:
            raise HTTPException(400, "No valid rows after parsing")

        # A-1 fix: same blank-ST_CD wildcard guard as the JSON endpoint.
        wildcard_rows = [
            {"rdc": r["rdc"], "article_number": r["article_number"]}
            for r in rows
            if not (r.get("st_cd") or "").strip()
        ]
        if wildcard_rows and not confirm_close_all_stores:
            raise HTTPException(
                status_code=400,
                detail={
                    "detail": (
                        "Some rows have blank ST_CD (would close ALL stores "
                        "for the RDC+ARTICLE). Set confirm_close_all_stores="
                        "true if intentional."
                    ),
                    "wildcard_rows":  wildcard_rows[:50],
                    "wildcard_count": len(wildcard_rows),
                },
            )
        if wildcard_rows:
            logger.warning(
                f"Adhoc close (file {file.filename}): explicit wildcard for "
                f"{len(wildcard_rows)} (rdc, article) keys by "
                f"reason={(reason or '')!r}"
            )

        summary_reason = global_reason or next((r["reason"] for r in rows if r["reason"]), "")
        username = getattr(current_user, "username", None)
        op_key = _uuid.uuid4().hex[:12]
        with _engine().connect() as conn:
            res = apply_adhoc_close(conn, rows, reason=summary_reason, created_by=username)
            log_operation(
                conn,
                op_type="ADHOC_CLOSE",
                op_key=op_key,
                payload={
                    "reason":          summary_reason,
                    "input_keys":      rows,
                    "pend_updates":    res["pend_updates"],
                    "history_updates": res["history_updates"],
                },
                summary=(
                    f"Adhoc close {op_key} (file {file.filename}): "
                    f"{len(rows)} key(s), "
                    f"{res['touched_pend']} pend rows closed, "
                    f"{res['touched_history']} BDC history rows cancelled"
                    + (f" — {summary_reason}" if summary_reason else "")
                ),
                rows_affected=res["touched_pend"],
                qty_total=0,
                created_by=username,
            )
        return {
            "success":                  True,
            "op_key":                   op_key,
            "input_keys":               len(rows),
            "pend_rows_closed":         res["touched_pend"],
            "history_rows_cancelled":   res["touched_history"],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[pend_alc] close-rows-file failed: {e}")
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
    "alloc_type":      "P.ALLOC_TYPE",
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
    alloc_type:  Optional[str]  = Query(
        None, description="FRESH | GRT | LEGACY (NULL/'' rows)"),
    closed:      Optional[bool] = Query(None),
    session_id:  Optional[str]  = Query(None),
    # Pagination
    page:        int            = Query(1,   ge=1),
    page_size:   int            = Query(100, ge=1, le=10000),
    # Sort
    sort_by:     Optional[str]  = Query(None,
        description="Column key from _RECO_SORTABLE (e.g. 'pend_qty')"),
    sort_dir:    str            = Query("desc", pattern="^(asc|desc)$"),
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
        at_pred = _alloc_type_pred(alloc_type, params)
        if at_pred:
            filters.append(at_pred)

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
                    B.BDC_DATE          AS BDC_DATE,
                    P.ALLOC_TYPE        AS ALLOC_TYPE
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
                    "alloc_type":      r[27] or None,
                }
                for r in rows
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# GET /pend-alc/reco-export — Excel of reco rows. Accepts the same filter
# params as /reco so the Reconciliation page tiles can hand off their scope
# straight to an export call.
# ---------------------------------------------------------------------------
@router.get("/reco-export")
def pend_alc_reco_export(
    date_from:    Optional[str]  = Query(None),
    date_to:      Optional[str]  = Query(None),
    rdc:          Optional[str]  = Query(None),
    maj_cat:      Optional[str]  = Query(None),
    alloc_mode:   Optional[str]  = Query(None),
    source:       Optional[str]  = Query(None),
    alloc_type:   Optional[str]  = Query(
        None, description="FRESH | GRT | LEGACY (NULL/'' rows)"),
    closed:       Optional[bool] = Query(None),
    session_id:   Optional[str]  = Query(None),
    f_rdc:        Optional[str]  = Query(None),
    f_st_cd:      Optional[str]  = Query(None),
    f_maj_cat:    Optional[str]  = Query(None),
    f_alloc_mode: Optional[str]  = Query(None),
    f_source:     Optional[str]  = Query(None),
    f_bdc_status: Optional[str]  = Query(None),
    f_aging_band: Optional[str]  = Query(None),
    q_article:    Optional[str]  = Query(None),
    limit:        int            = Query(200000, ge=1, le=1_000_000),
    current_user: User = Depends(get_current_user),
):
    """Export the same rows /reco would return (no pagination — single sheet).

    Filter params are byte-identical to /reco's, so the Reco page tiles can
    hand off their scope directly. Default cap is 200k rows; bump via
    `limit` if you really need more — rendering past that hurts Excel.
    """
    try:
        filters = ["1=1"]
        params: dict = {"lim": limit}
        if date_from:  filters.append("P.APPROVED_AT >= :df");                params["df"]  = date_from
        if date_to:    filters.append("P.APPROVED_AT < DATEADD(day,1,:dt)");  params["dt"]  = date_to
        if rdc:        filters.append("P.RDC = :rdc");                        params["rdc"] = rdc
        if maj_cat:    filters.append("P.MAJ_CAT = :mc");                     params["mc"]  = maj_cat
        if alloc_mode: filters.append("P.ALLOC_MODE = :am");                  params["am"]  = alloc_mode
        if source:     filters.append("P.SOURCE = :src");                     params["src"] = source
        if closed is not None:
            filters.append("P.IS_CLOSED = :cl"); params["cl"] = 1 if closed else 0
        if session_id:
            filters.append("P.SESSION_ID = :sid"); params["sid"] = session_id
        at_pred = _alloc_type_pred(alloc_type, params)
        if at_pred:
            filters.append(at_pred)

        def _multi(col: str, csv: Optional[str], prefix: str):
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

        aging_vals = _parse_csv_filter(f_aging_band)
        if aging_vals:
            cases = []
            for i, v in enumerate(aging_vals):
                cases.append(f":fage{i}"); params[f"fage{i}"] = v
            filters.append(
                f"(CASE "
                f"  WHEN DATEDIFF(day,P.APPROVED_AT,GETDATE())<=7 THEN '0-7d' "
                f"  WHEN DATEDIFF(day,P.APPROVED_AT,GETDATE())<=30 THEN '8-30d' "
                f"  WHEN DATEDIFF(day,P.APPROVED_AT,GETDATE())<=60 THEN '31-60d' "
                f"  ELSE '60d+' END) IN ({','.join(cases)})"
            )

        bdc_status_vals = _parse_csv_filter(f_bdc_status)
        if bdc_status_vals:
            real = [v for v in bdc_status_vals if v != "NEVER_SENT"]
            include_never = "NEVER_SENT" in bdc_status_vals
            parts = []
            if real:
                phs = ",".join(f":fbst{i}" for i in range(len(real)))
                parts.append(f"B.STATUS IN ({phs})")
                for i, v in enumerate(real):
                    params[f"fbst{i}"] = v
            if include_never:
                parts.append("B.STATUS IS NULL")
            filters.append("(" + " OR ".join(parts) + ")")

        where = "WHERE " + " AND ".join(filters)

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

        with _engine().connect() as conn:
            ensure_pend_alc_table(conn)
            rows = conn.execute(text(f"""
                SELECT TOP (:lim)
                    P.RDC, ISNULL(P.ST_CD,'') AS ST_CD, P.ARTICLE_NUMBER,
                    P.MAJ_CAT, P.GEN_ART_NUMBER, P.CLR,
                    P.ALLOC_MODE, P.SOURCE,
                    P.ALLOC_QTY, P.BDC_QTY, P.DO_QTY, P.PEND_QTY,
                    CASE WHEN P.BDC_QTY - P.DO_QTY > 0 THEN P.BDC_QTY - P.DO_QTY ELSE 0 END
                        AS BDC_UNCONFIRMED,
                    P.APPROVED_AT, P.LAST_BDC_AT, P.DO_NUMBER, P.DO_UPLOADED_AT,
                    P.IS_CLOSED, P.REMARKS,
                    DATEDIFF(day, P.APPROVED_AT, GETDATE()) AS AGING_DAYS,
                    B.ALLOCATION_NUMBER AS BDC_ALLOC_NO,
                    ISNULL(B.STATUS, 'NEVER_SENT') AS BDC_STATUS,
                    B.DO_RECEIVED AS BDC_DO_RECVD,
                    B.BDC_DATE,
                    P.ALLOC_TYPE
                FROM {PEND_ALC_TABLE} P
                {bdc_join}
                {where}
                ORDER BY P.APPROVED_AT DESC, P.ID
            """), params).fetchall()

        # CSV instead of Excel: opens reliably in any tool, no openpyxl
        # dependency, and large exports (100k+ rows) stream much faster.
        # Excel was tripping the user with "file can't be opened" warnings
        # on some Windows installs — CSV side-steps that entirely.
        import csv as _csv
        buf = io.StringIO()
        w = _csv.writer(buf, lineterminator="\n")
        w.writerow([
            "RDC", "ST_CD", "ARTICLE_NUMBER", "MAJ_CAT", "GEN_ART_NUMBER", "CLR",
            "ALLOC_MODE", "SOURCE",
            "ALLOC_QTY", "BDC_QTY", "DO_QTY", "PEND_QTY", "BDC_UNCONFIRMED",
            "APPROVED_AT", "LAST_BDC_AT", "DO_NUMBER", "DO_UPLOADED_AT",
            "IS_CLOSED", "REMARKS", "AGING_DAYS",
            "BDC_ALLOC_NO", "BDC_STATUS", "BDC_DO_RECEIVED", "BDC_DATE",
            "ALLOC_TYPE",
        ])
        for r in rows:
            w.writerow([
                r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                float(r[8] or 0), float(r[9] or 0), float(r[10] or 0),
                float(r[11] or 0), float(r[12] or 0),
                r[13].strftime("%Y-%m-%d %H:%M") if r[13] else "",
                r[14].strftime("%Y-%m-%d %H:%M") if r[14] else "",
                r[15] or "",
                r[16].strftime("%Y-%m-%d %H:%M") if r[16] else "",
                int(r[17] or 0),
                (r[18] or "").replace("\n", " ").replace("\r", " "),
                int(r[19] or 0),
                r[20] or "",
                r[21] or "",
                float(r[22] or 0) if r[22] is not None else "",
                r[23].strftime("%Y-%m-%d") if r[23] else "",
                r[24] or "",
            ])

        # File-name tag captures the dominant filter so the user can tell
        # tile exports apart in their Downloads folder.
        tag_bits = []
        if f_bdc_status: tag_bits.append(f_bdc_status.replace(',', '_'))
        if f_aging_band: tag_bits.append(f_aging_band.replace(',', '_'))
        if closed is True:  tag_bits.append("CLOSED")
        elif closed is False: tag_bits.append("OPEN")
        tag = "_".join(tag_bits) or "ALL"
        fname = f"PEND_ALC_RECO_{tag}_{datetime.date.today().strftime('%Y%m%d')}.csv"
        # UTF-8 BOM so Excel auto-detects the encoding when opening the CSV
        # — without it special chars (₹, accented names) render as mojibake.
        body = "﻿" + buf.getvalue()
        return StreamingResponse(
            iter([body]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[pend_alc] reco-export failed: {e}")
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# GET /pend-alc/reco-summary
# ---------------------------------------------------------------------------
@router.get("/reco-summary")
def pend_alc_reco_summary(
    alloc_type: Optional[str] = Query(
        None, description="FRESH | GRT | LEGACY (NULL/'' rows)"),
    current_user: User = Depends(get_current_user),
):
    """Aggregated reco tiles: by mode, source, aging band, RDC and type."""
    # Computed before the try so a bad filter surfaces as 400, not 500.
    at_params: dict = {}
    at_pred = _alloc_type_pred(alloc_type, at_params)
    p_and   = f" AND {at_pred}" if at_pred else ""             # P-aliased
    b_and   = (f" AND {at_pred.replace('P.ALLOC_TYPE', 'ALLOC_TYPE')}"
               if at_pred else "")                             # bare-table
    try:
        with _engine().connect() as conn:
            ensure_pend_alc_table(conn)

            by_mode = conn.execute(text(f"""
                ;WITH open_keys AS (
                    SELECT DISTINCT h.RDC,
                           ISNULL(h.ST_CD,'') AS ST_CD,
                           h.ARTICLE_NUMBER
                    FROM {BDC_HISTORY_TABLE} h WITH (NOLOCK)
                    WHERE h.STATUS = 'OPEN'
                )
                SELECT ISNULL(P.ALLOC_MODE,'AUTO') AS mode,
                       SUM(P.ALLOC_QTY) AS alloc_qty,
                       SUM(P.BDC_QTY)  AS bdc_qty,
                       SUM(P.DO_QTY)   AS do_qty,
                       SUM(P.PEND_QTY) AS pend_qty,
                       -- Pending BDC creation: qty awaiting the next
                       -- /bdc-generate (no STATUS='OPEN' history row for
                       -- this (RDC, ST_CD, ARTICLE)).
                       --
                       -- Implementation note: we LEFT JOIN against a CTE
                       -- of "keys with an OPEN BDC" rather than using
                       -- NOT EXISTS inside SUM(CASE ...). The latter
                       -- trips SQL Server error 130 ("Cannot perform an
                       -- aggregate function on an expression containing
                       -- ... a subquery") on certain compat levels,
                       -- which crashed /reco-summary on production.
                       SUM(CASE
                           WHEN P.PEND_QTY > 0 AND ok.RDC IS NULL
                           THEN P.PEND_QTY ELSE 0
                       END) AS pending_bdc_qty,
                       COUNT(*)      AS rows
                FROM {PEND_ALC_TABLE} P WITH (NOLOCK)
                LEFT JOIN open_keys ok
                  ON  ok.RDC = P.RDC
                 AND ok.ST_CD = ISNULL(P.ST_CD,'')
                 AND ok.ARTICLE_NUMBER = P.ARTICLE_NUMBER
                WHERE P.IS_CLOSED = 0{p_and}
                GROUP BY ISNULL(P.ALLOC_MODE,'AUTO')
            """), at_params).fetchall()

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
                WHERE IS_CLOSED = 0{b_and}
                GROUP BY
                    CASE
                        WHEN DATEDIFF(day, APPROVED_AT, GETDATE()) <= 7   THEN '0-7d'
                        WHEN DATEDIFF(day, APPROVED_AT, GETDATE()) <= 30  THEN '8-30d'
                        WHEN DATEDIFF(day, APPROVED_AT, GETDATE()) <= 60  THEN '31-60d'
                        ELSE '60d+'
                    END
                ORDER BY MIN(DATEDIFF(day, APPROVED_AT, GETDATE()))
            """), at_params).fetchall()

            by_rdc = conn.execute(text(f"""
                ;WITH open_keys AS (
                    SELECT DISTINCT h.RDC,
                           ISNULL(h.ST_CD,'') AS ST_CD,
                           h.ARTICLE_NUMBER
                    FROM {BDC_HISTORY_TABLE} h WITH (NOLOCK)
                    WHERE h.STATUS = 'OPEN'
                )
                SELECT P.RDC,
                       SUM(P.ALLOC_QTY) AS alloc_qty,
                       SUM(P.BDC_QTY)  AS bdc_qty,
                       SUM(P.DO_QTY)   AS do_qty,
                       SUM(P.PEND_QTY) AS pend_qty,
                       SUM(CASE
                           WHEN P.PEND_QTY > 0 AND ok.RDC IS NULL
                           THEN P.PEND_QTY ELSE 0
                       END) AS pending_bdc_qty,
                       COUNT(*)      AS rows
                FROM {PEND_ALC_TABLE} P WITH (NOLOCK)
                LEFT JOIN open_keys ok
                  ON  ok.RDC = P.RDC
                 AND ok.ST_CD = ISNULL(P.ST_CD,'')
                 AND ok.ARTICLE_NUMBER = P.ARTICLE_NUMBER
                WHERE P.IS_CLOSED = 0{p_and}
                GROUP BY P.RDC
                ORDER BY SUM(P.PEND_QTY) DESC
            """), at_params).fetchall()

            # Typed-pend breakdown (Package 3): LEGACY = ALLOC_TYPE NULL/''.
            by_alloc_type = conn.execute(text(f"""
                SELECT CASE WHEN ISNULL(ALLOC_TYPE,'') = '' THEN 'LEGACY'
                            ELSE ALLOC_TYPE END AS alloc_type,
                       SUM(ALLOC_QTY) AS alloc_qty,
                       SUM(BDC_QTY)   AS bdc_qty,
                       SUM(DO_QTY)    AS do_qty,
                       SUM(PEND_QTY)  AS pend_qty,
                       COUNT(*)       AS rows
                FROM {PEND_ALC_TABLE} WITH (NOLOCK)
                WHERE IS_CLOSED = 0{b_and}
                GROUP BY CASE WHEN ISNULL(ALLOC_TYPE,'') = '' THEN 'LEGACY'
                              ELSE ALLOC_TYPE END
                ORDER BY SUM(PEND_QTY) DESC
            """), at_params).fetchall()

            # Legacy status buckets driven by PEND_ALC.BDC_QTY snapshot.
            # Kept for back-compat with the existing 4-tile UI.
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
                WHERE 1=1{b_and}
            """), at_params).fetchone()

            # Accurate lifecycle tiles driven by ARS_BDC_HISTORY status:
            #   pending_bdc_generate = (RDC, ST_CD, ARTICLE) keys with open
            #     PEND_QTY and NO STATUS='OPEN' history row → what the next
            #     /bdc-generate would pick up. Matches the
            #     _NO_OPEN_BDC_PREDICATE filter exactly.
            #   pending_do_against_bdc = sum of (BDC_QTY - DO_RECEIVED) on
            #     ARS_BDC_HISTORY where STATUS='OPEN' → qty in flight to SAP.
            pending_bdc_gen = conn.execute(text(f"""
                SELECT COUNT(*) AS rows, ISNULL(SUM(PEND_QTY),0) AS qty
                FROM (
                    SELECT SUM(P.PEND_QTY) AS PEND_QTY
                    FROM {PEND_ALC_TABLE} P WITH (NOLOCK)
                    WHERE P.IS_CLOSED = 0 AND P.PEND_QTY > 0{p_and}
                      AND NOT EXISTS (
                          SELECT 1 FROM {BDC_HISTORY_TABLE} h WITH (NOLOCK)
                          WHERE h.RDC = P.RDC
                            AND ISNULL(h.ST_CD,'') = ISNULL(P.ST_CD,'')
                            AND h.ARTICLE_NUMBER = P.ARTICLE_NUMBER
                            AND h.STATUS = 'OPEN'
                      )
                    GROUP BY P.RDC, ISNULL(P.ST_CD,''), P.ARTICLE_NUMBER
                    HAVING SUM(P.PEND_QTY) > 0
                ) x
            """), at_params).fetchone()

            # Pending DO (Open BDC) is now anchored on PEND_ALC, not on
            # BDC_HISTORY. The previous version summed BDC_QTY-DO_RECEIVED
            # from STATUS='OPEN' history rows, which over-counted by 5-10×
            # whenever DO uploads closed PEND_ALC rows without matching
            # back to all of their open history (the orphan-history bug).
            # Reading from PEND_ALC's BDC_QTY/DO_QTY guarantees the
            # invariant: aging total = Pending BDC Generate + In flight.
            pending_do_in_flight = conn.execute(text(f"""
                SELECT COUNT(*) AS rows,
                       ISNULL(SUM(P.BDC_QTY - P.DO_QTY), 0) AS qty
                FROM {PEND_ALC_TABLE} P WITH (NOLOCK)
                WHERE P.IS_CLOSED = 0{p_and}
                  AND P.BDC_QTY > P.DO_QTY
                  AND EXISTS (
                      SELECT 1 FROM {BDC_HISTORY_TABLE} h WITH (NOLOCK)
                      WHERE h.RDC = P.RDC
                        AND ISNULL(h.ST_CD,'') = ISNULL(P.ST_CD,'')
                        AND h.ARTICLE_NUMBER = P.ARTICLE_NUMBER
                        AND h.STATUS = 'OPEN'
                  )
            """), at_params).fetchone()

            # Keep the old history-based view available too for diagnostics
            # (UI shows the new one; this lets us spot drift if it
            # reappears).
            _bdc_history_open_legacy = conn.execute(text(f"""
                SELECT COUNT(*) AS rows,
                       ISNULL(SUM(BDC_QTY - DO_RECEIVED),0) AS qty
                FROM {BDC_HISTORY_TABLE} WITH (NOLOCK)
                WHERE STATUS = 'OPEN' AND (BDC_QTY - DO_RECEIVED) > 0
            """)).fetchone()

        return {
            "success": True,
            "data": {
                "by_mode": [
                    {"mode": r[0], "alloc_qty": float(r[1] or 0),
                     "bdc_qty": float(r[2] or 0), "do_qty": float(r[3] or 0),
                     "pend_qty": float(r[4] or 0),
                     "pending_bdc_qty": float(r[5] or 0),
                     "rows": int(r[6] or 0)}
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
                     "pend_qty": float(r[4] or 0),
                     "pending_bdc_qty": float(r[5] or 0),
                     "rows": int(r[6] or 0)}
                    for r in by_rdc
                ],
                "by_alloc_type": [
                    {"alloc_type": r[0], "alloc_qty": float(r[1] or 0),
                     "bdc_qty": float(r[2] or 0), "do_qty": float(r[3] or 0),
                     "pend_qty": float(r[4] or 0), "rows": int(r[5] or 0)}
                    for r in by_alloc_type
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
                    # New: derived from ARS_BDC_HISTORY (accurate vs. the
                    # legacy PEND_ALC.BDC_QTY-snapshot tiles above).
                    "pending_bdc_generate": {
                        "rows": int(pending_bdc_gen[0] or 0),
                        "qty":  float(pending_bdc_gen[1] or 0),
                    },
                    "pending_do_against_bdc": {
                        "rows": int(pending_do_in_flight[0] or 0),
                        "qty":  float(pending_do_in_flight[1] or 0),
                    },
                    # Diagnostic only — the legacy BDC_HISTORY-based view
                    # of "Pending DO". If this drifts above
                    # pending_do_against_bdc, there are orphan OPEN
                    # history rows again — run
                    # POST /pend-alc/operations/close-orphan-bdc-history
                    # to clean them up.
                    "_bdc_history_open_legacy": {
                        "rows": int(_bdc_history_open_legacy[0] or 0),
                        "qty":  float(_bdc_history_open_legacy[1] or 0),
                    },
                },
            },
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# GET /pend-alc/pend-vs-msa-gap          — open pending whose MSA pool is
#                                          missing or too small to cover it.
# GET /pend-alc/pend-vs-msa-gap-export   — same data, CSV download.
#
# Returns one row per (RDC, ARTICLE_NUMBER) where:
#   * the article has open PEND_QTY > 0 in ARS_PEND_ALC, AND
#   * either MSA_TOTAL has no row for that (RDC, ARTICLE) — "NO_MSA", or
#     STK_QTY - HOLD_QTY < PEND_QTY (un-clamped — FNL_Q is floored at 0 and
#     hides the shortfall) — "SHORT".
#
# gap = PEND_QTY - max(STK_QTY - HOLD_QTY, 0)
# ---------------------------------------------------------------------------
def _msa_total_article_col(conn) -> Optional[str]:
    """Probe ARS_MSA_TOTAL for the article column (varies by deployment).
    Returns None if ARS_MSA_TOTAL itself doesn't exist."""
    exists = conn.execute(text(
        "SELECT CASE WHEN OBJECT_ID('dbo.ARS_MSA_TOTAL','U') IS NULL "
        "            THEN 0 ELSE 1 END"
    )).scalar() or 0
    if not exists:
        return None
    for candidate in ("ARTICLE_NUMBER", "VAR_ART", "ARTICLE"):
        found = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME='ARS_MSA_TOTAL' AND COLUMN_NAME=:c"
        ), {"c": candidate}).scalar() or 0
        if found:
            return candidate
    return "ARTICLE_NUMBER"


def _msa_total_has_col(conn, col: str) -> bool:
    found = conn.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME='ARS_MSA_TOTAL' AND COLUMN_NAME=:c"
    ), {"c": col}).scalar() or 0
    return bool(found)


def _pend_vs_msa_gap_query(art_col: Optional[str], rdc_filter: bool, maj_cat_filter: bool,
                           status_filter: Optional[str], has_hold: bool = True,
                           has_fnl: bool = True, has_msa_pend: bool = True) -> str:
    pend_filters = ["IS_CLOSED = 0", "PEND_QTY > 0"]
    if rdc_filter:     pend_filters.append("RDC = :rdc")
    if maj_cat_filter: pend_filters.append("MAJ_CAT = :mc")
    pend_where = " AND ".join(pend_filters)

    if not art_col:
        # MSA_TOTAL missing — every open pending row is NO_MSA.
        msa_cte = (
            "msa AS (SELECT TOP 0 CAST(NULL AS NVARCHAR(20)) AS RDC, "
            "                       CAST(NULL AS NVARCHAR(30)) AS ART, "
            "                       CAST(0 AS FLOAT) AS STK_QTY, "
            "                       CAST(0 AS FLOAT) AS HOLD_QTY, "
            "                       CAST(0 AS FLOAT) AS MSA_PEND_QTY, "
            "                       CAST(0 AS FLOAT) AS FNL_Q "
            "        FROM ARS_PEND_ALC WHERE 1=0)"
        )
    else:
        hold_sel = ("SUM(TRY_CAST(HOLD_QTY AS FLOAT))" if has_hold else "CAST(0 AS FLOAT)")
        fnl_sel  = ("SUM(TRY_CAST(FNL_Q    AS FLOAT))" if has_fnl  else "CAST(0 AS FLOAT)")
        pend_sel = ("SUM(TRY_CAST(PEND_QTY AS FLOAT))" if has_msa_pend else "CAST(0 AS FLOAT)")
        msa_cte = (
            f"msa AS (SELECT RDC, [{art_col}] AS ART, "
            f"         SUM(TRY_CAST(STK_QTY  AS FLOAT)) AS STK_QTY, "
            f"         {hold_sel} AS HOLD_QTY, "
            f"         {pend_sel} AS MSA_PEND_QTY, "
            f"         {fnl_sel}  AS FNL_Q "
            f"        FROM ARS_MSA_TOTAL WITH (NOLOCK) "
            f"        GROUP BY RDC, [{art_col}])"
        )

    status_where = ""
    if status_filter == "NO_MSA":
        status_where = "AND m.STK_QTY IS NULL"
    elif status_filter == "SHORT":
        status_where = "AND m.STK_QTY IS NOT NULL"

    return f"""
        ;WITH pend_agg AS (
            SELECT RDC, ARTICLE_NUMBER,
                   MAX(MAJ_CAT)        AS MAJ_CAT,
                   MAX(GEN_ART_NUMBER) AS GEN_ART_NUMBER,
                   MAX(CLR)            AS CLR,
                   SUM(TRY_CAST(PEND_QTY AS FLOAT)) AS PEND_QTY,
                   COUNT(*)            AS PEND_ROWS
            FROM {PEND_ALC_TABLE} WITH (NOLOCK)
            WHERE {pend_where}
            GROUP BY RDC, ARTICLE_NUMBER
        ), {msa_cte}
        SELECT p.RDC, p.ARTICLE_NUMBER, p.MAJ_CAT,
               p.GEN_ART_NUMBER, p.CLR,
               p.PEND_QTY,
               p.PEND_ROWS,
               ISNULL(m.STK_QTY, 0)      AS STK_QTY,
               ISNULL(m.HOLD_QTY, 0)     AS HOLD_QTY,
               ISNULL(m.MSA_PEND_QTY, 0) AS MSA_PEND_QTY,
               ISNULL(m.FNL_Q, 0)        AS FNL_Q,
               CASE
                   WHEN m.STK_QTY IS NULL                            THEN 0
                   WHEN ISNULL(m.STK_QTY,0)-ISNULL(m.HOLD_QTY,0) < 0 THEN 0
                   ELSE                ISNULL(m.STK_QTY,0)-ISNULL(m.HOLD_QTY,0)
               END AS AVAILABLE,
               p.PEND_QTY -
               CASE
                   WHEN m.STK_QTY IS NULL                            THEN 0
                   WHEN ISNULL(m.STK_QTY,0)-ISNULL(m.HOLD_QTY,0) < 0 THEN 0
                   ELSE                ISNULL(m.STK_QTY,0)-ISNULL(m.HOLD_QTY,0)
               END AS GAP,
               CASE WHEN m.STK_QTY IS NULL THEN 'NO_MSA' ELSE 'SHORT' END AS STATUS
        FROM pend_agg p
        LEFT JOIN msa m
               ON m.RDC = p.RDC AND m.ART = p.ARTICLE_NUMBER
        WHERE (m.STK_QTY IS NULL
            OR p.PEND_QTY > CASE WHEN ISNULL(m.STK_QTY,0)-ISNULL(m.HOLD_QTY,0) < 0
                                  THEN 0
                                  ELSE ISNULL(m.STK_QTY,0)-ISNULL(m.HOLD_QTY,0) END)
              {status_where}
    """


@router.get("/pend-vs-msa-gap")
def pend_vs_msa_gap(
    rdc:        Optional[str] = Query(None),
    maj_cat:    Optional[str] = Query(None),
    status:     Optional[str] = Query(None, pattern="^(NO_MSA|SHORT)$"),
    page:       int           = Query(1,   ge=1),
    page_size:  int           = Query(200, ge=1, le=5000),
    sort_by:    str           = Query("gap", pattern="^(gap|pend_qty|rdc|article_number|maj_cat|available)$"),
    sort_dir:   str           = Query("desc", pattern="^(asc|desc)$"),
    current_user: User = Depends(get_current_user),
):
    """Paged report of open pending qty whose MSA stock can't cover it.

    A row appears when:
      * the (RDC, ARTICLE) has open PEND_QTY > 0 in ARS_PEND_ALC, AND
      * ARS_MSA_TOTAL either has no row for that key (status='NO_MSA') or
        STK_QTY - HOLD_QTY < PEND_QTY (status='SHORT').

    `gap` = PEND_QTY - max(STK_QTY - HOLD_QTY, 0).
    """
    try:
        with _engine().connect() as conn:
            ensure_pend_alc_table(conn)
            art_col = _msa_total_article_col(conn)
            has_hold     = art_col is not None and _msa_total_has_col(conn, "HOLD_QTY")
            has_fnl      = art_col is not None and _msa_total_has_col(conn, "FNL_Q")
            has_msa_pend = art_col is not None and _msa_total_has_col(conn, "PEND_QTY")

            params: dict = {}
            if rdc:     params["rdc"] = rdc
            if maj_cat: params["mc"]  = maj_cat

            base_sql = _pend_vs_msa_gap_query(
                art_col, rdc_filter=bool(rdc), maj_cat_filter=bool(maj_cat),
                status_filter=status,
                has_hold=has_hold, has_fnl=has_fnl, has_msa_pend=has_msa_pend,
            )

            # T-SQL doesn't let us wrap a CTE-led query in a subquery, so we
            # materialise the gap set to a temp table once and run summary +
            # paged SELECTs against it.
            tmp = f"#gap_{_uuid.uuid4().hex[:8]}"
            # Inject INTO right before the final FROM by string-replace —
            # base_sql is fully under our control so this is safe.
            into_sql = base_sql.replace(
                "FROM pend_agg p\n        LEFT JOIN msa m",
                f"INTO {tmp}\n        FROM pend_agg p\n        LEFT JOIN msa m",
                1,
            )
            conn.execute(text(into_sql), params)

            summary_row = conn.execute(text(f"""
                SELECT
                    COUNT(*)                                                     AS rows_total,
                    ISNULL(SUM(GAP), 0)                                          AS gap_total,
                    ISNULL(SUM(PEND_QTY), 0)                                     AS pend_total,
                    ISNULL(SUM(CASE WHEN STATUS='NO_MSA' THEN 1 ELSE 0 END), 0)  AS rows_no_msa,
                    ISNULL(SUM(CASE WHEN STATUS='NO_MSA' THEN GAP ELSE 0 END),0) AS gap_no_msa,
                    ISNULL(SUM(CASE WHEN STATUS='SHORT'  THEN 1 ELSE 0 END), 0)  AS rows_short,
                    ISNULL(SUM(CASE WHEN STATUS='SHORT'  THEN GAP ELSE 0 END),0) AS gap_short
                FROM {tmp}
            """)).fetchone()

            sort_col = {
                "gap": "GAP", "pend_qty": "PEND_QTY", "rdc": "RDC",
                "article_number": "ARTICLE_NUMBER", "maj_cat": "MAJ_CAT",
                "available": "AVAILABLE",
            }[sort_by]
            order = "DESC" if sort_dir.lower() == "desc" else "ASC"
            offset = (page - 1) * page_size

            rows = conn.execute(text(f"""
                SELECT * FROM {tmp}
                ORDER BY {sort_col} {order}, RDC ASC, ARTICLE_NUMBER ASC
                OFFSET :off ROWS FETCH NEXT :ps ROWS ONLY
            """), {"off": offset, "ps": page_size}).mappings().all()

            try:
                conn.execute(text(f"DROP TABLE {tmp}"))
            except Exception:
                pass

        return {
            "success": True,
            "data": {
                "rows": [
                    {
                        "rdc":            r["RDC"],
                        "article_number": r["ARTICLE_NUMBER"],
                        "maj_cat":        r["MAJ_CAT"],
                        "gen_art_number": r["GEN_ART_NUMBER"],
                        "clr":            r["CLR"],
                        "pend_qty":       float(r["PEND_QTY"] or 0),
                        "pend_rows":      int(r["PEND_ROWS"] or 0),
                        "stk_qty":        float(r["STK_QTY"] or 0),
                        "hold_qty":       float(r["HOLD_QTY"] or 0),
                        "msa_pend_qty":   float(r["MSA_PEND_QTY"] or 0),
                        "fnl_q":          float(r["FNL_Q"] or 0),
                        "available":      float(r["AVAILABLE"] or 0),
                        "gap":            float(r["GAP"] or 0),
                        "status":         r["STATUS"],
                    }
                    for r in rows
                ],
                "page":      page,
                "page_size": page_size,
                "total":     int(summary_row[0] or 0),
                "summary": {
                    "rows_total":  int(summary_row[0] or 0),
                    "gap_total":   float(summary_row[1] or 0),
                    "pend_total":  float(summary_row[2] or 0),
                    "rows_no_msa": int(summary_row[3] or 0),
                    "gap_no_msa":  float(summary_row[4] or 0),
                    "rows_short":  int(summary_row[5] or 0),
                    "gap_short":   float(summary_row[6] or 0),
                    "msa_available": art_col is not None,
                },
            },
        }
    except Exception as e:
        logger.exception(f"[pend_alc] pend-vs-msa-gap failed: {e}")
        raise HTTPException(500, str(e))


@router.get("/pend-vs-msa-gap-export")
def pend_vs_msa_gap_export(
    rdc:     Optional[str] = Query(None),
    maj_cat: Optional[str] = Query(None),
    status:  Optional[str] = Query(None, pattern="^(NO_MSA|SHORT)$"),
    current_user: User = Depends(get_current_user),
):
    """CSV export of the full filtered gap report (no row cap)."""
    try:
        with _engine().connect() as conn:
            ensure_pend_alc_table(conn)
            art_col = _msa_total_article_col(conn)
            has_hold     = art_col is not None and _msa_total_has_col(conn, "HOLD_QTY")
            has_fnl      = art_col is not None and _msa_total_has_col(conn, "FNL_Q")
            has_msa_pend = art_col is not None and _msa_total_has_col(conn, "PEND_QTY")

            params: dict = {}
            if rdc:     params["rdc"] = rdc
            if maj_cat: params["mc"]  = maj_cat

            base_sql = _pend_vs_msa_gap_query(
                art_col, rdc_filter=bool(rdc), maj_cat_filter=bool(maj_cat),
                status_filter=status,
                has_hold=has_hold, has_fnl=has_fnl, has_msa_pend=has_msa_pend,
            )
            tmp = f"#gap_exp_{_uuid.uuid4().hex[:8]}"
            into_sql = base_sql.replace(
                "FROM pend_agg p\n        LEFT JOIN msa m",
                f"INTO {tmp}\n        FROM pend_agg p\n        LEFT JOIN msa m",
                1,
            )
            conn.execute(text(into_sql), params)
            rows = conn.execute(text(f"""
                SELECT * FROM {tmp}
                ORDER BY GAP DESC, RDC, ARTICLE_NUMBER
            """)).mappings().all()
            try:
                conn.execute(text(f"DROP TABLE {tmp}"))
            except Exception:
                pass

        buf = io.StringIO()
        buf.write("RDC,ARTICLE_NUMBER,MAJ_CAT,GEN_ART_NUMBER,CLR,STATUS,"
                  "PEND_QTY,PEND_ROWS,STK_QTY,HOLD_QTY,MSA_PEND_QTY,FNL_Q,"
                  "AVAILABLE,GAP\n")
        def _q(v):
            if v is None: return ""
            s = str(v)
            if any(c in s for c in (",", '"', "\n", "\r")):
                return '"' + s.replace('"', '""') + '"'
            return s
        for r in rows:
            buf.write(",".join([
                _q(r["RDC"]), _q(r["ARTICLE_NUMBER"]), _q(r["MAJ_CAT"]),
                _q(r["GEN_ART_NUMBER"]), _q(r["CLR"]), _q(r["STATUS"]),
                f"{float(r['PEND_QTY'] or 0):.0f}",
                str(int(r["PEND_ROWS"] or 0)),
                f"{float(r['STK_QTY'] or 0):.0f}",
                f"{float(r['HOLD_QTY'] or 0):.0f}",
                f"{float(r['MSA_PEND_QTY'] or 0):.0f}",
                f"{float(r['FNL_Q'] or 0):.0f}",
                f"{float(r['AVAILABLE'] or 0):.0f}",
                f"{float(r['GAP'] or 0):.0f}",
            ]) + "\n")

        fname = f"PEND_VS_MSA_GAP_{datetime.date.today().strftime('%Y%m%d')}.csv"
        body = "﻿" + buf.getvalue()
        return StreamingResponse(
            iter([body]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except Exception as e:
        logger.exception(f"[pend_alc] pend-vs-msa-gap-export failed: {e}")
        raise HTTPException(500, str(e))
