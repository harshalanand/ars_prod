"""
Dynamic Table Management API Endpoints
"""
import io
import os
import logging
import threading
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Body
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

from app.database.session import get_db, SessionLocal
from app.schemas.table_mgmt import CreateTableRequest, AlterTableRequest
from app.schemas.common import APIResponse
from app.services.table_mgmt_service import TableManagementService
from app.security.dependencies import get_current_user, RequirePermissions
from app.models.rbac import User

router = APIRouter(prefix="/tables", tags=["Table Management"])


# ============================================================================
# In-memory progress map for long-running truncate jobs.
# We don't persist these — a backend restart cancels in-flight truncates,
# but TRUNCATE TABLE itself is autocommit on the SQL side so the data is
# already gone by the time the client polls.
# ============================================================================
_truncate_jobs: Dict[str, Dict[str, Any]] = {}
_truncate_lock = threading.Lock()
_TRUNCATE_TTL_SEC = 600  # forget completed/failed jobs after 10 min


def _truncate_progress_set(job_id: str, **fields) -> None:
    """Thread-safe update of an in-memory truncate-progress record."""
    with _truncate_lock:
        rec = _truncate_jobs.setdefault(job_id, {})
        rec.update(fields)
        rec["updated_at"] = time.time()


def _truncate_progress_get(job_id: str) -> Optional[Dict[str, Any]]:
    with _truncate_lock:
        rec = _truncate_jobs.get(job_id)
        return dict(rec) if rec else None


def _truncate_progress_gc() -> None:
    """Drop records that have been finished and idle for a while."""
    now = time.time()
    with _truncate_lock:
        for jid in list(_truncate_jobs.keys()):
            rec = _truncate_jobs[jid]
            if rec.get("status") in ("done", "failed") and (
                now - rec.get("updated_at", now) > _TRUNCATE_TTL_SEC
            ):
                _truncate_jobs.pop(jid, None)


# ============================================================================
# Create Table
# ============================================================================

