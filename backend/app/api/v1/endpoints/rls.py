"""
Row-Level Security Management API
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.database.session import get_db
from app.schemas.auth import StoreAccessCreate, RegionAccessCreate, ColumnRestrictionCreate
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user, RequirePermissions
from app.models.rbac import User
from app.models.rls import UserStoreAccess, UserRegionAccess, ColumnRestriction, Store, TableRoleAccess
from app.audit.service import AuditService

router = APIRouter(prefix="/rls", tags=["Row-Level Security"])


# ============================================================================
# Store Access
# ============================================================================

@router.post(
    "/store-access",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_RLS_MANAGE"]))],
)
async def assign_store_access(
    body: StoreAccessCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Assign store-level access to a user."""
    added = 0
    for code in body.store_codes:
        existing = db.query(UserStoreAccess).filter(
            UserStoreAccess.user_id == body.user_id,
            UserStoreAccess.store_code == code,
        ).first()
        if existing:
            existing.is_active = True
            existing.access_level = body.access_level
        else:
            db.add(UserStoreAccess(
                user_id=body.user_id, store_code=code,
                access_level=body.access_level, granted_by=current_user.username,
            ))
            added += 1
    db.commit()

    AuditService(db).log(
        table_name="rls_user_store_access", action_type="INSERT",
        changed_by=current_user.username,
        new_data={"user_id": body.user_id, "stores": body.store_codes},
        row_count=added,
    )
    db.commit()

    return APIResponse(message=f"Store access granted for {len(body.store_codes)} stores")


