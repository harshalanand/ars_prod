"""
Auto Cont % — SQL-direct contribution pipeline
==============================================
Phase 1: synchronous single-preset run via sp_AutoContCompute (done).
Phase 2: background job queue + multi-preset orchestration (this file).
Phase 3: horizontal combine in SQL (dynamic LEFT JOIN on master keys).
Phase 4: mapping-assignment UPDATEs in SQL.

Tables (Rep_Data):
  Cont_presets             – shared with pandas pipeline (read-only here)
  Cont_mappings            – shared
  Cont_mapping_assignments – shared
  AutoCont_jobs            – job persistence (this file)
  AutoCont_<gc>_<preset>_<ts>      – per-preset detail (intermediate)
  AutoCont_<gc>_<preset>_CO_<ts>   – per-preset company (intermediate)
  AutoCont_FINAL_<gc>_<ts>         – combined detail (with all preset cols)
  AutoCont_FINAL_<gc>_CO_<ts>      – combined company

Endpoints:
  GET    /auto-cont/status          – proc-installed probe
  POST   /auto-cont/execute         – create job (returns job_id)
  GET    /auto-cont/jobs            – list jobs
  GET    /auto-cont/jobs/{id}       – job detail
  POST   /auto-cont/jobs/{id}/cancel
  DELETE /auto-cont/jobs/{id}
  GET    /auto-cont/tables          – list AutoCont_* output tables
  GET    /auto-cont/preview/{table} – TOP-N preview
  DELETE /auto-cont/tables/{table}  – drop
"""

import io
import json
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from typing import List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import inspect, text

from app.database.session import get_data_engine
from app.models.rbac import User
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user

router = APIRouter(prefix="/auto-cont", tags=["Auto Cont % (SQL-direct)"])

# ── Constants ───────────────────────────────────────────────────────────────
OUTPUT_PREFIX = "AutoCont"
FINAL_PREFIX  = "AutoCont_FINAL"
JOB_TABLE     = "AutoCont_jobs"

PRESET_TABLE     = "Cont_presets"
MAPPING_TABLE    = "Cont_mappings"
ASSIGNMENT_TABLE = "Cont_mapping_assignments"

VALID_GROUPING = (
    'CLR','SZ','RNG_SEG','M_VND_CD','MACRO_MVGR',
    'MICRO_MVGR','FAB','WEAVE_2','M_YARN_02',
)
VALID_KPI_TYPES = ('L30D', 'L7D', 'L18M')

# KPI columns that get a `|<preset>` suffix during combine
KPI_COLS = [
    '0001_STK_Q','0001_STK_V','FIX','DISP_AREA','GM_%','STR',
    'SALES PSF','SALE_PSF_MJ','SALES_PSF_ACH%','GM PSF','GM_PSF_MJ',
    'GM_PSF_ACH%','STOCK_CONT%','SALE_CONT%','ALGO','INITIAL AUTO CONT%',
    # raw aggregates (kept so each preset's volume is visible)
    'OP_STK_Q','OP_STK_V','CL_STK_Q','CL_STK_V','SALE_Q','SALE_V','GM_V',
    'STK_Q','STK_V',
]

# Cont_presets columns that distinguish "merge keys" from "per-preset values".
# Anything not in this set gets a `|<preset>` suffix in the combined output.
# We also dynamically add the master-hier columns when we read them.
BASE_MERGE_KEYS_DETAIL  = ['ST_CD','ST_NM','MAJ_CAT']
BASE_MERGE_KEYS_COMPANY = ['MAJ_CAT']

# ══════════════════════════════════════════════════════════════════════════════
#  Schemas
# ══════════════════════════════════════════════════════════════════════════════

class ExecutePayload(BaseModel):
    grouping_column: str = "MACRO_MVGR"
    presets: List[str] = []        # preset_names from Cont_presets; empty = all
    majcats: List[str] = []        # empty = all
    target: str = "Both"           # "Store", "Company", "Both"
    apply_mappings: bool = True    # apply Cont_mapping_assignments via UPDATE


