"""
rule_engine_new.py — Stage A (List OPTs) + Stages B/C/D (Allocate VAR_ART × SZ).

Full spec: docs/NEW_RULE_ENGINE_SPEC.md

Entry point:
    run_listing_and_allocation(conn, working_table, listed_table, alloc_table, ...)

This module is self-contained. It does NOT import or call the old
`rule_engine.py` or `listing_allocator.py` — those are kept for reference only.

Feature-flag constants at the top let the user toggle individual rules on/off
without editing allocation SQL.
"""
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import text
from loguru import logger
import json
import time

from app.utils.db_helpers import run_sql, table_exists, get_columns

_run = run_sql
_exists = table_exists
_cols = get_columns

# ───────────────────────────────────────────────────────────────
# FEATURE FLAGS — toggle rules here without touching SQL below.
# Defaults match docs/NEW_RULE_ENGINE_SPEC.md §7.
# ───────────────────────────────────────────────────────────────
RULE_R01_LISTING          = True
RULE_R02_NOT_MIX          = True
RULE_R03_NOT_NL           = False  # NL options handled upstream; disabled
RULE_R04_MSA_POS          = True
RULE_R05_REQ_POS          = True
RULE_R06_PRI_100          = True
RULE_R07_VAR_RATIO_TBL    = True
RULE_R08_MJ_REQ_BOOSTED   = False  # DEPRECATED: merged into unified R09 below (May-2026)
RULE_R09_TBL_TRIVIAL      = True   # Unified headroom check for ALL OPT_TYPEs
                                    # ((cap × MJ_MBQ) − MJ_STK_TTL − ALLOC_QTY) < 0.5 × ACS_D → SKIP

ENABLE_FOCUS_TIERING      = True
ENABLE_STORE_BROKEN       = True   # MJ_REQ_REM < factor × ACS_D → skip store in opt_type
ENABLE_GRID_OVERFLOW      = False
ENABLE_SIZE_COVERAGE_BREAK = False

ENABLE_PER_OPT_REVALIDATION = True   # revalidate after each band (requires BAND_SIZE=1)
ACS_SKIP_FACTOR           = 0.5     # MJ_REQ_REM < factor*ACS_D → skip; H_REM=0 if REQ_REM <= factor*ACS_D

OPT_TYPE_ORDER = ["RL", "TBC", "TBL"]
BAND_SIZE = 1  # rank band width; 1 = strict option-by-option (required for per-OPT revalidation)

POOL_TABLE = "#nre_pool"
_SKIP_ART = {"GEN_ART_NUMBER", "ARTICLE_NUMBER", "GEN_ART", "VAR_ART"}


# ───────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ───────────────────────────────────────────────────────────────
def run_listing_and_allocation(
    conn,
    working_table: str = "ARS_LISTING_WORKING",
    listed_table: str = "ARS_LISTED_OPT",
    alloc_table: str = "ARS_ALLOC_WORKING",
    msa_var_table: str = "ARS_MSA_VAR_ART",
    var_grid_table: str = "ARS_GRID_MJ_VAR_ART",
    cont_table: str = "Master_CONT_SZ",
    size_threshold: float = 0.6,
    min_size_count: int = 3,
    tbl_trivial_factor: float = 0.5,
    pri_ct_check_rl: bool = False,   # apply PRI_CT%>=100 gate to RL? Default False = MBQ-cap mode
    pri_ct_check_tbc: bool = False,  # apply PRI_CT%>=100 gate to TBC? Default False = MBQ-cap mode
    rl_mbq_cap_pct: float = 0.0,     # when pri_ct_check_rl=False, cap RL at X% of MJ_MBQ
    tbc_mbq_cap_pct: float = 0.0,    # when pri_ct_check_tbc=False, cap TBC at X% of MJ_MBQ
    tbl_mbq_cap_pct: float = 0.0,    # cap TBL at X% of MJ_MBQ (0 = disabled)
    # Per-OPT_TYPE downward MJ_REQ caps. Each OPT_TYPE's SUM(SHIP_QTY) per
    # (WERKS, MAJ_CAT) is clamped to cap_pct% × MJ_REQ. 100% = no over-ship
    # vs MAJ_CAT requirement; 0 = cap disabled. Independent ceilings per type.
    rl_mj_req_cap_pct:  float = 100.0,
    tbc_mj_req_cap_pct: float = 100.0,
    tbl_mj_req_cap_pct: float = 100.0,
    # MJ_REQ growth headroom — applied upstream by /listing-build by scaling
    # MJ_REQ on ARS_LISTING_WORKING.  Engine receives the already-scaled value
    # and does not re-scale; this param is informational (audit log only).
    mj_req_growth_pct:  float = 100.0,
    opt_types: Optional[List[str]] = None,  # restrict waterfall to these OPT_TYPEs only (default = all)
    apply_sec_cap_in_normal: bool = True,    # 130% cap on Secondary grids in main pass
) -> Dict:
    """
    Orchestrates Stages A–D. See docs/NEW_RULE_ENGINE_SPEC.md.

    Secondary-grid cap (when apply_sec_cap_in_normal=True): after the main
    waterfall completes and before Stage D, every OPT is re-evaluated by
    `_apply_sec_grid_cap_pre_gate` at OPT grain. An OPT is skipped whole if
    shipping it would push any of its Secondary grids
    (per ARS_GRID_BUILDER.grid_group) over 130% of that grid's MBQ — UNLESS
    OPT_REQ ≥ SEC_CAP_PRE_OVERRIDE_OPT_REQ_PCT × OPT_MBQ, in which case the
    high demand earns the OPT an override.

    TBL is no longer MBQ-capped. Its natural per-size SZ_REQ ceiling
    (Σ SZ_REQ across sizes == MJ_REQ) plus a final downward clamp
    (`tbl_mj_req_cap_pct`, default 100% of MJ_REQ) provide the only
    TBL cap. See backend/app/docs/processes/fallback_archived.md for
    the removed fallback phase and the prior `tbl_mbq_cap_pct` knob.
    """
    t0 = time.time()
    # Audit-log the PRI/MBQ-cap gate state actually received by the engine.
    # Helps diagnose UI-toggle vs. server-state mismatches.
    logger.info(
        f"[rule_engine_new] "
        f"RL: {'PRI>=100 strict' if pri_ct_check_rl else f'MBQ-cap {rl_mbq_cap_pct}%'} | "
        f"TBC: {'PRI>=100 strict' if pri_ct_check_tbc else f'MBQ-cap {tbc_mbq_cap_pct}%'} | "
        f"MJ_REQ growth={mj_req_growth_pct}%"
    )
    result = {
        "listed_opts": 0,
        "dropped_opts": 0,
        "alloc_rows": 0,
        "ship_qty_total": 0.0,
        "hold_qty_total": 0.0,
        "duration_sec": 0.0,
    }

    if not _exists(conn, working_table) or not _exists(conn, msa_var_table):
        logger.warning(
            f"rule_engine_new: missing {working_table} or {msa_var_table} — skipping"
        )
        return result

    # STAGE A — list OPTs
    _stage_a_add_columns(conn, working_table)
    _stage_a_apply_rules(conn, working_table, size_threshold, min_size_count,
                         tbl_trivial_factor,
                         pri_ct_check_rl=pri_ct_check_rl,
                         pri_ct_check_tbc=pri_ct_check_tbc,
                         rl_mbq_cap_pct=rl_mbq_cap_pct,
                         tbc_mbq_cap_pct=tbc_mbq_cap_pct,
                         tbl_mbq_cap_pct=tbl_mbq_cap_pct)
    _stage_a_assign_tier(conn, working_table)
    _stage_a_assign_rank(conn, working_table)
    listed_count = _stage_a_materialize_listed(conn, working_table, listed_table)
    result["listed_opts"] = listed_count
    logger.info(f"[A] listed={listed_count}")

    if listed_count == 0:
        result["duration_sec"] = round(time.time() - t0, 1)
        return result

    # STAGE B — explode to VAR_ART × SZ
    base_rows = _stage_b_explode(conn, listed_table, alloc_table, msa_var_table,
                                  pri_ct_check_rl=pri_ct_check_rl,
                                  pri_ct_check_tbc=pri_ct_check_tbc,
                                  opt_types=opt_types)
    logger.info(f"[B] alloc rows = {base_rows}")
    if base_rows == 0:
        result["duration_sec"] = round(time.time() - t0, 1)
        return result

    _stage_b_fill_cont(conn, alloc_table, cont_table)
    _stage_b_fill_targets(conn, alloc_table, var_grid_table)
    _stage_b_indexes(conn, alloc_table)

    # Ensure per-row reason-tracking columns exist on both tables now that
    # alloc_table has just been re-created by Stage B. _classify_alloc_reason
    # and the rollup in Stage D will populate these.
    _ensure_phase_reason_cols(conn, alloc_table, working_table)

    # Primary-grid map + _REM shadow columns (seeded from originals)
    grids = _discover_primary_grids(conn)
    logger.info(f"[C] primary grids = {list(grids.keys())}")
    if ENABLE_PER_OPT_REVALIDATION:
        _init_rem_columns(conn, working_table, grids)

    # STAGE C — allocate
    _stage_c_build_pool(conn, alloc_table)
    _stage_c_waterfall(conn, alloc_table, working_table, grids,
                        pri_ct_check_rl=pri_ct_check_rl,
                        pri_ct_check_tbc=pri_ct_check_tbc,
                        size_threshold=size_threshold,
                        min_size_count=min_size_count,
                        opt_types=opt_types,
                        rl_cap_pct=rl_mbq_cap_pct,
                        tbc_cap_pct=tbc_mbq_cap_pct)
    # Per-OPT MJ-level cap: each OPT_TYPE's SHIP is capped at
    # cap_pct% × MJ_MBQ − MJ_STK_TTL per (WERKS, MAJ_CAT). Independent ceilings
    # — they do NOT sum against a total MAJ_CAT cap.  TBL has no MJ-cap;
    # its only ceiling is per-size SZ_REQ (Σ == MJ_REQ).
    if rl_mbq_cap_pct > 0:
        _stage_c_apply_mbq_cap(conn, alloc_table, working_table, 'RL', rl_mbq_cap_pct)
    if tbc_mbq_cap_pct > 0:
        _stage_c_apply_mbq_cap(conn, alloc_table, working_table, 'TBC', tbc_mbq_cap_pct)

    # ── Secondary-grid cap (main pass, toggle-controlled) ──────────────
    # Default ON. Reads ALL active grids and PRE-GATES each OPT at its
    # Secondary-grid grains. An OPT either ships in full or is skipped whole;
    # a skip does not close the grid (later, smaller OPTs still get a check).
    if apply_sec_cap_in_normal:
        _all_grids_main = _discover_all_active_grids(conn)
        _apply_sec_grid_cap_pre_gate(
            conn, alloc_table, working_table, _all_grids_main,
            opt_type=None,
        )

    # OPT-grain MJ_REQ gate — full-OPT-or-skip semantics replace the old
    # size-grain trim cap. Per (WERKS, MAJ_CAT) the first OPT (in priority
    # order RL→TBC→TBL, then OPT_PRIORITY_TIER/RANK/ST_RANK) whose
    # `cap_pct × MJ_REQ ≥ 0.5 × OPT_MBQ` ships in full (SHIP and HOLD
    # unchanged from the waterfall). All other OPTs at that (WERKS, MAJ_CAT)
    # get SHIP=HOLD=0 with SKIP_REASON='{OPT}_MJ_REQ_GATE_FAIL' or
    # '{OPT}_MJ_REQ_POST_WINNER'. Designed so the engine ships clean,
    # complete OPTs rather than partial rows.
    _stage_c_apply_opt_mj_req_gate(
        conn, alloc_table, working_table,
        rl_cap_pct=rl_mj_req_cap_pct,
        tbc_cap_pct=tbc_mj_req_cap_pct,
        tbl_cap_pct=tbl_mj_req_cap_pct,
    )

    # Per-row reason classification. Runs BEFORE Stage D so the listing-side
    # rollup sees the final reason values.
    _classify_alloc_reason(conn, alloc_table)
    # STAGE D — reflect back to listing working
    _stage_d_reflect(conn, working_table, alloc_table)

    # Totals
    totals = conn.execute(text(f"""
        SELECT COUNT(*), ISNULL(SUM(SHIP_QTY),0), ISNULL(SUM(HOLD_QTY),0)
        FROM [{alloc_table}]
        WHERE ISNULL(SHIP_QTY,0) > 0 OR ISNULL(HOLD_QTY,0) > 0
    """)).fetchone()
    result["alloc_rows"] = int(totals[0] or 0)
    result["ship_qty_total"] = float(totals[1] or 0)
    result["hold_qty_total"] = float(totals[2] or 0)

    # Capture live #nre_pool.FNL_Q_REM into alloc_table.RDC_FNL_Q_REM_LIVE
    # and append a LIVE_POOL=<n> note to every NO_POOL_MSA remark, so the
    # remark reflects what was actually left in the RDC pool at end-of-run
    # rather than the seeded/restored value on alloc_table.FNL_Q_REM.
    # Must run BEFORE _cleanup drops #nre_pool.
    _snapshot_live_pool_to_alloc(conn, alloc_table)

    _cleanup(conn)
    result["duration_sec"] = round(time.time() - t0, 1)
    logger.info(
        f"rule_engine_new DONE: listed={result['listed_opts']}, "
        f"alloc_rows={result['alloc_rows']}, ship={result['ship_qty_total']:.0f}, "
        f"hold={result['hold_qty_total']:.0f}, {result['duration_sec']}s"
    )
    return result


# ───────────────────────────────────────────────────────────────
# STAGE A — LIST OPTs
# ───────────────────────────────────────────────────────────────
def _stage_a_add_columns(conn, working_table):
    """Add LISTED_FLAG / LISTED_REASON / OPT_PRIORITY_* columns idempotently."""
    cols = {
        "LISTED_FLAG":       "INT NULL",
        "LISTED_REASON":     "NVARCHAR(500) NULL",
        "OPT_PRIORITY_RANK": "INT NULL",
        "OPT_PRIORITY_TIER": "INT NULL",
        "ALLOC_QTY":         "FLOAT NULL",
        "HOLD_QTY":          "FLOAT NULL",
        "ALLOC_STATUS":      "NVARCHAR(50) NULL",
        "ALLOC_REMARKS":     "NVARCHAR(MAX) NULL",
        "ALLOC_SEQ":         "INT NULL",
    }
    existing = {c.upper() for c in _cols(conn, working_table)}
    for col, typedef in cols.items():
        if col.upper() in existing:
            continue
        try:
            _run(conn, f"ALTER TABLE [{working_table}] ADD [{col}] {typedef}")
        except Exception:
            pass

    # Reset status fields (idempotent rerun support)
    _run(conn, f"""
        UPDATE [{working_table}] SET
            LISTED_FLAG=0, LISTED_REASON='',
            OPT_PRIORITY_RANK=NULL, OPT_PRIORITY_TIER=NULL,
            ALLOC_QTY=0, HOLD_QTY=0,
            ALLOC_STATUS='PENDING', ALLOC_REMARKS='', ALLOC_SEQ=NULL
    """)


def _stage_a_apply_rules(conn, working_table, size_threshold, min_size_count,
                          tbl_trivial_factor,
                          pri_ct_check_rl: bool = False,
                          pri_ct_check_tbc: bool = False,
                          rl_mbq_cap_pct: float = 0.0,
                          tbc_mbq_cap_pct: float = 0.0,
                          tbl_mbq_cap_pct: float = 0.0):
    """
    Chain every rule into a reason string. LISTED_FLAG=1 iff the chain is empty.
    Rules are guarded by feature flags so the user can turn any off.

    pri_ct_check_rl / pri_ct_check_tbc:
        Scope the PRI_CT%>=100 gate (R06). TBL always enforces. RL and TBC
        honour the flag — when False, they pass R06 even with PRI_CT% < 100.

    rl_mbq_cap_pct / tbc_mbq_cap_pct (R08):
        When PRI gate is off for RL/TBC and these caps are > 0, compute a
        boosted MJ_REQ = MJ_MBQ × cap/100 − MJ_STK_TTL. If that boosted
        requirement < ACS_SKIP_FACTOR × ACS_D the store's RL/TBC rows are
        skipped — there is no meaningful gap left to fill after the cap lift.
    """
    pieces = []
    if RULE_R01_LISTING:
        pieces.append("CASE WHEN ISNULL(TRY_CAST([LISTING] AS INT),1) <> 1 THEN 'R01_LISTING;' ELSE '' END")
    if RULE_R02_NOT_MIX:
        pieces.append("CASE WHEN ISNULL([OPT_TYPE],'') = 'MIX' THEN 'R02_NOT_MIX;' ELSE '' END")
    # R03 removed: NL options are filtered out upstream (Part 3.6 / OPT_TYPE tagging)
    # R04: block only when BOTH MSA_FNL_Q=0 AND RL_HOLD_QTY=0 (no prior-run hold).
    #      RL_HOLD_QTY is the ARS_NL in-transit qty (separate from HOLD_QTY which is
    #      the current-run allocation hold written by Stage C).
    if RULE_R04_MSA_POS:
        pieces.append(
            "CASE WHEN ISNULL(TRY_CAST([MSA_FNL_Q]    AS FLOAT),0) <= 0 "
            "      AND ISNULL(TRY_CAST([RL_HOLD_QTY]  AS FLOAT),0) <= 0 "
            "      THEN 'R04_MSA_POS;' ELSE '' END"
        )
    if RULE_R05_REQ_POS:
        pieces.append("CASE WHEN ISNULL(TRY_CAST([OPT_REQ_WH] AS FLOAT),0) < 1 THEN 'R05_REQ_POS;' ELSE '' END")
    if RULE_R06_PRI_100:
        # Build the list of opt_types that enforce the PRI_CT gate.
        enforced = ["'TBL'"]  # TBL always enforces
        if pri_ct_check_rl:  enforced.append("'RL'")
        if pri_ct_check_tbc: enforced.append("'TBC'")
        opt_in = ", ".join(enforced)
        pieces.append(
            "CASE WHEN ISNULL(TRY_CAST([PRI_CT%] AS FLOAT),0) < 100 "
            "      AND ISNULL(TRY_CAST([ALLOC_FLAG] AS INT),0) <> 1 "
            f"     AND ISNULL([OPT_TYPE],'') IN ({opt_in}) "
            "      THEN 'R06_PRI_100;' ELSE '' END"
        )
    if RULE_R07_VAR_RATIO_TBL:
        pieces.append(
            f"CASE WHEN ISNULL([OPT_TYPE],'') = 'TBL' "
            f"      AND ISNULL([VAR_COUNT],0) > 0 "
            f"      AND (CAST(ISNULL([VAR_FNL_COUNT],0) AS FLOAT) / NULLIF([VAR_COUNT],0)) < {size_threshold} "
            f"      AND ISNULL([VAR_FNL_COUNT],0) < {min_size_count} "
            f"      THEN 'R07_VAR_RATIO_TBL;' ELSE '' END"
        )
    # R09 — UNIFIED HEADROOM CHECK (replaces old R08 + R09).
    # Skip OPT if:  (cap_pct × MJ_MBQ) − MJ_STK_TTL − ALLOC_QTY_RUNNING
    #               < ACS_SKIP_FACTOR × ACS_D
    # At Stage A (initial), ALLOC_QTY_RUNNING is always 0. The same predicate
    # is re-run by _check_r09_eligibility after each OPT_TYPE waterfall
    # completes; that call passes a non-zero ALLOC_QTY_RUNNING per (WERKS,
    # MAJ_CAT).
    # cap_pct per OPT_TYPE (main pass): RL→rl_mbq_cap_pct, TBC→tbc_mbq_cap_pct.
    # TBL has no MJ-cap (removed 2026-05-16) — its only ceiling is per-size
    # SZ_REQ. TBL therefore uses 1.0 here, which makes the headroom check
    # equivalent to "MJ_MBQ − MJ_STK_TTL ≥ 0.5 × ACS_D".
    if RULE_R09_TBL_TRIVIAL:
        rl_factor  = (rl_mbq_cap_pct  / 100.0) if rl_mbq_cap_pct  > 0 else 1.0
        tbc_factor = (tbc_mbq_cap_pct / 100.0) if tbc_mbq_cap_pct > 0 else 1.0
        tbl_factor = (tbl_mbq_cap_pct / 100.0) if tbl_mbq_cap_pct > 0 else 1.0
        cap_expr = (
            "CASE [OPT_TYPE] "
            f"WHEN 'RL'  THEN {rl_factor} "
            f"WHEN 'TBC' THEN {tbc_factor} "
            f"WHEN 'TBL' THEN {tbl_factor} "
            "ELSE 1.0 END"
        )
        pieces.append(
            f"CASE WHEN ISNULL([OPT_TYPE],'') IN ('RL','TBC','TBL') "
            f"      AND (ISNULL([MJ_MBQ],0) * ({cap_expr}) "
            f"           - ISNULL([MJ_STK_TTL],0)) "
            f"          < {ACS_SKIP_FACTOR} * ISNULL(NULLIF([ACS_D],0), 1) "
            f"     THEN 'R09_HEADROOM_TRIVIAL;' ELSE '' END"
        )

    if not pieces:
        reason_expr = "''"
    else:
        reason_expr = " + ".join(pieces)

    _run(conn, f"""
        UPDATE [{working_table}] SET
            LISTED_REASON = {reason_expr},
            LISTED_FLAG = CASE WHEN LEN({reason_expr}) = 0 THEN 1 ELSE 0 END
    """)

    r = conn.execute(text(f"""
        SELECT SUM(CASE WHEN LISTED_FLAG=1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN LISTED_FLAG=0 THEN 1 ELSE 0 END),
               COUNT(*)
        FROM [{working_table}]
    """)).fetchone()
    logger.info(f"[A] rules applied: listed={r[0]} dropped={r[1]} total={r[2]}")


def _check_r09_eligibility(
    conn,
    working_table: str,
    alloc_table: str,
    completed_opt_type: str,
    next_opt_types: List[str],
    rl_cap_pct: float = 110.0,
    tbc_cap_pct: float = 110.0,
) -> int:
    """R09 re-evaluation after `completed_opt_type` finishes its waterfall pass.

    For each (WERKS, MAJ_CAT) compute:
       headroom = (cap_pct × MJ_MBQ) − MJ_STK_TTL − Σ_ALLOC_QTY(WERKS, MAJ_CAT)
    If headroom < 0.5 × ACS_D, all PENDING rows of the upcoming OPT_TYPEs at
    that store/MAJ_CAT get SKIP_REASON='R09_HEADROOM_TRIVIAL' and
    ALLOC_STATUS='SKIPPED'. LISTED_FLAG on the working table is NOT touched
    (per business rule — Stage A's verdict is sticky).

    Per-OPT cap_pct (rl_cap_pct / tbc_cap_pct) is applied at MJ_MBQ level —
    each OPT_TYPE's headroom = cap_pct% × MJ_MBQ − MJ_STK_TTL − Σ_ALLOC(this
    OPT type). TBL is bounded only by SZ_REQ at size grain, so its R09
    factor here is hardcoded to 1.0 (MJ_MBQ − MJ_STK_TTL only).
    """
    if not next_opt_types:
        return 0
    rl_f  = (rl_cap_pct  / 100.0) if rl_cap_pct  > 0 else 1.0
    tbc_f = (tbc_cap_pct / 100.0) if tbc_cap_pct > 0 else 1.0
    cap_expr = (
        f"CASE A.OPT_TYPE "
        f"WHEN 'RL'  THEN {rl_f} "
        f"WHEN 'TBC' THEN {tbc_f} "
        f"WHEN 'TBL' THEN 1.0 "
        f"ELSE 1.0 END"
    )
    ot_in = ",".join(f"'{ot}'" for ot in next_opt_types)
    # Sum ALLOC_QTY per (WERKS, MAJ_CAT) across everything already shipped.
    _run(conn, f"""
        ;WITH StoreCat AS (
            SELECT WERKS, MAJ_CAT,
                   SUM(CAST(ISNULL(ALLOC_QTY, 0) AS FLOAT)) AS alloc_running
            FROM [{alloc_table}]
            GROUP BY WERKS, MAJ_CAT
        )
        UPDATE A SET
            A.ALLOC_STATUS = 'SKIPPED',
            A.SKIP_REASON  = COALESCE(
                NULLIF(A.SKIP_REASON, ''),
                'R09_HEADROOM_TRIVIAL'
            ),
            A.ALLOC_REMARKS = ISNULL(A.ALLOC_REMARKS, '')
                + ' R09_HEADROOM_TRIVIAL(after='
                + '{completed_opt_type}'
                + ', cap=' + CAST({cap_expr} AS NVARCHAR(20))
                + ', alloc_running=' + CAST(SC.alloc_running AS NVARCHAR(20))
                + ');'
        FROM [{alloc_table}] A
        INNER JOIN StoreCat SC
            ON A.WERKS = SC.WERKS AND A.MAJ_CAT = SC.MAJ_CAT
        INNER JOIN [{working_table}] W
            ON W.WERKS = A.WERKS AND W.MAJ_CAT = A.MAJ_CAT
           AND W.GEN_ART_NUMBER = A.GEN_ART_NUMBER
           AND ISNULL(W.CLR,'') = ISNULL(A.CLR,'')
        WHERE A.OPT_TYPE IN ({ot_in})
          AND A.ALLOC_STATUS IS NULL
          AND (ISNULL(W.MJ_MBQ, 0) * ({cap_expr})
               - ISNULL(W.MJ_STK_TTL, 0)
               - ISNULL(SC.alloc_running, 0))
              < {ACS_SKIP_FACTOR} * ISNULL(NULLIF(W.ACS_D, 0), 1)
    """)
    n = conn.execute(text(f"""
        SELECT COUNT(*) FROM [{alloc_table}]
        WHERE ALLOC_STATUS = 'SKIPPED'
          AND SKIP_REASON LIKE 'R09_HEADROOM_TRIVIAL%'
          AND OPT_TYPE IN ({ot_in})
    """)).scalar() or 0
    if n:
        logger.info(
            f"[R09] after {completed_opt_type}: skipped {n} rows in "
            f"{next_opt_types}"
        )
    return int(n)


