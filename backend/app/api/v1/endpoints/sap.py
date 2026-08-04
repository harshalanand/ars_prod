"""
SAP Integration API  (prefix /sap)

A self-contained, read-only SAP data module. It pulls data FROM SAP into local
SQL Server staging tables (SAP_ prefixed) on a schedule or on demand, via the V2
"universal MCP" gateway worker (no SAP SDK on this server). Writing back to SAP
is a future phase.

  • /sap/connection      — gateway URL + API key (encrypted) + Test Connection
  • /sap/pulls           — CRUD for pull definitions + Run Now
  • /sap/runs            — run history
  • /sap/preview         — ad-hoc read (Explorer) — never lands data
  • /sap/odata-services  — OData catalog discovery
"""
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from loguru import logger

from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.models.rbac import User
from app.services import sap_config_service as conn_svc
from app.services import sap_pull_service as pull_svc
from app.services import sap_client
from app.services import sap_snowflake_client as sf_client
from app.services.sap_client import SapError
from app.services.sap_scheduler_service import sap_scheduler

router = APIRouter(prefix="/sap", tags=["SAP Integration"])


def _uname(user: User) -> str:
    return getattr(user, "username", None) or getattr(user, "email", None) or "system"


def _roles(user: User) -> set:
    return set(getattr(user, "role_codes", []) or [])


def _require_admin(user: User) -> None:
    if not ({"SUPER_ADMIN", "ADMIN"} & _roles(user)):
        raise HTTPException(403, detail="SAP administration is restricted to admins")


def _require_superadmin(user: User) -> None:
    if "SUPER_ADMIN" not in _roles(user):
        raise HTTPException(403, detail="Restricted to superadmin")


# ── Schemas ──────────────────────────────────────────────────────────────────
class ConnectionReq(BaseModel):
    worker_url: Optional[str] = None
    api_key: Optional[str] = None
    default_env: Optional[str] = "prod"
    display_mode: Optional[str] = "name"   # name | label | both (global field display)
    enabled: Optional[bool] = False
    snowflake: Optional[Any] = None        # Snowflake door config (account/user/auth/…)


class PullReq(BaseModel):
    name: str
    description: Optional[str] = None
    door: str = "rfc_table"                 # rfc_table | odata | snowflake
    sap_table: Optional[str] = None
    fields: Optional[Any] = None            # list[str] or comma string
    where_clause: Optional[str] = None      # raw fallback (advanced)
    where_json: Optional[Any] = None        # structured conditions (builder)
    odata_service: Optional[str] = None
    odata_entity: Optional[str] = None
    odata_filter: Optional[str] = None
    odata_select: Optional[str] = None
    sf_query: Optional[str] = None          # Snowflake door: SELECT to run
    sf_database: Optional[str] = None
    sf_schema: Optional[str] = None
    env: Optional[str] = None
    row_limit: Optional[int] = 50000
    target_table: str
    write_mode: Optional[str] = "replace"   # replace | append
    trigger_type: Optional[str] = "manual"  # manual | schedule
    schedule_config: Optional[Any] = None
    enabled: Optional[bool] = True


class PreviewReq(BaseModel):
    door: str = "rfc_table"
    sap_table: Optional[str] = None
    fields: Optional[Any] = None
    where_clause: Optional[str] = None
    where_json: Optional[Any] = None
    odata_service: Optional[str] = None
    odata_entity: Optional[str] = None
    odata_filter: Optional[str] = None
    odata_select: Optional[str] = None
    sf_query: Optional[str] = None
    sf_database: Optional[str] = None
    sf_schema: Optional[str] = None
    env: Optional[str] = None
    limit: Optional[int] = 50


# ── Connection ───────────────────────────────────────────────────────────────
@router.get("/connection", response_model=APIResponse)
def get_connection(current_user: User = Depends(get_current_user)):
    return APIResponse(success=True, message="ok", data=conn_svc.get_config())


