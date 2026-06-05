"""Diagnostic for PendAlcRecoPage BDC columns.

Validates the SQL in backend/app/api/v1/endpoints/pend_alc.py /sessions endpoint
against raw table data:
  A) Conservation: total_open_residual vs endpoint_sum + orphan_residual
  B) STATUS variants in ARS_BDC_HISTORY
  C) PEND_ALC invariants: BDC_QTY <= ALLOC_QTY, DO_QTY <= BDC_QTY
  D) Per-row inflight cross-check for top 5 sessions
  E) Fair-split blast radius
"""
from __future__ import annotations

import sys, os
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

from sqlalchemy import text
from app.database.session import get_data_engine

eng = get_data_engine()


def section(title: str) -> None:
    print()
    print("=" * 90)
    print(title)
    print("=" * 90)


# The exact endpoint SQL, parameterized so we can compare to recomputations.
ENDPOINT_SQL = """
;WITH base AS (
    SELECT SESSION_ID,
           ISNULL(SOURCE,'AUTO')              AS source,
           MIN(APPROVED_AT)                   AS approved_at,
           SUM(ALLOC_QTY)                     AS alloc_qty,
           SUM(BDC_QTY)                       AS bdc_qty,
           SUM(DO_QTY)                        AS do_qty,
           SUM(PEND_QTY)                      AS pend_qty,
           COUNT(*)                           AS article_count
    FROM ARS_PEND_ALC WITH (NOLOCK)
    WHERE IS_CLOSED = 0
    GROUP BY SESSION_ID, ISNULL(SOURCE,'AUTO')
),
key_session_count AS (
    SELECT RDC, ISNULL(ST_CD,'') AS ST_CD, ARTICLE_NUMBER,
           COUNT(DISTINCT SESSION_ID) AS n
    FROM ARS_PEND_ALC WITH (NOLOCK)
    WHERE IS_CLOSED = 0
    GROUP BY RDC, ISNULL(ST_CD,''), ARTICLE_NUMBER
),
session_keys AS (
    SELECT DISTINCT P.SESSION_ID,
           P.RDC, ISNULL(P.ST_CD,'') AS ST_CD, P.ARTICLE_NUMBER
    FROM ARS_PEND_ALC P WITH (NOLOCK)
    WHERE P.IS_CLOSED = 0
),
inflight AS (
    SELECT sk.SESSION_ID,
           SUM((H.BDC_QTY - H.DO_RECEIVED) * 1.0 / k.n) AS qty
    FROM ARS_BDC_HISTORY H WITH (NOLOCK)
    JOIN session_keys sk
      ON sk.RDC = H.RDC
     AND sk.ST_CD = ISNULL(H.ST_CD,'')
     AND sk.ARTICLE_NUMBER = H.ARTICLE_NUMBER
    JOIN key_session_count k
      ON k.RDC = sk.RDC AND k.ST_CD = sk.ST_CD
     AND k.ARTICLE_NUMBER = sk.ARTICLE_NUMBER
    WHERE H.STATUS = 'OPEN' AND (H.BDC_QTY - H.DO_RECEIVED) > 0
    GROUP BY sk.SESSION_ID
)
SELECT b.SESSION_ID, b.approved_at, b.source,
       b.alloc_qty, b.bdc_qty, b.do_qty, b.pend_qty,
       b.article_count,
       ISNULL(i.qty, 0) AS bdc_in_flight_qty
FROM base b
LEFT JOIN inflight i ON i.SESSION_ID = b.SESSION_ID
ORDER BY b.approved_at DESC
"""

# Same but with relaxed STATUS filter, used to size the impact in (B).
ENDPOINT_SQL_RELAXED = ENDPOINT_SQL.replace(
    "WHERE H.STATUS = 'OPEN' AND (H.BDC_QTY - H.DO_RECEIVED) > 0",
    "WHERE UPPER(LTRIM(RTRIM(H.STATUS))) IN ('OPEN','PARTIAL') AND (H.BDC_QTY - H.DO_RECEIVED) > 0",
)


