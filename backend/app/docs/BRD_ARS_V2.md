# Business Requirements Document (BRD)
## ARS V2 Retail — Auto Replenishment System

**Document Version:** 1.0
**Date:** 2026-05-15
**Owner:** Akash Agarwal, Director — V2 Retail
**Author:** ARS Engineering
**Scope:** MSA Stock Calculation, GRID Builder, Listing, Pending Allocation, Rule Engine
**Rule Engine Scope:** `rule_engine_new.py` and `rule_engine_pandas.py` only

---

## 1. Executive Summary

The ARS V2 Retail Auto Replenishment System replaces a legacy 20-machine Excel-based replenishment process across 320+ retail stores and 242 major categories (MAJCATs). It automates the end-to-end flow of:

1. Reading warehouse closing stock and pending obligations.
2. Reconciling stock against in-flight allocations to compute true-available inventory (MSA).
3. Building category × store budget grids (GRID Builder).
4. Classifying every (store × article × colour) candidate as Replenish (RL), To-Be-Checked (TBC), To-Be-Listed (TBL), or MIX (Listing).
5. Allocating constrained warehouse stock across stores using a rules-based waterfall (Rule Engine).
6. Parking allocations for review and feeding approved quantities to SAP via BDC/DO.

This BRD documents the **business intent, inputs, outputs, rules, and edge cases** for each of these modules.

---

## 2. Glossary

| Term | Meaning |
|---|---|
| **MSA** | Multi-Stocking Arrangement — the closing inventory view per (RDC × article × colour × size) after deducting pending allocations and held stock. |
| **RDC** | Replenishment Distribution Centre — source warehouse. Formerly `ST_CD` in raw data. |
| **WERKS** | Store code (SAP plant). |
| **MAJ_CAT** | Major category (e.g., MEN SHIRT, KIDS T-SHIRT). 242 active categories. |
| **GEN_ART** | Generic article (style number). |
| **VAR_ART** | Variant article (style + size). |
| **CLR** | Colour code. |
| **OPT** | "Option" — one (WERKS, MAJ_CAT, GEN_ART, CLR) selling slot at a store. |
| **OPT_TYPE** | One of RL, TBC, TBL, MIX. Mutually exclusive at the OPT grain. |
| **RL** | Replenish — store has adequate display and needs top-up. |
| **TBC** | To-Be-Checked — store has partial display; partial top-up. |
| **TBL** | To-Be-Listed — store has empty/exhausted display; fresh launch. |
| **MIX** | Clearance/insufficient — option cannot be properly listed; rolled up, not allocated. |
| **ACS_D** | Accessories density — one OPT *display* quantity at (store × MAJ_CAT). **Not daily sale.** |
| **MAX_DAILY_SALE** | True velocity metric used for replenishment math. |
| **MBQ** | Merchandise Budget Quantity — replenishment cap at a grid grain. |
| **MJ_MBQ** | Primary MBQ at (WERKS, MAJ_CAT, GEN_ART) grain. |
| **RNG_SEG** | MRP tier (E / V / P / SP). |
| **SLOC** | Storage location within an RDC. |
| **PEND / Parked** | Allocation approved by ARS but not yet shipped via SAP Delivery Order. |
| **DO** | SAP Delivery Order — finalisation event. |
| **BDC** | SAP Batch Data Communication — programmatic upload channel from ARS to SAP. |

---

## 3. Business Objectives

| ID | Objective | Success Measure |
|---|---|---|
| BO-1 | Eliminate manual Excel replenishment across 20 PCs. | 100% of categories processed in ARS. |
| BO-2 | Ensure stock is never double-allocated. | Zero negative `FNL_Q`; zero duplicate `ARS_PEND_ALC` rows per (SESSION, RDC, WERKS, ARTICLE, ALLOC_MODE). |
| BO-3 | Respect category-level budgets (MBQ) and growth rules. | Allocations capped at `MJ_MBQ × cap_pct`; no per-OPT_TYPE growth abuse. |
| BO-4 | Provide an auditable trail from raw stock → MSA → listing → allocation → SAP DO. | Sequence IDs, session IDs, and `ARS_ALLOC_HISTORY` rows traceable end-to-end. |
| BO-5 | Allow user review and cancellation before SAP submission. | Parked-then-approved workflow with hard-kill capability. |
| BO-6 | Run a 320-store × 242-MAJ_CAT cycle within an operational window. | End-to-end run target ≤ 2 hours on pandas allocator. |

