"""
GRT ALC — Step 5: review a session.

    session     the run's record, where its stock went warehouse to warehouse,
                its leftovers by reason, whether the data has moved since
    lines       store × article (ARS_B2B_ALLOC), paged and filtered
    picks       the pick list (ARS_B2B_BIN_PLAN, ROW_TYPE ALLOC) in warehouse
                walking order, cross-warehouse picks flagged
    leftovers   stock left in bins (ROW_TYPE UNALLOC) and why
    why         "Why no stock?" — one store, or one store × article, walked
                through every gate; when the session's inputs are still the
                current ones its category is replayed with a tracer, which
                says exactly what happened at that store's turn
    compare     two sessions side by side: totals, stores, categories, lines
    export      the pick list workbook; locked unless every balance check passed
"""
from __future__ import annotations

import csv
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine
from app.services import b2b_alloc_engine as E
from app.services import b2b_alloc_service as A
from app.services import b2b_schema as S

EXPORT_DIR = Path(__file__).resolve().parents[2] / "uploads" / "b2b_export"
WORDS = {"SAME": "Own RDC only", "HOME_FIRST": "Own RDC first", "ANY": "All RDCs"}
PRIORITY_WORDS = {"SHORTFALL_DESC": "biggest shortfall", "CONT_DESC": "highest size share",
                  "MBQ_DESC": "biggest target"}


def _v(x):
    if isinstance(x, Decimal):
        return float(x)
    if isinstance(x, datetime):
        return x.isoformat()
    return x


def _rows(conn, sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{k: _v(v) for k, v in r._mapping.items()} for r in conn.execute(text(sql), params)]


def _need(sid: int) -> Dict[str, Any]:
    s = A.get(sid)
    if s is None:
        raise ValueError(f"Session {sid} not found")
    return s


# ═══════════════════════════════════════════════════════════════════════════
#  one session
# ═══════════════════════════════════════════════════════════════════════════
def context(s: Dict[str, Any]) -> Dict[str, Any]:
    """Is the data this session read still the current data? Only then can a
    store × article be replayed exactly."""
    from app.services import b2b_mbq_service as M
    latest = M.latest_done()
    fresh = M.freshness(latest)
    same_build = bool(latest) and latest["BUILD_ID"] == s.get("BUILD_ID")
    current = same_build and fresh["state"] in ("fresh", "aging")
    why = []
    if not same_build:
        why.append(f"The demand has been rebuilt since (session read build {s.get('BUILD_ID')}, "
                   f"current is {latest['BUILD_ID'] if latest else 'none'}).")
    elif fresh["state"] == "stale":
        why.extend(fresh["reasons"])
    return {"current": current, "why": why, "latest_build": latest["BUILD_ID"] if latest else None}


def session(sid: int) -> Dict[str, Any]:
    s = _need(sid)
    out: Dict[str, Any] = {"session": s, "context": context(s)}
    if s["STATUS"] != "DONE" or s.get("DRY_RUN"):
        return out
    p = {"s": sid}
    with data_engine.connect() as conn:
        out["rdc_flow"] = _rows(conn, f"""
            SELECT BIN_RDC, STORE_RDC, COUNT(*) AS PICKS, SUM(QTY) AS UNITS,
                   COUNT(DISTINCT STORE_CODE) AS STORES
              FROM dbo.{S.BIN_PLAN} WHERE SESSION_ID = :s AND ROW_TYPE = N'ALLOC'
             GROUP BY BIN_RDC, STORE_RDC ORDER BY BIN_RDC, STORE_RDC""", p)
        out["leftovers"] = _rows(conn, f"""
            SELECT LEFT_REASON, COUNT(*) AS BINS, SUM(QTY) AS PCS, COUNT(DISTINCT ART) AS ARTICLES
              FROM dbo.{S.BIN_PLAN} WHERE SESSION_ID = :s AND ROW_TYPE = N'UNALLOC'
             GROUP BY LEFT_REASON ORDER BY SUM(QTY) DESC""", p)
        out["stores"] = _rows(conn, f"""
            SELECT TOP 15 a.STORE_CODE, MAX(a.ST_NM) AS ST_NM, MAX(m.RDC) AS RDC, COUNT(*) AS LINES,
                   SUM(a.ALLOC_QTY) AS UNITS
              FROM dbo.{S.ALLOC} a LEFT JOIN dbo.{S.STORE_MASTER} m ON m.STORE_CODE = a.STORE_CODE
             WHERE a.SESSION_ID = :s GROUP BY a.STORE_CODE ORDER BY SUM(a.ALLOC_QTY) DESC""", p)
        out["stores_without"] = conn.execute(text(f"""
            SELECT COUNT(*) FROM dbo.{S.STORE_MASTER} m
             WHERE NOT EXISTS (SELECT 1 FROM dbo.{S.ALLOC} a WHERE a.SESSION_ID = :s
                                AND a.STORE_CODE = m.STORE_CODE)"""), p).scalar()
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  paged tables
# ═══════════════════════════════════════════════════════════════════════════
def _page(conn, base: str, where: List[str], params: Dict[str, Any], cols: str, order: str,
          page: int, size: int) -> Dict[str, Any]:
    w = " AND ".join(where)
    size = min(max(int(size), 1), 500)
    page = max(int(page), 1)
    total = conn.execute(text(f"SELECT COUNT_BIG(*) FROM {base} WHERE {w}"), params).scalar()
    rows = _rows(conn, f"""SELECT {cols} FROM {base} WHERE {w} ORDER BY {order}
                           OFFSET :off ROWS FETCH NEXT :n ROWS ONLY""",
                 {**params, "off": (page - 1) * size, "n": size})
    return {"total": int(total or 0), "page": page, "size": size, "rows": rows}


