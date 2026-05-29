"""
Listing Module — Build ARS_LISTING master table (Data Preparation)
Combines MSA gen-art data with grid stock data and store-RDC mapping.
Includes BOTH MSA-recommended gen-arts AND existing grid gen-arts.

RDC Modes:
  All       — all stores, all RDC options
  Own RDC   — stores tagged to selected RDC, unique options from that RDC only
  Cross RDC — take options FROM one RDC, send TO stores of another RDC

Endpoints:
  GET  /listing/config       — RDCs (from ST_MASTER), stores, MAJ_CATs, table status
  POST /listing/generate     — Build ARS_LISTING (MSA + grid unique options)
  GET  /listing/preview      — Preview with column filters & pagination
  GET  /listing/summary      — Summary stats
  GET  /listing/export       — Export to Excel
"""
import io
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Any, Dict, Optional, List
from sqlalchemy import text
from loguru import logger

from app.database.session import get_data_engine
from app.security.dependencies import get_current_user
from app.models.rbac import User
from app.utils.db_helpers import (
    run_sql, table_exists, get_columns, msa_expr, msa_col,
)

router = APIRouter(prefix="/listing", tags=["Listing"])

LISTING_TABLE = "ARS_LISTING"
FINAL_TABLE   = "ARS_LISTING_WORKING"
ALLOC_TABLE   = "ARS_ALLOC_WORKING"

# Columns to KEEP in the final table (identity + calculated outputs).
# Everything else (SLOC stock columns, Part 4 grid-prefix columns) is skipped.
_FINAL_KEEP_COLS = {
    "WERKS", "RDC", "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "GEN_ART_DESC",
    "STK_TTL", "IS_NEW", "OPT_TYPE",
    "ACS_D", "ALC_D", "AUTO_GEN_ART_SALE", "AGE",
    "LISTING", "I_ROD", "CLR_MIN", "CLR_MAX",
    "FOCUS_W_CAP", "FOCUS_WO_CAP",
    "RL_HOLD_QTY", "MSA_FNL_Q", "VAR_COUNT", "VAR_FNL_COUNT",
    "PER_OPT_SALE", "OPT_MBQ", "OPT_REQ", "OPT_MBQ_WH", "OPT_REQ_WH", "EXCESS_STK",
    "ST_RANK", "MAX_DAILY_SALE",
    "FINAL_OPT_TYPE", "ALLOC_BATCH_ID", "ALLOC_TYPE",
    "OPT_TYPE_REASON", "FOCUS_FLAG", "CLR_CAP_MODE", "STR_BOOST_PCT",
    # MAJ_CAT-level store aggregates — used by the MBQ cap in Stage C
    "MJ_MBQ", "MJ_STK_TTL",
    # Hierarchy category values (primary-grid keys — needed by rule_engine
    # revalidation to deduct grid REQ at correct grain)
    "M_VND_CD", "RNG_SEG", "MACRO_MVGR", "MICRO_MVGR", "FAB",
}
# Pattern: columns ending with _REQ are always kept (MJ_REQ, RNG_SEG_REQ, etc.)
_FINAL_KEEP_SUFFIX = {"_REQ"}


# ── Models ───────────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    rdc_mode: str = "all"              # "own" | "cross" | "all"
    rdc_values: List[str] = []         # Own RDC: selected RDC(s)
    cross_from: List[str] = []         # Cross RDC: take options FROM these RDCs
    cross_to: List[str] = []           # Cross RDC: send TO stores of these RDCs
    store_codes: List[str] = []        # selected stores (empty = all active)
    maj_cat_values: List[str] = []     # selected MAJ_CATs (empty = all)
    run_mode: str = "listing"          # "listing" | "full" (full = MSA+Grid+Listing)
    # MIX aggregation mode:
    #   "st_maj_rng" = 1 line per (WERKS, MAJ_CAT, RNG_SEG) — DEFAULT (finer)
    #   "st_maj"     = 1 line per (WERKS, MAJ_CAT)          — coarser, rolls everything together
    #   "each"       = keep each MIX row as-is (only tag, no aggregation)
    mix_mode: str = "st_maj_rng"
    # Configurable variables (editable from UI):
    stock_threshold_pct: float = 0.6   # OPT_TYPE: RL when STK >= X% of ACS_D (default 60%)
    excess_multiplier: float = 2.0     # EXCESS: STK > X × OPT_MBQ is excess (default 2×)
    hold_days: int = 0                 # OPT_MBQ_WH: extra days added to ALC_D for OPT_TYPE='TBL' only
    age_threshold: int = 15            # Articles with AGE < X use PER_OPT_SALE in OPT_MBQ
    req_weight: float = 0.4            # Store ranking: weight for requirement rank
    fill_weight: float = 0.6           # Store ranking: weight for fill rate rank
    # Allocation:
    # Secondary-grid dispatch cap toggle.
    # When True (default), main pass enforces cap = SEC_CAP_DEFAULT_PCT% (130)
    # on every grid where grid_group='Secondary' in ARS_GRID_BUILDER.
    apply_sec_cap_in_normal: bool = True
    default_acs_d: float = 18.0        # Default ACS_D when NULL/0 (used in OPT_TYPE fallback classification)
    min_size_count: int = 3            # Min sizes required for TBL listing (alternative to 60% ratio)
    # PRI_CT% >= 100 gate (R06 + revalidation SKIP_PRI_BROKEN). TBL always enforces.
    # When False, the opt_type is allowed in even if primary grid coverage is < 100%
    # (and the boosted MBQ-cap path is activated instead). Default False — matches
    # the frontend toggle's default state so missing-field requests don't silently
    # flip behavior to strict-gate.
    pri_ct_check_rl: bool = False
    pri_ct_check_tbc: bool = False
    # Per-OPT_TYPE MJ_REQ downward cap. SUM(SHIP_QTY) for each OPT_TYPE per
    # (WERKS, MAJ_CAT) is clamped to cap_pct% × MJ_REQ. 100 = hard ceiling at
    # MAJ_CAT requirement; 0 = cap disabled. Defaults 100/100/100 ensure no
    # over-allocation vs MJ_REQ out of the box. Independent of MBQ caps above —
    # MBQ caps trim against MJ_MBQ, these trim against MJ_REQ (= MJ_MBQ − MJ_STK_TTL).
    rl_mj_req_cap_pct:  float = 100.0
    tbc_mj_req_cap_pct: float = 100.0
    tbl_mj_req_cap_pct: float = 100.0
    # MJ_MBQ growth headroom (Allocation Gate).  100 = strict (waterfall stops
    # at the MAJ_CAT target, current default).  >100 scales MJ_MBQ to a
    # SIBLING column MJ_MBQ_REV — the original MJ_MBQ is preserved untouched —
    # and MJ_REQ_REV is re-derived as MAX(0, MJ_MBQ_REV − MJ_STK_TTL).  When
    # >100, MJ_REQ is then promoted to MJ_REQ_REV so every downstream engine
    # consumer (revalidate, OPT_MJ_REQ gate, store-broken pre-band, post-
    # waterfall MJ_REQ_REM recompute) reads the scaled ceiling with no math
    # change.  Original MJ_REQ value is kept in MJ_REQ_ORIG for audit.
    mj_req_growth_pct: float = 100.0
    # Allocation mode. pandas = multi-process per MAJ_CAT (fast). sequential = single-thread fallback.
    allocation_mode:  str = "pandas"  # "sequential" | "pandas"
    parallel_workers: int = 8        # used only by pandas mode
    # Per-run override for the single-writer-queue path. None → use .env default
    # (settings.USE_WRITER_QUEUE). True/False → force on/off for this run only.
    # Pandas mode only — sequential mode has no writers to coordinate.
    use_writer_queue: Optional[bool] = None
    # Source tables:
    msa_table: str = "ARS_MSA_GEN_ART"
    grid_table: str = "ARS_GRID_MJ_GEN_ART"
    st_master_table: str = "Master_ALC_INPUT_ST_MASTER"
    ssn_values: List[str] = []  # restrict run to MAJ_CATs whose articles belong to selected seasons
    opt_types: List[str] = ["RL", "TBC", "TBL"]  # which OPT_TYPEs the waterfall runs (subset to skip types)
    # Parking mode. False (default) = single-parked: a new run is blocked
    # while a parked session is awaiting approve/reject. True = multi-parked:
    # the pending-parked guard is bypassed so several parked snapshots can
    # coexist and be reviewed independently from the Parked Runs page.
    allow_multi_parked: bool = False


# ── Helpers — delegating to shared db_helpers ───────────────────────────────

_run = run_sql
_table_exists = table_exists
_get_columns = get_columns
_msa_expr = msa_expr
_msa_col = msa_col


def _safe_order(cols, table):
    """
    Build ORDER BY that only references columns that exist in the table.

    For working / alloc: ST_RANK -> MAJ_CAT -> OPT_TYPE (RL→TBC→TBL) ->
    OPT_PRIORITY_RANK -> WERKS. ST_RANK is the per-MAJ_CAT priority rank
    of the store, so leading with it surfaces the top-priority stores
    first across categories. WERKS is a late tie-breaker.
    """
    cu = {c.upper() for c in cols}
    if table in ("working", "alloc"):
        parts = []
        if "ST_RANK" in cu:
            parts.append("ISNULL([ST_RANK], 999999) ASC")
        if "MAJ_CAT" in cu:
            parts.append("[MAJ_CAT]")
        if "OPT_TYPE" in cu:
            parts.append(
                "CASE [OPT_TYPE] WHEN 'RL' THEN 1 WHEN 'TBC' THEN 2 "
                "WHEN 'TBL' THEN 3 ELSE 4 END"
            )
        if "OPT_PRIORITY_RANK" in cu:
            parts.append("ISNULL([OPT_PRIORITY_RANK], 999999) ASC")
        for c in ["WERKS", "GEN_ART_NUMBER", "CLR"]:
            if c in cu:
                parts.append(f"[{c}]")
        if table == "alloc" and "SZ" in cu:
            parts.append("[SZ]")
        return ", ".join(parts) if parts else "1"
    else:
        parts = []
        for c in ["WERKS", "MAJ_CAT", "GEN_ART_NUMBER", "CLR"]:
            if c in cu:
                parts.append(f"[{c}]")
        return ", ".join(parts) if parts else "1"


def _build_filter_where(filters_json, valid_cols, existing_where_parts=None):
    """Parse column filters JSON and build WHERE clauses."""
    where = list(existing_where_parts or [])
    params = {}
    if not filters_json:
        return where, params
    try:
        filters = json.loads(filters_json)
    except Exception:
        return where, params
    for col, val in filters.items():
        if col in valid_cols and val:
            safe_key = col.replace(" ", "_").replace("-", "_")
            where.append(f"CAST([{col}] AS NVARCHAR(MAX)) LIKE :f_{safe_key}")
            params[f"f_{safe_key}"] = f"%{val}%"
    return where, params


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/config")
def get_config(current_user: User = Depends(get_current_user)):
    """Return available RDCs (from ST_MASTER), stores, MAJ_CATs, and table status."""
    de = get_data_engine()
    result = {"rdcs": [], "stores": [], "maj_cats": [], "store_count": 0,
              "msa_gen_art_rows": 0, "grid_gen_art_rows": 0,
              "listing_rows": 0, "listing_exists": False}

    with de.connect() as conn:
        if _table_exists(conn, "Master_ALC_INPUT_ST_MASTER"):
            st_cols = _get_columns(conn, "Master_ALC_INPUT_ST_MASTER")
            st_rdc_col = None
            for candidate in ["RDC", "WAREHOUSE", "HUB", "WH_CD"]:
                if candidate in st_cols:
                    st_rdc_col = candidate
                    break

            has_listing_col = "LISTING" in st_cols
            listing_filter = ""
            if has_listing_col:
                listing_filter = " WHERE ISNULL(CAST([LISTING] AS NVARCHAR(10)), '1') NOT IN ('0', 'N', 'n')"

            if st_rdc_col:
                rdcs = conn.execute(text(
                    f"SELECT DISTINCT [{st_rdc_col}] FROM [Master_ALC_INPUT_ST_MASTER] "
                    f"WHERE [{st_rdc_col}] IS NOT NULL ORDER BY [{st_rdc_col}]"
                )).fetchall()
                result["rdcs"] = [str(r[0]).strip() for r in rdcs if r[0]]

            result["store_count"] = conn.execute(text(
                f"SELECT COUNT(DISTINCT [ST_CD]) FROM [Master_ALC_INPUT_ST_MASTER]{listing_filter}"
            )).scalar()

            stores = conn.execute(text(
                f"SELECT DISTINCT [ST_CD] FROM [Master_ALC_INPUT_ST_MASTER]{listing_filter} ORDER BY [ST_CD]"
            )).fetchall()
            result["stores"] = [str(r[0]).strip() for r in stores if r[0]]

            # Store → RDC mapping (for auto RDC detection in frontend)
            if st_rdc_col:
                store_rdc_rows = conn.execute(text(
                    f"SELECT DISTINCT [ST_CD], [{st_rdc_col}] FROM [Master_ALC_INPUT_ST_MASTER]{listing_filter}"
                )).fetchall()
                result["store_rdc_map"] = {str(r[0]).strip(): str(r[1]).strip() for r in store_rdc_rows if r[0] and r[1]}

        if _table_exists(conn, "ARS_MSA_GEN_ART"):
            result["msa_gen_art_rows"] = conn.execute(text(
                "SELECT COUNT(*) FROM [ARS_MSA_GEN_ART]"
            )).scalar()
            maj_cats = conn.execute(text(
                "SELECT DISTINCT [MAJ_CAT] FROM [ARS_MSA_GEN_ART] "
                "WHERE [MAJ_CAT] IS NOT NULL ORDER BY [MAJ_CAT]"
            )).fetchall()
            result["maj_cats"] = [str(r[0]).strip() for r in maj_cats if r[0]]

        if _table_exists(conn, "ARS_GRID_MJ_GEN_ART"):
            result["grid_gen_art_rows"] = conn.execute(text(
                "SELECT COUNT(*) FROM [ARS_GRID_MJ_GEN_ART]"
            )).scalar()

        if _table_exists(conn, LISTING_TABLE):
            result["listing_exists"] = True
            result["listing_rows"] = conn.execute(text(
                f"SELECT COUNT(*) FROM [{LISTING_TABLE}]"
            )).scalar()

        # Load saved listing variables from AppSettings
        result["settings"] = _load_listing_settings(conn)

        # Distinct seasons for SSN filter
        try:
            ssn_rows = conn.execute(text(
                "SELECT DISTINCT [SSN] FROM [vw_master_product] WITH (NOLOCK) "
                "WHERE [SSN] IS NOT NULL ORDER BY [SSN]"
            )).scalars().all()
            result["ssns"] = [str(r).strip() for r in ssn_rows if r]
        except Exception:
            result["ssns"] = []

    return {"success": True, "data": result}


# ── Listing Settings (persisted in AppSettings table) ──────────────────────

_SETTING_DEFAULTS = {
    "stock_threshold_pct": "0.6",
    "excess_multiplier": "2.0",
    "hold_days": "0",
    "age_threshold": "15",
    "mix_mode": "st_maj_rng",
    "rdc_mode": "all",
    "run_mode": "listing",
    "req_weight": "0.4",
    "fill_weight": "0.6",
    "apply_sec_cap_in_normal": "true",
    "default_acs_d": "18",
    "min_size_count": "3",
    "pri_ct_check_rl": "false",
    "pri_ct_check_tbc": "false",
    "rl_mj_req_cap_pct": "100.0",
    "tbc_mj_req_cap_pct": "100.0",
    "tbl_mj_req_cap_pct": "100.0",
    "mj_req_growth_pct": "100.0",
    "allow_multi_parked": "false",
}
_SETTING_PREFIX = "listing."


def _load_listing_settings(conn) -> dict:
    """Load listing_* keys from AppSettings, return as dict with defaults."""
    settings = dict(_SETTING_DEFAULTS)
    if not table_exists(conn, "AppSettings"):
        return settings
    rows = conn.execute(text(
        "SELECT setting_key, setting_value FROM AppSettings WHERE setting_key LIKE :pfx"
    ), {"pfx": f"{_SETTING_PREFIX}%"}).fetchall()
    for key, val in rows:
        short = key.replace(_SETTING_PREFIX, "", 1)
        if short in settings:
            settings[short] = val
    return settings


def _save_listing_settings(conn, settings: dict):
    """Upsert listing_* keys into AppSettings."""
    if not table_exists(conn, "AppSettings"):
        return
    for key, val in settings.items():
        if key not in _SETTING_DEFAULTS:
            continue
        full_key = f"{_SETTING_PREFIX}{key}"
        existing = conn.execute(text(
            "SELECT COUNT(*) FROM AppSettings WHERE setting_key = :k"
        ), {"k": full_key}).scalar()
        if existing:
            conn.execute(text(
                "UPDATE AppSettings SET setting_value = :v, updated_at = GETDATE() WHERE setting_key = :k"
            ), {"k": full_key, "v": str(val)})
        else:
            conn.execute(text(
                "INSERT INTO AppSettings (setting_key, setting_value, updated_at) VALUES (:k, :v, GETDATE())"
            ), {"k": full_key, "v": str(val)})
    conn.commit()


@router.post("/settings")
def save_listing_settings(body: dict, current_user: User = Depends(get_current_user)):
    """Save listing variables to AppSettings for persistence."""
    de = get_data_engine()
    with de.connect() as conn:
        _save_listing_settings(conn, body)
    return {"success": True, "data": body}


@router.post("/generate")
def generate_listing(req: GenerateRequest, current_user: User = Depends(get_current_user)):
    """Build ARS_LISTING = Grid data + MSA missing options.

    run_mode: "listing" = generate listing only, "full" = MSA calc → Grid build → Listing

    Runs ASYNCHRONOUSLY: a background thread does the actual work; this
    endpoint returns within milliseconds so reverse proxies (Cloudflare's
    100s edge timeout in particular) never see a hung connection. The UI
    follows progress via:
      - GET /listing/sessions/{session_id}      (overall status)
      - GET /listing/alloc-progress?batch_id=…  (per-MAJ_CAT, parallel modes)
    """
    import threading

    from app.services.listing_sessions import (
        make_session_id, start_session,
    )
    from app.services import parked_history

    # Refuse a second concurrent run — listing tables (ARS_ALLOC_WORKING in
    # particular) are dropped+recreated each run, so two overlapping calls
    # can race on DDL and corrupt each other's snapshot.
    if parked_history.has_running_session():
        raise HTTPException(
            409,
            "Another listing run is already in progress. Wait for it to finish "
            "or kill it from the Sessions page before starting a new one."
        )

    # Enforce one-parked-at-a-time: block new runs while a session is
    # awaiting approve/reject. Running a new listing would overwrite
    # ARS_ALLOC_WORKING and ARS_LISTING_WORKING, making the parked
    # snapshot unrecoverable.
    # allow_multi_parked=True opts out of this guard so users can stack
    # several parked snapshots for side-by-side review.
    if not getattr(req, "allow_multi_parked", False) and parked_history.has_pending_parked():
        raise HTTPException(
            409,
            "A parked session is awaiting review. Please approve or reject it "
            "from the Parked Runs page before generating a new one, or enable "
            "'Allow multiple parked' to stack snapshots."
        )

    session_id = make_session_id()
    user_name  = getattr(current_user, "username", None)
    req_dict   = req.dict() if hasattr(req, "dict") else dict(req.__dict__)
    mode       = (req_dict.get("allocation_mode") or "python_parallel").lower()
    # For parallel modes the batch_id == session_id so the UI can poll
    # /alloc-progress with the same id it gets back here, and the queue
    # table rows line up with the session row.
    alloc_batch_id = session_id if mode != "sequential" else None

    # Insert the RUNNING row + attach the per-session loguru sink BEFORE
    # the thread starts so the very first log line ('=== SESSION START …')
    # lands in the file and the UI can show the session immediately.
    start_session(session_id, user_name, req_dict)

    # Fire the actual work in a daemon thread. SQLAlchemy connections are
    # thread-safe (each thread checks out its own from the pool), so this
    # is safe. The thread terminates naturally when the work finishes.
    threading.Thread(
        target=_run_generate_in_thread,
        args=(req_dict, user_name, session_id, alloc_batch_id),
        daemon=True,
        name=f"listing-gen-{session_id}",
    ).start()

    # Return immediately — UI takes over via polling.
    return {
        "success": True,
        "message": (f"Listing generation started in background "
                    f"(mode={mode}, session={session_id}). "
                    f"Watch the progress panel below."),
        "data": {
            "session_id":      session_id,
            "alloc_batch_id":  alloc_batch_id,
            "allocation_mode": mode,
            "parallel_workers": req_dict.get("parallel_workers"),
            "status":          "RUNNING",
        },
    }


def _run_generate_in_thread(req_dict: dict, user_name, session_id: str,
                              alloc_batch_id: Optional[str]):
    """
    Background-thread entry point. Reconstructs GenerateRequest from the
    dict (FastAPI request objects can't cross thread boundaries safely)
    and runs the existing _generate_listing_impl, then closes the session.
    """
    from app.services.listing_sessions import end_session
    summary: dict = {}
    try:
        with logger.contextualize(session_id=session_id):
            try:
                # Rebuild the Pydantic model from the dict snapshot so the
                # impl sees req.* the same way as before.
                req = GenerateRequest(**req_dict)
                _generate_listing_impl(
                    req,
                    current_user=None,           # not used inside the impl
                    session_id=session_id,
                    summary=summary,
                    preset_batch_id=alloc_batch_id,
                )
            except Exception as e:
                summary["error"] = str(e)
                logger.exception(f"[generate] background thread failed: {e}")
    finally:
        # If the run errored out and we'd reserved a batch_id, mark any
        # PENDING/IN_PROGRESS rows for it as FAILED. Otherwise the queue
        # leaks an orphan and /listing/active-job keeps reporting it as
        # running forever.
        if summary.get("error") and alloc_batch_id:
            try:
                from app.services.alloc_queue import QUEUE_TABLE
                de = get_data_engine()
                with de.connect() as conn:
                    conn.execute(text(f"""
                        UPDATE {QUEUE_TABLE}
                           SET STATUS       = 'FAILED',
                               COMPLETED_AT = GETDATE(),
                               ERROR_MSG    = LEFT(ISNULL(ERROR_MSG, '') +
                                                   ' [generate-thread aborted]', 2000)
                         WHERE BATCH_ID = :b
                           AND STATUS IN ('PENDING','IN_PROGRESS')
                    """), {"b": alloc_batch_id})
                    conn.commit()
            except Exception:
                logger.warning("[generate] cleanup of orphan queue rows failed",
                               exc_info=True)
        try:
            end_session(
                session_id,
                "FAILED" if summary.get("error") else "SUCCESS",
                summary,
            )
        except Exception:
            pass
        # Drop any cancel-event / SPID registry entries we accumulated.
        try:
            from app.services import alloc_cancellation as ac
            ac.cleanup(alloc_batch_id or session_id)
        except Exception:
            pass


