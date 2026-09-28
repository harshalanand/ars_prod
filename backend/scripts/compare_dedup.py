#!/usr/bin/env python
"""
Own vs All RDCs with the Part-2 fan-out collapsed.

    python scripts/compare_dedup.py <own_sid> <all_sid>

Under All RDCs the Part 2 "MSA missing" INSERT joins stores to the MSA option
list with `ON 1=1` (the RDC predicate is added only in `own` mode), so an
option stocked at BOTH warehouses produces two identical listing rows and,
downstream, two identical allocation lines. This collapses each byte-identical
(WERKS, VAR_ART, SZ) group to a single line so the business delta can be read
without the phantom volume.

CAVEAT: this is an after-the-fact correction. The duplicated rows also
competed for pool stock during allocation, so a run with the defect FIXED
would not produce exactly these numbers — it would likely produce slightly
more, since the phantom lines consumed pool that real lines could have used.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd                                        # noqa: E402
from scripts.compare_three_way import load                 # noqa: E402

KEY = ["WERKS", "VAR_ART", "SZ"]
# columns that must all match for two rows to count as the same line
IDENT = ["MAJ_CAT", "GEN_ART_NUMBER", "CLR", "OPT_TYPE", "SHIP_QTY",
         "HOLD_QTY", "ALLOC_WAVE", "ALLOC_ROUND", "ALLOC_STATUS"]


def dedup(df: pd.DataFrame) -> tuple[pd.DataFrame, int, float]:
    """Drop byte-identical duplicate allocation lines. Returns
    (deduped, n_dropped, qty_dropped)."""
    before_rows, before_qty = len(df), df.SHIP_QTY.sum()
    out = df.drop_duplicates(subset=KEY + IDENT, keep="first")
    # anything still duplicated on KEY is a GENUINE multi-line case, left alone
    return out, before_rows - len(out), before_qty - out.SHIP_QTY.sum()


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    own, alls = load(sys.argv[1]), load(sys.argv[2])

    own_d, own_drop, own_q = dedup(own)
    all_d, all_drop, all_q = dedup(alls)

    print("=" * 74)
    print("FAN-OUT CORRECTION")
    print("=" * 74)
    print(f"  OWN  dropped {own_drop:>7} identical dup rows  ({own_q:>8.0f} pcs)")
    print(f"  ALL  dropped {all_drop:>7} identical dup rows  ({all_q:>8.0f} pcs)")
    still = all_d.duplicated(subset=KEY, keep=False).sum()
    print(f"  ALL  rows still sharing a key after dedup: {still} "
          f"(genuine multi-line, left alone)")

    print("\n" + "=" * 74)
    print("BUSINESS DELTA — corrected")
    print("=" * 74)
    o, a = own_d, all_d
    os_, as_ = o[o.SHIP_QTY > 0], a[a.SHIP_QTY > 0]

    def row(label, x, y):
        d = y - x
        pct = (d / x * 100) if x else float("nan")
        print(f"  {label:<22}{x:>10.0f}  ->{y:>10.0f}   {d:+10.0f}   {pct:+7.1f}%")

    row("SHIP qty", o.SHIP_QTY.sum(), a.SHIP_QTY.sum())
    row("rows with SHIP>0", len(os_), len(as_))
    row("stores fed", os_.WERKS.nunique(), as_.WERKS.nunique())
    row("options fed", os_.groupby(["GEN_ART_NUMBER", "CLR"]).ngroups,
        as_.groupby(["GEN_ART_NUMBER", "CLR"]).ngroups)

    print("\n  -- OPT_TYPE shipped (corrected) --")
    for k in sorted(set(os_.OPT_TYPE.dropna()) | set(as_.OPT_TYPE.dropna())):
        qo = os_[os_.OPT_TYPE == k].SHIP_QTY.sum()
        qa = as_[as_.OPT_TYPE == k].SHIP_QTY.sum()
        print(f"    {k:<6} {qo:>8.0f} -> {qa:<8.0f} ({qa-qo:+.0f})")

    print("\n  -- per store (corrected) --")
    st = (pd.concat([os_.groupby("WERKS").SHIP_QTY.sum().rename("OWN"),
                     as_.groupby("WERKS").SHIP_QTY.sum().rename("ALL")], axis=1)
          .fillna(0))
    st["DELTA"] = st.ALL - st.OWN
    print(f"    gained {int((st.DELTA > 0).sum()):>4} stores  "
          f"{st[st.DELTA>0].DELTA.sum():+,.0f} pcs")
    print(f"    lost   {int((st.DELTA < 0).sum()):>4} stores  "
          f"{st[st.DELTA<0].DELTA.sum():+,.0f} pcs")
    print(f"    flat   {int((st.DELTA == 0).sum()):>4} stores")

    print("\n  -- per MAJ_CAT (corrected) --")
    m = (pd.concat([os_.groupby("MAJ_CAT").SHIP_QTY.sum().rename("OWN"),
                    as_.groupby("MAJ_CAT").SHIP_QTY.sum().rename("ALL")], axis=1)
         .fillna(0))
    m["DELTA"] = m.ALL - m.OWN
    print(m.sort_values("DELTA", ascending=False).to_string(
        float_format=lambda x: f"{x:,.0f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
