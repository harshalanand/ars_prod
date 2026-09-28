#!/usr/bin/env python
"""
Diagnose the Own=66 / All-RDCs=0 divergence found by the Step 6 A/B test.

Method: run the SAME scope twice, snapshotting ARS_ALLOC_WORKING and
ARS_LISTING_WORKING in-process after each run (the tables are rebuilt every
Generate, so they cannot both be inspected afterwards). Then follow the exact
rows that shipped under Own into the All-RDCs run and report where they died.

Distinguishes three hypotheses:
  H1  order effect  — the larger listed set consumes the per-store grid-cap
                      budget before the rows that used to ship reach it
  H2  pool defect   — the clubbed pool lookup fails (the new NO_POOL_MSA rows)
  H3  listing shift — the options' OPT_TYPE / rank / eligibility changed

    python scripts/step6_diagnose_66_to_0.py
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd                                # noqa: E402
from loguru import logger                          # noqa: E402
from sqlalchemy import text                        # noqa: E402
from app.database.session import get_data_engine   # noqa: E402

MAJ_CAT = "JB_TEES_HS"
STORES = ["HH15", "HX55", "HN40", "HN50"]
RULE = "ALC_RDC_CENTRAL_POOL"
KEY = ["WERKS", "VAR_ART", "SZ"]


def switch(on: bool) -> None:
    with get_data_engine().connect() as c:
        c.execute(text("UPDATE ARS_BUSINESS_RULES SET is_active=:v "
                       "WHERE rule_key=:k"), {"v": 1 if on else 0, "k": RULE})
        c.commit()
    from app.services import business_rules as br
    br._load_all(force=True)


def run(label: str, mode: str) -> dict:
    from app.api.v1.endpoints.listing import GenerateRequest, _generate_listing_impl
    from app.services.listing_sessions import start_session, end_session
    sid = f"DX_{label}_{datetime.now().strftime('%H%M%S')}"
    kw = dict(rdc_mode=mode, store_codes=STORES, maj_cat_values=[MAJ_CAT],
              run_mode="listing", alloc_type="FRESH",
              stock_consider_dt=str(date.today()), picking_dt=str(date.today()),
              allocation_mode="per_opt", parallel_workers=2)
    if mode == "own":
        kw["rdc_values"] = ["DW01", "DH24"]
    req = GenerateRequest(**kw)
    start_session(sid, "diagnose", req.model_dump())
    s: dict = {}
    try:
        _generate_listing_impl(req, current_user=None, session_id=sid,
                               summary=s, preset_batch_id=sid)
    finally:
        end_session(sid, "SUCCESS", s)

    eng = get_data_engine()
    with eng.connect() as c:
        alloc = pd.read_sql(text("""
            SELECT WERKS, RDC, GEN_ART_NUMBER, CLR,
                   TRY_CAST(VAR_ART AS BIGINT) AS VAR_ART, SZ,
                   TRY_CAST(FNL_Q AS FLOAT)      AS FNL_Q,
                   TRY_CAST(FNL_Q_REM AS FLOAT)  AS FNL_Q_REM,
                   TRY_CAST(SZ_REQ AS FLOAT)     AS SZ_REQ,
                   TRY_CAST(SHIP_QTY AS FLOAT)   AS SHIP_QTY,
                   ALLOC_STATUS, SKIP_REASON,
                   TRY_CAST(ST_RANK AS FLOAT)            AS ST_RANK,
                   TRY_CAST(OPT_PRIORITY_RANK AS FLOAT)  AS OPT_RANK,
                   OPT_TYPE,
                   LEFT(ISNULL(ALLOC_REMARKS,''), 120)   AS REMARKS
            FROM ARS_ALLOC_WORKING WHERE MAJ_CAT = :mc
        """), c, params={"mc": MAJ_CAT})
        listing = pd.read_sql(text("""
            SELECT WERKS, RDC, GEN_ART_NUMBER, CLR, OPT_TYPE,
                   CAST(IS_NEW AS INT)                  AS IS_NEW,
                   TRY_CAST(MSA_FNL_Q AS FLOAT)         AS MSA_FNL_Q,
                   TRY_CAST(OPT_PRIORITY_RANK AS FLOAT) AS OPT_RANK,
                   TRY_CAST(OPT_REQ_WH AS FLOAT)        AS OPT_REQ_WH
            FROM ARS_LISTING_WORKING WHERE MAJ_CAT = :mc
        """), c, params={"mc": MAJ_CAT})
    print(f"  {label}: alloc={len(alloc)} ship={alloc.SHIP_QTY.sum():.0f} "
          f"listing={len(listing)} (MSA-only {int(listing.IS_NEW.sum())})")
    return {"alloc": alloc, "listing": listing}


def main() -> int:
    logger.remove()
    print(f"scope {MAJ_CAT} / {STORES}\n")
    try:
        print("running OWN ...")
        switch(False)
        own = run("OWN", "own")
        print("running ALL ...")
        switch(True)
        allr = run("ALL", "all")
    finally:
        switch(False)
        print("\nswitch restored to OFF")

    a, b = own["alloc"], allr["alloc"]
    winners = a[a.SHIP_QTY > 0].copy()
    print(f"\n=== the {len(winners)} rows that shipped under OWN ({winners.SHIP_QTY.sum():.0f} pcs) ===")

    m = winners.merge(b, on=KEY, how="left", suffixes=("_own", "_all"))
    print(f"  present in the ALL run too: {m.ALLOC_STATUS_all.notna().sum()} of {len(m)}")
    print()
    print("  what happened to them under ALL:")
    for (st, sk), g in m.groupby([m.ALLOC_STATUS_all.fillna("(row absent)"),
                                  m.SKIP_REASON_all.fillna("-")]):
        print(f"    {str(st)[:12]:<13} {str(sk)[:34]:<35} n={len(g)}  "
              f"own_ship={g.SHIP_QTY_own.sum():.0f}")

    print("\n  row-by-row (own shipped -> all outcome):")
    cols = ["WERKS", "VAR_ART", "SZ", "SHIP_QTY_own", "OPT_RANK_own",
            "OPT_RANK_all", "FNL_Q_own", "FNL_Q_all", "SZ_REQ_own",
            "SZ_REQ_all", "SKIP_REASON_all"]
    for _, r in m[cols].iterrows():
        print(f"    {r.WERKS:<6} {str(r.VAR_ART):<14} {str(r.SZ):<5} "
              f"ship={r.SHIP_QTY_own:>4.0f} rank {r.OPT_RANK_own}->{r.OPT_RANK_all} "
              f"FNL {r.FNL_Q_own}->{r.FNL_Q_all} REQ {r.SZ_REQ_own}->{r.SZ_REQ_all} "
              f"| {str(r.SKIP_REASON_all)[:30]}")

    # H3 — did the listing change for those options?
    print("\n=== H3: listing/rank shift for the winning OPTIONS ===")
    wopt = winners[["WERKS", "GEN_ART_NUMBER", "CLR"]].drop_duplicates()
    lo = own["listing"].merge(wopt, on=["WERKS", "GEN_ART_NUMBER", "CLR"])
    la = allr["listing"].merge(wopt, on=["WERKS", "GEN_ART_NUMBER", "CLR"])
    j = lo.merge(la, on=["WERKS", "GEN_ART_NUMBER", "CLR"],
                 how="left", suffixes=("_own", "_all"))
    for _, r in j.iterrows():
        print(f"    {r.WERKS:<6} {r.GEN_ART_NUMBER}/{str(r.CLR)[:10]:<10} "
              f"type {r.OPT_TYPE_own}->{r.OPT_TYPE_all}  "
              f"MSA_FNL_Q {r.MSA_FNL_Q_own}->{r.MSA_FNL_Q_all}  "
              f"rank {r.OPT_RANK_own}->{r.OPT_RANK_all}  "
              f"REQ_WH {r.OPT_REQ_WH_own}->{r.OPT_REQ_WH_all}")

    # H1 — how many options now compete per store, and how do ranks shift?
    print("\n=== H1: competition per store (listed options) ===")
    co = own["listing"].groupby("WERKS").size()
    ca = allr["listing"].groupby("WERKS").size()
    for w in sorted(set(co.index) | set(ca.index)):
        print(f"    {w:<6} own={co.get(w,0):<5} all={ca.get(w,0):<5} "
              f"(+{ca.get(w,0)-co.get(w,0)})")

    # H2 — the new NO_POOL_MSA rows
    print("\n=== H2: NO_POOL_MSA rows in the ALL run ===")
    np_ = b[b.SKIP_REASON.fillna("").str.contains("NO_POOL_MSA")]
    print(f"    count={len(np_)}  FNL_Q sum={np_.FNL_Q.sum():.0f}  "
          f"FNL_Q_REM sum={np_.FNL_Q_REM.sum():.0f}")
    if len(np_):
        print("    sample:")
        for _, r in np_.head(5).iterrows():
            print(f"      {r.WERKS:<6} {r.RDC:<6} {str(r.VAR_ART):<14} {str(r.SZ):<5} "
                  f"FNL={r.FNL_Q} REM={r.FNL_Q_REM} REQ={r.SZ_REQ} | {str(r.REMARKS)[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
