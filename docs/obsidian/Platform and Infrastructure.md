---
title: Platform and Infrastructure
tags: [ars, platform, infra, auth, rbac, rls]
updated: 2026-07-17
---

# Platform and Infrastructure

The cross-cutting layer under the [[Pipeline Overview|pipeline]]: FastAPI + SQLAlchemy (pyodbc) + SQL Server, React SPA served static in prod. Two DBs on `HOPC866`: **Claude** (system) + **Rep_Data** (data).

## App bootstrap (`backend/main.py`)
Lifespan startup: log DB target → `check_db_connection` → `enable_rcsi()` (READ_COMMITTED_SNAPSHOT so readers don't block uploads) → `prewarm_pend_alc_tables` (pre-create hot tables to avoid Sch-M DDL locks) → register models + `create_all` + `reconcile_columns` (ALTER ADD only) → seed super-admin + permissions → mark crashed `MSAStorageJob`s failed → **start daemons: `tempdb_cleaner`, `report_scheduler`**. Middleware order: CORS → request-logging → `auto_free_space_middleware` (post-job DB cleanup) → global exception handler. Static SPA catch-all serves `index.html`. `uvicorn` workers = `1 if DEBUG else 4`.

## Router map (`/api/v1`)
`/auth` `/users` `/roles` `/rls` `/audit` `/tables` `/data-ops` `/upload` · `/msa-stock` `/msa` · `/contrib` `/auto-contrib` `/bdc` · `/settings` · `/sloc-validation` `/grid-builder` `/merge-rules` `/data-dictionary` · `/lookup-art-master` · `/dashboard` `/hold-dashboard` `/ars-dashboard` `/gap-report` · `/pend-alc` `/checklist` `/trends` · `/reports` `/report-gen` · `/listing` · `/maintenance` `/pipeline` `/allocation-engine` `/project-tracker` `/activity-log`.

## Auth & security
JWT (HS256, `app/security/jwt_handler.py`). Login: lookup → bcrypt verify → lock at `MAX_LOGIN_ATTEMPTS`=5 → issue access (480 min: sub/user_id/roles/permissions) + refresh (7 days) → audit. `get_current_user` retries once on a SQL connection drop with a fresh session. **Superadmin is force-unlocked AND its password re-synced from `SUPER_ADMIN_PASSWORD` on every startup** — you can't change it via UI.

## RBAC (`app/models/rbac.py`)
`rbac_roles/permissions/role_permissions/users/user_roles`, eager-loaded. `User.permissions` = union across active roles. Gating dependencies: `RequireRoles([...])` (any listed role) and `RequirePermissions([...])` (all listed perms) — **`SUPER_ADMIN` bypasses both**. Permissions seeded idempotently (~22 codes: `DATA_UPLOAD`, `ADMIN_*`, `TABLE_*`, `MSA_*`, etc.).

## RLS (`app/models/rls.py`)
`rls_stores` + `rls_user_{store,region,category}_access` + `rls_column_restrictions` + `rls_table_role_access`. `get_rls_context` builds an `RLSContext`: SUPER_ADMIN/ADMIN unrestricted; else union of direct store access + region-resolved stores; category access from `UserCategoryAccess`. Non-admin with zero stores+categories → 403. Enforced across MSA/Listing/Allocation reads. Column-level: most-restrictive-wins. **Caveat:** `get_category_sql_filter` uses string interpolation, not bound params.

## Upload subsystem
Sync `/upload/` → `FileUploadService` → `UpsertEngine.upsert` (chunked 10k, row audit). Async `/upload/async` → `upload_job_service` (single daemon + in-memory `queue.Queue`, FIFO, one at a time). Cell semantics: blank→`__SKIP__` (keep), `"NA"`→literal, `|`/`-`→`__NULL__`. **Typed-table guard** (`TYPED_TABLES`) rejects generic upload of pool-governed tables (`ARS_PEND_ALC`, `ARS_MSA_*`, hold/parked/alloc-history). Uploading a `Master_CONT_<col>` parent triggers a `derived_masters` rebuild.

## Settings (`settings.py` + `config.py`)
`app_settings.json` is the UI-managed source of truth (blocks: database, email/SMTP, snowflake, application, ui, allocation). Precedence **JSON → env/.env → class defaults**. `POST /settings/database/apply` hot-swaps engine pools without restart (probe → persist JSON+.env → `reload_db_engines()` → verify). SMTP/Snowflake configured here. Backups via `BACKUP DATABASE ... WITH COMPRESSION`.

## Utility subsystems
- **Table Management** (`tables.py` + `table_mgmt_service.py`) — UI CREATE/ALTER/soft-delete on Data DB, registered in `sys_*_registry`; `PROTECTED_TABLES` can't be altered; export jobs, background truncate jobs, `table_permissions` gating Data Editor.
- **Data Dictionary** (`data_dictionary.py`) — `ARS_DATA_DICTIONARY` CRUD, self-seeding ~30 core column defs.
- **Checklist** (`checklist.py`) — `ARS_CHECKLIST`, tables the planner must keep updated pre-allocation.
- **Activity Log** (`activity_log.py`, superadmin) — rolls `audit_log` + git commits into daily YES/NO-reviewable pointers.
- **[[Report Generation Hub]]** — scheduler daemon started here.
- **Reset service** (`reset_service.py`) — `reset_transactional_data`: rediscovers tables via INFORMATION_SCHEMA, classifies transactional (pattern/explicit) vs protected (**protected always wins**), TRUNCATE/DELETE + reseed. Superadmin only.
- **TempDB cleaner** (`tempdb_cleanup_service.py`) — daemon; skipped on Azure SQL DB.

## Config & env (`config.py`)
System DB `HOPC866`/`Claude`/`sa`; Data DB `Rep_data`. Pool 15/overflow 25/recycle 300/pre_ping. JWT access 480 min / refresh 7 days. `MAX_LOGIN_ATTEMPTS`=5. Upload 100 MB / chunk 10k. `MSA_ALLOWED_ATT_TYP=["00","02"]`. Auto-free-after-job on heavy endpoints. **Hardcoded default secrets** (`DB_PASSWORD`, `SUPER_ADMIN_PASSWORD=Admin@12345`, placeholder `JWT_SECRET_KEY`) — must override in prod.

## Gotchas
See [[Known Risks and Doc Drift]] — superadmin password re-sync, broken backup path (`settings.SQL_DATABASE` undefined), `get_system_info` queries non-existent `users` table, RLS string-interpolation, async upload single global thread, report-scheduler settings keys undefined (always default 30s/2).

## Cross-links
[[Frontend Map]] · [[Data Model]] · [[ARS Work Log]].
