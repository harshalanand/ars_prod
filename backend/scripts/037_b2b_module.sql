/* ============================================================================
   037_b2b_module.sql
   GRT ALC — Bin-to-Bin Transfer. The ten ARS_B2B_* tables.
   Database: Rep_Data (Data DB).  Created: 2026-09-29.

   This file is the ONE source of the schema. app/services/b2b_schema.py reads
   it and executes it batch by batch (split on GO) the first time the module is
   used, and again from GRT ALC → Overview → "Repair tables". Running it by hand
   is optional; it exists so a DB-first deployment is complete on its own.
   Every statement is idempotent — safe to re-run at any time.

   Ported from the Streamlit tool in GRT_ART_ALLOC (sql/01, sql/03), which owns
   the B2B_* tables. The ARS_B2B_* prefix keeps this module clear of those,
   of the July spec's ARS_BIN_* names, and of the ARS_BIN_*_DHYANU set. The two
   can run side by side on the same workbook, which is how the port is proven.

   Changes from the Streamlit schema, each on purpose:
     * Non-key columns are NULLable. The upload CHECK step reports blanks by
       column before anything is written; a NOT NULL constraint only fails the
       load half-way through with a less useful message.
     * ARS_B2B_BIN_MASTER.BIN_RDC — the warehouse, fixed once at load and
       checked against Store Master. The tool re-parses it from the BIN prefix
       on every query. But the workbooks themselves carry no prefix
       (A2-0101-B1); the live B2B_BIN_MASTER has DH24-D4-2407-A3 only because
       bins were prefixed by hand. So the warehouse is declared per workbook
       (detected from its file name, overridable), and a bin with no
       warehouse is given the same prefix the live data uses. The same zone
       code can exist in both RDCs, so without it a pick line could not say
       which warehouse to pick from.
     * UPLOAD_ID on every loaded row, BUILD_ID on every demand row, and a
       session records the BUILD_ID it ran against — so any number can be
       traced back to the workbook and the settings that produced it.
     * ARS_B2B_BIN_PLAN carries STORE_RDC and BIN_RDC. The tool's single RDC
       column held the store's warehouse, so a cross-warehouse pick was only
       visible by parsing the bin code.
     * No VW_BIN_ALLOC / VW_BIN_UNALLOC views — those names belong to the tool.
   ============================================================================ */


/* ── 1. Loaded from the workbook ─────────────────────────────────────────── */

IF OBJECT_ID(N'dbo.ARS_B2B_BIN_MASTER', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_B2B_BIN_MASTER
    (
        SEG         NVARCHAR(10)   NULL,
        DIV         NVARCHAR(20)   NULL,
        SUB_DIV     NVARCHAR(30)   NULL,
        MAJ_CAT     NVARCHAR(60)   NOT NULL,
        [SIZE]      NVARCHAR(20)   NULL,       -- blank for some articles
        SEASON      NVARCHAR(20)   NULL,
        ART         NVARCHAR(20)   NOT NULL,
        BIN         NVARCHAR(20)   NOT NULL,
        QTY         INT            NOT NULL,
        BIN_RDC     NVARCHAR(10)   NULL,       -- warehouse, from the BIN prefix
        UPLOAD_ID   INT            NULL
    );
    -- Non-unique on purpose, as in the tool: the CHECK step reports duplicate
    -- (ART, BIN) rows instead of letting the key fail the load.
    CREATE CLUSTERED INDEX CX_ARS_B2B_BIN_MASTER ON dbo.ARS_B2B_BIN_MASTER (ART, BIN);
    CREATE INDEX IX_ARS_B2B_BIN_MASTER_BIN     ON dbo.ARS_B2B_BIN_MASTER (BIN);
    CREATE INDEX IX_ARS_B2B_BIN_MASTER_MAJ_CAT ON dbo.ARS_B2B_BIN_MASTER (MAJ_CAT, [SIZE], SEASON);
    CREATE INDEX IX_ARS_B2B_BIN_MASTER_RDC     ON dbo.ARS_B2B_BIN_MASTER (BIN_RDC) INCLUDE (QTY);
END
GO

-- Phase 3: each allocation worker reads only its own category's bins.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_ARS_B2B_BIN_MASTER_CAT'
                  AND object_id = OBJECT_ID(N'dbo.ARS_B2B_BIN_MASTER'))
    CREATE INDEX IX_ARS_B2B_BIN_MASTER_CAT ON dbo.ARS_B2B_BIN_MASTER (MAJ_CAT)
        INCLUDE (QTY, BIN_RDC, SEG, DIV, SUB_DIV, SEASON);
