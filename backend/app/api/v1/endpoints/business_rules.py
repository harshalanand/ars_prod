"""
Business Rules API  (prefix /business-rules)

Module-wise behavior switches with Active/Inactive + value, audit log.
SUPER_ADMIN only — read and change (the page itself is superadmin-only;
run-time consumers read rules through the service, not this API).
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.schemas.common import APIResponse
from app.security.dependencies import RequireRoles
from app.models.rbac import User
from app.services import business_rules as svc

router = APIRouter(prefix="/business-rules", tags=["Business Rules"])


def _uname(user: User) -> str:
    return getattr(user, "username", None) or getattr(user, "email", None) or "system"


class RuleUpdate(BaseModel):
    value: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("", response_model=APIResponse)
def list_rules(current_user: User = Depends(RequireRoles(["SUPER_ADMIN"]))):
    rules = svc.list_rules()
    modules = sorted({r["module"] for r in rules})
    return APIResponse(success=True, message=f"{len(rules)} rule(s)",
                       data={"items": rules, "modules": modules})


@router.put("/{rule_key}", response_model=APIResponse)
def update_rule(rule_key: str, body: RuleUpdate,
                current_user: User = Depends(RequireRoles(["SUPER_ADMIN"]))):
    if body.value is None and body.is_active is None:
        raise HTTPException(400, detail="nothing to update")
    try:
        res = svc.update_rule(rule_key, value=body.value,
                              is_active=body.is_active,
                              user=_uname(current_user))
        return APIResponse(success=True, message="Rule updated", data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.get("/{rule_key}/history", response_model=APIResponse)
def rule_history(rule_key: str, limit: int = 50,
                 current_user: User = Depends(RequireRoles(["SUPER_ADMIN"]))):
    return APIResponse(success=True, message="ok",
                       data={"items": svc.rule_history(rule_key, limit=limit)})