def _stage_a_assign_tier(conn, working_table):
    if not ENABLE_FOCUS_TIERING:
        _run(conn, f"UPDATE [{working_table}] SET OPT_PRIORITY_TIER = 3 WHERE LISTED_FLAG = 1")
        return
    _run(conn, f"""
        UPDATE [{working_table}] SET OPT_PRIORITY_TIER =
            CASE WHEN ISNULL(TRY_CAST([FOCUS_WO_CAP] AS INT),0) = 1 THEN 1
                 WHEN ISNULL(TRY_CAST([FOCUS_W_CAP]  AS INT),0) = 1 THEN 2
                 ELSE 3 END
        WHERE LISTED_FLAG = 1
    """)


def _stage_a_assign_rank(conn, working_table):
    """
    Per-(store, opt_type, majcat) rank. Each (WERKS, OPT_TYPE, MAJ_CAT)
    bucket gets its own 1..N priority list. NOT a global rank.

    Within each (WERKS, OPT_TYPE, MAJ_CAT) partition:
        OPT_PRIORITY_TIER (1=focus-uncapped, 2=focus-capped, 3=regular) ASC,
        SEC_CT% DESC,         (higher contribution % first)
        MAX_DAILY_SALE DESC,  (higher sales velocity first)
        OPT_REQ_WH DESC,      (more required first)

    SIZE_RATIO is an eligibility gate (R07 in _stage_a_apply_rules), not a
    ranking key — rows below threshold already have LISTED_FLAG=0 and are
    filtered out of the CTE below.

    Stable tie-breakers: terminal ORDER BY columns ([GEN_ART_NUMBER], [CLR])
    are the within-partition identity (partition fixes WERKS/OPT_TYPE/MAJ_CAT).
    Without them, SQL Server picks an arbitrary winner on rows tying every
    business-rule key, which makes two identical runs produce different
    OPT_PRIORITY_RANKs and therefore different ALLOC_QTYs downstream.
    """
    _run(conn, f"""
        ;WITH R AS (
            SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR,
                   ROW_NUMBER() OVER (
                       PARTITION BY [WERKS], ISNULL([OPT_TYPE],''), [MAJ_CAT]
                       ORDER BY
                         ISNULL([OPT_PRIORITY_TIER], 3)            ASC,
                         ISNULL(TRY_CAST([SEC_CT%] AS FLOAT), 0)   DESC,
                         ISNULL([MAX_DAILY_SALE], 0)               DESC,
                         ISNULL([OPT_REQ_WH], 0)                   DESC,
                         [GEN_ART_NUMBER]                          ASC,
                         ISNULL([CLR], '')                         ASC
                   ) AS rk
            FROM [{working_table}]
            WHERE LISTED_FLAG = 1
        )
        UPDATE W SET W.OPT_PRIORITY_RANK = R.rk
        FROM [{working_table}] W
        INNER JOIN R
            ON W.WERKS=R.WERKS AND W.MAJ_CAT=R.MAJ_CAT
           AND W.GEN_ART_NUMBER=R.GEN_ART_NUMBER
           AND ISNULL(W.CLR,'') = ISNULL(R.CLR,'')
    """)


def _rerank_for_next_opt_type(
    conn,
    alloc_table: str,
    next_ot: str,
    size_threshold: float = 0.6,
    min_size_count: int = 3,
) -> None:
    """Re-rank OPT_PRIORITY_RANK for `next_ot` rows and skip OPTs whose live
    size coverage has dropped below the threshold after prior OPT_TYPEs ran.

    Step 1 — SKIP: any (WERKS, GEN_ART_NUMBER, CLR) where
        live_SIZE_RATIO < size_threshold AND live_VAR_FNL_COUNT < min_size_count
    is marked ALLOC_STATUS='SKIPPED', SKIP_REASON='R07_SIZE_RATIO_LIVE'.
    This is the eligibility gate — SIZE_RATIO is validation only.

    Step 2 — RERANK: surviving rows get fresh OPT_PRIORITY_RANK using the
    same ORDER BY as _stage_a_assign_rank (tier → SEC_CT% → MAX_DAILY_SALE
    → OPT_REQ_WH) partitioned by (WERKS, MAJ_CAT). No identity tie-breakers.

    Called once per OPT_TYPE boundary (after RL → before TBC,
    after TBC → before TBL). Only rows with ALLOC_STATUS IS NULL are affected.
    """
    params = {
        "next_ot": next_ot,
        "size_thr": size_threshold,
        "min_sz":   min_size_count,
    }

    # Step 1: build live SIZE_RATIO per (GEN_ART_NUMBER, CLR, VAR_ART) and skip
    # OPTs that no longer have enough size coverage.
    _run(conn, f"""
        ;WITH PoolState AS (
            SELECT [GEN_ART_NUMBER], [CLR], [VAR_ART],
                   COUNT(*) AS VAR_COUNT_LIVE,
                   SUM(CASE WHEN ISNULL([FNL_Q_REM], 0) > 0 THEN 1 ELSE 0 END) AS VAR_FNL_LIVE
            FROM [{alloc_table}]
            WHERE [OPT_TYPE] = :next_ot
            GROUP BY [GEN_ART_NUMBER], [CLR], [VAR_ART]
        ),
        LowCoverage AS (
            SELECT DISTINCT A.WERKS, A.GEN_ART_NUMBER, A.CLR
            FROM [{alloc_table}] A
            INNER JOIN PoolState P
                ON A.GEN_ART_NUMBER = P.GEN_ART_NUMBER
               AND ISNULL(A.CLR,'') = ISNULL(P.CLR,'')
               AND A.VAR_ART = P.VAR_ART
            WHERE A.[OPT_TYPE] = :next_ot
              AND A.[ALLOC_STATUS] IS NULL
              AND P.VAR_COUNT_LIVE > 0
              AND (CAST(P.VAR_FNL_LIVE AS FLOAT) / P.VAR_COUNT_LIVE) < :size_thr
              AND P.VAR_FNL_LIVE < :min_sz
        )
        UPDATE A SET
            A.[ALLOC_STATUS]  = 'SKIPPED',
            A.[ALLOC_REMARKS] = ISNULL(A.[ALLOC_REMARKS],'')
                + ' R07_SIZE_RATIO_LIVE;'
        FROM [{alloc_table}] A
        INNER JOIN LowCoverage LC
            ON A.WERKS = LC.WERKS
           AND A.GEN_ART_NUMBER = LC.GEN_ART_NUMBER
           AND ISNULL(A.CLR,'') = ISNULL(LC.CLR,'')
        WHERE A.[OPT_TYPE] = :next_ot
          AND A.[ALLOC_STATUS] IS NULL
    """, params=params)

    # Step 2: re-rank survivors. SIZE_RATIO already gated the row set in
    # Step 1 above; ranking uses business keys only, mirroring
    # _stage_a_assign_rank. Partition by (WERKS, MAJ_CAT) — OPT_TYPE is
    # already pinned to :next_ot by the WHERE clause.
    _run(conn, f"""
        ;WITH R AS (
            SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR,
                   ROW_NUMBER() OVER (
                       PARTITION BY [WERKS], [MAJ_CAT]
                       ORDER BY
                         ISNULL([OPT_PRIORITY_TIER], 3)           ASC,
                         ISNULL(TRY_CAST([SEC_CT%] AS FLOAT), 0)  DESC,
                         ISNULL([MAX_DAILY_SALE], 0)              DESC,
                         ISNULL([OPT_REQ_WH], 0)                  DESC,
                         [GEN_ART_NUMBER]                         ASC,
                         ISNULL([CLR], '')                        ASC
                   ) AS rk
            FROM [{alloc_table}]
            WHERE [OPT_TYPE] = :next_ot
              AND [ALLOC_STATUS] IS NULL
        )
        UPDATE A SET A.[OPT_PRIORITY_RANK] = R.rk
        FROM [{alloc_table}] A
        INNER JOIN R
            ON A.WERKS = R.WERKS AND A.MAJ_CAT = R.MAJ_CAT
           AND A.GEN_ART_NUMBER = R.GEN_ART_NUMBER
           AND ISNULL(A.CLR,'') = ISNULL(R.CLR,'')
        WHERE A.[OPT_TYPE] = :next_ot
          AND A.[ALLOC_STATUS] IS NULL
    """, params=params)

    logger.debug(
        f"[C] {next_ot}: live-SIZE_RATIO skip + rerank done "
        f"(thr={size_threshold}, min_sz={min_size_count})"
    )


# Columns always present in the base SELECT of _stage_a_materialize_listed
# and _stage_b_explode — must not be re-emitted by _collect_grid_extra_cols
# or the resulting SELECT will fail with "duplicate column" errors.
_BASE_SELECT_COLS = frozenset({
    "WERKS", "RDC", "MAJ_CAT", "GEN_ART_NUMBER", "CLR",
})


def _collect_grid_extra_cols(conn, source_table: str) -> List[str]:
    """Discover MP-resolved grid-extra columns that must propagate
    listing → listed → alloc so the Secondary-grid PRE-GATE cap and
    fallback re-evaluation can join on the right grain.

    Source of truth: ARS_GRID_BUILDER. Returns the union of `extras`
    across every active grid (Primary + Secondary), filtered to columns
    that actually exist on `source_table` AND aren't already part of the
    base SELECT (WERKS / RDC / MAJ_CAT / GEN_ART_NUMBER / CLR). Adding a
    new grid in GRID_BUILDER automatically flows its hierarchy column
    through Stages A → B without code changes. Deduplicated,
    order-preserving.

    Without this propagation, _apply_sec_grid_cap_pre_gate silently drops
    grids whose `extras` aren't present on alloc_table (net effect: the
    apply_sec_cap_in_normal toggle becomes a no-op).
    """
    try:
        all_grids = _discover_all_active_grids(conn)
    except Exception as e:
        logger.warning(f"_collect_grid_extra_cols: grid discovery failed ({e})")
        return []
    src_cols = {c.upper() for c in _cols(conn, source_table)}
    seen: set = set()
    out: List[str] = []
    for _g_name, meta in all_grids.items():
        for extra in (meta.get("extras") or []):
            e_up = str(extra).upper()
            if e_up in seen or e_up in _BASE_SELECT_COLS:
                continue
            seen.add(e_up)
            if e_up in src_cols:
                out.append(e_up)
    return out


def _stage_a_materialize_listed(conn, working_table, listed_table) -> int:
    _run(conn, f"IF OBJECT_ID('{listed_table}','U') IS NOT NULL DROP TABLE [{listed_table}]")
    # MP-resolved grid-extra columns (derived dynamically from
    # ARS_GRID_BUILDER) must flow through listed → alloc so the
    # Secondary-grid cap can evaluate every active grid. Without this
    # propagation the filter in _apply_sec_grid_cap_pre_gate silently
    # drops grids whose extras are missing from alloc_table.
    mp_cols = _collect_grid_extra_cols(conn, working_table)
    mp_sel = ", ".join(f"[{c}]" for c in mp_cols)
    mp_sel = (mp_sel + ",") if mp_sel else ""
    _run(conn, f"""
        SELECT
            WERKS, RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, GEN_ART_DESC,
            OPT_TYPE, ISNULL(IS_NEW,0) AS IS_NEW, ISNULL(I_ROD,1) AS I_ROD,
            ISNULL(OPT_MBQ,0) AS OPT_MBQ, ISNULL(OPT_REQ,0) AS OPT_REQ,
            ISNULL(OPT_MBQ_WH, OPT_MBQ) AS OPT_MBQ_WH,
            ISNULL(OPT_REQ_WH, OPT_REQ) AS OPT_REQ_WH,
            ISNULL(MSA_FNL_Q,0) AS MSA_FNL_Q,
            ISNULL(VAR_COUNT,0) AS VAR_COUNT,
            ISNULL(VAR_FNL_COUNT,0) AS VAR_FNL_COUNT,
            ISNULL(STK_TTL,0) AS STK_TTL,
            ISNULL(ACS_D,0) AS ACS_D, ISNULL(AGE,0) AS AGE,
            ISNULL(MAX_DAILY_SALE,0) AS MAX_DAILY_SALE,
            ISNULL(MJ_REQ,0) AS MJ_REQ,
            {mp_sel}
            LISTING, [PRI_CT%], [SEC_CT%], ALLOC_FLAG,
            FOCUS_W_CAP, FOCUS_WO_CAP,
            ST_RANK, OPT_PRIORITY_RANK, OPT_PRIORITY_TIER,
            LISTED_FLAG, LISTED_REASON, ALLOC_SEQ
        INTO [{listed_table}]
        FROM [{working_table}]
        WHERE LISTED_FLAG = 1
    """)
    cnt = conn.execute(text(f"SELECT COUNT(*) FROM [{listed_table}]")).scalar()
    return int(cnt or 0)


# ───────────────────────────────────────────────────────────────
# STAGE B — EXPLODE TO VAR_ART × SZ
# ───────────────────────────────────────────────────────────────
def _stage_b_explode(conn, listed_table, alloc_table, msa_var_table,
                     pri_ct_check_rl: bool = True,
                     pri_ct_check_tbc: bool = True,
                     opt_types: Optional[List[str]] = None) -> int:
    # Build the OPT_TYPE list that must enforce PRI_CT%=100 — mirrors R06.
    enforced = ["'TBL'"]
    if pri_ct_check_rl:  enforced.append("'RL'")
    if pri_ct_check_tbc: enforced.append("'TBC'")
    enforced_in = ", ".join(enforced)

    # OPT_TYPE subset filter — only explode selected types when running a subset.
    ot_filter = ""
    if opt_types:
        ot_in = ", ".join(f"'{ot}'" for ot in opt_types)
        ot_filter = f"AND ISNULL(L.[OPT_TYPE],'') IN ({ot_in})"

    # Propagate MP-resolved grid-extra columns from listed → alloc so the
    # Secondary-grid cap (PRE-GATE and post-trim) and fallback re-evaluation
    # can join on the right grain. Source: ARS_GRID_BUILDER via
    # _collect_grid_extra_cols (dynamic, not a hardcoded list).
    mp_cols = _collect_grid_extra_cols(conn, listed_table)
    mp_sel = ", ".join(f"L.[{c}]" for c in mp_cols)
    mp_sel = (mp_sel + ",") if mp_sel else ""

    _run(conn, f"IF OBJECT_ID('{alloc_table}','U') IS NOT NULL DROP TABLE [{alloc_table}]")
    _run(conn, f"""
        SELECT
            L.WERKS, L.RDC, L.MAJ_CAT, L.GEN_ART_NUMBER, L.CLR, L.GEN_ART_DESC,
            V.[ARTICLE_NUMBER] AS VAR_ART,
            V.[ARTICLE_DESC]   AS VAR_DESC,
            V.[SZ], V.[MRP], V.[PAK_SZ],

            L.OPT_TYPE, L.IS_NEW, L.I_ROD,
            L.OPT_PRIORITY_RANK, L.OPT_PRIORITY_TIER, L.ST_RANK,
            L.OPT_MBQ, L.OPT_MBQ_WH, L.OPT_REQ, L.OPT_REQ_WH,
            L.MAX_DAILY_SALE, L.ALLOC_FLAG,
            L.[PRI_CT%], L.[SEC_CT%],
            {mp_sel}

            TRY_CAST(V.[FNL_Q] AS FLOAT) AS FNL_Q,
            TRY_CAST(V.[FNL_Q] AS FLOAT) AS FNL_Q_REM,
            CAST(NULL AS FLOAT) AS CONT,
            CAST(NULL AS FLOAT) AS SZ_MBQ,
            CAST(NULL AS FLOAT) AS SZ_MBQ_WH,
            CAST(0 AS FLOAT)    AS SZ_STK,
            CAST(NULL AS FLOAT) AS SZ_REQ,
            CAST(NULL AS FLOAT) AS SZ_REQ_WH,

            CAST(0 AS FLOAT) AS POOL_CONSUMED,
            CAST(0 AS FLOAT) AS SHIP_QTY,
            CAST(0 AS FLOAT) AS HOLD_QTY,
            CAST(0 AS FLOAT) AS ALLOC_QTY,
            CAST(0 AS FLOAT) AS ROUND_SHIP,
            CAST(0 AS FLOAT) AS ROUND_HOLD,
            CAST(NULL AS NVARCHAR(20))  AS ALLOC_WAVE,
            CAST(0 AS INT)              AS ALLOC_ROUND,
            CAST('PENDING' AS NVARCHAR(50)) AS ALLOC_STATUS,
            CAST(NULL AS NVARCHAR(500)) AS SKIP_REASON,
            CAST(NULL AS INT)           AS ALLOC_SEQ
        INTO [{alloc_table}]
        FROM [{listed_table}] L
        INNER JOIN [{msa_var_table}] V WITH (NOLOCK)
            ON  LTRIM(RTRIM(CAST(L.MAJ_CAT AS NVARCHAR(200))))
               = LTRIM(RTRIM(CAST(V.[MAJ_CAT] AS NVARCHAR(200))))
            AND TRY_CAST(L.GEN_ART_NUMBER AS BIGINT)
               = TRY_CAST(TRY_CAST(V.[GEN_ART_NUMBER] AS FLOAT) AS BIGINT)
            AND LTRIM(RTRIM(CAST(L.CLR AS NVARCHAR(200))))
               = LTRIM(RTRIM(CAST(V.[CLR] AS NVARCHAR(200))))
            AND LTRIM(RTRIM(CAST(L.RDC AS NVARCHAR(50))))
               = LTRIM(RTRIM(CAST(V.[RDC] AS NVARCHAR(50))))
        WHERE TRY_CAST(V.[FNL_Q] AS FLOAT) > 0
          -- PRI_CT%=100 gate mirrors R06: TBL always enforces; RL/TBC only when
          -- their pri_ct_check flag is True.  Rows whose OPT_TYPE is not in the
          -- enforced list pass through regardless of PRI_CT%.
          AND (    ISNULL(L.[OPT_TYPE],'') NOT IN ({enforced_in})
               OR  ISNULL(TRY_CAST(L.[PRI_CT%] AS FLOAT), 0) = 100
              )
          -- Meaningful MAJ_CAT requirement gate: prevents ineligible stores from
          -- consuming RDC pool in Stage C and being zeroed post-waterfall.
          -- Inclusive boundary: an OPT with MJ_REQ exactly equal to 0.5×ACS_D
          -- is eligible (matches the documented "list if MJ_REQ ≥ 0.5×ACS_D"
          -- rule; the strict `>` form was excluding borderline OPTs).
          AND ISNULL(TRY_CAST(L.[MJ_REQ] AS FLOAT), 0)
              >= 0.5 * ISNULL(NULLIF(TRY_CAST(L.[ACS_D] AS FLOAT), 0), 18.0)
          {ot_filter}
    """)
    cnt = conn.execute(text(f"SELECT COUNT(*) FROM [{alloc_table}]")).scalar()
    return int(cnt or 0)


def _stage_b_fill_cont(conn, alloc_table, cont_table):
    if _exists(conn, cont_table):
        _run(conn, f"""
            UPDATE A SET A.CONT = TRY_CAST(M.CONT AS FLOAT)
            FROM [{alloc_table}] A
            INNER JOIN [{cont_table}] M WITH (NOLOCK)
                ON LTRIM(RTRIM(CAST(M.ST_CD   AS NVARCHAR(50))))  = LTRIM(RTRIM(CAST(A.WERKS AS NVARCHAR(50))))
               AND LTRIM(RTRIM(CAST(M.MAJ_CAT AS NVARCHAR(200)))) = A.MAJ_CAT
               AND LTRIM(RTRIM(CAST(M.SZ      AS NVARCHAR(200)))) = LTRIM(RTRIM(CAST(A.SZ AS NVARCHAR(200))))
        """)
        _run(conn, f"""
            UPDATE A SET A.CONT = TRY_CAST(M.CONT AS FLOAT)
            FROM [{alloc_table}] A
            INNER JOIN [{cont_table}] M WITH (NOLOCK)
                ON LTRIM(RTRIM(CAST(M.ST_CD AS NVARCHAR(50)))) = 'CO'
               AND LTRIM(RTRIM(CAST(M.MAJ_CAT AS NVARCHAR(200)))) = A.MAJ_CAT
               AND LTRIM(RTRIM(CAST(M.SZ AS NVARCHAR(200)))) = LTRIM(RTRIM(CAST(A.SZ AS NVARCHAR(200))))
            WHERE A.CONT IS NULL
        """)
    # Uniform fallback
    _run(conn, f"""
        ;WITH SzCount AS (
            SELECT WERKS, MAJ_CAT, COUNT(DISTINCT SZ) AS sz_cnt
            FROM [{alloc_table}] GROUP BY WERKS, MAJ_CAT
        )
        UPDATE A SET A.CONT = ROUND(1.0 / NULLIF(C.sz_cnt, 0), 4)
        FROM [{alloc_table}] A
        INNER JOIN SzCount C ON A.WERKS = C.WERKS AND A.MAJ_CAT = C.MAJ_CAT
        WHERE ISNULL(A.CONT, 0) = 0
    """)


def _stage_b_fill_targets(conn, alloc_table, var_grid_table):
    # Optional: pull per-variant-size stock from variant grid, if present.
    if _exists(conn, var_grid_table):
        gcols = {c.upper() for c in _cols(conn, var_grid_table)}
        if {"STK_TTL", "WERKS", "MAJ_CAT"}.issubset(gcols):
            # Best-effort join — silently skip if variant key column isn't obvious.
            var_col = next((c for c in ("VAR_ART", "ARTICLE_NUMBER", "GEN_ART") if c in gcols), None)
            if var_col:
                _run(conn, f"""
                    UPDATE A SET A.SZ_STK = TRY_CAST(G.STK_TTL AS FLOAT)
                    FROM [{alloc_table}] A
                    INNER JOIN [{var_grid_table}] G WITH (NOLOCK)
                        ON G.WERKS = A.WERKS
                       AND G.MAJ_CAT = A.MAJ_CAT
                       AND TRY_CAST(G.[{var_col}] AS BIGINT) = TRY_CAST(A.VAR_ART AS BIGINT)
                """)

    _run(conn, f"""
        UPDATE [{alloc_table}] SET
            SZ_MBQ    = CASE
                WHEN ISNULL(CONT, 0) > 0
                     AND ISNULL(OPT_MBQ, 0) > 0
                     AND ROUND(ISNULL(OPT_MBQ, 0) * ISNULL(CONT, 0), 0) = 0
                    THEN 1
                ELSE ROUND(ISNULL(OPT_MBQ, 0) * ISNULL(CONT, 0), 0)
            END,
            SZ_MBQ_WH = CASE
                WHEN ISNULL(CONT, 0) > 0
                     AND ISNULL(OPT_MBQ_WH, 0) > 0
                     AND ROUND(ISNULL(OPT_MBQ_WH, 0) * ISNULL(CONT, 0), 0) = 0
                    THEN 1
                ELSE ROUND(ISNULL(OPT_MBQ_WH, 0) * ISNULL(CONT, 0), 0)
            END,
            SZ_STK    = ISNULL(SZ_STK, 0)
    """)
    _run(conn, f"""
        UPDATE [{alloc_table}] SET
            SZ_REQ    = CASE WHEN SZ_MBQ    - ISNULL(SZ_STK,0) > 0 THEN SZ_MBQ    - ISNULL(SZ_STK,0) ELSE 0 END,
            SZ_REQ_WH = CASE WHEN SZ_MBQ_WH - ISNULL(SZ_STK,0) > 0 THEN SZ_MBQ_WH - ISNULL(SZ_STK,0) ELSE 0 END
    """)


def _stage_b_indexes(conn, alloc_table):
    try:
        _run(conn, f"""
            CREATE CLUSTERED INDEX CIX_{alloc_table}_walk ON [{alloc_table}]
              (OPT_TYPE, OPT_PRIORITY_RANK, WERKS, MAJ_CAT,
               GEN_ART_NUMBER, CLR, VAR_ART, SZ)
        """)
    except Exception:
        pass
    try:
        _run(conn, f"""
            CREATE NONCLUSTERED INDEX IX_{alloc_table}_pool ON [{alloc_table}]
              (RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ)
              INCLUDE (WERKS, SHIP_QTY, HOLD_QTY, FNL_Q_REM)
        """)
    except Exception:
        pass
    # Slim MAJ_CAT-leading index — keeps seed_queue's GROUP BY MAJ_CAT (and
    # any per-MAJ_CAT lookups) on a narrow stream-aggregate scan instead of
    # walking the much wider clustered/pool indexes.
    try:
        _run(conn, f"""
            CREATE NONCLUSTERED INDEX IX_{alloc_table}_majcat ON [{alloc_table}]
              (MAJ_CAT)
        """)
    except Exception:
        pass


