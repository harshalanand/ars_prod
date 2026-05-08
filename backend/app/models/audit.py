"""
Audit Log Model and Export Settings
"""
from datetime import datetime
from sqlalchemy import Column, BigInteger, Integer, String, DateTime, Text
from app.database.session import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    table_name = Column(String(200), nullable=False, index=True)
    action_type = Column(String(50), nullable=False)  # INSERT, UPDATE, DELETE, UPSERT, BULK_UPLOAD, SCHEMA_CHANGE
    record_primary_key = Column(String(500))
    old_data = Column(Text)       # JSON
    new_data = Column(Text)       # JSON
    changed_columns = Column(Text) # JSON array
    changed_by = Column(String(100), nullable=False, index=True)
    changed_at = Column(DateTime, default=datetime.utcnow, index=True)
    source = Column(String(50), default="API")  # UI, API, UPLOAD, SYSTEM
    ip_address = Column(String(50))
    user_agent = Column(String(500))
    session_id = Column(String(200))
    batch_id = Column(String(100), index=True)
    duration_ms = Column(Integer)
    row_count = Column(Integer, default=1)
    notes = Column(String(1000))


class ExportSettings(Base):
    """Settings for data export behavior."""
    __tablename__ = "export_settings"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    setting_key = Column(String(100), nullable=False, unique=True)
    setting_value = Column(Text)
    description = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ExportJob(Base):
    """Background export job tracking."""
    __tablename__ = "export_jobs"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    job_id = Column(String(50), nullable=False, unique=True, index=True)
    table_name = Column(String(200), nullable=False)
    status = Column(String(20), nullable=False, default='pending')  # pending, running, completed, failed
    format = Column(String(10), default='xlsx')
    columns = Column(Text)  # JSON array
    filters = Column(Text)  # JSON object
    total_rows = Column(Integer)
    processed_rows = Column(Integer, default=0)
    file_path = Column(String(500))
    file_size = Column(BigInteger)
    error_message = Column(Text)
    created_by = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    downloaded = Column(Integer, default=0)  # download count


class TablePermission(Base):
    """Table-level permissions for upload/export/edit operations."""
    __tablename__ = "table_permissions"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    table_name = Column(String(200), nullable=False, unique=True, index=True)
    can_view = Column(Integer, default=1)
    can_edit = Column(Integer, default=0)
    can_upload = Column(Integer, default=0)
    can_export = Column(Integer, default=0)
    can_delete = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UploadJob(Base):
    """Background upload job tracking."""
    __tablename__ = "upload_jobs"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    job_id = Column(String(50), nullable=False, unique=True, index=True)
    table_name = Column(String(200), nullable=False)
    file_name = Column(String(500), nullable=False)
    file_path = Column(String(500))
    file_size = Column(BigInteger)
    status = Column(String(20), nullable=False, default='pending')  # pending, running, completed, failed
    primary_key_columns = Column(String(500), nullable=False)
    mode = Column(String(20), default='upsert')  # upsert, delete
    total_rows = Column(Integer)
    processed_rows = Column(Integer, default=0)
    inserted_rows = Column(Integer, default=0)
    updated_rows = Column(Integer, default=0)
    deleted_rows = Column(Integer, default=0)
    error_rows = Column(Integer, default=0)
    error_message = Column(Text)
    error_details = Column(Text)  # JSON array
    created_by = Column(String(100), nullable=False)
    ip_address = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    duration_ms = Column(Integer)
    changed_columns_summary = Column(Text)  # JSON: which columns were changed
    sample_changes = Column(Text)  # JSON: first 100 row changes for validation
    validation_errors = Column(Text)  # JSON: row-level type mismatch details [{row, column, value, expected}]


class DataChangeLog(Base):
    """
    Row-level audit log for detailed change tracking.
    This table stores individual row changes for UI edits, API updates, etc.
    For bulk uploads, detailed logging is optional and runs async to maintain performance.
    """
    __tablename__ = "data_change_log"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    audit_log_id = Column(BigInteger, index=True)  # Links to parent audit_log entry
    table_name = Column(String(200), nullable=False, index=True)
    action_type = Column(String(20), nullable=False)  # INSERT, UPDATE, DELETE
    record_key = Column(String(500), nullable=False)  # Primary key value(s) as JSON
    column_name = Column(String(200))  # Specific column changed (for UPDATE)
    old_value = Column(Text)  # Previous value
    new_value = Column(Text)  # New value
    data_type = Column(String(50))  # Column data type
    changed_by = Column(String(100), nullable=False, index=True)
    changed_at = Column(DateTime, default=datetime.utcnow, index=True)
    source = Column(String(50), default="UI")  # UI, API, UPLOAD
    batch_id = Column(String(100), index=True)  # Groups related changes
    row_index = Column(Integer)  # Row number in batch (for uploads)


class MSAStorageJob(Base):
    """Background MSA result storage job tracking."""
    __tablename__ = "msa_storage_jobs"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    job_id = Column(String(50), nullable=False, unique=True, index=True)
    sequence_id = Column(Integer, nullable=False, index=True)
    status = Column(String(20), nullable=False, default='pending')  # pending, running, completed, failed
    total_rows = Column(Integer)  # Total rows across all three tables
    processed_rows = Column(Integer, default=0)
    inserted_msa = Column(Integer, default=0)  # Rows inserted into ARS_MSA_TOTAL
    inserted_colors = Column(Integer, default=0)  # Rows inserted into ARS_MSA_GEN_ART
    inserted_variants = Column(Integer, default=0)  # Rows inserted into ARS_MSA_VAR_ART
    error_message = Column(Text)
    error_details = Column(Text)  # JSON
    created_by = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    duration_ms = Column(Integer)

