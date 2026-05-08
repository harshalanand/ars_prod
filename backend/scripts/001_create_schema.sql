-- ============================================================================
-- RETAIL LISTING & ALLOCATION SYSTEM - DATABASE SCHEMA
-- Target: Microsoft SQL Server (ODBC Driver 18)
-- Database: Claude
-- ============================================================================

USE Claude;
GO

-- ============================================================================
-- SECTION 1: RBAC TABLES (Role-Based Access Control)
-- ============================================================================

-- Roles Master
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'rbac_roles')
CREATE TABLE rbac_roles (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    role_name       NVARCHAR(100) NOT NULL UNIQUE,
    role_code       NVARCHAR(50) NOT NULL UNIQUE,
    description     NVARCHAR(500),
    is_system_role  BIT DEFAULT 0,           -- 1 = cannot delete
    is_active       BIT DEFAULT 1,
    created_at      DATETIME2 DEFAULT GETUTCDATE(),
    updated_at      DATETIME2 DEFAULT GETUTCDATE(),
    created_by      NVARCHAR(100)
);
GO

-- Permissions Master
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'rbac_permissions')
CREATE TABLE rbac_permissions (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    permission_name NVARCHAR(200) NOT NULL,
    permission_code NVARCHAR(100) NOT NULL UNIQUE,
    module          NVARCHAR(100) NOT NULL,   -- e.g. 'allocation', 'admin', 'reports'
    action          NVARCHAR(50) NOT NULL,    -- CREATE, READ, UPDATE, DELETE, UPLOAD, EXPORT
    resource        NVARCHAR(200),            -- specific table or resource
    description     NVARCHAR(500),
    is_active       BIT DEFAULT 1,
    created_at      DATETIME2 DEFAULT GETUTCDATE()
);
GO

-- Role-Permission Mapping
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'rbac_role_permissions')
CREATE TABLE rbac_role_permissions (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    role_id         INT NOT NULL REFERENCES rbac_roles(id),
    permission_id   INT NOT NULL REFERENCES rbac_permissions(id),
    granted_at      DATETIME2 DEFAULT GETUTCDATE(),
    granted_by      NVARCHAR(100),
    CONSTRAINT uq_role_permission UNIQUE (role_id, permission_id)
);
GO

-- Users
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'rbac_users')
CREATE TABLE rbac_users (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    username        NVARCHAR(100) NOT NULL UNIQUE,
    email           NVARCHAR(200) NOT NULL UNIQUE,
    password_hash   NVARCHAR(500) NOT NULL,
    full_name       NVARCHAR(200) NOT NULL,
    employee_code   NVARCHAR(50),
    phone           NVARCHAR(20),
    is_active       BIT DEFAULT 1,
    is_locked       BIT DEFAULT 0,
    failed_attempts INT DEFAULT 0,
    last_login      DATETIME2,
    password_changed_at DATETIME2,
    created_at      DATETIME2 DEFAULT GETUTCDATE(),
    updated_at      DATETIME2 DEFAULT GETUTCDATE(),
    created_by      NVARCHAR(100)
);
GO

-- User-Role Mapping (users can have multiple roles)
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'rbac_user_roles')
CREATE TABLE rbac_user_roles (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    user_id         INT NOT NULL REFERENCES rbac_users(id),
    role_id         INT NOT NULL REFERENCES rbac_roles(id),
    assigned_at     DATETIME2 DEFAULT GETUTCDATE(),
    assigned_by     NVARCHAR(100),
    is_active       BIT DEFAULT 1,
    CONSTRAINT uq_user_role UNIQUE (user_id, role_id)
);
GO

-- ============================================================================
-- SECTION 2: ROW-LEVEL SECURITY (RLS) TABLES
-- ============================================================================

-- Store Master
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'rls_stores')
CREATE TABLE rls_stores (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    store_code      NVARCHAR(20) NOT NULL UNIQUE,
    store_name      NVARCHAR(200) NOT NULL,
    region          NVARCHAR(100),
    hub             NVARCHAR(100),
    division        NVARCHAR(100),
    business_unit   NVARCHAR(100),
    store_grade     NVARCHAR(10),             -- A, B, C, D
    city            NVARCHAR(100),
    state           NVARCHAR(100),
    is_active       BIT DEFAULT 1,
    created_at      DATETIME2 DEFAULT GETUTCDATE(),
    updated_at      DATETIME2 DEFAULT GETUTCDATE()
);
GO