# ───────────────────────────────────────────────────────────────
# PRIMARY-GRID DISCOVERY + REM SHADOW COLUMNS
# ───────────────────────────────────────────────────────────────
def _discover_primary_grids(conn) -> Dict[str, Dict]:
    """
    Returns: {REQ_COL: {hier, gh_col, h_col, h_rem, req_rem, extras}}
      extras = hier columns excluding WERKS and MAJ_CAT (the grid's inner grain)

    Always includes MJ_REQ (primary grid at WERKS×MAJ_CAT grain).
    Reads ARS_GRID_BUILDER for additional ACTIVE Primary grids.
    """
    out: Dict[str, Dict] = {
        "MJ_REQ": {
            "hier": ["MAJ_CAT"],
            "gh_col": "GH_MJ",
            "h_col":  "H_MJ",
            "h_rem":  "H_MJ_REM",
            "req_rem": "MJ_REQ_REM",
            "extras": [],
        }
    }
    if not _exists(conn, "ARS_GRID_BUILDER"):
        return out
    try:
        rows = conn.execute(text(
            "SELECT grid_name, hierarchy_columns, ISNULL(grid_group,'Primary') "
            "FROM [ARS_GRID_BUILDER] WHERE UPPER(status) = 'ACTIVE' "
            "ORDER BY grid_name"
        )).fetchall()
    except Exception as e:
        logger.warning(f"_discover_primary_grids: {e}")
        return out
    for grid_name, hier_json, grid_group in rows:
        if str(grid_group).strip().lower() != "primary":
            continue
        try:
            hier = json.loads(hier_json) if isinstance(hier_json, str) else hier_json
        except Exception:
            continue
        if not hier or any(str(x).upper() in _SKIP_ART for x in hier):
            continue
        hier_u = [str(h).upper() for h in hier]
        last = hier_u[-1]
        if last in ("WERKS", "MAJ_CAT"):
            continue  # covered by MJ
        extras = [h for h in hier_u if h not in ("WERKS", "MAJ_CAT")]
        out[f"{last}_REQ"] = {
            "hier": hier_u,
            "gh_col": f"GH_{last}",
            "h_col":  f"H_{last}",
            "h_rem":  f"H_{last}_REM",
            "req_rem": f"{last}_REQ_REM",
            "extras": extras,
        }
    return out


def _init_rem_columns(conn, working_table, grids: Dict[str, Dict]):
    """
    Create / seed _REM shadow columns on working_table:
      MSA_FNL_Q_REM, PRI_CT_REM, <grid>_REQ_REM, H_<grid>_REM
    Seeds from the originals so each run starts fresh.
    """
    cols = {c.upper() for c in _cols(conn, working_table)}

    def _ensure(col, typedef):
        if col.upper() not in cols:
            try:
                _run(conn, f"ALTER TABLE [{working_table}] ADD [{col}] {typedef}")
            except Exception:
                pass

    _ensure("MSA_FNL_Q_REM", "FLOAT NULL")
    _ensure("PRI_CT_REM",    "FLOAT NULL")

    _run(conn, f"""
        UPDATE [{working_table}] SET
            MSA_FNL_Q_REM = TRY_CAST(MSA_FNL_Q AS FLOAT),
            PRI_CT_REM    = TRY_CAST([PRI_CT%] AS FLOAT)
    """)

    # Re-read cols after potential adds
    cols = {c.upper() for c in _cols(conn, working_table)}

    for req_col, meta in grids.items():
        req_rem = meta["req_rem"]
        h_col   = meta["h_col"]
        h_rem   = meta["h_rem"]

        if req_col.upper() in cols:
            if req_rem.upper() not in cols:
                try:
                    _run(conn, f"ALTER TABLE [{working_table}] ADD [{req_rem}] FLOAT NULL")
                except Exception:
                    pass
            _run(conn, f"UPDATE [{working_table}] SET [{req_rem}] = TRY_CAST([{req_col}] AS FLOAT)")

        if h_col.upper() in cols:
            if h_rem.upper() not in cols:
                try:
                    _run(conn, f"ALTER TABLE [{working_table}] ADD [{h_rem}] INT NULL")
                except Exception:
                    pass
            _run(conn, f"UPDATE [{working_table}] SET [{h_rem}] = TRY_CAST([{h_col}] AS INT)")


def _revalidate_after_band(conn, working_table, alloc_table, opt_type,
                            band_start, band_end, grids: Dict[str, Dict],
                            pri_ct_check_rl: bool = True,
                            pri_ct_check_tbc: bool = True,
                            maj_cat: Optional[str] = None):
    """
    After one band allocates (BAND_SIZE=1 → one rank/OPT):
      1) Reduce MSA_FNL_Q_REM per OPT by ROUND_SHIP + ROUND_HOLD.
      2) Reduce each <grid>_REQ_REM at the grid's grain by ROUND_SHIP
         (joined via working_table to pick up extras like RNG_SEG/MACRO_MVGR).
      3) Recompute H_<grid>_REM = 1 iff REQ_REM > ACS_SKIP_FACTOR*ACS_D AND GH = 1.
      4) Recompute PRI_CT_REM = Σ(H_REM) / Σ(GH) * 100.
      5) Skip rules (apply to rows with OPT_PRIORITY_RANK > band_end):
         - MSA_FNL_Q_REM <= 0                  → SKIPPED (SKIP_MSA_EXHAUSTED)
         - PRI_CT_REM   < 100                  → SKIPPED (SKIP_PRI_BROKEN)
         - MJ_REQ_REM   < factor*ACS_D  (store skip within opt_type, if enabled)
      6) Push skip status to alloc_table so future bands exclude them.

    maj_cat: when set, every alloc_table / working_table touch is also
    filtered by `MAJ_CAT = :mc`. Used by the parallel orchestrator so that
    workers operating on different MAJ_CATs don't interfere.
    """
    params = {
        "ot": opt_type, "bs": band_start, "be": band_end,
        # next-band window: skip rules only check the immediate next band so
        # that further-out OPTs are re-evaluated at their own turn. Prevents
        # mass-marking distant ranks SKIPPED prematurely while still pruning
        # the very next consumer before it enters the pool waterfall.
        "be_p1":   band_end + 1,
        "be_next": band_end + BAND_SIZE,
    }
    mc_pred_alloc = ""
    mc_pred_work  = ""
    if maj_cat is not None:
        params["mc"] = maj_cat
        mc_pred_alloc = " AND MAJ_CAT = :mc"
        mc_pred_work  = " AND MAJ_CAT = :mc"

    # Early-exit: if this band allocated nothing, no _REM values changed
    # (so no skip rules can newly trigger) — skip the 9 revalidation UPDATEs.
    # This cuts the common case where pool is already exhausted for the OPT.
    band_take = conn.execute(text(f"""
        SELECT ISNULL(SUM(ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0)), 0)
        FROM [{alloc_table}]
        WHERE OPT_TYPE = :ot
          AND OPT_PRIORITY_RANK BETWEEN :bs AND :be
          {mc_pred_alloc}
    """), params).scalar()
    if float(band_take or 0) <= 0:
        return

    # Scope: only the MAJ_CATs touched by this band need H_REM / PRI_CT_REM /
    # skip-rule recomputation. For BAND_SIZE=1 this is usually one MAJ_CAT.
    # Scoping turns full-table UPDATEs (10k+ rows) into narrow ones (<1k).
    if maj_cat is not None:
        # Worker-scoped: touched is always exactly [maj_cat] (or empty if the
        # band allocated nothing — already handled by the early-exit above).
        touched = [maj_cat]
    else:
        mc_rows = conn.execute(text(f"""
            SELECT DISTINCT MAJ_CAT FROM [{alloc_table}]
            WHERE OPT_TYPE = :ot
              AND OPT_PRIORITY_RANK BETWEEN :bs AND :be
              AND ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0) > 0
        """), params).fetchall()
        touched = [r[0] for r in mc_rows if r[0] is not None]
    if not touched:
        return
    mc_keys = {f"mc_{i}": mc for i, mc in enumerate(touched)}
    mc_in = ", ".join(f":mc_{i}" for i in range(len(touched)))
    params_mc = {**params, **mc_keys}

    work_cols = {c.upper() for c in _cols(conn, working_table)}
    alloc_cols = {c.upper() for c in _cols(conn, alloc_table)}

    # (1) Reduce MSA_FNL_Q_REM per OPT — this band's SHIP+HOLD.
    # Same UPDATE also appends an OPT-grain audit line to ALLOC_REMARKS
    # showing ship/hold and MSA before→after, so the reviewer sees the
    # full lifecycle of an OPT on its working-table row (Option A audit).
    # Round is read from MAX(ALLOC_ROUND) in the alloc rows of this band —
    # _stage_c_run_band stamps ALLOC_ROUND=r on every row it touches, so
    # this is always the round we're revalidating.
    _run(conn, f"""
        ;WITH OptTake AS (
            SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR,
                   SUM(ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0)) AS take_total,
                   SUM(ISNULL(ROUND_SHIP,0))                        AS take_ship,
                   SUM(ISNULL(ROUND_HOLD,0))                        AS take_hold,
                   MAX(ISNULL(OPT_PRIORITY_RANK,0))                 AS opt_rk,
                   MAX(ISNULL(ALLOC_ROUND,0))                       AS opt_round
            FROM [{alloc_table}]
            WHERE OPT_TYPE = :ot
              AND OPT_PRIORITY_RANK BETWEEN :bs AND :be
              {mc_pred_alloc}
            GROUP BY WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR
            HAVING SUM(ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0)) > 0
        )
        UPDATE W SET
            W.MSA_FNL_Q_REM = CASE
                WHEN ISNULL(W.MSA_FNL_Q_REM, 0) - O.take_total < 0 THEN 0
                ELSE ISNULL(W.MSA_FNL_Q_REM, 0) - O.take_total END,
            W.ALLOC_REMARKS = ISNULL(W.ALLOC_REMARKS, '')
                + ' B[' + :ot + '.r' + CAST(O.opt_round AS NVARCHAR(10))
                + '.rk' + CAST(O.opt_rk AS NVARCHAR(10))
                + '] ship=' + CAST(CAST(O.take_ship AS INT) AS NVARCHAR(20))
                + ' hold=' + CAST(CAST(O.take_hold AS INT) AS NVARCHAR(20))
                + ' msa='  + CAST(CAST(ISNULL(W.MSA_FNL_Q_REM, 0) AS INT) AS NVARCHAR(20))
                + '->'     + CAST(CAST(CASE
                                  WHEN ISNULL(W.MSA_FNL_Q_REM, 0) - O.take_total < 0 THEN 0
                                  ELSE ISNULL(W.MSA_FNL_Q_REM, 0) - O.take_total END
                                AS INT) AS NVARCHAR(20))
                + ';'
        FROM [{working_table}] W
        INNER JOIN OptTake O
            ON W.WERKS=O.WERKS AND W.MAJ_CAT=O.MAJ_CAT
           AND W.GEN_ART_NUMBER=O.GEN_ART_NUMBER
           AND ISNULL(W.CLR,'') = ISNULL(O.CLR,'')
    """, params)

    # (2) Reduce each primary grid's _REQ_REM at its grain
    for req_col, meta in grids.items():
        req_rem = meta["req_rem"]
        extras  = meta["extras"]
        if req_rem.upper() not in work_cols:
            continue
        # Grid grain = (WERKS, MAJ_CAT, *extras) — all must exist on working_table
        if not all(e.upper() in work_cols for e in extras):
            continue
        grid_keys = ["WERKS", "MAJ_CAT"] + extras
        key_sql  = ", ".join(f"W2.[{k}]" for k in grid_keys)
        group_by = ", ".join(f"W2.[{k}]" for k in grid_keys)
        join_cond = " AND ".join(
            f"ISNULL(CAST(W.[{k}] AS NVARCHAR(200)),'') = ISNULL(CAST(G.[{k}] AS NVARCHAR(200)),'')"
            for k in grid_keys
        )
        _run(conn, f"""
            ;WITH GridTake AS (
                SELECT {key_sql},
                       SUM(ISNULL(A.ROUND_SHIP,0)) AS grid_take
                FROM [{alloc_table}] A
                INNER JOIN [{working_table}] W2
                    ON A.WERKS=W2.WERKS AND A.MAJ_CAT=W2.MAJ_CAT
                   AND A.GEN_ART_NUMBER=W2.GEN_ART_NUMBER
                   AND ISNULL(A.CLR,'') = ISNULL(W2.CLR,'')
                WHERE A.OPT_TYPE = :ot
                  AND A.OPT_PRIORITY_RANK BETWEEN :bs AND :be
                  {('AND A.MAJ_CAT = :mc' if maj_cat is not None else '')}
                GROUP BY {group_by}
                HAVING SUM(ISNULL(A.ROUND_SHIP,0)) > 0
            )
            UPDATE W SET
                W.[{req_rem}] = CASE
                    WHEN ISNULL(W.[{req_rem}], 0) - G.grid_take < 0 THEN 0
                    ELSE ISNULL(W.[{req_rem}], 0) - G.grid_take END
            FROM [{working_table}] W
            INNER JOIN GridTake G ON {join_cond}
        """, params)

    # (3) Recompute H_<grid>_REM = (REQ_REM > ACS_SKIP_FACTOR*ACS_D) AND (GH=1)
    h_rem_sets = []
    for req_col, meta in grids.items():
        req_rem = meta["req_rem"]
        gh_col  = meta["gh_col"]
        h_rem   = meta["h_rem"]
        if h_rem.upper() not in work_cols or req_rem.upper() not in work_cols:
            continue
        if gh_col.upper() not in work_cols:
            continue
        h_rem_sets.append(
            f"[{h_rem}] = CASE "
            f"WHEN ISNULL([{req_rem}],0) > {ACS_SKIP_FACTOR} * ISNULL(ACS_D,0) "
            f"AND ISNULL([{gh_col}],0) = 1 THEN 1 ELSE 0 END"
        )
    if h_rem_sets:
        # Scope to touched MAJ_CATs (a grid-grain REQ_REM never crosses
        # MAJ_CAT, so H_REM only needs recompute for those rows).
        _run(conn, f"""
            UPDATE [{working_table}] WITH (ROWLOCK, UPDLOCK) SET {', '.join(h_rem_sets)}
            WHERE MAJ_CAT IN ({mc_in})
        """, params_mc)

    # (4) Recompute PRI_CT_REM = Σ(H_REM) / Σ(GH) × 100 — scoped too.
    pri_h = [meta["h_rem"] for meta in grids.values() if meta["h_rem"].upper() in work_cols]
    pri_gh = [meta["gh_col"] for meta in grids.values() if meta["gh_col"].upper() in work_cols]
    if pri_h and pri_gh:
        h_sum  = " + ".join(f"ISNULL([{c}],0)" for c in pri_h)
        gh_sum = " + ".join(f"ISNULL([{c}],0)" for c in pri_gh)
        _run(conn, f"""
            UPDATE [{working_table}] WITH (ROWLOCK, UPDLOCK) SET
                PRI_CT_REM = CASE
                    WHEN ({gh_sum}) = 0 THEN 0
                    ELSE ROUND(CAST(({h_sum}) AS FLOAT) / ({gh_sum}) * 100, 1) END
            WHERE MAJ_CAT IN ({mc_in})
        """, params_mc)

    # (5) Skip-rules — applied only to the NEXT BAND (not all remaining ranks).
    #   Each OPT is evaluated at its own turn instead of being marked SKIPPED
    #   many ranks ahead (next-band-only = less premature elimination).
    #   REM values are embedded in ALLOC_REMARKS for audit visibility.
    enforced = ["'TBL'"]
    if pri_ct_check_rl:  enforced.append("'RL'")
    if pri_ct_check_tbc: enforced.append("'TBC'")
    pri_opt_in = ", ".join(enforced)
    mj_req_rem_avail = "MJ_REQ_REM" in work_cols
    _run(conn, f"""
        UPDATE [{working_table}] WITH (ROWLOCK, UPDLOCK) SET
            ALLOC_STATUS = CASE
                WHEN ISNULL(MSA_FNL_Q_REM, 0) <= 0 THEN 'SKIPPED'
                {("WHEN ISNULL(MJ_REQ_REM, 0) <= 0 THEN 'SKIPPED'"
                  if mj_req_rem_avail else "")}
                WHEN ISNULL(PRI_CT_REM, 0)    < 100
                     AND ISNULL(OPT_TYPE,'') IN ({pri_opt_in}) THEN 'SKIPPED'
                ELSE ALLOC_STATUS END,
            ALLOC_REMARKS = CASE
                WHEN ISNULL(MSA_FNL_Q_REM, 0) <= 0
                    THEN ISNULL(ALLOC_REMARKS,'')
                         + ' SKIP_MSA_EXHAUSTED(rem='
                         + CAST(ISNULL(MSA_FNL_Q_REM,0) AS NVARCHAR(20)) + ');'
                {("WHEN ISNULL(MJ_REQ_REM, 0) <= 0 "
                  "THEN ISNULL(ALLOC_REMARKS,'') "
                  "+ ' SKIP_MJ_EXHAUSTED(mj_rem=' "
                  "+ CAST(ISNULL(MJ_REQ_REM,0) AS NVARCHAR(20)) + ');'"
                  if mj_req_rem_avail else "")}
                WHEN ISNULL(PRI_CT_REM, 0)    < 100
                     AND ISNULL(OPT_TYPE,'') IN ({pri_opt_in})
                    THEN ISNULL(ALLOC_REMARKS,'')
                         + ' SKIP_PRI_BROKEN(pri_ct='
                         + CAST(ISNULL(PRI_CT_REM,0) AS NVARCHAR(20)) + '%);'
                ELSE ALLOC_REMARKS END
        WHERE LISTED_FLAG = 1
          AND MAJ_CAT IN ({mc_in})
          AND ISNULL(ALLOC_STATUS,'PENDING') NOT IN ('SKIPPED','ALLOCATED')
          AND OPT_PRIORITY_RANK BETWEEN :be_p1 AND :be_next
    """, params_mc)

    # Store-broken: MJ_REQ_REM < factor × ACS_D → skip next band of this store+opt_type
    if ENABLE_STORE_BROKEN and "MJ_REQ_REM" in work_cols:
        _run(conn, f"""
            UPDATE [{working_table}] WITH (ROWLOCK, UPDLOCK) SET
                ALLOC_STATUS = 'SKIPPED',
                ALLOC_REMARKS = ISNULL(ALLOC_REMARKS,'')
                    + ' SKIP_STORE_BROKEN(req_rem='
                    + CAST(ISNULL(MJ_REQ_REM,0) AS NVARCHAR(20))
                    + ',acs_d=' + CAST(ISNULL(ACS_D,0) AS NVARCHAR(20)) + ');'
            WHERE LISTED_FLAG = 1
              AND MAJ_CAT IN ({mc_in})
              AND ISNULL(ALLOC_STATUS,'PENDING') NOT IN ('SKIPPED','ALLOCATED')
              AND OPT_TYPE = :ot
              AND OPT_PRIORITY_RANK BETWEEN :be_p1 AND :be_next
              AND ISNULL(MJ_REQ_REM, 0) < {ACS_SKIP_FACTOR} * ISNULL(ACS_D, 0)
        """, params_mc)

    # (6) Propagate SKIP to alloc_table so future bands' Target CTE excludes them.
    #   SKIP_REASON carries working_table ALLOC_REMARKS so the reviewer sees the
    #   actual state (rem=X, pri_ct=Y%) without needing to join working_table.
    _run(conn, f"""
        UPDATE A WITH (ROWLOCK, UPDLOCK) SET
            A.ALLOC_STATUS = 'SKIPPED',
            A.SKIP_REASON  = CASE
                WHEN A.SKIP_REASON IS NULL OR A.SKIP_REASON = ''
                    THEN LTRIM(ISNULL(W.ALLOC_REMARKS,''))
                ELSE A.SKIP_REASON END
        FROM [{alloc_table}] A
        INNER JOIN [{working_table}] W
            ON A.WERKS=W.WERKS AND A.MAJ_CAT=W.MAJ_CAT
           AND A.GEN_ART_NUMBER=W.GEN_ART_NUMBER
           AND ISNULL(A.CLR,'') = ISNULL(W.CLR,'')
        WHERE W.ALLOC_STATUS = 'SKIPPED'
          AND W.MAJ_CAT IN ({mc_in})
          AND A.MAJ_CAT IN ({mc_in})
          AND ISNULL(A.ALLOC_STATUS,'PENDING') NOT IN ('SKIPPED','ALLOCATED','PARTIAL')
    """, params_mc)


