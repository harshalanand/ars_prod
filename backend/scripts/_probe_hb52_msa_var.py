"""Check MSA_VAR_ART columns & whether VAR_ART column exists."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database.session import data_engine

WERKS = "HB52"
MAJ_CAT = "M_W_BERMUDA"


def main():
    with data_engine.connect() as conn:
        # 1. schema
        print("\n=== ARS_MSA_VAR_ART columns ===")
        rows = conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME='ARS_MSA_VAR_ART' ORDER BY ORDINAL_POSITION"
        )).fetchall()
        for r in rows:
            print(" ", r[0])

        # 2. one TBL example
        print("\n=== MSA_VAR_ART sample for 1115095397/L_GRY @ DW01 ===")
        rows = conn.execute(text(
            "SELECT TOP 20 * FROM ARS_MSA_VAR_ART "
            "WHERE RDC='DW01' AND GEN_ART_NUMBER=1115095397 AND CLR='L_GRY'"
        )).fetchall()
        for r in rows:
            print(dict(r._mapping))

        # 3. ALLOC_WORKING rows for same OPT — what VAR_ART do they use?
        print("\n=== ALLOC_WORKING rows for 1115095397/L_GRY TBL @ HB52 ===")
        rows = conn.execute(text("""
            SELECT VAR_ART, SZ, FNL_Q, FNL_Q_REM, POOL_CONSUMED,
                   SHIP_QTY, ALLOC_STATUS, SKIP_REASON
            FROM ARS_ALLOC_WORKING
            WHERE WERKS='HB52' AND MAJ_CAT='M_W_BERMUDA'
              AND GEN_ART_NUMBER=1115095397 AND CLR='L_GRY' AND OPT_TYPE='TBL'
            ORDER BY VAR_ART, SZ
        """)).fetchall()
        for r in rows:
            print(dict(r._mapping))

        # 4. Same OPT but TBC line for HB52 - which VAR_ART?  Confirms VAR_ART resolution upstream.
        # Compare a TBL OPT that did ship: TBC version of L_GRY ships per probe1.
        print("\n=== Sample MSA_VAR_ART rows showing VAR_ART column values (if any) for that gen_art ===")
        rows = conn.execute(text(
            "SELECT DISTINCT VAR_ART FROM ARS_MSA_VAR_ART "
            "WHERE GEN_ART_NUMBER=1115095397 AND CLR='L_GRY' AND RDC='DW01'"
        )).fetchall()
        for r in rows:
            print(dict(r._mapping))


if __name__ == "__main__":
    main()
