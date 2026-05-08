-- Migration: Add export settings table
-- Date: 2026-02-24

USE Claude;
GO

-- ============================================================================
-- 1. Create export_settings table
-- ============================================================================
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'export_settings')
BEGIN
    CREATE TABLE export_settings (
        id INT IDENTITY(1,1) PRIMARY KEY,
        setting_key NVARCHAR(100) NOT NULL UNIQUE,
        setting_value NVARCHAR(MAX),
        description NVARCHAR(500),
        created_at DATETIME2 DEFAULT GETDATE(),
        updated_at DATETIME2 DEFAULT GETDATE()
    );
    
    -- Insert default settings
    INSERT INTO export_settings (setting_key, setting_value, description) VALUES
        ('max_rows_per_file', '100000', 'Maximum rows per export file before splitting'),
        ('split_method', 'product', 'Split method: product or store'),
        ('product_hierarchy', '["SEG", "DIV", "SUB_DIV", "MAJ_CAT"]', 'Product master split hierarchy columns'),
        ('product_gm_field', 'SEG', 'Field to check for GM value'),
        ('product_gm_value', 'GM', 'Value that indicates GM category'),
        ('store_hierarchy', '["ZONE", "REG", "STORE"]', 'Store master split hierarchy columns'),
        ('enable_auto_split', 'true', 'Automatically split large exports'),
        ('export_chunk_size', '50000', 'Chunk size for streaming large exports');
    
    PRINT 'Created export_settings table';
END
GO

PRINT 'Migration 006 completed successfully';
GO
