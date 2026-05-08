"""
validate_alloc_modes.py
=======================
Run the same allocation in all three modes and confirm bit-identical output.

Procedure
---------
1. Build ARS_LISTING_WORKING + ARS_ALLOC_WORKING with the existing pipeline
   (Parts 1-7) — done by the caller before running this script, or via the
   /listing/generate endpoint with allocation_mode=sequential first time.
2. For each mode in [sequential, python_parallel, sql_parallel]:
     a. Reset the alloc-state columns on the existing working/alloc tables
        so each mode starts from the same baseline.
     b. Run that mode's Stage C end-to-end.
     c. Snapshot the result into ARS_ALLOC_WORKING__<mode> (a sibling table).
3. Diff every snapshot against ARS_ALLOC_WORKING__sequential. Report any
   row-level mismatches in SHIP_QTY / HOLD_QTY / ALLOC_STATUS.

Usage
-----
    cd backend
    python scripts/validate_alloc_modes.py                    # all 3 modes
    python scripts/validate_alloc_modes.py --modes seq,py     # subset
    python scripts/validate_alloc_modes.py --skip-prep        # if working+alloc already prepared

Exit status
-----------
    0  all modes match sequential
    1  diff found (or run failed)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from sqlalchemy import text

from app.database.session import data_engine
from app.utils.db_helpers import run_sql


WORKING = "ARS_LISTING_WORKING"
ALLOC   = "ARS_ALLOC_WORKING"

ALL_MODES = ["sequential", "python_parallel", "sql_parallel"]
ALIAS = {"seq": "sequential", "py": "python_parallel", "sql": "sql_parallel"}


# --------------------------------------------------------------------------- #
# Reset working/alloc state so a fresh mode run starts from the same baseline.
# Parts 1-7 are NOT re-run; only Stage A/B/C/D state columns are reset.
# --------------------------------------------------------------------------- #
RESET_SQL = f"""
UPDATE [{WORKING}] SET
    LISTED_FLAG=0, LISTED_REASON='',
    OPT_PRIORITY_RANK=NULL, OPT_PRIORITY_TIER=NULL,
    ALLOC_QTY=0, HOLD_QTY=0,
    ALLOC_STATUS='PENDING', ALLOC_REMARKS='';
