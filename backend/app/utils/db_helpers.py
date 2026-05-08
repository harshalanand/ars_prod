"""
Shared Database Helpers
========================
Centralized SQL execution, schema introspection, and column management.
Eliminates duplicate _run(), _table_exists(), _get_columns(), _col_exists(),
_ensure_col() across grid_builder, listing, grid_calculations, etc.
"""
import random
import time
from typing import Any, Callable, Dict, List, Optional, Set
from sqlalchemy import text
from loguru import logger


# SQL Server deadlock victim error number — used by both pyodbc / SQLAlchemy.
# 1205 = "Transaction was deadlocked on lock resources..."
# We also catch the ODBC SQLSTATE '40001' (serialization failure) which
# wraps deadlocks regardless of underlying number.
_DEADLOCK_TOKENS = ("1205", "40001", "deadlocked", "deadlock victim")


def _is_deadlock(exc: BaseException) -> bool:
    """True when an exception looks like a SQL Server deadlock victim."""
    msg = str(exc)
    return any(tok in msg for tok in _DEADLOCK_TOKENS)


# ==========================================================================
# Deadlock-retry telemetry
# ==========================================================================
# Process-local counters so the worker can report how many deadlocks it
# absorbed silently. ProcessPoolExecutor children each get their own copy;
# the parent picks them up via the per-worker `result()` payload.

class DeadlockStats:
    """Process-local counter (each worker has its own instance).

    Cleared at the start of every Stage C run via `reset()`. Read at the end
    via `snapshot()` so the orchestrator can log a single summary line like:
        deadlock retries: caught=47 succeeded=44 exhausted=3
    """
    _caught:    int = 0   # total times a deadlock was raised inside retry_on_deadlock
    _succeeded: int = 0   # caught and a later attempt succeeded
    _exhausted: int = 0   # all max_attempts exhausted, gave up

    @classmethod
    def reset(cls) -> None:
        cls._caught = 0
        cls._succeeded = 0
        cls._exhausted = 0

    @classmethod
    def record_caught(cls)    -> None: cls._caught    += 1
    @classmethod
    def record_succeeded(cls) -> None: cls._succeeded += 1
    @classmethod
    def record_exhausted(cls) -> None: cls._exhausted += 1

    @classmethod
    def snapshot(cls) -> Dict[str, int]:
        return {
            "caught":    cls._caught,
            "succeeded": cls._succeeded,
            "exhausted": cls._exhausted,
        }


# ==========================================================================
# SQL EXECUTION
# ==========================================================================

def run_sql(conn, sql: str, params: dict = None):
    """Execute SQL and commit. Central point for all DDL/DML fire-and-forget."""
    conn.execute(text(sql) if isinstance(sql, str) else sql, params or {})
    conn.commit()


