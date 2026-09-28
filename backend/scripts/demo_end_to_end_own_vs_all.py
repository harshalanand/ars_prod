#!/usr/bin/env python
"""
End-to-end: the SAME data run as Own and as All RDCs, explained step by step.

Runs the real /listing/generate pipeline twice and captures the state of every
table it produces, so each Part can be compared side by side and one concrete
option traced all the way from warehouse stock to picking instruction.

    python scripts/demo_end_to_end_own_vs_all.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pickle                                     # noqa: E402
import pandas as pd                                # noqa: E402
from loguru import logger                          # noqa: E402
from sqlalchemy import text                        # noqa: E402
from app.database.session import get_data_engine   # noqa: E402

MAJ_CATS = ["JB_TEES_HS", "LS_NAIL_PAINT", "JG_SLIPS", "LS_M_CAP"]
# Empty store list = every LISTING-enabled store (472 on ARSDBPRO), which is
# what the cockpit sends when nothing is picked in Select Store.
STORES: list = []
MC_IN = ",".join(f"'{m}'" for m in MAJ_CATS)
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_e2e_cache")


def hdr(n, title):
    print("\n" + "=" * 78)
    print(f"STEP {n} — {title}")
    print("=" * 78)


def run(mode: str) -> dict:
    from app.api.v1.endpoints.listing import GenerateRequest, _generate_listing_impl
    from app.services.listing_sessions import start_session, end_session
    sid = f"E2E_{mode.upper()}_{datetime.now().strftime('%H%M%S')}"
    kw = dict(rdc_mode=mode, store_codes=list(STORES), maj_cat_values=MAJ_CATS,
              run_mode="listing", alloc_type="FRESH",
              stock_consider_dt=str(date.today()), picking_dt=str(date.today()),
              allocation_mode="per_opt", parallel_workers=4)
    if mode == "own":
        kw["rdc_values"] = ["DW01", "DH24"]
    req = GenerateRequest(**kw)
    start_session(sid, "e2e_demo", req.model_dump())
    s: dict = {}
    try:
        _generate_listing_impl(req, current_user=None, session_id=sid,
                               summary=s, preset_batch_id=sid)
    finally:
        end_session(sid, "SUCCESS", s)

    with get_data_engine().connect() as c:
        listing = pd.read_sql(text(f"""
            SELECT WERKS, RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, OPT_TYPE,
                   CAST(IS_NEW AS INT)          AS IS_NEW,
                   TRY_CAST(MSA_FNL_Q AS FLOAT) AS MSA_FNL_Q
            FROM ARS_LISTING WHERE MAJ_CAT IN ({MC_IN})"""), c)
        working = pd.read_sql(text(f"""
            SELECT WERKS, RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, OPT_TYPE,
                   TRY_CAST(MSA_FNL_Q AS FLOAT) AS MSA_FNL_Q,
                   TRY_CAST(OPT_REQ_WH AS FLOAT) AS OPT_REQ_WH
            FROM ARS_LISTING_WORKING WHERE MAJ_CAT IN ({MC_IN})"""), c)
        alloc = pd.read_sql(text(f"""
            SELECT WERKS, RDC, MAJ_CAT, GEN_ART_NUMBER, CLR,
                   TRY_CAST(VAR_ART AS BIGINT) AS VAR_ART, SZ, OPT_TYPE,
                   TRY_CAST(FNL_Q AS FLOAT)    AS FNL_Q,
                   TRY_CAST(SZ_REQ AS FLOAT)   AS SZ_REQ,
                   TRY_CAST(SHIP_QTY AS FLOAT) AS SHIP_QTY,
                   ALLOC_STATUS, SKIP_REASON, SRC_RDC
            FROM ARS_ALLOC_WORKING WHERE MAJ_CAT IN ({MC_IN})"""), c)
        split = pd.read_sql(text(
            "SELECT * FROM ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID=:s"),
            c, params={"s": sid})
    snap = {"sid": sid, "mode": mode, "listing": listing,
            "working": working, "alloc": alloc, "split": split}
    os.makedirs(CACHE, exist_ok=True)
    with open(os.path.join(CACHE, f"{mode}.pkl"), "wb") as f:
        pickle.dump(snap, f)
    print(f"  {mode}: listing={len(listing)} alloc={len(alloc)} "
          f"ship={alloc.SHIP_QTY.sum():.0f} split={len(split)}  -> cached")
    return snap


def load(mode: str) -> dict:
    with open(os.path.join(CACHE, f"{mode}.pkl"), "rb") as f:
        return pickle.load(f)


def main() -> int:
    logger.remove()
    mode_arg = sys.argv[1] if len(sys.argv) > 1 else "--both"
    if mode_arg in ("own", "all", "--run"):
        m = sys.argv[2] if mode_arg == "--run" else mode_arg
        print(f"running {m} over ALL stores x {len(MAJ_CATS)} MAJ_CATs ...")
        run(m)
        return 0

    print("END-TO-END: the same data, run both ways")
    print(f"  scope   : {len(MAJ_CATS)} MAJ_CATs x "
          f"{'ALL active stores' if not STORES else str(len(STORES)) + ' stores'}")
    print(f"  MAJ_CATs: {MAJ_CATS}")

    # ---- warehouse stock, the common starting point --------------------
    hdr(0, "Warehouse stock — identical input for both runs")
    with get_data_engine().connect() as c:
        stock = pd.read_sql(text(f"""
            SELECT LTRIM(RTRIM(CAST(RDC AS NVARCHAR(50)))) AS RDC, MAJ_CAT,
                   SUM(TRY_CAST(FNL_Q AS FLOAT)) AS FNL_Q
            FROM ARS_MSA_VAR_ART
            WHERE MAJ_CAT IN ({MC_IN}) AND ISNULL(ALLOC_TYPE,'FRESH')='FRESH'
            GROUP BY LTRIM(RTRIM(CAST(RDC AS NVARCHAR(50)))), MAJ_CAT"""), c)
    piv = stock.pivot_table(index="MAJ_CAT", columns="RDC",
                            values="FNL_Q", aggfunc="sum").fillna(0)
    piv["TOTAL"] = piv.sum(axis=1)
    print(piv.astype(int).to_string())
    print(f"\n  Neither run changes this. Own lets a store see only its OWN")
    print(f"  warehouse's column; All RDCs lets it see the TOTAL.")

    print("\nrunning both modes ...")
    own = run("own")
    allr = run("all")
    o, a = own, allr

    # ---- Step 1 ---------------------------------------------------------
    hdr(1, "Listing build (Parts 1-2) — which store x option rows exist")
    print(f"  {'':<28}{'OWN':>12}{'ALL RDCs':>12}")
    print(f"  {'ARS_LISTING rows':<28}{len(o['listing']):>12}{len(a['listing']):>12}")
    print(f"  {'  of which MSA-only':<28}{int(o['listing'].IS_NEW.sum()):>12}"
          f"{int(a['listing'].IS_NEW.sum()):>12}")
    print(f"""
  Part 2 adds a row for every MSA option a store does not already stock.
  Under Own that is filtered to the store's OWN warehouse, so a store never
  sees an option held only at the other one. Under All RDCs the filter is
  dropped — which is why the row count grows.""")

    # ---- Step 2 ---------------------------------------------------------
    hdr(2, "Part 3.55 — MSA_FNL_Q: the number that actually changes")
    for lbl, d in (("OWN", o), ("ALL", a)):
        l = d["listing"]
        print(f"  {lbl:<4} rows with MSA_FNL_Q > 0 : {int((l.MSA_FNL_Q > 0).sum()):>8}"
              f"   sum {l.MSA_FNL_Q.sum():>12,.0f}")
    print(f"""
  Own  : MSA_FNL_Q = that store's own warehouse quantity.
  All  : MSA_FNL_Q = SUM across warehouses for the option.
  This single column feeds OPT_TYPE classification and the NO_STOCK gate, so
  clubbing it is what lets a starved store qualify at all.""")

    # ---- Step 3 ---------------------------------------------------------
    hdr(3, "Part 3.6 — OPT_TYPE classification (same rules, bigger input)")
    print(f"  {'OPT_TYPE':<10}{'OWN':>10}{'ALL':>10}")
    for t in sorted(set(o["listing"].OPT_TYPE.dropna())
                    | set(a["listing"].OPT_TYPE.dropna())):
        print(f"  {t:<10}{int((o['listing'].OPT_TYPE == t).sum()):>10}"
              f"{int((a['listing'].OPT_TYPE == t).sum()):>10}")
    print(f"""
  The CASE is untouched. An option that reads 'no stock' under Own can read
  TBL/TBC under All RDCs purely because MSA_FNL_Q is now the clubbed figure.""")

    # ---- Step 4 ---------------------------------------------------------
    hdr(4, "Part 6.6 — eligibility -> ARS_LISTING_WORKING")
    print(f"  {'':<28}{'OWN':>12}{'ALL RDCs':>12}")
    print(f"  {'eligible rows':<28}{len(o['working']):>12}{len(a['working']):>12}")
    print(f"""
  Every gate is unchanged (NOT_LISTED / NO_STOCK / NO_DEMAND / NO_DISPLAY /
  TBL_SIZE). More rows pass only because NO_STOCK now sees the clubbed pool.""")

    # ---- Step 5 ---------------------------------------------------------
    hdr(5, "Part 8 — allocation (the waterfall, byte-identical logic)")
    print(f"  {'':<28}{'OWN':>12}{'ALL RDCs':>12}")
    print(f"  {'alloc rows':<28}{len(o['alloc']):>12}{len(a['alloc']):>12}")
    print(f"  {'SHIP_QTY':<28}{o['alloc'].SHIP_QTY.sum():>12,.0f}"
          f"{a['alloc'].SHIP_QTY.sum():>12,.0f}")
    print(f"  {'rows that shipped':<28}"
          f"{int((o['alloc'].SHIP_QTY > 0).sum()):>12}"
          f"{int((a['alloc'].SHIP_QTY > 0).sum()):>12}")
    print("\n  top skip reasons:")
    so = Counter(o["alloc"].SKIP_REASON.fillna("(allocated)"))
    sa = Counter(a["alloc"].SKIP_REASON.fillna("(allocated)"))
    for k in sorted(set(list(dict(so.most_common(5))) + list(dict(sa.most_common(5))))):
        print(f"    {str(k)[:46]:<48}{so.get(k,0):>7}{sa.get(k,0):>8}")
    print(f"""
  Same gates, same caps, same pack rounding. The pool the waterfall draws from
  is the only difference: per-warehouse under Own, clubbed under All RDCs.""")

    # ---- Step 6 ---------------------------------------------------------
    hdr(6, "Part 8.37 — the split pass (All RDCs only)")
    print(f"  OWN : {len(o['split'])} split rows  "
          f"(SRC_RDC written on {int(o['alloc'].SRC_RDC.notna().sum())} rows)")
    print(f"  ALL : {len(a['split'])} split rows  "
          f"(SRC_RDC written on {int(a['alloc'].SRC_RDC.notna().sum())} rows)")
    sp = a["split"]
    if len(sp):
        print(f"\n  pick by warehouse : "
              f"{sp.groupby('SRC_RDC').SHIP_QTY.sum().round(0).to_dict()}")
        print(f"  cross-shipped     : {sp[sp.IS_CROSS == 1].SHIP_QTY.sum():.0f} pcs"
              f" to {sp[sp.IS_CROSS == 1].WERKS.nunique()} stores")
        print(f"  conservation      : split {sp.SHIP_QTY.sum():.0f}"
              f" vs alloc {a['alloc'].SHIP_QTY.sum():.0f}  "
              f"{'OK' if abs(sp.SHIP_QTY.sum()-a['alloc'].SHIP_QTY.sum())<0.5 else 'MISMATCH'}")
    print(f"""
  Own produces NOTHING here — no split rows, no SRC_RDC. That is the mode
  separation guarantee: an Own run cannot even enter this code.""")

    # ---- Step 7 ---------------------------------------------------------
    hdr(7, "Per-store outcome")
    po = o["alloc"][o["alloc"].SHIP_QTY > 0].groupby("WERKS").SHIP_QTY.sum()
    pa = a["alloc"][a["alloc"].SHIP_QTY > 0].groupby("WERKS").SHIP_QTY.sum()
    print(f"  {'STORE':<8}{'OWN':>8}{'ALL':>8}{'DELTA':>8}")
    for w in sorted(set(po.index) | set(pa.index)):
        x, y = po.get(w, 0), pa.get(w, 0)
        print(f"  {w:<8}{x:>8.0f}{y:>8.0f}{y-x:>+8.0f}")
    print(f"  {'TOTAL':<8}{po.sum():>8.0f}{pa.sum():>8.0f}{pa.sum()-po.sum():>+8.0f}")

    # ---- Step 8: trace one option ---------------------------------------
    hdr(8, "Trace ONE cross-shipped line end to end")
    if len(sp) and (sp.IS_CROSS == 1).any():
        r = sp[sp.IS_CROSS == 1].sort_values("SHIP_QTY", ascending=False).iloc[0]
        w, va, sz = r.WERKS, r.VAR_ART, r.SZ
        print(f"  store {w} (belongs to {r.STORE_RDC})  article {va}  size {sz}\n")
        with get_data_engine().connect() as c:
            st = pd.read_sql(text("""
                SELECT LTRIM(RTRIM(CAST(RDC AS NVARCHAR(50)))) AS RDC,
                       SUM(TRY_CAST(FNL_Q AS FLOAT)) AS STOCK
                FROM ARS_MSA_VAR_ART
                WHERE TRY_CAST(TRY_CAST(ARTICLE_NUMBER AS FLOAT) AS BIGINT)=:v
                  AND LTRIM(RTRIM(CAST(SZ AS NVARCHAR(50))))=:z
                  AND ISNULL(ALLOC_TYPE,'FRESH')='FRESH'
                GROUP BY LTRIM(RTRIM(CAST(RDC AS NVARCHAR(50))))"""),
                c, params={"v": int(va), "z": sz})
        print("  a) warehouse stock for this article-size:")
        for _, x in st.iterrows():
            print(f"       {x.RDC:<7} {x.STOCK:>8.0f}")
        oa = o["alloc"][(o["alloc"].WERKS == w) & (o["alloc"].VAR_ART == va)
                        & (o["alloc"].SZ == sz)]
        aa = a["alloc"][(a["alloc"].WERKS == w) & (a["alloc"].VAR_ART == va)
                        & (a["alloc"].SZ == sz)]
        def fmt(df):
            if not len(df):
                return "row not present"
            q = df.iloc[0]
            sk = f"  skip={str(q.SKIP_REASON)[:30]}" if q.SKIP_REASON else ""
            return (f"FNL_Q={q.FNL_Q:.0f} SZ_REQ={q.SZ_REQ:.0f} "
                    f"SHIP={q.SHIP_QTY:.0f} status={q.ALLOC_STATUS}{sk}")
        print(f"\n  b) OWN run : {fmt(oa)}")
        print(f"  c) ALL run : {fmt(aa)}")
        print(f"\n  d) split pass decided: ships from {r.SRC_RDC}, qty "
              f"{r.SHIP_QTY:.0f}   (store belongs to {r.STORE_RDC} -> CROSS)")
        print(f"\n  e) the warehouse picking list therefore shows this line under"
              f" {r.SRC_RDC},")
        print(f"     and ARS_PEND_ALC books the pending against {r.SRC_RDC} too —")
        print(f"     so next run's MSA deducts it from the warehouse that shipped.")
    else:
        print("  no cross-shipped line in this run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
