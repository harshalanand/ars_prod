"""
File Upload & Processing Service
==================================
Handles CSV and Excel file uploads, validation, and routing to the upsert engine.
Supports 1M+ rows via chunked reading.
"""
import os
import uuid
import time
import asyncio
from typing import Optional, List, Dict, Any
from io import BytesIO

import pandas as pd
from loguru import logger

from app.services.upsert_engine import UpsertEngine
from app.core.config import get_settings

settings = get_settings()

# Upload directory
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


class FileUploadService:
    """
    Processes uploaded CSV/Excel files and routes data to the upsert engine.
    """

    ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls"}
    MAX_FILE_SIZE = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024  # bytes

    def __init__(self, db):
        self.db = db
        self.upsert_engine = UpsertEngine(db)

    async def process_upload(
        self,
        file_content: bytes,
        file_name: str,
        table_name: str,
        primary_key_columns: List[str],
        changed_by: str,
        ip_address: Optional[str] = None,
        column_mapping: Optional[Dict[str, str]] = None,
        skip_rows: int = 0,
        sheet_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Process an uploaded file and upsert into the target table.

        Args:
            file_content: Raw file bytes
            file_name: Original filename
            table_name: Target SQL table
            primary_key_columns: PK columns for upsert
            changed_by: Username
            ip_address: Client IP
            column_mapping: Optional {file_col: table_col} mapping
            skip_rows: Number of rows to skip from top
            sheet_name: Excel sheet name (None = first sheet)

        Returns:
            Upload result with stats
        """
        start_time = time.time()
        batch_id = f"UPL_{uuid.uuid4().hex[:10]}"

        # Validate extension
        ext = os.path.splitext(file_name)[1].lower()
        if ext not in self.ALLOWED_EXTENSIONS:
            raise ValueError(f"Unsupported file type: {ext}. Allowed: {self.ALLOWED_EXTENSIONS}")

        # Validate file size
        if len(file_content) > self.MAX_FILE_SIZE:
            raise ValueError(
                f"File too large: {len(file_content) / (1024*1024):.1f}MB. "
                f"Max: {settings.MAX_UPLOAD_SIZE_MB}MB"
            )

        logger.info(f"[{batch_id}] Processing upload: {file_name} ({len(file_content)} bytes) → {table_name}")

        # Save a copy for audit trail
        saved_path = os.path.join(UPLOAD_DIR, f"{batch_id}_{file_name}")
        with open(saved_path, "wb") as f:
            f.write(file_content)

        # Read file into DataFrame — off the event loop (pandas/openpyxl is sync & slow)
        try:
            df = await asyncio.to_thread(
                self._read_file, file_content, ext, skip_rows, sheet_name
            )
        except Exception as e:
            raise ValueError(f"Failed to read file: {e}")

        if df.empty:
            raise ValueError("File contains no data")

        logger.info(f"[{batch_id}] Read {len(df)} rows, {len(df.columns)} columns from {file_name}")

        # Apply column mapping
        if column_mapping:
            df = df.rename(columns=column_mapping)

        # Clean and normalize column names (uppercase, replace special chars with underscores)
        import re
        def normalize_col(c):
            normalized = re.sub(r'[^A-Z0-9_]', '_', str(c).upper().strip())
            normalized = re.sub(r'_+', '_', normalized)  # Collapse multiple underscores
            return normalized.strip('_')  # Remove leading/trailing underscores
        
        df.columns = [normalize_col(c) for c in df.columns]

        # Validate PKs exist (case-insensitive)
        df_cols_upper = {c.upper() for c in df.columns}
        missing_pks = [pk for pk in primary_key_columns if pk.upper() not in df_cols_upper]
        if missing_pks:
            raise ValueError(
                f"Primary key columns not found in file: {missing_pks}. "
                f"Available columns: {list(df.columns)}"
            )

        # Drop rows where PK is null or blank
        pk_null_mask = pd.Series(False, index=df.index)
        for pk in primary_key_columns:
            col = df[pk]
            pk_null_mask = pk_null_mask | col.isna() | (col.astype(str).str.strip() == "")
        null_pk_count = pk_null_mask.sum()
        if null_pk_count > 0:
            logger.warning(f"[{batch_id}] Dropping {null_pk_count} rows with null/blank PKs")
            df = df[~pk_null_mask]

        # Clean data
        df = self._clean_dataframe(df)

        # Ensure PK columns are never __SKIP__ or __NULL__ after cleaning
        for pk in primary_key_columns:
            bad_pk = df[pk].isin(["__SKIP__", "__NULL__"])
            if bad_pk.any():
                logger.warning(f"[{batch_id}] Dropping {bad_pk.sum()} rows with blank/null PK '{pk}'")
                df = df[~bad_pk]

        # Pre-validate data types before upsert — gives users actionable error details.
        # Off the event loop: vectorized pandas, but still seconds on wide frames.
        validation_errors = await asyncio.to_thread(
            self.upsert_engine.validate_data_types,
            table_name,
            df,
            200,
        )
        if validation_errors:
            logger.warning(f"[{batch_id}] {len(validation_errors)} type validation errors found")

        # Execute upsert (proceed even with warnings — TRY_CAST handles gracefully).
        # CRITICAL: must run off the event loop. The upsert is sync pyodbc and
        # holds the loop for minutes — every other API request (Data Checklist,
        # health checks, dashboard, etc.) queues behind it until it returns.
        result = await asyncio.to_thread(
            self.upsert_engine.upsert,
            table_name=table_name,
            df=df,
            primary_key_columns=primary_key_columns,
            changed_by=changed_by,
            source="UPLOAD",
            ip_address=ip_address,
            chunk_size=settings.UPLOAD_CHUNK_SIZE,
            enable_row_audit=True,  # Always enable row-level audit for batch uploads
            collect_sample_changes=False,  # Disable sample-only, log all changes
        )

        # Attach validation errors to result so frontend can display them
        if validation_errors:
            existing = result.get("error_details") or []
            result["validation_errors"] = validation_errors
            result["error_details"] = existing

        # Log DataChangeLog for batch details (insert/update)
        from app.services.audit_service import log_bulk_changes
        batch_id = result.get("batch_id")
        all_row_changes = result.get("all_row_changes", [])
        row_changes = []
        for sc in all_row_changes:
            action_type = sc.get("action_type")
            pk = sc.get("record_primary_key")
            changed_columns = sc.get("changed_columns", {})
            changes = {}
            for col, diff in changed_columns.items():
                changes[col] = {
                    "old": diff.get("old"),
                    "new": diff.get("new"),
                    "type": diff.get("type", "")
                }
            row_changes.append({
                "action_type": action_type,
                "record_key": pk,
                "changes": changes if changes else None,
            })
        if row_changes:
            log_bulk_changes(
                table_name=table_name,
                batch_id=batch_id,
                row_changes=row_changes,
                changed_by=changed_by,
                source="UPLOAD",
            )

        # Add upload-specific info
        result["file_name"] = file_name
        result["file_size_bytes"] = len(file_content)
        result["null_pk_rows_dropped"] = int(null_pk_count)
        result["saved_file"] = saved_path

        duration_ms = int((time.time() - start_time) * 1000)
        result["total_duration_ms"] = duration_ms

        logger.info(
            f"[{batch_id}] Upload complete: {file_name} → {table_name} | "
            f"{result['inserted']} inserted, {result['updated']} updated, "
            f"{result['errors']} errors | {duration_ms}ms"
        )

        return result

    async def process_delete(
        self,
        file_content: bytes,
        file_name: str,
        table_name: str,
        primary_key_columns: List[str],
        changed_by: str,
        ip_address: Optional[str] = None,
        skip_rows: int = 0,
        sheet_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Process an uploaded file and delete matching rows from the target table.

        Args:
            file_content: Raw file bytes
            file_name: Original filename
            table_name: Target SQL table
            primary_key_columns: PK columns to match for deletion
            changed_by: Username
            ip_address: Client IP
            skip_rows: Number of rows to skip from top
            sheet_name: Excel sheet name (None = first sheet)

        Returns:
            Delete result with stats
        """
        start_time = time.time()
        batch_id = f"DEL_{uuid.uuid4().hex[:10]}"

        # Validate extension
        ext = os.path.splitext(file_name)[1].lower()
        if ext not in self.ALLOWED_EXTENSIONS:
            raise ValueError(f"Unsupported file type: {ext}. Allowed: {self.ALLOWED_EXTENSIONS}")

        # Validate file size
        if len(file_content) > self.MAX_FILE_SIZE:
            raise ValueError(
                f"File too large: {len(file_content) / (1024*1024):.1f}MB. "
                f"Max: {settings.MAX_UPLOAD_SIZE_MB}MB"
            )

        logger.info(f"[{batch_id}] Processing delete: {file_name} ({len(file_content)} bytes) → {table_name}")

        # Save a copy for audit trail
        saved_path = os.path.join(UPLOAD_DIR, f"{batch_id}_{file_name}")
        with open(saved_path, "wb") as f:
            f.write(file_content)

        # Read file into DataFrame — off the event loop
        try:
            df = await asyncio.to_thread(
                self._read_file, file_content, ext, skip_rows, sheet_name
            )
        except Exception as e:
            raise ValueError(f"Failed to read file: {e}")

        if df.empty:
            raise ValueError("File contains no data")

        logger.info(f"[{batch_id}] Read {len(df)} rows for deletion from {file_name}")

        # Clean and normalize column names (uppercase, replace special chars with underscores)
        import re
        def normalize_col(c):
            normalized = re.sub(r'[^A-Z0-9_]', '_', str(c).upper().strip())
            normalized = re.sub(r'_+', '_', normalized)  # Collapse multiple underscores
            return normalized.strip('_')  # Remove leading/trailing underscores
        
        df.columns = [normalize_col(c) for c in df.columns]

        # Validate PKs exist (case-insensitive)
        df_cols_upper = {c.upper() for c in df.columns}
        missing_pks = [pk for pk in primary_key_columns if pk.upper() not in df_cols_upper]
        if missing_pks:
            raise ValueError(
                f"Primary key columns not found in file: {missing_pks}. "
                f"Available columns: {list(df.columns)}"
            )

        # Drop rows where PK is null
        pk_null_mask = df[primary_key_columns].isna().any(axis=1)
        null_pk_count = pk_null_mask.sum()
        if null_pk_count > 0:
            logger.warning(f"[{batch_id}] Dropping {null_pk_count} rows with null PKs")
            df = df[~pk_null_mask]

        # Process deletions — entire loop runs off the event loop so concurrent
        # API requests (Data Checklist, dashboards, etc.) stay responsive.
        from sqlalchemy import text as sa_text
        from app.database.session import get_data_engine
        data_engine = get_data_engine()

        def _run_deletes():
            deleted_n = 0
            not_found_n = 0
            errors_n = 0
            errs: List[str] = []
            changes: List[Dict[str, Any]] = []

            for idx, row in df.iterrows():
                try:
                    where_parts = []
                    params: Dict[str, Any] = {}
                    for pk in primary_key_columns:
                        val = row[pk]
                        if pd.isna(val):
                            continue
                        param_name = f"pk_{pk}"
                        where_parts.append(f"[{pk}] = :{param_name}")
                        params[param_name] = val

                    if not where_parts:
                        not_found_n += 1
                        continue

                    where_clause = " AND ".join(where_parts)

                    with data_engine.connect() as data_conn:
                        existing_row = data_conn.execute(
                            sa_text(f"SELECT * FROM [{table_name}] WHERE {where_clause}"),
                            params,
                        ).fetchone()

                        if not existing_row:
                            not_found_n += 1
                            continue

                        data_conn.execute(
                            sa_text(f"DELETE FROM [{table_name}] WHERE {where_clause}"),
                            params,
                        )
                        data_conn.commit()

                    pk_value = "|".join(str(row[pk]) for pk in primary_key_columns)
                    old_data = {k: str(v) if v is not None else None for k, v in existing_row._mapping.items()}
                    changes.append({
                        "action_type": "DELETE",
                        "record_key": pk_value,
                        "changes": None,
                        "old_data": old_data,
                    })
                    deleted_n += 1
                except Exception as e:
                    errors_n += 1
                    errs.append(f"Row {idx + 1}: {str(e)}")
                    logger.error(f"[{batch_id}] Error deleting row {idx + 1}: {e}")

            return deleted_n, not_found_n, errors_n, errs, changes

        total = len(df)
        deleted, not_found, errors, error_details, row_changes = await asyncio.to_thread(_run_deletes)

        # Log DataChangeLog for batch details (delete)
        from app.services.audit_service import log_bulk_changes
        if row_changes:
            log_bulk_changes(
                table_name=table_name,
                batch_id=batch_id,
                row_changes=row_changes,
                changed_by=changed_by,
                source="UPLOAD",
            )

        duration_ms = int((time.time() - start_time) * 1000)

        result = {
            "total": total,
            "deleted": deleted,
            "not_found": not_found,
            "errors": errors,
            "error_details": error_details[:10],  # Limit error details
            "file_name": file_name,
            "file_size_bytes": len(file_content),
            "null_pk_rows_dropped": int(null_pk_count),
            "saved_file": saved_path,
            "total_duration_ms": duration_ms,
        }

        logger.info(
            f"[{batch_id}] Delete complete: {file_name} → {table_name} | "
            f"{deleted} deleted, {not_found} not found, {errors} errors | {duration_ms}ms"
        )

        return result

    def preview_file(
        self,
        file_content: bytes,
        file_name: str,
        rows: int = 20,
        skip_rows: int = 0,
        sheet_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Preview first N rows of an uploaded file (for mapping & validation UI).
        """
        ext = os.path.splitext(file_name)[1].lower()
        df = self._read_file(file_content, ext, skip_rows, sheet_name, nrows=rows)

        return {
            "file_name": file_name,
            "total_columns": len(df.columns),
            "preview_rows": len(df),
            "columns": [
                {
                    "name": str(col),
                    "dtype": str(df[col].dtype),
                    "null_count": int(df[col].isna().sum()),
                    "sample_values": [
                        str(v) if pd.notna(v) else None
                        for v in df[col].head(5).tolist()
                    ],
                }
                for col in df.columns
            ],
            "data": df.head(rows).where(pd.notna(df), None).to_dict(orient="records"),
        }

    def get_sheet_names(self, file_content: bytes, file_name: str) -> List[str]:
        """Get sheet names from an Excel file."""
        ext = os.path.splitext(file_name)[1].lower()
        if ext not in (".xlsx", ".xls"):
            return []

        try:
            xls = pd.ExcelFile(BytesIO(file_content))
            return xls.sheet_names
        except Exception:
            return []

    # ========================================================================
    # INTERNAL HELPERS
    # ========================================================================

    def _read_file(
        self,
        content: bytes,
        ext: str,
        skip_rows: int = 0,
        sheet_name: Optional[str] = None,
        nrows: Optional[int] = None,
    ) -> pd.DataFrame:
        """Read file content into a Pandas DataFrame."""
        buffer = BytesIO(content)

        read_kwargs = {}
        if skip_rows > 0:
            read_kwargs["skiprows"] = skip_rows
        if nrows:
            read_kwargs["nrows"] = nrows

        # keep_default_na=False: prevent pandas from treating "NA" as NaN.
        # "NA" should be stored as the string "NA" in the database.
        # Blank cells are still detected as NaN via our own cleaning logic.
        if ext == ".csv":
            # Try common encodings
            for encoding in ["utf-8", "latin-1", "cp1252"]:
                try:
                    buffer.seek(0)
                    return pd.read_csv(buffer, encoding=encoding,
                                       keep_default_na=False, na_values=[],
                                       **read_kwargs)
                except (UnicodeDecodeError, pd.errors.ParserError):
                    continue
            raise ValueError("Could not read CSV with any supported encoding")

        elif ext in (".xlsx", ".xls"):
            return pd.read_excel(
                buffer,
                sheet_name=sheet_name or 0,
                engine="openpyxl" if ext == ".xlsx" else None,
                keep_default_na=False, na_values=[],
                **read_kwargs,
            )

        raise ValueError(f"Unsupported file extension: {ext}")

    def _clean_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Clean DataFrame before upsert.

        Rules:
        - Blank/empty cells: __SKIP__  (ignore, keep existing DB value)
        - "NA" string:       kept as "NA" (update DB with the string "NA")
        - "|" symbol:        __NULL__  (set DB value to NULL)
        - "-" symbol:        __NULL__  (set DB value to NULL)
        - Everything else:   treated as a normal value
        """
        for col in df.columns:
            raw = df[col].astype(str)
            stripped = raw.str.strip()
            result = raw.copy()
            # Only blank → skip (no change in DB)
            result[stripped.isin(["", "nan", "None", "NaT"])] = "__SKIP__"
            # | and - → NULL in DB
            result[stripped.isin(["|", "-"])] = "__NULL__"
            # Everything else: as-is (including "NA", spaces, etc.)
            df[col] = result

        return df