-- User-Store Access Mapping (which user can see which stores)
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'rls_user_store_access')
CREATE TABLE rls_user_store_access (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    user_id         INT NOT NULL REFERENCES rbac_users(id),
    store_code      NVARCHAR(20) NOT NULL,
    access_level    NVARCHAR(50) DEFAULT 'READ', -- READ, WRITE, FULL
    granted_at      DATETIME2 DEFAULT GETUTCDATE(),
    granted_by      NVARCHAR(100),
    is_active       BIT DEFAULT 1,
    CONSTRAINT uq_user_store UNIQUE (user_id, store_code)
);
GO

-- User-Region Access (bulk region-level access)
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'rls_user_region_access')
CREATE TABLE rls_user_region_access (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    user_id         INT NOT NULL REFERENCES rbac_users(id),
    region          NVARCHAR(100),
    hub             NVARCHAR(100),
    division        NVARCHAR(100),
    business_unit   NVARCHAR(100),
    access_level    NVARCHAR(50) DEFAULT 'READ',
    granted_at      DATETIME2 DEFAULT GETUTCDATE(),
    granted_by      NVARCHAR(100),
    is_active       BIT DEFAULT 1
);
GO

-- Column-Level Security Rules
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'rls_column_restrictions')
CREATE TABLE rls_column_restrictions (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    table_name      NVARCHAR(200) NOT NULL,
    column_name     NVARCHAR(200) NOT NULL,
    role_id         INT NOT NULL REFERENCES rbac_roles(id),
    is_visible      BIT DEFAULT 1,           -- 0 = hidden
    is_masked       BIT DEFAULT 0,           -- 1 = show masked value
    mask_pattern    NVARCHAR(100),           -- e.g., '***' or 'XXXXX'
    created_at      DATETIME2 DEFAULT GETUTCDATE(),
    CONSTRAINT uq_col_restriction UNIQUE (table_name, column_name, role_id)
);
GO

-- ============================================================================
-- SECTION 3: AUDIT LOGGING TABLE
-- ============================================================================

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'audit_log')
CREATE TABLE audit_log (
    id                  BIGINT IDENTITY(1,1) PRIMARY KEY,
    table_name          NVARCHAR(200) NOT NULL,
    action_type         NVARCHAR(50) NOT NULL,   -- INSERT, UPDATE, DELETE, UPSERT, BULK_UPLOAD, SCHEMA_CHANGE
    record_primary_key  NVARCHAR(500),
    old_data            NVARCHAR(MAX),            -- JSON
    new_data            NVARCHAR(MAX),            -- JSON
    changed_columns     NVARCHAR(MAX),            -- JSON array of column names
    changed_by          NVARCHAR(100) NOT NULL,
    changed_at          DATETIME2 DEFAULT GETUTCDATE(),
    source              NVARCHAR(50) DEFAULT 'API', -- UI, API, UPLOAD, SYSTEM
    ip_address          NVARCHAR(50),
    user_agent          NVARCHAR(500),
    session_id          NVARCHAR(200),
    batch_id            NVARCHAR(100),            -- groups bulk operations
    duration_ms         INT,
    row_count           INT DEFAULT 1,
    notes               NVARCHAR(1000)
);
GO

-- Index for audit queries
CREATE NONCLUSTERED INDEX IX_audit_log_table_action
ON audit_log (table_name, action_type, changed_at DESC);
GO

CREATE NONCLUSTERED INDEX IX_audit_log_user
ON audit_log (changed_by, changed_at DESC);
GO

CREATE NONCLUSTERED INDEX IX_audit_log_batch
ON audit_log (batch_id) WHERE batch_id IS NOT NULL;
GO

