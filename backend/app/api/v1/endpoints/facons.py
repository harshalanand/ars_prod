"""
FA & CONS API  (prefix /fa-cons)

Data foundation for Project-Store (FA) and Consumables (CONS) allocation:
  • per-stream, per-scope SLOC selection      → facons_stock_service
  • dedicated FA/CONS MSA + store-stock calc   → facons_stock_service
  • MBQ Master (3-col upload + live enrichment) → facons_mbq_service

Allocation engine, Gap Report and pend_alc wiring are deferred (see
frontend/public/docs/manual/fa_cons.md).
"""
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from loguru import logger

from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.models.rbac import User
from app.services import facons_stock_service as stock_svc
from app.services import facons_mbq_service as mbq_svc
from app.services import facons_store_list_service as store_svc
from app.services import facons_alloc_service as alloc_svc
from app.services import facons_pend_service as pend_svc
from app.services import facons_gap_service as gap_svc

router = APIRouter(prefix="/fa-cons", tags=["FA & CONS"])

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _uname(user: User) -> str:
    return getattr(user, "username", None) or getattr(user, "email", None) or "system"


# ── Schemas ───────────────────────────────────────────────────────────────────
class SlocItem(BaseModel):
    source: str            # 'STORE' (ARS_STORE_SLOC_SETTINGS) | 'MSA' (ARS_MSA_SLOC_SETTINGS)
    sloc: str
    stream: str            # 'FA' | 'CONS'
    active: bool = False   # on/off for this stream in this table


class SlocBulk(BaseModel):
    items: List[SlocItem]


class MbqSaveReq(BaseModel):
    st_cd: str
    ref_art: str
    mbq_q: float
    clr: Optional[str] = ""
    priority: Optional[int] = None
    cover_days: Optional[int] = None
    reason: Optional[str] = None
    remarks: Optional[str] = None
    source_type: Optional[str] = None    # CENTRAL | LOCAL (ref-art property)
    # stream is auto-segregated from DIV (CO→CONS, FA→FA); not chosen by the user


class SourceTypeReq(BaseModel):
    ref_art: str
    value: str                           # CENTRAL | LOCAL
    stream: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# SLOC selection
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/sloc-settings", response_model=APIResponse)
def list_sloc(stream: str, scope: Optional[str] = None,
              current_user: User = Depends(get_current_user)):
    try:
        items = stock_svc.list_sloc_settings(stream, scope)
        return APIResponse(success=True, message=f"{len(items)} SLOC(s)",
                           data={"items": items})
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.put("/sloc-settings", response_model=APIResponse)
def bulk_update_sloc(body: SlocBulk, current_user: User = Depends(get_current_user)):
    try:
        n = stock_svc.bulk_update_sloc_settings(
            [i.model_dump() for i in body.items], user=_uname(current_user))
        return APIResponse(success=True, message=f"Saved {n} SLOC setting(s)", data={"saved": n})
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.exception("facons sloc bulk update failed")
        raise HTTPException(500, detail=str(e))


