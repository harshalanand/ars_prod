"""
MSA Storage Job Service - Background MSA result storage with queue
Handles storing MSA calculation results asynchronously without blocking API responses
"""
import uuid
import time
import threading
import queue
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session

from app.database.session import get_db, SessionLocal, DataSessionLocal
from app.models.audit import MSAStorageJob
from app.services.msa_result_storage import MSAResultStorageService
from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


# Job queue and worker state
_job_queue: queue.Queue = queue.Queue()
_current_job: Optional[str] = None
_cancel_requested: Dict[str, bool] = {}
_worker_started = False
_worker_lock = threading.Lock()


def create_msa_storage_job(
    db: Session,
    sequence_id: int,
    calculation_results: Dict[str, Any],
    created_by: str,
) -> dict:
    """
    Create a new MSA storage job and add to queue.
    Jobs run one at a time in FIFO order.
    
    Args:
        db: Database session
        sequence_id: MSA calculation sequence ID from database
        calculation_results: Dict with keys 'msa', 'msa_gen_clr', 'msa_gen_clr_var' containing data
        created_by: Username creating the job
    
    Returns:
        dict with job_id, status, position in queue
    """
    global _worker_started
    
    job_id = f"MSA_{uuid.uuid4().hex[:10]}"
    
    # Count total rows
    total_rows = (
        len(calculation_results.get('msa', [])) +
        len(calculation_results.get('msa_gen_clr', [])) +
        len(calculation_results.get('msa_gen_clr_var', []))
    )
    
    # Count pending/running/queued jobs to show position
    pending_count = db.query(MSAStorageJob).filter(
        MSAStorageJob.status.in_(['pending', 'running', 'queued'])
    ).count()
    
    # Create job record
    job = MSAStorageJob(
        job_id=job_id,
        sequence_id=sequence_id,
        status='pending',
        total_rows=total_rows,
        created_by=created_by,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    
    # Add to queue
    _job_queue.put({
        'job_id': job_id,
        'sequence_id': sequence_id,
        'calculation_results': calculation_results,
        'created_by': created_by,
    })
    
    # Start worker if not running
    _ensure_worker_started()
    
    return {
        'job_id': job_id,
        'status': 'queued',
        'sequence_id': sequence_id,
        'total_rows': total_rows,
        'position_in_queue': pending_count + 1,
        'message': f'MSA storage job queued. Position: {pending_count + 1}'
    }


def get_job_status(db: Session, job_id: str) -> Optional[dict]:
    """Get the status of a specific job."""
    job = db.query(MSAStorageJob).filter(MSAStorageJob.job_id == job_id).first()
    if not job:
        return None
    
    return {
        'job_id': job.job_id,
        'sequence_id': job.sequence_id,
        'status': job.status,
        'total_rows': job.total_rows,
        'processed_rows': job.processed_rows,
        'inserted_msa': job.inserted_msa,
        'inserted_colors': job.inserted_colors,
        'inserted_variants': job.inserted_variants,
        'error_message': job.error_message,
        'created_by': job.created_by,
        'created_at': job.created_at,
        'started_at': job.started_at,
        'completed_at': job.completed_at,
        'duration_ms': job.duration_ms,
    }


def list_jobs(db: Session, status: Optional[str] = None, limit: int = 20) -> List[dict]:
    """List jobs with optional status filter."""
    query = db.query(MSAStorageJob).order_by(MSAStorageJob.created_at.desc())
    
    if status:
        query = query.filter(MSAStorageJob.status == status)
    
    jobs = query.limit(limit).all()
    
    return [
        {
            'job_id': job.job_id,
            'sequence_id': job.sequence_id,
            'status': job.status,
            'total_rows': job.total_rows,
            'processed_rows': job.processed_rows,
            'inserted_msa': job.inserted_msa,
            'inserted_colors': job.inserted_colors,
            'inserted_variants': job.inserted_variants,
            'error_message': job.error_message,
            'created_by': job.created_by,
            'created_at': job.created_at,
            'started_at': job.started_at,
            'completed_at': job.completed_at,
            'duration_ms': job.duration_ms,
        }
        for job in jobs
    ]


def _ensure_worker_started():
    """Ensure the background worker thread is running."""
    global _worker_started, _worker_lock
    
    with _worker_lock:
        if not _worker_started:
            worker_thread = threading.Thread(target=_worker_loop, daemon=True)
            worker_thread.start()
            _worker_started = True
            logger.info("🔄 MSA Storage Job Worker started")


def _worker_loop():
    """Background worker that processes jobs from the queue."""
    while True:
        try:
            # Wait for job with timeout to allow graceful shutdown
            job_data = _job_queue.get(timeout=1)
            
            job_id = job_data['job_id']
            sequence_id = job_data['sequence_id']
            calculation_results = job_data['calculation_results']
            created_by = job_data['created_by']
            
            logger.info(f"🔄 Processing MSA storage job: {job_id}")
            
            # Create fresh database sessions
            system_db = SessionLocal()  # For job tracking (Claude database)
            data_db = DataSessionLocal()  # For MSA data storage (Rep_data database)
            
            try:
                start_time = time.time()
                
                # Update job status to running in system database
                job = system_db.query(MSAStorageJob).filter(MSAStorageJob.job_id == job_id).first()
                if job:
                    job.status = 'running'
                    job.started_at = datetime.utcnow()
                    system_db.commit()
                
                # Create storage service with data database
                storage_service = MSAResultStorageService(data_db)
                
                # Store each table
                try:
                    # Store MSA data
                    inserted_msa = storage_service._store_table_data(
                        'msa',
                        calculation_results.get('msa', []),
                        sequence_id
                    )
                    
                    job = system_db.query(MSAStorageJob).filter(MSAStorageJob.job_id == job_id).first()
                    job.inserted_msa = inserted_msa
                    job.processed_rows = inserted_msa
                    system_db.commit()
                    
                    logger.info(f"✅ Stored {inserted_msa} MSA rows for job {job_id}")
                    
                    # Store generated colors
                    inserted_colors = storage_service._store_table_data(
                        'msa_gen_clr',
                        calculation_results.get('msa_gen_clr', []),
                        sequence_id
                    )
                    
                    job = system_db.query(MSAStorageJob).filter(MSAStorageJob.job_id == job_id).first()
                    job.inserted_colors = inserted_colors
                    job.processed_rows = inserted_msa + inserted_colors
                    system_db.commit()
                    
                    logger.info(f"✅ Stored {inserted_colors} color rows for job {job_id}")
                    
                    # Store color variants
                    inserted_variants = storage_service._store_table_data(
                        'msa_gen_clr_var',
                        calculation_results.get('msa_gen_clr_var', []),
                        sequence_id
                    )
                    
                    job = system_db.query(MSAStorageJob).filter(MSAStorageJob.job_id == job_id).first()
                    job.inserted_variants = inserted_variants
                    job.processed_rows = inserted_msa + inserted_colors + inserted_variants
                    job.status = 'completed'
                    job.completed_at = datetime.utcnow()
                    
                    duration_ms = int((time.time() - start_time) * 1000)
                    job.duration_ms = duration_ms
                    system_db.commit()
                    
                    logger.info(f"✅ MSA storage job {job_id} completed in {duration_ms}ms")
                    logger.info(f"📊 Total inserted: MSA={inserted_msa}, Colors={inserted_colors}, Variants={inserted_variants}")
                    
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"❌ Error storing MSA data for job {job_id}: {error_msg}")
                    
                    job = system_db.query(MSAStorageJob).filter(MSAStorageJob.job_id == job_id).first()
                    job.status = 'failed'
                    job.error_message = error_msg
                    job.completed_at = datetime.utcnow()
                    
                    duration_ms = int((time.time() - start_time) * 1000)
                    job.duration_ms = duration_ms
                    system_db.commit()
                    
                    raise
                
            except Exception as e:
                logger.error(f"❌ MSA storage job {job_id} failed: {str(e)}")
            finally:
                system_db.close()
                data_db.close()
        
        except queue.Empty:
            # No job available, continue waiting
            continue
        except Exception as e:
            logger.error(f"❌ Worker error: {str(e)}")
            time.sleep(1)  # Prevent rapid loop on error


def cancel_job(db: Session, job_id: str) -> bool:
    """
    Cancel a job if it's not running yet.
    
    Returns:
        True if cancelled, False otherwise
    """
    job = db.query(MSAStorageJob).filter(MSAStorageJob.job_id == job_id).first()
    if not job:
        return False
    
    if job.status in ['pending', 'queued']:
        job.status = 'cancelled'
        job.completed_at = datetime.utcnow()
        db.commit()
        return True
    
    return False


def cancel_all_pending_jobs(db: Session) -> int:
    """
    Cancel all pending/queued jobs.
    
    Returns:
        Count of jobs cancelled
    """
    jobs = db.query(MSAStorageJob).filter(
        MSAStorageJob.status.in_(['pending', 'queued'])
    ).all()
    
    count = 0
    for job in jobs:
        job.status = 'cancelled'
        job.completed_at = datetime.utcnow()
        count += 1
    
    db.commit()
    return count
