"""
Grid Builder API
================
Manages dynamic pivot-grid definitions and executes them against ET_STORE_STOCK.

Tables (Rep_data):
  ARS_GRID_BUILDER  – grid metadata (name, hierarchy cols, kpi filter, output table …)

Endpoints:
  GET  /grid-builder/columns              – list columns from vw_master_product
  GET  /grid-builder/grids                – list all grids
  POST /grid-builder/grids                – create a grid
  PUT  /grid-builder/grids/{id}           – update a grid
  DELETE /grid-builder/grids/{id}         – delete a grid
  POST /grid-builder/grids/{id}/run       – run one grid
  POST /grid-builder/run-all              – run all Active grids
"""

import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, TypeVar

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, validator
from sqlalchemy import text
from loguru import logger

from app.core.config import get_settings
from app.database.session import get_data_engine
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.services.grid_calculations import calculate_per_day_sale
from app.models.rbac import User
from app.utils.db_helpers import (
    run_sql, get_columns as _db_get_columns, column_exists, ensure_column, get_col_type_sql,
)

_settings = get_settings()

router      = APIRouter(prefix="/grid-builder", tags=["Grid Builder"])
GRID_TABLE  = "ARS_GRID_BUILDER"
GRID_HIER_TABLE = "ARS_GRID_HIERARCHY"   # managed table with 1 column per grid hierarchy level
VALID_STATUS = {"Active", "Inactive"}

# ── Run-All async state ─────────────────────────────────────────────────────
# A full Run All takes 5-15 minutes — far longer than Cloudflare's 120-second
# proxy timeout. Running it synchronously kills the user's HTTP request long
# before the work finishes, leaving the UI guessing whether anything is still
# happening. We spawn the work onto a daemon thread and return immediately;
# the UI polls /run-all/status (or just /grids) to track per-grid progress.
_run_all_lock: threading.Lock = threading.Lock()
_run_all_state: Dict[str, Any] = {
    "running":       False,
    "started_at":    None,    # epoch seconds
    "started_by":    None,
    "completed_at":  None,
    "total_grids":   0,
    "workers":       0,
    "results":       [],
    "msa_sync":      {},
    "error":         None,
    "calc_duration": None,
}

# Grids whose hierarchy contains these are article-level → skip for hierarchy table
_ARTICLE_LEVEL_COLS = {"GEN_ART_NUMBER", "ARTICLE_NUMBER", "GEN_ART", "VAR_ART"}


# ── Schemas ──────────────────────────────────────────────────────────────────

class GridCreate(BaseModel):
    grid_name:         str
    description:       Optional[str] = None
    hierarchy_columns: List[str]           # columns from vw_master_product
    kpi_filter:        Optional[str] = None # e.g. 'STK'  – filters on sloc KPI
    output_table:      str                 # e.g. ARS_GRID_STK_RESULT
    status:            str = "Active"
    pivot_only:        bool = False         # True = only pivot, skip lookups & MBQ/OPT_CNT
    weightage:         Optional[float] = 1.0    # priority weight for this grid
    grid_group:        Optional[str] = "Primary" # Primary / Secondary / None
    use_for_opt_sale:  bool = False              # use this grid's MBQ/DISP_Q for listing PER_OPT_SALE
    sec_cap_applicable: bool = False             # participate in Secondary-grid cap math
    sec_cap_pct:       Optional[float] = None    # per-grid cap %; None → use global SEC_CAP_DEFAULT_PCT

    @validator("status")
    def _chk(cls, v):
        if v not in VALID_STATUS:
            raise ValueError("status must be Active or Inactive")
        return v

    @validator("grid_name")
    def _chk_name(cls, v):
        if not v.strip():
            raise ValueError("grid_name cannot be empty")
        return v.strip()

    @validator("output_table")
    def _chk_table(cls, v):
        # Only allow safe table name characters
        import re
        if not re.match(r'^[A-Za-z][A-Za-z0-9_]*$', v.strip()):
            raise ValueError("output_table must start with a letter and contain only letters, numbers, underscores")
        return v.strip().upper()

class GridUpdate(BaseModel):
    grid_name:         Optional[str]       = None
    description:       Optional[str]       = None
    hierarchy_columns: Optional[List[str]] = None
    kpi_filter:        Optional[str]       = None
    output_table:      Optional[str]       = None
    status:            Optional[str]       = None
    pivot_only:        Optional[bool]      = None
    weightage:         Optional[float]     = None
    grid_group:        Optional[str]       = None
    use_for_opt_sale:  Optional[bool]      = None
    sec_cap_applicable: Optional[bool]     = None
    sec_cap_pct:       Optional[float]     = None

    @validator("status")
    def _chk(cls, v):
        if v is not None and v not in VALID_STATUS:
            raise ValueError("status must be Active or Inactive")
        return v


# ── DDL / helpers ─────────────────────────────────────────────────────────────

_run = run_sql  # shared helper: execute + commit


T = TypeVar("T")


def _is_log_full_error(exc: BaseException) -> bool:
    """SQL Server 9002 — 'transaction log for database X is full'.
    pyodbc surfaces it as ProgrammingError SQLSTATE '42000' with '9002' in
    the message. SQLAlchemy wraps that in DBAPIError, so we walk the chain."""
    cur = exc
    while cur is not None:
        msg = str(cur)
        if "9002" in msg and ("transaction log" in msg.lower() or "log is full" in msg.lower()):
            return True
        cur = getattr(cur, "__cause__", None) or getattr(cur, "orig", None)
        # avoid infinite loops if an exception references itself
        if cur is exc:
            break
    return False


def _retry_on_log_full(label: str, fn: Callable[[], T]) -> T:
    """Run fn(); if Azure SQL signals 9002 (log full), wait and retry once.
    Azure SQL DB auto-runs log backups every few minutes, so the typical
    window between hitting 9002 and the platform clearing space is short.
    Configurable via settings.GRID_LOG_FULL_RETRY_COUNT / _DELAY_SEC."""
    delay = max(1, int(_settings.GRID_LOG_FULL_RETRY_DELAY_SEC))
    max_retries = max(0, int(_settings.GRID_LOG_FULL_RETRY_COUNT))
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as e:
            if not _is_log_full_error(e) or attempt >= max_retries:
                raise
            attempt += 1
            logger.warning(
                f"[grid] {label}: SQL 9002 log-full hit (attempt {attempt}/{max_retries}). "
                f"Sleeping {delay}s for Azure SQL to back up the log, then retrying."
            )
            time.sleep(delay)


