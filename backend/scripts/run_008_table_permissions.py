"""Run table_permissions migration"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.session import engine
from sqlalchemy import text

sql = """
-- Table permissions: which tables can be used for upload, export, edit, view
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='table_permissions' AND xtype='U')
BEGIN
    CREATE TABLE table_permissions (
        id INT IDENTITY(1,1) PRIMARY KEY,
        table_name VARCHAR(200) NOT NULL,
        can_view BIT DEFAULT 1,
        can_edit BIT DEFAULT 0,
        can_upload BIT DEFAULT 0,
        can_export BIT DEFAULT 0,
        can_delete BIT DEFAULT 0,
        created_at DATETIME2 DEFAULT GETDATE(),
        updated_at DATETIME2 DEFAULT GETDATE()
    );
    
    CREATE UNIQUE INDEX IX_table_permissions_table ON table_permissions(table_name);
    
    PRINT 'Created table_permissions table';
END
"""

if __name__ == "__main__":
    with engine.connect() as conn:
        conn.execute(text(sql))
        conn.commit()
        print("Migration completed: table_permissions table ready")