def _generate_listing_impl(req: GenerateRequest, current_user, session_id: str,
                            summary: dict, preset_batch_id: Optional[str] = None):
    """Original /generate body — unchanged behaviour, just hoisted into a
    helper so the public endpoint can wrap it with session capture.

    preset_batch_id: if set, parallel orchestrators reuse this id (so it
    matches the session_id returned to the UI). Sequential mode ignores it.
    """
    from app.services import parked_history

    start = time.time()
    de = get_data_engine()

    # Snapshot which tracked tables existed before the run started, so the
    # post-run sweep can label each one CREATED vs. RECREATED/TRUNCATED.
    pre_existence = parked_history.capture_pre_existence()

    # Cancel-check helper. Stage A/B can run for many minutes; without these
    # checkpoints the thread keeps grinding even after kill_session sets the
    # cancel event. We probe between heavy stages so the worst case is one
    # in-flight statement (which kill_session will KILL on the SPID anyway).
    # NOTE: do NOT alias this module to `ac` — several `with de.connect() as ac:`
    # blocks later in this function would shadow it and break _check_cancel.
    from app.services import alloc_cancellation as _cancel_svc
    _cancel_key = preset_batch_id or session_id

    def _check_cancel(stage: str = "") -> None:
        # Robust check: in-memory event OR DB session STATUS='CANCELLED'.
        # The DB fallback survives cleanup() of the in-memory event and
        # works for ProcessPool subprocesses that can't see the parent's
        # _CANCEL_EVENTS dict at all.
        cancelled = _cancel_svc.is_cancelled_anywhere(_cancel_key) \
            if _cancel_key else False
        # Always also probe the session row directly (covers the case
        # where the cancel arrived via /sessions/{sid}/kill before any
        # batch_id was minted).
        if not cancelled and session_id:
            cancelled = _cancel_svc.is_session_cancelled(session_id)
        if cancelled:
            logger.warning(f"[generate] cancel detected at stage={stage} — aborting")
            raise InterruptedError(f"cancelled by user (stage={stage})")

    # Auto-save current variables to DB for next session.
    # Was previously wrapped in `except: pass` — that swallowed save failures
    # and made toggle changes appear to "not stick" between sessions. Now logs
    # the error so the failure is visible in API logs.
    try:
        with de.connect() as sc:
            _save_listing_settings(sc, {
                "stock_threshold_pct": str(req.stock_threshold_pct),
                "excess_multiplier": str(req.excess_multiplier),
                "hold_days": str(req.hold_days),
                "age_threshold": str(req.age_threshold),
                "mix_mode": req.mix_mode,
                "rdc_mode": req.rdc_mode,
                "run_mode": req.run_mode,
                "req_weight": str(req.req_weight),
                "fill_weight": str(req.fill_weight),
                "apply_sec_cap_in_normal": str(req.apply_sec_cap_in_normal).lower(),
                "default_acs_d": str(req.default_acs_d),
                "min_size_count": str(req.min_size_count),
                "pri_ct_check_rl": str(req.pri_ct_check_rl).lower(),
                "pri_ct_check_tbc": str(req.pri_ct_check_tbc).lower(),
                "rl_mj_req_cap_pct": str(req.rl_mj_req_cap_pct),
                "tbc_mj_req_cap_pct": str(req.tbc_mj_req_cap_pct),
                "tbl_mj_req_cap_pct": str(req.tbl_mj_req_cap_pct),
                "mj_req_growth_pct": str(req.mj_req_growth_pct),
                "allow_multi_parked": str(req.allow_multi_parked).lower(),
            })
    except Exception as e:
        logger.error(f"[generate] failed to persist listing settings: {e}")

    # ── Full pipeline: MSA calc → Grid build → Listing ──────────────
    pipeline_msg = ""
    if req.run_mode == "full":
        try:
            from app.services.grid_calculations import calculate_per_day_sale
            from app.api.v1.endpoints.grid_builder import _build_and_run_grid
            from concurrent.futures import ThreadPoolExecutor

            # Step A: Pre-grid calculations
            with de.connect() as pc:
                calc_result = calculate_per_day_sale(pc)
                logger.info(f"Full pipeline: pre-grid calc done")

            # Step B: Run all active grids in parallel
            with de.connect() as gc:
                if _table_exists(gc, "ARS_GRID_BUILDER"):
                    grids = gc.execute(text(
                        "SELECT * FROM [ARS_GRID_BUILDER] WHERE UPPER(status)='ACTIVE' ORDER BY seq"
                    )).fetchall()
                    grid_cols_meta = [d[0] for d in gc.execute(text(
                        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='ARS_GRID_BUILDER' ORDER BY ORDINAL_POSITION"
                    )).fetchall()]
                    grid_dicts = [dict(zip(grid_cols_meta, g)) for g in grids]

                    def run_grid(g):
                        return _build_and_run_grid(de, g)

                    with ThreadPoolExecutor(max_workers=4) as pool:
                        results = list(pool.map(run_grid, grid_dicts))
                    logger.info(f"Full pipeline: {len(results)} grids completed")
                    pipeline_msg = f"Full pipeline: calc + {len(results)} grids | "
        except Exception as e:
            logger.error(f"Full pipeline error: {e}")
            pipeline_msg = f"Pipeline partial (error: {str(e)[:80]}) | "

    # Step timing collector — also probes the cancel flag at every step
    # boundary so a Stage A/B run aborts at the next gap when the user
    # clicks Stop, even before any in-flight statement is KILL'd.
    step_timings = []
    def _time_step(label, t0):
        dt = round(time.time() - t0, 1)
        step_timings.append({"step": label, "seconds": dt})
        logger.info(f"⏱ {label}: {dt}s")
        _check_cancel(label)
        return time.time()

    with de.connect() as conn:
        # Register this long-lived connection's SPID with the cancel registry
        # so kill_session(session_id) issues KILL on the in-flight statement
        # (Stage A/B INSERTs can run for minutes — without this, KILL has no
        # SPID to target and the Python thread keeps grinding). The SPID
        # becomes invalid when the connection closes at the end of this
        # with-block; cleanup(batch_id) at the end of the run scrubs it.
        _stage_ab_spid = _cancel_svc.get_current_spid(conn)
        if _cancel_key and _stage_ab_spid:
            _cancel_svc.register_spid(_cancel_key, _stage_ab_spid)
            logger.info(f"[generate] Stage A/B spid={_stage_ab_spid} registered for cancel")
        _check_cancel("stage_ab_start")

        for tbl in [req.msa_table, req.grid_table, req.st_master_table]:
            if not _table_exists(conn, tbl):
                raise HTTPException(400, f"Table '{tbl}' not found")

        msa_cols = _get_columns(conn, req.msa_table)
        grid_cols = _get_columns(conn, req.grid_table)
        st_cols = _get_columns(conn, req.st_master_table)

        msa_rdc_col = "RDC" if "RDC" in msa_cols else "ST_CD"
        if not all(c in msa_cols for c in ["MAJ_CAT", "GEN_ART_NUMBER", "CLR"]):
            raise HTTPException(400, "MSA table missing MAJ_CAT, GEN_ART_NUMBER, CLR")
        if not all(c in grid_cols for c in ["WERKS", "MAJ_CAT", "GEN_ART_NUMBER", "CLR"]):
            raise HTTPException(400, "Grid table missing WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR")

        # SLOC columns = pivot data only (exclude system/calc cols + sale cols)
        # Sale columns (L-7 DAYS SALE-Q etc.) are NOT stock — must not be summed into STK_TTL.
        skip_cols = {"WERKS", "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "STK_TTL", "STR", "IS_NEW",
                     "CONT", "MBQ", "OPT_CNT", "LISTING",
                     "ACS_D", "ALC_D", "DPN", "SAL_D", "SAL_PD", "DISP_Q", "DISP_GR_DGR",
                     "LW_ACT_SL_GR_DGR", "BGT_SL_GR_DGR", "MANUAL_DENSITY"}
        stock_cols = [c for c in grid_cols if c not in skip_cols]
        # Separate sale columns (contain "SALE" or "L-7" in name) — carried but NOT summed
        sale_cols = [c for c in stock_cols if "SALE" in c.upper() or "L-7" in c.upper() or "L_7" in c.upper()]
        # STK_TTL columns = stock_cols minus sale columns
        stk_sum_cols = [c for c in stock_cols if c not in sale_cols]

        # ST_MASTER RDC column
        st_rdc_col = None
        for c in ["RDC", "WAREHOUSE", "HUB", "WH_CD"]:
            if c in st_cols:
                st_rdc_col = c
                break
        if not st_rdc_col:
            raise HTTPException(400, "ST_MASTER missing RDC column")

        # ── Filters ─────────────────────────────────────────────────────
        # When SSN is selected but no explicit MAJ_CAT, resolve SSN→MAJ_CATs
        # once here so every downstream query benefits (grid, MSA, var, etc.).
        _effective_majcats: List[str] = list(req.maj_cat_values or [])
        if not _effective_majcats and req.ssn_values:
            try:
                _ssn_safe = ', '.join(
                    chr(39) + v.replace(chr(39), chr(39)*2) + chr(39)
                    for v in req.ssn_values
                )
                _effective_majcats = [
                    str(r[0]) for r in conn.execute(text(
                        f"SELECT DISTINCT [MAJ_CAT] FROM [vw_master_product] WITH (NOLOCK) "
                        f"WHERE [SSN] IN ({_ssn_safe}) AND [MAJ_CAT] IS NOT NULL "
                        f"ORDER BY [MAJ_CAT]"
                    )) if r[0]
                ]
                if _effective_majcats:
                    logger.info(
                        f"SSN filter {req.ssn_values} → {len(_effective_majcats)} MAJ_CATs: "
                        f"{_effective_majcats[:5]}{'...' if len(_effective_majcats) > 5 else ''}"
                    )
            except Exception as _ssn_err:
                logger.warning(f"SSN→MAJ_CAT resolution failed: {_ssn_err}")

        mc_where = ""
        if _effective_majcats:
            mc_list = ", ".join(f"'{v}'" for v in _effective_majcats)
            mc_where = f" AND [MAJ_CAT] IN ({mc_list})"

        # SSN filter — keep subquery fallback for grid/MSA alias forms
        if req.ssn_values:
            _ssn_list = ', '.join(
                chr(39) + v.replace(chr(39), chr(39)*2) + chr(39)
                for v in req.ssn_values
            )
            _mp_subq = (
                f"SELECT DISTINCT [MAJ_CAT] FROM [vw_master_product] WITH (NOLOCK) "
                f"WHERE [SSN] IN ({_ssn_list})"
            )
            mp_majcat_filter   = f" AND [MAJ_CAT] IN ({_mp_subq})"
            mp_majcat_filter_g = f" AND G.[MAJ_CAT] IN ({_mp_subq})"
            logger.info(f"SSN filter active: {req.ssn_values}")
        else:
            mp_majcat_filter   = ""
            mp_majcat_filter_g = ""

        # Active stores
        has_listing = "LISTING" in st_cols
        st_parts = []
        if has_listing:
            st_parts.append("ISNULL(CAST([LISTING] AS NVARCHAR(10)), '1') NOT IN ('0', 'N', 'n')")
        if req.store_codes:
            st_parts.append(f"[ST_CD] IN ({', '.join(f'{chr(39)}{v}{chr(39)}' for v in req.store_codes)})")

        # ── Stores SQL based on RDC mode ────────────────────────────────
        def _stores_sql(rdc_filter_list=None):
            parts = list(st_parts)
            if rdc_filter_list:
                rl = ", ".join(f"'{v}'" for v in rdc_filter_list)
                parts.append(f"[{st_rdc_col}] IN ({rl})")
            w = (" WHERE " + " AND ".join(parts)) if parts else ""
            return f"SELECT DISTINCT [ST_CD], [{st_rdc_col}] AS RDC FROM [{req.st_master_table}]{w}"

        # ── MSA option filter based on RDC mode ─────────────────────────
        # MSA stores all columns as VARCHAR(MAX) — must TRIM RDC for matching
        if req.rdc_mode == "own" and req.rdc_values:
            stores_sql = _stores_sql(req.rdc_values)
            rl = ", ".join(f"'{v}'" for v in req.rdc_values)
            msa_rdc_filter = f" AND LTRIM(RTRIM(CAST([{msa_rdc_col}] AS NVARCHAR(100)))) IN ({rl})"
        elif req.rdc_mode == "cross" and req.cross_from:
            stores_sql = _stores_sql(req.cross_to if req.cross_to else None)
            fl = ", ".join(f"'{v}'" for v in req.cross_from)
            msa_rdc_filter = f" AND LTRIM(RTRIM(CAST([{msa_rdc_col}] AS NVARCHAR(100)))) IN ({fl})"
        else:
            stores_sql = _stores_sql()
            msa_rdc_filter = ""

        # ── MSA unique options (proper types + RDC filtered) ──────────────
        msa_sql = f"""
            SELECT DISTINCT {_msa_col('MAJ_CAT')}, {_msa_col('GEN_ART_NUMBER')}, {_msa_col('CLR')}
            FROM [{req.msa_table}]
            WHERE [MAJ_CAT] IS NOT NULL AND [GEN_ART_NUMBER] IS NOT NULL{mc_where}{mp_majcat_filter}{msa_rdc_filter}
        """

        # ── Create listing table ────────────────────────────────────────
        _run(conn, f"IF OBJECT_ID('{LISTING_TABLE}','U') IS NOT NULL DROP TABLE [{LISTING_TABLE}]")

        stk_defs = ", ".join(f"[{c}] FLOAT NULL DEFAULT 0" for c in stock_cols)
        stk_defs_str = f", {stk_defs}" if stk_defs else ""
        _run(conn, f"""
            CREATE TABLE [{LISTING_TABLE}] (
                [WERKS] NVARCHAR(50),
                [RDC] NVARCHAR(50),
                [MAJ_CAT] NVARCHAR(100),
                [GEN_ART_NUMBER] BIGINT NULL,
                [CLR] NVARCHAR(100)
                {stk_defs_str},
                [STK_TTL] FLOAT NULL DEFAULT 0,
                [STR] FLOAT NULL DEFAULT 0,
                [IS_NEW] BIT NOT NULL DEFAULT 0,
                [OPT_TYPE] NVARCHAR(10) NULL
            )
        """)

        # ── SQL fragments ───────────────────────────────────────────────
        # All SLOC + sale columns are carried (SELECT + INSERT)
        stk_sel = ", ".join(f"ISNULL(G.[{c}], 0) AS [{c}]" for c in stock_cols)
        stk_sel_str = f", {stk_sel}" if stk_sel else ""
        # STK_TTL = sum of STOCK columns only (excludes sale columns like L-7 DAYS SALE-Q).
        # Negative totals (SLOC adjustments) are clamped to 0 — treated as "no stock",
        # never as a negative requirement offset that would inflate OPT_REQ/SZ_REQ.
        _stk_raw = " + ".join(f"ISNULL(G.[{c}], 0)" for c in stk_sum_cols) if stk_sum_cols else "0"
        stk_ttl  = f"CASE WHEN ({_stk_raw}) < 0 THEN 0 ELSE ({_stk_raw}) END"
        str_ttl  = " + ".join(f"ISNULL(G.[{c}], 0)" for c in sale_cols) if sale_cols else "0"
        stk_ins = ", ".join(f"[{c}]" for c in stock_cols)
        stk_ins_str = f", {stk_ins}" if stk_ins else ""
        stk_zeros = ", ".join("0" for _ in stock_cols)
        stk_zeros_str = f", {stk_zeros}" if stk_zeros else ""

        all_cols = f"[WERKS], [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR]{stk_ins_str}, [STK_TTL], [STR], [IS_NEW], [OPT_TYPE]"

        if sale_cols:
            logger.info(f"Sale columns excluded from STK_TTL sum: {sale_cols}")

        # ── Diagnostic: source counts ───────────────────────────────────
        diag_stores = conn.execute(text(f"SELECT COUNT(*) FROM ({stores_sql}) X")).scalar()
        grid_mc = f"AND [MAJ_CAT] IN ({', '.join(f'{chr(39)}{v}{chr(39)}' for v in _effective_majcats)})" if _effective_majcats else ""
        diag_grid = conn.execute(text(
            f"SELECT COUNT(*) FROM [{req.grid_table}] WITH (NOLOCK) "
            f"WHERE [WERKS] IN (SELECT [ST_CD] FROM ({stores_sql}) X) {grid_mc}"
        )).scalar()
        diag_msa = conn.execute(text(f"SELECT COUNT(*) FROM ({msa_sql}) X")).scalar()
        logger.info(f"Diagnostic: stores={diag_stores}, grid_rows={diag_grid}, msa_options={diag_msa}")

        # ── PART 1: Grid data (existing stock) → IS_NEW = 0 ────────────
        t0 = time.time()
        grid_mc_g = f"AND G.[MAJ_CAT] IN ({', '.join(f'{chr(39)}{v}{chr(39)}' for v in _effective_majcats)})" if _effective_majcats else ""

        _run(conn, f"""
            INSERT INTO [{LISTING_TABLE}] ({all_cols})
            SELECT
                G.[WERKS], S.[RDC],
                LTRIM(RTRIM(G.[MAJ_CAT])),
                TRY_CAST(G.[GEN_ART_NUMBER] AS BIGINT),
                LTRIM(RTRIM(G.[CLR]))
                {stk_sel_str}, {stk_ttl} AS STK_TTL, {str_ttl} AS STR, 0 AS IS_NEW, NULL AS OPT_TYPE
            FROM [{req.grid_table}] G WITH (NOLOCK)
            INNER JOIN ({stores_sql}) S ON G.[WERKS] = S.[ST_CD]
            WHERE 1=1 {grid_mc_g}{mp_majcat_filter_g}
        """)
        grid_count = conn.execute(text(f"SELECT COUNT(*) FROM [{LISTING_TABLE}]")).scalar()
        logger.info(f"Part 1 (Grid data): {grid_count} rows")
        t0 = _time_step("Part 1 (Grid data INSERT)", t0)

        # ── PART 2: MSA missing options → IS_NEW = 1 ───────────────────
        msa_rdc_join = ""
        if req.rdc_mode == "own":
            msa_rdc_join = f"AND M.[RDC] = S.[RDC]"
        # MSA base (with RDC column preserved for joining)
        msa_with_rdc = f"""
            SELECT DISTINCT
                LTRIM(RTRIM(CAST([{msa_rdc_col}] AS NVARCHAR(50)))) AS RDC,
                {_msa_col('MAJ_CAT')}, {_msa_col('GEN_ART_NUMBER')}, {_msa_col('CLR')}
            FROM [{req.msa_table}]
            WHERE [MAJ_CAT] IS NOT NULL AND [GEN_ART_NUMBER] IS NOT NULL{mc_where}{mp_majcat_filter}{msa_rdc_filter}
        """
        _run(conn, f"""
            INSERT INTO [{LISTING_TABLE}] ({all_cols})
            SELECT
                S.[ST_CD] AS WERKS, S.[RDC],
                M.[MAJ_CAT], M.[GEN_ART_NUMBER], M.[CLR]
                {stk_zeros_str}, 0 AS STK_TTL, 0 AS STR, 1 AS IS_NEW, NULL AS OPT_TYPE
            FROM ({msa_with_rdc}) M
            INNER JOIN ({stores_sql}) S ON 1=1 {msa_rdc_join}
            WHERE NOT EXISTS (
                SELECT 1 FROM [{LISTING_TABLE}] E
                WHERE E.[WERKS] = S.[ST_CD]
                  AND E.[MAJ_CAT] = M.[MAJ_CAT]
                  AND E.[GEN_ART_NUMBER] = M.[GEN_ART_NUMBER]
                  AND E.[CLR] = M.[CLR]
            )
        """)
        total = conn.execute(text(f"SELECT COUNT(*) FROM [{LISTING_TABLE}]")).scalar()
        new_count = total - grid_count
        logger.info(f"Part 2 (MSA missing): {new_count} rows")
        t0 = _time_step("Part 2 (MSA missing INSERT)", t0)

        # ── PART 2.5: Create indexes on listing BEFORE Part 4 (skip for tiny listings) ──
        # Index creation has significant fixed overhead (~1-2s); only worth it
        # when the listing has enough rows that Part 4 scans become expensive.
        if total >= 5000:
            try:
                _run(conn, f"CREATE NONCLUSTERED INDEX IX_{LISTING_TABLE}_WERKS_MJ ON [{LISTING_TABLE}]([WERKS], [MAJ_CAT]) INCLUDE ([GEN_ART_NUMBER], [CLR], [STK_TTL])")
            except Exception:
                pass
            try:
                _run(conn, f"CREATE NONCLUSTERED INDEX IX_{LISTING_TABLE}_GENART ON [{LISTING_TABLE}]([GEN_ART_NUMBER]) INCLUDE ([WERKS], [MAJ_CAT], [CLR])")
            except Exception:
                pass
            # Covers 4-col equi-joins in Part 3.5a Step 2 (ARS_CALC_ST_ART),
            # 3.5b (MASTER_GEN_ART_SALE), 3.5c (MASTER_GEN_ART_AGE), 3.54 (NL_TBL_HOLD).
            # Without this, those UPDATEs fall back to hash joins over millions of rows.
            try:
                _run(conn, f"CREATE NONCLUSTERED INDEX IX_{LISTING_TABLE}_OPTKEY ON [{LISTING_TABLE}]([WERKS], [MAJ_CAT], [GEN_ART_NUMBER], [CLR])")
            except Exception:
                pass
            t0 = _time_step("Part 2.5 (Indexes before Part 4)", t0)
        else:
            logger.info(f"Part 2.5: skipped indexes (listing has only {total} rows, < 5000 threshold)")
            t0 = _time_step("Part 2.5 (skipped — small listing)", t0)

        # ── PART 3: OPT_TYPE tagging — REMOVED (new logic will be added later)
        # OPT_TYPE column remains in the table (populated as NULL) for
        # backward compatibility with preview/summary endpoints.
        rl_count = 0
        nl_count = 0
        mixl_count = 0
        tbl_count = 0
        toc_count = 0
        untagged = total

        # ── PART 3.5: Populate ACS_D + ALC_D from ARS_CALC_ST_MAJ_CAT ─────
        # Needed BEFORE MIX tagging (for the STK_TTL < 60% * ACS_D rule)
        # and for Part 4 PER_OPT_SALE / Part 5 OPT_MBQ.
        for col in ["ACS_D", "ALC_D", "AUTO_GEN_ART_SALE", "AGE"]:
            try:
                _run(conn, f"ALTER TABLE [{LISTING_TABLE}] ADD [{col}] FLOAT NULL")
            except Exception:
                pass
        if _table_exists(conn, "ARS_CALC_ST_MAJ_CAT"):
            calc_cols = _get_columns(conn, "ARS_CALC_ST_MAJ_CAT")
            upd_parts = []
            # Support both new (ACS_D) and legacy (DPN) column names in source table
            acs_src = "ACS_D" if "ACS_D" in calc_cols else ("DPN" if "DPN" in calc_cols else None)
            alc_src = "ALC_D" if "ALC_D" in calc_cols else ("SAL_D" if "SAL_D" in calc_cols else None)
            if acs_src:
                upd_parts.append(f"L.[ACS_D] = TRY_CAST(C.[{acs_src}] AS FLOAT)")
            if alc_src:
                upd_parts.append(f"L.[ALC_D] = TRY_CAST(C.[{alc_src}] AS FLOAT)")
            if upd_parts:
                _run(conn, f"""
                    UPDATE L SET {', '.join(upd_parts)}
                    FROM [{LISTING_TABLE}] L
                    INNER JOIN [ARS_CALC_ST_MAJ_CAT] C WITH (NOLOCK)
                        ON L.[WERKS] = C.[ST_CD] AND L.[MAJ_CAT] = C.[MAJ_CAT]
                """)
                logger.info("Part 3.5: ACS_D, ALC_D from ARS_CALC_ST_MAJ_CAT")

        # Part 3.5a: Enrich LISTING, I_ROD, CLR_MIN, CLR_MAX, FOCUS_W_CAP, FOCUS_WO_CAP
        # Step 1: from ARS_CALC_ST_MAJ_CAT (store × MAJ_CAT grain)
        # Step 2: cascade from ARS_CALC_ST_ART (store × OPT grain — overrides where available)
        # Note: MANUAL_DENSITY is NOT enriched here — it is used for ACS_D override in Part 4c.
        enrich_cols_maj = ["LISTING", "I_ROD", "CLR_MIN", "CLR_MAX"]
        enrich_cols_art = ["LISTING", "I_ROD", "FOCUS_W_CAP", "FOCUS_WO_CAP"]
        all_enrich = sorted(set(enrich_cols_maj + enrich_cols_art))
        for col in all_enrich:
            try:
                _run(conn, f"ALTER TABLE [{LISTING_TABLE}] ADD [{col}] FLOAT NULL")
            except Exception:
                pass

        # Step 1: ST_MAJ_CAT base (LISTING, I_ROD, CLR_MIN, CLR_MAX)
        if _table_exists(conn, "ARS_CALC_ST_MAJ_CAT"):
            maj_cols = _get_columns(conn, "ARS_CALC_ST_MAJ_CAT")
            maj_upd = []
            for col in enrich_cols_maj:
                if col in maj_cols:
                    maj_upd.append(f"L.[{col}] = TRY_CAST(C.[{col}] AS FLOAT)")
            if maj_upd:
                _run(conn, f"""
                    UPDATE L SET {', '.join(maj_upd)}
                    FROM [{LISTING_TABLE}] L
                    INNER JOIN [ARS_CALC_ST_MAJ_CAT] C WITH (NOLOCK)
                        ON L.[WERKS] = C.[ST_CD] AND L.[MAJ_CAT] = C.[MAJ_CAT]
                """)
                logger.info(f"Part 3.5a: {[c.split('.')[-1] for c in maj_upd]} from ARS_CALC_ST_MAJ_CAT")

        # Step 2: ST_ART cascade (LISTING, I_ROD, FOCUS_W_CAP, FOCUS_WO_CAP)
        # Article-level values override MAJ_CAT-level where ARS_CALC_ST_ART has data.
        if _table_exists(conn, "ARS_CALC_ST_ART"):
            art_cols = _get_columns(conn, "ARS_CALC_ST_ART")
            # Per-column CASE in SET avoids the OR-heavy WHERE that blocked index seeks
            # on the 4-col (WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR) join at 6M+ rows.
            art_upd = []
            for col in enrich_cols_art:
                if col in art_cols:
                    art_upd.append(
                        f"L.[{col}] = CASE "
                        f"WHEN A.[{col}] IS NOT NULL "
                        f"AND LTRIM(RTRIM(CAST(A.[{col}] AS NVARCHAR(50)))) NOT IN ('', '0') "
                        f"THEN TRY_CAST(A.[{col}] AS FLOAT) ELSE L.[{col}] END"
                    )
            if art_upd:
                # Build join — ARS_CALC_ST_ART has ST_CD, MAJ_CAT, GEN_ART_NUMBER [, CLR]
                art_join = "L.[WERKS] = A.[ST_CD] AND L.[MAJ_CAT] = A.[MAJ_CAT]"
                if "GEN_ART_NUMBER" in art_cols:
                    art_join += " AND L.[GEN_ART_NUMBER] = A.[GEN_ART_NUMBER]"
                if "CLR" in art_cols:
                    art_join += " AND L.[CLR] = A.[CLR]"
                _run(conn, f"""
                    UPDATE L SET {', '.join(art_upd)}
                    FROM [{LISTING_TABLE}] L
                    INNER JOIN [ARS_CALC_ST_ART] A WITH (NOLOCK) ON {art_join}
                """)
                logger.info(f"Part 3.5a: {[c for c in enrich_cols_art if c in art_cols]} cascaded from ARS_CALC_ST_ART")

        # Part 3.5a override: CLR 'A' / 'A_MIX' → I_ROD = 2 (display-density floor)
        _run(conn, f"""
            UPDATE [{LISTING_TABLE}]
            SET [I_ROD] = 2
            WHERE UPPER(LTRIM(RTRIM([CLR]))) IN ('A', 'A_MIX')
        """)
        logger.info("Part 3.5a: I_ROD=2 forced for CLR in ('A','A_MIX')")
        t0 = _time_step("Part 3.5a (LISTING/I_ROD/CLR/FOCUS)", t0)

        # Part 3.5b: Populate AUTO_GEN_ART_SALE from MASTER_GEN_ART_SALE.SAL_PD
        # Option grain: (ST_CD, MAJ_CAT, GEN_ART_NUMBER, CLR). The master table
        # carries the full planned-sales universe (~21L rows) — much broader
        # than ARS_CALC_ST_ART, so we source AUTO_GEN_ART_SALE directly from it.
        # SAL_PD is precomputed in grid_calculations._step_master_sale_sal_pd.
        if _table_exists(conn, "MASTER_GEN_ART_SALE"):
            sale_cols = _get_columns(conn, "MASTER_GEN_ART_SALE")
            if "SAL_PD" in sale_cols:
                # Direct equality (no ISNULL) so SQL Server can use index seek.
                # CLR sentinel 'NA' matches what grid_builder already populates.
                _run(conn, f"""
                    UPDATE L SET L.[AUTO_GEN_ART_SALE] = TRY_CAST(S.[SAL_PD] AS FLOAT)
                    FROM [{LISTING_TABLE}] L
                    INNER JOIN [MASTER_GEN_ART_SALE] S WITH (NOLOCK)
                        ON  L.[WERKS]          = S.[ST_CD]
                        AND L.[MAJ_CAT]        = S.[MAJ_CAT]
                        AND L.[GEN_ART_NUMBER] = S.[GEN_ART_NUMBER]
                        AND L.[CLR]            = S.[CLR]
                """)
                logger.info("Part 3.5b: AUTO_GEN_ART_SALE from MASTER_GEN_ART_SALE.SAL_PD")
            else:
                logger.warning("Part 3.5b: MASTER_GEN_ART_SALE.SAL_PD not yet computed — run Contribution calc pipeline")
                logger.warning("Part 3.5b: MASTER_GEN_ART_SALE.SAL_PD not yet computed — run Contribution calc pipeline")

        # Part 3.5c: Populate AGE (option age in days) from MASTER_GEN_ART_AGE
        # An "option" = (ST_CD + MAJ_CAT + GEN_ART_NUMBER + CLR) — store-level grain.
        # This is the single authoritative source for option age.
        if _table_exists(conn, "MASTER_GEN_ART_AGE"):
            age_cols = _get_columns(conn, "MASTER_GEN_ART_AGE")
            required = {"ST_CD", "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "AGE"}
            missing = required - set(age_cols)
            if not missing:
                _run(conn, f"""
                    UPDATE L SET L.[AGE] = TRY_CAST(M.[AGE] AS FLOAT)
                    FROM [{LISTING_TABLE}] L
                    INNER JOIN [MASTER_GEN_ART_AGE] M WITH (NOLOCK)
                        ON  L.[WERKS]          = M.[ST_CD]
                        AND L.[MAJ_CAT]        = M.[MAJ_CAT]
                        AND L.[GEN_ART_NUMBER] = M.[GEN_ART_NUMBER]
                        AND L.[CLR]            = M.[CLR]
                """)
                logger.info("Part 3.5c: AGE from MASTER_GEN_ART_AGE (ST_CD+MAJ_CAT+GEN_ART_NUMBER+CLR)")
            else:
                logger.warning(f"Part 3.5c: MASTER_GEN_ART_AGE missing columns: {missing}")
        else:
            logger.warning("Part 3.5c: MASTER_GEN_ART_AGE table not found — AGE will remain NULL")
        t0 = _time_step("Part 3.5 (ACS_D/ALC_D/AUTO_GEN_ART_SALE/AGE)", t0)

        # ── PART 3.54: Populate RL_HOLD_QTY from ARS_NL_TBL_HOLD_TRACKING ────────
        # ARS_NL_TBL_HOLD_TRACKING records the warehouse hold reserved when an
        # option was first listed as TBL. Grain is (WERKS, VAR_ART, SZ); we roll
        # up open HOLD_REM to the listing's option grain
        # (WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR) and write it as RL_HOLD_QTY.
        #
        # RL_HOLD_QTY is distinct from HOLD_QTY (the current-run allocation hold
        # written by Stage C). Must be populated BEFORE Part 3.6 OPT_TYPE
        # classification — the RL/TBC/TBL rules treat RL_HOLD_QTY > 0 as
        # equivalent to having warehouse supply for the option.
        #
        # Filter: IS_CLOSED = 0 only. Closed holds are assumed to already be
        # reflected in the latest STK_TTL upload; counting them again would
        # double-count physical inventory.
        try:
            _run(conn, f"ALTER TABLE [{LISTING_TABLE}] ADD [RL_HOLD_QTY] FLOAT NULL DEFAULT 0")
        except Exception:
            pass  # column may already exist
        if _table_exists(conn, "ARS_NL_TBL_HOLD_TRACKING"):
            try:
                _run(conn, f"""
                    UPDATE L
                    SET    L.[RL_HOLD_QTY] = H.[HOLD_REM_OPT]
                    FROM   [{LISTING_TABLE}] L
                    INNER JOIN (
                        SELECT  [WERKS],
                                [MAJ_CAT],
                                [GEN_ART_NUMBER],
                                ISNULL([CLR],'') AS [CLR],
                                SUM([HOLD_REM])  AS [HOLD_REM_OPT]
                        FROM    [ARS_NL_TBL_HOLD_TRACKING]
                        WHERE   ISNULL([IS_CLOSED], 0) = 0
                          AND   ISNULL([HOLD_REM],  0) > 0
                        GROUP BY [WERKS], [MAJ_CAT], [GEN_ART_NUMBER], ISNULL([CLR],'')
                    ) H
                      ON  L.[WERKS]                              = H.[WERKS]
                      AND L.[MAJ_CAT]                            = H.[MAJ_CAT]
                      AND TRY_CAST(L.[GEN_ART_NUMBER] AS BIGINT) = H.[GEN_ART_NUMBER]
                      AND ISNULL(L.[CLR],'')                     = H.[CLR]
                    WHERE  H.[HOLD_REM_OPT] > 0
                """)
                hq_cnt = conn.execute(text(
                    f"SELECT COUNT(*) FROM [{LISTING_TABLE}] WHERE ISNULL([RL_HOLD_QTY],0) > 0"
                )).scalar() or 0
                hq_sum = conn.execute(text(
                    f"SELECT ISNULL(SUM([RL_HOLD_QTY]),0) FROM [{LISTING_TABLE}]"
                )).scalar() or 0
                logger.info(
                    f"Part 3.54: RL_HOLD_QTY from ARS_NL_TBL_HOLD_TRACKING — "
                    f"{hq_cnt} rows populated, total qty {float(hq_sum):.0f}"
                )
            except Exception as e:
                logger.warning(f"Part 3.54: RL_HOLD_QTY from ARS_NL_TBL_HOLD_TRACKING failed: {str(e)[:150]}")
        else:
            logger.info("Part 3.54: ARS_NL_TBL_HOLD_TRACKING not found — RL_HOLD_QTY stays 0")
        t0 = _time_step("Part 3.54 (RL_HOLD_QTY from ARS_NL_TBL_HOLD_TRACKING)", t0)

        # ── PART 3.55: Populate MSA_FNL_Q early (needed by Part 3.6 for TBL/TBC tagging)
        # Part 5c re-populates the same value later — idempotent.
        try:
            _run(conn, f"ALTER TABLE [{LISTING_TABLE}] ADD [MSA_FNL_Q] FLOAT NULL")
        except Exception:
            pass
        if _table_exists(conn, req.msa_table):
            _pre_msa_cols = _get_columns(conn, req.msa_table)
            if "FNL_Q" in _pre_msa_cols:
                _has_msa_rdc = msa_rdc_col in _pre_msa_cols
                _rdc_select = f", LTRIM(RTRIM(CAST([{msa_rdc_col}] AS NVARCHAR(50)))) AS MSA_RDC" if _has_msa_rdc else ""
                _rdc_group  = f", LTRIM(RTRIM(CAST([{msa_rdc_col}] AS NVARCHAR(50))))" if _has_msa_rdc else ""
                _rdc_join   = "AND L.[RDC] = M.[MSA_RDC]" if _has_msa_rdc and req.rdc_mode == "own" else ""
                try:
                    _run(conn, f"""
                        UPDATE L SET L.[MSA_FNL_Q] = TRY_CAST(M.[FNL_Q] AS FLOAT)
                        FROM [{LISTING_TABLE}] L
                        INNER JOIN (
                            SELECT {_msa_col('MAJ_CAT')}, {_msa_col('GEN_ART_NUMBER')}, {_msa_col('CLR')}
                                   {_rdc_select},
                                   SUM(TRY_CAST([FNL_Q] AS FLOAT)) AS FNL_Q
                            FROM [{req.msa_table}]
                            WHERE [MAJ_CAT] IS NOT NULL AND [GEN_ART_NUMBER] IS NOT NULL{msa_rdc_filter}
                            GROUP BY {_msa_expr('MAJ_CAT')}, {_msa_expr('GEN_ART_NUMBER')}, {_msa_expr('CLR')}{_rdc_group}
                        ) M ON L.[MAJ_CAT] = M.[MAJ_CAT]
                            AND L.[GEN_ART_NUMBER] = M.[GEN_ART_NUMBER]
                            AND L.[CLR] = M.[CLR] {_rdc_join}
                    """)
                    logger.info(f"Part 3.55: MSA_FNL_Q pre-populated from {req.msa_table} (for OPT_TYPE tagging)")
                except Exception as e:
                    logger.warning(f"Part 3.55: MSA_FNL_Q pre-populate failed: {str(e)[:150]}")

        # Also populate VAR_COUNT + VAR_FNL_COUNT alongside MSA_FNL_Q
        for col in ["VAR_COUNT", "VAR_FNL_COUNT"]:
            try:
                _run(conn, f"ALTER TABLE [{LISTING_TABLE}] ADD [{col}] FLOAT NULL")
            except Exception:
                pass
        if _table_exists(conn, "ARS_MSA_VAR_ART"):
            var_cols = _get_columns(conn, "ARS_MSA_VAR_ART")
            if all(c in var_cols for c in ["MAJ_CAT", "GEN_ART_NUMBER", "CLR"]):
                has_fnl = "FNL_Q" in var_cols
                has_var_rdc = "RDC" in var_cols
                fnl_expr = f", SUM(CASE WHEN TRY_CAST([FNL_Q] AS FLOAT) > 0 THEN 1 ELSE 0 END) AS fnl_cnt" if has_fnl else ", 0 AS fnl_cnt"
                vrdc_select = f", LTRIM(RTRIM(CAST([RDC] AS NVARCHAR(50)))) AS MSA_RDC" if has_var_rdc else ""
                vrdc_group = f", LTRIM(RTRIM(CAST([RDC] AS NVARCHAR(50))))" if has_var_rdc else ""
                vrdc_join = "AND L.[RDC] = V.[MSA_RDC]" if has_var_rdc and req.rdc_mode == "own" else ""
                var_rdc_where = msa_rdc_filter.replace(f"[{msa_rdc_col}]", "[RDC]") if has_var_rdc else ""
                try:
                    _run(conn, f"""
                        UPDATE L SET L.[VAR_COUNT] = V.var_cnt, L.[VAR_FNL_COUNT] = V.fnl_cnt
                        FROM [{LISTING_TABLE}] L
                        INNER JOIN (
                            SELECT {_msa_col('MAJ_CAT')}, {_msa_col('GEN_ART_NUMBER')}, {_msa_col('CLR')}
                                   {vrdc_select},
                                   COUNT(*) AS var_cnt{fnl_expr}
                            FROM [ARS_MSA_VAR_ART]
                            WHERE [MAJ_CAT] IS NOT NULL AND [GEN_ART_NUMBER] IS NOT NULL{mc_where}{var_rdc_where}
                            GROUP BY {_msa_expr('MAJ_CAT')}, {_msa_expr('GEN_ART_NUMBER')}, {_msa_expr('CLR')}{vrdc_group}
                        ) V ON L.[MAJ_CAT] = V.[MAJ_CAT]
                            AND L.[GEN_ART_NUMBER] = V.[GEN_ART_NUMBER]
                            AND L.[CLR] = V.[CLR] {vrdc_join}
                    """)
                    logger.info(f"Part 3.55: VAR_COUNT + VAR_FNL_COUNT from ARS_MSA_VAR_ART")
                except Exception as e:
                    logger.warning(f"Part 3.55: VAR_COUNT/FNL_COUNT failed: {str(e)[:150]}")
        t0 = _time_step("Part 3.55 (MSA_FNL_Q + VAR_COUNT)", t0)

        # ── PART 3.6: Populate GEN_ART_DESC + tag OPT_TYPE (4-way classification) ──
        # Rules (applies to ALL rows — both IS_NEW=0 and IS_NEW=1):
        #   MIX(a): low stock + no MSA   (b): poor color fill (VAR ratio < threshold)
        #   RL: adequate stock   TBC: low stock + MSA   TBL: zero stock + MSA
        try:
            _run(conn, f"ALTER TABLE [{LISTING_TABLE}] ADD [GEN_ART_DESC] NVARCHAR(500) NULL")
        except Exception:
            pass
        if _table_exists(conn, "vw_master_product"):
            try:
                _run(conn, f"""
                    UPDATE L SET L.[GEN_ART_DESC] = MP.[GEN_ART_DESC]
                    FROM [{LISTING_TABLE}] L
                    INNER JOIN [vw_master_product] MP WITH (NOLOCK)
                        ON L.[GEN_ART_NUMBER] = MP.[ARTICLE_NUMBER]
                    WHERE MP.[GEN_ART_DESC] IS NOT NULL
                """)
                logger.info("Part 3.6: GEN_ART_DESC populated from vw_master_product")
            except Exception as e:
                logger.warning(f"Part 3.6: GEN_ART_DESC population failed: {str(e)[:150]}")

        # OPT_TYPE classification — evaluated top-to-bottom, first match wins.
        # Order: MIX first (catch bad options early) → RL → TBC → TBL.
        #
        #   MIX (a): low stock + no MSA + no RL_HOLD_QTY (nothing to send)
        #   MIX (b): poor color fill (VAR ratio < threshold)
        #   RL:  (adequate stock OR RL_HOLD_QTY > 0) AND MSA_FNL_Q > 0
        #        — RL requires fresh MSA supply to top up against; an open TBL
        #          hold alone is no longer enough to land in RL.
        #   TBC: low stock, MSA or NL hold available
        #   TBL: zero stock, MSA or NL hold available
        threshold = req.stock_threshold_pct
        default_acs = float(req.default_acs_d or 18)
        def _classify_opt_type(label="OPT_TYPE"):
            _run(conn, f"""
                UPDATE [{LISTING_TABLE}]
                SET [OPT_TYPE] = CASE
                    -- MIX (a): low stock + no MSA AND no prior-run NL hold — nothing to send
                    WHEN ISNULL([STK_TTL], 0) < {threshold} * ISNULL(NULLIF([ACS_D], 0), {default_acs})
                     AND ISNULL([MSA_FNL_Q], 0)    = 0
                     AND ISNULL([RL_HOLD_QTY], 0)  = 0
                        THEN 'MIX'
                    -- MIX (b): poor color fill {f'OR count < {int(req.min_size_count)}' if int(req.min_size_count) > 0 else '(MinSz off)'}
                    WHEN ISNULL([VAR_COUNT], 0) > 0
                     AND (CAST(ISNULL([VAR_FNL_COUNT], 0) AS FLOAT) / [VAR_COUNT] < {threshold}
                          {f'OR ISNULL([VAR_FNL_COUNT], 0) < {int(req.min_size_count)}' if int(req.min_size_count) > 0 else ''})
                        THEN 'MIX'
                    -- RL: (adequate stock OR prior-run NL hold) AND fresh MSA supply available
                    WHEN (ISNULL([STK_TTL], 0) >= {threshold} * ISNULL(NULLIF([ACS_D], 0), {default_acs})
                          OR ISNULL([RL_HOLD_QTY], 0) > 0)
                     AND ISNULL([MSA_FNL_Q], 0) > 0
                        THEN 'RL'
                    -- TBC: low stock but MSA or NL hold available
                    WHEN ISNULL([STK_TTL], 0) > 0
                     AND [STK_TTL] < {threshold} * ISNULL(NULLIF([ACS_D], 0), {default_acs})
                     AND (ISNULL([MSA_FNL_Q], 0) > 0 OR ISNULL([RL_HOLD_QTY], 0) > 0)
                        THEN 'TBC'
                    -- TBL: zero/negative stock + MSA or NL hold available
                    WHEN ISNULL([STK_TTL], 0) <= 0
                     AND (ISNULL([MSA_FNL_Q], 0) > 0 OR ISNULL([RL_HOLD_QTY], 0) > 0)
                        THEN 'TBL'
                    ELSE 'MIX'
                END
            """)

        try:
            _classify_opt_type("Part 3.6")
            # Per-type counts (split by IS_NEW for visibility)
            type_counts = {}
            for row in conn.execute(text(
                f"SELECT [OPT_TYPE], [IS_NEW], COUNT(*) FROM [{LISTING_TABLE}] "
                f"GROUP BY [OPT_TYPE], [IS_NEW]"
            )).fetchall():
                key = (row[0] or "(null)", int(row[1]) if row[1] is not None else 0)
                type_counts[key] = row[2]
            def _sum(t):
                return type_counts.get((t, 0), 0) + type_counts.get((t, 1), 0)
            mixl_count = _sum("MIX")
            tbl_count  = _sum("TBL")
            toc_count  = _sum("TBC")
            rl_count   = _sum("RL")
            tagged_total = mixl_count + tbl_count + toc_count + rl_count
            untagged = total - tagged_total
            logger.info(
                f"Part 3.6: OPT_TYPE tagged — "
                f"MIX={mixl_count}, TBL={tbl_count}, TBC={toc_count}, RL={rl_count}, "
                f"untagged={untagged} "
                f"[IS_NEW=1 breakdown: TBL={type_counts.get(('TBL',1),0)}, "
                f"MIX={type_counts.get(('MIX',1),0)}, "
                f"TBC={type_counts.get(('TBC',1),0)}, "
                f"RL={type_counts.get(('RL',1),0)}]"
            )
        except Exception as e:
            tbl_count = 0
            toc_count = 0
            logger.warning(f"Part 3.6: OPT_TYPE tagging failed: {str(e)[:150]}")

        # VAR ratio override removed — MIX(b) now catches ALL rows (IS_NEW=0
        # and IS_NEW=1) with poor color availability. The RL rule in the CASE
        # statement naturally handles adequate-stock rows since MIX(b) fires
        # first and only catches poor-ratio rows.
        t0 = _time_step("Part 3.6 (OPT_TYPE classification)", t0)

        # ── PART 3.7: MIX handling ─────────────────────────────────────────
        # MIX aggregation always produces exactly 1 MIX row per (WERKS, MAJ_CAT).
        # mix_mode controls non-MIX behavior only; MIX rows are always grouped
        # at store × MAJ_CAT level to enforce the max-1-MIX-per-store-MAJ_CAT rule.
        # ALL MIX-tagged rows are aggregated (both IS_NEW=0 and IS_NEW=1).
        #
        # mix_mode values (for future non-MIX uses):
        #   "st_maj_rng" (DEFAULT), "st_maj", "each"
        # Legacy: "aggregate" → "st_maj"; "mark" → "each"
        mix_before = conn.execute(text(
            f"SELECT COUNT(*) FROM [{LISTING_TABLE}] WHERE [OPT_TYPE] = 'MIX'"
        )).scalar() or 0

        _alias = {"aggregate": "st_maj", "mark": "each"}
        mix_mode = (req.mix_mode or "st_maj_rng").lower()
        mix_mode = _alias.get(mix_mode, mix_mode)
        if mix_mode not in ("st_maj_rng", "st_maj", "each"):
            logger.warning(f"Part 3.7: unknown mix_mode={req.mix_mode!r}, defaulting to 'st_maj_rng'")
            mix_mode = "st_maj_rng"

        if mix_mode == "each":
            logger.info(f"Part 3.7: mix_mode=each — keeping all {mix_before} MIX rows as individual lines")
        elif mix_before > 0:
            try:
                all_cols_rows = conn.execute(text("""
                    SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_NAME = :t
                    ORDER BY ORDINAL_POSITION
                """), {"t": LISTING_TABLE}).fetchall()

                numeric_types = {'float','real','int','bigint','smallint','tinyint','decimal','numeric','money','smallmoney'}
                preserve_cols = {"WERKS", "RDC", "MAJ_CAT", "GEN_ART_NUMBER", "CLR",
                                 "GEN_ART_DESC", "IS_NEW", "OPT_TYPE",
                                 "ACS_D", "ALC_D", "RNG_SEG"}  # store-majcat/rng attrs (not summed)
                sum_cols = []
                for cname, ctype in all_cols_rows:
                    if cname.upper() in {c.upper() for c in preserve_cols}:
                        continue
                    if ctype.lower() in numeric_types:
                        sum_cols.append(cname)

                sum_select = ", ".join(f"SUM(ISNULL([L].[{c}], 0)) AS [{c}]" for c in sum_cols)
                sum_select_clause = f", {sum_select}" if sum_cols else ""

                has_calc = _table_exists(conn, "ARS_CALC_ST_MAJ_CAT")
                if has_calc:
                    calc_cols = _get_columns(conn, "ARS_CALC_ST_MAJ_CAT")
                    # Support both new (ACS_D/ALC_D) and legacy (DPN/SAL_D) column names
                    _acs = "ACS_D" if "ACS_D" in calc_cols else ("DPN" if "DPN" in calc_cols else None)
                    _alc = "ALC_D" if "ALC_D" in calc_cols else ("SAL_D" if "SAL_D" in calc_cols else None)
                    dpn_expr  = f"MAX(TRY_CAST(C.[{_acs}] AS FLOAT))" if _acs else "NULL"
                    sald_expr = f"MAX(TRY_CAST(C.[{_alc}] AS FLOAT))" if _alc else "NULL"
                    calc_join = """
                        LEFT JOIN [ARS_CALC_ST_MAJ_CAT] C WITH (NOLOCK)
                            ON L.[WERKS] = C.[ST_CD] AND L.[MAJ_CAT] = C.[MAJ_CAT]
                    """
                else:
                    dpn_expr = "NULL"; sald_expr = "NULL"; calc_join = ""

                # MIX always aggregates at (WERKS, MAJ_CAT) level — max 1 MIX
                # row per store × MAJ_CAT regardless of mix_mode setting.
                mp_join = ""
                rng_select_col = "CAST(NULL AS NVARCHAR(100)) AS [RNG_SEG]"
                group_by       = "L.[WERKS], L.[MAJ_CAT]"
                mode_label     = "per (WERKS, MAJ_CAT) — max 1 MIX per store×MAJ_CAT"

                staging = "#mix_agg"
                _run(conn, f"IF OBJECT_ID('tempdb..{staging}') IS NOT NULL DROP TABLE {staging}")
                # Aggregate ALL MIX-tagged rows (both IS_NEW=0 and IS_NEW=1)
                _run(conn, f"""
                    SELECT
                        L.[WERKS], MAX(L.[RDC]) AS [RDC], L.[MAJ_CAT],
                        CAST(0 AS BIGINT) AS [GEN_ART_NUMBER],
                        CAST('MIX' AS NVARCHAR(100)) AS [CLR],
                        CAST('MIX' AS NVARCHAR(500)) AS [GEN_ART_DESC],
                        CAST(0 AS BIT) AS [IS_NEW],
                        CAST('MIX' AS NVARCHAR(10)) AS [OPT_TYPE],
                        CAST({dpn_expr} AS FLOAT) AS [ACS_D],
                        CAST({sald_expr} AS FLOAT) AS [ALC_D],
                        {rng_select_col}
                        {sum_select_clause}
                    INTO {staging}
                    FROM [{LISTING_TABLE}] L
                    {calc_join}
                    {mp_join}
                    WHERE L.[OPT_TYPE] = 'MIX'
                    GROUP BY {group_by}
                """)
                agg_rows = conn.execute(text(f"SELECT COUNT(*) FROM {staging}")).scalar() or 0

                # Delete ALL MIX rows (both IS_NEW=0 and IS_NEW=1) — replaced by aggregated
                _run(conn, f"DELETE FROM [{LISTING_TABLE}] WHERE [OPT_TYPE] = 'MIX'")

                # Build INSERT columns
                ins_cols = ["WERKS", "RDC", "MAJ_CAT", "GEN_ART_NUMBER", "CLR",
                            "GEN_ART_DESC", "IS_NEW", "OPT_TYPE", "ACS_D", "ALC_D"]
                # Include RNG_SEG (always NULL for MIX) if column exists on listing
                listing_cols_upper = {c.upper() for c in _get_columns(conn, LISTING_TABLE)}
                if "RNG_SEG" in listing_cols_upper:
                    ins_cols.append("RNG_SEG")
                ins_cols += sum_cols
                ins_cols_sql = ", ".join(f"[{c}]" for c in ins_cols)
                _run(conn, f"""
                    INSERT INTO [{LISTING_TABLE}] ({ins_cols_sql})
                    SELECT {ins_cols_sql} FROM {staging}
                """)
                _run(conn, f"DROP TABLE {staging}")

                # Verify: max 1 MIX row per (WERKS, MAJ_CAT)
                mix_dupes = conn.execute(text(
                    f"SELECT COUNT(*) FROM (SELECT [WERKS], [MAJ_CAT], COUNT(*) AS cnt "
                    f"FROM [{LISTING_TABLE}] WHERE [OPT_TYPE] = 'MIX' "
                    f"GROUP BY [WERKS], [MAJ_CAT] HAVING COUNT(*) > 1) X"
                )).scalar() or 0

                logger.info(f"Part 3.7: aggregated {mix_before} MIX rows → {agg_rows} MIX lines "
                            f"[{mode_label}], summed {len(sum_cols)} numeric cols "
                            f"(ACS_D/ALC_D fetched from ARS_CALC_ST_MAJ_CAT, not summed)"
                            f"{f' | WARNING: {mix_dupes} store×MAJ_CAT with >1 MIX' if mix_dupes else ''}")
            except Exception as e:
                logger.warning(f"Part 3.7 MIX aggregation failed: {str(e)[:200]}")
        else:
            logger.info("Part 3.7: no MIX rows to aggregate")
        t0 = _time_step(f"Part 3.7 (MIX handling, mode={mix_mode})", t0)

        # ── PART 4: Add CONT, MBQ, OPT_CNT, DISP_Q from ALL grid tables ─────
        # Each grid adds prefixed columns: MJ_CONT, CLR_CONT, RNG_SEG_MBQ, etc.
        # DISP_Q is stored as DISP_Q * CONT (pre-computed in grid_builder).
        # Skip pivot_only grids (GEN_ART, VAR_ART — no CONT/MBQ/OPT_CNT/DISP_Q).
        # Also adds: {prefix}_GRID_GROUP, {prefix}_WEIGHTAGE, {prefix}_PER_OPT_SALE
        src_cols = ["STK_TTL", "STR", "CONT", "MBQ", "OPT_CNT", "DISP_Q"]

        if _table_exists(conn, "ARS_GRID_BUILDER"):
            grid_rows = conn.execute(text("""
                SELECT grid_name, output_table, hierarchy_columns
                FROM [ARS_GRID_BUILDER]
                WHERE UPPER(status) = 'ACTIVE'
                  AND ISNULL(pivot_only, 0) = 0
                ORDER BY seq ASC
            """)).fetchall()
        else:
            grid_rows = []

        listing_direct_cols = {"WERKS", "MAJ_CAT", "GEN_ART_NUMBER", "CLR"}
        # Get vw_master_product columns for resolving hierarchy via GEN_ART_NUMBER
        mp_cols_set = set()
        if _table_exists(conn, "vw_master_product"):
            mp_cols_set = {r[0].upper() for r in conn.execute(text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='vw_master_product'"
            )).fetchall()}

        # ── OPTIMIZATION: Pre-resolve MP attributes ONCE onto listing ─────
        # Previously each grid using MP cols (MACRO_MVGR, MICRO_MVGR, FAB,
        # M_VND_CD, RNG_SEG, etc.) re-joined vw_master_product for 5M rows.
        # Now we add those columns to listing + populate ONCE. Then every
        # Part 4 grid becomes a DIRECT join → saves N × (5M-row MP join).
        mp_needed_cols = set()
        for grow in grid_rows:
            try:
                _h = json.loads(grow[2]) if isinstance(grow[2], str) else grow[2]
                for hc in (_h or []):
                    hcu = hc.upper()
                    if hcu not in listing_direct_cols and hcu in mp_cols_set:
                        mp_needed_cols.add(hcu)
            except Exception:
                pass

        if mp_needed_cols and _table_exists(conn, "vw_master_product"):
            # Add columns to listing (NVARCHAR as default, BIGINT for known numeric)
            from app.utils.db_helpers import get_columns as _gc
            mp_type_rows = conn.execute(text(
                "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME='vw_master_product'"
            )).fetchall()
            mp_type_map = {r[0].upper(): r[1].lower() for r in mp_type_rows}
            mp_actual_map = {r[0].upper(): r[0] for r in mp_type_rows}
            _NUM = {'bigint','int','smallint','tinyint','float','real','decimal','numeric','money','smallmoney'}

            existing_listing_cols = {c.upper() for c in _gc(conn, LISTING_TABLE)}
            set_parts = []
            for mc in mp_needed_cols:
                if mc in existing_listing_cols:
                    continue
                is_num = mp_type_map.get(mc, '') in _NUM
                dtype = "BIGINT NULL" if is_num else "NVARCHAR(200) NULL"
                try:
                    _run(conn, f"ALTER TABLE [{LISTING_TABLE}] ADD [{mc}] {dtype}")
                except Exception:
                    pass
            # Build single UPDATE that populates all MP cols at once
            for mc in mp_needed_cols:
                actual = mp_actual_map.get(mc, mc)
                is_num = mp_type_map.get(mc, '') in _NUM
                if is_num:
                    set_parts.append(f"L.[{mc}] = ISNULL(TRY_CAST(MP.[{actual}] AS BIGINT), 0)")
                else:
                    set_parts.append(f"L.[{mc}] = ISNULL(LTRIM(RTRIM(CAST(MP.[{actual}] AS NVARCHAR(200)))), 'NA')")
            if set_parts:
                try:
                    _run(conn, f"""
                        UPDATE L SET {', '.join(set_parts)}
                        FROM [{LISTING_TABLE}] L
                        INNER JOIN [vw_master_product] MP WITH (NOLOCK)
                            ON L.[GEN_ART_NUMBER] = MP.[ARTICLE_NUMBER]
                    """)
                    logger.info(f"Part 4 pre-resolve: populated {len(mp_needed_cols)} MP cols on listing: {sorted(mp_needed_cols)}")
                except Exception as e:
                    logger.warning(f"Part 4 pre-resolve failed: {str(e)[:150]}")
            # Now treat all MP cols as "direct" — no more MP join needed
            listing_direct_cols = listing_direct_cols | mp_needed_cols
        t0 = _time_step("Part 4 pre-resolve (MP → listing cols)", t0)

        mapped_grids = []
        for grow in grid_rows:
            gname, gtable = grow[0], grow[1]
            try:
                ghier = json.loads(grow[2]) if isinstance(grow[2], str) else grow[2]
            except Exception:
                continue
            if not _table_exists(conn, gtable):
                continue

            gcols = _get_columns(conn, gtable)
            available = [c for c in src_cols if c in gcols]
            if not available:
                continue

            # Build join: hierarchy cols from listing directly (MP cols now
            # pre-populated onto listing in pre-resolve step, so always direct)
            join_parts = []
            can_join = True
            for hc in ghier:
                hcu = hc.upper()
                if hcu in listing_direct_cols:
                    # Both sides are BIGINT → no TRY_CAST needed (preserves index seek)
                    join_parts.append(f"L.[{hcu}] = G.[{hc}]")
                else:
                    can_join = False
                    break

            if not can_join:
                logger.info(f"Part 4: {gname} skipped — {ghier} not resolvable")
                continue

            join_sql = " AND ".join(join_parts)

            # Prefix: MJ_RNG_SEG→RNG_SEG, MJ_CLR→CLR, MJ→MJ
            prefix = gname.upper()
            if prefix.startswith("MJ_"):
                prefix = prefix[3:]

            col_map = {}
            for c in available:
                new_col = f"{prefix}_{c}"
                col_map[new_col] = c
                try:
                    _run(conn, f"ALTER TABLE [{LISTING_TABLE}] ADD [{new_col}] FLOAT NULL")
                except Exception:
                    pass

            set_parts = ", ".join(f"L.[{nc}] = TRY_CAST(G.[{oc}] AS FLOAT)" for nc, oc in col_map.items())

            update_sql = f"""
                UPDATE L SET {set_parts}
                FROM [{LISTING_TABLE}] L
                INNER JOIN [{gtable}] G WITH (NOLOCK) ON {join_sql}
            """

            g_t0 = time.time()
            try:
                _run(conn, update_sql)
                dt = round(time.time() - g_t0, 1)
                mapped_grids.append(gname)
                logger.info(f"Part 4: {gname} → {list(col_map.keys())} [direct, join on {ghier}] — {dt}s")
                step_timings.append({"step": f"Part 4 [{gname}]", "seconds": dt})
            except Exception as e:
                dt = round(time.time() - g_t0, 1)
                logger.warning(f"Part 4: {gname} failed in {dt}s: {str(e)[:200]}")
                step_timings.append({"step": f"Part 4 [{gname}] FAILED", "seconds": dt})

        t0 = _time_step("Part 4a (Grid column joins)", t0)

        # ── Part 4b: PER_OPT_SALE from the grid flagged use_for_opt_sale ──
        listing_cols = _get_columns(conn, LISTING_TABLE)
        has_dpn  = "ACS_D"  in listing_cols
        has_sald = "ALC_D" in listing_cols
        opt_grid_row = conn.execute(text("""
            SELECT TOP 1 grid_name FROM [ARS_GRID_BUILDER]
            WHERE ISNULL(use_for_opt_sale, 0) = 1 AND UPPER(status) = 'ACTIVE'
            ORDER BY seq ASC
        """)).fetchone()
        if opt_grid_row and has_dpn and has_sald:
            opt_prefix = opt_grid_row[0].upper()
            if opt_prefix.startswith("MJ_"):
                opt_prefix = opt_prefix[3:]
            opt_mbq  = f"{opt_prefix}_MBQ"
            opt_disp = f"{opt_prefix}_DISP_Q"
            if opt_mbq in listing_cols and opt_disp in listing_cols:
                if "PER_OPT_SALE" not in listing_cols:
                    try:
                        _run(conn, f"ALTER TABLE [{LISTING_TABLE}] ADD [PER_OPT_SALE] FLOAT NULL")
                    except Exception:
                        pass
                _run(conn, f"""
                    UPDATE [{LISTING_TABLE}] SET [PER_OPT_SALE] = ROUND(CASE
                        WHEN ISNULL([{opt_disp}],0) = 0 OR ISNULL([ALC_D],0) = 0 THEN 0
                        ELSE ((ISNULL([{opt_mbq}],0) - ISNULL([{opt_disp}],0))
                               / NULLIF([{opt_disp}],0) * ISNULL([ACS_D],0)) / NULLIF([ALC_D],0)
                    END, 2)
                """)
                logger.info(f"Part 4b: PER_OPT_SALE from {opt_grid_row[0]}")

        t0 = _time_step("Part 4b (PER_OPT_SALE)", t0)

        # ── Part 4c: OPT_MBQ + OPT_REQ (moved here from Part 5 — needed for excess calc) ──
        listing_cols = _get_columns(conn, LISTING_TABLE)
        sale_col = None
        for c in listing_cols:
            if ("L-7" in c.upper() or "L_7" in c.upper()) and "SALE" in c.upper() and "7" in c:
                sale_col = c
                break
        if not sale_col:
            for c in listing_cols:
                if c.upper().startswith("L-7") or c.upper().startswith("L_7"):
                    sale_col = c
                    break

        for col in ["OPT_MBQ", "OPT_REQ", "EXCESS_STK"]:
            try:
                _run(conn, f"ALTER TABLE [{LISTING_TABLE}] ADD [{col}] FLOAT NULL")
            except Exception:
                pass

        # ── Part 4c pre: ACS_D override from MANUAL_DENSITY (article-level) ──
        # If MANUAL_DENSITY > 0 at OPT grain, override ACS_D for that OPT.
        # Try ARS_CALC_ST_ART first (rebuilt in full mode), then fall back
        # to Master_ALC_INPUT_ST_ART directly (works even in listing-only mode).
        # Also handles legacy column name MANUAL_MBQ (before migration).
        _dpn_override_done = False
        for _src_tbl in ["ARS_CALC_ST_ART", "Master_ALC_INPUT_ST_ART"]:
            if _dpn_override_done or not _table_exists(conn, _src_tbl):
                continue
            _src_cols = _get_columns(conn, _src_tbl)
            # Find the column: MANUAL_DENSITY (new) or MANUAL_MBQ (legacy)
            _md_col = "MANUAL_DENSITY" if "MANUAL_DENSITY" in _src_cols else (
                      "MANUAL_MBQ" if "MANUAL_MBQ" in _src_cols else None)
            if not _md_col:
                continue
            # Build join: WERKS=ST_CD + MAJ_CAT [+ GEN_ART_NUMBER] [+ CLR]
            _art_join = "L.[WERKS] = A.[ST_CD] AND L.[MAJ_CAT] = A.[MAJ_CAT]"
            # Article key: GEN_ART_NUMBER or 10_DIGIT or ART_NUMBER
            _art_key = next((c for c in ["GEN_ART_NUMBER", "10_DIGIT", "ART_NUMBER", "ARTICLE_NUMBER"] if c in _src_cols), None)
            if _art_key:
                if _art_key == "GEN_ART_NUMBER":
                    _art_join += f" AND L.[GEN_ART_NUMBER] = A.[{_art_key}]"
                else:
                    _art_join += f" AND L.[GEN_ART_NUMBER] = TRY_CAST(TRY_CAST(A.[{_art_key}] AS FLOAT) AS BIGINT)"
            if "CLR" in _src_cols:
                _art_join += " AND L.[CLR] = A.[CLR]"
            try:
                _run(conn, f"""
                    UPDATE L SET L.[ACS_D] = TRY_CAST(A.[{_md_col}] AS FLOAT)
                    FROM [{LISTING_TABLE}] L
                    INNER JOIN [{_src_tbl}] A WITH (NOLOCK) ON {_art_join}
                    WHERE ISNULL(TRY_CAST(A.[{_md_col}] AS FLOAT), 0) > 0
                """)
                _cnt = conn.execute(text(
                    f"SELECT COUNT(*) FROM [{LISTING_TABLE}] L "
                    f"INNER JOIN [{_src_tbl}] A WITH (NOLOCK) ON {_art_join} "
                    f"WHERE ISNULL(TRY_CAST(A.[{_md_col}] AS FLOAT), 0) > 0"
                )).scalar() or 0
                logger.info(f"Part 4c: ACS_D overridden by {_src_tbl}.{_md_col} for {_cnt} rows")
                _dpn_override_done = True
            except Exception as e:
                logger.warning(f"Part 4c: {_src_tbl}.{_md_col} → ACS_D override failed: {str(e)[:150]}")

        listing_cols = _get_columns(conn, LISTING_TABLE)
        has_auto    = "AUTO_GEN_ART_SALE" in listing_cols
        has_age     = "AGE" in listing_cols
        has_per_opt = "PER_OPT_SALE" in listing_cols
        if sale_col and "ACS_D" in listing_cols:
            l7_daily   = f"(ISNULL(TRY_CAST([{sale_col}] AS FLOAT), 0) / 7.0)"
            auto_daily = "ISNULL([AUTO_GEN_ART_SALE], 0)" if has_auto else "0"
            per_opt    = "ISNULL([PER_OPT_SALE], 0)"    if has_per_opt else "0"

            def _sql_max(*exprs):
                values = ", ".join(f"({e})" for e in exprs)
                return f"(SELECT MAX(v) FROM (VALUES {values}) T(v))"

            default_rate = _sql_max(l7_daily, auto_daily) if has_auto else l7_daily
            new_rate = _sql_max(per_opt, l7_daily, auto_daily)

            # Use "new article" rate (includes PER_OPT_SALE) when:
            #   AGE < threshold  OR  AGE is NULL/blank/0 (unknown = treat as new)
            # Effective AGE = 0 (treat as new) when STK_TTL<=0 AND L-7 sale<=0
            # (no stock + no recent sale → fresh / about-to-dispatch OPT).
            if has_age:
                eff_age = (
                    "CASE "
                    f"WHEN ISNULL([STK_TTL], 0) <= 0 AND ISNULL(TRY_CAST([{sale_col}] AS FLOAT), 0) <= 0 THEN 0 "
                    "ELSE ISNULL([AGE], 0) "
                    "END"
                )
                rate_expr = (
                    f"CASE WHEN ({eff_age}) < {int(req.age_threshold)} "
                    f"THEN {new_rate} ELSE {default_rate} END"
                )
            else:
                rate_expr = new_rate
                eff_age = None  # used by MAX_DAILY_SALE branching below

            # OPT_MBQ = ACS_D + rate × ALC_D
            _run(conn, f"""
                UPDATE [{LISTING_TABLE}]
                SET [OPT_MBQ] = ROUND(ISNULL([ACS_D], 0) + ({rate_expr}) * ISNULL([ALC_D], 0), 0)
            """)
            # OPT_REQ = MAX(0, OPT_MBQ - STK_TTL)
            _run(conn, f"""
                UPDATE [{LISTING_TABLE}]
                SET [OPT_REQ] = CASE
                    WHEN ISNULL([OPT_MBQ], 0) - ISNULL([STK_TTL], 0) > 0
                    THEN ROUND(ISNULL([OPT_MBQ], 0) - ISNULL([STK_TTL], 0), 0)
                    ELSE 0 END
            """)

            # OPT_MBQ_WH = ACS_D + rate × (ALC_D + HOLD_DAYS) — "With Hold"
            # OPT_REQ_WH = MAX(0, OPT_MBQ_WH - STK_TTL)
            for col in ["OPT_MBQ_WH", "OPT_REQ_WH"]:
                try:
                    _run(conn, f"ALTER TABLE [{LISTING_TABLE}] ADD [{col}] FLOAT NULL")
                except Exception:
                    pass
            # HOLD_DAYS applies ONLY to OPT_TYPE='TBL' (zero-stock + MSA available —
            # the dispatch warrants a one-shot warehouse buffer alongside the display
            # set, regardless of whether the article is new or an existing one that
            # depleted to zero).  For RL/TBC/MIX rows, OPT_MBQ_WH = OPT_MBQ (no hold).
            hold = int(req.hold_days or 0)
            _run(conn, f"""
                UPDATE [{LISTING_TABLE}]
                SET [OPT_MBQ_WH] = ROUND(ISNULL([ACS_D], 0) + ({rate_expr})
                    * (ISNULL([ALC_D], 0) + CASE WHEN ISNULL([OPT_TYPE], '') = 'TBL' THEN {hold} ELSE 0 END), 0)
            """)
            _run(conn, f"""
                UPDATE [{LISTING_TABLE}]
                SET [OPT_REQ_WH] = CASE
                    WHEN ISNULL([OPT_MBQ_WH], 0) - ISNULL([STK_TTL], 0) > 0
                    THEN ROUND(ISNULL([OPT_MBQ_WH], 0) - ISNULL([STK_TTL], 0), 0)
                    ELSE 0 END
            """)
            # MAX_DAILY_SALE — same eff_age branching as the OPT_MBQ rate:
            #   eff_age < threshold → MAX(PER_OPT_SALE, L-7/7, AUTO_GEN_ART_SALE)
            #   else                → MAX(L-7/7, AUTO_GEN_ART_SALE)
            try:
                _run(conn, f"ALTER TABLE [{LISTING_TABLE}] ADD [MAX_DAILY_SALE] FLOAT NULL")
            except Exception:
                pass
            auto_expr = "ISNULL([AUTO_GEN_ART_SALE], 0)" if has_auto else "0"
            new_mds     = _sql_max(per_opt, l7_daily, auto_expr) if has_auto else _sql_max(per_opt, l7_daily)
            default_mds = _sql_max(l7_daily, auto_expr)          if has_auto else l7_daily
            if has_age and eff_age is not None:
                mds_expr = (
                    f"CASE WHEN ({eff_age}) < {int(req.age_threshold)} "
                    f"THEN {new_mds} ELSE {default_mds} END"
                )
            else:
                mds_expr = new_mds
            _run(conn, f"""
                UPDATE [{LISTING_TABLE}]
                SET [MAX_DAILY_SALE] = ROUND({mds_expr}, 3)
            """)

            logger.info(f"Part 4c: OPT_MBQ(ACS_D+ALC_D) + OPT_REQ + OPT_MBQ_WH(hold={hold}d) + OPT_REQ_WH + MAX_DAILY_SALE")

        t0 = _time_step("Part 4c (OPT_MBQ + OPT_REQ + OPT_MBQ_WH + MAX_DAILY_SALE)", t0)

        # ── Part 4d: ART_EXCESS = MAX(0, STK_TTL - eff_mult × OPT_MBQ), skip MIX ──
        # eff_mult = MAX(I_ROD, excess_multiplier). An OPT already configured with N
        # replen rounds carries N × OPT_MBQ of planned stock — only stock above that
        # threshold is true "excess" available for redistribution.
        try:
            _run(conn, f"ALTER TABLE [{LISTING_TABLE}] ADD [ART_EXCESS] FLOAT NULL")
        except Exception:
            pass
        eff_mult = (
            f"CASE WHEN ISNULL(TRY_CAST([I_ROD] AS FLOAT), 0) > {req.excess_multiplier} "
            f"THEN TRY_CAST([I_ROD] AS FLOAT) "
            f"ELSE {req.excess_multiplier} END"
        )
        _run(conn, f"""
            UPDATE [{LISTING_TABLE}]
            SET [ART_EXCESS] = CASE
                WHEN ISNULL([OPT_TYPE],'') = 'MIX' THEN 0
                WHEN ISNULL([STK_TTL],0) - ({eff_mult}) * ISNULL([OPT_MBQ],0) > 0
                THEN ROUND(ISNULL([STK_TTL],0) - ({eff_mult}) * ISNULL([OPT_MBQ],0), 0)
                ELSE 0 END
        """)
        # Also set overall EXCESS_STK (same formula, visible in output)
        _run(conn, f"""
            UPDATE [{LISTING_TABLE}]
            SET [EXCESS_STK] = [ART_EXCESS]
        """)
        art_excess_sum = conn.execute(text(f"SELECT SUM([ART_EXCESS]) FROM [{LISTING_TABLE}]")).scalar() or 0
        logger.info(f"Part 4d: ART_EXCESS calculated (total excess={art_excess_sum:.0f}, MIX rows skipped)")

        t0 = _time_step("Part 4d (ART_EXCESS + EXCESS_STK)", t0)

        # ── Part 4e: Per-grid stock deduction + REQ calc ────────────────
        # For each grid:
        #   1. Aggregate ART_EXCESS by that grid's hierarchy keys.
        #   2. Deduct from {prefix}_STK_TTL (clamped to 0).
        #   3. REQ = MAX(0, {prefix}_MBQ - deducted_{prefix}_STK_TTL).
        # Part 4 rewrites STK_TTL fresh on every rebuild, so deduction is idempotent.
        listing_cols = _get_columns(conn, LISTING_TABLE)
        req_log = []
        for gname in mapped_grids:
            prefix = gname.upper()
            if prefix.startswith("MJ_"):
                prefix = prefix[3:]
            mbq_col = f"{prefix}_MBQ"
            stk_col = f"{prefix}_STK_TTL"
            req_col = f"{prefix}_REQ"

            if mbq_col not in listing_cols or stk_col not in listing_cols:
                continue
            if req_col not in listing_cols:
                try:
                    _run(conn, f"ALTER TABLE [{LISTING_TABLE}] ADD [{req_col}] FLOAT NULL")
                except Exception:
                    pass

            # Determine hierarchy columns for this grid (for GROUP BY)
            grid_row = conn.execute(text("""
                SELECT hierarchy_columns FROM [ARS_GRID_BUILDER]
                WHERE grid_name = :gn
            """), {"gn": gname}).fetchone()
            if not grid_row:
                continue
            try:
                ghier = json.loads(grid_row[0]) if isinstance(grid_row[0], str) else grid_row[0]
            except Exception:
                continue

            # Build GROUP BY keys from hierarchy. WERKS+MAJ_CAT are always present
            # in the CTE base — strip them here to avoid duplicate column errors
            # (e.g. MJ grid's hierarchy IS just [WERKS, MAJ_CAT]).
            group_cols = [
                h.upper() for h in ghier
                if h.upper() in {c.upper() for c in listing_cols}
                and h.upper() not in {"WERKS", "MAJ_CAT"}
            ]

            # Extra (non-WERKS/MAJ_CAT) grouping keys, if any
            extra_sel  = (", " + ", ".join(f"[{c}]" for c in group_cols)) if group_cols else ""
            extra_join = (" AND " + " AND ".join(f"L.[{c}] = E.[{c}]" for c in group_cols)) if group_cols else ""

            # Reusable deducted-stock expression — used by both SET clauses; SQL Server
            # evaluates SET right-hand sides against the row's pre-UPDATE values, so
            # repeating the CASE is required to make REQ see the deducted stock.
            deducted_stk = (
                f"CASE WHEN ISNULL(L.[{stk_col}], 0) - ISNULL(E.exc, 0) > 0 "
                f"THEN ROUND(ISNULL(L.[{stk_col}], 0) - ISNULL(E.exc, 0), 0) "
                f"ELSE 0 END"
            )
            try:
                _run(conn, f"""
                    ;WITH ExcessByGrid AS (
                        SELECT [WERKS], [MAJ_CAT]{extra_sel},
                               SUM(ISNULL([ART_EXCESS], 0)) AS exc
                        FROM [{LISTING_TABLE}]
                        WHERE [WERKS] IS NOT NULL
                        GROUP BY [WERKS], [MAJ_CAT]{extra_sel}
                    )
                    UPDATE L SET
                        L.[{stk_col}] = {deducted_stk},
                        L.[{req_col}] = CASE
                            WHEN ISNULL(L.[{mbq_col}], 0) - ({deducted_stk}) > 0
                            THEN ROUND(ISNULL(L.[{mbq_col}], 0) - ({deducted_stk}), 0)
                            ELSE 0
                        END
                    FROM [{LISTING_TABLE}] L
                    LEFT JOIN ExcessByGrid E
                      ON L.[WERKS] = E.[WERKS]
                     AND L.[MAJ_CAT] = E.[MAJ_CAT]{extra_join}
                """)
                grp_label = ",".join(["WERKS", "MAJ_CAT"] + group_cols)
                req_log.append(f"{req_col}(by {grp_label})")
            except Exception as e:
                logger.warning(f"Part 4e: {req_col} failed: {str(e)[:150]}")

        if req_log:
            logger.info(f"Part 4e: REQ with excess deduction: {req_log}")

        # ART_EXCESS retained on listing for audit/UI — drives the deduction above
        # and remains queryable via the public EXCESS_STK alias.

        logger.info(f"Part 4 complete: {len(mapped_grids)} grids: {', '.join(mapped_grids)}")
        t0 = _time_step("Part 4e (Per-grid REQ with excess deduction)", t0)

        # ── PART 5: All moved earlier ──────────────────────────────────
        # OPT_MBQ/OPT_REQ/EXCESS_STK → Part 4c/4d
        # MSA_FNL_Q + VAR_COUNT + VAR_FNL_COUNT → Part 3.55

        # Additional indexes (WERKS-only, RDC)
        try:
            _run(conn, f"CREATE NONCLUSTERED INDEX IX_{LISTING_TABLE}_RDC ON [{LISTING_TABLE}]([RDC])")
        except Exception:
            pass
        t0 = _time_step("Part 5 (Final indexes)", t0)

    # ── Auto-create ARS_STORE_RANKING (before working table) ──────────
    RANK_TABLE = "ARS_STORE_RANKING"
    rank_rows = 0
    rw = float(req.req_weight or 0.4)
    fw = float(req.fill_weight or 0.6)
    try:
        with de.connect() as rc:
            _run(rc, f"IF OBJECT_ID('{RANK_TABLE}','U') IS NOT NULL DROP TABLE [{RANK_TABLE}]")
            _run(rc, f"""
                ;WITH StoreAgg AS (
                    SELECT
                        [MAJ_CAT], [WERKS], MAX([RDC]) AS RDC,
                        MAX(ISNULL([MJ_REQ], 0))     AS MJ_REQ,
                        MAX(ISNULL([MJ_MBQ], 0))     AS MJ_MBQ,
                        MAX(ISNULL([MJ_STK_TTL], 0))  AS MJ_STK,
                        MAX(ISNULL([ACS_D], 0))       AS ACS_D,
                        CASE WHEN MAX(ISNULL([MJ_MBQ],0)) = 0 THEN 0
                             ELSE ROUND(MAX(ISNULL([MJ_STK_TTL],0)) / NULLIF(MAX([MJ_MBQ]),0), 4)
                        END AS FILL_RATE
                    FROM [{LISTING_TABLE}]
                    WHERE ISNULL([OPT_TYPE],'') <> 'MIX'
                    GROUP BY [MAJ_CAT], [WERKS]
                ),
                Ranked AS (
                    SELECT *,
                        DENSE_RANK() OVER (PARTITION BY MAJ_CAT ORDER BY MJ_REQ ASC)    AS REQ_RANK,
                        DENSE_RANK() OVER (PARTITION BY MAJ_CAT ORDER BY FILL_RATE DESC) AS FILL_RANK
                    FROM StoreAgg
                )
                SELECT *,
                    ROUND(REQ_RANK * {rw} + FILL_RANK * {fw}, 2) AS W_SCORE,
                    ROW_NUMBER() OVER (PARTITION BY MAJ_CAT
                        ORDER BY ROUND(REQ_RANK * {rw} + FILL_RANK * {fw}, 2) DESC,
                                 WERKS ASC) AS ST_RANK
                INTO [{RANK_TABLE}]
                FROM Ranked
            """)
            rank_rows = rc.execute(text(f"SELECT COUNT(*) FROM [{RANK_TABLE}]")).scalar()
            logger.info(f"{RANK_TABLE}: {rank_rows} rows (req_wt={rw}, fill_wt={fw})")

            # Populate ST_RANK on ARS_LISTING. ST_RANK is the per-MAJ_CAT
            # store rank computed above (REQ × FILL weighted score). It is
            # NOT used by _stage_a_assign_rank anymore (that's now partitioned
            # per (WERKS, OPT_TYPE), so a per-store rank inside it would be
            # constant). It IS still needed downstream:
            #   • _stage_a_materialize_listed selects [ST_RANK] from the
            #     working table — without this column the query 500s.
            #   • The allocation waterfall uses it as a tiebreaker so two
            #     options with the same OPT_PRIORITY_RANK ship in store-rank
            #     order across MAJ_CATs.
            try:
                rc.execute(text(f"ALTER TABLE [{LISTING_TABLE}] ADD [ST_RANK] INT NULL"))
                rc.commit()
            except Exception:
                pass  # column may already exist from a prior run
            _run(rc, f"""
                UPDATE L SET L.[ST_RANK] = R.[ST_RANK]
                FROM [{LISTING_TABLE}] L
                INNER JOIN [{RANK_TABLE}] R
                    ON L.[WERKS] = R.[WERKS] AND L.[MAJ_CAT] = R.[MAJ_CAT]
            """)
            logger.info(f"ST_RANK populated into {LISTING_TABLE}")
    except Exception as e:
        logger.warning(f"{RANK_TABLE} creation failed: {e}")
    t0 = _time_step("Part 6 (Store Ranking)", t0)

    # ── Auto-create ARS_LISTING_WORKING (filtered copy) ───────────────
    working_rows = 0
    try:
        with de.connect() as wc:
            all_cols = _get_columns(wc, LISTING_TABLE)
            all_upper = {c.upper(): c for c in all_cols}
            selected = list(all_cols)
            if selected:
                col_list = ", ".join(f"[{c}]" for c in selected)
                _run(wc, f"IF OBJECT_ID('{FINAL_TABLE}','U') IS NOT NULL DROP TABLE [{FINAL_TABLE}]")

                where = []
                # Keep rows with a fresh MSA pool OR pending hold from a prior run.
                # Both conditions together mean there is something to work with.
                if "MSA_FNL_Q" in all_upper and "RL_HOLD_QTY" in all_upper:
                    where.append("(ISNULL([MSA_FNL_Q], 0) > 0 OR ISNULL([RL_HOLD_QTY], 0) > 0)")
                elif "MSA_FNL_Q" in all_upper:
                    where.append("ISNULL([MSA_FNL_Q], 0) > 0")
                if "OPT_REQ_WH" in all_upper:
                    where.append("ISNULL([OPT_REQ_WH], 0) >= 1")
                # VAR ratio check: only for TBL (new listings need proper size coverage)
                # RL/TBC skip this — they only need replenishment on available sizes
                # Condition: ratio >= 60%, and if MinSz active: OR count >= min_size_count
                min_sz = int(req.min_size_count or 0)
                if "VAR_COUNT" in all_upper and "VAR_FNL_COUNT" in all_upper and "OPT_TYPE" in all_upper:
                    min_sz_clause = f" OR ISNULL([VAR_FNL_COUNT], 0) >= {min_sz}" if min_sz > 0 else ""
                    where.append(
                        f"(ISNULL([OPT_TYPE], '') != 'TBL' OR ISNULL([VAR_COUNT], 0) = 0 OR "
                        f"CAST(ISNULL([VAR_FNL_COUNT], 0) AS FLOAT) / NULLIF([VAR_COUNT], 0) >= {threshold}"
                        f"{min_sz_clause})"
                    )
                # Only listed OPTs (LISTING = 1)
                if "LISTING" in all_upper:
                    where.append("ISNULL(TRY_CAST([LISTING] AS INT), 1) = 1")
                # MAJ_CAT display capacity must be positive — if the store has
                # no display slots for this MAJ_CAT, the OPT can't be allocated.
                if "MJ_DISP_Q" in all_upper:
                    where.append("ISNULL(TRY_CAST([MJ_DISP_Q] AS FLOAT), 0) > 0")
                where_sql = (" WHERE " + " AND ".join(where)) if where else ""

                _run(wc, f"""
                    SELECT {col_list}
                    INTO [{FINAL_TABLE}]
                    FROM [{LISTING_TABLE}]
                    {where_sql}
                """)
                working_rows = wc.execute(text(f"SELECT COUNT(*) FROM [{FINAL_TABLE}]")).scalar()
                logger.info(f"{FINAL_TABLE}: {working_rows} rows (MSA_FNL_Q>0 OR HOLD_QTY>0, OPT_REQ_WH>=1, MJ_DISP_Q>0)")

                # ── Allocation Gate: MJ_MBQ growth headroom ───────────────
                # Lift the per-(WERKS, MAJ_CAT) target by scaling MJ_MBQ into
                # a sibling MJ_MBQ_REV column (MJ_MBQ itself is NEVER mutated
                # — kept as the immutable source-of-truth target).  MJ_REQ_REV
                # is then re-derived from the scaled MBQ as the unfilled-
                # portion of the new target.  When growth>100, MJ_REQ is
                # promoted to MJ_REQ_REV so every downstream engine consumer
                # (revalidate, OPT_MJ_REQ gate, store-broken pre-band, post-
                # waterfall recompute) sees the new ceiling with zero code
                # changes.  MJ_MBQ / MJ_REQ_ORIG remain visible for UI/audit.
                growth_pct = float(req.mj_req_growth_pct or 100.0)
                # Always create the revised-target columns so the UI sees them
                # consistently — at growth=100 they equal MJ_MBQ / MJ_REQ.
                for _rev_col in ("MJ_MBQ_REV", "MJ_REQ_REV", "MJ_REQ_ORIG"):
                    try:
                        _run(wc, f"ALTER TABLE [{FINAL_TABLE}] ADD [{_rev_col}] FLOAT NULL")
                    except Exception:
                        pass  # idempotent — column may already exist
                # Snapshot pre-scaling MJ_REQ exactly once (first run wins).
                _run(wc, f"""
                    UPDATE [{FINAL_TABLE}]
                    SET [MJ_REQ_ORIG] = [MJ_REQ]
                    WHERE [MJ_REQ_ORIG] IS NULL
                """)
                # MJ_MBQ_REV = MJ_MBQ × growth_pct/100 (MJ_MBQ untouched).
                _run(wc, f"""
                    UPDATE [{FINAL_TABLE}]
                    SET [MJ_MBQ_REV] = ROUND(ISNULL([MJ_MBQ], 0) * {growth_pct} / 100.0, 0)
                """)
                # MJ_REQ_REV = MAX(0, MJ_MBQ_REV − MJ_STK_TTL).  At growth=100
                # this equals the original MJ_REQ; above 100 it's the scaled
                # ceiling the engine should respect.
                _run(wc, f"""
                    UPDATE [{FINAL_TABLE}]
                    SET [MJ_REQ_REV] = CASE
                        WHEN ISNULL([MJ_MBQ_REV], 0) - ISNULL([MJ_STK_TTL], 0) > 0
                        THEN ROUND(ISNULL([MJ_MBQ_REV], 0) - ISNULL([MJ_STK_TTL], 0), 0)
                        ELSE 0 END
                """)
                if growth_pct != 100.0:
                    # Promote MJ_REQ_REV → MJ_REQ so the engine reads the
                    # scaled ceiling.  MJ_REQ_ORIG retains the original.
                    _run(wc, f"""
                        UPDATE [{FINAL_TABLE}] SET [MJ_REQ] = [MJ_REQ_REV]
                    """)
                    logger.info(
                        f"{FINAL_TABLE}: MJ_MBQ × {growth_pct}% → MJ_MBQ_REV; "
                        f"MJ_REQ_REV recomputed from scaled MBQ; "
                        f"MJ_REQ promoted to MJ_REQ_REV (MJ_REQ_ORIG preserved)"
                    )
                else:
                    logger.info(
                        f"{FINAL_TABLE}: growth=100% — MJ_MBQ_REV/MJ_REQ_REV "
                        f"populated for audit; MJ_REQ unchanged"
                    )

                # ── Add ARS_GRID_HIERARCHY columns to working table ────────
                # For each hierarchy column (RNG_SEG, MACRO_MVGR, etc.):
                #   Add new column H_{name} = (1 if {name}_REQ > 0 else 0) × hierarchy value
                # Existing REQ columns are NOT modified — H_ columns are added alongside.
                HIER_TABLE = "ARS_GRID_HIERARCHY"
                if _table_exists(wc, HIER_TABLE):
                    hier_cols = _get_columns(wc, HIER_TABLE)
                    work_cols_upper = {c.upper() for c in _get_columns(wc, FINAL_TABLE)}

                    add_cols = []
                    set_parts = []

                    # Load grid_group (Primary/Secondary) for each hierarchy column
                    # Map: last hierarchy col → grid_group from ARS_GRID_BUILDER
                    grid_groups = {}  # {HIER_COL_UPPER: grid_group}
                    _SKIP_ART = {"GEN_ART_NUMBER", "ARTICLE_NUMBER", "GEN_ART", "VAR_ART"}
                    try:
                        gb_rows = wc.execute(text(
                            "SELECT grid_name, hierarchy_columns, ISNULL(grid_group, 'None') "
                            "FROM [ARS_GRID_BUILDER] WHERE UPPER(status)='ACTIVE' ORDER BY seq"
                        )).fetchall()
                        for gn, hj, gg in gb_rows:
                            try:
                                h = json.loads(hj) if isinstance(hj, str) else hj
                            except Exception:
                                continue
                            if not h or len(h) < 2:
                                continue
                            # Skip article-level grids (same rule as hierarchy table)
                            if any(x.upper() in _SKIP_ART for x in h):
                                continue
                            last = h[-1].upper()
                            if last not in ("WERKS", "MAJ_CAT"):
                                grid_groups[last] = gg
                        # MJ grid → MAJ_CAT level
                        grid_groups["MJ"] = next(
                            (gg for gn, hj, gg in gb_rows if gn.upper() == "MJ"), "Primary"
                        )
                    except Exception:
                        pass
                    logger.info(f"Grid group mapping: {grid_groups}")

                    pri_gh = []   # GH_ col names for Primary grids
                    sec_gh = []   # GH_ col names for Secondary grids
                    pri_h = []    # H_ col names for Primary grids
                    sec_h = []    # H_ col names for Secondary grids

                    # ── Step 1: GH_MJ + all GH_ columns (raw hierarchy 0/1) ──
                    # GH_MJ = 1 if MAJ_CAT matched in hierarchy
                    gh_mj = "GH_MJ"
                    if gh_mj not in work_cols_upper:
                        try: _run(wc, f"ALTER TABLE [{FINAL_TABLE}] ADD [{gh_mj}] INT NULL DEFAULT 0")
                        except Exception: pass
                    set_parts.append(f"W.[{gh_mj}] = 1")  # always 1 (MJ base grid)
                    add_cols.append(gh_mj)
                    if grid_groups.get("MJ", "Primary") == "Primary":
                        pri_gh.append(gh_mj)
                    elif grid_groups.get("MJ") == "Secondary":
                        sec_gh.append(gh_mj)

                    for hc in hier_cols:
                        if hc.upper() == "MAJ_CAT":
                            continue
                        col = f"GH_{hc.upper()}"
                        if col not in work_cols_upper:
                            try: _run(wc, f"ALTER TABLE [{FINAL_TABLE}] ADD [{col}] INT NULL DEFAULT 0")
                            except Exception: pass
                        # If MAJ_CAT not in hierarchy → default GH to 1 (assume all grids apply)
                        set_parts.append(
                            f"W.[{col}] = CASE WHEN H.[MAJ_CAT] IS NULL THEN 1 "
                            f"ELSE ISNULL(TRY_CAST(H.[{hc}] AS INT), 0) END")
                        add_cols.append(col)
                        grp = grid_groups.get(hc.upper(), "None")
                        if grp == "Primary": pri_gh.append(col)
                        elif grp == "Secondary": sec_gh.append(col)

                    # ── Step 2: H_MJ + all H_ columns (REQ>0 × hierarchy) ──
                    h_mj = "H_MJ"
                    if h_mj not in work_cols_upper:
                        try: _run(wc, f"ALTER TABLE [{FINAL_TABLE}] ADD [{h_mj}] INT NULL DEFAULT 0")
                        except Exception: pass
                    mj_req = "MJ_REQ"
                    if mj_req in work_cols_upper:
                        set_parts.append(f"W.[{h_mj}] = CASE WHEN ISNULL(W.[{mj_req}], 0) > 0 THEN 1 ELSE 0 END")
                    else:
                        set_parts.append(f"W.[{h_mj}] = 1")
                    add_cols.append(h_mj)
                    if grid_groups.get("MJ", "Primary") == "Primary": pri_h.append(h_mj)
                    elif grid_groups.get("MJ") == "Secondary": sec_h.append(h_mj)

                    for hc in hier_cols:
                        if hc.upper() == "MAJ_CAT":
                            continue
                        col = f"H_{hc.upper()}"
                        req_col = f"{hc.upper()}_REQ"
                        if col not in work_cols_upper:
                            try: _run(wc, f"ALTER TABLE [{FINAL_TABLE}] ADD [{col}] INT NULL DEFAULT 0")
                            except Exception: pass
                        # If MAJ_CAT not in hierarchy → treat hierarchy as 1
                        hier_val = (f"CASE WHEN H.[MAJ_CAT] IS NULL THEN 1 "
                                    f"ELSE ISNULL(TRY_CAST(H.[{hc}] AS INT), 0) END")
                        if req_col in work_cols_upper:
                            set_parts.append(
                                f"W.[{col}] = CASE WHEN ISNULL(W.[{req_col}], 0) > 0 THEN 1 ELSE 0 END "
                                f"* ({hier_val})")
                        else:
                            set_parts.append(f"W.[{col}] = ({hier_val})")
                        add_cols.append(col)
                        grp = grid_groups.get(hc.upper(), "None")
                        if grp == "Primary": pri_h.append(col)
                        elif grp == "Secondary": sec_h.append(col)

                    # ── UPDATE 1: Set all GH_ and H_ columns ──────────────
                    if set_parts:
                        try:
                            _run(wc, f"""
                                UPDATE W SET {', '.join(set_parts)}
                                FROM [{FINAL_TABLE}] W
                                LEFT JOIN [{HIER_TABLE}] H WITH (NOLOCK)
                                    ON W.[MAJ_CAT] = H.[MAJ_CAT]
                            """)
                            logger.info(f"{FINAL_TABLE}: set {len(add_cols)} GH/H cols: {add_cols}")
                        except Exception as he:
                            logger.warning(f"{FINAL_TABLE}: GH/H columns failed: {he}")

                    # ── UPDATE 2: PRI_CT% and SEC_CT% (SEPARATE UPDATE so it reads
                    #    the just-written H_/GH_ values, not pre-update zeros) ──
                    pct_sets = []
                    for pct_col, h_list, gh_list in [
                        ("PRI_CT%", pri_h, pri_gh),
                        ("SEC_CT%", sec_h, sec_gh),
                    ]:
                        if pct_col not in work_cols_upper:
                            try: _run(wc, f"ALTER TABLE [{FINAL_TABLE}] ADD [{pct_col}] FLOAT NULL DEFAULT 0")
                            except Exception: pass
                        if h_list and gh_list:
                            h_sum = " + ".join(f"ISNULL([{c}], 0)" for c in h_list)
                            gh_sum = " + ".join(f"ISNULL([{c}], 0)" for c in gh_list)
                            pct_sets.append(
                                f"[{pct_col}] = CASE WHEN ({gh_sum}) = 0 THEN 0 "
                                f"ELSE ROUND(CAST(({h_sum}) AS FLOAT) / ({gh_sum}) * 100, 1) END")
                        else:
                            pct_sets.append(f"[{pct_col}] = 0")
                    if pct_sets:
                        try:
                            _run(wc, f"UPDATE [{FINAL_TABLE}] SET {', '.join(pct_sets)}")
                            logger.info(f"{FINAL_TABLE}: PRI_CT%/SEC_CT% calculated (pri_h={len(pri_h)}, sec_h={len(sec_h)})")
                        except Exception as pe:
                            logger.warning(f"{FINAL_TABLE}: PRI/SEC CT% failed: {pe}")

                    # ALLOC_FLAG: 1 if PRI_CT% = 100 (eligible for allocation), 0 = fallback
                    try:
                        _run(wc, f"ALTER TABLE [{FINAL_TABLE}] ADD [ALLOC_FLAG] INT NULL DEFAULT 0")
                    except Exception:
                        pass
                    try:
                        _run(wc, f"""
                            UPDATE [{FINAL_TABLE}]
                            SET [ALLOC_FLAG] = CASE WHEN ISNULL([PRI_CT%], 0) >= 100 THEN 1 ELSE 0 END
                        """)
                        logger.info(f"{FINAL_TABLE}: ALLOC_FLAG set (1=eligible, 0=fallback)")
                    except Exception as ae:
                        logger.warning(f"{FINAL_TABLE}: ALLOC_FLAG failed: {ae}")
                else:
                    logger.info(f"{FINAL_TABLE}: {HIER_TABLE} not found, skipping")

    except Exception as e:
        logger.warning(f"Auto-create {FINAL_TABLE} failed: {e}")
    t0 = _time_step(f"Part 7 (Working table + Hierarchy + ALLOC_FLAG → {working_rows} rows)", t0)

    # ── Part 8 — NEW rule engine (list OPTs + allocate VAR_ART × SZ) ──
    # Spec: docs/NEW_RULE_ENGINE_SPEC.md
    # Old rule_engine.py / listing_allocator.py are preserved for reference
    # but no longer called. The `if False` block below documents the old call
    # site so reviewers can compare signatures.
    if False:  # OLD — kept for reference only
        from app.services.rule_engine import run_rule_based_allocation
        with de.connect() as ac:
            _ = run_rule_based_allocation(
                conn=ac,
                final_table=FINAL_TABLE,
                alloc_table=ALLOC_TABLE,
                size_threshold=req.stock_threshold_pct,
            )

    alloc_rows = 0
    alloc_batch_id = None
    alloc_failed_count = 0
    mode = (req.allocation_mode or "pandas").lower()
    n_workers = max(2, min(8, int(req.parallel_workers or 4)))
    _growth = req.mj_req_growth_pct
    try:
        if mode == "pandas":
            from app.services.rule_engine_pandas import (
                run_listing_and_allocation_pandas,
            )
            alloc_result = run_listing_and_allocation_pandas(
                working_table=FINAL_TABLE,
                listed_table="ARS_LISTED_OPT",
                alloc_table=ALLOC_TABLE,
                n_workers=n_workers,
                batch_id=preset_batch_id,
                size_threshold=req.stock_threshold_pct,
                min_size_count=req.min_size_count,
                pri_ct_check_rl=req.pri_ct_check_rl,
                pri_ct_check_tbc=req.pri_ct_check_tbc,
                rl_mbq_cap_pct=_growth,
                tbc_mbq_cap_pct=_growth,
                tbl_mbq_cap_pct=_growth,
                rl_mj_req_cap_pct=req.rl_mj_req_cap_pct,
                tbc_mj_req_cap_pct=req.tbc_mj_req_cap_pct,
                tbl_mj_req_cap_pct=req.tbl_mj_req_cap_pct,
                mj_req_growth_pct=_growth,
                opt_types=req.opt_types or ["RL", "TBC", "TBL"],
                use_writer_queue=req.use_writer_queue,
                apply_sec_cap_in_normal=req.apply_sec_cap_in_normal,
            )
        else:  # "sequential" — single-thread reference path
            from app.services.rule_engine_new import run_listing_and_allocation
            with de.connect() as ac:
                alloc_result = run_listing_and_allocation(
                    conn=ac,
                    working_table=FINAL_TABLE,
                    listed_table="ARS_LISTED_OPT",
                    alloc_table=ALLOC_TABLE,
                    size_threshold=req.stock_threshold_pct,
                    min_size_count=req.min_size_count,
                    pri_ct_check_rl=req.pri_ct_check_rl,
                    pri_ct_check_tbc=req.pri_ct_check_tbc,
                    rl_mbq_cap_pct=_growth,
                    tbc_mbq_cap_pct=_growth,
                    tbl_mbq_cap_pct=_growth,
                    rl_mj_req_cap_pct=req.rl_mj_req_cap_pct,
                    tbc_mj_req_cap_pct=req.tbc_mj_req_cap_pct,
                    tbl_mj_req_cap_pct=req.tbl_mj_req_cap_pct,
                    mj_req_growth_pct=_growth,
                    opt_types=req.opt_types or ["RL", "TBC", "TBL"],
                    apply_sec_cap_in_normal=req.apply_sec_cap_in_normal,
                )
        alloc_rows = alloc_result.get("alloc_rows", 0)
        alloc_batch_id = alloc_result.get("batch_id")
        alloc_failed_count = alloc_result.get("failed", 0) or 0
        # Engine returned but reported per-MAJ_CAT failures → surface as FAILED
        # so the parked-runs UI doesn't show a green SUCCESS for a broken run.
        if alloc_failed_count > 0:
            summary["error"] = (
                f"rule engine ({mode}): {alloc_failed_count} MAJ_CAT(s) failed"
            )
    except Exception as e:
        logger.exception(f"Rule engine ({mode}) failed: {e}")
        summary["error"] = f"rule engine ({mode}) raised: {e}"
    t0 = _time_step(
        f"Part 8 ({mode}, workers={n_workers} → {alloc_rows} alloc rows, "
        f"failed={alloc_failed_count}, batch={alloc_batch_id})",
        t0,
    )

    # ── Part 8.4 — Park ARS_ALLOC_WORKING + ARS_LISTING_WORKING for review ──
    # Snapshot the freshly-built allocation AND the working listing into
    # their parked tables tagged with this session_id. The user reviews
    # them in the UI and either promotes both to history (Approve) or
    # marks both as REJECTED.
    # Failures here do NOT fail the run — listing succeeded, parking is
    # bookkeeping. The session row carries PARKED_STATUS so the UI can
    # surface a warning when the snapshot was skipped.
    parked_status = "PARKED"
    try:
        snap = parked_history.snapshot_session_to_parked(session_id)
        # Aggregate result: at least one target parked rows → PARKED;
        # all targets had errors → SKIPPED_ERROR; all targets were empty
        # or already parked with zero rows → SKIPPED_EMPTY.
        if not snap.get("any_parked"):
            parked_status = (
                "SKIPPED_ERROR" if snap.get("any_error") else "SKIPPED_EMPTY"
            )
        elif snap.get("any_error"):
            # Partial success — at least one target parked, but another
            # failed. Treat as SKIPPED_ERROR so the UI surfaces a warning;
            # the partial parked rows are still safely recorded under
            # this session_id.
            parked_status = "SKIPPED_ERROR"
        logger.info(
            f"[generate] parked snapshot: total_rows={snap.get('total_parked_rows')} "
            f"by_table={snap.get('by_table')} parked_status={parked_status}"
        )
    except Exception as e:
        logger.warning(f"[generate] parked snapshot failed: {e}")
        parked_status = "SKIPPED_ERROR"
    summary["parked_status"] = parked_status
    t0 = _time_step("Part 8.4 (park alloc + listing snapshots)", t0)

    # ── Part 8.5 — OPT_STATUS post-alloc classification + TBL_LISTED_DATE ──
    # Rule (evaluated on post-alloc stock = STK_TTL + ALLOC_QTY):
    #   RL                                              → RL
    #   TBC alloc>0  & (STK+ALC) >= thr × eff_ACS_D     → RL    else MIX
    #   TBC alloc=0                                      → MIX
    #   TBL alloc>0  & (STK+ALC) >= thr × eff_ACS_D     → NL    else TBL
    #   TBL alloc=0                                      → TBL
    # TBL_LISTED_DATE = GETDATE() when OPT_TYPE='TBL' AND ALLOC_QTY>0.
    try:
        thr = float(req.stock_threshold_pct or 0.6)
        default_acs = float(req.default_acs_d or 18.0)
        with de.connect() as ac:
            # Guard: ALLOC_QTY only exists on FINAL_TABLE after the rule engine's
            # Stage A runs (_stage_a_add_columns). If the engine failed in Part 8
            # (e.g. kwarg mismatch, schema drift), the column is missing and this
            # UPDATE would fail with a confusing "Invalid column name 'ALLOC_QTY'"
            # SQL error. Surface the upstream failure clearly instead.
            alloc_qty_exists = ac.execute(text(
                "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME = :t AND COLUMN_NAME = 'ALLOC_QTY'"
            ), {"t": FINAL_TABLE}).fetchone()
            if not alloc_qty_exists:
                logger.warning(
                    f"Part 8.5 skipped: ALLOC_QTY column missing on {FINAL_TABLE} "
                    f"(rule engine likely failed in Part 8 — check warnings above)"
                )
                raise RuntimeError("ALLOC_QTY missing — engine failure upstream")
            for coldef in ("[OPT_STATUS] NVARCHAR(10) NULL",
                            "[TBL_LISTED_DATE] DATETIME NULL"):
                try:
                    _run(ac, f"ALTER TABLE [{FINAL_TABLE}] ADD {coldef}")
                except Exception:
                    pass  # column exists
            _run(ac, f"""
                UPDATE [{FINAL_TABLE}] SET
                  [OPT_STATUS] = CASE
                    WHEN [OPT_TYPE] = 'RL' THEN 'RL'
                    WHEN [OPT_TYPE] = 'TBC' AND ISNULL([ALLOC_QTY],0) > 0
                         AND ISNULL([STK_TTL],0) + ISNULL([ALLOC_QTY],0)
                             >= {thr} * ISNULL(NULLIF([ACS_D],0), {default_acs})
                        THEN 'RL'
                    WHEN [OPT_TYPE] = 'TBC' AND ISNULL([ALLOC_QTY],0) > 0
                        THEN 'MIX'
                    WHEN [OPT_TYPE] = 'TBC' THEN 'MIX'
                    WHEN [OPT_TYPE] = 'TBL' AND ISNULL([ALLOC_QTY],0) > 0
                         AND ISNULL([STK_TTL],0) + ISNULL([ALLOC_QTY],0)
                             >= {thr} * ISNULL(NULLIF([ACS_D],0), {default_acs})
                        THEN 'NL'
                    WHEN [OPT_TYPE] = 'TBL' AND ISNULL([ALLOC_QTY],0) > 0
                        THEN 'TBL'
                    WHEN [OPT_TYPE] = 'TBL' THEN 'TBL'
                    ELSE ISNULL([OPT_TYPE], 'MIX')
                  END,
                  [TBL_LISTED_DATE] = CASE
                    WHEN [OPT_TYPE] = 'TBL' AND ISNULL([ALLOC_QTY],0) > 0
                         AND [TBL_LISTED_DATE] IS NULL
                        THEN GETDATE()
                    ELSE [TBL_LISTED_DATE]
                  END
            """)
    except Exception as e:
        logger.warning(f"OPT_STATUS post-processing failed: {e}")
    t0 = _time_step("Part 8.5 (OPT_STATUS + TBL_LISTED_DATE)", t0)

    # ── Part 8.6 — NL/TBL hold-tracking table (persistent, WERKS × VAR_ART × SZ) ─
    # NOTE: Hold-tracking WRITES (Step A decrement of consumed RL/TBC,
    # Step B MERGE of new TBL holds) used to happen here during generate.
    # They've been moved to parked_history.approve_parked so HOLD commits
    # at the same lifecycle event as PEND_ALC — symmetric, and means a
    # rejected listing leaves no trace in ARS_NL_TBL_HOLD_TRACKING.
    #
    # What remains in this Part 8.6 block: the DDL guards (CREATE TABLE,
    # ALTER ADD RDC, indexes, FROM_HOLD_QTY column on alloc table) plus a
    # one-time RDC backfill from the store master. The actual data writes
    # are deferred until approve.
    try:
        with de.connect() as ac:
            _run(ac, """
                IF OBJECT_ID('ARS_NL_TBL_HOLD_TRACKING','U') IS NULL
                CREATE TABLE [ARS_NL_TBL_HOLD_TRACKING] (
                    [WERKS]           NVARCHAR(50)  NOT NULL,
                    [RDC]             NVARCHAR(20)  NULL,
                    [MAJ_CAT]         NVARCHAR(200) NULL,
                    [GEN_ART_NUMBER]  BIGINT        NULL,
                    [CLR]             NVARCHAR(200) NULL,
                    [VAR_ART]         BIGINT        NOT NULL,
                    [SZ]              NVARCHAR(50)  NOT NULL,
                    [OPT_STATUS]      NVARCHAR(10)  NULL,
                    [LISTED_DATE]     DATETIME      NOT NULL DEFAULT GETDATE(),
                    [HOLD_QTY_INITIAL] FLOAT        NOT NULL DEFAULT 0,
                    [HOLD_REM]        FLOAT         NOT NULL DEFAULT 0,
                    [LAST_UPDATED]    DATETIME      NOT NULL DEFAULT GETDATE(),
                    [IS_CLOSED]       BIT           NOT NULL DEFAULT 0,
                    [CLOSED_DATE]     DATETIME      NULL,
                    CONSTRAINT PK_ARS_NL_TBL_HOLD_TRACKING
                        PRIMARY KEY CLUSTERED ([WERKS], [VAR_ART], [SZ])
                )
            """)
            # Add RDC column if the table predates this change (idempotent).
            # Storing RDC directly avoids the WERKS→RDC join with the store
            # master every time MSA needs to reseed HOLD_QTY.
            try:
                _run(ac, """
                    IF NOT EXISTS (
                        SELECT 1 FROM sys.columns
                        WHERE object_id = OBJECT_ID('ARS_NL_TBL_HOLD_TRACKING')
                          AND name = 'RDC'
                    )
                    ALTER TABLE [ARS_NL_TBL_HOLD_TRACKING] ADD [RDC] NVARCHAR(20) NULL
                """)
            except Exception:
                pass

            # Backfill RDC on any rows still NULL by joining the store master.
            # Idempotent — only touches rows where RDC IS NULL.
            try:
                _run(ac, """
                    IF EXISTS (SELECT 1 FROM sys.columns
                               WHERE object_id = OBJECT_ID('ARS_NL_TBL_HOLD_TRACKING')
                                 AND name = 'RDC')
                    AND OBJECT_ID('Master_ALC_INPUT_ST_MASTER','U') IS NOT NULL
                    BEGIN
                        UPDATE T SET T.[RDC] = S.[RDC]
                        FROM [ARS_NL_TBL_HOLD_TRACKING] T
                        INNER JOIN [Master_ALC_INPUT_ST_MASTER] S
                            ON S.[ST_CD] = T.[WERKS]
                        WHERE T.[RDC] IS NULL OR T.[RDC] = ''
                    END
                """)
            except Exception:
                pass

            try:
                _run(ac, """
                    IF NOT EXISTS (SELECT 1 FROM sys.indexes
                                   WHERE name='IX_NLTBL_OPEN'
                                     AND object_id=OBJECT_ID('ARS_NL_TBL_HOLD_TRACKING'))
                    CREATE NONCLUSTERED INDEX IX_NLTBL_OPEN
                        ON [ARS_NL_TBL_HOLD_TRACKING] ([IS_CLOSED])
                        INCLUDE ([HOLD_REM])
                """)
            except Exception:
                pass

            # DDL guard — FROM_HOLD_QTY written by the allocation engine.
            try:
                _run(ac, f"""
                    IF NOT EXISTS (
                        SELECT 1 FROM sys.columns
                        WHERE object_id = OBJECT_ID('{ALLOC_TABLE}')
                          AND name = 'FROM_HOLD_QTY'
                    )
                    ALTER TABLE [{ALLOC_TABLE}] ADD [FROM_HOLD_QTY] FLOAT NULL
                """)
            except Exception:
                pass

            # STEP A / STEP B / MSA HOLD sync — REMOVED from generate.
            # All three are now executed during approve_parked so the HOLD
            # axis commits at the same point as PEND_ALC (symmetric design,
            # rejected listings leave no trace in hold tracking).
            # See parked_history._apply_hold_tracking_from_history.
    except Exception as e:
        logger.warning(f"NL/TBL hold tracking schema setup failed: {e}")
    t0 = _time_step("Part 8.6 (NL/TBL hold tracking schema)", t0)

    duration = round(time.time() - start, 1)
    logger.info(f"ARS_LISTING: {total} rows (grid={grid_count}, new={new_count}) in {duration}s")

    # Summary of all step timings
    logger.info("="*60)
    logger.info("STEP TIMINGS SUMMARY:")
    for st in step_timings:
        logger.info(f"  {st['step']:<45} {st['seconds']:>7}s")
    logger.info(f"  {'TOTAL':<45} {duration:>7}s")
    logger.info("="*60)

    # Post-run sweep: count rows in each tracked table and classify the
    # action relative to its pre-run existence. Single chokepoint — no
    # per-stage instrumentation. Surfaced in the session row's
    # TABLES_AFFECTED column for the UI's completion panel.
    tables_affected: List[Dict[str, Any]] = []
    try:
        tables_affected = parked_history.tables_affected_summary(pre_existence)
    except Exception as e:
        logger.warning(f"[generate] tables_affected sweep failed: {e}")

    # Feed the session record before returning so the UI's session list
    # shows correct totals + step timings.
    summary.update({
        "duration_sec":   duration,
        "alloc_rows":     alloc_rows,
        "ship_qty_total": (alloc_result.get("ship_qty_total")
                           if 'alloc_result' in locals() else None),
        "hold_qty_total": (alloc_result.get("hold_qty_total")
                           if 'alloc_result' in locals() else None),
        "listed_opts":    (alloc_result.get("listed_opts")
                           if 'alloc_result' in locals() else None),
        "failed_majcats": alloc_failed_count,
        "step_timings":   step_timings,
        "tables_affected": tables_affected,
    })

    return {
        "success": True,
        "message": (f"{pipeline_msg}Listing: {total:,} rows ({grid_count:,} grid + {new_count:,} new) "
                    f"| Working: {working_rows:,} | Alloc: {alloc_rows:,} | MIX={mixl_count}, TBL={tbl_count}, TBC={toc_count}, RL={rl_count} in {duration}s"),
        "data": {
            "total_rows": total, "existing_rows": grid_count,
            "new_rows": new_count, "working_rows": working_rows, "alloc_rows": alloc_rows,
            "duration_sec": duration,
            "stock_columns": len(stock_cols),
            "opt_type": {
                "MIX": mixl_count, "TBL": tbl_count, "TBC": toc_count,
                "RL": rl_count, "NL": nl_count, "untagged": untagged,
            },
            "step_timings": step_timings,
            "session_id":   session_id,
            # Parallel allocation tracking — None for sequential mode.
            "allocation_mode":  mode,
            "parallel_workers": n_workers if mode != "sequential" else None,
            "alloc_batch_id":   alloc_batch_id,
            "alloc_failed":     alloc_failed_count,
        }
    }


