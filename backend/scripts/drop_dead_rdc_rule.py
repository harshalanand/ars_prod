#!/usr/bin/env python
"""
Delete the dead ALC_RDC_CENTRAL_POOL business-rule row.

    python scripts/drop_dead_rdc_rule.py            # dry run
    python scripts/drop_dead_rdc_rule.py --yes      # delete

The rule was the original on/off switch for central pooling. It was removed
in September when RDC Scope became the single control ("CONCEPT SHOULD BE
ONLY IN TUNABLE PARAMETERS HAVE RDC SCOPE"), but the seeded row stayed in
ARS_BUSINESS_RULES and still shows on Settings -> Business Rules, where it
reads as a live switch that does nothing.

Verified dead before writing this: no rule_flag() / rule_value() lookup
anywhere in backend/app or frontend/src — the only remaining mentions are
five stale code comments.
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

KEY = "ALC_RDC_CENTRAL_POOL"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    engine = get_data_engine()

    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, module, rule_key, rule_name, rule_value, default_value,
                   is_active, is_wired, updated_by, updated_at
              FROM ARS_BUSINESS_RULES WHERE rule_key = :k
        """), {"k": KEY}).mappings().fetchall()

    print(f"rows matching {KEY}: {len(rows)}")
    for r in rows:
        print(f"  id={r['id']}  module={r['module']}  value={r['rule_value']!r}  "
              f"default={r['default_value']!r}  active={r['is_active']}  "
              f"wired={r['is_wired']}  by={r['updated_by']} {r['updated_at']}")

    if len(rows) == 0:
        print("\nalready gone — nothing to do")
        return 0
    if len(rows) != 1:
        print("\nexpected exactly one row — refusing to delete several")
        return 1
    if any(r["is_active"] for r in rows):
        print("\nrow is ACTIVE — refusing. Deactivate and re-check first.")
        return 1
    if not args.yes:
        print("\ndry run — pass --yes to delete")
        return 0

    with engine.connect() as c:
        n = c.execute(text("DELETE FROM ARS_BUSINESS_RULES WHERE rule_key = :k"),
                      {"k": KEY}).rowcount
        c.commit()
    print(f"\n  deleted {n} row")

    with engine.connect() as c:
        print("\nremaining ALC_RDC* rules:")
        for x in c.execute(text("""
                SELECT rule_key, rule_value, is_active FROM ARS_BUSINESS_RULES
                 WHERE rule_key LIKE 'ALC_RDC%' ORDER BY rule_key""")):
            print(f"  {x[0]:<24} value={str(x[1]):<16} active={x[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
