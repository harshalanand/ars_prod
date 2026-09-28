#!/usr/bin/env python
"""
Full validation — allocation rules + Own vs All RDCs (spec v1.5, Step 6).

RDC Scope is the only control: `Own` keeps per-RDC pools, `All RDCs` clubs the
warehouses. Two runs over the SAME scope:

    OWN   rdc_mode='own'   per-RDC pools, no split pass
    ALL   rdc_mode='all'   clubbed pool + Part 8.37 split

  OWN   must write zero SRC_RDC and zero split rows          (V14 / I-11)
  ALL   conservation, no warehouse over-drawn, no reductions (V3 / V4 / V10)
  delta the business difference ops would actually see

Then checks the ALLOCATION RULES still fire the same way (BR-RDC-02: clubbing
changes only WHICH POOL a row draws from, never a store's entitlement) by
comparing, per run: OPT_TYPE mix, every skip reason, cap stamps, pak-rounding
stamps, I_ROD rounds, hold behaviour and per-store distribution.

    python scripts/step6_full_validation.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd                                # noqa: E402
from loguru import logger                          # noqa: E402
from sqlalchemy import text                        # noqa: E402
from app.database.session import get_data_engine   # noqa: E402

# JB_TEES_HS carries real demand (619 pcs shipped on a 4-store probe), so the
# business delta is measurable rather than noise. LS_NAIL_PAINT and JG_SLIPS
# are heavily DW01-weighted, LS_M_CAP heavily DH24 — the asymmetry clubbing
# is meant to exploit.
MAJ_CATS = ["JB_TEES_HS", "LS_NAIL_PAINT", "JG_SLIPS", "LS_M_CAP"]
# 6 stores per warehouse. NOTE the RDC assignment is MIRRORED between the two
# servers: on HOPC866 the first six are DW01, on ARSDBPRO they are DH24. The
# test does not depend on which is which — `own` mode passes both RDCs — but
# do not read the grouping below as a fixed warehouse label.
STORES = ["HH15", "HX55", "HL07", "HD22", "HK04", "HU36",
          "HN40", "HN50", "HJ08", "HB35", "HC13", "HW52"]
KEY = ["WERKS", "VAR_ART", "SZ"]


def run(tag: str, mode: str) -> dict:
    from app.api.v1.endpoints.listing import GenerateRequest, _generate_listing_impl
    from app.services.listing_sessions import start_session, end_session
    sid = f"FV_{tag}_{datetime.now().strftime('%H%M%S')}"
    kw = dict(rdc_mode=mode, store_codes=STORES, maj_cat_values=MAJ_CATS,
              run_mode="listing", alloc_type="FRESH",
              stock_consider_dt=str(date.today()), picking_dt=str(date.today()),
              allocation_mode="per_opt", parallel_workers=4)
    if mode == "own":
        kw["rdc_values"] = ["DW01", "DH24"]
    req = GenerateRequest(**kw)
    start_session(sid, "full_validation", req.model_dump())
    s: dict = {}
    try:
        _generate_listing_impl(req, current_user=None, session_id=sid,
                               summary=s, preset_batch_id=sid)
    finally:
        end_session(sid, "SUCCESS", s)

    with get_data_engine().connect() as c:
        alloc = pd.read_sql(text("""
            SELECT WERKS, RDC, MAJ_CAT, GEN_ART_NUMBER, CLR,
                   TRY_CAST(VAR_ART AS BIGINT) AS VAR_ART, SZ, OPT_TYPE,
                   TRY_CAST(SHIP_QTY AS FLOAT)  AS SHIP_QTY,
                   TRY_CAST(HOLD_QTY AS FLOAT)  AS HOLD_QTY,
                   TRY_CAST(ALLOC_QTY AS FLOAT) AS ALLOC_QTY,
                   TRY_CAST(FNL_Q AS FLOAT)     AS FNL_Q,
                   TRY_CAST(SZ_REQ AS FLOAT)    AS SZ_REQ,
                   TRY_CAST(ALLOC_ROUND AS INT) AS ALLOC_ROUND,
                   ALLOC_WAVE, ALLOC_STATUS, SKIP_REASON, SRC_RDC,
                   TRY_CAST(SRC_SPLIT_CNT AS INT) AS SRC_SPLIT_CNT,
                   ISNULL(ALLOC_REMARKS,'')     AS REMARKS
            FROM ARS_ALLOC_WORKING"""), c)
        listing = pd.read_sql(text("""
            SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR, OPT_TYPE, OPT_STATUS,
                   CAST(IS_NEW AS INT)           AS IS_NEW,
                   TRY_CAST(MSA_FNL_Q AS FLOAT)  AS MSA_FNL_Q,
                   TRY_CAST(VAR_COUNT AS FLOAT)  AS VAR_COUNT,
                   TRY_CAST(VAR_FNL_COUNT AS FLOAT) AS VAR_FNL_COUNT
            FROM ARS_LISTING_WORKING"""), c)
        split = pd.read_sql(text(
            "SELECT * FROM ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID=:s"),
            c, params={"s": sid})
    print(f"  {tag:<8} alloc={len(alloc):<6} ship={alloc.SHIP_QTY.sum():<8.0f} "
          f"hold={alloc.HOLD_QTY.sum():<6.0f} listing={len(listing):<6} "
          f"split={len(split)}")
    return {"sid": sid, "alloc": alloc, "listing": listing, "split": split}


def remark_tags(df) -> Counter:
    """Count the rule stamps the engine left, so we can see WHICH rules fired."""
    tags = Counter()
    for r in df.REMARKS.fillna(""):
        for t in ("PAK_SZ_ROUND", "PAK_SZ_GATE", "MBQ_CAP_OVERSHOOT",
                  "MBQ_CAP_SCALE", "SEC_CAP", "PRIMARY_CAP", "POOL_EMPTY",
                  "HOLD_RELEASED", "RDC_SINGLE_SHORT", "RDC_SPLIT_CAPPED",
                  "RDC_HOLD_SHORT"):
            if t in r:
                tags[t] += 1
    return tags


def cmp_identical(name, x, y) -> bool:
    """Row-for-row equality on the allocation outcome."""
    cols = ["SHIP_QTY", "HOLD_QTY", "ALLOC_QTY", "ALLOC_STATUS",
            "SKIP_REASON", "ALLOC_ROUND", "ALLOC_WAVE"]
    a = x["alloc"].set_index(KEY)[cols].sort_index()
    b = y["alloc"].set_index(KEY)[cols].sort_index()
    if len(a) != len(b):
        print(f"  {name}: row COUNT differs — {len(a)} vs {len(b)}   FAIL")
        return False
    if not a.index.equals(b.index):
        print(f"  {name}: row KEYS differ   FAIL")
        return False
    diff = (a.fillna("~") != b.fillna("~"))
    n = int(diff.any(axis=1).sum())
    if n:
        print(f"  {name}: {n} of {len(a)} rows differ   FAIL")
        for col in cols:
            d = int(diff[col].sum())
            if d:
                print(f"      {col}: {d} rows")
        return False
    print(f"  {name}: {len(a)} rows IDENTICAL   PASS")
    return True


def profile(tag, r) -> None:
    a, l = r["alloc"], r["listing"]
    shipped = a[a.SHIP_QTY > 0]
    print(f"\n  --- {tag} ---")
    print(f"    listing rows {len(l):<6} (MSA-only {int(l.IS_NEW.sum())})   "
          f"alloc rows {len(a):<6}   shipped rows {len(shipped)}")
    print(f"    OPT_TYPE listed : {dict(Counter(l.OPT_TYPE.fillna('?')))}")
    print(f"    OPT_TYPE shipped: {dict(Counter(shipped.OPT_TYPE.fillna('?')))}")
    print(f"    SHIP {a.SHIP_QTY.sum():.0f}  HOLD {a.HOLD_QTY.sum():.0f}  "
          f"rounds {sorted(set(a.ALLOC_ROUND.dropna().astype(int)))}")
    sk = Counter(a.SKIP_REASON.fillna("(allocated)"))
    print(f"    skip reasons    : {dict(sk.most_common(6))}")
    print(f"    rule stamps     : {dict(remark_tags(a))}")
    per_store = shipped.groupby("WERKS").SHIP_QTY.sum().to_dict()
    print(f"    ship by store   : { {k: int(v) for k, v in sorted(per_store.items())} }")


def main() -> int:
    logger.remove()
    print(f"scope: {len(MAJ_CATS)} MAJ_CATs x {len(STORES)} stores "
          f"(4 DW01 + 4 DH24)\n  {MAJ_CATS}\n")
    runs = {}
    print("running both RDC Scope modes ...")
    runs["OWN"] = run("OWN", "own")
    runs["ALL"] = run("ALL", "all")

    a1, b2 = runs["OWN"], runs["ALL"]
    print("\n" + "=" * 74)
    print("1. Mode separation — Own must not touch any of the new machinery")
    print("=" * 74)
    n_src = int(a1["alloc"].SRC_RDC.notna().sum())
    ok = (n_src == 0) and len(a1["split"]) == 0
    print(f"  Own wrote SRC_RDC on {n_src} rows and {len(a1['split'])} split "
          f"rows   {'PASS' if ok else 'FAIL'}   (V14 / I-11)")
    n_all = int(b2["alloc"].SRC_RDC.notna().sum())
    print(f"  All RDCs stamped SRC_RDC on {n_all} rows   "
          f"{'PASS' if n_all > 0 else 'FAIL'}")

    print("\n" + "=" * 74)
    print("2. Rule-by-rule profile of each mode")
    print("=" * 74)
    for t in ("OWN", "ALL"):
        profile(t, runs[t])

    print("\n" + "=" * 74)
    print("3. Business delta   (Own  vs  All RDCs)")
    print("=" * 74)
    print(f"  SHIP        {a1['alloc'].SHIP_QTY.sum():>10.0f}  ->"
          f" {b2['alloc'].SHIP_QTY.sum():>10.0f}")
    print(f"  HOLD        {a1['alloc'].HOLD_QTY.sum():>10.0f}  ->"
          f" {b2['alloc'].HOLD_QTY.sum():>10.0f}")
    print(f"  alloc rows  {len(a1['alloc']):>10}  -> {len(b2['alloc']):>10}")
    print(f"  stores fed  {a1['alloc'][a1['alloc'].SHIP_QTY>0].WERKS.nunique():>10}"
          f"  -> {b2['alloc'][b2['alloc'].SHIP_QTY>0].WERKS.nunique():>10}")
    sp = b2["split"]
    if len(sp):
        print(f"\n  split table: {len(sp)} rows  ship={sp.SHIP_QTY.sum():.0f} "
              f"hold={sp.HOLD_QTY.sum():.0f}")
        print(f"  pick by warehouse: "
              f"{sp.groupby('SRC_RDC').SHIP_QTY.sum().round(0).to_dict()}")
        print(f"  cross-shipped: {sp[sp.IS_CROSS==1].SHIP_QTY.sum():.0f} pcs "
              f"to {sp[sp.IS_CROSS==1].WERKS.nunique()} stores")
        print(f"  conservation: split ship {sp.SHIP_QTY.sum():.0f} vs alloc "
              f"{b2['alloc'].SHIP_QTY.sum():.0f}  "
              f"{'PASS' if abs(sp.SHIP_QTY.sum()-b2['alloc'].SHIP_QTY.sum())<0.5 else 'FAIL'}")
        print(f"  PREF_TIER mix: {dict(Counter(sp.PREF_TIER))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
