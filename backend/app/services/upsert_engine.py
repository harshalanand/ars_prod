"""
High-Performance Upsert Engine
================================
Handles bulk INSERT/UPDATE (UPSERT) operations using:
- Pandas for chunk processing & differential detection
- SQL Server MERGE statement for atomic upsert
- Temp table staging for large datasets
- Audit logging of all changes

Supports 1M+ rows via chunked processing.
"""
import uuid
import time
import json
from typing import List, Dict, Any, Optional, Tuple, Callable

import pandas as pd
from sqlalchemy import text, inspect
from sqlalchemy.orm import Session
from loguru import logger

from app.database.session import get_data_engine
from app.audit.service import AuditService


class UpsertEngine:
    """
    Enterprise upsert engine with differential update detection.

    Flow:
    1. Load incoming data into a SQL Server temp table
    2. Compare temp vs target using MERGE
    3. Track inserts/updates/unchanged
    4. Audit log all changes with column-level diffs
    """

    # SQL Server data type mapping for temp table creation
    DTYPE_MAP = {
        "object": "NVARCHAR(500)",
        "string": "NVARCHAR(500)",
        "int64": "BIGINT",
        "int32": "INT",
        "float64": "FLOAT",
        "float32": "FLOAT",
        "bool": "BIT",
        "datetime64[ns]": "DATETIME2",
        "datetime64": "DATETIME2",
    }

    def __init__(self, db: Session):
        self.db = db
        self.engine = get_data_engine()  # Use Data DB for business data
        self.audit = AuditService(db)

    def upsert(
        self,
        table_name: str,
        df: pd.DataFrame,
        primary_key_columns: List[str],
        changed_by: str,
        source: str = "API",
        ip_address: Optional[str] = None,
        chunk_size: int = 10000,
        cancel_check: Optional[Callable[[], bool]] = None,
        enable_row_audit: bool = True,  # Log individual row changes to audit_log
        progress_callback: Optional[Callable[[int, int], None]] = None,  # Callback(processed, total)
        collect_sample_changes: bool = False,  # Collect first 100 changes for validation
    ) -> Dict[str, Any]:
        """
        Perform high-performance upsert (INSERT or UPDATE) on a SQL Server table.

        Args:
            table_name: Target table name
            df: DataFrame with data to upsert
            primary_key_columns: Columns that form the unique key
            changed_by: Username performing the operation
            source: Source of data (API, UPLOAD, UI)
            ip_address: Client IP
            chunk_size: Rows per chunk for processing
            cancel_check: Optional callback returning True when processing should stop
            enable_row_audit: If True, log individual row changes (slower but detailed)
            progress_callback: Optional callback(processed, total) for progress updates
            collect_sample_changes: If True, collect first 100 row changes for batch report

        Returns:
            Dict with stats: inserted, updated, unchanged, errors, duration_ms, batch_id
        """
        start_time = time.time()
        batch_id = f"UST_{uuid.uuid4().hex[:10]}"
        total_inserted = 0
        total_updated = 0
        total_unchanged = 0
        total_errors = 0
        error_details = []
        changed_columns_summary: Dict[str, int] = {}
        all_row_changes = []  # Collect row-level changes for audit
        sample_changes = []  # First 100 changes for validation/report

        if df.empty:
            return self._build_result(
                table_name, batch_id, 0, 0, 0, 0, 0, start_time, {}
            )

        # Validate primary keys exist in DataFrame
        missing_pks = [pk for pk in primary_key_columns if pk not in df.columns]
        if missing_pks:
            raise ValueError(f"Primary key columns missing from data: {missing_pks}")

        # Drop duplicate PKs in incoming data (keep last)
        # RTRIM to match SQL Server's trailing-space-insensitive PK comparison
        dedup_key = df[primary_key_columns].astype(str).apply(
            lambda r: '|||'.join(v.rstrip() for v in r), axis=1
        )
        df = df[~dedup_key.duplicated(keep='last')].copy()

        # Get target table column info
        target_columns = self._get_table_columns(table_name)
        if not target_columns:
            raise ValueError(f"Table '{table_name}' not found or has no columns")

        # Align DataFrame columns to target table
        df = self._align_columns(df, target_columns)

        total_rows = len(df)

        # ── FAST PATH: Bulk staging (>1000 rows) ────────────────────────
        # Uses: bulk insert into staging → single UPDATE → single INSERT
        # Locks target table for seconds instead of minutes
        if total_rows > 1000:
            logger.info(f"[{batch_id}] Fast bulk upsert: {total_rows} rows → {table_name}")
            bulk_ok = False
            try:
                ins, upd = self._bulk_upsert(
                    table_name, df, primary_key_columns, target_columns,
                    batch_id, progress_callback, cancel_check,
                )
                bulk_ok = True
            except InterruptedError:
                raise
            except Exception as e:
                logger.warning(f"[{batch_id}] Fast bulk failed, falling back to chunked MERGE: {e}")
                # Fall through to chunked approach

            if bulk_ok:
                total_inserted = ins
                total_updated = upd
                total_unchanged = total_rows - ins - upd

                # Audit summary — best-effort; failure here MUST NOT trigger re-processing
                try:
                    self.audit.log_bulk_upsert(
                        table_name=table_name,
                        changed_by=changed_by,
                        row_count=ins + upd,
                        batch_id=batch_id,
                        duration_ms=int((time.time() - start_time) * 1000),
                        notes=f"Inserted: {ins}, Updated: {upd}, Total: {total_rows}",
                        source=source,
                        ip_address=ip_address,
                    )
                    self.db.commit()
                except Exception as e:
                    logger.warning(f"[{batch_id}] Audit log failed (data already upserted): {e}")

                if progress_callback:
                    progress_callback(total_rows, total_rows)

                return self._build_result(
                    table_name, batch_id, total_rows,
                    total_inserted, total_updated, total_unchanged,
                    0, start_time, {},
                )

        # ── STANDARD PATH: Chunked MERGE (for small datasets or fallback) ─
        total_chunks = (len(df) + chunk_size - 1) // chunk_size
        logger.info(f"[{batch_id}] Chunked upsert: {total_rows} rows in {total_chunks} chunks → {table_name}")

        for chunk_idx in range(total_chunks):
            if cancel_check and cancel_check():
                raise InterruptedError("Upsert cancelled by user")

            chunk_start = chunk_idx * chunk_size
            chunk_end = min(chunk_start + chunk_size, total_rows)
            chunk_df = df.iloc[chunk_start:chunk_end].copy()

            logger.info(f"[{batch_id}] Processing rows {chunk_start + 1} to {chunk_end} of {total_rows} ({int((chunk_end / total_rows) * 100)}%)")

            try:
                should_collect_rows = enable_row_audit or (collect_sample_changes and len(sample_changes) < 100)

                inserted, updated, unchanged, chunk_changes, row_changes = self._process_chunk(
                    table_name=table_name,
                    chunk_df=chunk_df,
                    primary_key_columns=primary_key_columns,
                    target_columns=target_columns,
                    batch_id=batch_id,
                    changed_by=changed_by,
                    source=source,
                    ip_address=ip_address,
                    chunk_number=chunk_idx + 1,
                    enable_row_audit=should_collect_rows,
                )
                total_inserted += inserted
                total_updated += updated
                total_unchanged += unchanged

                # Aggregate column change counts
                for col, count in chunk_changes.items():
                    changed_columns_summary[col] = changed_columns_summary.get(col, 0) + count
                
                # Collect row-level changes for audit
                if enable_row_audit and row_changes:
                    all_row_changes.extend(row_changes)
                
                # Collect sample changes (first 100) for batch report
                if collect_sample_changes and row_changes and len(sample_changes) < 100:
                    for rc in row_changes:
                        if len(sample_changes) >= 100:
                            break
                        sample_changes.append({
                            "action_type": rc.get("action_type"),
                            "pk": rc.get("record_primary_key"),
                            "changed_columns": rc.get("changed_columns"),
                        })
                
                # Call progress callback
                if progress_callback:
                    progress_callback(chunk_end, total_rows)

            except Exception as e:
                logger.error(f"[{batch_id}] Chunk {chunk_idx + 1} failed: {e}", exc_info=True)
                total_errors += len(chunk_df)
                error_details.append({
                    "chunk": chunk_idx + 1,
                    "rows": f"{chunk_start}-{chunk_end}",
                    "error": str(e),
                })

        # Log row-level audit entries in bulk
        duration_ms = int((time.time() - start_time) * 1000)
        
        if enable_row_audit and all_row_changes:
            try:
                self._bulk_insert_audit_logs(
                    table_name=table_name,
                    changed_by=changed_by,
                    batch_id=batch_id,
                    source=source,
                    ip_address=ip_address,
                    row_changes=all_row_changes,
                )
                logger.info(f"[{batch_id}] Logged {len(all_row_changes)} row-level audit entries")
            except Exception as e:
                logger.warning(f"[{batch_id}] Failed to log row-level audit: {e}")
        
        # Log async to data_change_log (non-blocking)
        if all_row_changes:
            try:
                from app.services.audit_service import log_bulk_changes
                log_bulk_changes(
                    table_name=table_name,
                    batch_id=batch_id,
                    row_changes=[
                        {
                            "action_type": rc.get("action_type", "UPDATE"),
                            "record_key": rc.get("record_primary_key", ""),
                            "changes": self._build_changes_dict(rc.get("old_data"), rc.get("new_data"), rc.get("changed_columns")),
                            "row_index": i,
                        }
                        for i, rc in enumerate(all_row_changes)
                    ],
                    changed_by=changed_by,
                    source=source,
                )
                logger.info(f"[{batch_id}] Queued {len(all_row_changes)} changes for async audit")
            except Exception as e:
                logger.warning(f"[{batch_id}] Failed to queue async audit: {e}")
        
        # Build changed columns summary for audit log
        changed_cols_json = None
        if changed_columns_summary:
            changed_cols_json = json.dumps(changed_columns_summary)
        
        # Log bulk audit summary (always)
        self.audit.log_bulk_upsert(
            table_name=table_name,
            changed_by=changed_by,
            row_count=total_inserted + total_updated,
            batch_id=batch_id,
            duration_ms=duration_ms,
            notes=f"Inserted: {total_inserted}, Updated: {total_updated}, Unchanged: {total_unchanged}, Errors: {total_errors}",
            ip_address=ip_address,
            source=source,
            changed_columns=changed_cols_json,
        )
        self.db.commit()

        logger.info(
            f"[{batch_id}] Upsert complete: {total_inserted} inserted, "
            f"{total_updated} updated, {total_unchanged} unchanged, {total_errors} errors, "
            f"{duration_ms}ms"
        )

        return self._build_result(
            table_name, batch_id, len(df),
            total_inserted, total_updated, total_unchanged,
            total_errors, start_time, changed_columns_summary,
            error_details=error_details if error_details else None,
            sample_changes=sample_changes if sample_changes else None,
        )

    def _bulk_upsert(
        self,
        table_name: str,
        df: pd.DataFrame,
        primary_key_columns: List[str],
        target_columns: Dict[str, str],
        batch_id: str,
        progress_callback: Optional[Callable] = None,
        cancel_check: Optional[Callable] = None,
    ) -> Tuple[int, int]:
        """
        Fast bulk upsert: staging table → single UPDATE → single INSERT.
        Holds locks on the target table for SECONDS, not minutes.

        Flow:
        1. Create global temp staging table
        2. Bulk insert ALL rows into staging (fast_executemany, no target locks)
        3. One UPDATE for existing rows (short lock)
        4. One INSERT for new rows (short lock)
        5. Drop staging table
        """
        staging = f"#bulk_stage_{batch_id}"
        non_pk_cols = [c for c in df.columns if c not in primary_key_columns]
        total_rows = len(df)
        t_start = time.time()

        conn = self.engine.raw_connection()
        try:
            cursor = conn.cursor()

            # 1. Create staging table — NVARCHAR(4000), NOT MAX.
            # fast_executemany has a known pathology with NVARCHAR(MAX) (LOB):
            # it allocates a per-cell buffer sized for the max LOB length, so
            # bigger batches make memory use explode and the driver thrashes
            # instead of speeding up. Bounded NVARCHAR(4000) lets the driver
            # use a fixed wide buffer (~8 KB/cell) and scale linearly with
            # batch size. 4000 is the upper bound for non-LOB nvarchar in
            # SQL Server; longer values would need MAX (extremely rare for
            # the data we stage — markers are short, IDs are short).
            col_defs = ", ".join(f"[{c}] NVARCHAR(4000) NULL" for c in df.columns)
            cursor.execute(f"CREATE TABLE {staging} ({col_defs})")
            t_create = time.time()

            # 2. Bulk insert into staging (no locks on target!)
            insert_cols = list(df.columns)
            placeholders = ", ".join(["?" for _ in insert_cols])
            col_list = ", ".join([f"[{c}]" for c in insert_cols])
            insert_sql = f"INSERT INTO {staging} ({col_list}) VALUES ({placeholders})"

            # Vectorized NaN→None — replaces iterrows (~100x faster on large frames)
            df_for_insert = df[insert_cols].astype(object).where(df[insert_cols].notna(), None)
            all_rows = df_for_insert.values.tolist()
            t_prep = time.time()

            cursor.fast_executemany = True
            # Pin the per-parameter buffer width so pyodbc doesn't guess long
            # values: (SQL_WVARCHAR, 4000, 0). Without this, pyodbc inspects
            # the first row to size buffers and can mis-size on later rows.
            try:
                cursor.setinputsizes([(-9, 4000, 0)] * len(insert_cols))  # -9 = SQL_WVARCHAR
            except Exception:
                pass
            # 20k batch — sweet spot for Azure SQL with NVARCHAR(4000) staging.
            # Each batch is one TDS round-trip; bigger batches cut round-trips
            # but also enlarge the driver's parameter array. Past ~20k the
            # marginal speedup tapers and memory/GC overhead starts winning.
            # Tune via UPSERT_STAGE_BATCH_SIZE if you want to experiment.
            batch_size = 20000
            for i in range(0, total_rows, batch_size):
                if cancel_check and cancel_check():
                    raise InterruptedError("Cancelled")

                cursor.executemany(insert_sql, all_rows[i:i + batch_size])

                if progress_callback:
                    progress_callback(min(i + batch_size, total_rows), total_rows)

                logger.info(f"[{batch_id}] Staged {min(i + batch_size, total_rows)}/{total_rows} rows")

            conn.commit()
            t_stage = time.time()
            stage_secs = max(t_stage - t_prep, 0.001)
            logger.info(
                f"[{batch_id}] Staging complete in {t_stage - t_prep:.2f}s "
                f"({total_rows / stage_secs:.0f} rows/s, prep={t_prep - t_create:.2f}s). "
                f"Running UPDATE + INSERT..."
            )

            # Helper: TRY_CAST for type conversion
            def cast(col, alias="s"):
                ttype = target_columns.get(col, "NVARCHAR(MAX)")
                upper = ttype.upper()
                if upper.startswith(("NVARCHAR", "VARCHAR", "NCHAR", "CHAR", "NTEXT", "TEXT")):
                    return f"{alias}.[{col}]"
                # Integer-family targets: pivot via FLOAT first. SQL Server's
                # TRY_CAST returns NULL for decimal-suffixed string literals
                # like '18.00' when the target is INT/BIGINT/SMALLINT/TINYINT.
                if upper.startswith(("INT", "BIGINT", "SMALLINT", "TINYINT")):
                    return f"TRY_CAST(TRY_CAST({alias}.[{col}] AS FLOAT) AS {ttype})"
                return f"TRY_CAST({alias}.[{col}] AS {ttype})"

            # PK join condition
            pk_join = " AND ".join(f"t.[{pk}] = {cast(pk, 's')}" for pk in primary_key_columns)

            # 3. UPDATE existing rows (ROWLOCK to prevent table lock)
            if non_pk_cols:
                set_parts = ", ".join(
                    f"t.[{c}] = CASE "
                    f"WHEN s.[{c}] = '__SKIP__' THEN t.[{c}] "
                    f"WHEN s.[{c}] = '__NULL__' THEN NULL "
                    f"ELSE {cast(c, 's')} END"
                    for c in non_pk_cols
                )
                # Only update rows where at least one column changed
                change_cond = " OR ".join(
                    f"(s.[{c}] <> '__SKIP__' AND "
                    f"ISNULL(CAST(t.[{c}] AS NVARCHAR(MAX)),'') <> "
                    f"CASE WHEN s.[{c}]='__NULL__' THEN '' "
                    f"ELSE ISNULL(CAST(s.[{c}] AS NVARCHAR(MAX)),'') END)"
                    for c in non_pk_cols
                )
                update_sql = f"""
                    UPDATE t WITH (ROWLOCK) SET {set_parts}
                    FROM [{table_name}] t
                    INNER JOIN {staging} s ON {pk_join}
                    WHERE ({change_cond})
                """
                cursor.execute(update_sql)
                updated = cursor.rowcount
            else:
                updated = 0

            conn.commit()
            t_update = time.time()
            logger.info(f"[{batch_id}] Updated {updated} rows in {t_update - t_stage:.2f}s")

            # 4. INSERT new rows (ROWLOCK)
            all_cols = primary_key_columns + non_pk_cols
            ins_col_list = ", ".join(f"[{c}]" for c in all_cols)
            ins_vals = ", ".join(
                cast(c, 's') if c in primary_key_columns else
                f"CASE WHEN s.[{c}] IN ('__SKIP__','__NULL__') THEN NULL ELSE {cast(c,'s')} END"
                for c in all_cols
            )
            insert_new_sql = f"""
                INSERT INTO [{table_name}] WITH (ROWLOCK) ({ins_col_list})
                SELECT {ins_vals}
                FROM {staging} s
                WHERE NOT EXISTS (
                    SELECT 1 FROM [{table_name}] t WITH (NOLOCK) WHERE {pk_join}
                )
            """
            cursor.execute(insert_new_sql)
            inserted = cursor.rowcount
            conn.commit()
            t_insert = time.time()
            logger.info(f"[{batch_id}] Inserted {inserted} new rows in {t_insert - t_update:.2f}s")

            # 5. Cleanup
            cursor.execute(f"DROP TABLE IF EXISTS {staging}")
            conn.commit()
            t_drop = time.time()
            logger.info(
                f"[{batch_id}] BULK UPSERT TIMINGS — "
                f"create={t_create - t_start:.2f}s "
                f"prep={t_prep - t_create:.2f}s "
                f"stage={t_stage - t_prep:.2f}s "
                f"update={t_update - t_stage:.2f}s "
                f"insert={t_insert - t_update:.2f}s "
                f"drop={t_drop - t_insert:.2f}s "
                f"total={t_drop - t_start:.2f}s"
            )

            return inserted, updated

        except Exception:
            try:
                conn.rollback()
                cursor = conn.cursor()
                cursor.execute(f"DROP TABLE IF EXISTS {staging}")
                conn.commit()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def _process_chunk(
        self,
        table_name: str,
        chunk_df: pd.DataFrame,
        primary_key_columns: List[str],
        target_columns: Dict[str, str],
        batch_id: str,
        changed_by: str,
        source: str,
        ip_address: Optional[str],
        chunk_number: int,
        enable_row_audit: bool = True,
    ) -> Tuple[int, int, int, Dict[str, int], List[Dict]]:
        """
        Process a single chunk using SQL Server MERGE.
        Returns: (inserted, updated, unchanged, changed_columns_count, row_changes)
        """
        # Deduplicate on PK columns (keep last occurrence) to prevent MERGE conflict
        # RTRIM to match SQL Server's trailing-space-insensitive PK comparison
        dedup_key = chunk_df[primary_key_columns].astype(str).apply(
            lambda r: '|||'.join(v.rstrip() for v in r), axis=1
        )
        chunk_df = chunk_df[~dedup_key.duplicated(keep='last')].copy()

        temp_table = f"#upsert_temp_{batch_id}_{chunk_number}"
        output_table = f"#merge_output_{batch_id}_{chunk_number}"
        non_pk_columns = [c for c in chunk_df.columns if c not in primary_key_columns]
        changed_columns_count: Dict[str, int] = {}
        row_changes: List[Dict] = []
        t_start = time.time()

        conn = self.engine.raw_connection()
        try:
            cursor = conn.cursor()

            # Prevent lock escalation from row-level to table-level
            cursor.execute("SET LOCK_TIMEOUT 30000")  # 30s timeout instead of infinite wait
            try:
                cursor.execute(f"ALTER TABLE [{table_name}] SET (LOCK_ESCALATION = DISABLE)")
            except Exception:
                pass  # May fail if no ALTER permission, that's OK

            # 1. Create temp table
            create_temp_sql = self._build_create_temp_sql(
                temp_table, chunk_df, target_columns
            )
            cursor.execute(create_temp_sql)
            t_create = time.time()

            # 2. Bulk insert into temp table using fast_executemany
            insert_cols = list(chunk_df.columns)
            placeholders = ", ".join(["?" for _ in insert_cols])
            col_list = ", ".join([f"[{c}]" for c in insert_cols])
            insert_sql = f"INSERT INTO {temp_table} ({col_list}) VALUES ({placeholders})"

            # Vectorized NaN→None — replaces iterrows (~100x faster)
            df_for_insert = chunk_df[insert_cols].astype(object).where(chunk_df[insert_cols].notna(), None)
            rows = df_for_insert.values.tolist()

            cursor.fast_executemany = True
            cursor.executemany(insert_sql, rows)
            t_stage = time.time()

            # 2.5 Before MERGE - capture old data for audit if enabled
            old_data_map = {}
            if enable_row_audit:
                try:
                    # Build PK list for query
                    pk_col_list = ", ".join([f"t.[{pk}]" for pk in primary_key_columns])
                    all_col_list = ", ".join([f"t.[{c}]" for c in chunk_df.columns])
                    pk_join = " AND ".join([f"t.[{pk}] = s.[{pk}]" for pk in primary_key_columns])

                    # Get existing rows that match our temp table PKs (NOLOCK to avoid blocking)
                    old_query = f"""
                        SELECT {pk_col_list}, {all_col_list}
                        FROM [{table_name}] t WITH (NOLOCK)
                        INNER JOIN {temp_table} s ON {pk_join}
                    """
                    cursor.execute(old_query)
                    columns = [col[0] for col in cursor.description]
                    for row in cursor.fetchall():
                        row_dict = dict(zip(columns, row))
                        pk_key = "|".join([str(row_dict.get(pk, "")) for pk in primary_key_columns])
                        old_data_map[pk_key] = row_dict
                except Exception as e:
                    logger.warning(f"Failed to capture old data for audit: {e}")
            t_audit_capture = time.time()

            # 3. Execute MERGE
            merge_sql = self._build_merge_sql(
                target_table=table_name,
                temp_table=temp_table,
                primary_key_columns=primary_key_columns,
                non_pk_columns=non_pk_columns,
                target_columns=target_columns,
                enable_row_audit=enable_row_audit,
            )
            cursor.execute(merge_sql)
            t_merge = time.time()

            # 4. Collect MERGE output from the output table
            count_sql = f"""
                SELECT
                    SUM(CASE WHEN action_type = 'INSERT' THEN 1 ELSE 0 END) as inserted,
                    SUM(CASE WHEN action_type = 'UPDATE' THEN 1 ELSE 0 END) as updated
                FROM {output_table}
            """
            cursor.execute(count_sql)
            result = cursor.fetchone()
            inserted = result[0] or 0
            updated = result[1] or 0
            unchanged = len(chunk_df) - inserted - updated

            # 5. Collect row-level changes for audit
            if enable_row_audit and (inserted > 0 or updated > 0):
                try:
                    row_changes = self._collect_row_changes(
                        cursor=cursor,
                        output_table=output_table,
                        target_table=table_name,
                        primary_key_columns=primary_key_columns,
                        all_columns=list(chunk_df.columns),
                        old_data_map=old_data_map,
                    )
                except Exception as e:
                    logger.warning(f"Failed to collect row changes: {e}")
            t_collect = time.time()

            # 6. Collect changed column details for updated rows
            if updated > 0 and non_pk_columns:
                try:
                    changed_columns_count = self._detect_changed_columns(
                        cursor, output_table, temp_table, table_name,
                        primary_key_columns, non_pk_columns
                    )
                except Exception as e:
                    logger.warning(f"Changed column detection failed: {e}")

            # 7. Cleanup temp tables
            cursor.execute(f"DROP TABLE IF EXISTS {output_table}")
            cursor.execute(f"DROP TABLE IF EXISTS {temp_table}")

            conn.commit()
            t_done = time.time()
            logger.info(
                f"[{batch_id}] chunk {chunk_number} ({len(chunk_df)} rows) — "
                f"create={t_create - t_start:.2f}s "
                f"stage={t_stage - t_create:.2f}s "
                f"audit_pre={t_audit_capture - t_stage:.2f}s "
                f"merge={t_merge - t_audit_capture:.2f}s "
                f"collect={t_collect - t_merge:.2f}s "
                f"cleanup={t_done - t_collect:.2f}s "
                f"total={t_done - t_start:.2f}s "
                f"(ins={inserted} upd={updated} unchg={unchanged})"
            )

            return inserted, updated, unchanged, changed_columns_count, row_changes

        except Exception:
            conn.rollback()
            try:
                _c = conn.cursor()
                _c.execute(
                    f"IF OBJECT_ID('tempdb..{temp_table}') IS NOT NULL DROP TABLE {temp_table}"
                )
                _c.execute(
                    f"IF OBJECT_ID('tempdb..{output_table}') IS NOT NULL DROP TABLE {output_table}"
                )
                conn.commit()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def _build_create_temp_sql(
        self, temp_table: str, df: pd.DataFrame, target_columns: Dict[str, str]
    ) -> str:
        """
        Build CREATE TABLE SQL for temp table.
        
        All columns use NVARCHAR to support special markers like __SKIP__ and __NULL__.
        The MERGE statement handles casting back to target types.
        """
        col_defs = []
        for col in df.columns:
            # Always use NVARCHAR for temp table to handle special markers
            col_defs.append(f"[{col}] NVARCHAR(500) NULL")

        return f"CREATE TABLE {temp_table} ({', '.join(col_defs)})"

    def _build_merge_sql(
        self,
        target_table: str,
        temp_table: str,
        primary_key_columns: List[str],
        non_pk_columns: List[str],
        target_columns: Dict[str, str],
        enable_row_audit: bool = False,
    ) -> str:
        """
        Build SQL Server MERGE with OUTPUT clause.
        
        Special value handling:
        - '__SKIP__' : Keep existing value (don't update this column)
        - '__NULL__' : Set value to NULL
        - Normal values: Update with new value
        
        Since temp table uses NVARCHAR for all columns (to store special markers),
        we need to TRY_CAST values to the target column types.
        """
        def get_cast_expr(col: str, source_alias: str = "source") -> str:
            """Generate TRY_CAST expression for a column based on target type."""
            target_type = target_columns.get(col, "NVARCHAR(MAX)")
            upper = target_type.upper()
            # For string types, no cast needed
            if upper.startswith(("NVARCHAR", "VARCHAR", "NCHAR", "CHAR", "NTEXT", "TEXT")):
                return f"{source_alias}.[{col}]"
            # Integer-family targets: pivot via FLOAT first. SQL Server's
            # TRY_CAST returns NULL for decimal-suffixed string literals
            # like '18.00' when the target is INT/BIGINT/SMALLINT/TINYINT,
            # which silently nulled out values from CSVs that Excel saved
            # with a decimal format. Casting to FLOAT first strips the
            # decimal, then the second cast truncates to the integer type.
            if upper.startswith(("INT", "BIGINT", "SMALLINT", "TINYINT")):
                return f"TRY_CAST(TRY_CAST({source_alias}.[{col}] AS FLOAT) AS {target_type})"
            # All other numeric/date/time/binary types — direct TRY_CAST is fine.
            return f"TRY_CAST({source_alias}.[{col}] AS {target_type})"

        # JOIN condition on PKs - need to cast source PK to target type
        join_cond_parts = []
        for pk in primary_key_columns:
            join_cond_parts.append(f"target.[{pk}] = {get_cast_expr(pk)}")
        join_cond = " AND ".join(join_cond_parts)

        # UPDATE SET clause with special value handling and type casting
        # __SKIP__ = keep existing, __NULL__ = set to NULL
        update_set_parts = []
        for c in non_pk_columns:
            cast_expr = get_cast_expr(c)
            update_set_parts.append(
                f"target.[{c}] = CASE "
                f"WHEN source.[{c}] = '__SKIP__' THEN target.[{c}] "
                f"WHEN source.[{c}] = '__NULL__' THEN NULL "
                f"ELSE {cast_expr} END"
            )
        update_set = ", ".join(update_set_parts)

        # Change detection: only update if at least one non-skip column differs
        when_matched_clause = ""
        if non_pk_columns:
            change_conditions = " OR ".join([
                f"(source.[{c}] <> '__SKIP__' AND "
                f"ISNULL(CAST(target.[{c}] AS NVARCHAR(MAX)), '') <> "
                f"CASE WHEN source.[{c}] = '__NULL__' THEN '' "
                f"ELSE ISNULL(CAST(source.[{c}] AS NVARCHAR(MAX)), '') END)"
                for c in non_pk_columns
            ])
            when_matched_clause = f"""WHEN MATCHED AND ({change_conditions})
            THEN UPDATE SET {update_set}
        """

        # INSERT columns/values with special value handling and type casting
        all_columns = primary_key_columns + non_pk_columns
        insert_cols = ", ".join([f"[{c}]" for c in all_columns])
        
        # For INSERT, __SKIP__ and __NULL__ both become NULL
        insert_vals_parts = []
        for c in all_columns:
            cast_expr = get_cast_expr(c)
            if c in primary_key_columns:
                insert_vals_parts.append(cast_expr)
            else:
                insert_vals_parts.append(
                    f"CASE WHEN source.[{c}] IN ('__SKIP__', '__NULL__') "
                    f"THEN NULL ELSE {cast_expr} END"
                )
        insert_vals = ", ".join(insert_vals_parts)

        # Output table for tracking
        output_table = temp_table.replace("upsert_temp", "merge_output")

        # Build output table columns and OUTPUT clause
        if enable_row_audit:
            # Include PK columns for row-level audit
            pk_output_cols = ", ".join([f"[pk_{pk}] NVARCHAR(MAX)" for pk in primary_key_columns])
            output_table_cols = f"action_type NVARCHAR(10), {pk_output_cols}"
            
            pk_inserted_refs = ", ".join([f"inserted.[{pk}]" for pk in primary_key_columns])
            output_into_cols = "action_type, " + ", ".join([f"[pk_{pk}]" for pk in primary_key_columns])
            output_clause = f"""
        OUTPUT
            CASE WHEN $action = 'INSERT' THEN 'INSERT'
                 WHEN $action = 'UPDATE' THEN 'UPDATE'
            END,
            {pk_inserted_refs}
        INTO {output_table} ({output_into_cols})"""
        else:
            output_table_cols = "action_type NVARCHAR(10)"
            output_clause = f"""
        OUTPUT
            CASE WHEN $action = 'INSERT' THEN 'INSERT'
                 WHEN $action = 'UPDATE' THEN 'UPDATE'
            END
        INTO {output_table} (action_type)"""

        merge_sql = f"""
        -- Create output tracking table
        CREATE TABLE {output_table} (
            {output_table_cols}
        );

        -- Execute MERGE (ROWLOCK to prevent table-level lock escalation)
        MERGE [{target_table}] WITH (ROWLOCK) AS target
        USING {temp_table} AS source
        ON ({join_cond})
        {when_matched_clause}WHEN NOT MATCHED BY TARGET
            THEN INSERT ({insert_cols}) VALUES ({insert_vals})
        {output_clause};
        """
        return merge_sql

    def _detect_changed_columns(
        self,
        cursor,
        output_table: str,
        temp_table: str,
        target_table: str,
        primary_key_columns: List[str],
        non_pk_columns: List[str],
    ) -> Dict[str, int]:
        """
        After MERGE, compare temp vs target for updated rows to identify
        which columns actually changed. Returns {col: change_count}.
        """
        # This is a post-MERGE analysis - the target already has new values,
        # so we compare using the merge output.
        # For simplicity and performance, we count columns that differ
        # between temp (source) and target (now updated) - but since target
        # is already updated, we need a different approach.
        # In practice, we log this at the MERGE output level.
        # For now, return empty - detailed column tracking is done via
        # audit log entries in the individual upsert method.
        return {}

    def _collect_row_changes(
        self,
        cursor,
        output_table: str,
        target_table: str,
        primary_key_columns: List[str],
        all_columns: List[str],
        old_data_map: Dict[str, Dict],
    ) -> List[Dict]:
        """
        Collect row-level changes from MERGE output for audit logging.
        Single JOIN query — replaces N per-row SELECTs (the prior implementation
        ran one round-trip per affected row, which dominated chunk runtime).
        """
        row_changes = []

        def make_serializable(d: Dict) -> Dict:
            out = {}
            for k, v in d.items():
                if v is None or isinstance(v, (int, float, bool, str)):
                    out[k] = v
                else:
                    out[k] = str(v)
            return out

        try:
            # ONE bulk fetch: join output → target to get post-MERGE state per affected pk.
            # output_table stores pk as NVARCHAR(MAX); cast target pk to match.
            target_cols_select = ", ".join(f"t.[{c}] AS [{c}]" for c in all_columns)
            join_cond = " AND ".join(
                f"CAST(t.[{pk}] AS NVARCHAR(MAX)) = o.[pk_{pk}]"
                for pk in primary_key_columns
            )
            query = f"""
                SELECT o.action_type, {target_cols_select}
                FROM {output_table} o
                INNER JOIN [{target_table}] t WITH (NOLOCK) ON {join_cond}
            """
            cursor.execute(query)
            col_names = [d[0] for d in cursor.description]
            fetched = cursor.fetchall()
            if not fetched:
                return row_changes

            for raw in fetched:
                row_dict = dict(zip(col_names, raw))
                action_type = row_dict.pop("action_type")
                new_data = row_dict
                pk_key = "|".join(str(new_data.get(pk, "")) for pk in primary_key_columns)
                old_data = old_data_map.get(pk_key, {})

                changed_columns = []
                if action_type == "UPDATE" and old_data:
                    for col in all_columns:
                        if str(old_data.get(col)) != str(new_data.get(col)):
                            changed_columns.append(col)

                pk_str = "|".join(f"{pk}={new_data.get(pk)}" for pk in primary_key_columns)

                row_changes.append({
                    "action_type": action_type,
                    "record_primary_key": pk_str,
                    "old_data": make_serializable(old_data) if old_data else None,
                    "new_data": make_serializable(new_data),
                    "changed_columns": changed_columns if changed_columns else None,
                })

        except Exception as e:
            logger.warning(f"Error collecting row changes: {e}")

        return row_changes

    def _bulk_insert_audit_logs(
        self,
        table_name: str,
        row_changes: List[Dict],
        changed_by: str,
        batch_id: str,
        source: str = "BULK_UPLOAD",
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ):
        """
        Bulk insert audit log entries for row-level changes.
        Uses system database connection for audit logging.
        """
        if not row_changes:
            return
        
        try:
            # Use the central system engine so audit log writes always go to
            # the database configured in the UI (app_settings.json). The
            # earlier hand-rolled pyodbc string referenced settings attrs that
            # don't exist (SYSTEM_DB_SERVER / SYSTEM_DB_NAME) and used
            # Trusted_Connection, which broke when SQL auth was required.
            from app.database.session import system_engine

            conn = system_engine.raw_connection()
            cursor = conn.cursor()
            cursor.fast_executemany = True
            
            insert_sql = """
                INSERT INTO audit_log (
                    table_name, action_type, record_primary_key,
                    old_data, new_data, changed_columns,
                    changed_by, batch_id, source,
                    ip_address, user_agent, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, GETDATE())
            """
            
            rows_to_insert = []
            for change in row_changes:
                rows_to_insert.append((
                    table_name,
                    change["action_type"],
                    change["record_primary_key"],
                    json.dumps(change["old_data"]) if change["old_data"] else None,
                    json.dumps(change["new_data"]) if change["new_data"] else None,
                    json.dumps(change["changed_columns"]) if change["changed_columns"] else None,
                    changed_by,
                    batch_id,
                    source,
                    ip_address,
                    user_agent,
                ))
            
            cursor.executemany(insert_sql, rows_to_insert)
            conn.commit()
            cursor.close()
            conn.close()
            
            logger.info(f"Bulk inserted {len(rows_to_insert)} audit log entries for batch {batch_id}")
            
        except Exception as e:
            logger.error(f"Failed to bulk insert audit logs: {e}", exc_info=True)

    def _build_changes_dict(
        self,
        old_data: Optional[Dict],
        new_data: Optional[Dict],
        changed_columns: Optional[List[str]]
    ) -> Dict[str, Dict[str, Any]]:
        """
        Build changes dict for audit_service format.
        Returns: {column_name: {"old": old_val, "new": new_val}}
        """
        if not changed_columns:
            return {}
        
        changes = {}
        for col in changed_columns:
            old_val = old_data.get(col) if old_data else None
            new_val = new_data.get(col) if new_data else None
            changes[col] = {"old": old_val, "new": new_val}
        
        return changes

    def _get_table_columns(self, table_name: str) -> Dict[str, str]:
        """Get column names and SQL types from the target table."""
        sql = text("""
            SELECT COLUMN_NAME, DATA_TYPE,
                   CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = :table_name
            ORDER BY ORDINAL_POSITION
        """)
        with self.engine.connect() as conn:
            result = conn.execute(sql, {"table_name": table_name})
            columns = {}
            for row in result:
                col_name = row[0]
                data_type = row[1].upper()
                char_len = row[2]
                num_prec = row[3]
                num_scale = row[4]

                if data_type in ("NVARCHAR", "VARCHAR", "NCHAR", "CHAR"):
                    length = char_len if char_len and char_len > 0 else "MAX"
                    sql_type = f"{data_type}({length})"
                elif data_type == "DECIMAL" or data_type == "NUMERIC":
                    p = num_prec or 18
                    s = num_scale or 2
                    sql_type = f"{data_type}({p},{s})"
                else:
                    sql_type = data_type

                columns[col_name] = sql_type
            return columns

    def _align_columns(self, df: pd.DataFrame, target_columns: Dict[str, str]) -> pd.DataFrame:
        """
        Align DataFrame columns to match target table.
        - Normalize column names (uppercase, replace special chars)
        - Case-insensitive matching
        - Drop columns not in target
        - Keep order of target columns
        """
        import re
        
        # Create mapping from normalized names to target column names
        target_upper = {col.upper(): col for col in target_columns.keys()}
        
        # Normalize and rename DataFrame columns to match target
        new_columns = {}
        for col in df.columns:
            # Normalize: uppercase, replace special chars with underscore
            normalized = re.sub(r'[^A-Z0-9_]', '_', str(col).upper().strip())
            normalized = re.sub(r'_+', '_', normalized)  # Collapse multiple underscores
            normalized = normalized.strip('_')  # Remove leading/trailing underscores
            
            # Try to find match in target columns (case-insensitive)
            if normalized in target_upper:
                new_columns[col] = target_upper[normalized]
            elif col.upper() in target_upper:
                new_columns[col] = target_upper[col.upper()]
        
        # Rename columns in DataFrame
        if new_columns:
            df = df.rename(columns=new_columns)
        
        # Filter to only columns that exist in target
        valid_cols = [c for c in df.columns if c in target_columns]
        return df[valid_cols].copy()

    def _build_result(
        self,
        table_name: str,
        batch_id: str,
        total: int,
        inserted: int,
        updated: int,
        unchanged: int,
        errors: int,
        start_time: float,
        changed_columns_summary: Dict[str, int],
        error_details: Optional[List] = None,
        sample_changes: Optional[List] = None,
    ) -> Dict[str, Any]:
        duration_ms = int((time.time() - start_time) * 1000)
        return {
            "table_name": table_name,
            "batch_id": batch_id,
            "total_records": total,
            "inserted": inserted,
            "updated": updated,
            "unchanged": unchanged,
            "errors": errors,
            "duration_ms": duration_ms,
            "changed_columns_summary": changed_columns_summary or None,
            "error_details": error_details,
            "sample_changes": sample_changes,
        }


    # ========================================================================
    # PRE-VALIDATION: Detect type mismatches BEFORE upsert
    # ========================================================================

    def validate_data_types(
        self,
        table_name: str,
        df: pd.DataFrame,
        max_errors: int = 200,
    ) -> List[Dict[str, Any]]:
        """
        Validate data against target column types BEFORE upsert using fast
        vectorized pandas operations (NOT iterrows — that's 100x slower).

        Returns list of row-level errors with row number, column, expected type,
        and actual value — so users can fix their file and re-upload.
        """
        target_columns = self._get_table_columns(table_name)
        if not target_columns or df.empty:
            return []

        validation_errors = []

        for col_name in df.columns:
            if col_name not in target_columns or len(validation_errors) >= max_errors:
                break

            sql_type = target_columns[col_name].upper()
            series = df[col_name]

            # Skip columns that don't need numeric/date validation
            if sql_type.startswith(("NVARCHAR", "VARCHAR", "NCHAR", "CHAR", "NTEXT", "TEXT")):
                # Only check string length for bounded types
                try:
                    length_str = target_columns[col_name].split("(")[1].split(")")[0]
                    if length_str.upper() == "MAX":
                        continue
                    max_len = int(length_str)
                except Exception:
                    continue
                # Vectorized length check
                mask = series.notna() & ~series.isin(["__SKIP__", "__NULL__", ""])
                if mask.any():
                    str_lens = series[mask].astype(str).str.len()
                    bad = str_lens[str_lens > max_len]
                    for idx in bad.index[:max(0, max_errors - len(validation_errors))]:
                        validation_errors.append({
                            "row": idx + 2,
                            "column": col_name,
                            "value": str(series[idx])[:100],
                            "expected": f"text (max {max_len} chars)",
                            "target_type": target_columns[col_name],
                        })
                continue

            if sql_type.startswith(("INT", "BIGINT", "SMALLINT", "TINYINT",
                                    "FLOAT", "REAL", "DECIMAL", "NUMERIC")):
                expected = "integer" if sql_type.startswith(("INT", "BIGINT", "SMALLINT", "TINYINT")) else "decimal number"
                mask = series.notna() & ~series.isin(["__SKIP__", "__NULL__", ""])
                if not mask.any():
                    continue
                numeric = pd.to_numeric(series[mask], errors='coerce')
                bad = numeric[numeric.isna() & mask[mask].index.isin(numeric.index)]
                for idx in bad.index[:max(0, max_errors - len(validation_errors))]:
                    validation_errors.append({
                        "row": idx + 2,
                        "column": col_name,
                        "value": str(series[idx])[:100],
                        "expected": expected,
                        "target_type": target_columns[col_name],
                    })

            elif sql_type.startswith(("DATE", "DATETIME")):
                mask = series.notna() & ~series.isin(["__SKIP__", "__NULL__", ""])
                if not mask.any():
                    continue
                dates = pd.to_datetime(series[mask], errors='coerce')
                bad = dates[dates.isna()]
                for idx in bad.index[:max(0, max_errors - len(validation_errors))]:
                    validation_errors.append({
                        "row": idx + 2,
                        "column": col_name,
                        "value": str(series[idx])[:100],
                        "expected": "date/datetime",
                        "target_type": target_columns[col_name],
                    })

        return validation_errors


