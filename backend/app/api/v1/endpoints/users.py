"""
User Management API Endpoints
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from typing import Optional

from fastapi import Request
from loguru import logger

from app.database.session import get_db
from app.schemas.auth import UserCreate, UserUpdate, UserResponse, UserListResponse
from app.schemas.common import APIResponse
from app.services.auth_service import AuthService
from app.security.dependencies import get_current_user, RequirePermissions
from app.models.rbac import User, UserRole

router = APIRouter(prefix="/users", tags=["User Management"])


@router.post(
    "",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_USERS_CREATE"]))],
)
async def create_user(
    body: UserCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new user (requires ADMIN_USERS_CREATE permission)."""
    try:
        service = AuthService(db)
        user = service.create_user(body, created_by=current_user.username)
        return APIResponse(data=user.model_dump(), message="User created successfully")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))



@router.get(
    "",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_USERS_READ"]))],
)
async def list_users(
    page: Optional[int] = Query(None, ge=1, description="Page number (optional)"),
    page_size: Optional[int] = Query(None, ge=1, le=1000),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """List all users with pagination and search. Accepts page and page_size as optional query params."""
    # Default values if missing or empty
    if page is None:
        page = 1
    if page_size is None:
        page_size = 50
    service = AuthService(db)
    result = service.list_users(page=page, page_size=page_size, search=search)
    return APIResponse(data=result)


@router.get(
    "/{user_id}",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_USERS_READ"]))],
)
async def get_user(user_id: int, db: Session = Depends(get_db)):
    """Get user by ID."""
    service = AuthService(db)
    user = service.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return APIResponse(data=user.model_dump())


@router.put(
    "/{user_id}",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_USERS_UPDATE"]))],
)
async def update_user(
    user_id: int,
    body: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update user details."""
    try:
        service = AuthService(db)
        user = service.update_user(user_id, body, updated_by=current_user.username)
        return APIResponse(data=user.model_dump(), message="User updated successfully")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post(
    "/{user_id}/unlock",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_USERS_UPDATE"]))],
)
async def unlock_user(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Unlock a locked user account."""
    try:
        service = AuthService(db)
        service.unlock_user(user_id, unlocked_by=current_user.username)
        return APIResponse(message="User unlocked successfully")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.delete(
    "/{user_id}",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_USERS_DELETE"]))],
)
async def delete_user(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Hard delete a user and all related records."""
    from sqlalchemy import text

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.username == "superadmin":
        raise HTTPException(status_code=400, detail="Cannot delete superadmin")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")

    username = user.username
    # Delete all related records first (foreign keys)
    db.execute(text("DELETE FROM rls_user_store_access WHERE user_id = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM rls_user_region_access WHERE user_id = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM rbac_user_roles WHERE user_id = :uid"), {"uid": user_id})
    db.execute(text("UPDATE audit_log SET changed_by = :name WHERE changed_by = (SELECT username FROM rbac_users WHERE id = :uid)"), {"name": f"[deleted]{username}", "uid": user_id})
    db.execute(text("DELETE FROM rbac_users WHERE id = :uid"), {"uid": user_id})
    db.commit()
    return APIResponse(message=f"User '{username}' deleted permanently")
