"""
Auth & User Pydantic Schemas
"""
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, EmailStr, Field


# ============================================================================
# Auth Schemas
# ============================================================================

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=6)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: "UserResponse"


class TokenPayload(BaseModel):
    sub: str          # username
    user_id: int
    roles: List[str]
    permissions: List[str]
    exp: int


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


# ============================================================================
# User Schemas
# ============================================================================

class UserCreate(BaseModel):
    model_config = {"extra": "ignore"}
    username: str = Field(..., min_length=3, max_length=100)
    email: Optional[str] = None
    mobile_no: str = Field(..., min_length=10, max_length=15)
    password: str = Field(..., min_length=8)
    full_name: str = Field(..., min_length=2, max_length=200)
    employee_code: Optional[str] = None
    phone: Optional[str] = None
    is_active: Optional[bool] = True
    role_ids: List[int] = Field(default_factory=list)


class UserUpdate(BaseModel):
    model_config = {"extra": "ignore"}  # ignore extra fields like username
    username: Optional[str] = None
    email: Optional[str] = None
    mobile_no: Optional[str] = None
    full_name: Optional[str] = None
    employee_code: Optional[str] = None
    phone: Optional[str] = None
    is_active: Optional[bool] = None
    role_ids: Optional[List[int]] = None
    password: Optional[str] = None


class UserResponse(BaseModel):
    id: int
    username: str
    email: Optional[str] = None
    mobile_no: str
    full_name: str
    employee_code: Optional[str] = None
    phone: Optional[str] = None
    is_active: bool
    is_locked: bool
    last_login: Optional[datetime] = None
    created_at: datetime
    roles: List[str] = []
    permissions: List[str] = []

    class Config:
        from_attributes = True


class UserListResponse(BaseModel):
    users: List[UserResponse]
    total: int
    page: int
    page_size: int


# ============================================================================
# Role Schemas
# ============================================================================

class RoleCreate(BaseModel):
    role_name: str = Field(..., min_length=2, max_length=100)
    role_code: str = Field(..., min_length=2, max_length=50)
    description: Optional[str] = None


class RoleUpdate(BaseModel):
    role_name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class RoleResponse(BaseModel):
    id: int
    role_name: str
    role_code: str
    description: Optional[str] = None
    is_system_role: bool
    is_active: bool
    created_at: datetime
    permissions: List[str] = []

    class Config:
        from_attributes = True


class PermissionResponse(BaseModel):
    id: int
    permission_name: str
    permission_code: str
    module: str
    action: str
    resource: Optional[str] = None

    class Config:
        from_attributes = True


class AssignPermissionsRequest(BaseModel):
    permission_ids: Optional[List[int]] = None
    permission_codes: Optional[List[str]] = None


# ============================================================================
# RLS Schemas
# ============================================================================

class StoreAccessCreate(BaseModel):
    user_id: int
    store_codes: List[str]
    access_level: str = "READ"


class RegionAccessCreate(BaseModel):
    user_id: int
    region: Optional[str] = None
    hub: Optional[str] = None
    division: Optional[str] = None
    business_unit: Optional[str] = None
    access_level: str = "READ"


class ColumnRestrictionCreate(BaseModel):
    table_name: str
    column_name: str
    role_id: int
    is_visible: bool = True
    is_masked: bool = False
    mask_pattern: Optional[str] = None
    can_edit: bool = True


# Resolve forward reference
TokenResponse.model_rebuild()
