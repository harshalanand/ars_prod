"""
1SZ ALC — size allocation endpoints (prefix /sz1).

Step 1  Generate SZ Base         preview (no write) · build (background job)
Step 2  Generate ST/MJ/SZ req    preview (no write) · build (background job)
Step 3  Store × article run      stores → validate · preview (no write) · run (background job)
        Sessions                 list · one · lines · export CSV · delete

Builds and runs return an id at once; the page polls GET /sz1/build/{id} or
GET /sz1/run/{id}. Only one 1SZ ALC job runs at a time.
Spec: frontend/public/docs/manual/sz1_alloc.md.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel

from app.models.rbac import User
from app.schemas.common import APIResponse
from app.security.dependencies import RequirePermissions, get_current_user
from app.services import b2b_extract_service as extract_svc
from app.services import sz1_alloc_engine as E
from app.services import sz1_approve_service as approve_svc
from app.services import sz1_base_service as base_svc
from app.services import sz1_gap_service as gap_svc
from app.services import sz1_quality_service as quality_svc
from app.services import sz1_run_service as run_svc
from app.services import sz1_filter, sz1_readiness, sz1_schema, sz1_settings

router = APIRouter(prefix="/sz1", tags=["1SZ ALC — Size Allocation"])


def _uname(user: User) -> str:
    return getattr(user, "username", None) or getattr(user, "email", None) or "system"


def _fail(e: Exception, what: str):
    if isinstance(e, (ValueError, RuntimeError)):
        raise HTTPException(400, detail=str(e))
    logger.exception(f"[sz1] {what} failed")
    raise HTTPException(500, detail=f"{what} failed: {e}")


# ── status, Steps 1 and 2 ───────────────────────────────────────────────────
@router.get("/status", response_model=APIResponse)
def status(current_user: User = Depends(get_current_user)):
    """Latest Step 1 / Step 2 builds, the running job, table sizes, warnings."""
    try:
        return APIResponse(data=base_svc.status())
    except Exception as e:
        _fail(e, "Status")


@router.get("/readiness", response_model=APIResponse)
def readiness(scope: str = "run", current_user: User = Depends(get_current_user)):
    """Validate first, then create: the checklist for a Step 1 build (build1),
    a Step 2 build (build2) or a Step 3 run (run) — sources, order, freshness,
    rules, coverage, sessions — each item ok / warn / block. Read only."""
    if scope not in sz1_readiness.SCOPES:
        raise HTTPException(400, detail=f"scope must be one of {', '.join(sz1_readiness.SCOPES)}")
    try:
        return APIResponse(data=sz1_readiness.check(scope))
    except Exception as e:
        _fail(e, "Readiness check")


def _readiness_item(scope: str, key: str) -> None:
    """400 unless `key` is an item of `scope` that has a list behind it."""
    if scope not in sz1_readiness.SCOPES:
        raise HTTPException(400, detail=f"scope must be one of {', '.join(sz1_readiness.SCOPES)}")
    if sz1_readiness.row_kind(scope, key) is None:
        raise HTTPException(400, detail=f"Readiness item '{key}' has no list for scope {scope}")


@router.get("/readiness/{scope}/items/{key}/rows", response_model=APIResponse)
def readiness_item_rows(scope: str, key: str, top: int = sz1_readiness.ROW_TOP,
                        current_user: User = Depends(get_current_user)):
    """Review: the rows behind one readiness item (e.g. cov_grid — the listed
    store × MAJ_CAT pairs with no grid row), by the rule the check counts.
    {columns, rows, total, note, truncated, seconds}. Read only."""
    _readiness_item(scope, key)
    try:
        return APIResponse(data=sz1_readiness.item_rows(scope, key, top))
    except Exception as e:
        _fail(e, "Readiness list")


@router.get("/readiness/{scope}/items/{key}/export")
def readiness_item_export(scope: str, key: str, current_user: User = Depends(get_current_user)):
    """Review: every row behind one readiness item as CSV. Read only."""
    _readiness_item(scope, key)
    try:
        name, body = sz1_readiness.export_csv(scope, key)
    except Exception as e:
        _fail(e, "Readiness export")
    return StreamingResponse(body, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/repair-tables", response_model=APIResponse)
def repair_tables(current_user: User = Depends(get_current_user)):
    """Re-run 039_sz1_module.sql. Creates only what is missing; never drops."""
    try:
        sz1_schema.ensure_tables(force=True)
        counts = sz1_schema.table_counts()
        missing = [t for t in sz1_schema.ALL_TABLES if counts.get(t) == -1]
        return APIResponse(success=not missing,
                           message=f"All {len(sz1_schema.ALL_TABLES)} tables present" if not missing else f"Still missing: {', '.join(missing)}",
                           data={"tables": counts})
    except Exception as e:
        _fail(e, "Repair tables")


@router.post("/repair-balances", response_model=APIResponse)
def repair_balances(current_user: User = Depends(get_current_user)):
    """Re-apply BAL_FNL_Q and REM_MJ_SZ_SRT from the latest session (chain)."""
    try:
        ids = base_svc.repair_balances(_uname(current_user))
        return APIResponse(message="Balances re-applied" + (f" from session(s) {', '.join(ids)}" if ids
                                                              else " — no session on record, back to full"),
                           data={"balance_sessions": ids})
    except Exception as e:
        _fail(e, "Re-apply balances")


class SettingsReq(BaseModel):
    values: Dict[str, Any] = {}


@router.get("/settings", response_model=APIResponse)
def settings_get(current_user: User = Depends(get_current_user)):
    """Step 1 rule settings (R1–R4 switches and values), the defaults and the tool preset."""
    try:
        return APIResponse(data={"settings": sz1_settings.get_all(), "defaults": sz1_settings.defaults(),
                                 "tool_preset": sz1_settings.TOOL_PRESET, "meta": sz1_settings.meta()})
    except Exception as e:
        _fail(e, "Settings")


@router.put("/settings", response_model=APIResponse)
def settings_save(body: SettingsReq, current_user: User = Depends(get_current_user)):
    """Save rule values. They apply from the next Step 1 build."""
    try:
        saved = sz1_settings.save(body.values, _uname(current_user))
        return APIResponse(message="Rule settings saved — generate Step 1, then Step 2, to apply them",
                           data={"settings": saved, "meta": sz1_settings.meta()})
    except Exception as e:
        _fail(e, "Save settings")


@router.post("/settings/reset", response_model=APIResponse)
def settings_reset(current_user: User = Depends(get_current_user)):
    try:
        saved = sz1_settings.save(sz1_settings.defaults(), _uname(current_user))
        return APIResponse(message="Rule settings reset to the defaults", data={"settings": saved})
    except Exception as e:
        _fail(e, "Reset settings")


@router.post("/preview/{step}", response_model=APIResponse)
def preview(step: int, body: Optional[SettingsReq] = None, current_user: User = Depends(get_current_user)):
    """Run the step's selection without writing: sample rows and per-RDC counts.
    Step 1 takes optional unsaved rule values (Preview with these values)."""
    try:
        if step == 1:
            return APIResponse(data=base_svc.preview_base(settings=(body.values if body else None)))
        if step == 2:
            return APIResponse(data=base_svc.preview_req())
        raise ValueError("step must be 1 or 2")
    except Exception as e:
        _fail(e, f"Step {step} preview")


@router.post("/build/{step}", response_model=APIResponse)
def build(step: int, confirm: bool = False, current_user: User = Depends(get_current_user)):
    """Rebuild the step's table as a background job. Needs confirm=true."""
    if not confirm:
        raise HTTPException(400, detail="Tick the confirmation first: a build replaces every row of the table.")
    try:
        bid = base_svc.start_build(step, _uname(current_user))
        return APIResponse(message=f"Step {step} build {bid} started", data={"build_id": bid})
    except Exception as e:
        _fail(e, f"Step {step} build")