def _ensure_grid_table(engine):
    """Auto-create ARS_GRID_BUILDER in Rep_data."""
    with engine.connect() as c:
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{GRID_TABLE}')
            BEGIN
                CREATE TABLE {GRID_TABLE} (
                    id                INT IDENTITY(1,1) PRIMARY KEY,
                    grid_name         NVARCHAR(100) NOT NULL,
                    description       NVARCHAR(500) NULL,
                    hierarchy_columns NVARCHAR(MAX) NOT NULL DEFAULT '[]',
                    kpi_filter        NVARCHAR(200) NULL,
                    output_table      NVARCHAR(200) NOT NULL,
                    status            NVARCHAR(20)  NOT NULL DEFAULT 'Active',
                    seq               INT           NOT NULL DEFAULT 0,
                    created_at        DATETIME      NOT NULL DEFAULT GETDATE(),
                    updated_at        DATETIME      NOT NULL DEFAULT GETDATE(),
                    last_run_at       DATETIME      NULL,
                    last_run_status   NVARCHAR(50)  NULL,
                    last_run_rows     INT           NULL,
                    last_run_error    NVARCHAR(MAX) NULL,
                    CONSTRAINT UQ_{GRID_TABLE}_name UNIQUE (grid_name)
                )
            END
        """)
        # Add seq column if missing (for existing tables)
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                           WHERE TABLE_NAME='{GRID_TABLE}' AND COLUMN_NAME='seq')
            BEGIN
                ALTER TABLE {GRID_TABLE} ADD seq INT NOT NULL DEFAULT 0
            END
        """)
        # Add duration_sec column if missing
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                           WHERE TABLE_NAME='{GRID_TABLE}' AND COLUMN_NAME='duration_sec')
            BEGIN
                ALTER TABLE {GRID_TABLE} ADD duration_sec FLOAT NULL
            END
        """)
        # Add pivot_only flag (1 = skip lookups & MBQ/OPT_CNT, just pivot)
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                           WHERE TABLE_NAME='{GRID_TABLE}' AND COLUMN_NAME='pivot_only')
            BEGIN
                ALTER TABLE {GRID_TABLE} ADD pivot_only BIT NOT NULL DEFAULT 0
            END
        """)
        # Add weightage (numeric priority weight)
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                           WHERE TABLE_NAME='{GRID_TABLE}' AND COLUMN_NAME='weightage')
            BEGIN
                ALTER TABLE {GRID_TABLE} ADD weightage FLOAT NULL DEFAULT 1.0
            END
        """)
        # Add grid_group (Primary / Secondary classification)
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                           WHERE TABLE_NAME='{GRID_TABLE}' AND COLUMN_NAME='grid_group')
            BEGIN
                ALTER TABLE {GRID_TABLE} ADD grid_group NVARCHAR(50) NULL DEFAULT 'Primary'
            END
        """)
        # Add use_for_opt_sale flag (marks ONE grid as source for PER_OPT_SALE calc)
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                           WHERE TABLE_NAME='{GRID_TABLE}' AND COLUMN_NAME='use_for_opt_sale')
            BEGIN
                ALTER TABLE {GRID_TABLE} ADD use_for_opt_sale BIT NOT NULL DEFAULT 0
            END
        """)
        # Per-grid sec-cap applicability + optional per-grid % override.
        # Backfill: OFF for every existing row (opt-in path chosen 2026-05-17).
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                           WHERE TABLE_NAME='{GRID_TABLE}' AND COLUMN_NAME='sec_cap_applicable')
            BEGIN
                ALTER TABLE {GRID_TABLE} ADD sec_cap_applicable BIT NOT NULL DEFAULT 0
            END
        """)
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                           WHERE TABLE_NAME='{GRID_TABLE}' AND COLUMN_NAME='sec_cap_pct')
            BEGIN
                ALTER TABLE {GRID_TABLE} ADD sec_cap_pct FLOAT NULL
            END
        """)
        # Auto-assign sequence where seq=0 based on id order
        _run(c, f"""
            ;WITH CTE AS (
                SELECT id, seq, ROW_NUMBER() OVER (ORDER BY id) AS rn
                FROM {GRID_TABLE} WHERE seq = 0
            )
            UPDATE CTE SET seq = rn WHERE seq = 0
        """)
        # Reset stuck "Running" status (from crashed/stopped server)
        _run(c, f"""
            UPDATE {GRID_TABLE}
            SET last_run_status = 'Interrupted', last_run_error = 'Server stopped during run'
            WHERE last_run_status = 'Running'
        """)

    # Also sync the hierarchy table
    _ensure_hierarchy_table(engine)


def _ensure_hierarchy_table(engine):
    """
    Auto-create and maintain ARS_GRID_HIERARCHY — a managed table whose
    columns are derived from ARS_GRID_BUILDER grid definitions.

    Rules:
      - Base column: MAJ_CAT (always present, PK)
      - One column per active non-article grid, named after the LAST
        hierarchy column (e.g. RNG_SEG, MACRO_MVGR, FAB, CLR, M_VND_CD)
      - Grids with GEN_ART/VAR_ART in hierarchy are skipped
      - First-time creation: columns are created in grid.seq order
      - Subsequent runs: ADD-ONLY. Columns for newly-active grids are added
        via ALTER TABLE ADD. Columns for grids that were deleted or moved
        to Inactive are NEVER dropped, so their data survives. Physical
        column order is therefore not re-shuffled by reorder/deactivate
        (consumers reference columns by name, not position). Use
        /hierarchy/compact to prune orphan columns explicitly.
    """
    with engine.connect() as conn:
        # Read grid definitions
        try:
            grids = conn.execute(text(f"""
                SELECT grid_name, hierarchy_columns, seq
                FROM {GRID_TABLE}
                WHERE UPPER(status) = 'ACTIVE'
                ORDER BY seq ASC, id ASC
            """)).fetchall()
        except Exception:
            return  # ARS_GRID_BUILDER might not exist yet

        # Derive column list from grids
        hier_cols = []  # [(col_name, grid_name, seq)]
        for gname, hier_json, seq in grids:
            try:
                hier = json.loads(hier_json) if isinstance(hier_json, str) else hier_json
            except Exception:
                continue
            if not hier or len(hier) < 2:
                continue
            # Skip article-level grids
            if any(h.upper() in _ARTICLE_LEVEL_COLS for h in hier):
                continue
            last_col = hier[-1].upper()
            # Skip WERKS/MAJ_CAT (already base columns)
            if last_col in ("WERKS", "MAJ_CAT"):
                continue
            hier_cols.append((last_col, gname, seq))

        if not hier_cols:
            return

        # Check if table exists
        tbl_exists = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :t"
        ), {"t": GRID_HIER_TABLE}).scalar() > 0

        if not tbl_exists:
            # Create with MAJ_CAT as base + all derived columns (in seq order)
            col_defs = "[MAJ_CAT] NVARCHAR(100) NOT NULL"
            for col_name, gname, seq in hier_cols:
                col_defs += f", [{col_name}] NVARCHAR(200) NULL"
            _run(conn, f"""
                CREATE TABLE [{GRID_HIER_TABLE}] (
                    {col_defs},
                    CONSTRAINT PK_{GRID_HIER_TABLE} PRIMARY KEY ([MAJ_CAT])
                )
            """)
            logger.info(f"Created {GRID_HIER_TABLE} with columns: MAJ_CAT, {', '.join(c[0] for c in hier_cols)}")
            return

        # Table exists — ADD missing columns only. Never DROP, never rebuild.
        existing_upper = {r[0].upper() for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"
        ), {"t": GRID_HIER_TABLE}).fetchall()}

        added = []
        for col_name, gname, seq in hier_cols:
            if col_name.upper() in existing_upper:
                continue
            _run(conn, f'ALTER TABLE [{GRID_HIER_TABLE}] ADD [{col_name}] NVARCHAR(200) NULL')
            existing_upper.add(col_name.upper())
            added.append(col_name)
        if added:
            logger.info(f"{GRID_HIER_TABLE}: added column(s) {added} (existing data preserved)")


def _populate_merge_columns(engine) -> None:
    """
    For every MERGE_<X> column in ARS_GRID_HIERARCHY, re-derive its values
    from the parent column [X] using the active mapping in ARS_MERGE_RULES.

    Idempotent: runs after _ensure_hierarchy_table and after merge-rule edits.
    Skips (with a logged warning) if the parent column is missing or no
    active rules exist for the parent source_col.
    """
    from app.services import derived_masters as dm

    with engine.connect() as conn:
        tbl_exists = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :t"
        ), {"t": GRID_HIER_TABLE}).scalar() > 0
        if not tbl_exists:
            return

        cols = [r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"
        ), {"t": GRID_HIER_TABLE}).fetchall()]
        cols_upper = {c.upper(): c for c in cols}

        for col in cols:
            if not col.upper().startswith(dm.MERGE_COL_PREFIX):
                continue
            parent = col[len(dm.MERGE_COL_PREFIX):]
            parent_actual = cols_upper.get(parent.upper())
            if not parent_actual:
                logger.warning(
                    f"{GRID_HIER_TABLE}: {col} present but parent column "
                    f"{parent} missing — skipping populate"
                )
                continue

            mapping = dm.get_mapping(conn, parent)
            if not mapping:
                logger.warning(
                    f"{GRID_HIER_TABLE}: no active ARS_MERGE_RULES for {parent} — "
                    f"{col} left as-is"
                )
                continue

            def _q(s: str) -> str:
                return "'" + s.replace("'", "''") + "'"
            case_parts = " ".join(
                f"WHEN {_q(sv)} THEN {_q(tv)}" for sv, tv in mapping.items()
            )
            case_sql = (
                f"CASE [{parent_actual}] {case_parts} "
                f"ELSE [{parent_actual}] END"
            )
            _run(conn, f"UPDATE [{GRID_HIER_TABLE}] SET [{col}] = {case_sql}")
            conn.commit()
            logger.info(
                f"{GRID_HIER_TABLE}: re-populated [{col}] from [{parent_actual}] "
                f"via ARS_MERGE_RULES ({len(mapping)} rule(s))"
            )


def _ensure_merge_parent_grid_exists(conn, hierarchy_columns: List[str]) -> None:
    """
    If hierarchy ends with MERGE_<X>, require an Active grid whose last
    hierarchy column = X already exists. Raises 400 otherwise.
    """
    if not hierarchy_columns:
        return
    last = hierarchy_columns[-1].upper()
    if not last.startswith("MERGE_"):
        return
    parent = last[len("MERGE_"):]
    rows = conn.execute(text(
        f"SELECT hierarchy_columns FROM {GRID_TABLE} WHERE UPPER(status) = 'ACTIVE'"
    )).fetchall()
    for (hj,) in rows:
        try:
            h = json.loads(hj) if isinstance(hj, str) else hj
        except Exception:
            continue
        if h and str(h[-1]).upper() == parent:
            return
    raise HTTPException(
        400,
        f"Cannot create/update grid with last column [{last}]: "
        f"no Active parent grid found whose last hierarchy column = [{parent}]. "
        f"Create the parent grid first."
    )


def _row_to_dict(r) -> dict:
    """Convert a row tuple to dict. Column order must match SELECT statements."""
    hier = r[3]
    try:
        hier = json.loads(hier) if hier else []
    except Exception:
        hier = []
    return {
        "id":                r[0],
        "grid_name":         r[1],
        "description":       r[2],
        "hierarchy_columns": hier,
        "kpi_filter":        r[4],
        "output_table":      r[5],
        "status":            r[6],
        "seq":               r[7],
        "created_at":        r[8].isoformat() if r[8] else None,
        "updated_at":        r[9].isoformat() if r[9] else None,
        "last_run_at":       r[10].isoformat() if r[10] else None,
        "last_run_status":   r[11],
        "last_run_rows":     r[12],
        "last_run_error":    r[13],
        "duration_sec":      r[14] if len(r) > 14 else None,
        "pivot_only":         bool(r[15]) if len(r) > 15 else False,
        "weightage":          r[16] if len(r) > 16 else 1.0,
        "grid_group":         r[17] if len(r) > 17 else "Primary",
        "use_for_opt_sale":   bool(r[18]) if len(r) > 18 else False,
        "sec_cap_applicable": bool(r[19]) if len(r) > 19 else False,
        "sec_cap_pct":        r[20] if len(r) > 20 else None,
    }


def _get_table_columns(engine, table_name: str) -> List[str]:
    """Return column names for a table/view from INFORMATION_SCHEMA."""
    try:
        with engine.connect() as conn:
            return _db_get_columns(conn, table_name)
    except Exception as e:
        logger.warning(f"Could not read {table_name} columns: {e}")
        return []


def _get_master_product_columns(engine) -> List[str]:
    """Return column names from vw_master_product."""
    return _get_table_columns(engine, "vw_master_product")


# ==========================================================================
# POST-GRID CALCULATIONS — imported from app.services.grid_calculations
# Function: calculate_per_day_sale(conn) → returns step logs
# Edit column names/logic in: app/services/grid_calculations.py
# ==========================================================================




# ==========================================================================
# POST-PIVOT LOOKUP CONFIG
# ==========================================================================
# Each entry defines a lookup join to run after the pivot INSERT.
# To add a new lookup: copy an entry and edit the fields.
#
#   lookup_table : source table name — supports templates:
#                  {HIER_LAST}  = last hierarchy column name (e.g. MAJ_CAT, RNG_SEG)
#                  {HIER_2}     = 2nd hierarchy column
#                  {HIER_3}     = 3rd hierarchy column
#                  Example: "Master_CONT_{HIER_LAST}" → "Master_CONT_RNG_SEG"
#   columns      : list of columns to pull (empty [] = filter only, ["*"] = all non-key cols)
#   join_on      : dict mapping {output_table_col: lookup_table_col}
#                  supports {HIER_LAST} in values too
#   requires     : list of hierarchy columns that must be present (uppercase)
#   filter       : (optional) {"column": "COL", "value": "1"}
#                  After join, DELETE rows where column != value
#
POST_PIVOT_LOOKUPS = [
    # 1. Filter: keep only stores where LISTING=1 in Master_ALC_INPUT_ST_MASTER
    {
        "lookup_table": "Master_ALC_INPUT_ST_MASTER",
        "columns":      ["LISTING"],
        "join_on":      {"WERKS": "ST_CD"},
        "requires":     ["WERKS"],
        "filter":       {"column": "LISTING", "value": "1"},
    },
    # 2. Lookup from ARS_CALC_ST_MAJ_CAT (has ALC_D, SAL_PD, CONT — MBQ/OPT_CNT calculated after)
    #    CONT: ST_MAJ_CAT first, CO_MAJ_CAT fallback (merged during pre-grid calc)
    {
        "lookup_table": "ARS_CALC_ST_MAJ_CAT",
        "columns":      ["DISP_Q", "ACS_D", "ALC_D", "DPN", "SAL_D", "SAL_PD", "DISP_GR_DGR", "LW_ACT_SL_GR_DGR", "BGT_SL_GR_DGR", "CONT"],
        "join_on":      {"WERKS": "ST_CD", "MAJ_CAT": "MAJ_CAT"},
        "requires":     ["WERKS", "MAJ_CAT"],
    },
    # 3. Dynamic: join contribution data from Master_CONT_{last hierarchy col}
    #    Grid MJ (WERKS, MAJ_CAT)                        → join on ST_CD + MAJ_CAT
    #    Grid MJ_MACRO_MVGR (WERKS, MAJ_CAT, MACRO_MVGR) → join on ST_CD + MAJ_CAT + MACRO_MVGR
    {
        "lookup_table": "Master_CONT_{HIER_LAST}",
        "columns":      ["CONT"],
        "join_on":      {"WERKS": "ST_CD", "MAJ_CAT": "MAJ_CAT", "{HIER_LAST}": "{HIER_LAST}"},
        "requires":     ["WERKS", "MAJ_CAT"],
    },
    # ── Add more lookups below ──────────────────────────────────────────
]


def _resolve_template(template: str, hier_cols: List[str]) -> str:
    """Resolve {HIER_LAST}, {HIER_2}, {HIER_3} etc. in config strings."""
    result = template
    if "{HIER_LAST}" in result and hier_cols:
        result = result.replace("{HIER_LAST}", hier_cols[-1])
    for i, col in enumerate(hier_cols):
        result = result.replace(f"{{HIER_{i}}}", col)
        result = result.replace(f"{{HIER_{i+1}}}", col)  # 1-based
    return result


_get_col_type_sql = get_col_type_sql  # shared helper


def _insert_missing_msa_rows(
    conn,
    out_table: str,
    hier_cols: List[str],
    slocs: List[str],
    numeric_hier: Set[str],
    mp_cols_upper: dict,
    merge_case_cache: dict,
) -> int:
    """
    Post-pivot synthetic-row injection.

    For every (hier_cols) tuple that exists in the intended-dispatch universe
    (ARS_MSA_TOTAL × Master_ALC_INPUT_ST_MASTER, joined by RDC) but is absent
    from the just-built grid, insert one row with SLOC cols = 0 and STK_TTL=0.

    Covers three scenarios uniformly:
      (a) new MAJ_CAT — warehouse has stock, no in-store stock yet
      (b) new store   — store opened, no stock loaded yet for any MAJ_CAT
      (c) partial gap — (WERKS, MAJ_CAT) exists in grid for some sub-key values
                        (e.g. MERGE_RNG_SEG='PSP') but not others (e.g. 'EV');
                        the EV row gets injected based on MSA articles whose
                        RNG_SEG maps to EV via ARS_MERGE_RULES.

    Performance: the NOT EXISTS check runs at the GRID's full hier_cols grain
    against the indexed output table — fast (hash anti-join). For mature
    MAJ_CATs with full coverage, every Universe row is filtered out and the
    INSERT is essentially free.

    Runs AFTER the main pivot INSERT but BEFORE _apply_post_lookups, so the
    LISTING filter / CONT lookup / MBQ / OPT_CNT calculations apply to
    synthetic rows identically to real ones.

    Returns: number of synthetic rows inserted (0 if no gaps or skipped).
    """
    # Skip cleanly when required source tables are absent.
    msa_exists = conn.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='ARS_MSA_TOTAL'"
    )).scalar() > 0
    stm_exists = conn.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='Master_ALC_INPUT_ST_MASTER'"
    )).scalar() > 0
    if not (msa_exists and stm_exists):
        return 0

    # Universe is built by joining MSA (RDC-keyed) to ST_MASTER (WERKS=ST_CD,
    # RDC) — both keys must be in the grid's hier for the result to be useful.
    upper_hier = {c.upper() for c in hier_cols}
    if 'WERKS' not in upper_hier or 'MAJ_CAT' not in upper_hier:
        return 0

    _msa_native = {"MAJ_CAT", "GEN_ART_NUMBER", "CLR", "ARTICLE_NUMBER"}
    hier_select_parts = []
    for col in hier_cols:
        cu = col.upper()
        default = "0" if cu in numeric_hier else "'NA'"
        if cu == "WERKS":
            hier_select_parts.append(f"SM.[ST_CD] AS [{col}]")
        elif cu in ("ARTICLE_NUMBER", "MATNR"):
            hier_select_parts.append(
                f"ISNULL(TRY_CAST(MSA.[ARTICLE_NUMBER] AS BIGINT), {default}) AS [{col}]"
            )
        elif cu in _msa_native:
            src = f"MSA.[{cu}]"
            if cu in numeric_hier:
                hier_select_parts.append(
                    f"ISNULL(TRY_CAST({src} AS BIGINT), {default}) AS [{col}]"
                )
            else:
                hier_select_parts.append(f"ISNULL({src}, {default}) AS [{col}]")
        elif cu in merge_case_cache:
            # MERGE_<col> derived from MP3 via the same CASE used by the main
            # build — guarantees Universe and grid agree on derived values.
            expr_mp3 = (merge_case_cache[cu]
                        .replace("[MP].", "[MP3].")
                        .replace("MP.", "MP3."))
            hier_select_parts.append(f"ISNULL({expr_mp3}, {default}) AS [{col}]")
        elif cu in mp_cols_upper:
            actual = mp_cols_upper[cu]
            hier_select_parts.append(f"ISNULL(MP3.[{actual}], {default}) AS [{col}]")
        else:
            hier_select_parts.append(f"{default} AS [{col}]")
    msa_hier_select = ", ".join(hier_select_parts)

    hier_cols_sql = ", ".join(f"[{c}]" for c in hier_cols)
    sloc_cols_sql = ", ".join(f"[{s}]" for s in slocs) if slocs else ""
    zero_sloc_vals = ", ".join("CAST(0 AS FLOAT)" for _ in slocs) if slocs else ""

    if sloc_cols_sql:
        insert_cols = f"{hier_cols_sql}, {sloc_cols_sql}, [STK_TTL]"
        select_cols = f"{hier_cols_sql}, {zero_sloc_vals}, CAST(0 AS FLOAT)"
    else:
        insert_cols = f"{hier_cols_sql}, [STK_TTL]"
        select_cols = f"{hier_cols_sql}, CAST(0 AS FLOAT)"

    # NOT EXISTS at FULL hier_cols grain — every column of the grid's
    # composite key is compared. ISNULL on the grid side handles rows whose
    # hier values are still NULL at this stage (NULL→default fill happens
    # later in step 8); the Universe side already has ISNULL'd defaults baked
    # into msa_hier_select.
    not_exists_parts = []
    for col in hier_cols:
        cu = col.upper()
        default = "0" if cu in numeric_hier else "'NA'"
        not_exists_parts.append(f"ISNULL(G.[{col}], {default}) = U.[{col}]")
    not_exists_clause = " AND ".join(not_exists_parts)

    insert_sql = f"""
