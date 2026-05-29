"""
ars_dashboard.py — Unified ARS Dashboard analytics endpoints (rev 2)

A single page combining the four data sources already in production:
    ARS_PEND_ALC               — approved allocations, DO, pending qty
    ARS_NL_TBL_HOLD_TRACKING   — held-back inventory
    ARS_ALLOC_WORKING          — current run's row-level allocation (via /listing)
    ARS_LISTING_WORKING        — current run's listing master      (via /listing)

The Product Drill tab is pure frontend — it reuses /listing/store-by-majcat,
/listing/opt-summary, /listing/var-summary (already battle-tested). The Overview
charts reuse /listing/summary. So this module is intentionally small.

Endpoints
    GET /ars-dashboard/summary             — KPI strip (alloc, pend, hold, gap)
    GET /ars-dashboard/dates               — date list with daily rollups
    GET /ars-dashboard/sessions-by-date    — populates the global Session dropdown
    GET /ars-dashboard/sessions            — full session rollup for one date
    GET /ars-dashboard/session-detail      — paged row detail for one session
    GET /ars-dashboard/trend               — alloc/pend/hold per day (stacked-chart fuel)
    GET /ars-dashboard/pending             — filterable pending list (Pending tab)
    GET /ars-dashboard/gap                 — gap rollup; group_by=majcat|rdc_article|session_article
    GET /ars-dashboard/gap/export          — Excel download of the current gap view

Scope filters are accepted as query params on every endpoint:
    ?date=YYYY-MM-DD  &sid=AW26-001
    &mc=MENWEAR,WOMENWEAR  &werks=V001,V002  &rdc=2700,1100
    &from=YYYY-MM-DD  &to=YYYY-MM-DD
"""
from __future__ import annotations

import io
from datetime import date as _date, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine
from app.models.rbac import User
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user

router = APIRouter(prefix="/ars-dashboard", tags=["ARS Dashboard"])

PEND_ALC  = "ARS_PEND_ALC"
HOLD_TBL  = "ARS_NL_TBL_HOLD_TRACKING"
ST_MASTER = "Master_ALC_INPUT_ST_MASTER"     # adds HUB + ST_STATUS via ST_CD join
PROD_VIEW = "VW_MASTER_PRODUCT"              # adds DIV + SSN + SUB_DIV via ARTICLE_NUMBER/MATNR join

# Session-review archives. Schema identical across all three:
# WORKING (current run, no SESSION_ID) → PARKED (parked sessions) → HISTORY (approved sessions).
LIST_HISTORY = "ARS_LISTING_WORKING_HISTORY"
LIST_PARKED  = "ARS_LISTING_WORKING_PARKED"
# Snapshot/input listing tables — EXCESS_STK source. The _WORKING_* equivalents
# zero out EXCESS_STK during allocation, so we read it from these instead.
LIST_SNAPSHOT_HISTORY = "ARS_LISTING_HISTORY"
LIST_SNAPSHOT_PARKED  = "ARS_LISTING_PARKED"
# MSA stock archives — RDC-grain (not store-grain) — sourced via GEN_ART rollups.
# FNL_Q = max(STK_QTY − PEND_QTY − ARS_PEND, 0) — the net replenishable stock.
MSA_HISTORY  = "ARS_MSA_GEN_ART_HISTORY"
MSA_PARKED   = "ARS_MSA_GEN_ART_PARKED"
# Variant-grain MSA archives — used only for stock context when alloc archive
# is missing. The primary article-size source is now ARS_ALLOC_PARKED/HISTORY.
MSA_VAR_HISTORY = "ARS_MSA_VAR_ART_HISTORY"
MSA_VAR_PARKED  = "ARS_MSA_VAR_ART_PARKED"
# Size-grain allocation archives. Keyed by SESSION_ID + (WERKS, MAJ_CAT, RDC,
# GEN_ART_NUMBER, CLR, VAR_ART, SZ). Carry SZ_MBQ / SZ_STK / SZ_REQ / FNL_Q /
# SHIP_QTY / HOLD_QTY / ALLOC_QTY / ALLOC_STATUS / ALLOC_REMARKS / etc.
ALLOC_HISTORY = "ARS_ALLOC_HISTORY"
ALLOC_PARKED  = "ARS_ALLOC_PARKED"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _engine():
    return get_data_engine()


def _table_exists(conn, name: str) -> bool:
    return conn.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME=:n"
    ), {"n": name}).scalar() > 0


def _parse_scope(request: Request) -> Dict:
    """Pull the standard scope filters from query string.

    Scope keys:
        date, sid, mc, werks, rdc, from, to              (PEND_ALC native)
        hub, status                                      (joined from ST_MASTER)
        seg, div, sub_div, ssn                           (joined from VW_MASTER_PRODUCT)
        gen_art, clr, article                            (drill keys, single-value)
    """
    qp = request.query_params

    def _csv(key: str) -> List[str]:
        raw = qp.get(key) or ""
        return [s.strip() for s in raw.split(",") if s.strip()]

    return {
        "date":    qp.get("date") or None,
        "sid":     qp.get("sid")  or None,
        "mc":      _csv("mc"),
        "werks":   _csv("werks"),
        "rdc":     _csv("rdc"),
        "from":    qp.get("from") or None,
        "to":      qp.get("to")   or None,
        # NEW dimensions (rev 3)
        "hub":     _csv("hub"),
        "status":  _csv("status"),     # OLD / UPC
        "seg":     _csv("seg"),        # APP / GM
        "div":     _csv("div"),
        "sub_div": _csv("sub_div"),
        "ssn":     _csv("ssn"),
        # Drill keys (single-value, used by /drill/* endpoints only)
        "gen_art": qp.get("gen_art") or None,
        "clr":     qp.get("clr")     or None,
        "article": qp.get("article") or None,
    }


def _needs_joins(scope: Dict) -> Tuple[bool, bool]:
    """Return (needs_st_master, needs_master_product) based on which scope keys are set."""
    needs_st   = bool(scope.get("hub")) or bool(scope.get("status"))
    needs_prod = (bool(scope.get("seg")) or bool(scope.get("div"))
                  or bool(scope.get("sub_div")) or bool(scope.get("ssn")))
    return needs_st, needs_prod


def _from_clause(needs_st: bool, needs_prod: bool) -> str:
    """Build the FROM + LEFT JOIN fragment used by every reporting query."""
    sql = f"FROM {PEND_ALC} PA WITH (NOLOCK)"
    if needs_st:
        sql += f" LEFT JOIN {ST_MASTER} SM WITH (NOLOCK) ON SM.ST_CD = PA.ST_CD"
    if needs_prod:
        # PEND_ALC.ARTICLE_NUMBER (variant) ↔ VW_MASTER_PRODUCT.ARTICLE_NUMBER.
        # (Legacy code joined on PA.MATNR, but that column is fully NULL in prod.)
        sql += f" LEFT JOIN {PROD_VIEW} MP WITH (NOLOCK) ON MP.ARTICLE_NUMBER = PA.ARTICLE_NUMBER"
    return sql


def _default_window() -> Tuple[str, str]:
    """If neither date nor from/to is supplied, default to last 7 days."""
    today = _date.today()
    return (today - timedelta(days=6)).isoformat(), today.isoformat()


def _where_pend(scope: Dict, params: Dict, alias: str = "PA") -> str:
    """Build a WHERE clause for ARS_PEND_ALC from the scope dict."""
    parts: List[str] = []

    # Date / range
    if scope.get("date"):
        parts.append(f"CAST({alias}.APPROVED_AT AS DATE) = :p_date")
        params["p_date"] = scope["date"]
    else:
        d_from = scope.get("from")
        d_to   = scope.get("to")
        if not d_from and not d_to:
            d_from, d_to = _default_window()
        if d_from:
            parts.append(f"CAST({alias}.APPROVED_AT AS DATE) >= :p_from")
            params["p_from"] = d_from
        if d_to:
            parts.append(f"CAST({alias}.APPROVED_AT AS DATE) <= :p_to")
            params["p_to"] = d_to

    if scope.get("sid"):
        parts.append(f"{alias}.SESSION_ID = :p_sid")
        params["p_sid"] = scope["sid"]

    for col, key, vals in (
        ("MAJ_CAT", "p_mc",   scope.get("mc")    or []),
        ("ST_CD",   "p_werks", scope.get("werks") or []),
        ("RDC",     "p_rdc",   scope.get("rdc")   or []),
    ):
        if vals:
            ph = []
            for i, v in enumerate(vals):
                k = f"{key}{i}"
                params[k] = v
                ph.append(f":{k}")
            parts.append(f"{alias}.{col} IN ({','.join(ph)})")

    # NEW (rev 3) — joined dimensions
    for col, alias_join, key, vals in (
        ("HUB",       "SM", "p_hub",    scope.get("hub")     or []),
        ("ST_STATUS", "SM", "p_status", scope.get("status")  or []),
        ("SEG",       "MP", "p_seg",    scope.get("seg")     or []),
        ("DIV",       "MP", "p_div",    scope.get("div")     or []),
        ("SUB_DIV",   "MP", "p_sub_div", scope.get("sub_div") or []),
        ("SSN",       "MP", "p_ssn",    scope.get("ssn")     or []),
    ):
        if vals:
            ph = []
            for i, v in enumerate(vals):
                k = f"{key}{i}"
                params[k] = v
                ph.append(f":{k}")
            parts.append(f"{alias_join}.{col} IN ({','.join(ph)})")

    # Single-value drill keys
    if scope.get("gen_art"):
        parts.append(f"{alias}.GEN_ART_NUMBER = :p_gen_art")
        params["p_gen_art"] = scope["gen_art"]
    if scope.get("clr"):
        parts.append(f"{alias}.CLR = :p_clr")
        params["p_clr"] = scope["clr"]
    if scope.get("article"):
        parts.append(f"{alias}.ARTICLE_NUMBER = :p_article")
        params["p_article"] = scope["article"]

    return " WHERE " + " AND ".join(parts) if parts else ""


def _empty_summary() -> Dict:
    return {
        "alloc_qty":     0,
        "pend_qty":      0,
        "hold_qty":      0,
        "gap_rows":      0,
        "sessions":      0,
        "stores":        0,
        "articles_pend": 0,
        "articles_hold": 0,
        "open_rows":     0,
    }


# ---------------------------------------------------------------------------
# GET /summary    — KPI strip (4 cards) for the current scope
# ---------------------------------------------------------------------------
@router.get("/summary", response_model=APIResponse)
def get_summary(request: Request, current_user: User = Depends(get_current_user)):
    scope  = _parse_scope(request)
    engine = _engine()
    out    = _empty_summary()

    with engine.connect() as conn:
        if not _table_exists(conn, PEND_ALC):
            return APIResponse(success=True, data=out, message="ARS_PEND_ALC not found")

        params: Dict = {}
        where = _where_pend(scope, params)
        needs_st, needs_prod = _needs_joins(scope)
        from_sql = _from_clause(needs_st, needs_prod)

        row = conn.execute(text(f"""
            SELECT
                ISNULL(SUM(PA.ALLOC_QTY), 0)                                AS alloc_qty,
                ISNULL(SUM(CASE WHEN PA.IS_CLOSED = 0
                                THEN PA.PEND_QTY ELSE 0 END), 0)            AS pend_qty,
                ISNULL(SUM(CASE WHEN PA.IS_CLOSED = 0 AND PA.PEND_QTY > 0
                                THEN 1 ELSE 0 END), 0)                      AS gap_rows,
                COUNT(DISTINCT PA.SESSION_ID)                               AS sessions,
                COUNT(DISTINCT PA.ST_CD)                                    AS stores,
                COUNT(DISTINCT CASE WHEN PA.IS_CLOSED = 0 AND PA.PEND_QTY > 0
                                    THEN PA.ARTICLE_NUMBER END)             AS articles_pend,
                SUM(CASE WHEN PA.IS_CLOSED = 0 THEN 1 ELSE 0 END)           AS open_rows
            {from_sql}
            {where}
        """), params).mappings().first()

        if row:
            out.update({k: int(row[k] or 0) for k in (
                "alloc_qty", "pend_qty", "gap_rows", "sessions", "stores",
                "articles_pend", "open_rows",
            )})

        # Hold KPI — separate table. Apply only RDC / WERKS filters since hold
        # table doesn't carry SESSION_ID or APPROVED_AT.
        if _table_exists(conn, HOLD_TBL):
            hparams: Dict = {}
            hparts: List[str] = ["H.IS_CLOSED = 0"]
            if scope.get("werks"):
                ph = []
                for i, v in enumerate(scope["werks"]):
                    k = f"hw{i}"
                    hparams[k] = v
                    ph.append(f":{k}")
                hparts.append(f"H.WERKS IN ({','.join(ph)})")
            if scope.get("rdc"):
                # HOLD table may not have RDC column — guard at query time
                ph = []
                for i, v in enumerate(scope["rdc"]):
                    k = f"hr{i}"
                    hparams[k] = v
                    ph.append(f":{k}")
                hparts.append(
                    f"(NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('{HOLD_TBL}') "
                    f"AND name='RDC') OR H.RDC IN ({','.join(ph)}))"
                )
            hwhere = " WHERE " + " AND ".join(hparts)
            hrow = conn.execute(text(f"""
                SELECT
                    ISNULL(SUM(H.HOLD_REM), 0)            AS hold_qty,
                    COUNT(DISTINCT H.VAR_ART)             AS articles_hold
                FROM {HOLD_TBL} H WITH (NOLOCK)
                {hwhere}
            """), hparams).mappings().first()
            if hrow:
                out["hold_qty"]      = int(hrow["hold_qty"] or 0)
                out["articles_hold"] = int(hrow["articles_hold"] or 0)

    out["scope"] = scope
    return APIResponse(success=True, data=out)