def _like(where, params, col, key, value, exact=False):
    if value:
        v = str(value).strip()
        if exact:
            where.append(f"{col} = :{key}")
            params[key] = v
        else:
            where.append(f"{col} LIKE :{key}")
            params[key] = f"%{v}%"


LINE_SORT = {"seq": "ALLOC_SEQ", "qty": "ALLOC_QTY DESC, ALLOC_SEQ", "store": "STORE_CODE, ALLOC_SEQ",
             "sendable": "STILL_SENDABLE DESC, ALLOC_SEQ"}


def lines(sid: int, store=None, art=None, maj_cat=None, sort="seq", page=1, size=50) -> Dict[str, Any]:
    where, p = ["SESSION_ID = :s"], {"s": sid}
    _like(where, p, "STORE_CODE", "st", store, exact=True)
    _like(where, p, "ART", "art", art)
    _like(where, p, "MAJ_CAT", "cat", maj_cat)
    with data_engine.connect() as conn:
        return _page(conn, f"dbo.{S.ALLOC}", where, p,
                     "ALLOC_SEQ, STORE_CODE, ST_NM, ART, MAJ_CAT, BIN_SIZE, SZ, MBQ_ROUNDED, STK_TTL, "
                     "SHORTFALL, REQ_CAP, ALLOC_QTY, ART_BIN_QTY, ART_BIN_LEFT, REQ_CAP_LEFT, STILL_SENDABLE",
                     LINE_SORT.get(sort, LINE_SORT["seq"]), page, size)


PICK_SORT = {"walk": "BIN_RDC, BIN, ART, PICK_SEQ", "store": "STORE_CODE, BIN, ART",
             "qty": "QTY DESC, BIN, ART"}


def picks(sid: int, store=None, bin_=None, art=None, maj_cat=None, rdc=None, cross=False,
          sort="walk", page=1, size=50) -> Dict[str, Any]:
    where, p = ["SESSION_ID = :s", "ROW_TYPE = N'ALLOC'"], {"s": sid}
    _like(where, p, "STORE_CODE", "st", store, exact=True)
    _like(where, p, "BIN", "bin", bin_)
    _like(where, p, "ART", "art", art)
    _like(where, p, "MAJ_CAT", "cat", maj_cat)
    _like(where, p, "BIN_RDC", "rdc", rdc, exact=True)
    if cross:
        where.append("ISNULL(STORE_RDC, N'') <> ISNULL(BIN_RDC, N'')")
    with data_engine.connect() as conn:
        res = _page(conn, f"dbo.{S.BIN_PLAN}", where, p,
                    "BIN, BIN_RDC, ART, MAJ_CAT, BIN_SIZE, SZ, STORE_CODE, ST_NM, STORE_RDC, QTY, "
                    "PICK_SEQ, BIN_QTY, BIN_QTY_LEFT, STORE_ALLOC_QTY, ALLOC_SEQ",
                    PICK_SORT.get(sort, PICK_SORT["walk"]), page, size)
    for r in res["rows"]:
        r["CROSS"] = (r.get("STORE_RDC") or "") != (r.get("BIN_RDC") or "")
    return res


def leftovers(sid: int, reason=None, maj_cat=None, bin_=None, art=None, page=1, size=50) -> Dict[str, Any]:
    where, p = ["SESSION_ID = :s", "ROW_TYPE = N'UNALLOC'"], {"s": sid}
    _like(where, p, "LEFT_REASON", "why", reason, exact=True)
    _like(where, p, "MAJ_CAT", "cat", maj_cat)
    _like(where, p, "BIN", "bin", bin_)
    _like(where, p, "ART", "art", art)
    with data_engine.connect() as conn:
        return _page(conn, f"dbo.{S.BIN_PLAN}", where, p,
                     "BIN, BIN_RDC, ART, MAJ_CAT, BIN_SIZE, SEASON, BIN_QTY, PICKED_QTY, QTY, LEFT_REASON, "
                     "STORES_WANTING, TOTAL_SHORTFALL", "QTY DESC, BIN, ART", page, size)


# ═══════════════════════════════════════════════════════════════════════════
#  why no stock?
# ═══════════════════════════════════════════════════════════════════════════
OUTCOME_WORDS = {
    "ART_EMPTY": "the article had run out by the time this store's turn came",
    "NO_RDC": "the article is not in a warehouse this store may draw from under the warehouse rule",
    "NO_REQ": "the store has no REQ line for this category + size",
    "REQ_FULL": "the store's REQ for this category + size was already filled by other articles",
    "BELOW_MIN": "what it could have had was below the smallest line",
    "SERVED": "it was served",
}


