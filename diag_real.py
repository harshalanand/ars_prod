import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
from sqlalchemy import text
from app.database.session import get_data_engine

# Pick (DH24, HD22) — has substantial BDC=5499 and DO=16603, so it's
# the closest analogue to "had a BDC, partial DO received".
RDC, ST = "DH24", "HD22"

eng = get_data_engine()
with eng.connect() as conn:
    print(f"--- STEP 1: sample PEND_ALC for {RDC}/{ST} with BDC>0 & DO<BDC ---")
    rows = conn.execute(text("""
        SELECT TOP 5
               ARTICLE_NUMBER, ALLOC_QTY, BDC_QTY, DO_QTY, PEND_QTY,
               LAST_BDC_AT, IS_CLOSED, ALLOC_MODE, SOURCE
        FROM ARS_PEND_ALC WITH (NOLOCK)
        WHERE RDC = :rdc AND ST_CD = :st
          AND BDC_QTY > 0 AND DO_QTY < BDC_QTY
          AND IS_CLOSED = 0
        ORDER BY (BDC_QTY - DO_QTY) DESC
    """), {"rdc": RDC, "st": ST}).fetchall()
    arts = []
    for r in rows:
        print(f"  ART={r[0]} ALLOC={r[1]} BDC={r[2]} DO={r[3]} PEND={r[4]} "
              f"LAST_BDC_AT={r[5]} CLOSED={r[6]} MODE={r[7]} SRC={r[8]}")
        arts.append(r[0])

    print(f"\n--- STEP 2: BDC_HISTORY for those articles ---")
    for a in arts:
        hist = conn.execute(text("""
            SELECT ID, ALLOCATION_NUMBER, RDC, ST_CD, ARTICLE_NUMBER,
                   BDC_QTY, DO_RECEIVED, STATUS, BDC_DATE
            FROM ARS_BDC_HISTORY WITH (NOLOCK)
            WHERE RDC = :rdc AND ST_CD = :st AND ARTICLE_NUMBER = :art
        """), {"rdc": RDC, "st": ST, "art": a}).fetchall()
        print(f"  ART={a}: {len(hist)} history row(s)")
        for h in hist:
            print(f"    {h}")

    print(f"\n--- STEP 4: filter probe ---")
    a = conn.execute(text("""
        SELECT COUNT(*) FROM ARS_PEND_ALC WITH (NOLOCK)
        WHERE RDC = :rdc AND ST_CD = :st AND IS_CLOSED = 0 AND PEND_QTY > 0
    """), {"rdc": RDC, "st": ST}).scalar()
    b = conn.execute(text("""
        SELECT COUNT(*) FROM ARS_PEND_ALC P WITH (NOLOCK)
        WHERE P.RDC = :rdc AND P.ST_CD = :st AND P.IS_CLOSED = 0 AND P.PEND_QTY > 0
          AND NOT EXISTS (
              SELECT 1 FROM ARS_BDC_HISTORY h WITH (NOLOCK)
              WHERE h.RDC = P.RDC
                AND ISNULL(h.ST_CD,'') = ISNULL(P.ST_CD,'')
                AND h.ARTICLE_NUMBER = P.ARTICLE_NUMBER
                AND h.STATUS = 'OPEN'
          )
    """), {"rdc": RDC, "st": ST}).scalar()
    print(f"  (a) IS_CLOSED=0 & PEND_QTY>0          : {a}")
    print(f"  (b) ALSO passes _NO_OPEN_BDC predicate : {b}")
    print(f"  excluded by predicate                  : {a-b}")

    print(f"\n--- BDC stamping evidence: how many PEND rows have BDC_QTY>0 (stamped) but NO history? ---")
    stamped_no_hist = conn.execute(text("""
        SELECT COUNT(*) FROM ARS_PEND_ALC P WITH (NOLOCK)
        WHERE P.RDC = :rdc AND P.ST_CD = :st AND P.BDC_QTY > 0 AND P.IS_CLOSED = 0
          AND NOT EXISTS (
              SELECT 1 FROM ARS_BDC_HISTORY h WITH (NOLOCK)
              WHERE h.RDC = P.RDC AND ISNULL(h.ST_CD,'') = ISNULL(P.ST_CD,'')
                AND h.ARTICLE_NUMBER = P.ARTICLE_NUMBER
          )
    """), {"rdc": RDC, "st": ST}).scalar()
    stamped_with_hist = conn.execute(text("""
        SELECT COUNT(*) FROM ARS_PEND_ALC P WITH (NOLOCK)
        WHERE P.RDC = :rdc AND P.ST_CD = :st AND P.BDC_QTY > 0 AND P.IS_CLOSED = 0
          AND EXISTS (
              SELECT 1 FROM ARS_BDC_HISTORY h WITH (NOLOCK)
              WHERE h.RDC = P.RDC AND ISNULL(h.ST_CD,'') = ISNULL(P.ST_CD,'')
                AND h.ARTICLE_NUMBER = P.ARTICLE_NUMBER
          )
    """), {"rdc": RDC, "st": ST}).scalar()
    print(f"  PEND.BDC_QTY>0 with NO history row     : {stamped_no_hist}")
    print(f"  PEND.BDC_QTY>0 WITH  history row(s)    : {stamped_with_hist}")

    print(f"\n--- Total BDC stamped vs history mismatch across ALL of ARS_PEND_ALC ---")
    g = conn.execute(text("""
        SELECT
          (SELECT COUNT(*) FROM ARS_PEND_ALC WITH (NOLOCK) WHERE BDC_QTY > 0) AS pend_bdc_pos,
          (SELECT COUNT(*) FROM ARS_PEND_ALC WITH (NOLOCK) WHERE BDC_QTY > 0 AND IS_CLOSED = 0) AS pend_bdc_pos_open,
          (SELECT COUNT(*) FROM ARS_BDC_HISTORY WITH (NOLOCK)) AS hist_n,
          (SELECT SUM(BDC_QTY) FROM ARS_PEND_ALC WITH (NOLOCK)) AS pend_sum_bdc,
          (SELECT SUM(DO_QTY)  FROM ARS_PEND_ALC WITH (NOLOCK)) AS pend_sum_do
    """)).fetchone()
    print(f"  PEND rows with BDC_QTY>0         : {g[0]}")
    print(f"  PEND rows with BDC_QTY>0 & open  : {g[1]}")
    print(f"  ARS_BDC_HISTORY total rows       : {g[2]}")
    print(f"  PEND total BDC_QTY               : {g[3]}")
    print(f"  PEND total DO_QTY                : {g[4]}")
