"""
gap_report.py — multi-category GAP review surface.

Surfaces the diagnostic taxonomy the rule engine writes into the parked /
history archives so operators can review *why* the algorithm made each
decision and act on it. The existing /ars-dashboard/gap endpoint only
covers ARS_PEND_ALC.PEND_QTY > 0 (the SAP-delivery gap); this module is a
strict superset covering eight gap categories grouped into three families:

    Algorithm Decision
      1. excess-stk         EXCESS_STK > 0 on ARS_LISTING_(PARKED|HISTORY)
      2. listed-not-alloc   LISTING=1 but SHIP+HOLD = 0 on ARS_ALLOC_*
      3. skip-reason        ALLOC_STATUS='SKIPPED' grouped by SKIP_REASON

    Quantity & Balance
      4. hold-anomaly       SHIP=0 AND HOLD>0 (TBL holdback that never
                            converted into ship)
      5. mbq-deviation      side=under | side=over — post-alloc MJ_STK_TTL
                            below MBQ floor, or Σ(SHIP) above MJ_REQ ceiling

    Lifecycle
      6. pend-aging         IS_CLOSED=0 AND PEND_QTY>0 AND age>min_days
      7. bdc-do-reco        ARS_BDC_HISTORY with DO_RECEIVED < BDC_QTY
      8. parked-drift       ARS_*_PARKED with PARK_STATUS='PENDING' and
                            DATEDIFF(PARKED_AT, NOW) > min_days

Each endpoint returns the standard
    {"items": [...], "group_by": ..., "columns": [...], "totals": {...}}
shape so the frontend renderer (FlatDrillTable) can paint any of them.

Scope filtering reuses the same helpers as /ars-dashboard/* (date/sid/mc/
werks/rdc/hub/status/div/ssn) — see ars_dashboard.py for the canonical
definition. We import the private names here; the convention is consistent
across the dashboard module family.
"""
from __future__ import annotations

import inspect
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

# Reuse scope helpers from the sibling ars_dashboard module — same scope
# filters and join-fragment rules apply across every analytics endpoint.
from app.api.v1.endpoints.ars_dashboard import (
    _parse_scope,
    _where_pend,
    _needs_joins,
    _from_clause,
    _default_window,
    _table_exists,
    _engine,
    PEND_ALC,
)


router = APIRouter(prefix="/gap-report", tags=["GAP Report"])


# ---------------------------------------------------------------------------
# Table constants
# ---------------------------------------------------------------------------
# Per-OPT listing snapshot (carries EXCESS_STK). The _WORKING_* equivalents
# zero EXCESS_STK during allocation so we always read it from the snapshot.
LIST_SNAP_PARKED  = "ARS_LISTING_PARKED"
LIST_SNAP_HISTORY = "ARS_LISTING_HISTORY"

# Per-OPT working output (LISTING, OPT_TYPE, MJ_*, ALLOC_QTY/HOLD_QTY,
# ALLOC_STATUS, SKIP_REASON on the OPT grain).
LIST_WRK_PARKED  = "ARS_LISTING_WORKING_PARKED"
LIST_WRK_HISTORY = "ARS_LISTING_WORKING_HISTORY"

# Per-VAR_ART × SZ allocation output (SHIP_QTY/HOLD_QTY, ALLOC_STATUS,
# SKIP_REASON at size grain).
ALLOC_PARKED  = "ARS_ALLOC_PARKED"
ALLOC_HISTORY = "ARS_ALLOC_HISTORY"

# Pending and BDC.
BDC_HISTORY = "ARS_BDC_HISTORY"

# Categories the summary endpoint reports on (in display order).
GAP_CATEGORIES: List[Tuple[str, str, str]] = [
    ("excess-stk",       "Excess Stock",            "algorithm"),
    ("listed-not-alloc", "Listed-Not-Allocated",    "algorithm"),
    ("skip-reason",      "Skip-Reason Breakdown",   "algorithm"),
    ("hold-anomaly",     "Hold-Without-Ship",       "quantity"),
    ("mbq-deviation",    "MBQ Deviation",           "quantity"),
    ("pend-aging",       "Pend-Alloc Aging",        "lifecycle"),
    ("bdc-do-reco",      "BDC vs DO Reconciliation","lifecycle"),
    ("parked-drift",     "Parked Drift",            "lifecycle"),
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _column_exists(conn, table: str, col: str) -> bool:
    return conn.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME=:t AND COLUMN_NAME=:c"
    ), {"t": table, "c": col}).scalar() > 0


def _resolve_sources(source: str, parked: str, history: str) -> List[str]:
    """Map the public ?source= switch to the underlying table list."""
    s = (source or "both").lower()
    if s == "parked":  return [parked]
    if s == "history": return [history]
    return [parked, history]


def _where_listing(scope: Dict, params: Dict, alias: str = "T") -> str:
    """WHERE clause for ARS_LISTING_* / ARS_ALLOC_* tables (no PEND_ALC).

    Uses the same scope dict produced by _parse_scope but applies it to a
    listing-style table whose primary keys are (SESSION_ID, WERKS, MAJ_CAT,
    RDC, ARTICLE_NUMBER). HUB/STATUS/DIV/SSN dimensions are intentionally
    ignored here — those joins live on the PEND_ALC path; surfacing them on
    the listing path would require an extra JOIN on WERKS↔ST_CD which we
    can add later if asked.
    """
    parts: List[str] = []

    if scope.get("sid"):
        parts.append(f"{alias}.SESSION_ID = :p_sid")
        params["p_sid"] = scope["sid"]

    for col, key, vals in (
        ("MAJ_CAT", "p_mc",    scope.get("mc")    or []),
        ("WERKS",   "p_werks", scope.get("werks") or []),
        ("RDC",     "p_rdc",   scope.get("rdc")   or []),
    ):
        if vals:
            ph = []
            for i, v in enumerate(vals):
                k = f"{key}{i}"
                params[k] = v
                ph.append(f":{k}")
            parts.append(f"{alias}.{col} IN ({','.join(ph)})")

    return (" WHERE " + " AND ".join(parts)) if parts else ""


