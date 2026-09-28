#!/usr/bin/env python
"""
Where do the duplicate allocation lines under All RDCs come from?

    python scripts/diag_dup_source.py <own_sid> <all_sid>

Checks the listing grain (WERKS, GEN_ART_NUMBER, CLR) in ARS_LISTING_PARKED.
If the listing is already duplicated, the fan-out happened when the MSA join
lost its `L.RDC = M.MSA_RDC` pin, and the engine merely allocated what it was
handed. If the listing is clean, the duplication was introduced later.
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


def listing_dups(c, sid: str) -> None:
    row = c.execute(text("""
        SELECT COUNT(*) AS grp, ISNULL(SUM(N),0) AS rows_in_dup
          FROM (SELECT WERKS AS W, GEN_ART_NUMBER AS G,
                       LTRIM(RTRIM(ISNULL(CLR,''))) AS C, COUNT(*) AS N
                  FROM ARS_LISTING_PARKED
                 WHERE SESSION_ID = :sid AND ISNULL(GEN_ART_NUMBER,0) <> 0
                 GROUP BY WERKS, GEN_ART_NUMBER, LTRIM(RTRIM(ISNULL(CLR,'')))
                HAVING COUNT(*) > 1) X"""), {"sid": sid}).fetchone()
    tot = c.execute(text("""
        SELECT COUNT(*) FROM (SELECT 1 AS x FROM ARS_LISTING_PARKED
                WHERE SESSION_ID = :sid AND ISNULL(GEN_ART_NUMBER,0) <> 0
                GROUP BY WERKS, GEN_ART_NUMBER, LTRIM(RTRIM(ISNULL(CLR,'')))) Y"""),
        {"sid": sid}).scalar()
    n = c.execute(text("SELECT COUNT(*) FROM ARS_LISTING_PARKED WHERE SESSION_ID=:sid AND ISNULL(GEN_ART_NUMBER,0) <> 0"),
                  {"sid": sid}).scalar()
    print(f"  listing rows {n:>9}   distinct (WERKS,GEN_ART,CLR) {tot:>9}"
          f"   dup groups {row[0]:>8}   rows in dups {row[1]:>9}")


def sample(c, sid: str) -> None:
    """Show one duplicated listing key with the columns that might differ."""
    key = c.execute(text("""
        SELECT TOP 1 WERKS, GEN_ART_NUMBER, LTRIM(RTRIM(ISNULL(CLR,''))) AS CLR
          FROM ARS_LISTING_PARKED
         WHERE SESSION_ID = :sid AND ISNULL(GEN_ART_NUMBER,0) <> 0
         GROUP BY WERKS, GEN_ART_NUMBER, LTRIM(RTRIM(ISNULL(CLR,'')))
        HAVING COUNT(*) > 1"""), {"sid": sid}).fetchone()
    if not key:
        print("  (no duplicated listing keys)")
        return
    print(f"\n  sample duplicated listing key: {tuple(key)}")
    rows = c.execute(text("""
        SELECT RDC, OPT_TYPE, IS_NEW,
               TRY_CAST(MSA_FNL_Q AS FLOAT) AS MSA_FNL_Q,
               TRY_CAST(VAR_COUNT AS FLOAT) AS VAR_COUNT,
               TRY_CAST(STK_TTL AS FLOAT)   AS STK_TTL,
               TRY_CAST(OPT_REQ AS FLOAT)   AS OPT_REQ
          FROM ARS_LISTING_PARKED
         WHERE SESSION_ID = :sid AND WERKS = :w AND GEN_ART_NUMBER = :g
           AND LTRIM(RTRIM(ISNULL(CLR,''))) = :c"""),
        {"sid": sid, "w": key[0], "g": key[1], "c": key[2]}).fetchall()
    print("    RDC  OPT_TYPE IS_NEW MSA_FNL_Q VAR_COUNT STK_TTL OPT_REQ")
    for r in rows:
        print("    " + "  ".join(str(x) for x in r))


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    with get_data_engine().connect() as c:
        for tag, sid in (("OWN", sys.argv[1]), ("ALL RDCs", sys.argv[2])):
            print(f"== {tag} ({sid}) ==")
            listing_dups(c, sid)
            sample(c, sid)
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