IF OBJECT_ID('{ALLOC}','U') IS NOT NULL DROP TABLE [{ALLOC}];
"""


def _snap_table(mode: str) -> str:
    return f"{ALLOC}__{mode}"


def _drop_snapshot(conn, mode: str):
    run_sql(conn, f"IF OBJECT_ID('{_snap_table(mode)}','U') IS NOT NULL DROP TABLE [{_snap_table(mode)}]")


def _snap(conn, mode: str):
    run_sql(conn, f"SELECT * INTO [{_snap_table(mode)}] FROM [{ALLOC}]")


def _run_mode(mode: str):
    """Drive Stage C in the named mode. Returns the result dict."""
    print(f"\n=== Running mode: {mode} ===")
    t0 = time.time()
    if mode == "sequential":
        from app.services.rule_engine_new import run_listing_and_allocation
        with data_engine.connect() as ac:
            r = run_listing_and_allocation(
                conn=ac, working_table=WORKING,
                listed_table="ARS_LISTED_OPT", alloc_table=ALLOC,
            )
    elif mode == "python_parallel":
        from app.services.rule_engine_parallel_python import (
            run_listing_and_allocation_python_parallel,
        )
        r = run_listing_and_allocation_python_parallel(
            working_table=WORKING, alloc_table=ALLOC,
        )
    elif mode == "sql_parallel":
        from app.services.rule_engine_parallel_sql import (
            run_listing_and_allocation_sql_parallel,
        )
        r = run_listing_and_allocation_sql_parallel(
            working_table=WORKING, alloc_table=ALLOC,
        )
    else:
        raise ValueError(f"Unknown mode: {mode}")
    print(f"=== {mode} done in {round(time.time()-t0,1)}s "
          f"(alloc_rows={r.get('alloc_rows')} "
          f"ship={r.get('ship_qty_total')} hold={r.get('hold_qty_total')}) ===")
    return r


def _diff(conn, mode_a: str, mode_b: str):
    """Diff two snapshots; report mismatches in SHIP_QTY/HOLD_QTY/ALLOC_STATUS."""
    sql = f"""
        SELECT
            ISNULL(A.WERKS, B.WERKS)             AS WERKS,
            ISNULL(A.MAJ_CAT, B.MAJ_CAT)         AS MAJ_CAT,
            ISNULL(A.GEN_ART_NUMBER, B.GEN_ART_NUMBER) AS GEN_ART_NUMBER,
            ISNULL(A.CLR, B.CLR)                 AS CLR,
            ISNULL(A.VAR_ART, B.VAR_ART)         AS VAR_ART,
            ISNULL(A.SZ, B.SZ)                   AS SZ,
            A.SHIP_QTY     AS A_SHIP, B.SHIP_QTY     AS B_SHIP,
            A.HOLD_QTY     AS A_HOLD, B.HOLD_QTY     AS B_HOLD,
            A.ALLOC_STATUS AS A_STAT, B.ALLOC_STATUS AS B_STAT
        FROM [{_snap_table(mode_a)}] A
        FULL OUTER JOIN [{_snap_table(mode_b)}] B
          ON A.WERKS = B.WERKS AND A.MAJ_CAT = B.MAJ_CAT
         AND A.GEN_ART_NUMBER = B.GEN_ART_NUMBER
         AND ISNULL(A.CLR,'') = ISNULL(B.CLR,'')
         AND A.VAR_ART = B.VAR_ART AND A.SZ = B.SZ
        WHERE
            ISNULL(A.SHIP_QTY,0)     <> ISNULL(B.SHIP_QTY,0)
         OR ISNULL(A.HOLD_QTY,0)     <> ISNULL(B.HOLD_QTY,0)
         OR ISNULL(A.ALLOC_STATUS,'') <> ISNULL(B.ALLOC_STATUS,'')
    """
    diffs = conn.execute(text(sql)).fetchall()
    n = len(diffs)
    print(f"\n--- Diff: {mode_a} vs {mode_b} ---")
    if n == 0:
        print(f"OK  Bit-identical — 0 differing rows")
        return 0
    print(f"FAIL  {n} differing rows. First 10:")
    for r in diffs[:10]:
        print(f"  {r.WERKS}/{r.MAJ_CAT}/{r.GEN_ART_NUMBER}/{r.CLR}/{r.VAR_ART}/{r.SZ}: "
              f"SHIP {r.A_SHIP} vs {r.B_SHIP} | "
              f"HOLD {r.A_HOLD} vs {r.B_HOLD} | "
              f"STAT {r.A_STAT} vs {r.B_STAT}")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default="sequential,python_parallel,sql_parallel",
                    help="Comma-separated mode list. Aliases: seq, py, sql.")
    ap.add_argument("--skip-prep", action="store_true",
                    help="Skip the per-mode reset (advanced — assumes you ran "
                         "Parts 1-7 manually and want to compare a single mode "
                         "against an existing snapshot)")
    args = ap.parse_args()

    raw_modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    modes = [ALIAS.get(m, m) for m in raw_modes]
    for m in modes:
        if m not in ALL_MODES:
            print(f"Unknown mode {m!r}; valid={ALL_MODES}")
            sys.exit(1)
    print(f"Validating modes: {modes}")

    # Per-mode: drop snapshot, run, snapshot.
    for mode in modes:
        with data_engine.connect() as conn:
            _drop_snapshot(conn, mode)
            if not args.skip_prep:
                # Reset Stage A/B/C/D state so this mode starts clean.
                # Working table's grid columns (Parts 1-7 output) are kept.
                run_sql(conn, RESET_SQL)
        try:
            _run_mode(mode)
        except Exception as e:
            print(f"FAIL mode {mode}: {e}")
            sys.exit(1)
        with data_engine.connect() as conn:
            _snap(conn, mode)
            print(f"snapshotted -> {_snap_table(mode)}")

    # Diff every mode against the first (usually sequential).
    if len(modes) < 2:
        print("Only one mode requested — nothing to diff.")
        return
    baseline = modes[0]
    fails = 0
    with data_engine.connect() as conn:
        for other in modes[1:]:
            fails += _diff(conn, baseline, other)

    print("\n=== SUMMARY ===")
    if fails == 0:
        print(f"OK  All modes match {baseline}.")
        sys.exit(0)
    else:
        print(f"FAIL  {fails} differing rows total. See diffs above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
