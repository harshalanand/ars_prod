"""Probe to quantify Hypothesis A (row-level clipping at different grain) vs
Hypothesis B (delta drift) for STK_TTL discrepancy across ARS_GRID_MJ* tables.

Read-only. Targets the local Rep_data DB via the project SQLAlchemy engine."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.session import get_data_engine
from sqlalchemy import text

eng = get_data_engine()

GRIDS = [
    "ARS_GRID_MJ",
    "ARS_GRID_MJ_MERGE_RNG_SEG",
    "ARS_GRID_MJ_RNG_SEG",
    "ARS_GRID_MJ_MACRO_MVGR",
    "ARS_GRID_MJ_MICRO_MVGR",
    "ARS_GRID_MJ_FAB",
    "ARS_GRID_MJ_CLR",
    "ARS_GRID_MJ_M_VND_CD",
    "ARS_GRID_MJ_GEN_ART",
    "ARS_GRID_MJ_VAR_ART",
]


def hdr(s):
    print(); print("=" * 96); print(s); print("=" * 96)


def cols(tbl):
    with eng.connect() as c:
        return [r[0] for r in c.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=:t "
            "ORDER BY ORDINAL_POSITION"
        ), {"t": tbl}).fetchall()]


def stk_sloc_cols(tbl):
    """Return the columns in [tbl] whose name is also an ACTIVE SLOC with KPI='STK'."""
    with eng.connect() as c:
        active = {r[0] for r in c.execute(text(
            "SELECT SLOC FROM ARS_STORE_SLOC_SETTINGS WITH (NOLOCK) "
            "WHERE UPPER(STATUS)='ACTIVE' AND UPPER(KPI)='STK'"
        )).fetchall()}
    return [c for c in cols(tbl) if c in active]


# ---------------------------------------------------------------------------
# TASK 1 + 3: per-grid totals + sys.tables timestamps
# ---------------------------------------------------------------------------
hdr("TASK 1 + 3  Per-grid totals, deltas, and create/modify timestamps")

with eng.connect() as c:
    meta_rows = c.execute(text(
        "SELECT name, create_date, modify_date FROM sys.tables WHERE name IN ("
        + ",".join(f"'{g}'" for g in GRIDS) + ")"
    )).fetchall()
meta = {r[0]: (r[1], r[2]) for r in meta_rows}

base_stk = None
base_pend = None
print(f"{'TABLE':<28} {'ROWS':>10} {'PEND_ALC':>14} {'STK_TTL':>14} {'D_STK_vs_MJ':>14} {'D_PEND_vs_MJ':>14} {'CREATED':<20} {'MODIFIED':<20}")
print("-" * 138)
results = {}
for g in GRIDS:
    with eng.connect() as c:
        try:
            r = c.execute(text(
                f"SELECT COUNT(*), SUM(CAST(PEND_ALC AS FLOAT)), SUM(CAST(STK_TTL AS FLOAT)) FROM [{g}] WITH (NOLOCK)"
            )).fetchone()
            n = int(r[0] or 0); p = float(r[1] or 0); s = float(r[2] or 0)
        except Exception as e:
            print(f"{g:<28} ERROR: {e}")
            continue
    results[g] = (n, p, s)
    if g == "ARS_GRID_MJ":
        base_stk = s; base_pend = p
    d_stk = s - base_stk if base_stk is not None else 0
    d_pend = p - base_pend if base_pend is not None else 0
    cd, md = meta.get(g, (None, None))
    print(f"{g:<28} {n:>10} {p:>14.0f} {s:>14.0f} {d_stk:>+14.0f} {d_pend:>+14.0f} {str(cd)[:19]:<20} {str(md)[:19]:<20}")


# ---------------------------------------------------------------------------
# TASK 2: Quantify clipping at three different grains (MJ, GEN_ART, VAR_ART)
# ---------------------------------------------------------------------------
hdr("TASK 2  Clipping quantification (raw uncapped vs CASE-clipped, per grid)")

for tbl in ["ARS_GRID_MJ", "ARS_GRID_MJ_GEN_ART", "ARS_GRID_MJ_VAR_ART"]:
    stk_cols_in_tbl = stk_sloc_cols(tbl)
    if not stk_cols_in_tbl:
        print(f"{tbl}: no STK-KPI SLOC columns present"); continue
    raw_sum = " + ".join(f"ISNULL([{s}],0)" for s in stk_cols_in_tbl)
    # PEND_ALC is also KPI='STK' in most setups; exclude PEND_ALC from raw_sum
    # to compare with stk_ttl which DOES include PEND_ALC if KPI is STK.
    # The probe should mirror what grid_builder does: stk_ttl = clipped raw_sum
    # including PEND_ALC if its KPI is STK. So include all KPI=STK SLOCs.
    q = f"""
        SELECT
            COUNT(*) AS rows_total,
            SUM(CASE WHEN ({raw_sum}) < 0 THEN 1 ELSE 0 END) AS rows_clipped,
            SUM(CAST(({raw_sum}) AS FLOAT)) AS raw_sum_total,
            SUM(CAST(STK_TTL AS FLOAT)) AS stk_ttl_total,
            SUM(CAST(CASE WHEN ({raw_sum}) < 0 THEN ({raw_sum}) ELSE 0 END AS FLOAT)) AS clipped_negative_total
        FROM [{tbl}] WITH (NOLOCK)
    """
    with eng.connect() as c:
        r = c.execute(text(q)).fetchone()
    rows_total, rows_clipped, raw_total, stk_total, clipped_neg_total = r
    print(f"\n{tbl}:")
    print(f"  STK_KPI SLOC cols ({len(stk_cols_in_tbl)}): {stk_cols_in_tbl[:6]}{' ...' if len(stk_cols_in_tbl) > 6 else ''}")
    print(f"  Rows total          : {rows_total:>14}")
    print(f"  Rows clipped (<0)   : {rows_clipped:>14}  ({100.0 * (rows_clipped or 0) / max(rows_total,1):.2f}%)")
    print(f"  RAW sum of stk slocs: {float(raw_total or 0):>14.0f}")
    print(f"  STK_TTL sum (clipped): {float(stk_total or 0):>14.0f}")
    print(f"  Clip uplift (STK_TTL - RAW) : {float(stk_total or 0) - float(raw_total or 0):>+14.0f}")
    print(f"  Sum of negative-row raw     : {float(clipped_neg_total or 0):>+14.0f}   <- magnitude of clipping at this grain")


# ---------------------------------------------------------------------------
# TASK 4: Concrete row-level example: pick one (WERKS, MAJ_CAT) where MJ and
# VAR_ART totals differ a lot, show the article-level breakdown.
# ---------------------------------------------------------------------------
hdr("TASK 4  Concrete row-level example of clipping divergence")

stk_cols_var = stk_sloc_cols("ARS_GRID_MJ_VAR_ART")
stk_cols_mj  = stk_sloc_cols("ARS_GRID_MJ")
raw_sum_var = " + ".join(f"ISNULL([{s}],0)" for s in stk_cols_var) or "0"
raw_sum_mj  = " + ".join(f"ISNULL([{s}],0)" for s in stk_cols_mj) or "0"

# Find a (WERKS, MAJ_CAT) where SUM(STK_TTL) in VAR_ART exceeds MJ.STK_TTL by a lot.
# We need WERKS and MAJ_CAT columns to be present in both.
q_find = f"""
    SELECT TOP 5 v.WERKS, v.MAJ_CAT,
           v.stk_var, m.stk_mj, (v.stk_var - m.stk_mj) AS diff
    FROM (
        SELECT WERKS, MAJ_CAT, SUM(CAST(STK_TTL AS FLOAT)) AS stk_var
        FROM ARS_GRID_MJ_VAR_ART WITH (NOLOCK)
        GROUP BY WERKS, MAJ_CAT
    ) v
    JOIN (
        SELECT WERKS, MAJ_CAT, SUM(CAST(STK_TTL AS FLOAT)) AS stk_mj
        FROM ARS_GRID_MJ WITH (NOLOCK)
        GROUP BY WERKS, MAJ_CAT
    ) m ON m.WERKS = v.WERKS AND m.MAJ_CAT = v.MAJ_CAT
    ORDER BY (v.stk_var - m.stk_mj) DESC
