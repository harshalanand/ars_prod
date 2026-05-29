"""Check VAR_ART pool key match between ALLOC_WORKING and MSA pool source."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database.session import data_engine


def main():
    with data_engine.connect() as conn:
        # 1. ALLOC_WORKING - get distinct VAR_ART for one TBL OPT
        print("\n=== ALLOC_WORKING VAR_ART for 1115095397/L_GRY/TBL @ HB52 ===")
        rows = conn.execute(text("""
            SELECT VAR_ART, SZ, FNL_Q, FNL_Q_REM, SHIP_QTY, ALLOC_STATUS, SKIP_REASON
            FROM ARS_ALLOC_WORKING
            WHERE WERKS='HB52' AND MAJ_CAT='M_W_BERMUDA'
              AND GEN_ART_NUMBER=1115095397 AND CLR='L_GRY' AND OPT_TYPE='TBL'
            ORDER BY VAR_ART, SZ
        """)).fetchall()
        for r in rows: print(dict(r._mapping))

        # 2. Compare with the same OPT for a known shipping store via TBC
        print("\n=== ALLOC_WORKING for 1115095397/L_GRY/TBC any WERKS that allocated ===")
        rows = conn.execute(text("""
            SELECT TOP 5 WERKS, VAR_ART, SZ, FNL_Q, FNL_Q_REM, SHIP_QTY, ALLOC_STATUS
            FROM ARS_ALLOC_WORKING
            WHERE GEN_ART_NUMBER=1115095397 AND CLR='L_GRY' AND OPT_TYPE='TBC'
              AND SHIP_QTY > 0
            ORDER BY WERKS, SZ
        """)).fetchall()
        for r in rows: print(dict(r._mapping))

        # 3. What's the LISTING side - any VAR_ART filtering issue?
        print("\n=== LISTING_WORKING shows OPT_TYPE='TBL' 1115095397/L_GRY @ HB52 ===")
        rows = conn.execute(text("""
            SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR, OPT_TYPE, OPT_REQ, OPT_MBQ,
                   ALLOC_FLAG, LISTED_FLAG, OPT_STATUS, ALLOC_STATUS, ALLOC_REMARKS,
                   ALLOC_PHASE, ALLOC_REASON, ALLOC_SEQ, ALLOC_QTY,
                   PRI_CT_REM, MJ_REQ_REM
            FROM ARS_LISTING_WORKING
            WHERE WERKS='HB52' AND MAJ_CAT='M_W_BERMUDA'
              AND GEN_ART_NUMBER=1115095397 AND CLR='L_GRY' AND OPT_TYPE='TBL'
        """)).fetchall()
        for r in rows: print(dict(r._mapping))

        # 4. Count rows by VAR_ART pattern - is there a VAR_ART present for the failing OPT?
        print("\n=== Distinct VAR_ART for ALL TBL OPTs in HB52/M_W_BERMUDA ===")
        rows = conn.execute(text("""
            SELECT GEN_ART_NUMBER, CLR,
                   COUNT(DISTINCT VAR_ART) AS var_cnt,
                   COUNT(DISTINCT SZ) AS sz_cnt,
                   COUNT(*) AS rows_
            FROM ARS_ALLOC_WORKING
            WHERE WERKS='HB52' AND MAJ_CAT='M_W_BERMUDA' AND OPT_TYPE='TBL'
            GROUP BY GEN_ART_NUMBER, CLR
            ORDER BY var_cnt DESC, sz_cnt DESC
        """)).fetchall()
        for r in rows: print(dict(r._mapping))

        # 5. AND - critically - what does ALLOC_PHASE look like for TBL?
        # Did TBL allocation ever run at all?
        print("\n=== Per-OPT_TYPE ALLOC_PHASE distribution in HB52/M_W_BERMUDA ===")
        rows = conn.execute(text("""
            SELECT OPT_TYPE, ALLOC_PHASE, COUNT(*) AS rows_
            FROM ARS_ALLOC_WORKING
            WHERE WERKS='HB52' AND MAJ_CAT='M_W_BERMUDA'
            GROUP BY OPT_TYPE, ALLOC_PHASE
            ORDER BY OPT_TYPE, ALLOC_PHASE
        """)).fetchall()
        for r in rows: print(dict(r._mapping))

        # 6. WERKS competition: how many OTHER stores claim the same RDC pool
        # for the same TBL OPTs? If many higher-priority stores drain it first ...
        print("\n=== Across all stores: SHIP_QTY for the failing TBL OPTs ===")
        rows = conn.execute(text("""
            SELECT GEN_ART_NUMBER, CLR,
                   SUM(SHIP_QTY) AS sum_ship_all,
                   COUNT(DISTINCT WERKS) AS werks_cnt,
                   SUM(CASE WHEN SHIP_QTY>0 THEN 1 ELSE 0 END) AS rows_shipped
            FROM ARS_ALLOC_WORKING
            WHERE RDC='DW01' AND MAJ_CAT='M_W_BERMUDA' AND OPT_TYPE='TBL'
              AND GEN_ART_NUMBER IN (1115095397, 1115095399, 1115108429)
            GROUP BY GEN_ART_NUMBER, CLR
            ORDER BY GEN_ART_NUMBER, CLR
        """)).fetchall()
        for r in rows: print(dict(r._mapping))


if __name__ == "__main__":
    main()
