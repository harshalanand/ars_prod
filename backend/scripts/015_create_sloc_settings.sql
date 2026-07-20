-- =============================================================================
-- Migration 015: Create SLOC Settings Table
-- Purpose: Stores KPI, Active/Inactive and FRESH/GRT pool classification for
--          each distinct SLOC value found in VW_ET_MSA_STK_WITH_MASTER
--          (the MSA stock source view over et_msa_stk).
--
-- sloc_type semantics (FSD_FRESH_GRT_HOLD_CONTROL.md FS-DB-01 / FS-01):
--   FRESH — SLOC stock belongs to the Fresh allocation pool (default)
--   GRT   — SLOC stock belongs to the Growth allocation pool
--   Pools are mutually exclusive; there is no shared/BOTH classification.
--   A SLOC absent from this table is treated as FRESH (with a logged warning).
-- =============================================================================

IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_NAME = 'ARS_SLOC_SETTINGS'
)
BEGIN
    CREATE TABLE ARS_SLOC_SETTINGS (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        sloc        NVARCHAR(50)  NOT NULL UNIQUE,
        kpi         NVARCHAR(200) NULL,
        sloc_type   NVARCHAR(10)  NOT NULL DEFAULT 'FRESH'
                    CONSTRAINT CK_ARS_SLOC_SETTINGS_type
                    CHECK (sloc_type IN ('FRESH','GRT')),
        is_active   BIT           NOT NULL DEFAULT 1,
        created_at  DATETIME      NOT NULL DEFAULT GETDATE(),
        updated_at  DATETIME      NOT NULL DEFAULT GETDATE()
    );

    CREATE INDEX IX_ARS_SLOC_SETTINGS_sloc ON ARS_SLOC_SETTINGS(sloc);

    PRINT 'Table ARS_SLOC_SETTINGS created successfully.';
END
ELSE
BEGIN
    -- Table pre-dates this version: add sloc_type if missing (idempotent).
    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'ARS_SLOC_SETTINGS' AND COLUMN_NAME = 'sloc_type'
    )
    BEGIN
        ALTER TABLE ARS_SLOC_SETTINGS
            ADD sloc_type NVARCHAR(10) NOT NULL
                CONSTRAINT DF_ARS_SLOC_SETTINGS_type DEFAULT 'FRESH'
                CONSTRAINT CK_ARS_SLOC_SETTINGS_type
                CHECK (sloc_type IN ('FRESH','GRT'));
        PRINT 'Column sloc_type added to ARS_SLOC_SETTINGS.';
    END
    ELSE
        PRINT 'Table ARS_SLOC_SETTINGS already exists with sloc_type. Skipping.';
END
GO

-- Seed every live SLOC (source: MSA stock view). V02_GRT is the growth bin;
-- everything else defaults to FRESH — the business refines via this table.
INSERT INTO ARS_SLOC_SETTINGS (sloc, sloc_type)
SELECT s.SLOC,
       CASE WHEN s.SLOC = 'V02_GRT' THEN 'GRT' ELSE 'FRESH' END
FROM (SELECT DISTINCT SLOC FROM VW_ET_MSA_STK_WITH_MASTER WHERE SLOC IS NOT NULL) s
WHERE NOT EXISTS (
    SELECT 1 FROM ARS_SLOC_SETTINGS t WHERE t.sloc = s.SLOC
);

PRINT 'ARS_SLOC_SETTINGS seeded from VW_ET_MSA_STK_WITH_MASTER.';
