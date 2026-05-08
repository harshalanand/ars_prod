"""
Allocation Engine Schemas
"""
from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from enum import Enum


class AllocationType(str, Enum):
    INITIAL = "INITIAL"
    REPLENISHMENT = "REPLENISHMENT"
    TRANSFER = "TRANSFER"


class AllocationStatus(str, Enum):
    DRAFT = "DRAFT"
    IN_PROGRESS = "IN_PROGRESS"
    APPROVED = "APPROVED"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"


class AllocationBasis(str, Enum):
    STOCK = "STOCK"        # Based on current stock levels
    SALES = "SALES"        # Based on sales history
    RATIO = "RATIO"        # Fixed ratio per store grade
    MANUAL = "MANUAL"      # Manual override


# ============================================================================
# Allocation Header
# ============================================================================

class AllocationCreateRequest(BaseModel):
    allocation_name: str = Field(..., min_length=3, max_length=300)
    allocation_type: AllocationType
    division_id: Optional[int] = None
    season: Optional[str] = None
    basis: AllocationBasis = AllocationBasis.RATIO

    # Filters for which products/stores to include
    gen_article_ids: Optional[List[int]] = None
    gen_article_codes: Optional[List[str]] = None
    store_codes: Optional[List[str]] = None
    store_grades: Optional[List[str]] = None  # e.g. ["A", "B"]
    warehouse_code: str = "WH001"

    # Allocation parameters
    grade_ratios: Optional[Dict[str, float]] = Field(
        None,
        description="Ratio per grade, e.g. {'A': 1.0, 'B': 0.7, 'C': 0.4}",
    )
    total_qty_limit: Optional[int] = Field(None, description="Max total qty to allocate")
    per_store_max: Optional[int] = Field(None, description="Max qty per store per variant")
    per_store_min: Optional[int] = Field(None, description="Min qty per store per variant")
    size_curve: Optional[Dict[str, float]] = Field(
        None,
        description="Size distribution curve, e.g. {'S': 0.15, 'M': 0.30, 'L': 0.30, 'XL': 0.25}",
    )
    sales_lookback_days: int = Field(30, description="Days of sales history for SALES basis")


class AllocationUpdateRequest(BaseModel):
    allocation_name: Optional[str] = None
    status: Optional[AllocationStatus] = None


class AllocationApproveRequest(BaseModel):
    allocation_id: int
    notes: Optional[str] = None


# ============================================================================
# Allocation Detail (Override)
# ============================================================================

class AllocationOverrideRequest(BaseModel):
    """Manual override for specific store × variant allocation."""
    allocation_id: int
    overrides: List[Dict[str, Any]]  # [{"store_code": "S001", "variant_id": 123, "override_qty": 10}]


# ============================================================================
# Allocation Response
# ============================================================================

class AllocationHeaderResponse(BaseModel):
    id: int
    allocation_code: str
    allocation_name: Optional[str]
    allocation_type: str
    season: Optional[str]
    status: str
    total_qty: int
    total_stores: int
    total_options: int
    created_by: str
    approved_by: Optional[str]
    executed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class AllocationDetailRow(BaseModel):
    store_code: str
    store_grade: Optional[str]
    gen_article_code: Optional[str]
    variant_code: Optional[str]
    size_code: Optional[str]
    color_code: Optional[str]
    allocated_qty: int
    override_qty: Optional[int]
    final_qty: int
    allocation_basis: Optional[str]


class AllocationSummary(BaseModel):
    total_qty: int
    total_stores: int
    total_variants: int
    qty_by_grade: Dict[str, int]
    qty_by_size: Dict[str, int]
    qty_by_color: Dict[str, int]
    top_stores: List[Dict[str, Any]]


class AllocationRunResponse(BaseModel):
    allocation_id: int
    allocation_code: str
    status: str
    summary: AllocationSummary
    duration_ms: int
