"""
Report engine — execute one report definition end to end.

A report is an ordered list of steps (SQL stored procedures and/or registered
code steps). run_report():
  1. creates the session-code folder  <base_dir>/data/<session_code>/
  2. inserts an ARS_REPORT_RUNS row (status=running)
  3. runs each step in order, collecting the files it produced and any errors
  4. optionally upserts into Snowflake (OUTPUT_TYPE in snowflake|both)
  5. finalizes the run row (completed|failed) and stamps the report's LAST_*.

Trigger source is free text: "schedule", "manual", or "event:<name>".
"""
import json
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine
from app.services.data_export_service import (
    make_session_dir,
    run_procedure_to_files,
    run_query_to_files,
)
from app.services.report_code_steps import StepContext, run_code_step

REPORTS_TABLE = "ARS_REPORTS"
RUNS_TABLE = "ARS_REPORT_RUNS"


# ── Cancellation ────────────────────────────────────────────────────────────
class CancelToken:
    """Per-run cancel signal. Also holds the active DB cursor so a cancel can
    interrupt an in-flight query (pyodbc cursor.cancel() is thread-safe)."""

    def __init__(self, report_id: int):
        self.report_id = report_id
        self._event = threading.Event()
        self._cursor = None
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            cur = self._cursor
        if cur is not None:
            try:
                cur.cancel()   # interrupts a running execute()/fetch()
            except Exception:
                pass

    def bind_cursor(self, cur) -> None:
        with self._lock:
            self._cursor = cur

    def unbind_cursor(self) -> None:
        with self._lock:
            self._cursor = None


_ACTIVE_RUNS: Dict[int, CancelToken] = {}     # run_id -> token
_ACTIVE_LOCK = threading.Lock()


def _register_run(run_id: int, report_id: int) -> CancelToken:
    tok = CancelToken(report_id)
    with _ACTIVE_LOCK:
        _ACTIVE_RUNS[run_id] = tok
    return tok


def _unregister_run(run_id: int) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_RUNS.pop(run_id, None)


def cancel_run(run_id: int) -> bool:
    """Request cancellation of one run. Returns True if it was active."""
    with _ACTIVE_LOCK:
        tok = _ACTIVE_RUNS.get(run_id)
    if tok:
        tok.cancel()
        return True
    return False


def cancel_report(report_id: int) -> List[int]:
    """Cancel all active runs of a report. Returns the run_ids cancelled."""
    with _ACTIVE_LOCK:
        toks = [(rid, t) for rid, t in _ACTIVE_RUNS.items() if t.report_id == report_id]
    for _, t in toks:
        t.cancel()
    return [rid for rid, _ in toks]


def active_run_report_ids() -> List[int]:
    """report_ids that currently have a running run in THIS process."""
    with _ACTIVE_LOCK:
        return sorted({t.report_id for t in _ACTIVE_RUNS.values()})


def make_session_code() -> str:
    """Millisecond-precision code — safe as a folder name and collision-free."""
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


def _parse_json(value: Any) -> Optional[Dict[str, Any]]:
    if not value:
        return None
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def _parse_split(split_json: Any) -> Optional[Dict[str, Any]]:
    return _parse_json(split_json)


def _parse_steps(steps_json: Any) -> List[Dict[str, Any]]:
    if isinstance(steps_json, list):
        raw = steps_json
    else:
        raw = json.loads(steps_json or "[]")
    steps = []
    for s in raw:
        stype = str(s.get("type", "sql")).lower()
        name = str(s.get("name", "")).strip()
        params = s.get("params") or {}
        if stype not in ("sql", "code", "query") or not name:
            raise ValueError(f"Invalid step: {s!r}")
        step = {"type": stype, "name": name, "params": params}
        if s.get("label"):
            step["label"] = str(s["label"]).strip()
        steps.append(step)
    return steps


def _insert_run(report_id: int, session_code: str, export_dir: str,
                trigger_source: str, created_by: Optional[str]) -> int:
    engine = get_data_engine()
    with engine.begin() as conn:
        row = conn.execute(text(f"""
            INSERT INTO {RUNS_TABLE}
                (REPORT_ID, SESSION_CODE, TRIGGER_SOURCE, STATUS,
                 EXPORT_DIR, STARTED_AT, CREATED_BY)
            OUTPUT inserted.RUN_ID
            VALUES (:rid, :sc, :ts, 'running', :ed, SYSUTCDATETIME(), :cb)
        """), {"rid": report_id, "sc": session_code, "ts": trigger_source,
               "ed": export_dir, "cb": created_by}).fetchone()
    return int(row[0])


def _finalize_run(run_id: int, report_id: int, status: str,
                  files: List[Dict], errors: List[Dict],
                  duration_ms: int) -> None:
    engine = get_data_engine()
    total_rows = sum(int(f.get("rows", 0) or 0) for f in files)
    with engine.begin() as conn:
        conn.execute(text(f"""
            UPDATE {RUNS_TABLE} SET
                STATUS = :st, FILES = :files, ERRORS = :errs,
                ROW_COUNT = :rc, COMPLETED_AT = SYSUTCDATETIME(),
                DURATION_MS = :dur
            WHERE RUN_ID = :id
        """), {"st": status, "files": json.dumps(files),
               "errs": json.dumps(errors), "rc": total_rows,
               "dur": duration_ms, "id": run_id})
        conn.execute(text(f"""
            UPDATE {REPORTS_TABLE} SET
                LAST_RUN_AT = SYSUTCDATETIME(), LAST_STATUS = :st,
                UPDATED_AT = SYSUTCDATETIME()
            WHERE REPORT_ID = :rid
        """), {"st": status, "rid": report_id})


