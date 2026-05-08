-- ============================================================================
-- ALLOCATION ENGINE v2 - Score-Based Allocation System
-- Run on Rep_data database
-- ============================================================================
USE [Rep_data]
GO

-- ============================================================================
-- 1. SCORE CONFIGURATION (UI-configurable)
-- ============================================================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.alloc_score_config') AND type = 'U')
CREATE TABLE dbo.alloc_score_config (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    config_name     NVARCHAR(100) NOT NULL DEFAULT 'default',
    attribute_name  NVARCHAR(50)  NOT NULL,  -- SEG, MACRO_MVGR, VENDOR, MRP, FABRIC, COLOR, SEASON, NECK, HERO, FOCUS, ST_SPECIFIC
    score_weight    INT           NOT NULL DEFAULT 0,
    is_active       BIT           NOT NULL DEFAULT 1,
    description     NVARCHAR(500),
    created_at      DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at      DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_by      NVARCHAR(100),
    CONSTRAINT UQ_score_config UNIQUE (config_name, attribute_name)
)
GO

-- Seed default score weights
IF NOT EXISTS (SELECT 1 FROM dbo.alloc_score_config WHERE config_name = 'default')
BEGIN
    INSERT INTO dbo.alloc_score_config (config_name, attribute_name, score_weight, description) VALUES
    ('default', 'ST_SPECIFIC',  9999, 'Store-specific override — infinite priority, article only scores for target stores'),
    ('default', 'NATIONAL_HERO', 100, 'National Hero article bonus — top priority after ST_SPECIFIC'),
    ('default', 'CORE_FOCUS',     60, 'Core Focus article bonus'),
    ('default', 'ASSORTED',       30, 'Assorted/Planned article bonus'),
    ('default', 'SEG',            30, 'Segment match (E/V/P)'),
    ('default', 'MACRO_MVGR',     25, 'Macro merchandise group match'),
    ('default', 'VENDOR',         20, 'Vendor match'),
    ('default', 'MRP_RANGE',      15, 'MRP/Price range match'),
    ('default', 'FABRIC',         10, 'Fabric type match'),
    ('default', 'COLOR',          10, 'Color match'),
    ('default', 'SEASON',         10, 'Season match'),
    ('default', 'NECK',            5, 'Neck/construction match'),
    ('default', 'MVGR1',          15, 'Merchandise group level 1 match'),
    ('default', 'GP_PSF_RANK',    10, 'Gross profit per sq ft ranking bonus')
    PRINT '>> Seeded default score weights'
END
GO

-- ============================================================================
-- 2. ALLOCATION ENGINE SETTINGS
-- ============================================================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.alloc_engine_settings') AND type = 'U')
CREATE TABLE dbo.alloc_engine_settings (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    setting_key     NVARCHAR(100) NOT NULL UNIQUE,
    setting_value   NVARCHAR(MAX),
    data_type       NVARCHAR(20) DEFAULT 'string',  -- string, int, float, bool, json
    description     NVARCHAR(500),
    updated_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_by      NVARCHAR(100)
)
GO

IF NOT EXISTS (SELECT 1 FROM dbo.alloc_engine_settings WHERE setting_key = 'min_score_threshold')
BEGIN
    INSERT INTO dbo.alloc_engine_settings (setting_key, setting_value, data_type, description) VALUES
    ('min_score_threshold',      '0',     'int',   'Minimum score to allocate (0 = no minimum, 30 = must match at least SEG)'),
    ('multi_option_enabled',     'true',  'bool',  'Allow hot articles to take multiple option slots'),
    ('multi_option_min_score',   '150',   'int',   'Minimum score for multi-option eligibility'),
    ('multi_option_max_slots',   '3',     'int',   'Maximum slots a single article can take'),
    ('max_colors_per_store',     '5',     'int',   'Maximum colors of same generic article at one store'),
    ('fallback_level',           'MAJCAT','string', 'Lowest fallback level: MAJCAT, SEG, MVGR (stop allocation below this)'),
    ('mbq_accessory_density',    '3',     'int',   'Default accessory density for MBQ calculation'),
    ('mbq_sales_cover_days',     '14',    'int',   'Sales cover days for MBQ'),
    ('mbq_intransit_days',       '3',     'int',   'In-transit days for MBQ'),
    ('mbq_scan_days',            '2',     'int',   'Scan days cover for MBQ'),
    ('parallel_workers',         '8',     'int',   'Number of parallel MAJCAT workers'),
    ('score_config_name',        'default','string','Which score config to use')
    PRINT '>> Seeded default engine settings'
