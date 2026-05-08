"""
rule_engine_parallel_python.py

Parallel orchestrator that wraps rule_engine_new and runs Stage C
(allocation waterfall) concurrently across MAJ_CATs. Each worker thread
holds its own Data DB connection (=> own session, own #nre_pool tempdb
table) and pulls one MAJ_CAT at a time from the DB-backed queue
(ARS_ALLOC_MAJCAT_QUEUE).

Stage A and Stage B run once on the main thread before workers start —
exactly as in sequential mode — so Parts 1-7 in listing.py are unchanged
and the working / alloc / msa / pool tables are identical entering
Stage C.

A given MAJ_CAT is owned by exactly one worker for the entirety of its
RL → TBC → TBL waterfall. MAJ_CATs are independent in the rule engine:
the pool is keyed per MAJ_CAT, MJ_REQ_REM and PRI_CT_REM are scoped per
(WERKS, MAJ_CAT), and the store-broken rule never crosses MAJ_CATs.
Therefore parallel execution produces bit-identical SHIP_QTY / HOLD_QTY
to sequential mode.

Returns the same result dict shape as rule_engine_new.run_listing_and_allocation
plus extras (batch_id, failed list).
"""
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine
from app.services import rule_engine_new as rne
from app.services import alloc_cancellation as ac
from app.services.alloc_queue import (
    claim_next,
    get_done_summary,
    get_progress,
    make_batch_id,
    mark_done,
    mark_failed,
    seed_queue,
)
from app.utils.db_helpers import run_sql


# Lowered from 8/16 to 4/8. With 8 ThreadPoolExecutor workers each running
# CPU-heavy Python code in a single uvicorn process, the GIL gets saturated
# and unrelated endpoints (auth/login, polling, navigation) hit the proxy's
# 120s read-timeout. 4 workers keep CPU headroom available; bump higher
# only when running uvicorn with --workers N (separate processes => own GIL).
DEFAULT_WORKERS = int(os.getenv("ARS_PARALLEL_WORKERS", "4"))
MIN_WORKERS = 2
MAX_WORKERS = 8


