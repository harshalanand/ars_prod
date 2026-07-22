# Dev-Server Refresh Plan — HOPC866 (prod) → ARSDBPRO (dev)

**Status:** FOR REVIEW — nothing implemented yet.
**Date:** 2026-07-20
**Direction:** one-way, prod → dev only.
**Hard rules:**
1. Prod (HOPC866) is **read-only**. No new objects, no writes on prod.
2. Every dev-side operation is **reversible** (backup + transactional; rollback on any error).

---

## 0. Current state (verified live)

- Both servers host the same DBs: `Claude`, `Rep_Data`, `Excel_Dump`; same `sa` login.
- ARSDBPRO already exists as a **stale partial clone** of HOPC866.
- Daily SAP input tables (`ET_*`, `COUNT_STOCK_DATA_18M`, `ALLOCATION_MRDC_RAW_DATA`,
  `Master_ALC_PEND`, `MASTER_ART_BROADER_MENU`) are **already identical** on both
  (SSIS package dual-loads them) — no action needed, just verify.
- Config/rule/settings tables are **stale or empty** on dev and will break alloc/listing:
  `ARS_MSA_SLOC_SETTINGS` 29→**0**, `ARS_DATA_DICTIONARY` 101→**0**,
  `ARS_SEC_CAP_GROWTH_MATRIX` 5→**0**, `ARS_STORE_SLOC_SETTINGS` 40→35, etc.
- **24 Rep_Data + 4 Claude tables missing entirely** on dev.
- A prod→dev linked server `ARSDBPRO` exists **on HOPC866** (will be left unused —
  see method below).

---

## 1. Method (the "better way")

Put **all sync machinery on dev**, so prod is never modified.

- Create ONE linked server `HOPC866` **on ARSDBPRO** (read-only login preferred; `sa`
  acceptable if the proc only does SELECT from prod).
- Sync stored proc `usp_refresh_from_prod` + SQL Agent job live **on dev**.
- The proc only `SELECT`s from prod and `INSERT`s into dev.

### Sync scope = pull everything EXCEPT (exclude list)
- **Engine outputs (dev regenerates):** `ARS_MSA_TOTAL/GEN_ART/VAR_ART`,
  `ARS_GRID_MJ*`, `ARS_LISTING*`, `ARS_ALLOC_WORKING`, `ARS_ALLOC_PARKED`,
  `ARS_pend_alc`, `ARS_LISTED_OPT`, `ARS_CALC_*`, `ARS_ONESIZE_ALLOCATION`,
  `ARS_NL_TBL_HOLD_*`, `allocation_results`, `Cont_Percentage_*`,
  `Listing_Compile_SUMMARY_CO`.
- **History / archive (huge, not needed in dev):** `*_HISTORY`,
  `ET_STORE_STOCK_ARCHIVE_DATA`, `ET_STORE_STOCK_1307`, `AuditLog`, `*_AUDIT`,
  `*_PARKED`.
