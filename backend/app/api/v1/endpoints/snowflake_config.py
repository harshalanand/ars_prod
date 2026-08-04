"""
Snowflake Configuration API  (prefix /settings/snowflake)

The single app-wide Snowflake connection (Settings → Snowflake). Used by both
the SAP module's Snowflake door and the Report Generation engine/scheduler.
"""
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.models.rbac import User
from app.services import snowflake_config_service as svc
from app.services.snowflake_config_service import SnowflakeError

router = APIRouter(prefix="/settings/snowflake", tags=["Snowflake"])


def _uname(user: User) -> str:
    return getattr(user, "username", None) or getattr(user, "email", None) or "system"


def _roles(user: User) -> set:
    return set(getattr(user, "role_codes", []) or [])


def _require_admin(user: User) -> None:
    if not ({"SUPER_ADMIN", "ADMIN"} & _roles(user)):
        raise HTTPException(403, detail="Snowflake administration is restricted to admins")


def _require_superadmin(user: User) -> None:
    if "SUPER_ADMIN" not in _roles(user):
        raise HTTPException(403, detail="Restricted to superadmin")


class SnowflakeConfigReq(BaseModel):
    account: Optional[str] = None
    user: Optional[str] = None
    auth: Optional[str] = "keypair"          # keypair | password
    private_key_path: Optional[str] = None
    private_key_pwd: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    warehouse: Optional[str] = None
    database: Optional[str] = None
    schema: Optional[str] = None
    enabled: Optional[bool] = False


@router.get("/config", response_model=APIResponse)
def get_config(current_user: User = Depends(get_current_user)):
    return APIResponse(success=True, message="ok", data=svc.get_config())


@router.put("/config", response_model=APIResponse)
def save_config(body: SnowflakeConfigReq, current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    cfg = svc.save_config(body.model_dump(), user=_uname(current_user))
    return APIResponse(success=True, message="Snowflake settings saved", data=cfg)


@router.post("/test", response_model=APIResponse)
def test(current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    res = svc.test_connection()
    return APIResponse(success=res.get("success", False),
                       message=res.get("message", ""), data=res)


@router.get("/tables", response_model=APIResponse)
def tables(database: Optional[str] = None, schema: Optional[str] = None,
           like: Optional[str] = None, current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    try:
        res = svc.list_tables(database=database, schema=schema, like=like)
        return APIResponse(success=True, message=f"{len(res['rows'])} table(s)",
                           data={"columns": res["columns"], "rows": res["rows"]})
    except SnowflakeError as e:
        raise HTTPException(400, detail=str(e))