# ===========================================================================
# PARALLEL ALLOCATION — progress poll, retry, recent batches
# ===========================================================================

@router.get("/alloc-progress")
def alloc_progress(batch_id: str,
                   current_user: User = Depends(get_current_user)):
    """
    Live progress for a parallel allocation run. The UI polls this every
    few seconds while a Generate is in flight, then once more after it
    completes to populate the Failed list.
    """
    from app.services.alloc_queue import (
        get_failed_list, get_progress, get_done_summary,
    )
    de = get_data_engine()
    with de.connect() as conn:
        return {
            "success":  True,
            "progress": get_progress(conn, batch_id),
            "failed":   get_failed_list(conn, batch_id),
            "summary":  get_done_summary(conn, batch_id),
        }


class RetryFailedRequest(BaseModel):
    batch_id:         str
    allocation_mode:  str = "pandas"  # "sequential" | "pandas"
    parallel_workers: int = 8


@router.post("/retry-failed")
def retry_failed(req: RetryFailedRequest,
                 current_user: User = Depends(get_current_user)):
    """
    Manual retry path. Resets every FAILED row in the batch back to
    PENDING (with ATTEMPTS=0 so the auto-retry budget is restored),
    then re-dispatches workers on those MAJ_CATs only. Parts 1-7 are
    NOT re-run — the existing ARS_LISTING_WORKING / ARS_ALLOC_WORKING
    are reused.

    Defensive against the "no action" UX trap: if a previous retry
    click already moved the rows to PENDING and a worker is already
    chewing through them, this endpoint won't re-spawn another pool.
    Instead it tells the caller exactly what state the batch is in so
    the UI can show a meaningful message.
    """
    from app.services.alloc_queue import (
        get_failed_list, reset_failed_for_retry, get_progress,
    )
    de = get_data_engine()
    with de.connect() as conn:
        failed = get_failed_list(conn, req.batch_id)
        progress_before = get_progress(conn, req.batch_id)

        if not failed:
            # Nothing currently FAILED. If there are PENDING/IN_PROGRESS
            # rows it means a prior retry click is still working — surface
            # that to the user instead of pretending we did something.
            in_flight = (
                int(progress_before.get("pending", 0))
                + int(progress_before.get("in_progress", 0))
            )
            if in_flight > 0:
                return {
                    "success":  True,
                    "retried":  0,
                    "message":  (
                        f"Nothing to retry — {in_flight} MAJ_CAT(s) are still "
                        f"running from an earlier retry/run. Wait for them to "
                        f"finish, then retry again if any fail."
                    ),
                    "progress": progress_before,
                }
            raise HTTPException(400, "No failed MAJ_CATs to retry for this batch_id")

        failed_mcs = [f["maj_cat"] for f in failed]
        reset_count = reset_failed_for_retry(conn, req.batch_id)

    logger.info(
        f"[retry-failed] batch={req.batch_id} mode={req.allocation_mode} "
        f"workers={req.parallel_workers} → re-dispatching {reset_count} "
        f"MAJ_CAT(s): {failed_mcs[:10]}{'...' if len(failed_mcs) > 10 else ''}"
    )

    n_workers = max(2, min(8, int(req.parallel_workers or 4)))
    from app.services.rule_engine_pandas import run_listing_and_allocation_pandas
    result = run_listing_and_allocation_pandas(
        n_workers=n_workers,
        batch_id=req.batch_id,
        only_majcats=failed_mcs,
    )

    # Re-read progress so the UI can update without a separate poll round-trip.
    with de.connect() as conn:
        progress_after = get_progress(conn, req.batch_id)
        failed_after = get_failed_list(conn, req.batch_id)

    logger.info(
        f"[retry-failed] batch={req.batch_id} done: "
        f"done={progress_after.get('done')} failed={progress_after.get('failed')}"
    )

    return {
        "success":         True,
        "retried":         len(failed_mcs),
        "still_failed":    len(failed_after),
        "progress":        progress_after,
        "failed":          failed_after,
        "result":          result,
    }


