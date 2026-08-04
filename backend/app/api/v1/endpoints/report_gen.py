"""
Report Generation API — manage report definitions, run them, view run history.

A report = ordered steps (SQL stored procedures + registered code steps) with a
trigger (schedule | event | manual) and an output (folder | snowflake | both).

Distinct from the existing /reports endpoint (the pend-alc report); this one is
prefixed /report-gen.
"""
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text, bindparam
from loguru import logger

from app.database.session import get_data_engine
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.models.rbac import User
from app.services.report_engine import REPORTS_TABLE, RUNS_TABLE
from app.services.report_code_steps import list_code_steps
from app.services.report_scheduler_service import (
    report_scheduler, compute_next_run, ensure_report_tables,
    KNOWN_EVENTS,
)

router = APIRouter(prefix="/report-gen", tags=["Report Generation"])

_JSON_COLS = ("STEPS", "SCHEDULE_CONFIG", "SNOWFLAKE_CONFIG", "SPLIT_CONFIG",
              "EMAIL_CONFIG", "WHATSAPP_CONFIG", "SMS_CONFIG")
_RUN_JSON_COLS = ("FILES", "ERRORS")


# ── Request models ──────────────────────────────────────────────────────────
class ReportStep(BaseModel):
    type: str = Field(..., pattern="^(sql|code|query)$")
    name: str
    params: Dict[str, Any] = Field(default_factory=dict)
    label: Optional[str] = None      # custom output file name (overrides proc name)


class ReportBody(BaseModel):
    name: str
    description: Optional[str] = None
    steps: List[ReportStep] = Field(default_factory=list)
    output_type: str = Field("folder", pattern="^(folder|snowflake|both|email)$")
    base_dir: Optional[str] = None
    file_format: str = Field("csv", pattern="^(csv|xlsx)$")
    folder_per_run: bool = True      # False = reuse one <base>/data folder
    snowflake_config: Optional[Dict[str, Any]] = None
    split_config: Optional[Dict[str, Any]] = None
    email_config: Optional[Dict[str, Any]] = None
    whatsapp_config: Optional[Dict[str, Any]] = None
    sms_config: Optional[Dict[str, Any]] = None
    trigger_type: str = Field("manual", pattern="^(schedule|event|manual)$")
    schedule_config: Optional[Dict[str, Any]] = None
    trigger_event: Optional[str] = None
    enabled: bool = True


class ToggleBody(BaseModel):
    enabled: bool


