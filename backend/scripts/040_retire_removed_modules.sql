/* ============================================================================
   040 — Retire removed modules (2026-10-14)

   Removed from the app: Contribution %, Auto Cont %, ALC_Fixture, Trends,
   Project Tracker, Training Manual (in-app viewer only — the dossiers in
   frontend/public/docs/manual/*.md stay as the spec / Claude memory).

   ORDER MATTERS: deploy the code first. The startup seed
   (auth_service.seed_permissions_if_needed) only ever INSERTS — if these rows
   are deleted while the old code is still running, the next startup re-creates
   them and re-grants every MOD_* to every active role.

   Run on EACH environment (local HOPC866 dev, Azure prod).
     Part 1  [Claude DB]    permission rows                     — safe, run it
     Part 2  [Rep_data]     checks + data-dictionary row         — safe, run it
     Part 3  [Rep_data]     table / proc drops                   — generates the
             DROP statements for review; nothing is dropped until you run them.
   ============================================================================ */


/* ── Part 1 · Claude DB — retired permission codes ─────────────────────────── */
USE [Claude];
GO

DECLARE @retired TABLE (code NVARCHAR(100) PRIMARY KEY);
INSERT INTO @retired (code) VALUES
    -- Contribution %
    ('MOD_CONTRIB'), ('CONTRIB_PRESETS'), ('CONTRIB_MAPPINGS'), ('CONTRIB_EXECUTE'), ('CONTRIB_REVIEW'),
    -- Auto Cont % / ALC_Fixture
    ('MOD_AUTO_CONT'), ('MOD_ALC_FIXTURE'),
    -- Trends
    ('MOD_TRENDS'), ('TRENDS_DASHBOARD'), ('TRENDS_UPLOAD'), ('TRENDS_REVIEW'),
    -- Project Tracker
    ('MOD_PROJECT_TRACKER'), ('PAGE_PT_DASHBOARD'), ('PAGE_PT_PROJECTS'), ('PAGE_PT_MY_TASKS'),
    -- Training Manual
    ('MOD_TRAINING'), ('PAGE_MANUAL_GALLERY'), ('PAGE_MANUAL_START'), ('PAGE_MANUAL_MSA'),
    ('PAGE_MANUAL_GRID'), ('PAGE_MANUAL_MERGE'), ('PAGE_MANUAL_LISTING'), ('PAGE_MANUAL_REVIEW'),
    ('PAGE_MANUAL_HOLD'), ('PAGE_MANUAL_PENDALC'), ('PAGE_MANUAL_DICTIONARY');

-- Preview: what is about to go
SELECT p.permission_code, p.permission_name,
       (SELECT COUNT(*) FROM rbac_role_permissions rp WHERE rp.permission_id = p.id) AS role_grants
FROM rbac_permissions p
JOIN @retired r ON r.code = p.permission_code
ORDER BY p.permission_code;

BEGIN TRAN;
    DELETE rp
    FROM rbac_role_permissions rp
    JOIN rbac_permissions p ON p.id = rp.permission_id
    JOIN @retired r ON r.code = p.permission_code;

    DELETE p
    FROM rbac_permissions p
    JOIN @retired r ON r.code = p.permission_code;
COMMIT;
GO


/* ── Part 2 · Rep_data — checks + data dictionary ─────────────────────────── */
USE [Rep_data];
GO

-- 2a. Scheduled reports still triggered by the removed Auto Cont % event.
--     The event can no longer fire; switch these to another trigger.
IF OBJECT_ID(N'dbo.ARS_REPORTS', N'U') IS NOT NULL
    SELECT REPORT_ID, NAME, TRIGGER_TYPE, TRIGGER_EVENT
    FROM dbo.ARS_REPORTS
    WHERE TRIGGER_EVENT = 'autocont.completed';

-- 2b. DB-only views / procs / functions that still reference the retired
--     tables (the repo has none; anything listed here lives only in SQL).
SELECT OBJECT_SCHEMA_NAME(m.object_id) AS [schema], OBJECT_NAME(m.object_id) AS [object],
       o.type_desc
