/* ============================================================================
   023_dev_sync_tables.sql
   Config table for the Dev Sync Manager (PROD HOPC866 -> DEV ARSDBPRO).

   Runs on the DEV (target) data DB (Rep_data on ARSDBPRO). The app also creates
   it lazily (services/dev_sync_service.py :: ensure_tables), so this script is
   for documentation / explicit provisioning. Idempotent — safe to re-run.

   NOTE: Connection settings (source/target servers, users, passwords, linked
   server name, schedule) are NOT stored in the DB — they live in
   backend/app_settings.json under the "dev_sync" block, so nothing is written
   to prod. Only this per-table config list lives in the DB, on the DEV target.

   No RBAC rows needed: the page + API are gated by SUPER_ADMIN in code
   (see api/v1/endpoints/dev_sync.py).
   ============================================================================ */

IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='ARS_DEV_SYNC_TABLES')
BEGIN
    CREATE TABLE ARS_DEV_SYNC_TABLES (
        id             INT IDENTITY(1,1) PRIMARY KEY,
        db_name        NVARCHAR(100)  NOT NULL,           -- Claude | Rep_data
        table_name     NVARCHAR(200)  NOT NULL,
        category       NVARCHAR(30)   NULL,               -- input|reference|config|output|history|identity|other
        is_active      BIT            NOT NULL DEFAULT 0,  -- opt-in per table
        sync_mode      NVARCHAR(20)   NOT NULL DEFAULT 'full',   -- full | incremental
        incr_key_col   NVARCHAR(200)  NULL,               -- monotonic key for incremental
        exclude_reason NVARCHAR(400)  NULL,
        last_sync_at   DATETIME       NULL,
        last_full_at   DATETIME       NULL,
        last_mode      NVARCHAR(20)   NULL,
        src_rows       BIGINT         NULL,
        tgt_rows       BIGINT         NULL,
        last_status    NVARCHAR(20)   NULL,               -- ok | skipped | error
        last_message   NVARCHAR(MAX)  NULL,
        discovered_at  DATETIME       NOT NULL DEFAULT GETDATE(),
        updated_at     DATETIME       NOT NULL DEFAULT GETDATE(),
        CONSTRAINT UQ_ARS_DEV_SYNC_TABLES UNIQUE (db_name, table_name)
    );
END
GO

-- add category column if the table pre-existed without it
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_NAME='ARS_DEV_SYNC_TABLES' AND COLUMN_NAME='category')
    ALTER TABLE ARS_DEV_SYNC_TABLES ADD category NVARCHAR(30) NULL;
GO