GO

IF OBJECT_ID(N'dbo.ARS_B2B_STORE_MASTER', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_B2B_STORE_MASTER
    (
        STORE_CODE  NVARCHAR(10)   NOT NULL,
        ST_NM       NVARCHAR(100)  NULL,
        OLD_NEW     NVARCHAR(10)   NULL,       -- sheet header: OLD\NEW
        RDC         NVARCHAR(10)   NULL,
        UPLOAD_ID   INT            NULL,
        CONSTRAINT PK_ARS_B2B_STORE_MASTER PRIMARY KEY CLUSTERED (STORE_CODE)
    );
    CREATE INDEX IX_ARS_B2B_STORE_MASTER_RDC ON dbo.ARS_B2B_STORE_MASTER (RDC, OLD_NEW);
END
GO

-- No FK to Store Master on purpose: the REQ sheet routinely names stores the
-- master does not list. That gap is check G1, reported rather than rejected.
IF OBJECT_ID(N'dbo.ARS_B2B_REQ', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_B2B_REQ
    (
        STORE_CODE       NVARCHAR(10)   NOT NULL,
        ST_NM            NVARCHAR(100)  NULL,
        SEG              NVARCHAR(10)   NULL,
        DIV              NVARCHAR(20)   NULL,
        SUB_DIV          NVARCHAR(30)   NULL,
        MAJ_CAT          NVARCHAR(60)   NOT NULL,
        [SIZE]           NVARCHAR(20)   NULL,
        ACC_D            INT            NULL,  -- a CATEGORY figure: same on every size
        SEASON           NVARCHAR(20)   NULL,
        REQ              DECIMAL(18,6)  NOT NULL,
        SALE_COVER_DAYS  INT            NULL,  -- not in the workbook; set after load
        UPLOAD_ID        INT            NULL
    );
    CREATE CLUSTERED INDEX CX_ARS_B2B_REQ ON dbo.ARS_B2B_REQ (STORE_CODE, MAJ_CAT, [SIZE]);
    CREATE INDEX IX_ARS_B2B_REQ_MAJ_CAT ON dbo.ARS_B2B_REQ (MAJ_CAT, [SIZE], SEASON)
        INCLUDE (STORE_CODE, REQ);
END
GO

-- One row per upload attempt: the check, then (if confirmed) the load.
-- STATUS: CHECKING → CHECKED → LOADING → LOADED, or FAILED / CANCELLED /
-- EXPIRED. The page polls this row, so a refresh never loses a running job.
IF OBJECT_ID(N'dbo.ARS_B2B_UPLOAD', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_B2B_UPLOAD
    (
        UPLOAD_ID     INT IDENTITY(1,1) NOT NULL,
        STATUS        NVARCHAR(20)   NOT NULL,
        SOURCE_KIND   NVARCHAR(10)   NOT NULL,   -- PATH | FILE
        SOURCE_NAME   NVARCHAR(500)  NULL,
        FILE_BYTES    BIGINT         NULL,
        MODE          NVARCHAR(10)   NOT NULL,   -- OVERWRITE | APPEND
        SHEETS        NVARCHAR(100)  NULL,       -- which sheets this upload loads
        ROWS_BIN      INT            NULL,
        ROWS_STORE    INT            NULL,
        ROWS_REQ      INT            NULL,
        BLOCKING      INT            NULL,       -- checks that stop the load
        WARNINGS      INT            NULL,
        CHECKS_JSON   NVARCHAR(MAX)  NULL,
        PROGRESS      NVARCHAR(300)  NULL,
        PROGRESS_PCT  INT            NULL,
        ERROR         NVARCHAR(MAX)  NULL,
        STAGE_DIR     NVARCHAR(500)  NULL,
        CREATED_BY    NVARCHAR(100)  NULL,
        CREATED_AT    DATETIME2(0)   NOT NULL CONSTRAINT DF_ARS_B2B_UPLOAD_AT DEFAULT (SYSDATETIME()),
        CHECKED_AT    DATETIME2(0)   NULL,
        LOADED_AT     DATETIME2(0)   NULL,
        LOADED_BY     NVARCHAR(100)  NULL,
        READ_SEC      DECIMAL(9,1)   NULL,
        LOAD_SEC      DECIMAL(9,1)   NULL,
        OPTIONS_JSON  NVARCHAR(MAX)  NULL,       -- bin warehouse, column swap
        CONSTRAINT PK_ARS_B2B_UPLOAD PRIMARY KEY CLUSTERED (UPLOAD_ID)
    );
END
GO

-- Added after the first release of this file; guards a table created before it.
IF COL_LENGTH(N'dbo.ARS_B2B_UPLOAD', N'OPTIONS_JSON') IS NULL
    ALTER TABLE dbo.ARS_B2B_UPLOAD ADD OPTIONS_JSON NVARCHAR(MAX) NULL;
GO


/* ── 2. Settings ─────────────────────────────────────────────────────────── */

-- All 19 seeded on first use (b2b_settings.seed). SEED_SOURCE records where a
-- value came from: LEGACY (copied from the tool's B2B_MBQ_SETTING, so both
-- run on the same settings), DEFAULT (the tool's code default) or USER.
IF OBJECT_ID(N'dbo.ARS_B2B_SETTING', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_B2B_SETTING
    (
        SETTING_KEY    NVARCHAR(60)   NOT NULL,
        SETTING_VALUE  NVARCHAR(200)  NULL,
        SETTING_GROUP  NVARCHAR(10)   NOT NULL,   -- MBQ | ALLOC
        NEEDS_REBUILD  BIT            NOT NULL,   -- 1 = change → rebuild MBQ
        SEED_SOURCE    NVARCHAR(10)   NOT NULL,   -- LEGACY | DEFAULT | USER
        UPDATED_BY     NVARCHAR(100)  NULL,
        UPDATED_AT     DATETIME2(0)   NOT NULL CONSTRAINT DF_ARS_B2B_SETTING_AT DEFAULT (SYSDATETIME()),
        CONSTRAINT PK_ARS_B2B_SETTING PRIMARY KEY CLUSTERED (SETTING_KEY)
    );
END
GO


/* ── 3. Demand (Phase 2 fills these) ─────────────────────────────────────── */

-- Same columns as the tool's B2B_ART_MBQ, plus BUILD_ID. A clustered
-- columnstore with the tool's key as a NONCLUSTERED primary key: 2.3x faster
-- to build and 7x smaller than the tool's rowstore (measured, see
-- b2b_mbq_service). The build owns the shape — it drops the key before its
-- insert, adds it after, and converts a rowstore table it finds.
IF OBJECT_ID(N'dbo.ARS_B2B_ART_MBQ', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_B2B_ART_MBQ
    (
        STORE_CODE       NVARCHAR(10)   NOT NULL,
        ST_NM            NVARCHAR(100)  NULL,
        RDC              NVARCHAR(10)   NULL,
        ART              NVARCHAR(20)   NOT NULL,
        ART_NUM          BIGINT         NULL,
        SEG              NVARCHAR(10)   NULL,
        DIV              NVARCHAR(20)   NULL,
        SUB_DIV          NVARCHAR(30)   NULL,
        MAJ_CAT          NVARCHAR(60)   NULL,
        BIN_SIZE         NVARCHAR(20)   NULL,
        SZ               NVARCHAR(20)   NULL,
        SZ_SOURCE        NVARCHAR(10)   NULL,
        SEASON           NVARCHAR(20)   NULL,
        CONT             DECIMAL(18,6)  NULL,
        CONT_SOURCE      NVARCHAR(10)   NULL,
        CONT_EFF         DECIMAL(18,6)  NULL,
        CONT_RULE        NVARCHAR(12)   NULL,
        ACC_D            INT            NULL,
        ACC_D_EFF        DECIMAL(18,6)  NULL,
        NORM_DAYS        INT            NULL,
        SALE_COVER_DAYS  INT            NULL,
        MBQ_RAW          DECIMAL(18,4)  NULL,
        MBQ              DECIMAL(18,4)  NULL,
        MBQ_ROUNDED      INT            NULL,
        STK_TTL          DECIMAL(18,4)  NULL,
        EXCESS           DECIMAL(18,4)  NULL,
        SHORTFALL        DECIMAL(18,4)  NULL,
        BIN_QTY          INT            NULL,
        BIN_COUNT        INT            NULL,
        BUILD_ID         INT            NULL,
        CONSTRAINT PK_ARS_B2B_ART_MBQ PRIMARY KEY NONCLUSTERED (STORE_CODE, ART),
        INDEX CCI_ARS_B2B_ART_MBQ CLUSTERED COLUMNSTORE
    );
END
GO

-- One row per demand build: what it read, the settings it used, what it made.
IF OBJECT_ID(N'dbo.ARS_B2B_MBQ_BUILD', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_B2B_MBQ_BUILD
    (
        BUILD_ID         INT IDENTITY(1,1) NOT NULL,
        STATUS           NVARCHAR(20)   NOT NULL,   -- RUNNING | DONE | FAILED | CANCELLED
        UPLOAD_ID        INT            NULL,       -- the latest load it read
        SETTINGS_JSON    NVARCHAR(MAX)  NULL,
        ROWS_BUILT       INT            NULL,
        ROWS_SHORT       INT            NULL,
        ROWS_EXCESS      INT            NULL,
        SHORTFALL_TOTAL  DECIMAL(18,2)  NULL,
        EXCESS_TOTAL     DECIMAL(18,2)  NULL,
        PROGRESS         NVARCHAR(300)  NULL,
        PROGRESS_PCT     INT            NULL,
        ERROR            NVARCHAR(MAX)  NULL,
        CREATED_BY       NVARCHAR(100)  NULL,
        STARTED_AT       DATETIME2(0)   NOT NULL CONSTRAINT DF_ARS_B2B_MBQ_BUILD_AT DEFAULT (SYSDATETIME()),
        FINISHED_AT      DATETIME2(0)   NULL,
        DURATION_SEC     DECIMAL(9,1)   NULL,
        CONSTRAINT PK_ARS_B2B_MBQ_BUILD PRIMARY KEY CLUSTERED (BUILD_ID)
    );
END
GO

-- Phase 2: what the build proved about its own output, and what it read.
IF COL_LENGTH(N'dbo.ARS_B2B_MBQ_BUILD', N'SUMMARY_JSON') IS NULL
    ALTER TABLE dbo.ARS_B2B_MBQ_BUILD ADD
        SUMMARY_JSON     NVARCHAR(MAX)  NULL,    -- counts, totals, by category
        CHECKS_JSON      NVARCHAR(MAX)  NULL,    -- the build's own checks
        CHECKS_PASSED    BIT            NULL,    -- 0 = rolled back, previous build kept
        STEPS_JSON       NVARCHAR(MAX)  NULL,    -- seconds per step
        GRID_UPDATED_AT  DATETIME2(0)   NULL,    -- last change to the stock grid when read
        CONT_UPDATED_AT  DATETIME2(0)   NULL;    -- last change to Master_CONT_SZ when read
GO


/* ── 4. Allocation (Phase 3 fills these) ─────────────────────────────────── */

-- One row per run. Nothing is ever overwritten: a run you do not want is
-- deleted, the others are untouched.
IF OBJECT_ID(N'dbo.ARS_B2B_SESSION', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_B2B_SESSION
    (
        SESSION_ID        INT IDENTITY(1,1) NOT NULL,
        SESSION_TAG       NVARCHAR(40)   NULL,
        STATUS            NVARCHAR(20)   NOT NULL,   -- RUNNING | DONE | FAILED | CANCELLED
        DRY_RUN           BIT            NOT NULL CONSTRAINT DF_ARS_B2B_SESSION_DRY DEFAULT (0),
        BUILD_ID          INT            NULL,       -- the demand build it ran against
        ALLOC_PRIORITY    NVARCHAR(30)   NULL,
        ALLOC_MIN_QTY     INT            NULL,
        ALLOC_FILL_MODE   NVARCHAR(20)   NULL,
        ALLOC_FAIR_BASIS  NVARCHAR(20)   NULL,
        ALLOC_BIN_PICK    NVARCHAR(20)   NULL,
        ALLOC_CROSS_RDC   NVARCHAR(20)   NULL,
        SETTINGS_JSON     NVARCHAR(MAX)  NULL,       -- every setting, as used
        SUPPLY_UNITS      BIGINT         NULL,
        UNITS_ALLOCATED   BIGINT         NULL,
        UNITS_LEFT        BIGINT         NULL,
        CROSS_RDC_UNITS   BIGINT         NULL,
        ART_LINES         INT            NULL,
        BIN_LINES         INT            NULL,
        UNALLOC_LINES     INT            NULL,
        STORES_SERVED     INT            NULL,
        ARTS_USED         INT            NULL,
        SKIPS_JSON        NVARCHAR(MAX)  NULL,       -- skip counts by reason
        CHECKS_JSON       NVARCHAR(MAX)  NULL,       -- the four balance checks
        CHECKS_PASSED     BIT            NULL,
        NOTE              NVARCHAR(200)  NULL,
        PROGRESS          NVARCHAR(300)  NULL,
        PROGRESS_PCT      INT            NULL,
        ERROR             NVARCHAR(MAX)  NULL,
        CREATED_BY        NVARCHAR(100)  NULL,
        CREATED_AT        DATETIME2(0)   NOT NULL CONSTRAINT DF_ARS_B2B_SESSION_AT DEFAULT (SYSDATETIME()),
        FINISHED_AT       DATETIME2(0)   NULL,
        DURATION_SEC      DECIMAL(9,1)   NULL,
        CONSTRAINT PK_ARS_B2B_SESSION PRIMARY KEY CLUSTERED (SESSION_ID)
    );
END
GO

-- Phase 3: what the run measured about itself.
IF COL_LENGTH(N'dbo.ARS_B2B_SESSION', N'SUMMARY_JSON') IS NULL
    ALTER TABLE dbo.ARS_B2B_SESSION ADD
        SUMMARY_JSON  NVARCHAR(MAX)  NULL,      -- leftovers by reason, top categories, warnings
        STEPS_JSON    NVARCHAR(MAX)  NULL,      -- seconds per step
        WORKERS       INT            NULL;      -- processes the engine used
GO

-- Same columns as the tool's B2B_ART_ALLOC.
IF OBJECT_ID(N'dbo.ARS_B2B_ALLOC', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_B2B_ALLOC
    (
        SESSION_ID          INT            NOT NULL,
        STORE_CODE          NVARCHAR(10)   NOT NULL,
        ST_NM               NVARCHAR(100)  NULL,
        ART                 NVARCHAR(20)   NOT NULL,
        SEG                 NVARCHAR(10)   NULL,
        DIV                 NVARCHAR(20)   NULL,
        SUB_DIV             NVARCHAR(30)   NULL,
        MAJ_CAT             NVARCHAR(60)   NULL,
        BIN_SIZE            NVARCHAR(20)   NULL,
        SZ                  NVARCHAR(20)   NULL,
        SEASON              NVARCHAR(20)   NULL,
        CONT                DECIMAL(18,6)  NULL,
        MBQ_ROUNDED         INT            NULL,
        STK_TTL             DECIMAL(18,4)  NULL,
        SHORTFALL           DECIMAL(18,4)  NULL,
        ALLOC_SEQ           INT            NULL,   -- central rank: 1 = served first
        ALLOC_SEQ_GRP       INT            NULL,
        ART_BIN_QTY         INT            NULL,
        ART_BIN_LEFT        INT            NULL,
        REQ_CAP             INT            NULL,
        REQ_CAP_LEFT        INT            NULL,
        ALLOC_CUM           INT            NULL,
        ALLOC_QTY           INT            NOT NULL,
        RESIDUAL_SHORT      DECIMAL(18,4)  NULL,
        RESIDUAL_SHORT_GRP  DECIMAL(18,4)  NULL,
        STILL_SENDABLE      DECIMAL(18,4)  NULL,   -- the only "can more ship?" column
        CONSTRAINT PK_ARS_B2B_ALLOC PRIMARY KEY CLUSTERED (SESSION_ID, STORE_CODE, ART)
    );
    CREATE INDEX IX_ARS_B2B_ALLOC_ART ON dbo.ARS_B2B_ALLOC (SESSION_ID, ART);
    CREATE INDEX IX_ARS_B2B_ALLOC_SEQ ON dbo.ARS_B2B_ALLOC (SESSION_ID, ALLOC_SEQ);
    CREATE INDEX IX_ARS_B2B_ALLOC_CAT ON dbo.ARS_B2B_ALLOC
        (SESSION_ID, STORE_CODE, MAJ_CAT, BIN_SIZE, ALLOC_SEQ_GRP);
END
GO

-- Picks and leftovers in one table, marked by ROW_TYPE, so SUM(QTY) per
-- (BIN, ART) equals the bin's held quantity exactly. STORE_CODE is N'' on a
-- leftover row (it is part of the key, so it cannot be NULL).
IF OBJECT_ID(N'dbo.ARS_B2B_BIN_PLAN', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_B2B_BIN_PLAN
    (
        SESSION_ID       INT            NOT NULL,
        ROW_TYPE         NVARCHAR(8)    NOT NULL,   -- ALLOC | UNALLOC
        BIN              NVARCHAR(20)   NOT NULL,
        ART              NVARCHAR(20)   NOT NULL,
        STORE_CODE       NVARCHAR(10)   NOT NULL,
        ST_NM            NVARCHAR(100)  NULL,
        STORE_RDC        NVARCHAR(10)   NULL,       -- the store's warehouse
        BIN_RDC          NVARCHAR(10)   NULL,       -- the bin's warehouse
        SEG              NVARCHAR(10)   NULL,
        DIV              NVARCHAR(20)   NULL,
        SUB_DIV          NVARCHAR(30)   NULL,
        MAJ_CAT          NVARCHAR(60)   NULL,
        BIN_SIZE         NVARCHAR(20)   NULL,
        SZ               NVARCHAR(20)   NULL,
        SEASON           NVARCHAR(20)   NULL,
        BIN_QTY          INT            NULL,       -- snapshot at run time
        PICKED_QTY       INT            NULL,
        QTY              INT            NOT NULL,
        ALLOC_SEQ        INT            NULL,
        PICK_SEQ         INT            NULL,
        BIN_QTY_LEFT     INT            NULL,
        STORE_ALLOC_QTY  INT            NULL,
        LEFT_REASON      NVARCHAR(20)   NULL,
        STORES_WANTING   INT            NULL,
        TOTAL_SHORTFALL  DECIMAL(18,4)  NULL,
        CONSTRAINT PK_ARS_B2B_BIN_PLAN PRIMARY KEY CLUSTERED (SESSION_ID, BIN, ART, STORE_CODE),
        CONSTRAINT CK_ARS_B2B_BIN_PLAN_ROW_TYPE CHECK (ROW_TYPE IN (N'ALLOC', N'UNALLOC'))
    );
    CREATE INDEX IX_ARS_B2B_BIN_PLAN_TYPE   ON dbo.ARS_B2B_BIN_PLAN (SESSION_ID, ROW_TYPE);
    CREATE INDEX IX_ARS_B2B_BIN_PLAN_ART    ON dbo.ARS_B2B_BIN_PLAN (SESSION_ID, ART, BIN);
    CREATE INDEX IX_ARS_B2B_BIN_PLAN_STORE  ON dbo.ARS_B2B_BIN_PLAN (SESSION_ID, STORE_CODE, MAJ_CAT);
    CREATE INDEX IX_ARS_B2B_BIN_PLAN_REASON ON dbo.ARS_B2B_BIN_PLAN (SESSION_ID, LEFT_REASON);
END
GO
