"""
Trends Module — manage Trend_* prefixed tables in the data DB.
Provides upload, review, download, schema inspection, and admin operations.
"""
import io
import json
import re
import socket
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine
from app.models.rbac import User
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user

router = APIRouter(prefix="/trends", tags=["Trends"])

TABLE_PREFIX = "Trend_"

SYSTEM_COLUMNS = {
    "VERSION":         "NVARCHAR(50)",
    "REPORT_DATE":     "DATE",
    "UPLOAD_DATETIME": "DATETIME2",
    "SYSTEM_IP":       "NVARCHAR(100)",
    "SYSTEM_NAME":     "NVARCHAR(255)",
    "SYSTEM_LOGIN_ID": "NVARCHAR(100)",
}


# ============================================================================
# Helper Functions
# ============================================================================

def _safe_name(name: str) -> str:
    """Sanitize a table or column name: uppercase, alphanumeric + underscore only."""
    if not name or not name.strip():
        raise HTTPException(400, detail="Name cannot be empty")
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name.strip()).upper()
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        raise HTTPException(400, detail=f"Name '{name}' results in empty string after sanitization")
    if cleaned[0].isdigit():
        cleaned = "_" + cleaned
    return cleaned


def _validate_trend_table(name: str) -> str:
    """Assert table name starts with Trend_ prefix (case-insensitive). Return the validated name."""
    if not name:
        raise HTTPException(400, detail="Table name is required")
    if not name.upper().startswith(TABLE_PREFIX.upper()):
        raise HTTPException(400, detail=f"Table name must start with '{TABLE_PREFIX}'")
    # Ensure the rest is safe
    suffix = name[len(TABLE_PREFIX):]
    if not re.match(r"^[A-Za-z0-9_]+$", suffix):
        raise HTTPException(400, detail=f"Invalid characters in table name: {name}")
    return name


def _infer_sql_type(series: pd.Series, is_pk: bool = False) -> str:
    """Map a pandas Series dtype to a SQL Server type string."""
    dtype = series.dtype
    if pd.api.types.is_integer_dtype(dtype):
        return "INT" if not is_pk else "INT NOT NULL"
    elif pd.api.types.is_float_dtype(dtype):
        return "FLOAT"
    elif pd.api.types.is_bool_dtype(dtype):
        return "BIT"
    elif pd.api.types.is_datetime64_any_dtype(dtype):
        return "DATETIME2"
    else:
        max_len = series.dropna().astype(str).str.len().max()
        if pd.isna(max_len) or max_len == 0:
            max_len = 50
        else:
            max_len = int(max_len)
        if max_len <= 50:
            length = 50
        elif max_len <= 255:
            length = 255
        elif max_len <= 4000:
            length = max(max_len + 50, 500)
        else:
            return "NVARCHAR(MAX)"
        not_null = " NOT NULL" if is_pk else ""
        return f"NVARCHAR({length}){not_null}"


def _get_primary_keys(engine, table_name: str) -> List[str]:
    """Return list of primary-key column names for a table."""
    sql = text("""
        SELECT c.name
        FROM sys.indexes i
        JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
        JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
        WHERE i.is_primary_key = 1
          AND OBJECT_NAME(i.object_id) = :table_name
        ORDER BY ic.key_ordinal
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"table_name": table_name}).fetchall()
    return [r[0] for r in rows]


def _get_next_version(engine, table_name: str) -> str:
    """Calculate the next version string (e.g. '1.1') by incrementing MAX(VERSION) by 0.1."""
    sql = text(f"""
        SELECT MAX(TRY_CAST(VERSION AS FLOAT))
        FROM [{table_name}]
    """)
    try:
        with engine.connect() as conn:
            result = conn.execute(sql).scalar()
        if result is None:
            return "1.0"
        next_ver = round(float(result) + 0.1, 1)
        return f"{next_ver:.1f}"
    except Exception:
        return "1.0"


def _table_exists(engine, table_name: str) -> bool:
    """Check if a table exists in the data DB."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :t"),
            {"t": table_name},
        ).scalar()
    return row > 0