@router.get("/build/{build_id}", response_model=APIResponse)
def build_get(build_id: int, current_user: User = Depends(get_current_user)):
    try:
        row = base_svc.get_build(build_id)
        if row is None:
            raise HTTPException(404, detail=f"Build {build_id} not found")
        return APIResponse(data=row)
    except HTTPException:
        raise
    except Exception as e:
        _fail(e, "Build status")


@router.get("/builds", response_model=APIResponse)
def builds(step: int, limit: int = 10, current_user: User = Depends(get_current_user)):
    try:
        return APIResponse(data={"builds": base_svc.recent_builds(step, min(max(limit, 1), 50))})
    except Exception as e:
        _fail(e, "Build history")


@router.delete("/builds", response_model=APIResponse)
def builds_clear(step: int, confirm: str = "", current_user: User = Depends(get_current_user)):
    """Clear a step's build history, keeping the latest successful build. Requires confirm=CLEAR."""
    if confirm != "CLEAR":
        raise HTTPException(400, detail="Confirm first: this removes the older build-history rows")
    try:
        n = base_svc.clear_builds(step, _uname(current_user))
        return APIResponse(message=f"Removed {n} build-history row(s); the latest build is kept", data={"removed": n})
    except Exception as e:
        _fail(e, "Clear build history")


