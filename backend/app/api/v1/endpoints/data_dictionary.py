"""
Data Dictionary API
===================
CRUD on ARS_DATA_DICTIONARY — a reference of every important column:
its abbreviation/full form, purpose in plain words, the tables it lives in,
and the algorithm/formula behind it. Backs the Data Dictionary UI page so
any user can look up what a column means.

Endpoints:
  GET    /data-dictionary            – list entries (optional ?q= search)
  POST   /data-dictionary            – create one entry
  PUT    /data-dictionary/{entry_id} – update one entry
  DELETE /data-dictionary/{entry_id} – delete one entry
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from loguru import logger

from app.database.session import get_data_engine
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.models.rbac import User


router = APIRouter(prefix="/data-dictionary", tags=["Data Dictionary"])

TABLE = "ARS_DATA_DICTIONARY"


# ── Schemas ─────────────────────────────────────────────────────────────────
class DictEntryPayload(BaseModel):
    column_name:    str
    abbreviation:   Optional[str] = None   # full form / expansion of the name
    purpose:        Optional[str] = None   # what it means, in plain words
    related_tables: Optional[str] = None   # comma-separated table names
    formula:        Optional[str] = None   # algorithm / formula, if derived
    module:         Optional[str] = None   # pipeline area: MSA, Grid, Listing…


class DictEntryUpdate(BaseModel):
    column_name:    Optional[str] = None
    abbreviation:   Optional[str] = None
    purpose:        Optional[str] = None
    related_tables: Optional[str] = None
    formula:        Optional[str] = None
    module:         Optional[str] = None


# ── Seed rows — the core ARS vocabulary, loaded once on first use ───────────
SEED = [
    # (column, abbreviation/full form, purpose, related tables, formula, module)
    ("FNL_Q", "Final Quantity", "Free-to-allocate stock per option per RDC. The single number every later step splits.",
     "ARS_MSA_GEN_ART, ARS_MSA_TOTAL, ARS_MSA_VAR_ART", "FNL_Q = MAX(STK_QTY − PEND_QTY, 0)", "MSA"),
    ("STK_QTY", "Stock Quantity", "Raw warehouse stock counted from allowed SLOCs before pending is subtracted.",
     "et_msa_stk, ARS_MSA_GEN_ART", "SUM(stock over allowed SLOCs via VW_ET_MSA_STK_WITH_MASTER)", "MSA"),
    ("PEND_QTY", "Pending Quantity", "Pieces already promised to stores by earlier runs but not yet delivered.",
     "MASTER_ALC_PEND, ARS_MSA_GEN_ART", "SUM(open pendings per article × RDC)", "MSA"),
    ("ARS_PEND", "ARS Pending", "Pending created by ARS runs themselves (subset of PEND_QTY).",
     "MASTER_ALC_PEND", None, "MSA"),
    ("SEG", "Segment", "Top split of merchandise: APP (apparel) or GM (general merchandise).",
     "ARS_MSA_GEN_ART, vw_master_product", None, "Master"),
    ("DIV", "Division", "Merchandise division, e.g. MENS, LADIES, HH.",
     "ARS_MSA_GEN_ART, vw_master_product", None, "Master"),
    ("SUB_DIV", "Sub-Division", "Division subdivision, e.g. MU (mens upper).",
     "ARS_MSA_GEN_ART, vw_master_product", None, "Master"),
    ("MAJ_CAT", "Major Category", "The planning unit — budgets, grids and runs are per MAJ_CAT (e.g. M_TEES_HS).",
     "ARS_MSA_GEN_ART, ARS_GRID_HIERARCHY, ARS_LISTING", None, "Master"),
    ("SSN", "Season", "Season tag of the article: A, NA, OC, PW, S, SSNL, W.",
     "ARS_MSA_GEN_ART, vw_master_product", None, "Master"),
    ("GEN_ART_NUMBER", "Generic Article Number", "Style-level article id before colour/size split.",
     "ARS_MSA_GEN_ART, retail_gen_article", None, "Master"),
    ("CLR", "Colour", "Colour of the generic article. One OPT = GEN_ART × CLR at one store.",
     "ARS_MSA_GEN_ART, ARS_LISTING", None, "Master"),
    ("WERKS", "Plant / Store code (SAP term)", "The store the row belongs to. Same as ST_CD in master tables.",
     "ARS_LISTING, ARS_ALLOC_WORKING, Master_ALC_INPUT_ST_MASTER", None, "Master"),
    ("RDC", "Regional Distribution Centre", "Warehouse that serves the store. MSA output ST_CD column was renamed RDC.",
     "Master_ALC_INPUT_ST_MASTER, ARS_MSA_GEN_ART", None, "Master"),
    ("HUB", "Hub", "Delivery hub between RDC and stores; used for grouping and truck planning.",
     "Master_ALC_INPUT_ST_MASTER", None, "Master"),
    ("MANUAL_ST_PRIORITY", "Manual Store Priority", "Optional manual pin for a store's ST_RANK. Positive integer P forces the store to ST_RANK=P inside every MAJ_CAT it is listed in; blank/0/negative = ranked by W_SCORE. Non-manual stores keep score order but skip pinned slots (per MAJ_CAT).",
     "Master_ALC_INPUT_ST_MASTER", "P pins ST_RANK; else auto by W_SCORE", "Listing"),
    ("AUTO_ST_RANK", "Auto Store Rank (score-only)", "The store's rank by W_SCORE alone, ignoring manual pins — the ST_RANK it would get with no override. Audit column for comparing against the final ST_RANK; never read by the allocation engine.",
     "ARS_STORE_RANKING", "ROW_NUMBER by W_SCORE DESC, WERKS ASC (per MAJ_CAT)", "Listing"),
    ("RANK_DELTA", "Rank Delta", "AUTO_ST_RANK − ST_RANK. Positive = the pin promoted the store; negative = pushed down by another store's pin; 0 = unaffected.",
     "ARS_STORE_RANKING", "AUTO_ST_RANK − ST_RANK", "Listing"),
    ("OPT_TYPE", "Option Type", "Per-run classification of an option at a store: RL (refill live), TBC (continue), TBL (new listing). Mutually exclusive per (WERKS, MAJ_CAT, GEN_ART, CLR).",
     "ARS_LISTING, ARS_ALLOC_WORKING", "STK ≥ Stock% × ACS_D → RL, else TBC/TBL by history", "Listing"),
    ("ACS_D", "Accessories Density (display quantity)", "How many pieces ONE option occupies on the store floor (default 18). Used as the classification yardstick — it is NOT a daily sales number (velocity uses MAX_DAILY_SALE).",
     "ARS_LISTING settings", "RL if store stock ≥ Stock% × ACS_D", "Listing"),
    ("RNG_SEG", "Range Segment (MRP tier)", "Price tier of the option: E (economy), V (value), P (premium), SP (special). The primary grid MJ_RNG_SEG budgets per tier.",
     "ARS_GRID_HIERARCHY, ARS_GRID_MJ_*", None, "Grid"),
    ("MERGE_RNG_SEG", "Merged Range Segment", "Combined tier after Merge Rules (e.g. E+V → EV) so budgets are checked on one bucket.",
     "ARS_GRID_HIERARCHY, ARS_MERGE_RULES", "Mapping maintained in Merge Rules screen", "Grid"),
    ("MJ_MBQ", "Major-category Minimum Budget Quantity", "How many pieces a store should hold for a MAJ_CAT (per grid key). 0 = no budget set = no constraint, never 'ship zero'.",
     "ARS_GRID_MJ_GEN_ART", "Built from sales history in Grid Builder", "Grid"),
    ("MJ_REQ", "Major-category Requirement", "Pieces the store still needs today. The allocation walk consumes this per store × MAJ_CAT.",
     "ARS_GRID_MJ_GEN_ART, ARS_ALLOC_WORKING", "MJ_REQ = MAX(MJ_MBQ − store stock, 0)", "Grid"),
    ("MBQ_REV / REQ_REV", "Revised MBQ / Requirement", "Growth-lifted budget used by the run. Reads *_MBQ_ORIG so re-runs never compound growth.",
     "ARS_GRID_* tables", "MBQ_REV = MBQ_ORIG × Growth% ; REQ_REV = MAX(0, MBQ_REV − STK_TTL)", "Grid"),
    ("OPT_MBQ", "Option MBQ", "Pieces one option needs to make a complete display/ratio at a store.",
     "ARS_LISTING", "Σ SZ_MBQ over the option's sizes", "Listing"),
    ("SZ_MBQ", "Size MBQ", "Pieces per size within an option's display ratio (e.g. S3-M2-L2-XL1).",
     "ARS_LISTING, ARS_ALLOC_WORKING", "From size contribution (CONT) × OPT_MBQ", "Listing"),
    ("PAK_SZ", "Pak Size", "Warehouse pack multiple — shipments round to whole paks.",
     "ARS_MSA_GEN_ART", "SHIP_QTY rounds to n × PAK_SZ (cap: SZ_MBQ × I_ROD)", "Allocation"),
    ("STOCK_CONSIDER_DT", "Stock Consider Date", "Information-only run date: the business date the stock figures the run was based on reflect (mandatory; UI defaults to D-1 / yesterday). Stamped on every allocation output row; does NOT enter any stock/MSA/allocation calculation.",
     "ARS_ALLOC_WORKING, ARS_ALLOC_PARKED, ARS_ALLOC_HISTORY", "User-entered at run time; constant across the run's rows", "Allocation"),
    ("PICKING_DT", "Picking Date", "Information-only run date: the planned warehouse picking / dispatch date (mandatory since 2026-07-18; UI leaves it blank so the user picks it consciously each run). Stamped on every allocation output row; does NOT enter any calculation.",
     "ARS_ALLOC_WORKING, ARS_ALLOC_PARKED, ARS_ALLOC_HISTORY", "User-entered at run time; constant across the run's rows", "Allocation"),
    ("CONT", "Contribution %", "Share of each size within an option; always sums to 1 per option. For SZ_APPLICABLE='N' categories the fallback fill is Uniform 1/N or stock-based.",
     "Cont_presets, ARS_ALLOC_WORKING", "CONT ladder: Site → CO → fallback (P4_UNIFORM / P3_FNL_Q)", "Allocation"),
    ("SHIP_QTY", "Ship Quantity", "Pieces the run decided to send to the store (per size / rolled up).",
     "ARS_ALLOC_WORKING, alloc detail history", "Waterfall result after budgets, sec-cap, pak rounding", "Allocation"),
    ("HOLD_QTY", "Hold Quantity", "Pieces reserved at the RDC instead of shipped (NL/TBL ramp-up, hold window). Subtracted from free stock but no delivery order yet.",
     "ARS_ALLOC_WORKING, hold reports", None, "Allocation"),
    ("ALLOC_STATUS", "Allocation Status", "Outcome per row: ALLOCATED, PARTIAL, NOT_ALLOCATED, INELIGIBLE — with honest skip reasons (MBQ_CAP_*, POOL_EMPTY, R07_SIZE_RATIO_LIVE …).",
     "ARS_ALLOC_WORKING", None, "Allocation"),
    ("PRI_CT", "Priority Count coverage %", "How complete the store's priority assortment is. PRI ≥ 100% toggles gate RL/TBC listing on full coverage.",
     "ARS_LISTING", None, "Listing"),
    ("FAB / MICRO_MVGR / M_VND_CD", "Fabric / Micro merchandise group / Vendor code", "Secondary-grid (sec-cap) dimensions — fences so one fabric/group/vendor cannot eat the whole MAJ_CAT budget. Must flow listing → alloc or the fence silently disappears.",
     "ARS_GRID_FAB, ARS_GRID_MICRO_MVGR, ARS_GRID_M_VND_CD", "cap = MAX(0, grid MBQ_ORIG × cap% − STK_TTL); value 0 = no fence", "Grid"),
]


# ── Helpers ─────────────────────────────────────────────────────────────────
def _ensure_table(conn) -> None:
    conn.execute(text(f"""
        IF OBJECT_ID('{TABLE}', 'U') IS NULL
        CREATE TABLE [{TABLE}] (
            id             INT IDENTITY(1,1) PRIMARY KEY,
            column_name    NVARCHAR(128)  NOT NULL,
            abbreviation   NVARCHAR(256)  NULL,
            purpose        NVARCHAR(MAX)  NULL,
            related_tables NVARCHAR(1024) NULL,
            formula        NVARCHAR(MAX)  NULL,
            module         NVARCHAR(128)  NULL,
            updated_by     NVARCHAR(128)  NULL,
            updated_at     DATETIME2      NOT NULL DEFAULT SYSDATETIME()
        )
    """))
    empty = conn.execute(text(f"SELECT COUNT(*) FROM [{TABLE}]")).scalar() == 0
    if empty:
        for col, abbr, purpose, tables, formula, module in SEED:
            conn.execute(text(f"""
                INSERT INTO [{TABLE}] (column_name, abbreviation, purpose, related_tables, formula, module, updated_by)
                VALUES (:c, :a, :p, :t, :f, :m, 'seed')
            """), {"c": col, "a": abbr, "p": purpose, "t": tables, "f": formula, "m": module})
        logger.info(f"[data-dictionary] seeded {len(SEED)} entries")
    conn.commit()


def _row_to_dict(r) -> dict:
    return {
        "id": r[0], "column_name": r[1], "abbreviation": r[2], "purpose": r[3],
        "related_tables": r[4], "formula": r[5], "module": r[6],
        "updated_by": r[7], "updated_at": str(r[8]) if r[8] else None,
    }


COLS = "id, column_name, abbreviation, purpose, related_tables, formula, module, updated_by, updated_at"


# ── Endpoints ───────────────────────────────────────────────────────────────
@router.get("", response_model=APIResponse)
def list_entries(q: Optional[str] = None, current_user: User = Depends(get_current_user)):
    de = get_data_engine()
    with de.connect() as conn:
        _ensure_table(conn)
        if q:
            rows = conn.execute(text(f"""
                SELECT {COLS} FROM [{TABLE}]
                WHERE column_name LIKE :q OR abbreviation LIKE :q OR purpose LIKE :q
                   OR related_tables LIKE :q OR formula LIKE :q OR module LIKE :q
                ORDER BY module, column_name
            """), {"q": f"%{q}%"}).fetchall()
        else:
            rows = conn.execute(text(
                f"SELECT {COLS} FROM [{TABLE}] ORDER BY module, column_name"
            )).fetchall()
    return {"success": True, "data": [_row_to_dict(r) for r in rows]}


@router.post("", response_model=APIResponse)
def create_entry(payload: DictEntryPayload, current_user: User = Depends(get_current_user)):
    if not payload.column_name.strip():
        raise HTTPException(status_code=400, detail="column_name is required")
    de = get_data_engine()
    with de.connect() as conn:
        _ensure_table(conn)
        row = conn.execute(text(f"""
            INSERT INTO [{TABLE}] (column_name, abbreviation, purpose, related_tables, formula, module, updated_by)
            OUTPUT INSERTED.{COLS.replace(', ', ', INSERTED.')}
            VALUES (:c, :a, :p, :t, :f, :m, :u)
        """), {
            "c": payload.column_name.strip(), "a": payload.abbreviation,
            "p": payload.purpose, "t": payload.related_tables,
            "f": payload.formula, "m": payload.module,
            "u": getattr(current_user, "username", None) or getattr(current_user, "email", "user"),
        }).fetchone()
        conn.commit()
    return {"success": True, "data": _row_to_dict(row)}


@router.put("/{entry_id}", response_model=APIResponse)
def update_entry(entry_id: int, payload: DictEntryUpdate,
                 current_user: User = Depends(get_current_user)):
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")
    sets = ", ".join(f"[{k}] = :{k}" for k in fields)
    de = get_data_engine()
    with de.connect() as conn:
        _ensure_table(conn)
        result = conn.execute(text(f"""
            UPDATE [{TABLE}]
            SET {sets}, updated_by = :_user, updated_at = SYSDATETIME()
            WHERE id = :_id
        """), {**fields,
               "_user": getattr(current_user, "username", None) or getattr(current_user, "email", "user"),
               "_id": entry_id})
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="entry not found")
        row = conn.execute(text(f"SELECT {COLS} FROM [{TABLE}] WHERE id = :i"), {"i": entry_id}).fetchone()
        conn.commit()
    return {"success": True, "data": _row_to_dict(row)}


@router.delete("/{entry_id}", response_model=APIResponse)
def delete_entry(entry_id: int, current_user: User = Depends(get_current_user)):
    de = get_data_engine()
    with de.connect() as conn:
        _ensure_table(conn)
        result = conn.execute(text(f"DELETE FROM [{TABLE}] WHERE id = :i"), {"i": entry_id})
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="entry not found")
        conn.commit()
    return {"success": True, "data": {"deleted": entry_id}}
