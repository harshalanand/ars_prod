"""
Get Data API  (prefix /get-data)

Brings data INTO local SQL (Rep_data) from outside sources. Phase 1 = Snowflake:
  • /get-data/overview             — module dashboard
  • /get-data/snowflake/*          — status, browse, validate, preview, views
  • /get-data/jobs                 — sync-job CRUD, test, Run now
  • /get-data/runs                 — run history (AUTO / MANUAL, row counts)

Manage (create views, edit jobs) = SUPER_ADMIN / ADMIN or GET_DATA_MANAGE.
Run now / preview = the above or GET_DATA_RUN. Spec: docs/manual/get_data.md.
"""
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from app.models.rbac import User
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.services import get_data_snowflake as gsf
from app.services import get_data_sync_service as svc
from app.services.get_data_scheduler import get_data_scheduler
from app.services.get_data_snowflake import GetDataError
from app.services.snowflake_config_service import SnowflakeError

router = APIRouter(prefix="/get-data", tags=["Get Data"])


def _uname(user: User) -> str:
    return getattr(user, "username", None) or getattr(user, "email", None) or "system"


def _can(user: User, perm: str) -> bool:
    roles = set(getattr(user, "role_codes", []) or [])
    if {"SUPER_ADMIN", "ADMIN"} & roles:
        return True
    try:
        return perm in (user.permissions or set())
    except Exception:
        return False


def _require_manage(user: User) -> None:
    if not _can(user, "GET_DATA_MANAGE"):
        raise HTTPException(403, detail="Managing Get Data needs the GET_DATA_MANAGE permission (or an admin role).")


def _require_run(user: User) -> None:
    if not (_can(user, "GET_DATA_MANAGE") or _can(user, "GET_DATA_RUN")):
        raise HTTPException(403, detail="Running Get Data jobs needs the GET_DATA_RUN permission.")


def _fail(e: Exception, what: str):
    if isinstance(e, HTTPException):
        raise e
    if isinstance(e, svc.AlreadyRunning):
        raise HTTPException(409, detail=str(e))
    if isinstance(e, (GetDataError, SnowflakeError, ValueError)):
        raise HTTPException(400, detail=str(e))
    if isinstance(e, KeyError):
        raise HTTPException(404, detail=f"{what}: not found")
    logger.exception(f"[get-data] {what} failed")
    raise HTTPException(500, detail=f"{what} failed: {e}")


# ── Schemas ──────────────────────────────────────────────────────────────────
class SqlReq(BaseModel):
    sql: str
    with_count: Optional[bool] = True


class PreviewReq(BaseModel):
    sql: Optional[str] = None
    object: Optional[str] = None
    limit: Optional[int] = 100


class ViewReq(BaseModel):
    name: str
    sql: str
    description: Optional[str] = None


class JobReq(BaseModel):
    job_name: str
    description: Optional[str] = None
    source_object: str
    target_table: str
    load_mode: Optional[str] = "replace"
    key_cols: Optional[Any] = None
    watermark_col: Optional[str] = None
    trigger_type: Optional[str] = "manual"
    schedule_config: Optional[Any] = None
    retry_on_fail: Optional[bool] = True
    retry_delay_min: Optional[int] = 15
    enabled: Optional[bool] = True
    loader: Optional[str] = "bulk"          # bulk (default) | classic
    allow_empty: Optional[bool] = False     # let a 0-row result empty a filled table


class JobTestReq(BaseModel):
    source_object: str
    load_mode: Optional[str] = "replace"
    key_cols: Optional[Any] = None
    watermark_col: Optional[str] = None


# ── Overview / status ────────────────────────────────────────────────────────
@router.get("/overview", response_model=APIResponse)
def overview(current_user: User = Depends(get_current_user)):
    try:
        data = svc.overview()
        data["scheduler"] = get_data_scheduler.status
        return APIResponse(success=True, message="ok", data=data)
    except Exception as e:
        _fail(e, "overview")


@router.get("/scheduler/status", response_model=APIResponse)
def scheduler_status(current_user: User = Depends(get_current_user)):
    return APIResponse(success=True, message="ok", data=get_data_scheduler.status)


