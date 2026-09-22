#!/usr/bin/env python
"""
Step 0 of the Central RDC Pool change — evidence and regression baselines.

Spec: docs/superpowers/specs/2026-09-21-central-rdc-pool-allocation-brd-fsd-v1.5.md
      Part C, Step 0.

READ-ONLY. This script never writes to the database. It:

  1. lists candidate completed sessions per rdc_mode          (--list)
  2. measures defect D-1 on an `all` session                  (V1)
  3. exports the 4-table regression baseline for a session    (V5)
  4. reports the ledger row counts quoted in the spec §B7.3

Why it must run BEFORE any code changes: V5 diffs a re-run of an `Own`
session against these files. Once the code changes, the "before" state is
gone and Own can never be proven unchanged.

Usage
-----
    python scripts/step0_rdc_baseline.py --list
    python scripts/step0_rdc_baseline.py --counts
    python scripts/step0_rdc_baseline.py --d1 <all-session-id>
    python scripts/step0_rdc_baseline.py --baseline <session-id> [--label own]
    python scripts/step0_rdc_baseline.py --all      # counts + auto-pick + export

Output lands in  docs/baselines/<label>_<session_id>/  as CSV + a manifest
with SHA-256 per file, so a later diff is provable.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text                      # noqa: E402
from app.database.session import get_data_engine  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_ROOT = os.path.join(REPO, "docs", "baselines")

# The tables V5 diffs.
#   ARS_ALLOC_WORKING has NO SESSION_ID — it is a pure working table, dropped and
#   rebuilt by SELECT..INTO every run, so the durable evidence for a finished
#   session is ARS_ALLOC_PARKED / ARS_ALLOC_HISTORY.
BASELINE_TABLES = [
    ("ARS_ALLOC_PARKED", "SESSION_ID"),
    ("ARS_ALLOC_HISTORY", "SESSION_ID"),
    ("ARS_PEND_ALC", "SESSION_ID"),
    ("ARS_NL_TBL_HOLD_TRACKING", None),    # no SESSION_ID — snapshot whole open set
]

# Columns excluded from the fingerprint: they legitimately change on a re-run
# and would produce false V5 failures.
FINGERPRINT_SKIP = {
    "SESSION_ID", "PARKED_AT", "APPROVED_AT", "APPROVED_BY", "CREATED_AT",
    "LAST_UPDATED", "LISTED_DATE", "CLOSED_DATE", "LAST_BDC_AT", "LAST_DO_AT",
    "DO_UPLOADED_AT", "ID",
}

LEDGER_COUNTS = [
    "ARS_PEND_ALC",
    "ARS_NL_TBL_HOLD_TRACKING",
    "ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT",
    "Master_ALC_PEND",
    "ARS_FACONS_PEND",
]


def _exists(c, tbl: str) -> bool:
    return bool(c.execute(
        text("SELECT CASE WHEN OBJECT_ID(:t,'U') IS NULL THEN 0 ELSE 1 END"), {"t": tbl}
    ).scalar())


def _has_col(c, tbl: str, col: str) -> bool:
    return bool(c.execute(
        text("SELECT CASE WHEN COL_LENGTH(:t,:c) IS NULL THEN 0 ELSE 1 END"),
        {"t": tbl, "c": col},
    ).scalar())


# ---------------------------------------------------------------------------
# 1. candidate sessions
# ---------------------------------------------------------------------------
def list_sessions(c, limit: int = 40) -> None:
    rows = c.execute(text(f"""
        SELECT TOP {int(limit)}
               SESSION_ID, RDC_MODE, ALLOC_TYPE, STATUS, PARKED_STATUS,
               STORE_COUNT, ALLOC_ROWS, SHIP_QTY_TOTAL, HOLD_QTY_TOTAL,
               STARTED_AT, COMPLETED_AT
        FROM   ARS_LISTING_SESSIONS
        WHERE  STATUS IN ('SUCCESS', 'COMPLETED')
        ORDER  BY STARTED_AT DESC
    """)).mappings().all()

    if not rows:
        print("No SUCCESS sessions found.")
        return

    print(f"{'SESSION_ID':<38} {'MODE':<6} {'POOL':<6} {'PARK':<10} "
          f"{'STORES':>7} {'ROWS':>8} {'SHIP':>10}  STARTED")
    print("-" * 118)
    for r in rows:
        print(f"{str(r['SESSION_ID'])[:37]:<38} {str(r['RDC_MODE'] or '-'):<6} "
              f"{str(r['ALLOC_TYPE'] or '-'):<6} {str(r['PARKED_STATUS'] or '-'):<10} "
              f"{r['STORE_COUNT'] or 0:>7} {r['ALLOC_ROWS'] or 0:>8} "
              f"{float(r['SHIP_QTY_TOTAL'] or 0):>10,.0f}  {r['STARTED_AT']}")

    print("\nBest baseline candidates (largest completed run per mode):")
    for mode in ("own", "cross", "all"):
        best = [r for r in rows if (r["RDC_MODE"] or "").lower() == mode]
        best.sort(key=lambda x: float(x["SHIP_QTY_TOTAL"] or 0), reverse=True)
        if best:
            b = best[0]
            print(f"  {mode:<6} -> {b['SESSION_ID']}  "
                  f"({b['ALLOC_ROWS'] or 0:,} rows, "
                  f"{float(b['SHIP_QTY_TOTAL'] or 0):,.0f} pcs)")
        else:
            print(f"  {mode:<6} -> none found")


# ---------------------------------------------------------------------------
# 2. ledger row counts (verifies the figures quoted in spec §B7.3)
# ---------------------------------------------------------------------------
def ledger_counts(c) -> dict:
    out = {}
    print(f"{'TABLE':<42} {'ROWS':>14}   SRC_RDC?")
    print("-" * 74)
    for t in LEDGER_COUNTS:
        if not _exists(c, t):
            print(f"{t:<42} {'(absent)':>14}")
            out[t] = None
            continue
        n = c.execute(text(f"SELECT COUNT_BIG(*) FROM [{t}]")).scalar()
        has = "yes" if _has_col(c, t, "SRC_RDC") else "no"
        print(f"{t:<42} {n:>14,}   {has}")
        out[t] = int(n)
    return out


# ---------------------------------------------------------------------------
# 3. V1 — quantify defect D-1
# ---------------------------------------------------------------------------
def measure_d1(c, session_id: str) -> dict:
    """D-1: in `all` mode the MSA_FNL_Q join is ambiguous, so a listing row can
    carry the OTHER RDC's quantity. Count rows whose stored MSA_FNL_Q matches a
    single RDC's figure rather than the clubbed sum."""
    if not _exists(c, "ARS_LISTING_WORKING"):
        return {"error": "ARS_LISTING_WORKING not found"}

    sql = """
    WITH per_rdc AS (
        SELECT LTRIM(RTRIM(CAST([RDC] AS NVARCHAR(50))))            AS MSA_RDC,
               LTRIM(RTRIM(CAST([MAJ_CAT] AS NVARCHAR(200))))       AS MAJ_CAT,
               TRY_CAST(TRY_CAST([GEN_ART_NUMBER] AS FLOAT) AS BIGINT) AS GEN_ART_NUMBER,
               LTRIM(RTRIM(CAST([CLR] AS NVARCHAR(200))))           AS CLR,
               SUM(TRY_CAST([FNL_Q] AS FLOAT))                      AS FNL_Q
        FROM   ARS_MSA_GEN_ART
        GROUP  BY LTRIM(RTRIM(CAST([RDC] AS NVARCHAR(50)))),
                  LTRIM(RTRIM(CAST([MAJ_CAT] AS NVARCHAR(200)))),
                  TRY_CAST(TRY_CAST([GEN_ART_NUMBER] AS FLOAT) AS BIGINT),
                  LTRIM(RTRIM(CAST([CLR] AS NVARCHAR(200))))
    ),
    clubbed AS (
        SELECT MAJ_CAT, GEN_ART_NUMBER, CLR,
               SUM(FNL_Q)      AS CLUB_Q,
               COUNT(*)        AS RDC_N
        FROM   per_rdc
        GROUP  BY MAJ_CAT, GEN_ART_NUMBER, CLR
        HAVING COUNT(*) > 1                     -- option present at 2+ RDCs
    )
    SELECT COUNT(*)                                            AS rows_multi_rdc,
           SUM(CASE WHEN ABS(ISNULL(L.MSA_FNL_Q,0) - K.CLUB_Q) > 0.001
                    THEN 1 ELSE 0 END)                         AS rows_not_clubbed,
           SUM(CASE WHEN ABS(ISNULL(L.MSA_FNL_Q,0) - ISNULL(O.FNL_Q,-1)) < 0.001
                    THEN 1 ELSE 0 END)                         AS rows_showing_other_rdc,
           SUM(CASE WHEN ABS(ISNULL(L.MSA_FNL_Q,0) - K.CLUB_Q) > 0.001
                    THEN K.CLUB_Q - ISNULL(L.MSA_FNL_Q,0) ELSE 0 END) AS qty_hidden
    FROM   ARS_LISTING_WORKING L
    JOIN   clubbed K
           ON  K.MAJ_CAT        = L.MAJ_CAT
           AND K.GEN_ART_NUMBER = L.GEN_ART_NUMBER
           AND ISNULL(K.CLR,'') = ISNULL(L.CLR,'')
    LEFT   JOIN per_rdc O
           ON  O.MAJ_CAT        = L.MAJ_CAT
           AND O.GEN_ART_NUMBER = L.GEN_ART_NUMBER
           AND ISNULL(O.CLR,'') = ISNULL(L.CLR,'')
           AND O.MSA_RDC       <> LTRIM(RTRIM(CAST(L.RDC AS NVARCHAR(50))))
    """
    r = c.execute(text(sql)).mappings().first() or {}
    res = {k: (float(v) if v is not None else 0) for k, v in dict(r).items()}
    res["session_id"] = session_id

    print("\n--- V1: defect D-1 ---")
    print(f"  listing rows for options held at 2+ RDCs : {res.get('rows_multi_rdc',0):,.0f}")
    print(f"  rows whose MSA_FNL_Q is NOT the clubbed sum: {res.get('rows_not_clubbed',0):,.0f}")
    print(f"  ... of which match the OTHER RDC exactly  : {res.get('rows_showing_other_rdc',0):,.0f}")
    print(f"  pieces hidden from listing by the bad join: {res.get('qty_hidden',0):,.0f}")
    return res


