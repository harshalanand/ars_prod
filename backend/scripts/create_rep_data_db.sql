-- ============================================================================
-- CREATE DATABASE: Rep_data (Business/Working Data)
-- MDF/LDF on E: drive
-- ============================================================================
USE [master]
GO

IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = 'Rep_data')
BEGIN
    CREATE DATABASE [Rep_data]
    ON PRIMARY (
        NAME       = N'Rep_data',
        FILENAME   = N'E:\MSSQL_DATA\Rep_data.mdf',
        SIZE       = 1GB,
        MAXSIZE    = 20GB,
        FILEGROWTH = 256MB
    )
    LOG ON (
        NAME       = N'Rep_data_log',
        FILENAME   = N'E:\MSSQL_DATA\Rep_data_log.ldf',
        SIZE       = 512MB,
        MAXSIZE    = 5GB,
        FILEGROWTH = 128MB
    )
    PRINT '>> Created database: Rep_data'
END
ELSE
    PRINT '>> Database Rep_data already exists'
GO

-- Enable RCSI for better concurrency (no blocking on reads)
ALTER DATABASE [Rep_data] SET READ_COMMITTED_SNAPSHOT ON
GO

USE [Rep_data]
GO

-- ============================================================================
-- 1. PRODUCT HIERARCHY TABLES
-- ============================================================================

-- 1.1 retail_division
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.retail_division') AND type = 'U')
CREATE TABLE dbo.retail_division (
    id            INT IDENTITY(1,1) PRIMARY KEY,
    division_code NVARCHAR(20)  NOT NULL UNIQUE,
    division_name NVARCHAR(200) NOT NULL,
    is_active     BIT NOT NULL DEFAULT 1,
    created_at    DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
)
GO

-- 1.2 retail_sub_division
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.retail_sub_division') AND type = 'U')
CREATE TABLE dbo.retail_sub_division (
    id                INT IDENTITY(1,1) PRIMARY KEY,
    sub_division_code NVARCHAR(20)  NOT NULL UNIQUE,
    sub_division_name NVARCHAR(200) NOT NULL,
    division_id       INT NOT NULL REFERENCES dbo.retail_division(id),
    is_active         BIT NOT NULL DEFAULT 1,
    created_at        DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
)
GO

-- 1.3 retail_major_category
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.retail_major_category') AND type = 'U')
CREATE TABLE dbo.retail_major_category (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    category_code   NVARCHAR(20)  NOT NULL UNIQUE,
    category_name   NVARCHAR(200) NOT NULL,
    sub_division_id INT NOT NULL REFERENCES dbo.retail_sub_division(id),
    is_active       BIT NOT NULL DEFAULT 1,
    created_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
)
GO

-- 1.4 retail_size_master
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.retail_size_master') AND type = 'U')
CREATE TABLE dbo.retail_size_master (
    id         INT IDENTITY(1,1) PRIMARY KEY,
    size_code  NVARCHAR(20) NOT NULL UNIQUE,
    size_name  NVARCHAR(50),
    size_order INT DEFAULT 0,
    category   NVARCHAR(50),
    is_active  BIT NOT NULL DEFAULT 1
)
GO

-- 1.5 retail_color_master
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.retail_color_master') AND type = 'U')
CREATE TABLE dbo.retail_color_master (
    id         INT IDENTITY(1,1) PRIMARY KEY,
    color_code NVARCHAR(20)  NOT NULL UNIQUE,
    color_name NVARCHAR(100),
    color_hex  NVARCHAR(10),
    is_active  BIT NOT NULL DEFAULT 1
)
GO

-- ============================================================================
-- 2. ARTICLE / PRODUCT TABLES
-- ============================================================================

-- 2.1 retail_gen_article (Generic/Parent Article)
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.retail_gen_article') AND type = 'U')
CREATE TABLE dbo.retail_gen_article (
    id               INT IDENTITY(1,1) PRIMARY KEY,
    gen_article_code NVARCHAR(50) NOT NULL UNIQUE,
    article_name     NVARCHAR(300),
    division_id      INT REFERENCES dbo.retail_division(id),
    sub_division_id  INT REFERENCES dbo.retail_sub_division(id),
    category_id      INT REFERENCES dbo.retail_major_category(id),
    mvgr             NVARCHAR(100),
    fabric           NVARCHAR(200),
    season           NVARCHAR(100),
    brand            NVARCHAR(100),
    mrp              DECIMAL(12,2),
    cost_price       DECIMAL(12,2),
    margin_pct       DECIMAL(8,2),
    is_active        BIT NOT NULL DEFAULT 1,
    created_at       DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at       DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
)
GO
CREATE NONCLUSTERED INDEX IX_gen_article_code ON dbo.retail_gen_article(gen_article_code)
GO

