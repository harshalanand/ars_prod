/* ============================================================================
   024_upc_store_tracking.sql
   Tables for the UPC Store Tracking module (store-opening lifecycle tracker).

   Replaces the manual "STORE OPENING DATES *.xlsx" workbook. The user uploads
   only ST_CD + proposed opening date + share date; the rest of the identity
   (name/RDC/hub/status/op-date/priority) is joined LIVE from
   Master_ALC_INPUT_ST_MASTER, and the metrics (MBQ / total stock / SLOC-wise /
   fill-rate / display qty) are joined LIVE from the latest TREND_ST row per
   store. Only the *tracking state* (dates given, remarks, layout) is persisted
   here.

   History is EVENT-BASED: every time a proposed opening date or a remark is
   submitted (via upload or edit) an immutable row is appended to the *_HIST
   tables, so "how many times a date was given / changed" and "how many times
   remarks changed / last remark" are derivable and auditable.

   Runs on the app's DATA DB (Rep_data). The service also creates these lazily
   (services/upc_store_track_service.py :: ensure_tables), so this script is for
   documentation / explicit provisioning. Idempotent — safe to re-run.

   Gated by permission in code (see api/v1/endpoints/upc_store_track.py).
   ============================================================================ */

-- ── Head: one row per store (current tracking state + derived counters) ──────
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='ARS_UPC_STORE_TRACK')
BEGIN
    CREATE TABLE ARS_UPC_STORE_TRACK (
        id                    INT IDENTITY(1,1) PRIMARY KEY,
        st_cd                 NVARCHAR(20)  NOT NULL,

        -- proposed opening date lifecycle (derived from DATE_HIST)
        first_proposed_dt     DATE          NULL,   -- 1ST DATE  (first ever given)
        latest_proposed_dt    DATE          NULL,   -- LATEST BGT OP-DT (current)
        first_share_dt        DATE          NULL,   -- 1ST DATE SHARE (first snapshot)
        latest_share_dt       DATE          NULL,
        date_given_count      INT           NOT NULL DEFAULT 0,  -- GIVEN DAYS CNT
        date_change_count     INT           NOT NULL DEFAULT 0,  -- # times value changed

        -- layout / display tracking
        layout_generated      BIT           NOT NULL DEFAULT 0,
        layout_rec_dt         DATE          NULL,   -- LAYOUT REC_DT
        display_generated     BIT           NOT NULL DEFAULT 0,
        first_disp_dt         DATE          NULL,   -- PLAN/DISP_DT FIRST (1st display shared)

        -- outcome
        actual_open_dt        DATE          NULL,   -- ACT OP_DT
        status                NVARCHAR(20)  NULL,   -- ACTIVE | OPENED | HOLD | CANCELLED
        status_priority       NVARCHAR(40)  NULL,   -- STATUS PRIORITY (free-text)

        -- remarks lifecycle (derived from REMARK_HIST)
        last_remarks          NVARCHAR(MAX) NULL,
        remarks_change_count  INT           NOT NULL DEFAULT 0,

        created_at            DATETIME      NOT NULL DEFAULT GETDATE(),
        updated_at            DATETIME      NOT NULL DEFAULT GETDATE(),
        CONSTRAINT UQ_ARS_UPC_STORE_TRACK UNIQUE (st_cd)
    );
END
GO

-- add columns if the head table pre-existed without them
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_NAME='ARS_UPC_STORE_TRACK' AND COLUMN_NAME='status')
    ALTER TABLE ARS_UPC_STORE_TRACK ADD status NVARCHAR(20) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_NAME='ARS_UPC_STORE_TRACK' AND COLUMN_NAME='display_generated')
    ALTER TABLE ARS_UPC_STORE_TRACK ADD display_generated BIT NOT NULL DEFAULT 0;
GO

-- ── Date history: one immutable row per proposed-date/share submission ───────
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='ARS_UPC_STORE_TRACK_DATE_HIST')
BEGIN
    CREATE TABLE ARS_UPC_STORE_TRACK_DATE_HIST (
        id                 INT IDENTITY(1,1) PRIMARY KEY,
        st_cd              NVARCHAR(20)  NOT NULL,
        proposed_opening_dt DATE         NULL,
        share_dt           DATE          NULL,      -- date this value was shared
        changed            BIT           NOT NULL DEFAULT 0,  -- 1 if differs from prev proposed
        prev_proposed_dt   DATE          NULL,
        source             NVARCHAR(20)  NOT NULL DEFAULT 'upload', -- upload | edit | manual
        note               NVARCHAR(400) NULL,
        changed_by         NVARCHAR(100) NULL,
        changed_at         DATETIME      NOT NULL DEFAULT GETDATE()
    );
    CREATE INDEX IX_UPC_DATE_HIST_ST ON ARS_UPC_STORE_TRACK_DATE_HIST (st_cd, changed_at);
END
GO

-- ── Remark history: one immutable row per remark change ──────────────────────
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='ARS_UPC_STORE_TRACK_REMARK_HIST')
BEGIN
    CREATE TABLE ARS_UPC_STORE_TRACK_REMARK_HIST (
        id            INT IDENTITY(1,1) PRIMARY KEY,
        st_cd         NVARCHAR(20)  NOT NULL,
        remarks       NVARCHAR(MAX) NULL,
        prev_remarks  NVARCHAR(MAX) NULL,
        source        NVARCHAR(20)  NOT NULL DEFAULT 'edit',
        changed_by    NVARCHAR(100) NULL,
        changed_at    DATETIME      NOT NULL DEFAULT GETDATE()
    );
    CREATE INDEX IX_UPC_REMARK_HIST_ST ON ARS_UPC_STORE_TRACK_REMARK_HIST (st_cd, changed_at);
END
GO

-- ── Status history: one immutable row per lifecycle status change ────────────
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='ARS_UPC_STORE_TRACK_STATUS_HIST')
BEGIN
    CREATE TABLE ARS_UPC_STORE_TRACK_STATUS_HIST (
        id           INT IDENTITY(1,1) PRIMARY KEY,
        st_cd        NVARCHAR(20)  NOT NULL,
        status       NVARCHAR(20)  NULL,       -- new status
        prev_status  NVARCHAR(20)  NULL,       -- status it changed from
        source       NVARCHAR(20)  NOT NULL DEFAULT 'edit',
        changed_by   NVARCHAR(100) NULL,
        changed_at   DATETIME      NOT NULL DEFAULT GETDATE()
    );
    CREATE INDEX IX_UPC_STATUS_HIST_ST ON ARS_UPC_STORE_TRACK_STATUS_HIST (st_cd, changed_at);
END
GO