@router.get("/alloc-batches")
def alloc_batches(limit: int = 20,
                  current_user: User = Depends(get_current_user)):
    """List recent allocation batches for the UI history panel."""
    from app.services.alloc_queue import list_recent_batches
    de = get_data_engine()
    with de.connect() as conn:
        return {
            "success": True,
            "batches": list_recent_batches(conn, limit=limit),
        }


# ---------------------------------------------------------------------------
# Parked alloc runs — review queue (snapshot of ARS_ALLOC_WORKING per session)
# ---------------------------------------------------------------------------
class _RejectParkedRequest(BaseModel):
    note: Optional[str] = None


@router.get("/parked-runs")
def list_parked_runs(include_rejected: bool = False,
                     current_user: User = Depends(get_current_user)):
    """List sessions whose alloc snapshot is still parked (awaiting review).
    Pass `include_rejected=true` to also see sessions the user already
    rejected (kept for audit until TTL purge)."""
    from app.services import parked_history
    return {
        "success": True,
        "runs": parked_history.list_parked_runs(
            include_rejected=bool(include_rejected)
        ),
    }


@router.get("/parked-runs/{session_id}")
def get_parked_run_detail(session_id: str,
                          page: int = 1,
                          page_size: int = 100,
                          which: str = "alloc",
                          current_user: User = Depends(get_current_user)):
    """Paginated detail rows for one parked session (for review UI).
    `which` selects which parked table to read:
      - 'alloc'   → ARS_ALLOC_PARKED (default)
      - 'listing' → ARS_LISTING_WORKING_PARKED
    """
    from app.services import parked_history
    return {
        "success": True,
        "data": parked_history.get_parked_detail(
            session_id, page=page, page_size=page_size, which=which
        ),
    }