@router.get("/store-access/{user_id}", response_model=APIResponse)
async def get_user_store_access(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Get store access for a user."""
    records = db.query(UserStoreAccess).filter(
        UserStoreAccess.user_id == user_id, UserStoreAccess.is_active == True
    ).all()
    return APIResponse(data=[
        {"store_code": r.store_code, "access_level": r.access_level}
        for r in records
    ])


@router.delete(
    "/store-access/{user_id}/{store_code}",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_RLS_MANAGE"]))],
)
async def revoke_store_access(
    user_id: int,
    store_code: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke store access from a user."""
    record = db.query(UserStoreAccess).filter(
        UserStoreAccess.user_id == user_id,
        UserStoreAccess.store_code == store_code,
    ).first()
    if record:
        record.is_active = False
        db.commit()
    return APIResponse(message="Store access revoked")


# ============================================================================
# Region Access
# ============================================================================

@router.post(
    "/region-access",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_RLS_MANAGE"]))],
)
async def assign_region_access(
    body: RegionAccessCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Assign region-level access to a user."""
    record = UserRegionAccess(
        user_id=body.user_id,
        region=body.region,
        hub=body.hub,
        division=body.division,
        business_unit=body.business_unit,
        access_level=body.access_level,
        granted_by=current_user.username,
    )
    db.add(record)
    db.commit()
    return APIResponse(message="Region access granted")


@router.get("/region-access/{user_id}", response_model=APIResponse)
async def get_user_region_access(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Get region access for a user."""
    records = db.query(UserRegionAccess).filter(
        UserRegionAccess.user_id == user_id, UserRegionAccess.is_active == True
    ).all()
    return APIResponse(data=[
        {
            "id": r.id, "region": r.region, "hub": r.hub,
            "division": r.division, "business_unit": r.business_unit,
            "access_level": r.access_level,
        }
        for r in records
    ])


# ============================================================================
# Column Restrictions
# ============================================================================

@router.post(
    "/column-restrictions",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_RLS_MANAGE"]))],
)
async def create_column_restriction(
    body: ColumnRestrictionCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create or update a column-level restriction."""
    existing = db.query(ColumnRestriction).filter(
        ColumnRestriction.table_name == body.table_name,
        ColumnRestriction.column_name == body.column_name,
        ColumnRestriction.role_id == body.role_id,
    ).first()

    if existing:
        existing.is_visible = body.is_visible
        existing.is_masked = body.is_masked
        existing.mask_pattern = body.mask_pattern
        existing.can_edit = body.can_edit
    else:
        db.add(ColumnRestriction(
            table_name=body.table_name, column_name=body.column_name,
            role_id=body.role_id, is_visible=body.is_visible,
            is_masked=body.is_masked, mask_pattern=body.mask_pattern,
            can_edit=body.can_edit,
        ))

    db.commit()
    return APIResponse(message="Column restriction saved")


@router.get("/my-column-restrictions/{table_name}", response_model=APIResponse)
async def get_my_column_restrictions(
    table_name: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get column restrictions for the current user's roles (most restrictive wins)."""
    role_ids = [ur.role_id for ur in current_user.user_roles if ur.is_active]
    if not role_ids:
        return APIResponse(data=[])
    records = db.query(ColumnRestriction).filter(
        ColumnRestriction.table_name == table_name,
        ColumnRestriction.role_id.in_(role_ids),
    ).all()
    col_map = {}
    for r in records:
        c = r.column_name
        if c not in col_map:
            col_map[c] = {"is_visible": True, "is_masked": False, "can_edit": True}
        if not r.is_visible:
            col_map[c]["is_visible"] = False
        if r.is_masked:
            col_map[c]["is_masked"] = True
        if not getattr(r, "can_edit", True):
            col_map[c]["can_edit"] = False
    return APIResponse(data=[{"column_name": col, **perms} for col, perms in col_map.items()])


@router.get("/column-restrictions/{table_name}", response_model=APIResponse)
async def get_column_restrictions(
    table_name: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Get column restrictions for a table."""
    records = db.query(ColumnRestriction).filter(
        ColumnRestriction.table_name == table_name
    ).all()
    return APIResponse(data=[
        {
            "id": r.id, "column_name": r.column_name, "role_id": r.role_id,
            "is_visible": r.is_visible, "is_masked": r.is_masked,
            "mask_pattern": r.mask_pattern, "can_edit": getattr(r, 'can_edit', True),
        }
        for r in records
    ])


@router.post(
    "/column-restrictions/bulk",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_RLS_MANAGE"]))],
)
async def bulk_save_column_restrictions(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Bulk save column restrictions for a table+role. Replaces existing."""
    table_name = body.get("table_name")
    role_id = body.get("role_id")
    restrictions = body.get("restrictions", [])

    if not table_name or not role_id:
        raise HTTPException(400, detail="table_name and role_id required")

    # Delete existing for this table+role
    db.query(ColumnRestriction).filter(
        ColumnRestriction.table_name == table_name,
        ColumnRestriction.role_id == role_id,
    ).delete()

    # Insert new
    for r in restrictions:
        db.add(ColumnRestriction(
            table_name=table_name, column_name=r["column_name"],
            role_id=role_id,
            is_visible=r.get("is_visible", True),
            is_masked=r.get("is_masked", False),
            mask_pattern=r.get("mask_pattern"),
            can_edit=r.get("can_edit", True),
        ))

    db.commit()
    return APIResponse(message=f"Saved {len(restrictions)} column restrictions for {table_name}")


@router.delete(
    "/column-restrictions/{restriction_id}",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_RLS_MANAGE"]))],
)
async def delete_column_restriction(
    restriction_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Delete a column restriction."""
    r = db.query(ColumnRestriction).filter(ColumnRestriction.id == restriction_id).first()
    if not r:
        raise HTTPException(404, detail="Restriction not found")
    db.delete(r)
    db.commit()
    return APIResponse(message="Column restriction deleted")


# ============================================================================
# Table-Role Access Control
# ============================================================================

@router.get("/table-access/{table_name}", response_model=APIResponse)
async def get_table_access(table_name: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    """Get role access for a table."""
    records = db.query(TableRoleAccess).filter(TableRoleAccess.table_name == table_name).all()
    return APIResponse(data=[
        {"id": r.id, "table_name": r.table_name, "role_id": r.role_id,
         "can_read": r.can_read, "can_write": r.can_write,
         "can_upload": r.can_upload, "can_export": r.can_export}
        for r in records
    ])


@router.post(
    "/table-access/bulk",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ADMIN_RLS_MANAGE"]))],
)
async def bulk_save_table_access(body: dict, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Bulk save table-role access. Replaces existing for the given table."""
    table_name = body.get("table_name")
    access_list = body.get("access", [])
    if not table_name:
        raise HTTPException(400, detail="table_name required")

    db.query(TableRoleAccess).filter(TableRoleAccess.table_name == table_name).delete()
    for a in access_list:
        db.add(TableRoleAccess(
            table_name=table_name, role_id=a["role_id"],
            can_read=a.get("can_read", True), can_write=a.get("can_write", False),
            can_upload=a.get("can_upload", False), can_export=a.get("can_export", False),
            granted_by=current_user.username,
        ))
    db.commit()
    return APIResponse(message=f"Table access saved for {table_name} ({len(access_list)} roles)")


@router.get("/table-access-by-role/{role_id}", response_model=APIResponse)
async def get_tables_for_role(role_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    """Get all table access for a specific role."""
    records = db.query(TableRoleAccess).filter(TableRoleAccess.role_id == role_id).all()
    return APIResponse(data=[
        {"table_name": r.table_name, "can_read": r.can_read, "can_write": r.can_write,
         "can_upload": r.can_upload, "can_export": r.can_export}
        for r in records
    ])


# ============================================================================
# Stores (for RLS configuration)
# ============================================================================

@router.get("/stores", response_model=APIResponse)
async def list_stores(
    region: str = None,
    division: str = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """List all stores (filterable by region/division)."""
    query = db.query(Store).filter(Store.is_active == True)
    if region:
        query = query.filter(Store.region == region)
    if division:
        query = query.filter(Store.division == division)

    stores = query.order_by(Store.store_code).all()
    return APIResponse(data=[
        {
            "store_code": s.store_code, "store_name": s.store_name,
            "region": s.region, "hub": s.hub, "division": s.division,
            "store_grade": s.store_grade, "city": s.city, "state": s.state,
        }
        for s in stores
    ])
