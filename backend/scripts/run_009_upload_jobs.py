"""
Migration: Create upload_jobs table for background upload processing
Run: python scripts/run_009_upload_jobs.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database.session import get_system_engine

def run_migration():
    engine = get_system_engine()
    
    migration_sql = """
    -- Create upload_jobs table for background upload processing
    IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'upload_jobs')
    BEGIN
        CREATE TABLE upload_jobs (
            id BIGINT IDENTITY(1,1) PRIMARY KEY,
            job_id NVARCHAR(50) NOT NULL UNIQUE,
            table_name NVARCHAR(200) NOT NULL,
            file_name NVARCHAR(500) NOT NULL,
            file_path NVARCHAR(500),
            file_size BIGINT,
            status NVARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending, running, completed, failed
            primary_key_columns NVARCHAR(500) NOT NULL,
            mode NVARCHAR(20) DEFAULT 'upsert',  -- upsert, delete
            total_rows INT,
            processed_rows INT DEFAULT 0,
            inserted_rows INT DEFAULT 0,
            updated_rows INT DEFAULT 0,
            deleted_rows INT DEFAULT 0,
            error_rows INT DEFAULT 0,
            error_message NVARCHAR(MAX),
            error_details NVARCHAR(MAX),  -- JSON array of row errors
            created_by NVARCHAR(100) NOT NULL,
            ip_address NVARCHAR(50),
            created_at DATETIME2 DEFAULT GETUTCDATE(),
            started_at DATETIME2,
            completed_at DATETIME2,
            duration_ms INT
        );
        
        CREATE INDEX idx_upload_jobs_job_id ON upload_jobs(job_id);
        CREATE INDEX idx_upload_jobs_status ON upload_jobs(status);
        CREATE INDEX idx_upload_jobs_created_by ON upload_jobs(created_by);
        CREATE INDEX idx_upload_jobs_created_at ON upload_jobs(created_at);
        
        PRINT 'Created upload_jobs table';
    END
    ELSE
    BEGIN
        PRINT 'upload_jobs table already exists';
    END
    """
    
    with engine.connect() as conn:
        for statement in migration_sql.split('END'):
            stmt = statement.strip()
            if stmt and 'BEGIN' in stmt:
                stmt = stmt + 'END'
                try:
                    conn.execute(text(stmt))
                    conn.commit()
                except Exception as e:
                    print(f"Statement error: {e}")
        
        # Verify table exists
        result = conn.execute(text("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES 
            WHERE TABLE_NAME = 'upload_jobs'
        """))
        exists = result.scalar() > 0
        
        if exists:
            print("✅ upload_jobs table ready")
        else:
            print("❌ Failed to create upload_jobs table")

if __name__ == "__main__":
    run_migration()
