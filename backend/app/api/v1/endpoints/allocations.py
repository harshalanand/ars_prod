"""
Allocation Engine API Endpoints
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database.session import get_db
from app.schemas.allocation import (
    AllocationCreateRequest, AllocationUpdateRequest,
    AllocationOverrideRequest, AllocationApproveRequest,
)
from app.schemas.common import APIResponse
from app.services.allocation_engine import AllocationEngine
from app.security.dependencies import (
    get_current_user, RequirePermissions, get_rls_context, RLSContext
)
from app.models.rbac import User
from app.models.retail import AllocationHeader, AllocationDetail
from app.audit.service import get_client_ip

router = APIRouter(prefix="/allocations", tags=["Allocation Engine"])


# ============================================================================
# Run Allocation
# ============================================================================

@router.post(
    "/run",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ALLOC_CREATE"]))],
)
async def run_allocation(
    body: AllocationCreateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Run a new allocation.

    Workflow:
    1. Creates allocation header (DRAFT)
    2. Resolves eligible stores & products
    3. Calculates allocation per store × variant
    4. Applies constraints (min/max/total cap)
    5. Caps at warehouse availability
    6. Saves allocation details

    Basis options:
    - RATIO: Distribute by store grade ratios
    - SALES: Proportional to historical sales
    - STOCK: Fill stores with low stock
    """
    try:
        engine = AllocationEngine(db)
        result = engine.run_allocation(
            allocation_name=body.allocation_name,
            allocation_type=body.allocation_type.value,
            created_by=current_user.username,
            division_id=body.division_id,
            season=body.season,
            basis=body.basis.value,
            gen_article_ids=body.gen_article_ids,
            gen_article_codes=body.gen_article_codes,
            store_codes=body.store_codes,
            store_grades=body.store_grades,
            warehouse_code=body.warehouse_code,
            grade_ratios=body.grade_ratios,
            total_qty_limit=body.total_qty_limit,
            per_store_max=body.per_store_max,
            per_store_min=body.per_store_min,
            size_curve=body.size_curve,
            sales_lookback_days=body.sales_lookback_days,
            ip_address=get_client_ip(request),
        )
        return APIResponse(data=result, message="Allocation run completed")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Allocation failed: {str(e)}")


# ============================================================================
# List Allocations
# ============================================================================