def _union_listing_select(sources: List[str], select_cols: str, scope: Dict,
                          params: Dict, extra: str = "") -> str:
    """UNION ALL across parked/history listing-style tables, applying the
    same scope filter to each. `extra` is appended to each branch's WHERE.
    """
    branches = []
    where = _where_listing(scope, params, alias="T")
    for tbl in sources:
        if extra:
            sep = " AND " if where else " WHERE "
            branches.append(
                f"SELECT {select_cols} FROM {tbl} T WITH (NOLOCK) "
                f"{where}{sep}{extra}"
            )
        else:
            branches.append(
                f"SELECT {select_cols} FROM {tbl} T WITH (NOLOCK) {where}"
            )
    return " UNION ALL ".join(branches)


def _to_response(items: List[Dict], group_by: str, columns: List[str],
                 totals: Dict) -> APIResponse:
    return APIResponse(success=True, data={
        "items":    items,
        "group_by": group_by,
        "columns":  columns,
        "totals":   totals,
    })


def _row_to_item(row, cols: List[str], extras: Dict) -> Dict:
    out = {c.lower(): row[c] for c in cols if c in row}
    out.update(extras)
    return out


# ---------------------------------------------------------------------------
# GET /summary — KPI strip
# ---------------------------------------------------------------------------
@router.get("/summary", response_model=APIResponse)
def get_summary(
    request: Request,
    min_pend_days: int = Query(7, ge=0, le=365),
    min_drift_days: int = Query(3, ge=0, le=365),
    current_user: User = Depends(get_current_user),
):
    """Counts + totals per gap category for the current scope."""
    scope = _parse_scope(request)
    eng = _engine()

    out: List[Dict] = []
    with eng.connect() as conn:
        # --- 1. excess-stk (listing snapshot, parked+history) ---
        rows = qty = 0
        for tbl in (LIST_SNAP_PARKED, LIST_SNAP_HISTORY):
            if not _table_exists(conn, tbl):
                continue
            p: Dict = {}
            w = _where_listing(scope, p, "T")
            sep = " AND " if w else " WHERE "
            sql = (
                f"SELECT ISNULL(COUNT(*),0) AS rows_, "
                f"       ISNULL(SUM(EXCESS_STK),0) AS qty "
                f"FROM {tbl} T WITH (NOLOCK) {w}{sep} ISNULL(EXCESS_STK,0) > 0"
            )
            r = conn.execute(text(sql), p).mappings().first()
            rows += int(r["rows_"] or 0); qty += int(r["qty"] or 0)
        out.append({"key": "excess-stk", "label": "Excess Stock",
                    "family": "algorithm", "rows": rows, "qty": qty})

        # --- 2. listed-not-alloc (alloc tables, SHIP=HOLD=0, not INELIGIBLE) ---
        rows = qty = 0
        for tbl in (ALLOC_PARKED, ALLOC_HISTORY):
            if not _table_exists(conn, tbl):
                continue
            p: Dict = {}
            w = _where_listing(scope, p, "T")
            sep = " AND " if w else " WHERE "
            sql = (
                f"SELECT ISNULL(COUNT(*),0) AS rows_ "
                f"FROM {tbl} T WITH (NOLOCK) {w}{sep}"
                f" ISNULL(SHIP_QTY,0)=0 AND ISNULL(HOLD_QTY,0)=0 "
                f" AND ISNULL(ALLOC_STATUS,'') <> 'INELIGIBLE'"
            )
            r = conn.execute(text(sql), p).mappings().first()
            rows += int(r["rows_"] or 0)
        out.append({"key": "listed-not-alloc", "label": "Listed-Not-Allocated",
                    "family": "algorithm", "rows": rows, "qty": 0})

        # --- 3. skip-reason (alloc tables, SKIPPED) ---
        rows = 0
        for tbl in (ALLOC_PARKED, ALLOC_HISTORY):
            if not _table_exists(conn, tbl):
                continue
            p: Dict = {}
            w = _where_listing(scope, p, "T")
            sep = " AND " if w else " WHERE "
            sql = (
                f"SELECT ISNULL(COUNT(*),0) AS rows_ "
                f"FROM {tbl} T WITH (NOLOCK) {w}{sep}"
                f" ISNULL(ALLOC_STATUS,'') = 'SKIPPED'"
            )
            r = conn.execute(text(sql), p).mappings().first()
            rows += int(r["rows_"] or 0)
        out.append({"key": "skip-reason", "label": "Skip-Reason Breakdown",
                    "family": "algorithm", "rows": rows, "qty": 0})

        # --- 4. hold-anomaly (SHIP=0 AND HOLD>0) ---
        rows = qty = 0
        for tbl in (ALLOC_PARKED, ALLOC_HISTORY):
            if not _table_exists(conn, tbl):
                continue
            p: Dict = {}
            w = _where_listing(scope, p, "T")
            sep = " AND " if w else " WHERE "
            sql = (
                f"SELECT ISNULL(COUNT(*),0) AS rows_, "
                f"       ISNULL(SUM(HOLD_QTY),0) AS qty "
                f"FROM {tbl} T WITH (NOLOCK) {w}{sep}"
                f" ISNULL(SHIP_QTY,0)=0 AND ISNULL(HOLD_QTY,0)>0"
            )
            r = conn.execute(text(sql), p).mappings().first()
            rows += int(r["rows_"] or 0); qty += int(r["qty"] or 0)
        out.append({"key": "hold-anomaly", "label": "Hold-Without-Ship",
                    "family": "quantity", "rows": rows, "qty": qty})

        # --- 5. mbq-deviation (LISTING_WORKING grain) — sum the under-floor side
        rows = 0
        for tbl in (LIST_WRK_PARKED, LIST_WRK_HISTORY):
            if not _table_exists(conn, tbl):
                continue
            if not _column_exists(conn, tbl, "MJ_MBQ"):
                continue
            p: Dict = {}
            w = _where_listing(scope, p, "T")
            sep = " AND " if w else " WHERE "
            sql = (
                f"SELECT ISNULL(COUNT(*),0) AS rows_ "
                f"FROM {tbl} T WITH (NOLOCK) {w}{sep}"
                f" ISNULL(MJ_MBQ,0) > 0 "
                f" AND (ISNULL(MJ_STK_TTL,0) + ISNULL(ALLOC_QTY,0)) "
                f"     < 0.7 * ISNULL(MJ_MBQ,0)"
            )
            r = conn.execute(text(sql), p).mappings().first()
            rows += int(r["rows_"] or 0)
        out.append({"key": "mbq-deviation", "label": "MBQ Deviation",
                    "family": "quantity", "rows": rows, "qty": 0})

        # --- 6. pend-aging (ARS_PEND_ALC.IS_CLOSED=0 AND PEND_QTY>0 AND age>N) ---
        rows = qty = oldest = 0
        if _table_exists(conn, PEND_ALC):
            p: Dict = {}
            w = _where_pend(scope, p)
            extra = (
                f" PA.IS_CLOSED = 0 AND PA.PEND_QTY > 0 "
                f" AND DATEDIFF(DAY, PA.APPROVED_AT, GETDATE()) > {int(min_pend_days)}"
            )
            w = (w + " AND" + extra) if w else (" WHERE" + extra)
            needs_st, needs_prod = _needs_joins(scope)
            from_sql = _from_clause(needs_st, needs_prod)
            sql = (
                f"SELECT ISNULL(COUNT(*),0) AS rows_, "
                f"       ISNULL(SUM(PA.PEND_QTY),0) AS qty, "
                f"       ISNULL(MAX(DATEDIFF(DAY, PA.APPROVED_AT, GETDATE())),0) AS oldest "
                f"{from_sql} {w}"
            )
            r = conn.execute(text(sql), p).mappings().first()
            rows = int(r["rows_"] or 0); qty = int(r["qty"] or 0)
            oldest = int(r["oldest"] or 0)
        out.append({"key": "pend-aging", "label": "Pend-Alloc Aging",
                    "family": "lifecycle", "rows": rows, "qty": qty,
                    "oldest_days": oldest})

        # --- 7. bdc-do-reco (BDC_HISTORY where DO_RECEIVED < BDC_QTY) ---
        rows = qty = 0
        if _table_exists(conn, BDC_HISTORY):
            sql = (
                f"SELECT ISNULL(COUNT(*),0) AS rows_, "
                f"       ISNULL(SUM(BDC_QTY - DO_RECEIVED),0) AS qty "
                f"FROM {BDC_HISTORY} B WITH (NOLOCK) "
                f"WHERE ISNULL(B.DO_RECEIVED,0) < ISNULL(B.BDC_QTY,0) "
                f"  AND ISNULL(B.STATUS,'') <> 'CLOSED'"
            )
            r = conn.execute(text(sql), {}).mappings().first()
            rows = int(r["rows_"] or 0); qty = int(r["qty"] or 0)
        out.append({"key": "bdc-do-reco", "label": "BDC vs DO Reconciliation",
                    "family": "lifecycle", "rows": rows, "qty": qty})

        # --- 8. parked-drift (ARS_*_PARKED with PARK_STATUS='PENDING' > N days) ---
        rows = 0; oldest = 0
        for tbl in (LIST_WRK_PARKED, ALLOC_PARKED):
            if not _table_exists(conn, tbl):
                continue
            if not _column_exists(conn, tbl, "PARK_STATUS"):
                continue
            sql = (
                f"SELECT ISNULL(COUNT(DISTINCT SESSION_ID),0) AS rows_, "
                f"       ISNULL(MAX(DATEDIFF(DAY, PARKED_AT, GETDATE())),0) AS oldest "
                f"FROM {tbl} WITH (NOLOCK) "
                f"WHERE ISNULL(PARK_STATUS,'') = 'PENDING' "
                f"  AND DATEDIFF(DAY, PARKED_AT, GETDATE()) > {int(min_drift_days)}"
            )
            r = conn.execute(text(sql), {}).mappings().first()
            rows += int(r["rows_"] or 0)
            oldest = max(oldest, int(r["oldest"] or 0))
        out.append({"key": "parked-drift", "label": "Parked Drift",
                    "family": "lifecycle", "rows": rows, "qty": 0,
                    "oldest_days": oldest})

    return APIResponse(success=True, data={"categories": out})