;WITH Universe AS (
    SELECT DISTINCT
        {msa_hier_select}
    FROM dbo.ARS_MSA_TOTAL MSA WITH (NOLOCK)
    INNER JOIN dbo.Master_ALC_INPUT_ST_MASTER SM WITH (NOLOCK)
        ON SM.RDC = MSA.RDC
       AND UPPER(SM.ST_STATUS) IN ('OLD','NEW','UPC')
    LEFT JOIN dbo.vw_master_product MP3 WITH (NOLOCK)
        ON TRY_CAST(MSA.ARTICLE_NUMBER AS BIGINT) = MP3.ARTICLE_NUMBER
    WHERE MSA.sequence_id = (SELECT MAX(sequence_id) FROM dbo.ARS_MSA_TOTAL)
)
INSERT INTO [{out_table}] ({insert_cols})
SELECT {select_cols}
FROM Universe U
WHERE NOT EXISTS (
    SELECT 1 FROM [{out_table}] G WITH (NOLOCK)
    WHERE {not_exists_clause}
)
"""

    result = conn.execute(text(insert_sql))
    n = result.rowcount if hasattr(result, "rowcount") and result.rowcount is not None else 0
    conn.commit()
    return int(n) if n and n > 0 else 0


def _apply_post_lookups(
    conn,
    out_table: str,
    hier_cols: List[str],
    skip_cont: bool = False,
    filters_only: bool = False,
) -> List[str]:
    """
    After pivot INSERT, join lookup tables and add extra columns.
    skip_cont:    if True, skip CONT lookup (for article-level grids).
    filters_only: if True, process only entries that DELETE rows (currently
                  just the LISTING filter). Used by pivot_only grids that
                  don't need CONT / MBQ / OPT_CNT but MUST still respect the
                  listed-store universe — otherwise unlisted warehouses
                  survive in VAR_ART/GEN_ART and inflate SUM(STK_TTL) vs
                  the rollup grids that do filter.
    Returns list of warning messages (e.g. missing tables).
    """
    hier_upper = {c.upper(): c for c in hier_cols}
    warnings = []

    for cfg in POST_PIVOT_LOOKUPS:
        # filters_only: only run entries that DELETE rows (have a "filter" key)
        if filters_only and not cfg.get("filter"):
            continue

        # Skip CONT lookup for article-level grids
        if skip_cont and "CONT" in cfg.get("columns", []):
            logger.info(f"Skipping CONT lookup for article-level grid {out_table}")
            continue

        # Check all required hierarchy columns are present
        if not all(r in hier_upper for r in cfg["requires"]):
            continue

        # Resolve template in table name
        lookup_table = _resolve_template(cfg["lookup_table"], hier_cols)

        # Self-heal: if this is a derived Master_CONT_MERGE_* table, rebuild
        # from its parent before the existence check. No-op if not a MERGE table
        # or if the parent doesn't exist / has no active rules.
        try:
            from app.services import derived_masters as _dm
            if _dm.is_derived_master_table(lookup_table):
                _dm.ensure_derived_master(conn, lookup_table)
        except Exception as _e:
            logger.warning(f"derived_masters.ensure failed for {lookup_table}: {_e}")

        # Check lookup table exists in DB
        exists = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :tn"
        ), {"tn": lookup_table}).scalar() > 0
        if not exists:
            msg = f"Lookup table '{lookup_table}' not found in DB"
            logger.warning(f"Post-lookup: {msg}")
            warnings.append(msg)

            # If CONT table missing, calculate CONT = 1/COUNT per WERKS+MAJ_CAT group
            if "Master_CONT_" in cfg.get("lookup_table", ""):
                default_cols = [c for c in cfg.get("columns", []) if c != "*"]
                if default_cols:
                    existing_out = {r[0].upper() for r in conn.execute(text(
                        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :tbl"
                    ), {"tbl": out_table}).fetchall()}
                    for col in default_cols:
                        if col.upper() not in existing_out:
                            try:
                                _run(conn, f"ALTER TABLE [{out_table}] ADD [{col}] FLOAT NULL")
                            except Exception:
                                pass

                    # Calculate 1/COUNT of unique rows per WERKS+MAJ_CAT
                    werks_col = hier_upper.get("WERKS", "WERKS")
                    majcat_col = hier_upper.get("MAJ_CAT", "MAJ_CAT")
                    for col in default_cols:
                        _run(conn, f"""
                            ;WITH GrpCount AS (
                                SELECT [{werks_col}], [{majcat_col}],
                                       COUNT(*) AS cnt
                                FROM [{out_table}]
                                GROUP BY [{werks_col}], [{majcat_col}]
                            )
                            UPDATE O SET O.[{col}] = CAST(1.0 / G.cnt AS FLOAT)
                            FROM [{out_table}] O
                            INNER JOIN GrpCount G
                                ON O.[{werks_col}] = G.[{werks_col}]
                                AND O.[{majcat_col}] = G.[{majcat_col}]
                        """)
                    msg2 = f"Column(s) {default_cols} set to 1/COUNT(WERKS+MAJ_CAT) ('{lookup_table}' not found)"
                    logger.info(msg2)
                    warnings.append(msg2)
            continue

        # Resolve template in join_on keys and values
        # Get actual columns from lookup table to handle name mismatches
        lkp_actual_cols = {r[0].upper(): r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :tbl"
        ), {"tbl": lookup_table}).fetchall()}

        join_on = {}
        join_skip = False
        for out_col, lkp_col in cfg["join_on"].items():
            resolved_out = _resolve_template(out_col, hier_cols)
            resolved_lkp = _resolve_template(lkp_col, hier_cols)
            # Auto-fix: if resolved lookup column doesn't exist, try matching by suffix
            # e.g., grid has "M_VND_CD" but lookup table has "VND_CD"
            if resolved_lkp.upper() not in lkp_actual_cols:
                matched = None
                for actual_upper, actual_name in lkp_actual_cols.items():
                    if resolved_lkp.upper().endswith(actual_upper) or actual_upper.endswith(resolved_lkp.upper()):
                        matched = actual_name
                        break
                if matched:
                    logger.info(f"Post-lookup column fix: [{resolved_lkp}] -> [{matched}] in {lookup_table}")
                    resolved_lkp = matched
                else:
                    warnings.append(f"Column [{resolved_lkp}] not found in {lookup_table}")
                    join_skip = True
                    break
            else:
                resolved_lkp = lkp_actual_cols[resolved_lkp.upper()]
            join_on[resolved_out] = resolved_lkp

        if join_skip:
            continue

        # Resolve columns: ["*"] = all columns from lookup except join-target columns
        columns = cfg["columns"]
        # Get actual columns in lookup table
        all_lkp_col_rows = conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = :tbl ORDER BY ORDINAL_POSITION"
        ), {"tbl": lookup_table}).fetchall()
        lkp_col_set = {r[0].upper() for r in all_lkp_col_rows}

        if columns == ["*"]:
            join_target_cols = {v.upper() for v in join_on.values()}
            columns = [r[0] for r in all_lkp_col_rows if r[0].upper() not in join_target_cols]
        else:
            # Filter to only columns that actually exist in the lookup table
            missing = [c for c in columns if c.upper() not in lkp_col_set]
            if missing:
                logger.info(f"Post-lookup: columns {missing} not in {lookup_table}, skipping them")
            columns = [c for c in columns if c.upper() in lkp_col_set]

        if not columns:
            logger.info(f"Post-lookup skipped: no columns to add from {lookup_table}")
            continue

        # Get existing output columns to avoid duplicates
        existing_out_cols = {r[0].upper() for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :tbl"
        ), {"tbl": out_table}).fetchall()}

        # Add missing columns to output table (using real types from lookup)
        for col in columns:
            if col.upper() not in existing_out_cols:
                col_type = _get_col_type_sql(conn, lookup_table, col)
                try:
                    _run(conn, f"ALTER TABLE [{out_table}] ADD [{col}] {col_type} NULL")
                    existing_out_cols.add(col.upper())  # track newly added
                except Exception:
                    pass  # column may already exist from a prior run

        # Build join condition
        join_parts = " AND ".join(
            f"O.[{hier_upper.get(ok.upper(), ok)}] = L.[{lv}]"
            for ok, lv in join_on.items()
        )

        # UPDATE output table with lookup columns
        set_parts = ", ".join(f"O.[{c}] = L.[{c}]" for c in columns)
        _run(conn, f"""
            UPDATE O SET {set_parts}
            FROM [{out_table}] O
            INNER JOIN [{lookup_table}] L WITH (NOLOCK) ON {join_parts}
        """)
        logger.info(f"Post-lookup: joined {len(columns)} cols from {lookup_table} into {out_table}")

        # CONT fallback: if store-level CONT is NULL, use CO (company) level
        if "Master_CONT_" in cfg.get("lookup_table", "") and "CONT" in columns:
            # Check if lookup table has ST_CD column (it should)
            has_st_cd = conn.execute(text(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME = :tbl AND COLUMN_NAME = 'ST_CD'"
            ), {"tbl": lookup_table}).scalar() > 0
            if has_st_cd:
                # Build CO join: same as original join but replace WERKS=ST_CD with ST_CD='CO'
                co_join_parts = []
                for ok, lv in join_on.items():
                    resolved_ok = hier_upper.get(ok.upper(), ok)
                    if lv.upper() == "ST_CD":
                        co_join_parts.append(f"L.[ST_CD] = 'CO'")
                    else:
                        co_join_parts.append(f"O.[{resolved_ok}] = L.[{lv}]")
                co_join = " AND ".join(co_join_parts)

                co_set = ", ".join(f"O.[{c}] = L.[{c}]" for c in columns)
                null_check = " AND ".join(f"O.[{c}] IS NULL" for c in columns)
                _run(conn, f"""
                    UPDATE O SET {co_set}
                    FROM [{out_table}] O
                    INNER JOIN [{lookup_table}] L WITH (NOLOCK) ON {co_join}
                    WHERE {null_check}
                """)
                co_count = conn.execute(text(
                    f"SELECT COUNT(*) FROM [{out_table}] WHERE [CONT] IS NOT NULL"
                )).scalar()
                logger.info(f"CONT CO fallback applied — {co_count} rows now have CONT")

        # Apply filter: DELETE rows that don't match criteria
        flt = cfg.get("filter")
        if flt:
            fcol = flt["column"]
            fval = flt["value"]
            before = conn.execute(text(f"SELECT COUNT(*) FROM [{out_table}]")).scalar()
            _run(conn, f"""
                DELETE FROM [{out_table}]
                WHERE ISNULL(CAST([{fcol}] AS NVARCHAR(50)), '') <> :fval
            """, {"fval": str(fval)})
            after = conn.execute(text(f"SELECT COUNT(*) FROM [{out_table}]")).scalar()
            logger.info(f"Post-lookup filter: [{fcol}]={fval} → kept {after}/{before} rows")

    return warnings


# ==========================================================================
# GRID-LEVEL CALCULATIONS (run on output table after lookups)
# ==========================================================================
# MBQ     = (SAL_PD * BGT_SL_GR_DGR) * ALC_D + (DISP_Q * DISP_GR_DGR)
#           Default 1 if BGT_SL_GR_DGR or DISP_GR_DGR is blank/null
#           If DISP_Q is 0/NULL → MBQ = 0 (no display fixture ⇒ no minimum buy)
#           Then: MBQ = ROUND(MBQ * CONT, 1)
# OPT_CNT = ROUND(DISP_Q * CONT / ACS_D, 1)
# [L-7 DAYS SALE-Q] = [L-7 DAYS SALE-Q] * LW_ACT_SL_GR_DGR (default 1 if null/0)
# ==========================================================================

_col_exists_in = column_exists  # shared helper
_ensure_output_col = ensure_column  # shared helper


def _calculate_grid_columns(conn, out_table: str) -> List[str]:
    """
    Calculate MBQ and OPT_CNT in the grid output table.
    Returns list of warning messages.
    """
    warnings = []

    # ── MBQ = (SAL_PD * BGT_SL_GR_DGR) * ALC_D + (DISP_Q * DISP_GR_DGR) ──
    # Support both new (ALC_D/ACS_D) and legacy (SAL_D/DPN) column names
    _alc_col = "ALC_D" if _col_exists_in(conn, out_table, "ALC_D") else ("SAL_D" if _col_exists_in(conn, out_table, "SAL_D") else "ALC_D")
    _acs_col = "ACS_D" if _col_exists_in(conn, out_table, "ACS_D") else ("DPN" if _col_exists_in(conn, out_table, "DPN") else "ACS_D")
    mbq_required = ["SAL_PD", _alc_col, "DISP_Q", "DISP_GR_DGR", "BGT_SL_GR_DGR"]
    mbq_missing = [c for c in mbq_required if not _col_exists_in(conn, out_table, c)]
    if mbq_missing:
        warnings.append(f"MBQ skipped: missing {mbq_missing}")
    else:
        _ensure_output_col(conn, out_table, "MBQ")
        try:
            # Step 1: Calculate raw MBQ
            # Hard rule: DISP_Q = 0 / NULL ⇒ MBQ = 0 (no display fixture ⇒
            # no minimum buy, regardless of SAL_PD contribution).
            _run(conn, f"""
                UPDATE [{out_table}] SET [MBQ] =
                    CASE WHEN ISNULL(TRY_CAST([DISP_Q] AS FLOAT), 0) = 0 THEN 0
                         ELSE ROUND(
                            (ISNULL(TRY_CAST([SAL_PD] AS FLOAT), 0)
                             * CASE WHEN ISNULL(TRY_CAST([BGT_SL_GR_DGR] AS FLOAT), 0) = 0 THEN 1
                                    ELSE TRY_CAST([BGT_SL_GR_DGR] AS FLOAT) END)
                            * ISNULL(TRY_CAST([{_alc_col}] AS FLOAT), 0)
                            + (TRY_CAST([DISP_Q] AS FLOAT)
                               * CASE WHEN ISNULL(TRY_CAST([DISP_GR_DGR] AS FLOAT), 0) = 0 THEN 1
                                      ELSE TRY_CAST([DISP_GR_DGR] AS FLOAT) END), 0)
                    END
            """)
            # Step 2: MBQ = ROUND(MBQ * CONT, 0) — if CONT is 0 or NULL, MBQ = 0
            if _col_exists_in(conn, out_table, "CONT"):
                _run(conn, f"""
                    UPDATE [{out_table}] SET [MBQ] =
                        CASE WHEN ISNULL(TRY_CAST([CONT] AS FLOAT), 0) = 0 THEN 0
                             ELSE ROUND([MBQ] * TRY_CAST([CONT] AS FLOAT), 0)
                        END
                """)
            logger.info(f"MBQ calculated in {out_table}")
        except Exception as e:
            warnings.append(f"MBQ error: {str(e)[:150]}")

    # ── OPT_CNT = ROUND(DISP_Q * DISP_GR_DGR * CONT / ACS_D, 0) ─────────────
    opt_required = ["DISP_Q", "CONT", _acs_col, "DISP_GR_DGR"]
    opt_missing = [c for c in opt_required if not _col_exists_in(conn, out_table, c)]
    if opt_missing:
        warnings.append(f"OPT_CNT skipped: missing {opt_missing}")
    else:
        _ensure_output_col(conn, out_table, "OPT_CNT")
        try:
            _run(conn, f"""
                UPDATE [{out_table}] SET [OPT_CNT] =
                    CASE
                        WHEN ISNULL(TRY_CAST([CONT] AS FLOAT), 0) = 0 THEN 0
                        WHEN ISNULL(TRY_CAST([{_acs_col}] AS FLOAT), 0) = 0 THEN 0
                        ELSE ROUND(ISNULL(TRY_CAST([DISP_Q] AS FLOAT), 0)
                                 * CASE WHEN ISNULL(TRY_CAST([DISP_GR_DGR] AS FLOAT), 0) = 0 THEN 1
                                        ELSE TRY_CAST([DISP_GR_DGR] AS FLOAT) END
                                 * TRY_CAST([CONT] AS FLOAT)
                                 / TRY_CAST([{_acs_col}] AS FLOAT), 0)
                    END
            """)
            logger.info(f"OPT_CNT calculated in {out_table}")
        except Exception as e:
            warnings.append(f"OPT_CNT error: {str(e)[:150]}")

    # ── [L-7 DAYS SALE-Q] *= LW_ACT_SL_GR_DGR  (default 1 if null/0) ────────
    # Mutates the column in-place so downstream STR uses the grown value.
    _sale_q_col = "L-7 DAYS SALE-Q"
    if _col_exists_in(conn, out_table, _sale_q_col) and _col_exists_in(conn, out_table, "LW_ACT_SL_GR_DGR"):
        try:
            _run(conn, f"""
                UPDATE [{out_table}] SET [{_sale_q_col}] = ROUND(
                    ISNULL(TRY_CAST([{_sale_q_col}] AS FLOAT), 0)
                    * CASE WHEN ISNULL(TRY_CAST([LW_ACT_SL_GR_DGR] AS FLOAT), 0) = 0 THEN 1
                           ELSE TRY_CAST([LW_ACT_SL_GR_DGR] AS FLOAT) END, 2)
            """)
            logger.info(f"[{_sale_q_col}] multiplied by LW_ACT_SL_GR_DGR in {out_table}")
        except Exception as e:
            warnings.append(f"[{_sale_q_col}] * LW_ACT_SL_GR_DGR error: {str(e)[:150]}")

    # ── STR = STK_TTL / ([L-7 DAYS SALE-Q] / 7)  (days of stock cover)
    if _col_exists_in(conn, out_table, "STK_TTL") and _col_exists_in(conn, out_table, _sale_q_col):
        _ensure_output_col(conn, out_table, "STR")
        try:
            _run(conn, f"""
                UPDATE [{out_table}] SET [STR] =
                    CASE
                        WHEN ISNULL(TRY_CAST([{_sale_q_col}] AS FLOAT), 0) / 7.0 <= 0 THEN NULL
                        ELSE ROUND(ISNULL(TRY_CAST([STK_TTL] AS FLOAT), 0)
                                 / (TRY_CAST([{_sale_q_col}] AS FLOAT) / 7.0), 0)
                    END
            """)
            logger.info(f"STR calculated in {out_table}")
        except Exception as e:
            warnings.append(f"STR error: {str(e)[:150]}")

    # ── DISP_Q = DISP_Q * CONT  (effective display qty after contribution)
    # IMPORTANT: must run AFTER MBQ and OPT_CNT since those use the RAW DISP_Q.
    if _col_exists_in(conn, out_table, "DISP_Q") and _col_exists_in(conn, out_table, "CONT"):
        try:
            _run(conn, f"""
                UPDATE [{out_table}] SET [DISP_Q] =
                    CASE WHEN ISNULL(TRY_CAST([CONT] AS FLOAT), 0) = 0 THEN 0
                         ELSE ROUND(ISNULL(TRY_CAST([DISP_Q] AS FLOAT), 0)
                                  * TRY_CAST([CONT] AS FLOAT), 0)
                    END
            """)
            logger.info(f"DISP_Q multiplied by CONT in {out_table}")
        except Exception as e:
            warnings.append(f"DISP_Q*CONT error: {str(e)[:150]}")

    return warnings


def _build_and_run_grid(engine, grid: dict) -> dict:
    """
    Execute the dynamic pivot SQL for a grid and store results in output_table.
    Returns {"rows": N, "error": None|str}
    """
    hier_cols:  List[str] = grid["hierarchy_columns"] or ["MATNR", "WERKS"]
    kpi_filter: Optional[str] = grid["kpi_filter"]
    out_table:  str = grid["output_table"]

    # ── 1. Get active SLOCs (optionally filtered by KPI) ────────────────────
    kpi_clause = ""
    if kpi_filter:
        kpi_clause = f" AND UPPER(S.KPI) = '{kpi_filter.upper().replace(chr(39), '')}'"

    with engine.connect() as conn:
        sloc_rows = conn.execute(text(f"""
            SELECT DISTINCT STK.SLOC, S.KPI
            FROM dbo.ET_STORE_STOCK STK WITH (NOLOCK)
            INNER JOIN ARS_STORE_SLOC_SETTINGS S WITH (NOLOCK) ON STK.SLOC = S.SLOC
            WHERE UPPER(S.STATUS) = 'ACTIVE'{kpi_clause}
            ORDER BY STK.SLOC ASC
        """)).fetchall()

        # Include PEND_ALC whenever dbo.ARS_PEND_ALC (V2 schema) exists. The
        # SLOC-settings row is optional: if present its KPI wins so admins can
        # still flag PEND_ALC as KPI='STK'; if absent we default to a non-STK
        # KPI so PEND_ALC appears as a column without rolling into STK_TTL.
        pend_table_exists = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='ARS_PEND_ALC'"
        )).scalar() > 0
        if pend_table_exists:
            pend_row = conn.execute(text(
                "SELECT S.SLOC, S.KPI FROM ARS_STORE_SLOC_SETTINGS S WITH (NOLOCK) "
                "WHERE S.SLOC='PEND_ALC' AND UPPER(S.STATUS)='ACTIVE'"
            )).fetchone()
            pend_kpi = (pend_row[1] if pend_row else 'PEND') or 'PEND'
            sloc_rows = list(sloc_rows) + [('PEND_ALC', pend_kpi)]

    sloc_kpi_pairs = [(r[0], (r[1] or '').upper()) for r in sloc_rows if r[0]]
    if not sloc_kpi_pairs:
        return {"rows": 0, "error": "No ACTIVE SLOCs found matching the criteria"}

    # Sort SLOCs by KPI group → same KPI SLOCs are together, then alphabetical within group
    sloc_kpi_pairs.sort(key=lambda x: (x[1] or 'ZZZ', x[0]))
    slocs = [s for s, _ in sloc_kpi_pairs]

    # SLOCs where KPI = 'STK' → used for STK_TTL calculation
    stk_slocs = [s for s, k in sloc_kpi_pairs if k == "STK"]

    # ── 2. Build quoted column lists ─────────────────────────────────────────
    q_slocs      = ", ".join(f"[{s}]" for s in slocs)
    isnull_cols  = ", ".join(f"ISNULL([{s}],0) AS [{s}]" for s in slocs)
    # SLOC columns in the OUTER SELECT after aggregation to hier_cols grain:
    # raw signed sums (user instruction: clip only STK_TTL, not individual SLOCs).
    sum_isnull_cols = ", ".join(
        f"SUM(ISNULL([{s}],0)) AS [{s}]" for s in slocs
    )
    # Negative SLOC totals (from stock adjustments/returns) are clamped to 0:
    # downstream OPT_REQ = MAX(0, OPT_MBQ - STK_TTL) would otherwise over-order.
    # IMPORTANT: this clip is applied INSIDE the FineStage CTE at (WERKS, MATNR)
    # grain — the finest practical grain — so every grid (coarse or fine)
    # SUMs the same already-non-negative per-article values, guaranteeing
    # SUM(STK_TTL) parity across all grids regardless of grouping.
    _raw_sum     = " + ".join(f"ISNULL([{s}],0)" for s in stk_slocs) if stk_slocs else "0"
    sum_expr     = f"CASE WHEN ({_raw_sum}) < 0 THEN 0 ELSE ({_raw_sum}) END"

    # ── 3. Hierarchy columns SELECT & JOIN ────────────────────────────────────
    # Determine which columns come from vw_master_product vs ET_STORE_STOCK
    mp_cols = _get_master_product_columns(engine)
    mp_cols_upper = {c.upper(): c for c in mp_cols}   # upper→actual name
    stk_cols = _get_table_columns(engine, "ET_STORE_STOCK")
    stk_cols_upper = {c.upper() for c in stk_cols}

    # Dynamically detect numeric columns from INFORMATION_SCHEMA
    # (so numeric cols use ISNULL(..., 0) instead of 'NA' which would fail)
    _NUMERIC_TYPES = {'bigint', 'int', 'smallint', 'tinyint',
                      'float', 'real', 'decimal', 'numeric', 'money', 'smallmoney'}
    mp_type_map = {}   # upper col name → data_type (lowercase)
    stk_type_map = {}
    try:
        with engine.connect() as _tc:
            for r in _tc.execute(text(
                "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME IN ('vw_master_product','ET_STORE_STOCK')"
            )).fetchall():
                cname, ctype = r[0].upper(), r[1].lower()
                # There's one row per (table, column); but the table name isn't returned.
                # Since columns may overlap, we populate both maps — membership check
                # in stk_cols_upper/mp_cols_upper disambiguates.
                if cname in stk_cols_upper:
                    stk_type_map[cname] = ctype
                if cname in mp_cols_upper:
                    mp_type_map[cname] = ctype
    except Exception as _e:
        logger.warning(f"Could not load column types: {_e}")

    def _is_num(col_upper: str, src: str) -> bool:
        m = mp_type_map if src == 'mp' else stk_type_map
        return m.get(col_upper, '') in _NUMERIC_TYPES

    # Fallback set for columns that might exist in ET_STORE_STOCK but not enumerated
    _BIGINT_COLS = {"ARTICLE_NUMBER", "GEN_ART_NUMBER", "MATNR"}

    # Resolve MERGE_<col> hierarchy cols (e.g. MERGE_RNG_SEG) by reading the
    # mapping from ARS_MERGE_RULES and emitting a CASE on the parent MP column.
    # Parent must exist in vw_master_product; otherwise we fall through to the
    # generic resolution and the grid will error visibly.
    from app.services import derived_masters as _dm
    _merge_case_cache: dict = {}
    with engine.connect() as _mc:
        for _col in hier_cols:
            if not _dm.is_merge_col(_col):
                continue
            _parent = _dm.parent_col(_col) or ""
            if _parent.upper() not in mp_cols_upper:
                continue
            _expr = _dm.build_case_expr(_mc, _col, table_alias="MP")
            if _expr:
                _merge_case_cache[_col.upper()] = _expr

    hier_select_parts = []
    has_mp_cols       = False
    numeric_hier: Set[str] = set()   # hier cols that should be numeric (for DDL + PK)
    for col in hier_cols:
        # Priority: ET_STORE_STOCK first (WERKS, MATNR always from fact table)
        # then vw_master_product (LEFT JOIN — can be NULL if no match)
        # ISNULL wraps MP columns: numeric→0, text→'NA' (prevents NULL PKs)
        cu = col.upper()
        if cu in _merge_case_cache:
            # MERGE_<col> resolved via ARS_MERGE_RULES → CASE on parent MP col
            expr = f"ISNULL({_merge_case_cache[cu]}, 'NA') AS [{col}]"
            hier_select_parts.append(expr)
            has_mp_cols = True
        elif cu in stk_cols_upper:
            is_num = _is_num(cu, 'stk') or cu in _BIGINT_COLS
            if is_num:
                numeric_hier.add(cu)
                expr = f"TRY_CAST(STK.[{col}] AS BIGINT) AS [{col}]"
            else:
                expr = f"STK.[{col}]"
            hier_select_parts.append(expr)
        elif cu in mp_cols_upper:
            actual = mp_cols_upper[cu]
            is_num = _is_num(cu, 'mp') or cu in _BIGINT_COLS
            if is_num:
                numeric_hier.add(cu)
                expr = f"ISNULL(TRY_CAST(MP.[{actual}] AS BIGINT), 0) AS [{col}]"
            else:
                expr = f"ISNULL(MP.[{actual}], 'NA') AS [{col}]"
            hier_select_parts.append(expr)
            has_mp_cols = True
        else:
            hier_select_parts.append(f"STK.[{col}]")

    hier_select = ", ".join(hier_select_parts)
    mp_join     = ""
    if has_mp_cols:
        mp_join = "LEFT JOIN dbo.vw_master_product MP WITH (NOLOCK) ON STK.MATNR = MP.ARTICLE_NUMBER"

    # ── 4. Determine output columns & types for CREATE TABLE ─────────────────
    col_defs_parts = []
    for c in hier_cols:
        if c.upper() in numeric_hier:
            col_defs_parts.append(f"[{c}] BIGINT NULL")
        else:
            col_defs_parts.append(f"[{c}] NVARCHAR(200) NULL")
    col_defs = ", ".join(col_defs_parts)
    col_defs += ", " + ", ".join(f"[{s}] FLOAT NULL" for s in slocs)
    col_defs += ", [STK_TTL] FLOAT NULL"

    all_cols  = ", ".join(f"[{c}]" for c in hier_cols) + \
                ", " + q_slocs + ", [STK_TTL]"

    # Expected columns: hierarchy cols + active SLOC cols + STK_TTL
    expected_cols_upper = {c.upper() for c in hier_cols} | {s.upper() for s in slocs} | {"STK_TTL"}

    with engine.connect() as conn:
        # Create output table if it doesn't exist
        _run(conn, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{out_table}')
            CREATE TABLE [{out_table}] ({col_defs})
        """)

        # Get existing columns in the output table
        existing_rows = conn.execute(text("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :tbl
        """), {"tbl": out_table}).fetchall()
        existing_cols = {r[0].upper(): r[0] for r in existing_rows}

        # Add columns for newly active SLOCs (and any missing hierarchy/STK_TTL cols)
        for s in slocs:
            if s.upper() not in existing_cols:
                _run(conn, f"ALTER TABLE [{out_table}] ADD [{s}] FLOAT NULL")
        for c in hier_cols:
            if c.upper() not in existing_cols:
                ctype = "BIGINT NULL" if c.upper() in numeric_hier else "NVARCHAR(200) NULL"
                _run(conn, f"ALTER TABLE [{out_table}] ADD [{c}] {ctype}")
        if "STK_TTL" not in existing_cols:
            _run(conn, f"ALTER TABLE [{out_table}] ADD [STK_TTL] FLOAT NULL")

        # Drop columns for inactive SLOCs (columns not in expected set)
        for col_upper, col_actual in existing_cols.items():
            if col_upper not in expected_cols_upper:
                _run(conn, f"ALTER TABLE [{out_table}] DROP COLUMN [{col_actual}]")

        # Truncate before inserting fresh data
        _run(conn, f"TRUNCATE TABLE [{out_table}]")

        # Check if PEND_ALC is an active SLOC AND the V2 source table exists.
        pend_active = 'PEND_ALC' in [s.upper() for s in slocs]
        has_pend_table = False
        if pend_active:
            has_pend_table = conn.execute(text(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='ARS_PEND_ALC'"
            )).scalar() > 0

        pend_union = ""
        if pend_active and has_pend_table:
            # Build hierarchy select for pend_alc. Columns carried natively by
            # ARS_PEND_ALC (ST_CD/MAJ_CAT/GEN_ART_NUMBER/CLR/ARTICLE_NUMBER) are
            # read from PA directly so rows match even when vw_master_product
            # is stale; anything else (FAB/RNG_SEG/MACRO_MVGR/MICRO_MVGR/vendor
            # codes) still resolves via MP2 on ARTICLE_NUMBER.
            _pa_cols = {'WERKS', 'MAJ_CAT', 'GEN_ART_NUMBER', 'CLR', 'ARTICLE_NUMBER'}
            pend_hier_parts = []
            for col in hier_cols:
                cu = col.upper()
                default = "0" if cu in numeric_hier else "'NA'"
                if cu == 'WERKS':
                    pend_hier_parts.append(f"PA.[ST_CD] AS [{col}]")
                elif cu in _pa_cols:
                    src = f"PA.[{cu}]"
                    if cu in numeric_hier:
                        pend_hier_parts.append(
                            f"ISNULL(TRY_CAST({src} AS BIGINT), {default}) AS [{col}]"
                        )
                    else:
                        pend_hier_parts.append(f"ISNULL({src}, {default}) AS [{col}]")
                elif cu in _merge_case_cache:
                    # Derived MERGE_<col> — compute via the SAME CASE expression
                    # as the stock branch, but rooted at MP2 (the pend-side
                    # master alias). Without this, MERGE_RNG_SEG (and any other
                    # derived column) resolves to 'NA' for every pend row and
                    # the pend qty lands in a phantom 'NA' bucket — separate
                    # from the corresponding stock row — making PEND_ALC=0 in
                    # merge grids. The original expression uses [MP].[col] with
                    # bracket-quoted alias, so we replace both bracketed and
                    # unbracketed forms.
                    expr_mp2 = (_merge_case_cache[cu]
                                .replace("[MP].", "[MP2].")
                                .replace("MP.", "MP2."))
                    pend_hier_parts.append(f"ISNULL({expr_mp2}, {default}) AS [{col}]")
                elif cu in mp_cols_upper:
                    actual = mp_cols_upper[cu]
                    pend_hier_parts.append(
                        f"ISNULL(MP2.[{actual}], {default}) AS [{col}]"
                    )
                else:
                    pend_hier_parts.append(f"{default} AS [{col}]")
            pend_hier_select = ", ".join(pend_hier_parts)

            # Mirror MSA's _load_ars_pending filter (IS_CLOSED=0, PEND_QTY>0)
            # so grid and MSA read the same slice of ARS_PEND_ALC.
            # `__fine_matnr` is the article-grain key carried alongside the
            # grid's hierarchy columns so the PIVOT preserves per-article
            # rows; the FineStage CTE then clips STK_TTL at this grain and
            # the outer SELECT aggregates up to the grid's hier_cols.
            pend_union = f"""
    UNION ALL
    SELECT
        {pend_hier_select},
        CAST(PA.ARTICLE_NUMBER AS BIGINT) AS __fine_matnr,
        'PEND_ALC' AS SLOC,
        PA.PEND_QTY AS PARTICULARS_VALUE
    FROM dbo.ARS_PEND_ALC PA WITH (NOLOCK)
    LEFT JOIN dbo.vw_master_product MP2 ON PA.ARTICLE_NUMBER = MP2.ARTICLE_NUMBER
    WHERE PA.IS_CLOSED = 0
      AND PA.PEND_QTY  > 0
"""

        # ── Staged + chunked INSERT (avoids Azure SQL 9002 log-full) ────
        # The original code did a single `INSERT … SELECT … PIVOT` of up to
        # 9.85M rows, which was logged in one transaction in Rep_Data and
        # filled the per-tier log cap before COMMIT could free space.
        #
        # New flow:
        #   1. SELECT … PIVOT INTO #stage  — runs against tempdb, whose log
        #      is always SIMPLE recovery and doesn't count against Rep_Data.
        #   2. INSERT … SELECT FROM #stage WHERE __rn BETWEEN lo AND hi —
        #      committed every CHUNK_SIZE rows so the platform's auto-backup
        #      can clear log between batches.
        #   3. DROP #stage (auto-cleaned on session close anyway).
        # Each chunked INSERT goes through _retry_on_log_full so a transient
        # 9002 is recovered automatically without restarting the whole grid.

        chunk_size = max(10000, int(getattr(_settings, "GRID_INSERT_CHUNK_SIZE", 250000)))
        hier_cols_sql = ", ".join(f"[{c}]" for c in hier_cols)
        # Local temp table — bound to this session, auto-dropped at session end
        # if the explicit DROP below fails to run for any reason.
        stage_table = "#grid_stage_pivot"

        # The stage SQL is structured in 3 layers:
        #   Stock_CTE   — raw stock+pend rows, includes __fine_matnr so the
        #                 PIVOT preserves article granularity regardless of
        #                 the grid's hier_cols.
        #   FineStage   — PIVOTed at (hier_cols + __fine_matnr) grain;
        #                 STK_TTL is clipped to >= 0 HERE (per article) so
        #                 every grid aggregates the same already-non-negative
        #                 values → SUM(STK_TTL) parity across grids.
        #   Outer SELECT — aggregates SLOC columns and STK_TTL up to the
        #                 grid's own hier_cols grain. SLOC sums stay signed
        #                 (user instruction: clip only STK_TTL).
        stage_sql = f""";
WITH Stock_CTE AS (
    SELECT
        {hier_select},
        STK.MATNR AS __fine_matnr,
        STK.SLOC,
        STK.PARTICULARS_VALUE
    FROM dbo.ET_STORE_STOCK STK WITH (NOLOCK)
    {mp_join}
    INNER JOIN ARS_STORE_SLOC_SETTINGS S WITH (NOLOCK) ON STK.SLOC = S.SLOC
    WHERE UPPER(S.STATUS) = 'ACTIVE'{kpi_clause}
      AND STK.WERKS IS NOT NULL AND STK.WERKS <> ''
    {pend_union}
),
FineStage AS (
    SELECT
        {hier_cols_sql},
        __fine_matnr,
        {isnull_cols},
        {sum_expr} AS STK_TTL
    FROM Stock_CTE
    PIVOT (
        SUM(PARTICULARS_VALUE)
        FOR SLOC IN ({q_slocs})
    ) AS P
)
SELECT
    ROW_NUMBER() OVER (ORDER BY {hier_cols_sql}) AS __rn,
    {hier_cols_sql},
    {sum_isnull_cols},
    SUM(STK_TTL) AS STK_TTL
INTO {stage_table}
FROM FineStage
GROUP BY {hier_cols_sql};
"""

        t_stage_start = time.time()
        conn.execute(text(stage_sql))
        conn.commit()
        total_staged = conn.execute(
            text(f"SELECT COUNT(*) FROM {stage_table}")
        ).scalar() or 0
        logger.info(
            f"[grid {grid.get('id')}] staged {total_staged} rows into {stage_table} "
            f"in {time.time() - t_stage_start:.1f}s; chunking into {out_table} "
            f"at {chunk_size}/chunk"
        )

        try:
            stage_select_cols = f"{hier_cols_sql}, {q_slocs}, [STK_TTL]"
            inserted_so_far = 0
            chunk_idx = 0
            lo = 1
            while lo <= total_staged:
                hi = lo + chunk_size - 1
                chunk_idx += 1
                chunk_sql = (
                    f"INSERT INTO [{out_table}] ({all_cols}) "
                    f"SELECT {stage_select_cols} "
                    f"FROM {stage_table} "
                    f"WHERE __rn BETWEEN :lo AND :hi"
                )
                # Capture lo/hi by value for the closure
                _lo, _hi = lo, hi

                def _do_chunk():
                    # Explicit rollback first: if a previous attempt raised,
                    # SQLAlchemy 2.x leaves the connection in an aborted txn
                    # state and the next execute() would fail with
                    # "PendingRollbackError". Idempotent on a clean conn.
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    conn.execute(text(chunk_sql), {"lo": _lo, "hi": _hi})
                    conn.commit()

                _retry_on_log_full(
                    f"insert chunk={chunk_idx} grid={grid.get('id')} rows={_lo}-{_hi}",
                    _do_chunk,
                )
                inserted_so_far += min(chunk_size, total_staged - lo + 1)
                if chunk_idx == 1 or chunk_idx % 10 == 0 or hi >= total_staged:
                    logger.info(
                        f"[grid {grid.get('id')}] chunk {chunk_idx}: "
                        f"{inserted_so_far}/{total_staged} rows committed"
                    )
                lo += chunk_size
        finally:
            # Best-effort cleanup; SQL Server auto-drops on session close anyway.
            try:
                conn.execute(text(f"IF OBJECT_ID('tempdb..{stage_table}') IS NOT NULL DROP TABLE {stage_table}"))
                conn.commit()
            except Exception as e:
                logger.warning(f"[grid {grid.get('id')}] could not drop {stage_table}: {e}")

        # ── 5.5. Post-pivot synthetic-row injection ─────────────────────
        # Fill (WERKS, MAJ_CAT) pairs that exist in the intended-dispatch
        # universe (ARS_MSA_TOTAL × Master_ALC_INPUT_ST_MASTER) but are
        # missing from the just-built grid because ET_STORE_STOCK had no
        # rows for them yet. Handles new-MAJ_CAT and new-store cases in one
        # pass. Cheap because NOT EXISTS hits the grid table, not the
        # 100M-row stock table. Runs BEFORE post-lookups so LISTING / CONT
        # / MBQ / OPT_CNT calculations apply to synthetic rows identically.
        #
        # Skipped for pivot_only grids: ARS_MSA_TOTAL is at MAJ_CAT grain,
        # so it can't produce meaningful article-grain rows for
        # GEN_ART / VAR_ART pivots — it would only inject (NA, NA)
        # placeholders that pollute the output.
        if grid.get("pivot_only"):
            logger.info(
                f"[grid {grid.get('id')}] pivot_only — skipping synthetic-row "
                f"injection (MSA universe is at MAJ_CAT grain)"
            )
        else:
            try:
                t_syn = time.time()
                synthetic_n = _insert_missing_msa_rows(
                    conn, out_table, hier_cols, slocs,
                    numeric_hier, mp_cols_upper, _merge_case_cache,
                )
                if synthetic_n:
                    logger.info(
                        f"[grid {grid.get('id')}] post-pivot synthetic rows: "
                        f"{synthetic_n} added in {time.time() - t_syn:.1f}s "
                        f"(MSA × ST_MASTER for missing (WERKS, MAJ_CAT) pairs)"
                    )
            except Exception as e:
                logger.warning(
                    f"[grid {grid.get('id')}] synthetic-row INSERT skipped: {e}"
                )

        # ── 6. Post-pivot lookups & 7. MBQ/OPT_CNT ──────────────────────
        # GEN_ART and VAR_ART grids: skip CONT lookup + MBQ/OPT_CNT
        # (article-level grids only need LISTING filter + calc columns)
        is_article_grid = any(c in hier_cols for c in ["GEN_ART_NUMBER", "ARTICLE_NUMBER", "GEN_ART", "VAR_ART"])
        lookup_warnings = []
        if grid.get("pivot_only"):
            # Pivot-only grids (GEN_ART, VAR_ART) skip CONT/MBQ/OPT_CNT
            # because those calculations aren't meaningful at article grain.
            # BUT they MUST still apply the LISTING filter — otherwise
            # unlisted warehouses (rows not in Master_ALC_INPUT_ST_MASTER)
            # survive here and SUM(STK_TTL) inflates vs the rollup grids
            # which do filter.
            logger.info(
                f"Pivot-only mode for {out_table}: applying filters only "
                f"(LISTING), skipping CONT/MBQ/OPT_CNT"
            )
            lookup_warnings = _apply_post_lookups(
                conn, out_table, hier_cols, filters_only=True
            )
        elif is_article_grid:
            logger.info(f"Article-level grid: applying lookups but skipping CONT/MBQ/OPT_CNT for {out_table}")
            lookup_warnings = _apply_post_lookups(conn, out_table, hier_cols, skip_cont=True)
        else:
            lookup_warnings = _apply_post_lookups(conn, out_table, hier_cols)
            grid_calc_warnings = _calculate_grid_columns(conn, out_table)
            lookup_warnings.extend(grid_calc_warnings)

        # Count inserted rows
        count_row = conn.execute(text(f"SELECT COUNT(*) FROM [{out_table}]")).fetchone()
        row_count = count_row[0] if count_row else 0

        # ── 8. Fill NULLs in hierarchy columns + add primary key ──────
        try:
            pk_name = f"PK_{out_table}"

            # Drop existing PK if any
            _run(conn, f"""
                IF EXISTS (SELECT 1 FROM sys.key_constraints WHERE name='{pk_name}')
                    ALTER TABLE [{out_table}] DROP CONSTRAINT [{pk_name}]
            """)

            # Fill NULLs: 0 for numeric columns, 'NA' for text columns
            for c in hier_cols:
                if c.upper() in numeric_hier:
                    _run(conn, f"UPDATE [{out_table}] SET [{c}] = 0 WHERE [{c}] IS NULL")
                else:
                    _run(conn, f"UPDATE [{out_table}] SET [{c}] = 'NA' WHERE [{c}] IS NULL")
            logger.info(f"PK prep: filled NULLs in hierarchy cols (numeric→0, text→'NA')")

            # Delete duplicate rows (keep first occurrence per hierarchy key)
            pk_cols_str = ", ".join(f"[{c}]" for c in hier_cols)
            before = conn.execute(text(f"SELECT COUNT(*) FROM [{out_table}]")).scalar()
            _run(conn, f"""
                ;WITH CTE AS (
                    SELECT *, ROW_NUMBER() OVER (PARTITION BY {pk_cols_str} ORDER BY [STK_TTL] DESC) AS rn
                    FROM [{out_table}]
                )
                DELETE FROM CTE WHERE rn > 1
            """)
            after = conn.execute(text(f"SELECT COUNT(*) FROM [{out_table}]")).scalar()
            if before != after:
                logger.info(f"PK prep: removed {before - after} duplicate rows (kept highest STK_TTL)")

            # Make hierarchy columns NOT NULL for PK
            for c in hier_cols:
                ctype = "BIGINT" if c.upper() in numeric_hier else "NVARCHAR(200)"
                _run(conn, f"ALTER TABLE [{out_table}] ALTER COLUMN [{c}] {ctype} NOT NULL")

            _run(conn, f"ALTER TABLE [{out_table}] ADD CONSTRAINT [{pk_name}] PRIMARY KEY ({pk_cols_str})")

            row_count = conn.execute(text(f"SELECT COUNT(*) FROM [{out_table}]")).scalar()
            logger.info(f"PK created on {out_table}: ({pk_cols_str}), {row_count} rows")
        except Exception as e:
            lookup_warnings.append(f"PK creation skipped: {str(e)[:100]}")
            logger.warning(f"Could not create PK on {out_table}: {e}")

    return {"rows": row_count, "error": None, "warnings": lookup_warnings}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/columns", response_model=APIResponse)
def get_columns(current_user: User = Depends(get_current_user)):
    """Return available columns from vw_master_product for hierarchy selection."""
    de = get_data_engine()
    cols = _get_master_product_columns(de)

    # Always include fallback columns even if view missing
    fallback = ["MATNR", "WERKS"]
    all_cols = list(dict.fromkeys(fallback + cols))  # deduplicate, preserve order

    return APIResponse(success=True, message=f"{len(all_cols)} columns available",
                       data={"columns": all_cols})


@router.get("/grids", response_model=APIResponse)
def list_grids(current_user: User = Depends(get_current_user)):
    de = get_data_engine()
    _ensure_grid_table(de)
    with de.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT id, grid_name, description, hierarchy_columns,
                   kpi_filter, output_table, status, seq,
                   created_at, updated_at,
                   last_run_at, last_run_status, last_run_rows, last_run_error, duration_sec, pivot_only, weightage, grid_group, use_for_opt_sale, sec_cap_applicable, sec_cap_pct
            FROM {GRID_TABLE}
            ORDER BY seq ASC, id ASC
        """)).fetchall()
    grids = [_row_to_dict(r) for r in rows]
    return APIResponse(success=True, message=f"{len(grids)} grid(s) found",
                       data={"grids": grids, "total": len(grids)})


