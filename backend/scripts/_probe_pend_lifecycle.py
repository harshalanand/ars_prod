"""Read-only probes for ARS_PEND_ALC ↔ MSA ↔ Grid lifecycle review."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.session import get_data_engine
from sqlalchemy import text
import json

eng = get_data_engine()

def hdr(s):
    print()
    print("=" * 78)
    print(s)
    print("=" * 78)

def q(sql, params=None, label=None):
    if label:
        print(f"\n--- {label} ---")
    with eng.connect() as c:
        rows = c.execute(text(sql), params or {}).fetchall()
        for r in rows:
            print(r)
        return rows

# -------------------------------------------------------------------
hdr("1. ARS_PEND_ALC row counts by IS_CLOSED × SOURCE × ALLOC_MODE")
q("""
SELECT IS_CLOSED, ISNULL(SOURCE,'?') AS SOURCE, ISNULL(ALLOC_MODE,'?') AS MODE,
       COUNT(*) AS rows, SUM(ALLOC_QTY) AS alloc_q, SUM(DO_QTY) AS do_q,
       SUM(PEND_QTY) AS pend_q
FROM ARS_PEND_ALC
GROUP BY IS_CLOSED, SOURCE, ALLOC_MODE
ORDER BY IS_CLOSED, SOURCE, MODE
""")

# -------------------------------------------------------------------
hdr("2. Operations log: counts by OP_TYPE and reverted status")
q("""
SELECT OP_TYPE,
       SUM(CASE WHEN REVERTED_AT IS NULL THEN 1 ELSE 0 END) AS active,
       SUM(CASE WHEN REVERTED_AT IS NOT NULL THEN 1 ELSE 0 END) AS reverted,
       COUNT(*) AS total
FROM ARS_PEND_ALC_OPERATIONS
GROUP BY OP_TYPE
""")

# -------------------------------------------------------------------
hdr("3. Recent revert operations — top 5 most recent")
q("""
SELECT TOP 5 OP_ID, OP_TYPE, OP_KEY, OP_DATE, REVERTED_AT, REVERTED_BY,
       ROWS_AFFECTED, QTY_TOTAL, LEFT(SUMMARY,80) AS SUMM,
       LEN(PAYLOAD) AS payload_chars
FROM ARS_PEND_ALC_OPERATIONS
WHERE REVERTED_AT IS NOT NULL
ORDER BY REVERTED_AT DESC
""")

# -------------------------------------------------------------------
hdr("4. Sample PAYLOAD for recent reverts — peek the shape")
rows = q("""
SELECT TOP 5 OP_ID, OP_TYPE, PAYLOAD
FROM ARS_PEND_ALC_OPERATIONS
WHERE REVERTED_AT IS NOT NULL
ORDER BY REVERTED_AT DESC
""", label="Reverted op payloads")
for r in rows:
    try:
        p = json.loads(r[2])
        keys = list(p.keys()) if isinstance(p, dict) else 'not-dict'
        n_inserted = len(p.get("inserted_ids", [])) if isinstance(p, dict) else None
        n_stamped = len(p.get("stamped_rows", [])) if isinstance(p, dict) else None
        n_pend = len(p.get("pend_updates", [])) if isinstance(p, dict) else None
        n_hist = len(p.get("history_updates", [])) if isinstance(p, dict) else None
        print(f"OP {r[0]} ({r[1]}): keys={keys} "
              f"inserted_ids={n_inserted} stamped={n_stamped} "
              f"pend_updates={n_pend} hist_updates={n_hist}")
    except Exception as e:
        print(f"OP {r[0]}: payload parse error: {e}")

# -------------------------------------------------------------------
hdr("5. ARS_PEND_ALC open rows with NO matching ARTICLE in ARS_MSA_TOTAL")
# Determine MSA_TOTAL article column
with eng.connect() as c:
    col = c.execute(text("""
        SELECT TOP 1 COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME='ARS_MSA_TOTAL'
          AND COLUMN_NAME IN ('ARTICLE_NUMBER','VAR_ART','ARTICLE')
        ORDER BY CASE COLUMN_NAME WHEN 'ARTICLE_NUMBER' THEN 1 WHEN 'VAR_ART' THEN 2 ELSE 3 END
    """)).fetchone()
art_col = col[0] if col else 'ARTICLE_NUMBER'
print(f"ARS_MSA_TOTAL article column = {art_col}")

q(f"""
SELECT COUNT(DISTINCT P.RDC + '|' + P.ARTICLE_NUMBER) AS pend_keys_total,
       COUNT(DISTINCT CASE WHEN T.RDC IS NULL
                      THEN P.RDC + '|' + P.ARTICLE_NUMBER END) AS pend_keys_NOT_in_msa,
       SUM(CASE WHEN T.RDC IS NULL THEN P.PEND_QTY ELSE 0 END) AS pend_qty_missing_from_msa
