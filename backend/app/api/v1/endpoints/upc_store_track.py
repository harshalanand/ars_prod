"""
UPC Store Tracking API  (prefix /upc-store-track)

Store-opening lifecycle tracker — the dynamic replacement for the manual
"STORE OPENING DATES *.xlsx" workbook. Upload only ST_CD + proposed opening
date + share date; identity is joined from Master_ALC_INPUT_ST_MASTER and
metrics (MBQ / stock / SLOC-wise / fill-rate) from the latest TREND_ST row.
Date & remark history is event-based. See services/upc_store_track_service.py.
"""
from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from loguru import logger

from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.models.rbac import User
from app.services import upc_store_track_service as svc

router = APIRouter(prefix="/upc-store-track", tags=["UPC Store Tracking"])


def _uname(user: User) -> str:
    return getattr(user, "username", None) or getattr(user, "email", None) or "system"


# ── Schemas ───────────────────────────────────────────────────────────────────
class SaveReq(BaseModel):
    st_cd: str
    proposed_opening_dt: Optional[str] = None
    share_dt: Optional[str] = None
    remarks: Optional[str] = None
    layout_generated: Optional[bool] = None
    layout_rec_dt: Optional[str] = None
    display_generated: Optional[bool] = None
    first_disp_dt: Optional[str] = None
    actual_open_dt: Optional[str] = None
    status: Optional[str] = None
    status_priority: Optional[str] = None
    note: Optional[str] = None


# ── Reads ───────────────────────────────────────────────────────────────────
def _segs(segments: Optional[str]):
    """Parse ?segments=APP,GM → tuple; None/blank → service default (APP+GM)."""
    if not segments:
        return svc.DEFAULT_SEG
    return tuple(s.strip().upper() for s in segments.split(",") if s.strip())


@router.get("", response_model=APIResponse)
@router.get("/", response_model=APIResponse)
def list_stores(segments: Optional[str] = None, current_user: User = Depends(get_current_user)):
    items = svc.list_stores(_segs(segments))
    return APIResponse(success=True, message=f"{len(items)} store(s)",
                       data={"items": items, "total": len(items),
                             "segments": list(svc._clean_segments(_segs(segments))),
                             "seg_options": list(svc.VALID_SEG)})


@router.get("/charts", response_model=APIResponse)
def charts(segments: Optional[str] = None, current_user: User = Depends(get_current_user)):
    return APIResponse(success=True, message="ok", data=svc.charts(_segs(segments)))


@router.get("/export")
def export(segments: Optional[str] = None, current_user: User = Depends(get_current_user)):
    data = svc.export_bytes(_segs(segments))
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="upc_store_tracking.xlsx"'})


@router.get("/template")
def template(current_user: User = Depends(get_current_user)):
    data = svc.template_bytes()
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="upc_store_tracking_template.xlsx"'})


@router.get("/{st_cd}", response_model=APIResponse)
def get_store(st_cd: str, current_user: User = Depends(get_current_user)):
    try:
        return APIResponse(success=True, message="ok", data=svc.get_store(st_cd))
    except KeyError:
        raise HTTPException(404, detail=f"Store {st_cd} is not tracked")


# ── Writes ──────────────────────────────────────────────────────────────────
@router.post("", response_model=APIResponse)
@router.post("/", response_model=APIResponse)
def save_store(body: SaveReq, current_user: User = Depends(get_current_user)):
    try:
        res = svc.save_store(
            body.st_cd,
            proposed_opening_dt=body.proposed_opening_dt,
            share_dt=body.share_dt,
            remarks=body.remarks,
            source="manual",
            user=_uname(current_user),
            note=body.note,
            layout_generated=body.layout_generated,
            layout_rec_dt=body.layout_rec_dt,
            display_generated=body.display_generated,
            first_disp_dt=body.first_disp_dt,
            actual_open_dt=body.actual_open_dt,
            status=body.status,
            status_priority=body.status_priority,
        )
        return APIResponse(success=True, message=f"Saved {res['st_cd']}", data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.exception("upc save failed")
        raise HTTPException(500, detail=str(e))


@router.put("/{st_cd}", response_model=APIResponse)
def update_store(st_cd: str, body: SaveReq, current_user: User = Depends(get_current_user)):
    try:
        res = svc.save_store(
            st_cd,
            proposed_opening_dt=body.proposed_opening_dt,
            share_dt=body.share_dt,
            remarks=body.remarks,
            source="edit",
            user=_uname(current_user),
            note=body.note,
            layout_generated=body.layout_generated,
            layout_rec_dt=body.layout_rec_dt,
            display_generated=body.display_generated,
            first_disp_dt=body.first_disp_dt,
            actual_open_dt=body.actual_open_dt,
            status=body.status,
            status_priority=body.status_priority,
        )
        return APIResponse(success=True, message=f"Updated {res['st_cd']}", data=res)
    except Exception as e:
        logger.exception("upc update failed")
        raise HTTPException(500, detail=str(e))


@router.post("/upload", response_model=APIResponse)
async def upload(file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    if not (file.filename or "").lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, detail="Please upload an .xlsx/.xls file")
    try:
        content = await file.read()
        res = svc.ingest_upload(content, user=_uname(current_user))
        msg = (f"{res['processed']} store(s): {res['created']} new, "
               f"{res['changed']} changed, {res['skipped']} skipped"
               + (f", {res['error_count']} error(s)" if res.get("error_count") else ""))
        return APIResponse(success=(res.get("error_count", 0) == 0), message=msg, data=res)
    except Exception as e:
        logger.exception("upc upload failed")
        raise HTTPException(400, detail=str(e))


@router.post("/compact-history", response_model=APIResponse)
def compact_history(current_user: User = Depends(get_current_user)):
    res = svc.compact_history()
    return APIResponse(success=True,
                       message=(f"Cleaned {res['removed_rows']} redundant date row(s) + "
                                f"{res.get('removed_remarks',0)} blank remark(s) across {res['stores']} store(s)"),
                       data=res)


@router.delete("/{st_cd}", response_model=APIResponse)
def delete_store(st_cd: str, current_user: User = Depends(get_current_user)):
    svc.delete_store(st_cd)
    return APIResponse(success=True, message=f"Removed {st_cd.upper()}", data=None)