@router.get(
    "",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ALLOC_READ"]))],
)
async def list_allocations(
    status: str = Query(None),
    allocation_type: str = Query(None),
    division_id: int = Query(None),
    season: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """List allocations with filters."""
    query = db.query(AllocationHeader)
    if status:
        query = query.filter(AllocationHeader.status == status)
    if allocation_type:
        query = query.filter(AllocationHeader.allocation_type == allocation_type)
    if division_id:
        query = query.filter(AllocationHeader.division_id == division_id)
    if season:
        query = query.filter(AllocationHeader.season == season)

    total = query.count()
    allocations = (
        query.order_by(AllocationHeader.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return APIResponse(data={
        "allocations": [
            {
                "id": a.id,
                "allocation_code": a.allocation_code,
                "allocation_name": a.allocation_name,
                "allocation_type": a.allocation_type,
                "season": a.season,
                "status": a.status,
                "total_qty": a.total_qty,
                "total_stores": a.total_stores,
                "total_options": a.total_options,
                "created_by": a.created_by,
                "approved_by": a.approved_by,
                "executed_at": a.executed_at.isoformat() if a.executed_at else None,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in allocations
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


# ============================================================================
# Get Allocation Details
# ============================================================================

@router.get(
    "/{allocation_id}/details",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ALLOC_READ"]))],
)
async def get_allocation_details(
    allocation_id: int,
    store_code: str = Query(None),
    size_code: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=5000),
    db: Session = Depends(get_db),
    rls: RLSContext = Depends(get_rls_context),
):
    """
    Get allocation detail rows with pagination.
    RLS filters apply: users only see stores they have access to.
    """
    engine = AllocationEngine(db)

    # Apply RLS: if user has restricted store access, filter
    effective_store_code = store_code
    if not rls.is_unrestricted and store_code:
        if store_code not in rls.accessible_stores:
            raise HTTPException(status_code=403, detail="No access to this store")

    result = engine.get_allocation_details(
        allocation_id=allocation_id,
        page=page,
        page_size=page_size,
        store_code=effective_store_code,
        size_code=size_code,
    )

    # Apply RLS filter on results
    if not rls.is_unrestricted:
        result["details"] = [
            d for d in result["details"]
            if d["store_code"] in rls.accessible_stores
        ]
        result["total"] = len(result["details"])

    return APIResponse(data=result)


# ============================================================================
# Allocation Summary
# ============================================================================

@router.get(
    "/{allocation_id}/summary",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ALLOC_READ"]))],
)
async def get_allocation_summary(
    allocation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Get allocation summary with qty breakdowns by grade, size, color."""
    engine = AllocationEngine(db)
    summary = engine.get_allocation_summary(allocation_id)
    return APIResponse(data=summary)


# ============================================================================
# Apply Manual Overrides
# ============================================================================

@router.post(
    "/{allocation_id}/overrides",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ALLOC_UPDATE"]))],
)
async def apply_overrides(
    allocation_id: int,
    body: AllocationOverrideRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Apply manual qty overrides to allocation detail rows."""
    try:
        engine = AllocationEngine(db)
        result = engine.apply_overrides(
            allocation_id=allocation_id,
            overrides=body.overrides,
            changed_by=current_user.username,
            ip_address=get_client_ip(request),
        )
        return APIResponse(data=result, message=f"Applied {result['applied']} override(s)")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================================
# Approve / Execute / Cancel
# ============================================================================

@router.post(
    "/{allocation_id}/approve",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ALLOC_APPROVE"]))],
)
async def approve_allocation(
    allocation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Approve a DRAFT allocation."""
    try:
        engine = AllocationEngine(db)
        result = engine.approve_allocation(allocation_id, approved_by=current_user.username)
        return APIResponse(data=result, message="Allocation approved")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/{allocation_id}/execute",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ALLOC_EXECUTE"]))],
)
async def execute_allocation(
    allocation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Execute an APPROVED allocation (lock it)."""
    try:
        engine = AllocationEngine(db)
        result = engine.execute_allocation(allocation_id, executed_by=current_user.username)
        return APIResponse(data=result, message="Allocation executed")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/{allocation_id}/cancel",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ALLOC_UPDATE"]))],
)
async def cancel_allocation(
    allocation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cancel an allocation."""
    try:
        engine = AllocationEngine(db)
        result = engine.cancel_allocation(allocation_id, cancelled_by=current_user.username)
        return APIResponse(data=result, message="Allocation cancelled")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================================
# Grid View: Size × Color allocation matrix for a store
# ============================================================================

@router.get(
    "/{allocation_id}/grid/{store_code}",
    response_model=APIResponse,
    dependencies=[Depends(RequirePermissions(["ALLOC_READ"]))],
)
async def get_store_allocation_grid(
    allocation_id: int,
    store_code: str,
    gen_article_id: int = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """
    Get Size × Color allocation grid for a specific store.
    Returns a pivot-style matrix for the data grid UI.
    """
    query = db.query(AllocationDetail).filter(
        AllocationDetail.allocation_id == allocation_id,
        AllocationDetail.store_code == store_code,
    )
    if gen_article_id:
        query = query.filter(AllocationDetail.gen_article_id == gen_article_id)

    details = query.all()

    if not details:
        return APIResponse(data={"grid": [], "sizes": [], "colors": []})

    # Build pivot: rows = colors, columns = sizes
    import pandas as pd
    df = pd.DataFrame([{
        "size_code": d.size_code or "",
        "color_code": d.color_code or "",
        "final_qty": d.final_qty or 0,
        "override_qty": d.override_qty,
        "variant_id": d.variant_id,
    } for d in details])

    sizes = sorted(df["size_code"].unique().tolist())
    colors = sorted(df["color_code"].unique().tolist())

    grid = []
    for color in colors:
        row = {"color_code": color}
        color_data = df[df["color_code"] == color]
        for size in sizes:
            cell = color_data[color_data["size_code"] == size]
            if not cell.empty:
                row[size] = {
                    "qty": int(cell.iloc[0]["final_qty"]),
                    "override": cell.iloc[0]["override_qty"],
                    "variant_id": int(cell.iloc[0]["variant_id"]) if pd.notna(cell.iloc[0]["variant_id"]) else None,
                }
            else:
                row[size] = {"qty": 0, "override": None, "variant_id": None}
        grid.append(row)

    return APIResponse(data={
        "store_code": store_code,
        "allocation_id": allocation_id,
        "sizes": sizes,
        "colors": colors,
        "grid": grid,
        "total_qty": int(df["final_qty"].sum()),
    })