@router.get("/distinct/{kind}", response_model=APIResponse)
def distinct_values(kind: str, col: str, cf: Optional[str] = None, search: Optional[str] = None,
                    rdc: Optional[str] = None, maj_cat: Optional[str] = None, store: Optional[str] = None,
                    art: Optional[str] = None, sz: Optional[str] = None, rule: Optional[str] = None,
                    cont_src: Optional[str] = None, gap_type: Optional[str] = None, only_excluded: bool = False,
                    current_user: User = Depends(get_current_user)):
    """The distinct values (with counts) a column's filter list offers, from the
    WHOLE table under the page's other filters. kind = base | req | gap."""
    try:
        if kind in ("base", "req"):
            _t, w, p, _o = base_svc._built_filter(1 if kind == "base" else 2, rdc, maj_cat, store, art=art, sz=sz,
                                                   rule=rule, cont_src=cont_src)
        else:
            w, p = gap_svc._filter(rdc, maj_cat, gap_type, only_excluded, art)
        base_where = [w[len("WHERE "):]] if w else []
        return APIResponse(data=sz1_filter.distinct(kind, col, sz1_filter.parse(cf), search, base_where, p))
    except Exception as e:
        _fail(e, "Filter values")


@router.get("/rows/{step}", response_model=APIResponse)
def built_rows(step: int, rdc: Optional[str] = None, maj_cat: Optional[str] = None, store: Optional[str] = None,
               art: Optional[str] = None, sz: Optional[str] = None, rule: Optional[str] = None,
               cont_src: Optional[str] = None, cf: Optional[str] = None, top: int = 200,
               ob: Optional[str] = None, od: Optional[str] = None,
               current_user: User = Depends(get_current_user)):
    """Rows of the built Step 1 / Step 2 table, filtered. Read only."""
    try:
        f = dict(art=art, sz=sz, rule=rule, cont_src=cont_src, cf=sz1_filter.parse(cf))
        return APIResponse(data={"rows": base_svc.sample(step, top, rdc, maj_cat, store, ob=ob, od=od, **f),
                                 "total": base_svc.count_rows(step, rdc, maj_cat, store, **f)})
    except Exception as e:
        _fail(e, "Rows")


