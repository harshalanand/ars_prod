"""
audit_no_pool_msa.py — capture BEFORE/AFTER state for the NO_POOL_MSA
mis-attribution and MJ_REQ-boundary fixes (F1..F4).

Run before applying the fixes, then re-run listing+allocation, then run
again with --after to produce a side-by-side diff for the M_BOXER row.

Usage (from backend/):
    python scripts/audit_no_pool_msa.py            # writes audit_before.txt
    python scripts/audit_no_pool_msa.py --after    # writes audit_after.txt
"""
import os
import sys
import argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from sqlalchemy import text  # noqa: E402

from app.database.session import get_data_engine  # noqa: E402


WERKS = "HB05"
MAJ_CAT = "M_BOXER"
GEN_ART = "1114093352"
CLR = "BLK"


def dump_rows(conn, label, sql, **params):
    rows = conn.execute(text(sql), params).fetchall()
    out = [f"\n=== {label} ({len(rows)} rows) ===\n{sql.strip()}\n"]
    if not rows:
        out.append("(no rows)\n")
        return "".join(out)
    cols = list(rows[0]._mapping.keys())
    out.append("\t".join(cols) + "\n")
    for r in rows:
        out.append("\t".join("" if v is None else str(v) for v in r) + "\n")
    return "".join(out)


