"""
Release Notes / Changelog API  (prefix /release-notes)

Records changes and auto-compiles them into daily release notes.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from loguru import logger

from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.models.rbac import User
from app.services import release_notes_service as svc

router = APIRouter(prefix="/release-notes", tags=["Release Notes"])


def _uname(user: User) -> str:
    return getattr(user, "username", None) or getattr(user, "email", None) or "system"


class EntryReq(BaseModel):
    note_date: Optional[str] = None       # defaults to today
    area: str = "General"
    title: str
    detail: Optional[str] = None
    example: Optional[str] = None
    emoji: Optional[str] = None
    marker: Optional[str] = "✅"
    tag: Optional[str] = None
    sort_order: Optional[int] = None


@router.get("/entries", response_model=APIResponse)
def list_entries(date_from: Optional[str] = None, date_to: Optional[str] = None,
                 area: Optional[str] = None, limit: int = 2000,
                 current_user: User = Depends(get_current_user)):
    items = svc.list_entries(date_from=date_from, date_to=date_to, area=area, limit=limit)
    return APIResponse(success=True, message=f"{len(items)} entry(ies)",
                       data={"items": items, "days": svc.list_days()})


@router.get("/days", response_model=APIResponse)
def list_days(current_user: User = Depends(get_current_user)):
    return APIResponse(success=True, message="ok", data={"items": svc.list_days()})


@router.post("/entries", response_model=APIResponse)
def add_entry(body: EntryReq, current_user: User = Depends(get_current_user)):
    try:
        res = svc.add_entry(note_date=body.note_date, area=body.area, title=body.title,
                            detail=body.detail, example=body.example, emoji=body.emoji,
                            marker=body.marker or "✅", tag=body.tag, sort_order=body.sort_order,
                            user=_uname(current_user))
        return APIResponse(success=True, message="Change recorded", data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.exception("release-notes add failed")
        raise HTTPException(500, detail=str(e))


@router.put("/entries/{entry_id}", response_model=APIResponse)
def update_entry(entry_id: int, body: EntryReq, current_user: User = Depends(get_current_user)):
    svc.update_entry(entry_id, note_date=body.note_date, area=body.area, title=body.title,
                     detail=body.detail, example=body.example, emoji=body.emoji,
                     marker=body.marker, tag=body.tag, sort_order=body.sort_order)
    return APIResponse(success=True, message="Updated", data={"id": entry_id})


@router.delete("/entries/{entry_id}", response_model=APIResponse)
def delete_entry(entry_id: int, current_user: User = Depends(get_current_user)):
    svc.delete_entry(entry_id)
    return APIResponse(success=True, message=f"Removed entry {entry_id}", data=None)
