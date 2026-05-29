"""Compare two listing sessions at OPT grain.

Usage:
    python scripts/compare_sessions.py <session_a> <session_b>

Prints:
- Aggregate ship/hold/alloc-row totals per session
- OPT rows where SHIP_QTY differs between A and B (these are the mismatches)
- OPT rows where HOLD_QTY differs (should be empty if hold matches perfectly)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database.session import get_data_engine

SESSION_A = sys.argv[1] if len(sys.argv) > 1 else "20260514_152345_428"
SESSION_B = sys.argv[2] if len(sys.argv) > 2 else "20260514_152624_574"


def main() -> None:
    eng = get_data_engine()
    with eng.connect() as conn:
        # 1. Aggregate totals per session
        print("=" * 78)
        print(f"SESSION A: {SESSION_A}")
        print(f"SESSION B: {SESSION_B}")
        print("=" * 78)

        totals = conn.execute(text("""
            SELECT SESSION_ID,
                   COUNT(*)                                          AS alloc_rows,
                   SUM(CASE WHEN ISNULL(SHIP_QTY,0) > 0 THEN 1 ELSE 0 END) AS live_rows,
                   ISNULL(SUM(SHIP_QTY), 0)                          AS ship_total,
                   ISNULL(SUM(HOLD_QTY), 0)                          AS hold_total
            FROM ARS_ALLOC_PARKED
            WHERE SESSION_ID IN (:a, :b)
            GROUP BY SESSION_ID
        """), {"a": SESSION_A, "b": SESSION_B}).fetchall()
        print("\nAggregate totals:")
        print(f"{'session':40} {'alloc_rows':>10} {'live':>8} {'ship':>10} {'hold':>10}")
        for r in totals:
            print(f"{r[0]:40} {r[1]:>10} {r[2]:>8} {int(r[3]):>10} {int(r[4]):>10}")

        # 2. OPT-grain SHIP differences
        opt_diffs = conn.execute(text("""
            WITH A AS (
                SELECT MAJ_CAT, GEN_ART_NUMBER, CLR, WERKS,
                       SUM(ISNULL(SHIP_QTY,0)) AS SHIP_A,
                       SUM(ISNULL(HOLD_QTY,0)) AS HOLD_A,
                       COUNT(*)                AS ROWS_A
                FROM ARS_ALLOC_PARKED
                WHERE SESSION_ID = :a
                GROUP BY MAJ_CAT, GEN_ART_NUMBER, CLR, WERKS
            ),
            B AS (
                SELECT MAJ_CAT, GEN_ART_NUMBER, CLR, WERKS,
                       SUM(ISNULL(SHIP_QTY,0)) AS SHIP_B,
                       SUM(ISNULL(HOLD_QTY,0)) AS HOLD_B,
                       COUNT(*)                AS ROWS_B
                FROM ARS_ALLOC_PARKED
                WHERE SESSION_ID = :b
                GROUP BY MAJ_CAT, GEN_ART_NUMBER, CLR, WERKS
            )
            SELECT COALESCE(A.MAJ_CAT, B.MAJ_CAT) AS MAJ_CAT,
                   COALESCE(A.GEN_ART_NUMBER, B.GEN_ART_NUMBER) AS GEN_ART,
                   COALESCE(A.CLR, B.CLR) AS CLR,
                   COALESCE(A.WERKS, B.WERKS) AS WERKS,
                   ISNULL(A.SHIP_A, 0) AS SHIP_A,
                   ISNULL(B.SHIP_B, 0) AS SHIP_B,
                   ISNULL(B.SHIP_B, 0) - ISNULL(A.SHIP_A, 0) AS SHIP_DIFF,
                   ISNULL(A.HOLD_A, 0) AS HOLD_A,
                   ISNULL(B.HOLD_B, 0) AS HOLD_B,
                   ISNULL(A.ROWS_A, 0) AS ROWS_A,
                   ISNULL(B.ROWS_B, 0) AS ROWS_B
            FROM A FULL OUTER JOIN B
              ON  A.MAJ_CAT        = B.MAJ_CAT
              AND A.GEN_ART_NUMBER = B.GEN_ART_NUMBER
              AND A.CLR            = B.CLR
              AND A.WERKS          = B.WERKS
            WHERE ISNULL(A.SHIP_A, 0) <> ISNULL(B.SHIP_B, 0)
               OR ISNULL(A.HOLD_A, 0) <> ISNULL(B.HOLD_B, 0)
            ORDER BY ABS(ISNULL(B.SHIP_B, 0) - ISNULL(A.SHIP_A, 0)) DESC
        """), {"a": SESSION_A, "b": SESSION_B}).fetchall()

        if not opt_diffs:
            print("\nNo OPT-grain mismatches between A and B.")
            return

        print(f"\nOPT-grain mismatches (SHIP or HOLD differs): {len(opt_diffs)} OPTs")
        print(
            f"\n{'WERKS':>8} {'MAJ_CAT':<10} {'GEN_ART':>12} {'CLR':<10} "
            f"{'SHIP_A':>8} {'SHIP_B':>8} {'DIFF':>6} "
            f"{'HOLD_A':>8} {'HOLD_B':>8} {'rA':>3} {'rB':>3}"
        )
        for r in opt_diffs:
            print(
                f"{str(r.WERKS):>8} {str(r.MAJ_CAT):<10} {str(r.GEN_ART):>12} "
                f"{str(r.CLR or ''):<10} "
                f"{int(r.SHIP_A):>8} {int(r.SHIP_B):>8} {int(r.SHIP_DIFF):>+6} "
                f"{int(r.HOLD_A):>8} {int(r.HOLD_B):>8} "
                f"{int(r.ROWS_A):>3} {int(r.ROWS_B):>3}"
            )

        # 3. Per-grid blocked vs overridden classification — pull REASON
        print("\n\nMismatch reasons (SKIP_REASON for blocked OPTs in A):")
        reasons = conn.execute(text("""
            WITH A_blocked AS (
                SELECT a.MAJ_CAT, a.GEN_ART_NUMBER, a.CLR, a.WERKS,
                       SUM(ISNULL(a.SHIP_QTY,0)) AS SHIP_A,
                       MAX(a.SKIP_REASON)        AS SKIP_REASON,
                       MAX(a.ALLOC_STATUS)       AS ALLOC_STATUS,
                       MAX(a.ALLOC_REASON)       AS ALLOC_REASON,
                       MAX(a.OPT_TYPE)           AS OPT_TYPE,
                       MAX(a.FB_REASON)          AS FB_REASON
                FROM ARS_ALLOC_PARKED a
                WHERE a.SESSION_ID = :a
                GROUP BY a.MAJ_CAT, a.GEN_ART_NUMBER, a.CLR, a.WERKS
            ),
            B_ship AS (
                SELECT MAJ_CAT, GEN_ART_NUMBER, CLR, WERKS,
                       SUM(ISNULL(SHIP_QTY,0)) AS SHIP_B
                FROM ARS_ALLOC_PARKED
                WHERE SESSION_ID = :b
                GROUP BY MAJ_CAT, GEN_ART_NUMBER, CLR, WERKS
            )
            SELECT A_blocked.WERKS, A_blocked.MAJ_CAT, A_blocked.GEN_ART_NUMBER,
                   A_blocked.CLR, A_blocked.OPT_TYPE,
                   A_blocked.SHIP_A, B_ship.SHIP_B,
                   A_blocked.ALLOC_STATUS, A_blocked.ALLOC_REASON,
                   A_blocked.SKIP_REASON, A_blocked.FB_REASON
            FROM A_blocked
            INNER JOIN B_ship
              ON  A_blocked.MAJ_CAT        = B_ship.MAJ_CAT
              AND A_blocked.GEN_ART_NUMBER = B_ship.GEN_ART_NUMBER
              AND A_blocked.CLR            = B_ship.CLR
              AND A_blocked.WERKS          = B_ship.WERKS
            WHERE A_blocked.SHIP_A <> B_ship.SHIP_B
            ORDER BY ABS(B_ship.SHIP_B - A_blocked.SHIP_A) DESC
        """), {"a": SESSION_A, "b": SESSION_B}).fetchall()
        for r in reasons:
            diff = int(r.SHIP_B) - int(r.SHIP_A)
            print(
                f"  WERKS={r.WERKS} MAJ_CAT={r.MAJ_CAT} "
                f"GEN_ART={r.GEN_ART_NUMBER} CLR={r.CLR} OPT_TYPE={r.OPT_TYPE}: "
                f"SHIP A={int(r.SHIP_A)} B={int(r.SHIP_B)} ({'+' if diff>=0 else ''}{diff})  "
                f"status={r.ALLOC_STATUS!r} alloc_reason={r.ALLOC_REASON!r} "
                f"skip_reason={r.SKIP_REASON!r} fb_reason={r.FB_REASON!r}"
            )


if __name__ == "__main__":
    main()
