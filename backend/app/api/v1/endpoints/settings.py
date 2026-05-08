"""
Application Settings API Endpoints
- System configuration
- Database settings
- Email configuration
- Application preferences
- Backup management
"""
import json
import os
from datetime import datetime
from typing import Optional, Dict, Any, List
from urllib.parse import quote_plus
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.database.session import get_db, get_data_engine, get_system_engine, reload_db_engines
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user, RequirePermissions
from app.models.rbac import User
from app.core.config import get_settings, APP_SETTINGS_FILE

PASSWORD_MASK = "********"

router = APIRouter(prefix="/settings", tags=["Settings"])

settings = get_settings()

# Settings file path — single source of truth defined in app.core.config so
# the runtime engine builder and this endpoint never disagree on location.
SETTINGS_FILE = APP_SETTINGS_FILE
# backend/ root — both app_settings.json and .env live here.
# APP_SETTINGS_FILE = backend/app_settings.json → dirname = backend/
BACKEND_ROOT = os.path.dirname(APP_SETTINGS_FILE)
ENV_FILE = os.path.join(BACKEND_ROOT, ".env")
# Backup directory — sibling of the settings file (backend/backups)
BACKUP_DIR = os.path.join(BACKEND_ROOT, "backups")


def load_app_settings() -> Dict[str, Any]:
    """Load application settings from file."""
    default_settings = {
        "database": {
            "server": settings.DB_SERVER or "",
            "system_database": settings.DB_NAME or "Claude",
            "data_database": settings.DATA_DB_NAME or "Rep_data",
            "username": settings.DB_USERNAME or "sa",
            "password": settings.DB_PASSWORD or "",
            "driver": settings.DB_DRIVER or "ODBC Driver 18 for SQL Server",
            "trust_cert": settings.DB_TRUST_CERT or "yes",
            "encrypt": settings.DB_ENCRYPT or "no",
        },
        "email": {
            "smtp_server": "",
            "smtp_port": 587,
            "smtp_username": "",
            "smtp_password": "",
            "from_address": "",
            "use_tls": True,
            "notifications_enabled": False,
        },
        "application": {
            "app_name": "ARS - Allocation & Reporting System",
            "max_upload_size_mb": settings.MAX_UPLOAD_SIZE_MB,
            "session_timeout_minutes": 60,
            "enable_audit_logging": True,
            "enable_row_level_security": True,
            "default_page_size": 50,
            "max_export_rows": 500000,
        },
        "ui": {
            "primary_color": "#4f46e5",
            "sidebar_collapsed": False,
            "show_row_numbers": True,
            "date_format": "YYYY-MM-DD",
            "number_format": "en-US",
        },
        "allocation": {
            "history_retention_days": 30,
        },
    }
    
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                saved = json.load(f)
                # Merge saved settings with defaults
                for category, values in saved.items():
                    if category in default_settings:
                        default_settings[category].update(values)
                    else:
                        default_settings[category] = values
        except:
            pass
    
    return default_settings


def save_app_settings(settings_dict: Dict[str, Any]) -> bool:
    """Save application settings to file."""
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings_dict, f, indent=2)
        return True
    except Exception as e:
        raise ValueError(f"Failed to save settings: {e}")


# ----------------------------------------------------------------------------
# .env writer — keeps the file as the canonical environment source of truth
# so that even a hard restart (no app_settings.json) still connects to the
# DB the user last saved through the UI.
# ----------------------------------------------------------------------------
def _format_env_value(value: Any) -> str:
    """Quote a value if it contains chars that would break a bare KEY=VAL line."""
    s = "" if value is None else str(value)
    if s == "":
        return ""
    # Quote when there's whitespace, '#', or quote chars; otherwise keep bare.
    needs_quote = any(c in s for c in (" ", "\t", "#", '"', "'", "$"))
    if needs_quote:
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