def retry_on_deadlock(
    fn: Callable[[], Any],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.4,
    label: str = "",
) -> Any:
    """Run `fn()` and transparently retry on SQL Server deadlock victim errors.

    On a deadlock the rolled-back transaction is *fully* released by SQL
    Server, so the simplest correct response is exactly: wait a moment,
    rerun. We use exponential backoff with jitter to avoid hot-spinning
    when multiple workers are pummelling the same pages.

    Re-raises any non-deadlock exception unchanged. Re-raises the last
    deadlock if all `max_attempts` are exhausted.

    Caller contract: `fn` must encapsulate its **own** transaction (open
    its own engine.connect()/raw_connection() inside fn, run the SQL,
    commit, close). Retrying with a stale already-rolled-back connection
    won't work.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = fn()
            # If a previous attempt deadlocked and we got here, this attempt
            # succeeded — record it for the end-of-run summary.
            if attempt > 1:
                DeadlockStats.record_succeeded()
            return result
        except Exception as e:
            if not _is_deadlock(e):
                raise
            DeadlockStats.record_caught()
            last_exc = e
            if attempt >= max_attempts:
                DeadlockStats.record_exhausted()
                logger.error(
                    f"[deadlock-retry] {label or fn.__name__}: "
                    f"giving up after {max_attempts} attempts: {str(e)[:200]}"
                )
                raise
            # Exponential backoff with jitter: 0.4s, 0.8s, 1.6s ± 30%
            delay = base_delay * (2 ** (attempt - 1))
            delay *= 1.0 + random.uniform(-0.3, 0.3)
            logger.warning(
                f"[deadlock-retry] {label or fn.__name__}: "
                f"attempt {attempt}/{max_attempts} hit deadlock, "
                f"sleeping {delay:.2f}s and retrying"
            )
            time.sleep(delay)
    # Defensive — _should_ have raised already
    if last_exc is not None:
        raise last_exc


# ==========================================================================
# SCHEMA INTROSPECTION (with per-connection caching)
# ==========================================================================

class SchemaCache:
    """
    Caches INFORMATION_SCHEMA lookups for the lifetime of a single connection.
    Avoids 40-60 redundant queries per grid build / listing generation.

    Usage:
        cache = SchemaCache(conn)
        if cache.table_exists("ARS_LISTING"):
            cols = cache.get_columns("ARS_LISTING")
    """

    def __init__(self, conn):
        self.conn = conn
        self._tables: Optional[Set[str]] = None
        self._columns: Dict[str, List[str]] = {}
        self._column_sets: Dict[str, Set[str]] = {}

    def _load_tables(self):
        if self._tables is None:
            rows = self.conn.execute(text(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES"
            )).fetchall()
            self._tables = {r[0] for r in rows}

    def table_exists(self, table_name: str) -> bool:
        self._load_tables()
        return table_name in self._tables

    def invalidate_table(self, table_name: str):
        """Call after CREATE/DROP TABLE to refresh cache for that table."""
        self._tables = None  # force reload next check
        self._columns.pop(table_name, None)
        self._column_sets.pop(table_name, None)

    def get_columns(self, table_name: str) -> List[str]:
        """Return ordered column names for a table."""
        if table_name not in self._columns:
            rows = self.conn.execute(text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION"
            ), {"t": table_name}).fetchall()
            self._columns[table_name] = [r[0] for r in rows]
            self._column_sets[table_name] = {r[0].upper() for r in rows}
        return self._columns[table_name]

    def get_column_set(self, table_name: str) -> Set[str]:
        """Return uppercase column name set for fast membership checks."""
        if table_name not in self._column_sets:
            self.get_columns(table_name)
        return self._column_sets[table_name]

    def get_column_map(self, table_name: str) -> Dict[str, str]:
        """Return {UPPER_NAME: actual_name} mapping."""
        cols = self.get_columns(table_name)
        return {c.upper(): c for c in cols}

    def column_exists(self, table_name: str, column_name: str) -> bool:
        return column_name.upper() in self.get_column_set(table_name)


# ==========================================================================
# STANDALONE FUNCTIONS (for when a full cache isn't needed)
# ==========================================================================

def table_exists(conn, table_name: str) -> bool:
    return conn.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :t"
    ), {"t": table_name}).scalar() > 0


def get_columns(conn, table_name: str) -> List[str]:
    rows = conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION"
    ), {"t": table_name}).fetchall()
    return [r[0] for r in rows]


def column_exists(conn, table_name: str, column_name: str) -> bool:
    return conn.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME = :t AND COLUMN_NAME = :c"
    ), {"t": table_name, "c": column_name}).scalar() > 0


def ensure_column(conn, table_name: str, column_name: str, dtype: str = "FLOAT"):
    """Add column if it doesn't exist. Silently succeeds if it already exists."""
    if not column_exists(conn, table_name, column_name):
        try:
            run_sql(conn, f"ALTER TABLE [{table_name}] ADD [{column_name}] {dtype} NULL")
        except Exception as e:
            logger.debug(f"ensure_column {table_name}.{column_name}: {e}")


def get_col_type_sql(conn, table_name: str, col_name: str) -> str:
    """Get SQL type string for a column from INFORMATION_SCHEMA."""
    row = conn.execute(text(
        "SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :tbl AND COLUMN_NAME = :col"
    ), {"tbl": table_name, "col": col_name}).fetchone()
    if not row:
        return "NVARCHAR(255)"
    dt = row[0].upper()
    if dt in ("NVARCHAR", "VARCHAR", "NCHAR", "CHAR"):
        ml = row[1]
        return f"{dt}({ml})" if ml and ml > 0 else f"{dt}(MAX)"
    elif dt in ("DECIMAL", "NUMERIC"):
        return f"{dt}({row[2] or 18},{row[3] or 2})"
    return dt


# ==========================================================================
# SQL EXPRESSION BUILDERS (for type normalization)
# ==========================================================================

def msa_expr(col: str) -> str:
    """Raw expression for MSA VARCHAR(MAX) columns (no alias). For GROUP BY / WHERE."""
    if col.upper() == "GEN_ART_NUMBER":
        return f"TRY_CAST(TRY_CAST([{col}] AS FLOAT) AS BIGINT)"
    return f"LTRIM(RTRIM(CAST([{col}] AS NVARCHAR(200))))"


def msa_col(col: str) -> str:
    """SELECT expression with alias for MSA columns."""
    return f"{msa_expr(col)} AS [{col}]"


def grid_col(table_alias: str, col: str) -> str:
    """Comparison expression to normalize grid column for matching with MSA."""
    if col.upper() == "GEN_ART_NUMBER":
        return f"TRY_CAST({table_alias}.[{col}] AS BIGINT)"
    return f"LTRIM(RTRIM(CAST({table_alias}.[{col}] AS NVARCHAR(200))))"
