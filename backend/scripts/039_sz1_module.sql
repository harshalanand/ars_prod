/* ============================================================================
   039_sz1_module.sql
   1SZ ALC — size allocation of broken-size warehouse stock. The five
   ARS_SZ1_* tables.
   Database: Rep_Data (Data DB).  Created: 2026-10-09.

   This file is the ONE source of the schema. app/services/sz1_schema.py reads
   it and executes it batch by batch (split on GO) the first time the module is
   used, and again from 1SZ ALC → "Repair tables". Every statement is
   idempotent — safe to re-run at any time.

   Ported from the SZ1 Allocation Streamlit tool (1SZ ALLOC: msa_base.py,
   req_base.py, art_store.py), which owns the SZ1_* tables. The ARS_SZ1_*
   prefix keeps the two apart, so both can run side by side on the same
   ARS_MSA_TOTAL / grid data — which is how the port is proven
   (scripts/sz1_parity.py). Spec: frontend/public/docs/manual/sz1_alloc.md.

   Changes from the Streamlit schema, each on purpose:
     * The tool drops and recreates SZ1_MSA_SZ_BASE and SZ1_ST_MJ_SZ_REQ_BASE
       on every build. Here the tables are permanent and a build replaces
       their rows inside one transaction (TRUNCATE + INSERT + balance push),
       so a failed or cancelled build leaves the previous build in place.
     * BUILD_ID on both base tables, logged in ARS_SZ1_BUILD (who, when, how
       many rows, the summary shown on the page).
     * ARS_SZ1_SESSION — one header row per run: every setting, the store
       list, status, progress, totals and the allocation-limit checks. The
       tool derives its sessions from the line table, so a run that failed
       half-way left no trace and a setting could only be read off a line.
     * SESSION_ID is NVARCHAR(12): DDMMYYHHMM as in the tool, and
       DDMMYYHHMMSS only when a second run lands in the same minute (the tool
       silently replaced the earlier run instead).
     * The line table also keeps MJ_STK, MJ_MBQ and MJ_SRT, so the full-option
       cap (MJ_SRT) can be checked on the stored rows.
   ============================================================================ */


