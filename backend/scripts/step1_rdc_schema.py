#!/usr/bin/env python
"""
Step 1 of the Central RDC Pool change — schema and switch only.

Spec: docs/superpowers/specs/2026-09-21-central-rdc-pool-allocation-brd-fsd-v1.5.md
      Part C, Step 1.

ADDITIVE ONLY. Every change is a new nullable column or a new table:

  * ARS_ALLOC_RDC_SPLIT                        new table + 2 indexes
  * ARS_PEND_ALC.SRC_RDC                       new NULL column (17.9 M rows)
  * ARS_NL_TBL_HOLD_TRACKING.SRC_RDC           new NULL column (2.75 M rows)
  * ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT.SRC_RDC  new NULL column (6.56 M rows)
  * 6 business rules seeded, ALC_RDC_CENTRAL_POOL INACTIVE

A nullable column with no default is a metadata-only ALTER in SQL Server —
instant even at 17.9 M rows, no table rewrite, no blocking scan.

Nothing reads any of this yet. Behaviour is unchanged by construction: the
only code path touched is the SELECT..INTO that rebuilds ARS_ALLOC_WORKING,
which now emits two extra all-NULL columns.

    python scripts/step1_rdc_schema.py --apply
    python scripts/step1_rdc_schema.py --verify
    python scripts/step1_rdc_schema.py --rollback    # drops all of it
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text                       # noqa: E402
from app.database.session import get_data_engine  # noqa: E402
from app.services.rdc_split_service import (      # noqa: E402
    SPLIT_TABLE, ensure_split_table,
)

LEDGERS = [
    "ARS_PEND_ALC",
    "ARS_NL_TBL_HOLD_TRACKING",
    "ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT",
]

RULE_KEYS = [
    "ALC_RDC_CENTRAL_POOL",
    "ALC_RDC_SPLIT_POLICY",
    "ALC_RDC_PRIORITY",
    "ALC_RDC_MAX_SPLIT",
    "ALC_RDC_HOLD_SPLIT",
    "ALC_MIN_CROSS_SHIP_QTY",
]


def _has_col(c, tbl: str, col: str) -> bool:
    return bool(c.execute(
        text("SELECT CASE WHEN COL_LENGTH(:t,:c) IS NULL THEN 0 ELSE 1 END"),
        {"t": tbl, "c": col},
    ).scalar())


def _exists(c, tbl: str) -> bool:
    return bool(c.execute(
        text("SELECT CASE WHEN OBJECT_ID(:t,'U') IS NULL THEN 0 ELSE 1 END"), {"t": tbl}
    ).scalar())


def apply(c) -> None:
    print("--- table ---")
    ensure_split_table(c)
    print(f"  {SPLIT_TABLE}: {'present' if _exists(c, SPLIT_TABLE) else 'MISSING'}")
    for r in c.execute(text(
        f"SELECT name FROM sys.indexes WHERE object_id=OBJECT_ID('{SPLIT_TABLE}') "
        f"AND name IS NOT NULL ORDER BY name"
    )):
        print(f"    index {r[0]}")

    print("\n--- ledger columns (metadata-only ALTERs) ---")
    for t in LEDGERS:
        if not _exists(c, t):
            print(f"  {t:<38} table absent — skipped")
            continue
        if _has_col(c, t, "SRC_RDC"):
            print(f"  {t:<38} SRC_RDC already present")
            continue
        c.execute(text(f"ALTER TABLE [{t}] ADD [SRC_RDC] NVARCHAR(20) NULL"))
        print(f"  {t:<38} SRC_RDC added")

    c.commit()

    print("\n--- business rules ---")
    # ensure_tables() commits on its own connection-handling terms, so it is
    # given a fresh connection rather than being nested inside ours — a
    # commit from inside a caller's `begin()` block closes that transaction
    # and every later statement on it fails.
    from app.services import business_rules as br
    with get_data_engine().connect() as bc:
        br.ensure_tables(bc)
    br._load_all(force=True)
    for k in RULE_KEYS:
        row = c.execute(text(
            "SELECT rule_value, is_active, is_wired, value_type, choices "
            "FROM ARS_BUSINESS_RULES WHERE rule_key = :k"
        ), {"k": k}).first()
        if not row:
            print(f"  {k:<26} NOT SEEDED")
            continue
        print(f"  {k:<26} value={str(row[0]):<16} active={row[1]} "
              f"wired={row[2]} type={row[3]}")


def verify(c) -> int:
    bad = 0
    print("--- V-Step1a: schema present ---")
    ok = _exists(c, SPLIT_TABLE)
    print(f"  {SPLIT_TABLE:<38} {'OK' if ok else 'MISSING'}")
    bad += 0 if ok else 1
    for t in LEDGERS:
        ok = _has_col(c, t, "SRC_RDC")
        print(f"  {t + '.SRC_RDC':<38} {'OK' if ok else 'MISSING'}")
        bad += 0 if ok else 1

    print("\n--- V-Step1b: nothing written yet (I-11 / V14 precondition) ---")
    n = c.execute(text(f"SELECT COUNT(*) FROM [{SPLIT_TABLE}]")).scalar()
    print(f"  {SPLIT_TABLE + ' rows':<38} {n:>12,}  {'OK' if n == 0 else 'UNEXPECTED'}")
    bad += 0 if n == 0 else 1
    for t in LEDGERS:
        if not _has_col(c, t, "SRC_RDC"):
            continue
        n = c.execute(text(f"SELECT COUNT_BIG(*) FROM [{t}] WHERE SRC_RDC IS NOT NULL")).scalar()
        print(f"  {t + ' non-null SRC_RDC':<38} {n:>12,}  {'OK' if n == 0 else 'UNEXPECTED'}")
        bad += 0 if n == 0 else 1

    print("\n--- V-Step1c: the switch is OFF ---")
    row = c.execute(text(
        "SELECT rule_value, is_active, is_wired FROM ARS_BUSINESS_RULES "
        "WHERE rule_key = 'ALC_RDC_CENTRAL_POOL'"
    )).first()
    if not row:
        print("  ALC_RDC_CENTRAL_POOL           NOT SEEDED")
        bad += 1
    else:
        ok = (row[1] == 0)
        print(f"  ALC_RDC_CENTRAL_POOL           active={row[1]} wired={row[2]}  "
              f"{'OK (inactive)' if ok else 'UNEXPECTED — should be inactive'}")
        bad += 0 if ok else 1

    print("\n--- V-Step1d: ledger totals unchanged vs Step 0 baseline ---")
    expect = {
        "ARS_PEND_ALC": 17_948_674,
        "ARS_NL_TBL_HOLD_TRACKING": 2_750_316,
        "ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT": 6_558_761,
    }
    for t, want in expect.items():
        got = c.execute(text(f"SELECT COUNT_BIG(*) FROM [{t}]")).scalar()
        delta = got - want
        note = "OK" if delta == 0 else f"delta {delta:+,} (growth is fine; a DROP is not)"
        print(f"  {t:<38} {got:>12,}  {note}")

    print(f"\n{'STEP 1 VERIFY: PASS' if bad == 0 else f'STEP 1 VERIFY: {bad} PROBLEM(S)'}")
    return bad


def rollback(c) -> None:
    print("--- rollback ---")
    for t in LEDGERS:
        if _has_col(c, t, "SRC_RDC"):
            c.execute(text(f"ALTER TABLE [{t}] DROP COLUMN [SRC_RDC]"))
            print(f"  {t:<38} SRC_RDC dropped")
    if _exists(c, SPLIT_TABLE):
        c.execute(text(f"DROP TABLE dbo.{SPLIT_TABLE}"))
        print(f"  {SPLIT_TABLE:<38} dropped")
    keys = "','".join(RULE_KEYS)
    c.execute(text(f"DELETE FROM ARS_BUSINESS_RULES WHERE rule_key IN ('{keys}')"))
    print(f"  {len(RULE_KEYS)} business rules removed")
    print("\nNOTE: SRC_RDC / SRC_SPLIT_CNT on ARS_ALLOC_WORKING are emitted by the"
          "\n      SELECT..INTO in rule_engine_new.py — revert that code edit to"
          "\n      remove them; they vanish on the next Generate.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Step 1 — schema and switch only")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--apply", action="store_true")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--rollback", action="store_true")
    args = ap.parse_args()

    eng = get_data_engine()
    with eng.connect() as c:
        print(f"connected: {c.execute(text('SELECT DB_NAME()')).scalar()} "
              f"@ {c.execute(text('SELECT @@SERVERNAME')).scalar()}\n")
        if args.apply:
            apply(c)
            c.commit()
            print()
            return verify(c)
        if args.verify:
            return verify(c)
        rollback(c)
        c.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
