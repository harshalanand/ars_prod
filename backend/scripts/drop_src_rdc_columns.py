#!/usr/bin/env python
"""
Drop the redundant SRC_RDC column from the pending and hold ledgers.

    python scripts/drop_src_rdc_columns.py            # dry run
    python scripts/drop_src_rdc_columns.py --yes      # drop

RDC on these two tables now carries the SOURCING warehouse, which is what
ARS_PEND_ALC's documented grain always said it held and what MSA groups by:

    SELECT RDC, ARTICLE_NUMBER, SUM(PEND_QTY) ... GROUP BY RDC, ARTICLE_NUMBER
    FNL_Q = max(STK - PEND - HOLD, 0)      per (RDC, ARTICLE)

SRC_RDC was added alongside it on 2026-09-21 and is now redundant: it
duplicated the meaning while RDC kept the store's warehouse, which sent the
deduction to the wrong pool. Measured on approved session
20261003_130926_698: DH24 shipped 9,699 but was debited 2,046; DW01 shipped
3,031 and was debited 10,684 — 7,653 of 12,730 pcs against the wrong
warehouse.

The split tables keep their own SRC_RDC — that is the source of truth for
the picklist and is NOT touched here.

Refuses to drop a column that holds data, so a database where an All-RDCs
run has populated it must be reverted first.
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

TARGETS = [
    "ARS_PEND_ALC",
    "ARS_NL_TBL_HOLD_TRACKING",
    "ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    engine = get_data_engine()

    plan = []
    with engine.connect() as c:
        for t in TARGETS:
            if not c.execute(text(
                    f"SELECT CASE WHEN OBJECT_ID('dbo.{t}','U') IS NULL "
                    f"THEN 0 ELSE 1 END")).scalar():
                print(f"  {t:<38} table absent")
                continue
            if not c.execute(text(
                    f"SELECT CASE WHEN COL_LENGTH('dbo.{t}','SRC_RDC') IS NULL "
                    f"THEN 0 ELSE 1 END")).scalar():
                print(f"  {t:<38} no SRC_RDC column — already done")
                continue
            populated = c.execute(text(
                f"SELECT COUNT(*) FROM [{t}] WHERE SRC_RDC IS NOT NULL")).scalar() or 0
            total = c.execute(text(f"SELECT COUNT(*) FROM [{t}]")).scalar() or 0
            print(f"  {t:<38} rows={total:>12,}  SRC_RDC populated={populated:,}")
            if populated:
                print(f"      !! holds data — refusing. Revert the All-RDCs "
                      f"session(s) that wrote it, then re-run.")
                return 1
            plan.append(t)

    if not plan:
        print("\nnothing to drop")
        return 0
    if not args.yes:
        print(f"\ndry run — pass --yes to drop SRC_RDC from {len(plan)} table(s)")
        return 0

    with engine.connect() as c:
        for t in plan:
            # Any index or default on the column must go first, or the DROP
            # fails. Neither exists today, but check rather than assume.
            dflt = c.execute(text("""
                SELECT dc.name FROM sys.default_constraints dc
                  JOIN sys.columns col ON col.object_id = dc.parent_object_id
                                      AND col.column_id = dc.parent_column_id
                 WHERE dc.parent_object_id = OBJECT_ID('dbo.' + :t)
                   AND col.name = 'SRC_RDC'"""), {"t": t}).scalar()
            if dflt:
                c.execute(text(f"ALTER TABLE dbo.[{t}] DROP CONSTRAINT [{dflt}]"))
                print(f"  dropped default {dflt} on {t}")
            c.execute(text(f"ALTER TABLE dbo.[{t}] DROP COLUMN [SRC_RDC]"))
            c.commit()
            print(f"  dropped SRC_RDC from {t}")

    with engine.connect() as c:
        print("\nverify:")
        for t in TARGETS:
            if not c.execute(text(
                    f"SELECT CASE WHEN OBJECT_ID('dbo.{t}','U') IS NULL "
                    f"THEN 0 ELSE 1 END")).scalar():
                continue
            has = c.execute(text(
                f"SELECT CASE WHEN COL_LENGTH('dbo.{t}','SRC_RDC') IS NULL "
                f"THEN 0 ELSE 1 END")).scalar()
            print(f"  {t:<38} SRC_RDC present: {bool(has)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
