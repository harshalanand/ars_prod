"""Probe HN43 zero-allocation issue.

Steps:
  1. Find most-recent alloc table.
  2. Check HN43 presence / row count / shipped totals.
  3. If present but ships 0: walk gate logic.
  4. If absent: walk listing -> listed -> alloc to find drop point.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database.session import data_engine


def cols_of(conn, table):
    return {c[0].upper() for c in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :n"
    ), {"n": table}).fetchall()}


def main():
    with data_engine.connect() as conn:
        # 1) Pick most-recent alloc table (with WERKS + shipping column)
        rows = conn.execute(text("""
            SELECT t.name AS TBL, t.modify_date
            FROM sys.tables t
            WHERE (t.name LIKE '%ALLOC%' OR t.name LIKE '%alloc%')
            ORDER BY t.modify_date DESC
        """)).fetchall()
        print("=== Candidate alloc tables (top 10) ===")
        for r in rows[:10]:
            print(f"  {r[0]}  modified={r[1]}")

        target = None
        target_mod = None
        for r in rows:
            t_name = r[0]
            c = cols_of(conn, t_name)
            if "WERKS" in c and ("FNL_Q" in c or "OPT_SHIP" in c or "MJ_REQ" in c):
                target = t_name
                target_mod = r[1]
                break
        if not target:
            print("No alloc table with WERKS + shipping col found.")
            return
        cset = cols_of(conn, target)
        print(f"\n>>> Using table: {target}  modified={target_mod}")
        print(f"    Columns present (key): "
              f"FNL_Q={'FNL_Q' in cset}  OPT_SHIP={'OPT_SHIP' in cset}  "
              f"MJ_REQ={'MJ_REQ' in cset}  OPT_TYPE={'OPT_TYPE' in cset}  "
              f"OPT_MBQ={'OPT_MBQ' in cset}")

        # 2) HN43 presence
        ship_col = "OPT_SHIP" if "OPT_SHIP" in cset else ("FNL_Q" if "FNL_Q" in cset else None)
        n_hn43 = conn.execute(text(
            f"SELECT COUNT(*) FROM [{target}] WHERE WERKS = 'HN43'"
        )).scalar()
        n_all = conn.execute(text(f"SELECT COUNT(*) FROM [{target}]")).scalar()
        print(f"\n--- HN43 presence in {target} ---")
        print(f"  HN43 rows: {n_hn43:,}  /  total rows: {n_all:,}")

        if n_hn43 == 0:
            # HN43 not in alloc — walk upstream
            print("\n!!! HN43 is ABSENT from alloc table. Walking upstream...")
            walk_upstream(conn)
            return

        # 3) HN43 is present — check shipped totals + gates
        agg_cols = []
        if "FNL_Q" in cset: agg_cols.append("SUM(ISNULL(FNL_Q,0))     AS sum_FNL_Q")
        if "OPT_SHIP" in cset: agg_cols.append("SUM(ISNULL(OPT_SHIP,0))  AS sum_OPT_SHIP")
        if "MJ_REQ" in cset: agg_cols.append("SUM(ISNULL(MJ_REQ,0))    AS sum_MJ_REQ")
        if "OPT_MBQ" in cset: agg_cols.append("SUM(ISNULL(OPT_MBQ,0))   AS sum_OPT_MBQ")
        agg_sql = ", ".join(agg_cols) if agg_cols else "1 AS x"
        agg = conn.execute(text(
            f"SELECT {agg_sql} FROM [{target}] WHERE WERKS='HN43'"
        )).fetchone()
        print(f"  Aggregates for HN43:")
        for k, v in zip([c.split(" AS ")[1].strip() for c in agg_cols], agg):
            print(f"    {k} = {v}")

        # Distinct MAJ_CATs
        if "MAJ_CAT" in cset:
            mc = conn.execute(text(
                f"SELECT DISTINCT MAJ_CAT FROM [{target}] WHERE WERKS='HN43' ORDER BY MAJ_CAT"
            )).fetchall()
            print(f"  Distinct MAJ_CATs ({len(mc)}): {[r[0] for r in mc][:20]}{'...' if len(mc)>20 else ''}")

        # OPT_TYPE distribution
        if "OPT_TYPE" in cset:
            ot = conn.execute(text(
                f"SELECT ISNULL(OPT_TYPE,'<NULL>') AS OPT_TYPE, COUNT(*) AS N FROM [{target}] "
                f"WHERE WERKS='HN43' GROUP BY OPT_TYPE ORDER BY N DESC"
            )).fetchall()
            print(f"  OPT_TYPE distribution:")
            for r in ot:
                print(f"    {r[0]}: {r[1]:,}")

        # Sample rows
        ship_select = ship_col if ship_col else "NULL AS NO_SHIP_COL"
        # Build a comprehensive sample query
        sample_cols = ["WERKS", "MAJ_CAT"]
        for c in ["GEN_ART_NUMBER", "CLR", "OPT_TYPE", "OPT_MBQ", "MJ_REQ", "FNL_Q",
                  "OPT_SHIP", "MJ_FAB_MBQ", "MJ_MICRO_MVGR_MBQ"]:
            if c in cset:
                sample_cols.append(c)
        sample_sql = ", ".join(sample_cols)

        # Sample of HN43 rows where shipping=0
        if ship_col:
            zero_rows = conn.execute(text(
                f"SELECT TOP 8 {sample_sql} FROM [{target}] "
                f"WHERE WERKS='HN43' AND ISNULL({ship_col},0)=0 "
                f"ORDER BY MAJ_CAT"
            )).fetchall()
            print(f"\n  Sample HN43 rows where {ship_col}=0:")
            print("    " + " | ".join(sample_cols))
            for r in zero_rows:
                print("    " + " | ".join(str(v) for v in r))

            nonzero_rows = conn.execute(text(
                f"SELECT TOP 5 {sample_sql} FROM [{target}] "
                f"WHERE WERKS='HN43' AND ISNULL({ship_col},0) > 0"
            )).fetchall()
            print(f"\n  Sample HN43 rows where {ship_col}>0 (count={len(nonzero_rows)}):")
            if nonzero_rows:
                for r in nonzero_rows:
                    print("    " + " | ".join(str(v) for v in r))
            else:
                print("    NONE — HN43 ships exactly 0 across the board")

        # Compare HN43 vs other werks — is HN43 truly anomalous or is everyone 0?
        if ship_col:
            werks_summary = conn.execute(text(
                f"SELECT TOP 15 WERKS, COUNT(*) AS N, "
                f"SUM(ISNULL({ship_col},0)) AS sum_ship, "
                f"SUM(CASE WHEN ISNULL({ship_col},0)>0 THEN 1 ELSE 0 END) AS n_shipping "
                f"FROM [{target}] "
                f"GROUP BY WERKS ORDER BY sum_ship DESC"
            )).fetchall()
            print(f"\n  WERKS leaderboard by sum({ship_col}):")
            print("    WERKS | rows | sum_ship | n_shipping_rows")
            for r in werks_summary:
                print(f"    {r[0]:>6} | {r[1]:>6} | {r[2]:>8} | {r[3]:>6}")

            # Specifically HN43 rank
            hn43_rank = conn.execute(text(
                f"SELECT WERKS, COUNT(*) AS N, "
                f"SUM(ISNULL({ship_col},0)) AS sum_ship "
                f"FROM [{target}] WHERE WERKS='HN43' GROUP BY WERKS"
            )).fetchone()
            if hn43_rank:
                print(f"\n  HN43 line: rows={hn43_rank[1]}  sum({ship_col})={hn43_rank[2]}")


def walk_upstream(conn):
    """HN43 missing from alloc — check listing / listed / store_ranking / pend."""
    for tbl in ["listing", "Listing", "LISTING", "store_listing",
                "listed", "Listed", "LISTED",
                "store_ranking", "Store_Ranking", "STORE_RANKING",
                "MASTER_ALC_PEND", "alloc_header", "alloc_detail",
                "rep_data.dbo.alloc_detail"]:
        try:
            n = conn.execute(text(
                f"SELECT COUNT(*) FROM {tbl} WHERE WERKS='HN43'"
            )).scalar()
            print(f"  {tbl}: HN43 rows = {n:,}")
        except Exception as e:
            # table might not exist — that's fine
            pass


if __name__ == "__main__":
    main()