# ───────────────────────────────────────────────────────────────
# FAST PATH — single round-trip band+revalidate (used by parallel modes)
# ───────────────────────────────────────────────────────────────
def _run_band_and_revalidate_batched(
    conn, working_table, alloc_table,
    opt_type: str, r: int, rank: int,
    grids: Dict[str, Dict],
    maj_cat: str,
    pri_ct_check_rl: bool = True,
    pri_ct_check_tbc: bool = True,
    werks: Optional[str] = None,
):
    """
    Single multi-statement T-SQL batch that does everything one rank/band needs:
      Step 1   - cumulative-window pool take (band UPDATE)
      Step 2   - pool decrement
      Reval    - MSA_FNL_Q_REM, per-grid REQ_REM, H_REM, PRI_CT_REM, skip
                 rules, store-broken, SKIP propagate to alloc

    All inside one `IF EXISTS (...) BEGIN ... END` guard so when the band
    took zero, the entire revalidation block is skipped server-side.

    Round-trips per band: 1 (down from ~11 in the sequential helpers).

    REQUIRED scope: maj_cat (this is the parallel/single-MAJ_CAT path; never
    runs across multiple MAJ_CATs). Sequential mode keeps using the original
    `_stage_c_run_band` + `_revalidate_after_band` — see those for the
    canonical, statement-by-statement implementation.

    werks: when provided, every SQL statement in the batch is additionally
    scoped to this single WERKS. Used by the store-first execution loop in
    _run_one_majcat so that revalidation (MSA_FNL_Q_REM, PRI_CT_REM, skip
    rules) only touches the store currently being processed — not all stores
    in the MAJ_CAT simultaneously.
    """
    work_cols = {c.upper() for c in _cols(conn, working_table)}

    # Store-scoped filter predicates — empty string when running all stores.
    wk_a  = " AND A.WERKS = :wk"           if werks else ""   # alloc alias A
    wk_w  = " AND WERKS = :wk"             if werks else ""   # plain / alias W
    wk_aw = " AND W.WERKS = :wk AND A.WERKS = :wk" if werks else ""  # step 6

    # ── Per-grid REQ_REM update fragments (built from grids dict) ──
    grid_update_fragments: List[str] = []
    for req_col, meta in grids.items():
        req_rem = meta["req_rem"]
        extras  = meta["extras"]
        if req_rem.upper() not in work_cols:
            continue
        if not all(e.upper() in work_cols for e in extras):
            continue
        grid_keys = ["WERKS", "MAJ_CAT"] + extras
        key_select = ", ".join(f"W2.[{k}]" for k in grid_keys)
        group_by   = ", ".join(f"W2.[{k}]" for k in grid_keys)
        # NULL-safe NVARCHAR-cast match — keeps semantics identical to the
        # sequential _revalidate_after_band path the user already validated.
        join_cond = " AND ".join(
            f"ISNULL(CAST(W.[{k}] AS NVARCHAR(200)),'') = "
            f"ISNULL(CAST(G.[{k}] AS NVARCHAR(200)),'')"
            for k in grid_keys
        )
        grid_update_fragments.append(f"""
            ;WITH GridTake_{req_col} AS (
                SELECT {key_select},
                       SUM(ISNULL(A.ROUND_SHIP,0)) AS grid_take
                FROM [{alloc_table}] A
                INNER JOIN [{working_table}] W2
                    ON A.WERKS=W2.WERKS AND A.MAJ_CAT=W2.MAJ_CAT
                   AND A.GEN_ART_NUMBER=W2.GEN_ART_NUMBER
                   AND ISNULL(A.CLR,'') = ISNULL(W2.CLR,'')
                WHERE A.OPT_TYPE = :ot
                  AND A.OPT_PRIORITY_RANK = :rk
                  AND A.MAJ_CAT = :mc{wk_a}
                GROUP BY {group_by}
                HAVING SUM(ISNULL(A.ROUND_SHIP,0)) > 0
            )
            UPDATE W SET
                W.[{req_rem}] = CASE
                    WHEN ISNULL(W.[{req_rem}], 0) - G.grid_take < 0 THEN 0
                    ELSE ISNULL(W.[{req_rem}], 0) - G.grid_take END
            FROM [{working_table}] W WITH (ROWLOCK)
            INNER JOIN GridTake_{req_col} G ON {join_cond};
        """)

    # ── H_REM SET fragment + PRI_CT_REM numerator/denominator ──
    h_rem_sets, pri_h, pri_gh = [], [], []
    for req_col, meta in grids.items():
        h_rem  = meta["h_rem"]
        gh_col = meta["gh_col"]
        req_rem = meta["req_rem"]
        if (h_rem.upper() in work_cols and req_rem.upper() in work_cols
                and gh_col.upper() in work_cols):
            h_rem_sets.append(
                f"[{h_rem}] = CASE "
                f"WHEN ISNULL([{req_rem}],0) > {ACS_SKIP_FACTOR} * ISNULL(ACS_D,0) "
                f"AND ISNULL([{gh_col}],0) = 1 THEN 1 ELSE 0 END"
            )
            pri_h.append(h_rem)
            pri_gh.append(gh_col)

    h_rem_sql = ""
    if h_rem_sets:
        # ROWLOCK hint on UPDATE-target table — concurrent workers updating
        # different MAJ_CATs (= different rows) won't escalate to page locks.
        h_rem_sql = f"""
            UPDATE [{working_table}] WITH (ROWLOCK) SET {', '.join(h_rem_sets)}
            WHERE MAJ_CAT = :mc{wk_w};
        """

    pri_ct_sql = ""
    if pri_h and pri_gh:
        h_sum  = " + ".join(f"ISNULL([{c}],0)" for c in pri_h)
        gh_sum = " + ".join(f"ISNULL([{c}],0)" for c in pri_gh)
        pri_ct_sql = f"""
            UPDATE [{working_table}] WITH (ROWLOCK) SET
                PRI_CT_REM = CASE
                    WHEN ({gh_sum}) = 0 THEN 0
                    ELSE ROUND(CAST(({h_sum}) AS FLOAT) / ({gh_sum}) * 100, 1) END
            WHERE MAJ_CAT = :mc{wk_w};
        """

    # PRI_CT% gate enforcement list
    enforced = ["'TBL'"]
    if pri_ct_check_rl:  enforced.append("'RL'")
    if pri_ct_check_tbc: enforced.append("'TBC'")
    pri_opt_in = ", ".join(enforced)

    store_broken_sql = ""
    if ENABLE_STORE_BROKEN and "MJ_REQ_REM" in work_cols:
        store_broken_sql = f"""
            UPDATE [{working_table}] WITH (ROWLOCK) SET
                ALLOC_STATUS = 'SKIPPED',
                ALLOC_REMARKS = ISNULL(ALLOC_REMARKS,'')
                    + ' SKIP_STORE_BROKEN(req_rem='
                    + CAST(ISNULL(MJ_REQ_REM,0) AS NVARCHAR(20))
                    + ',acs_d=' + CAST(ISNULL(ACS_D,0) AS NVARCHAR(20)) + ');'
            WHERE LISTED_FLAG = 1
              AND MAJ_CAT = :mc
              AND ISNULL(ALLOC_STATUS,'PENDING') NOT IN ('SKIPPED','ALLOCATED')
              AND OPT_TYPE = :ot
              AND OPT_PRIORITY_RANK BETWEEN :next_rk AND :next_end
              AND ISNULL(MJ_REQ_REM, 0) < {ACS_SKIP_FACTOR} * ISNULL(ACS_D, 0){wk_w};
        """

    # ── ONE multi-statement batch ──
    sql = f"""
        SET NOCOUNT ON;

        -- Step 1: band UPDATE (pool take + SHIP/HOLD writes)
        ;WITH Target AS (
            SELECT A.WERKS, A.RDC, A.MAJ_CAT, A.GEN_ART_NUMBER, A.CLR,
                   A.VAR_ART, A.SZ,
                   A.OPT_PRIORITY_RANK, A.ST_RANK, A.IS_NEW,
                   /* need_pool rules:
                      TBL — hold buffer counted ONCE across all rounds (all TBL,
                        not just IS_NEW=1, since TBL is pre-classified based on IS_NEW).
                        Cumulative target = SZ_MBQ_WH + (r-1)*SZ_MBQ.
                        Pure-HOLD guard: suppress when no ship demand remains.
                      RL/TBC — pool demand = ship demand (no hold buffer). */
                   CASE
                        WHEN :ot = 'TBL'
                             AND (:r * ISNULL(A.SZ_MBQ,0) - ISNULL(A.SZ_STK,0)
                                  - ISNULL(A.SHIP_QTY,0)) <= 0
                        THEN 0
                        WHEN :ot = 'TBL'
                             AND ISNULL(A.SZ_MBQ_WH,0) + :r*ISNULL(A.SZ_MBQ,0)
                                 - ISNULL(A.SZ_MBQ,0)
                                 > ISNULL(A.SZ_STK,0) + ISNULL(A.POOL_CONSUMED,0)
                        THEN ISNULL(A.SZ_MBQ_WH,0) + :r*ISNULL(A.SZ_MBQ,0)
                             - ISNULL(A.SZ_MBQ,0)
                             - ISNULL(A.SZ_STK,0) - ISNULL(A.POOL_CONSUMED,0)
                        WHEN :r * ISNULL(A.SZ_MBQ,0) - ISNULL(A.SZ_STK,0)
                                > ISNULL(A.POOL_CONSUMED,0)
                        THEN :r * ISNULL(A.SZ_MBQ,0) - ISNULL(A.SZ_STK,0)
                           - ISNULL(A.POOL_CONSUMED,0)
                        ELSE 0 END AS need_pool,
                   CASE WHEN :r * ISNULL(A.SZ_MBQ,0) - ISNULL(A.SZ_STK,0)
                           > ISNULL(A.SHIP_QTY,0)
                        THEN :r * ISNULL(A.SZ_MBQ,0) - ISNULL(A.SZ_STK,0)
                           - ISNULL(A.SHIP_QTY,0)
                        ELSE 0 END AS need_ship
            FROM [{alloc_table}] A
            WHERE A.OPT_TYPE = :ot
              AND A.OPT_PRIORITY_RANK = :rk
              AND A.MAJ_CAT = :mc
              AND ISNULL(A.ALLOC_STATUS,'PENDING') NOT IN ('SKIPPED','INELIGIBLE')
              AND ISNULL(A.I_ROD, 1) >= :r{wk_a}
        ),
        Ranked AS (
            SELECT T.*, P.FNL_Q_REM,
                   ROW_NUMBER() OVER (
                       PARTITION BY T.RDC, T.MAJ_CAT, T.GEN_ART_NUMBER, T.CLR, T.VAR_ART, T.SZ
                       ORDER BY ISNULL(T.ST_RANK,999999) ASC,
                                T.OPT_PRIORITY_RANK     ASC,
                                T.WERKS                 ASC
                   ) AS ord
            FROM Target T
            INNER JOIN {POOL_TABLE} P
                ON P.RDC = T.RDC AND P.MAJ_CAT = T.MAJ_CAT
               AND P.GEN_ART_NUMBER = T.GEN_ART_NUMBER
               AND ISNULL(P.CLR,'') = ISNULL(T.CLR,'')
               AND P.VAR_ART = T.VAR_ART AND P.SZ = T.SZ
            WHERE T.need_pool > 0 AND P.FNL_Q_REM > 0
        ),
        Cum AS (
            SELECT *,
                   SUM(need_pool) OVER (
                       PARTITION BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ
                       ORDER BY ord ROWS UNBOUNDED PRECEDING
                   ) AS cum_demand
            FROM Ranked
        ),
        Take AS (
            SELECT *,
                   CASE
                       WHEN FNL_Q_REM - (cum_demand - need_pool) <= 0 THEN 0
                       WHEN FNL_Q_REM - (cum_demand - need_pool) >= need_pool THEN need_pool
                       ELSE FNL_Q_REM - (cum_demand - need_pool)
                   END AS take_pool
            FROM Cum
        )
        UPDATE A SET
            A.POOL_CONSUMED = ISNULL(A.POOL_CONSUMED,0) + X.take_pool,
            /* All TBL: split at need_ship; excess to warehouse HOLD. RL/TBC 100% SHIP. */
            A.ROUND_SHIP    = CASE WHEN :ot = 'TBL'
                                   THEN CASE WHEN X.take_pool < X.need_ship
                                             THEN X.take_pool ELSE X.need_ship END
                                   ELSE X.take_pool END,
            A.ROUND_HOLD    = CASE WHEN :ot = 'TBL'
                                   THEN X.take_pool - CASE WHEN X.take_pool < X.need_ship
                                                           THEN X.take_pool ELSE X.need_ship END
                                   ELSE 0 END,
            A.SHIP_QTY      = ISNULL(A.SHIP_QTY,0) +
                              CASE WHEN :ot = 'TBL'
                                   THEN CASE WHEN X.take_pool < X.need_ship
                                             THEN X.take_pool ELSE X.need_ship END
                                   ELSE X.take_pool END,
            A.HOLD_QTY      = ISNULL(A.HOLD_QTY,0) +
                              CASE WHEN :ot = 'TBL'
                                   THEN X.take_pool - CASE WHEN X.take_pool < X.need_ship
                                                           THEN X.take_pool ELSE X.need_ship END
                                   ELSE 0 END,
            A.ALLOC_WAVE    = CONCAT(:ot, '_R', :r),
            A.ALLOC_ROUND   = :r,
            /* ALLOC_STATUS target:
               TBL → SZ_MBQ_WH + (I_ROD-1)×SZ_MBQ − SZ_STK  (hold counted once)
               All others   → I_ROD × SZ_MBQ − SZ_STK */
            A.ALLOC_STATUS  = CASE
                WHEN ISNULL(A.POOL_CONSUMED,0) + X.take_pool >= CASE
                     WHEN :ot = 'TBL' THEN
                          CASE WHEN ISNULL(A.SZ_MBQ_WH,0)
                                    + ISNULL(A.I_ROD,1)*ISNULL(A.SZ_MBQ,0)
                                    - ISNULL(A.SZ_MBQ,0) - ISNULL(A.SZ_STK,0) > 0
                               THEN ISNULL(A.SZ_MBQ_WH,0)
                                    + ISNULL(A.I_ROD,1)*ISNULL(A.SZ_MBQ,0)
                                    - ISNULL(A.SZ_MBQ,0) - ISNULL(A.SZ_STK,0)
                               ELSE 0 END
                     ELSE CASE WHEN ISNULL(A.I_ROD,1)*ISNULL(A.SZ_MBQ,0)
                                    - ISNULL(A.SZ_STK,0) > 0
                               THEN ISNULL(A.I_ROD,1)*ISNULL(A.SZ_MBQ,0)
                                    - ISNULL(A.SZ_STK,0)
                               ELSE 0 END
                     END
                THEN 'ALLOCATED'
                ELSE 'PARTIAL' END
        FROM [{alloc_table}] A WITH (ROWLOCK)
        INNER JOIN Take X
            ON A.WERKS = X.WERKS AND A.RDC = X.RDC
           AND A.MAJ_CAT = X.MAJ_CAT AND A.GEN_ART_NUMBER = X.GEN_ART_NUMBER
           AND ISNULL(A.CLR,'') = ISNULL(X.CLR,'')
           AND A.VAR_ART = X.VAR_ART AND A.SZ = X.SZ
        WHERE X.take_pool > 0;

        -- Step 2: pool decrement
        ;WITH S AS (
            SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
                   SUM(ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0)) AS taken
            FROM [{alloc_table}]
            WHERE OPT_TYPE = :ot
              AND OPT_PRIORITY_RANK = :rk
              AND ALLOC_ROUND = :r
              AND MAJ_CAT = :mc{wk_w}
            GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ
            HAVING SUM(ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0)) > 0
        )
        UPDATE P SET P.FNL_Q_REM = P.FNL_Q_REM - S.taken
        FROM {POOL_TABLE} P
        INNER JOIN S
            ON P.RDC = S.RDC AND P.MAJ_CAT = S.MAJ_CAT
           AND P.GEN_ART_NUMBER = S.GEN_ART_NUMBER
           AND ISNULL(P.CLR,'') = ISNULL(S.CLR,'')
           AND P.VAR_ART = S.VAR_ART AND P.SZ = S.SZ;

        -- Revalidation block — only if this band actually took something
        IF EXISTS (
            SELECT 1 FROM [{alloc_table}]
            WHERE OPT_TYPE = :ot AND OPT_PRIORITY_RANK = :rk AND MAJ_CAT = :mc
              AND ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0) > 0{wk_w}
        )
        BEGIN
            -- (1) MSA_FNL_Q_REM
            ;WITH OptTake AS (
                SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR,
                       SUM(ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0)) AS take_total
                FROM [{alloc_table}]
                WHERE OPT_TYPE = :ot
                  AND OPT_PRIORITY_RANK = :rk
                  AND MAJ_CAT = :mc{wk_w}
                GROUP BY WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR
                HAVING SUM(ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0)) > 0
            )
            UPDATE W SET
                W.MSA_FNL_Q_REM = CASE
                    WHEN ISNULL(W.MSA_FNL_Q_REM, 0) - O.take_total < 0 THEN 0
                    ELSE ISNULL(W.MSA_FNL_Q_REM, 0) - O.take_total END
            FROM [{working_table}] W WITH (ROWLOCK)
            INNER JOIN OptTake O
                ON W.WERKS=O.WERKS AND W.MAJ_CAT=O.MAJ_CAT
               AND W.GEN_ART_NUMBER=O.GEN_ART_NUMBER
               AND ISNULL(W.CLR,'') = ISNULL(O.CLR,'');

            -- (2) Per-grid REQ_REM
            {''.join(grid_update_fragments)}

            -- (3) H_<grid>_REM
            {h_rem_sql}

            -- (4) PRI_CT_REM
            {pri_ct_sql}

            -- (5) Skip rules — next band only; REM values in remarks for audit
            UPDATE [{working_table}] WITH (ROWLOCK) SET
                ALLOC_STATUS = CASE
                    WHEN ISNULL(MSA_FNL_Q_REM, 0) <= 0 THEN 'SKIPPED'
                    WHEN ISNULL(MJ_REQ_REM, 0)    <= 0 THEN 'SKIPPED'
                    WHEN ISNULL(PRI_CT_REM, 0)    < 100
                         AND ISNULL(OPT_TYPE,'') IN ({pri_opt_in}) THEN 'SKIPPED'
                    ELSE ALLOC_STATUS END,
                ALLOC_REMARKS = CASE
                    WHEN ISNULL(MSA_FNL_Q_REM, 0) <= 0
                        THEN ISNULL(ALLOC_REMARKS,'')
                             + ' SKIP_MSA_EXHAUSTED(rem='
                             + CAST(ISNULL(MSA_FNL_Q_REM,0) AS NVARCHAR(20)) + ');'
                    WHEN ISNULL(MJ_REQ_REM, 0)    <= 0
                        THEN ISNULL(ALLOC_REMARKS,'')
                             + ' SKIP_MJ_EXHAUSTED(mj_rem='
                             + CAST(ISNULL(MJ_REQ_REM,0) AS NVARCHAR(20)) + ');'
                    WHEN ISNULL(PRI_CT_REM, 0)    < 100
                         AND ISNULL(OPT_TYPE,'') IN ({pri_opt_in})
                        THEN ISNULL(ALLOC_REMARKS,'')
                             + ' SKIP_PRI_BROKEN(pri_ct='
                             + CAST(ISNULL(PRI_CT_REM,0) AS NVARCHAR(20)) + '%);'
                    ELSE ALLOC_REMARKS END
            WHERE LISTED_FLAG = 1
              AND MAJ_CAT = :mc
              AND ISNULL(ALLOC_STATUS,'PENDING') NOT IN ('SKIPPED','ALLOCATED')
              AND OPT_PRIORITY_RANK BETWEEN :next_rk AND :next_end{wk_w};

            -- (5b) Store-broken
            {store_broken_sql}

            -- (6) Propagate SKIP back to alloc_table; carry ALLOC_REMARKS as SKIP_REASON
            UPDATE A SET
                A.ALLOC_STATUS = 'SKIPPED',
                A.SKIP_REASON  = CASE
                    WHEN A.SKIP_REASON IS NULL OR A.SKIP_REASON = ''
                        THEN LTRIM(ISNULL(W.ALLOC_REMARKS,''))
                    ELSE A.SKIP_REASON END
            FROM [{alloc_table}] A WITH (ROWLOCK)
            INNER JOIN [{working_table}] W
                ON A.WERKS=W.WERKS AND A.MAJ_CAT=W.MAJ_CAT
               AND A.GEN_ART_NUMBER=W.GEN_ART_NUMBER
               AND ISNULL(A.CLR,'') = ISNULL(W.CLR,'')
            WHERE W.ALLOC_STATUS = 'SKIPPED'
              AND W.MAJ_CAT = :mc AND A.MAJ_CAT = :mc
              AND ISNULL(A.ALLOC_STATUS,'PENDING') NOT IN ('SKIPPED','ALLOCATED','PARTIAL'){wk_aw};
        END
    """
    params = {
        "ot": opt_type, "r": r, "rk": rank, "mc": maj_cat,
        "next_rk":  rank + 1,
        "next_end": rank + BAND_SIZE,
    }
    if werks:
        params["wk"] = werks
    _run(conn, sql, params)


# ───────────────────────────────────────────────────────────────
# STAGE C — ALLOCATE (pool waterfall)
# ───────────────────────────────────────────────────────────────
def _stage_c_build_pool(conn, alloc_table):
    _run(conn, f"IF OBJECT_ID('tempdb..{POOL_TABLE}') IS NOT NULL DROP TABLE {POOL_TABLE}")
    _run(conn, f"""
        SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
               MAX(ISNULL(FNL_Q,0)) AS FNL_Q_ORIG,
               MAX(ISNULL(FNL_Q,0)) AS FNL_Q_REM
        INTO {POOL_TABLE}
        FROM [{alloc_table}]
        GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ
    """)
    try:
        _run(conn, f"""
            CREATE UNIQUE CLUSTERED INDEX IX_pool_key ON {POOL_TABLE}
              (RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ)
        """)
    except Exception:
        pass


def _stage_c_apply_mbq_cap(conn, alloc_table: str, working_table: str,
                            opt_type: str, cap_pct: float) -> None:
    """Post-waterfall clip: for each (WERKS, MAJ_CAT) whose cumulative SHIP_QTY
    exceeds cap_pct% of MJ_MBQ, zero out the lowest-priority rows until the
    total is within budget. Rows that are trimmed are set to SHIP_QTY=0 and
    ALLOC_STATUS='SKIPPED', SKIP_REASON='MBQ_CAP'.
    Only used by sequential mode — pandas mode applies the cap inline in _run_band."""
    _ensure_alloc_remarks_col(conn, alloc_table)
    try:
        _run(conn, f"""
            ;WITH Budget AS (
                SELECT W.WERKS, W.MAJ_CAT,
                       ISNULL(MAX(ISNULL(W.MJ_MBQ,0)) * :cap / 100.0
                              - MAX(ISNULL(W.MJ_STK_TTL,0)), 0) AS budget
                FROM [{working_table}] W
                WHERE W.LISTED_FLAG = 1
                GROUP BY W.WERKS, W.MAJ_CAT
            ),
            Ordered AS (
                SELECT A.WERKS, A.MAJ_CAT, A.GEN_ART_NUMBER, A.CLR, A.VAR_ART, A.SZ,
                       A.SHIP_QTY,
                       SUM(A.SHIP_QTY) OVER (
                           PARTITION BY A.WERKS, A.MAJ_CAT
                           ORDER BY ISNULL(A.OPT_PRIORITY_RANK,999999) ASC,
                                    ISNULL(A.ST_RANK,999999) ASC,
                                    A.GEN_ART_NUMBER         ASC,
                                    ISNULL(A.CLR, '')        ASC,
                                    ISNULL(A.VAR_ART, '')    ASC,
                                    ISNULL(A.SZ, '')         ASC
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) AS cum_ship,
                       B.budget
                FROM [{alloc_table}] A
                JOIN Budget B ON A.WERKS = B.WERKS AND A.MAJ_CAT = B.MAJ_CAT
                WHERE A.OPT_TYPE = :ot
            )
            UPDATE A
               SET A.SHIP_QTY     = CASE
                       WHEN O.cum_ship <= O.budget THEN O.SHIP_QTY
                       WHEN O.cum_ship - O.SHIP_QTY >= O.budget THEN 0
                       ELSE O.budget - (O.cum_ship - O.SHIP_QTY) END,
                   A.ALLOC_STATUS = CASE
                       WHEN O.cum_ship - O.SHIP_QTY >= O.budget THEN 'SKIPPED'
                       ELSE A.ALLOC_STATUS END,
                   A.SKIP_REASON  = CASE
                       WHEN O.cum_ship - O.SHIP_QTY >= O.budget
                            THEN 'MBQ_CAP_' + :ot_name
                            + '(cap_pct=' + CAST(:cap_pct_dbg AS NVARCHAR(20)) + ')'
                       ELSE A.SKIP_REASON END,
                   A.ALLOC_REMARKS = CASE
                       WHEN O.cum_ship - O.SHIP_QTY >= O.budget
                            THEN ISNULL(A.ALLOC_REMARKS,'')
                                + ' MBQ_CAP_HIT(opt_type=' + :ot_name
                                + ', cap_pct=' + CAST(:cap_pct_dbg AS NVARCHAR(20))
                                + ', cum_ship=' + CAST(O.cum_ship   AS NVARCHAR(20))
                                + ', budget='   + CAST(O.budget     AS NVARCHAR(20))
                                + ', trim_row=' + CAST(O.SHIP_QTY   AS NVARCHAR(20))
                                + ');'
                       WHEN O.cum_ship > O.budget
                            THEN ISNULL(A.ALLOC_REMARKS,'')
                                + ' MBQ_CAP_PARTIAL(opt_type=' + :ot_name
                                + ', cap_pct=' + CAST(:cap_pct_dbg AS NVARCHAR(20))
                                + ', kept='    + CAST(O.budget - (O.cum_ship - O.SHIP_QTY) AS NVARCHAR(20))
                                + ', trimmed=' + CAST(O.SHIP_QTY - (O.budget - (O.cum_ship - O.SHIP_QTY)) AS NVARCHAR(20))
                                + ');'
                       ELSE A.ALLOC_REMARKS END
            FROM [{alloc_table}] A
            INNER JOIN Ordered O
                ON A.WERKS          = O.WERKS
               AND A.MAJ_CAT        = O.MAJ_CAT
               AND A.GEN_ART_NUMBER = O.GEN_ART_NUMBER
               AND ISNULL(A.CLR,'') = ISNULL(O.CLR,'')
               AND A.VAR_ART        = O.VAR_ART
               AND A.SZ             = O.SZ
            WHERE A.OPT_TYPE = :ot
        """, {"cap": float(cap_pct), "ot": opt_type,
              "ot_name": opt_type, "cap_pct_dbg": float(cap_pct)})
    except Exception as e:
        logger.warning(f"[C] MBQ cap ({opt_type}) failed: {e}")


def _stage_c_apply_opt_mj_req_cap(conn, alloc_table: str, working_table: str,
                                    opt_type: str, cap_pct: float,
                                    prior_opt_types: Optional[List[str]] = None
                                    ) -> None:
    """Per-OPT_TYPE downward MJ_REQ cap, shared-sequential model.

    For the given opt_type, SUM(SHIP_QTY) per (WERKS, MAJ_CAT) is clamped to
        max(0, cap_pct% × MJ_REQ − SUM(SHIP across prior_opt_types))

    With `prior_opt_types=None` or empty, the budget is the full `cap_pct% × MJ_REQ`
    (independent cap, legacy behaviour). With non-empty `prior_opt_types`, this
    opt_type only gets the REMAINING headroom — so RL → TBC → TBL never breach
    a single shared MJ_REQ budget at default 100/100/100. Caller is responsible
    for ordering the call sequence (typically RL, TBC, TBL by priority).

    Trimmed-to-zero rows: ALLOC_STATUS='SKIPPED', SKIP_REASON='<OPT>_MJ_REQ_CAP'.
    Partial trims: keep ALLOC_STATUS, append <OPT>_MJ_REQ_CAP_PARTIAL audit
    line to ALLOC_REMARKS. Mirrors _stage_c_apply_mbq_cap so reason/remark
    bookkeeping stays consistent."""
    if cap_pct <= 0:
        return
    ot = str(opt_type).upper().strip()
    if ot not in ("RL", "TBC", "TBL"):
        logger.warning(f"[C] {opt_type} MJ_REQ cap: unsupported opt_type, skipped")
        return
    _ensure_alloc_remarks_col(conn, alloc_table)
    # Inline-quote the prior opt_types list because ODBC params can't expand
    # into IN (...). Each value is validated against the allowed set above.
    prior_clean = [str(x).upper().strip() for x in (prior_opt_types or [])
                   if str(x).upper().strip() in ("RL", "TBC", "TBL")]
    if prior_clean:
        prior_in = ", ".join(f"'{p}'" for p in prior_clean)
        prior_cte = (
            f"PriorShip AS (\n"
            f"    SELECT WERKS, MAJ_CAT,\n"
            f"           SUM(ISNULL(SHIP_QTY, 0.0)) AS prior_ship\n"
            f"    FROM [{alloc_table}]\n"
            f"    WHERE OPT_TYPE IN ({prior_in})\n"
            f"    GROUP BY WERKS, MAJ_CAT\n"
            f"),\n"
        )
        prior_join = ("LEFT JOIN PriorShip P "
                      "ON W.WERKS = P.WERKS AND W.MAJ_CAT = P.MAJ_CAT")
        prior_sub  = "- ISNULL(MAX(P.prior_ship), 0.0)"
    else:
        prior_cte  = ""
        prior_join = ""
        prior_sub  = ""
    try:
        _run(conn, f"""
            ;WITH {prior_cte}Budget AS (
                SELECT W.WERKS, W.MAJ_CAT,
                       CASE
                           WHEN ISNULL(MAX(ISNULL(W.MJ_REQ,0)) * :cap / 100.0, 0)
                                {prior_sub} > 0
                           THEN ISNULL(MAX(ISNULL(W.MJ_REQ,0)) * :cap / 100.0, 0)
                                {prior_sub}
                           ELSE 0
                       END AS budget
                FROM [{working_table}] W
                {prior_join}
                WHERE W.LISTED_FLAG = 1
                GROUP BY W.WERKS, W.MAJ_CAT
            ),
            Ordered AS (
                SELECT A.WERKS, A.MAJ_CAT, A.GEN_ART_NUMBER, A.CLR, A.VAR_ART, A.SZ,
                       A.SHIP_QTY,
                       SUM(A.SHIP_QTY) OVER (
                           PARTITION BY A.WERKS, A.MAJ_CAT
                           ORDER BY ISNULL(A.OPT_PRIORITY_RANK,999999) ASC,
                                    ISNULL(A.ST_RANK,999999) ASC,
                                    A.GEN_ART_NUMBER         ASC,
                                    ISNULL(A.CLR, '')        ASC,
                                    ISNULL(A.VAR_ART, '')    ASC,
                                    ISNULL(A.SZ, '')         ASC
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) AS cum_ship,
                       B.budget
                FROM [{alloc_table}] A
                JOIN Budget B ON A.WERKS = B.WERKS AND A.MAJ_CAT = B.MAJ_CAT
                WHERE A.OPT_TYPE = :ot AND ISNULL(A.SHIP_QTY, 0) > 0
            )
            UPDATE A
               SET A.SHIP_QTY     = CASE
                       WHEN O.cum_ship <= O.budget THEN O.SHIP_QTY
                       WHEN O.cum_ship - O.SHIP_QTY >= O.budget THEN 0
                       ELSE O.budget - (O.cum_ship - O.SHIP_QTY) END,
                   A.ALLOC_STATUS = CASE
                       WHEN O.cum_ship - O.SHIP_QTY >= O.budget THEN 'SKIPPED'
                       ELSE A.ALLOC_STATUS END,
                   A.SKIP_REASON  = CASE
                       WHEN O.cum_ship - O.SHIP_QTY >= O.budget
                            THEN :ot + '_MJ_REQ_CAP'
                                + '(cap_pct=' + CAST(:cap_pct_dbg AS NVARCHAR(20)) + ')'
                       ELSE A.SKIP_REASON END,
                   A.ALLOC_REMARKS = CASE
                       WHEN O.cum_ship - O.SHIP_QTY >= O.budget
                            THEN ISNULL(A.ALLOC_REMARKS,'')
                                + ' ' + :ot + '_MJ_REQ_CAP_HIT(cap_pct='
                                + CAST(:cap_pct_dbg AS NVARCHAR(20))
                                + ', cum_ship=' + CAST(O.cum_ship AS NVARCHAR(20))
                                + ', budget='   + CAST(O.budget   AS NVARCHAR(20))
                                + ', trim_row=' + CAST(O.SHIP_QTY AS NVARCHAR(20))
                                + ');'
                       WHEN O.cum_ship > O.budget
                            THEN ISNULL(A.ALLOC_REMARKS,'')
                                + ' ' + :ot + '_MJ_REQ_CAP_PARTIAL(cap_pct='
                                + CAST(:cap_pct_dbg AS NVARCHAR(20))
                                + ', kept='    + CAST(O.budget - (O.cum_ship - O.SHIP_QTY) AS NVARCHAR(20))
                                + ', trimmed=' + CAST(O.SHIP_QTY - (O.budget - (O.cum_ship - O.SHIP_QTY)) AS NVARCHAR(20))
                                + ');'
                       ELSE A.ALLOC_REMARKS END
            FROM [{alloc_table}] A
            INNER JOIN Ordered O
                ON A.WERKS          = O.WERKS
               AND A.MAJ_CAT        = O.MAJ_CAT
               AND A.GEN_ART_NUMBER = O.GEN_ART_NUMBER
               AND ISNULL(A.CLR,'') = ISNULL(O.CLR,'')
               AND A.VAR_ART        = O.VAR_ART
               AND A.SZ             = O.SZ
            WHERE A.OPT_TYPE = :ot
        """, {"cap": float(cap_pct), "cap_pct_dbg": float(cap_pct), "ot": ot})
        logger.info(f"[C] {ot} MJ_REQ cap applied (cap_pct={cap_pct})")
    except Exception as e:
        logger.warning(f"[C] {ot} MJ_REQ cap failed: {e}")