FROM (
    SELECT DISTINCT RDC, ARTICLE_NUMBER, SUM(PEND_QTY) AS PEND_QTY
    FROM ARS_PEND_ALC WHERE IS_CLOSED=0
    GROUP BY RDC, ARTICLE_NUMBER
) P
LEFT JOIN ARS_MSA_TOTAL T
       ON T.RDC = P.RDC AND T.[{art_col}] = P.ARTICLE_NUMBER
""", label="Pend rows missing from MSA")

# -------------------------------------------------------------------
hdr("6. ARS_PEND_ALC open rows with NO matching row in ARS_GRID_MJ_VAR_ART")
# Probe grid var art article col
with eng.connect() as c:
    col = c.execute(text("""
        SELECT TOP 1 COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME='ARS_GRID_MJ_VAR_ART'
          AND COLUMN_NAME IN ('ARTICLE_NUMBER','VAR_ART','ARTICLE')
        ORDER BY CASE COLUMN_NAME WHEN 'ARTICLE_NUMBER' THEN 1 WHEN 'VAR_ART' THEN 2 ELSE 3 END
    """)).fetchone()
g_art = col[0] if col else 'ARTICLE_NUMBER'
print(f"ARS_GRID_MJ_VAR_ART article column = {g_art}")

q(f"""
SELECT COUNT(*) AS pend_grain_rows,
       SUM(CASE WHEN V.WERKS IS NULL THEN 1 ELSE 0 END) AS missing_from_grid,
       SUM(CASE WHEN V.WERKS IS NULL THEN P.PEND_QTY ELSE 0 END) AS qty_missing_from_grid
FROM (
    SELECT ST_CD, MAJ_CAT, GEN_ART_NUMBER, CLR, ARTICLE_NUMBER, SUM(PEND_QTY) AS PEND_QTY
    FROM ARS_PEND_ALC WHERE IS_CLOSED=0 AND PEND_QTY>0
    GROUP BY ST_CD, MAJ_CAT, GEN_ART_NUMBER, CLR, ARTICLE_NUMBER
) P
LEFT JOIN ARS_GRID_MJ_VAR_ART V
       ON V.WERKS = P.ST_CD
      AND ISNULL(V.MAJ_CAT,'') = ISNULL(P.MAJ_CAT,'')
      AND ISNULL(V.GEN_ART_NUMBER,'') = ISNULL(P.GEN_ART_NUMBER,'')
      AND ISNULL(V.CLR,'') = ISNULL(P.CLR,'')
      AND V.[{g_art}] = P.ARTICLE_NUMBER
""", label="Pend grain rows missing from VAR_ART grid")

# -------------------------------------------------------------------
hdr("7. List all live ARS_GRID_MJ* tables on this server")
q("""
SELECT TABLE_NAME
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_TYPE='BASE TABLE' AND TABLE_NAME LIKE 'ARS_GRID_MJ%'
ORDER BY TABLE_NAME
""")
