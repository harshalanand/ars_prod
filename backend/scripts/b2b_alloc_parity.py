"""Prove the ARS allocation engine gives the Streamlit tool's lines and picks.

Imports the tool's own allocate.py (read-only: no bytecode is written into its
folder) and runs its allocate_rows + split_to_bins on exactly the inputs the
ARS engine reads — the ARS_B2B_* tables — then compares:

    lines   every (store, article): ALLOC_QTY and all 26 columns
    picks   every (bin, article, store): QTY, PICK_SEQ, BIN_QTY_LEFT, …
    stats   candidates, every skip reason, cross-warehouse units, totals

The tool is fed per MAJ_CAT, the way it runs itself (ALLOC_CHUNK_BY = MAJ_CAT);
ARS runs each category as its own task. Nothing is written anywhere.

Usage (from backend/):
    python scripts/b2b_alloc_parity.py                          # every category, tool defaults
    python scripts/b2b_alloc_parity.py --cats 40 --fill ROUND_ROBIN --fair FLAT --cross SAME
    python scripts/b2b_alloc_parity.py --matrix --cats 60       # all modes on the 60 largest categories
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
TOOL_DIR = r"D:\001- WIP PROJECT WITH VERSION\GRT_ART_ALLOC"

from loguru import logger  # noqa: E402

logger.remove()
from app.database.session import data_engine  # noqa: E402
from app.services import b2b_alloc_engine as E  # noqa: E402
from app.services import b2b_schema as S  # noqa: E402

ARS = {"bin": S.BIN_MASTER, "store": S.STORE_MASTER, "req": S.REQ, "mbq": S.ART_MBQ,
       "bin_rdc": "BIN_RDC"}


def import_tool():
    sys.dont_write_bytecode = True           # leave the tool's folder exactly as it is
    sys.path.insert(0, TOOL_DIR)
    import config as tool_config             # noqa: E402  (the tool's, read-only)
    import allocate as tool                  # noqa: E402
    # Point the tool at the ARS tables, in memory only, so both engines read
    # the same rows. Its BIN_RDC_SQL parses the BIN prefix, which on our
    # prefixed bins equals the stored BIN_RDC.
    tool_config.TBL_BIN_MASTER = S.BIN_MASTER
    tool_config.TBL_STORE_MASTER = S.STORE_MASTER
    tool_config.TBL_REQ = S.REQ
    tool_config.TBL_ART_MBQ = S.ART_MBQ
    return tool


def run_tool(tool, cats, o):
    t0 = time.time()
    _c, supply, caps, bins, store_rdc = tool.load_inputs(data_engine, with_cand=False)
    store_nm, art_attrs = tool.load_labels(data_engine)
    cand = (tool.load_candidates(data_engine, c) for c in cats)
    rows, stats = tool.allocate_rows(cand, supply, caps, o["priority"], o["min_qty"], o["fill_mode"],
                                     o["fair_basis"], o["cross_rdc"], store_nm, art_attrs)
    picks, unf = tool.split_to_bins(rows, bins, store_rdc, o["bin_pick"], o["cross_rdc"])
    return rows, picks, stats, unf, time.time() - t0


def run_ars(cats, o, workers):
    """The production path: allocate_all, categories on a process pool."""
    t0 = time.time()
    r = E.allocate_all(ARS, o, workers=workers, only=cats)
    return r["lines"], r["picks"], r["stats"], r["unfulfilled"], time.time() - t0, r


def compare(tool_rows, tool_picks, tool_stats, tool_unf, lines, picks, stats, unf, cats_subset):
    ok = True
    a = {(r["STORE_CODE"], r["ART"]): r for r in tool_rows}
    b = {(r["STORE_CODE"], r["ART"]): r for r in lines}
    only_a, only_b = a.keys() - b.keys(), b.keys() - a.keys()
    diffs = {}
    for k in a.keys() & b.keys():
        for c, v in a[k].items():
            w = b[k].get(c)
            same = (v == w) or (isinstance(v, float) and isinstance(w, float) and abs(v - w) < 1e-9)
            if not same:
                diffs.setdefault(c, []).append((k, v, w))
    print(f"  lines   tool {len(a):,} · ARS {len(b):,} · only tool {len(only_a):,} · only ARS {len(only_b):,} · "
          f"columns differing: {({c: len(v) for c, v in diffs.items()} or 'none')}")
    ok &= not only_a and not only_b and not diffs
    for c, v in list(diffs.items())[:3]:
        print(f"      {c}: e.g. {v[:2]}")

    pa = {(r["BIN"], r["ART"], r["STORE_CODE"]): r for r in tool_picks}
    pb = {(r["BIN"], r["ART"], r["STORE_CODE"]): r for r in picks}
    pdiff = {}
    for k in pa.keys() & pb.keys():
        for c, v in pa[k].items():
            w = pb[k].get("STORE_RDC") if c == "RDC" else pb[k].get(c)
            if c in ("LEFT_REASON", "STORES_WANTING", "TOTAL_SHORTFALL"):
                continue                          # UNALLOC-only columns, None on picks in both
            if v != w:
                pdiff.setdefault(c, []).append((k, v, w))
    print(f"  picks   tool {len(pa):,} · ARS {len(pb):,} · only tool {len(pa.keys() - pb.keys()):,} · "
          f"only ARS {len(pb.keys() - pa.keys()):,} · columns differing: {({c: len(v) for c, v in pdiff.items()} or 'none')}")
    ok &= pa.keys() == pb.keys() and not pdiff
    print(f"  units   tool {sum(r['ALLOC_QTY'] for r in tool_rows):,} · ARS {sum(r['ALLOC_QTY'] for r in lines):,} · "
          f"picked tool {sum(r['QTY'] for r in tool_picks):,} · ARS {sum(r['QTY'] for r in picks):,} · "
          f"unfulfilled tool {tool_unf} · ARS {unf}")
    ok &= tool_unf == unf
    skip = [k for k in E.STAT_KEYS if tool_stats.get(k) != stats.get(k)]
    print(f"  stats   {'identical' if not skip else 'DIFFER: ' + str({k: (tool_stats.get(k), stats.get(k)) for k in skip})}")
    ok &= not skip
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cats", type=int, help="only the N largest categories")
    ap.add_argument("--sample", type=int, help="N categories spread evenly from largest to smallest")
    ap.add_argument("--workers", type=int, default=8, help="ARS process-pool size")
    ap.add_argument("--priority", default="SHORTFALL_DESC", choices=E.PRIORITIES)
    ap.add_argument("--min-qty", type=int, default=1)
    ap.add_argument("--fill", default="GREEDY", choices=E.FILL_MODES)
    ap.add_argument("--fair", default="PROPORTIONAL", choices=E.FAIR_BASES)
    ap.add_argument("--pick", default="MAX_CONSUMPTION", choices=E.BIN_PICKS)
    ap.add_argument("--cross", default="ANY", choices=E.CROSS_RDCS)
    ap.add_argument("--matrix", action="store_true", help="every fill mode, fair basis, warehouse rule, bin pick")
    args = ap.parse_args()

    tool = import_tool()
    with data_engine.connect() as conn:
        all_cats = [c for c, _ in E.list_categories(conn, ARS)]
    cats = all_cats[:args.cats] if args.cats else all_cats
    if args.sample:
        step = max(1, len(all_cats) // args.sample)
        cats = all_cats[::step][:args.sample]
    print(f"{len(cats)} of {len(all_cats)} categories")

    base = {"priority": args.priority, "min_qty": args.min_qty, "fill_mode": args.fill,
            "fair_basis": args.fair, "bin_pick": args.pick, "cross_rdc": args.cross}
    runs = [base]
    if args.matrix:
        runs = [
            {**base, "fill_mode": "GREEDY", "cross_rdc": "ANY", "bin_pick": "MAX_CONSUMPTION"},
            {**base, "fill_mode": "GREEDY", "cross_rdc": "HOME_FIRST", "bin_pick": "BIN_ORDER"},
            {**base, "fill_mode": "GREEDY", "cross_rdc": "SAME", "bin_pick": "MAX_CONSUMPTION"},
            {**base, "fill_mode": "ROUND_ROBIN", "fair_basis": "PROPORTIONAL", "cross_rdc": "ANY"},
            {**base, "fill_mode": "ROUND_ROBIN", "fair_basis": "FLAT", "cross_rdc": "HOME_FIRST"},
            {**base, "fill_mode": "ROUND_ROBIN", "fair_basis": "NONE", "cross_rdc": "SAME", "bin_pick": "BIN_ORDER"},
            {**base, "priority": "CONT_DESC", "min_qty": 2},
            {**base, "priority": "MBQ_DESC", "fill_mode": "ROUND_ROBIN", "min_qty": 3},
        ]
    def inputs_version():
        """Latest demand build + last load: if either moves mid-test, the two
        engines read different data and the comparison means nothing."""
        with data_engine.connect() as conn:
            return tuple(conn.exec_driver_sql(
                f"SELECT (SELECT MAX(BUILD_ID) FROM dbo.{S.MBQ_BUILD} WHERE STATUS = 'DONE'), "
                f"(SELECT MAX(UPLOAD_ID) FROM dbo.{S.UPLOAD} WHERE STATUS = 'LOADED')").fetchone())

    all_ok = True
    for o in runs:
        print(f"\n== {o}")
        before = inputs_version()
        tr = run_tool(tool, cats, o)
        print(f"  tool ran in {tr[4]:.1f}s")
        ar = run_ars(cats, o, args.workers)
        after = inputs_version()
        if after != before:
            print(f"  INVALID — the inputs changed during the test (build/upload {before} → {after}). Run it again.")
            all_ok = False
            continue
        print(f"  ARS  ran in {ar[4]:.1f}s ({ar[5]["workers"]} workers)")
        ok = compare(*tr[:4], *ar[:4], cats)
        print("  RESULT:", "PASS — identical" if ok else "FAIL")
        all_ok &= ok
    print("\nOVERALL:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