END
GO

-- ============================================================================
-- 3. ALLOCATION RUN TRACKING
-- ============================================================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.alloc_runs') AND type = 'U')
CREATE TABLE dbo.alloc_runs (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    run_id          NVARCHAR(50)  NOT NULL UNIQUE,  -- UUID
    run_date        DATE          NOT NULL,
    status          NVARCHAR(20)  NOT NULL DEFAULT 'pending',  -- pending, running, completed, failed
    majcats         NVARCHAR(MAX),  -- JSON array of MAJCATs to process (null = all)
    rdc_code        NVARCHAR(20),   -- Which DC
    total_majcats   INT DEFAULT 0,
    processed_majcats INT DEFAULT 0,
    total_stores    INT DEFAULT 0,
    total_articles_scored INT DEFAULT 0,
    total_slots_filled   INT DEFAULT 0,
    total_variants_allocated INT DEFAULT 0,
    total_dos_generated INT DEFAULT 0,
    score_config    NVARCHAR(100) DEFAULT 'default',
    settings_json   NVARCHAR(MAX),  -- Snapshot of settings at run time
    error_message   NVARCHAR(MAX),
    created_by      NVARCHAR(100),
    created_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    started_at      DATETIME2,
    completed_at    DATETIME2,
    duration_ms     INT
)
GO
CREATE NONCLUSTERED INDEX IX_alloc_runs_date ON dbo.alloc_runs(run_date DESC)
CREATE NONCLUSTERED INDEX IX_alloc_runs_status ON dbo.alloc_runs(status)
GO

-- ============================================================================
-- 4. BUDGET CASCADE RESULTS
-- ============================================================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.alloc_budget_cascade') AND type = 'U')
CREATE TABLE dbo.alloc_budget_cascade (
    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
    run_id          NVARCHAR(50)  NOT NULL,
    st_cd           NVARCHAR(20)  NOT NULL,
    majcat          NVARCHAR(50)  NOT NULL,
    seg             NVARCHAR(10),
    macro_mvgr      NVARCHAR(50),
    bgt_disp_q      DECIMAL(12,2) DEFAULT 0,  -- Budget dispatch quantity
    opt_density     INT DEFAULT 0,             -- Options per store for this segment
    opt_count       INT DEFAULT 0,             -- Actual option count this month
    bgt_sales_per_day DECIMAL(10,4) DEFAULT 0,
    mbq             INT DEFAULT 0,             -- Minimum buy quantity
    CONSTRAINT UQ_budget_cascade UNIQUE (run_id, st_cd, majcat, seg)
)
GO
CREATE NONCLUSTERED INDEX IX_bgt_cascade_run ON dbo.alloc_budget_cascade(run_id)
CREATE NONCLUSTERED INDEX IX_bgt_cascade_majcat ON dbo.alloc_budget_cascade(majcat, st_cd)
GO

-- ============================================================================
-- 5. ARTICLE SCORES (output of Engine 2)
-- ============================================================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.alloc_article_scores') AND type = 'U')
CREATE TABLE dbo.alloc_article_scores (
    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
    run_id          NVARCHAR(50)  NOT NULL,
    st_cd           NVARCHAR(20)  NOT NULL,
    majcat          NVARCHAR(50)  NOT NULL,
    gen_art_color   NVARCHAR(50)  NOT NULL,    -- Article-color combo
    gen_art         NVARCHAR(30),
    color           NVARCHAR(20),
    seg             NVARCHAR(10),
    total_score     INT NOT NULL DEFAULT 0,    -- Composite score
    -- Score breakdown
    score_st_specific  INT DEFAULT 0,
    score_hero         INT DEFAULT 0,
    score_focus        INT DEFAULT 0,
    score_seg          INT DEFAULT 0,
    score_mvgr         INT DEFAULT 0,
    score_vendor       INT DEFAULT 0,
    score_mrp          INT DEFAULT 0,
    score_fabric       INT DEFAULT 0,
    score_color        INT DEFAULT 0,
    score_season       INT DEFAULT 0,
    score_neck         INT DEFAULT 0,
    score_gp_psf       INT DEFAULT 0,
    -- Article metadata
    dc_stock_qty    INT DEFAULT 0,
    mrp             DECIMAL(10,2),
    vendor_code     NVARCHAR(20),
    fabric          NVARCHAR(50),
    season          NVARCHAR(10),
    is_st_specific  BIT DEFAULT 0,
    priority_type   NVARCHAR(20),  -- NATIONAL_HERO, CORE_FOCUS, ASSORTED, null
    CONSTRAINT UQ_article_scores UNIQUE (run_id, st_cd, gen_art_color)
)
GO
CREATE NONCLUSTERED INDEX IX_scores_run ON dbo.alloc_article_scores(run_id)
CREATE NONCLUSTERED INDEX IX_scores_majcat ON dbo.alloc_article_scores(run_id, majcat)
CREATE NONCLUSTERED INDEX IX_scores_sort ON dbo.alloc_article_scores(run_id, majcat, total_score DESC)
GO

-- ============================================================================
-- 6. OPTION ASSIGNMENTS (output of Engine 3)
-- ============================================================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.alloc_option_assignments') AND type = 'U')
CREATE TABLE dbo.alloc_option_assignments (
    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
    run_id          NVARCHAR(50)  NOT NULL,
    st_cd           NVARCHAR(20)  NOT NULL,
    majcat          NVARCHAR(50)  NOT NULL,
    seg             NVARCHAR(10),
    opt_no          INT NOT NULL,              -- Option slot number
    gen_art_color   NVARCHAR(50)  NOT NULL,    -- Assigned article-color
    gen_art         NVARCHAR(30),
    color           NVARCHAR(20),
    total_score     INT DEFAULT 0,
    art_status      NVARCHAR(10),              -- L (live), MIX (new), X (exit), FALLBACK
    is_multi_opt    BIT DEFAULT 0,             -- Whether this is a multi-option assignment
    disp_q          INT DEFAULT 0,             -- Dispatch quantity for this option
    mbq             INT DEFAULT 0,
    bgt_sales_per_day DECIMAL(10,4) DEFAULT 0,
    dc_stock_before INT DEFAULT 0,             -- DC stock before this allocation
    dc_stock_after  INT DEFAULT 0,             -- DC stock after deduction
    CONSTRAINT UQ_option_assignment UNIQUE (run_id, st_cd, majcat, seg, opt_no)
)
GO
CREATE NONCLUSTERED INDEX IX_opt_assign_run ON dbo.alloc_option_assignments(run_id)
CREATE NONCLUSTERED INDEX IX_opt_assign_store ON dbo.alloc_option_assignments(run_id, st_cd)
GO

-- ============================================================================
-- 7. VARIANT ASSIGNMENTS (output of Engine 4 — size level)
-- ============================================================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.alloc_variant_assignments') AND type = 'U')
CREATE TABLE dbo.alloc_variant_assignments (
    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
    run_id          NVARCHAR(50)  NOT NULL,
    st_cd           NVARCHAR(20)  NOT NULL,
    gen_art_color   NVARCHAR(50)  NOT NULL,
    var_art         NVARCHAR(30)  NOT NULL,    -- Variant article (size SKU)
    sz              NVARCHAR(20),              -- Size code
    alloc_qty       INT DEFAULT 0,             -- Allocated quantity
    hold_qty        INT DEFAULT 0,             -- Hold quantity
    bgt_sz_cont_pct DECIMAL(8,4) DEFAULT 0,   -- Budget size contribution %
    dc_sz_stock     INT DEFAULT 0,             -- DC stock at this size
    st_sz_stock     INT DEFAULT 0,             -- Store stock at this size
    fill_rate_pct   DECIMAL(5,2) DEFAULT 0,
    short_qty       INT DEFAULT 0,
    excess_qty      INT DEFAULT 0,
    CONSTRAINT UQ_variant_assignment UNIQUE (run_id, st_cd, var_art)
)
GO
CREATE NONCLUSTERED INDEX IX_var_assign_run ON dbo.alloc_variant_assignments(run_id)
CREATE NONCLUSTERED INDEX IX_var_assign_art ON dbo.alloc_variant_assignments(run_id, gen_art_color)
GO

-- ============================================================================
-- 8. DELIVERY ORDERS (output of Engine 5)
-- ============================================================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.alloc_delivery_orders') AND type = 'U')
CREATE TABLE dbo.alloc_delivery_orders (
    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
    run_id          NVARCHAR(50)  NOT NULL,
    do_number       NVARCHAR(50),              -- Generated DO number
    rdc_code        NVARCHAR(20),
    st_cd           NVARCHAR(20)  NOT NULL,
    majcat          NVARCHAR(50),
    gen_art         NVARCHAR(30),
    gen_art_color   NVARCHAR(50),
    var_art         NVARCHAR(30),
    sz              NVARCHAR(20),
    alloc_qty       INT DEFAULT 0,
    status          NVARCHAR(20) DEFAULT 'PENDING',  -- PENDING, POSTED, FAILED
    posted_at       DATETIME2,
    sap_doc_number  NVARCHAR(50)
)
GO
CREATE NONCLUSTERED INDEX IX_do_run ON dbo.alloc_delivery_orders(run_id)
CREATE NONCLUSTERED INDEX IX_do_store ON dbo.alloc_delivery_orders(st_cd)
CREATE NONCLUSTERED INDEX IX_do_status ON dbo.alloc_delivery_orders(status)
GO

-- ============================================================================
-- 9. RUN SUMMARY (per MAJCAT per store)
-- ============================================================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.alloc_run_summary') AND type = 'U')
CREATE TABLE dbo.alloc_run_summary (
    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
    run_id          NVARCHAR(50)  NOT NULL,
    level           NVARCHAR(10)  NOT NULL,    -- CO, ST, ST_SEG
    st_cd           NVARCHAR(20),
    majcat          NVARCHAR(50),
    seg             NVARCHAR(10),
    bgt_disp_q      INT DEFAULT 0,
    bgt_opt         INT DEFAULT 0,
    filled_opt      INT DEFAULT 0,
    unfilled_opt    INT DEFAULT 0,
    l_art_opt       INT DEFAULT 0,
    mix_art_opt     INT DEFAULT 0,
    fallback_opt    INT DEFAULT 0,
    total_alloc_qty INT DEFAULT 0,
    total_do_qty    INT DEFAULT 0,
    avg_score       DECIMAL(8,2) DEFAULT 0,
    min_score       INT DEFAULT 0,
    max_score       INT DEFAULT 0,
    fill_rate_pct   DECIMAL(5,2) DEFAULT 0
)
GO
CREATE NONCLUSTERED INDEX IX_summary_run ON dbo.alloc_run_summary(run_id, level)
GO

PRINT '============================================'
PRINT '  Allocation Engine tables created'
PRINT '============================================'
GO