-- ============================================================================
-- SECTION 4: DYNAMIC TABLE MANAGEMENT METADATA
-- ============================================================================

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'sys_table_registry')
CREATE TABLE sys_table_registry (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    table_name      NVARCHAR(200) NOT NULL UNIQUE,
    display_name    NVARCHAR(200),
    description     NVARCHAR(1000),
    module          NVARCHAR(100),           -- allocation, planning, reports
    primary_key_columns NVARCHAR(500),       -- JSON array
    is_system_table BIT DEFAULT 0,           -- 1 = cannot delete
    is_active       BIT DEFAULT 1,           -- soft delete
    row_count       BIGINT DEFAULT 0,
    created_at      DATETIME2 DEFAULT GETUTCDATE(),
    updated_at      DATETIME2 DEFAULT GETUTCDATE(),
    created_by      NVARCHAR(100)
);
GO

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'sys_column_registry')
CREATE TABLE sys_column_registry (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    table_id        INT NOT NULL REFERENCES sys_table_registry(id),
    column_name     NVARCHAR(200) NOT NULL,
    display_name    NVARCHAR(200),
    data_type       NVARCHAR(100) NOT NULL,  -- NVARCHAR, INT, DECIMAL, DATETIME2, BIT
    max_length      INT,
    is_nullable     BIT DEFAULT 1,
    is_primary_key  BIT DEFAULT 0,
    default_value   NVARCHAR(500),
    column_order    INT DEFAULT 0,
    is_active       BIT DEFAULT 1,
    created_at      DATETIME2 DEFAULT GETUTCDATE()
);
GO

-- ============================================================================
-- SECTION 5: RETAIL / PRODUCT MASTER TABLES
-- ============================================================================

-- Division Master
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'retail_division')
CREATE TABLE retail_division (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    division_code   NVARCHAR(20) NOT NULL UNIQUE,
    division_name   NVARCHAR(200) NOT NULL,
    is_active       BIT DEFAULT 1,
    created_at      DATETIME2 DEFAULT GETUTCDATE()
);
GO

-- Sub Division
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'retail_sub_division')
CREATE TABLE retail_sub_division (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    sub_division_code NVARCHAR(20) NOT NULL UNIQUE,
    sub_division_name NVARCHAR(200) NOT NULL,
    division_id     INT REFERENCES retail_division(id),
    is_active       BIT DEFAULT 1,
    created_at      DATETIME2 DEFAULT GETUTCDATE()
);
GO

-- Major Category
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'retail_major_category')
CREATE TABLE retail_major_category (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    category_code   NVARCHAR(20) NOT NULL UNIQUE,
    category_name   NVARCHAR(200) NOT NULL,
    sub_division_id INT REFERENCES retail_sub_division(id),
    is_active       BIT DEFAULT 1,
    created_at      DATETIME2 DEFAULT GETUTCDATE()
);
GO

-- Gen Article (Parent Product)
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'retail_gen_article')
CREATE TABLE retail_gen_article (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    gen_article_code NVARCHAR(50) NOT NULL UNIQUE,
    article_name    NVARCHAR(300) NOT NULL,
    division_id     INT REFERENCES retail_division(id),
    sub_division_id INT REFERENCES retail_sub_division(id),
    category_id     INT REFERENCES retail_major_category(id),
    mvgr            NVARCHAR(100),           -- Merchandise Value Group
    fabric          NVARCHAR(200),
    season          NVARCHAR(100),
    brand           NVARCHAR(100),
    mrp             DECIMAL(12,2),
    cost_price      DECIMAL(12,2),           -- Column-level security
    margin_pct      DECIMAL(8,2),            -- Column-level security
    is_active       BIT DEFAULT 1,
    created_at      DATETIME2 DEFAULT GETUTCDATE(),
    updated_at      DATETIME2 DEFAULT GETUTCDATE()
);
GO

-- Variant Article (Size × Color variant of Gen Article)
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'retail_variant_article')
CREATE TABLE retail_variant_article (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    variant_code    NVARCHAR(50) NOT NULL UNIQUE,
    gen_article_id  INT NOT NULL REFERENCES retail_gen_article(id),
    size_code       NVARCHAR(20) NOT NULL,
    size_name       NVARCHAR(50),
    color_code      NVARCHAR(20) NOT NULL,
    color_name      NVARCHAR(100),
    barcode         NVARCHAR(50),
    mrp             DECIMAL(12,2),
    cost_price      DECIMAL(12,2),
    is_active       BIT DEFAULT 1,
    created_at      DATETIME2 DEFAULT GETUTCDATE(),
    updated_at      DATETIME2 DEFAULT GETUTCDATE()
);
GO

