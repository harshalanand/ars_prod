"""
Schemas for Dynamic Table Management & Data Operations
"""
from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


# ============================================================================
# Table Management Schemas
# ============================================================================

class ColumnDefinition(BaseModel):
    column_name: str = Field(..., min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_][a-zA-Z0-9_]*$")
    display_name: Optional[str] = None
    data_type: str = Field(..., description="NVARCHAR, INT, BIGINT, DECIMAL, DATETIME2, BIT, FLOAT, DATE")
    max_length: Optional[int] = Field(None, description="For NVARCHAR/VARCHAR columns")
    is_nullable: bool = True
    is_primary_key: bool = False
    default_value: Optional[str] = None
    column_order: int = 0


class CreateTableRequest(BaseModel):
    table_name: str = Field(..., min_length=1, max_length=200, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    display_name: Optional[str] = None
    description: Optional[str] = None
    module: Optional[str] = None
    columns: List[ColumnDefinition] = Field(..., min_length=1)

    class Config:
        json_schema_extra = {
            "example": {
                "table_name": "store_targets",
                "display_name": "Store Targets",
                "module": "planning",
                "columns": [
                    {"column_name": "store_code", "data_type": "NVARCHAR", "max_length": 20, "is_primary_key": True, "is_nullable": False},
                    {"column_name": "target_qty", "data_type": "INT"},
                    {"column_name": "target_value", "data_type": "DECIMAL", "max_length": 12},
                ]
            }
        }


class AlterTableRequest(BaseModel):
    add_columns: Optional[List[ColumnDefinition]] = None
    drop_columns: Optional[List[str]] = None
    rename_columns: Optional[Dict[str, str]] = None  # old_name: new_name
    # For single-action requests from frontend
    action: Optional[str] = None  # 'add_column', 'drop_column', 'rename_column', 'alter_column'
    column_name: Optional[str] = None
    new_name: Optional[str] = None  # For rename_column
    new_type: Optional[str] = None  # For alter_column
    data_type: Optional[str] = None  # For add_column
    nullable: Optional[bool] = True  # For add_column


class TableMetadataResponse(BaseModel):
    id: int
    table_name: str
    display_name: Optional[str]
    description: Optional[str]
    module: Optional[str]
    is_system_table: bool
    is_active: bool
    row_count: int
    columns: List[Dict[str, Any]]
    created_at: datetime
    created_by: Optional[str]


# ============================================================================
# Upsert / Data Operation Schemas
# ============================================================================

class UpsertRequest(BaseModel):
    """For direct JSON-based upsert (small batches)."""
    table_name: str
    primary_key_columns: List[str]
    records: List[Dict[str, Any]] = Field(..., min_length=1)


class UpsertResponse(BaseModel):
    table_name: str
    total_records: int
    inserted: int
    updated: int
    unchanged: int
    errors: int
    duration_ms: int
    batch_id: str
    changed_columns_summary: Optional[Dict[str, int]] = None  # col_name: count of changes


class BulkUploadResponse(BaseModel):
    table_name: str
    file_name: str
    total_rows: int
    inserted: int
    updated: int
    unchanged: int
    errors: int
    error_details: Optional[List[Dict[str, Any]]] = None
    duration_ms: int
    batch_id: str


class DataQueryRequest(BaseModel):
    table_name: str
    columns: Optional[List[str]] = None  # None = all columns
    filters: Optional[Dict[str, Any]] = None
    order_by: Optional[str] = None
    order_dir: str = "ASC"
    page: int = Field(1, ge=1)
    page_size: int = Field(50, ge=1, le=5000)


class DataDeleteRequest(BaseModel):
    table_name: str
    primary_key_columns: List[str]
    primary_key_values: List[Dict[str, Any]]  # list of PK dicts to delete


class DataUpdateRequest(BaseModel):
    """For inline cell edits (small direct updates)."""
    table_name: str
    primary_key_columns: List[str]
    primary_key_values: Dict[str, Any]
    updates: Dict[str, Any]  # column: new_value
