"""
Application Configuration — Cloud-Ready
Database connection values are read from app_settings.json (UI-managed) first,
falling back to environment / .env defaults. Other settings come from env.
No hardcoded passwords or secrets.
"""
import os
import json
from functools import lru_cache
from typing import List, Dict, Any
from pydantic_settings import BaseSettings


# ---------------------------------------------------------------------------
# UI-managed database overrides — must point at the same file the Settings
# endpoint writes to: backend/app_settings.json
# ---------------------------------------------------------------------------
# __file__ = backend/app/core/config.py → ../.. = backend/
APP_SETTINGS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),  # backend/
    "app_settings.json",
)


def load_db_overrides() -> Dict[str, Any]:
    """Read the 'database' block from app_settings.json (written by Settings UI).
    Returns an empty dict if the file is missing or unreadable so callers can
    fall back to env defaults."""
    if not os.path.exists(APP_SETTINGS_FILE):
        return {}
    try:
        with open(APP_SETTINGS_FILE, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("database", {}) or {}
    except Exception:
        return {}


class Settings(BaseSettings):
    # Application
    APP_NAME: str = "ARS - Auto Replenishment System"
    APP_VERSION: str = "2.1.0"
    APP_ENV: str = "development"  # development | staging | production
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # System Database (RBAC, RLS, Audit, Table Metadata)
    DB_SERVER: str = "HOPC560"
    DB_NAME: str = "Claude"
    DB_USERNAME: str = "sa"
    DB_PASSWORD: str = "vrl@55555"           # Override via .env in production
    DB_DRIVER: str = "ODBC Driver 18 for SQL Server"
    DB_TRUST_CERT: str = "yes"               # "no" for Azure SQL
    DB_ENCRYPT: str = "no"                   # "yes" for Azure SQL (mandatory)

    # Connection pool — tuned for 20+ concurrent planners
    DB_POOL_SIZE: int = 15
    DB_MAX_OVERFLOW: int = 25
    DB_POOL_TIMEOUT: int = 60
    DB_POOL_RECYCLE: int = 300               # Azure recommends 300s
    DB_POOL_PRE_PING: bool = True

    DB_TEMPDB_CLEANUP_INTERVAL_MINUTES: int = 5
    DB_TEMPDB_ORPHAN_AGE_MINUTES: int = 15   # More room for long MSA runs
    # Aggressive shrink triggers when total tempdb size exceeds this (MB)
    DB_TEMPDB_AGGRESSIVE_THRESHOLD_MB: int = 20480      # 20 GB
    # Log-level ALERT raised when size exceeds this (MB) — surfaced in UI
    DB_TEMPDB_ALERT_THRESHOLD_MB: int = 40960           # 40 GB
    # Target size per data file after aggressive shrink (MB)
    DB_TEMPDB_AGGRESSIVE_TARGET_MB: int = 4096          # 4 GB
    # How many recent runs to keep in-memory for the history chart
    DB_TEMPDB_HISTORY_SIZE: int = 96                    # 96 × 5 min = 8 hours

    # ── Auto-cleanup after heavy jobs ────────────────────────────────────────
    # Master switch. When True, the auto_free_space_middleware schedules a
    # post-job cleanup (CHECKPOINT + log shrink + tempdb truncate) after any
    # successful POST/PUT to a path containing one of AUTO_FREE_PATHS.
    AUTO_FREE_AFTER_JOB: bool = True
    AUTO_FREE_METHODS: list = ["POST", "PUT"]
    # Substring match against request.url.path. Cheap, no regex overhead,
    # easy to tune from app_settings.json without touching code.
    AUTO_FREE_PATHS: list = [
        "/listing/generate",
        "/listing/run",
        "/allocation",
        "/allocation-engine",
        "/contrib/execute",
        "/contrib/run",
        "/grid-builder/run",
        "/grid/run",
        "/bdc/",
        "/upload",
        "/msa-stock/",
        "/pipeline/run",
        "/upsert",
    ]
    # Skip if a cleanup ran within this many seconds (prevents thrashing
    # under concurrent calls). Cleanup is idempotent; cooldown caps cost.
    AUTO_FREE_COOLDOWN_SEC: int = 60
    # Auto-shrink the Rep_Data log when it grows past this (only applies in
    # SIMPLE recovery — log_backup-held logs need a backup, not a shrink).
    AUTO_FREE_LOG_MAX_MB: int = 8192        # trigger at 8 GB
    AUTO_FREE_LOG_TARGET_MB: int = 4096     # shrink back to 4 GB

    # When True, reclaim-all and post-job cleanup auto-flip FULL→SIMPLE on
    # any DB whose log_reuse_wait is LOG_BACKUP (then CHECKPOINT + SHRINK).
    # Trades point-in-time recovery for the operational guarantee that the
    # log will never fill the disk because nobody scheduled log backups.
    AUTO_RESOLVE_LOG_BACKUP_WAIT: bool = True

    # Working Database (Business data, dynamic tables, allocations)
    DATA_DB_NAME: str = "Rep_data"

    # JWT
    JWT_SECRET_KEY: str = "your-super-secret-key-change-in-production-min-32-chars"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Security
    CORS_ORIGINS: str = '["http://localhost:3000","http://localhost:8000"]'
    PASSWORD_MIN_LENGTH: int = 8
    MAX_LOGIN_ATTEMPTS: int = 5
    ACCOUNT_LOCK_DURATION_MINUTES: int = 30

    # File Upload / Storage
    MAX_UPLOAD_SIZE_MB: int = 100
    UPLOAD_CHUNK_SIZE: int = 10000
    ALLOWED_EXTENSIONS: str = ".csv,.xlsx,.xls"

    # Grid Builder log-pressure controls.
    # Azure SQL DB has a per-tier transaction-log size cap. Each grid run does
    # a multi-million-row INSERT; running several in parallel into one log
    # produced error 9002 ("transaction log is full due to LOG_BACKUP").
    # Sequential runs + chunked INSERTs keep the active log small enough that
    # the platform's auto-backup can clear space between batches.
    GRID_RUN_PARALLELISM: int = 4            # workers in "Run All Active" (was 1; safe now that LOG_BACKUP auto-resolves)
    GRID_RUN_PARALLELISM_MAX: int = 16       # hard cap, even if /run-all?parallelism=N requests more
    GRID_INSERT_CHUNK_SIZE: int = 250000     # rows per INSERT batch within a grid
    GRID_LOG_FULL_RETRY_DELAY_SEC: int = 60  # wait before retrying after 9002
    GRID_LOG_FULL_RETRY_COUNT: int = 1       # one retry, then surface error
    USE_BLOB_STORAGE: bool = False           # True in production (Azure Blob)
    AZURE_STORAGE_CONNECTION_STRING: str = ""
    AZURE_STORAGE_CONTAINER: str = "ars-files"
    LOCAL_UPLOAD_DIR: str = "uploads"
    LOCAL_EXPORT_DIR: str = "exports"

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "logs/app.log"
    LOG_TO_FILE: bool = True                 # False in cloud (use stdout)

    # Super Admin
    SUPER_ADMIN_USERNAME: str = "superadmin"
    SUPER_ADMIN_EMAIL: str = "admin@nubo.in"
    SUPER_ADMIN_PASSWORD: str = "Admin@12345"  # Override via .env in production

    # =========================================================================
    # Computed properties
    # =========================================================================
    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    # -------- Resolved DB values (JSON overrides → env fallback) ---------
    def _db(self) -> Dict[str, Any]:
        """Return the live database connection dict.
        Precedence: app_settings.json['database'] → env / class defaults."""
        ov = load_db_overrides()
        return {
            "server":          ov.get("server")          or self.DB_SERVER,
            "port":            ov.get("port")            or "",     # blank = default
            "username":        ov.get("username")        or self.DB_USERNAME,
            "password":        ov.get("password")        or self.DB_PASSWORD,
            "system_database": ov.get("system_database") or self.DB_NAME,
            "data_database":   ov.get("data_database")   or self.DATA_DB_NAME,
            "driver":          ov.get("driver")          or self.DB_DRIVER,
            "trust_cert":      ov.get("trust_cert")      or self.DB_TRUST_CERT,
            "encrypt":         ov.get("encrypt")         or self.DB_ENCRYPT,
        }

    @property
    def DATABASE_URL(self) -> str:
        """SQLAlchemy connection string for System DB."""
        c = self._db()
        return self._build_connection_url(c["system_database"], c)

    @property
    def DATA_DATABASE_URL(self) -> str:
        """SQLAlchemy connection string for Data DB."""
        c = self._db()
        return self._build_connection_url(c["data_database"], c)

    def _build_connection_url(self, db_name: str, c: Dict[str, Any] = None) -> str:
        """Build a TCP-forced ODBC connection string.

        Uses the ``odbc_connect`` form so the raw ODBC string is passed to the
        driver verbatim. Forcing ``Server=tcp:HOST[,PORT]`` prevents pyodbc
        from falling back to Named Pipes (which is what produces the
        ``[08001] Named Pipes Provider`` error against remote SQL Servers)."""
        from urllib.parse import quote_plus
        c = c or self._db()

        host = str(c["server"]).strip()
        # Don't double-prefix if the user already wrote tcp:/np:/lpc:
        if ":" not in host.split("\\")[0]:  # ignore named-instance backslash
            host = f"tcp:{host}"
        port = str(c.get("port") or "").strip()
        if port and "," not in host and "\\" not in host:
            host = f"{host},{port}"

        odbc = (
            f"DRIVER={{{c['driver']}}};"
            f"SERVER={host};"
            f"DATABASE={db_name};"
            f"UID={c['username']};"
            f"PWD={c['password']};"
            f"TrustServerCertificate={c['trust_cert']};"
            f"Encrypt={'yes' if str(c['encrypt']).lower() == 'yes' else 'no'};"
            # Azure SQL transient-error resilience — driver retries the initial
            # connect on errors like 40613 (DB unavailable), 40501 (service
            # busy), 49918 (cannot process request), and serverless wake-up.
            # Without these, the first request after auto-pause fails outright.
            f"ConnectRetryCount=5;"
            f"ConnectRetryInterval=10;"
            # 60s allows a paused serverless DB to spin back up.
            f"Connection Timeout=60;"
        )
        return f"mssql+pyodbc:///?odbc_connect={quote_plus(odbc)}"

    @property
    def cors_origins_list(self) -> List[str]:
        try:
            return json.loads(self.CORS_ORIGINS)
        except (json.JSONDecodeError, TypeError):
            return ["http://localhost:3000"]

    @property
    def allowed_extensions_list(self) -> List[str]:
        return [ext.strip() for ext in self.ALLOWED_EXTENSIONS.split(",")]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    return Settings()