-- Size Master
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'retail_size_master')
CREATE TABLE retail_size_master (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    size_code       NVARCHAR(20) NOT NULL UNIQUE,
    size_name       NVARCHAR(50) NOT NULL,
    size_order      INT DEFAULT 0,           -- for display ordering
    category        NVARCHAR(50),            -- Apparel, Footwear, etc.
    is_active       BIT DEFAULT 1
);
GO

-- Color Master
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'retail_color_master')
CREATE TABLE retail_color_master (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    color_code      NVARCHAR(20) NOT NULL UNIQUE,
    color_name      NVARCHAR(100) NOT NULL,
    color_hex       NVARCHAR(10),
    is_active       BIT DEFAULT 1
);
GO

-- ============================================================================
-- SECTION 6: ALLOCATION TABLES
-- ============================================================================

-- Allocation Header (one per allocation run)
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'alloc_header')
CREATE TABLE alloc_header (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    allocation_code NVARCHAR(50) NOT NULL UNIQUE,
    allocation_name NVARCHAR(300),
    allocation_type NVARCHAR(50) NOT NULL,   -- INITIAL, REPLENISHMENT, TRANSFER
    division_id     INT REFERENCES retail_division(id),
    season          NVARCHAR(100),
    status          NVARCHAR(50) DEFAULT 'DRAFT', -- DRAFT, IN_PROGRESS, APPROVED, EXECUTED, CANCELLED
    total_qty       INT DEFAULT 0,
    total_stores    INT DEFAULT 0,
    total_options   INT DEFAULT 0,
    created_by      NVARCHAR(100) NOT NULL,
    approved_by     NVARCHAR(100),
    executed_at     DATETIME2,
    created_at      DATETIME2 DEFAULT GETUTCDATE(),
    updated_at      DATETIME2 DEFAULT GETUTCDATE()
);
GO

-- Allocation Detail (store × variant level)
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'alloc_detail')
CREATE TABLE alloc_detail (
    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
    allocation_id   INT NOT NULL REFERENCES alloc_header(id),
    store_code      NVARCHAR(20) NOT NULL,
    gen_article_id  INT REFERENCES retail_gen_article(id),
    variant_id      INT REFERENCES retail_variant_article(id),
    size_code       NVARCHAR(20),
    color_code      NVARCHAR(20),
    allocated_qty   INT DEFAULT 0,
    override_qty    INT,                     -- manual override (Column-level security)
    final_qty       INT DEFAULT 0,
    store_grade     NVARCHAR(10),
    allocation_basis NVARCHAR(50),           -- STOCK, SALES, RATIO, MANUAL
    created_at      DATETIME2 DEFAULT GETUTCDATE(),
    updated_at      DATETIME2 DEFAULT GETUTCDATE()
);
GO

-- Index for allocation queries
CREATE NONCLUSTERED INDEX IX_alloc_detail_store
ON alloc_detail (store_code, allocation_id);
GO

CREATE NONCLUSTERED INDEX IX_alloc_detail_article
ON alloc_detail (gen_article_id, variant_id);
GO

-- Store Stock (for stock-based allocation)
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'store_stock')
CREATE TABLE store_stock (
    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
    store_code      NVARCHAR(20) NOT NULL,
    variant_code    NVARCHAR(50) NOT NULL,
    stock_qty       INT DEFAULT 0,
    in_transit_qty  INT DEFAULT 0,
    reserved_qty    INT DEFAULT 0,
    available_qty   AS (stock_qty - reserved_qty), -- computed column
    last_updated    DATETIME2 DEFAULT GETUTCDATE(),
    CONSTRAINT uq_store_variant_stock UNIQUE (store_code, variant_code)
);
GO

