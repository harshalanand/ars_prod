"""
MSA Result Storage Service
Handles storing MSA calculation results into ARS_MSA_TOTAL, ARS_MSA_GEN_ART,
ARS_MSA_VAR_ART tables with sequence tracking and automatic column management
"""
import pandas as pd
import json
from sqlalchemy import text, inspect
from typing import Dict, List, Any, Optional, Tuple
from loguru import logger


class MSAResultStorageService:
    """Service for storing MSA calculation results with sequence tracking"""

    def __init__(self, db):
        """
        Initialize MSAResultStorageService
        
        Args:
            db: SQLAlchemy session (Main DB session, not Data DB)
        """
        self.db = db
        self.result_tables = {
            'msa': 'dbo.ARS_MSA_TOTAL',
            'msa_gen_clr': 'dbo.ARS_MSA_GEN_ART',
            'msa_gen_clr_var': 'dbo.ARS_MSA_VAR_ART'
        }
        self.tracking_table = 'dbo.MSA_Calculation_Sequence'
        self.column_definitions_table = 'dbo.MSA_Column_Definitions'
        self._ensure_tables_exist()

    def _ensure_tables_exist(self):
        """Auto-create ARS_MSA_* tables if they don't exist, or recreate if id has no IDENTITY."""
        try:
            connection = self.db.connection().connection
            cursor = connection.cursor()
            try:
                for key, db_table in self.result_tables.items():
                    table_name = db_table.split('.')[-1]
                    # Check if table exists and id column has IDENTITY
                    cursor.execute(f"""
                        SELECT COLUMNPROPERTY(OBJECT_ID(N'dbo.{table_name}'), 'id', 'IsIdentity')
                    """)
                    row = cursor.fetchone()
                    is_identity = row[0] if row and row[0] is not None else None

                    if is_identity == 1:
                        # Table exists with correct IDENTITY — nothing to do
                        continue

                    # Drop if exists (broken schema) then recreate
                    cursor.execute(f"""
                        IF EXISTS (SELECT * FROM sys.objects
                                   WHERE object_id = OBJECT_ID(N'[dbo].[{table_name}]')
                                   AND type in (N'U'))
                            DROP TABLE [dbo].[{table_name}];
                    """)
                    cursor.execute(f"""
                        CREATE TABLE [dbo].[{table_name}] (
                            [id] INT PRIMARY KEY IDENTITY(1,1),
                            [sequence_id] INT NOT NULL
                        );
                    """)
                    cursor.execute(f"""
                        CREATE NONCLUSTERED INDEX [IX_{table_name}_sequence_id]
                            ON [dbo].[{table_name}]([sequence_id]);
                    """)
                    logger.info(f"Created table dbo.{table_name} with IDENTITY")
                connection.commit()
                logger.info("ARS_MSA_* tables verified/created")
            finally:
                cursor.close()
        except Exception as e:
            logger.warning(f"Could not auto-create ARS tables: {e}")

    def fix_column_types(self, table_name: str, data: List[Dict] = None) -> int:
        """Fix existing VARCHAR(MAX) columns to proper types (FLOAT for numeric, NVARCHAR for text).
        Call this before inserting new data to ensure correct types."""
        db_table = self.result_tables.get(table_name)
        if not db_table:
            return 0
        tbl = db_table.split('.')[-1]
        fixed = 0
        try:
            connection = self.db.connection().connection
            cursor = connection.cursor()
            try:
                # Get all VARCHAR(MAX) columns
                cursor.execute(f"""
                    SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_NAME = '{tbl}' AND DATA_TYPE = 'varchar'
                    AND CHARACTER_MAXIMUM_LENGTH = -1
                    AND COLUMN_NAME NOT IN ('id', 'sequence_id')
                """)
                varchar_cols = [r[0] for r in cursor.fetchall()]
                if not varchar_cols:
                    return 0

                for col in varchar_cols:
                    new_type = self._infer_sql_type(col, data or [])
                    if new_type == "FLOAT":
                        # Convert: update NULLs/blanks to NULL, then ALTER
                        try:
                            cursor.execute(f"""
                                UPDATE {db_table}
                                SET [{col}] = NULL
                                WHERE [{col}] = '' OR [{col}] IS NULL
                            """)
                            cursor.execute(f"""
                                ALTER TABLE {db_table}
                                ALTER COLUMN [{col}] FLOAT NULL
                            """)
                            fixed += 1
                            logger.info(f"Fixed {tbl}.{col}: VARCHAR(MAX) -> FLOAT")
                        except Exception as e:
                            logger.warning(f"Could not convert {tbl}.{col} to FLOAT: {e}")
                    else:
                        try:
                            cursor.execute(f"""
                                ALTER TABLE {db_table}
                                ALTER COLUMN [{col}] NVARCHAR(200) NULL
                            """)
                            fixed += 1
                            logger.info(f"Fixed {tbl}.{col}: VARCHAR(MAX) -> NVARCHAR(200)")
                        except Exception as e:
                            logger.warning(f"Could not convert {tbl}.{col} to NVARCHAR: {e}")

                connection.commit()
                logger.info(f"Fixed {fixed}/{len(varchar_cols)} columns in {tbl}")
            finally:
                cursor.close()
        except Exception as e:
            logger.error(f"Error fixing column types: {e}")
        return fixed

    # ========================================================================
    # Sequence Management
    # ========================================================================

    def get_last_sequence_id(self) -> int:
        """
        Get the last sequence ID from the tracking table
        
        Returns:
            Last sequence_id (0 if no sequences yet)
        """
        try:
            sql = f"SELECT MAX(sequence_id) as max_seq FROM {self.tracking_table}"
            result = pd.read_sql(text(sql), self.db.bind)
            last_seq = int(result['max_seq'].iloc[0]) if result['max_seq'].iloc[0] is not None else 0
            logger.info(f"Last sequence ID: {last_seq}")
            return last_seq
        except Exception as e:
            logger.warning(f"Error getting last sequence ID: {e}, returning 0")
            return 0

    def create_sequence_record(
        self,
        date_filter: str,
        filter_columns: List[str],
        filters: Dict[str, List[str]],
        threshold: int,
        slocs: List[str],
        msa_row_count: int,
        gen_color_row_count: int,
        color_variant_row_count: int,
        created_by: str = "system"
    ) -> int:
        """
        Create a sequence record for this calculation
        
        Args:
            date_filter: Date filter applied
            filter_columns: List of filter columns
            filters: Dict of filter values
            threshold: Threshold percentage
            slocs: List of SLOC codes
            msa_row_count: Row count for MSA results
            gen_color_row_count: Row count for generated colors
            color_variant_row_count: Row count for color variants
            created_by: User who triggered calculation
        
        Returns:
            New sequence_id
        """
        try:
            # Get the raw pyodbc connection from SQLAlchemy
            connection = self.db.connection().connection
            cursor = connection.cursor()
            
            try:
                # Simple approach: INSERT, then query max sequence_id
                insert_sql = f"""
                INSERT INTO {self.tracking_table}
                (date_filter, filter_columns, filters, threshold, slocs, 
                 msa_row_count, gen_color_row_count, color_variant_row_count,
                 created_by, status)
                VALUES
                (?, ?, ?, ?, ?,
                 ?, ?, ?,
                 ?, 'COMPLETED')
                """
                
                # Execute the INSERT
                cursor.execute(
                    insert_sql,
                    (
                        date_filter,
                        json.dumps(filter_columns),
                        json.dumps(filters),
                        int(threshold),
                        json.dumps(slocs),
                        int(msa_row_count),
                        int(gen_color_row_count),
                        int(color_variant_row_count),
                        created_by
                    )
                )
                
                # Commit the insert
                connection.commit()
                
                # Now get the max sequence_id (which should be the one we just inserted)
                cursor.execute(f"SELECT MAX(sequence_id) FROM {self.tracking_table}")
                result = cursor.fetchone()
                sequence_id = int(result[0]) if result and result[0] is not None else None
                
                if sequence_id:
                    logger.info(f"✅ Created sequence record: {sequence_id}")
                    return sequence_id
                else:
                    raise ValueError("Could not retrieve sequence_id after insert")
                    
            except Exception as e:
                try:
                    connection.rollback()
                except Exception:
                    pass
                raise
            finally:
                cursor.close()
                
        except Exception as e:
            logger.error(f"❌ Error creating sequence record: {e}")
            raise

    # ========================================================================
    # Column Management
    # ========================================================================

    def get_existing_columns(self, table_name: str) -> List[str]:
        """
        Get all existing column names for a table
        
        Args:
            table_name: Table name (msa, msa_gen_clr, msa_gen_clr_var)
        
        Returns:
            List of column names
        """
        try:
            db_table = self.result_tables.get(table_name)
            if not db_table:
                logger.warning(f"Unknown table name: {table_name}")
                return []
            
            # Get columns from information schema
            sql = f"""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = '{db_table.split('.')[-1]}'
            AND TABLE_SCHEMA = 'dbo'
            ORDER BY ORDINAL_POSITION
            """
            
            df = pd.read_sql(text(sql), self.db.bind)
            columns = df['COLUMN_NAME'].tolist()
            logger.debug(f"Existing columns in {db_table}: {columns}")
            return columns
        except Exception as e:
            logger.warning(f"Error getting existing columns for {table_name}: {e}")
            return ['id', 'sequence_id']

    # ── Column type mapping: actual SQL types per column name ──────────
    # BIGINT: numeric identifiers (article numbers, material numbers)
    # NVARCHAR: text codes and descriptions
    # FLOAT: quantities, stock values, percentages
    _KNOWN_TYPES = {
        # Numeric IDs → BIGINT
        "ARTICLE_NUMBER": "BIGINT", "MATNR": "BIGINT",
        "GEN_ART_NUMBER": "BIGINT",
        # Text codes → NVARCHAR
        "RDC": "NVARCHAR(50)", "ST_CD": "NVARCHAR(50)",
        "WERKS": "NVARCHAR(50)", "SLOC": "NVARCHAR(50)",
        "MAJ_CAT": "NVARCHAR(100)", "CLR": "NVARCHAR(100)",
        "DIV": "NVARCHAR(100)", "SZ": "NVARCHAR(50)",
        "FAB": "NVARCHAR(100)", "RNG_SEG": "NVARCHAR(100)",
        "MACRO_MVGR": "NVARCHAR(100)", "MICRO_MVGR": "NVARCHAR(100)",
        "VND_CD": "NVARCHAR(100)", "M_VND_CD": "NVARCHAR(100)",
        "ARTICLE_DESC": "NVARCHAR(500)", "GEN_ART_DESC": "NVARCHAR(500)",
    }

    @staticmethod
    def _infer_sql_type(col_name: str, data: List[Dict]) -> str:
        """Infer SQL type from column name and actual data values."""
        upper = col_name.upper()

        # 1. Check known types first
        if upper in MSAResultStorageService._KNOWN_TYPES:
            return MSAResultStorageService._KNOWN_TYPES[upper]

        # 2. Sample up to 50 non-null values to detect numeric vs text
        sample = []
        for row in data:
            v = row.get(col_name)
            if v is not None:
                sample.append(v)
                if len(sample) >= 50:
                    break

        if not sample:
            return "NVARCHAR(200)"

        all_numeric = True
        has_decimal = False
        for v in sample:
            if isinstance(v, float):
                has_decimal = True
                continue
            if isinstance(v, int):
                continue
            try:
                fv = float(v)
                if '.' in str(v):
                    has_decimal = True
            except (ValueError, TypeError):
                all_numeric = False
                break

        if all_numeric:
            return "FLOAT"
        return "NVARCHAR(200)"

    def get_new_columns(self, table_name: str, data: List[Dict]) -> List[str]:
        """
        Identify new columns in the data that don't exist in the table

        Args:
            table_name: Table name (msa, msa_gen_clr, msa_gen_clr_var)
            data: List of dictionaries with result data

        Returns:
            List of new column names
        """
        if not data:
            return []

        existing = set(self.get_existing_columns(table_name))
        data_columns = set(data[0].keys()) if data else set()
        reserved_columns = {'id', 'sequence_id'}

        new_columns = list(data_columns - existing - reserved_columns)

        if new_columns:
            logger.info(f"New columns detected in {table_name}: {new_columns}")

        return new_columns

    def create_columns(self, table_name: str, new_columns: List[str], sequence_id: int,
                       data: List[Dict] = None) -> None:
        """
        Create new columns in the result table with proper data types.
        Infers FLOAT for numeric data, NVARCHAR(200) for text.
        """
        if not new_columns:
            return

        try:
            db_table = self.result_tables.get(table_name)
            if not db_table:
                logger.error(f"Unknown table name: {table_name}")
                return

            connection = self.db.connection().connection
            cursor = connection.cursor()

            try:
                tbl_name_short = db_table.split('.')[-1]
                # Batch-fetch existing columns once instead of N queries
                cursor.execute(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_NAME = ?", (tbl_name_short,)
                )
                existing_set = {r[0] for r in cursor.fetchall()}

                for col_name in new_columns:
                    try:
                        sql_type = self._infer_sql_type(col_name, data or [])

                        if col_name not in existing_set:
                            cursor.execute(f"ALTER TABLE {db_table} ADD [{col_name}] {sql_type} NULL")
                            logger.info(f"Created column {db_table}.{col_name} ({sql_type})")

                        # Record in column definitions
                        try:
                            cursor.execute(
                                f"INSERT INTO {self.column_definitions_table} "
                                f"(table_name, column_name, column_type, first_sequence_id) "
                                f"VALUES (?, ?, ?, ?)",
                                (table_name, col_name, sql_type, sequence_id)
                            )
                        except Exception:
                            pass  # duplicate col def entry is fine

                    except Exception as col_err:
                        logger.warning(f"Could not create column {col_name}: {col_err}")

                connection.commit()
                logger.info(f"All columns created for {table_name}")

            except Exception as e:
                try:
                    connection.rollback()
                except Exception:
                    pass
                raise
            finally:
                cursor.close()

        except Exception as e:
            logger.error(f"Error in create_columns: {e}")
            raise

    # ========================================================================
    # Data Storage
    # ========================================================================

    def store_results(
        self,
        calculation_results: Dict[str, Any],
        date_filter: str,
        filter_columns: List[str],
        filters: Dict[str, List[str]],
        threshold: int,
        slocs: List[str],
        created_by: str = "system"
    ) -> Dict[str, Any]:
        """
        Store MSA calculation results to database with sequence tracking
        
        Args:
            calculation_results: Dict with keys: msa, msa_gen_clr, msa_gen_clr_var, row_counts
            date_filter: Date filter applied
            filter_columns: List of filter columns
            filters: Dict of filter values
            threshold: Threshold percentage
            slocs: List of SLOC codes
            created_by: User who triggered calculation
        
        Returns:
            Dict with sequence_id and storage info
        """
        try:
            logger.info(f"📦 Starting MSA result storage...")
            
            # Extract results
            msa_data = calculation_results.get('msa', [])
            msa_gen_clr_data = calculation_results.get('msa_gen_clr', [])
            msa_gen_clr_var_data = calculation_results.get('msa_gen_clr_var', [])
            row_counts = calculation_results.get('row_counts', {})
            
            logger.info(f"   MSA: {len(msa_data)} rows")
            logger.info(f"   Generated Colors: {len(msa_gen_clr_data)} rows")
            logger.info(f"   Color Variants: {len(msa_gen_clr_var_data)} rows")
            
            # Create sequence record first
            sequence_id = self.create_sequence_record(
                date_filter=date_filter,
                filter_columns=filter_columns,
                filters=filters,
                threshold=threshold,
                slocs=slocs,
                msa_row_count=len(msa_data),
                gen_color_row_count=len(msa_gen_clr_data),
                color_variant_row_count=len(msa_gen_clr_var_data),
                created_by=created_by
            )
            
            storage_info = {
                'sequence_id': sequence_id,
                'msa_stored': False,
                'gen_color_stored': False,
                'color_variant_stored': False,
                'errors': []
            }
            
            # Store each result set
            if msa_data:
                try:
                    self._store_table_data('msa', msa_data, sequence_id)
                    storage_info['msa_stored'] = True
                except Exception as e:
                    storage_info['errors'].append(f"MSA storage error: {str(e)}")
                    logger.error(f"❌ Error storing MSA data: {e}")
            
            if msa_gen_clr_data:
                try:
                    self._store_table_data('msa_gen_clr', msa_gen_clr_data, sequence_id)
                    storage_info['gen_color_stored'] = True
                except Exception as e:
                    storage_info['errors'].append(f"Generated color storage error: {str(e)}")
                    logger.error(f"❌ Error storing generated color data: {e}")
            
            if msa_gen_clr_var_data:
                try:
                    self._store_table_data('msa_gen_clr_var', msa_gen_clr_var_data, sequence_id)
                    storage_info['color_variant_stored'] = True
                except Exception as e:
                    storage_info['errors'].append(f"Color variant storage error: {str(e)}")
                    logger.error(f"❌ Error storing color variant data: {e}")
            
            logger.info(f"✅ Result storage complete: sequence {sequence_id}")
            return storage_info
        except Exception as e:
            logger.error(f"❌ Error in store_results: {e}")
            raise

    def _store_table_data(self, table_name: str, data: List[Dict], sequence_id: int) -> int:
        try:
            if not data:
                logger.info(f"No data to store for {table_name}")
                return 0

            db_table = self.result_tables.get(table_name)
            if not db_table:
                raise ValueError(f"Unknown table name: {table_name}")

            # Fix existing VARCHAR(MAX) columns to proper types
            self.fix_column_types(table_name, data)

            # Detect new columns and create with proper types
            new_columns = self.get_new_columns(table_name, data)
            if new_columns:
                logger.info(f"Creating {len(new_columns)} new columns in {table_name}")
                self.create_columns(table_name, new_columns, sequence_id, data=data)

            existing_columns = self.get_existing_columns(table_name)

            # Build case-insensitive data key map (DB col name → data key) — single pass
            sample_keys = set(data[0].keys()) if data else set()
            data_keys_lower = {k.lower(): k for k in sample_keys}
            key_map = {
                col: (col if col in sample_keys else data_keys_lower.get(col.lower()))
                for col in existing_columns
                if col not in ('id', 'sequence_id')
            }
            # Remove unmatched entries
            key_map = {col: dk for col, dk in key_map.items() if dk is not None}

            logger.info(f"Column mapping: {len(key_map)}/{len(existing_columns)-2} matched, "
                        f"data keys: {list(sample_keys)[:8]}")

            # Prepare rows
            rows_to_insert = []
            for row in data:
                insert_row = {'sequence_id': sequence_id}
                for col in existing_columns:
                    if col in ['id', 'sequence_id']:
                        continue
                    data_key = key_map.get(col)
                    insert_row[col] = row.get(data_key) if data_key else None
                rows_to_insert.append(insert_row)

            # Single raw connection for TRUNCATE + INSERT
            connection = self.db.connection().connection
            cursor = connection.cursor()

            # Clear old data first (same connection as insert)
            try:
                cursor.execute(f"TRUNCATE TABLE {db_table}")
                connection.commit()
                logger.info(f"Truncated {db_table}")
            except Exception:
                try:
                    connection.rollback()
                    cursor.execute(f"DELETE FROM {db_table}")
                    connection.commit()
                    logger.info(f"Deleted all from {db_table}")
                except Exception as e2:
                    logger.error(f"Could not clear {db_table}: {e2}")

            try:
                # IMPORTANT: enable fast executemany
                cursor.fast_executemany = True

                column_list = [
                    col for col in existing_columns
                    if col not in ['id']
                ]

                if 'sequence_id' not in column_list:
                    column_list.insert(0, 'sequence_id')

                column_names = ', '.join([f'[{c}]' for c in column_list])
                placeholders = ', '.join(['?' for _ in column_list])

                insert_sql = f"""
                INSERT INTO {db_table}
                ({column_names})
                VALUES ({placeholders})
                """

                batch_size = 20000
                total_inserted = 0

                for i in range(0, len(rows_to_insert), batch_size):
                    batch = rows_to_insert[i:i + batch_size]

                    values_list = [
                        tuple(row_data.get(col) for col in column_list)
                        for row_data in batch
                    ]

                    cursor.executemany(insert_sql, values_list)
                    total_inserted += len(batch)

                    logger.info(
                        f"📊 Inserted {total_inserted}/{len(rows_to_insert)} rows into {table_name}"
                    )

                # SINGLE COMMIT
                connection.commit()

                logger.info(f"✅ Stored {total_inserted} rows in {table_name}")
                return total_inserted

            except Exception as e:
                connection.rollback()
                raise

            finally:
                cursor.close()

        except Exception as e:
            logger.error(f"❌ Error storing table data for {table_name}: {e}")
            raise
    # ========================================================================
    # Retrieval Methods
    # ========================================================================

    def get_sequence_data(
        self,
        sequence_id: int,
        table_name: str
    ) -> Tuple[List[Dict], Dict[str, Any]]:
        """
        Retrieve stored data for a specific sequence and table
        
        Args:
            sequence_id: Sequence ID
            table_name: Table name (msa, msa_gen_clr, msa_gen_clr_var)
        
        Returns:
            Tuple of (data list, metadata dict)
        """
        try:
            # Get sequence metadata
            meta_sql = f"""
            SELECT sequence_id, calculation_date, date_filter, filter_columns,
                   filters, threshold, slocs, msa_row_count, gen_color_row_count,
                   color_variant_row_count, created_by, created_at, status
            FROM {self.tracking_table}
            WHERE sequence_id = :seq_id
            """
            
            meta_df = pd.read_sql(text(meta_sql), self.db.bind, params={'seq_id': sequence_id})
            
            if meta_df.empty:
                logger.warning(f"No sequence found for ID: {sequence_id}")
                return [], {}
            
            meta = meta_df.iloc[0].to_dict()
            
            # Parse JSON fields
            meta['filter_columns'] = json.loads(meta['filter_columns']) if meta['filter_columns'] else []
            meta['filters'] = json.loads(meta['filters']) if meta['filters'] else {}
            meta['slocs'] = json.loads(meta['slocs']) if meta['slocs'] else []
            
            # Get result data
            db_table = self.result_tables.get(table_name)
            if not db_table:
                raise ValueError(f"Unknown table name: {table_name}")
            
            data_sql = f"""
            SELECT *
            FROM {db_table}
            WHERE sequence_id = :seq_id
            ORDER BY id
            """
            
            data_df = pd.read_sql(text(data_sql), self.db.bind, params={'seq_id': sequence_id})
            data = data_df.where(pd.notna(data_df), None).to_dict('records')
            
            logger.info(f"✅ Retrieved {len(data)} rows from {table_name} (sequence {sequence_id})")
            
            return data, meta
        except Exception as e:
            logger.error(f"❌ Error retrieving sequence data: {e}")
            return [], {}

    def get_latest_sequences(self, limit: int = 10) -> List[Dict]:
        """
        Get the latest calculation sequences
        
        Args:
            limit: Number of sequences to return
        
        Returns:
            List of sequence metadata dicts
        """
        try:
            # Use raw connection to ensure consistent access
            connection = self.db.connection().connection
            cursor = connection.cursor()
            
            try:
                sql = f"""
                SELECT TOP {limit}
                       sequence_id, calculation_date, date_filter, msa_row_count,
                       gen_color_row_count, color_variant_row_count, created_by,
                       created_at, status
                FROM {self.tracking_table}
                ORDER BY sequence_id DESC
                """
                
                cursor.execute(sql)
                columns = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()
                
                # Convert to list of dicts
                sequences = []
                for row in rows:
                    sequences.append(dict(zip(columns, row)))
                
                logger.info(f"✅ Retrieved {len(sequences)} latest sequences")
                return sequences
                
            finally:
                cursor.close()
                # Don't close connection - SQLAlchemy manages it
                
        except Exception as e:
            logger.error(f"Error retrieving latest sequences: {e}")
            return []