# ---------------------------------------------------------------------------
# 4. V5 — export the regression baseline
# ---------------------------------------------------------------------------
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export_baseline(c, session_id: str, label: str) -> dict:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in session_id)
    out_dir = os.path.join(OUT_ROOT, f"{label}_{safe}")
    os.makedirs(out_dir, exist_ok=True)

    manifest = {
        "session_id": session_id,
        "label": label,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "purpose": "Step 0 regression baseline for V5 (Own/Cross unchanged) — "
                   "spec 2026-09-21-central-rdc-pool-allocation-brd-fsd-v1.5.md",
        "files": [],
    }

    for tbl, key in BASELINE_TABLES:
        if not _exists(c, tbl):
            print(f"  {tbl:<34} skipped (absent)")
            continue
        if key and not _has_col(c, tbl, key):
            print(f"  {tbl:<34} skipped (no {key})")
            continue

        if key:
            sql = f"SELECT * FROM [{tbl}] WHERE [{key}] = :sid"
            params = {"sid": session_id}
        else:
            sql = f"SELECT * FROM [{tbl}] WHERE ISNULL([IS_CLOSED],0) = 0"
            params = {}

        rs = c.execute(text(sql), params)
        cols = list(rs.keys())
        path = os.path.join(out_dir, f"{tbl}.csv.gz")
        sums = {}
        n = 0
        with gzip.open(path, "wt", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for row in rs:
                w.writerow(["" if v is None else v for v in row])
                n += 1
                for j, col in enumerate(cols):
                    if col.upper() in FINGERPRINT_SKIP:
                        continue
                    v = row[j]
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        sums[col] = sums.get(col, 0.0) + float(v)
        mb = os.path.getsize(path) / 1e6
        print(f"  {tbl:<30} {n:>10,} rows  {mb:>7.1f} MB -> {os.path.basename(path)}")
        manifest["files"].append({
            "table": tbl, "rows": n, "file": f"{tbl}.csv.gz",
            "sha256": _sha256(path),
            # numeric fingerprint: what V5 compares first. Small, committable,
            # and a mismatch localises the defect to a column immediately.
            "column_sums": {k: round(v, 4) for k, v in sorted(sums.items())},
        })

    mpath = os.path.join(out_dir, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"\n  manifest -> {mpath}")
    return manifest


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Step 0 — RDC pool evidence and baselines")
    ap.add_argument("--list", action="store_true", help="list successful sessions per mode")
    ap.add_argument("--counts", action="store_true", help="ledger row counts (spec §B7.3)")
    ap.add_argument("--d1", metavar="SESSION_ID", help="measure defect D-1 (V1)")
    ap.add_argument("--baseline", metavar="SESSION_ID", help="export the 4-table baseline")
    ap.add_argument("--label", default="own", help="baseline folder label (own | cross)")
    ap.add_argument("--all", action="store_true", help="counts + list + auto-export own & cross")
    args = ap.parse_args()

    if not any([args.list, args.counts, args.d1, args.baseline, args.all]):
        ap.print_help()
        return 1

    eng = get_data_engine()
    with eng.connect() as c:
        print(f"connected: {c.execute(text('SELECT DB_NAME()')).scalar()} "
              f"@ {c.execute(text('SELECT @@SERVERNAME')).scalar()}\n")

        if args.counts or args.all:
            print("=== LEDGER ROW COUNTS (spec §B7.3) ===")
            ledger_counts(c)
            print()

        if args.list or args.all:
            print("=== SUCCESSFUL SESSIONS ===")
            list_sessions(c)
            print()

        if args.d1:
            measure_d1(c, args.d1)

        if args.baseline:
            print(f"=== BASELINE EXPORT — {args.label} / {args.baseline} ===")
            export_baseline(c, args.baseline, args.label)

        if args.all:
            print("Auto-export not run — pick sessions from the list above and re-run:")
            print("  python scripts/step0_rdc_baseline.py --baseline <own-sid>   --label own")
            print("  python scripts/step0_rdc_baseline.py --baseline <cross-sid> --label cross")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
