-- Migration: Add column edit permissions and data audit log
-- Date: 2026-02-23

USE Claude;
GO

-- ============================================================================
-- 1. Add can_edit column to rls_column_restrictions
-- ============================================================================
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS 
    WHERE TABLE_NAME = 'rls_column_restrictions' AND COLUMN_NAME = 'can_edit')
BEGIN
    ALTER TABLE rls_column_restrictions 
    ADD can_edit BIT DEFAULT 1;
    
    PRINT 'Added can_edit column to rls_column_restrictions';
END
GO

-- ============================================================================
-- 2. Create table for heavy/large tables configuration
-- ============================================================================
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'table_settings')
BEGIN
    CREATE TABLE table_settings (
        id INT IDENTITY(1,1) PRIMARY KEY,
        table_name NVARCHAR(200) NOT NULL UNIQUE,
        is_heavy BIT DEFAULT 0,
        row_threshold INT DEFAULT 100000,
        require_filter BIT DEFAULT 0,
        created_at DATETIME2 DEFAULT GETDATE(),
        updated_at DATETIME2 DEFAULT GETDATE()
    );
    
    PRINT 'Created table_settings table';
END
GO

-- ============================================================================
-- 3. Create data change audit log table (for tracking cell edits)
-- ============================================================================
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'data_change_log')
BEGIN
    CREATE TABLE data_change_log (
        id BIGINT IDENTITY(1,1) PRIMARY KEY,
        table_name NVARCHAR(200) NOT NULL,
        primary_key_values NVARCHAR(MAX),  -- JSON of PK column:value pairs
        column_name NVARCHAR(200) NOT NULL,
        old_value NVARCHAR(MAX),
        new_value NVARCHAR(MAX),
        change_type NVARCHAR(20) NOT NULL,  -- 'UPDATE', 'INSERT', 'DELETE'
        changed_by NVARCHAR(100) NOT NULL,
        changed_at DATETIME2 DEFAULT GETDATE(),
        ip_address NVARCHAR(50),
        user_agent NVARCHAR(500)
    );
    
    -- Index for common queries
    CREATE INDEX ix_data_change_log_table ON data_change_log(table_name);
    CREATE INDEX ix_data_change_log_changed_by ON data_change_log(changed_by);
    CREATE INDEX ix_data_change_log_changed_at ON data_change_log(changed_at DESC);
    
    PRINT 'Created data_change_log table';
END
GO

-- ============================================================================
-- 4. Add new permissions for column management
-- ============================================================================
IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'COLUMN_EDIT_MANAGE')
BEGIN
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource, description)
    VALUES 
        ('Manage Column Edit Permissions', 'COLUMN_EDIT_MANAGE', 'admin', 'manage', 'column_edit', 'Manage which columns users/roles can edit'),
        ('View Data Change Log', 'DATA_CHANGE_LOG_VIEW', 'admin', 'read', 'data_change_log', 'View data change audit log');
    
    -- Grant to SUPER_ADMIN role
    DECLARE @super_admin_id INT = (SELECT id FROM rbac_roles WHERE role_code = 'SUPER_ADMIN');
    DECLARE @perm1_id INT = (SELECT id FROM rbac_permissions WHERE permission_code = 'COLUMN_EDIT_MANAGE');
    DECLARE @perm2_id INT = (SELECT id FROM rbac_permissions WHERE permission_code = 'DATA_CHANGE_LOG_VIEW');
    
    IF @super_admin_id IS NOT NULL AND @perm1_id IS NOT NULL
        INSERT INTO rbac_role_permissions (role_id, permission_id, granted_by) VALUES (@super_admin_id, @perm1_id, 'SYSTEM');
    IF @super_admin_id IS NOT NULL AND @perm2_id IS NOT NULL
        INSERT INTO rbac_role_permissions (role_id, permission_id, granted_by) VALUES (@super_admin_id, @perm2_id, 'SYSTEM');
    
    PRINT 'Added column management permissions';
END
GO

PRINT 'Migration 004 completed successfully';
GO
