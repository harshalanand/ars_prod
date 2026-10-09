"""
Roles & Permissions Management API
"""
import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.database.session import get_db
from app.schemas.auth import (
    RoleCreate, RoleUpdate, RoleResponse, PermissionResponse, AssignPermissionsRequest
)
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user, RequirePermissions
from app.models.rbac import Role, Permission, RolePermission, User, UserRole
from app.models.rls import ColumnRestriction, TableRoleAccess
from app.audit.service import AuditService

router = APIRouter(prefix="/roles", tags=["Roles & Permissions"])


def _derive_role_code(text: str) -> str:
    """'Store Manager' -> 'STORE_MANAGER'. The UI only collects a name."""
    code = re.sub(r"[^A-Z0-9]+", "_", text.upper()).strip("_")[:50]
    if len(code) < 2:
        raise HTTPException(status_code=400, detail="Role name must contain at least 2 letters or digits")
    return code


def _ensure_unique(db: Session, role_name: str, role_code: str, exclude_id: int = None):
    q = db.query(Role).filter((Role.role_code == role_code) | (Role.role_name == role_name))
    if exclude_id is not None:
        q = q.filter(Role.id != exclude_id)
    existing = q.first()
    if existing:
        state = "" if existing.is_active else " (inactive)"
        raise HTTPException(
            status_code=400,
            detail=f"Role '{existing.role_name}'{state} already uses this name or code '{existing.role_code}'",
        )


# ============================================================================
# Roles
# ============================================================================

@router.get("", response_model=APIResponse)
async def list_roles(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    """List all roles."""
    roles = db.query(Role).filter(Role.is_active == True).all()
    result = []
    for r in roles:
        perms = [rp.permission.permission_code for rp in r.role_permissions if rp.permission]
        result.append(RoleResponse(
            id=r.id, role_name=r.role_name, role_code=r.role_code,
            description=r.description, is_system_role=r.is_system_role,
            is_active=r.is_active, created_at=r.created_at, permissions=perms,
        ))
    return APIResponse(data=[r.model_dump() for r in result])


@router.post(
    "",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_ROLES_MANAGE"]))],
)
async def create_role(
    body: RoleCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new role."""
    role_name = body.role_name.strip()
    role_code = _derive_role_code(body.role_code or role_name)
    _ensure_unique(db, role_name, role_code)

    role = Role(
        role_name=role_name,
        role_code=role_code,
        description=body.description,
        created_by=current_user.username,
    )
    db.add(role)
    db.commit()
    db.refresh(role)

    AuditService(db).log_insert(
        table_name="rbac_roles", changed_by=current_user.username,
        record_pk=str(role.id), new_data={"role_name": role.role_name, "role_code": role.role_code},
    )
    db.commit()

    return APIResponse(data={"id": role.id, "role_code": role.role_code}, message="Role created")


@router.put(
    "/{role_id}",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_ROLES_MANAGE"]))],
)
async def update_role(
    role_id: int,
    body: RoleUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update a role."""
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.is_system_role and body.is_active is False:
        raise HTTPException(status_code=400, detail="Cannot deactivate system role")

    old = {"role_name": role.role_name, "role_code": role.role_code, "description": role.description}
    new_name = (body.role_name or "").strip()
    if new_name and new_name != role.role_name:
        if role.is_system_role:
            raise HTTPException(status_code=400, detail=f"'{role.role_name}' is a system role and cannot be renamed")
        # Custom roles keep their code in step with the name; access is granted
        # by role_id + permissions, never by the code string.
        new_code = _derive_role_code(new_name)
        _ensure_unique(db, new_name, new_code, exclude_id=role.id)
        role.role_name = new_name
        role.role_code = new_code
    if body.description is not None:
        role.description = body.description.strip()
    if body.is_active is not None:
        role.is_active = body.is_active

    db.commit()
    AuditService(db).log(
        table_name="rbac_roles", action_type="UPDATE",
        changed_by=current_user.username, record_primary_key=str(role.id),
        old_data=old,
        new_data={"role_name": role.role_name, "role_code": role.role_code, "description": role.description},
    )
    db.commit()
    return APIResponse(data={"id": role.id, "role_code": role.role_code}, message="Role updated")


@router.delete(
    "/{role_id}",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_ROLES_MANAGE"]))],
)
async def delete_role(
    role_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Permanently delete a custom role. Refused while any user still holds it."""
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.is_system_role:
        raise HTTPException(status_code=400, detail=f"'{role.role_name}' is a system role and cannot be deleted")

    holders = (
        db.query(User.username)
        .join(UserRole, UserRole.user_id == User.id)
        .filter(UserRole.role_id == role_id)
        .order_by(User.username)
        .all()
    )
    if holders:
        names = [h.username for h in holders]
        shown = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
        raise HTTPException(
            status_code=400,
            detail=f"Role '{role.role_name}' is assigned to {len(names)} user(s): {shown}. "
                   f"Remove it from them in Settings → Users first.",
        )

    snapshot = {"role_name": role.role_name, "role_code": role.role_code, "description": role.description}
    # Child rows first (FKs to rbac_roles, no cascade).
    db.query(RolePermission).filter(RolePermission.role_id == role_id).delete(synchronize_session=False)
    db.query(ColumnRestriction).filter(ColumnRestriction.role_id == role_id).delete(synchronize_session=False)
    db.query(TableRoleAccess).filter(TableRoleAccess.role_id == role_id).delete(synchronize_session=False)
    # Bulk delete, not db.delete(role): the selectin-loaded child collections
    # would make the ORM try to NULL the FKs of rows already removed above.
    db.query(Role).filter(Role.id == role_id).delete(synchronize_session=False)
    db.commit()

    AuditService(db).log(
        table_name="rbac_roles", action_type="DELETE",
        changed_by=current_user.username, record_primary_key=str(role_id),
        old_data=snapshot, notes="Role deleted",
    )
    db.commit()
    return APIResponse(message=f"Role '{snapshot['role_name']}' deleted")


# ============================================================================
# Permissions
# ============================================================================

@router.get("/permissions", response_model=APIResponse)
async def list_permissions(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    """List all permissions."""
    perms = db.query(Permission).filter(Permission.is_active == True).all()
    result = [PermissionResponse.model_validate(p) for p in perms]
    return APIResponse(data=[r.model_dump() for r in result])


@router.post(
    "/{role_id}/permissions",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_PERMS_MANAGE"]))],
)
async def assign_permissions(
    role_id: int,
    body: AssignPermissionsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Assign permissions to a role (replaces existing)."""
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    # Resolve permission IDs from codes if needed
    perm_ids = body.permission_ids or []
    if body.permission_codes:
        for code in body.permission_codes:
            perm = db.query(Permission).filter(Permission.permission_code == code).first()
            if perm:
                perm_ids.append(perm.id)
    perm_ids = list(set(perm_ids))  # deduplicate

    # Remove existing
    db.query(RolePermission).filter(RolePermission.role_id == role_id).delete()

    # Add new
    for pid in perm_ids:
        perm = db.query(Permission).filter(Permission.id == pid).first()
        if perm:
            db.add(RolePermission(
                role_id=role_id, permission_id=pid, granted_by=current_user.username
            ))

    db.commit()

    AuditService(db).log(
        table_name="rbac_role_permissions", action_type="UPDATE",
        changed_by=current_user.username, record_primary_key=str(role_id),
        new_data={"permission_ids": perm_ids},
        notes="Permissions reassigned",
    )
    db.commit()

    return APIResponse(message=f"Permissions updated for role '{role.role_name}'")