---

## 4. Module 1 — MSA Stock Calculation

### 4.1 Business Problem
Raw warehouse stock (per RDC × SLOC × SKU) cannot be used directly for allocation because:
- Some quantity is already approved for shipment but not yet dispatched (pending allocations).
- Some quantity is physically held aside at the RDC for specific stores (TBL/NL holds).
- Stock is spread across multiple SLOCs and must be consolidated.
- Sparse/missing dimensions (colour, fabric, vendor) break GROUP BY math.

**MSA** transforms warehouse closing stock into the **available-for-allocation view** per (RDC × Article × Colour × Size).

### 4.2 Inputs

| Source | Purpose |
|---|---|
| `VW_ET_MSA_STK_WITH_MASTER` | Live stock + master attributes (article, MAJ_CAT, SEG, dim, MRP). |
| `ARS_PEND_ALC` (where `IS_CLOSED=0`) | Approved-but-not-yet-DO'd allocations from prior runs. |
| `ARS_NL_TBL_HOLD_TRACKING` | TBL / NL physical hold reservations. |
| `Master_ALC_INPUT_ST_MASTER` | WERKS → RDC mapping. |
| `MSA_Calculation_Sequence` | Audit log of every MSA run (date, filters, user, row counts). |

### 4.3 The 9-Step Algorithm

| # | Step | Business Rule |
|---|---|---|
| 1 | Filter SLOC | Restrict to user-selected storage locations. |
| 2 | Numeric safety | `STK_Q` coerced to numeric; NaN → 0. |
| 3 | Fill dimensions | Missing CLR → "A"; missing vendor/fabric → "NA"; missing SZ/SSN → 0. Prevents NaN-driven groupby loss. |
| 4 | Filter `SEG ∈ {APP, GM}` | Apparel + General Merchandise only. Excludes non-saleable. Also applies user-level **MAJ_CAT RLS**. |
| 5 | Pivot by SLOC | Row-per-SKU view; columns = SLOC codes (e.g., `DH24_STK`, `DH25_STK`). Dates normalised to midnight to prevent same-day duplicates. |
| 6 | Merge PEND + HOLD | Left-join on (RDC, ARTICLE_NUMBER) — **both keys force-cast to string** to avoid dtype-mismatch nulls. Warn if <99% match rate. |
| 7 | `FNL_Q = max(STK − PEND − HOLD, 0)` | **Never negative.** Pending and held units are removed from the available pool. |
| 8 | Generate colour variants | Retain (RDC, GEN_ART, CLR) rows where `Σ FNL_Q > threshold` (default 25). Drops low-value combos. |
| 9 | Aggregate hierarchy | Group by (RDC, MAJ_CAT, GEN_ART, CLR); sum stock/pend/hold/fnl + SLOC columns. Rename `ST_CD` → `RDC`. |

### 4.4 Outputs

| Table | Grain | Use |
|---|---|---|
| `ARS_MSA_TOTAL` | RDC × GEN_ART × CLR × SZ | Per-warehouse, per-size allocation basis. Full SLOC pivot retained. |
| `ARS_MSA_GEN_ART` | RDC × GEN_ART × CLR | Colour-level rollup for strategy/reporting. |
| `ARS_MSA_VAR_ART` | RDC × GEN_ART × CLR × SZ | High-threshold variants (`FNL_Q > 25`) consumed by the rule engine. |

### 4.5 Functional Requirements

| ID | Requirement |
|---|---|
| MSA-FR-1 | The system **shall** never output `FNL_Q < 0`. |
| MSA-FR-2 | The system **shall** deduct only `ARS_PEND_ALC` rows where `IS_CLOSED = 0`. |
| MSA-FR-3 | The system **shall** apply user MAJ_CAT RLS before aggregation. |
| MSA-FR-4 | The system **shall** log a data-quality warning when pend/hold merge match rate falls below 99%. |
| MSA-FR-5 | The system **shall** persist each run with a unique `sequence_id` and full filter/user metadata. |
| MSA-FR-6 | The system **shall** queue storage as an asynchronous FIFO job and return `job_id` immediately. |
| MSA-FR-7 | The system **shall** auto-create new dimension columns (FLOAT for numeric, NVARCHAR(200) for text) when schema evolves — no manual DDL. |
| MSA-FR-8 | The system **shall** re-seed `PEND_QTY` and `HOLD_QTY` from open `ARS_PEND_ALC` and `ARS_NL_TBL_HOLD_TRACKING` after every MSA write (defensive resync). |