@router.get("/sloc-settings/stock-totals", response_model=APIResponse)
def sloc_stock_totals(stream: str, date: Optional[str] = None, force: bool = False,
                      current_user: User = Depends(get_current_user)):
    """Live per-SLOC stock qty (all SLOCs, active or not) for the stream's division —
    so you can see which SLOCs carry stock before activating them. Cached (15 min);
    force=true rebuilds and re-discovers new SLOCs from the same scan."""
    try:
        res = stock_svc.sloc_stock_totals(stream, date=date, force=force)
        return APIResponse(success=True, message="SLOC stock totals", data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.exception("facons sloc stock totals failed")
        raise HTTPException(500, detail=str(e))


@router.post("/sloc-settings/sync", response_model=APIResponse)
def sync_sloc(stream: str, current_user: User = Depends(get_current_user)):
    try:
        res = stock_svc.sync_slocs(stream, user=_uname(current_user))
        return APIResponse(success=True,
                           message=f"Synced — added {res['added']} new SLOC(s) to the listing tables",
                           data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.exception("facons sloc sync failed")
        raise HTTPException(500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Stock calc
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/stock/calculate", response_model=APIResponse)
def stock_calculate(stream: str, date: Optional[str] = None, divs: Optional[str] = None,
                    current_user: User = Depends(get_current_user)):
    """divs = comma-separated product DIVs to consider (default = stream's own div).
    stream='ALL' runs both FA and CONS."""
    div_list = [d.strip() for d in divs.split(",") if d.strip()] if divs else None
    user = _uname(current_user)
    try:
        if (stream or "").strip().upper() == "ALL":
            runs = [stock_svc.calculate(s, date=date, divs=div_list, user=user) for s in ("FA", "CONS")]
            store = sum(r["store_rows"] for r in runs)
            msa = sum(r["msa_rows"] for r in runs)
            warns = [w for r in runs for w in r["warnings"]]
            return APIResponse(success=True,
                               message=f"Stock calc done for FA + CONS: {store} store row(s), {msa} DC-pool row(s)",
                               data={"runs": runs, "store_rows": store, "msa_rows": msa, "warnings": warns})
        res = stock_svc.calculate(stream, date=date, divs=div_list, user=user)
        return APIResponse(success=True,
                           message=(f"Stock calc done (seq {res['sequence_id']}): "
                                    f"{res['store_rows']} store row(s), {res['msa_rows']} DC-pool row(s)"),
                           data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.exception("facons stock calc failed")
        raise HTTPException(500, detail=str(e))


@router.get("/stock/results", response_model=APIResponse)
def stock_results(stream: str, scope: str, sequence_id: Optional[int] = None,
                  limit: int = 500, grain: str = "ref",
                  current_user: User = Depends(get_current_user)):
    try:
        return APIResponse(success=True, message="ok",
                           data=stock_svc.get_results(stream, scope, sequence_id, limit, grain))
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.get("/stock/sequences", response_model=APIResponse)
def stock_sequences(stream: Optional[str] = None, limit: int = 20,
                    current_user: User = Depends(get_current_user)):
    return APIResponse(success=True, message="ok",
                       data={"items": stock_svc.get_sequences(stream, limit)})


# ══════════════════════════════════════════════════════════════════════════════
# MBQ Master
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/mbq", response_model=APIResponse)
def list_mbq(stream: Optional[str] = None, current_user: User = Depends(get_current_user)):
    """stream omitted → all rows (each carries its auto-segregated FA/CONS stream)."""
    try:
        items = mbq_svc.list_mbq(stream)
        return APIResponse(success=True, message=f"{len(items)} MBQ row(s)",
                           data={"items": items, "summary": mbq_svc.summary(stream)})
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.post("/mbq", response_model=APIResponse)
def save_mbq(body: MbqSaveReq, current_user: User = Depends(get_current_user)):
    try:
        stream = mbq_svc.resolve_stream(body.ref_art)   # auto-segregate by DIV
        res = mbq_svc.save_mbq(
            stream, body.st_cd, body.ref_art, body.mbq_q,
            clr=body.clr or "", priority=body.priority, cover_days=body.cover_days,
            reason=body.reason, remarks=body.remarks, source="edit", user=_uname(current_user),
            source_type=body.source_type)
        return APIResponse(success=True, message=f"Saved {res['st_cd']}/{res['ref_art']} ({stream})", data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.exception("facons mbq save failed")
        raise HTTPException(500, detail=str(e))


@router.post("/mbq/source-type", response_model=APIResponse)
def set_source_type(body: SourceTypeReq, current_user: User = Depends(get_current_user)):
    """Set CENTRAL/LOCAL for a reference article across all its stores (ref-art property)."""
    try:
        res = mbq_svc.set_source_type(body.ref_art, body.value, stream=body.stream, user=_uname(current_user))
        return APIResponse(success=True,
                           message=f"{res['ref_art']} → {res['source_type']} ({res['rows_updated']} store row(s))",
                           data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.delete("/mbq/{row_id}", response_model=APIResponse)
def delete_mbq(row_id: int, reason: Optional[str] = None,
               current_user: User = Depends(get_current_user)):
    """Delete an MBQ row. `reason` is mandatory (recorded on the DELETE event)."""
    try:
        mbq_svc.delete_mbq(row_id, user=_uname(current_user), reason=reason)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    return APIResponse(success=True, message=f"Removed row {row_id}", data=None)


@router.get("/mbq/change-review", response_model=APIResponse)
def mbq_change_review(date_from: Optional[str] = None, date_to: Optional[str] = None,
                      stream: Optional[str] = None, action: Optional[str] = None,
                      st_cd: Optional[str] = None, ref_art: Optional[str] = None,
                      changed_by: Optional[str] = None, session_id: Optional[int] = None,
                      limit: int = 2000, current_user: User = Depends(get_current_user)):
    """Audit of MBQ changes for a date / date-range OR a specific upload session
    (+ filters). Read-only."""
    res = mbq_svc.change_review(date_from=date_from, date_to=date_to, stream=stream, action=action,
                                st_cd=st_cd, ref_art=ref_art, changed_by=changed_by,
                                session_id=session_id, limit=limit)
    return APIResponse(success=True, message=f"{res['summary']['total']} change event(s)", data=res)


@router.get("/mbq/change-review/export")
def mbq_change_review_export(date_from: Optional[str] = None, date_to: Optional[str] = None,
                             stream: Optional[str] = None, action: Optional[str] = None,
                             st_cd: Optional[str] = None, ref_art: Optional[str] = None,
                             changed_by: Optional[str] = None, session_id: Optional[int] = None,
                             current_user: User = Depends(get_current_user)):
    data = mbq_svc.change_review_export(date_from=date_from, date_to=date_to, stream=stream,
                                        action=action, st_cd=st_cd, ref_art=ref_art,
                                        changed_by=changed_by, session_id=session_id)
    return StreamingResponse(iter([data]), media_type=_XLSX,
                             headers={"Content-Disposition": 'attachment; filename="mbq_change_review.xlsx"'})


@router.get("/mbq/sessions", response_model=APIResponse)
def mbq_sessions(date_from: Optional[str] = None, date_to: Optional[str] = None,
                 limit: int = 50, current_user: User = Depends(get_current_user)):
    """Recent MBQ upload batches (sessions) for session-wise change review."""
    items = mbq_svc.list_sessions(date_from=date_from, date_to=date_to, limit=limit)
    return APIResponse(success=True, message=f"{len(items)} session(s)", data={"items": items})


@router.get("/mbq/history", response_model=APIResponse)
def mbq_history(st_cd: Optional[str] = None, ref_art: Optional[str] = None,
                limit: int = 300, current_user: User = Depends(get_current_user)):
    items = mbq_svc.list_history(st_cd, ref_art, limit)
    return APIResponse(success=True, message=f"{len(items)} event(s)", data={"items": items})


@router.post("/mbq/upload", response_model=APIResponse)
async def upload_mbq(file: UploadFile = File(...), dry_run: bool = False,
                     reason: Optional[str] = None,
                     current_user: User = Depends(get_current_user)):
    """MBQ upload — auto-segregated FA/CONS by DIV. dry_run=true previews the
    create/update/unchanged breakdown WITHOUT writing (for a confirm-before-commit
    step); dry_run=false commits. `reason` is a batch-level change justification
    stamped on every changed row's event (shown in MBQ Change Review)."""
    if not (file.filename or "").lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, detail="Please upload an .xlsx/.xls file")
    try:
        content = await file.read()
        res = mbq_svc.ingest_upload(content, user=_uname(current_user), dry_run=dry_run,
                                    reason=reason, filename=file.filename)
        verb = "Preview" if dry_run else "Uploaded"
        msg = (f"{verb}: {res['created']} new, {res['updated']} changed, {res['unchanged']} unchanged, "
               f"{res['skipped']} skipped ({res.get('fa', 0)} FA / {res.get('cons', 0)} CONS)"
               + (f", {res['duplicates']} duplicate key(s)" if res.get("duplicates") else "")
               + (f", {res['error_count']} error(s)" if res.get("error_count") else ""))
        return APIResponse(success=(res.get("error_count", 0) == 0), message=msg, data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.exception("facons mbq upload failed")
        raise HTTPException(400, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Allocation (Phase B.1 — ref-art level, session-wise, SEPARATE from core pend_alc)
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/alloc/run", response_model=APIResponse)
def alloc_run(stream: str = "ALL", store_scope: str = "UPC", run_date: Optional[str] = None,
              current_user: User = Depends(get_current_user)):
    """Run a FA/CONS allocation for a stream (FA/CONS/ALL) + store scope (UPC/OLD/ALL).
    CENTRAL refs only; pack-rounded; older-store-first pool draw-down. Session-wise."""
    try:
        res = alloc_svc.allocate(stream=stream, store_scope=store_scope, run_date=run_date,
                                 user=_uname(current_user))
        return APIResponse(success=True,
                           message=f"Alloc session {res['session_id']}: {res['ref_rows']} ref line(s), "
                                   f"{int(res['units'])} unit(s) across {res['stores']} store(s)",
                           data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.exception("facons alloc run failed")
        raise HTTPException(500, detail=str(e))


@router.get("/alloc/sessions", response_model=APIResponse)
def alloc_sessions(limit: int = 50, current_user: User = Depends(get_current_user)):
    return APIResponse(success=True, message="ok", data={"items": alloc_svc.list_sessions(limit)})


@router.get("/alloc/results", response_model=APIResponse)
def alloc_results(session_id: int, current_user: User = Depends(get_current_user)):
    return APIResponse(success=True, message="ok", data={"items": alloc_svc.get_ref_rows(session_id)})


class AllocArtReq(BaseModel):
    session_id: int
    ref_id: int
    items: List[dict]


@router.get("/alloc/articles", response_model=APIResponse)
def alloc_articles(session_id: int, ref_id: int, current_user: User = Depends(get_current_user)):
    """Member articles for a ref line — warehouse stock + current split — for the
    manual-override picker."""
    try:
        return APIResponse(success=True, message="ok", data=alloc_svc.article_suggestions(session_id, ref_id))
    except ValueError as e:
        raise HTTPException(404, detail=str(e))


@router.post("/alloc/articles", response_model=APIResponse)
def alloc_save_articles(body: AllocArtReq, current_user: User = Depends(get_current_user)):
    """Save a manual article split (override) for one ref line."""
    try:
        res = alloc_svc.save_articles(body.session_id, body.ref_id, body.items, user=_uname(current_user))
        return APIResponse(success=True, message=f"Saved {res['articles']} article(s) = {int(res['total'])} unit(s)", data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Gap Report — MBQ vs store stock + pending, validated against the warehouse (MSA)
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/gap", response_model=APIResponse)
def gap_report(stream: str = "ALL", store_scope: str = "UPC",
               current_user: User = Depends(get_current_user)):
    try:
        res = gap_svc.compute(stream=stream, store_scope=store_scope)
        s = res["summary"]
        return APIResponse(success=True,
                           message=(f"{s['short']} short · dispatch {int(s['to_dispatch'])} · "
                                    f"purchase {int(s['to_purchase'])} · return {int(s['to_return'])}"),
                           data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.exception("facons gap report failed")
        raise HTTPException(500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Pending Allocation (Phase C — SEPARATE from core pend_alc; core is untouched)
# ══════════════════════════════════════════════════════════════════════════════
class PendManualReq(BaseModel):
    stream: str
    st_cd: str
    ref_art: str
    qty: float
    article_number: Optional[str] = None
    sz: Optional[str] = None
    clr: Optional[str] = None
    rdc: Optional[str] = None
    reason: Optional[str] = None


class PendQtyReq(BaseModel):
    qty: float
    remarks: Optional[str] = None


class PendCloseReq(BaseModel):
    reason: Optional[str] = None


class PendGenerateDoReq(BaseModel):
    pend_ids: List[int]
    do_number: Optional[str] = None      # blank → auto FACONS-DO-000n


class PendUpdateReq(BaseModel):
    article_number: Optional[str] = None
    sz: Optional[str] = None
    clr: Optional[str] = None
    qty: Optional[float] = None
    reason: Optional[str] = None


@router.get("/pend", response_model=APIResponse)
def pend_list(stream: Optional[str] = None, status: Optional[str] = None,
              alloc_session_id: Optional[int] = None, current_user: User = Depends(get_current_user)):
    items = pend_svc.list_pend(stream, status, alloc_session_id)
    return APIResponse(success=True, message=f"{len(items)} line(s)",
                       data={"items": items, "summary": pend_svc.summary(stream)})


@router.post("/pend/approve-session", response_model=APIResponse)
def pend_approve(alloc_session_id: int, current_user: User = Depends(get_current_user)):
    try:
        res = pend_svc.approve_session(alloc_session_id, user=_uname(current_user))
        return APIResponse(success=True, message=f"Approved {res['lines']} line(s) into pending", data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.post("/pend/manual", response_model=APIResponse)
def pend_manual(body: PendManualReq, current_user: User = Depends(get_current_user)):
    try:
        res = pend_svc.add_manual(body.stream, body.st_cd, body.ref_art, body.qty,
                                  article_number=body.article_number, sz=body.sz, clr=body.clr,
                                  rdc=body.rdc, reason=body.reason, user=_uname(current_user))
        return APIResponse(success=True, message=f"Added pending {res['st_cd']}/{res['ref_art']} ({res['qty']})", data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.post("/pend/{pend_id}/deliver", response_model=APIResponse)
def pend_deliver(pend_id: int, body: PendQtyReq, current_user: User = Depends(get_current_user)):
    try:
        res = pend_svc.deliver(pend_id, body.qty, remarks=body.remarks, user=_uname(current_user))
        return APIResponse(success=True, message=f"Delivered — {res['status']}", data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.post("/pend/{pend_id}/close", response_model=APIResponse)
def pend_close(pend_id: int, body: PendCloseReq, current_user: User = Depends(get_current_user)):
    try:
        res = pend_svc.close(pend_id, reason=body.reason, user=_uname(current_user))
        return APIResponse(success=True, message="Closed", data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.post("/pend/generate-do", response_model=APIResponse)
def pend_generate_do(body: PendGenerateDoReq, current_user: User = Depends(get_current_user)):
    try:
        res = pend_svc.generate_do(body.pend_ids, do_number=body.do_number, user=_uname(current_user))
        return APIResponse(success=True, message=f"DO {res['do_number']} generated for {res['lines']} line(s)", data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.put("/pend/{pend_id}", response_model=APIResponse)
def pend_update(pend_id: int, body: PendUpdateReq, current_user: User = Depends(get_current_user)):
    try:
        res = pend_svc.update_line(pend_id, article_number=body.article_number, sz=body.sz,
                                   clr=body.clr, qty=body.qty, reason=body.reason, user=_uname(current_user))
        return APIResponse(success=True, message="Line updated", data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.post("/pend/{pend_id}/reopen", response_model=APIResponse)
def pend_reopen(pend_id: int, current_user: User = Depends(get_current_user)):
    try:
        return APIResponse(success=True, message="Reopened", data=pend_svc.reopen(pend_id, user=_uname(current_user)))
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.get("/pend/ops", response_model=APIResponse)
def pend_ops(stream: Optional[str] = None, action: Optional[str] = None, st_cd: Optional[str] = None,
             limit: int = 500, current_user: User = Depends(get_current_user)):
    items = pend_svc.list_ops(stream, action, st_cd, limit)
    return APIResponse(success=True, message=f"{len(items)} event(s)", data={"items": items})


# ══════════════════════════════════════════════════════════════════════════════
# UPC Store List
# ══════════════════════════════════════════════════════════════════════════════
class StoreAddReq(BaseModel):
    st_cd: str
    remarks: Optional[str] = None


@router.get("/store-list", response_model=APIResponse)
def store_list(current_user: User = Depends(get_current_user)):
    items = store_svc.list_stores()
    return APIResponse(success=True, message=f"{len(items)} store(s)",
                       data={"items": items, "summary": store_svc.summary()})


@router.get("/store-list/validation", response_model=APIResponse)
def store_list_validation(current_user: User = Depends(get_current_user)):
    res = store_svc.validation()
    return APIResponse(success=True,
                       message=f"{len(res['missing_in_master'])} missing in master, {len(res['missing_in_list'])} missing in list",
                       data=res)


@router.post("/store-list", response_model=APIResponse)
def store_add(body: StoreAddReq, current_user: User = Depends(get_current_user)):
    try:
        res = store_svc.add_store(body.st_cd, remarks=body.remarks, user=_uname(current_user))
        return APIResponse(success=True, message=f"{res['st_cd']} {'added' if res['added'] else 'updated'}", data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.delete("/store-list/{row_id}", response_model=APIResponse)
def store_delete(row_id: int, current_user: User = Depends(get_current_user)):
    store_svc.delete_store(row_id, user=_uname(current_user))
    return APIResponse(success=True, message=f"Removed store {row_id}", data=None)


@router.post("/store-list/add-missing", response_model=APIResponse)
def store_add_missing(source: str = "master_upc", status: str = "UPC",
                      current_user: User = Depends(get_current_user)):
    """Bulk-add missing stores: source=master_upc | master | mbq."""
    res = store_svc.add_missing(source=source, status=status, user=_uname(current_user))
    return APIResponse(success=True, message=f"Added {res['added']} store(s) from {res['source']}", data=res)


@router.post("/store-list/upload", response_model=APIResponse)
async def store_upload(file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    if not (file.filename or "").lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, detail="Please upload an .xlsx/.xls file")
    try:
        res = store_svc.ingest_upload(await file.read(), user=_uname(current_user))
        msg = (f"Uploaded: {res['added']} new, {res['existing']} existing"
               + (f", {len(res['missing_in_master'])} not in master" if res['missing_in_master'] else ""))
        return APIResponse(success=True, message=msg, data=res)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.exception("facons store upload failed")
        raise HTTPException(400, detail=str(e))


@router.get("/store-list/history", response_model=APIResponse)
def store_history(st_cd: Optional[str] = None, limit: int = 300,
                  current_user: User = Depends(get_current_user)):
    items = store_svc.list_history(st_cd, limit)
    return APIResponse(success=True, message=f"{len(items)} event(s)", data={"items": items})


@router.get("/store-list/template")
def store_template(current_user: User = Depends(get_current_user)):
    return StreamingResponse(
        iter([store_svc.template_bytes()]), media_type=_XLSX,
        headers={"Content-Disposition": 'attachment; filename="facons_store_list_template.xlsx"'})


@router.get("/mbq/template")
def mbq_template(current_user: User = Depends(get_current_user)):
    return StreamingResponse(
        iter([mbq_svc.template_bytes()]), media_type=_XLSX,
        headers={"Content-Disposition": 'attachment; filename="facons_mbq_template.xlsx"'})


@router.get("/mbq/export")
def mbq_export(stream: Optional[str] = None, current_user: User = Depends(get_current_user)):
    try:
        data = mbq_svc.export_bytes(stream)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    fname = f"facons_mbq_{stream.lower()}.xlsx" if stream else "facons_mbq_all.xlsx"
    return StreamingResponse(
        iter([data]), media_type=_XLSX,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