# Backwards-compat thin wrapper — existing call sites can keep using the
# TBL-specific name. New code should call _stage_c_apply_opt_mj_req_cap.
def _stage_c_apply_tbl_mj_req_cap(conn, alloc_table: str, working_table: str,
                                   cap_pct: float) -> None:
    _stage_c_apply_opt_mj_req_cap(conn, alloc_table, working_table, "TBL", cap_pct)


def _stage_c_apply_opt_mj_req_gate(conn, alloc_table: str, working_table: str,
                                     rl_cap_pct: float,
                                     tbc_cap_pct: float,
                                     tbl_cap_pct: float,
                                     mbq_gate_factor: float = 0.5,
                                     ) -> None:
    """OPT-grain MJ_REQ gate (sequential req_rem consumption).

    Per (WERKS, MAJ_CAT), `req_rem` starts at cap_pct × MJ_REQ for whatever
    opt_type is being checked. Walk OPTs in priority order (OPT_TYPE:
    RL→TBC→TBL, then OPT_PRIORITY_TIER/RANK/ST_RANK):

      - if req_rem ≥ gate_factor × OPT_MBQ:
            SHIP this OPT in full (waterfall SHIP and HOLD stay untouched)
            req_rem -= opt_ship    (only ship consumes the budget — hold is
                                    a reserved-for-later quantity)
      - else:
            SKIP this OPT (SHIP=HOLD=0, ALLOC_STATUS='SKIPPED')
            req_rem unchanged
            continue to next OPT

    Multiple OPTs may ship per (WERKS, MAJ_CAT) — the loop only stops shipping
    when req_rem drops below the gate threshold of the OPT under consideration.
    Smaller-OPT-MBQ rows further down the priority list can still pass after
    a larger one fails. SKIP_REASON='{OPT}_MJ_REQ_GATE_FAIL' on every skipped
    OPT, with audit detail (budget_at_decision, opt_mbq, gate_factor) appended
    to ALLOC_REMARKS.

    Per-type cap_pct sliders modify the EFFECTIVE BUDGET at the moment that
    opt_type's OPT is evaluated. With cap_pct=100, the budget is the current
    req_rem (raw MJ_REQ at start, decreasing as OPTs ship). With cap_pct=50,
    each gate test for that opt_type uses 0.5 × req_rem (stricter — fewer
    OPTs pass). cap_pct=0 disables the gate for that opt_type → every OPT
    of that type is skipped.

    Implementation: pulled into Python because the running req_rem is
    inherently sequential per (WERKS, MAJ_CAT). Skip decisions are applied
    via a temp-table JOIN UPDATE.
    """
    if max(rl_cap_pct, tbc_cap_pct, tbl_cap_pct) <= 0:
        return
    _ensure_alloc_remarks_col(conn, alloc_table)

    # 1) Fetch OPT-level aggregates + MJ_REQ per (WERKS, MAJ_CAT)
    rows = conn.execute(text(f"""
        SELECT O.WERKS, O.MAJ_CAT, O.GEN_ART_NUMBER, ISNULL(O.CLR,'') AS CLR_K,
               O.OPT_TYPE,
               MAX(ISNULL(O.OPT_PRIORITY_TIER, 3))     AS opt_tier,
               MAX(ISNULL(O.OPT_PRIORITY_RANK, 999999)) AS opt_rank,
               MAX(ISNULL(O.ST_RANK, 999999))          AS st_rank,
               MAX(ISNULL(O.OPT_MBQ, 0))               AS opt_mbq,
               SUM(ISNULL(O.SHIP_QTY, 0))              AS opt_ship,
               SUM(ISNULL(O.HOLD_QTY, 0))              AS opt_hold,
               ISNULL(MAX(M.mj_req), 0)                AS mj_req
        FROM [{alloc_table}] O
        LEFT JOIN (
            SELECT WERKS, MAJ_CAT, MAX(ISNULL(MJ_REQ, 0)) AS mj_req
            FROM [{working_table}]
            WHERE LISTED_FLAG = 1
            GROUP BY WERKS, MAJ_CAT
        ) M ON M.WERKS = O.WERKS AND M.MAJ_CAT = O.MAJ_CAT
        GROUP BY O.WERKS, O.MAJ_CAT, O.GEN_ART_NUMBER, ISNULL(O.CLR,''), O.OPT_TYPE
    """)).fetchall()

    if not rows:
        logger.info(f"[C] OPT-grain MJ_REQ gate: no OPTs to evaluate")
        return

    # 2) Sort by (WERKS, MAJ_CAT, opt_type_order, opt_tier, opt_rank, st_rank,
    #    gen_art, clr) — matches the priority order in the walk.
    ot_order = {"RL": 1, "TBC": 2, "TBL": 3}
    cap_map  = {"RL": float(rl_cap_pct), "TBC": float(tbc_cap_pct),
                "TBL": float(tbl_cap_pct)}

    def _key(r):
        return (
            str(r.WERKS or ""), str(r.MAJ_CAT or ""),
            ot_order.get(str(r.OPT_TYPE or "").upper(), 9),
            int(r.opt_tier or 3),
            int(r.opt_rank or 999999),
            int(r.st_rank or 999999),
            str(r.GEN_ART_NUMBER or ""),
            str(r.CLR_K or ""),
        )

    rows_sorted = sorted(rows, key=_key)

    # Precompute the smallest TBL OPT_MBQ per (WERKS, MAJ_CAT) slice over
    # TBL rows that actually have a positive waterfall outcome
    # (opt_ship + opt_hold > 0). This anchors the first-shipment-floor
    # escape below: if no TBL has shipped yet in a slice and the OPT
    # under evaluation has the smallest TBL OPT_MBQ available, let it
    # through even when budget < gate_threshold. Prevents a slice from
    # silently zeroing every TBL when MJ_REQ is small relative to OPT_MBQ.
    min_opt_mbq_by_slice: Dict[Tuple[str, str], float] = {}
    for _r in rows:
        if str(_r.OPT_TYPE or "").upper().strip() != "TBL":
            continue
        _ship = float(_r.opt_ship or 0.0)
        _hold = float(_r.opt_hold or 0.0)
        if _ship + _hold <= 0.0:
            continue
        _mbq = float(_r.opt_mbq or 0.0)
        if _mbq <= 0.0:
            continue
        _slk = (str(_r.WERKS or ""), str(_r.MAJ_CAT or ""))
        cur = min_opt_mbq_by_slice.get(_slk)
        if cur is None or _mbq < cur:
            min_opt_mbq_by_slice[_slk] = _mbq

    # 3) Iterate per (WERKS, MAJ_CAT) tracking req_rem. Record skip decisions.
    skip_records: List[Dict[str, Any]] = []
    current_key: Tuple[str, str] = ("", "")
    req_rem: float = 0.0
    n_shipped_in_slice: int = 0  # resets per (WERKS, MAJ_CAT)
    n_skipped = 0
    n_shipped = 0
    n_floor_escape = 0
    for r in rows_sorted:
        wm_key = (str(r.WERKS or ""), str(r.MAJ_CAT or ""))
        if wm_key != current_key:
            current_key = wm_key
            req_rem = float(r.mj_req or 0.0)
            n_shipped_in_slice = 0
        ot = str(r.OPT_TYPE or "").upper().strip()
        cap_pct = cap_map.get(ot, 0.0)
        opt_ship = float(r.opt_ship or 0.0)
        opt_hold = float(r.opt_hold or 0.0)
        opt_mbq  = float(r.opt_mbq or 0.0)
        # Skip immediately if this OPT had no ship/hold from waterfall — no
        # work to do (no rows to zero, no budget to deduct).
        if opt_ship + opt_hold <= 0.0:
            continue
        # OPT-grain MJ_REQ gate applies to TBL only. RL/TBC are mandatory
        # replenishments of existing/depleting stock — gating them on
        # half-OPT_MBQ would block legitimate top-ups.  Still track their
        # ship against req_rem so the TBL gate evaluation is correct.
        if ot != 'TBL':
            req_rem -= opt_ship
            n_shipped += 1
            n_shipped_in_slice += 1
            continue
        if cap_pct <= 0.0:
            # TBL cap disabled → always skip
            skip_records.append({
                "WERKS": wm_key[0], "MAJ_CAT": wm_key[1],
                "GEN_ART_NUMBER": str(r.GEN_ART_NUMBER or ""),
                "CLR_K": str(r.CLR_K or ""),
                "OPT_TYPE": ot,
                "skip_reason": f"{ot}_MJ_REQ_GATE_DISABLED",
                "budget": round(req_rem, 2),
                "opt_mbq": round(opt_mbq, 2),
            })
            n_skipped += 1
            continue
        # Effective budget for this opt_type's gate test. cap_pct < 100 makes
        # the gate stricter without permanently shrinking req_rem (req_rem is
        # the shared running budget; cap_pct only affects the comparison).
        budget = req_rem * (cap_pct / 100.0)
        gate_threshold = float(mbq_gate_factor) * opt_mbq
        if budget >= gate_threshold:
            # SHIP this OPT in full. Waterfall rows stay untouched.
            req_rem -= opt_ship
            n_shipped += 1
            n_shipped_in_slice += 1
        else:
            # First-shipment-floor escape (TBL only): if NOTHING has shipped
            # yet in this slice, there is still positive req_rem, and this OPT
            # carries the smallest TBL OPT_MBQ available in the slice, allow
            # it through. Without this, a slice with MJ_REQ=8 and three TBL
            # OPTs at OPT_MBQ=17 each would zero every TBL (8 < 0.5*17=8.5)
            # despite the waterfall having found stores willing to take them.
            slice_min_mbq = min_opt_mbq_by_slice.get(wm_key)
            if (
                n_shipped_in_slice == 0
                and req_rem > 0.0
                and slice_min_mbq is not None
                and abs(opt_mbq - slice_min_mbq) <= 1e-9
            ):
                req_rem -= opt_ship
                n_shipped += 1
                n_shipped_in_slice += 1
                n_floor_escape += 1
                logger.info(
                    f"[C] TBL_MJ_REQ_GATE floor_escape: "
                    f"WERKS={wm_key[0]} MAJ_CAT={wm_key[1]} "
                    f"GEN_ART={r.GEN_ART_NUMBER} CLR={r.CLR_K} "
                    f"req_rem={req_rem + opt_ship:.2f} opt_mbq={opt_mbq:.2f} "
                    f"opt_ship={opt_ship:.2f} (smallest TBL OPT_MBQ in slice; "
                    f"first shipment allowed)"
                )
                continue
            # SKIP this OPT. req_rem unchanged — a later, smaller-OPT_MBQ
            # OPT may still pass.
            skip_records.append({
                "WERKS": wm_key[0], "MAJ_CAT": wm_key[1],
                "GEN_ART_NUMBER": str(r.GEN_ART_NUMBER or ""),
                "CLR_K": str(r.CLR_K or ""),
                "OPT_TYPE": ot,
                "skip_reason": (
                    f"{ot}_MJ_REQ_GATE_FAIL"
                    f"(req_rem={req_rem:.2f},opt_mbq={opt_mbq:.2f},"
                    f"gate_factor={float(mbq_gate_factor):.3f},"
                    f"cap_pct={cap_pct:.0f})"
                ),
                "budget": round(req_rem, 2),
                "opt_mbq": round(opt_mbq, 2),
            })
            n_skipped += 1

    if not skip_records:
        logger.info(
            f"[C] OPT-grain MJ_REQ gate: {n_shipped} OPTs shipped "
            f"(floor_escape={n_floor_escape}), 0 skipped "
            f"(gate_factor={mbq_gate_factor})"
        )
        return

    # 4) Apply skips via temp table + UPDATE JOIN. Temp tables are
    # session-scoped, so they persist across these statements on the same
    # connection.
    try:
        try:
            _run(conn, "DROP TABLE #OptSkip")
        except Exception:
            pass
        _run(conn, """
            CREATE TABLE #OptSkip (
                WERKS          NVARCHAR(80)   NOT NULL,
                MAJ_CAT        NVARCHAR(120)  NOT NULL,
                GEN_ART_NUMBER NVARCHAR(80)   NOT NULL,
                CLR_K          NVARCHAR(80)   NOT NULL,
                OPT_TYPE       NVARCHAR(10)   NOT NULL,
                skip_reason    NVARCHAR(500)  NOT NULL,
                budget         DECIMAL(18, 2) NULL,
                opt_mbq        DECIMAL(18, 2) NULL,
                PRIMARY KEY (WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR_K, OPT_TYPE)
            )
        """)
        # Bulk insert in chunks so the parameter list doesn't blow up.
        CHUNK = 500
        for i in range(0, len(skip_records), CHUNK):
            batch = skip_records[i:i + CHUNK]
            conn.execute(
                text(
                    "INSERT INTO #OptSkip "
                    "(WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR_K, OPT_TYPE, "
                    " skip_reason, budget, opt_mbq) "
                    "VALUES (:WERKS, :MAJ_CAT, :GEN_ART_NUMBER, :CLR_K, "
                    ":OPT_TYPE, :skip_reason, :budget, :opt_mbq)"
                ),
                batch,
            )
        # GATE_ZEROED audit: capture original SHIP/HOLD per OPT BEFORE the
        # UPDATE below zeroes the size rows. We aggregate to OPT grain so
        # the token reflects the OPT-level waterfall outcome (not a single
        # size). Only rows where was_ship > 0 receive the token — rows that
        # were already zero have nothing to record.
        try:
            try:
                _run(conn, "DROP TABLE #OptGateAudit")
            except Exception:
                pass
            _run(conn, f"""
                SELECT A.WERKS, A.MAJ_CAT, A.GEN_ART_NUMBER,
                       ISNULL(A.CLR,'') AS CLR_K, A.OPT_TYPE,
                       SUM(ISNULL(A.SHIP_QTY, 0)) AS was_ship,
                       SUM(ISNULL(A.HOLD_QTY, 0)) AS was_hold,
                       MAX(S.skip_reason)         AS skip_reason
                INTO #OptGateAudit
                FROM [{alloc_table}] A
                INNER JOIN #OptSkip S
                    ON  S.WERKS          = A.WERKS
                    AND S.MAJ_CAT        = A.MAJ_CAT
                    AND S.GEN_ART_NUMBER = A.GEN_ART_NUMBER
                    AND S.CLR_K          = ISNULL(A.CLR, '')
                    AND S.OPT_TYPE       = A.OPT_TYPE
                WHERE S.skip_reason LIKE 'TBL_MJ_REQ_GATE_FAIL%'
                GROUP BY A.WERKS, A.MAJ_CAT, A.GEN_ART_NUMBER,
                         ISNULL(A.CLR,''), A.OPT_TYPE
                HAVING SUM(ISNULL(A.SHIP_QTY, 0)) > 0
            """)
        except Exception as _audit_e:
            logger.warning(f"[C] GATE_ZEROED audit snapshot failed: {_audit_e}")

        _run(conn, f"""
            UPDATE A
               SET A.SHIP_QTY     = 0,
                   A.HOLD_QTY     = 0,
                   A.ROUND_SHIP   = 0,
                   A.ROUND_HOLD   = 0,
                   A.ALLOC_STATUS = 'SKIPPED',
                   A.SKIP_REASON  = S.skip_reason,
                   A.ALLOC_REMARKS = ISNULL(A.ALLOC_REMARKS, '')
                       + ' OPT_MJ_REQ_GATE_SKIP('
                       + 'opt_type=' + S.OPT_TYPE
                       + ',req_rem=' + CAST(S.budget AS NVARCHAR(20))
                       + ',opt_mbq=' + CAST(S.opt_mbq AS NVARCHAR(20))
                       + ');'
            FROM [{alloc_table}] A
            INNER JOIN #OptSkip S
                ON  S.WERKS          = A.WERKS
                AND S.MAJ_CAT        = A.MAJ_CAT
                AND S.GEN_ART_NUMBER = A.GEN_ART_NUMBER
                AND S.CLR_K          = ISNULL(A.CLR, '')
                AND S.OPT_TYPE       = A.OPT_TYPE
            WHERE (ISNULL(A.SHIP_QTY, 0) > 0 OR ISNULL(A.HOLD_QTY, 0) > 0)
        """)

        # GATE_ZEROED token: stamp the pre-zero ship/hold + reason onto
        # ALLOC_REMARKS for both the size-grain alloc rows and the matching
        # OPT-grain working_table listing rows (LISTED_FLAG=1). This makes
        # the loss visible during reconciliation — auditors can see what
        # the waterfall produced before the gate erased it.
        try:
            _run(conn, f"""
                UPDATE A
                   SET A.ALLOC_REMARKS = ISNULL(A.ALLOC_REMARKS, '')
                       + ' | GATE_ZEROED(was_ship='
                       + CAST(G.was_ship AS NVARCHAR(20))
                       + ', was_hold='
                       + CAST(G.was_hold AS NVARCHAR(20))
                       + ', reason='
                       + ISNULL(G.skip_reason, '')
                       + ')'
                FROM [{alloc_table}] A
                INNER JOIN #OptGateAudit G
                    ON  G.WERKS          = A.WERKS
                    AND G.MAJ_CAT        = A.MAJ_CAT
                    AND G.GEN_ART_NUMBER = A.GEN_ART_NUMBER
                    AND G.CLR_K          = ISNULL(A.CLR, '')
                    AND G.OPT_TYPE       = A.OPT_TYPE
            """)
            _run(conn, f"""
                UPDATE W
                   SET W.ALLOC_REMARKS = ISNULL(W.ALLOC_REMARKS, '')
                       + ' | GATE_ZEROED(was_ship='
                       + CAST(G.was_ship AS NVARCHAR(20))
                       + ', was_hold='
                       + CAST(G.was_hold AS NVARCHAR(20))
                       + ', reason='
                       + ISNULL(G.skip_reason, '')
                       + ')'
                FROM [{working_table}] W
                INNER JOIN #OptGateAudit G
                    ON  G.WERKS          = W.WERKS
                    AND G.MAJ_CAT        = W.MAJ_CAT
                    AND G.GEN_ART_NUMBER = W.GEN_ART_NUMBER
                    AND G.CLR_K          = ISNULL(W.CLR, '')
                    AND G.OPT_TYPE       = W.OPT_TYPE
                WHERE ISNULL(W.LISTED_FLAG, 0) = 1
            """)
        except Exception as _stamp_e:
            logger.warning(f"[C] GATE_ZEROED stamp failed: {_stamp_e}")
        try:
            _run(conn, "DROP TABLE #OptGateAudit")
        except Exception:
            pass
        _run(conn, "DROP TABLE #OptSkip")
        logger.info(
            f"[C] OPT-grain MJ_REQ gate: {n_shipped} OPTs shipped "
            f"(floor_escape={n_floor_escape}), {n_skipped} skipped "
            f"(gate_factor={mbq_gate_factor}, "
            f"caps RL={rl_cap_pct}% TBC={tbc_cap_pct}% TBL={tbl_cap_pct}%)"
        )
    except Exception as e:
        logger.warning(f"[C] OPT-grain MJ_REQ gate failed: {e}")
        try:
            _run(conn, "DROP TABLE #OptSkip")
        except Exception:
            pass


def _revalidate_cross_type(
    conn, working_table, alloc_table,
    completed_ot: str, next_types: List[str],
    grids: Dict[str, Dict],
    pri_ct_check_rl: bool = True,
    pri_ct_check_tbc: bool = True,
):
    """
    After opt_type `completed_ot` finishes ALL its rounds, apply skip rules to
    ALL pending rows of `next_types` in working_table. This ensures:
      - TBC/TBL OPTs for stores that RL already filled to MJ_REQ are SKIPPED
        before those types start, not discovered mid-waterfall.
      - MJ_REQ_REM / MSA_FNL_Q_REM / PRI_CT_REM already reflect the completed
        type's deductions (updated by per-band revalidation), so the cross-type
        check uses current values.
      - ALLOC_REMARKS record which opt_type's completion triggered each skip.
    """
    work_cols = {c.upper() for c in _cols(conn, working_table)}

    # Build the IN list for next types
    next_in = ", ".join(f"'{t}'" for t in next_types)

    # PRI gate enforcement list for the skip check
    enforced = ["'TBL'"]
    if pri_ct_check_rl:  enforced.append("'RL'")
    if pri_ct_check_tbc: enforced.append("'TBC'")
    pri_opt_in = ", ".join(enforced)

    # Apply MSA_EXHAUSTED and PRI_BROKEN to all pending next-type rows
    _run(conn, f"""
        UPDATE [{working_table}] WITH (ROWLOCK, UPDLOCK) SET
            ALLOC_STATUS = CASE
                WHEN ISNULL(MSA_FNL_Q_REM, 0) <= 0 THEN 'SKIPPED'
                WHEN ISNULL(PRI_CT_REM, 0) < 100
                     AND ISNULL(OPT_TYPE,'') IN ({pri_opt_in}) THEN 'SKIPPED'
                ELSE ALLOC_STATUS END,
            ALLOC_REMARKS = CASE
                WHEN ISNULL(MSA_FNL_Q_REM, 0) <= 0
                    THEN ISNULL(ALLOC_REMARKS,'')
                         + ' CROSS_SKIP_{completed_ot}_MSA(rem='
                         + CAST(ISNULL(MSA_FNL_Q_REM,0) AS NVARCHAR(20)) + ');'
                WHEN ISNULL(PRI_CT_REM, 0) < 100
                     AND ISNULL(OPT_TYPE,'') IN ({pri_opt_in})
                    THEN ISNULL(ALLOC_REMARKS,'')
                         + ' CROSS_SKIP_{completed_ot}_PRI(pri_ct='
                         + CAST(ISNULL(PRI_CT_REM,0) AS NVARCHAR(20)) + '%);'
                ELSE ALLOC_REMARKS END
        WHERE LISTED_FLAG = 1
          AND ISNULL(OPT_TYPE,'') IN ({next_in})
          AND ISNULL(ALLOC_STATUS,'PENDING') NOT IN ('SKIPPED','ALLOCATED')
    """)

    # Store-broken across types: MJ_REQ_REM < factor × ACS_D after completed type
    if ENABLE_STORE_BROKEN and "MJ_REQ_REM" in work_cols:
        _run(conn, f"""
            UPDATE [{working_table}] WITH (ROWLOCK, UPDLOCK) SET
                ALLOC_STATUS = 'SKIPPED',
                ALLOC_REMARKS = ISNULL(ALLOC_REMARKS,'')
                    + ' CROSS_SKIP_{completed_ot}_STORE_BROKEN(req_rem='
                    + CAST(ISNULL(MJ_REQ_REM,0) AS NVARCHAR(20))
                    + ',acs_d=' + CAST(ISNULL(ACS_D,0) AS NVARCHAR(20)) + ');'
            WHERE LISTED_FLAG = 1
              AND ISNULL(OPT_TYPE,'') IN ({next_in})
              AND ISNULL(ALLOC_STATUS,'PENDING') NOT IN ('SKIPPED','ALLOCATED')
              AND ISNULL(MJ_REQ_REM, 0) < {ACS_SKIP_FACTOR} * ISNULL(ACS_D, 0)
        """)

    # Propagate SKIP to alloc_table
    _run(conn, f"""
        UPDATE A WITH (ROWLOCK, UPDLOCK) SET
            A.ALLOC_STATUS = 'SKIPPED',
            A.SKIP_REASON  = CASE
                WHEN A.SKIP_REASON IS NULL OR A.SKIP_REASON = ''
                    THEN LTRIM(ISNULL(W.ALLOC_REMARKS,''))
                ELSE A.SKIP_REASON END
        FROM [{alloc_table}] A
        INNER JOIN [{working_table}] W
            ON A.WERKS=W.WERKS AND A.MAJ_CAT=W.MAJ_CAT
           AND A.GEN_ART_NUMBER=W.GEN_ART_NUMBER
           AND ISNULL(A.CLR,'') = ISNULL(W.CLR,'')
        WHERE W.ALLOC_STATUS = 'SKIPPED'
          AND W.OPT_TYPE IN ({next_in})
          AND ISNULL(A.ALLOC_STATUS,'PENDING') NOT IN ('SKIPPED','ALLOCATED','PARTIAL')
    """)

    skipped = conn.execute(text(f"""
        SELECT COUNT(*) FROM [{working_table}]
        WHERE OPT_TYPE IN ({next_in})
          AND ALLOC_STATUS = 'SKIPPED'
          AND ALLOC_REMARKS LIKE '%CROSS_SKIP_{completed_ot}%'
    """)).scalar()
    if int(skipped or 0) > 0:
        logger.info(f"[C] cross-type after {completed_ot}: {skipped} rows pre-skipped in {next_in}")