# ---------------------------------------------------------------------------
# GET /excess-stk
# ---------------------------------------------------------------------------
_EXCESS_GROUPS = {
    "majcat":  ("MAJ_CAT",       "MAJ_CAT"),
    "rdc":     ("RDC",           "RDC"),
    "store":   ("WERKS",         "WERKS"),
    "article": ("GEN_ART_NUMBER, CLR", "GEN_ART_NUMBER, ISNULL(CLR,'')"),
    "session": ("SESSION_ID",    "SESSION_ID"),
}

@router.get("/excess-stk", response_model=APIResponse)
def get_excess_stk(
    request: Request,
    source:   str  = Query("both", pattern=r"^(parked|history|both)$"),
    group_by: str  = Query("majcat"),
    limit:    int  = Query(500, ge=1, le=5000),
    current_user: User = Depends(get_current_user),
):
    if group_by not in _EXCESS_GROUPS:
        group_by = "majcat"
    select_cols, group_cols = _EXCESS_GROUPS[group_by]
    scope = _parse_scope(request)
    sources = _resolve_sources(source, LIST_SNAP_PARKED, LIST_SNAP_HISTORY)

    raw_cols = [c.strip() for c in select_cols.split(",")]  # ["GEN_ART_NUMBER","CLR"] etc.

    eng = _engine()
    with eng.connect() as conn:
        present = [t for t in sources if _table_exists(conn, t)]
        if not present:
            return _to_response([], group_by, [], {})

        # Per-table SELECT then merge in python — groups are small (<500 rows).
        agg: Dict[Tuple, Dict] = {}
        for tbl in present:
            p: Dict = {}
            w = _where_listing(scope, p, "T")
            sep = " AND " if w else " WHERE "
            sql = f"""
                SELECT {select_cols},
                       SUM(ISNULL(EXCESS_STK,0)) AS excess_qty,
                       COUNT(*)                  AS rows_n,
                       COUNT(DISTINCT WERKS)     AS stores,
                       COUNT(DISTINCT RDC)       AS rdcs
                FROM {tbl} T WITH (NOLOCK)
                {w}{sep} ISNULL(EXCESS_STK,0) > 0
                GROUP BY {group_cols}
            """
            for r in conn.execute(text(sql), p).mappings().all():
                key = tuple(r[c] for c in raw_cols)
                cur = agg.setdefault(key, {
                    "excess_qty": 0, "rows_n": 0, "stores": 0, "rdcs": 0,
                    **{c.lower(): r[c] for c in raw_cols},
                })
                cur["excess_qty"] += int(r["excess_qty"] or 0)
                cur["rows_n"]     += int(r["rows_n"] or 0)
                cur["stores"]     = max(cur["stores"], int(r["stores"] or 0))
                cur["rdcs"]       = max(cur["rdcs"], int(r["rdcs"] or 0))

    rows = sorted(agg.values(), key=lambda x: -x["excess_qty"])[:limit]
    cols = [c.lower() for c in raw_cols] + ["excess_qty", "rows_n", "stores", "rdcs"]
    totals = {
        "excess_qty": sum(r["excess_qty"] for r in rows),
        "rows_n":     sum(r["rows_n"]     for r in rows),
    }
    return _to_response(rows, group_by, cols, totals)


