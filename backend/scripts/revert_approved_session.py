#!/usr/bin/env python
"""
Revert an APPROVED listing session through the application's own undo path.

    python scripts/revert_approved_session.py <session_id> [--yes]

Uses pend_alc_service.revert_operation on the session's APPROVE operation,
which is the same code the Operations UI runs. That call:

  1. applies a -1 MSA/Grid delta (gives the reserved stock back)
  2. deletes the session's ARS_PEND_ALC rows
  3. restores ARS_NL_TBL_HOLD_TRACKING from the pre-approve snapshot
  4. re-seeds MSA HOLD_QTY / FNL_Q for the session
  5. demotes HISTORY -> PARKED so the run returns to the Parked Runs queue

Nothing is deleted outright — the session goes back to "awaiting review".
Prints before/after state so the effect is auditable.
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


def snapshot(c, sid: str) -> dict:
    def one(q, **kw):
        return c.execute(text(q), {"sid": sid, **kw}).scalar()
    return {
        "pend_rows":  one("SELECT COUNT(*) FROM ARS_PEND_ALC WHERE SESSION_ID=:sid"),
        "pend_qty":   one("SELECT ISNULL(SUM(TRY_CAST(PEND_QTY AS FLOAT)),0) "
                          "FROM ARS_PEND_ALC WHERE SESSION_ID=:sid"),
        "pend_open_total": one("SELECT ISNULL(SUM(TRY_CAST(PEND_QTY AS FLOAT)),0) "
                               "FROM ARS_PEND_ALC WHERE IS_CLOSED=0 AND PEND_QTY>0"),
        "hist_rows":  one("SELECT COUNT(*) FROM ARS_ALLOC_HISTORY WHERE SESSION_ID=:sid"),
        "parked_rows": one("SELECT COUNT(*) FROM ARS_ALLOC_PARKED WHERE SESSION_ID=:sid"),
        "split_rows": one("SELECT COUNT(*) FROM ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID=:sid"),
    }


def show(label: str, s: dict) -> None:
    print(f"  {label}")
    for k, v in s.items():
        print(f"    {k:<18} {v:>12,.0f}" if isinstance(v, (int, float))
              else f"    {k:<18} {v}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session_id")
    ap.add_argument("--yes", action="store_true", help="actually perform the revert")
    args = ap.parse_args()
    sid = args.session_id

    engine = get_data_engine()
    with engine.connect() as c:
        op = c.execute(text("""
            SELECT OP_ID, OP_TYPE, OP_KEY, OP_DATE, CREATED_BY,
                   ROWS_AFFECTED, QTY_TOTAL, REVERTED_AT
              FROM ARS_PEND_ALC_OPERATIONS
             WHERE OP_TYPE='APPROVE' AND OP_KEY=:sid
             ORDER BY OP_DATE DESC"""), {"sid": sid}).mappings().fetchall()
        if not op:
            print(f"no APPROVE operation found for {sid}")
            return 1
        print(f"APPROVE operations for {sid}:")
        for o in op:
            print(f"  op_id={o['OP_ID']}  {o['OP_DATE']}  by {o['CREATED_BY']}"
                  f"  rows={o['ROWS_AFFECTED']} qty={o['QTY_TOTAL']}"
                  f"  reverted={o['REVERTED_AT']}")
        active = [o for o in op if o["REVERTED_AT"] is None]
        if not active:
            print("\nall APPROVE ops already reverted — nothing to do")
            return 0
        target = active[0]
        before = snapshot(c, sid)

    print()
    show("BEFORE", before)
    if not args.yes:
        print("\ndry run — pass --yes to perform the revert")
        return 0

    print(f"\nreverting op_id={target['OP_ID']} ...")
    from app.services.pend_alc_service import revert_operation
    with engine.connect() as c:
        res = revert_operation(
            c, int(target["OP_ID"]), reverted_by="claude-code",
            note=f"reverting All-RDCs run {sid}: Part 2 fan-out inflated "
                 f"allocation (~3.1k phantom pcs) and pending (+6.2k)")
        try:
            c.commit()
        except Exception:
            pass

    print("\nrevert result:")
    for k, v in (res or {}).items():
        sv = str(v)
        print(f"  {k:<26} {sv[:160]}")

    with engine.connect() as c:
        after = snapshot(c, sid)
    print()
    show("AFTER", after)
    print("\n  deltas:")
    for k in before:
        d = (after[k] or 0) - (before[k] or 0)
        print(f"    {k:<18} {before[k]:>12,.0f} -> {after[k]:>12,.0f}  {d:+,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