def run_listing_and_allocation_python_parallel(
    working_table: str = "ARS_LISTING_WORKING",
    listed_table:  str = "ARS_LISTED_OPT",
    alloc_table:   str = "ARS_ALLOC_WORKING",
    msa_var_table: str = "ARS_MSA_VAR_ART",
    var_grid_table: str = "ARS_GRID_MJ_VAR_ART",
    cont_table:    str = "Master_CONT_SZ",
    n_workers:     int = DEFAULT_WORKERS,
    batch_id:      Optional[str] = None,
    only_majcats:  Optional[List[str]] = None,
    size_threshold: float = 0.6,
    min_size_count: int = 3,
    tbl_trivial_factor: float = 0.5,
    pri_ct_check_rl:  bool = True,
    pri_ct_check_tbc: bool = True,
) -> Dict:
    """
    Drop-in replacement for rule_engine_new.run_listing_and_allocation,
    parallelised by MAJ_CAT.

    only_majcats=None  → full run: do Stage A + Stage B, seed queue with all
                         MAJ_CATs from alloc_table, then dispatch workers.
    only_majcats=[..]  → retry mode: skip Stage A/B (assumes alloc_table is
                         already populated from a previous run), seed queue
                         with just the given MAJ_CATs, dispatch workers.
    """
    t0 = time.time()
    n_workers = max(MIN_WORKERS, min(MAX_WORKERS, int(n_workers or DEFAULT_WORKERS)))
    batch_id = batch_id or make_batch_id()
    engine = get_data_engine()

    result: Dict = {
        "batch_id":        batch_id,
        "listed_opts":     0,
        "alloc_rows":      0,
        "ship_qty_total":  0.0,
        "hold_qty_total":  0.0,
        "errors":          [],
        "duration_sec":    0.0,
    }

    # ── Stage A + Stage B run once on main thread (skipped on retry) ──
    if only_majcats is None:
        with engine.connect() as conn:
            if not rne._exists(conn, working_table) or not rne._exists(conn, msa_var_table):
                logger.warning(
                    f"[C-py] missing {working_table} or {msa_var_table} — skipping"
                )
                result["duration_sec"] = round(time.time() - t0, 1)
                return result

            # Stage A
            rne._stage_a_add_columns(conn, working_table)
            rne._stage_a_apply_rules(
                conn, working_table, size_threshold, min_size_count,
                tbl_trivial_factor,
                pri_ct_check_rl=pri_ct_check_rl,
                pri_ct_check_tbc=pri_ct_check_tbc,
            )
            rne._stage_a_assign_tier(conn, working_table)
            rne._stage_a_assign_rank(conn, working_table)
            listed_count = rne._stage_a_materialize_listed(
                conn, working_table, listed_table
            )
            logger.info(f"[A] listed={listed_count}")
            result["listed_opts"] = listed_count
            if listed_count == 0:
                result["duration_sec"] = round(time.time() - t0, 1)
                return result

            # Stage B
            base_rows = rne._stage_b_explode(
                conn, listed_table, alloc_table, msa_var_table
            )
            logger.info(f"[B] alloc rows = {base_rows}")
            if base_rows == 0:
                result["duration_sec"] = round(time.time() - t0, 1)
                return result
            rne._stage_b_fill_cont(conn, alloc_table, cont_table)
            rne._stage_b_fill_targets(conn, alloc_table, var_grid_table)
            rne._stage_b_indexes(conn, alloc_table)

            # Primary-grid map + _REM shadow columns (seeded from originals).
            grids = rne._discover_primary_grids(conn)
            logger.info(f"[C-py] primary grids = {list(grids.keys())}")
            if rne.ENABLE_PER_OPT_REVALIDATION:
                rne._init_rem_columns(conn, working_table, grids)

            # Seed the queue: one PENDING row per MAJ_CAT in alloc_table.
            total = seed_queue(conn, batch_id, alloc_table,
                               "python_parallel", only_majcats=None)
    else:
        # Retry path — alloc_table & working_table already prepared.
        with engine.connect() as conn:
            grids = rne._discover_primary_grids(conn)
            total = seed_queue(conn, batch_id, alloc_table,
                               "python_parallel", only_majcats=only_majcats)

    if total == 0:
        logger.warning(f"[C-py] queue empty for batch_id={batch_id} — nothing to do")
        result["duration_sec"] = round(time.time() - t0, 1)
        return result

    logger.info(
        f"[C-py] batch_id={batch_id} total_majcats={total} workers={n_workers}"
    )

    # ── Stage C — parallel by MAJ_CAT ──
    state_lock = threading.Lock()

    def worker(worker_id: int):
        # Each thread = own engine connection = own SQL Server session = own
        # #nre_pool. Pool is built once per worker covering all MAJ_CATs;
        # decrements happen only on rows in this worker's claimed MAJ_CATs.
        # Re-establish loguru's session_id binding for this thread (contextvars
        # don't propagate across ThreadPoolExecutor) so worker errors land in
        # the per-session log file.
        wlogger = logger.bind(session_id=batch_id)
        spid = 0
        with engine.connect() as wconn:
            # Capture this connection's SPID + register it with the cancel
            # registry so /listing/cancel-batch can KILL the in-flight
            # query when the user clicks Cancel Batch.
            try:
                spid = ac.get_current_spid(wconn)
                if spid:
                    ac.register_spid(batch_id, spid)
                    wlogger.info(f"[C-py-W{worker_id}] spid={spid} registered for cancel")
            except Exception:
                pass

            # Lower this session's deadlock priority so SQL Server picks it
            # as the victim before any other connection. Combined with the
            # WITH (ROWLOCK) hints inside _run_band_and_revalidate_batched
            # and the retry-with-jitter loop below, the rare cross-page
            # deadlock now resolves automatically instead of failing the
            # whole MAJ_CAT.
            try:
                run_sql(wconn, "SET DEADLOCK_PRIORITY LOW")
            except Exception as e:
                wlogger.warning(f"[C-py-W{worker_id}] could not set deadlock priority: {e}")

            try:
                rne._stage_c_build_pool(wconn, alloc_table)
            except Exception as e:
                wlogger.error(f"[C-py-W{worker_id}] pool build failed: {e}")
                ac.unregister_spid(batch_id, spid)
                return

            try:
                while True:
                    # Cooperative cancel: if the user clicked Cancel Batch,
                    # exit before pulling another MAJ_CAT.
                    if ac.is_cancelled(batch_id):
                        wlogger.warning(
                            f"[C-py-W{worker_id}] cancel detected — exiting cleanly"
                        )
                        return
                    # Short-lived claim conn so the long-running wconn doesn't
                    # hold the queue page-lock across the entire MAJ_CAT.
                    with engine.connect() as claim_conn:
                        mc = claim_next(claim_conn, batch_id, worker_id)
                    if mc is None:
                        return  # queue exhausted

                    t_mc = time.time()
                    try:
                        # Deadlock-tolerant retry. With 8 workers all hitting
                        # the same alloc_table, SQL Server can pick any of them
                        # as the deadlock victim (state 40001 / err 1205). Retry
                        # the WHOLE MAJ_CAT a few times with backoff + jitter
                        # before giving up — much cheaper than failing the row
                        # and using the 2-attempt queue retry budget.
                        _DEADLOCK_TOKENS = ("40001", "1205", "deadlock")
                        last_exc = None
                        for retry in range(4):  # 1 initial + 3 retries
                            # Bail out of the retry loop the moment cancel
                            # is requested — don't burn the budget retrying
                            # a doomed MAJ_CAT.
                            if ac.is_cancelled(batch_id):
                                wlogger.warning(
                                    f"[C-py-W{worker_id}] MAJ_CAT={mc} "
                                    f"cancelled mid-retry"
                                )
                                raise RuntimeError("cancelled by user")
                            try:
                                _run_one_majcat(
                                    wconn, working_table, alloc_table, mc, grids,
                                    pri_ct_check_rl=pri_ct_check_rl,
                                    pri_ct_check_tbc=pri_ct_check_tbc,
                                )
                                last_exc = None
                                break
                            except Exception as exc:
                                es = str(exc)
                                if any(t in es for t in _DEADLOCK_TOKENS):
                                    last_exc = exc
                                    # Roll the wconn transaction back so the
                                    # next attempt starts from a clean slate.
                                    try: wconn.rollback()
                                    except Exception: pass
                                    wait = 0.4 * (2 ** retry) + random.uniform(0, 0.4)
                                    wlogger.warning(
                                        f"[C-py-W{worker_id}] MAJ_CAT={mc} "
                                        f"deadlock — retry {retry+1}/3 in {wait:.1f}s"
                                    )
                                    time.sleep(wait)
                                    continue
                                raise  # non-retryable
                        if last_exc is not None:
                            raise last_exc
                        s = wconn.execute(text(f"""
                            SELECT ISNULL(SUM(SHIP_QTY),0),
                                   ISNULL(SUM(HOLD_QTY),0),
                                   COUNT(*)
                            FROM [{alloc_table}] WHERE MAJ_CAT = :mc
                        """), {"mc": mc}).fetchone()
                        ship_mc = float(s[0] or 0)
                        hold_mc = float(s[1] or 0)
                        rows_mc = int(s[2] or 0)
                        dur = time.time() - t_mc

                        with engine.connect() as upd_conn:
                            mark_done(upd_conn, batch_id, mc,
                                      ship_mc, hold_mc, rows_mc, dur)
                            prog = get_progress(upd_conn, batch_id)

                        wlogger.info(
                            f"[C-py-W{worker_id}] {prog['done']}/{prog['total']} "
                            f"({prog['pct']}%) — MAJ_CAT={mc} "
                            f"ship={ship_mc:.0f} hold={hold_mc:.0f} "
                            f"rows={rows_mc} in {dur:.1f}s"
                        )
                    except Exception as e:
                        err = str(e)[:2000]
                        dur = time.time() - t_mc
                        wlogger.error(
                            f"[C-py-W{worker_id}] MAJ_CAT={mc} FAILED in "
                            f"{dur:.1f}s: {err}"
                        )
                        try:
                            with engine.connect() as upd_conn:
                                mark_failed(upd_conn, batch_id, mc, err, dur)
                        except Exception as e2:
                            wlogger.error(
                                f"[C-py-W{worker_id}] mark_failed itself failed "
                                f"for MAJ_CAT={mc}: {e2}"
                            )
                        with state_lock:
                            result["errors"].append({"maj_cat": mc, "error": err})
                        # continue the loop — pick next MAJ_CAT
            finally:
                # Always unregister this SPID so KILL won't target a
                # connection that's already returned to the pool.
                ac.unregister_spid(batch_id, spid)

    with ThreadPoolExecutor(max_workers=n_workers,
                            thread_name_prefix="ars-alloc") as ex:
        futures = [ex.submit(worker, i) for i in range(n_workers)]
        for f in as_completed(futures):
            f.result()  # propagate unexpected exceptions

    # NOTE: do NOT call ac.cleanup(batch_id) here. The orchestrator
    # (_generate_listing_impl) needs to query is_cancelled / is_session_cancelled
    # AFTER this function returns to decide whether to skip Part 8.4 / 8.5
    # / 8.6. Cleanup happens once at the very end of the daemon thread's
    # finally block in listing.py:_run_generate_in_thread.

    # ── Finalise on main thread (verbatim from rule_engine_new._stage_c_waterfall) ──
    with engine.connect() as conn:
        run_sql(conn, f"UPDATE [{alloc_table}] SET ALLOC_QTY = SHIP_QTY")
        run_sql(conn, f"""
            UPDATE [{alloc_table}] SET
                ALLOC_STATUS = CASE
                    WHEN SHIP_QTY + HOLD_QTY > 0
                         AND SHIP_QTY + HOLD_QTY
                             >= CASE WHEN ISNULL(SZ_MBQ_WH,0) * ISNULL(I_ROD,1)
                                          - ISNULL(SZ_STK,0) > 0
                                     THEN ISNULL(SZ_MBQ_WH,0) * ISNULL(I_ROD,1)
                                          - ISNULL(SZ_STK,0)
                                     ELSE 0 END
                         THEN 'ALLOCATED'
                    WHEN SHIP_QTY > 0                 THEN 'PARTIAL'
                    ELSE 'SKIPPED' END,
                SKIP_REASON = CASE
                    WHEN SHIP_QTY = 0 AND HOLD_QTY = 0
                         AND ISNULL(SZ_MBQ_WH,0) * ISNULL(I_ROD,1)
                             - ISNULL(SZ_STK,0) <= 0
                         THEN 'ALREADY_STOCKED'
                    WHEN SHIP_QTY = 0 AND HOLD_QTY = 0 THEN 'NO_POOL_OR_DEMAND'
                    ELSE SKIP_REASON END
        """)

        # Stage D — reflect back to working table (unchanged from sequential).
        rne._stage_d_reflect(conn, working_table, alloc_table)

        # Final totals from the alloc table (drives the API response).
        totals = conn.execute(text(f"""
            SELECT COUNT(*), ISNULL(SUM(SHIP_QTY),0), ISNULL(SUM(HOLD_QTY),0)
            FROM [{alloc_table}]
            WHERE ISNULL(SHIP_QTY,0) > 0 OR ISNULL(HOLD_QTY,0) > 0
        """)).fetchone()
        result["alloc_rows"]     = int(totals[0] or 0)
        result["ship_qty_total"] = float(totals[1] or 0)
        result["hold_qty_total"] = float(totals[2] or 0)

        # Queue-level summary for the response.
        prog = get_progress(conn, batch_id)
        summary = get_done_summary(conn, batch_id)
        result["done"]   = prog["done"]
        result["failed"] = prog["failed"]
        result["queue_summary"] = summary

    result["duration_sec"] = round(time.time() - t0, 1)
    logger.info(
        f"[C-py] DONE batch={batch_id} listed={result['listed_opts']} "
        f"alloc_rows={result['alloc_rows']} ship={result['ship_qty_total']:.0f} "
        f"hold={result['hold_qty_total']:.0f} "
        f"done={result.get('done',0)} failed={result.get('failed',0)} "
        f"in {result['duration_sec']}s"
    )
    return result


