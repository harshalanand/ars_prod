"""Debug OPT 1116113040 — SHIP/HOLD/POOL/ALLOC vs OPT_MBQ / OPT_MBQ_WH."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from app.database.session import DataSessionLocal

GA = '1116113040'

q1 = """
SELECT WERKS, CLR, OPT_TYPE,
       MAX(OPT_MBQ)        AS opt_mbq,
       MAX(OPT_MBQ_WH)     AS opt_mbq_wh,
       SUM(SHIP_QTY)       AS sum_ship,
       SUM(HOLD_QTY)       AS sum_hold,
       SUM(POOL_CONSUMED)  AS sum_pool_consumed,
       SUM(ALLOC_QTY)      AS sum_alloc,
       COUNT(*)            AS n_rows
FROM ARS_ALLOC_WORKING
WHERE GEN_ART_NUMBER = :ga
GROUP BY WERKS, CLR, OPT_TYPE
ORDER BY WERKS, CLR
"""

q2_pick = """
SELECT TOP 1 WERKS, CLR FROM ARS_ALLOC_WORKING
WHERE GEN_ART_NUMBER=:ga AND ISNULL(SHIP_QTY,0)>0
ORDER BY WERKS, CLR
"""

q2 = """
SELECT TOP 40 WERKS, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
       I_ROD, SZ_MBQ, SZ_MBQ_WH, SZ_STK, SZ_REQ_WH,
       SHIP_QTY, HOLD_QTY, POOL_CONSUMED, ALLOC_QTY,
       OPT_MBQ, OPT_MBQ_WH, OPT_TYPE,
       ALLOC_STATUS, SKIP_REASON,
       LEFT(ISNULL(ALLOC_REMARKS,''), 400) AS remarks
FROM ARS_ALLOC_WORKING
WHERE GEN_ART_NUMBER = :ga AND WERKS = :w
ORDER BY CLR, VAR_ART, SZ
"""

with DataSessionLocal() as db:
    rows = db.execute(text(q1), {"ga": GA}).fetchall()
    print(f"=== Q1: per-(WERKS,CLR,OPT_TYPE) totals for GA={GA} ({len(rows)} rows) ===")
    hdr = ["WERKS","CLR","OPT_TYPE","opt_mbq","opt_mbq_wh","sum_ship","sum_hold","sum_pool","sum_alloc","n"]
    print("\t".join(hdr))
    for r in rows:
        print("\t".join(str(x) for x in r))

    print()
    print("=== Q1 derived comparisons ===")
    print("WERKS\tCLR\tOPT_TYPE\tship-hold\tship+hold\topt_mbq\topt_mbq_wh\tpool_consumed\tsum_alloc\tcheck")
    for r in rows:
        werks, clr, ot, opt_mbq, opt_mbq_wh, sship, shold, spc, salloc, n = r
        sship = float(sship or 0); shold = float(shold or 0)
        opt_mbq = float(opt_mbq or 0); opt_mbq_wh = float(opt_mbq_wh or 0)
        spc = float(spc or 0); salloc = float(salloc or 0)
        ship_minus = sship - shold
        ship_plus  = sship + shold
        chk = []
        if abs(ship_minus - opt_mbq) < 0.5: chk.append("ship-hold==opt_mbq")
        if abs(sship - opt_mbq) < 0.5: chk.append("ship==opt_mbq")
        if abs(sship - opt_mbq_wh) < 0.5: chk.append("ship==opt_mbq_wh")
        if abs(ship_plus - opt_mbq_wh) < 0.5: chk.append("ship+hold==opt_mbq_wh")
        if abs(spc - opt_mbq_wh) < 0.5: chk.append("pool==opt_mbq_wh")
        if abs(spc - opt_mbq) < 0.5: chk.append("pool==opt_mbq")
        if abs(salloc - sship) < 0.5: chk.append("alloc==ship")
        print(f"{werks}\t{clr}\t{ot}\t{ship_minus:.1f}\t{ship_plus:.1f}\t{opt_mbq:.1f}\t{opt_mbq_wh:.1f}\t{spc:.1f}\t{salloc:.1f}\t{','.join(chk)}")

    print()
    pick = db.execute(text(q2_pick), {"ga": GA}).fetchone()
    if pick is None:
        print("No row with SHIP_QTY>0 — picking any WERKS")
        pick = db.execute(text("SELECT TOP 1 WERKS, CLR FROM ARS_ALLOC_WORKING WHERE GEN_ART_NUMBER=:ga"), {"ga": GA}).fetchone()
    if pick:
        w, c = pick
        print(f"=== Q2: per-size detail for WERKS={w} ===")
        d = db.execute(text(q2), {"ga": GA, "w": w}).fetchall()
        if d:
            cols = list(d[0]._mapping.keys())
            print("\t".join(cols))
            for r in d:
                print("\t".join(str(x) for x in r))