# ---------------------------------------------------------------------------
# GET /breakdown    — top-N for Overview tab charts (scope-aware)
# Returns 4 chart-ready arrays in one call:
#   by_opt_type, by_rdc, by_maj_cat, by_store
# All sourced from ARS_PEND_ALC and ARS_ALLOC_WORKING (for OPT_TYPE).
# ---------------------------------------------------------------------------
@router.get("/breakdown", response_model=APIResponse)
def get_breakdown(
    request: Request,
    limit: int = Query(15, ge=1, le=100),
    current_user: User = Depends(get_current_user),
):
    scope = _parse_scope(request)
    engine = _engine()
    out = {
        "by_opt_type": [], "by_rdc": [], "by_maj_cat": [], "by_store": [],
        "by_hub": [], "by_status": [], "by_div": [], "by_ssn": [],
    }

    with engine.connect() as conn:
        if not _table_exists(conn, PEND_ALC):
            return APIResponse(success=True, data=out, message="ARS_PEND_ALC not found")

        # Per-column joins (DIV/SSN need VW_MASTER_PRODUCT which is large; only join when
        # that specific sub-query needs it — and use the same scope filters every time).
        def _top(col_expr: str, key: str, *, need_st: bool = False, need_prod: bool = False):
            params_local: Dict = {}
            where_local = _where_pend(scope, params_local)
            # If scope.hub/status/div/ssn is set, those joins are already required
            ns, np = _needs_joins(scope)
            from_local = _from_clause(ns or need_st, np or need_prod)
            try:
                rows = conn.execute(text(f"""
                    SELECT TOP {limit} {col_expr} AS name, ISNULL(SUM(PA.ALLOC_QTY), 0) AS qty
                    {from_local} {where_local}
                    GROUP BY {col_expr}
                    HAVING {col_expr} IS NOT NULL AND {col_expr} <> ''
                    ORDER BY ISNULL(SUM(PA.ALLOC_QTY), 0) DESC
                """), params_local).mappings().all()
                out[key] = [{"name": r["name"] or "—", "qty": int(r["qty"] or 0)} for r in rows]
            except Exception as e:
                logger.warning(f"/breakdown {key} failed: {e}")
                out[key] = []

        _top("PA.RDC",       "by_rdc")
        _top("PA.MAJ_CAT",   "by_maj_cat")
        _top("PA.ST_CD",     "by_store")
        _top("SM.HUB",       "by_hub",    need_st=True)
        _top("SM.ST_STATUS", "by_status", need_st=True)
        _top("MP.DIV",       "by_div",    need_prod=True)
        _top("MP.SSN",       "by_ssn",    need_prod=True)

    # by_opt_type — ARS_PEND_ALC doesn't carry OPT_TYPE. Read from ARS_ALLOC_WORKING
    # as a current-snapshot view (NOT scope-aware — same caveat as the listing page).
    with engine.connect() as conn:
        if _table_exists(conn, "ARS_ALLOC_WORKING"):
            try:
                rows = conn.execute(text("""
                    SELECT ISNULL([OPT_TYPE], 'UNTAGGED') AS name,
                           ROUND(ISNULL(SUM(TRY_CAST([ALLOC_QTY] AS FLOAT)), 0), 0) AS qty
                    FROM ARS_ALLOC_WORKING WITH (NOLOCK)
                    GROUP BY [OPT_TYPE]
                    ORDER BY qty DESC
                """)).mappings().all()
                out["by_opt_type"] = [{"name": r["name"], "qty": int(r["qty"] or 0)} for r in rows]
            except Exception as e:
                logger.warning(f"OPT_TYPE breakdown skipped: {e}")

    return APIResponse(success=True, data=out)


# ---------------------------------------------------------------------------
# GET /dates    — list of dates with per-day rollups
# ---------------------------------------------------------------------------
@router.get("/dates", response_model=APIResponse)
def get_dates(
    request: Request,
    days: int = Query(60, ge=1, le=365),
    current_user: User = Depends(get_current_user),
):
    scope = _parse_scope(request)
    # If neither from nor to passed, look back N days from today
    if not scope.get("from") and not scope.get("to") and not scope.get("date"):
        scope["from"] = (_date.today() - timedelta(days=days - 1)).isoformat()
        scope["to"]   = _date.today().isoformat()

    engine = _engine()
    with engine.connect() as conn:
        if not _table_exists(conn, PEND_ALC):
            return APIResponse(success=True, data={"items": []})

        params: Dict = {}
        where = _where_pend(scope, params)
        needs_st, needs_prod = _needs_joins(scope)
        from_sql = _from_clause(needs_st, needs_prod)
        rows = conn.execute(text(f"""
            SELECT
                CAST(PA.APPROVED_AT AS DATE)                                     AS run_date,
                COUNT(DISTINCT PA.SESSION_ID)                                    AS sessions,
                ISNULL(SUM(PA.ALLOC_QTY), 0)                                     AS alloc_qty,
                ISNULL(SUM(PA.DO_QTY),    0)                                     AS do_qty,
                ISNULL(SUM(CASE WHEN PA.IS_CLOSED = 0 THEN PA.PEND_QTY ELSE 0 END), 0) AS pend_qty,
                COUNT(DISTINCT PA.ST_CD)                                         AS stores,
                SUM(CASE WHEN PA.IS_CLOSED = 0 THEN 1 ELSE 0 END)                AS open_rows
            {from_sql}
            {where}
            GROUP BY CAST(PA.APPROVED_AT AS DATE)
            ORDER BY run_date DESC
        """), params).mappings().all()

    items = []
    for r in rows:
        d = r["run_date"]
        items.append({
            "date":      d.isoformat() if hasattr(d, "isoformat") else str(d),
            "sessions":  int(r["sessions"]  or 0),
            "alloc_qty": int(r["alloc_qty"] or 0),
            "do_qty":    int(r["do_qty"]    or 0),
            "pend_qty":  int(r["pend_qty"]  or 0),
            "stores":    int(r["stores"]    or 0),
            "open_rows": int(r["open_rows"] or 0),
            "status":    "open"    if (r["open_rows"] or 0) > 0 and (r["pend_qty"] or 0) > 0
                         else ("partial" if (r["open_rows"] or 0) > 0 else "closed"),
        })
    return APIResponse(success=True, data={"items": items})


# ---------------------------------------------------------------------------
# GET /sessions-by-date    — feeds the global Session dropdown
# ---------------------------------------------------------------------------
@router.get("/sessions-by-date", response_model=APIResponse)
def get_sessions_by_date(
    date: str = Query(..., description="YYYY-MM-DD"),
    current_user: User = Depends(get_current_user),
):
    engine = _engine()
    with engine.connect() as conn:
        if not _table_exists(conn, PEND_ALC):
            return APIResponse(success=True, data={"items": []})
        rows = conn.execute(text(f"""
            SELECT
                PA.SESSION_ID,
                MIN(PA.MAJ_CAT)                       AS maj_cat,
                MIN(PA.RDC)                           AS rdc,
                MIN(PA.ALLOC_MODE)                    AS mode,
                ISNULL(SUM(PA.ALLOC_QTY), 0)          AS alloc_qty,
                COUNT(DISTINCT PA.ST_CD)              AS stores
            FROM {PEND_ALC} PA WITH (NOLOCK)
            WHERE CAST(PA.APPROVED_AT AS DATE) = :d
            GROUP BY PA.SESSION_ID
            ORDER BY MIN(PA.APPROVED_AT) ASC
        """), {"d": date}).mappings().all()

    items = [{
        "session_id": r["SESSION_ID"],
        "maj_cat":    r["maj_cat"]   or "",
        "rdc":        r["rdc"]       or "",
        "mode":       r["mode"]      or "AUTO",
        "alloc_qty":  int(r["alloc_qty"] or 0),
        "stores":     int(r["stores"]    or 0),
        "label":      f"{r['SESSION_ID']} · {r['maj_cat'] or '—'} · {r['rdc'] or '—'} · {r['mode'] or 'AUTO'}",
    } for r in rows]
    return APIResponse(success=True, data={"items": items, "date": date})


# ---------------------------------------------------------------------------
# GET /sessions    — full session-grain rollup for one date
# ---------------------------------------------------------------------------
@router.get("/sessions", response_model=APIResponse)
def get_sessions(
    date: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
):
    engine = _engine()
    params: Dict = {}
    extra = ""
    if date:
        extra = "WHERE CAST(PA.APPROVED_AT AS DATE) = :d"
        params["d"] = date

    with engine.connect() as conn:
        if not _table_exists(conn, PEND_ALC):
            return APIResponse(success=True, data={"items": []})
        rows = conn.execute(text(f"""
            SELECT
                PA.SESSION_ID,
                MIN(PA.MAJ_CAT)                                                     AS maj_cat,
                MIN(PA.RDC)                                                         AS rdc,
                MIN(PA.ALLOC_MODE)                                                  AS mode,
                COUNT(DISTINCT PA.ARTICLE_NUMBER)                                   AS articles,
                COUNT(DISTINCT PA.ST_CD)                                            AS stores,
                ISNULL(SUM(PA.ALLOC_QTY), 0)                                        AS alloc_qty,
                ISNULL(SUM(PA.DO_QTY), 0)                                           AS do_qty,
                ISNULL(SUM(CASE WHEN PA.IS_CLOSED = 0
                                THEN PA.PEND_QTY ELSE 0 END), 0)                    AS pend_qty,
                SUM(CASE WHEN PA.IS_CLOSED = 0 THEN 1 ELSE 0 END)                   AS open_rows,
                MIN(PA.APPROVED_AT)                                                 AS started_at
            FROM {PEND_ALC} PA WITH (NOLOCK)
            {extra}
            GROUP BY PA.SESSION_ID
            ORDER BY MIN(PA.APPROVED_AT) ASC
        """), params).mappings().all()

    items = []
    for r in rows:
        pend = int(r["pend_qty"] or 0)
        items.append({
            "session_id": r["SESSION_ID"],
            "maj_cat":    r["maj_cat"] or "",
            "rdc":        r["rdc"]     or "",
            "mode":       r["mode"]    or "AUTO",
            "articles":   int(r["articles"]  or 0),
            "stores":     int(r["stores"]    or 0),
            "alloc_qty":  int(r["alloc_qty"] or 0),
            "do_qty":     int(r["do_qty"]    or 0),
            "pend_qty":   pend,
            "open_rows":  int(r["open_rows"] or 0),
            "status":     "open" if pend > 0 else ("partial" if (r["open_rows"] or 0) > 0 else "closed"),
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
        })
    return APIResponse(success=True, data={"items": items, "date": date})