def run_report(report: Dict[str, Any], trigger_source: str,
               session_code: Optional[str] = None,
               created_by: Optional[str] = None) -> Dict[str, Any]:
    """Execute a report definition. Returns the run manifest.

    Never raises for step-level failures — those land in manifest["errors"] and
    the run is marked 'failed'. Raises only for a malformed definition.
    """
    report_id = int(report["REPORT_ID"])
    session_code = session_code or make_session_code()
    steps = _parse_steps(report.get("STEPS"))
    file_format = (report.get("FILE_FORMAT") or "csv").lower()
    base_dir = report.get("BASE_DIR") or None
    output_type = (report.get("OUTPUT_TYPE") or "folder").lower()
    split_config = _parse_split(report.get("SPLIT_CONFIG"))
    per_run = bool(report.get("FOLDER_PER_RUN", 1))

    export_dir = make_session_dir(session_code, base_dir, per_run=per_run)
    run_id = _insert_run(report_id, session_code, export_dir,
                         trigger_source, created_by)
    token = _register_run(run_id, report_id)

    # For event-triggered runs, suffix output files with the triggering session
    # id so each export is tied to the process run that fired it.
    event_suffix = session_code if str(trigger_source).startswith("event:") else None

    started = time.monotonic()
    files: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    cancelled = False

    try:
        for idx, step in enumerate(steps, start=1):
            if token.cancelled:
                cancelled = True
                break
            label = f"step {idx} ({step['type']}:{step['name']})"
            try:
                if step["type"] == "sql":
                    files.extend(run_procedure_to_files(
                        {"name": step["name"], "params": step["params"]},
                        export_dir, file_format, split_config, cancel_token=token,
                        output_name=step.get("label"), name_suffix=event_suffix))
                elif step["type"] == "query":
                    # Raw read-only SQL pasted into the report. step["name"] is
                    # the output file label; the SQL lives in params.sql.
                    files.extend(run_query_to_files(
                        step["params"].get("sql", ""), step["name"],
                        export_dir, file_format, split_config, cancel_token=token,
                        name_suffix=event_suffix))
                else:  # code
                    ctx = StepContext(
                        session_code=session_code, export_dir=export_dir,
                        params=step["params"], prior_files=list(files))
                    files.extend(run_code_step(step["name"], ctx))
            except Exception as e:
                # A cancel interrupts the running query with a DB error — treat
                # it as cancellation, not a failure.
                if token.cancelled:
                    cancelled = True
                    break
                logger.error(f"[report {report_id}] {label} failed: {e}")
                errors.append({"step": idx, "type": step["type"],
                               "name": step["name"], "error": str(e)})
    finally:
        _unregister_run(run_id)

    # Optional Snowflake push (deferred feature — records a clear error if the
    # connector or config is missing, rather than crashing the run).
    # Skip Snowflake + email entirely if the run was cancelled.
    if not cancelled and output_type in ("snowflake", "both"):
        try:
            from app.services.snowflake_sync import sync_report_to_snowflake
            sf_result = sync_report_to_snowflake(report, export_dir, files)
            files.extend(sf_result.get("files", []))
            errors.extend(sf_result.get("errors", []))
        except Exception as e:
            logger.error(f"[report {report_id}] snowflake sync failed: {e}")
            errors.append({"step": "snowflake", "error": str(e)})

    # Snapshot the RUN outcome BEFORE email bookkeeping: whether steps errored
    # and how many real output files they produced. Email/alert entries get
    # appended to `files` below and must not affect the pass/fail decision.
    run_had_errors = bool(errors)
    output_count = len(files)

    email_cfg = _parse_json(report.get("EMAIL_CONFIG"))
    report_name = report.get("NAME") or f"report {report_id}"

    # Optional email delivery — convert output to the chosen format and send.
    if not cancelled and email_cfg and email_cfg.get("enabled") and files:
        try:
            from app.services.report_delivery import send_report_email
            mail = send_report_email(email_cfg, report_name, files, export_dir, session_code)
            if mail.get("sent"):
                files.append({"procedure": "email", "email": mail, "rows": 0})
            else:
                errors.append({"step": "email", "error": mail.get("error", "send failed")})
        except Exception as e:
            logger.error(f"[report {report_id}] email delivery failed: {e}")
            errors.append({"step": "email", "error": str(e)})

    # Failure notification — alert recipients when the run had errors.
    if not cancelled and email_cfg and email_cfg.get("enabled") and email_cfg.get("notify_on_fail") and run_had_errors:
        try:
            from app.services.report_delivery import send_failure_email
            alert = send_failure_email(email_cfg, report_name, session_code, errors)
            if alert.get("sent"):
                files.append({"procedure": "failure_alert", "email": alert, "rows": 0})
        except Exception as e:
            logger.error(f"[report {report_id}] failure alert failed: {e}")

    duration_ms = int((time.monotonic() - started) * 1000)
    # cancelled > failed > completed. Failed = errored with no real output.
    if cancelled:
        db_status = "cancelled"
        errors.append({"step": "cancel", "error": "run cancelled by user"})
    elif run_had_errors and output_count == 0:
        db_status = "failed"
    else:
        db_status = "completed"
    _finalize_run(run_id, report_id, db_status, files, errors, duration_ms)

    logger.info(f"[report {report_id}] {db_status}: {len(files)} files, "
                f"{len(errors)} errors, {duration_ms} ms, dir={export_dir}")
    return {"run_id": run_id, "report_id": report_id,
            "session_code": session_code, "export_dir": export_dir,
            "status": db_status, "files": files, "errors": errors,
            "duration_ms": duration_ms}