# ══════════════════════════════════════════════════════════════════════════════
#  Job state (in-memory + DB persistence)
# ══════════════════════════════════════════════════════════════════════════════

_jobs: OrderedDict = OrderedDict()
_job_lock = threading.Lock()
_jobs_loaded = False


def _ensure_job_table(engine):
    with engine.connect() as c:
        c.execute(text(f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{JOB_TABLE}')
            CREATE TABLE {JOB_TABLE} (
                job_id        NVARCHAR(50) PRIMARY KEY,
                status        NVARCHAR(20) NOT NULL DEFAULT 'pending',
                label         NVARCHAR(500),
                payload_json  NVARCHAR(MAX),
                log_json      NVARCHAR(MAX),
                duration      FLOAT NULL,
                detail_table  NVARCHAR(300) NULL,
                company_table NVARCHAR(300) NULL,
                detail_rows   INT DEFAULT 0,
                company_rows  INT DEFAULT 0,
                error         NVARCHAR(MAX) NULL,
                created_at    DATETIME DEFAULT GETDATE(),
                finished_at   DATETIME NULL
            )
        """))
        c.commit()


def _persist_job(job: dict) -> None:
    """Save job snapshot to AutoCont_jobs. Swallows errors — never breaks the worker."""
    try:
        engine = get_data_engine()
        _ensure_job_table(engine)
        params = {
            "id":     job["id"],
            "status": job.get("status", ""),
            "label":  job.get("label", ""),
            "payload": json.dumps(job.get("payload", {})),
            "log":    json.dumps(job.get("log", [])),
            "dur":    job.get("duration"),
            "dt":     job.get("detail_table"),
            "ct":     job.get("company_table"),
            "dr":     int(job.get("detail_rows") or 0),
            "cr":     int(job.get("company_rows") or 0),
            "err":    (job.get("error") or "")[:3500],
        }
        with engine.connect() as c:
            exists = c.execute(text(f"SELECT 1 FROM {JOB_TABLE} WHERE job_id=:id"),
                               {"id": job["id"]}).fetchone()
            if exists:
                c.execute(text(f"""
                    UPDATE {JOB_TABLE}
                       SET status=:status, log_json=:log, duration=:dur,
                           detail_table=:dt, company_table=:ct,
                           detail_rows=:dr, company_rows=:cr,
                           error=:err,
                           finished_at = CASE WHEN :status IN ('completed','failed','cancelled')
                                              THEN GETDATE() ELSE finished_at END
                     WHERE job_id=:id
                """), params)
            else:
                c.execute(text(f"""
                    INSERT INTO {JOB_TABLE}
                        (job_id,status,label,payload_json,log_json,duration,
                         detail_table,company_table,detail_rows,company_rows,error)
                    VALUES
                        (:id,:status,:label,:payload,:log,:dur,
                         :dt,:ct,:dr,:cr,:err)
                """), params)
            c.commit()
    except Exception as e:
        logger.warning(f"[AutoCont] _persist_job failed (non-fatal): {e}")


def _load_persisted_jobs() -> None:
    """Hydrate _jobs from AutoCont_jobs on first request after restart."""
    try:
        engine = get_data_engine()
        _ensure_job_table(engine)
        with engine.connect() as c:
            rows = c.execute(text(f"""
                SELECT job_id, status, label, payload_json, log_json, duration,
                       detail_table, company_table, detail_rows, company_rows, error,
                       created_at, finished_at
                  FROM {JOB_TABLE}
              ORDER BY created_at DESC
            """)).fetchall()
        for r in rows:
            jid = r[0]
            if jid in _jobs:
                continue
            _jobs[jid] = {
                "id": jid, "status": r[1], "label": r[2] or "",
                "payload": json.loads(r[3]) if r[3] else {},
                "log":     json.loads(r[4]) if r[4] else [],
                "duration":      r[5],
                "detail_table":  r[6],
                "company_table": r[7],
                "detail_rows":   r[8] or 0,
                "company_rows":  r[9] or 0,
                "error":         r[10],
                "created_at":  r[11].isoformat() if r[11] else None,
                "finished_at": r[12].isoformat() if r[12] else None,
                "progress": "done" if r[1] in ("completed","failed","cancelled") else "—",
                "started_at": None,
            }
    except Exception as e:
        logger.warning(f"[AutoCont] _load_persisted_jobs failed: {e}")


def _lazy_load_jobs() -> None:
    global _jobs_loaded
    if not _jobs_loaded:
        _jobs_loaded = True
        _load_persisted_jobs()


def _update_job(job_id: str, persist: bool = False, **kwargs) -> None:
    with _job_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)
            snap = dict(_jobs[job_id])
    if persist:
        _persist_job(snap)


# ══════════════════════════════════════════════════════════════════════════════
#  SQL helpers (Phase 3 + 4)
# ══════════════════════════════════════════════════════════════════════════════

def _check_proc_installed(engine) -> bool:
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT 1 FROM sys.procedures WHERE name = 'sp_AutoContCompute'"
        )).fetchone()
    return row is not None


def _safe_name(s: str) -> str:
    keep = "_"
    return "".join(ch if (ch.isalnum() or ch in keep) else "_" for ch in (s or ""))[:60]


def _master_hier_cols(engine, gc: str) -> List[str]:
    """Columns on Master_HIER_<gc> minus excluded upload metadata."""
    table = f"Master_HIER_{gc}"
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT COLUMN_NAME
              FROM INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_NAME=:t AND TABLE_SCHEMA='dbo'
          ORDER BY ORDINAL_POSITION
        """), {"t": table}).fetchall()
    exclude = {"UPLOAD_DATETIME", "upload_datetime"}
    return [r[0] for r in rows if r[0] not in exclude]