@router.post(
    "",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["TABLE_CREATE"]))],
)
async def create_table(
    body: CreateTableRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new database table from UI."""
    try:
        service = TableManagementService(db)
        result = service.create_table(
            table_name=body.table_name,
            columns=[c.model_dump() for c in body.columns],
            display_name=body.display_name,
            description=body.description,
            module=body.module,
            created_by=current_user.username,
        )
        return APIResponse(data=result, message=f"Table '{body.table_name}' created successfully")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================================
# Alter Table
# ============================================================================

@router.put(
    "/{table_name}/alter",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["TABLE_ALTER"]))],
)
async def alter_table(
    table_name: str,
    body: AlterTableRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Alter table: add/drop/rename/alter columns."""
    try:
        service = TableManagementService(db)
        
        # Handle alter_column action (change data type)
        if body.action == 'alter_column' and body.column_name and body.new_type:
            result = service.alter_column_type(
                table_name=table_name,
                column_name=body.column_name,
                new_type=body.new_type,
                changed_by=current_user.username,
            )
            return APIResponse(data=result, message="Column type changed successfully")
        
        # Handle drop_column action
        if body.action == 'drop_column' and body.column_name:
            result = service.alter_table(
                table_name=table_name,
                drop_columns=[body.column_name],
                changed_by=current_user.username,
            )
            return APIResponse(data=result, message="Column dropped successfully")
        
        # Handle add_column action
        if body.action == 'add_column' and body.column_name and body.data_type:
            # Build column definition
            col_def = {
                "column_name": body.column_name,
                "data_type": body.data_type,
                "is_nullable": body.nullable if body.nullable is not None else True,
            }
            result = service.alter_table(
                table_name=table_name,
                add_columns=[col_def],
                changed_by=current_user.username,
            )
            return APIResponse(data=result, message="Column added successfully")
        
        # Handle rename_column action
        if body.action == 'rename_column' and body.column_name and body.new_name:
            result = service.alter_table(
                table_name=table_name,
                rename_columns={body.column_name: body.new_name},
                changed_by=current_user.username,
            )
            return APIResponse(data=result, message="Column renamed successfully")
        
        # Original alter table logic (batch operations)
        result = service.alter_table(
            table_name=table_name,
            add_columns=[c.model_dump() for c in body.add_columns] if body.add_columns else None,
            drop_columns=body.drop_columns,
            rename_columns=body.rename_columns,
            changed_by=current_user.username,
        )
        return APIResponse(data=result, message="Table altered successfully")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================================
# Soft Delete Table
# ============================================================================

@router.delete(
    "/{table_name}",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["TABLE_DELETE"]))],
)
async def delete_table(
    table_name: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Soft-delete a table (marks inactive, does NOT drop from DB)."""
    try:
        service = TableManagementService(db)
        result = service.soft_delete_table(table_name, deleted_by=current_user.username)
        return APIResponse(data=result, message="Table soft-deleted")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================================
# Export Settings API (static routes before dynamic)
# ============================================================================

@router.get("/export/settings", response_model=APIResponse)
async def get_export_settings(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Get all export settings."""
    from app.models.audit import ExportSettings
    settings = db.query(ExportSettings).all()
    return APIResponse(data=[{
        "key": s.setting_key,
        "value": s.setting_value,
        "description": s.description
    } for s in settings])


async def _update_export_settings_logic(settings: dict, db: Session):
    """Update export settings logic."""
    from app.models.audit import ExportSettings
    
    for key, value in settings.items():
        setting = db.query(ExportSettings).filter(ExportSettings.setting_key == key).first()
        if setting:
            setting.setting_value = str(value) if not isinstance(value, str) else value
        else:
            db.add(ExportSettings(setting_key=key, setting_value=str(value)))
    
    db.commit()
    return APIResponse(data=settings, message="Settings updated")


@router.put("/export/settings", response_model=APIResponse)
async def update_export_settings_put(
    settings: dict,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Update export settings (PUT)."""
    return await _update_export_settings_logic(settings, db)


@router.post("/export/settings", response_model=APIResponse)
async def update_export_settings_post(
    settings: dict,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Update export settings (POST)."""
    return await _update_export_settings_logic(settings, db)


# ============================================================================
# Table Permissions API (which tables can be edited/uploaded/exported)
# ============================================================================

@router.get("/permissions", response_model=APIResponse)
async def list_table_permissions(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Get all table permissions."""
    from app.models.audit import TablePermission
    perms = db.query(TablePermission).order_by(TablePermission.table_name).all()
    return APIResponse(data=[{
        "table_name": p.table_name,
        "can_view": bool(p.can_view),
        "can_edit": bool(p.can_edit),
        "can_upload": bool(p.can_upload),
        "can_export": bool(p.can_export),
        "can_delete": bool(p.can_delete),
    } for p in perms])


@router.get("/permissions/allowed", response_model=APIResponse)
async def get_allowed_tables(
    action: str = Query(..., description="Action: view, edit, upload, export, delete"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Get list of tables allowed for a specific action."""
    from app.models.audit import TablePermission
    from sqlalchemy import text
    
    action_map = {
        "view": "can_view",
        "edit": "can_edit", 
        "upload": "can_upload",
        "export": "can_export",
        "delete": "can_delete",
    }
    
    if action not in action_map:
        raise HTTPException(status_code=400, detail=f"Invalid action. Allowed: {list(action_map.keys())}")
    
    col = action_map[action]
    perms = db.query(TablePermission).filter(getattr(TablePermission, col) == 1).all()
    return APIResponse(data=[p.table_name for p in perms])


@router.post("/permissions", response_model=APIResponse)
async def save_table_permissions(
    permissions: List[Dict[str, Any]] = Body(...),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Save table permissions."""
    from app.models.audit import TablePermission
    from datetime import datetime
    
    for perm in permissions:
        existing = db.query(TablePermission).filter(
            TablePermission.table_name == perm["table_name"]
        ).first()
        
        if existing:
            existing.can_view = perm.get("can_view", True)
            existing.can_edit = perm.get("can_edit", False)
            existing.can_upload = perm.get("can_upload", False)
            existing.can_export = perm.get("can_export", False)
            existing.can_delete = perm.get("can_delete", False)
            existing.updated_at = datetime.utcnow()
        else:
            db.add(TablePermission(
                table_name=perm["table_name"],
                can_view=perm.get("can_view", True),
                can_edit=perm.get("can_edit", False),
                can_upload=perm.get("can_upload", False),
                can_export=perm.get("can_export", False),
                can_delete=perm.get("can_delete", False),
            ))
    
    db.commit()
    return APIResponse(message="Permissions saved")


# ============================================================================
# Export Jobs API (background processing)
# ============================================================================

@router.get("/export/jobs", response_model=APIResponse)
async def list_export_jobs(
    limit: int = Query(20, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List user's export jobs."""
    from app.services.export_job_service import get_user_jobs
    jobs = get_user_jobs(db, current_user.username, limit)
    return APIResponse(data=jobs)


@router.get("/export/jobs/{job_id}", response_model=APIResponse)
async def get_export_job_status(
    job_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Get status of an export job."""
    from app.services.export_job_service import get_job_status
    status = get_job_status(db, job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Job not found")
    return APIResponse(data=status)


@router.get("/export/jobs/{job_id}/download")
async def download_export_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Download completed export job file. File is auto-deleted after download."""
    from app.services.export_job_service import get_job_file
    from fastapi.responses import FileResponse

    result = get_job_file(db, job_id)
    if not result:
        raise HTTPException(status_code=404, detail="File not found or job not completed")

    filepath, filename = result

    def _cleanup_file(path: str):
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info(f"Auto-deleted export file after download: {path}")
        except Exception as e:
            logger.warning(f"Failed to auto-delete export file: {e}")

    background_tasks.add_task(_cleanup_file, filepath)

    return FileResponse(
        path=filepath,
        filename=filename,
        media_type='application/octet-stream'
    )


@router.delete("/export/jobs/{job_id}", response_model=APIResponse)
async def delete_export_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete an export job."""
    from app.services.export_job_service import delete_job
    success = delete_job(db, job_id, current_user.username)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot delete job (not found or running)")
    return APIResponse(message="Job deleted")


@router.post("/export/jobs/start", response_model=APIResponse)
async def start_export_job(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Start a background export job."""
    from app.services.export_job_service import create_export_job
    
    table_name = body.get('table_name')
    format = body.get('format', 'xlsx')
    columns = body.get('columns', [])
    filters = body.get('filters', {})
    
    if not table_name:
        raise HTTPException(status_code=400, detail="table_name required")
    
    job = create_export_job(db, table_name, format, columns, filters, current_user.username)
    return APIResponse(data={
        'job_id': job.job_id,
        'status': job.status,
        'message': 'Export job started'
    })


# ============================================================================
# Table Metadata & Schema
# ============================================================================

@router.get("/{table_name}/schema", response_model=APIResponse)
async def get_table_schema(
    table_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get table schema, metadata, and editable columns for current user."""
    from app.security.dependencies import get_editable_columns
    
    try:
        service = TableManagementService(db)
        result = service.get_table_metadata(table_name)
        
        # Add editable columns for current user
        all_column_names = [c["column_name"] for c in result.get("columns", [])]
        editable_columns = get_editable_columns(db, table_name, current_user.role_codes, all_column_names)
        result["editable_columns"] = editable_columns
        
        # Mark columns as editable in the column list
        for col in result.get("columns", []):
            col["is_editable"] = col["column_name"] in editable_columns and not col.get("is_primary_key")
        
        return APIResponse(data=result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{table_name}/row-count", response_model=APIResponse)
async def get_table_row_count(
    table_name: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Get row count for a table (exact count using COUNT(*))."""
    from app.database.session import get_data_engine
    from sqlalchemy import text
    
    try:
        engine = get_data_engine()
        with engine.connect() as conn:
            # Use COUNT(*) for accurate count - safe table name escaping
            safe_table = table_name.replace("'", "''").replace("[", "").replace("]", "")
            result = conn.execute(text(f"SELECT COUNT(*) FROM [{safe_table}] WITH (NOLOCK)"))
            row = result.fetchone()
            count = row[0] if row else 0
            
        return APIResponse(data={"table_name": table_name, "row_count": count})
    except Exception as e:
        # Fallback: try approximate count from sys tables
        try:
            engine = get_data_engine()
            with engine.connect() as conn:
                result = conn.execute(text("""
                    SELECT SUM(p.[rows]) as row_count
                    FROM sys.tables t
                    INNER JOIN sys.partitions p ON t.object_id = p.object_id
                    WHERE t.name = :table_name AND p.index_id IN (0, 1)
                """), {"table_name": table_name})
                row = result.fetchone()
                count = row[0] if row and row[0] else 0
            return APIResponse(data={"table_name": table_name, "row_count": count})
        except:
            raise HTTPException(status_code=500, detail=str(e))


@router.get("/{table_name}/settings", response_model=APIResponse)
async def get_table_settings(
    table_name: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Get table-specific settings (heavy table config, etc.)."""
    import json
    try:
        from app.models.rls import TableSettings
        
        settings = db.query(TableSettings).filter(TableSettings.table_name == table_name).first()
        if settings:
            filter_cols = []
            if settings.filter_columns:
                try:
                    filter_cols = json.loads(settings.filter_columns)
                except:
                    pass
            return APIResponse(data={
                "table_name": table_name,
                "is_heavy": settings.is_heavy,
                "row_threshold": settings.row_threshold,
                "require_filter": settings.require_filter,
                "visible_in_editor": getattr(settings, 'visible_in_editor', True),
                "filter_columns": filter_cols,
            })
        else:
            return APIResponse(data={
                "table_name": table_name,
                "is_heavy": False,
                "row_threshold": 100000,
                "require_filter": False,
                "visible_in_editor": True,
                "filter_columns": [],
            })
    except Exception:
        # If table_settings table doesn't exist, return defaults
        return APIResponse(data={
            "table_name": table_name,
            "is_heavy": False,
            "row_threshold": 100000,
            "require_filter": False,
            "visible_in_editor": True,
            "filter_columns": [],
        })


@router.get("/{table_name}/distinct/{column_name}", response_model=APIResponse)
async def get_distinct_values(
    table_name: str,
    column_name: str,
    filters: str = Query(None, description="JSON encoded filter object for cascading (supports arrays for IN clause)"),
    search: str = Query(None, description="Search term for the column values"),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """
    Get distinct values for a column with cascading filter support.
    Supports multi-select filters (arrays become IN clauses).
    Used for filter dropdowns in Data Editor.
    """
    import json
    from app.database.session import get_data_engine
    from sqlalchemy import text
    
    try:
        engine = get_data_engine()
        safe_table = table_name.replace("'", "''").replace("[", "").replace("]", "")
        safe_column = column_name.replace("'", "''").replace("[", "").replace("]", "")
        
        # Build WHERE clause from existing filters (for cascading)
        where_parts = ["1=1"]
        params = {}
        
        if filters:
            try:
                filter_dict = json.loads(filters)
                param_idx = 0
                for col, filter_info in filter_dict.items():
                    # Skip the current column (we're getting distinct for it)
                    if col == column_name:
                        continue
                    
                    safe_col = col.replace("'", "''").replace("[", "").replace("]", "")
                    
                    # Support array values (multi-select -> IN clause)
                    if isinstance(filter_info, list) and len(filter_info) > 0:
                        placeholders = []
                        for val in filter_info:
                            param_name = f"p{param_idx}"
                            param_idx += 1
                            placeholders.append(f":{param_name}")
                            params[param_name] = val
                        where_parts.append(f"[{safe_col}] IN ({', '.join(placeholders)})")
                    elif isinstance(filter_info, dict):
                        filter_type = filter_info.get("type", "equals")
                        filter_val = filter_info.get("filter", "")
                        # Support array in filter value
                        if isinstance(filter_val, list) and len(filter_val) > 0:
                            placeholders = []
                            for val in filter_val:
                                param_name = f"p{param_idx}"
                                param_idx += 1
                                placeholders.append(f":{param_name}")
                                params[param_name] = val
                            where_parts.append(f"[{safe_col}] IN ({', '.join(placeholders)})")
                        elif filter_val:
                            param_name = f"p{param_idx}"
                            param_idx += 1
                            
                            if filter_type == "equals":
                                where_parts.append(f"[{safe_col}] = :{param_name}")
                                params[param_name] = filter_val
                            elif filter_type == "contains":
                                where_parts.append(f"[{safe_col}] LIKE :{param_name}")
                                params[param_name] = f"%{filter_val}%"
                            elif filter_type == "startsWith":
                                where_parts.append(f"[{safe_col}] LIKE :{param_name}")
                                params[param_name] = f"{filter_val}%"
                            elif filter_type == "endsWith":
                                where_parts.append(f"[{safe_col}] LIKE :{param_name}")
                                params[param_name] = f"%{filter_val}"
            except json.JSONDecodeError:
                pass
        
        # Add search filter for the column itself (case-insensitive)
        if search:
            where_parts.append(f"LOWER(CAST([{safe_column}] AS NVARCHAR(MAX))) LIKE LOWER(:search)")
            params["search"] = f"%{search}%"
        
        where_clause = " AND ".join(where_parts)
        
        # Query distinct values
        sql = f"""
            SELECT TOP {limit} [{safe_column}] as value, COUNT(*) as count
            FROM [{safe_table}] WITH (NOLOCK)
            WHERE {where_clause} AND [{safe_column}] IS NOT NULL AND CAST([{safe_column}] AS NVARCHAR(MAX)) != ''
            GROUP BY [{safe_column}]
            ORDER BY COUNT(*) DESC
        """
        
        with engine.connect() as conn:
            result = conn.execute(text(sql), params)
            values = [{"value": str(row[0]), "count": row[1]} for row in result.fetchall()]
        
        return APIResponse(data={
            "column_name": column_name,
            "values": values,
            "has_more": len(values) >= limit
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("", response_model=APIResponse)
async def list_tables(
    module: str = Query(None),
    include_system: bool = Query(False),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """List registered tables."""
    service = TableManagementService(db)
    tables = service.list_tables(module=module, include_system=include_system)
    return APIResponse(data=tables)


@router.get("/database/all", response_model=APIResponse)
async def list_all_database_tables(
    visible_only: bool = Query(False, description="Only return tables marked as visible in editor"),
    db: Session = Depends(get_db),
    _: User = Depends(RequirePermissions(["TABLE_READ"])),
):
    """List all tables from SQL Server (optionally filtered by visibility settings)."""
    service = TableManagementService(db)
    tables = service.list_all_database_tables()
    
    if visible_only:
        # Filter by table_settings visibility
        try:
            from sqlalchemy import text
            visible_tables_result = db.execute(text("""
                SELECT table_name FROM table_settings WHERE visible_in_editor = 1
            """)).fetchall()
            visible_set = {row[0] for row in visible_tables_result}

            # If no settings exist for a table, it's visible by default
            # So we only filter out tables explicitly marked as hidden
            hidden_result = db.execute(text("""
                SELECT table_name FROM table_settings WHERE visible_in_editor = 0
            """)).fetchall()
            hidden_set = {row[0] for row in hidden_result}

            # tables is a list of dicts ({"table_name": ..., "row_count": ...})
            # so compare on the table_name field, not the dict itself.
            tables = [t for t in tables if t.get("table_name") not in hidden_set]
        except Exception:
            pass  # If table_settings doesn't exist, return all tables

        # Also honour the Settings → Table Permissions toggles. A table whose
        # row in table_permissions has can_view = 0 is hidden from the Data
        # Editor dropdown. Filter on can_view (not can_edit) because can_view
        # defaults to 1 in the UI — only tables the user explicitly unchecks
        # "View" on get hidden. Tables with no row in table_permissions remain
        # visible (consistent with the visible-by-default rule above).
        try:
            from sqlalchemy import text
            hidden_perm_result = db.execute(text("""
                SELECT table_name FROM table_permissions WHERE can_view = 0
            """)).fetchall()
            hidden_perm_set = {row[0] for row in hidden_perm_result}
            tables = [t for t in tables if t.get("table_name") not in hidden_perm_set]
        except Exception:
            pass  # If table_permissions doesn't exist yet, skip this filter

    return APIResponse(data=tables)


@router.get("/settings/all", response_model=APIResponse)
async def list_all_table_settings(
    db: Session = Depends(get_db),
    _: User = Depends(RequirePermissions(["SETTINGS_VIEW"])),
):
    """List all table settings for management UI."""
    from sqlalchemy import text
    from app.database.session import get_data_engine
    
    try:
        # Get all tables from database
        data_engine = get_data_engine()
        with data_engine.connect() as conn:
            result = conn.execute(text("""
                SELECT t.name as table_name, 
                       SUM(p.[rows]) as row_count
                FROM sys.tables t
                LEFT JOIN sys.partitions p ON t.object_id = p.object_id AND p.index_id IN (0, 1)
                WHERE t.type = 'U'
                GROUP BY t.name
                ORDER BY t.name
            """))
            all_tables = {row[0]: row[1] or 0 for row in result.fetchall()}
        
        # Get existing settings
        settings_result = db.execute(text("""
            SELECT table_name, is_heavy, row_threshold, require_filter, 
                   ISNULL(visible_in_editor, 1) as visible_in_editor, 
                   filter_columns
            FROM table_settings
        """)).fetchall()
        
        settings_map = {}
        for row in settings_result:
            settings_map[row[0]] = {
                "is_heavy": row[1],
                "row_threshold": row[2],
                "require_filter": row[3],
                "visible_in_editor": row[4],
                "filter_columns": row[5],
            }
        
        # Combine
        result_list = []
        for table_name, row_count in all_tables.items():
            setting = settings_map.get(table_name, {
                "is_heavy": False,
                "row_threshold": 100000,
                "require_filter": False,
                "visible_in_editor": True,
                "filter_columns": None,
            })
            result_list.append({
                "table_name": table_name,
                "row_count": row_count,
                **setting
            })
        
        return APIResponse(data=result_list)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/settings/{table_name}", response_model=APIResponse)
async def update_table_settings(
    table_name: str,
    visible_in_editor: bool = Query(None),
    is_heavy: bool = Query(None),
    filter_columns: str = Query(None, description="JSON array of column names"),
    db: Session = Depends(get_db),
    _: User = Depends(RequirePermissions(["SETTINGS_EDIT"])),
):
    """Update settings for a specific table (visibility, filter columns, etc)."""
    from sqlalchemy import text
    
    try:
        # Check if settings exist
        existing = db.execute(text(
            "SELECT id FROM table_settings WHERE table_name = :name"
        ), {"name": table_name}).fetchone()
        
        if existing:
            # Update
            updates = []
            params = {"name": table_name}
            
            if visible_in_editor is not None:
                updates.append("visible_in_editor = :visible")
                params["visible"] = visible_in_editor
            if is_heavy is not None:
                updates.append("is_heavy = :heavy")
                params["heavy"] = is_heavy
            if filter_columns is not None:
                updates.append("filter_columns = :filters")
                params["filters"] = filter_columns
            
            if updates:
                updates.append("updated_at = GETDATE()")
                sql = f"UPDATE table_settings SET {', '.join(updates)} WHERE table_name = :name"
                db.execute(text(sql), params)
                db.commit()
        else:
            # Insert new settings
            db.execute(text("""
                INSERT INTO table_settings (table_name, visible_in_editor, is_heavy, filter_columns, created_at, updated_at)
                VALUES (:name, :visible, :heavy, :filters, GETDATE(), GETDATE())
            """), {
                "name": table_name,
                "visible": visible_in_editor if visible_in_editor is not None else True,
                "heavy": is_heavy if is_heavy is not None else False,
                "filters": filter_columns
            })
            db.commit()
        
        return APIResponse(data={"table_name": table_name, "updated": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Column Reorder
# ============================================================================

@router.put("/{table_name}/reorder-columns", response_model=APIResponse)
async def reorder_columns(
    table_name: str,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Reorder columns in a SQL Server table.
    Uses CREATE new table -> copy data -> drop old -> rename approach.
    """
    from app.database.session import get_data_engine
    from sqlalchemy import text as sa_text

    new_order = body.get("columns", [])
    if not new_order:
        raise HTTPException(400, detail="columns list is required")

    data_engine = get_data_engine()

    try:
        with data_engine.connect() as conn:
            # Get current columns with full type info
            rows = conn.execute(sa_text("""
                SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
                       NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE,
                       COLUMN_DEFAULT
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = :tn
                ORDER BY ORDINAL_POSITION
            """), {"tn": table_name}).fetchall()

            if not rows:
                raise HTTPException(404, detail=f"Table '{table_name}' not found")

            col_info = {}
            for r in rows:
                col_info[r[0]] = {
                    "name": r[0], "data_type": r[1],
                    "max_length": r[2], "precision": r[3], "scale": r[4],
                    "nullable": r[5], "default": r[6],
                }

            # Validate all columns in new_order exist
            existing = set(col_info.keys())
            ordered = list(new_order)
            # Add any missing columns at the end
            for c in col_info:
                if c not in ordered:
                    ordered.append(c)

            # Get primary key info
            pk_row = conn.execute(sa_text("""
                SELECT kc.name AS constraint_name,
                       STRING_AGG(c.name, ',') WITHIN GROUP (ORDER BY ic.key_ordinal) AS pk_columns
                FROM sys.key_constraints kc
                INNER JOIN sys.index_columns ic ON kc.parent_object_id = ic.object_id AND kc.unique_index_id = ic.index_id
                INNER JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
                WHERE kc.type = 'PK' AND OBJECT_NAME(kc.parent_object_id) = :tn
                GROUP BY kc.name
            """), {"tn": table_name}).fetchone()

            pk_name = pk_row[0] if pk_row else None
            pk_cols = pk_row[1].split(",") if pk_row else []

            # Build column definitions for new table
            def build_col_def(cname):
                info = col_info[cname]
                dt = info["data_type"].upper()
                if dt in ("NVARCHAR", "VARCHAR", "NCHAR", "CHAR"):
                    ml = info["max_length"]
                    type_sql = f"{dt}({ml})" if ml and ml > 0 else f"{dt}(MAX)"
                elif dt in ("DECIMAL", "NUMERIC"):
                    p = info["precision"] or 18
                    s = info["scale"] or 0
                    type_sql = f"{dt}({p},{s})"
                else:
                    type_sql = dt
                null_sql = "NULL" if info["nullable"] == "YES" else "NOT NULL"
                return f"[{cname}] {type_sql} {null_sql}"

            col_defs = ", ".join(build_col_def(c) for c in ordered)
            col_list = ", ".join(f"[{c}]" for c in ordered)
            tmp_name = f"_tmp_reorder_{table_name}"

            # Execute reorder: create tmp -> copy -> drop original -> rename
            conn.execute(sa_text(f"CREATE TABLE [{tmp_name}] ({col_defs})"))
            conn.execute(sa_text(f"INSERT INTO [{tmp_name}] ({col_list}) SELECT {col_list} FROM [{table_name}]"))
            conn.execute(sa_text(f"DROP TABLE [{table_name}]"))
            conn.execute(sa_text(f"EXEC sp_rename '{tmp_name}', '{table_name}'"))

            # Recreate primary key if existed
            if pk_cols:
                pk_col_list = ", ".join(f"[{c}]" for c in pk_cols)
                pk_constraint = pk_name or f"PK_{table_name}"
                conn.execute(sa_text(
                    f"ALTER TABLE [{table_name}] ADD CONSTRAINT [{pk_constraint}] PRIMARY KEY ({pk_col_list})"
                ))

            conn.commit()

        logger.info(f"Columns reordered for {table_name} by {current_user.username}: {ordered}")
        return APIResponse(message=f"Column order updated for {table_name}", data={"columns": ordered})

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Column reorder failed for {table_name}: {e}")
        raise HTTPException(500, detail=f"Failed to reorder columns: {e}")


# ============================================================================
# Table Data Operations
# ============================================================================

@router.get("/{table_name}/data", response_model=APIResponse)
async def query_table_data(
    table_name: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=10000),
    order_by: str = Query(None),
    order_dir: str = Query("ASC"),
    filters: str = Query(None, description="JSON encoded filter object"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Query paginated data from any table (for data grid)."""
    import json
    try:
        # Parse filters from JSON string
        filter_dict = None
        if filters:
            try:
                filter_dict = json.loads(filters)
            except:
                filter_dict = None
        
        service = TableManagementService(db)
        result = service.query_table_data(
            table_name=table_name,
            page=page,
            page_size=page_size,
            order_by=order_by,
            order_dir=order_dir,
            filters=filter_dict,
        )
        return APIResponse(data=result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _run_truncate_job(job_id: str, table_name: str, username: str) -> None:
    """Background worker — runs truncate in its own DB session and reports
    progress through the in-memory _truncate_jobs map."""
    db = SessionLocal()
    try:
        _truncate_progress_set(
            job_id, status="running", phase="connecting", processed=0, total=0,
            started_at=datetime.utcnow().isoformat(),
        )

        def cb(processed: int, total: int, phase: str) -> None:
            pct = 0
            if total and total > 0:
                pct = int(min(100, round(100.0 * processed / total)))
            _truncate_progress_set(
                job_id, processed=processed, total=total,
                phase=phase, percent=pct,
            )

        service = TableManagementService(db)
        result = service.truncate_table_data(
            table_name, deleted_by=username, progress_cb=cb,
        )
        _truncate_progress_set(
            job_id, status="done", phase="done", percent=100,
            rows_deleted=result.get("rows_deleted", 0),
            method=result.get("method"),
            finished_at=datetime.utcnow().isoformat(),
        )
        logger.info(
            f"[truncate {job_id}] done — {result.get('rows_deleted')} rows "
            f"via {result.get('method')}"
        )
    except Exception as e:
        logger.exception(f"[truncate {job_id}] failed")
        _truncate_progress_set(
            job_id, status="failed", phase="failed",
            error=str(e)[:300],
            finished_at=datetime.utcnow().isoformat(),
        )
    finally:
        try:
            db.close()
        except Exception:
            pass
        _truncate_progress_gc()


@router.delete(
    "/{table_name}/data",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["TABLE_DELETE"]))],
)
async def truncate_table_data(
    table_name: str,
    current_user: User = Depends(get_current_user),
):
    """Delete all data from a table (does NOT drop the table).

    Runs as a background job so the HTTP request returns immediately with a
    `job_id`. Poll `GET /tables/truncate/progress/{job_id}` to drive a
    progress bar in the UI.

    The service tries `TRUNCATE TABLE` first (milliseconds, minimally logged,
    no escalated lock); if a foreign-key constraint blocks TRUNCATE it falls
    back to batched `DELETE TOP (N)` with autocommit between batches so the
    log can checkpoint and other queries can interleave.
    """
    job_id = f"TRUNC_{uuid.uuid4().hex[:10]}"
    _truncate_progress_set(
        job_id,
        status="queued",
        phase="queued",
        table=table_name,
        user=current_user.username,
        percent=0,
        processed=0,
        total=0,
        created_at=datetime.utcnow().isoformat(),
    )
    threading.Thread(
        target=_run_truncate_job,
        args=(job_id, table_name, current_user.username),
        name=f"truncate-{job_id}",
        daemon=True,
    ).start()
    return APIResponse(
        data={"job_id": job_id, "status": "queued", "table": table_name},
        message="Truncate started — poll /tables/truncate/progress/{job_id}",
    )


@router.get(
    "/truncate/progress/{job_id}",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["TABLE_DELETE"]))],
)
async def get_truncate_progress(
    job_id: str,
    current_user: User = Depends(get_current_user),
):
    """Poll endpoint for the progress bar. Returns
    `{status, phase, percent, processed, total, rows_deleted?, method?, error?}`."""
    rec = _truncate_progress_get(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Unknown or expired truncate job")
    return APIResponse(data=rec)


# ============================================================================
# Export Table Data
# ============================================================================

def _build_export_where_clause(filter_dict):
    """Build WHERE clause and params from filter dict."""
    where_clause = ""
    params = {}
    param_idx = 0
    
    if not filter_dict:
        return where_clause, params
    
    conditions = []
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
    
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)
    
    return where_clause, params


@router.get("/{table_name}/export")
async def export_table_data(
    table_name: str,
    format: str = Query("xlsx", pattern="^(xlsx|csv)$"),
    columns: str = Query(None, description="Comma-separated column names"),
    filters: str = Query(None, description="JSON encoded filter object"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Export table data to Excel or CSV with auto-split based on settings."""
    import pandas as pd
    import json
    import zipfile
    from sqlalchemy import text
    from app.database.session import get_data_engine
    from app.models.audit import ExportSettings
    
    try:
        # Load export settings
        settings_rows = db.query(ExportSettings).all()
        settings = {s.setting_key: s.setting_value for s in settings_rows}
        max_rows = int(settings.get('max_rows_per_file', 100000))
        auto_split = settings.get('enable_auto_split', 'true').lower() == 'true'
        
        # Parse filters
        filter_dict = None
        if filters:
            try:
                filter_dict = json.loads(filters)
            except:
                filter_dict = None
        
        data_engine = get_data_engine()
        
        # Column selection
        col_list = "*"
        if columns:
            selected_cols = [c.strip() for c in columns.split(",") if c.strip()]
            if selected_cols:
                col_list = ", ".join([f"[{c}]" for c in selected_cols])
        
        # Build WHERE clause
        where_clause, params = _build_export_where_clause(filter_dict)
        
        # Get total count first
        count_query = f"SELECT COUNT(*) FROM [{table_name}] WITH (NOLOCK) {where_clause}"
        with data_engine.connect() as conn:
            total_rows = conn.execute(text(count_query), params).scalar()
        
        # Decide if we need to split
        need_split = auto_split and total_rows > max_rows
        
        if not need_split:
            # Single file export
            query = f"SELECT {col_list} FROM [{table_name}] WITH (NOLOCK) {where_clause}"
            with data_engine.connect() as conn:
                df = pd.read_sql(text(query), conn, params=params)
            
            output = io.BytesIO()
            if format == "xlsx":
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False, sheet_name=table_name[:31])
                output.seek(0)
                media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                filename = f"{table_name}.xlsx"
            else:
                df.to_csv(output, index=False)
                output.seek(0)
                media_type = "text/csv"
                filename = f"{table_name}.csv"
            
            return StreamingResponse(
                output,
                media_type=media_type,
                headers={"Content-Disposition": f'attachment; filename="{filename}"'}
            )
        
        # Split export - create ZIP with multiple files
        zip_buffer = io.BytesIO()
        num_files = (total_rows + max_rows - 1) // max_rows
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for part in range(num_files):
                offset = part * max_rows
                
                # Use OFFSET/FETCH for pagination (SQL Server 2012+)
                paginated_query = f"""
                    SELECT {col_list} FROM [{table_name}] WITH (NOLOCK) {where_clause}
                    ORDER BY (SELECT NULL)
                    OFFSET {offset} ROWS FETCH NEXT {max_rows} ROWS ONLY
                """
                
                with data_engine.connect() as conn:
                    df_part = pd.read_sql(text(paginated_query), conn, params=params)
                
                part_buffer = io.BytesIO()
                part_num = part + 1
                
                if format == "xlsx":
                    with pd.ExcelWriter(part_buffer, engine='openpyxl') as writer:
                        df_part.to_excel(writer, index=False, sheet_name=table_name[:31])
                    part_buffer.seek(0)
                    zf.writestr(f"{table_name}_part{part_num}.xlsx", part_buffer.getvalue())
                else:
                    df_part.to_csv(part_buffer, index=False)
                    part_buffer.seek(0)
                    zf.writestr(f"{table_name}_part{part_num}.csv", part_buffer.getvalue())
        
        zip_buffer.seek(0)
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{table_name}_export.zip"'}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")
