"""Verify mj_req_growth_pct unification.

Runs the pandas engine twice:
  1) mj_req_growth_pct=110 -> R09 factor=1.10 across RL/TBC/TBL
  2) mj_req_growth_pct=100 -> R09 factor=1.00 across RL/TBC/TBL (gate strict)

For each run, restrict to HN43 + M_TEES_HS and print
SUM(SHIP_QTY) by OPT_TYPE.
"""
import sys, os, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database.session import data_engine
from app.services.rule_engine_pandas import run_listing_and_allocation_pandas

# Capture audit log line emitted by the engine
logging.basicConfig(level=logging.INFO, format="%(message)s")

WERKS = "HN43"
MAJ_CAT = "M_TEES_HS"


def restrict_working_to_one_majcat():
    """Snapshot ARS_LISTING_WORKING, then narrow it to HN43 + M_TEES_HS for the run."""
    with data_engine.begin() as c:
        # Save
        c.execute(text("IF OBJECT_ID('tempdb..#bak_full') IS NOT NULL DROP TABLE #bak_full"))
        c.execute(text("SELECT * INTO ARS_LISTING_WORKING__BAK FROM ARS_LISTING_WORKING")) \
            if not _table_exists(c, "ARS_LISTING_WORKING__BAK") else None
        # NB: only back up once — re-running this script reuses the backup.


def _table_exists(conn, name):
    return conn.execute(text(
        "SELECT COUNT(*) FROM sys.tables WHERE name=:n"
    ), {"n": name}).scalar() > 0


def backup_once():
    with data_engine.begin() as c:
        if not _table_exists(c, "ARS_LISTING_WORKING__BAK_MJG"):
            c.execute(text("SELECT * INTO ARS_LISTING_WORKING__BAK_MJG FROM ARS_LISTING_WORKING"))
            print("[setup] backed up ARS_LISTING_WORKING -> ARS_LISTING_WORKING__BAK_MJG")
        else:
            print("[setup] backup ARS_LISTING_WORKING__BAK_MJG already exists")


def narrow_to_target():
    with data_engine.begin() as c:
        c.execute(text("TRUNCATE TABLE ARS_LISTING_WORKING"))
        c.execute(text(
            "INSERT INTO ARS_LISTING_WORKING "
            "SELECT * FROM ARS_LISTING_WORKING__BAK_MJG "
            "WHERE WERKS=:w AND MAJ_CAT=:m"
        ), {"w": WERKS, "m": MAJ_CAT})
        # Clear alloc so the next summarize sees only the new run's output
        try:
            c.execute(text("TRUNCATE TABLE ARS_ALLOC_WORKING"))
        except Exception:
            c.execute(text("DELETE FROM ARS_ALLOC_WORKING"))
        try:
            c.execute(text("TRUNCATE TABLE ARS_LISTED_OPT"))
        except Exception:
            c.execute(text("DELETE FROM ARS_LISTED_OPT"))
        n = c.execute(text("SELECT COUNT(*) FROM ARS_LISTING_WORKING")).scalar()
        print(f"[setup] narrowed ARS_LISTING_WORKING to {WERKS}+{MAJ_CAT}: {n} rows (cleared ARS_ALLOC_WORKING/ARS_LISTED_OPT)")


def restore():
    with data_engine.begin() as c:
        c.execute(text("TRUNCATE TABLE ARS_LISTING_WORKING"))
        c.execute(text("INSERT INTO ARS_LISTING_WORKING SELECT * FROM ARS_LISTING_WORKING__BAK_MJG"))
        n = c.execute(text("SELECT COUNT(*) FROM ARS_LISTING_WORKING")).scalar()
        print(f"[cleanup] restored ARS_LISTING_WORKING: {n} rows")


def summarize(label, batch_id):
    with data_engine.connect() as c:
        # Find ship column
        cols = {r[0].upper() for r in c.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='ARS_ALLOC_WORKING'"
        )).fetchall()}
        ship = "SHIP_QTY" if "SHIP_QTY" in cols else ("FNL_Q" if "FNL_Q" in cols else "OPT_SHIP")
        rows = c.execute(text(
            f"SELECT OPT_TYPE, SUM([{ship}]) AS S, COUNT(*) AS N "
            f"FROM ARS_ALLOC_WORKING WHERE WERKS=:w AND MAJ_CAT=:m "
            f"GROUP BY OPT_TYPE ORDER BY OPT_TYPE"
        ), {"w": WERKS, "m": MAJ_CAT}).fetchall()
        print(f"\n=== {label} (batch={batch_id}) — {ship} by OPT_TYPE, HN43+M_TEES_HS ===")
        if not rows:
            print("  (no rows allocated)")
        for r in rows:
            print(f"  {r[0]:<5} ship={r[1] or 0:>6}  rows={r[2]}")


def run(growth):
    print(f"\n##### run mj_req_growth_pct={growth} #####")
    _growth = growth
    res = run_listing_and_allocation_pandas(
        working_table="ARS_LISTING_WORKING",
        listed_table="ARS_LISTED_OPT",
        alloc_table="ARS_ALLOC_WORKING",
        n_workers=2,
        size_threshold=0.6,
        min_size_count=3,
        pri_ct_check_rl=False,
        pri_ct_check_tbc=False,
        rl_mbq_cap_pct=_growth,
        tbc_mbq_cap_pct=_growth,
        tbl_mbq_cap_pct=_growth,
        rl_mj_req_cap_pct=100.0,
        tbc_mj_req_cap_pct=100.0,
        tbl_mj_req_cap_pct=100.0,
        mj_req_growth_pct=_growth,
        opt_types=["RL", "TBC", "TBL"],
        apply_sec_cap_in_normal=True,
    )
    summarize(f"growth={growth}", res.get("batch_id"))


if __name__ == "__main__":
    try:
        backup_once()
        narrow_to_target()
        run(110.0)
        narrow_to_target()  # re-narrow since engine may consume/mutate
        run(100.0)
    finally:
        restore()
