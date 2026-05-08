"""
File Upload API Endpoints
- Bulk CSV/Excel upload → Upsert
- Background job processing for large files
- File preview (for column mapping UI)
- Sheet names extraction
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form, Query
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.schemas.common import APIResponse
from app.services.file_upload_service import FileUploadService
from app.security.dependencies import get_current_user, RequirePermissions
from app.models.rbac import User
from app.audit.service import get_client_ip

router = APIRouter(prefix="/upload", tags=["File Upload"])


# ============================================================================
# Bulk File Upload → Upsert
# ============================================================================

@router.post(
    "/",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["DATA_UPLOAD"]))],
)
async def upload_file(
    request: Request,
    file: UploadFile = File(..., description="CSV or Excel file to upload"),
    table_name: str = Form(..., description="Target table name"),
    primary_key_columns: str = Form(..., description="Comma-separated PK column names"),
    mode: str = Form("upsert", description="Operation mode: 'upsert' or 'delete'"),
    column_mapping: Optional[str] = Form(None, description="JSON: {file_col: table_col}"),
    skip_rows: int = Form(0, description="Number of rows to skip from top"),
    sheet_name: Optional[str] = Form(None, description="Excel sheet name"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Upload a CSV or Excel file and upsert data into the target table.

    - If record exists (based on PKs) → update only changed fields
    - If record doesn't exist → insert new record
    - Supports 1M+ rows (chunked processing)
    - Audit logs all changes with batch_id

    Example:
    ```
    curl -X POST /api/v1/upload/ \
      -H "Authorization: Bearer <token>" \
      -F "file=@data.csv" \
      -F "table_name=store_stock" \
      -F "primary_key_columns=store_code,variant_code"
    ```
    """
    try:
        # Parse inputs
        pk_cols = [c.strip() for c in primary_key_columns.split(",")]

        col_map = None
        if column_mapping:
            try:
                col_map = json.loads(column_mapping)
            except json.JSONDecodeError:
                raise ValueError("Invalid column_mapping JSON")

        # Read file content
        content = await file.read()
        if not content:
            raise ValueError("File is empty")

        # Process based on mode
        service = FileUploadService(db)
        
        if mode == "delete":
            # Delete mode: delete rows based on primary key values
            result = await service.process_delete(
                file_content=content,
                file_name=file.filename or "unknown",
                table_name=table_name,
                primary_key_columns=pk_cols,
                changed_by=current_user.username,
                ip_address=get_client_ip(request),
                skip_rows=skip_rows,
                sheet_name=sheet_name,
            )
            return APIResponse(
                data=result,
                message=f"Delete complete: {result['deleted']} deleted, {result['not_found']} not found, {result['errors']} errors",
            )
        else:
            # Upsert mode (default)
            result = await service.process_upload(
                file_content=content,
                file_name=file.filename or "unknown",
                table_name=table_name,
                primary_key_columns=pk_cols,
                changed_by=current_user.username,
                ip_address=get_client_ip(request),
                column_mapping=col_map,
                skip_rows=skip_rows,
                sheet_name=sheet_name,
            )
            return APIResponse(
                data=result,
                message=(
                    f"Upload complete: {result['inserted']} inserted, "
                    f"{result['updated']} updated, {result['errors']} errors"
                ),
            )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


# ============================================================================
# Background Upload (for large files)
# ============================================================================

