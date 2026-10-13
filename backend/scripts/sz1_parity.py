"""Prove 1SZ ALC gives the SZ1 Allocation Streamlit tool's numbers.

Imports the tool's own msa_base / req_base / art_store (read-only: no bytecode
is written into its folder), points them at this backend's connection, and
compares, on exactly the same live data:

    step1   the Step 1 selection, every column of every row
    step2   the Step 2 grid, every column of every row (both read the TOOL's
            SZ1_MSA_SZ_BASE, so the comparison does not depend on when either
            base table was built)
    step3   the candidate lines (SQL + reserve), then the store priority and
            the pack-by-pack allocation run by both engines on one frame

Nothing is written anywhere. The tool's warehouse modes only (same, home_first,
pooled, fallback); ARS's All RDC pool has no tool equivalent. Full-stock
("initial") runs only: a
remaining-stock run deliberately differs (it also uses REM_MJ_SZ_SRT — see
sz1_run_service's docstring).

Usage (from backend/):
    python scripts/sz1_parity.py                          # steps 1-3, stores of the tool's latest session
    python scripts/sz1_parity.py --steps 3 --cross pooled --mode mj_srt
    python scripts/sz1_parity.py --steps 3 --stores HB05,HB06,HY04 --cross home_first
    python scripts/sz1_parity.py --steps 3 --matrix --max-stores 60   # every warehouse × priority mode
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
TOOL_DIR = r"D:\001- WIP PROJECT WITH VERSION\1SZ ALLOC"

import pandas as pd  # noqa: E402
from loguru import logger  # noqa: E402

logger.remove()
from sqlalchemy import text  # noqa: E402

from app.database.session import data_engine  # noqa: E402
from app.services import sz1_alloc_engine as E  # noqa: E402
from app.services import sz1_base_service as B  # noqa: E402
from app.services import sz1_run_service as R  # noqa: E402
from app.services import sz1_schema as S  # noqa: E402

TOL = 0.005


def import_tool():
    sys.dont_write_bytecode = True
    sys.path.insert(0, TOOL_DIR)
    import db as tool_db                       # noqa: E402  (the tool's, read-only)
    import msa_base, req_base, art_store       # noqa: E402
    cfg = {"schema": "dbo"}
    tool_db.engine = lambda: data_engine       # same connection, same data
    for m in (tool_db, msa_base, req_base, art_store):
        m.get_config = lambda: cfg
    return msa_base, req_base, art_store


def frame(sql, params=None):
    with data_engine.connect() as c:
        res = c.execute(text(sql), params or {})
        return pd.DataFrame(res.fetchall(), columns=list(res.keys()))


def compare(name, a: pd.DataFrame, b: pd.DataFrame, keys, cols=None) -> bool:
    """Every column of `cols` (default: all shared) equal on `keys`."""
    cols = cols or [c for c in a.columns if c in b.columns and c not in keys]
    print(f"  {name}: tool {len(a):,} rows · ARS {len(b):,} rows")
    m = a.merge(b, on=keys, how="outer", suffixes=("_t", "_a"), indicator=True)
    only_t, only_a = (m["_merge"] == "left_only").sum(), (m["_merge"] == "right_only").sum()
    ok = not only_t and not only_a
    if not ok:
        print(f"    rows only in tool: {only_t:,} · only in ARS: {only_a:,}")
        print(m[m["_merge"] != "both"][keys + ["_merge"]].head(8).to_string(index=False))
    both = m[m["_merge"] == "both"]
    bad_cols = []
    for c in cols:
        x, y = both[f"{c}_t"], both[f"{c}_a"]
        xn = pd.to_numeric(x, errors="coerce").astype(float)
        yn = pd.to_numeric(y, errors="coerce").astype(float)
        if xn.notna().any() or yn.notna().any():
            diff = ~(((xn - yn).abs() <= TOL) | (xn.isna() & yn.isna()))
        else:
            diff = ~((x.astype(str) == y.astype(str)) | (x.isna() & y.isna()))
        if diff.any():
            bad_cols.append(c)
            print(f"    {c}: {int(diff.sum()):,} differ, e.g.")
            print(both.loc[diff, keys + [f"{c}_t", f"{c}_a"]].head(5).to_string(index=False))
    ok = ok and not bad_cols
    print(f"    {'MATCH' if ok else 'DIFFER'} on {len(cols)} columns")
    return ok


def step1(msa_base) -> bool:
    print("Step 1 — SZ Base selection")
    t = frame(msa_base.selection_sql())
    from app.services import sz1_settings
    a = frame(B.base_sql("SELECT * FROM selected", sz1_settings.TOOL_PRESET))   # the tool's fixed rules
    return compare("selection", t, a, ["RDC", "ARTICLE_NUMBER"])


def step2(req_base) -> bool:
    print("Step 2 — ST/MJ/SZ req grid (both on the tool's SZ1_MSA_SZ_BASE)")
    t = frame(req_base.selection_sql())
    keep = S.SZ_BASE
    S.SZ_BASE = "SZ1_MSA_SZ_BASE"
    try:
        a = frame(B.req_sql("SELECT * FROM grid", share_rule="tool"))
    finally:
        S.SZ_BASE = keep
    # ARS builds every store's need for the pairs of ALL RDCs (for the All RDC
    # pool); the tool only its own RDC's pairs. Compare on the tool's pairs.
    own = frame("SELECT DISTINCT RDC, MAJ_CAT, SZ FROM dbo.SZ1_MSA_SZ_BASE")
    extra = len(a)
    a = a.merge(own, on=["RDC", "MAJ_CAT", "SZ"])
    print(f"  ARS grid {extra:,} rows, {len(a):,} on the store's own-RDC pairs")
    return compare("grid", t, a, ["RDC", "ST_CD", "MAJ_CAT", "SZ"])


def tool_candidates(art_store, codes, cross):
    sql, params = art_store._selection(codes, "initial", cross)
    cand = art_store.read_sql(sql, params)
    res = art_store.reserve("initial").rename(columns={"RDC": "SRC_RDC"})
    cand = cand.merge(res, on=["SRC_RDC", "ARTICLE_NUMBER"], how="left")
    cand["OWN_NEED"] = pd.to_numeric(cand["OWN_NEED"], errors="coerce").fillna(0.0)
    stock_q = pd.to_numeric(cand["ART_FNL_Q"], errors="coerce").fillna(0.0)
    cand["SURPLUS_QTY"] = (stock_q - cand["OWN_NEED"]).clip(lower=0).round()
    cand["OWN_NEED"] = cand["OWN_NEED"].round().astype(int)
    cand["SURPLUS_QTY"] = cand["SURPLUS_QTY"].astype(int)
    return cand


def step3(art_store, codes, cross, mode, weight) -> bool:
    print(f"Step 3 — {len(codes)} stores · {cross} · {mode}" + (f" {weight}" if mode == "weighted" else ""))
    # both read the tool's built tables, so a difference can only be logic
    keep = (S.SZ_BASE, S.REQ_BASE, S.LINES, S.SESSION)
    S.SZ_BASE, S.REQ_BASE, S.LINES = "SZ1_MSA_SZ_BASE", "SZ1_ST_MJ_SZ_REQ_BASE", "SZ1_ST_ART_REQ"
    try:
        t0 = time.time()
        tc = tool_candidates(art_store, codes, cross)
        t1 = time.time()
        with data_engine.connect() as c:
            ac = R.candidates(c, codes, "initial", cross)
        t2 = time.time()
    finally:
        S.SZ_BASE, S.REQ_BASE, S.LINES, S.SESSION = keep
    print(f"  read: tool {t1 - t0:.1f}s · ARS {t2 - t1:.1f}s")
    keys = ["ST_CD", "ARTICLE_NUMBER", "SRC_RDC"]
    ok = compare("candidates", tc, ac, keys)

    seq = {c: i for i, c in enumerate(codes, 1)} if mode == "sequence" else None
    t0 = time.time()
    tr = art_store.allocate(art_store.add_priority(tc, mode, seq, weight), "none", cross)
    t1 = time.time()
    ar = E.allocate(E.add_priority(ac, mode, seq, weight), "none", cross)
    t2 = time.time()
    print(f"  allocate: tool {t1 - t0:.1f}s · ARS {t2 - t1:.1f}s · "
          f"units tool {tr['ALLOC_QTY'].sum():,.0f} · ARS {ar['ALLOC_QTY'].sum():,.0f}")
    ok &= compare("allocation", tr, ar, keys,
                  ["ST_SEQ", "ALLOC_QTY", "ALC_I_ROD", "PACKS", "UNMET_QTY", "BAL_ART_FNL_Q", "REM_MJ_SZ_SRT"])
    return ok


def latest_tool_stores(limit):
    rows = frame("""SELECT TOP 1 SESSION_ID FROM dbo.SZ1_ST_ART_REQ GROUP BY SESSION_ID ORDER BY MAX(SESSION_AT) DESC""")
    sid = rows.iat[0, 0]
    st = frame("SELECT DISTINCT ST_CD FROM dbo.SZ1_ST_ART_REQ WHERE SESSION_ID = :s ORDER BY ST_CD", {"s": sid})
    codes = st["ST_CD"].tolist()
    print(f"Stores from the tool's latest session {sid}: {len(codes)}" + (f", first {limit} used" if limit else ""))
    return codes[:limit] if limit else codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="1,2,3")
    ap.add_argument("--stores", default="")
    ap.add_argument("--max-stores", type=int, default=0)
    ap.add_argument("--cross", default="same", choices=["same", *E.LEGACY_CROSS_RDC_MODES])
    ap.add_argument("--mode", default="weighted", choices=list(E.PRIORITY_MODES))
    ap.add_argument("--weight", type=float, default=0.5)
    ap.add_argument("--matrix", action="store_true", help="step 3 under every warehouse mode × priority mode")
    o = ap.parse_args()
    msa_base, req_base, art_store = import_tool()
    steps = {s.strip() for s in o.steps.split(",")}
    results = {}
    if "1" in steps:
        results["step1"] = step1(msa_base)
    if "2" in steps:
        results["step2"] = step2(req_base)
    if "3" in steps:
        codes = E.parse_store_codes(o.stores) if o.stores else latest_tool_stores(o.max_stores)
        if o.matrix:
            for cross in ["same", *E.LEGACY_CROSS_RDC_MODES]:
                for mode in E.PRIORITY_MODES:
                    results[f"step3 {cross}/{mode}"] = step3(art_store, codes, cross, mode, o.weight)
        else:
            results["step3"] = step3(art_store, codes, o.cross, o.mode, o.weight)
    print("\n" + "\n".join(f"{k:28s} {'MATCH' if v else 'DIFFER'}" for k, v in results.items()))
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