def why(sid: int, store: str, art: Optional[str] = None) -> Dict[str, Any]:
    s = _need(sid)
    store = (store or "").strip().upper()
    if not store:
        raise ValueError("Give a store code")
    art = (art or "").strip() or None
    ctx = context(s)
    steps: List[Dict[str, Any]] = []

    def step(label, value, detail="", ok=True):
        steps.append({"label": label, "value": value, "detail": detail, "ok": ok})

    with data_engine.connect() as conn:
        sm = conn.execute(text(f"SELECT ST_NM, RDC FROM dbo.{S.STORE_MASTER} WHERE STORE_CODE = :s"),
                          {"s": store}).fetchone()
        got = _rows(conn, f"""SELECT MAJ_CAT, COUNT(*) AS LINES, SUM(ALLOC_QTY) AS UNITS
                                FROM dbo.{S.ALLOC} WHERE SESSION_ID = :sid AND STORE_CODE = :s
                               GROUP BY MAJ_CAT ORDER BY SUM(ALLOC_QTY) DESC""", {"sid": sid, "s": store})
    if not sm:
        step("Store in Store Master", "No", f"{store} is not in Store Master, so no demand row was ever built "
             "for it and no session can send it anything. Reload Store Master with it.", ok=False)
        return {"store": store, "art": art, "verdict": f"{store} gets nothing: it is not in Store Master.",
                "steps": steps, "context": ctx}
    step("Store in Store Master", f"Yes · {sm[0] or ''} · {sm[1] or 'no RDC'}")
    units = sum(g["UNITS"] or 0 for g in got)
    step("This session sent it", f"{units:,} units on {sum(g['LINES'] for g in got):,} lines",
         ", ".join(f"{g['MAJ_CAT']} {int(g['UNITS']):,}" for g in got[:8]) or "nothing", ok=units > 0)

    if art is None:
        return _why_store(s, store, sm, got, steps, ctx)
    return _why_article(s, store, sm, art, steps, ctx)


def _why_store(s, store, sm, got, steps, ctx) -> Dict[str, Any]:
    """Store level: what it was short of, what it got, and its biggest misses."""
    misses: List[Dict[str, Any]] = []
    if ctx["current"]:
        with data_engine.connect() as conn:
            misses = _rows(conn, f"""
                SELECT TOP 20 m.ART, m.MAJ_CAT, m.BIN_SIZE, m.SHORTFALL, m.MBQ_ROUNDED, m.STK_TTL, m.BIN_QTY
                  FROM dbo.{S.ART_MBQ} m
                 WHERE m.STORE_CODE = :st AND m.SHORTFALL >= 1 AND m.BIN_QTY > 0
                   AND NOT EXISTS (SELECT 1 FROM dbo.{S.ALLOC} a WHERE a.SESSION_ID = :sid
                                    AND a.STORE_CODE = m.STORE_CODE AND a.ART = m.ART)
                 ORDER BY m.SHORTFALL DESC, m.ART""", {"st": store, "sid": s["SESSION_ID"]})
            tot = conn.execute(text(f"""SELECT COUNT(*), SUM(SHORTFALL) FROM dbo.{S.ART_MBQ}
                                         WHERE STORE_CODE = :st AND SHORTFALL >= 1 AND BIN_QTY > 0"""),
                               {"st": store}).fetchone()
        steps.insert(1, {"label": "Short of", "value": f"{float(tot[1] or 0):,.0f} units on {int(tot[0] or 0):,} articles",
                         "detail": "Articles that have bin stock and where the store is below its target.",
                         "ok": True})
    units = sum(g["UNITS"] or 0 for g in got)
    verdict = (f"{store} received {units:,} units in session {s['SESSION_ID']}." if units else
               f"{store} received nothing in session {s['SESSION_ID']}.")
    if misses:
        verdict += " Pick an article below to see why it got none of it."
    return {"store": store, "art": None, "verdict": verdict, "steps": steps, "misses": misses,
            "context": ctx}


