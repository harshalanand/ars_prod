import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
from sqlalchemy import text
from app.database.session import get_data_engine

eng = get_data_engine()
with eng.connect() as conn:
    print("DB:", conn.execute(text("SELECT @@SERVERNAME, DB_NAME()")).fetchone())

    print("\n--- table existence ---")
    for t in ("ARS_PEND_ALC", "ARS_BDC_HISTORY"):
        n = conn.execute(text(f"SELECT COUNT(*) FROM {t} WITH (NOLOCK)")).scalar()
        print(f"  {t}: {n} rows")

    print("\n--- ARS_PEND_ALC rowcount by (RDC, ST_CD) top 15 ---")
    rows = conn.execute(text("""
        SELECT TOP 15 RDC, ST_CD, COUNT(*) AS n,
               SUM(CASE WHEN IS_CLOSED=0 THEN 1 ELSE 0 END) AS open_n,
               SUM(BDC_QTY) AS sum_bdc, SUM(DO_QTY) AS sum_do, SUM(PEND_QTY) AS sum_pend
        FROM ARS_PEND_ALC WITH (NOLOCK)
        GROUP BY RDC, ST_CD
        ORDER BY n DESC
    """)).fetchall()
    for r in rows:
        print(f"  RDC={r[0]} ST={r[1]} rows={r[2]} open={r[3]} BDC={r[4]} DO={r[5]} PEND={r[6]}")

    print("\n--- HD24 / HB05 specifically ---")
    rows = conn.execute(text("""
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN IS_CLOSED=0 THEN 1 ELSE 0 END) AS open_n,
               SUM(BDC_QTY) AS sum_bdc, SUM(DO_QTY) AS sum_do, SUM(PEND_QTY) AS sum_pend
        FROM ARS_PEND_ALC WITH (NOLOCK)
        WHERE RDC = 'HD24' AND ST_CD = 'HB05'
    """)).fetchone()
    print(f"  rows={rows[0]} open={rows[1]} BDC={rows[2]} DO={rows[3]} PEND={rows[4]}")

    print("\n--- ARS_BDC_HISTORY rowcount by (RDC, ST_CD) top 15 ---")
    rows = conn.execute(text("""
        SELECT TOP 15 RDC, ST_CD, COUNT(*) AS n
        FROM ARS_BDC_HISTORY WITH (NOLOCK)
        GROUP BY RDC, ST_CD
        ORDER BY n DESC
    """)).fetchall()
    for r in rows:
        print(f"  RDC={r[0]} ST={r[1]} n={r[2]}")

    print("\n--- global STATUS distribution (full DB) ---")
    rows = conn.execute(text("""
        SELECT STATUS, COUNT(*) FROM ARS_BDC_HISTORY WITH (NOLOCK)
        GROUP BY STATUS ORDER BY 2 DESC
    """)).fetchall()
    for r in rows:
        print(f"  STATUS=[{r[0]!r}] count={r[1]}")
