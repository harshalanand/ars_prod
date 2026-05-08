"""
Audit Log Viewer API
"""
from datetime import datetime
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database.session import get_db
from app.schemas.common import APIResponse
from app.security.dependencies import RequirePermissions, get_current_user
from app.models.rbac import User
from app.models.audit import AuditLog, DataChangeLog

router = APIRouter(prefix="/audit", tags=["Audit Logs"])


@router.get(
    "",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_AUDIT_READ"]))],
)
async def list_audit_logs(
    table_name: str = Query(None),
    action_type: str = Query(None),
    changed_by: str = Query(None),
    batch_id: str = Query(None),
    date_from: datetime = Query(None),
    date_to: datetime = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Query audit logs with filters."""
    query = db.query(AuditLog)

    if table_name:
        query = query.filter(AuditLog.table_name == table_name)
    if action_type:
        query = query.filter(AuditLog.action_type == action_type)
    if changed_by:
        query = query.filter(AuditLog.changed_by == changed_by)
    if batch_id:
        query = query.filter(AuditLog.batch_id == batch_id)
    if date_from:
        query = query.filter(AuditLog.changed_at >= date_from)
    if date_to:
        query = query.filter(AuditLog.changed_at <= date_to)

    total = query.count()
    logs = (
        query.order_by(AuditLog.changed_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return APIResponse(data={
        "logs": [
            {
                "id": log.id,
                "table_name": log.table_name,
                "action_type": log.action_type,
                "record_primary_key": log.record_primary_key,
                "changed_columns": log.changed_columns,
                "changed_by": log.changed_by,
                "changed_at": log.changed_at.isoformat() if log.changed_at else None,
                "source": log.source,
                "ip_address": log.ip_address,
                "batch_id": log.batch_id,
                "row_count": log.row_count,
                "notes": log.notes,
            }
            for log in logs
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.get(
    "/changes",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_AUDIT_READ"]))],
)
async def list_data_changes(
    table_name: str = Query(None),
    action_type: str = Query(None),
    record_key: str = Query(None),
    column_name: str = Query(None),
    changed_by: str = Query(None),
    batch_id: str = Query(None),
    date_from: datetime = Query(None),
    date_to: datetime = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """
    Query detailed row/column level changes from data_change_log.
    This provides granular change history for each cell.
    """
    query = db.query(DataChangeLog)

    if table_name:
        query = query.filter(DataChangeLog.table_name == table_name)
    if action_type:
        query = query.filter(DataChangeLog.action_type == action_type)
    if record_key:
        query = query.filter(DataChangeLog.record_key.contains(record_key))
    if column_name:
        query = query.filter(DataChangeLog.column_name == column_name)
    if changed_by:
        query = query.filter(DataChangeLog.changed_by == changed_by)
    if batch_id:
        query = query.filter(DataChangeLog.batch_id == batch_id)
    if date_from:
        query = query.filter(DataChangeLog.changed_at >= date_from)
    if date_to:
        query = query.filter(DataChangeLog.changed_at <= date_to)

    total = query.count()
    changes = (
        query.order_by(DataChangeLog.changed_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return APIResponse(data={
        "changes": [
            {
                "id": c.id,
                "audit_log_id": c.audit_log_id,
                "table_name": c.table_name,
                "action_type": c.action_type,
                "record_key": c.record_key,
                "column_name": c.column_name,
                "old_value": c.old_value,
                "new_value": c.new_value,
                "data_type": c.data_type,
                "changed_by": c.changed_by,
                "changed_at": c.changed_at.isoformat() if c.changed_at else None,
                "source": c.source,
                "batch_id": c.batch_id,
                "row_index": c.row_index,
            }
            for c in changes
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.get(
    "/changes/record/{table_name}",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_AUDIT_READ"]))],
)
async def get_record_history(
    table_name: str,
    record_key: str = Query(..., description="Primary key value(s) as JSON"),
    db: Session = Depends(get_db),
):
    """
    Get full change history for a specific record.
    Useful for tracking all changes to a particular row over time.
    """
    changes = (
        db.query(DataChangeLog)
        .filter(DataChangeLog.table_name == table_name)
        .filter(DataChangeLog.record_key == record_key)
        .order_by(DataChangeLog.changed_at.desc())
        .all()
    )

    return APIResponse(data={
        "record_key": record_key,
        "table_name": table_name,
        "change_count": len(changes),
        "changes": [
            {
                "id": c.id,
                "action_type": c.action_type,
                "column_name": c.column_name,
                "old_value": c.old_value,
                "new_value": c.new_value,
                "data_type": c.data_type,
                "changed_by": c.changed_by,
                "changed_at": c.changed_at.isoformat() if c.changed_at else None,
                "source": c.source,
            }
            for c in changes
        ],
    })


@router.get(
    "/changes/batch/{batch_id}",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_AUDIT_READ"]))],
)
async def get_batch_changes(
    batch_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """
    Get all changes for a specific batch/upload job.
    Returns column-level changes made during a bulk upload.
    """
    query = db.query(DataChangeLog).filter(DataChangeLog.batch_id == batch_id)
    
    total = query.count()
    changes = (
        query.order_by(DataChangeLog.row_index, DataChangeLog.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    
    # Get summary stats
    from sqlalchemy import func
    summary = db.query(
        func.count(DataChangeLog.id).label('total_changes'),
        func.count(func.distinct(DataChangeLog.record_key)).label('unique_records'),
    ).filter(DataChangeLog.batch_id == batch_id).first()

    return APIResponse(data={
        "batch_id": batch_id,
        "total_changes": summary.total_changes if summary else 0,
        "unique_records": summary.unique_records if summary else 0,
        "page": page,
        "page_size": page_size,
        "changes": [
            {
                "id": c.id,
                "table_name": c.table_name,
                "action_type": c.action_type,
                "record_key": c.record_key,
                "column_name": c.column_name,
                "old_value": c.old_value,
                "new_value": c.new_value,
                "data_type": c.data_type,
                "changed_by": c.changed_by,
                "changed_at": c.changed_at.isoformat() if c.changed_at else None,
                "row_index": c.row_index,
            }
            for c in changes
        ],
    })


# IMPORTANT: This parameterized route must be AFTER all /changes/* routes
# to prevent "changes" from being matched as a log_id
@router.get(
    "/{log_id}",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_AUDIT_READ"]))],
)
async def get_audit_detail(log_id: int, db: Session = Depends(get_db)):
    """Get full audit log entry with old/new data."""
    log = db.query(AuditLog).filter(AuditLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Audit log not found")

    return APIResponse(data={
        "id": log.id,
        "table_name": log.table_name,
        "action_type": log.action_type,
        "record_primary_key": log.record_primary_key,
        "old_data": log.old_data,
        "new_data": log.new_data,
        "changed_columns": log.changed_columns,
        "changed_by": log.changed_by,
        "changed_at": log.changed_at.isoformat() if log.changed_at else None,
        "source": log.source,
        "ip_address": log.ip_address,
        "user_agent": log.user_agent,
        "session_id": log.session_id,
        "batch_id": log.batch_id,
        "duration_ms": log.duration_ms,
        "row_count": log.row_count,
        "notes": log.notes,
    })


