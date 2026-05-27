/*
 * 2026_05_17_drop_fb_columns.sql
 * ─────────────────────────────────────────────────────────────────────
 * Drop the fallback-related columns left behind by the 2026-05-16
 * fallback removal. Engine no longer reads or writes these columns;
 * _ensure_phase_reason_cols stopped re-creating them on the same date
 * this script was authored.
 *
 * Tables touched:
 *   ARS_ALLOC_WORKING    FB_SHIP_QTY, SHIP_QTY_PRE_FB, FB_REASON
 *   ARS_LISTING_WORKING  FB_ALLOC_QTY, FB_REASON
 *   ARS_ALLOC_PARKED     FALLBACK_LVL, FB_SHIP_QTY, SHIP_QTY_PRE_FB, FB_REASON
 *
 * Safe to run:
 *   - Each DROP is guarded by an INFORMATION_SCHEMA existence check, so
 *     re-running the script is a no-op.
 *   - If you ever need the fallback feature again, the column shapes are
 *     documented in backend/app/docs/processes/fallback_archived.md §4.
 *
 * How to run:
 *   sqlcmd -S HOPC560 -d Rep_data -E -i 2026_05_17_drop_fb_columns.sql
 *   (or open in SSMS and execute against Rep_data)
 *
 * Verify after:
 *   SELECT TABLE_NAME, COLUMN_NAME
 *   FROM INFORMATION_SCHEMA.COLUMNS
 *   WHERE TABLE_NAME IN ('ARS_LISTING_WORKING','ARS_ALLOC_WORKING','ARS_ALLOC_PARKED')
 *     AND (COLUMN_NAME LIKE 'FB[_]%'
 *          OR COLUMN_NAME LIKE '%[_]PRE[_]FB'
 *          OR COLUMN_NAME = 'FALLBACK_LVL');
 *   -- expect 0 rows
 */

SET XACT_ABORT ON;
BEGIN TRANSACTION;

/* ── ARS_ALLOC_WORKING ─────────────────────────────────────────────── */

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME = 'ARS_ALLOC_WORKING' AND COLUMN_NAME = 'FB_SHIP_QTY')
BEGIN
    PRINT 'Dropping ARS_ALLOC_WORKING.FB_SHIP_QTY';
    /* Drop any default-constraint first (FB_SHIP_QTY had DEFAULT 0). */
    DECLARE @def1 NVARCHAR(200);
    SELECT @def1 = dc.name
    FROM sys.default_constraints dc
    INNER JOIN sys.columns c ON c.default_object_id = dc.object_id
    WHERE c.object_id = OBJECT_ID('dbo.ARS_ALLOC_WORKING') AND c.name = 'FB_SHIP_QTY';
    IF @def1 IS NOT NULL
        EXEC('ALTER TABLE [dbo].[ARS_ALLOC_WORKING] DROP CONSTRAINT [' + @def1 + ']');
    ALTER TABLE [dbo].[ARS_ALLOC_WORKING] DROP COLUMN [FB_SHIP_QTY];
END

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME = 'ARS_ALLOC_WORKING' AND COLUMN_NAME = 'SHIP_QTY_PRE_FB')
BEGIN
    PRINT 'Dropping ARS_ALLOC_WORKING.SHIP_QTY_PRE_FB';
    ALTER TABLE [dbo].[ARS_ALLOC_WORKING] DROP COLUMN [SHIP_QTY_PRE_FB];
END

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME = 'ARS_ALLOC_WORKING' AND COLUMN_NAME = 'FB_REASON')
BEGIN
    PRINT 'Dropping ARS_ALLOC_WORKING.FB_REASON';
    ALTER TABLE [dbo].[ARS_ALLOC_WORKING] DROP COLUMN [FB_REASON];
END

