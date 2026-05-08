"""
Export Job Service - Background export processing
"""
import os
import io
import json
import uuid
import threading
from datetime import datetime
from typing import Optional, Dict, Any, List
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.session import get_db, get_data_engine, SessionLocal
from app.models.audit import ExportJob, ExportSettings

# Store for running jobs
_running_jobs: Dict[str, dict] = {}

EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'exports')
os.makedirs(EXPORT_DIR, exist_ok=True)


def _safe_filename_part(value: Any, max_length: int = 40) -> str:
    """Convert value to filesystem-safe token."""
    text_value = str(value) if value is not None else "NULL"
    text_value = text_value.strip() or "EMPTY"
    text_value = text_value.replace("/", "-").replace("\\", "-").replace(":", "-")
    text_value = text_value.replace("*", "").replace("?", "").replace('"', "")
    text_value = text_value.replace("<", "").replace(">", "").replace("|", "")
    text_value = "_".join(text_value.split())
    return text_value[:max_length]


def _load_json_list(raw_value: Optional[str], default_list: List[str]) -> List[str]:
    """Load a JSON list safely with fallback."""
    if not raw_value:
        return default_list
    try:
        parsed = json.loads(raw_value)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except Exception:
        pass
    return default_list


def _resolve_split_columns(settings: dict, dataframe_columns: List[str]) -> List[str]:
    """Resolve split columns based on split method and available dataframe columns."""
    split_method = str(settings.get('split_method', 'product')).lower()
    product_default = ["SEG", "DIV", "SUB_DIV", "MAJ_CAT"]
    store_default = ["ZONE", "REG", "STORE"]

    if split_method == 'store':
        preferred = _load_json_list(settings.get('store_hierarchy'), store_default)
    else:
        preferred = _load_json_list(settings.get('product_hierarchy'), product_default)

    available = set(dataframe_columns)
    return [col for col in preferred if col in available]


def _build_where_clause(filter_dict: dict) -> tuple:
    """Build WHERE clause and params from filter dict."""
    if not filter_dict:
        return "", {}
    
    conditions = []
    params = {}
    param_idx = 0
    
    for col, val in filter_dict.items():
        safe_col = col.replace("'", "''").replace("[", "").replace("]", "")
        
        if isinstance(val, dict):
            filter_type = val.get('type', 'contains')
            filter_val = val.get('filter', '')
            
            if filter_type == 'in':
                if isinstance(filter_val, list) and len(filter_val) > 0:
                    placeholders = []
                    for v in filter_val:
                        param_name = f"f{param_idx}"
                        param_idx += 1
                        placeholders.append(f":{param_name}")
                        params[param_name] = v
                    conditions.append(f"[{safe_col}] IN ({', '.join(placeholders)})")
                continue
            
            if filter_type == 'between':
                from_val = val.get('from')
                to_val = val.get('to')
                if from_val is not None and to_val is not None:
                    param_from = f"f{param_idx}"
                    param_to = f"f{param_idx + 1}"
                    param_idx += 2
                    params[param_from] = from_val
                    params[param_to] = to_val
                    conditions.append(f"[{safe_col}] BETWEEN :{param_from} AND :{param_to}")
                continue
            
            if filter_type == 'blank':
                conditions.append(f"([{safe_col}] IS NULL OR [{safe_col}] = '')")
                continue
            elif filter_type == 'notBlank':
                conditions.append(f"([{safe_col}] IS NOT NULL AND [{safe_col}] != '')")
                continue
            
            if not filter_val and filter_val != 0:
                continue
            
            param_name = f"f{param_idx}"
            param_idx += 1
            
            if filter_type == 'contains':
                conditions.append(f"[{safe_col}] LIKE :{param_name}")
                params[param_name] = f"%{filter_val}%"
            elif filter_type == 'equals':
                conditions.append(f"[{safe_col}] = :{param_name}")
                params[param_name] = filter_val
            elif filter_type == 'startsWith':
                conditions.append(f"[{safe_col}] LIKE :{param_name}")
                params[param_name] = f"{filter_val}%"
            elif filter_type == 'endsWith':
                conditions.append(f"[{safe_col}] LIKE :{param_name}")
                params[param_name] = f"%{filter_val}"
            elif filter_type == 'greaterThan':
                conditions.append(f"[{safe_col}] > :{param_name}")
                params[param_name] = filter_val
            elif filter_type == 'lessThan':
                conditions.append(f"[{safe_col}] < :{param_name}")
                params[param_name] = filter_val
        else:
            param_name = f"f{param_idx}"
            param_idx += 1
            conditions.append(f"[{safe_col}] = :{param_name}")
            params[param_name] = val
    
    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
    return where_clause, params


