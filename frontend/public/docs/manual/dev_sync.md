# Dev Sync Manager — PROD → DEV table refresh

## BRD (why)
V2 Retail runs ARS on **HOPC866 (production)**. Development and testing must happen
on a separate **ARSDBPRO (dev)** server without risking prod. Both servers hold the
same databases (`Claude`, `Rep_data`) and the daily SAP extract (SSIS) already loads
the raw input tables (`ET_*`, `COUNT_STOCK_DATA_18M`, `Master_ALC_PEND`, …) into
**both** servers. What drifts is everything else — the config/rule/reference tables
that alloc & listing depend on. Dev needs those kept fresh from prod so a developer
can run MSA → Grid → Listing → Allocation against production-like data, then publish
tested changes back to prod through the normal code/migration path.

The Dev Sync Manager is the controlled, **one-way** channel that keeps dev current.

## FSD (rules)

### Direction & safety
- **One-way only: prod (HOPC866) → dev (ARSDBPRO).** Prod is *read-only*.
- **Target = the app's own data DB.** The page is meant to run on the DEV app
  instance (its data DB points at ARSDBPRO). All writes go to the local server.
- **Source** is reached through a **linked server** created on dev; the linked-server
  login should be `db_datareader` on prod. `remote proc transaction promotion` is set
  `false` so per-table transactions do not enlist MSDTC.
- **Hard guard:** a run is refused if the source server == this (target) server
  (i.e. the page was opened on the PROD app). This alone guarantees prod is never
  written.

### Sync modes
- **Incremental** (default, daily): `INSERT … SELECT WHERE incr_key > MAX(incr_key on dev)`.
  Cheap; captures newly-appended rows only. Requires a monotonic key (identity/int PK
  or a `*_at`/`created`/`updated` column), auto-detected on discovery.
- **Full** (weekly): `TRUNCATE + INSERT` inside a transaction. Reconciles the
  UPDATEs/DELETEs that incremental append cannot see. Falls back to `DELETE` when the
  table is FK-referenced; preserves identity values via `SET IDENTITY_INSERT`.
- Each table syncs in **its own transaction** — a failure rolls back only that table;
  the run continues and reports per-table status.

### Table selection
- Nothing syncs unless the user **activates** it. New rows are added inactive.
- **Discovery** diffs prod `sys.tables` against the config and surfaces:
  - *new in prod* — not yet configured (engine-output/history names are ⚠-flagged);
  - *missing on dev* — configured but absent on target (needs schema create);
  - *orphan on dev* — on dev, not on prod.
- Engine outputs (`ARS_MSA_*`, `ARS_GRID_MJ*`, `ARS_LISTING*`, `ARS_ALLOC_*`,
  `ARS_pend_alc`, …), history/archive (`*_HISTORY`, `*_ARCHIVE`, `AuditLog`), and
  identity tables (`rbac_*`, `ARSS_*`) are flagged as excluded — dev regenerates or
  keeps its own. The user can still add any table explicitly.

### Publish dev → prod (separate path)
Sync is prod→dev only. Tested changes go back to prod via git code deploy + numbered
`scripts/NNN_*.sql` migrations run first on dev, then on prod. Never automated here.

## Table categories
Every discovered table is auto-classified so you can bulk-select what to sync:
- **input** — daily SAP extract (`ET_*`, `COUNT_STOCK_*`, `ALLOCATION_MRDC_*`,
  `Master_ALC_*`, `MASTER_ART_BROADER_MENU`, `store_*`). *Recommended to sync.*
- **reference** — master data (`Master_*`, `MASTER_*`, `retail_*`, `Master_AVG_DENSITY`).
  *Recommended.*
- **config** — rules/settings that drive alloc/listing (`*_SETTINGS`, `ARS_MERGE_RULES`,
  `ARS_GRID_BUILDER/HIERARCHY`, `*SEC_CAP*`, `ARS_CHECKLIST`, `ARS_DATA_DICTIONARY`,
  `ARS_STORE_RANKING`, `*BDC*`, `Cont_*`, `app_config`). *Recommended.*
- **output** — engine results dev regenerates (`ARS_MSA_*`, `ARS_GRID_MJ*`,
  `ARS_LISTING*`, `ARS_ALLOC*`, `ARS_pend_alc`, `Cont_Percentage_*`). *Skip.*
- **history** — `*_HISTORY`, `*_ARCHIVE`, `AuditLog`, `*_PARKED`, `ET_STORE_STOCK_1307`. *Skip.*
- **identity** — `rbac_*`, `ARSS_*`, `App*`, `ARS_user/role/permission`. *Skip (dev keeps own).*

"Add recommended" adds input+reference+config in one click.

## Data
- Connection settings (source + **target** servers/users/passwords, linked-server
  name, schedule, last-run) live in `backend/app_settings.json` → block `dev_sync`.
  **Nothing is written to prod.**
- `ARS_DEV_SYNC_TABLES` — one row per managed table on the **target** (dev) data DB:
  category, active flag, mode, incr key, last-sync status, prod/dev row counts.

## Recorded rules
- 2026-07-20: Module created. Movement = server-side `INSERT…SELECT` over a linked
  server on dev. Manual run buttons first; daily/weekly scheduling is a follow-up
  (schedule fields already in the `dev_sync` settings block).
- 2026-07-20: Dev Sync has **explicit source + target connections** (not the app's
  own DB), so the app can stay on PROD while pushing PROD→DEV. The prod-safety guard
  compares the configured **host names** (`source_server` vs `target_server`), NOT
  `@@SERVERNAME` — both HOPC866 and ARSDBPRO report the same registered instance
  name `ARS`, which previously caused a false "same server" refusal.
- 2026-07-20: Tables are auto-categorised; sync **skips** any table whose prod & dev
  row counts already match (a `force` toggle overrides).
- Gated by `SUPER_ADMIN` in code (no table permission row).