@router.put("/connection", response_model=APIResponse)
def save_connection(body: ConnectionReq, current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    cfg = conn_svc.save_config(body.model_dump(), user=_uname(current_user))
    return APIResponse(success=True, message="Connection saved", data=cfg)


@router.post("/connection/test", response_model=APIResponse)
def test_connection(current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    res = sap_client.test_connection()
    conn_svc.persist_status("Connected" if res.get("success") else "Error",
                            res.get("message"))
    return APIResponse(success=res.get("success", False),
                       message=res.get("message", ""), data=res)


# ── Pull definitions ─────────────────────────────────────────────────────────
@router.get("/pulls", response_model=APIResponse)
def list_pulls(current_user: User = Depends(get_current_user)):
    items = pull_svc.list_pulls()
    return APIResponse(success=True, message=f"{len(items)} pull(s)",
                       data={"items": items, "total": len(items)})


@router.get("/pulls/{pull_id}", response_model=APIResponse)
def get_pull(pull_id: int, current_user: User = Depends(get_current_user)):
    p = pull_svc.get_pull(pull_id)
    if not p:
        raise HTTPException(404, detail=f"Pull {pull_id} not found")
    return APIResponse(success=True, message="ok", data=p)


@router.post("/pulls", response_model=APIResponse)
def create_pull(body: PullReq, current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    try:
        p = pull_svc.create_pull(body.model_dump(), user=_uname(current_user))
        return APIResponse(success=True, message=f"Created '{p['NAME']}'", data=p)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.exception("sap create_pull failed")
        raise HTTPException(500, detail=str(e))


@router.put("/pulls/{pull_id}", response_model=APIResponse)
def update_pull(pull_id: int, body: PullReq, current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    try:
        p = pull_svc.update_pull(pull_id, body.model_dump(), user=_uname(current_user))
        return APIResponse(success=True, message=f"Updated '{p['NAME']}'", data=p)
    except KeyError:
        raise HTTPException(404, detail=f"Pull {pull_id} not found")
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.exception("sap update_pull failed")
        raise HTTPException(500, detail=str(e))


@router.post("/pulls/{pull_id}/enable", response_model=APIResponse)
def enable_pull(pull_id: int, enabled: bool = True,
                current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    try:
        p = pull_svc.set_enabled(pull_id, enabled)
        return APIResponse(success=True, message=f"{'Enabled' if enabled else 'Disabled'}",
                           data=p)
    except KeyError:
        raise HTTPException(404, detail=f"Pull {pull_id} not found")


@router.delete("/pulls/{pull_id}", response_model=APIResponse)
def delete_pull(pull_id: int, current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    pull_svc.delete_pull(pull_id)
    return APIResponse(success=True, message=f"Removed pull {pull_id}", data=None)


@router.post("/pulls/{pull_id}/run", response_model=APIResponse)
def run_pull(pull_id: int, current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    if not pull_svc.get_pull(pull_id):
        raise HTTPException(404, detail=f"Pull {pull_id} not found")
    res = pull_svc.run_now(pull_id, user=_uname(current_user))
    return APIResponse(success=res.get("success", False),
                       message=res.get("message") or res.get("error") or "",
                       data=res)


# ── Runs / status ────────────────────────────────────────────────────────────
@router.get("/runs", response_model=APIResponse)
def list_runs(pull_id: Optional[int] = None, limit: int = 50,
              current_user: User = Depends(get_current_user)):
    items = pull_svc.list_runs(pull_id, limit)
    return APIResponse(success=True, message=f"{len(items)} run(s)",
                       data={"items": items, "total": len(items)})


@router.get("/scheduler/status", response_model=APIResponse)
def scheduler_status(current_user: User = Depends(get_current_user)):
    return APIResponse(success=True, message="ok", data=sap_scheduler.status)


# ── Explorer (ad-hoc read — never lands data) ────────────────────────────────
@router.post("/preview", response_model=APIResponse)
def preview(body: PreviewReq, current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    limit = max(1, min(int(body.limit or 50), 500))
    fields = body.fields
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.split(",") if f.strip()]
    # Structured conditions win over the raw clause when supplied.
    where = body.where_clause or None
    if isinstance(body.where_json, list):
        clean = [c for c in body.where_json
                 if isinstance(c, dict) and str(c.get("field") or "").strip()]
        if clean:
            where = pull_svc.build_where_clause(clean) or where
    door = (body.door or "rfc_table").lower()
    try:
        if door == "odata":
            if not (body.odata_service and body.odata_entity):
                raise HTTPException(400, detail="OData service and entity are required")
            res = sap_client.odata_pull(
                service=body.odata_service, entity=body.odata_entity,
                top=limit, skip=0, filter=body.odata_filter or None,
                select=body.odata_select or None, env=body.env or None)
        elif door == "snowflake":
            if not (body.sf_query or "").strip():
                raise HTTPException(400, detail="A SELECT query is required")
            res = sf_client.run_query(
                body.sf_query, row_limit=limit,
                database=body.sf_database or None, schema=body.sf_schema or None)
        else:
            if not body.sap_table:
                raise HTTPException(400, detail="SAP table is required")
            res = sap_client.read_table(
                table=body.sap_table, fields=fields or None,
                where=where or None, limit=limit, offset=0,
                env=body.env or None)
        return APIResponse(success=True,
                           message=f"{len(res['rows'])} row(s)",
                           data={"columns": res["columns"], "rows": res["rows"]})
    except SapError as e:
        raise HTTPException(400, detail=str(e))


# ── Snowflake door ───────────────────────────────────────────────────────────
@router.post("/snowflake/test", response_model=APIResponse)
def snowflake_test(current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    res = sf_client.test_connection()
    return APIResponse(success=res.get("success", False),
                       message=res.get("message", ""), data=res)


@router.get("/snowflake/tables", response_model=APIResponse)
def snowflake_tables(database: Optional[str] = None, schema: Optional[str] = None,
                     like: Optional[str] = None,
                     current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    try:
        res = sf_client.list_tables(database=database, schema=schema, like=like)
        return APIResponse(success=True, message=f"{len(res['rows'])} table(s)",
                           data={"columns": res["columns"], "rows": res["rows"]})
    except SapError as e:
        raise HTTPException(400, detail=str(e))


@router.get("/odata-services", response_model=APIResponse)
def odata_services(env: Optional[str] = None,
                   current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    try:
        return APIResponse(success=True, message="ok",
                           data=sap_client.odata_services(env or None))
    except SapError as e:
        raise HTTPException(400, detail=str(e))
