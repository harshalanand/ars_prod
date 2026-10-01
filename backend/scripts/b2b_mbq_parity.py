"""Prove the ARS demand build gives the Streamlit tool's numbers.

Runs the ARS build (b2b_mbq_service.build_into) over the TOOL's own input
tables — B2B_BIN_MASTER, B2B_STORE_MASTER, B2B_REQ — with the tool's saved
settings, into a scratch table, and compares every row and column with the
tool's B2B_ART_MBQ. Nothing the tool owns is written; the scratch table is
dropped at the end unless --keep is given.

The tool's table was built at a moment in the past, and the store stock grid
(ARS_GRID_MJ_VAR_ART) has changed since, so columns are compared in three
groups:

    inputs only     must match exactly
    size-dependent  SZ comes from the grid; where SZ is equal these must match
    stock-dependent STK_TTL comes from the grid; where STK_TTL and MBQ_ROUNDED
                    are equal, SHORTFALL and EXCESS must match

Usage (from backend/):
    python scripts/b2b_mbq_parity.py            # compare, then drop the scratch table
    python scripts/b2b_mbq_parity.py --keep     # keep ARS_B2B_ART_MBQ_PARITY for digging
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger  # noqa: E402

logger.remove()
from sqlalchemy import text  # noqa: E402

from app.database.session import data_engine  # noqa: E402
from app.services import b2b_mbq_service as M  # noqa: E402
from app.services import b2b_schema as S  # noqa: E402

SCRATCH = "ARS_B2B_ART_MBQ_PARITY"
TOOL = {"bin": "B2B_BIN_MASTER", "store": "B2B_STORE_MASTER", "req": "B2B_REQ",
        "grid": S.SRC_GRID, "cont": S.SRC_CONT, "out": SCRATCH}
# The tool's config.MBQ_DEFAULTS, under its saved B2B_MBQ_SETTING rows (db_utils.get_settings).
TOOL_DEFAULTS = {"SHORT_DAYS": "60", "LONG_DAYS": "90", "SHORT_SZ_LIST": "A,A_MIX,NA",
                 "TREAT_MAJCAT_MIX_AS_SHORT": "1", "DEFAULT_SALE_COVER_DAYS": "30",
                 "TREAT_MISSING_STK_AS_ZERO": "0", "CONT_SOURCE_MODE": "HYBRID", "CONT_APPLY": "1",
                 "CONT_FULL_SZ_LIST": "A,NA", "CONT_FALLBACK": "1", "MBQ_MIN_WHEN_CONT": "1",
                 "MBQ_PRUNE_TO_REQ": "1", "MBQ_MIN_REQ_UNITS": "1"}

INPUT_COLS = ["ST_NM", "RDC", "ART_NUM", "SEG", "DIV", "SUB_DIV", "MAJ_CAT", "BIN_SIZE", "SEASON",
              "CONT", "CONT_SOURCE", "CONT_EFF", "CONT_RULE", "ACC_D", "ACC_D_EFF",
              "SALE_COVER_DAYS", "BIN_QTY", "BIN_COUNT"]
# SZ and SZ_SOURCE both come from the grid: SZ_SOURCE says whether the grid had
# a row at all, so it moves with the grid even where SZ does not. The terms
# built from SZ must match wherever SZ does.
SIZE_COLS = ["SZ", "NORM_DAYS", "MBQ_RAW", "MBQ", "MBQ_ROUNDED"]
STOCK_COLS = ["SZ_SOURCE", "STK_TTL", "EXCESS", "SHORTFALL"]


def ne(c: str) -> str:
    """NULL-safe 'differs'."""
    return (f"(o.{c} <> t.{c} OR (o.{c} IS NULL AND t.{c} IS NOT NULL) "
            f"OR (o.{c} IS NOT NULL AND t.{c} IS NULL))")


def main() -> int:
    keep = "--keep" in sys.argv
    compare_only = "--compare-only" in sys.argv          # reuse a kept scratch table
    with data_engine.begin() as conn:
        saved = {r[0]: r[1] for r in conn.execute(text(
            "SELECT SETTING_KEY, SETTING_VALUE FROM dbo.B2B_MBQ_SETTING")) if r[1] is not None}
        if not compare_only:
            conn.execute(text(f"DROP TABLE IF EXISTS dbo.{SCRATCH}"))
            conn.execute(text(f"SELECT TOP 0 * INTO dbo.{SCRATCH} FROM dbo.{S.ART_MBQ}"))
    settings = {**TOOL_DEFAULTS, **saved}
    print("Tool settings used:", {k: settings[k] for k in TOOL_DEFAULTS})

    try:
        if not compare_only:
            t0 = time.time()
            res = M.build_into(TOOL, settings, None,
                               progress=lambda m, p: print(f"  {time.time() - t0:6.1f}s  {m}", flush=True))
            print(f"ARS build over the tool's inputs: {res['rows']:,} rows in {time.time() - t0:.1f}s, "
                  f"checks {'passed' if res['passed'] else 'FAILED'} · steps {res['steps']}")
            for c in res["checks"]:
                print(f"   [{c['level']:5}] {c['code']}: {c['title']}")

        cols = INPUT_COLS + SIZE_COLS + STOCK_COLS
        sz_eq = "NOT " + ne("SZ")
        stk_eq = f"NOT {ne('STK_TTL')} AND NOT {ne('MBQ_ROUNDED')}"
        with data_engine.connect() as conn:
            t1 = time.time()
            keys = conn.execute(text(f"""
                SELECT (SELECT COUNT_BIG(*) FROM dbo.B2B_ART_MBQ),
                       (SELECT COUNT_BIG(*) FROM dbo.{SCRATCH}),
                       (SELECT COUNT_BIG(*) FROM dbo.B2B_ART_MBQ t WHERE NOT EXISTS
                           (SELECT 1 FROM dbo.{SCRATCH} o WHERE o.STORE_CODE = t.STORE_CODE AND o.ART = t.ART)),
                       (SELECT COUNT_BIG(*) FROM dbo.{SCRATCH} o WHERE NOT EXISTS
                           (SELECT 1 FROM dbo.B2B_ART_MBQ t WHERE t.STORE_CODE = o.STORE_CODE AND t.ART = o.ART))
            """)).fetchone()
            diff = conn.execute(text(f"""
                SELECT COUNT_BIG(*),
                       {', '.join(f'SUM(CASE WHEN {ne(c)} THEN 1 ELSE 0 END)' for c in cols)},
                       SUM(CASE WHEN {sz_eq} THEN 1 ELSE 0 END),
                       {', '.join(f'SUM(CASE WHEN {sz_eq} AND {ne(c)} THEN 1 ELSE 0 END)' for c in SIZE_COLS[1:])},
                       SUM(CASE WHEN {stk_eq} THEN 1 ELSE 0 END),
                       {', '.join(f'SUM(CASE WHEN {stk_eq} AND {ne(c)} THEN 1 ELSE 0 END)' for c in ('EXCESS', 'SHORTFALL'))}
                  FROM dbo.{SCRATCH} o
                  JOIN dbo.B2B_ART_MBQ t ON t.STORE_CODE = o.STORE_CODE AND t.ART = o.ART
            """)).fetchone()
            # How the grid moved: which way SZ_SOURCE flipped, and stock up or down.
            moved = conn.execute(text(f"""
                SELECT SUM(CASE WHEN t.SZ_SOURCE = N'BIN'  AND o.SZ_SOURCE = N'GRID' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN t.SZ_SOURCE = N'GRID' AND o.SZ_SOURCE = N'BIN'  THEN 1 ELSE 0 END),
                       SUM(CASE WHEN o.STK_TTL > t.STK_TTL THEN 1 ELSE 0 END),
                       SUM(CASE WHEN o.STK_TTL < t.STK_TTL THEN 1 ELSE 0 END),
                       SUM(o.STK_TTL - t.STK_TTL)
                  FROM dbo.{SCRATCH} o
                  JOIN dbo.B2B_ART_MBQ t ON t.STORE_CODE = o.STORE_CODE AND t.ART = o.ART
            """)).fetchone()
            print(f"\nCompared in {time.time() - t1:.1f}s")

        tool_rows, ars_rows, only_tool, only_ars = (int(x) for x in keys)
        print(f"\nROWS   tool {tool_rows:,} · ARS {ars_rows:,} · only in tool {only_tool:,} · only in ARS {only_ars:,}")
        joined = int(diff[0])
        per = dict(zip(cols, (int(x or 0) for x in diff[1:1 + len(cols)])))
        i = 1 + len(cols)
        same_sz = int(diff[i] or 0)
        size_cond = dict(zip(SIZE_COLS[1:], (int(x or 0) for x in diff[i + 1:i + len(SIZE_COLS)])))
        i += len(SIZE_COLS)
        same_stk = int(diff[i] or 0)
        stock_cond = dict(zip(("EXCESS", "SHORTFALL"), (int(x or 0) for x in diff[i + 1:i + 3])))

        ok = only_tool == 0 and only_ars == 0
        print(f"\nINPUTS ONLY — must match exactly ({joined:,} rows compared)")
        for c in INPUT_COLS:
            ok &= per[c] == 0
            print(f"   {c:<16} {per[c]:>12,} differ")
        print("\nSIZE-DEPENDENT — SZ comes from the grid")
        for c in SIZE_COLS:
            print(f"   {c:<16} {per[c]:>12,} differ")
        print(f"   ...where SZ is the same ({same_sz:,} rows):")
        for c, n in size_cond.items():
            ok &= n == 0
            print(f"      {c:<13} {n:>12,} differ")
        print("\nGRID-DEPENDENT — the grid changed after the tool's build")
        for c in STOCK_COLS:
            print(f"   {c:<16} {per[c]:>12,} differ")
        print(f"   grid row appeared (BIN → GRID) {int(moved[0] or 0):,} · disappeared (GRID → BIN) "
              f"{int(moved[1] or 0):,} · stock up on {int(moved[2] or 0):,} rows, down on "
              f"{int(moved[3] or 0):,}, net {float(moved[4] or 0):+,.0f} pcs")
        print(f"   ...where STK_TTL and MBQ_ROUNDED are the same ({same_stk:,} rows):")
        for c, n in stock_cond.items():
            ok &= n == 0
            print(f"      {c:<13} {n:>12,} differ")
        print("\nRESULT:", "PASS — same rows, same values; every difference is the grid moving"
              if ok else "FAIL — see the counts above")
        return 0 if ok else 1
    finally:
        if not keep:
            with data_engine.begin() as conn:
                conn.execute(text(f"DROP TABLE IF EXISTS dbo.{SCRATCH}"))
            print(f"(scratch table {SCRATCH} dropped)")


if __name__ == "__main__":
    sys.exit(main())
