"""Run export_jobs migration"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.session import engine
from sqlalchemy import text

sql = """
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='export_jobs' AND xtype='U')
BEGIN
    CREATE TABLE export_jobs (
        id BIGINT IDENTITY(1,1) PRIMARY KEY,
        job_id VARCHAR(50) NOT NULL UNIQUE,
        table_name VARCHAR(200) NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        format VARCHAR(10) DEFAULT 'xlsx',
        columns NVARCHAR(MAX),
        filters NVARCHAR(MAX),
        total_rows INT,
        processed_rows INT DEFAULT 0,
        file_path VARCHAR(500),
        file_size BIGINT,
        error_message NVARCHAR(MAX),
        created_by VARCHAR(100) NOT NULL,
        created_at DATETIME2 DEFAULT GETDATE(),
        started_at DATETIME2,
        completed_at DATETIME2,
        downloaded INT DEFAULT 0
    );
    
    CREATE INDEX IX_export_jobs_job_id ON export_jobs(job_id);
    CREATE INDEX IX_export_jobs_created_by ON export_jobs(created_by);
    CREATE INDEX IX_export_jobs_status ON export_jobs(status);
    
    PRINT 'Created export_jobs table';
END
"""

if __name__ == "__main__":
    with engine.connect() as conn:
        conn.execute(text(sql))
        conn.commit()
        print("Migration completed: export_jobs table ready")
