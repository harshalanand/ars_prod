"""Confirm the scope of the latest runs."""
from sqlalchemy import text
from app.database.session import DataSessionLocal


def main():
    with DataSessionLocal() as db:
        cols = db.execute(text("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'ARS_LISTING_SESSIONS'
            ORDER BY ORDINAL_POSITION
        """)).fetchall()
        print("ARS_LISTING_SESSIONS columns:")
        print(", ".join(c[0] for c in cols))

        rows = db.execute(text("""
            SELECT TOP 10 SESSION_ID, STATUS, LISTED_OPTS, ALLOC_ROWS, SHIP_QTY_TOTAL,
                   STARTED_AT
            FROM rep_data.dbo.ARS_LISTING_SESSIONS
            ORDER BY STARTED_AT DESC
        """)).fetchall()
        print("\nRecent listing sessions:")
        for r in rows:
            print(" ", r)


if __name__ == "__main__":
    main()
