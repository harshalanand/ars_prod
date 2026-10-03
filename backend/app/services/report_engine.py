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
import os
import shutil
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine
from app.services.data_export_service import (
    make_session_dir,
    fanout_param_runs,
    run_procedure_to_files,
    run_query_to_files,
    cleanup_old_run_folders,
    assert_free_space,
    DEFAULT_BASE_DIR,
)
from app.services.report_code_steps import StepContext, run_code_step

REPORTS_TABLE = "ARS_REPORTS"
RUNS_TABLE = "ARS_REPORT_RUNS"


def _report_storage_cfg() -> Dict[str, Any]:
    """Storage guardrail settings (retention/min-free), tunable in app_settings
    → 'reports'. Falls back to safe defaults if unavailable."""
    cfg = {"retention_days": 7, "min_free_mb": 500, "cleanup_enabled": True}
    try:
        from app.api.v1.endpoints.settings import load_app_settings
        cfg.update((load_app_settings() or {}).get("reports", {}) or {})
    except Exception:
        pass
    return cfg


def _storage_maintenance(base_dir: Optional[str]) -> None:
    """Prune old run-date folders so the output location doesn't fill up."""
    cfg = _report_storage_cfg()
    if not cfg.get("cleanup_enabled", True):
        return
    base = base_dir or DEFAULT_BASE_DIR
    try:
        res = cleanup_old_run_folders(base, cfg.get("retention_days", 7))
        if res.get("deleted"):
            shown = res["deleted"][:5]
            more = "…" if len(res["deleted"]) > 5 else ""
            logger.info(f"[report] retention: pruned {len(res['deleted'])} old run "
                        f"folder(s) from {base} ({', '.join(shown)}{more})")
        for e in res.get("errors", [])[:3]:
            logger.warning(f"[report] retention cleanup issue: {e}")
    except Exception as e:
        logger.warning(f"[report] storage maintenance skipped: {e}")


def _cleanup_local_output(export_dir: str, files: List[Dict[str, Any]],
                          per_run: bool) -> int:
    """Delete this run's local output after a successful Snowflake upload.
    per_run → remove the whole session folder; otherwise (shared date folder)
    remove only the files THIS run wrote. Never raises; returns items removed."""
    removed = 0
    try:
        if per_run and export_dir and os.path.isdir(export_dir):
            shutil.rmtree(export_dir, ignore_errors=True)
            return 1
        for f in files:
            p = f.get("file")
            if p and os.path.isfile(p):
                try:
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass
    except Exception as e:
        logger.warning(f"[report] local cleanup after upload failed: {e}")
    return removed


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


STEP_TIMING_TABLE = "ARS_REPORT_STEP_TIMING"


def _ensure_progress_schema() -> None:
    """Self-heal: progress columns on the run row + a per-step timing table used
    to weight the completion %. Idempotent; never raises."""
    try:
        eng = get_data_engine()
        with eng.begin() as c:
            for col, ddl in (("PROGRESS_PCT", "INT"),
                             ("PROGRESS_STEP", "NVARCHAR(300)"),
                             ("PROGRESS_TOTAL", "INT")):
                c.execute(text(f"""
                    IF NOT EXISTS (SELECT 1 FROM sys.columns
                        WHERE object_id = OBJECT_ID('dbo.{RUNS_TABLE}') AND name = '{col}')
                    ALTER TABLE dbo.{RUNS_TABLE} ADD {col} {ddl} NULL
                """))
            c.execute(text(f"""
                IF OBJECT_ID('dbo.{STEP_TIMING_TABLE}', 'U') IS NULL
                CREATE TABLE dbo.{STEP_TIMING_TABLE} (
                    REPORT_ID  INT           NOT NULL,
                    STEP_IDX   INT           NOT NULL,
                    STEP_NAME  NVARCHAR(300) NULL,
                    LAST_MS    INT           NULL,
                    UPDATED_AT DATETIME2     NULL DEFAULT SYSDATETIME(),
                    CONSTRAINT PK_{STEP_TIMING_TABLE} PRIMARY KEY (REPORT_ID, STEP_IDX)
                )
            """))
    except Exception as e:
        logger.debug(f"[report] progress schema ensure skipped: {e}")


def _load_step_weights(report_id: int, n: int) -> List[float]:
    """Per-step time weights from the last run (ms), so the % advances in
    proportion to how long each step actually takes. Equal weights if no history."""
    weights = [1.0] * n
    try:
        eng = get_data_engine()
        with eng.connect() as c:
            rows = c.execute(text(
                f"SELECT STEP_IDX, LAST_MS FROM {STEP_TIMING_TABLE} WHERE REPORT_ID=:r"),
                {"r": report_id}).fetchall()
        m = {int(i): float(ms) for i, ms in rows if ms}
        if m:
            weights = [max(m.get(i + 1, 1.0), 1.0) for i in range(n)]
    except Exception as e:
        logger.debug(f"[report] step-weight load skipped: {e}")
    return weights


