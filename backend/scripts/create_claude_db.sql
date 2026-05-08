-- ============================================================================
-- CREATE DATABASE: Claude (System/Administrative)
-- MDF/LDF on E: drive
-- ============================================================================
USE [master]
GO

IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = 'Claude')
BEGIN
    CREATE DATABASE [Claude]
    ON PRIMARY (
        NAME       = N'Claude',
        FILENAME   = N'E:\MSSQL_DATA\Claude.mdf',
        SIZE       = 256MB,
        MAXSIZE    = 2GB,
        FILEGROWTH = 64MB
    )
    LOG ON (
        NAME       = N'Claude_log',
        FILENAME   = N'E:\MSSQL_DATA\Claude_log.ldf',
        SIZE       = 128MB,
        MAXSIZE    = 1GB,
        FILEGROWTH = 64MB
    )
    PRINT '>> Created database: Claude'
END
ELSE
    PRINT '>> Database Claude already exists'
GO

USE [Claude]
GO

-- ============================================================================
-- 1. RBAC TABLES
-- ============================================================================

-- 1.1 rbac_roles
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.rbac_roles') AND type = 'U')
CREATE TABLE dbo.rbac_roles (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    role_name       NVARCHAR(100) NOT NULL UNIQUE,
    role_code       NVARCHAR(50)  NOT NULL UNIQUE,
    description     NVARCHAR(500),
    is_system_role  BIT NOT NULL DEFAULT 0,
    is_active       BIT NOT NULL DEFAULT 1,
    created_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    created_by      NVARCHAR(100)
)
GO

-- 1.2 rbac_permissions
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.rbac_permissions') AND type = 'U')
CREATE TABLE dbo.rbac_permissions (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    permission_name NVARCHAR(200) NOT NULL,
    permission_code NVARCHAR(100) NOT NULL UNIQUE,
    module          NVARCHAR(100),
    action          NVARCHAR(50),
    resource        NVARCHAR(200),
    description     NVARCHAR(500),
    is_active       BIT NOT NULL DEFAULT 1,
    created_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
)
GO

-- 1.3 rbac_role_permissions
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.rbac_role_permissions') AND type = 'U')
CREATE TABLE dbo.rbac_role_permissions (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    role_id         INT NOT NULL REFERENCES dbo.rbac_roles(id),
    permission_id   INT NOT NULL REFERENCES dbo.rbac_permissions(id),
    granted_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    granted_by      NVARCHAR(100),
    CONSTRAINT UQ_role_permission UNIQUE (role_id, permission_id)
)
GO

-- 1.4 rbac_users
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.rbac_users') AND type = 'U')
CREATE TABLE dbo.rbac_users (
    id                  INT IDENTITY(1,1) PRIMARY KEY,
    username            NVARCHAR(100) NOT NULL UNIQUE,
    email               NVARCHAR(200) NOT NULL UNIQUE,
    mobile_no           NVARCHAR(15)  UNIQUE,
    password_hash       NVARCHAR(500) NOT NULL,
    full_name           NVARCHAR(200),
    employee_code       NVARCHAR(50),
    phone               NVARCHAR(20),
    is_active           BIT NOT NULL DEFAULT 1,
    is_locked           BIT NOT NULL DEFAULT 0,
    failed_attempts     INT NOT NULL DEFAULT 0,
    last_login          DATETIME2,
    password_changed_at DATETIME2,
    created_at          DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at          DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    created_by          NVARCHAR(100)
)
GO
CREATE NONCLUSTERED INDEX IX_rbac_users_username ON dbo.rbac_users(username)
CREATE NONCLUSTERED INDEX IX_rbac_users_email    ON dbo.rbac_users(email)
GO

-- 1.5 rbac_user_roles
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.rbac_user_roles') AND type = 'U')
CREATE TABLE dbo.rbac_user_roles (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES dbo.rbac_users(id),
    role_id     INT NOT NULL REFERENCES dbo.rbac_roles(id),
    assigned_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    assigned_by NVARCHAR(100),
    is_active   BIT NOT NULL DEFAULT 1,
    CONSTRAINT UQ_user_role UNIQUE (user_id, role_id)
)
GO

-- ============================================================================
-- 2. RLS TABLES
-- ============================================================================