def _column_exists(engine, table_name: str, column_name: str) -> bool:
    """Check if a column exists in a table."""
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = :t AND COLUMN_NAME = :c
            """),
            {"t": table_name, "c": column_name},
        ).scalar()
    return row > 0


def _validate_column_in_table(engine, table_name: str, column_name: str) -> None:
    """Validate that a column exists in a table."""
    if not _column_exists(engine, table_name, column_name):
        raise HTTPException(404, detail=f"Column '{column_name}' not found in table '{table_name}'")


def _read_excel(file_bytes: bytes) -> pd.DataFrame:
    """Read an Excel file, trying openpyxl first, then xlrd."""
    try:
        df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
    except Exception:
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), engine="xlrd")
        except Exception as e:
            raise HTTPException(400, detail=f"Cannot read Excel file: {e}")
    return df


def _clean_headers(df: pd.DataFrame) -> pd.DataFrame:
    """Clean DataFrame column headers: uppercase, replace non-alphanumeric with _, strip."""
    new_cols = []
    for col in df.columns:
        cleaned = re.sub(r"[^A-Za-z0-9_]", "_", str(col).strip()).upper()
        cleaned = re.sub(r"_+", "_", cleaned).strip("_")
        if not cleaned:
            cleaned = "COLUMN"
        new_cols.append(cleaned)
    # Deduplicate column names
    seen = {}
    final_cols = []
    for c in new_cols:
        if c in seen:
            seen[c] += 1
            final_cols.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 0
            final_cols.append(c)
    df.columns = final_cols
    return df


def _get_client_ip(request: Request) -> str:
    """Extract client IP from request."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


# ============================================================================
# 1. GET /trends/tables — List all Trend_ tables
# ============================================================================

@router.get("/tables", response_model=APIResponse)
def list_tables(current_user: User = Depends(get_current_user)):
    """List all tables in data DB whose name starts with Trend_."""
    de = get_data_engine()
    try:
        with de.connect() as conn:
            rows = conn.execute(text("""
                SELECT t.TABLE_NAME,
                       ISNULL(SUM(p.[rows]), 0) AS row_count
                FROM INFORMATION_SCHEMA.TABLES t
                LEFT JOIN sys.partitions p
                    ON OBJECT_ID(t.TABLE_NAME) = p.object_id AND p.index_id IN (0, 1)
                WHERE t.TABLE_TYPE = 'BASE TABLE'
                  AND t.TABLE_NAME LIKE :prefix
                GROUP BY t.TABLE_NAME
                ORDER BY t.TABLE_NAME
            """), {"prefix": TABLE_PREFIX + "%"}).fetchall()

        tables = [{"table_name": r[0], "row_count": int(r[1])} for r in rows]
        return APIResponse(
            success=True,
            message=f"Found {len(tables)} Trend table(s)",
            data={"tables": tables, "total": len(tables)},
        )
    except Exception as e:
        logger.error(f"Error listing trend tables: {e}")
        raise HTTPException(500, detail=f"Failed to list tables: {e}")


# ============================================================================
# 2. GET /trends/tables/{table_name}/schema — Table schema
# ============================================================================