@router.post("/parked-runs/{session_id}/approve")
def approve_parked_run(session_id: str,
                       current_user: User = Depends(get_current_user)):
    """Promote a parked session into ARS_ALLOC_HISTORY. Idempotent — a
    duplicate call returns `{already_approved: true}` without inserting."""
    from app.services import parked_history
    user = getattr(current_user, "username", None) or "user"
    result = parked_history.approve_parked(session_id, user=user)
    if result.get("error") and not result.get("already_approved"):
        raise HTTPException(400, result["error"])
    return {"success": True, **result}


@router.post("/parked-runs/{session_id}/reject")
def reject_parked_run(session_id: str,
                      body: _RejectParkedRequest,
                      current_user: User = Depends(get_current_user)):
    """Mark a parked session as REJECTED. Rows stay in ARS_ALLOC_PARKED
    (kept for audit until TTL purge)."""
    from app.services import parked_history
    user = getattr(current_user, "username", None) or "user"
    result = parked_history.reject_parked(
        session_id, user=user, note=body.note
    )
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return {"success": True, **result}


@router.get("/alloc-history")
def alloc_history(session_id: Optional[str] = None,
                  date_from: Optional[str] = None,
                  date_to:   Optional[str] = None,
                  page: int = 1, page_size: int = 100,
                  current_user: User = Depends(get_current_user)):
    """Query approved alloc history (ARS_ALLOC_HISTORY). Filter by
    session_id (exact) OR APPROVED_AT date range."""
    from app.services import parked_history
    return {
        "success": True,
        "data": parked_history.list_alloc_history(
            session_id=session_id,
            date_from=date_from, date_to=date_to,
            page=page, page_size=page_size,
        ),
    }


