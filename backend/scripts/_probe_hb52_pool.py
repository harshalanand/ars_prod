"""HB52 / M_W_BERMUDA TBL pool diagnosis."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database.session import data_engine

WERKS = "HB52"
MAJ_CAT = "M_W_BERMUDA"


def hdr(s):
    print("\n" + "=" * 80); print(s); print("=" * 80)


def main():
    with data_engine.connect() as conn:
        # Which RDC feeds HB52?
        hdr("RDC for HB52 in ALLOC_WORKING")
        rows = conn.execute(text("""
            SELECT DISTINCT RDC FROM ARS_ALLOC_WORKING WHERE WERKS=:w AND MAJ_CAT=:m
        """), {"w": WERKS, "m": MAJ_CAT}).fetchall()
        for r in rows: print(dict(r._mapping))

        # MSA pool snapshot for the TBL gen_arts
        hdr("MSA pool (ARS_MSA_VAR_ART) for HB52 RDC × TBL gen_arts in M_W_BERMUDA")
        rows = conn.execute(text("""
            WITH tbl_arts AS (
                SELECT DISTINCT GEN_ART_NUMBER, CLR, RDC
                FROM ARS_ALLOC_WORKING
                WHERE WERKS=:w AND MAJ_CAT=:m AND OPT_TYPE='TBL'
            )
            SELECT m.RDC, m.GEN_ART_NUMBER, m.CLR, m.SZ,
                   m.FNL_Q
            FROM ARS_MSA_VAR_ART m
            JOIN tbl_arts t ON m.RDC=t.RDC AND m.GEN_ART_NUMBER=t.GEN_ART_NUMBER AND m.CLR=t.CLR
            ORDER BY m.GEN_ART_NUMBER, m.CLR, m.SZ
        """), {"w": WERKS, "m": MAJ_CAT}).fetchall()
        print(f"row count = {len(rows)}")
        for r in rows[:30]:
            print(dict(r._mapping))

        # Aggregate
        hdr("Aggregated FNL_Q per TBL (GEN_ART,CLR) for HB52 RDC")
        rows = conn.execute(text("""
            WITH tbl_arts AS (
                SELECT DISTINCT GEN_ART_NUMBER, CLR, RDC
                FROM ARS_ALLOC_WORKING
                WHERE WERKS=:w AND MAJ_CAT=:m AND OPT_TYPE='TBL'
            )
            SELECT m.GEN_ART_NUMBER, m.CLR,
                   SUM(m.FNL_Q) AS total_fnl_q,
                   COUNT(*) AS sz_rows
            FROM ARS_MSA_VAR_ART m
            JOIN tbl_arts t ON m.RDC=t.RDC AND m.GEN_ART_NUMBER=t.GEN_ART_NUMBER AND m.CLR=t.CLR
            GROUP BY m.GEN_ART_NUMBER, m.CLR
            ORDER BY total_fnl_q DESC
        """), {"w": WERKS, "m": MAJ_CAT}).fetchall()
        for r in rows[:40]:
            print(dict(r._mapping))

        # Now check what the ALLOC_WORKING saw as pool - look at FNL_Q on the ship rows
        hdr("ALLOC_WORKING FNL_Q for TBL OPTs in HB52/M_W_BERMUDA")
        rows = conn.execute(text("""
            SELECT GEN_ART_NUMBER, CLR, SZ, OPT_TYPE,
                   FNL_Q, FNL_Q_REM, POOL_CONSUMED,
                   SHIP_QTY, SKIP_REASON
            FROM ARS_ALLOC_WORKING
            WHERE WERKS=:w AND MAJ_CAT=:m AND OPT_TYPE='TBL'
            ORDER BY GEN_ART_NUMBER, CLR, SZ
        """), {"w": WERKS, "m": MAJ_CAT}).fetchall()
        print(f"rows={len(rows)}")
        for r in rows[:40]:
            print(dict(r._mapping))

        # Compare TBC pool sample (TBCs are shipping)
        hdr("ALLOC_WORKING FNL_Q for TBC OPTs (working, for comparison)")
        rows = conn.execute(text("""
            SELECT GEN_ART_NUMBER, CLR, SZ, OPT_TYPE,
                   FNL_Q, FNL_Q_REM, POOL_CONSUMED,
                   SHIP_QTY, SKIP_REASON
            FROM ARS_ALLOC_WORKING
            WHERE WERKS=:w AND MAJ_CAT=:m AND OPT_TYPE='TBC'
            ORDER BY GEN_ART_NUMBER, CLR, SZ
        """), {"w": WERKS, "m": MAJ_CAT}).fetchall()
        for r in rows[:20]:
            print(dict(r._mapping))


if __name__ == "__main__":
    main()