# ---------------------------------------------------------------------------
# GET /listed-not-alloc
# ---------------------------------------------------------------------------
_LNA_GROUPS = {
    "majcat":   "MAJ_CAT",
    "opt_type": "OPT_TYPE",
    "store":    "WERKS",
    "rdc":      "RDC",
    "article":  "GEN_ART_NUMBER",
}

@router.get("/listed-not-alloc", response_model=APIResponse)
def get_listed_not_alloc(
    request: Request,
    source:   str  = Query("both", pattern=r"^(parked|history|both)$"),
    group_by: str  = Query("majcat"),
    limit:    int  = Query(500, ge=1, le=5000),
    current_user: User = Depends(get_current_user),
):
    if group_by not in _LNA_GROUPS:
        group_by = "majcat"
    gcol = _LNA_GROUPS[group_by]
    scope = _parse_scope(request)
    sources = _resolve_sources(source, ALLOC_PARKED, ALLOC_HISTORY)

    eng = _engine()
    agg: Dict[str, Dict] = {}
    with eng.connect() as conn:
        present = [t for t in sources if _table_exists(conn, t)]
        if not present:
            return _to_response([], group_by, [], {})

        for tbl in present:
            p: Dict = {}
            w = _where_listing(scope, p, "T")
            sep = " AND " if w else " WHERE "
            sql = f"""
                SELECT {gcol} AS grp_,
                       COUNT(*)               AS rows_n,
                       COUNT(DISTINCT WERKS)  AS stores,
                       COUNT(DISTINCT RDC)    AS rdcs
                FROM {tbl} T WITH (NOLOCK)
                {w}{sep} ISNULL(SHIP_QTY,0)=0 AND ISNULL(HOLD_QTY,0)=0
                       AND ISNULL(ALLOC_STATUS,'') <> 'INELIGIBLE'
                GROUP BY {gcol}
            """
            for r in conn.execute(text(sql), p).mappings().all():
                k = str(r["grp_"] or "")
                cur = agg.setdefault(k, {gcol.lower(): r["grp_"],
                                         "rows_n": 0, "stores": 0, "rdcs": 0})
                cur["rows_n"] += int(r["rows_n"] or 0)
                cur["stores"]  = max(cur["stores"], int(r["stores"] or 0))
                cur["rdcs"]    = max(cur["rdcs"], int(r["rdcs"] or 0))

    rows = sorted(agg.values(), key=lambda x: -x["rows_n"])[:limit]
    cols = [gcol.lower(), "rows_n", "stores", "rdcs"]
    totals = {"rows_n": sum(r["rows_n"] for r in rows)}
    return _to_response(rows, group_by, cols, totals)