def _validate_lookups(conn, hier_cols: List[str]) -> List[str]:
    """Check if all required lookup tables exist for the given hierarchy. Returns warnings."""
    warnings = []
    for cfg in POST_PIVOT_LOOKUPS:
        if not all(r in {c.upper() for c in hier_cols} for r in cfg["requires"]):
            continue
        lookup_table = _resolve_template(cfg["lookup_table"], hier_cols)
        exists = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :tn"
        ), {"tn": lookup_table}).scalar() > 0
        if not exists:
            warnings.append(f"Lookup table '{lookup_table}' not found in DB")
            if "Master_CONT_" in cfg.get("lookup_table", ""):
                warnings.append(f"Contribution data will default to 1 ('{lookup_table}' missing)")
    return warnings


@router.post("/grids", response_model=APIResponse)
def create_grid(payload: GridCreate, current_user: User = Depends(get_current_user)):
    de = get_data_engine()
    _ensure_grid_table(de)
    hier_json = json.dumps(payload.hierarchy_columns)
    with de.connect() as conn:
        # Reject MERGE_<X> grids unless an Active parent grid exists
        _ensure_merge_parent_grid_exists(conn, payload.hierarchy_columns)
        # Validate lookup tables for this hierarchy
        warnings = _validate_lookups(conn, payload.hierarchy_columns)
        warn_msg = ("⚠ " + "; ".join(warnings)) if warnings else None

        # Auto-assign next sequence
        max_seq = conn.execute(text(f"SELECT ISNULL(MAX(seq),0) FROM {GRID_TABLE}")).scalar() or 0
        # If this grid is flagged use_for_opt_sale, unset any existing flag first
        if payload.use_for_opt_sale:
            conn.execute(text(f"UPDATE {GRID_TABLE} SET use_for_opt_sale = 0 WHERE ISNULL(use_for_opt_sale,0) = 1"))
        # Sec-cap applicability — only meaningful for Secondary, non-pivot grids.
        # Force OFF for Primary, None, or pivot_only grids regardless of what the
        # client sent.
        _grp = (payload.grid_group or "Primary").strip()
        _sec_cap_app = bool(
            payload.sec_cap_applicable
            and _grp.lower() == "secondary"
            and not payload.pivot_only
        )
        _sec_cap_pct = payload.sec_cap_pct if _sec_cap_app else None
        conn.execute(text(f"""
            INSERT INTO {GRID_TABLE}
                (grid_name, description, hierarchy_columns, kpi_filter, output_table, status, seq,
                 last_run_error, pivot_only, weightage, grid_group, use_for_opt_sale,
                 sec_cap_applicable, sec_cap_pct, created_at, updated_at)
            VALUES
                (:name, :desc, :hier, :kpi, :out, :status, :seq,
                 :warn, :ponly, :wt, :grp, :uos,
                 :sca, :scp, GETDATE(), GETDATE())
        """), {
            "name":   payload.grid_name,
            "desc":   payload.description,
            "hier":   hier_json,
            "kpi":    payload.kpi_filter,
            "out":    payload.output_table,
            "status": payload.status,
            "seq":    max_seq + 1,
            "warn":   warn_msg,
            "ponly":  1 if payload.pivot_only else 0,
            "wt":    payload.weightage or 1.0,
            "grp":   payload.grid_group or "Primary",
            "uos":   1 if payload.use_for_opt_sale else 0,
            "sca":   1 if _sec_cap_app else 0,
            "scp":   _sec_cap_pct,
        })
        conn.commit()
    _ensure_hierarchy_table(de)  # sync hierarchy table after new grid
    _populate_merge_columns(de)  # re-derive MERGE_<X> columns from parent + ARS_MERGE_RULES
    return APIResponse(success=True, message=f"Grid '{payload.grid_name}' created.",
                       data={"grid_name": payload.grid_name, "warnings": warnings})