# ── Snowflake: browse ────────────────────────────────────────────────────────
@router.get("/snowflake/status", response_model=APIResponse)
def sf_status(current_user: User = Depends(get_current_user)):
    return APIResponse(success=True, message="ok", data=gsf.status())


@router.get("/snowflake/schemas", response_model=APIResponse)
def sf_schemas(current_user: User = Depends(get_current_user)):
    try:
        items = gsf.list_schemas()
        return APIResponse(success=True, message=f"{len(items)} schema(s)", data={"items": items})
    except Exception as e:
        _fail(e, "list schemas")


@router.get("/snowflake/objects", response_model=APIResponse)
def sf_objects(schema: str, like: Optional[str] = None, limit: int = 1000,
               current_user: User = Depends(get_current_user)):
    try:
        items = gsf.list_objects(schema, like=like, limit=limit)
        return APIResponse(success=True, message=f"{len(items)} object(s)", data={"items": items})
    except Exception as e:
        _fail(e, "list objects")


@router.get("/snowflake/columns", response_model=APIResponse)
def sf_columns(object: str, current_user: User = Depends(get_current_user)):
    try:
        return APIResponse(success=True, message="ok", data=gsf.describe_object(object))
    except Exception as e:
        _fail(e, "describe")


# ── Snowflake: validate / preview / views ────────────────────────────────────
@router.post("/snowflake/validate", response_model=APIResponse)
def sf_validate(body: SqlReq, current_user: User = Depends(get_current_user)):
    _require_run(current_user)
    try:
        res = gsf.validate_sql(body.sql, with_count=bool(body.with_count))
        return APIResponse(success=True, message="Valid" if res["ok"] else "Validation failed", data=res)
    except Exception as e:
        _fail(e, "validate")


@router.post("/snowflake/preview", response_model=APIResponse)
def sf_preview(body: PreviewReq, current_user: User = Depends(get_current_user)):
    _require_run(current_user)
    try:
        res = gsf.preview(sql=body.sql, obj=body.object, limit=body.limit or 100)
        return APIResponse(success=True, message=f"{len(res['rows'])} row(s)", data=res)
    except Exception as e:
        _fail(e, "preview")


@router.get("/snowflake/views", response_model=APIResponse)
def sf_views(current_user: User = Depends(get_current_user)):
    try:
        return APIResponse(success=True, message="ok", data=gsf.list_views())
    except Exception as e:
        _fail(e, "list views")


@router.get("/snowflake/views/{name}", response_model=APIResponse)
def sf_view(name: str, current_user: User = Depends(get_current_user)):
    try:
        return APIResponse(success=True, message="ok", data=gsf.get_view(name))
    except Exception as e:
        _fail(e, "get view")


@router.post("/snowflake/views", response_model=APIResponse)
def sf_create_view(body: ViewReq, current_user: User = Depends(get_current_user)):
    _require_manage(current_user)
    try:
        res = gsf.create_view(body.name, body.sql, body.description, _uname(current_user))
        msg = f"Saved {res['object']}" if res["created"] else "Validation failed — nothing was created"
        return APIResponse(success=res["created"], message=msg, data=res)
    except Exception as e:
        _fail(e, "create view")


@router.delete("/snowflake/views/{name}", response_model=APIResponse)
def sf_drop_view(name: str, current_user: User = Depends(get_current_user)):
    _require_manage(current_user)
    try:
        res = gsf.drop_view(name, _uname(current_user))
        return APIResponse(success=True, message=f"Dropped {res['name']}", data=res)
    except Exception as e:
        _fail(e, "drop view")


# ── Jobs ─────────────────────────────────────────────────────────────────────
@router.get("/jobs", response_model=APIResponse)
def list_jobs(current_user: User = Depends(get_current_user)):
    try:
        items = svc.list_jobs()
        return APIResponse(success=True, message=f"{len(items)} job(s)",
                           data={"items": items, "total": len(items)})
    except Exception as e:
        _fail(e, "list jobs")


