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
from typing import Dict, List, Optional
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
RULE_R08_MJ_REQ_BOOSTED   = True   # skip RL/TBC when boosted MJ_REQ < ACS_D/2 (only when PRI gate is off)
RULE_R09_TBL_TRIVIAL      = True

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
    opt_types: Optional[List[str]] = None,  # restrict waterfall to these OPT_TYPEs only (default = all)
) -> Dict:
    """
    Orchestrates Stages A–D. See docs/NEW_RULE_ENGINE_SPEC.md.
    """
    t0 = time.time()
    # Audit-log the PRI/MBQ-cap gate state actually received by the engine.
    # Helps diagnose UI-toggle vs. server-state mismatches.
    logger.info(
        f"[rule_engine_new] "
        f"RL: {'PRI>=100 strict' if pri_ct_check_rl else f'MBQ-cap {rl_mbq_cap_pct}%'} | "
        f"TBC: {'PRI>=100 strict' if pri_ct_check_tbc else f'MBQ-cap {tbc_mbq_cap_pct}%'}"
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
                         tbc_mbq_cap_pct=tbc_mbq_cap_pct)
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
                        opt_types=opt_types)
    # Apply MBQ cap for OPT_TYPEs whose PRI gate is disabled.
    if (not pri_ct_check_rl) and rl_mbq_cap_pct > 0:
        _stage_c_apply_mbq_cap(conn, alloc_table, working_table, 'RL', rl_mbq_cap_pct)
    if (not pri_ct_check_tbc) and tbc_mbq_cap_pct > 0:
        _stage_c_apply_mbq_cap(conn, alloc_table, working_table, 'TBC', tbc_mbq_cap_pct)

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
                          tbc_mbq_cap_pct: float = 0.0):
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
    if RULE_R09_TBL_TRIVIAL:
        pieces.append(
            f"CASE WHEN ISNULL([OPT_TYPE],'') = 'TBL' "
            f"      AND ISNULL([MJ_REQ],0) < {tbl_trivial_factor} * ISNULL([ACS_D],0) "
            f"      THEN 'R09_TBL_TRIVIAL;' ELSE '' END"
        )
    # R08: boosted-MJ_REQ skip — only active when PRI gate is off for a type
    # and a cap_pct is configured. Boosted req = MJ_MBQ×cap% − MJ_STK_TTL;
    # if that gap is smaller than ACS_SKIP_FACTOR×ACS_D, the store is already
    # near its capped target and listing this OPT adds no meaningful value.
    if RULE_R08_MJ_REQ_BOOSTED:
        if (not pri_ct_check_rl) and rl_mbq_cap_pct > 0:
            rl_factor = rl_mbq_cap_pct / 100.0
            pieces.append(
                f"CASE WHEN ISNULL([OPT_TYPE],'') = 'RL' "
                f"      AND ISNULL([MJ_MBQ],0) * {rl_factor} - ISNULL([MJ_STK_TTL],0) "
                f"          < {ACS_SKIP_FACTOR} * ISNULL(NULLIF([ACS_D],0), 1) "
                f"     THEN 'R08_MJ_REQ_BOOSTED;' ELSE '' END"
            )
        if (not pri_ct_check_tbc) and tbc_mbq_cap_pct > 0:
            tbc_factor = tbc_mbq_cap_pct / 100.0
            pieces.append(
                f"CASE WHEN ISNULL([OPT_TYPE],'') = 'TBC' "
                f"      AND ISNULL([MJ_MBQ],0) * {tbc_factor} - ISNULL([MJ_STK_TTL],0) "
                f"          < {ACS_SKIP_FACTOR} * ISNULL(NULLIF([ACS_D],0), 1) "
                f"     THEN 'R08_MJ_REQ_BOOSTED;' ELSE '' END"
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
    Per-(store, opt_type) rank. Each store gets its own 1..N priority list
    inside each opt_type bucket (RL, TBC, TBL). NOT a global rank — store
    HB05's rank=1 is independent of HB07's rank=1; they tie at the pool
    and the waterfall's WERKS tie-break decides who ships first.

    Within each (WERKS, OPT_TYPE) partition:
        OPT_PRIORITY_TIER (1=focus-uncapped, 2=focus-capped, 3=regular) ASC,
        SIZE_RATIO DESC,      (VAR_FNL_COUNT/VAR_COUNT — more complete size coverage first)
        SEC_CT% DESC,         (higher contribution % first)
        MAX_DAILY_SALE DESC,  (higher sales velocity first)
        OPT_REQ_WH DESC,      (more required first)

    ST_RANK is NOT used here — it's a store-level rank, constant within a
    single (WERKS, OPT_TYPE) partition, so it can't influence the order.
    """
    _run(conn, f"""
        ;WITH Base AS (
            SELECT *,
                CASE WHEN ISNULL([VAR_COUNT], 0) = 0 THEN 0
                     ELSE CAST(ISNULL([VAR_FNL_COUNT], 0) AS FLOAT) / [VAR_COUNT]
                END AS SIZE_RATIO
            FROM [{working_table}]
            WHERE LISTED_FLAG = 1
        ),
        R AS (
            SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR,
                   ROW_NUMBER() OVER (
                       PARTITION BY [WERKS], ISNULL([OPT_TYPE],'')
                       ORDER BY
                         ISNULL([OPT_PRIORITY_TIER], 3)            ASC,
                         ISNULL([SIZE_RATIO], 0)                    DESC,
                         ISNULL(TRY_CAST([SEC_CT%] AS FLOAT), 0)   DESC,
                         ISNULL([MAX_DAILY_SALE], 0)               DESC,
                         ISNULL([OPT_REQ_WH], 0)                   DESC,
                         -- Deterministic tie-breakers: identity columns so two
                         -- OPTs that tie on all priority columns still get a
                         -- reproducible rank across runs.
                         [MAJ_CAT]                                  ASC,
                         [GEN_ART_NUMBER]                           ASC,
                         ISNULL([CLR], '')                          ASC
                   ) AS rk
            FROM Base
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

    Step 2 — RERANK: surviving rows get fresh OPT_PRIORITY_RANK using the
    same ORDER BY as _stage_a_assign_rank but with live SIZE_RATIO.

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

    # Step 2: re-rank survivors using live SIZE_RATIO.
    _run(conn, f"""
        ;WITH PoolState AS (
            SELECT [GEN_ART_NUMBER], [CLR], [VAR_ART],
                   COUNT(*) AS VAR_COUNT_LIVE,
                   SUM(CASE WHEN ISNULL([FNL_Q_REM], 0) > 0 THEN 1 ELSE 0 END) AS VAR_FNL_LIVE
            FROM [{alloc_table}]
            WHERE [OPT_TYPE] = :next_ot
            GROUP BY [GEN_ART_NUMBER], [CLR], [VAR_ART]
        ),
        Base AS (
            SELECT A.WERKS, A.MAJ_CAT, A.GEN_ART_NUMBER, A.CLR,
                   A.[OPT_PRIORITY_TIER],
                   CASE WHEN ISNULL(P.VAR_COUNT_LIVE, 0) = 0 THEN 0
                        ELSE CAST(ISNULL(P.VAR_FNL_LIVE, 0) AS FLOAT) / P.VAR_COUNT_LIVE
                   END AS SIZE_RATIO,
                   A.[SEC_CT%], A.[MAX_DAILY_SALE], A.[OPT_REQ_WH]
            FROM [{alloc_table}] A
            LEFT JOIN PoolState P
                ON A.GEN_ART_NUMBER = P.GEN_ART_NUMBER
               AND ISNULL(A.CLR,'') = ISNULL(P.CLR,'')
               AND A.VAR_ART = P.VAR_ART
            WHERE A.[OPT_TYPE] = :next_ot
              AND A.[ALLOC_STATUS] IS NULL
        ),
        R AS (
            SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR,
                   ROW_NUMBER() OVER (
                       PARTITION BY [WERKS]
                       ORDER BY
                         ISNULL([OPT_PRIORITY_TIER], 3)           ASC,
                         ISNULL([SIZE_RATIO], 0)                   DESC,
                         ISNULL(TRY_CAST([SEC_CT%] AS FLOAT), 0)  DESC,
                         ISNULL([MAX_DAILY_SALE], 0)              DESC,
                         ISNULL([OPT_REQ_WH], 0)                  DESC,
                         -- Deterministic tie-breakers — identity columns
                         -- guarantee a reproducible rank across runs.
                         [MAJ_CAT]                                ASC,
                         [GEN_ART_NUMBER]                         ASC,
                         ISNULL([CLR], '')                        ASC
                   ) AS rk
            FROM Base
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


def _stage_a_materialize_listed(conn, working_table, listed_table) -> int:
    _run(conn, f"IF OBJECT_ID('{listed_table}','U') IS NOT NULL DROP TABLE [{listed_table}]")
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
          AND ISNULL(TRY_CAST(L.[MJ_REQ] AS FLOAT), 0)
              > 0.5 * ISNULL(NULLIF(TRY_CAST(L.[ACS_D] AS FLOAT), 0), 18.0)
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
            SZ_MBQ    = ROUND(ISNULL(OPT_MBQ,    0) * ISNULL(CONT, 0), 0),
            SZ_MBQ_WH = ROUND(ISNULL(OPT_MBQ_WH, 0) * ISNULL(CONT, 0), 0),
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

    # (1) Reduce MSA_FNL_Q_REM per OPT — this band's SHIP+HOLD
    _run(conn, f"""
        ;WITH OptTake AS (
            SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR,
                   SUM(ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0)) AS take_total,
                   SUM(ISNULL(ROUND_SHIP,0)) AS take_ship
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
                ELSE ISNULL(W.MSA_FNL_Q_REM, 0) - O.take_total END
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
    _run(conn, f"""
        UPDATE [{working_table}] WITH (ROWLOCK, UPDLOCK) SET
            ALLOC_STATUS = CASE
                WHEN ISNULL(MSA_FNL_Q_REM, 0) <= 0 THEN 'SKIPPED'
                WHEN ISNULL(PRI_CT_REM, 0)    < 100
                     AND ISNULL(OPT_TYPE,'') IN ({pri_opt_in}) THEN 'SKIPPED'
                ELSE ALLOC_STATUS END,
            ALLOC_REMARKS = CASE
                WHEN ISNULL(MSA_FNL_Q_REM, 0) <= 0
                    THEN ISNULL(ALLOC_REMARKS,'')
                         + ' SKIP_MSA_EXHAUSTED(rem='
                         + CAST(ISNULL(MSA_FNL_Q_REM,0) AS NVARCHAR(20)) + ');'
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
                       ORDER BY ISNULL(T.ST_RANK,999999) ASC, T.OPT_PRIORITY_RANK ASC
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
                    WHEN ISNULL(PRI_CT_REM, 0)    < 100
                         AND ISNULL(OPT_TYPE,'') IN ({pri_opt_in}) THEN 'SKIPPED'
                    ELSE ALLOC_STATUS END,
                ALLOC_REMARKS = CASE
                    WHEN ISNULL(MSA_FNL_Q_REM, 0) <= 0
                        THEN ISNULL(ALLOC_REMARKS,'')
                             + ' SKIP_MSA_EXHAUSTED(rem='
                             + CAST(ISNULL(MSA_FNL_Q_REM,0) AS NVARCHAR(20)) + ');'
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
                                    A.GEN_ART_NUMBER ASC
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
                       WHEN O.cum_ship - O.SHIP_QTY >= O.budget THEN 'MBQ_CAP'
                       ELSE A.SKIP_REASON END
            FROM [{alloc_table}] A
            INNER JOIN Ordered O
                ON A.WERKS          = O.WERKS
               AND A.MAJ_CAT        = O.MAJ_CAT
               AND A.GEN_ART_NUMBER = O.GEN_ART_NUMBER
               AND ISNULL(A.CLR,'') = ISNULL(O.CLR,'')
               AND A.VAR_ART        = O.VAR_ART
               AND A.SZ             = O.SZ
            WHERE A.OPT_TYPE = :ot
        """, {"cap": float(cap_pct), "ot": opt_type})
    except Exception as e:
        logger.warning(f"[C] MBQ cap ({opt_type}) failed: {e}")


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
                        opt_types: Optional[List[str]] = None):
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

    # Global MJ_REQ cap: SUM(SHIP_QTY across all OPTs) must not exceed MJ_REQ
    # per (WERKS, MAJ_CAT). Without this, each OPT allocates independently up
    # to OPT_MBQ − OPT_STK, and their sum routinely exceeds MJ_MBQ − MJ_STK_TTL.
    # Excess is trimmed from lowest-priority rows (TBL > TBC > RL, then by
    # OPT_PRIORITY_RANK DESC, ST_RANK DESC) to preserve high-priority allocations.
    if working_table:
        _stage_c_apply_mj_req_cap(conn, alloc_table, working_table)

    # Finalise: copy SHIP_QTY to ALLOC_QTY
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
                WHEN SHIP_QTY = 0 AND HOLD_QTY = 0
                     AND CASE WHEN OPT_TYPE='TBL'
                              THEN ISNULL(SZ_MBQ_WH,0)+(ISNULL(I_ROD,1)-1)*ISNULL(SZ_MBQ,0)
                              ELSE ISNULL(I_ROD,1)*ISNULL(SZ_MBQ,0) END
                          - ISNULL(SZ_STK,0) <= 0
                     THEN 'ALREADY_STOCKED'
                WHEN SHIP_QTY = 0 AND HOLD_QTY = 0 THEN 'NO_POOL_OR_DEMAND'
                ELSE SKIP_REASON END
    """)


def _stage_c_apply_mj_req_cap(conn, alloc_table: str, working_table: str) -> None:
    """
    Global post-waterfall cap: for each (WERKS, MAJ_CAT), ensure
    SUM(SHIP_QTY across all OPTs and sizes) ≤ MJ_REQ = MJ_MBQ − MJ_STK_TTL.

    Without this, each OPT is allocated to OPT_MBQ − OPT_STK independently.
    With n OPTs, their sum frequently exceeds the store's net requirement.

    Trim strategy: excess is removed from the lowest-priority rows first
    (TBL before TBC before RL, then by OPT_PRIORITY_RANK DESC, ST_RANK DESC,
    GEN_ART_NUMBER DESC) — the same order as allocation priority but reversed,
    so the first-allocated high-value OPTs are protected.

    Rows trimmed to zero get ALLOC_STATUS='SKIPPED', SKIP_REASON='MJ_REQ_CAP'.
    Partially trimmed rows keep their partial SHIP_QTY and stay PARTIAL.
    """
    try:
        _run(conn, f"""
            ;WITH Budget AS (
                -- MJ_REQ = MJ_MBQ − MJ_STK_TTL (clamped at 0)
                SELECT W.WERKS, W.MAJ_CAT,
                       ISNULL(MAX(ISNULL(W.MJ_MBQ, 0)) - MAX(ISNULL(W.MJ_STK_TTL, 0)), 0) AS budget
                FROM [{working_table}] W
                WHERE W.LISTED_FLAG = 1
                GROUP BY W.WERKS, W.MAJ_CAT
                HAVING MAX(ISNULL(W.MJ_MBQ, 0)) - MAX(ISNULL(W.MJ_STK_TTL, 0)) > 0
            ),
            Ordered AS (
                -- Cumulative SHIP_QTY in reverse-priority order
                -- (lowest-priority rows eat into the budget last)
                SELECT A.WERKS, A.MAJ_CAT, A.GEN_ART_NUMBER, A.CLR,
                       A.VAR_ART, A.SZ, A.SHIP_QTY,
                       SUM(A.SHIP_QTY) OVER (
                           PARTITION BY A.WERKS, A.MAJ_CAT
                           ORDER BY
                               CASE A.OPT_TYPE WHEN 'RL' THEN 1 WHEN 'TBC' THEN 2 ELSE 3 END ASC,
                               ISNULL(A.OPT_PRIORITY_RANK, 999999) ASC,
                               ISNULL(A.ST_RANK, 999999) ASC,
                               A.GEN_ART_NUMBER ASC
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) AS cum_ship,
                       B.budget
                FROM [{alloc_table}] A
                INNER JOIN Budget B
                    ON A.WERKS = B.WERKS AND A.MAJ_CAT = B.MAJ_CAT
                WHERE ISNULL(A.SHIP_QTY, 0) > 0
            )
            UPDATE A SET
                A.SHIP_QTY    = CASE
                    WHEN O.cum_ship <= O.budget                     THEN O.SHIP_QTY
                    WHEN O.cum_ship - O.SHIP_QTY >= O.budget        THEN 0
                    ELSE O.budget - (O.cum_ship - O.SHIP_QTY) END,
                A.ALLOC_STATUS = CASE
                    WHEN O.cum_ship - O.SHIP_QTY >= O.budget THEN 'SKIPPED'
                    ELSE A.ALLOC_STATUS END,
                A.SKIP_REASON  = CASE
                    WHEN O.cum_ship - O.SHIP_QTY >= O.budget THEN 'MJ_REQ_CAP'
                    ELSE A.SKIP_REASON END
            FROM [{alloc_table}] A
            INNER JOIN Ordered O
                ON A.WERKS          = O.WERKS
               AND A.MAJ_CAT        = O.MAJ_CAT
               AND A.GEN_ART_NUMBER = O.GEN_ART_NUMBER
               AND ISNULL(A.CLR,'') = ISNULL(O.CLR,'')
               AND A.VAR_ART        = O.VAR_ART
               AND A.SZ             = O.SZ
            WHERE ISNULL(A.SHIP_QTY, 0) > 0
        """)
        capped = conn.execute(text(f"""
            SELECT COUNT(*) FROM [{alloc_table}] WHERE SKIP_REASON = 'MJ_REQ_CAP'
        """)).scalar()
        if int(capped or 0) > 0:
            logger.info(f"[C] MJ_REQ cap: trimmed {capped} rows")

        # Rows cancelled by the cap (SHIP=0, HOLD=0) must not hold pool consumption.
        # Reset POOL_CONSUMED so FNL_Q_REM is not wrongly reduced for them.
        _run(conn, f"""
            UPDATE [{alloc_table}] SET POOL_CONSUMED = 0
            WHERE ISNULL(SHIP_QTY,  0) = 0
              AND ISNULL(HOLD_QTY,  0) = 0
              AND ISNULL(POOL_CONSUMED, 0) > 0
        """)
        # Recompute FNL_Q_REM per pool key from actual (non-zero) pool consumption.
        _run(conn, f"""
            UPDATE A SET A.FNL_Q_REM = ISNULL(A.FNL_Q, 0) - ISNULL(B.consumed, 0)
            FROM [{alloc_table}] A
            LEFT JOIN (
                SELECT [RDC], [MAJ_CAT], [GEN_ART_NUMBER],
                       ISNULL([CLR], '') AS CLR, [VAR_ART], [SZ],
                       SUM(ISNULL([POOL_CONSUMED], 0)) AS consumed
                FROM   [{alloc_table}]
                GROUP  BY [RDC], [MAJ_CAT], [GEN_ART_NUMBER],
                          ISNULL([CLR], ''), [VAR_ART], [SZ]
            ) B ON  A.[RDC]            = B.[RDC]
                AND A.[MAJ_CAT]        = B.[MAJ_CAT]
                AND A.[GEN_ART_NUMBER] = B.[GEN_ART_NUMBER]
                AND ISNULL(A.[CLR],'') = B.[CLR]
                AND A.[VAR_ART]        = B.[VAR_ART]
                AND A.[SZ]             = B.[SZ]
        """)
    except Exception as e:
        logger.warning(f"[C] MJ_REQ cap failed: {e}")


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
                       ORDER BY ISNULL(T.ST_RANK,999999) ASC, T.OPT_PRIORITY_RANK ASC
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
    # row in alloc_table.  All sizes of the same OPT share one sequence number.
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
    _run(conn, f"""
        UPDATE [{working_table}] SET ALLOC_STATUS = 'INELIGIBLE'
        WHERE LISTED_FLAG = 0
    """)
    _run(conn, f"""
        UPDATE [{working_table}] SET ALLOC_STATUS = 'NOT_ALLOCATED',
               ALLOC_REMARKS = ISNULL(ALLOC_REMARKS,'') + ' no pool'
        WHERE LISTED_FLAG = 1
          AND (ALLOC_QTY  IS NULL OR ALLOC_QTY  = 0)
          AND (HOLD_QTY   IS NULL OR HOLD_QTY   = 0)
    """)


def _cleanup(conn):
    _run(conn, f"IF OBJECT_ID('tempdb..{POOL_TABLE}') IS NOT NULL DROP TABLE {POOL_TABLE}")