# ---------------------------------------------------------------------------
# GET /skip-reason
# ---------------------------------------------------------------------------
_SR_GROUPS = {
    "skip_reason": "SKIP_REASON",
    "majcat":      "MAJ_CAT",
    "opt_type":    "OPT_TYPE",
    "store":       "WERKS",
    "rdc":         "RDC",
}

@router.get("/skip-reason", response_model=APIResponse)
def get_skip_reason(
    request: Request,
    source:   str  = Query("both", pattern=r"^(parked|history|both)$"),
    group_by: str  = Query("skip_reason"),
    skip_like: Optional[str] = Query(None, description="filter SKIP_REASON LIKE pattern"),
    limit:    int  = Query(500, ge=1, le=5000),
    current_user: User = Depends(get_current_user),
):
    if group_by not in _SR_GROUPS:
        group_by = "skip_reason"
    gcol = _SR_GROUPS[group_by]
    scope = _parse_scope(request)
    sources = _resolve_sources(source, ALLOC_PARKED, ALLOC_HISTORY)

    eng = _engine()
    agg: Dict[str, Dict] = {}
    with eng.connect() as conn:
        present = [t for t in sources if _table_exists(conn, t)]
        if not present:
            return _to_response([], group_by, [], {})

        for tbl in present:
            p: Dict = {}
            w = _where_listing(scope, p, "T")
            sep = " AND " if w else " WHERE "
            extra = (
                "ISNULL(ALLOC_STATUS,'') = 'SKIPPED' "
                "AND ISNULL(SKIP_REASON,'') <> ''"
            )
            if skip_like:
                p["p_skip_like"] = skip_like
                extra += " AND SKIP_REASON LIKE :p_skip_like"
            sql = f"""
                SELECT ISNULL({gcol},'') AS grp_,
                       COUNT(*)              AS rows_n,
                       COUNT(DISTINCT WERKS) AS stores,
                       COUNT(DISTINCT SESSION_ID) AS sessions
                FROM {tbl} T WITH (NOLOCK)
                {w}{sep} {extra}
                GROUP BY ISNULL({gcol},'')
            """
            for r in conn.execute(text(sql), p).mappings().all():
                k = str(r["grp_"] or "")
                cur = agg.setdefault(k, {gcol.lower(): r["grp_"],
                                         "rows_n": 0, "stores": 0, "sessions": 0})
                cur["rows_n"]   += int(r["rows_n"] or 0)
                cur["stores"]    = max(cur["stores"], int(r["stores"] or 0))
                cur["sessions"]  = max(cur["sessions"], int(r["sessions"] or 0))

    rows = sorted(agg.values(), key=lambda x: -x["rows_n"])[:limit]
    cols = [gcol.lower(), "rows_n", "stores", "sessions"]
    totals = {"rows_n": sum(r["rows_n"] for r in rows)}
    return _to_response(rows, group_by, cols, totals)


# ---------------------------------------------------------------------------
# GET /hold-anomaly
# ---------------------------------------------------------------------------
_HA_GROUPS = _LNA_GROUPS  # same group dims as listed-not-alloc

@router.get("/hold-anomaly", response_model=APIResponse)
def get_hold_anomaly(
    request: Request,
    source:   str  = Query("both", pattern=r"^(parked|history|both)$"),
    group_by: str  = Query("majcat"),
    limit:    int  = Query(500, ge=1, le=5000),
    current_user: User = Depends(get_current_user),
):
    if group_by not in _HA_GROUPS:
        group_by = "majcat"
    gcol = _HA_GROUPS[group_by]
    scope = _parse_scope(request)
    sources = _resolve_sources(source, ALLOC_PARKED, ALLOC_HISTORY)

    eng = _engine()
    agg: Dict[str, Dict] = {}
    with eng.connect() as conn:
        present = [t for t in sources if _table_exists(conn, t)]
        if not present:
            return _to_response([], group_by, [], {})

        for tbl in present:
            p: Dict = {}
            w = _where_listing(scope, p, "T")
            sep = " AND " if w else " WHERE "
            sql = f"""
                SELECT {gcol} AS grp_,
                       COUNT(*)                AS rows_n,
                       SUM(ISNULL(HOLD_QTY,0)) AS hold_qty,
                       COUNT(DISTINCT WERKS)   AS stores
                FROM {tbl} T WITH (NOLOCK)
                {w}{sep} ISNULL(SHIP_QTY,0)=0 AND ISNULL(HOLD_QTY,0)>0
                GROUP BY {gcol}
            """
            for r in conn.execute(text(sql), p).mappings().all():
                k = str(r["grp_"] or "")
                cur = agg.setdefault(k, {gcol.lower(): r["grp_"],
                                         "rows_n": 0, "hold_qty": 0, "stores": 0})
                cur["rows_n"]   += int(r["rows_n"] or 0)
                cur["hold_qty"] += int(r["hold_qty"] or 0)
                cur["stores"]    = max(cur["stores"], int(r["stores"] or 0))

    rows = sorted(agg.values(), key=lambda x: -x["hold_qty"])[:limit]
    cols = [gcol.lower(), "rows_n", "hold_qty", "stores"]
    totals = {"rows_n": sum(r["rows_n"] for r in rows),
              "hold_qty": sum(r["hold_qty"] for r in rows)}
    return _to_response(rows, group_by, cols, totals)