@router.get("/jobs/{job_id}", response_model=APIResponse)
def get_job(job_id: int, current_user: User = Depends(get_current_user)):
    j = svc.get_job(job_id)
    if not j:
        raise HTTPException(404, detail=f"Job {job_id} not found")
    return APIResponse(success=True, message="ok", data=j)


@router.post("/jobs/test", response_model=APIResponse)
def test_job(body: JobTestReq, current_user: User = Depends(get_current_user)):
    _require_run(current_user)
    try:
        res = svc.test_job(body.model_dump())
        return APIResponse(success=True, message="ok" if res["ok"] else "Checks failed", data=res)
    except Exception as e:
        _fail(e, "test job")


@router.post("/jobs", response_model=APIResponse)
def create_job(body: JobReq, current_user: User = Depends(get_current_user)):
    _require_manage(current_user)
    try:
        j = svc.create_job(body.model_dump(), _uname(current_user))
        return APIResponse(success=True, message=f"Created '{j['JOB_NAME']}'", data=j)
    except Exception as e:
        _fail(e, "create job")


@router.put("/jobs/{job_id}", response_model=APIResponse)
def update_job(job_id: int, body: JobReq, current_user: User = Depends(get_current_user)):
    _require_manage(current_user)
    try:
        j = svc.update_job(job_id, body.model_dump(), _uname(current_user))
        return APIResponse(success=True, message=f"Updated '{j['JOB_NAME']}'", data=j)
    except Exception as e:
        _fail(e, f"job {job_id}")


@router.post("/jobs/{job_id}/enable", response_model=APIResponse)
def enable_job(job_id: int, enabled: bool = True, current_user: User = Depends(get_current_user)):
    _require_manage(current_user)
    try:
        j = svc.set_enabled(job_id, enabled, _uname(current_user))
        return APIResponse(success=True, message="Enabled" if enabled else "Disabled", data=j)
    except Exception as e:
        _fail(e, f"job {job_id}")


@router.delete("/jobs/{job_id}", response_model=APIResponse)
def delete_job(job_id: int, drop_table: bool = False, current_user: User = Depends(get_current_user)):
    _require_manage(current_user)
    try:
        res = svc.delete_job(job_id, drop_table, _uname(current_user))
        msg = f"Deleted job{' and dropped ' + res['target_table'] if res['table_dropped'] else ' (local table kept)'}"
        return APIResponse(success=True, message=msg, data=res)
    except Exception as e:
        _fail(e, f"job {job_id}")


@router.post("/jobs/{job_id}/run", response_model=APIResponse)
def run_job(job_id: int, full_reload: bool = False, current_user: User = Depends(get_current_user)):
    """Start a MANUAL run and return at once; poll /runs/{run_id} for progress."""
    _require_run(current_user)
    try:
        run_id = svc.start_run(job_id, "MANUAL", _uname(current_user), full_reload=full_reload)
        get_data_scheduler.submit(run_id)
        return APIResponse(success=True, message=f"Started run #{run_id}", data={"run_id": run_id})
    except Exception as e:
        _fail(e, f"run job {job_id}")


# ── Runs ─────────────────────────────────────────────────────────────────────
@router.get("/runs", response_model=APIResponse)
def list_runs(date_from: Optional[str] = None, date_to: Optional[str] = None,
              job_id: Optional[int] = None, run_type: Optional[str] = None,
              status: Optional[str] = None, limit: int = 200, offset: int = 0,
              current_user: User = Depends(get_current_user)):
    try:
        data = svc.list_runs(date_from, date_to, job_id, run_type, status, limit, offset)
        return APIResponse(success=True, message=f"{data['total']} run(s)", data=data)
    except Exception as e:
        _fail(e, "list runs")


@router.get("/runs/{run_id}", response_model=APIResponse)
def get_run(run_id: int, current_user: User = Depends(get_current_user)):
    r = svc.get_run(run_id)
    if not r:
        raise HTTPException(404, detail=f"Run {run_id} not found")
    return APIResponse(success=True, message="ok", data=r)