def main(after: bool):
    out_path = os.path.join(HERE, "audit_after.txt" if after else "audit_before.txt")
    engine = get_data_engine()
    sections = [f"# audit_{'after' if after else 'before'} @ {datetime.now().isoformat(timespec='seconds')}\n"]

    with engine.connect() as conn:
        # Q1: user's first example — listing rows that allocated
        sections.append(dump_rows(
            conn,
            "Q1 ARS_LISTING_WORKING (M_BOXER, HB05, ALLOC_QTY>0)",
            """
            SELECT OPT_TYPE, OPT_PRIORITY_RANK, GEN_ART_NUMBER, CLR,
                   MJ_REQ, ACS_D, MJ_MBQ, MJ_STK_TTL,
                   OPT_REQ, OPT_REQ_WH, MSA_FNL_Q, MSA_FNL_Q_REM,
                   PRI_CT_REM, MJ_REQ_REM,
                   LISTED_FLAG, ALLOC_QTY, HOLD_QTY,
                   ALLOC_STATUS, ALLOC_REMARKS
            FROM ARS_LISTING_WORKING
            WHERE MAJ_CAT = :mc AND WERKS = :wk AND ALLOC_QTY > 0
            ORDER BY OPT_TYPE, OPT_PRIORITY_RANK
            """,
            mc=MAJ_CAT, wk=WERKS,
        ))

        # Q2: the alloc rows for the suspect article × colour
        sections.append(dump_rows(
            conn,
            "Q2 ARS_ALLOC_WORKING (M_BOXER, HB05, 1114093352, BLK)",
            """
            SELECT OPT_TYPE, OPT_PRIORITY_RANK, ST_RANK, VAR_ART, SZ,
                   SZ_REQ, SZ_MBQ, SZ_STK, CONT, I_ROD,
                   FNL_Q, FNL_Q_REM, POOL_CONSUMED,
                   SHIP_QTY, HOLD_QTY, ALLOC_QTY, ALLOC_ROUND,
                   ALLOC_STATUS, SKIP_REASON, ALLOC_REASON,
                   ALLOC_REMARKS
            FROM ARS_ALLOC_WORKING
            WHERE MAJ_CAT = :mc AND WERKS = :wk
              AND GEN_ART_NUMBER = :ga AND CLR = :cl
            ORDER BY OPT_TYPE, OPT_PRIORITY_RANK, VAR_ART, SZ
            """,
            mc=MAJ_CAT, wk=WERKS, ga=GEN_ART, cl=CLR,
        ))

        # Q3: cross-store rank race for the same VAR×SZ — confirms whether the
        # per-size pool was drained by higher-rank stores in the same RDC.
        sections.append(dump_rows(
            conn,
            "Q3 cross-store ranks (same MAJ_CAT, GEN_ART, CLR, all WERKS)",
            """
            SELECT WERKS, OPT_TYPE, OPT_PRIORITY_RANK, ST_RANK, VAR_ART, SZ,
                   SHIP_QTY, HOLD_QTY, FNL_Q, FNL_Q_REM,
                   ALLOC_STATUS, SKIP_REASON, ALLOC_REASON
            FROM ARS_ALLOC_WORKING
            WHERE MAJ_CAT = :mc AND GEN_ART_NUMBER = :ga AND CLR = :cl
            ORDER BY VAR_ART, SZ, OPT_PRIORITY_RANK, ST_RANK, WERKS
            """,
            mc=MAJ_CAT, ga=GEN_ART, cl=CLR,
        ))

        # Q4: aggregate misattribution — rows tagged NO_POOL_MSA that
        # still carry a cap remark in ALLOC_REMARKS. Counts the F2 impact.
        sections.append(dump_rows(
            conn,
            "Q4 NO_POOL_MSA rows that smell like a cap (suspected mis-tag)",
            """
            SELECT TOP (50)
                   MAJ_CAT,
                   COUNT(*) AS rows_n,
                   SUM(CASE WHEN ALLOC_REMARKS LIKE '%MBQ_CAP%' THEN 1 ELSE 0 END) AS mbq_cap_hit,
                   SUM(CASE WHEN ALLOC_REMARKS LIKE '%SEC_CAP%' THEN 1 ELSE 0 END) AS sec_cap_hit,
                   SUM(CASE WHEN ALLOC_REMARKS LIKE '%MJ_REQ_CAP%' THEN 1 ELSE 0 END) AS mj_req_cap_hit,
                   SUM(CASE WHEN ALLOC_REMARKS LIKE '%R09_HEADROOM%' THEN 1 ELSE 0 END) AS r09_hit
            FROM ARS_ALLOC_WORKING
            WHERE ALLOC_STATUS = 'SKIPPED'
              AND ISNULL(SKIP_REASON,'') = 'NO_POOL_MSA'
              AND (ALLOC_REMARKS LIKE '%MBQ_CAP%'
                   OR ALLOC_REMARKS LIKE '%SEC_CAP%'
                   OR ALLOC_REMARKS LIKE '%MJ_REQ_CAP%'
                   OR ALLOC_REMARKS LIKE '%R09_HEADROOM%')
            GROUP BY MAJ_CAT
            ORDER BY rows_n DESC
            """,
        ))

        # Q5: the boundary case for F1 — listed OPTs sitting exactly at
        # MJ_REQ = 0.5 × ACS_D (excluded today by `>`, eligible after F1).
        sections.append(dump_rows(
            conn,
            "Q5 boundary OPTs where MJ_REQ == 0.5 * ACS_D (F1 inclusivity)",
            """
            SELECT TOP (50)
                   WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR,
                   OPT_TYPE, MJ_REQ, ACS_D,
                   LISTED_FLAG, ALLOC_QTY
            FROM ARS_LISTING_WORKING
            WHERE LISTED_FLAG = 1
              AND ISNULL(ACS_D, 0) > 0
              AND ABS(CAST(MJ_REQ AS FLOAT) - 0.5 * CAST(ACS_D AS FLOAT)) < 0.5
            ORDER BY MAJ_CAT, WERKS
            """,
        ))

        # Q6: reason distribution across the whole alloc table — for a
        # before/after rollup at the bottom of each report.
        sections.append(dump_rows(
            conn,
            "Q6 ALLOC_REASON distribution",
            """
            SELECT ISNULL(ALLOC_REASON, '(null)') AS reason,
                   COUNT(*) AS rows_n
            FROM ARS_ALLOC_WORKING
            GROUP BY ALLOC_REASON
            ORDER BY rows_n DESC
            """,
        ))

    with open(out_path, "w", encoding="utf-8") as f:
        f.writelines(sections)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--after", action="store_true",
                    help="write audit_after.txt instead of audit_before.txt")
    main(ap.parse_args().after)