# ---------------------------------------------------------------------------
# GET /session-detail    — paged row-level rows for one session
# ---------------------------------------------------------------------------
@router.get("/session-detail", response_model=APIResponse)
def get_session_detail(
    sid:       str = Query(..., alias="sid"),
    page:      int = Query(1,  ge=1),
    page_size: int = Query(50, ge=1, le=500),
    only_gap:  bool = Query(False, description="Only rows with PEND_QTY > 0"),
    current_user: User = Depends(get_current_user),
):
    engine = _engine()
    params: Dict = {"sid": sid}
    where = "PA.SESSION_ID = :sid"
    if only_gap:
        where += " AND PA.IS_CLOSED = 0 AND PA.PEND_QTY > 0"
    offset = (page - 1) * page_size

    with engine.connect() as conn:
        if not _table_exists(conn, PEND_ALC):
            return APIResponse(success=True, data={"items": [], "total": 0, "page": page})

        total = int(conn.execute(text(
            f"SELECT COUNT(*) FROM {PEND_ALC} PA WITH (NOLOCK) WHERE {where}"
        ), params).scalar() or 0)

        rows = conn.execute(text(f"""
            SELECT
                PA.SESSION_ID, PA.RDC, PA.ST_CD,
                PA.ARTICLE_NUMBER, PA.MAJ_CAT, PA.GEN_ART_NUMBER, PA.CLR,
                PA.ALLOC_QTY, PA.DO_QTY, PA.PEND_QTY,
                PA.IS_CLOSED, PA.ALLOC_MODE, PA.SOURCE,
                PA.APPROVED_AT, PA.LAST_DO_AT
            FROM {PEND_ALC} PA WITH (NOLOCK)
            WHERE {where}
            ORDER BY PA.RDC, PA.ST_CD, PA.ARTICLE_NUMBER
            OFFSET :off ROWS FETCH NEXT :ps ROWS ONLY
        """), {**params, "off": offset, "ps": page_size}).mappings().all()

    items = []
    for r in rows:
        alloc = int(r["ALLOC_QTY"] or 0)
        pend  = int(r["PEND_QTY"]  or 0)
        gap_pct = int(round(pend / alloc * 100)) if alloc else 0
        items.append({
            "session_id": r["SESSION_ID"],
            "rdc":        r["RDC"],
            "st_cd":      r["ST_CD"],
            "article":    r["ARTICLE_NUMBER"],
            "maj_cat":    r["MAJ_CAT"],
            "gen_art":    r["GEN_ART_NUMBER"],
            "clr":        r["CLR"],
            "alloc_qty":  alloc,
            "do_qty":     int(r["DO_QTY"] or 0),
            "pend_qty":   pend,
            "is_closed":  bool(r["IS_CLOSED"]),
            "mode":       r["ALLOC_MODE"] or "AUTO",
            "source":     r["SOURCE"]     or "AUTO",
            "approved_at": r["APPROVED_AT"].isoformat() if r["APPROVED_AT"] else None,
            "last_do_at":  r["LAST_DO_AT"].isoformat()  if r["LAST_DO_AT"]  else None,
            "gap_pct":    gap_pct,
        })
    return APIResponse(success=True, data={
        "items": items, "total": total, "page": page, "page_size": page_size,
    })


# ---------------------------------------------------------------------------
# GET /trend    — per-day alloc / pend / hold for the stacked trend chart
# ---------------------------------------------------------------------------
@router.get("/trend", response_model=APIResponse)
def get_trend(
    request: Request,
    days: int = Query(7, ge=1, le=90),
    current_user: User = Depends(get_current_user),
):
    scope = _parse_scope(request)
    # Force a window of N days ending today (unless the user passed from/to)
    if not scope.get("from") and not scope.get("to"):
        scope["from"] = (_date.today() - timedelta(days=days - 1)).isoformat()
        scope["to"]   = _date.today().isoformat()
    # Clear single-date if set — trend always wants a range
    scope["date"] = None

    engine = _engine()
    params: Dict = {}
    where = _where_pend(scope, params)
    needs_st, needs_prod = _needs_joins(scope)
    from_sql = _from_clause(needs_st, needs_prod)

    with engine.connect() as conn:
        if not _table_exists(conn, PEND_ALC):
            return APIResponse(success=True, data={"items": []})
        rows = conn.execute(text(f"""
            SELECT
                CAST(PA.APPROVED_AT AS DATE)                                      AS run_date,
                ISNULL(SUM(PA.ALLOC_QTY), 0)                                      AS alloc_qty,
                ISNULL(SUM(CASE WHEN PA.IS_CLOSED = 0
                                THEN PA.PEND_QTY ELSE 0 END), 0)                  AS pend_qty
            {from_sql}
            {where}
            GROUP BY CAST(PA.APPROVED_AT AS DATE)
            ORDER BY run_date ASC
        """), params).mappings().all()

    # Hold is a snapshot (no APPROVED_AT) — return one constant value alongside.
    hold_total = 0
    with engine.connect() as conn:
        if _table_exists(conn, HOLD_TBL):
            hold_total = int(conn.execute(text(
                f"SELECT ISNULL(SUM(HOLD_REM), 0) FROM {HOLD_TBL} WITH (NOLOCK) WHERE IS_CLOSED = 0"
            )).scalar() or 0)

    items = [{
        "date":      r["run_date"].isoformat() if hasattr(r["run_date"], "isoformat") else str(r["run_date"]),
        "alloc_qty": int(r["alloc_qty"] or 0),
        "pend_qty":  int(r["pend_qty"]  or 0),
        "hold_qty":  hold_total,  # repeated each day — UI shows it as a flat layer
    } for r in rows]
    return APIResponse(success=True, data={"items": items, "hold_snapshot": hold_total})


# ---------------------------------------------------------------------------
# GET /pending    — filterable pending list (drives the Pending tab)
# ---------------------------------------------------------------------------
@router.get("/pending", response_model=APIResponse)
def get_pending(
    request:   Request,
    page:      int  = Query(1, ge=1),
    page_size: int  = Query(50, ge=1, le=500),
    age_bucket: Optional[str] = Query(None, pattern=r"^(0_7|8_30|31\+)?$"),
    current_user: User = Depends(get_current_user),
):
    scope = _parse_scope(request)
    engine = _engine()
    params: Dict = {}
    where  = _where_pend(scope, params)
    needs_st, needs_prod = _needs_joins(scope)
    from_sql = _from_clause(needs_st, needs_prod)

    # Only-open + age bucket
    open_clause = " AND PA.IS_CLOSED = 0 AND PA.PEND_QTY > 0"
    age_clause  = ""
    if age_bucket == "0_7":
        age_clause = " AND DATEDIFF(DAY, PA.APPROVED_AT, GETDATE()) BETWEEN 0 AND 7"
    elif age_bucket == "8_30":
        age_clause = " AND DATEDIFF(DAY, PA.APPROVED_AT, GETDATE()) BETWEEN 8 AND 30"
    elif age_bucket == "31+":
        age_clause = " AND DATEDIFF(DAY, PA.APPROVED_AT, GETDATE()) > 30"

    if where:
        where = where + open_clause + age_clause
    else:
        where = " WHERE 1=1" + open_clause + age_clause

    offset = (page - 1) * page_size
    with engine.connect() as conn:
        if not _table_exists(conn, PEND_ALC):
            return APIResponse(success=True, data={"items": [], "total": 0})

        total = int(conn.execute(text(
            f"SELECT COUNT(*) {from_sql} {where}"
        ), params).scalar() or 0)

        agg = conn.execute(text(f"""
            SELECT
                ISNULL(SUM(PA.PEND_QTY), 0)                                AS pend_qty,
                AVG(CAST(DATEDIFF(DAY, PA.APPROVED_AT, GETDATE()) AS FLOAT)) AS avg_age
            {from_sql} {where}
        """), params).mappings().first() or {}

        rows = conn.execute(text(f"""
            SELECT
                PA.SESSION_ID, PA.RDC, PA.ST_CD, PA.ARTICLE_NUMBER, PA.MAJ_CAT,
                PA.ALLOC_QTY, PA.DO_QTY, PA.PEND_QTY,
                DATEDIFF(DAY, PA.APPROVED_AT, GETDATE())                 AS age_days,
                PA.ALLOC_MODE, PA.APPROVED_AT
            FROM {PEND_ALC} PA WITH (NOLOCK)
            {where}
            ORDER BY DATEDIFF(DAY, PA.APPROVED_AT, GETDATE()) DESC, PA.PEND_QTY DESC
            OFFSET :off ROWS FETCH NEXT :ps ROWS ONLY
        """), {**params, "off": offset, "ps": page_size}).mappings().all()

    items = []
    for r in rows:
        alloc = int(r["ALLOC_QTY"] or 0)
        pend  = int(r["PEND_QTY"]  or 0)
        age   = int(r["age_days"]  or 0)
        items.append({
            "session_id": r["SESSION_ID"],
            "rdc":        r["RDC"],
            "st_cd":      r["ST_CD"],
            "article":    r["ARTICLE_NUMBER"],
            "maj_cat":    r["MAJ_CAT"],
            "alloc_qty":  alloc,
            "do_qty":     int(r["DO_QTY"] or 0),
            "pend_qty":   pend,
            "pend_pct":   int(round(pend / alloc * 100)) if alloc else 0,
            "age_days":   age,
            "mode":       r["ALLOC_MODE"] or "AUTO",
            "status":     "aged" if age > 30 else "open",
        })
    return APIResponse(success=True, data={
        "items":      items,
        "total":      total,
        "page":       page,
        "page_size":  page_size,
        "pend_total": int(agg.get("pend_qty") or 0),
        "avg_age":    float(round(agg.get("avg_age") or 0, 1)),
    })


# ---------------------------------------------------------------------------
# GET /drill/*    — hierarchical drill (used by BOTH Date&Session tab AND
#                   Product Drill tab). Every level returns the SAME rollup
#                   shape: { name, alloc_qty, do_qty, pend_qty, stores, articles }
# ---------------------------------------------------------------------------
def _drill_level(conn, scope: Dict, group_col: str, extra_select: str = "",
                 name_alias: str = "name", limit: int = 1000,
                 force_st: bool = False, force_prod: bool = False) -> List[Dict]:
    params: Dict = {}
    where = _where_pend(scope, params)
    needs_st, needs_prod = _needs_joins(scope)
    # If grouping by, or extra-selecting from, a joined column — force the relevant join
    if group_col.startswith("SM.") or "SM." in extra_select or force_st:
        needs_st = True
    if group_col.startswith("MP.") or "MP." in extra_select or force_prod:
        needs_prod = True
    from_sql = _from_clause(needs_st, needs_prod)
    rows = conn.execute(text(f"""
        SELECT TOP {limit}
            {group_col} AS {name_alias}{(', ' + extra_select) if extra_select else ''},
            ISNULL(SUM(PA.ALLOC_QTY), 0)                                        AS alloc_qty,
            ISNULL(SUM(PA.DO_QTY),    0)                                        AS do_qty,
            ISNULL(SUM(CASE WHEN PA.IS_CLOSED = 0
                            THEN PA.PEND_QTY ELSE 0 END), 0)                    AS pend_qty,
            COUNT(DISTINCT PA.ST_CD)                                            AS stores,
            COUNT(DISTINCT PA.ARTICLE_NUMBER)                                   AS articles,
            COUNT(*)                                                            AS rows_n
        {from_sql}
        {where}
        GROUP BY {group_col}
        ORDER BY ISNULL(SUM(PA.ALLOC_QTY), 0) DESC
    """), params).mappings().all()
    out = []
    for r in rows:
        item = dict(r)
        # Cast numerics defensively
        for k in ("alloc_qty", "do_qty", "pend_qty", "stores", "articles", "rows_n"):
            if k in item and item[k] is not None:
                item[k] = int(item[k]) if isinstance(item[k], (int, float)) else item[k]
        if item.get(name_alias) is None:
            item[name_alias] = "—"
        out.append(item)
    return out


@router.get("/drill/maj-cats", response_model=APIResponse)
def drill_maj_cats(request: Request, current_user: User = Depends(get_current_user)):
    """Top-level MAJ_CAT rollup for the given scope."""
    scope = _parse_scope(request)
    engine = _engine()
    with engine.connect() as conn:
        if not _table_exists(conn, PEND_ALC):
            return APIResponse(success=True, data={"items": []})
        items = _drill_level(conn, scope, "PA.MAJ_CAT")
    return APIResponse(success=True, data={"items": items})


@router.get("/drill/stores", response_model=APIResponse)
def drill_stores(request: Request, current_user: User = Depends(get_current_user)):
    """Store rollup. Scope.mc / scope.werks etc all apply."""
    scope = _parse_scope(request)
    engine = _engine()
    with engine.connect() as conn:
        if not _table_exists(conn, PEND_ALC):
            return APIResponse(success=True, data={"items": []})
        items = _drill_level(conn, scope, "PA.ST_CD",
                             extra_select="MIN(SM.ST_NM) AS st_nm, MIN(SM.HUB) AS hub, MIN(SM.ST_STATUS) AS status")
    return APIResponse(success=True, data={"items": items})


