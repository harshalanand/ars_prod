"""
1SZ ALC — Step 3: Store × article run, and the sessions it leaves behind.

For a run-time store list, every article of Step 1 that matches a short size
of the store in Step 2 becomes a candidate line; the engine
(sz1_alloc_engine) issues whole packs; the lines are written as one session
of ARS_SZ1_ST_ART_REQ. Spec: frontend/public/docs/manual/sz1_alloc.md §8.3–8.4.

The candidate and reserve SQL is the Streamlit tool's (art_store.py,
2026-09-25) with the table names changed, and three deliberate differences:

  * Warehouse mode "all" (the default, 2026-10-09): every RDC's stock of an
    article is one pool; stores of every RDC compete for it in priority
    order; each pack is drawn from the store's own RDC while it holds a
    whole pack, else from the RDC holding the most, so a line served from
    two RDCs is split into one row per source (sz1_alloc_engine._allocate_pool).
    No home reserve under "all": OWN_NEED = 0, SURPLUS_QTY = the stock.
    "same" = own RDC only. The tool's home_first / pooled / fallback are
    refused by the API (start_run / simulate / preview accept only all|same);
    they stay in _RDC_JOIN / the engine for scripts/sz1_parity.py, which
    calls candidates() / selection() and sz1_alloc_engine directly.

  * Live netting (phase 3, 2026-10-12): there is no Full / Remaining choice
    any more. Every run uses the LIVE remaining figures — warehouse stock =
    live ARS_MSA_TOTAL.FNL_Q less every PARKED session, size budget = the
    store's need less live open pending (ARS_PEND_ALC) less every PARKED
    session, full-option cap likewise per store + MAJ_CAT (manual §J P2).
    A run refreshes BAL_FNL_Q / REM_MJ_SZ_SRT first and reads them; the
    read-only preview / simulate compute the same figures in the SELECT
    ("live_calc"), so they are right even when the balances are stale.
    stock_basis is still accepted from the API but always means "live".
    "initial" (FNL_Q, MJ_SZ_SRT) is kept only for scripts/sz1_parity.py.
  * A finished run is PARKED (not DONE): it holds its allocation until it
    is approved into Pend ALC or discarded.
  * The ARS_CALC_ST_ART I_ROD override is joined on GEN_ART_NUMBER as text.
    The master holds it as BIGINT; the tool's implicit conversion would fail
    the whole run on one non-numeric article number.

A run is a background job. Lines, the allocation-limit checks on the stored
rows, the session header and the balance push are ONE transaction: if any
limit is breached the session is rolled back and marked FAILED with examples,
and nothing — balances included — changes.
"""
from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pandas as pd
from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine
from app.services import sz1_alloc_engine as E
from app.services import sz1_base_service as B
from app.services import sz1_schema as S
from app.services.sz1_base_service import NOW, jsonable, q, rows

BATCH = 20_000

# Which warehouse rows a store can see, per mode. RDC is always the store's own.
_RDC_JOIN = {
    "all": "1 = 1",
    "same": "b.RDC = r.RDC",
    "home_first": "1 = 1",
    "pooled": "1 = 1",
    # the other warehouse only where home has no such article
    "fallback": """(b.RDC = r.RDC OR NOT EXISTS (
                        SELECT 1 FROM {base} h
                        WHERE h.RDC = r.RDC AND h.ARTICLE_NUMBER = b.ARTICLE_NUMBER))""",
}

# Nested derived tables, not CTEs: the statement is used inside FROM (...).
SELECT_SQL = """
SELECT  l3.RDC, l3.SRC_RDC, l3.IS_HOME, l3.ST_CD, l3.ST_NM, l3.MAJ_CAT, l3.SZ, l3.SZ_APPLICABLE,
        l3.GEN_ART_NUMBER, l3.CLR, l3.ARTICLE_NUMBER,
        l3.ART_FNL_Q, l3.PAK_SZ, l3.ALC_D, l3.ACS_D, l3.MJ_STR, l3.MJ_SZ_SAL_PD,
        l3.CONT_PCT_SZ_REV, l3.MJ_SZ_STK, l3.MJ_SZ_MBQ, l3.MJ_SZ_SRT,
        l3.MJ_STK, l3.MJ_MBQ, l3.MJ_SRT, l3.I_ROD,
        l3.ART_DISP, l3.ART_SAL_PD, l3.ART_STK, l3.ART_MBQ,
        CAST(CASE WHEN l3.ART_MBQ - l3.ART_STK <= 0 THEN 0
                  ELSE ROUND((l3.ART_MBQ - l3.ART_STK) / NULLIF(l3.PAK_SZ, 0), 0) * l3.PAK_SZ
             END AS DECIMAL(18,2)) AS ART_SRT,
        CAST(CASE WHEN l3.I_ROD * l3.ART_MBQ - l3.ART_STK <= 0 THEN 0
                  ELSE ROUND((l3.I_ROD * l3.ART_MBQ - l3.ART_STK) / NULLIF(l3.PAK_SZ, 0), 0) * l3.PAK_SZ
             END AS DECIMAL(18,2)) AS ART_SRT_ROD
FROM (
    SELECT  l2.*,
            -- whole units like MJ_MBQ, floored at 1 wherever the article is displayed at all
            CAST(CASE
                   WHEN l2.ART_DISP > 0
                        AND ROUND(l2.ART_DISP + (l2.ART_SAL_PD * ISNULL(l2.ALC_D, 0)), 0) < 1
                        THEN 1
                   ELSE ROUND(l2.ART_DISP + (l2.ART_SAL_PD * ISNULL(l2.ALC_D, 0)), 0)
                 END AS DECIMAL(18,2)) AS ART_MBQ
    FROM (
        SELECT  l1.*, CAST(l1.ART_DISP / l1.STR_DIV AS DECIMAL(18,4)) AS ART_SAL_PD
        FROM (
            SELECT  r.RDC,
                    b.RDC AS SRC_RDC,
                    CAST(CASE WHEN b.RDC = r.RDC THEN 1 ELSE 0 END AS BIT) AS IS_HOME,
                    r.ST_CD, r.ST_NM, r.MAJ_CAT, r.SZ, r.SZ_APPLICABLE,
                    b.GEN_ART_NUMBER, b.CLR, b.ARTICLE_NUMBER,
                    CAST({stock_expr} AS DECIMAL(18,2)) AS ART_FNL_Q,
                    CAST(b.PAK_SZ AS DECIMAL(18,2)) AS PAK_SZ,
                    r.ALC_D, r.ACS_D, r.MJ_STR, r.MJ_SZ_SAL_PD,
                    r.CONT_PCT_SZ_REV, r.MJ_SZ_STK, r.MJ_SZ_MBQ, {sz_srt_expr} AS MJ_SZ_SRT,
                    r.MJ_STK, r.MJ_MBQ, {mj_srt_expr} AS MJ_SRT,
                    CAST(ISNULL(v.ART_STK, 0) AS DECIMAL(18,2)) AS ART_STK,
                    CAST(r.ACS_D * r.CONT_PCT_SZ_REV / 100.0 AS DECIMAL(18,2)) AS ART_DISP,
                    -- no usable stock-turn ratio: fall back to 90 days
                    CAST(CASE WHEN r.MJ_STR IS NULL OR r.MJ_STR <= 0 THEN 90
                              ELSE r.MJ_STR END AS DECIMAL(18,2)) AS STR_DIV,
                    -- option master over category master; A / A_MIX (CLR or SZ) floored, never lowered
                    CAST(CASE
                        WHEN (UPPER(LTRIM(RTRIM(ISNULL(b.CLR, '')))) IN ('A', 'A_MIX')
                              OR UPPER(LTRIM(RTRIM(ISNULL(b.SZ, '')))) IN ('A', 'A_MIX'))
                             AND ISNULL(NULLIF(COALESCE(ia.I_ROD, im.I_ROD), 0), {irod_default}) < {amix_floor}
                             THEN {amix_floor}
                        ELSE ISNULL(NULLIF(COALESCE(ia.I_ROD, im.I_ROD), 0), {irod_default})
                    END AS DECIMAL(18,2)) AS I_ROD
            FROM    {req} r
            JOIN    {base} b
                 ON b.MAJ_CAT = r.MAJ_CAT AND b.SZ = r.SZ
                AND {rdc_join}
            LEFT JOIN (SELECT WERKS, CAST(ARTICLE_NUMBER AS NVARCHAR(200)) AS ARTICLE_NUMBER,
                              SUM(STK_TTL) AS ART_STK
                         FROM {varart}
                        GROUP BY WERKS, CAST(ARTICLE_NUMBER AS NVARCHAR(200))) v
                 ON v.WERKS = r.ST_CD AND v.ARTICLE_NUMBER = b.ARTICLE_NUMBER
            LEFT JOIN (SELECT ST_CD, MAJ_CAT, MAX(TRY_CAST(I_ROD AS FLOAT)) AS I_ROD
                         FROM {calcmaj} GROUP BY ST_CD, MAJ_CAT) im
                 ON im.ST_CD = r.ST_CD AND im.MAJ_CAT = r.MAJ_CAT
            LEFT JOIN (SELECT ST_CD, MAJ_CAT, CAST(GEN_ART_NUMBER AS NVARCHAR(200)) AS GEN_ART_NUMBER, CLR,
                              MAX(TRY_CAST(I_ROD AS FLOAT)) AS I_ROD
                         FROM {calcart}
                        GROUP BY ST_CD, MAJ_CAT, CAST(GEN_ART_NUMBER AS NVARCHAR(200)), CLR) ia
                 ON ia.ST_CD = r.ST_CD AND ia.MAJ_CAT = r.MAJ_CAT
                AND ia.GEN_ART_NUMBER = b.GEN_ART_NUMBER AND ia.CLR = b.CLR{live_joins}
            WHERE   {sz_srt_expr} > 0
              AND   r.ST_CD IN ({codes})
        ) l1
    ) l2
) l3
"""

