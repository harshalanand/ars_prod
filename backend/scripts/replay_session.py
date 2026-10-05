#!/usr/bin/env python
"""
Replay a past listing session's EXACT parameters under a chosen RDC Scope.

    python scripts/replay_session.py --source 20260925_131654_512 --mode own
    python scripts/replay_session.py --source 20260925_131654_512 --mode all

Reads REQUEST_JSON from ARS_LISTING_SESSIONS, changes ONLY `rdc_mode`
(and `rdc_values`, which the endpoint reads only in `own`/`cross`), and
re-runs the pipeline. Every other knob — store list, MAJ_CATs, caps,
dispatch modes, dates — is carried over byte-for-byte, so any difference
in the output is attributable to the scope change alone.

The run parks itself, so its full ARS_ALLOC_WORKING lands in
ARS_ALLOC_PARKED under the new SESSION_ID and survives the next run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger                                  # noqa: E402
from sqlalchemy import text                                # noqa: E402
from app.database.session import get_data_engine           # noqa: E402

# Knobs the replay is allowed to change. Everything else is inherited.
_SCOPE_KEYS = {"rdc_mode", "rdc_values", "cross_from", "cross_to"}


def load_request(source_sid: str) -> dict:
    with get_data_engine().connect() as c:
        js = c.execute(
            text("SELECT REQUEST_JSON FROM ARS_LISTING_SESSIONS WHERE SESSION_ID=:s"),
            {"s": source_sid},
        ).scalar()
    if not js:
        raise SystemExit(f"session {source_sid} not found / has no REQUEST_JSON")
    return json.loads(js)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="session id to copy parameters from")
    ap.add_argument("--mode", required=True, choices=["own", "all", "cross"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    base = load_request(args.source)
    payload = dict(base)
    payload["rdc_mode"] = args.mode
    if args.mode == "own":
        # keep whatever the source run used; both RDCs in the baseline
        payload["rdc_values"] = base.get("rdc_values") or []
    else:
        # `all` clubs every warehouse — the endpoint ignores rdc_values here,
        # but blank it so the audit row cannot be misread as a filter.
        payload["rdc_values"] = []
    payload["cross_from"] = []
    payload["cross_to"] = []

    changed = {k: (base.get(k), payload.get(k))
               for k in payload if base.get(k) != payload.get(k)}
    unexpected = set(changed) - _SCOPE_KEYS
    if unexpected:
        raise SystemExit(f"refusing to run — non-scope keys changed: {unexpected}")

    print(f"source      : {args.source}")
    print(f"mode        : {base.get('rdc_mode')} -> {args.mode}")
    print(f"stores      : {len(payload.get('store_codes') or [])}")
    print(f"maj_cats    : {len(payload.get('maj_cat_values') or [])}")
    print(f"alloc_type  : {payload.get('alloc_type')}   "
          f"stock_dt={payload.get('stock_consider_dt')} "
          f"picking_dt={payload.get('picking_dt')}")
    print(f"changed keys: { {k: v for k, v in changed.items()} }")
    if args.dry_run:
        return 0

    from app.api.v1.endpoints.listing import GenerateRequest, _generate_listing_impl
    from app.services.listing_sessions import start_session, end_session

    req = GenerateRequest(**payload)
    now = datetime.now()
    sid = f"{now.strftime('%Y%m%d_%H%M%S')}_{now.microsecond // 1000:03d}"
    print(f"session_id  : {sid}")
    sys.stdout.flush()

    start_session(sid, f"replay_{args.mode}", req.model_dump())
    summary: dict = {}
    t0 = time.time()
    status = "FAILED"
    try:
        _generate_listing_impl(req, current_user=None, session_id=sid,
                               summary=summary, preset_batch_id=sid)
        status = "SUCCESS"
    finally:
        end_session(sid, status, summary)
        dur = time.time() - t0

    print(f"\n{status} in {dur:.0f}s")
    with get_data_engine().connect() as c:
        row = c.execute(text("""
            SELECT LISTED_OPTS, ALLOC_ROWS, SHIP_QTY_TOTAL, HOLD_QTY_TOTAL
              FROM ARS_LISTING_SESSIONS WHERE SESSION_ID=:s"""), {"s": sid}).fetchone()
        parked = c.execute(text(
            "SELECT COUNT(*) FROM ARS_ALLOC_PARKED WHERE SESSION_ID=:s"),
            {"s": sid}).scalar()
        # Since 2026-10-03 the split table is the per-run WORKING copy with no
        # SESSION_ID; this run's rows are in the parked snapshot by now
        # (Part 8.4 ran before we got here).
        split = c.execute(text(
            "SELECT COUNT(*) FROM ARS_ALLOC_RDC_SPLIT_PARKED WHERE SESSION_ID=:s"),
            {"s": sid}).scalar()
    print(f"listed_opts={row[0]} alloc_rows={row[1]} ship={row[2]} hold={row[3]}")
    print(f"parked_rows={parked}  split_rows={split}")
    print(f"REPLAY_SID={sid}")
    return 0 if status == "SUCCESS" else 1


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    raise SystemExit(main())
