"""Why does the RAW (uncapped) stock sum differ between ARS_GRID_MJ and ARS_GRID_MJ_VAR_ART?
Both are built from the same ET_STORE_STOCK base. The clipping accounts for some of
the STK_TTL delta but ~222K still seems to come from raw-sum drift. Quantify."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.session import get_data_engine
from sqlalchemy import text

eng = get_data_engine()

with eng.connect() as c:
    stk_cols = [r[0] for r in c.execute(text(
        "SELECT SLOC FROM ARS_STORE_SLOC_SETTINGS WITH (NOLOCK) "
        "WHERE UPPER(STATUS)='ACTIVE' AND UPPER(KPI)='STK'"
    )).fetchall()]
print(f"KPI=STK SLOCs ({len(stk_cols)}): {stk_cols}")

raw_sum_expr = " + ".join(f"ISNULL([{s}],0)" for s in stk_cols)

for tbl in ["ARS_GRID_MJ", "ARS_GRID_MJ_GEN_ART", "ARS_GRID_MJ_VAR_ART"]:
    with eng.connect() as c:
        r = c.execute(text(
            f"SELECT SUM(CAST(({raw_sum_expr}) AS FLOAT)) FROM [{tbl}] WITH (NOLOCK)"
        )).fetchone()
    print(f"{tbl:<28} raw_sum = {float(r[0] or 0):>14.0f}")

# Now do raw-sum at (WERKS,MAJ_CAT) grain on each, and quantify any row-count drop
for tbl in ["ARS_GRID_MJ", "ARS_GRID_MJ_VAR_ART"]:
    with eng.connect() as c:
        r = c.execute(text(
            f"SELECT COUNT(DISTINCT CAST(WERKS AS NVARCHAR(50)) + '|' + CAST(MAJ_CAT AS NVARCHAR(100))) "
            f"FROM [{tbl}] WITH (NOLOCK)"
        )).fetchone()
    print(f"{tbl:<28} distinct (WERKS,MAJ_CAT) = {int(r[0] or 0)}")

# Show MJ raw sum AND VAR_ART raw sum aggregated to (WERKS,MAJ_CAT), find the rows
# that appear in VAR_ART but not in MJ (or vice versa).
print("\nWERKS,MAJ_CAT cells with biggest raw-sum gap (VAR_ART - MJ):")
q = f"""
    SELECT TOP 10 v.WERKS, v.MAJ_CAT, v.raw_var, m.raw_mj, (v.raw_var - m.raw_mj) AS gap
    FROM (
        SELECT WERKS, MAJ_CAT, SUM(CAST(({raw_sum_expr}) AS FLOAT)) AS raw_var
        FROM ARS_GRID_MJ_VAR_ART WITH (NOLOCK)
        GROUP BY WERKS, MAJ_CAT
    ) v
    FULL OUTER JOIN (
        SELECT WERKS, MAJ_CAT, SUM(CAST(({raw_sum_expr}) AS FLOAT)) AS raw_mj
        FROM ARS_GRID_MJ WITH (NOLOCK)
        GROUP BY WERKS, MAJ_CAT
    ) m ON m.WERKS = v.WERKS AND m.MAJ_CAT = v.MAJ_CAT
    WHERE ISNULL(v.raw_var,0) <> ISNULL(m.raw_mj,0)
    ORDER BY ABS(ISNULL(v.raw_var,0) - ISNULL(m.raw_mj,0)) DESC
"""
with eng.connect() as c:
    for r in c.execute(text(q)).fetchall():
        print(f"  W={r[0]} MJ={r[1]:<22} raw_var={float(r[2] or 0):>12.0f} raw_mj={float(r[3] or 0):>12.0f} gap={float(r[4] or 0):>+10.0f}")