# Every listed store that is short, NOT only this run's: a run of DW01 stores
# must still respect what DH24's own stores need.
RESERVE_SQL = """
SELECT  n.RDC, n.ARTICLE_NUMBER, ROUND(SUM(n.NEED), 0) AS OWN_NEED
FROM (
    SELECT  c.RDC, c.ARTICLE_NUMBER,
            -- a store cannot receive more than MJ_SZ_SRT in a size, so its
            -- article needs are scaled back to fit inside it
            CASE WHEN c.SZ_TOTAL <= c.MJ_SZ_SRT THEN c.ART_SRT
                 WHEN c.SZ_TOTAL > 0 THEN c.ART_SRT * c.MJ_SZ_SRT / c.SZ_TOTAL
                 ELSE 0 END AS NEED
    FROM (
        SELECT  s.RDC, s.ARTICLE_NUMBER, s.ART_SRT, s.MJ_SZ_SRT,
                SUM(s.ART_SRT) OVER (PARTITION BY s.ST_CD, s.MAJ_CAT, s.SZ) AS SZ_TOTAL
        FROM ({sel}) s
        WHERE s.ART_SRT > 0
    ) c
) n
GROUP BY n.RDC, n.ARTICLE_NUMBER
"""

LINE_COLS: List[Tuple[str, str, int]] = [   # (column, kind s|f|i, nvarchar width)
    ("SESSION_ID", "s", 12), ("SESSION_AT", "s", 19), ("STOCK_BASIS", "s", 10), ("CROSS_RDC", "s", 12),
    ("RDC", "s", 10), ("SRC_RDC", "s", 10), ("IS_HOME", "i", 0), ("ST_CD", "s", 20), ("ST_NM", "s", 100),
    ("MAJ_CAT", "s", 200), ("SZ", "s", 50), ("SZ_APPLICABLE", "s", 1), ("GEN_ART_NUMBER", "s", 200),
    ("CLR", "s", 200), ("ARTICLE_NUMBER", "s", 200), ("ART_FNL_Q", "f", 0), ("PAK_SZ", "f", 0),
    ("ALC_D", "f", 0), ("ACS_D", "f", 0), ("MJ_STR", "f", 0), ("MJ_SZ_SAL_PD", "f", 0),
    ("CONT_PCT_SZ_REV", "f", 0), ("MJ_STK", "f", 0), ("MJ_MBQ", "f", 0), ("MJ_SRT", "f", 0),
    ("MJ_SZ_STK", "f", 0), ("MJ_SZ_MBQ", "f", 0), ("MJ_SZ_SRT", "f", 0),
    ("ART_DISP", "f", 0), ("ART_SAL_PD", "f", 0), ("ART_STK", "f", 0), ("ART_MBQ", "f", 0), ("ART_SRT", "f", 0),
    ("I_ROD", "f", 0), ("ART_SRT_ROD", "f", 0), ("ST_SEQ", "i", 0), ("PRIORITY_MODE", "s", 20),
    ("ROUND_ENVELOPE", "s", 10), ("ALLOC_QTY", "f", 0), ("ALC_I_ROD", "i", 0), ("PACKS", "i", 0),
    ("UNMET_QTY", "f", 0), ("BAL_ART_FNL_Q", "f", 0), ("REM_MJ_SZ_SRT", "f", 0),
    ("OWN_NEED", "i", 0), ("SURPLUS_QTY", "i", 0),
]


# ═══════════════════════════════════════════════════════════════════════════
#  candidates
# ═══════════════════════════════════════════════════════════════════════════
def _bind(codes: List[str]) -> Tuple[str, Dict[str, str]]:
    params = {f"c{i}": c for i, c in enumerate(codes)}
    return ", ".join(f":{k}" for k in params), params


# Stock bases inside this module (the API's choice is gone — see api_basis):
#   initial    FNL_Q / MJ_SZ_SRT / MJ_SRT as built — the Streamlit tool, for sz1_parity.py only
#   live       BAL_FNL_Q / REM_MJ_SZ_SRT as stored (a run refreshes them first) + the live MJ_SRT cap
#   live_calc  the same live figures computed in the SELECT, writing nothing (preview, simulate)
# "balance" (the old Remaining-stock choice) is read as "live".
LIVE = "live"
LIVE_CALC = "live_calc"
BASES = ("initial", LIVE, LIVE_CALC)


def api_basis(v: Any, read_only: bool = False) -> str:
    """The run's stock basis from an API value: always live (P3). Old values
    initial / balance are accepted for backward compatibility."""
    v = str(v or LIVE)
    if v not in E.STOCK_BASES and v not in (LIVE, LIVE_CALC):
        raise ValueError(f"stock_basis must be one of {', '.join([*E.STOCK_BASES, LIVE])}")
    return LIVE_CALC if read_only else LIVE


def _basis(stock_basis: str) -> str:
    return LIVE if stock_basis == "balance" else stock_basis


def _parts(stock_basis: str, parked: Optional[List[str]]) -> Dict[str, str]:
    """SQL for ART_FNL_Q, MJ_SZ_SRT, MJ_SRT under a basis (aliases b, r)."""
    basis = _basis(stock_basis)
    if basis == "initial":
        return {"stock_expr": "b.FNL_Q", "sz_srt_expr": "r.MJ_SZ_SRT", "mj_srt_expr": "r.MJ_SRT", "live_joins": ""}
    ids = list(parked or [])
    if basis == LIVE:
        return {"stock_expr": "b.BAL_FNL_Q", "sz_srt_expr": "r.REM_MJ_SZ_SRT",
                "mj_srt_expr": f"CAST({B.LIVE_MJ_SRT_EXPR} AS DECIMAL(18,2))",
                "live_joins": B.live_joins_req(ids, cat=True, size=False)}
    if basis == LIVE_CALC:
        return {"stock_expr": B.LIVE_FREE_EXPR, "sz_srt_expr": f"CAST({B.LIVE_ROOM_EXPR} AS DECIMAL(18,2))",
                "mj_srt_expr": f"CAST({B.LIVE_MJ_SRT_EXPR} AS DECIMAL(18,2))",
                "live_joins": B.live_joins_base(ids) + B.live_joins_req(ids, cat=True, size=True)}
    raise ValueError(f"stock basis must be one of {BASES}")


def selection(codes: List[str], stock_basis: str = "initial", cross_rdc: str = "same",
              parked: Optional[List[str]] = None) -> Tuple[str, Dict[str, str]]:
    """The candidate SELECT for these stores. `parked` = the parked session
    ids netted off under the live bases (B.parked_sessions)."""
    placeholders, params = _bind(codes)
    sql = SELECT_SQL.format(
        req=q(S.REQ_BASE), base=q(S.SZ_BASE), varart=q(S.GRID_VAR_ART),
        calcmaj=q(S.CALC_ST_MAJ_CAT), calcart=q(S.CALC_ST_ART),
        irod_default=E.IROD_DEFAULT, amix_floor=E.IROD_AMIX_FLOOR, codes=placeholders,
        rdc_join=_RDC_JOIN[cross_rdc].format(base=q(S.SZ_BASE)), **_parts(stock_basis, parked))
    return sql, params


def _frame(conn, sql: str, params: dict) -> pd.DataFrame:
    res = conn.execute(text(sql), params)
    return pd.DataFrame(res.fetchall(), columns=list(res.keys()))


def _room(stock_basis: str, parked: Optional[List[str]]) -> Tuple[str, str]:
    """(joins, expression) for a Step 2 row's size budget (alias r) under a basis."""
    basis = _basis(stock_basis)
    if basis == "initial":
        return "", "r.MJ_SZ_SRT"
    if basis == LIVE:
        return "", "r.REM_MJ_SZ_SRT"
    return B.live_joins_req(list(parked or []), cat=False, size=True), B.LIVE_ROOM_EXPR


def _short_stores(conn, stock_basis: str, parked: Optional[List[str]] = None) -> List[str]:
    joins, room = _room(stock_basis, parked)
    return [r[0] for r in conn.execute(text(f"""
        SELECT DISTINCT r.ST_CD FROM {q(S.REQ_BASE)} r
          JOIN {q(S.STORE_MASTER)} m ON m.ST_CD = r.ST_CD {joins}
         WHERE {room} > 0 AND m.LISTING = 1"""))]


