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
from pydantic import BaseModel, field_validator
from typing import Any, Dict, Literal, Optional, List
from sqlalchemy import text
from loguru import logger

from app.database.session import get_data_engine
from app.security.dependencies import get_current_user, RequireRoles
from app.services.business_rules import rule_flag, rule_value
from app.models.rbac import User
from app.utils.db_helpers import (
    run_sql, table_exists, get_columns, msa_expr, msa_col,
)
from app.services.alloc_pool import (
    NoPoolColumnsError, get_sloc_type_map, typed_sloc_cols,
    pool_stk_expr, pend_agg_sql, hold_agg_sql, fnl_q_eff_expr,
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
    # Fresh/GRT hold control (FS-04): UPC flag + segment for the FS-05/FS-06
    # suppression predicate
    "ST_STATUS", "SEG",
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
    # Secondary-grid dispatch cap (strict mode — only mode after 2026-06-16).
    # When True, the main-pass pre-gate enforces, per grid where
    # ARS_GRID_BUILDER.sec_cap_applicable=1:
    #   budget = max(0, MBQ_ORIG × sec_cap_pct% − STK_TTL); breach = block.
    # Per-grid % comes from ARS_GRID_BUILDER.sec_cap_pct (default 130 when
    # NULL). No high-demand override — strict binary semantics.
    apply_sec_cap_in_normal: bool = True
    default_acs_d: float = 18.0        # Default ACS_D when NULL/0 (used in OPT_TYPE fallback classification)
    min_size_count: int = 3            # Min sizes required for TBL listing (alternative to 60% ratio)
    # R07 size-coverage gate. Skip TBL when VAR_FNL_COUNT / VAR_COUNT < size_threshold
    # AND VAR_FNL_COUNT < min_size_count. Independent of stock_threshold_pct (which
    # only drives OPT_TYPE classification). Defaults 0.6 to preserve prior behavior
    # — until this was split, R07 silently reused stock_threshold_pct.
    size_threshold: float = 0.6
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
    # Per-OPT_TYPE MBQ caps (Decision 4-B). Anchored to MJ_MBQ_ORIG, NOT the
    # post-growth MJ_MBQ_REV — so unchecking the "Use Default 100% (MBQ)" box
    # surfaces independent ceilings for RL / TBC / TBL on the *original* budget.
    # Defaults 100 = no cap (engine's _stage_c_apply_mbq_cap treats 0 as
    # disabled and ≥100 as headroom; we use 100 = no over-ship vs original MBQ).
    rl_mbq_cap_pct:  float = 100.0
    tbc_mbq_cap_pct: float = 100.0
    tbl_mbq_cap_pct: float = 100.0
    # Per-OPT_TYPE dispatch mode when an OPT's total_need exceeds its
    # per-WERKS MBQ budget (the live `werks_cap` from _live_mbq_budget).
    #   SCALED   — proportional round-then-shave; OPT ships partial up to
    #              werks_cap. Recovers the units that the legacy floor-per-
    #              size truncation silently lost when scale was just under 1.
    #   COMPLETE — all-or-skip. Skip the entire OPT, leave werks_cap intact
    #              for the next OPT in the band. Cleaner rod-completion at
    #              the cost of possibly stranding budget when every OPT in
    #              band exceeds the remaining cap.
    # Per-OPT engine settings.
    rl_dispatch_mode:  str = "COMPLETE"
    tbc_dispatch_mode: str = "COMPLETE"

    @field_validator("rl_dispatch_mode", "tbc_dispatch_mode")
    @classmethod
    def _force_complete_dispatch(cls, v):
        # SCALED dispatch is DISABLED (2026-07-17). The round-then-shave path
        # produced a contradictory OVERSHOOT+SCALE double-stamp when it ran as
        # an unguarded fall-through after a COMPLETE-mode overshoot admit (see
        # rule_engine_per_opt.py). COMPLETE is now the ONLY supported RL/TBC
        # dispatch mode. Coerce any incoming value — stale saved setting,
        # direct-API caller, or legacy UI — to COMPLETE so SCALED can never
        # take effect regardless of what the client sends.
        return "COMPLETE"
    # MJ_MBQ growth headroom (Allocation Gate).  100 = strict (waterfall stops
    # at the MAJ_CAT target, current default).  >100 scales MJ_MBQ to a
    # SIBLING column MJ_MBQ_REV — the original MJ_MBQ is preserved untouched —
    # and MJ_REQ_REV is re-derived as MAX(0, MJ_MBQ_REV − MJ_STK_TTL).  When
    # >100, MJ_REQ is then promoted to MJ_REQ_REV so every downstream engine
    # consumer (revalidate, OPT_MJ_REQ gate, store-broken pre-band, post-
    # waterfall MJ_REQ_REM recompute) reads the scaled ceiling with no math
    # change.  Original MJ_REQ value is kept in MJ_REQ_ORIG for audit.
    mj_req_growth_pct: float = 100.0
    # Allocation mode: 'per_opt' is the ONLY supported engine (2026-07-10
    # removal — see docs/REMOVAL_PLAN_PER_OPT_ONLY.md). The field is kept so
    # old scripts/replays fail LOUDLY: /generate hard-400s on any other value
    # instead of silently running a renamed engine. Dispatch goes through
    # rule_engine_pandas.run_listing_and_allocation_pandas (orchestration:
    # worker pool, writer queue, table loads, Stage D, write-back), whose
    # band is hard-pinned to rule_engine_per_opt._run_band_per_opt.
    allocation_mode:  str = "per_opt"
    parallel_workers: int = 8        # worker pool size for the per_opt run
    # Per-run override for the single-writer-queue path. None → use .env default
    # (settings.USE_WRITER_QUEUE). True/False → force on/off for this run only.
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
    # CONT fallback for SZ_APPLICABLE='N' MAJ_CATs (see rule_engine_new
    # _stage_b_fill_cont docstring). ALWAYS fills so Σ CONT = 1 per OPT —
    # size-agnostic categories never leave 100% unallocated. Applied after
    # the Site → CO ladder. SZ_APPLICABLE='Y' MAJ_CATs are untouched.
    #   'P4_UNIFORM' — CONT = 1 / COUNT(DISTINCT SZ) per OPT (default)
    #   'P3_FNL_Q'   — CONT = FNL_Q / SUM(FNL_Q) per OPT
    #   'STRICT'     — deprecated (still handled backend-side for API back-
    #                  compat); UI no longer exposes this
    cont_fallback_mode: str = "P4_UNIFORM"
    # ── Fresh/GRT typed pool + selective hold control ────────────────────
    # (FSD_FRESH_GRT_HOLD_CONTROL FS-02/FS-03/FS-05/FS-06)
    # alloc_type is REQUIRED — no default, 422 when missing/invalid (E-02).
    # Every run allocates from exactly one warehouse pool; SLOC → pool
    # membership lives in ARS_SLOC_SETTINGS (FS-01). Stage A/B recomputes
    #   FNL_Q_EFF = max(min(Σ SLOC-cols(type) − PEND_T − HOLD_T, FNL_Q), 0)
    alloc_type: Literal["FRESH", "GRT"]
    # Hold-suppression toggles (per_opt / pandas modes only — IM-3 guard):
    #   skip_hold_upc      — True  = UPC stores get no TBL warehouse hold
    #   apply_hold_seg_app — False = APP options get no TBL warehouse hold
    #   apply_hold_seg_gm  — False = GM options get no TBL warehouse hold
    # Suppressed rows are budgeted like RL/TBC from the start (FS-05):
    # OPT_MBQ_WH = OPT_MBQ, and OPT_REQ_WH follows automatically.
    skip_hold_upc: bool = False
    apply_hold_seg_app: bool = True
    apply_hold_seg_gm: bool = True
    # ── Run dates (information-only, 2026-07-18) ─────────────────────────
    # Pure metadata stamped onto every allocation output row — they do NOT
    # enter any stock/MSA/allocation math. Carried WORKING → PARKED →
    # HISTORY by the column-introspection copy in parked_history.py.
    #   stock_consider_dt — MANDATORY. The business date the stock figures
    #                       reflect (UI defaults it to D-1 / yesterday).
    #   picking_dt        — MANDATORY (2026-07-18). Planned warehouse
    #                       picking/dispatch date. UI leaves it BLANK by
    #                       default so the user must consciously pick a date
    #                       per requirement — there is no sensible default.
    # Both are ISO 'YYYY-MM-DD' strings; neither may be empty.
    stock_consider_dt: str
    picking_dt: str

    @field_validator("stock_consider_dt", "picking_dt")
    @classmethod
    def _require_run_date(cls, v, info):
        from datetime import date as _date
        field = info.field_name
        s = (str(v).strip() if v is not None else "")
        if not s:
            raise ValueError(f"{field} is required (YYYY-MM-DD)")
        try:
            _date.fromisoformat(s)
        except ValueError:
            raise ValueError(
                f"{field} must be a valid YYYY-MM-DD date, got {v!r}"
            )
        return s


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

            # Store code → name map so the UI can show "HB05 — GUWAHATI"
            if "ST_NM" in st_cols:
                name_rows = conn.execute(text(
                    f"SELECT DISTINCT [ST_CD], [ST_NM] FROM [Master_ALC_INPUT_ST_MASTER]{listing_filter}"
                )).fetchall()
                result["store_name_map"] = {
                    str(r[0]).strip(): str(r[1]).strip() for r in name_rows if r[0] and r[1]
                }

            # Store code → HUB map so the UI can search/bulk-select by hub
            # (store_rdc_map below covers the same for RDC)
            if "HUB" in st_cols:
                hub_rows = conn.execute(text(
                    f"SELECT DISTINCT [ST_CD], [HUB] FROM [Master_ALC_INPUT_ST_MASTER]{listing_filter}"
                )).fetchall()
                result["store_hub_map"] = {
                    str(r[0]).strip(): str(r[1]).strip() for r in hub_rows if r[0] and r[1]
                }

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

            # MAJ_CAT → SEG/DIV/SUB_DIV/SSN map so the UI can search and
            # bulk-select MAJ_CATs by hierarchy level (all four are
            # single-valued per MAJ_CAT in ARS_MSA_GEN_ART; MAX() collapses
            # the group cleanly).
            attr_rows = conn.execute(text(
                "SELECT [MAJ_CAT], MAX([SEG]), MAX([DIV]), MAX([SUB_DIV]), MAX([SSN]) "
                "FROM [ARS_MSA_GEN_ART] WHERE [MAJ_CAT] IS NOT NULL GROUP BY [MAJ_CAT]"
            )).fetchall()
            result["maj_cat_attr_map"] = {
                str(r[0]).strip(): {
                    "seg":     str(r[1]).strip() if r[1] else None,
                    "div":     str(r[2]).strip() if r[2] else None,
                    "sub_div": str(r[3]).strip() if r[3] else None,
                    "ssn":     str(r[4]).strip() if r[4] else None,
                }
                for r in attr_rows if r[0]
            }

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
    "size_threshold": "0.6",
    "pri_ct_check_rl": "false",
    "pri_ct_check_tbc": "false",
    "rl_mj_req_cap_pct": "100.0",
    "tbc_mj_req_cap_pct": "100.0",
    "tbl_mj_req_cap_pct": "100.0",
    "rl_mbq_cap_pct": "100.0",
    "tbc_mbq_cap_pct": "100.0",
    "tbl_mbq_cap_pct": "100.0",
    "rl_dispatch_mode":  "COMPLETE",
    "tbc_dispatch_mode": "COMPLETE",
    "mj_req_growth_pct": "100.0",
    "allow_multi_parked": "false",
    "cont_fallback_mode": "P4_UNIFORM",  # P4_UNIFORM (default) | P3_FNL_Q
    # Fresh/GRT hold control (FS-03). alloc_type persists the last-used pool
    # for display only — the UI still forces an explicit choice per run.
    "alloc_type": "",
    "skip_hold_upc": "false",
    "apply_hold_seg_app": "true",
    "apply_hold_seg_gm": "true",
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


# ── Parking mode (admin-only) ────────────────────────────────────────────────
# The Single/Multiple parking toggle was moved out of ListingPage into the
# Settings page (Application tab).  Only SUPER_ADMIN / ADMIN can flip it;
# regular users see the effective value in /listing/config but can't change
# it.  Persisted under the same AppSettings key (`listing.allow_multi_parked`)
# so the existing /generate guard at line ~405 keeps working unchanged — it
# now reads the persisted setting rather than the request payload field.

@router.get("/parking-mode")
def get_parking_mode(current_user: User = Depends(get_current_user)):
    """Return the active parking-mode setting.  Any authenticated user can read.
    `allow_multi_parked = True` → multi-parked (new runs stack alongside
    pending parked sessions).  False → single-parked (block new runs while a
    session is awaiting review).
    Source of truth since 2026-07-31 = business rule ALC_MULTI_PARKED
    (Settings → Business Rules); this endpoint reflects it so the legacy
    Settings → Application toggle stays in sync."""
    return {"success": True,
            "data": {"allow_multi_parked": rule_flag('ALC_MULTI_PARKED', False)}}


@router.put("/parking-mode")
def set_parking_mode(
    body: dict,
    current_user: User = Depends(RequireRoles(["ADMIN", "SUPER_ADMIN"])),
):
    """Admin-only: set the parking-mode setting.  Body: `{"allow_multi_parked": bool}`.
    Writes the business rule ALC_MULTI_PARKED (single source of truth) and
    mirrors into the legacy AppSettings key for back-compat readers."""
    raw = body.get("allow_multi_parked")
    if raw is None:
        raise HTTPException(status_code=400, detail="allow_multi_parked required")
    val = bool(raw) if isinstance(raw, bool) else \
          str(raw).lower() in ("true", "1", "yes", "on")
    from app.services.business_rules import update_rule as _br_update
    _br_update('ALC_MULTI_PARKED', is_active=val,
               user=getattr(current_user, "username", None) or "admin")
    de = get_data_engine()
    with de.connect() as conn:
        _save_listing_settings(conn, {"allow_multi_parked": str(val).lower()})
    logger.info(
        f"[parking-mode] changed by {current_user.username} → "
        f"allow_multi_parked={val} (business rule ALC_MULTI_PARKED)"
    )
    return {"success": True, "data": {"allow_multi_parked": val}}


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
    #
    # Source of truth = AppSettings (`listing.allow_multi_parked`), managed
    # by admins from Settings → Application.  The request-payload field is
    # kept for backwards compatibility but ignored here — non-admins can't
    # smuggle in `allow_multi_parked=true` via a hand-crafted call.
    de = get_data_engine()
    try:
        with de.connect() as _pc:
            _persisted = _load_listing_settings(_pc).get("allow_multi_parked", "false")
        _legacy_mp = str(_persisted).lower() in ("true", "1", "yes")
    except Exception as _e:
        logger.warning(f"[generate] failed to read persisted parking mode: {_e}")
        _legacy_mp = False
    # Business rule ALC_MULTI_PARKED is the source of truth (2026-07-31);
    # the legacy AppSettings value only serves as the fallback default if
    # the rules table is unreachable / the row is missing.
    _allow_mp = rule_flag('ALC_MULTI_PARKED', _legacy_mp)
    # Reflect the persisted value back onto the request so downstream
    # consumers (audit log, persisted-settings rewrite at line ~585) see
    # the authoritative value.
    req.allow_multi_parked = _allow_mp
    # Admin bypass: ADMIN / SUPER_ADMIN can already flip Parking Mode in
    # Settings → Application, so the "ask an admin" 409 message is hostile
    # when an admin hits the guard themselves.  Skip the guard for them and
    # log the bypass so the audit trail records who overrode it.
    _user_roles = set(getattr(current_user, "role_codes", []) or [])
    _is_admin_bypass = bool(_user_roles.intersection({"ADMIN", "SUPER_ADMIN"}))
    if not _allow_mp and parked_history.has_pending_parked():
        if _is_admin_bypass:
            logger.warning(
                f"[generate] parked-session guard bypassed by admin "
                f"{getattr(current_user, 'username', None)} "
                f"(role_codes={sorted(_user_roles)}) — a parked session is "
                f"awaiting review but the run was admitted anyway."
            )
        else:
            raise HTTPException(
                409,
                "A parked session is awaiting review. Please approve or reject it "
                "from the Parked Runs page before generating a new one. If you "
                "need to stack multiple parked snapshots, ask an admin to switch "
                "Parking Mode to 'Multiple' under Settings → Application."
            )

    # ── Engine guard — per_opt is the only allocation engine ────────────
    # pandas/sequential were removed 2026-07-10 (identical inputs produced
    # divergent allocations — see docs/REMOVAL_PLAN_PER_OPT_ONLY.md). Reject
    # loudly instead of accept-and-ignore so old scripts/replays fail fast.
    # This also subsumes the former IM-3 guard: the hold-suppression toggles
    # (skip_hold_upc / apply_hold_seg_app / apply_hold_seg_gm) are fully
    # supported by per_opt, so no per-mode toggle rejection is needed.
    _req_mode = (req.allocation_mode or "").strip().lower()
    if _req_mode != "per_opt":
        raise HTTPException(
            400,
            f"allocation_mode '{req.allocation_mode}' was removed 2026-07-10 "
            f"— only 'per_opt' is supported"
        )
    # E-01: the MSA must expose at least one SLOC pivot column of the
    # requested pool type — otherwise the run would allocate from an empty
    # pool. Missing MSA table is left to the impl's own 400.
    try:
        with de.connect() as _pool_conn:
            if table_exists(_pool_conn, req.msa_table):
                typed_sloc_cols(
                    _pool_conn, req.msa_table, req.alloc_type,
                    get_sloc_type_map(_pool_conn),
                )
    except NoPoolColumnsError:
        raise HTTPException(
            400,
            f"MSA contains no {req.alloc_type} SLOC columns. Regenerate MSA "
            f"including {req.alloc_type} SLOCs (see ARS_SLOC_SETTINGS)."
        )
    except HTTPException:
        raise
    except Exception as _pool_err:
        logger.warning(f"[generate] E-01 pool-column pre-check failed: {_pool_err}")

    session_id = make_session_id()
    user_name  = getattr(current_user, "username", None)
    req_dict   = req.dict() if hasattr(req, "dict") else dict(req.__dict__)
    mode       = "per_opt"  # only engine — guarded above with a hard 400
    # batch_id == session_id so the UI can poll /alloc-progress with the
    # same id it gets back here, and the queue table rows line up with the
    # session row.
    alloc_batch_id = session_id

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
                "size_threshold": str(req.size_threshold),
                "pri_ct_check_rl": str(req.pri_ct_check_rl).lower(),
                "pri_ct_check_tbc": str(req.pri_ct_check_tbc).lower(),
                "rl_mj_req_cap_pct": str(req.rl_mj_req_cap_pct),
                "tbc_mj_req_cap_pct": str(req.tbc_mj_req_cap_pct),
                "tbl_mj_req_cap_pct": str(req.tbl_mj_req_cap_pct),
                "rl_mbq_cap_pct":  str(req.rl_mbq_cap_pct),
                "tbc_mbq_cap_pct": str(req.tbc_mbq_cap_pct),
                "tbl_mbq_cap_pct": str(req.tbl_mbq_cap_pct),
                "rl_dispatch_mode":  str(req.rl_dispatch_mode),
                "tbc_dispatch_mode": str(req.tbc_dispatch_mode),
                "mj_req_growth_pct": str(req.mj_req_growth_pct),
                "allow_multi_parked": str(req.allow_multi_parked).lower(),
                "cont_fallback_mode": str(req.cont_fallback_mode).upper(),
                "alloc_type": str(req.alloc_type),
                "skip_hold_upc": str(req.skip_hold_upc).lower(),
                "apply_hold_seg_app": str(req.apply_hold_seg_app).lower(),
                "apply_hold_seg_gm": str(req.apply_hold_seg_gm).lower(),
            })
    except Exception as e:
        logger.error(f"[generate] failed to persist listing settings: {e}")

    # ── Audit log: per-run snapshot of every input parameter ───────────
    # ARS_RUN_PARAMS_AUDIT — one row per param per run.  Lets reviewers
    # answer "what % / I_ROD / hold-days / cap did we use last week?" in
    # a single query, and trace param drift across days without grep'ing
    # logs.  Idempotent: CREATE IF NOT EXISTS + bulk insert.
    try:
        _run_uid = session_id or preset_batch_id or f"run-{int(time.time()*1000)}"
        # Sec-cap growth matrix — snapshot BEFORE the run so the audit row
        # reflects exactly the matrix each engine will see. Best-effort:
        # audit never blocks the run, so any load failure stamps '{}'.
        try:
            from app.services.sec_cap_growth_matrix import (
                load_matrix as _load_growth_matrix,
                snapshot_matrix as _snapshot_growth_matrix,
            )
            _gm_enabled, _gm_bands = _load_growth_matrix(de)
            _growth_matrix_snapshot = json.dumps(
                _snapshot_growth_matrix(_gm_enabled, _gm_bands)
            )
        except Exception as _gm_err:
            logger.warning(f"[audit] growth matrix snapshot skipped: {_gm_err}")
            _growth_matrix_snapshot = "{}"
            _gm_enabled = False  # so the sec_cap_mode row below never NameErrors
            _gm_bands = []

        # ── Grid config + matrix snapshot (extra audit rows) ───────────────
        # Capture the grid configuration that was in force during this run
        # (ARS_GRID_BUILDER) so reviewers can diff grid state across runs and
        # trend grid metrics. One readable, diffable row per grid + numeric
        # summary rows. Best-effort — never blocks a run.
        _extra_rows: List[tuple] = []
        try:
            with de.connect() as _gc:
                _grid_rows_db = _gc.execute(text(
                    "SELECT grid_name, status, grid_group, sec_cap_applicable, "
                    "sec_cap_pct, weightage, use_for_opt_sale "
                    "FROM [ARS_GRID_BUILDER] ORDER BY seq, grid_name"
                )).fetchall()
            _n_active = 0
            _capping_names: List[str] = []
            for _gname, _gstatus, _ggroup, _gseccap, _gcap_pct, _gwt, _gopt in _grid_rows_db:
                _gname = str(_gname)
                _is_active  = str(_gstatus or "").strip().lower() == "active"
                _is_capping = bool(_gseccap)
                if _is_active:
                    _n_active += 1
                if _is_capping:
                    _capping_names.append(_gname)
                _parts = [str(_gstatus or "—"), str(_ggroup or "—"),
                          f"capping {'ON' if _is_capping else 'off'}"]
                if _is_capping and _gcap_pct is not None:
                    _parts.append(f"cap {float(_gcap_pct):g}%")
                if _gwt is not None:
                    _parts.append(f"wt {float(_gwt):g}")
                if _gopt:
                    _parts.append("opt_sale")
                _extra_rows.append(("GRIDS", _gname, " · ".join(_parts)))
                if _is_capping and _gcap_pct is not None:
                    # numeric per-capping-grid cap% — trendable
                    _extra_rows.append(("GRIDS", f"cap_pct::{_gname}", f"{float(_gcap_pct):g}"))
            _extra_rows.append(("GRIDS", "active_grid_count",  str(_n_active)))
            _extra_rows.append(("GRIDS", "capping_grid_count", str(len(_capping_names))))
            _extra_rows.append(("GRIDS", "capping_grids",      ",".join(_capping_names)))
        except Exception as _grid_err:
            logger.warning(f"[audit] grid config snapshot skipped: {_grid_err}")
        # Matrix band values as a readable row (the full JSON is stamped below).
        try:
            if _gm_bands:
                _band_strs = []
                for _lo, _hi, _g in _gm_bands:
                    _rng = (f"{float(_lo):g}-{float(_hi):g}" if _hi is not None
                            else f"{float(_lo):g}+")
                    _band_strs.append(f"{_rng}→{float(_g):g}%")
                _extra_rows.append(("SEC_CAP", "matrix_bands", "; ".join(_band_strs)))
        except Exception as _mb_err:
            logger.warning(f"[audit] matrix band rows skipped: {_mb_err}")

        _audit_rows = [
            # LISTING params
            ("LISTING",    "stock_threshold_pct",   str(req.stock_threshold_pct)),
            ("LISTING",    "excess_multiplier",     str(req.excess_multiplier)),
            ("LISTING",    "hold_days",             str(req.hold_days)),
            ("LISTING",    "age_threshold",         str(req.age_threshold)),
            ("LISTING",    "default_acs_d",         str(req.default_acs_d)),
            ("LISTING",    "min_size_count",        str(req.min_size_count)),
            ("LISTING",    "size_threshold",        str(req.size_threshold)),
            ("LISTING",    "mix_mode",              str(req.mix_mode)),
            ("LISTING",    "rdc_mode",              str(req.rdc_mode)),
            ("LISTING",    "run_mode",              str(req.run_mode)),
            ("LISTING",    "ssn_values",            json.dumps(req.ssn_values)),
            # Run dates (information-only, 2026-07-18)
            ("LISTING",    "stock_consider_dt",     str(req.stock_consider_dt)),
            ("LISTING",    "picking_dt",            str(req.picking_dt)),
            # RANKING
            ("RANKING",    "req_weight",            str(req.req_weight)),
            ("RANKING",    "fill_weight",           str(req.fill_weight)),
            # ALLOCATION gates
            ("ALLOCATION", "pri_ct_check_rl",       str(req.pri_ct_check_rl).lower()),
            ("ALLOCATION", "pri_ct_check_tbc",      str(req.pri_ct_check_tbc).lower()),
            ("ALLOCATION", "rl_mj_req_cap_pct",     str(req.rl_mj_req_cap_pct)),
            ("ALLOCATION", "tbc_mj_req_cap_pct",    str(req.tbc_mj_req_cap_pct)),
            ("ALLOCATION", "tbl_mj_req_cap_pct",    str(req.tbl_mj_req_cap_pct)),
            ("ALLOCATION", "rl_mbq_cap_pct",        str(req.rl_mbq_cap_pct)),
            ("ALLOCATION", "tbc_mbq_cap_pct",       str(req.tbc_mbq_cap_pct)),
            ("ALLOCATION", "tbl_mbq_cap_pct",       str(req.tbl_mbq_cap_pct)),
            ("ALLOCATION", "rl_dispatch_mode",      str(req.rl_dispatch_mode)),
            ("ALLOCATION", "tbc_dispatch_mode",     str(req.tbc_dispatch_mode)),
            ("ALLOCATION", "mj_req_growth_pct",     str(req.mj_req_growth_pct)),
            ("ALLOCATION", "opt_types",             json.dumps(req.opt_types)),
            ("ALLOCATION", "cont_fallback_mode",    str(req.cont_fallback_mode).upper()),
            # Fresh/GRT typed pool + hold suppression (FS-03)
            ("ALLOCATION", "alloc_type",            str(req.alloc_type)),
            ("ALLOCATION", "skip_hold_upc",         str(req.skip_hold_upc).lower()),
            ("ALLOCATION", "apply_hold_seg_app",    str(req.apply_hold_seg_app).lower()),
            ("ALLOCATION", "apply_hold_seg_gm",     str(req.apply_hold_seg_gm).lower()),
            # SEC_CAP
            ("SEC_CAP",    "apply_sec_cap_in_normal", str(req.apply_sec_cap_in_normal).lower()),
            # SEC_CAP fallback condition as a flat, reviewable flag (2026-07-18):
            # STANDARD = flat per-grid cap%; MATRIX = cont%→growth% band lookup.
            # Derived from the matrix `enabled` flag loaded above. The full
            # `growth_matrix` JSON row below still carries the bands; this row
            # exists so the "which sec-cap fallback did this run use?" question
            # is answerable without parsing JSON.
            ("SEC_CAP",    "sec_cap_mode",          ("MATRIX" if _gm_enabled else "STANDARD")),
            # SEC_CAP growth matrix snapshot (spec 2026-07-08). Loaded above
            # into _growth_matrix_snapshot; a single JSON row so historic
            # runs can be traced to the matrix active at the time (matches
            # cont_fallback_mode pattern).
            ("SEC_CAP",    "growth_matrix",         _growth_matrix_snapshot),
            # FLAGS
            ("FLAGS",      "allocation_mode",       str(req.allocation_mode)),
            ("FLAGS",      "parallel_workers",      str(req.parallel_workers)),
            ("FLAGS",      "use_writer_queue",      str(req.use_writer_queue)),
            ("FLAGS",      "allow_multi_parked",    str(req.allow_multi_parked).lower()),
        ]
        # Grid config + matrix band rows (built above) — one row per grid,
        # grid summary/numeric metrics, and the readable matrix bands.
        _audit_rows.extend(_extra_rows)
        with de.connect() as ac:
            _run(ac, """
                IF OBJECT_ID('ARS_RUN_PARAMS_AUDIT', 'U') IS NULL
                CREATE TABLE [ARS_RUN_PARAMS_AUDIT] (
                    AUDIT_ID     BIGINT IDENTITY(1,1) PRIMARY KEY,
                    RUN_ID       NVARCHAR(128)  NOT NULL,
                    SESSION_ID   NVARCHAR(128)  NULL,
                    USER_ID      NVARCHAR(128)  NULL,
                    RUN_TS       DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
                    PARAM_GROUP  NVARCHAR(32)   NOT NULL,
                    PARAM_NAME   NVARCHAR(64)   NOT NULL,
                    PARAM_VALUE  NVARCHAR(512)  NULL,
                    SOURCE       NVARCHAR(16)   NOT NULL DEFAULT 'UI',
                    INDEX IX_RUN_PARAMS_RUN     (RUN_ID),
                    INDEX IX_RUN_PARAMS_SESSION (SESSION_ID, RUN_TS DESC)
                )
            """)
            ac.execute(text("""
                INSERT INTO [ARS_RUN_PARAMS_AUDIT]
                    (RUN_ID, SESSION_ID, USER_ID, PARAM_GROUP, PARAM_NAME,
                     PARAM_VALUE, SOURCE)
                VALUES
                    (:run_id, :session_id, :user_id, :grp, :name, :val, :src)
            """), [
                {"run_id": _run_uid, "session_id": session_id,
                 "user_id": (current_user.username if current_user else None),
                 "grp": grp, "name": pname, "val": pval, "src": "UI"}
                for grp, pname, pval in _audit_rows
            ])
            # CRITICAL: commit the INSERT. `_run` (run_sql) auto-commits the
            # CREATE above, but this executemany goes through ac.execute()
            # directly — without this commit SQLAlchemy rolls it back when the
            # `with de.connect()` block closes, so the table was created but
            # stayed EMPTY on every run (bug found 2026-07-18).
            ac.commit()
        logger.info(f"[audit] ARS_RUN_PARAMS_AUDIT: {len(_audit_rows)} rows for run={_run_uid}")
    except Exception as e:
        # Audit failure must never abort the run.
        logger.warning(f"[audit] ARS_RUN_PARAMS_AUDIT insert failed: {e}")

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

        # FS-04(a): ST_STATUS from the store master — feeds the skip_hold_upc
        # suppression (FS-05). Missing column → '' (a row is never suppressed
        # on an unknown status).
        _has_st_status = "ST_STATUS" in st_cols
        if not _has_st_status:
            logger.warning(
                f"FS-04: [{req.st_master_table}] has no ST_STATUS column — "
                f"skip_hold_upc cannot match UPC stores"
            )

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
            # FS-04(a): carry ST_STATUS through the store pool ('' when the
            # master lacks the column so downstream SELECTs stay uniform).
            st_status_sel = (
                ", ISNULL([ST_STATUS], '') AS ST_STATUS" if _has_st_status
                else ", CAST('' AS NVARCHAR(50)) AS ST_STATUS"
            )
            return (f"SELECT DISTINCT [ST_CD], [{st_rdc_col}] AS RDC"
                    f"{st_status_sel} FROM [{req.st_master_table}]{w}")

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
                [OPT_TYPE] NVARCHAR(10) NULL,
                [ST_STATUS] NVARCHAR(50) NULL
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

        all_cols = f"[WERKS], [RDC], [MAJ_CAT], [GEN_ART_NUMBER], [CLR]{stk_ins_str}, [STK_TTL], [STR], [IS_NEW], [OPT_TYPE], [ST_STATUS]"

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
                {stk_sel_str}, {stk_ttl} AS STK_TTL, {str_ttl} AS STR, 0 AS IS_NEW, NULL AS OPT_TYPE, S.[ST_STATUS]
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
                {stk_zeros_str}, 0 AS STK_TTL, 0 AS STR, 1 AS IS_NEW, NULL AS OPT_TYPE, S.[ST_STATUS]
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

        # Part 3.5a override: CLR 'A' / 'A_MIX' → I_ROD floor (TRUE floor,
        # 2026-07-31: MAX semantics — a maintained I_ROD of 3/4 from
        # ARS_CALC_ST_ART / ST_MAJ_CAT is KEPT, only lower values are lifted.
        # Business rule LST_IROD_AMIX_FLOOR: active → floor value (default 2,
        # bounds 1..4); INACTIVE → no floor at all (maintained I_ROD used
        # as-is). The old unconditional SET silently downgraded maintained
        # values.
        _irod_floor = rule_value('LST_IROD_AMIX_FLOOR', None)
        if _irod_floor:
            _irod_floor = int(_irod_floor)
            _run(conn, f"""
                UPDATE [{LISTING_TABLE}]
                SET [I_ROD] = {_irod_floor}
                WHERE UPPER(LTRIM(RTRIM([CLR]))) IN ('A', 'A_MIX')
                  AND ISNULL(TRY_CAST([I_ROD] AS FLOAT), 0) < {_irod_floor}
            """)
            logger.info(f"Part 3.5a: I_ROD floored to {_irod_floor} (MAX semantics) for CLR in ('A','A_MIX')")
        else:
            logger.info("Part 3.5a: LST_IROD_AMIX_FLOOR inactive — no A/A_MIX I_ROD floor applied")
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
            # FS-09: the RL_HOLD_QTY bake is pool-scoped — a hold created by
            # a run of the OTHER type is invisible here; legacy NULL/'' rows
            # match any run type. Column guard keeps pre-migration DBs
            # working (filter simply not applied there).
            _ht_has_type = "ALLOC_TYPE" in {
                c.upper() for c in _get_columns(conn, "ARS_NL_TBL_HOLD_TRACKING")
            }
            _ht_type_filter = (
                "\n                          AND   ([ALLOC_TYPE] = :pool_alloc_type "
                "OR ISNULL([ALLOC_TYPE],'') = '')"
                if _ht_has_type else ""
            )
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
                          AND   ISNULL([HOLD_REM],  0) > 0{_ht_type_filter}
                        GROUP BY [WERKS], [MAJ_CAT], [GEN_ART_NUMBER], ISNULL([CLR],'')
                    ) H
                      ON  L.[WERKS]                              = H.[WERKS]
                      AND L.[MAJ_CAT]                            = H.[MAJ_CAT]
                      AND TRY_CAST(L.[GEN_ART_NUMBER] AS BIGINT) = H.[GEN_ART_NUMBER]
                      AND ISNULL(L.[CLR],'')                     = H.[CLR]
                    WHERE  H.[HOLD_REM_OPT] > 0
                """, {"pool_alloc_type": req.alloc_type} if _ht_has_type else None)
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
        # FS-02: MSA_FNL_Q is the TYPED pool at gen grain — per MSA row
        #   FNL_Q_EFF = max(min(Σ SLOC-cols(alloc_type) − PEND_T − HOLD_T, FNL_Q), 0)
        # summed per option. PEND_T/HOLD_T are LIVE typed aggregates at
        # (RDC, GEN_ART_NUMBER, CLR); legacy NULL/'' ledger rows deduct from
        # both pools. FNL_Q (total) remains the safety cap.
        try:
            _run(conn, f"ALTER TABLE [{LISTING_TABLE}] ADD [MSA_FNL_Q] FLOAT NULL")
        except Exception:
            pass
        # Row-per-type MSA: MSA_FNL_Q reads the baked FNL_Q of the run's typed
        # MSA rows directly (WHERE ALLOC_TYPE = req.alloc_type). No column-sum
        # or live pend/hold math — the typed row already has the correct pool.
        _mtype_filter = ""
        _mtype_params = None
        if 'ALLOC_TYPE' in _get_columns(conn, req.msa_table):
            _mtype_filter = " AND ISNULL([ALLOC_TYPE],'FRESH') = :pool_alloc_type"
            _mtype_params = {"pool_alloc_type": req.alloc_type}
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
                            WHERE [MAJ_CAT] IS NOT NULL AND [GEN_ART_NUMBER] IS NOT NULL{msa_rdc_filter}{_mtype_filter}
                            GROUP BY {_msa_expr('MAJ_CAT')}, {_msa_expr('GEN_ART_NUMBER')}, {_msa_expr('CLR')}{_rdc_group}
                        ) M ON L.[MAJ_CAT] = M.[MAJ_CAT]
                            AND L.[GEN_ART_NUMBER] = M.[GEN_ART_NUMBER]
                            AND L.[CLR] = M.[CLR] {_rdc_join}
                    """, _mtype_params)
                    logger.info(
                        f"Part 3.55: MSA_FNL_Q = {req.alloc_type} typed rows from {req.msa_table}"
                    )
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
                has_var_art = "ARTICLE_NUMBER" in var_cols
                vrdc_select = f", LTRIM(RTRIM(CAST([RDC] AS NVARCHAR(50)))) AS MSA_RDC" if has_var_rdc else ""
                vrdc_group = f", LTRIM(RTRIM(CAST([RDC] AS NVARCHAR(50))))" if has_var_rdc else ""
                vrdc_join = "AND L.[RDC] = V.[MSA_RDC]" if has_var_rdc and req.rdc_mode == "own" else ""
                var_rdc_where = msa_rdc_filter.replace(f"[{msa_rdc_col}]", "[RDC]") if has_var_rdc else ""
                # Row-per-type MSA: VAR_FNL_COUNT counts sizes whose baked typed
                # FNL_Q is positive, filtered to the run's ALLOC_TYPE rows.
                _var_type_where = ""
                var_params = None
                if 'ALLOC_TYPE' in var_cols:
                    _var_type_where = " AND ISNULL([ALLOC_TYPE],'FRESH') = :pool_alloc_type"
                    var_params = {"pool_alloc_type": req.alloc_type}
                if has_fnl:
                    fnl_expr = (", SUM(CASE WHEN TRY_CAST([FNL_Q] AS FLOAT) > 0 "
                                "THEN 1 ELSE 0 END) AS fnl_cnt")
                else:
                    fnl_expr = ", 0 AS fnl_cnt"
                try:
                    _run(conn, f"""
                        UPDATE L SET L.[VAR_COUNT] = V.var_cnt, L.[VAR_FNL_COUNT] = V.fnl_cnt
                        FROM [{LISTING_TABLE}] L
                        INNER JOIN (
                            SELECT {_msa_col('MAJ_CAT')}, {_msa_col('GEN_ART_NUMBER')}, {_msa_col('CLR')}
                                   {vrdc_select},
                                   COUNT(*) AS var_cnt{fnl_expr}
                            FROM [ARS_MSA_VAR_ART]
                            WHERE [MAJ_CAT] IS NOT NULL AND [GEN_ART_NUMBER] IS NOT NULL{mc_where}{var_rdc_where}{_var_type_where}
                            GROUP BY {_msa_expr('MAJ_CAT')}, {_msa_expr('GEN_ART_NUMBER')}, {_msa_expr('CLR')}{vrdc_group}
                        ) V ON L.[MAJ_CAT] = V.[MAJ_CAT]
                            AND L.[GEN_ART_NUMBER] = V.[GEN_ART_NUMBER]
                            AND L.[CLR] = V.[CLR] {vrdc_join}
                    """, var_params)
                    logger.info(f"Part 3.55: VAR_COUNT + VAR_FNL_COUNT from ARS_MSA_VAR_ART"
                                f"{' (typed ' + req.alloc_type + ')' if _var_type_where else ''}")
                except Exception as e:
                    logger.warning(f"Part 3.55: VAR_COUNT/FNL_COUNT failed: {str(e)[:150]}")
        t0 = _time_step("Part 3.55 (MSA_FNL_Q + VAR_COUNT)", t0)

        # ── PART 3.6: Populate GEN_ART_DESC + tag OPT_TYPE (4-way classification) ──
        # Rules (applies to ALL rows — both IS_NEW=0 and IS_NEW=1):
        #   MIX: low stock + no MSA + no NL hold (nothing to send) OR final ELSE
        #   RL: adequate stock   TBC: low stock + MSA   TBL: zero stock + MSA
        #   (MIX(b) poor-color-fill rule removed 2026-07-30 — see _classify_opt_type)
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
        #   MIX (b): REMOVED 2026-07-30 — poor color fill no longer forces MIX here.
        #            size_threshold / min_size_count now only drive the downstream
        #            R07 size-coverage gate (skip poor-size TBL at alloc time).
        #   RL:  (adequate stock OR RL_HOLD_QTY > 0) AND MSA_FNL_Q > 0
        #        — RL requires fresh MSA supply to top up against; an open TBL
        #          hold alone is no longer enough to land in RL.
        #   TBC: low stock, MSA or NL hold available
        #   TBL: zero stock, MSA or NL hold available
        threshold = req.stock_threshold_pct         # STK vs ACS_D gate (MIX(a)/RL/TBC)
        size_threshold = req.size_threshold         # VAR-ratio gate (MIX(b))
        default_acs = float(req.default_acs_d or 18)
        def _classify_opt_type(label="OPT_TYPE"):
            _run(conn, f"""
                UPDATE [{LISTING_TABLE}]
                SET [OPT_TYPE] = CASE
                    -- Classification universe (g = threshold × ACS_D|default_acs).
                    -- MIX(b) poor-color-fill removed 2026-07-30. L added + TBL/RL
                    -- reworked 2026-07-30 so no (STK, MSA, HOLD) combo falls through
                    -- to the ELSE (which is now an unreachable safety net).
                    --
                    -- MIX (a): below-threshold stock, no MSA, no NL hold — nothing to send
                    WHEN ISNULL([STK_TTL], 0) < {threshold} * ISNULL(NULLIF([ACS_D], 0), {default_acs})
                     AND ISNULL([MSA_FNL_Q], 0)    = 0
                     AND ISNULL([RL_HOLD_QTY], 0)  = 0
                        THEN 'MIX'
                    -- L: adequate stock, no fresh MSA, no NL hold — store already covered,
                    --    nothing to replenish. Report-only: fails the ELIG stock-gate
                    --    (MSA=0 AND HOLD=0) so it never enters the alloc working table,
                    --    exactly like MIX. NOT processed by the RL/TBC/TBL waterfall.
                    WHEN ISNULL([STK_TTL], 0) >= {threshold} * ISNULL(NULLIF([ACS_D], 0), {default_acs})
                     AND ISNULL([MSA_FNL_Q], 0)   = 0
                     AND ISNULL([RL_HOLD_QTY], 0) = 0
                        THEN 'L'
                    -- RL: adequate stock with MSA or NL hold to work against, OR a
                    --     sold-out (STK<=0) option carrying only a prior-run NL hold
                    --     (no fresh MSA) — ships by releasing the held qty.
                    WHEN (ISNULL([STK_TTL], 0) >= {threshold} * ISNULL(NULLIF([ACS_D], 0), {default_acs})
                          AND (ISNULL([MSA_FNL_Q], 0) > 0 OR ISNULL([RL_HOLD_QTY], 0) > 0))
                      OR (ISNULL([STK_TTL], 0) <= 0
                          AND ISNULL([MSA_FNL_Q], 0)   = 0
                          AND ISNULL([RL_HOLD_QTY], 0) > 0)
                        THEN 'RL'
                    -- TBC: below-threshold (but positive) stock with MSA or NL hold
                    WHEN ISNULL([STK_TTL], 0) > 0
                     AND [STK_TTL] < {threshold} * ISNULL(NULLIF([ACS_D], 0), {default_acs})
                     AND (ISNULL([MSA_FNL_Q], 0) > 0 OR ISNULL([RL_HOLD_QTY], 0) > 0)
                        THEN 'TBC'
                    -- TBL: zero/negative stock with fresh MSA supply (hold-only sold-out
                    --      handled by the RL branch above)
                    WHEN ISNULL([STK_TTL], 0) <= 0
                     AND ISNULL([MSA_FNL_Q], 0) > 0
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
            l_count    = _sum("L")
            tagged_total = mixl_count + tbl_count + toc_count + rl_count + l_count
            untagged = total - tagged_total
            logger.info(
                f"Part 3.6: OPT_TYPE tagged — "
                f"MIX={mixl_count}, L={l_count}, TBL={tbl_count}, TBC={toc_count}, RL={rl_count}, "
                f"untagged={untagged} "
                f"[IS_NEW=1 breakdown: TBL={type_counts.get(('TBL',1),0)}, "
                f"MIX={type_counts.get(('MIX',1),0)}, "
                f"L={type_counts.get(('L',1),0)}, "
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

            # ── FS-05: hold-suppression source layer ────────────────────
            # suppress(row) = (skip_hold_upc AND ST_STATUS='UPC')
            #              OR (NOT apply_hold_seg_app AND SEG='APP')
            #              OR (NOT apply_hold_seg_gm  AND SEG='GM')
            # Suppressed rows never receive the TBL hold-days uplift:
            # OPT_MBQ_WH = OPT_MBQ, so the OPT_REQ_WH computed below mirrors
            # OPT_REQ automatically — the option is budgeted like RL/TBC
            # from the start (BR-10). NULL ST_STATUS/SEG never suppresses
            # (E-04). Runs between the OPT_MBQ_WH and OPT_REQ_WH updates by
            # design.
            _suppress_parts = []
            if req.skip_hold_upc:
                _suppress_parts.append(
                    "UPPER(LTRIM(RTRIM(CAST(ISNULL([ST_STATUS],'') "
                    "AS NVARCHAR(50))))) = 'UPC'")
            if not req.apply_hold_seg_app:
                _suppress_parts.append(
                    "UPPER(LTRIM(RTRIM(CAST(ISNULL([SEG],'') "
                    "AS NVARCHAR(50))))) = 'APP'")
            if not req.apply_hold_seg_gm:
                _suppress_parts.append(
                    "UPPER(LTRIM(RTRIM(CAST(ISNULL([SEG],'') "
                    "AS NVARCHAR(50))))) = 'GM'")
            if _suppress_parts:
                # SEG lookup at MAJ_CAT grain — vw_master_product first,
                # MSA source view as fallback. Deliberately NOT the MSA SEG
                # column (pre-filtered to APP/GM at MSA Step 4 and not
                # projected by Stage B) — FS-04(2).
                try:
                    _run(conn, f"ALTER TABLE [{LISTING_TABLE}] ADD [SEG] NVARCHAR(50) NULL")
                except Exception:
                    pass  # column may already exist
                _seg_src = next(
                    (t for t in ("vw_master_product", "VW_ET_MSA_STK_WITH_MASTER")
                     if _table_exists(conn, t)
                     and {"MAJ_CAT", "SEG"} <= {c.upper() for c in _get_columns(conn, t)}),
                    None,
                )
                if _seg_src:
                    _run(conn, f"""
                        UPDATE L SET L.[SEG] = S.[SEG]
                        FROM [{LISTING_TABLE}] L
                        INNER JOIN (
                            SELECT [MAJ_CAT],
                                   MAX(LTRIM(RTRIM(CAST([SEG] AS NVARCHAR(50))))) AS SEG
                            FROM [{_seg_src}] WITH (NOLOCK)
                            WHERE [MAJ_CAT] IS NOT NULL AND [SEG] IS NOT NULL
                            GROUP BY [MAJ_CAT]
                        ) S ON LTRIM(RTRIM(CAST(L.[MAJ_CAT] AS NVARCHAR(200))))
                             = LTRIM(RTRIM(CAST(S.[MAJ_CAT] AS NVARCHAR(200))))
                    """)
                    logger.info(f"Part 4c (FS-05): SEG merged from {_seg_src} (MAJ_CAT grain)")
                else:
                    logger.warning(
                        "Part 4c (FS-05): no MAJ_CAT→SEG source found "
                        "(vw_master_product / VW_ET_MSA_STK_WITH_MASTER) — "
                        "SEG toggles cannot suppress")
                _supp_res = conn.execute(text(f"""
                    UPDATE [{LISTING_TABLE}]
                    SET [OPT_MBQ_WH] = [OPT_MBQ]
                    WHERE ({' OR '.join(_suppress_parts)})
                """))
                conn.commit()
                # E-04: rows with NULL/blank attributes are never suppressed —
                # log the counts so a silent mismatch is visible per run.
                try:
                    _null_status = conn.execute(text(
                        f"SELECT COUNT(*) FROM [{LISTING_TABLE}] "
                        f"WHERE LTRIM(RTRIM(CAST(ISNULL([ST_STATUS],'') AS NVARCHAR(50)))) = ''"
                    )).scalar() or 0
                    _null_seg = conn.execute(text(
                        f"SELECT COUNT(*) FROM [{LISTING_TABLE}] "
                        f"WHERE LTRIM(RTRIM(CAST(ISNULL([SEG],'') AS NVARCHAR(50)))) = ''"
                    )).scalar() or 0
                    logger.info(
                        f"Part 4c (FS-05): hold suppressed on "
                        f"{_supp_res.rowcount} rows "
                        f"(skip_hold_upc={req.skip_hold_upc}, "
                        f"apply_hold_seg_app={req.apply_hold_seg_app}, "
                        f"apply_hold_seg_gm={req.apply_hold_seg_gm}); "
                        f"E-04 unsuppressable: ST_STATUS blank={_null_status}, "
                        f"SEG blank={_null_seg}"
                    )
                except Exception:
                    pass

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
        # Sec-grids (FAB, MACRO_MVGR, …):
        #   1. Aggregate ART_EXCESS by that grid's hierarchy keys.
        #   2. Deduct from {prefix}_STK_TTL (clamped to 0).
        #   3. REQ = MAX(0, {prefix}_MBQ - deducted_{prefix}_STK_TTL).
        # MJ only (conservative cap, since MJ_REQ is the binding budget downstream):
        #   1. Snapshot raw stock to MJ_STK_TTL_ORIG (used later by the growth-lift block).
        #   2. MJ_REQ_WITH_EXC = MAX(0, MJ_MBQ - raw_STK_TTL)        (excess treated as in-stock)
        #   3. MJ_REQ_NO_EXC   = MAX(0, MJ_MBQ - deducted_STK_TTL)   (excess stripped from stock)
        #   4. MJ_REQ = MAX(0, MIN(WITH_EXC, NO_EXC)).
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

            if prefix == "MJ":
                # Ensure audit columns exist (idempotent across rebuilds).
                for _col in ("MJ_STK_TTL_ORIG", "MJ_REQ_WITH_EXC", "MJ_REQ_NO_EXC"):
                    try:
                        _run(conn, f"ALTER TABLE [{LISTING_TABLE}] ADD [{_col}] FLOAT NULL")
                    except Exception:
                        pass

                # WITH_EXC: req using raw stock (excess is part of available stock).
                with_exc_expr = (
                    f"CASE WHEN ISNULL(L.[{mbq_col}], 0) - ISNULL(L.[{stk_col}], 0) > 0 "
                    f"THEN ROUND(ISNULL(L.[{mbq_col}], 0) - ISNULL(L.[{stk_col}], 0), 0) "
                    f"ELSE 0 END"
                )
                # NO_EXC: req using deducted stock (excess stripped out → higher req).
                no_exc_expr = (
                    f"CASE WHEN ISNULL(L.[{mbq_col}], 0) - ({deducted_stk}) > 0 "
                    f"THEN ROUND(ISNULL(L.[{mbq_col}], 0) - ({deducted_stk}), 0) "
                    f"ELSE 0 END"
                )
                # MIN of the two branches; outer MAX(0, …) is defensive — both
                # inputs are already ≥ 0, but it protects future formula edits.
                min_expr = (
                    f"CASE WHEN ({with_exc_expr}) < ({no_exc_expr}) "
                    f"THEN ({with_exc_expr}) ELSE ({no_exc_expr}) END"
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
                            L.[MJ_STK_TTL_ORIG] = ISNULL(L.[{stk_col}], 0),
                            L.[MJ_REQ_WITH_EXC] = {with_exc_expr},
                            L.[MJ_REQ_NO_EXC]   = {no_exc_expr},
                            L.[{stk_col}]       = {deducted_stk},
                            L.[{req_col}]       = CASE WHEN ({min_expr}) > 0
                                                       THEN ({min_expr})
                                                       ELSE 0 END
                        FROM [{LISTING_TABLE}] L
                        LEFT JOIN ExcessByGrid E
                          ON L.[WERKS] = E.[WERKS]
                         AND L.[MAJ_CAT] = E.[MAJ_CAT]{extra_join}
                    """)
                    grp_label = ",".join(["WERKS", "MAJ_CAT"] + group_cols)
                    req_log.append(f"{req_col}=MIN(WITH_EXC,NO_EXC) by {grp_label}")
                except Exception as e:
                    logger.warning(f"Part 4e: {req_col} (MJ MIN) failed: {str(e)[:150]}")
                continue

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

    # ── Manual store priority override ────────────────────────────────
    # MANUAL_ST_PRIORITY on the store master (Master_ALC_INPUT_ST_MASTER,
    # keyed by ST_CD = WERKS) lets ops pin a store's ST_RANK. A positive
    # integer P pins that store to ST_RANK = P inside every MAJ_CAT it is
    # listed in; NULL/0/blank/negative means "rank me by score as usual".
    # Non-manual stores keep their score order but shift into the smallest
    # rank numbers NOT occupied by a pinned store (literal + skip-used-slots).
    # Self-heal the column so it survives store-master re-uploads.
    ST_MASTER_TBL = "Master_ALC_INPUT_ST_MASTER"
    has_manual = False
    try:
        with de.connect() as mc:
            if _table_exists(mc, ST_MASTER_TBL):
                st_cols_up = {c.upper() for c in _get_columns(mc, ST_MASTER_TBL)}
                if "MANUAL_ST_PRIORITY" not in st_cols_up:
                    _run(mc, f"ALTER TABLE [{ST_MASTER_TBL}] ADD [MANUAL_ST_PRIORITY] INT NULL")
                    logger.info(f"Added MANUAL_ST_PRIORITY column to {ST_MASTER_TBL}")
                has_manual = True
    except Exception as e:
        logger.warning(f"MANUAL_ST_PRIORITY ensure on {ST_MASTER_TBL} failed: {e}")

    if has_manual:
        manual_join = f"LEFT JOIN [{ST_MASTER_TBL}] SM WITH (NOLOCK) ON SM.[ST_CD] = L.[WERKS]"
        # Only positive integers count as a manual pin; anything else → NULL (auto).
        manual_expr = ("CASE WHEN MAX(ISNULL(SM.[MANUAL_ST_PRIORITY],0)) > 0 "
                       "THEN MAX(ISNULL(SM.[MANUAL_ST_PRIORITY],0)) ELSE NULL END")
    else:
        manual_join = ""
        manual_expr = "CAST(NULL AS INT)"

    try:
        with de.connect() as rc:
            _run(rc, f"IF OBJECT_ID('{RANK_TABLE}','U') IS NOT NULL DROP TABLE [{RANK_TABLE}]")
            _run(rc, f"""
                ;WITH StoreAgg AS (
                    SELECT
                        L.[MAJ_CAT], L.[WERKS], MAX(L.[RDC]) AS RDC,
                        MAX(ISNULL(L.[MJ_REQ], 0))      AS MJ_REQ,
                        MAX(ISNULL(L.[MJ_MBQ], 0))      AS MJ_MBQ,
                        MAX(ISNULL(L.[MJ_STK_TTL], 0))  AS MJ_STK,
                        MAX(ISNULL(L.[ACS_D], 0))       AS ACS_D,
                        CASE WHEN MAX(ISNULL(L.[MJ_MBQ],0)) = 0 THEN 0
                             ELSE ROUND(MAX(ISNULL(L.[MJ_STK_TTL],0)) / NULLIF(MAX(L.[MJ_MBQ]),0), 4)
                        END AS FILL_RATE,
                        {manual_expr} AS MANUAL_PRI
                    FROM [{LISTING_TABLE}] L
                    {manual_join}
                    WHERE ISNULL(L.[OPT_TYPE],'') NOT IN ('MIX','L')
                    GROUP BY L.[MAJ_CAT], L.[WERKS]
                ),
                Ranked AS (
                    SELECT *,
                        DENSE_RANK() OVER (PARTITION BY MAJ_CAT ORDER BY MJ_REQ ASC)     AS REQ_RANK,
                        DENSE_RANK() OVER (PARTITION BY MAJ_CAT ORDER BY FILL_RATE DESC)  AS FILL_RANK
                    FROM StoreAgg
                ),
                Scored AS (
                    SELECT *, ROUND(REQ_RANK * {rw} + FILL_RANK * {fw}, 2) AS W_SCORE
                    FROM Ranked
                ),
                Tagged AS (
                    -- Winning manual row per (MAJ_CAT, MANUAL_PRI): best W_SCORE keeps
                    -- the pinned slot. Duplicate manual values and non-manual rows get
                    -- IS_MANUAL=0 and are placed by score into the free slots below.
                    SELECT *,
                        CASE WHEN MANUAL_PRI IS NOT NULL
                                  AND ROW_NUMBER() OVER (PARTITION BY MAJ_CAT, MANUAL_PRI
                                                         ORDER BY W_SCORE DESC, WERKS ASC) = 1
                             THEN 1 ELSE 0 END AS IS_MANUAL,
                        -- Score-only rank (ignores manual pins). Audit column so ops
                        -- can compare the final ST_RANK against what pure W_SCORE gives.
                        ROW_NUMBER() OVER (PARTITION BY MAJ_CAT
                            ORDER BY W_SCORE DESC, WERKS ASC) AS AUTO_ST_RANK
                    FROM Scored
                ),
                AutoRows AS (
                    SELECT *,
                        ROW_NUMBER() OVER (PARTITION BY MAJ_CAT ORDER BY W_SCORE DESC, WERKS ASC) AS auto_seq
                    FROM Tagged
                    WHERE IS_MANUAL = 0
                ),
                Counts AS (
                    SELECT MAJ_CAT, COUNT(*) AS n_rows FROM Tagged GROUP BY MAJ_CAT
                ),
                Nums AS (
                    SELECT TOP (5000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS n
                    FROM sys.all_objects a CROSS JOIN sys.all_objects b
                ),
                FreeSlots AS (
                    -- Rank numbers 1..n_rows not taken by a pinned store, numbered
                    -- ascending. The k-th free slot receives the k-th best non-manual
                    -- store. n_rows is always enough: pins within 1..n_rows are exactly
                    -- the numbers removed here; any pin > n_rows just leaves extra slots.
                    SELECT c.MAJ_CAT, nums.n,
                           ROW_NUMBER() OVER (PARTITION BY c.MAJ_CAT ORDER BY nums.n) AS free_seq
                    FROM Counts c
                    JOIN Nums nums ON nums.n <= c.n_rows
                    WHERE NOT EXISTS (
                        SELECT 1 FROM Tagged t
                        WHERE t.MAJ_CAT = c.MAJ_CAT AND t.IS_MANUAL = 1 AND t.MANUAL_PRI = nums.n
                    )
                )
                SELECT MAJ_CAT, WERKS, RDC, MJ_REQ, MJ_MBQ, MJ_STK, ACS_D, FILL_RATE,
                       REQ_RANK, FILL_RANK, W_SCORE, MANUAL_PRI, ST_RANK, MANUAL,
                       AUTO_ST_RANK, RANK_DELTA
                INTO [{RANK_TABLE}]
                FROM (
                    SELECT MAJ_CAT, WERKS, RDC, MJ_REQ, MJ_MBQ, MJ_STK, ACS_D, FILL_RATE,
                           REQ_RANK, FILL_RANK, W_SCORE, MANUAL_PRI,
                           MANUAL_PRI AS ST_RANK, CAST(1 AS BIT) AS MANUAL,
                           AUTO_ST_RANK, (AUTO_ST_RANK - MANUAL_PRI) AS RANK_DELTA
                    FROM Tagged WHERE IS_MANUAL = 1
                    UNION ALL
                    SELECT a.MAJ_CAT, a.WERKS, a.RDC, a.MJ_REQ, a.MJ_MBQ, a.MJ_STK, a.ACS_D, a.FILL_RATE,
                           a.REQ_RANK, a.FILL_RANK, a.W_SCORE, a.MANUAL_PRI,
                           fs.n AS ST_RANK, CAST(0 AS BIT) AS MANUAL,
                           a.AUTO_ST_RANK, (a.AUTO_ST_RANK - fs.n) AS RANK_DELTA
                    FROM AutoRows a
                    JOIN FreeSlots fs ON fs.MAJ_CAT = a.MAJ_CAT AND fs.free_seq = a.auto_seq
                ) X
            """)
            rank_rows = rc.execute(text(f"SELECT COUNT(*) FROM [{RANK_TABLE}]")).scalar()
            manual_cnt = rc.execute(text(f"SELECT COUNT(*) FROM [{RANK_TABLE}] WHERE [MANUAL] = 1")).scalar()
            logger.info(f"{RANK_TABLE}: {rank_rows} rows (req_wt={rw}, fill_wt={fw}); "
                        f"manual priority pinned {manual_cnt} store-MAJ_CAT slot(s)")
            # Validation: two stores set to the SAME priority P inside one MAJ_CAT.
            # Both are kept (best W_SCORE holds slot P, the other shifts to the next
            # free slot); surface a warning so the master data can be corrected.
            if has_manual:
                dups = rc.execute(text(f"""
                    SELECT MAJ_CAT, MANUAL_PRI, COUNT(*) AS c
                    FROM [{RANK_TABLE}]
                    WHERE MANUAL_PRI IS NOT NULL
                    GROUP BY MAJ_CAT, MANUAL_PRI
                    HAVING COUNT(*) > 1
                """)).fetchall()
                for mc_name, pri, c in dups[:50]:
                    logger.warning(
                        f"{RANK_TABLE}: MANUAL_ST_PRIORITY={pri} set on {c} stores in "
                        f"MAJ_CAT={mc_name}; kept best by W_SCORE at slot {pri}, "
                        f"other(s) shifted to next free slot"
                    )

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

    # ── PART 6.6: Materialize eligibility into ELIG_FLAG / ELIG_REASON ─
    # Codifies the legacy Part 7 WHERE clause (listing flag, MSA stock,
    # store demand, display capacity, TBL size coverage) into two columns
    # on ARS_LISTING.  Part 7's filter then collapses to a single column,
    # and the row count for each rejection reason is trivially queryable:
    #     SELECT ELIG_REASON, COUNT(*) FROM ARS_LISTING GROUP BY ELIG_REASON
    #
    # ELIG_FLAG is the conjunctive AND of every applicable gate.  Reason
    # priority below (NOT_LISTED → NO_STOCK → NO_DEMAND → NO_DISPLAY →
    # TBL_SIZE_LT_60) only governs which label a failing row reports; it
    # does not change which rows are eligible.
    try:
        with de.connect() as ec:
            elig_cols_upper = {c.upper() for c in _get_columns(ec, LISTING_TABLE)}
            if "ELIG_FLAG" not in elig_cols_upper:
                ec.execute(text(f"ALTER TABLE [{LISTING_TABLE}] ADD [ELIG_FLAG] INT NOT NULL DEFAULT 0"))
                ec.commit()
            if "ELIG_REASON" not in elig_cols_upper:
                ec.execute(text(f"ALTER TABLE [{LISTING_TABLE}] ADD [ELIG_REASON] NVARCHAR(50) NULL"))
                ec.commit()
            elig_cols_upper = {c.upper() for c in _get_columns(ec, LISTING_TABLE)}

            _has_listing = "LISTING"      in elig_cols_upper
            _has_msa     = "MSA_FNL_Q"    in elig_cols_upper
            _has_hold    = "RL_HOLD_QTY"  in elig_cols_upper
            _has_req     = "OPT_REQ_WH"   in elig_cols_upper
            _has_disp    = "MJ_DISP_Q"    in elig_cols_upper
            _has_opttype = "OPT_TYPE"     in elig_cols_upper
            _has_var     = "VAR_COUNT" in elig_cols_upper and "VAR_FNL_COUNT" in elig_cols_upper

            # "1=1" means "gate not applicable" — never fails, never names a reason.
            gate_listed  = "ISNULL(TRY_CAST([LISTING] AS INT), 1) = 1" if _has_listing else "1=1"
            if _has_msa and _has_hold:
                gate_stock = "(ISNULL([MSA_FNL_Q], 0) > 0 OR ISNULL([RL_HOLD_QTY], 0) > 0)"
            elif _has_msa:
                gate_stock = "ISNULL([MSA_FNL_Q], 0) > 0"
            else:
                gate_stock = "1=1"
            gate_demand  = "ISNULL([OPT_REQ_WH], 0) >= 1" if _has_req else "1=1"
            gate_display = "ISNULL(TRY_CAST([MJ_DISP_Q] AS FLOAT), 0) > 0" if _has_disp else "1=1"

            _t = float(req.stock_threshold_pct or 0.6)
            _min_sz = int(req.min_size_count or 0)
            if _has_var and _has_opttype:
                _min_sz_rescue = f" OR ISNULL([VAR_FNL_COUNT], 0) >= {_min_sz}" if _min_sz > 0 else ""
                gate_size = (
                    f"(ISNULL([OPT_TYPE], '') != 'TBL' "
                    f"OR ISNULL([VAR_COUNT], 0) = 0 "
                    f"OR CAST(ISNULL([VAR_FNL_COUNT], 0) AS FLOAT) / NULLIF([VAR_COUNT], 0) >= {_t}"
                    f"{_min_sz_rescue})"
                )
            else:
                gate_size = "1=1"

            _run(ec, f"""
                UPDATE [{LISTING_TABLE}] SET
                  [ELIG_REASON] = CASE
                    WHEN NOT ({gate_listed})  THEN 'NOT_LISTED'
                    WHEN NOT ({gate_stock})   THEN 'NO_STOCK'
                    WHEN NOT ({gate_demand})  THEN 'NO_DEMAND'
                    WHEN NOT ({gate_display}) THEN 'NO_DISPLAY'
                    WHEN NOT ({gate_size})    THEN 'TBL_SIZE_LT_60'
                    ELSE 'OK'
                  END,
                  [ELIG_FLAG] = CASE
                    WHEN ({gate_listed}) AND ({gate_stock}) AND ({gate_demand})
                     AND ({gate_display}) AND ({gate_size})
                    THEN 1 ELSE 0
                  END
            """)

            try:
                _run(ec, f"CREATE NONCLUSTERED INDEX IX_{LISTING_TABLE}_ELIG ON [{LISTING_TABLE}]([ELIG_FLAG])")
            except Exception:
                pass

            _diag_rows = ec.execute(text(
                f"SELECT [ELIG_REASON], COUNT(*) FROM [{LISTING_TABLE}] GROUP BY [ELIG_REASON]"
            )).fetchall()
            logger.info(
                f"Part 6.6 eligibility breakdown: "
                f"{ {r[0]: r[1] for r in _diag_rows} }"
            )
    except Exception as e:
        logger.warning(f"Part 6.6 (eligibility) failed: {e}")
    t0 = _time_step("Part 6.6 (Eligibility flag)", t0)

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

                # All eligibility gates (stock / demand / display / listing /
                # TBL size coverage) are materialized into ELIG_FLAG by Part 6.6
                # above.  When the toggle is OFF we filter here; when ON the
                # working table mirrors ARS_LISTING (audit mode) and downstream
                # readers anchor on ELIG_FLAG = 1 themselves.
                # Audit mode — business rule LST_AUDIT_ALL_TO_WORKING is the
                # source of truth (2026-07-31, migrated from Settings →
                # Application). Legacy app_settings.json value only serves as
                # the fallback default when the rules table is unreachable.
                _legacy_shift = False
                try:
                    from app.api.v1.endpoints.settings import load_app_settings as _load_app_settings
                    _app_cfg = _load_app_settings()
                    _legacy_shift = bool(
                        (_app_cfg.get("application") or {}).get("shift_all_to_working", False)
                    )
                except Exception as _cfg_err:
                    logger.warning(f"shift_all_to_working lookup failed, defaulting OFF: {_cfg_err}")
                shift_all_to_working = rule_flag('LST_AUDIT_ALL_TO_WORKING', _legacy_shift)
                if shift_all_to_working or "ELIG_FLAG" not in all_upper:
                    where_sql = ""
                else:
                    where_sql = "WHERE [ELIG_FLAG] = 1"

                _run(wc, f"""
                    SELECT {col_list}
                    INTO [{FINAL_TABLE}]
                    FROM [{LISTING_TABLE}]
                    {where_sql}
                """)
                working_rows = wc.execute(text(f"SELECT COUNT(*) FROM [{FINAL_TABLE}]")).scalar()
                logger.info(
                    f"{FINAL_TABLE}: {working_rows} rows "
                    f"(shift_all_to_working={shift_all_to_working}, where='{where_sql}')"
                )
                # Index on ELIG_FLAG so downstream `WHERE ELIG_FLAG = 1` reads
                # stay cheap when audit mode triples the row count.
                try:
                    _run(wc, f"CREATE NONCLUSTERED INDEX IX_{FINAL_TABLE}_ELIG ON [{FINAL_TABLE}]([ELIG_FLAG])")
                except Exception:
                    pass

                # ── Allocation Gate: MBQ growth headroom (MJ + all sec-grids) ─
                # Lift the per-(WERKS, MAJ_CAT[, extras]) target by scaling
                # each grid's MBQ into a sibling {prefix}_MBQ_REV column.  The
                # ORIGINAL {prefix}_MBQ value is snapshotted to {prefix}_MBQ_ORIG
                # (first run wins) and the growth multiplier is ALWAYS applied
                # against ORIG — so re-runs are idempotent and never compound.
                # When growth ≠ 100, the lifted REV value is promoted into the
                # live {prefix}_MBQ and {prefix}_REQ columns so every engine
                # consumer (waterfall, sec-cap gate, MJ_REQ gate, store-broken
                # pre-band, post-waterfall recompute) sees the new ceiling
                # with zero code changes.  ORIG columns remain queryable for
                # audit and for the MBQ-cap formulas that anchor to original
                # (rl/tbc/tbl_mbq_cap_pct × MJ_MBQ_ORIG, Decision 4-B).
                #
                # Scope: MJ (always) + every active Grid Builder grid where
                # pivot_only = 0.  Pivot-only grids skip lookups & MBQ math,
                # so they have no _MBQ column to lift.
                growth_pct = float(req.mj_req_growth_pct or 100.0)

                # Discover the set of grid prefixes to lift. Always include MJ.
                grid_prefixes: List[str] = ["MJ"]
                try:
                    grid_rows_lift = wc.execute(text("""
                        SELECT grid_name, ISNULL(pivot_only, 0) AS po
                        FROM [ARS_GRID_BUILDER]
                        WHERE UPPER(status) = 'ACTIVE'
                    """)).fetchall()
                    for gn, po in grid_rows_lift:
                        if int(po or 0) == 1:
                            continue  # skip pivot-only grids
                        p = str(gn).upper()
                        if p.startswith("MJ_"):
                            p = p[3:]
                        if p and p not in grid_prefixes:
                            grid_prefixes.append(p)
                except Exception as _gx:
                    logger.warning(f"grid prefix discovery for MBQ lift failed: {_gx}")

                final_cols_upper = {c.upper() for c in _get_columns(wc, FINAL_TABLE)}
                lifted_log: List[str] = []
                for prefix in grid_prefixes:
                    mbq_col      = f"{prefix}_MBQ"
                    mbq_rev_col  = f"{prefix}_MBQ_REV"
                    mbq_orig_col = f"{prefix}_MBQ_ORIG"
                    req_col      = f"{prefix}_REQ"
                    req_rev_col  = f"{prefix}_REQ_REV"
                    req_orig_col = f"{prefix}_REQ_ORIG"
                    stk_col      = f"{prefix}_STK_TTL"

                    if mbq_col.upper() not in final_cols_upper:
                        continue  # grid's MBQ column not materialised on FINAL_TABLE

                    has_req = req_col.upper() in final_cols_upper
                    has_stk = stk_col.upper() in final_cols_upper

                    # Always create the audit/revised columns (idempotent).
                    for _rev_col in (mbq_orig_col, mbq_rev_col,
                                     req_orig_col if has_req else None,
                                     req_rev_col  if has_req else None):
                        if not _rev_col:
                            continue
                        try:
                            _run(wc, f"ALTER TABLE [{FINAL_TABLE}] ADD [{_rev_col}] FLOAT NULL")
                        except Exception:
                            pass  # idempotent — column may already exist

                    # Snapshot original MBQ (one-time per session).
                    _run(wc, f"""
                        UPDATE [{FINAL_TABLE}]
                        SET [{mbq_orig_col}] = [{mbq_col}]
                        WHERE [{mbq_orig_col}] IS NULL
                    """)
                    if has_req:
                        _run(wc, f"""
                            UPDATE [{FINAL_TABLE}]
                            SET [{req_orig_col}] = [{req_col}]
                            WHERE [{req_orig_col}] IS NULL
                        """)

                    # Lift: REV = ORIG × growth_pct/100 (always reads ORIG, never REV → idempotent).
                    _run(wc, f"""
                        UPDATE [{FINAL_TABLE}]
                        SET [{mbq_rev_col}] = ROUND(ISNULL([{mbq_orig_col}], 0) * {growth_pct} / 100.0, 0)
                    """)
                    if has_req and has_stk:
                        # MJ uses the same MIN(WITH_EXC, NO_EXC) logic as Part 4e,
                        # but against the revised MBQ. WITH_EXC needs raw stock,
                        # which Part 4e snapshotted to MJ_STK_TTL_ORIG; STK_TTL
                        # carries deducted stock. MJ_REQ_WITH_EXC / MJ_REQ_NO_EXC
                        # are refreshed alongside so the audit columns reflect
                        # the post-growth view that drives MJ_REQ.
                        if prefix == "MJ" and "MJ_STK_TTL_ORIG" in final_cols_upper:
                            with_exc_branch = (
                                f"CASE WHEN ISNULL([{mbq_rev_col}], 0) - ISNULL([MJ_STK_TTL_ORIG], 0) > 0 "
                                f"THEN ROUND(ISNULL([{mbq_rev_col}], 0) - ISNULL([MJ_STK_TTL_ORIG], 0), 0) "
                                f"ELSE 0 END"
                            )
                            no_exc_branch = (
                                f"CASE WHEN ISNULL([{mbq_rev_col}], 0) - ISNULL([{stk_col}], 0) > 0 "
                                f"THEN ROUND(ISNULL([{mbq_rev_col}], 0) - ISNULL([{stk_col}], 0), 0) "
                                f"ELSE 0 END"
                            )
                            min_branch = (
                                f"CASE WHEN ({with_exc_branch}) < ({no_exc_branch}) "
                                f"THEN ({with_exc_branch}) ELSE ({no_exc_branch}) END"
                            )
                            _run(wc, f"""
                                UPDATE [{FINAL_TABLE}]
                                SET [MJ_REQ_WITH_EXC] = {with_exc_branch},
                                    [MJ_REQ_NO_EXC]   = {no_exc_branch},
                                    [{req_rev_col}]   = CASE WHEN ({min_branch}) > 0
                                                             THEN ({min_branch})
                                                             ELSE 0 END
                            """)
                        else:
                            _run(wc, f"""
                                UPDATE [{FINAL_TABLE}]
                                SET [{req_rev_col}] = CASE
                                    WHEN ISNULL([{mbq_rev_col}], 0) - ISNULL([{stk_col}], 0) > 0
                                    THEN ROUND(ISNULL([{mbq_rev_col}], 0) - ISNULL([{stk_col}], 0), 0)
                                    ELSE 0 END
                            """)

                    # Promote: lifted MBQ → live column, only when growth ≠ 100.
                    if growth_pct != 100.0:
                        _run(wc, f"UPDATE [{FINAL_TABLE}] SET [{mbq_col}] = [{mbq_rev_col}]")
                        if has_req:
                            _run(wc, f"UPDATE [{FINAL_TABLE}] SET [{req_col}] = [{req_rev_col}]")
                        lifted_log.append(prefix)

                if growth_pct != 100.0:
                    logger.info(
                        f"{FINAL_TABLE}: MBQ × {growth_pct}% (lifted from ORIG) — "
                        f"prefixes: {lifted_log or ['(none)']}; "
                        f"_MBQ_REV / _REQ_REV materialised; "
                        f"_MBQ_ORIG / _REQ_ORIG preserved for caps & audit"
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
                # Info-only master-data cols sitting inside ARS_GRID_HIERARCHY
                # (Y/N labels, not 1/0 grid flags). Skipped by the GH_/H_
                # generators; copied verbatim into the working table so
                # downstream consumers can read the label without joining
                # the hierarchy table.
                _HIER_INFO_ONLY_COLS = {"SZ_APPLICABLE"}
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
                        if hc.upper() in _HIER_INFO_ONLY_COLS:
                            continue  # info label (Y/N), not a grid flag
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
                        if hc.upper() in _HIER_INFO_ONLY_COLS:
                            continue  # info label (Y/N), not a grid flag
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

                    # ── Copy info-only hierarchy cols (SZ_APPLICABLE etc.) ─
                    # Not flags — just copy the Y/N value verbatim into the
                    # working table for downstream consumers. NULL when the
                    # MAJ_CAT is missing from the hierarchy table.
                    hier_cols_upper = {c.upper() for c in hier_cols}
                    for info_col in _HIER_INFO_ONLY_COLS:
                        if info_col not in hier_cols_upper:
                            continue  # column not yet present in hierarchy
                        if info_col not in work_cols_upper:
                            try: _run(wc, f"ALTER TABLE [{FINAL_TABLE}] ADD [{info_col}] NVARCHAR(10) NULL")
                            except Exception: pass
                        set_parts.append(f"W.[{info_col}] = H.[{info_col}]")
                        add_cols.append(info_col)

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

    # ── Part 8 — rule engine (list OPTs + allocate VAR_ART × SZ) ──
    # Spec: docs/NEW_RULE_ENGINE_SPEC.md
    # per_opt is the ONLY engine (2026-07-10 removal — see
    # docs/REMOVAL_PLAN_PER_OPT_ONLY.md). Dispatch goes through
    # rule_engine_pandas.run_listing_and_allocation_pandas, which owns the
    # orchestration (worker pool, writer queue, table loads, sec-cap specs,
    # Stage D pak rounding, write-back) and is hard-pinned to the per_opt
    # band (rule_engine_per_opt._run_band_per_opt). No env-var switch —
    # /generate already 400s on any allocation_mode other than 'per_opt'.
    alloc_rows = 0
    alloc_batch_id = None
    alloc_failed_count = 0
    mode = "per_opt"
    n_workers = max(2, min(8, int(req.parallel_workers or 4)))
    _growth = req.mj_req_growth_pct
    try:
        from app.services.rule_engine_pandas import (
            run_listing_and_allocation_pandas,
        )
        alloc_result = run_listing_and_allocation_pandas(
            working_table=FINAL_TABLE,
            listed_table="ARS_LISTED_OPT",
            alloc_table=ALLOC_TABLE,
            n_workers=n_workers,
            batch_id=preset_batch_id,
            size_threshold=req.size_threshold,
            min_size_count=req.min_size_count,
            pri_ct_check_rl=req.pri_ct_check_rl,
            pri_ct_check_tbc=req.pri_ct_check_tbc,
            rl_mbq_cap_pct=req.rl_mbq_cap_pct,
            tbc_mbq_cap_pct=req.tbc_mbq_cap_pct,
            tbl_mbq_cap_pct=req.tbl_mbq_cap_pct,
            rl_mj_req_cap_pct=req.rl_mj_req_cap_pct,
            tbc_mj_req_cap_pct=req.tbc_mj_req_cap_pct,
            tbl_mj_req_cap_pct=req.tbl_mj_req_cap_pct,
            mj_req_growth_pct=_growth,
            opt_types=req.opt_types or ["RL", "TBC", "TBL"],
            use_writer_queue=req.use_writer_queue,
            apply_sec_cap_in_normal=req.apply_sec_cap_in_normal,
            rl_dispatch_mode=req.rl_dispatch_mode,
            tbc_dispatch_mode=req.tbc_dispatch_mode,
            cont_fallback_mode=req.cont_fallback_mode,
            # Fresh/GRT typed pool + hold suppression (FS-02/03/05/06)
            alloc_type=req.alloc_type,
            skip_hold_upc=req.skip_hold_upc,
            apply_hold_seg_app=req.apply_hold_seg_app,
            apply_hold_seg_gm=req.apply_hold_seg_gm,
            # Post-TBL hold release + retry (Option B) — cover threshold
            # mirrors the Part 3.6/8.5 classification inputs. Master switch =
            # business rule ALC_TBL_HOLD_RETRY (inactive → freed pcs wait for
            # the next run; Part 8.55 still releases post-run).
            stock_threshold_pct=req.stock_threshold_pct,
            default_acs_d=float(req.default_acs_d or 18.0),
            tbl_hold_release_retry=rule_flag('ALC_TBL_HOLD_RETRY', True),
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

    # ── Part 8.35 — FS-07: stamp ALLOC_TYPE on ARS_ALLOC_WORKING ──────
    # Stage B re-creates the alloc table via SELECT…INTO every run, so the
    # column must be (re-)added when the engine path didn't emit it (this
    # is the catch-all for the per_opt dispatch). Runs BEFORE Part 8.4 so
    # the parked snapshot — and the later promotion to ARS_ALLOC_HISTORY —
    # carries the run's pool type.
    try:
        with de.connect() as ac:
            if _table_exists(ac, ALLOC_TABLE):
                _run(ac, f"""
                    IF NOT EXISTS (
                        SELECT 1 FROM sys.columns
                        WHERE object_id = OBJECT_ID('{ALLOC_TABLE}')
                          AND name = 'ALLOC_TYPE'
                    )
                    ALTER TABLE [{ALLOC_TABLE}] ADD [ALLOC_TYPE] NVARCHAR(10) NULL
                """)
                _run(ac, f"UPDATE [{ALLOC_TABLE}] SET [ALLOC_TYPE] = :t",
                     {"t": req.alloc_type})
                logger.info(
                    f"Part 8.35: ALLOC_TYPE='{req.alloc_type}' stamped on {ALLOC_TABLE}"
                )
    except Exception as e:
        logger.warning(f"Part 8.35: ALLOC_TYPE stamp failed: {e}")
    t0 = _time_step("Part 8.35 (ALLOC_TYPE stamp)", t0)

    # ── Part 8.36 — stamp run dates on ARS_ALLOC_WORKING (2026-07-18) ──
    # Information-only columns: STOCK_CONSIDER_DT (mandatory, the date the
    # stock figures reflect) and PICKING_DT (optional, planned pick/dispatch
    # date). Same pattern as the ALLOC_TYPE stamp above and runs BEFORE Part
    # 8.4 parking, so the column-introspection copy in parked_history.py
    # carries both columns and their values into ARS_ALLOC_PARKED and then
    # ARS_ALLOC_HISTORY with no changes to the parking code. These dates do
    # NOT enter any allocation/stock math — pure traceability.
    try:
        with de.connect() as ac:
            if _table_exists(ac, ALLOC_TABLE):
                _run(ac, f"""
                    IF NOT EXISTS (
                        SELECT 1 FROM sys.columns
                        WHERE object_id = OBJECT_ID('{ALLOC_TABLE}')
                          AND name = 'STOCK_CONSIDER_DT'
                    )
                    ALTER TABLE [{ALLOC_TABLE}] ADD [STOCK_CONSIDER_DT] DATE NULL
                """)
                _run(ac, f"""
                    IF NOT EXISTS (
                        SELECT 1 FROM sys.columns
                        WHERE object_id = OBJECT_ID('{ALLOC_TABLE}')
                          AND name = 'PICKING_DT'
                    )
                    ALTER TABLE [{ALLOC_TABLE}] ADD [PICKING_DT] DATE NULL
                """)
                _run(ac, f"""
                    UPDATE [{ALLOC_TABLE}]
                    SET [STOCK_CONSIDER_DT] = :s, [PICKING_DT] = :p
                """, {"s": req.stock_consider_dt, "p": req.picking_dt})
                logger.info(
                    f"Part 8.36: run dates stamped on {ALLOC_TABLE} "
                    f"(stock={req.stock_consider_dt}, picking={req.picking_dt})"
                )
    except Exception as e:
        logger.warning(f"Part 8.36: run-date stamp failed: {e}")
    t0 = _time_step("Part 8.36 (run-date stamp)", t0)

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
        elif snap.get("empty_targets"):
            # At least one EXPECTED target parked 0 rows (its source table was
            # empty at park time — e.g. ARS_MSA_TOTAL emptied by a concurrent
            # MSA rebuild). The run's alloc/listing are safely parked, but the
            # empty target will NOT reach *_HISTORY on Approve. Flag it so the
            # UI warns instead of showing a clean PARKED. Observed on
            # 20260713_164356_383 (MSA_TOTAL missing from history).
            parked_status = "PARKED_PARTIAL"
            summary["parked_empty_targets"] = snap.get("empty_targets")
        logger.info(
            f"[generate] parked snapshot: total_rows={snap.get('total_parked_rows')} "
            f"by_table={snap.get('by_table')} parked_status={parked_status} "
            f"empty_targets={snap.get('empty_targets')}"
        )
    except Exception as e:
        logger.warning(f"[generate] parked snapshot failed: {e}")
        parked_status = "SKIPPED_ERROR"
    summary["parked_status"] = parked_status
    t0 = _time_step("Part 8.4 (park alloc + listing snapshots)", t0)

    # ── Part 8.5 — OPT_STATUS post-alloc classification + TBL_LISTED_DATE ──
    # Reworked 2026-07-30 (user-specified). Evaluated on post-alloc stock
    # (post = STK_TTL + ALLOC_QTY) against the same threshold as OPT_TYPE
    # (g = thr × eff_ACS_D). First match wins:
    #   1. OPT_TYPE L    → L      (report-only passthrough)
    #   2. OPT_TYPE MIX  → MIX    (passthrough)
    #   3. RL  alloc=0   → WRL    (not allocated this run — Waiting RL)
    #   4. TBL alloc=0   → UNQ    (not allocated this run — UNQualified;
    #                              symmetric with RL→WRL. Renamed from
    #                              'TBL' 2026-07-31 so the post-alloc status
    #                              never collides with the OPT_TYPE value)
    #   5. post < g      → MIX    (gate for rows that DID ship but remain
    #                              under the display threshold; also catches
    #                              TBC that got 0)
    #   6. TBL           → NL     (shipped and covered — newly listed)
    #   7. TBC           → RL     (shipped to full cover — graduated live)
    #   8. RL            → RL     (refilled)
    #   9. anything else → covered leftovers: alloc>0 → RL, else → L
    # Every row gets a real status — there is NO 'NA' in the vocabulary.
    # TBL_LISTED_DATE = GETDATE() when OPT_TYPE='TBL' AND ALLOC_QTY>0.
    # Business rule LST_OPT_STATUS_STAMP: inactive → skip Part 8.5 entirely
    # (and with it 8.55, which keys on OPT_STATUS='MIX').
    _opt_status_enabled = rule_flag('LST_OPT_STATUS_STAMP', True)
    if not _opt_status_enabled:
        logger.info("Part 8.5 skipped — business rule LST_OPT_STATUS_STAMP inactive")
    try:
        if not _opt_status_enabled:
            raise StopIteration("skip")  # caught below; clean rule-driven skip
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
                UPDATE [{FINAL_TABLE}] WITH (ROWLOCK, UPDLOCK) SET
                  [OPT_STATUS] = CASE
                    -- 1-2. L / MIX pass through unchanged (never allocated)
                    WHEN [OPT_TYPE] = 'L'   THEN 'L'
                    WHEN [OPT_TYPE] = 'MIX' THEN 'MIX'
                    -- 3-4. not allocated this run → per-type waiting status
                    WHEN [OPT_TYPE] = 'RL'  AND ISNULL([ALLOC_QTY],0) = 0
                        THEN 'WRL'
                    WHEN [OPT_TYPE] = 'TBL' AND ISNULL([ALLOC_QTY],0) = 0
                        THEN 'UNQ'
                    -- 5. shipped but still under the display threshold → MIX
                    --    (also catches TBC that got 0)
                    WHEN ISNULL([STK_TTL],0) + ISNULL([ALLOC_QTY],0)
                         < {thr} * ISNULL(NULLIF([ACS_D],0), {default_acs})
                        THEN 'MIX'
                    -- 6. TBL shipped and covered → newly listed
                    WHEN [OPT_TYPE] = 'TBL' THEN 'NL'
                    -- 7. TBC shipped to full cover → live
                    WHEN [OPT_TYPE] = 'TBC' THEN 'RL'
                    -- 8. RL refilled
                    WHEN [OPT_TYPE] = 'RL' THEN 'RL'
                    -- 9. covered leftovers (unknown/NULL type, post >= g):
                    --    shipped → RL, untouched → L. Never 'NA'.
                    WHEN ISNULL([ALLOC_QTY],0) > 0 THEN 'RL'
                    ELSE 'L'
                  END,
                  [TBL_LISTED_DATE] = CASE
                    WHEN [OPT_TYPE] = 'TBL' AND ISNULL([ALLOC_QTY],0) > 0
                         AND [TBL_LISTED_DATE] IS NULL
                        THEN GETDATE()
                    ELSE [TBL_LISTED_DATE]
                  END
            """)

            # ── Part 8.55 — release warehouse holds on NOT-covered options ──
            # An option whose post-alloc OPT_STATUS is MIX (it shipped
            # something but the display is still under threshold) keeps NO
            # warehouse hold: reserving RDC stock for a display that isn't
            # viable just strands it. The released qty simply stays in the
            # free pool for the next run (no counter-booking needed — holds
            # only materialise in ARS_NL_TBL_HOLD_TRACKING at Approve).
            #
            # MUST zero three copies: the option row (FINAL_TABLE), the
            # size rows (ALLOC_TABLE) and this session's ARS_ALLOC_PARKED
            # rows — hold-tracking Step B reads HOLD_QTY from
            # ARS_ALLOC_HISTORY, which is copied from PARKED at Approve
            # (parking ran in Part 8.4, BEFORE this step), so zeroing only
            # the working copies would still commit the hold.
            # Audit: HOLD_RELEASED_QTY on the option row + ALLOC_REMARKS
            # stamp on every size row.
            try:
                # Business rule ALC_HOLD_RELEASE_855: inactive → MIX options
                # keep their holds (pre-2026-07-30 behavior).
                if not rule_flag('ALC_HOLD_RELEASE_855', True):
                    logger.info("Part 8.55 skipped — business rule ALC_HOLD_RELEASE_855 inactive")
                    raise StopIteration("skip")
                try:
                    _run(ac, f"ALTER TABLE [{FINAL_TABLE}] ADD [HOLD_RELEASED_QTY] FLOAT NULL")
                except Exception:
                    pass  # column exists
                # 1. option grain: remember the released qty, zero the hold
                _run(ac, f"""
                    UPDATE [{FINAL_TABLE}]
                    SET [HOLD_RELEASED_QTY] = ISNULL([HOLD_QTY], 0),
                        [HOLD_QTY] = 0
                    WHERE [OPT_STATUS] = 'MIX'
                      AND ISNULL([HOLD_QTY], 0) > 0
                """)
                # 2-3. size grain: alloc working + this session's parked copy
                for _htbl, _hfilter in ((ALLOC_TABLE, ""),
                                        ("ARS_ALLOC_PARKED",
                                         "AND A.[SESSION_ID] = :sid")):
                    if not _table_exists(ac, _htbl):
                        continue
                    ac.execute(text(f"""
                        UPDATE A SET
                            A.[ALLOC_REMARKS] = ISNULL(A.[ALLOC_REMARKS], '')
                                + ';HOLD_RELEASED_NOT_COVERED('
                                + CAST(CAST(ISNULL(A.[HOLD_QTY],0) AS INT) AS NVARCHAR(20)) + ')',
                            A.[HOLD_QTY] = 0,
                            A.[ROUND_HOLD] = 0
                        FROM [{_htbl}] A
                        INNER JOIN [{FINAL_TABLE}] L
                            ON  L.[WERKS] = A.[WERKS]
                            AND L.[MAJ_CAT] = A.[MAJ_CAT]
                            AND L.[GEN_ART_NUMBER] = A.[GEN_ART_NUMBER]
                            AND ISNULL(L.[CLR], '') = ISNULL(A.[CLR], '')
                        WHERE L.[OPT_STATUS] = 'MIX'
                          AND ISNULL(L.[HOLD_RELEASED_QTY], 0) > 0
                          AND ISNULL(A.[HOLD_QTY], 0) > 0
                          {_hfilter}
                    """), ({"sid": session_id} if _hfilter else {}))
                rel = ac.execute(text(
                    f"SELECT COUNT(*), ISNULL(SUM([HOLD_RELEASED_QTY]),0) "
                    f"FROM [{FINAL_TABLE}] WHERE ISNULL([HOLD_RELEASED_QTY],0) > 0 "
                    f"AND [OPT_STATUS] = 'MIX'"
                )).fetchone()
                logger.info(
                    f"Part 8.55: released warehouse holds on {rel[0]} "
                    f"not-covered (MIX) options — {rel[1]:.0f} pcs back to pool"
                )
            except StopIteration:
                pass  # rule-driven skip (ALC_HOLD_RELEASE_855 inactive) — logged above
            except Exception as he:
                logger.warning(f"Part 8.55 hold release failed: {he}")
    except StopIteration:
        pass  # rule-driven skip (LST_OPT_STATUS_STAMP inactive) — logged above
    except Exception as e:
        logger.warning(f"OPT_STATUS post-processing failed: {e}")
    t0 = _time_step("Part 8.5 (OPT_STATUS + TBL_LISTED_DATE + hold release)", t0)

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
                    [ALLOC_TYPE]      NVARCHAR(10)  NOT NULL DEFAULT '',
                    CONSTRAINT PK_ARS_NL_TBL_HOLD_TRACKING
                        PRIMARY KEY CLUSTERED ([WERKS], [VAR_ART], [SZ], [ALLOC_TYPE])
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

            # Add ALLOC_TYPE if the table predates the Fresh/GRT change
            # (FS-DB-03 / FS-10 item 1 — mirrors the RDC guard above).
            # NOT NULL DEFAULT '' backfills legacy rows with '' = "matches
            # any run type". Note: the PK widening to (WERKS, VAR_ART, SZ,
            # ALLOC_TYPE) on pre-existing tables is handled by the migration
            # (already applied on HOPC866); this inline guard only adds the
            # column so fresh envs and old snapshots keep working.
            try:
                _run(ac, """
                    IF NOT EXISTS (
                        SELECT 1 FROM sys.columns
                        WHERE object_id = OBJECT_ID('ARS_NL_TBL_HOLD_TRACKING')
                          AND name = 'ALLOC_TYPE'
                    )
                    ALTER TABLE [ARS_NL_TBL_HOLD_TRACKING]
                        ADD [ALLOC_TYPE] NVARCHAR(10) NOT NULL DEFAULT ''
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
            # Parallel allocation tracking.
            "allocation_mode":  mode,
            "parallel_workers": n_workers,
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
        progress = get_progress(conn, batch_id)
        failed   = get_failed_list(conn, batch_id)
        # Skip the SUM aggregate while the batch still has open rows — its
        # totals are only meaningful once everything is DONE, and running it
        # mid-run was the worst lock-contention query on every 3s poll.
        if int(progress.get("pending", 0)) + int(progress.get("in_progress", 0)) > 0:
            summary = {
                "ship_total":   0.0, "hold_total":   0.0,
                "rows_total":   0,
                "max_duration": 0.0, "sum_duration": 0.0,
            }
        else:
            summary = get_done_summary(conn, batch_id)
        return {
            "success":  True,
            "progress": progress,
            "failed":   failed,
            "summary":  summary,
        }


class RetryFailedRequest(BaseModel):
    batch_id:         str
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

        # Recover the original run's pool + hold-toggle context. Retry reuses
        # the existing ARS_ALLOC_WORKING (Stage A/B not re-run), but the
        # waterfall still needs alloc_type for the typed hold-tracking load
        # and the three toggles for the TBL suppression mask — defaulting
        # them would silently retry a GRT run as FRESH.
        _retry_alloc_type = None
        try:
            _retry_alloc_type = conn.execute(text(
                "SELECT TOP 1 [ALLOC_TYPE] FROM [ARS_ALLOC_WORKING] "
                "WHERE [ALLOC_TYPE] IN ('FRESH','GRT')"
            )).scalar()
        except Exception:
            pass
        _saved = _load_listing_settings(conn)
        _retry_alloc_type = _retry_alloc_type or _saved.get("alloc_type") or "FRESH"
        _retry_skip_upc  = str(_saved.get("skip_hold_upc", "false")).lower() == "true"
        _retry_hold_app  = str(_saved.get("apply_hold_seg_app", "true")).lower() != "false"
        _retry_hold_gm   = str(_saved.get("apply_hold_seg_gm", "true")).lower() != "false"
        try:
            _retry_stock_thr = float(_saved.get("stock_threshold_pct") or 0.6)
        except Exception:
            _retry_stock_thr = 0.6
        try:
            _retry_dacs = float(_saved.get("default_acs_d") or 18.0)
        except Exception:
            _retry_dacs = 18.0

    logger.info(
        f"[retry-failed] batch={req.batch_id} mode=per_opt "
        f"workers={req.parallel_workers} alloc_type={_retry_alloc_type} "
        f"→ re-dispatching {reset_count} "
        f"MAJ_CAT(s): {failed_mcs[:10]}{'...' if len(failed_mcs) > 10 else ''}"
    )

    n_workers = max(2, min(8, int(req.parallel_workers or 4)))
    from app.services.rule_engine_pandas import run_listing_and_allocation_pandas
    result = run_listing_and_allocation_pandas(
        n_workers=n_workers,
        batch_id=req.batch_id,
        only_majcats=failed_mcs,
        alloc_type=_retry_alloc_type,
        skip_hold_upc=_retry_skip_upc,
        apply_hold_seg_app=_retry_hold_app,
        apply_hold_seg_gm=_retry_hold_gm,
        stock_threshold_pct=_retry_stock_thr,
        default_acs_d=_retry_dacs,
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
    # Fire report-generation events on a FRESH approve only (not a duplicate).
    # In ARS one parked-run approve both promotes the listing AND writes its
    # pending allocations, so both events fire from this single point — a
    # report subscribed to either will run in the background with this
    # session_id as its folder name. Never blocks/​fails the approve.
    if not result.get("error") and not result.get("already_approved"):
        try:
            from app.services.report_scheduler_service import (
                emit_event, EVENT_LISTING_APPROVED, EVENT_PENDALC_APPROVED,
            )
            emit_event(EVENT_LISTING_APPROVED, session_id=session_id, user=user)
            emit_event(EVENT_PENDALC_APPROVED, session_id=session_id, user=user)
        except Exception as e:
            logger.warning(f"[report-gen] emit approve events failed: {e}")
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


@router.get("/sessions/{session_id}/run-params")
def get_session_run_params(session_id: str,
                           current_user: User = Depends(get_current_user)):
    """Every tunable + condition that produced a run, from ARS_RUN_PARAMS_AUDIT.

    Session-wise review surface for the audit table (which is otherwise
    write-only). Returns params grouped by PARAM_GROUP plus a friendly
    `conditions` summary that answers "which fallback / which mode did this
    run use?" at a glance — including the sec-cap STANDARD vs MATRIX
    condition.

    Pins to the LATEST run of the session (a session normally has one
    /generate, but reruns are guarded via MAX(RUN_TS)). Never raises 500 —
    a missing table or a session with no audit rows returns empty groups
    with a `note`, so the UI degrades gracefully for pre-audit sessions.
    """
    # Friendly summary keys → the "conditions" strip in the UI.
    _COND_KEYS = [
        "sec_cap_mode", "apply_sec_cap_in_normal", "cont_fallback_mode",
        "rl_dispatch_mode", "tbc_dispatch_mode", "alloc_type",
        "mix_mode", "rdc_mode", "run_mode",
    ]
    _GROUP_ORDER = ["LISTING", "RANKING", "ALLOCATION", "SEC_CAP", "FLAGS"]

    de = get_data_engine()
    try:
        with de.connect() as conn:
            if not _table_exists(conn, "ARS_RUN_PARAMS_AUDIT"):
                return {"success": True, "session_id": session_id,
                        "run_id": None, "user_id": None, "run_ts": None,
                        "groups": {}, "conditions": {},
                        "note": "ARS_RUN_PARAMS_AUDIT does not exist yet — "
                                "no run has been audited."}

            head = conn.execute(text(
                "SELECT TOP 1 RUN_ID, USER_ID, RUN_TS "
                "FROM [ARS_RUN_PARAMS_AUDIT] WHERE SESSION_ID = :sid "
                "ORDER BY RUN_TS DESC"
            ), {"sid": session_id}).fetchone()
            if not head:
                return {"success": True, "session_id": session_id,
                        "run_id": None, "user_id": None, "run_ts": None,
                        "groups": {}, "conditions": {},
                        "note": f"No audited parameters for session {session_id}."}

            run_id  = head[0]
            user_id = head[1]
            run_ts  = head[2].isoformat() if head[2] is not None else None

            rows = conn.execute(text(
                "SELECT PARAM_GROUP, PARAM_NAME, PARAM_VALUE "
                "FROM [ARS_RUN_PARAMS_AUDIT] WHERE RUN_ID = :rid "
                "ORDER BY PARAM_GROUP, PARAM_NAME"
            ), {"rid": run_id}).fetchall()

        groups: Dict[str, List[Dict[str, Any]]] = {}
        flat: Dict[str, str] = {}
        for grp, name, val in rows:
            grp = str(grp)
            groups.setdefault(grp, []).append({"name": str(name), "value": val})
            flat[str(name)] = val

        # Back-compat: runs written before 2026-07-18 have no flat
        # `sec_cap_mode` row — derive it from the growth_matrix JSON `enabled`
        # flag so historic runs still show STANDARD/MATRIX.
        if "sec_cap_mode" not in flat and "growth_matrix" in flat:
            try:
                _gm = json.loads(flat["growth_matrix"] or "{}")
                _mode = "MATRIX" if _gm.get("enabled") else "STANDARD"
            except Exception:
                _mode = "STANDARD"
            flat["sec_cap_mode"] = _mode
            groups.setdefault("SEC_CAP", []).append(
                {"name": "sec_cap_mode", "value": _mode, "derived": True})

        # Order groups for stable UI rendering; unknown groups appended.
        ordered = {g: groups[g] for g in _GROUP_ORDER if g in groups}
        for g in groups:
            if g not in ordered:
                ordered[g] = groups[g]

        conditions = {k: flat[k] for k in _COND_KEYS if k in flat}

        return {"success": True, "session_id": session_id,
                "run_id": run_id, "user_id": user_id, "run_ts": run_ts,
                "groups": ordered, "conditions": conditions}
    except Exception as e:
        logger.warning(f"[run-params] read failed for session {session_id}: {e}")
        return {"success": False, "session_id": session_id,
                "groups": {}, "conditions": {}, "error": str(e)}


def _rp_is_number(v) -> bool:
    if v is None:
        return False
    try:
        float(str(v))
        return True
    except (TypeError, ValueError):
        return False


@router.get("/run-params/catalog")
def get_run_params_catalog(current_user: User = Depends(get_current_user)):
    """Distinct (group, name) params ever audited — drives the Trends picker.

    `numeric` = every non-null value for that param parses as a float, so the
    UI can plot it as a line vs. render it as a categorical step.
    """
    de = get_data_engine()
    try:
        with de.connect() as conn:
            if not _table_exists(conn, "ARS_RUN_PARAMS_AUDIT"):
                return {"success": True, "params": []}
            rows = conn.execute(text(
                "SELECT PARAM_GROUP, PARAM_NAME, PARAM_VALUE "
                "FROM [ARS_RUN_PARAMS_AUDIT]"
            )).fetchall()
        agg: Dict[tuple, Dict[str, Any]] = {}
        for grp, name, val in rows:
            key = (str(grp), str(name))
            a = agg.setdefault(key, {"group": str(grp), "name": str(name),
                                     "numeric": True, "n": 0})
            if val is not None and str(val) != "":
                a["n"] += 1
                if not _rp_is_number(val):
                    a["numeric"] = False
        params = sorted(agg.values(), key=lambda x: (x["group"], x["name"]))
        return {"success": True, "params": params}
    except Exception as e:
        logger.warning(f"[run-params] catalog failed: {e}")
        return {"success": False, "params": [], "error": str(e)}


@router.get("/run-params/trend")
def get_run_params_trend(params: str = "",
                         limit: int = 30,
                         current_user: User = Depends(get_current_user)):
    """Value of each requested param across the last N runs (ascending by time).

    `params` = comma-separated PARAM_NAMEs. Returns one series per param with
    points {session_id, run_ts, value, num}; `num` is the float value or null,
    and series-level `numeric` says whether every point is numeric.
    """
    names = [p.strip() for p in (params or "").split(",") if p.strip()]
    de = get_data_engine()
    try:
        with de.connect() as conn:
            if not names or not _table_exists(conn, "ARS_RUN_PARAMS_AUDIT"):
                return {"success": True, "series": []}
            # The last N runs (by RUN_TS), ascending for charting.
            run_rows = conn.execute(text(
                "SELECT RUN_ID, MAX(RUN_TS) AS TS, MAX(SESSION_ID) AS SID "
                "FROM [ARS_RUN_PARAMS_AUDIT] GROUP BY RUN_ID "
                "ORDER BY TS DESC OFFSET 0 ROWS FETCH NEXT :lim ROWS ONLY"
            ), {"lim": int(limit)}).fetchall()
            runs = list(reversed(run_rows))  # ascending
            run_ids = [r[0] for r in runs]
            if not run_ids:
                return {"success": True, "series": []}
            # Pull all values for the requested names across those runs.
            in_names = ", ".join(f":n{i}" for i in range(len(names)))
            in_runs = ", ".join(f":r{i}" for i in range(len(run_ids)))
            qp: Dict[str, Any] = {}
            for i, n in enumerate(names):
                qp[f"n{i}"] = n
            for i, r in enumerate(run_ids):
                qp[f"r{i}"] = r
            vrows = conn.execute(text(
                f"SELECT RUN_ID, PARAM_NAME, PARAM_VALUE FROM [ARS_RUN_PARAMS_AUDIT] "
                f"WHERE PARAM_NAME IN ({in_names}) AND RUN_ID IN ({in_runs})"
            ), qp).fetchall()
        val_by = {(str(rid), str(nm)): val for rid, nm, val in vrows}
        meta_by_run = {r[0]: (r[1], r[2]) for r in runs}
        series = []
        for nm in names:
            points = []
            numeric = True
            for rid in run_ids:
                v = val_by.get((str(rid), nm))
                ts, sid = meta_by_run.get(rid, (None, None))
                num = float(str(v)) if _rp_is_number(v) else None
                if v is not None and num is None:
                    numeric = False
                points.append({
                    "session_id": sid,
                    "run_ts": ts.isoformat() if ts is not None else None,
                    "value": v,
                    "num": num,
                })
            series.append({"name": nm, "numeric": numeric, "points": points})
        return {"success": True, "series": series}
    except Exception as e:
        logger.warning(f"[run-params] trend failed: {e}")
        return {"success": False, "series": [], "error": str(e)}


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
