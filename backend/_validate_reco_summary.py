"""Ad-hoc validation for /reco-summary query rewrite."""
from sqlalchemy import text
from app.database.session import get_data_engine

NEW_BY_MODE = """
;WITH open_keys AS (
    SELECT DISTINCT h.RDC, ISNULL(h.ST_CD,'') AS ST_CD, h.ARTICLE_NUMBER
    FROM ARS_BDC_HISTORY h WITH (NOLOCK)
    WHERE h.STATUS = 'OPEN'
)
SELECT ISNULL(P.ALLOC_MODE,'AUTO') AS mode,
       SUM(P.ALLOC_QTY) AS alloc_qty, SUM(P.BDC_QTY) AS bdc_qty,
       SUM(P.DO_QTY) AS do_qty, SUM(P.PEND_QTY) AS pend_qty,
       SUM(CASE WHEN P.PEND_QTY > 0 AND ok.RDC IS NULL THEN P.PEND_QTY ELSE 0 END) AS pending_bdc_qty,
       COUNT(*) AS rows_n
FROM ARS_PEND_ALC P WITH (NOLOCK)
LEFT JOIN open_keys ok
  ON  ok.RDC = P.RDC
 AND ok.ST_CD = ISNULL(P.ST_CD,'')
 AND ok.ARTICLE_NUMBER = P.ARTICLE_NUMBER
WHERE P.IS_CLOSED = 0
GROUP BY ISNULL(P.ALLOC_MODE,'AUTO')
"""

NEW_BY_RDC = """
;WITH open_keys AS (
    SELECT DISTINCT h.RDC, ISNULL(h.ST_CD,'') AS ST_CD, h.ARTICLE_NUMBER
    FROM ARS_BDC_HISTORY h WITH (NOLOCK)
    WHERE h.STATUS = 'OPEN'
)
SELECT P.RDC AS rdc,
       SUM(P.ALLOC_QTY) AS alloc_qty, SUM(P.BDC_QTY) AS bdc_qty,
       SUM(P.DO_QTY) AS do_qty, SUM(P.PEND_QTY) AS pend_qty,
       SUM(CASE WHEN P.PEND_QTY > 0 AND ok.RDC IS NULL THEN P.PEND_QTY ELSE 0 END) AS pending_bdc_qty,
       COUNT(*) AS rows_n
FROM ARS_PEND_ALC P WITH (NOLOCK)
LEFT JOIN open_keys ok
  ON  ok.RDC = P.RDC
 AND ok.ST_CD = ISNULL(P.ST_CD,'')
 AND ok.ARTICLE_NUMBER = P.ARTICLE_NUMBER
WHERE P.IS_CLOSED = 0
GROUP BY P.RDC
"""

OLD_BY_MODE = """
SELECT ISNULL(P.ALLOC_MODE,'AUTO') AS mode,
       SUM(P.PEND_QTY) AS pend_qty_total,
       SUM(CASE WHEN P.PEND_QTY > 0 AND NOT EXISTS (
           SELECT 1 FROM ARS_BDC_HISTORY h WITH (NOLOCK)
           WHERE h.RDC = P.RDC AND ISNULL(h.ST_CD,'') = ISNULL(P.ST_CD,'')
             AND h.ARTICLE_NUMBER = P.ARTICLE_NUMBER AND h.STATUS = 'OPEN'
       ) THEN P.PEND_QTY ELSE 0 END) AS pending_bdc_old
FROM ARS_PEND_ALC P WITH (NOLOCK)
WHERE P.IS_CLOSED = 0
GROUP BY ISNULL(P.ALLOC_MODE,'AUTO')
"""

eng = get_data_engine()
with eng.connect() as c:
    print("=== NEW by_mode ===")
    rows = c.execute(text(NEW_BY_MODE)).fetchall()
    print(f"rows returned: {len(rows)}")
    for r in rows:
        print(dict(r._mapping))

    print("\n=== NEW by_rdc ===")
    rows_rdc = c.execute(text(NEW_BY_RDC)).fetchall()
    print(f"rows returned: {len(rows_rdc)}")
    for r in rows_rdc[:10]:
        print(dict(r._mapping))

    print("\n=== OLD by_mode (NOT EXISTS) ===")
    try:
        old_rows = c.execute(text(OLD_BY_MODE)).fetchall()
        print(f"rows returned: {len(old_rows)}")
        old_map = {r._mapping["mode"]: r._mapping["pending_bdc_old"] for r in old_rows}
        new_map = {r._mapping["mode"]: r._mapping["pending_bdc_qty"] for r in rows}
        print("\nMode | new pending_bdc_qty | old pending_bdc_old | equal?")
        all_modes = set(old_map) | set(new_map)
        all_eq = True
        for m in sorted(all_modes, key=str):
            n = new_map.get(m)
            o = old_map.get(m)
            eq = (n == o)
            all_eq = all_eq and eq
            print(f"  {m!r:>10} | {n!s:>20} | {o!s:>20} | {eq}")
        print(f"\nALL EQUAL: {all_eq}")
    except Exception as e:
        print(f"OLD form errored (expected on prod): {type(e).__name__}: {str(e)[:200]}")