### 4.6 Edge Cases

- **Same-day re-run**: Idempotent — same filters yield identical results; results are versioned, never overwritten.
- **Missing hold table**: Step 6 logs gracefully and proceeds with PEND-only deduction.
- **RDC/article dtype drift**: Forced-string casts on merge keys prevent silent join loss.

---

## 5. Module 2 — GRID Builder

### 5.1 Business Problem
Allocations must respect category and segmentation budgets so no single grid (e.g., fabric, vendor, MRP tier) is over-replenished. GRID Builder produces the budget framework that every downstream rule honours.

### 5.2 Grid Hierarchy

| Type | Grain | Output Field | Purpose |
|---|---|---|---|
| **Primary** | (WERKS, MAJ_CAT, GEN_ART) | `MJ_MBQ`, `MJ_CONT` | Foundational budget per article slot. |
| **Secondary** | (MAJ_CAT, RNG_SEG) | `RNG_SEG_MBQ` | MRP-tier cap. |
| **Secondary** | (MAJ_CAT, FAB) | `FAB_MBQ` | Fabric cap. |
| **Secondary** | (MAJ_CAT, MACRO_MVGR) | `MACRO_MVGR_MBQ` | Macro merchandise group cap. |
| **Secondary** | (MAJ_CAT, MICRO_MVGR) | `MICRO_MVGR_MBQ` | Micro merchandise group cap. |
| **Secondary** | (MAJ_CAT, M_VND_CD) | `M_VND_MBQ` | Vendor cap. |

Each grid row is tagged in `ARS_GRID_BUILDER` with `grid_group ∈ {Primary, Secondary}`.

### 5.3 Inputs

- `vw_master_product` (article master)
- `ET_STORE_STOCK` (closing stock)
- `Master_ALC_INPUT_ST_MAJ_CAT` and `Master_ALC_INPUT_CO_MAJ_CAT`
- Configurable: growth %, coverage %, stock-days, sec-cap %.

### 5.4 Outputs

- `ARS_GRID_MJ_GEN_ART` (primary)
- `ARS_GRID_RNG_SEG_MBQ`, `ARS_GRID_FAB_MBQ`, `ARS_GRID_MACRO_MVGR_MBQ`, `ARS_GRID_MICRO_MVGR_MBQ`, `ARS_GRID_M_VND_MBQ` (secondary)
- Materialised into `ARS_CALC_ST_MAJ_CAT` for fast join during listing.

### 5.5 Functional Requirements

| ID | Requirement |
|---|---|
| GB-FR-1 | The system **shall** treat `MBQ = 0` at any secondary grain as *no constraint* at that grain — **NOT** a zero budget. The constraint defers to the parent (primary) cap. |
| GB-FR-2 | The system **shall** apply a default 130% breach cap (`SEC_CAP_DEFAULT_PCT = 130`) on secondary grids during the **main pass**. |
| GB-FR-3 | In **fallback pass**, the breach cap **shall** lift to `max(SEC_CAP_DEFAULT_PCT, static_growth_pct)`. |
| GB-FR-4 | Growth % **shall** apply only at MAJ_CAT + grid level. **Growth must never be applied per OPT_TYPE (RL / TBC / TBL).** |
| GB-FR-5 | The system **shall** propagate FAB, MACRO_MVGR, MICRO_MVGR, M_VND_CD, RNG_SEG from master → `ARS_LISTING` → `ARS_LISTED_OPT` → allocation table. **If any column is missing, the corresponding secondary grid is silently dropped** — operations must be alerted to verify column propagation each release. |

### 5.6 Edge Cases

- **Sparse vendor/fabric**: An article without a vendor master entry is excluded from vendor sec-cap evaluation, not blocked.
- **Sec-cap silent drop**: If MP columns are not propagated, the breach check returns no rows; ops loses visibility into vendor/fabric over-allocation. This is the single highest-impact integration risk.

---

## 6. Module 3 — Listing

