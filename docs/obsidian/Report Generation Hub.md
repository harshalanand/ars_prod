---
title: Report Generation Hub
tags: [ars, reports, scheduler]
updated: 2026-07-17
---

# Report Generation Hub

Scheduled / manual / event-triggered reporting. Part of the [[ARS Work Log]]. Implemented 2026-07-09, extended through 2026-07-10. Source: `report_gen.py` (API, `/report-gen`), `report_engine.py`, `report_scheduler_service.py`, `data_export_service.py`, `report_code_steps.py`, `report_delivery.py`, `snowflake_sync.py`.

## Model
A **report** = ordered **steps** + an **output** + a **trigger**.

- **Steps:** `sql` (stored proc → files, `nextset()` for multi result-set), `query` (raw read-only SELECT, guarded by `_DESTRUCTIVE_RE`), or `code` (SAFE registry via `@register_code_step`, e.g. `build_manifest`). Per-step `label` overrides the output filename.
- **Output ("Deliver to" chips):** Folder / Snowflake / Email → `OUTPUT_TYPE` folder|snowflake|both|email. Folder = `<base>/data/<YYYYMMDD>[/<session_code>]`; `FOLDER_PER_RUN` toggles fresh-vs-reused. Progressive/adaptive `SPLIT_CONFIG` (SEG→DIV→SUB_DIV→MAJ_CAT, then size-chunk `_partN`).
- **Triggers:** `schedule` (once/daily/weekly/monthly/every-N-hours; multiple times/weekdays/dates; all UTC), `manual` (`POST /reports/{id}/run`), `event` (`emit_event(name)`). Known events: `listing.approved` + `pendalc.approved` (from [[Listing]] approve), `msa.completed` (from [[MSA Stock Calculation]] job), `autocont.completed` (from [[Contribution and CONT|auto-contrib]]). Event runs get a `_<session_id>` filename suffix.

## Tables (Rep_Data, self-healing via `ensure_report_tables()`)
- `ARS_REPORTS` — definition: `STEPS` json, `OUTPUT_TYPE`, `BASE_DIR`, `FILE_FORMAT`, `FOLDER_PER_RUN`, `SNOWFLAKE/SPLIT/EMAIL_CONFIG`, `TRIGGER_TYPE`, `SCHEDULE_CONFIG`, `TRIGGER_EVENT`, `ENABLED`, `NEXT_RUN_AT`, `LAST_*`.
- `ARS_REPORT_RUNS` — run history: `SESSION_CODE`, `TRIGGER_SOURCE`, `STATUS`, `EXPORT_DIR`, files/errors json, `ROW_COUNT`, timings.

## Scheduler daemon
Singleton daemon thread; 30s tick; **atomically claims** due reports via `UPDATE ... WHERE NEXT_RUN_AT <= now` (safe across 4 uvicorn workers); runs on `ThreadPoolExecutor(max_parallel=2)`; an already-running report is skipped (logged), not stacked. `reconcile_orphaned_runs()` at startup marks stuck `running` rows failed. `compute_next_run` supports all cadences, UTC.

## Run engine
`run_report`: makes the session folder, inserts a `running` run row, executes steps collecting files+errors, optional Snowflake push + email delivery/failure alert, finalizes status (`cancelled`>`failed`>`completed`; failed = errored + zero output). **Cancellation** via `CancelToken` binding the live pyodbc cursor → `cursor.cancel()` (verified 25s→~3s).

## Delivery
`report_delivery.py` converts CSV → xlsx (openpyxl) / pdf (fpdf2) / docx (python-docx), zips if >1, sends via stdlib `smtplib`. SMTP read from Settings→Email (shared with the Settings "Send Test").

## Still TODO
- Install `snowflake-connector-python[pandas]` on the server to activate sync.
- SMTP: O365 basic-auth blocked (535 5.7.139) — needs Authenticated SMTP / App Password / relay.
- No RBAC permission gate on the page yet. Split UI section in `ReportForm` not finished (backend fully supports it).

## Cross-links
[[Listing]] / [[Pending Allocation and Hold]] / [[MSA Stock Calculation]] (event sources) · [[Platform and Infrastructure]] (scheduler starts in `main.py` lifespan).