def _stage_c_waterfall(conn, alloc_table, working_table=None, grids=None,
                        pri_ct_check_rl: bool = True,
                        pri_ct_check_tbc: bool = True,
                        size_threshold: float = 0.6,
                        min_size_count: int = 3,
                        opt_types: Optional[List[str]] = None,
                        rl_cap_pct: float = 110.0,
                        tbc_cap_pct: float = 110.0):
    """
    For each (OPT_TYPE, round r, rank band) — run one batch SQL that:
      1) computes need_pool / need_ship per eligible row,
      2) orders rows inside each pool key by (rank, ST_RANK),
      3) takes a cumulative window to see what fraction of FNL_Q_REM each
         row can claim without overdraft,
      4) commits SHIP_QTY / HOLD_QTY, decrements the pool,
      5) if ENABLE_PER_OPT_REVALIDATION + working_table + grids are provided,
         revalidates grid REQs / PRI_CT_REM / MSA_FNL_Q_REM on working_table
         and propagates SKIP to alloc_table before the next band.
    """
    # Make sure ALLOC_REMARKS exists — the finalise UPDATE below writes NO_REQ
    # / NO_POOL_MSA audit detail into it, and older alloc tables predate the column.
    _ensure_alloc_remarks_col(conn, alloc_table)

    # per-opt_type bounds: scan only the rank range that actually belongs
    # to this opt_type (ranks are global; TBL's ranks start after RL+TBC).
    active_types = [ot for ot in OPT_TYPE_ORDER if not opt_types or ot in opt_types]
    for ot in active_types:
        bounds = conn.execute(text(f"""
            SELECT ISNULL(MAX(I_ROD), 0),
                   ISNULL(MIN(OPT_PRIORITY_RANK), 0),
                   ISNULL(MAX(OPT_PRIORITY_RANK), 0)
            FROM [{alloc_table}] WHERE OPT_TYPE = :ot
        """), {"ot": ot}).fetchone()
        max_round = int(bounds[0] or 0)
        min_rank  = int(bounds[1] or 0)
        max_rank  = int(bounds[2] or 0)
        if max_round == 0 or max_rank == 0 or min_rank == 0:
            continue
        logger.info(f"[C] opt_type={ot} rounds={max_round} ranks={min_rank}..{max_rank}")
        ot_start = time.time()

        for r in range(1, max_round + 1):
            # Tighten rank window to rows whose I_ROD allows this round.
            rb = conn.execute(text(f"""
                SELECT ISNULL(MIN(OPT_PRIORITY_RANK), 0),
                       ISNULL(MAX(OPT_PRIORITY_RANK), 0)
                FROM [{alloc_table}]
                WHERE OPT_TYPE = :ot AND ISNULL(I_ROD, 1) >= :r
            """), {"ot": ot, "r": r}).fetchone()
            r_min = int(rb[0] or 0)
            r_max = int(rb[1] or 0)
            if r_min == 0 or r_max == 0:
                continue
            # Reset per-round deltas for the whole opt_type once.
            _run(conn, f"""
                UPDATE [{alloc_table}] SET ROUND_SHIP = 0, ROUND_HOLD = 0
                WHERE OPT_TYPE = :ot
            """, {"ot": ot})
            band_start = r_min
            while band_start <= r_max:
                band_end = band_start + BAND_SIZE - 1
                _stage_c_run_band(conn, alloc_table, ot, r, band_start, band_end)
                if ENABLE_PER_OPT_REVALIDATION and working_table and grids:
                    _revalidate_after_band(
                        conn, working_table, alloc_table,
                        ot, band_start, band_end, grids,
                        pri_ct_check_rl=pri_ct_check_rl,
                        pri_ct_check_tbc=pri_ct_check_tbc,
                    )
                band_start = band_end + 1

        s = conn.execute(text(f"""
            SELECT COUNT(*),
                   ISNULL(SUM(SHIP_QTY),0), ISNULL(SUM(HOLD_QTY),0),
                   SUM(CASE WHEN SHIP_QTY>0 OR HOLD_QTY>0 THEN 1 ELSE 0 END)
            FROM [{alloc_table}] WHERE OPT_TYPE = :ot
        """), {"ot": ot}).fetchone()
        logger.info(
            f"[C] {ot} done in {round(time.time()-ot_start,1)}s — "
            f"rows={s[0]}, ship={float(s[1] or 0):.0f}, "
            f"hold={float(s[2] or 0):.0f}, filled_rows={s[3]}"
        )

        # Cross-type revalidation: after this opt_type finishes ALL its rounds,
        # evaluate skip rules against ALL pending rows of subsequent opt_types
        # in working_table. This prevents TBC/TBL from allocating to stores that
        # RL already brought to MJ_REQ, or to OPTs whose pool is exhausted.
        next_types = OPT_TYPE_ORDER[OPT_TYPE_ORDER.index(ot) + 1:]
        if next_types and working_table and grids and ENABLE_PER_OPT_REVALIDATION:
            _revalidate_cross_type(
                conn, working_table, alloc_table,
                completed_ot=ot, next_types=next_types, grids=grids,
                pri_ct_check_rl=pri_ct_check_rl,
                pri_ct_check_tbc=pri_ct_check_tbc,
            )
        # Skip OPTs whose live size coverage dropped below threshold, then
        # re-rank surviving rows using live SIZE_RATIO before their bands start.
        if next_types:
            for next_ot in next_types:
                _rerank_for_next_opt_type(
                    conn, alloc_table, next_ot,
                    size_threshold=size_threshold,
                    min_size_count=min_size_count,
                )
        # R09 re-evaluation — May 2026. After this OPT_TYPE shipped its bands,
        # skip any PENDING row in upcoming OPT_TYPEs whose
        #   (cap_pct × MJ_MBQ) − MJ_STK_TTL − Σ_ALLOC_QTY(WERKS, MAJ_CAT)
        # has dropped below 0.5 × ACS_D. LISTED_FLAG stays 1 — only the alloc
        # row gets ALLOC_STATUS='SKIPPED', SKIP_REASON='R09_HEADROOM_TRIVIAL'.
        if next_types and working_table:
            _check_r09_eligibility(
                conn, working_table, alloc_table,
                completed_opt_type=ot, next_opt_types=next_types,
                rl_cap_pct=rl_cap_pct,
                tbc_cap_pct=tbc_cap_pct,
            )

    # No global MJ_REQ cap. Per-OPT MBQ-caps (rl/tbc_mbq_cap_pct) are the only
    # MAJ_CAT-level ceilings — applied at MJ_MBQ level per OPT_TYPE inside
    # _stage_c_apply_mbq_cap. They are independent (do NOT sum against a
    # global MAJ_CAT cap), so SUM(SHIP) per (WERKS, MAJ_CAT) can exceed
    # original MJ_REQ when any slider > 100%. TBL has no MJ-cap; its only
    # ceiling is per-size SZ_REQ.

    # Finalise: copy SHIP_QTY to ALLOC_QTY
    _stage_d_apply_pak_sz_rounding(conn, alloc_table)
    _run(conn, f"UPDATE [{alloc_table}] SET ALLOC_QTY = SHIP_QTY")
    # Classify final status.
    # Target: TBL → SZ_MBQ_WH + (I_ROD-1)*SZ_MBQ - SZ_STK (hold buffer, counted once)
    #         RL/TBC → I_ROD * SZ_MBQ - SZ_STK             (no hold buffer)
    _run(conn, f"""
        UPDATE [{alloc_table}] SET
            ALLOC_STATUS = CASE
                WHEN SHIP_QTY + HOLD_QTY > 0
                     AND SHIP_QTY + HOLD_QTY >= CASE
                         WHEN OPT_TYPE='TBL'
                         THEN CASE WHEN ISNULL(SZ_MBQ_WH,0)
                                        + (ISNULL(I_ROD,1)-1)*ISNULL(SZ_MBQ,0)
                                        - ISNULL(SZ_STK,0) > 0
                                   THEN ISNULL(SZ_MBQ_WH,0)
                                        + (ISNULL(I_ROD,1)-1)*ISNULL(SZ_MBQ,0)
                                        - ISNULL(SZ_STK,0)
                                   ELSE 0 END
                         ELSE CASE WHEN ISNULL(I_ROD,1)*ISNULL(SZ_MBQ,0)
                                        - ISNULL(SZ_STK,0) > 0
                                   THEN ISNULL(I_ROD,1)*ISNULL(SZ_MBQ,0)
                                        - ISNULL(SZ_STK,0)
                                   ELSE 0 END
                         END
                     THEN 'ALLOCATED'
                WHEN SHIP_QTY > 0                 THEN 'PARTIAL'
                ELSE 'SKIPPED' END,
            SKIP_REASON = CASE
                -- Preserve any cap / cross-skip / R09 reason already written by
                -- the waterfall or post-waterfall caps. Without this guard, the
                -- three NO_REQ / NO_POOL_MSA / ALREADY_STOCKED branches below
                -- would clobber MBQ_CAP_<ot>, SEC_CAP_<grid>, MJ_REQ_CAP,
                -- R09_HEADROOM_TRIVIAL, and CROSS_SKIP_<ot>_* reasons.
                WHEN ISNULL(SKIP_REASON,'') <> '' THEN SKIP_REASON
                WHEN SHIP_QTY = 0 AND HOLD_QTY = 0
                     AND CASE WHEN OPT_TYPE='TBL'
                              THEN ISNULL(SZ_MBQ_WH,0)+(ISNULL(I_ROD,1)-1)*ISNULL(SZ_MBQ,0)
                              ELSE ISNULL(I_ROD,1)*ISNULL(SZ_MBQ,0) END
                          - ISNULL(SZ_STK,0) <= 0
                     THEN 'ALREADY_STOCKED'
                -- Split NO_POOL_OR_DEMAND into the two distinct causes so users
                -- can tell why the row got nothing: did the store not need stock
                -- (SZ_REQ<=0 → NO_REQ) or was the MSA pool empty at this size
                -- (had demand, nothing left to draw → NO_POOL_MSA).
                WHEN SHIP_QTY = 0 AND HOLD_QTY = 0
                     AND ISNULL(SZ_REQ, 0) <= 0 THEN 'NO_REQ'
                WHEN SHIP_QTY = 0 AND HOLD_QTY = 0 THEN 'NO_POOL_MSA'
                ELSE SKIP_REASON END,
            ALLOC_REMARKS = CASE
                WHEN SHIP_QTY = 0 AND HOLD_QTY = 0
                     AND ISNULL(SZ_REQ, 0) <= 0
                     AND CASE WHEN OPT_TYPE='TBL'
                              THEN ISNULL(SZ_MBQ_WH,0)+(ISNULL(I_ROD,1)-1)*ISNULL(SZ_MBQ,0)
                              ELSE ISNULL(I_ROD,1)*ISNULL(SZ_MBQ,0) END
                          - ISNULL(SZ_STK,0) > 0
                     THEN ISNULL(ALLOC_REMARKS,'')
                          + ' NO_REQ(SZ_REQ=' + CAST(ISNULL(SZ_REQ,0) AS NVARCHAR(20))
                          + ', SZ_STK=' + CAST(ISNULL(SZ_STK,0) AS NVARCHAR(20))
                          + ', SZ_MBQ=' + CAST(ISNULL(SZ_MBQ,0) AS NVARCHAR(20))
                          + ', OPT_MBQ=' + CAST(ISNULL(OPT_MBQ,0) AS NVARCHAR(20)) + ');'
                WHEN SHIP_QTY = 0 AND HOLD_QTY = 0
                     AND ISNULL(SZ_REQ, 0) > 0
                     AND CASE WHEN OPT_TYPE='TBL'
                              THEN ISNULL(SZ_MBQ_WH,0)+(ISNULL(I_ROD,1)-1)*ISNULL(SZ_MBQ,0)
                              ELSE ISNULL(I_ROD,1)*ISNULL(SZ_MBQ,0) END
                          - ISNULL(SZ_STK,0) > 0
                     THEN ISNULL(ALLOC_REMARKS,'')
                          + ' NO_POOL_MSA(SZ_REQ=' + CAST(ISNULL(SZ_REQ,0) AS NVARCHAR(20))
                          + ', SZ_MBQ=' + CAST(ISNULL(SZ_MBQ,0) AS NVARCHAR(20))
                          + ', FNL_Q='     + CAST(ISNULL(FNL_Q,0)     AS NVARCHAR(20))
                          + ', FNL_Q_REM=' + CAST(ISNULL(FNL_Q_REM,0) AS NVARCHAR(20)) + ');'
                ELSE ALLOC_REMARKS END
    """)


def _stage_c_run_band(conn, alloc_table, opt_type, r, band_start, band_end,
                       maj_cat: Optional[str] = None):
    """
    One rank-band × one round × one opt_type.

    Two-statement sequence (ROUND_SHIP/ROUND_HOLD are pre-zeroed per round
    by the outer waterfall loop, so no per-band reset is needed):

      Step 1 — cumulative-window UPDATE: take pool by priority × ST_RANK.
      Step 2 — decrement #nre_pool by ROUND_SHIP + ROUND_HOLD for this band.

    maj_cat: when set, both statements are filtered to that single MAJ_CAT.
    Used by the parallel orchestrator. None preserves original behaviour.
    """
    params = {"ot": opt_type, "bs": band_start, "be": band_end, "r": r}
    mc_pred = ""
    if maj_cat is not None:
        params["mc"] = maj_cat
        mc_pred = " AND A.MAJ_CAT = :mc"

    # Step 1 — compute take_pool per row, write SHIP_QTY / HOLD_QTY deltas.
    _run(conn, f"""
        ;WITH Target AS (
            SELECT A.WERKS, A.RDC, A.MAJ_CAT, A.GEN_ART_NUMBER, A.CLR,
                   A.VAR_ART, A.SZ,
                   A.OPT_PRIORITY_RANK, A.ST_RANK, A.IS_NEW,
                   /* need_pool rules:
                      TBL — hold buffer counted ONCE across all rounds (all TBL, not just
                        IS_NEW=1, since TBL is pre-classified based on IS_NEW).
                        Cumulative target = SZ_MBQ_WH + (r-1)*SZ_MBQ.
                        Pure-HOLD guard: suppress when no ship demand remains.
                      RL/TBC — pool demand = ship demand (no hold buffer). */
                   CASE
                        WHEN :ot = 'TBL'
                             AND (:r * ISNULL(A.SZ_MBQ,0) - ISNULL(A.SZ_STK,0)
                                  - ISNULL(A.SHIP_QTY,0)) <= 0
                        THEN 0
                        WHEN :ot = 'TBL'
                             AND ISNULL(A.SZ_MBQ_WH,0) + :r*ISNULL(A.SZ_MBQ,0)
                                 - ISNULL(A.SZ_MBQ,0)
                                 > ISNULL(A.SZ_STK,0) + ISNULL(A.POOL_CONSUMED,0)
                        THEN ISNULL(A.SZ_MBQ_WH,0) + :r*ISNULL(A.SZ_MBQ,0)
                             - ISNULL(A.SZ_MBQ,0)
                             - ISNULL(A.SZ_STK,0) - ISNULL(A.POOL_CONSUMED,0)
                        WHEN :r * ISNULL(A.SZ_MBQ,0) - ISNULL(A.SZ_STK,0)
                                > ISNULL(A.POOL_CONSUMED,0)
                        THEN :r * ISNULL(A.SZ_MBQ,0) - ISNULL(A.SZ_STK,0)
                           - ISNULL(A.POOL_CONSUMED,0)
                        ELSE 0 END AS need_pool,
                   CASE WHEN :r * ISNULL(A.SZ_MBQ,0) - ISNULL(A.SZ_STK,0)
                           > ISNULL(A.SHIP_QTY,0)
                        THEN :r * ISNULL(A.SZ_MBQ,0) - ISNULL(A.SZ_STK,0)
                           - ISNULL(A.SHIP_QTY,0)
                        ELSE 0 END AS need_ship
            FROM [{alloc_table}] A
            WHERE A.OPT_TYPE = :ot
              AND A.OPT_PRIORITY_RANK BETWEEN :bs AND :be
              AND ISNULL(A.ALLOC_STATUS,'PENDING') NOT IN ('SKIPPED','INELIGIBLE')
              AND ISNULL(A.I_ROD, 1) >= :r
              {mc_pred}
        ),
        Ranked AS (
            SELECT T.*, P.FNL_Q_REM,
                   ROW_NUMBER() OVER (
                       PARTITION BY T.RDC, T.MAJ_CAT, T.GEN_ART_NUMBER, T.CLR, T.VAR_ART, T.SZ
                       ORDER BY ISNULL(T.ST_RANK,999999) ASC,
                                T.OPT_PRIORITY_RANK     ASC,
                                T.WERKS                 ASC
                   ) AS ord
            FROM Target T
            INNER JOIN {POOL_TABLE} P
                ON P.RDC = T.RDC AND P.MAJ_CAT = T.MAJ_CAT
               AND P.GEN_ART_NUMBER = T.GEN_ART_NUMBER
               AND ISNULL(P.CLR,'') = ISNULL(T.CLR,'')
               AND P.VAR_ART = T.VAR_ART AND P.SZ = T.SZ
            WHERE T.need_pool > 0 AND P.FNL_Q_REM > 0
        ),
        Cum AS (
            SELECT *,
                   SUM(need_pool) OVER (
                       PARTITION BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ
                       ORDER BY ord ROWS UNBOUNDED PRECEDING
                   ) AS cum_demand
            FROM Ranked
        ),
        Take AS (
            SELECT *,
                   CASE
                       WHEN FNL_Q_REM - (cum_demand - need_pool) <= 0 THEN 0
                       WHEN FNL_Q_REM - (cum_demand - need_pool) >= need_pool THEN need_pool
                       ELSE FNL_Q_REM - (cum_demand - need_pool)
                   END AS take_pool
            FROM Cum
        )
        UPDATE A SET
            A.POOL_CONSUMED = ISNULL(A.POOL_CONSUMED,0) + X.take_pool,
            /* All TBL: split at need_ship; excess to warehouse HOLD. RL/TBC 100% SHIP. */
            A.ROUND_SHIP    = CASE WHEN :ot = 'TBL'
                                   THEN CASE WHEN X.take_pool < X.need_ship
                                             THEN X.take_pool ELSE X.need_ship END
                                   ELSE X.take_pool END,
            A.ROUND_HOLD    = CASE WHEN :ot = 'TBL'
                                   THEN X.take_pool - CASE WHEN X.take_pool < X.need_ship
                                                           THEN X.take_pool ELSE X.need_ship END
                                   ELSE 0 END,
            A.SHIP_QTY      = ISNULL(A.SHIP_QTY,0) +
                              CASE WHEN :ot = 'TBL'
                                   THEN CASE WHEN X.take_pool < X.need_ship
                                             THEN X.take_pool ELSE X.need_ship END
                                   ELSE X.take_pool END,
            A.HOLD_QTY      = ISNULL(A.HOLD_QTY,0) +
                              CASE WHEN :ot = 'TBL'
                                   THEN X.take_pool - CASE WHEN X.take_pool < X.need_ship
                                                           THEN X.take_pool ELSE X.need_ship END
                                   ELSE 0 END,
            A.ALLOC_WAVE    = CONCAT(:ot, '_R', :r),
            A.ALLOC_ROUND   = :r,
            /* ALLOC_STATUS target:
               TBL → SZ_MBQ_WH + (I_ROD-1)×SZ_MBQ − SZ_STK  (hold counted once)
               All others   → I_ROD × SZ_MBQ − SZ_STK                  (no hold buffer) */
            A.ALLOC_STATUS  = CASE
                WHEN ISNULL(A.POOL_CONSUMED,0) + X.take_pool >= CASE
                     WHEN :ot = 'TBL' THEN
                          CASE WHEN ISNULL(A.SZ_MBQ_WH,0)
                                    + ISNULL(A.I_ROD,1)*ISNULL(A.SZ_MBQ,0)
                                    - ISNULL(A.SZ_MBQ,0) - ISNULL(A.SZ_STK,0) > 0
                               THEN ISNULL(A.SZ_MBQ_WH,0)
                                    + ISNULL(A.I_ROD,1)*ISNULL(A.SZ_MBQ,0)
                                    - ISNULL(A.SZ_MBQ,0) - ISNULL(A.SZ_STK,0)
                               ELSE 0 END
                     ELSE CASE WHEN ISNULL(A.I_ROD,1)*ISNULL(A.SZ_MBQ,0)
                                    - ISNULL(A.SZ_STK,0) > 0
                               THEN ISNULL(A.I_ROD,1)*ISNULL(A.SZ_MBQ,0)
                                    - ISNULL(A.SZ_STK,0)
                               ELSE 0 END
                     END
                THEN 'ALLOCATED'
                ELSE 'PARTIAL' END
        FROM [{alloc_table}] A WITH (ROWLOCK, UPDLOCK)
        INNER JOIN Take X
            ON A.WERKS = X.WERKS AND A.RDC = X.RDC
           AND A.MAJ_CAT = X.MAJ_CAT AND A.GEN_ART_NUMBER = X.GEN_ART_NUMBER
           AND ISNULL(A.CLR,'') = ISNULL(X.CLR,'')
           AND A.VAR_ART = X.VAR_ART AND A.SZ = X.SZ
        WHERE X.take_pool > 0
    """, params)

    # Step 2 — decrement pool by this band's total take (ROUND_SHIP+ROUND_HOLD).
    mc_pred2 = " AND MAJ_CAT = :mc" if maj_cat is not None else ""
    _run(conn, f"""
        ;WITH S AS (
            SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
                   SUM(ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0)) AS taken
            FROM [{alloc_table}]
            WHERE OPT_TYPE = :ot
              AND OPT_PRIORITY_RANK BETWEEN :bs AND :be
              AND ALLOC_ROUND = :r
              {mc_pred2}
            GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ
            HAVING SUM(ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0)) > 0
        )
        UPDATE P SET P.FNL_Q_REM = P.FNL_Q_REM - S.taken
        FROM {POOL_TABLE} P
        INNER JOIN S
            ON P.RDC = S.RDC AND P.MAJ_CAT = S.MAJ_CAT
           AND P.GEN_ART_NUMBER = S.GEN_ART_NUMBER
           AND ISNULL(P.CLR,'') = ISNULL(S.CLR,'')
           AND P.VAR_ART = S.VAR_ART AND P.SZ = S.SZ
    """, params)

    # Step 3 — Audit-trail append (Option A). For every alloc row that
    # actually moved this band, append a compact per-band entry to
    # ALLOC_REMARKS so the reviewer can read the full lifecycle of an OPT
    # (one entry per round it took stock) directly on the alloc row.
    # Format: ` B[ot.rN.rkN] sh=## hld=## pool=##->##;`
    # pool=before->after is read from #nre_pool AFTER step 2 decrement, so
    # `before = FNL_Q_REM + this band's taken`, `after = FNL_Q_REM`.
    _run(conn, f"""
        ;WITH S AS (
            SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
                   SUM(ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0)) AS taken
            FROM [{alloc_table}]
            WHERE OPT_TYPE = :ot
              AND OPT_PRIORITY_RANK BETWEEN :bs AND :be
              AND ALLOC_ROUND = :r
              {mc_pred2}
            GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ
            HAVING SUM(ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0)) > 0
        )
        UPDATE A SET
            A.ALLOC_REMARKS = ISNULL(A.ALLOC_REMARKS, '')
                + ' B[' + :ot + '.r' + CAST(:r AS NVARCHAR(10))
                + '.rk' + CAST(ISNULL(A.OPT_PRIORITY_RANK, 0) AS NVARCHAR(10))
                + '] sh=' + CAST(CAST(ISNULL(A.ROUND_SHIP, 0) AS INT) AS NVARCHAR(20))
                + ' hld=' + CAST(CAST(ISNULL(A.ROUND_HOLD, 0) AS INT) AS NVARCHAR(20))
                + ' pool=' + CAST(CAST(ISNULL(P.FNL_Q_REM, 0) + S.taken AS INT) AS NVARCHAR(20))
                + '->' + CAST(CAST(ISNULL(P.FNL_Q_REM, 0) AS INT) AS NVARCHAR(20))
                + ';'
        FROM [{alloc_table}] A
        INNER JOIN S
            ON A.RDC = S.RDC AND A.MAJ_CAT = S.MAJ_CAT
           AND A.GEN_ART_NUMBER = S.GEN_ART_NUMBER
           AND ISNULL(A.CLR,'') = ISNULL(S.CLR,'')
           AND A.VAR_ART = S.VAR_ART AND A.SZ = S.SZ
        INNER JOIN {POOL_TABLE} P
            ON P.RDC = S.RDC AND P.MAJ_CAT = S.MAJ_CAT
           AND P.GEN_ART_NUMBER = S.GEN_ART_NUMBER
           AND ISNULL(P.CLR,'') = ISNULL(S.CLR,'')
           AND P.VAR_ART = S.VAR_ART AND P.SZ = S.SZ
        WHERE A.OPT_TYPE = :ot
          AND A.OPT_PRIORITY_RANK BETWEEN :bs AND :be
          AND A.ALLOC_ROUND = :r
          AND (ISNULL(A.ROUND_SHIP, 0) + ISNULL(A.ROUND_HOLD, 0)) > 0
          {mc_pred2.replace('MAJ_CAT', 'A.MAJ_CAT')}
    """, params)


