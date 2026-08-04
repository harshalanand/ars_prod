---
title: SAP Integration
tags: [sap, module, data-ingestion, gateway]
---

# SAP Integration Module

Read-only SAP data ingestion into ARS via the V2 universal MCP gateway. Pulls live data from SAP into local SQL Server staging tables on schedule or on demand. Zero impact on existing ARS modules.

## Module Status
**Built and working** (2026-07-25). Self-contained sidebar section (SAP) with 4 routes:
- **Connection** — gateway config + test + field display preference
- **Data Pulls** — define/schedule/run pulls via RFC or Snowflake
- **SAP Explorer** — discover tables/fields/OData before pulling
- **Run History** — pull execution log

## Architecture

**No SAP SDK / pyrfc on server.** All calls go through V2 universal MCP gateway (https://universal-mcp.akash-bab.workers.dev, header X-API-Key) over HTTPS JSON-RPC.

### Two Data Doors

1. **RFC Gateway** (`rfc_table`) — RFC_READ_TABLE via sap_read_table MCP tool
   - Args: table, fields[], where, limit, offset, env (dev|qa|prod)
   - **GOTCHA:** DATA_BUFFER_EXCEEDED (512-byte limit) on wide tables unless explicit fields[] passed. Always send explicit fields to avoid.
   - Example: LQUA (warehouse bin stock), MARD (warehouse storage location), MARC (material master), MARA (material), etc.

2. **OData Gateway** (`odata`) — Custom or standard OData services via sap_odata_pull MCP tool
   - Args: service, entity, top, skip, filter, select, env
   - Catalog available via sap_odata_services (list live services)

3. **Snowflake Door** (`snowflake`) — Direct reads from SAP BRONZE data lake (2026-07-27)
   - Cost/latency optimized for bulk tables; complements RFC for MARD/MARC (empty in BRONZE)
   - Lands into same local SAP_* tables, reuses scheduler

## Database Schema

**Rep_data DB** (backend/scripts/026_sap_foundation.sql):

- **SAP_CONNECTION** (ID=1) — Single row, gateway config
  - api_key (encrypted Fernet via app/core/crypto)
  - default_env (dev|qa|prod)
  - enabled flag
  - **DISPLAY_MODE** ('name'|'label'|'both') — global app-wide field display preference

- **SAP_PULL_DEF** — Pull definitions (table, fields[], where, schedule, target SAP_ table)
  - door ('rfc_table'|'odata'|'snowflake')
  - WHERE_JSON (structured conditions from builder) or where_clause fallback
  - For OData: service, entity, filter, select
  - For Snowflake: sf_query, sf_database, sf_schema
  - replace_mode (truncate|append), last_run_at, next_run_at, enabled

- **SAP_PULL_RUN** — Run history (run_id, pull_id, start_at, end_at, status, row_count, error)

- **Target SAP_* tables** — Auto-created, all cols NVARCHAR(1000) + audit (_RUN_ID, _PULLED_AT)

## Services (Backend)

- **sap_config_service.py** — DB-backed connection config CRUD; test_connection; status. Handles DISPLAY_MODE.
- **sap_client.py** — HTTPS JSON-RPC client; robust SAP error parse (EX_RETURN); test_connection, read_table, odata_pull, odata_services.
- **sap_pull_service.py** — CRUD for pulls; run_pull engine (fetch paged → land rows into SAP_ table with pyodbc 2100-param batching, column sanitization, truncate/append); build_where_clause() mirrors frontend builder.
- **sap_scheduler_service.py** — Singleton daemon; claims due pulls (NEXT_RUN_AT ≤ now); started/stopped in main.py lifespan.
- **sap_snowflake_client.py** — Thin adapter delegating to snowflake_config_service.connect(); re-raises SnowflakeError as SapError.

## API Endpoints (backend/app/api/v1/endpoints/sap.py, prefix /sap)

- GET/PUT /sap/connection, POST /sap/connection/test (SUPER_ADMIN for connection; others ADMIN/SUPER_ADMIN)
- /sap/pulls CRUD + /{id}/run + /{id}/enable
- GET /sap/runs (run history)
- POST /sap/preview (ad-hoc read; never lands rows)
- GET /sap/odata-services (list OData services)
- GET /sap/scheduler/status
- POST /sap/snowflake/test (test Snowflake door)
- GET /sap/snowflake/tables (list Snowflake tables in configured DB/schema)

PullReq/PreviewReq accept both where_json (structured) and where_clause (fallback).

## Frontend Components

- **sapAPI** (frontend/src/services/api.js) — Unified SAP API client
- **sapUiStore.js** (Zustand) — Global DISPLAY_MODE state
- **WhereBuilder.jsx** — Structured WHERE condition editor (field · operator · value, AND/OR)
- **sapWhere.js** — buildWhere/cleanConds helpers; backend mirror in sap_pull_service.py
- **Pages:** SapConnectionPage (accepts `embedded` prop), SapPullsPage, SapExplorerPage (Discovery), SapRunsPage
- **Routes** in App.jsx

## Metadata Discovery (SAP Explorer)

Find and inspect SAP tables + fields + OData before pulling:

- **DD02T** (TABNAME/DDTEXT) — Find tables by keyword (semantic search on name + description)
- **DD03L** (FIELDNAME, ROLLNAME, DATATYPE, LENG, DECIMALS) — List table fields; resolve data types
- **DD04T** (ROLLNAME → SCRTEXT_M / DDTEXT) — Human labels. E.g.:
  - MATNR → 'Material Number'
  - WERKS_D → 'Plant'
  - LIFNR → 'Vendor'
  - *Note: DD03T (field texts) often EMPTY for standard tables; labels live on the DATA ELEMENT in DD04T*
- **sap_odata_services** — Catalog live OData services (list services + entities)

### Data Type Gotcha
- NUMC, DATS, TIMS **look numeric but are CHAR-like** → must be quoted in WHERE
- Only DEC, CURR, QUAN, FLTP, INT* are true numbers → no quotes in WHERE

## Field Display Modes

Single app-wide setting (SAP_CONNECTION.DISPLAY_MODE), chosen on Connection page:

- **'name'** — Show field names only (FIELDNAME)
- **'label'** — Show human labels only (resolved from DD04T)
- **'both'** — Show both: "Label (FIELDNAME)"

Applied across every SAP screen (explorer, pull forms, run history) via Zustand store.

## WHERE Condition Builder

Structured editor in Data Pulls page (when defining/editing a pull):

- Multi-condition builder: field · operator · value
- Operators: =, <>, <, >, <=, >=, LIKE, IN, BETWEEN, etc.
- Logic: AND / OR between conditions
- Stored as WHERE_JSON on SAP_PULL_DEF
- Backend mirror in sap_pull_service.py build_where_clause()
- Raw where_clause fallback for complex cases

**Trialed & Reverted:**
- Per-field FIELD_MAP labels (reverted; too complex)
- Data-type-aware WHERE inputs (reverted; simpler global DISPLAY_MODE replaces)

## Snowflake Door (2026-07-27)

Direct reads from SAP BRONZE data lake (V2RETAIL Snowflake). Complements RFC for cost/latency on bulk tables.

- **97 SAP_* tables**, ~4.15B rows (delta/CDC: DL_INGESTED_AT, SOURCE, BATCH_ID, DELTA_TOKEN)
- **Notable tables:** LQUA (2.9M bin-level stock), ZWM_HU_BIN (140M HU→bin mappings)
- **Gotcha:** MARD/MARC empty (0 rows) → use live RFC door for storage-location stock
- **Config:** Shared app-wide via Settings → Snowflake (key-pair JWT or password auth); backend restart required; key file must exist

For pull definitions: choose "Snowflake (direct)" door, enter database/schema, write SELECT query → lands into local SAP_* table with run history.

## Other Ways to Fetch SAP Data

### v2_rfc_call (sap-api.v2retail.net)
Ready-made RFC relay; backed by DataV2 SQL Server (192.168.151.28). `/health` works. Slower than DataV2 direct.

### DataV2 SQL Server (MCP tools: v2_sql_query / datav2_list_tables / datav2_describe_table)
- SAP data nightly synced (~1501 tables: ACTUAL_SALE_*, ALLOCATION_MSA_DATA, ALC_ARTICLE_ALLOCATION…)
- **Fastest, zero SAP load, ≤1 day old**
- Best for batch/overnight; recommended for read-heavy reports

### BAPI / Function Modules
Curated, computed SAP objects (vs raw RFC_READ_TABLE). Higher latency; use when needing business logic.

### Rule of Thumb
- **Live / any table / real-time** → RFC read_table
- **Standard daily-fresh, fast, no SAP load** → DataV2 SQL copy
- **Bulk read** → Snowflake BRONZE (complement with RFC when MARD/MARC needed live)

## How to Build an OData Service (S/4HANA, Reference)

Custom SAP data via OData (3-object stack):

1. **CDS View** (DDLS, the SELECT)
   - Author in Eclipse ADT (Ctrl+N → Data Definition) or headlessly via `sap_adt_create_ddls` gateway tool
   - Standard naming: Z_<NAME>_CDS

2. **Service Definition** (SRVD)
   - Expose the view: `@OData.publish: true; define service as expose … as Entity …`
   - Create via ADT (Ctrl+N → Service Definition) or `sap_adt_create_srvd`
   - Standard naming: Z_<NAME>_SRVD

3. **Service Binding** (SRVB)
   - Publish as OData V2 or V4
   - Create via ADT (Ctrl+N → Service Binding) or `sap_adt_create_srvb`
   - Binding type OData V2/V4 → live at `/sap/opu/odata/sap/<NAME>/`
   - Standard naming: Z_SB_<NAME>

**Workflow:**
1. Build in DEV (requires package + transport request + developer auth)
2. Transport to PROD (existing Z_SB_* services show status 'not_shipped' = in DEV only, not yet PROD)
3. Once live in PROD, use in Data Pulls / Explorer as OData service + entity

**Gateway tools:** `sap_adt_create_ddls`, `sap_adt_create_srvd`, `sap_adt_create_srvb` (each takes name, source, package, corrnr=transport, env).

## Related

- [[Bin Allocation module]] — consumes bin stock from LQUA (via this module)
- [[Snowflake shared config]] — app-wide Snowflake connection (shared by SAP door + Report Gen)
- [[Reports]] — Report Generation Hub
- [[ARS Work Log]] — milestones