@router.get("/tables/{table_name}/schema", response_model=APIResponse)
def get_table_schema(table_name: str, current_user: User = Depends(get_current_user)):
    """Return columns and primary keys for a Trend_ table."""
    table_name = _validate_trend_table(table_name)
    de = get_data_engine()

    if not _table_exists(de, table_name):
        raise HTTPException(404, detail=f"Table '{table_name}' not found")

    try:
        pk_cols = _get_primary_keys(de, table_name)

        with de.connect() as conn:
            rows = conn.execute(text("""
                SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE,
                       CHARACTER_MAXIMUM_LENGTH
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = :t
                ORDER BY ORDINAL_POSITION
            """), {"t": table_name}).fetchall()

        columns = []
        for r in rows:
            columns.append({
                "name": r[0],
                "data_type": r[1],
                "is_nullable": r[2] == "YES",
                "is_pk": r[0] in pk_cols,
                "max_length": r[3],
            })

        return APIResponse(
            success=True,
            message=f"Schema for {table_name}",
            data={"columns": columns, "primary_keys": pk_cols},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting schema for {table_name}: {e}")
        raise HTTPException(500, detail=f"Failed to get schema: {e}")


# ============================================================================
# 3. GET /trends/tables/{table_name}/distinct/{column} — Distinct values
# ============================================================================

@router.get("/tables/{table_name}/distinct/{column}", response_model=APIResponse)
def get_distinct_values(
    table_name: str,
    column: str,
    current_user: User = Depends(get_current_user),
):
    """Return distinct non-null values for a column (limit 500)."""
    table_name = _validate_trend_table(table_name)
    de = get_data_engine()

    if not _table_exists(de, table_name):
        raise HTTPException(404, detail=f"Table '{table_name}' not found")
    _validate_column_in_table(de, table_name, column)

    safe_col = _safe_name(column)
    try:
        with de.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT DISTINCT TOP 500 [{safe_col}]
                FROM [{table_name}]
                WHERE [{safe_col}] IS NOT NULL
                ORDER BY [{safe_col}]
            """)).fetchall()

        values = [r[0] for r in rows]
        # Convert non-serializable types
        clean_values = []
        for v in values:
            if hasattr(v, "isoformat"):
                clean_values.append(v.isoformat())
            else:
                clean_values.append(v)

        return APIResponse(
            success=True,
            message=f"{len(clean_values)} distinct value(s) for {column}",
            data={"values": clean_values},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting distinct values for {table_name}.{column}: {e}")
        raise HTTPException(500, detail=f"Failed to get distinct values: {e}")


# ============================================================================
# 4. POST /trends/upload — Upload Excel to a Trend_ table
# ============================================================================

@router.post("/upload", response_model=APIResponse)
async def upload_data(
    request: Request,
    file: UploadFile = File(...),
    table_name: str = Form(...),
    report_date: str = Form(...),
    conflict_mode: str = Form("append"),
    current_user: User = Depends(get_current_user),
):
    """Upload Excel data to a Trend_ table."""
    table_name = _validate_trend_table(table_name)

    if conflict_mode not in ("append", "upsert", "replace"):
        raise HTTPException(400, detail="conflict_mode must be 'append', 'upsert', or 'replace'")

    try:
        datetime.strptime(report_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, detail="report_date must be in YYYY-MM-DD format")

    # Read and clean Excel
    try:
        file_bytes = await file.read()
        df = _read_excel(file_bytes)
        df = _clean_headers(df)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, detail=f"Error reading file: {e}")

    if df.empty:
        raise HTTPException(400, detail="Uploaded file contains no data")

    de = get_data_engine()
    version = _get_next_version(de, table_name) if _table_exists(de, table_name) else "1.0"

    # Add system columns
    client_ip = _get_client_ip(request)
    hostname = socket.gethostname()
    df["VERSION"] = version
    df["REPORT_DATE"] = report_date
    df["UPLOAD_DATETIME"] = datetime.now()
    df["SYSTEM_IP"] = client_ip
    df["SYSTEM_NAME"] = hostname
    df["SYSTEM_LOGIN_ID"] = current_user.username

    try:
        with de.connect() as conn:
            # Create table if not exists
            if not _table_exists(de, table_name):
                col_defs = []
                for col in df.columns:
                    if col in SYSTEM_COLUMNS:
                        col_defs.append(f"[{col}] {SYSTEM_COLUMNS[col]}")
                    else:
                        col_defs.append(f"[{col}] {_infer_sql_type(df[col])}")
                create_sql = f"CREATE TABLE [{table_name}] ({', '.join(col_defs)})"
                conn.execute(text(create_sql))
                conn.commit()
                logger.info(f"Created table {table_name}")
            else:
                # Add missing columns
                existing_cols_rows = conn.execute(
                    text("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"),
                    {"t": table_name},
                ).fetchall()
                existing_cols = {r[0].upper() for r in existing_cols_rows}

                for col in df.columns:
                    if col.upper() not in existing_cols:
                        if col in SYSTEM_COLUMNS:
                            sql_type = SYSTEM_COLUMNS[col]
                        else:
                            sql_type = _infer_sql_type(df[col])
                        conn.execute(text(f"ALTER TABLE [{table_name}] ADD [{col}] {sql_type}"))
                        logger.info(f"Added column [{col}] to {table_name}")
                conn.commit()

            # Handle conflict modes
            if conflict_mode == "replace":
                conn.execute(
                    text(f"DELETE FROM [{table_name}] WHERE CONVERT(DATE, REPORT_DATE) = :d"),
                    {"d": report_date},
                )
                conn.commit()
                logger.info(f"Deleted rows for date {report_date} from {table_name}")

            elif conflict_mode == "upsert":
                pk_cols = _get_primary_keys(de, table_name)
                if pk_cols:
                    # Staging table approach: bulk insert → UPDATE → INSERT
                    # Much faster and non-blocking compared to MERGE
                    all_cols = [c for c in df.columns]
                    update_cols = [c for c in all_cols if c not in pk_cols]

                    import uuid as _ut
                    temp_table = f"#temp_{table_name}_{_ut.uuid4().hex[:8]}"
                    df.to_sql(temp_table, de, if_exists="replace", index=False, chunksize=5000)

                    try:
                        pk_join = " AND ".join(
                            f"t.[{pk}] = s.[{pk}]" for pk in pk_cols
                        )

                        # UPDATE existing rows (ROWLOCK = no table lock)
                        updated = 0
                        if update_cols:
                            update_set = ", ".join(f"t.[{c}] = s.[{c}]" for c in update_cols)
                            conn.execute(text(f"""
                                UPDATE t WITH (ROWLOCK) SET {update_set}
                                FROM [{table_name}] t
                                INNER JOIN [{temp_table}] s ON {pk_join}
                            """))
                            updated = conn.connection.cursor().rowcount if hasattr(conn, 'connection') else 0

                        # INSERT new rows (ROWLOCK)
                        insert_cols = ", ".join(f"[{c}]" for c in all_cols)
                        insert_vals = ", ".join(f"s.[{c}]" for c in all_cols)
                        conn.execute(text(f"""
                            INSERT INTO [{table_name}] WITH (ROWLOCK) ({insert_cols})
                            SELECT {insert_vals}
                            FROM [{temp_table}] s
                            WHERE NOT EXISTS (
                                SELECT 1 FROM [{table_name}] t WITH (NOLOCK) WHERE {pk_join}
                            )
                        """))
                        conn.commit()
                    finally:
                        try:
                            conn.execute(text(f"DROP TABLE IF EXISTS [{temp_table}]"))
                            conn.commit()
                        except Exception:
                            pass
                    logger.info(f"Upserted {len(df)} rows into {table_name} (staging approach)")
                    return APIResponse(
                        success=True,
                        message=f"Upserted {len(df)} rows into {table_name}",
                        data={
                            "rows_uploaded": len(df),
                            "version": version,
                            "table_name": table_name,
                        },
                    )
                else:
                    logger.warning(f"No PKs found for {table_name}, falling back to append")

        # Insert data (append or after replace/delete)
        df.to_sql(table_name, de, if_exists="append", index=False, chunksize=1000)
        logger.info(f"Uploaded {len(df)} rows to {table_name} (mode={conflict_mode}, version={version})")

        return APIResponse(
            success=True,
            message=f"Uploaded {len(df)} rows to {table_name}",
            data={
                "rows_uploaded": len(df),
                "version": version,
                "table_name": table_name,
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Upload error for {table_name}: {e}")
        raise HTTPException(500, detail=f"Upload failed: {e}")


# ============================================================================
# 5. POST /trends/upload/check-conflicts — Check existing rows for a date
# ============================================================================

@router.post("/upload/check-conflicts", response_model=APIResponse)
def check_conflicts(
    table_name: str = Form(...),
    report_date: str = Form(...),
    current_user: User = Depends(get_current_user),
):
    """Return count of existing rows for a given REPORT_DATE."""
    table_name = _validate_trend_table(table_name)
    de = get_data_engine()

    if not _table_exists(de, table_name):
        return APIResponse(
            success=True,
            message="Table does not exist yet",
            data={"count": 0, "table_exists": False},
        )

    try:
        with de.connect() as conn:
            count = conn.execute(
                text(f"SELECT COUNT(*) FROM [{table_name}] WHERE CONVERT(DATE, REPORT_DATE) = :d"),
                {"d": report_date},
            ).scalar()

        return APIResponse(
            success=True,
            message=f"{count} existing row(s) for {report_date}",
            data={"count": count, "table_exists": True},
        )
    except Exception as e:
        logger.error(f"Check conflicts error for {table_name}: {e}")
        raise HTTPException(500, detail=f"Failed to check conflicts: {e}")


# ============================================================================
# 6. POST /trends/upload/preview — Preview Excel file
# ============================================================================

@router.post("/upload/preview", response_model=APIResponse)
async def preview_upload(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Read Excel, clean headers, return columns + sample data."""
    try:
        file_bytes = await file.read()
        df = _read_excel(file_bytes)
        df = _clean_headers(df)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, detail=f"Error reading file: {e}")

    columns = []
    for col in df.columns:
        columns.append({
            "name": col,
            "inferred_type": _infer_sql_type(df[col]),
        })

    # Prepare sample data (first 10 rows)
    sample_df = df.head(10).copy()
    # Convert non-serializable values
    for col in sample_df.columns:
        sample_df[col] = sample_df[col].apply(
            lambda x: x.isoformat() if hasattr(x, "isoformat") else x
        )
    sample_df = sample_df.where(pd.notnull(sample_df), None)
    sample_data = sample_df.to_dict(orient="records")

    return APIResponse(
        success=True,
        message=f"Preview: {len(df)} rows, {len(df.columns)} columns",
        data={
            "columns": columns,
            "row_count": len(df),
            "sample_data": sample_data,
        },
    )