/* ── 1. Step 1 — Generate SZ Base: the broken-size candidates ────────────── */
IF OBJECT_ID('dbo.ARS_SZ1_MSA_SZ_BASE', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_SZ1_MSA_SZ_BASE (
        ROW_ID          BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        BUILD_ID        INT            NULL,
        RDC             NVARCHAR(200)  NOT NULL,
        MAJ_CAT         NVARCHAR(200)  NOT NULL,
        SZ_APPLICABLE   NVARCHAR(1)    NULL,
        GEN_ART_NUMBER  NVARCHAR(200)  NULL,
        CLR             NVARCHAR(200)  NULL,
        OPT_KEY         NVARCHAR(620)  NULL,
        ARTICLE_NUMBER  NVARCHAR(200)  NOT NULL,
        SZ              NVARCHAR(200)  NULL,
        ALLOC_TYPE      NVARCHAR(25)   NULL,
        STK_QTY         DECIMAL(18,2)  NULL,
        PEND_QTY        DECIMAL(18,2)  NULL,
        HOLD_QTY        DECIMAL(18,2)  NULL,
        FNL_Q           DECIMAL(18,2)  NULL,
        PAK_SZ          DECIMAL(18,2)  NULL,
        BAL_FNL_Q       DECIMAL(18,2)  NULL,   -- FNL_Q less the latest session (chain)
        VAR_COUNT       INT            NULL,
        SZ_COUNT        INT            NULL,
        OPT_FNL         DECIMAL(18,2)  NULL,
        CAT_FNL         DECIMAL(18,2)  NULL,
        R1_LOW_VAR      BIT            NOT NULL,
        R2_SINGLE_A     BIT            NOT NULL,
        R3_CAT_LE200    BIT            NOT NULL,
        R4_OPT_LE20     BIT            NOT NULL,
        RULES           NVARCHAR(50)   NULL,
        BUILT_AT        DATETIME2(0)   NOT NULL CONSTRAINT DF_ARS_SZ1_MSA_BUILT DEFAULT (SYSDATETIME())
    );
    CREATE INDEX IX_ARS_SZ1_MSA_RDC_CAT ON dbo.ARS_SZ1_MSA_SZ_BASE (RDC, MAJ_CAT);
    CREATE INDEX IX_ARS_SZ1_MSA_OPT     ON dbo.ARS_SZ1_MSA_SZ_BASE (RDC, MAJ_CAT, GEN_ART_NUMBER, CLR);
    CREATE INDEX IX_ARS_SZ1_MSA_ARTICLE ON dbo.ARS_SZ1_MSA_SZ_BASE (ARTICLE_NUMBER);
    CREATE INDEX IX_ARS_SZ1_MSA_CAT_SZ  ON dbo.ARS_SZ1_MSA_SZ_BASE (MAJ_CAT, SZ);
END
GO


/* Company size contribution (2026-10-09): Master_CONT_SZ holds stores + 'CO',
   no RDC rows, so Step 1 (per RDC) reads the CO curve — the listing ladder at
   company level. Added in place so an existing table keeps its rows. */
IF COL_LENGTH('dbo.ARS_SZ1_MSA_SZ_BASE', 'ART_CONT_PCT') IS NULL
    ALTER TABLE dbo.ARS_SZ1_MSA_SZ_BASE ADD ART_CONT_PCT DECIMAL(9,4) NULL;   -- CO CONT × 100 of the article's size
IF COL_LENGTH('dbo.ARS_SZ1_MSA_SZ_BASE', 'OPT_CONT_PCT') IS NULL
    ALTER TABLE dbo.ARS_SZ1_MSA_SZ_BASE ADD OPT_CONT_PCT DECIMAL(9,4) NULL;   -- Σ ART_CONT_PCT of the option's live sizes
IF COL_LENGTH('dbo.ARS_SZ1_MSA_SZ_BASE', 'CONT_SRC') IS NULL
    ALTER TABLE dbo.ARS_SZ1_MSA_SZ_BASE ADD CONT_SRC NVARCHAR(20) NULL;       -- CO | CO_SIZE_MISSING | NO_CO_CURVE
GO


/* ── 2. Step 2 — Generate ST/MJ/SZ req: store × MAJ_CAT × size need ──────── */
IF OBJECT_ID('dbo.ARS_SZ1_ST_MJ_SZ_REQ_BASE', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_SZ1_ST_MJ_SZ_REQ_BASE (
        ROW_ID        BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        BUILD_ID      INT            NULL,
        RDC           NVARCHAR(10)   NOT NULL,
        ST_CD         NVARCHAR(20)   NOT NULL,
        ST_NM         NVARCHAR(100)  NULL,
        SEG           NVARCHAR(50)   NULL,
        DIV           NVARCHAR(50)   NULL,
        SUB_DIV       NVARCHAR(50)   NULL,
        MAJ_CAT       NVARCHAR(200)  NOT NULL,
        SSN           NVARCHAR(20)   NULL,
        SZ            NVARCHAR(50)   NOT NULL,
        SZ_APPLICABLE NVARCHAR(1)    NULL,

        CONT_PCT_SZ     DECIMAL(9,4) NULL,   -- BGT template: CONT_%_SZ
        CONT_PCT_SZ_REV DECIMAL(9,4) NULL,   -- BGT template: CONT_%_SZ_REV (9.99 where 0 / blank)
        CONT_SRC        NVARCHAR(20) NULL,   -- which rule supplied CONT_PCT_SZ
        SZ_COUNT        INT          NULL,   -- real sizes (not A / NA), RDC + MAJ_CAT

        MJ_STK        DECIMAL(18,2)  NULL,
        MJ_DISP       DECIMAL(18,2)  NULL,
        MJ_SAL_PD     DECIMAL(18,4)  NULL,
        MJ_STR        DECIMAL(18,2)  NULL,
        ALC_D         DECIMAL(18,2)  NULL,
        ACS_D         DECIMAL(18,2)  NULL,
        MJ_MBQ        DECIMAL(18,2)  NULL,
        MJ_SRT        DECIMAL(18,2)  NULL,
        MJ_EXC        DECIMAL(18,2)  NULL,

        MJ_SZ_STK     DECIMAL(18,2)  NULL,
        MJ_SZ_DISP    DECIMAL(18,2)  NULL,
        MJ_SZ_SAL_PD  DECIMAL(18,4)  NULL,
        MJ_SZ_MBQ     DECIMAL(18,2)  NULL,
        MJ_SZ_SRT     DECIMAL(18,2)  NULL,
        REM_MJ_SZ_SRT DECIMAL(18,2)  NULL,   -- MJ_SZ_SRT less the latest session (chain)
        MJ_SZ_EXC     DECIMAL(18,2)  NULL,

        BUILT_AT      DATETIME2(0)   NOT NULL CONSTRAINT DF_ARS_SZ1_REQ_BUILT DEFAULT (SYSDATETIME()),
        CONSTRAINT UQ_ARS_SZ1_REQ_BASE UNIQUE (RDC, ST_CD, MAJ_CAT, SZ)
    );
    CREATE INDEX IX_ARS_SZ1_REQ_STORE ON dbo.ARS_SZ1_ST_MJ_SZ_REQ_BASE (RDC, ST_CD);
    CREATE INDEX IX_ARS_SZ1_REQ_CAT   ON dbo.ARS_SZ1_ST_MJ_SZ_REQ_BASE (MAJ_CAT, SZ);
    CREATE INDEX IX_ARS_SZ1_REQ_SHORT ON dbo.ARS_SZ1_ST_MJ_SZ_REQ_BASE (ST_CD) INCLUDE (MJ_SZ_SRT);
END
GO


/* ── 3. Step 3 — Store × article run: one row per candidate line ─────────── */
IF OBJECT_ID('dbo.ARS_SZ1_ST_ART_REQ', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_SZ1_ST_ART_REQ (
        ROW_ID          BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        SESSION_ID      NVARCHAR(12)   NOT NULL,   -- DDMMYYHHMM[SS]
        SESSION_AT      DATETIME2(0)   NOT NULL,
        STOCK_BASIS     NVARCHAR(10)   NULL,       -- initial | balance
        CROSS_RDC       NVARCHAR(12)   NULL,       -- same | home_first | pooled | fallback
        RDC             NVARCHAR(10)   NOT NULL,   -- the store's own warehouse
        SRC_RDC         NVARCHAR(10)   NOT NULL,   -- warehouse the stock comes from
        IS_HOME         BIT            NOT NULL,   -- 1 when SRC_RDC = RDC
        ST_CD           NVARCHAR(20)   NOT NULL,
        ST_NM           NVARCHAR(100)  NULL,
        MAJ_CAT         NVARCHAR(200)  NOT NULL,
        SZ              NVARCHAR(50)   NOT NULL,
        SZ_APPLICABLE   NVARCHAR(1)    NULL,
        GEN_ART_NUMBER  NVARCHAR(200)  NULL,
        CLR             NVARCHAR(200)  NULL,
        ARTICLE_NUMBER  NVARCHAR(200)  NOT NULL,
        ART_FNL_Q       DECIMAL(18,2)  NULL,   -- warehouse stock used (FNL_Q or BAL_FNL_Q)
        PAK_SZ          DECIMAL(18,2)  NULL,
        ALC_D           DECIMAL(18,2)  NULL,
        ACS_D           DECIMAL(18,2)  NULL,
        MJ_STR          DECIMAL(18,2)  NULL,
        MJ_SZ_SAL_PD    DECIMAL(18,4)  NULL,
        CONT_PCT_SZ_REV DECIMAL(9,4)   NULL,
        MJ_STK          DECIMAL(18,2)  NULL,
        MJ_MBQ          DECIMAL(18,2)  NULL,
        MJ_SRT          DECIMAL(18,2)  NULL,
        MJ_SZ_STK       DECIMAL(18,2)  NULL,
        MJ_SZ_MBQ       DECIMAL(18,2)  NULL,
        MJ_SZ_SRT       DECIMAL(18,2)  NULL,

        ART_DISP        DECIMAL(18,2)  NULL,   -- ACS_D × CONT_PCT_SZ_REV / 100
        ART_SAL_PD      DECIMAL(18,4)  NULL,   -- ART_DISP / MJ_STR (90 when unusable)
        ART_STK         DECIMAL(18,2)  NULL,   -- store stock of this article
        ART_MBQ         DECIMAL(18,2)  NULL,   -- one-round target, ≥ 1 when ART_DISP > 0
        ART_SRT         DECIMAL(18,2)  NULL,   -- packround(ART_MBQ − ART_STK)

        I_ROD           DECIMAL(18,2)  NULL,   -- refill rounds entitled to
        ART_SRT_ROD     DECIMAL(18,2)  NULL,   -- packround(I_ROD × ART_MBQ − ART_STK)

        ST_SEQ          INT            NULL,   -- store order inside MAJ_CAT + SZ
        PRIORITY_MODE   NVARCHAR(20)   NULL,
        ROUND_ENVELOPE  NVARCHAR(10)   NULL,
        ALLOC_QTY       DECIMAL(18,2)  NULL,
        ALC_I_ROD       INT            NULL,   -- last round that shipped (0 = nothing)
        PACKS           INT            NULL,
        UNMET_QTY       DECIMAL(18,2)  NULL,   -- ART_SRT_ROD − all the store got of the article
        BAL_ART_FNL_Q   DECIMAL(18,2)  NULL,   -- warehouse stock left for the article
        REM_MJ_SZ_SRT   DECIMAL(18,2)  NULL,   -- size shortage left for the store

        OWN_NEED        INT            NULL,   -- SRC_RDC's own stores' claim, whole units
        SURPLUS_QTY     INT            NULL,   -- stock − OWN_NEED: what other-RDC stores may take

        BUILT_AT        DATETIME2(0)   NOT NULL CONSTRAINT DF_ARS_SZ1_ART_BUILT DEFAULT (SYSDATETIME()),
        CONSTRAINT UQ_ARS_SZ1_ST_ART UNIQUE (SESSION_ID, ST_CD, ARTICLE_NUMBER, SRC_RDC)
    );
    CREATE INDEX IX_ARS_SZ1_ART_STORE ON dbo.ARS_SZ1_ST_ART_REQ (SESSION_ID, ST_CD);
    CREATE INDEX IX_ARS_SZ1_ART_SRC   ON dbo.ARS_SZ1_ST_ART_REQ (SESSION_ID, SRC_RDC, ARTICLE_NUMBER);
    CREATE INDEX IX_ARS_SZ1_ART_CAT   ON dbo.ARS_SZ1_ST_ART_REQ (SESSION_ID, MAJ_CAT, SZ);
END
GO


/* ── 4. One row per Step 3 run ───────────────────────────────────────────── */
IF OBJECT_ID('dbo.ARS_SZ1_SESSION', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_SZ1_SESSION (
        SESSION_ID      NVARCHAR(12)   NOT NULL PRIMARY KEY,
        SESSION_AT      DATETIME2(0)   NOT NULL,
        STATUS          NVARCHAR(12)   NOT NULL,   -- RUNNING | PARKED (legacy DONE) | APPROVED | DISCARDED | FAILED | CANCELLED
        PRIORITY_MODE   NVARCHAR(20)   NULL,       -- weighted | mj_srt | sequence
        WEIGHT_MJ       DECIMAL(4,2)   NULL,
        ROUND_ENVELOPE  NVARCHAR(10)   NULL,
        STOCK_BASIS     NVARCHAR(10)   NULL,
        CROSS_RDC       NVARCHAR(12)   NULL,
        BASE_BUILD_ID   INT            NULL,       -- Step 1 build the run read
        REQ_BUILD_ID    INT            NULL,       -- Step 2 build the run read
        STORES_GIVEN    INT            NULL,
        STORES_OK       INT            NULL,
        STORE_LIST      NVARCHAR(MAX)  NULL,       -- JSON: the ok stores, and the sequence if one was given
        SKIPPED_JSON    NVARCHAR(MAX)  NULL,       -- JSON: codes not used, with their status
        ROWS_WRITTEN    INT            NULL,
        STORES_SERVED   INT            NULL,
        LINES_SERVED    INT            NULL,
        ASK_QTY         DECIMAL(18,2)  NULL,       -- Σ ART_SRT (one round)
        ASK_QTY_ROD     DECIMAL(18,2)  NULL,       -- Σ ART_SRT_ROD (all rounds)
        ALLOC_QTY       DECIMAL(18,2)  NULL,
        PACKS           INT            NULL,
        CROSS_QTY       DECIMAL(18,2)  NULL,       -- units shipped from another RDC
        CHECKS_PASSED   BIT            NULL,
        CHECKS_JSON     NVARCHAR(MAX)  NULL,
        SUMMARY_JSON    NVARCHAR(MAX)  NULL,
        PROGRESS        NVARCHAR(400)  NULL,
        PROGRESS_PCT    INT            NULL,
        ERROR           NVARCHAR(MAX)  NULL,
        NOTE            NVARCHAR(200)  NULL,
        CREATED_BY      NVARCHAR(100)  NULL,
        CREATED_AT      DATETIME2(0)   NOT NULL CONSTRAINT DF_ARS_SZ1_SESSION_AT DEFAULT (SYSDATETIME()),
        FINISHED_AT     DATETIME2(0)   NULL,
        DURATION_SEC    DECIMAL(9,1)   NULL
    );
    CREATE INDEX IX_ARS_SZ1_SESSION_AT ON dbo.ARS_SZ1_SESSION (STATUS, SESSION_AT DESC);
END
GO


/* ── 7. Step 1 CONT gap: articles with no company size contribution ─────── */
/* Rebuilt with every Step 1 build. GAP_TYPE: OPT_NO_CONT = the whole option has
   0 CONT (OPT_CONT_PCT = 0); SZ_NO_CONT = this size has 0 CONT in an option
   that has some. EXCLUDED = 1 when the gap rule kept a row out of SZ Base that
   a rule would have admitted. */
IF OBJECT_ID('dbo.ARS_SZ1_GAP', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_SZ1_GAP (
        ROW_ID          BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        BUILD_ID        INT            NULL,
        RDC             NVARCHAR(200)  NOT NULL,
        MAJ_CAT         NVARCHAR(200)  NOT NULL,
        GEN_ART_NUMBER  NVARCHAR(200)  NULL,
        CLR             NVARCHAR(200)  NULL,
        OPT_KEY         NVARCHAR(620)  NULL,
        ARTICLE_NUMBER  NVARCHAR(200)  NOT NULL,
        SZ              NVARCHAR(200)  NULL,
        FNL_Q           DECIMAL(18,2)  NULL,
        PAK_SZ          DECIMAL(18,2)  NULL,
        VAR_COUNT       INT            NULL,
        OPT_FNL         DECIMAL(18,2)  NULL,
        CAT_FNL         DECIMAL(18,2)  NULL,
        ART_CONT_PCT    DECIMAL(9,4)   NULL,
        OPT_CONT_PCT    DECIMAL(9,4)   NULL,
        CONT_SRC        NVARCHAR(20)   NULL,
        GAP_TYPE        NVARCHAR(20)   NOT NULL,   -- OPT_NO_CONT | SZ_NO_CONT
        RULES_IF_KEPT   NVARCHAR(50)   NULL,       -- the rules that would have admitted it
        EXCLUDED        BIT            NOT NULL,   -- kept out of SZ Base by the gap rule
        BUILT_AT        DATETIME2(0)   NOT NULL CONSTRAINT DF_ARS_SZ1_GAP_BUILT DEFAULT (SYSDATETIME())
    );
    CREATE INDEX IX_ARS_SZ1_GAP_TYPE ON dbo.ARS_SZ1_GAP (GAP_TYPE, RDC, MAJ_CAT);
END
GO


/* ── 6. Step 1 rule settings (R1–R4 switches and values), one row per key ── */
IF OBJECT_ID('dbo.ARS_SZ1_SETTING', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_SZ1_SETTING (
        SETTING_KEY   NVARCHAR(40)   NOT NULL PRIMARY KEY,
        SETTING_VALUE NVARCHAR(100)  NULL,
        UPDATED_BY    NVARCHAR(100)  NULL,
        UPDATED_AT    DATETIME2(0)   NOT NULL CONSTRAINT DF_ARS_SZ1_SETTING_AT DEFAULT (SYSDATETIME())
    );
END
GO


/* ── Phase 3 (2026-10-12): park → approve into Pend ALC, live netting ───── */
/* Session lifecycle: RUNNING → PARKED (a finished run; legacy rows say DONE
   and mean the same) → APPROVED (posted to ARS_PEND_ALC) or DISCARDED; also
   FAILED / CANCELLED. A reverted approval goes back to PARKED. Every PARKED
   (and legacy DONE) session holds what it allocated until approved or
   discarded. Columns added in place so existing rows are kept. */
IF COL_LENGTH('dbo.ARS_SZ1_SESSION', 'APPROVED_BY') IS NULL
    ALTER TABLE dbo.ARS_SZ1_SESSION ADD APPROVED_BY NVARCHAR(100) NULL;
IF COL_LENGTH('dbo.ARS_SZ1_SESSION', 'APPROVED_AT') IS NULL
    ALTER TABLE dbo.ARS_SZ1_SESSION ADD APPROVED_AT DATETIME2(0) NULL;
IF COL_LENGTH('dbo.ARS_SZ1_SESSION', 'PEND_SESSION_ID') IS NULL
    ALTER TABLE dbo.ARS_SZ1_SESSION ADD PEND_SESSION_ID NVARCHAR(50) NULL;   -- ARS_PEND_ALC.SESSION_ID it was posted as
IF COL_LENGTH('dbo.ARS_SZ1_SESSION', 'CLOSED_BY') IS NULL
    ALTER TABLE dbo.ARS_SZ1_SESSION ADD CLOSED_BY NVARCHAR(100) NULL;        -- who discarded it
IF COL_LENGTH('dbo.ARS_SZ1_SESSION', 'CLOSED_AT') IS NULL
    ALTER TABLE dbo.ARS_SZ1_SESSION ADD CLOSED_AT DATETIME2(0) NULL;
IF COL_LENGTH('dbo.ARS_SZ1_SESSION', 'REFIT_OF') IS NULL
    ALTER TABLE dbo.ARS_SZ1_SESSION ADD REFIT_OF NVARCHAR(12) NULL;          -- the session this one re-fits
/* Step 2 nets the store's open pending (ARS_PEND_ALC, IS_CLOSED = 0, any
   SOURCE) at build time: MJ_SRT = max(MJ_MBQ − MJ_STK − MJ_PEND, 0) and
   MJ_SZ_SRT = max(MJ_SZ_MBQ − MJ_SZ_STK − MJ_SZ_PEND, 0). */
IF COL_LENGTH('dbo.ARS_SZ1_ST_MJ_SZ_REQ_BASE', 'MJ_PEND') IS NULL
    ALTER TABLE dbo.ARS_SZ1_ST_MJ_SZ_REQ_BASE ADD MJ_PEND DECIMAL(18,2) NULL;     -- open pending, store + MAJ_CAT
IF COL_LENGTH('dbo.ARS_SZ1_ST_MJ_SZ_REQ_BASE', 'MJ_SZ_PEND') IS NULL
    ALTER TABLE dbo.ARS_SZ1_ST_MJ_SZ_REQ_BASE ADD MJ_SZ_PEND DECIMAL(18,2) NULL;  -- the same per size (A / NA: MJ_PEND)
GO


/* ── 5. One row per Step 1 / Step 2 build ────────────────────────────────── */
IF OBJECT_ID('dbo.ARS_SZ1_BUILD', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_SZ1_BUILD (
        BUILD_ID      INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        STEP          TINYINT        NOT NULL,     -- 1 = SZ Base, 2 = ST/MJ/SZ req
        STATUS        NVARCHAR(12)   NOT NULL,     -- RUNNING | DONE | FAILED
        ROWS_BUILT    INT            NULL,
        SUMMARY_JSON  NVARCHAR(MAX)  NULL,
        PROGRESS      NVARCHAR(400)  NULL,
        ERROR         NVARCHAR(MAX)  NULL,
        CREATED_BY    NVARCHAR(100)  NULL,
        STARTED_AT    DATETIME2(0)   NOT NULL CONSTRAINT DF_ARS_SZ1_BUILD_AT DEFAULT (SYSDATETIME()),
        FINISHED_AT   DATETIME2(0)   NULL,
        DURATION_SEC  DECIMAL(9,1)   NULL
    );
    CREATE INDEX IX_ARS_SZ1_BUILD_STEP ON dbo.ARS_SZ1_BUILD (STEP, STATUS, BUILD_ID DESC);
END
GO
