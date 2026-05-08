"""
Auth API Endpoints: Login, Refresh Token, Password Change
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.schemas.auth import (
    LoginRequest, TokenResponse, RefreshTokenRequest, ChangePasswordRequest
)
from app.schemas.common import APIResponse
from app.services.auth_service import AuthService
from app.security.dependencies import get_current_user
from app.models.rbac import User
from app.audit.service import get_client_ip

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/login", response_model=TokenResponse)
async def login(request: Request, body: LoginRequest, db: Session = Depends(get_db)):
    """Authenticate and get access + refresh tokens."""
    try:
        service = AuthService(db)
        ip = get_client_ip(request)
        return service.authenticate(body, ip_address=ip)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(body: RefreshTokenRequest, db: Session = Depends(get_db)):
    """Refresh access token."""
    try:
        service = AuthService(db)
        return service.refresh_tokens(body.refresh_token)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))


@router.post("/change-password", response_model=APIResponse)
async def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change current user's password."""
    try:
        service = AuthService(db)
        service.change_password(current_user, body.current_password, body.new_password)
        return APIResponse(message="Password changed successfully")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/me", response_model=APIResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """Get current logged-in user info."""
    from app.schemas.auth import UserResponse
    return APIResponse(
        data=UserResponse(
            id=current_user.id,
            username=current_user.username,
            email=current_user.email,
            full_name=current_user.full_name,
            employee_code=current_user.employee_code,
            mobile_no=current_user.mobile_no,
            phone=current_user.phone,
            is_active=current_user.is_active,
            is_locked=current_user.is_locked,
            last_login=current_user.last_login,
            created_at=current_user.created_at,
            roles=current_user.role_codes,
            permissions=list(current_user.permissions),
        ).model_dump()
    )


from pydantic import BaseModel
from typing import Optional

class ProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    mobile_no: Optional[str] = None


@router.put("/profile", response_model=APIResponse)
async def update_profile(
    body: ProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update current user's profile."""
    if body.email is not None and body.email != current_user.email:
        dup = db.query(User).filter(User.email == body.email, User.id != current_user.id).first()
        if dup:
            raise HTTPException(400, detail=f"Email '{body.email}' is already used by another user")
        current_user.email = body.email
    if body.mobile_no is not None and body.mobile_no != current_user.mobile_no:
        dup = db.query(User).filter(User.mobile_no == body.mobile_no, User.id != current_user.id).first()
        if dup:
            raise HTTPException(400, detail=f"Mobile '{body.mobile_no}' is already used by another user")
        current_user.mobile_no = body.mobile_no
    if body.full_name is not None:
        current_user.full_name = body.full_name
    db.commit()
    db.refresh(current_user)
    return APIResponse(message="Profile updated successfully")