-- 2.1 rls_stores
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.rls_stores') AND type = 'U')
CREATE TABLE dbo.rls_stores (
    id            INT IDENTITY(1,1) PRIMARY KEY,
    store_code    NVARCHAR(20) NOT NULL UNIQUE,
    store_name    NVARCHAR(200),
    region        NVARCHAR(100),
    hub           NVARCHAR(100),
    division      NVARCHAR(100),
    business_unit NVARCHAR(100),
    store_grade   NVARCHAR(10),
    city          NVARCHAR(100),
    state         NVARCHAR(100),
    is_active     BIT NOT NULL DEFAULT 1,
    created_at    DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at    DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
)
GO
CREATE NONCLUSTERED INDEX IX_rls_stores_code     ON dbo.rls_stores(store_code)
CREATE NONCLUSTERED INDEX IX_rls_stores_region   ON dbo.rls_stores(region)
CREATE NONCLUSTERED INDEX IX_rls_stores_division ON dbo.rls_stores(division)
GO

-- 2.2 rls_user_store_access
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.rls_user_store_access') AND type = 'U')
CREATE TABLE dbo.rls_user_store_access (
    id           INT IDENTITY(1,1) PRIMARY KEY,
    user_id      INT NOT NULL REFERENCES dbo.rbac_users(id),
    store_code   NVARCHAR(20) NOT NULL,
    access_level NVARCHAR(50) NOT NULL DEFAULT 'READ',
    granted_at   DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    granted_by   NVARCHAR(100),
    is_active    BIT NOT NULL DEFAULT 1,
    CONSTRAINT UQ_user_store UNIQUE (user_id, store_code)
)
GO

-- 2.3 rls_user_region_access
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.rls_user_region_access') AND type = 'U')
CREATE TABLE dbo.rls_user_region_access (
    id            INT IDENTITY(1,1) PRIMARY KEY,
    user_id       INT NOT NULL REFERENCES dbo.rbac_users(id),
    region        NVARCHAR(100),
    hub           NVARCHAR(100),
    division      NVARCHAR(100),
    business_unit NVARCHAR(100),
    access_level  NVARCHAR(50) NOT NULL DEFAULT 'READ',
    granted_at    DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    granted_by    NVARCHAR(100),
    is_active     BIT NOT NULL DEFAULT 1
)
GO

-- 2.4 rls_column_restrictions
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.rls_column_restrictions') AND type = 'U')
CREATE TABLE dbo.rls_column_restrictions (
    id           INT IDENTITY(1,1) PRIMARY KEY,
    table_name   NVARCHAR(200) NOT NULL,
    column_name  NVARCHAR(200) NOT NULL,
    role_id      INT NOT NULL REFERENCES dbo.rbac_roles(id),
    is_visible   BIT NOT NULL DEFAULT 1,
    is_masked    BIT NOT NULL DEFAULT 0,
    mask_pattern NVARCHAR(100),
    can_edit     BIT,
    created_at   DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT UQ_col_restriction UNIQUE (table_name, column_name, role_id)
)
GO

-- 2.5 rls_table_role_access
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.rls_table_role_access') AND type = 'U')
CREATE TABLE dbo.rls_table_role_access (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    table_name  NVARCHAR(200) NOT NULL,
    role_id     INT NOT NULL REFERENCES dbo.rbac_roles(id),
    can_read    BIT NOT NULL DEFAULT 1,
    can_write   BIT NOT NULL DEFAULT 0,
    can_upload  BIT NOT NULL DEFAULT 0,
    can_export  BIT NOT NULL DEFAULT 0,
    granted_at  DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    granted_by  NVARCHAR(100),
    CONSTRAINT UQ_table_role UNIQUE (table_name, role_id)
)
GO

-- 2.6 table_settings
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.table_settings') AND type = 'U')
CREATE TABLE dbo.table_settings (
    id                INT IDENTITY(1,1) PRIMARY KEY,
    table_name        NVARCHAR(200) NOT NULL UNIQUE,
    is_heavy          BIT NOT NULL DEFAULT 0,
    row_threshold     INT NOT NULL DEFAULT 100000,
    require_filter    BIT NOT NULL DEFAULT 0,
    visible_in_editor BIT NOT NULL DEFAULT 1,
    filter_columns    NVARCHAR(2000),
    created_at        DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at        DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
)
GO

-- ============================================================================
-- 3. AUDIT TABLES
-- ============================================================================

