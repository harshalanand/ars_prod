#!/usr/bin/env python
"""
Pull real worked examples out of a completed All RDCs run.

    python scripts/show_rdc_examples.py --own <sid> --all <sid> [--top 3]

Shows, from live data:
  1. a store that could only be served by cross-shipping (own warehouse empty)
  2. a line that had to be SPLIT across both warehouses
  3. a store that LOST volume under clubbing, with the reason visible
  4. the warehouse ledger for one option, before and after
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd                                        # noqa: E402
from sqlalchemy import text                                # noqa: E402
from app.database.session import get_data_engine           # noqa: E402
from scripts.compare_three_way import load, load_split     # noqa: E402


def sec(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def msa_stock(opts) -> pd.DataFrame:
    """Live warehouse stock for a set of (GEN_ART_NUMBER, CLR) options."""
    if not opts:
        return pd.DataFrame()
    conds = " OR ".join(
        f"(LTRIM(RTRIM(CAST(GEN_ART_NUMBER AS NVARCHAR(50))))='{g}' "
        f"AND LTRIM(RTRIM(CAST(CLR AS NVARCHAR(100))))='{c}')" for g, c in opts)
    with get_data_engine().connect() as cn:
        return pd.read_sql(text(f"""
            SELECT LTRIM(RTRIM(CAST(RDC AS NVARCHAR(50))))  AS RDC,
                   LTRIM(RTRIM(CAST(GEN_ART_NUMBER AS NVARCHAR(50)))) AS GEN_ART_NUMBER,
                   LTRIM(RTRIM(CAST(CLR AS NVARCHAR(100)))) AS CLR,
                   LTRIM(RTRIM(CAST(SZ  AS NVARCHAR(50))))  AS SZ,
                   SUM(TRY_CAST(FNL_Q AS FLOAT))            AS FNL_Q
              FROM ARS_MSA_VAR_ART
             WHERE {conds}
             GROUP BY LTRIM(RTRIM(CAST(RDC AS NVARCHAR(50)))),
                      LTRIM(RTRIM(CAST(GEN_ART_NUMBER AS NVARCHAR(50)))),
                      LTRIM(RTRIM(CAST(CLR AS NVARCHAR(100)))),
                      LTRIM(RTRIM(CAST(SZ AS NVARCHAR(50))))"""), cn)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--own", required=True)
    ap.add_argument("--all", dest="alls", required=True)
    ap.add_argument("--top", type=int, default=3)
    args = ap.parse_args()

    own, alls = load(args.own), load(args.alls)
    sp = load_split(args.alls)
    for c in ("SHIP_QTY", "HOLD_QTY"):
        sp[c] = pd.to_numeric(sp[c], errors="coerce").fillna(0)
    sp["VAR_ART"] = sp.VAR_ART.astype(str).str.strip()
    sp["SZ"] = sp.SZ.astype(str).str.strip()

    # ── 1. pure cross-ship: store fed ONLY from the other warehouse ──────
    sec("EXAMPLE 1 - stores served ENTIRELY by the other warehouse")
    cross = sp[sp.IS_CROSS == 1]
    pure = (cross.groupby(["WERKS", "MAJ_CAT", "GEN_ART_NUMBER", "CLR"])
                 .agg(pcs=("SHIP_QTY", "sum"), rows=("SHIP_QTY", "size"),
                      src=("SRC_RDC", lambda s: "/".join(sorted(set(s)))))
                 .sort_values("pcs", ascending=False).head(args.top))
    print(pure.to_string())
    for (w, mc, ga, clr) in pure.index[:1]:
        o = own[(own.WERKS == w) & (own.GEN_ART_NUMBER == ga) & (own.CLR == clr)]
        a = alls[(alls.WERKS == w) & (alls.GEN_ART_NUMBER == ga) & (alls.CLR == clr)]
        print(f"\n  {w} / {mc} / {ga} / {clr}")
        print(f"    store's own warehouse RDC = "
              f"{a.RDC.iloc[0] if len(a) else '?'}")
        print(f"    OWN mode : {len(o)} rows, shipped {o.SHIP_QTY.sum():.0f}"
              f"   skip={sorted(set(o.SKIP_REASON))[:3]}")
        print(f"    ALL mode : {len(a)} rows, shipped {a.SHIP_QTY.sum():.0f}")
        st = msa_stock([(str(ga), str(clr))])
        if len(st):
            piv = st.pivot_table(index="SZ", columns="RDC", values="FNL_Q",
                                 aggfunc="sum", fill_value=0)
            print("    warehouse stock (ARS_MSA_VAR_ART FNL_Q):")
            print("      " + piv.to_string().replace("\n", "\n      "))

    # ── 2. a genuinely SPLIT line ────────────────────────────────────────
    sec("EXAMPLE 2 - lines filled from BOTH warehouses (a split)")
    g = sp.groupby(["WERKS", "VAR_ART", "SZ"])
    multi = g.size()
    multi = multi[multi > 1]
    if not len(multi):
        print("  none - every line was covered by a single warehouse")
    else:
        print(f"  {len(multi)} split lines of {len(g)} total\n")
        for key in list(multi.index)[:args.top]:
            rows = sp[(sp.WERKS == key[0]) & (sp.VAR_ART == key[1])
                      & (sp.SZ == key[2])]
            print(f"  {key[0]} / VAR_ART {key[1]} / SZ {key[2]}")
            print(rows[["SRC_RDC", "SHIP_QTY", "HOLD_QTY", "IS_CROSS",
                        "PREF_TIER", "SRC_SPLIT_CNT"]].to_string(index=False))
            print()

    # ── 3. who lost, and why ─────────────────────────────────────────────
    sec("EXAMPLE 3 - stores that LOST volume under clubbing")
    os_, as_ = own[own.SHIP_QTY > 0], alls[alls.SHIP_QTY > 0]
    st = (pd.concat([os_.groupby("WERKS").SHIP_QTY.sum().rename("OWN"),
                     as_.groupby("WERKS").SHIP_QTY.sum().rename("ALL")], axis=1)
          .fillna(0))
    st["DELTA"] = st.ALL - st.OWN
    losers = st[st.DELTA < 0].sort_values("DELTA")
    print(f"  {len(losers)} stores lost volume, {st[st.DELTA>0].shape[0]} gained\n")
    print(losers.head(args.top).to_string())
    if len(losers):
        w = losers.index[0]
        o = own[(own.WERKS == w)]
        a = alls[(alls.WERKS == w)]
        print(f"\n  {w}: OWN shipped {o.SHIP_QTY.sum():.0f} over "
              f"{o[o.SHIP_QTY>0].shape[0]} rows; "
              f"ALL shipped {a.SHIP_QTY.sum():.0f} over "
              f"{a[a.SHIP_QTY>0].shape[0]} rows")
        print("  skip reasons OWN:",
              dict(o.SKIP_REASON.replace("", "(allocated)").value_counts().head(5)))
        print("  skip reasons ALL:",
              dict(a.SKIP_REASON.replace("", "(allocated)").value_counts().head(5)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