@router.get("/listing-history")
def listing_history(session_id: Optional[str] = None,
                    date_from: Optional[str] = None,
                    date_to:   Optional[str] = None,
                    page: int = 1, page_size: int = 100,
                    current_user: User = Depends(get_current_user)):
    """Query approved listing-working history (ARS_LISTING_WORKING_HISTORY).
    Filter by session_id (exact) OR APPROVED_AT date range."""
    from app.services import parked_history
    return {
        "success": True,
        "data": parked_history.list_listing_history(
            session_id=session_id,
            date_from=date_from, date_to=date_to,
            page=page, page_size=page_size,
        ),
    }


@router.post("/parked-runs/purge")
def purge_parked_runs(current_user: User = Depends(get_current_user)):
    """Maintenance: delete PARKED rows older than 14d, REJECTED rows older
    than 30d, and approved history rows older than allocation.history_retention_days
    (default 30d, 0 = keep forever). Configurable via Settings → Allocation."""
    from app.services import parked_history
    return {"success": True, **parked_history.purge_old_parked()}


# ---------------------------------------------------------------------------
# Listing sessions — per-run header rows + per-session log files
# ---------------------------------------------------------------------------
@router.get("/sessions")
def list_listing_sessions(limit:  int           = 100,
                          status: Optional[str] = None,
                          mode:   Optional[str] = None,
                          user:   Optional[str] = None,
                          current_user: User = Depends(get_current_user)):
    """
    Recent /listing/generate runs. Drives the Logs page session selector.
    Filters: status (RUNNING/SUCCESS/FAILED), mode (sequential/python_parallel/
    sql_parallel), user (username).
    """
    from app.services.listing_sessions import list_sessions
    return {
        "success":  True,
        "sessions": list_sessions(limit=limit, status=status, mode=mode, user=user),
    }


@router.get("/sessions/{session_id}")
def get_listing_session(session_id: str,
                        current_user: User = Depends(get_current_user)):
    """Full metadata for one session — request params, step timings, errors."""
    from app.services.listing_sessions import get_session
    s = get_session(session_id)
    if not s:
        raise HTTPException(404, f"Session {session_id} not found")
    return {"success": True, "session": s}


@router.get("/sessions/{session_id}/log")
def get_listing_session_log(session_id: str,
                            tail: Optional[int] = None,
                            current_user: User = Depends(get_current_user)):
    """
    Return the per-session loguru log as plain text.
    `tail=N` returns only the last N lines (cheap for huge runs).
    """
    from app.services.listing_sessions import get_session_log
    content = get_session_log(session_id, tail_lines=tail)
    if content is None:
        raise HTTPException(404, f"Log for session {session_id} not found")
    return {
        "success":    True,
        "session_id": session_id,
        "tail":       tail,
        "log":        content,
        "size_bytes": len(content),
    }


@router.post("/sessions/{session_id}/kill")
def kill_listing_session(session_id: str,
                         current_user: User = Depends(get_current_user)):
    """
    Force-terminate a RUNNING session: marks the session row FAILED and
    cancels any PENDING/IN_PROGRESS queue rows linked to its batch_id.
    Use when a run has hung or you want to stop it from the Logs page.
    """
    from app.services.listing_sessions import kill_session
    try:
        result = kill_session(
            session_id,
            reason=f"killed by {getattr(current_user, 'username', 'user')}",
        )
    except Exception as e:
        raise HTTPException(500, f"kill failed: {e}")
    return {"success": True, **result}


@router.delete("/sessions/{session_id}")
def delete_listing_session(session_id: str,
                           current_user: User = Depends(get_current_user)):
    """
    Permanently delete a finished session header row and its log file.
    Refuses to delete a session whose status is still RUNNING — kill it
    via POST /sessions/{id}/kill first.
    """
    from app.services.listing_sessions import delete_session
    try:
        result = delete_session(session_id)
    except RuntimeError as e:
        # Most common case: trying to delete a still-running session.
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(500, f"delete failed: {e}")
    return {"success": True, **result}


class CancelBatchRequest(BaseModel):
    batch_id: str


@router.post("/cancel-batch")
def cancel_batch(req: CancelBatchRequest,
                 current_user: User = Depends(get_current_user)):
    """
    HARD-cancel a running allocation batch. Four-step kill:

      1. Set the in-process cancel event so worker threads exit before
         claiming any new MAJ_CAT.
      2. KILL each worker's SQL Server SPID so any in-flight UPDATE
         terminates immediately (best-effort: needs ALTER ANY CONNECTION
         on the app login).
      3. Mark every PENDING / IN_PROGRESS queue row as FAILED with
         ERROR_MSG='cancelled by <user>'.
      4. Mark the matching ARS_LISTING_SESSIONS row as FAILED if it's
         still RUNNING.

    DONE rows are untouched — partial results stay intact.
    """
    from app.services import alloc_cancellation as ac
    from app.services.alloc_queue import QUEUE_TABLE
    from app.services.listing_sessions import SESSIONS_TABLE

    user = getattr(current_user, "username", "user")

    # Step 1+2: signal the threads + KILL their SQL sessions.
    cancel_info = ac.hard_cancel(req.batch_id)

    de = get_data_engine()
    with de.connect() as conn:
        # Step 3: mark queue rows CANCELLED (terminal state).
        # Crucially NOT 'FAILED' — a 'FAILED' row would be re-claimable by
        # claim_next (STATUS='FAILED' AND ATTEMPTS<MAX) and resurrectable
        # by mark_in_progress, defeating the cancel. CANCELLED is excluded
        # from both filters so workers can never pick the rows back up.
        # Also covers FAILED rows in the same batch (if a deadlock-failed
        # row was about to be auto-retried, freeze it permanently too).
        res = conn.execute(text(f"""
            UPDATE {QUEUE_TABLE}
               SET STATUS       = 'CANCELLED',
                   COMPLETED_AT = GETDATE(),
                   ERROR_MSG    = :msg
             WHERE BATCH_ID = :b
               AND STATUS IN ('PENDING','IN_PROGRESS','FAILED')
        """), {"b": req.batch_id, "msg": f"cancelled by {user}"})
        cancelled = int(res.rowcount or 0)

        # Step 4: mark session row CANCELLED so the orchestrator's post-
        # Part-8 check sees it and short-circuits before Part 8.4 / 8.5 /
        # 8.6 / parking. (batch_id == session_id in async path.)
        try:
            conn.execute(text(f"""
                UPDATE {SESSIONS_TABLE}
                   SET STATUS       = 'CANCELLED',
                       COMPLETED_AT = GETDATE(),
                       ERROR_MSG    = :msg
                 WHERE SESSION_ID = :sid
                   AND STATUS = 'RUNNING'
            """), {"sid": req.batch_id, "msg": f"cancelled by {user}"})
        except Exception as e:
            logger.warning(f"[cancel-batch] session update skipped: {e}")
        conn.commit()

    logger.warning(
        f"[cancel-batch] batch={req.batch_id} cancelled_by={user} "
        f"queue_rows={cancelled} kill_attempted={cancel_info['kill_attempted']} "
        f"killed={cancel_info.get('killed')} "
        f"kill_failed={len(cancel_info.get('kill_failed', []))}"
    )
    return {
        "success":         True,
        "batch_id":        req.batch_id,
        "cancelled":       cancelled,
        "kill_attempted":  cancel_info["kill_attempted"],
        "killed":          cancel_info.get("killed", []),
        "kill_failed":     cancel_info.get("kill_failed", []),
        "event_set":       cancel_info.get("event_set"),
    }


@router.get("/active-job")
def active_job(current_user: User = Depends(get_current_user)):
    """
    Detect any in-flight allocation batch on the server. Used by the UI to
    pick up a Python job already running in the backend (e.g. when the
    user lands on the page mid-run, or after a refresh that drops the
    locally-cached batch_id).

    Returns the most-recent batch with PENDING / IN_PROGRESS rows. If no
    such batch exists, returns the latest completed batch (so the UI can
    still show the last result). Stage is inferred from queue state:
        no queue rows yet         -> "listing"   (Stage A/B in flight)
        any IN_PROGRESS / PENDING -> "alloc"     (Stage C waterfall)
        all DONE/FAILED           -> "complete"  (Stage D done)
    """
    from app.services.alloc_queue import (
        QUEUE_TABLE, get_progress, get_failed_list, get_done_summary,
    )
    # Stale-batch thresholds (in minutes).
    #   STALE_MIN: how long a PENDING/IN_PROGRESS row may sit without ANY
    #     queue activity (PICKED_AT update) before it's treated as abandoned.
    #     Workers usually claim within seconds, so 10 min is generous.
    #   ORPHAN_PEND_MIN: a queue with NO PICKED_AT at all (workers never
    #     started) is considered an orphan after this much time. Lower
    #     because Stage A/B normally take a few minutes max before workers
    #     begin Stage C.
    STALE_MIN = 10
    ORPHAN_PEND_MIN = 5
    de = get_data_engine()
    with de.connect() as conn:
        # Skip if queue table doesn't exist yet (first-ever install).
        exists = conn.execute(text(
            "SELECT 1 FROM sys.tables WHERE name = :t"
        ), {"t": QUEUE_TABLE}).fetchone()
        if not exists:
            return {"success": True, "active": None, "last": None}

        # ── Auto-fail abandoned rows so this endpoint stops reporting them.
        # Two cases:
        #   1. IN_PROGRESS with no PICKED_AT update in STALE_MIN min — the
        #      worker probably crashed / connection dropped.
        #   2. PENDING in a batch that has NEVER been claimed (no row has
        #      PICKED_AT) and was created > ORPHAN_PEND_MIN min ago — the
        #      caller (e.g. the synchronous /listing/generate request)
        #      errored before workers could start Stage C.
        try:
            conn.execute(text(f"""
                UPDATE {QUEUE_TABLE}
                   SET STATUS       = 'FAILED',
                       COMPLETED_AT = GETDATE(),
                       ERROR_MSG    = 'auto-cancelled (stale, no worker activity)'
                 WHERE STATUS = 'IN_PROGRESS'
                   AND DATEDIFF(MINUTE, ISNULL(PICKED_AT, CREATED_AT), GETDATE()) > :stale
            """), {"stale": STALE_MIN})
            conn.execute(text(f"""
                UPDATE q
                   SET STATUS       = 'FAILED',
                       COMPLETED_AT = GETDATE(),
                       ERROR_MSG    = 'auto-cancelled (orphan, never claimed)'
                  FROM {QUEUE_TABLE} q
                  JOIN (
                       SELECT BATCH_ID
                         FROM {QUEUE_TABLE}
                        GROUP BY BATCH_ID
                        HAVING MAX(PICKED_AT) IS NULL
                           AND DATEDIFF(MINUTE, MIN(CREATED_AT), GETDATE()) > :orphan
                       ) o ON o.BATCH_ID = q.BATCH_ID
                 WHERE q.STATUS = 'PENDING'
            """), {"orphan": ORPHAN_PEND_MIN})
            conn.commit()
        except Exception as exc:
            logger.warning(f"[active-job] stale-batch sweep failed: {exc}")

        # 1) Try to find a batch with open work (most recent first).
        row = conn.execute(text(f"""
            SELECT TOP 1 BATCH_ID,
                   MIN(CREATED_AT)        AS started_at,
                   MAX(ALLOCATION_MODE)   AS mode,
                   MAX(PICKED_AT)         AS last_pick
            FROM {QUEUE_TABLE}
            WHERE STATUS IN ('PENDING','IN_PROGRESS')
            GROUP BY BATCH_ID
            ORDER BY MIN(CREATED_AT) DESC
        """)).fetchone()
        is_active = row is not None
        if not row:
            # 2) Fall back to the most recent batch (completed) so the UI
            #    can still display the last run.
            row = conn.execute(text(f"""
                SELECT TOP 1 BATCH_ID,
                       MIN(CREATED_AT)      AS started_at,
                       MAX(ALLOCATION_MODE) AS mode,
                       MAX(COMPLETED_AT)    AS last_pick
                FROM {QUEUE_TABLE}
                GROUP BY BATCH_ID
                ORDER BY MIN(CREATED_AT) DESC
            """)).fetchone()
        if not row:
            return {"success": True, "active": None, "last": None}

        batch_id   = row[0]
        started_at = row[1]
        mode       = row[2]
        last_event = row[3]
        progress   = get_progress(conn, batch_id)
        failed     = get_failed_list(conn, batch_id)
        summary    = get_done_summary(conn, batch_id)

        if progress["pending"] > 0 or progress["in_progress"] > 0:
            stage = "alloc"          # Stage C waterfall in flight
        elif progress["total"] > 0:
            stage = "complete"
        else:
            stage = "listing"        # queue empty — Stage A/B before seed

        # Final completion timestamp (only meaningful when the batch is done)
        completed_at = conn.execute(text(f"""
            SELECT MAX(COMPLETED_AT) FROM {QUEUE_TABLE}
            WHERE BATCH_ID = :b
        """), {"b": batch_id}).scalar() if not is_active else None

        elapsed = None
        if started_at:
            from datetime import datetime as _dt
            try:
                ref = completed_at or _dt.now()
                elapsed = max(0.0, (ref - started_at).total_seconds())
            except Exception:
                elapsed = None

        # Per-store completion (proxy: how many distinct WERKS are covered by
        # MAJ_CATs the queue has marked DONE, vs total WERKS in alloc table).
        # Lets the UI render a Store-based progress bar alongside MAJ_CAT %.
        store_total, store_done = 0, 0
        if _table_exists(conn, ALLOC_TABLE):
            ac = _get_columns(conn, ALLOC_TABLE)
            if "WERKS" in ac and "MAJ_CAT" in ac:
                try:
                    store_total = conn.execute(text(
                        f"SELECT COUNT(DISTINCT [WERKS]) FROM [{ALLOC_TABLE}]"
                    )).scalar() or 0
                    store_done = conn.execute(text(f"""
                        SELECT COUNT(DISTINCT a.[WERKS])
                        FROM [{ALLOC_TABLE}] a
                        WHERE a.[MAJ_CAT] IN (
                            SELECT MAJ_CAT FROM {QUEUE_TABLE}
                            WHERE BATCH_ID = :b AND STATUS = 'DONE'
                        )
                    """), {"b": batch_id}).scalar() or 0
                except Exception:
                    store_total, store_done = 0, 0
        store_pct = round(100.0 * store_done / store_total, 1) if store_total else 0.0

        payload = {
            "batch_id":     batch_id,
            "started_at":   started_at.isoformat() if started_at else None,
            "completed_at": completed_at.isoformat() if completed_at else None,
            "last_event":   last_event.isoformat() if last_event else None,
            "mode":         mode,
            "stage":        stage,
            "is_active":    is_active,
            "elapsed_sec":  elapsed,
            "progress":     progress,
            "store_progress": {
                "total":  store_total,
                "done":   store_done,
                "pct":    store_pct,
            },
            "failed":       failed,
            "summary":      summary,
        }
        return {
            "success":  True,
            "active":   payload if is_active else None,
            "last":     payload,  # always include latest, active or not
        }


# ===========================================================================
# FINAL TABLE — filtered + cleaned extract from ARS_LISTING
# ===========================================================================

@router.post("/create-final")
def create_final_table(
    body: dict = None,
    current_user: User = Depends(get_current_user),
):
    """
    Create ARS_LISTING_FINAL from ARS_LISTING:
      - Filter: MSA_FNL_Q > 0 AND OPT_REQ_WH >= 1
      - Columns: only identity + calculated outputs (no SLOC stock, no Part 4 grid-prefix)

    Optional body params:
      min_opt_req_wh: float (default 1) — minimum OPT_REQ_WH to include
      min_msa_fnl_q: float (default 0) — minimum MSA_FNL_Q (> this value)
      extra_keep_cols: list[str] — additional columns to keep beyond defaults
      extra_filters: dict — {column: {op: 'gte'|'gt'|'lte'|'lt'|'eq', value: N}}
    """
    import time as _t
    start = _t.time()
    body = body or {}
    min_req_wh = float(body.get("min_opt_req_wh", 1))
    min_fnl_q  = float(body.get("min_msa_fnl_q", 0))
    extra_keep = set(c.upper() for c in body.get("extra_keep_cols", []))
    extra_filters = body.get("extra_filters", {})

    de = get_data_engine()
    with de.connect() as conn:
        if not _table_exists(conn, LISTING_TABLE):
            raise HTTPException(404, f"{LISTING_TABLE} not found. Generate listing first.")

        # Get all listing columns
        all_cols = _get_columns(conn, LISTING_TABLE)
        all_upper = {c.upper(): c for c in all_cols}

        # Determine which columns to include
        keep = _FINAL_KEEP_COLS | extra_keep
        selected = [c for c in all_cols if c.upper() in keep]
        if not selected:
            raise HTTPException(400, "No columns selected for final table")

        col_list = ", ".join(f"[{c}]" for c in selected)

        # Build WHERE clause
        where_parts = []
        params = {}

        # MSA_FNL_Q > min_fnl_q
        if "MSA_FNL_Q" in all_upper:
            where_parts.append(f"ISNULL([MSA_FNL_Q], 0) > :min_fnl")
            params["min_fnl"] = min_fnl_q

        # OPT_REQ_WH >= min_req_wh
        if "OPT_REQ_WH" in all_upper:
            where_parts.append(f"ISNULL([OPT_REQ_WH], 0) >= :min_req")
            params["min_req"] = min_req_wh

        # Extra user-supplied filters
        for i, (col, flt) in enumerate(extra_filters.items()):
            if col.upper() not in all_upper:
                continue
            actual = all_upper[col.upper()]
            op_map = {"gte": ">=", "gt": ">", "lte": "<=", "lt": "<", "eq": "="}
            op = op_map.get(flt.get("op", "gte"), ">=")
            pname = f"ef{i}"
            where_parts.append(f"ISNULL([{actual}], 0) {op} :{pname}")
            params[pname] = float(flt.get("value", 0))

        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

        # Drop + create final table
        _run(conn, f"IF OBJECT_ID('{FINAL_TABLE}','U') IS NOT NULL DROP TABLE [{FINAL_TABLE}]")
        _run(conn, f"""
            SELECT {col_list}
            INTO [{FINAL_TABLE}]
            FROM [{LISTING_TABLE}]
            {where_sql}
            ORDER BY [WERKS], [MAJ_CAT], [GEN_ART_NUMBER], [CLR]
        """, params)

        row_count = conn.execute(text(f"SELECT COUNT(*) FROM [{FINAL_TABLE}]")).scalar()
        src_count = conn.execute(text(f"SELECT COUNT(*) FROM [{LISTING_TABLE}]")).scalar()

    duration = round(_t.time() - start, 1)
    logger.info(f"ARS_LISTING_FINAL: {row_count} rows (from {src_count} listing rows) in {duration}s")

    return {
        "success": True,
        "message": f"Final: {row_count:,} rows from {src_count:,} listing (MSA_FNL_Q>{min_fnl_q}, OPT_REQ_WH>={min_req_wh}) in {duration}s",
        "data": {
            "table": FINAL_TABLE,
            "rows": row_count,
            "source_rows": src_count,
            "columns": selected,
            "filters_applied": where_parts,
            "duration_sec": duration,
        },
    }


@router.get("/final/preview")
def preview_final(
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=10, le=5000),
    search: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Preview ARS_LISTING_FINAL with pagination and search."""
    de = get_data_engine()
    with de.connect() as conn:
        if not _table_exists(conn, FINAL_TABLE):
            raise HTTPException(404, f"{FINAL_TABLE} not found. Create it first.")

        cols = _get_columns(conn, FINAL_TABLE)
        where_parts = []
        params = {}

        if search and search.strip():
            search_conds = [f"CAST([{c}] AS NVARCHAR(MAX)) LIKE :_gs" for c in cols]
            where_parts.append(f"({' OR '.join(search_conds)})")
            params["_gs"] = f"%{search.strip()}%"

        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        total = conn.execute(text(f"SELECT COUNT(*) FROM [{FINAL_TABLE}]{where_sql}"), params).scalar()

        col_list = ", ".join(f"[{c}]" for c in cols)
        offset = (page - 1) * page_size
        rows = conn.execute(text(f"""
            SELECT {col_list} FROM [{FINAL_TABLE}]{where_sql}
            ORDER BY [WERKS],
                     CASE [OPT_TYPE] WHEN 'RL'  THEN 1
                                     WHEN 'TBC' THEN 2
                                     WHEN 'TBL' THEN 3
                                     ELSE 4 END,
                     ISNULL([OPT_PRIORITY_RANK], 999999) ASC,
                     ISNULL([ST_RANK], 999999) ASC,
                     [MAJ_CAT], [GEN_ART_NUMBER], [CLR]
            OFFSET :off ROWS FETCH NEXT :ps ROWS ONLY
        """), {**params, "off": offset, "ps": page_size}).fetchall()

        data = [dict(zip(cols, r)) for r in rows]

    return {
        "success": True,
        "data": {"columns": cols, "data": data, "total": total, "page": page, "page_size": page_size},
    }


@router.get("/alloc-preview")
def preview_alloc(
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=10, le=5000),
    search: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Preview ARS_ALLOC_WORKING with pagination and search."""
    de = get_data_engine()
    with de.connect() as conn:
        if not _table_exists(conn, ALLOC_TABLE):
            raise HTTPException(404, f"{ALLOC_TABLE} not found. Generate listing first.")
        cols = _get_columns(conn, ALLOC_TABLE)
        where_parts, params = [], {}
        if search and search.strip():
            conds = [f"CAST([{c}] AS NVARCHAR(MAX)) LIKE :_gs" for c in cols]
            where_parts.append(f"({' OR '.join(conds)})")
            params["_gs"] = f"%{search.strip()}%"
        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        total = conn.execute(text(f"SELECT COUNT(*) FROM [{ALLOC_TABLE}]{where_sql}"), params).scalar()
        col_list = ", ".join(f"[{c}]" for c in cols)
        offset = (page - 1) * page_size
        rows = conn.execute(text(f"""
            SELECT {col_list} FROM [{ALLOC_TABLE}]{where_sql}
            ORDER BY [WERKS],
                     CASE [OPT_TYPE] WHEN 'RL'  THEN 1
                                     WHEN 'TBC' THEN 2
                                     WHEN 'TBL' THEN 3
                                     ELSE 4 END,
                     ISNULL([OPT_PRIORITY_RANK], 999999) ASC,
                     ISNULL([ST_RANK], 999999) ASC,
                     [MAJ_CAT], [GEN_ART_NUMBER], [CLR], [SZ]
            OFFSET :off ROWS FETCH NEXT :ps ROWS ONLY
        """), {**params, "off": offset, "ps": page_size}).fetchall()
        data = [dict(zip(cols, r)) for r in rows]
    return {
        "success": True,
        "data": {"columns": cols, "data": data, "total": total, "page": page, "page_size": page_size},
    }


