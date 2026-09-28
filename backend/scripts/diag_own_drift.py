#!/usr/bin/env python
"""
Why does today's Own run differ from yesterday's, when yesterday's Own was
byte-identical to the reference run?

    python scripts/diag_own_drift.py <own_25th_sid> <own_26th_sid>

Compares the two Own runs row by row and reports how much of the difference
sits on (store, option) keys whose INPUTS changed — i.e. rows the source data
moved under, rather than rows the code decided differently about.
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
from sqlalchemy import text                                # noqa: E402
from app.database.session import get_data_engine           # noqa: E402
from scripts.compare_three_way import load, KEY, OUTCOME   # noqa: E402


def source_freshness() -> None:
    print("=" * 74)
    print("1. Did the SOURCE data change between the two runs?")
    print("=" * 74)
    with get_data_engine().connect() as c:
        for tbl, col in (("ARS_GRID_MJ_GEN_ART", None),
                         ("store_stock", None),
                         ("store_sales", None),
                         ("ARS_NL_TBL_HOLD_TRACKING", "LAST_UPDATED"),
                         ("ARS_PEND_ALC", None),
                         ("Master_ALC_INPUT_ST_MASTER", None)):
            try:
                n = c.execute(text(f"SELECT COUNT(*) FROM [{tbl}]")).scalar()
                extra = ""
                if col:
                    mx = c.execute(text(f"SELECT MAX([{col}]) FROM [{tbl}]")).scalar()
                    extra = f"   max {col} = {mx}"
                # look for any datetime column to date the table
                dt = c.execute(text("""
                    SELECT TOP 1 COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                     WHERE TABLE_NAME = :t AND DATA_TYPE IN ('datetime','datetime2','date')
                     ORDER BY ORDINAL_POSITION"""), {"t": tbl}).scalar()
                if dt and not col:
                    mx = c.execute(text(f"SELECT MAX([{dt}]) FROM [{tbl}]")).scalar()
                    extra = f"   max {dt} = {mx}"
                print(f"  {tbl:<30} rows {n:>12,}{extra}")
            except Exception as e:
                print(f"  {tbl:<30} -- {str(e)[:70]}")


def row_diff(a_sid: str, b_sid: str) -> None:
    print("\n" + "=" * 74)
    print("2. Row-level difference between the two Own runs")
    print("=" * 74)
    a, b = load(a_sid), load(b_sid)
    print(f"  25th Own {len(a):>8,} rows   ship {a.SHIP_QTY.sum():>9,.0f}")
    print(f"  26th Own {len(b):>8,} rows   ship {b.SHIP_QTY.sum():>9,.0f}")

    ai = a.set_index(KEY)
    bi = b.set_index(KEY)
    only_a = ai.index.difference(bi.index)
    only_b = bi.index.difference(ai.index)
    common = ai.index.intersection(bi.index)
    print(f"\n  keys only on the 25th : {len(only_a):>8,}"
          f"   ship {a.set_index(KEY).loc[only_a].SHIP_QTY.sum():>8,.0f}")
    print(f"  keys only on the 26th : {len(only_b):>8,}"
          f"   ship {b.set_index(KEY).loc[only_b].SHIP_QTY.sum():>8,.0f}")
    print(f"  keys in both          : {len(common):>8,}")

    ca = ai.loc[common, OUTCOME].sort_index()
    cb = bi.loc[common, OUTCOME].sort_index()
    diff = (ca.fillna("~") != cb.fillna("~"))
    changed = int(diff.any(axis=1).sum())
    print(f"\n  of the shared keys, {changed:,} differ "
          f"({changed / max(len(common),1) * 100:.2f}%)")
    for col in OUTCOME:
        d = int(diff[col].sum())
        if d:
            print(f"      {col:<14} {d:>8,} rows")
    dq = cb.SHIP_QTY.sum() - ca.SHIP_QTY.sum()
    print(f"  ship delta on shared keys: {dq:+,.0f}")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    source_freshness()
    row_diff(sys.argv[1], sys.argv[2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