def reserve(conn, stock_basis: str = "initial", parked: Optional[List[str]] = None) -> pd.DataFrame:
    """Per warehouse + article, how much its own listed stores already claim
    (whole units, halves up)."""
    if _basis(stock_basis) != "initial" and parked is None:
        parked = B.parked_sessions(conn)
    codes = _short_stores(conn, stock_basis, parked)
    if not codes:
        return pd.DataFrame(columns=["RDC", "ARTICLE_NUMBER", "OWN_NEED"])
    out = []
    for i in range(0, len(codes), E.MAX_STORES):           # stays under the parameter limit
        sel, params = selection(codes[i:i + E.MAX_STORES], stock_basis, "same", parked)
        out.append(_frame(conn, RESERVE_SQL.format(sel=sel), params))
    res = pd.concat(out, ignore_index=True)
    if len(out) > 1:   # a warehouse's stores can straddle chunks
        res = res.groupby(["RDC", "ARTICLE_NUMBER"], as_index=False)["OWN_NEED"].sum()
    return res


def candidates(conn, codes: List[str], stock_basis: str, cross_rdc: str,
               protect_other: bool = False) -> pd.DataFrame:
    """Candidate lines ready for the engine. Under the live bases the MAJ_CAT
    cap (MJ_SRT) is already net of live pending and every parked session."""
    parked = B.parked_sessions(conn) if _basis(stock_basis) != "initial" else None
    sql, params = selection(codes, stock_basis, cross_rdc, parked)
    raw = _frame(conn, sql, params)
    if raw.empty:
        return raw
    res = reserve(conn, stock_basis, parked) if (cross_rdc != "all" or protect_other) else None
    return prepare(raw, cross_rdc, protect_other, res)


def prepare(raw: pd.DataFrame, cross_rdc: str, protect_other: bool = False,
            res: Optional[pd.DataFrame] = None,
            taken: Optional[Dict[Tuple[str, str], float]] = None) -> pd.DataFrame:
    """Candidate lines ready for the engine. `raw` is the SELECT_SQL frame; the
    "all" frame filtered to IS_HOME = 1 is exactly the "same" frame, so a
    simulation reads the database once for every warehouse mode.

    OWN_NEED / SURPLUS_QTY: what each RDC's own listed short stores claim, and
    what is left over for another RDC's stores. Under "all" with protection
    off nothing is held back (SURPLUS_QTY = the stock)."""
    cand = raw.copy()
    if cand.empty:
        return cand
    stock_q = pd.to_numeric(cand["ART_FNL_Q"], errors="coerce").fillna(0.0)
    if cross_rdc == "all":
        # One pool, and one pack size per article so a split line is whole
        # packs in every source row; entitlements re-derived on it.
        pak = pd.to_numeric(cand["PAK_SZ"], errors="coerce").fillna(1.0)
        cand["PAK_SZ"] = pak.groupby(cand["ARTICLE_NUMBER"]).transform("max")
        mbq = pd.to_numeric(cand["ART_MBQ"], errors="coerce").fillna(0.0)
        stk = pd.to_numeric(cand["ART_STK"], errors="coerce").fillna(0.0)
        irod = pd.to_numeric(cand["I_ROD"], errors="coerce").fillna(1.0)
        cand["ART_SRT"] = [E.pack_round(m - s, p) for m, s, p in zip(mbq, stk, cand["PAK_SZ"])]
        cand["ART_SRT_ROD"] = [E.pack_round(i * m - s, p) for i, m, s, p in zip(irod, mbq, stk, cand["PAK_SZ"])]
    if res is not None and (cross_rdc != "all" or protect_other):
        r = res.rename(columns={"RDC": "SRC_RDC"})[["SRC_RDC", "ARTICLE_NUMBER", "OWN_NEED"]]
        cand = cand.drop(columns=[c for c in ("OWN_NEED",) if c in cand.columns]) \
                   .merge(r, on=["SRC_RDC", "ARTICLE_NUMBER"], how="left")
        cand["OWN_NEED"] = pd.to_numeric(cand["OWN_NEED"], errors="coerce").fillna(0.0)
    else:
        cand["OWN_NEED"] = 0.0
    cand["SURPLUS_QTY"] = (stock_q.values - cand["OWN_NEED"]).clip(lower=0).round()
    cand["OWN_NEED"] = cand["OWN_NEED"].round().astype(int)
    cand["SURPLUS_QTY"] = cand["SURPLUS_QTY"].astype(int)
    if taken:
        # remaining-stock run: the full-option cap is the MAJ_CAT shortage still open
        mj = pd.to_numeric(cand["MJ_SRT"], errors="coerce").fillna(0.0)
        t = [taken.get((s, m), 0.0) for s, m in zip(cand["ST_CD"], cand["MAJ_CAT"])]
        cand["MJ_SRT"] = (mj - pd.Series(t, index=cand.index)).clip(lower=0)
    return cand


# ═══════════════════════════════════════════════════════════════════════════
#  store list
# ═══════════════════════════════════════════════════════════════════════════
def validate_stores(codes: List[str], stock_basis: str = LIVE) -> List[Dict[str, Any]]:
    """Each supplied code: in the store master? listed? anything short — on
    the live room (live open pending and every parked session netted off,
    computed here, nothing written)? Any API stock_basis means live."""
    if not codes:
        return []
    if len(codes) > E.MAX_STORES:
        raise ValueError(f"{len(codes):,} stores supplied; the limit is {E.MAX_STORES:,}.")
    api_basis(stock_basis)
    S.ensure_tables()
    _p, params = _bind(codes)
    values = ", ".join(f"(:{k})" for k in params)
    with data_engine.connect() as conn:
        joins, room = _room(LIVE_CALC, B.parked_sessions(conn))
        return rows(conn, f"""
            WITH given AS (SELECT DISTINCT ST_CD FROM (VALUES {values}) v(ST_CD)),
            short AS (SELECT r.ST_CD, COUNT(*) AS short_rows, SUM(x.ROOM) AS short_qty
                        FROM {q(S.REQ_BASE)} r
                        JOIN given gg ON gg.ST_CD = r.ST_CD {joins}
                       CROSS APPLY (SELECT {room} AS ROOM) x
                       WHERE x.ROOM > 0 GROUP BY r.ST_CD)
            SELECT  g.ST_CD, m.ST_NM, m.RDC,
                    CASE WHEN m.ST_CD IS NULL          THEN 'not in store master'
                         WHEN m.LISTING <> 1           THEN 'not listed (LISTING <> 1)'
                         WHEN ISNULL(s.short_rows,0)=0 THEN 'listed, nothing short'
                         ELSE 'ok' END AS STATUS,
                    ISNULL(s.short_rows, 0) AS SHORT_ROWS,
                    ISNULL(s.short_qty, 0) AS SHORT_QTY
            FROM    given g
            LEFT JOIN {q(S.STORE_MASTER)} m ON m.ST_CD = g.ST_CD
            LEFT JOIN short s ON s.ST_CD = g.ST_CD
            ORDER BY CASE WHEN m.ST_CD IS NULL THEN 0 ELSE 1 END, g.ST_CD""", params)


def _ok_codes(codes: List[str], stock_basis: str) -> Tuple[List[str], List[Dict[str, Any]], List[Dict[str, Any]]]:
    checked = validate_stores(codes, stock_basis)
    ok = [r["ST_CD"] for r in checked if r["STATUS"] == "ok"]
    skipped = [r for r in checked if r["STATUS"] != "ok"]
    return ok, skipped, checked


def preview(codes: List[str], stock_basis: str = LIVE, cross_rdc: str = "all", top: int = 100) -> Dict[str, Any]:
    """Per store: lines, articles, short qty, I_ROD effect; and the first rows.
    On the live figures, computed in the SELECT. Nothing is written."""
    basis = api_basis(stock_basis, read_only=True)
    _check_choice(cross_rdc, E.CROSS_RDC_MODES, "cross_rdc")   # legacy modes: sz1_parity.py only
    ok, skipped, checked = _ok_codes(codes, basis)
    if not ok:
        return {"stores": checked, "per_store": [], "sample": [], "skipped": skipped}
    with data_engine.connect() as conn:
        sql, params = selection(ok, basis, cross_rdc, B.parked_sessions(conn))
        per_store = rows(conn, f"""
            SELECT ST_CD, MIN(RDC) AS RDC, COUNT(*) AS LINES_,
                   COUNT(DISTINCT ARTICLE_NUMBER) AS ARTICLES, COUNT(DISTINCT MAJ_CAT) AS MAJ_CATS,
                   SUM(CASE WHEN IS_HOME = 0 THEN 1 ELSE 0 END) AS OTHER_RDC_LINES,
                   MAX(I_ROD) AS MAX_I_ROD, SUM(ART_SRT) AS ART_SRT_1_ROUND,
                   SUM(ART_SRT_ROD) AS ART_SRT_ALL_ROUNDS,
                   SUM(CASE WHEN ART_SRT <= 0 AND ART_SRT_ROD > 0 THEN 1 ELSE 0 END) AS ADMITTED_BY_I_ROD
              FROM ({sql}) x GROUP BY ST_CD ORDER BY ST_CD""", params)
        sample = rows(conn, f"SELECT TOP ({int(top)}) * FROM ({sql}) x "
                            "ORDER BY ST_CD, MAJ_CAT, SZ, ARTICLE_NUMBER", params)
    return {"stores": checked, "per_store": per_store, "sample": sample, "skipped": skipped}