# ---------------------------------------------------------------------------
# GET /mbq-deviation  — side=under | over
# ---------------------------------------------------------------------------
_MBQ_GROUPS = {
    "majcat":   "MAJ_CAT",
    "opt_type": "OPT_TYPE",
    "store":    "WERKS",
    "rdc":      "RDC",
}

@router.get("/mbq-deviation", response_model=APIResponse)
def get_mbq_deviation(
    request: Request,
    side:        str   = Query("under", pattern=r"^(under|over)$"),
    source:      str   = Query("both",  pattern=r"^(parked|history|both)$"),
    group_by:    str   = Query("majcat"),
    under_factor: float = Query(0.7,  ge=0.1, le=1.0),
    over_factor:  float = Query(1.10, ge=1.0, le=3.0),
    limit:       int   = Query(500,  ge=1, le=5000),
    current_user: User = Depends(get_current_user),
):
    if group_by not in _MBQ_GROUPS:
        group_by = "majcat"
    gcol = _MBQ_GROUPS[group_by]
    scope = _parse_scope(request)
    sources = _resolve_sources(source, LIST_WRK_PARKED, LIST_WRK_HISTORY)

    if side == "under":
        gate = ("ISNULL(MJ_MBQ,0) > 0 "
                "AND (ISNULL(MJ_STK_TTL,0) + ISNULL(ALLOC_QTY,0)) "
                f"    < {float(under_factor)} * ISNULL(MJ_MBQ,0)")
        metric_sql = ("SUM(ISNULL(MJ_MBQ,0) - ISNULL(MJ_STK_TTL,0) - ISNULL(ALLOC_QTY,0))")
        metric_key = "shortfall_qty"
    else:
        gate = ("ISNULL(MJ_REQ,0) > 0 "
                f"AND ISNULL(ALLOC_QTY,0) > {float(over_factor)} * ISNULL(MJ_REQ,0)")
        metric_sql = ("SUM(ISNULL(ALLOC_QTY,0) - ISNULL(MJ_REQ,0))")
        metric_key = "overshoot_qty"

    eng = _engine()
    agg: Dict[str, Dict] = {}
    with eng.connect() as conn:
        present = [t for t in sources if _table_exists(conn, t)
                   and _column_exists(conn, t, "MJ_MBQ")
                   and _column_exists(conn, t, "MJ_REQ")]
        if not present:
            return _to_response([], group_by, [], {})

        for tbl in present:
            p: Dict = {}
            w = _where_listing(scope, p, "T")
            sep = " AND " if w else " WHERE "
            sql = f"""
                SELECT {gcol} AS grp_,
                       COUNT(*)             AS rows_n,
                       {metric_sql}         AS metric_,
                       COUNT(DISTINCT WERKS) AS stores
                FROM {tbl} T WITH (NOLOCK)
                {w}{sep} {gate}
                GROUP BY {gcol}
            """
            for r in conn.execute(text(sql), p).mappings().all():
                k = str(r["grp_"] or "")
                cur = agg.setdefault(k, {gcol.lower(): r["grp_"],
                                         "rows_n": 0, metric_key: 0, "stores": 0})
                cur["rows_n"]    += int(r["rows_n"] or 0)
                cur[metric_key]  += int(r["metric_"] or 0)
                cur["stores"]     = max(cur["stores"], int(r["stores"] or 0))

    rows = sorted(agg.values(), key=lambda x: -abs(x[metric_key]))[:limit]
    cols = [gcol.lower(), "rows_n", metric_key, "stores"]
    totals = {"rows_n": sum(r["rows_n"] for r in rows),
              metric_key: sum(r[metric_key] for r in rows)}
    return _to_response(rows, group_by, cols, totals)


# ---------------------------------------------------------------------------
# GET /pend-aging  — reframed PEND_ALC gap, aged
# ---------------------------------------------------------------------------
_PA_GROUPS = {
    "majcat":      (["MAJ_CAT"],                       "MAJ_CAT"),
    "rdc_article": (["RDC", "ARTICLE_NUMBER"],         "RDC, ARTICLE_NUMBER"),
    "store":       (["ST_CD"],                         "ST_CD"),
    "session":     (["SESSION_ID"],                    "SESSION_ID"),
}