### 6.1 Business Problem
Out of millions of (store × article) combinations, the system must decide which are eligible for replenishment in a given run, classify them by store inventory state (RL/TBC/TBL), and emit a feed for the allocation engine.

### 6.2 Listing Grain

- Input/Output base grain: **(WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR)** in `ARS_LISTING`.
- Expanded for allocation to **(WERKS, RDC, MAJ_CAT, GEN_ART, CLR, VAR_ART, SZ)** in `ARS_LISTED_OPT`.

### 6.3 OPT_TYPE Classification (Mutually Exclusive at OPT grain)

| Tag | Plain-English Definition | Trigger |
|---|---|---|
| **RL** (Replenish) | Store has adequate display and warehouse has stock. Standard top-up. | `STK_TTL ≥ (threshold × ACS_D)` AND `MSA_FNL_Q > 0` |
| **TBC** (To-Be-Checked) | Store has *partial* display; warehouse has stock. Partial top-up. | `0 < STK_TTL < (threshold × ACS_D)` AND `(MSA_FNL_Q > 0 OR RL_HOLD_QTY > 0)` |
| **TBL** (To-Be-Listed) | Store empty / exhausted; warehouse has stock. Fresh launch. | `STK_TTL ≤ 0` AND `(MSA_FNL_Q > 0 OR RL_HOLD_QTY > 0)` |
| **MIX** | Clearance / insufficient warehouse supply or too few sizes to make a complete option. | Catch-all; rolled up to 1 row per (WERKS, MAJ_CAT, RNG_SEG); `ALLOC_FLAG=0` (skipped by allocator) |

### 6.4 ACS_D vs. MAX_DAILY_SALE

- **ACS_D** is the *display* quantity for one OPT, computed at (WERKS, MAJ_CAT). It is **not daily sale**.
- **MAX_DAILY_SALE** is the *velocity* metric used in replenishment math downstream.
- Fallback: when ACS_D is 0/NULL, the system uses `default_acs_d` (default 18) to prevent divide-by-zero and to allow stores with no sales history to receive launch inventory.

### 6.5 Inputs

- `ET_STORE_STOCK` (raw `STK_TTL`)
- 6-month + recent sales (velocity)
- `vw_master_product`
- Grid tables (`ARS_GRID_MJ_GEN_ART` + secondary)
- MSA (`ARS_MSA_TOTAL` / `ARS_MSA_VAR_ART`)

### 6.6 Outputs

- `ARS_LISTING` — primary listing rows with OPT_TYPE, ACS_D, ALC_D, MBQ columns, eligibility flags.
- `ARS_LISTED_OPT` — variant-level rows for the rule engine.
- `ARS_LISTING_SESSIONS` — session metadata (parameters, duration, outcome, metrics).

### 6.7 Session & Job Lifecycle

- Each `/listing/generate` call creates a `SESSION_ID`, persists a session row, and attaches a per-session loguru sink (`backend/logs/listing_sessions/<SESSION_ID>.log`).
- Status: `RUNNING → SUCCESS | FAILED | CANCELLED | PARKED`.
- `ListingJobManager` exposes progress (current step, % complete) and cancellation.
- Cancellation flips dependent allocation queue rows to `CANCELLED`.

### 6.8 Functional Requirements

| ID | Requirement |
|---|---|
| LST-FR-1 | The system **shall** assign exactly one OPT_TYPE per (WERKS, MAJ_CAT, GEN_ART, CLR). |
| LST-FR-2 | The system **shall** treat `RL_HOLD_QTY > 0` as sufficient grounds to retain TBL even if `MSA_FNL_Q = 0` (protects in-transit stock from re-allocation). |
| LST-FR-3 | The system **shall** roll MIX rows up to one row per (WERKS, MAJ_CAT, RNG_SEG) with `ALLOC_FLAG = 0`. |
| LST-FR-4 | The system **shall** propagate FAB / MACRO_MVGR / MICRO_MVGR / M_VND_CD / RNG_SEG into every listed row. |
| LST-FR-5 | Sessions **shall** be cancellable mid-run; the allocation queue **shall** flush to `CANCELLED`. |
| LST-FR-6 | Every session **shall** persist parameter set, metrics, error summary, and per-step timings. |

### 6.9 UI Surface (frontend/src/pages/ListingPage.jsx)