def _why_article(s, store, sm, art, steps, ctx) -> Dict[str, Any]:
    sid = s["SESSION_ID"]
    with data_engine.connect() as conn:
        a = conn.execute(text(f"""SELECT MAX(MAJ_CAT), MAX([SIZE]), SUM(CAST(QTY AS BIGINT)), COUNT(DISTINCT BIN)
                                    FROM dbo.{S.BIN_MASTER} WHERE ART = :a"""), {"a": art}).fetchone()
        line = conn.execute(text(f"""SELECT ALLOC_QTY, SHORTFALL, ALLOC_SEQ, REQ_CAP, REQ_CAP_LEFT
                                       FROM dbo.{S.ALLOC} WHERE SESSION_ID = :sid AND STORE_CODE = :s AND ART = :a"""),
                            {"sid": sid, "s": store, "a": art}).fetchone()
        bins = _rows(conn, f"""SELECT BIN, BIN_RDC, QTY FROM dbo.{S.BIN_PLAN}
                                WHERE SESSION_ID = :sid AND ROW_TYPE = N'ALLOC' AND STORE_CODE = :s AND ART = :a
                                ORDER BY PICK_SEQ""", {"sid": sid, "s": store, "a": art})
        held = conn.execute(text(f"""SELECT SUM(QTY), SUM(CASE WHEN ROW_TYPE = N'ALLOC' THEN QTY ELSE 0 END),
                                            COUNT(DISTINCT CASE WHEN ROW_TYPE = N'ALLOC' THEN STORE_CODE END)
                                       FROM dbo.{S.BIN_PLAN} WHERE SESSION_ID = :sid AND ART = :a"""),
                            {"sid": sid, "a": art}).fetchone()
    if not (a and a[0]):
        step = {"label": "Article in Bin Master", "value": "No", "ok": False,
                "detail": f"There is no GRT stock of {art}."}
        steps.append(step)
        return {"store": store, "art": art, "verdict": f"No stock: {art} is not in any bin.", "steps": steps,
                "context": ctx}
    cat, size = a[0], a[1]
    steps.append({"label": "Article in the bins (at the session)", "ok": True,
                  "value": f"{int(held[0] or 0):,} pcs" if held and held[0] is not None else f"{int(a[2] or 0):,} pcs",
                  "detail": f"{cat} · size {size or '(blank)'} · {int(held[1] or 0):,} pcs went to "
                            f"{int(held[2] or 0):,} store(s) in this session."})
    if line:
        steps.append({"label": "Line in this session", "value": f"{int(line[0]):,} units", "ok": True,
                      "detail": f"Shortfall {float(line[1]):g}, served at position {int(line[2]):,} of the run. "
                                f"REQ for {cat} / {size or '(blank)'}: {int(line[3])}, left after this line {int(line[4])}."})
        steps.append({"label": "Picked from", "value": f"{len(bins)} bin(s)", "ok": True,
                      "detail": " · ".join(f"{b['BIN']} ({b['QTY']})" + ("" if b["BIN_RDC"] == sm[1] else " cross")
                                           for b in bins)})
        return {"store": store, "art": art, "steps": steps, "context": ctx,
                "verdict": f"{store} got {int(line[0]):,} of {art} in session {sid}."}

    if not ctx["current"]:
        steps.append({"label": "Replay", "value": "Not possible", "ok": False,
                      "detail": " ".join(ctx["why"]) + " The exact turn can only be replayed on the data the "
                                "session read."})
        return {"store": store, "art": art, "steps": steps, "context": ctx,
                "verdict": f"{store} got none of {art}. The data has changed since this session, so the "
                           f"exact reason cannot be replayed; run a new session to trace it."}
    return _replay(s, store, sm, art, cat, steps, ctx)


def _replay(s, store, sm, art, cat, steps, ctx) -> Dict[str, Any]:
    """Re-run the article's category exactly as the session did, tracing one
    store × article. Deterministic: same inputs, same order, same result — and
    the replayed category is compared with what the session stored."""
    opts = {k: (s["settings"] or {}).get(k) for k in ("priority", "min_qty", "fill_mode", "fair_basis",
                                                       "bin_pick", "cross_rdc")}
    t0 = time.time()
    with data_engine.connect() as conn:
        store_rdc, store_nm = E.load_stores(conn, A.LIVE)
        part = E.load_part(conn, A.LIVE, cat)
        cand = E.load_candidates(conn, A.LIVE, cat, store_rdc, part["art_size"], E.codes_are_ascii(conn, A.LIVE))
        stored = conn.execute(text(f"""SELECT COUNT(*), ISNULL(SUM(ALLOC_QTY), 0) FROM dbo.{S.ALLOC}
                                        WHERE SESSION_ID = :sid AND MAJ_CAT = :c"""),
                              {"sid": s["SESSION_ID"], "c": cat}).fetchone()
        row = conn.execute(text(f"""SELECT MBQ_ROUNDED, CAST(STK_TTL AS FLOAT), CAST(SHORTFALL AS FLOAT)
                                      FROM dbo.{S.ART_MBQ} WHERE STORE_CODE = :s AND ART = :a"""),
                           {"s": store, "a": art}).fetchone()
    work = E.prepare_work(cand, opts["priority"])
    mine = work[(work["STORE_CODE"] == store) & (work["ART"] == art)]
    if row is None:
        steps.append({"label": "Demand row", "value": "None", "ok": False,
                      "detail": f"The build made no row for {store} × {art}: the store's REQ for {cat} at this "
                                f"size is below the minimum (see Build MBQ → Why this number?)."})
        return {"store": store, "art": art, "steps": steps, "context": ctx,
                "verdict": f"{store} got none of {art}: it asks for none of {cat} at this size."}
    steps.append({"label": "Target · stock · shortfall", "ok": (row[2] or 0) >= 1,
                  "value": f"{row[0]} · {'—' if row[1] is None else f'{row[1]:g}'} · {'—' if row[2] is None else f'{row[2]:g}'}",
                  "detail": "From the demand build. A shortfall below 1 is never a candidate."})
    if mine.empty:
        return {"store": store, "art": art, "steps": steps, "context": ctx,
                "verdict": f"{store} got none of {art}: it is not short of it (shortfall below 1)."}
    pos = int(((work["STORE_CODE"] == store) & (work["ART"] == art)).to_numpy().argmax()) + 1
    ahead = work.iloc[:pos - 1]
    ahead_same = ahead[ahead["ART"] == art]
    steps.append({"label": "Its place in the queue", "ok": True,
                  "value": f"{pos:,} of {len(work):,} in {cat}",
                  "detail": f"{len(ahead_same):,} store(s) wanting this article were ranked ahead "
                            f"({PRIORITY_WORDS.get(opts['priority'], opts['priority'])} first; ties go to the "
                            f"bigger shortfall, then the bigger size share, then store code)."})
    trace: Dict[str, Any] = {"key": (store, art)}
    res = E.run_category(cand, part, opts, store_rdc, store_nm, part["art_attrs"], trace=trace)
    same = (len(res["lines"]) == int(stored[0]) and sum(r["ALLOC_QTY"] for r in res["lines"]) == int(stored[1]))
    steps.append({"label": "Replay of the session", "ok": same,
                  "value": "identical" if same else "DIFFERENT",
                  "detail": (f"{cat} re-run as session {s['SESSION_ID']} ran it: {len(res['lines']):,} lines, "
                             f"{sum(r['ALLOC_QTY'] for r in res['lines']):,} units — the same as stored. "
                             f"{time.time() - t0:.1f}s.") if same else
                            (f"The re-run gave {len(res['lines']):,} lines / {sum(r['ALLOC_QTY'] for r in res['lines']):,} "
                             f"units; the session stored {int(stored[0]):,} / {int(stored[1]):,}. The reason below may "
                             f"not be the session's.")})
    verdict = _trace_words(trace, store, art, sm[1])
    steps.extend(verdict["steps"])
    return {"store": store, "art": art, "steps": steps, "context": ctx, "verdict": verdict["text"],
            "trace": {k: v for k, v in trace.items() if k != "key"}}


