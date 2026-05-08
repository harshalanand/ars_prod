-- ============================================================================
-- 017: Add module-level permissions for all ARS features
-- Run once. Safe to re-run (idempotent).
-- ============================================================================

-- ── New Permission Codes ────────────────────────────────────────────────────

-- Data Management
IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'DATA_VIEW')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('View Data Tables', 'DATA_VIEW', 'data', 'READ', '*');

IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'DATA_EDITOR')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('Use Data Editor', 'DATA_EDITOR', 'data', 'UPDATE', '*');

IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'JOBS_VIEW')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('View Jobs Dashboard', 'JOBS_VIEW', 'data', 'READ', 'jobs');

-- Data Preparation: MSA
IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'MSA_VIEW')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('View MSA Stock', 'MSA_VIEW', 'data_prep', 'READ', 'msa');

IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'MSA_EXECUTE')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('Execute MSA Calculation', 'MSA_EXECUTE', 'data_prep', 'CREATE', 'msa');

-- Data Preparation: BDC
IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'BDC_VIEW')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('View BDC Creation', 'BDC_VIEW', 'data_prep', 'READ', 'bdc');

IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'BDC_EXECUTE')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('Execute BDC Creation', 'BDC_EXECUTE', 'data_prep', 'CREATE', 'bdc');

-- Data Preparation: Grid Builder
IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'GRID_VIEW')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('View Grid Builder', 'GRID_VIEW', 'data_prep', 'READ', 'grid_builder');

IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'GRID_RUN')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('Run Grid Builder', 'GRID_RUN', 'data_prep', 'CREATE', 'grid_builder');

IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'GRID_MANAGE')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('Manage Grid Builder', 'GRID_MANAGE', 'data_prep', 'UPDATE', 'grid_builder');

-- Data Preparation: Lookup Art Master
IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'LOOKUP_VIEW')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('View Lookup Art Master', 'LOOKUP_VIEW', 'data_prep', 'READ', 'lookup');

-- Contribution
IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'CONTRIB_PRESETS')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('Manage Contrib Presets', 'CONTRIB_PRESETS', 'contribution', 'UPDATE', 'contrib_presets');

IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'CONTRIB_MAPPINGS')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('Manage Contrib Mappings', 'CONTRIB_MAPPINGS', 'contribution', 'UPDATE', 'contrib_mappings');

IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'CONTRIB_EXECUTE')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('Execute Contribution', 'CONTRIB_EXECUTE', 'contribution', 'CREATE', 'contrib_execute');

IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'CONTRIB_REVIEW')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('Review Contribution', 'CONTRIB_REVIEW', 'contribution', 'READ', 'contrib_review');

-- Trends
IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'TRENDS_DASHBOARD')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('View Trends Dashboard', 'TRENDS_DASHBOARD', 'trends', 'READ', 'trends');

IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'TRENDS_UPLOAD')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('Upload Trend Data', 'TRENDS_UPLOAD', 'trends', 'CREATE', 'trends');

IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'TRENDS_REVIEW')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('Review Trend Data', 'TRENDS_REVIEW', 'trends', 'READ', 'trends');

-- Reports
IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'REPORTS_PEND_ALC')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('View Pending Allocation Report', 'REPORTS_PEND_ALC', 'reports', 'READ', 'reports');

-- Data Validation
IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'CHECKLIST_VIEW')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('View Data Checklist', 'CHECKLIST_VIEW', 'validation', 'READ', 'checklist');

IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'CHECKLIST_MANAGE')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('Manage Data Checklist', 'CHECKLIST_MANAGE', 'validation', 'UPDATE', 'checklist');

IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'STORE_SLOC_VIEW')
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('View Store SLOC Validation', 'STORE_SLOC_VIEW', 'validation', 'READ', 'store_sloc');

-- ── Assign ALL new permissions to Super Admin ───────────────────────────────
INSERT INTO rbac_role_permissions (role_id, permission_id, granted_by)
SELECT r.id, p.id, 'SYSTEM'
FROM rbac_roles r
CROSS JOIN rbac_permissions p
WHERE r.role_code = 'SUPER_ADMIN'
AND NOT EXISTS (
    SELECT 1 FROM rbac_role_permissions rp
    WHERE rp.role_id = r.id AND rp.permission_id = p.id
);
GO