@router.get("/export/{step}")
def built_export(step: int, rdc: Optional[str] = None, maj_cat: Optional[str] = None, store: Optional[str] = None,
                 art: Optional[str] = None, sz: Optional[str] = None, rule: Optional[str] = None,
                 cont_src: Optional[str] = None, cf: Optional[str] = None, ob: Optional[str] = None,
                 od: Optional[str] = None, current_user: User = Depends(get_current_user)):
    """The built Step 1 / Step 2 table as CSV — all rows, or the browse filters. Read only."""
    if step not in (1, 2):
        raise HTTPException(400, detail="step must be 1 or 2")
    flt = "_".join(x.strip().upper().replace(" ", "") for x in (rdc, maj_cat, store, art, sz, rule, cont_src) if x and x.strip())
    name = f"1SZ_ALC_step{step}_{'SZ_BASE' if step == 1 else 'ST_MJ_SZ_REQ'}{'_' + flt if flt else ''}.csv"
    return StreamingResponse(base_svc.export_csv(step, rdc, maj_cat, store, art=art, sz=sz, rule=rule, cont_src=cont_src, cf=sz1_filter.parse(cf), ob=ob, od=od), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ── CONT gap report (rebuilt with every Step 1 build) ───────────────────────
@router.get("/gap", response_model=APIResponse)
def gap_summary(current_user: User = Depends(get_current_user)):
    """Zero-CONT gap of the latest Step 1 build: by type, RDC, source, category."""
    try:
        return APIResponse(data=gap_svc.summary())
    except Exception as e:
        _fail(e, "Gap report")


@router.get("/gap/rows", response_model=APIResponse)
def gap_rows(rdc: Optional[str] = None, maj_cat: Optional[str] = None, gap_type: Optional[str] = None,
             only_excluded: bool = False, art: Optional[str] = None, sort: str = "pcs", page: int = 1, size: int = 100,
             cf: Optional[str] = None, ob: Optional[str] = None, od: Optional[str] = None,
             current_user: User = Depends(get_current_user)):
    try:
        return APIResponse(data=gap_svc.lines(rdc, maj_cat, gap_type, only_excluded, art, sort, page, size, sz1_filter.parse(cf), ob, od))
    except Exception as e:
        _fail(e, "Gap lines")


@router.get("/gap/export")
def gap_export(rdc: Optional[str] = None, maj_cat: Optional[str] = None, gap_type: Optional[str] = None,
               only_excluded: bool = False, art: Optional[str] = None, cf: Optional[str] = None,
               sort: str = "cat", ob: Optional[str] = None, od: Optional[str] = None,
               current_user: User = Depends(get_current_user)):
    """The gap lines as CSV — all, or filtered like the page."""
    flt = "_".join(x.strip().upper() for x in (rdc, maj_cat, gap_type) if x and x.strip())
    name = f"1SZ_ALC_CONT_GAP{'_' + flt if flt else ''}{'_excluded' if only_excluded else ''}.csv"
    return StreamingResponse(gap_svc.export_csv(rdc, maj_cat, gap_type, only_excluded, art, sz1_filter.parse(cf), sort, ob, od), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ── master data quality (read only; worked out together, kept 5 min) ────────
@router.get("/quality", response_model=APIResponse)
def quality_summary(refresh: bool = False, current_user: User = Depends(get_current_user)):
    """What the 1SZ rules work around in the masters: A / NA beside real sizes,
    curves off 100 %, CO curve problems, duplicate keys, listed store × category
    with no grid row (with a diagnosis), no size curve at all. KPI + first 200
    rows per finding. refresh=true re-reads instead of the 5-minute copy."""
    try:
        return APIResponse(data=quality_svc.summary(refresh))
    except Exception as e:
        _fail(e, "Data quality report")


@router.get("/quality/{key}/rows", response_model=APIResponse)
def quality_rows(key: str, cf: Optional[str] = None, ob: Optional[str] = None, od: Optional[str] = None,
                 page: int = 1, size: int = 200, current_user: User = Depends(get_current_user)):
    """One finding's rows, column-filtered (`cf`), sorted (`ob`/`od`) and paged."""
    try:
        return APIResponse(data=quality_svc.rows(key, sz1_filter.parse(cf), ob, od, page, size))
    except Exception as e:
        _fail(e, "Data quality rows")


@router.get("/quality/{key}/distinct", response_model=APIResponse)
def quality_distinct(key: str, col: str, cf: Optional[str] = None, search: Optional[str] = None,
                     current_user: User = Depends(get_current_user)):
    """A column's distinct values (with counts) under the other filters — the filter list."""
    try:
        return APIResponse(data=quality_svc.distinct(key, col, sz1_filter.parse(cf), search))
    except Exception as e:
        _fail(e, "Filter values")


@router.get("/quality/{key}/export")
def quality_export(key: str, cf: Optional[str] = None, ob: Optional[str] = None, od: Optional[str] = None,
                   current_user: User = Depends(get_current_user)):
    """Every row of one finding (or the filtered ones) as CSV."""
    try:
        name = quality_svc.export_name(key)
        body = quality_svc.export_csv(key, sz1_filter.parse(cf), ob, od)
    except Exception as e:
        _fail(e, "Data quality export")
    return StreamingResponse(body, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ── Step 3: stores, preview, run ────────────────────────────────────────────
class StoresReq(BaseModel):
    text: str = ""
    stock_basis: str = "initial"


class PreviewReq(BaseModel):
    codes: List[str]
    stock_basis: str = "initial"
    cross_rdc: str = "all"


class RunReq(BaseModel):
    codes: List[str]
    mode: str = "weighted"
    weight_mj: float = 0.5
    sequence: Optional[str] = None
    stock_basis: str = "initial"
    cross_rdc: str = "all"
    protect_other: bool = True          # All RDC: hold back what the other RDC's own stores need
    one_source: bool = True             # All RDC: fill a line from one warehouse where it can
    note: Optional[str] = None


class SimulateReq(BaseModel):
    codes: List[str]
    mode: str = "weighted"
    weight_mj: float = 0.5
    sequence: Optional[str] = None
    stock_basis: str = "initial"
    variants: Optional[List[Dict[str, Any]]] = None


@router.get("/run", response_model=APIResponse)
def run_page(current_user: User = Depends(get_current_user)):
    """Gates, choices, the running job and recent sessions."""
    try:
        return APIResponse(data=run_svc.page())
    except Exception as e:
        _fail(e, "Run page")


@router.post("/stores/validate", response_model=APIResponse)
def stores_validate(body: StoresReq, current_user: User = Depends(get_current_user)):
    """Parse a pasted list and report each code's status. Writes nothing."""
    try:
        codes = E.parse_store_codes(body.text)
        return APIResponse(data={"codes": codes, "stores": run_svc.validate_stores(codes, body.stock_basis)})
    except Exception as e:
        _fail(e, "Check stores")


@router.post("/stores/parse", response_model=APIResponse)
def stores_parse(file: UploadFile = File(...), column: str = Form(""),
                 current_user: User = Depends(get_current_user)):
    """Store codes from an uploaded csv / txt / xlsx; the column is guessed
    (ST_CD, STORE_CODE, WERKS …) and can be overridden."""
    try:
        return APIResponse(data=extract_svc.from_file(file.file.read(), file.filename or "", "store", column or None))
    except Exception as e:
        _fail(e, "Read the store list")


@router.post("/run/preview", response_model=APIResponse)
def run_preview(body: PreviewReq, current_user: User = Depends(get_current_user)):
    """Store validation, candidate lines per store and the I_ROD effect. Writes nothing."""
    try:
        return APIResponse(data=run_svc.preview(E.parse_store_codes(" ".join(body.codes)),
                                                body.stock_basis, body.cross_rdc))
    except Exception as e:
        _fail(e, "Run preview")


@router.post("/run/simulate", response_model=APIResponse)
def run_simulate(body: SimulateReq, current_user: User = Depends(get_current_user)):
    """Allocate in memory under several warehouse modes (default Own RDC and All RDC
    protected, one source per line) and compare them. Writes nothing."""
    try:
        return APIResponse(data=run_svc.simulate(body.model_dump()))
    except Exception as e:
        _fail(e, "Simulate")


@router.post("/run", response_model=APIResponse)
def run_start(body: RunReq, current_user: User = Depends(get_current_user)):
    """Start a session. Every run is a new session; nothing is overwritten."""
    try:
        sid = run_svc.start_run(body.model_dump(), _uname(current_user))
        return APIResponse(message=f"Session {sid} started", data={"session_id": sid})
    except Exception as e:
        _fail(e, "Run allocation")


@router.get("/run/{session_id}", response_model=APIResponse)
def run_get(session_id: str, current_user: User = Depends(get_current_user)):
    try:
        row = run_svc.get(session_id)
        if row is None:
            raise HTTPException(404, detail=f"Session {session_id} not found")
        return APIResponse(data=row)
    except HTTPException:
        raise
    except Exception as e:
        _fail(e, "Session status")


@router.post("/run/{session_id}/cancel", response_model=APIResponse)
def run_cancel(session_id: str, current_user: User = Depends(get_current_user)):
    ok = run_svc.cancel(session_id)
    return APIResponse(success=ok, message="Cancelling" if ok else "That session is not running")


# ── sessions ────────────────────────────────────────────────────────────────
@router.get("/sessions", response_model=APIResponse)
def sessions_list(limit: int = 50, current_user: User = Depends(get_current_user)):
    """Recent sessions; `pend` = Pend ALC progress {sid: …} of the APPROVED ones."""
    try:
        sessions = run_svc.recent(min(max(limit, 1), 200))
        pend: Dict[str, Any] = {}
        approved = [s["SESSION_ID"] for s in sessions if s.get("STATUS") == "APPROVED"]
        if approved:
            try:
                pend = approve_svc.pend_progress(approved)
            except Exception:  # noqa: BLE001 — the list must still load
                logger.exception("[sz1] pend progress for the sessions list failed")
        return APIResponse(data={"sessions": sessions, "running": base_svc.running(), "pend": pend})
    except Exception as e:
        _fail(e, "Sessions")


@router.get("/sessions/{session_id}/lines", response_model=APIResponse)
def sessions_lines(session_id: str, store: Optional[str] = None, art: Optional[str] = None,
                   maj_cat: Optional[str] = None, only: Optional[str] = None, sort: str = "store",
                   page: int = 1, size: int = 50, current_user: User = Depends(get_current_user)):
    """only = alloc | unmet | cross; sort = store | alloc | unmet | seq."""
    try:
        return APIResponse(data=run_svc.lines(session_id, store, art, maj_cat, only, sort, page, size))
    except Exception as e:
        _fail(e, "Session lines")


@router.get("/sessions/{session_id}/export")
def sessions_export(session_id: str, only: Optional[str] = "alloc", current_user: User = Depends(get_current_user)):
    """The session's lines as CSV. only = alloc (default: lines that got stock) | all."""
    if run_svc.get(session_id, full=False) is None:
        raise HTTPException(404, detail=f"Session {session_id} not found")
    flt = None if only == "all" else "alloc"
    name = f"1SZ_ALC_{session_id}_{'all_lines' if flt is None else 'allocated'}.csv"
    return StreamingResponse(run_svc.export_csv(session_id, flt), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.delete("/sessions/{session_id}", response_model=APIResponse)
def sessions_delete(session_id: str, confirm: str = "", current_user: User = Depends(get_current_user)):
    """Delete one session and re-apply the balances. Requires confirm=DELETE."""
    if confirm != "DELETE":
        raise HTTPException(400, detail="Type DELETE to confirm")
    try:
        head = run_svc.get(session_id, full=False)
        if head is not None and head.get("STATUS") == "APPROVED":
            raise ValueError(f"Session {session_id} is approved into Pend ALC — revert it first.")
        res = run_svc.delete(session_id, _uname(current_user))
        return APIResponse(message=f"Session {session_id} deleted ({res['lines']:,} lines); balances re-applied",
                           data=res)
    except Exception as e:
        _fail(e, "Delete session")


# ── phase 3: sessions into Pend ALC (manual §J P3; sz1_approve_service) ────
# Approve / revert write ARS_PEND_ALC and move MSA + grid: they need
# ACT_1SZ_APPROVE (SUPER_ADMIN passes). The other session actions need
# PAGE_1SZ_SESSIONS; the read-only ones only a login.
class RemoveStoresReq(BaseModel):
    stores: List[str]


@router.get("/sessions/{session_id}/recheck", response_model=APIResponse)
def sessions_recheck(session_id: str, current_user: User = Depends(get_current_user)):
    """Is the parked session still valid against live free stock and live store
    room? blocks / warnings / short_stock / short_budget / totals. Read only."""
    try:
        return APIResponse(data=approve_svc.recheck(session_id))
    except Exception as e:
        _fail(e, "Re-check session")


@router.get("/sessions/{session_id}/pend-preview", response_model=APIResponse)
def sessions_pend_preview(session_id: str, top: int = 100, current_user: User = Depends(get_current_user)):
    """The ARS_PEND_ALC rows an approve would write (FRESH / GRT split). Read only."""
    try:
        return APIResponse(data=approve_svc.pend_rows_preview(session_id, min(max(top, 1), 1000)))
    except Exception as e:
        _fail(e, "Pend ALC preview")


@router.post("/sessions/{session_id}/approve", response_model=APIResponse)
def sessions_approve(session_id: str, current_user: User = Depends(RequirePermissions(["ACT_1SZ_APPROVE"]))):
    """Re-check, then write the session into ARS_PEND_ALC (SOURCE / ALLOC_MODE '1SZ'),
    move MSA + grid, log APPROVE_1SZ — one transaction."""
    try:
        res = approve_svc.approve(session_id, _uname(current_user))
        return APIResponse(message=f"Session {session_id} approved: {res['rows']:,} pending rows, "
                                   f"{res['units']:,.0f} units (operation #{res['op_id']})", data=res)
    except Exception as e:
        _fail(e, "Approve session")


@router.post("/sessions/{session_id}/revert", response_model=APIResponse)
def sessions_revert(session_id: str, current_user: User = Depends(RequirePermissions(["ACT_1SZ_APPROVE"]))):
    """Undo the approval through Pend ALC (refused once BDC / DO touched a row);
    the session is PARKED again."""
    try:
        res = approve_svc.revert(session_id, _uname(current_user))
        return APIResponse(message=f"Approval of session {session_id} reverted; it is parked again", data=res)
    except Exception as e:
        _fail(e, "Revert approval")


@router.post("/sessions/{session_id}/discard", response_model=APIResponse)
def sessions_discard(session_id: str, current_user: User = Depends(RequirePermissions(["PAGE_1SZ_SESSIONS"]))):
    """PARKED → DISCARDED; lines kept for audit, stock and room back in the pool."""
    try:
        res = approve_svc.discard(session_id, _uname(current_user))
        return APIResponse(message=f"Session {session_id} discarded", data=res)
    except Exception as e:
        _fail(e, "Discard session")


@router.post("/sessions/{session_id}/refit", response_model=APIResponse)
def sessions_refit(session_id: str, current_user: User = Depends(RequirePermissions(["PAGE_1SZ_SESSIONS"]))):
    """Discard the session and start a new run with its stores and options on
    today's stock. Returns the new session id (poll GET /sz1/run/{id})."""
    try:
        res = approve_svc.refit(session_id, _uname(current_user))
        return APIResponse(message=f"Session {session_id} re-fitted: new session {res['session_id']} started",
                           data=res)
    except Exception as e:
        _fail(e, "Re-fit session")


@router.post("/sessions/{session_id}/remove-stores", response_model=APIResponse)
def sessions_remove_stores(session_id: str, body: RemoveStoresReq,
                           current_user: User = Depends(RequirePermissions(["PAGE_1SZ_SESSIONS"]))):
    """Drop those stores' lines from a parked session; totals, checks and summary recomputed."""
    try:
        res = approve_svc.remove_stores(session_id, body.stores, _uname(current_user))
        return APIResponse(message=f"Removed {len(res['removed'])} store(s) from session {session_id} "
                                   f"({res['lines_removed']:,} lines, {res['units_removed']:,.0f} units)", data=res)
    except Exception as e:
        _fail(e, "Remove stores")


@router.get("/sessions/{session_id}/pend", response_model=APIResponse)
def sessions_pend(session_id: str, current_user: User = Depends(get_current_user)):
    """Pend ALC progress of an approved session: rows, alloc / BDC / DO / pending qty,
    closed vs open rows; shipped = every row closed. Read only."""
    try:
        return APIResponse(data=approve_svc.pend_progress([session_id]).get(session_id))
    except Exception as e:
        _fail(e, "Pend ALC progress")
