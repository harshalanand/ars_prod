#!/usr/bin/env python
"""
One concrete store-option, told end to end: Own vs All RDCs.

    python scripts/show_one_example.py <own_sid> <all_sid>

Picks a real (store, option) that was duplicated under All RDCs and shows
the warehouse stock, what each mode listed, what each mode allocated, and
what the picklist says — so the fan-out is visible in one place.
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


def main() -> int:
    own_sid, all_sid = sys.argv[1], sys.argv[2]
    with get_data_engine().connect() as c:
        # a store-option that shipped under ALL and is duplicated
        cand = c.execute(text("""
            SELECT TOP 1 WERKS, MAJ_CAT, GEN_ART_NUMBER,
                   LTRIM(RTRIM(ISNULL(CLR,''))) AS CLR,
                   LTRIM(RTRIM(CAST(VAR_ART AS NVARCHAR(50)))) AS VA,
                   LTRIM(RTRIM(ISNULL(SZ,''))) AS SZK,
                   COUNT(*) AS N, SUM(TRY_CAST(SHIP_QTY AS FLOAT)) AS Q
              FROM ARS_ALLOC_PARKED
             WHERE SESSION_ID = :s AND TRY_CAST(SHIP_QTY AS FLOAT) > 0
             GROUP BY WERKS, MAJ_CAT, GEN_ART_NUMBER,
                      LTRIM(RTRIM(ISNULL(CLR,''))),
                      LTRIM(RTRIM(CAST(VAR_ART AS NVARCHAR(50)))),
                      LTRIM(RTRIM(ISNULL(SZ,'')))
            HAVING COUNT(*) > 1
             ORDER BY SUM(TRY_CAST(SHIP_QTY AS FLOAT)) DESC"""),
            {"s": all_sid}).fetchone()
        w, mc, ga, clr, va, sz = cand[0], cand[1], cand[2], cand[3], cand[4], cand[5]
        p = {"w": w, "mc": mc, "ga": ga, "clr": clr, "va": va, "sz": sz,
             "own": own_sid, "all": all_sid}

        print(f"STORE {w}   OPTION {mc} / {ga} / {clr}   SIZE {sz}\n")

        rdc = c.execute(text(
            "SELECT TOP 1 RDC FROM ARS_ALLOC_PARKED WHERE SESSION_ID=:all "
            "AND WERKS=:w"), p).scalar()
        print(f"  {w} belongs to warehouse {rdc}\n")

        print("  WAREHOUSE STOCK for this option (ARS_MSA_VAR_ART, FNL_Q):")
        for r in c.execute(text("""
            SELECT LTRIM(RTRIM(CAST(RDC AS NVARCHAR(20)))) AS RDC,
                   LTRIM(RTRIM(CAST(SZ AS NVARCHAR(20))))  AS SZ,
                   SUM(TRY_CAST(FNL_Q AS FLOAT))           AS FNL_Q
              FROM ARS_MSA_VAR_ART
             WHERE LTRIM(RTRIM(CAST(GEN_ART_NUMBER AS NVARCHAR(50)))) = :ga
               AND LTRIM(RTRIM(CAST(CLR AS NVARCHAR(100)))) = :clr
             GROUP BY LTRIM(RTRIM(CAST(RDC AS NVARCHAR(20)))),
                      LTRIM(RTRIM(CAST(SZ AS NVARCHAR(20))))
             ORDER BY 2, 1"""), {"ga": str(ga), "clr": clr}):
            mark = "  <-- this size" if str(r[1]).strip() == sz else ""
            print(f"    {r[0]:<6} {r[1]:<8} {r[2]:>6.0f}{mark}")

        for tag, sid in (("OWN", own_sid), ("ALL RDCs", all_sid)):
            print(f"\n  ---- {tag} ----")
            rows = c.execute(text("""
                SELECT RDC, OPT_TYPE,
                       TRY_CAST(SHIP_QTY AS FLOAT) AS SHIP,
                       TRY_CAST(FNL_Q AS FLOAT)    AS FNL_Q,
                       TRY_CAST(SZ_REQ AS FLOAT)   AS SZ_REQ,
                       ISNULL(SRC_RDC,'-')         AS SRC_RDC,
                       ALLOC_STATUS
                  FROM ARS_ALLOC_PARKED
                 WHERE SESSION_ID=:sid AND WERKS=:w
                   AND LTRIM(RTRIM(CAST(VAR_ART AS NVARCHAR(50))))=:va
                   AND LTRIM(RTRIM(ISNULL(SZ,'')))=:sz"""),
                {**p, "sid": sid}).fetchall()
            if not rows:
                print("    (no allocation row)")
                continue
            print("    RDC    TYPE  SHIP  POOL_FNL_Q  SZ_REQ  SRC_RDC  STATUS")
            for r in rows:
                print(f"    {r[0]:<6} {r[1]:<5} {r[2]:>4.0f}  {r[3]:>10.0f}"
                      f"  {r[4]:>6.0f}  {r[5]:<7}  {r[6]}")
            print(f"    -> allocation table says this store gets "
                  f"{sum(r[2] or 0 for r in rows):.0f} pcs")

        print("\n  ---- PICKLIST (ARS_ALLOC_RDC_SPLIT, All RDCs only) ----")
        sp = c.execute(text("""
            SELECT SRC_RDC, TRY_CAST(SHIP_QTY AS FLOAT), STORE_RDC, IS_CROSS
              FROM ARS_ALLOC_RDC_SPLIT
             WHERE SESSION_ID=:all AND WERKS=:w
               AND LTRIM(RTRIM(CAST(VAR_ART AS NVARCHAR(50))))=:va
               AND LTRIM(RTRIM(ISNULL(SZ,'')))=:sz"""), p).fetchall()
        for r in sp:
            print(f"    ship {r[1]:>4.0f} from {r[0]}"
                  + ("   CROSS" if r[3] else "   own"))
        print(f"    -> picklist says dispatch {sum(r[1] or 0 for r in sp):.0f} pcs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
