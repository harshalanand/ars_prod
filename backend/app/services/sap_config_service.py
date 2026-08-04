"""
SAP gateway connection config — the "SAP Connection" tab of the SAP module.

Owns the single active row in SAP_CONNECTION: the MCP gateway worker URL, the
API key (encrypted at rest), the default SAP environment, and the enabled flag.
This is the SAP equivalent of the Database / WhatsApp settings — configure once,
then every pull uses it.

The app never talks to SAP directly. It calls the V2 "universal MCP" worker over
HTTPS with the X-API-Key header; the worker relays to SAP (RFC / OData) and
returns JSON rows. So there is NO SAP SDK on this server.

Storage:  Rep_data (data engine), self-healing DDL (mirrors whatsapp_settings).
Secrets:  API_KEY stored via app.core.crypto (Fernet); never returned raw to the
          UI (masked), decrypted only for the outbound gateway call.
"""
import json
from datetime import datetime
from typing import Any, Dict, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine
from app.core.crypto import encrypt_secret, decrypt_secret

CONN_TABLE = "SAP_CONNECTION"
MASK = "********"
DEFAULT_WORKER_URL = "https://universal-mcp.akash-bab.workers.dev"
_ACTIVE_ID = 1
VALID_ENVS = ("dev", "qa", "prod")
VALID_DISPLAY = ("name", "label", "both")
# NOTE: Snowflake configuration has been moved OUT of the SAP connection into a
# dedicated app-wide config (snowflake_config_service, surfaced at Settings →
# Snowflake) so the SAP Snowflake door and the Report engine share one setting.
# The legacy SF_CONFIG column on SAP_CONNECTION is left in place but unused.


# ── Schema (self-healing) ────────────────────────────────────────────────────
def ensure_sap_tables() -> None:
    eng = get_data_engine()
    with eng.begin() as c:
        c.execute(text(f"""
            IF OBJECT_ID('dbo.{CONN_TABLE}', 'U') IS NULL
            CREATE TABLE dbo.{CONN_TABLE} (
                ID            INT            NOT NULL PRIMARY KEY,
                WORKER_URL    NVARCHAR(400)  NULL,
                API_KEY       NVARCHAR(MAX)  NULL,
                DEFAULT_ENV   NVARCHAR(10)   NOT NULL DEFAULT 'prod',
                DISPLAY_MODE  NVARCHAR(10)   NOT NULL DEFAULT 'name',
                ENABLED       BIT            NOT NULL DEFAULT 0,
                LAST_STATUS   NVARCHAR(32)   NULL,
                LAST_MESSAGE  NVARCHAR(1000) NULL,
                LAST_VERIFIED DATETIME2      NULL,
                CREATED_BY    NVARCHAR(128)  NULL,
                CREATED_DATE  DATETIME2      NULL DEFAULT SYSUTCDATETIME(),
                MODIFIED_BY   NVARCHAR(128)  NULL,
                MODIFIED_DATE DATETIME2      NULL
            )
        """))
        # Self-heal for connections created before the display setting existed.
        c.execute(text(f"""
            IF COL_LENGTH('dbo.{CONN_TABLE}','DISPLAY_MODE') IS NULL
            ALTER TABLE dbo.{CONN_TABLE} ADD DISPLAY_MODE NVARCHAR(10) NOT NULL DEFAULT 'name'
        """))
        # Self-heal for the Snowflake door (encrypted JSON blob).
        c.execute(text(f"""
            IF COL_LENGTH('dbo.{CONN_TABLE}','SF_CONFIG') IS NULL
            ALTER TABLE dbo.{CONN_TABLE} ADD SF_CONFIG NVARCHAR(MAX) NULL
        """))


# ── Read / write ─────────────────────────────────────────────────────────────
def _empty() -> Dict[str, Any]:
    return {
        "worker_url": DEFAULT_WORKER_URL, "api_key": "", "default_env": "prod",
        "display_mode": "name",
        "enabled": False, "has_key": False,
        "last_status": None, "last_message": None, "last_verified": None,
    }


