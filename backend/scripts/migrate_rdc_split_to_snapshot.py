#!/usr/bin/env python
"""
Convert ARS_ALLOC_RDC_SPLIT from a durable session-keyed table into the
working → parked → history trio every other listing output already uses.

    python scripts/migrate_rdc_split_to_snapshot.py            # dry run
    python scripts/migrate_rdc_split_to_snapshot.py --yes      # apply

Before (pre-2026-10-03)
    ARS_ALLOC_RDC_SPLIT   one durable table, SESSION_ID in the PK,
                          rows deleted on reject and on revert

After
    ARS_ALLOC_RDC_SPLIT           working — the current run only, no SESSION_ID
    ARS_ALLOC_RDC_SPLIT_PARKED    snapshot per parked session
    ARS_ALLOC_RDC_SPLIT_HISTORY   promoted on Approve

What this does with the rows already in the table, per session:

  * session still PARKED (has ARS_ALLOC_PARKED rows)
        -> copied into _PARKED with PARK_STATUS='PARKED' and
           PARKED_AT taken from CREATED_AT
  * session APPROVED (has ARS_ALLOC_HISTORY rows)
        -> copied into _HISTORY, APPROVED_AT/BY read from the alloc history
  * anything else (the allocation data is gone)
        -> DROPPED. These are orphans: the picklist outlived the run it
           describes and still reads as a live warehouse instruction, which
           is one of the defects this change exists to remove.

The old table is renamed to ARS_ALLOC_RDC_SPLIT_LEGACY rather than dropped,
so the rows remain recoverable until someone deletes it deliberately.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from sqlalchemy import text                                # noqa: E402
from app.database.session import get_data_engine           # noqa: E402

SPLIT = "ARS_ALLOC_RDC_SPLIT"
LEGACY = f"{SPLIT}_LEGACY"
PARKED = f"{SPLIT}_PARKED"
HISTORY = f"{SPLIT}_HISTORY"

# Payload columns, i.e. everything except the lifecycle control columns.
PAYLOAD = ["WERKS", "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "VAR_ART", "SZ",
           "SRC_RDC", "SHIP_QTY", "HOLD_QTY", "ALLOC_TYPE", "STORE_RDC",
           "PREF_TIER", "IS_CROSS", "CREATED_AT"]


def exists(c, t: str) -> bool:
    return bool(c.execute(text(
        f"SELECT CASE WHEN OBJECT_ID('dbo.{t}','U') IS NULL THEN 0 ELSE 1 END"
    )).scalar())


def source_table(c) -> Optional[str]:
    """Where the pre-migration rows live right now.

    Normally {SPLIT}. If a previous attempt renamed the table but failed
    before rebuilding it, they are already in {LEGACY} — this makes the
    script resumable from that half-migrated state instead of refusing.
    """
    if exists(c, LEGACY):
        return LEGACY
    if exists(c, SPLIT) and bool(c.execute(text(
            f"SELECT CASE WHEN COL_LENGTH('dbo.{SPLIT}','SESSION_ID') "
            f"IS NULL THEN 0 ELSE 1 END")).scalar()):
        return SPLIT
    return None


def free_legacy_names(c) -> list[str]:
    """Rename the PK and indexes that `sp_rename` left behind.

    sp_rename moves the TABLE but not its constraints or indexes, so
    PK_ARS_ALLOC_RDC_SPLIT and IX_..._PICK / _PEND stay under their original
    names on the legacy table — and creating the new table with the same
    constraint name then fails with 'There is already an object named ...'.
    Index names are only unique per table, but a PK is a database-level
    object, so at minimum the PK must move. All three are renamed so the
    legacy table reads unambiguously.
    """
    renamed = []
    pk = c.execute(text("""
        SELECT i.name FROM sys.indexes i
         WHERE i.object_id = OBJECT_ID('dbo.' + :lg) AND i.is_primary_key = 1
    """), {"lg": LEGACY}).scalar()
    if pk and not pk.endswith("_LEGACY"):
        c.execute(text(f"EXEC sp_rename '{pk}', '{pk}_LEGACY', 'OBJECT'"))
        renamed.append(f"{pk} -> {pk}_LEGACY")
    for ix in [r[0] for r in c.execute(text("""
            SELECT i.name FROM sys.indexes i
             WHERE i.object_id = OBJECT_ID('dbo.' + :lg)
               AND i.is_primary_key = 0 AND i.name IS NOT NULL
        """), {"lg": LEGACY}).fetchall()]:
        if not ix.endswith("_LEGACY"):
            c.execute(text(
                f"EXEC sp_rename 'dbo.{LEGACY}.{ix}', '{ix}_LEGACY', 'INDEX'"))
            renamed.append(f"{ix} -> {ix}_LEGACY")
    if renamed:
        c.commit()
    return renamed


def classify(c, src: str) -> list[dict]:
    """One row per session in the pre-migration table, with its destination."""
    out = []
    for sid, n in c.execute(text(
            f"SELECT SESSION_ID, COUNT(*) FROM [{src}] "
            f"GROUP BY SESSION_ID ORDER BY MIN(CREATED_AT)")).fetchall():
        hist = c.execute(text(
            "SELECT TOP 1 1 FROM ARS_ALLOC_HISTORY WHERE SESSION_ID=:s"),
            {"s": sid}).scalar()
        park = c.execute(text(
            "SELECT TOP 1 1 FROM ARS_ALLOC_PARKED WHERE SESSION_ID=:s"),
            {"s": sid}).scalar()
        dest = "HISTORY" if hist else ("PARKED" if park else "DROP (orphan)")
        out.append({"session_id": sid, "rows": int(n), "dest": dest})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="apply the migration")
    args = ap.parse_args()

    engine = get_data_engine()
    with engine.connect() as c:
        src = source_table(c)
        if src is None:
            print(f"nothing to migrate — {SPLIT} is absent or already has the "
                  f"new shape, and no {LEGACY} is waiting")
            return 0
        if src == LEGACY:
            print(f"resuming: a previous attempt already renamed the table, "
                  f"rows are in {LEGACY}\n")

        plan = classify(c, src)
        total = sum(p["rows"] for p in plan)
        print(f"{src}: {total:,} rows across {len(plan)} session(s)\n")
        print(f"  {'session':<24} {'rows':>9}  destination")
        for p in plan:
            print(f"  {p['session_id']:<24} {p['rows']:>9,}  {p['dest']}")

    if not args.yes:
        print("\ndry run — pass --yes to apply")
        return 0

    cols = ", ".join(f"[{c_}]" for c_ in PAYLOAD)
    with engine.connect() as c:
        # 1. Rename the old table aside so the new working table can be built
        #    under the canonical name. Keeps the rows recoverable. Skipped
        #    when a previous attempt already did it.
        if not exists(c, LEGACY):
            c.execute(text(f"EXEC sp_rename '{SPLIT}', '{LEGACY}'"))
            c.commit()
            print(f"\n  renamed {SPLIT} -> {LEGACY}")
        else:
            print(f"\n  {LEGACY} already present — skipping the rename")

        # 1b. Free the constraint / index names. sp_rename moves the table
        #     only, so PK_ARS_ALLOC_RDC_SPLIT would still exist and collide
        #     with the new table's PK (error 2714).
        for r in free_legacy_names(c):
            print(f"  renamed {r}")

        # 2. Build the new working table + the two snapshot tables.
        from app.services.rdc_split_service import ensure_split_table
        from app.services.parked_history import (
            _ensure_parked_table, _ensure_history_table, _get_target)
        ensure_split_table(c)
        c.commit()
        tgt = _get_target("rdc_split")
        _ensure_parked_table(c, tgt)
        _ensure_history_table(c, tgt)
        print(f"  created {SPLIT} (working), {PARKED}, {HISTORY}")

        # 3. Add the payload columns to the snapshot tables, which the
        #    machinery would otherwise only reconcile at the next park.
        for tbl in (PARKED, HISTORY):
            for col in PAYLOAD:
                c.execute(text(f"""
                    IF COL_LENGTH('dbo.{tbl}','{col}') IS NULL
                    ALTER TABLE dbo.{tbl} ADD [{col}] {
                        'BIGINT' if col in ('GEN_ART_NUMBER','VAR_ART')
                        else 'FLOAT' if col in ('SHIP_QTY','HOLD_QTY')
                        else 'BIT' if col == 'IS_CROSS'
                        else 'DATETIME' if col == 'CREATED_AT'
                        else 'NVARCHAR(200)'} NULL
                """))
        c.commit()

        # 4. Move the rows.
        moved = {"PARKED": 0, "HISTORY": 0, "DROP (orphan)": 0}
        for p in plan:
            sid, dest = p["session_id"], p["dest"]
            if dest == "PARKED":
                res = c.execute(text(f"""
                    INSERT INTO [{PARKED}] ({cols}, [SESSION_ID], [PARKED_AT], [PARK_STATUS])
                    SELECT {cols}, :s, [CREATED_AT], 'PARKED'
                      FROM [{LEGACY}] WHERE [SESSION_ID] = :s"""), {"s": sid})
                moved["PARKED"] += int(res.rowcount or 0)
            elif dest == "HISTORY":
                res = c.execute(text(f"""
                    INSERT INTO [{HISTORY}]
                        ({cols}, [SESSION_ID], [PARKED_AT], [PARK_STATUS],
                         [APPROVED_AT], [APPROVED_BY])
                    SELECT L.{', L.'.join(PAYLOAD)}, :s, L.[CREATED_AT], 'APPROVED',
                           ISNULL(A.APPROVED_AT, L.[CREATED_AT]),
                           ISNULL(A.APPROVED_BY, 'migration')
                      FROM [{LEGACY}] L
                      OUTER APPLY (SELECT TOP 1 APPROVED_AT, APPROVED_BY
                                     FROM ARS_ALLOC_HISTORY
                                    WHERE SESSION_ID = :s) A
                     WHERE L.[SESSION_ID] = :s"""), {"s": sid})
                moved["HISTORY"] += int(res.rowcount or 0)
            else:
                moved["DROP (orphan)"] += p["rows"]
        c.commit()

    print(f"\n  moved: {moved}")
    print(f"\n  {LEGACY} kept for recovery — drop it once you are satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