User-visible workflow:
- Filter selection (stores, MAJ_CATs, seasons, RDCs).
- Parameter toggles (stock_threshold_pct, excess_multiplier, apply_sec_cap_in_normal, min_size_count, allocation_mode). (Fallback toggles removed 2026-05-16.)
- Run / poll status / view sessions / cancel.

---

## 7. Module 4 — Pending Allocation (Parked)

### 7.1 Business Problem
Allocations approved by ARS take time to (a) be reviewed, (b) be uploaded to SAP, (c) be confirmed via SAP Delivery Orders. During this window, the approved quantities **must not** be available for re-allocation in subsequent MSA cycles.

### 7.2 Lifecycle

```
[Allocation run] → ARS_ALLOC_PARKED  (PARK_STATUS = NULL)
                       │
        ┌──────────────┼──────────────┐
        │              │              │
   Reject (pre-     Approve       Hard-kill
   approval)        session         (kill)
        │              │              │
        ▼              ▼              ▼
   PARK_STATUS=    ARS_ALLOC_     ARS_ALLOC_QUEUE
   'REJECTED'      HISTORY +       rows → CANCELLED
   (no PEND)       ARS_PEND_ALC
                   (IS_CLOSED=0,
                    PEND_QTY=ALLOC_QTY)
                       │
                       ▼
                   BDC stamped → uploaded to SAP
                       │
                       ▼
                   DO confirmed → PEND_QTY ↓, IS_CLOSED=1 when zero
```

### 7.3 Key Tables

| Table | Purpose | Lifecycle |
|---|---|---|
| `ARS_ALLOC_PARKED` | User review staging. | Insert on alloc; delete/keep on approve/reject. |
| `ARS_ALLOC_HISTORY` | Permanent record of approved allocations. | Append-only. |
| `ARS_PEND_ALC` | Open-order ledger (PEND_QTY = ALLOC_QTY − DO_QTY). | Insert on approve; update on DO; close (`IS_CLOSED=1`) when fully shipped. |
| `ARS_BDC_HISTORY` | SAP upload audit. | Append on BDC generate. |
| `ARS_NL_TBL_HOLD_TRACKING` | Physical RDC reservations for TBL/NL. | Created on approve; cleared on DO. |

### 7.4 MSA ↔ PEND Coupling

On every `ARS_PEND_ALC` insert, `adjust_msa_after_pend_insert()` immediately updates:

```
ARS_MSA_TOTAL.PEND_QTY = Σ ARS_PEND_ALC.PEND_QTY (IS_CLOSED=0) per (RDC, ARTICLE)
ARS_MSA_TOTAL.FNL_Q    = max(STK_QTY − PEND_QTY − HOLD_QTY, 0)
```

The DO event does **not** trigger MSA recalculation because MSA's `STK_QTY` is a daily snapshot — refreshing FNL_Q without new stock would overstate availability.

### 7.5 Cancellation

| Type | Mechanism | Reversal |
|---|---|---|
| Pre-approval reject | Set `PARK_STATUS='REJECTED'` in `ARS_ALLOC_PARKED`. | None needed — never reached PEND. |
| Cooperative cancel (mid-run) | `threading.Event` halts new MAJ_CAT claims. | Queue rows → `CANCELLED`. |
| Hard cancel | `KILL <spid>` on in-flight SQL sessions. | Queue rows → `CANCELLED`. |
| Post-approval revert | `revert_operation()` deletes PEND_ALC rows; symmetric delta restores MSA `FNL_Q`. | Hold-tracking snapshot restored. |

### 7.6 Queue Behaviour

| Status | Transition | Retry |
|---|---|---|
| `PENDING` | → `IN_PROGRESS` → `DONE` / `FAILED` | Auto-retry if `ATTEMPTS < MAX_ATTEMPTS` (=2). |
| `FAILED` | Manual reset → `PENDING` | Fresh `ATTEMPTS = 0`. |
| `CANCELLED` | Terminal | Never re-tried; `mark_done`/`mark_failed` refuse to overwrite (`WHERE STATUS <> 'CANCELLED'`). |

