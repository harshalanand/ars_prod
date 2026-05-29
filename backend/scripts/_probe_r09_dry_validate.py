"""One-off dry-validation probe for the new R09 (TBL-only, ACS_D fallback=18).
Read-only. No writes. Safe to delete after use.
"""
import sys
sys.path.insert(0, ".")
from sqlalchemy import text

from app.database.session import data_engine


def main():
    with data_engine.connect() as c:
        candidates = ["ARS_LISTING_WORKING"]
        src = None
        for t in candidates:
            n = c.execute(text(f"SELECT COUNT(*) FROM [{t}]")).scalar() or 0
            print(f"rows[{t}] = {n}")
            if n > 0:
                src = t
                break
        if not src:
            print("NO_SOURCE")
            return

        cols = [
            r[0]
            for r in c.execute(
                text(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_NAME = :t"
                ),
                {"t": src},
            )
        ]
        needed = ["LISTING", "OPT_TYPE", "MJ_MBQ", "MJ_STK_TTL", "ACS_D"]
        miss = [x for x in needed if x not in cols]
        print(f"missing_cols: {miss}")
        if miss:
            return

        # Probe 1 — TBL newly blocked (PASS old → FAIL new, ACS_D fallback 1→18)
        r = c.execute(
            text(
                f"""
                SELECT COUNT(*) FROM [{src}]
                WHERE ISNULL(TRY_CAST(LISTING AS INT),1) = 1
                  AND ISNULL(OPT_TYPE,'') = 'TBL'
                  AND NOT ((ISNULL(MJ_MBQ,0) - ISNULL(MJ_STK_TTL,0))
                           < 0.5 * ISNULL(NULLIF(ACS_D,0), 1))
                  AND     ((ISNULL(MJ_MBQ,0) - ISNULL(MJ_STK_TTL,0))
                           < 0.5 * ISNULL(NULLIF(ACS_D,0), 18))
                """
            )
        ).scalar()
        print(f"PROBE_1_TBL_NEWLY_BLOCKED = {r}")

        # Probe 2 — RL/TBC rows previously blocked by R09 under cap=1.10
        rows = c.execute(
            text(
                f"""
                SELECT OPT_TYPE, COUNT(*) FROM [{src}]
                WHERE ISNULL(TRY_CAST(LISTING AS INT),1) = 1
                  AND ISNULL(OPT_TYPE,'') IN ('RL','TBC')
                  AND ((1.10 * ISNULL(MJ_MBQ,0) - ISNULL(MJ_STK_TTL,0))
                        < 0.5 * ISNULL(NULLIF(ACS_D,0), 1))
                GROUP BY OPT_TYPE
                """
            )
        ).fetchall()
        print(f"PROBE_2_RL_TBC_FREED = {dict((r[0], r[1]) for r in rows)}")

        # Probe 3 — ACS_D histogram on TBL
        r = c.execute(
            text(
                f"""
                SELECT
                  SUM(CASE WHEN ACS_D IS NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN ACS_D = 0 THEN 1 ELSE 0 END),
                  SUM(CASE WHEN ACS_D BETWEEN 1 AND 9 THEN 1 ELSE 0 END),
                  SUM(CASE WHEN ACS_D BETWEEN 10 AND 17 THEN 1 ELSE 0 END),
                  SUM(CASE WHEN ACS_D = 18 THEN 1 ELSE 0 END),
                  SUM(CASE WHEN ACS_D >= 19 THEN 1 ELSE 0 END),
                  COUNT(*)
                FROM [{src}]
                WHERE ISNULL(TRY_CAST(LISTING AS INT),1) = 1
                  AND ISNULL(OPT_TYPE,'') = 'TBL'
                """
            )
        ).fetchone()
        print(
            f"PROBE_3_ACS_D_HIST_TBL: "
            f"null={r[0]} zero={r[1]} 1-9={r[2]} 10-17={r[3]} =18={r[4]} >=19={r[5]} "
            f"total={r[6]}"
        )

        # Probe 4 — TBL where MJ_MBQ - MJ_STK_TTL < 9 (trips under default ACS_D=18)
        r = c.execute(
            text(
                f"""
                SELECT COUNT(*) FROM [{src}]
                WHERE ISNULL(TRY_CAST(LISTING AS INT),1) = 1
                  AND ISNULL(OPT_TYPE,'') = 'TBL'
                  AND (ISNULL(MJ_MBQ,0) - ISNULL(MJ_STK_TTL,0)) < 9
                """
            )
        ).scalar()
        print(f"PROBE_4_TBL_SKEW_LT_9 = {r}")

        # TBL total for context
        r = c.execute(
            text(
                f"""
                SELECT COUNT(*) FROM [{src}]
                WHERE ISNULL(TRY_CAST(LISTING AS INT),1) = 1
                  AND ISNULL(OPT_TYPE,'') = 'TBL'
                """
            )
        ).scalar()
        print(f"TBL_TOTAL_LISTED1 = {r}")


if __name__ == "__main__":
    main()
