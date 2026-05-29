"""
rule_engine_pandas.py

In-memory pandas/numpy port of Stage C (the allocation waterfall).
Same Stage A + Stage B as the other engines (they run once in SQL); the hot
loop — RL → TBC → TBL × rounds × ranks × bands — runs entirely in pandas
on per-MAJ_CAT slices, then results are bulk-written back to the DB at the
end. A single MAJ_CAT that took 23 minutes in python_parallel finishes in
seconds because every "UPDATE" becomes a numpy vector op.

Concurrency: thread-fanned by MAJ_CAT against the same DB queue table
(ARS_ALLOC_MAJCAT_QUEUE) used by python_parallel / sql_parallel, so the UI
progress endpoint and retry endpoint work unchanged. Each worker takes a
disjoint MAJ_CAT slice — no shared mutable DataFrame state, no locks on
the hot path.

Correctness: the pandas waterfall mirrors rule_engine_new._stage_c_run_band
and _revalidate_after_band statement-by-statement. Stable mergesort
reproduces SQL ROW_NUMBER() tie-breaking on (OPT_PRIORITY_RANK, ST_RANK).
"""
from __future__ import annotations

import os
import queue
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger
from sqlalchemy import text

from app.core.config import get_settings
from app.database.session import get_data_engine
from app.services import alloc_cancellation as ac
from app.services import rule_engine_new as rne
from app.services.alloc_queue import (
    claim_next,
    get_done_summary,
    get_failed_list,
    get_progress,
    make_batch_id,
    mark_done,
    mark_failed,
    mark_in_progress,
    seed_queue,
)
from app.utils.db_helpers import run_sql, retry_on_deadlock, DeadlockStats


# Default 4 (was 8). Pandas operations are CPU-bound and hold Python's GIL,
# so spawning 8 ThreadPoolExecutor workers in a single uvicorn process
# saturates the GIL — /auth/login, /listing/active-job and any other
# unrelated endpoint sharing this process get starved and the upstream
# proxy hits its 120s read-timeout window before the response makes it
# back. With 4 workers the process retains CPU headroom for foreground
# requests. Override with the ARS_PARALLEL_WORKERS env var if you're
# running uvicorn with --workers N (each uvicorn worker is a separate
# process => own GIL => safe to fan out wider per process).
DEFAULT_WORKERS = int(os.getenv("ARS_PARALLEL_WORKERS", "4"))
MIN_WORKERS = 2
MAX_WORKERS = 8   # was 16; capped lower for the same GIL-saturation reason

# Below this many MAJ_CATs we don't bother spawning a process pool — the
# subprocess startup cost (~1–2s per child on Windows spawn) dwarfs the
# work itself. Tiny inputs run inline on a single thread.
PROCESS_POOL_MIN_MAJCATS = 3

