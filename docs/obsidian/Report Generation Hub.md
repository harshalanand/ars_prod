---
title: Report Generation Hub
tags: [ars, reports, scheduler]
updated: 2026-07-30
---

# Report Generation Hub

Scheduled / manual / event-triggered reporting. Part of the [[ARS Work Log]]. Implemented 2026-07-09, extended through 2026-07-27 (Snowflake shared config), with reliability hardening on 2026-07-20 (streaming export, process isolation, storage guardrails). Source: `report_gen.py` (API, `/report-gen`), `report_engine.py`, `report_scheduler_service.py`, `data_export_service.py`, `report_code_steps.py`, `report_delivery.py`, `snowflake_sync.py`.

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

### WhatsApp Configuration (2026-07-20)
DB-backed WhatsApp settings module with encryption at rest. Two tables (`APP_WHATSAPP_SETTINGS`, `APP_WHATSAPP_AUDIT_LOG`); service `whatsapp_settings_service.py` (get/save config, test_connection, verify_template, send_test_message); API `/settings/whatsapp` (RBAC: `ADMIN_SETTINGS` + `SUPER_ADMIN` for token modify); access token encrypted via [[project_secret_encryption|app/core/crypto.py]] (Fernet, key `APP_ENC_KEY`). Delivery wiring: `report_delivery._provider_cfg('whatsapp')` reads decrypted DB config first, fallback to `app_settings.json`. Verified: 16/16 service tests (encryption, masking, delivery reads decrypted, mocked API calls) + live API + UI (all settings sections). See [[project_report_generation_hub]] memory for full FSD.

### Large-data streaming export (2026-07-20)
Fixed "Unable to allocate 3+ GiB" OOM on multi-million-row procedures (reports 70's USP_PARK_TEMP ~4.5M rows, USP_VAR_ART_ALC_RPT ~7M rows). Root cause: `data_export_service` built entire DataFrame via `cursor.fetchall()` before splitting; now **peaks at 250k rows** in memory regardless of total. `_export_one_resultset()` peeks at result-set size: ≤250k → in-memory hierarchy split (as before); >250k → streaming to CSV/xlsx (csv.writer, batched writes, memory-bounded). Result naming via `_RS_TOKEN` placeholder (single RS → no suffix; multi → `_rsN`). Verified: 4/4 live DB (300k→3 files, peak heap 48.5 MB vs old 3+ GiB). Related: [[project_report_generation_hub]] for full detail.

### Adaptive split-by-hierarchy fix (2026-07-20)
Streaming path originally ignored product/store hierarchy split config, only used row-count partitions. Added `_stream_grouped_split()` with two-pass local spill: Pass 1 counts full-hierarchy groups in a temp CSV (not the export share); Pass 2 re-reads and routes rows to group files via LRU-bounded `_GroupedCsvWriter`. **CRITICAL fix:** split threshold is NOW governed SOLELY by `max_rows` (user's "Max rows per file" setting), NOT the 250k memory cap. ARS_GRID_MJ_MERGE_RNG_SEG (~310k rows, max_rows=1M) was wrongly split into 4 SEG files; now correctly produces 1 file. Verified: 12/12 unit + 4/4 live (300k→10 SEG×MAJ_CAT files correct).

### Process isolation (2026-07-20)
Reports now run in a **separate OS process** (`app/services/report_worker.py`) launched via `subprocess.Popen`, not in-thread. Root cause of freeze: ThreadPoolExecutor thread inside uvicorn worker held the GIL during CPU-bound pandas/CSV work → single dev worker couldn't serve HTTP (health/login didn't respond >25s). Now: scheduler writes payload to temp JSON, launches worker process (detached: `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`), controller thread just `proc.wait()`s (GIL-free). Tracking: `_procs {report_id→Popen}`; `/status running_report_ids` now lists reports with live workers (not in-process tokens). Cancel: marks run 'cancelled' in DB then `proc.terminate()`s the worker. Dead-worker reconcile marks stuck 'running' rows 'failed' on next reconcile. Verified: worker selftest (separate pid, correct final filenames, exit 0) + live (health latency ~0.22s while generating vs >25s frozen before). GOTCHA: multi-worker prod cancellation may not reach a Popen in a different uvicorn worker (pre-existing) — a `CHILD_PID` column + `os.kill` would fix it (not done).

### Storage guardrails (2026-07-20)
Added retention cleanup + disk pre-flight checks to prevent "[Errno 28] No space left on device" on report output share (\\File\...\98-ALC_REPORT was filled by old `FOLDER_PER_RUN` dated folders accumulating). `cleanup_old_run_folders()` deletes `<base>/data/<YYYYMMDD>` folders older than `retention_days`; `assert_free_space()` raises if free disk < `min_free_mb`. `report_engine.run_report` calls `_storage_maintenance()` BEFORE `make_session_dir` (frees room first), then `assert_free_space()` AFTER inserting the run — if low, run is finalized 'failed' with a preflight error. Config in `app_settings.json` 'reports' {`retention_days:7`, `min_free_mb:500`, `cleanup_enabled:true`} (tunable, no restart). GOTCHA: retention is **destructive by design** — old report output is auto-deleted; disable via `cleanup_enabled:false` or `retention_days:0`. Verified: 10/10 storage unit tests + live report run still completes with pruned folders.

## Still TODO (2026-07-20)
- Install `snowflake-connector-python[pandas]` on the server to activate Snowflake sync (already in requirements.txt, just needs server deploy).
- SMTP: O365 basic-auth blocked (535 5.7.139) — needs Authenticated SMTP / App Password / relay.
- WhatsApp / SMS live credentials not yet entered in Settings (built graceful-until-configured).
- WhatsApp inbound webhook (delivery receipts) not built — Webhook Status always 'Not Configured'.
- SMS could migrate to DB+encrypted module like WhatsApp if desired (currently uses app_settings.json).
- No RBAC permission gate on the page yet. Split UI form controls in `ReportForm` still to add (backend fully supports splitting).
- Multi-worker prod cancel: would need DB flag + `CHILD_PID` column + `os.kill` to cancel in different uvicorn worker.

## Cross-links
[[Listing]] / [[Pending Allocation and Hold]] / [[MSA Stock Calculation]] (event sources) · [[Platform and Infrastructure]] (scheduler starts in `main.py` lifespan) · [[project_secret_encryption]] (encryption for WhatsApp token) · [[Reports]] (stored procs / Grid Report / MSA Master) · [[2026-07-20]]
