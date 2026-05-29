"""Diagnose OPT-grain MJ_REQ gate behaviour on the latest ARS session.

Run from d:\\ARS_PROD\\ars_prod\\backend with:
    python -m scripts.diag_opt_mj_req_gate
"""
from sqlalchemy import text
from app.database.session import DataSessionLocal


def dump(rows, headers=None, max_rows=40):
    if headers is None and rows and hasattr(rows[0], "_fields"):
        headers = rows[0]._fields
    if headers:
        print(" | ".join(str(h) for h in headers))
        print("-" * 120)
    for r in rows[:max_rows]:
        print(" | ".join("" if v is None else str(v) for v in r))
    if len(rows) > max_rows:
        print(f"... {len(rows) - max_rows} more rows")


def main():
    with DataSessionLocal() as db:
        # ------------------------------------------------------------------
        print("\n### (a) Latest ARS sessions")
        rows = db.execute(text("""
            SELECT TOP 3 SESSION_ID, STATUS, ALLOC_ROWS, SHIP_QTY_TOTAL,
                   STARTED_AT, COMPLETED_AT
            FROM rep_data.dbo.ARS_LISTING_SESSIONS
            ORDER BY STARTED_AT DESC
        """)).fetchall()
        dump(rows)

        # ------------------------------------------------------------------
        print("\n### (a2) ALLOC_WORKING grouped by OPT_TYPE / STATUS")
        rows = db.execute(text("""
            SELECT OPT_TYPE, ALLOC_STATUS,
                   COUNT(*) AS rows_cnt,
                   SUM(ISNULL(SHIP_QTY,0)) AS ship_total,
                   SUM(ISNULL(HOLD_QTY,0)) AS hold_total
            FROM rep_data.dbo.ARS_ALLOC_WORKING
            GROUP BY OPT_TYPE, ALLOC_STATUS
            ORDER BY OPT_TYPE, ALLOC_STATUS
        """)).fetchall()
        dump(rows, ["OPT_TYPE","ALLOC_STATUS","rows","ship","hold"])

        # ------------------------------------------------------------------
        print("\n### (b) SKIP_REASON distribution")
        rows = db.execute(text("""
            SELECT TOP 25 SKIP_REASON, COUNT(*) AS n,
                   SUM(ISNULL(SHIP_QTY,0)) AS ship_total
            FROM rep_data.dbo.ARS_ALLOC_WORKING
            WHERE ALLOC_STATUS = 'SKIPPED'
            GROUP BY SKIP_REASON
            ORDER BY n DESC
        """)).fetchall()
        dump(rows, ["SKIP_REASON","n","ship_total"])

        # ------------------------------------------------------------------
        print("\n### (c1) OPT_MBQ presence on ARS_ALLOC_WORKING")
        rows = db.execute(text("""
            SELECT COUNT(*) AS n_rows,
                   COUNT(CASE WHEN OPT_MBQ IS NOT NULL THEN 1 END) AS n_not_null,
                   COUNT(CASE WHEN OPT_MBQ IS NOT NULL AND OPT_MBQ > 0 THEN 1 END) AS n_positive,
                   MIN(OPT_MBQ) AS min_mbq, MAX(OPT_MBQ) AS max_mbq,
                   AVG(CAST(OPT_MBQ AS FLOAT)) AS avg_mbq
            FROM rep_data.dbo.ARS_ALLOC_WORKING
        """)).fetchall()
        dump(rows, ["n_rows","n_not_null","n_positive","min","max","avg"])

        # ------------------------------------------------------------------
        print("\n### (c1b) OPT_MBQ stats by OPT_TYPE (also OPT_MBQ_WH)")
        rows = db.execute(text("""
            SELECT OPT_TYPE,
                   COUNT(*) AS n_rows,
                   MIN(OPT_MBQ) AS min_mbq, MAX(OPT_MBQ) AS max_mbq,
                   AVG(CAST(OPT_MBQ AS FLOAT)) AS avg_mbq,
                   MIN(OPT_MBQ_WH) AS min_wh, MAX(OPT_MBQ_WH) AS max_wh,
                   AVG(CAST(OPT_MBQ_WH AS FLOAT)) AS avg_wh
            FROM rep_data.dbo.ARS_ALLOC_WORKING
            GROUP BY OPT_TYPE
            ORDER BY OPT_TYPE
        """)).fetchall()
        dump(rows, ["OPT_TYPE","n","min_mbq","max_mbq","avg_mbq","min_wh","max_wh","avg_wh"])

        # ------------------------------------------------------------------
        print("\n### (c2) MJ_REQ on ARS_LISTING_WORKING (LISTED_FLAG=1)")
        rows = db.execute(text("""
            SELECT COUNT(*) AS n_rows,
                   COUNT(CASE WHEN LISTED_FLAG=1 THEN 1 END) AS n_listed,
                   COUNT(CASE WHEN LISTED_FLAG=1 AND ISNULL(MJ_REQ,0) > 0 THEN 1 END) AS n_listed_mjreq_pos,
                   MIN(CASE WHEN LISTED_FLAG=1 THEN MJ_REQ END) AS min_mjreq,
                   MAX(CASE WHEN LISTED_FLAG=1 THEN MJ_REQ END) AS max_mjreq,
                   AVG(CASE WHEN LISTED_FLAG=1 THEN CAST(MJ_REQ AS FLOAT) END) AS avg_mjreq
            FROM rep_data.dbo.ARS_LISTING_WORKING
        """)).fetchall()
        dump(rows, ["n_rows","n_listed","n_listed_mjreq_pos","min_mjreq","max_mjreq","avg_mjreq"])

        # ------------------------------------------------------------------
        print("\n### (c3) Top MJ_REQ rows (LISTED_FLAG=1)")
        rows = db.execute(text("""
            SELECT TOP 10 WERKS, MAJ_CAT, MJ_REQ, MJ_MBQ, MJ_STK_TTL
            FROM rep_data.dbo.ARS_LISTING_WORKING
            WHERE LISTED_FLAG = 1
            ORDER BY MJ_REQ DESC
        """)).fetchall()
        dump(rows, ["WERKS","MAJ_CAT","MJ_REQ","MJ_MBQ","MJ_STK_TTL"])

        # ------------------------------------------------------------------
        print("\n### (c4) Sample OPT-grain rows from ARS_ALLOC_WORKING")
        rows = db.execute(text("""
            SELECT TOP 15 WERKS, MAJ_CAT, OPT_TYPE, GEN_ART_NUMBER, CLR,
                          OPT_MBQ, OPT_MBQ_WH, OPT_REQ, OPT_REQ_WH
            FROM rep_data.dbo.ARS_ALLOC_WORKING
            WHERE OPT_MBQ IS NOT NULL OR OPT_MBQ_WH IS NOT NULL
        """)).fetchall()
        dump(rows, ["WERKS","MAJ_CAT","OPT_TYPE","GEN_ART","CLR","OPT_MBQ","OPT_MBQ_WH","OPT_REQ","OPT_REQ_WH"])

        # ------------------------------------------------------------------
        print("\n### (d) The 1 RL OPT that did ship — top rows")
        rows = db.execute(text("""
            SELECT TOP 15 WERKS, MAJ_CAT, OPT_TYPE, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
                          OPT_MBQ, SHIP_QTY, HOLD_QTY, ALLOC_STATUS, SKIP_REASON,
                          LEFT(ISNULL(ALLOC_REMARKS,''), 300) AS remarks
            FROM rep_data.dbo.ARS_ALLOC_WORKING
            WHERE ISNULL(SHIP_QTY,0) > 0 OR ISNULL(HOLD_QTY,0) > 0
            ORDER BY SHIP_QTY DESC
        """)).fetchall()
        dump(rows, ["WERKS","MAJ_CAT","OPT_TYPE","GEN_ART","CLR","VAR_ART","SZ",
                    "OPT_MBQ","SHIP","HOLD","STATUS","SKIP_REASON","remarks"])

        # ------------------------------------------------------------------
        print("\n### (e) Replay Budgeted/Winner CTEs — pick a (WERKS,MAJ_CAT) with skips")
        # Find a (WERKS,MAJ_CAT) that has many skips for inspection
        target = db.execute(text("""
            SELECT TOP 1 WERKS, MAJ_CAT
            FROM rep_data.dbo.ARS_ALLOC_WORKING
            WHERE ALLOC_STATUS = 'SKIPPED'
              AND SKIP_REASON LIKE '%MJ_REQ_GATE_FAIL%'
            GROUP BY WERKS, MAJ_CAT
            ORDER BY COUNT(*) DESC
        """)).fetchone()
        if target:
            werks, mj = target
            print(f"Inspecting WERKS={werks}, MAJ_CAT={mj}")
            rows = db.execute(text("""
                ;WITH OptInfo AS (
                    SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, ISNULL(CLR,'') AS CLR_K,
                           OPT_TYPE,
                           MAX(ISNULL(OPT_PRIORITY_TIER, 3))   AS opt_tier,
                           MAX(ISNULL(OPT_PRIORITY_RANK, 999999)) AS opt_rank,
                           MAX(ISNULL(ST_RANK, 999999))        AS st_rank,
                           MAX(ISNULL(OPT_MBQ, 0))             AS opt_mbq,
                           SUM(ISNULL(SHIP_QTY, 0))            AS opt_ship,
                           SUM(ISNULL(HOLD_QTY, 0))            AS opt_hold
                    FROM rep_data.dbo.ARS_ALLOC_WORKING
                    WHERE WERKS = :w AND MAJ_CAT = :m
                    GROUP BY WERKS, MAJ_CAT, GEN_ART_NUMBER, ISNULL(CLR,''), OPT_TYPE
                ),
                MjReq AS (
                    SELECT WERKS, MAJ_CAT, MAX(ISNULL(MJ_REQ, 0)) AS mj_req
                    FROM rep_data.dbo.ARS_LISTING_WORKING
                    WHERE LISTED_FLAG = 1 AND WERKS = :w AND MAJ_CAT = :m
                    GROUP BY WERKS, MAJ_CAT
                )
                SELECT O.OPT_TYPE, O.GEN_ART_NUMBER, O.CLR_K, O.opt_tier, O.opt_rank, O.st_rank,
                       O.opt_mbq, O.opt_ship, O.opt_hold,
                       ISNULL(M.mj_req, 0) AS mj_req,
                       100.0 * ISNULL(M.mj_req,0)/100.0 AS budget_at_100pct,
                       CASE WHEN 100.0 * ISNULL(M.mj_req,0)/100.0 >= 0.5 * O.opt_mbq
                                 AND (O.opt_ship + O.opt_hold) > 0
                            THEN 1 ELSE 0 END AS passes_at_100pct
                FROM OptInfo O
                LEFT JOIN MjReq M ON M.WERKS = O.WERKS AND M.MAJ_CAT = O.MAJ_CAT
                ORDER BY O.OPT_TYPE, O.opt_tier, O.opt_rank, O.st_rank
            """), {"w": werks, "m": mj}).fetchall()
            dump(rows, ["OPT_TYPE","GEN_ART","CLR","tier","rank","st_rank",
                        "opt_mbq","opt_ship","opt_hold","mj_req","budget","passes"])

            # MJ_REQ row from listing — is it present?
            rows = db.execute(text("""
                SELECT WERKS, MAJ_CAT, LISTED_FLAG, MJ_REQ, MJ_MBQ, MJ_STK_TTL,
                       COUNT(*) AS n_rows
                FROM rep_data.dbo.ARS_LISTING_WORKING
                WHERE WERKS = :w AND MAJ_CAT = :m
                GROUP BY WERKS, MAJ_CAT, LISTED_FLAG, MJ_REQ, MJ_MBQ, MJ_STK_TTL
            """), {"w": werks, "m": mj}).fetchall()
            print("\n### (e2) ARS_LISTING_WORKING rows for same (WERKS,MAJ_CAT)")
            dump(rows, ["WERKS","MAJ_CAT","LISTED_FLAG","MJ_REQ","MJ_MBQ","MJ_STK_TTL","n_rows"])

        # ------------------------------------------------------------------
        print("\n### (f) Columns on ARS_ALLOC_WORKING — confirm OPT_MBQ is present")
        rows = db.execute(text("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'ARS_ALLOC_WORKING'
              AND COLUMN_NAME IN
                  ('OPT_MBQ','OPT_MBQ_WH','OPT_REQ','OPT_REQ_WH',
                   'OPT_TYPE','OPT_PRIORITY_TIER','OPT_PRIORITY_RANK',
                   'ST_RANK','SHIP_QTY','HOLD_QTY','ALLOC_STATUS','SKIP_REASON')
            ORDER BY COLUMN_NAME
        """)).fetchall()
        dump(rows, ["COLUMN_NAME"])


if __name__ == "__main__":
    main()
