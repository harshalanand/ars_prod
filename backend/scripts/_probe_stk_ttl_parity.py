"""STK_TTL parity probe across all ARS_GRID_MJ* tables for the same
(WERKS, MAJ_CAT, GEN_ART). Each grid is a slice of the same physical stock
by a different attribute, so SUM(STK_TTL) per (WERKS, MAJ_CAT, GEN_ART)
must be identical across grids that include GEN_ART_NUMBER as a key, and
SUM per (WERKS, MAJ_CAT) must be identical across all rollup grids."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.session import get_data_engine
from sqlalchemy import text

eng = get_data_engine()


def hdr(s):
    print(); print("=" * 78); print(s); print("=" * 78)


def cols(tbl):
    with eng.connect() as c:
        return {r[0].upper() for r in c.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=:t"
        ), {"t": tbl}).fetchall()}


def total_stk_ttl(tbl):
    with eng.connect() as c:
        try:
            r = c.execute(text(
                f"SELECT COUNT(*), SUM(CAST(STK_TTL AS FLOAT)), "
                f"       SUM(CAST(PEND_ALC AS FLOAT)) FROM [{tbl}]"
            )).fetchone()
            return int(r[0] or 0), float(r[1] or 0), float(r[2] or 0)
        except Exception as e:
            return None, None, str(e)


hdr("A. Per-grid total row count + STK_TTL + PEND_ALC")
print(f"{'TABLE':<32} {'ROWS':>10} {'STK_TTL_SUM':>16} {'PEND_ALC_SUM':>16}")
print("-" * 76)
grids = [
    "ARS_GRID_MJ",
    "ARS_GRID_MJ_CLR",
    "ARS_GRID_MJ_FAB",
    "ARS_GRID_MJ_MACRO_MVGR",
    "ARS_GRID_MJ_MICRO_MVGR",
    "ARS_GRID_MJ_M_VND_CD",
    "ARS_GRID_MJ_VND_CD",
    "ARS_GRID_MJ_RNG_SEG",
    "ARS_GRID_MJ_MERGE_RNG_SEG",
    "ARS_GRID_MJ_GEN_ART",
    "ARS_GRID_MJ_VAR_ART",
]
totals = {}
for g in grids:
    n, s, p = total_stk_ttl(g)
    if n is None: print(f"{g:<32} ERROR: {p}"); continue
    totals[g] = (n, s, p)
    print(f"{g:<32} {n:>10} {s:>16.0f} {p:>16.0f}")


hdr("B. Per (WERKS, MAJ_CAT) STK_TTL — pairwise diff between rollup grids")
# All rollup grids have (WERKS, MAJ_CAT, ...attribute..., STK_TTL).
# Grouping by (WERKS, MAJ_CAT) and summing STK_TTL should give the SAME
# value across grids (they all aggregate the same physical stock differently).
ref = "ARS_GRID_MJ"  # reference: pure (WERKS, MAJ_CAT) grain
with eng.connect() as c:
    ref_rows = {
        (r[0], r[1]): float(r[2] or 0)
        for r in c.execute(text(
            f"SELECT WERKS, MAJ_CAT, SUM(CAST(STK_TTL AS FLOAT)) "
            f"FROM {ref} GROUP BY WERKS, MAJ_CAT"
        )).fetchall()
    }
print(f"Reference grid: {ref} -> {len(ref_rows)} (WERKS, MAJ_CAT) keys")
print()

for g in grids:
    if g == ref: continue
    if g in ("ARS_GRID_MJ_GEN_ART", "ARS_GRID_MJ_VAR_ART"):
        continue  # different grain — will be checked separately
    with eng.connect() as c:
        try:
            other = {
                (r[0], r[1]): float(r[2] or 0)
                for r in c.execute(text(
                    f"SELECT WERKS, MAJ_CAT, SUM(CAST(STK_TTL AS FLOAT)) "
                    f"FROM {g} GROUP BY WERKS, MAJ_CAT"
                )).fetchall()
            }
        except Exception as e:
            print(f"{g}: ERROR {e}"); continue
    # Diffs > 1 unit
    mismatches = []
    ref_total = 0.0
    other_total = 0.0
    for k, v in ref_rows.items():
        ref_total += v
        v2 = other.get(k, 0.0)
        other_total += v2
        diff = v - v2
        if abs(diff) >= 1.0:
            mismatches.append((k, v, v2, diff))
    # also check keys in other not in ref
    for k, v2 in other.items():
        if k not in ref_rows:
            if abs(v2) >= 1.0:
                mismatches.append((k, 0.0, v2, -v2))
    print(f"{g:<32}: ref_total={ref_total:>14.0f} other_total={other_total:>14.0f} "
          f"|delta|={abs(ref_total-other_total):>10.0f} mismatched_keys={len(mismatches)}")
    if mismatches[:3]:
        for k, v, v2, d in mismatches[:3]:
            print(f"   {k} ref={v:.0f} {g.replace('ARS_GRID_MJ_','')}={v2:.0f} diff={d:.0f}")


hdr("C. GEN_ART grain — sum STK_TTL by (WERKS, MAJ_CAT) vs ARS_GRID_MJ_GEN_ART")
with eng.connect() as c:
    try:
        gen = {
            (r[0], r[1]): float(r[2] or 0)
            for r in c.execute(text(
                "SELECT WERKS, MAJ_CAT, SUM(CAST(STK_TTL AS FLOAT)) "
                "FROM ARS_GRID_MJ_GEN_ART GROUP BY WERKS, MAJ_CAT"
            )).fetchall()
        }
        gen_total = sum(gen.values())
        ref_total_for_compare = sum(ref_rows.values())
        print(f"{ref}.STK_TTL.SUM   = {ref_total_for_compare:.0f}")
        print(f"GEN_ART.STK_TTL.SUM = {gen_total:.0f}")
        print(f"delta               = {ref_total_for_compare - gen_total:.0f}")
    except Exception as e:
        print(f"ERROR: {e}")


hdr("D. VAR_ART grain — sum STK_TTL by (WERKS, MAJ_CAT) vs ARS_GRID_MJ")
with eng.connect() as c:
    try:
        var = {
            (r[0], r[1]): float(r[2] or 0)
            for r in c.execute(text(
                "SELECT WERKS, MAJ_CAT, SUM(CAST(STK_TTL AS FLOAT)) "
                "FROM ARS_GRID_MJ_VAR_ART GROUP BY WERKS, MAJ_CAT"
            )).fetchall()
        }
        var_total = sum(var.values())
        ref_total_for_compare = sum(ref_rows.values())
        print(f"{ref}.STK_TTL.SUM   = {ref_total_for_compare:.0f}")
        print(f"VAR_ART.STK_TTL.SUM = {var_total:.0f}")
        print(f"delta               = {ref_total_for_compare - var_total:.0f}")
    except Exception as e:
        print(f"ERROR: {e}")
