#!/usr/bin/env python
"""
Cancel (reject) parked listing sessions through the application's own path.

    python scripts/cancel_sessions.py SID [SID ...] [--yes] [--drop-header]

Calls parked_history.reject_parked for each session, which deletes its parked
rows across every snapshot target, clears its ARS_ALLOC_RDC_SPLIT rows and
reverts ARS_NL_TBL_HOLD_TRACKING to the pre-run state. --drop-header also
removes the ARS_LISTING_SESSIONS row so the run disappears from the run list
entirely.

Refuses any session that is still APPROVED — revert it first.
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


def state(c, sid: str) -> dict:
    def one(q):
        return c.execute(text(q), {"sid": sid}).scalar()
    return {
        "hdr":     one("SELECT COUNT(*) FROM ARS_LISTING_SESSIONS WHERE SESSION_ID=:sid"),
        "parked":  one("SELECT COUNT(*) FROM ARS_ALLOC_PARKED WHERE SESSION_ID=:sid"),
        "lparked": one("SELECT COUNT(*) FROM ARS_LISTING_PARKED WHERE SESSION_ID=:sid"),
        "history": one("SELECT COUNT(*) FROM ARS_ALLOC_HISTORY WHERE SESSION_ID=:sid"),
        "split":   one("SELECT COUNT(*) FROM ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID=:sid"),
        "pend":    one("SELECT COUNT(*) FROM ARS_PEND_ALC WHERE SESSION_ID=:sid"),
    }


def line(sid: str, s: dict) -> str:
    return (f"  {sid:<24} hdr={s['hdr']} parked={s['parked']:<8} "
            f"listing_parked={s['lparked']:<9} history={s['history']:<8} "
            f"split={s['split']:<7} pend={s['pend']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sids", nargs="+")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--drop-header", action="store_true")
    args = ap.parse_args()

    engine = get_data_engine()
    print("BEFORE")
    with engine.connect() as c:
        before = {s: state(c, s) for s in args.sids}
        for s in args.sids:
            print(line(s, before[s]))
        blocked = [s for s, v in before.items() if v["history"] or v["pend"]]
    if blocked:
        print(f"\nREFUSING — still approved (history/pend rows present): {blocked}")
        print("revert those first with scripts/revert_approved_session.py")
        return 1
    if not args.yes:
        print("\ndry run — pass --yes to cancel")
        return 0

    from app.services.parked_history import reject_parked
    print()
    for s in args.sids:
        try:
            res = reject_parked(s, "claude-code",
                                note="cancelled at user request")
            ok = res.get("success", True)
            summary = {k: v for k, v in res.items()
                       if k in ("rejected_by_table", "hold_revert", "error")}
            print(f"  {s:<24} -> {'OK' if ok else 'FAILED'}  {str(summary)[:150]}")
        except Exception as e:
            print(f"  {s:<24} -> EXCEPTION {e}")

    if args.drop_header:
        with engine.connect() as c:
            for s in args.sids:
                c.execute(text("DELETE FROM ARS_LISTING_SESSIONS WHERE SESSION_ID=:sid"),
                          {"sid": s})
            c.commit()
        print("\n  session header rows removed")

    print("\nAFTER")
    with engine.connect() as c:
        for s in args.sids:
            print(line(s, state(c, s)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