### 7.7 User Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /pend-alc/summary` | Aggregate pending by ALLOC_MODE × SOURCE. |
| `GET /pend-alc/detail` | Paginated row-level view. |
| `POST /pend-alc/manual-upload` | Insert `SOURCE=MANUAL` rows + adjust MSA. |
| `POST /pend-alc/bdc-generate` | Stamp BDC_QTY; return Excel for SAP upload. |
| `POST /pend-alc/do-update` | Apply DO deductions; close fully-shipped rows. |
| `GET /pend-alc/reco` | Aging + reconciliation report. |

### 7.8 Functional Requirements

| ID | Requirement |
|---|---|
| PND-FR-1 | The system **shall** prevent duplicate PEND_ALC rows per (SESSION_ID, RDC, WERKS, ARTICLE, ALLOC_MODE). |
| PND-FR-2 | The system **shall** recompute MSA `FNL_Q` on every PEND insert. |
| PND-FR-3 | The system **shall not** recompute MSA `FNL_Q` on DO updates. |
| PND-FR-4 | The system **shall** support hard-kill of in-flight allocation sessions, cascading to `CANCELLED` queue rows. |
| PND-FR-5 | The system **shall** make approve idempotent (re-running approve on the same session **shall not** create duplicate history or PEND rows). |
| PND-FR-6 | The system **shall** preserve a pre-approve snapshot to enable post-approval revert. |
| PND-FR-7 | The system **shall** detect and clear orphan BDC stamps (BDC_QTY without a matching `ARS_BDC_HISTORY`). |
| PND-FR-8 | Cancellation **shall** be all-or-nothing per operation (entire BDC/DO/upload batch); row-level cancel is out of scope. |

### 7.9 Edge Cases

- **Double-approve**: `NOT EXISTS` guard rejects the second call.
- **DO partial cover**: PEND_QTY decreases; `IS_CLOSED` stays 0 until full coverage.
- **Worker race after cancel**: `WHERE STATUS <> 'CANCELLED'` clause in `mark_done`/`mark_failed` prevents resurrection.
- **Aging**: `/pend-alc/reco` reports days-pending so operations can chase stuck batches.

---

## 8. Module 5 — Rule Engine

> **Scope note:** This BRD covers only `backend/app/services/rule_engine_new.py` and `backend/app/services/rule_engine_pandas.py`. The two parallel variants (`rule_engine_parallel_python.py`, `rule_engine_parallel_sql.py`) are out of scope.

### 8.1 Business Problem
Given a candidate list of options (from Listing), a constrained inventory pool (from MSA), and a category-level budget framework (from GRID Builder), the rule engine must decide:

1. Which options are eligible to ship?
2. How much should each store receive, by size, by RDC?
3. How should over-budget contention be resolved?

### 8.2 Stage Architecture

| Stage | Purpose | Output |
|---|---|---|
| **A — Eligibility** | Apply ordered rules R01–R09 to set `LISTED_FLAG`. | `ARS_LISTED_OPT` with flag + skip reason. |
| **B — Explosion** | Explode listed options into per-store, per-article, per-size rows. Propagate MP columns. | `ARS_ALLOC_WORKING` skeleton. |
| **C — Waterfall** | Allocate inventory in OPT_TYPE order: RL → TBC → TBL, with re-evaluation of headroom after each pass. | Filled `SHIP_QTY` per row. |
| **D — Reflect** | Write final allocations back to listing for reporting and audit. | `ARS_LISTING` updated. |

### 8.3 Rule Catalogue (Stage A)

All rules are OR'd as exclusion criteria — an option is listed only if **every** rule passes.

