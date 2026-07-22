"""
WhatsApp (Meta Cloud API) Configuration endpoints — FSD "WhatsApp Configuration".

Navigation: Settings → App Settings → WhatsApp.

Access:
  - View / edit config  → ADMIN_SETTINGS permission (SUPER_ADMIN bypasses).
  - Modify Access Token → SUPER_ADMIN only (Business Rule §7).
Every configuration change is written to APP_WHATSAPP_AUDIT_LOG with the acting
user, action, masked old/new values, client IP and machine name.
"""
import socket
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.models.rbac import User
from app.security.dependencies import get_current_user, RequirePermissions
from app.schemas.common import APIResponse
from app.services import whatsapp_settings_service as wa

router = APIRouter(prefix="/settings/whatsapp", tags=["WhatsApp Config"])


# ── Request context helpers ──────────────────────────────────────────────────
def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _machine_name(request: Request, ip: str) -> str:
    # Client may advertise its hostname; else best-effort reverse DNS.
    hdr = request.headers.get("x-machine-name")
    if hdr:
        return hdr[:256]
    if ip:
        try:
            return socket.gethostbyaddr(ip)[0][:256]
        except Exception:
            pass
    return (request.headers.get("user-agent") or "")[:256]


def _is_super_admin(user: User) -> bool:
    try:
        return "SUPER_ADMIN" in set(user.role_codes)
    except Exception:
        return False


# ── Payloads ─────────────────────────────────────────────────────────────────
class WhatsAppConfig(BaseModel):
    phone_number_id: Optional[str] = ""
    waba_id: Optional[str] = ""
    access_token: Optional[str] = None      # MASK ('********') = keep stored
    default_template: Optional[str] = ""
    enabled: bool = False


class TestConnectionBody(BaseModel):
    phone_number_id: Optional[str] = None
    access_token: Optional[str] = None


class VerifyTemplateBody(BaseModel):
    waba_id: Optional[str] = None
    access_token: Optional[str] = None
    template: Optional[str] = None


class SendTestBody(BaseModel):
    to: str
    message: Optional[str] = None


# ── Endpoints ────────────────────────────────────────────────────────────────
@router.get("/config", response_model=APIResponse)
def get_config(current_user: User = Depends(get_current_user),
               _: User = Depends(RequirePermissions(["ADMIN_SETTINGS"]))):
    """FR-01 (read): active configuration with the access token masked."""
    return APIResponse(success=True, data=wa.get_config())


@router.put("/config", response_model=APIResponse)
def save_config(body: WhatsAppConfig, request: Request,
                current_user: User = Depends(get_current_user),
                _: User = Depends(RequirePermissions(["ADMIN_SETTINGS"]))):
    """FR-01/05: save configuration. Modifying the Access Token is SUPER_ADMIN-only."""
    # Numeric-only Phone Number ID (Validation §8).
    pnid = (body.phone_number_id or "").strip()
    if pnid and not pnid.isdigit():
        raise HTTPException(400, "Phone Number ID must be numeric")

    changing_token = body.access_token not in (None, wa.MASK, "")
    if changing_token and not _is_super_admin(current_user):
        raise HTTPException(403, "Only SUPER_ADMIN can modify the Access Token")

    # If enabling delivery, the mandatory fields must be present.
    if body.enabled:
        existing = wa.get_config()
        has_token = changing_token or existing.get("has_token")
        missing = []
        if not pnid:
            missing.append("Phone Number ID")
        if not (body.waba_id or "").strip():
            missing.append("WhatsApp Business Account ID")
        if not has_token:
            missing.append("Access Token")
        if not (body.default_template or "").strip():
            missing.append("Default Template")
        if missing:
            raise HTTPException(400, f"Cannot enable — missing: {', '.join(missing)}")

    ip = _client_ip(request)
    saved = wa.save_config(
        body.model_dump(), user=getattr(current_user, "username", None),
        ip=ip, machine=_machine_name(request, ip),
    )
    return APIResponse(success=True, message="Configuration Saved Successfully.", data=saved)


@router.post("/test-connection", response_model=APIResponse)
def test_connection(body: TestConnectionBody,
                    current_user: User = Depends(get_current_user),
                    _: User = Depends(RequirePermissions(["ADMIN_SETTINGS"]))):
    """FR-02: validate token + phone number id against Meta."""
    res = wa.test_connection(body.model_dump(exclude_none=True))
    if res.get("success"):
        return APIResponse(success=True, message="Connection Successful.", data=res)
    return APIResponse(success=False, message=res.get("error", "Connection failed"), data=res)


@router.post("/verify-template", response_model=APIResponse)
def verify_template(body: VerifyTemplateBody,
                    current_user: User = Depends(get_current_user),
                    _: User = Depends(RequirePermissions(["ADMIN_SETTINGS"]))):
    """FR-03: validate the template and return approval status + language."""
    res = wa.verify_template(body.model_dump(exclude_none=True), body.template)
    if res.get("success"):
        return APIResponse(success=True, message="Template Verified Successfully.", data=res)
    # A found-but-not-approved template is informative, not a hard error.
    msg = res.get("error") or f"Template status: {res.get('status')}"
    return APIResponse(success=False, message=msg, data=res)


@router.post("/send-test", response_model=APIResponse)
def send_test(body: SendTestBody,
              current_user: User = Depends(get_current_user),
              _: User = Depends(RequirePermissions(["ADMIN_SETTINGS"]))):
    """FR-04: send a test message to the entered mobile number."""
    cfg = wa.get_config()
    if not cfg.get("enabled"):
        # FSD error: WhatsApp Delivery Disabled.
        raise HTTPException(400, "WhatsApp Delivery Disabled")
    res = wa.send_test_message({}, body.to, body.message)
    if res.get("success"):
        return APIResponse(success=True, message="Test Message Sent Successfully.", data=res)
    return APIResponse(success=False, message=res.get("error", "Failed to send test message"), data=res)


@router.get("/status", response_model=APIResponse)
def status(live: bool = False, current_user: User = Depends(get_current_user),
           _: User = Depends(RequirePermissions(["ADMIN_SETTINGS"]))):
    """FR-06/07: API / Token / Template / Webhook status + last verified."""
    return APIResponse(success=True, data=wa.get_status(live=live))


@router.get("/audit", response_model=APIResponse)
def audit(limit: int = 50, current_user: User = Depends(get_current_user),
          _: User = Depends(RequirePermissions(["ADMIN_SETTINGS"]))):
    """Configuration change history (Business Rule §7)."""
    return APIResponse(success=True, data=wa.list_audit(limit=min(max(limit, 1), 500)))
