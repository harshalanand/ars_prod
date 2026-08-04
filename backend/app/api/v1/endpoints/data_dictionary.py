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
     "ARS_MSA_GEN_ART, ARS_MSA_TOTAL, ARS_MSA_VAR_ART", "Free stock = warehouse stock minus what's already promised, never below zero:  FNL_Q = MAX(STK_QTY − PEND_QTY, 0).", "MSA"),
    ("STK_QTY", "Stock Quantity", "Raw warehouse stock counted from allowed SLOCs before pending is subtracted.",
     "et_msa_stk, ARS_MSA_GEN_ART", "Add up warehouse stock across the allowed storage locations (SLOCs), per article × RDC. Source view: VW_ET_MSA_STK_WITH_MASTER.", "MSA"),
    ("PEND_QTY", "Pending Quantity", "Pieces already promised to stores by earlier runs but not yet delivered.",
     "MASTER_ALC_PEND, ARS_MSA_GEN_ART", "Add up every open (not-yet-delivered) pending for the article × RDC.", "MSA"),
    ("ARS_PEND", "ARS Pending", "Pending created by ARS runs themselves (subset of PEND_QTY).",
     "MASTER_ALC_PEND", "The slice of PEND_QTY whose open delivery orders were raised by ARS runs (not other systems).", "MSA"),
    ("SEG", "Segment", "Top split of merchandise: APP (apparel) or GM (general merchandise).",
     "ARS_MSA_GEN_ART, vw_master_product", "Read from the product master — no calculation.", "Master"),
    ("DIV", "Division", "Merchandise division, e.g. MENS, LADIES, HH.",
     "ARS_MSA_GEN_ART, vw_master_product", "Read from the product master — no calculation.", "Master"),
    ("SUB_DIV", "Sub-Division", "Division subdivision, e.g. MU (mens upper).",
     "ARS_MSA_GEN_ART, vw_master_product", "Read from the product master — no calculation.", "Master"),
    ("MAJ_CAT", "Major Category", "The planning unit — budgets, grids and runs are per MAJ_CAT (e.g. M_TEES_HS).",
     "ARS_MSA_GEN_ART, ARS_GRID_HIERARCHY, ARS_LISTING", "Read from the product master — no calculation.", "Master"),
    ("SSN", "Season", "Season tag of the article: A, NA, OC, PW, S, SSNL, W.",
     "ARS_MSA_GEN_ART, vw_master_product", "Read from the product master — no calculation.", "Master"),
    ("GEN_ART_NUMBER", "Generic Article Number", "Style-level article id before colour/size split.",
     "ARS_MSA_GEN_ART, retail_gen_article", "Read from the article master — no calculation.", "Master"),
    ("CLR", "Colour", "Colour of the generic article. One OPT = GEN_ART × CLR at one store.",
     "ARS_MSA_GEN_ART, ARS_LISTING", "Read from the article master — no calculation.", "Master"),
    ("WERKS", "Plant / Store code (SAP term)", "The store the row belongs to. Same as ST_CD in master tables.",
     "ARS_LISTING, ARS_ALLOC_WORKING, Master_ALC_INPUT_ST_MASTER", "Read from the store master — no calculation (SAP plant code = ST_CD).", "Master"),
    ("RDC", "Regional Distribution Centre", "Warehouse that serves the store. MSA output ST_CD column was renamed RDC.",
     "Master_ALC_INPUT_ST_MASTER, ARS_MSA_GEN_ART", "Read from the store master (store→RDC mapping) — no calculation.", "Master"),
    ("HUB", "Hub", "Delivery hub between RDC and stores; used for grouping and truck planning.",
     "Master_ALC_INPUT_ST_MASTER", "Read from the store master — no calculation.", "Master"),
    ("MANUAL_ST_PRIORITY", "Manual Store Priority", "Optional manual pin for a store's ST_RANK. Positive integer P forces the store to ST_RANK=P inside every MAJ_CAT it is listed in; blank/0/negative = ranked by W_SCORE. Non-manual stores keep score order but skip pinned slots (per MAJ_CAT).",
     "Master_ALC_INPUT_ST_MASTER", "If a positive number P is entered, force this store to rank P in every MAJ_CAT it appears in. If blank, 0 or negative, rank it automatically by W_SCORE instead.", "Listing"),
    ("AUTO_ST_RANK", "Auto Store Rank (score-only)", "The store's rank by W_SCORE alone, ignoring manual pins — the ST_RANK it would get with no override. Audit column for comparing against the final ST_RANK; never read by the allocation engine.",
     "ARS_STORE_RANKING", "Rank the stores in each MAJ_CAT by W_SCORE (highest first; ties broken by store code). Manual pins are ignored here.", "Listing"),
    ("RANK_DELTA", "Rank Delta", "AUTO_ST_RANK − ST_RANK. Positive = the pin promoted the store; negative = pushed down by another store's pin; 0 = unaffected.",
     "ARS_STORE_RANKING", "RANK_DELTA = AUTO_ST_RANK − ST_RANK.  Positive = a pin promoted this store; negative = a pin pushed it down; 0 = unchanged.", "Listing"),
    ("OPT_TYPE", "Option Type", "For one option (one colour of an article) at one store, the decision this run made and whether it will ship. Set once during Listing; every option gets exactly one value. Only RL/TBC/TBL are actually allocated stock — L and MIX are review-only (they fail the eligibility stock-gate). RL = Repeated Listed (already carried, refill). TBC = To Be Continued (below shelf-need, replenish). TBL = To Be Listed (sold-out with fresh supply). L = Listed/Covered (enough stock, nothing to add). MIX = nothing to send.",
     "ARS_LISTING, ARS_ALLOC_WORKING", "First set the shelf-need  g = Stock% (0.60) × ACS_D  (if ACS_D is blank use 18). Then check the option in this order and STOP at the first match: "
     "(1) MIX — stock < g AND no fresh supply AND no hold (nothing to send). "
     "(2) L — stock ≥ g AND no fresh supply AND no hold (already covered → NOT shipped). "
     "(3) RL — stock ≥ g AND (supply OR hold), OR a sold-out option (stock ≤ 0) that has only a hold and no supply (refill / release the hold). "
     "(4) TBC — 0 < stock < g AND (supply OR hold) (below shelf-need, replenish). "
     "(5) TBL — sold-out (stock ≤ 0) AND fresh supply > 0 (send a first listing). "
     "Anything left over → MIX (safety net; with the rules above it is never reached). "
     "Inputs: stock = STK_TTL, supply = MSA_FNL_Q, hold = RL_HOLD_QTY.", "Listing"),
    ("OPT_STATUS", "Option Status (post-allocation)", "The AFTER-allocation verdict per option — what the option became once the run shipped (or didn't). Complements OPT_TYPE (the BEFORE-allocation decision) and ALLOC_STATUS (the mechanical row result). Values: NL = Newly Listed (TBL that shipped and reached cover), RL = Repeated Listed (refilled, or TBC graduated), WRL = Waiting RL (live option, nothing allocated this run), UNQ = UNQualified this run (new/TBL option, nothing allocated — symmetric with WRL; renamed from 'TBL' 2026-07-31 to avoid colliding with the OPT_TYPE value), L = Listed/Covered (passthrough / covered leftover), MIX = shipped something but the display is still under threshold. Every option gets one of these six — there is no 'NA'.",
     "ARS_LISTING_WORKING", "Computed after allocation on post-alloc stock  post = STK_TTL + ALLOC_QTY  against the shelf-need  g = Stock% (0.60) × ACS_D  (18 if blank). Check in order, STOP at first match: "
     "(1) OPT_TYPE L → L.  (2) OPT_TYPE MIX → MIX.  "
     "(3) RL with nothing allocated → WRL (waiting).  (4) TBL with nothing allocated → UNQ (unqualified this run). "
     "(5) post < g → MIX — shipped but still under the display threshold (also catches TBC that got 0). "
     "(6) TBL → NL (shipped and covered).  (7) TBC → RL (shipped to full cover, graduated). "
     "(8) RL → RL (refilled).  (9) any covered leftover: shipped → RL, untouched → L.", "Listing"),
    ("ACS_D", "Accessories Density (display quantity)", "How many pieces ONE option occupies on the store floor (default 18). Used as the classification yardstick — it is NOT a daily sales number (velocity uses MAX_DAILY_SALE).",
     "ARS_LISTING settings", "A fixed shelf display quantity per option (18 if blank). Used only to set the 'enough stock' line  g = Stock% × ACS_D  that OPT_TYPE checks against.", "Listing"),
    ("RNG_SEG", "Range Segment (MRP tier)", "Price tier of the option: E (economy), V (value), P (premium), SP (special). The primary grid MJ_RNG_SEG budgets per tier.",
     "ARS_GRID_HIERARCHY, ARS_GRID_MJ_*", "Read from the product master / grid hierarchy (price tier) — no calculation.", "Grid"),
    ("MERGE_RNG_SEG", "Merged Range Segment", "Combined tier after Merge Rules (e.g. E+V → EV) so budgets are checked on one bucket.",
     "ARS_GRID_HIERARCHY, ARS_MERGE_RULES", "A lookup, not a formula: the Merge Rules screen maps two or more tiers into one bucket (e.g. E+V → EV) so their budgets are checked together.", "Grid"),
    ("MJ_MBQ", "Major-category Minimum Budget Quantity", "How many pieces a store should hold for a MAJ_CAT (per grid key). 0 = no budget set = no constraint, never 'ship zero'.",
     "ARS_GRID_MJ_GEN_ART", "Built in Grid Builder from sales history + display capacity (see MBQ). 0 means 'no budget set' = no constraint, NOT 'ship zero'.", "Grid"),
    ("MJ_REQ", "Major-category Requirement", "Pieces the store still needs today. The allocation walk consumes this per store × MAJ_CAT.",
     "ARS_GRID_MJ_GEN_ART, ARS_ALLOC_WORKING", "What the store still needs = budget minus what it already holds, never below zero:  MJ_REQ = MAX(MJ_MBQ − store stock, 0).", "Grid"),
    ("MBQ_REV / REQ_REV", "Revised MBQ / Requirement", "Growth-lifted budget used by the run. Reads *_MBQ_ORIG so re-runs never compound growth.",
     "ARS_GRID_* tables", "Grow the budget by the growth %:  MBQ_REV = MBQ_ORIG × Growth%.  Remaining need:  REQ_REV = MAX(0, MBQ_REV − STK_TTL).  Always reads the ORIGINAL MBQ so re-runs don't compound growth.", "Grid"),
    ("OPT_MBQ", "Option MBQ", "Pieces one option needs to make a complete display/ratio at a store.",
     "ARS_LISTING", "Add up the size needs of the option:  OPT_MBQ = sum of SZ_MBQ over all its sizes.", "Listing"),
    ("SZ_MBQ", "Size MBQ", "Pieces per size within an option's display ratio (e.g. S3-M2-L2-XL1).",
     "ARS_LISTING, ARS_ALLOC_WORKING", "Split the option's need across sizes by each size's contribution share:  SZ_MBQ = round(CONT_size × OPT_MBQ).", "Listing"),
    ("PAK_SZ", "Pak Size", "Warehouse pack multiple — shipments round to whole paks.",
     "ARS_MSA_GEN_ART", "Warehouse pack multiple: the shipped quantity is rounded to whole paks (n × PAK_SZ), capped at SZ_MBQ × I_ROD.", "Allocation"),
    ("STOCK_CONSIDER_DT", "Stock Consider Date", "Information-only run date: the business date the stock figures the run was based on reflect (mandatory; UI defaults to D-1 / yesterday). Stamped on every allocation output row; does NOT enter any stock/MSA/allocation calculation.",
     "ARS_ALLOC_WORKING, ARS_ALLOC_PARKED, ARS_ALLOC_HISTORY", "User-entered at run time; constant across the run's rows", "Allocation"),
    ("PICKING_DT", "Picking Date", "Information-only run date: the planned warehouse picking / dispatch date (mandatory since 2026-07-18; UI leaves it blank so the user picks it consciously each run). Stamped on every allocation output row; does NOT enter any calculation.",
     "ARS_ALLOC_WORKING, ARS_ALLOC_PARKED, ARS_ALLOC_HISTORY", "User-entered at run time; constant across the run's rows", "Allocation"),
    ("CONT", "Contribution %", "Share of each size within an option; always sums to 1 per option. For SZ_APPLICABLE='N' categories the fallback fill is Uniform 1/N or stock-based. Stored/rounded to 4 decimals (2026-07-18).",
     "Cont_presets, ARS_ALLOC_WORKING", "CONT ladder: Site → CO → fallback (P4_UNIFORM / P3_FNL_Q); ROUND(…, 4)", "Allocation"),
    ("STOCK_CONT% / SALE_CONT% / INITIAL AUTO CONT%", "Contribution percentages (stock / sale / algo)", "Auto-contribution shares per grain: STOCK_CONT% = stock share, SALE_CONT% = sales-value share, INITIAL AUTO CONT% = algorithm-blended final share. All rounded to 4 decimals (2026-07-18).",
     "Cont_presets, Master_CONT_*, contribution output", "share = value / Σ(value) within group; INITIAL AUTO CONT% = ALGO / Σ ALGO; ROUND(…, 4)", "Contribution"),
    ("SHIP_QTY", "Ship Quantity", "Pieces the run decided to send to the store (per size / rolled up).",
     "ARS_ALLOC_WORKING, alloc detail history", "The final quantity to send, after the allocation waterfall applies budgets, the sec-cap fences, and pak rounding.", "Allocation"),
    ("HOLD_QTY", "Hold Quantity", "Pieces reserved at the RDC instead of shipped (NL/TBL ramp-up, hold window). Subtracted from free stock but no delivery order yet. Released (zeroed) when the option ends the run NOT covered (OPT_STATUS=MIX) — see HOLD_RELEASED_QTY.",
     "ARS_ALLOC_WORKING, hold reports", "Pieces kept back at the RDC (for NL/TBL ramp-up or the hold window) instead of shipped. They reduce free stock but no delivery order is raised yet — not a formula, an outcome of the hold rules. If the option's post-alloc OPT_STATUS is MIX (display still under threshold), the hold is released back to the pool and this becomes 0.", "Allocation"),
    ("HOLD_RELEASED_QTY", "Hold Released Quantity", "Audit of the warehouse hold that was RELEASED because the option ended the run not covered (OPT_STATUS=MIX after allocation). Holding RDC stock for a display that is not viable would strand it, so the reservation is cancelled and the pieces stay in the free pool for the next run. Covered (NL) options keep their holds.",
     "ARS_LISTING_WORKING", "When OPT_STATUS = MIX and the option had HOLD_QTY > 0: HOLD_RELEASED_QTY = that hold, then HOLD_QTY is set to 0 on the option row, its size rows (ARS_ALLOC_WORKING) and this session's ARS_ALLOC_PARKED copy, with ';HOLD_RELEASED_NOT_COVERED(n)' stamped in ALLOC_REMARKS. Blank/0 = nothing was released.", "Listing"),
    ("ALLOC_STATUS", "Allocation Status", "Outcome per row: ALLOCATED, PARTIAL, NOT_ALLOCATED, INELIGIBLE — with honest skip reasons (MBQ_CAP_*, POOL_EMPTY, R07_SIZE_RATIO_LIVE …).",
     "ARS_ALLOC_WORKING", "Set from the row's result: ALLOCATED (fully shipped), PARTIAL (some shipped), NOT_ALLOCATED (nothing shipped, with a skip reason), or INELIGIBLE (failed the gates before the walk).", "Allocation"),
    ("PRI_CT", "Priority Count coverage %", "How complete the store's priority assortment is. PRI ≥ 100% toggles gate RL/TBC listing on full coverage.",
     "ARS_LISTING", "PRI_CT% = priority items the store already covers ÷ priority items required, as a %.  100% (full coverage) opens the RL/TBC listing gate.", "Listing"),
    ("FAB / MICRO_MVGR / M_VND_CD", "Fabric / Micro merchandise group / Vendor code", "Secondary-grid (sec-cap) dimensions — fences so one fabric/group/vendor cannot eat the whole MAJ_CAT budget. Must flow listing → alloc or the fence silently disappears.",
     "ARS_GRID_FAB, ARS_GRID_MICRO_MVGR, ARS_GRID_M_VND_CD", "Each acts as a spending fence:  cap = MAX(0, that grid's original MBQ × cap% − current stock).  A cap value of 0 means 'no fence for this key'.", "Grid"),
    # ── Grid pre-calc + budget columns (audit fill 2026-07-18) ──
    ("ALC_D", "Allocation / sale-cover Days", "Number of days the grid budget should cover. SAL_PD is averaged over this window and MBQ multiplies by it.",
     "ARS_CALC_ST_MAJ_CAT, ARS_CALC_ST_ART", "ALC_D = ISNULL(INT_DAYS,0)+ISNULL(PRD_DAYS,0)+ISNULL(SL_CVR,0); SL_CVR priority ST_MAJ_CAT > CO_MAJ_CAT > ST_MASTER", "Grid"),
    ("SAL_PD", "Sale Per Day", "Blended per-day sale rate over the ALC_D window: current-month actuals, extended by the next-month daily rate for the remaining days. Rounded to 2 decimals (2026-07-18).",
     "ARS_CALC_ST_MAJ_CAT, ARS_CALC_ST_ART, MASTER_GEN_ART_SALE", "if CM_REM_D=0→0; elif CM_REM_D≥ALC_D→CM_SAL_Q/CM_REM_D; elif ALC_D=0→0; elif NM_REM_D=0→CM_SAL_Q/CM_REM_D; else (CM_SAL_Q + (NM_SAL_Q/NM_REM_D)×(ALC_D−CM_REM_D))/ALC_D — ROUND(…, 2)", "Grid"),
    ("DISP_Q", "Display Quantity", "Fixture display capacity at the grid grain. 0/NULL ⇒ no minimum buy (MBQ = 0). Feeds both MBQ and OPT_CNT.",
     "ARS_CALC_ST_MAJ_CAT, ARS_GRID_*", "Fixture display capacity at the grid grain, read from ARS_CALC_ST_MAJ_CAT. If 0 or blank the budget is forced to 0 (MBQ = 0). Feeds both MBQ and OPT_CNT.", "Grid"),
    ("MBQ", "Minimum Buy Quantity (grid)", "Per-grid target stock for the store: demand over the cover window plus display capacity, scaled by contribution.",
     "ARS_GRID_MJ_*, ARS_GRID_<grid>", "MBQ = (SAL_PD×BGT_SL_GR_DGR)×ALC_D + DISP_Q×DISP_GR_DGR; then ROUND(MBQ×CONT, 0); 0 if DISP_Q=0 or CONT=0", "Grid"),
    ("OPT_CNT", "Option Count", "How many options the fixture can hold at the grid grain (display ÷ accessories density).",
     "ARS_GRID_*", "OPT_CNT = ROUND(DISP_Q×DISP_GR_DGR×CONT / ACS_D, 0)", "Grid"),
    ("BGT_SL_GR_DGR / DISP_GR_DGR", "Budget-sale / Display growth degree", "Grid-level growth multipliers baked into MBQ. Growth lives at MAJ_CAT + grid only — never per OPT_TYPE.",
     "ARS_CALC_ST_MAJ_CAT, ARS_GRID_*", "Growth multipliers (×) applied inside MBQ: BGT_SL_GR_DGR lifts the sales part, DISP_GR_DGR lifts the display part. 1.00 = no growth. Set per MAJ_CAT + grid only — never per OPT_TYPE.", "Grid"),
    ("I_ROD", "Items per Rod", "Pack/rod multiple; per-size ship is capped at SZ_MBQ × I_ROD and the engine gives the option one allocation round per I_ROD. Defaults to 1. Assorted colours (CLR 'A'/'A_MIX') are floored to 2 — a maintained higher value is kept.",
     "ARS_CALC_ST_MAJ_CAT, ARS_CALC_ST_ART", "Pack/rod multiple (defaults to 1). Caps how much one size can ship: per-size ship ≤ SZ_MBQ × I_ROD, and the option participates in allocation rounds 1..I_ROD. For CLR 'A'/'A_MIX' the listing run applies a FLOOR of 2 (MAX semantics, 2026-07-31): NULL/0/1 is lifted to 2, a maintained 3/4 stays as maintained.", "Grid"),
    # ── Listing columns ──
    ("STK_TTL", "Total Store Stock", "Store on-hand at the grain (clamped ≥ 0 at article grain before rollup). Basis for OPT_TYPE classification and REQ.",
     "ARS_LISTING, ARS_GRID_*", "Add up the store's on-hand at the grain (each article floored at 0 before rollup). This is the main input to OPT_TYPE and to the requirement (REQ).", "Listing"),
    ("MSA_FNL_Q", "MSA Free Quantity (listing copy)", "The MSA free-to-allocate quantity joined onto listing; > 0 is required for RL / TBC / TBL.",
     "ARS_LISTING ← ARS_MSA_GEN_ART.FNL_Q", "The MSA free stock (FNL_Q) copied onto the listing row. Must be > 0 for an option to become RL, TBC or TBL (this is the 'fresh supply' in OPT_TYPE).", "Listing"),
    ("ELIG_FLAG", "Eligibility Flag", "1 = option passes eligibility (E1–E7) and projects into ARS_LISTING_WORKING; 0 = excluded from the run.",
     "ARS_LISTING, ARS_LISTING_WORKING", "1 only if the option passes ALL gates: listed, has supply-or-hold, has demand, display > 0, and (for TBL) enough size coverage. Otherwise 0. Only the 1s are copied into ARS_LISTING_WORKING and allocated — so L and MIX get 0.", "Listing"),
    ("RL_HOLD_QTY", "Refill Hold Quantity", "Prior-run NL/TBL hold carried into this run (pool-scoped). Lets a held option list as RL/TBC.",
     "ARS_NL_TBL_HOLD_TRACKING → ARS_LISTING", "Warehouse stock held for this option by an earlier NL/TBL run (same pool), carried into this run. If > 0 the option can list as RL/TBC even with no fresh MSA (this is the 'hold' in OPT_TYPE).", "Listing"),
    ("VAR_COUNT / VAR_FNL_COUNT", "Variant count / with-stock count", "Distinct sizes of the option, and how many have free stock. Drive the R07 size-coverage gate for TBL.",
     "ARS_LISTING", "R07: skip TBL when VAR_FNL_COUNT/VAR_COUNT < Size Cov% AND VAR_FNL_COUNT < Min size #", "Listing"),
    ("AGE", "Article age (days)", "Effective age of the option; when < the AGE threshold, PER_OPT_SALE may drive the demand rate. 0 when STK_TTL ≤ 0 and L-7 ≤ 0.",
     "ARS_LISTING", "Effective days since the option went live. If AGE < the age threshold, the demand rate can come from PER_OPT_SALE instead of the usual velocity. Set to 0 when STK_TTL ≤ 0 and last-7-day sale ≤ 0.", "Listing"),
    ("MAX_DAILY_SALE", "Max daily sale (velocity)", "Velocity term for OPT_MBQ demand (NOT ACS_D). Part of the rate_expr ladder (PER_OPT / L-7 / AUTO).",
     "ARS_LISTING", "Sales velocity (pieces/day) that feeds the OPT_MBQ demand — this is the true sales-rate term, NOT ACS_D. Picked from the rate ladder: PER_OPT_SALE → last-7-day → AUTO.", "Listing"),
    ("OPT_MBQ_WH / OPT_REQ_WH", "Option MBQ / REQ with warehouse hold", "TBL variant that adds hold_days lookback so the first dispatch parks a warehouse buffer. For RL/TBC the WH value equals the base.",
     "ARS_LISTING", "OPT_MBQ_WH = ROUND(ACS_D + rate×(ALC_D + hold_days_if_TBL), 0); OPT_REQ_WH = MAX(0, OPT_MBQ_WH − STK_TTL)", "Listing"),
    ("ART_EXCESS", "Article Excess", "Stock above the excess ceiling — surfaced for pull / cross-RDC. 0 for MIX options.",
     "ARS_LISTING", "ART_EXCESS = MAX(0, STK_TTL − excess_multiplier × OPT_MBQ)", "Listing"),
    ("ST_RANK / W_SCORE", "Store Rank / Weighted Score", "Per-MAJ_CAT store priority. W_SCORE blends the REQ and FILL ranks; ST_RANK orders stores by it (manual pins override). Also the cross-MAJ_CAT allocation tiebreaker.",
     "ARS_STORE_RANKING, ARS_LISTING", "W_SCORE = ROUND(REQ_RANK×req_wt + FILL_RANK×fill_wt, 2); ST_RANK = ROW_NUMBER by W_SCORE DESC, WERKS ASC", "Listing"),
    # ── Allocation columns ──
    ("ALLOC_TYPE", "Allocation Pool Type", "FRESH or GRT — which warehouse SLOC pool the run allocated from. Stamped on every alloc output row; pend/hold deductions are typed to the pool.",
     "ARS_ALLOC_WORKING, ARS_PEND_ALC, ARS_SLOC_SETTINGS", "Chosen at run start: FRESH or GRT — which warehouse SLOC pool to ship from. Stamped on every output row; pending and hold deductions are kept separate per pool. Not a calculation.", "Allocation"),
    ("OPT_PRIORITY_RANK", "Option Priority Rank", "Waterfall order within a store × MAJ_CAT (RL→TBC→TBL, then rank). Lower ships first.",
     "ARS_LISTING_WORKING, ARS_ALLOC_WORKING", "The order options ship inside a store × MAJ_CAT: first by type (RL, then TBC, then TBL), then by rank. The lower the number, the earlier it ships.", "Allocation"),
    ("MJ_REQ_REM", "MAJ_CAT Requirement Remaining", "Live remaining MJ_REQ during the per-OPT walk; each shipping OPT consumes it. Gates TBL admission and bounded overshoot.",
     "rule engine (per_opt), ARS_ALLOC_WORKING", "starts at MJ_REQ; skip an OPT when req_rem < 0.5 × OPT_MBQ", "Allocation"),
    ("ALLOC_REMARKS", "Allocation Remarks", "Per-row audit trace: band steps B[ot.rN.rkK], cap stamps (MBQ_CAP_OVERSHOOT / MBQ_CAP_SCALE / skip), and sec-cap veto/override reasons.",
     "ARS_ALLOC_WORKING, ARS_ALLOC_HISTORY", "A human-readable audit trail (not a formula): the band steps taken, any cap stamps (MBQ_CAP_OVERSHOOT / MBQ_CAP_SCALE / skip), and the sec-cap veto or override reasons for this row.", "Allocation"),
    ("FROM_HOLD", "From Hold", "Units drawn from the RDC hold buffer (consumed before pool stock) for RL/TBC rows.",
     "ARS_ALLOC_WORKING", "Pieces taken from the RDC hold buffer first, before touching pool stock, for RL/TBC rows. It is the amount consumed from RL_HOLD_QTY on this row.", "Allocation"),
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
    # Idempotent top-up: insert any SEED column that isn't already present.
    # Runs on first use (empty table) AND after the SEED list grows, so the
    # live dictionary picks up newly-documented columns without wiping any
    # user-edited rows (match is by column_name).
    existing = {
        (r[0] or "").strip().lower()
        for r in conn.execute(text(f"SELECT column_name FROM [{TABLE}]")).fetchall()
    }
    added = 0
    refreshed = 0
    for col, abbr, purpose, tables, formula, module in SEED:
        if (col or "").strip().lower() in existing:
            # Row already present — refresh it ONLY if it is still system-owned
            # (updated_by in 'seed'/'code-sweep'), so edits to the SEED list
            # propagate to the live dictionary while any USER-edited row (whose
            # updated_by is a username/email, set by the POST/PUT endpoints) is
            # left untouched. updated_by is consolidated to 'seed' on refresh.
            res = conn.execute(text(f"""
                UPDATE [{TABLE}]
                SET abbreviation = :a, purpose = :p, related_tables = :t,
                    formula = :f, module = :m, updated_by = 'seed', updated_at = SYSDATETIME()
                WHERE column_name = :c
                  AND ISNULL(updated_by, 'seed') IN ('seed', 'code-sweep')
                  AND (ISNULL(abbreviation,'') <> ISNULL(:a,'')
                    OR ISNULL(purpose,'')       <> ISNULL(:p,'')
                    OR ISNULL(related_tables,'')<> ISNULL(:t,'')
                    OR ISNULL(formula,'')       <> ISNULL(:f,'')
                    OR ISNULL(module,'')        <> ISNULL(:m,''))
            """), {"c": col, "a": abbr, "p": purpose, "t": tables, "f": formula, "m": module})
            refreshed += (res.rowcount or 0)
            continue
        conn.execute(text(f"""
            INSERT INTO [{TABLE}] (column_name, abbreviation, purpose, related_tables, formula, module, updated_by)
            VALUES (:c, :a, :p, :t, :f, :m, 'seed')
        """), {"c": col, "a": abbr, "p": purpose, "t": tables, "f": formula, "m": module})
        added += 1
    if added or refreshed:
        logger.info(f"[data-dictionary] seeded {added} new, refreshed {refreshed} seed-owned entries")
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


@router.get("/export")
def export_entries(q: Optional[str] = None, current_user: User = Depends(get_current_user)):
    """Download the data dictionary as an .xlsx (honours the same ?q= filter
    as the list endpoint). One sheet, human-readable column headers."""
    import io
    import pandas as pd
    from fastapi.responses import StreamingResponse

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

    df = pd.DataFrame([{
        "Module":         r[6],
        "Column":         r[1],
        "Abbreviation":   r[2],
        "Purpose":        r[3],
        "Related tables": r[4],
        "Formula":        r[5],
        "Updated by":     r[7],
        "Updated at":     str(r[8]) if r[8] else None,
    } for r in rows], columns=[
        "Module", "Column", "Abbreviation", "Purpose",
        "Related tables", "Formula", "Updated by", "Updated at",
    ])

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name="Data Dictionary")
        # Widen columns for readability.
        ws = xw.sheets["Data Dictionary"]
        widths = {"A": 16, "B": 26, "C": 30, "D": 70, "E": 40, "F": 55, "G": 14, "H": 20}
        for col, w in widths.items():
            ws.column_dimensions[col].width = w
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=ars_data_dictionary.xlsx"},
    )


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