FROM sys.sql_modules m
JOIN sys.objects o ON o.object_id = m.object_id
WHERE OBJECT_NAME(m.object_id) <> 'sp_AutoContCompute'
  AND (   m.definition LIKE '%Cont[_]presets%'      OR m.definition LIKE '%Cont[_]mapping%'
       OR m.definition LIKE '%Cont[_]Percentage%'   OR m.definition LIKE '%Cont[_]jobs%'
       OR m.definition LIKE '%AutoCont[_]%'         OR m.definition LIKE '%Trend[_]%'
       OR m.definition LIKE '%PT[_]PROJECT%'        OR m.definition LIKE '%PT[_]ACTIVITY[_]LOG%'
       OR m.definition LIKE '%PT[_]ATTACHMENT%');

-- 2c. Retired tables with row counts — export anything worth keeping
--     (e.g. PT_PROJECT history) before Part 3.
SELECT t.name AS table_name, SUM(ps.row_count) AS row_count
FROM sys.tables t
JOIN sys.dm_db_partition_stats ps ON ps.object_id = t.object_id AND ps.index_id IN (0, 1)
WHERE t.name IN ('Cont_presets', 'Cont_mappings', 'Cont_mapping_assignments', 'Cont_jobs',
                 'PT_PROJECT', 'PT_ACTIVITY_LOG', 'PT_ATTACHMENT')
   OR t.name LIKE 'Cont[_]Percentage[_]%'
   OR t.name LIKE 'AutoCont[_]%'
   OR t.name LIKE 'Trend[_]%'
GROUP BY t.name
ORDER BY t.name;

-- 2d. Dictionary entry for the removed module (the seeder never deletes rows).
IF OBJECT_ID(N'dbo.ARS_DATA_DICTIONARY', N'U') IS NOT NULL
    DELETE FROM dbo.ARS_DATA_DICTIONARY
    WHERE column_name = 'STOCK_CONT% / SALE_CONT% / INITIAL AUTO CONT%';
GO


/* ── Part 3 · Rep_data — generate DROP statements (review, then run) ──────────
   Copy the output column, check every line, then execute it. Trend_* matches
   every table the Trends upload created (its fixed prefix); confirm none of
   them is used elsewhere before dropping.
   Also delete the Project Tracker attachment folder on the app server:
   backend/pt_attachments/                                                     */
USE [Rep_data];
GO

SELECT stmt FROM (
    -- PT children first (they reference PT_PROJECT)
    SELECT 1 AS ord, t.name, 'DROP TABLE [dbo].[' + t.name + '];' AS stmt
    FROM sys.tables t WHERE t.name IN ('PT_ATTACHMENT', 'PT_ACTIVITY_LOG')
    UNION ALL
    SELECT 2, t.name, 'DROP TABLE [dbo].[' + t.name + '];'
    FROM sys.tables t WHERE t.name = 'PT_PROJECT'
    UNION ALL
    -- Cont_mapping_assignments references Cont_mappings
    SELECT 3, t.name, 'DROP TABLE [dbo].[' + t.name + '];'
    FROM sys.tables t WHERE t.name = 'Cont_mapping_assignments'
    UNION ALL
    SELECT 4, t.name, 'DROP TABLE [dbo].[' + t.name + '];'
    FROM sys.tables t
    WHERE t.name IN ('Cont_presets', 'Cont_mappings', 'Cont_jobs')
       OR t.name LIKE 'Cont[_]Percentage[_]%'
       OR t.name LIKE 'AutoCont[_]%'
       OR t.name LIKE 'Trend[_]%'
    UNION ALL
    SELECT 5, o.name, 'DROP PROCEDURE [dbo].[' + o.name + '];'
    FROM sys.objects o WHERE o.name = 'sp_AutoContCompute' AND o.type = 'P'
) x
ORDER BY ord, name;
GO
