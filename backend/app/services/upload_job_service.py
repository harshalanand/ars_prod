"""
Upload Job Service - Background upload processing with queue
"""
import os
import io
import json
import uuid
import time
import threading
import queue
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.session import get_db, get_data_engine, SessionLocal
from app.models.audit import UploadJob
from app.services.upsert_engine import UpsertEngine
from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class CancelledError(Exception):
    """Raised when a job is cancelled."""
    pass


# Job queue and worker state
_job_queue: queue.Queue = queue.Queue()
_current_job: Optional[str] = None
_cancel_requested: Dict[str, bool] = {}
_worker_started = False
_worker_lock = threading.Lock()

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Allowed file extensions
ALLOWED_EXTENSIONS = {'.csv', '.xlsx', '.xls'}


def create_upload_job(
    db: Session,
    table_name: str,
    file_name: str,
    file_content: bytes,
    primary_key_columns: List[str],
    mode: str,
    created_by: str,
    ip_address: Optional[str] = None,
    column_mapping: Optional[Dict] = None,
    skip_rows: int = 0,
    sheet_name: Optional[str] = None,
) -> dict:
    """
    Create a new upload job and add to queue.
    Jobs run one at a time in FIFO order.
    """
    global _worker_started
    
    job_id = f"UPL_{uuid.uuid4().hex[:10]}"
    
    # Validate file extension
    ext = os.path.splitext(file_name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}")
    
    # Save file
    file_path = os.path.join(UPLOAD_DIR, f"{job_id}_{file_name}")
    with open(file_path, 'wb') as f:
        f.write(file_content)
    
    file_size = len(file_content)
    
    # Count pending/running jobs to show position
    pending_count = db.query(UploadJob).filter(
        UploadJob.status.in_(['pending', 'running'])
    ).count()
    
    # Create job record
    job = UploadJob(
        job_id=job_id,
        table_name=table_name,
        file_name=file_name,
        file_path=file_path,
        file_size=file_size,
        status='queued',
        primary_key_columns=','.join(primary_key_columns),
        mode=mode,
        created_by=created_by,
        ip_address=ip_address,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    
    logger.info(f"[{job_id}] Upload job queued: {file_name} → {table_name} (position: {pending_count + 1})")
    
    # Store metadata for background processing
    job_metadata = {
        'column_mapping': column_mapping,
        'skip_rows': skip_rows,
        'sheet_name': sheet_name,
    }
    
    # Add to queue
    _job_queue.put((job_id, job_metadata))
    
    # Start worker if not running
    with _worker_lock:
        if not _worker_started:
            _start_worker()
            _worker_started = True
    
    return {
        'job_id': job_id,
        'status': 'queued',
        'table_name': table_name,
        'file_name': file_name,
        'file_size': file_size,
        'mode': mode,
        'queue_position': pending_count + 1,
    }


def _start_worker():
    """Start the background worker thread."""
    thread = threading.Thread(target=_worker_loop, daemon=True, name="UploadWorker")
    thread.start()
    logger.info("Upload worker thread started")


def _worker_loop():
    """Main worker loop - processes jobs one at a time from queue."""
    global _current_job
    
    while True:
        try:
            # Wait for next job (blocking)
            job_id, metadata = _job_queue.get(timeout=60)
            
            _current_job = job_id
            logger.info(f"[{job_id}] Starting job from queue")
            
            _run_upload_job(job_id, metadata)
            
            _current_job = None
            _job_queue.task_done()
            
        except queue.Empty:
            # No jobs, keep waiting
            continue
        except Exception as e:
            logger.error(f"Worker error: {e}", exc_info=True)
            _current_job = None


def _read_file(file_path: str, skip_rows: int = 0, sheet_name: Optional[str] = None) -> pd.DataFrame:
    """Read CSV or Excel file into DataFrame.
    keep_default_na=False so that 'NA' is kept as the string 'NA', not treated as NaN."""
    ext = os.path.splitext(file_path)[1].lower()

    if ext == '.csv':
        return pd.read_csv(file_path, skiprows=skip_rows, dtype=str, encoding='utf-8',
                           keep_default_na=False, na_values=[])
    elif ext in ('.xlsx', '.xls'):
        return pd.read_excel(
            file_path,
            sheet_name=sheet_name or 0,
            skiprows=skip_rows,
            dtype=str,
            keep_default_na=False, na_values=[],
            engine='openpyxl' if ext == '.xlsx' else 'xlrd'
        )
    else:
        raise ValueError(f"Unsupported file extension: {ext}")


def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Clean DataFrame before upsert.

    Rules:
    - Blank/empty → __SKIP__  (ignore, keep existing DB value)
    - 'NA' string → kept as 'NA' (update DB with string 'NA')
    - '|' symbol  → __NULL__  (set DB value to NULL)
    - '-' symbol  → __NULL__  (set DB value to NULL)
    """
    for col in df.columns:
        raw = df[col].astype(str)
        stripped = raw.str.strip()
        result = raw.copy()
        result[stripped.isin(["", "nan", "None", "NaT"])] = "__SKIP__"
        result[stripped.isin(["|", "-"])] = "__NULL__"
        df[col] = result
    return df


def _run_upload_job(job_id: str, metadata: dict):
    """Background thread function to run upload job."""
    db = SessionLocal()
    start_time = time.time()
    
    try:
        job = db.query(UploadJob).filter(UploadJob.job_id == job_id).first()
        if not job:
            return
        
        # Check if cancelled before starting
        if _cancel_requested.get(job_id):
            job.status = 'cancelled'
            job.completed_at = datetime.utcnow()
            job.error_message = 'Cancelled before start'
            db.commit()
            _cancel_requested.pop(job_id, None)
            return
        
        # Update status to running
        job.status = 'running'
        job.started_at = datetime.utcnow()
        db.commit()
        
        pk_columns = [c.strip() for c in job.primary_key_columns.split(',')]
        column_mapping = metadata.get('column_mapping')
        skip_rows = metadata.get('skip_rows', 0)
        sheet_name = metadata.get('sheet_name')
        
        # Read file
        logger.info(f"[{job_id}] Reading file: {job.file_path}")
        df = _read_file(job.file_path, skip_rows, sheet_name)
        
        if df.empty:
            raise ValueError("File contains no data")
        
        # Check cancellation
        if _cancel_requested.get(job_id):
            raise CancelledError("Job cancelled by user")
        
        job.total_rows = len(df)
        db.commit()
        
        logger.info(f"[{job_id}] Read {len(df)} rows, {len(df.columns)} columns")
        
        # Apply column mapping
        if column_mapping:
            df = df.rename(columns=column_mapping)
        
        # Clean and normalize column names (uppercase, replace special chars with underscores)
        import re
        def normalize_col(c):
            normalized = re.sub(r'[^A-Z0-9_]', '_', str(c).upper().strip())
            normalized = re.sub(r'_+', '_', normalized)  # Collapse multiple underscores
            return normalized.strip('_')  # Remove leading/trailing underscores
        
        df.columns = [normalize_col(c) for c in df.columns]
        
        # Validate PKs (case-insensitive)
        df_cols_upper = {c.upper() for c in df.columns}
        missing_pks = [pk for pk in pk_columns if pk.upper() not in df_cols_upper]
        if missing_pks:
            raise ValueError(f"Primary key columns not found: {missing_pks}")
        
        # Drop rows with null PKs
        pk_null_mask = df[pk_columns].isna().any(axis=1)
        null_pk_count = pk_null_mask.sum()
        if null_pk_count > 0:
            logger.warning(f"[{job_id}] Dropping {null_pk_count} rows with null PKs")
            df = df[~pk_null_mask]
            job.total_rows = len(df)
            db.commit()
        
        # Clean data
        df = _clean_dataframe(df)
        
        if job.mode == 'delete':
            # Delete mode
            result = _run_delete(db, job, df, pk_columns)
        else:
            # Upsert mode - create progress callback to update DB
            def progress_callback(processed: int, total: int):
                """Update job progress in database."""
                try:
                    job.processed_rows = processed
                    db.commit()
                except Exception as e:
                    logger.warning(f"[{job_id}] Failed to update progress: {e}")
            
            try:
                result = _run_upsert(db, job, df, pk_columns, progress_callback)
            except InterruptedError:
                raise CancelledError("Job cancelled by user")

        db.refresh(job)
        if job.status == 'cancelled' or _cancel_requested.get(job_id):
            raise CancelledError("Job cancelled by user")
        
        # Update job with results
        duration_ms = int((time.time() - start_time) * 1000)
        job.status = 'completed'
        job.completed_at = datetime.utcnow()
        job.duration_ms = duration_ms
        job.processed_rows = result.get('total_records', 0)
        job.inserted_rows = result.get('inserted', 0)
        job.updated_rows = result.get('updated', 0)
        job.deleted_rows = result.get('deleted', 0)
        job.error_rows = result.get('errors', 0)
        
        # Store batch report details
        if result.get('changed_columns_summary'):
            job.changed_columns_summary = json.dumps(result['changed_columns_summary'])
        if result.get('sample_changes'):
            job.sample_changes = json.dumps(result['sample_changes'][:100])  # First 100 changes
        
        if result.get('error_details'):
            job.error_details = json.dumps(result['error_details'][:100])  # Limit to 100 errors

        # Store row-level validation errors (type mismatches with row/column detail)
        if result.get('validation_errors'):
            job.validation_errors = json.dumps(result['validation_errors'][:200])
        
        db.commit()
        
        logger.info(
            f"[{job_id}] Upload complete: {job.inserted_rows} inserted, "
            f"{job.updated_rows} updated, {job.error_rows} errors, {duration_ms}ms"
        )
        
    except CancelledError as e:
        logger.info(f"[{job_id}] Upload cancelled: {e}")
        duration_ms = int((time.time() - start_time) * 1000)
        job.status = 'cancelled'
        job.completed_at = datetime.utcnow()
        job.duration_ms = duration_ms
        job.error_message = str(e)
        db.commit()
        
    except Exception as e:
        logger.error(f"[{job_id}] Upload failed: {e}", exc_info=True)
        
        duration_ms = int((time.time() - start_time) * 1000)
        job.status = 'failed'
        job.completed_at = datetime.utcnow()
        job.duration_ms = duration_ms
        job.error_message = str(e)
        db.commit()
        
    finally:
        # Auto-delete uploaded file after processing
        try:
            if job and job.file_path and os.path.exists(job.file_path):
                os.remove(job.file_path)
                logger.info(f"[{job_id}] Cleaned up uploaded file: {job.file_path}")
        except Exception as e:
            logger.warning(f"[{job_id}] Failed to clean up file: {e}")

        db.close()
        # Cleanup cancellation flag
        _cancel_requested.pop(job_id, None)


def _run_upsert(db: Session, job: UploadJob, df: pd.DataFrame, pk_columns: List[str], progress_callback=None) -> dict:
    """Execute upsert operation."""
    engine = UpsertEngine(db)

    # Pre-validate data types — gives users row-level error details
    validation_errors = engine.validate_data_types(
        table_name=job.table_name,
        df=df,
        max_errors=200,
    )
    if validation_errors:
        logger.warning(f"[{job.job_id}] {len(validation_errors)} type validation errors detected")

    # For large uploads (>50k rows), skip detailed row-level audit collection
    # as it's too slow. Summary audit is always logged.
    # Sample changes (first 100) are always collected for validation.
    total_rows = len(df)
    enable_detailed_audit = total_rows <= 50000

    if not enable_detailed_audit:
        logger.info(f"[{job.job_id}] Large upload ({total_rows} rows) - collecting sample changes only")

    result = engine.upsert(
        table_name=job.table_name,
        df=df,
        primary_key_columns=pk_columns,
        changed_by=job.created_by,
        source="UPLOAD",
        ip_address=job.ip_address,
        chunk_size=settings.UPLOAD_CHUNK_SIZE,
        cancel_check=lambda: bool(_cancel_requested.get(job.job_id)),
        enable_row_audit=enable_detailed_audit,  # Only collect full audit for smaller uploads
        progress_callback=progress_callback,
        collect_sample_changes=True,  # Always collect sample for validation
    )

    # Attach validation errors to result
    if validation_errors:
        result["validation_errors"] = validation_errors

    return result


def _run_delete(db: Session, job: UploadJob, df: pd.DataFrame, pk_columns: List[str]) -> dict:
    """Execute delete operation."""
    from app.services.file_upload_service import FileUploadService
    
    service = FileUploadService(db)
    
    # Build delete query for each row
    data_engine = get_data_engine()
    deleted = 0
    not_found = 0
    errors = 0
    error_details = []
    total_rows = len(df)
    
    with data_engine.connect() as conn:
        for idx, row in df.iterrows():
            if _cancel_requested.get(job.job_id):
                raise CancelledError("Job cancelled by user")
            
            # Log progress every 1000 rows
            if (idx + 1) % 1000 == 0 or (idx + 1) == total_rows:
                logger.info(f"[{job.job_id}] Processing delete row {idx + 1} of {total_rows} ({int(((idx + 1) / total_rows) * 100)}%)")

            try:
                where_parts = []
                params = {}
                for i, pk in enumerate(pk_columns):
                    param_name = f"pk{i}"
                    where_parts.append(f"[{pk}] = :{param_name}")
                    params[param_name] = row[pk]
                
                where_clause = " AND ".join(where_parts)
                
                # Check if exists
                check_sql = f"SELECT COUNT(*) FROM [{job.table_name}] WHERE {where_clause}"
                exists = conn.execute(text(check_sql), params).scalar()
                
                if exists:
                    delete_sql = f"DELETE FROM [{job.table_name}] WHERE {where_clause}"
                    conn.execute(text(delete_sql), params)
                    deleted += 1
                else:
                    not_found += 1
                    
            except Exception as e:
                errors += 1
                error_details.append(f"Row {idx + 1}: {str(e)}")
        
        conn.commit()
    
    return {
        'total_records': len(df),
        'deleted': deleted,
        'not_found': not_found,
        'errors': errors,
        'error_details': error_details,
    }


def get_job_status(db: Session, job_id: str) -> Optional[dict]:
    """Get status of an upload job."""
    job = db.query(UploadJob).filter(UploadJob.job_id == job_id).first()
    if not job:
        return None
    
    return {
        'job_id': job.job_id,
        'table_name': job.table_name,
        'file_name': job.file_name,
        'file_size': job.file_size,
        'status': job.status,
        'mode': job.mode,
        'total_rows': job.total_rows,
        'processed_rows': job.processed_rows,
        'inserted_rows': job.inserted_rows,
        'updated_rows': job.updated_rows,
        'deleted_rows': job.deleted_rows,
        'error_rows': job.error_rows,
        'error_message': job.error_message,
        'error_details': json.loads(job.error_details) if job.error_details else None,
        'validation_errors': json.loads(job.validation_errors) if job.validation_errors else None,
        'changed_columns_summary': json.loads(job.changed_columns_summary) if job.changed_columns_summary else None,
        'sample_changes': json.loads(job.sample_changes) if job.sample_changes else None,
        'created_by': job.created_by,
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'started_at': job.started_at.isoformat() if job.started_at else None,
        'completed_at': job.completed_at.isoformat() if job.completed_at else None,
        'duration_ms': job.duration_ms,
    }


def get_user_jobs(db: Session, username: str, limit: int = 20) -> List[dict]:
    """Get recent upload jobs for a user."""
    jobs = db.query(UploadJob).filter(
        UploadJob.created_by == username
    ).order_by(UploadJob.created_at.desc()).limit(limit).all()
    
    return [
        {
            'job_id': j.job_id,
            'table_name': j.table_name,
            'file_name': j.file_name,
            'file_size': j.file_size,
            'status': j.status,
            'mode': j.mode,
            'total_rows': j.total_rows,
            'inserted_rows': j.inserted_rows,
            'updated_rows': j.updated_rows,
            'error_rows': j.error_rows,
            'created_at': j.created_at.isoformat() if j.created_at else None,
            'duration_ms': j.duration_ms,
        }
        for j in jobs
    ]


def get_all_jobs(db: Session, limit: int = 50) -> List[dict]:
    """Get all recent upload jobs (admin view)."""
    jobs = db.query(UploadJob).order_by(UploadJob.created_at.desc()).limit(limit).all()
    
    return [
        {
            'job_id': j.job_id,
            'table_name': j.table_name,
            'file_name': j.file_name,
            'file_size': j.file_size,
            'status': j.status,
            'mode': j.mode,
            'total_rows': j.total_rows,
            'inserted_rows': j.inserted_rows,
            'updated_rows': j.updated_rows,
            'error_rows': j.error_rows,
            'created_by': j.created_by,
            'created_at': j.created_at.isoformat() if j.created_at else None,
            'duration_ms': j.duration_ms,
        }
        for j in jobs
    ]


def cancel_job(db: Session, job_id: str, force: bool = False) -> dict:
    """
    Cancel a queued or running upload job.
    For queued jobs: marks as cancelled immediately.
    For running jobs: sets cancellation flag (job checks this periodically).
    """
    job = db.query(UploadJob).filter(UploadJob.job_id == job_id).first()
    if not job:
        return {'success': False, 'error': 'Job not found'}
    
    if job.status in ('completed', 'failed', 'cancelled'):
        return {'success': False, 'error': f'Job already {job.status}'}
    
    if job.status == 'queued':
        # Not started yet - mark as cancelled immediately
        job.status = 'cancelled'
        job.completed_at = datetime.utcnow()
        job.error_message = 'Cancelled by user'
        db.commit()
        logger.info(f"[{job_id}] Queued job cancelled")
        return {'success': True, 'message': 'Job cancelled'}
    
    if job.status == 'running':
        # Set cancellation flag - job will check this
        _cancel_requested[job_id] = True

        if force:
            job.status = 'cancelled'
            job.completed_at = datetime.utcnow()
            job.error_message = 'Force stopped by user'
            if job.started_at:
                job.duration_ms = int((datetime.utcnow() - job.started_at).total_seconds() * 1000)
            db.commit()
            logger.info(f"[{job_id}] Force stop requested for running job")
            return {'success': True, 'message': 'Force stop requested'}

        logger.info(f"[{job_id}] Cancellation requested for running job")
        return {'success': True, 'message': 'Cancellation requested'}
    
    return {'success': False, 'error': f'Unknown status: {job.status}'}


def get_queue_status() -> dict:
    """Get current queue status."""
    return {
        'queue_size': _job_queue.qsize(),
        'current_job': _current_job,
        'worker_running': _worker_started,
    }


def delete_job(db: Session, job_id: str, username: str) -> dict:
    """Delete a completed/failed/cancelled job record."""
    job = db.query(UploadJob).filter(UploadJob.job_id == job_id).first()
    
    if not job:
        return {'success': False, 'error': 'Job not found'}
    
    # Only allow deletion of terminal state jobs
    if job.status in ['running', 'queued']:
        return {'success': False, 'error': 'Cannot delete running or queued jobs. Cancel them first.'}
    
    # Optional: check ownership (allow admin or job creator)
    # For now, allow anyone to delete their own jobs
    if job.created_by != username:
        # Check if user is admin (simplified - adjust based on your RBAC)
        # For now, just allow it
        pass
    
    db.delete(job)
    db.commit()
    
    logger.info(f"[{job_id}] Job deleted by {username}")
    
    return {
        'success': True,
        'message': f'Job {job_id} deleted successfully'
    }
