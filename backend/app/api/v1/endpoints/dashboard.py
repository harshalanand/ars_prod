"""
Dashboard API endpoints - Stats and overview
"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func, text

from app.database.session import get_db, get_data_db
from app.security.dependencies import get_current_user
from app.models.rbac import User
from app.models.audit import AuditLog, UploadJob, ExportJob
from app.schemas.common import APIResponse

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/stats")
def get_dashboard_stats(
    db: Session = Depends(get_db),
    data_db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user)
):
    """Get overall dashboard statistics"""
    try:
        now = datetime.utcnow()
        last_24h = now - timedelta(hours=24)
        
        # Audit log stats (last 24h)
        audit_count = db.query(func.count(AuditLog.id)).filter(
            AuditLog.changed_at >= last_24h
        ).scalar() or 0
        
        # Upload job stats
        upload_stats = db.query(
            func.count(UploadJob.id).label('total'),
            func.sum(func.case((UploadJob.status == 'completed', 1), else_=0)).label('completed'),
            func.sum(func.case((UploadJob.status == 'running', 1), else_=0)).label('running'),
            func.sum(func.case((UploadJob.status == 'failed', 1), else_=0)).label('failed'),
        ).first()
        
        # Export job stats
        export_stats = db.query(
            func.count(ExportJob.id).label('total'),
            func.sum(func.case((ExportJob.status == 'completed', 1), else_=0)).label('completed'),
            func.sum(func.case((ExportJob.status == 'running', 1), else_=0)).label('running'),
            func.sum(func.case((ExportJob.status == 'failed', 1), else_=0)).label('failed'),
        ).first()
        
        # Get table count and total rows from data database
        table_count = 0
        total_rows = 0
        try:
            tables_result = data_db.execute(text("""
                SELECT COUNT(*) as cnt FROM INFORMATION_SCHEMA.TABLES 
                WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA = 'dbo'
            """))
            table_count = tables_result.scalar() or 0
            
            # Get approximate row counts from sys.partitions (very fast)
            rows_result = data_db.execute(text("""
                SELECT SUM(CAST(p.[rows] AS BIGINT)) as total_rows
                FROM sys.tables t
                INNER JOIN sys.partitions p ON t.object_id = p.object_id
                WHERE p.index_id IN (0, 1)  -- heap or clustered index
                AND t.is_ms_shipped = 0
            """))
            total_rows = rows_result.scalar() or 0
        except Exception as e:
            pass  # If data DB query fails, just use 0
        
        return APIResponse(data={
            "total_audit_logs": audit_count,
            "total_tables": table_count,
            "total_rows": total_rows,
            "upload_jobs": {
                "total": upload_stats.total or 0,
                "completed": int(upload_stats.completed or 0),
                "running": int(upload_stats.running or 0),
                "failed": int(upload_stats.failed or 0),
            },
            "export_jobs": {
                "total": export_stats.total or 0,
                "completed": int(export_stats.completed or 0),
                "running": int(export_stats.running or 0),
                "failed": int(export_stats.failed or 0),
            },
            "timestamp": now.isoformat(),
        })
    except Exception as e:
        # Return partial data on error
        return APIResponse(data={
            "total_audit_logs": 0,
            "total_tables": 0,
            "total_rows": 0,
            "upload_jobs": {"total": 0, "completed": 0, "running": 0, "failed": 0},
            "export_jobs": {"total": 0, "completed": 0, "running": 0, "failed": 0},
            "error": str(e),
        })
