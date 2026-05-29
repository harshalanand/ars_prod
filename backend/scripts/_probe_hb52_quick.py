"""Quick sanity check - alloc rows present?"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database.session import data_engine


def main():
    with data_engine.connect() as conn:
        for t in ("ARS_ALLOC_WORKING", "ARS_LISTING_WORKING"):
            r = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).fetchone()
            print(f"{t}: total rows = {r[0]}")

        for t in ("ARS_ALLOC_WORKING", "ARS_LISTING_WORKING"):
            r = conn.execute(text(f"""
                SELECT COUNT(*) FROM {t}
                WHERE WERKS='HB52' AND MAJ_CAT='M_W_BERMUDA'
            """)).fetchone()
            print(f"{t}: HB52/M_W_BERMUDA rows = {r[0]}")

        print("\nDistinct GEN_ART_NUMBER (top 5) in ALLOC_WORKING for HB52/M_W_BERMUDA:")
        rows = conn.execute(text("""
            SELECT TOP 5 GEN_ART_NUMBER, CLR, OPT_TYPE, COUNT(*) c
            FROM ARS_ALLOC_WORKING
            WHERE WERKS='HB52' AND MAJ_CAT='M_W_BERMUDA'
            GROUP BY GEN_ART_NUMBER, CLR, OPT_TYPE
            ORDER BY c DESC
        """)).fetchall()
        for r in rows: print(dict(r._mapping))


if __name__ == "__main__":
    main()
