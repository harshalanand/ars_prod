"""Sample the virtual pool from the most-recent alloc table."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database.session import data_engine


def main():
    with data_engine.connect() as conn:
        # 1) Find most-recent alloc-style table
        rows = conn.execute(text("""
            SELECT t.name AS TBL, t.modify_date
            FROM sys.tables t
            WHERE (t.name LIKE '%ALLOC%' OR t.name LIKE '%alloc%')
            ORDER BY t.modify_date DESC
        """)).fetchall()
        print("=== Candidate tables (most-recent first) ===")
        for r in rows[:25]:
            print(f"  {r[0]}  modified={r[1]}")

        # 2) Pick the first one that actually has the pool key columns AND FNL_Q
        target = None
        for r in rows:
            t_name = r[0]
            cols = {c[0].upper() for c in conn.execute(text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :n"
            ), {"n": t_name}).fetchall()}
            needed = {"RDC", "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "VAR_ART", "SZ", "FNL_Q"}
            if needed.issubset(cols):
                target = t_name
                has_rem = "FNL_Q_REM" in cols
                print(f"\n>>> Using table: {target}  (FNL_Q_REM present: {has_rem})")
                break
        if not target:
            print("\nNo alloc table has the full pool-key set. Falling back to MSA tables.")
            return

        # 3) Pool count
        cnt = conn.execute(text(f"""
            SELECT COUNT(*) FROM (
                SELECT DISTINCT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ
                FROM [{target}]
            ) X
        """)).scalar()
        print(f"\nTOTAL DISTINCT POOL KEYS: {cnt:,}")

        # 4) Sample 20 rows pivoted to pool grain
        rem_expr = "MAX(ISNULL(FNL_Q_REM, 0))" if has_rem else "MAX(ISNULL(FNL_Q, 0))"
        sample = conn.execute(text(f"""
            SELECT TOP 20
              RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
              MAX(ISNULL(FNL_Q, 0)) AS FNL_Q_ORIG,
              {rem_expr} AS FNL_Q_REM
            FROM [{target}]
            GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ
            ORDER BY MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ
        """)).fetchall()

        print("\n=== Sample pool rows ===")
        print("| RDC | MAJ_CAT | GEN_ART_NUMBER | CLR | VAR_ART | SZ | FNL_Q_ORIG | FNL_Q_REM |")
        print("|-----|---------|----------------|-----|---------|----|------------|-----------|")
        for r in sample:
            print(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]} | {r[7]} |")


if __name__ == "__main__":
    main()
