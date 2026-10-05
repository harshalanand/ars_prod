#!/usr/bin/env python
"""
READ-ONLY preview: what ARS_PEND_ALC would look like if RDC carried the
SOURCING warehouse instead of the store's warehouse.

    python scripts/preview_rdc_correction.py <session_id>

Writes nothing. Re-derives the corrected RDC by joining each pend row to the
split rows for the same (store, article) and taking SRC_RDC — which is what
`write_pend_alc` would do after the proposed change.

Why it matters: msa_service._load_ars_pending does
    SELECT RDC, ARTICLE_NUMBER, SUM(PEND_QTY) ... GROUP BY RDC, ARTICLE_NUMBER
so RDC alone decides which warehouse pool loses the stock, and
    FNL_Q = max(STK - PEND - HOLD, 0)
is computed per (RDC, ARTICLE).
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

# One pend row per (store, article) joined to its split row(s). The split
# table is the only place that knows which warehouse physically ships.
_JOIN = """
    FROM ARS_PEND_ALC P
    LEFT JOIN (
        SELECT SESSION_ID, WERKS,
               LTRIM(RTRIM(CAST(VAR_ART AS NVARCHAR(50)))) AS VA,
               SRC_RDC,
               SUM(TRY_CAST(SHIP_QTY AS FLOAT)) AS SRC_QTY
          FROM ARS_ALLOC_RDC_SPLIT_HISTORY
         GROUP BY SESSION_ID, WERKS,
                  LTRIM(RTRIM(CAST(VAR_ART AS NVARCHAR(50)))), SRC_RDC
    ) S
      ON  S.SESSION_ID = P.SESSION_ID
      AND S.WERKS      = P.ST_CD
      AND S.VA         = LTRIM(RTRIM(CAST(P.ARTICLE_NUMBER AS NVARCHAR(50))))
   WHERE P.SESSION_ID = :s
"""


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    sid = sys.argv[1]
    with get_data_engine().connect() as c:
        print(f"session {sid}\n")

        rows = c.execute(text(f"""
            SELECT ISNULL(S.SRC_RDC, P.RDC) AS corrected_rdc,
                   P.RDC                    AS current_rdc,
                   COUNT(*)                 AS n,
                   SUM(TRY_CAST(P.PEND_QTY AS FLOAT)) AS qty
            {_JOIN}
            GROUP BY ISNULL(S.SRC_RDC, P.RDC), P.RDC
            ORDER BY 1, 2"""), {"s": sid}).fetchall()

        print("  how each pend row is labelled now vs corrected")
        print(f"  {'corrected (ships)':<20} {'current (store)':<18} {'rows':>8} {'qty':>10}")
        for r in rows:
            flag = "" if r[0] == r[1] else "   <-- MIS-ATTRIBUTED"
            print(f"  {r[0]:<20} {r[1]:<18} {r[2]:>8,} {r[3]:>10,.0f}{flag}")

        cur = dict((r[0], (r[1], r[2])) for r in c.execute(text("""
            SELECT RDC, COUNT(*), SUM(TRY_CAST(PEND_QTY AS FLOAT))
              FROM ARS_PEND_ALC WHERE SESSION_ID = :s GROUP BY RDC"""),
            {"s": sid}).fetchall())
        new = dict((r[0], (r[1], r[2])) for r in c.execute(text(f"""
            SELECT ISNULL(S.SRC_RDC, P.RDC), COUNT(*),
                   SUM(TRY_CAST(P.PEND_QTY AS FLOAT))
            {_JOIN}
            GROUP BY ISNULL(S.SRC_RDC, P.RDC)"""), {"s": sid}).fetchall())

        print("\n  PER-WAREHOUSE PEND — the number MSA deducts")
        print(f"  {'warehouse':<12} {'now':>12} {'corrected':>12} {'change':>12}")
        tot_now = tot_new = 0.0
        for w in sorted(set(cur) | set(new)):
            a = cur.get(w, (0, 0.0))[1] or 0.0
            b = new.get(w, (0, 0.0))[1] or 0.0
            tot_now += a
            tot_new += b
            print(f"  {w:<12} {a:>12,.0f} {b:>12,.0f} {b - a:>+12,.0f}")
        print(f"  {'TOTAL':<12} {tot_now:>12,.0f} {tot_new:>12,.0f} {tot_new - tot_now:>+12,.0f}")

        wrong = c.execute(text(f"""
            SELECT COUNT(*), ISNULL(SUM(TRY_CAST(P.PEND_QTY AS FLOAT)),0)
            {_JOIN} AND S.SRC_RDC IS NOT NULL AND S.SRC_RDC <> P.RDC"""),
            {"s": sid}).fetchone()
        print(f"\n  rows whose warehouse changes: {wrong[0]:,}  "
              f"carrying {wrong[1]:,.0f} pcs")

        print("\n  SAMPLE ROWS (largest mis-attributions)")
        samp = c.execute(text(f"""
            SELECT TOP 8 P.ST_CD, P.ARTICLE_NUMBER, P.RDC AS now_rdc,
                   S.SRC_RDC AS corrected_rdc,
                   TRY_CAST(P.PEND_QTY AS FLOAT) AS qty
            {_JOIN} AND S.SRC_RDC IS NOT NULL AND S.SRC_RDC <> P.RDC
            ORDER BY TRY_CAST(P.PEND_QTY AS FLOAT) DESC"""), {"s": sid}).fetchall()
        print(f"  {'store':<8} {'article':<16} {'now':<7} {'corrected':<10} {'qty':>7}")
        for r in samp:
            print(f"  {r[0]:<8} {str(r[1]):<16} {r[2]:<7} {r[3]:<10} {r[4]:>7,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
