-- =============================================================================
-- Migration 015: Create SLOC Settings Table
-- Purpose: Stores KPI and Active/Inactive configuration for each distinct
--          SLOC value found in ET_STORE_STOCK table.
-- =============================================================================

IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_NAME = 'ARS_SLOC_SETTINGS'
)
BEGIN
    CREATE TABLE ARS_SLOC_SETTINGS (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        sloc        NVARCHAR(50)  NOT NULL UNIQUE,
        kpi         NVARCHAR(200) NULL,
        is_active   BIT           NOT NULL DEFAULT 1,
        created_at  DATETIME      NOT NULL DEFAULT GETDATE(),
        updated_at  DATETIME      NOT NULL DEFAULT GETDATE()
    );

    CREATE INDEX IX_ARS_SLOC_SETTINGS_sloc ON ARS_SLOC_SETTINGS(sloc);

    PRINT 'Table ARS_SLOC_SETTINGS created successfully.';
END
ELSE
BEGIN
    PRINT 'Table ARS_SLOC_SETTINGS already exists. Skipping.';
END