def update_env_file(updates: Dict[str, Any]) -> str:
    """Update specific keys in backend/.env, preserving every other line
    (comments, blanks, unrelated keys). Creates the file if missing.
    Returns the absolute path of the file written."""
    lines: List[str] = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()

    seen: set = set()
    out: List[str] = []
    for line in lines:
        stripped = line.lstrip()
        # Keep comments/blank lines unchanged
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={_format_env_value(updates[key])}\n")
            seen.add(key)
        else:
            out.append(line)

    # Append keys that weren't already present
    missing = [k for k in updates.keys() if k not in seen]
    if missing:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        if out and out[-1].strip() != "":
            out.append("\n")
        out.append("# --- Updated by Settings UI ---\n")
        for k in missing:
            out.append(f"{k}={_format_env_value(updates[k])}\n")

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(out)
    return ENV_FILE




# ============================================================================
# Get Settings
# ============================================================================

def _mask_passwords(data: Dict[str, Any]) -> Dict[str, Any]:
    """Replace any password fields with the mask before sending to UI."""
    if data.get("email", {}).get("smtp_password"):
        data["email"]["smtp_password"] = PASSWORD_MASK
    if data.get("database", {}).get("password"):
        data["database"]["password"] = PASSWORD_MASK
    return data


@router.get("", response_model=APIResponse)
async def get_all_settings(
    current_user: User = Depends(get_current_user),
    _: User = Depends(RequirePermissions(["ADMIN_SETTINGS"])),
):
    """Get all application settings."""
    return APIResponse(data=_mask_passwords(load_app_settings()))


# ============================================================================
# Update Settings
# ============================================================================

class UpdateSettingsRequest(BaseModel):
    category: str
    settings: Dict[str, Any]


@router.put("", response_model=APIResponse)
async def update_settings(
    body: UpdateSettingsRequest,
    current_user: User = Depends(get_current_user),
    _: User = Depends(RequirePermissions(["ADMIN_SETTINGS"])),
):
    """Update settings for a category. Database changes require backend restart
    because SQLAlchemy engines and sessionmakers are bound at process start."""
    all_settings = load_app_settings()

    if body.category not in all_settings:
        all_settings[body.category] = {}

    # Preserve masked password fields — never overwrite with the mask string.
    incoming = dict(body.settings)
    if body.category == "email" and incoming.get("smtp_password") == PASSWORD_MASK:
        incoming["smtp_password"] = all_settings.get("email", {}).get("smtp_password", "")
    if body.category == "database" and incoming.get("password") == PASSWORD_MASK:
        incoming["password"] = all_settings.get("database", {}).get("password", "")

    all_settings[body.category].update(incoming)
    save_app_settings(all_settings)

    saved = dict(all_settings[body.category])
    if body.category == "database" and saved.get("password"):
        saved["password"] = PASSWORD_MASK

    msg = "Settings updated successfully"
    requires_restart = False
    if body.category == "database":
        msg = "Database settings saved. Restart the backend to connect to the new server."
        requires_restart = True

    return APIResponse(
        data={"settings": saved, "requires_restart": requires_restart},
        message=msg,
    )


# ============================================================================
# Database Connection Test
# ============================================================================

class TestConnectionRequest(BaseModel):
    """Optional payload for /test-connection. If any field is missing,
    the saved value from app_settings.json is used as fallback."""
    server: Optional[str] = None
    port: Optional[str] = None
    system_database: Optional[str] = None
    data_database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    driver: Optional[str] = None
    trust_cert: Optional[str] = None
    encrypt: Optional[str] = None


