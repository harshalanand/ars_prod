-- Migration: Add Data Editor visibility and filter settings
-- Date: 2026-02-23

USE Claude;
GO

-- ============================================================================
-- 1. Add visible_in_editor column to table_settings
-- ============================================================================
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS 
    WHERE TABLE_NAME = 'table_settings' AND COLUMN_NAME = 'visible_in_editor')
BEGIN
    ALTER TABLE table_settings 
    ADD visible_in_editor BIT DEFAULT 1;
    
    PRINT 'Added visible_in_editor column to table_settings';
END
GO

-- ============================================================================
-- 2. Add filter_columns column for storing default filter columns as JSON
-- ============================================================================
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS 
    WHERE TABLE_NAME = 'table_settings' AND COLUMN_NAME = 'filter_columns')
BEGIN
    ALTER TABLE table_settings 
    ADD filter_columns NVARCHAR(2000) NULL;  -- JSON array of column names
    
    PRINT 'Added filter_columns column to table_settings';
END
GO

-- ============================================================================
-- 3. Add index for visibility queries
-- ============================================================================
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_table_settings_visible')
BEGIN
    CREATE INDEX ix_table_settings_visible ON table_settings(visible_in_editor);
    
    PRINT 'Added index ix_table_settings_visible';
END
GO

PRINT 'Migration 005 completed successfully';
GO