def _save_step_timing(report_id: int, idx: int, name: str, ms: int) -> None:
    try:
        eng = get_data_engine()
        with eng.begin() as c:
            c.execute(text(f"""
                MERGE {STEP_TIMING_TABLE} AS t
                USING (SELECT :r AS REPORT_ID, :i AS STEP_IDX) AS s
                ON t.REPORT_ID=s.REPORT_ID AND t.STEP_IDX=s.STEP_IDX
                WHEN MATCHED THEN UPDATE SET LAST_MS=:ms, STEP_NAME=:n, UPDATED_AT=SYSDATETIME()
                WHEN NOT MATCHED THEN INSERT (REPORT_ID, STEP_IDX, STEP_NAME, LAST_MS)
                    VALUES (:r, :i, :n, :ms);
            """), {"r": report_id, "i": idx, "n": name[:300], "ms": int(ms)})
    except Exception as e:
        logger.debug(f"[report] step-timing save skipped: {e}")


def _update_progress(run_id: int, pct: int, step_label: str, total: int) -> None:
    """Write live progress to the run row (read by the UI's 5s poll). Never raises."""
    try:
        eng = get_data_engine()
        with eng.begin() as c:
            c.execute(text(f"""
                UPDATE {RUNS_TABLE} SET PROGRESS_PCT=:p, PROGRESS_STEP=:s, PROGRESS_TOTAL=:t
                WHERE RUN_ID=:id
            """), {"p": int(pct), "s": (step_label or "")[:300], "t": int(total), "id": run_id})
    except Exception as e:
        logger.debug(f"[report] progress update skipped: {e}")


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
        # Stamp STARTED_AT from the APP clock (same source as session_code) so
        # run times match the session code and the user's wall clock, regardless
        # of any clock skew on the DB server.
        row = conn.execute(text(f"""
            INSERT INTO {RUNS_TABLE}
                (REPORT_ID, SESSION_CODE, TRIGGER_SOURCE, STATUS,
                 EXPORT_DIR, STARTED_AT, CREATED_BY)
            OUTPUT inserted.RUN_ID
            VALUES (:rid, :sc, :ts, 'running', :ed, :started, :cb)
        """), {"rid": report_id, "sc": session_code, "ts": trigger_source,
               "ed": export_dir, "cb": created_by,
               "started": datetime.now()}).fetchone()
    return int(row[0])


def _finalize_run(run_id: int, report_id: int, status: str,
                  files: List[Dict], errors: List[Dict],
                  duration_ms: int) -> None:
    engine = get_data_engine()
    # Count rows from the actual exported FILES only. Delivery summaries appended
    # to `files` (Snowflake upload / email / whatsapp / sms) carry their own
    # "rows" and would otherwise double-count (e.g. a Snowflake report showed 2×
    # — once for the CSV, once for the upload of the same rows). In DIRECT mode
    # there is no CSV, so fall back to the Snowflake upload ("target") entries.
    file_rows = sum(int(f.get("rows", 0) or 0) for f in files if f.get("file"))
    total_rows = file_rows or sum(int(f.get("rows", 0) or 0) for f in files if f.get("target"))
    now = datetime.now()   # app clock — consistent with STARTED_AT & session code
    with engine.begin() as conn:
        conn.execute(text(f"""
            UPDATE {RUNS_TABLE} SET
                STATUS = :st, FILES = :files, ERRORS = :errs,
                ROW_COUNT = :rc, COMPLETED_AT = :now,
                DURATION_MS = :dur
            WHERE RUN_ID = :id
        """), {"st": status, "files": json.dumps(files),
               "errs": json.dumps(errors), "rc": total_rows,
               "dur": duration_ms, "id": run_id, "now": now})
        conn.execute(text(f"""
            UPDATE {REPORTS_TABLE} SET
                LAST_RUN_AT = :now, LAST_STATUS = :st,
                UPDATED_AT = :now
            WHERE REPORT_ID = :rid
        """), {"st": status, "rid": report_id, "now": now})