def _build_test_url(cfg: Dict[str, Any], db_name: str) -> str:
    """Build a TCP-forced odbc_connect URL — mirrors config._build_connection_url."""
    host = str(cfg["server"]).strip()
    if ":" not in host.split("\\")[0]:
        host = f"tcp:{host}"
    port = str(cfg.get("port") or "").strip()
    if port and "," not in host and "\\" not in host:
        host = f"{host},{port}"

    odbc = (
        f"DRIVER={{{cfg['driver']}}};"
        f"SERVER={host};"
        f"DATABASE={db_name};"
        f"UID={cfg['username']};"
        f"PWD={cfg['password']};"
        f"TrustServerCertificate={cfg.get('trust_cert', 'yes')};"
        f"Encrypt={'yes' if str(cfg.get('encrypt', 'no')).lower() == 'yes' else 'no'};"
    )
    return f"mssql+pyodbc:///?odbc_connect={quote_plus(odbc)}"


def _classify_db_error(err: str) -> str:
    """Translate the verbose pyodbc error into a one-line, actionable hint."""
    e = err.lower()
    if "named pipes provider" in e or "tcp provider" in e or "08001" in e:
        return ("Cannot reach SQL Server on the network. "
                "Enable TCP/IP in SQL Server Configuration Manager, "
                "open port 1433 in the firewall, and restart the SQL Server service.")
    if "login failed" in e or "18456" in e:
        return ("Login failed — check the username/password and that "
                "Mixed Mode (SQL + Windows) authentication is enabled on the server.")
    if "cannot open database" in e or "4060" in e:
        return ("Server reached, but the database does not exist on it. "
                "Restore or create the database, then test again.")
    if "timeout" in e:
        return "Connection timeout — server is unreachable or firewall is dropping the packets."
    return "Connection failed."


def _probe(url: str) -> Dict[str, Any]:
    """Open a one-shot engine, run a version probe, then dispose."""
    eng = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
    try:
        with eng.connect() as conn:
            row = conn.execute(text(
                "SELECT @@VERSION AS version, DB_NAME() AS database_name"
            )).fetchone()
            return {
                "status": "connected",
                "database": row[1],
                "server_version": (row[0] or "")[:80],
            }
    finally:
        eng.dispose()


@router.post("/test-connection", response_model=APIResponse)
async def test_database_connection(
    body: Optional[TestConnectionRequest] = None,
    current_user: User = Depends(get_current_user),
    _: User = Depends(RequirePermissions(["ADMIN_SETTINGS"])),
):
    """Test both database connections against the values supplied in the
    request (live form values). Any field omitted falls back to what's saved
    in app_settings.json. The unmasked password from disk is used if the
    UI sends the mask string."""
    saved = load_app_settings().get("database", {})
    payload = body.model_dump(exclude_none=True) if body else {}

    # Mask handling: UI sends '********' to indicate "use saved password"
    if payload.get("password") == PASSWORD_MASK:
        payload.pop("password")

    cfg = {**saved, **payload}
    missing = [k for k in ("server", "system_database", "data_database",
                           "username", "password", "driver", "trust_cert")
               if not cfg.get(k)]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required database fields: {', '.join(missing)}",
        )

    results = {
        "system_db": {"status": "disconnected", "database": None, "error": None, "hint": None},
        "data_db":   {"status": "disconnected", "database": None, "error": None, "hint": None},
    }
    for label, db_name in [("system_db", cfg["system_database"]),
                           ("data_db",   cfg["data_database"])]:
        try:
            results[label] = _probe(_build_test_url(cfg, db_name))
        except Exception as e:
            err = str(e)
            results[label]["error"] = err[:300]
            results[label]["hint"] = _classify_db_error(err)

    sys_ok = results["system_db"]["status"] == "connected"
    data_ok = results["data_db"]["status"] == "connected"

    if sys_ok and data_ok:
        msg = "Both databases connected."
    elif sys_ok and not data_ok:
        msg = (f"System DB '{cfg['system_database']}' connected. "
               f"Data DB '{cfg['data_database']}' is not yet available — "
               f"create or restore it, then test again.")
    elif not sys_ok and data_ok:
        msg = (f"Data DB connected, but System DB '{cfg['system_database']}' "
               f"is missing — the app cannot start without it.")
    else:
        msg = "Both databases failed to connect — check the hint under each error."

    return APIResponse(data=results, message=msg)


