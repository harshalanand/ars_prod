-- ============================================================================
-- MIGRATION 015: Add Category-Level Access Control
-- Purpose: Each planner handles specific Major Categories (MAJ_CAT)
--          so no two planners work on the same articles
-- Run on: Claude database (system DB)
-- ============================================================================
USE [Claude]
GO

-- 1. Create the category access table
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.rls_user_category_access') AND type = 'U')
CREATE TABLE dbo.rls_user_category_access (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    user_id         INT NOT NULL REFERENCES dbo.rbac_users(id),
    division        NVARCHAR(100),
    sub_division    NVARCHAR(100),
    major_category  NVARCHAR(100),
    access_level    NVARCHAR(50) NOT NULL DEFAULT 'FULL',
    is_exclusive    BIT NOT NULL DEFAULT 1,
    granted_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    granted_by      NVARCHAR(100),
    is_active       BIT NOT NULL DEFAULT 1,
    notes           NVARCHAR(500),
    CONSTRAINT UQ_user_category UNIQUE (user_id, division, sub_division, major_category)
)
GO

CREATE NONCLUSTERED INDEX IX_rls_cat_user     ON dbo.rls_user_category_access(user_id, is_active)
CREATE NONCLUSTERED INDEX IX_rls_cat_majcat   ON dbo.rls_user_category_access(major_category)
GO

PRINT '>> Created table: rls_user_category_access'
GO

-- 2. View: easy lookup of all assignments
IF EXISTS (SELECT * FROM sys.views WHERE name = 'vw_user_category_assignments')
    DROP VIEW dbo.vw_user_category_assignments
GO

CREATE VIEW dbo.vw_user_category_assignments AS
SELECT 
    u.id AS user_id, u.username, u.full_name,
    ca.division, ca.sub_division, ca.major_category,
    ca.access_level, ca.is_exclusive, ca.is_active,
    ca.granted_by, ca.granted_at
FROM dbo.rls_user_category_access ca
INNER JOIN dbo.rbac_users u ON u.id = ca.user_id
WHERE ca.is_active = 1 AND u.is_active = 1
GO

-- 3. View: detect conflicts (two planners assigned same exclusive category)
IF EXISTS (SELECT * FROM sys.views WHERE name = 'vw_category_conflicts')
    DROP VIEW dbo.vw_category_conflicts
GO

CREATE VIEW dbo.vw_category_conflicts AS
SELECT 
    ca1.major_category, ca1.division, ca1.sub_division,
    u1.username AS user_1, u2.username AS user_2
FROM dbo.rls_user_category_access ca1
INNER JOIN dbo.rls_user_category_access ca2 
    ON ca1.major_category = ca2.major_category
    AND ISNULL(ca1.division, '') = ISNULL(ca2.division, '')
    AND ISNULL(ca1.sub_division, '') = ISNULL(ca2.sub_division, '')
    AND ca1.user_id < ca2.user_id
    AND ca1.is_exclusive = 1 AND ca2.is_exclusive = 1
    AND ca1.is_active = 1 AND ca2.is_active = 1
INNER JOIN dbo.rbac_users u1 ON u1.id = ca1.user_id
INNER JOIN dbo.rbac_users u2 ON u2.id = ca2.user_id
GO

PRINT '>> Migration 015 complete — category RLS ready'
PRINT '>> Next: INSERT rows into rls_user_category_access for each planner'
PRINT '>> Then: SELECT * FROM vw_category_conflicts (should return 0 rows)'
GO
