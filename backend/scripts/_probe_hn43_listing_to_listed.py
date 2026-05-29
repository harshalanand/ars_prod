"""HN43 drops between ARS_LISTING_WORKING (159 rows) and ARS_LISTED_OPT (0 rows).
Diagnose why."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database.session import data_engine


def cols(conn, t):
    return [c[0] for c in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME=:n ORDER BY ORDINAL_POSITION"
    ), {"n": t}).fetchall()]


def main():
    with data_engine.connect() as conn:
        # --- ARS_STORE_RANKING for HN43 ---
        print("=== ARS_STORE_RANKING for HN43 ===")
        sr_cols = cols(conn, "ARS_STORE_RANKING")
        print(f"  columns: {sr_cols}")
        row = conn.execute(text(
            "SELECT * FROM ARS_STORE_RANKING WHERE WERKS='HN43'"
        )).fetchone()
        if row:
            print(f"  data: {dict(zip(sr_cols, row))}")

        # other werks for comparison
        nonzero = conn.execute(text("""
            SELECT TOP 5 * FROM ARS_STORE_RANKING WHERE WERKS != 'HN43'
        """)).fetchall()
        print("  Sample non-HN43 rows:")
        for r in nonzero:
            print(f"    {dict(zip(sr_cols, r))}")

        # --- ARS_LISTING_WORKING for HN43 ---
        print("\n=== ARS_LISTING_WORKING for HN43 (sample 5) ===")
        lw_cols = cols(conn, "ARS_LISTING_WORKING")
        print(f"  columns ({len(lw_cols)}): {lw_cols}")
        sample = conn.execute(text(
            "SELECT TOP 5 * FROM ARS_LISTING_WORKING WHERE WERKS='HN43'"
        )).fetchall()
        for r in sample:
            d = dict(zip(lw_cols, r))
            # truncate massive values
            print("    " + ", ".join(f"{k}={v}" for k, v in d.items() if v not in (None, "", 0)))

        # --- ARS_LISTED_OPT columns ---
        print("\n=== ARS_LISTED_OPT structure & summary ===")
        lo_cols = cols(conn, "ARS_LISTED_OPT")
        print(f"  columns ({len(lo_cols)}): {lo_cols}")
        total = conn.execute(text("SELECT COUNT(*) FROM ARS_LISTED_OPT")).scalar()
        distinct_werks = conn.execute(text(
            "SELECT COUNT(DISTINCT WERKS) FROM ARS_LISTED_OPT"
        )).scalar()
        print(f"  total rows: {total:,}  distinct WERKS: {distinct_werks}")

        # Which WERKS ARE present in LISTED_OPT?
        werks_in_listed = conn.execute(text("""
            SELECT TOP 20 WERKS, COUNT(*) AS N
            FROM ARS_LISTED_OPT GROUP BY WERKS ORDER BY N DESC
        """)).fetchall()
        print(f"  Top WERKS in ARS_LISTED_OPT:")
        for r in werks_in_listed:
            print(f"    {r[0]}: {r[1]:,}")

        # Distinct WERKS in LISTING_WORKING
        werks_in_listing = conn.execute(text("""
            SELECT WERKS, COUNT(*) AS N
            FROM ARS_LISTING_WORKING GROUP BY WERKS ORDER BY N DESC
        """)).fetchall()
        print(f"\n  WERKS in ARS_LISTING_WORKING (count={len(werks_in_listing)}):")
        for r in werks_in_listing:
            mark = " <-- HN43" if r[0] == "HN43" else ""
            print(f"    {r[0]}: {r[1]:,}{mark}")

        # Set diff: WERKS in LISTING but not in LISTED_OPT
        missing = conn.execute(text("""
            SELECT DISTINCT lw.WERKS, COUNT(*) AS N
            FROM ARS_LISTING_WORKING lw
            WHERE NOT EXISTS (
                SELECT 1 FROM ARS_LISTED_OPT lo WHERE lo.WERKS = lw.WERKS
            )
            GROUP BY lw.WERKS
        """)).fetchall()
        print(f"\n  WERKS in LISTING_WORKING but absent from LISTED_OPT ({len(missing)}):")
        for r in missing:
            print(f"    {r[0]}: {r[1]:,} listing rows missing in listed_opt")

        # --- Check store_ranking for these missing WERKS ---
        print("\n=== ARS_STORE_RANKING — are the missing WERKS ranked? ===")
        for r in missing:
            w = r[0]
            sr_row = conn.execute(text(
                "SELECT * FROM ARS_STORE_RANKING WHERE WERKS=:w"
            ), {"w": w}).fetchone()
            if sr_row:
                d = dict(zip(sr_cols, sr_row))
                print(f"    {w}: {d}")
            else:
                print(f"    {w}: <NOT IN STORE_RANKING>")


if __name__ == "__main__":
    main()