/* ── ARS_LISTING_WORKING ───────────────────────────────────────────── */

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME = 'ARS_LISTING_WORKING' AND COLUMN_NAME = 'FB_ALLOC_QTY')
BEGIN
    PRINT 'Dropping ARS_LISTING_WORKING.FB_ALLOC_QTY (will clear 3,438 stale non-zero rows)';
    DECLARE @def2 NVARCHAR(200);
    SELECT @def2 = dc.name
    FROM sys.default_constraints dc
    INNER JOIN sys.columns c ON c.default_object_id = dc.object_id
    WHERE c.object_id = OBJECT_ID('dbo.ARS_LISTING_WORKING') AND c.name = 'FB_ALLOC_QTY';
    IF @def2 IS NOT NULL
        EXEC('ALTER TABLE [dbo].[ARS_LISTING_WORKING] DROP CONSTRAINT [' + @def2 + ']');
    ALTER TABLE [dbo].[ARS_LISTING_WORKING] DROP COLUMN [FB_ALLOC_QTY];
END

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME = 'ARS_LISTING_WORKING' AND COLUMN_NAME = 'FB_REASON')
BEGIN
    PRINT 'Dropping ARS_LISTING_WORKING.FB_REASON';
    ALTER TABLE [dbo].[ARS_LISTING_WORKING] DROP COLUMN [FB_REASON];
END

/* ── ARS_ALLOC_PARKED ──────────────────────────────────────────────── */

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME = 'ARS_ALLOC_PARKED' AND COLUMN_NAME = 'FALLBACK_LVL')
BEGIN
    PRINT 'Dropping ARS_ALLOC_PARKED.FALLBACK_LVL';
    DECLARE @def3 NVARCHAR(200);
    SELECT @def3 = dc.name
    FROM sys.default_constraints dc
    INNER JOIN sys.columns c ON c.default_object_id = dc.object_id
    WHERE c.object_id = OBJECT_ID('dbo.ARS_ALLOC_PARKED') AND c.name = 'FALLBACK_LVL';
    IF @def3 IS NOT NULL
        EXEC('ALTER TABLE [dbo].[ARS_ALLOC_PARKED] DROP CONSTRAINT [' + @def3 + ']');
    ALTER TABLE [dbo].[ARS_ALLOC_PARKED] DROP COLUMN [FALLBACK_LVL];
END

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME = 'ARS_ALLOC_PARKED' AND COLUMN_NAME = 'FB_SHIP_QTY')
BEGIN
    PRINT 'Dropping ARS_ALLOC_PARKED.FB_SHIP_QTY';
    DECLARE @def4 NVARCHAR(200);
    SELECT @def4 = dc.name
    FROM sys.default_constraints dc
    INNER JOIN sys.columns c ON c.default_object_id = dc.object_id
    WHERE c.object_id = OBJECT_ID('dbo.ARS_ALLOC_PARKED') AND c.name = 'FB_SHIP_QTY';
    IF @def4 IS NOT NULL
        EXEC('ALTER TABLE [dbo].[ARS_ALLOC_PARKED] DROP CONSTRAINT [' + @def4 + ']');
    ALTER TABLE [dbo].[ARS_ALLOC_PARKED] DROP COLUMN [FB_SHIP_QTY];
END

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME = 'ARS_ALLOC_PARKED' AND COLUMN_NAME = 'SHIP_QTY_PRE_FB')
BEGIN
    PRINT 'Dropping ARS_ALLOC_PARKED.SHIP_QTY_PRE_FB';
    ALTER TABLE [dbo].[ARS_ALLOC_PARKED] DROP COLUMN [SHIP_QTY_PRE_FB];
END

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME = 'ARS_ALLOC_PARKED' AND COLUMN_NAME = 'FB_REASON')
BEGIN
    PRINT 'Dropping ARS_ALLOC_PARKED.FB_REASON';
    ALTER TABLE [dbo].[ARS_ALLOC_PARKED] DROP COLUMN [FB_REASON];
END

COMMIT TRANSACTION;
PRINT 'fallback column drop complete';