# ═══════════════════════════════════════════════════════════════════════════
#  run
# ═══════════════════════════════════════════════════════════════════════════
def _check_choice(v: str, allowed: dict, name: str) -> None:
    if v not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}")


def gates() -> Dict[str, Any]:
    """Every reason a run may not start now, and the warnings it would carry —
    the readiness checklist for a run (sz1_readiness): sources, a job already
    running, Steps 1 and 2 built and in order, stale sources, rule changes,
    coverage gaps, sessions held in the balances, balance drift."""
    from app.services import sz1_readiness
    r, ctx = sz1_readiness.check_with_context("run")
    return {"ok": r["ok"], "block": r["block"], "warn": r["warn"], "step1": ctx["step1"], "step2": ctx["step2"],
            "balance_sessions": ctx["balance_sessions"], "readiness": r}


def _new_session_id(conn, now: datetime) -> str:
    for fmt in ("%d%m%y%H%M", "%d%m%y%H%M%S"):
        sid = now.strftime(fmt)
        if not conn.execute(text(f"SELECT 1 FROM {q(S.SESSION)} WHERE SESSION_ID = :s"), {"s": sid}).fetchone():
            return sid
    raise RuntimeError("Another session was saved this very second. Try again.")


def _update(sid: str, **cols) -> None:
    sets, params = B.set_clause(cols)
    with data_engine.begin() as conn:
        conn.execute(text(f"UPDATE {q(S.SESSION)} SET {sets} WHERE SESSION_ID = :id"), {**params, "id": sid})


def _progress(sid: str, pct: int, msg: str, **extra) -> None:
    _update(sid, PROGRESS_PCT=pct, PROGRESS=json.dumps({"text": msg, **extra}, default=str)[:400])


def start_run(body: Dict[str, Any], user: Optional[str]) -> str:
    """Validate, claim the job slot, write the RUNNING header, start the thread."""
    codes = E.parse_store_codes(" ".join(body.get("codes") or []) if isinstance(body.get("codes"), list)
                                else body.get("codes") or "")
    mode = str(body.get("mode") or "weighted")
    # P3: no Full / Remaining choice — every run nets live (any value accepted)
    stock_basis = api_basis(body.get("stock_basis"))
    cross_rdc = str(body.get("cross_rdc") or "all")
    # All RDC only (manual §D): hold back what the other RDC's own stores need,
    # and fill a line from one warehouse where it can. Both on by default.
    protect_other = _flag(body.get("protect_other"), True)
    one_source = _flag(body.get("one_source"), True)
    round_envelope = "none"          # fixed: only the article target grows per round (FSD §8.3)
    _check_choice(mode, E.PRIORITY_MODES, "mode")
    _check_choice(cross_rdc, E.CROSS_RDC_MODES, "cross_rdc")   # legacy modes: sz1_parity.py only
    try:
        weight = min(max(float(body.get("weight_mj", 0.5)), 0.0), 1.0)
    except (TypeError, ValueError):
        raise ValueError("weight_mj must be a number between 0 and 1") from None
    sequence = E.parse_sequence(body.get("sequence") or "") if mode == "sequence" else None
    if mode == "sequence" and not sequence:
        raise ValueError("Own sequence was chosen but no store order was given.")
    if not codes:
        raise ValueError("No store codes supplied.")
    if len(codes) > E.MAX_STORES:
        raise ValueError(f"{len(codes):,} stores supplied; the limit is {E.MAX_STORES:,}.")

    S.ensure_tables()
    with B.job_slot("run"):
        g = gates()
        if not g["ok"]:
            raise RuntimeError(" ".join(g["block"]))
        ok, skipped, _ = _ok_codes(codes, stock_basis)
        if not ok:
            raise ValueError("None of those stores have anything short. Nothing to allocate.")
        now = datetime.now().replace(microsecond=0)
        with data_engine.begin() as conn:
            sid = _new_session_id(conn, now)
            conn.execute(text(f"""
                INSERT INTO {q(S.SESSION)}
                    (SESSION_ID, SESSION_AT, STATUS, PRIORITY_MODE, WEIGHT_MJ, ROUND_ENVELOPE, STOCK_BASIS,
                     CROSS_RDC, BASE_BUILD_ID, REQ_BUILD_ID, STORES_GIVEN, STORES_OK, STORE_LIST,
                     SKIPPED_JSON, PROGRESS, PROGRESS_PCT, NOTE, CREATED_BY)
                VALUES (:sid, :at, 'RUNNING', :mode, :w, :env, :basis, :cross, :b1, :b2, :given, :ok,
                        :list, :skipped, :prog, 0, :note, :by)"""),
                {"sid": sid, "at": now, "mode": mode, "w": weight if mode == "weighted" else None,
                 "env": round_envelope, "basis": stock_basis, "cross": cross_rdc,
                 "b1": g["step1"]["BUILD_ID"], "b2": g["step2"]["BUILD_ID"], "given": len(codes), "ok": len(ok),
                 "list": json.dumps({"stores": ok, "sequence": sequence,
                                     "options": _options(cross_rdc, protect_other, one_source, mode, weight,
                                                         stock_basis)}),
                 "skipped": json.dumps(skipped, default=str),
                 "prog": json.dumps({"text": "Starting"}), "note": (body.get("note") or "")[:200] or None,
                 "by": user})
        opts = {"mode": mode, "weight": weight, "sequence": sequence, "stock_basis": stock_basis,
                "cross_rdc": cross_rdc, "round_envelope": round_envelope,
                "protect_other": protect_other, "one_source": one_source,
                "readiness": list(g["warn"])}       # kept on the session summary
        B.start_job("run", sid, _run, (sid, now, ok, opts, user))
    return sid


