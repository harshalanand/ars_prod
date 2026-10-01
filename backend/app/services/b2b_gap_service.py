"""
GRT ALC — Gap Report: where supply and requirement do not line up.

Ported from the Streamlit tool's gap_report.py. Same twelve sheets, same SQL
meaning, read from the ARS_B2B_* tables, plus one ARS adds:

    13_Warehouse_Balance  stock, stores and requirement per RDC, and (for a
                          session) what moved within and between warehouses —
                          the numbers the DH24 / DW01 decision needs

Two conventions run through every sheet, as in the tool:

  * The category + size key is (MAJ_CAT, ISNULL(SIZE, '')) — the key the
    demand build and the REQ cap use, so a gap shown here is one allocation hits.
  * REQ is split into SERVABLE (store in Store Master) and ORPHAN (not): the
    build inner-joins REQ to Store Master, so orphan REQ can never be filled.
    COVER_PCT is against servable REQ only.

Faster than the tool: the coverage matrix is built ONCE into a temp table and
every sheet slices it, instead of five sheets recomputing it. Results are
cached per (latest load, session): the inputs only change when a workbook is
loaded, and a session never changes.
"""
from __future__ import annotations

import csv
import io
import threading
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.database.session import data_engine
from app.services import b2b_schema as S

TOP_N = 50_000                   # detail sheets are capped; a capped sheet is flagged
PREVIEW = 200
EXPORT_DIR = Path(__file__).resolve().parents[2] / "uploads" / "b2b_export"

# (key, title, what it shows, needs a session)
SHEETS: List[Tuple[str, str, str, bool]] = [
    ("01_Summary", "Summary", "Headline figures and the size of every gap class.", False),
    ("02_Coverage_Matrix", "Coverage matrix",
     "Every category + size in either source with its gap verdict. The master sheet — 03 to 06 are slices of it.", False),
    ("03_REQ_No_Stock", "REQ but no stock", "Stores want it, the bins hold none.", False),
    ("04_REQ_Short_Stock", "REQ but not enough stock", "Oversubscribed: how short, and what an even share would be.", False),
    ("05_Stock_No_REQ", "Stock but no REQ", "Stock nobody asked for: out of season, or not merchandise.", False),
    ("06_Stock_Surplus", "Stock above REQ", "More stock than the servable stores can take.", False),
    ("07_Stores_Missing_Master", "Stores missing from Store Master",
     "Their REQ can never be filled — the largest gap you can fix.", False),
    ("08_Stores_No_REQ", "Stores with no REQ", "In Store Master but asked for nothing.", False),
    ("09_Store_Summary", "Store summary", "Per store: asked for, received, unmet, fill %.", True),
    ("10_Store_Cat_Gap", "Store × category × size gap", "Where each store's requirement went unmet.", True),
    ("11_Key_Mismatch", "Join key mismatches",
     "Category or size values in one sheet only — they silently fail to join, so stock or demand disappears.", False),
    ("12_Leftover_Reasons", "Leftover reasons", "Why each unit stayed in its bin, from the session's plan.", True),
    ("13_Warehouse_Balance", "Warehouse balance (new)",
     "Per RDC: bin stock, stores, servable REQ; and for the session, what moved within and across warehouses.", False),
]
LEFT_MEANING = {
    "NO_STORE_MASTER": "ACTIONABLE — wanted, but only by stores missing from Store Master",
    "NO_SEASON_REQ": "every store asked for zero of this category + size",
    "NOT_IN_REQ": "category absent from the REQ sheet — not merchandise",
    "REQ_CAP_FULL": "stores were short, but their category + size REQ was already filled",
    "NO_SHORTFALL": "stores carry it and none is below its target",
    "PARTIAL": "some of this bin moved; demand ran out first",
    "NO_STORE_NEED": "no demand row at all (catch-all)",
}

_cache: Dict[Tuple, Dict[str, Any]] = {}
_lock = threading.Lock()