def _trace_words(t: Dict[str, Any], store: str, art: str, home: Optional[str]) -> Dict[str, Any]:
    steps = []
    if t.get("mode") == "GREEDY":
        o = t["outcome"]
        before = t.get("before_same_art") or []
        took = sum(b["qty"] for b in before)
        if o == "ART_EMPTY":
            steps.append({"label": "At its turn", "value": "0 left", "ok": False,
                          "detail": f"{len(before)} store(s) ahead took all {took:,} of {t['stock']:,} pcs: "
                                    + ", ".join(f"{b['store']} {b['qty']}" for b in before[:12])
                                    + (" …" if len(before) > 12 else "")})
            text_ = f"{store} got none of {art}: the {t['stock']:,} pcs were gone before its turn."
        elif o == "REQ_FULL":
            grp = t.get("before_same_group") or []
            steps.append({"label": "At its turn", "value": f"REQ {t['req_cap']} already used", "ok": False,
                          "detail": f"{len(grp)} other article(s) of the same category + size filled it first: "
                                    + ", ".join(f"{g['art']} {g['qty']}" for g in grp[:10])})
            text_ = (f"{store} got none of {art}: its REQ of {t['req_cap']} for this category + size was already "
                     f"filled by other articles.")
        elif o == "NO_RDC":
            steps.append({"label": "At its turn", "value": "No warehouse allowed", "ok": False,
                          "detail": f"The article is not in {home}, and the warehouse rule lets {store} draw only "
                                    f"from its own RDC."})
            text_ = f"{store} got none of {art}: none of it is in {home}, and the rule was Own RDC only."
        elif o == "NO_REQ":
            text_ = f"{store} got none of {art}: it has no REQ line for this category + size."
        elif o == "BELOW_MIN":
            steps.append({"label": "At its turn", "value": f"only {t.get('qty')} possible", "ok": False,
                          "detail": "Below the smallest line allowed in this session."})
            text_ = f"{store} got none of {art}: only {t.get('qty')} could have gone, below the smallest line."
        else:
            text_ = f"The replay serves {store} {t.get('qty')} of {art}, but the session stored none — see the replay row."
        return {"steps": steps, "text": text_}
    if t.get("mode") == "ROUND_ROBIN":
        sc = t.get("screened")
        if sc == "ART_EMPTY_AT_START":
            return {"steps": [], "text": f"{store} got none of {art}: the article was already empty when its queue began."}
        if sc != "LIVE":
            return {"steps": [], "text": f"{store} got none of {art}: {OUTCOME_WORDS.get(sc, sc)}."}
        others = t.get("others") or []
        steps.append({"label": "Sharing", "value": f"{len(others)} store(s) shared it", "ok": False,
                      "detail": f"{t['stock']:,} pcs handed out {t['step']} at a time: "
                                + ", ".join(f"{o['store']} {o['qty']}" for o in others[:12])
                                + (" …" if len(others) > 12 else "")})
        if (t.get("room_at_end") or 0) < t.get("step", 1):
            text_ = (f"{store} got none of {art}: its REQ of {t['req_cap']} for this category + size was used up by "
                     f"other articles before a unit of this one reached it.")
        else:
            text_ = (f"{store} got none of {art}: the {t['stock']:,} pcs ran out before its turn in the "
                     f"rotation.")
        return {"steps": steps, "text": text_}
    return {"steps": [], "text": f"{store} × {art} was not a candidate in this session."}


