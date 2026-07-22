"""
WhatsApp (Meta Cloud API) configuration service — FSD "WhatsApp Configuration".

Owns the two dedicated tables (single active configuration + audit log), the
encryption of the access token at rest, and the live Meta Graph API calls used
by Test Connection / Verify Template / Send Test Message.

Storage:  Rep_data (data engine), self-healing DDL (mirrors report tables).
Secrets:  ACCESS_TOKEN stored via app.core.crypto (Fernet); never returned raw
          to the UI (masked), never written to the audit log (masked).
"""
import json
from datetime import datetime
from typing import Any, Dict, Optional

import requests
from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine
from app.core.crypto import encrypt_secret, decrypt_secret

SETTINGS_TABLE = "APP_WHATSAPP_SETTINGS"
AUDIT_TABLE = "APP_WHATSAPP_AUDIT_LOG"
MASK = "********"
GRAPH_BASE = "https://graph.facebook.com/v20.0"
_HTTP_TIMEOUT = 20

# The single active configuration always lives on this row.
_ACTIVE_ID = 1


# ── Schema (self-healing) ────────────────────────────────────────────────────
def ensure_whatsapp_tables() -> None:
    eng = get_data_engine()
    with eng.begin() as c:
        c.execute(text(f"""
            IF OBJECT_ID('dbo.{SETTINGS_TABLE}', 'U') IS NULL
            CREATE TABLE dbo.{SETTINGS_TABLE} (
                ID               INT             NOT NULL PRIMARY KEY,
                PHONE_NUMBER_ID  NVARCHAR(64)    NULL,
                WABA_ID          NVARCHAR(64)    NULL,
                ACCESS_TOKEN     NVARCHAR(MAX)   NULL,   -- Fernet-encrypted ('enc:...')
                DEFAULT_TEMPLATE NVARCHAR(256)   NULL,
                ENABLED          BIT             NOT NULL DEFAULT 0,
                API_STATUS       NVARCHAR(32)    NULL,
                TOKEN_STATUS     NVARCHAR(32)    NULL,
                TEMPLATE_STATUS  NVARCHAR(32)    NULL,
                WEBHOOK_STATUS   NVARCHAR(32)    NULL,
                LAST_VERIFIED    DATETIME2       NULL,
                CREATED_BY       NVARCHAR(128)   NULL,
                CREATED_DATE     DATETIME2       NULL DEFAULT SYSUTCDATETIME(),
                MODIFIED_BY      NVARCHAR(128)   NULL,
                MODIFIED_DATE    DATETIME2       NULL
            )
        """))
        c.execute(text(f"""
            IF OBJECT_ID('dbo.{AUDIT_TABLE}', 'U') IS NULL
            CREATE TABLE dbo.{AUDIT_TABLE} (
                LOG_ID       INT IDENTITY(1,1) PRIMARY KEY,
                USER_NAME    NVARCHAR(128)  NULL,
                ACTION       NVARCHAR(64)   NULL,
                OLD_VALUE    NVARCHAR(MAX)  NULL,
                NEW_VALUE    NVARCHAR(MAX)  NULL,
                IP_ADDRESS   NVARCHAR(64)   NULL,
                MACHINE_NAME NVARCHAR(256)  NULL,
                LOG_DATE     DATETIME2      NULL DEFAULT SYSUTCDATETIME()
            )
        """))
        # Self-heal status/verification columns for tables created before they existed.
        for col, ddl in [
            ("API_STATUS", "NVARCHAR(32) NULL"),
            ("TOKEN_STATUS", "NVARCHAR(32) NULL"),
            ("TEMPLATE_STATUS", "NVARCHAR(32) NULL"),
            ("WEBHOOK_STATUS", "NVARCHAR(32) NULL"),
            ("LAST_VERIFIED", "DATETIME2 NULL"),
        ]:
            c.execute(text(f"""
                IF NOT EXISTS (SELECT 1 FROM sys.columns
                    WHERE object_id = OBJECT_ID('dbo.{SETTINGS_TABLE}') AND name = '{col}')
                ALTER TABLE dbo.{SETTINGS_TABLE} ADD {col} {ddl}
            """))


# ── Read / write config ──────────────────────────────────────────────────────
def _empty_config() -> Dict[str, Any]:
    return {
        "phone_number_id": "", "waba_id": "", "access_token": "",
        "default_template": "", "enabled": False,
        "api_status": None, "token_status": None, "template_status": None,
        "webhook_status": "Not Configured", "last_verified": None,
        "has_token": False,
    }


