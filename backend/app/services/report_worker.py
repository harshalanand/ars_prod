r"""
Standalone report worker — runs ONE report in its own OS process.

The scheduler launches this (via `python -m app.services.report_worker <payload>`)
so heavy report generation — multi-million-row procs, CSV/pandas writing — runs
in a SEPARATE process and never holds the API worker's GIL. The web UI stays
responsive while a report generates; the OS schedules the worker on another core.

payload.json = {"report": {...}, "trigger_source": "manual",
                "session_code": "...", "user": "..."}

Exit code 0  = run_report finalized the ARS_REPORT_RUNS row itself.
Exit code !=0 / killed = the parent scheduler marks the run failed (or, if it
terminated the worker for a cancel, cancelled).

Importing this module pulls in report_engine + database.session (which builds
fresh SQLAlchemy engines in this process) but NOT main/app — so no second
scheduler or FastAPI app starts in the child.
"""
import json
import os
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: report_worker <payload.json>", file=sys.stderr)
        return 2
    try:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:  # noqa - surface a clear reason on stderr
        print(f"report_worker: could not read payload: {e}", file=sys.stderr)
        return 2

    from loguru import logger
    from app.services.report_engine import run_report

    report = payload.get("report") or {}
    rid = report.get("REPORT_ID")
    logger.info(f"[report-worker] pid={os.getpid()} report={rid} "
                f"session={payload.get('session_code')} — starting")
    try:
        run_report(report, payload.get("trigger_source") or "manual",
                   payload.get("session_code"), payload.get("user"))
        logger.info(f"[report-worker] report {rid} finished")
        return 0
    except Exception as e:
        logger.exception(f"[report-worker] report {rid} crashed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