# ============================================================================
# Apply Database Settings — test, persist to JSON + .env, restart backend
# ============================================================================

@router.post("/database/apply", response_model=APIResponse)
async def apply_database_settings(
    body: TestConnectionRequest,
    current_user: User = Depends(get_current_user),
    _: User = Depends(RequirePermissions(["ADMIN_SETTINGS"])),
):
    """Atomic save flow used by the Database tab in Settings UI:
    1. Probe both System DB and Data DB with the supplied (or saved) values.
    2. If either probe fails → reject the save (no files touched).
    3. Persist to backend/app_settings.json AND backend/.env (canonical
       sources for any future hard restart).
    4. Hot-reload the running engines — clear the settings cache, swap each
       engine's connection pool to the new server, dispose the old pools.
       No process restart is needed; the next request from any code path
       (FastAPI dep-injected sessions, raw `data_engine.connect()`, etc.)
       opens connections to the new server."""
    saved = load_app_settings().get("database", {})
    payload = body.model_dump(exclude_none=True) if body else {}
    # UI sends '********' to mean "use the saved password" — never persist that.
    if payload.get("password") == PASSWORD_MASK:
        payload.pop("password")

    cfg = {**saved, **payload}
    required = ("server", "system_database", "data_database",
                "username", "password", "driver", "trust_cert")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required database fields: {', '.join(missing)}",
        )

    # ---- Step 1: Probe both databases ----
    results = {}
    for label, db_name in [("system_db", cfg["system_database"]),
                           ("data_db",   cfg["data_database"])]:
        try:
            results[label] = _probe(_build_test_url(cfg, db_name))
        except Exception as e:
            err = str(e)
            raise HTTPException(
                status_code=400,
                detail={
                    "message": f"{label.replace('_', ' ').title()} probe failed — settings NOT saved.",
                    "hint": _classify_db_error(err),
                    "error": err[:300],
                    "failed_target": db_name,
                },
            )

    # ---- Step 2: Persist to app_settings.json ----
    all_settings = load_app_settings()
    all_settings.setdefault("database", {}).update({
        "server":          cfg["server"],
        "port":            cfg.get("port", "") or "",
        "system_database": cfg["system_database"],
        "data_database":   cfg["data_database"],
        "username":        cfg["username"],
        "password":        cfg["password"],
        "driver":          cfg["driver"],
        "trust_cert":      cfg.get("trust_cert", "yes"),
        "encrypt":         cfg.get("encrypt", "no"),
    })
    save_app_settings(all_settings)

    # ---- Step 3: Persist to .env (canonical for hard restarts) ----
    env_updates = {
        "DB_SERVER":     cfg["server"],
        "DB_NAME":       cfg["system_database"],
        "DATA_DB_NAME":  cfg["data_database"],
        "DB_USERNAME":   cfg["username"],
        "DB_PASSWORD":   cfg["password"],
        "DB_DRIVER":     cfg["driver"],
        "DB_TRUST_CERT": cfg.get("trust_cert", "yes"),
        "DB_ENCRYPT":    cfg.get("encrypt", "no"),
    }
    if cfg.get("port"):
        env_updates["DB_PORT"] = str(cfg["port"])
    env_path = update_env_file(env_updates)

    # ---- Step 4: Hot-reload engines (clears settings cache + swaps pools) ----
    try:
        reload_info = reload_db_engines()
    except Exception as e:
        # Files were already written — surface the reload failure but don't
        # roll back, since the next hard restart will pick up the new config.
        logger.exception("Hot-reload of engines failed after saving settings")
        raise HTTPException(
            status_code=500,
            detail={
                "message": ("Settings saved to disk but live engines failed to "
                            "reload. Restart the backend manually to apply."),
                "error": str(e)[:300],
            },
        )

    # ---- Step 5: Ensure system schema exists on the new DB ----
    # If the user pointed us at a fresh / wrong database, the rbac_users etc.
    # tables won't exist and every authenticated endpoint will return 500.
    # Auto-create them (mirrors main.py lifespan), reconcile any columns the
    # model has that the DB lacks, then verify the table is actually
    # queryable. If we can't make this work, surface a warning.
    try:
        from app.database.session import (
            system_engine as _sys_engine, Base, reconcile_columns,
        )
        import app.models.rbac  # noqa - registers RBAC tables on Base
        import app.models.rls   # noqa
        import app.models.audit # noqa
        Base.metadata.create_all(bind=_sys_engine, checkfirst=True)
        added_cols = reconcile_columns(_sys_engine)
        if added_cols:
            logger.info(f"Schema reconcile (apply_database_settings): {added_cols}")

        with _sys_engine.connect() as conn:
            conn.execute(text("SELECT TOP 1 1 FROM rbac_users"))
        schema_status = "ok"
    except Exception as e:
        logger.error(f"System schema check failed on new DB: {e}")
        schema_status = "missing"
        # Engines are already pointing at the new DB. Don't raise — the user
        # may want to populate the DB next. Surface the warning in the response.
        schema_error = str(e)[:300]
    else:
        schema_error = None

    # Return masked summary
    saved_summary = {
        "server":          cfg["server"],
        "port":            cfg.get("port", ""),
        "system_database": cfg["system_database"],
        "data_database":   cfg["data_database"],
        "username":        cfg["username"],
        "password":        PASSWORD_MASK,
        "driver":          cfg["driver"],
        "trust_cert":      cfg.get("trust_cert", "yes"),
        "encrypt":         cfg.get("encrypt", "no"),
    }
    if schema_status == "ok":
        message = (
            "Connections verified, settings saved, live engines reloaded. "
            "The application is now using the new server."
        )
    else:
        message = (
            f"Connected to the new server, but the system schema (rbac_users, "
            f"rbac_roles, etc.) is not present on '{cfg['system_database']}'. "
            f"Auth-protected endpoints will return 500 until the schema is "
            f"created. Restart the backend or run the migrations against the "
            f"new database."
        )

    return APIResponse(
        data={
            "settings":      saved_summary,
            "system_db":     results["system_db"],
            "data_db":       results["data_db"],
            "json_path":     SETTINGS_FILE,
            "env_path":      env_path,
            "reloaded":      reload_info,
            "schema_status": schema_status,
            "schema_error":  schema_error,
        },
        message=message,
    )