-- 2.2 retail_variant_article (Size x Color Variant)
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.retail_variant_article') AND type = 'U')
CREATE TABLE dbo.retail_variant_article (
    id             INT IDENTITY(1,1) PRIMARY KEY,
    variant_code   NVARCHAR(50) NOT NULL UNIQUE,
    gen_article_id INT NOT NULL REFERENCES dbo.retail_gen_article(id),
    size_code      NVARCHAR(20),
    size_name      NVARCHAR(50),
    color_code     NVARCHAR(20),
    color_name     NVARCHAR(100),
    barcode        NVARCHAR(50),
    mrp            DECIMAL(12,2),
    cost_price     DECIMAL(12,2),
    is_active      BIT NOT NULL DEFAULT 1,
    created_at     DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at     DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
)
GO
CREATE NONCLUSTERED INDEX IX_variant_code ON dbo.retail_variant_article(variant_code)
GO

-- ============================================================================
-- 3. ALLOCATION TABLES
-- ============================================================================

-- 3.1 alloc_header
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.alloc_header') AND type = 'U')
CREATE TABLE dbo.alloc_header (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    allocation_code NVARCHAR(50) NOT NULL UNIQUE,
    allocation_name NVARCHAR(300),
    allocation_type NVARCHAR(50),
    division_id     INT REFERENCES dbo.retail_division(id),
    season          NVARCHAR(100),
    status          NVARCHAR(50) NOT NULL DEFAULT 'DRAFT',
    total_qty       INT DEFAULT 0,
    total_stores    INT DEFAULT 0,
    total_options   INT DEFAULT 0,
    created_by      NVARCHAR(100),
    approved_by     NVARCHAR(100),
    executed_at     DATETIME2,
    created_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
)
GO
CREATE NONCLUSTERED INDEX IX_alloc_header_code ON dbo.alloc_header(allocation_code)
GO

-- 3.2 alloc_detail
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.alloc_detail') AND type = 'U')
CREATE TABLE dbo.alloc_detail (
    id               BIGINT IDENTITY(1,1) PRIMARY KEY,
    allocation_id    INT NOT NULL REFERENCES dbo.alloc_header(id),
    store_code       NVARCHAR(20),
    gen_article_id   INT REFERENCES dbo.retail_gen_article(id),
    variant_id       INT REFERENCES dbo.retail_variant_article(id),
    size_code        NVARCHAR(20),
    color_code       NVARCHAR(20),
    allocated_qty    INT DEFAULT 0,
    override_qty     INT,
    final_qty        INT DEFAULT 0,
    store_grade      NVARCHAR(10),
    allocation_basis NVARCHAR(50),
    created_at       DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at       DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
)
GO
CREATE NONCLUSTERED INDEX IX_alloc_detail_store   ON dbo.alloc_detail(store_code, allocation_id)
CREATE NONCLUSTERED INDEX IX_alloc_detail_article ON dbo.alloc_detail(gen_article_id, variant_id)
GO

-- ============================================================================
-- 4. STOCK & SALES TABLES
-- ============================================================================

-- 4.1 store_stock
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.store_stock') AND type = 'U')
CREATE TABLE dbo.store_stock (
    id             BIGINT IDENTITY(1,1) PRIMARY KEY,
    store_code     NVARCHAR(20) NOT NULL,
    variant_code   NVARCHAR(50) NOT NULL,
    stock_qty      INT DEFAULT 0,
    in_transit_qty INT DEFAULT 0,
    reserved_qty   INT DEFAULT 0,
    available_qty  AS (stock_qty - reserved_qty),
    last_updated   DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT UQ_store_variant UNIQUE (store_code, variant_code)
)
GO
CREATE NONCLUSTERED INDEX IX_store_stock_store ON dbo.store_stock(store_code)
CREATE NONCLUSTERED INDEX IX_store_stock_var   ON dbo.store_stock(variant_code)
GO

-- 4.2 store_sales
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.store_sales') AND type = 'U')
CREATE TABLE dbo.store_sales (
    id           BIGINT IDENTITY(1,1) PRIMARY KEY,
    store_code   NVARCHAR(20) NOT NULL,
    variant_code NVARCHAR(50) NOT NULL,
    sale_date    DATE NOT NULL,
    qty_sold     INT DEFAULT 0,
    sale_value   DECIMAL(12,2) DEFAULT 0,
    CONSTRAINT UQ_store_sale UNIQUE (store_code, variant_code, sale_date)
)
GO
CREATE NONCLUSTERED INDEX IX_store_sales_date ON dbo.store_sales(sale_date, store_code)
GO

