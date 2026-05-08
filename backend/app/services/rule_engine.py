"""
Rule Engine — Multi-Wave Grid-Aware Allocator for ARS Replenishment
====================================================================

Drop-in replacement for `listing_allocator.run_multilevel_allocation` that
implements three new rules (per harshalanand, April 2026):

  Rule 1: OPT_TYPE order = RL -> TBC -> TBL.
          Within each OPT_TYPE, run MAX(I_ROD) rounds.
          Round N scales demand to (OPT_MBQ * N * CONT) — same as legacy.

  Rule 2: Waterfall both FNL_Q (pool) and REQ (demand) per grid wave.
          Eligibility walks PRI_CT% and SEC_CT% thresholds in a strict -> loose
          sequence. High-confidence OPTs (100% primary grid coverage) get
          first crack at the pool; less-covered OPTs fall through in later
          waves. Pool is consumed set-wise inside each wave.

  Rule 3: Default pool reservation uses OPT_MBQ_WH (with hold).
          For TBL (new listings, IS_NEW=1):
            - pool is reserved on OPT_MBQ_WH (includes hold days)
            - SHIP_QTY to store = OPT_MBQ (without hold)
            - HOLD_QTY = OPT_MBQ_WH - OPT_MBQ (warehouse buffer for lead time)
          For RL / TBC:
            - SHIP_QTY = POOL_CONSUMED (no hold split; OPT_MBQ_WH == OPT_MBQ)
          ALLOC_QTY is reported as SHIP_QTY on listing_working.

Speed design:
  - One batch SQL per (wave x opt_type x round); all OPTs handled via
    PARTITION BY (RDC, MAJ_CAT, GEN_ART, CLR, VAR_ART, SZ). No Python loops
    over OPTs.
  - `#rule_pool` indexed; `ARS_ALLOC_WORKING` gets a composite index after
    enrichment.
  - Post-round MSA_FNL_Q / OPT_REQ_WH sync runs once per wave (not per
    round) — reduces sync cost by ~MAX(I_ROD)x.
  - Rows already allocated in an earlier wave are filtered via SZ_REQ > 0,
    not a separate "done" scan.

Public entry: run_rule_based_allocation(conn, final_table, alloc_table, ...)
"""
from typing import Dict, List, Optional, Tuple
from sqlalchemy import text
from loguru import logger
import json
import time

from app.utils.db_helpers import run_sql, table_exists, get_columns, ensure_column

_run = run_sql
_exists = table_exists
_cols = get_columns

# ────────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ────────────────────────────────────────────────────────────────────────

OPT_TYPE_ORDER: List[str] = ["RL", "TBC", "TBL"]

# (wave_name, ct_column, threshold_pct, description)
# v3 rule: only PRI_CT% >= 100 options enter the allocator. Relaxed
# secondary waves are disabled — primary-grid fill must already be
# complete at the time of allocation. Kept as a single-entry list so the
# wave-loop structure still holds for future extension.
DEFAULT_WAVES: List[Tuple[str, str, float, str]] = [
    ("PRI_100", "PRI_CT%", 100.0, "Primary grid 100% coverage (only eligible wave)"),
]

POOL_TABLE = "#rule_pool"
BREAK_TABLE = "#rule_break"

# Priority-rank band size for the allocator loop. Smaller band = closer to
# per-option greedy semantics (more accurate); larger band = fewer SQL
# batches (faster). v3: lowered from 25 → 5 to approximate one-OPT-at-a-time
# revalidation (user requirement). Within a band, the per-variant-size
# waterfall still orders by ST_RANK → OPT_PRIORITY_RANK so higher-ranked
# stores take pool first.
PRIORITY_BAND_SIZE = 1

# TBL primary-trivial guard: skip TBL allocation when MJ_REQ (the primary
# master-grid requirement on listing_working) is below this multiple of
# MAX_DAILY_SALE (ACS_D proxy). Prevents over-filling a store whose
# primary need is a fraction of a day's sale.
TBL_TRIVIAL_NEED_FACTOR = 0.5


# ────────────────────────────────────────────────────────────────────────
#  PUBLIC ENTRY POINT
# ────────────────────────────────────────────────────────────────────────

def run_rule_based_allocation(
    conn,
    final_table: str,
    alloc_table: str,
    msa_var_table: str = "ARS_MSA_VAR_ART",
    var_grid_table: str = "ARS_GRID_MJ_VAR_ART",
    cont_table: str = "Master_CONT_SZ",
    size_threshold: float = 0.6,
    waves: Optional[List[Tuple[str, str, float, str]]] = None,
) -> Dict:
    """
    Execute rule-based allocation and return summary stats.

    Parameters
    ----------
    conn           : live SQLAlchemy connection (committed by caller)
    final_table    : ARS_LISTING_WORKING (must already exist)
    alloc_table    : ARS_ALLOC_WORKING (will be rebuilt)
    msa_var_table  : variant-level MSA output (default ARS_MSA_VAR_ART)
    var_grid_table : variant-level grid (for STK_TTL enrichment)
    cont_table     : size contribution table (Master_CONT_SZ)
    size_threshold : size-availability fraction below which an OPT is
                     skipped at the break rank (default 0.6 = 60%)
    waves          : list of (name, ct_col, threshold, desc) tuples. When
                     None, DEFAULT_WAVES is used (PRI 100 -> PRI 80 ->
                     SEC 100 -> SEC 80).
    """
    t0 = time.time()
    waves = waves or DEFAULT_WAVES
    result: Dict = {
        "alloc_rows": 0,
        "ship_qty_total": 0.0,
        "hold_qty_total": 0.0,
        "skipped_opts": 0,
        "waves": [],
        "duration_sec": 0.0,
    }

    if not _exists(conn, final_table) or not _exists(conn, msa_var_table):
        logger.warning(
            f"rule_engine: missing {final_table} or {msa_var_table} — skipping"
        )
        return result

    # ── Step 0: assign OPT_PRIORITY_RANK on listing_working ───────────
    # Drives the order in which options compete for pool. Tier-gated:
    #   Tier 1 = FOCUS_WO_CAP   (uncapped focus stores)
    #   Tier 2 = FOCUS_W_CAP    (capped focus stores)
    #   Tier 3 = rest, sorted by SEC_CT% desc, PRI_CT%=100 first,
    #            MAX_DAILY_SALE desc, OPT_REQ_WH desc
    _assign_opt_priority(conn, final_table)

    # ── Step 1: build ARS_ALLOC_WORKING ───────────────────────────────
    base_rows = _create_alloc_working(conn, final_table, alloc_table, msa_var_table)
    if base_rows == 0:
        logger.info("rule_engine: no rows after join (ALLOC_FLAG=1 & FNL_Q>0)")
        return result
    logger.info(f"rule_engine: base rows = {base_rows}")

    # ── Step 2: enrich (STK_TTL, CONT) ────────────────────────────────
    _enrich_variant_stock(conn, alloc_table, var_grid_table)
    _enrich_size_cont(conn, alloc_table, cont_table)

    # ── Step 3: add tracking columns ──────────────────────────────────
    _add_tracking_columns(conn, alloc_table, final_table)

    # ── Step 4: index for speed ───────────────────────────────────────
    _create_indexes(conn, alloc_table)

    # ── Step 5: pool tracker ──────────────────────────────────────────
    _create_pool(conn, alloc_table)

    # ── Step 6: base eligibility (MIX / unlisted / zero-MSA) ──────────
    _mark_base_eligibility(conn, final_table)

    # ── Step 6.5: discover primary grids for per-rank revalidation ────
    primary_grids = _discover_primary_grids(conn)
    logger.info(f"rule_engine: primary grids = {list(primary_grids.keys())}")

    # ── Step 6.6: seed _REM shadow columns (preserve originals) ───────
    _init_req_rem_columns(conn, final_table, primary_grids)

    # ── Step 7: run waves ─────────────────────────────────────────────
    for wave_name, ct_col, ct_thr, ct_desc in waves:
        wave_stats = _run_wave(
            conn, alloc_table, final_table,
            wave_name, ct_col, ct_thr, size_threshold,
            primary_grids,
        )
        wave_stats["description"] = ct_desc
        result["waves"].append(wave_stats)
        result["skipped_opts"] += wave_stats.get("skipped", 0)
        # Sync listing_working once per wave (not per round)
        _sync_working_after_wave(conn, final_table, alloc_table)
        logger.info(
            f"rule_engine wave {wave_name}: "
            f"ship={wave_stats['ship']}, hold={wave_stats['hold']}, "
            f"skip={wave_stats['skipped']}, {wave_stats['seconds']}s"
        )

    # ── Step 8: reflect to listing_working ────────────────────────────
    _reflect_to_working(conn, final_table, alloc_table)

    # ── Step 9: totals + cleanup ──────────────────────────────────────
    totals = conn.execute(text(f"""
        SELECT
            COUNT(*) AS row_cnt,
            ISNULL(SUM(ISNULL([SHIP_QTY], 0)), 0) AS ship_total,
            ISNULL(SUM(ISNULL([HOLD_QTY], 0)), 0) AS hold_total
        FROM [{alloc_table}]
        WHERE ISNULL([SHIP_QTY], 0) > 0 OR ISNULL([HOLD_QTY], 0) > 0
    """)).fetchone()

    result["alloc_rows"] = int(totals[0] or 0)
    result["ship_qty_total"] = float(totals[1] or 0)
    result["hold_qty_total"] = float(totals[2] or 0)

    _cleanup(conn)
    result["duration_sec"] = round(time.time() - t0, 1)

    logger.info(
        f"rule_engine DONE: rows={result['alloc_rows']}, "
        f"ship={result['ship_qty_total']:.0f}, "
        f"hold={result['hold_qty_total']:.0f}, "
        f"skipped_opts={result['skipped_opts']}, "
        f"{result['duration_sec']}s"
    )
    return result


# ────────────────────────────────────────────────────────────────────────
#  PRIMARY-GRID DISCOVERY  (for per-rank PRI_CT% revalidation)
# ────────────────────────────────────────────────────────────────────────

_SKIP_ART = {"GEN_ART_NUMBER", "ARTICLE_NUMBER", "GEN_ART", "VAR_ART"}


def _discover_primary_grids(conn) -> Dict[str, List[str]]:
    """
    Returns {grid_name_upper: [hierarchy_columns_upper]} for grids where
    grid_group='Primary' in ARS_GRID_BUILDER. Article-level grids skipped.
    MJ is always included with ['MAJ_CAT'] as the default primary grid.
    """
    result: Dict[str, List[str]] = {"MJ": ["MAJ_CAT"]}
    if not _exists(conn, "ARS_GRID_BUILDER"):
        return result
    try:
        rows = conn.execute(text("""
            SELECT grid_name, hierarchy_columns, ISNULL(grid_group, 'Primary')
            FROM [ARS_GRID_BUILDER]
            WHERE UPPER(status) = 'ACTIVE'
            ORDER BY grid_name
        """)).fetchall()
    except Exception as e:
        logger.warning(f"_discover_primary_grids: {e}")
        return result

    for grid_name, hier_json, grid_group in rows:
        if str(grid_group).strip().lower() != "primary":
            continue
        try:
            hier = json.loads(hier_json) if isinstance(hier_json, str) else hier_json
        except Exception:
            continue
        if not hier:
            continue
        if any(str(x).upper() in _SKIP_ART for x in hier):
            continue
        result[str(grid_name).upper()] = [str(h).upper() for h in hier]
    return result


# ────────────────────────────────────────────────────────────────────────
#  INIT REM SHADOW COLUMNS  (preserve originals, mutate _REM versions)
# ────────────────────────────────────────────────────────────────────────