# ============================================================================
# Email Test
# ============================================================================

class TestEmailRequest(BaseModel):
    to_address: str


@router.post("/test-email", response_model=APIResponse)
async def test_email(
    body: TestEmailRequest,
    current_user: User = Depends(get_current_user),
    _: User = Depends(RequirePermissions(["ADMIN_SETTINGS"])),
):
    """Send a test email."""
    import smtplib
    from email.mime.text import MIMEText
    
    settings_data = load_app_settings()
    email_config = settings_data.get("email", {})
    
    if not email_config.get("smtp_server"):
        raise HTTPException(status_code=400, detail="SMTP server not configured")
    
    try:
        msg = MIMEText("This is a test email from ARS Application.")
        msg['Subject'] = "ARS Test Email"
        msg['From'] = email_config.get("from_address", "noreply@ars.local")
        msg['To'] = body.to_address
        
        server = smtplib.SMTP(email_config["smtp_server"], email_config.get("smtp_port", 587))
        if email_config.get("use_tls"):
            server.starttls()
        if email_config.get("smtp_username") and email_config.get("smtp_password"):
            server.login(email_config["smtp_username"], email_config["smtp_password"])
        server.send_message(msg)
        server.quit()
        
        return APIResponse(message=f"Test email sent to {body.to_address}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Email failed: {str(e)}")


# ============================================================================
# System Info
# ============================================================================