-- 4.3 warehouse_stock
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.warehouse_stock') AND type = 'U')
CREATE TABLE dbo.warehouse_stock (
    id             BIGINT IDENTITY(1,1) PRIMARY KEY,
    warehouse_code NVARCHAR(20) NOT NULL,
    variant_code   NVARCHAR(50) NOT NULL,
    stock_qty      INT DEFAULT 0,
    reserved_qty   INT DEFAULT 0,
    available_qty  AS (stock_qty - reserved_qty),
    last_updated   DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT UQ_wh_variant UNIQUE (warehouse_code, variant_code)
)
GO

-- ============================================================================
-- 5. CONTRIBUTION ANALYSIS TABLES
-- ============================================================================

-- 5.1 Cont_presets
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.Cont_presets') AND type = 'U')
CREATE TABLE dbo.Cont_presets (
    preset_name    NVARCHAR(255) PRIMARY KEY,
    preset_type    NVARCHAR(50)  NOT NULL DEFAULT 'standard',
    description    NVARCHAR(MAX),
    config_json    NVARCHAR(MAX),
    sequence_order INT NOT NULL DEFAULT 9999,
    created_date   DATETIME NOT NULL DEFAULT GETDATE(),
    modified_date  DATETIME NOT NULL DEFAULT GETDATE()
)
GO
CREATE NONCLUSTERED INDEX idx_cont_presets_seq ON dbo.Cont_presets(sequence_order)
GO

-- 5.2 Cont_mappings
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.Cont_mappings') AND type = 'U')
CREATE TABLE dbo.Cont_mappings (
    mapping_name  NVARCHAR(255) PRIMARY KEY,
    mapping_json  NVARCHAR(MAX),
    fallback_json NVARCHAR(MAX),
    description   NVARCHAR(MAX),
    created_date  DATETIME DEFAULT GETDATE(),
    modified_date DATETIME DEFAULT GETDATE()
)
GO

-- 5.3 Cont_mapping_assignments
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.Cont_mapping_assignments') AND type = 'U')
CREATE TABLE dbo.Cont_mapping_assignments (
    id            INT IDENTITY(1,1) PRIMARY KEY,
    col_name      NVARCHAR(255),
    mapping_name  NVARCHAR(255) REFERENCES dbo.Cont_mappings(mapping_name),
    prefix        NVARCHAR(255),
    target        NVARCHAR(20) NOT NULL DEFAULT 'Both',
    created_date  DATETIME DEFAULT GETDATE(),
    modified_date DATETIME DEFAULT GETDATE()
)
GO
CREATE NONCLUSTERED INDEX idx_assign_col     ON dbo.Cont_mapping_assignments(col_name)
CREATE NONCLUSTERED INDEX idx_assign_mapping ON dbo.Cont_mapping_assignments(mapping_name)
GO

-- ============================================================================
-- 6. SLOC / STORE LOCATION SETTINGS
-- ============================================================================

IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.ARS_SLOC_SETTINGS') AND type = 'U')
CREATE TABLE dbo.ARS_SLOC_SETTINGS (
    id         INT IDENTITY(1,1) PRIMARY KEY,
    sloc       NVARCHAR(50) NOT NULL UNIQUE,
    kpi        NVARCHAR(200),
    status     NVARCHAR(20) NOT NULL DEFAULT 'Active',
    created_at DATETIME NOT NULL DEFAULT GETDATE(),
    updated_at DATETIME NOT NULL DEFAULT GETDATE()
)
GO
CREATE NONCLUSTERED INDEX IX_sloc ON dbo.ARS_SLOC_SETTINGS(sloc)
GO

-- ============================================================================
-- 7. MSA CALCULATION RESULT TABLES
-- ============================================================================

-- 7.1 MSA_Calculation_Sequence (tracking)
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.MSA_Calculation_Sequence') AND type = 'U')
CREATE TABLE dbo.MSA_Calculation_Sequence (
    sequence_id          INT IDENTITY(1,1) PRIMARY KEY,
    calculation_date     DATETIME2 DEFAULT SYSUTCDATETIME(),
    date_filter          VARCHAR(10),
    filter_columns       NVARCHAR(MAX),
    filters              NVARCHAR(MAX),
    threshold            INT,
    slocs                NVARCHAR(MAX),
    msa_row_count        INT DEFAULT 0,
    gen_color_row_count  INT DEFAULT 0,
    color_variant_row_count INT DEFAULT 0,
    created_by           VARCHAR(255),
    created_at           DATETIME2 DEFAULT SYSUTCDATETIME(),
    status               VARCHAR(50) DEFAULT 'COMPLETED'
)
GO
CREATE NONCLUSTERED INDEX IX_msa_seq_date ON dbo.MSA_Calculation_Sequence(calculation_date DESC)
CREATE NONCLUSTERED INDEX IX_msa_seq_user ON dbo.MSA_Calculation_Sequence(created_by, calculation_date DESC)
GO