def _table_columns(engine, tbl: str) -> List[str]:
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_NAME = :t
          ORDER BY ORDINAL_POSITION
        """), {"t": tbl}).fetchall()
    return [r[0] for r in rows]


def _drop_table(engine, tbl: str) -> None:
    safe = tbl.replace("'", "").replace(";", "")
    with engine.connect() as c:
        c.execute(text(f"IF OBJECT_ID('{safe}','U') IS NOT NULL DROP TABLE [{safe}]"))
        c.commit()


def _run_preset_sp(engine, preset_name: str, preset_cfg: dict, gc: str,
                   majcats: List[str], ts: str) -> tuple:
    """Run sp_AutoContCompute for ONE preset. Returns (detail_tbl, company_tbl, duration)."""
    safe_pre = _safe_name(preset_name).upper() or "PRESET"
    out_det  = f"{OUTPUT_PREFIX}_{gc.upper()}_{safe_pre}_{ts}"
    out_co   = f"{OUTPUT_PREFIX}_{gc.upper()}_{safe_pre}_CO_{ts}"

    kpi_type    = preset_cfg.get("kpi_type", "L30D")
    avg_days    = int(preset_cfg.get("avg_days", 30))
    months      = preset_cfg.get("months", []) or []
    months_csv  = ",".join(months)        if months  else None
    majcats_csv = ",".join(majcats)       if majcats else None

    t0 = time.time()
    with engine.connect() as c:
        c.execute(text("""
            EXEC dbo.sp_AutoContCompute
                @grouping_column=:gc, @kpi_type=:kpi, @avg_days=:ad,
                @months_csv=:m,       @majcats_csv=:mc,
                @out_detail=:od,      @out_company=:oc
        """), {
            "gc": gc, "kpi": kpi_type, "ad": avg_days,
            "m": months_csv, "mc": majcats_csv,
            "od": out_det, "oc": out_co,
        })
        c.commit()
    return out_det, out_co, round(time.time() - t0, 2)


def _combine_preset_tables(engine, preset_outputs: List[tuple], gc: str,
                           level: str, out_tbl: str) -> int:
    """Phase 3 — horizontal combine.

    preset_outputs: list of (preset_name, table_name) for one level (detail OR company).
    level:          'detail' or 'company' (different merge keys).
    out_tbl:        target table name.

    Returns row count in out_tbl. Uses dynamic LEFT JOIN since each preset's
    output table has identical key columns (same Master_HIER × Master_STORE_PLAN).
    """
    if not preset_outputs:
        return 0

    # Determine merge keys: master hier cols + base keys + grouping column
    hier_cols = _master_hier_cols(engine, gc)
    if level == "detail":
        merge_keys = list(dict.fromkeys(BASE_MERGE_KEYS_DETAIL + hier_cols + [gc]))
    else:
        merge_keys = list(dict.fromkeys(BASE_MERGE_KEYS_COMPANY + hier_cols + [gc]))

    # First preset gives us the column list to suffix
    first_preset, first_tbl = preset_outputs[0]
    first_cols = _table_columns(engine, first_tbl)
    if not first_cols:
        raise RuntimeError(f"Could not read columns from {first_tbl}")

    # Only keep merge_keys that ACTUALLY exist in the first table
    merge_keys = [k for k in merge_keys if k in first_cols]

    # Columns that aren't merge keys → renamed with |preset suffix
    value_cols = [c for c in first_cols if c not in merge_keys]

    # Build SELECT list and JOIN clauses
    select_parts = [f"P0.[{k}] AS [{k}]" for k in merge_keys]
    for c in value_cols:
        select_parts.append(f"P0.[{c}] AS [{c}|{first_preset}]")

    join_parts = []
    for idx, (pname, ptbl) in enumerate(preset_outputs[1:], start=1):
        alias = f"P{idx}"
        on = " AND ".join(f"P0.[{k}] = {alias}.[{k}]" for k in merge_keys)
        join_parts.append(f"LEFT JOIN [{ptbl}] {alias} WITH (NOLOCK) ON {on}")
        # Pull each preset's value columns (assume same shape; tolerate missing)
        pcols = _table_columns(engine, ptbl)
        for c in value_cols:
            if c in pcols:
                select_parts.append(f"{alias}.[{c}] AS [{c}|{pname}]")
            else:
                select_parts.append(f"NULL AS [{c}|{pname}]")

    # Drop output if exists, then SELECT INTO
    _drop_table(engine, out_tbl)
    sql = (
        f"SELECT " + ",\n       ".join(select_parts) + "\n"
        f"INTO [{out_tbl}]\n"
        f"FROM [{first_tbl}] P0 WITH (NOLOCK)\n"
        + ("\n".join(join_parts) if join_parts else "")
    )
    with engine.connect() as c:
        c.execute(text(sql))
        c.commit()
        n = c.execute(text(f"SELECT COUNT(*) FROM [{out_tbl}] WITH (NOLOCK)")).scalar() or 0
    return int(n)


def _apply_mappings(engine, table_name: str, target_filter: str) -> List[str]:
    """Phase 4 — apply Cont_mapping_assignments to a combined table.

    For each assignment whose target matches target_filter:
      ALTER TABLE … ADD [col_name] FLOAT NULL
      UPDATE  … SET [col_name] = CASE WHEN SSN='X' THEN MAX(of mapped cols)
                                       …
                                       ELSE MAX(of fallback cols) END

    Returns the list of column names added.
    """
    added = []
    with engine.connect() as c:
        rows = c.execute(text(f"""
            SELECT a.col_name, a.mapping_name, a.prefix, a.target,
                   m.mapping_json, m.fallback_json
              FROM {ASSIGNMENT_TABLE} a
         LEFT JOIN {MAPPING_TABLE}    m ON m.mapping_name = a.mapping_name
        """)).fetchall()
    if not rows:
        return added

    table_cols = set(_table_columns(engine, table_name))
    if "SSN" not in table_cols:
        logger.info(f"[AutoCont] {table_name} has no SSN column — skipping mappings")
        return added

    for r in rows:
        col_name, mapping_name, prefix, target = r[0], r[1], r[2] or "INITIAL AUTO CONT%|", r[3] or "Both"
        if target_filter not in ("Both", target) and target != "Both":
            # If table is Store-only and assignment targets Company, skip (and vice-versa)
            continue
        if not mapping_name:
            continue
        try:
            suffix_map = json.loads(r[4]) if r[4] else {}
            fb_list    = json.loads(r[5]) if r[5] else []
        except Exception:
            logger.warning(f"[AutoCont] mapping {mapping_name} has invalid JSON — skipping")
            continue

        # Build "when SSN='X' then GREATEST(...)" branches, but only with columns
        # that actually exist in this combined table. GREATEST → use VALUES + MAX.
        when_parts = []
        for ssn_key, sufs in suffix_map.items():
            sufs = sufs if isinstance(sufs, list) else [sufs]
            cols = [f"{prefix}{s}" for s in sufs]
            cols = [c for c in cols if c in table_cols]
            if not cols:
                continue
            values = ",".join(f"([{c}])" for c in cols)
            when_parts.append(
                f"WHEN SSN = '{str(ssn_key).replace(chr(39), chr(39)+chr(39))}' THEN "
                f"(SELECT MAX(v) FROM (VALUES {values}) AS x(v))"
            )

        fb_cols = [f"{prefix}{s}" for s in fb_list]
        fb_cols = [c for c in fb_cols if c in table_cols]
        if fb_cols:
            fb_vals = ",".join(f"([{c}])" for c in fb_cols)
            else_expr = f"(SELECT MAX(v) FROM (VALUES {fb_vals}) AS x(v))"
        else:
            else_expr = "NULL"

        if not when_parts and else_expr == "NULL":
            logger.info(f"[AutoCont] assignment {col_name}: no columns matched in {table_name}, skipping")
            continue

        case_expr = "CASE " + " ".join(when_parts) + f" ELSE {else_expr} END" if when_parts else else_expr

        # Quote the column name (it can contain '|')
        col_safe = col_name.replace("]", "")
        with engine.connect() as c:
            # ADD column if it doesn't exist (safe re-run)
            exists = c.execute(text(f"""
                SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_NAME=:t AND COLUMN_NAME=:c
            """), {"t": table_name, "c": col_name}).fetchone()
            if not exists:
                c.execute(text(f"ALTER TABLE [{table_name}] ADD [{col_safe}] FLOAT NULL"))
                c.commit()
            c.execute(text(f"UPDATE [{table_name}] SET [{col_safe}] = {case_expr}"))
            c.commit()
        added.append(col_name)
    return added


# ══════════════════════════════════════════════════════════════════════════════
#  Worker (Phase 2)
# ══════════════════════════════════════════════════════════════════════════════

def _run_job(job_id: str) -> None:
    """Background worker: run all selected presets, combine, apply mappings."""
    _lazy_load_jobs()
    with _job_lock:
        job = _jobs.get(job_id)
    if not job or job.get("status") == "cancelled":
        return
    _update_job(job_id, status="running",
                started_at=datetime.now().isoformat(),
                progress="loading presets…",
                persist=True)

    log: List[dict] = []
    t_total = time.time()
    payload = job["payload"]
    gc      = payload["grouping_column"]
    target  = payload.get("target", "Both")
    apply_m = bool(payload.get("apply_mappings", True))
    majcats = payload.get("majcats") or []

    engine = get_data_engine()

    try:
        if gc not in VALID_GROUPING:
            raise ValueError(f"Invalid grouping_column: {gc}")
        if not _check_proc_installed(engine):
            raise RuntimeError("sp_AutoContCompute is not installed in the data DB")

        # ── Load preset configs ──────────────────────────────────────────────
        with engine.connect() as c:
            rows = c.execute(text(f"""
                SELECT preset_name, config_json, sequence_order
                  FROM {PRESET_TABLE}
              ORDER BY sequence_order
            """)).fetchall()
        all_presets = OrderedDict((r[0], json.loads(r[1]) if r[1] else {}) for r in rows)

        selected = payload.get("presets") or list(all_presets.keys())
        # Preserve sequence_order
        selected = [p for p in all_presets.keys() if p in selected]
        if not selected:
            raise ValueError("No valid presets selected (Cont_presets is empty?)")

        # If majcats unspecified, pull all for this grouping_column
        if not majcats:
            try:
                with engine.connect() as c:
                    mj_rows = c.execute(text(f"""
                        SELECT DISTINCT MAJ_CAT FROM dbo.Master_HIER_{gc} WITH (NOLOCK)
                         WHERE SEG IN ('APP','GM')
                    """)).fetchall()
                majcats = [r[0] for r in mj_rows]
            except Exception:
                majcats = []

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log.append({"step": "init", "presets": selected, "majcats_n": len(majcats), "ts": ts})

        # ── Phase 1 loop: run SP per preset ──────────────────────────────────
        per_preset_detail:  List[tuple] = []   # (preset_name, table)
        per_preset_company: List[tuple] = []
        for idx, pname in enumerate(selected, 1):
            with _job_lock:
                st = _jobs.get(job_id, {}).get("status")
            if st == "cancelled":
                log.append({"step": "cancelled_at_preset", "preset": pname})
                _update_job(job_id, persist=True, status="cancelled", log=log,
                            finished_at=datetime.now().isoformat())
                return

            _update_job(job_id, progress=f"preset {idx}/{len(selected)}: {pname}",
                        log=list(log) + [{"step":"preset_start","preset":pname}])
            try:
                d, co, dur = _run_preset_sp(engine, pname, all_presets[pname],
                                            gc, majcats, ts)
                per_preset_detail.append((pname, d))
                per_preset_company.append((pname, co))
                log.append({"step": "preset_ok", "preset": pname,
                            "detail": d, "company": co, "duration": dur})
                logger.info(f"[AutoCont job {job_id}] {pname} ok in {dur}s → {d}, {co}")
            except Exception as e:
                logger.error(f"[AutoCont job {job_id}] {pname} failed: {e}")
                log.append({"step": "preset_error", "preset": pname, "error": str(e)[:500]})

        ok_detail  = [t for t in per_preset_detail  if t]
        ok_company = [t for t in per_preset_company if t]
        if not ok_detail and not ok_company:
            raise RuntimeError("All presets failed; nothing to combine")

        # ── Phase 3: horizontal combine ──────────────────────────────────────
        detail_table  = None
        company_table = None
        det_rows = co_rows = 0

        if target in ("Both", "Store") and ok_detail:
            detail_table = f"{FINAL_PREFIX}_{gc.upper()}_{ts}"
            _update_job(job_id, progress=f"combining detail across {len(ok_detail)} presets…",
                        log=list(log))
            t = time.time()
            det_rows = _combine_preset_tables(engine, ok_detail, gc, "detail", detail_table)
            log.append({"step": "combine_detail", "table": detail_table,
                        "rows": det_rows, "duration": round(time.time()-t, 2)})

        if target in ("Both", "Company") and ok_company:
            company_table = f"{FINAL_PREFIX}_{gc.upper()}_CO_{ts}"
            _update_job(job_id, progress=f"combining company across {len(ok_company)} presets…",
                        log=list(log))
            t = time.time()
            co_rows = _combine_preset_tables(engine, ok_company, gc, "company", company_table)
            log.append({"step": "combine_company", "table": company_table,
                        "rows": co_rows, "duration": round(time.time()-t, 2)})

        # ── Phase 4: mapping assignments ─────────────────────────────────────
        if apply_m:
            _update_job(job_id, progress="applying mappings…", log=list(log))
            if detail_table:
                t = time.time()
                added = _apply_mappings(engine, detail_table, "Store")
                log.append({"step": "mappings_detail", "added": added,
                            "duration": round(time.time()-t, 2)})
            if company_table:
                t = time.time()
                added = _apply_mappings(engine, company_table, "Company")
                log.append({"step": "mappings_company", "added": added,
                            "duration": round(time.time()-t, 2)})

        # ── Cleanup intermediate per-preset tables ──────────────────────────
        # We KEEP the combined finals; drop the intermediates.
        for _, tbl in ok_detail + ok_company:
            try:
                _drop_table(engine, tbl)
            except Exception as e:
                logger.warning(f"[AutoCont job {job_id}] cleanup drop {tbl}: {e}")
        log.append({"step": "cleanup", "intermediate_dropped":
                    len(ok_detail) + len(ok_company)})

        total_dur = round(time.time() - t_total, 2)
        _update_job(
            job_id, persist=True,
            status="completed",
            progress="done",
            log=log,
            duration=total_dur,
            detail_table=detail_table,
            company_table=company_table,
            detail_rows=det_rows,
            company_rows=co_rows,
            finished_at=datetime.now().isoformat(),
        )
        logger.info(f"[AutoCont job {job_id}] DONE in {total_dur}s — "
                    f"detail={detail_table} ({det_rows}), company={company_table} ({co_rows})")

    except Exception as e:
        logger.error(f"[AutoCont job {job_id}] FAILED: {e}")
        log.append({"step": "fatal", "error": str(e)[:1000]})
        _update_job(job_id, persist=True, status="failed",
                    log=log, error=str(e)[:1000],
                    finished_at=datetime.now().isoformat())


def _start_job(job_id: str) -> None:
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
#  Endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/status", response_model=APIResponse)
def status(current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    installed = _check_proc_installed(engine)
    _lazy_load_jobs()
    with _job_lock:
        active = sum(1 for j in _jobs.values() if j["status"] in ("pending","running"))
    return APIResponse(success=True, data={
        "proc_installed":          installed,
        "proc_name":               "sp_AutoContCompute",
        "valid_grouping_columns":  list(VALID_GROUPING),
        "valid_kpi_types":         list(VALID_KPI_TYPES),
        "output_prefix":           OUTPUT_PREFIX,
        "active_jobs":             active,
    })


@router.post("/execute", response_model=APIResponse)
def execute(payload: ExecutePayload, current_user: User = Depends(get_current_user)):
    """Create a background job. Returns job_id immediately — poll /jobs/{id}."""
    if payload.grouping_column not in VALID_GROUPING:
        raise HTTPException(400, f"Invalid grouping_column. Allowed: {VALID_GROUPING}")
    if payload.target not in ("Store", "Company", "Both"):
        raise HTTPException(400, "target must be Store / Company / Both")

    engine = get_data_engine()
    if not _check_proc_installed(engine):
        raise HTTPException(500, "sp_AutoContCompute is not installed; run "
                                  "backend/sql/sp_AutoContCompute.sql against Rep_Data first.")

    job_id = str(uuid.uuid4())[:8]
    preset_label = (", ".join(payload.presets[:3]) +
                    (f" +{len(payload.presets)-3}" if len(payload.presets) > 3 else "")) \
                   if payload.presets else "all"
    job = {
        "id": job_id,
        "status": "pending",
        "payload": payload.dict(),
        "label": f"{payload.grouping_column} | {preset_label}",
        "progress": "queued",
        "log": [],
        "created_at": datetime.now().isoformat(),
        "started_at": None,
        "finished_at": None,
        "duration": None,
        "detail_table":  None,
        "company_table": None,
        "detail_rows":   0,
        "company_rows":  0,
        "error":         None,
    }
    with _job_lock:
        _jobs[job_id] = job
    _persist_job(job)
    _start_job(job_id)
    logger.info(f"[AutoCont] job {job_id} created — gc={payload.grouping_column}, "
                f"presets={preset_label}, target={payload.target}")
    return APIResponse(success=True, message=f"Job {job_id} started",
                       data={"job_id": job_id})


@router.get("/jobs", response_model=APIResponse)
def list_jobs(current_user: User = Depends(get_current_user)):
    _lazy_load_jobs()
    with _job_lock:
        jobs = list(reversed(_jobs.values()))
    out = []
    for j in jobs:
        out.append({
            "id": j["id"], "status": j["status"], "label": j.get("label", ""),
            "progress": j.get("progress"),
            "created_at": j.get("created_at"),
            "started_at": j.get("started_at"),
            "finished_at": j.get("finished_at"),
            "duration": j.get("duration"),
            "detail_table":  j.get("detail_table"),
            "company_table": j.get("company_table"),
            "detail_rows":   j.get("detail_rows", 0),
            "company_rows":  j.get("company_rows", 0),
            "error": j.get("error"),
        })
    return APIResponse(success=True, data={"jobs": out})


@router.get("/jobs/{job_id}", response_model=APIResponse)
def get_job(job_id: str, current_user: User = Depends(get_current_user)):
    _lazy_load_jobs()
    with _job_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return APIResponse(success=True, data={"job": job})


@router.post("/jobs/{job_id}/cancel", response_model=APIResponse)
def cancel_job(job_id: str, current_user: User = Depends(get_current_user)):
    with _job_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job["status"] in ("pending", "running"):
            job["status"] = "cancelled"
            job["finished_at"] = datetime.now().isoformat()
    _persist_job(job)
    return APIResponse(success=True, message=f"Job {job_id} cancelled")


@router.delete("/jobs/{job_id}", response_model=APIResponse)
def delete_job(job_id: str, current_user: User = Depends(get_current_user)):
    with _job_lock:
        job = _jobs.pop(job_id, None)
    try:
        engine = get_data_engine()
        with engine.connect() as c:
            c.execute(text(f"DELETE FROM {JOB_TABLE} WHERE job_id=:id"), {"id": job_id})
            c.commit()
    except Exception:
        pass
    return APIResponse(success=True, message=f"Job {job_id} deleted")


@router.get("/tables", response_model=APIResponse)
def list_tables(current_user: User = Depends(get_current_user)):
    """List AutoCont_FINAL_* output tables (intermediates are auto-dropped)."""
    engine = get_data_engine()
    try:
        insp = inspect(engine)
        all_tables = insp.get_table_names()
        tables = sorted(
            [t for t in all_tables if t.upper().startswith(FINAL_PREFIX.upper())],
            reverse=True,
        )
    except Exception:
        tables = []
    return APIResponse(success=True, data={"tables": tables, "total": len(tables)})


@router.get("/preview/{table_name}", response_model=APIResponse)
def preview(table_name: str, limit: int = Query(200, ge=1, le=2000),
            current_user: User = Depends(get_current_user)):
    if not table_name.upper().startswith(OUTPUT_PREFIX.upper()):
        raise HTTPException(400, "Invalid table name")
    safe = table_name.replace("'", "").replace(";", "")
    engine = get_data_engine()
    try:
        df = pd.read_sql(f"SELECT TOP {int(limit)} * FROM [{safe}] WITH (NOLOCK)", engine)
        for c in df.select_dtypes(include=["float64", "float32", "float"]).columns:
            df[c] = df[c].round(4)
        with engine.connect() as c:
            total = c.execute(text(f"SELECT COUNT(*) FROM [{safe}] WITH (NOLOCK)")).scalar() or 0
    except Exception as e:
        raise HTTPException(500, f"Preview failed: {str(e)[:500]}")
    return APIResponse(success=True, data={
        "columns":    list(df.columns),
        "total_rows": int(total),
        "preview":    json.loads(df.to_json(orient="records", date_format="iso")),
    })


@router.delete("/tables/{table_name}", response_model=APIResponse)
def drop_table(table_name: str, current_user: User = Depends(get_current_user)):
    if not table_name.upper().startswith(OUTPUT_PREFIX.upper()):
        raise HTTPException(400, "Invalid table name")
    safe = table_name.replace("'", "").replace(";", "")
    engine = get_data_engine()
    with engine.connect() as c:
        c.execute(text(f"IF OBJECT_ID('{safe}','U') IS NOT NULL DROP TABLE [{safe}]"))
        c.commit()
    return APIResponse(success=True, message=f"Dropped [{safe}]")


@router.get("/download/{table_name}")
def download_table(table_name: str, current_user: User = Depends(get_current_user)):
    """Stream the full table as CSV."""
    if not table_name.upper().startswith(OUTPUT_PREFIX.upper()):
        raise HTTPException(400, "Invalid table name")
    safe = table_name.replace("'", "").replace(";", "")
    engine = get_data_engine()

    def csv_stream():
        first = True
        for chunk in pd.read_sql(f"SELECT * FROM [{safe}] WITH (NOLOCK)",
                                 engine, chunksize=100_000):
            yield chunk.to_csv(index=False, header=first)
            first = False

    return StreamingResponse(csv_stream(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={safe}.csv"})
