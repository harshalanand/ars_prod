"""
rule_engine_parallel_sql.py

PHASE 2 — Parallel allocation that pushes the entire per-MAJ_CAT waterfall
into a SQL Server stored procedure (dbo.usp_ars_allocate_majcat). The
Python side does Stage A + Stage B (same as sequential / python_parallel),
seeds the queue, then spawns N workers — each worker just calls the proc
once per claimed MAJ_CAT.

Why this is faster than python_parallel
---------------------------------------
python_parallel pays ~10 SQL round-trips per band (alloc + 9 revalidation
statements). With ~7,800 bands per full run that is ~78,000 round-trips,
each costing ~10 ms of network latency. sql_parallel reduces that to
~ONE round-trip per MAJ_CAT — the proc loops every band server-side.

Correctness invariant
---------------------
The stored proc is a verbatim T-SQL port of rule_engine_new._stage_c_run_band
and _revalidate_after_band. The 3-mode diff script
(scripts/validate_alloc_modes.py) confirms SHIP_QTY / HOLD_QTY are
bit-identical across sequential, python_parallel, and sql_parallel.

Deployment
----------
Before first use, deploy the proc:
    python scripts/run_011_alloc_majcat_queue.py    # queue table
    python scripts/run_012_deploy_alloc_proc.py     # stored proc
"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine
from app.services import rule_engine_new as rne
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


# Lowered from 8/16 to 4/8 — same rationale as the pandas/python_parallel
# orchestrators: 8 worker threads in a single uvicorn process saturate the
# Python GIL and starve unrelated endpoints (auth/login etc.). Bump higher
# only when running uvicorn with --workers N in production.
DEFAULT_WORKERS = int(os.getenv("ARS_PARALLEL_WORKERS", "4"))
MIN_WORKERS = 2
MAX_WORKERS = 8

# Path to the .sql file that defines the three stored procs. The orchestrator
# auto-deploys these on first use if the main proc isn't found in dbo.
_SQL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "sql", "usp_ars_allocate_majcat.sql",
)
_PROC_LOCK = threading.Lock()
_PROC_NAMES = (
    "usp_ars_allocate_majcat",
    "_usp_ars_alloc_band_one",
    "_usp_ars_revalidate_band_one",
)
# First sql_parallel call per process always force-redeploys, so a backend
# restart is enough to pick up .sql edits without manual deploy steps.
_FIRST_CALL_DONE = False


def _split_on_go(sql: str) -> List[str]:
    """Split a T-SQL script on GO batch separators (case-insensitive,
    line-oriented). Identical to scripts/run_012_deploy_alloc_proc.py."""
    out, buf = [], []
    for line in sql.splitlines():
        if line.strip().upper() == "GO":
            if buf:
                out.append("\n".join(buf))
                buf = []
        else:
            buf.append(line)
    if buf:
        out.append("\n".join(buf))
    return [b for b in out if b.strip()]


def _check_procs(conn) -> Dict[str, bool]:
    """Return {proc_name: exists_in_dbo} for the three procs we need.
    Schema-qualified — finds the proc only when it's reachable as
    `dbo.<name>` (matches how the orchestrator EXECs it)."""
    out: Dict[str, bool] = {}
    for name in _PROC_NAMES:
        row = conn.execute(text(
            "SELECT 1 FROM sys.procedures p "
            "INNER JOIN sys.schemas s ON p.schema_id = s.schema_id "
            "WHERE s.name = 'dbo' AND p.name = :n"
        ), {"n": name}).scalar()
        out[name] = bool(row)
    return out


def ensure_alloc_proc(force: bool = False) -> None:
    """
    Idempotent: deploy dbo.usp_ars_allocate_majcat (and helpers) from the
    bundled .sql file if any of the three procs are missing in the Data DB.

    Uses SQLAlchemy's `isolation_level="AUTOCOMMIT"` execution option for
    DDL — the canonical SQLAlchemy pattern. The earlier `raw_connection() +
    raw.autocommit = True` approach silently rolled back CREATE PROCEDURE
    on Azure SQL because the pooled raw connection had an open implicit
    transaction that the autocommit toggle didn't commit.

    Verifies each CREATE PROCEDURE after the batch executes — if a batch
    claims success but the proc didn't land, fail loudly with the bad
    batch's text instead of returning a misleading "all green" result.
    """
    global _FIRST_CALL_DONE
    engine = get_data_engine()
    with _PROC_LOCK:
        # First call per process: force a redeploy so any edit to the .sql
        # file lands automatically on backend restart. Subsequent calls
        # within the same process do the cheap existence check only.
        first_call = not _FIRST_CALL_DONE
        if first_call:
            force = True
            _FIRST_CALL_DONE = True

        # ── 1. Initial existence check ──
        with engine.connect() as conn:
            db_name = conn.execute(text("SELECT DB_NAME()")).scalar()
            present = _check_procs(conn)
        missing = [n for n, ok in present.items() if not ok]
        logger.info(
            f"[C-sql] proc check on DB={db_name!r}: "
            f"present={[n for n,ok in present.items() if ok]} "
            f"missing={missing}"
            + (" — first call this process, forcing redeploy" if first_call else "")
        )
        if not missing and not force:
            return

        if not os.path.exists(_SQL_PATH):
            raise FileNotFoundError(
                f"SQL file not found: {_SQL_PATH}. Cannot auto-deploy. "
                f"Run `python scripts/run_012_deploy_alloc_proc.py` instead."
            )
        with open(_SQL_PATH, encoding="utf-8") as f:
            script = f.read()
        batches = _split_on_go(script)
        logger.info(
            f"[C-sql] auto-deploying ({len(batches)} batches) "
            f"to DB={db_name!r} from {_SQL_PATH}"
        )

        # ── 2. Deploy via SQLAlchemy AUTOCOMMIT — DDL commits per-statement ──
        # Each batch runs as its own DDL transaction so a partial failure
        # mid-deploy doesn't unwind earlier successful CREATEs.
        ac_engine = engine.execution_options(isolation_level="AUTOCOMMIT")
        with ac_engine.connect() as conn:
            for i, b in enumerate(batches, 1):
                head = b.strip().splitlines()[0][:80]
                try:
                    # exec_driver_sql sends the raw string directly to the
                    # DBAPI without SQLAlchemy parameter parsing — safer
                    # for batches that contain T-SQL '@var' tokens.
                    conn.exec_driver_sql(b)
                except Exception as e:
                    logger.error(
                        f"[C-sql] auto-deploy FAILED at batch {i}/{len(batches)}: {e}"
                    )
                    logger.error(f"[C-sql] failing batch head: {head}")
                    raise

                # After every batch, snapshot the procs that exist in dbo.
                # This pinpoints which batch actually creates each proc — if
                # CREATE PROCEDURE 'succeeds' but the proc doesn't appear,
                # the failure is right here, not a deferred mystery.
                present_now = _check_procs(conn)
                pres_str = ", ".join(
                    n for n, ok in present_now.items() if ok
                ) or "(none)"
                logger.info(
                    f"[C-sql]   batch {i}/{len(batches)} OK — "
                    f"dbo procs now: [{pres_str}] | head: {head}"
                )

        # ── 3. Final verification on a fresh connection ──
        with engine.connect() as conn:
            present_after = _check_procs(conn)
        still_missing = [n for n, ok in present_after.items() if not ok]
        if still_missing:
            raise RuntimeError(
                f"[C-sql] auto-deploy reported success but procs still "
                f"missing in dbo: {still_missing}. "
                f"User likely lacks CREATE PROCEDURE permission in dbo on "
                f"DB={db_name!r}, or default schema is not dbo. "
                f"Workaround: have a DBA run scripts/run_012_deploy_alloc_proc.py "
                f"from an account with CREATE PROCEDURE rights."
            )
        logger.info(f"[C-sql] auto-deploy complete on DB={db_name!r}")


def _grids_to_json(grids: Dict[str, Dict]) -> str:
    """Serialise the discovered grid metadata for the stored proc."""
    return json.dumps([
        {
            "req_col":  req_col,
            "req_rem":  meta["req_rem"],
            "gh_col":   meta["gh_col"],
            "h_col":    meta["h_col"],
            "h_rem":    meta["h_rem"],
            "extras":   meta.get("extras") or [],
        }
        for req_col, meta in grids.items()
    ])


def run_listing_and_allocation_sql_parallel(
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
    t0 = time.time()
    n_workers = max(MIN_WORKERS, min(MAX_WORKERS, int(n_workers or DEFAULT_WORKERS)))
    batch_id = batch_id or make_batch_id()
    engine = get_data_engine()

    # Self-heal: deploy the stored proc on first use so the user doesn't
    # need to run scripts/run_012_deploy_alloc_proc.py separately. Once
    # successful this is a free metadata check on subsequent calls.
    try:
        ensure_alloc_proc()
    except Exception as e:
        logger.error(f"[C-sql] cannot deploy usp_ars_allocate_majcat: {e}")
        raise

    result: Dict = {
        "batch_id":        batch_id,
        "listed_opts":     0,
        "alloc_rows":      0,
        "ship_qty_total":  0.0,
        "hold_qty_total":  0.0,
        "errors":          [],
        "duration_sec":    0.0,
    }

    # ── Stage A + Stage B (single thread; skipped on retry) ──
    if only_majcats is None:
        with engine.connect() as conn:
            if not rne._exists(conn, working_table) or not rne._exists(conn, msa_var_table):
                logger.warning(
                    f"[C-sql] missing {working_table} or {msa_var_table} — skipping"
                )
                result["duration_sec"] = round(time.time() - t0, 1)
                return result

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

            grids = rne._discover_primary_grids(conn)
            logger.info(f"[C-sql] primary grids = {list(grids.keys())}")
            if rne.ENABLE_PER_OPT_REVALIDATION:
                rne._init_rem_columns(conn, working_table, grids)

            grids_json = _grids_to_json(grids)
            total = seed_queue(conn, batch_id, alloc_table,
                               "sql_parallel", only_majcats=None)
    else:
        with engine.connect() as conn:
            grids = rne._discover_primary_grids(conn)
            grids_json = _grids_to_json(grids)
            total = seed_queue(conn, batch_id, alloc_table,
                               "sql_parallel", only_majcats=only_majcats)

    if total == 0:
        logger.warning(f"[C-sql] queue empty for batch_id={batch_id} — nothing to do")
        result["duration_sec"] = round(time.time() - t0, 1)
        return result

    logger.info(
        f"[C-sql] batch_id={batch_id} total_majcats={total} workers={n_workers}"
    )

    state_lock = threading.Lock()

    def worker(worker_id: int):
        # Re-establish loguru's session_id context inside this thread —
        # contextvars don't propagate across ThreadPoolExecutor boundaries,
        # so without this every worker's logger.error() event would be
        # filtered out by the per-session sink (which keys on session_id).
        # Using batch_id == session_id (set by the async generate handler).
        wlogger = logger.bind(session_id=batch_id)
        with engine.connect() as wconn:
            while True:
                with engine.connect() as claim_conn:
                    mc = claim_next(claim_conn, batch_id, worker_id)
                if mc is None:
                    return

                t_mc = time.time()
                try:
                    # One round-trip — the proc runs the entire RL/TBC/TBL
                    # waterfall for this MAJ_CAT server-side.
                    row = wconn.execute(text("""
                        DECLARE @ship FLOAT, @hold FLOAT, @rows INT;
                        EXEC dbo.usp_ars_allocate_majcat
                            @maj_cat          = :mc,
                            @working_table    = :wt,
                            @alloc_table      = :at,
                            @msa_var_table    = :mvt,
                            @grids_json       = :gj,
                            @pri_ct_check_rl  = :rl,
                            @pri_ct_check_tbc = :tbc,
                            @ship_out         = @ship OUTPUT,
                            @hold_out         = @hold OUTPUT,
                            @rows_out         = @rows OUTPUT;
                        SELECT @ship AS ship, @hold AS hold, @rows AS rws;
                    """), {
                        "mc":  mc,
                        "wt":  working_table,
                        "at":  alloc_table,
                        "mvt": msa_var_table,
                        "gj":  grids_json,
                        "rl":  int(bool(pri_ct_check_rl)),
                        "tbc": int(bool(pri_ct_check_tbc)),
                    }).fetchone()
                    ship_mc = float(row[0] or 0)
                    hold_mc = float(row[1] or 0)
                    rows_mc = int(row[2] or 0)
                    dur = time.time() - t_mc

                    with engine.connect() as upd_conn:
                        mark_done(upd_conn, batch_id, mc,
                                  ship_mc, hold_mc, rows_mc, dur)
                        prog = get_progress(upd_conn, batch_id)

                    wlogger.info(
                        f"[C-sql-W{worker_id}] {prog['done']}/{prog['total']} "
                        f"({prog['pct']}%) — MAJ_CAT={mc} "
                        f"ship={ship_mc:.0f} hold={hold_mc:.0f} "
                        f"rows={rows_mc} in {dur:.1f}s"
                    )
                except Exception as e:
                    err = str(e)[:2000]
                    dur = time.time() - t_mc
                    wlogger.error(
                        f"[C-sql-W{worker_id}] MAJ_CAT={mc} FAILED in "
                        f"{dur:.1f}s: {err}"
                    )
                    try:
                        with engine.connect() as upd_conn:
                            mark_failed(upd_conn, batch_id, mc, err, dur)
                    except Exception as e2:
                        wlogger.error(
                            f"[C-sql-W{worker_id}] mark_failed itself failed "
                            f"for MAJ_CAT={mc}: {e2}"
                        )
                    with state_lock:
                        result["errors"].append({"maj_cat": mc, "error": err})

    with ThreadPoolExecutor(max_workers=n_workers,
                            thread_name_prefix="ars-sql") as ex:
        futures = [ex.submit(worker, i) for i in range(n_workers)]
        for f in as_completed(futures):
            f.result()

    # ── Finalise on main thread (verbatim from sequential path) ──
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
        rne._stage_d_reflect(conn, working_table, alloc_table)

        totals = conn.execute(text(f"""
            SELECT COUNT(*), ISNULL(SUM(SHIP_QTY),0), ISNULL(SUM(HOLD_QTY),0)
            FROM [{alloc_table}]
            WHERE ISNULL(SHIP_QTY,0) > 0 OR ISNULL(HOLD_QTY,0) > 0
        """)).fetchone()
        result["alloc_rows"]     = int(totals[0] or 0)
        result["ship_qty_total"] = float(totals[1] or 0)
        result["hold_qty_total"] = float(totals[2] or 0)

        prog = get_progress(conn, batch_id)
        summary = get_done_summary(conn, batch_id)
        result["done"]   = prog["done"]
        result["failed"] = prog["failed"]
        result["queue_summary"] = summary

    result["duration_sec"] = round(time.time() - t0, 1)

    # Belt & braces: if any worker failed, log every error message on the
    # main thread (which has session_id in its loguru context, so the
    # per-session sink keeps these). Also pull error_msg from the queue
    # table so we surface anything mark_failed wrote even if the worker's
    # in-thread log got lost for any reason.
    if result.get("failed", 0):
        with engine.connect() as conn:
            failed_rows = conn.execute(text(f"""
                SELECT MAJ_CAT, ATTEMPTS, ERROR_MSG
                FROM ARS_ALLOC_MAJCAT_QUEUE
                WHERE BATCH_ID = :b AND STATUS = 'FAILED'
                ORDER BY MAJ_CAT
            """), {"b": batch_id}).fetchall()
        for r in failed_rows:
            logger.error(
                f"[C-sql] worker error: MAJ_CAT={r[0]} "
                f"attempts={r[1]} error={r[2]}"
            )

    logger.info(
        f"[C-sql] DONE batch={batch_id} listed={result['listed_opts']} "
        f"alloc_rows={result['alloc_rows']} ship={result['ship_qty_total']:.0f} "
        f"hold={result['hold_qty_total']:.0f} "
        f"done={result.get('done',0)} failed={result.get('failed',0)} "
        f"in {result['duration_sec']}s"
    )
    return result