-- Store Sales (for sales-based allocation)
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'store_sales')
CREATE TABLE store_sales (
    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
    store_code      NVARCHAR(20) NOT NULL,
    variant_code    NVARCHAR(50) NOT NULL,
    sale_date       DATE NOT NULL,
    qty_sold        INT DEFAULT 0,
    sale_value      DECIMAL(12,2) DEFAULT 0,
    CONSTRAINT uq_store_variant_sale UNIQUE (store_code, variant_code, sale_date)
);
GO

CREATE NONCLUSTERED INDEX IX_store_sales_date
ON store_sales (sale_date, store_code);
GO

-- Warehouse Stock
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'warehouse_stock')
CREATE TABLE warehouse_stock (
    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
    warehouse_code  NVARCHAR(20) NOT NULL,
    variant_code    NVARCHAR(50) NOT NULL,
    stock_qty       INT DEFAULT 0,
    reserved_qty    INT DEFAULT 0,
    available_qty   AS (stock_qty - reserved_qty),
    last_updated    DATETIME2 DEFAULT GETUTCDATE(),
    CONSTRAINT uq_wh_variant UNIQUE (warehouse_code, variant_code)
);
GO

-- ============================================================================
-- SECTION 7: SEED DATA - DEFAULT ROLES & PERMISSIONS
-- ============================================================================

-- Insert default roles
IF NOT EXISTS (SELECT 1 FROM rbac_roles WHERE role_code = 'SUPER_ADMIN')
BEGIN
    INSERT INTO rbac_roles (role_name, role_code, description, is_system_role) VALUES
    ('Super Admin', 'SUPER_ADMIN', 'Full system access with no restrictions', 1),
    ('Admin', 'ADMIN', 'System administration without user management', 1),
    ('Planner', 'PLANNER', 'Allocation planning and execution', 1),
    ('Analyst', 'ANALYST', 'Read access with reporting capabilities', 1),
    ('Viewer', 'VIEWER', 'Read-only access to assigned data', 1);
END
GO

-- Insert default permissions
IF NOT EXISTS (SELECT 1 FROM rbac_permissions WHERE permission_code = 'ADMIN_USERS_CREATE')
BEGIN
    -- Admin module
    INSERT INTO rbac_permissions (permission_name, permission_code, module, action, resource) VALUES
    ('Create Users', 'ADMIN_USERS_CREATE', 'admin', 'CREATE', 'rbac_users'),
    ('Read Users', 'ADMIN_USERS_READ', 'admin', 'READ', 'rbac_users'),
    ('Update Users', 'ADMIN_USERS_UPDATE', 'admin', 'UPDATE', 'rbac_users'),
    ('Delete Users', 'ADMIN_USERS_DELETE', 'admin', 'DELETE', 'rbac_users'),
    ('Manage Roles', 'ADMIN_ROLES_MANAGE', 'admin', 'CREATE', 'rbac_roles'),
    ('Manage Permissions', 'ADMIN_PERMS_MANAGE', 'admin', 'CREATE', 'rbac_permissions'),
    ('View Audit Logs', 'ADMIN_AUDIT_READ', 'admin', 'READ', 'audit_log'),
    ('Manage RLS', 'ADMIN_RLS_MANAGE', 'admin', 'CREATE', 'rls_config'),
    -- Table management
    ('Create Tables', 'TABLE_CREATE', 'tables', 'CREATE', 'sys_table_registry'),
    ('Alter Tables', 'TABLE_ALTER', 'tables', 'UPDATE', 'sys_table_registry'),
    ('Delete Tables', 'TABLE_DELETE', 'tables', 'DELETE', 'sys_table_registry'),
    ('View Tables', 'TABLE_READ', 'tables', 'READ', 'sys_table_registry'),
    -- Data operations
    ('Upload Data', 'DATA_UPLOAD', 'data', 'UPLOAD', '*'),
    ('Export Data', 'DATA_EXPORT', 'data', 'EXPORT', '*'),
    ('Edit Data', 'DATA_EDIT', 'data', 'UPDATE', '*'),
    -- Allocation module
    ('Create Allocation', 'ALLOC_CREATE', 'allocation', 'CREATE', 'alloc_header'),
    ('Read Allocation', 'ALLOC_READ', 'allocation', 'READ', 'alloc_header'),
    ('Update Allocation', 'ALLOC_UPDATE', 'allocation', 'UPDATE', 'alloc_header'),
    ('Delete Allocation', 'ALLOC_DELETE', 'allocation', 'DELETE', 'alloc_header'),
    ('Approve Allocation', 'ALLOC_APPROVE', 'allocation', 'UPDATE', 'alloc_approval'),
    ('Execute Allocation', 'ALLOC_EXECUTE', 'allocation', 'CREATE', 'alloc_execution'),
    -- Product module
    ('Manage Products', 'PRODUCT_MANAGE', 'product', 'CREATE', 'retail_gen_article'),
    ('Read Products', 'PRODUCT_READ', 'product', 'READ', 'retail_gen_article'),
    -- Reports
    ('View Reports', 'REPORT_VIEW', 'reports', 'READ', '*'),
    ('Export Reports', 'REPORT_EXPORT', 'reports', 'EXPORT', '*');