def _stage_d_apply_pak_sz_rounding(conn, alloc_table: str) -> None:
    """Final per-row PAK_SZ rounding.

    Half-up rule: SHIP_QTY rounds to the nearest whole pak. req >= 0.5*pak
    rounds UP (e.g. 5/6 -> 6, 11/6 -> 12); below the half-pak threshold the
    row is gated to 0 and marked SKIPPED. POOL_CONSUMED is adjusted by the
    same delta so the downstream FNL_Q_REM recompute refunds (or charges)
    the pool correctly. Applies to all OPT_TYPEs uniformly."""
    try:
        _ensure_alloc_remarks_col(conn, alloc_table)
    except Exception as e:
        logger.warning(f"[D] PAK_SZ ensure ALLOC_REMARKS col failed: {e}")
    try:
        _run(conn, f"""
            ;WITH P AS (
                SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
                       ISNULL(SHIP_QTY, 0)                       AS old_ship,
                       COALESCE(NULLIF(PAK_SZ, 0), 1)            AS pak,
                       ISNULL(SHIP_QTY, 0)                      AS req
                FROM [{alloc_table}]
                WHERE ISNULL(SHIP_QTY, 0) > 0
            ),
            R AS (
                SELECT P.*,
                       CASE WHEN P.req < 0.5 * P.pak THEN 0
                            ELSE CAST(FLOOR((CAST(P.req AS FLOAT) + 0.5 * CAST(P.pak AS FLOAT))
                                            / CAST(P.pak AS FLOAT)) AS INT) * P.pak
                       END AS new_ship
                FROM P
            )
            UPDATE A
               SET A.SHIP_QTY      = R.new_ship,
                   A.POOL_CONSUMED = CASE
                       WHEN A.POOL_CONSUMED IS NULL THEN NULL
                       ELSE CASE WHEN A.POOL_CONSUMED - (R.old_ship - R.new_ship) < 0
                                 THEN 0
                                 ELSE A.POOL_CONSUMED - (R.old_ship - R.new_ship) END
                   END,
                   A.ALLOC_STATUS  = CASE WHEN R.new_ship = 0 THEN 'SKIPPED' ELSE A.ALLOC_STATUS END,
                   A.SKIP_REASON   = CASE WHEN R.new_ship = 0
                                          THEN 'PAK_SZ_BELOW_HALF(pak=' + CAST(R.pak AS NVARCHAR(10)) + ')'
                                          ELSE A.SKIP_REASON END,
                   A.ALLOC_REMARKS = ISNULL(A.ALLOC_REMARKS,'') +
                       CASE
                           WHEN R.new_ship = 0
                                THEN ' PAK_SZ_GATE(req=' + CAST(R.req AS NVARCHAR(20))
                                     + ',pak=' + CAST(R.pak AS NVARCHAR(10)) + ');'
                           WHEN R.new_ship <> R.old_ship
                                THEN ' PAK_SZ_ROUND(from=' + CAST(R.old_ship AS NVARCHAR(20))
                                     + ',to=' + CAST(R.new_ship AS NVARCHAR(20))
                                     + ',pak=' + CAST(R.pak AS NVARCHAR(10)) + ');'
                           ELSE ''
                       END
            FROM [{alloc_table}] A
            INNER JOIN R
              ON A.WERKS = R.WERKS AND A.MAJ_CAT = R.MAJ_CAT
             AND A.GEN_ART_NUMBER = R.GEN_ART_NUMBER
             AND ISNULL(A.CLR,'') = ISNULL(R.CLR,'')
             AND A.VAR_ART = R.VAR_ART AND A.SZ = R.SZ
        """)
        logger.info("[D] PAK_SZ rounding applied")
    except Exception as e:
        logger.warning(f"[D] PAK_SZ rounding failed: {e}")


# ───────────────────────────────────────────────────────────────
# STAGE D — REFLECT & AUDIT
# ───────────────────────────────────────────────────────────────
def _stage_d_reflect(conn, working_table, alloc_table):
    # ALLOC_SEQ is generated at OPT-grain from ARS_LISTING_WORKING so that
    # the sequence represents execution order of OPTs (one number per store×OPT
    # pair), not size rows.  ALLOC_ROUND lives at size-grain in alloc_table, so
    # we aggregate it (MIN) first, then use it in the ROW_NUMBER ordering on
    # working_table. The resulting seq is then propagated back to every size
    # row in alloc_table by a join on OPT keys.
    #
    # ORDER: OPT_TYPE → first_alloc_round → ST_RANK → OPT_PRIORITY_RANK
    #   - Round-first so round-1 OPTs precede round-2 OPTs regardless of store.
    #   - ST_RANK within a round so ST_RANK=1 store is fully listed first.
    #   - OPT_PRIORITY_RANK as final tie-break within a store.
    try:
        _run(conn, f"""
            ;WITH Agg AS (
                -- Aggregate alloc_table to OPT grain. MIN(ALLOC_ROUND) gives
                -- the earliest round this OPT was allocated (0 when no alloc).
                SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR,
                       SUM(ISNULL(SHIP_QTY, 0))  AS ship_q,
                       SUM(ISNULL(HOLD_QTY, 0))  AS hold_q,
                       COUNT(*)                   AS sz_rows,
                       SUM(CASE WHEN ISNULL(SHIP_QTY,0) + ISNULL(HOLD_QTY,0) > 0
                                THEN 1 ELSE 0 END) AS filled_rows,
                       MIN(ISNULL(ALLOC_ROUND, 0)) AS first_round
                FROM [{alloc_table}]
                GROUP BY WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR
            ),
            Seq AS (
                -- Sequence OPTs using listing-grain attributes (ST_RANK,
                -- OPT_PRIORITY_RANK, OPT_TYPE live in working_table) plus
                -- the aggregated first_round from alloc_table.
                SELECT W.WERKS, W.MAJ_CAT, W.GEN_ART_NUMBER,
                       ISNULL(W.CLR, '') AS CLR,
                       ROW_NUMBER() OVER (
                           PARTITION BY W.MAJ_CAT
                           ORDER BY
                               CASE W.OPT_TYPE WHEN 'RL' THEN 1 WHEN 'TBC' THEN 2 ELSE 3 END,
                               ISNULL(A.first_round, 0),
                               ISNULL(W.ST_RANK, 999999),
                               ISNULL(W.OPT_PRIORITY_RANK, 999999)
                       ) AS seq,
                       A.ship_q, A.hold_q, A.sz_rows, A.filled_rows
                FROM [{working_table}] W
                LEFT JOIN Agg A
                    ON  A.WERKS           = W.WERKS
                    AND A.MAJ_CAT         = W.MAJ_CAT
                    AND A.GEN_ART_NUMBER  = W.GEN_ART_NUMBER
                    AND ISNULL(A.CLR,'')  = ISNULL(W.CLR,'')
                WHERE W.LISTED_FLAG = 1
            )
            UPDATE W SET
                W.ALLOC_SEQ    = S.seq,
                W.ALLOC_QTY    = ISNULL(S.ship_q, 0),
                W.HOLD_QTY     = ISNULL(S.hold_q, 0),
                W.ALLOC_STATUS = CASE
                    WHEN ISNULL(S.ship_q,0) + ISNULL(S.hold_q,0) = 0
                         THEN 'NOT_ALLOCATED'
                    WHEN ISNULL(S.filled_rows,0) < ISNULL(S.sz_rows,0)
                         THEN 'PARTIAL'
                    ELSE 'ALLOCATED' END,
                W.ALLOC_REMARKS = CONCAT(
                    -- Preserve any skip/revalidation remarks written during bands
                    CASE WHEN LEN(LTRIM(ISNULL(W.ALLOC_REMARKS,''))) > 0
                         THEN LTRIM(W.ALLOC_REMARKS) + ' | '
                         ELSE '' END,
                    'ship=',  CAST(ISNULL(S.ship_q,  0) AS NVARCHAR(20)),
                    '; hold=', CAST(ISNULL(S.hold_q,  0) AS NVARCHAR(20)),
                    '; sizes=',CAST(ISNULL(S.filled_rows,0) AS NVARCHAR(10)),
                    '/',       CAST(ISNULL(S.sz_rows,  0) AS NVARCHAR(10)),
                    '; seq=',  CAST(S.seq AS NVARCHAR(10)))
            FROM [{working_table}] W
            INNER JOIN Seq S
                ON  S.WERKS          = W.WERKS
                AND S.MAJ_CAT        = W.MAJ_CAT
                AND S.GEN_ART_NUMBER = W.GEN_ART_NUMBER
                AND S.CLR            = ISNULL(W.CLR, '')
        """)
    except Exception as e:
        logger.warning(f"[D] ALLOC_SEQ stamp failed: {e}")

    # Propagate the OPT-grain ALLOC_SEQ from working_table down to every size
    # row in alloc_table. All sizes of the same OPT share one number.
    try:
        _run(conn, f"""
            UPDATE A SET A.ALLOC_SEQ = W.ALLOC_SEQ
            FROM [{alloc_table}] A
            INNER JOIN [{working_table}] W
                ON  W.WERKS          = A.WERKS
                AND W.MAJ_CAT        = A.MAJ_CAT
                AND W.GEN_ART_NUMBER = A.GEN_ART_NUMBER
                AND ISNULL(W.CLR,'') = ISNULL(A.CLR,'')
        """)
    except Exception as e:
        logger.warning(f"[D] ALLOC_SEQ propagate to alloc_table failed: {e}")

    # ── Roll up ALLOC_PHASE / ALLOC_REASON from size-grain (alloc_table) up
    # to OPT-grain (working_table). Runs after the size rows have their
    # values set by _classify_alloc_reason.
    #
    # OPT-level reasoning:
    #   • ALLOC_PHASE   = MAX phase tag across this OPT's size rows. With
    #                     fallback removed (2026-05-16) this is always 'MAIN'
    #                     but the column is preserved as the audit anchor.
    #   • ALLOC_REASON  = one summary code, derived from the size rows:
    #       all SHIPPED_FULL                      → SHIPPED_FULL
    #       any shipped + any blocked/partial     → SHIPPED_PARTIAL
    #       all blocked w/ same reason            → that reason
    #       all blocked w/ different reasons      → BLOCKED_MIXED
    try:
        _run(conn, f"""
            ;WITH A AS (
                SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR,
                       MAX(ISNULL(ALLOC_PHASE, 'MAIN'))           AS phase_tag,
                       COUNT(*)                                   AS sz_total,
                       SUM(CASE WHEN ALLOC_REASON = 'SHIPPED_FULL'    THEN 1 ELSE 0 END) AS sz_full,
                       SUM(CASE WHEN ALLOC_REASON = 'SHIPPED_PARTIAL' THEN 1 ELSE 0 END) AS sz_partial,
                       SUM(CASE WHEN ALLOC_REASON LIKE 'BLOCKED_%'    THEN 1 ELSE 0 END) AS sz_blocked,
                       SUM(CASE WHEN ALLOC_REASON LIKE 'BLOCKED_%' OR ALLOC_REASON LIKE 'SHIPPED_PARTIAL%'
                                THEN 1 ELSE 0 END)                                       AS sz_unmet,
                       MIN(CASE WHEN ALLOC_REASON LIKE 'BLOCKED_%' THEN ALLOC_REASON END) AS first_block,
                       COUNT(DISTINCT CASE WHEN ALLOC_REASON LIKE 'BLOCKED_%' THEN ALLOC_REASON END) AS distinct_blocks
                FROM [{alloc_table}]
                GROUP BY WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR
            )
            UPDATE W SET
                W.ALLOC_PHASE = A.phase_tag,
                W.ALLOC_REASON = CASE
                    -- All sizes fully shipped → SHIPPED_FULL
                    WHEN A.sz_full = A.sz_total AND A.sz_total > 0 THEN 'SHIPPED_FULL'
                    -- Any partial or some shipped + some blocked → SHIPPED_PARTIAL
                    WHEN A.sz_full + A.sz_partial > 0 AND A.sz_unmet > 0 THEN 'SHIPPED_PARTIAL'
                    -- All blocked with same reason → that reason
                    WHEN A.sz_blocked = A.sz_total AND A.distinct_blocks = 1 THEN A.first_block
                    -- All blocked with different reasons → MIXED
                    WHEN A.sz_blocked = A.sz_total AND A.distinct_blocks > 1 THEN 'BLOCKED_MIXED'
                    ELSE W.ALLOC_REASON END
            FROM [{working_table}] W
            INNER JOIN A
                ON  A.WERKS          = W.WERKS
                AND A.MAJ_CAT        = W.MAJ_CAT
                AND A.GEN_ART_NUMBER = W.GEN_ART_NUMBER
                AND ISNULL(A.CLR,'') = ISNULL(W.CLR,'')
            WHERE W.LISTED_FLAG = 1
        """)
    except Exception as e:
        logger.warning(f"[D] reason rollup to working_table failed: {e}")

    _run(conn, f"""
        UPDATE [{working_table}] SET
            ALLOC_STATUS = 'INELIGIBLE',
            ALLOC_PHASE  = ISNULL(ALLOC_PHASE, 'MAIN'),
            ALLOC_REASON = COALESCE(
                CASE WHEN ALLOC_REASON IS NOT NULL AND ALLOC_REASON LIKE 'INELIGIBLE_%' THEN ALLOC_REASON END,
                'INELIGIBLE_' + LEFT(ISNULL(NULLIF(LISTED_REASON, ''), 'STAGE_A'), 50)
            )
        WHERE LISTED_FLAG = 0
    """)
    # Split the "no pool" bucket into the two real causes so users can tell
    # demand-side issues (OPT_REQ=0 → store didn't need stock) from supply-side
    # issues (had demand, RDC pool was empty → MSA exhausted at OPT-grain).
    _run(conn, f"""
        UPDATE [{working_table}] SET ALLOC_STATUS = 'NOT_ALLOCATED',
               ALLOC_PHASE  = ISNULL(ALLOC_PHASE, 'MAIN'),
               ALLOC_REASON = ISNULL(ALLOC_REASON,
                   CASE WHEN ISNULL(OPT_REQ, 0) <= 0
                        THEN 'BLOCKED_NO_REQ'
                        ELSE 'BLOCKED_NO_POOL_MSA' END),
               ALLOC_REMARKS = ISNULL(ALLOC_REMARKS,'') +
                   CASE WHEN ISNULL(OPT_REQ, 0) <= 0
                        THEN ' NO_REQ(OPT_REQ=0, OPT_MBQ='
                             + CAST(ISNULL(OPT_REQ,0) AS NVARCHAR(20))
                             + '/' + CAST(ISNULL(OPT_MBQ,0) AS NVARCHAR(20)) + ')'
                        ELSE ' NO_POOL_MSA(OPT_REQ='
                             + CAST(ISNULL(OPT_REQ,0) AS NVARCHAR(20))
                             + ', MSA_FNL_Q=' + CAST(ISNULL(MSA_FNL_Q,0) AS NVARCHAR(20))
                             + ', MSA_FNL_Q_REM=' + CAST(ISNULL(MSA_FNL_Q_REM,0) AS NVARCHAR(20))
                             + ')' END
        WHERE LISTED_FLAG = 1
          AND (ALLOC_QTY  IS NULL OR ALLOC_QTY  = 0)
          AND (HOLD_QTY   IS NULL OR HOLD_QTY   = 0)
    """)


def _snapshot_live_pool_to_alloc(conn, alloc_table: str) -> None:
    """
    Snapshot the live `#nre_pool.FNL_Q_REM` onto alloc_table.RDC_FNL_Q_REM_LIVE
    BEFORE _cleanup drops the temp pool. Then append a `LIVE_POOL=<n>` audit
    detail to ALLOC_REMARKS for every row currently flagged
    `SKIP_REASON='NO_POOL_MSA'`.

    Why: the existing `FNL_Q_REM` column on alloc_table is re-derived from
    `FNL_Q − POOL_CONSUMED` after the MJ_REQ cap restoration (~line 2221),
    so a row that lost the per-(RDC, MAJ_CAT, GEN_ART, CLR, VAR_ART, SZ) pool
    race ends up looking like the pool is still healthy. The live snapshot
    captures what was actually left in the pool at end-of-allocation, which
    is what NO_POOL_MSA really means.

    Must be called BEFORE `_cleanup(conn)` because `#nre_pool` is dropped there.
    """
    try:
        _run(conn, f"ALTER TABLE [{alloc_table}] ADD [RDC_FNL_Q_REM_LIVE] FLOAT NULL")
    except Exception:
        pass

    # Pool may already be gone if this is a re-run on a finalized table.
    try:
        pool_exists = conn.execute(
            text(f"SELECT OBJECT_ID('tempdb..{POOL_TABLE}')")
        ).scalar()
    except Exception:
        pool_exists = None
    if not pool_exists:
        return

    _run(conn, f"""
        UPDATE A
           SET A.RDC_FNL_Q_REM_LIVE = P.FNL_Q_REM
        FROM [{alloc_table}] A
        INNER JOIN {POOL_TABLE} P
            ON P.RDC            = A.RDC
           AND P.MAJ_CAT        = A.MAJ_CAT
           AND P.GEN_ART_NUMBER = A.GEN_ART_NUMBER
           AND ISNULL(P.CLR,'') = ISNULL(A.CLR,'')
           AND P.VAR_ART        = A.VAR_ART
           AND P.SZ             = A.SZ
    """)

    _run(conn, f"""
        UPDATE [{alloc_table}] SET
            ALLOC_REMARKS = ISNULL(ALLOC_REMARKS,'')
                + ' LIVE_POOL=' + CAST(ISNULL(RDC_FNL_Q_REM_LIVE, 0) AS NVARCHAR(20)) + ';'
        WHERE ISNULL(SKIP_REASON,'') = 'NO_POOL_MSA'
    """)


def _cleanup(conn):
    _run(conn, f"IF OBJECT_ID('tempdb..{POOL_TABLE}') IS NOT NULL DROP TABLE {POOL_TABLE}")


def _ensure_alloc_remarks_col(conn, alloc_table: str):
    """
    Idempotent ALTER to add ALLOC_REMARKS column on alloc_table. The column
    was added late in the engine's life — older runs created alloc_table
    without it, but cap / pack-round / sec-cap helpers all write detailed
    audit lines into it.

    Called defensively at the top of every helper that writes ALLOC_REMARKS
    so missing-column errors don't blow up the whole run.
    """
    try:
        _run(conn, f"ALTER TABLE [{alloc_table}] ADD [ALLOC_REMARKS] NVARCHAR(MAX) NULL")
    except Exception:
        pass


def _classify_alloc_reason(conn, alloc_table: str) -> None:
    """Per-row reason classifier — stub.

    The real per-size SHIPPED_FULL / SHIPPED_PARTIAL_<reason> /
    BLOCKED_<reason> / INELIGIBLE_<R##> logic was referenced by the
    May-2026 refactor but never landed. Until it does, this is a safe
    no-op: ALLOC_REASON stays NULL (its column default after
    _ensure_phase_reason_cols), and the Stage D rollup gracefully maps
    that to NULL on the listing side.

    Without this stub the call sites raise AttributeError, which
    silently nukes alloc_result['ship_qty_total'], 'hold_qty_total',
    and 'alloc_rows' for the whole session (caught upstream as a warning
    but the session row writes NULL totals).
    """
    logger.debug(f"[_classify_alloc_reason] stub no-op on {alloc_table}")


def _ensure_phase_reason_cols(conn, alloc_table: str,
                              working_table: Optional[str] = None) -> None:
    """
    Idempotent ALTERs for the per-row reason-tracking columns introduced in
    the May-2026 audit redesign. Fallback was removed 2026-05-16, so the
    FB_* / SHIP_QTY_PRE_FB columns are no longer added — see
    `backend/scripts/migrations/2026_05_17_drop_fb_columns.sql` for the
    one-time DROP that retires the orphaned columns from existing tables.

      ARS_ALLOC_WORKING (size-grain):
        ALLOC_PHASE   which phase finalised this row (always 'MAIN' today;
                      kept as a stable audit anchor)
        ALLOC_REASON  one short code explaining the outcome
                      (SHIPPED_FULL / SHIPPED_PARTIAL / BLOCKED_<reason>)

      ARS_LISTING_WORKING (OPT-grain, when working_table is given):
        ALLOC_PHASE, ALLOC_REASON (rolled up from alloc-table rows at Stage D)
    """
    alloc_cols = [
        ("ALLOC_PHASE",  "NVARCHAR(20) NULL"),
        ("ALLOC_REASON", "NVARCHAR(80) NULL"),
    ]
    for col, typedef in alloc_cols:
        try:
            _run(conn, f"ALTER TABLE [{alloc_table}] ADD [{col}] {typedef}")
        except Exception:
            pass

    if working_table:
        working_cols = [
            ("ALLOC_PHASE",  "NVARCHAR(20) NULL"),
            ("ALLOC_REASON", "NVARCHAR(80) NULL"),
        ]
        for col, typedef in working_cols:
            try:
                _run(conn, f"ALTER TABLE [{working_table}] ADD [{col}] {typedef}")
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════
#  SECONDARY-GRID CAP — constants + helpers
#  ───────────────────────────────────────────────────────────────────────
#  The main-pass Secondary-grid cap (_apply_sec_grid_cap_pre_gate) blocks an
#  OPT at OPT grain when shipping it would push any participating Secondary
#  grid past its cap. Cap is per-grid (ARS_GRID_BUILDER.sec_cap_pct, default
#  130) and the grid must have ARS_GRID_BUILDER.sec_cap_applicable = 1.
#
#  Fallback (the post-main reallocation phase, F0–F5) was removed 2026-05-16
#  along with its post-trim sec-cap variant. See
#  backend/app/docs/processes/fallback_archived.md if you ever need to
#  rebuild it. Note: ACS_D = accessories density (one OPT display quantity),
#  NOT daily sale — used here only as part of the override gate math.
# ═══════════════════════════════════════════════════════════════════════

# Default Secondary-grid dispatch cap percentage. Used when a grid's
# ARS_GRID_BUILDER.sec_cap_pct is NULL (no per-grid override).
SEC_CAP_DEFAULT_PCT = 130.0

# High-demand override for the pre-gate sec-cap: when an OPT would breach a
# Secondary grid's 130% budget, admit it anyway if its OPT_REQ is at least
# this fraction of its OPT_MBQ. Rationale — only OPTs the store needs at
# full display capacity (or higher) get to bypass the sec-cap; lower-demand
# OPTs respect the grid budget. The earlier 50% default was too lax —
# virtually every listed OPT cleared 50% × OPT_MBQ, making the cap a no-op.
SEC_CAP_PRE_OVERRIDE_OPT_REQ_PCT = 100.0