@router.put("/grids/{grid_id}", response_model=APIResponse)
def update_grid(grid_id: int, payload: GridUpdate, current_user: User = Depends(get_current_user)):
    de = get_data_engine()
    _ensure_grid_table(de)

    # Build dynamic SET clause from non-None fields
    sets, params = [], {"id": grid_id}
    if payload.grid_name         is not None: sets.append("grid_name=:grid_name");         params["grid_name"]         = payload.grid_name
    if payload.description       is not None: sets.append("description=:description");     params["description"]       = payload.description
    if payload.hierarchy_columns is not None: sets.append("hierarchy_columns=:hier");      params["hier"]              = json.dumps(payload.hierarchy_columns)
    if payload.kpi_filter        is not None: sets.append("kpi_filter=:kpi_filter");       params["kpi_filter"]        = payload.kpi_filter
    if payload.output_table      is not None: sets.append("output_table=:output_table");   params["output_table"]      = payload.output_table.upper()
    if payload.status            is not None: sets.append("status=:status");               params["status"]            = payload.status
    if payload.pivot_only        is not None: sets.append("pivot_only=:ponly");             params["ponly"]             = 1 if payload.pivot_only else 0
    if payload.weightage         is not None: sets.append("weightage=:wt");               params["wt"]                = payload.weightage
    if payload.grid_group        is not None: sets.append("grid_group=:grp");             params["grp"]               = payload.grid_group
    if payload.use_for_opt_sale  is not None: sets.append("use_for_opt_sale=:uos");       params["uos"]               = 1 if payload.use_for_opt_sale else 0
    if payload.sec_cap_applicable is not None: sets.append("sec_cap_applicable=:sca");    params["sca"]               = 1 if payload.sec_cap_applicable else 0
    if payload.sec_cap_pct       is not None: sets.append("sec_cap_pct=:scp");            params["scp"]               = payload.sec_cap_pct
    if not sets:
        raise HTTPException(400, "No fields to update")
    sets.append("updated_at=GETDATE()")

    # Re-validate lookups if hierarchy changed
    hier_cols = payload.hierarchy_columns
    warnings = []
    if hier_cols:
        with de.connect() as conn:
            _ensure_merge_parent_grid_exists(conn, hier_cols)
            warnings = _validate_lookups(conn, hier_cols)
            warn_msg = ("⚠ " + "; ".join(warnings)) if warnings else None
            sets.append("last_run_error=:warn")
            params["warn"] = warn_msg

    with de.connect() as conn:
        # Enforce "only one grid with use_for_opt_sale = 1"
        if payload.use_for_opt_sale is True:
            conn.execute(text(f"UPDATE {GRID_TABLE} SET use_for_opt_sale = 0 WHERE id <> :id AND ISNULL(use_for_opt_sale,0) = 1"), {"id": grid_id})
        conn.execute(text(f"UPDATE {GRID_TABLE} SET {', '.join(sets)} WHERE id=:id"), params)
        # Sec-cap only meaningful for non-pivot Secondary grids — auto-zero
        # the flag (and clear any leftover pct) whenever the row no longer
        # qualifies, regardless of which fields the client sent.
        conn.execute(text(f"""
            UPDATE {GRID_TABLE}
               SET sec_cap_applicable = 0,
                   sec_cap_pct        = NULL
             WHERE id = :id
               AND (UPPER(ISNULL(grid_group,'Primary')) <> 'SECONDARY'
                    OR ISNULL(pivot_only, 0) = 1)
               AND (ISNULL(sec_cap_applicable, 0) = 1 OR sec_cap_pct IS NOT NULL)
        """), {"id": grid_id})
        conn.commit()
    _ensure_hierarchy_table(de)  # sync hierarchy table after update
    _populate_merge_columns(de)  # re-derive MERGE_<X> columns from parent + ARS_MERGE_RULES
    return APIResponse(success=True, message=f"Grid {grid_id} updated.", data={"id": grid_id, "warnings": warnings})


