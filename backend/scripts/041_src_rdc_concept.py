#!/usr/bin/env python
"""
RDC / SRC_RDC concept (2026-10-14) — schema + data migration.

    RDC     = the store's own warehouse (Master_ALC_INPUT_ST_MASTER.RDC)
    SRC_RDC = the warehouse the qty is dispatched / reserved FROM — what MSA
              deducts, what the BDC names as VENDOR, what DO uploads match

Tables: ARS_PEND_ALC, ARS_BDC_HISTORY, ARS_NL_TBL_HOLD_TRACKING,
        ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT.

Run order (each step is idempotent and can be re-run):

  1. python scripts/041_src_rdc_concept.py                    # dry run — reports only
  2. python scripts/041_src_rdc_concept.py --phase add --yes
        adds SRC_RDC (nullable → metadata-only), copies SRC_RDC = RDC on
        every row (between 2026-10-05 and today RDC held the dispatch
        warehouse, and before that the two coincided except under All RDCs),
        builds the two new indexes. SAFE while the OLD code is still running:
        old code ignores SRC_RDC, and new code falls back to RDC when
        SRC_RDC is blank.
  3. deploy the new code (every reader keys on SRC_RDC from then on)
  4. python scripts/041_src_rdc_concept.py --phase restamp --yes
        re-runs the backfill (catches rows the old code wrote meanwhile),
        then sets RDC = the store master's RDC. Never touches SRC_RDC.
        Add --resync-msa to re-seed MSA PEND/HOLD from the ledgers
        afterwards (normally the next Calculate MSA does this).
  5. python scripts/041_src_rdc_concept.py --phase verify      # read only

Never run restamp before step 3: the old code reads RDC as the dispatch
warehouse, so re-pointing RDC at the store's warehouse under it would move
MSA deductions onto the wrong pool.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from sqlalchemy import text                                # noqa: E402
from app.database.session import get_data_engine           # noqa: E402

ST_MASTER = "Master_ALC_INPUT_ST_MASTER"
BATCH = 200_000

# table → (store column, has ID identity)
TABLES = {
    "ARS_PEND_ALC":                      ("ST_CD", True),
    "ARS_BDC_HISTORY":                   ("ST_CD", True),
    "ARS_NL_TBL_HOLD_TRACKING":          ("WERKS", False),
    "ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT": ("WERKS", False),
}

INDEXES = [
    ("ARS_PEND_ALC", "IX_ARS_PEND_ALC_open_src",
     "(IS_CLOSED, SRC_RDC, ST_CD, ARTICLE_NUMBER) "
     "INCLUDE (RDC, ALLOC_QTY, DO_QTY, PEND_QTY, BDC_QTY, MAJ_CAT, "
     "GEN_ART_NUMBER, CLR, ALLOC_TYPE, LAST_BDC_AT, APPROVED_AT)"),
    ("ARS_BDC_HISTORY", "IX_ARS_BDC_HISTORY_store_art",
     "(ARTICLE_NUMBER, ST_CD) "
     "INCLUDE (SRC_RDC, RDC, STATUS, BDC_QTY, DO_RECEIVED, BDC_DATE)"),
]


def scalar(conn, sql, **p):
    return conn.execute(text(sql), p).scalar()


def exists(c, t):
    return bool(scalar(c, f"SELECT CASE WHEN OBJECT_ID('dbo.{t}','U') IS NULL THEN 0 ELSE 1 END"))


def has_col(c, t, col):
    return bool(scalar(c, "SELECT CASE WHEN COL_LENGTH(:t, :col) IS NULL THEN 0 ELSE 1 END",
                       t=f"dbo.{t}", col=col))


def rows_of(c, t):
    return int(scalar(c, f"SELECT ISNULL(SUM(p.rows),0) FROM sys.partitions p "
                         f"WHERE p.object_id = OBJECT_ID('dbo.{t}') AND p.index_id IN (0,1)") or 0)


# ── report ─────────────────────────────────────────────────────────────────
def report(c):
    print("\n── state ──────────────────────────────────────────────────────────")
    for t, (st, _) in TABLES.items():
        if not exists(c, t):
            print(f"  {t:<36} absent"); continue
        n = rows_of(c, t)
        if not has_col(c, t, "SRC_RDC"):
            print(f"  {t:<36} rows={n:>12,}  SRC_RDC column: MISSING"); continue
        blank = scalar(c, f"SELECT COUNT_BIG(*) FROM [{t}] WITH (NOLOCK) "
                          f"WHERE NULLIF(SRC_RDC,'') IS NULL AND NULLIF(RDC,'') IS NOT NULL")
        restamp = scalar(c, f"""SELECT COUNT_BIG(*) FROM [{t}] T WITH (NOLOCK)
                                JOIN [{ST_MASTER}] M ON M.ST_CD = T.[{st}]
                                WHERE NULLIF(M.RDC,'') IS NOT NULL AND ISNULL(T.RDC,'') <> M.RDC""")
        print(f"  {t:<36} rows={n:>12,}  SRC_RDC to backfill={blank:>12,}  "
              f"RDC differing from store master={restamp:>12,}")
    for t, name, _ in INDEXES:
        if exists(c, t):
            ok = scalar(c, "SELECT COUNT(*) FROM sys.indexes WHERE name=:n AND object_id=OBJECT_ID(:t)",
                        n=name, t=f"dbo.{t}")
            print(f"  index {name:<34} {'present' if ok else 'missing'}")

    if exists(c, "ARS_PEND_ALC"):
        src = ("COALESCE(NULLIF(SRC_RDC,''), RDC)" if has_col(c, "ARS_PEND_ALC", "SRC_RDC") else "RDC")
        print("\n── open pending by dispatch warehouse (what MSA deducts) ────────────")
        for r in c.execute(text(f"""SELECT {src}, COUNT(*), SUM(PEND_QTY) FROM ARS_PEND_ALC WITH (NOLOCK)
                                    WHERE IS_CLOSED = 0 AND PEND_QTY > 0 GROUP BY {src} ORDER BY 1""")):
            print(f"  {r[0]!s:<8} rows={int(r[1]):>8,}  pend={float(r[2] or 0):>12,.0f}")
        if exists(c, "ARS_MSA_TOTAL"):
            print("  vs ARS_MSA_TOTAL.PEND_QTY (as last calculated / adjusted):")
            for r in c.execute(text("SELECT RDC, SUM(CAST(PEND_QTY AS FLOAT)) FROM ARS_MSA_TOTAL WITH (NOLOCK) "
                                    "GROUP BY RDC ORDER BY 1")):
                print(f"  {r[0]!s:<8} msa pend={float(r[1] or 0):>12,.0f}")
            print("  (MSA is not touched by DO uploads, so it lags the ledger until the next Calculate MSA)")

        # Open rows whose recorded warehouse disagrees with the alloc line's
        # single source — the 2026-10-10 class (split rows never promoted).
        if has_col(c, "ARS_ALLOC_HISTORY", "SRC_RDC"):
            bad = c.execute(text(f"""
                WITH h AS (SELECT SESSION_ID, WERKS, VAR_ART, MAX(SRC_RDC) S1, MIN(SRC_RDC) S2
                           FROM ARS_ALLOC_HISTORY WITH (NOLOCK)
                           WHERE NULLIF(SRC_RDC,'') IS NOT NULL AND SRC_RDC <> 'MULTI'
                             AND SESSION_ID IN (SELECT DISTINCT SESSION_ID FROM ARS_PEND_ALC WITH (NOLOCK)
                                                WHERE IS_CLOSED = 0)
                           GROUP BY SESSION_ID, WERKS, VAR_ART)
                SELECT COUNT(*), ISNULL(SUM(P.PEND_QTY),0)
                FROM ARS_PEND_ALC P WITH (NOLOCK)
                JOIN h ON h.SESSION_ID = P.SESSION_ID AND h.WERKS = P.ST_CD AND h.VAR_ART = P.ARTICLE_NUMBER
                WHERE P.IS_CLOSED = 0 AND P.PEND_QTY > 0 AND h.S1 = h.S2 AND h.S1 <> {src}
            """)).fetchone()
            print(f"\n  open pending rows on a warehouse other than their alloc line's single source: "
                  f"{int(bad[0]):,} rows / {float(bad[1]):,.0f} pcs"
                  + ("  ← these are deducted from the wrong pool; report only, not auto-corrected"
                     if bad[0] else ""))


# ── phase add ──────────────────────────────────────────────────────────────
def phase_add(c):
    for t in TABLES:
        if exists(c, t) and not has_col(c, t, "SRC_RDC"):
            c.execute(text(f"ALTER TABLE dbo.[{t}] ADD [SRC_RDC] NVARCHAR(20) NULL"))
            c.commit()
            print(f"  added SRC_RDC to {t}")
    backfill(c)
    for t, name, cols in INDEXES:
        if not exists(c, t):
            continue
        if scalar(c, "SELECT COUNT(*) FROM sys.indexes WHERE name=:n AND object_id=OBJECT_ID(:t)",
                  n=name, t=f"dbo.{t}"):
            print(f"  index {name} already present"); continue
        t0 = time.time()
        try:
            c.execute(text(f"CREATE INDEX {name} ON dbo.[{t}] {cols} WITH (ONLINE = ON)"))
        except Exception as e:                       # edition without online index builds
            c.rollback()
            print(f"  online build unavailable ({str(e)[:80]}…) — building offline")
            c.execute(text(f"CREATE INDEX {name} ON dbo.[{t}] {cols}"))
        c.commit()
        print(f"  built {name} in {time.time() - t0:.0f}s")


def backfill(c):
    """SRC_RDC = RDC wherever SRC_RDC is blank. Batched; ID-ranged where the
    table has an identity so each batch is a clustered range seek."""
    for t, (_, has_id) in TABLES.items():
        if not exists(c, t) or not has_col(c, t, "SRC_RDC"):
            continue
        done, t0 = 0, time.time()
        if has_id:
            lo, hi = c.execute(text(f"SELECT ISNULL(MIN(ID),0), ISNULL(MAX(ID),0) FROM [{t}]")).fetchone()
            a = int(lo)
            while a <= int(hi):
                b = a + BATCH - 1
                r = c.execute(text(f"""UPDATE [{t}] SET SRC_RDC = RDC
                                       WHERE ID BETWEEN :a AND :b
                                         AND NULLIF(SRC_RDC,'') IS NULL AND NULLIF(RDC,'') IS NOT NULL"""),
                              {"a": a, "b": b})
                c.commit()
                done += int(r.rowcount or 0)
                a = b + 1
        else:
            while True:
                r = c.execute(text(f"""UPDATE TOP ({BATCH}) [{t}] SET SRC_RDC = RDC
                                       WHERE NULLIF(SRC_RDC,'') IS NULL AND NULLIF(RDC,'') IS NOT NULL"""))
                c.commit()
                n = int(r.rowcount or 0)
                done += n
                if n == 0:
                    break
        print(f"  backfilled SRC_RDC on {done:,} {t} rows ({time.time() - t0:.0f}s)")


# ── phase restamp ──────────────────────────────────────────────────────────
def phase_restamp(c, resync_msa: bool):
    for t in TABLES:
        if exists(c, t) and not has_col(c, t, "SRC_RDC"):
            sys.exit(f"  {t} has no SRC_RDC — run --phase add first")
    backfill(c)        # rows the old code wrote since phase add
    for t, (st, has_id) in TABLES.items():
        if not exists(c, t):
            continue
        left = scalar(c, f"SELECT COUNT_BIG(*) FROM [{t}] WHERE NULLIF(SRC_RDC,'') IS NULL "
                         f"AND NULLIF(RDC,'') IS NOT NULL")
        if left:
            sys.exit(f"  {t}: {left:,} rows still without SRC_RDC — refusing to restamp")
        done, t0 = 0, time.time()
        upd = (f"UPDATE T SET T.RDC = M.RDC FROM [{t}] T JOIN [{ST_MASTER}] M ON M.ST_CD = T.[{st}] "
               f"WHERE NULLIF(M.RDC,'') IS NOT NULL AND ISNULL(T.RDC,'') <> M.RDC")
        if has_id:
            lo, hi = c.execute(text(f"SELECT ISNULL(MIN(ID),0), ISNULL(MAX(ID),0) FROM [{t}]")).fetchone()
            a = int(lo)
            while a <= int(hi):
                b = a + BATCH - 1
                r = c.execute(text(upd + " AND T.ID BETWEEN :a AND :b"), {"a": a, "b": b})
                c.commit()
                done += int(r.rowcount or 0)
                a = b + 1
        else:
            while True:
                r = c.execute(text(upd.replace("UPDATE T SET", f"UPDATE TOP ({BATCH}) T SET")))
                c.commit()
                n = int(r.rowcount or 0)
                done += n
                if n == 0:
                    break
        print(f"  RDC → store master on {done:,} {t} rows ({time.time() - t0:.0f}s)")
    if resync_msa:
        from app.services.pend_alc_service import bootstrap_msa_pend_sync, bootstrap_msa_hold_sync
        print("  MSA pend re-sync:", bootstrap_msa_pend_sync(c))
        print("  MSA hold re-sync:", bootstrap_msa_hold_sync(c))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["report", "add", "restamp", "verify"], default="report")
    ap.add_argument("--yes", action="store_true", help="actually write (add / restamp)")
    ap.add_argument("--resync-msa", action="store_true",
                    help="restamp only: re-seed MSA PEND/HOLD from the ledgers afterwards")
    args = ap.parse_args()
    eng = get_data_engine()
    with eng.connect() as c:
        print(f"database: {scalar(c, 'SELECT DB_NAME()')} on {scalar(c, 'SELECT @@SERVERNAME')}")
        if args.phase in ("add", "restamp") and not args.yes:
            print(f"\ndry run for --phase {args.phase} — pass --yes to write")
            report(c)
            return 0
        if args.phase == "add":
            phase_add(c)
        elif args.phase == "restamp":
            phase_restamp(c, args.resync_msa)
        report(c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
