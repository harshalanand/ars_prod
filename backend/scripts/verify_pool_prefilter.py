#!/usr/bin/env python
"""
Verify the Listing pool pre-filter (FS-LPP-01..03) — read-only.

    python scripts/verify_pool_prefilter.py [FRESH|GRT]

Rebuilds the exact `_msa_pool_filter` the endpoint builds, runs the DISTINCT
count with and without it, and proves the gate is a strict subset of what the
existing stock gates admit:

    Part 6.6 gate_stock = MSA_FNL_Q > 0 OR RL_HOLD_QTY > 0
    Stage A  R04        = block only when BOTH are 0

so an option may only be dropped when it has neither pool stock nor a live
prior-run hold. Anything dropped here would have been dropped downstream.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from sqlalchemy import text                                # noqa: E402
from app.database.session import get_data_engine           # noqa: E402
from app.utils.db_helpers import table_exists, get_columns  # noqa: E402

MSA = "ARS_MSA_GEN_ART"
HOLD = "ARS_NL_TBL_HOLD_TRACKING"


def build_filter(conn, msa_table: str, alloc_type: str):
    """Byte-for-byte the same construction as listing.py FS-LPP-01."""
    cols = {c.upper() for c in (get_columns(conn, msa_table)
                                if table_exists(conn, msa_table) else [])}
    filt, params = "", {}
    if "ALLOC_TYPE" in cols:
        filt += " AND ISNULL([ALLOC_TYPE],'FRESH') = :pool_alloc_type"
        params["pool_alloc_type"] = alloc_type
    if "FNL_Q" in cols:
        rescue = ""
        if table_exists(conn, HOLD):
            hr_type = ""
            if "ALLOC_TYPE" in {c.upper() for c in get_columns(conn, HOLD)}:
                hr_type = ("\n                            AND (H.[ALLOC_TYPE] = :pool_alloc_type"
                           " OR ISNULL(H.[ALLOC_TYPE],'') = '')")
                params["pool_alloc_type"] = alloc_type
            rescue = f"""
                        OR EXISTS (
                            SELECT 1 FROM [{HOLD}] H
                            WHERE ISNULL(H.[IS_CLOSED], 0) = 0
                              AND ISNULL(H.[HOLD_REM],  0) > 0{hr_type}
                              AND H.[MAJ_CAT] = LTRIM(RTRIM(CAST([{msa_table}].[MAJ_CAT] AS NVARCHAR(200))))
                              AND H.[GEN_ART_NUMBER] = TRY_CAST(TRY_CAST([{msa_table}].[GEN_ART_NUMBER] AS FLOAT) AS BIGINT)
                              AND ISNULL(H.[CLR],'') = ISNULL(LTRIM(RTRIM(CAST([{msa_table}].[CLR] AS NVARCHAR(200)))),'')
                        )"""
        filt += f" AND (TRY_CAST([FNL_Q] AS FLOAT) > 0{rescue}\n                    )"
    return filt, params


def distinct_count(conn, msa_table: str, extra: str, params: dict) -> int:
    sql = f"""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT
                   LTRIM(RTRIM(CAST([MAJ_CAT] AS NVARCHAR(200)))) AS MAJ_CAT,
                   TRY_CAST(TRY_CAST([GEN_ART_NUMBER] AS FLOAT) AS BIGINT) AS GEN_ART_NUMBER,
                   LTRIM(RTRIM(CAST([CLR] AS NVARCHAR(200)))) AS CLR
              FROM [{msa_table}]
             WHERE [MAJ_CAT] IS NOT NULL AND [GEN_ART_NUMBER] IS NOT NULL{extra}
        ) X"""
    return conn.execute(text(sql), params).scalar() or 0


def main() -> int:
    pool = (sys.argv[1] if len(sys.argv) > 1 else "FRESH").upper()
    with get_data_engine().connect() as c:
        filt, params = build_filter(c, MSA, pool)
        print(f"pool = {pool}\n")
        print("generated filter:")
        print("  " + (filt.strip() or "(empty — legacy MSA, no-op)"))
        print()

        base = distinct_count(c, MSA, "", {})
        new = distinct_count(c, MSA, filt, params)
        print(f"  DISTINCT options unfiltered : {base:>9,}")
        print(f"  DISTINCT options filtered   : {new:>9,}   "
              f"({(base - new) / max(base, 1):.1%} removed)")

        # Subset proof: nothing may be dropped that still has stock or a hold.
        viol = c.execute(text(f"""
            SELECT COUNT(*) FROM (
                SELECT DISTINCT
                       LTRIM(RTRIM(CAST([MAJ_CAT] AS NVARCHAR(200)))) AS MC,
                       TRY_CAST(TRY_CAST([GEN_ART_NUMBER] AS FLOAT) AS BIGINT) AS GA,
                       LTRIM(RTRIM(CAST([CLR] AS NVARCHAR(200)))) AS CL
                  FROM [{MSA}]
                 WHERE [MAJ_CAT] IS NOT NULL AND [GEN_ART_NUMBER] IS NOT NULL
                   AND NOT ( 1=1 {filt} )
            ) D
            WHERE EXISTS (
                SELECT 1 FROM [{HOLD}] H
                 WHERE ISNULL(H.[IS_CLOSED],0) = 0 AND ISNULL(H.[HOLD_REM],0) > 0
                   AND H.[MAJ_CAT] = D.MC AND H.[GEN_ART_NUMBER] = D.GA
                   AND ISNULL(H.[CLR],'') = ISNULL(D.CL,''))"""), params).scalar() or 0
        print()
        print(f"  dropped options that still hold a live hold : {viol}   "
              f"{'PASS — strict subset' if viol == 0 else 'FAIL — would lose held stock'}")

        held = c.execute(text(
            f"SELECT COUNT(*) FROM [{HOLD}] "
            f"WHERE ISNULL(IS_CLOSED,0)=0 AND ISNULL(HOLD_REM,0)>0")).scalar()
        print(f"  open hold rows on the ledger right now      : {held:,}")
        if not held:
            print("      (the rescue clause is inert today — it protects the "
                  "case where holds exist again)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