@router.delete("/grids/{grid_id}", response_model=APIResponse)
def delete_grid(grid_id: int, current_user: User = Depends(get_current_user)):
    de = get_data_engine()
    _ensure_grid_table(de)

    # Fetch the grid to get its output_table name
    with de.connect() as conn:
        row = conn.execute(text(f"SELECT output_table FROM {GRID_TABLE} WHERE id=:id"),
                           {"id": grid_id}).fetchone()
    if not row:
        raise HTTPException(404, f"Grid {grid_id} not found")

    out_table = row[0]

    with de.connect() as conn:
        # Drop the output table if it exists
        conn.execute(text(f"IF OBJECT_ID(:tbl, 'U') IS NOT NULL DROP TABLE [{out_table}]"),
                     {"tbl": out_table})
        # Delete the grid record
        conn.execute(text(f"DELETE FROM {GRID_TABLE} WHERE id=:id"), {"id": grid_id})
        conn.commit()

    _ensure_hierarchy_table(de)  # sync hierarchy table after delete
    _populate_merge_columns(de)  # re-derive MERGE_<X> columns from parent + ARS_MERGE_RULES
    return APIResponse(success=True,
        message=f"Grid {grid_id} and table [{out_table}] deleted.",
        data={"id": grid_id, "dropped_table": out_table})