"""
with eng.connect() as c:
    cands = c.execute(text(q_find)).fetchall()

if not cands:
    print("No candidate (WERKS, MAJ_CAT) pair returned.")
else:
    print(f"Top 5 (WERKS, MAJ_CAT) by (VAR_ART.STK_TTL - MJ.STK_TTL):")
    print(f"  {'WERKS':<6} {'MAJ_CAT':<10} {'STK_VAR':>12} {'STK_MJ':>12} {'DIFF':>10}")
    for c in cands:
        print(f"  {str(c[0]):<6} {str(c[1]):<10} {float(c[2]):>12.0f} {float(c[3]):>12.0f} {float(c[4]):>+10.0f}")

    werks, majcat = cands[0][0], cands[0][1]
    print(f"\nDrill-down for WERKS={werks}, MAJ_CAT={majcat}")

    # Show per-VAR_ART row: raw_sum_var, STK_TTL (clipped), and whether it got clipped
    # Try GEN_ART_NUMBER + VAR_ART_NUMBER as the article-level identifier.
    var_cols = [c.upper() for c in cols("ARS_GRID_MJ_VAR_ART")]
    article_key = None
    for cand in ("VAR_ART_NUMBER", "VARIANT_ARTICLE", "MATNR", "GEN_ART_NUMBER", "ARTICLE_NUMBER"):
        if cand in var_cols:
            article_key = cand
            break
    print(f"  Using article key: {article_key}")

    q_detail = f"""
        SELECT TOP 20 [{article_key}],
               CAST(({raw_sum_var}) AS FLOAT) AS raw_row_sum,
               CAST(STK_TTL AS FLOAT)         AS stk_ttl_clipped,
               CASE WHEN ({raw_sum_var}) < 0 THEN 1 ELSE 0 END AS was_clipped
        FROM ARS_GRID_MJ_VAR_ART WITH (NOLOCK)
        WHERE WERKS = :w AND MAJ_CAT = :m
        ORDER BY CAST(({raw_sum_var}) AS FLOAT) ASC
    """
    with eng.connect() as c:
        rows = c.execute(text(q_detail), {"w": werks, "m": majcat}).fetchall()

    print(f"\n  Bottom-20 VAR_ART rows (most-negative raw sums) in this MJ_CAT:")
    print(f"  {'ARTICLE':<22} {'RAW_SUM':>14} {'STK_TTL':>14} {'CLIPPED':>8}")
    for r in rows:
        print(f"  {str(r[0]):<22} {float(r[1]):>14.2f} {float(r[2]):>14.2f} {int(r[3]):>8}")

    # Aggregate at MAJ_CAT grain: what if we summed first, then clipped once
    q_agg = f"""
        SELECT
            SUM(CAST(({raw_sum_var}) AS FLOAT))                         AS raw_sum_at_var_grain,
            SUM(CAST(STK_TTL AS FLOAT))                                  AS stk_ttl_var_grain,
            CAST(CASE WHEN SUM({raw_sum_var}) < 0 THEN 0 ELSE SUM({raw_sum_var}) END AS FLOAT)
                                                                         AS would_be_at_mj_grain
        FROM ARS_GRID_MJ_VAR_ART WITH (NOLOCK)
        WHERE WERKS = :w AND MAJ_CAT = :m
    """
    with eng.connect() as c:
        r = c.execute(text(q_agg), {"w": werks, "m": majcat}).fetchone()
    raw_at_var, stk_at_var, would_be_mj = float(r[0] or 0), float(r[1] or 0), float(r[2] or 0)

    q_mj = f"""
        SELECT
            SUM(CAST(({raw_sum_mj}) AS FLOAT)) AS raw_sum_mj,
            SUM(CAST(STK_TTL AS FLOAT))         AS stk_ttl_mj
        FROM ARS_GRID_MJ WITH (NOLOCK)
        WHERE WERKS = :w AND MAJ_CAT = :m
    """
    with eng.connect() as c:
        r = c.execute(text(q_mj), {"w": werks, "m": majcat}).fetchone()
    raw_at_mj, stk_at_mj = float(r[0] or 0), float(r[1] or 0)

    print(f"\n  Summary for (WERKS={werks}, MAJ_CAT={majcat}):")
    print(f"    Raw stock total (sum of SLOC qtys, can be negative):")
    print(f"      at VAR_ART grain (then aggregated)  : {raw_at_var:>14.0f}")
    print(f"      at MJ      grain                    : {raw_at_mj:>14.0f}")
    print(f"    STK_TTL totals (after per-row clip):")
    print(f"      VAR_ART grid: SUM(STK_TTL)          : {stk_at_var:>14.0f}   <- many small clips")
    print(f"      MJ grid:      SUM(STK_TTL)          : {stk_at_mj:>14.0f}   <- one big clip")
    print(f"    Hypothetical 'sum-then-clip-once at MJ grain using VAR rows': {would_be_mj:>14.0f}")
    print(f"    Clip-divergence overhead = {stk_at_var - stk_at_mj:>+.0f}")