-- 3.1 audit_log
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.audit_log') AND type = 'U')
CREATE TABLE dbo.audit_log (
    id                 BIGINT IDENTITY(1,1) PRIMARY KEY,
    table_name         NVARCHAR(200),
    action_type        NVARCHAR(50),
    record_primary_key NVARCHAR(500),
    old_data           NVARCHAR(MAX),
    new_data           NVARCHAR(MAX),
    changed_columns    NVARCHAR(MAX),
    changed_by         NVARCHAR(100),
    changed_at         DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    source             NVARCHAR(50)  DEFAULT 'API',
    ip_address         NVARCHAR(50),
    user_agent         NVARCHAR(500),
    session_id         NVARCHAR(200),
    batch_id           NVARCHAR(100),
    duration_ms        INT,
    row_count          INT DEFAULT 1,
    notes              NVARCHAR(1000)
)
GO
CREATE NONCLUSTERED INDEX IX_audit_log_table   ON dbo.audit_log(table_name, action_type, changed_at DESC)
CREATE NONCLUSTERED INDEX IX_audit_log_user    ON dbo.audit_log(changed_by, changed_at DESC)
CREATE NONCLUSTERED INDEX IX_audit_log_batch   ON dbo.audit_log(batch_id) WHERE batch_id IS NOT NULL
GO

-- 3.2 data_change_log
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.data_change_log') AND type = 'U')
CREATE TABLE dbo.data_change_log (
    id           BIGINT IDENTITY(1,1) PRIMARY KEY,
    audit_log_id BIGINT,
    table_name   NVARCHAR(200),
    action_type  NVARCHAR(20),
    record_key   NVARCHAR(500),
    column_name  NVARCHAR(200),
    old_value    NVARCHAR(MAX),
    new_value    NVARCHAR(MAX),
    data_type    NVARCHAR(50),
    changed_by   NVARCHAR(100),
    changed_at   DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    source       NVARCHAR(50) DEFAULT 'UI',
    batch_id     NVARCHAR(100),
    row_index    INT
)
GO
CREATE NONCLUSTERED INDEX IX_dcl_table      ON dbo.data_change_log(table_name)
CREATE NONCLUSTERED INDEX IX_dcl_changed_at ON dbo.data_change_log(changed_at DESC)
CREATE NONCLUSTERED INDEX IX_dcl_changed_by ON dbo.data_change_log(changed_by)
CREATE NONCLUSTERED INDEX IX_dcl_batch      ON dbo.data_change_log(batch_id)
CREATE NONCLUSTERED INDEX IX_dcl_audit      ON dbo.data_change_log(audit_log_id)
GO

-- ============================================================================
-- 4. JOB TRACKING TABLES
-- ============================================================================

-- 4.1 export_settings
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.export_settings') AND type = 'U')
CREATE TABLE dbo.export_settings (
    id            INT IDENTITY(1,1) PRIMARY KEY,
    setting_key   NVARCHAR(100) NOT NULL UNIQUE,
    setting_value NVARCHAR(MAX),
    description   NVARCHAR(500),
    created_at    DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at    DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
)
GO

-- 4.2 export_jobs
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.export_jobs') AND type = 'U')
CREATE TABLE dbo.export_jobs (
    id             BIGINT IDENTITY(1,1) PRIMARY KEY,
    job_id         NVARCHAR(50) NOT NULL UNIQUE,
    table_name     NVARCHAR(200),
    status         NVARCHAR(20) NOT NULL DEFAULT 'pending',
    format         NVARCHAR(10) DEFAULT 'xlsx',
    columns        NVARCHAR(MAX),
    filters        NVARCHAR(MAX),
    total_rows     INT,
    processed_rows INT DEFAULT 0,
    file_path      NVARCHAR(500),
    file_size      BIGINT,
    error_message  NVARCHAR(MAX),
    created_by     NVARCHAR(100),
    created_at     DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    started_at     DATETIME2,
    completed_at   DATETIME2,
    downloaded     INT DEFAULT 0
)
GO
CREATE NONCLUSTERED INDEX IX_export_jobs_status ON dbo.export_jobs(status)
CREATE NONCLUSTERED INDEX IX_export_jobs_date   ON dbo.export_jobs(created_at DESC)
GO

