"""
Get Data — table definitions (Rep_data, self-healing DDL).

    GD_VIEW          Snowflake views created through the module (live copy)
    GD_VIEW_VERSION  every saved version of a view's SQL (audit trail)
    GD_JOB           sync-job definitions (source → local GD_SF_* table)
    GD_RUN           run history — one row per run, AUTO or MANUAL

The GD_SF_ prefix belongs to synced data tables only — no metadata table may
use it. GD_JOB / GD_RUN are source-agnostic (SOURCE_TYPE) so the later Excel,
DataV2 and SAP pages write into the same history. Reference DDL for DBAs lives in
backend/scripts/038_get_data_module.sql — keep the two in sync.
Spec: frontend/public/docs/manual/get_data.md.
"""
import threading

from sqlalchemy import text

from app.database.session import get_data_engine

VIEW_TABLE = "GD_VIEW"
VIEW_VERSION_TABLE = "GD_VIEW_VERSION"
JOB_TABLE = "GD_JOB"
RUN_TABLE = "GD_RUN"

_ready = False
_ready_lock = threading.Lock()


def ensure_tables(force: bool = False) -> None:
    """Create the module's tables if missing. Cheap after the first call."""
    global _ready
    if _ready and not force:
        return
    with _ready_lock:
        if _ready and not force:
            return
        eng = get_data_engine()
        with eng.begin() as c:
            c.execute(text(f"""
                IF OBJECT_ID('dbo.{VIEW_TABLE}', 'U') IS NULL
                CREATE TABLE dbo.{VIEW_TABLE} (
                    VIEW_ID       INT IDENTITY(1,1) PRIMARY KEY,
                    VIEW_NAME     NVARCHAR(128)  NOT NULL,
                    SF_DATABASE   NVARCHAR(128)  NOT NULL,
                    SF_SCHEMA     NVARCHAR(128)  NOT NULL,
                    DESCRIPTION   NVARCHAR(500)  NULL,
                    VIEW_SQL      NVARCHAR(MAX)  NOT NULL,
                    VERSION       INT            NOT NULL DEFAULT 1,
                    COLUMN_COUNT  INT            NULL,
                    ROW_COUNT     BIGINT         NULL,
                    STATUS        NVARCHAR(20)   NOT NULL DEFAULT 'active',
                    CREATED_BY    NVARCHAR(128)  NULL,
                    CREATED_AT    DATETIME2(0)   NOT NULL DEFAULT SYSUTCDATETIME(),
                    UPDATED_BY    NVARCHAR(128)  NULL,
                    UPDATED_AT    DATETIME2(0)   NOT NULL DEFAULT SYSUTCDATETIME()
                )
            """))
            c.execute(text(f"""
                IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UX_{VIEW_TABLE}_name')
                CREATE UNIQUE INDEX UX_{VIEW_TABLE}_name
                    ON dbo.{VIEW_TABLE} (SF_DATABASE, SF_SCHEMA, VIEW_NAME)
            """))
            c.execute(text(f"""
                IF OBJECT_ID('dbo.{VIEW_VERSION_TABLE}', 'U') IS NULL
                CREATE TABLE dbo.{VIEW_VERSION_TABLE} (
                    VERSION_ID    INT IDENTITY(1,1) PRIMARY KEY,
                    VIEW_ID       INT            NOT NULL,
                    VERSION       INT            NOT NULL,
                    ACTION        NVARCHAR(20)   NOT NULL,   -- created | replaced | dropped
                    VIEW_SQL      NVARCHAR(MAX)  NULL,
                    COLUMN_COUNT  INT            NULL,
                    ROW_COUNT     BIGINT         NULL,
                    CHANGED_BY    NVARCHAR(128)  NULL,
                    CHANGED_AT    DATETIME2(0)   NOT NULL DEFAULT SYSUTCDATETIME()
                )
            """))
            c.execute(text(f"""
                IF OBJECT_ID('dbo.{JOB_TABLE}', 'U') IS NULL
                CREATE TABLE dbo.{JOB_TABLE} (
                    JOB_ID          INT IDENTITY(1,1) PRIMARY KEY,
                    JOB_NAME        NVARCHAR(200)  NOT NULL,
                    DESCRIPTION     NVARCHAR(500)  NULL,
                    SOURCE_TYPE     NVARCHAR(20)   NOT NULL DEFAULT 'SNOWFLAKE',
                    SOURCE_OBJECT   NVARCHAR(400)  NOT NULL,   -- DB.SCHEMA.NAME
                    TARGET_TABLE    NVARCHAR(128)  NOT NULL,   -- GD_SF_* in Rep_data
                    LOAD_MODE       NVARCHAR(20)   NOT NULL DEFAULT 'replace',
                    KEY_COLS        NVARCHAR(MAX)  NULL,       -- JSON list
                    WATERMARK_COL   NVARCHAR(128)  NULL,
                    WATERMARK_VALUE NVARCHAR(100)  NULL,
                    TRIGGER_TYPE    NVARCHAR(20)   NOT NULL DEFAULT 'manual',
                    SCHEDULE_CONFIG NVARCHAR(MAX)  NULL,       -- JSON, times in IST
                    RETRY_ON_FAIL   BIT            NOT NULL DEFAULT 1,
                    RETRY_DELAY_MIN INT            NOT NULL DEFAULT 15,
                    ENABLED         BIT            NOT NULL DEFAULT 1,
                    NEXT_RUN_AT     DATETIME2(0)   NULL,       -- UTC
                    RETRY_AT        DATETIME2(0)   NULL,       -- UTC
                    LOCK_RUN_ID     BIGINT         NULL,
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
                )
            """))
            # Self-heal (2026-10-03): column mapping — rename / skip source columns.
            # COLUMN_MAP = what the user chose; APPLIED_COLUMN_MAP = the local
            # names the table actually has (so a rename can be done in place).
            for col in ("COLUMN_MAP", "APPLIED_COLUMN_MAP"):
                c.execute(text(f"""
                    IF COL_LENGTH('dbo.{JOB_TABLE}', '{col}') IS NULL
                    ALTER TABLE dbo.{JOB_TABLE} ADD {col} NVARCHAR(MAX) NULL
                """))
            # Self-heal (2026-10-01): per-job loader choice and the empty-source guard.
            c.execute(text(f"""
                IF COL_LENGTH('dbo.{JOB_TABLE}', 'LOADER_PREF') IS NULL
                ALTER TABLE dbo.{JOB_TABLE} ADD LOADER_PREF NVARCHAR(10) NOT NULL
                    CONSTRAINT DF_{JOB_TABLE}_LOADER_PREF DEFAULT 'bulk'
            """))
            c.execute(text(f"""
                IF COL_LENGTH('dbo.{JOB_TABLE}', 'ALLOW_EMPTY') IS NULL
                ALTER TABLE dbo.{JOB_TABLE} ADD ALLOW_EMPTY BIT NOT NULL
                    CONSTRAINT DF_{JOB_TABLE}_ALLOW_EMPTY DEFAULT 0
            """))
            c.execute(text(f"""
                IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_{JOB_TABLE}_due')
                CREATE INDEX IX_{JOB_TABLE}_due
                    ON dbo.{JOB_TABLE} (ENABLED, TRIGGER_TYPE, NEXT_RUN_AT)
            """))
            c.execute(text(f"""
                IF OBJECT_ID('dbo.{RUN_TABLE}', 'U') IS NULL
                CREATE TABLE dbo.{RUN_TABLE} (
                    RUN_ID             BIGINT IDENTITY(1,1) PRIMARY KEY,
                    JOB_ID             INT            NOT NULL,
                    JOB_NAME           NVARCHAR(200)  NULL,
                    SOURCE_TYPE        NVARCHAR(20)   NOT NULL DEFAULT 'SNOWFLAKE',
                    SOURCE_OBJECT      NVARCHAR(400)  NULL,
                    TARGET_TABLE       NVARCHAR(128)  NULL,
                    LOAD_MODE          NVARCHAR(20)   NULL,
                    RUN_TYPE           NVARCHAR(10)   NOT NULL,   -- AUTO | MANUAL
                    TRIGGERED_BY       NVARCHAR(128)  NULL,
                    ATTEMPT            INT            NOT NULL DEFAULT 1,
                    FULL_RELOAD        BIT            NOT NULL DEFAULT 0,
                    STATUS             NVARCHAR(20)   NOT NULL DEFAULT 'running',
                    STEP               NVARCHAR(200)  NULL,
                    SOURCE_ROWS        BIGINT         NULL,
                    ROWS_LOADED        BIGINT         NULL,
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
                )
            """))
            # Self-heal: which loader a run used (bulk | classic), added 2026-10-01.
            c.execute(text(f"""
                IF COL_LENGTH('dbo.{RUN_TABLE}', 'LOADER') IS NULL
                ALTER TABLE dbo.{RUN_TABLE} ADD LOADER NVARCHAR(20) NULL
            """))
            # Self-heal: the column mapping a run used (source → local name), 2026-10-03.
            c.execute(text(f"""
                IF COL_LENGTH('dbo.{RUN_TABLE}', 'COLUMN_MAP') IS NULL
                ALTER TABLE dbo.{RUN_TABLE} ADD COLUMN_MAP NVARCHAR(MAX) NULL
            """))
            c.execute(text(f"""
                IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_{RUN_TABLE}_job')
                CREATE INDEX IX_{RUN_TABLE}_job ON dbo.{RUN_TABLE} (JOB_ID, RUN_ID DESC)
            """))
            c.execute(text(f"""
                IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_{RUN_TABLE}_started')
                CREATE INDEX IX_{RUN_TABLE}_started ON dbo.{RUN_TABLE} (STARTED_AT DESC)
                    INCLUDE (RUN_TYPE, STATUS, ROWS_LOADED)
            """))
        _ready = True