END
GO

-- Assign all permissions to Super Admin
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

-- Assign Planner permissions
INSERT INTO rbac_role_permissions (role_id, permission_id, granted_by)
SELECT r.id, p.id, 'SYSTEM'
FROM rbac_roles r
CROSS JOIN rbac_permissions p
WHERE r.role_code = 'PLANNER'
AND p.permission_code IN (
    'ALLOC_CREATE','ALLOC_READ','ALLOC_UPDATE','ALLOC_APPROVE',
    'PRODUCT_READ','DATA_UPLOAD','DATA_EDIT','DATA_EXPORT',
    'TABLE_READ','REPORT_VIEW','REPORT_EXPORT'
)
AND NOT EXISTS (
    SELECT 1 FROM rbac_role_permissions rp
    WHERE rp.role_id = r.id AND rp.permission_id = p.id
);
GO

-- Assign Analyst permissions
INSERT INTO rbac_role_permissions (role_id, permission_id, granted_by)
SELECT r.id, p.id, 'SYSTEM'
FROM rbac_roles r
CROSS JOIN rbac_permissions p
WHERE r.role_code = 'ANALYST'
AND p.permission_code IN (
    'ALLOC_READ','PRODUCT_READ','DATA_EXPORT',
    'TABLE_READ','REPORT_VIEW','REPORT_EXPORT'
)
AND NOT EXISTS (
    SELECT 1 FROM rbac_role_permissions rp
    WHERE rp.role_id = r.id AND rp.permission_id = p.id
);
GO

-- Assign Viewer permissions
INSERT INTO rbac_role_permissions (role_id, permission_id, granted_by)
SELECT r.id, p.id, 'SYSTEM'
FROM rbac_roles r
CROSS JOIN rbac_permissions p
WHERE r.role_code = 'VIEWER'
AND p.permission_code IN ('ALLOC_READ','PRODUCT_READ','TABLE_READ','REPORT_VIEW')
AND NOT EXISTS (
    SELECT 1 FROM rbac_role_permissions rp
    WHERE rp.role_id = r.id AND rp.permission_id = p.id
);
GO

-- Column-level security defaults (hide sensitive columns from non-admin roles)
INSERT INTO rls_column_restrictions (table_name, column_name, role_id, is_visible, is_masked) VALUES
('retail_gen_article', 'cost_price', (SELECT id FROM rbac_roles WHERE role_code = 'VIEWER'), 0, 0),
('retail_gen_article', 'margin_pct', (SELECT id FROM rbac_roles WHERE role_code = 'VIEWER'), 0, 0),
('retail_gen_article', 'cost_price', (SELECT id FROM rbac_roles WHERE role_code = 'ANALYST'), 1, 1),
('retail_gen_article', 'margin_pct', (SELECT id FROM rbac_roles WHERE role_code = 'ANALYST'), 1, 1),
('alloc_detail', 'override_qty', (SELECT id FROM rbac_roles WHERE role_code = 'VIEWER'), 0, 0),
('alloc_detail', 'override_qty', (SELECT id FROM rbac_roles WHERE role_code = 'ANALYST'), 0, 0);
GO

PRINT 'Schema creation completed successfully.';
GO
