"""
Dynamic Table Management Service
==================================
Create, alter, and manage tables from the UI.
All operations are registered in sys_table_registry and audited.
"""
import json
from typing import List, Dict, Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from loguru import logger

from app.models.table_mgmt import TableRegistry, ColumnRegistry
from app.audit.service import AuditService
from app.database.session import get_engine, get_data_engine


# Allowed SQL data types (whitelist to prevent injection)
ALLOWED_DATA_TYPES = {
    "NVARCHAR", "VARCHAR", "INT", "BIGINT", "SMALLINT", "TINYINT",
    "DECIMAL", "NUMERIC", "FLOAT", "REAL",
    "BIT", "DATE", "DATETIME2", "DATETIME", "TIME",
    "UNIQUEIDENTIFIER", "NTEXT", "TEXT",
}

# Tables that cannot be altered or deleted
PROTECTED_TABLES = {
    "rbac_roles", "rbac_permissions", "rbac_role_permissions",
    "rbac_users", "rbac_user_roles",
    "rls_stores", "rls_user_store_access", "rls_user_region_access", "rls_column_restrictions",
    "audit_log", "sys_table_registry", "sys_column_registry",
}


class TableManagementService:
    """Manages dynamic table creation, alteration, and metadata."""

    def __init__(self, db: Session):
        self.db = db  # System DB for metadata/registry
        self.engine = get_engine()  # System DB engine (for reference)
        self.data_engine = get_data_engine()  # Data DB engine (for DDL)
        self.audit = AuditService(db)

    # ========================================================================
    # CREATE TABLE
    # ========================================================================

    def create_table(
        self,
        table_name: str,
        columns: List[Dict[str, Any]],
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        module: Optional[str] = None,
        created_by: str = "system",
    ) -> Dict[str, Any]:
        """
        Create a new SQL Server table and register it in the metadata registry.
        """
        # Validate table name
        table_name = table_name.strip()
        if table_name.lower() in PROTECTED_TABLES:
            raise ValueError(f"Cannot create table with protected name: {table_name}")

        # Check if table already exists in registry AND in actual DB
        existing = self.db.query(TableRegistry).filter(
            TableRegistry.table_name == table_name
        ).first()
        if existing and existing.is_active:
            # Verify it actually exists in data DB
            with self.data_engine.connect() as check_conn:
                real_exists = check_conn.execute(text(
                    "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :t"
                ), {"t": table_name}).scalar() > 0
            if real_exists:
                raise ValueError(f"Table '{table_name}' already exists")
            else:
                # Registry says exists but DB doesn't — allow recreation
                logger.info(f"Table '{table_name}' in registry but not in DB, recreating")

        # Validate columns
        if not columns:
            raise ValueError("At least one column is required")

        pk_columns = [c for c in columns if c.get("is_primary_key")]

        # Build CREATE TABLE SQL
        col_defs = []
        for col in columns:
            col_sql = self._build_column_sql(col)
            col_defs.append(col_sql)

        # Add primary key constraint
        if pk_columns:
            pk_names = ", ".join([f"[{c['column_name']}]" for c in pk_columns])
            col_defs.append(f"CONSTRAINT PK_{table_name} PRIMARY KEY ({pk_names})")

        create_sql = f"CREATE TABLE [{table_name}] (\n  {','.join(col_defs)}\n)"

        try:
            with self.data_engine.connect() as conn:
                # Drop if exists (from previous failed attempt)
                conn.execute(text(f"IF OBJECT_ID('[{table_name}]','U') IS NOT NULL DROP TABLE [{table_name}]"))
                conn.commit()
                conn.execute(text(create_sql))
                conn.commit()
        except Exception as e:
            raise ValueError(f"Failed to create table: {e}")

        # Register in metadata
        if existing:
            # Reactivate soft-deleted table
            existing.is_active = True
            existing.display_name = display_name or table_name
            existing.description = description
            existing.module = module
            existing.primary_key_columns = json.dumps([c["column_name"] for c in pk_columns])
            existing.created_by = created_by
            registry = existing
        else:
            registry = TableRegistry(
                table_name=table_name,
                display_name=display_name or table_name,
                description=description,
                module=module,
                primary_key_columns=json.dumps([c["column_name"] for c in pk_columns]),
                created_by=created_by,
            )
            self.db.add(registry)

        self.db.flush()

        # Register columns
        for idx, col in enumerate(columns):
            col_reg = ColumnRegistry(
                table_id=registry.id,
                column_name=col["column_name"],
                display_name=col.get("display_name", col["column_name"]),
                data_type=col["data_type"],
                max_length=col.get("max_length"),
                is_nullable=col.get("is_nullable", True),
                is_primary_key=col.get("is_primary_key", False),
                default_value=col.get("default_value"),
                column_order=col.get("column_order", idx),
            )
            self.db.add(col_reg)

        # Audit
        self.audit.log_schema_change(
            table_name=table_name,
            changed_by=created_by,
            action="CREATE_TABLE",
            details={
                "columns": [c["column_name"] for c in columns],
                "primary_keys": [c["column_name"] for c in pk_columns],
            },
        )
        self.db.commit()

        logger.info(f"Table '{table_name}' created with {len(columns)} columns by {created_by}")

        return {
            "table_name": table_name,
            "columns_created": len(columns),
            "primary_keys": [c["column_name"] for c in pk_columns],
        }

    # ========================================================================
    # ALTER TABLE
    # ========================================================================

    def alter_table(
        self,
        table_name: str,
        add_columns: Optional[List[Dict[str, Any]]] = None,
        drop_columns: Optional[List[str]] = None,
        rename_columns: Optional[Dict[str, str]] = None,
        changed_by: str = "system",
    ) -> Dict[str, Any]:
        """
        Alter an existing table: add/drop/rename columns.
        """
        if table_name.lower() in PROTECTED_TABLES:
            raise ValueError(f"Cannot alter protected table: {table_name}")

        registry = self.db.query(TableRegistry).filter(
            TableRegistry.table_name == table_name, TableRegistry.is_active == True
        ).first()

        # Verify table exists in data DB even if not in registry
        if not registry:
            try:
                with self.data_engine.connect() as conn:
                    exists = conn.execute(text(
                        "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :tn"
                    ), {"tn": table_name}).fetchone()
                if not exists:
                    raise ValueError(f"Table '{table_name}' not found")
            except ValueError:
                raise
            except Exception:
                raise ValueError(f"Table '{table_name}' not found")

        changes = []

        # Add columns
        if add_columns:
            for col in add_columns:
                col_sql = self._build_column_sql(col)
                alter_sql = f"ALTER TABLE [{table_name}] ADD {col_sql}"
                try:
                    with self.data_engine.connect() as conn:
                        conn.execute(text(alter_sql))
                        conn.commit()
                except Exception as e:
                    raise ValueError(f"Failed to add column '{col['column_name']}': {e}")

                # Register column if registry exists
                if registry:
                    col_reg = ColumnRegistry(
                        table_id=registry.id,
                        column_name=col["column_name"],
                        display_name=col.get("display_name", col["column_name"]),
                        data_type=col["data_type"],
                        max_length=col.get("max_length"),
                        is_nullable=col.get("is_nullable", True),
                        is_primary_key=False,
                        default_value=col.get("default_value"),
                    )
                    self.db.add(col_reg)
                changes.append(f"ADD {col['column_name']}")

        # Drop columns
        if drop_columns:
            for col_name in drop_columns:
                # Check it's not a PK
                if registry:
                    pk_cols = json.loads(registry.primary_key_columns or "[]")
                    if col_name in pk_cols:
                        raise ValueError(f"Cannot drop primary key column: {col_name}")

                alter_sql = f"ALTER TABLE [{table_name}] DROP COLUMN [{col_name}]"
                try:
                    with self.data_engine.connect() as conn:
                        conn.execute(text(alter_sql))
                        conn.commit()
                except Exception as e:
                    raise ValueError(f"Failed to drop column '{col_name}': {e}")

                # Soft-delete from registry if exists
                if registry:
                    col_reg = self.db.query(ColumnRegistry).filter(
                        ColumnRegistry.table_id == registry.id,
                        ColumnRegistry.column_name == col_name,
                    ).first()
                    if col_reg:
                        col_reg.is_active = False
                changes.append(f"DROP {col_name}")

        # Rename columns
        if rename_columns:
            for old_name, new_name in rename_columns.items():
                rename_sql = f"EXEC sp_rename '[{table_name}].[{old_name}]', '{new_name}', 'COLUMN'"
                try:
                    with self.data_engine.connect() as conn:
                        conn.execute(text(rename_sql))
                        conn.commit()
                except Exception as e:
                    raise ValueError(f"Failed to rename '{old_name}' → '{new_name}': {e}")

                # Update registry if exists
                if registry:
                    col_reg = self.db.query(ColumnRegistry).filter(
                        ColumnRegistry.table_id == registry.id,
                        ColumnRegistry.column_name == old_name,
                    ).first()
                    if col_reg:
                        col_reg.column_name = new_name
                changes.append(f"RENAME {old_name} → {new_name}")

        # Audit
        self.audit.log_schema_change(
            table_name=table_name,
            changed_by=changed_by,
            action="ALTER_TABLE",
            details={"changes": changes},
        )
        self.db.commit()

        return {"table_name": table_name, "changes": changes}

    # ========================================================================
    # ALTER COLUMN TYPE
    # ========================================================================

    def alter_column_type(
        self,
        table_name: str,
        column_name: str,
        new_type: str,
        changed_by: str = "system",
    ) -> Dict[str, Any]:
        """
        Alter the data type of an existing column.
        Handles primary key constraints by temporarily dropping and recreating them.
        """
        if table_name.lower() in PROTECTED_TABLES:
            raise ValueError(f"Cannot alter protected table: {table_name}")

        # Verify table exists (registry or data DB)
        registry = self.db.query(TableRegistry).filter(
            TableRegistry.table_name == table_name, TableRegistry.is_active == True
        ).first()
        if not registry:
            try:
                with self.data_engine.connect() as conn:
                    exists = conn.execute(text(
                        "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :tn"
                    ), {"tn": table_name}).fetchone()
                if not exists:
                    raise ValueError(f"Table '{table_name}' not found")
            except ValueError:
                raise
            except Exception:
                raise ValueError(f"Table '{table_name}' not found")

        try:
            with self.data_engine.connect() as conn:
                # Check if column is part of a primary key constraint
                pk_check_sql = text("""
                    SELECT kc.name AS constraint_name, 
                           STRING_AGG(c.name, ',') WITHIN GROUP (ORDER BY ic.key_ordinal) AS pk_columns
                    FROM sys.key_constraints kc
                    INNER JOIN sys.index_columns ic ON kc.parent_object_id = ic.object_id AND kc.unique_index_id = ic.index_id
                    INNER JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
                    WHERE kc.type = 'PK' 
                      AND OBJECT_NAME(kc.parent_object_id) = :table_name
                    GROUP BY kc.name
                    HAVING SUM(CASE WHEN c.name = :column_name THEN 1 ELSE 0 END) > 0
                """)
                pk_result = conn.execute(pk_check_sql, {"table_name": table_name, "column_name": column_name}).fetchone()
                
                pk_constraint_name = None
                pk_columns = []
                
                if pk_result:
                    pk_constraint_name = pk_result[0]
                    pk_columns = pk_result[1].split(',')
                    
                    # Drop the primary key constraint
                    drop_pk_sql = f"ALTER TABLE [{table_name}] DROP CONSTRAINT [{pk_constraint_name}]"
                    conn.execute(text(drop_pk_sql))
                    conn.commit()
                
                # Execute ALTER COLUMN SQL
                alter_sql = f"ALTER TABLE [{table_name}] ALTER COLUMN [{column_name}] {new_type}"
                conn.execute(text(alter_sql))
                conn.commit()
                
                # Recreate primary key if it was dropped
                if pk_constraint_name and pk_columns:
                    pk_cols_str = ", ".join([f"[{c}]" for c in pk_columns])
                    create_pk_sql = f"ALTER TABLE [{table_name}] ADD CONSTRAINT [{pk_constraint_name}] PRIMARY KEY ({pk_cols_str})"
                    conn.execute(text(create_pk_sql))
                    conn.commit()
                    
        except Exception as e:
            raise ValueError(f"Failed to alter column '{column_name}': {e}")

        # Update column registry (extract base type from new_type like 'NVARCHAR(255)')
        base_type = new_type.split('(')[0].upper()
        max_length = None
        if '(' in new_type and ')' in new_type:
            try:
                length_part = new_type.split('(')[1].split(')')[0]
                if ',' not in length_part:  # Simple length, not precision/scale
                    max_length = int(length_part)
            except:
                pass

        if registry:
            col_reg = self.db.query(ColumnRegistry).filter(
                ColumnRegistry.table_id == registry.id,
                ColumnRegistry.column_name == column_name,
            ).first()
            if col_reg:
                col_reg.data_type = base_type
                if max_length:
                    col_reg.max_length = max_length

        # Audit
        self.audit.log_schema_change(
            table_name=table_name,
            changed_by=changed_by,
            action="ALTER_COLUMN",
            details={"column": column_name, "new_type": new_type},
        )
        self.db.commit()

        return {"table_name": table_name, "column": column_name, "new_type": new_type}

    # ========================================================================
    # SOFT DELETE TABLE
    # ========================================================================

    def soft_delete_table(self, table_name: str, deleted_by: str) -> Dict[str, Any]:
        """Soft-delete a table (mark inactive in registry). If not registered, actually DROP from SQL Server."""
        if table_name.lower() in PROTECTED_TABLES:
            raise ValueError(f"Cannot delete protected table: {table_name}")

        # Try exact match first
        registry = self.db.query(TableRegistry).filter(
            TableRegistry.table_name == table_name, TableRegistry.is_active == True
        ).first()
        
        # If not found, try case-insensitive match
        if not registry:
            registry = self.db.query(TableRegistry).filter(
                TableRegistry.table_name.ilike(table_name), TableRegistry.is_active == True
            ).first()
        
        if registry:
            # Table is registered - soft delete (mark inactive)
            if registry.is_system_table:
                raise ValueError(f"Cannot delete system table: {table_name}")

            registry.is_active = False

            self.audit.log_schema_change(
                table_name=table_name,
                changed_by=deleted_by,
                action="SOFT_DELETE_TABLE",
                details={"table_name": table_name},
            )
            self.db.commit()

            return {"table_name": table_name, "status": "soft_deleted"}
        else:
            # Table not in registry - check if it exists in SQL Server and DROP it
            with self.data_engine.connect() as conn:
                check_sql = text("""
                    SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES 
                    WHERE TABLE_NAME = :table_name AND TABLE_TYPE = 'BASE TABLE'
                """)
                result = conn.execute(check_sql, {"table_name": table_name}).fetchone()
                
                if not result:
                    raise ValueError(f"Table '{table_name}' not found in registry or database")
                
                # Table exists in SQL Server but not registered - DROP it
                drop_sql = text(f"DROP TABLE [{table_name}]")
                conn.execute(drop_sql)
                conn.commit()
                
                self.audit.log_schema_change(
                    table_name=table_name,
                    changed_by=deleted_by,
                    action="DROP_TABLE",
                    details={"table_name": table_name, "note": "Unregistered table dropped"},
                )
                self.db.commit()
                
                logger.info(f"Table '{table_name}' (unregistered) dropped from SQL Server by {deleted_by}")
                
                return {"table_name": table_name, "status": "dropped"}

    # ========================================================================
    # TABLE METADATA & SCHEMA VIEWER
    # ========================================================================

    def get_table_metadata(self, table_name: str) -> Dict[str, Any]:
        """Get full metadata for a table from both registry and INFORMATION_SCHEMA."""
        registry = self.db.query(TableRegistry).filter(
            TableRegistry.table_name == table_name, TableRegistry.is_active == True
        ).first()

        # Get live schema from SQL Server
        sql = text("""
            SELECT
                c.COLUMN_NAME,
                c.DATA_TYPE,
                c.CHARACTER_MAXIMUM_LENGTH,
                c.NUMERIC_PRECISION,
                c.NUMERIC_SCALE,
                c.IS_NULLABLE,
                c.COLUMN_DEFAULT,
                CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 1 ELSE 0 END as IS_PK
            FROM INFORMATION_SCHEMA.COLUMNS c
            LEFT JOIN (
                SELECT ku.COLUMN_NAME
                FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku
                    ON tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME
                WHERE tc.TABLE_NAME = :table_name
                AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
            ) pk ON c.COLUMN_NAME = pk.COLUMN_NAME
            WHERE c.TABLE_NAME = :table_name
            ORDER BY c.ORDINAL_POSITION
        """)

        with self.data_engine.connect() as conn:
            result = conn.execute(sql, {"table_name": table_name})
            columns = []
            for row in result:
                columns.append({
                    "column_name": row[0],
                    "data_type": row[1],
                    "max_length": row[2],
                    "numeric_precision": row[3],
                    "numeric_scale": row[4],
                    "is_nullable": row[5] == "YES",
                    "default_value": row[6],
                    "is_primary_key": bool(row[7]),
                    "display_name": None,
                })

        if not columns:
            raise ValueError(f"Table '{table_name}' not found in database")

        # Enrich with registry display names
        if registry:
            reg_cols = {c.column_name: c for c in registry.columns if c.is_active}
            for col in columns:
                rc = reg_cols.get(col["column_name"])
                if rc:
                    col["display_name"] = rc.display_name

        # Get row count
        count_sql = text(f"SELECT COUNT(*) FROM [{table_name}] WITH (NOLOCK)")
        with self.data_engine.connect() as conn:
            row_count = conn.execute(count_sql).scalar()

        return {
            "table_name": table_name,
            "display_name": registry.display_name if registry else table_name,
            "description": registry.description if registry else None,
            "module": registry.module if registry else None,
            "is_system_table": registry.is_system_table if registry else False,
            "is_active": registry.is_active if registry else True,
            "row_count": row_count,
            "columns": columns,
            "created_at": registry.created_at.isoformat() if registry else None,
            "created_by": registry.created_by if registry else None,
        }

    def list_tables(self, module: Optional[str] = None, include_system: bool = False) -> List[Dict]:
        """List all registered tables."""
        query = self.db.query(TableRegistry).filter(TableRegistry.is_active == True)
        if module:
            query = query.filter(TableRegistry.module == module)
        if not include_system:
            query = query.filter(TableRegistry.is_system_table == False)

        tables = query.order_by(TableRegistry.table_name).all()

        return [
            {
                "id": t.id,
                "table_name": t.table_name,
                "display_name": t.display_name,
                "description": t.description,
                "module": t.module,
                "is_system_table": t.is_system_table,
                "row_count": t.row_count,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "created_by": t.created_by,
            }
            for t in tables
        ]

    def list_all_database_tables(self) -> List[Dict]:
        """List all tables from SQL Server INFORMATION_SCHEMA (not just registered)."""
        sql = text("""
            SELECT t.TABLE_NAME, p.[rows] as row_count
            FROM INFORMATION_SCHEMA.TABLES t
            LEFT JOIN sys.partitions p
                ON OBJECT_ID(t.TABLE_NAME) = p.object_id AND p.index_id IN (0, 1)
            WHERE t.TABLE_TYPE = 'BASE TABLE'
            ORDER BY t.TABLE_NAME
        """)
        with self.data_engine.connect() as conn:
            result = conn.execute(sql)
            return [{"table_name": row[0], "row_count": row[1] or 0} for row in result]

    # ========================================================================
    # DATA QUERY (Generic)
    # ========================================================================

    def query_table_data(
        self,
        table_name: str,
        columns: Optional[List[str]] = None,
        filters: Optional[Dict[str, Any]] = None,
        order_by: Optional[str] = None,
        order_dir: str = "ASC",
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """
        Generic paginated data query for any table.
        Used by the editable data grid frontend.
        """
        # Validate table exists
        check_sql = text("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_NAME = :table_name
        """)
        with self.data_engine.connect() as conn:
            exists = conn.execute(check_sql, {"table_name": table_name}).scalar()
        if not exists:
            raise ValueError(f"Table '{table_name}' not found")

        # Build SELECT
        col_list = ", ".join([f"[{c}]" for c in columns]) if columns else "*"

        # Build WHERE from filters
        # Supports AG-Grid filter model format: {column: {type: 'contains', filter: 'value'}}
        # Also supports 'in' type for multi-select: {column: {type: 'in', filter: ['val1', 'val2']}}
        # Or simple format: {column: 'value'}
        where_clause = ""
        params = {}
        param_idx = 0
        if filters:
            conditions = []
            for col, val in filters.items():
                safe_col = col.replace("'", "''").replace("[", "").replace("]", "")
                
                # Handle AG-Grid filter model format
                if isinstance(val, dict):
                    filter_type = val.get('type', 'contains')
                    filter_val = val.get('filter', '')
                    
                    # Handle 'in' filter type for multi-select
                    if filter_type == 'in':
                        if isinstance(filter_val, list) and len(filter_val) > 0:
                            placeholders = []
                            for v in filter_val:
                                param_name = f"f{param_idx}"
                                param_idx += 1
                                placeholders.append(f":{param_name}")
                                params[param_name] = v
                            conditions.append(f"[{safe_col}] IN ({', '.join(placeholders)})")
                        continue
                    
                    if not filter_val and filter_val != 0:
                        continue
                    
                    param_name = f"f{param_idx}"
                    param_idx += 1
                    
                    if filter_type == 'contains':
                        conditions.append(f"[{safe_col}] LIKE :{param_name}")
                        params[param_name] = f"%{filter_val}%"
                    elif filter_type == 'equals':
                        conditions.append(f"[{safe_col}] = :{param_name}")
                        params[param_name] = filter_val
                    elif filter_type == 'startsWith':
                        conditions.append(f"[{safe_col}] LIKE :{param_name}")
                        params[param_name] = f"{filter_val}%"
                    elif filter_type == 'endsWith':
                        conditions.append(f"[{safe_col}] LIKE :{param_name}")
                        params[param_name] = f"%{filter_val}"
                    elif filter_type == 'notEqual':
                        conditions.append(f"[{safe_col}] != :{param_name}")
                        params[param_name] = filter_val
                    elif filter_type == 'blank':
                        conditions.append(f"([{safe_col}] IS NULL OR [{safe_col}] = '')")
                    elif filter_type == 'notBlank':
                        conditions.append(f"([{safe_col}] IS NOT NULL AND [{safe_col}] != '')")
                    else:
                        # Default to contains
                        conditions.append(f"[{safe_col}] LIKE :{param_name}")
                        params[param_name] = f"%{filter_val}%"
                else:
                    # Simple value filter
                    param_name = f"f{param_idx}"
                    param_idx += 1
                    if isinstance(val, str) and "%" in val:
                        conditions.append(f"[{safe_col}] LIKE :{param_name}")
                        params[param_name] = val
                    elif val is None:
                        conditions.append(f"[{safe_col}] IS NULL")
                    else:
                        conditions.append(f"CAST([{safe_col}] AS NVARCHAR) LIKE :{param_name}")
                        params[param_name] = f"%{val}%"
            
            if conditions:
                where_clause = "WHERE " + " AND ".join(conditions)

        # Count total
        count_sql = text(f"SELECT COUNT(*) FROM [{table_name}] WITH (NOLOCK) {where_clause}")
        with self.data_engine.connect() as conn:
            total = conn.execute(count_sql, params).scalar()

        # Order
        order_clause = ""
        if order_by:
            direction = "DESC" if order_dir.upper() == "DESC" else "ASC"
            order_clause = f"ORDER BY [{order_by}] {direction}"
        else:
            order_clause = "ORDER BY (SELECT NULL)"

        # Paginate with OFFSET/FETCH
        offset = (page - 1) * page_size
        data_sql = text(f"""
            SELECT {col_list} FROM [{table_name}] WITH (NOLOCK)
            {where_clause}
            {order_clause}
            OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY
        """)
        params["offset"] = offset
        params["page_size"] = page_size

        with self.data_engine.connect() as conn:
            result = conn.execute(data_sql, params)
            col_names = list(result.keys())
            rows = []
            for row in result:
                record = {}
                for i, col in enumerate(col_names):
                    val = row[i]
                    if isinstance(val, float):
                        val = round(val, 4)
                    elif hasattr(val, 'isoformat'):
                        val = val.isoformat()
                    record[col] = val
                rows.append(record)

        return {
            "table_name": table_name,
            "columns": col_names,
            "data": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
        }

    # ========================================================================
    # TRUNCATE TABLE DATA (not drop)
    # ========================================================================

    def truncate_table_data(
        self,
        table_name: str,
        deleted_by: str,
        progress_cb=None,
        batch_size: int = 50000,
    ) -> Dict[str, Any]:
        """Empty a table without dropping it.

        Strategy (in order):
        1. **TRUNCATE TABLE** — minimally logged, takes milliseconds, holds
           only a brief Sch-M lock. Works UNLESS the table is referenced by a
           foreign key from another table.
        2. **Batched DELETE** — `DELETE TOP (N) FROM [t]` in a loop with a
           commit between each batch. Each batch lets the log truncate, lets
           other queries interleave (no escalated table lock for hours), and
           lets us report progress.

        `progress_cb(processed, total, phase)` is invoked between batches so
        the API layer can publish progress to a polling client.
        """
        if table_name.lower() in PROTECTED_TABLES:
            raise ValueError(f"Cannot truncate protected table: {table_name}")

        # Initial row count (NOLOCK so this can't itself be blocked)
        count_sql = text(f"SELECT COUNT(*) FROM [{table_name}] WITH (NOLOCK)")
        with self.data_engine.connect() as conn:
            row_count = conn.execute(count_sql).scalar() or 0

        if progress_cb:
            progress_cb(0, row_count, "starting")

        if row_count == 0:
            # Nothing to do — still log the no-op for audit consistency
            self.audit.log(
                table_name=table_name,
                action_type="DELETE",
                changed_by=deleted_by,
                notes="Truncate requested but table was already empty.",
                row_count=0,
            )
            self.db.commit()
            if progress_cb:
                progress_cb(0, 0, "done")
            return {"table_name": table_name, "rows_deleted": 0, "method": "noop"}

        # ---- Try TRUNCATE TABLE first (fast path) ----
        method = None
        try:
            with self.data_engine.connect() as conn:
                # autocommit so TRUNCATE doesn't sit inside a long transaction
                conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                conn.execute(text(f"TRUNCATE TABLE [{table_name}]"))
            method = "truncate"
            if progress_cb:
                progress_cb(row_count, row_count, "done")
            logger.info(
                f"TRUNCATE TABLE [{table_name}] succeeded — {row_count} rows freed"
            )
        except SQLAlchemyError as truncate_err:
            # Most common reason: FK constraint references this table. Fall
            # back to batched DELETE.
            err_msg = str(truncate_err)[:200]
            logger.info(
                f"TRUNCATE not allowed on [{table_name}] ({err_msg}) — "
                f"falling back to batched DELETE"
            )
            method = "batched_delete"
            deleted_total = 0
            # Loop until @@ROWCOUNT == 0. Commit each batch so the log can
            # checkpoint and other queries can interleave.
            with self.data_engine.connect() as conn:
                conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                while True:
                    res = conn.execute(
                        text(f"DELETE TOP ({batch_size}) FROM [{table_name}]")
                    )
                    deleted = res.rowcount or 0
                    if deleted <= 0:
                        break
                    deleted_total += deleted
                    if progress_cb:
                        progress_cb(deleted_total, row_count, "deleting")
            if progress_cb:
                progress_cb(deleted_total, row_count, "done")
            row_count = deleted_total

        self.audit.log(
            table_name=table_name,
            action_type="DELETE",
            changed_by=deleted_by,
            notes=f"Table data cleared via {method}. {row_count} rows deleted.",
            row_count=row_count,
        )
        self.db.commit()

        return {
            "table_name":   table_name,
            "rows_deleted": row_count,
            "method":       method,
        }

    # ========================================================================
    # HELPERS
    # ========================================================================

    def _build_column_sql(self, col: Dict[str, Any]) -> str:
        """Build SQL column definition from column dict."""
        name = col["column_name"]
        raw_type = col["data_type"].upper().strip()

        # Extract base type and inline length, e.g. "NVARCHAR(4)" -> base="NVARCHAR", inline_len=4
        base_type = raw_type.split("(")[0].strip()
        inline_len = None
        if "(" in raw_type and ")" in raw_type:
            try:
                inline_len = raw_type.split("(")[1].split(")")[0].strip()
            except Exception:
                pass

        if base_type not in ALLOWED_DATA_TYPES:
            raise ValueError(f"Unsupported data type: {base_type}. Allowed: {ALLOWED_DATA_TYPES}")

        data_type = base_type

        # Build type with length/precision
        if data_type in ("NVARCHAR", "VARCHAR", "NCHAR", "CHAR"):
            length = inline_len or col.get("max_length", 255)
            type_sql = f"{data_type}({length})"
        elif data_type in ("DECIMAL", "NUMERIC"):
            precision = col.get("max_length", 18)
            scale = 2  # default scale
            type_sql = f"{data_type}({precision},{scale})"
        else:
            type_sql = data_type

        # Nullable — PK columns must be NOT NULL
        if col.get("is_primary_key"):
            null_sql = "NOT NULL"
        else:
            null_sql = "NULL" if col.get("is_nullable", True) else "NOT NULL"

        # Default
        default_sql = ""
        if col.get("default_value"):
            default_sql = f"DEFAULT {col['default_value']}"

        return f"[{name}] {type_sql} {null_sql} {default_sql}".strip()