with eng.connect() as conn:
    # -----------------------------------------------------------------
    # B (run first so we know what STATUS values exist)
    # -----------------------------------------------------------------
    section("B. STATUS variants in ARS_BDC_HISTORY")
    dist = conn.execute(text("""
        SELECT STATUS,
               COUNT(*) AS n,
               MIN(LEN(STATUS)) AS minlen,
               MAX(LEN(STATUS)) AS maxlen,
               SUM(CASE WHEN (BDC_QTY - DO_RECEIVED) > 0 THEN 1 ELSE 0 END) AS rows_with_residual,
               SUM(CASE WHEN (BDC_QTY - DO_RECEIVED) > 0
                        THEN (BDC_QTY - DO_RECEIVED) ELSE 0 END) AS total_residual
        FROM ARS_BDC_HISTORY WITH (NOLOCK)
        GROUP BY STATUS
        ORDER BY n DESC
    """)).fetchall()
    for r in dist:
        print(f"  STATUS=[{r[0]!r}] count={r[1]:>6} len=[{r[2]}..{r[3]}] "
              f"rows_w_residual={r[4]:>6} total_residual={float(r[5] or 0):.1f}")

    # Strict vs relaxed totals
    strict = conn.execute(text("""
        SELECT COUNT(*) AS rows_, SUM(BDC_QTY - DO_RECEIVED) AS residual
        FROM ARS_BDC_HISTORY WITH (NOLOCK)
        WHERE STATUS = 'OPEN' AND (BDC_QTY - DO_RECEIVED) > 0
    """)).fetchone()
    relaxed = conn.execute(text("""
        SELECT COUNT(*) AS rows_, SUM(BDC_QTY - DO_RECEIVED) AS residual
        FROM ARS_BDC_HISTORY WITH (NOLOCK)
        WHERE UPPER(LTRIM(RTRIM(STATUS))) IN ('OPEN','PARTIAL')
          AND (BDC_QTY - DO_RECEIVED) > 0
    """)).fetchone()
    print()
    print(f"  STRICT  STATUS='OPEN'                          rows={strict[0]:>6} residual={float(strict[1] or 0):.1f}")
    print(f"  RELAXED UPPER(TRIM(STATUS)) IN (OPEN,PARTIAL)  rows={relaxed[0]:>6} residual={float(relaxed[1] or 0):.1f}")
    relaxed_gap_residual = float(relaxed[1] or 0) - float(strict[1] or 0)
    print(f"  → relaxed adds {relaxed[0] - strict[0]} rows / {relaxed_gap_residual:.1f} residual")

    # -----------------------------------------------------------------
    # A. Conservation
    # -----------------------------------------------------------------
    section("A. Conservation of BDC IN FLIGHT")

    # total open residual under strict filter (the filter the endpoint uses)
    total_open_residual_strict = float(conn.execute(text("""
        SELECT SUM(BDC_QTY - DO_RECEIVED)
        FROM ARS_BDC_HISTORY WITH (NOLOCK)
        WHERE STATUS = 'OPEN' AND (BDC_QTY - DO_RECEIVED) > 0
    """)).scalar() or 0)

    # endpoint result
    endpoint_rows = conn.execute(text(ENDPOINT_SQL)).fetchall()
    endpoint_sum = sum(float(r[8] or 0) for r in endpoint_rows)

    # orphan residual under strict filter: history rows whose
    # (RDC, ST_CD, ARTICLE) has no matching open PEND_ALC row
    orphan_residual_strict = float(conn.execute(text("""
        SELECT SUM(H.BDC_QTY - H.DO_RECEIVED)
        FROM ARS_BDC_HISTORY H WITH (NOLOCK)
        WHERE H.STATUS = 'OPEN'
          AND (H.BDC_QTY - H.DO_RECEIVED) > 0
          AND NOT EXISTS (
              SELECT 1 FROM ARS_PEND_ALC P WITH (NOLOCK)
              WHERE P.RDC = H.RDC
                AND ISNULL(P.ST_CD,'') = ISNULL(H.ST_CD,'')
                AND P.ARTICLE_NUMBER = H.ARTICLE_NUMBER
                AND P.IS_CLOSED = 0
          )
    """)).scalar() or 0)

    print(f"  total_open_residual (STATUS='OPEN', residual>0)     : {total_open_residual_strict:.2f}")
    print(f"  endpoint_sum  (SUM of bdc_in_flight_qty per session): {endpoint_sum:.2f}")
    print(f"  orphan_residual (no matching open PEND_ALC row)     : {orphan_residual_strict:.2f}")
    gap = total_open_residual_strict - (endpoint_sum + orphan_residual_strict)
    print(f"  GAP = total - (endpoint + orphan)                   : {gap:.4f}")
    if abs(gap) < 0.01:
        print("  → conservation PASS (within 0.01)")
    else:
        print("  → conservation FAIL — investigating")

    # Same calc under relaxed filter, just for visibility
    total_open_residual_relaxed = float(conn.execute(text("""
        SELECT SUM(BDC_QTY - DO_RECEIVED)
        FROM ARS_BDC_HISTORY WITH (NOLOCK)
        WHERE UPPER(LTRIM(RTRIM(STATUS))) IN ('OPEN','PARTIAL')
          AND (BDC_QTY - DO_RECEIVED) > 0
    """)).scalar() or 0)
    relaxed_rows = conn.execute(text(ENDPOINT_SQL_RELAXED)).fetchall()
    endpoint_sum_relaxed = sum(float(r[8] or 0) for r in relaxed_rows)
    orphan_residual_relaxed = float(conn.execute(text("""
        SELECT SUM(H.BDC_QTY - H.DO_RECEIVED)
        FROM ARS_BDC_HISTORY H WITH (NOLOCK)
        WHERE UPPER(LTRIM(RTRIM(H.STATUS))) IN ('OPEN','PARTIAL')
          AND (H.BDC_QTY - H.DO_RECEIVED) > 0
          AND NOT EXISTS (
              SELECT 1 FROM ARS_PEND_ALC P WITH (NOLOCK)
              WHERE P.RDC = H.RDC
                AND ISNULL(P.ST_CD,'') = ISNULL(H.ST_CD,'')
                AND P.ARTICLE_NUMBER = H.ARTICLE_NUMBER
                AND P.IS_CLOSED = 0
          )
    """)).scalar() or 0)
    gap_relaxed = total_open_residual_relaxed - (endpoint_sum_relaxed + orphan_residual_relaxed)
    print()
    print("  --- under RELAXED filter (sanity preview) ---")
    print(f"  total_open_residual_relaxed : {total_open_residual_relaxed:.2f}")
    print(f"  endpoint_sum_relaxed        : {endpoint_sum_relaxed:.2f}")
    print(f"  orphan_residual_relaxed     : {orphan_residual_relaxed:.2f}")
    print(f"  GAP_relaxed                 : {gap_relaxed:.4f}")

    # -----------------------------------------------------------------
    # C. PEND_ALC invariants
    # -----------------------------------------------------------------
    section("C. PEND_ALC invariants (BDC_QTY<=ALLOC_QTY, DO_QTY<=BDC_QTY)")
    inv_bdc = conn.execute(text("""
        SELECT COUNT(*) FROM ARS_PEND_ALC WITH (NOLOCK)
        WHERE IS_CLOSED = 0 AND BDC_QTY > ALLOC_QTY
    """)).scalar()
    inv_do = conn.execute(text("""
        SELECT COUNT(*) FROM ARS_PEND_ALC WITH (NOLOCK)
        WHERE IS_CLOSED = 0 AND DO_QTY > BDC_QTY
    """)).scalar()
    print(f"  rows where BDC_QTY > ALLOC_QTY : {inv_bdc}")
    print(f"  rows where DO_QTY > BDC_QTY    : {inv_do}")

    if inv_bdc:
        print("  Sample BDC>ALLOC offenders:")
        for r in conn.execute(text("""
            SELECT TOP 10 ID, SESSION_ID, RDC, ST_CD, ARTICLE_NUMBER,
                   ALLOC_QTY, BDC_QTY, DO_QTY, PEND_QTY
            FROM ARS_PEND_ALC WITH (NOLOCK)
            WHERE IS_CLOSED = 0 AND BDC_QTY > ALLOC_QTY
            ORDER BY (BDC_QTY - ALLOC_QTY) DESC
        """)).fetchall():
            print(f"    ID={r[0]} SES={r[1]} {r[2]}/{r[3]}/{r[4]}  "
                  f"ALLOC={r[5]} BDC={r[6]} DO={r[7]} PEND={r[8]}")
    if inv_do:
        print("  Sample DO>BDC offenders:")
        for r in conn.execute(text("""
            SELECT TOP 10 ID, SESSION_ID, RDC, ST_CD, ARTICLE_NUMBER,
                   ALLOC_QTY, BDC_QTY, DO_QTY, PEND_QTY
            FROM ARS_PEND_ALC WITH (NOLOCK)
            WHERE IS_CLOSED = 0 AND DO_QTY > BDC_QTY
            ORDER BY (DO_QTY - BDC_QTY) DESC
        """)).fetchall():
            print(f"    ID={r[0]} SES={r[1]} {r[2]}/{r[3]}/{r[4]}  "
                  f"ALLOC={r[5]} BDC={r[6]} DO={r[7]} PEND={r[8]}")

    # -----------------------------------------------------------------
    # D. Per-row inflight cross-check (top 5 sessions)
    # -----------------------------------------------------------------
    section("D. Per-session inflight cross-check (top 5 by bdc_in_flight_qty)")
    top5 = sorted(endpoint_rows, key=lambda r: float(r[8] or 0), reverse=True)[:5]
    d_mismatches = []
    for r in top5:
        ses = r[0]
        endpoint_qty = float(r[8] or 0)
        recomputed = float(conn.execute(text("""
            ;WITH key_session_count AS (
                SELECT RDC, ISNULL(ST_CD,'') AS ST_CD, ARTICLE_NUMBER,
                       COUNT(DISTINCT SESSION_ID) AS n
                FROM ARS_PEND_ALC WITH (NOLOCK)
                WHERE IS_CLOSED = 0
                GROUP BY RDC, ISNULL(ST_CD,''), ARTICLE_NUMBER
            ),
            session_keys AS (
                SELECT DISTINCT P.RDC, ISNULL(P.ST_CD,'') AS ST_CD, P.ARTICLE_NUMBER
                FROM ARS_PEND_ALC P WITH (NOLOCK)
                WHERE P.IS_CLOSED = 0 AND P.SESSION_ID = :ses
            )
            SELECT SUM((H.BDC_QTY - H.DO_RECEIVED) * 1.0 / k.n)
            FROM ARS_BDC_HISTORY H WITH (NOLOCK)
            JOIN session_keys sk
              ON sk.RDC = H.RDC
             AND sk.ST_CD = ISNULL(H.ST_CD,'')
             AND sk.ARTICLE_NUMBER = H.ARTICLE_NUMBER
            JOIN key_session_count k
              ON k.RDC = sk.RDC AND k.ST_CD = sk.ST_CD
             AND k.ARTICLE_NUMBER = sk.ARTICLE_NUMBER
            WHERE H.STATUS = 'OPEN' AND (H.BDC_QTY - H.DO_RECEIVED) > 0
        """), {"ses": ses}).scalar() or 0)
        diff = endpoint_qty - recomputed
        flag = "OK" if abs(diff) < 0.01 else "MISMATCH"
        print(f"  SES={ses!r:<25} endpoint={endpoint_qty:>10.2f}  recomputed={recomputed:>10.2f}  Δ={diff:+.4f}  [{flag}]")
        if abs(diff) >= 0.01:
            d_mismatches.append((ses, endpoint_qty, recomputed, diff))

    # -----------------------------------------------------------------
    # E. Fair-split blast radius
    # -----------------------------------------------------------------
    section("E. Fair-split blast radius")
    fs = conn.execute(text("""
        ;WITH key_session_count AS (
            SELECT RDC, ISNULL(ST_CD,'') AS ST_CD, ARTICLE_NUMBER,
                   COUNT(DISTINCT SESSION_ID) AS n
            FROM ARS_PEND_ALC WITH (NOLOCK)
            WHERE IS_CLOSED = 0
            GROUP BY RDC, ISNULL(ST_CD,''), ARTICLE_NUMBER
        )
        SELECT
          SUM(CASE WHEN k.n > 1 THEN (H.BDC_QTY - H.DO_RECEIVED) ELSE 0 END) AS split_residual,
          SUM(CASE WHEN k.n = 1 THEN (H.BDC_QTY - H.DO_RECEIVED) ELSE 0 END) AS single_residual,
          SUM(H.BDC_QTY - H.DO_RECEIVED) AS total_matched_residual,
          SUM(CASE WHEN k.n > 1 THEN 1 ELSE 0 END) AS split_rows,
          SUM(CASE WHEN k.n = 1 THEN 1 ELSE 0 END) AS single_rows,
          MAX(k.n) AS max_overlap
        FROM ARS_BDC_HISTORY H WITH (NOLOCK)
        JOIN key_session_count k
          ON k.RDC = H.RDC
         AND k.ST_CD = ISNULL(H.ST_CD,'')
         AND k.ARTICLE_NUMBER = H.ARTICLE_NUMBER
        WHERE H.STATUS = 'OPEN' AND (H.BDC_QTY - H.DO_RECEIVED) > 0
    """)).fetchone()
    split_res = float(fs[0] or 0)
    single_res = float(fs[1] or 0)
    matched_total = float(fs[2] or 0)
    split_pct = (split_res / matched_total * 100.0) if matched_total else 0.0
    print(f"  split rows (n>1)        : {fs[3]}    residual={split_res:.2f}")
    print(f"  single rows (n=1)       : {fs[4]}    residual={single_res:.2f}")
    print(f"  total matched residual  : {matched_total:.2f}")
    print(f"  max overlap (max n)     : {fs[5]}")
    print(f"  → fair-split share of TOTAL OPEN residual : {split_res / total_open_residual_strict * 100.0 if total_open_residual_strict else 0:.2f}%")
    print(f"  → fair-split share of MATCHED residual    : {split_pct:.2f}%")

    # -----------------------------------------------------------------
    # Summary block
    # -----------------------------------------------------------------
    section("FINAL SUMMARY")
    A_pass = abs(gap) < 0.01
    B_changes = abs(relaxed_gap_residual) > 0.01
    C_pass = (inv_bdc == 0 and inv_do == 0)
    D_pass = (len(d_mismatches) == 0)
    print(f"  A (conservation): {'PASS' if A_pass else 'FAIL'}  gap={gap:.4f}")
    print(f"     total_open={total_open_residual_strict:.2f}  endpoint={endpoint_sum:.2f}  orphan={orphan_residual_strict:.2f}")
    print(f"  B (STATUS variants): relaxed-filter delta residual = {relaxed_gap_residual:.2f}  "
          f"{'CHANGES ANSWER' if B_changes else 'no material change'}")
    print(f"  C (PEND_ALC sanity): {'PASS' if C_pass else 'FAIL'}  BDC>ALLOC={inv_bdc}  DO>BDC={inv_do}")
    print(f"  D (per-session inflight): {'PASS' if D_pass else 'FAIL'}  mismatches={len(d_mismatches)}")
    print(f"  E (fair-split share of total): {split_res / total_open_residual_strict * 100.0 if total_open_residual_strict else 0:.2f}%")