- **Identity / app plumbing (keep dev's own):** `rbac_*`, `ARS_user`, `ARS_role`,
  `ARS_role_permission`, `ARS_permission`, `ARSS_*`, `AppUsers`.

Everything else is pulled prod → dev: all `ET_*` inputs, `Master_*`, `retail_*`,
and all config/rule tables (`ARS_MSA_SLOC_SETTINGS`, `ARS_STORE_SLOC_SETTINGS`,
`ARS_SEC_CAP_GROWTH_MATRIX(_CFG)`, `ARS_MERGE_RULES`, `ARS_GRID_BUILDER`,
`ARS_GRID_HIERARCHY`, `Cont_presets`, `ARS_CHECKLIST`, `ARS_DATA_DICTIONARY`,
`ARS_STORE_RANKING`, `ARS_STORE_BDC_SCHEDULE`, `ARS_DIVISION_DELETE_*_BDC`, etc.).

### Why not a full backup/restore
A full restore would (a) wipe the dev work you're developing/testing, and
(b) copy the 494M-row archive table for no reason. Selective one-way sync keeps
dev usable as a sandbox and is far smaller/faster.

---

## 2. Rollback strategy (per run)

1. **Pre-run backup** of dev target DB: `BACKUP DATABASE [Rep_Data] ... WITH COPY_ONLY`
   to a timestamped file. Keep last N backups.
2. Each table refresh runs inside a **transaction**: `TRUNCATE` + `INSERT … SELECT`
   from the linked prod table; any error → `ROLLBACK` (dev unchanged for that table).
3. If a run is bad after the fact → restore the pre-run dev backup.
4. Prod is only ever read, so there is nothing to roll back on prod.

---

## 3. Implementation phases (each gated on your approval)

**Phase 1 — Schema parity (one-time)**
Use SSMS **Schema Compare** (HOPC866 → ARSDBPRO), per DB, to create the 24+4 missing
tables *with indexes/constraints*. (Not `SELECT INTO` — it loses keys/indexes.)

**Phase 2 — Linked server + read-only login on dev (one-time)**
Create linked server `HOPC866` on ARSDBPRO. Optionally create a `db_datareader`-only
login on prod for it.

**Phase 3 — Sync proc + rollback wrapper (one-time)**
Deploy `usp_refresh_from_prod` on dev implementing the exclude-list logic +
pre-run backup + per-table transaction.

**Phase 4 — Test run (manual)**
Run the proc manually; verify row counts match prod for the synced tables; run one
MSA→Listing→Alloc cycle in dev to confirm it works.

**Phase 5 — Schedule**
SQL Agent job on dev, after the daily SSIS load window.

---

## 4. Separate: publish dev → prod (your change workflow)

Sync is prod→dev only. Publishing your tested changes back to prod is a **separate,
manual, reviewed** path (never automated):
- **Code:** git branch → PR → existing `zipdeploy` flow.
- **Schema:** new `scripts/023_*.sql`, run on dev, test, then run same script on prod.
- **Rule/config data:** script changed rows as idempotent `MERGE`, apply to prod as
  part of the numbered migration.

---

## 5. Open questions for you

1. OK to create a linked server + read-only login **on dev** (nothing on prod)?
2. Any table in the exclude list you actually DO want copied (e.g. seed
   `ARS_LISTING_WORKING` once so you can test allocation without re-running listing)?
3. Refresh frequency: daily after SSIS, or on-demand only?

---

# PART 2 — In-app "Dev Sync Manager" page

Instead of a SQL Agent job, drive the sync from a new ARS page where the user
activates/deactivates tables, sees new tables discovered in prod, and runs the
sync. Incremental daily + full weekly.

## 2.1 Data model (2 config tables, live on TARGET = dev)

**`ARS_DEV_SYNC_SETTINGS`** (single row) — the sync source + schedule.
```
id | source_server | source_user | source_pwd(enc) | link_server_name |
incr_enabled | incr_hour | full_enabled | full_weekday | full_hour |
last_incr_at | last_full_at | updated_at
```
Defaults: source_server=`hopc866` (read-only), link_server_name=`HOPC866`.
**Target = the app's own data DB** (the DEV app instance points at arsdbpro), so no
target credentials are stored. Hard guard: the run refuses if the app's current
server == source_server (i.e. someone opened the page on the PROD app) — this alone
guarantees prod is never written.

**`ARS_DEV_SYNC_TABLES`** (one row per managed table).
```
id | db_name(Claude|Rep_data) | table_name | is_active(bit) |
sync_mode(incremental|full) | incr_key_col(nullable) | exclude_reason(nullable) |
last_sync_at | last_full_at | last_mode | src_rows | tgt_rows |
last_status(ok|error|skipped) | last_message | discovered_at | updated_at
UNIQUE(db_name, table_name)
```

## 2.2 Sync engine (`services/dev_sync_service.py`)

Movement mechanism: **server-side `INSERT … SELECT` over a linked server on dev**
(fast; keeps prod read-only; app only writes to target). All new objects on dev.

- **Incremental** (mode=incremental, incr_key_col set):
  `@m = SELECT MAX(incr_key) FROM target;`
  `INSERT target (cols) SELECT cols FROM [LINK].src WHERE incr_key > @m;`
  Captures new appended rows cheaply. (Cannot see UPDATEs/DELETEs — that's what
  the weekly full is for.)
- **Full** (mode=full, or the weekly run, or no incr_key):
  `BEGIN TRAN; TRUNCATE target; INSERT target SELECT * FROM [LINK].src; COMMIT;`
  Any error → `ROLLBACK` (that table reverts). Other tables continue.
- Auto-detect an incr_key candidate on discovery: identity/int PK, else a column
  named `*_at`/`created`/`updated`/`id`; else default mode=full.
- Every table runs independently with per-table status → partial failures are
  isolated and reported, never abort the whole run.
- Hard guards: writes only ever target `target_server`; refuse if source==target;
  never touch exclude-list tables unless the user explicitly adds+activates them.

## 2.3 New-table discovery

`GET /dev-sync/discover` reads `sys.tables` on the source (both DBs), diffs against
`ARS_DEV_SYNC_TABLES`, and returns:
- **NEW in prod** — in source, not in config → shown with an "Add" action
  (inserted as `is_active=0` by default, so nothing syncs until the user opts in).
- **Missing on dev** — configured/active but absent on target → flagged "needs
  schema create" (Phase 1 schema-compare, or an app "Create empty table" action).
- **Orphan on dev** — on target, not on source → info only.

## 2.4 API (`endpoints/dev_sync.py`, prefix `/dev-sync`)

```
GET  /dev-sync/settings                 GET  /dev-sync/tables
PUT  /dev-sync/settings                 POST /dev-sync/tables          (add)
POST /dev-sync/test-connection          PUT  /dev-sync/tables/{id}     (edit/toggle)
POST /dev-sync/setup-linkserver         DELETE /dev-sync/tables/{id}
GET  /dev-sync/discover                 POST /dev-sync/tables/bulk-toggle
POST /dev-sync/run       {mode: incremental|full, table_ids?: []}   (async job)
GET  /dev-sync/runs      (history + live status)
```
Mirrors the `checklist.py` pattern: self-contained, `_ensure_table`, raw `text()`
SQL, `APIResponse`, `get_current_user`, new permissions `DEV_SYNC_VIEW/EDIT/RUN`.

## 2.5 Frontend (`pages/DevSyncManagerPage.jsx`)

- **Connections** card: source/target servers, Test, Setup-linked-server buttons;
  incremental + weekly-full schedule toggles.
- **Tables** grid: db, table, active toggle, mode dropdown (incremental/full),
  incr-key column, src rows vs dev rows, last sync, last status. Bulk activate/
  deactivate. Filter by db / active / status.
- **Discovery** banner: "N new tables found in prod" → review & add.
- **Run** bar: "Run Incremental (active)" / "Run Full (active)" + live progress.
- Route in `App.jsx` under an Admin/Settings group, guarded by `DEV_SYNC_VIEW`;
  entry in `Sidebar.jsx`; `devSyncAPI` block in `services/api.js`.

## 2.6 Scheduling

Incremental daily + full weekly, driven by the settings row. Wire into the existing
in-app scheduler (`report_scheduler_service.py` pattern) OR expose the two run
endpoints for an external caller — chosen per your answer below.

## 2.7 Files to add/change  — ✅ IMPLEMENTED 2026-07-20
- `backend/app/api/v1/endpoints/dev_sync.py` (new)
- `backend/app/services/dev_sync_service.py` (new)
- `backend/app/api/v1/router.py` (register)
- `backend/scripts/023_dev_sync_tables.sql` (new — config table DDL + permissions)
- `frontend/src/pages/DevSyncManagerPage.jsx` (new)
- `frontend/src/services/api.js` (add `devSyncAPI`)
- `frontend/src/App.jsx` + `frontend/src/components/layout/Sidebar.jsx` (register)
- `frontend/public/docs/manual/dev_sync.md` (new dossier — per CLAUDE.md)

## 2.8 Safety recap
Prod strictly read-only (linked-server login can be `db_datareader`). All writes to
dev only. Per-table transactions → isolated rollback. source==target refused.
Nothing syncs unless the user activates it. Weekly full reconciles incremental drift.
