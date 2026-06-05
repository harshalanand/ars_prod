"""
Database Engine & Session Management for SQL Server
Dual Database Setup:
- System DB (Claude): RBAC, RLS, Audit, Table Metadata
- Data DB (Rep_data): Business data, dynamic tables, allocations
"""
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase
from sqlalchemy.pool import QueuePool
from typing import Generator
from typing import Dict
from loguru import logger

from app.core.config import get_settings

settings = get_settings()


# ============================================================================
# System Database Engine (Claude) - RBAC, RLS, Audit
# ============================================================================
system_engine = create_engine(
    settings.DATABASE_URL,
    poolclass=QueuePool,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_pre_ping=settings.DB_POOL_PRE_PING,
    echo=settings.DEBUG,
    fast_executemany=True,
)

# Alias for backward compatibility
engine = system_engine


# ============================================================================
# Data Database Engine (Rep_data) - Business Data
# ============================================================================
data_engine = create_engine(
    settings.DATA_DATABASE_URL,
    poolclass=QueuePool,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_pre_ping=settings.DB_POOL_PRE_PING,
    echo=settings.DEBUG,
    fast_executemany=True,
)


# ============================================================================
# Session Factories
# ============================================================================
SystemSessionLocal = sessionmaker(
    bind=system_engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

DataSessionLocal = sessionmaker(
    bind=data_engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

# Alias for backward compatibility
SessionLocal = SystemSessionLocal


# ============================================================================
# Declarative Bases
# ============================================================================
class Base(DeclarativeBase):
    """Base for system tables (RBAC, RLS, Audit)."""
    pass


class DataBase(DeclarativeBase):
    """Base for data tables (business data)."""
    pass


# ============================================================================
# Dependencies: Get DB Sessions (for FastAPI)
# ============================================================================
def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for System DB (RBAC, RLS, Audit)."""
    db = SystemSessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_data_db() -> Generator[Session, None, None]:
    """FastAPI dependency for Data DB (business data, dynamic tables)."""
    db = DataSessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ============================================================================
# Raw Connection Helpers (for Pandas / bulk ops)
# ============================================================================
def get_raw_connection():
    """Get a raw DBAPI connection for System DB."""
    return system_engine.raw_connection()


def get_data_raw_connection():
    """Get a raw DBAPI connection for Data DB."""
    return data_engine.raw_connection()


def get_engine():
    """Get the System DB SQLAlchemy engine."""
    return system_engine


def get_system_engine():
    """Get the System DB SQLAlchemy engine (alias for get_engine)."""
    return system_engine


def get_data_engine():
    """Get the Data DB SQLAlchemy engine."""
    return data_engine


def get_system_db_url() -> str:
    """Get the System DB connection URL string (for background tasks)."""
    return str(settings.DATABASE_URL)


def get_data_db_url() -> str:
    """Get the Data DB connection URL string (for background tasks)."""
    return str(settings.DATA_DATABASE_URL)


# ============================================================================
# Enable Read Committed Snapshot Isolation (RCSI)
# ============================================================================
def enable_rcsi():
    """
    Enable READ_COMMITTED_SNAPSHOT on both databases.
    This is the #1 fix for 'DB locked during upsert' — readers use row versioning
    instead of shared locks, so they NEVER block writers and vice versa.
    This is a one-time DB setting that persists across restarts.
    """
    for label, eng in [("System", system_engine), ("Data", data_engine)]:
        try:
            db_name = None
            with eng.connect() as conn:
                db_name = conn.execute(text("SELECT DB_NAME()")).scalar()
                is_rcsi = conn.execute(text(
                    "SELECT is_read_committed_snapshot_on FROM sys.databases WHERE name = DB_NAME()"
                )).scalar()
                if is_rcsi:
                    logger.info(f"{label} DB [{db_name}]: RCSI already enabled")
                    continue

            # Must run ALTER DATABASE outside of a transaction on a separate connection
            # using autocommit mode
            raw = eng.raw_connection()
            raw.autocommit = True
            try:
                cursor = raw.cursor()
                cursor.execute(f"ALTER DATABASE [{db_name}] SET READ_COMMITTED_SNAPSHOT ON")
                cursor.close()
                logger.info(f"{label} DB [{db_name}]: RCSI enabled successfully")
            except Exception as e:
                logger.debug(f"{label} DB [{db_name}]: RCSI not set (OK on Azure SQL — usually pre-enabled): {e}")
            finally:
                raw.close()

        except Exception as e:
            logger.debug(f"{label} DB: RCSI check skipped: {e}")


# ============================================================================
# Health Checks
# ============================================================================
def check_db_connection() -> bool:
    """Verify System DB connectivity."""
    try:
        with system_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"System DB connection failed: {e}")
        return False


def check_data_db_connection() -> bool:
    """Verify Data DB connectivity."""
    try:
        with data_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"Data DB connection failed: {e}")
        return False


# ============================================================================
# Event Listeners
# ============================================================================
@event.listens_for(system_engine, "connect")
def set_system_connection_options(dbapi_connection, connection_record):
    """Set connection-level options for system DB."""
    pass


@event.listens_for(data_engine, "connect")
def set_data_connection_options(dbapi_connection, connection_record):
    """Set connection-level options for data DB.
    Use READ COMMITTED SNAPSHOT so readers never block writers and vice versa."""
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        cursor.close()
    except Exception:
        pass


# ============================================================================
# Schema reconciliation — close the gap that Base.metadata.create_all leaves
# ============================================================================
def _sqlalchemy_to_mssql_type(col) -> str:
    """Translate a SQLAlchemy Column into the MSSQL DDL type string we'd
    use in `ALTER TABLE ... ADD`. Mirrors the subset used by the ARS models
    (BigInteger, Integer, String, Text, DateTime, Boolean, etc.)."""
    from sqlalchemy import (
        BigInteger, Integer, SmallInteger, String, Text, DateTime, Boolean,
        Numeric, Float, LargeBinary,
    )
    t = col.type
    if isinstance(t, BigInteger):    return "BIGINT"
    if isinstance(t, SmallInteger):  return "SMALLINT"
    if isinstance(t, Integer):       return "INT"
    if isinstance(t, Boolean):       return "BIT"
    if isinstance(t, DateTime):      return "DATETIME"
    if isinstance(t, Float):         return "FLOAT"
    if isinstance(t, Numeric):
        prec = getattr(t, "precision", None) or 18
        scale = getattr(t, "scale", None) or 2
        return f"NUMERIC({prec},{scale})"
    if isinstance(t, Text):          return "NVARCHAR(MAX)"
    if isinstance(t, String):
        length = getattr(t, "length", None)
        return f"NVARCHAR({length})" if length else "NVARCHAR(MAX)"
    if isinstance(t, LargeBinary):   return "VARBINARY(MAX)"
    # Fall back to compiling against the MSSQL dialect — works for unusual types
    try:
        from sqlalchemy.dialects import mssql
        return t.compile(dialect=mssql.dialect())
    except Exception:
        return "NVARCHAR(MAX)"


def reconcile_columns(target_engine=None) -> Dict[str, list]:
    """For every table in `Base.metadata`, reconcile drift between the model
    and the live database:

    1. **Add missing columns** — `ALTER TABLE [t] ADD [c] <type> NULL` for any
       column the model declares but the live DB lacks. (Closes the gap that
       `Base.metadata.create_all(checkfirst=True)` leaves: it only creates
       missing TABLES, never adds new columns to existing ones.)
    2. **Widen nullability** — `ALTER TABLE [t] ALTER COLUMN [c] <type> NULL`
       when the model says `nullable=True` but the live DB has `IS_NULLABLE='NO'`.
       Only widens; never narrows `NULL → NOT NULL` automatically (that needs
       data validation and could fail on existing NULL rows). Skips PK columns
       since their nullability is structural.

    Idempotent — safe on every startup and after a hot-reload.

    Returns {table_name: [<change description>, ...], ...} for what was changed
    this run; empty dict means everything already matched.
    """
    eng = target_engine or system_engine
    changes: Dict[str, list] = {}
    try:
        with eng.connect() as conn:
            for table_name, table in Base.metadata.tables.items():
                # Pull every column with its nullability flag in one shot.
                # Empty result = table doesn't exist (create_all owns that path).
                rows = conn.execute(
                    text(
                        "SELECT COLUMN_NAME, IS_NULLABLE "
                        "FROM INFORMATION_SCHEMA.COLUMNS "
                        "WHERE TABLE_NAME = :t"
                    ),
                    {"t": table_name},
                ).fetchall()
                if not rows:
                    continue
                # name(lower) -> is_nullable_bool
                actual: Dict[str, bool] = {
                    r[0].lower(): (str(r[1]).upper() == "YES") for r in rows
                }

                for col in table.columns:
                    key = col.name.lower()

                    # ---- 1) Missing column → ADD ----
                    if key not in actual:
                        col_type = _sqlalchemy_to_mssql_type(col)
                        # Always allow NULL on retro-add: existing rows have
                        # no value for the new column, and a hard NOT NULL
                        # would fail on Azure SQL. Model-level NOT NULL is
                        # still enforced for new INSERTs by the ORM.
                        ddl = (
                            f"ALTER TABLE [{table_name}] "
                            f"ADD [{col.name}] {col_type} NULL"
                        )
                        try:
                            conn.execute(text(ddl))
                            try:
                                conn.commit()
                            except Exception:
                                pass
                            changes.setdefault(table_name, []).append(
                                f"added {col.name} ({col_type})"
                            )
                            logger.info(
                                f"reconcile_columns: added "
                                f"{table_name}.{col.name} ({col_type})"
                            )
                        except Exception as e:
                            logger.warning(
                                f"reconcile_columns: failed to add "
                                f"{table_name}.{col.name}: {e}"
                            )
                        continue  # nothing more to check on a freshly-added column

                    # ---- 2) Existing column → check nullability drift ----
                    # Only widen NOT NULL → NULL when the model says nullable.
                    # Skip PK columns (nullability is structural for them).
                    if col.primary_key:
                        continue
                    db_nullable = actual[key]
                    if col.nullable and not db_nullable:
                        col_type = _sqlalchemy_to_mssql_type(col)
                        ddl = (
                            f"ALTER TABLE [{table_name}] "
                            f"ALTER COLUMN [{col.name}] {col_type} NULL"
                        )
                        try:
                            conn.execute(text(ddl))
                            try:
                                conn.commit()
                            except Exception:
                                pass
                            changes.setdefault(table_name, []).append(
                                f"relaxed {col.name} to NULL"
                            )
                            logger.info(
                                f"reconcile_columns: relaxed "
                                f"{table_name}.{col.name} to NULL"
                            )
                        except Exception as e:
                            logger.warning(
                                f"reconcile_columns: failed to relax "
                                f"{table_name}.{col.name} to NULL: {e}"
                            )
    except Exception as e:
        logger.warning(f"reconcile_columns failed for engine: {e}")
    return changes


# ============================================================================
# Hot-Reload (used by Settings UI when DB credentials change)
# ============================================================================
def reload_db_engines() -> dict:
    """Re-point the running app at a new database without restarting.

    Why this works despite many modules doing `from app.database.session import
    data_engine` at import time:

    SQLAlchemy's Engine object holds a connection pool, a dialect, and a URL.
    `engine.connect()` calls `engine.pool.connect()`, which in turn calls a
    *creator* closure built from the URL. By swapping `engine.pool` with a
    pool built from the NEW URL, every existing reference to `data_engine`
    starts producing connections to the new server — without changing the
    Engine object's identity. Direct imports keep working.

    Steps:
      1. Bust the lru_cache on get_settings() so it re-reads .env.
      2. Build new pools from the new URLs (via temporary engines).
      3. Swap the live engines' .pool and .url attributes in place.
      4. Dispose the old pools (closes their checked-in connections).
      5. Re-bind the sessionmaker factories so future sessions use the
         updated engines (no-op if they're already bound to the same object).

    Returns a small status dict for logging.
    """
    from app.core.config import get_settings as _gs

    # 1) Refresh the cached Settings instance so .env changes take effect
    _gs.cache_clear()
    new_settings = _gs()

    sys_url = new_settings.DATABASE_URL
    data_url = new_settings.DATA_DATABASE_URL

    # 2) Build new engines temporarily — only their pools/urls will be used
    new_sys_engine = create_engine(
        sys_url,
        poolclass=QueuePool,
        pool_size=new_settings.DB_POOL_SIZE,
        max_overflow=new_settings.DB_MAX_OVERFLOW,
        pool_timeout=new_settings.DB_POOL_TIMEOUT,
        pool_recycle=new_settings.DB_POOL_RECYCLE,
        pool_pre_ping=new_settings.DB_POOL_PRE_PING,
        echo=new_settings.DEBUG,
        fast_executemany=True,
    )
    new_data_engine = create_engine(
        data_url,
        poolclass=QueuePool,
        pool_size=new_settings.DB_POOL_SIZE,
        max_overflow=new_settings.DB_MAX_OVERFLOW,
        pool_timeout=new_settings.DB_POOL_TIMEOUT,
        pool_recycle=new_settings.DB_POOL_RECYCLE,
        pool_pre_ping=new_settings.DB_POOL_PRE_PING,
        echo=new_settings.DEBUG,
        fast_executemany=True,
    )

    # 3) Swap pools/url onto the live engine objects (preserve identity)
    old_sys_pool = system_engine.pool
    old_data_pool = data_engine.pool
    system_engine.pool = new_sys_engine.pool
    data_engine.pool = new_data_engine.pool
    try:
        system_engine.url = new_sys_engine.url
        data_engine.url = new_data_engine.url
    except AttributeError:
        # Older SQLAlchemy treats .url as read-only — that's fine, .pool is
        # what actually drives connection creation.
        pass

    # 4) Dispose the old pools to release their connections
    try:
        old_sys_pool.dispose()
    except Exception as e:
        logger.warning(f"Old system pool dispose failed: {e}")
    try:
        old_data_pool.dispose()
    except Exception as e:
        logger.warning(f"Old data pool dispose failed: {e}")

    # 5) Re-bind sessionmaker factories — same engine object, but force a
    #    re-validation so any cached state inside the factory is refreshed.
    SystemSessionLocal.configure(bind=system_engine)
    DataSessionLocal.configure(bind=data_engine)

    # 6) Probe both engines with a trivial query against the new pool. If the
    #    swap left the engine in a bad state (or the new credentials cannot
    #    open a session even though the up-front probe passed), fail loudly
    #    here instead of letting downstream API endpoints return mystery 500s.
    sys_db_name = None
    data_db_name = None
    try:
        with system_engine.connect() as conn:
            sys_db_name = conn.execute(text("SELECT DB_NAME()")).scalar()
    except Exception as e:
        logger.error(f"Post-reload probe FAILED for system engine: {e}")
        raise
    try:
        with data_engine.connect() as conn:
            data_db_name = conn.execute(text("SELECT DB_NAME()")).scalar()
    except Exception as e:
        logger.error(f"Post-reload probe FAILED for data engine: {e}")
        raise

    # 7) Re-run the pend_alc / bdc_history / operations prewarm against the
    #    new data DB. Without this, the module-level _TABLES_PREWARMED latch
    #    stays True from the original startup, ensure_*_table() short-circuits
    #    on every request, and any table missing from the new DB surfaces as
    #    "Invalid object name 'ARS_BDC_HISTORY'" (42S02) on the hot path.
    try:
        import app.services.pend_alc_service as _ps
        _ps._TABLES_PREWARMED = False
        _ps.prewarm_pend_alc_tables(data_engine)
        logger.info("Post-reload prewarm completed against new data engine")
    except Exception as e:
        logger.warning(f"Post-reload prewarm failed: {e}")

    db_cfg = new_settings._db()
    logger.info(
        f"DB engines reloaded → server={db_cfg['server']} "
        f"system_db={sys_db_name} data_db={data_db_name} "
        f"user={db_cfg['username']}"
    )
    return {
        "server": db_cfg["server"],
        "system_database": sys_db_name,
        "data_database": data_db_name,
        "username": db_cfg["username"],
    }