-- 4.3 upload_jobs
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.upload_jobs') AND type = 'U')
CREATE TABLE dbo.upload_jobs (
    id                      BIGINT IDENTITY(1,1) PRIMARY KEY,
    job_id                  NVARCHAR(50) NOT NULL UNIQUE,
    table_name              NVARCHAR(200),
    file_name               NVARCHAR(500),
    file_path               NVARCHAR(500),
    file_size               BIGINT,
    status                  NVARCHAR(20) NOT NULL DEFAULT 'pending',
    primary_key_columns     NVARCHAR(500),
    mode                    NVARCHAR(20) DEFAULT 'upsert',
    total_rows              INT,
    processed_rows          INT DEFAULT 0,
    inserted_rows           INT DEFAULT 0,
    updated_rows            INT DEFAULT 0,
    deleted_rows            INT DEFAULT 0,
    error_rows              INT DEFAULT 0,
    error_message           NVARCHAR(MAX),
    error_details           NVARCHAR(MAX),
    created_by              NVARCHAR(100),
    ip_address              NVARCHAR(50),
    created_at              DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    started_at              DATETIME2,
    completed_at            DATETIME2,
    duration_ms             INT,
    changed_columns_summary NVARCHAR(MAX),
    sample_changes          NVARCHAR(MAX)
)
GO
CREATE NONCLUSTERED INDEX IX_upload_jobs_status ON dbo.upload_jobs(status)
CREATE NONCLUSTERED INDEX IX_upload_jobs_table  ON dbo.upload_jobs(table_name)
GO

-- 4.4 msa_storage_jobs
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.msa_storage_jobs') AND type = 'U')
CREATE TABLE dbo.msa_storage_jobs (
    id               BIGINT IDENTITY(1,1) PRIMARY KEY,
    job_id           NVARCHAR(50) NOT NULL UNIQUE,
    sequence_id      INT NOT NULL,
    status           NVARCHAR(20) NOT NULL DEFAULT 'pending',
    total_rows       INT,
    processed_rows   INT DEFAULT 0,
    inserted_msa     INT DEFAULT 0,
    inserted_colors  INT DEFAULT 0,
    inserted_variants INT DEFAULT 0,
    error_message    NVARCHAR(MAX),
    error_details    NVARCHAR(MAX),
    created_by       NVARCHAR(100) NOT NULL,
    created_at       DATETIME NOT NULL DEFAULT GETUTCDATE(),
    started_at       DATETIME,
    completed_at     DATETIME,
    duration_ms      INT
)
GO
CREATE NONCLUSTERED INDEX IX_msa_jobs_seq    ON dbo.msa_storage_jobs(sequence_id)
CREATE NONCLUSTERED INDEX IX_msa_jobs_status ON dbo.msa_storage_jobs(status)
GO

-- 4.5 table_permissions
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.table_permissions') AND type = 'U')
CREATE TABLE dbo.table_permissions (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    table_name  NVARCHAR(200) NOT NULL UNIQUE,
    can_view    INT NOT NULL DEFAULT 1,
    can_edit    INT NOT NULL DEFAULT 0,
    can_upload  INT NOT NULL DEFAULT 0,
    can_export  INT NOT NULL DEFAULT 0,
    can_delete  INT NOT NULL DEFAULT 0,
    created_at  DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at  DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
)
GO

-- ============================================================================
-- 5. METADATA REGISTRY
-- ============================================================================

-- 5.1 sys_table_registry
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.sys_table_registry') AND type = 'U')
CREATE TABLE dbo.sys_table_registry (
    id                  INT IDENTITY(1,1) PRIMARY KEY,
    table_name          NVARCHAR(200) NOT NULL UNIQUE,
    display_name        NVARCHAR(200),
    description         NVARCHAR(1000),
    module              NVARCHAR(100),
    primary_key_columns NVARCHAR(500),
    is_system_table     BIT NOT NULL DEFAULT 0,
    is_active           BIT NOT NULL DEFAULT 1,
    row_count           BIGINT DEFAULT 0,
    created_at          DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at          DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    created_by          NVARCHAR(100)
)
GO

-- 5.2 sys_column_registry
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.sys_column_registry') AND type = 'U')
CREATE TABLE dbo.sys_column_registry (
    id             INT IDENTITY(1,1) PRIMARY KEY,
    table_id       INT NOT NULL REFERENCES dbo.sys_table_registry(id),
    column_name    NVARCHAR(200) NOT NULL,
    display_name   NVARCHAR(200),
    data_type      NVARCHAR(100),
    max_length     INT,
    is_nullable    BIT DEFAULT 1,
    is_primary_key BIT DEFAULT 0,
    default_value  NVARCHAR(500),
    column_order   INT DEFAULT 0,
    is_active      BIT DEFAULT 1,
    created_at     DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
)
GO

PRINT '============================================'
PRINT '  Claude database schema created successfully'
PRINT '============================================'
GO
