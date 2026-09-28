#!/usr/bin/env python
"""
Step 6 — scoped A/B run: Own vs All RDCs (spec v1.5 Part C, Step 6).

Runs the SAME small scope twice against the real pipeline:

    Run A   rdc_mode='own'   ALC_RDC_CENTRAL_POOL OFF   -> today's behaviour
    Run B   rdc_mode='all'   ALC_RDC_CENTRAL_POOL ON    -> club, allocate, split

then validates every blocking check that needs a live run, and restores the
switch to OFF whichever way the runs go.

Scope is deliberately tiny so the numbers are checkable by hand:
  MAJ_CAT JGW_JKT_SL  — stocked ONLY at DH24 (6,909 pcs), DW01 holds zero.
  2 DW01 stores + 2 DH24 stores.
So the DW01 stores must get nothing in Run A and real supply in Run B. That
single asymmetry is the whole point of the change.

    python scripts/step6_rdc_ab_test.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger                          # noqa: E402
from sqlalchemy import text                        # noqa: E402
from app.database.session import get_data_engine   # noqa: E402

# JB_TEES_HS: DH24 holds 99,324 pcs / DW01 67,298 — and 481 of 605 stocked
# articles sit at exactly ONE warehouse, so clubbing genuinely bites. Stores
# chosen because they demonstrably allocate this MAJ_CAT, which the first
# attempt (JGW_JKT_SL) did not — it listed 0 options and proved nothing.
MAJ_CAT = "JB_TEES_HS"
DW01_STORES = ["HH15", "HX55"]
DH24_STORES = ["HN40", "HN50"]
STORES = DW01_STORES + DH24_STORES
RULE = "ALC_RDC_CENTRAL_POOL"


def set_switch(on: bool) -> None:
    eng = get_data_engine()
    with eng.connect() as c:
        c.execute(text(
            "UPDATE ARS_BUSINESS_RULES SET is_active = :v, updated_by = 'step6_ab_test' "
            "WHERE rule_key = :k"
        ), {"v": 1 if on else 0, "k": RULE})
        c.commit()
    from app.services import business_rules as br
    br._load_all(force=True)          # drop the 30s cache immediately
    state = br.rule_flag(RULE, False)
    print(f"  switch {RULE} -> {'ON' if on else 'OFF'} (reads back {state})")
    if state != on:
        raise RuntimeError("switch did not take effect")


def run_generate(label: str, rdc_mode: str) -> dict:
    """Drive the real /listing/generate body in-process."""
    from app.api.v1.endpoints.listing import GenerateRequest, _generate_listing_impl
    from app.services.listing_sessions import start_session, end_session

    sid = f"AB_{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    req_kwargs = dict(
        rdc_mode=rdc_mode,
        store_codes=STORES,
        maj_cat_values=[MAJ_CAT],
        run_mode="listing",
        alloc_type="FRESH",
        stock_consider_dt=str(date.today()),
        picking_dt=str(date.today()),
        allocation_mode="per_opt",
        parallel_workers=2,
    )
    if rdc_mode == "own":
        # Mirrors the cockpit: rdc_values is auto-derived from the stores.
        req_kwargs["rdc_values"] = ["DW01", "DH24"]
    req = GenerateRequest(**req_kwargs)

    print(f"\n=== RUN {label}: rdc_mode={rdc_mode} session={sid} ===")
    start_session(sid, "step6_ab_test", req.dict())
    summary: dict = {}
    t0 = time.time()
    try:
        _generate_listing_impl(req, current_user=None, session_id=sid,
                               summary=summary, preset_batch_id=sid)
        err = summary.get("error")
    except Exception as e:                       # noqa: BLE001
        err = str(e)
        logger.exception("generate failed")
    el = time.time() - t0
    try:
        end_session(sid, 'FAILED' if err else 'SUCCESS', summary)
    except Exception:
        pass
    print(f"  finished in {el:.0f}s  error={err or 'none'}")
    return {"session_id": sid, "elapsed": el, "error": err, "summary": summary}


def measure(sid: str) -> dict:
    """Read back what the run actually produced."""
    eng = get_data_engine()
    out: dict = {"session_id": sid}
    with eng.connect() as c:
        row = c.execute(text("""
            SELECT COUNT(*),
                   ISNULL(SUM(TRY_CAST(SHIP_QTY AS FLOAT)),0),
                   ISNULL(SUM(TRY_CAST(HOLD_QTY AS FLOAT)),0),
                   COUNT(DISTINCT WERKS)
            FROM ARS_ALLOC_WORKING WHERE MAJ_CAT = :mc
        """), {"mc": MAJ_CAT}).first()
        out.update(alloc_rows=int(row[0] or 0), ship=float(row[1] or 0),
                   hold=float(row[2] or 0), stores=int(row[3] or 0))

        out["by_store"] = {
            r[0]: float(r[1] or 0) for r in c.execute(text("""
                SELECT WERKS, ISNULL(SUM(TRY_CAST(SHIP_QTY AS FLOAT)),0)
                FROM ARS_ALLOC_WORKING WHERE MAJ_CAT = :mc
                GROUP BY WERKS ORDER BY WERKS
            """), {"mc": MAJ_CAT})
        }
        # I-11 / V14 — mode separation
        has_src = c.execute(text(
            "SELECT CASE WHEN COL_LENGTH('ARS_ALLOC_WORKING','SRC_RDC') "
            "IS NULL THEN 0 ELSE 1 END")).scalar()
        out["src_rdc_col_exists"] = bool(has_src)
        out["src_rdc_non_null"] = int(c.execute(text(
            "SELECT COUNT(*) FROM ARS_ALLOC_WORKING WHERE SRC_RDC IS NOT NULL"
        )).scalar() or 0) if has_src else 0
        out["split_rows"] = int(c.execute(text(
            "SELECT COUNT(*) FROM ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID=:s"
        ), {"s": sid}).scalar() or 0)
        out["split_ship"] = float(c.execute(text(
            "SELECT ISNULL(SUM(SHIP_QTY),0) FROM ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID=:s"
        ), {"s": sid}).scalar() or 0)
        out["split_hold"] = float(c.execute(text(
            "SELECT ISNULL(SUM(HOLD_QTY),0) FROM ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID=:s"
        ), {"s": sid}).scalar() or 0)
        out["by_src_rdc"] = {
            r[0]: float(r[1] or 0) for r in c.execute(text("""
                SELECT SRC_RDC, ISNULL(SUM(SHIP_QTY),0) FROM ARS_ALLOC_RDC_SPLIT
                WHERE SESSION_ID=:s GROUP BY SRC_RDC ORDER BY SRC_RDC"""), {"s": sid})
        }
        out["cross_qty"] = float(c.execute(text(
            "SELECT ISNULL(SUM(SHIP_QTY),0) FROM ARS_ALLOC_RDC_SPLIT "
            "WHERE SESSION_ID=:s AND IS_CROSS=1"), {"s": sid}).scalar() or 0)
        out["split_lines"] = int(c.execute(text("""
            SELECT COUNT(*) FROM (
              SELECT WERKS, VAR_ART, SZ FROM ARS_ALLOC_RDC_SPLIT
              WHERE SESSION_ID=:s GROUP BY WERKS, VAR_ART, SZ HAVING COUNT(*)>1) X
        """), {"s": sid}).scalar() or 0)
        out["reduced_lines"] = int(c.execute(text("""
            SELECT COUNT(*) FROM ARS_ALLOC_WORKING
            WHERE ALLOC_REMARKS LIKE '%RDC_SINGLE_SHORT%'
               OR ALLOC_REMARKS LIKE '%RDC_SPLIT_CAPPED%'
               OR ALLOC_REMARKS LIKE '%RDC_HOLD_SHORT%'
        """)).scalar() or 0)
        out["listing_rows"] = int(c.execute(text(
            "SELECT COUNT(*) FROM ARS_LISTING_WORKING")).scalar() or 0)
    return out


def validate(a: dict, b: dict) -> list:
    """Every blocking check that needs a live run."""
    res = []

    def chk(gate, name, ok, detail):
        res.append({"gate": gate, "name": name, "pass": bool(ok), "detail": detail})

    # V14 / I-11 — mode separation
    chk("V14", "Own run writes NO SRC_RDC", a["src_rdc_non_null"] == 0,
        f"{a['src_rdc_non_null']} non-null")
    chk("V14", "Own run writes NO split rows", a["split_rows"] == 0,
        f"{a['split_rows']} rows")
    chk("V14", "All-RDCs run writes SRC_RDC", b["src_rdc_non_null"] > 0,
        f"{b['src_rdc_non_null']} rows stamped")

    # V4 / I-1,I-2 — conservation
    chk("V4", "split SHIP == alloc SHIP", abs(b["split_ship"] - b["ship"]) < 0.5,
        f"split {b['split_ship']:.0f} vs alloc {b['ship']:.0f}")
    chk("V4", "split HOLD == alloc HOLD", abs(b["split_hold"] - b["hold"]) < 0.5,
        f"split {b['split_hold']:.0f} vs alloc {b['hold']:.0f}")

    # V10 — with 2 RDCs and the default policy, nothing may be reduced
    chk("V10", "no reduced lines at N=2", b["reduced_lines"] == 0,
        f"{b['reduced_lines']} reduced")

    # The business outcome: DW01 stores have zero stock for this MAJ_CAT.
    a_dw01 = sum(a["by_store"].get(s, 0) for s in DW01_STORES)
    b_dw01 = sum(b["by_store"].get(s, 0) for s in DW01_STORES)
    chk("BR-RDC-01", "DW01 stores gain supply from the clubbed pool",
        b_dw01 > a_dw01, f"Own {a_dw01:.0f} -> All {b_dw01:.0f}")
    chk("BR-RDC-05", "cross-ship actually occurred", b["cross_qty"] > 0,
        f"{b['cross_qty']:.0f} pcs")

    # No warehouse may be tagged beyond its physical stock (I-3) — checked in SQL
    eng = get_data_engine()
    with eng.connect() as c:
        over = c.execute(text("""
            WITH t AS (
              SELECT SRC_RDC, VAR_ART, SZ,
                     SUM(ISNULL(SHIP_QTY,0)+ISNULL(HOLD_QTY,0)) taken
              FROM ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID=:s
              GROUP BY SRC_RDC, VAR_ART, SZ
            ), s AS (
              SELECT LTRIM(RTRIM(CAST(RDC AS NVARCHAR(50)))) RDC,
                     TRY_CAST(TRY_CAST(ARTICLE_NUMBER AS FLOAT) AS BIGINT) VAR_ART,
                     LTRIM(RTRIM(CAST(SZ AS NVARCHAR(50)))) SZ,
                     SUM(TRY_CAST(FNL_Q AS FLOAT)) stock
              FROM ARS_MSA_VAR_ART WHERE ISNULL(ALLOC_TYPE,'FRESH')='FRESH'
              GROUP BY LTRIM(RTRIM(CAST(RDC AS NVARCHAR(50)))),
                       TRY_CAST(TRY_CAST(ARTICLE_NUMBER AS FLOAT) AS BIGINT),
                       LTRIM(RTRIM(CAST(SZ AS NVARCHAR(50))))
            )
            SELECT COUNT(*) FROM t LEFT JOIN s
              ON s.RDC=t.SRC_RDC AND s.VAR_ART=t.VAR_ART AND s.SZ=t.SZ
            WHERE t.taken > ISNULL(s.stock,0) + 0.001
        """), {"s": b["session_id"]}).scalar() or 0
    chk("V3", "no warehouse over-drawn", over == 0, f"{over} violations")
    return res


def main() -> int:
    logger.remove()
    logger.add(sys.stdout, format="{message}", level="WARNING")
    print(f"scope: MAJ_CAT={MAJ_CAT}  stores={STORES}")
    print(f"        DW01={DW01_STORES}  DH24={DH24_STORES}")

    results = {}
    try:
        print("\n--- Run A: Own, central pool OFF ---")
        set_switch(False)
        ra = run_generate("OWN", "own")
        results["own"] = measure(ra["session_id"])
        results["own"]["error"] = ra["error"]

        print("\n--- Run B: All RDCs, central pool ON ---")
        set_switch(True)
        rb = run_generate("ALL", "all")
        results["all"] = measure(rb["session_id"])
        results["all"]["error"] = rb["error"]
    finally:
        print("\n--- restoring the switch ---")
        try:
            set_switch(False)
        except Exception as e:                   # noqa: BLE001
            print(f"  !! FAILED to restore the switch: {e}")

    if results.get("own") and results.get("all"):
        results["checks"] = validate(results["own"], results["all"])
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "step6_ab_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nraw result -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
