-- 022_dispatch_control_bdc.sql
-- Dispatch-control tables for Generate BDC (2026-07-18).
-- Three tables gate BDC generation so controlled stock is never dispatched:
--   ARS_HOLD_ARTICLE_BDC               (GEN_ART_NUMBER, CLR)  — hold an article colour
--   ARS_DIVISION_DELETE_BDC            (STORE, DIV)           — no dispatch of a division to a store
--   ARS_DIVISION_DELETE_ON_MAJ_CAT_BDC (STORE, MAJ_CAT)       — no dispatch of a MAJ_CAT to a store
--
-- This script normalises ARS_DIVISION_DELETE_BDC: rename STATUS -> DIV and strip the
-- legacy '-DEL' suffix so DIV holds the plain division name (KIDS-DEL -> KIDS), matched
-- against a MAJ_CAT's division from ARS_MSA_GEN_ART. Idempotent.
-- NOTE: the rename and the UPDATE must be in SEPARATE batches (GO) — SQL Server can't
-- reference the new column name in the same batch that renames it.

SET NOCOUNT ON;
GO

-- 1) Rename STATUS -> DIV if the old column still exists.
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME = 'ARS_DIVISION_DELETE_BDC' AND COLUMN_NAME = 'STATUS')
   AND NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME = 'ARS_DIVISION_DELETE_BDC' AND COLUMN_NAME = 'DIV')
BEGIN
    EXEC sp_rename 'dbo.ARS_DIVISION_DELETE_BDC.STATUS', 'DIV', 'COLUMN';
    PRINT '  renamed ARS_DIVISION_DELETE_BDC.STATUS -> DIV';
END
ELSE
    PRINT '  ARS_DIVISION_DELETE_BDC.DIV already present (or STATUS missing) — skip rename';
GO

-- 2) Normalise values: strip the '-DEL' suffix so DIV = plain division (e.g. KIDS).
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME = 'ARS_DIVISION_DELETE_BDC' AND COLUMN_NAME = 'DIV')
BEGIN
    UPDATE dbo.ARS_DIVISION_DELETE_BDC
       SET DIV = LTRIM(RTRIM(REPLACE(DIV, '-DEL', '')))
     WHERE DIV LIKE '%-DEL%';
    PRINT '  normalised DIV values (stripped -DEL)';
END
GO

PRINT 'ARS_DIVISION_DELETE_BDC dispatch-control migration complete.';
GO
