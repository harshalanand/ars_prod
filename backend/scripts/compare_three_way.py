#!/usr/bin/env python
"""
Three-way comparison of replayed sessions, read from ARS_ALLOC_PARKED.

    python scripts/compare_three_way.py --base <sid> --own <sid> --all <sid>

  BASE  the original run, produced by the reference checkout (\\192.168.150.100\\ars_prod)
  OWN   same parameters, this branch, RDC Scope = Own      -> must be IDENTICAL to BASE
  ALL   same parameters, this branch, RDC Scope = All RDCs -> the business delta

Part A is the regression gate: if BASE != OWN, the central-pool work has
leaked into Own and nothing else in the report matters.
Part B is what the business would actually see from clubbing.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd                                        # noqa: E402
from sqlalchemy import text                                # noqa: E402
from app.database.session import get_data_engine           # noqa: E402

KEY = ["WERKS", "VAR_ART", "SZ"]
OUTCOME = ["SHIP_QTY", "HOLD_QTY", "ALLOC_QTY", "ALLOC_STATUS",
           "SKIP_REASON", "ALLOC_ROUND", "ALLOC_WAVE"]
STAMPS = ("PAK_SZ_ROUND", "PAK_SZ_GATE", "MBQ_CAP_OVERSHOOT", "MBQ_CAP_SCALE",
          "SEC_CAP", "PRIMARY_CAP", "POOL_EMPTY", "HOLD_RELEASED",
          "RDC_SINGLE_SHORT", "RDC_SPLIT_CAPPED", "RDC_HOLD_SHORT")


def load(sid: str) -> pd.DataFrame:
    with get_data_engine().connect() as c:
        df = pd.read_sql(text("""
            SELECT WERKS, RDC, MAJ_CAT, GEN_ART_NUMBER, CLR,
                   LTRIM(RTRIM(CAST(VAR_ART AS NVARCHAR(50)))) AS VAR_ART,
                   LTRIM(RTRIM(ISNULL(SZ,'')))                 AS SZ,
                   OPT_TYPE,
                   TRY_CAST(SHIP_QTY  AS FLOAT) AS SHIP_QTY,
                   TRY_CAST(HOLD_QTY  AS FLOAT) AS HOLD_QTY,
                   TRY_CAST(ALLOC_QTY AS FLOAT) AS ALLOC_QTY,
                   TRY_CAST(FNL_Q     AS FLOAT) AS FNL_Q,
                   TRY_CAST(SZ_REQ    AS FLOAT) AS SZ_REQ,
                   TRY_CAST(ALLOC_ROUND AS INT) AS ALLOC_ROUND,
                   ISNULL(ALLOC_WAVE,'')   AS ALLOC_WAVE,
                   ISNULL(ALLOC_STATUS,'') AS ALLOC_STATUS,
                   ISNULL(SKIP_REASON,'')  AS SKIP_REASON,
                   SRC_RDC, TRY_CAST(SRC_SPLIT_CNT AS INT) AS SRC_SPLIT_CNT,
                   ISNULL(ALLOC_REMARKS,'') AS REMARKS
              FROM ARS_ALLOC_PARKED WHERE SESSION_ID = :s"""), c, params={"s": sid})
    for c_ in ("SHIP_QTY", "HOLD_QTY", "ALLOC_QTY"):
        df[c_] = df[c_].fillna(0.0)
    return df


def load_split(sid: str) -> pd.DataFrame:
    with get_data_engine().connect() as c:
        return pd.read_sql(text(
            "SELECT * FROM ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID=:s"),
            c, params={"s": sid})


def stamps(df) -> Counter:
    out = Counter()
    for r in df.REMARKS.fillna(""):
        for t in STAMPS:
            if t in r:
                out[t] += 1
    return out


def hdr(n, t):
    print("\n" + "=" * 78)
    print(f"{n}. {t}")
    print("=" * 78)


# ── Part A — regression gate ────────────────────────────────────────────────
def part_a(base, own) -> bool:
    hdr("A", "REGRESSION GATE — Own under this branch vs the reference run")
    ok = True
    print(f"  row count      BASE {len(base):>8}   OWN {len(own):>8}   "
          f"{'PASS' if len(base) == len(own) else 'FAIL'}")
    ok &= len(base) == len(own)

    a = base.set_index(KEY)[OUTCOME].sort_index()
    b = own.set_index(KEY)[OUTCOME].sort_index()
    if a.index.duplicated().any() or b.index.duplicated().any():
        print(f"  ! duplicate keys — BASE {int(a.index.duplicated().sum())}, "
              f"OWN {int(b.index.duplicated().sum())}; aggregating to compare")
        a = base.groupby(KEY)[["SHIP_QTY", "HOLD_QTY", "ALLOC_QTY"]].sum().sort_index()
        b = own.groupby(KEY)[["SHIP_QTY", "HOLD_QTY", "ALLOC_QTY"]].sum().sort_index()

    if not a.index.equals(b.index):
        only_a = a.index.difference(b.index)
        only_b = b.index.difference(a.index)
        print(f"  key sets DIFFER — BASE-only {len(only_a)}, OWN-only {len(only_b)}   FAIL")
        for k in list(only_a)[:5]:
            print(f"      BASE only: {k}")
        for k in list(only_b)[:5]:
            print(f"      OWN  only: {k}")
        ok = False
        common = a.index.intersection(b.index)
        a, b = a.loc[common], b.loc[common]
    else:
        print(f"  key sets       IDENTICAL ({len(a)} keys)   PASS")

    diff = a.fillna("~") != b.fillna("~")
    nrows = int(diff.any(axis=1).sum())
    if nrows:
        print(f"  outcome        {nrows} of {len(a)} rows DIFFER   FAIL")
        for col in a.columns:
            d = int(diff[col].sum())
            if d:
                print(f"      {col:<14} {d} rows")
        sample = a[diff.any(axis=1)].join(b[diff.any(axis=1)], lsuffix="_BASE",
                                          rsuffix="_OWN").head(8)
        print(sample.to_string())
        ok = False
    else:
        print(f"  outcome        all {len(a)} rows IDENTICAL   PASS")

    print(f"  SHIP total     BASE {base.SHIP_QTY.sum():>10.0f}   "
          f"OWN {own.SHIP_QTY.sum():>10.0f}")
    print(f"  HOLD total     BASE {base.HOLD_QTY.sum():>10.0f}   "
          f"OWN {own.HOLD_QTY.sum():>10.0f}")

    n_src = int(own.SRC_RDC.notna().sum())
    print(f"  Own wrote SRC_RDC on {n_src} rows   "
          f"{'PASS' if n_src == 0 else 'FAIL'}   (V14 / I-11)")
    ok &= n_src == 0
    print(f"\n  VERDICT: {'PASS — Own is byte-identical' if ok else 'FAIL — Own changed'}")
    return ok


# ── Part B — business delta ─────────────────────────────────────────────────
def part_b(own, alloc_all, split):
    hdr("B", "BUSINESS DELTA — Own vs All RDCs (same parameters, same data)")
    o, a = own, alloc_all
    os_, as_ = o[o.SHIP_QTY > 0], a[a.SHIP_QTY > 0]

    def row(label, x, y, fmt="{:>10.0f}"):
        d = y - x
        pct = (d / x * 100) if x else float("nan")
        print(f"  {label:<22}" + fmt.format(x) + "  ->" + fmt.format(y)
              + f"   {d:+10.0f}   {pct:+7.1f}%")

    row("SHIP qty", o.SHIP_QTY.sum(), a.SHIP_QTY.sum())
    row("HOLD qty", o.HOLD_QTY.sum(), a.HOLD_QTY.sum())
    row("ALLOC qty", o.ALLOC_QTY.sum(), a.ALLOC_QTY.sum())
    row("rows with SHIP>0", len(os_), len(as_))
    row("stores fed", os_.WERKS.nunique(), as_.WERKS.nunique())
    row("options fed", os_.groupby(["GEN_ART_NUMBER", "CLR"]).ngroups,
        as_.groupby(["GEN_ART_NUMBER", "CLR"]).ngroups)

    print("\n  -- OPT_TYPE shipped --")
    co, ca = Counter(os_.OPT_TYPE.fillna("?")), Counter(as_.OPT_TYPE.fillna("?"))
    for k in sorted(set(co) | set(ca)):
        qo = os_[os_.OPT_TYPE == k].SHIP_QTY.sum()
        qa = as_[as_.OPT_TYPE == k].SHIP_QTY.sum()
        print(f"    {k:<6} rows {co.get(k,0):>7} -> {ca.get(k,0):<7}"
              f"  qty {qo:>8.0f} -> {qa:<8.0f} ({qa-qo:+.0f})")

    print("\n  -- skip reasons (top 12 by change) --")
    so = Counter(o.SKIP_REASON.replace("", "(allocated)"))
    sa = Counter(a.SKIP_REASON.replace("", "(allocated)"))
    keys = sorted(set(so) | set(sa), key=lambda k: -abs(sa.get(k, 0) - so.get(k, 0)))
    for k in keys[:12]:
        print(f"    {k[:44]:<44} {so.get(k,0):>8} -> {sa.get(k,0):<8} "
              f"({sa.get(k,0)-so.get(k,0):+d})")

    print("\n  -- rule stamps --")
    to, ta = stamps(o), stamps(a)
    for k in sorted(set(to) | set(ta)):
        print(f"    {k:<20} {to.get(k,0):>7} -> {ta.get(k,0):<7} "
              f"({ta.get(k,0)-to.get(k,0):+d})")

    print("\n  -- per MAJ_CAT ship --")
    m = (pd.concat([os_.groupby("MAJ_CAT").SHIP_QTY.sum().rename("OWN"),
                    as_.groupby("MAJ_CAT").SHIP_QTY.sum().rename("ALL")], axis=1)
         .fillna(0))
    m["DELTA"] = m.ALL - m.OWN
    m = m.sort_values("DELTA", ascending=False)
    print(m.to_string(float_format=lambda x: f"{x:,.0f}"))

    print("\n  -- per store: winners / losers --")
    st = (pd.concat([os_.groupby("WERKS").SHIP_QTY.sum().rename("OWN"),
                     as_.groupby("WERKS").SHIP_QTY.sum().rename("ALL")], axis=1)
          .fillna(0))
    st["DELTA"] = st.ALL - st.OWN
    up, dn, fl = st[st.DELTA > 0], st[st.DELTA < 0], st[st.DELTA == 0]
    print(f"    gained {len(up):>4} stores  {up.DELTA.sum():+,.0f} pcs")
    print(f"    lost   {len(dn):>4} stores  {dn.DELTA.sum():+,.0f} pcs")
    print(f"    flat   {len(fl):>4} stores")
    if len(up):
        print("    top gainers:", up.DELTA.nlargest(8).round(0).to_dict())
    if len(dn):
        print("    top losers :", dn.DELTA.nsmallest(8).round(0).to_dict())

    # ── split pass ──
    hdr("C", "SPLIT PASS — which warehouse physically ships (All RDCs only)")
    if not len(split):
        print("  no split rows")
        return
    sp = split.copy()
    for c_ in ("SHIP_QTY", "HOLD_QTY"):
        sp[c_] = pd.to_numeric(sp[c_], errors="coerce").fillna(0)
    print(f"  split rows          {len(sp)}")
    print(f"  pick by warehouse   "
          f"{sp.groupby('SRC_RDC').SHIP_QTY.sum().round(0).to_dict()}")
    cross = sp[sp.IS_CROSS == 1]
    print(f"  cross-shipped       {cross.SHIP_QTY.sum():,.0f} pcs "
          f"({cross.SHIP_QTY.sum()/max(sp.SHIP_QTY.sum(),1)*100:.1f}%) "
          f"to {cross.WERKS.nunique()} stores")
    multi = sp.groupby(["WERKS", "VAR_ART", "SZ"]).size()
    print(f"  lines split >1 src  {int((multi > 1).sum())} of {len(multi)}")
    print(f"  PREF_TIER mix       {dict(Counter(sp.PREF_TIER))}")
    d = sp.SHIP_QTY.sum() - a.SHIP_QTY.sum()
    print(f"  conservation        split {sp.SHIP_QTY.sum():,.0f} vs alloc "
          f"{a.SHIP_QTY.sum():,.0f}  delta {d:+.0f}   "
          f"{'PASS' if abs(d) < 0.5 else 'FAIL'}")
    hd = sp.HOLD_QTY.sum() - a.HOLD_QTY.sum()
    print(f"  hold conservation   split {sp.HOLD_QTY.sum():,.0f} vs alloc "
          f"{a.HOLD_QTY.sum():,.0f}  delta {hd:+.0f}")
    red = [t for t in stamps(a) if t.startswith("RDC_")]
    print(f"  reduction stamps    { {t: stamps(a)[t] for t in red} or 'none'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--own", required=True)
    ap.add_argument("--all", dest="alls", required=True)
    args = ap.parse_args()

    print(f"BASE = {args.base}   (reference checkout, Own)")
    print(f"OWN  = {args.own}    (this branch, Own)")
    print(f"ALL  = {args.alls}   (this branch, All RDCs)")

    base, own, alls = load(args.base), load(args.own), load(args.alls)
    split = load_split(args.alls)
    own_split = load_split(args.own)
    print(f"\nloaded  base={len(base)}  own={len(own)}  all={len(alls)}  "
          f"split(all)={len(split)}  split(own)={len(own_split)}")
    if len(own_split):
        print(f"  !! OWN produced {len(own_split)} split rows — FAIL (V14)")

    part_a(base, own)
    part_b(own, alls, split)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