def _init_req_rem_columns(conn, final_table,
                            primary_grids: Dict[str, List[str]]):
    """
    Create parallel _REM columns on ARS_LISTING_WORKING and seed them from
    the originals at the start of the allocation run. rule_engine then
    mutates only the _REM columns; originals stay intact for audit:

      <grid>_REQ       (original, never mutated during allocation)
      <grid>_REQ_REM   (decremented as ship_qty consumes grid capacity)

      H_<grid>         (original, never mutated)
      H_<grid>_REM     (recomputed = (REQ_REM > 0 AND GH_<grid> = 1) ? 1 : 0)

      PRI_CT%          (original, never mutated)
      PRI_CT_REM       (recomputed = SUM(H_REM) / SUM(GH) * 100)

    Columns are added idempotently (try/except on duplicate). Seeding only
    overwrites rows where the _REM column is NULL so reruns preserve
    in-progress state if needed.
    """
    final_cols = {c.upper() for c in _cols(conn, final_table)}

    # Per-grid REQ_REM + H_REM
    for grid_name in primary_grids:
        req_col = f"{grid_name}_REQ"
        req_rem = f"{req_col}_REM"
        h_col = f"H_{grid_name}"
        h_rem = f"{h_col}_REM"

        if req_col.upper() in final_cols:
            try:
                _run(conn, f"ALTER TABLE [{final_table}] ADD [{req_rem}] FLOAT NULL")
            except Exception:
                pass
            _run(conn, f"""
                UPDATE [{final_table}]
                SET [{req_rem}] = TRY_CAST([{req_col}] AS FLOAT)
            """)

        if h_col.upper() in final_cols:
            try:
                _run(conn, f"ALTER TABLE [{final_table}] ADD [{h_rem}] INT NULL")
            except Exception:
                pass
            _run(conn, f"""
                UPDATE [{final_table}]
                SET [{h_rem}] = TRY_CAST([{h_col}] AS INT)
            """)

    # PRI_CT_REM on listing_working (original PRI_CT% stays as-is)
    if "PRI_CT%" in final_cols:
        try:
            _run(conn, f"ALTER TABLE [{final_table}] ADD [PRI_CT_REM] FLOAT NULL")
        except Exception:
            pass
        _run(conn, f"""
            UPDATE [{final_table}]
            SET [PRI_CT_REM] = TRY_CAST([PRI_CT%] AS FLOAT)
        """)


# ────────────────────────────────────────────────────────────────────────
#  PRE-ALLOCATION SKIP GATE  (strict, no partial allocation)
# ────────────────────────────────────────────────────────────────────────

def _skip_options_exceeding_grid(conn, alloc_table, final_table, opt_type,
                                   band_start, band_end,
                                   primary_grids: Dict[str, List[str]]):
    """
    Strict pre-allocation gate for the current band. No partial allocation
    is allowed — either an option fits fully within every primary grid it
    belongs to, or it's SKIPPED entirely and the next rank is tried.

    Two rules (per harshalanand's spec):

      Rule A (STORE skip):
        If MJ_REQ_REM < 0.5 × ACS_D for this (WERKS, MAJ_CAT), mark
        STORE_BROKEN=1. All remaining options in this store+opt_type are
        dropped for the rest of the run.

      Rule B (OPTION skip — grid overflow):
        For each primary grid G the option belongs to:
            SUM(SZ_POOL_REQ) per option    <= MAX(G_REQ_REM) at G's grain
        If the option's total pool demand would exceed ANY primary grid's
        remaining REQ, SKIP the option:
            SKIP_FLAG    = 1
            ALLOC_STATUS = 'SKIPPED'
            SKIP_REASON  = 'GRID_OVERFLOW[<grid>]; '

    Called at the top of every band iteration so the gate sees the
    updated REQ values written by the previous rank's revalidation.
    """
    final_cols = {c.upper() for c in _cols(conn, final_table)}
    alloc_cols = {c.upper() for c in _cols(conn, alloc_table)}

    # Rule A (STORE_BROKEN when MJ_REQ_REM < 0.5 × ACS_D) is intentionally
    # NOT run here — doing so at the top of band 1 would break stores that
    # haven't allocated yet, making their full MJ_REQ never get a chance.
    # _revalidate_after_rank handles this check POST-allocation so a store
    # only breaks AFTER some rank has shipped.

    # ── Rule B: skip options whose total demand exceeds any grid REM ──
    #    Reads <grid>_REQ_REM (updated by revalidation), not the original
    #    <grid>_REQ. Falls back to original if _REM column is missing.
    for grid_name, hier in primary_grids.items():
        req_col = f"{grid_name}_REQ"
        req_rem = f"{req_col}_REM"
        req_read = req_rem if req_rem.upper() in final_cols else req_col
        if req_read.upper() not in final_cols:
            continue
        extra = [h for h in hier
                 if h.upper() not in ("MAJ_CAT", "WERKS")
                 and h.upper() in alloc_cols and h.upper() in final_cols]
        grid_keys = ["WERKS", "MAJ_CAT"] + extra
        opt_keys = grid_keys + ["GEN_ART_NUMBER", "CLR"]
        grid_cols_sql = ", ".join(f"[{k}]" for k in grid_keys)
        opt_cols_sql = ", ".join(f"[{k}]" for k in opt_keys)
        opt_join = " AND ".join(
            f"(ISNULL(A.[{k}],'') = ISNULL(D.[{k}],''))" if k == "CLR"
            else f"A.[{k}] = D.[{k}]"
            for k in opt_keys
        )
        grid_join = " AND ".join(f"G.[{k}] = D.[{k}]" for k in grid_keys)

        _run(conn, f"""
            ;WITH OptDemand AS (
                SELECT {opt_cols_sql},
                       SUM(ISNULL([SZ_POOL_REQ], 0)) AS total_demand
                FROM [{alloc_table}]
                WHERE [OPT_TYPE] = :ot
                  AND ISNULL([OPT_PRIORITY_RANK], 0) BETWEEN :bs AND :be
                  AND ISNULL([SKIP_FLAG], 0) = 0
                  AND ISNULL([STORE_BROKEN], 0) = 0
                GROUP BY {opt_cols_sql}
                HAVING SUM(ISNULL([SZ_POOL_REQ], 0)) > 0
            ),
            GridRem AS (
                SELECT {grid_cols_sql},
                       MAX(ISNULL(TRY_CAST([{req_read}] AS FLOAT), 0)) AS grid_rem
                FROM [{final_table}]
                GROUP BY {grid_cols_sql}
            )
            UPDATE A SET
                A.[SKIP_FLAG] = 1,
                A.[ALLOC_STATUS] = 'SKIPPED',
                A.[SKIP_REASON] = CONCAT(
                    ISNULL(A.[SKIP_REASON], ''),
                    'GRID_OVERFLOW[{grid_name}]:demand=',
                    CAST(CAST(D.total_demand AS INT) AS NVARCHAR(20)),
                    ',rem=', CAST(CAST(G.grid_rem AS INT) AS NVARCHAR(20)), '; ')
            FROM [{alloc_table}] A
            INNER JOIN OptDemand D ON {opt_join}
            INNER JOIN GridRem G ON {grid_join}
            WHERE A.[OPT_TYPE] = :ot
              AND ISNULL(A.[OPT_PRIORITY_RANK], 0) BETWEEN :bs AND :be
              AND ISNULL(A.[SKIP_FLAG], 0) = 0
              AND ISNULL(A.[STORE_BROKEN], 0) = 0
              AND D.total_demand > G.grid_rem
        """, {"ot": opt_type, "bs": band_start, "be": band_end})


# ────────────────────────────────────────────────────────────────────────
#  REVALIDATE AFTER EACH RANK  (per-option PRI_CT% + store-skip gate)
# ────────────────────────────────────────────────────────────────────────

def _revalidate_after_rank(conn, alloc_table, final_table, opt_type,
                             current_rank, wave_name, round_n,
                             primary_grids: Dict[str, List[str]]):
    """
    Called after STEP D.2 of each band (BAND_SIZE=1 => after each rank).

    Workflow per rank:
      1. For each primary grid, deduct this rank's SHIP_QTY from
         <grid>_REQ on listing_working at the grid's natural grain.
      2. Recompute H_<grid> = (REQ > 0 ? 1 : 0) on listing_working.
      3. Recompute PRI_CT% from SUM(H_pri) / SUM(GH_pri) * 100.
      4. Mirror PRI_CT% to alloc_working for future ranks.
      5. Mark SKIP_FLAG=1 on future-rank alloc rows where PRI_CT% < 100.
      6. Mark STORE_BROKEN=1 where MJ_REQ <= 0 OR MJ_REQ <= 0.5 * ACS_D.
    """
    alloc_wave_tag = f"{wave_name}_R{round_n}"
    final_cols = {c.upper() for c in _cols(conn, final_table)}
    alloc_cols = {c.upper() for c in _cols(conn, alloc_table)}

    # 1) Deduct SHIP_QTY from each primary grid's _REM column (originals
    #    are preserved; _REM is what tracks remaining capacity).
    for grid_name, hier in primary_grids.items():
        req_col = f"{grid_name}_REQ"
        req_rem = f"{req_col}_REM"
        if req_rem.upper() not in final_cols:
            continue
        extra = [h for h in hier
                 if h.upper() not in ("MAJ_CAT", "WERKS")
                 and h.upper() in alloc_cols and h.upper() in final_cols]
        keys = ["WERKS", "MAJ_CAT"] + extra
        cte_select = ", ".join(f"A.[{k}]" for k in keys)
        cte_group = ", ".join(f"A.[{k}]" for k in keys)
        join_on = " AND ".join(f"W.[{k}] = S.[{k}]" for k in keys)

        _run(conn, f"""
            ;WITH ShippedAtRank AS (
                SELECT {cte_select},
                       SUM(ISNULL(A.[ROUND_SHIP], 0)) AS ship_qty
                FROM [{alloc_table}] A
                WHERE A.[OPT_TYPE] = :ot
                  AND ISNULL(A.[OPT_PRIORITY_RANK], 0) = :rnk
                  AND A.[ALLOC_WAVE] = :wv
                  AND ISNULL(A.[ROUND_SHIP], 0) > 0
                GROUP BY {cte_group}
                HAVING SUM(ISNULL(A.[ROUND_SHIP], 0)) > 0
            )
            UPDATE W SET W.[{req_rem}] = CASE
                WHEN ISNULL(TRY_CAST(W.[{req_rem}] AS FLOAT), 0) - S.ship_qty > 0
                THEN ISNULL(TRY_CAST(W.[{req_rem}] AS FLOAT), 0) - S.ship_qty
                ELSE 0 END
            FROM [{final_table}] W
            INNER JOIN ShippedAtRank S ON {join_on}
        """, {"ot": opt_type, "rnk": current_rank, "wv": alloc_wave_tag})

    # 2) Recompute H_<grid>_REM = 1 ONLY if (REQ_REM > 0) AND (GH_<grid> = 1)
    for grid_name in primary_grids:
        h_col = f"H_{grid_name}"
        h_rem = f"{h_col}_REM"
        gh_col = f"GH_{grid_name}"
        req_rem = f"{grid_name}_REQ_REM"
        if h_rem.upper() not in final_cols or req_rem.upper() not in final_cols:
            continue
        if gh_col.upper() in final_cols:
            _run(conn, f"""
                UPDATE [{final_table}]
                SET [{h_rem}] = CASE
                    WHEN ISNULL(TRY_CAST([{req_rem}] AS FLOAT), 0) > 0
                         AND ISNULL(TRY_CAST([{gh_col}] AS INT), 0) = 1
                    THEN 1 ELSE 0 END
            """)
        else:
            _run(conn, f"""
                UPDATE [{final_table}]
                SET [{h_rem}] = CASE
                    WHEN ISNULL(TRY_CAST([{req_rem}] AS FLOAT), 0) > 0
                    THEN 1 ELSE 0 END
            """)

    # 3) Recompute PRI_CT_REM on listing_working (SUM(H_REM) / SUM(GH))
    final_cols_now = {c.upper() for c in _cols(conn, final_table)}
    pri_h_rem = [f"H_{g}_REM" for g in primary_grids
                 if f"H_{g}_REM".upper() in final_cols_now]
    pri_gh    = [f"GH_{g}"    for g in primary_grids
                 if f"GH_{g}".upper() in final_cols_now]
    if pri_h_rem and pri_gh and "PRI_CT_REM" in final_cols_now:
        h_sum  = " + ".join(f"ISNULL([{c}], 0)" for c in pri_h_rem)
        gh_sum = " + ".join(f"ISNULL([{c}], 0)" for c in pri_gh)
        _run(conn, f"""
            UPDATE [{final_table}]
            SET [PRI_CT_REM] = CASE
                WHEN ({gh_sum}) = 0 THEN 0
                ELSE ROUND(CAST(({h_sum}) AS FLOAT) / ({gh_sum}) * 100, 1)
            END
        """)

    # 4) Mirror PRI_CT_REM to alloc_working PRI_CT% for future-rank gates
    _run(conn, f"""
        UPDATE A SET A.[PRI_CT%] = TRY_CAST(W.[PRI_CT_REM] AS FLOAT)
        FROM [{alloc_table}] A
        INNER JOIN [{final_table}] W
          ON A.[WERKS] = W.[WERKS]
         AND A.[MAJ_CAT] = W.[MAJ_CAT]
         AND A.[GEN_ART_NUMBER] = W.[GEN_ART_NUMBER]
         AND ISNULL(A.[CLR], '') = ISNULL(W.[CLR], '')
        WHERE A.[OPT_TYPE] = :ot
          AND ISNULL(A.[OPT_PRIORITY_RANK], 0) > :rnk
          AND ISNULL(A.[SKIP_FLAG], 0) = 0
    """, {"ot": opt_type, "rnk": current_rank})

    # 5) SKIP future-rank options whose PRI_CT_REM dropped below 100
    _run(conn, f"""
        UPDATE [{alloc_table}] SET
            [SKIP_FLAG] = 1,
            [ALLOC_STATUS] = CASE
                WHEN ISNULL([SHIP_QTY], 0) > 0 THEN 'PARTIAL' ELSE 'SKIPPED' END,
            [SKIP_REASON] = CONCAT(ISNULL([SKIP_REASON], ''),
                'PRI_CT_REM_DROPPED@RANK=', CAST(:rnk AS NVARCHAR(10)), '; ')
        WHERE [OPT_TYPE] = :ot
          AND ISNULL([OPT_PRIORITY_RANK], 0) > :rnk
          AND ISNULL([SKIP_FLAG], 0) = 0
          AND ISNULL([PRI_CT%], 100) < 100
    """, {"ot": opt_type, "rnk": current_rank})

    # 6) STORE_BROKEN when MJ_REQ_REM <= 0 OR MJ_REQ_REM <= 0.5 * ACS_D
    if "MJ_REQ_REM" in final_cols_now and "ACS_D" in final_cols_now:
        _run(conn, f"""
            UPDATE A SET A.[STORE_BROKEN] = 1
            FROM [{alloc_table}] A
            INNER JOIN (
                SELECT [WERKS], [MAJ_CAT],
                       MAX(ISNULL(TRY_CAST([MJ_REQ_REM] AS FLOAT), 0)) AS mj,
                       MAX(ISNULL(TRY_CAST([ACS_D]      AS FLOAT), 0)) AS acs
                FROM [{final_table}]
                GROUP BY [WERKS], [MAJ_CAT]
            ) S
              ON A.[WERKS] = S.[WERKS]
             AND A.[MAJ_CAT] = S.[MAJ_CAT]
            WHERE A.[OPT_TYPE] = :ot
              AND ISNULL(A.[STORE_BROKEN], 0) = 0
              AND (
                    S.mj <= 0
                 OR (S.acs > 0 AND S.mj <= 0.5 * S.acs)
              )
        """, {"ot": opt_type})


