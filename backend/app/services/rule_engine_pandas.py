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
import re
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

# Per-OPT is the ONLY band engine (2026-07-10 removal — see
# app/docs/REMOVAL_PLAN_PER_OPT_ONLY.md). The former ARS_PER_OPT_MODE env
# switch (_is_per_opt_mode) and the pandas cumulative-window band (_run_band,
# with its _hold_suppress_arr helper) were deleted; this module keeps ONLY
# the orchestration per_opt runs on: worker pool, writer queue, table loads,
# sec-cap specs, Stage D pak rounding, result write-back. The band itself is
# rule_engine_per_opt._run_band_per_opt (which has its own hold-suppress
# helper at rule_engine_per_opt.py:67).


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
    if len(args) >= 15:
        (mc, a_slice, w_slice, grids, batch_id, alloc_table, working_table,
         pri_ct_check_rl, pri_ct_check_tbc,
         rl_mbq_cap_pct, tbc_mbq_cap_pct,
         size_threshold, min_size_count, opt_types, defer_writes,
         *_extras) = args
    else:
        (mc, a_slice, w_slice, grids, batch_id, alloc_table, working_table,
         pri_ct_check_rl, pri_ct_check_tbc,
         rl_mbq_cap_pct, tbc_mbq_cap_pct,
         size_threshold, min_size_count, opt_types) = args
        defer_writes = False
        _extras = []
    tbl_mbq_cap_pct = float(_extras[0]) if _extras else 0.0
    # _extras[1] (17th tuple element) carries the post-loop TBL MJ_REQ cap_pct
    # forward to the per-OPT engine so the same threshold can be evaluated
    # PRE-allocation (Fix B). Backwards-compatible: missing → 100% (the same
    # default as run_listing_and_allocation_pandas).
    tbl_mj_req_cap_pct = float(_extras[1]) if len(_extras) > 1 else 100.0
    # _extras[2] (18th tuple element) carries the per-OPT sec-cap grid specs
    # so each worker builds its MAJ_CAT-scoped budgets/running state inside
    # _run_majcat_waterfall. Backwards-compatible: missing → None (post-pass
    # SQL gate handles it instead).
    sec_cap_grid_specs = _extras[2] if len(_extras) > 2 else None
    # _extras[3], _extras[4] (19th, 20th elements) carry the per-OPT_TYPE
    # dispatch mode for RL and TBC respectively. SCALED = round-then-shave
    # partial OPT against werks_cap; COMPLETE = all-or-skip. Missing → both
    # 'COMPLETE' (matches the new default).
    rl_dispatch_mode  = str(_extras[3]) if len(_extras) > 3 else 'COMPLETE'
    tbc_dispatch_mode = str(_extras[4]) if len(_extras) > 4 else 'COMPLETE'
    # _extras[5], _extras[6] (21st, 22nd elements) carry the sec-cap growth
    # matrix (spec 2026-07-08). matrix_enabled=False → flat per-grid cap
    # (byte-identical to pre-matrix behaviour). Missing → disabled.
    matrix_enabled = bool(_extras[5]) if len(_extras) > 5 else False
    matrix_bands   = list(_extras[6]) if len(_extras) > 6 else []
    # _extras[7..10] (23rd–26th elements) — Fresh/GRT hold control
    # (FSD_FRESH_GRT_HOLD_CONTROL FS-03/FS-06/FS-09). alloc_type scopes the
    # typed hold-tracking load below; the three flags drive the FS-06
    # waterfall hold-suppression mask. Missing → defaults (byte-identical
    # legacy behaviour).
    alloc_type         = str(_extras[7])   if len(_extras) > 7  else "FRESH"
    skip_hold_upc      = bool(_extras[8])  if len(_extras) > 8  else False
    apply_hold_seg_app = bool(_extras[9])  if len(_extras) > 9  else True
    apply_hold_seg_gm  = bool(_extras[10]) if len(_extras) > 10 else True

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
                    # FS-09 typed hold consumption: a hold created by a run of
                    # type T may only be drawn by runs of type T; legacy
                    # untyped rows (ALLOC_TYPE = '' / NULL) match ANY run type
                    # so pre-migration holds keep draining for every run.
                    _hrows = _hc.execute(text(
                        "SELECT WERKS, VAR_ART, SZ, ISNULL(HOLD_REM, 0.0) AS hold_rem "
                        "FROM ARS_NL_TBL_HOLD_TRACKING "
                        "WHERE IS_CLOSED = 0 AND ISNULL(HOLD_REM, 0.0) > 0 "
                        "AND ([ALLOC_TYPE] = :alloc_type OR ISNULL([ALLOC_TYPE],'') = '')"
                    ), {"alloc_type": str(alloc_type or "FRESH")}).fetchall()
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
            tbl_mbq_cap_pct=tbl_mbq_cap_pct,
            hold_dict=hold_dict if hold_dict else None,
            size_threshold=size_threshold,
            min_size_count=min_size_count,
            opt_types=opt_types,
            tbl_mj_req_cap_pct=tbl_mj_req_cap_pct,
            sec_cap_grid_specs=sec_cap_grid_specs,
            rl_dispatch_mode=rl_dispatch_mode,
            tbc_dispatch_mode=tbc_dispatch_mode,
            matrix_enabled=matrix_enabled,
            matrix_bands=matrix_bands,
            alloc_type=alloc_type,
            skip_hold_upc=skip_hold_upc,
            apply_hold_seg_app=apply_hold_seg_app,
            apply_hold_seg_gm=apply_hold_seg_gm,
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
        import traceback as _tb
        tb_str = _tb.format_exc()
        # Bare keys (KeyError → just 'COLNAME') and similar one-word repr
        # exceptions are useless without context. Keep the short form for
        # the QUEUE_TABLE error column but log the full traceback so the
        # operator can see file:line of the actual raise.
        err = (f"{type(e).__name__}: {e}")[:2000]
        dur = time.time() - t_mc
        logger.error(
            f"[pandas] {mc}: worker FAILED ({err}) — full traceback:\n{tb_str}"
        )
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