@router.post("/grids/{grid_id}/run", response_model=APIResponse)
def run_grid(grid_id: int, current_user: User = Depends(get_current_user)):
    de = get_data_engine()
    _ensure_grid_table(de)

    with de.connect() as conn:
        row = conn.execute(text(f"""
            SELECT id, grid_name, description, hierarchy_columns,
                   kpi_filter, output_table, status, seq,
                   created_at, updated_at,
                   last_run_at, last_run_status, last_run_rows, last_run_error, duration_sec, pivot_only, weightage, grid_group, use_for_opt_sale, sec_cap_applicable, sec_cap_pct
            FROM {GRID_TABLE} WHERE id=:id
        """), {"id": grid_id}).fetchone()

    if not row:
        raise HTTPException(404, f"Grid {grid_id} not found")

    grid = _row_to_dict(row)

    # Build calc table ONCE before grid run
    calc_warns, calc_duration = _build_calc_table_once()

    res = _run_single_grid(grid)
    all_warns = calc_warns + res.get("warnings", [])

    if res["status"] == "Failed":
        raise HTTPException(500, detail=f"Grid run failed: {res['error']}")

    return APIResponse(success=True,
        message=f"Grid '{grid['grid_name']}' ran in {res.get('duration',0)}s. {res['rows']} rows → [{grid['output_table']}].",
        data={"rows_inserted": res["rows"], "output_table": grid["output_table"], "status": res["status"], "warnings": all_warns, "duration": res.get("duration", 0)})


@router.get("/calculation-preview", response_model=APIResponse)
def preview_calculations(current_user: User = Depends(get_current_user)):
    """Run the pre-grid calculation and return step-by-step logs with timing."""
    _time = time
    de = get_data_engine()
    start = _time.time()
    with de.connect() as conn:
        steps = calculate_per_day_sale(conn)
    duration = round(_time.time() - start, 1)
    return APIResponse(success=True, message=f"{len(steps)} steps in {duration}s",
                       data={"steps": steps, "duration": duration})


@router.post("/build-calc-tables", response_model=APIResponse)
def build_calc_tables(current_user: User = Depends(get_current_user)):
    """Build ARS_CALC_ST_MAJ_CAT and ARS_CALC_ST_ART independently (no grid run)."""
    _time = time
    de = get_data_engine()
    start = _time.time()
    warnings = []
    try:
        with de.connect() as conn:
            steps = calculate_per_day_sale(conn)
            for s in steps:
                logger.info(f"[BuildCalc] {s['step']}: {s['detail']} ({s['status']})")
                if s["status"] == "error":
                    warnings.append(f"{s['step']}: {s['detail']}")
    except Exception as e:
        logger.error(f"Build calc tables failed: {e}")
        raise HTTPException(500, detail=f"Calc table build failed: {str(e)[:200]}")
    duration = round(_time.time() - start, 1)
    ok_count = sum(1 for s in steps if s["status"] == "ok")
    skip_count = sum(1 for s in steps if s["status"] == "skip")
    err_count = sum(1 for s in steps if s["status"] == "error")
    return APIResponse(
        success=True,
        message=f"Calc tables built in {duration}s — {ok_count} ok, {skip_count} skipped, {err_count} errors",
        data={"steps": steps, "duration": duration, "warnings": warnings})


@router.put("/reorder", response_model=APIResponse)
def reorder_grids(body: dict, current_user: User = Depends(get_current_user)):
    """Update sequence order for grids. Body: {sequence: [{id, seq}, ...]}"""
    seq_list = body.get("sequence", [])
    if not seq_list:
        raise HTTPException(400, detail="sequence list is required")
    de = get_data_engine()
    _ensure_grid_table(de)
    with de.connect() as conn:
        for item in seq_list:
            conn.execute(text(f"UPDATE {GRID_TABLE} SET seq=:seq, updated_at=GETDATE() WHERE id=:id"),
                         {"seq": item["seq"], "id": item["id"]})
        conn.commit()
    # Rebuild hierarchy table column order to match new seq
    _ensure_hierarchy_table(de)
    _populate_merge_columns(de)  # re-derive MERGE_<X> columns from parent + ARS_MERGE_RULES
    return APIResponse(success=True, message=f"Sequence updated for {len(seq_list)} grid(s)")


def _build_calc_table_once():
    """Build ARS_CALC_ST_MAJ_CAT once. Called before grid runs."""
    _time = time
    de = get_data_engine()
    warnings = []
    start = _time.time()
    try:
        with de.connect() as conn:
            steps = calculate_per_day_sale(conn)
            for s in steps:
                logger.info(f"[Calc] {s['step']}: {s['detail']} ({s['status']})")
                if s["status"] == "error":
                    warnings.append(f"{s['step']}: {s['detail']}")
    except Exception as e:
        logger.warning(f"Calc table build failed: {e}")
        warnings.append(f"Calc table: {e}")
    duration = round(_time.time() - start, 1)
    logger.info(f"Pre-grid calc completed in {duration}s")
    return warnings, duration


def _run_single_grid(grid: dict) -> dict:
    """Run a single grid — used by both individual run and parallel run-all."""
    _time = time
    de = get_data_engine()
    start = _time.time()

    # Mark running. Retry on 9002 — losing this update would leave the grid
    # stuck on whatever status it had before (often 'Running' from a prior
    # crashed run), and the user would have no signal that work has begun.
    def _mark_running():
        with de.connect() as conn:
            _run(conn, f"UPDATE {GRID_TABLE} SET last_run_status='Running', updated_at=GETDATE() WHERE id=:id",
                 {"id": grid["id"]})
    try:
        _retry_on_log_full(f"mark-running grid={grid['id']}", _mark_running)
    except Exception as e:
        # If the status update can't land even after retry, log and proceed —
        # the grid run itself may still succeed and the final-status UPDATE
        # below has its own retry. We don't want a status-write failure to
        # block doing the actual work.
        logger.warning(f"Could not set Running status for grid {grid['id']}: {e}")

    try:
        result = _build_and_run_grid(de, grid)
        status = "Success" if not result["error"] else "Failed"
        err_msg = result["error"]
        n_rows = result["rows"]
        warn_list = result.get("warnings", [])

    except Exception as e:
        status = "Failed"
        err_msg = str(e)
        n_rows = 0
        warn_list = []
        logger.error(f"Grid {grid['id']} failed: {e}")

    # Store warnings in last_run_error
    stored_msg = err_msg
    if not stored_msg and warn_list:
        stored_msg = "⚠ " + "; ".join(warn_list)

    duration = round(_time.time() - start, 1)

    # Final-status update — also retry on 9002. This is the write that was
    # failing in the production incident: the heavy INSERT had completed,
    # but the log filled before this tiny UPDATE could land, leaving every
    # grid showing the failing UPDATE as its error message.
    def _mark_final():
        with de.connect() as conn:
            _run(conn, f"""
                UPDATE {GRID_TABLE}
                SET last_run_at=GETDATE(), last_run_status=:status,
                    last_run_rows=:rows, last_run_error=:err, duration_sec=:dur, updated_at=GETDATE()
                WHERE id=:id
            """, {"status": status, "rows": n_rows, "err": stored_msg, "dur": duration, "id": grid["id"]})
    try:
        _retry_on_log_full(f"final-status grid={grid['id']}", _mark_final)
    except Exception as e:
        logger.error(f"Could not write final status for grid {grid['id']}: {e}")
    logger.info(f"Grid '{grid['grid_name']}' completed in {duration}s")

    return {"grid_name": grid["grid_name"], "status": status,
            "rows": n_rows, "error": err_msg, "warnings": warn_list, "duration": duration}


def _do_run_all_background(active_grids: List[dict], workers: int) -> None:
    """
    Heavy work for Run All — executed on a daemon thread so the HTTP handler
    can return immediately. Updates _run_all_state as it progresses; the UI
    polls /run-all/status (and /grids for per-grid detail).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    de = get_data_engine()
    try:
        # Calc table ONCE before all grids
        calc_warns, calc_duration = _build_calc_table_once()
        logger.info(f"Calc table built once for {len(active_grids)} grids (took {calc_duration}s)")
        with _run_all_lock:
            _run_all_state["calc_duration"] = calc_duration

        # Parallel grid run
        results: List[dict] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_single_grid, g): g for g in active_grids}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    g = futures[future]
                    results.append({"grid_name": g["grid_name"], "status": "Failed",
                                    "rows": 0, "error": str(e), "warnings": []})

        name_order = {g["grid_name"]: i for i, g in enumerate(active_grids)}
        results.sort(key=lambda r: name_order.get(r["grid_name"], 999))

        # MSA sync — keeps ARS_MSA_TOTAL/VAR_ART/GEN_ART consistent with
        # current ARS_PEND_ALC after the heavy rebuild.
        msa_sync: dict = {}
        try:
            from app.services.pend_alc_service import bootstrap_msa_pend_sync
            with de.connect() as _conn:
                msa_sync = bootstrap_msa_pend_sync(_conn)
            logger.info(
                f"Post-grid MSA bootstrap: total={msa_sync.get('msa_total', 0)} "
                f"var={msa_sync.get('msa_var_art', 0)} gen={msa_sync.get('msa_gen_art', 0)}"
            )
        except Exception as e:
            logger.warning(f"Post-grid bootstrap_msa_pend_sync failed (non-fatal): {e}")

        # NOTE: routine DBCC SHRINKFILE was removed here — it is an
        # anti-pattern in SQL Server (causes index fragmentation, and the
        # log just regrows on the next Run All). Size the log file once in
        # SQL config and let it stay sized. Run shrink manually only if a
        # one-off space crunch demands it.

        with _run_all_lock:
            _run_all_state["results"]  = results
            _run_all_state["msa_sync"] = msa_sync
        ok = sum(1 for r in results if r["status"] == "Success")
        logger.info(f"Run All background complete: {ok}/{len(results)} grids succeeded.")
    except Exception as e:
        logger.error(f"Run All background failed: {e}", exc_info=True)
        with _run_all_lock:
            _run_all_state["error"] = str(e)
    finally:
        with _run_all_lock:
            _run_all_state["running"]      = False
            _run_all_state["completed_at"] = time.time()


@router.post("/run-all", response_model=APIResponse)
def run_all_active(
    parallelism: Optional[int] = None,
    current_user: User = Depends(get_current_user),
):
    """
    Spawn a Run All in the background. Returns immediately (202-ish) so the
    HTTP request finishes well within Cloudflare's 120s proxy timeout. The UI
    polls /run-all/status and /grids to watch progress.

    `parallelism` (1..GRID_RUN_PARALLELISM_MAX) overrides the configured
    default for this invocation.
    """
    # Reject duplicate triggers — a previous Run All is still in progress.
    with _run_all_lock:
        if _run_all_state["running"]:
            elapsed = int(time.time() - (_run_all_state["started_at"] or time.time()))
            return APIResponse(
                success=False,
                message=(f"Run All already in progress ({elapsed}s elapsed, "
                         f"{_run_all_state.get('total_grids', 0)} grids). "
                         f"Wait for it to finish before triggering again."),
                data={"running": True, "elapsed_sec": elapsed,
                      "started_at": _run_all_state.get("started_at"),
                      "started_by": _run_all_state.get("started_by")},
            )

    de = get_data_engine()
    _ensure_grid_table(de)

    with de.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT id, grid_name, description, hierarchy_columns,
                   kpi_filter, output_table, status, seq,
                   created_at, updated_at,
                   last_run_at, last_run_status, last_run_rows, last_run_error, duration_sec, pivot_only, weightage, grid_group, use_for_opt_sale, sec_cap_applicable, sec_cap_pct
            FROM {GRID_TABLE} WHERE status='Active' ORDER BY seq ASC, id ASC
        """)).fetchall()

    active_grids = [_row_to_dict(r) for r in rows]
    if not active_grids:
        return APIResponse(success=True, message="No Active grids to run.",
                           data={"running": False, "results": []})

    cfg_default = max(1, int(getattr(_settings, "GRID_RUN_PARALLELISM", 4)))
    cap         = max(1, int(getattr(_settings, "GRID_RUN_PARALLELISM_MAX", 16)))
    requested   = parallelism if parallelism is not None else cfg_default
    workers     = max(1, min(int(requested), cap, len(active_grids)))

    username = getattr(current_user, "username", None) or getattr(current_user, "email", None) or "unknown"

    # Claim the lock and stash the new run's metadata BEFORE spawning the
    # thread, otherwise a second Run All click could slip through.
    with _run_all_lock:
        _run_all_state.update({
            "running":       True,
            "started_at":    time.time(),
            "started_by":    username,
            "completed_at":  None,
            "total_grids":   len(active_grids),
            "workers":       workers,
            "results":       [],
            "msa_sync":      {},
            "error":         None,
            "calc_duration": None,
        })

    threading.Thread(
        target=_do_run_all_background,
        args=(active_grids, workers),
        daemon=True,
        name=f"run-all-grids-{int(time.time())}",
    ).start()

    logger.info(
        f"Run All Active: launched in background — {len(active_grids)} grids, "
        f"parallelism={workers} (requested={requested}, default={cfg_default}, "
        f"cap={cap}), user={username}"
    )

    return APIResponse(
        success=True,
        message=(f"Run All started in background — {len(active_grids)} grids queued "
                 f"with {workers} parallel workers. Watch the table for per-grid status."),
        data={
            "running":      True,
            "total_grids":  len(active_grids),
            "workers":      workers,
            "started_at":   _run_all_state["started_at"],
            "started_by":   username,
            "poll_endpoint": "/api/v1/grid-builder/run-all/status",
        },
    )


