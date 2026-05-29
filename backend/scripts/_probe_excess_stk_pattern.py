"""Probe: verify new EXCESS_STK sourcing pattern against ARS_LISTING_PARKED."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from app.database.session import DataSessionLocal

SESSION_ID = '20260528_110819_156'

Q1 = """
WITH excess AS (
    SELECT WERKS, GEN_ART_NUMBER, ISNULL(CLR, '') AS CLR, RDC,
           SUM(ISNULL(EXCESS_STK, 0)) AS excess_stk
    FROM ARS_LISTING_PARKED WITH (NOLOCK)
    WHERE SESSION_ID = :sid
    GROUP BY WERKS, GEN_ART_NUMBER, ISNULL(CLR, ''), RDC
)
SELECT TOP 5 L.MAJ_CAT, L.RDC,
       SUM(ISNULL(L.ALLOC_QTY, 0))   AS alloc_qty,
       SUM(ISNULL(e.excess_stk, 0))  AS excess_stk_total
FROM ARS_LISTING_WORKING_PARKED L WITH (NOLOCK)
LEFT JOIN excess e
       ON e.WERKS = L.WERKS
      AND e.GEN_ART_NUMBER = L.GEN_ART_NUMBER
      AND e.CLR = ISNULL(L.CLR, '')
      AND e.RDC = L.RDC
WHERE L.SESSION_ID = :sid AND L.WERKS = 'HB05'
GROUP BY L.MAJ_CAT, L.RDC
ORDER BY excess_stk_total DESC
"""

Q2 = """
SELECT TOP 5 MAJ_CAT, RDC, SUM(ISNULL(EXCESS_STK, 0)) AS expected
FROM ARS_LISTING_PARKED WITH (NOLOCK)
WHERE SESSION_ID = :sid AND WERKS = 'HB05'
GROUP BY MAJ_CAT, RDC
ORDER BY expected DESC
"""

Q3 = """
WITH excess AS (
    SELECT WERKS, GEN_ART_NUMBER, ISNULL(CLR, '') AS CLR, RDC,
           SUM(ISNULL(EXCESS_STK, 0)) AS excess_stk
    FROM ARS_LISTING_PARKED WITH (NOLOCK)
    WHERE SESSION_ID = :sid
    GROUP BY WERKS, GEN_ART_NUMBER, ISNULL(CLR, ''), RDC
)
SELECT TOP 5 L.MAJ_CAT, L.RDC,
       SUM(ISNULL(L.ALLOC_QTY, 0))   AS alloc_qty,
       SUM(ISNULL(e.excess_stk, 0))  AS excess_stk_total
FROM ARS_LISTING_WORKING_PARKED L WITH (NOLOCK)
LEFT JOIN excess e
       ON e.WERKS = L.WERKS
      AND e.GEN_ART_NUMBER = L.GEN_ART_NUMBER
      AND e.CLR = ISNULL(L.CLR, '')
      AND e.RDC = L.RDC
WHERE L.SESSION_ID = :sid
GROUP BY L.MAJ_CAT, L.RDC
ORDER BY excess_stk_total DESC
"""

Q3_REF = """
SELECT TOP 5 MAJ_CAT, RDC, SUM(ISNULL(EXCESS_STK, 0)) AS expected
FROM ARS_LISTING_PARKED WITH (NOLOCK)
WHERE SESSION_ID = :sid
GROUP BY MAJ_CAT, RDC
ORDER BY expected DESC
"""


def run(label, sql, params):
    print(f"\n=== {label} ===")
    with DataSessionLocal() as db:
        rows = db.execute(text(sql), params).fetchall()
        if not rows:
            print("(no rows)")
            return
        cols = rows[0]._mapping.keys()
        print(" | ".join(cols))
        for r in rows:
            print(" | ".join(str(r._mapping[c]) for c in cols))


if __name__ == "__main__":
    p = {"sid": SESSION_ID}
    run("Q1: NEW pattern (WERKS=HB05) — alloc + excess by (MAJ_CAT, RDC)", Q1, p)
    run("Q2: REFERENCE (WERKS=HB05) — direct SUM(EXCESS_STK) by (MAJ_CAT, RDC)", Q2, p)
    run("Q3: NEW pattern (no WERKS filter) — by (MAJ_CAT, RDC)", Q3, p)
    run("Q3-REF: direct SUM(EXCESS_STK) no WERKS filter — by (MAJ_CAT, RDC)", Q3_REF, p)
