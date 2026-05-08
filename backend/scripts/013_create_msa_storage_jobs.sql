-- ============================================================================
-- 013_create_msa_storage_jobs.sql
-- Create background job tracking table for MSA result storage
-- Database: Claude (System/RBAC database)
-- Created: 2026-03-11
-- ============================================================================

USE Claude;
GO

-- Create MSA Storage Jobs table for tracking background data insertion jobs
CREATE TABLE dbo.msa_storage_jobs (
    id BIGINT PRIMARY KEY IDENTITY(1,1),
    
    -- Job Identification
    job_id VARCHAR(50) NOT NULL UNIQUE,
    sequence_id INT NOT NULL,
    
    -- Job Status & Progress
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending, running, completed, failed, cancelled
    total_rows INT,  -- Total rows across all three tables
    processed_rows INT DEFAULT 0,
    
    -- Table-specific Insert Counts
    inserted_msa INT DEFAULT 0,  -- Rows inserted into cl_msa
    inserted_colors INT DEFAULT 0,  -- Rows inserted into cl_generated_color
    inserted_variants INT DEFAULT 0,  -- Rows inserted into cl_color_variant
    
    -- Error Tracking
    error_message NVARCHAR(MAX),
    error_details NVARCHAR(MAX),  -- JSON with error stack trace
    
    -- Metadata
    created_by VARCHAR(100) NOT NULL,
    created_at DATETIME DEFAULT SYSUTCDATETIME(),
    started_at DATETIME,
    completed_at DATETIME,
    duration_ms INT  -- Processing duration in milliseconds
);

-- Create indexes separately
CREATE UNIQUE INDEX IX_msa_storage_jobs_job_id ON dbo.msa_storage_jobs(job_id);
CREATE INDEX IX_msa_storage_jobs_sequence_id ON dbo.msa_storage_jobs(sequence_id);
CREATE INDEX IX_msa_storage_jobs_status ON dbo.msa_storage_jobs(status);
CREATE INDEX IX_msa_storage_jobs_created_at ON dbo.msa_storage_jobs(created_at);
CREATE INDEX IX_msa_storage_jobs_created_by ON dbo.msa_storage_jobs(created_by);

GO

PRINT N'✅ Created table: dbo.msa_storage_jobs';

-- Add sample data
INSERT INTO dbo.msa_storage_jobs
(job_id, sequence_id, status, total_rows, created_by)
VALUES
('SAMPLE_JOB_001', 1, 'completed', 446360, 'system');

PRINT N'✅ Created msa_storage_jobs table successfully';
PRINT N'   - Tracks background MSA data insertion jobs';
PRINT N'   - Records: job_id, sequence_id, status, row counts, error info';
PRINT N'   - Indexes on: job_id, sequence_id, status, created_at, created_by';

GO