@router.get("/store-ranking")
def preview_store_ranking(
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=10, le=5000),
    search: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Preview ARS_STORE_RANKING with pagination and search."""
    de = get_data_engine()
    with de.connect() as conn:
        if not _table_exists(conn, "ARS_STORE_RANKING"):
            raise HTTPException(404, "ARS_STORE_RANKING not found. Generate listing first.")
        cols = _get_columns(conn, "ARS_STORE_RANKING")
        where_parts, params = [], {}
        if search and search.strip():
            conds = [f"CAST([{c}] AS NVARCHAR(MAX)) LIKE :_gs" for c in cols]
            where_parts.append(f"({' OR '.join(conds)})")
            params["_gs"] = f"%{search.strip()}%"
        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        total = conn.execute(text(f"SELECT COUNT(*) FROM [ARS_STORE_RANKING]{where_sql}"), params).scalar()
        col_list = ", ".join(f"[{c}]" for c in cols)
        offset = (page - 1) * page_size
        rows = conn.execute(text(f"""
            SELECT {col_list} FROM [ARS_STORE_RANKING]{where_sql}
            ORDER BY [MAJ_CAT], [ST_RANK] DESC
            OFFSET :off ROWS FETCH NEXT :ps ROWS ONLY
        """), {**params, "off": offset, "ps": page_size}).fetchall()
        data = [dict(zip(cols, r)) for r in rows]
    return {
        "success": True,
        "data": {"columns": cols, "data": data, "total": total, "page": page, "page_size": page_size},
    }


@router.get("/preview")
def preview_listing(
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=10, le=5000),
    filters: Optional[str] = None,
    search: Optional[str] = None,
    table: str = Query("working", pattern="^(listing|working|alloc)$"),
    current_user: User = Depends(get_current_user),
):
    """Preview ARS_LISTING, ARS_LISTING_WORKING, or ARS_ALLOC_WORKING with column filters, global search, and pagination.

    Uses READ UNCOMMITTED so it never deadlocks with a concurrent
    listing-generation / allocation job. Dirty reads are acceptable for a
    preview — at worst an in-flight UPDATE's intermediate value is shown.
    """
    tbl = {"working": FINAL_TABLE, "alloc": ALLOC_TABLE}.get(table, LISTING_TABLE)
    de = get_data_engine()
    with de.connect() as conn:
        # Session-level isolation — applies to every SELECT on this connection.
        conn.execute(text("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED"))

        if not _table_exists(conn, tbl):
            raise HTTPException(404, f"{tbl} not found. Generate listing first.")

        cols = _get_columns(conn, tbl)
        where_parts, params = _build_filter_where(filters, set(cols))

        if search and search.strip():
            search_conds = [f"CAST([{c}] AS NVARCHAR(MAX)) LIKE :_gsearch" for c in cols]
            where_parts.append(f"({' OR '.join(search_conds)})")
            params["_gsearch"] = f"%{search.strip()}%"

        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        total = conn.execute(text(f"SELECT COUNT(*) FROM [{tbl}] WITH (NOLOCK){where_sql}"), params).scalar()

        col_list = ", ".join(f"[{c}]" for c in cols)
        offset = (page - 1) * page_size
        order = _safe_order(cols, table)
        rows = conn.execute(text(f"""
            SELECT {col_list} FROM [{tbl}] WITH (NOLOCK){where_sql}
            ORDER BY {order}
            OFFSET :off ROWS FETCH NEXT :ps ROWS ONLY
        """), {**params, "off": offset, "ps": page_size}).fetchall()

        data = [dict(zip(cols, row)) for row in rows]

    return {
        "success": True,
        "data": {"data": data, "total": total, "columns": cols, "page": page, "page_size": page_size, "table": tbl}
    }


@router.get("/summary")
def listing_summary(current_user: User = Depends(get_current_user)):
    """Summary stats for ARS_LISTING.

    Uses READ UNCOMMITTED so it never blocks (or gets deadlocked by) a
    concurrent allocation / listing-generation job. A summary view is
    tolerant of dirty reads — a row that's being inserted right now is
    at worst off-by-one in the counts shown to the user.

    Retries once on SQL Server deadlock (error 1205).
    """
    import time as _time

    de = get_data_engine()

    def _run():
        with de.connect() as conn:
            # Avoid shared locks; tolerate dirty reads for this summary.
            conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
            if not _table_exists(conn, LISTING_TABLE):
                return {"success": True, "data": None}
            return _compute_listing_summary(conn)

    for attempt in (1, 2):
        try:
            return _run()
        except Exception as exc:
            # 1205 = deadlock victim; transient, safe to retry
            if "1205" in str(exc) and attempt == 1:
                logger.warning("listing/summary deadlocked — retrying once")
                _time.sleep(0.25)
                continue
            raise


def _compute_listing_summary(conn):
    """Internal: compute the summary dict using an already-open connection."""
    summary = {}

    rows = conn.execute(text(f"""
        SELECT [RDC], COUNT(*) AS cnt,
               SUM(CASE WHEN [IS_NEW] = 1 THEN 1 ELSE 0 END) AS new_cnt,
               SUM(CASE WHEN [IS_NEW] = 0 THEN 1 ELSE 0 END) AS existing_cnt
        FROM [{LISTING_TABLE}]
        GROUP BY [RDC] ORDER BY [RDC]
    """)).fetchall()
    by_rdc = {r[0]: {"rdc": r[0], "total": r[1], "new": r[2], "existing": r[3], "alloc_qty": 0}
              for r in rows}

    # Allocated qty + Hold qty from ARS_ALLOC_WORKING (size-grain source of
    # truth produced by the waterfall). Previously read from
    # ARS_LISTING_WORKING (option-grain rollup via _stage_d_reflect); using
    # the source directly avoids any drift if the rollup ever lags or
    # filters rows.
    WORKING_TABLE = "ARS_ALLOC_WORKING"
    if _table_exists(conn, WORKING_TABLE):
        wk_cols = _get_columns(conn, WORKING_TABLE)
        if "ALLOC_QTY" in wk_cols and "RDC" in wk_cols:
            hold_expr = "ISNULL(SUM(TRY_CAST([HOLD_QTY] AS FLOAT)), 0)" if "HOLD_QTY" in wk_cols else "0"
            fnl_expr  = "ISNULL(MAX(TRY_CAST([FNL_Q] AS FLOAT)), 0)"     if "FNL_Q"    in wk_cols else "0"
            alloc_rows = conn.execute(text(f"""
                ;WITH AllocByRdc AS (
                    SELECT [RDC],
                           ISNULL(SUM(TRY_CAST([ALLOC_QTY] AS FLOAT)), 0) AS aq,
                           {hold_expr} AS hq
                    FROM [{WORKING_TABLE}] WITH (NOLOCK)
                    GROUP BY [RDC]
                ),
                PoolByRdc AS (
                    SELECT [RDC], SUM(pool_sz) AS stock_avail
                    FROM (
                        SELECT [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR], [VAR_ART], [SZ],
                               MAX(TRY_CAST([FNL_Q] AS FLOAT)) AS pool_sz
                        FROM [{WORKING_TABLE}] WITH (NOLOCK)
                        GROUP BY [RDC],[MAJ_CAT],[GEN_ART_NUMBER],[CLR],[VAR_ART],[SZ]
                    ) T
                    GROUP BY [RDC]
                )
                SELECT A.[RDC], A.aq, A.hq, ISNULL(P.stock_avail, 0) AS stock_avail
                FROM AllocByRdc A
                LEFT JOIN PoolByRdc P ON P.[RDC] = A.[RDC]
            """)).fetchall()
            for ar in alloc_rows:
                rdc_key = ar[0]
                aq = round(ar[1] or 0)
                hq = round(ar[2] or 0)
                sa = round(ar[3] or 0)
                if rdc_key in by_rdc:
                    by_rdc[rdc_key]["alloc_qty"]   = aq
                    by_rdc[rdc_key]["hold_qty"]    = hq
                    by_rdc[rdc_key]["stock_avail"] = sa
                else:
                    by_rdc[rdc_key] = {"rdc": rdc_key, "total": 0, "new": 0, "existing": 0,
                                        "alloc_qty": aq, "hold_qty": hq, "stock_avail": sa}

    # Ensure every row has hold_qty / stock_avail keys
    for v in by_rdc.values():
        v.setdefault("hold_qty", 0)
        v.setdefault("alloc_qty", 0)
        v.setdefault("stock_avail", 0)

    summary["by_rdc"] = sorted(by_rdc.values(), key=lambda x: x["rdc"])

    # by_maj_cat: SUM(ALLOC_QTY) from ARS_ALLOC_WORKING (size-grain) — every
    # MAJ_CAT that contributed to the allocation. Same source as the by_rdc
    # totals above, so the MAJ_CAT-modal total reconciles exactly with the
    # TOTAL ALLOC QTY tile. The chart only renders top/bottom N anyway.
    # Also enrich each row with msa_qty = SUM(<MSA qty col>) FROM ARS_MSA_GEN_ART
    # for the same MAJ_CAT so the modal can show ALLOC vs MSA side-by-side.
    by_maj_cat: List[Dict[str, Any]] = []
    if _table_exists(conn, WORKING_TABLE):
        wk_cols = _get_columns(conn, WORKING_TABLE)
        if "ALLOC_QTY" in wk_cols and "MAJ_CAT" in wk_cols:
            rows = conn.execute(text(f"""
                SELECT [MAJ_CAT],
                       ROUND(ISNULL(SUM(TRY_CAST([ALLOC_QTY] AS FLOAT)), 0), 0) AS aq
                FROM [{WORKING_TABLE}]
                WHERE [MAJ_CAT] IS NOT NULL
                GROUP BY [MAJ_CAT]
                HAVING ISNULL(SUM(TRY_CAST([ALLOC_QTY] AS FLOAT)), 0) > 0
                ORDER BY aq DESC
            """)).fetchall()
            by_maj_cat = [
                {"maj_cat": r[0], "alloc_qty": round(r[1] or 0), "msa_qty": 0}
                for r in rows
            ]

    # Lookup MSA stock per MAJ_CAT and merge into by_maj_cat. Done as a
    # separate query (not a JOIN) because the MSA table may have MAJ_CATs
    # that aren't in this listing run, and vice versa — keeping them
    # decoupled is cheaper than a full outer join.
    if by_maj_cat and _table_exists(conn, "ARS_MSA_GEN_ART"):
        mc = _get_columns(conn, "ARS_MSA_GEN_ART")
        qty_col = next((c for c in ("FNL_Q", "MSA_FNL_Q", "QTY") if c in mc), None)
        if qty_col and "MAJ_CAT" in mc:
            try:
                msa_rows = conn.execute(text(f"""
                    SELECT [MAJ_CAT],
                           ROUND(ISNULL(SUM(TRY_CAST([{qty_col}] AS FLOAT)), 0), 0) AS mq
                    FROM [ARS_MSA_GEN_ART]
                    WHERE [MAJ_CAT] IS NOT NULL
                    GROUP BY [MAJ_CAT]
                """)).fetchall()
                msa_map = {r[0]: round(r[1] or 0) for r in msa_rows}
                for row in by_maj_cat:
                    row["msa_qty"] = msa_map.get(row["maj_cat"], 0)
            except Exception:
                # Leave msa_qty=0 on any error — modal still functions
                pass

    summary["by_maj_cat"] = by_maj_cat

    # by_maj_cat_rdc: (MAJ_CAT × RDC) breakdown — stock available, alloc, hold,
    # and MJ_REQ. Used by the MAJ_CAT modal to pivot one row per MAJ_CAT with
    # per-RDC columns. Residual FNL_Q (stock − alloc − hold) is computed on the
    # frontend so the modal stays in sync if any of the three move.
    if _table_exists(conn, WORKING_TABLE):
        wk_cols = _get_columns(conn, WORKING_TABLE)
        if "ALLOC_QTY" in wk_cols and "MAJ_CAT" in wk_cols and "RDC" in wk_cols and "FNL_Q" in wk_cols:
            try:
                hold_sel = ("ROUND(ISNULL(SUM(TRY_CAST(A.[HOLD_QTY] AS FLOAT)), 0), 0)"
                            if "HOLD_QTY" in wk_cols else "0")
                # MJ_REQ / MJ_MBQ / MJ_STK_TTL all live at WERKS×MAJ_CAT grain
                # in ARS_LISTING_WORKING. Dedup per (WERKS, MAJ_CAT, RDC) with
                # MAX, then SUM across WERKS to land at MAJ_CAT × RDC.
                lw_exists = _table_exists(conn, "ARS_LISTING_WORKING")
                lw_cols   = _get_columns(conn, "ARS_LISTING_WORKING") if lw_exists else []
                lw_keys_ok = (lw_exists and "WERKS" in lw_cols
                              and "RDC" in lw_cols and "MAJ_CAT" in lw_cols)
                has_req = lw_keys_ok and "MJ_REQ" in lw_cols
                has_mbq = lw_keys_ok and "MJ_MBQ" in lw_cols
                has_stk = lw_keys_ok and "MJ_STK_TTL" in lw_cols
                src_cte = ""
                src_join = ""
                req_sel = "0"
                mbq_sel = "0"
                stk_sel = "0"
                if lw_keys_ok and (has_req or has_mbq or has_stk):
                    req_inner = ("MAX(ISNULL(TRY_CAST([MJ_REQ] AS FLOAT), 0))"
                                 if has_req else "0")
                    mbq_inner = ("MAX(ISNULL(TRY_CAST([MJ_MBQ] AS FLOAT), 0))"
                                 if has_mbq else "0")
                    stk_inner = ("MAX(ISNULL(TRY_CAST([MJ_STK_TTL] AS FLOAT), 0))"
                                 if has_stk else "0")
                    src_cte = f""",
                    SrcByMR AS (
                        SELECT [MAJ_CAT], [RDC],
                               SUM(req_w) AS req_qty,
                               SUM(mbq_w) AS mbq_qty,
                               SUM(stk_w) AS stk_qty
                        FROM (
                            SELECT [WERKS], [MAJ_CAT], [RDC],
                                   {req_inner} AS req_w,
                                   {mbq_inner} AS mbq_w,
                                   {stk_inner} AS stk_w
                            FROM [ARS_LISTING_WORKING] WITH (NOLOCK)
                            WHERE [MAJ_CAT] IS NOT NULL AND [RDC] IS NOT NULL
                            GROUP BY [WERKS], [MAJ_CAT], [RDC]
                        ) S
                        GROUP BY [MAJ_CAT], [RDC]
                    )"""
                    src_join = ("LEFT JOIN SrcByMR R "
                                "ON R.[MAJ_CAT] = A.[MAJ_CAT] AND R.[RDC] = A.[RDC]")
                    if has_req:
                        req_sel = "ROUND(ISNULL(MAX(R.req_qty), 0), 0)"
                    if has_mbq:
                        mbq_sel = "ROUND(ISNULL(MAX(R.mbq_qty), 0), 0)"
                    if has_stk:
                        stk_sel = "ROUND(ISNULL(MAX(R.stk_qty), 0), 0)"

                # EXCESS_STK lives at option grain on ARS_LISTING (Part 4d
                # populates: max(0, STK_TTL − excess_multiplier × OPT_MBQ),
                # MIX rows skipped). Aggregate to MAJ_CAT × RDC here.
                lst_exists = _table_exists(conn, LISTING_TABLE)
                lst_cols   = _get_columns(conn, LISTING_TABLE) if lst_exists else []
                has_excess = (lst_exists and "EXCESS_STK" in lst_cols
                              and "MAJ_CAT" in lst_cols and "RDC" in lst_cols)
                excess_cte = ""
                excess_join = ""
                excess_sel = "0"
                if has_excess:
                    excess_cte = f""",
                    ExcessByMR AS (
                        SELECT [MAJ_CAT], [RDC],
                               SUM(ISNULL(TRY_CAST([EXCESS_STK] AS FLOAT), 0)) AS excess_stk
                        FROM [{LISTING_TABLE}] WITH (NOLOCK)
                        WHERE [MAJ_CAT] IS NOT NULL AND [RDC] IS NOT NULL
                        GROUP BY [MAJ_CAT], [RDC]
                    )"""
                    excess_join = ("LEFT JOIN ExcessByMR EX "
                                   "ON EX.[MAJ_CAT] = A.[MAJ_CAT] AND EX.[RDC] = A.[RDC]")
                    excess_sel = "ROUND(ISNULL(MAX(EX.excess_stk), 0), 0)"

                mr_rows = conn.execute(text(f"""
                    ;WITH PoolPerSize AS (
                        SELECT [MAJ_CAT], [RDC], [GEN_ART_NUMBER], [CLR], [VAR_ART], [SZ],
                               MAX(TRY_CAST([FNL_Q] AS FLOAT)) AS pool_sz
                        FROM [{WORKING_TABLE}] WITH (NOLOCK)
                        GROUP BY [MAJ_CAT],[RDC],[GEN_ART_NUMBER],[CLR],[VAR_ART],[SZ]
                    ){src_cte}{excess_cte}
                    SELECT A.[MAJ_CAT], A.[RDC],
                           ROUND(ISNULL(SUM(TRY_CAST(A.[ALLOC_QTY] AS FLOAT)), 0), 0) AS aq,
                           ROUND(ISNULL(MAX(P.stock_avail), 0), 0) AS stock_avail,
                           {hold_sel} AS hq,
                           {req_sel} AS req_qty,
                           {mbq_sel} AS mbq_qty,
                           {stk_sel} AS store_stk,
                           {excess_sel} AS excess_stk
                    FROM [{WORKING_TABLE}] A WITH (NOLOCK)
                    LEFT JOIN (
                        SELECT [MAJ_CAT], [RDC], SUM(pool_sz) AS stock_avail
                        FROM PoolPerSize
                        GROUP BY [MAJ_CAT], [RDC]
                    ) P ON P.[MAJ_CAT] = A.[MAJ_CAT] AND P.[RDC] = A.[RDC]
                    {src_join}
                    {excess_join}
                    WHERE A.[MAJ_CAT] IS NOT NULL AND A.[RDC] IS NOT NULL
                    GROUP BY A.[MAJ_CAT], A.[RDC]
                    ORDER BY A.[MAJ_CAT], A.[RDC]
                """)).fetchall()
                summary["by_maj_cat_rdc"] = [
                    {"maj_cat": r[0], "rdc": r[1],
                     "alloc_qty":   int(r[2] or 0), "stock_avail": int(r[3] or 0),
                     "hold_qty":    int(r[4] or 0), "req_qty":     int(r[5] or 0),
                     "mbq_qty":     int(r[6] or 0), "store_stk":   int(r[7] or 0),
                     "excess_stk":  int(r[8] or 0)}
                    for r in mr_rows if r[0] and r[1]
                ]
            except Exception:
                summary["by_maj_cat_rdc"] = []
        else:
            summary["by_maj_cat_rdc"] = []
    else:
        summary["by_maj_cat_rdc"] = []

    # GEN_ART_NUMBER is BIGINT — must CAST for string concatenation
    opt_key = "ISNULL([MAJ_CAT],'') + '|' + ISNULL(CAST([GEN_ART_NUMBER] AS NVARCHAR(50)),'') + '|' + ISNULL([CLR],'')"
    row = conn.execute(text(f"""
        SELECT COUNT(*) AS total,
               ISNULL(SUM(CASE WHEN [IS_NEW] = 1 THEN 1 ELSE 0 END), 0) AS new_rows,
               COUNT(DISTINCT [WERKS]) AS stores,
               COUNT(DISTINCT [RDC]) AS rdcs,
               COUNT(DISTINCT {opt_key}) AS options,
               COUNT(DISTINCT CASE WHEN [IS_NEW] = 1 THEN {opt_key} END) AS new_options,
               COUNT(DISTINCT CASE WHEN [IS_NEW] = 0 THEN {opt_key} END) AS existing_options
        FROM [{LISTING_TABLE}]
    """)).fetchone()
    total = row[0] or 0
    new_rows = row[1] or 0
    summary["totals"] = {
        "total": total, "new": new_rows, "existing": total - new_rows,
        "stores": row[2] or 0, "rdcs": row[3] or 0,
        "options": row[4] or 0,
        "new_options": row[5] or 0,
        "existing_options": row[6] or 0,
        "hold_qty": sum(r.get("hold_qty", 0) for r in by_rdc.values()),
        "alloc_qty": sum(r.get("alloc_qty", 0) for r in by_rdc.values()),
    }

    # OPT_TYPE breakdown
    cols = _get_columns(conn, LISTING_TABLE)
    if "OPT_TYPE" in cols:
        opt_rows = conn.execute(text(f"""
            SELECT ISNULL([OPT_TYPE], 'UNTAGGED') AS opt, COUNT(*) AS cnt
            FROM [{LISTING_TABLE}]
            GROUP BY [OPT_TYPE]
        """)).fetchall()
        summary["by_opt_type"] = {r[0]: r[1] for r in opt_rows}

    # Alloc qty by OPT_TYPE from ARS_ALLOC_WORKING (size-grain source of truth)
    if _table_exists(conn, WORKING_TABLE):
        wk_cols = _get_columns(conn, WORKING_TABLE)
        if "ALLOC_QTY" in wk_cols and "OPT_TYPE" in wk_cols:
            alloc_opt_rows = conn.execute(text(f"""
                SELECT ISNULL([OPT_TYPE], 'UNTAGGED') AS opt,
                       ROUND(ISNULL(SUM(TRY_CAST([ALLOC_QTY] AS FLOAT)), 0), 0) AS aq
                FROM [{WORKING_TABLE}]
                GROUP BY [OPT_TYPE]
            """)).fetchall()
            summary["alloc_by_opt_type"] = {r[0]: round(r[1] or 0) for r in alloc_opt_rows}

    # Working + Alloc row counts
    if _table_exists(conn, FINAL_TABLE):
        summary["working_rows"] = conn.execute(text(
            f"SELECT COUNT(*) FROM [{FINAL_TABLE}]"
        )).scalar() or 0
    if _table_exists(conn, ALLOC_TABLE):
        ac = _get_columns(conn, ALLOC_TABLE)
        if "ALLOC_QTY" in ac:
            alloc_total = conn.execute(text(
                f"SELECT COUNT(*) FROM [{ALLOC_TABLE}] WHERE ISNULL([ALLOC_QTY], 0) > 0"
            )).scalar() or 0
            summary["alloc_rows"] = alloc_total
        else:
            summary["alloc_rows"] = 0

    # ALLOC_STATUS breakdown from ARS_LISTING_WORKING
    if _table_exists(conn, FINAL_TABLE):
        fc = _get_columns(conn, FINAL_TABLE)
        if "ALLOC_STATUS" in fc:
            status_rows = conn.execute(text(f"""
                SELECT ISNULL([ALLOC_STATUS], 'UNKNOWN') AS st, COUNT(*) AS cnt
                FROM [{FINAL_TABLE}] GROUP BY [ALLOC_STATUS]
            """)).fetchall()
            summary["by_alloc_status"] = {r[0]: r[1] for r in status_rows}

    # MSA total quantity (sum of FNL_Q across MSA gen-art rows) for KPI tile
    if _table_exists(conn, "ARS_MSA_GEN_ART"):
        mc = _get_columns(conn, "ARS_MSA_GEN_ART")
        qty_col = next((c for c in ("FNL_Q", "MSA_FNL_Q", "QTY") if c in mc), None)
        if qty_col:
            try:
                msa_qty = conn.execute(text(
                    f"SELECT ISNULL(SUM(TRY_CAST([{qty_col}] AS FLOAT)), 0) "
                    f"FROM [ARS_MSA_GEN_ART]"
                )).scalar() or 0
                summary["msa_qty"] = round(float(msa_qty))
            except Exception:
                summary["msa_qty"] = 0

    # Distinct active stores from master + store-status breakdown for the chart
    if _table_exists(conn, "Master_ALC_INPUT_ST_MASTER"):
        st_cols = _get_columns(conn, "Master_ALC_INPUT_ST_MASTER")
        listing_filter = ""
        if "LISTING" in st_cols:
            listing_filter = (" WHERE ISNULL(CAST([LISTING] AS NVARCHAR(10)), '1') "
                              "NOT IN ('0','N','n')")
        try:
            summary["active_store_count"] = conn.execute(text(
                f"SELECT COUNT(DISTINCT [ST_CD]) FROM [Master_ALC_INPUT_ST_MASTER]{listing_filter}"
            )).scalar() or 0
        except Exception:
            summary["active_store_count"] = 0
        if "STSTATUS" in st_cols:
            try:
                rows = conn.execute(text("""
                    SELECT ISNULL(NULLIF(LTRIM(RTRIM(CAST([STSTATUS] AS NVARCHAR(50)))),''), 'UNKNOWN') AS st,
                           COUNT(DISTINCT [ST_CD])
                    FROM [Master_ALC_INPUT_ST_MASTER]
                    GROUP BY [STSTATUS]
                    ORDER BY 2 DESC
                """)).fetchall()
                summary["by_store_status"] = [
                    {"status": r[0], "count": int(r[1])} for r in rows
                ]
            except Exception:
                summary["by_store_status"] = []

    # Listed-store count (distinct WERKS in current listing) — for "5 / 346 active"
    summary["listed_store_count"] = summary.get("totals", {}).get("stores", 0)

    # Alloc + Hold by SSN and DIV — drives the new season/division charts
    if _table_exists(conn, WORKING_TABLE):
        wk_cols = _get_columns(conn, WORKING_TABLE)
        if "ALLOC_QTY" in wk_cols and "GEN_ART_NUMBER" in wk_cols:
            hold_col = "ROUND(ISNULL(SUM(TRY_CAST(W.[HOLD_QTY] AS FLOAT)),0),0)" if "HOLD_QTY" in wk_cols else "0"
            try:
                rows = conn.execute(text(f"""
                    SELECT MP.[SSN],
                           ROUND(ISNULL(SUM(TRY_CAST(W.[ALLOC_QTY] AS FLOAT)),0),0) AS aq,
                           {hold_col} AS hq
                    FROM [{WORKING_TABLE}] W WITH (NOLOCK)
                    LEFT JOIN [vw_master_product] MP WITH (NOLOCK)
                          ON W.[GEN_ART_NUMBER] = MP.[ARTICLE_NUMBER]
                    WHERE MP.[SSN] IS NOT NULL
                    GROUP BY MP.[SSN]
                    ORDER BY aq DESC
                """)).fetchall()
                summary["by_ssn"] = [
                    {"ssn": r[0], "alloc_qty": int(r[1] or 0), "hold_qty": int(r[2] or 0)}
                    for r in rows if r[0]
                ]
            except Exception:
                summary["by_ssn"] = []
            try:
                rows = conn.execute(text(f"""
                    SELECT MP.[DIV],
                           ROUND(ISNULL(SUM(TRY_CAST(W.[ALLOC_QTY] AS FLOAT)),0),0) AS aq,
                           {hold_col} AS hq
                    FROM [{WORKING_TABLE}] W WITH (NOLOCK)
                    LEFT JOIN [vw_master_product] MP WITH (NOLOCK)
                          ON W.[GEN_ART_NUMBER] = MP.[ARTICLE_NUMBER]
                    WHERE MP.[DIV] IS NOT NULL
                    GROUP BY MP.[DIV]
                    ORDER BY aq DESC
                """)).fetchall()
                summary["by_div"] = [
                    {"div": r[0], "alloc_qty": int(r[1] or 0), "hold_qty": int(r[2] or 0)}
                    for r in rows if r[0]
                ]
            except Exception:
                summary["by_div"] = []

    # Top stores by allocated qty — drives the new Top/Bottom N stores chart
    if _table_exists(conn, FINAL_TABLE):
        fc = _get_columns(conn, FINAL_TABLE)
        if "ALLOC_QTY" in fc and "WERKS" in fc:
            try:
                hold_sel = ("ROUND(ISNULL(SUM(TRY_CAST([HOLD_QTY] AS FLOAT)), 0), 0)"
                            if "HOLD_QTY" in fc else "0")
                # Join ARS_LISTING_WORKING for MJ_REQ (total store requirement
                # across all MAJ_CATs in this run).
                wt_exists = _table_exists(conn, "ARS_LISTING_WORKING")
                wt_cols   = _get_columns(conn, "ARS_LISTING_WORKING") if wt_exists else []
                has_req   = wt_exists and "MJ_REQ" in wt_cols and "WERKS" in wt_cols
                hold_sel_f = ("ROUND(ISNULL(SUM(TRY_CAST(f.[HOLD_QTY] AS FLOAT)), 0), 0)"
                              if "HOLD_QTY" in fc else "0")
                if has_req:
                    rows = conn.execute(text(f"""
                        SELECT f.[WERKS],
                               ROUND(ISNULL(SUM(TRY_CAST(f.[ALLOC_QTY] AS FLOAT)), 0), 0) AS aq,
                               {hold_sel_f} AS hq,
                               COUNT(*) AS rows_cnt,
                               ROUND(ISNULL(MAX(w.mj_req_store), 0), 0) AS mj_req
                        FROM [{FINAL_TABLE}] f
                        LEFT JOIN (
                            -- MAX per (WERKS, MAJ_CAT) de-duplicates OPT rows that share
                            -- the same MAJ_CAT-level MJ_REQ, then SUM across MAJ_CATs.
                            SELECT [WERKS],
                                   SUM(mj_req_per_mc) AS mj_req_store
                            FROM (
                                SELECT [WERKS], [MAJ_CAT],
                                       MAX(ISNULL(TRY_CAST([MJ_REQ] AS FLOAT), 0)) AS mj_req_per_mc
                                FROM [ARS_LISTING_WORKING]
                                GROUP BY [WERKS], [MAJ_CAT]
                            ) mc
                            GROUP BY [WERKS]
                        ) w ON w.[WERKS] = f.[WERKS]
                        GROUP BY f.[WERKS]
                    """)).fetchall()
                    summary["by_store"] = [
                        {"werks": r[0], "alloc_qty": int(r[1] or 0),
                         "hold_qty": int(r[2] or 0), "rows": int(r[3] or 0),
                         "mj_req": int(r[4] or 0)}
                        for r in rows if r[0]
                    ]
                else:
                    rows = conn.execute(text(f"""
                        SELECT [WERKS],
                               ROUND(ISNULL(SUM(TRY_CAST([ALLOC_QTY] AS FLOAT)), 0), 0) AS aq,
                               {hold_sel} AS hq,
                               COUNT(*) AS rows_cnt
                        FROM [{FINAL_TABLE}]
                        GROUP BY [WERKS]
                    """)).fetchall()
                    summary["by_store"] = [
                        {"werks": r[0], "alloc_qty": int(r[1] or 0),
                         "hold_qty": int(r[2] or 0), "rows": int(r[3] or 0),
                         "mj_req": 0}
                        for r in rows if r[0]
                    ]
            except Exception:
                summary["by_store"] = []

    # By-HUB allocation — joins ARS_LISTING_WORKING.WERKS to store master HUB
    # column when one exists in addition to the RDC column.
    if _table_exists(conn, FINAL_TABLE) and _table_exists(conn, "Master_ALC_INPUT_ST_MASTER"):
        fc = _get_columns(conn, FINAL_TABLE)
        sm_cols = _get_columns(conn, "Master_ALC_INPUT_ST_MASTER")
        if "ALLOC_QTY" in fc and "WERKS" in fc and "HUB" in sm_cols and "ST_CD" in sm_cols:
            try:
                hold_sel = ("ROUND(ISNULL(SUM(TRY_CAST(f.[HOLD_QTY] AS FLOAT)),0),0)"
                            if "HOLD_QTY" in fc else "0")
                rows = conn.execute(text(f"""
                    SELECT ISNULL(NULLIF(LTRIM(RTRIM(CAST(s.[HUB] AS NVARCHAR(50)))),''), 'UNKNOWN') AS hub,
                           ROUND(ISNULL(SUM(TRY_CAST(f.[ALLOC_QTY] AS FLOAT)),0),0) AS aq,
                           {hold_sel} AS hq
                    FROM [{FINAL_TABLE}] f
                    LEFT JOIN [Master_ALC_INPUT_ST_MASTER] s ON s.[ST_CD] = f.[WERKS]
                    GROUP BY s.[HUB]
                    ORDER BY aq DESC
                """)).fetchall()
                summary["by_hub"] = [
                    {"hub": r[0], "alloc_qty": int(r[1] or 0), "hold_qty": int(r[2] or 0)}
                    for r in rows
                ]
            except Exception:
                summary["by_hub"] = []

    return {"success": True, "data": summary}


@router.get("/store-by-majcat")
def store_by_majcat(
    maj_cat: str,
    rdc: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """
    Per-store breakdown for ONE MAJ_CAT (optionally filtered to one RDC).
    Powers the click-to-drill from the MAJ_CAT modal in the listing UI.

    Returns one row per WERKS with: store_stk, mbq, req, alloc, hold,
    fnl_q (residual at the store grain), req_pct (= alloc/req), and
    fill_pct (= (store_stk + alloc) / mbq) — same columns the parent
    modal shows, but at the store grain.
    """
    if not (maj_cat or "").strip():
        return {"success": True, "data": [], "maj_cat": maj_cat, "rdc": rdc}
    A_TBL = "ARS_ALLOC_WORKING"
    L_TBL = "ARS_LISTING_WORKING"
    de = get_data_engine()
    with de.connect() as conn:
        conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        if not _table_exists(conn, A_TBL):
            return {"success": True, "data": [], "maj_cat": maj_cat, "rdc": rdc}
        ac = _get_columns(conn, A_TBL)
        if not all(k in ac for k in ("WERKS", "MAJ_CAT", "RDC", "ALLOC_QTY")):
            return {"success": True, "data": [], "maj_cat": maj_cat, "rdc": rdc}
        lw_exists = _table_exists(conn, L_TBL)
        lc = _get_columns(conn, L_TBL) if lw_exists else []
        lw_ok = lw_exists and all(k in lc for k in ("WERKS", "MAJ_CAT", "RDC"))
        has_req = lw_ok and "MJ_REQ" in lc
        has_mbq = lw_ok and "MJ_MBQ" in lc
        has_stk = lw_ok and "MJ_STK_TTL" in lc
        hold_sel = ("ROUND(ISNULL(SUM(TRY_CAST(A.[HOLD_QTY] AS FLOAT)),0),0)"
                    if "HOLD_QTY" in ac else "0")
        # Source per (WERKS, MAJ_CAT, RDC) — MAX dedups OPT rows that share
        # the same MAJ_CAT-level value.
        src_cte = ""
        src_join = ""
        req_sel = "0"
        mbq_sel = "0"
        stk_sel = "0"
        params: Dict[str, Any] = {"mc": maj_cat}
        rdc_filter_lw = ""
        if rdc:
            params["rdc"] = rdc
            rdc_filter_lw = "AND [RDC] = :rdc"
        if lw_ok and (has_req or has_mbq or has_stk):
            req_inner = ("MAX(ISNULL(TRY_CAST([MJ_REQ] AS FLOAT),0))"
                         if has_req else "0")
            mbq_inner = ("MAX(ISNULL(TRY_CAST([MJ_MBQ] AS FLOAT),0))"
                         if has_mbq else "0")
            stk_inner = ("MAX(ISNULL(TRY_CAST([MJ_STK_TTL] AS FLOAT),0))"
                         if has_stk else "0")
            src_cte = f""";WITH Src AS (
                SELECT [WERKS],
                       {req_inner} AS req_q,
                       {mbq_inner} AS mbq_q,
                       {stk_inner} AS stk_q
                FROM [{L_TBL}] WITH (NOLOCK)
                WHERE [MAJ_CAT] = :mc {rdc_filter_lw}
                GROUP BY [WERKS]
            )"""
            src_join = "LEFT JOIN Src R ON R.[WERKS] = A.[WERKS]"
            if has_req:
                req_sel = "ROUND(ISNULL(MAX(R.req_q),0),0)"
            if has_mbq:
                mbq_sel = "ROUND(ISNULL(MAX(R.mbq_q),0),0)"
            if has_stk:
                stk_sel = "ROUND(ISNULL(MAX(R.stk_q),0),0)"
        rdc_filter_a = "AND A.[RDC] = :rdc" if rdc else ""
        sql = f"""
            {src_cte}
            SELECT A.[WERKS],
                   ROUND(ISNULL(SUM(TRY_CAST(A.[ALLOC_QTY] AS FLOAT)),0),0) AS aq,
                   {hold_sel} AS hq,
                   {req_sel} AS req_q,
                   {mbq_sel} AS mbq_q,
                   {stk_sel} AS stk_q
            FROM [{A_TBL}] A WITH (NOLOCK)
            {src_join}
            WHERE A.[MAJ_CAT] = :mc {rdc_filter_a}
            GROUP BY A.[WERKS]
            ORDER BY aq DESC
        """
        rows = conn.execute(text(sql), params).fetchall()
        data = []
        for r in rows:
            werks = r[0]
            if not werks:
                continue
            alloc = int(r[1] or 0)
            hold  = int(r[2] or 0)
            req   = int(r[3] or 0)
            mbq   = int(r[4] or 0)
            stk   = int(r[5] or 0)
            data.append({
                "werks": werks,
                "alloc_qty": alloc,
                "hold_qty":  hold,
                "req_qty":   req,
                "mbq_qty":   mbq,
                "store_stk": stk,
            })
        return {"success": True, "data": data,
                "maj_cat": maj_cat, "rdc": rdc}


@router.get("/opt-summary")
def opt_summary(
    maj_cat: str,
    rdc: Optional[str] = None,
    werks: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Per-OPT drill for a MAJ_CAT (optionally filtered by RDC and/or WERKS).

    Returns one row per (WERKS, GEN_ART_NUMBER, CLR) with the OPT-grain
    columns (OPT_MBQ, OPT_REQ, STK_TTL, EXCESS_STK, ALLOC_QTY, HOLD_QTY,
    MSA_FNL_Q_REM, OPT_TYPE, OPT_STATUS, ALLOC_STATUS, ALLOC_REMARKS,
    OPT_PRIORITY_RANK, ST_RANK). Sourced from ARS_LISTING_WORKING (OPT
    grain) joined with aggregated ARS_ALLOC_WORKING (size grain rolled
    to OPT).
    """
    if not (maj_cat or "").strip():
        return {"success": True, "data": [], "maj_cat": maj_cat, "rdc": rdc, "werks": werks}
    L_TBL = "ARS_LISTING_WORKING"
    A_TBL = "ARS_ALLOC_WORKING"
    de = get_data_engine()
    with de.connect() as conn:
        conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        if not _table_exists(conn, L_TBL):
            return {"success": True, "data": [], "maj_cat": maj_cat, "rdc": rdc, "werks": werks}
        lc = _get_columns(conn, L_TBL)
        if not all(k in lc for k in ("WERKS", "MAJ_CAT", "GEN_ART_NUMBER", "CLR")):
            return {"success": True, "data": [], "maj_cat": maj_cat, "rdc": rdc, "werks": werks}
        # Optional columns — selected only when present.
        opt_cols = {
            "RDC", "GEN_ART_DESC", "OPT_TYPE", "OPT_STATUS", "FINAL_OPT_TYPE",
            "IS_NEW", "I_ROD", "ACS_D", "ALC_D", "MAX_DAILY_SALE",
            "STK_TTL", "EXCESS_STK", "MSA_FNL_Q", "MSA_FNL_Q_REM",
            "OPT_MBQ", "OPT_REQ", "OPT_REQ_WH",
            "MJ_REQ", "MJ_MBQ", "MJ_STK_TTL", "MJ_REQ_REM",
            "PRI_CT%", "PRI_CT_REM", "SEC_CT%",
            "ALLOC_QTY", "HOLD_QTY", "ALLOC_STATUS", "ALLOC_REMARKS",
            "ST_RANK", "OPT_PRIORITY_RANK", "OPT_PRIORITY_TIER",
            "LISTED_FLAG", "LISTED_REASON",
        }
        present = [c for c in opt_cols if c in lc]
        sel_list = ", ".join(f"L.[{c}]" for c in present)

        params: Dict[str, Any] = {"mc": maj_cat}
        rdc_filter = ""
        if rdc:
            params["rdc"] = rdc
            rdc_filter = " AND L.[RDC] = :rdc"
        werks_filter = ""
        if werks:
            params["werks"] = werks
            werks_filter = " AND L.[WERKS] = :werks"

        # Pull alloc-side totals at OPT grain from ARS_ALLOC_WORKING so the
        # numbers match the size-grain source of truth (Stage D rollup can lag
        # if anything in the pipeline interrupted).
        alloc_join_sel = "0 AS alloc_qty_a, 0 AS hold_qty_a"
        alloc_join_cte = ""
        alloc_join_clause = ""
        if _table_exists(conn, A_TBL):
            ac = _get_columns(conn, A_TBL)
            if all(k in ac for k in ("WERKS", "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "ALLOC_QTY")):
                hold_sel = ("ROUND(ISNULL(SUM(TRY_CAST([HOLD_QTY] AS FLOAT)),0),0)"
                            if "HOLD_QTY" in ac else "0")
                alloc_join_cte = f""", AggAlloc AS (
                    SELECT [WERKS], [MAJ_CAT], [GEN_ART_NUMBER], ISNULL([CLR],'') AS CLR,
                           ROUND(ISNULL(SUM(TRY_CAST([ALLOC_QTY] AS FLOAT)),0),0) AS aq,
                           {hold_sel} AS hq
                    FROM [{A_TBL}] WITH (NOLOCK)
                    WHERE [MAJ_CAT] = :mc {('AND [RDC] = :rdc' if rdc else '')}
                    GROUP BY [WERKS], [MAJ_CAT], [GEN_ART_NUMBER], ISNULL([CLR],'')
                )"""
                alloc_join_sel = "ISNULL(AA.aq, 0) AS alloc_qty_a, ISNULL(AA.hq, 0) AS hold_qty_a"
                alloc_join_clause = ("LEFT JOIN AggAlloc AA "
                                     "ON AA.[WERKS] = L.[WERKS] "
                                     "AND AA.[MAJ_CAT] = L.[MAJ_CAT] "
                                     "AND AA.[GEN_ART_NUMBER] = L.[GEN_ART_NUMBER] "
                                     "AND AA.[CLR] = ISNULL(L.[CLR],'')")

        sql = f"""
            ;WITH Listed AS (
                SELECT L.[WERKS], L.[MAJ_CAT], L.[GEN_ART_NUMBER], L.[CLR]
                FROM [{L_TBL}] L WITH (NOLOCK)
                WHERE L.[MAJ_CAT] = :mc {rdc_filter}{werks_filter}
            ){alloc_join_cte}
            SELECT L.[WERKS], L.[MAJ_CAT], L.[GEN_ART_NUMBER], L.[CLR],
                   {sel_list},
                   {alloc_join_sel}
            FROM [{L_TBL}] L WITH (NOLOCK)
            {alloc_join_clause}
            WHERE L.[MAJ_CAT] = :mc {rdc_filter}{werks_filter}
            ORDER BY ISNULL(L.[OPT_PRIORITY_RANK], 999999) ASC,
                     L.[WERKS], L.[GEN_ART_NUMBER], L.[CLR]
        """
        rows = conn.execute(text(sql), params).fetchall()
        # Column index map: 4 keys + present cols + 2 alloc-join cols
        keys = ["WERKS", "MAJ_CAT", "GEN_ART_NUMBER", "CLR"]
        all_cols = keys + present + ["alloc_qty_a", "hold_qty_a"]
        data = []
        for r in rows:
            rec: Dict[str, Any] = {}
            for i, c in enumerate(all_cols):
                v = r[i]
                rec[c] = v if not isinstance(v, bytes) else v.decode("utf-8", "ignore")
            # Convert BIGINT to int safely
            if rec.get("GEN_ART_NUMBER") is not None:
                try: rec["GEN_ART_NUMBER"] = int(rec["GEN_ART_NUMBER"])
                except Exception: pass
            # Prefer alloc-side totals when available; falls back to working
            # table values when alloc table isn't present.
            if rec.get("alloc_qty_a") is not None and "ALLOC_QTY" in rec:
                rec["ALLOC_QTY"] = rec["alloc_qty_a"]
            if rec.get("hold_qty_a") is not None and "HOLD_QTY" in rec:
                rec["HOLD_QTY"] = rec["hold_qty_a"]
            rec.pop("alloc_qty_a", None)
            rec.pop("hold_qty_a", None)
            data.append(rec)
        return {"success": True, "data": data, "columns": keys + present,
                "maj_cat": maj_cat, "rdc": rdc, "werks": werks}


@router.get("/var-summary")
def var_summary(
    maj_cat: str,
    werks: str,
    gen_art: int,
    clr: str = "",
    rdc: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Per-VAR_ART × SZ drill for ONE OPT (WERKS, MAJ_CAT, GEN_ART, CLR).

    Returns one row per (VAR_ART, SZ) from ARS_ALLOC_WORKING with:
    VAR_DESC, MRP, SZ_MBQ, SZ_STK, SZ_REQ, ALLOC_QTY, HOLD_QTY, FNL_Q,
    FNL_Q_REM, ALLOC_STATUS, SKIP_REASON, ALLOC_WAVE, ALLOC_ROUND,
    ALLOC_REMARKS, FROM_HOLD_QTY.
    """
    A_TBL = "ARS_ALLOC_WORKING"
    de = get_data_engine()
    with de.connect() as conn:
        conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        if not _table_exists(conn, A_TBL):
            return {"success": True, "data": [], "maj_cat": maj_cat,
                    "werks": werks, "gen_art": gen_art, "clr": clr, "rdc": rdc}
        ac = _get_columns(conn, A_TBL)
        if not all(k in ac for k in ("WERKS", "MAJ_CAT", "GEN_ART_NUMBER",
                                       "VAR_ART", "SZ")):
            return {"success": True, "data": [], "maj_cat": maj_cat,
                    "werks": werks, "gen_art": gen_art, "clr": clr, "rdc": rdc}
        # Optional columns
        var_cols = {
            "RDC", "VAR_DESC", "MRP", "PAK_SZ", "OPT_TYPE",
            "OPT_PRIORITY_RANK", "ST_RANK", "IS_NEW", "I_ROD",
            "CONT", "SZ_MBQ", "SZ_MBQ_WH", "SZ_STK", "SZ_REQ", "SZ_REQ_WH",
            "FNL_Q", "FNL_Q_REM",
            "POOL_CONSUMED", "SHIP_QTY", "HOLD_QTY", "ALLOC_QTY",
            "FROM_HOLD_QTY",
            "ALLOC_STATUS", "SKIP_REASON", "ALLOC_REMARKS",
            "ALLOC_WAVE", "ALLOC_ROUND",
        }
        present = [c for c in var_cols if c in ac]
        sel_list = ", ".join(f"[{c}]" for c in present)

        params: Dict[str, Any] = {"mc": maj_cat, "werks": werks,
                                    "ga": int(gen_art), "clr": clr or ""}
        rdc_filter = ""
        if rdc:
            params["rdc"] = rdc
            rdc_filter = " AND [RDC] = :rdc"
        sql = f"""
            SELECT [WERKS], [MAJ_CAT], [GEN_ART_NUMBER], [CLR],
                   [VAR_ART], [SZ], {sel_list}
            FROM [{A_TBL}] WITH (NOLOCK)
            WHERE [MAJ_CAT] = :mc
              AND [WERKS] = :werks
              AND TRY_CAST([GEN_ART_NUMBER] AS BIGINT) = :ga
              AND ISNULL([CLR],'') = :clr
              {rdc_filter}
            ORDER BY [VAR_ART], [SZ]
        """
        rows = conn.execute(text(sql), params).fetchall()
        keys = ["WERKS", "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "VAR_ART", "SZ"]
        all_cols = keys + present
        data = []
        for r in rows:
            rec: Dict[str, Any] = {}
            for i, c in enumerate(all_cols):
                v = r[i]
                rec[c] = v if not isinstance(v, bytes) else v.decode("utf-8", "ignore")
            if rec.get("GEN_ART_NUMBER") is not None:
                try: rec["GEN_ART_NUMBER"] = int(rec["GEN_ART_NUMBER"])
                except Exception: pass
            if rec.get("VAR_ART") is not None:
                try: rec["VAR_ART"] = int(rec["VAR_ART"])
                except Exception: pass
            data.append(rec)
        return {"success": True, "data": data, "columns": keys + present,
                "maj_cat": maj_cat, "werks": werks, "gen_art": gen_art,
                "clr": clr, "rdc": rdc}


@router.get("/sloc-breakdown")
def sloc_breakdown(
    maj_cat: str,
    rdc: Optional[str] = None,
    werks: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """SLOC-wise inventory breakdown for STORE_STOCK drill.

    The SLOC columns on ARS_LISTING are dynamic (discovered from the
    grid table at generate time). Returns the SUM of each SLOC column
    for the selected (MAJ_CAT [, RDC] [, WERKS]) — one entry per SLOC.
    Plus STK_TTL grand-total for sanity.
    """
    if not (maj_cat or "").strip():
        return {"success": True, "data": [], "maj_cat": maj_cat,
                "rdc": rdc, "werks": werks}
    de = get_data_engine()
    with de.connect() as conn:
        conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        if not _table_exists(conn, LISTING_TABLE):
            return {"success": True, "data": [], "maj_cat": maj_cat,
                    "rdc": rdc, "werks": werks}
        cols = _get_columns(conn, LISTING_TABLE)
        # Identity / known calc columns — everything else that's numeric is
        # treated as a SLOC. This mirrors how Part 1 builds the table:
        # SLOC cols = grid stock cols not in the skip set.
        non_sloc = {
            "WERKS", "RDC", "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "GEN_ART_DESC",
            "STK_TTL", "STR", "IS_NEW", "OPT_TYPE", "ACS_D", "ALC_D",
            "AUTO_GEN_ART_SALE", "AGE", "LISTING", "I_ROD",
            "CLR_MIN", "CLR_MAX", "FOCUS_W_CAP", "FOCUS_WO_CAP",
            "RL_HOLD_QTY", "MSA_FNL_Q", "VAR_COUNT", "VAR_FNL_COUNT",
            "PER_OPT_SALE", "OPT_MBQ", "OPT_REQ",
            "OPT_MBQ_WH", "OPT_REQ_WH", "EXCESS_STK",
            "ST_RANK", "MAX_DAILY_SALE",
            "FINAL_OPT_TYPE", "ALLOC_BATCH_ID", "ALLOC_TYPE",
            "OPT_TYPE_REASON", "FOCUS_FLAG", "CLR_CAP_MODE", "STR_BOOST_PCT",
            "MJ_MBQ", "MJ_STK_TTL", "MJ_REQ",
            "M_VND_CD", "RNG_SEG", "MACRO_MVGR", "MICRO_MVGR", "FAB",
        }
        # SLOC candidates: numeric (FLOAT-typed) columns not in the non_sloc set,
        # not ending in _REQ / _MBQ / _CONT / _STK_TTL / _STR / _OPT_CNT
        # / _DISP_Q / _WEIGHTAGE / _PER_OPT_SALE / _GRID_GROUP / _GROUP / _REM
        # (all of which are grid-prefix calc columns, not SLOC stock).
        bad_suffixes = ("_REQ", "_MBQ", "_CONT", "_STK_TTL", "_STR",
                        "_OPT_CNT", "_DISP_Q", "_WEIGHTAGE", "_PER_OPT_SALE",
                        "_GRID_GROUP", "_GROUP", "_REM", "_REASON")
        # We need data-type info to skip non-numeric — re-query INFORMATION_SCHEMA.
        type_rows = conn.execute(text(
            "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = :t"
        ), {"t": LISTING_TABLE}).fetchall()
        type_map = {r[0]: (r[1] or "").lower() for r in type_rows}
        numeric_types = {"float", "real", "int", "bigint", "smallint",
                         "tinyint", "decimal", "numeric", "money", "smallmoney"}
        sloc_cols = []
        for c in cols:
            if c in non_sloc:
                continue
            if type_map.get(c, "") not in numeric_types:
                continue
            if any(c.upper().endswith(s) for s in bad_suffixes):
                continue
            if c.upper().startswith("H_") or c.upper().startswith("GH_"):
                continue
            sloc_cols.append(c)

        params: Dict[str, Any] = {"mc": maj_cat}
        where_parts = ["[MAJ_CAT] = :mc"]
        if rdc:
            params["rdc"] = rdc
            where_parts.append("[RDC] = :rdc")
        if werks:
            params["werks"] = werks
            where_parts.append("[WERKS] = :werks")
        where_sql = " AND ".join(where_parts)

        # Single roundtrip — sum every SLOC + STK_TTL together.
        agg_exprs = [f"ISNULL(SUM(TRY_CAST([{c}] AS FLOAT)), 0) AS [{c}]"
                     for c in sloc_cols]
        agg_exprs.append("ISNULL(SUM(TRY_CAST([STK_TTL] AS FLOAT)), 0) AS [STK_TTL]")
        sql = f"""
            SELECT {', '.join(agg_exprs)}, COUNT(*) AS row_count
            FROM [{LISTING_TABLE}] WITH (NOLOCK)
            WHERE {where_sql}
        """
        row = conn.execute(text(sql), params).fetchone()
        if row is None:
            return {"success": True, "data": [], "stk_ttl": 0, "row_count": 0,
                    "maj_cat": maj_cat, "rdc": rdc, "werks": werks}

        data = []
        for i, c in enumerate(sloc_cols):
            qty = float(row[i] or 0)
            if qty > 0:
                data.append({"sloc": c, "qty": int(round(qty))})
        # Sort descending by qty
        data.sort(key=lambda x: x["qty"], reverse=True)
        stk_ttl  = int(round(float(row[len(sloc_cols)] or 0)))
        row_cnt  = int(row[len(sloc_cols) + 1] or 0)
        return {"success": True, "data": data, "stk_ttl": stk_ttl,
                "row_count": row_cnt, "sloc_count": len(sloc_cols),
                "maj_cat": maj_cat, "rdc": rdc, "werks": werks}


@router.get("/contribution")
def listing_contribution(
    maj_cats: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """
    Per-RDC contribution: total stock vs allocated qty for the selected
    MAJ_CAT(s). Drives the live "RDC Stock vs Alloc" chart on the listing
    page. When `maj_cats` is empty, returns the all-MAJ_CAT view.
    """
    mc_list = [m.strip() for m in (maj_cats or "").split(",") if m.strip()]
    de = get_data_engine()
    with de.connect() as conn:
        conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        if not _table_exists(conn, FINAL_TABLE):
            return {"success": True, "data": [], "maj_cats": mc_list}
        fc = _get_columns(conn, FINAL_TABLE)
        if "ALLOC_QTY" not in fc or "RDC" not in fc:
            return {"success": True, "data": [], "maj_cats": mc_list}
        stk_col = next((c for c in ("STK_TTL", "STK", "STOCK_QTY") if c in fc), None)
        params: Dict[str, Any] = {}
        where = ""
        if mc_list and "MAJ_CAT" in fc:
            keys = ", ".join(f":mc_{i}" for i in range(len(mc_list)))
            where = f"WHERE [MAJ_CAT] IN ({keys})"
            for i, m in enumerate(mc_list):
                params[f"mc_{i}"] = m
        stk_sel = (f"ROUND(ISNULL(SUM(TRY_CAST([{stk_col}] AS FLOAT)),0),0)"
                   if stk_col else "0")
        rows = conn.execute(text(f"""
            SELECT [RDC],
                   {stk_sel} AS stock,
                   ROUND(ISNULL(SUM(TRY_CAST([ALLOC_QTY] AS FLOAT)),0),0) AS alloc
            FROM [{FINAL_TABLE}]
            {where}
            GROUP BY [RDC]
            ORDER BY [RDC]
        """), params).fetchall()
        return {
            "success": True,
            "maj_cats": mc_list,
            "data": [
                {"rdc": r[0], "stock": int(r[1] or 0), "alloc": int(r[2] or 0)}
                for r in rows if r[0]
            ],
        }


@router.post("/migrate-columns")
def migrate_dpn_sald_columns(current_user: User = Depends(get_current_user)):
    """
    Rename DPN → ACS_D and SAL_D → ALC_D in ALL database tables.
    Scans every user table for these columns and renames them using sp_rename.
    Safe to run multiple times — skips tables that already have the new names.
    """
    de = get_data_engine()
    results = {"renamed": [], "skipped": [], "errors": []}

    with de.connect() as conn:
        # Find ALL tables that have DPN or SAL_D columns
        col_rows = conn.execute(text("""
            SELECT TABLE_NAME, COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE COLUMN_NAME IN ('DPN', 'SAL_D')
              AND TABLE_SCHEMA = 'dbo'
            ORDER BY TABLE_NAME, COLUMN_NAME
        """)).fetchall()

        rename_map = {"DPN": "ACS_D", "SAL_D": "ALC_D"}

        for tbl, col in col_rows:
            new_col = rename_map.get(col)
            if not new_col:
                continue

            # Check if new column already exists (don't rename if it does)
            existing = conn.execute(text("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = :tbl AND COLUMN_NAME = :new_col AND TABLE_SCHEMA = 'dbo'
            """), {"tbl": tbl, "new_col": new_col}).scalar()

            if existing:
                results["skipped"].append(f"{tbl}.{col} (already has {new_col})")
                continue

            try:
                conn.execute(text(
                    f"EXEC sp_rename '[{tbl}].[{col}]', '{new_col}', 'COLUMN'"
                ))
                conn.commit()
                results["renamed"].append(f"{tbl}.{col} → {new_col}")
                logger.info(f"Renamed {tbl}.{col} → {new_col}")
            except Exception as e:
                results["errors"].append(f"{tbl}.{col}: {str(e)[:100]}")
                logger.warning(f"Failed to rename {tbl}.{col}: {e}")

        # Verify: count remaining DPN/SAL_D columns
        remaining = conn.execute(text("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
            WHERE COLUMN_NAME IN ('DPN', 'SAL_D') AND TABLE_SCHEMA = 'dbo'
        """)).scalar()
        results["remaining_old_columns"] = remaining

    total = len(results["renamed"])
    return {
        "success": True,
        "message": f"Renamed {total} columns. {len(results['skipped'])} skipped, {len(results['errors'])} errors, {remaining} remaining.",
        "data": results
    }


@router.get("/export")
def export_listing(
    filters: Optional[str] = None,
    table: str = Query("working", pattern="^(listing|working|alloc)$"),
    current_user: User = Depends(get_current_user),
):
    """Export active table (Working, Full Listing, or Alloc) to Excel."""
    import pandas as pd

    tbl = {"working": FINAL_TABLE, "alloc": ALLOC_TABLE}.get(table, LISTING_TABLE)
    de = get_data_engine()
    with de.connect() as conn:
        if not _table_exists(conn, tbl):
            raise HTTPException(404, f"{tbl} not found.")

        cols = _get_columns(conn, tbl)
        where_parts, params = _build_filter_where(filters, set(cols))
        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

        col_list = ", ".join(f"[{c}]" for c in cols)
        order = _safe_order(cols, table)
        sql = f"SELECT {col_list} FROM [{tbl}]{where_sql} ORDER BY {order}"
        df = pd.read_sql(text(sql), conn, params=params)

    sheet = {"working": "ARS_LISTING_WORKING", "alloc": "ARS_ALLOC_WORKING"}.get(table, "ARS_LISTING")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=ARS_LISTING_{len(df)}_rows.xlsx"}
    )