def _v(x):
    if isinstance(x, Decimal):
        return float(x)
    if isinstance(x, datetime):
        return x.isoformat()
    return x


def _q(cur, sql: str, *params) -> Dict[str, Any]:
    cur.execute(sql, *params)
    cols = [d[0] for d in cur.description]
    return {"columns": cols, "rows": [[_v(v) for v in r] for r in cur.fetchall()]}


def _scalar_rows(cur, sql: str, *params):
    cur.execute(sql, *params)
    return cur.fetchall()


def version() -> Tuple[Optional[int], Optional[int]]:
    """(latest loaded upload, latest stored session): what the report depends on."""
    with data_engine.connect() as c:
        r = c.exec_driver_sql(
            f"SELECT (SELECT MAX(UPLOAD_ID) FROM dbo.{S.UPLOAD} WHERE STATUS = 'LOADED'), "
            f"(SELECT MAX(SESSION_ID) FROM dbo.{S.SESSION} WHERE STATUS = 'DONE' AND DRY_RUN = 0)").fetchone()
    return (r[0], r[1])


def _matrix_sql() -> str:
    b, q, m = S.BIN_MASTER, S.REQ, S.STORE_MASTER
    return f"""
    SET NOCOUNT ON;
    IF OBJECT_ID('tempdb..#matrix') IS NOT NULL DROP TABLE #matrix;
    WITH sup AS (
        SELECT MAJ_CAT, ISNULL([SIZE], N'') AS SZ_KEY, MAX(SEG) AS SEG, MAX(DIV) AS DIV,
               MAX(SUB_DIV) AS SUB_DIV, MAX(SEASON) AS SEASON, COUNT(DISTINCT ART) AS ARTICLES,
               COUNT(DISTINCT BIN) AS BINS, SUM(CAST(QTY AS BIGINT)) AS SUPPLY_UNITS
          FROM dbo.{b} GROUP BY MAJ_CAT, ISNULL([SIZE], N'')),
    dem AS (
        SELECT r.MAJ_CAT, ISNULL(r.[SIZE], N'') AS SZ_KEY, MAX(r.SEASON) AS REQ_SEASON,
               CONVERT(DECIMAL(18,2), SUM(CASE WHEN r.REQ > 0 THEN r.REQ ELSE 0 END)) AS REQ_ALL,
               CONVERT(DECIMAL(18,2), SUM(CASE WHEN r.REQ > 0 AND m.STORE_CODE IS NOT NULL THEN r.REQ ELSE 0 END)) AS REQ_SERVABLE,
               CONVERT(DECIMAL(18,2), SUM(CASE WHEN r.REQ > 0 AND m.STORE_CODE IS NULL THEN r.REQ ELSE 0 END)) AS REQ_ORPHAN,
               COUNT(DISTINCT CASE WHEN r.REQ > 0 THEN r.STORE_CODE END) AS STORES_WANTING,
               COUNT(DISTINCT CASE WHEN r.REQ > 0 AND m.STORE_CODE IS NOT NULL THEN r.STORE_CODE END) AS STORES_SERVABLE
          FROM dbo.{q} r LEFT JOIN dbo.{m} m ON m.STORE_CODE = r.STORE_CODE
         GROUP BY r.MAJ_CAT, ISNULL(r.[SIZE], N''))
    SELECT  COALESCE(s.MAJ_CAT, d.MAJ_CAT) AS MAJ_CAT, COALESCE(s.SZ_KEY, d.SZ_KEY) AS SZ_KEY,
            s.SEG, s.DIV, s.SUB_DIV, s.SEASON AS BIN_SEASON, d.REQ_SEASON,
            ISNULL(s.ARTICLES, 0) AS ARTICLES, ISNULL(s.BINS, 0) AS BINS,
            ISNULL(s.SUPPLY_UNITS, 0) AS SUPPLY_UNITS, ISNULL(d.REQ_ALL, 0) AS REQ_ALL,
            ISNULL(d.REQ_SERVABLE, 0) AS REQ_SERVABLE, ISNULL(d.REQ_ORPHAN, 0) AS REQ_ORPHAN,
            ISNULL(d.STORES_WANTING, 0) AS STORES_WANTING, ISNULL(d.STORES_SERVABLE, 0) AS STORES_SERVABLE,
            CONVERT(DECIMAL(18,2), ISNULL(s.SUPPLY_UNITS, 0) - ISNULL(d.REQ_SERVABLE, 0)) AS SUPPLY_LESS_REQ,
            CASE WHEN ISNULL(d.REQ_SERVABLE, 0) > 0
                 THEN CONVERT(DECIMAL(9,1), 100.0 * ISNULL(s.SUPPLY_UNITS, 0) / d.REQ_SERVABLE) END AS COVER_PCT,
            CASE WHEN ISNULL(d.REQ_ALL, 0) = 0 AND ISNULL(s.SUPPLY_UNITS, 0) > 0 THEN N'A. STOCK, NO REQ'
                 WHEN ISNULL(d.REQ_SERVABLE, 0) = 0 AND ISNULL(d.REQ_ORPHAN, 0) > 0 THEN N'B. REQ ONLY FROM ORPHAN STORES'
                 WHEN ISNULL(s.SUPPLY_UNITS, 0) = 0 AND ISNULL(d.REQ_SERVABLE, 0) > 0 THEN N'C. REQ, NO STOCK'
                 WHEN ISNULL(s.SUPPLY_UNITS, 0) < ISNULL(d.REQ_SERVABLE, 0) THEN N'D. REQ, NOT ENOUGH STOCK'
                 WHEN ISNULL(s.SUPPLY_UNITS, 0) > ISNULL(d.REQ_SERVABLE, 0) THEN N'E. STOCK ABOVE REQ'
                 ELSE N'F. BALANCED' END AS VERDICT
      INTO #matrix
      FROM sup s FULL OUTER JOIN dem d ON d.MAJ_CAT = s.MAJ_CAT AND d.SZ_KEY = s.SZ_KEY;
    CREATE CLUSTERED INDEX IX_matrix ON #matrix (VERDICT, MAJ_CAT, SZ_KEY);"""


