"""
Dev Sync Manager API  (prefix /dev-sync)

One-way table refresh PROD (HOPC866) → DEV (this app's data DB).
Superadmin-only — it is a developer/ops tool. See services/dev_sync_service.py
and docs/DEV_SYNC_PLAN.md.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from loguru import logger

from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.models.rbac import User
from app.services import dev_sync_service as svc

router = APIRouter(prefix="/dev-sync", tags=["Dev Sync"])


def _require_superadmin(user: User):
    # Matches the codebase convention (see activity_log.py): role_codes carries
    # the assigned role codes; SUPER_ADMIN gates developer/ops tools.
    if "SUPER_ADMIN" not in set(getattr(user, "role_codes", []) or []):
        raise HTTPException(403, detail="Dev Sync is restricted to superadmin")


# ── Schemas ───────────────────────────────────────────────────────────────────
class SettingsUpdate(BaseModel):
    source_server:    Optional[str] = None
    source_user:      Optional[str] = None
    source_pwd:       Optional[str] = None
    target_server:    Optional[str] = None
    target_user:      Optional[str] = None
    target_pwd:       Optional[str] = None
    target_system_db: Optional[str] = None
    target_data_db:   Optional[str] = None
    link_server_name: Optional[str] = None
    incr_enabled:     Optional[bool] = None
    incr_hour:        Optional[int]  = None
    full_enabled:     Optional[bool] = None
    full_weekday:     Optional[int]  = None
    full_hour:        Optional[int]  = None

class TestConnReq(BaseModel):
    which:  str = "source"          # source | target
    server: str
    user:   str
    pwd:    Optional[str] = None

class AddTableItem(BaseModel):
    db_name:      str
    table_name:   str
    is_active:    Optional[bool] = False
    sync_mode:    Optional[str]  = None
    incr_key_col: Optional[str]  = None

class AddTablesReq(BaseModel):
    items: List[AddTableItem]

class TableUpdate(BaseModel):
    is_active:      Optional[bool] = None
    sync_mode:      Optional[str]  = None
    incr_key_col:   Optional[str]  = None
    exclude_reason: Optional[str]  = None

class BulkToggleReq(BaseModel):
    table_ids: List[int]
    active:    bool

class BulkIdsReq(BaseModel):
    table_ids: List[int]

class RunReq(BaseModel):
    mode:      str = "incremental"           # incremental | full
    table_ids: Optional[List[int]] = None
    force:     bool = False                  # reload even if row counts already match


# ── Settings ────────────────────────────────────────────────────────────────
@router.get("/settings", response_model=APIResponse)
def get_settings(current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    return APIResponse(success=True, message="ok", data=svc.get_settings_row())


@router.put("/settings", response_model=APIResponse)
def put_settings(body: SettingsUpdate, current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    try:
        return APIResponse(success=True, message="Settings saved",
                           data=svc.save_settings(body.model_dump(exclude_none=True)))
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/test-connection", response_model=APIResponse)
def test_connection(body: TestConnReq, current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    try:
        info = svc.test_connection(body.which, body.server, body.user, body.pwd)
        return APIResponse(success=True,
                           message=f"{body.which}: connected to {info['server_name']}", data=info)
    except Exception as e:
        raise HTTPException(400, detail=f"Connection failed: {e}")


@router.post("/setup-linkserver", response_model=APIResponse)
def setup_linkserver(current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    try:
        info = svc.setup_linkserver()
        return APIResponse(success=True,
                           message=f"Linked server ready → {info['remote_server']}", data=info)
    except Exception as e:
        raise HTTPException(400, detail=f"Linked-server setup failed: {e}")


# ── Discovery + table config ──────────────────────────────────────────────────
@router.get("/discover", response_model=APIResponse)
def discover(current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    try:
        data = svc.discover()
        return APIResponse(success=True,
                           message=f"{len(data.get('new', []))} new table(s) in prod", data=data)
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.get("/tables", response_model=APIResponse)
def list_tables(current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    items = svc.list_tables()
    return APIResponse(success=True, message=f"{len(items)} configured table(s)",
                       data={"items": items, "total": len(items)})


@router.post("/tables", response_model=APIResponse)
def add_tables(body: AddTablesReq, current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    n = svc.add_tables([i.model_dump() for i in body.items])
    return APIResponse(success=True, message=f"Added {n} table(s)", data={"added": n})


@router.put("/tables/{tid}", response_model=APIResponse)
def update_table(tid: int, body: TableUpdate, current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    svc.update_table(tid, body.model_dump(exclude_none=True))
    return APIResponse(success=True, message="Updated", data=None)


@router.delete("/tables/{tid}", response_model=APIResponse)
def delete_table(tid: int, current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    svc.delete_table(tid)
    return APIResponse(success=True, message="Removed", data=None)


@router.post("/tables/bulk-toggle", response_model=APIResponse)
def bulk_toggle(body: BulkToggleReq, current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    n = svc.bulk_toggle(body.table_ids, body.active)
    return APIResponse(success=True, message=f"{n} table(s) updated", data={"updated": n})


@router.post("/tables/bulk-delete", response_model=APIResponse)
def bulk_delete(body: BulkIdsReq, current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    n = svc.bulk_delete(body.table_ids)
    return APIResponse(success=True, message=f"Removed {n} table(s) from the list", data={"deleted": n})


@router.post("/tables/clear", response_model=APIResponse)
def clear_all(current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    n = svc.clear_all()
    return APIResponse(success=True, message=f"Cleared {n} table(s) from the list", data={"deleted": n})


# ── Run ────────────────────────────────────────────────────────────────────
@router.post("/run", response_model=APIResponse)
def run(body: RunReq, current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    if body.mode not in ("incremental", "full"):
        raise HTTPException(400, detail="mode must be 'incremental' or 'full'")
    try:
        res = svc.run_sync(mode=body.mode, table_ids=body.table_ids, force=body.force)
        msg = (f"{body.mode} sync: {res['ok']} updated, "
               f"{res.get('skipped',0)} skipped (identical), {res['errors']} error(s)")
        return APIResponse(success=(res["errors"] == 0), message=msg, data=res)
    except Exception as e:
        raise HTTPException(400, detail=str(e))
