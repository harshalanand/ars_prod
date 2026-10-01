/* ============================================================================
   038 · Get Data module (Rep_data)
   ----------------------------------------------------------------------------
   Reference DDL. The app creates these tables itself on first use
   (app/services/get_data_schema.py) — keep both in sync. Idempotent.

     GD_VIEW          Snowflake views created through the module
     GD_VIEW_VERSION  every saved version of a view's SQL
     GD_JOB           sync jobs: Snowflake view/table → local GD_SF_* table
     GD_RUN           run history, one row per run (AUTO | MANUAL)

   Synced data tables are created by the loader and always start with GD_SF_.
   No metadata table may use that prefix.
   Spec: frontend/public/docs/manual/get_data.md
   ============================================================================ */

IF OBJECT_ID('dbo.GD_VIEW', 'U') IS NULL
CREATE TABLE dbo.GD_VIEW (
    VIEW_ID       INT IDENTITY(1,1) PRIMARY KEY,
    VIEW_NAME     NVARCHAR(128)  NOT NULL,
    SF_DATABASE   NVARCHAR(128)  NOT NULL,
    SF_SCHEMA     NVARCHAR(128)  NOT NULL,
    DESCRIPTION   NVARCHAR(500)  NULL,
    VIEW_SQL      NVARCHAR(MAX)  NOT NULL,
    VERSION       INT            NOT NULL DEFAULT 1,
    COLUMN_COUNT  INT            NULL,
    ROW_COUNT     BIGINT         NULL,
    STATUS        NVARCHAR(20)   NOT NULL DEFAULT 'active',     -- active | dropped
    CREATED_BY    NVARCHAR(128)  NULL,
    CREATED_AT    DATETIME2(0)   NOT NULL DEFAULT SYSUTCDATETIME(),
    UPDATED_BY    NVARCHAR(128)  NULL,
    UPDATED_AT    DATETIME2(0)   NOT NULL DEFAULT SYSUTCDATETIME()
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_GD_VIEW_name')
CREATE UNIQUE INDEX UX_GD_VIEW_name ON dbo.GD_VIEW (SF_DATABASE, SF_SCHEMA, VIEW_NAME);
GO

IF OBJECT_ID('dbo.GD_VIEW_VERSION', 'U') IS NULL
CREATE TABLE dbo.GD_VIEW_VERSION (
    VERSION_ID    INT IDENTITY(1,1) PRIMARY KEY,
    VIEW_ID       INT            NOT NULL,
    VERSION       INT            NOT NULL,
    ACTION        NVARCHAR(20)   NOT NULL,                      -- created | replaced | dropped
    VIEW_SQL      NVARCHAR(MAX)  NULL,
    COLUMN_COUNT  INT            NULL,
    ROW_COUNT     BIGINT         NULL,
    CHANGED_BY    NVARCHAR(128)  NULL,
    CHANGED_AT    DATETIME2(0)   NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

IF OBJECT_ID('dbo.GD_JOB', 'U') IS NULL
CREATE TABLE dbo.GD_JOB (
    JOB_ID          INT IDENTITY(1,1) PRIMARY KEY,
    JOB_NAME        NVARCHAR(200)  NOT NULL,
    DESCRIPTION     NVARCHAR(500)  NULL,
    SOURCE_TYPE     NVARCHAR(20)   NOT NULL DEFAULT 'SNOWFLAKE',
    SOURCE_OBJECT   NVARCHAR(400)  NOT NULL,                    -- DB.SCHEMA.NAME
    TARGET_TABLE    NVARCHAR(128)  NOT NULL,                    -- GD_SF_*
    LOAD_MODE       NVARCHAR(20)   NOT NULL DEFAULT 'replace',  -- replace | incremental | append
    KEY_COLS        NVARCHAR(MAX)  NULL,                        -- JSON list
    WATERMARK_COL   NVARCHAR(128)  NULL,
    WATERMARK_VALUE NVARCHAR(100)  NULL,                        -- last loaded max (UTC for timestamps)
    TRIGGER_TYPE    NVARCHAR(20)   NOT NULL DEFAULT 'manual',   -- manual | schedule
    SCHEDULE_CONFIG NVARCHAR(MAX)  NULL,                        -- JSON, times in IST
    RETRY_ON_FAIL   BIT            NOT NULL DEFAULT 1,
    RETRY_DELAY_MIN INT            NOT NULL DEFAULT 15,
    ENABLED         BIT            NOT NULL DEFAULT 1,
    NEXT_RUN_AT     DATETIME2(0)   NULL,                        -- UTC
    RETRY_AT        DATETIME2(0)   NULL,                        -- UTC
    LOCK_RUN_ID     BIGINT         NULL,                        -- run holding the job
    LOCK_HEARTBEAT  DATETIME2(0)   NULL,
    LAST_RUN_ID     BIGINT         NULL,
    LAST_RUN_AT     DATETIME2(0)   NULL,
    LAST_STATUS     NVARCHAR(20)   NULL,
    LAST_ROWS       BIGINT         NULL,
    LAST_MESSAGE    NVARCHAR(1000) NULL,
    CREATED_BY      NVARCHAR(128)  NULL,
    CREATED_AT      DATETIME2(0)   NOT NULL DEFAULT SYSUTCDATETIME(),
    UPDATED_BY      NVARCHAR(128)  NULL,
    UPDATED_AT      DATETIME2(0)   NOT NULL DEFAULT SYSUTCDATETIME()
);
GO
-- 2026-10-01: per-job loader (bulk default | classic) and the empty-source guard
IF COL_LENGTH('dbo.GD_JOB', 'LOADER_PREF') IS NULL
ALTER TABLE dbo.GD_JOB ADD LOADER_PREF NVARCHAR(10) NOT NULL CONSTRAINT DF_GD_JOB_LOADER_PREF DEFAULT 'bulk';
GO
IF COL_LENGTH('dbo.GD_JOB', 'ALLOW_EMPTY') IS NULL
ALTER TABLE dbo.GD_JOB ADD ALLOW_EMPTY BIT NOT NULL CONSTRAINT DF_GD_JOB_ALLOW_EMPTY DEFAULT 0;
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_GD_JOB_due')
CREATE INDEX IX_GD_JOB_due ON dbo.GD_JOB (ENABLED, TRIGGER_TYPE, NEXT_RUN_AT);
GO

IF OBJECT_ID('dbo.GD_RUN', 'U') IS NULL
CREATE TABLE dbo.GD_RUN (
    RUN_ID             BIGINT IDENTITY(1,1) PRIMARY KEY,
    JOB_ID             INT            NOT NULL,
    JOB_NAME           NVARCHAR(200)  NULL,
    SOURCE_TYPE        NVARCHAR(20)   NOT NULL DEFAULT 'SNOWFLAKE',
    SOURCE_OBJECT      NVARCHAR(400)  NULL,
    TARGET_TABLE       NVARCHAR(128)  NULL,
    LOAD_MODE          NVARCHAR(20)   NULL,
    RUN_TYPE           NVARCHAR(10)   NOT NULL,                 -- AUTO | MANUAL
    TRIGGERED_BY       NVARCHAR(128)  NULL,
    ATTEMPT            INT            NOT NULL DEFAULT 1,       -- 2 = automatic retry
    FULL_RELOAD        BIT            NOT NULL DEFAULT 0,
    STATUS             NVARCHAR(20)   NOT NULL DEFAULT 'running', -- running | success | failed | skipped
    STEP               NVARCHAR(200)  NULL,
    SOURCE_ROWS        BIGINT         NULL,                     -- rows Snowflake returned
    ROWS_LOADED        BIGINT         NULL,                     -- rows that reached SQL Server
    ROWS_INSERTED      BIGINT         NULL,
    ROWS_UPDATED       BIGINT         NULL,
    TARGET_ROWS_BEFORE BIGINT         NULL,
    TARGET_ROWS_AFTER  BIGINT         NULL,
    WATERMARK_FROM     NVARCHAR(100)  NULL,
    WATERMARK_TO       NVARCHAR(100)  NULL,
    SF_QUERY_ID        NVARCHAR(100)  NULL,
    STARTED_AT         DATETIME2(0)   NOT NULL DEFAULT SYSUTCDATETIME(),
    HEARTBEAT_AT       DATETIME2(0)   NULL,
    COMPLETED_AT       DATETIME2(0)   NULL,
    DURATION_MS        BIGINT         NULL,
    MESSAGE            NVARCHAR(4000) NULL
);
GO
-- 2026-10-01: which loader a run used — bulk (bcp) or classic (parameterised inserts)
IF COL_LENGTH('dbo.GD_RUN', 'LOADER') IS NULL
ALTER TABLE dbo.GD_RUN ADD LOADER NVARCHAR(20) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_GD_RUN_job')
CREATE INDEX IX_GD_RUN_job ON dbo.GD_RUN (JOB_ID, RUN_ID DESC);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_GD_RUN_started')
CREATE INDEX IX_GD_RUN_started ON dbo.GD_RUN (STARTED_AT DESC) INCLUDE (RUN_TYPE, STATUS, ROWS_LOADED);
GO
