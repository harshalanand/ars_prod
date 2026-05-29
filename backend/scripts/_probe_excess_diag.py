"""Diagnose why the LEFT JOIN drops all EXCESS_STK rows."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from app.database.session import DataSessionLocal

SID = '20260528_110819_156'

DIAGS = {
    "A: row counts": """
        SELECT
          (SELECT COUNT(*) FROM ARS_LISTING_PARKED WITH (NOLOCK) WHERE SESSION_ID = :sid) AS parked_rows,
          (SELECT COUNT(*) FROM ARS_LISTING_WORKING_PARKED WITH (NOLOCK) WHERE SESSION_ID = :sid) AS working_rows,
          (SELECT COUNT(*) FROM ARS_LISTING_PARKED WITH (NOLOCK) WHERE SESSION_ID = :sid AND ISNULL(EXCESS_STK,0) > 0) AS parked_with_excess
    """,
    "B: FW_K_SLIPPER / HB05 / DH24 — parked rows w/ excess (sample)": """
        SELECT TOP 5 WERKS, GEN_ART_NUMBER, CLR, RDC, EXCESS_STK
        FROM ARS_LISTING_PARKED WITH (NOLOCK)
        WHERE SESSION_ID = :sid AND WERKS='HB05' AND MAJ_CAT='FW_K_SLIPPER' AND RDC='DH24'
          AND ISNULL(EXCESS_STK,0) > 0
    """,
    "C: same gen_art in WORKING_PARKED?": """
        SELECT TOP 5 W.WERKS, W.GEN_ART_NUMBER, W.CLR, W.RDC, W.MAJ_CAT, W.ALLOC_QTY
        FROM ARS_LISTING_WORKING_PARKED W WITH (NOLOCK)
        WHERE W.SESSION_ID = :sid AND W.WERKS='HB05' AND W.MAJ_CAT='FW_K_SLIPPER' AND W.RDC='DH24'
    """,
    "D: any FW_K_SLIPPER in WORKING for HB05?": """
        SELECT TOP 5 RDC, COUNT(*) AS n
        FROM ARS_LISTING_WORKING_PARKED WITH (NOLOCK)
        WHERE SESSION_ID = :sid AND WERKS='HB05' AND MAJ_CAT='FW_K_SLIPPER'
        GROUP BY RDC
    """,
    "E: distinct MAJ_CAT for HB05 in PARKED (top by excess)": """
        SELECT TOP 10 MAJ_CAT, COUNT(*) AS rows_, SUM(ISNULL(EXCESS_STK,0)) AS sum_excess
        FROM ARS_LISTING_PARKED WITH (NOLOCK)
        WHERE SESSION_ID = :sid AND WERKS='HB05'
        GROUP BY MAJ_CAT
        ORDER BY sum_excess DESC
    """,
    "F: distinct MAJ_CAT for HB05 in WORKING_PARKED": """
        SELECT TOP 10 MAJ_CAT, COUNT(*) AS rows_, SUM(ISNULL(ALLOC_QTY,0)) AS sum_alloc
        FROM ARS_LISTING_WORKING_PARKED WITH (NOLOCK)
        WHERE SESSION_ID = :sid AND WERKS='HB05'
        GROUP BY MAJ_CAT
        ORDER BY sum_alloc DESC
    """,
    "G: overlap on (WERKS,GEN_ART,CLR,RDC) HB05": """
        SELECT
          (SELECT COUNT(DISTINCT CONCAT(WERKS,'|',GEN_ART_NUMBER,'|',ISNULL(CLR,''),'|',RDC))
             FROM ARS_LISTING_PARKED WITH (NOLOCK)
             WHERE SESSION_ID = :sid AND WERKS='HB05' AND ISNULL(EXCESS_STK,0) > 0) AS parked_keys_with_excess,
          (SELECT COUNT(DISTINCT CONCAT(WERKS,'|',GEN_ART_NUMBER,'|',ISNULL(CLR,''),'|',RDC))
             FROM ARS_LISTING_WORKING_PARKED WITH (NOLOCK)
             WHERE SESSION_ID = :sid AND WERKS='HB05') AS working_keys,
          (SELECT COUNT(*) FROM (
              SELECT DISTINCT WERKS, GEN_ART_NUMBER, ISNULL(CLR,'') AS CLR, RDC
              FROM ARS_LISTING_PARKED WITH (NOLOCK)
              WHERE SESSION_ID = :sid AND WERKS='HB05' AND ISNULL(EXCESS_STK,0) > 0
              INTERSECT
              SELECT DISTINCT WERKS, GEN_ART_NUMBER, ISNULL(CLR,'') AS CLR, RDC
              FROM ARS_LISTING_WORKING_PARKED WITH (NOLOCK)
              WHERE SESSION_ID = :sid AND WERKS='HB05'
          ) X) AS overlap_keys
    """,
    "H: schema — does ARS_LISTING_PARKED actually have GEN_ART_NUMBER/RDC/CLR/EXCESS_STK?": """
        SELECT COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME='ARS_LISTING_PARKED'
          AND COLUMN_NAME IN ('WERKS','GEN_ART_NUMBER','CLR','RDC','EXCESS_STK','SESSION_ID','MAJ_CAT')
    """,
    "I: schema — ARS_LISTING_WORKING_PARKED key cols": """
        SELECT COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME='ARS_LISTING_WORKING_PARKED'
          AND COLUMN_NAME IN ('WERKS','GEN_ART_NUMBER','CLR','RDC','ALLOC_QTY','SESSION_ID','MAJ_CAT')
    """,
}

def run(label, sql):
    print(f"\n=== {label} ===")
    with DataSessionLocal() as db:
        rows = db.execute(text(sql), {"sid": SID}).fetchall()
        if not rows:
            print("(no rows)")
            return
        cols = list(rows[0]._mapping.keys())
        print(" | ".join(cols))
        for r in rows:
            print(" | ".join(str(r._mapping[c]) for c in cols))

if __name__ == "__main__":
    for k, q in DIAGS.items():
        try:
            run(k, q)
        except Exception as e:
            print(f"\n=== {k} ===\nERROR: {e}")
