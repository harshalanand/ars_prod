"""
FA & CONS — Gap Report.

For each store × reference article, compares the target (MBQ) against what the store
already has (store stock) + what's on the way (open pending), then validates the
warehouse (MSA/DC) pool to recommend ONE action:

    short & warehouse has it      → DISPATCH
    short & warehouse partial      → DISPATCH (avail) + PURCHASE (rest)
    short & warehouse empty        → PURCHASE
    shelf already ≥ MBQ (+pending) → HOLD the pending
    shelf > MBQ                    → STORE_RETURN the excess
    covered = MBQ                  → OK

Returns line-level rows (Store Gap), an aggregated Purchase Requirement (per ref across
the network), and a summary. Read-only.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.database.session import data_engine
from app.services import facons_store_list_service as store_svc
from app.services import facons_alloc_service as alloc

MBQ = "ARS_FACONS_MBQ"
STOCK = "ARS_FACONS_STOCK"
MSA = "ARS_FACONS_MSA"
PEND = "ARS_FACONS_PEND"


def _pack_floor(qty: float, pack: int) -> float:
    pack = pack if pack and pack > 0 else 1
    return float(int(qty // pack) * pack)


def compute(stream: Optional[str] = "ALL", store_scope: str = "UPC") -> Dict[str, Any]:
    streams = alloc._stream_list(stream)
    if not streams:
        raise ValueError("stream must be FA / CONS / ALL")
    scope = (store_scope or "UPC").strip().upper()
    if scope not in ("UPC", "OLD", "ALL"):
        raise ValueError("store_scope must be UPC / OLD / ALL")

    stores = [s for s in store_svc.list_stores()
              if s.get("rdc") and (scope == "ALL" and s["status"] in ("UPC", "OLD") or s["status"] == scope)]
    store_rdc = {s["st_cd"]: str(s["rdc"]).strip() for s in stores}
    store_prio = {s["st_cd"]: s["priority"] for s in stores}
    scoped = set(store_rdc)

    sp = ", ".join("'" + s + "'" for s in streams)
    with data_engine.connect() as c:
        mbq_rows = c.execute(text(
            f"SELECT stream, st_cd, ref_art, mbq_q, maj_cat, ref_art_desc, "
            f"ISNULL(source_type,'CENTRAL') source_type FROM {MBQ} WHERE stream IN ({sp})")).mappings().all()
        store_stock = alloc._latest_map(c, STOCK, "st_cd", "store_stk_ttl", streams)
        pool = alloc._latest_map(c, MSA, "rdc", "dc_stk_ttl", streams)
        packs = alloc._pack_map(c, [r["ref_art"] for r in mbq_rows])
        pend_rows = c.execute(text(
            f"SELECT stream, st_cd, ref_art, SUM(qty-delivered) q FROM {PEND} WHERE status='OPEN' "
            f"AND stream IN ({sp}) GROUP BY stream, st_cd, ref_art")).fetchall()
    pending = {(str(a), str(b).strip().upper(), str(cc).strip()): float(d or 0) for a, b, cc, d in pend_rows}

    work = [r for r in mbq_rows if r["st_cd"] in scoped]
    work.sort(key=lambda r: (store_prio.get(r["st_cd"], 1e9), str(r["ref_art"])))
    pool_rem = dict(pool)

    lines: List[Dict[str, Any]] = []
    for r in work:
        strm, st, ref = r["stream"], r["st_cd"], str(r["ref_art"]).strip()
        rdc = store_rdc.get(st, "")
        mbq = float(r["mbq_q"] or 0)
        stk = store_stock.get((strm, st, ref), 0.0)
        pend = pending.get((strm, st, ref), 0.0)
        covered = stk + pend
        gap = mbq - covered
        pack = packs.get(ref, 1)
        warehouse = pool_rem.get((strm, rdc, ref), 0.0)
        dispatch = purchase = ret = hold = 0.0
        if gap > 0:
            dispatch = _pack_floor(min(gap, warehouse), pack)
            if dispatch > 0:
                pool_rem[(strm, rdc, ref)] = warehouse - dispatch
            purchase = round(gap - dispatch, 3)
            atr = "DISPATCH+PURCHASE" if dispatch > 0 and purchase > 0 else ("DISPATCH" if dispatch > 0 else "PURCHASE")
        elif stk > mbq:
            ret = round(stk - mbq, 3)
            atr = "STORE_RETURN"
        elif pend > 0:
            hold = pend
            atr = "HOLD"
        else:
            atr = "OK"
        lines.append({
            "stream": strm, "st_cd": st, "rdc": rdc, "priority": store_prio.get(st),
            "ref_art": ref, "ref_art_desc": r["ref_art_desc"], "maj_cat": r["maj_cat"],
            "source_type": r["source_type"], "mbq": mbq, "store_stock": stk, "pending": pend,
            "covered": round(covered, 3), "gap": round(gap, 3), "warehouse": warehouse,
            "dispatch": dispatch, "purchase": purchase, "return_qty": ret, "hold": hold, "atr": atr,
        })

    # Purchase Requirement — net qty to buy per (stream, ref) across the network
    agg: Dict[tuple, Dict[str, Any]] = {}
    for ln in lines:
        k = (ln["stream"], ln["ref_art"])
        a = agg.setdefault(k, {"stream": ln["stream"], "ref_art": ln["ref_art"],
                               "ref_art_desc": ln["ref_art_desc"], "maj_cat": ln["maj_cat"],
                               "stores": 0, "demand": 0.0, "store_stock": 0.0, "pending": 0.0,
                               "warehouse": pool.get((ln["stream"], ln["rdc"], ln["ref_art"]), 0.0),
                               "dispatch": 0.0, "purchase": 0.0})
        a["stores"] += 1
        a["demand"] += ln["mbq"]
        a["store_stock"] += ln["store_stock"]
        a["pending"] += ln["pending"]
        a["dispatch"] += ln["dispatch"]
        a["purchase"] += ln["purchase"]
    purchase_req = sorted((a for a in agg.values() if a["purchase"] > 0), key=lambda x: -x["purchase"])

    summary = {
        "lines": len(lines),
        "short": sum(1 for l in lines if l["gap"] > 0),
        "to_dispatch": round(sum(l["dispatch"] for l in lines), 3),
        "to_purchase": round(sum(l["purchase"] for l in lines), 3),
        "to_return": round(sum(l["return_qty"] for l in lines), 3),
        "to_hold": round(sum(l["hold"] for l in lines), 3),
        "ok": sum(1 for l in lines if l["atr"] == "OK"),
    }
    return {"lines": lines, "purchase_req": purchase_req, "summary": summary,
            "stream": (stream or "ALL").upper(), "store_scope": scope}
