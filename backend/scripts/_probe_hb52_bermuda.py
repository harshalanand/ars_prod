"""Investigate HB52 / M_W_BERMUDA alloc < req gap."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database.session import data_engine

WERKS = "HB52"
MAJ_CAT = "M_W_BERMUDA"


def hdr(s):
    print("\n" + "=" * 90); print(s); print("=" * 90)


def main():
    with data_engine.connect() as conn:
        # ---- 1. MAJ_CAT-level rollup from LISTING_WORKING (the demand side) ----
        hdr("1. MAJ_CAT-level demand from ARS_LISTING_WORKING (single OPT row)")
        rows = conn.execute(text("""
            SELECT TOP 5
                WERKS, MAJ_CAT, COUNT(*) AS OPT_ROWS,
                SUM(OPT_REQ) AS SUM_OPT_REQ,
                SUM(OPT_MBQ) AS SUM_OPT_MBQ,
                MAX(MJ_REQ) AS MJ_REQ_max,
                MAX(MJ_REQ_REV) AS MJ_REQ_REV_max,
                MAX(MJ_REQ_ORIG) AS MJ_REQ_ORIG_max,
                MAX(MJ_MBQ) AS MJ_MBQ_max,
                MAX(MJ_MBQ_REV) AS MJ_MBQ_REV_max
            FROM ARS_LISTING_WORKING
            WHERE WERKS=:w AND MAJ_CAT=:m
            GROUP BY WERKS, MAJ_CAT
        """), {"w": WERKS, "m": MAJ_CAT}).fetchall()
        for r in rows:
            print(dict(r._mapping))

        # ---- 2. ALLOC_WORKING totals at MAJ_CAT level ----
        hdr("2. MAJ_CAT-level alloc rollup from ARS_ALLOC_WORKING")
        rows = conn.execute(text("""
            SELECT
                COUNT(*) AS rows_,
                COUNT(DISTINCT CONCAT(GEN_ART_NUMBER,'|',CLR,'|',OPT_TYPE)) AS opt_cnt,
                SUM(OPT_REQ) AS sum_opt_req_dup,
                SUM(SZ_REQ) AS sum_sz_req,
                SUM(SHIP_QTY) AS sum_ship,
                SUM(ALLOC_QTY) AS sum_alloc,
                SUM(HOLD_QTY) AS sum_hold
            FROM ARS_ALLOC_WORKING
            WHERE WERKS=:w AND MAJ_CAT=:m
        """), {"w": WERKS, "m": MAJ_CAT}).fetchone()
        print(dict(rows._mapping))

        # ---- 3. Per OPT_TYPE rollup at MAJ_CAT ----
        hdr("3. Per-OPT_TYPE rollup (HB52, M_W_BERMUDA)")
        rows = conn.execute(text("""
            SELECT
                OPT_TYPE,
                COUNT(DISTINCT CONCAT(GEN_ART_NUMBER,'|',CLR)) AS opt_cnt,
                SUM(SZ_REQ) AS sz_req,
                SUM(SHIP_QTY) AS sum_ship,
                SUM(ALLOC_QTY) AS sum_alloc,
                SUM(HOLD_QTY) AS sum_hold
            FROM ARS_ALLOC_WORKING
            WHERE WERKS=:w AND MAJ_CAT=:m
            GROUP BY OPT_TYPE
            ORDER BY OPT_TYPE
        """), {"w": WERKS, "m": MAJ_CAT}).fetchall()
        for r in rows:
            print(dict(r._mapping))

        # ---- 4. ALLOC_STATUS distribution ----
        hdr("4. ALLOC_STATUS distribution (row level)")
        rows = conn.execute(text("""
            SELECT ALLOC_STATUS, COUNT(*) AS rows_,
                   SUM(SZ_REQ) AS sz_req, SUM(SHIP_QTY) AS ship
            FROM ARS_ALLOC_WORKING
            WHERE WERKS=:w AND MAJ_CAT=:m
            GROUP BY ALLOC_STATUS
            ORDER BY ALLOC_STATUS
        """), {"w": WERKS, "m": MAJ_CAT}).fetchall()
        for r in rows:
            print(dict(r._mapping))

        # ---- 5. SKIP_REASON distribution ----
        hdr("5. SKIP_REASON distribution")
        rows = conn.execute(text("""
            SELECT SKIP_REASON, COUNT(*) AS rows_,
                   SUM(SZ_REQ) AS sz_req
            FROM ARS_ALLOC_WORKING
            WHERE WERKS=:w AND MAJ_CAT=:m
            GROUP BY SKIP_REASON
            ORDER BY rows_ DESC
        """), {"w": WERKS, "m": MAJ_CAT}).fetchall()
        for r in rows:
            print(dict(r._mapping))

        # ---- 6. OPT-level grouping: REQ vs SHIP per OPT ----
        hdr("6. Per-OPT REQ vs SHIP (ALLOC_WORKING) — sorted by gap desc")
        rows = conn.execute(text("""
            SELECT
                GEN_ART_NUMBER, CLR, OPT_TYPE,
                MAX(OPT_REQ) AS opt_req,
                MAX(OPT_MBQ) AS opt_mbq,
                SUM(SZ_REQ) AS sum_sz_req,
                SUM(SHIP_QTY) AS sum_ship,
                SUM(ALLOC_QTY) AS sum_alloc,
                SUM(HOLD_QTY) AS sum_hold,
                MAX(OPT_PRIORITY_TIER) AS pri_tier,
                MAX(OPT_PRIORITY_RANK) AS pri_rank
            FROM ARS_ALLOC_WORKING
            WHERE WERKS=:w AND MAJ_CAT=:m
            GROUP BY GEN_ART_NUMBER, CLR, OPT_TYPE
            ORDER BY (MAX(OPT_REQ) - SUM(SHIP_QTY)) DESC, GEN_ART_NUMBER, CLR
        """), {"w": WERKS, "m": MAJ_CAT}).fetchall()
        for r in rows:
            d = dict(r._mapping)
            gap = (d["opt_req"] or 0) - (d["sum_ship"] or 0)
            d["GAP"] = gap
            print(d)

        # ---- 7. SKIP_REASON for OPTs with gap > 0 ----
        hdr("7. Row-level SKIP_REASON for OPTs where sum_ship < opt_req")
        rows = conn.execute(text("""
            WITH per_opt AS (
                SELECT GEN_ART_NUMBER, CLR, OPT_TYPE,
                       MAX(OPT_REQ) AS opt_req,
                       SUM(SHIP_QTY) AS sum_ship
                FROM ARS_ALLOC_WORKING
                WHERE WERKS=:w AND MAJ_CAT=:m
                GROUP BY GEN_ART_NUMBER, CLR, OPT_TYPE
            )
            SELECT a.GEN_ART_NUMBER, a.CLR, a.OPT_TYPE, a.SZ, a.PAK_SZ,
                   a.SZ_REQ, a.SZ_STK, a.SZ_MBQ, a.SHIP_QTY, a.HOLD_QTY,
                   a.ALLOC_STATUS, a.SKIP_REASON,
                   LEFT(a.ALLOC_REMARKS, 220) AS REMARKS
            FROM ARS_ALLOC_WORKING a
            JOIN per_opt p
              ON a.GEN_ART_NUMBER=p.GEN_ART_NUMBER AND a.CLR=p.CLR AND a.OPT_TYPE=p.OPT_TYPE
            WHERE a.WERKS=:w AND a.MAJ_CAT=:m
              AND p.sum_ship < p.opt_req
            ORDER BY a.GEN_ART_NUMBER, a.CLR, a.OPT_TYPE, a.SZ
        """), {"w": WERKS, "m": MAJ_CAT}).fetchall()
        for r in rows:
            print(dict(r._mapping))

        # ---- 8. From LISTING side: LISTED_FLAG / OPT_STATUS / LISTED_REASON ----
        hdr("8. LISTING-side OPT status: which OPTs even made it to alloc?")
        rows = conn.execute(text("""
            SELECT OPT_TYPE, LISTED_FLAG, ALLOC_FLAG, OPT_STATUS, LISTED_REASON,
                   COUNT(*) AS opts,
                   SUM(OPT_REQ) AS sum_opt_req
            FROM ARS_LISTING_WORKING
            WHERE WERKS=:w AND MAJ_CAT=:m
            GROUP BY OPT_TYPE, LISTED_FLAG, ALLOC_FLAG, OPT_STATUS, LISTED_REASON
            ORDER BY OPT_TYPE, LISTED_FLAG, ALLOC_FLAG, OPT_STATUS
        """), {"w": WERKS, "m": MAJ_CAT}).fetchall()
        for r in rows:
            print(dict(r._mapping))


if __name__ == "__main__":
    main()