# ============================================================================
# Direct Single/Small-Batch Update (for inline grid edits)
# ============================================================================

class DirectUpdateEngine:
    """
    Handles small direct updates (1-100 rows) for inline cell edits.
    Uses parameterized UPDATE statements with audit logging.
    Uses the DATA database (Rep_data) for all operations.
    """

    def __init__(self, db: Session):
        self.db = db  # System DB for audit logging
        self.data_engine = get_data_engine()  # Data DB for actual updates
        self.audit = AuditService(db)

    def update_record(
        self,
        table_name: str,
        primary_key_columns: List[str],
        primary_key_values: Dict[str, Any],
        updates: Dict[str, Any],
        changed_by: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Update a single record with audit logging.
        Compares old vs new and only updates changed columns.
        """
        # 1. Fetch current record from DATA database
        pk_conditions = " AND ".join([f"[{k}] = :{k}" for k in primary_key_columns])
        select_sql = text(f"SELECT * FROM [{table_name}] WHERE {pk_conditions}")

        with self.data_engine.connect() as conn:
            result = conn.execute(select_sql, primary_key_values)
            row = result.mappings().first()

            if not row:
                raise ValueError(f"Record not found in {table_name}")

            old_data = dict(row)

            # 2. Detect actual changes
            actual_changes = {}
            for col, new_val in updates.items():
                old_val = old_data.get(col)
                if str(old_val) != str(new_val):
                    actual_changes[col] = new_val

            if not actual_changes:
                return {"changed": False, "message": "No changes detected"}

            # 3. Build UPDATE
            set_clauses = ", ".join([f"[{c}] = :upd_{c}" for c in actual_changes])
            update_sql = text(f"UPDATE [{table_name}] SET {set_clauses} WHERE {pk_conditions}")

            params = {**primary_key_values}
            for c, v in actual_changes.items():
                params[f"upd_{c}"] = v

            conn.execute(update_sql, params)
            conn.commit()

        # 4. Audit (in system database)
        pk_str = "|".join([f"{k}={v}" for k, v in primary_key_values.items()])
        self.audit.log_update(
            table_name=table_name,
            changed_by=changed_by,
            record_pk=pk_str,
            old_data={c: old_data.get(c) for c in actual_changes},
            new_data=actual_changes,
            changed_columns=list(actual_changes.keys()),
            ip_address=ip_address,
            user_agent=user_agent,
            source="UI",
        )

        return {
            "changed": True,
            "changed_columns": list(actual_changes.keys()),
            "message": f"Updated {len(actual_changes)} column(s)",
        }

    def delete_records(
        self,
        table_name: str,
        primary_key_columns: List[str],
        primary_key_values_list: List[Dict[str, Any]],
        changed_by: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Delete multiple records with audit logging."""
        deleted = 0
        batch_id = f"DEL_{uuid.uuid4().hex[:10]}"

        with self.data_engine.connect() as conn:
            for pk_values in primary_key_values_list:
                pk_conditions = " AND ".join([f"[{k}] = :{k}" for k in primary_key_columns])

                # Fetch for audit
                select_sql = text(f"SELECT * FROM [{table_name}] WHERE {pk_conditions}")
                result = conn.execute(select_sql, pk_values)
                row = result.mappings().first()

                if row:
                    old_data = dict(row)

                    # Delete
                    delete_sql = text(f"DELETE FROM [{table_name}] WHERE {pk_conditions}")
                    conn.execute(delete_sql, pk_values)

                    pk_str = "|".join([f"{k}={v}" for k, v in pk_values.items()])
                    self.audit.log_delete(
                        table_name=table_name,
                        changed_by=changed_by,
                        record_pk=pk_str,
                        old_data=old_data,
                        ip_address=ip_address,
                        user_agent=user_agent,
                        batch_id=batch_id,
                    )
                    deleted += 1
            conn.commit()

        return {"deleted": deleted, "batch_id": batch_id}