# ────────────────────────────────────────────────────────────────────────
#  STEP 0 — ASSIGN OPT_PRIORITY_RANK (drives allocation order)
# ────────────────────────────────────────────────────────────────────────

def _assign_opt_priority(conn, final_table):
    """
    Write OPT_PRIORITY_TIER and OPT_PRIORITY_RANK onto ARS_LISTING_WORKING.

    TIER = 1 (FOCUS_WO_CAP=1), 2 (FOCUS_W_CAP=1), 3 (rest).
    RANK = ROW_NUMBER() within each (store, opt_type), ordered by:
        TIER ASC,
        [SEC_CT%] DESC,
        (PRI_CT%=100 first),
        MAX_DAILY_SALE DESC,
        OPT_REQ_WH DESC.

    Partitioning by (WERKS, OPT_TYPE) gives RL, TBC, and TBL each their own
    rank sequence starting at 1 per store. The allocator loops opt_types
    separately (RL -> TBC -> TBL) so ranks inside each sequence are what
    it actually walks.
    """
    for col, typedef in (
        ("OPT_PRIORITY_TIER", "INT NULL DEFAULT 3"),
        ("OPT_PRIORITY_RANK", "INT NULL DEFAULT 0"),
    ):
        try:
            _run(conn, f"ALTER TABLE [{final_table}] ADD [{col}] {typedef}")
        except Exception:
            pass

    _run(conn, f"""
        ;WITH Ranked AS (
            SELECT
                [WERKS], [OPT_TYPE], [MAJ_CAT], [GEN_ART_NUMBER], [CLR],
                CASE
                    WHEN ISNULL(TRY_CAST([FOCUS_WO_CAP] AS INT), 0) = 1 THEN 1
                    WHEN ISNULL(TRY_CAST([FOCUS_W_CAP]  AS INT), 0) = 1 THEN 2
                    ELSE 3
                END AS tier,
                ROW_NUMBER() OVER (
                    PARTITION BY [WERKS], [OPT_TYPE], [ST_RANK]
                    ORDER BY
                        CASE
                            WHEN ISNULL(TRY_CAST([FOCUS_WO_CAP] AS INT), 0) = 1 THEN 1
                            WHEN ISNULL(TRY_CAST([FOCUS_W_CAP]  AS INT), 0) = 1 THEN 2
                            ELSE 3
                        END ASC,
                        ISNULL(TRY_CAST([SEC_CT%] AS FLOAT), 0) DESC,
                        CASE WHEN ISNULL(TRY_CAST([PRI_CT%] AS FLOAT), 0) >= 100
                             THEN 0 ELSE 1 END ASC,
                        ISNULL(TRY_CAST([MAX_DAILY_SALE] AS FLOAT), 0) DESC,
                        ISNULL(TRY_CAST([OPT_REQ_WH]     AS FLOAT), 0) DESC,
                        [MAJ_CAT], [GEN_ART_NUMBER], [CLR]
                ) AS rnk
            FROM [{final_table}]
            WHERE ISNULL([ALLOC_FLAG], 0) = 1
        )
        UPDATE L SET
            L.[OPT_PRIORITY_TIER] = R.tier,
            L.[OPT_PRIORITY_RANK] = R.rnk
        FROM [{final_table}] L
        INNER JOIN Ranked R
            ON  L.[WERKS] = R.[WERKS]
            AND ISNULL(L.[OPT_TYPE], '') = ISNULL(R.[OPT_TYPE], '')
            AND L.[MAJ_CAT] = R.[MAJ_CAT]
            AND L.[GEN_ART_NUMBER] = R.[GEN_ART_NUMBER]
            AND L.[CLR] = R.[CLR]
    """)


# ────────────────────────────────────────────────────────────────────────
#  STEP 1 — BUILD ARS_ALLOC_WORKING (variant-grain from listing x MSA)
# ────────────────────────────────────────────────────────────────────────

def _create_alloc_working(conn, final_table, alloc_table, msa_var_table) -> int:
    """Drop + rebuild alloc_table as listing_working x MSA_VAR_ART."""
    _run(conn, f"IF OBJECT_ID('{alloc_table}','U') IS NOT NULL DROP TABLE [{alloc_table}]")
    _run(conn, f"""
        SELECT
            W.[WERKS], W.[RDC], W.[MAJ_CAT], W.[GEN_ART_NUMBER], W.[CLR],
            W.[GEN_ART_DESC], W.[OPT_TYPE], W.[ST_RANK], W.[IS_NEW],
            W.[I_ROD],
            W.[OPT_MBQ], W.[OPT_REQ],
            ISNULL(W.[OPT_MBQ_WH], W.[OPT_MBQ]) AS OPT_MBQ_WH,
            ISNULL(W.[OPT_REQ_WH], W.[OPT_REQ]) AS OPT_REQ_WH,
            W.[MAX_DAILY_SALE], W.[ALLOC_FLAG],
            W.[PRI_CT%], W.[SEC_CT%],
            /* MJ_REQ carried through as the primary master-grid REQ proxy
               used by the TBL trivial-need guard (skip when MJ_REQ <
               0.5 * MAX_DAILY_SALE). Read-only here — never mutated. */
            ISNULL(TRY_CAST(W.[MJ_REQ] AS FLOAT), 0) AS MJ_REQ_ORG,
            ISNULL(W.[OPT_PRIORITY_TIER], 3) AS OPT_PRIORITY_TIER,
            ISNULL(W.[OPT_PRIORITY_RANK], 0) AS OPT_PRIORITY_RANK,
            V.[ARTICLE_NUMBER] AS VAR_ART,
            V.[ARTICLE_DESC]   AS VAR_DESC,
            V.[SZ], V.[MRP], V.[PAK_SZ],
            TRY_CAST(V.[FNL_Q]    AS FLOAT) AS FNL_Q,
            TRY_CAST(V.[STK_QTY]  AS FLOAT) AS STK_QTY,
            TRY_CAST(V.[PEND_QTY] AS FLOAT) AS PEND_QTY,
            V.[RDC] AS VAR_RDC, V.[FAB] AS VAR_FAB, V.[SSN] AS VAR_SSN
        INTO [{alloc_table}]
        FROM [{final_table}] W
        INNER JOIN [{msa_var_table}] V WITH (NOLOCK)
            ON  W.[MAJ_CAT] = LTRIM(RTRIM(CAST(V.[MAJ_CAT] AS NVARCHAR(200))))
            AND W.[GEN_ART_NUMBER] = TRY_CAST(TRY_CAST(V.[GEN_ART_NUMBER] AS FLOAT) AS BIGINT)
            AND W.[CLR] = LTRIM(RTRIM(CAST(V.[CLR] AS NVARCHAR(200))))
            AND LTRIM(RTRIM(CAST(W.[RDC] AS NVARCHAR(50))))
                = LTRIM(RTRIM(CAST(V.[RDC] AS NVARCHAR(50))))
        WHERE W.[ALLOC_FLAG] = 1
          AND TRY_CAST(V.[FNL_Q] AS FLOAT) > 0
    """)
    cnt = conn.execute(text(f"SELECT COUNT(*) FROM [{alloc_table}]")).scalar()
    return int(cnt or 0)


# ────────────────────────────────────────────────────────────────────────
#  STEP 2 — ENRICH (STK_TTL, CONT)
# ────────────────────────────────────────────────────────────────────────

def _enrich_variant_stock(conn, alloc_table, var_grid_table):
    """Bring variant-level STK_TTL from the variant grid."""
    try:
        _run(conn, f"ALTER TABLE [{alloc_table}] ADD [STK_TTL] FLOAT NULL")
    except Exception:
        pass

    if _exists(conn, var_grid_table):
        gcols = {c.upper() for c in _cols(conn, var_grid_table)}
        var_col = next(
            (c for c in ("VAR_ART", "ARTICLE_NUMBER", "GEN_ART") if c in gcols),
            None,
        )
        if {"STK_TTL", "WERKS", "MAJ_CAT"}.issubset(gcols) and var_col:
            _run(conn, f"""
                UPDATE A SET A.[STK_TTL] = TRY_CAST(G.[STK_TTL] AS FLOAT)
                FROM [{alloc_table}] A
                INNER JOIN [{var_grid_table}] G WITH (NOLOCK)
                    ON G.[WERKS] = A.[WERKS]
                   AND G.[MAJ_CAT] = A.[MAJ_CAT]
                   AND TRY_CAST(G.[{var_col}] AS BIGINT) = TRY_CAST(A.[VAR_ART] AS BIGINT)
            """)
    _run(conn, f"UPDATE [{alloc_table}] SET [STK_TTL] = 0 WHERE [STK_TTL] IS NULL")


