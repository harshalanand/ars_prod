"""Diagnostic script for inspecting the HOLD-tracking → MSA sync pipeline.
Reads database connection from app_settings.json (UI-managed) via the
central engine, so it always targets the same server the running app uses.

Usage:
    python check_hold_sync.py                # full report
    python check_hold_sync.py --bootstrap    # also run bootstrap_msa_hold_sync
"""
import sys
from sqlalchemy import text

from app.database.session import get_data_engine
from app.core.config import get_settings

eng = get_data_engine()
s = get_settings()
db_cfg = s._db()

print("=" * 78)
print(f"DB target → server={db_cfg['server']}  data_db={db_cfg['data_database']}")
print("=" * 78)

with eng.connect() as conn:
    actual = conn.execute(text("SELECT @@SERVERNAME, DB_NAME()")).fetchone()
    print(f"Connected   → server={actual[0]}  db={actual[1]}\n")

    def cols_of(table):
        rows = conn.execute(text("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION
        """), {"t": table}).fetchall()
        return [r[0] for r in rows]

    print("─" * 78)
    print("1. ARS_NL_TBL_HOLD_TRACKING schema (RDC column status)")
    print("─" * 78)
    h_cols = cols_of("ARS_NL_TBL_HOLD_TRACKING")
    if not h_cols:
        print("   TABLE DOES NOT EXIST — listing.py Part 8.6 will create on first run")
    else:
        print(f"   columns ({len(h_cols)}):")
        for c in h_cols:
            mark = "  ← NEW" if c == "RDC" else ""
            print(f"     {c}{mark}")
        print(f"   has RDC column? {'✓ YES' if 'RDC' in h_cols else '✗ NO'}")

        # Row counts + RDC null/non-null mix
        if "RDC" in h_cols:
            r = conn.execute(text("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN ISNULL(IS_CLOSED,0)=0 THEN 1 ELSE 0 END) AS open_rows,
                       SUM(CASE WHEN ISNULL(RDC,'')='' THEN 1 ELSE 0 END) AS rdc_null,
                       SUM(CAST(HOLD_REM AS FLOAT)) AS sum_hold_rem,
                       SUM(CASE WHEN ISNULL(IS_CLOSED,0)=0
                                THEN CAST(HOLD_REM AS FLOAT) ELSE 0 END) AS sum_open_hold
                FROM ARS_NL_TBL_HOLD_TRACKING
            """)).fetchone()
            print(f"\n   total rows:       {r[0]}")
            print(f"   open rows:        {r[1]}")
            print(f"   rows with RDC NULL: {r[2]}  (will backfill on next listing run)")
            print(f"   sum HOLD_REM (all):  {float(r[3] or 0):,.0f}")
            print(f"   sum HOLD_REM (open): {float(r[4] or 0):,.0f}")
        else:
            r = conn.execute(text("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN ISNULL(IS_CLOSED,0)=0 THEN 1 ELSE 0 END) AS open_rows,
                       SUM(CAST(HOLD_REM AS FLOAT)) AS sum_hold_rem
                FROM ARS_NL_TBL_HOLD_TRACKING
            """)).fetchone()
            print(f"\n   total rows: {r[0]}  open_rows: {r[1]}  sum_hold_rem: {float(r[2] or 0):,.0f}")

    print()
    print("─" * 78)
    print("2. ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT schema")
    print("─" * 78)
    s_cols = cols_of("ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT")
    if not s_cols:
        print("   TABLE DOES NOT EXIST — created on first listing generate")
    else:
        print(f"   has RDC column? {'✓ YES' if 'RDC' in s_cols else '✗ NO'}")

    print()
    print("─" * 78)
    print("3. Master_ALC_INPUT_ST_MASTER (RDC mapping source)")
    print("─" * 78)
    m_cols = cols_of("Master_ALC_INPUT_ST_MASTER")
    if not m_cols:
        print("   TABLE DOES NOT EXIST")
    else:
        relevant = [c for c in m_cols if c.upper() in
                    {"ST_CD", "RDC", "WAREHOUSE", "HUB", "WH_CD", "WERKS"}]
        print(f"   relevant columns: {relevant}")

    print()
    print("─" * 78)
    print("4. ARS_MSA_TOTAL hold/pend/fnl invariant check")
    print("─" * 78)
    msa_cols = cols_of("ARS_MSA_TOTAL")
    if not msa_cols:
        print("   ARS_MSA_TOTAL not found")
    else:
        r = conn.execute(text("""
            SELECT
                COUNT(*) AS rows,
                SUM(CAST(STK_QTY  AS FLOAT)) AS stk,
                SUM(CAST(PEND_QTY AS FLOAT)) AS pend,
                SUM(CAST(HOLD_QTY AS FLOAT)) AS hold,
                SUM(CAST(FNL_Q    AS FLOAT)) AS fnl
            FROM ARS_MSA_TOTAL
        """)).fetchone()
        rows, stk, pend, hold, fnl = r
        expected_max = max((stk or 0) - (pend or 0) - (hold or 0), 0)
        gap = (fnl or 0) - expected_max
        print(f"   rows:    {rows}")
        print(f"   STK:     {float(stk or 0):>15,.0f}")
        print(f"   PEND:    {float(pend or 0):>15,.0f}")
        print(f"   HOLD:    {float(hold or 0):>15,.0f}")
        print(f"   FNL_Q:   {float(fnl or 0):>15,.0f}")
        print(f"   STK − PEND − HOLD: {expected_max:>15,.0f}  (per-row max(...,0) summed may differ)")

    print()
    print("─" * 78)
    print("5. HOLD truth from tracking vs. MSA (per (RDC, ARTICLE))")
    print("─" * 78)
    # If RDC column exists, prefer it. Else join store master.
    if "RDC" in (cols_of("ARS_NL_TBL_HOLD_TRACKING") or []):
        truth_sql = """
            SELECT TOP 5 'GAP' AS what,
                   T.RDC, T.ARTICLE_NUMBER,
                   T.HOLD_QTY AS msa_says,
                   ISNULL(H.qty, 0) AS truth,
                   T.HOLD_QTY - ISNULL(H.qty, 0) AS gap
            FROM ARS_MSA_TOTAL T
            LEFT JOIN (
                SELECT COALESCE(NULLIF(RDC,''), 'NULL') AS rdc,
                       CAST(VAR_ART AS NVARCHAR(30)) AS art,
                       SUM(CAST(HOLD_REM AS FLOAT)) AS qty
                FROM ARS_NL_TBL_HOLD_TRACKING
                WHERE ISNULL(IS_CLOSED,0)=0 AND ISNULL(HOLD_REM,0) > 0
                GROUP BY COALESCE(NULLIF(RDC,''), 'NULL'), CAST(VAR_ART AS NVARCHAR(30))
            ) H ON H.rdc = T.RDC AND H.art = T.ARTICLE_NUMBER
            WHERE ABS(T.HOLD_QTY - ISNULL(H.qty, 0)) > 0.01
            ORDER BY ABS(T.HOLD_QTY - ISNULL(H.qty, 0)) DESC
        """
    else:
        truth_sql = """
            SELECT TOP 5 'GAP' AS what,
                   T.RDC, T.ARTICLE_NUMBER,
                   T.HOLD_QTY AS msa_says,
                   ISNULL(H.qty, 0) AS truth,
                   T.HOLD_QTY - ISNULL(H.qty, 0) AS gap
            FROM ARS_MSA_TOTAL T
            LEFT JOIN (
                SELECT SM.RDC AS rdc,
                       CAST(H.VAR_ART AS NVARCHAR(30)) AS art,
                       SUM(CAST(H.HOLD_REM AS FLOAT)) AS qty
                FROM ARS_NL_TBL_HOLD_TRACKING H
                INNER JOIN Master_ALC_INPUT_ST_MASTER SM ON SM.ST_CD = H.WERKS
                WHERE ISNULL(H.IS_CLOSED,0)=0 AND ISNULL(H.HOLD_REM,0) > 0
                GROUP BY SM.RDC, CAST(H.VAR_ART AS NVARCHAR(30))
            ) H ON H.rdc = T.RDC AND H.art = T.ARTICLE_NUMBER
            WHERE ABS(T.HOLD_QTY - ISNULL(H.qty, 0)) > 0.01
            ORDER BY ABS(T.HOLD_QTY - ISNULL(H.qty, 0)) DESC
        """
    try:
        rows = conn.execute(text(truth_sql)).fetchall()
        if not rows:
            print("   ✓ NO DRIFT — MSA HOLD_QTY matches ARS_NL_TBL_HOLD_TRACKING for every (RDC, ARTICLE)")
        else:
            print(f"   ✗ {len(rows)} keys with DRIFT (top 5 by gap size):")
            print(f"   {'RDC':<8} {'ARTICLE':<14} {'MSA says':>12} {'truth':>12} {'gap':>10}")
            for r in rows:
                print(f"   {str(r[1]):<8} {str(r[2]):<14} {float(r[3] or 0):>12,.0f} {float(r[4] or 0):>12,.0f} {float(r[5] or 0):>10,.0f}")
    except Exception as e:
        print(f"   query failed (most likely tables not yet created): {e}")

# Optional bootstrap
if "--bootstrap" in sys.argv:
    print()
    print("=" * 78)
    print("Running bootstrap_msa_hold_sync ...")
    print("=" * 78)
    from sqlalchemy.orm import sessionmaker
    from app.services.pend_alc_service import bootstrap_msa_hold_sync
    Session = sessionmaker(bind=eng)
    db = Session()
    try:
        result = bootstrap_msa_hold_sync(db)
        print(f"   result: {result}")
    finally:
        db.close()

print()
print("Done.")
