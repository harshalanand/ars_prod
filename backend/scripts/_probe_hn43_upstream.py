"""Find where HN43 is dropped between listing -> listed -> alloc."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database.session import data_engine


def main():
    with data_engine.connect() as conn:
        # Find any table that has WERKS column AND is recently touched
        rows = conn.execute(text("""
            SELECT t.name, t.modify_date
            FROM sys.tables t
            WHERE EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS c
                WHERE c.TABLE_NAME = t.name AND c.COLUMN_NAME = 'WERKS'
            )
            ORDER BY t.modify_date DESC
        """)).fetchall()
        print("=== Tables with WERKS column (most-recent first, top 30) ===")
        for r in rows[:30]:
            n = conn.execute(text(
                f"SELECT COUNT(*) FROM [{r[0]}] WHERE WERKS='HN43'"
            )).scalar()
            total = conn.execute(text(f"SELECT COUNT(*) FROM [{r[0]}]")).scalar()
            print(f"  {r[0]:<45} modified={r[1]}  total={total:>10,}  HN43={n:,}")

        # Check store_ranking / store_master / werks master
        print("\n=== Looking for WERKS master / store-related tables ===")
        master_tables = conn.execute(text("""
            SELECT name FROM sys.tables
            WHERE name LIKE '%store%' OR name LIKE '%werks%' OR name LIKE '%STORE%'
               OR name LIKE '%rank%' OR name LIKE '%RANK%'
               OR name LIKE '%listing%' OR name LIKE '%LISTING%'
               OR name LIKE '%listed%' OR name LIKE '%LISTED%'
            ORDER BY name
        """)).fetchall()
        for r in master_tables:
            try:
                cols = {c[0].upper() for c in conn.execute(text(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=:n"
                ), {"n": r[0]}).fetchall()}
                if "WERKS" in cols:
                    n = conn.execute(text(
                        f"SELECT COUNT(*) FROM [{r[0]}] WHERE WERKS='HN43'"
                    )).scalar()
                    total = conn.execute(text(f"SELECT COUNT(*) FROM [{r[0]}]")).scalar()
                    print(f"  {r[0]:<45} total={total:>10,}  HN43={n:,}")
                else:
                    print(f"  {r[0]:<45}  (no WERKS column)")
            except Exception as e:
                print(f"  {r[0]:<45}  ERROR: {e}")


if __name__ == "__main__":
    main()
