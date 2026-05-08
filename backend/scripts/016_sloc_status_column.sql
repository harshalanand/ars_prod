-- =============================================================================
-- Migration 016: Rename is_active -> status in ARS_SLOC_SETTINGS
-- Changes column from BIT (0/1) to NVARCHAR(20) storing 'Active'/'Inactive'
-- =============================================================================

IF EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_NAME = 'ARS_SLOC_SETTINGS'
)
BEGIN
    -- Add new status column if it doesn't exist
    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'ARS_SLOC_SETTINGS' AND COLUMN_NAME = 'status'
    )
    BEGIN
        ALTER TABLE ARS_SLOC_SETTINGS
        ADD status NVARCHAR(20) NOT NULL DEFAULT 'Active';

        -- Migrate existing is_active data into status
        IF EXISTS (
            SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'ARS_SLOC_SETTINGS' AND COLUMN_NAME = 'is_active'
        )
        BEGIN
            UPDATE ARS_SLOC_SETTINGS
            SET status = CASE WHEN is_active = 1 THEN 'Active' ELSE 'Inactive' END;

            -- Drop old column
            ALTER TABLE ARS_SLOC_SETTINGS DROP COLUMN is_active;
        END

        PRINT 'Column is_active renamed to status (Active/Inactive) successfully.';
    END
    ELSE
    BEGIN
        PRINT 'Column status already exists. Skipping.';
    END
END
ELSE
BEGIN
    PRINT 'Table ARS_SLOC_SETTINGS does not exist. Run 015 migration first.';
END