def _stage_d_apply_pak_sz_rounding(conn, alloc_table: str) -> None:
    """SQL safety-net for PAK_SZ rounding. Most rows are already pak-aligned
    by `_apply_pak_sz_rounding_df` during MAJ_CAT write-back; this catches
    anything that slipped through.

    Half-up rule: SHIP_QTY rounds to the nearest whole pak. req >= 0.5*pak
    rounds UP (e.g. 5/6 -> 6, 11/6 -> 12); below the half-pak threshold the
    row is gated to 0 and marked SKIPPED. POOL_CONSUMED is adjusted by the
    same delta so the FNL_Q_REM recompute downstream refunds (or charges)
    the pool correctly."""
    try:
        run_sql(conn, f"ALTER TABLE [{alloc_table}] ADD [ALLOC_REMARKS] NVARCHAR(MAX) NULL")
    except Exception:
        pass  # idempotent — column may already exist
    try:
        run_sql(conn, f"""
            ;WITH P AS (
                SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
                       ISNULL(SHIP_QTY, 0)                       AS old_ship,
                       COALESCE(NULLIF(PAK_SZ, 0), 1)            AS pak,
                       ISNULL(SHIP_QTY, 0)                       AS req
                FROM [{alloc_table}]
                WHERE ISNULL(SHIP_QTY, 0) > 0
                  -- Per-OPT engine (rule_engine_per_opt._run_band_per_opt)
                  -- applies pak rounding in-loop and stamps a PAK_SZ_GATE
                  -- or PAK_SZ_ROUND marker in ALLOC_REMARKS. Skip those
                  -- rows here so this safety-net does not re-round (and
                  -- in particular does not undo the new stock-clipped
                  -- SHIP values which are intentionally non-pak-aligned).
                  AND CHARINDEX('PAK_SZ_', ISNULL(ALLOC_REMARKS, '')) = 0
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
    tbl_mbq_cap_pct: float = 0.0,
    # Per-OPT_TYPE downward MJ_REQ caps applied AFTER waterfall write-back,
    # BEFORE PAK_SZ rounding. Each clamps SUM(SHIP_QTY) for that OPT_TYPE
    # per (WERKS, MAJ_CAT) to cap_pct% × MJ_REQ. 100 = no over-ship vs
    # MAJ_CAT req; 0 = cap disabled.
    rl_mj_req_cap_pct:  float = 100.0,
    tbc_mj_req_cap_pct: float = 100.0,
    tbl_mj_req_cap_pct: float = 100.0,
    # MJ_REQ growth headroom — applied upstream by /listing-build by scaling
    # MJ_REQ on ARS_LISTING_WORKING.  Engine receives the already-scaled value
    # and does not re-scale; this param is informational (audit log only).
    mj_req_growth_pct:  float = 100.0,
    opt_types: Optional[List[str]] = None,  # restrict waterfall to these OPT_TYPEs only
    use_writer_queue: Optional[bool] = None, # per-run override; None = fall back to .env
    apply_sec_cap_in_normal: bool = True,    # strict per-grid cap on opted-in grids
    # Per-OPT_TYPE dispatch mode (per-OPT engine only). SCALED = proportional
    # round-then-shave partial OPT; COMPLETE = all-or-skip, leave werks_cap
    # for next OPT in band. Defaults match the new run-config defaults.
    rl_dispatch_mode:  str = 'COMPLETE',
    tbc_dispatch_mode: str = 'COMPLETE',
    # CONT fallback for SZ_APPLICABLE='N' MAJ_CATs (see rule_engine_new).
    # Default P4_UNIFORM = always fill with 1/N (Σ=1 per OPT, never leave
    # size-agnostic MAJ_CATs unallocated).
    cont_fallback_mode: str = "P4_UNIFORM",
    # ── Fresh/GRT hold control (FSD_FRESH_GRT_HOLD_CONTROL) ──
    # alloc_type scopes the typed ARS_NL_TBL_HOLD_TRACKING load (FS-09):
    # rows of another type are invisible to this run; legacy '' / NULL rows
    # match any type. The three flags drive the FS-06 waterfall hold-
    # suppression mask (skip UPC stores / SEG toggles). Defaults are
    # byte-identical to pre-change behaviour.
    alloc_type: str = "FRESH",
    skip_hold_upc: bool = False,
    apply_hold_seg_app: bool = True,
    apply_hold_seg_gm: bool = True,
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
        f"RL: {'PRI>=100 strict (MJ-cap 100%)' if pri_ct_check_rl else f'MBQ-cap {rl_mbq_cap_pct}%'} | "
        f"TBC: {'PRI>=100 strict (MJ-cap 100%)' if pri_ct_check_tbc else f'MBQ-cap {tbc_mbq_cap_pct}%'} | "
        f"MJ_REQ growth={mj_req_growth_pct}%"
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
                rl_mbq_cap_pct=rl_mbq_cap_pct,
                tbc_mbq_cap_pct=tbc_mbq_cap_pct,
                tbl_mbq_cap_pct=tbl_mbq_cap_pct,
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
                alloc_type=alloc_type,
            )
            logger.info(f"[B] alloc rows = {base_rows}")
            if base_rows == 0:
                result["duration_sec"] = round(time.time() - t0, 1)
                return result
            rne._stage_b_fill_cont(conn, alloc_table, cont_table, mode=cont_fallback_mode)
            rne._stage_b_fill_targets(conn, alloc_table, var_grid_table)
            rne._stage_b_indexes(conn, alloc_table)

            # Ensure per-row reason-tracking columns exist (alloc_table was
            # just re-created by Stage B). ALLOC_REMARKS is also written by
            # the per-band audit trail in _run_band, so it must exist DB-side
            # before the per-MAJ_CAT write-back fires — not just at finalise.
            rne._ensure_phase_reason_cols(conn, alloc_table, working_table)
            rne._ensure_alloc_remarks_col(conn, alloc_table)

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

    # ── Build per-OPT sec-cap grid specs once at the parent ──────────────
    # A grid participates ONLY when ARS_GRID_BUILDER.sec_cap_applicable=1.
    # Nothing is enforced by default — including MJ. If an operator wants
    # the MAJ_CAT-level cap, they explicitly add an MJ row to Grid Builder
    # with sec_cap_applicable=1 and a sec_cap_pct. Grids whose sec_cap_pct
    # is NULL fall back to SEC_CAP_DEFAULT_PCT (130) just like before.
    #
    # This is the strict reading of "cap only applies to grids toggled on
    # in Grid Builder, rest run as normal" — no Primary special case, no
    # implicit MJ enforcement.
    sec_cap_grid_specs: Optional[List[Tuple[str, Dict[str, Any]]]] = None
    if apply_sec_cap_in_normal:
        try:
            with engine.connect() as _sc_conn:
                _all = rne._discover_all_active_grids(_sc_conn)
            sec_cap_grid_specs = []
            skipped = []
            for g_name, meta in _all.items():
                grid_group = str(meta.get("group", "")).strip().lower()
                is_primary = (grid_group == "primary")
                # Primary grids (every group='Primary' row in ARS_GRID_BUILDER —
                # today MJ and MJ_MERGE_RNG_SEG) are hard-capped regardless of
                # the sec_cap_applicable flag. Secondary grids only participate
                # when the operator opted them in via Grid Builder.
                if not is_primary and not meta.get("sec_cap_applicable"):
                    skipped.append(g_name)
                    continue
                grid_pct = meta.get("sec_cap_pct")
                if grid_pct is not None:
                    cap_pct = float(grid_pct)
                elif is_primary:
                    # Primary grids anchor to the UI run headroom
                    # (mj_req_growth_pct). Secondary fallback keeps the legacy
                    # SEC_CAP_DEFAULT_PCT baseline.
                    cap_pct = float(mj_req_growth_pct)
                else:
                    cap_pct = float(rne.SEC_CAP_DEFAULT_PCT)
                sec_cap_grid_specs.append((g_name, {
                    "prefix":     meta.get("prefix") or g_name,
                    "extras":     list(meta.get("extras") or []),
                    "cap_factor": cap_pct / 100.0,
                    "cap_pct":    cap_pct,
                    "gh_col":     meta.get("gh_col", ""),
                    "is_primary": is_primary,
                }))
            # Primary first: when one OPT breaches both a Primary and a
            # Secondary grid, the Primary block is the harder constraint
            # (no override path) — the operator should see that SKIP_REASON.
            sec_cap_grid_specs.sort(key=lambda kv: 0 if kv[1].get("is_primary") else 1)
            logger.info(
                f"[C-pd] per-OPT sec-cap specs built — grids: "
                f"{[(g, s['cap_pct'], 'P' if s.get('is_primary') else 'S') for g, s in sec_cap_grid_specs]} | "
                f"skipped (non-Primary with sec_cap_applicable=0): {skipped}"
            )
        except Exception as _e:
            logger.warning(
                f"[C-pd] failed to build per-OPT sec-cap specs ({_e}) — "
                f"falling back to post-pass SQL gate"
            )
            sec_cap_grid_specs = None

    # ── Sec-cap growth matrix (spec 2026-07-08) ────────────────────────
    # Load the cont%-band matrix ONCE per run and thread the resolved
    # (enabled, bands) pair to every worker. Toggle off (default) →
    # matrix_enabled=False here; downstream behaviour is byte-identical
    # to the pre-matrix implementation.
    matrix_enabled = False
    matrix_bands: List[Tuple[float, Optional[float], float]] = []
    if sec_cap_grid_specs:
        try:
            from app.services.sec_cap_growth_matrix import load_matrix
            matrix_enabled, matrix_bands = load_matrix(engine)
            logger.info(
                f"[C-pd] sec-cap growth matrix — enabled={matrix_enabled} "
                f"bands={len(matrix_bands)}"
            )
        except Exception as _e:
            logger.warning(
                f"[C-pd] sec-cap growth matrix load failed ({_e}); flat cap used"
            )
            matrix_enabled = False
            matrix_bands = []

    # use_pool decides whether we'll spin up a ProcessPoolExecutor. Below the
    # min-MAJ_CATs threshold or with n_workers≤1 we fall back to inline
    # execution. defer_writes must be tied to use_pool: the writer thread
    # ONLY runs when use_pool=True (see writer_queue setup below). If
    # defer_writes=True but use_pool=False, the worker returns DataFrames
    # that nobody writes — the inline result loop drops them, alloc_rows
    # ends up 0, and the MAJ_CAT queue row stays IN_PROGRESS (mark_done
    # lives in the writer thread). Symptom: ship/hold logged per round but
    # final totals all zero. Fix: only defer when we're actually pooling.
    use_pool = (len(alloc_groups) >= PROCESS_POOL_MIN_MAJCATS and n_workers > 1)
    defer_writes_flag = bool(use_writer_queue and use_pool)

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
            # callers still work (defer_writes defaults in the worker's
            # unpack).
            defer_writes_flag,
            float(tbl_mbq_cap_pct),
            # 17th element — TBL post-loop MJ_REQ cap_pct, forwarded so the
            # per-OPT engine can evaluate the same gate PRE-allocation
            # (Fix B). Falls back to 100% in the worker's unpack.
            float(tbl_mj_req_cap_pct),
            # 18th element — per-OPT sec-cap grid specs. None disables
            # in-band sec-cap (post-pass SQL gate handles it instead).
            sec_cap_grid_specs,
            # 19th, 20th elements — per-OPT_TYPE dispatch mode (RL, TBC).
            # SCALED = proportional round-then-shave partial OPT;
            # COMPLETE = all-or-skip with budget pass-through.
            str(rl_dispatch_mode),
            str(tbc_dispatch_mode),
            # 21st, 22nd elements — sec-cap growth matrix (spec 2026-07-08).
            # matrix_enabled=False means workers use the per-grid flat
            # cap_factor (byte-identical to pre-matrix behaviour).
            bool(matrix_enabled),
            list(matrix_bands),
            # 23rd–26th elements — Fresh/GRT hold control
            # (FSD_FRESH_GRT_HOLD_CONTROL FS-03/FS-06/FS-09). alloc_type
            # scopes the worker's typed hold-tracking load; the flags feed
            # the FS-06 hold-suppression mask. Missing → legacy defaults.
            str(alloc_type or "FRESH"),
            bool(skip_hold_upc),
            bool(apply_hold_seg_app),
            bool(apply_hold_seg_gm),
        )
        for mc in alloc_groups
    ]

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
        # Ensure ALLOC_REMARKS column on alloc_table exists before any
        # cap helper writes audit detail to it. Older alloc_tables didn't
        # have this column; idempotent ALTER is safe to call here.
        rne._ensure_alloc_remarks_col(conn, alloc_table)
        # PAK_SZ rounding runs before the MJ_REQ gate. The gate uses OPT-grain
        # SHIP/HOLD sums to validate each OPT, so individual sizes must be in
        # their final pak-aligned form first.
        _stage_d_apply_pak_sz_rounding(conn, alloc_table)
        # OPT-grain MJ_REQ gate (full-OPT-or-skip). Per (WERKS, MAJ_CAT) the
        # first OPT (in priority order RL→TBC→TBL, then OPT_PRIORITY_TIER/
        # RANK/ST_RANK) whose `cap_pct × MJ_REQ ≥ 0.5 × OPT_MBQ` ships in
        # full — SHIP and HOLD at every (VAR_ART, SZ) row stay as the
        # waterfall produced them. Everything else in (WERKS, MAJ_CAT) gets
        # SHIP=HOLD=0 with SKIP_REASON='{OPT}_MJ_REQ_GATE_FAIL' or
        # '{OPT}_MJ_REQ_POST_WINNER'. Reuses the sequential engine's SQL
        # helper since the operation is identical across engines.
        #
        # Per-OPT (Fix B): the TBL portion of this gate is enforced
        # PRE-allocation inside _run_band_per_opt, so skip_tbl_branch is
        # hard-pinned True — the post-loop bypasses the TBL skip-records
        # logic entirely while still consuming req_rem for RL/TBC accounting
        # consistency.
        # IMPORTANT: do NOT set tbl_cap_pct=0 — that triggers the disabled
        # branch which zeroes every TBL OPT (regression confirmed: 4,088
        # TBL OPTs vaporized with TBL_MJ_REQ_GATE_DISABLED).
        rne._stage_c_apply_opt_mj_req_gate(
            conn, alloc_table, working_table,
            rl_cap_pct=rl_mj_req_cap_pct,
            tbc_cap_pct=tbc_mj_req_cap_pct,
            tbl_cap_pct=tbl_mj_req_cap_pct,
            skip_tbl_branch=True,
        )
        # Safety-net: a SKIPPED row with no ship has no business holding WH
        # stock — zero its hold so the buffer is returned to FNL_Q_REM.  Rows
        # that DID ship (SHIP_QTY>0) but were round-locked by the revalidate
        # propagation are legitimate r=1 partial dispatches; their HOLD is
        # the warehouse buffer paired with the ship and MUST be preserved.
        run_sql(conn, f"""
            UPDATE [{alloc_table}] SET HOLD_QTY = 0, ROUND_HOLD = 0
            WHERE ISNULL(HOLD_QTY, 0) > 0
              AND ISNULL(SHIP_QTY, 0) = 0
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
        # NO FNL_Q_REM recompute here: the per-OPT engine writes a live
        # post-draw value into FNL_Q_REM at each OPT's turn (see
        # rule_engine_per_opt.py step 5f.1) — that value is authoritative
        # for audit (tells pool-exhausted apart from pak-gated) and must
        # NOT be replaced with an aggregate residual. (The old non-per_opt
        # recompute branch was removed 2026-07-10 with the pandas band.)
        # PAK_SZ rounding moved earlier (before MJ_REQ cap). ALLOC_QTY now
        # reflects the post-cap, pak-aligned SHIP_QTY.
        run_sql(conn, f"UPDATE [{alloc_table}] WITH (ROWLOCK, UPDLOCK) SET ALLOC_QTY = SHIP_QTY")
        run_sql(conn, f"""
            UPDATE [{alloc_table}] WITH (ROWLOCK, UPDLOCK) SET
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
                    -- Preserve any pre-stamped reason from the waterfall or
                    -- post-waterfall gates (PAK_SZ_*, *_MJ_REQ_GATE_*, SEC_CAP_*,
                    -- MBQ_CAP_*, MJ_REQ_CAP, R09_HEADROOM_TRIVIAL, R07_SIZE_RATIO_LIVE,
                    -- SKIP_PRI_BROKEN, CROSS_SKIP_*, REVALIDATION_SKIP).  Without
                    -- this broad guard the catch-all NO_POOL_MSA arm below stomps
                    -- the real cause and the audit trail loses why the row was zeroed.
                    -- Mirrors rule_engine_new.py:2547.
                    WHEN ISNULL(SKIP_REASON,'') <> '' THEN SKIP_REASON
                    WHEN SHIP_QTY = 0 AND HOLD_QTY = 0
                         AND CASE WHEN OPT_TYPE='TBL'
                                  THEN ISNULL(SZ_MBQ_WH,0)+(ISNULL(I_ROD,1)-1)*ISNULL(SZ_MBQ,0)
                                  ELSE ISNULL(I_ROD,1)*ISNULL(SZ_MBQ,0) END
                             - ISNULL(SZ_STK,0) <= 0
                         THEN 'ALREADY_STOCKED'
                    -- Split NO_POOL_OR_DEMAND so users can tell demand-side
                    -- (SZ_REQ<=0 → no demand) from supply-side (MSA pool empty).
                    WHEN SHIP_QTY = 0 AND HOLD_QTY = 0
                         AND ISNULL(SZ_REQ, 0) <= 0 THEN 'NO_REQ'
                    WHEN SHIP_QTY = 0 AND HOLD_QTY = 0 THEN 'NO_POOL_MSA'
                    ELSE SKIP_REASON END
            OPTION (MAXDOP 1)
        """)
        # ── Secondary-grid cap (main pass, toggle-controlled) ─
        # Pandas workers used in-memory pools; build a #nre_pool on the parent
        # from the current FNL_Q_REM state so the sec-cap helper can return
        # stock against the same authoritative table.
        #
        # IMPORTANT: when sec_cap_grid_specs was built successfully, the
        # per-OPT engine already enforced sec-cap inside each band — running
        # totals were maintained as OPTs shipped, blocked OPTs never touched
        # pool, remarks are clean. The post-pass SQL gate would then walk the
        # same OPTs again, find no breaches (per-OPT already blocked the
        # breakers), and waste a few seconds of work. Skip it. The post-pass
        # gate below is per_opt's SAFETY NET for when the spec build failed
        # (sec_cap_grid_specs is None) — do not delete it.
        _per_opt_sec_cap_already_ran = sec_cap_grid_specs is not None
        if apply_sec_cap_in_normal and not _per_opt_sec_cap_already_ran:
            run_sql(conn, f"IF OBJECT_ID('tempdb..{rne.POOL_TABLE}') IS NOT NULL DROP TABLE {rne.POOL_TABLE}")
            run_sql(conn, f"""
                SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
                       MAX(ISNULL(FNL_Q,     0)) AS FNL_Q_ORIG,
                       MAX(ISNULL(FNL_Q_REM, 0)) AS FNL_Q_REM
                INTO {rne.POOL_TABLE}
                FROM [{alloc_table}]
                GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ
            """)
            try:
                run_sql(conn, f"""
                    CREATE UNIQUE CLUSTERED INDEX IX_pool_key ON {rne.POOL_TABLE}
                      (RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ)
                """)
            except Exception:
                pass
            logger.info(f"[C-pd] built {rne.POOL_TABLE} on parent for sec-cap "
                        f"(post-pandas remaining stock)")

            _all_grids_main = rne._discover_all_active_grids(conn)
            rne._apply_sec_grid_cap_pre_gate(
                conn, alloc_table, working_table, _all_grids_main,
                opt_type=None,
                growth_pct=mj_req_growth_pct,
                include_primary=True,
            )

            # Drop the parent-side pool now — Stage D doesn't need it, and the
            # parent connection persists across runs in some callers.
            run_sql(conn, f"IF OBJECT_ID('tempdb..{rne.POOL_TABLE}') IS NOT NULL DROP TABLE {rne.POOL_TABLE}")

        # Refund every Primary grid's *_REQ_REM from final SHIP_QTY, then
        # recompute the row-level H_<grid>_REM flags and PRI_CT_REM that
        # depend on them.
        #
        # The waterfall + _revalidate_after_band decremented *_REQ_REM with
        # in-band ROUND_SHIP. Post-waterfall stages (PAK_SZ rounding gate,
        # OPT_MJ_REQ gate, SEC_CAP pre-gate) may have zeroed SHIP_QTY AFTER
        # that decrement. Without this refund, *_REQ_REM stays stale and
        # H_<grid>_REM (set from *_REQ_REM > 0.5×ACS_D) likewise — leading
        # to the visible inconsistency MJ_REQ_REM=139 / H_MJ_REM=0 on rows
        # whose ship was reverted by a post-band gate.
        #
        # Idempotent and gate-agnostic — recomputes from scratch against
        # whatever final SHIP_QTY ended up being.
        grids_for_refund = rne._discover_primary_grids(conn)
        work_cols_refund = {c.upper() for c in rne._cols(conn, working_table)}
        alloc_cols_refund = {c.upper() for c in rne._cols(conn, alloc_table)}

        # (1) Refund each grid's *_REQ_REM. Grids whose grain columns don't
        # exist on alloc_table are skipped (they'd never have been touched
        # by the waterfall either).
        for req_col, meta in grids_for_refund.items():
            req_rem = meta["req_rem"]
            extras  = meta.get("extras", []) or []
            if req_col.upper() not in work_cols_refund:
                continue
            if req_rem.upper() not in work_cols_refund:
                continue
            grain_cols = ["WERKS", "MAJ_CAT"] + list(extras)
            if not all(c.upper() in alloc_cols_refund for c in grain_cols):
                continue
            grp_sql = ", ".join(f"[{c}]" for c in grain_cols)
            join_sql = " AND ".join(
                f"ISNULL(S.[{c}], '') = ISNULL(W.[{c}], '')" if c == "CLR"
                else f"S.[{c}] = W.[{c}]"
                for c in grain_cols
            )
            run_sql(conn, f"""
                UPDATE W
                SET W.[{req_rem}] = CASE
                    WHEN ISNULL(W.[{req_col}], 0) - ISNULL(S.shipped, 0) > 0
                    THEN ISNULL(W.[{req_col}], 0) - ISNULL(S.shipped, 0)
                    ELSE 0 END
                FROM [{working_table}] W
                LEFT JOIN (
                    SELECT {grp_sql},
                           SUM(ISNULL(SHIP_QTY, 0)) AS shipped
                    FROM [{alloc_table}]
                    GROUP BY {grp_sql}
                ) S ON {join_sql}
                WHERE ISNULL(W.[LISTED_FLAG], 0) = 1
            """)

        # (2) Recompute H_<grid>_REM = 1 iff *_REQ_REM > 0.5×ACS_D AND GH=1.
        # Same formula as _revalidate_after_band step (3) so values stay
        # consistent across the pipeline.
        h_rem_sets = []
        pri_h, pri_gh = [], []
        for req_col, meta in grids_for_refund.items():
            h_rem  = meta["h_rem"]
            gh_col = meta["gh_col"]
            req_rem = meta["req_rem"]
            if (h_rem.upper() in work_cols_refund
                    and req_rem.upper() in work_cols_refund
                    and gh_col.upper() in work_cols_refund):
                h_rem_sets.append(
                    f"[{h_rem}] = CASE "
                    f"WHEN ISNULL([{req_rem}],0) > {rne.ACS_SKIP_FACTOR} * ISNULL(ACS_D,0) "
                    f"AND ISNULL([{gh_col}],0) = 1 THEN 1 ELSE 0 END"
                )
                pri_h.append(h_rem)
                pri_gh.append(gh_col)
        if h_rem_sets:
            run_sql(conn, f"""
                UPDATE [{working_table}] SET {', '.join(h_rem_sets)}
                WHERE ISNULL(LISTED_FLAG, 0) = 1
            """)

        # (3) Recompute PRI_CT_REM = Σ(H_<grid>_REM) / Σ(GH_<grid>) × 100.
        if pri_h and pri_gh and "PRI_CT_REM" in work_cols_refund:
            h_sum  = " + ".join(f"ISNULL([{c}],0)" for c in pri_h)
            gh_sum = " + ".join(f"ISNULL([{c}],0)" for c in pri_gh)
            run_sql(conn, f"""
                UPDATE [{working_table}] SET
                    PRI_CT_REM = CASE
                        WHEN ({gh_sum}) = 0 THEN 0
                        ELSE ROUND(CAST(({h_sum}) AS FLOAT) / ({gh_sum}) * 100, 1) END
                WHERE ISNULL(LISTED_FLAG, 0) = 1
            """)

        # Per-row reason classification before Stage D rolls up to the
        # listing working table.
        rne._classify_alloc_reason(conn, alloc_table)

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

        # ── FS-04 (FSD_FRESH_GRT_HOLD_CONTROL): ST_STATUS + SEG lookups ──
        # Needed by the FS-06 hold-suppression mask. LEFT-join semantics via
        # .map (one value per key → can never multiply alloc rows). Missing /
        # NULL → '' so a row is never suppressed on a NULL attribute (E-04).
        _st_map: Dict[str, str] = {}
        try:
            _st_rows = conn.execute(text(
                "SELECT DISTINCT ST_CD, ISNULL(ST_STATUS,'') AS ST_STATUS "
                "FROM Master_ALC_INPUT_ST_MASTER"
            )).fetchall()
            _st_map = {str(r[0]).strip(): str(r[1] or '').strip() for r in _st_rows}
        except Exception as _e:
            logger.warning(
                f"[C-pd] ST_STATUS lookup failed ({_e}) — "
                f"UPC hold suppression inert this run"
            )
        _seg_map: Dict[str, str] = {}
        try:
            _seg_rows = conn.execute(text(
                "SELECT DISTINCT MAJ_CAT, SEG FROM vw_master_product "
                "WHERE SEG IS NOT NULL"
            )).fetchall()
        except Exception:
            # Fallback source per FS-04 — same MAJ_CAT→SEG mapping, wider view.
            try:
                _seg_rows = conn.execute(text(
                    "SELECT DISTINCT MAJ_CAT, SEG FROM VW_ET_MSA_STK_WITH_MASTER "
                    "WHERE SEG IS NOT NULL"
                )).fetchall()
            except Exception as _e2:
                _seg_rows = []
                logger.warning(
                    f"[C-pd] SEG lookup failed ({_e2}) — "
                    f"SEG hold suppression inert this run"
                )
        # Deterministic: if a MAJ_CAT ever carried two SEGs, the first after
        # sort wins (setdefault keeps it) instead of run-order roulette.
        for _mc_k, _sg_v in sorted(
            (str(r[0]).strip(), str(r[1] or '').strip()) for r in _seg_rows
        ):
            _seg_map.setdefault(_mc_k, _sg_v)

    # FS-04 merge onto the alloc frame BEFORE type coercion: ST_STATUS on
    # WERKS == ST_CD, SEG on MAJ_CAT. Extra columns are inert downstream —
    # _write_back_alloc filters on _ALLOC_WRITE_COLS.
    if 'WERKS' in alloc_df.columns:
        alloc_df['ST_STATUS'] = (
            alloc_df['WERKS'].astype(str).str.strip().map(_st_map).fillna('')
        )
    else:
        alloc_df['ST_STATUS'] = ''
    if 'MAJ_CAT' in alloc_df.columns:
        alloc_df['SEG'] = (
            alloc_df['MAJ_CAT'].astype(str).str.strip().map(_seg_map).fillna('')
        )
    else:
        alloc_df['SEG'] = ''

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
    # ALLOC_REMARKS is appended to by the per-band audit-trail in _run_band.
    # The DB column is added lazily by _ensure_alloc_remarks_col during finalise,
    # so on a fresh ARS_ALLOC_WORKING it may not exist on the SELECT * read above.
    # Materialise unconditionally here so the audit writes never KeyError.
    if 'ALLOC_REMARKS' not in alloc_df.columns:
        alloc_df['ALLOC_REMARKS'] = ''
    else:
        alloc_df['ALLOC_REMARKS'] = alloc_df['ALLOC_REMARKS'].fillna('').astype(str)
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
    """Return the working_table columns we need to load — base + per-grid.

    Also includes every `<prefix>_MBQ_ORIG`, `<prefix>_MBQ`, and
    `<prefix>_STK_TTL` column that exists on the working table. These are
    needed by the per-OPT sec-cap state-builder so it can compute
    `budget = max(0, MBQ_ORIG × cap% − STK_TTL)` per grid grain. Loading
    them unconditionally is cheap (~20 extra float columns) and avoids a
    silent "grid not materialised" fallback when sec-cap specs reference
    a grid the legacy loader didn't include.
    """
    base = [
        'WERKS', 'MAJ_CAT', 'GEN_ART_NUMBER', 'CLR',
        'OPT_TYPE', 'OPT_PRIORITY_RANK', 'LISTED_FLAG',
        'ALLOC_STATUS', 'ALLOC_REMARKS', 'ACS_D',
        'MSA_FNL_Q_REM', 'PRI_CT_REM',
        # MAJ_CAT-level store aggregates — used by the MBQ cap in _run_band
        'MJ_MBQ', 'MJ_STK_TTL', 'MJ_REQ', 'MJ_REQ_REM',
    ]
    existing_all = rne._cols(conn, working_table)
    existing = {c.upper() for c in existing_all}
    cols = [c for c in base if c.upper() in existing]
    for meta in grids.values():
        for col in (meta['req_rem'], meta['h_rem'], meta['gh_col']):
            if col.upper() in existing and col not in cols:
                cols.append(col)
        for ex in meta.get('extras', []):
            if ex.upper() in existing and ex not in cols:
                cols.append(ex)
    # Per-grid MBQ / STK_TTL / GH columns — needed by the per-OPT sec-cap
    # state-builder. Pull every column matching the known suffix patterns so
    # we don't have to know each grid's prefix up front. Also pull the
    # extras (hierarchy) column itself — derived from any `<prefix>_MBQ_ORIG`
    # column whose <prefix> is also a column on the working table (these are
    # secondary-grid extras like M_YARN_02, FIT, RNG_SEG, etc., not loaded
    # by the legacy base list).
    existing_set = set(existing_all)
    for c in existing_all:
        cu = c.upper()
        if (cu.endswith('_MBQ_ORIG') or cu.endswith('_STK_TTL')
                or cu.endswith('_MBQ') or cu.startswith('GH_')):
            if c not in cols:
                cols.append(c)
        if cu.endswith('_MBQ_ORIG'):
            prefix = c[:-len('_MBQ_ORIG')]
            # Strip 'MJ_' if present? No — Secondary grid prefixes like
            # 'M_YARN_02' don't have it; MJ has MJ_MBQ_ORIG → prefix='MJ',
            # which isn't a hierarchy column. Only add prefix as an extras
            # column when it exists ON the working table — that filters
            # out non-extras prefixes like 'MJ'/'MERGE_RNG_SEG'.
            if prefix in existing_set and prefix not in cols:
                cols.append(prefix)
    return cols


# ---------------------------------------------------------------------------
# Per-MAJ_CAT waterfall (pandas)
# ---------------------------------------------------------------------------
def _live_mbq_budget(working_df: pd.DataFrame, cap_pct: float) -> Dict[str, float]:
    """Per-WERKS cap rebuilt from the LIVE MJ_REQ_REM at the moment of call.
    Use this at the start of every band so the cap reflects everything that
    has already shipped — across all OPT_TYPEs and rounds — without having
    to re-derive from MJ_MBQ - MJ_STK_TTL - cum_ships.

    Math:  budget = max(0, MJ_REQ_REM + ((cap_pct - 100) / 100) × MJ_MBQ_ORIG)
      • cap_pct = 100 (PRI strict) → budget = MJ_REQ_REM
      • cap_pct = 130            → budget = MJ_REQ_REM + 30% × MJ_MBQ_ORIG
    Decision 4-B: headroom is computed off ORIG so the cap is anchored to the
    pre-growth budget. Empty dict (cap disabled) when cap_pct ≤ 0 or required
    columns missing."""
    if cap_pct <= 0:
        return {}
    needed = {'WERKS', 'MJ_REQ_REM', 'MJ_MBQ'}
    if not needed.issubset(working_df.columns):
        return {}
    # Anchor column: prefer ORIG (pre-growth), fall back to live MJ_MBQ.
    mbq_anchor = 'MJ_MBQ_ORIG' if 'MJ_MBQ_ORIG' in working_df.columns else 'MJ_MBQ'
    headroom = (cap_pct - 100.0) / 100.0
    store_data = (
        working_df[['WERKS', 'MJ_REQ_REM', mbq_anchor]]
        .sort_values(['WERKS', 'MJ_REQ_REM', mbq_anchor], kind='mergesort')
        .drop_duplicates(subset=['WERKS'])
    )
    budget: Dict[str, float] = {}
    for _, row in store_data.iterrows():
        rem = float(row['MJ_REQ_REM'] or 0)
        mbq = float(row[mbq_anchor] or 0)
        budget[str(row['WERKS'])] = max(0.0, rem + headroom * mbq)
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
    tbl_mbq_cap_pct: float = 0.0,
    hold_dict: Optional[Dict[Tuple, float]] = None,
    size_threshold: float = 0.6,
    min_size_count: int = 3,
    opt_types: Optional[List[str]] = None,
    # Per-OPT engine only: drives the TBL MJ_REQ_GATE pre-check (Fix B).
    # tbl_mj_req_cap_pct mirrors the post-loop _stage_c_apply_opt_mj_req_gate
    # `tbl_cap_pct` so the pre-check and post-loop use the same threshold.
    tbl_mj_req_cap_pct: float = 100.0,
    # Per-OPT engine only: list of (grid_name, meta) tuples describing every
    # grid that participates in sec-cap (sec_cap_applicable=1 + MJ when
    # include_primary=True). Built once at the wrapper level via DB conn.
    # None or empty disables per-OPT sec-cap (legacy post-pass still runs).
    sec_cap_grid_specs: Optional[List[Tuple[str, Dict[str, Any]]]] = None,
    # Per-OPT engine only: dispatch mode for RL and TBC when an OPT's total
    # need exceeds its werks_cap.
    #   SCALED   — proportional round-then-shave, partial OPT ships.
    #   COMPLETE — all-or-skip; budget passes to the next OPT in band.
    # Defaults match the new run-config defaults; sequential mode ignores
    # these (uses SQL-side floor logic).
    rl_dispatch_mode: str = 'COMPLETE',
    tbc_dispatch_mode: str = 'COMPLETE',
    # Sec-cap growth matrix (spec 2026-07-08). Off (default) is byte-identical
    # to pre-matrix behaviour.
    matrix_enabled: bool = False,
    matrix_bands: Optional[List[Tuple[float, Optional[float], float]]] = None,
    # Fresh/GRT hold control (FSD_FRESH_GRT_HOLD_CONTROL). alloc_type is
    # informational at this level — the typed hold load already consumed it
    # upstream (hold_dict arrives pre-filtered); the three flags feed the
    # FS-06 hold-suppression mask in _run_band / _run_band_per_opt.
    alloc_type: str = "FRESH",
    skip_hold_upc: bool = False,
    apply_hold_seg_app: bool = True,
    apply_hold_seg_gm: bool = True,
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

    # Per-OPT MJ-level cap PCT. PRI>=100 strict pins the cap to 100%; otherwise
    # the UI-supplied % is used. TBL has no MJ-cap (removed 2026-05-16) — its
    # only ceiling is per-size SZ_REQ, so cap_pct is forced to 0 below to skip
    # the budget check entirely for TBL. The actual budget DICTS are rebuilt
    # fresh at the start of every band from the live working_df['MJ_REQ_REM']
    # (see _live_mbq_budget). MJ_REQ_REM is decremented after every band by
    # _revalidate_after_band, so deriving the budget from it gives a single
    # source of truth across opt_types + rounds — no stale static budget,
    # no double-deduction of prior ships.
    eff_rl_cap  = 100.0 if pri_ct_check_rl  else rl_mbq_cap_pct
    eff_tbc_cap = 100.0 if pri_ct_check_tbc else tbc_mbq_cap_pct
    eff_tbl_cap = tbl_mbq_cap_pct

    # Initial snapshot: all rows get the pre-waterfall pool value so that
    # SKIPPED rows (which never enter elig_mask) always show a meaningful
    # FNL_Q_REM rather than NULL.
    _snapshot_fnl_q_rem(alloc_df, pool_dict)

    # per_opt is the only band engine (2026-07-10 removal) — no mode switch.
    logger.info(
        "[C-pd] per-OPT engine (rule_engine_per_opt._run_band_per_opt) "
        f"tbl_mj_req_cap_pct={tbl_mj_req_cap_pct}"
    )

    # Build per-OPT sec-cap state ONCE for this MAJ_CAT slice. Mutates its
    # `running` dict in place as each OPT ships across bands/rounds. None
    # when sec_cap_grid_specs was not passed (legacy callers / spec-build
    # failure; post-pass gate still runs in that case).
    sec_cap_state = None
    if sec_cap_grid_specs:
        try:
            from app.services.rule_engine_per_opt import build_sec_cap_state
            sec_cap_state = build_sec_cap_state(
                working_df, sec_cap_grid_specs,
                matrix_enabled=bool(matrix_enabled),
                matrix_bands=list(matrix_bands or []),
            )
            n_grids = sum(1 for g in sec_cap_state.get("grids", []) or [])
            logger.info(
                f"[C-pd] per-OPT sec-cap state built — {n_grids} grids "
                f"participating; pool stays live for blocked OPTs"
            )
        except Exception as _scerr:
            logger.warning(
                f"[C-pd] per-OPT sec-cap state build failed: {_scerr} — "
                f"falling back to post-pass gate"
            )
            sec_cap_state = None

    # Per-WERKS MJ_REQ_REM dict for the TBL MJ_REQ_GATE pre-check. Lives
    # across the whole MAJ_CAT loop but is RE-SEEDED from the live
    # working_df at the top of every band (see `_rebuild_mj_req_rem_dict`
    # below) so it picks up the post-RL/TBC decrement that
    # `_revalidate_after_band` writes back to working_df['MJ_REQ_REM'].
    #
    # Without that refresh the TBL band would see the pre-RL budget and
    # admit more TBL OPTs than the remaining budget can cover — the
    # HM30 × M_W_SHIRT_FS overshoot was caused exactly by this staleness.
    mj_req_rem_dict: Dict[str, float] = {}

    def _rebuild_mj_req_rem_dict() -> None:
        nonlocal mj_req_rem_dict
        if working_df is None or 'MJ_REQ_REM' not in working_df.columns:
            mj_req_rem_dict = {}
            return
        try:
            _mw = working_df[['WERKS', 'MJ_REQ_REM']].copy()
            _mw['MJ_REQ_REM'] = pd.to_numeric(_mw['MJ_REQ_REM'], errors='coerce').fillna(0.0)
            # One value per WERKS (working_df has one row per OPT, but
            # MJ_REQ_REM is a MAJ_CAT-level aggregate so all rows for a
            # WERKS share the same value — first wins).
            mj_req_rem_dict = {
                str(k): float(v) for k, v in (
                    _mw.drop_duplicates(subset=['WERKS'])
                       .set_index('WERKS')['MJ_REQ_REM']
                       .astype(float)
                       .to_dict()
                ).items()
            }
        except Exception as _err:
            logger.warning(
                f"[C-per-opt] failed to (re)build mj_req_rem_dict ({_err}) "
                f"— TBL MJ_REQ_GATE pre-check disabled this run"
            )
            mj_req_rem_dict = {}

    _rebuild_mj_req_rem_dict()

    active_ot = [ot for ot in OPT_TYPE_ORDER if not opt_types or ot in opt_types]
    for i, ot in enumerate(active_ot):
        ot_mask = (alloc_df['OPT_TYPE'] == ot)
        if not ot_mask.any():
            continue
        max_round = int(alloc_df.loc[ot_mask, 'I_ROD'].max() or 0)
        if max_round == 0:
            continue

        cap_pct_for_ot = (
            eff_rl_cap  if ot == 'RL'
            else eff_tbc_cap if ot == 'TBC'
            else eff_tbl_cap
        )

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

            # NO pre-band FNL_Q_REM snapshot here: the per-OPT engine writes a
            # live POST-draw value at each OPT's turn (rule_engine_per_opt
            # step 5f.1) and that is the value the operator must see in
            # history. A band-wide pre-band snapshot would overwrite prior
            # rounds' per-OPT writes with a uniform pool value. (The initial
            # pre-waterfall snapshot above still seeds SKIPPED rows.)

            # Rebuild the per-WERKS cap from the live MJ_REQ_REM at the start
            # of every band — captures all ships from prior opt_types AND prior
            # rounds of this opt_type in one read.
            mbq_budget = (
                _live_mbq_budget(working_df, cap_pct_for_ot)
                if cap_pct_for_ot > 0 and has_working else {}
            )

            # Round N finishes for ALL stores before round N+1 starts.
            # Cross-type eligibility (R06: MJ_REQ_REM < 0.5×ACS_D) is evaluated
            # by _pre_band_check before the first round of each OPT_TYPE.
            #
            # Sequential per-OPT engine (the only band since 2026-07-10):
            # pre-validates every gate at each OPT's turn against the LIVE
            # pool, no post-loop refunds, honest SKIP_REASONs. See
            # rule_engine_per_opt._run_band_per_opt.
            #
            # Re-seed mj_req_rem_dict from working_df so the TBL gate sees
            # the latest remainder (RL/TBC ships of prior bands have already
            # been written back by the previous `_revalidate_after_band`).
            _rebuild_mj_req_rem_dict()
            from app.services.rule_engine_per_opt import _run_band_per_opt
            _run_band_per_opt(alloc_df, pool_dict, ot, int(r),
                              mbq_budget=mbq_budget,
                              hold_dict=hold_dict,
                              size_threshold=size_threshold,
                              min_size_count=min_size_count,
                              mj_req_rem_dict=mj_req_rem_dict,
                              tbl_mj_req_cap_pct=tbl_mj_req_cap_pct,
                              sec_cap_state=sec_cap_state,
                              rl_dispatch_mode=rl_dispatch_mode,
                              tbc_dispatch_mode=tbc_dispatch_mode,
                              alloc_type=alloc_type,
                              skip_hold_upc=skip_hold_upc,
                              apply_hold_seg_app=apply_hold_seg_app,
                              apply_hold_seg_gm=apply_hold_seg_gm)

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


# _run_band (the pandas cumulative-window band) was removed 2026-07-10 —
# per_opt (rule_engine_per_opt._run_band_per_opt) is the only band engine.
# See app/docs/REMOVAL_PLAN_PER_OPT_ONLY.md.


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

    # (1) Reduce MSA_FNL_Q_REM per OPT (WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR).
    # Note: OPT_KEYS already includes WERKS, so this groupby is naturally
    # per-store. The `_ts` / `_hs` aliases below are per-store ship/hold
    # sums for the OPT — renamed from terse `_t` / `_h` for clarity. The
    # groupby keys and the dict produced here are unchanged from before;
    # only the column names within `opt_take` are different.
    if 'MSA_FNL_Q_REM' in work_cols:
        opt_take = (
            band.groupby(OPT_KEYS, sort=False, observed=True, dropna=False)
                .agg(_ts=('ROUND_SHIP', 'sum'), _hs=('ROUND_HOLD', 'sum'))
        )
        opt_take['_total'] = opt_take['_ts'] + opt_take['_hs']
        opt_take_active = opt_take[opt_take['_total'] > 0]
        if not opt_take_active.empty:
            opt_dict = opt_take_active['_total'].to_dict()
            keys = pd.Series(
                list(zip(*[working_df[c].to_numpy() for c in OPT_KEYS])),
                index=working_df.index,
            )
            decrement = keys.map(opt_dict).fillna(0).astype('float64')
            prev_msa = working_df['MSA_FNL_Q_REM'].fillna(0).to_numpy()
            new_msa = prev_msa - decrement.to_numpy()
            new_msa = np.maximum(new_msa, 0.0)
            working_df['MSA_FNL_Q_REM'] = new_msa

            # Audit-trail (Option A) at OPT grain: append the per-band
            # ship/hold and MSA_REM before→after to working_df.ALLOC_REMARKS
            # for every OPT that actually consumed pool. The reviewer can
            # read the full lifecycle of an OPT directly on the listing
            # working row (initial vs revised side-by-side via this trace).
            moved_w = decrement.to_numpy() > 0
            if moved_w.any():
                idx_w = working_df.index[moved_w]
                ship_map = opt_take_active['_ts'].to_dict()
                hold_map = opt_take_active['_hs'].to_dict()
                sh_w = keys.loc[idx_w].map(ship_map).fillna(0).astype('float64').to_numpy()
                hl_w = keys.loc[idx_w].map(hold_map).fillna(0).astype('float64').to_numpy()
                rk_w = (
                    working_df.loc[idx_w, 'OPT_PRIORITY_RANK']
                    .fillna(0).astype(int).astype(str).values
                )
                prev_w = (
                    working_df.loc[idx_w, 'ALLOC_REMARKS']
                    .fillna('').astype(str).values
                )
                trace_w = (
                    ' B[' + ot + '.r' + str(int(r)) + '.rk' + rk_w
                    + '] ship='
                    + np.round(sh_w, 0).astype(int).astype(str)
                    + ' hold='
                    + np.round(hl_w, 0).astype(int).astype(str)
                    + ' msa='
                    + np.round(prev_msa[moved_w], 0).astype(int).astype(str)
                    + '->'
                    + np.round(new_msa[moved_w], 0).astype(int).astype(str)
                    + ';'
                )
                working_df.loc[idx_w, 'ALLOC_REMARKS'] = prev_w + trace_w

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
        mj_dead = (
            (working_df['MJ_REQ_REM'].fillna(0) <= 0)
            if 'MJ_REQ_REM' in work_cols else
            pd.Series(False, index=working_df.index)
        )
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
        # MJ_EXHAUSTED — MAJ_CAT-level requirement satisfied in this round, so
        # subsequent rounds for any OPT at the same (WERKS, MAJ_CAT) should not
        # fire. Without this gate, round 2 would over-ship past MJ_REQ since the
        # per-row need_ship (r×SZ_MBQ−SZ_STK) is computed independently of the
        # MAJ_CAT-level cumulative ship.
        m_mj = pending_mask & mj_dead & (~msa_dead)
        if m_mj.any():
            working_df.loc[m_mj, 'ALLOC_STATUS'] = 'SKIPPED'
            mj_vals = working_df.loc[m_mj, 'MJ_REQ_REM'].fillna(0)
            suffix = mj_vals.apply(lambda v: f' SKIP_MJ_EXHAUSTED(mj_rem={v:.1f});')
            working_df.loc[m_mj, 'ALLOC_REMARKS'] = (
                working_df.loc[m_mj, 'ALLOC_REMARKS'].fillna('').astype(str) + suffix
            )
        # PRI_BROKEN — include PRI_CT_REM value
        m_pri = pending_mask & pri_dead & (~msa_dead) & (~mj_dead)
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
        # PARTIAL rows MUST be re-marked SKIPPED here: working_df only enters
        # newly_skipped via MJ_EXHAUSTED / PRI_BROKEN / STORE_BROKEN, all of
        # which are hard stops for the OPT.  Leaving PARTIAL alloc rows alone
        # lets _run_band re-admit them in round r+1 (its gate only excludes
        # SKIPPED/INELIGIBLE), producing ship past MJ_REQ.
        next_alloc_mask = alloc_df['I_ROD'] >= r + 1
        if next_alloc_mask.any():
            sub = alloc_df.loc[next_alloc_mask]
            alloc_keys_next = list(zip(*[sub[c].to_numpy() for c in OPT_KEYS]))
            m_in_next = np.array([k in remarks_map for k in alloc_keys_next], dtype=bool)
            prop_idx = sub.index[
                m_in_next
                & (~sub['ALLOC_STATUS'].isin(['SKIPPED', 'ALLOCATED'])).to_numpy()
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
    'ALLOC_STATUS', 'SKIP_REASON', 'ALLOC_REMARKS',
    'POOL_CONSUMED', 'ALLOC_WAVE', 'ALLOC_ROUND',
    'FNL_Q_REM',
]

_SHORT_TEXT_BUFFER = 1000  # default cap for key/status columns
_TEXT_HEADROOM     = 200   # safety margin above observed max length
_TEXT_MIN_BUFFER   = 500   # never bind smaller than this
# SQL_WVARCHAR maxes out at 4000 chars in MS SQL. Past that we must
# escalate to SQL_WLONGVARCHAR (NVARCHAR(MAX) / LOB binding).
_WVARCHAR_CAP      = 4000


def _bind_string_buffers(cur, cols, out_df, numeric_cols):
    """Explicitly size pyodbc string parameter buffers for fast_executemany.

    Why this exists: when fast_executemany targets a TEMP table, pyodbc's
    SQLDescribeParam can't read the temp-column metadata and falls back to
    a 255-char buffer for every string parameter. The first long band-trace
    raises 'String data, right truncation: length X buffer 510' even when
    the temp column is NVARCHAR(MAX). setinputsizes() bypasses the fallback
    by stating the buffer width up front, per-parameter.

    Sizing strategy — buffer = max(observed_len + headroom, MIN), then:
      - <= 4000 chars → SQL_WVARCHAR(buffer)
      - >  4000 chars → SQL_WLONGVARCHAR(buffer)   (LOB)
    pyodbc.fast_executemany pre-allocates buffer × num_rows for each
    parameter, so binding the type's theoretical max (~1 GB) would
    request tens of TB and raise MemoryError. Sizing to actual data
    keeps the allocation bounded.

    Every slot must be a valid (type, precision, scale) tuple — ODBC
    Driver 18 rejects `None` with HY104 ('Invalid precision value (0)'),
    so numeric params get an explicit SQL_DOUBLE spec rather than a
    default.
    """
    import pyodbc as _pyodbc
    sizes = []
    for c in cols:
        if c in numeric_cols:
            # ODBC SQL_DOUBLE column-size is 15 (decimal digits), NOT 53
            # (that's SQL_FLOAT's bit-precision). Driver 18 rejects 53
            # with HY104 'Invalid precision value'.
            sizes.append((_pyodbc.SQL_DOUBLE, 15, 0))
            continue

        # Probe actual max char length in this batch, with a NULL-safe
        # fallback when the column is absent or entirely null.
        try:
            ser = out_df[c]
            observed = int(ser.dropna().astype(str).str.len().max() or 0)
        except Exception:
            observed = 0
        buf = max(observed + _TEXT_HEADROOM, _TEXT_MIN_BUFFER, _SHORT_TEXT_BUFFER)

        if buf <= _WVARCHAR_CAP:
            sizes.append((_pyodbc.SQL_WVARCHAR, buf, 0))
        else:
            # Past the WVARCHAR cap: must use the LOB type. pyodbc will
            # pre-allocate `buf * row_count` per param — keep `buf` close
            # to actual need rather than the type's theoretical max.
            sizes.append((_pyodbc.SQL_WLONGVARCHAR, buf, 0))
    try:
        cur.setinputsizes(sizes)
    except Exception as e:
        # Old pyodbc versions or some drivers may reject setinputsizes —
        # log and continue; fast_executemany will use its fallback buffer.
        logger.warning(f"[pandas] setinputsizes failed: {e}")


# Matches the band-trace tokens written by _run_band in
# ALLOC_REMARKS, e.g. " B[TBL.r1.rk1] sh=5 hld=1 pool=382->376;".
_BAND_TRACE_RE = re.compile(r'\s*B\[[^\]]+\]\s*sh=\d+\s+hld=\d+\s+pool=\d+->\d+;')


def _apply_pak_sz_rounding_df(alloc_df: pd.DataFrame) -> pd.DataFrame:
    """In-memory PAK_SZ rounding. Half-up: SHIP_QTY rounds to the nearest
    whole pak (req >= 0.5*pak rounds UP, e.g. 5/6->6 and 11/6->12; below
    the 0.5*pak threshold the row is gated to 0 and marked SKIPPED).

    Side-effects:
      - POOL_CONSUMED is adjusted by the SHIP delta so the downstream
        FNL_Q_REM recompute refunds (or charges) the pool correctly.
      - The trailing B[..] sh=N hld=M pool=A->B; band-trace token in
        ALLOC_REMARKS is rewritten to match the post-PAK SHIP (or
        dropped entirely when the row is gated to 0), then a
        PAK_SZ_GATE/PAK_SZ_ROUND audit marker is appended."""
    if 'PAK_SZ' not in alloc_df.columns:
        return alloc_df
    if 'SHIP_QTY' not in alloc_df.columns:
        return alloc_df
    nonzero = alloc_df['SHIP_QTY'].fillna(0) > 0
    # Per-OPT engine stamps PAK_SZ_GATE / PAK_SZ_ROUND markers in
    # ALLOC_REMARKS at allocation time. Those rows are already in their
    # final form (including stock-clipped non-pak-aligned SHIP values),
    # so the in-memory safety-net here must leave them untouched. Rows
    # produced by the cumulative-window _run_band path do not carry the
    # marker and continue to be rounded as before.
    if 'ALLOC_REMARKS' in alloc_df.columns:
        marker_mask = (
            alloc_df['ALLOC_REMARKS']
            .fillna('')
            .astype(str)
            .str.contains('PAK_SZ_', regex=False)
        )
        nonzero = nonzero & (~marker_mask)
    if not nonzero.any():
        return alloc_df
    pak_v = (
        alloc_df.loc[nonzero, 'PAK_SZ']
        .fillna(0).replace(0, 1).astype(float).values
    )
    old_v = alloc_df.loc[nonzero, 'SHIP_QTY'].fillna(0).astype(float).values
    # Half-up rounding: floor((req + 0.5*pak) / pak) * pak — anything at
    # or above the 0.5*pak threshold rounds UP to the next whole pak.
    rounded = (np.floor((old_v + 0.5 * pak_v) / pak_v) * pak_v).astype(int)
    gate = old_v < 0.5 * pak_v
    new_v = np.where(gate, 0, rounded).astype(int)
    idx = alloc_df.index[nonzero]
    delta = old_v.astype(int) - new_v  # +ve = freed units, -ve = drew more
    alloc_df.loc[idx, 'SHIP_QTY'] = new_v
    # POOL_CONSUMED mirrors SHIP+HOLD draws; only SHIP changed here so
    # subtract the delta. Clamp to zero in case prior accounting is off.
    if 'POOL_CONSUMED' in alloc_df.columns:
        prev_pc = (
            alloc_df.loc[idx, 'POOL_CONSUMED']
            .fillna(0).astype(float).values
        )
        alloc_df.loc[idx, 'POOL_CONSUMED'] = np.maximum(
            prev_pc - delta.astype(float), 0.0
        )
    # Mark gated rows (req < 0.5*pak -> SHIP=0, status=SKIPPED)
    gated_idx = idx[gate]
    if len(gated_idx) > 0:
        if 'ALLOC_STATUS' in alloc_df.columns:
            alloc_df.loc[gated_idx, 'ALLOC_STATUS'] = 'SKIPPED'
        if 'SKIP_REASON' in alloc_df.columns:
            alloc_df.loc[gated_idx, 'SKIP_REASON'] = 'PAK_SZ_BELOW_HALF'
    # Rewrite the trailing band-trace token + append PAK audit marker.
    _rewrite_remarks_after_pak(
        alloc_df, idx, old_v.astype(int), new_v.astype(int),
        pak_v.astype(int), gate
    )
    return alloc_df


def _rewrite_remarks_after_pak(alloc_df, idx, old_v, new_v, pak_v, gate):
    """Update ALLOC_REMARKS to reflect the post-PAK SHIP. For the LAST
    `B[..] sh=N hld=M pool=A->B;` token in the string:
      - if SHIP became 0  -> drop the token entirely
      - if SHIP changed   -> rewrite `sh=N` to the new value
    Then append `PAK_SZ_GATE(...)` (zeroed rows) or `PAK_SZ_ROUND(...)`
    (resized rows) — same format as the SQL safety-net stage."""
    if 'ALLOC_REMARKS' not in alloc_df.columns:
        return
    prev = alloc_df.loc[idx, 'ALLOC_REMARKS'].fillna('').astype(str).values
    out = prev.copy()
    for i in range(len(idx)):
        old_s = int(old_v[i])
        new_s = int(new_v[i])
        if old_s == new_s:
            continue
        s = out[i]
        matches = list(_BAND_TRACE_RE.finditer(s))
        if matches:
            m = matches[-1]
            if new_s == 0:
                s = s[:m.start()] + s[m.end():]
            else:
                token = m.group(0)
                token = re.sub(r'sh=\d+', f'sh={new_s}', token, count=1)
                s = s[:m.start()] + token + s[m.end():]
        marker = (
            f' PAK_SZ_GATE(req={old_s},pak={int(pak_v[i])});'
            if new_s == 0
            else f' PAK_SZ_ROUND(from={old_s},to={new_s},pak={int(pak_v[i])});'
        )
        out[i] = s + marker
    alloc_df.loc[idx, 'ALLOC_REMARKS'] = out


def _write_back_alloc(engine, alloc_table: str, df: pd.DataFrame) -> None:
    """Bulk-MERGE the updated alloc rows back into alloc_table."""
    _apply_pak_sz_rounding_df(df)
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
            elif c in {'ALLOC_REMARKS', 'SKIP_REASON'}:
                # Band-trace accumulates across many rounds × ranks — must
                # not be truncated. Mirrors the persisted column types.
                # COLLATE DATABASE_DEFAULT pins the temp column to the
                # user-DB collation (Latin1_General_CI_AS), not tempdb's
                # default (SQL_Latin1_General_CP1_CI_AS), so the JOIN below
                # doesn't hit error 468.
                col_defs.append(f"[{c}] NVARCHAR(MAX) COLLATE DATABASE_DEFAULT NULL")
            else:
                col_defs.append(f"[{c}] NVARCHAR(200) COLLATE DATABASE_DEFAULT NULL")
        cur.execute(f"CREATE TABLE {tmp} ({', '.join(col_defs)})")

        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(f"[{c}]" for c in cols)
        rows = [
            tuple(None if (isinstance(v, float) and np.isnan(v)) else v for v in row)
            for row in out.itertuples(index=False, name=None)
        ]
        _bind_string_buffers(cur, cols, out, numeric_cols={
            'SHIP_QTY', 'HOLD_QTY', 'ALLOC_QTY', 'FROM_HOLD_QTY',
            'POOL_CONSUMED', 'ALLOC_ROUND', 'FNL_Q_REM',
        })
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
            if c == 'ALLOC_REMARKS':
                # Band-trace accumulates across many rounds × ranks — must
                # not be truncated. pyodbc fast_executemany against a temp
                # table falls back to a 255-char (510-byte) parameter buffer
                # when SQLDescribeParam can't read column metadata, so any
                # NVARCHAR(<N>) wider than 255 still raises "String data,
                # right truncation: length X buffer 510". NVARCHAR(MAX) is
                # bound as SQL_WLONGVARCHAR and bypasses the cap. Mirrors
                # the persisted column + the fix in _write_back_alloc.
                col_defs.append(f"[{c}] NVARCHAR(MAX) COLLATE DATABASE_DEFAULT NULL")
            elif c in OPT_KEYS or c == 'ALLOC_STATUS':
                # See _write_back_alloc — pin to DB collation to avoid
                # error 468 on the JOIN against the persisted table.
                col_defs.append(f"[{c}] NVARCHAR(400) COLLATE DATABASE_DEFAULT NULL")
            else:
                col_defs.append(f"[{c}] FLOAT NULL")
        cur.execute(f"CREATE TABLE {tmp} ({', '.join(col_defs)})")

        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(f"[{c}]" for c in cols)
        rows = [
            tuple(None if (isinstance(v, float) and np.isnan(v)) else v for v in row)
            for row in out.itertuples(index=False, name=None)
        ]
        # _write_back_working col taxonomy: OPT_KEYS + ALLOC_STATUS/REMARKS
        # are strings; everything else (MSA_FNL_Q_REM, PRI_CT_REM, grid
        # _REM cols) is numeric.
        _bind_string_buffers(cur, cols, out, numeric_cols={
            c for c in cols if c not in OPT_KEYS
            and c not in ('ALLOC_STATUS', 'ALLOC_REMARKS')
        })
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