# ═══════════════════════════════════════════════════════════════════════════
#  compare two sessions
# ═══════════════════════════════════════════════════════════════════════════
def compare(a: int, b: int) -> Dict[str, Any]:
    sa, sb = _need(a), _need(b)
    for s in (sa, sb):
        if s["STATUS"] != "DONE" or s.get("DRY_RUN"):
            raise ValueError(f"Session {s['SESSION_ID']} has no stored lines (a dry run, or not finished).")
    p = {"a": a, "b": b}
    with data_engine.connect() as conn:
        lines_cmp = conn.execute(text(f"""
            SELECT SUM(CASE WHEN x.QA IS NOT NULL AND x.QB IS NULL THEN 1 ELSE 0 END),
                   SUM(CASE WHEN x.QA IS NULL AND x.QB IS NOT NULL THEN 1 ELSE 0 END),
                   SUM(CASE WHEN x.QA <> x.QB THEN 1 ELSE 0 END),
                   SUM(CASE WHEN x.QA = x.QB THEN 1 ELSE 0 END)
              FROM (SELECT ISNULL(la.STORE_CODE, lb.STORE_CODE) STORE_CODE, la.ALLOC_QTY QA, lb.ALLOC_QTY QB
                      FROM (SELECT STORE_CODE, ART, ALLOC_QTY FROM dbo.{S.ALLOC} WHERE SESSION_ID = :a) la
                      FULL JOIN (SELECT STORE_CODE, ART, ALLOC_QTY FROM dbo.{S.ALLOC} WHERE SESSION_ID = :b) lb
                        ON la.STORE_CODE = lb.STORE_CODE AND la.ART = lb.ART) x"""), p).fetchone()
        stores = _rows(conn, f"""
            SELECT ISNULL(x.STORE_CODE, y.STORE_CODE) AS STORE_CODE, ISNULL(x.U, 0) AS A, ISNULL(y.U, 0) AS B,
                   ISNULL(y.U, 0) - ISNULL(x.U, 0) AS DELTA, m.ST_NM, m.RDC
              FROM (SELECT STORE_CODE, SUM(ALLOC_QTY) U FROM dbo.{S.ALLOC} WHERE SESSION_ID = :a GROUP BY STORE_CODE) x
              FULL JOIN (SELECT STORE_CODE, SUM(ALLOC_QTY) U FROM dbo.{S.ALLOC} WHERE SESSION_ID = :b GROUP BY STORE_CODE) y
                ON x.STORE_CODE = y.STORE_CODE
              LEFT JOIN dbo.{S.STORE_MASTER} m ON m.STORE_CODE = ISNULL(x.STORE_CODE, y.STORE_CODE)""", p)
        cats = _rows(conn, f"""
            SELECT ISNULL(x.MAJ_CAT, y.MAJ_CAT) AS MAJ_CAT, ISNULL(x.U, 0) AS A, ISNULL(y.U, 0) AS B,
                   ISNULL(y.U, 0) - ISNULL(x.U, 0) AS DELTA
              FROM (SELECT MAJ_CAT, SUM(ALLOC_QTY) U FROM dbo.{S.ALLOC} WHERE SESSION_ID = :a GROUP BY MAJ_CAT) x
              FULL JOIN (SELECT MAJ_CAT, SUM(ALLOC_QTY) U FROM dbo.{S.ALLOC} WHERE SESSION_ID = :b GROUP BY MAJ_CAT) y
                ON x.MAJ_CAT = y.MAJ_CAT""", p)
    changed = [r for r in stores if r["DELTA"]]
    pick = lambda s: {k: s.get(k) for k in ("SESSION_ID", "CREATED_AT", "ALLOC_FILL_MODE", "ALLOC_FAIR_BASIS",
                                             "ALLOC_CROSS_RDC", "ALLOC_PRIORITY", "ALLOC_MIN_QTY", "ALLOC_BIN_PICK",
                                             "BUILD_ID", "SUPPLY_UNITS", "UNITS_ALLOCATED", "UNITS_LEFT",
                                             "CROSS_RDC_UNITS", "ART_LINES", "BIN_LINES", "STORES_SERVED",
                                             "CHECKS_PASSED")}
    return {
        "a": pick(sa), "b": pick(sb),
        "same_build": sa.get("BUILD_ID") == sb.get("BUILD_ID"),
        "lines": {"only_a": int(lines_cmp[0] or 0), "only_b": int(lines_cmp[1] or 0),
                  "qty_changed": int(lines_cmp[2] or 0), "same": int(lines_cmp[3] or 0)},
        "stores_changed": len(changed),
        "stores_up": sorted([r for r in changed if r["DELTA"] > 0], key=lambda r: -r["DELTA"])[:15],
        "stores_down": sorted([r for r in changed if r["DELTA"] < 0], key=lambda r: r["DELTA"])[:15],
        "stores_gained_any": sum(1 for r in stores if not r["A"] and r["B"]),
        "stores_lost_all": sum(1 for r in stores if r["A"] and not r["B"]),
        "categories": sorted([c for c in cats if c["DELTA"]], key=lambda c: -abs(c["DELTA"]))[:15],
    }