def _load_row() -> Optional[Dict[str, Any]]:
    ensure_whatsapp_tables()
    eng = get_data_engine()
    with eng.connect() as c:
        row = c.execute(text(f"""
            SELECT PHONE_NUMBER_ID, WABA_ID, ACCESS_TOKEN, DEFAULT_TEMPLATE, ENABLED,
                   API_STATUS, TOKEN_STATUS, TEMPLATE_STATUS, WEBHOOK_STATUS, LAST_VERIFIED,
                   MODIFIED_BY, MODIFIED_DATE
            FROM {SETTINGS_TABLE} WHERE ID = :id
        """), {"id": _ACTIVE_ID}).mappings().fetchone()
    return dict(row) if row else None


def get_config(reveal: bool = False) -> Dict[str, Any]:
    """Return the active configuration. access_token is MASKed unless reveal=True
    (reveal is used internally by the delivery service, never exposed to the UI)."""
    row = _load_row()
    if not row:
        return _empty_config()
    stored = row.get("ACCESS_TOKEN") or ""
    plain = decrypt_secret(stored)
    lv = row.get("LAST_VERIFIED")
    return {
        "phone_number_id": row.get("PHONE_NUMBER_ID") or "",
        "waba_id": row.get("WABA_ID") or "",
        "access_token": (plain if reveal else (MASK if plain else "")),
        "default_template": row.get("DEFAULT_TEMPLATE") or "",
        "enabled": bool(row.get("ENABLED")),
        "api_status": row.get("API_STATUS"),
        "token_status": row.get("TOKEN_STATUS"),
        "template_status": row.get("TEMPLATE_STATUS"),
        "webhook_status": row.get("WEBHOOK_STATUS") or "Not Configured",
        "last_verified": lv.isoformat() if hasattr(lv, "isoformat") else lv,
        "has_token": bool(plain),
    }


def _audit_snapshot(cfg: Dict[str, Any]) -> str:
    """A masked, log-safe view of a config for the audit trail."""
    return json.dumps({
        "phone_number_id": cfg.get("phone_number_id", ""),
        "waba_id": cfg.get("waba_id", ""),
        "access_token": MASK if cfg.get("has_token") or cfg.get("access_token") else "",
        "default_template": cfg.get("default_template", ""),
        "enabled": bool(cfg.get("enabled")),
    }, ensure_ascii=False)


def _write_audit(user: str, action: str, old: Optional[str], new: Optional[str],
                 ip: Optional[str], machine: Optional[str]) -> None:
    try:
        eng = get_data_engine()
        with eng.begin() as c:
            c.execute(text(f"""
                INSERT INTO {AUDIT_TABLE}
                    (USER_NAME, ACTION, OLD_VALUE, NEW_VALUE, IP_ADDRESS, MACHINE_NAME)
                VALUES (:u, :a, :o, :n, :ip, :m)
            """), {"u": user, "a": action, "o": old, "n": new, "ip": ip, "m": machine})
    except Exception as e:  # audit must never break the operation
        logger.warning(f"[whatsapp] audit write failed: {e}")


def save_config(payload: Dict[str, Any], user: str,
                ip: Optional[str] = None, machine: Optional[str] = None) -> Dict[str, Any]:
    """Upsert the single active configuration. A MASK access_token means
    'keep the stored one'. Writes an audit-log entry with old→new (masked)."""
    ensure_whatsapp_tables()
    before = get_config()  # masked, for audit

    incoming_token = payload.get("access_token")
    if incoming_token == MASK or incoming_token is None:
        # Preserve the stored (already-encrypted) token.
        row = _load_row()
        stored_token = (row or {}).get("ACCESS_TOKEN") or ""
    else:
        stored_token = encrypt_secret(str(incoming_token))

    vals = {
        "id": _ACTIVE_ID,
        "pnid": (payload.get("phone_number_id") or "").strip(),
        "waba": (payload.get("waba_id") or "").strip(),
        "tok": stored_token,
        "tpl": (payload.get("default_template") or "").strip(),
        "en": 1 if payload.get("enabled") else 0,
        "user": user,
    }
    eng = get_data_engine()
    with eng.begin() as c:
        updated = c.execute(text(f"""
            UPDATE {SETTINGS_TABLE} SET
                PHONE_NUMBER_ID=:pnid, WABA_ID=:waba, ACCESS_TOKEN=:tok,
                DEFAULT_TEMPLATE=:tpl, ENABLED=:en,
                -- credentials may have changed → cached verification is now stale
                API_STATUS=NULL, TOKEN_STATUS=NULL, TEMPLATE_STATUS=NULL, LAST_VERIFIED=NULL,
                MODIFIED_BY=:user, MODIFIED_DATE=SYSUTCDATETIME()
            WHERE ID=:id
        """), vals).rowcount
        if not updated:
            c.execute(text(f"""
                INSERT INTO {SETTINGS_TABLE}
                    (ID, PHONE_NUMBER_ID, WABA_ID, ACCESS_TOKEN, DEFAULT_TEMPLATE,
                     ENABLED, CREATED_BY, CREATED_DATE, MODIFIED_BY, MODIFIED_DATE)
                VALUES (:id, :pnid, :waba, :tok, :tpl, :en, :user, SYSUTCDATETIME(),
                        :user, SYSUTCDATETIME())
            """), vals)

    after = get_config()
    _write_audit(user, "SAVE_CONFIG", _audit_snapshot(before), _audit_snapshot(after), ip, machine)
    return after


