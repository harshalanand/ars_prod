-- Migration 014: Replace cl_* MSA tables with ARS_MSA_* tables
-- Purpose: Rename MSA result tables to ARS naming convention (schema: id + sequence_id only)
-- Old tables: cl_msa, cl_generated_color, cl_color_variant
-- New tables: ARS_MSA_TOTAL, ARS_MSA_GEN_ART, ARS_MSA_VAR_ART
-- Date Created: 2026-04-06

-- ============================================================================
-- Create ARS_MSA_TOTAL table (replaces cl_msa - Base MSA Analysis)
-- ============================================================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'[dbo].[ARS_MSA_TOTAL]') AND type in (N'U'))
BEGIN
    CREATE TABLE [dbo].[ARS_MSA_TOTAL] (
        [id] INT PRIMARY KEY IDENTITY(1,1),
        [sequence_id] INT NOT NULL
    );

    CREATE NONCLUSTERED INDEX [IX_ARS_MSA_TOTAL_sequence_id] ON [dbo].[ARS_MSA_TOTAL]([sequence_id]);

    PRINT '✅ Created table: dbo.ARS_MSA_TOTAL'
END
ELSE
BEGIN
    PRINT '⚠️ Table dbo.ARS_MSA_TOTAL already exists'
END

-- ============================================================================
-- Create ARS_MSA_GEN_ART table (replaces cl_generated_color - Generated Articles)
-- ============================================================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'[dbo].[ARS_MSA_GEN_ART]') AND type in (N'U'))
BEGIN
    CREATE TABLE [dbo].[ARS_MSA_GEN_ART] (
        [id] INT PRIMARY KEY IDENTITY(1,1),
        [sequence_id] INT NOT NULL
    );

    CREATE NONCLUSTERED INDEX [IX_ARS_MSA_GEN_ART_sequence_id] ON [dbo].[ARS_MSA_GEN_ART]([sequence_id]);

    PRINT '✅ Created table: dbo.ARS_MSA_GEN_ART'
END
ELSE
BEGIN
    PRINT '⚠️ Table dbo.ARS_MSA_GEN_ART already exists'
END

-- ============================================================================
-- Create ARS_MSA_VAR_ART table (replaces cl_color_variant - Variant Articles)
-- ============================================================================
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'[dbo].[ARS_MSA_VAR_ART]') AND type in (N'U'))
BEGIN
    CREATE TABLE [dbo].[ARS_MSA_VAR_ART] (
        [id] INT PRIMARY KEY IDENTITY(1,1),
        [sequence_id] INT NOT NULL
    );

    CREATE NONCLUSTERED INDEX [IX_ARS_MSA_VAR_ART_sequence_id] ON [dbo].[ARS_MSA_VAR_ART]([sequence_id]);

    PRINT '✅ Created table: dbo.ARS_MSA_VAR_ART'
END
ELSE
BEGIN
    PRINT '⚠️ Table dbo.ARS_MSA_VAR_ART already exists'
END

-- ============================================================================
-- Drop old tables (cl_msa, cl_generated_color, cl_color_variant)
-- ============================================================================
IF EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'[dbo].[cl_msa]') AND type in (N'U'))
BEGIN
    DROP TABLE [dbo].[cl_msa];
    PRINT '✅ Dropped old table: dbo.cl_msa'
END

IF EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'[dbo].[cl_generated_color]') AND type in (N'U'))
BEGIN
    DROP TABLE [dbo].[cl_generated_color];
    PRINT '✅ Dropped old table: dbo.cl_generated_color'
END

IF EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'[dbo].[cl_color_variant]') AND type in (N'U'))
BEGIN
    DROP TABLE [dbo].[cl_color_variant];
    PRINT '✅ Dropped old table: dbo.cl_color_variant'
END

PRINT '✅ ARS MSA Tables Migration Complete'
PRINT '   ARS_MSA_TOTAL    <- replaces cl_msa (schema: id, sequence_id + dynamic columns)'
PRINT '   ARS_MSA_GEN_ART  <- replaces cl_generated_color (schema: id, sequence_id + dynamic columns)'
PRINT '   ARS_MSA_VAR_ART  <- replaces cl_color_variant (schema: id, sequence_id + dynamic columns)'