def _flag(v, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _options(cross_rdc, protect_other, one_source, mode, weight, stock_basis) -> Dict[str, Any]:
    """The run's choices as kept on the session (STORE_LIST at start, summary at the end)."""
    pooled = cross_rdc == "all"
    return {"cross_rdc": cross_rdc, "protect_other": bool(protect_other) if pooled else None,
            "one_source": bool(one_source) if pooled else None, "mode": mode,
            "weight_mj": weight if mode == "weighted" else None, "stock_basis": stock_basis}


DEFAULT_VARIANTS = [
    {"key": "own", "cross_rdc": "same"},
    {"key": "all", "cross_rdc": "all", "protect_other": True, "one_source": True},
]


def _variant_label(v: Dict[str, Any]) -> str:
    if v["cross_rdc"] == "same":
        return "Own RDC"
    bits = ["protected" if v["protect_other"] else "unprotected"]
    if v["one_source"]:
        bits.append("one source per line")
    return "All RDC · " + ", ".join(bits)


def simulate(body: Dict[str, Any]) -> Dict[str, Any]:
    """Run the engine in memory for several warehouse modes on the same store
    list and settings, and compare them. Writes nothing. The database is read
    once (the All RDC frame; Own RDC is its home rows) plus the reserve."""
    t0 = time.time()
    codes = E.parse_store_codes(" ".join(body.get("codes") or []) if isinstance(body.get("codes"), list)
                                else body.get("codes") or "")
    mode = str(body.get("mode") or "weighted")
    # P3: the live figures, computed in the SELECT — right even when the stored
    # balances are stale, and nothing is written
    stock_basis = api_basis(body.get("stock_basis"), read_only=True)
    _check_choice(mode, E.PRIORITY_MODES, "mode")
    try:
        weight = min(max(float(body.get("weight_mj", 0.5)), 0.0), 1.0)
    except (TypeError, ValueError):
        raise ValueError("weight_mj must be a number between 0 and 1") from None
    sequence = E.parse_sequence(body.get("sequence") or "") if mode == "sequence" else None
    if mode == "sequence" and not sequence:
        raise ValueError("Own sequence was chosen but no store order was given.")
    if not codes:
        raise ValueError("No store codes supplied.")
    variants = []
    for i, v in enumerate(body.get("variants") or DEFAULT_VARIANTS):
        cross = str(v.get("cross_rdc") or "all")
        _check_choice(cross, E.CROSS_RDC_MODES, "cross_rdc")
        variants.append({"key": str(v.get("key") or f"v{i + 1}"), "cross_rdc": cross,
                         "protect_other": _flag(v.get("protect_other"), True) if cross == "all" else False,
                         "one_source": _flag(v.get("one_source"), True) if cross == "all" else False})
    if len({v["key"] for v in variants}) != len(variants) or len(variants) > 4:
        raise ValueError("Give up to 4 variants, each with its own key.")
    for v in variants:
        v["label"] = _variant_label(v)

    S.ensure_tables()
    ok, skipped, checked = _ok_codes(codes, stock_basis)
    if not ok:
        return {"variants": [], "per_store": [], "stores": checked, "skipped": skipped}
    with data_engine.connect() as conn:
        parked = B.parked_sessions(conn)
        sql, params = selection(ok, stock_basis, "all", parked)
        raw = _frame(conn, sql, params)
        need_res = any(v["cross_rdc"] != "all" or v["protect_other"] for v in variants)
        res = reserve(conn, stock_basis, parked) if need_res and not raw.empty else None
        taken = None                    # MJ_SRT is already net of live pending + parked
        _p, sp = _bind(ok)
        joins, room = _room(stock_basis, parked)
        need = rows(conn, f"""
            SELECT r.ST_CD, MAX(m.ST_NM) AS ST_NM, MAX(m.RDC) AS RDC, SUM({room}) AS SZ_SRT
              FROM {q(S.REQ_BASE)} r WITH (NOLOCK)
              JOIN {q(S.STORE_MASTER)} m ON m.ST_CD = r.ST_CD {joins}
             WHERE r.ST_CD IN ({_p}) GROUP BY r.ST_CD""", sp)
    read_s = round(time.time() - t0, 1)
    total_need = float(sum(float(r["SZ_SRT"] or 0) for r in need))
    per_store = {r["ST_CD"]: {**r, "SZ_SRT": float(r["SZ_SRT"] or 0)} for r in need}

    out_variants = []
    for v in variants:
        t1 = time.time()
        frame = raw if v["cross_rdc"] == "all" else raw[raw["IS_HOME"].astype(int) == 1]
        cand = prepare(frame, v["cross_rdc"], v["protect_other"], res, taken)
        if cand.empty:
            totals = {k: 0 for k in ("ALLOC_QTY", "PACKS", "LINES_SERVED", "STORES_SERVED", "SPLIT_LINES",
                                     "CROSS_QTY", "ASK_QTY_ROD")}
            got = {}
        else:
            cand = E.add_priority(cand, mode, sequence, weight)
            res_df = E.allocate(cand, "none", v["cross_rdc"], protect_other=v["protect_other"],
                                one_source=v["one_source"])
            t = _summary_totals(res_df)
            totals = {k: t[k] for k in ("ALLOC_QTY", "PACKS", "LINES_SERVED", "STORES_SERVED", "SPLIT_LINES",
                                         "CROSS_QTY", "ASK_QTY_ROD")}
            got = res_df.groupby("ST_CD")["ALLOC_QTY"].sum().to_dict()
        totals["SZ_SRT"] = total_need
        totals["FILL_PCT"] = round(totals["ALLOC_QTY"] / total_need * 100, 2) if total_need else 0.0
        for st, row in per_store.items():
            row[v["key"]] = float(got.get(st, 0.0))
        out_variants.append({**v, "totals": totals, "seconds": round(time.time() - t1, 1)})
    return {"variants": out_variants, "per_store": sorted(per_store.values(), key=lambda r: r["ST_CD"]),
            "stores": checked, "skipped": skipped, "read_seconds": read_s,
            "stock_basis": LIVE, "parked_sessions": parked,
            "seconds": round(time.time() - t0, 1)}


def cancel(session_id: str) -> bool:
    return B.cancel_job("run", session_id)


def _run(sid: str, now: datetime, codes: List[str], o: Dict[str, Any], user: Optional[str],
         ev: threading.Event) -> None:
    t0 = time.time()
    steps: Dict[str, float] = {}
    try:
        # live netting (P2/P3): bring BAL_FNL_Q / REM_MJ_SZ_SRT up to the live
        # MSA, open Pend ALC and every parked session first (short transaction),
        # then read the candidates from them; _write re-applies them with this run.
        _progress(sid, 2, "Refreshing the live remaining stock and size budgets")
        t_b = time.time()
        with data_engine.begin() as conn:
            o["parked"] = B.push_balances(conn)
        steps["refresh"] = round(time.time() - t_b, 1)
        _progress(sid, 3, f"Reading candidate lines for {len(codes):,} store(s)")
        with data_engine.connect() as conn:
            cand = candidates(conn, codes, o["stock_basis"], o["cross_rdc"], o.get("protect_other", False))
        steps["read"] = round(time.time() - t0, 1)
        if cand.empty:
            raise ValueError("No store × article combinations found for those stores.")
        if ev.is_set():
            raise E.Cancelled()

        _progress(sid, 25, f"Ranking stores ({len(cand):,} candidate lines)", lines=len(cand))
        cand = E.add_priority(cand, o["mode"], o["sequence"], o["weight"])

        t1 = time.time()

        def prog(p):
            pct = 30 + int(p["pass"] / max(p["passes"], 1) * 40)
            word = "full options" if p["full_opt"] else "real sizes"
            _progress(sid, pct, f"Allocating — {word}, round {p['round']}", units=p["units"], lines=len(cand))

        out = E.allocate(cand, o["round_envelope"], o["cross_rdc"], progress=prog, cancelled=ev.is_set,
                         protect_other=o.get("protect_other", False), one_source=o.get("one_source", False))
        steps["allocate"] = round(time.time() - t1, 1)
        if ev.is_set():
            raise E.Cancelled()

        out["PRIORITY_MODE"] = o["mode"]
        out["ROUND_ENVELOPE"] = o["round_envelope"]
        out["STOCK_BASIS"] = o["stock_basis"]
        out["CROSS_RDC"] = o["cross_rdc"]
        out["SESSION_ID"] = sid
        out["SESSION_AT"] = now.strftime("%Y-%m-%d %H:%M:%S")

        summary = _summary(out)
        summary["steps"] = steps
        summary["readiness"] = list(o.get("readiness") or [])     # the warnings the run started with
        summary["options"] = _options(o["cross_rdc"], o.get("protect_other", False), o.get("one_source", False),
                                      o["mode"], o["weight"], o["stock_basis"])
        _progress(sid, 72, f"Writing {len(out):,} lines (one transaction)", lines=len(out))
        try:
            _write(sid, out, ev, steps, summary, t0)
        except _Breach as b:
            bad = [c["name"] for c in b.checks if not c["ok"]]
            _update(sid, STATUS="FAILED", CHECKS_PASSED=0, CHECKS_JSON=json.dumps(b.checks, default=str),
                    PROGRESS=json.dumps({"text": "Allocation limits breached — rolled back, nothing written"}),
                    ERROR="Breached: " + "; ".join(bad), FINISHED_AT=NOW,
                    DURATION_SEC=round(time.time() - t0, 1))
            logger.error(f"[sz1 run] session {sid} rolled back: {bad}")
            return
        logger.info(f"[sz1 run] session {sid} by {user}: {len(out):,} lines, "
                    f"{summary['totals']['ALLOC_QTY']:,.0f} units in {time.time() - t0:.1f}s")
    except E.Cancelled:
        _update(sid, STATUS="CANCELLED", PROGRESS=json.dumps({"text": "Cancelled — nothing written"}),
                FINISHED_AT=NOW, DURATION_SEC=round(time.time() - t0, 1))
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[sz1 run] session {sid} failed")
        _update(sid, STATUS="FAILED", PROGRESS=json.dumps({"text": f"Failed — nothing written: {str(e)[:200]}"}),
                ERROR=f"{e}\n{traceback.format_exc()}"[:8000], FINISHED_AT=NOW,
                DURATION_SEC=round(time.time() - t0, 1))


def _val(v, kind: str):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if kind == "s":
        return str(v)
    if kind == "i":
        return int(v)
    return float(v)


class _Breach(Exception):
    """An allocation limit failed on the stored rows: the transaction rolls back."""

    def __init__(self, checks: List[Dict[str, Any]]):
        super().__init__("allocation limits breached")
        self.checks = checks


def _write(sid: str, out: pd.DataFrame, ev: threading.Event, steps: Dict[str, float],
           summary: Dict[str, Any], started: float) -> None:
    """Lines, the limit checks on the stored rows, the balance push and the
    header — all in one transaction. A breached limit raises _Breach, which
    rolls everything back. The header turns PARKED last, so the row pollers
    read is locked only for the final statement."""
    import pyodbc
    names = [c for c, _, _ in LINE_COLS]
    sizes = [(pyodbc.SQL_WVARCHAR, w, 0) if k == "s" else (pyodbc.SQL_DOUBLE, 0, 0) if k == "f"
             else (pyodbc.SQL_BIGINT, 0, 0) for _, k, w in LINE_COLS]
    sql = (f"INSERT INTO {q(S.LINES)} ({', '.join(f'[{n}]' for n in names)}) "
           f"VALUES ({', '.join('?' for _ in names)})")
    kinds = [k for _, k, _ in LINE_COLS]
    data = out[names]
    basis = str(out["STOCK_BASIS"].iloc[0])
    t = time.time()
    with data_engine.begin() as conn:
        cur = conn.connection.cursor()
        cur.fast_executemany = True
        total = len(data)
        for i in range(0, total, BATCH):
            if ev.is_set():
                raise E.Cancelled()
            part = [tuple(_val(v, k) for v, k in zip(rec, kinds))
                    for rec in data.iloc[i:i + BATCH].itertuples(index=False, name=None)]
            cur.setinputsizes(sizes)
            cur.executemany(sql, part)
            _progress(sid, 72 + int((i + len(part)) / max(total, 1) * 18),
                      f"Writing lines {i + len(part):,} of {total:,}", lines=total)
        cur.fast_executemany = False
        cur.close()
        steps["write"] = round(time.time() - t, 1)

        _progress(sid, 92, "Checking the allocation limits on the stored rows")
        t = time.time()
        checks = check_stored(conn, sid, expect_rows=total, expect_qty=float(out["ALLOC_QTY"].sum()))
        steps["check"] = round(time.time() - t, 1)
        if not all(c["ok"] for c in checks):
            raise _Breach(checks)

        _progress(sid, 96, "Re-applying balances to the Step 1 and Step 2 tables")
        t = time.time()
        summary["balance_sessions"] = B.push_balances(conn, pending=(sid, basis))   # every parked + this one
        steps["balances"] = round(time.time() - t, 1)

        tot = _summary_totals(out)
        conn.execute(text(f"""
            UPDATE {q(S.SESSION)}
               SET STATUS = 'PARKED', ROWS_WRITTEN = :rows, STORES_SERVED = :served, LINES_SERVED = :lines,
                   ASK_QTY = :ask, ASK_QTY_ROD = :askr, ALLOC_QTY = :alloc, PACKS = :packs,
                   CROSS_QTY = :cross, CHECKS_PASSED = 1, CHECKS_JSON = :checks, SUMMARY_JSON = :summary,
                   PROGRESS_PCT = 100, PROGRESS = :prog, FINISHED_AT = SYSDATETIME(), DURATION_SEC = :dur
             WHERE SESSION_ID = :sid"""),
            {"rows": total, "served": tot["STORES_SERVED"], "lines": tot["LINES_SERVED"],
             "ask": tot["ASK_QTY"], "askr": tot["ASK_QTY_ROD"], "alloc": tot["ALLOC_QTY"],
             "packs": tot["PACKS"], "cross": tot["CROSS_QTY"], "checks": json.dumps(checks, default=str),
             "summary": json.dumps(summary, default=str), "dur": round(time.time() - started, 1),
             "prog": json.dumps({"text": f"{tot['ALLOC_QTY']:,.0f} units in {tot['PACKS']:,} packs to "
                                         f"{tot['STORES_SERVED']:,} store(s)"}), "sid": sid})


# ═══════════════════════════════════════════════════════════════════════════
#  checks — FSD §9, on the stored rows of one session
# ═══════════════════════════════════════════════════════════════════════════
def _check_defs(sid_param: str = ":sid") -> List[Tuple[str, str, str]]:
    L = q(S.LINES)
    w = f"WHERE SESSION_ID = {sid_param}"
    return [
        ("size_budget", "Size budget MJ_SZ_SRT never exceeded", f"""
            SELECT TOP 6 ST_CD + ' · ' + MAJ_CAT + ' · ' + SZ AS K, SUM(ALLOC_QTY) AS GOT, MAX(MJ_SZ_SRT) AS LIMIT_
              FROM {L} {w} GROUP BY ST_CD, MAJ_CAT, SZ HAVING SUM(ALLOC_QTY) > MAX(MJ_SZ_SRT) + 0.005"""),
        ("entitlement", "No store + article above its entitlement ART_SRT_ROD", f"""
            SELECT TOP 6 ST_CD + ' · ' + ARTICLE_NUMBER AS K, SUM(ALLOC_QTY) AS GOT, MAX(ART_SRT_ROD) AS LIMIT_
              FROM {L} {w} GROUP BY ST_CD, ARTICLE_NUMBER HAVING SUM(ALLOC_QTY) > MAX(ART_SRT_ROD) + 0.005"""),
        ("oversold", "No warehouse oversold", f"""
            SELECT TOP 6 SRC_RDC + ' · ' + ARTICLE_NUMBER AS K, SUM(ALLOC_QTY) AS GOT, MAX(ART_FNL_Q) AS LIMIT_
              FROM {L} {w} GROUP BY SRC_RDC, ARTICLE_NUMBER HAVING SUM(ALLOC_QTY) > MAX(ART_FNL_Q) + 0.005"""),
        ("surplus", "Other-RDC stores took no more than the surplus", f"""
            SELECT TOP 6 SRC_RDC + ' · ' + ARTICLE_NUMBER AS K,
                   SUM(CASE WHEN IS_HOME = 0 THEN ALLOC_QTY ELSE 0 END) AS GOT, MAX(SURPLUS_QTY) AS LIMIT_
              FROM {L} {w} GROUP BY SRC_RDC, ARTICLE_NUMBER
            HAVING SUM(CASE WHEN IS_HOME = 0 THEN ALLOC_QTY ELSE 0 END) > MAX(SURPLUS_QTY) + 0.005"""),
        ("zero_surplus", "No other-RDC store served from zero surplus", f"""
            SELECT TOP 6 ST_CD + ' · ' + ARTICLE_NUMBER + ' from ' + SRC_RDC AS K, ALLOC_QTY AS GOT,
                   SURPLUS_QTY AS LIMIT_
              FROM {L} {w} AND IS_HOME = 0 AND ISNULL(SURPLUS_QTY, 0) <= 0 AND ALLOC_QTY > 0"""),
        ("whole_packs", "Every allocation a whole number of packs", f"""
            SELECT TOP 6 ST_CD + ' · ' + ARTICLE_NUMBER AS K, ALLOC_QTY AS GOT, PAK_SZ AS LIMIT_
              FROM {L} {w} AND ALLOC_QTY > 0
               AND (ISNULL(PAK_SZ, 0) <= 0 OR ABS(ALLOC_QTY - ROUND(ALLOC_QTY / PAK_SZ, 0) * PAK_SZ) > 0.005)"""),
        ("negative", "No negative allocation", f"""
            SELECT TOP 6 ST_CD + ' · ' + ARTICLE_NUMBER AS K, ALLOC_QTY AS GOT, 0 AS LIMIT_
              FROM {L} {w} AND ALLOC_QTY < 0"""),
        ("no_entitlement", "Nothing allocated without an entitlement", f"""
            SELECT TOP 6 ST_CD + ' · ' + ARTICLE_NUMBER AS K, ALLOC_QTY AS GOT, ART_SRT_ROD AS LIMIT_
              FROM {L} {w} AND ALLOC_QTY > 0 AND ISNULL(ART_SRT_ROD, 0) <= 0"""),
        ("full_option", "Full options (SZ A / NA) kept within MJ_SRT", f"""
            SELECT TOP 6 ST_CD + ' · ' + MAJ_CAT AS K, SUM(ALLOC_QTY) AS GOT, MAX(MJ_SRT) AS LIMIT_
              FROM {L} {w} GROUP BY ST_CD, MAJ_CAT
            HAVING SUM(CASE WHEN UPPER(LTRIM(RTRIM(SZ))) IN ('A', 'NA') THEN ALLOC_QTY ELSE 0 END) > 0
               AND SUM(ALLOC_QTY) > MAX(MJ_SRT) + 0.005"""),
    ]


def check_stored(conn, sid: str, expect_rows: Optional[int] = None,
                 expect_qty: Optional[float] = None) -> List[Dict[str, Any]]:
    out = []
    for key, name, sql in _check_defs():
        ex = rows(conn, sql, {"sid": sid})
        out.append({"key": key, "name": name, "ok": not ex, "examples": ex})
    if expect_rows is not None:
        n, qty = conn.execute(text(f"SELECT COUNT(*), ISNULL(SUM(ALLOC_QTY), 0) FROM {q(S.LINES)} "
                                   "WHERE SESSION_ID = :sid"), {"sid": sid}).fetchone()
        ok = int(n) == int(expect_rows) and abs(float(qty) - float(expect_qty or 0)) < 0.01
        out.append({"key": "stored", "name": "Stored rows match the run", "ok": ok,
                    "examples": [] if ok else [{"K": "rows · units", "GOT": f"{n} · {qty}",
                                                "LIMIT_": f"{expect_rows} · {expect_qty}"}]})
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  summaries — computed once from the in-memory result and kept on the header
# ═══════════════════════════════════════════════════════════════════════════
def _summary_totals(df: pd.DataFrame) -> Dict[str, Any]:
    a = df["ALLOC_QTY"]
    srcs = df[a > 0].groupby(["ST_CD", "ARTICLE_NUMBER"])["SRC_RDC"].nunique()
    return {"SPLIT_LINES": int((srcs > 1).sum()),       # store × article served from 2+ RDCs
            "ROWS": int(len(df)), "STORES": int(df["ST_CD"].nunique()),
            "STORES_SERVED": int(df.loc[a > 0, "ST_CD"].nunique()), "LINES_SERVED": int((a > 0).sum()),
            "ASK_QTY": float(df["ART_SRT"].sum()), "ASK_QTY_ROD": float(df["ART_SRT_ROD"].sum()),
            "ALLOC_QTY": float(a.sum()), "PACKS": int(df["PACKS"].sum()),
            "CROSS_QTY": float(a[df["IS_HOME"].astype(int) == 0].sum()),
            "SZ_SRT": float(df.groupby(["ST_CD", "MAJ_CAT", "SZ"])["MJ_SZ_SRT"].max().sum())}


def _summary(df: pd.DataFrame) -> Dict[str, Any]:
    d = df.copy()
    d["IS_HOME"] = d["IS_HOME"].astype(int)
    d["HOME_QTY"] = d["ALLOC_QTY"].where(d["IS_HOME"] == 1, 0.0)
    d["CROSS_QTY"] = d["ALLOC_QTY"].where(d["IS_HOME"] == 0, 0.0)
    d["SERVED"] = (d["ALLOC_QTY"] > 0).astype(int)
    d["BY_IROD"] = ((d["ART_SRT"] <= 0) & (d["ART_SRT_ROD"] > 0)).astype(int)

    per_store = d.groupby("ST_CD").agg(
        ST_NM=("ST_NM", "first"), RDC=("RDC", "first"), AVG_SEQ=("ST_SEQ", "mean"), LINES=("ST_CD", "size"),
        ARTICLES=("ARTICLE_NUMBER", "nunique"), MAJ_CATS=("MAJ_CAT", "nunique"), MAX_I_ROD=("I_ROD", "max"),
        ALC_I_ROD=("ALC_I_ROD", "max"), SHORT_QTY=("ART_SRT", "sum"), SHORT_QTY_ROD=("ART_SRT_ROD", "sum"),
        ALLOC_QTY=("ALLOC_QTY", "sum"), HOME_QTY=("HOME_QTY", "sum"), CROSS_QTY=("CROSS_QTY", "sum"),
        PACKS=("PACKS", "sum"), SERVED_LINES=("SERVED", "sum")).reset_index()
    sz = d.groupby(["ST_CD", "MAJ_CAT", "SZ"])["MJ_SZ_SRT"].max().groupby("ST_CD").sum()
    per_store["SZ_SRT"] = per_store["ST_CD"].map(sz).fillna(0)
    per_store["FILL_PCT"] = (per_store["ALLOC_QTY"] * 100 / per_store["SZ_SRT"].where(per_store["SZ_SRT"] > 0)).round(1)
    per_store["AVG_SEQ"] = per_store["AVG_SEQ"].round(2)

    by_round = d.groupby("ALC_I_ROD").agg(LINES=("ST_CD", "size"), STORES=("ST_CD", "nunique"),
                                          ARTICLES=("ARTICLE_NUMBER", "nunique"), ALLOC_QTY=("ALLOC_QTY", "sum"),
                                          PACKS=("PACKS", "sum")).reset_index().rename(columns={"ALC_I_ROD": "ROUND"})
    by_irod = d.groupby("I_ROD").agg(LINES=("ST_CD", "size"), ADMITTED_BY_I_ROD=("BY_IROD", "sum"),
                                     SHORT_QTY=("ART_SRT", "sum"), SHORT_QTY_ROD=("ART_SRT_ROD", "sum"),
                                     ALLOC_QTY=("ALLOC_QTY", "sum")).reset_index()
    by_src = d.groupby("SRC_RDC").agg(
        TO_OWN_STORES=("HOME_QTY", "sum"), TO_OTHER_RDC=("CROSS_QTY", "sum"),
        LINES_NO_SURPLUS=("SURPLUS_QTY", lambda s: int(((s <= 0) & (d.loc[s.index, "IS_HOME"] == 0)).sum())),
        LINES_WITH_SURPLUS=("SURPLUS_QTY", lambda s: int(((s > 0) & (d.loc[s.index, "IS_HOME"] == 0)).sum())),
    ).reset_index()
    stock = d.groupby(["SRC_RDC", "ARTICLE_NUMBER"])["ART_FNL_Q"].max().groupby("SRC_RDC").sum()
    by_src["STOCK_OFFERED"] = by_src["SRC_RDC"].map(stock).fillna(0)
    by_cat = (d.groupby("MAJ_CAT").agg(LINES=("ST_CD", "size"), STORES=("ST_CD", "nunique"),
                                       ALLOC_QTY=("ALLOC_QTY", "sum"), SHORT_QTY_ROD=("ART_SRT_ROD", "sum"))
              .reset_index().sort_values("ALLOC_QTY", ascending=False).head(40))
    by_full = d.assign(KIND=d["SZ"].astype(str).str.strip().str.upper().isin(E.FULL_OPT_SIZES)
                       .map({True: "Full option (A / NA)", False: "Real size"})) \
               .groupby("KIND").agg(LINES=("ST_CD", "size"), ALLOC_QTY=("ALLOC_QTY", "sum")).reset_index()

    def recs(x: pd.DataFrame) -> List[Dict[str, Any]]:
        return json.loads(x.to_json(orient="records"))

    return {"totals": _summary_totals(df), "per_store": recs(per_store), "by_round": recs(by_round),
            "by_irod": recs(by_irod), "by_src": recs(by_src), "by_cat": recs(by_cat), "by_kind": recs(by_full)}


# ═══════════════════════════════════════════════════════════════════════════
#  reading, exporting and deleting sessions
# ═══════════════════════════════════════════════════════════════════════════
def _session_row(r) -> Dict[str, Any]:
    d = {k: jsonable(v) for k, v in r._mapping.items()}
    for k, name in (("STORE_LIST", "store_list"), ("SKIPPED_JSON", "skipped"), ("CHECKS_JSON", "checks"),
                    ("SUMMARY_JSON", "summary"), ("PROGRESS", "progress")):
        if k in d:
            v = d.pop(k)
            try:
                d[name] = json.loads(v) if v else None
            except ValueError:
                d[name] = {"text": v} if name == "progress" else None
    _lifecycle(d)
    return d


# Session lifecycle (phase 3): RUNNING → PARKED (legacy DONE = the same) →
# APPROVED (in ARS_PEND_ALC) or DISCARDED; also FAILED, CANCELLED.
STATE_WORDS = {"PARKED": "Parked — holds its allocation inside 1SZ",
               "APPROVED": "Approved — posted to Pend ALC",
               "DISCARDED": "Discarded — released, nothing posted",
               "RUNNING": "Running", "FAILED": "Failed — nothing written", "CANCELLED": "Cancelled — nothing written"}
LIFECYCLE_COLS = "APPROVED_BY, APPROVED_AT, PEND_SESSION_ID, CLOSED_BY, CLOSED_AT, REFIT_OF"


def _lifecycle(d: Dict[str, Any]) -> None:
    """STATE = STATUS with a legacy DONE read as PARKED; IS_PARKED = it holds stock."""
    if "STATUS" in d:
        st = str(d["STATUS"] or "")
        d["STATE"] = "PARKED" if st in B.PARKED_STATUSES else st
        d["IS_PARKED"] = st in B.PARKED_STATUSES
        d["LEGACY_DONE"] = st == "DONE"
        d["STATE_TEXT"] = STATE_WORDS.get(d["STATE"], st)


def get(session_id: str, full: bool = True) -> Optional[Dict[str, Any]]:
    S.ensure_tables()
    cols = "*" if full else ("SESSION_ID, SESSION_AT, STATUS, PRIORITY_MODE, WEIGHT_MJ, STOCK_BASIS, CROSS_RDC, "
                             "STORES_GIVEN, STORES_OK, ROWS_WRITTEN, STORES_SERVED, ALLOC_QTY, PACKS, CROSS_QTY, "
                             "CHECKS_PASSED, PROGRESS, PROGRESS_PCT, ERROR, CREATED_BY, DURATION_SEC, "
                             + LIFECYCLE_COLS)
    with data_engine.connect() as conn:
        r = conn.execute(text(f"SELECT {cols} FROM {q(S.SESSION)} WHERE SESSION_ID = :id"),
                         {"id": session_id}).fetchone()
    if r is None:
        return None
    d = _session_row(r)
    if d["STATUS"] == "RUNNING" and not B._local_running():
        # Possibly another process's run: orphaned only once it is too old to be live.
        with data_engine.begin() as conn:
            hit = conn.execute(text(f"""
                UPDATE {q(S.SESSION)} SET STATUS = 'FAILED', FINISHED_AT = SYSDATETIME(),
                       PROGRESS = '{{"text": "Interrupted"}}',
                       ERROR = 'The server stopped while this session was running. Its transaction never committed, so nothing was written.'
                 WHERE SESSION_ID = :id AND STATUS = 'RUNNING'
                   AND DATEDIFF(MINUTE, CREATED_AT, SYSDATETIME()) >= {B.RUN_STALE_MIN}"""), {"id": session_id}).rowcount
        if hit:
            return get(session_id, full)
    return d


def recent(limit: int = 30) -> List[Dict[str, Any]]:
    S.ensure_tables()
    with data_engine.connect() as conn:
        res = conn.execute(text(f"""
            SELECT TOP (:n) SESSION_ID, SESSION_AT, STATUS, PRIORITY_MODE, WEIGHT_MJ, ROUND_ENVELOPE, STOCK_BASIS,
                   CROSS_RDC, STORES_GIVEN, STORES_OK, ROWS_WRITTEN, STORES_SERVED, LINES_SERVED, ASK_QTY,
                   ASK_QTY_ROD, ALLOC_QTY, PACKS, CROSS_QTY, CHECKS_PASSED, NOTE, CREATED_BY, DURATION_SEC,
                   {LIFECYCLE_COLS}, LEFT(ERROR, 300) AS ERROR
              FROM {q(S.SESSION)} ORDER BY SESSION_AT DESC, SESSION_ID DESC"""), {"n": limit})
        out = [_session_row(r) for r in res]
        bal = set(B.parked_sessions(conn))
    for s in out:
        s["IN_BALANCE"] = s["SESSION_ID"] in bal          # = parked: it holds stock and size budget
    return out


def page() -> Dict[str, Any]:
    g = gates()
    return {"gates": {"ok": g["ok"], "block": g["block"], "warn": g["warn"]},
            "step1": g["step1"] and {k: g["step1"].get(k) for k in ("BUILD_ID", "ROWS_BUILT", "FINISHED_AT", "CREATED_BY")},
            "step2": g["step2"] and {k: g["step2"].get(k) for k in ("BUILD_ID", "ROWS_BUILT", "FINISHED_AT", "CREATED_BY")},
            "balance_sessions": g["balance_sessions"], "parked_sessions": g["balance_sessions"],
            "running": B.running(),
            "choices": {"mode": E.PRIORITY_MODES, "cross_rdc": E.CROSS_RDC_MODES,
                        # P3: no Full / Remaining choice any more
                        "stock_basis": {LIVE: "Live remaining stock — MSA now, less open Pend ALC and parked sessions"}},
            "max_stores": E.MAX_STORES, "sessions": recent(12)}


LINE_SORTS = {"store": "ST_CD, MAJ_CAT, SZ, ARTICLE_NUMBER", "alloc": "ALLOC_QTY DESC, ST_CD",
              "unmet": "UNMET_QTY DESC, ST_CD", "seq": "MAJ_CAT, SZ, ST_SEQ, ARTICLE_NUMBER"}


def _line_filter(session_id: str, store, art, maj_cat, only) -> Tuple[str, Dict[str, Any]]:
    where, p = ["SESSION_ID = :sid"], {"sid": session_id}
    if store:
        where.append("ST_CD = :st"); p["st"] = store.strip().upper()
    if art:
        where.append("(ARTICLE_NUMBER = :art OR GEN_ART_NUMBER = :art)"); p["art"] = art.strip()
    if maj_cat:
        where.append("MAJ_CAT LIKE :mc"); p["mc"] = f"%{maj_cat.strip()}%"
    if only == "alloc":
        where.append("ALLOC_QTY > 0")
    elif only == "unmet":
        where.append("UNMET_QTY > 0")
    elif only == "cross":
        where.append("IS_HOME = 0")
    return "WHERE " + " AND ".join(where), p


def lines(session_id: str, store=None, art=None, maj_cat=None, only=None, sort="store",
          page_no: int = 1, size: int = 50) -> Dict[str, Any]:
    S.ensure_tables()
    w, p = _line_filter(session_id, store, art, maj_cat, only)
    size = min(max(int(size), 1), 500)
    off = (max(int(page_no), 1) - 1) * size
    # NOLOCK: a run writing ~1M lines escalates to a table lock for minutes;
    # these reads are of finished sessions, filtered by SESSION_ID.
    with data_engine.connect() as conn:
        tot = conn.execute(text(f"SELECT COUNT(*), ISNULL(SUM(ALLOC_QTY), 0) FROM {q(S.LINES)} WITH (NOLOCK) {w}"),
                           p).fetchone()
        data = rows(conn, f"""
            SELECT ST_CD, ST_NM, RDC, SRC_RDC, IS_HOME, MAJ_CAT, SZ, GEN_ART_NUMBER, CLR, ARTICLE_NUMBER,
                   ART_FNL_Q, PAK_SZ, MJ_SRT, MJ_SZ_SRT, ART_DISP, ART_SAL_PD, ART_STK, ART_MBQ, ART_SRT,
                   I_ROD, ART_SRT_ROD, ST_SEQ, ALLOC_QTY, PACKS, ALC_I_ROD, UNMET_QTY, BAL_ART_FNL_Q,
                   REM_MJ_SZ_SRT, OWN_NEED, SURPLUS_QTY
              FROM {q(S.LINES)} WITH (NOLOCK) {w} ORDER BY {LINE_SORTS.get(sort, LINE_SORTS['store'])}
             OFFSET {off} ROWS FETCH NEXT {size} ROWS ONLY""", p)
    return {"rows": data, "total": int(tot[0]), "alloc_qty": float(tot[1]), "page": page_no, "size": size}


def export_csv(session_id: str, only: Optional[str] = None) -> Iterator[bytes]:
    """The session's lines as CSV, streamed in chunks — a full session can be
    close to a million rows."""
    S.ensure_tables()
    w, p = _line_filter(session_id, None, None, None, only)
    # named → positional for the raw cursor, in order of appearance
    args = [p[m] for m in re.findall(r":(\w+)", w)]
    w = re.sub(r":\w+", "?", w)
    names = [c for c, _, _ in LINE_COLS]
    raw = data_engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(f"SELECT {', '.join(names)} FROM {q(S.LINES)} WITH (NOLOCK) {w} "
                    "ORDER BY ST_CD, MAJ_CAT, SZ, ARTICLE_NUMBER", *args)
        buf = io.StringIO()
        wr = csv.writer(buf)
        wr.writerow(names)
        while True:
            chunk = cur.fetchmany(10_000)
            if not chunk:
                break
            wr.writerows(chunk)
            yield buf.getvalue().encode("utf-8")
            buf.seek(0); buf.truncate(0)
        if buf.tell():
            yield buf.getvalue().encode("utf-8")
    finally:
        try:
            raw.close()
        except Exception:
            pass


def delete(session_id: str, user: Optional[str]) -> Dict[str, Any]:
    """Remove one session and re-apply the balances from what is left — in one
    transaction. A parked session's hold is released (balances re-applied
    live). An APPROVED session is in Pend ALC: it must be reverted first.

    Race-safe against approve: the header is read WITH (UPDLOCK, HOLDLOCK) —
    the same lock approve takes (sz1_approve_service.lock_session) — so the two
    serialise; and both DELETEs are themselves conditional on the header not
    being APPROVED, so even a header that turns APPROVED between the check and
    the delete keeps its lines (the header delete then hits 0 rows and the
    whole transaction rolls back)."""
    not_approved = "ISNULL(h.STATUS, '') <> 'APPROVED'"
    with B.job_slot("delete"):
        with data_engine.begin() as conn:
            st = conn.execute(text(f"SELECT STATUS FROM {q(S.SESSION)} WITH (UPDLOCK, HOLDLOCK) "
                                   "WHERE SESSION_ID = :s"), {"s": session_id}).fetchone()
            if st is None:
                raise ValueError(f"Session {session_id} not found")
            if str(st[0]) == "APPROVED":
                raise ValueError(f"Session {session_id} is approved into Pend ALC — revert it first, then delete it.")
            n_lines = conn.execute(text(f"""
                DELETE l FROM {q(S.LINES)} l
                 WHERE l.SESSION_ID = :s
                   AND EXISTS (SELECT 1 FROM {q(S.SESSION)} h WITH (UPDLOCK, HOLDLOCK)
                                WHERE h.SESSION_ID = :s AND {not_approved})"""),
                {"s": session_id}).rowcount
            n_head = conn.execute(text(f"""
                DELETE h FROM {q(S.SESSION)} h
                 WHERE h.SESSION_ID = :s AND {not_approved}"""), {"s": session_id}).rowcount
            if not n_head:
                # gone or approved since the check: roll everything back (lines included)
                now_st = conn.execute(text(f"SELECT STATUS FROM {q(S.SESSION)} WHERE SESSION_ID = :s"),
                                      {"s": session_id}).scalar()
                if now_st is not None and str(now_st) == "APPROVED":
                    raise ValueError(f"Session {session_id} was approved into Pend ALC meanwhile — "
                                     "revert it first, then delete it.")
                raise ValueError(f"Session {session_id} not found")
            ids = B.push_balances(conn)
    logger.info(f"[sz1] session {session_id} deleted by {user}: {n_lines} lines; balances now from {ids}")
    return {"lines": n_lines, "balance_sessions": ids}