def _persist_status(**cols: Any) -> None:
    """Update cached status columns + LAST_VERIFIED on the active row."""
    if not cols:
        return
    sets = ", ".join(f"{k}=:{k}" for k in cols)
    cols["id"] = _ACTIVE_ID
    try:
        eng = get_data_engine()
        with eng.begin() as c:
            c.execute(text(f"UPDATE {SETTINGS_TABLE} SET {sets} WHERE ID=:id"), cols)
    except Exception as e:
        logger.warning(f"[whatsapp] status cache update failed: {e}")


# ── Meta Graph API calls ─────────────────────────────────────────────────────
def _resolve_token(cfg: Dict[str, Any]) -> str:
    """A test/verify call may pass a fresh token (MASK = use stored)."""
    tok = cfg.get("access_token")
    if tok and tok != MASK:
        return str(tok)
    return get_config(reveal=True).get("access_token") or ""


def test_connection(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """FR-02: validate access token + phone number id against Meta."""
    pnid = (cfg.get("phone_number_id") or get_config().get("phone_number_id") or "").strip()
    token = _resolve_token(cfg)
    if not pnid:
        return {"success": False, "error": "Invalid Phone Number ID", "field": "phone_number_id"}
    if not token:
        return {"success": False, "error": "Invalid Access Token", "field": "access_token"}
    try:
        r = requests.get(
            f"{GRAPH_BASE}/{pnid}",
            params={"fields": "verified_name,display_phone_number,quality_rating"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_HTTP_TIMEOUT,
        )
        data = r.json() if r.content else {}
    except Exception as e:
        _persist_status(API_STATUS="Error", TOKEN_STATUS="Unknown", LAST_VERIFIED=datetime.utcnow())
        return {"success": False, "error": f"Failed to Connect to Meta API: {e}"}

    if r.status_code == 200:
        _persist_status(API_STATUS="Connected", TOKEN_STATUS="Valid",
                        LAST_VERIFIED=datetime.utcnow())
        return {
            "success": True,
            "verified_name": data.get("verified_name"),
            "display_phone_number": data.get("display_phone_number"),
            "quality_rating": data.get("quality_rating"),
        }

    err = (data.get("error") or {}) if isinstance(data, dict) else {}
    code = err.get("code")
    msg = err.get("message") or "Failed to Connect to Meta API"
    # 190 = invalid/expired OAuth token; 100/803 = bad object id (phone number id)
    if code == 190:
        _persist_status(API_STATUS="Connected", TOKEN_STATUS="Expired",
                        LAST_VERIFIED=datetime.utcnow())
        return {"success": False, "error": "Invalid Access Token", "detail": msg}
    _persist_status(API_STATUS="Error", LAST_VERIFIED=datetime.utcnow())
    return {"success": False, "error": "Invalid Phone Number ID" if code in (100, 803) else msg,
            "detail": msg}


def verify_template(cfg: Dict[str, Any], template_name: Optional[str] = None) -> Dict[str, Any]:
    """FR-03: validate a template on the WABA and return approval status + language."""
    waba = (cfg.get("waba_id") or get_config().get("waba_id") or "").strip()
    name = (template_name or cfg.get("default_template")
            or get_config().get("default_template") or "").strip()
    token = _resolve_token(cfg)
    if not waba:
        return {"success": False, "error": "WhatsApp Business Account ID is required"}
    if not name:
        return {"success": False, "error": "Template Not Found", "detail": "No template name given"}
    if not token:
        return {"success": False, "error": "Invalid Access Token"}
    try:
        r = requests.get(
            f"{GRAPH_BASE}/{waba}/message_templates",
            params={"name": name, "fields": "name,status,language,category"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_HTTP_TIMEOUT,
        )
        data = r.json() if r.content else {}
    except Exception as e:
        return {"success": False, "error": f"Failed to Connect to Meta API: {e}"}

    if r.status_code != 200:
        err = (data.get("error") or {}) if isinstance(data, dict) else {}
        return {"success": False, "error": err.get("message") or "Failed to Connect to Meta API"}

    matches = [t for t in (data.get("data") or []) if t.get("name") == name]
    if not matches:
        _persist_status(TEMPLATE_STATUS="Not Found", LAST_VERIFIED=datetime.utcnow())
        return {"success": False, "error": "Template Not Found", "template": name}
    t = matches[0]
    _persist_status(TEMPLATE_STATUS=t.get("status"), LAST_VERIFIED=datetime.utcnow())
    return {
        "success": t.get("status") == "APPROVED",
        "template": name,
        "status": t.get("status"),
        "language": t.get("language"),
        "category": t.get("category"),
    }


def send_test_message(cfg: Dict[str, Any], to: str, message: Optional[str] = None) -> Dict[str, Any]:
    """FR-04: send a test message. Uses the default template if configured,
    otherwise a plain text message (only deliverable within the 24h window)."""
    pnid = (cfg.get("phone_number_id") or get_config().get("phone_number_id") or "").strip()
    token = _resolve_token(cfg)
    tpl = (cfg.get("default_template") or get_config().get("default_template") or "").strip()
    to_clean = "".join(ch for ch in (to or "") if ch.isdigit())
    if len(to_clean) < 7:
        return {"success": False, "error": "Invalid Mobile Number"}
    if not pnid:
        return {"success": False, "error": "Invalid Phone Number ID"}
    if not token:
        return {"success": False, "error": "Invalid Access Token"}

    if tpl:
        lang = "en_US"
        # Prefer the template's real language if we can look it up.
        vt = verify_template(cfg, tpl)
        if vt.get("language"):
            lang = vt["language"]
        payload = {
            "messaging_product": "whatsapp", "to": to_clean, "type": "template",
            "template": {"name": tpl, "language": {"code": lang}},
        }
    else:
        payload = {
            "messaging_product": "whatsapp", "to": to_clean, "type": "text",
            "text": {"body": message or "ARS test message."},
        }
    try:
        r = requests.post(
            f"{GRAPH_BASE}/{pnid}/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload, timeout=_HTTP_TIMEOUT,
        )
        data = r.json() if r.content else {}
    except Exception as e:
        return {"success": False, "error": f"Failed to Connect to Meta API: {e}"}

    if r.status_code == 200 and (data.get("messages") or data.get("contacts")):
        mid = (data.get("messages") or [{}])[0].get("id")
        return {"success": True, "message_id": mid, "to": to_clean, "via": "template" if tpl else "text"}
    err = (data.get("error") or {}) if isinstance(data, dict) else {}
    return {"success": False, "error": err.get("message") or "Failed to send test message",
            "detail": err}


def get_status(live: bool = False) -> Dict[str, Any]:
    """FR-06/07: status panel. Returns cached statuses; if live=True and the
    config is complete, re-runs test_connection + verify_template first."""
    cfg = get_config()
    if live and cfg.get("phone_number_id") and cfg.get("has_token"):
        test_connection({})           # updates API_STATUS / TOKEN_STATUS
        if cfg.get("waba_id") and cfg.get("default_template"):
            verify_template({})       # updates TEMPLATE_STATUS
        cfg = get_config()
    return {
        "api_status": cfg.get("api_status") or "Not Verified",
        "token_status": cfg.get("token_status") or "Not Verified",
        "template_status": cfg.get("template_status") or "Not Verified",
        "webhook_status": cfg.get("webhook_status") or "Not Configured",
        "last_verified": cfg.get("last_verified"),
        "enabled": cfg.get("enabled"),
    }


def list_audit(limit: int = 50) -> list:
    ensure_whatsapp_tables()
    eng = get_data_engine()
    with eng.connect() as c:
        rows = c.execute(text(f"""
            SELECT TOP (:lim) LOG_ID, USER_NAME, ACTION, OLD_VALUE, NEW_VALUE,
                   IP_ADDRESS, MACHINE_NAME, LOG_DATE
            FROM {AUDIT_TABLE} ORDER BY LOG_ID DESC
        """), {"lim": limit}).mappings().fetchall()
    out = []
    for r in rows:
        d = dict(r)
        ld = d.get("LOG_DATE")
        if hasattr(ld, "isoformat"):
            d["LOG_DATE"] = ld.isoformat()
        out.append(d)
    return out