def build(session_id: Optional[int]) -> Dict[str, Any]:
    """All thirteen sheets as {key: {columns, rows, total, capped}}. Cached."""
    S.ensure_tables()
    up, _latest = version()
    key = (up, session_id)
    with _lock:
        if key in _cache:
            return _cache[key]
    t0 = time.time()
    b, q, m, al, bp, ss = S.BIN_MASTER, S.REQ, S.STORE_MASTER, S.ALLOC, S.BIN_PLAN, S.SESSION
    out: Dict[str, Dict[str, Any]] = {}
    raw = data_engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(_matrix_sql())
        while cur.nextset():
            pass

        # 02 and its slices
        out["02_Coverage_Matrix"] = _q(cur, """
            SELECT VERDICT, SEG, DIV, SUB_DIV, MAJ_CAT, SZ_KEY AS [SIZE], BIN_SEASON, REQ_SEASON,
                   ARTICLES, BINS, SUPPLY_UNITS, REQ_ALL, REQ_SERVABLE, REQ_ORPHAN,
                   STORES_WANTING, STORES_SERVABLE, SUPPLY_LESS_REQ, COVER_PCT
              FROM #matrix
             ORDER BY VERDICT, CASE WHEN REQ_SERVABLE > SUPPLY_UNITS THEN REQ_SERVABLE - SUPPLY_UNITS ELSE 0 END DESC,
                      SUPPLY_UNITS DESC""")
        out["03_REQ_No_Stock"] = _q(cur, f"""
            SELECT TOP {TOP_N} MAJ_CAT, SZ_KEY AS [SIZE], REQ_SEASON, REQ_SERVABLE AS REQ_UNITS_UNFILLABLE,
                   STORES_SERVABLE AS STORES_WANTING, ARTICLES AS ARTICLES_IN_BINS
              FROM #matrix WHERE VERDICT = N'C. REQ, NO STOCK' ORDER BY REQ_SERVABLE DESC""")
        out["04_REQ_Short_Stock"] = _q(cur, f"""
            SELECT TOP {TOP_N} MAJ_CAT, SZ_KEY AS [SIZE], BIN_SEASON, SUPPLY_UNITS, REQ_SERVABLE,
                   STORES_SERVABLE AS STORES_WANTING,
                   CONVERT(DECIMAL(18,2), REQ_SERVABLE - SUPPLY_UNITS) AS SHORT_UNITS, COVER_PCT,
                   CONVERT(DECIMAL(18,2), SUPPLY_UNITS * 1.0 / NULLIF(STORES_SERVABLE, 0)) AS UNITS_PER_STORE_IF_SHARED
              FROM #matrix WHERE VERDICT = N'D. REQ, NOT ENOUGH STOCK' ORDER BY (REQ_SERVABLE - SUPPLY_UNITS) DESC""")
        out["05_Stock_No_REQ"] = _q(cur, f"""
            WITH cat AS (SELECT DISTINCT MAJ_CAT FROM dbo.{q})
            SELECT TOP {TOP_N} x.SEG, x.DIV, x.MAJ_CAT, x.SZ_KEY AS [SIZE], x.BIN_SEASON, x.ARTICLES, x.BINS,
                   x.SUPPLY_UNITS AS STOCK_WITH_NO_REQ,
                   CASE WHEN c.MAJ_CAT IS NULL THEN N'NO - not merchandise'
                        ELSE N'YES - category traded, this size zeroed' END AS CAT_IN_REQ
              FROM #matrix x LEFT JOIN cat c ON c.MAJ_CAT = x.MAJ_CAT
             WHERE x.VERDICT = N'A. STOCK, NO REQ' ORDER BY x.SUPPLY_UNITS DESC""")
        out["06_Stock_Surplus"] = _q(cur, f"""
            SELECT TOP {TOP_N} MAJ_CAT, SZ_KEY AS [SIZE], BIN_SEASON, SUPPLY_UNITS, REQ_SERVABLE,
                   STORES_SERVABLE AS STORES_WANTING, SUPPLY_LESS_REQ AS SURPLUS_UNITS, COVER_PCT
              FROM #matrix WHERE VERDICT = N'E. STOCK ABOVE REQ' ORDER BY SUPPLY_LESS_REQ DESC""")
        out["07_Stores_Missing_Master"] = _q(cur, f"""
            SELECT r.STORE_CODE, MAX(r.ST_NM) AS ST_NM, COUNT(*) AS REQ_LINES,
                   CONVERT(DECIMAL(18,2), SUM(r.REQ)) AS REQ_UNITS_LOST, COUNT(DISTINCT r.MAJ_CAT) AS CATEGORIES
              FROM dbo.{q} r
             WHERE NOT EXISTS (SELECT 1 FROM dbo.{m} x WHERE x.STORE_CODE = r.STORE_CODE)
             GROUP BY r.STORE_CODE ORDER BY REQ_UNITS_LOST DESC""")
        out["08_Stores_No_REQ"] = _q(cur, f"""
            SELECT x.STORE_CODE, x.ST_NM, x.RDC FROM dbo.{m} x
             WHERE NOT EXISTS (SELECT 1 FROM dbo.{q} r WHERE r.STORE_CODE = x.STORE_CODE)
             ORDER BY x.STORE_CODE""")
        out["11_Key_Mismatch"] = _q(cur, f"""
            SELECT N'MAJ_CAT' AS KEY_TYPE, N'in BIN MASTER only' AS SIDE, bm.MAJ_CAT AS VALUE_,
                   COUNT(*) AS ROWS_, CONVERT(DECIMAL(18,2), SUM(bm.QTY)) AS UNITS
              FROM dbo.{b} bm WHERE NOT EXISTS (SELECT 1 FROM dbo.{q} r WHERE r.MAJ_CAT = bm.MAJ_CAT)
             GROUP BY bm.MAJ_CAT
            UNION ALL
            SELECT N'MAJ_CAT', N'in REQ only', r.MAJ_CAT, COUNT(*), CONVERT(DECIMAL(18,2), SUM(r.REQ))
              FROM dbo.{q} r WHERE NOT EXISTS (SELECT 1 FROM dbo.{b} bm WHERE bm.MAJ_CAT = r.MAJ_CAT)
             GROUP BY r.MAJ_CAT
            UNION ALL
            SELECT N'SIZE', N'in BIN MASTER only', ISNULL(bm.[SIZE], N''), COUNT(*), CONVERT(DECIMAL(18,2), SUM(bm.QTY))
              FROM dbo.{b} bm WHERE NOT EXISTS (SELECT 1 FROM dbo.{q} r WHERE ISNULL(r.[SIZE], N'') = ISNULL(bm.[SIZE], N''))
             GROUP BY ISNULL(bm.[SIZE], N'')
            UNION ALL
            SELECT N'SIZE', N'in REQ only', ISNULL(r.[SIZE], N''), COUNT(*), CONVERT(DECIMAL(18,2), SUM(r.REQ))
              FROM dbo.{q} r WHERE NOT EXISTS (SELECT 1 FROM dbo.{b} bm WHERE ISNULL(bm.[SIZE], N'') = ISNULL(r.[SIZE], N''))
             GROUP BY ISNULL(r.[SIZE], N'')
            ORDER BY KEY_TYPE, SIDE, UNITS DESC""")

        # session-dependent sheets
        sid = session_id
        if sid is not None:
            out["09_Store_Summary"] = _q(cur, f"""
                WITH req AS (SELECT STORE_CODE, MAX(ST_NM) AS ST_NM, CONVERT(DECIMAL(18,2), SUM(REQ)) AS REQ_UNITS,
                                    COUNT(*) AS REQ_LINES FROM dbo.{q} GROUP BY STORE_CODE),
                     got AS (SELECT STORE_CODE, SUM(ALLOC_QTY) AS GOT FROM dbo.{al} WHERE SESSION_ID = ? GROUP BY STORE_CODE)
                SELECT x.STORE_CODE, COALESCE(x.ST_NM, r.ST_NM) AS ST_NM, x.RDC,
                       ISNULL(r.REQ_UNITS, 0) AS REQ_UNITS, ISNULL(r.REQ_LINES, 0) AS REQ_LINES,
                       ISNULL(a.GOT, 0) AS ALLOCATED_UNITS,
                       CONVERT(DECIMAL(18,2), ISNULL(r.REQ_UNITS, 0) - ISNULL(a.GOT, 0)) AS UNMET_UNITS,
                       CASE WHEN ISNULL(r.REQ_UNITS, 0) > 0
                            THEN CONVERT(DECIMAL(9,1), 100.0 * ISNULL(a.GOT, 0) / r.REQ_UNITS) END AS FILL_PCT
                  FROM dbo.{m} x LEFT JOIN req r ON r.STORE_CODE = x.STORE_CODE
                  LEFT JOIN got a ON a.STORE_CODE = x.STORE_CODE
                 ORDER BY FILL_PCT, UNMET_UNITS DESC""", sid)
            out["10_Store_Cat_Gap"] = _q(cur, f"""
                WITH req AS (
                    SELECT r.STORE_CODE, MAX(r.ST_NM) AS ST_NM, r.MAJ_CAT, ISNULL(r.[SIZE], N'') AS SZ_KEY,
                           CONVERT(DECIMAL(18,2), SUM(r.REQ)) AS REQ_UNITS
                      FROM dbo.{q} r JOIN dbo.{m} x ON x.STORE_CODE = r.STORE_CODE
                     GROUP BY r.STORE_CODE, r.MAJ_CAT, ISNULL(r.[SIZE], N'') HAVING SUM(r.REQ) > 0),
                got AS (
                    SELECT STORE_CODE, MAJ_CAT, ISNULL(BIN_SIZE, N'') AS SZ_KEY, SUM(ALLOC_QTY) AS GOT
                      FROM dbo.{al} WHERE SESSION_ID = ? GROUP BY STORE_CODE, MAJ_CAT, ISNULL(BIN_SIZE, N''))
                SELECT TOP {TOP_N} qq.STORE_CODE, qq.ST_NM, qq.MAJ_CAT, qq.SZ_KEY AS [SIZE], qq.REQ_UNITS,
                       ISNULL(g.GOT, 0) AS ALLOCATED_UNITS,
                       CONVERT(DECIMAL(18,2), qq.REQ_UNITS - ISNULL(g.GOT, 0)) AS UNMET_UNITS,
                       CONVERT(DECIMAL(9,1), 100.0 * ISNULL(g.GOT, 0) / qq.REQ_UNITS) AS FILL_PCT
                  FROM req qq LEFT JOIN got g ON g.STORE_CODE = qq.STORE_CODE AND g.MAJ_CAT = qq.MAJ_CAT
                                             AND g.SZ_KEY = qq.SZ_KEY
                 WHERE qq.REQ_UNITS - ISNULL(g.GOT, 0) > 0
                 ORDER BY UNMET_UNITS DESC""", sid)
            lr = _q(cur, f"""
                SELECT LEFT_REASON, COUNT(*) AS LINES, COUNT(DISTINCT ART) AS ARTICLES, SUM(QTY) AS UNITS_LEFT
                  FROM dbo.{bp} WHERE SESSION_ID = ? AND ROW_TYPE = N'UNALLOC'
                 GROUP BY LEFT_REASON ORDER BY SUM(QTY) DESC""", sid)
            lr["columns"].insert(1, "MEANING")
            for r in lr["rows"]:
                r.insert(1, LEFT_MEANING.get(r[0], ""))
            out["12_Leftover_Reasons"] = lr

        # 13 — warehouse balance (new in ARS)
        flows = {}
        if sid is not None:
            for r in _scalar_rows(cur, f"""SELECT BIN_RDC, STORE_RDC, SUM(QTY) FROM dbo.{bp}
                                            WHERE SESSION_ID = ? AND ROW_TYPE = N'ALLOC'
                                            GROUP BY BIN_RDC, STORE_RDC""", sid):
                flows[(r[0], r[1])] = int(r[2] or 0)
        bins = {r[0]: int(r[1] or 0) for r in _scalar_rows(cur, f"SELECT BIN_RDC, SUM(CAST(QTY AS BIGINT)) FROM dbo.{b} GROUP BY BIN_RDC")}
        stores = {r[0]: (int(r[1]), float(r[2] or 0)) for r in _scalar_rows(cur, f"""
            SELECT x.RDC, COUNT(DISTINCT x.STORE_CODE),
                   SUM(CASE WHEN r.REQ > 0 THEN r.REQ ELSE 0 END)
              FROM dbo.{m} x LEFT JOIN dbo.{q} r ON r.STORE_CODE = x.STORE_CODE GROUP BY x.RDC""")}
        wh_rows = []
        for w in sorted(set(bins) | set(stores), key=lambda z: str(z)):
            own = flows.get((w, w), 0)
            into = sum(v for (f, t_), v in flows.items() if t_ == w and f != w)
            outof = sum(v for (f, t_), v in flows.items() if f == w and t_ != w)
            wh_rows.append([w, bins.get(w, 0), stores.get(w, (0, 0))[0], round(stores.get(w, (0, 0))[1], 2),
                            own if sid else None, into if sid else None, outof if sid else None,
                            (own + into) if sid else None])
        out["13_Warehouse_Balance"] = {"columns": ["RDC", "BIN_STOCK", "STORES", "SERVABLE_REQ",
                                                   "SENT_FROM_OWN_BINS", "RECEIVED_FROM_OTHER_RDC",
                                                   "SENT_TO_OTHER_RDC_STORES", "STORES_RECEIVED_TOTAL"],
                                       "rows": wh_rows}

        # 01 — summary last, from what the others found
        tot = _scalar_rows(cur, """SELECT SUM(SUPPLY_UNITS), SUM(REQ_ALL), SUM(REQ_SERVABLE), SUM(REQ_ORPHAN),
                                          COUNT(*) FROM #matrix""")[0]
        st = _scalar_rows(cur, f"""
            SELECT (SELECT COUNT(DISTINCT STORE_CODE) FROM dbo.{q}), (SELECT COUNT(*) FROM dbo.{m}),
                   (SELECT COUNT(*) FROM (SELECT DISTINCT STORE_CODE FROM dbo.{q}) x
                     WHERE NOT EXISTS (SELECT 1 FROM dbo.{m} y WHERE y.STORE_CODE = x.STORE_CODE)),
                   (SELECT COUNT(*) FROM dbo.{m} y
                     WHERE NOT EXISTS (SELECT 1 FROM dbo.{q} r WHERE r.STORE_CODE = y.STORE_CODE))""")[0]
        rows = [
            ["Sources", "Bin Master units on hand", float(tot[0] or 0), ""],
            ["Sources", "REQ units stated, all stores", float(tot[1] or 0), ""],
            ["Sources", "REQ units from stores in Store Master", float(tot[2] or 0), "the only REQ the build can act on"],
            ["Sources", "REQ units from stores NOT in Store Master", float(tot[3] or 0),
             "UNFILLABLE: the demand build inner-joins REQ to Store Master"],
            ["Stores", "Stores with a REQ line", float(st[0] or 0), ""],
            ["Stores", "Stores in Store Master", float(st[1] or 0), ""],
            ["Stores", "REQ stores missing from Store Master", float(st[2] or 0), "see sheet 07"],
            ["Stores", "Store Master stores with no REQ", float(st[3] or 0), "see sheet 08"],
            ["Scope", "Category + size combinations seen", float(tot[4] or 0), ""],
        ]
        for r in _scalar_rows(cur, """SELECT VERDICT, COUNT(*), SUM(SUPPLY_UNITS), SUM(REQ_SERVABLE)
                                        FROM #matrix GROUP BY VERDICT ORDER BY VERDICT"""):
            rows.append(["Gap verdict", r[0], float(r[1]),
                         f"{float(r[2] or 0):,.0f} units of stock, {float(r[3] or 0):,.0f} units of servable REQ"])
        if sid is not None:
            a = _scalar_rows(cur, f"""SELECT UNITS_ALLOCATED, UNITS_LEFT, ALLOC_FILL_MODE, ALLOC_FAIR_BASIS, ALLOC_CROSS_RDC,
                                             CROSS_RDC_UNITS FROM dbo.{ss} WHERE SESSION_ID = ?""", sid)
            if a:
                a = a[0]
                rows += [["Allocation", f"Session {sid} units allocated", float(a[0] or 0),
                          f"{a[2]}{' / ' + a[3] if a[3] else ''} · warehouse rule {a[4]}"],
                         ["Allocation", f"Session {sid} units left in bins", float(a[1] or 0), "see sheet 12"],
                         ["Allocation", f"Session {sid} units sent across warehouses", float(a[5] or 0), "see sheet 13"]]
        rows.append(["Note", "Detail sheet row cap", float(TOP_N), "sheets 03–06, 10 and 11 stop at this many rows"])
        out["01_Summary"] = {"columns": ["AREA", "MEASURE", "VALUE", "NOTE"], "rows": rows}
        cur.execute("IF OBJECT_ID('tempdb..#matrix') IS NOT NULL DROP TABLE #matrix;")
        raw.commit()
    finally:
        raw.close()

    for k, v in out.items():
        v["total"] = len(v["rows"])
        v["capped"] = k in ("03_REQ_No_Stock", "04_REQ_Short_Stock", "05_Stock_No_REQ", "06_Stock_Surplus",
                            "10_Store_Cat_Gap") and len(v["rows"]) >= TOP_N
    res = {"upload_id": up, "session_id": session_id, "built_at": datetime.now().isoformat(timespec="seconds"),
           "seconds": round(time.time() - t0, 1), "sheets": out}
    with _lock:
        _cache.clear() if len(_cache) > 8 else None
        _cache[key] = res
    logger.info(f"[b2b gap] built for upload {up}, session {session_id} in {res['seconds']}s")
    return res


