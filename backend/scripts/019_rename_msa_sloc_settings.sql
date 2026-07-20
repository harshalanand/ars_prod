-- =============================================================================
-- Migration 019: Rename ARS_SLOC_SETTINGS -> ARS_MSA_SLOC_SETTINGS
--
-- Why: "ARS_SLOC_SETTINGS" is ALSO the legacy name of the STORE-sloc table
-- (sloc_validation.py migrates OLD_TABLE="ARS_SLOC_SETTINGS" into
-- ARS_STORE_SLOC_SETTINGS). The warehouse/MSA pool-classification table
-- created by migration 015 collides with that name on deployments where the
-- legacy store table still exists. The MSA table gets an unambiguous name.
--
-- Semantics unchanged (see 015_create_sloc_settings.sql):
--   sloc_type FRESH | GRT — mutually exclusive warehouse pools; a SLOC absent
--   from this table is treated as FRESH (with a logged warning).
--
-- Also adds audit columns for the new management UI on the MSA page:
--   updated_by      NVARCHAR(100) NULL — user who last saved the row
--   type_changed_at DATETIME      NULL — when sloc_type last actually changed
-- =============================================================================

IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_NAME = 'ARS_MSA_SLOC_SETTINGS'
)
BEGIN
    IF EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_NAME = 'ARS_SLOC_SETTINGS'
    )
    AND EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'ARS_SLOC_SETTINGS' AND COLUMN_NAME = 'sloc_type'
    )
    BEGIN
        -- The existing table IS the warehouse-pool table (015 shape): rename it.
        -- (A legacy STORE-sloc table of the same name has no sloc_type column
        -- and must NOT be touched — the fresh-create branch below handles that.)
        EXEC sp_rename 'dbo.ARS_SLOC_SETTINGS', 'ARS_MSA_SLOC_SETTINGS';
        PRINT 'Renamed table ARS_SLOC_SETTINGS -> ARS_MSA_SLOC_SETTINGS.';

        -- Defensively rename the explicitly-named constraints/index so nothing
        -- keeps the legacy prefix. System-named PK/UQ/DF constraints are left
        -- alone (their names are random anyway).
        IF EXISTS (
            SELECT 1 FROM sys.check_constraints
            WHERE name = 'CK_ARS_SLOC_SETTINGS_type'
              AND parent_object_id = OBJECT_ID('dbo.ARS_MSA_SLOC_SETTINGS')
        )
            EXEC sp_rename 'dbo.CK_ARS_SLOC_SETTINGS_type',
                           'CK_ARS_MSA_SLOC_SETTINGS_type', 'OBJECT';

        IF EXISTS (
            SELECT 1 FROM sys.default_constraints
            WHERE name = 'DF_ARS_SLOC_SETTINGS_type'
              AND parent_object_id = OBJECT_ID('dbo.ARS_MSA_SLOC_SETTINGS')
        )
            EXEC sp_rename 'dbo.DF_ARS_SLOC_SETTINGS_type',
                           'DF_ARS_MSA_SLOC_SETTINGS_type', 'OBJECT';

        IF EXISTS (
            SELECT 1 FROM sys.indexes
            WHERE name = 'IX_ARS_SLOC_SETTINGS_sloc'
              AND object_id = OBJECT_ID('dbo.ARS_MSA_SLOC_SETTINGS')
        )
            EXEC sp_rename 'dbo.ARS_MSA_SLOC_SETTINGS.IX_ARS_SLOC_SETTINGS_sloc',
                           'IX_ARS_MSA_SLOC_SETTINGS_sloc', 'INDEX';
    END
    ELSE
    BEGIN
        -- No warehouse-pool table to rename: create fresh (same DDL as 015,
        -- new names). Seeding happens in the shared block below.
        CREATE TABLE ARS_MSA_SLOC_SETTINGS (
            id          INT IDENTITY(1,1) PRIMARY KEY,
            sloc        NVARCHAR(50)  NOT NULL UNIQUE,
            kpi         NVARCHAR(200) NULL,
            sloc_type   NVARCHAR(10)  NOT NULL
                        CONSTRAINT DF_ARS_MSA_SLOC_SETTINGS_type DEFAULT 'FRESH'
                        CONSTRAINT CK_ARS_MSA_SLOC_SETTINGS_type
                        CHECK (sloc_type IN ('FRESH','GRT')),
            is_active   BIT           NOT NULL DEFAULT 1,
            created_at  DATETIME      NOT NULL DEFAULT GETDATE(),
            updated_at  DATETIME      NOT NULL DEFAULT GETDATE()
        );

        CREATE INDEX IX_ARS_MSA_SLOC_SETTINGS_sloc ON ARS_MSA_SLOC_SETTINGS(sloc);

        PRINT 'Table ARS_MSA_SLOC_SETTINGS created fresh.';
    END
END
ELSE
    PRINT 'Table ARS_MSA_SLOC_SETTINGS already exists. Skipping rename/create.';
GO

-- Audit columns (idempotent; applies to both the renamed and fresh table).
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'ARS_MSA_SLOC_SETTINGS' AND COLUMN_NAME = 'updated_by'
)
BEGIN
    ALTER TABLE ARS_MSA_SLOC_SETTINGS ADD updated_by NVARCHAR(100) NULL;
    PRINT 'Column updated_by added.';
END

IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'ARS_MSA_SLOC_SETTINGS' AND COLUMN_NAME = 'type_changed_at'
)
BEGIN
    ALTER TABLE ARS_MSA_SLOC_SETTINGS ADD type_changed_at DATETIME NULL;
    PRINT 'Column type_changed_at added.';
END
GO

-- Seed any live SLOC missing from the table (source: MSA stock view).
-- V02_GRT is the growth bin; everything else defaults to FRESH. Idempotent —
-- the fresh-create branch relies on this entirely, the rename branch usually
-- inserts nothing.
IF EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.VIEWS
    WHERE TABLE_NAME = 'VW_ET_MSA_STK_WITH_MASTER'
)
BEGIN
    INSERT INTO ARS_MSA_SLOC_SETTINGS (sloc, sloc_type)
    SELECT s.SLOC,
           CASE WHEN s.SLOC = 'V02_GRT' THEN 'GRT' ELSE 'FRESH' END
    FROM (SELECT DISTINCT SLOC FROM VW_ET_MSA_STK_WITH_MASTER WHERE SLOC IS NOT NULL) s
    WHERE NOT EXISTS (
        SELECT 1 FROM ARS_MSA_SLOC_SETTINGS t WHERE t.sloc = s.SLOC
    );
    PRINT 'ARS_MSA_SLOC_SETTINGS seeded from VW_ET_MSA_STK_WITH_MASTER.';
END
ELSE
    PRINT 'VW_ET_MSA_STK_WITH_MASTER not found - seeding skipped.';
GO