# ── Helpers ─────────────────────────────────────────────────────────────────
def _row_to_report(row: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(row)
    for col in _JSON_COLS:
        if col in d and isinstance(d[col], str) and d[col]:
            try:
                d[col] = json.loads(d[col])
            except Exception:
                pass
    for dt in ("NEXT_RUN_AT", "LAST_RUN_AT", "CREATED_AT", "UPDATED_AT"):
        if d.get(dt) is not None:
            d[dt] = d[dt].isoformat() if hasattr(d[dt], "isoformat") else d[dt]
    return d


def _validate_trigger(body: ReportBody) -> None:
    if body.trigger_type == "event":
        if body.trigger_event not in KNOWN_EVENTS:
            raise HTTPException(400, f"trigger_event must be one of {sorted(KNOWN_EVENTS)}")
    if body.trigger_type == "schedule":
        cfg = body.schedule_config or {}
        if not cfg:
            raise HTTPException(400, "schedule_config is required for schedule trigger")
        freq = str(cfg.get("freq", "daily")).lower()
        if freq == "once" and not cfg.get("datetime"):
            raise HTTPException(400, "a one-time schedule needs a date & time")
        if freq == "once":
            try:
                dt = datetime.fromisoformat(str(cfg["datetime"]))
            except Exception:
                raise HTTPException(400, "one-time 'datetime' must be ISO format")
            if dt <= datetime.utcnow():
                raise HTTPException(400, "one-time date & time must be in the future")
        if freq == "monthly":
            days = cfg.get("days")
            if days is not None:
                if not isinstance(days, list) or not days:
                    raise HTTPException(400, "monthly schedule needs at least one date")
                if any(not (1 <= int(d) <= 31) for d in days):
                    raise HTTPException(400, "monthly dates must be 1-31")
            else:
                day = cfg.get("day")
                if day is None or not (1 <= int(day) <= 31):
                    raise HTTPException(400, "monthly schedule needs day of month 1-31")
        if freq == "weekly":
            wds = cfg.get("weekdays")
            if wds is None and cfg.get("weekday") is None:
                raise HTTPException(400, "weekly schedule needs at least one weekday")
            if wds is not None and (not isinstance(wds, list) or not wds):
                raise HTTPException(400, "weekly schedule needs at least one weekday")


def _validate_output(body: ReportBody) -> None:
    if body.output_type == "email":
        em = body.email_config or {}
        if not em.get("enabled") or not (em.get("to") or em.get("cc") or em.get("bcc")):
            raise HTTPException(400, "Email-only output needs email enabled with at least "
                                     "one recipient")


def _next_run_for(body: ReportBody):
    if body.trigger_type == "schedule" and body.enabled and body.schedule_config:
        return compute_next_run(body.schedule_config)
    return None


# ── Endpoints ───────────────────────────────────────────────────────────────
@router.get("/reports", response_model=APIResponse)
def list_reports(current_user: User = Depends(get_current_user)):
    ensure_report_tables()
    engine = get_data_engine()
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT * FROM {REPORTS_TABLE} ORDER BY CREATED_AT DESC
        """)).mappings().fetchall()
    return APIResponse(success=True, data=[_row_to_report(r) for r in rows])


@router.get("/reports/{report_id}", response_model=APIResponse)
def get_report(report_id: int, current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    with engine.connect() as conn:
        row = conn.execute(text(f"""
            SELECT * FROM {REPORTS_TABLE} WHERE REPORT_ID = :id
        """), {"id": report_id}).mappings().fetchone()
    if not row:
        raise HTTPException(404, "Report not found")
    return APIResponse(success=True, data=_row_to_report(row))


@router.post("/reports", response_model=APIResponse)
def create_report(body: ReportBody, current_user: User = Depends(get_current_user)):
    ensure_report_tables()
    _validate_trigger(body)
    _validate_output(body)
    engine = get_data_engine()
    with engine.begin() as conn:
        row = conn.execute(text(f"""
            INSERT INTO {REPORTS_TABLE}
                (NAME, DESCRIPTION, STEPS, OUTPUT_TYPE, BASE_DIR, FILE_FORMAT,
                 SNOWFLAKE_CONFIG, SPLIT_CONFIG, EMAIL_CONFIG, WHATSAPP_CONFIG, SMS_CONFIG,
                 FOLDER_PER_RUN, TRIGGER_TYPE, SCHEDULE_CONFIG, TRIGGER_EVENT, ENABLED, NEXT_RUN_AT, CREATED_BY)
            OUTPUT inserted.REPORT_ID
            VALUES (:name, :desc, :steps, :otype, :bdir, :fmt, :sf, :split, :email, :wa, :sms, :fpr,
                    :ttype, :sched, :tev, :en, :nr, :cb)
        """), {
            "name": body.name, "desc": body.description,
            "steps": json.dumps([s.model_dump() for s in body.steps]),
            "otype": body.output_type, "bdir": body.base_dir,
            "fmt": body.file_format, "fpr": 1 if body.folder_per_run else 0,
            "sf": json.dumps(body.snowflake_config) if body.snowflake_config else None,
            "split": json.dumps(body.split_config) if body.split_config else None,
            "email": json.dumps(body.email_config) if body.email_config else None,
            "wa": json.dumps(body.whatsapp_config) if body.whatsapp_config else None,
            "sms": json.dumps(body.sms_config) if body.sms_config else None,
            "ttype": body.trigger_type,
            "sched": json.dumps(body.schedule_config) if body.schedule_config else None,
            "tev": body.trigger_event, "en": 1 if body.enabled else 0,
            "nr": _next_run_for(body),
            "cb": getattr(current_user, "username", None),
        }).fetchone()
    logger.info(f"[report-gen] created report {row[0]} '{body.name}'")
    return APIResponse(success=True, message="Report created",
                       data={"report_id": int(row[0])})


@router.put("/reports/{report_id}", response_model=APIResponse)
def update_report(report_id: int, body: ReportBody,
                  current_user: User = Depends(get_current_user)):
    _validate_trigger(body)
    _validate_output(body)
    engine = get_data_engine()
    with engine.begin() as conn:
        res = conn.execute(text(f"""
            UPDATE {REPORTS_TABLE} SET
                NAME=:name, DESCRIPTION=:desc, STEPS=:steps, OUTPUT_TYPE=:otype,
                BASE_DIR=:bdir, FILE_FORMAT=:fmt, SNOWFLAKE_CONFIG=:sf,
                SPLIT_CONFIG=:split, EMAIL_CONFIG=:email, WHATSAPP_CONFIG=:wa, SMS_CONFIG=:sms,
                FOLDER_PER_RUN=:fpr,
                TRIGGER_TYPE=:ttype, SCHEDULE_CONFIG=:sched, TRIGGER_EVENT=:tev,
                ENABLED=:en, NEXT_RUN_AT=:nr, UPDATED_AT=SYSUTCDATETIME()
            WHERE REPORT_ID=:id
        """), {
            "name": body.name, "desc": body.description,
            "steps": json.dumps([s.model_dump() for s in body.steps]),
            "otype": body.output_type, "bdir": body.base_dir,
            "fmt": body.file_format, "fpr": 1 if body.folder_per_run else 0,
            "sf": json.dumps(body.snowflake_config) if body.snowflake_config else None,
            "split": json.dumps(body.split_config) if body.split_config else None,
            "email": json.dumps(body.email_config) if body.email_config else None,
            "wa": json.dumps(body.whatsapp_config) if body.whatsapp_config else None,
            "sms": json.dumps(body.sms_config) if body.sms_config else None,
            "ttype": body.trigger_type,
            "sched": json.dumps(body.schedule_config) if body.schedule_config else None,
            "tev": body.trigger_event, "en": 1 if body.enabled else 0,
            "nr": _next_run_for(body), "id": report_id,
        })
    if res.rowcount == 0:
        raise HTTPException(404, "Report not found")
    return APIResponse(success=True, message="Report updated")


@router.post("/reports/{report_id}/toggle", response_model=APIResponse)
def toggle_report(report_id: int, body: ToggleBody,
                  current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    with engine.begin() as conn:
        # When re-enabling a schedule, recompute its next run.
        row = conn.execute(text(f"""
            SELECT TRIGGER_TYPE, SCHEDULE_CONFIG FROM {REPORTS_TABLE}
            WHERE REPORT_ID=:id
        """), {"id": report_id}).fetchone()
        if not row:
            raise HTTPException(404, "Report not found")
        next_run = None
        if body.enabled and row[0] == "schedule" and row[1]:
            try:
                next_run = compute_next_run(json.loads(row[1]))
            except Exception:
                next_run = None
        conn.execute(text(f"""
            UPDATE {REPORTS_TABLE} SET ENABLED=:en, NEXT_RUN_AT=:nr,
                   UPDATED_AT=SYSUTCDATETIME() WHERE REPORT_ID=:id
        """), {"en": 1 if body.enabled else 0, "nr": next_run, "id": report_id})
    return APIResponse(success=True, message="enabled" if body.enabled else "disabled")


@router.delete("/reports/{report_id}", response_model=APIResponse)
def delete_report(report_id: int, current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    with engine.begin() as conn:
        res = conn.execute(text(f"DELETE FROM {REPORTS_TABLE} WHERE REPORT_ID=:id"),
                           {"id": report_id})
    if res.rowcount == 0:
        raise HTTPException(404, "Report not found")
    return APIResponse(success=True, message="Report deleted")


@router.post("/reports/{report_id}/run", response_model=APIResponse)
def run_report_now(report_id: int, current_user: User = Depends(get_current_user)):
    """Generate the report now (queued through the same worker pool)."""
    result = report_scheduler.run_now(
        report_id, getattr(current_user, "username", None))
    if not result.get("queued"):
        raise HTTPException(404, result.get("error", "could not queue"))
    return APIResponse(success=True, message="Report queued", data=result)


@router.post("/reports/{report_id}/cancel", response_model=APIResponse)
def cancel_report_run(report_id: int, current_user: User = Depends(get_current_user)):
    """Stop a running report — terminates its worker process and marks the run
    cancelled. No-op if the report isn't currently running."""
    # Reports run as separate worker processes; cancel = terminate that process
    # (which also marks the run 'cancelled'). Falls back to clearing stale rows.
    if report_scheduler.cancel_running(report_id):
        logger.info(f"[report-gen] cancel requested for report {report_id} (worker terminated)")
        return APIResponse(success=True, message="Cancelling run",
                           data={"report_id": report_id})
    # No live worker in this process — clear any STALE 'running' rows.
    engine = get_data_engine()
    with engine.begin() as conn:
        res = conn.execute(text(f"""
            UPDATE {RUNS_TABLE}
            SET STATUS='cancelled',
                ERRORS='[{{"step":"cancel","error":"cleared stale run (was not active)"}}]',
                COMPLETED_AT=SYSUTCDATETIME()
            WHERE REPORT_ID=:id AND STATUS='running'
        """), {"id": report_id})
    if res.rowcount:
        return APIResponse(success=True, message=f"Cleared {res.rowcount} stale run(s)")
    return APIResponse(success=False, message="No running run to cancel")


@router.get("/reports/{report_id}/runs", response_model=APIResponse)
def list_runs(report_id: int, limit: int = 50,
              current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT TOP (:lim) * FROM {RUNS_TABLE}
            WHERE REPORT_ID=:id ORDER BY CREATED_AT DESC
        """), {"lim": limit, "id": report_id}).mappings().fetchall()
    runs = []
    for r in rows:
        d = dict(r)
        for col in _RUN_JSON_COLS:
            if isinstance(d.get(col), str) and d[col]:
                try:
                    d[col] = json.loads(d[col])
                except Exception:
                    pass
        for dt in ("STARTED_AT", "COMPLETED_AT", "CREATED_AT"):
            if d.get(dt) is not None and hasattr(d[dt], "isoformat"):
                d[dt] = d[dt].isoformat()
        runs.append(d)
    return APIResponse(success=True, data=runs)


@router.delete("/reports/{report_id}/runs/{run_id}", response_model=APIResponse)
def delete_run(report_id: int, run_id: int,
               current_user: User = Depends(get_current_user)):
    """Delete one run (session) from a report's history. Also removes that run's
    dedicated per-run session folder on disk (never a shared/date folder)."""
    import os
    import shutil
    engine = get_data_engine()
    with engine.connect() as conn:
        row = conn.execute(text(f"""
            SELECT SESSION_CODE, EXPORT_DIR, STATUS FROM {RUNS_TABLE}
            WHERE RUN_ID=:rid AND REPORT_ID=:pid
        """), {"rid": run_id, "pid": report_id}).fetchone()
    if not row:
        raise HTTPException(404, "Run not found")
    session_code, export_dir, status = row[0], row[1], row[2]
    if status == "running":
        raise HTTPException(409, "Run is still running — stop it first")

    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {RUNS_TABLE} WHERE RUN_ID=:rid"),
                     {"rid": run_id})

    # Remove the folder ONLY if it is this run's dedicated session folder
    # (basename == session code). A shared date folder never matches, so it's
    # left untouched.
    removed_folder = False
    try:
        if export_dir and os.path.isdir(export_dir):
            base = os.path.basename(os.path.normpath(export_dir))
            if session_code and base == session_code:
                shutil.rmtree(export_dir, ignore_errors=True)
                removed_folder = not os.path.isdir(export_dir)
    except Exception as e:
        logger.warning(f"[report-gen] run {run_id} folder cleanup failed: {e}")

    logger.info(f"[report-gen] deleted run {run_id} (report {report_id}), "
                f"folder_removed={removed_folder}")
    return APIResponse(success=True, message="Run deleted",
                       data={"removed_folder": removed_folder})


def _remove_run_folder(export_dir, session_code) -> bool:
    """Remove a run's dedicated per-run folder only (never a shared folder)."""
    import os
    import shutil
    try:
        if export_dir and os.path.isdir(export_dir) and session_code and \
           os.path.basename(os.path.normpath(export_dir)) == session_code:
            shutil.rmtree(export_dir, ignore_errors=True)
            return not os.path.isdir(export_dir)
    except Exception as e:
        logger.warning(f"[report-gen] folder cleanup failed for {export_dir}: {e}")
    return False


class BulkDeleteRuns(BaseModel):
    run_ids: List[int]


@router.post("/reports/{report_id}/runs/bulk-delete", response_model=APIResponse)
def bulk_delete_runs(report_id: int, body: BulkDeleteRuns,
                     current_user: User = Depends(get_current_user)):
    """Delete several runs at once. Skips any that are still running; removes
    each run's dedicated per-run folder (shared date folders are left alone)."""
    ids = [int(x) for x in (body.run_ids or [])]
    if not ids:
        return APIResponse(success=False, message="No runs selected")
    engine = get_data_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""SELECT RUN_ID, SESSION_CODE, EXPORT_DIR, STATUS
                     FROM {RUNS_TABLE}
                     WHERE REPORT_ID=:pid AND RUN_ID IN :ids""")
            .bindparams(bindparam("ids", expanding=True)),
            {"pid": report_id, "ids": ids}).fetchall()

    deletable = [r for r in rows if r[3] != "running"]
    skipped_running = len(rows) - len(deletable)
    del_ids = [r[0] for r in deletable]
    if del_ids:
        with engine.begin() as conn:
            conn.execute(
                text(f"DELETE FROM {RUNS_TABLE} WHERE RUN_ID IN :ids")
                .bindparams(bindparam("ids", expanding=True)),
                {"ids": del_ids})
        folders = sum(_remove_run_folder(r[2], r[1]) for r in deletable)
    else:
        folders = 0

    logger.info(f"[report-gen] bulk-deleted {len(del_ids)} run(s) of report "
                f"{report_id}, folders_removed={folders}, skipped_running={skipped_running}")
    msg = f"Deleted {len(del_ids)} run(s)"
    if skipped_running:
        msg += f" ({skipped_running} still running — skipped)"
    return APIResponse(success=True, message=msg,
                       data={"deleted": len(del_ids), "folders_removed": folders,
                             "skipped_running": skipped_running})


@router.get("/code-steps", response_model=APIResponse)
def get_code_steps(current_user: User = Depends(get_current_user)):
    """Registered code steps, for the 'Add code step' picker."""
    return APIResponse(success=True, data=list_code_steps())


@router.get("/procedures", response_model=APIResponse)
def list_procedures(search: str = "", limit: int = 500,
                    current_user: User = Depends(get_current_user)):
    """Stored procedures available on the Data DB, for the picker.

    Optional ?search= filters by name (contains). Excludes system procs.
    """
    engine = get_data_engine()
    like = f"%{search.strip()}%" if search.strip() else "%"
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT TOP (:lim) ROUTINE_SCHEMA, ROUTINE_NAME
            FROM INFORMATION_SCHEMA.ROUTINES
            WHERE ROUTINE_TYPE = 'PROCEDURE'
              AND ROUTINE_NAME NOT LIKE 'sp\\_%' ESCAPE '\\'
              AND ROUTINE_NAME NOT LIKE 'dt\\_%' ESCAPE '\\'
              AND ROUTINE_NAME LIKE :like
            ORDER BY ROUTINE_NAME
        """), {"lim": limit, "like": like}).fetchall()
    procs = [{"schema": r[0], "name": r[1],
              "full": f"{r[0]}.{r[1]}" if r[0] and r[0] != "dbo" else r[1]}
             for r in rows]
    return APIResponse(success=True, data=procs)


