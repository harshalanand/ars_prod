"""
inspect_parked_diffs.py — verify ALLOC_QTY drift between two parked sessions.

Reads ARS_ALLOC_PARKED and reports:
  1. Sessions currently in the parked table (with row counts + totals)
  2. For the two latest sessions: row-level diff on
     (WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ) — SHIP_QTY / ALLOC_QTY / HOLD_QTY
  3. Top diverging keys
  4. OPT_PRIORITY_RANK diff for the same join key — confirms whether rank
     assignment itself is varying or whether only the waterfall pick order is.

Usage (from backend/):
  python scripts/inspect_parked_diffs.py
  python scripts/inspect_parked_diffs.py <session_id_1> <session_id_2>
"""
import os
import sys

# Make the backend package importable when run from scripts/
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from sqlalchemy import text  # noqa: E402

from app.database.session import get_data_engine  # noqa: E402


ALLOC_PARKED = "ARS_ALLOC_PARKED"
KEY_COLS = ["WERKS", "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "VAR_ART", "SZ"]


def fetchall(conn, sql, **params):
    return conn.execute(text(sql), params).fetchall()


def list_sessions(conn, limit=10):
    rows = fetchall(conn, f"""
        SELECT TOP ({limit})
               SESSION_ID,
               MIN(PARKED_AT)        AS parked_at,
               MAX(PARK_STATUS)      AS park_status,
               COUNT(*)              AS rows_n,
               SUM(CAST(ISNULL(SHIP_QTY,0)  AS FLOAT)) AS ship_qty_total,
               SUM(CAST(ISNULL(ALLOC_QTY,0) AS FLOAT)) AS alloc_qty_total,
               SUM(CAST(ISNULL(HOLD_QTY,0)  AS FLOAT)) AS hold_qty_total
        FROM {ALLOC_PARKED}
        GROUP BY SESSION_ID
        ORDER BY MIN(PARKED_AT) DESC
    """)
    return [dict(r._mapping) for r in rows]


def column_exists(conn, table, col):
    n = conn.execute(text("""
        SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = :t AND COLUMN_NAME = :c
    """), {"t": table, "c": col}).scalar() or 0
    return int(n) > 0


def compare_two_sessions(conn, sid1, sid2):
    print(f"\n=== Diff: {sid1}  vs  {sid2} ===")
    join_keys = " AND ".join([f"a.[{k}] = b.[{k}]" for k in KEY_COLS])
    on_a_null = " AND ".join([f"a.[{k}] IS NULL" for k in KEY_COLS])
    on_b_null = " AND ".join([f"b.[{k}] IS NULL" for k in KEY_COLS])

    qty_cols = ["SHIP_QTY", "ALLOC_QTY", "HOLD_QTY"]

    select_a = ",\n               ".join(
        [f"CAST(ISNULL(a.[{c}],0) AS FLOAT) AS a_{c.lower()}" for c in qty_cols]
    )
    select_b = ",\n               ".join(
        [f"CAST(ISNULL(b.[{c}],0) AS FLOAT) AS b_{c.lower()}" for c in qty_cols]
    )

    diff_sql = f"""
        SELECT
            {", ".join([f"ISNULL(a.[{k}], b.[{k}]) AS [{k}]" for k in KEY_COLS])},
            {select_a},
            {select_b},
            CAST(ISNULL(a.ALLOC_QTY,0) - ISNULL(b.ALLOC_QTY,0) AS FLOAT) AS d_alloc,
            CAST(ISNULL(a.SHIP_QTY,0)  - ISNULL(b.SHIP_QTY,0)  AS FLOAT) AS d_ship,
            CAST(ISNULL(a.HOLD_QTY,0)  - ISNULL(b.HOLD_QTY,0)  AS FLOAT) AS d_hold
        FROM (SELECT * FROM {ALLOC_PARKED} WHERE SESSION_ID = :s1) a
        FULL OUTER JOIN
             (SELECT * FROM {ALLOC_PARKED} WHERE SESSION_ID = :s2) b
          ON {join_keys}
        WHERE ABS(ISNULL(a.ALLOC_QTY,0) - ISNULL(b.ALLOC_QTY,0)) > 0.0001
           OR ABS(ISNULL(a.SHIP_QTY,0)  - ISNULL(b.SHIP_QTY,0))  > 0.0001
           OR ABS(ISNULL(a.HOLD_QTY,0)  - ISNULL(b.HOLD_QTY,0))  > 0.0001
    """

    rows = fetchall(conn, diff_sql, s1=sid1, s2=sid2)
    print(f"Diverging rows (any of SHIP/ALLOC/HOLD differs): {len(rows):,}")
    if not rows:
        print("Both runs produced identical SHIP/ALLOC/HOLD on every key — nothing to explain.")
        return

    # Aggregate impact
    sum_a_alloc = sum(r._mapping["a_alloc_qty"] for r in rows)
    sum_b_alloc = sum(r._mapping["b_alloc_qty"] for r in rows)
    sum_d_alloc = sum(r._mapping["d_alloc"] for r in rows)
    print(f"  Sum ALLOC_QTY on diverging rows  | {sid1}={sum_a_alloc:,.0f}  {sid2}={sum_b_alloc:,.0f}  d={sum_d_alloc:+,.0f}")

    # Top 20 by |d alloc|
    sorted_rows = sorted(rows, key=lambda r: abs(r._mapping["d_alloc"]), reverse=True)[:20]
    print(f"\nTop {len(sorted_rows)} rows by |d ALLOC_QTY|:")
    hdr = f"{'WERKS':<7} {'MAJ_CAT':<10} {'GEN_ART':<14} {'CLR':<8} {'VAR_ART':<14} {'SZ':<6} {'A_alloc':>9} {'B_alloc':>9} {'d':>9}"
    print(hdr)
    print("-" * len(hdr))
    for r in sorted_rows:
        m = r._mapping
        print(f"{str(m['WERKS'] or ''):<7} {str(m['MAJ_CAT'] or ''):<10} "
              f"{str(m['GEN_ART_NUMBER'] or ''):<14} {str(m['CLR'] or ''):<8} "
              f"{str(m['VAR_ART'] or ''):<14} {str(m['SZ'] or ''):<6} "
              f"{m['a_alloc_qty']:>9,.0f} {m['b_alloc_qty']:>9,.0f} {m['d_alloc']:>+9,.0f}")

    # Aggregate where the deltas live — per WERKS and per MAJ_CAT
    print("\nd ALLOC_QTY aggregated by WERKS (top 10):")
    werks_agg = {}
    for r in rows:
        m = r._mapping
        werks_agg.setdefault(m["WERKS"], 0.0)
        werks_agg[m["WERKS"]] += m["d_alloc"]
    for w, d in sorted(werks_agg.items(), key=lambda x: -abs(x[1]))[:10]:
        print(f"  {str(w):<8} d={d:+,.0f}")

    print("\nd ALLOC_QTY aggregated by MAJ_CAT (top 10):")
    mc_agg = {}
    for r in rows:
        m = r._mapping
        mc_agg.setdefault(m["MAJ_CAT"], 0.0)
        mc_agg[m["MAJ_CAT"]] += m["d_alloc"]
    for c, d in sorted(mc_agg.items(), key=lambda x: -abs(x[1]))[:10]:
        print(f"  {str(c):<12} d={d:+,.0f}")


def check_rank_drift(conn, sid1, sid2):
    """Did OPT_PRIORITY_RANK itself differ for the same (WERKS, MAJ_CAT, GEN_ART, CLR)
    between the two runs? If yes → Stage A is non-deterministic. If no → only the
    waterfall pick order varies."""
    if not column_exists(conn, ALLOC_PARKED, "OPT_PRIORITY_RANK"):
        print("\n[OPT_PRIORITY_RANK column not present in ARS_ALLOC_PARKED — skipping rank drift check]")
        return
    join_keys = " AND ".join([
        "a.[WERKS] = b.[WERKS]", "a.[MAJ_CAT] = b.[MAJ_CAT]",
        "a.[GEN_ART_NUMBER] = b.[GEN_ART_NUMBER]", "ISNULL(a.[CLR],'') = ISNULL(b.[CLR],'')"
    ])
    sql = f"""
        SELECT TOP 25
               a.WERKS, a.MAJ_CAT, a.GEN_ART_NUMBER, a.CLR,
               MAX(a.OPT_PRIORITY_RANK) AS rank_a,
               MAX(b.OPT_PRIORITY_RANK) AS rank_b
        FROM (SELECT DISTINCT WERKS,MAJ_CAT,GEN_ART_NUMBER,CLR,OPT_PRIORITY_RANK FROM {ALLOC_PARKED} WHERE SESSION_ID = :s1) a
        JOIN (SELECT DISTINCT WERKS,MAJ_CAT,GEN_ART_NUMBER,CLR,OPT_PRIORITY_RANK FROM {ALLOC_PARKED} WHERE SESSION_ID = :s2) b
          ON {join_keys}
        WHERE ISNULL(a.OPT_PRIORITY_RANK,-1) <> ISNULL(b.OPT_PRIORITY_RANK,-1)
        GROUP BY a.WERKS, a.MAJ_CAT, a.GEN_ART_NUMBER, a.CLR
    """
    rows = fetchall(conn, sql, s1=sid1, s2=sid2)
    print(f"\nKeys whose OPT_PRIORITY_RANK differed across runs: {len(rows)} (top 25 shown)")
    if rows:
        print(f"{'WERKS':<7} {'MAJ_CAT':<10} {'GEN_ART':<14} {'CLR':<8} {'rank_A':>8} {'rank_B':>8}")
        for r in rows:
            m = r._mapping
            print(f"{str(m['WERKS'] or ''):<7} {str(m['MAJ_CAT'] or ''):<10} "
                  f"{str(m['GEN_ART_NUMBER'] or ''):<14} {str(m['CLR'] or ''):<8} "
                  f"{m['rank_a']:>8} {m['rank_b']:>8}")
        print("\n→ Rank assignment is NOT deterministic across runs (Stage A SQL window has no stable tie-breaker).")
    else:
        print("→ OPT_PRIORITY_RANK is identical for shared keys — drift is from waterfall pick order, not rank assignment.")


def main():
    eng = get_data_engine()
    with eng.connect() as conn:
        # Sanity
        try:
            db_name = conn.execute(text("SELECT DB_NAME()")).scalar()
        except Exception as e:
            print(f"Cannot reach data DB: {e}")
            sys.exit(2)
        print(f"Connected to data DB: {db_name}")

        # Has the parked table?
        if not column_exists(conn, ALLOC_PARKED, "SESSION_ID"):
            print(f"Table {ALLOC_PARKED} not found (or no SESSION_ID column). Nothing to compare.")
            sys.exit(0)

        sessions = list_sessions(conn, 20)
        if not sessions:
            print(f"{ALLOC_PARKED} is empty. Park two runs first, then re-run this script.")
            sys.exit(0)

        print(f"\nSessions in {ALLOC_PARKED} (latest first):")
        print(f"{'SESSION_ID':<40} {'parked_at':<22} {'status':<10} {'rows':>10} {'ship':>12} {'alloc':>12} {'hold':>10}")
        for s in sessions:
            print(f"{str(s['SESSION_ID']):<40} {str(s['parked_at'])[:22]:<22} "
                  f"{str(s['park_status']):<10} {s['rows_n']:>10,} "
                  f"{s['ship_qty_total']:>12,.0f} {s['alloc_qty_total']:>12,.0f} {s['hold_qty_total']:>10,.0f}")

        # Pick two sessions to compare: CLI args take priority, else the two latest
        if len(sys.argv) >= 3:
            sid1, sid2 = sys.argv[1], sys.argv[2]
        else:
            if len(sessions) < 2:
                print("\nNeed at least 2 parked sessions to diff. Park another run, then re-run.")
                sys.exit(0)
            sid1, sid2 = sessions[0]["SESSION_ID"], sessions[1]["SESSION_ID"]
            print(f"\n(no CLI args — comparing latest two sessions)")

        compare_two_sessions(conn, sid1, sid2)
        check_rank_drift(conn, sid1, sid2)


if __name__ == "__main__":
    main()