def _load_row() -> Optional[Dict[str, Any]]:
    ensure_sap_tables()
    eng = get_data_engine()
    with eng.connect() as c:
        row = c.execute(text(f"""
            SELECT WORKER_URL, API_KEY, DEFAULT_ENV, DISPLAY_MODE, ENABLED,
                   SF_CONFIG, LAST_STATUS, LAST_MESSAGE, LAST_VERIFIED
            FROM {CONN_TABLE} WHERE ID = :id
        """), {"id": _ACTIVE_ID}).mappings().fetchone()
    return dict(row) if row else None


def get_config(reveal: bool = False) -> Dict[str, Any]:
    """Active connection. api_key is MASKed unless reveal=True (reveal is used
    internally by sap_client for the outbound call, never exposed to the UI)."""
    row = _load_row()
    if not row:
        return _empty()
    plain = decrypt_secret(row.get("API_KEY") or "")
    lv = row.get("LAST_VERIFIED")
    return {
        "worker_url": row.get("WORKER_URL") or DEFAULT_WORKER_URL,
        "api_key": (plain if reveal else (MASK if plain else "")),
        "default_env": (row.get("DEFAULT_ENV") or "prod").lower(),
        "display_mode": (row.get("DISPLAY_MODE") or "name").lower(),
        "enabled": bool(row.get("ENABLED")),
        "has_key": bool(plain),
        "last_status": row.get("LAST_STATUS"),
        "last_message": row.get("LAST_MESSAGE"),
        "last_verified": lv.isoformat() if hasattr(lv, "isoformat") else lv,
    }


def save_config(payload: Dict[str, Any], user: str) -> Dict[str, Any]:
    """Upsert the single active connection. A MASK api_key means 'keep stored'."""
    ensure_sap_tables()

    incoming = payload.get("api_key")
    if incoming == MASK or incoming is None:
        row = _load_row()
        stored_key = (row or {}).get("API_KEY") or ""
    else:
        stored_key = encrypt_secret(str(incoming))

    env = (payload.get("default_env") or "prod").lower()
    if env not in VALID_ENVS:
        env = "prod"
    disp = (payload.get("display_mode") or "name").lower()
    if disp not in VALID_DISPLAY:
        disp = "name"

    vals = {
        "id": _ACTIVE_ID,
        "url": (payload.get("worker_url") or DEFAULT_WORKER_URL).strip(),
        "key": stored_key,
        "env": env,
        "disp": disp,
        "en": 1 if payload.get("enabled") else 0,
        "user": user,
    }
    eng = get_data_engine()
    with eng.begin() as c:
        updated = c.execute(text(f"""
            UPDATE {CONN_TABLE} SET
                WORKER_URL=:url, API_KEY=:key, DEFAULT_ENV=:env, DISPLAY_MODE=:disp, ENABLED=:en,
                LAST_STATUS=NULL, LAST_MESSAGE=NULL, LAST_VERIFIED=NULL,
                MODIFIED_BY=:user, MODIFIED_DATE=SYSUTCDATETIME()
            WHERE ID=:id
        """), vals).rowcount
        if not updated:
            c.execute(text(f"""
                INSERT INTO {CONN_TABLE}
                    (ID, WORKER_URL, API_KEY, DEFAULT_ENV, DISPLAY_MODE, ENABLED,
                     CREATED_BY, CREATED_DATE, MODIFIED_BY, MODIFIED_DATE)
                VALUES (:id, :url, :key, :env, :disp, :en, :user, SYSUTCDATETIME(),
                        :user, SYSUTCDATETIME())
            """), vals)
    return get_config()


def persist_status(status: str, message: Optional[str] = None) -> None:
    """Cache the last Test-Connection outcome on the active row."""
    try:
        ensure_sap_tables()
        eng = get_data_engine()
        with eng.begin() as c:
            c.execute(text(f"""
                UPDATE {CONN_TABLE}
                SET LAST_STATUS=:s, LAST_MESSAGE=:m, LAST_VERIFIED=:v
                WHERE ID=:id
            """), {"s": status, "m": (message or "")[:1000],
                   "v": datetime.utcnow(), "id": _ACTIVE_ID})
    except Exception as e:
        logger.warning(f"[sap] status cache update failed: {e}")