def report(session_id: Optional[int]) -> Dict[str, Any]:
    """What the page shows: contents, verdict totals, and a preview of each sheet."""
    r = build(session_id)
    contents, previews = [], {}
    for key, title, desc, needs in SHEETS:
        sh = r["sheets"].get(key)
        contents.append({"sheet": key, "title": title, "desc": desc, "needs_session": needs,
                         "rows": sh["total"] if sh else None, "capped": bool(sh and sh["capped"])})
        if sh:
            previews[key] = {"columns": sh["columns"], "rows": sh["rows"][:PREVIEW], "total": sh["total"]}
    return {"upload_id": r["upload_id"], "session_id": r["session_id"], "built_at": r["built_at"],
            "seconds": r["seconds"], "contents": contents, "previews": previews}


def sheet_csv(session_id: Optional[int], key: str) -> Tuple[str, bytes]:
    r = build(session_id)
    sh = r["sheets"].get(key)
    if sh is None:
        raise ValueError(f"Sheet {key} is not in this report" + (" (it needs a session)" if session_id is None else ""))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(sh["columns"])
    w.writerows(sh["rows"])
    return f"GRT_ALC_gap_{key}_upload{r['upload_id']}{'_session' + str(session_id) if session_id else ''}.csv", \
        buf.getvalue().encode("utf-8-sig")