@router.get("/system/info", response_model=APIResponse)
async def get_system_info(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get comprehensive system information."""
    import platform
    import sys
    import psutil
    from datetime import datetime, timedelta
    
    # System metrics
    try:
        cpu_percent = psutil.cpu_percent(interval=0.5)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        boot_time = datetime.fromtimestamp(psutil.boot_time())
        uptime = datetime.now() - boot_time
    except:
        cpu_percent = 0
        memory = None
        disk = None
        uptime = timedelta(0)
    
    # Data database stats
    try:
        engine = get_data_engine()
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 
                    COUNT(*) as table_count,
                    (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS) as column_count
                FROM INFORMATION_SCHEMA.TABLES 
                WHERE TABLE_TYPE = 'BASE TABLE'
            """))
            row = result.fetchone()
            data_table_count = row[0] if row else 0
            data_column_count = row[1] if row else 0
            
            # Get database size
            size_result = conn.execute(text("""
                SELECT 
                    SUM(CAST(FILEPROPERTY(name, 'SpaceUsed') AS BIGINT) * 8 / 1024) as size_mb
                FROM sys.database_files
            """))
            size_row = size_result.fetchone()
            data_db_size_mb = size_row[0] if size_row and size_row[0] else 0
    except:
        data_table_count = 0
        data_column_count = 0
        data_db_size_mb = 0
    
    # System database stats
    try:
        engine = get_system_engine()
        with engine.connect() as conn:
            # Get database size
            size_result = conn.execute(text("""
                SELECT 
                    SUM(CAST(FILEPROPERTY(name, 'SpaceUsed') AS BIGINT) * 8 / 1024) as size_mb
                FROM sys.database_files
            """))
            size_row = size_result.fetchone()
            system_db_size_mb = size_row[0] if size_row and size_row[0] else 0
    except:
        system_db_size_mb = 0
    
    # Active users (logged in within last 24 hours)
    try:
        active_users_result = db.execute(text("""
            SELECT COUNT(*) FROM users 
            WHERE last_login >= DATEADD(day, -1, GETDATE()) AND is_active = 1
        """))
        active_users = active_users_result.scalar() or 0
        
        total_users_result = db.execute(text("SELECT COUNT(*) FROM users WHERE is_active = 1"))
        total_users = total_users_result.scalar() or 0
    except:
        active_users = 0
        total_users = 0
    
    return APIResponse(data={
        # Server info
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "hostname": platform.node(),
        "processor": platform.processor() or "Unknown",
        
        # Resource usage
        "cpu_percent": cpu_percent,
        "memory_total_gb": round(memory.total / (1024**3), 2) if memory else 0,
        "memory_used_gb": round(memory.used / (1024**3), 2) if memory else 0,
        "memory_percent": memory.percent if memory else 0,
        "disk_total_gb": round(disk.total / (1024**3), 2) if disk else 0,
        "disk_used_gb": round(disk.used / (1024**3), 2) if disk else 0,
        "disk_percent": disk.percent if disk else 0,
        
        # Uptime
        "uptime_days": uptime.days,
        "uptime_hours": uptime.seconds // 3600,
        "uptime_formatted": f"{uptime.days}d {uptime.seconds // 3600}h {(uptime.seconds % 3600) // 60}m",
        
        # Database stats
        "data_db": {
            "tables": data_table_count,
            "columns": data_column_count,
            "size_mb": data_db_size_mb,
        },
        "system_db": {
            "size_mb": system_db_size_mb,
        },
        
        # User stats
        "active_users_24h": active_users,
        "total_users": total_users,
        
        # Current user
        "current_user": current_user.username,
    })


# ============================================================================
# Database Backup
# ============================================================================

class BackupRequest(BaseModel):
    database: str = Field(..., description="Which database to backup: 'system', 'data', or 'both'")


