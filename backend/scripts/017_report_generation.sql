-- ============================================================================
-- 017_report_generation.sql
-- Report Generation hub — report definitions + run history.
-- Database: Rep_data (Data DB — same as ARS_* operational tables)
-- Created: 2026-07-09
--
-- NOTE: report_scheduler_service.ensure_report_tables() self-heals these at
-- app startup, so running this script is optional. It exists to document the
-- schema and to provision the tables ahead of first boot.
-- ============================================================================

IF OBJECT_ID('dbo.ARS_REPORTS', 'U') IS NULL
CREATE TABLE dbo.ARS_REPORTS (
    REPORT_ID         INT IDENTITY(1,1) PRIMARY KEY,
    NAME              NVARCHAR(200)  NOT NULL,
    DESCRIPTION       NVARCHAR(500)  NULL,
    STEPS             NVARCHAR(MAX)  NOT NULL,   -- JSON [{type:'sql'|'code', name, params}]
    OUTPUT_TYPE       NVARCHAR(20)   NOT NULL DEFAULT 'folder',  -- folder|snowflake|both
    BASE_DIR          NVARCHAR(400)  NULL,       -- desired export folder (folder output)
    FILE_FORMAT       NVARCHAR(10)   NOT NULL DEFAULT 'csv',     -- csv|xlsx
    SNOWFLAKE_CONFIG  NVARCHAR(MAX)  NULL,        -- JSON {database,schema,table,key_cols,watermark_col}
    TRIGGER_TYPE      NVARCHAR(20)   NOT NULL DEFAULT 'manual',  -- schedule|event|manual
    SCHEDULE_CONFIG   NVARCHAR(MAX)  NULL,        -- JSON {freq:'daily'|'weekly'|'hourly', time, every_n_hours, weekday}
    TRIGGER_EVENT     NVARCHAR(50)   NULL,        -- listing.approved|pendalc.approved|msa.completed
    ENABLED           BIT            NOT NULL DEFAULT 1,
    NEXT_RUN_AT       DATETIME       NULL,
    LAST_RUN_AT       DATETIME       NULL,
    LAST_STATUS       NVARCHAR(20)   NULL,
    CREATED_BY        NVARCHAR(100)  NULL,
    CREATED_AT        DATETIME       NOT NULL DEFAULT SYSUTCDATETIME(),
    UPDATED_AT        DATETIME       NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_ARS_REPORTS_due')
CREATE INDEX IX_ARS_REPORTS_due ON dbo.ARS_REPORTS (TRIGGER_TYPE, ENABLED, NEXT_RUN_AT);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_ARS_REPORTS_event')
CREATE INDEX IX_ARS_REPORTS_event ON dbo.ARS_REPORTS (TRIGGER_EVENT, ENABLED);
GO

IF OBJECT_ID('dbo.ARS_REPORT_RUNS', 'U') IS NULL
CREATE TABLE dbo.ARS_REPORT_RUNS (
    RUN_ID          BIGINT IDENTITY(1,1) PRIMARY KEY,
    REPORT_ID       INT            NOT NULL,
    SESSION_CODE    NVARCHAR(80)   NOT NULL,
    TRIGGER_SOURCE  NVARCHAR(40)   NOT NULL,      -- schedule|manual|event:<name>
    STATUS          NVARCHAR(20)   NOT NULL DEFAULT 'pending',  -- pending|running|completed|failed|skipped
    EXPORT_DIR      NVARCHAR(500)  NULL,
    FILES           NVARCHAR(MAX)  NULL,          -- JSON
    ERRORS          NVARCHAR(MAX)  NULL,          -- JSON
    ROW_COUNT       INT            NULL,
    STARTED_AT      DATETIME       NULL,
    COMPLETED_AT    DATETIME       NULL,
    DURATION_MS     INT            NULL,
    CREATED_AT      DATETIME       NOT NULL DEFAULT SYSUTCDATETIME(),
    CREATED_BY      NVARCHAR(100)  NULL
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_ARS_REPORT_RUNS_report')
CREATE INDEX IX_ARS_REPORT_RUNS_report ON dbo.ARS_REPORT_RUNS (REPORT_ID, CREATED_AT DESC);
GO

PRINT N'Created ARS_REPORTS + ARS_REPORT_RUNS';
GO
