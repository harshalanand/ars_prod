"""Targeted: show the full ALLOC_REMARKS and the per-row ship for the over-ship OPT."""
import sys
from sqlalchemy import text
sys.path.insert(0, ".")
from app.database.session import get_data_engine

SID = "20260518_005130_783"
eng = get_data_engine()

with eng.connect() as conn:
    # Full remarks for the TBL OPT that shipped 52
    print("=== full remarks: TBL rank1 GA=1116113282 O_WHT ===")
    rows = conn.execute(text("""
        SELECT GEN_ART_NUMBER, CLR, OPT_TYPE, OPT_PRIORITY_RANK,
               ALLOC_QTY, HOLD_QTY, ALLOC_REMARKS
        FROM ARS_LISTING_WORKING_PARKED
        WHERE SESSION_ID = :s
          AND GEN_ART_NUMBER = 1116113282 AND CLR = 'O_WHT'
    """), {"s": SID}).fetchall()
    for r in rows:
        print(r)
        print()

    # Per-size alloc detail for that OPT
    print("\n=== per-size alloc rows: TBL GA=1116113282 O_WHT ===")
    rows = conn.execute(text("""
        SELECT VAR_ART, SZ, SHIP_QTY, HOLD_QTY, ALLOC_QTY, SZ_MBQ, SZ_MBQ_WH,
               SZ_STK, FNL_Q, FNL_Q_REM, POOL_CONSUMED,
               ALLOC_WAVE, ALLOC_ROUND, ALLOC_STATUS, ALLOC_REMARKS
        FROM ARS_ALLOC_PARKED
        WHERE SESSION_ID = :s
          AND GEN_ART_NUMBER = 1116113282 AND CLR = 'O_WHT'
        ORDER BY SZ
    """), {"s": SID}).fetchall()
    for r in rows:
        print(r)

    # The RL rank2 row that shipped 14
    print("\n=== full remarks: RL rank2 GA=1116112688 D_GRY ===")
    rows = conn.execute(text("""
        SELECT GEN_ART_NUMBER, CLR, OPT_TYPE, OPT_PRIORITY_RANK,
               ALLOC_QTY, HOLD_QTY, ALLOC_REMARKS, MJ_REQ_REM
        FROM ARS_LISTING_WORKING_PARKED
        WHERE SESSION_ID = :s
          AND GEN_ART_NUMBER = 1116112688 AND CLR = 'D_GRY'
    """), {"s": SID}).fetchall()
    for r in rows:
        print(r)

    # The RL rank1 row that shipped 3
    print("\n=== full remarks: RL rank1 GA=1116115144 LT_PST ===")
    rows = conn.execute(text("""
        SELECT GEN_ART_NUMBER, CLR, OPT_TYPE, OPT_PRIORITY_RANK,
               ALLOC_QTY, HOLD_QTY, ALLOC_REMARKS, MJ_REQ_REM
        FROM ARS_LISTING_WORKING_PARKED
        WHERE SESSION_ID = :s
          AND GEN_ART_NUMBER = 1116115144 AND CLR = 'LT_PST'
    """), {"s": SID}).fetchall()
    for r in rows:
        print(r)
