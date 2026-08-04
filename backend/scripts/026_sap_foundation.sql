/* ============================================================================
   026_sap_foundation.sql
   SAP Integration (read-only) foundation.

   A self-contained module that pulls data FROM SAP into SQL Server on a
   schedule (or on demand), leaving every existing ARS module untouched.

   The app does NOT talk to SAP directly — it calls the V2 "universal MCP"
   gateway worker over HTTPS (X-API-Key header), which relays to SAP via
   RFC_READ_TABLE or OData and returns JSON rows. So NO SAP SDK / pyrfc is
   installed on the ARS server.

   Data lands in tables the planner names on each pull definition, prefixed
   SAP_ (e.g. SAP_LQUA). Those target tables are created lazily by the pull
   service on first run (all columns NVARCHAR — SAP RFC returns strings),
   so they are not declared here.

   Runs on the DATA DB (Rep_data). The services also create these lazily
   (sap_config_service / sap_pull_service :: ensure_*), so this script is for
   documentation / explicit provisioning. Idempotent — safe to re-run.
   ============================================================================ */

-- ── Gateway connection (single active row, ID=1) ────────────────────────────
IF OBJECT_ID('dbo.SAP_CONNECTION', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.SAP_CONNECTION (
        ID            INT            NOT NULL PRIMARY KEY,   -- always 1
        WORKER_URL    NVARCHAR(400)  NULL,                  -- MCP gateway URL
        API_KEY       NVARCHAR(MAX)  NULL,                  -- Fernet-encrypted ('enc:...')
        DEFAULT_ENV   NVARCHAR(10)   NOT NULL DEFAULT 'prod',-- dev | qa | prod
        DISPLAY_MODE  NVARCHAR(10)   NOT NULL DEFAULT 'name',-- name | label | both (global field display)
        ENABLED       BIT            NOT NULL DEFAULT 0,
        LAST_STATUS   NVARCHAR(32)   NULL,                  -- Connected | Error | Not Verified
        LAST_MESSAGE  NVARCHAR(1000) NULL,
        LAST_VERIFIED DATETIME2      NULL,
        CREATED_BY    NVARCHAR(128)  NULL,
        CREATED_DATE  DATETIME2      NULL DEFAULT SYSUTCDATETIME(),
        MODIFIED_BY   NVARCHAR(128)  NULL,
        MODIFIED_DATE DATETIME2      NULL
    );
END

-- ── Pull definitions (what to fetch → where to land → on what schedule) ─────
IF OBJECT_ID('dbo.SAP_PULL_DEF', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.SAP_PULL_DEF (
        PULL_ID       INT IDENTITY(1,1) PRIMARY KEY,
        NAME          NVARCHAR(200)  NOT NULL,
        DESCRIPTION   NVARCHAR(500)  NULL,
        DOOR          NVARCHAR(20)   NOT NULL DEFAULT 'rfc_table', -- rfc_table | odata
        -- rfc_table door:
        SAP_TABLE     NVARCHAR(64)   NULL,   -- e.g. LQUA
        FIELDS        NVARCHAR(MAX)  NULL,   -- JSON array of column names (required for wide tables)
        WHERE_CLAUSE  NVARCHAR(MAX)  NULL,   -- RFC_READ_TABLE WHERE (no 'WHERE' keyword)
        -- odata door:
        ODATA_SERVICE NVARCHAR(128)  NULL,   -- e.g. Z_SB_ARTICLE
        ODATA_ENTITY  NVARCHAR(128)  NULL,   -- e.g. ArtMin
        ODATA_FILTER  NVARCHAR(MAX)  NULL,   -- $filter
        ODATA_SELECT  NVARCHAR(MAX)  NULL,   -- $select
        -- common:
        ENV           NVARCHAR(10)   NULL,   -- override connection default (dev|qa|prod)
        ROW_LIMIT     INT            NOT NULL DEFAULT 50000, -- max rows to fetch (paged)
        TARGET_TABLE  NVARCHAR(128)  NOT NULL,               -- SQL table to fill (SAP_ prefixed)
        WRITE_MODE    NVARCHAR(10)   NOT NULL DEFAULT 'replace', -- replace | append
        TRIGGER_TYPE  NVARCHAR(20)   NOT NULL DEFAULT 'manual',  -- manual | schedule
        SCHEDULE_CONFIG NVARCHAR(MAX) NULL,  -- JSON (freq/times/…) — same shape as reports
        ENABLED       BIT            NOT NULL DEFAULT 1,
        NEXT_RUN_AT   DATETIME       NULL,
        LAST_RUN_AT   DATETIME       NULL,
        LAST_STATUS   NVARCHAR(20)   NULL,
        LAST_ROWS     INT            NULL,
        CREATED_BY    NVARCHAR(128)  NULL,
        CREATED_AT    DATETIME       NOT NULL DEFAULT SYSUTCDATETIME(),
        UPDATED_AT    DATETIME       NOT NULL DEFAULT SYSUTCDATETIME()
    );
    CREATE INDEX IX_SAP_PULL_DEF_due
        ON dbo.SAP_PULL_DEF (TRIGGER_TYPE, ENABLED, NEXT_RUN_AT);
END

-- ── Run history (one row per pull execution) ────────────────────────────────
IF OBJECT_ID('dbo.SAP_PULL_RUN', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.SAP_PULL_RUN (
        RUN_ID        BIGINT IDENTITY(1,1) PRIMARY KEY,
        PULL_ID       INT            NOT NULL,
        TRIGGER_SOURCE NVARCHAR(40)  NOT NULL,   -- manual | schedule
        STATUS        NVARCHAR(20)   NOT NULL DEFAULT 'running', -- running | success | failed | skipped
        ROWS_FETCHED  INT            NULL,
        TARGET_TABLE  NVARCHAR(128)  NULL,
        MESSAGE       NVARCHAR(MAX)  NULL,
        STARTED_AT    DATETIME       NULL,
        COMPLETED_AT  DATETIME       NULL,
        DURATION_MS   INT            NULL,
        CREATED_BY    NVARCHAR(128)  NULL,
        CREATED_AT    DATETIME       NOT NULL DEFAULT SYSUTCDATETIME()
    );
    CREATE INDEX IX_SAP_PULL_RUN_pull ON dbo.SAP_PULL_RUN (PULL_ID, CREATED_AT DESC);
END