@router.get("/pend-aging", response_model=APIResponse)
def get_pend_aging(
    request: Request,
    group_by: str = Query("majcat"),
    min_days: int = Query(7, ge=0, le=365),
    limit:    int = Query(500, ge=1, le=5000),
    current_user: User = Depends(get_current_user),
):
    if group_by not in _PA_GROUPS:
        group_by = "majcat"
    sel_cols, grp_sql = _PA_GROUPS[group_by]
    scope = _parse_scope(request)

    eng = _engine()
    items: List[Dict] = []
    with eng.connect() as conn:
        if not _table_exists(conn, PEND_ALC):
            return _to_response([], group_by, [], {})

        params: Dict = {}
        where = _where_pend(scope, params)
        needs_st, needs_prod = _needs_joins(scope)
        from_sql = _from_clause(needs_st, needs_prod)
        extra = (f" PA.IS_CLOSED = 0 AND PA.PEND_QTY > 0 "
                 f" AND DATEDIFF(DAY, PA.APPROVED_AT, GETDATE()) > {int(min_days)}")
        where = (where + " AND" + extra) if where else (" WHERE" + extra)

        select_cols = ", ".join(f"PA.{c}" for c in sel_cols)
        sql = f"""
            SELECT TOP {int(limit)}
                {select_cols},
                COUNT(DISTINCT PA.ST_CD)        AS stores,
                ISNULL(SUM(PA.ALLOC_QTY),0)     AS alloc_qty,
                ISNULL(SUM(PA.DO_QTY),0)        AS do_qty,
                ISNULL(SUM(PA.PEND_QTY),0)      AS pend_qty,
                MAX(DATEDIFF(DAY, PA.APPROVED_AT, GETDATE())) AS oldest_days,
                COUNT(*)                        AS rows_n
            {from_sql} {where}
            GROUP BY {', '.join(f'PA.{c}' for c in sel_cols)}
            ORDER BY ISNULL(SUM(PA.PEND_QTY),0) DESC
        """
        for r in conn.execute(text(sql), params).mappings().all():
            item = {c.lower(): r[c] for c in sel_cols}
            item.update({
                "stores":      int(r["stores"] or 0),
                "alloc_qty":   int(r["alloc_qty"] or 0),
                "do_qty":      int(r["do_qty"] or 0),
                "pend_qty":    int(r["pend_qty"] or 0),
                "oldest_days": int(r["oldest_days"] or 0),
                "rows_n":      int(r["rows_n"] or 0),
            })
            items.append(item)

    cols = [c.lower() for c in sel_cols] + \
           ["stores", "alloc_qty", "do_qty", "pend_qty", "oldest_days", "rows_n"]
    totals = {"pend_qty": sum(i["pend_qty"] for i in items),
              "rows_n":   sum(i["rows_n"]   for i in items)}
    return _to_response(items, group_by, cols, totals)


# ---------------------------------------------------------------------------
# GET /bdc-do-reco
# ---------------------------------------------------------------------------
_BDC_GROUPS = {
    "rdc_article": ("RDC, ARTICLE_NUMBER", ["RDC", "ARTICLE_NUMBER"]),
    "store":       ("ST_CD",               ["ST_CD"]),
    "alloc_no":    ("ALLOCATION_NUMBER",   ["ALLOCATION_NUMBER"]),
    "majcat":      ("MAJ_CAT",             ["MAJ_CAT"]),
}

@router.get("/bdc-do-reco", response_model=APIResponse)
def get_bdc_do_reco(
    request: Request,
    group_by: str = Query("rdc_article"),
    limit:    int = Query(500, ge=1, le=5000),
    current_user: User = Depends(get_current_user),
):
    if group_by not in _BDC_GROUPS:
        group_by = "rdc_article"
    grp_sql, sel_cols = _BDC_GROUPS[group_by]
    scope = _parse_scope(request)

    eng = _engine()
    items: List[Dict] = []
    with eng.connect() as conn:
        if not _table_exists(conn, BDC_HISTORY):
            return _to_response([], group_by, [], {})

        # BDC_HISTORY uses BDC_DATE rather than APPROVED_AT — apply the same
        # date window if scope.date / from / to are present.
        parts: List[str] = []
        params: Dict = {}
        if scope.get("date"):
            parts.append("CAST(B.BDC_DATE AS DATE) = :p_date")
            params["p_date"] = scope["date"]
        else:
            d_from = scope.get("from")
            d_to   = scope.get("to")
            if not d_from and not d_to:
                d_from, d_to = _default_window()
            if d_from:
                parts.append("CAST(B.BDC_DATE AS DATE) >= :p_from")
                params["p_from"] = d_from
            if d_to:
                parts.append("CAST(B.BDC_DATE AS DATE) <= :p_to")
                params["p_to"] = d_to
        for col, key, vals in (
            ("RDC",   "p_rdc",   scope.get("rdc")   or []),
            ("ST_CD", "p_werks", scope.get("werks") or []),
            ("MAJ_CAT","p_mc",   scope.get("mc")    or []),
        ):
            if vals:
                ph = []
                for i, v in enumerate(vals):
                    k = f"{key}{i}"; params[k] = v; ph.append(f":{k}")
                parts.append(f"B.{col} IN ({','.join(ph)})")

        gate = ("ISNULL(B.DO_RECEIVED,0) < ISNULL(B.BDC_QTY,0) "
                "AND ISNULL(B.STATUS,'') <> 'CLOSED'")
        parts.append(gate)
        where = " WHERE " + " AND ".join(parts)

        sql = f"""
            SELECT TOP {int(limit)}
                {grp_sql},
                COUNT(*)                                AS rows_n,
                ISNULL(SUM(B.BDC_QTY),0)                AS bdc_qty,
                ISNULL(SUM(B.DO_RECEIVED),0)            AS do_qty,
                ISNULL(SUM(B.BDC_QTY - B.DO_RECEIVED),0) AS gap_qty,
                MAX(DATEDIFF(DAY, B.BDC_DATE, GETDATE())) AS oldest_days
            FROM {BDC_HISTORY} B WITH (NOLOCK)
            {where}
            GROUP BY {grp_sql}
            ORDER BY ISNULL(SUM(B.BDC_QTY - B.DO_RECEIVED),0) DESC
        """
        for r in conn.execute(text(sql), params).mappings().all():
            item = {c.lower(): r[c] for c in sel_cols}
            item.update({
                "rows_n":      int(r["rows_n"] or 0),
                "bdc_qty":     int(r["bdc_qty"] or 0),
                "do_qty":      int(r["do_qty"] or 0),
                "gap_qty":     int(r["gap_qty"] or 0),
                "oldest_days": int(r["oldest_days"] or 0),
            })
            items.append(item)

    cols = [c.lower() for c in sel_cols] + \
           ["rows_n", "bdc_qty", "do_qty", "gap_qty", "oldest_days"]
    totals = {"gap_qty": sum(i["gap_qty"] for i in items),
              "rows_n":  sum(i["rows_n"]  for i in items)}
    return _to_response(items, group_by, cols, totals)