@router.get("/drill/gen-arts", response_model=APIResponse)
def drill_gen_arts(request: Request, current_user: User = Depends(get_current_user)):
    """GEN_ART/CLR rollup. Returns one row per (gen_art_number, clr)."""
    scope = _parse_scope(request)
    engine = _engine()
    with engine.connect() as conn:
        if not _table_exists(conn, PEND_ALC):
            return APIResponse(success=True, data={"items": []})
        params: Dict = {}
        where = _where_pend(scope, params)
        needs_st, needs_prod = _needs_joins(scope)
        from_sql = _from_clause(needs_st, needs_prod)
        rows = conn.execute(text(f"""
            SELECT TOP 1000
                PA.GEN_ART_NUMBER                                                AS gen_art_number,
                ISNULL(PA.CLR, '')                                               AS clr,
                ISNULL(SUM(PA.ALLOC_QTY), 0)                                     AS alloc_qty,
                ISNULL(SUM(PA.DO_QTY),    0)                                     AS do_qty,
                ISNULL(SUM(CASE WHEN PA.IS_CLOSED = 0
                                THEN PA.PEND_QTY ELSE 0 END), 0)                 AS pend_qty,
                COUNT(DISTINCT PA.ST_CD)                                         AS stores,
                COUNT(DISTINCT PA.ARTICLE_NUMBER)                                AS articles
            {from_sql}
            {where}
            GROUP BY PA.GEN_ART_NUMBER, PA.CLR
            ORDER BY ISNULL(SUM(PA.ALLOC_QTY), 0) DESC
        """), params).mappings().all()
        items = [{
            "name":            f"{r['gen_art_number']} · {r['clr'] or '—'}",
            "gen_art_number":  r["gen_art_number"],
            "clr":             r["clr"] or "",
            "alloc_qty":       int(r["alloc_qty"] or 0),
            "do_qty":          int(r["do_qty"]    or 0),
            "pend_qty":        int(r["pend_qty"]  or 0),
            "stores":          int(r["stores"]    or 0),
            "articles":        int(r["articles"]  or 0),
        } for r in rows]
    return APIResponse(success=True, data={"items": items})


@router.get("/drill/articles", response_model=APIResponse)
def drill_articles(request: Request, current_user: User = Depends(get_current_user)):
    """Article (variant) rollup. Scope must include enough to narrow to a single OPT."""
    scope = _parse_scope(request)
    engine = _engine()
    with engine.connect() as conn:
        if not _table_exists(conn, PEND_ALC):
            return APIResponse(success=True, data={"items": []})
        params: Dict = {}
        where = _where_pend(scope, params)
        needs_st, needs_prod = _needs_joins(scope)
        from_sql = _from_clause(needs_st, needs_prod)
        rows = conn.execute(text(f"""
            SELECT TOP 1000
                PA.ARTICLE_NUMBER                                                AS article_number,
                PA.MAJ_CAT, PA.GEN_ART_NUMBER, PA.CLR, PA.ST_CD, PA.RDC,
                ISNULL(SUM(PA.ALLOC_QTY), 0)                                     AS alloc_qty,
                ISNULL(SUM(PA.DO_QTY),    0)                                     AS do_qty,
                ISNULL(SUM(CASE WHEN PA.IS_CLOSED = 0
                                THEN PA.PEND_QTY ELSE 0 END), 0)                 AS pend_qty,
                MAX(PA.APPROVED_AT)                                              AS approved_at,
                MAX(PA.LAST_DO_AT)                                               AS last_do_at
            {from_sql}
            {where}
            GROUP BY PA.ARTICLE_NUMBER, PA.MAJ_CAT, PA.GEN_ART_NUMBER, PA.CLR, PA.ST_CD, PA.RDC
            ORDER BY ISNULL(SUM(PA.ALLOC_QTY), 0) DESC
        """), params).mappings().all()
        items = [{
            "article_number": r["article_number"],
            "maj_cat":        r["MAJ_CAT"],
            "gen_art_number": r["GEN_ART_NUMBER"],
            "clr":            r["CLR"],
            "st_cd":          r["ST_CD"],
            "rdc":            r["RDC"],
            "alloc_qty":      int(r["alloc_qty"] or 0),
            "do_qty":         int(r["do_qty"]    or 0),
            "pend_qty":       int(r["pend_qty"]  or 0),
            "approved_at":    r["approved_at"].isoformat() if r["approved_at"] else None,
            "last_do_at":     r["last_do_at"].isoformat()  if r["last_do_at"]  else None,
        } for r in rows]
    return APIResponse(success=True, data={"items": items})