def _enrich_size_cont(conn, alloc_table, cont_table):
    """CONT from Master_CONT_SZ: ST-level -> CO-level -> auto (1/N) fallback."""
    for col in ("CONT",):
        try:
            _run(conn, f"ALTER TABLE [{alloc_table}] ADD [{col}] FLOAT NULL")
        except Exception:
            pass

    if _exists(conn, cont_table):
        _run(conn, f"""
            UPDATE A SET A.[CONT] = TRY_CAST(M.[CONT] AS FLOAT)
            FROM [{alloc_table}] A
            INNER JOIN [{cont_table}] M WITH (NOLOCK)
                ON  LTRIM(RTRIM(CAST(M.[ST_CD]  AS NVARCHAR(50))))  = LTRIM(RTRIM(CAST(A.[WERKS] AS NVARCHAR(50))))
                AND LTRIM(RTRIM(CAST(M.[MAJ_CAT] AS NVARCHAR(200)))) = A.[MAJ_CAT]
                AND LTRIM(RTRIM(CAST(M.[SZ]     AS NVARCHAR(200)))) = LTRIM(RTRIM(CAST(A.[SZ] AS NVARCHAR(200))))
        """)
        _run(conn, f"""
            UPDATE A SET A.[CONT] = TRY_CAST(M.[CONT] AS FLOAT)
            FROM [{alloc_table}] A
            INNER JOIN [{cont_table}] M WITH (NOLOCK)
                ON  LTRIM(RTRIM(CAST(M.[ST_CD] AS NVARCHAR(50)))) = 'CO'
                AND LTRIM(RTRIM(CAST(M.[MAJ_CAT] AS NVARCHAR(200)))) = A.[MAJ_CAT]
                AND LTRIM(RTRIM(CAST(M.[SZ] AS NVARCHAR(200)))) = LTRIM(RTRIM(CAST(A.[SZ] AS NVARCHAR(200))))
            WHERE A.[CONT] IS NULL
        """)

    # Auto fallback: uniform 1/N where N = distinct sizes per (WERKS, MAJ_CAT)
    _run(conn, f"""
        ;WITH SzCount AS (
            SELECT [WERKS], [MAJ_CAT], COUNT(DISTINCT [SZ]) AS sz_cnt
            FROM [{alloc_table}]
            GROUP BY [WERKS], [MAJ_CAT]
        )
        UPDATE A SET A.[CONT] = ROUND(1.0 / NULLIF(C.sz_cnt, 0), 4)
        FROM [{alloc_table}] A
        INNER JOIN SzCount C
            ON A.[WERKS] = C.[WERKS] AND A.[MAJ_CAT] = C.[MAJ_CAT]
        WHERE ISNULL(A.[CONT], 0) = 0
    """)


# ────────────────────────────────────────────────────────────────────────
#  STEP 3 — TRACKING COLUMNS
# ────────────────────────────────────────────────────────────────────────

def _add_tracking_columns(conn, alloc_table, final_table):
    """
    ARS_ALLOC_WORKING columns:
      SZ_POOL_MBQ   — pool target for this round (OPT_MBQ_WH * round * CONT)
      SZ_SHIP_MBQ   — ship target (OPT_MBQ * round * CONT for TBL, else = pool)
      SZ_POOL_REQ   — outstanding pool demand this round
      SZ_SHIP_REQ   — outstanding ship demand this round
      POOL_CONSUMED — cumulative pool taken (drives MSA_FNL_Q)
      SHIP_QTY      — cumulative actual ship-to-store (what ALLOC_QTY becomes)
      HOLD_QTY      — cumulative warehouse buffer (TBL only)
      ROUND_POOL / ROUND_SHIP / ROUND_HOLD — per-round deltas
      ALLOC_WAVE    — which wave (and round) committed the row
      ALLOC_ROUND   — last round processed
      SKIP_FLAG     — 1 when OPT broken at size-availability check
      ALLOC_STATUS  — PENDING / PARTIAL / ALLOCATED / SKIPPED / INELIGIBLE
      SKIP_REASON   — free-text diagnostic
    """
    alloc_cols = {
        "SZ_POOL_MBQ":   "FLOAT NULL DEFAULT 0",
        "SZ_SHIP_MBQ":   "FLOAT NULL DEFAULT 0",
        "SZ_POOL_REQ":   "FLOAT NULL DEFAULT 0",
        "SZ_SHIP_REQ":   "FLOAT NULL DEFAULT 0",
        "POOL_CONSUMED": "FLOAT NULL DEFAULT 0",
        "SHIP_QTY":      "FLOAT NULL DEFAULT 0",
        "HOLD_QTY":      "FLOAT NULL DEFAULT 0",
        "ROUND_POOL":    "FLOAT NULL DEFAULT 0",
        "ROUND_SHIP":    "FLOAT NULL DEFAULT 0",
        "ROUND_HOLD":    "FLOAT NULL DEFAULT 0",
        "ALLOC_WAVE":    "NVARCHAR(20) NULL",
        "ALLOC_ROUND":   "INT NULL DEFAULT 0",
        "SKIP_FLAG":     "INT NULL DEFAULT 0",
        "STORE_BROKEN":  "INT NULL DEFAULT 0",
        # ORG vs REM separation — ORG = static snapshot from listing_working,
        # REM = dynamic, refreshed after each band based on pool consumption.
        # PRI_CT%/SEC_CT% (unsuffixed, on alloc_table from the SELECT) are
        # the ORG values. REM versions live here.
        "PRI_CT_REM":    "FLOAT NULL DEFAULT NULL",
        "SEC_CT_REM":    "FLOAT NULL DEFAULT NULL",
        "OPT_REQ_REM":   "FLOAT NULL DEFAULT NULL",
        "ALLOC_STATUS":  "NVARCHAR(50) NULL DEFAULT 'PENDING'",
        "SKIP_REASON":   "NVARCHAR(500) NULL",
    }
    for col, typedef in alloc_cols.items():
        try:
            _run(conn, f"ALTER TABLE [{alloc_table}] ADD [{col}] {typedef}")
        except Exception:
            pass

    _run(conn, f"""
        UPDATE [{alloc_table}] SET
            [SZ_POOL_MBQ]=0, [SZ_SHIP_MBQ]=0,
            [SZ_POOL_REQ]=0, [SZ_SHIP_REQ]=0,
            [POOL_CONSUMED]=0, [SHIP_QTY]=0, [HOLD_QTY]=0,
            [ROUND_POOL]=0, [ROUND_SHIP]=0, [ROUND_HOLD]=0,
            [ALLOC_WAVE]=NULL, [ALLOC_ROUND]=0, [SKIP_FLAG]=0,
            [STORE_BROKEN]=0,
            [PRI_CT_REM]  = TRY_CAST([PRI_CT%] AS FLOAT),
            [SEC_CT_REM]  = TRY_CAST([SEC_CT%] AS FLOAT),
            [OPT_REQ_REM] = ISNULL([OPT_REQ_WH], 0),
            [ALLOC_STATUS]='PENDING', [SKIP_REASON]=NULL
    """)

    # Listing-level columns
    work_cols = {
        "ALLOC_STATUS":  "NVARCHAR(50) NULL DEFAULT 'PENDING'",
        "ALLOC_REMARKS": "NVARCHAR(MAX) NULL",
        "HOLD_QTY":      "FLOAT NULL DEFAULT 0",
    }
    for col, typedef in work_cols.items():
        try:
            _run(conn, f"ALTER TABLE [{final_table}] ADD [{col}] {typedef}")
        except Exception:
            pass
    _run(conn, f"""
        UPDATE [{final_table}]
        SET [ALLOC_STATUS]='PENDING', [ALLOC_REMARKS]='', [HOLD_QTY]=0
    """)


def _create_indexes(conn, alloc_table):
    """
    Two indexes on ARS_ALLOC_WORKING:

      1. CLUSTERED on (WERKS, OPT_TYPE, OPT_PRIORITY_RANK, ST_RANK, ...) —
         physically sorts the table in the order the rule engine walks it.
         Rows end up grouped per store, RL before TBC before TBL, and
         within each opt_type sorted by priority rank. Preview / export
         reads get that same order for free.
      2. NONCLUSTERED on the pool-join key — speeds up the batch CTE.
    """
    try:
        _run(conn, f"""
            CREATE CLUSTERED INDEX CIX_{alloc_table}_priority
            ON [{alloc_table}]
              ([WERKS],
               [OPT_TYPE],
               [OPT_PRIORITY_RANK],
               [ST_RANK],
               [MAJ_CAT], [GEN_ART_NUMBER], [CLR], [VAR_ART], [SZ])
        """)
    except Exception as e:
        logger.debug(f"clustered index create skipped: {e}")

    try:
        _run(conn, f"""
            CREATE NONCLUSTERED INDEX IX_{alloc_table}_pool
            ON [{alloc_table}]
              ([OPT_TYPE], [OPT_PRIORITY_RANK], [RDC], [MAJ_CAT],
               [GEN_ART_NUMBER], [CLR], [VAR_ART], [SZ], [ST_RANK])
            INCLUDE ([WERKS], [OPT_MBQ], [OPT_MBQ_WH], [CONT],
                     [STK_TTL], [I_ROD], [ALLOC_FLAG], [STORE_BROKEN],
                     [PRI_CT%], [SEC_CT%], [IS_NEW])
        """)
    except Exception as e:
        logger.debug(f"nonclustered index create skipped: {e}")


# ────────────────────────────────────────────────────────────────────────
#  STEP 5 — POOL TRACKER
# ────────────────────────────────────────────────────────────────────────

def _create_pool(conn, alloc_table):
    """#rule_pool holds remaining FNL_Q per variant-size."""
    _run(conn, f"IF OBJECT_ID('tempdb..{POOL_TABLE}') IS NOT NULL DROP TABLE {POOL_TABLE}")
    _run(conn, f"""
        SELECT
            [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR], [VAR_ART], [SZ],
            MAX(ISNULL([FNL_Q], 0)) AS FNL_Q_ORIG,
            MAX(ISNULL([FNL_Q], 0)) AS FNL_Q_REM
        INTO {POOL_TABLE}
        FROM [{alloc_table}]
        GROUP BY [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR], [VAR_ART], [SZ]
    """)
    try:
        _run(conn, f"""
            CREATE UNIQUE CLUSTERED INDEX IX_pool_key ON {POOL_TABLE}
              ([RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR], [VAR_ART], [SZ])
        """)
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────────
#  STEP 6 — BASE ELIGIBILITY (once; wave-agnostic rejections)
# ────────────────────────────────────────────────────────────────────────

