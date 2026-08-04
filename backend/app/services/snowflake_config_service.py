"""
Snowflake connection — the single, app-wide Snowflake configuration.

This is the ONE place Snowflake credentials live. Both consumers read from it:
  • the SAP module's "Snowflake door" (sap_snowflake_client)
  • the Report Generation engine / scheduler (snowflake_sync)

Supports key-pair (JWT) auth — matching the global Snowflake MCP setup — and
username/password. Secrets (password, key passphrase) are encrypted at rest
(Fernet). Read helpers return them masked for the UI; connect() decrypts.

Storage: Rep_data (data engine), self-healing DDL (mirrors sap_config_service).
"""
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine
from app.core.crypto import encrypt_secret, decrypt_secret

CONN_TABLE = "SNOWFLAKE_CONNECTION"
MASK = "********"
_ACTIVE_ID = 1
VALID_AUTH = ("keypair", "password")
_QUERY_TIMEOUT = 120

# Pre-filled, non-secret defaults (the known-good V2 values / user's setup).
_DEFAULT: Dict[str, Any] = {
    "account": "iafphkw-hh80816",
    "user": "SANTOSH",
    "auth": "keypair",
    "private_key_path": r"C:\Users\santosh.kumar3\.snowflake\santosh_rsa.p8",
    "private_key_pwd": "",
    "password": "",
    "role": "DATA_PLATFORM_ADMIN",
    "warehouse": "COMPUTE_WH",
    "database": "V2RETAIL",
    "schema": "BRONZE",
    "enabled": False,
}
_PLAIN_KEYS = ("account", "user", "auth", "private_key_path",
               "role", "warehouse", "database", "schema")
_SECRET_KEYS = ("password", "private_key_pwd")


class SnowflakeError(Exception):
    """Config- or query-level failure, surfaced with an actionable message."""


# ── Schema (self-healing) ────────────────────────────────────────────────────
def ensure_table() -> None:
    eng = get_data_engine()
    with eng.begin() as c:
        c.execute(text(f"""
            IF OBJECT_ID('dbo.{CONN_TABLE}', 'U') IS NULL
            CREATE TABLE dbo.{CONN_TABLE} (
                ID             INT            NOT NULL PRIMARY KEY,
                SF_ACCOUNT     NVARCHAR(128)  NULL,
                SF_USER        NVARCHAR(128)  NULL,
                SF_AUTH        NVARCHAR(20)   NOT NULL DEFAULT 'keypair',
                SF_KEY_PATH    NVARCHAR(400)  NULL,
                SF_KEY_PWD     NVARCHAR(MAX)  NULL,   -- Fernet-encrypted
                SF_PASSWORD    NVARCHAR(MAX)  NULL,   -- Fernet-encrypted
                SF_ROLE        NVARCHAR(64)   NULL,
                SF_WAREHOUSE   NVARCHAR(64)   NULL,
                SF_DATABASE    NVARCHAR(128)  NULL,
                SF_SCHEMA      NVARCHAR(128)  NULL,
                ENABLED        BIT            NOT NULL DEFAULT 0,
                LAST_STATUS    NVARCHAR(32)   NULL,
                LAST_MESSAGE   NVARCHAR(1000) NULL,
                LAST_VERIFIED  DATETIME2      NULL,
                CREATED_BY     NVARCHAR(128)  NULL,
                CREATED_DATE   DATETIME2      NULL DEFAULT SYSUTCDATETIME(),
                MODIFIED_BY    NVARCHAR(128)  NULL,
                MODIFIED_DATE  DATETIME2      NULL
            )
        """))


# ── Read / write ─────────────────────────────────────────────────────────────
def _load_row() -> Optional[Dict[str, Any]]:
    ensure_table()
    eng = get_data_engine()
    with eng.connect() as c:
        row = c.execute(text(f"""
            SELECT SF_ACCOUNT, SF_USER, SF_AUTH, SF_KEY_PATH, SF_KEY_PWD, SF_PASSWORD,
                   SF_ROLE, SF_WAREHOUSE, SF_DATABASE, SF_SCHEMA, ENABLED,
                   LAST_STATUS, LAST_MESSAGE, LAST_VERIFIED
            FROM {CONN_TABLE} WHERE ID = :id
        """), {"id": _ACTIVE_ID}).mappings().fetchone()
    return dict(row) if row else None


