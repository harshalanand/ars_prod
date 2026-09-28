#!/usr/bin/env python
"""
Small-scope smoke test for the Part 2 fan-out fix.

    python scripts/smoke_part2_fanout.py

Runs a tiny listing (a few stores, one MAJ_CAT) in BOTH modes and checks the
listing grain for duplicate (WERKS, GEN_ART_NUMBER, CLR) rows. Before the fix
the `all` run duplicated every option held at both warehouses; after it, both
modes must be duplicate-free.

Deliberately small so it costs ~1 minute, not the 20 a full run takes.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from loguru import logger                                  # noqa: E402
from sqlalchemy import text                                # noqa: E402
from app.database.session import get_data_engine           # noqa: E402

STORES = ["HH15", "HX55", "HN40", "HB35"]
MAJ_CATS = ["M_BRIEF"]


def run(mode: str) -> dict:
    from app.api.v1.endpoints.listing import GenerateRequest, _generate_listing_impl
    from app.services.listing_sessions import start_session, end_session
    sid = f"SMOKE_{mode.upper()}_{datetime.now().strftime('%H%M%S')}"
    kw = dict(rdc_mode=mode, store_codes=STORES, maj_cat_values=MAJ_CATS,
              run_mode="listing", alloc_type="FRESH",
              stock_consider_dt=str(date.today()), picking_dt=str(date.today()),
              allocation_mode="per_opt", parallel_workers=2)
    if mode == "own":
        kw["rdc_values"] = ["DW01", "DH24"]
    req = GenerateRequest(**kw)
    start_session(sid, "smoke_part2", req.model_dump())
    summary: dict = {}
    try:
        _generate_listing_impl(req, current_user=None, session_id=sid,
                               summary=summary, preset_batch_id=sid)
    finally:
        end_session(sid, "SUCCESS", summary)

    with get_data_engine().connect() as c:
        total = c.execute(text(
            "SELECT COUNT(*) FROM ARS_LISTING_WORKING "
            "WHERE ISNULL(GEN_ART_NUMBER,0) <> 0")).scalar()
        distinct = c.execute(text("""
            SELECT COUNT(*) FROM (SELECT 1 AS x FROM ARS_LISTING_WORKING
                    WHERE ISNULL(GEN_ART_NUMBER,0) <> 0
                    GROUP BY WERKS, GEN_ART_NUMBER,
                             LTRIM(RTRIM(ISNULL(CLR,'')))) Y""")).scalar()
        alloc_dups = c.execute(text("""
            SELECT COUNT(*) FROM (
                SELECT WERKS AS W,
                       LTRIM(RTRIM(CAST(VAR_ART AS NVARCHAR(50)))) AS VA,
                       LTRIM(RTRIM(ISNULL(SZ,''))) AS SZK
                  FROM ARS_ALLOC_WORKING
                 GROUP BY WERKS, LTRIM(RTRIM(CAST(VAR_ART AS NVARCHAR(50)))),
                          LTRIM(RTRIM(ISNULL(SZ,'')))
                HAVING COUNT(*) > 1) Z""")).scalar()
        # ARS_ALLOC_WORKING is only rebuilt when the run actually allocates.
        # At this scope it often allocates nothing, leaving the PREVIOUS run's
        # table in place — so its rows say nothing about this run. Trust it
        # only when it matches the stores we just ran.
        mine = c.execute(text(
            "SELECT COUNT(*) FROM ARS_ALLOC_WORKING WHERE WERKS IN :st"
            .replace(":st", "('" + "','".join(STORES) + "')"))).scalar()
        total_alloc = c.execute(text(
            "SELECT COUNT(*) FROM ARS_ALLOC_WORKING")).scalar()
        fresh = bool(total_alloc) and mine == total_alloc
        ship = c.execute(text(
            "SELECT SUM(TRY_CAST(SHIP_QTY AS FLOAT)) FROM ARS_ALLOC_WORKING")).scalar()
        split = c.execute(text(
            "SELECT COUNT(*) FROM ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID=:s"),
            {"s": sid}).scalar()
        split_ship = c.execute(text(
            "SELECT ISNULL(SUM(TRY_CAST(SHIP_QTY AS FLOAT)),0) "
            "FROM ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID=:s"), {"s": sid}).scalar()
    return {"sid": sid, "listing": total, "distinct": distinct,
            "alloc_dups": alloc_dups, "ship": ship or 0, "fresh": fresh,
            "split": split, "split_ship": split_ship or 0}


def main() -> int:
    logger.remove()
    print(f"scope: {STORES} x {MAJ_CATS}\n")
    ok = True
    for mode in ("own", "all"):
        r = run(mode)
        dup = r["listing"] - r["distinct"]
        verdict = "PASS" if dup == 0 else "FAIL"
        ok &= verdict == "PASS"
        print(f"  {mode.upper():<4} listing {r['listing']:>7} rows / "
              f"{r['distinct']:>7} distinct  -> dup {dup:>6}   {verdict}")
        if r["fresh"]:
            av = "PASS" if r["alloc_dups"] == 0 else "FAIL"
            ok &= r["alloc_dups"] == 0
            print(f"       alloc dup keys {r['alloc_dups']:>5}   {av}"
                  f"   ship {r['ship']:>8.0f}   split rows {r['split']:>6}")
        else:
            print("       alloc: this run allocated nothing — ARS_ALLOC_WORKING "
                  "still holds a previous run, not checked")
        if mode == "all" and r["fresh"] and r["split"]:
            d = r["split_ship"] - r["ship"]
            print(f"       conservation: split - alloc = {d:+.0f}   "
                  f"{'PASS' if abs(d) < 0.5 else 'FAIL'}")
            ok &= abs(d) < 0.5
        if mode == "own" and r["split"]:
            print(f"       !! OWN wrote {r['split']} split rows   FAIL")
            ok = False
    print(f"\n{'ALL CHECKS PASS' if ok else 'FAILURES ABOVE'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