def _mark_base_eligibility(conn, final_table):
    """
    One-time INELIGIBLE marks — conditions that no wave can rescue:
      - OPT_TYPE = MIX         (no MSA backup by definition)
      - LISTING != 1            (store not carrying category)
      - MSA_FNL_Q <= 0          (warehouse empty)
      - OPT_REQ_WH < 1          (already satisfied / below unit)
      - PRI_CT% < 100           (v3: primary grid not yet complete — this
                                 allocator is a secondary-wave engine;
                                 primary-grid fill happens upstream)

    Rows that pass these are left as PENDING.
    """
    _run(conn, f"""
        UPDATE [{final_table}] SET
            [ALLOC_STATUS] = CASE
                WHEN ISNULL([OPT_TYPE], '') = 'MIX'                       THEN 'INELIGIBLE'
                WHEN ISNULL(TRY_CAST([LISTING] AS INT), 1) != 1           THEN 'INELIGIBLE'
                WHEN ISNULL(TRY_CAST([MSA_FNL_Q] AS FLOAT), 0) <= 0       THEN 'INELIGIBLE'
                WHEN ISNULL(TRY_CAST([OPT_REQ_WH] AS FLOAT), 0) < 1       THEN 'INELIGIBLE'
                WHEN ISNULL(TRY_CAST([PRI_CT%] AS FLOAT), 0) < 100        THEN 'INELIGIBLE'
                ELSE 'PENDING'
            END,
            [ALLOC_REMARKS] = CASE
                WHEN ISNULL([OPT_TYPE], '') = 'MIX'                       THEN 'BASE:MIX; '
                WHEN ISNULL(TRY_CAST([LISTING] AS INT), 1) != 1           THEN 'BASE:LISTING!=1; '
                WHEN ISNULL(TRY_CAST([MSA_FNL_Q] AS FLOAT), 0) <= 0       THEN 'BASE:MSA=0; '
                WHEN ISNULL(TRY_CAST([OPT_REQ_WH] AS FLOAT), 0) < 1       THEN 'BASE:OPT_REQ_WH<1; '
                WHEN ISNULL(TRY_CAST([PRI_CT%] AS FLOAT), 0) < 100        THEN 'BASE:PRI_CT<100; '
                ELSE ''
            END
    """)
    r = conn.execute(text(f"""
        SELECT
            SUM(CASE WHEN [ALLOC_STATUS]='PENDING'    THEN 1 ELSE 0 END),
            SUM(CASE WHEN [ALLOC_STATUS]='INELIGIBLE' THEN 1 ELSE 0 END),
            COUNT(*)
        FROM [{final_table}]
    """)).fetchone()
    logger.info(
        f"rule_engine base eligibility: pending={r[0]}, "
        f"ineligible={r[1]} / total={r[2]}"
    )


# ────────────────────────────────────────────────────────────────────────
#  STEP 7 — WAVE LOOP
# ────────────────────────────────────────────────────────────────────────

def _run_wave(conn, alloc_table, final_table,
              wave_name: str, ct_col: str, ct_threshold: float,
              size_threshold: float,
              primary_grids: Optional[Dict[str, List[str]]] = None) -> Dict:
    """
    Run one eligibility wave: RL -> TBC -> TBL, each with MAX(I_ROD) rounds.

    Within a wave, the eligibility predicate is:
        [ct_col] >= ct_threshold
        AND row not already fully shipped in an earlier wave
        AND OPT not SKIP_FLAG'd in this or a prior wave

    Pool state is carried from previous waves — each wave sees whatever
    FNL_Q_REM remains.
    """
    t_wave = time.time()
    stats = {
        "wave": wave_name, "ct_col": ct_col, "ct_threshold": ct_threshold,
        "pool": 0, "ship": 0, "hold": 0, "skipped": 0, "rounds": [],
        "seconds": 0.0,
    }

    for opt_type in OPT_TYPE_ORDER:
        max_irod = conn.execute(text(f"""
            SELECT MAX(ISNULL(CAST([I_ROD] AS INT), 1))
            FROM [{alloc_table}]
            WHERE [OPT_TYPE] = :ot AND ISNULL([SKIP_FLAG], 0) = 0
        """), {"ot": opt_type}).scalar() or 0
        if max_irod <= 0:
            continue

        for round_n in range(1, int(max_irod) + 1):
            _scale_demand(conn, alloc_table, opt_type, round_n)
            round_stats = _allocate_round(
                conn, alloc_table, final_table,
                wave_name, ct_col, ct_threshold,
                opt_type, round_n, size_threshold,
                primary_grids,
            )
            stats["rounds"].append(round_stats)
            stats["pool"]     += round_stats["pool"]
            stats["ship"]     += round_stats["ship"]
            stats["hold"]     += round_stats["hold"]
            stats["skipped"]  += round_stats["skipped"]

    stats["seconds"] = round(time.time() - t_wave, 1)
    return stats


# ────────────────────────────────────────────────────────────────────────
#  DEMAND SCALING (Rule 1 round cumulation + Rule 3 WH/no-hold split)
# ────────────────────────────────────────────────────────────────────────

def _scale_demand(conn, alloc_table, opt_type: str, round_n: int):
    """
    Compute SZ_POOL_REQ and SZ_SHIP_REQ for the current (opt_type, round).

        SZ_POOL_MBQ = OPT_MBQ_WH * round_n * CONT
        SZ_SHIP_MBQ = (OPT_MBQ if TBL else OPT_MBQ_WH) * round_n * CONT

        SZ_POOL_REQ = MAX(0, SZ_POOL_MBQ - STK_TTL - POOL_CONSUMED)
        SZ_SHIP_REQ = MAX(0, SZ_SHIP_MBQ - STK_TTL - SHIP_QTY)

    POOL_CONSUMED and SHIP_QTY accumulate across waves + rounds, so this
    naturally zeroes out rows already satisfied earlier.

    TBL trivial-need guard (v3): for OPT_TYPE='TBL', if MJ_REQ_ORG <
    TBL_TRIVIAL_NEED_FACTOR × MAX_DAILY_SALE, SZ_POOL_REQ is forced to 0
    and the OPT is marked SKIPPED. Prevents over-filling the primary grid
    on stores whose master-grid need is below half a day's sale.
    """
    _run(conn, f"""
        UPDATE [{alloc_table}]
        SET
            [SZ_POOL_MBQ] = ROUND(ISNULL([OPT_MBQ_WH], 0) * :rnd * ISNULL([CONT], 0), 0),
            [SZ_SHIP_MBQ] = ROUND(
                CASE WHEN [OPT_TYPE] = 'TBL'
                     THEN ISNULL([OPT_MBQ], 0)
                     ELSE ISNULL([OPT_MBQ_WH], 0)
                END * :rnd * ISNULL([CONT], 0), 0),
            [SZ_POOL_REQ] = CASE
                WHEN ROUND(ISNULL([OPT_MBQ_WH], 0) * :rnd * ISNULL([CONT], 0), 0)
                     - ISNULL([STK_TTL], 0) - ISNULL([POOL_CONSUMED], 0) > 0
                THEN ROUND(ISNULL([OPT_MBQ_WH], 0) * :rnd * ISNULL([CONT], 0), 0)
                     - ISNULL([STK_TTL], 0) - ISNULL([POOL_CONSUMED], 0)
                ELSE 0
            END,
            [SZ_SHIP_REQ] = CASE
                WHEN ROUND(
                       CASE WHEN [OPT_TYPE] = 'TBL'
                            THEN ISNULL([OPT_MBQ], 0)
                            ELSE ISNULL([OPT_MBQ_WH], 0)
                       END * :rnd * ISNULL([CONT], 0), 0)
                     - ISNULL([STK_TTL], 0) - ISNULL([SHIP_QTY], 0) > 0
                THEN ROUND(
                       CASE WHEN [OPT_TYPE] = 'TBL'
                            THEN ISNULL([OPT_MBQ], 0)
                            ELSE ISNULL([OPT_MBQ_WH], 0)
                       END * :rnd * ISNULL([CONT], 0), 0)
                     - ISNULL([STK_TTL], 0) - ISNULL([SHIP_QTY], 0)
                ELSE 0
            END,
            [ROUND_POOL] = 0, [ROUND_SHIP] = 0, [ROUND_HOLD] = 0
        WHERE [OPT_TYPE] = :ot
          AND ISNULL(CAST([I_ROD] AS INT), 1) >= :rnd
          AND ISNULL([SKIP_FLAG], 0) = 0
    """, {"ot": opt_type, "rnd": round_n})

    # TBL trivial-need guard: skip TBL options where the primary master
    # grid need (MJ_REQ) is below TBL_TRIVIAL_NEED_FACTOR × MAX_DAILY_SALE.
    # Forces SZ_POOL_REQ/SZ_SHIP_REQ to 0 and SKIP_FLAG=1 so the waterfall
    # passes over them. Only runs on TBL (RL/TBC are not affected).
    if opt_type == "TBL":
        _run(conn, f"""
            UPDATE [{alloc_table}]
            SET [SZ_POOL_REQ] = 0,
                [SZ_SHIP_REQ] = 0,
                [SKIP_FLAG]   = 1,
                [ALLOC_STATUS] = CASE
                    WHEN ISNULL([SHIP_QTY], 0) > 0 THEN 'PARTIAL'
                    ELSE 'SKIPPED' END,
                [SKIP_REASON] = CONCAT(
                    ISNULL([SKIP_REASON], ''),
                    'TBL:primary_trivial(MJ_REQ=',
                    CAST(ISNULL([MJ_REQ_ORG], 0) AS NVARCHAR(20)),
                    ',MDS=', CAST(ISNULL([MAX_DAILY_SALE], 0) AS NVARCHAR(20)),
                    '); ')
            WHERE [OPT_TYPE] = 'TBL'
              AND ISNULL([SKIP_FLAG], 0) = 0
              AND ISNULL([MAX_DAILY_SALE], 0) > 0
              AND ISNULL([MJ_REQ_ORG], 0)
                  < :factor * ISNULL([MAX_DAILY_SALE], 0)
        """, {"factor": TBL_TRIVIAL_NEED_FACTOR})


# ────────────────────────────────────────────────────────────────────────
#  REM METRIC REFRESH (dynamic PRI_CT%, SEC_CT%, OPT_REQ)
# ────────────────────────────────────────────────────────────────────────

def _refresh_rem_metrics(conn, alloc_table, opt_type, band_start, band_end):
    """
    Recompute PRI_CT_REM, SEC_CT_REM, OPT_REQ_REM for all options in this
    band's (WERKS × MAJ × GEN × CLR) set.

    Definitions:
      PRI_CT_REM  — % of sizes with unfilled SHIP target
                    (SHIP_QTY < SZ_POOL_MBQ).  100 = untouched primary
                    grid, 0 = fully satisfied, (0,100) = partial.
      SEC_CT_REM  — mirrors PRI_CT_REM (engine does not split primary vs
                    secondary at size grain; kept for reporting parity).
      OPT_REQ_REM — remaining option REQ = MAX(0, OPT_REQ_WH - POOL_CONSUMED).

    Called once per band AFTER pool deduction and any TBL size-ratio
    restore, so POOL_CONSUMED / SHIP_QTY already reflect the band.
    """
    # Note: compare POOL_CONSUMED (what the pool actually gave this row)
    # against SZ_POOL_MBQ (the pool target for this round). Both are
    # pool-grain, so TBL rows where SHIP_QTY is capped at OPT_MBQ×CONT
    # do not falsely look "partial" — pool was fully taken even when the
    # ship side was smaller than OPT_MBQ_WH×CONT.
    _run(conn, f"""
        ;WITH OptRoll AS (
            SELECT
                [WERKS], [MAJ_CAT], [GEN_ART_NUMBER], [CLR],
                SUM(ISNULL([POOL_CONSUMED], 0))                 AS consumed,
                MAX(ISNULL([OPT_REQ_WH], 0))                    AS req_wh_org,
                CAST(SUM(CASE WHEN ISNULL([SZ_POOL_MBQ], 0) > 0
                               AND ISNULL([POOL_CONSUMED], 0) + ISNULL([STK_TTL], 0)
                                   < ISNULL([SZ_POOL_MBQ], 0)
                              THEN 1 ELSE 0 END) AS FLOAT)      AS sz_unfilled,
                CAST(SUM(CASE WHEN ISNULL([SZ_POOL_MBQ], 0) > 0
                              THEN 1 ELSE 0 END) AS FLOAT)      AS sz_total
            FROM [{alloc_table}]
            WHERE [OPT_TYPE] = :ot
              AND ISNULL([OPT_PRIORITY_RANK], 0) BETWEEN :bs AND :be
            GROUP BY [WERKS], [MAJ_CAT], [GEN_ART_NUMBER], [CLR]
        )
        UPDATE A SET
            A.[OPT_REQ_REM] = CASE
                WHEN O.req_wh_org - O.consumed > 0 THEN O.req_wh_org - O.consumed
                ELSE 0 END,
            A.[PRI_CT_REM] = CASE
                WHEN O.sz_total > 0
                    THEN 100.0 * O.sz_unfilled / O.sz_total
                ELSE TRY_CAST(A.[PRI_CT%] AS FLOAT) END,
            A.[SEC_CT_REM] = CASE
                WHEN O.sz_total > 0
                    THEN 100.0 * O.sz_unfilled / O.sz_total
                ELSE TRY_CAST(A.[SEC_CT%] AS FLOAT) END
        FROM [{alloc_table}] A
        INNER JOIN OptRoll O
            ON  A.[WERKS] = O.[WERKS]
            AND A.[MAJ_CAT] = O.[MAJ_CAT]
            AND A.[GEN_ART_NUMBER] = O.[GEN_ART_NUMBER]
            AND A.[CLR] = O.[CLR]
        WHERE A.[OPT_TYPE] = :ot
          AND ISNULL(A.[OPT_PRIORITY_RANK], 0) BETWEEN :bs AND :be
    """, {"ot": opt_type, "bs": band_start, "be": band_end})