@router.post(
    "/async",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["DATA_UPLOAD"]))],
)
async def upload_file_async(
    request: Request,
    file: UploadFile = File(..., description="CSV or Excel file to upload"),
    table_name: str = Form(..., description="Target table name"),
    primary_key_columns: str = Form(..., description="Comma-separated PK column names"),
    mode: str = Form("upsert", description="Operation mode: 'upsert' or 'delete'"),
    column_mapping: Optional[str] = Form(None, description="JSON: {file_col: table_col}"),
    skip_rows: int = Form(0, description="Number of rows to skip from top"),
    sheet_name: Optional[str] = Form(None, description="Excel sheet name"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Upload a file for background processing.
    Returns immediately with a job_id that can be polled for status.
    Ideal for large files (100K+ rows) where sync processing would timeout.
    """
    from app.services.upload_job_service import create_upload_job
    
    try:
        pk_cols = [c.strip() for c in primary_key_columns.split(",")]

        col_map = None
        if column_mapping:
            try:
                col_map = json.loads(column_mapping)
            except json.JSONDecodeError:
                raise ValueError("Invalid column_mapping JSON")

        content = await file.read()
        if not content:
            raise ValueError("File is empty")

        result = create_upload_job(
            db=db,
            table_name=table_name,
            file_name=file.filename or "unknown",
            file_content=content,
            primary_key_columns=pk_cols,
            mode=mode,
            created_by=current_user.username,
            ip_address=get_client_ip(request),
            column_mapping=col_map,
            skip_rows=skip_rows,
            sheet_name=sheet_name,
        )
        
        return APIResponse(
            data=result,
            message=f"Upload job created: {result['job_id']}. Processing in background.",
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create upload job: {str(e)}")


# ============================================================================
# Upload Jobs API
# ============================================================================

@router.get("/jobs", response_model=APIResponse)
async def list_upload_jobs(
    limit: int = Query(20, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List user's upload jobs."""
    from app.services.upload_job_service import get_user_jobs
    jobs = get_user_jobs(db, current_user.username, limit)
    return APIResponse(data=jobs)


@router.get("/jobs/all", response_model=APIResponse)
async def list_all_upload_jobs(
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    _: User = Depends(RequirePermissions(["ADMIN_SETTINGS"])),
):
    """List all upload jobs (admin)."""
    from app.services.upload_job_service import get_all_jobs
    jobs = get_all_jobs(db, limit)
    return APIResponse(data=jobs)


@router.get("/jobs/{job_id}", response_model=APIResponse)
async def get_upload_job_status(
    job_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Get status of an upload job."""
    from app.services.upload_job_service import get_job_status
    status = get_job_status(db, job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Job not found")
    return APIResponse(data=status)


@router.post("/jobs/{job_id}/cancel", response_model=APIResponse)
async def cancel_upload_job(
    job_id: str,
    force: bool = Query(False, description="Force stop immediately"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cancel a queued or running upload job."""
    from app.services.upload_job_service import cancel_job
    result = cancel_job(db, job_id, force=force)
    if not result.get('success'):
        raise HTTPException(status_code=400, detail=result.get('error'))
    return APIResponse(data=result, message=result.get('message'))


@router.get("/queue/status", response_model=APIResponse)
async def get_queue_status_endpoint(
    _: User = Depends(get_current_user),
):
    """Get upload queue status."""
    from app.services.upload_job_service import get_queue_status
    status = get_queue_status()
    return APIResponse(data=status)


@router.delete("/jobs/{job_id}", response_model=APIResponse)
async def delete_upload_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a completed/failed/cancelled upload job record."""
    from app.services.upload_job_service import delete_job
    result = delete_job(db, job_id, current_user.username)
    if not result.get('success'):
        raise HTTPException(status_code=400, detail=result.get('error'))
    return APIResponse(data=result, message=result.get('message'))


# ============================================================================
# File Preview (for column mapping UI)
# ============================================================================

@router.post("/preview", response_model=APIResponse)
async def preview_file(
    file: UploadFile = File(...),
    rows: int = Form(20, description="Number of rows to preview"),
    skip_rows: int = Form(0),
    sheet_name: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Preview an uploaded file before processing.
    Returns column names, data types, sample values, and first N rows.
    Useful for building a column mapping UI.
    """
    try:
        content = await file.read()
        if not content:
            raise ValueError("File is empty")

        service = FileUploadService(db)
        result = service.preview_file(
            file_content=content,
            file_name=file.filename or "unknown",
            rows=rows,
            skip_rows=skip_rows,
            sheet_name=sheet_name,
        )
        return APIResponse(data=result)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================================
# Excel Sheet Names
# ============================================================================

@router.post("/sheets", response_model=APIResponse)
async def get_sheet_names(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get sheet names from an uploaded Excel file."""
    try:
        content = await file.read()
        service = FileUploadService(db)
        sheets = service.get_sheet_names(content, file.filename or "unknown")
        return APIResponse(data={"sheets": sheets})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
