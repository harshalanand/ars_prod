-- =============================================================================
-- Migration 018: ALLOC_TYPE integrity (P1)
--   1. CHECK constraints so no writer can store an invalid pool value
--      ('FRSH' typos etc. would silently fold-as-FRESH in some paths and
--      miss exact-match joins in others). Verified 2026-07-10: zero invalid
--      values exist, so plain ADD CONSTRAINT (WITH CHECK) is safe.
--      NOTE: ARS_ALLOC_WORKING is DROP+SELECT INTO per run — a constraint
--      would not survive; its writes are normalized in code instead.
--   2. ARS_LISTING_SESSIONS.ALLOC_TYPE — queryable run history (was only
--      inside REQUEST_JSON). Backfilled via JSON_VALUE.
-- =============================================================================

-- 1a) ARS_PEND_ALC: NULL (legacy) or FRESH/GRT
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME='ARS_PEND_ALC' AND COLUMN_NAME='ALLOC_TYPE')
AND NOT EXISTS (SELECT 1 FROM sys.check_constraints
                WHERE name='CK_ARS_PEND_ALC_alloc_type')
    ALTER TABLE ARS_PEND_ALC ADD CONSTRAINT CK_ARS_PEND_ALC_alloc_type
        CHECK (ALLOC_TYPE IS NULL OR ALLOC_TYPE IN ('FRESH','GRT'));
GO

-- 1b) ARS_NL_TBL_HOLD_TRACKING: '' (legacy sentinel, NOT NULL PK member) or FRESH/GRT
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME='ARS_NL_TBL_HOLD_TRACKING' AND COLUMN_NAME='ALLOC_TYPE')
AND NOT EXISTS (SELECT 1 FROM sys.check_constraints
                WHERE name='CK_NL_TBL_HOLD_alloc_type')
    ALTER TABLE ARS_NL_TBL_HOLD_TRACKING ADD CONSTRAINT CK_NL_TBL_HOLD_alloc_type
        CHECK (ALLOC_TYPE IN ('','FRESH','GRT'));
GO

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME='ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT' AND COLUMN_NAME='ALLOC_TYPE')
AND NOT EXISTS (SELECT 1 FROM sys.check_constraints
                WHERE name='CK_NL_TBL_HOLD_SNAP_alloc_type')
    ALTER TABLE ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT ADD CONSTRAINT CK_NL_TBL_HOLD_SNAP_alloc_type
        CHECK (ALLOC_TYPE IN ('','FRESH','GRT'));
GO

-- 1c) Parked + history (NULL for pre-change rows)
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME='ARS_ALLOC_PARKED' AND COLUMN_NAME='ALLOC_TYPE')
AND NOT EXISTS (SELECT 1 FROM sys.check_constraints
                WHERE name='CK_ARS_ALLOC_PARKED_alloc_type')
    ALTER TABLE ARS_ALLOC_PARKED ADD CONSTRAINT CK_ARS_ALLOC_PARKED_alloc_type
        CHECK (ALLOC_TYPE IS NULL OR ALLOC_TYPE IN ('','FRESH','GRT'));
GO

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME='ARS_ALLOC_HISTORY' AND COLUMN_NAME='ALLOC_TYPE')
AND NOT EXISTS (SELECT 1 FROM sys.check_constraints
                WHERE name='CK_ARS_ALLOC_HISTORY_alloc_type')
    ALTER TABLE ARS_ALLOC_HISTORY ADD CONSTRAINT CK_ARS_ALLOC_HISTORY_alloc_type
        CHECK (ALLOC_TYPE IS NULL OR ALLOC_TYPE IN ('','FRESH','GRT'));
GO

-- 1d) MSA tables: FRESH/GRT only (row-per-type output)
DECLARE @t sysname;
DECLARE msa_tbls CURSOR FOR
    SELECT name FROM sys.tables WHERE name IN ('ARS_MSA_TOTAL','ARS_MSA_GEN_ART','ARS_MSA_VAR_ART');
OPEN msa_tbls; FETCH NEXT FROM msa_tbls INTO @t;
WHILE @@FETCH_STATUS = 0
BEGIN
    IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_' + @t + '_alloc_type')
        EXEC('ALTER TABLE [' + @t + '] ADD CONSTRAINT [CK_' + @t + '_alloc_type] '
           + 'CHECK (ALLOC_TYPE IN (''FRESH'',''GRT''))');
    FETCH NEXT FROM msa_tbls INTO @t;
END
CLOSE msa_tbls; DEALLOCATE msa_tbls;
GO

-- 2) Session history column + backfill from REQUEST_JSON
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_NAME='ARS_LISTING_SESSIONS' AND COLUMN_NAME='ALLOC_TYPE')
    ALTER TABLE ARS_LISTING_SESSIONS ADD ALLOC_TYPE NVARCHAR(10) NULL;
GO
UPDATE ARS_LISTING_SESSIONS
SET    ALLOC_TYPE = UPPER(LTRIM(RTRIM(JSON_VALUE(REQUEST_JSON, '$.alloc_type'))))
WHERE  ALLOC_TYPE IS NULL
  AND  ISJSON(REQUEST_JSON) = 1
  AND  JSON_VALUE(REQUEST_JSON, '$.alloc_type') IN ('FRESH','GRT','fresh','grt');
GO

PRINT 'Migration 018 complete.';