def _discover_all_active_grids(conn) -> Dict[str, Dict]:
    """
    Returns ALL active grids (Primary + Secondary) including the implicit MJ.

    Shape extends `_discover_primary_grids` with `prefix`, `group`, `seq`,
    `mbq_col`, `req_col`, `stk_col` — everything the sec-cap and recompute
    helpers need to address columns and decide trim eligibility.
    """
    out: Dict[str, Dict] = {
        "MJ": {
            "hier":    ["MAJ_CAT"],
            "extras":  [],
            "prefix":  "MJ",
            "group":   "Primary",
            "seq":     1,
            "gh_col":  "GH_MJ",
            "h_col":   "H_MJ",
            "h_rem":   "H_MJ_REM",
            "mbq_col": "MJ_MBQ",
            "req_col": "MJ_REQ",
            "req_rem": "MJ_REQ_REM",
            "stk_col": "MJ_STK_TTL",
            # MJ is the Primary grid — never participates in sec-cap.
            "sec_cap_applicable": False,
            "sec_cap_pct":        None,
        }
    }
    if not _exists(conn, "ARS_GRID_BUILDER"):
        return out
    # Pull sec_cap_applicable / sec_cap_pct alongside the existing fields.
    # ISNULL+TRY pattern keeps the helper backwards-compatible with older
    # deployments where the two columns haven't been ALTERed in yet.
    try:
        rows = conn.execute(text(
            "SELECT grid_name, hierarchy_columns, ISNULL(grid_group,'Primary'), "
            "       ISNULL(seq, 999), "
            "       ISNULL(sec_cap_applicable, 0) AS sec_cap_applicable, "
            "       sec_cap_pct "
            "FROM [ARS_GRID_BUILDER] WHERE UPPER(status)='ACTIVE' "
            "ORDER BY ISNULL(seq, 999), grid_name"
        )).fetchall()
    except Exception:
        # Older schema without the two new columns — fall back to the
        # original SELECT and synthesize off/None values.
        try:
            rows_legacy = conn.execute(text(
                "SELECT grid_name, hierarchy_columns, ISNULL(grid_group,'Primary'), "
                "       ISNULL(seq, 999) "
                "FROM [ARS_GRID_BUILDER] WHERE UPPER(status)='ACTIVE' "
                "ORDER BY ISNULL(seq, 999), grid_name"
            )).fetchall()
            rows = [(r[0], r[1], r[2], r[3], 0, None) for r in rows_legacy]
        except Exception as e:
            logger.warning(f"_discover_all_active_grids: {e}")
            return out
    for grid_name, hier_json, grid_group, seq, sec_cap_app, sec_cap_pct in rows:
        try:
            hier = json.loads(hier_json) if isinstance(hier_json, str) else hier_json
        except Exception:
            continue
        if not hier or any(str(x).upper() in _SKIP_ART for x in hier):
            continue
        hier_u = [str(h).upper() for h in hier]
        last = hier_u[-1]
        if last in ("WERKS", "MAJ_CAT"):
            # Covered by MJ above — skip duplicate.
            continue
        prefix = grid_name.upper()
        if prefix.startswith("MJ_"):
            prefix = prefix[3:]
        out[grid_name.upper()] = {
            "hier":    hier_u,
            "extras":  [h for h in hier_u if h not in ("WERKS", "MAJ_CAT")],
            "prefix":  prefix,
            "group":   str(grid_group).strip() or "Primary",
            "seq":     int(seq) if seq is not None else 999,
            "gh_col":  f"GH_{last}",
            "h_col":   f"H_{last}",
            "h_rem":   f"H_{last}_REM",
            "mbq_col": f"{prefix}_MBQ",
            "req_col": f"{prefix}_REQ",
            "req_rem": f"{prefix}_REQ_REM",
            "stk_col": f"{prefix}_STK_TTL",
            # Per-grid sec-cap controls. _apply_sec_grid_cap_pre_gate filters
            # on `sec_cap_applicable` and uses `sec_cap_pct` (when set) to
            # override the global SEC_CAP_DEFAULT_PCT for this grid.
            "sec_cap_applicable": bool(sec_cap_app),
            "sec_cap_pct":        float(sec_cap_pct) if sec_cap_pct is not None else None,
        }
    return out


def _apply_sec_grid_cap_pre_gate(
    conn,
    alloc_table: str,
    working_table: str,
    all_grids: Dict[str, Dict],
    opt_type: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Secondary-grid PRE-GATE cap (main pass, May-2026 redesign).

    Unlike _apply_sec_grid_cap (post-trim) — which lets the waterfall ship
    everything and then trims partial rows backwards — this helper decides at
    OPT grain whether the whole OPT may ship. An OPT either ships in full to
    the store or doesn't ship at all; no half-trimmed VAR_ART × SZ rows.

    Algorithm — "keep checking" (a skip does NOT close the grid):

      1. For each Secondary grid (per ARS_GRID_BUILDER.grid_group), precompute
         budget per (WERKS, MAJ_CAT, <grid_extras>) = MAX(MBQ_<grid>) × 1.30.
      2. Aggregate alloc_table to OPT grain — one row per
         (WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR, OPT_TYPE) carrying SUM(SHIP_QTY),
         OPT_REQ, OPT_MBQ, and every Secondary grid's extra
         (FAB, MICRO_MVGR, CLR, M_VND_CD).
      3. Walk OPTs in priority order (OPT_PRIORITY_RANK ASC, ST_RANK ASC,
         GEN_ART_NUMBER ASC, CLR ASC, OPT_TYPE ASC), keeping per-grid running
         totals.
      4. For each OPT, evaluate ALL its Secondary grids. A grid is skipped
         entirely for the OPT when EITHER applicability gate fails (OR logic
         — running totals are NOT advanced on a skipped grid):
            GH_<HC> = 0 for this MAJ_CAT — the grid does not apply (per
                ARS_GRID_HIERARCHY / Part 7), OR
            <grid>_MBQ = 0 at this grain — no MBQ configured (*_MBQ columns
                are sparse; zero means "unconstrained", not "zero").
         On grids that survive both gates, breach = (running + opt_ship >
         budget). The first breaching grid wins the blame for SKIP_REASON
         (multi-grid AND short-circuits on the first failure).
      5. HIGH-DEMAND OVERRIDE: when a breach exists, check
            OPT_REQ >= SEC_CAP_PRE_OVERRIDE_OPT_REQ_PCT / 100 × OPT_MBQ.
         If yes — admit the OPT despite the breach, advance running on every
         grid, and stamp an 'SEC_CAP_PRE_OVERRIDE(...)' remark on its rows.
         If no — block the OPT, running totals are NOT advanced.
      6. Bulk-update alloc_table: blocked OPTs get SHIP_QTY=0, ALLOC_QTY=0,
         ALLOC_STATUS=SKIPPED, SKIP_REASON='SEC_CAP_PRE_<grid>', ALLOC_REMARKS
         appended with the diagnostic 'SEC_CAP_PRE_BLOCK(grid=…, running=…,
         intended=…, budget=…)'. Overridden OPTs keep their SHIP_QTY and get
         only the override remark.

    Why no pool restore: blocked OPTs were never *committed* in the sense that
    the waterfall already deducted their units from #nre_pool when SHIP_QTY was
    written. To restore correctly we'd need the same logic as the post-trim
    helper. The simpler path the user requested: keep blocked stock in the
    SHIP_QTY=0 alloc rows; downstream fallback re-derives the pool from the
    current FNL_Q_REM state (see rule_engine_pandas main-pass pool rebuild)
    or re-reads alloc_table totals. The net effect is the same — stock the
    main pass would have shipped is now available for fallback.

    Returns audit dict: {mode, cap_pct, grids_evaluated, opts_blocked,
    units_blocked, blocks_by_grid}.
    """
    cap_pct = SEC_CAP_DEFAULT_PCT
    cap_factor = cap_pct / 100.0

    _ensure_alloc_remarks_col(conn, alloc_table)

    work_cols  = {c.upper() for c in _cols(conn, working_table)}
    alloc_cols = {c.upper() for c in _cols(conn, alloc_table)}

    # sec_grids — each entry is (grid_name, meta). `meta["cap_factor"]` is the
    # per-grid factor (overrides the global SEC_CAP_DEFAULT_PCT when the grid
    # has its own `sec_cap_pct`).
    sec_grids: List[tuple] = []
    skipped_not_applicable: List[str] = []
    for g_name, meta in all_grids.items():
        if str(meta.get("group", "")).strip().lower() != "secondary":
            continue
        # Per-grid opt-in flag from ARS_GRID_BUILDER.sec_cap_applicable.
        # Grids with the flag OFF are NOT iterated by the cap. Backfill on
        # first deploy sets the flag OFF for every existing row, so this
        # path becomes the default until operators opt in grid-by-grid.
        if not meta.get("sec_cap_applicable"):
            skipped_not_applicable.append(g_name)
            continue
        mbq = meta.get("mbq_col", "") or ""
        extras = list(meta.get("extras") or [])
        gh_col = meta.get("gh_col", "") or ""
        if not extras or not mbq:
            continue
        if mbq.upper() not in work_cols:
            continue
        grid_keys = ["WERKS", "MAJ_CAT"] + extras
        if not all(k.upper() in work_cols  for k in grid_keys): continue
        if not all(k.upper() in alloc_cols for k in grid_keys): continue
        # Per-grid cap %: meta["sec_cap_pct"] if set, else fall back to the
        # global SEC_CAP_DEFAULT_PCT.
        grid_pct = meta.get("sec_cap_pct")
        grid_factor = (float(grid_pct) / 100.0) if grid_pct else cap_factor
        sec_grids.append((g_name, {
            "mbq":        mbq,
            "extras":     extras,
            "gh_col":     gh_col if gh_col.upper() in work_cols else None,
            "cap_factor": grid_factor,
            "cap_pct":    float(grid_pct) if grid_pct else cap_pct,
        }))

    audit: Dict[str, Any] = {
        "mode": "pre_gate",
        "cap_pct": cap_pct,  # global default; per-grid overrides recorded below
        "override_req_pct": SEC_CAP_PRE_OVERRIDE_OPT_REQ_PCT,
        "grids_evaluated": len(sec_grids),
        "grids_skipped_not_applicable": skipped_not_applicable,
        "opts_blocked": 0,
        "units_blocked": 0.0,
        "opts_overridden": 0,
        "units_overridden": 0.0,
        "blocks_by_grid": {n: 0 for n, _ in sec_grids},
        "overrides_by_grid": {n: 0 for n, _ in sec_grids},
        "cap_pct_by_grid": {n: m["cap_pct"] for n, m in sec_grids},
    }
    if not sec_grids:
        if skipped_not_applicable:
            logger.info(
                f"[sec_cap_pre] no participating Secondary grids — "
                f"sec_cap_applicable=0 on {len(skipped_not_applicable)} grid(s): "
                f"{skipped_not_applicable}"
            )
        else:
            logger.info("[sec_cap_pre] no Secondary grids active — skipping")
        return audit

    ot_filter = "AND A.OPT_TYPE = :ot" if opt_type else ""
    ot_params: Dict[str, Any] = {"ot": opt_type} if opt_type else {}

    # ── Step 1: budgets per (grid, grain) ─────────────────────────────────
    # Budget uses the *per-grid* cap_factor (m["cap_factor"]). Falls back to
    # the global SEC_CAP_DEFAULT_PCT factor when the grid has no override.
    budgets: Dict[str, Dict[tuple, float]] = {}
    for g_name, m in sec_grids:
        extras = m["extras"]
        mbq = m["mbq"]
        grid_factor = m["cap_factor"]
        sel_keys = ["WERKS", "MAJ_CAT"] + extras
        sel_sql = ", ".join(f"[{k}]" for k in sel_keys)
        try:
            rows = conn.execute(text(f"""
                SELECT {sel_sql}, MAX(ISNULL([{mbq}], 0)) AS mbq_val
                FROM [{working_table}]
                GROUP BY {sel_sql}
            """)).fetchall()
        except Exception as e:
            logger.warning(f"[sec_cap_pre] budget load failed for {g_name}: {str(e)[:200]}")
            continue
        bmap: Dict[tuple, float] = {}
        for r in rows:
            grain = tuple(r[i] for i in range(len(sel_keys)))
            bmap[grain] = float(r[-1] or 0) * grid_factor
        budgets[g_name] = bmap

    # ── Step 1b: GH_<HC> per MAJ_CAT per grid (grid applicability flag) ──
    # GH_<HC> is set in Part 7 to 1 when the grid's hierarchy column is active
    # for that MAJ_CAT, 0 otherwise. The cap should only fire when GH=1 — a
    # grid that doesn't apply to the MAJ_CAT cannot have an over-allocation.
    gh_by_grid: Dict[str, Dict[Any, int]] = {}
    for g_name, m in sec_grids:
        gh_col = m.get("gh_col")
        if not gh_col:
            # No GH column resolved → fall back to "always applicable".
            gh_by_grid[g_name] = {}
            continue
        try:
            rows = conn.execute(text(f"""
                SELECT MAJ_CAT, MAX(ISNULL(TRY_CAST([{gh_col}] AS INT), 0)) AS gh
                FROM [{working_table}]
                GROUP BY MAJ_CAT
            """)).fetchall()
        except Exception as e:
            logger.warning(f"[sec_cap_pre] GH load failed for {g_name}/{gh_col}: {str(e)[:200]}")
            gh_by_grid[g_name] = {}
            continue
        gh_by_grid[g_name] = {r[0]: int(r[1] or 0) for r in rows}

    # ── Step 2: OPT-level aggregates from alloc_table ─────────────────────
    # Union of all Secondary-grid extras (each is a column on alloc_table per
    # the column-existence check above).
    extra_cols: List[str] = []
    seen: set = set()
    for _, m in sec_grids:
        for e in m["extras"]:
            if e.upper() not in seen:
                seen.add(e.upper())
                extra_cols.append(e)

    extra_sel = ", ".join(f"MAX(A.[{e}]) AS [{e}]" for e in extra_cols)
    extra_sel_sql = (", " + extra_sel) if extra_sel else ""

    # OPT-grain REQ / MBQ for the high-demand override. Both originate at
    # OPT grain in working_table and are propagated to alloc_table by Stage B.
    # MAX is fine because every VAR_ART × SZ row for the same OPT carries the
    # same value.
    has_opt_req = "OPT_REQ" in alloc_cols
    has_opt_mbq = "OPT_MBQ" in alloc_cols
    opt_req_sel = "MAX(ISNULL(A.OPT_REQ, 0)) AS opt_req" if has_opt_req \
                  else "CAST(0 AS FLOAT) AS opt_req"
    opt_mbq_sel = "MAX(ISNULL(A.OPT_MBQ, 0)) AS opt_mbq" if has_opt_mbq \
                  else "CAST(0 AS FLOAT) AS opt_mbq"

    try:
        opts = conn.execute(text(f"""
            SELECT A.WERKS, A.MAJ_CAT, A.GEN_ART_NUMBER, A.CLR, A.OPT_TYPE,
                   MAX(ISNULL(A.OPT_PRIORITY_RANK, 999999)) AS opt_pri,
                   MAX(ISNULL(A.ST_RANK,           999999)) AS st_rank,
                   SUM(ISNULL(A.SHIP_QTY, 0))               AS opt_ship,
                   {opt_req_sel},
                   {opt_mbq_sel}
                   {extra_sel_sql}
            FROM [{alloc_table}] A
            WHERE ISNULL(A.SHIP_QTY, 0) > 0 {ot_filter}
            GROUP BY A.WERKS, A.MAJ_CAT, A.GEN_ART_NUMBER, A.CLR, A.OPT_TYPE
            ORDER BY opt_pri ASC, st_rank ASC,
                     A.WERKS ASC, A.MAJ_CAT ASC,
                     A.GEN_ART_NUMBER ASC, ISNULL(A.CLR, '') ASC, A.OPT_TYPE ASC
        """), ot_params).fetchall()
    except Exception as e:
        logger.warning(f"[sec_cap_pre] OPT aggregate failed: {str(e)[:200]}")
        return audit

    if not opts:
        logger.info("[sec_cap_pre] no shipped OPTs to evaluate")
        return audit

    base_cols = ["WERKS", "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "OPT_TYPE",
                 "opt_pri", "st_rank", "opt_ship", "opt_req", "opt_mbq"]
    col_index = {c: i for i, c in enumerate(base_cols + extra_cols)}

    # ── Step 3: priority-ordered walk with per-grid running totals ────────
    override_ratio = SEC_CAP_PRE_OVERRIDE_OPT_REQ_PCT / 100.0
    running: Dict[tuple, float] = {}  # (g_name, grain) -> running ship
    blocked: List[tuple] = []
    # (WERKS, MAJ_CAT, GEN_ART, CLR, OPT_TYPE, blocking_grid, running, intended, budget)
    overridden: List[tuple] = []
    # (WERKS, MAJ_CAT, GEN_ART, CLR, OPT_TYPE, breach_grid, running, intended, budget, opt_req, opt_mbq)

    for r in opts:
        werks   = r[col_index["WERKS"]]
        majc    = r[col_index["MAJ_CAT"]]
        gen     = r[col_index["GEN_ART_NUMBER"]]
        clr     = r[col_index["CLR"]]
        otype   = r[col_index["OPT_TYPE"]]
        ship    = float(r[col_index["opt_ship"]] or 0)
        opt_req = float(r[col_index["opt_req"]] or 0)
        opt_mbq = float(r[col_index["opt_mbq"]] or 0)
        if ship <= 0:
            continue

        per_grid: List[tuple] = []  # (g_name, grain, run_before, budget)
        breach: Optional[tuple] = None
        for g_name, m in sec_grids:
            grain_vals = [werks, majc] + [r[col_index[e]] for e in m["extras"]]
            grain = tuple(grain_vals)
            budget = budgets.get(g_name, {}).get(grain, 0.0)
            run_before = running.get((g_name, grain), 0.0)

            # Skip the grid entirely for this OPT when EITHER applicability
            # gate fails (OR logic — both per_grid tracking and breach check
            # are bypassed):
            #
            #   GH_<HC> = 0   → grid does not apply to this MAJ_CAT
            #                   (per ARS_GRID_HIERARCHY / Part 7)
            #   <grid>_MBQ = 0 → no MBQ configured at this grain
            #                    (*_MBQ columns are sparse — many grains
            #                     legitimately carry no per-grid MBQ)
            #
            # Default GH to "applies" when the GH map couldn't be resolved
            # (column missing); fall back to MBQ-only gate in that case.
            gh_map = gh_by_grid.get(g_name) or {}
            gh_applies = (gh_map.get(majc, 1) == 1) if gh_map else True
            if (not gh_applies) or (budget <= 0):
                continue

            per_grid.append((g_name, grain, run_before, budget))
            if breach is None and run_before + ship > budget:
                breach = (g_name, grain, run_before, budget)
                # Don't break — finish gathering per_grid for the override path
                # so we can advance running on every grid if we admit.

        if breach is not None:
            g_name, grain, run_before, budget = breach
            # High-demand override: OPT_REQ >= 50% × OPT_MBQ → admit anyway.
            if opt_mbq > 0 and opt_req >= override_ratio * opt_mbq:
                for gn, gr, rb, _ in per_grid:
                    running[(gn, gr)] = rb + ship
                overridden.append((werks, majc, gen, clr, otype,
                                   g_name, run_before, ship, budget,
                                   opt_req, opt_mbq))
                audit["opts_overridden"]  += 1
                audit["units_overridden"] += ship
                audit["overrides_by_grid"][g_name] = audit["overrides_by_grid"].get(g_name, 0) + 1
                continue
            # No override — block.
            blocked.append((werks, majc, gen, clr, otype,
                            g_name, run_before, ship, budget))
            audit["opts_blocked"]   += 1
            audit["units_blocked"]  += ship
            audit["blocks_by_grid"][g_name] = audit["blocks_by_grid"].get(g_name, 0) + 1
            continue

        # Admit — advance running on every grid this OPT belongs to.
        for g_name, grain, run_before, _ in per_grid:
            running[(g_name, grain)] = run_before + ship

    if not blocked and not overridden:
        logger.info(
            f"[sec_cap_pre] cap={cap_pct:.0f}% grids={[n for n,_ in sec_grids]} "
            f"evaluated={len(opts)} blocked=0 overridden=0"
        )
        return audit

    # ── Step 4: bulk-update blocked + overridden OPTs ─────────────────────
    # One temp table carries both groups, distinguished by `decision`. Types
    # for the key columns mirror alloc_table via a 0-row SELECT INTO.
    try:
        _run(conn, "IF OBJECT_ID('tempdb..#sec_cap_pre_decisions') IS NOT NULL DROP TABLE #sec_cap_pre_decisions")
        _run(conn, f"""
            SELECT TOP 0
                A.WERKS, A.MAJ_CAT, A.GEN_ART_NUMBER, A.CLR, A.OPT_TYPE
            INTO #sec_cap_pre_decisions
            FROM [{alloc_table}] A
        """)
        _run(conn, """
            ALTER TABLE #sec_cap_pre_decisions ADD
                decision      NVARCHAR(10)  NULL,
                blocking_grid NVARCHAR(100) NULL,
                running_val   FLOAT         NULL,
                intended_val  FLOAT         NULL,
                budget_val    FLOAT         NULL,
                opt_req_val   FLOAT         NULL,
                opt_mbq_val   FLOAT         NULL
        """)

        insert_sql = text("""
            INSERT INTO #sec_cap_pre_decisions
                (WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR, OPT_TYPE,
                 decision, blocking_grid, running_val, intended_val, budget_val,
                 opt_req_val, opt_mbq_val)
            VALUES (:w, :m, :g, :c, :o, :dec, :grid, :run, :int_v, :bud,
                    :req, :mbq)
        """)
        params: List[Dict[str, Any]] = []
        for (w, mj, gn, cv, ov, grid, rv, iv, bv) in blocked:
            params.append({
                "w": w, "m": mj, "g": int(gn) if gn is not None else None,
                "c": cv, "o": ov,
                "dec": "BLOCK", "grid": grid,
                "run": float(rv), "int_v": float(iv), "bud": float(bv),
                "req": 0.0, "mbq": 0.0,
            })
        for (w, mj, gn, cv, ov, grid, rv, iv, bv, rq, mq) in overridden:
            params.append({
                "w": w, "m": mj, "g": int(gn) if gn is not None else None,
                "c": cv, "o": ov,
                "dec": "OVERRIDE", "grid": grid,
                "run": float(rv), "int_v": float(iv), "bud": float(bv),
                "req": float(rq), "mbq": float(mq),
            })
        if params:
            conn.execute(insert_sql, params)

        # Blocked: zero SHIP/ALLOC, stamp SKIPPED + SEC_CAP_PRE_<grid> reason.
        if blocked:
            _run(conn, f"""
                UPDATE A SET
                    A.SHIP_QTY    = 0,
                    A.ALLOC_QTY   = 0,
                    A.ALLOC_STATUS = 'SKIPPED',
                    A.SKIP_REASON = CASE
                        WHEN A.SKIP_REASON IS NULL OR A.SKIP_REASON = ''
                             THEN 'SEC_CAP_PRE_' + B.blocking_grid
                        ELSE A.SKIP_REASON END,
                    A.ALLOC_REMARKS = ISNULL(A.ALLOC_REMARKS, '')
                        + ' SEC_CAP_PRE_BLOCK(grid=' + B.blocking_grid
                        + ', running='  + CAST(B.running_val  AS NVARCHAR(20))
                        + ', intended=' + CAST(B.intended_val AS NVARCHAR(20))
                        + ', budget='   + CAST(B.budget_val   AS NVARCHAR(20))
                        + ');'
                FROM [{alloc_table}] A
                INNER JOIN #sec_cap_pre_decisions B
                    ON  A.WERKS = B.WERKS
                    AND A.MAJ_CAT = B.MAJ_CAT
                    AND A.GEN_ART_NUMBER = B.GEN_ART_NUMBER
                    AND ISNULL(A.CLR, '') = ISNULL(B.CLR, '')
                    AND A.OPT_TYPE = B.OPT_TYPE
                WHERE B.decision = 'BLOCK'
            """)

        # Overridden: keep SHIP, append diagnostic remark only.
        if overridden:
            _run(conn, f"""
                UPDATE A SET
                    A.ALLOC_REMARKS = ISNULL(A.ALLOC_REMARKS, '')
                        + ' SEC_CAP_PRE_OVERRIDE(grid=' + B.blocking_grid
                        + ', running='  + CAST(B.running_val  AS NVARCHAR(20))
                        + ', intended=' + CAST(B.intended_val AS NVARCHAR(20))
                        + ', budget='   + CAST(B.budget_val   AS NVARCHAR(20))
                        + ', opt_req='  + CAST(B.opt_req_val  AS NVARCHAR(20))
                        + ', opt_mbq='  + CAST(B.opt_mbq_val  AS NVARCHAR(20))
                        + ');'
                FROM [{alloc_table}] A
                INNER JOIN #sec_cap_pre_decisions B
                    ON  A.WERKS = B.WERKS
                    AND A.MAJ_CAT = B.MAJ_CAT
                    AND A.GEN_ART_NUMBER = B.GEN_ART_NUMBER
                    AND ISNULL(A.CLR, '') = ISNULL(B.CLR, '')
                    AND A.OPT_TYPE = B.OPT_TYPE
                WHERE B.decision = 'OVERRIDE'
            """)

        _run(conn, "IF OBJECT_ID('tempdb..#sec_cap_pre_decisions') IS NOT NULL DROP TABLE #sec_cap_pre_decisions")
    except Exception as e:
        logger.warning(f"[sec_cap_pre] bulk-update failed: {str(e)[:300]}")
        try:
            _run(conn, "IF OBJECT_ID('tempdb..#sec_cap_pre_decisions') IS NOT NULL DROP TABLE #sec_cap_pre_decisions")
        except Exception:
            pass
        return audit

    # Render per-grid caps inline (e.g. "FAB@150 MICRO_MVGR@130") so the log
    # makes the override pattern explicit. Skipped grids (sec_cap_applicable=0)
    # are listed separately so operators can see what was NOT capped.
    pct_str = " ".join(f"{n}@{m['cap_pct']:.0f}" for n, m in sec_grids)
    skip_str = (f" skipped(applicable=0)={skipped_not_applicable}"
                if skipped_not_applicable else "")
    logger.info(
        f"[sec_cap_pre] override>={SEC_CAP_PRE_OVERRIDE_OPT_REQ_PCT:.0f}%×OPT_MBQ "
        f"caps={{{pct_str}}}{skip_str}: "
        f"blocked={audit['opts_blocked']} OPTs ({audit['units_blocked']:.0f}u) "
        f"overridden={audit['opts_overridden']} OPTs ({audit['units_overridden']:.0f}u) "
        f"by_grid_block={audit['blocks_by_grid']} by_grid_over={audit['overrides_by_grid']}"
    )
    return audit

