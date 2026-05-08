"""
Upsert & Data Operations API Endpoints
- JSON-based upsert (small batches)
- Direct cell update (inline edits)
- Bulk delete
- Column-level permission checks
- Data change audit logging (handled by DirectUpdateEngine)
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.schemas.table_mgmt import UpsertRequest, DataUpdateRequest, DataDeleteRequest
from app.schemas.common import APIResponse
from app.services.upsert_engine import UpsertEngine, DirectUpdateEngine
from app.security.dependencies import get_current_user, RequirePermissions, get_editable_columns
from app.models.rbac import User
from app.audit.service import get_client_ip

router = APIRouter(prefix="/data", tags=["Data Operations"])


# ============================================================================
# JSON Upsert (for small-medium batches via API)
# ============================================================================

@router.post(
    "/upsert",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["DATA_EDIT"]))],
)
async def upsert_data(
    body: UpsertRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Upsert records into a table (JSON body).
    - If record exists (by PK) → update only changed fields
    - If record doesn't exist → insert

    Best for small-medium batches (< 10K rows).
    For larger batches, use the file upload endpoint.
    """
    import pandas as pd

    try:
        df = pd.DataFrame(body.records)

        engine = UpsertEngine(db)
        result = engine.upsert(
            table_name=body.table_name,
            df=df,
            primary_key_columns=body.primary_key_columns,
            changed_by=current_user.username,
            source="API",
            ip_address=get_client_ip(request),
        )
        return APIResponse(data=result, message="Upsert completed")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upsert failed: {str(e)}")


# ============================================================================
# Direct Cell Update (for inline grid edits)
# ============================================================================

@router.put(
    "/update",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["DATA_EDIT"]))],
)
async def update_record(
    body: DataUpdateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Update a single record (for inline cell editing in the data grid).
    Only updates columns that actually changed.
    Enforces column-level edit permissions.
    Audit logging is handled by DirectUpdateEngine.
    """
    from loguru import logger
    
    try:
        # Check column edit permissions
        all_columns = list(body.updates.keys())
        editable_columns = get_editable_columns(db, body.table_name, current_user.role_codes, all_columns)
        
        # Find columns user is not allowed to edit
        forbidden_columns = [col for col in all_columns if col not in editable_columns]
        if forbidden_columns:
            raise HTTPException(
                status_code=403, 
                detail=f"Not allowed to edit columns: {', '.join(forbidden_columns)}"
            )
        
        # Perform the update (DirectUpdateEngine already handles audit logging)
        engine = DirectUpdateEngine(db)
        result = engine.update_record(
            table_name=body.table_name,
            primary_key_columns=body.primary_key_columns,
            primary_key_values=body.primary_key_values,
            updates=body.updates,
            changed_by=current_user.username,
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent", "")[:500],
        )
        
        return APIResponse(data=result, message="Record updated")
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Update failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Bulk Delete
# ============================================================================

@router.post(
    "/delete",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["DATA_EDIT"]))],
)
async def delete_records(
    body: DataDeleteRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete multiple records by primary key values."""
    try:
        # Perform the delete (DirectUpdateEngine already handles audit logging)
        engine = DirectUpdateEngine(db)
        result = engine.delete_records(
            table_name=body.table_name,
            primary_key_columns=body.primary_key_columns,
            primary_key_values_list=body.primary_key_values,
            changed_by=current_user.username,
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent", "")[:500],
        )
        
        return APIResponse(data=result, message=f"Deleted {result['deleted']} record(s)")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