def workbook(session_id: Optional[int]) -> Path:
    """The whole report as one workbook, contents first. Kept per (load, session)."""
    r = build(session_id)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPORT_DIR / f"GRT_ALC_gap_report_upload{r['upload_id']}{'_session' + str(session_id) if session_id else ''}.xlsx"
    if path.exists():
        return path
    import xlsxwriter
    tmp = path.with_suffix(".xlsx.part")
    wb = xlsxwriter.Workbook(str(tmp), {"constant_memory": True})
    bold = wb.add_format({"bold": True, "bg_color": "#EEF2F7", "border": 1})
    ws = wb.add_worksheet("00_Contents")
    ws.set_column(0, 0, 28)
    ws.set_column(1, 1, 90)
    for i, (k, v) in enumerate([("Report", "GRT ALC · Bin-to-Bin gap report"),
                                ("Loaded workbook (upload)", r["upload_id"]),
                                ("Allocation session", r["session_id"] or "none"),
                                ("Generated", r["built_at"].replace("T", " "))]):
        ws.write(i, 0, k, bold)
        ws.write(i, 1, v)
    ws.write_row(6, 0, ["SHEET", "WHAT IT SHOWS", "ROWS", "NOTE"], bold)
    row = 7
    for key, title, desc, _needs in SHEETS:
        sh = r["sheets"].get(key)
        ws.write_row(row, 0, [key, f"{title} — {desc}", sh["total"] if sh else "—",
                              "TRUNCATED at the row cap" if sh and sh["capped"] else
                              ("needs a session" if sh is None else "")])
        row += 1
    for key, _t, _d, _n in SHEETS:
        sh = r["sheets"].get(key)
        if sh is None:
            continue
        s = wb.add_worksheet(key[:31])
        for c, h in enumerate(sh["columns"]):
            s.set_column(c, c, max(10, min(40, len(h) + 4)))
        s.write_row(0, 0, sh["columns"], bold)
        s.freeze_panes(1, 0)
        for i, rr in enumerate(sh["rows"], start=1):
            s.write_row(i, 0, rr)
        if sh["rows"]:
            s.autofilter(0, 0, len(sh["rows"]), len(sh["columns"]) - 1)
    wb.close()
    tmp.replace(path)
    return path