# ---------------------------------------------------------------------------
# GET /pivot/maj-cat-rdc    — wide MAJ_CAT × RDC matrix (listing-page style)
# Returns:
#   { rdcs: [...sorted], items: [{ maj_cat, tot:{alloc,do,pend,...}, by_rdc:{RDC: {...}} }] }
# ---------------------------------------------------------------------------
@router.get("/pivot/maj-cat-rdc", response_model=APIResponse)
def pivot_maj_cat_rdc(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    scope = _parse_scope(request)
    engine = _engine()
    out = {"rdcs": [], "items": [], "totals": {}}
    with engine.connect() as conn:
        if not _table_exists(conn, PEND_ALC):
            return APIResponse(success=True, data=out)

        params: Dict = {}
        where = _where_pend(scope, params)
        needs_st, needs_prod = _needs_joins(scope)
        from_sql = _from_clause(needs_st, needs_prod)

        # Distinct RDCs that have data in scope
        rdc_rows = conn.execute(text(f"""
            SELECT DISTINCT PA.RDC
            {from_sql}
            {where}
            ORDER BY PA.RDC
        """), params).fetchall()
        rdcs = [str(r[0]).strip() for r in rdc_rows if r[0]]
        out["rdcs"] = rdcs

        # MAJ_CAT × RDC aggregate
        rows = conn.execute(text(f"""
            SELECT
                ISNULL(PA.MAJ_CAT, '—')                                          AS maj_cat,
                ISNULL(PA.RDC,     '—')                                          AS rdc,
                ISNULL(SUM(PA.ALLOC_QTY), 0)                                     AS alloc,
                ISNULL(SUM(PA.DO_QTY),    0)                                     AS do_qty,
                ISNULL(SUM(CASE WHEN PA.IS_CLOSED = 0
                                THEN PA.PEND_QTY ELSE 0 END), 0)                 AS pend,
                COUNT(DISTINCT PA.ST_CD)                                         AS stores,
                COUNT(DISTINCT PA.ARTICLE_NUMBER)                                AS articles
            {from_sql}
            {where}
            GROUP BY PA.MAJ_CAT, PA.RDC
        """), params).mappings().all()

        by_mc: Dict[str, Dict] = {}
        grand = {"alloc": 0, "do_qty": 0, "pend": 0, "stores_set": set(), "articles_set": set()}
        for r in rows:
            mc  = r["maj_cat"]
            rdc = r["rdc"]
            a   = int(r["alloc"]   or 0)
            d   = int(r["do_qty"]  or 0)
            p   = int(r["pend"]    or 0)
            st  = int(r["stores"]  or 0)
            ar  = int(r["articles"] or 0)
            row = by_mc.setdefault(mc, {
                "maj_cat": mc,
                "tot": {"alloc": 0, "do_qty": 0, "pend": 0, "stores": 0, "articles": 0, "pend_pct": 0, "fill_pct": 0},
                "by_rdc": {},
            })
            row["by_rdc"][rdc] = {
                "alloc": a, "do_qty": d, "pend": p, "stores": st, "articles": ar,
                "pend_pct": int(round(p / a * 100)) if a else 0,
                "fill_pct": int(round(d / a * 100)) if a else 0,
            }
            row["tot"]["alloc"]    += a
            row["tot"]["do_qty"]   += d
            row["tot"]["pend"]     += p
            row["tot"]["stores"]   += st       # may double-count if a store ships from multiple RDCs
            row["tot"]["articles"] += ar
            grand["alloc"] += a
            grand["do_qty"] += d
            grand["pend"] += p

        # Compute percentage on totals
        for row in by_mc.values():
            t = row["tot"]
            t["pend_pct"] = int(round(t["pend"] / t["alloc"] * 100)) if t["alloc"] else 0
            t["fill_pct"] = int(round(t["do_qty"] / t["alloc"] * 100)) if t["alloc"] else 0

        items = sorted(by_mc.values(), key=lambda x: -x["tot"]["alloc"])
        out["items"]  = items
        out["totals"] = {
            "alloc":    grand["alloc"],
            "do_qty":   grand["do_qty"],
            "pend":     grand["pend"],
            "pend_pct": int(round(grand["pend"] / grand["alloc"] * 100)) if grand["alloc"] else 0,
            "fill_pct": int(round(grand["do_qty"] / grand["alloc"] * 100)) if grand["alloc"] else 0,
        }
    return APIResponse(success=True, data=out)


# ---------------------------------------------------------------------------
# GET /drill/level   — generic flat rollup at a chosen dimension
#   ?dim=SEG|DIV|SUB_DIV|MAJ_CAT|ST_CD
# Honours all standard scope filters (including new parent crumbs seg, div,
# sub_div). Returns rows: { name, alloc_qty, do_qty, pend_qty, pend_pct,
# fill_pct, stores, articles, rows_n }.
# ---------------------------------------------------------------------------
_DRILL_DIM_MAP = {
    "SEG":     ("MP.SEG",     True),
    "DIV":     ("MP.DIV",     True),
    "SUB_DIV": ("MP.SUB_DIV", True),
    "MAJ_CAT": ("PA.MAJ_CAT", False),
    "ST_CD":   ("PA.ST_CD",   False),
}


@router.get("/drill/level", response_model=APIResponse)
def drill_level(
    request: Request,
    dim: str = Query(..., description="One of SEG, DIV, SUB_DIV, MAJ_CAT, ST_CD"),
    current_user: User = Depends(get_current_user),
):
    dim_up = (dim or "").strip().upper()
    if dim_up not in _DRILL_DIM_MAP:
        return APIResponse(success=False, data={"items": []},
                           message=f"Unsupported dim '{dim}'. Use one of {list(_DRILL_DIM_MAP)}")
    group_col, force_prod = _DRILL_DIM_MAP[dim_up]
    extra_select = ""
    # Stores benefit from HUB/status metadata (same shape as /drill/stores)
    if dim_up == "ST_CD":
        extra_select = "MIN(SM.ST_NM) AS st_nm, MIN(SM.HUB) AS hub, MIN(SM.ST_STATUS) AS status"

    scope = _parse_scope(request)
    engine = _engine()
    with engine.connect() as conn:
        if not _table_exists(conn, PEND_ALC):
            return APIResponse(success=True, data={"items": []})
        items = _drill_level(
            conn, scope, group_col,
            extra_select=extra_select,
            force_st=(dim_up == "ST_CD"),
            force_prod=force_prod,
        )
        # Derive %pend, %fill once on the server so the UI is dumb
        for r in items:
            a = int(r.get("alloc_qty") or 0)
            r["pend_pct"] = int(round((r.get("pend_qty") or 0) / a * 100)) if a else 0
            r["fill_pct"] = int(round((r.get("do_qty")   or 0) / a * 100)) if a else 0
    return APIResponse(success=True, data={"items": items, "dim": dim_up})


# ---------------------------------------------------------------------------
# GET /sessions/latest    — pick the most recent SESSION_ID
# Used by the Product Drill "Session Review" mode to auto-pick a session
# when the user hasn't selected one explicitly.
#
# source=pend_alc (default) — most recent session that has shipped allocations.
# source=review            — most recent session that has a PARKED/HISTORY
#                             listing snapshot (so a rich Session Review report
#                             is actually possible).
# ---------------------------------------------------------------------------
@router.get("/sessions/latest", response_model=APIResponse)
def get_latest_session(
    date: Optional[str] = Query(None, description="YYYY-MM-DD; defaults to latest date with data"),
    source: str = Query("pend_alc", description="pend_alc | review"),
    current_user: User = Depends(get_current_user),
):
    engine = _engine()
    with engine.connect() as conn:
        if source == "review":
            # Union HISTORY + PARKED, pick latest by APPROVED_AT/PARKED_AT.
            parts = []
            if _table_exists(conn, LIST_HISTORY):
                parts.append(f"""
                    SELECT SESSION_ID, MAX(APPROVED_AT) AS ts, 'history' AS src
                    FROM {LIST_HISTORY} WITH (NOLOCK)
                    GROUP BY SESSION_ID
                """)
            if _table_exists(conn, LIST_PARKED):
                parts.append(f"""
                    SELECT SESSION_ID, MAX(PARKED_AT) AS ts, 'parked' AS src
                    FROM {LIST_PARKED} WITH (NOLOCK)
                    GROUP BY SESSION_ID
                """)
            if not parts:
                return APIResponse(success=True, data=None)
            union_sql = " UNION ALL ".join(parts)
            params: Dict = {}
            date_filter = ""
            if date:
                params["p_date"] = date
                date_filter = " WHERE CAST(ts AS DATE) = :p_date"
            row = conn.execute(text(f"""
                SELECT TOP 1 SESSION_ID, ts AS approved_at, src
                FROM ({union_sql}) u {date_filter}
                ORDER BY ts DESC
            """), params).mappings().first()
            if not row:
                return APIResponse(success=True, data=None)
            return APIResponse(success=True, data={
                "session_id":  row["SESSION_ID"],
                "approved_at": row["approved_at"].isoformat() if row["approved_at"] else None,
                "source":      row["src"],
                "date":        date,
            })

        # Default: PEND_ALC (the legacy behaviour)
        if not _table_exists(conn, PEND_ALC):
            return APIResponse(success=True, data=None)
        params: Dict = {}
        if date:
            params["p_date"] = date
            row = conn.execute(text(f"""
                SELECT TOP 1 SESSION_ID, MAX(APPROVED_AT) AS approved_at
                FROM {PEND_ALC} WITH (NOLOCK)
                WHERE CAST(APPROVED_AT AS DATE) = :p_date
                GROUP BY SESSION_ID
                ORDER BY MAX(APPROVED_AT) DESC
            """), params).mappings().first()
        else:
            row = conn.execute(text(f"""
                SELECT TOP 1 SESSION_ID, MAX(APPROVED_AT) AS approved_at
                FROM {PEND_ALC} WITH (NOLOCK)
                GROUP BY SESSION_ID
                ORDER BY MAX(APPROVED_AT) DESC
            """)).mappings().first()
        if not row:
            return APIResponse(success=True, data=None)
        return APIResponse(success=True, data={
            "session_id":  row["SESSION_ID"],
            "approved_at": row["approved_at"].isoformat() if row["approved_at"] else None,
            "source":      "pend_alc",
            "date":        date,
        })


# ---------------------------------------------------------------------------
# GET /sessions/review-list   — every SESSION_ID that has a PARKED or HISTORY
# listing snapshot, newest first. Drives the Session dropdown when the user
# is in Session Review mode (PEND_ALC sessions are excluded — they cannot be
# rendered with the rich grid).
# ---------------------------------------------------------------------------
@router.get("/sessions/review-list", response_model=APIResponse)
def list_review_sessions(
    date: Optional[str] = Query(None, description="YYYY-MM-DD; exact match (overrides from/to)"),
    from_: Optional[str] = Query(None, alias="from", description="YYYY-MM-DD lower bound"),
    to:    Optional[str] = Query(None, description="YYYY-MM-DD upper bound (inclusive)"),
    src:   Optional[str] = Query(None, description="parked | history | all (default all)"),
    current_user: User = Depends(get_current_user),
):
    engine = _engine()
    items: List[Dict] = []
    src_filter = (src or "all").strip().lower()
    with engine.connect() as conn:
        parts = []
        if src_filter in ("all", "history") and _table_exists(conn, LIST_HISTORY):
            parts.append(f"""
                SELECT SESSION_ID, MAX(APPROVED_AT) AS ts, 'history' AS src
                FROM {LIST_HISTORY} WITH (NOLOCK)
                GROUP BY SESSION_ID
            """)
        if src_filter in ("all", "parked") and _table_exists(conn, LIST_PARKED):
            parts.append(f"""
                SELECT SESSION_ID, MAX(PARKED_AT) AS ts, 'parked' AS src
                FROM {LIST_PARKED} WITH (NOLOCK)
                GROUP BY SESSION_ID
            """)
        if not parts:
            return APIResponse(success=True, data={"items": []})

        params: Dict = {}
        where_parts: List[str] = []
        if date:
            params["p_date"] = date
            where_parts.append("CAST(ts AS DATE) = :p_date")
        else:
            if from_:
                params["p_from"] = from_
                where_parts.append("CAST(ts AS DATE) >= :p_from")
            if to:
                params["p_to"] = to
                where_parts.append("CAST(ts AS DATE) <= :p_to")
        date_filter = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

        rows = conn.execute(text(f"""
            SELECT SESSION_ID, MAX(ts) AS ts, MAX(src) AS src
            FROM ({' UNION ALL '.join(parts)}) u {date_filter}
            GROUP BY SESSION_ID
            ORDER BY MAX(ts) DESC
        """), params).mappings().all()
        items = [{
            "session_id": r["SESSION_ID"],
            "ts":         r["ts"].isoformat() if r["ts"] else None,
            "src":        r["src"],
            # Format a friendly label for the dropdown
            "label":      f"{r['SESSION_ID']} · {r['src'].upper()}",
        } for r in rows]
    return APIResponse(success=True, data={"items": items})


# ---------------------------------------------------------------------------
# GET /session-review    — rich MAJ_CAT × RDC pivot for one SESSION_ID, with
# MBQ/STOCK/STORE_STK/EXCESS_STK/REQ/ALLOC/HOLD/derived.
#
# Source cascade:
#   1. ARS_LISTING_WORKING_HISTORY  (approved sessions)
#   2. ARS_LISTING_WORKING_PARKED   (parked sessions)
#   3. ARS_PEND_ALC                 (fallback — reduced columns only)
#
# Aggregation (matches listing.py /summary semantics):
#   MJ_* (MBQ, STK_TTL, REQ) deduped per (WERKS, MAJ_CAT, RDC) with MAX,
#     then SUM across WERKS to MAJ_CAT × RDC.
#   ALLOC_QTY / HOLD_QTY / STK_TTL (option-grain stock) / EXCESS_STK SUM directly.
# ---------------------------------------------------------------------------
def _session_review_msa_stock(conn, table: str, sid: str, dim: str, crumb: Dict) -> Dict:
    """RDC-grain MSA stock keyed by (dim_value, RDC).
       Reads ARS_MSA_GEN_ART_PARKED/_HISTORY which carries SEG/DIV/SUB_DIV/MAJ_CAT/RDC
       natively, so it can be grouped at any of the listing-dim levels without joins.
       For dim=ST_CD: returns empty (RDC stock isn't store-specific).
       For dim=GEN_ART: groups by GEN_ART · CLR.
       For dim=ARTICLE: returns empty (article-level stock comes from MSA_VAR_*).
    """
    if not _table_exists(conn, table):
        return {}
    if dim in ("ST_CD", "ARTICLE"):
        return {}

    dim_expr_map = {
        "SEG":     "ISNULL(SEG, '—')",
        "DIV":     "ISNULL(DIV, '—')",
        "SUB_DIV": "ISNULL(SUB_DIV, '—')",
        "MAJ_CAT": "ISNULL(MAJ_CAT, '—')",
        "GEN_ART": "CAST(GEN_ART_NUMBER AS NVARCHAR(50)) + ' · ' + ISNULL(CLR, '')",
    }
    dim_expr = dim_expr_map.get(dim, "ISNULL(MAJ_CAT, '—')")

    params: Dict = {"sid": sid}
    parts: List[str] = ["SESSION_ID = :sid"]
    for col, key in (("MAJ_CAT", "mc"), ("SEG", "seg"), ("DIV", "div"), ("SUB_DIV", "sub_div")):
        vals = crumb.get(key) or []
        if vals:
            ph = []
            for i, v in enumerate(vals):
                k = f"m{key}{i}"
                params[k] = v
                ph.append(f":{k}")
            parts.append(f"{col} IN ({','.join(ph)})")
    if crumb.get("gen_art"):
        params["p_genart"] = crumb["gen_art"]
        parts.append("GEN_ART_NUMBER = :p_genart")
    if crumb.get("clr"):
        params["p_clr"] = crumb["clr"]
        parts.append("CLR = :p_clr")
    where_sql = " WHERE " + " AND ".join(parts)

    sql = f"""
        SELECT {dim_expr} AS dim_val, RDC,
               SUM(ISNULL(FNL_Q,    0)) AS stock_fnl,
               SUM(ISNULL(STK_QTY,  0)) AS stock_raw
        FROM {table} WITH (NOLOCK)
        {where_sql}
        GROUP BY {dim_expr}, RDC
    """
    rows = conn.execute(text(sql), params).mappings().all()
    out = {}
    for r in rows:
        key = (str(r["dim_val"] or "—").strip(), str(r["RDC"] or "—").strip())
        out[key] = {
            "stock_fnl": int(r["stock_fnl"] or 0),
            "stock_raw": int(r["stock_raw"] or 0),
        }
    return out


# Per-dim aggregation configuration. Each entry tells us:
#   sql_dim_expr  → expression used as the grouping value (alias 'dim_val')
#   needs_mp_join → whether VW_MASTER_PRODUCT must be joined
#   level_kind    → 'mj' = MAJ_CAT-grain MBQ/REQ apply
#                   'store' = WERKS-grain; uses MJ_* deduped per (WERKS,MAJ,RDC) then summed
#                   'opt' = GEN_ART_NUMBER · CLR grain; uses OPT_MBQ, STK_TTL, OPT_REQ
#                   'article' = ARTICLE_NUMBER grain; sourced from MSA_VAR_*
_SR_DIM_CONFIG = {
    "SEG":     {"expr": "ISNULL(MP.SEG, '—')",     "needs_mp": True,  "kind": "mj"},
    "DIV":     {"expr": "ISNULL(MP.DIV, '—')",     "needs_mp": True,  "kind": "mj"},
    "SUB_DIV": {"expr": "ISNULL(MP.SUB_DIV, '—')", "needs_mp": True,  "kind": "mj"},
    "MAJ_CAT": {"expr": "ISNULL(L.MAJ_CAT, '—')",  "needs_mp": False, "kind": "mj"},
    "ST_CD":   {"expr": "ISNULL(L.WERKS, '—')",    "needs_mp": False, "kind": "store"},
    "GEN_ART": {"expr": "CAST(L.GEN_ART_NUMBER AS NVARCHAR(50)) + ' · ' + ISNULL(L.CLR, '')", "needs_mp": False, "kind": "opt"},
    "ARTICLE": {"expr": "VAR_ART",                 "needs_mp": False, "kind": "article"},
    # Grid-dim review — every sec-cap grid (column with matching *_MBQ /
    # *_STK_TTL / *_REQ rollups on the listing table). Same aggregation
    # pattern as MAJ_CAT (OPT-grain SUM, no MJ_* dedup needed).
    "FAB":           {"expr": "ISNULL(L.FAB, '—')",           "needs_mp": False, "kind": "grid"},
    "MACRO_MVGR":    {"expr": "ISNULL(L.MACRO_MVGR, '—')",    "needs_mp": False, "kind": "grid"},
    "MICRO_MVGR":    {"expr": "ISNULL(L.MICRO_MVGR, '—')",    "needs_mp": False, "kind": "grid"},
    "RNG_SEG":       {"expr": "ISNULL(L.RNG_SEG, '—')",       "needs_mp": False, "kind": "grid"},
    "MERGE_RNG_SEG": {"expr": "ISNULL(L.MERGE_RNG_SEG, '—')", "needs_mp": False, "kind": "grid"},
    "M_VND_CD":      {"expr": "ISNULL(L.M_VND_CD, '—')",      "needs_mp": False, "kind": "grid"},
    "M_YARN_02":     {"expr": "ISNULL(L.M_YARN_02, '—')",     "needs_mp": False, "kind": "grid"},
    "WEAVE_2":       {"expr": "ISNULL(L.WEAVE_2, '—')",       "needs_mp": False, "kind": "grid"},
    "CLR":           {"expr": "ISNULL(L.CLR, '—')",           "needs_mp": False, "kind": "grid"},
}


def _sr_crumb_where(crumb: Dict, params: Dict, listing_alias: str = "L", mp_alias: str = "MP"):
    """Build WHERE-fragments for the session-review SQL based on crumb filters.
    Returns (where_clause_str_without_WHERE, needs_mp_for_filters).
    """
    parts: List[str] = []
    needs_mp = False
    # Listing-native filters
    for col, key in (("MAJ_CAT", "mc"), ("WERKS", "werks")):
        vals = crumb.get(key) or []
        if vals:
            ph = []
            for i, v in enumerate(vals):
                k = f"{key}{i}"
                params[k] = v
                ph.append(f":{k}")
            parts.append(f"{listing_alias}.{col} IN ({','.join(ph)})")
    # Drill single-value filters
    if crumb.get("gen_art"):
        params["p_genart"] = crumb["gen_art"]
        parts.append(f"{listing_alias}.GEN_ART_NUMBER = :p_genart")
    if crumb.get("clr"):
        params["p_clr"] = crumb["clr"]
        parts.append(f"{listing_alias}.CLR = :p_clr")
    # VW_MASTER_PRODUCT-joined filters
    for col, key in (("SEG", "seg"), ("DIV", "div"), ("SUB_DIV", "sub_div")):
        vals = crumb.get(key) or []
        if vals:
            ph = []
            for i, v in enumerate(vals):
                k = f"{key}{i}"
                params[k] = v
                ph.append(f":{k}")
            parts.append(f"{mp_alias}.{col} IN ({','.join(ph)})")
            needs_mp = True
    return (" AND ".join(parts), needs_mp)


def _session_review_from_listing(conn, table: str, sid: str, dim: str, crumb: Dict):
    """Aggregate one of the listing archives at the requested dim × RDC grain.

    EXCESS_STK is sourced from the snapshot listing table (ARS_LISTING_PARKED /
    ARS_LISTING_HISTORY), NOT from the working table — the working table's
    EXCESS_STK column is zeroed out during allocation. The snapshot CTE is
    joined at OPT grain (WERKS, GEN_ART, CLR, RDC) so any crumb filter on L
    (WERKS, MAJ_CAT, GEN_ART, CLR) naturally narrows the excess sum.

    Returns rows with: dim_val, RDC, mbq_qty, store_stk, req_qty, alloc_qty,
    hold_qty, excess_stk, st_nm/hub (store kind), opt_type/rank/seq/status/
    remarks (opt kind).
    """
    cfg = _SR_DIM_CONFIG[dim]
    dim_expr = cfg["expr"]
    kind = cfg["kind"]
    needs_mp = cfg["needs_mp"]

    params: Dict = {"sid": sid}
    crumb_where, crumb_needs_mp = _sr_crumb_where(crumb, params)
    needs_mp = needs_mp or crumb_needs_mp

    mp_join = ""
    if needs_mp:
        mp_join = f" LEFT JOIN {PROD_VIEW} MP WITH (NOLOCK) ON MP.GEN_ART_NUMBER = L.GEN_ART_NUMBER"

    extra_where = f" AND {crumb_where}" if crumb_where else ""

    # EXCESS_STK is sourced separately (see _session_review_snapshot_excess) because
    # ARS_LISTING_PARKED and ARS_LISTING_WORKING_PARKED have disjoint GEN_ART sets —
    # joining them would silently drop excess rows. Listing SQL emits 0 here; the
    # orchestrator overwrites each cell's excess_stk after both fetches complete.
    if kind == "mj":
        # MJ_* are MAJ_CAT+WERKS aggregates → dedup per (WERKS, MAJ_CAT, RDC) with MAX,
        # then SUM up to the requested dim × RDC. This works for SEG/DIV/SUB_DIV/MAJ_CAT.
        sql = f"""
        WITH src_mj AS (
            SELECT {dim_expr} AS dim_val,
                   L.WERKS, L.MAJ_CAT, L.RDC,
                   MAX(ISNULL(L.MJ_MBQ, 0))     AS mj_mbq,
                   MAX(ISNULL(L.MJ_STK_TTL, 0)) AS mj_stk_ttl,
                   MAX(ISNULL(L.MJ_REQ, 0))     AS mj_req
            FROM {table} L WITH (NOLOCK){mp_join}
            WHERE L.SESSION_ID = :sid{extra_where}
            GROUP BY {dim_expr}, L.WERKS, L.MAJ_CAT, L.RDC
        ),
        rolled AS (
            SELECT dim_val, RDC,
                   SUM(mj_mbq)     AS mbq_qty,
                   SUM(mj_stk_ttl) AS store_stk,
                   SUM(mj_req)     AS req_qty
            FROM src_mj
            GROUP BY dim_val, RDC
        ),
        opt AS (
            SELECT {dim_expr} AS dim_val, L.RDC,
                   SUM(ISNULL(L.ALLOC_QTY, 0))   AS alloc_qty,
                   SUM(ISNULL(L.HOLD_QTY,  0))   AS hold_qty
            FROM {table} L WITH (NOLOCK){mp_join}
            WHERE L.SESSION_ID = :sid{extra_where}
            GROUP BY {dim_expr}, L.RDC
        )
        SELECT r.dim_val, r.RDC,
               r.mbq_qty, r.store_stk, r.req_qty,
               ISNULL(o.alloc_qty,  0) AS alloc_qty,
               ISNULL(o.hold_qty,   0) AS hold_qty,
               0                      AS excess_stk   /* filled in by orchestrator from snapshot */
        FROM rolled r LEFT JOIN opt o ON o.dim_val = r.dim_val AND o.RDC = r.RDC
        """
    elif kind == "store":
        # Group by WERKS × RDC. MJ_* deduped per (WERKS, MAJ_CAT, RDC) then summed
        # across MAJ_CATs within the WERKS. Join ST_MASTER for ST_NM/HUB display.
        sql = f"""
        WITH src_mj AS (
            SELECT L.WERKS, L.MAJ_CAT, L.RDC,
                   MAX(ISNULL(L.MJ_MBQ, 0))     AS mj_mbq,
                   MAX(ISNULL(L.MJ_STK_TTL, 0)) AS mj_stk_ttl,
                   MAX(ISNULL(L.MJ_REQ, 0))     AS mj_req
            FROM {table} L WITH (NOLOCK){mp_join}
            WHERE L.SESSION_ID = :sid{extra_where}
            GROUP BY L.WERKS, L.MAJ_CAT, L.RDC
        ),
        rolled AS (
            SELECT WERKS AS dim_val, RDC,
                   SUM(mj_mbq)     AS mbq_qty,
                   SUM(mj_stk_ttl) AS store_stk,
                   SUM(mj_req)     AS req_qty
            FROM src_mj
            GROUP BY WERKS, RDC
        ),
        opt AS (
            SELECT L.WERKS AS dim_val, L.RDC,
                   SUM(ISNULL(L.ALLOC_QTY, 0))   AS alloc_qty,
                   SUM(ISNULL(L.HOLD_QTY,  0))   AS hold_qty
            FROM {table} L WITH (NOLOCK){mp_join}
            WHERE L.SESSION_ID = :sid{extra_where}
            GROUP BY L.WERKS, L.RDC
        )
        SELECT r.dim_val, r.RDC,
               r.mbq_qty, r.store_stk, r.req_qty,
               ISNULL(o.alloc_qty,  0) AS alloc_qty,
               ISNULL(o.hold_qty,   0) AS hold_qty,
               0                      AS excess_stk,  /* filled in by orchestrator */
               SM.ST_NM AS st_nm,
               SM.HUB   AS hub
        FROM rolled r
        LEFT JOIN opt o ON o.dim_val = r.dim_val AND o.RDC = r.RDC
        LEFT JOIN {ST_MASTER} SM WITH (NOLOCK) ON SM.ST_CD = r.dim_val
        """
    elif kind == "opt":
        # OPT-grain: GEN_ART · CLR × RDC. Use OPT_MBQ, STK_TTL (option stock), OPT_REQ.
        # I_ROD: per-store rod allowance from listing → "planned" = SUM across
        # stores in scope; "used" = SUM only for stores that actually got
        # allocation (ALLOC_QTY > 0). ALLOC_WAVE lives on ARS_ALLOC_PARKED, so
        # we join via a CTE.
        snapshot_alloc = ALLOC_HISTORY if table == LIST_HISTORY else ALLOC_PARKED
        sql = f"""
        WITH wave_cte AS (
            SELECT WERKS, MAJ_CAT, RDC, GEN_ART_NUMBER, ISNULL(CLR,'') AS CLR,
                   MAX(ALLOC_WAVE) AS alloc_wave
            FROM {snapshot_alloc} WITH (NOLOCK)
            WHERE SESSION_ID = :sid AND ALLOC_WAVE IS NOT NULL AND ALLOC_WAVE <> ''
            GROUP BY WERKS, MAJ_CAT, RDC, GEN_ART_NUMBER, ISNULL(CLR,'')
        )
        SELECT {dim_expr} AS dim_val, L.RDC,
               SUM(ISNULL(L.OPT_MBQ,  0))       AS mbq_qty,
               SUM(ISNULL(L.STK_TTL,  0))       AS store_stk,
               SUM(ISNULL(L.OPT_REQ,  0))       AS req_qty,
               SUM(ISNULL(L.ALLOC_QTY,0))       AS alloc_qty,
               SUM(ISNULL(L.HOLD_QTY, 0))       AS hold_qty,
               0                                AS excess_stk,  /* filled in by orchestrator */
               MAX(L.GEN_ART_NUMBER)            AS gen_art_number,
               MAX(L.CLR)                       AS clr,
               MAX(L.OPT_TYPE)                  AS opt_type,
               MIN(L.OPT_PRIORITY_RANK)         AS opt_priority_rank,
               MIN(L.ALLOC_SEQ)                 AS alloc_seq,
               MAX(L.ALLOC_STATUS)              AS alloc_status,
               MAX(L.ALLOC_REMARKS)             AS alloc_remarks,
               -- I_ROD planned vs used (rods at stores that actually got allocation)
               SUM(ISNULL(L.I_ROD, 0))                                              AS i_rod_planned,
               SUM(CASE WHEN ISNULL(L.ALLOC_QTY,0) > 0 THEN ISNULL(L.I_ROD,0) ELSE 0 END) AS i_rod_used,
               MAX(w.alloc_wave)                AS alloc_wave
        FROM {table} L WITH (NOLOCK){mp_join}
        LEFT JOIN wave_cte w
               ON w.WERKS = L.WERKS
              AND w.MAJ_CAT = L.MAJ_CAT
              AND w.GEN_ART_NUMBER = L.GEN_ART_NUMBER
              AND w.CLR = ISNULL(L.CLR, '')
              AND w.RDC = L.RDC
        WHERE L.SESSION_ID = :sid{extra_where}
        GROUP BY {dim_expr}, L.RDC
        """
    elif kind == "grid":
        # Grid-dim review (FAB / MACRO_MVGR / MICRO_MVGR / RNG_SEG / M_VND_CD).
        # Same OPT-grain SUMs as MAJ_CAT but grouped by the sec-cap grid column.
        sql = f"""
        SELECT {dim_expr} AS dim_val, L.RDC,
               SUM(ISNULL(L.OPT_MBQ,   0))      AS mbq_qty,
               SUM(ISNULL(L.STK_TTL,   0))      AS store_stk,
               SUM(ISNULL(L.OPT_REQ,   0))      AS req_qty,
               SUM(ISNULL(L.ALLOC_QTY, 0))      AS alloc_qty,
               SUM(ISNULL(L.HOLD_QTY,  0))      AS hold_qty,
               0                                AS excess_stk  /* filled in by orchestrator */
        FROM {table} L WITH (NOLOCK){mp_join}
        WHERE L.SESSION_ID = :sid{extra_where}
        GROUP BY {dim_expr}, L.RDC
        """
    else:
        # article level handled separately (different source table)
        return []

    return conn.execute(text(sql), params).mappings().all()


def _session_review_snapshot_excess(conn, snapshot_tbl: str, sid: str,
                                    dim: str, crumb: Dict) -> Dict:
    """Aggregate EXCESS_STK from ARS_LISTING_PARKED / ARS_LISTING_HISTORY at the
    requested dim × RDC grain, applying the same crumb filters as the listing
    query. The snapshot table has *different* OPT rows than the working table
    (snapshot rows include parked-out OPTs that don't make it to the working
    pipeline), so we aggregate it independently and merge after-the-fact.

    Returns: { (dim_val_str, rdc_str): excess_stk_int }
    """
    if not _table_exists(conn, snapshot_tbl) or dim == "ARTICLE":
        return {}

    cfg = _SR_DIM_CONFIG[dim]
    # Use the same dim_expr as the listing query (it already references the
    # listing-table alias 'L', which we re-use here for the snapshot).
    dim_expr = cfg["expr"]
    needs_mp = cfg["needs_mp"]

    params: Dict = {"sid": sid}
    crumb_where, crumb_needs_mp = _sr_crumb_where(crumb, params)
    needs_mp = needs_mp or crumb_needs_mp

    mp_join = ""
    if needs_mp:
        mp_join = f" LEFT JOIN {PROD_VIEW} MP WITH (NOLOCK) ON MP.GEN_ART_NUMBER = L.GEN_ART_NUMBER"

    extra_where = f" AND {crumb_where}" if crumb_where else ""

    sql = f"""
    SELECT {dim_expr} AS dim_val, L.RDC,
           SUM(ISNULL(L.EXCESS_STK, 0)) AS excess_stk
    FROM {snapshot_tbl} L WITH (NOLOCK){mp_join}
    WHERE L.SESSION_ID = :sid{extra_where}
    GROUP BY {dim_expr}, L.RDC
    """
    rows = conn.execute(text(sql), params).mappings().all()
    out = {}
    for r in rows:
        key = (str(r["dim_val"] or "—").strip(), str(r["RDC"] or "—").strip())
        out[key] = int(r["excess_stk"] or 0)
    return out


def _session_review_from_alloc(conn, table: str, sid: str, crumb: Dict):
    """Article-grain rollup at VAR_ART × SZ from ARS_ALLOC_PARKED/_HISTORY.
    Returns one row per (VAR_ART, SZ) — the leaf of the drill, matching the
    listing-page reference report. Carries SZ_MBQ/SZ_STK/SZ_REQ/FNL_Q/SHIP/
    FROM_HOLD/HOLD/ALLOC/STATUS/REASON/BAND_TRACE.
    """
    params: Dict = {"sid": sid}
    parts: List[str] = ["SESSION_ID = :sid"]
    # Listing-native filters apply directly (same column names on ARS_ALLOC_*)
    for col, key in (("MAJ_CAT", "mc"), ("WERKS", "werks"), ("RDC", "rdc")):
        vals = crumb.get(key) or []
        if vals:
            ph = []
            for i, v in enumerate(vals):
                k = f"{key}{i}"
                params[k] = v
                ph.append(f":{k}")
            parts.append(f"{col} IN ({','.join(ph)})")
    if crumb.get("gen_art"):
        params["p_genart"] = crumb["gen_art"]
        parts.append("GEN_ART_NUMBER = :p_genart")
    if crumb.get("clr"):
        params["p_clr"] = crumb["clr"]
        parts.append("CLR = :p_clr")
    where_sql = " WHERE " + " AND ".join(parts)
    # Aggregate per (VAR_ART, SZ, RDC). Across multiple WERKS (when no WERKS
    # crumb is set), we sum the quantities. CONT / PAK_SZ are size-level
    # attributes — MAX picks one (identical across WERKS for the same VAR_ART/SZ).
    sql = f"""
    SELECT VAR_ART                    AS dim_val,
           SZ                         AS sz,
           RDC,
           MAX(ISNULL(CONT,    0))    AS cont,
           MAX(ISNULL(PAK_SZ,  0))    AS pak_sz,
           SUM(ISNULL(SZ_MBQ,  0))    AS sz_mbq,
           SUM(ISNULL(SZ_STK,  0))    AS sz_stk,
           SUM(ISNULL(SZ_REQ,  0))    AS sz_req,
           SUM(ISNULL(FNL_Q,   0))    AS fnl_q,
           SUM(ISNULL(SHIP_QTY,0))    AS ship_qty,
           SUM(ISNULL(FROM_HOLD_QTY,0)) AS from_hold_qty,
           SUM(ISNULL(HOLD_QTY,0))    AS hold_qty,
           SUM(ISNULL(ALLOC_QTY,0))   AS alloc_qty,
           MAX(ALLOC_STATUS)          AS alloc_status,
           MAX(ALLOC_REMARKS)         AS alloc_remarks,
           MAX(SKIP_REASON)           AS skip_reason
    FROM {table} WITH (NOLOCK)
    {where_sql}
    GROUP BY VAR_ART, SZ, RDC
    ORDER BY VAR_ART, SZ
    """
    return conn.execute(text(sql), params).mappings().all()


def _parse_sr_crumb(request: Request) -> Dict:
    """Pull crumb filters from the session-review query string. Each is csv."""
    qp = request.query_params
    def _csv(key):
        raw = qp.get(key) or ""
        return [s.strip() for s in raw.split(",") if s.strip()]
    return {
        "seg":     _csv("seg"),
        "div":     _csv("div"),
        "sub_div": _csv("sub_div"),
        "mc":      _csv("mc"),
        "werks":   _csv("werks"),
        "rdc":     _csv("rdc"),
        "gen_art": qp.get("gen_art") or None,
        "clr":     qp.get("clr")     or None,
    }


@router.get("/session-review", response_model=APIResponse)
def session_review(
    request: Request,
    sid: str = Query(..., description="SESSION_ID to review"),
    dim: str = Query("MAJ_CAT", description="SEG|DIV|SUB_DIV|MAJ_CAT|ST_CD|GEN_ART|ARTICLE"),
    current_user: User = Depends(get_current_user),
):
    dim_up = (dim or "MAJ_CAT").strip().upper()
    if dim_up not in _SR_DIM_CONFIG:
        return APIResponse(success=False, data={},
                           message=f"Unsupported dim '{dim}'. Use one of {list(_SR_DIM_CONFIG)}")
    crumb = _parse_sr_crumb(request)
    engine = _engine()
    out = {"source": None, "session_id": sid, "dim": dim_up,
           "rdcs": [], "items": [], "totals": {}}

    with engine.connect() as conn:
        rows = []
        msa_stock: Dict = {}
        excess_map: Dict = {}     # NEW: snapshot-sourced EXCESS_STK keyed by (dim_val, RDC)
        source = None

        if dim_up == "ARTICLE":
            # Size-grain (VAR_ART × SZ) from ARS_ALLOC_PARKED/HISTORY — leaf of the drill.
            if _table_exists(conn, ALLOC_HISTORY):
                rows = _session_review_from_alloc(conn, ALLOC_HISTORY, sid, crumb)
                if rows: source = "history"
            if not rows and _table_exists(conn, ALLOC_PARKED):
                rows = _session_review_from_alloc(conn, ALLOC_PARKED, sid, crumb)
                if rows: source = "parked"
        else:
            # SEG/DIV/SUB_DIV/MAJ_CAT/ST_CD/GEN_ART — listing archive (MBQ/REQ/ALLOC/HOLD)
            # + MSA stock (FNL_Q) + snapshot listing (EXCESS_STK, sourced separately
            # because snapshot and working tables have disjoint GEN_ART sets).
            if _table_exists(conn, LIST_HISTORY):
                rows = _session_review_from_listing(conn, LIST_HISTORY, sid, dim_up, crumb)
                if rows:
                    source = "history"
                    msa_stock  = _session_review_msa_stock(conn, MSA_HISTORY, sid, dim_up, crumb)
                    excess_map = _session_review_snapshot_excess(conn, LIST_SNAPSHOT_HISTORY, sid, dim_up, crumb)
            if not rows and _table_exists(conn, LIST_PARKED):
                rows = _session_review_from_listing(conn, LIST_PARKED, sid, dim_up, crumb)
                if rows:
                    source = "parked"
                    msa_stock  = _session_review_msa_stock(conn, MSA_PARKED, sid, dim_up, crumb)
                    excess_map = _session_review_snapshot_excess(conn, LIST_SNAPSHOT_PARKED, sid, dim_up, crumb)

        if not rows:
            return APIResponse(success=True, data=out,
                               message=f"Session {sid} has no parked/history snapshot")

        out["source"]    = source
        out["msa_found"] = bool(msa_stock)

        # ─── ARTICLE level returns a flat list of (VAR_ART, SZ) rows. The
        # leaf view uses a dedicated columnset; no pivot/by_rdc bucketing. ───
        if dim_up == "ARTICLE":
            items = []
            tot = {"sz_mbq": 0, "sz_stk": 0, "sz_req": 0, "fnl_q": 0,
                   "ship": 0, "from_hold": 0, "hold": 0, "alloc": 0}
            for r in rows:
                var_art = str(r.get("dim_val") or "—").strip()
                sz      = str(r.get("sz") or "—").strip()
                rdc     = str(r.get("RDC") or "—").strip()
                remarks = r.get("alloc_remarks") or ""
                # Pull the "B[<TIER>.r<round>.rk<rank>]" prefix from remarks as band_trace.
                import re as _re
                m = _re.match(r"^(B\[[^\]]+\])", remarks)
                band_trace = m.group(1) if m else ""
                sz_mbq     = int(r.get("sz_mbq")    or 0)
                sz_stk     = int(r.get("sz_stk")    or 0)
                sz_req     = int(r.get("sz_req")    or 0)
                fnl_q      = int(r.get("fnl_q")     or 0)
                ship       = int(r.get("ship_qty")  or 0)
                from_hold  = int(r.get("from_hold_qty") or 0)
                hold       = int(r.get("hold_qty")  or 0)
                alloc      = int(r.get("alloc_qty") or 0)
                msa_rem    = max(0, fnl_q - alloc - hold)
                items.append({
                    "var_art":   var_art,
                    "sz":        sz,
                    "rdc":       rdc,
                    "cont":      float(r.get("cont")   or 0),
                    "pak_sz":    float(r.get("pak_sz") or 0),
                    "sz_mbq":    sz_mbq,
                    "sz_stk":    sz_stk,
                    "sz_req":    sz_req,
                    "fnl_q":     fnl_q,
                    "msa_rem":   msa_rem,
                    "ship":      ship,
                    "from_hold": from_hold,
                    "hold":      hold,
                    "alloc":     alloc,
                    "status":    r.get("alloc_status") or "",
                    "reason":    r.get("skip_reason")  or "",
                    "remarks":   remarks,
                    "band_trace": band_trace,
                })
                tot["sz_mbq"]    += sz_mbq
                tot["sz_stk"]    += sz_stk
                tot["sz_req"]    += sz_req
                tot["fnl_q"]     += fnl_q
                tot["ship"]      += ship
                tot["from_hold"] += from_hold
                tot["hold"]      += hold
                tot["alloc"]     += alloc
            out["items"]  = items
            out["totals"] = tot
            out["rdcs"]   = []  # not pivoted by RDC at this level
            return APIResponse(success=True, data=out)

        # ─── All other dims (mj/store/opt/grid): bucket into rdcs[] + items[]. ───
        # Each item is { key, by_rdc, tot }. The 'maj_cat' alias is preserved
        # for frontend compatibility.
        rdcs_set = set()
        by_key: Dict[str, Dict] = {}
        grand = {"mbq": 0, "stock": 0, "store_stk": 0, "excess_stk": 0,
                 "req": 0, "alloc": 0, "hold": 0}

        for r in rows:
            key_val = str(r.get("dim_val") or "—").strip()
            rdc = str(r["RDC"] or "—").strip()
            rdcs_set.add(rdc)

            # STOCK column source:
            #   - SEG/DIV/SUB_DIV/MAJ_CAT/GEN_ART: from MSA aggregated at same dim grain
            #   - ST_CD: STOCK = 0 (RDC stock isn't store-specific)
            #   - ARTICLE: stock_avail is on the MSA_VAR row itself
            stock_val = 0
            if dim_up == "ARTICLE":
                stock_val = int(r.get("stock_avail") or 0)
            else:
                msa = msa_stock.get((key_val, rdc), {})
                stock_val = int(msa.get("stock_fnl") or 0)

            # EXCESS_STK comes from the snapshot table (independent aggregation).
            # Falls back to whatever the listing query emitted (0 in the new SQL)
            # when there's no snapshot entry for this (dim, rdc).
            excess_val = excess_map.get((key_val, rdc))
            if excess_val is None:
                excess_val = int(r.get("excess_stk") or 0)
            cell = {
                "mbq":        int(r.get("mbq_qty")    or 0),
                "stock":      stock_val,
                "store_stk":  int(r.get("store_stk")  or 0),
                "excess_stk": int(excess_val),
                "req":        int(r.get("req_qty")    or 0),
                "alloc":      int(r.get("alloc_qty")  or 0),
                "hold":       int(r.get("hold_qty")   or 0),
            }
            cell["req_rem"]  = max(0, cell["req"] - cell["alloc"])
            cell["msa_rem"]  = max(0, cell["store_stk"] - cell["alloc"] - cell["hold"])
            cell["req_pct"]  = round(cell["alloc"] / cell["req"] * 100, 1)  if cell["req"]   else 0.0
            cell["fill_pct"] = round((cell["store_stk"] + cell["alloc"]) / cell["mbq"] * 100, 1) if cell["mbq"]   else 0.0
            cell["stk_pct"]  = round(cell["alloc"] / cell["stock"] * 100, 1) if cell["stock"] else 0.0

            row = by_key.setdefault(key_val, {
                "key": key_val,
                # Legacy alias for frontend (SessionReviewGrid was written
                # against MAJ_CAT-grain output).
                "maj_cat": key_val,
                "by_rdc": {},
                "tot": {"mbq": 0, "stock": 0, "store_stk": 0, "excess_stk": 0,
                        "req": 0, "alloc": 0, "hold": 0},
            })
            # Sidecar metadata: store name/hub + gen_art for crumb building +
            # OPT-level attributes (OPT_TYPE / rank / alloc_seq / status / remarks)
            # used by the OPT-grain flat table.
            if "st_nm" in r and r["st_nm"]:
                row["st_nm"] = r["st_nm"]
            if "hub" in r and r["hub"]:
                row["hub"] = r["hub"]
            if "gen_art_number" in r and r["gen_art_number"]:
                row["gen_art_number"] = r["gen_art_number"]
                row["clr"] = r.get("clr") or ""
            # OPT attributes are per (GEN_ART, CLR, RDC) — store them on the
            # by_rdc cell so the frontend can render them per-RDC row.
            for opt_key in ("opt_type", "opt_priority_rank", "alloc_seq",
                            "alloc_status", "alloc_remarks",
                            "i_rod_planned", "i_rod_used", "alloc_wave"):
                if opt_key in r and r[opt_key] is not None:
                    cell[opt_key] = r[opt_key]
            row["by_rdc"][rdc] = cell
            for k in ("mbq", "stock", "store_stk", "excess_stk", "req", "alloc", "hold"):
                row["tot"][k] += cell[k]
                grand[k]      += cell[k]

        # Derive row totals after sum
        for row in by_key.values():
            t = row["tot"]
            t["req_rem"]  = max(0, t["req"] - t["alloc"])
            t["msa_rem"]  = max(0, t["store_stk"] - t["alloc"] - t["hold"])
            t["req_pct"]  = round(t["alloc"] / t["req"] * 100, 1)  if t["req"]   else 0.0
            t["fill_pct"] = round((t["store_stk"] + t["alloc"]) / t["mbq"] * 100, 1) if t["mbq"]   else 0.0
            t["stk_pct"]  = round(t["alloc"] / t["stock"] * 100, 1) if t["stock"] else 0.0

        out["rdcs"]  = sorted(rdcs_set)
        out["items"] = sorted(by_key.values(), key=lambda x: -x["tot"]["alloc"])
        out["totals"] = {
            **grand,
            "req_rem":  max(0, grand["req"] - grand["alloc"]),
            "msa_rem":  max(0, grand["store_stk"] - grand["alloc"] - grand["hold"]),
            "req_pct":  round(grand["alloc"] / grand["req"] * 100, 1) if grand["req"]   else 0.0,
            "fill_pct": round((grand["store_stk"] + grand["alloc"]) / grand["mbq"] * 100, 1) if grand["mbq"] else 0.0,
            "stk_pct":  round(grand["alloc"] / grand["stock"] * 100, 1) if grand["stock"] else 0.0,
        }
    return APIResponse(success=True, data=out)


# ---------------------------------------------------------------------------
# GET /hold-by-rdc    — RDC × {HOLD_QTY_INITIAL, HOLD_REM, REDUCED}
# ARS_NL_TBL_HOLD_TRACKING has WERKS not RDC → LEFT JOIN ST_MASTER to map.
# ---------------------------------------------------------------------------
@router.get("/hold-by-rdc", response_model=APIResponse)
def get_hold_by_rdc(
    only_open: bool = Query(True),
    current_user: User = Depends(get_current_user),
):
    engine = _engine()
    with engine.connect() as conn:
        if not _table_exists(conn, HOLD_TBL):
            return APIResponse(success=True, data={"items": []})
        if not _table_exists(conn, ST_MASTER):
            # Fallback — group by WERKS instead of RDC
            rows = conn.execute(text(f"""
                SELECT
                    ISNULL(H.WERKS, '—')          AS rdc,
                    ISNULL(SUM(H.HOLD_QTY_INITIAL), 0)  AS hold_int,
                    ISNULL(SUM(H.HOLD_REM),         0)  AS hold_rem
                FROM {HOLD_TBL} H WITH (NOLOCK)
                {'WHERE H.IS_CLOSED = 0' if only_open else ''}
                GROUP BY H.WERKS
                ORDER BY ISNULL(SUM(H.HOLD_REM), 0) DESC
            """)).mappings().all()
        else:
            rows = conn.execute(text(f"""
                SELECT
                    ISNULL(SM.RDC, '—')                 AS rdc,
                    ISNULL(SUM(H.HOLD_QTY_INITIAL), 0)  AS hold_int,
                    ISNULL(SUM(H.HOLD_REM),         0)  AS hold_rem
                FROM {HOLD_TBL} H WITH (NOLOCK)
                LEFT JOIN {ST_MASTER} SM WITH (NOLOCK) ON SM.ST_CD = H.WERKS
                {'WHERE H.IS_CLOSED = 0' if only_open else ''}
                GROUP BY SM.RDC
                ORDER BY ISNULL(SUM(H.HOLD_REM), 0) DESC
            """)).mappings().all()
    items = []
    for r in rows:
        i = int(r["hold_int"] or 0)
        rem = int(r["hold_rem"] or 0)
        items.append({
            "rdc":       r["rdc"] or "—",
            "hold_int":  i,
            "hold_rem":  rem,
            "reduced":   max(0, i - rem),
            "reduced_pct": int(round((i - rem) / i * 100)) if i else 0,
        })
    return APIResponse(success=True, data={"items": items})


# ---------------------------------------------------------------------------
# GET /trend-sessions    — Alloc/Pending per SESSION (within current scope)
# ---------------------------------------------------------------------------
@router.get("/trend-sessions", response_model=APIResponse)
def get_trend_sessions(
    request: Request,
    limit:   int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
):
    scope = _parse_scope(request)
    engine = _engine()
    params: Dict = {}
    where = _where_pend(scope, params)
    needs_st, needs_prod = _needs_joins(scope)
    from_sql = _from_clause(needs_st, needs_prod)
    with engine.connect() as conn:
        if not _table_exists(conn, PEND_ALC):
            return APIResponse(success=True, data={"items": []})
        rows = conn.execute(text(f"""
            SELECT TOP {limit}
                PA.SESSION_ID,
                MIN(PA.MAJ_CAT)                                                  AS maj_cat,
                MIN(PA.RDC)                                                      AS rdc,
                MIN(PA.APPROVED_AT)                                              AS started_at,
                ISNULL(SUM(PA.ALLOC_QTY), 0)                                     AS alloc_qty,
                ISNULL(SUM(PA.DO_QTY),    0)                                     AS do_qty,
                ISNULL(SUM(CASE WHEN PA.IS_CLOSED = 0
                                THEN PA.PEND_QTY ELSE 0 END), 0)                 AS pend_qty
            {from_sql}
            {where}
            GROUP BY PA.SESSION_ID
            ORDER BY MIN(PA.APPROVED_AT) DESC
        """), params).mappings().all()
    items = [{
        "session_id": r["SESSION_ID"],
        "maj_cat":    r["maj_cat"] or "",
        "rdc":        r["rdc"]     or "",
        "alloc_qty":  int(r["alloc_qty"] or 0),
        "do_qty":     int(r["do_qty"]    or 0),
        "pend_qty":   int(r["pend_qty"]  or 0),
        "started_at": r["started_at"].isoformat() if r["started_at"] else None,
    } for r in rows]
    # Reverse so the chart reads left → right oldest → newest
    items.reverse()
    return APIResponse(success=True, data={"items": items})


# ---------------------------------------------------------------------------
# GET /config-extras    — distinct values for the new dimension filters
# ---------------------------------------------------------------------------
@router.get("/config-extras", response_model=APIResponse)
def get_config_extras(current_user: User = Depends(get_current_user)):
    engine = _engine()
    out = {"hubs": [], "statuses": [], "divs": [], "ssns": [], "maj_cats": []}
    with engine.connect() as conn:
        if _table_exists(conn, ST_MASTER):
            try:
                rows = conn.execute(text(
                    f"SELECT DISTINCT HUB FROM {ST_MASTER} WHERE HUB IS NOT NULL AND HUB <> '' ORDER BY HUB"
                )).fetchall()
                out["hubs"] = [str(r[0]).strip() for r in rows if r[0]]
            except Exception:
                pass
            try:
                rows = conn.execute(text(
                    f"SELECT DISTINCT ST_STATUS FROM {ST_MASTER} WHERE ST_STATUS IS NOT NULL ORDER BY ST_STATUS"
                )).fetchall()
                out["statuses"] = [str(r[0]).strip() for r in rows if r[0]]
            except Exception:
                pass
        if _table_exists(conn, PROD_VIEW):
            try:
                rows = conn.execute(text(
                    f"SELECT DISTINCT DIV FROM {PROD_VIEW} WHERE DIV IS NOT NULL AND DIV <> '' ORDER BY DIV"
                )).fetchall()
                out["divs"] = [str(r[0]).strip() for r in rows if r[0]]
            except Exception:
                pass
            try:
                rows = conn.execute(text(
                    f"SELECT DISTINCT SSN FROM {PROD_VIEW} WHERE SSN IS NOT NULL AND SSN <> '' ORDER BY SSN"
                )).fetchall()
                out["ssns"] = [str(r[0]).strip() for r in rows if r[0]]
            except Exception:
                pass
        # MAJ_CAT from PEND_ALC — fresh list filtered by data actually present
        if _table_exists(conn, PEND_ALC):
            try:
                rows = conn.execute(text(
                    f"SELECT DISTINCT MAJ_CAT FROM {PEND_ALC} WHERE MAJ_CAT IS NOT NULL AND MAJ_CAT <> '' ORDER BY MAJ_CAT"
                )).fetchall()
                out["maj_cats"] = [str(r[0]).strip() for r in rows if r[0]]
            except Exception:
                pass
    return APIResponse(success=True, data=out)


# ---------------------------------------------------------------------------
# GET /gap    — gap rollup. group_by = majcat | rdc_article | session_article
# ---------------------------------------------------------------------------
_GAP_GROUPS = {
    "majcat":          (["MAJ_CAT"],                                           "MAJ_CAT"),
    "rdc_article":     (["RDC", "ARTICLE_NUMBER"],                             "RDC, ARTICLE_NUMBER"),
    "session_article": (["SESSION_ID", "ARTICLE_NUMBER"],                      "SESSION_ID, ARTICLE_NUMBER"),
    "rdc_majcat":      (["RDC", "MAJ_CAT"],                                    "RDC, MAJ_CAT"),
    "store":           (["ST_CD"],                                             "ST_CD"),
}

@router.get("/gap", response_model=APIResponse)
def get_gap(
    request:  Request,
    group_by: str = Query("rdc_article"),
    limit:    int = Query(500, ge=1, le=5000),
    current_user: User = Depends(get_current_user),
):
    if group_by not in _GAP_GROUPS:
        group_by = "rdc_article"
    cols, order = _GAP_GROUPS[group_by]
    select_cols = ", ".join([f"PA.{c}" for c in cols])
    group_cols  = ", ".join([f"PA.{c}" for c in cols])

    scope = _parse_scope(request)
    engine = _engine()
    params: Dict = {}
    where = _where_pend(scope, params)
    needs_st, needs_prod = _needs_joins(scope)
    from_sql = _from_clause(needs_st, needs_prod)

    # Force open + gap > 0
    extra = " PA.IS_CLOSED = 0 AND PA.PEND_QTY > 0"
    where = (where + " AND" + extra) if where else (" WHERE" + extra)

    with engine.connect() as conn:
        if not _table_exists(conn, PEND_ALC):
            return APIResponse(success=True, data={"items": [], "group_by": group_by})

        rows = conn.execute(text(f"""
            SELECT TOP {limit}
                {select_cols},
                COUNT(DISTINCT PA.ST_CD)                            AS stores,
                ISNULL(SUM(PA.ALLOC_QTY), 0)                        AS alloc_qty,
                ISNULL(SUM(PA.DO_QTY),    0)                        AS do_qty,
                ISNULL(SUM(PA.PEND_QTY),  0)                        AS gap_qty,
                MAX(DATEDIFF(DAY, PA.APPROVED_AT, GETDATE()))       AS oldest_days,
                COUNT(*)                                            AS rows_n
            {from_sql}
            {where}
            GROUP BY {group_cols}
            ORDER BY ISNULL(SUM(PA.PEND_QTY), 0) DESC
        """), params).mappings().all()

    items = []
    for r in rows:
        alloc = int(r["alloc_qty"] or 0)
        gap   = int(r["gap_qty"]   or 0)
        item = {c.lower(): r[c] for c in cols}
        item.update({
            "stores":      int(r["stores"] or 0),
            "alloc_qty":   alloc,
            "do_qty":      int(r["do_qty"] or 0),
            "gap_qty":     gap,
            "gap_pct":     int(round(gap / alloc * 100)) if alloc else 0,
            "oldest_days": int(r["oldest_days"] or 0),
            "rows_n":      int(r["rows_n"] or 0),
        })
        items.append(item)
    return APIResponse(success=True, data={
        "items":    items,
        "group_by": group_by,
        "columns":  cols,
    })


# ---------------------------------------------------------------------------
# GET /gap/export    — Excel download of the current gap view
# ---------------------------------------------------------------------------
@router.get("/gap/export")
def export_gap(
    request:  Request,
    group_by: str = Query("rdc_article"),
    current_user: User = Depends(get_current_user),
):
    if group_by not in _GAP_GROUPS:
        group_by = "rdc_article"
    cols, _order = _GAP_GROUPS[group_by]
    select_cols  = ", ".join([f"PA.{c}" for c in cols])
    group_cols   = ", ".join([f"PA.{c}" for c in cols])

    scope = _parse_scope(request)
    engine = _engine()
    params: Dict = {}
    where = _where_pend(scope, params)
    needs_st, needs_prod = _needs_joins(scope)
    from_sql = _from_clause(needs_st, needs_prod)
    extra = " PA.IS_CLOSED = 0 AND PA.PEND_QTY > 0"
    where = (where + " AND" + extra) if where else (" WHERE" + extra)

    sql = f"""
        SELECT
            {select_cols},
            COUNT(DISTINCT PA.ST_CD)                            AS stores,
            ISNULL(SUM(PA.ALLOC_QTY), 0)                        AS alloc_qty,
            ISNULL(SUM(PA.DO_QTY),    0)                        AS do_qty,
            ISNULL(SUM(PA.PEND_QTY),  0)                        AS gap_qty,
            MAX(DATEDIFF(DAY, PA.APPROVED_AT, GETDATE()))       AS oldest_days,
            COUNT(*)                                            AS rows_n
        {from_sql}
        {where}
        GROUP BY {group_cols}
        ORDER BY ISNULL(SUM(PA.PEND_QTY), 0) DESC
    """
    with engine.connect() as conn:
        df = pd.read_sql(text(sql), conn, params=params)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name=f"gap_{group_by}", index=False)
    buf.seek(0)

    fname = f"ars_dashboard_gap_{group_by}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )
