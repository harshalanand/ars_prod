-- Migration 011: Create MSA Calculation Result Tables with Sequence Tracking
-- Purpose: Store MSA calculation results with version/sequence tracking and dynamic column support
-- Date Created: 2026-03-11

-- ============================================================================
-- Create cl_msa table (Base MSA Analysis)
-- ============================================================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'[dbo].[cl_msa]') AND type in (N'U'))
BEGIN
    CREATE TABLE [dbo].[cl_msa] (
        [id] INT PRIMARY KEY IDENTITY(1,1),
        [sequence_id] INT NOT NULL,
        [calculation_date] DATETIME2 DEFAULT SYSUTCDATETIME(),
        [created_by] VARCHAR(255),
        [created_at] DATETIME2 DEFAULT SYSUTCDATETIME(),
        [updated_at] DATETIME2 DEFAULT SYSUTCDATETIME()
    );
    
    CREATE NONCLUSTERED INDEX [IX_cl_msa_sequence_id] ON [dbo].[cl_msa]([sequence_id]) INCLUDE ([calculation_date]);
    CREATE NONCLUSTERED INDEX [IX_cl_msa_calculation_date] ON [dbo].[cl_msa]([calculation_date] DESC);
    
    PRINT '✅ Created table: dbo.cl_msa'
END
ELSE
BEGIN
    PRINT '⚠️ Table dbo.cl_msa already exists'
END

-- ============================================================================
-- Create cl_generated_color table (Generated Colors Analysis)
-- ============================================================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'[dbo].[cl_generated_color]') AND type in (N'U'))
BEGIN
    CREATE TABLE [dbo].[cl_generated_color] (
        [id] INT PRIMARY KEY IDENTITY(1,1),
        [sequence_id] INT NOT NULL,
        [calculation_date] DATETIME2 DEFAULT SYSUTCDATETIME(),
        [created_by] VARCHAR(255),
        [created_at] DATETIME2 DEFAULT SYSUTCDATETIME(),
        [updated_at] DATETIME2 DEFAULT SYSUTCDATETIME()
    );
    
    CREATE NONCLUSTERED INDEX [IX_cl_generated_color_sequence_id] ON [dbo].[cl_generated_color]([sequence_id]) INCLUDE ([calculation_date]);
    CREATE NONCLUSTERED INDEX [IX_cl_generated_color_calculation_date] ON [dbo].[cl_generated_color]([calculation_date] DESC);
    
    PRINT '✅ Created table: dbo.cl_generated_color'
END
ELSE
BEGIN
    PRINT '⚠️ Table dbo.cl_generated_color already exists'
END

-- ============================================================================
-- Create cl_color_variant table (Color Variants Analysis)
-- ============================================================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'[dbo].[cl_color_variant]') AND type in (N'U'))
BEGIN
    CREATE TABLE [dbo].[cl_color_variant] (
        [id] INT PRIMARY KEY IDENTITY(1,1),
        [sequence_id] INT NOT NULL,
        [calculation_date] DATETIME2 DEFAULT SYSUTCDATETIME(),
        [created_by] VARCHAR(255),
        [created_at] DATETIME2 DEFAULT SYSUTCDATETIME(),
        [updated_at] DATETIME2 DEFAULT SYSUTCDATETIME()
    );
    
    CREATE NONCLUSTERED INDEX [IX_cl_color_variant_sequence_id] ON [dbo].[cl_color_variant]([sequence_id]) INCLUDE ([calculation_date]);
    CREATE NONCLUSTERED INDEX [IX_cl_color_variant_calculation_date] ON [dbo].[cl_color_variant]([calculation_date] DESC);
    
    PRINT '✅ Created table: dbo.cl_color_variant'
END
ELSE
BEGIN
    PRINT '⚠️ Table dbo.cl_color_variant already exists'
END

-- ============================================================================
-- Create MSA_Calculation_Sequence table (Sequence Tracking)
-- ============================================================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'[dbo].[MSA_Calculation_Sequence]') AND type in (N'U'))
BEGIN
    CREATE TABLE [dbo].[MSA_Calculation_Sequence] (
        [sequence_id] INT PRIMARY KEY IDENTITY(1,1),
        [calculation_date] DATETIME2 DEFAULT SYSUTCDATETIME(),
        [date_filter] VARCHAR(10),
        [filter_columns] NVARCHAR(MAX),  -- JSON array of filter columns
        [filters] NVARCHAR(MAX),         -- JSON object of filter values {column: [values]}
        [threshold] INT,
        [slocs] NVARCHAR(MAX),           -- JSON array of SLOC codes
        [msa_row_count] INT DEFAULT 0,
        [gen_color_row_count] INT DEFAULT 0,
        [color_variant_row_count] INT DEFAULT 0,
        [created_by] VARCHAR(255),
        [created_at] DATETIME2 DEFAULT SYSUTCDATETIME(),
        [status] VARCHAR(50) DEFAULT 'COMPLETED'  -- COMPLETED, PENDING, ERROR, etc.
    );
    
    CREATE NONCLUSTERED INDEX [IX_MSA_Calculation_Sequence_date] ON [dbo].[MSA_Calculation_Sequence]([calculation_date] DESC);
    CREATE NONCLUSTERED INDEX [IX_MSA_Calculation_Sequence_user] ON [dbo].[MSA_Calculation_Sequence]([created_by], [calculation_date] DESC);
    
    PRINT '✅ Created table: dbo.MSA_Calculation_Sequence'
END
ELSE
BEGIN
    PRINT '⚠️ Table dbo.MSA_Calculation_Sequence already exists'
END

-- ============================================================================
-- Create MSA_Column_Definitions table (Track new columns)
-- ============================================================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'[dbo].[MSA_Column_Definitions]') AND type in (N'U'))
BEGIN
    CREATE TABLE [dbo].[MSA_Column_Definitions] (
        [id] INT PRIMARY KEY IDENTITY(1,1),
        [table_name] VARCHAR(255) NOT NULL,  -- cl_msa, cl_generated_color, cl_color_variant
        [column_name] VARCHAR(255) NOT NULL,
        [column_type] VARCHAR(50) DEFAULT 'VARCHAR(MAX)',
        [created_at] DATETIME2 DEFAULT SYSUTCDATETIME(),
        [first_sequence_id] INT,  -- First calculation that introduced this column
        UNIQUE([table_name], [column_name])
    );
    
    CREATE NONCLUSTERED INDEX [IX_MSA_Column_Definitions_table] ON [dbo].[MSA_Column_Definitions]([table_name]);
    
    PRINT '✅ Created table: dbo.MSA_Column_Definitions'
END
ELSE
BEGIN
    PRINT '⚠️ Table dbo.MSA_Column_Definitions already exists'
END

PRINT '✅ MSA Result Tables Migration Complete'
