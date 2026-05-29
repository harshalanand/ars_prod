"""Follow-up: data shows only ONE (WERKS,MAJ_CAT)=(HK15, M_TEES_PN_HS) was processed.
The gate didn't run (no MJ_REQ_GATE_FAIL skips). Investigate why."""
from sqlalchemy import text
from app.database.session import DataSessionLocal


def dump(rows, headers=None, max_rows=80):
    if rows and headers is None and hasattr(rows[0], "_fields"):
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
        # ---- 1: scope summary
        print("\n### Distinct (WERKS, MAJ_CAT) keys on each table")
        for tbl in ("ARS_LISTING_WORKING", "ARS_ALLOC_WORKING"):
            try:
                rows = db.execute(text(f"""
                    SELECT COUNT(*) AS n_total,
                           COUNT(DISTINCT CONCAT(WERKS,'|',MAJ_CAT)) AS n_keys,
                           COUNT(DISTINCT WERKS) AS n_werks,
                           COUNT(DISTINCT MAJ_CAT) AS n_mj
                    FROM rep_data.dbo.{tbl}
                """)).fetchall()
                print(f"\n[{tbl}]")
                dump(rows, ["n_total","n_keys","n_werks","n_mj"])
            except Exception as e:
                print(f"[{tbl}] ERROR {e}")

        # ---- 2: LISTED_FLAG distribution on listing_working
        print("\n### LISTED_FLAG distribution on ARS_LISTING_WORKING")
        rows = db.execute(text("""
            SELECT LISTED_FLAG, COUNT(*) AS n,
                   COUNT(DISTINCT CONCAT(WERKS,'|',MAJ_CAT)) AS n_keys,
                   COUNT(DISTINCT WERKS) AS n_werks
            FROM rep_data.dbo.ARS_LISTING_WORKING
            GROUP BY LISTED_FLAG
            ORDER BY LISTED_FLAG
        """)).fetchall()
        dump(rows, ["LISTED_FLAG","n","n_keys","n_werks"])

        # ---- 3: For the one MAJ_CAT in play, replay Budgeted/Winner manually
        print("\n### Manual replay for (HK15, M_TEES_PN_HS)")
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
                WHERE WERKS = 'HK15' AND MAJ_CAT = 'M_TEES_PN_HS'
                GROUP BY WERKS, MAJ_CAT, GEN_ART_NUMBER, ISNULL(CLR,''), OPT_TYPE
            ),
            MjReq AS (
                SELECT WERKS, MAJ_CAT, MAX(ISNULL(MJ_REQ, 0)) AS mj_req
                FROM rep_data.dbo.ARS_LISTING_WORKING
                WHERE LISTED_FLAG = 1 AND WERKS = 'HK15' AND MAJ_CAT = 'M_TEES_PN_HS'
                GROUP BY WERKS, MAJ_CAT
            )
            SELECT O.OPT_TYPE, O.GEN_ART_NUMBER, O.CLR_K, O.opt_tier, O.opt_rank, O.st_rank,
                   O.opt_mbq, O.opt_ship, O.opt_hold,
                   ISNULL(M.mj_req, 0) AS mj_req,
                   100.0 * ISNULL(M.mj_req,0)/100.0 AS budget_at_100pct,
                   0.5 * O.opt_mbq AS gate_threshold,
                   CASE WHEN 100.0 * ISNULL(M.mj_req,0)/100.0 >= 0.5 * O.opt_mbq
                             AND (O.opt_ship + O.opt_hold) > 0
                        THEN 1 ELSE 0 END AS passes_at_100pct
            FROM OptInfo O
            LEFT JOIN MjReq M ON M.WERKS = O.WERKS AND M.MAJ_CAT = O.MAJ_CAT
            ORDER BY
                CASE O.OPT_TYPE WHEN 'RL' THEN 1 WHEN 'TBC' THEN 2 WHEN 'TBL' THEN 3 ELSE 9 END,
                O.opt_tier, O.opt_rank, O.st_rank, O.GEN_ART_NUMBER, O.CLR_K
        """)).fetchall()
        dump(rows, ["OPT_TYPE","GEN_ART","CLR","tier","rank","st_rank",
                    "opt_mbq","opt_ship","opt_hold","mj_req","budget","threshold","passes"])

        # ---- 4: Sample raw rows showing which OPTs got ALREADY_STOCKED / NO_POOL_MSA
        print("\n### Sample SKIPPED rows by SKIP_REASON")
        rows = db.execute(text("""
            SELECT TOP 30 OPT_TYPE, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
                          OPT_MBQ, SHIP_QTY, HOLD_QTY, SKIP_REASON,
                          LEFT(ISNULL(ALLOC_REMARKS,''), 200) AS remarks
            FROM rep_data.dbo.ARS_ALLOC_WORKING
            WHERE ALLOC_STATUS = 'SKIPPED'
            ORDER BY OPT_TYPE, GEN_ART_NUMBER
        """)).fetchall()
        dump(rows, ["OPT_TYPE","GEN_ART","CLR","VAR_ART","SZ",
                    "OPT_MBQ","SHIP","HOLD","SKIP_REASON","remarks"])


if __name__ == "__main__":
    main()
