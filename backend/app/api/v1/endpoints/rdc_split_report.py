"""
Central RDC Pool — reporting (spec v1.5 §B9, Step 5).

Four read-only views over ARS_ALLOC_RDC_SPLIT. They exist because after
clubbing, "which warehouse ships this" is no longer answerable from the
store master:

    GET /listing/rdc-split/summary/{session_id}    the cockpit block (§B8.4)
    GET /listing/rdc-split/picklist/{session_id}   RDC-wise picking requirement
    GET /listing/rdc-split/dispatch/{session_id}   per store x source warehouse
    GET /listing/rdc-split/cross-ship/{session_id} freight exposure
    GET /listing/rdc-split/residual/{session_id}   what each warehouse has left

An `Own` or `Cross` session has no split rows (BR-RDC-11), so every endpoint
returns empty with `central_pool: false` rather than an error — the UI simply
does not show the block.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Query
from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine
from app.security.dependencies import get_current_user

router = APIRouter(prefix="/listing/rdc-split", tags=["listing"])

SPLIT = "ARS_ALLOC_RDC_SPLIT"
MSA_VAR = "ARS_MSA_VAR_ART"


def _rows(conn, sql: str, params: Dict[str, Any]) -> list:
    rs = conn.execute(text(sql), params)
    cols = list(rs.keys())
    return [dict(zip(cols, r)) for r in rs.fetchall()]


def _exists(conn, table: str) -> bool:
    return bool(conn.execute(text(
        f"SELECT CASE WHEN OBJECT_ID('dbo.{table}','U') IS NULL THEN 0 ELSE 1 END"
    )).scalar())


def _resolve_split(conn, session_id: str
                   ) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """Find which table holds this session's split rows.

    Since 2026-10-03 the picklist follows the same lifecycle as every other
    output, so a session's rows sit in exactly one place depending on where
    the run is in review:

        HISTORY  approved
        PARKED   awaiting approve / reject
        WORKING  the run just finished and has not parked yet

    Checked newest-state-first, because approve leaves no parked rows behind
    and reject leaves none anywhere. The WORKING fallback has no SESSION_ID
    to match on — the column only exists on the parked and history copies as
    a control column — so it is only offered when `session_id` IS the latest
    run. In practice Part 8.4 parks immediately after Part 8.37, so that
    window is a few seconds wide.

    Returns (table, sid_where, params), or None when the session has no split
    rows anywhere: an Own/Cross run, or a rejected one.
    """
    for tbl in (f"{SPLIT}_HISTORY", f"{SPLIT}_PARKED"):
        if not _exists(conn, tbl):
            continue
        if conn.execute(text(f"SELECT TOP 1 1 FROM [{tbl}] WHERE [SESSION_ID] = :s"),
                        {"s": session_id}).scalar():
            return tbl, "[SESSION_ID] = :s", {"s": session_id}
    if _exists(conn, SPLIT):
        latest = conn.execute(text(
            "SELECT MAX(SESSION_ID) FROM ARS_LISTING_SESSIONS"
        )).scalar()
        if latest == session_id and conn.execute(
                text(f"SELECT TOP 1 1 FROM [{SPLIT}]")).scalar():
            return SPLIT, "1=1", {}
    return None


@router.get("/summary/{session_id}")
def rdc_split_summary(session_id: str,
                      current_user=Depends(get_current_user)):
    """The post-run cockpit block (§B8.4).

    Reports the five figures that tell ops whether clubbing worked: how much
    each warehouse picks, how much moved between warehouses, how often a line
    had to split, whether ANY line was reduced, and how much was routed by
    fallback rather than by the store's own tag.

    `reduced_lines` is the one to watch. With two warehouses and the default
    policy it must be 0 for shipments — a non-zero value means either strict
    single-sourcing or a binding split cap took quantity away (§B6.3.3).
    """
    with get_data_engine().connect() as conn:
        src = _resolve_split(conn, session_id)
        if src is None:
            return {"success": True, "data": {"central_pool": False,
                                              "session_id": session_id}}
        tbl, sw, sp = src
        by_rdc = _rows(conn, f"""
            SELECT [SRC_RDC]                                AS src_rdc,
                   SUM(ISNULL([SHIP_QTY],0))                AS ship_qty,
                   SUM(ISNULL([HOLD_QTY],0))                AS hold_qty,
                   COUNT(*)                                 AS lines_
            FROM   [{tbl}] WHERE {sw}
            GROUP  BY [SRC_RDC] ORDER BY [SRC_RDC]
        """, sp)

        tot = _rows(conn, f"""
            SELECT COUNT(DISTINCT CAST([WERKS] AS NVARCHAR(50))
                                + '|' + CAST([VAR_ART] AS NVARCHAR(30))
                                + '|' + [SZ])               AS alloc_lines,
                   COUNT(*)                                 AS split_rows,
                   SUM(ISNULL([SHIP_QTY],0))                AS ship_qty,
                   SUM(ISNULL([HOLD_QTY],0))                AS hold_qty,
                   SUM(CASE WHEN [IS_CROSS] = 1
                            THEN ISNULL([SHIP_QTY],0) ELSE 0 END) AS cross_ship_qty,
                   COUNT(DISTINCT CASE WHEN [IS_CROSS] = 1
                                       THEN [WERKS] END)    AS cross_stores
            FROM   [{tbl}] WHERE {sw}
        """, sp)[0]

        # A split line is one store-size with more than one source row.
        split_lines = conn.execute(text(f"""
            SELECT COUNT(*) FROM (
                SELECT [WERKS], [VAR_ART], [SZ]
                FROM   [{tbl}] WHERE {sw}
                GROUP  BY [WERKS], [VAR_ART], [SZ]
                HAVING COUNT(*) > 1
            ) X
        """), sp).scalar() or 0

        by_tier = _rows(conn, f"""
            SELECT ISNULL([PREF_TIER],'?')  AS pref_tier,
                   COUNT(*)                 AS rows_,
                   SUM(ISNULL([SHIP_QTY],0)) AS ship_qty
            FROM   [{tbl}] WHERE {sw}
            GROUP  BY ISNULL([PREF_TIER],'?') ORDER BY 1
        """, sp)

        # Lines the split pass had to reduce. Stamped in ALLOC_REMARKS, so the
        # count is read from the allocation, not the split rows.
        reduced = {"lines": 0, "detail": []}
        try:
            # `_atbl`, not `tbl`: `tbl` is already bound to this session's
            # resolved SPLIT table above, and reusing the name here would
            # silently point any later split query at an alloc table.
            for _atbl in ("ARS_ALLOC_HISTORY", "ARS_ALLOC_PARKED", "ARS_ALLOC_WORKING"):
                if not conn.execute(text(
                    f"SELECT CASE WHEN OBJECT_ID('dbo.{_atbl}','U') IS NULL "
                    f"THEN 0 ELSE 1 END")).scalar():
                    continue
                sid_filter = ("WHERE [SESSION_ID] = :s AND "
                              if conn.execute(text(
                                  f"SELECT CASE WHEN COL_LENGTH('{_atbl}',"
                                  f"'SESSION_ID') IS NULL THEN 0 ELSE 1 END"
                              )).scalar() else "WHERE ")
                d = _rows(conn, f"""
                    SELECT CASE
                             WHEN [ALLOC_REMARKS] LIKE '%RDC_HOLD_SHORT%'   THEN 'RDC_HOLD_SHORT'
                             WHEN [ALLOC_REMARKS] LIKE '%RDC_SINGLE_SHORT%' THEN 'RDC_SINGLE_SHORT'
                             ELSE 'RDC_SPLIT_CAPPED'
                           END          AS reason,
                           COUNT(*)     AS lines_
                    FROM   [{_atbl}]
                    {sid_filter} ([ALLOC_REMARKS] LIKE '%RDC_SINGLE_SHORT%'
                                  OR [ALLOC_REMARKS] LIKE '%RDC_SPLIT_CAPPED%'
                                  OR [ALLOC_REMARKS] LIKE '%RDC_HOLD_SHORT%')
                    GROUP BY CASE
                             WHEN [ALLOC_REMARKS] LIKE '%RDC_HOLD_SHORT%'   THEN 'RDC_HOLD_SHORT'
                             WHEN [ALLOC_REMARKS] LIKE '%RDC_SINGLE_SHORT%' THEN 'RDC_SINGLE_SHORT'
                             ELSE 'RDC_SPLIT_CAPPED'
                           END
                """, {"s": session_id} if "SESSION_ID" in sid_filter else {})
                if d:
                    reduced = {"lines": sum(int(x["lines_"]) for x in d), "detail": d}
                    break
        except Exception as e:
            logger.warning(f"[rdc-split] reduced-line probe failed: {e}")

        ship = float(tot["ship_qty"] or 0)
        cross = float(tot["cross_ship_qty"] or 0)
        return {"success": True, "data": {
            "central_pool": True,
            "session_id": session_id,
            "by_rdc": by_rdc,
            "alloc_lines": int(tot["alloc_lines"] or 0),
            "split_rows": int(tot["split_rows"] or 0),
            "split_lines": int(split_lines),
            "ship_qty": ship,
            "hold_qty": float(tot["hold_qty"] or 0),
            "cross_ship_qty": cross,
            "cross_ship_pct": round(100.0 * cross / ship, 1) if ship else 0.0,
            "cross_stores": int(tot["cross_stores"] or 0),
            "reduced_lines": reduced["lines"],
            "reduced_detail": reduced["detail"],
            "by_pref_tier": by_tier,
        }}


@router.get("/picklist/{session_id}")
def rdc_picklist(session_id: str,
                 src_rdc: Optional[str] = Query(None, description="one warehouse"),
                 current_user=Depends(get_current_user)):
    """RDC-wise picking requirement — what each warehouse must physically pick.

    This is the document the change exists to produce. Keyed on SRC_RDC, not
    on the store's warehouse.
    """
    with get_data_engine().connect() as conn:
        src = _resolve_split(conn, session_id)
        if src is None:
            return {"success": True, "data": {"central_pool": False, "rows": []}}
        tbl, sw, sp = src
        where = f"WHERE {sw}"
        params: Dict[str, Any] = dict(sp)
        if src_rdc:
            where += " AND [SRC_RDC] = :r"
            params["r"] = src_rdc
        rows = _rows(conn, f"""
            SELECT [SRC_RDC] AS src_rdc, [MAJ_CAT] AS maj_cat,
                   [GEN_ART_NUMBER] AS gen_art_number, [CLR] AS clr,
                   [VAR_ART] AS var_art, [SZ] AS sz,
                   [ALLOC_TYPE] AS alloc_type,
                   SUM(ISNULL([SHIP_QTY],0)) AS pick_qty,
                   SUM(ISNULL([HOLD_QTY],0)) AS hold_qty
            FROM   [{tbl}] {where}
            GROUP  BY [SRC_RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR],
                      [VAR_ART], [SZ], [ALLOC_TYPE]
            HAVING SUM(ISNULL([SHIP_QTY],0)) + SUM(ISNULL([HOLD_QTY],0)) > 0
            ORDER  BY [SRC_RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR], [SZ]
        """, params)
        return {"success": True, "data": {
            "central_pool": True, "session_id": session_id,
            "row_count": len(rows),
            "pick_qty": sum(float(r["pick_qty"] or 0) for r in rows),
            "rows": rows,
        }}


@router.get("/dispatch/{session_id}")
def store_dispatch(session_id: str,
                   current_user=Depends(get_current_user)):
    """Per store x source warehouse — how many documents each store expects.

    A store served from both warehouses receives two dispatches for the same
    option, which is the operational cost of clubbing (risk R4).
    """
    with get_data_engine().connect() as conn:
        src = _resolve_split(conn, session_id)
        if src is None:
            return {"success": True, "data": {"central_pool": False, "rows": []}}
        tbl, sw, sp = src
        rows = _rows(conn, f"""
            SELECT [WERKS] AS werks, [STORE_RDC] AS store_rdc,
                   [SRC_RDC] AS src_rdc,
                   SUM(ISNULL([SHIP_QTY],0)) AS ship_qty,
                   SUM(ISNULL([HOLD_QTY],0)) AS hold_qty,
                   COUNT(*)                  AS lines_,
                   -- CAST is required: SQL Server rejects MAX() on a BIT.
                   MAX(CAST([IS_CROSS] AS INT)) AS is_cross
            FROM   [{tbl}] WHERE {sw}
            GROUP  BY [WERKS], [STORE_RDC], [SRC_RDC]
            ORDER  BY [WERKS], [SRC_RDC]
        """, sp)
        multi = conn.execute(text(f"""
            SELECT COUNT(*) FROM (
                SELECT [WERKS] FROM [{tbl}] WHERE {sw}
                GROUP BY [WERKS] HAVING COUNT(DISTINCT [SRC_RDC]) > 1
            ) X
        """), sp).scalar() or 0
        return {"success": True, "data": {
            "central_pool": True, "session_id": session_id,
            "stores_with_two_sources": int(multi),
            "rows": rows,
        }}


@router.get("/cross-ship/{session_id}")
def cross_ship_exposure(session_id: str,
                        current_user=Depends(get_current_user)):
    """Pieces shipped from a warehouse other than the store's own tag.

    Freight monitoring, and the input to the phase-4 ALC_MIN_CROSS_SHIP_QTY
    guardrail: it shows whether clubbing is generating uneconomically small
    inter-warehouse movements.
    """
    with get_data_engine().connect() as conn:
        src = _resolve_split(conn, session_id)
        if src is None:
            return {"success": True, "data": {"central_pool": False, "rows": []}}
        tbl, sw, sp = src
        rows = _rows(conn, f"""
            SELECT ISNULL([STORE_RDC],'') AS store_rdc, [SRC_RDC] AS src_rdc,
                   COUNT(DISTINCT [WERKS])   AS stores,
                   COUNT(*)                  AS lines_,
                   SUM(ISNULL([SHIP_QTY],0)) AS ship_qty,
                   AVG(ISNULL([SHIP_QTY],0)) AS avg_qty_per_line,
                   SUM(CASE WHEN ISNULL([SHIP_QTY],0) BETWEEN 1 AND 2
                            THEN 1 ELSE 0 END) AS tiny_lines
            FROM   [{tbl}]
            WHERE  {sw} AND [IS_CROSS] = 1
            GROUP  BY ISNULL([STORE_RDC],''), [SRC_RDC]
            ORDER  BY 5 DESC
        """, sp)
        return {"success": True, "data": {
            "central_pool": True, "session_id": session_id,
            "cross_ship_qty": sum(float(r["ship_qty"] or 0) for r in rows),
            "tiny_lines": sum(int(r["tiny_lines"] or 0) for r in rows),
            "rows": rows,
        }}


@router.get("/residual/{session_id}")
def residual_by_warehouse(session_id: str,
                          alloc_type: str = Query("FRESH"),
                          current_user=Depends(get_current_user)):
    """What each warehouse has left after the run, derived from the SPLIT
    LEDGER — not from RDC_FNL_Q_REM_LIVE.

    That column becomes a CLUBBED residual under central pooling (§B5), so it
    cannot answer a per-warehouse question. This computes
    `warehouse stock - taken from that warehouse` instead.
    """
    with get_data_engine().connect() as conn:
        src = _resolve_split(conn, session_id)
        if src is None:
            return {"success": True, "data": {"central_pool": False, "rows": []}}
        tbl, sw, sp = src
        rows = _rows(conn, f"""
            WITH stock AS (
                SELECT LTRIM(RTRIM(CAST([RDC] AS NVARCHAR(50)))) AS rdc,
                       SUM(TRY_CAST([FNL_Q] AS FLOAT))           AS stock_qty
                FROM   [{MSA_VAR}] WITH (NOLOCK)
                WHERE  ISNULL([ALLOC_TYPE],'FRESH') = :at
                GROUP  BY LTRIM(RTRIM(CAST([RDC] AS NVARCHAR(50))))
            ), taken AS (
                SELECT [SRC_RDC] AS rdc,
                       SUM(ISNULL([SHIP_QTY],0) + ISNULL([HOLD_QTY],0)) AS taken_qty
                FROM   [{tbl}] WHERE {sw}
                GROUP  BY [SRC_RDC]
            )
            SELECT ISNULL(s.rdc, t.rdc)                       AS rdc,
                   ISNULL(s.stock_qty, 0)                     AS stock_qty,
                   ISNULL(t.taken_qty, 0)                     AS taken_qty,
                   ISNULL(s.stock_qty,0) - ISNULL(t.taken_qty,0) AS residual_qty
            FROM   stock s
            FULL OUTER JOIN taken t ON t.rdc = s.rdc
            ORDER  BY 1
        """, {**sp, "at": alloc_type})
        return {"success": True, "data": {
            "central_pool": True, "session_id": session_id,
            "alloc_type": alloc_type, "rows": rows,
        }}
