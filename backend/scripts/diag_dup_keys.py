#!/usr/bin/env python
"""
Does the allocation working table hold DUPLICATE (WERKS, VAR_ART, SZ) lines,
and does clubbing introduce them?

    python scripts/diag_dup_keys.py <own_sid> <all_sid>

The split pass (Part 8.37) keys its stamp/reduction UPDATEs on
(WERKS, VAR_ART, SZ). If that is not unique per allocation line, one walked
line's reduction lands on every duplicate, and the split table and the alloc
table stop agreeing. This measures the duplication independently of the split
pass, on the PARKED copy of each run.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from sqlalchemy import text                                # noqa: E402
from app.database.session import get_data_engine           # noqa: E402

KEYEXPR = ("WERKS, LTRIM(RTRIM(CAST(VAR_ART AS NVARCHAR(50)))), "
           "LTRIM(RTRIM(ISNULL(SZ,'')))")
# same three expressions, aliased — a derived table needs named columns
KEYSEL = ("WERKS AS W, LTRIM(RTRIM(CAST(VAR_ART AS NVARCHAR(50)))) AS VA, "
          "LTRIM(RTRIM(ISNULL(SZ,''))) AS SZK")


def report(c, tag: str, sid: str) -> None:
    print(f"== {tag}  ({sid}) ==")
    for label, extra in (("all rows", ""),
                         ("SHIP>0 only", " AND TRY_CAST(SHIP_QTY AS FLOAT) > 0")):
        dup = c.execute(text(f"""
            SELECT COUNT(*) AS grp, ISNULL(SUM(N),0) AS rows_in_dup,
                   ISNULL(SUM(Q),0) AS ship_in_dup,
                   ISNULL(SUM(Q - Q/N),0) AS excess_ship
              FROM (SELECT {KEYSEL},
                           COUNT(*) AS N,
                           SUM(TRY_CAST(SHIP_QTY AS FLOAT)) AS Q
                      FROM ARS_ALLOC_PARKED
                     WHERE SESSION_ID = :sid {extra}
                     GROUP BY {KEYEXPR}
                    HAVING COUNT(*) > 1) X"""), {"sid": sid}).fetchone()
        tot = c.execute(text(f"""
            SELECT COUNT(*) FROM (SELECT 1 AS x FROM ARS_ALLOC_PARKED
                    WHERE SESSION_ID = :sid {extra}
                    GROUP BY {KEYEXPR}) Y"""), {"sid": sid}).scalar()
        print(f"  {label:<12} dup groups {dup[0]:>7} / {tot:<8}"
              f"  rows-in-dups {dup[1]:>8}"
              f"  ship-in-dups {dup[2]:>9.0f}"
              f"  excess {dup[3]:>8.0f}")

    # Are the duplicate rows genuinely identical, or different allocation lines?
    same = c.execute(text(f"""
        SELECT COUNT(*) FROM (
            SELECT {KEYSEL}
              FROM ARS_ALLOC_PARKED
             WHERE SESSION_ID = :sid AND TRY_CAST(SHIP_QTY AS FLOAT) > 0
             GROUP BY {KEYEXPR}
            HAVING COUNT(*) > 1
               AND COUNT(DISTINCT CONCAT(MAJ_CAT,'|',GEN_ART_NUMBER,'|',CLR,'|',
                         OPT_TYPE,'|',CAST(SHIP_QTY AS NVARCHAR(30)),'|',
                         ISNULL(ALLOC_WAVE,''),'|',
                         CAST(ISNULL(ALLOC_ROUND,0) AS NVARCHAR(10)))) = 1
        ) Z"""), {"sid": sid}).scalar()
    print(f"  of the SHIP>0 dup groups, {same} are byte-identical copies\n")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    own, alls = sys.argv[1], sys.argv[2]
    with get_data_engine().connect() as c:
        report(c, "OWN", own)
        report(c, "ALL RDCs", alls)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
