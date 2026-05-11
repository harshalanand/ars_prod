"""
reset_transactional_data.py
===========================
CLI to wipe all transactional data and return the app to a fresh,
zero-transaction state. Master / RBAC / RLS / config tables are preserved.

The list of tables is REDISCOVERED on every run via
app.services.reset_service — adding a new transactional table that follows
the project's naming conventions (ARS_*_PARKED, *_HISTORY, *_WORKING,
ARS_MSA_*, alloc_*, *_jobs, audit_log, …) requires no code change here.

Usage
-----
    # Preview what would be cleared (no writes)
    python -m scripts.reset_transactional_data --dry-run

    # Actually clear (asks for confirmation)
    python -m scripts.reset_transactional_data

    # Skip the confirmation prompt
    python -m scripts.reset_transactional_data --yes

    # Deep reset: ALSO clear MSA sequence audit + user-defined schedules
    python -m scripts.reset_transactional_data --yes --include-msa-tracking

    # JSON output (for scripting / CI)
    python -m scripts.reset_transactional_data --dry-run --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List


# Make `app.*` importable when run as `python scripts/reset_transactional_data.py`
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from app.services.reset_service import reset_transactional_data  # noqa: E402


def _print_table(rows: List[dict], cols: List[str]) -> None:
    if not rows:
        print("  (none)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    header = " | ".join(c.ljust(widths[c]) for c in cols)
    print("  " + header)
    print("  " + "-+-".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  " + " | ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def main() -> int:
    p = argparse.ArgumentParser(
        description="Reset ARS_V2 to zero transactional data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be cleared, do not write.")
    p.add_argument("--yes", action="store_true",
                   help="Skip the confirmation prompt.")
    p.add_argument("--include-msa-tracking", action="store_true",
                   help="Also clear MSA_Calculation_Sequence, "
                        "MSA_Column_Definitions, and ARS_PEND_ALC_SCHEDULE.")
    p.add_argument("--json", action="store_true",
                   help="Print the full report as JSON instead of a table.")
    args = p.parse_args()

    if not args.dry_run and not args.yes:
        print("\n>>> THIS WILL DELETE ALL TRANSACTIONAL DATA in BOTH databases.")
        print(">>> Master / RBAC / RLS / config tables are preserved.")
        if args.include_msa_tracking:
            print(">>> --include-msa-tracking is ON: MSA sequence audit "
                  "and user schedules will ALSO be cleared.")
        ans = input(">>> Type 'RESET' to confirm: ").strip()
        if ans != "RESET":
            print("Aborted.")
            return 1

    report = reset_transactional_data(
        dry_run=args.dry_run,
        include_msa_tracking=args.include_msa_tracking,
    )
    out = report.to_dict()

    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return 0 if not out["totals"]["errors"] else 2

    print()
    print("=" * 70)
    print("ARS_V2 — Transactional data reset")
    print(f"  dry_run             : {out['dry_run']}")
    print(f"  include_msa_tracking: {out['include_msa_tracking']}")
    print(f"  cleared             : {out['totals']['cleared']} table(s)")
    print(f"  rows deleted        : {out['totals']['rows_deleted']:,}")
    print(f"  skipped             : {out['totals']['skipped']} table(s)")
    print(f"  errors              : {out['totals']['errors']}")
    print("=" * 70)

    print("\nCLEARED")
    _print_table(out["cleared"], ["db", "table", "method", "rows_before", "rows_after"])

    if out["errors"]:
        print("\nERRORS")
        _print_table(out["errors"], ["db", "table", "error"])

    print(f"\nSKIPPED (protected / non-transactional): {len(out['skipped'])} table(s)")
    print("  (use --json to see the full skipped list)")
    return 0 if not out["totals"]["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