OPT_TYPE_ORDER = ["RL", "TBC", "TBL"]
POOL_KEYS = ["RDC", "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "VAR_ART", "SZ"]
OPT_KEYS  = ["WERKS", "MAJ_CAT", "GEN_ART_NUMBER", "CLR"]


# ---------------------------------------------------------------------------
# Per-MAJ_CAT worker — top-level so ProcessPoolExecutor can pickle it
# ---------------------------------------------------------------------------
def _pandas_run_one_majcat(args: Tuple[Any, ...]) -> Dict[str, Any]:
    """
    One MAJ_CAT, one process. Runs the in-memory waterfall, writes results
    back, marks the queue row DONE. Returns a small dict the parent uses
    for progress logging.

    Pickleable contract: every argument and the return value must be
    picklable so the pool can ship them across the process boundary.
    DataFrames are picklable; the slices we pass are typically a few MB.

    When `defer_writes=True` (15th tuple element), the worker skips the
    write-back + mark_done step and instead returns the computed DataFrames
    so the parent's dedicated writer thread can apply them serially.
    Eliminates 8-way writer-writer deadlocks on ARS_ALLOC_WORKING /
    ARS_LISTING_WORKING. See USE_WRITER_QUEUE flag.
    """
    # Backwards-compat unpack: defer_writes is optional (tuple may be 14 or 15
    # long). Older callers pass 14 elements; the new writer-queue path passes
    # 15 with defer_writes as the trailing flag.
    if len(args) == 15:
        (mc, a_slice, w_slice, grids, batch_id, alloc_table, working_table,
         pri_ct_check_rl, pri_ct_check_tbc,
         rl_mbq_cap_pct, tbc_mbq_cap_pct,
         size_threshold, min_size_count, opt_types, defer_writes) = args
    else:
        (mc, a_slice, w_slice, grids, batch_id, alloc_table, working_table,
         pri_ct_check_rl, pri_ct_check_tbc,
         rl_mbq_cap_pct, tbc_mbq_cap_pct,
         size_threshold, min_size_count, opt_types) = args
        defer_writes = False

    t_mc = time.time()
    worker_id = os.getpid()  # surfaced in QUEUE_TABLE.WORKER_ID for diagnostics

    # Reset per-MAJ_CAT deadlock counters so the snapshot we return at the
    # end of this function describes only this cat's retries (parent sums
    # them across all cats for the run-level summary log line).
    DeadlockStats.reset()

    # Stamp the row IN_PROGRESS so /listing/alloc-progress reflects active
    # workers in real time. Best-effort — if this fails the run still works,
    # just won't show in the live counter.
    try:
        eng = get_data_engine()
        with eng.connect() as upd:
            mark_in_progress(upd, batch_id, mc, worker_id)
    except Exception:
        pass

    _write_back_done = False  # declared outside try so except can always read it
    try:
        # Empty slice — mark done and bail.
        if a_slice is None or a_slice.empty:
            try:
                eng = get_data_engine()
                with eng.connect() as upd:
                    mark_done(upd, batch_id, mc, 0.0, 0.0, 0, time.time() - t_mc)
            except Exception:
                pass
            return {"mc": mc, "ship": 0.0, "hold": 0.0, "rows": 0,
                    "dur": time.time() - t_mc, "wb_secs": 0.0}

        a_in = a_slice.copy()
        w_in = w_slice.copy() if w_slice is not None else pd.DataFrame()

        # Load hold tracking for RL/TBC consume-from-hold logic.
        # Key: (WERKS, VAR_ART, SZ) — matches ARS_NL_TBL_HOLD_TRACKING PK.
        # RL/TBC rows draw from this warehouse hold before touching the RDC pool.
        hold_dict: Dict = {}
        var_arts = set(a_in['VAR_ART'].dropna().astype(str).tolist())
        if var_arts:
            try:
                _heng = get_data_engine()
                with _heng.connect() as _hc:
                    _hrows = _hc.execute(text(
                        "SELECT WERKS, VAR_ART, SZ, ISNULL(HOLD_REM, 0.0) AS hold_rem "
                        "FROM ARS_NL_TBL_HOLD_TRACKING "
                        "WHERE IS_CLOSED = 0 AND ISNULL(HOLD_REM, 0.0) > 0"
                    )).fetchall()
                    hold_dict = {
                        (str(r[0]), str(r[1]), str(r[2])): float(r[3])
                        for r in _hrows
                        if str(r[1]) in var_arts
                    }
            except Exception as _he:
                logger.warning(
                    f"[pandas] {mc}: hold_tracking load failed ({_he}) — "
                    f"hold tracking disabled for this MAJ_CAT"
                )

        a_out, w_out = _run_majcat_waterfall(
            a_in, w_in, grids,
            pri_ct_check_rl=pri_ct_check_rl,
            pri_ct_check_tbc=pri_ct_check_tbc,
            rl_mbq_cap_pct=rl_mbq_cap_pct,
            tbc_mbq_cap_pct=tbc_mbq_cap_pct,
            hold_dict=hold_dict if hold_dict else None,
            size_threshold=size_threshold,
            min_size_count=min_size_count,
            opt_types=opt_types,
        )
        ship_mc = float(a_out['SHIP_QTY'].fillna(0).sum())
        hold_mc = float(a_out['HOLD_QTY'].fillna(0).sum())
        rows_mc = int(len(a_out))

        # ── Writer-queue path: skip per-worker DB writes and hand results
        # back to the parent's single-writer thread. The parent serialises
        # all UPDATEs through one DB connection → zero writer-writer
        # contention. mark_done is deferred to the writer too, so the row
        # stays IN_PROGRESS in the queue until the bytes are actually in DB.
        if defer_writes:
            dur = time.time() - t_mc
            return {
                "mc":        mc,
                "a_out":     a_out,
                "w_out":     w_out,
                "ship":      ship_mc,
                "hold":      hold_mc,
                "rows":      rows_mc,
                "dur":       dur,
                "wb_secs":   0.0,
                "deferred":  True,
                "deadlocks": DeadlockStats.snapshot(),
            }

        # Live write-back for THIS MAJ_CAT — disjoint slices, safe to run
        # concurrently across processes (different rows in alloc/working).
        # Wrapped in retry_on_deadlock: even with ROWLOCK/UPDLOCK hints,
        # 8 concurrent workers can occasionally race on shared pages, so
        # we let SQL Server pick a victim and rerun cleanly (each call
        # opens its own raw_connection() so a retry gets a fresh tx).
        eng = get_data_engine()
        t_wb = time.time()
        retry_on_deadlock(
            lambda: _write_back_alloc(eng, alloc_table, a_out),
            label=f"write_back_alloc[{mc}]",
        )
        if not w_out.empty:
            retry_on_deadlock(
                lambda: _write_back_working(eng, working_table, w_out, grids),
                label=f"write_back_working[{mc}]",
            )
        wb_secs = time.time() - t_wb
        _write_back_done = True  # both write-backs committed to DB

        dur = time.time() - t_mc
        # mark_done in its own try/except: data is already in DB, so a
        # deadlock here must NOT flip the row to FAILED. Retry once on a
        # fresh connection; if that also fails, leave the row IN_PROGRESS
        # rather than trigger a misleading FAILED status.
        try:
            with eng.connect() as upd:
                mark_done(upd, batch_id, mc, ship_mc, hold_mc, rows_mc, dur)
        except Exception as md_err:
            try:
                time.sleep(0.3)
                with get_data_engine().connect() as c2:
                    mark_done(c2, batch_id, mc, ship_mc, hold_mc, rows_mc, dur)
                logger.warning(f"[pandas] {mc}: mark_done retry succeeded ({md_err})")
            except Exception as md_err2:
                logger.warning(
                    f"[pandas] {mc}: mark_done failed twice ({md_err2}) "
                    f"but write-backs committed — row stays IN_PROGRESS (data saved)"
                )

        return {"mc": mc, "ship": ship_mc, "hold": hold_mc, "rows": rows_mc,
                "dur": dur, "wb_secs": wb_secs,
                "deadlocks": DeadlockStats.snapshot()}

    except Exception as e:
        err = str(e)[:2000]
        dur = time.time() - t_mc
        if not _write_back_done:
            # True failure — waterfall or write-back exhausted retries.
            try:
                eng = get_data_engine()
                with eng.connect() as upd:
                    mark_failed(upd, batch_id, mc, err, dur)
            except Exception:
                pass
        else:
            # Shouldn't be reachable (write-back success path only raises
            # inside the inner mark_done try/except), but guard anyway.
            logger.warning(
                f"[pandas] {mc}: post-write-back exception ({err}); "
                f"NOT marking FAILED — data already committed"
            )
        return {"mc": mc, "error": err, "dur": dur,
                "deadlocks": DeadlockStats.snapshot()}


# ---------------------------------------------------------------------------
# Writer thread — drains computed MAJ_CAT results and applies them serially.
# Used only when USE_WRITER_QUEUE is True. One thread, one DB connection,
# ONE writer in the whole pipeline → zero writer-writer deadlocks possible.
# ---------------------------------------------------------------------------
_WRITER_SENTINEL = object()  # singleton signal to shut down the writer


def _writer_thread_fn(
    result_queue: "queue.Queue",
    alloc_table: str,
    working_table: str,
    grids: Dict,
    batch_id: str,
    stats: Dict,
) -> None:
    """Drain `result_queue`, write each MAJ_CAT's DataFrames sequentially,
    then mark_done. On a write failure (rare with single-writer), call
    mark_failed so the operator can retry just that MAJ_CAT.

    `stats` is a shared dict the parent populates with running totals
    (deadlocks, wb_total_secs) — written here, read by the parent at end."""
    eng = get_data_engine()
    while True:
        item = result_queue.get()
        try:
            if item is _WRITER_SENTINEL:
                return
            mc       = item["mc"]
            a_out    = item["a_out"]
            w_out    = item["w_out"]
            ship_mc  = item["ship"]
            hold_mc  = item["hold"]
            rows_mc  = item["rows"]
            t_wb     = time.time()
            wb_done  = False
            try:
                retry_on_deadlock(
                    lambda: _write_back_alloc(eng, alloc_table, a_out),
                    label=f"writer_alloc[{mc}]",
                )
                if not w_out.empty:
                    retry_on_deadlock(
                        lambda: _write_back_working(eng, working_table, w_out, grids),
                        label=f"writer_working[{mc}]",
                    )
                wb_done = True
                wb_secs = time.time() - t_wb
                stats["wb_total_secs"] = stats.get("wb_total_secs", 0.0) + wb_secs

                # Total per-MAJ_CAT duration = compute (in worker) + write-back.
                total_dur = float(item.get("dur", 0.0)) + wb_secs
                with eng.connect() as upd:
                    mark_done(upd, batch_id, mc, ship_mc, hold_mc, rows_mc, total_dur)

                # Aggregate deadlock telemetry the worker captured before
                # handing off — keeps the end-of-run summary line accurate.
                dl = item.get("deadlocks") or {}
                stats["dl_caught"]    = stats.get("dl_caught", 0)    + int(dl.get("caught", 0))
                stats["dl_succeeded"] = stats.get("dl_succeeded", 0) + int(dl.get("succeeded", 0))
                stats["dl_exhausted"] = stats.get("dl_exhausted", 0) + int(dl.get("exhausted", 0))

                # Live progress log — mirrors the inline path's log shape so
                # operators can grep [C-pd-pool] regardless of mode.
                try:
                    with eng.connect() as conn:
                        prog = get_progress(conn, batch_id)
                    prog_str = f"{prog['done']}/{prog['total']} ({prog['pct']}%)"
                except Exception:
                    prog_str = "?"
                logger.info(
                    f"[C-pd-writer] {prog_str} — MAJ_CAT={mc} "
                    f"ship={ship_mc:.0f} hold={hold_mc:.0f} "
                    f"rows={rows_mc} in {total_dur:.1f}s (wb={wb_secs:.1f}s)"
                )
            except Exception as e:
                err = str(e)[:2000]
                wb_secs = time.time() - t_wb
                if not wb_done:
                    try:
                        with eng.connect() as upd:
                            mark_failed(upd, batch_id, mc, err, wb_secs)
                    except Exception as me:
                        logger.error(
                            f"[C-pd-writer] mark_failed itself failed for "
                            f"MAJ_CAT={mc}: {me}"
                        )
                    stats.setdefault("errors", []).append({"maj_cat": mc, "error": err})
                    logger.error(
                        f"[C-pd-writer] MAJ_CAT={mc} write-back FAILED "
                        f"in {wb_secs:.1f}s: {err}"
                    )
                else:
                    logger.warning(
                        f"[C-pd-writer] MAJ_CAT={mc}: post-write exception "
                        f"({err}); data already committed — NOT marking FAILED"
                    )
        finally:
            result_queue.task_done()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def run_listing_and_allocation_pandas(
    working_table:  str = "ARS_LISTING_WORKING",
    listed_table:   str = "ARS_LISTED_OPT",
    alloc_table:    str = "ARS_ALLOC_WORKING",
    msa_var_table:  str = "ARS_MSA_VAR_ART",
    var_grid_table: str = "ARS_GRID_MJ_VAR_ART",
    cont_table:     str = "Master_CONT_SZ",
    n_workers:      int = DEFAULT_WORKERS,
    batch_id:       Optional[str] = None,
    only_majcats:   Optional[List[str]] = None,
    size_threshold: float = 0.6,
    min_size_count: int = 3,
    tbl_trivial_factor: float = 0.5,
    pri_ct_check_rl:  bool = False,
    pri_ct_check_tbc: bool = False,
    rl_mbq_cap_pct:  float = 0.0,
    tbc_mbq_cap_pct: float = 0.0,
    opt_types: Optional[List[str]] = None,  # restrict waterfall to these OPT_TYPEs only
    use_writer_queue: Optional[bool] = None, # per-run override; None = fall back to .env
) -> Dict:
    """
    Drop-in replacement for rule_engine_new.run_listing_and_allocation,
    with Stage C ported to pandas + thread-fanned by MAJ_CAT.
    """
    t0 = time.time()
    n_workers = max(MIN_WORKERS, min(MAX_WORKERS, int(n_workers or DEFAULT_WORKERS)))
    batch_id = batch_id or make_batch_id()
    engine = get_data_engine()

    # Audit-log the PRI/MBQ-cap gate state actually received by the engine.
    # Helps diagnose UI-toggle vs. server-state mismatches.
    logger.info(
        f"[engine] batch={batch_id} | "
        f"RL: {'PRI>=100 strict' if pri_ct_check_rl else f'MBQ-cap {rl_mbq_cap_pct}%'} | "
        f"TBC: {'PRI>=100 strict' if pri_ct_check_tbc else f'MBQ-cap {tbc_mbq_cap_pct}%'}"
    )

    result: Dict = {
        "batch_id":       batch_id,
        "listed_opts":    0,
        "alloc_rows":     0,
        "ship_qty_total": 0.0,
        "hold_qty_total": 0.0,
        "errors":         [],
        "duration_sec":   0.0,
    }

    # ── Stage A + Stage B (SQL, single thread; skipped on retry) ──
    if only_majcats is None:
        with engine.connect() as conn:
            if not rne._exists(conn, working_table) or not rne._exists(conn, msa_var_table):
                logger.warning(
                    f"[C-pd] missing {working_table} or {msa_var_table} — skipping"
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
                conn, listed_table, alloc_table, msa_var_table,
                pri_ct_check_rl=pri_ct_check_rl,
                pri_ct_check_tbc=pri_ct_check_tbc,
                opt_types=opt_types,
            )
            logger.info(f"[B] alloc rows = {base_rows}")
            if base_rows == 0:
                result["duration_sec"] = round(time.time() - t0, 1)
                return result
            rne._stage_b_fill_cont(conn, alloc_table, cont_table)
            rne._stage_b_fill_targets(conn, alloc_table, var_grid_table)
            rne._stage_b_indexes(conn, alloc_table)

            grids = rne._discover_primary_grids(conn)
            logger.info(f"[C-pd] primary grids = {list(grids.keys())}")
            if rne.ENABLE_PER_OPT_REVALIDATION:
                rne._init_rem_columns(conn, working_table, grids)

            total = seed_queue(conn, batch_id, alloc_table,
                               "pandas", only_majcats=None)
    else:
        with engine.connect() as conn:
            grids = rne._discover_primary_grids(conn)
            total = seed_queue(conn, batch_id, alloc_table,
                               "pandas", only_majcats=only_majcats)

    if total == 0:
        logger.warning(f"[C-pd] queue empty for batch_id={batch_id} — nothing to do")
        result["duration_sec"] = round(time.time() - t0, 1)
        return result

    logger.info(
        f"[C-pd] batch_id={batch_id} total_majcats={total} workers={n_workers}"
    )

    # ── Load tables once into pandas ──
    t_load = time.time()
    alloc_df, working_df, working_cols = _load_tables(
        engine, alloc_table, working_table, grids, only_majcats
    )
    logger.info(
        f"[C-pd] loaded alloc={len(alloc_df)} working={len(working_df)} "
        f"cols={len(working_cols)} in {time.time()-t_load:.1f}s"
    )

    # Per-MAJ_CAT slices — disjoint, so workers can take them by reference.
    alloc_groups   = {mc: g for mc, g in alloc_df.groupby('MAJ_CAT', sort=False)}
    working_groups = {mc: g for mc, g in working_df.groupby('MAJ_CAT', sort=False)}

    # ── Stage C — process-fanned by MAJ_CAT ──
    # Each MAJ_CAT runs in its own subprocess (ProcessPoolExecutor). The
    # in-memory waterfall is pandas/numpy → almost entirely GIL-bound, so
    # threads serialise (8 threads = 1 effective worker). Subprocesses each
    # have their own GIL → real parallelism. Each worker writes its own
    # MAJ_CAT slice back the moment the waterfall finishes, so the dashboard
    # ticks up live as MAJ_CATs complete.
    wb_total_secs = 0.0
    # Aggregate deadlock-retry telemetry across every worker; logged at the
    # end of Stage C so the operator sees one summary line instead of having
    # to grep the per-worker [deadlock-retry] warnings.
    dl_caught = 0
    dl_succeeded = 0
    dl_exhausted = 0

    _active_types = [ot for ot in OPT_TYPE_ORDER if not opt_types or ot in opt_types]
    # Per-run override beats .env default. UI passes True/False; CLI/retry
    # callers may omit it, in which case we fall back to settings.
    if use_writer_queue is None:
        use_writer_queue = bool(get_settings().USE_WRITER_QUEUE)
    else:
        use_writer_queue = bool(use_writer_queue)
    pool_args = [
        (
            mc,
            alloc_groups[mc],
            working_groups.get(mc, pd.DataFrame()),
            grids,
            batch_id,
            alloc_table,
            working_table,
            bool(pri_ct_check_rl),
            bool(pri_ct_check_tbc),
            float(rl_mbq_cap_pct),
            float(tbc_mbq_cap_pct),
            float(size_threshold),
            int(min_size_count),
            list(_active_types),
            # 15th element — defer_writes flag. When True the subprocess
            # returns the computed DataFrames instead of writing them
            # itself; the parent's writer thread (Pattern A) does all DB
            # UPDATEs sequentially. Backwards-compatible: legacy 14-tuple
            # callers still work (defer_writes defaults to False).
            bool(use_writer_queue),
        )
        for mc in alloc_groups
    ]

    use_pool = (len(pool_args) >= PROCESS_POOL_MIN_MAJCATS and n_workers > 1)

    # Writer-queue setup — only when both the flag is on AND we're using the
    # process pool (single-MAJ_CAT inline runs don't have a contention
    # problem and need no writer thread).
    writer_thread = None
    writer_queue: Optional["queue.Queue"] = None
    writer_stats: Dict[str, Any] = {}
    if use_pool and use_writer_queue:
        logger.info(
            f"[C-pd] USE_WRITER_QUEUE=ON → routing writes through "
            f"dedicated single-writer thread (zero writer-writer contention)"
        )
        # maxsize bounds memory if workers outpace the writer. Each pickle
        # is ~5 MB; cap depth at 2× worker count → ≤80 MB peak in worst case.
        writer_queue = queue.Queue(maxsize=max(4, n_workers * 2))
        writer_thread = threading.Thread(
            target=_writer_thread_fn,
            args=(writer_queue, alloc_table, working_table, grids,
                  batch_id, writer_stats),
            name=f"pd-writer-{batch_id}",
            daemon=True,
        )
        writer_thread.start()

    if use_pool:
        logger.info(
            f"[C-pd] dispatching {len(pool_args)} MAJ_CATs to "
            f"ProcessPoolExecutor(max_workers={n_workers})"
        )
        # max_workers can't exceed the # of tasks meaningfully — clamp it.
        actual_workers = min(n_workers, len(pool_args))
        with ProcessPoolExecutor(max_workers=actual_workers) as ex:
            futures = {ex.submit(_pandas_run_one_majcat, args): args[0]
                       for args in pool_args}
            for f in as_completed(futures):
                mc = futures[f]
                try:
                    r = f.result()
                except Exception as e:
                    err = str(e)[:2000]
                    logger.error(f"[C-pd-pool] MAJ_CAT={mc} subprocess raised: {err}")
                    result["errors"].append({"maj_cat": mc, "error": err})
                    continue

                # Aggregate this worker's deadlock counters for the
                # end-of-Stage-C summary log line. Done unconditionally —
                # even FAILED workers may have caught (and exhausted) retries.
                dl = r.get("deadlocks") or {}
                dl_caught    += int(dl.get("caught", 0))
                dl_succeeded += int(dl.get("succeeded", 0))
                dl_exhausted += int(dl.get("exhausted", 0))

                if r.get("error"):
                    result["errors"].append({"maj_cat": mc, "error": r["error"]})
                    logger.error(
                        f"[C-pd-pool] MAJ_CAT={mc} FAILED in {r.get('dur', 0):.1f}s: "
                        f"{r['error']}"
                    )
                elif r.get("deferred"):
                    # Writer-queue path: hand the computed DataFrames to the
                    # single writer thread. mark_done / progress logging /
                    # error handling all happen there (see _writer_thread_fn).
                    # Blocks briefly if the writer is behind — that's the
                    # backpressure mechanism keeping queue depth bounded.
                    writer_queue.put(r)
                else:
                    wb_total_secs += float(r.get("wb_secs", 0.0))
                    # Cheap progress query — one row from the queue table.
                    try:
                        with engine.connect() as conn:
                            prog = get_progress(conn, batch_id)
                        prog_str = f"{prog['done']}/{prog['total']} ({prog['pct']}%)"
                    except Exception:
                        prog_str = "?"
                    logger.info(
                        f"[C-pd-pool] {prog_str} — MAJ_CAT={mc} "
                        f"ship={r.get('ship', 0):.0f} hold={r.get('hold', 0):.0f} "
                        f"rows={r.get('rows', 0)} in {r.get('dur', 0):.1f}s "
                        f"(wb={r.get('wb_secs', 0):.1f}s)"
                    )

        # All workers finished. If we were using the writer thread, signal
        # it to drain remaining items and shut down, then merge its stats
        # back into the run summary.
        if writer_thread is not None and writer_queue is not None:
            logger.info(
                f"[C-pd] all workers done; waiting for writer thread to drain"
            )
            writer_queue.put(_WRITER_SENTINEL)
            writer_thread.join(timeout=600)  # 10 min safety cap
            if writer_thread.is_alive():
                logger.error(
                    f"[C-pd] writer thread did not finish in 10min — "
                    f"backend should be restarted; some MAJ_CATs may remain "
                    f"IN_PROGRESS in the queue"
                )
            else:
                logger.info(f"[C-pd] writer thread drained cleanly")
            # Merge writer stats back into the run aggregates
            wb_total_secs += float(writer_stats.get("wb_total_secs", 0.0))
            dl_caught     += int(writer_stats.get("dl_caught", 0))
            dl_succeeded  += int(writer_stats.get("dl_succeeded", 0))
            dl_exhausted  += int(writer_stats.get("dl_exhausted", 0))
            for err in writer_stats.get("errors", []):
                result["errors"].append(err)
    else:
        # Inline fallback: tiny inputs (or n_workers=1) skip subprocess overhead.
        logger.info(
            f"[C-pd] inline run for {len(pool_args)} MAJ_CAT(s) "
            f"(below process-pool threshold or single worker)"
        )
        for args in pool_args:
            mc = args[0]
            r = _pandas_run_one_majcat(args)
            dl = r.get("deadlocks") or {}
            dl_caught    += int(dl.get("caught", 0))
            dl_succeeded += int(dl.get("succeeded", 0))
            dl_exhausted += int(dl.get("exhausted", 0))
            if r.get("error"):
                result["errors"].append({"maj_cat": mc, "error": r["error"]})
            else:
                wb_total_secs += float(r.get("wb_secs", 0.0))

    logger.info(
        f"[C-pd] live write-back done — total wb time across workers "
        f"{wb_total_secs:.1f}s"
    )
    # One-line summary so the operator can tell at a glance whether the
    # retry path absorbed deadlocks silently or whether real failures slipped
    # through. `caught - succeeded - exhausted` should be 0 (every caught
    # event ends one way or the other).
    logger.info(
        f"[C-pd] deadlock retries: caught={dl_caught} "
        f"succeeded={dl_succeeded} exhausted={dl_exhausted}"
    )
    result["deadlock_retries"] = {
        "caught":    dl_caught,
        "succeeded": dl_succeeded,
        "exhausted": dl_exhausted,
    }

    # Retry/failure detail log — operators can see per-MAJ_CAT attempt counts.
    try:
        with engine.connect() as conn:
            q_prog = get_progress(conn, batch_id)
            failed_list = get_failed_list(conn, batch_id)
        logger.info(
            f"[C-pd] batch={batch_id} COMPLETE — "
            f"total={q_prog['total']} done={q_prog['done']} "
            f"in_progress={q_prog['in_progress']} failed={q_prog['failed']} "
            f"pct={q_prog['pct']}%"
        )
        if failed_list:
            total_attempts = sum(f['attempts'] for f in failed_list)
            logger.warning(
                f"[C-pd] {len(failed_list)} MAJ_CAT(s) still FAILED "
                f"after {total_attempts} total attempt(s):"
            )
            for f in failed_list:
                logger.warning(
                    f"  FAILED maj_cat={f['maj_cat']} attempts={f['attempts']} "
                    f"error={str(f['error'])[:200]}"
                )
        else:
            logger.info(f"[C-pd] All MAJ_CATs completed successfully (0 failures)")
    except Exception as log_err:
        logger.warning(f"[C-pd] post-run progress log failed: {log_err}")

    # ── Finalise + Stage D (SQL) ──
    with engine.connect() as conn:
        # MJ_REQ cap: prevent SUM(SHIP_QTY across all OPTs) > MJ_REQ per store
        rne._stage_c_apply_mj_req_cap(conn, alloc_table, working_table)
        # Safety-net: skipped rows must never carry warehouse hold.
        run_sql(conn, f"""
            UPDATE [{alloc_table}] SET HOLD_QTY = 0, ROUND_HOLD = 0
            WHERE ISNULL(HOLD_QTY, 0) > 0
              AND ALLOC_STATUS = 'SKIPPED'
        """)
        # Rows with SHIP=0 AND HOLD=0 consumed pool during the waterfall but were
        # cancelled afterwards (MJ_REQ_CAP or other post-waterfall zeroing).
        # Reset POOL_CONSUMED so the pool is not wrongly held against FNL_Q_REM.
        run_sql(conn, f"""
            UPDATE [{alloc_table}] SET POOL_CONSUMED = 0
            WHERE ISNULL(SHIP_QTY,     0) = 0
              AND ISNULL(HOLD_QTY,     0) = 0
              AND ISNULL(POOL_CONSUMED, 0) > 0
        """)
        # Recompute FNL_Q_REM per pool key: FNL_Q minus only real (non-zero) consumption.
        run_sql(conn, f"""
            UPDATE A SET A.FNL_Q_REM = ISNULL(A.FNL_Q, 0) - ISNULL(B.consumed, 0)
            FROM [{alloc_table}] A
            LEFT JOIN (
                SELECT [RDC], [MAJ_CAT], [GEN_ART_NUMBER],
                       ISNULL([CLR],'') AS CLR, [VAR_ART], [SZ],
                       SUM(ISNULL([POOL_CONSUMED], 0)) AS consumed
                FROM   [{alloc_table}]
                GROUP  BY [RDC], [MAJ_CAT], [GEN_ART_NUMBER],
                          ISNULL([CLR],''), [VAR_ART], [SZ]
            ) B ON  A.[RDC]            = B.[RDC]
                AND A.[MAJ_CAT]        = B.[MAJ_CAT]
                AND A.[GEN_ART_NUMBER] = B.[GEN_ART_NUMBER]
                AND ISNULL(A.[CLR],'') = B.[CLR]
                AND A.[VAR_ART]        = B.[VAR_ART]
                AND A.[SZ]             = B.[SZ]
        """)
        run_sql(conn, f"UPDATE [{alloc_table}] SET ALLOC_QTY = SHIP_QTY")
        run_sql(conn, f"""
            UPDATE [{alloc_table}] SET
                ALLOC_STATUS = CASE
                    WHEN SHIP_QTY + HOLD_QTY > 0
                         AND SHIP_QTY + HOLD_QTY >= CASE
                             -- TBL: hold buffer counted once (applies to all TBL)
                             WHEN OPT_TYPE='TBL'
                             THEN CASE WHEN ISNULL(SZ_MBQ_WH,0)
                                            + (ISNULL(I_ROD,1)-1)*ISNULL(SZ_MBQ,0)
                                            - ISNULL(SZ_STK,0) > 0
                                       THEN ISNULL(SZ_MBQ_WH,0)
                                            + (ISNULL(I_ROD,1)-1)*ISNULL(SZ_MBQ,0)
                                            - ISNULL(SZ_STK,0)
                                       ELSE 0 END
                             -- RL/TBC: I_ROD * SZ_MBQ (no hold buffer)
                             ELSE CASE WHEN ISNULL(I_ROD,1)*ISNULL(SZ_MBQ,0)
                                            - ISNULL(SZ_STK,0) > 0
                                       THEN ISNULL(I_ROD,1)*ISNULL(SZ_MBQ,0)
                                            - ISNULL(SZ_STK,0)
                                       ELSE 0 END
                             END
                         THEN 'ALLOCATED'
                    WHEN SHIP_QTY + HOLD_QTY > 0      THEN 'PARTIAL'
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
    logger.info(
        f"[C-pd] DONE batch={batch_id} listed={result['listed_opts']} "
        f"alloc_rows={result['alloc_rows']} ship={result['ship_qty_total']:.0f} "
        f"hold={result['hold_qty_total']:.0f} "
        f"done={result.get('done',0)} failed={result.get('failed',0)} "
        f"in {result['duration_sec']}s"
    )
    return result


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def _load_tables(engine, alloc_table, working_table, grids, only_majcats):
    """Read alloc + working tables into DataFrames. Coerce types up-front."""
    where_a = ""
    where_w = "WHERE LISTED_FLAG = 1"
    params: Dict[str, str] = {}
    if only_majcats:
        keys = ", ".join(f":mc_{i}" for i in range(len(only_majcats)))
        where_a = f"WHERE MAJ_CAT IN ({keys})"
        where_w += f" AND MAJ_CAT IN ({keys})"
        for i, mc in enumerate(only_majcats):
            params[f"mc_{i}"] = mc

    with engine.connect() as conn:
        working_cols = _select_working_cols(conn, working_table, grids)
        col_sql = ", ".join(f"[{c}]" for c in working_cols)
        # Deterministic ORDER BY — without this, SQL Server can return rows in
        # any order (especially under parallel scan), which makes mergesort's
        # tie-break in _run_band non-deterministic and gives slightly different
        # alloc/hold totals from run to run on the same input.
        alloc_order = (
            "ORDER BY [MAJ_CAT], [RDC], [GEN_ART_NUMBER], "
            "ISNULL([CLR],''), [VAR_ART], [SZ], "
            "[OPT_PRIORITY_RANK], ISNULL([ST_RANK], 999999), [WERKS]"
        )
        working_order = (
            "ORDER BY [MAJ_CAT], [WERKS], [GEN_ART_NUMBER], ISNULL([CLR],'')"
        )
        alloc_df = pd.read_sql(
            text(f"SELECT * FROM [{alloc_table}] {where_a} {alloc_order}"),
            conn, params=params,
        )
        working_df = pd.read_sql(
            text(f"SELECT {col_sql} FROM [{working_table}] {where_w} {working_order}"),
            conn, params=params,
        )

    # ── alloc_df type coercion ──
    num_cols = [
        "OPT_PRIORITY_RANK", "ST_RANK", "IS_NEW", "I_ROD",
        "SZ_MBQ", "SZ_MBQ_WH", "SZ_STK", "FNL_Q",
        "POOL_CONSUMED", "SHIP_QTY", "HOLD_QTY",
        "ROUND_SHIP", "ROUND_HOLD", "ALLOC_ROUND", "ALLOC_QTY",
    ]
    for c in num_cols:
        if c in alloc_df.columns:
            alloc_df[c] = pd.to_numeric(alloc_df[c], errors='coerce').fillna(0).astype('float64')
    for c in ['POOL_CONSUMED', 'SHIP_QTY', 'HOLD_QTY', 'ROUND_SHIP', 'ROUND_HOLD',
              'ALLOC_QTY', 'FROM_HOLD_QTY']:
        if c not in alloc_df.columns:
            alloc_df[c] = 0.0

    # Always reset to PENDING so _run_band processes every row from scratch.
    # The finalise step from a previous run may have written SKIPPED/ALLOCATED
    # back to the DB; carrying those stale statuses causes zero allocation.
    # INELIGIBLE rows are intentionally excluded upstream and must stay excluded.
    if 'ALLOC_STATUS' not in alloc_df.columns:
        alloc_df['ALLOC_STATUS'] = 'PENDING'
    else:
        alloc_df['ALLOC_STATUS'] = alloc_df['ALLOC_STATUS'].fillna('').astype(str)
        _non_inelig = alloc_df['ALLOC_STATUS'] != 'INELIGIBLE'
        alloc_df.loc[_non_inelig, 'ALLOC_STATUS'] = 'PENDING'
    if 'SKIP_REASON' not in alloc_df.columns:
        alloc_df['SKIP_REASON'] = ''
    else:
        alloc_df['SKIP_REASON'] = alloc_df['SKIP_REASON'].fillna('').astype(str)
        alloc_df.loc[alloc_df['ALLOC_STATUS'] == 'PENDING', 'SKIP_REASON'] = ''
    # Zero out accumulators so each run starts clean.
    _non_inelig = alloc_df['ALLOC_STATUS'] != 'INELIGIBLE'
    for _c in ['POOL_CONSUMED', 'SHIP_QTY', 'HOLD_QTY', 'ROUND_SHIP', 'ROUND_HOLD',
               'ALLOC_QTY', 'FROM_HOLD_QTY', 'ALLOC_ROUND']:
        if _c in alloc_df.columns:
            alloc_df.loc[_non_inelig, _c] = 0.0
    if 'ALLOC_WAVE' not in alloc_df.columns:
        alloc_df['ALLOC_WAVE'] = ''
    else:
        alloc_df.loc[_non_inelig, 'ALLOC_WAVE'] = ''
    if 'OPT_TYPE' in alloc_df.columns:
        alloc_df['OPT_TYPE'] = alloc_df['OPT_TYPE'].fillna('').astype(str)

    # Pool key strings — pad nulls to '' for stable hashing.
    for c in POOL_KEYS:
        if c in alloc_df.columns:
            alloc_df[c] = alloc_df[c].fillna('').astype(str)

    # ── working_df type coercion ──
    if 'MSA_FNL_Q_REM' in working_df.columns:
        working_df['MSA_FNL_Q_REM'] = pd.to_numeric(
            working_df['MSA_FNL_Q_REM'], errors='coerce'
        ).fillna(0).astype('float64')
    if 'PRI_CT_REM' in working_df.columns:
        working_df['PRI_CT_REM'] = pd.to_numeric(
            working_df['PRI_CT_REM'], errors='coerce'
        ).fillna(100.0).astype('float64')  # NULL = uninitialized → assume eligible (100%)
    if 'ACS_D' in working_df.columns:
        working_df['ACS_D'] = pd.to_numeric(
            working_df['ACS_D'], errors='coerce'
        ).fillna(0).astype('float64')
    for _mj_col in ('MJ_MBQ', 'MJ_STK_TTL', 'MJ_REQ'):
        if _mj_col in working_df.columns:
            working_df[_mj_col] = pd.to_numeric(
                working_df[_mj_col], errors='coerce'
            ).fillna(0).astype('float64')
    # MJ_REQ_REM: NULL means never initialized — use MJ_REQ as the baseline
    # (do NOT fill with 0, that would make store-broken fire for every row)
    if 'MJ_REQ_REM' in working_df.columns:
        _mj_rem_null = working_df['MJ_REQ_REM'].isna() | (
            pd.to_numeric(working_df['MJ_REQ_REM'], errors='coerce').isna()
        )
        working_df['MJ_REQ_REM'] = pd.to_numeric(
            working_df['MJ_REQ_REM'], errors='coerce'
        ).fillna(0).astype('float64')
        if _mj_rem_null.any() and 'MJ_REQ' in working_df.columns:
            _fallback = pd.to_numeric(working_df['MJ_REQ'], errors='coerce').fillna(0)
            working_df.loc[_mj_rem_null, 'MJ_REQ_REM'] = _fallback[_mj_rem_null].values
    for meta in grids.values():
        for col in (meta['req_rem'], meta['h_rem'], meta['gh_col']):
            if col in working_df.columns:
                working_df[col] = pd.to_numeric(
                    working_df[col], errors='coerce'
                ).fillna(0).astype('float64')
    for c in OPT_KEYS:
        if c in working_df.columns:
            working_df[c] = working_df[c].fillna('').astype(str)
    # Grid extras (RNG_SEG, MACRO_MVGR, FAB, …) are used as dict-key components
    # for REQ_REM decrements. Normalize to string so str/numeric mismatches
    # don't cause silent lookup misses.
    for meta in grids.values():
        for ex in meta.get('extras', []):
            if ex in working_df.columns:
                working_df[ex] = working_df[ex].fillna('').astype(str)
    if 'ALLOC_STATUS' in working_df.columns:
        working_df['ALLOC_STATUS'] = (
            working_df['ALLOC_STATUS'].fillna('PENDING').astype(str)
        )
    else:
        working_df['ALLOC_STATUS'] = 'PENDING'
    if 'ALLOC_REMARKS' in working_df.columns:
        working_df['ALLOC_REMARKS'] = (
            working_df['ALLOC_REMARKS'].fillna('').astype(str)
        )
    else:
        working_df['ALLOC_REMARKS'] = ''
    if 'OPT_TYPE' in working_df.columns:
        working_df['OPT_TYPE'] = working_df['OPT_TYPE'].fillna('').astype(str)

    return alloc_df, working_df, working_cols


def _select_working_cols(conn, working_table, grids) -> List[str]:
    """Return the working_table columns we need to load — base + per-grid."""
    base = [
        'WERKS', 'MAJ_CAT', 'GEN_ART_NUMBER', 'CLR',
        'OPT_TYPE', 'OPT_PRIORITY_RANK', 'LISTED_FLAG',
        'ALLOC_STATUS', 'ALLOC_REMARKS', 'ACS_D',
        'MSA_FNL_Q_REM', 'PRI_CT_REM',
        # MAJ_CAT-level store aggregates — used by the MBQ cap in _run_band
        'MJ_MBQ', 'MJ_STK_TTL', 'MJ_REQ', 'MJ_REQ_REM',
    ]
    existing = {c.upper() for c in rne._cols(conn, working_table)}
    cols = [c for c in base if c.upper() in existing]
    for meta in grids.values():
        for col in (meta['req_rem'], meta['h_rem'], meta['gh_col']):
            if col.upper() in existing and col not in cols:
                cols.append(col)
        for ex in meta.get('extras', []):
            if ex.upper() in existing and ex not in cols:
                cols.append(ex)
    return cols


# ---------------------------------------------------------------------------
# Per-MAJ_CAT waterfall (pandas)
# ---------------------------------------------------------------------------
def _build_mbq_budget(working_df: pd.DataFrame, cap_pct: float) -> Dict[str, float]:
    """Per-WERKS allocation budget: max(0, cap_pct/100 * MJ_MBQ - MJ_STK_TTL).
    MJ_REQ = MJ_MBQ * factor then stock (including excess) is deducted.
    Returns empty dict (disables cap) when the required columns are absent."""
    if 'MJ_MBQ' not in working_df.columns or 'MJ_STK_TTL' not in working_df.columns:
        return {}
    # Deterministic: sort by WERKS first so drop_duplicates always keeps the
    # same row across runs even if upstream input order varies.
    store_data = (
        working_df[['WERKS', 'MJ_MBQ', 'MJ_STK_TTL']]
        .sort_values(['WERKS', 'MJ_MBQ', 'MJ_STK_TTL'], kind='mergesort')
        .drop_duplicates(subset=['WERKS'])
    )
    budget: Dict[str, float] = {}
    factor = cap_pct / 100.0
    for _, row in store_data.iterrows():
        cap = float(row['MJ_MBQ'] or 0) * factor - float(row['MJ_STK_TTL'] or 0)
        budget[str(row['WERKS'])] = max(0.0, cap)
    return budget


def _snapshot_fnl_q_rem(
    alloc_df: pd.DataFrame,
    pool_dict: Dict[Tuple, float],
    mask: Optional[pd.Series] = None,
) -> None:
    """Write current pool_dict values into alloc_df['FNL_Q_REM'].
    Called before each band so the column captures the pool state
    *before* that band's allocation runs — useful for debugging skips."""
    if 'FNL_Q_REM' not in alloc_df.columns:
        return
    if mask is not None:
        idx = alloc_df.index[mask]
        keys = pd.Series(
            list(zip(*[alloc_df.loc[idx, c].to_numpy() for c in POOL_KEYS])),
            index=idx,
        )
        alloc_df.loc[idx, 'FNL_Q_REM'] = (
            keys.map(pool_dict).fillna(0).astype('float64')
        )
    else:
        keys = pd.Series(
            list(zip(*[alloc_df[c].to_numpy() for c in POOL_KEYS])),
            index=alloc_df.index,
        )
        alloc_df['FNL_Q_REM'] = keys.map(pool_dict).fillna(0).astype('float64')


def _run_majcat_waterfall(
    alloc_df: pd.DataFrame,
    working_df: pd.DataFrame,
    grids: Dict[str, Dict],
    pri_ct_check_rl: bool = False,
    pri_ct_check_tbc: bool = False,
    rl_mbq_cap_pct: float = 0.0,
    tbc_mbq_cap_pct: float = 0.0,
    hold_dict: Optional[Dict[Tuple, float]] = None,
    size_threshold: float = 0.6,
    min_size_count: int = 3,
    opt_types: Optional[List[str]] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Run RL → TBC → TBL waterfall in pandas for one MAJ_CAT slice.
    `alloc_df` and `working_df` are pre-sliced to one MAJ_CAT and copies —
    safe to mutate in place. Returns the same frames after mutation.
    hold_dict: (WERKS, VAR_ART) -> HOLD_REM from ARS_NL_TBL_HOLD_TRACKING.
    Mutated in place as hold is consumed (safe — each MAJ_CAT has its own copy).
    """
    # Build the per-MAJ_CAT pool dict: max(FNL_Q) per pool key.
    if alloc_df.empty:
        return alloc_df, working_df
    pool_grp = (
        alloc_df.groupby(POOL_KEYS, sort=False, observed=True)['FNL_Q']
        .max()
        .fillna(0)
        .astype('float64')
    )
    pool_dict: Dict[Tuple, float] = pool_grp.to_dict()

    has_working = (working_df is not None) and (not working_df.empty)
    revalidate_enabled = bool(rne.ENABLE_PER_OPT_REVALIDATION) and has_working

    # Build per-WERKS MBQ budget dicts for capped OPT_TYPEs.
    # Cap is only active when the corresponding PRI gate is OFF (unchecked).
    # Budget = max(0, cap_pct/100 × MJ_MBQ − MJ_STK_TTL) per WERKS.
    _rl_budget: Dict[str, float] = (
        _build_mbq_budget(working_df, rl_mbq_cap_pct)
        if (not pri_ct_check_rl) and rl_mbq_cap_pct > 0 and has_working
        else {}
    )
    _tbc_budget: Dict[str, float] = (
        _build_mbq_budget(working_df, tbc_mbq_cap_pct)
        if (not pri_ct_check_tbc) and tbc_mbq_cap_pct > 0 and has_working
        else {}
    )

    # Initial snapshot: all rows get the pre-waterfall pool value so that
    # SKIPPED rows (which never enter elig_mask) always show a meaningful
    # FNL_Q_REM rather than NULL.
    _snapshot_fnl_q_rem(alloc_df, pool_dict)

    active_ot = [ot for ot in OPT_TYPE_ORDER if not opt_types or ot in opt_types]
    for i, ot in enumerate(active_ot):
        ot_mask = (alloc_df['OPT_TYPE'] == ot)
        if not ot_mask.any():
            continue
        max_round = int(alloc_df.loc[ot_mask, 'I_ROD'].max() or 0)
        if max_round == 0:
            continue

        mbq_budget = _rl_budget if ot == 'RL' else (_tbc_budget if ot == 'TBC' else {})

        # Skip low-coverage OPTs and re-rank survivors using live SIZE_RATIO.
        # Only for TBC and TBL — RL uses the initial rank set at the start.
        if i > 0:
            _rerank_opt_priority_pandas(
                alloc_df, pool_dict, ot,
                size_threshold=size_threshold,
                min_size_count=min_size_count,
            )

        # Pre-band: actively evaluate PRI_CT_REM and MJ_REQ_REM from current
        # working_df values, mark failing OPTs SKIPPED, then propagate to
        # alloc_df — before the first round of this OPT_TYPE runs.
        if revalidate_enabled and has_working:
            _pre_band_check(alloc_df, working_df, ot,
                            pri_ct_check_rl, pri_ct_check_tbc)

        for r in range(1, max_round + 1):
            # Reset round deltas across the whole opt_type once per round.
            alloc_df.loc[ot_mask, 'ROUND_SHIP'] = 0.0
            alloc_df.loc[ot_mask, 'ROUND_HOLD'] = 0.0

            elig_mask = ot_mask & (alloc_df['I_ROD'] >= r)
            if not elig_mask.any():
                continue

            # Snapshot FNL_Q_REM *before* this band runs so the column reflects
            # the pool state at the moment each row's allocation is attempted.
            # This lets users see exactly what pool was available and why skips
            # fired, rather than the post-waterfall depleted value.
            _snapshot_fnl_q_rem(alloc_df, pool_dict, mask=elig_mask)

            # All stores compete in one vectorised band call.  Priority order
            # is enforced by the sort inside _run_band:
            #   POOL_KEYS → OPT_PRIORITY_RANK ASC → ST_RANK ASC → WERKS
            # Within each pool key, the highest-priority OPT takes pool first;
            # ties in OPT_PRIORITY_RANK are broken by ST_RANK (best store wins).
            # Round N finishes for ALL stores before round N+1 starts.
            # Cross-type eligibility (R06: MJ_REQ_REM < 0.5×ACS_D) is evaluated
            # by _pre_band_check before the first round of each OPT_TYPE.
            _run_band(alloc_df, pool_dict, ot, int(r),
                      mbq_budget=mbq_budget,
                      hold_dict=hold_dict,
                      size_threshold=size_threshold,
                      min_size_count=min_size_count)

            if revalidate_enabled:
                _revalidate_after_band(
                    alloc_df, working_df, grids, ot, int(r),
                    pri_ct_check_rl=pri_ct_check_rl,
                    pri_ct_check_tbc=pri_ct_check_tbc,
                )

            logger.info(
                f"[C-pd] {ot} round={r}/{max_round} — "
                f"ship={int(alloc_df.loc[ot_mask,'SHIP_QTY'].sum())} "
                f"hold={int(alloc_df.loc[ot_mask,'HOLD_QTY'].sum())}"
            )

    return alloc_df, working_df


def _rerank_opt_priority_pandas(
    alloc_df: pd.DataFrame,
    pool_dict: Dict[Tuple, float],
    target_ot: str,
    size_threshold: float = 0.6,
    min_size_count: int = 3,
) -> None:
    """Skip low-coverage OPTs then re-rank survivors using live SIZE_RATIO.

    Step 1 — SKIP: for each (WERKS, GEN_ART_NUMBER, CLR) in target_ot,
    compute live SIZE_RATIO from pool_dict.  If SIZE_RATIO < size_threshold
    AND live_VAR_FNL_COUNT < min_size_count → mark all their rows SKIPPED
    with SKIP_REASON 'R07_SIZE_RATIO_LIVE'.

    Step 2 — RERANK: surviving (ALLOC_STATUS is NaN) rows get fresh
    OPT_PRIORITY_RANK.  ORDER BY same as _stage_a_assign_rank but using
    live SIZE_RATIO:  TIER ASC → SIZE_RATIO DESC → SEC_CT% DESC
                      → MAX_DAILY_SALE DESC → OPT_REQ_WH DESC

    Mutates alloc_df in-place for target_ot rows only.
    """
    mask = (alloc_df['OPT_TYPE'] == target_ot) & alloc_df['ALLOC_STATUS'].isna()
    if not mask.any():
        return

    sub = alloc_df.loc[mask].copy()

    # Build live pool availability per size row.
    pool_keys_arr = list(zip(
        sub['RDC'].astype(str),
        sub['MAJ_CAT'].astype(str),
        sub['GEN_ART_NUMBER'].astype(str),
        sub['CLR'].astype(str),
        sub['VAR_ART'].astype(str),
        sub['SZ'].astype(str),
    ))
    sub['_has_pool'] = pd.array(
        [1 if pool_dict.get(k, 0.0) > 0.0 else 0 for k in pool_keys_arr],
        dtype='int8',
    )

    # Aggregate to (GEN_ART_NUMBER, CLR, VAR_ART) to get live SIZE_RATIO.
    grp = sub.groupby(['GEN_ART_NUMBER', 'CLR', 'VAR_ART'], sort=False).agg(
        _var_count=('SZ', 'count'),
        _var_fnl=('_has_pool', 'sum'),
    ).reset_index()
    grp['SIZE_RATIO'] = np.where(
        grp['_var_count'] > 0,
        grp['_var_fnl'].astype(float) / grp['_var_count'],
        0.0,
    )

    sub = sub.merge(
        grp[['GEN_ART_NUMBER', 'CLR', 'VAR_ART', 'SIZE_RATIO', '_var_fnl']],
        on=['GEN_ART_NUMBER', 'CLR', 'VAR_ART'],
        how='left',
    )
    sub['SIZE_RATIO'] = sub['SIZE_RATIO'].fillna(0.0)
    sub['_var_fnl']   = sub['_var_fnl'].fillna(0)

    # ------------------------------------------------------------------ #
    # Step 1: mark low-coverage OPTs as SKIPPED in the original alloc_df. #
    # ------------------------------------------------------------------ #
    low_cov = (sub['SIZE_RATIO'] < size_threshold) & (sub['_var_fnl'] < min_size_count)
    if low_cov.any():
        skip_idx = sub.index[low_cov]
        alloc_df.loc[skip_idx, 'ALLOC_STATUS'] = 'SKIPPED'
        alloc_df.loc[skip_idx, 'ALLOC_REMARKS'] = (
            alloc_df.loc[skip_idx, 'ALLOC_REMARKS'].fillna('') + ' R07_SIZE_RATIO_LIVE;'
        )
        logger.debug(
            f"[C-pd] {target_ot}: skipped {low_cov.sum()} rows "
            f"with live SIZE_RATIO < {size_threshold} and fnl_count < {min_size_count}"
        )
        # Refresh mask — survivors only.
        mask = (alloc_df['OPT_TYPE'] == target_ot) & alloc_df['ALLOC_STATUS'].isna()
        if not mask.any():
            return
        sub = sub.loc[~low_cov].copy()

    # ------------------------------------------------------------------ #
    # Step 2: re-rank survivors.                                          #
    # ------------------------------------------------------------------ #
    for col in ['OPT_PRIORITY_TIER', 'SEC_CT%', 'MAX_DAILY_SALE', 'OPT_REQ_WH']:
        if col in sub.columns:
            sub[col] = pd.to_numeric(sub[col], errors='coerce').fillna(0.0)

    opt_cols = ['WERKS', 'GEN_ART_NUMBER', 'CLR', 'OPT_PRIORITY_TIER',
                'SIZE_RATIO', 'SEC_CT%', 'MAX_DAILY_SALE', 'OPT_REQ_WH']
    # Deterministic: pre-sort by the OPT identity columns BEFORE drop_duplicates,
    # so that "first row of each (WERKS, GEN_ART, CLR)" is reproducible regardless
    # of upstream input order.
    opt_level = (
        sub[opt_cols]
        .sort_values(['WERKS', 'GEN_ART_NUMBER', 'CLR'], kind='mergesort')
        .drop_duplicates(subset=['WERKS', 'GEN_ART_NUMBER', 'CLR'])
        .copy()
    )

    # Final sort for rank assignment. The trailing tie-breakers
    # (GEN_ART_NUMBER, CLR) guarantee no two rows share a complete sort key,
    # so cumcount produces stable ranks across runs.
    opt_level = opt_level.sort_values(
        by=['WERKS', 'OPT_PRIORITY_TIER', 'SIZE_RATIO', 'SEC_CT%',
            'MAX_DAILY_SALE', 'OPT_REQ_WH', 'GEN_ART_NUMBER', 'CLR'],
        ascending=[True, True, False, False, False, False, True, True],
        kind='mergesort',
    )
    opt_level['_new_rank'] = opt_level.groupby('WERKS', sort=False).cumcount() + 1

    rank_map = dict(zip(
        zip(opt_level['WERKS'], opt_level['GEN_ART_NUMBER'], opt_level['CLR']),
        opt_level['_new_rank'],
    ))

    new_ranks = alloc_df.loc[mask, ['WERKS', 'GEN_ART_NUMBER', 'CLR']].apply(
        lambda row: rank_map.get((row['WERKS'], row['GEN_ART_NUMBER'], row['CLR'])),
        axis=1,
    )
    alloc_df.loc[mask, 'OPT_PRIORITY_RANK'] = new_ranks
    logger.debug(
        f"[C-pd] {target_ot}: live SIZE_RATIO skip+rerank done "
        f"(thr={size_threshold}, min_sz={min_size_count})"
    )


def _run_band(
    alloc_df: pd.DataFrame,
    pool_dict: Dict[Tuple, float],
    ot: str,
    r: int,
    mbq_budget: Optional[Dict[str, float]] = None,
    hold_dict: Optional[Dict[Tuple, float]] = None,
    size_threshold: float = 0.6,
    min_size_count: int = 3,
) -> None:
    """One round × one opt_type — all stores compete simultaneously.

    Sort order inside each pool key: OPT_PRIORITY_RANK ASC → ST_RANK ASC → WERKS.
    The cumulative-window pool-take drains the pool in this order, so:
      - OPT_PRIORITY_RANK=1 OPT wins over rank=2 within the same pool key
      - Ties in OPT_PRIORITY_RANK are broken by ST_RANK (best store first)
    Processing sequence: RL (all rounds) → TBC (all rounds) → TBL (all rounds).
    Cross-type store eligibility checked via _pre_band_check before each type.

    mbq_budget: per-WERKS cap (active when PRI gate is OFF and cap_pct > 0).
    hold_dict: keyed by (WERKS, VAR_ART, SZ) — matches ARS_NL_TBL_HOLD_TRACKING PK.
               RL/TBC: draw from hold_rem first; only shortfall pulls from pool.
               TBL: draws from pool; HOLD_QTY recorded only when fully ALLOCATED."""
    # 1) Eligible rows
    mask = (
        (alloc_df['OPT_TYPE'] == ot)
        & (alloc_df['I_ROD'] >= r)
        & (~alloc_df['ALLOC_STATUS'].isin(['SKIPPED', 'INELIGIBLE']))
    )
    if not mask.any():
        return

    # Work on a copy that preserves the original index for write-back.
    sub = alloc_df.loc[mask, [
        *POOL_KEYS, 'WERKS', 'OPT_PRIORITY_RANK', 'ST_RANK', 'IS_NEW',
        'SZ_MBQ', 'SZ_MBQ_WH', 'SZ_STK',
        'POOL_CONSUMED', 'SHIP_QTY',
    ]].copy()

    sz_mbq_wh = sub['SZ_MBQ_WH'].to_numpy()
    sz_mbq    = sub['SZ_MBQ'].to_numpy()
    sz_stk    = sub['SZ_STK'].to_numpy()
    pool_cons = sub['POOL_CONSUMED'].to_numpy()
    ship_qty  = sub['SHIP_QTY'].to_numpy()

    need_ship = np.maximum(r * sz_mbq - sz_stk - ship_qty, 0.0)

    if ot == 'TBL':
        # TBL: warehouse hold buffer counted once (SZ_MBQ_WH), then rolling SZ_MBQ.
        # Suppress pool when no shipping demand to avoid pure-HOLD pool consumption.
        tbl_cum = sz_mbq_wh + (r - 1) * sz_mbq
        need_pool = np.maximum(tbl_cum - sz_stk - pool_cons, 0.0)
        need_pool = np.where(need_ship == 0, 0.0, need_pool)
    else:
        # RL/TBC: pool demand = net shipping need (hold draw handled in step 1b).
        need_pool = np.maximum(r * sz_mbq - sz_stk - pool_cons, 0.0)

    sub['need_pool'] = need_pool
    sub['need_ship'] = need_ship

    # 1b) RL/TBC: consume warehouse hold (hold_rem) first; only shortfall from pool.
    # hold_dict keyed (WERKS, VAR_ART, SZ) — sized grain matching table PK.
    # TBL does NOT draw from hold here; new TBL hold is created by Part 8.6 Step B.
    sub['FROM_HOLD_QTY'] = 0.0
    if ot in ('RL', 'TBC') and hold_dict:
        hold_keys_3 = list(zip(
            sub['WERKS'].tolist(), sub['VAR_ART'].tolist(), sub['SZ'].tolist()
        ))
        hold_avail = np.array(
            [hold_dict.get((str(w), str(v), str(s)), 0.0)
             for w, v, s in hold_keys_3],
            dtype='float64',
        )
        from_hold = np.minimum(sub['need_pool'].to_numpy(), hold_avail)
        sub['FROM_HOLD_QTY'] = from_hold
        sub['need_pool'] = np.maximum(sub['need_pool'].to_numpy() - from_hold, 0.0)
        need_pool = sub['need_pool'].to_numpy()

    # Early write-back for hold draws BEFORE the pool filter.
    # Rows fully covered by hold have need_pool=0 and won't reach step 7.
    hold_rows = sub[sub['FROM_HOLD_QTY'] > 0]
    if not hold_rows.empty:
        h_idx  = hold_rows.index
        h_take = hold_rows['FROM_HOLD_QTY'].to_numpy()
        new_pc_h = alloc_df.loc[h_idx, 'POOL_CONSUMED'].to_numpy() + h_take
        alloc_df.loc[h_idx, 'POOL_CONSUMED']   = new_pc_h
        alloc_df.loc[h_idx, 'SHIP_QTY']        = alloc_df.loc[h_idx, 'SHIP_QTY'].to_numpy() + h_take
        alloc_df.loc[h_idx, 'FROM_HOLD_QTY']   = alloc_df.loc[h_idx, 'FROM_HOLD_QTY'].to_numpy() + h_take
        alloc_df.loc[h_idx, 'ALLOC_WAVE']       = f"{ot}_R{r}"
        alloc_df.loc[h_idx, 'ALLOC_ROUND']      = float(r)
        # Update ALLOC_STATUS for hold-only rows (they may not reach step 7)
        i_rod_h  = alloc_df.loc[h_idx, 'I_ROD'].to_numpy()
        smbq_h   = alloc_df.loc[h_idx, 'SZ_MBQ'].to_numpy()
        sstk_h   = alloc_df.loc[h_idx, 'SZ_STK'].to_numpy()
        target_h = np.maximum(i_rod_h * smbq_h - sstk_h, 0.0)
        alloc_df.loc[h_idx, 'ALLOC_STATUS'] = np.where(
            new_pc_h >= target_h, 'ALLOCATED', 'PARTIAL'
        )
        # Decrement hold_dict in-memory so later rounds see reduced hold_rem.
        for (w, va, sz_val), amt in (
            hold_rows.groupby(['WERKS', 'VAR_ART', 'SZ'])['FROM_HOLD_QTY'].sum().items()
        ):
            if amt > 0:
                key = (str(w), str(va), str(sz_val))
                if key in hold_dict:
                    hold_dict[key] = max(0.0, hold_dict[key] - float(amt))

    sub = sub[sub['need_pool'] > 0]
    if sub.empty:
        return

    # 2) Pool lookup — vectorized via Series.map on a MultiIndex.
    pool_keys_series = pd.Series(
        list(zip(*[sub[c].to_numpy() for c in POOL_KEYS])),
        index=sub.index,
    )
    fnl_q_rem = pool_keys_series.map(pool_dict).fillna(0).astype('float64')
    sub['FNL_Q_REM'] = fnl_q_rem.to_numpy()

    # TBL size-completeness gate — mirrors R07_VAR_RATIO_TBL from Stage A but
    # applied to the LIVE pool so that stores which arrive late (after other
    # stores have drained most sizes) don't get a partial-size allocation.
    # Skip an OPT for this store when: (sizes_with_pool < min_size_count)
    #   AND (sizes_with_pool / total_sizes_needed < size_threshold).
    # If EITHER condition is false the OPT passes (same "both must be true to
    # skip" semantics as R07).
    if ot == 'TBL' and (size_threshold > 0 or min_size_count > 0):
        _tbl_grp = ['WERKS', 'GEN_ART_NUMBER', 'CLR', 'VAR_ART']
        sub['_has_pool'] = (sub['FNL_Q_REM'] > 0).astype(float)
        _total = sub.groupby(_tbl_grp, observed=True, dropna=False)['SZ'].transform('count').astype(float)
        _avail = sub.groupby(_tbl_grp, observed=True, dropna=False)['_has_pool'].transform('sum').astype(float)
        _ratio = _avail / _total.where(_total > 0, other=np.inf)
        _too_few = (_avail < min_size_count) & (_ratio < size_threshold)
        sub = sub[~_too_few]
        if sub.empty:
            return

    sub = sub[sub['FNL_Q_REM'] > 0]
    if sub.empty:
        return

    # 3) Stable sort within pool key — OPT priority first, then store rank.
    # OPT_PRIORITY_RANK=1 OPT takes pool before rank=2 OPT; ties broken by ST_RANK.
    sub['_st_rank_fill'] = sub['ST_RANK'].fillna(999999).astype('float64')
    sort_cols = POOL_KEYS + ['OPT_PRIORITY_RANK', '_st_rank_fill', 'WERKS']
    sub.sort_values(sort_cols, kind='mergesort', inplace=True)

    # 4) Cumulative demand within pool key
    sub['cum_demand'] = (
        sub.groupby(POOL_KEYS, sort=False, observed=True)['need_pool'].cumsum()
    )
    cum_prev = (sub['cum_demand'] - sub['need_pool']).to_numpy()
    fnl      = sub['FNL_Q_REM'].to_numpy()
    np_need  = sub['need_pool'].to_numpy()

    # 5) take_pool = max(0, min(need_pool, FNL_Q_REM - cum_prev))
    remaining = np.maximum(fnl - cum_prev, 0.0)
    take_pool = np.minimum(remaining, np_need)
    sub['take_pool'] = take_pool

    sub = sub[sub['take_pool'] > 0]
    if sub.empty:
        return

    # 5a) Per-WERKS MBQ cap — only when mbq_budget is provided (PRI gate OFF).
    # Budget = max(0, cap_pct/100 × MJ_MBQ − MJ_STK_TTL) computed once per
    # waterfall run. Here we subtract whatever was already shipped for this
    # OPT_TYPE in previous rounds, then cap within-batch take in priority order.
    if mbq_budget:
        ot_shipped = (
            alloc_df.loc[alloc_df['OPT_TYPE'] == ot, ['WERKS', 'SHIP_QTY']]
            .groupby('WERKS', sort=False)['SHIP_QTY'].sum()
        )
        budg_ser = pd.Series(mbq_budget, dtype='float64')
        shipped_ser = ot_shipped.reindex(budg_ser.index).fillna(0.0)
        budget_before = (budg_ser - shipped_ser).clip(lower=0.0)

        sub['_budg_before'] = sub['WERKS'].map(budget_before.to_dict()).fillna(0.0)

        # Sort by (WERKS, OPT_PRIORITY_RANK) so highest-priority rows eat
        # the per-WERKS budget first.
        idx_orig = sub.index.copy()
        sub_s = sub.sort_values(['WERKS', 'OPT_PRIORITY_RANK', *POOL_KEYS], kind='mergesort')
        sub_s['_cum_w'] = sub_s.groupby('WERKS', sort=False)['take_pool'].cumsum()
        sub_s['_prev_w'] = sub_s['_cum_w'] - sub_s['take_pool']
        sub_s['_row_rem'] = np.maximum(
            sub_s['_budg_before'].to_numpy() - sub_s['_prev_w'].to_numpy(), 0.0
        )
        sub_s['take_pool'] = np.minimum(sub_s['take_pool'].to_numpy(), sub_s['_row_rem'].to_numpy())

        sub = sub_s.reindex(idx_orig)
        sub = sub[sub['take_pool'] > 0]
        if sub.empty:
            return

    # 6) SHIP / HOLD split (pool-take only; FROM_HOLD_QTY already written in step 1b)
    # TBL (IS_NEW=0 and IS_NEW=1): split pool take by need_ship; excess → HOLD.
    # RL/TBC: pool take 100% ships (hold draw was already shipped in step 1b).
    take   = sub['take_pool'].to_numpy()
    n_ship = sub['need_ship'].to_numpy()
    if ot == 'TBL':
        round_ship = np.minimum(take, n_ship)
        round_hold = np.maximum(take - n_ship, 0.0)
    else:
        round_ship = take
        round_hold = np.zeros_like(take)
    sub['ROUND_SHIP_NEW'] = round_ship
    sub['ROUND_HOLD_NEW'] = round_hold

    # 7) Write back pool results to alloc_df by preserved index.
    # POOL_CONSUMED += pool take (FROM_HOLD_QTY was already added in step 1b).
    idx = sub.index

    # Read per-row size params once for ALLOC_STATUS and TBL hold gate.
    i_rod   = alloc_df.loc[idx, 'I_ROD'].to_numpy()
    sstk    = alloc_df.loc[idx, 'SZ_STK'].to_numpy()
    smbq    = alloc_df.loc[idx, 'SZ_MBQ'].to_numpy()
    prev_pc = alloc_df.loc[idx, 'POOL_CONSUMED'].to_numpy()

    # ALLOCATED = store ship requirement (I_ROD × SZ_MBQ) is fully met.
    # Hold is separate: allowed whenever ship demand is covered, even partially.
    target = np.maximum(i_rod * smbq - sstk, 0.0)   # ship-only, same for all types
    if ot == 'TBL':
        # Allow hold only when this round's pool take covers the ship demand.
        # If pool was too small to fully ship, there is nothing left to hold.
        is_ship_met = take >= n_ship
        round_hold  = np.where(is_ship_met, round_hold, 0.0)
        pool_take   = round_ship + round_hold
    else:
        pool_take = take                   # RL/TBC: all pool take ships

    alloc_df.loc[idx, 'POOL_CONSUMED'] = prev_pc + pool_take
    alloc_df.loc[idx, 'ROUND_SHIP'] = round_ship
    alloc_df.loc[idx, 'ROUND_HOLD'] = round_hold
    alloc_df.loc[idx, 'SHIP_QTY']   = (
        alloc_df.loc[idx, 'SHIP_QTY'].to_numpy() + round_ship
    )
    alloc_df.loc[idx, 'HOLD_QTY']   = (
        alloc_df.loc[idx, 'HOLD_QTY'].to_numpy() + round_hold
    )
    alloc_df.loc[idx, 'ALLOC_WAVE']  = f"{ot}_R{r}"
    alloc_df.loc[idx, 'ALLOC_ROUND'] = float(r)

    # ALLOC_STATUS: compare cumulative SHIP_QTY (not ship+hold) against ship target.
    new_ship = alloc_df.loc[idx, 'SHIP_QTY'].to_numpy()
    alloc_df.loc[idx, 'ALLOC_STATUS'] = np.where(
        new_ship >= target, 'ALLOCATED', 'PARTIAL'
    )

    # 8) Decrement pool by pool_take only (FROM_HOLD_QTY does not consume RDC pool).
    # TBL PARTIAL rows have their cancelled hold returned to pool automatically
    # because pool_take = round_ship only (hold was zeroed above).
    sub['_taken'] = pool_take
    band_take = (
        sub.groupby(POOL_KEYS, sort=False, observed=True)['_taken'].sum()
    )
    for key, taken in band_take.items():
        if taken <= 0:
            continue
        cur = pool_dict.get(key, 0.0)
        pool_dict[key] = max(cur - float(taken), 0.0)

    # FNL_Q_REM refresh is deferred to the end of _run_majcat_waterfall.
    # _run_band reads pool_dict directly (not alloc_df['FNL_Q_REM']), so
    # in-flight bands stay correct without a full-table refresh here.


def _propagate_skips_to_alloc(
    alloc_df: pd.DataFrame,
    working_df: pd.DataFrame,
) -> None:
    """Propagate SKIPPED OPTs from working_df (OPT-level) into alloc_df (size-level).
    Called before each OPT_TYPE's first band so that cross-type store_broken and
    PRI_CT skips from prior types are visible to _run_band before it starts.
    SKIP_REASON is set to the ALLOC_REMARKS from working_df so the trigger value
    (e.g. 'SKIP_PRI_BROKEN(pri=85.0)') is preserved at the size level."""
    # Deterministic: sort by OPT_KEYS + ALLOC_REMARKS so the kept row for any
    # duplicated OPT key is reproducible across runs.
    skipped_df = (
        working_df.loc[
            working_df['ALLOC_STATUS'] == 'SKIPPED',
            OPT_KEYS + ['ALLOC_REMARKS'],
        ]
        .sort_values(OPT_KEYS + ['ALLOC_REMARKS'], kind='mergesort')
        .drop_duplicates(subset=OPT_KEYS)
    )
    if skipped_df.empty:
        return
    # Build OPT_KEYS tuple → ALLOC_REMARKS mapping for O(1) lookup
    remarks_map: dict = dict(
        zip(
            zip(*[skipped_df[c].to_numpy() for c in OPT_KEYS]),
            skipped_df['ALLOC_REMARKS'].fillna('').astype(str),
        )
    )
    alloc_keys = list(zip(*[alloc_df[c].to_numpy() for c in OPT_KEYS]))
    m_in = np.array([k in remarks_map for k in alloc_keys], dtype=bool)
    prop_mask = (
        m_in
        & (~alloc_df['ALLOC_STATUS'].isin(['SKIPPED', 'ALLOCATED', 'PARTIAL'])).to_numpy()
    )
    if prop_mask.any():
        prop_series = pd.Series(prop_mask, index=alloc_df.index)
        alloc_df.loc[prop_series, 'ALLOC_STATUS'] = 'SKIPPED'
        no_reason = prop_series & (alloc_df['SKIP_REASON'].fillna('').astype(str) == '')
        if no_reason.any():
            reason_vals = pd.Series(alloc_keys, index=alloc_df.index).map(remarks_map)
            alloc_df.loc[no_reason, 'SKIP_REASON'] = (
                reason_vals[no_reason].fillna('REVALIDATION_SKIP')
            )

    # Zero HOLD_QTY for ALL rows in the skip set, including any that were
    # previously ALLOCATED/PARTIAL.  A store-broken skip (MJ_REQ_REM < 0.5×ACS_D)
    # means the store has no meaningful remaining need — warehouse hold is wasted.
    hold_clear = pd.Series(
        m_in & (alloc_df['HOLD_QTY'].fillna(0).to_numpy() > 0),
        index=alloc_df.index,
    )
    if hold_clear.any():
        alloc_df.loc[hold_clear, 'HOLD_QTY']    = 0.0
        alloc_df.loc[hold_clear, 'ROUND_HOLD']  = 0.0


def _pre_band_check(
    alloc_df: pd.DataFrame,
    working_df: pd.DataFrame,
    ot: str,
    pri_ct_check_rl: bool,
    pri_ct_check_tbc: bool,
) -> None:
    """Actively evaluate PRI_CT_REM and MJ_REQ_REM before the first band of
    each OPT_TYPE and mark failing OPTs SKIPPED in working_df, then propagate
    those skips into alloc_df.

    Rules (applied in order):
      1. PRI_CT_REM < 100  → SKIP for enforced types (TBL always; RL/TBC when
         their primary-count gates are on). "alloc only pri_ct% is 1" means
         only OPTs where every primary grid still needs stock are eligible.
      2. MJ_REQ_REM < ACS_SKIP_FACTOR × ACS_D  → SKIP for the current type
         AND all later types (cross-type store-broken propagation).
    Finally propagates SKIPPED OPTs from working_df → alloc_df."""
    if working_df is None or working_df.empty:
        return
    work_cols = set(working_df.columns)

    # Types where PRI_CT_REM < 100 is a hard gate.
    enforced: set = {'TBL'}
    if pri_ct_check_rl:
        enforced.add('RL')
    if pri_ct_check_tbc:
        enforced.add('TBC')

    ot_idx = OPT_TYPE_ORDER.index(ot) if ot in OPT_TYPE_ORDER else 0
    remaining_types = OPT_TYPE_ORDER[ot_idx:]

    pending_mask = (
        (working_df['LISTED_FLAG'].fillna(0) == 1)
        & (~working_df['ALLOC_STATUS'].isin(['SKIPPED', 'ALLOCATED']))
    )
    if not pending_mask.any():
        return

    # Rule 1 — PRI_CT_REM < 100 for enforced types
    if 'PRI_CT_REM' in work_cols:
        pri_dead = (
            (working_df['PRI_CT_REM'].fillna(0) < 100)
            & (working_df['OPT_TYPE'].isin(enforced))
        )
        m_pri = pending_mask & pri_dead
        if m_pri.any():
            working_df.loc[m_pri, 'ALLOC_STATUS'] = 'SKIPPED'
            pri_vals = working_df.loc[m_pri, 'PRI_CT_REM'].fillna(0)
            suffix = pri_vals.apply(lambda v: f' SKIP_PRI_BROKEN(pri={v:.1f});')
            working_df.loc[m_pri, 'ALLOC_REMARKS'] = (
                working_df.loc[m_pri, 'ALLOC_REMARKS'].fillna('').astype(str) + suffix
            )

    # Rule 2 — store-broken cross-type (MJ_REQ_REM < threshold)
    if rne.ENABLE_STORE_BROKEN and 'MJ_REQ_REM' in work_cols and 'ACS_D' in work_cols:
        # Re-derive pending_mask after Rule 1 may have updated ALLOC_STATUS
        pending_mask2 = (
            (working_df['LISTED_FLAG'].fillna(0) == 1)
            & (~working_df['ALLOC_STATUS'].isin(['SKIPPED', 'ALLOCATED']))
        )
        sb_mask = (
            pending_mask2
            & (working_df['OPT_TYPE'].isin(remaining_types))
            & (working_df['MJ_REQ_REM'].fillna(0)
               < rne.ACS_SKIP_FACTOR * working_df['ACS_D'].fillna(0))
        )
        if sb_mask.any():
            working_df.loc[sb_mask, 'ALLOC_STATUS'] = 'SKIPPED'
            mj_vals = working_df.loc[sb_mask, 'MJ_REQ_REM'].fillna(0)
            suffix = mj_vals.apply(lambda v: f' SKIP_STORE_BROKEN(mj_rem={v:.1f});')
            working_df.loc[sb_mask, 'ALLOC_REMARKS'] = (
                working_df.loc[sb_mask, 'ALLOC_REMARKS'].fillna('').astype(str) + suffix
            )

    # Propagate all SKIPPED OPTs from working_df into alloc_df
    _propagate_skips_to_alloc(alloc_df, working_df)


def _revalidate_after_band(
    alloc_df: pd.DataFrame,
    working_df: pd.DataFrame,
    grids: Dict[str, Dict],
    ot: str,
    r: int,
    pri_ct_check_rl: bool,
    pri_ct_check_tbc: bool,
) -> None:
    """pandas equivalent of rule_engine_new._revalidate_after_band, scoped
    to one MAJ_CAT (alloc_df / working_df are already MAJ_CAT slices).
    Called once per (OPT_TYPE × round) after all stores have competed.
    r: the round just completed — skip-marking is limited to OPTs that have
    at least one alloc_df row with I_ROD >= r+1 (i.e. a next band coming)."""
    band_mask = (alloc_df['OPT_TYPE'] == ot)
    if not band_mask.any():
        return
    band = alloc_df.loc[band_mask, [
        *OPT_KEYS, 'ROUND_SHIP', 'ROUND_HOLD',
    ]]
    band_take_total = float(band['ROUND_SHIP'].sum() + band['ROUND_HOLD'].sum())
    if band_take_total <= 0:
        return  # early-exit — no _REM values changed

    work_cols = set(working_df.columns)

    # (1) Reduce MSA_FNL_Q_REM per OPT (WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR)
    if 'MSA_FNL_Q_REM' in work_cols:
        opt_take = (
            band.groupby(OPT_KEYS, sort=False, observed=True, dropna=False)
                .agg(_t=('ROUND_SHIP', 'sum'), _h=('ROUND_HOLD', 'sum'))
        )
        opt_take['_total'] = opt_take['_t'] + opt_take['_h']
        opt_take = opt_take[opt_take['_total'] > 0]['_total']
        if not opt_take.empty:
            opt_dict = opt_take.to_dict()
            keys = pd.Series(
                list(zip(*[working_df[c].to_numpy() for c in OPT_KEYS])),
                index=working_df.index,
            )
            decrement = keys.map(opt_dict).fillna(0).astype('float64')
            new_msa = (working_df['MSA_FNL_Q_REM'].to_numpy()
                       - decrement.to_numpy())
            working_df['MSA_FNL_Q_REM'] = np.maximum(new_msa, 0.0)

    # (2) Reduce each primary grid's REQ_REM at its grain
    band_ship = alloc_df.loc[band_mask, [*OPT_KEYS, 'ROUND_SHIP']]
    band_ship = band_ship[band_ship['ROUND_SHIP'] > 0]
    for req_col, meta in grids.items():
        req_rem = meta['req_rem']
        extras  = meta.get('extras') or []
        if req_rem not in work_cols:
            continue
        if not all(e in work_cols for e in extras):
            continue
        # Need extras values per alloc row → join through working_df by OPT_KEYS.
        if band_ship.empty:
            continue
        # Bring extras onto band rows by joining on OPT_KEYS (one row per OPT
        # in working_df, so this maps each alloc row to its extras values).
        if extras:
            # Deterministic: sort by OPT_KEYS + extras before drop_duplicates
            # so first-row-wins is reproducible across runs.
            opt_extras = (
                working_df[OPT_KEYS + extras]
                .sort_values(OPT_KEYS + extras, kind='mergesort')
                .drop_duplicates(subset=OPT_KEYS)
            )
            joined = band_ship.merge(opt_extras, on=OPT_KEYS, how='inner')
        else:
            joined = band_ship.copy()

        grid_keys = ['WERKS', 'MAJ_CAT'] + extras
        grid_take = (
            joined.groupby(grid_keys, sort=False, observed=True, dropna=False)
                  ['ROUND_SHIP'].sum()
        )
        grid_take = grid_take[grid_take > 0]
        if grid_take.empty:
            continue
        gt_dict = grid_take.to_dict()
        keys = pd.Series(
            list(zip(*[working_df[c].to_numpy() for c in grid_keys])),
            index=working_df.index,
        )
        decrement = keys.map(gt_dict).fillna(0).astype('float64')
        new_req = (working_df[req_rem].to_numpy() - decrement.to_numpy())
        working_df[req_rem] = np.maximum(new_req, 0.0)

    # (3) Recompute H_<grid>_REM = (REQ_REM >= ACS_SKIP_FACTOR*ACS_D) AND (GH=1)
    # Inclusive threshold (>=): a slot at exactly half-display still counts as
    # eligible — matches the all-or-nothing dispatch model where an OPT keeps
    # going as long as MJ_REQ_REM hasn't dropped strictly below the floor.
    acs = working_df['ACS_D'].to_numpy() if 'ACS_D' in work_cols else \
          np.zeros(len(working_df), dtype='float64')
    pri_h_cols: List[str] = []
    pri_gh_cols: List[str] = []
    for meta in grids.values():
        req_rem = meta['req_rem']
        gh_col  = meta['gh_col']
        h_rem   = meta['h_rem']
        if not all(c in work_cols for c in (req_rem, gh_col, h_rem)):
            continue
        req = working_df[req_rem].to_numpy()
        gh  = working_df[gh_col].to_numpy()
        new_h = ((req >= rne.ACS_SKIP_FACTOR * acs) & (gh == 1)).astype('float64')
        working_df[h_rem] = new_h
        pri_h_cols.append(h_rem)
        pri_gh_cols.append(gh_col)

    # (4) PRI_CT_REM = Σ(H_REM)/Σ(GH) × 100
    if pri_h_cols and pri_gh_cols and 'PRI_CT_REM' in work_cols:
        h_sum  = sum(working_df[c].to_numpy() for c in pri_h_cols)
        gh_sum = sum(working_df[c].to_numpy() for c in pri_gh_cols)
        with np.errstate(divide='ignore', invalid='ignore'):
            pri = np.where(
                gh_sum == 0,
                0.0,
                np.round(h_sum.astype('float64') / gh_sum * 100, 1),
            )
        working_df['PRI_CT_REM'] = pri

    # (5) Skip rules — scoped to OPTs that have a next band (I_ROD >= r+1).
    # Skipping OPTs with no remaining rounds is meaningless for the user and
    # creates confusing SKIP_REASON values on already-finished rows.
    enforced = {'TBL'}
    if pri_ct_check_rl:  enforced.add('RL')
    if pri_ct_check_tbc: enforced.add('TBC')

    # Compute the set of OPT_KEYS tuples that still have rounds left.
    next_round_alloc = alloc_df[alloc_df['I_ROD'] >= r + 1]
    if not next_round_alloc.empty:
        next_opt_tuples: set = set(
            zip(*[next_round_alloc[c].to_numpy() for c in OPT_KEYS])
        )
        nxt_working = pd.Series(
            [tuple(row) in next_opt_tuples
             for row in zip(*[working_df[c].to_numpy() for c in OPT_KEYS])],
            index=working_df.index,
        )
    else:
        nxt_working = pd.Series(False, index=working_df.index)

    pending_mask = (
        (working_df['LISTED_FLAG'].fillna(0) == 1)
        & (~working_df['ALLOC_STATUS'].isin(['SKIPPED', 'ALLOCATED']))
        & nxt_working
    )
    if pending_mask.any():
        msa_dead = (working_df['MSA_FNL_Q_REM'].fillna(0) <= 0)
        pri_dead = (
            (working_df['PRI_CT_REM'].fillna(0) < 100)
            & (working_df['OPT_TYPE'].isin(enforced))
        )
        # MSA_EXHAUSTED — include remaining qty in the remark for debuggability
        m_msa = pending_mask & msa_dead
        if m_msa.any():
            working_df.loc[m_msa, 'ALLOC_STATUS'] = 'SKIPPED'
            rem_vals = working_df.loc[m_msa, 'MSA_FNL_Q_REM'].fillna(0)
            suffix = rem_vals.apply(lambda v: f' SKIP_MSA_EXHAUSTED(rem={v:.1f});')
            working_df.loc[m_msa, 'ALLOC_REMARKS'] = (
                working_df.loc[m_msa, 'ALLOC_REMARKS'].fillna('').astype(str) + suffix
            )
        # PRI_BROKEN — include PRI_CT_REM value
        m_pri = pending_mask & pri_dead & (~msa_dead)
        if m_pri.any():
            working_df.loc[m_pri, 'ALLOC_STATUS'] = 'SKIPPED'
            pri_vals = working_df.loc[m_pri, 'PRI_CT_REM'].fillna(0)
            suffix = pri_vals.apply(lambda v: f' SKIP_PRI_BROKEN(pri={v:.1f});')
            working_df.loc[m_pri, 'ALLOC_REMARKS'] = (
                working_df.loc[m_pri, 'ALLOC_REMARKS'].fillna('').astype(str) + suffix
            )

    # (5b) Store-broken — scoped to next-band OPTs only.
    if rne.ENABLE_STORE_BROKEN and 'MJ_REQ_REM' in work_cols:
        ot_idx = OPT_TYPE_ORDER.index(ot) if ot in OPT_TYPE_ORDER else 0
        remaining_types = OPT_TYPE_ORDER[ot_idx:]
        sb_mask = (
            (working_df['LISTED_FLAG'].fillna(0) == 1)
            & (~working_df['ALLOC_STATUS'].isin(['SKIPPED', 'ALLOCATED']))
            & nxt_working
            & (working_df['OPT_TYPE'].isin(remaining_types))
            & (working_df['MJ_REQ_REM'].fillna(0)
               < rne.ACS_SKIP_FACTOR * working_df['ACS_D'].fillna(0))
        )
        if sb_mask.any():
            working_df.loc[sb_mask, 'ALLOC_STATUS'] = 'SKIPPED'
            mj_vals = working_df.loc[sb_mask, 'MJ_REQ_REM'].fillna(0)
            suffix = mj_vals.apply(lambda v: f' SKIP_STORE_BROKEN(mj_rem={v:.1f});')
            working_df.loc[sb_mask, 'ALLOC_REMARKS'] = (
                working_df.loc[sb_mask, 'ALLOC_REMARKS'].fillna('').astype(str) + suffix
            )

    # (6) Propagate SKIP back to alloc_df — scoped to next-band alloc rows.
    # Build OPT_KEYS → ALLOC_REMARKS mapping so SKIP_REASON carries the
    # exact trigger value (e.g. "SKIP_PRI_BROKEN(pri=85.0)") instead of
    # the generic 'REVALIDATION_SKIP'.
    # Deterministic: sort by OPT_KEYS + ALLOC_REMARKS before drop_duplicates
    # so the kept ALLOC_REMARKS string is reproducible across runs.
    newly_skipped = (
        working_df.loc[
            working_df['ALLOC_STATUS'] == 'SKIPPED',
            OPT_KEYS + ['ALLOC_REMARKS'],
        ]
        .sort_values(OPT_KEYS + ['ALLOC_REMARKS'], kind='mergesort')
        .drop_duplicates(subset=OPT_KEYS)
    )
    if not newly_skipped.empty:
        remarks_map: dict = dict(
            zip(
                zip(*[newly_skipped[c].to_numpy() for c in OPT_KEYS]),
                newly_skipped['ALLOC_REMARKS'].fillna('').astype(str),
            )
        )
        # Only propagate to alloc rows that have a next band.
        next_alloc_mask = alloc_df['I_ROD'] >= r + 1
        if next_alloc_mask.any():
            sub = alloc_df.loc[next_alloc_mask]
            alloc_keys_next = list(zip(*[sub[c].to_numpy() for c in OPT_KEYS]))
            m_in_next = np.array([k in remarks_map for k in alloc_keys_next], dtype=bool)
            prop_idx = sub.index[
                m_in_next
                & (~sub['ALLOC_STATUS'].isin(['SKIPPED', 'ALLOCATED', 'PARTIAL'])).to_numpy()
            ]
            if len(prop_idx):
                alloc_df.loc[prop_idx, 'ALLOC_STATUS'] = 'SKIPPED'
                opt_keys_series = pd.Series(
                    [tuple(row) for row in zip(*[alloc_df.loc[prop_idx, c].to_numpy()
                                                  for c in OPT_KEYS])],
                    index=prop_idx,
                )
                reason_series = opt_keys_series.map(remarks_map).fillna('REVALIDATION_SKIP')
                sr_existing = alloc_df.loc[prop_idx, 'SKIP_REASON'].fillna('').astype(str)
                alloc_df.loc[prop_idx, 'SKIP_REASON'] = np.where(
                    sr_existing == '', reason_series, sr_existing
                )


# ---------------------------------------------------------------------------
# Bulk write-back
# ---------------------------------------------------------------------------
_ALLOC_WRITE_COLS = [
    'WERKS', 'RDC', 'MAJ_CAT', 'GEN_ART_NUMBER', 'CLR', 'VAR_ART', 'SZ',
    'SHIP_QTY', 'HOLD_QTY', 'ALLOC_QTY', 'FROM_HOLD_QTY',
    'ALLOC_STATUS', 'SKIP_REASON',
    'POOL_CONSUMED', 'ALLOC_WAVE', 'ALLOC_ROUND',
    'FNL_Q_REM',
]


def _write_back_alloc(engine, alloc_table: str, df: pd.DataFrame) -> None:
    """Bulk-MERGE the updated alloc rows back into alloc_table."""
    cols = [c for c in _ALLOC_WRITE_COLS if c in df.columns]
    out = df[cols].copy()
    if 'ALLOC_QTY' not in out.columns and 'SHIP_QTY' in out.columns:
        out['ALLOC_QTY'] = out['SHIP_QTY']
    # Pad pool-key strings so '' joins match in SQL too.
    for c in POOL_KEYS:
        if c in out.columns:
            out[c] = out[c].fillna('').astype(str)
    if out.empty:
        return

    tmp = "#alloc_pd_writeback"
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        # Use fast_executemany on the underlying pyodbc cursor.
        try:
            cur.fast_executemany = True
        except Exception:
            pass

        # Make this session the designated deadlock victim. When 8 workers
        # contend on tempdb metadata / memory grants, SQL Server picks ONE
        # to kill — by setting LOW we ensure the writer (which has a tiny
        # rollback cost) loses, not auth/login/progress-poll requests. The
        # outer retry_on_deadlock then reruns the writer cleanly.
        try:
            cur.execute("SET DEADLOCK_PRIORITY LOW")
        except Exception:
            pass

        # DDL guard: ensure FROM_HOLD_QTY column exists in target table.
        try:
            cur.execute(f"""
                IF NOT EXISTS (
                    SELECT 1 FROM sys.columns
                    WHERE object_id = OBJECT_ID('{alloc_table}')
                      AND name = 'FROM_HOLD_QTY'
                )
                ALTER TABLE [{alloc_table}] ADD [FROM_HOLD_QTY] FLOAT NULL
            """)
            raw.commit()
        except Exception:
            pass

        col_defs = []
        for c in cols:
            if c in {'SHIP_QTY', 'HOLD_QTY', 'ALLOC_QTY', 'FROM_HOLD_QTY',
                     'POOL_CONSUMED', 'ALLOC_ROUND', 'FNL_Q_REM'}:
                col_defs.append(f"[{c}] FLOAT NULL")
            else:
                col_defs.append(f"[{c}] NVARCHAR(200) NULL")
        cur.execute(f"CREATE TABLE {tmp} ({', '.join(col_defs)})")

        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(f"[{c}]" for c in cols)
        rows = [
            tuple(None if (isinstance(v, float) and np.isnan(v)) else v for v in row)
            for row in out.itertuples(index=False, name=None)
        ]
        cur.executemany(
            f"INSERT INTO {tmp} ({col_list}) VALUES ({placeholders})",
            rows,
        )

        update_pairs = ", ".join(
            f"T.[{c}] = S.[{c}]"
            for c in cols
            if c not in ('WERKS', 'RDC', 'MAJ_CAT', 'GEN_ART_NUMBER', 'CLR', 'VAR_ART', 'SZ')
        )
        cur.execute(f"""
            UPDATE T SET {update_pairs}
            FROM [{alloc_table}] T WITH (ROWLOCK, UPDLOCK)
            INNER JOIN {tmp} S
              ON T.WERKS = S.WERKS AND T.RDC = S.RDC
             AND T.MAJ_CAT = S.MAJ_CAT
             AND T.GEN_ART_NUMBER = S.GEN_ART_NUMBER
             AND ISNULL(T.CLR,'')   = ISNULL(S.CLR,'')
             AND T.VAR_ART = S.VAR_ART AND T.SZ = S.SZ
            OPTION (MAXDOP 1)
        """)
        cur.execute(f"DROP TABLE {tmp}")
        raw.commit()
    finally:
        raw.close()


def _write_back_working(engine, working_table: str, df: pd.DataFrame,
                        grids: Dict[str, Dict]) -> None:
    """Bulk-MERGE the updated working rows back into working_table.
    Writes the _REM family + ALLOC_STATUS/REMARKS only (other columns
    untouched)."""
    write_cols = [
        'WERKS', 'MAJ_CAT', 'GEN_ART_NUMBER', 'CLR',
        'ALLOC_STATUS', 'ALLOC_REMARKS',
        'MSA_FNL_Q_REM', 'PRI_CT_REM',
    ]
    for meta in grids.values():
        for col in (meta['req_rem'], meta['h_rem']):
            if col in df.columns and col not in write_cols:
                write_cols.append(col)

    cols = [c for c in write_cols if c in df.columns]
    out = df[cols].copy()
    for c in OPT_KEYS:
        if c in out.columns:
            out[c] = out[c].fillna('').astype(str)
    if out.empty:
        return

    tmp = "#working_pd_writeback"
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        try:
            cur.fast_executemany = True
        except Exception:
            pass

        # See _write_back_alloc — designate this session as deadlock victim
        # so the retry path (not unrelated requests) absorbs any contention.
        try:
            cur.execute("SET DEADLOCK_PRIORITY LOW")
        except Exception:
            pass

        col_defs = []
        for c in cols:
            if c in OPT_KEYS or c in ('ALLOC_STATUS', 'ALLOC_REMARKS'):
                col_defs.append(f"[{c}] NVARCHAR(400) NULL")
            else:
                col_defs.append(f"[{c}] FLOAT NULL")
        cur.execute(f"CREATE TABLE {tmp} ({', '.join(col_defs)})")

        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(f"[{c}]" for c in cols)
        rows = [
            tuple(None if (isinstance(v, float) and np.isnan(v)) else v for v in row)
            for row in out.itertuples(index=False, name=None)
        ]
        cur.executemany(
            f"INSERT INTO {tmp} ({col_list}) VALUES ({placeholders})",
            rows,
        )

        update_pairs = ", ".join(
            f"T.[{c}] = S.[{c}]" for c in cols if c not in OPT_KEYS
        )
        cur.execute(f"""
            UPDATE T SET {update_pairs}
            FROM [{working_table}] T WITH (ROWLOCK, UPDLOCK)
            INNER JOIN {tmp} S
              ON T.WERKS = S.WERKS
             AND T.MAJ_CAT = S.MAJ_CAT
             AND T.GEN_ART_NUMBER = S.GEN_ART_NUMBER
             AND ISNULL(T.CLR,'') = ISNULL(S.CLR,'')
            OPTION (MAXDOP 1)
        """)
        cur.execute(f"DROP TABLE {tmp}")
        raw.commit()
    finally:
        raw.close()