| Rule | Business Intent | Grain | Default | Skip Reason |
|---|---|---|---|---|
| **R01 — Listing Status** | Respect user marking; only allocate explicitly listed options. | Option | ON | `R01_LISTING` |
| **R02 — OPT_TYPE Filter** | Exclude MIX (clearance/incomplete) options. | Option | ON | `R02_NOT_MIX` |
| **R03 — NL Filter** | *Deprecated.* NL filtering moved upstream to OPT_TYPE tagging. | Option | OFF | n/a |
| **R04 — MSA Availability** | Ship only if MSA has sourced inventory OR prior-run hold exists. | Option | ON | `R04_MSA_POS` |
| **R05 — Positive Requirement** | Enforce minimum demand (`OPT_REQ_WH ≥ 1`). | Option | ON | `R05_REQ_POS` |
| **R06 — Primary Inventory Ceiling** | Gate by primary-supplier ratio (`PRI_CT% ≥ 100`). TBL always enforced; RL/TBC enforcement is config-driven (else they use MBQ-cap instead). | Option | ON (TBL); config (RL/TBC) | `R06_PRI_100` |
| **R07 — Size Coverage (TBL)** | Skip TBL when size coverage is sparse (`VAR_FNL_COUNT / VAR_COUNT < size_threshold` AND `VAR_FNL_COUNT < min_size_count`). | Option | ON | `R07_VAR_RATIO_TBL` |
| **R09 — Headroom (TBL only)** | Prevent **TBL** allocation when (store × MAJ_CAT) headroom is trivial: `headroom = tbl_cap × MJ_MBQ − MJ_STK_TTL − ALLOC_QTY_RUNNING`; skip if `< 0.5 × ACS_D`. ACS_D NULL/0 falls back to `default_acs_d` (UI default = 18). RL/TBC are NOT gated by R09 — they rely on MBQ-cap + MJ_REQ-cap post-waterfall. Re-evaluated after each OPT_TYPE waterfall pass when TBL is still upcoming. | (WERKS, MAJ_CAT) | ON | `MJ_REQ < .5 OF ACS_D` |

### 8.4 Configurable Parameters

| Parameter | Type | Default | Purpose |
|---|---|---|---|
| `size_threshold` | float | 0.6 | R07 minimum size-ratio for TBL. |
| `min_size_count` | int | 3 | R07 minimum size count for TBL. |
| `tbl_trivial_factor` | float | 0.5 | R09 trivial-headroom multiplier (`< 0.5 × ACS_D`). |
| `pri_ct_check_rl` | bool | False | If True, R06 enforces PRI_CT≥100 for RL; else MBQ-cap. |
| `pri_ct_check_tbc` | bool | False | Same, for TBC. |
| `rl_mbq_cap_pct` | float | 0.0 | RL cap as % of MJ_MBQ (0.0 ⇒ no cap, i.e. 1.0×). |
| `tbc_mbq_cap_pct` | float | 0.0 | TBC cap as % of MJ_MBQ. |
| `tbl_mbq_cap_pct` | float | 100.0 | TBL cap as % of MJ_MBQ. |
| `apply_sec_cap_in_normal` | bool | True | Apply secondary-grid cap in main pass; per-grid % via `ARS_GRID_BUILDER.sec_cap_pct` (default 130 when blank). |

### 8.5 Critical Business Constraints

| ID | Constraint |
|---|---|
| RE-CN-1 | **`MBQ = 0` means "no constraint at this grain", NOT "zero budget".** The cap_pct must default to `1.0×` (full budget) in this case; the 1.30× breach penalty **shall not** apply. |
| RE-CN-2 | **Growth % applies at MAJ_CAT + grid level only.** It **shall never** be applied per OPT_TYPE (RL / TBC / TBL). |
| RE-CN-3 | Stage B **shall** propagate MP columns (FAB, MACRO_MVGR, MICRO_MVGR, M_VND_CD, RNG_SEG) into `ARS_ALLOC_WORKING`. Missing extras → silent drop of the matching secondary grid (per GB-FR-5). |
| RE-CN-4 | OPT_TYPE order in the waterfall is fixed: RL → TBC → TBL. Headroom is recomputed after each pass using `ALLOC_QTY_RUNNING`. |
| RE-CN-5 | _(Retired 2026-05-16 — fallback was removed; the constraint no longer applies. See `fallback_archived.md`.)_ |

### 8.6 Secondary-Grid Pre-Gate (F4, Main Pass)

When `apply_sec_cap_in_normal = True`, an OPT is skipped pre-emptively if shipping it would exceed its Secondary grid's cap (default `1.30 × Secondary_MBQ`, per-grid override via `ARS_GRID_BUILDER.sec_cap_pct`) for **any** participating Secondary grid — **unless** demand is high enough to earn the override (`OPT_REQ ≥ SEC_CAP_PRE_OVERRIDE_OPT_REQ_PCT × OPT_MBQ`). Only grids with `sec_cap_applicable = 1` participate; toggle per-grid in the Grid Builder UI.

### 8.7 `rule_engine_new.py` vs. `rule_engine_pandas.py`