# ---------------------------------------------------------------------------
# GET /parked-drift
# ---------------------------------------------------------------------------
@router.get("/parked-drift", response_model=APIResponse)
def get_parked_drift(
    request: Request,
    kind:     str = Query("both", pattern=r"^(listing|alloc|both)$"),
    min_days: int = Query(3,   ge=0, le=365),
    limit:    int = Query(500, ge=1, le=5000),
    current_user: User = Depends(get_current_user),
):
    """Surface SESSION_IDs that have been sitting in *_PARKED with
    PARK_STATUS='PENDING' for longer than min_days — never approved
    and never rejected."""
    scope = _parse_scope(request)
    tables: List[Tuple[str, str]] = []
    if kind in ("listing", "both"):
        tables.append(("listing_working", LIST_WRK_PARKED))
    if kind in ("alloc", "both"):
        tables.append(("alloc", ALLOC_PARKED))

    eng = _engine()
    items: List[Dict] = []
    with eng.connect() as conn:
        for label, tbl in tables:
            if not _table_exists(conn, tbl):
                continue
            if not _column_exists(conn, tbl, "PARK_STATUS"):
                continue
            params: Dict = {}
            extra_parts = [
                "ISNULL(PARK_STATUS,'') = 'PENDING'",
                f"DATEDIFF(DAY, PARKED_AT, GETDATE()) > {int(min_days)}",
            ]
            if scope.get("sid"):
                extra_parts.append("SESSION_ID = :p_sid")
                params["p_sid"] = scope["sid"]
            where = " WHERE " + " AND ".join(extra_parts)
            sql = f"""
                SELECT TOP {int(limit)}
                    SESSION_ID,
                    COUNT(*)                                        AS rows_n,
                    MIN(PARKED_AT)                                  AS parked_at,
                    DATEDIFF(DAY, MIN(PARKED_AT), GETDATE())        AS age_days
                FROM {tbl} WITH (NOLOCK)
                {where}
                GROUP BY SESSION_ID
                ORDER BY MIN(PARKED_AT) ASC
            """
            for r in conn.execute(text(sql), params).mappings().all():
                items.append({
                    "kind":       label,
                    "session_id": r["SESSION_ID"],
                    "rows_n":     int(r["rows_n"] or 0),
                    "parked_at":  str(r["parked_at"]) if r["parked_at"] else None,
                    "age_days":   int(r["age_days"] or 0),
                })

    items.sort(key=lambda x: -x["age_days"])
    cols = ["kind", "session_id", "rows_n", "parked_at", "age_days"]
    totals = {"rows_n": sum(i["rows_n"] for i in items)}
    return _to_response(items, "session", cols, totals)


# ---------------------------------------------------------------------------
# GET /export — Excel of any gap_type
# ---------------------------------------------------------------------------
_EXPORT_HANDLERS = {
    "excess-stk":        get_excess_stk,
    "listed-not-alloc":  get_listed_not_alloc,
    "skip-reason":       get_skip_reason,
    "hold-anomaly":      get_hold_anomaly,
    "mbq-deviation":     get_mbq_deviation,
    "pend-aging":        get_pend_aging,
    "bdc-do-reco":       get_bdc_do_reco,
    "parked-drift":      get_parked_drift,
}


@router.get("/export")
def export_gap(
    request: Request,
    gap_type: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    """Re-runs the picked endpoint with limit=5000 and ships an xlsx of the
    items array. We do the second call inside the same request to preserve
    every query param (group_by, source, side, min_days, …).
    """
    handler = _EXPORT_HANDLERS.get(gap_type)
    if not handler:
        return APIResponse(success=False, message=f"unknown gap_type: {gap_type}")

    # Build a kwargs dict from the request's query params, honoring each
    # handler's accepted signature.
    qp = request.query_params
    kwargs: Dict = {"request": request, "current_user": current_user}
    # The handler signature defines its own typed params; FastAPI normally
    # parses them. Re-implement minimal parsing for the export shortcut.
    for k in ("source", "group_by", "side", "kind", "skip_like"):
        if qp.get(k):
            kwargs[k] = qp.get(k)
    for k in ("limit", "min_days", "min_pend_days", "min_drift_days"):
        if qp.get(k):
            try: kwargs[k] = int(qp.get(k))
            except Exception: pass
    for k in ("under_factor", "over_factor"):
        if qp.get(k):
            try: kwargs[k] = float(qp.get(k))
            except Exception: pass
    # Bump limit unless the caller set one explicitly.
    kwargs.setdefault("limit", 5000)

    # Only pass kwargs the handler actually declares as parameters.
    sig_params = inspect.signature(handler).parameters
    resp = handler(**{k: v for k, v in kwargs.items() if k in sig_params})
    data = resp.data if hasattr(resp, "data") else (resp.get("data") or {})
    items = data.get("items", []) or []
    cols  = data.get("columns", []) or []

    df = pd.DataFrame(items)
    if cols:
        df = df.reindex(columns=cols)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        sheet = f"gap_{gap_type[:25]}".replace("-", "_")
        df.to_excel(xw, sheet_name=sheet, index=False)
    buf.seek(0)

    fname = f"gap_report_{gap_type}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )
