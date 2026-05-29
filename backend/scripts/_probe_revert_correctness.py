"""Probe revert correctness — focused on the 'when we revert than not perform
correct' issue Santosh reported. Inspects:
1. Whether the existing reverted MANUAL op left the MSA/grid in a consistent
   state — i.e., does the -1 delta exactly cancel the +1 delta?
2. Sample PAYLOAD shape on all op_types to find missing reverse-delta fields.
3. DO/BDC revert: does the safety gate actually catch a multi-DO sequence?
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.session import get_data_engine
from sqlalchemy import text
import json

eng = get_data_engine()


def hdr(s):
    print(); print("=" * 78); print(s); print("=" * 78)


hdr("1. The 1 reverted op — full payload + summary")
with eng.connect() as c:
    rev = c.execute(text("""
        SELECT OP_ID, OP_TYPE, OP_KEY, OP_DATE, REVERTED_AT,
               ROWS_AFFECTED, QTY_TOTAL, SUMMARY, PAYLOAD
        FROM ARS_PEND_ALC_OPERATIONS
        WHERE REVERTED_AT IS NOT NULL
    """)).fetchall()
for r in rev:
    print(f"OP_ID={r[0]} TYPE={r[1]} KEY={r[2]}")
    print(f"  date={r[3]} reverted_at={r[4]} rows={r[5]} qty={r[6]}")
    print(f"  summary={r[7]}")
    try:
        p = json.loads(r[8])
        print(f"  payload keys: {list(p.keys()) if isinstance(p, dict) else p}")
        for k in ('inserted_ids', 'stamped_rows', 'pend_updates',
                  'history_updates', 'article_rdc_pairs'):
            v = p.get(k) if isinstance(p, dict) else None
            if v is not None:
                print(f"    {k}: type={type(v).__name__} len={len(v) if hasattr(v,'__len__') else '-'}")
                if isinstance(v, list) and v:
                    print(f"      sample[0] = {v[0]}")
    except Exception as e:
        print(f"  payload parse err: {e}")


hdr("2. For the reverted MANUAL op — check what's left in ARS_PEND_ALC")
if rev:
    key = rev[0][2]
    with eng.connect() as c:
        r = c.execute(text("""
            SELECT COUNT(*), ISNULL(SUM(ALLOC_QTY),0), ISNULL(SUM(DO_QTY),0), ISNULL(SUM(PEND_QTY),0)
            FROM ARS_PEND_ALC WHERE SESSION_ID = :s
        """), {"s": key}).fetchone()
        print(f"  PEND_ALC remaining for SESSION_ID={key}: "
              f"rows={r[0]} alloc={r[1]} do={r[2]} pend={r[3]}")


hdr("3. Sample of un-reverted op PAYLOADs by type (look at delta payload coverage)")
with eng.connect() as c:
    for op_type in ('MANUAL', 'BDC', 'DO'):
        rows = c.execute(text("""
            SELECT TOP 1 OP_ID, OP_KEY, ROWS_AFFECTED, QTY_TOTAL,
                   LEFT(PAYLOAD, 1000) as P
            FROM ARS_PEND_ALC_OPERATIONS
            WHERE OP_TYPE = :t AND REVERTED_AT IS NULL
            ORDER BY OP_DATE DESC
        """), {"t": op_type}).fetchall()
        for r in rows:
            print(f"\n {op_type} op {r[0]} ({r[1]}) rows={r[2]} qty={r[3]}")
            try:
                p = json.loads(r[4] + ("..." if len(r[4]) >= 1000 else ""))
                if isinstance(p, dict):
                    for k, v in p.items():
                        if isinstance(v, list):
                            print(f"    {k}: list len={len(v)} sample={v[0] if v else None}")
                        else:
                            print(f"    {k}: {v!r:.120}")
            except Exception as e:
                print(f"    truncated payload (full was probably valid JSON): "
                      f"{r[4][:200]}...")


hdr("4. ARS_NL_TBL_HOLD_TRACKING: any rows touched by DO updates "
    "but PEND_ALC op_log won't reverse?")
# apply_do_deductions also decrements HOLD_REM (via the temp-table UPDATE
# at line ~1912 in pend_alc_service.py). But the DO op's PAYLOAD records
# only pend_updates + history_updates — NOT hold updates. So _revert_do
# CANNOT restore HOLD_REM.
with eng.connect() as c:
    n_do_ops = c.execute(text(
        "SELECT COUNT(*) FROM ARS_PEND_ALC_OPERATIONS WHERE OP_TYPE='DO'"
    )).scalar()
    print(f"  Total DO operations logged: {n_do_ops}")
    # Look at the most recent un-reverted DO op and check if its payload
    # carries any hold-related keys
    sample = c.execute(text("""
        SELECT TOP 1 OP_ID, LEFT(PAYLOAD, 4000)
        FROM ARS_PEND_ALC_OPERATIONS
        WHERE OP_TYPE='DO' AND REVERTED_AT IS NULL
        ORDER BY OP_DATE DESC
    """)).fetchone()
    if sample:
        try:
            p = json.loads(sample[1])
            print(f"  Latest DO op {sample[0]} payload keys: {list(p.keys())}")
            has_hold = any('hold' in k.lower() for k in p.keys())
            print(f"  Payload contains a 'hold' key? {has_hold}")
        except Exception as e:
            print(f"  parse err: {e}")
    else:
        print("  No DO operations to inspect.")


hdr("5. msa_adjusted symmetry — recompute what _revert_manual would do "
    "to ARS_MSA_TOTAL for the existing reverted op, and check if it cleanly "
    "balances the +1 delta from the original write.")
# We can't time-travel; just compute the current ARS_MSA_TOTAL.PEND_QTY for the
# session's articles. Expect = sum of PEND_ALC.PEND_QTY for open rows on those
# (RDC, ARTICLE) keys. If MSA.PEND_QTY > sum_of_open_PEND for the same keys,
# the revert under-deducted.
if rev:
    key = rev[0][2]
    with eng.connect() as c:
        # Articles + RDC from the original session (read back from any
        # still-present PEND_ALC for that session, even after revert)
        affected = c.execute(text("""
            SELECT DISTINCT RDC, ARTICLE_NUMBER
            FROM ARS_PEND_ALC
            WHERE SESSION_ID = :s
        """), {"s": key}).fetchall()
        print(f"  PEND_ALC rows still present for session {key}: {len(affected)} "
              f"(0 expected after MANUAL revert, since revert DELETEs the rows)")

# Even though the session was revert-deleted, we can still confirm symmetry
# by picking a sample of (RDC, ARTICLE) keys present in ARS_PEND_ALC and
# comparing MSA.PEND_QTY vs sum(open PEND_ALC.PEND_QTY).
hdr("6. INVARIANT CHECK: MSA.PEND_QTY == SUM(open PEND_ALC.PEND_QTY) per "
    "(RDC, ARTICLE)?")
with eng.connect() as c:
    rows = c.execute(text("""
        SELECT TOP 5
            T.RDC, T.ARTICLE_NUMBER, T.PEND_QTY AS msa_pend,
            ISNULL(P.q, 0) AS pend_alc_q,
            T.PEND_QTY - ISNULL(P.q, 0) AS delta,
            T.STK_QTY, T.HOLD_QTY, T.FNL_Q
        FROM ARS_MSA_TOTAL T
        LEFT JOIN (
            SELECT RDC, ARTICLE_NUMBER, SUM(PEND_QTY) AS q
            FROM ARS_PEND_ALC
            WHERE IS_CLOSED = 0
            GROUP BY RDC, ARTICLE_NUMBER
        ) P ON T.RDC = P.RDC AND T.ARTICLE_NUMBER = P.ARTICLE_NUMBER
        WHERE T.PEND_QTY <> ISNULL(P.q, 0)
        ORDER BY ABS(T.PEND_QTY - ISNULL(P.q, 0)) DESC
    """)).fetchall()
    print("  Top 5 rows where MSA.PEND_QTY drifts from open PEND_ALC sum:")
    print("    (rdc, art, msa_pend, alc_pend, delta, stk, hold, fnl)")
    for r in rows:
        print(f"   {tuple(r)}")
    n_total = c.execute(text("""
        SELECT COUNT(*), SUM(ABS(T.PEND_QTY - ISNULL(P.q, 0)))
        FROM ARS_MSA_TOTAL T
        LEFT JOIN (
            SELECT RDC, ARTICLE_NUMBER, SUM(PEND_QTY) AS q
            FROM ARS_PEND_ALC
            WHERE IS_CLOSED = 0
            GROUP BY RDC, ARTICLE_NUMBER
        ) P ON T.RDC = P.RDC AND T.ARTICLE_NUMBER = P.ARTICLE_NUMBER
        WHERE T.PEND_QTY <> ISNULL(P.q, 0)
    """)).fetchone()
    delta_sum = float(n_total[1] or 0)
    print(f"\n  Total MSA rows with PEND_QTY != open PEND_ALC sum: "
          f"{n_total[0]} (total |delta| = {delta_sum:.0f})")

hdr("7. Same invariant for ARS_GRID_MJ_VND_CD — does its PEND_ALC double-count?")
# VND_CD has 3.7M PEND_ALC vs 1.2M everywhere else. Suggests the delta is
# being applied multiple times because of multi-vendor article splits.
with eng.connect() as c:
    r = c.execute(text("""
        SELECT COUNT(*), SUM(STK_TTL), SUM(PEND_ALC)
        FROM ARS_GRID_MJ_VND_CD
    """)).fetchone()
    print(f"  VND_CD grid: rows={r[0]} STK_TTL_SUM={r[1]:.0f} PEND_ALC_SUM={r[2]:.0f}")
    # Probe MAJ_CAT explosion factor for VND_CD per article
    fanout = c.execute(text("""
        SELECT TOP 10 ARTICLE_NUMBER, COUNT(DISTINCT VND_CD) AS vendors_per_article
        FROM vw_master_product
        WHERE ARTICLE_NUMBER IS NOT NULL AND VND_CD IS NOT NULL
        GROUP BY ARTICLE_NUMBER
        HAVING COUNT(DISTINCT VND_CD) > 1
        ORDER BY COUNT(DISTINCT VND_CD) DESC
    """)).fetchall()
    print("  Articles with > 1 VND_CD on master product:")
    for f in fanout:
        print(f"    {f[0]}: {f[1]} vendor codes")