@router.get("/run-all/status", response_model=APIResponse)
def run_all_active_status(current_user: User = Depends(get_current_user)):
    """
    Definitive "is a Run All in progress?" check. Frontend polls this to know
    when to stop its grid-status poll loop. Returns the same shape whether the
    background thread is mid-run, completed, or never started this session.
    """
    with _run_all_lock:
        state = dict(_run_all_state)

    now      = time.time()
    started  = state.get("started_at")
    finished = state.get("completed_at")

    if state.get("running"):
        elapsed = int(now - (started or now))
        return APIResponse(
            success=True,
            message=f"Run All in progress: {elapsed}s elapsed",
            data={
                "running":       True,
                "elapsed_sec":   elapsed,
                "started_at":    started,
                "started_by":    state.get("started_by"),
                "total_grids":   state.get("total_grids", 0),
                "workers":       state.get("workers", 0),
                "calc_duration": state.get("calc_duration"),
            },
        )

    last_duration = int(finished - started) if (started and finished) else None
    results       = state.get("results") or []
    ok            = sum(1 for r in results if r.get("status") == "Success")
    msg = ("No Run All currently in progress." if not finished
           else f"Last Run All completed: {ok}/{len(results)} grids in {last_duration}s")

    return APIResponse(
        success=True,
        message=msg,
        data={
            "running":           False,
            "last_started_at":   started,
            "last_completed_at": finished,
            "last_duration_sec": last_duration,
            "last_results":      results,
            "last_msa_sync":     state.get("msa_sync") or {},
            "last_error":        state.get("error"),
        },
    )


# ===========================================================================
# HIERARCHY TABLE — auto-managed from grid definitions
# ===========================================================================

@router.get("/hierarchy/schema", response_model=APIResponse)
def get_hierarchy_schema(current_user: User = Depends(get_current_user)):
    """Return the current ARS_GRID_HIERARCHY table structure (columns in seq order)."""
    de = get_data_engine()
    _ensure_grid_table(de)

    with de.connect() as conn:
        # Get grid → column mapping
        grids = conn.execute(text(f"""
            SELECT grid_name, hierarchy_columns, seq, status,
                   ISNULL(weightage, 1.0) AS weightage,
                   ISNULL(grid_group, 'None') AS grid_group
            FROM {GRID_TABLE}
            ORDER BY seq ASC, id ASC
        """)).fetchall()

        columns = []
        for gname, hier_json, seq, status, wt, grp in grids:
            try:
                hier = json.loads(hier_json) if isinstance(hier_json, str) else hier_json
            except Exception:
                continue
            if not hier or len(hier) < 2:
                continue
            if any(h.upper() in _ARTICLE_LEVEL_COLS for h in hier):
                continue
            last_col = hier[-1]
            if last_col.upper() in ("WERKS", "MAJ_CAT"):
                continue
            columns.append({
                "column_name": last_col.upper(),
                "grid_name": gname,
                "hierarchy": hier,
                "seq": seq,
                "status": status,
                "weightage": wt,
                "grid_group": grp,
            })

        # Check table status
        tbl_exists = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :t"
        ), {"t": GRID_HIER_TABLE}).scalar() > 0

        row_count = 0
        if tbl_exists:
            row_count = conn.execute(text(f"SELECT COUNT(*) FROM [{GRID_HIER_TABLE}]")).scalar()

    return APIResponse(
        success=True,
        message=f"{len(columns)} hierarchy columns from {len(grids)} grids",
        data={
            "table_name": GRID_HIER_TABLE,
            "table_exists": tbl_exists,
            "row_count": row_count,
            "base_columns": ["MAJ_CAT"],
            "grid_columns": columns,
        },
    )


@router.get("/hierarchy/data", response_model=APIResponse)
def get_hierarchy_data(
    page: int = 1,
    page_size: int = 100,
    current_user: User = Depends(get_current_user),
):
    """Read data from ARS_GRID_HIERARCHY with pagination."""
    de = get_data_engine()
    with de.connect() as conn:
        exists = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :t"
        ), {"t": GRID_HIER_TABLE}).scalar() > 0
        if not exists:
            raise HTTPException(404, f"{GRID_HIER_TABLE} does not exist yet. Run any grid first.")

        cols = [r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION"
        ), {"t": GRID_HIER_TABLE}).fetchall()]

        total = conn.execute(text(f"SELECT COUNT(*) FROM [{GRID_HIER_TABLE}]")).scalar()
        offset = (page - 1) * page_size
        col_list = ", ".join(f"[{c}]" for c in cols)
        rows = conn.execute(text(f"""
            SELECT {col_list} FROM [{GRID_HIER_TABLE}]
            ORDER BY [MAJ_CAT]
            OFFSET :off ROWS FETCH NEXT :ps ROWS ONLY
        """), {"off": offset, "ps": page_size}).fetchall()

        data = [dict(zip(cols, row)) for row in rows]

    return APIResponse(
        success=True,
        message=f"{len(data)} rows (page {page}, {total} total)",
        data={"columns": cols, "data": data, "total": total, "page": page, "page_size": page_size},
    )


@router.get("/hierarchy/gaps", response_model=APIResponse)
def get_hierarchy_gaps(current_user: User = Depends(get_current_user)):
    """
    Pre-flight check for Listing: report MAJ_CATs that exist in ARS_MSA_TOTAL
    but are missing from ARS_GRID_HIERARCHY (or present with NULL grid columns).

    Returns:
      expected: # of distinct MAJ_CATs in ARS_MSA_TOTAL
      covered:  # of MAJ_CATs that exist in ARS_GRID_HIERARCHY
      missing:  list of MAJ_CATs in MSA but not in hierarchy
      partial:  list of {maj_cat, null_cols[]} where hierarchy row has NULL grid cols
    """
    de = get_data_engine()
    with de.connect() as conn:
        hier_exists = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :t"
        ), {"t": GRID_HIER_TABLE}).scalar() > 0
        msa_exists = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'ARS_MSA_TOTAL'"
        )).scalar() > 0

        if not msa_exists:
            return APIResponse(success=True, message="ARS_MSA_TOTAL not found — skip check",
                data={"expected": 0, "covered": 0, "missing": [], "partial": [],
                      "hier_exists": hier_exists, "msa_exists": False})

        expected = conn.execute(text(
            "SELECT COUNT(DISTINCT MAJ_CAT) FROM ARS_MSA_TOTAL WHERE MAJ_CAT IS NOT NULL"
        )).scalar() or 0

        if not hier_exists:
            missing_rows = conn.execute(text(
                "SELECT DISTINCT MAJ_CAT FROM ARS_MSA_TOTAL "
                "WHERE MAJ_CAT IS NOT NULL ORDER BY MAJ_CAT"
            )).fetchall()
            return APIResponse(success=True,
                message=f"{GRID_HIER_TABLE} does not exist — all {expected} MAJ_CATs missing",
                data={"expected": expected, "covered": 0,
                      "missing": [r[0] for r in missing_rows], "partial": [],
                      "hier_exists": False, "msa_exists": True})

        covered = conn.execute(text(f"SELECT COUNT(*) FROM [{GRID_HIER_TABLE}]")).scalar() or 0

        # Anti-join: MSA MAJ_CATs missing from hierarchy
        missing_rows = conn.execute(text(f"""
            SELECT DISTINCT m.MAJ_CAT
            FROM ARS_MSA_TOTAL m
            LEFT JOIN [{GRID_HIER_TABLE}] h ON h.MAJ_CAT = m.MAJ_CAT
            WHERE m.MAJ_CAT IS NOT NULL AND h.MAJ_CAT IS NULL
            ORDER BY m.MAJ_CAT
        """)).fetchall()
        missing = [r[0] for r in missing_rows]

        # Partial-fill: MAJ_CATs in hierarchy where any grid column is NULL
        grid_cols = [r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = :t AND UPPER(COLUMN_NAME) <> 'MAJ_CAT' "
            "ORDER BY ORDINAL_POSITION"
        ), {"t": GRID_HIER_TABLE}).fetchall()]

        partial = []
        if grid_cols:
            null_expr = " OR ".join(f"[{c}] IS NULL" for c in grid_cols)
            null_col_exprs = ", ".join(
                f"CASE WHEN [{c}] IS NULL THEN '{c}' ELSE NULL END AS [_n_{c}]"
                for c in grid_cols
            )
            partial_rows = conn.execute(text(f"""
                SELECT MAJ_CAT, {null_col_exprs}
                FROM [{GRID_HIER_TABLE}]
                WHERE {null_expr}
                ORDER BY MAJ_CAT
            """)).fetchall()
            for row in partial_rows:
                maj = row[0]
                nulls = [v for v in row[1:] if v]
                partial.append({"maj_cat": maj, "null_cols": nulls})

    return APIResponse(
        success=True,
        message=f"{len(missing)} missing, {len(partial)} partial of {expected} MSA MAJ_CATs",
        data={
            "expected": expected,
            "covered": covered,
            "missing": missing,
            "partial": partial,
            "hier_exists": True,
            "msa_exists": True,
        },
    )


@router.post("/hierarchy/compact", response_model=APIResponse)
def compact_hierarchy(
    dry_run: bool = True,
    current_user: User = Depends(get_current_user),
):
    """
    Explicit prune: drop ARS_GRID_HIERARCHY columns that no longer correspond
    to an Active grid. Use this after deleting / deactivating grids when you
    want to reclaim the column (and its data) from the hierarchy table.

    Routine grid CRUD does NOT call this — orphan columns are kept by default
    so deletes / deactivations are reversible. Operators must run /compact
    deliberately to drop a column for good.

    Args:
        dry_run: if True (default), report what WOULD be dropped without
                 actually altering the table. Pass dry_run=false to execute.

    Returns:
        kept:    columns still in use by Active grids
        orphans: columns that would be / were dropped
        dropped: columns actually dropped (empty unless dry_run=false)
    """
    from app.services import derived_masters as dm

    de = get_data_engine()
    _ensure_grid_table(de)

    with de.connect() as conn:
        tbl_exists = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :t"
        ), {"t": GRID_HIER_TABLE}).scalar() > 0
        if not tbl_exists:
            return APIResponse(success=True,
                message=f"{GRID_HIER_TABLE} does not exist — nothing to compact",
                data={"kept": [], "orphans": [], "dropped": [], "dry_run": dry_run})

        # Build expected column set from Active grids (same rules as
        # _ensure_hierarchy_table).
        grids = conn.execute(text(f"""
            SELECT hierarchy_columns FROM {GRID_TABLE}
            WHERE UPPER(status) = 'ACTIVE'
        """)).fetchall()
        expected = {"MAJ_CAT"}
        for (hj,) in grids:
            try:
                h = json.loads(hj) if isinstance(hj, str) else hj
            except Exception:
                continue
            if not h or len(h) < 2:
                continue
            if any(str(c).upper() in _ARTICLE_LEVEL_COLS for c in h):
                continue
            last = str(h[-1]).upper()
            if last in ("WERKS", "MAJ_CAT"):
                continue
            expected.add(last)

        existing = [r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION"
        ), {"t": GRID_HIER_TABLE}).fetchall()]

        # A MERGE_<X> column is an orphan when X itself is not in expected.
        orphans = []
        kept = []
        for col in existing:
            cu = col.upper()
            if cu == "MAJ_CAT":
                kept.append(col)
                continue
            if cu.startswith(dm.MERGE_COL_PREFIX):
                parent = cu[len(dm.MERGE_COL_PREFIX):]
                if parent in expected:
                    kept.append(col)
                else:
                    orphans.append(col)
            else:
                if cu in expected:
                    kept.append(col)
                else:
                    orphans.append(col)

        dropped = []
        if not dry_run and orphans:
            for col in orphans:
                try:
                    _run(conn, f'ALTER TABLE [{GRID_HIER_TABLE}] DROP COLUMN [{col}]')
                    dropped.append(col)
                except Exception as e:
                    logger.warning(f"compact: could not drop [{col}]: {e}")
            logger.info(
                f"{GRID_HIER_TABLE}: compacted — dropped {len(dropped)} orphan "
                f"column(s) {dropped}"
            )

    msg = (
        f"{len(orphans)} orphan column(s) — dry-run (pass dry_run=false to execute)"
        if dry_run else f"dropped {len(dropped)} orphan column(s)"
    )
    return APIResponse(success=True, message=msg, data={
        "kept": kept,
        "orphans": orphans,
        "dropped": dropped,
        "dry_run": dry_run,
    })