@router.post("/backup/create", response_model=APIResponse)
async def create_backup(
    body: BackupRequest,
    current_user: User = Depends(get_current_user),
    _: User = Depends(RequirePermissions(["ADMIN_SETTINGS"])),
):
    """Create database backup."""
    # Ensure backup directory exists
    os.makedirs(BACKUP_DIR, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []
    
    if body.database in ['system', 'both']:
        try:
            backup_file = os.path.join(BACKUP_DIR, f"system_db_{timestamp}.bak")
            engine = get_system_engine()
            db_name = settings.SQL_DATABASE or "Claude"
            with engine.connect() as conn:
                conn.execute(text(f"""
                    BACKUP DATABASE [{db_name}] 
                    TO DISK = N'{backup_file}' 
                    WITH FORMAT, INIT, COMPRESSION,
                    NAME = N'System DB Backup - {timestamp}'
                """))
                conn.commit()
            results.append({"database": "system", "status": "success", "file": backup_file})
        except Exception as e:
            results.append({"database": "system", "status": "failed", "error": str(e)[:200]})
    
    if body.database in ['data', 'both']:
        try:
            backup_file = os.path.join(BACKUP_DIR, f"data_db_{timestamp}.bak")
            engine = get_data_engine()
            db_name = settings.DATA_DATABASE or "Rep_data"
            with engine.connect() as conn:
                conn.execute(text(f"""
                    BACKUP DATABASE [{db_name}] 
                    TO DISK = N'{backup_file}' 
                    WITH FORMAT, INIT, COMPRESSION,
                    NAME = N'Data DB Backup - {timestamp}'
                """))
                conn.commit()
            results.append({"database": "data", "status": "success", "file": backup_file})
        except Exception as e:
            results.append({"database": "data", "status": "failed", "error": str(e)[:200]})
    
    success_count = sum(1 for r in results if r["status"] == "success")
    
    return APIResponse(
        data={"backups": results, "backup_dir": BACKUP_DIR},
        message=f"{success_count} backup(s) created successfully"
    )


@router.get("/backup/list", response_model=APIResponse)
async def list_backups(
    current_user: User = Depends(get_current_user),
    _: User = Depends(RequirePermissions(["ADMIN_SETTINGS"])),
):
    """List available backups."""
    if not os.path.exists(BACKUP_DIR):
        return APIResponse(data={"backups": [], "backup_dir": BACKUP_DIR})
    
    backups = []
    for filename in os.listdir(BACKUP_DIR):
        if filename.endswith('.bak'):
            filepath = os.path.join(BACKUP_DIR, filename)
            stat = os.stat(filepath)
            backups.append({
                "filename": filename,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                "database": "system" if filename.startswith("system_") else "data",
            })
    
    backups.sort(key=lambda x: x["created"], reverse=True)
    
    return APIResponse(data={"backups": backups, "backup_dir": BACKUP_DIR})


@router.delete("/backup/{filename}", response_model=APIResponse)
async def delete_backup(
    filename: str,
    current_user: User = Depends(get_current_user),
    _: User = Depends(RequirePermissions(["ADMIN_SETTINGS"])),
):
    """Delete a backup file."""
    if not filename.endswith('.bak'):
        raise HTTPException(status_code=400, detail="Invalid backup file")
    
    filepath = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Backup not found")
    
    os.remove(filepath)
    return APIResponse(message=f"Backup '{filename}' deleted successfully")


# ============================================================================
# Get Settings by Category (MUST BE LAST - catches all paths)
# ============================================================================

@router.get("/{category}", response_model=APIResponse)
async def get_settings_category(
    category: str,
    current_user: User = Depends(get_current_user),
    _: User = Depends(RequirePermissions(["ADMIN_SETTINGS"])),
):
    """Get settings for a specific category."""
    settings_data = load_app_settings()
    if category not in settings_data:
        raise HTTPException(status_code=404, detail=f"Category '{category}' not found")
    
    result = settings_data[category]
    if category == "email" and result.get("smtp_password"):
        result["smtp_password"] = PASSWORD_MASK
    if category == "database" and result.get("password"):
        result["password"] = PASSWORD_MASK

    return APIResponse(data=result)
