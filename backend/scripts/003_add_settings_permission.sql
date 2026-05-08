-- Migration: Add ADMIN_SETTINGS permission
-- Run this script to add the settings management permission

IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'ADMIN_SETTINGS')
BEGIN
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) 
    VALUES ('Manage Settings', 'ADMIN_SETTINGS', 'admin', 'UPDATE', 'settings');
    
    -- Assign to Super Admin
    INSERT INTO rbac_role_permissions (role_id, permission_id, granted_by)
    SELECT r.id, p.id, 'SYSTEM'
    FROM rbac_roles r
    CROSS JOIN rbac_permissions p
    WHERE r.role_code = 'SUPER_ADMIN'
    AND p.permission_code = 'ADMIN_SETTINGS'
    AND NOT EXISTS (
        SELECT 1 FROM rbac_role_permissions rp
        WHERE rp.role_id = r.id AND rp.permission_id = p.id
    );
    
    PRINT 'ADMIN_SETTINGS permission added';
END
ELSE
BEGIN
    PRINT 'ADMIN_SETTINGS permission already exists';
END
GO
