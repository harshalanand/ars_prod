"""Look at previous LARGE sessions in ARS_ALLOC_HISTORY for comparison."""
from sqlalchemy import text
from app.database.session import DataSessionLocal


def main():
    with DataSessionLocal() as db:
        # Column check
        cols = db.execute(text("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'ARS_ALLOC_HISTORY'
            ORDER BY ORDINAL_POSITION
        """)).fetchall()
        print("ARS_ALLOC_HISTORY columns:")
        print(", ".join(c[0] for c in cols))

        # Latest large session
        rows = db.execute(text("""
            SELECT TOP 10 SESSION_ID, COUNT(*) AS n_rows,
                   SUM(CASE WHEN ALLOC_STATUS='ALLOCATED' THEN 1 ELSE 0 END) AS n_alloc,
                   SUM(CASE WHEN ALLOC_STATUS='SKIPPED'   THEN 1 ELSE 0 END) AS n_skip,
                   COUNT(DISTINCT WERKS) AS n_werks,
                   COUNT(DISTINCT MAJ_CAT) AS n_mj,
                   SUM(ISNULL(SHIP_QTY,0)) AS ship
            FROM rep_data.dbo.ARS_ALLOC_HISTORY
            GROUP BY SESSION_ID
            ORDER BY MAX(SESSION_ID) DESC
        """)).fetchall()
        print("\nRecent sessions:")
        for r in rows:
            print(" ", r)

        # Pick the most recent session that had > 1000 rows
        large = db.execute(text("""
            SELECT TOP 1 SESSION_ID
            FROM rep_data.dbo.ARS_ALLOC_HISTORY
            GROUP BY SESSION_ID
            HAVING COUNT(*) > 1000
            ORDER BY MAX(SESSION_ID) DESC
        """)).fetchone()
        if not large:
            print("\nNo large session found.")
            return
        sid = large[0]
        print(f"\n=== Inspecting large session {sid} ===")

        rows = db.execute(text("""
            SELECT TOP 25 SKIP_REASON, COUNT(*) AS n
            FROM rep_data.dbo.ARS_ALLOC_HISTORY
            WHERE SESSION_ID = :sid AND ALLOC_STATUS = 'SKIPPED'
            GROUP BY SKIP_REASON
            ORDER BY n DESC
        """), {"sid": sid}).fetchall()
        print("\nSKIP_REASON breakdown (large session):")
        for r in rows:
            print(" ", r)

        # How many distinct (WERKS, MAJ_CAT) with at least 1 ALLOCATED row?
        rows = db.execute(text("""
            SELECT COUNT(DISTINCT CONCAT(WERKS,'|',MAJ_CAT)) AS n_winning_keys,
                   (SELECT COUNT(DISTINCT CONCAT(WERKS,'|',MAJ_CAT))
                    FROM rep_data.dbo.ARS_ALLOC_HISTORY WHERE SESSION_ID=:sid) AS n_all_keys
            FROM rep_data.dbo.ARS_ALLOC_HISTORY
            WHERE SESSION_ID = :sid AND ALLOC_STATUS = 'ALLOCATED'
        """), {"sid": sid}).fetchall()
        print("\nWinning keys vs total keys:")
        for r in rows:
            print(" ", r)


if __name__ == "__main__":
    main()
