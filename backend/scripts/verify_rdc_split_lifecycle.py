#!/usr/bin/env python
"""
Prove the Option A lifecycle: approve -> revert -> re-approve preserves a
split line's per-warehouse breakdown.

    python scripts/verify_rdc_split_lifecycle.py <session_id> [--yes]

This is the defect the change exists to fix. Before 2026-10-03 the split rows
were DELETED on revert and never recreated, so the second approve could not
see that a 'MULTI' line was e.g. DH24 2 + DW01 4 — it fell back to the whole
line quantity and wrote the wrong per-source ARS_PEND_ALC.

Steps, each verified:

  0  baseline         split rows sit in _PARKED, nothing in _HISTORY
  1  approve          _PARKED -> _HISTORY, pend written; fingerprint it
  2  revert           _HISTORY -> _PARKED  (NOT deleted — the old bug)
  3  re-approve       _PARKED -> _HISTORY, pend written again
  4  compare          the two pend fingerprints must be IDENTICAL
  5  revert           leave nothing committed

The fingerprint is the full (WERKS, VAR_ART, SRC_RDC, qty) set from
ARS_PEND_ALC for the session — the thing that silently went wrong before.

Writes to ARS_PEND_ALC / hold tracking and reverts at the end; --yes required.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from sqlalchemy import text                                # noqa: E402
from app.database.session import get_data_engine           # noqa: E402

SPLIT = "ARS_ALLOC_RDC_SPLIT"


def counts(c, sid: str) -> dict:
    q = lambda s: c.execute(text(s), {"s": sid}).scalar() or 0   # noqa: E731
    return {
        "split_working": c.execute(text(f"SELECT COUNT(*) FROM [{SPLIT}]")).scalar() or 0,
        "split_parked":  q(f"SELECT COUNT(*) FROM [{SPLIT}_PARKED]  WHERE SESSION_ID=:s"),
        "split_history": q(f"SELECT COUNT(*) FROM [{SPLIT}_HISTORY] WHERE SESSION_ID=:s"),
        "alloc_parked":  q("SELECT COUNT(*) FROM ARS_ALLOC_PARKED  WHERE SESSION_ID=:s"),
        "alloc_history": q("SELECT COUNT(*) FROM ARS_ALLOC_HISTORY WHERE SESSION_ID=:s"),
        "pend_rows":     q("SELECT COUNT(*) FROM ARS_PEND_ALC WHERE SESSION_ID=:s"),
        "pend_qty":      q("SELECT ISNULL(SUM(TRY_CAST(PEND_QTY AS FLOAT)),0) "
                           "FROM ARS_PEND_ALC WHERE SESSION_ID=:s"),
    }


def show(label: str, d: dict) -> None:
    print(f"  {label:<14} " + "  ".join(f"{k}={v:,}" if isinstance(v, int)
                                        else f"{k}={v:,.0f}" for k, v in d.items()))


def fingerprint(c, sid: str) -> list[tuple]:
    """Per-source pending for the session — the value the old code got wrong."""
    # ARS_PEND_ALC is keyed on ST_CD / ARTICLE_NUMBER, not the alloc table's
    # WERKS / VAR_ART. SRC_RDC is the column this change exists to protect.
    return [tuple(r) for r in c.execute(text("""
        SELECT ST_CD, ARTICLE_NUMBER, ISNULL(SRC_RDC,'~') AS SRC_RDC,
               ROUND(SUM(TRY_CAST(PEND_QTY AS FLOAT)), 3) AS QTY
          FROM ARS_PEND_ALC WHERE SESSION_ID = :s
         GROUP BY ST_CD, ARTICLE_NUMBER, ISNULL(SRC_RDC,'~')
         ORDER BY ST_CD, ARTICLE_NUMBER, ISNULL(SRC_RDC,'~')"""), {"s": sid}).fetchall()]


def multi_lines(c, sid: str, table: str) -> int:
    """Lines sourced from more than one warehouse — the ones that break."""
    return c.execute(text(f"""
        SELECT COUNT(*) FROM (
            SELECT WERKS, VAR_ART, SZ FROM [{table}] WHERE SESSION_ID = :s
             GROUP BY WERKS, VAR_ART, SZ HAVING COUNT(DISTINCT SRC_RDC) > 1) X
    """), {"s": sid}).scalar() or 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session_id")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    sid = args.session_id
    engine = get_data_engine()

    with engine.connect() as c:
        print("STEP 0 — baseline")
        base = counts(c, sid)
        show("baseline", base)
        mp = multi_lines(c, sid, SPLIT + "_PARKED")
        mh = multi_lines(c, sid, SPLIT + "_HISTORY")
        print(f"  split lines with >1 source: parked={mp}  history={mh}")
        if mp + mh == 0:
            print("  !! no multi-source lines — the fingerprints would match "
                  "trivially and prove nothing")
        # Accept an already-approved session: the rows are then in _HISTORY,
        # which is a valid resume point (STEP 1 detects the active APPROVE op
        # and skips straight to the revert).
        if base["split_parked"] == 0 and base["split_history"] == 0:
            print("\n  no split rows for this session in either copy — "
                  "nothing to prove")
            return 1
    if not args.yes:
        print("\ndry run — pass --yes to run the approve/revert cycle")
        return 0

    from app.services.parked_history import approve_parked
    from app.services.pend_alc_service import revert_operation

    def do_revert(tag: str) -> None:
        with engine.connect() as c:
            op = c.execute(text("""
                SELECT TOP 1 OP_ID FROM ARS_PEND_ALC_OPERATIONS
                 WHERE OP_TYPE='APPROVE' AND OP_KEY=:s AND REVERTED_AT IS NULL
                 ORDER BY OP_DATE DESC"""), {"s": sid}).scalar()
            if not op:
                print(f"  [{tag}] no active APPROVE op to revert")
                return
            res = revert_operation(c, int(op), reverted_by="lifecycle-verify",
                                   note="Option A lifecycle verification")
            try:
                c.commit()
            except Exception:
                pass
            print(f"  [{tag}] revert op {op}: success={res.get('success')} "
                  f"pend_deleted={res.get('pend_alc_rows_deleted')} "
                  f"demoted={res.get('demoted_by_table')}")

    print("\nSTEP 1 — approve")
    r1 = approve_parked(sid, "lifecycle-verify")
    print(f"  approve: success={r1.get('success')} by_table={r1.get('approved_by_table')}")
    with engine.connect() as c:
        a1 = counts(c, sid)
        show("after approve", a1)
        fp1 = fingerprint(c, sid)
        print(f"  pend fingerprint rows: {len(fp1):,}")
        print(f"  split lines with >1 source (history): "
              f"{multi_lines(c, sid, SPLIT + '_HISTORY')}")

    print("\nSTEP 2 — revert  (the old code DELETED the split rows here)")
    do_revert("revert-1")
    with engine.connect() as c:
        a2 = counts(c, sid)
        show("after revert", a2)
        # Compare against what was in HISTORY immediately before the revert,
        # not against the STEP 0 baseline: when the script resumes an
        # already-approved session, baseline split_parked is legitimately 0
        # and comparing to it reported a false FAIL.
        kept, had = a2["split_parked"], a1["split_history"]
        print(f"  split rows back in _PARKED: {kept:,} (history held {had:,})   "
              f"{'PASS — demoted, not deleted' if kept == had and kept > 0 else 'FAIL — lost'}")

    print("\nSTEP 3 — re-approve")
    r3 = approve_parked(sid, "lifecycle-verify")
    print(f"  approve: success={r3.get('success')}")
    with engine.connect() as c:
        a3 = counts(c, sid)
        show("after re-appr", a3)
        fp2 = fingerprint(c, sid)
        print(f"  pend fingerprint rows: {len(fp2):,}")

    print("\nSTEP 4 — compare the two pend fingerprints")
    if fp1 == fp2:
        print(f"  IDENTICAL across {len(fp1):,} (store, article, source) rows   PASS")
    else:
        only1 = [x for x in fp1 if x not in set(fp2)]
        only2 = [x for x in fp2 if x not in set(fp1)]
        print(f"  DIFFER — {len(only1)} only in first, {len(only2)} only in second   FAIL")
        for x in only1[:5]:
            print(f"    1st only: {x}")
        for x in only2[:5]:
            print(f"    2nd only: {x}")

    print("\nSTEP 5 — revert, leaving nothing committed")
    do_revert("revert-2")
    with engine.connect() as c:
        show("final", counts(c, sid))
    return 0 if fp1 == fp2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