# ────────────────────────────────────────────────────────────────────────
#  BATCH ALLOCATION FOR ONE (WAVE, OPT_TYPE, ROUND)
# ────────────────────────────────────────────────────────────────────────

def _allocate_round(
    conn, alloc_table, final_table,
    wave_name: str, ct_col: str, ct_threshold: float,
    opt_type: str, round_n: int, size_threshold: float,
    primary_grids: Optional[Dict[str, List[str]]] = None,
) -> Dict:
    """
    Priority-rank-banded allocator.

    Loops OPT_PRIORITY_RANK in bands of PRIORITY_BAND_SIZE. Within each band:
      A) Batch waterfall-CTE allocates all (store × option × variant × size)
         whose rank falls in the band AND whose store is not STORE_BROKEN.
         Window ORDER BY (OPT_PRIORITY_RANK ASC, ST_RANK ASC, WERKS) so
         higher-priority options and higher-rank stores take pool first.
      B) Deduct pool for this band's allocations.
      C) TBL-only size-ratio break: if <size_threshold of sizes survive for
         a given TBL option at a given ST_RANK, restore pool + mark SKIPPED.
      D) Mark stores as STORE_BROKEN when any size of any option in this
         band had pool shortfall (dynamic PRI_CT% < 100). Cascades to all
         future bands — the store drops out for the rest of the run.
      E) Commit cumulative qtys (POOL_CONSUMED / SHIP_QTY / HOLD_QTY).

    Fidelity vs pure per-option greedy: within a band, up to BAND_SIZE
    ranks get evaluated together against the pool state at band start.
    Broken stores drop out on the NEXT band, not immediately after the
    breaking option. With BAND_SIZE=25 this is a small deviation.
    """
    t0 = time.time()
    stats = {
        "wave": wave_name, "opt_type": opt_type, "round": round_n,
        "pool": 0, "ship": 0, "hold": 0, "opts": 0,
        "skipped": 0, "bands": 0, "seconds": 0.0,
    }

    # Wave eligibility predicate (quoted because of '%' in column names)
    ct_col_bracketed = f"[{ct_col}]"
    wave_predicate = f"""
        TRY_CAST(A.{ct_col_bracketed} AS FLOAT) >= {float(ct_threshold)}
    """

    max_rnk = conn.execute(text(f"""
        SELECT MAX(ISNULL([OPT_PRIORITY_RANK], 0))
        FROM [{alloc_table}]
        WHERE [OPT_TYPE] = :ot
          AND ISNULL([SKIP_FLAG], 0) = 0
          AND ISNULL([STORE_BROKEN], 0) = 0
          AND ISNULL(CAST([I_ROD] AS INT), 1) >= :rnd
          AND ISNULL([SZ_POOL_REQ], 0) > 0
    """), {"ot": opt_type, "rnd": round_n}).scalar() or 0
    if int(max_rnk) <= 0:
        stats["seconds"] = round(time.time() - t0, 1)
        return stats

    alloc_wave_tag = f"{wave_name}_R{round_n}"

    for band_start in range(1, int(max_rnk) + 1, PRIORITY_BAND_SIZE):
        band_end = band_start + PRIORITY_BAND_SIZE - 1
        stats["bands"] += 1

        # ── STEP A-pre: strict skip gate (no partial alloc)
        # For each option in this band:
        #   - STORE_BROKEN if MJ_REQ_REM < 0.5 × ACS_D
        #   - SKIP if SUM(SZ_POOL_REQ) > any primary-grid REQ_REM
        # Options that pass run their full OPT_MBQ_WH allocation in STEP A.
        if primary_grids:
            _skip_options_exceeding_grid(
                conn, alloc_table, final_table, opt_type,
                band_start, band_end, primary_grids,
            )

        # ── STEP A: band waterfall (priority-then-store-rank ordered) ─
        _run(conn, f"""
            ;WITH Eligible AS (
                SELECT
                    A.[WERKS], A.[RDC], A.[MAJ_CAT], A.[GEN_ART_NUMBER], A.[CLR],
                    A.[VAR_ART], A.[SZ], A.[ST_RANK],
                    A.[OPT_TYPE], A.[OPT_PRIORITY_RANK],
                    P.[FNL_Q_REM],
                    ISNULL(A.[SZ_POOL_REQ], 0) AS SZ_POOL_REQ,
                    ISNULL(A.[SZ_SHIP_REQ], 0) AS SZ_SHIP_REQ,
                    /* v3: Waterfall ordered by ST_RANK first so the
                       top-ranked store within a MAJ_CAT consumes pool
                       before lower-ranked stores. OPT_PRIORITY_RANK is the
                       secondary tiebreaker — within a single store's
                       competing OPTs, rank-1 wins over rank-5. */
                    ISNULL(SUM(ISNULL(A.[SZ_POOL_REQ], 0)) OVER (
                        PARTITION BY A.[RDC], A.[MAJ_CAT], A.[GEN_ART_NUMBER],
                                     A.[CLR], A.[VAR_ART], A.[SZ]
                        ORDER BY ISNULL(A.[ST_RANK], 999999) ASC,
                                 A.[OPT_PRIORITY_RANK] ASC,
                                 A.[WERKS]
                        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                    ), 0) AS prev_pool_demand
                FROM [{alloc_table}] A
                INNER JOIN {POOL_TABLE} P
                    ON  A.[RDC] = P.[RDC] AND A.[MAJ_CAT] = P.[MAJ_CAT]
                    AND A.[GEN_ART_NUMBER] = P.[GEN_ART_NUMBER]
                    AND A.[CLR] = P.[CLR] AND A.[VAR_ART] = P.[VAR_ART]
                    AND A.[SZ] = P.[SZ]
                WHERE A.[OPT_TYPE] = :ot
                  AND ISNULL(A.[SKIP_FLAG], 0) = 0
                  AND ISNULL(A.[STORE_BROKEN], 0) = 0
                  AND ISNULL(A.[OPT_PRIORITY_RANK], 0) BETWEEN :bs AND :be
                  AND ISNULL(CAST(A.[I_ROD] AS INT), 1) >= :rnd
                  AND ISNULL(A.[SZ_POOL_REQ], 0) > 0
                  AND {wave_predicate}
                  AND EXISTS (
                      SELECT 1 FROM [{final_table}] W
                      WHERE W.[WERKS] = A.[WERKS]
                        AND W.[MAJ_CAT] = A.[MAJ_CAT]
                        AND W.[GEN_ART_NUMBER] = A.[GEN_ART_NUMBER]
                        AND W.[CLR] = A.[CLR]
                        AND W.[ALLOC_STATUS] IN ('PENDING', 'PARTIAL')
                        AND ISNULL(TRY_CAST(W.[LISTING] AS INT), 1) = 1
                        AND ISNULL(TRY_CAST(W.[MSA_FNL_Q] AS FLOAT), 0) > 0
                  )
            ),
            Grab AS (
                SELECT *,
                    CASE
                        WHEN FNL_Q_REM - prev_pool_demand <= 0 THEN 0
                        WHEN SZ_POOL_REQ <= FNL_Q_REM - prev_pool_demand THEN SZ_POOL_REQ
                        ELSE FNL_Q_REM - prev_pool_demand
                    END AS pool_taken
                FROM Eligible
            ),
            Split AS (
                SELECT *,
                    CASE
                        WHEN OPT_TYPE = 'TBL'
                            THEN CASE WHEN pool_taken <= SZ_SHIP_REQ THEN pool_taken
                                      ELSE SZ_SHIP_REQ END
                        ELSE pool_taken
                    END AS ship_qty,
                    CASE
                        WHEN OPT_TYPE = 'TBL' AND pool_taken > SZ_SHIP_REQ
                            THEN pool_taken - SZ_SHIP_REQ
                        ELSE 0
                    END AS hold_qty
                FROM Grab
            )
            UPDATE A SET
                A.[ROUND_POOL]  = S.pool_taken,
                A.[ROUND_SHIP]  = S.ship_qty,
                A.[ROUND_HOLD]  = S.hold_qty,
                A.[ALLOC_ROUND] = :rnd,
                A.[ALLOC_WAVE]  = :wv
            FROM [{alloc_table}] A
            INNER JOIN Split S
                ON  A.[WERKS] = S.[WERKS] AND A.[RDC] = S.[RDC]
                AND A.[MAJ_CAT] = S.[MAJ_CAT]
                AND A.[GEN_ART_NUMBER] = S.[GEN_ART_NUMBER]
                AND A.[CLR] = S.[CLR] AND A.[VAR_ART] = S.[VAR_ART]
                AND A.[SZ] = S.[SZ]
            WHERE S.pool_taken > 0
        """, {"ot": opt_type, "rnd": round_n, "wv": alloc_wave_tag,
              "bs": band_start, "be": band_end})

        # ── STEP A.2: perfect-combination gate ───────────────────────
        # v3 rule: if ANY variant-size of an OPT (at a store) fell short
        # this band (ROUND_POOL < SZ_POOL_REQ), roll back the entire OPT
        # for that store and mark SKIPPED. "Any grid false → skip the
        # option." Only OPTs that can fully satisfy their per-size pool
        # demand get committed. Per-store skip — same OPT can still win
        # at other stores in later bands.
        _run(conn, f"""
            ;WITH Shortfall AS (
                SELECT DISTINCT
                    [WERKS], [OPT_TYPE], [MAJ_CAT], [GEN_ART_NUMBER], [CLR]
                FROM [{alloc_table}]
                WHERE [OPT_TYPE] = :ot
                  AND ISNULL([OPT_PRIORITY_RANK], 0) BETWEEN :bs AND :be
                  AND ISNULL([SZ_POOL_REQ], 0) > 0
                  AND ISNULL([ROUND_POOL], 0) < ISNULL([SZ_POOL_REQ], 0)
            )
            UPDATE A SET
                A.[ROUND_POOL]  = 0,
                A.[ROUND_SHIP]  = 0,
                A.[ROUND_HOLD]  = 0,
                A.[ALLOC_WAVE]  = NULL,
                A.[ALLOC_ROUND] = 0,
                A.[SKIP_FLAG]   = 1,
                A.[ALLOC_STATUS] = CASE
                    WHEN ISNULL(A.[SHIP_QTY], 0) > 0 THEN 'PARTIAL'
                    ELSE 'SKIPPED' END,
                A.[SKIP_REASON] = CONCAT(
                    ISNULL(A.[SKIP_REASON], ''),
                    'PERFECT_COMBO_FAIL@WAVE=', :wv, '; ')
            FROM [{alloc_table}] A
            INNER JOIN Shortfall S
                ON  A.[WERKS] = S.[WERKS]
                AND A.[OPT_TYPE] = S.[OPT_TYPE]
                AND A.[MAJ_CAT] = S.[MAJ_CAT]
                AND A.[GEN_ART_NUMBER] = S.[GEN_ART_NUMBER]
                AND A.[CLR] = S.[CLR]
            WHERE ISNULL(A.[OPT_PRIORITY_RANK], 0) BETWEEN :bs AND :be
        """, {"ot": opt_type, "bs": band_start, "be": band_end,
              "wv": alloc_wave_tag})

        band_totals = conn.execute(text(f"""
            SELECT
                ISNULL(SUM(ISNULL([ROUND_POOL], 0)), 0),
                ISNULL(SUM(ISNULL([ROUND_SHIP], 0)), 0),
                ISNULL(SUM(ISNULL([ROUND_HOLD], 0)), 0),
                ISNULL(SUM(CASE WHEN [SKIP_REASON] LIKE '%PERFECT_COMBO_FAIL%'
                                 AND [ALLOC_WAVE] IS NULL
                                THEN 1 ELSE 0 END), 0) AS combo_skipped
            FROM [{alloc_table}]
            WHERE [OPT_TYPE] = :ot
              AND ISNULL([OPT_PRIORITY_RANK], 0) BETWEEN :bs AND :be
              AND ( [ALLOC_WAVE] = :wv
                    OR [SKIP_REASON] LIKE '%PERFECT_COMBO_FAIL%' )
        """), {"ot": opt_type, "bs": band_start, "be": band_end,
               "wv": alloc_wave_tag}).fetchone()
        band_pool = float(band_totals[0] or 0)
        stats["pool"] += band_pool
        stats["ship"] += float(band_totals[1] or 0)
        stats["hold"] += float(band_totals[2] or 0)
        stats["skipped"] += int(band_totals[3] or 0)
        if band_pool == 0:
            continue

        # ── STEP B: deduct pool for this band's allocations ──────────
        _run(conn, f"""
            UPDATE P SET P.[FNL_Q_REM] = P.[FNL_Q_REM] - ISNULL(D.consumed, 0)
            FROM {POOL_TABLE} P
            INNER JOIN (
                SELECT [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR], [VAR_ART], [SZ],
                       SUM(ISNULL([ROUND_POOL], 0)) AS consumed
                FROM [{alloc_table}]
                WHERE [OPT_TYPE] = :ot
                  AND ISNULL([OPT_PRIORITY_RANK], 0) BETWEEN :bs AND :be
                  AND [ALLOC_WAVE] = :wv
                  AND ISNULL([ROUND_POOL], 0) > 0
                GROUP BY [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR], [VAR_ART], [SZ]
            ) D
                ON  P.[RDC] = D.[RDC] AND P.[MAJ_CAT] = D.[MAJ_CAT]
                AND P.[GEN_ART_NUMBER] = D.[GEN_ART_NUMBER]
                AND P.[CLR] = D.[CLR] AND P.[VAR_ART] = D.[VAR_ART]
                AND P.[SZ] = D.[SZ]
        """, {"ot": opt_type, "bs": band_start, "be": band_end,
              "wv": alloc_wave_tag})

        # ── STEP C: TBL-only size-ratio break within this band ───────
        # RL/TBC accept partial fills — they only hit the STORE_BROKEN
        # gate in STEP D (any shortfall => break).
        if opt_type == "TBL":
            _run(conn, f"IF OBJECT_ID('tempdb..{BREAK_TABLE}') IS NOT NULL DROP TABLE {BREAK_TABLE}")
            _ot_lit = str(opt_type).replace("'", "''")
            _thr_lit = float(size_threshold)
            _bs_lit = int(band_start)
            _be_lit = int(band_end)
            _wv_lit = alloc_wave_tag.replace("'", "''")
            _run(conn, f"""
            ;WITH AllocCum AS (
                SELECT [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR],
                       [VAR_ART], [SZ], [ST_RANK],
                       SUM(ISNULL([ROUND_POOL], 0)) OVER (
                           PARTITION BY [RDC], [MAJ_CAT], [GEN_ART_NUMBER],
                                        [CLR], [VAR_ART], [SZ]
                           ORDER BY ISNULL([ST_RANK], 999999), [WERKS]
                       ) AS cum_alloc
                FROM [{alloc_table}]
                WHERE [OPT_TYPE] = '{_ot_lit}'
                  AND ISNULL([OPT_PRIORITY_RANK], 0) BETWEEN {_bs_lit} AND {_be_lit}
                  AND [ALLOC_WAVE] = '{_wv_lit}'
                  AND ISNULL([ROUND_POOL], 0) > 0
            ),
            PerRank AS (
                SELECT [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR], [VAR_ART],
                       [SZ], [ST_RANK], MAX(cum_alloc) AS cum_alloc
                FROM AllocCum
                GROUP BY [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR],
                         [VAR_ART], [SZ], [ST_RANK]
            ),
            PerPool AS (
                SELECT [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR], [VAR_ART], [SZ],
                       MAX(cum_alloc) AS tot
                FROM PerRank
                GROUP BY [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR], [VAR_ART], [SZ]
            ),
            PoolAtRank AS (
                SELECT R.[MAJ_CAT], R.[GEN_ART_NUMBER], R.[CLR],
                       R.[ST_RANK], P.[SZ],
                       P.[FNL_Q_REM] + ISNULL(T.tot, 0) - R.cum_alloc AS pool_after
                FROM PerRank R
                INNER JOIN {POOL_TABLE} P
                    ON P.[RDC]=R.[RDC] AND P.[MAJ_CAT]=R.[MAJ_CAT]
                    AND P.[GEN_ART_NUMBER]=R.[GEN_ART_NUMBER] AND P.[CLR]=R.[CLR]
                    AND P.[VAR_ART]=R.[VAR_ART] AND P.[SZ]=R.[SZ]
                INNER JOIN PerPool T
                    ON T.[RDC]=R.[RDC] AND T.[MAJ_CAT]=R.[MAJ_CAT]
                    AND T.[GEN_ART_NUMBER]=R.[GEN_ART_NUMBER] AND T.[CLR]=R.[CLR]
                    AND T.[VAR_ART]=R.[VAR_ART] AND T.[SZ]=R.[SZ]
            ),
            SzAvail AS (
                SELECT [MAJ_CAT], [GEN_ART_NUMBER], [CLR], [ST_RANK],
                       COUNT(DISTINCT [SZ]) AS total_sz,
                       COUNT(DISTINCT CASE WHEN pool_after > 0 THEN [SZ] END) AS sz_ok
                FROM PoolAtRank
                GROUP BY [MAJ_CAT], [GEN_ART_NUMBER], [CLR], [ST_RANK]
            )
            SELECT [MAJ_CAT], [GEN_ART_NUMBER], [CLR],
                   MIN([ST_RANK]) AS break_rank
            INTO {BREAK_TABLE}
            FROM SzAvail
            WHERE total_sz > 0 AND CAST(sz_ok AS FLOAT) / total_sz < {_thr_lit}
            GROUP BY [MAJ_CAT], [GEN_ART_NUMBER], [CLR]
            """)

            break_count = conn.execute(text(
                f"SELECT COUNT(*) FROM {BREAK_TABLE}"
            )).scalar() or 0

            if break_count > 0:
                stats["skipped"] += int(break_count)
                _run(conn, f"""
                    UPDATE P SET P.[FNL_Q_REM] = P.[FNL_Q_REM] + ISNULL(R.restore_qty, 0)
                    FROM {POOL_TABLE} P
                    INNER JOIN (
                        SELECT A.[RDC], A.[MAJ_CAT], A.[GEN_ART_NUMBER], A.[CLR],
                               A.[VAR_ART], A.[SZ],
                               SUM(ISNULL(A.[ROUND_POOL], 0)) AS restore_qty
                        FROM [{alloc_table}] A
                        INNER JOIN {BREAK_TABLE} BR
                            ON  A.[MAJ_CAT] = BR.[MAJ_CAT]
                            AND A.[GEN_ART_NUMBER] = BR.[GEN_ART_NUMBER]
                            AND A.[CLR] = BR.[CLR]
                        WHERE A.[OPT_TYPE] = :ot
                          AND ISNULL(A.[OPT_PRIORITY_RANK], 0) BETWEEN :bs AND :be
                          AND A.[ALLOC_WAVE] = :wv
                          AND ISNULL(A.[ST_RANK], 999999) >= BR.break_rank
                          AND ISNULL(A.[ROUND_POOL], 0) > 0
                        GROUP BY A.[RDC], A.[MAJ_CAT], A.[GEN_ART_NUMBER], A.[CLR],
                                 A.[VAR_ART], A.[SZ]
                    ) R ON  P.[RDC]=R.[RDC] AND P.[MAJ_CAT]=R.[MAJ_CAT]
                        AND P.[GEN_ART_NUMBER]=R.[GEN_ART_NUMBER]
                        AND P.[CLR]=R.[CLR] AND P.[VAR_ART]=R.[VAR_ART]
                        AND P.[SZ]=R.[SZ]
                """, {"ot": opt_type, "bs": band_start, "be": band_end,
                      "wv": alloc_wave_tag})

                _run(conn, f"""
                    UPDATE A SET
                        A.[ROUND_POOL] = 0,
                        A.[ROUND_SHIP] = 0,
                        A.[ROUND_HOLD] = 0,
                        A.[ALLOC_STATUS] = CASE
                            WHEN ISNULL(A.[SHIP_QTY], 0) > 0 THEN 'PARTIAL'
                            ELSE 'SKIPPED'
                        END,
                        A.[SKIP_REASON] = CONCAT(
                            'SZ<{int(size_threshold*100)}pct@RANK=',
                            CAST(BR.break_rank AS NVARCHAR(10)),
                            ';WAVE=', :wv)
                    FROM [{alloc_table}] A
                    INNER JOIN {BREAK_TABLE} BR
                        ON  A.[MAJ_CAT] = BR.[MAJ_CAT]
                        AND A.[GEN_ART_NUMBER] = BR.[GEN_ART_NUMBER]
                        AND A.[CLR] = BR.[CLR]
                    WHERE A.[OPT_TYPE] = :ot
                      AND ISNULL(A.[ST_RANK], 999999) >= BR.break_rank
                """, {"ot": opt_type, "wv": wave_name})

                _run(conn, f"""
                    UPDATE A SET A.[SKIP_FLAG] = 1
                    FROM [{alloc_table}] A
                    INNER JOIN {BREAK_TABLE} BR
                        ON  A.[MAJ_CAT] = BR.[MAJ_CAT]
                        AND A.[GEN_ART_NUMBER] = BR.[GEN_ART_NUMBER]
                        AND A.[CLR] = BR.[CLR]
                    WHERE A.[OPT_TYPE] = :ot
                """, {"ot": opt_type})

        # ── STEP D.1: refresh REM metrics for this band ──────────────
        # Commit POOL_CONSUMED/SHIP_QTY deltas first so REM computations
        # see them. We mini-commit here instead of waiting for STEP E so
        # PRI_CT_REM reflects the band's true post-alloc state.
        _run(conn, f"""
            UPDATE [{alloc_table}]
            SET [POOL_CONSUMED] = ISNULL([POOL_CONSUMED], 0) + ISNULL([ROUND_POOL], 0),
                [SHIP_QTY]      = ISNULL([SHIP_QTY], 0)      + ISNULL([ROUND_SHIP], 0),
                [HOLD_QTY]      = ISNULL([HOLD_QTY], 0)      + ISNULL([ROUND_HOLD], 0)
            WHERE [OPT_TYPE] = :ot
              AND ISNULL([OPT_PRIORITY_RANK], 0) BETWEEN :bs AND :be
              AND [ALLOC_WAVE] = :wv
        """, {"ot": opt_type, "bs": band_start, "be": band_end,
              "wv": alloc_wave_tag})

        _refresh_rem_metrics(conn, alloc_table, opt_type, band_start, band_end)

        # Zero the round deltas so STEP E (end-of-round commit) won't
        # double-count them.
        _run(conn, f"""
            UPDATE [{alloc_table}]
            SET [ROUND_POOL] = 0, [ROUND_SHIP] = 0, [ROUND_HOLD] = 0
            WHERE [OPT_TYPE] = :ot
              AND ISNULL([OPT_PRIORITY_RANK], 0) BETWEEN :bs AND :be
              AND [ALLOC_WAVE] = :wv
        """, {"ot": opt_type, "bs": band_start, "be": band_end,
              "wv": alloc_wave_tag})

        # ── STEP D.2: dynamic PRI_CT% break — cascade STORE_BROKEN ───
        # PRI_CT_REM in (0, 100) = partial fill of primary grid
        # (started at 100, didn't reach 0) → break the store so its
        # remaining options in later bands are skipped. PRI_CT_REM = 0
        # means fully satisfied this option; PRI_CT_REM = 100 means
        # nothing touched yet (shouldn't normally happen post-alloc).
        #
        # Scoped BY OPT_TYPE: ranks are per-opt_type (RL / TBC / TBL each
        # start at 1), so "break and next store" also resets between
        # opt_types. Marking only rows where A.[OPT_TYPE] = :ot leaves
        # the other opt_types' STORE_BROKEN=0 so they allocate cleanly.
        _run(conn, f"""
            UPDATE A SET A.[STORE_BROKEN] = 1
            FROM [{alloc_table}] A
            INNER JOIN (
                SELECT DISTINCT [WERKS]
                FROM [{alloc_table}]
                WHERE [OPT_TYPE] = :ot
                  AND ISNULL([OPT_PRIORITY_RANK], 0) BETWEEN :bs AND :be
                  AND [ALLOC_WAVE] = :wv
                  AND ISNULL([PRI_CT_REM], 100) > 0
                  AND ISNULL([PRI_CT_REM], 100) < 100
            ) X ON A.[WERKS] = X.[WERKS]
            WHERE A.[OPT_TYPE] = :ot
              AND ISNULL(A.[STORE_BROKEN], 0) = 0
        """, {"ot": opt_type, "bs": band_start, "be": band_end,
              "wv": alloc_wave_tag})

        # ── STEP D.3: per-rank PRI_CT% revalidation + store-skip gate ─
        # Deduct this rank's SHIP_QTY from each primary grid's REQ,
        # recompute H_<grid> (REQ>0 AND GH=1), recompute PRI_CT%,
        # mirror to alloc_working, SKIP future ranks whose PRI_CT% fell
        # below 100, and STORE_BROKEN stores where MJ_REQ <= 0 or
        # MJ_REQ <= 0.5 × ACS_D.
        if primary_grids:
            _revalidate_after_rank(
                conn, alloc_table, final_table, opt_type,
                band_start, wave_name, round_n, primary_grids,
            )

    # ── STEP E: finalize ALLOC_STATUS for this round ─────────────────
    # POOL_CONSUMED / SHIP_QTY / HOLD_QTY were already rolled up per band
    # in STEP D.1. Here we only refresh ALLOC_STATUS from the cumulative
    # totals.
    _run(conn, f"""
        UPDATE [{alloc_table}]
        SET [ALLOC_STATUS] = CASE
                WHEN [ALLOC_STATUS] IN ('SKIPPED', 'INELIGIBLE')
                    THEN [ALLOC_STATUS]
                WHEN ISNULL([SHIP_QTY], 0) > 0
                     AND ISNULL([SHIP_QTY], 0) >= ISNULL([SZ_SHIP_MBQ], 0)
                    THEN 'ALLOCATED'
                WHEN ISNULL([SHIP_QTY], 0) > 0
                    THEN 'PARTIAL'
                ELSE [ALLOC_STATUS]
            END
        WHERE [OPT_TYPE] = :ot
    """, {"ot": opt_type})

    # stats["pool"/"ship"/"hold"] already accumulated per band above.
    stats["seconds"] = round(time.time() - t0, 1)

    logger.info(
        f"  {wave_name} {opt_type} R{round_n}: "
        f"bands={stats['bands']}, ship={stats['ship']:.0f}, "
        f"hold={stats['hold']:.0f}, skip={stats['skipped']}, "
        f"{stats['seconds']}s"
    )
    return stats