| Aspect | `rule_engine_new.py` | `rule_engine_pandas.py` |
|---|---|---|
| **Role** | Canonical spec implementation; delegates Stages A & B to itself. | High-performance allocator; calls into `rule_engine_new` for Stages A & B; rewrites Stage C in pandas. |
| **Stage A / B** | SQL, single-threaded. | Same code (reused). |
| **Stage C** | T-SQL waterfall — per-band SQL round-trips (~11 calls per rank). | In-memory pandas per MAJ_CAT; vectorised numpy ops; 1 bulk write per MAJ_CAT. |
| **Concurrency** | Single thread. | Thread-fanned by MAJ_CAT (default 4 workers, 2–8 configurable). |
| **Rules Applied** | Identical. | Identical. |
| **Tie-Breaking** | SQL `ROW_NUMBER()` over `(OPT_PRIORITY_RANK, ST_RANK)`. | Pandas stable mergesort reproduces SQL ordering. |
| **Performance** | Slower on large MAJ_CATs (round-trip overhead). | 10–100× faster on the hot loop. |
| **Production Default** | Reference implementation. | **Canonical production allocator.** |

### 8.8 Error & Edge Case Handling

| Scenario | Behaviour |
|---|---|
| Missing working / MSA table | Warn + return empty; no exception. |
| Empty grid after filters | Stage proceeds; allocation rows reflect zero; no error. |
| Missing MP / grid extras | Silently exclude that secondary grid from sec-cap. |
| Duplicate column on `ALTER TABLE` | Wrapped in try/except; reruns idempotent. |
| `MBQ = 0` at secondary grain | Treated as no constraint (per RE-CN-1). |
| Conflicting rules | All OR'd — option fails if **any** rule fails. |

### 8.9 Functional Requirements

| ID | Requirement |
|---|---|
| RE-FR-1 | The engine **shall** execute Stages A–D in order, with re-evaluation of R09 after each OPT_TYPE waterfall pass. |
| RE-FR-2 | The engine **shall** produce identical eligibility decisions between `rule_engine_new` and `rule_engine_pandas` for the same input set and parameters. |
| RE-FR-3 | The engine **shall** preserve audit fields (skip reason, rule fired, OPT_PRIORITY_RANK, ST_RANK) for every option. |
| RE-FR-4 | The engine **shall** complete a full 320-store × 242-MAJ_CAT cycle within 2 hours on the pandas allocator. |
| RE-FR-5 | _(Retired 2026-05-16 — fallback was removed. See `backend/app/docs/processes/fallback_archived.md`.)_ |
| RE-FR-6 | The engine **shall not** apply growth % per OPT_TYPE under any configuration. |

---

## 9. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-1 | All long-running operations (MSA storage, Listing, Allocation) **shall** be asynchronous with progress + cancel APIs. |
| NFR-2 | All state-changing operations **shall** be idempotent or guarded by `NOT EXISTS`. |
| NFR-3 | Every session, every rule firing, every state transition **shall** be auditable via `*_HISTORY` / `*_SESSIONS` tables. |
| NFR-4 | The system **shall** support hard-kill of stuck SQL sessions without data corruption. |
| NFR-5 | Schema evolution (new dimension columns) **shall not** require manual DDL. |
| NFR-6 | The system **shall** enforce MAJ_CAT-level RLS across MSA, Listing, and Allocation reads. |

---

## 10. Out of Scope

- Allocation engines other than `rule_engine_new.py` and `rule_engine_pandas.py` (parallel_python and parallel_sql variants).
- The actual BDC mechanics inside SAP (covered by the SAP BDC programme).
- Forecasting / demand-planning modules (separate Planning Hub).
- Frontend visual design — only workflow surface is captured.

---

## 11. Assumptions & Dependencies

- Master article data (`vw_master_product`) is current and includes MP columns (FAB, MACRO_MVGR, MICRO_MVGR, M_VND_CD, RNG_SEG).
- Snowflake provides `VW_ET_MSA_STK_WITH_MASTER` daily before the ARS window.
- SAP Delivery Orders are uploaded daily and `/pend-alc/do-update` is invoked to close the loop.
- User MAJ_CAT RLS is configured via the existing `rbac` schema.

---

## 12. Approvals

| Role | Name | Date | Signature |
|---|---|---|---|
| Director, V2 Retail | Akash Agarwal | | |
| Engineering Lead | | | |
| Operations Lead | | | |
| QA Lead | | | |