# ═══════════════════════════════════════════════════════════════════════════
#  export — the pick list the warehouse works from
# ═══════════════════════════════════════════════════════════════════════════
def export_ready(s: Dict[str, Any]) -> Optional[str]:
    """None when the session may be exported, else the reason it may not."""
    if s["STATUS"] != "DONE":
        return f"Session {s['SESSION_ID']} is {s['STATUS']}."
    if s.get("DRY_RUN"):
        return "A dry run has no pick list. Run it for real."
    if not s.get("CHECKS_PASSED"):
        return "Export is locked: at least one balance check failed on this session."
    return None


def purge_exports(sid: int) -> int:
    """Remove a deleted session's cached files."""
    n = 0
    for f in EXPORT_DIR.glob(f"GRT_ALC_session_{sid}_*") if EXPORT_DIR.exists() else []:
        f.unlink(missing_ok=True)
        n += 1
    return n


EXPORT_KINDS = {"xlsx": "xlsx", "picks": "csv", "leftovers": "csv"}
PICK_COLS = ["BIN_RDC", "BIN", "ART", "MAJ_CAT", "BIN_SIZE", "SZ", "QTY", "STORE_CODE", "ST_NM",
             "STORE_RDC", "PICK_SEQ", "BIN_QTY", "BIN_QTY_LEFT", "ALLOC_SEQ"]
LEFT_COLS = ["BIN_RDC", "BIN", "ART", "MAJ_CAT", "BIN_SIZE", "BIN_QTY", "PICKED_QTY", "QTY", "LEFT_REASON",
             "STORES_WANTING", "TOTAL_SHORTFALL"]
LINE_COLS = ["ALLOC_SEQ", "STORE_CODE", "ST_NM", "ART", "MAJ_CAT", "BIN_SIZE", "SZ", "MBQ_ROUNDED",
             "STK_TTL", "SHORTFALL", "REQ_CAP", "ALLOC_QTY", "STILL_SENDABLE"]
FLOATS = {"STK_TTL", "SHORTFALL", "STILL_SENDABLE", "TOTAL_SHORTFALL"}


def _select(cols):
    """DECIMALs come out as FLOAT: no Python Decimal per cell."""
    return ", ".join(f"CAST({c} AS FLOAT)" if c in FLOATS else c for c in cols)