def _run_export_job(job_id: str):
    """Background thread function to run export job."""
    import zipfile
    
    db = SessionLocal()
    try:
        job = db.query(ExportJob).filter(ExportJob.job_id == job_id).first()
        if not job:
            return
        
        # Update status to running
        job.status = 'running'
        job.started_at = datetime.utcnow()
        db.commit()
        
        # Parse job params
        columns = json.loads(job.columns) if job.columns else None
        filters = json.loads(job.filters) if job.filters else None
        
        # Get settings
        settings_rows = db.query(ExportSettings).all()
        settings = {s.setting_key: s.setting_value for s in settings_rows}
        max_rows = int(settings.get('max_rows_per_file', 100000))
        auto_split = settings.get('enable_auto_split', 'true').lower() == 'true'
        
        # Build query
        data_engine = get_data_engine()
        col_list = "*"
        if columns:
            col_list = ", ".join([f"[{c}]" for c in columns])
        
        where_clause, params = _build_where_clause(filters)
        
        # Get total count
        count_query = f"SELECT COUNT(*) FROM [{job.table_name}] {where_clause}"
        with data_engine.connect() as conn:
            total_rows = conn.execute(text(count_query), params).scalar()
        
        job.total_rows = total_rows
        db.commit()
        
        split_method = str(settings.get('split_method', 'product')).lower()

        # Decide if split needed (by size and/or split method)
        need_split = auto_split and total_rows > max_rows
        
        if not need_split:
            # Single file export
            query = f"SELECT {col_list} FROM [{job.table_name}] {where_clause}"
            with data_engine.connect() as conn:
                df = pd.read_sql(text(query), conn, params=params)
            
            job.processed_rows = len(df)
            
            if job.format == 'xlsx':
                filename = f"{job.job_id}.xlsx"
                filepath = os.path.join(EXPORT_DIR, filename)
                with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False, sheet_name=job.table_name[:31])
            else:
                filename = f"{job.job_id}.csv"
                filepath = os.path.join(EXPORT_DIR, filename)
                df.to_csv(filepath, index=False)
            
            job.file_path = filepath
            job.file_size = os.path.getsize(filepath)
        else:
            # Split into ZIP
            filename = f"{job.job_id}.zip"
            filepath = os.path.join(EXPORT_DIR, filename)
            processed = 0

            # Load full data when split-by-method is enabled; otherwise keep paginated splitting.
            split_columns: List[str] = []
            split_groups = []

            if auto_split and split_method in ('product', 'store'):
                query = f"SELECT {col_list} FROM [{job.table_name}] {where_clause}"
                with data_engine.connect() as conn:
                    df_all = pd.read_sql(text(query), conn, params=params)

                split_columns = _resolve_split_columns(settings, list(df_all.columns))
                if split_columns:
                    need_split = True
                    for key_values, group_df in df_all.groupby(split_columns, dropna=False, sort=False):
                        if not isinstance(key_values, tuple):
                            key_values = (key_values,)
                        label_parts = [
                            f"{col}-{_safe_filename_part(val, 40)}"
                            for col, val in zip(split_columns, key_values)
                        ]
                        group_label = "_".join(label_parts)[:140]
                        split_groups.append((group_label, group_df.reset_index(drop=True)))

            with zipfile.ZipFile(filepath, 'w', zipfile.ZIP_DEFLATED) as zf:
                if split_groups:
                    for group_label, group_df in split_groups:
                        group_rows = len(group_df)
                        chunks = max(1, (group_rows + max_rows - 1) // max_rows)

                        for chunk_idx in range(chunks):
                            start = chunk_idx * max_rows
                            end = min(start + max_rows, group_rows)
                            df_part = group_df.iloc[start:end]

                            processed += len(df_part)
                            job.processed_rows = processed
                            db.commit()

                            suffix = f"{group_label}"
                            if chunks > 1:
                                suffix = f"{suffix}_part{chunk_idx + 1}"
                            base_name = _safe_filename_part(f"{job.table_name}_{suffix}", 180)

                            if job.format == 'xlsx':
                                part_buffer = io.BytesIO()
                                with pd.ExcelWriter(part_buffer, engine='openpyxl') as writer:
                                    df_part.to_excel(writer, index=False, sheet_name=job.table_name[:31])
                                part_buffer.seek(0)
                                zf.writestr(f"{base_name}.xlsx", part_buffer.getvalue())
                            else:
                                part_buffer = io.StringIO()
                                df_part.to_csv(part_buffer, index=False)
                                zf.writestr(f"{base_name}.csv", part_buffer.getvalue().encode('utf-8-sig'))
                else:
                    num_files = (total_rows + max_rows - 1) // max_rows
                    for part in range(num_files):
                        offset = part * max_rows

                        paginated_query = f"""
                            SELECT {col_list} FROM [{job.table_name}] {where_clause}
                            ORDER BY (SELECT NULL)
                            OFFSET {offset} ROWS FETCH NEXT {max_rows} ROWS ONLY
                        """

                        with data_engine.connect() as conn:
                            df_part = pd.read_sql(text(paginated_query), conn, params=params)

                        processed += len(df_part)
                        job.processed_rows = processed
                        db.commit()

                        part_num = part + 1

                        if job.format == 'xlsx':
                            part_buffer = io.BytesIO()
                            with pd.ExcelWriter(part_buffer, engine='openpyxl') as writer:
                                df_part.to_excel(writer, index=False, sheet_name=job.table_name[:31])
                            part_buffer.seek(0)
                            zf.writestr(f"{job.table_name}_part{part_num}.xlsx", part_buffer.getvalue())
                        else:
                            part_buffer = io.StringIO()
                            df_part.to_csv(part_buffer, index=False)
                            zf.writestr(f"{job.table_name}_part{part_num}.csv", part_buffer.getvalue().encode('utf-8-sig'))
            
            job.file_path = filepath
            job.file_size = os.path.getsize(filepath)
        
        job.status = 'completed'
        job.completed_at = datetime.utcnow()
        db.commit()
        
    except Exception as e:
        job = db.query(ExportJob).filter(ExportJob.job_id == job_id).first()
        if job:
            job.status = 'failed'
            job.error_message = str(e)
            job.completed_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()
        if job_id in _running_jobs:
            del _running_jobs[job_id]


def create_export_job(
    db: Session,
    table_name: str,
    format: str,
    columns: list,
    filters: dict,
    username: str
) -> ExportJob:
    """Create and start a background export job."""
    job_id = f"EXP-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
    
    job = ExportJob(
        job_id=job_id,
        table_name=table_name,
        status='pending',
        format=format,
        columns=json.dumps(columns) if columns else None,
        filters=json.dumps(filters) if filters else None,
        created_by=username
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    
    # Start background thread
    thread = threading.Thread(target=_run_export_job, args=(job_id,), daemon=True)
    thread.start()
    _running_jobs[job_id] = {'thread': thread, 'started': datetime.utcnow()}
    
    return job


def get_user_jobs(db: Session, username: str, limit: int = 20) -> list:
    """Get recent export jobs for a user."""
    jobs = db.query(ExportJob).filter(
        ExportJob.created_by == username
    ).order_by(ExportJob.created_at.desc()).limit(limit).all()
    
    return [{
        'job_id': j.job_id,
        'table_name': j.table_name,
        'status': j.status,
        'format': j.format,
        'total_rows': j.total_rows,
        'processed_rows': j.processed_rows,
        'file_size': j.file_size,
        'error_message': j.error_message,
        'created_at': j.created_at.isoformat() if j.created_at else None,
        'started_at': j.started_at.isoformat() if j.started_at else None,
        'completed_at': j.completed_at.isoformat() if j.completed_at else None,
        'downloaded': j.downloaded
    } for j in jobs]


def get_job_status(db: Session, job_id: str) -> Optional[dict]:
    """Get status of a specific job."""
    job = db.query(ExportJob).filter(ExportJob.job_id == job_id).first()
    if not job:
        return None
    
    return {
        'job_id': job.job_id,
        'table_name': job.table_name,
        'status': job.status,
        'format': job.format,
        'total_rows': job.total_rows,
        'processed_rows': job.processed_rows,
        'progress': round(job.processed_rows / job.total_rows * 100, 1) if job.total_rows else 0,
        'file_size': job.file_size,
        'error_message': job.error_message,
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'completed_at': job.completed_at.isoformat() if job.completed_at else None
    }


def get_job_file(db: Session, job_id: str) -> Optional[tuple]:
    """Get file path and name for download."""
    job = db.query(ExportJob).filter(ExportJob.job_id == job_id).first()
    if not job or job.status != 'completed' or not job.file_path:
        return None
    
    if not os.path.exists(job.file_path):
        return None
    
    # Increment download count
    job.downloaded += 1
    db.commit()
    
    # Determine filename
    ext = os.path.splitext(job.file_path)[1]
    filename = f"{job.table_name}{ext}"
    
    return job.file_path, filename


def delete_job(db: Session, job_id: str, username: str) -> bool:
    """Delete a job and its file."""
    job = db.query(ExportJob).filter(
        ExportJob.job_id == job_id,
        ExportJob.created_by == username
    ).first()
    
    if not job:
        return False
    
    # Don't delete running jobs
    if job.status == 'running':
        return False
    
    # Delete file if exists
    if job.file_path and os.path.exists(job.file_path):
        try:
            os.remove(job.file_path)
        except:
            pass
    
    db.delete(job)
    db.commit()
    return True
