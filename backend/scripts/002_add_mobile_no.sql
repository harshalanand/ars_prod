-- ============================================================================
-- Add mobile_no column to rbac_users table
-- Mobile number is unique, email is not unique
-- ============================================================================

USE Claude;
GO

-- Add mobile_no column if not exists
IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('rbac_users') AND name = 'mobile_no')
BEGIN
    ALTER TABLE rbac_users ADD mobile_no NVARCHAR(15);
    PRINT 'Added mobile_no column';
END
GO

-- Update existing users with a default mobile_no based on their id
UPDATE rbac_users 
SET mobile_no = CONCAT('999', RIGHT('0000000' + CAST(id AS NVARCHAR), 7)) 
WHERE mobile_no IS NULL;
PRINT 'Updated existing users with default mobile numbers';
GO

-- Make mobile_no NOT NULL
ALTER TABLE rbac_users ALTER COLUMN mobile_no NVARCHAR(15) NOT NULL;
PRINT 'Made mobile_no NOT NULL';
GO

-- Create unique index on mobile_no if not exists
IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'UQ_rbac_users_mobile_no' AND object_id = OBJECT_ID('rbac_users'))
BEGIN
    CREATE UNIQUE INDEX UQ_rbac_users_mobile_no ON rbac_users(mobile_no);
    PRINT 'Created unique index on mobile_no';
END
GO

-- Drop unique constraint on email if exists
DECLARE @EmailConstraintName NVARCHAR(200);
SELECT @EmailConstraintName = i.name 
FROM sys.indexes i
JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
WHERE i.object_id = OBJECT_ID('rbac_users') 
  AND i.is_unique = 1 
  AND c.name = 'email';

IF @EmailConstraintName IS NOT NULL
BEGIN
    EXEC('DROP INDEX ' + @EmailConstraintName + ' ON rbac_users');
    PRINT 'Dropped unique constraint on email';
END
GO

-- Allow NULL for email
ALTER TABLE rbac_users ALTER COLUMN email NVARCHAR(200) NULL;
PRINT 'Made email column nullable';
GO

PRINT 'Schema update complete - mobile_no is now unique, email is not unique';
