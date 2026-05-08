-- Migration: Create data_change_log table for row-level audit logging
-- Supports detailed tracking of individual row/column changes

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'data_change_log')
BEGIN
    CREATE TABLE data_change_log (
        id BIGINT IDENTITY(1,1) PRIMARY KEY,
        audit_log_id BIGINT NULL,
        table_name NVARCHAR(200) NOT NULL,
        action_type NVARCHAR(20) NOT NULL,  -- INSERT, UPDATE, DELETE
        record_key NVARCHAR(500) NOT NULL,  -- Primary key as JSON
        column_name NVARCHAR(200) NULL,     -- Column changed (for UPDATE)
        old_value NVARCHAR(MAX) NULL,
        new_value NVARCHAR(MAX) NULL,
        data_type NVARCHAR(50) NULL,
        changed_by NVARCHAR(100) NOT NULL,
        changed_at DATETIME DEFAULT GETUTCDATE(),
        source NVARCHAR(50) DEFAULT 'UI',
        batch_id NVARCHAR(100) NULL,
        row_index INT NULL
    );
    
    -- Indexes for common queries
    CREATE INDEX IX_data_change_log_table_name ON data_change_log(table_name);
    CREATE INDEX IX_data_change_log_changed_at ON data_change_log(changed_at DESC);
    CREATE INDEX IX_data_change_log_changed_by ON data_change_log(changed_by);
    CREATE INDEX IX_data_change_log_batch_id ON data_change_log(batch_id) WHERE batch_id IS NOT NULL;
    CREATE INDEX IX_data_change_log_audit_log_id ON data_change_log(audit_log_id) WHERE audit_log_id IS NOT NULL;
    
    PRINT 'Created data_change_log table';
END
ELSE
BEGIN
    PRINT 'data_change_log table already exists';
END
GO