def _merged(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge a DB row over defaults into a flat plaintext dict (secrets decrypted)."""
    m = dict(_DEFAULT)
    if row:
        m.update({
            "account": row.get("SF_ACCOUNT") or _DEFAULT["account"],
            "user": row.get("SF_USER") or _DEFAULT["user"],
            "auth": (row.get("SF_AUTH") or "keypair").lower(),
            "private_key_path": row.get("SF_KEY_PATH") or _DEFAULT["private_key_path"],
            "private_key_pwd": decrypt_secret(row.get("SF_KEY_PWD") or ""),
            "password": decrypt_secret(row.get("SF_PASSWORD") or ""),
            "role": row.get("SF_ROLE") or _DEFAULT["role"],
            "warehouse": row.get("SF_WAREHOUSE") or _DEFAULT["warehouse"],
            "database": row.get("SF_DATABASE") or _DEFAULT["database"],
            "schema": row.get("SF_SCHEMA") or _DEFAULT["schema"],
            "enabled": bool(row.get("ENABLED")),
        })
    return m


def get_config(reveal: bool = False) -> Dict[str, Any]:
    """reveal=True → plaintext secrets (used by connect()); reveal=False → secrets
    masked + has_* flags + last status (for the UI)."""
    row = _load_row()
    m = _merged(row)
    if reveal:
        return m
    lv = (row or {}).get("LAST_VERIFIED")
    out = {k: m[k] for k in _PLAIN_KEYS}
    out["enabled"] = m["enabled"]
    out["has_password"] = bool(m.get("password"))
    out["password"] = MASK if m.get("password") else ""
    out["has_key_pwd"] = bool(m.get("private_key_pwd"))
    out["private_key_pwd"] = MASK if m.get("private_key_pwd") else ""
    out["last_status"] = (row or {}).get("LAST_STATUS")
    out["last_message"] = (row or {}).get("LAST_MESSAGE")
    out["last_verified"] = lv.isoformat() if hasattr(lv, "isoformat") else lv
    return out


def save_config(payload: Dict[str, Any], user: str) -> Dict[str, Any]:
    """Upsert the single active config. Masked secrets ('********') keep stored."""
    ensure_table()
    cur = get_config(reveal=True)  # current plaintext
    for k in _PLAIN_KEYS:
        if payload.get(k) is not None:
            cur[k] = str(payload[k]).strip()
    if (cur.get("auth") or "keypair").lower() not in VALID_AUTH:
        cur["auth"] = "keypair"
    if "enabled" in payload:
        cur["enabled"] = bool(payload["enabled"])
    for sk in _SECRET_KEYS:
        v = payload.get(sk)
        if v is not None and v != MASK:
            cur[sk] = str(v)

    vals = {
        "id": _ACTIVE_ID,
        "acct": cur["account"], "usr": cur["user"], "auth": cur["auth"],
        "kpath": cur["private_key_path"],
        "kpwd": encrypt_secret(cur.get("private_key_pwd") or ""),
        "pwd": encrypt_secret(cur.get("password") or ""),
        "role": cur["role"], "wh": cur["warehouse"],
        "db": cur["database"], "sch": cur["schema"],
        "en": 1 if cur["enabled"] else 0, "user": user,
    }
    eng = get_data_engine()
    with eng.begin() as c:
        updated = c.execute(text(f"""
            UPDATE {CONN_TABLE} SET
                SF_ACCOUNT=:acct, SF_USER=:usr, SF_AUTH=:auth, SF_KEY_PATH=:kpath,
                SF_KEY_PWD=:kpwd, SF_PASSWORD=:pwd, SF_ROLE=:role, SF_WAREHOUSE=:wh,
                SF_DATABASE=:db, SF_SCHEMA=:sch, ENABLED=:en,
                LAST_STATUS=NULL, LAST_MESSAGE=NULL, LAST_VERIFIED=NULL,
                MODIFIED_BY=:user, MODIFIED_DATE=SYSUTCDATETIME()
            WHERE ID=:id
        """), vals).rowcount
        if not updated:
            c.execute(text(f"""
                INSERT INTO {CONN_TABLE}
                    (ID, SF_ACCOUNT, SF_USER, SF_AUTH, SF_KEY_PATH, SF_KEY_PWD, SF_PASSWORD,
                     SF_ROLE, SF_WAREHOUSE, SF_DATABASE, SF_SCHEMA, ENABLED,
                     CREATED_BY, CREATED_DATE, MODIFIED_BY, MODIFIED_DATE)
                VALUES (:id, :acct, :usr, :auth, :kpath, :kpwd, :pwd,
                        :role, :wh, :db, :sch, :en, :user, SYSUTCDATETIME(),
                        :user, SYSUTCDATETIME())
            """), vals)
    return get_config()


def persist_status(status: str, message: Optional[str] = None) -> None:
    try:
        ensure_table()
        eng = get_data_engine()
        with eng.begin() as c:
            c.execute(text(f"""
                UPDATE {CONN_TABLE}
                SET LAST_STATUS=:s, LAST_MESSAGE=:m, LAST_VERIFIED=:v WHERE ID=:id
            """), {"s": status, "m": (message or "")[:1000],
                   "v": datetime.utcnow(), "id": _ACTIVE_ID})
    except Exception as e:
        logger.warning(f"[snowflake] status cache update failed: {e}")


def is_enabled() -> bool:
    return bool(get_config(reveal=True).get("enabled"))


# ── Connection + query primitives (shared by SAP door and report engine) ─────
def _load_private_key_der(path: str, passphrase: Optional[str]) -> bytes:
    from cryptography.hazmat.primitives import serialization
    try:
        with open(path, "rb") as f:
            pem = f.read()
    except OSError as e:
        raise SnowflakeError(f"Cannot read Snowflake private key file '{path}': {e}")
    try:
        key = serialization.load_pem_private_key(
            pem, password=(passphrase.encode() if passphrase else None))
    except Exception as e:
        raise SnowflakeError(f"Invalid Snowflake private key: {e}")
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())


def connect(require_enabled: bool = True):
    """Open a snowflake.connector connection from the stored config."""
    try:
        import snowflake.connector  # noqa
    except ImportError as e:
        raise SnowflakeError(
            "snowflake-connector-python is not installed on the server — run: "
            "pip install 'snowflake-connector-python[pandas]'") from e
    cfg = get_config(reveal=True)
    if require_enabled and not cfg.get("enabled"):
        raise SnowflakeError("Snowflake is disabled — enable it in Settings → Snowflake.")
    account = (cfg.get("account") or "").strip()
    user = (cfg.get("user") or "").strip()
    if not account or not user:
        raise SnowflakeError("Snowflake account and user are required (Settings → Snowflake).")
    kwargs: Dict[str, Any] = {"account": account, "user": user,
                              "login_timeout": 20, "network_timeout": _QUERY_TIMEOUT}
    for k in ("role", "warehouse", "database", "schema"):
        v = (cfg.get(k) or "").strip()
        if v:
            kwargs[k] = v
    if (cfg.get("auth") or "keypair").lower() == "keypair":
        path = (cfg.get("private_key_path") or "").strip()
        if not path:
            raise SnowflakeError("Key-pair auth needs a private key file path (Settings → Snowflake).")
        kwargs["private_key"] = _load_private_key_der(path, cfg.get("private_key_pwd") or None)
    else:
        pwd = cfg.get("password") or ""
        if not pwd:
            raise SnowflakeError("Password auth needs a password (Settings → Snowflake).")
        kwargs["password"] = pwd
    try:
        return snowflake.connector.connect(**kwargs)
    except Exception as e:
        raise SnowflakeError(f"Snowflake connection failed: {e}")


def test_connection() -> Dict[str, Any]:
    try:
        conn = connect(require_enabled=False)
    except SnowflakeError as e:
        persist_status("Error", str(e))
        return {"success": False, "message": str(e)}
    try:
        cur = conn.cursor()
        cur.execute("SELECT CURRENT_VERSION(), CURRENT_ACCOUNT(), CURRENT_WAREHOUSE()")
        ver, acct, wh = cur.fetchone()
        msg = f"Connected to Snowflake {acct} (v{ver}, wh={wh or '-'})."
        persist_status("Connected", msg)
        return {"success": True, "message": msg}
    except Exception as e:
        persist_status("Error", str(e))
        return {"success": False, "message": f"Query failed: {e}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_query(sql: str, row_limit: Optional[int] = None,
              database: Optional[str] = None, schema: Optional[str] = None) -> Dict[str, List]:
    """Read-only SELECT/WITH → {'columns': [...], 'rows': [ {col: val} ]}."""
    if not (sql or "").strip():
        raise SnowflakeError("Snowflake query is empty.")
    head = sql.strip().lstrip("(").lower()
    if not (head.startswith("select") or head.startswith("with")):
        raise SnowflakeError("Only read-only SELECT/WITH queries are allowed.")
    conn = connect()
    try:
        cur = conn.cursor()
        if database and database.strip():
            cur.execute(f"USE DATABASE {database.strip()}")
        if schema and schema.strip():
            cur.execute(f"USE SCHEMA {schema.strip()}")
        cur.execute(sql)
        columns = [d[0] for d in (cur.description or [])]
        fetched = cur.fetchmany(row_limit) if row_limit else cur.fetchall()
        return {"columns": columns, "rows": [dict(zip(columns, r)) for r in fetched]}
    except SnowflakeError:
        raise
    except Exception as e:
        raise SnowflakeError(f"Snowflake query failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_tables(database: Optional[str] = None, schema: Optional[str] = None,
                like: Optional[str] = None, limit: int = 500) -> Dict[str, List]:
    db = (database or "").strip() or (get_config(reveal=True).get("database") or "V2RETAIL")
    sch = (schema or "").strip()
    where = []
    if sch:
        where.append(f"TABLE_SCHEMA = '{sch.replace(chr(39), chr(39) * 2)}'")
    if like and like.strip():
        where.append(f"TABLE_NAME ILIKE '{like.strip().replace(chr(39), chr(39) * 2)}'")
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = (f"SELECT TABLE_SCHEMA, TABLE_NAME, ROW_COUNT, BYTES "
           f"FROM {db}.INFORMATION_SCHEMA.TABLES {wsql} ORDER BY TABLE_SCHEMA, TABLE_NAME")
    return run_query(sql, row_limit=limit)
