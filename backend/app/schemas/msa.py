"""
MSA Stock Calculation Schemas
Request/Response models for MSA filtering, calculation, and pivot operations
"""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


# ============================================================================
# Request Models
# ============================================================================

class MSAFilterRequest(BaseModel):
    """Apply filters to MSA data and load results"""
    date: str = Field(..., description="Selected date (YYYY-MM-DD)")
    filters: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Filter columns and their selected values. Example: {'SLOC': ['DC01', 'DC02'], 'CLR': ['RED']}"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "date": "2026-03-01",
                "filters": {
                    "SLOC": ["DC01", "DC02"],
                    "CLR": ["RED", "BLUE"]
                }
            }
        }


class MSACalculateRequest(BaseModel):
    """Calculate MSA allocation with threshold"""
    slocs: List[str] = Field(..., description="Selected SLOC codes")
    threshold: int = Field(25, ge=0, le=100, description="Minimum allocation percentage (0-100)")
    date: Optional[str] = Field(None, description="Selected date (YYYY-MM-DD) for filtering data")
    filters: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Filter columns and their selected values. Example: {'SLOC': ['DC01', 'DC02'], 'CLR': ['RED']}"
    )
    auto_store_results: bool = Field(True, description="Whether to automatically store results to database")

    class Config:
        json_schema_extra = {
            "example": {
                "slocs": ["DC01", "DC02"],
                "threshold": 25,
                "date": "2026-03-01",
                "filters": {
                    "SLOC": ["DC01", "DC02"],
                    "CLR": ["RED", "BLUE"]
                }
            }
        }


class PivotTableRequest(BaseModel):
    """Generate pivot table from MSA data"""
    index_cols: List[str] = Field(..., description="Column(s) for index (rows)")
    pivot_cols: List[str] = Field(..., description="Column(s) for pivot (columns)")
    value_cols: List[str] = Field(..., description="Column(s) for values")
    agg_funcs: List[str] = Field(default=["sum"], description="Aggregation functions: sum, mean, count, min, max, std")
    fill_zero: bool = Field(True, description="Fill missing values with 0")
    margin_totals: bool = Field(False, description="Add margin totals row/column")

    class Config:
        json_schema_extra = {
            "example": {
                "index_cols": ["ARTICLE_NUMBER"],
                "pivot_cols": ["SLOC"],
                "value_cols": ["STK_Q"],
                "agg_funcs": ["sum"],
                "fill_zero": True,
                "margin_totals": False
            }
        }


class MSARunRequest(BaseModel):
    """Legacy: Run MSA calculation (for backward compatibility)"""
    table: str = Field(..., description="Table or view name")
    filters: Dict[str, Any] = Field(default_factory=dict)
    slocs: Optional[List[str]] = None
    threshold: int = Field(25, ge=0, le=100)


# ============================================================================
# Response Models
# ============================================================================

class DistinctValuesResponse(BaseModel):
    """Distinct values for a column"""
    column: str
    values: List[str] = Field(..., description="List of distinct values")
    total_count: int = Field(..., description="Total number of distinct values")


class InitialDataResponse(BaseModel):
    """Initial data load response (columns and dates)"""
    columns: List[str] = Field(..., description="Available columns for filtering")
    dates: List[str] = Field(..., description="Available dates (sorted DESC)")
    sample_count: int = Field(..., description="Number of available records")


class MSAFilterResponse(BaseModel):
    """Response after applying filters"""
    row_count: int = Field(..., description="Number of rows loaded")
    columns: List[str] = Field(..., description="Column names")
    total_stock_qty: float = Field(..., description="Sum of STK_Q column")
    message: str = Field(..., description="Status message")

    class Config:
        json_schema_extra = {
            "example": {
                "row_count": 5000,
                "columns": ["ARTICLE_NUMBER", "CLR", "SLOC", "STK_Q"],
                "total_stock_qty": 125000.0,
                "message": "Loaded 5000 rows successfully"
            }
        }


class MSACalculateResponse(BaseModel):
    """MSA calculation results"""
    msa: List[Dict[str, Any]] = Field(..., description="MSA base analysis table")
    msa_gen_clr: List[Dict[str, Any]] = Field(..., description="Generated colors analysis")
    msa_gen_clr_var: List[Dict[str, Any]] = Field(..., description="Color variants analysis")
    row_counts: Dict[str, int] = Field(..., description="Row counts for each result set")

    class Config:
        json_schema_extra = {
            "example": {
                "msa": [
                    {"ARTICLE_NUMBER": "ART001", "CLR": "RED", "DC01": 100, "DC02": 50, "STK_QTY": 150}
                ],
                "msa_gen_clr": [
                    {"ARTICLE_NUMBER": "ART001", "CLR": "RED", "DC01": 100, "DC02": 50, "STK_QTY": 150}
                ],
                "msa_gen_clr_var": [
                    {"ARTICLE_NUMBER": "ART001", "CLR": "RED", "DC01": 100, "DC02": 50, "STK_QTY": 150}
                ],
                "row_counts": {
                    "msa": 5000,
                    "msa_gen_clr": 2500,
                    "msa_gen_clr_var": 2500
                }
            }
        }


class PivotTableResponse(BaseModel):
    """Pivot table response"""
    columns: List[str] = Field(..., description="Column names")
    data: List[Dict[str, Any]] = Field(..., description="Pivot table data rows")
    row_count: int = Field(..., description="Number of rows in pivot table")

    class Config:
        json_schema_extra = {
            "example": {
                "columns": ["ARTICLE_NUMBER", "DC01", "DC02", "Total"],
                "data": [
                    {"ARTICLE_NUMBER": "ART001", "DC01": 100, "DC02": 50, "Total": 150}
                ],
                "row_count": 1
            }
        }