# ---------------------------------------------------------------------------
# Per-MAJ_CAT waterfall — the inner loop that one worker runs for one MAJ_CAT.
# Mirrors rule_engine_new._stage_c_waterfall but scoped to a single MAJ_CAT
# and using only ranks that actually exist for that MAJ_CAT (not 1..max).
# ---------------------------------------------------------------------------
def _run_one_majcat(conn, working_table, alloc_table, mc, grids,
                    pri_ct_check_rl: bool, pri_ct_check_tbc: bool):
    """
    Run RL → TBC → TBL waterfall for ONE MAJ_CAT.

    Execution order: OPT_TYPE → round → ST_RANK → OPT_PRIORITY_RANK.
    Round 1 is completed for ALL stores (in ST_RANK priority order) before
    round 2 starts. Within each round, the store with the best ST_RANK (=1)
    exhausts its OPTs before the next store starts. Revalidation (MSA_FNL_Q_REM,
    PRI_CT_REM, skip rules) is scoped to the current store via the werks
    parameter so one store's deductions don't prematurely skip another.
    """
    for ot in rne.OPT_TYPE_ORDER:
        bounds = conn.execute(text(f"""
            SELECT ISNULL(MAX(I_ROD), 0)
            FROM [{alloc_table}]
            WHERE OPT_TYPE = :ot AND MAJ_CAT = :mc
        """), {"ot": ot, "mc": mc}).fetchone()
        max_round = int(bounds[0] or 0)
        if max_round == 0:
            continue

        for r in range(1, max_round + 1):
            # Reset round deltas once per round, scoped to this MAJ_CAT.
            run_sql(conn, f"""
                UPDATE [{alloc_table}]
                   SET ROUND_SHIP = 0, ROUND_HOLD = 0
                 WHERE OPT_TYPE = :ot AND MAJ_CAT = :mc
            """, {"ot": ot, "mc": mc})

            # Distinct (ST_RANK, WERKS) pairs for this round, best store first.
            store_rows = conn.execute(text(f"""
                SELECT DISTINCT ST_RANK, WERKS
                FROM [{alloc_table}]
                WHERE OPT_TYPE = :ot
                  AND MAJ_CAT  = :mc
                  AND ISNULL(I_ROD, 1) >= :r
                  AND ST_RANK IS NOT NULL
                  AND ISNULL(ALLOC_STATUS, 'PENDING')
                      NOT IN ('SKIPPED', 'INELIGIBLE', 'ALLOCATED')
                ORDER BY ST_RANK
            """), {"ot": ot, "mc": mc, "r": r}).fetchall()

            for (st_rank, werks) in store_rows:
                # OPT_PRIORITY_RANKs for this specific store in this round.
                ranks = [
                    int(row[0]) for row in conn.execute(text(f"""
                        SELECT DISTINCT OPT_PRIORITY_RANK
                        FROM [{alloc_table}]
                        WHERE OPT_TYPE = :ot
                          AND MAJ_CAT  = :mc
                          AND WERKS    = :wk
                          AND ISNULL(I_ROD, 1) >= :r
                          AND OPT_PRIORITY_RANK IS NOT NULL
                          AND ISNULL(ALLOC_STATUS, 'PENDING')
                              NOT IN ('SKIPPED', 'INELIGIBLE', 'ALLOCATED')
                        ORDER BY OPT_PRIORITY_RANK
                    """), {"ot": ot, "mc": mc, "wk": werks, "r": r}).fetchall()
                    if row[0] is not None
                ]
                if not ranks:
                    continue

                for rank in ranks:
                    rne._run_band_and_revalidate_batched(
                        conn, working_table, alloc_table,
                        ot, r, rank, grids, maj_cat=mc,
                        pri_ct_check_rl=pri_ct_check_rl,
                        pri_ct_check_tbc=pri_ct_check_tbc,
                        werks=werks,
                    )