# ────────────────────────────────────────────────────────────────────────
#  PER-WAVE SYNC BACK TO LISTING_WORKING
# ────────────────────────────────────────────────────────────────────────

def _sync_working_after_wave(conn, final_table, alloc_table):
    """
    Write post-wave remainders to SHADOW columns on listing_working —
    originals (MSA_FNL_Q, OPT_REQ, OPT_REQ_WH, VAR_FNL_COUNT) are never
    mutated. Called once per wave.

    Shadow columns written:
      MSA_FNL_Q_REM      — remaining pool qty per (RDC, MAJ, GEN_ART, CLR)
      OPT_REQ_REM_W      — remaining ship demand = OPT_MBQ - STK_TTL - shipped
      OPT_REQ_WH_REM_W   — remaining pool demand = OPT_MBQ_WH - STK_TTL - pool_used
      VAR_FNL_COUNT_REM  — distinct variants still alive in pool
    """
    # Ensure shadow columns exist
    shadow_cols = (
        ("MSA_FNL_Q_REM",     "FLOAT NULL"),
        ("OPT_REQ_REM_W",     "FLOAT NULL"),
        ("OPT_REQ_WH_REM_W",  "FLOAT NULL"),
        ("VAR_FNL_COUNT_REM", "INT NULL"),
    )
    for col, typedef in shadow_cols:
        try:
            _run(conn, f"ALTER TABLE [{final_table}] ADD [{col}] {typedef}")
        except Exception:
            pass

    # MSA_FNL_Q_REM = SUM of remaining pool qty per (RDC, MAJ_CAT, GEN_ART, CLR)
    _run(conn, f"""
        UPDATE W SET W.[MSA_FNL_Q_REM] = ISNULL(P.pool_rem, 0)
        FROM [{final_table}] W
        INNER JOIN (
            SELECT [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR],
                   SUM([FNL_Q_REM]) AS pool_rem
            FROM {POOL_TABLE}
            GROUP BY [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR]
        ) P
            ON LTRIM(RTRIM(CAST(W.[RDC] AS NVARCHAR(50)))) = P.[RDC]
           AND W.[MAJ_CAT] = P.[MAJ_CAT]
           AND W.[GEN_ART_NUMBER] = P.[GEN_ART_NUMBER]
           AND W.[CLR] = P.[CLR]
    """)

    # OPT_REQ_REM_W / OPT_REQ_WH_REM_W — originals stay frozen
    _run(conn, f"""
        UPDATE W SET
            W.[OPT_REQ_REM_W] = CASE
                WHEN ISNULL(W.[OPT_MBQ], 0) - ISNULL(W.[STK_TTL], 0)
                     - ISNULL(SA.shipped, 0) > 0
                THEN ISNULL(W.[OPT_MBQ], 0) - ISNULL(W.[STK_TTL], 0)
                     - ISNULL(SA.shipped, 0)
                ELSE 0 END,
            W.[OPT_REQ_WH_REM_W] = CASE
                WHEN ISNULL(W.[OPT_MBQ_WH], 0) - ISNULL(W.[STK_TTL], 0)
                     - ISNULL(SA.pool_used, 0) > 0
                THEN ISNULL(W.[OPT_MBQ_WH], 0) - ISNULL(W.[STK_TTL], 0)
                     - ISNULL(SA.pool_used, 0)
                ELSE 0 END
        FROM [{final_table}] W
        INNER JOIN (
            SELECT [WERKS], [MAJ_CAT], [GEN_ART_NUMBER], [CLR],
                   SUM(ISNULL([SHIP_QTY],      0)) AS shipped,
                   SUM(ISNULL([POOL_CONSUMED], 0)) AS pool_used
            FROM [{alloc_table}]
            GROUP BY [WERKS], [MAJ_CAT], [GEN_ART_NUMBER], [CLR]
        ) SA
            ON  W.[WERKS] = SA.[WERKS]
            AND W.[MAJ_CAT] = SA.[MAJ_CAT]
            AND W.[GEN_ART_NUMBER] = SA.[GEN_ART_NUMBER]
            AND W.[CLR] = SA.[CLR]
    """)

    # VAR_FNL_COUNT_REM — distinct variants still alive in pool
    _run(conn, f"""
        UPDATE W SET W.[VAR_FNL_COUNT_REM] = ISNULL(VC.live_vars, 0)
        FROM [{final_table}] W
        INNER JOIN (
            SELECT [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR],
                   COUNT(DISTINCT [VAR_ART]) AS live_vars
            FROM {POOL_TABLE}
            WHERE [FNL_Q_REM] > 0
            GROUP BY [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR]
        ) VC
            ON LTRIM(RTRIM(CAST(W.[RDC] AS NVARCHAR(50)))) = VC.[RDC]
           AND W.[MAJ_CAT] = VC.[MAJ_CAT]
           AND W.[GEN_ART_NUMBER] = VC.[GEN_ART_NUMBER]
           AND W.[CLR] = VC.[CLR]
    """)


