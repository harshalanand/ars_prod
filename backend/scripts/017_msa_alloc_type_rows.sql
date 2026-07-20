-- =============================================================================
-- Migration 017: Row-per-type MSA — ALLOC_TYPE on MSA output tables
-- Spec: row-based MSA (user decision 2026-07-09). One row per
--   (RDC × GEN_ART × CLR × SZ × ALLOC_TYPE). ALLOC_TYPE ∈ {FRESH, GRT}.
--   Legacy/untyped pend/hold fold into the FRESH row.
--
-- MSA tables are re-created/refreshed by MSAResultStorageService each run, so
-- the authoritative schema lives there; this migration just makes existing
-- deployed tables consistent so downstream queries don't break before the
-- next MSA generation. Existing rows default to FRESH.
-- =============================================================================

DECLARE @t NVARCHAR(128);
DECLARE tbls CURSOR FOR
    SELECT name FROM sys.tables
    WHERE name IN ('ARS_MSA_TOTAL','ARS_MSA_GEN_ART','ARS_MSA_VAR_ART');
OPEN tbls;
FETCH NEXT FROM tbls INTO @t;
WHILE @@FETCH_STATUS = 0
BEGIN
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME = @t AND COLUMN_NAME = 'ALLOC_TYPE')
    BEGIN
        EXEC('ALTER TABLE [' + @t + '] ADD [ALLOC_TYPE] NVARCHAR(10) NOT NULL '
           + 'CONSTRAINT [DF_' + @t + '_alloc_type] DEFAULT ''FRESH''');
        PRINT 'Added ALLOC_TYPE to ' + @t;
    END
    FETCH NEXT FROM tbls INTO @t;
END
CLOSE tbls;
DEALLOCATE tbls;

PRINT 'Migration 017 complete.';
