"""Diagnostic: why does the next BDC for HB05/HD24 re-stamp open BDCs?"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

from sqlalchemy import text
from app.database.session import get_data_engine

RDC = "HD24"
ST  = "HB05"

eng = get_data_engine()
with eng.connect() as conn:
    print("=" * 80)
    print("STEP 1: Sample PEND_ALC rows (RDC=HD24, ST_CD=HB05, BDC_QTY>0, DO_QTY<BDC_QTY)")
    print("=" * 80)
    rows = conn.execute(text("""
        SELECT TOP 5
               ARTICLE_NUMBER, ALLOC_QTY, BDC_QTY, DO_QTY, PEND_QTY,
               LAST_BDC_AT, IS_CLOSED, ALLOC_MODE, SOURCE
        FROM ARS_PEND_ALC WITH (NOLOCK)
        WHERE RDC = :rdc AND ST_CD = :st
          AND BDC_QTY > 0 AND DO_QTY < BDC_QTY
          AND IS_CLOSED = 0
        ORDER BY LAST_BDC_AT DESC, ARTICLE_NUMBER
    """), {"rdc": RDC, "st": ST}).fetchall()
    arts = []
    for r in rows:
        print(f"  ART={r[0]}  ALLOC={r[1]} BDC={r[2]} DO={r[3]} PEND={r[4]}  "
              f"LAST_BDC_AT={r[5]} IS_CLOSED={r[6]} MODE={r[7]} SRC={r[8]}")
        arts.append(r[0])

    if not arts:
        print("  (no rows)")
    else:
        print()
        print("=" * 80)
        print("STEP 2: ARS_BDC_HISTORY rows for those exact articles")
        print("=" * 80)
        for a in arts:
            hist = conn.execute(text("""
                SELECT ID, ALLOCATION_NUMBER, RDC, ST_CD, ARTICLE_NUMBER,
                       BDC_QTY, DO_RECEIVED, STATUS, BDC_DATE,
                       LEN(STATUS) AS slen, ASCII(RIGHT(STATUS,1)) AS last_ch
                FROM ARS_BDC_HISTORY WITH (NOLOCK)
                WHERE RDC = :rdc AND ST_CD = :st AND ARTICLE_NUMBER = :art
                ORDER BY BDC_DATE DESC, ID DESC
            """), {"rdc": RDC, "st": ST, "art": a}).fetchall()
            print(f"\n  ART={a}: {len(hist)} history row(s)")
            for h in hist:
                print(f"    ID={h[0]} ALC={h[1]} BDC_QTY={h[5]} DO_REC={h[6]} "
                      f"STATUS=[{h[7]!r}] len={h[9]} last_ascii={h[10]} "
                      f"BDC_DATE={h[8]}")

    print()
    print("=" * 80)
    print("STEP 3: Global STATUS distribution in ARS_BDC_HISTORY")
    print("=" * 80)
    dist = conn.execute(text("""
        SELECT STATUS, COUNT(*) AS n,
               MIN(LEN(STATUS)) AS minlen, MAX(LEN(STATUS)) AS maxlen
        FROM ARS_BDC_HISTORY WITH (NOLOCK)
        GROUP BY STATUS
        ORDER BY n DESC
    """)).fetchall()
    for r in dist:
        print(f"  STATUS=[{r[0]!r}] count={r[1]}  len_min={r[2]} len_max={r[3]}")

    print()
    print("=" * 80)
    print("STEP 4: Filter probe for RDC=HD24, ST_CD=HB05")
    print("=" * 80)
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
    print(f"  (a) PEND open & PEND_QTY>0   : {a}")
    print(f"  (b) ALSO passes _NO_OPEN_BDC : {b}")
    print(f"  excluded by predicate        : {a - b}")

    # Also try the predicate with a relaxed STATUS match
    c = conn.execute(text("""
        SELECT COUNT(*) FROM ARS_PEND_ALC P WITH (NOLOCK)
        WHERE P.RDC = :rdc AND P.ST_CD = :st AND P.IS_CLOSED = 0 AND P.PEND_QTY > 0
          AND NOT EXISTS (
              SELECT 1 FROM ARS_BDC_HISTORY h WITH (NOLOCK)
              WHERE h.RDC = P.RDC
                AND ISNULL(h.ST_CD,'') = ISNULL(P.ST_CD,'')
                AND h.ARTICLE_NUMBER = P.ARTICLE_NUMBER
                AND (h.DO_RECEIVED < h.BDC_QTY)
                AND UPPER(LTRIM(RTRIM(h.STATUS))) IN ('OPEN','PARTIAL')
          )
    """), {"rdc": RDC, "st": ST}).scalar()
    print(f"  (c) predicate w/ trim+IN(OPEN,PARTIAL)+residual>0 : {c}")

    print()
    print("=" * 80)
    print("STEP 5: Row-by-row EXISTS for sample articles")
    print("=" * 80)
    for a_art in arts:
        ex = conn.execute(text("""
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM ARS_BDC_HISTORY h WITH (NOLOCK)
                WHERE h.RDC = :rdc
                  AND ISNULL(h.ST_CD,'') = ISNULL(:st,'')
                  AND h.ARTICLE_NUMBER = :art
                  AND h.STATUS = 'OPEN'
            ) THEN 1 ELSE 0 END,
            CASE WHEN EXISTS (
                SELECT 1 FROM ARS_BDC_HISTORY h WITH (NOLOCK)
                WHERE h.RDC = :rdc
                  AND ISNULL(h.ST_CD,'') = ISNULL(:st,'')
                  AND h.ARTICLE_NUMBER = :art
                  AND UPPER(LTRIM(RTRIM(h.STATUS))) IN ('OPEN','PARTIAL')
                  AND (h.DO_RECEIVED < h.BDC_QTY)
            ) THEN 1 ELSE 0 END
        """), {"rdc": RDC, "st": ST, "art": a_art}).fetchone()
        print(f"  ART={a_art}  exists(STATUS='OPEN')={ex[0]}  exists(trim+IN+residual>0)={ex[1]}")

    print()
    print("=" * 80)
    print("BONUS: Are there duplicate PEND_ALC rows per (RDC,ST,ART)?")
    print("=" * 80)
    dup = conn.execute(text("""
        SELECT TOP 10 ARTICLE_NUMBER, COUNT(*) AS n
        FROM ARS_PEND_ALC WITH (NOLOCK)
        WHERE RDC = :rdc AND ST_CD = :st AND IS_CLOSED = 0
        GROUP BY ARTICLE_NUMBER
        HAVING COUNT(*) > 1
        ORDER BY n DESC
    """), {"rdc": RDC, "st": ST}).fetchall()
    if not dup:
        print("  no duplicates")
    else:
        for r in dup:
            print(f"  ART={r[0]} rows={r[1]}")