-- 7.2 MSA_Column_Definitions
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.MSA_Column_Definitions') AND type = 'U')
CREATE TABLE dbo.MSA_Column_Definitions (
    id                INT IDENTITY(1,1) PRIMARY KEY,
    table_name        VARCHAR(255) NOT NULL,
    column_name       VARCHAR(255) NOT NULL,
    column_type       VARCHAR(50) DEFAULT 'VARCHAR(MAX)',
    created_at        DATETIME2 DEFAULT SYSUTCDATETIME(),
    first_sequence_id INT,
    CONSTRAINT UQ_msa_col UNIQUE (table_name, column_name)
)
GO

-- 7.3 MSA_Filter_Config
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.MSA_Filter_Config') AND type = 'U')
CREATE TABLE dbo.MSA_Filter_Config (
    id             INT IDENTITY(1,1) PRIMARY KEY,
    config_name    NVARCHAR(255) NOT NULL UNIQUE,
    filter_columns NVARCHAR(MAX),
    filter_values  NVARCHAR(MAX),
    sql_agg        INT DEFAULT 25,
    is_last_used   BIT DEFAULT 0,
    created_at     DATETIME2 DEFAULT SYSUTCDATETIME(),
    updated_at     DATETIME2
)
GO

-- 7.4 ARS_MSA_TOTAL (auto-created by app, schema: id + sequence_id + dynamic columns)
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.ARS_MSA_TOTAL') AND type = 'U')
CREATE TABLE dbo.ARS_MSA_TOTAL (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    sequence_id INT NOT NULL
)
GO
CREATE NONCLUSTERED INDEX IX_ARS_MSA_TOTAL_seq ON dbo.ARS_MSA_TOTAL(sequence_id)
GO

-- 7.5 ARS_MSA_GEN_ART (auto-created by app, schema: id + sequence_id + dynamic columns)
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.ARS_MSA_GEN_ART') AND type = 'U')
CREATE TABLE dbo.ARS_MSA_GEN_ART (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    sequence_id INT NOT NULL
)
GO
CREATE NONCLUSTERED INDEX IX_ARS_MSA_GEN_ART_seq ON dbo.ARS_MSA_GEN_ART(sequence_id)
GO

-- 7.6 ARS_MSA_VAR_ART (auto-created by app, schema: id + sequence_id + dynamic columns)
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.ARS_MSA_VAR_ART') AND type = 'U')
CREATE TABLE dbo.ARS_MSA_VAR_ART (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    sequence_id INT NOT NULL
)
GO
CREATE NONCLUSTERED INDEX IX_ARS_MSA_VAR_ART_seq ON dbo.ARS_MSA_VAR_ART(sequence_id)
GO

-- ============================================================================
-- 8. DATA CHECKLIST TABLE
-- ============================================================================

IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.ARS_CHECKLIST') AND type = 'U')
CREATE TABLE dbo.ARS_CHECKLIST (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    table_name      NVARCHAR(200) NOT NULL UNIQUE,
    display_name    NVARCHAR(200),
    group_name      NVARCHAR(100),
    sort_order      INT NOT NULL DEFAULT 0,
    is_active       BIT NOT NULL DEFAULT 1,
    last_checked_at DATETIME,
    created_at      DATETIME NOT NULL DEFAULT GETDATE(),
    updated_at      DATETIME NOT NULL DEFAULT GETDATE()
)
GO

-- ============================================================================
-- 9. ARS_LISTING TABLE (generated dynamically, template shown)
-- ============================================================================
-- NOTE: ARS_LISTING is created dynamically by the /listing/generate endpoint
-- with columns from the grid table. Template structure:
--   ST_CD NVARCHAR(50), RDC NVARCHAR(50),
--   MAJ_CAT NVARCHAR(100), GEN_ART_NUMBER NVARCHAR(100), CLR NVARCHAR(100),
--   [stock_col_1] FLOAT, [stock_col_2] FLOAT, ...
--   STK_TTL FLOAT, IS_NEW BIT DEFAULT 1

PRINT '============================================'
PRINT '  Rep_data database schema created successfully'
PRINT '============================================'
GO