def export(sid: int, kind: str = "xlsx") -> Path:
    """One of the session's downloads, built once and kept (a session never
    changes):

        xlsx       Summary · Pick list (walking order) · Store totals · Lines ·
                   Leftovers by reason × category
        picks      the pick list as CSV
        leftovers  every bin position left holding stock, as CSV

    The full leftover list is CSV only: at 440k rows it would double the
    workbook's build time (the writer manages ~5k rows a second here), and it
    is analysis, not something the warehouse works from."""
    if kind not in EXPORT_KINDS:
        raise ValueError(f"kind must be one of {', '.join(EXPORT_KINDS)}")
    s = _need(sid)
    why_not = export_ready(s)
    if why_not:
        raise ValueError(why_not)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    tag = str(s.get("SESSION_TAG") or "").replace(":", "").replace(" ", "_").replace("-", "")
    name = {"xlsx": "pick_list", "picks": "pick_list", "leftovers": "leftovers"}[kind]
    path = EXPORT_DIR / f"GRT_ALC_session_{sid}_{tag}_{name}.{EXPORT_KINDS[kind]}"
    if path.exists():
        return path
    t0 = time.time()
    tmp = path.with_suffix(path.suffix + ".part")
    raw = data_engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.arraysize = 50_000
        if kind == "leftovers":
            cur.execute(f"""SELECT {_select(LEFT_COLS)} FROM dbo.{S.BIN_PLAN}
                             WHERE SESSION_ID = ? AND ROW_TYPE = N'UNALLOC' ORDER BY BIN_RDC, BIN, ART""", sid)
            _csv(tmp, LEFT_COLS, cur.fetchall())
            tmp.replace(path)
            return path
        cur.execute(f"""SELECT {_select(PICK_COLS)} FROM dbo.{S.BIN_PLAN}
                         WHERE SESSION_ID = ? AND ROW_TYPE = N'ALLOC' ORDER BY BIN_RDC, BIN, ART, PICK_SEQ""", sid)
        picks_ = [(*r, "Y" if (r[9] or "") != (r[0] or "") else "") for r in cur.fetchall()]
        if kind == "picks":
            _csv(tmp, PICK_COLS + ["CROSS"], picks_)
            tmp.replace(path)
            return path
        cur.execute(f"""SELECT STORE_CODE, MAX(ST_NM), MAX(STORE_RDC), COUNT(DISTINCT ART), COUNT(*), SUM(QTY),
                               SUM(CASE WHEN ISNULL(STORE_RDC, N'') <> ISNULL(BIN_RDC, N'') THEN QTY ELSE 0 END)
                          FROM dbo.{S.BIN_PLAN} WHERE SESSION_ID = ? AND ROW_TYPE = N'ALLOC'
                         GROUP BY STORE_CODE ORDER BY STORE_CODE""", sid)
        by_store = cur.fetchall()
        cur.execute(f"SELECT {_select(LINE_COLS)} FROM dbo.{S.ALLOC} WHERE SESSION_ID = ? ORDER BY ALLOC_SEQ", sid)
        lines_ = cur.fetchall()
        cur.execute(f"""SELECT LEFT_REASON, MAJ_CAT, COUNT(*), COUNT(DISTINCT ART), SUM(QTY)
                          FROM dbo.{S.BIN_PLAN} WHERE SESSION_ID = ? AND ROW_TYPE = N'UNALLOC'
                         GROUP BY LEFT_REASON, MAJ_CAT ORDER BY LEFT_REASON, SUM(QTY) DESC""", sid)
        left_sum = cur.fetchall()
    finally:
        raw.close()

    import xlsxwriter
    wb = xlsxwriter.Workbook(str(tmp), {"constant_memory": True})
    bold = wb.add_format({"bold": True, "bg_color": "#EEF2F7", "border": 1})
    num = wb.add_format({"num_format": "#,##0"})
    dec = wb.add_format({"num_format": "#,##0.##"})

    def sheet(name, cols, rows, widths=None, fmt_cols=None):
        """Whole rows at a time, number formats set once per column."""
        ws = wb.add_worksheet(name)
        for c, h in enumerate(cols):
            ws.set_column(c, c, (widths or {}).get(h, max(10, len(h) + 2)), (fmt_cols or {}).get(h))
        ws.write_row(0, 0, cols, bold)
        ws.freeze_panes(1, 0)
        for i, r in enumerate(rows, start=1):
            ws.write_row(i, 0, tuple(r))
        ws.autofilter(0, 0, len(rows), len(cols) - 1)
        return ws

    summary = wb.add_worksheet("Summary")
    summary.set_column(0, 0, 34)
    summary.set_column(1, 1, 56)
    facts = [
        ("Session", s["SESSION_ID"]), ("Run at", str(s.get("CREATED_AT"))[:19].replace("T", " ")),
        ("Run by", s.get("CREATED_BY")), ("Note", s.get("NOTE") or ""),
        ("Demand build", s.get("BUILD_ID")),
        ("How stock is shared", s.get("ALLOC_FILL_MODE") + (f" / {s['ALLOC_FAIR_BASIS']}" if s.get("ALLOC_FAIR_BASIS") else "")),
        ("Which warehouse may ship", WORDS.get(s.get("ALLOC_CROSS_RDC"), s.get("ALLOC_CROSS_RDC"))),
        ("Who is served first", PRIORITY_WORDS.get(s.get("ALLOC_PRIORITY"), s.get("ALLOC_PRIORITY"))),
        ("Smallest line", s.get("ALLOC_MIN_QTY")), ("Which bin", s.get("ALLOC_BIN_PICK")),
        ("Bin stock (pcs)", s.get("SUPPLY_UNITS")), ("Allocated (pcs)", s.get("UNITS_ALLOCATED")),
        ("Left in bins (pcs)", s.get("UNITS_LEFT")), ("Cross-warehouse (pcs)", s.get("CROSS_RDC_UNITS")),
        ("Store × article lines", s.get("ART_LINES")), ("Pick rows", s.get("BIN_LINES")),
        ("Stores served", s.get("STORES_SERVED")),
        ("Pick list order", "Warehouse, then bin, then article, then PICK_SEQ — walk the bins in this order"),
        ("Full leftover list", "Download separately as CSV from the Sessions page"),
    ]
    summary.write(0, 0, "GRT ALC · Bin-to-Bin pick list", bold)
    for i, (k, v) in enumerate(facts, start=2):
        summary.write(i, 0, k)
        summary.write(i, 1, v)
    r0 = len(facts) + 3
    summary.write(r0, 0, "Balance checks", bold)
    for i, c in enumerate(s.get("checks") or [], start=r0 + 1):
        summary.write(i, 0, ("PASS  " if c["level"] == "ok" else "FAIL  ") + c["code"])
        summary.write(i, 1, c["title"])

    sheet("Pick list", PICK_COLS + ["CROSS"], picks_, widths={"BIN": 18, "ART": 16, "MAJ_CAT": 18, "ST_NM": 22},
          fmt_cols={"QTY": num, "BIN_QTY": num, "BIN_QTY_LEFT": num})
    sheet("Store totals", ["STORE_CODE", "ST_NM", "STORE_RDC", "ARTICLES", "PICKS", "UNITS", "CROSS_UNITS"],
          by_store, widths={"ST_NM": 24}, fmt_cols={"UNITS": num, "CROSS_UNITS": num})
    sheet("Lines", LINE_COLS, lines_, widths={"ART": 16, "MAJ_CAT": 18, "ST_NM": 22},
          fmt_cols={"STK_TTL": dec, "SHORTFALL": dec, "STILL_SENDABLE": dec})
    sheet("Leftovers by reason", ["LEFT_REASON", "MAJ_CAT", "BINS", "ARTICLES", "PCS"], left_sum,
          widths={"LEFT_REASON": 18, "MAJ_CAT": 20}, fmt_cols={"PCS": num})
    wb.close()
    tmp.replace(path)
    logger.info(f"[b2b sessions] exported session {sid}: {len(picks_):,} picks, {len(lines_):,} lines "
                f"in {time.time() - t0:.1f}s → {path.name}")
    return path


def _csv(path: Path, cols: List[str], rows) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        w.writerows(rows)