@router.get("/proc-params", response_model=APIResponse)
def proc_params(name: str, current_user: User = Depends(get_current_user)):
    """Declared input parameters of a stored procedure (for the params editor).

    `name` may be 'schema.proc' or just 'proc'. Returns the @param names (minus
    the @), data types, and ordinal — input/inout params only.
    """
    raw = str(name).strip().strip("[]")
    if "." in raw:
        schema, proc = raw.split(".", 1)
        schema, proc = schema.strip("[]"), proc.strip("[]")
    else:
        schema, proc = None, raw
    engine = get_data_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT PARAMETER_NAME, DATA_TYPE, ORDINAL_POSITION
            FROM INFORMATION_SCHEMA.PARAMETERS
            WHERE SPECIFIC_NAME = :proc
              AND (:schema IS NULL OR SPECIFIC_SCHEMA = :schema)
              AND PARAMETER_NAME <> ''
              AND ISNULL(PARAMETER_MODE, 'IN') IN ('IN', 'INOUT')
            ORDER BY ORDINAL_POSITION
        """), {"proc": proc, "schema": schema}).fetchall()
    params = [{"name": str(r[0]).lstrip("@"), "type": r[1]} for r in rows]

    # Attach any registered allowed-values (for a dropdown) from ARS_PROC_PARAM_VALUES.
    # Matched case-insensitively on proc (no schema) + param name. Optional table.
    try:
        with engine.connect() as conn:
            if conn.execute(text("SELECT OBJECT_ID('dbo.ARS_PROC_PARAM_VALUES')")).scalar():
                vrows = conn.execute(text("""
                    SELECT PARAM_NAME, VAL, LABEL
                    FROM dbo.ARS_PROC_PARAM_VALUES
                    WHERE LOWER(PROC_NAME) = LOWER(:proc)
                    ORDER BY PARAM_NAME, SORT_ORDER, VAL
                """), {"proc": proc}).fetchall()
                allowed: dict = {}
                for pn, val, lab in vrows:
                    allowed.setdefault(str(pn).lstrip("@").lower(), []).append(
                        {"value": val, "label": lab or val})
                for p in params:
                    opts = allowed.get(p["name"].lower())
                    if opts:
                        p["allowed"] = opts
    except Exception:
        pass  # registry is optional; never break the params editor

    return APIResponse(success=True, data=params)


@router.get("/events", response_model=APIResponse)
def get_events(current_user: User = Depends(get_current_user)):
    """Known process-completion events a report can subscribe to."""
    labels = {
        "listing.approved": "Listing approve",
        "pendalc.approved": "Pending allocation approve",
        "msa.completed": "MSA calculation",
        "autocont.completed": "Auto Contrib",
    }
    return APIResponse(success=True, data=[
        {"event": e, "label": labels.get(e, e)} for e in sorted(KNOWN_EVENTS)])


@router.get("/status", response_model=APIResponse)
def scheduler_status(current_user: User = Depends(get_current_user)):
    # Reports run in separate worker PROCESSES now — the live set comes from the
    # scheduler's process table, not the (per-process) in-memory token registry.
    data = dict(report_scheduler.status)
    data["running_report_ids"] = report_scheduler.active_report_ids()
    return APIResponse(success=True, data=data)
