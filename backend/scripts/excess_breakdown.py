"""Show why ALLOC_QTY exceeds MJ_REQ for a session.

For each store in a session: total SHIP per OPT_TYPE, plus MJ_REQ, MJ_MBQ,
MJ_STK_TTL. This makes it obvious which OPT_TYPE pushed the store past 100%
of MJ_REQ.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text
from app.database.session import get_data_engine

SESSION = sys.argv[1] if len(sys.argv) > 1 else "20260514_220843_986"
TOP = int(sys.argv[2]) if len(sys.argv) > 2 else 25


def main() -> None:
    eng = get_data_engine()
    with eng.connect() as conn:
        # Pull per-store ship by OPT_TYPE plus MJ_* metrics from working snapshot
        rows = conn.execute(text("""
            WITH ship_by_type AS (
                SELECT WERKS, MAJ_CAT,
                       SUM(CASE WHEN OPT_TYPE='RL'  THEN ISNULL(SHIP_QTY,0) ELSE 0 END) AS SHIP_RL,
                       SUM(CASE WHEN OPT_TYPE='TBC' THEN ISNULL(SHIP_QTY,0) ELSE 0 END) AS SHIP_TBC,
                       SUM(CASE WHEN OPT_TYPE='TBL' THEN ISNULL(SHIP_QTY,0) ELSE 0 END) AS SHIP_TBL,
                       SUM(ISNULL(SHIP_QTY,0))                                  AS SHIP_TOT,
                       SUM(ISNULL(HOLD_QTY,0))                                  AS HOLD_TOT
                FROM ARS_ALLOC_PARKED
                WHERE SESSION_ID = :sid
                GROUP BY WERKS, MAJ_CAT
            ),
            mj AS (
                SELECT WERKS, MAJ_CAT,
                       MAX(TRY_CAST(MJ_MBQ     AS FLOAT)) AS MJ_MBQ,
                       MAX(TRY_CAST(MJ_STK_TTL AS FLOAT)) AS MJ_STK_TTL,
                       MAX(TRY_CAST(MJ_REQ     AS FLOAT)) AS MJ_REQ
                FROM ARS_LISTING_WORKING_PARKED
                WHERE SESSION_ID = :sid
                GROUP BY WERKS, MAJ_CAT
            )
            SELECT s.WERKS, s.MAJ_CAT,
                   s.SHIP_RL, s.SHIP_TBC, s.SHIP_TBL, s.SHIP_TOT, s.HOLD_TOT,
                   mj.MJ_MBQ, mj.MJ_STK_TTL, mj.MJ_REQ,
                   CASE WHEN mj.MJ_REQ > 0 THEN 100.0 * s.SHIP_TOT / mj.MJ_REQ ELSE NULL END AS PCT_OF_REQ,
                   CASE WHEN mj.MJ_MBQ > 0 THEN 100.0 * (mj.MJ_STK_TTL + s.SHIP_TOT) / mj.MJ_MBQ ELSE NULL END AS PCT_OF_MBQ
            FROM ship_by_type s
            LEFT JOIN mj ON mj.WERKS = s.WERKS AND mj.MAJ_CAT = s.MAJ_CAT
            ORDER BY s.SHIP_TOT DESC
        """), {"sid": SESSION}).fetchall()

        print(f"\nSESSION {SESSION} — top {TOP} stores by ship\n")
        print(f"{'WERKS':>6} {'MAJ_CAT':<12} "
              f"{'SHIP_RL':>7} {'SHIP_TBC':>8} {'SHIP_TBL':>8} {'SHIP':>6} {'HOLD':>5}  "
              f"{'MJ_MBQ':>7} {'MJ_STK':>7} {'MJ_REQ':>7}  "
              f"{'%REQ':>6} {'%MBQ':>6}")
        print("-" * 130)
        for r in rows[:TOP]:
            pct_req = f"{r.PCT_OF_REQ:.1f}%" if r.PCT_OF_REQ is not None else "n/a"
            pct_mbq = f"{r.PCT_OF_MBQ:.1f}%" if r.PCT_OF_MBQ is not None else "n/a"
            print(f"{str(r.WERKS):>6} {str(r.MAJ_CAT):<12} "
                  f"{int(r.SHIP_RL):>7} {int(r.SHIP_TBC):>8} {int(r.SHIP_TBL):>8} "
                  f"{int(r.SHIP_TOT):>6} {int(r.HOLD_TOT):>5}  "
                  f"{int(r.MJ_MBQ or 0):>7} {int(r.MJ_STK_TTL or 0):>7} {int(r.MJ_REQ or 0):>7}  "
                  f"{pct_req:>6} {pct_mbq:>6}")

        # Summary: total ship vs MJ_REQ across all stores
        tot_ship = sum(int(r.SHIP_TOT) for r in rows)
        tot_req  = sum(int(r.MJ_REQ or 0) for r in rows)
        tot_mbq  = sum(int(r.MJ_MBQ or 0) for r in rows)
        tot_stk  = sum(int(r.MJ_STK_TTL or 0) for r in rows)
        tot_tbl  = sum(int(r.SHIP_TBL) for r in rows)
        tot_rl   = sum(int(r.SHIP_RL)  for r in rows)
        tot_tbc  = sum(int(r.SHIP_TBC) for r in rows)
        print("-" * 130)
        print(f"\nTotals across {len(rows)} (store,MAJ_CAT) rows:")
        print(f"  SHIP_RL  = {tot_rl}")
        print(f"  SHIP_TBC = {tot_tbc}")
        print(f"  SHIP_TBL = {tot_tbl}  ({100.0*tot_tbl/max(tot_ship,1):.1f}% of total ship)")
        print(f"  SHIP_TOT = {tot_ship}")
        print(f"  MJ_REQ   = {tot_req}   (ship/req = {100.0*tot_ship/max(tot_req,1):.1f}%)")
        print(f"  MJ_MBQ   = {tot_mbq}   (stk+ship vs mbq = {100.0*(tot_stk+tot_ship)/max(tot_mbq,1):.1f}%)")

        # How many stores are over 100% of MJ_REQ?
        over = [r for r in rows if r.PCT_OF_REQ and r.PCT_OF_REQ > 100]
        print(f"\nStores where SHIP > MJ_REQ: {len(over)} / {len(rows)}")
        if over:
            avg = sum(r.PCT_OF_REQ for r in over) / len(over)
            mx = max(over, key=lambda r: r.PCT_OF_REQ)
            print(f"  avg overshoot: {avg:.1f}% of MJ_REQ")
            print(f"  max overshoot: {mx.WERKS} at {mx.PCT_OF_REQ:.1f}% "
                  f"(SHIP {int(mx.SHIP_TOT)} vs MJ_REQ {int(mx.MJ_REQ)})")


if __name__ == "__main__":
    main()