# ────────────────────────────────────────────────────────────────────────
#  STEP 8 — REFLECT FINAL ALLOC_QTY + HOLD_QTY ONTO LISTING_WORKING
# ────────────────────────────────────────────────────────────────────────

def _reflect_to_working(conn, final_table, alloc_table):
    """
    Roll variant-size SHIP_QTY and HOLD_QTY up to option grain
    (store x MAJ_CAT x GEN_ART x CLR) on listing_working.

    ALLOC_QTY corresponds to SHIP_QTY (what goes out to store today).
    HOLD_QTY is the warehouse buffer retained for TBL lead-time cover.
    """
    try:
        _run(conn, f"ALTER TABLE [{final_table}] ADD [ALLOC_QTY] FLOAT NULL")
    except Exception:
        pass

    _run(conn, f"""
        UPDATE W SET
            W.[ALLOC_QTY] = ISNULL(A.ship_total, 0),
            W.[HOLD_QTY]  = ISNULL(A.hold_total, 0)
        FROM [{final_table}] W
        INNER JOIN (
            SELECT [WERKS], [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR],
                   SUM(ISNULL([SHIP_QTY], 0)) AS ship_total,
                   SUM(ISNULL([HOLD_QTY], 0)) AS hold_total
            FROM [{alloc_table}]
            WHERE ISNULL([SHIP_QTY], 0) > 0 OR ISNULL([HOLD_QTY], 0) > 0
            GROUP BY [WERKS], [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR]
        ) A
            ON  W.[WERKS] = A.[WERKS] AND W.[RDC] = A.[RDC]
            AND W.[MAJ_CAT] = A.[MAJ_CAT]
            AND W.[GEN_ART_NUMBER] = A.[GEN_ART_NUMBER]
            AND W.[CLR] = A.[CLR]
    """)

    _run(conn, f"""
        UPDATE [{final_table}] SET
            [ALLOC_STATUS] = CASE
                WHEN [ALLOC_STATUS] = 'INELIGIBLE' THEN 'INELIGIBLE'
                WHEN ISNULL([ALLOC_QTY], 0) > 0
                     AND ISNULL([ALLOC_QTY], 0) >= ISNULL([OPT_MBQ], 0)
                    THEN 'ALLOCATED'
                WHEN ISNULL([ALLOC_QTY], 0) > 0 THEN 'PARTIAL'
                WHEN [ALLOC_STATUS] = 'PENDING' THEN 'NOT_PROCESSED'
                ELSE [ALLOC_STATUS]
            END
    """)


# ────────────────────────────────────────────────────────────────────────
#  STEP 9 — CLEANUP
# ────────────────────────────────────────────────────────────────────────

def _cleanup(conn):
    for t in (POOL_TABLE, BREAK_TABLE):
        try:
            _run(conn, f"IF OBJECT_ID('tempdb..{t}') IS NOT NULL DROP TABLE {t}")
        except Exception:
            pass


# ────────────────────────────────────────────────────────────────────────
#  USAGE EXAMPLE (from listing.py endpoint)
# ────────────────────────────────────────────────────────────────────────
#
# from app.services.rule_engine import run_rule_based_allocation
#
# with de.connect() as ac:
#     result = run_rule_based_allocation(
#         conn=ac,
#         final_table="ARS_LISTING_WORKING",
#         alloc_table="ARS_ALLOC_WORKING",
#         size_threshold=req.stock_threshold_pct,   # e.g. 0.6
#         # waves=DEFAULT_WAVES,                     # or pass custom
#     )
# # result keys:
# #   alloc_rows, ship_qty_total, hold_qty_total,
# #   skipped_opts, waves[...], duration_sec
#