# ============================================================================
# 7. POST /trends/review — Query data with filters
# ============================================================================

@router.post("/review", response_model=APIResponse)
def review_data(
    body: Dict[str, Any],
    current_user: User = Depends(get_current_user),
):
    """Query data from a Trend_ table with optional filters."""
    table_name = body.get("table_name")
    if not table_name:
        raise HTTPException(400, detail="table_name is required")
    table_name = _validate_trend_table(table_name)

    date_from = body.get("date_from")
    date_to = body.get("date_to")
    filters: Dict[str, List] = body.get("filters", {})
    limit = body.get("limit", 1000)

    de = get_data_engine()
    if not _table_exists(de, table_name):
        raise HTTPException(404, detail=f"Table '{table_name}' not found")

    try:
        where_clauses = []
        params: Dict[str, Any] = {}

        if date_from:
            where_clauses.append("CONVERT(DATE, REPORT_DATE) >= :date_from")
            params["date_from"] = date_from
        if date_to:
            where_clauses.append("CONVERT(DATE, REPORT_DATE) <= :date_to")
            params["date_to"] = date_to

        # Column-value filters
        filter_idx = 0
        for col_name, values in filters.items():
            safe_col = _safe_name(col_name)
            if not isinstance(values, list) or not values:
                continue
            placeholders = []
            for v in values:
                pname = f"f{filter_idx}"
                placeholders.append(f":{pname}")
                params[pname] = v
                filter_idx += 1
            where_clauses.append(f"[{safe_col}] IN ({', '.join(placeholders)})")

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        # Get total count
        with de.connect() as conn:
            count_sql = f"SELECT COUNT(*) FROM [{table_name}] WITH (NOLOCK) {where_sql}"
            total = conn.execute(text(count_sql), params).scalar()

            # Get data
            data_sql = f"SELECT TOP(:lim) * FROM [{table_name}] WITH (NOLOCK) {where_sql}"
            params["lim"] = int(limit)
            rows = conn.execute(text(data_sql), params)
            columns = list(rows.keys())
            result_rows = rows.fetchall()

        data = []
        for row in result_rows:
            record = {}
            for i, col in enumerate(columns):
                val = row[i]
                if hasattr(val, "isoformat"):
                    val = val.isoformat()
                record[col] = val
            data.append(record)

        return APIResponse(
            success=True,
            message=f"{len(data)} row(s) returned (total: {total})",
            data={"data": data, "total": total, "columns": columns},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Review error for {table_name}: {e}")
        raise HTTPException(500, detail=f"Failed to query data: {e}")


# ============================================================================
# 8. GET /trends/review/{table_name}/download — Download CSV
# ============================================================================

@router.get("/review/{table_name}/download")
def download_data(
    request: Request,
    table_name: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    filters: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Download filtered data as CSV (all rows, no limit)."""
    table_name = _validate_trend_table(table_name)
    de = get_data_engine()

    if not _table_exists(de, table_name):
        raise HTTPException(404, detail=f"Table '{table_name}' not found")

    try:
        where_clauses = []
        params: Dict[str, Any] = {}

        if date_from:
            where_clauses.append("CONVERT(DATE, REPORT_DATE) >= :date_from")
            params["date_from"] = date_from
        if date_to:
            where_clauses.append("CONVERT(DATE, REPORT_DATE) <= :date_to")
            params["date_to"] = date_to

        # Parse filters from JSON string query param
        filter_dict = {}
        if filters:
            try:
                filter_dict = json.loads(filters)
            except json.JSONDecodeError:
                raise HTTPException(400, detail="Invalid JSON in 'filters' parameter")

        filter_idx = 0
        for col_name, values in filter_dict.items():
            safe_col = _safe_name(col_name)
            if not isinstance(values, list) or not values:
                continue
            placeholders = []
            for v in values:
                pname = f"f{filter_idx}"
                placeholders.append(f":{pname}")
                params[pname] = v
                filter_idx += 1
            where_clauses.append(f"[{safe_col}] IN ({', '.join(placeholders)})")

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        with de.connect() as conn:
            rows = conn.execute(text(f"SELECT * FROM [{table_name}] WITH (NOLOCK) {where_sql}"), params)
            columns = list(rows.keys())
            result_rows = rows.fetchall()

        df = pd.DataFrame(result_rows, columns=columns)

        csv_buffer = io.StringIO()
        df.to_csv(csv_buffer, index=False)
        csv_buffer.seek(0)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{table_name}_{timestamp}.csv"

        return StreamingResponse(
            iter([csv_buffer.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Download error for {table_name}: {e}")
        raise HTTPException(500, detail=f"Failed to download data: {e}")


# ============================================================================
# 9. POST /trends/create-table — Create a new Trend_ table from sample file
# ============================================================================

@router.post("/create-table", response_model=APIResponse)
async def create_table(
    request: Request,
    file: UploadFile = File(...),
    table_name: str = Form(...),
    primary_keys: str = Form("[]"),
    column_types: str = Form("{}"),
    upload_data: str = Form("false"),
    current_user: User = Depends(get_current_user),
):
    """Create a new Trend_ table from a sample Excel file."""
    # Auto-prefix if needed
    safe_suffix = _safe_name(table_name)
    full_table_name = f"{TABLE_PREFIX}{safe_suffix}" if not safe_suffix.startswith(TABLE_PREFIX.upper().rstrip("_")) else f"Trend_{safe_suffix}"

    # Parse JSON inputs
    try:
        pk_list: List[str] = json.loads(primary_keys)
    except json.JSONDecodeError:
        raise HTTPException(400, detail="Invalid JSON for primary_keys")
    try:
        type_overrides: Dict[str, str] = json.loads(column_types)
    except json.JSONDecodeError:
        raise HTTPException(400, detail="Invalid JSON for column_types")

    should_upload = upload_data.lower() in ("true", "1", "yes")

    # Read file
    try:
        file_bytes = await file.read()
        df = _read_excel(file_bytes)
        df = _clean_headers(df)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, detail=f"Error reading file: {e}")

    de = get_data_engine()

    if _table_exists(de, full_table_name):
        raise HTTPException(409, detail=f"Table '{full_table_name}' already exists")

    try:
        # Build column definitions
        col_defs = []

        # Data columns from file
        for col in df.columns:
            col_upper = col.upper()
            if col_upper in SYSTEM_COLUMNS:
                continue  # We'll add system columns separately
            is_pk = col in pk_list or col_upper in [p.upper() for p in pk_list]
            if col in type_overrides:
                sql_type = type_overrides[col]
                if is_pk and "NOT NULL" not in sql_type.upper():
                    sql_type += " NOT NULL"
                col_defs.append(f"[{col}] {sql_type}")
            else:
                col_defs.append(f"[{col}] {_infer_sql_type(df[col], is_pk=is_pk)}")

        # System columns
        for sys_col, sys_type in SYSTEM_COLUMNS.items():
            col_defs.append(f"[{sys_col}] {sys_type}")

        # PK constraint
        pk_constraint = ""
        if pk_list:
            safe_pks = ", ".join([f"[{_safe_name(pk)}]" for pk in pk_list])
            pk_constraint = f", CONSTRAINT PK_{full_table_name} PRIMARY KEY ({safe_pks})"

        create_sql = f"CREATE TABLE [{full_table_name}] ({', '.join(col_defs)}{pk_constraint})"

        with de.connect() as conn:
            conn.execute(text(create_sql))
            conn.commit()

        logger.info(f"Created table {full_table_name} with {len(col_defs)} columns")

        rows_uploaded = 0
        if should_upload and not df.empty:
            # Add system columns to DataFrame
            client_ip = _get_client_ip(request)
            hostname = socket.gethostname()
            df["VERSION"] = "1.0"
            df["REPORT_DATE"] = datetime.now().strftime("%Y-%m-%d")
            df["UPLOAD_DATETIME"] = datetime.now()
            df["SYSTEM_IP"] = client_ip
            df["SYSTEM_NAME"] = hostname
            df["SYSTEM_LOGIN_ID"] = current_user.username

            df.to_sql(full_table_name, de, if_exists="append", index=False, chunksize=1000)
            rows_uploaded = len(df)
            logger.info(f"Uploaded {rows_uploaded} sample rows to {full_table_name}")

        return APIResponse(
            success=True,
            message=f"Table '{full_table_name}' created successfully",
            data={
                "table_name": full_table_name,
                "columns": len(col_defs),
                "primary_keys": pk_list,
                "rows_uploaded": rows_uploaded,
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create table error: {e}")
        raise HTTPException(500, detail=f"Failed to create table: {e}")


# ============================================================================
# 10. POST /trends/admin/truncate/{table_name} — Truncate table
# ============================================================================

@router.post("/admin/truncate/{table_name}", response_model=APIResponse)
def truncate_table(table_name: str, current_user: User = Depends(get_current_user)):
    """Truncate a Trend_ table. Falls back to DELETE if FK constraints exist."""
    table_name = _validate_trend_table(table_name)
    de = get_data_engine()

    if not _table_exists(de, table_name):
        raise HTTPException(404, detail=f"Table '{table_name}' not found")

    try:
        with de.connect() as conn:
            try:
                conn.execute(text(f"TRUNCATE TABLE [{table_name}]"))
            except Exception:
                logger.warning(f"TRUNCATE failed for {table_name}, falling back to DELETE")
                conn.execute(text(f"DELETE FROM [{table_name}]"))
            conn.commit()

        logger.info(f"Truncated table {table_name} by user {current_user.username}")
        return APIResponse(
            success=True,
            message=f"Table '{table_name}' truncated successfully",
            data={"table_name": table_name},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Truncate error for {table_name}: {e}")
        raise HTTPException(500, detail=f"Failed to truncate table: {e}")


# ============================================================================
# 11. DELETE /trends/admin/drop/{table_name} — Drop table
# ============================================================================

@router.delete("/admin/drop/{table_name}", response_model=APIResponse)
def drop_table(table_name: str, current_user: User = Depends(get_current_user)):
    """Drop a Trend_ table."""
    table_name = _validate_trend_table(table_name)
    de = get_data_engine()

    if not _table_exists(de, table_name):
        raise HTTPException(404, detail=f"Table '{table_name}' not found")

    try:
        with de.connect() as conn:
            conn.execute(text(f"DROP TABLE [{table_name}]"))
            conn.commit()

        logger.info(f"Dropped table {table_name} by user {current_user.username}")
        return APIResponse(
            success=True,
            message=f"Table '{table_name}' dropped successfully",
            data={"table_name": table_name},
        )
    except Exception as e:
        logger.error(f"Drop error for {table_name}: {e}")
        raise HTTPException(500, detail=f"Failed to drop table: {e}")


# ============================================================================
# 12. PUT /trends/admin/{table_name}/columns — Alter table columns
# ============================================================================

@router.put("/admin/{table_name}/columns", response_model=APIResponse)
def alter_columns(
    table_name: str,
    body: Dict[str, Any],
    current_user: User = Depends(get_current_user),
):
    """Alter table columns: add, drop, rename, or alter type."""
    table_name = _validate_trend_table(table_name)
    de = get_data_engine()

    if not _table_exists(de, table_name):
        raise HTTPException(404, detail=f"Table '{table_name}' not found")

    action = body.get("action")
    if action not in ("add", "drop", "rename", "alter"):
        raise HTTPException(400, detail="action must be 'add', 'drop', 'rename', or 'alter'")

    col_name = body.get("col_name")
    if not col_name:
        raise HTTPException(400, detail="col_name is required")
    safe_col = _safe_name(col_name)

    try:
        with de.connect() as conn:
            if action == "add":
                col_type = body.get("col_type")
                if not col_type:
                    raise HTTPException(400, detail="col_type is required for 'add' action")
                if _column_exists(de, table_name, safe_col):
                    raise HTTPException(409, detail=f"Column '{safe_col}' already exists")
                conn.execute(text(f"ALTER TABLE [{table_name}] ADD [{safe_col}] {col_type}"))
                conn.commit()
                msg = f"Added column [{safe_col}] ({col_type}) to {table_name}"

            elif action == "drop":
                _validate_column_in_table(de, table_name, safe_col)
                # Drop default constraint first if any
                conn.execute(text(f"""
                    DECLARE @con NVARCHAR(256)
                    SELECT @con = dc.name
                    FROM sys.default_constraints dc
                    JOIN sys.columns col ON dc.parent_object_id = col.object_id
                        AND dc.parent_column_id = col.column_id
                    JOIN sys.tables t ON col.object_id = t.object_id
                    WHERE t.name = :t AND col.name = :c
                    IF @con IS NOT NULL
                        EXEC('ALTER TABLE [{table_name}] DROP CONSTRAINT [' + @con + ']')
                """), {"t": table_name, "c": safe_col})
                conn.execute(text(f"ALTER TABLE [{table_name}] DROP COLUMN [{safe_col}]"))
                conn.commit()
                msg = f"Dropped column [{safe_col}] from {table_name}"

            elif action == "rename":
                new_name = body.get("new_name")
                if not new_name:
                    raise HTTPException(400, detail="new_name is required for 'rename' action")
                safe_new = _safe_name(new_name)
                _validate_column_in_table(de, table_name, safe_col)
                conn.execute(
                    text(f"EXEC sp_rename '{table_name}.{safe_col}', '{safe_new}', 'COLUMN'")
                )
                conn.commit()
                msg = f"Renamed column [{safe_col}] to [{safe_new}] in {table_name}"

            elif action == "alter":
                new_type = body.get("new_type")
                if not new_type:
                    raise HTTPException(400, detail="new_type is required for 'alter' action")
                _validate_column_in_table(de, table_name, safe_col)
                conn.execute(text(f"ALTER TABLE [{table_name}] ALTER COLUMN [{safe_col}] {new_type}"))
                conn.commit()
                msg = f"Altered column [{safe_col}] to type {new_type} in {table_name}"

            else:
                raise HTTPException(400, detail=f"Unknown action: {action}")

        logger.info(f"{msg} by user {current_user.username}")
        return APIResponse(success=True, message=msg, data={"table_name": table_name})

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Alter column error for {table_name}: {e}")
        raise HTTPException(500, detail=f"Failed to alter column: {e}")