def run_report(report: Dict[str, Any], trigger_source: str,
               session_code: Optional[str] = None,
               created_by: Optional[str] = None) -> Dict[str, Any]:
    """Execute a report definition. Returns the run manifest.

    Never raises for step-level failures — those land in manifest["errors"] and
    the run is marked 'failed'. Raises only for a malformed definition.
    """
    report_id = int(report["REPORT_ID"])
    session_code = session_code or make_session_code()
    _ensure_progress_schema()
    steps = _parse_steps(report.get("STEPS"))
    file_format = (report.get("FILE_FORMAT") or "csv").lower()
    base_dir = report.get("BASE_DIR") or None
    output_type = (report.get("OUTPUT_TYPE") or "folder").lower()
    split_config = _parse_split(report.get("SPLIT_CONFIG"))
    per_run = bool(report.get("FOLDER_PER_RUN", 1))

    # DIRECT Snowflake mode: for a snowflake-ONLY report (unless direct=false),
    # stream each step straight from SQL Server into Snowflake with NO CSV export.
    sf_cfg = _parse_json(report.get("SNOWFLAKE_CONFIG")) or {}
    # Default = the fast CSV pipeline (write CSV → upload). 'direct' (stream with
    # no CSV) is OPT-IN — it's slower because it processes rows in Python instead
    # of pandas' C-optimised to_csv/read_csv.
    direct_sf = (output_type == "snowflake") and bool(sf_cfg.get("direct", False))

    if direct_sf:
        # No file output → no export folder, no retention/disk pre-flight.
        export_dir = ""
        run_id = _insert_run(report_id, session_code, export_dir,
                             trigger_source, created_by)
        token = _register_run(run_id, report_id)
    else:
        # Retention cleanup FIRST — prune old run folders so this run has room.
        _storage_maintenance(base_dir)
        export_dir = make_session_dir(session_code, base_dir, per_run=per_run)
        run_id = _insert_run(report_id, session_code, export_dir,
                             trigger_source, created_by)
        token = _register_run(run_id, report_id)
        # Disk pre-flight — fail fast (with the run recorded) if the output volume
        # is too low, instead of producing a half-written report.
        try:
            assert_free_space(export_dir, _report_storage_cfg().get("min_free_mb", 500))
        except Exception as e:
            _unregister_run(run_id)
            errs = [{"step": "preflight", "error": str(e)}]
            _finalize_run(run_id, report_id, "failed", [], errs, 0)
            logger.error(f"[report {report_id}] pre-flight failed: {e}")
            return {"run_id": run_id, "report_id": report_id, "session_code": session_code,
                    "export_dir": export_dir, "status": "failed", "files": [],
                    "errors": errs, "duration_ms": 0}

    # For event-triggered runs, suffix output files with the triggering session
    # id so each export is tied to the process run that fired it.
    event_suffix = session_code if str(trigger_source).startswith("event:") else None

    started = time.monotonic()
    files: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    cancelled = False

    if direct_sf:
        _unregister_run(run_id)   # direct mode has no per-step cancel token
        try:
            from app.services.snowflake_sync import sync_report_direct_to_snowflake
            res = sync_report_direct_to_snowflake(report, session_code)
            files.extend(res.get("files", []))
            errors.extend(res.get("errors", []))
        except Exception as e:
            logger.error(f"[report {report_id}] snowflake direct sync failed: {e}")
            errors.append({"step": "snowflake_direct", "error": str(e)})
    else:
        # Completion % — weight each step by its last run's duration so the bar
        # advances in proportion to real time; equal weights on the first run.
        n_steps = len(steps)
        weights = _load_step_weights(report_id, n_steps)
        total_w = sum(weights) or 1.0
        done_w = 0.0
        try:
            for idx, step in enumerate(steps, start=1):
                if token.cancelled:
                    cancelled = True
                    break
                _update_progress(run_id, int(done_w / total_w * 100),
                                 f"{idx}/{n_steps}: {step['name']}", n_steps)
                step_t0 = time.monotonic()
                label = f"step {idx} ({step['type']}:{step['name']})"
                try:
                    if step["type"] == "sql":
                        # A FANOUT param (e.g. usp_ars_msa_master.@Level) with a comma
                        # list expands into one run per value -> a SEPARATE output file
                        # suffixed with the value (e.g. MSA_OP_CL_DETAIL, _GEN_CLR, ...).
                        for run_params, fo_sfx in fanout_param_runs(step["name"], step["params"]):
                            if token.cancelled:
                                cancelled = True
                                break
                            sfx = "_".join([p for p in (fo_sfx, event_suffix) if p]) or None
                            files.extend(run_procedure_to_files(
                                {"name": step["name"], "params": run_params},
                                export_dir, file_format, split_config, cancel_token=token,
                                output_name=step.get("label"), name_suffix=sfx))
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
                    if not cancelled:
                        _save_step_timing(report_id, idx, step["name"],
                                          int((time.monotonic() - step_t0) * 1000))
                        done_w += weights[idx - 1]
        finally:
            _unregister_run(run_id)

        # File-based Snowflake push (reads the CSVs produced above).
        if not cancelled and output_type in ("snowflake", "both"):
            _update_progress(run_id, max(int(done_w / total_w * 100), 95),
                             "uploading to Snowflake", n_steps)
            sf_ok = False
            try:
                from app.services.snowflake_sync import sync_report_to_snowflake
                sf_result = sync_report_to_snowflake(report, export_dir, files, session_code)
                files.extend(sf_result.get("files", []))
                errors.extend(sf_result.get("errors", []))
                sf_ok = not sf_result.get("errors")   # every target uploaded cleanly
            except Exception as e:
                logger.error(f"[report {report_id}] snowflake sync failed: {e}")
                errors.append({"step": "snowflake", "error": str(e)})

            # Cleanup: for a snowflake-ONLY report, delete the local CSV once it's
            # safely in Snowflake. Never for 'both' (folder is the deliverable),
            # and only when the upload SUCCEEDED (keep files on failure to retry).
            if (output_type == "snowflake" and sf_ok
                    and sf_cfg.get("cleanup_after_upload", True)):
                removed = _cleanup_local_output(export_dir, files, per_run)
                files.append({"procedure": "cleanup", "rows": 0,
                              "note": f"local files deleted after upload ({removed})"})
                logger.info(f"[report {report_id}] cleaned up local output after upload")

    # Snapshot the RUN outcome BEFORE email bookkeeping: whether steps errored
    # and how many real output files they produced. Email/alert entries get
    # appended to `files` below and must not affect the pass/fail decision.
    run_had_errors = bool(errors)
    output_count = len(files)

    email_cfg = _parse_json(report.get("EMAIL_CONFIG"))
    wa_cfg = _parse_json(report.get("WHATSAPP_CONFIG"))
    sms_cfg = _parse_json(report.get("SMS_CONFIG"))
    report_name = report.get("NAME") or f"report {report_id}"

    # Message context for WhatsApp/SMS templating ({report}/{status}/{rows}/…).
    total_rows = sum(int(f.get("rows", 0) or 0) for f in files if f.get("file"))
    msg_ctx = {
        "report": report_name,
        "status": ("failed" if (run_had_errors and output_count == 0)
                   else "partial" if run_had_errors else "completed"),
        "rows": f"{total_rows:,}",
        "session": session_code,
        "when": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "files": sum(1 for f in files if f.get("file")),
    }

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

    # Optional WhatsApp delivery (Meta Cloud) — message + optional document.
    if not cancelled and wa_cfg and wa_cfg.get("enabled"):
        try:
            from app.services.report_delivery import send_whatsapp
            wa = send_whatsapp(wa_cfg, report_name, files, export_dir, session_code, msg_ctx)
            if wa.get("sent"):
                files.append({"procedure": "whatsapp", "delivery": wa, "rows": 0})
            else:
                errors.append({"step": "whatsapp", "error": wa.get("error", "send failed")})
        except Exception as e:
            logger.error(f"[report {report_id}] whatsapp delivery failed: {e}")
            errors.append({"step": "whatsapp", "error": str(e)})

    # Optional SMS summary (MSG91) — text only.
    if not cancelled and sms_cfg and sms_cfg.get("enabled"):
        try:
            from app.services.report_delivery import send_sms
            sm = send_sms(sms_cfg, report_name, msg_ctx)
            if sm.get("sent"):
                files.append({"procedure": "sms", "delivery": sm, "rows": 0})
            else:
                errors.append({"step": "sms", "error": sm.get("error", "send failed")})
        except Exception as e:
            logger.error(f"[report {report_id}] sms delivery failed: {e}")
            errors.append({"step": "sms", "error": str(e)})

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
    # cancelled > failed > partial > completed.
    #   failed   = errored and produced NO output at all.
    #   partial  = some steps produced output but at least one step errored
    #              (e.g. a GEN_ART step OOM'd) — must NOT read as green.
    #   completed= every step succeeded.
    if cancelled:
        db_status = "cancelled"
        errors.append({"step": "cancel", "error": "run cancelled by user"})
    elif run_had_errors and output_count == 0:
        db_status = "failed"
    elif run_had_errors:
        db_status = "partial"
    else:
        db_status = "completed"
    _finalize_run(run_id, report_id, db_status, files, errors, duration_ms)

    logger.info(f"[report {report_id}] {db_status}: {len(files)} files, "
                f"{len(errors)} errors, {duration_ms} ms, dir={export_dir}")
    return {"run_id": run_id, "report_id": report_id,
            "session_code": session_code, "export_dir": export_dir,
            "status": db_status, "files": files, "errors": errors,
            "duration_ms": duration_ms}
