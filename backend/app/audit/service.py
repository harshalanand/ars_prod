"""
Audit Logging Service - Tracks all data modifications
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from sqlalchemy.orm import Session
from loguru import logger

from app.models.audit import AuditLog


class AuditService:
    """Centralized audit logging for all data operations."""

    def __init__(self, db: Session):
        self.db = db

    def log(
        self,
        table_name: str,
        action_type: str,
        changed_by: str,
        record_primary_key: Optional[str] = None,
        old_data: Optional[Dict] = None,
        new_data: Optional[Dict] = None,
        changed_columns: Optional[List[str]] = None,
        source: str = "API",
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        session_id: Optional[str] = None,
        batch_id: Optional[str] = None,
        duration_ms: Optional[int] = None,
        row_count: int = 1,
        notes: Optional[str] = None,
    ) -> AuditLog:
        """Create a single audit log entry."""
        try:
            entry = AuditLog(
                table_name=table_name,
                action_type=action_type,
                record_primary_key=str(record_primary_key) if record_primary_key else None,
                old_data=json.dumps(old_data, default=str) if old_data else None,
                new_data=json.dumps(new_data, default=str) if new_data else None,
                changed_columns=json.dumps(changed_columns) if changed_columns else None,
                changed_by=changed_by,
                changed_at=datetime.now(timezone.utc),
                source=source,
                ip_address=ip_address,
                user_agent=user_agent,
                session_id=session_id,
                batch_id=batch_id,
                duration_ms=duration_ms,
                row_count=row_count,
                notes=notes,
            )
            self.db.add(entry)
            self.db.commit()  # Commit the audit entry to the database
            return entry
        except Exception as e:
            logger.error(f"Audit log failed: {e}")
            self.db.rollback()
            # Audit failure should NOT block the operation
            return None

    def log_insert(
        self,
        table_name: str,
        changed_by: str,
        record_pk: str,
        new_data: Dict,
        **kwargs,
    ):
        return self.log(
            table_name=table_name,
            action_type="INSERT",
            changed_by=changed_by,
            record_primary_key=record_pk,
            new_data=new_data,
            **kwargs,
        )

    def log_update(
        self,
        table_name: str,
        changed_by: str,
        record_pk: str,
        old_data: Dict,
        new_data: Dict,
        changed_columns: List[str],
        **kwargs,
    ):
        return self.log(
            table_name=table_name,
            action_type="UPDATE",
            changed_by=changed_by,
            record_primary_key=record_pk,
            old_data=old_data,
            new_data=new_data,
            changed_columns=changed_columns,
            **kwargs,
        )

    def log_delete(
        self,
        table_name: str,
        changed_by: str,
        record_pk: str,
        old_data: Dict,
        **kwargs,
    ):
        return self.log(
            table_name=table_name,
            action_type="DELETE",
            changed_by=changed_by,
            record_primary_key=record_pk,
            old_data=old_data,
            **kwargs,
        )

    def log_bulk_upsert(
        self,
        table_name: str,
        changed_by: str,
        row_count: int,
        batch_id: Optional[str] = None,
        duration_ms: Optional[int] = None,
        notes: Optional[str] = None,
        source: str = "UPLOAD",
        **kwargs,
    ):
        if not batch_id:
            batch_id = str(uuid.uuid4())[:12]
        return self.log(
            table_name=table_name,
            action_type="BULK_UPLOAD",
            changed_by=changed_by,
            source=source,
            batch_id=batch_id,
            row_count=row_count,
            duration_ms=duration_ms,
            notes=notes,
            **kwargs,
        )

    def log_schema_change(
        self,
        table_name: str,
        changed_by: str,
        action: str,  # CREATE_TABLE, ALTER_TABLE, DROP_TABLE, ADD_COLUMN, etc.
        details: Dict,
        **kwargs,
    ):
        return self.log(
            table_name=table_name,
            action_type="SCHEMA_CHANGE",
            changed_by=changed_by,
            new_data=details,
            notes=action,
            **kwargs,
        )

    @staticmethod
    def diff_records(old: Dict, new: Dict) -> tuple:
        """
        Compare old and new record dicts.
        Returns: (changed_columns, old_values, new_values)
        """
        changed_cols = []
        old_vals = {}
        new_vals = {}

        all_keys = set(list(old.keys()) + list(new.keys()))
        for key in all_keys:
            old_val = old.get(key)
            new_val = new.get(key)
            if str(old_val) != str(new_val):
                changed_cols.append(key)
                old_vals[key] = old_val
                new_vals[key] = new_val

        return changed_cols, old_vals, new_vals


def get_audit_service(db: Session) -> AuditService:
    """Factory for AuditService."""
    return AuditService(db)


def get_client_ip(request) -> str:
    """Extract client IP from request."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
