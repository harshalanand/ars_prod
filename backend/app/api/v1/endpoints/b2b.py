"""
GRT ALC — Bin-to-Bin Transfer endpoints (prefix /b2b).

Phase 1: overview, settings, and the two-step upload (check, then load).
Phase 2: build MBQ (the store × article demand table), browse it, explain a row.
Allocation, sessions and reports arrive in later phases.

Uploads and builds run as background jobs and return an id at once; the page
polls GET /b2b/upload/{id} or GET /b2b/mbq/build/{id} for progress.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from loguru import logger
from pydantic import BaseModel

from app.models.rbac import User
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.services import b2b_schema, b2b_settings
from app.services import b2b_alloc_service as alloc_svc
from app.services import b2b_extract_service as extract_svc
from app.services import b2b_gap_service as gap_svc
from app.services import b2b_mbq_service as mbq_svc
from app.services import b2b_overview_service as overview_svc
from app.services import b2b_sessions_service as sessions_svc
from app.services import b2b_upload_service as upload_svc

router = APIRouter(prefix="/b2b", tags=["GRT ALC — Bin-to-Bin"])


def _uname(user: User) -> str:
    return getattr(user, "username", None) or getattr(user, "email", None) or "system"


def _fail(e: Exception, what: str):
    if isinstance(e, (ValueError, RuntimeError)):
        raise HTTPException(400, detail=str(e))
    logger.exception(f"[b2b] {what} failed")
    raise HTTPException(500, detail=f"{what} failed: {e}")


# ── overview ────────────────────────────────────────────────────────────────
@router.get("/overview", response_model=APIResponse)
def get_overview(current_user: User = Depends(get_current_user)):
    try:
        return APIResponse(data=overview_svc.overview())
    except Exception as e:
        _fail(e, "Overview")


@router.post("/repair-tables", response_model=APIResponse)
def repair_tables(current_user: User = Depends(get_current_user)):
    """Re-run the schema file. Creates only what is missing; never drops."""
    try:
        b2b_schema.ensure_tables(force=True)
        seeded = b2b_settings.seed()
        counts = b2b_schema.table_counts()
        missing = [t for t, n in counts.items() if n < 0]
        return APIResponse(success=not missing,
                           message=("All 10 tables present" if not missing
                                    else f"Still missing: {', '.join(missing)}")
                                   + (f" · seeded {len(seeded)} setting(s)" if seeded else ""),
                           data={"tables": counts, "seeded": seeded})
    except Exception as e:
        _fail(e, "Repair tables")


# ── settings ────────────────────────────────────────────────────────────────
class SettingsSaveReq(BaseModel):
    values: Dict[str, Any]


class SettingsResetReq(BaseModel):
    keys: Optional[List[str]] = None


@router.get("/settings", response_model=APIResponse)
def get_settings(current_user: User = Depends(get_current_user)):
    try:
        return APIResponse(data={"settings": b2b_settings.get_all()})
    except Exception as e:
        _fail(e, "Settings")


@router.put("/settings", response_model=APIResponse)
def save_settings(body: SettingsSaveReq, current_user: User = Depends(get_current_user)):
    try:
        res = b2b_settings.save(body.values, _uname(current_user))
        n = len(res["changed"])
        msg = ("Nothing changed" if not n else f"Saved {n} setting(s)"
               + (". Rebuild MBQ before the next allocation." if res["needs_rebuild"] else "."))
        return APIResponse(message=msg, data={**res, "settings": b2b_settings.get_all()})
    except Exception as e:
        _fail(e, "Save settings")


@router.post("/settings/reset", response_model=APIResponse)
def reset_settings(body: SettingsResetReq, current_user: User = Depends(get_current_user)):
    try:
        res = b2b_settings.reset_to_defaults(body.keys, _uname(current_user))
        return APIResponse(message=f"Reset {len(res['changed'])} setting(s) to the default",
                           data={**res, "settings": b2b_settings.get_all()})
    except Exception as e:
        _fail(e, "Reset settings")


# ── upload ──────────────────────────────────────────────────────────────────
class CheckPathReq(BaseModel):
    path: str
    mode: str = "OVERWRITE"
    sheets: Optional[List[str]] = None       # bin | store | req — all three by default
    bin_rdc: Optional[str] = None            # warehouse of the bins; default from the file name
    swap_store_cols: bool = False            # swap RDC and OLD\NEW back for this load


@router.get("/upload/folder", response_model=APIResponse)
def upload_folder(folder: Optional[str] = None, current_user: User = Depends(get_current_user)):
    """Workbooks in the source folder, newest first. Lists names only."""
    try:
        return APIResponse(data=upload_svc.list_folder(folder))
    except Exception as e:
        _fail(e, "Folder listing")


@router.get("/upload/defaults", response_model=APIResponse)
def upload_defaults(current_user: User = Depends(get_current_user)):
    return APIResponse(data={
        "path": upload_svc.DEFAULT_PATH,
        "sheets": [{"key": k, "sheet": s, "table": upload_svc.SHEETS[s][0],
                    "columns": [c[0] for c in upload_svc.SHEETS[s][1]]}
                   for s, k in upload_svc.SHEET_KEYS.items()],
        "running": upload_svc.running_job(),
    })


@router.post("/upload/check-path", response_model=APIResponse)
def check_path(body: CheckPathReq, current_user: User = Depends(get_current_user)):
    """Check a workbook the server can reach on the network. Writes nothing."""
    try:
        uid = upload_svc.start_check("PATH", body.path, body.mode, body.sheets or [],
                                     _uname(current_user),
                                     options={"bin_rdc": body.bin_rdc,
                                              "swap_store_cols": body.swap_store_cols})
        return APIResponse(message=f"Checking — upload {uid}", data={"upload_id": uid})
    except Exception as e:
        _fail(e, "Check")


# Sync (not async) on purpose: copying a large upload to disk would otherwise
# block the event loop. FastAPI runs a sync endpoint in its thread pool.
@router.post("/upload/check-file", response_model=APIResponse)
def check_file(file: UploadFile = File(...), mode: str = Form("OVERWRITE"),
               sheets: str = Form("bin,store,req"), bin_rdc: str = Form(""),
               swap_store_cols: bool = Form(False),
               current_user: User = Depends(get_current_user)):
    """Check an uploaded workbook. Writes nothing to the data tables."""
    try:
        uid = upload_svc.start_check("FILE", "", mode,
                                     [s for s in sheets.split(",") if s.strip()],
                                     _uname(current_user), file_obj=file.file,
                                     file_name=file.filename,
                                     options={"bin_rdc": bin_rdc or None,
                                              "swap_store_cols": swap_store_cols})
        return APIResponse(message=f"Checking — upload {uid}", data={"upload_id": uid})
    except Exception as e:
        _fail(e, "Check")


@router.post("/upload/{upload_id}/load", response_model=APIResponse)
def load_upload(upload_id: int, current_user: User = Depends(get_current_user)):
    """Load a checked upload. All chosen sheets in one transaction."""
    try:
        upload_svc.start_load(upload_id, _uname(current_user))
        return APIResponse(message=f"Loading upload {upload_id}", data={"upload_id": upload_id})
    except Exception as e:
        _fail(e, "Load")


@router.post("/upload/{upload_id}/cancel", response_model=APIResponse)
def cancel_upload(upload_id: int, current_user: User = Depends(get_current_user)):
    ok = upload_svc.cancel(upload_id)
    return APIResponse(success=ok, message="Cancelling" if ok else "That upload is not running")


@router.get("/upload/{upload_id}", response_model=APIResponse)
def get_upload(upload_id: int, current_user: User = Depends(get_current_user)):
    try:
        row = upload_svc.get(upload_id)
        if row is None:
            raise HTTPException(404, detail=f"Upload {upload_id} not found")
        return APIResponse(data=row)
    except HTTPException:
        raise
    except Exception as e:
        _fail(e, "Upload status")


@router.get("/uploads", response_model=APIResponse)
def list_uploads(limit: int = 15, current_user: User = Depends(get_current_user)):
    try:
        upload_svc.purge_stale_stages()
        return APIResponse(data={"uploads": upload_svc.recent(min(max(limit, 1), 100)),
                                 "running": upload_svc.running_job()})
    except Exception as e:
        _fail(e, "Upload list")


# ── build MBQ ───────────────────────────────────────────────────────────────
@router.get("/mbq", response_model=APIResponse)
def mbq_status(current_user: User = Depends(get_current_user)):
    """Latest build with its summary and checks, the running build, freshness."""
    try:
        return APIResponse(data=mbq_svc.status())
    except Exception as e:
        _fail(e, "MBQ status")


@router.post("/mbq/build", response_model=APIResponse)
def mbq_build(current_user: User = Depends(get_current_user)):
    """Rebuild the demand table as a background job. The previous build stays
    in place until this one has passed its checks and committed."""
    try:
        bid = mbq_svc.start_build(_uname(current_user))
        return APIResponse(message=f"Building — build {bid}", data={"build_id": bid})
    except Exception as e:
        _fail(e, "Build MBQ")


@router.get("/mbq/build/{build_id}", response_model=APIResponse)
def mbq_build_get(build_id: int, current_user: User = Depends(get_current_user)):
    try:
        row = mbq_svc.get(build_id)
        if row is None:
            raise HTTPException(404, detail=f"Build {build_id} not found")
        return APIResponse(data=row)
    except HTTPException:
        raise
    except Exception as e:
        _fail(e, "Build status")


@router.post("/mbq/build/{build_id}/cancel", response_model=APIResponse)
def mbq_build_cancel(build_id: int, current_user: User = Depends(get_current_user)):
    ok = mbq_svc.cancel(build_id)
    return APIResponse(success=ok, message="Cancelling" if ok else "That build is not running")


@router.get("/mbq/rows", response_model=APIResponse)
def mbq_rows(store: Optional[str] = None, art: Optional[str] = None, maj_cat: Optional[str] = None,
             only: Optional[str] = None, sort: str = "shortfall", page: int = 1, size: int = 50,
             current_user: User = Depends(get_current_user)):
    """Page through the demand table. `only` = short | excess | zero."""
    try:
        return APIResponse(data=mbq_svc.browse(store, art, maj_cat, only, sort, page, size))
    except Exception as e:
        _fail(e, "Demand rows")


@router.get("/mbq/explain", response_model=APIResponse)
def mbq_explain(store: str, art: str, current_user: User = Depends(get_current_user)):
    """Walk one store × article through the build: why this target, why this shortfall."""
    try:
        return APIResponse(data=mbq_svc.explain(store, art))
    except Exception as e:
        _fail(e, "Explain")


# ── run allocation ──────────────────────────────────────────────────────────
class RunReq(BaseModel):
    priority: str = "SHORTFALL_DESC"
    min_qty: int = 1
    fill_mode: str = "GREEDY"
    fair_basis: str = "PROPORTIONAL"
    bin_pick: str = "MAX_CONSUMPTION"
    cross_rdc: Optional[str] = None          # required: no default (G11)
    dry_run: bool = False
    note: Optional[str] = None


@router.get("/run", response_model=APIResponse)
def run_page(current_user: User = Depends(get_current_user)):
    """Defaults, gates, warehouses, the running session and recent sessions."""
    try:
        return APIResponse(data=alloc_svc.page())
    except Exception as e:
        _fail(e, "Run page")


@router.post("/run", response_model=APIResponse)
def run_start(body: RunReq, current_user: User = Depends(get_current_user)):
    """Start a session. Every run is a new session; nothing is overwritten."""
    try:
        sid = alloc_svc.start_run(body.model_dump(), body.dry_run, body.note, _uname(current_user))
        return APIResponse(message=f"{'Dry run' if body.dry_run else 'Session'} {sid} started",
                           data={"session_id": sid})
    except Exception as e:
        _fail(e, "Run allocation")


@router.get("/run/{session_id}", response_model=APIResponse)
def run_get(session_id: int, current_user: User = Depends(get_current_user)):
    try:
        row = alloc_svc.get(session_id)
        if row is None:
            raise HTTPException(404, detail=f"Session {session_id} not found")
        return APIResponse(data=row)
    except HTTPException:
        raise
    except Exception as e:
        _fail(e, "Session status")


@router.post("/run/{session_id}/cancel", response_model=APIResponse)
def run_cancel(session_id: int, current_user: User = Depends(get_current_user)):
    ok = alloc_svc.cancel(session_id)
    return APIResponse(success=ok, message="Cancelling" if ok else "That session is not running")


@router.get("/sessions", response_model=APIResponse)
def sessions_list(limit: int = 30, current_user: User = Depends(get_current_user)):
    try:
        return APIResponse(data={"sessions": alloc_svc.recent(min(max(limit, 1), 200)),
                                 "running": alloc_svc.running_run()})
    except Exception as e:
        _fail(e, "Sessions")


@router.delete("/sessions/{session_id}", response_model=APIResponse)
def sessions_delete(session_id: int, confirm: str = "", current_user: User = Depends(get_current_user)):
    """Delete one session from all three tables. Requires confirm=DELETE."""
    if confirm != "DELETE":
        raise HTTPException(400, detail="Type DELETE to confirm")
    try:
        removed = alloc_svc.delete(session_id, _uname(current_user))
        sessions_svc.purge_exports(session_id)
        return APIResponse(message=f"Session {session_id} deleted", data={"removed": removed})
    except Exception as e:
        _fail(e, "Delete session")


# ── review a session (Step 5) ───────────────────────────────────────────────
# /sessions/compare is declared before /sessions/{session_id} so the literal
# path is not read as an id.
@router.get("/sessions/compare", response_model=APIResponse)
def sessions_compare(a: int, b: int, current_user: User = Depends(get_current_user)):
    """Two stored sessions side by side: totals, stores, categories, lines."""
    try:
        return APIResponse(data=sessions_svc.compare(a, b))
    except Exception as e:
        _fail(e, "Compare sessions")


@router.get("/sessions/{session_id}", response_model=APIResponse)
def sessions_one(session_id: int, current_user: User = Depends(get_current_user)):
    try:
        return APIResponse(data=sessions_svc.session(session_id))
    except Exception as e:
        _fail(e, "Session")


@router.get("/sessions/{session_id}/lines", response_model=APIResponse)
def sessions_lines(session_id: int, store: Optional[str] = None, art: Optional[str] = None,
                   maj_cat: Optional[str] = None, sort: str = "seq", page: int = 1, size: int = 50,
                   current_user: User = Depends(get_current_user)):
    try:
        return APIResponse(data=sessions_svc.lines(session_id, store, art, maj_cat, sort, page, size))
    except Exception as e:
        _fail(e, "Session lines")


@router.get("/sessions/{session_id}/picks", response_model=APIResponse)
def sessions_picks(session_id: int, store: Optional[str] = None, bin: Optional[str] = None,
                   art: Optional[str] = None, maj_cat: Optional[str] = None, rdc: Optional[str] = None,
                   cross: bool = False, sort: str = "walk", page: int = 1, size: int = 50,
                   current_user: User = Depends(get_current_user)):
    """The pick list in warehouse walking order (BIN_RDC, BIN, PICK_SEQ)."""
    try:
        return APIResponse(data=sessions_svc.picks(session_id, store, bin, art, maj_cat, rdc, cross,
                                                   sort, page, size))
    except Exception as e:
        _fail(e, "Pick list")


@router.get("/sessions/{session_id}/leftovers", response_model=APIResponse)
def sessions_leftovers(session_id: int, reason: Optional[str] = None, maj_cat: Optional[str] = None,
                       bin: Optional[str] = None, art: Optional[str] = None, page: int = 1, size: int = 50,
                       current_user: User = Depends(get_current_user)):
    try:
        return APIResponse(data=sessions_svc.leftovers(session_id, reason, maj_cat, bin, art, page, size))
    except Exception as e:
        _fail(e, "Leftovers")


@router.get("/sessions/{session_id}/why", response_model=APIResponse)
def sessions_why(session_id: int, store: str, art: Optional[str] = None,
                 current_user: User = Depends(get_current_user)):
    """Why a store (or a store × article) got no stock in this session."""
    try:
        return APIResponse(data=sessions_svc.why(session_id, store, art))
    except Exception as e:
        _fail(e, "Why no stock")


@router.get("/sessions/{session_id}/export")
def sessions_export(session_id: int, kind: str = "xlsx", current_user: User = Depends(get_current_user)):
    """kind = xlsx (the pick-list workbook) | picks (pick list CSV) |
    leftovers (leftover CSV). Locked unless every balance check passed;
    built once per session and kept."""
    try:
        path = sessions_svc.export(session_id, kind)
    except ValueError as e:
        raise HTTPException(409, detail=str(e))
    except Exception as e:
        _fail(e, "Export")
    media = ("text/csv" if path.suffix == ".csv"
             else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return FileResponse(str(path), media_type=media, filename=path.name)


# ── reports (Phase 5) ───────────────────────────────────────────────────────
@router.get("/gap", response_model=APIResponse)
def gap_report(session_id: Optional[int] = None, current_user: User = Depends(get_current_user)):
    """The gap report: contents, and a preview of every sheet. Read only."""
    try:
        return APIResponse(data=gap_svc.report(session_id))
    except Exception as e:
        _fail(e, "Gap report")


@router.get("/gap/sheet/{key}")
def gap_sheet_csv(key: str, session_id: Optional[int] = None, current_user: User = Depends(get_current_user)):
    try:
        name, data = gap_svc.sheet_csv(session_id, key)
    except ValueError as e:
        raise HTTPException(404, detail=str(e))
    return Response(content=data, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/gap/workbook")
def gap_workbook(session_id: Optional[int] = None, current_user: User = Depends(get_current_user)):
    try:
        path = gap_svc.workbook(session_id)
    except Exception as e:
        _fail(e, "Gap report workbook")
    return FileResponse(str(path), filename=path.name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


class ExtractReq(BaseModel):
    object: str
    stores: List[str] = []
    arts: List[str] = []
    session_id: Optional[int] = None
    format: str = "csv"


@router.get("/extract/objects", response_model=APIResponse)
def extract_objects(current_user: User = Depends(get_current_user)):
    try:
        return APIResponse(data={"objects": extract_svc.objects()})
    except Exception as e:
        _fail(e, "Extract objects")


@router.post("/extract/parse", response_model=APIResponse)
def extract_parse(file: UploadFile = File(...), kind: str = Form("store"), column: str = Form(""),
                  current_user: User = Depends(get_current_user)):
    """Codes from an uploaded csv / txt / xlsx; the column is guessed and can be overridden."""
    try:
        return APIResponse(data=extract_svc.from_file(file.file.read(), file.filename or "", kind, column or None))
    except Exception as e:
        _fail(e, "Read the list")


@router.post("/extract/preview", response_model=APIResponse)
def extract_preview(body: ExtractReq, current_user: User = Depends(get_current_user)):
    """Row count, which codes matched, and the first rows. Nothing is downloaded."""
    try:
        return APIResponse(data=extract_svc.preview(body.object, body.stores, body.arts, body.session_id))
    except Exception as e:
        _fail(e, "Extract preview")


@router.post("/extract/download")
def extract_download(body: ExtractReq, current_user: User = Depends(get_current_user)):
    try:
        name, data, media = extract_svc.download(body.object, body.stores, body.arts, body.session_id,
                                                 "xlsx" if body.format == "xlsx" else "csv")
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        _fail(e, "Extract download")
    return Response(content=data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})
