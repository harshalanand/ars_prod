---
title: Full Replenishment Cycle — MSA → Grid → Listing → Approve → DO → Repeat
category: Allocation
order: 10
source: backend/app/services/msa_service.py, backend/app/services/parked_history.py, backend/app/services/pend_alc_service.py, backend/app/api/v1/endpoints/listing.py, backend/app/api/v1/endpoints/pend_alc.py
last_reviewed: 2026-05-03
---

# Full Replenishment Cycle

> **This is the master SOP.** It covers everything from the first data upload to the final DO entry that closes the loop. Run through this checklist every replenishment cycle. Each section links to the dedicated doc for deep-dives.

---

## Live system health

<!-- @metric sql="SELECT ISNULL(SUM(FNL_Q),0) FROM ARS_MSA_VAR_ART" label="Shippable units (MSA FNL_Q)" -->

<!-- @metric sql="SELECT ISNULL(SUM(PEND_QTY),0) FROM ARS_PEND_ALC WHERE IS_CLOSED = 0" label="Units pending DO (ARS_PEND_ALC)" -->

<!-- @metric sql="SELECT COUNT(DISTINCT SESSION_ID) FROM ARS_PEND_ALC WHERE IS_CLOSED = 0" label="Open allocation sessions awaiting DO" -->

<!-- @metric sql="SELECT ISNULL(SUM(ALLOC_QTY),0) FROM ARS_LISTING_WORKING" label="Current cycle allocation qty" -->

<!-- @metric format="table" sql="SELECT TOP 6 MAJ_CAT, SUM(PEND_QTY) AS Pending FROM ARS_PEND_ALC WHERE IS_CLOSED=0 GROUP BY MAJ_CAT ORDER BY Pending DESC" label="Top MAJ_CATs with open pending" -->

---

## The complete picture — one glance

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  DAILY / WEEKLY REPLENISHMENT CYCLE                                          ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  [UPLOADS]          [MSA]              [GRID BUILDER]     [LISTING]          ║
║  Stock              MSA calculates     Builds per-store   8-part run:        ║
║  Sales        ───►  FNL_Q per          MBQ, REQ, grid     classify → rank   ║
║  Master data        (RDC, Article)     columns            → allocate         ║
║                          │                  │                  │             ║
║                     ARS_MSA_VAR_ART    ARS_GRID_MJ        ARS_ALLOC_WORKING  ║
║                          │                  │                  │             ║
║                          └──────────────────┘                  │             ║
║                                 feeds                          │             ║
║                                                         [AUTO-PARK]          ║
║                                                         ARS_ALLOC_PARKED     ║
║                                                                │             ║
║                                                         [APPROVE]            ║
║                                                         ARS_ALLOC_HISTORY    ║
║                                                         ARS_PEND_ALC  ◄─ NEW ║
║                                                                │             ║
║                    [NEXT MSA]     ◄───────── PEND_QTY ─────────┤             ║
║                    FNL_Q drops                                 │             ║
║                    for approved                         [SAP issues DO]      ║
║                    articles                             1–5 days later       ║
║                                                                │             ║
║                                                         [DAILY DO ENTRY]     ║
║                                                         ARS_PEND_ALC.DO_QTY  ║
║                                                         IS_CLOSED = 1        ║
║                                                                │             ║
║                    [NEXT MSA]     ◄───────── PEND_QTY=0 ───────┘             ║
║                    FNL_Q restored                                            ║
║                    for DO'd articles                                         ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

## Phase 1 — Upload source data

**Who:** Data team / warehouse operations
**When:** Before every MSA run — whenever fresh stock/sales are available
**Pages:** Data Management → Upload Data

### What to upload

| Dataset | Table | Frequency |
|---------|-------|-----------|
| Warehouse stock snapshot | `ET_STORE_STOCK` (or MSA staging) | Daily or per-run |
| Store sales | `ET_STORE_SALES` | Daily or per-run |
| Master product data | `vw_master_product` (refreshed via view) | When articles change |
| Average sale / age | `MASTER_GEN_ART_SALE`, `MASTER_GEN_ART_AGE` | When season changes |
| Size master | `Master_CONT_SZ` | When size grids change |
| Store master | `Master_ALC_INPUT_ST_MASTER` | When stores open/close |

### Checklist

- [ ] `ET_STORE_STOCK` row count increased vs last upload
- [ ] `ET_STORE_SALES` covers the correct date range
- [ ] No upload errors in Jobs Dashboard (`/jobs`)
- [ ] `vw_master_product` returns all expected MAJ_CATs

---

## Phase 2 — MSA Stock Calculation

**Who:** Planning team
**When:** After uploads, before Listing
**Page:** Data Preparation → MSA Stock Calculation

MSA answers: *"For every article at every RDC, how many units can we freely ship right now?"*

### The 9-step algorithm

| Step | What happens |
|------|-------------|
| 1 | Filter to selected SLOCs (storage locations) |
| 2 | Normalise: blanks → 0, negatives → 0 |
| 3 | Fill missing colour / vendor / size defaults |
| 4 | Keep only SEG = APP and GM |
| 5 | Pivot by SLOC → `STK_QTY` = sum across all SLOCs |
| **6** | **Subtract ARS_PEND_ALC** (approved not-yet-DO'd units) — see Phase 6 |
| 6.5 | Subtract open HOLD_QTY (NL/TBL reservations from ARS_NL_TBL_HOLD_TRACKING) |
| 7 | `FNL_Q = max(STK_QTY − PEND_QTY − HOLD_QTY, 0)` |
| 8 | Expand to colour+size variant grain |
| 9 | Aggregate to option level |

### The formula

```
FNL_Q = max( STK_QTY  −  PEND_QTY  −  HOLD_QTY , 0 )
              │                │              │
              │         from ARS_PEND_ALC     │
              │         IS_CLOSED=0 rows       from ARS_NL_TBL_HOLD_TRACKING
              └─ raw warehouse stock          IS_CLOSED=0 rows
                 from ET_STORE_STOCK
```

> **Important:** `PEND_QTY` comes exclusively from `ARS_PEND_ALC`. The legacy `MASTER_ALC_PEND` table is no longer read.

### Output tables

| Table | Grain | Used by |
|-------|-------|---------|
| `ARS_MSA_TOTAL` | RDC × SLOC × Article | Audit |
| `ARS_MSA_GEN_ART` | RDC × GEN_ART × CLR | Reporting |
| `ARS_MSA_VAR_ART` | RDC × Article × Size | **Allocator** (the pool) |

### Checklist

- [ ] Run completed without error
- [ ] `ARS_MSA_VAR_ART` row count > 0
- [ ] `SUM(FNL_Q)` is reasonable (not zero, not equal to STK_QTY if pending exists)
- [ ] `SUM(PEND_QTY)` matches open rows in `ARS_PEND_ALC`

---

## Phase 3 — Grid Builder

**Who:** Planning team
**When:** After master data refresh, before Listing
**Page:** Data Preparation → Grid Builder

The Grid Builder computes, for every combination of store × category × level: how many units a store *should* have on its floor (MBQ — Minimum Base Quantity), how much it currently has (stock), and what the demand gap is.

### Grids built

| Grid table | Level | Used for |
|------------|-------|---------|
| `ARS_GRID_MJ` | MAJ_CAT | Category-level MBQ |
| `ARS_GRID_MJ_RNG_SEG` | MAJ_CAT × Range × Segment | Per-option sale reference |
| `ARS_GRID_MJ_FAB` | MAJ_CAT × Fabric | Fabric-level grid |
| `ARS_GRID_MJ_VAR_ART` | Variant article | Size-grain grid |

Only grids with `GRID_BUILDER.status = 'ACTIVE'` are used in Listing. Inactive grids are skipped silently.

### Checklist

- [ ] Grid Builder → Run All completed
- [ ] All target grids show `ACTIVE` status
- [ ] `ARS_GRID_MJ` has rows for all MAJ_CATs you're running this cycle
- [ ] Spot-check: `OPT_MBQ` values are not zero for your focus category

---

## Phase 4 — Listing & Allocation

**Who:** Planning team (clicks Generate)
**When:** After MSA and Grids are ready
**Page:** Data Preparation → Listing

### Before you click Generate

| # | Check | Where |
|---|-------|-------|
| 1 | MSA run today | `ARS_MSA_VAR_ART` — check the max date |
| 2 | Grids rebuilt | Grid Builder → all ACTIVE |
| 3 | Pending rows are current | Pending Allocation → Overview — PEND_QTY matches expectation |
| 4 | No other session running | Listing will return 409 if another run is active |

### Season filter (SSN)

Before clicking Generate, select the season(s) in the **Season (SSN)** checkbox group in Tunable Parameters. Only MAJ_CATs that belong to the selected seasons will be included in the run. Leave all unchecked to run for all seasons.

### Tunable parameters (key ones)

| Parameter | Effect |
|-----------|--------|
| Stock% | Threshold for TBC vs RL classification |
| Hold (days) | Buffer days of stock to reserve at RDC |
| Req% / Fill% | Store ranking weights (need vs current fill rate) |
| Fallback | Allow allocation to partially-covered options |
| Season (SSN) | Run only for selected season's MAJ_CATs |

### The 8-part pipeline

```
Part 1  Copy variant grid rows → ARS_LISTING (IS_NEW=0)
Part 2  Add MSA-only options (no store stock yet, IS_NEW=1)
Part 3  Bring ACS_D, ALC_D, article age, sale reference
Part 3.55  Attach MSA_FNL_Q to each listing row          ← reads ARS_MSA_VAR_ART
Part 3.6   Classify: MIX / RL / TBC / TBL
Part 4  Join grids → compute OPT_MBQ, OPT_REQ, demand gap
Part 6  Build ARS_STORE_RANKING (rank stores by need within MAJ_CAT)
Part 7  Build ARS_LISTING_WORKING (eligible rows: MSA>0, REQ_WH≥1)
Part 8  ALLOCATE: wave through stores, consume MSA pool → ALLOC_QTY / HOLD_QTY
```

### OPT_TYPE classification

| Tag | Meaning | What happens |
|----|---------|-------------|
| **MIX** | Colour/size set is incomplete | Allocate to round out the set |
| **RL** | Regular line — adequate stock | Allocate normally by store ranking |
| **TBC** | To Be Confirmed — borderline stock | Allocate with caution; may hold |
| **TBL** | To Be Listed — RDC stock only | Move to NL hold (no store delivery yet) |

### HOLD_QTY (NL / TBL)

TBL articles produce `HOLD_QTY` rows in `ARS_NL_TBL_HOLD_TRACKING`. These units sit at the RDC but are **reserved** — MSA's Step 6.5 deducts them so they can't be re-allocated to a different store on the next run.

When the DO for that hold article is later issued, `apply_do_deductions()` reduces `HOLD_REM` in the hold tracking table, and closes the row when `HOLD_REM ≤ 0`.

### After Generate

The run produces `ARS_ALLOC_WORKING` — the allocation plan for this session. Immediately after a successful run, the system **auto-parks** the results:

```
ARS_ALLOC_WORKING  ─────► ARS_ALLOC_PARKED   (PARK_STATUS = 'PARKED')
ARS_LISTING_WORKING ─────► ARS_LISTING_WORKING_PARKED
```

Both are snapshotted under the same `SESSION_ID`. The live working tables are then cleared for the next run.

### Checklist

- [ ] Part 1–7 completed without error
- [ ] Part 8 produced > 0 alloc rows
- [ ] OPT_TYPE distribution looks reasonable (not all MIX, not all TBC)
- [ ] ALLOC_QTY > 0 for at least the top-priority MAJ_CATs
- [ ] Auto-park succeeded (PARKED_STATUS = 'PARKED' in session log)

---

## Phase 5 — Review & Approve Parked Run

**Who:** Senior planner / allocations manager
**When:** After Generate, before BDC export
**Page:** Listing page → Parked Runs section (collapsible at the bottom)

### What to check in the parked run

- **Alloc rows tab** — `ALLOC_QTY` per store per article looks right
- **Listing rows tab** — `OPT_TYPE` and `HOLD_QTY` make sense
- **PEND_QTY** — check that this session's approval won't push PEND_QTY unreasonably high

### Approve

Click **Approve** on the parked run. The following happens atomically:

```
ARS_ALLOC_PARKED        ──► ARS_ALLOC_HISTORY       (permanent record)
ARS_LISTING_WORKING_PARKED ──► ARS_LISTING_WORKING_HISTORY

Then (still in same connection):
ARS_PEND_ALC  ◄── INSERT one row per (RDC, Article)
  SESSION_ID   = this session
  ALLOC_QTY    = SUM(ALLOC_QTY) from ARS_ALLOC_HISTORY for this session
  DO_QTY       = 0
  PEND_QTY     = ALLOC_QTY − DO_QTY   (computed automatically)
  IS_CLOSED    = 0
```

If the INSERT into ARS_PEND_ALC fails, the approval still completes — the error is logged and `pend_alc_rows: 0` is returned. Check the server log if you suspect pend_alc wasn't written.

### What this means for the next MSA

From this moment, `ARS_PEND_ALC` contains open rows for this session. The **very next MSA run** will read them and reduce FNL_Q for every (RDC, Article) in this session by the approved ALLOC_QTY.

### Checklist

- [ ] Reviewed alloc rows — spot-check 5–10 articles in key MAJ_CATs
- [ ] Approved session shows in Alloc History
- [ ] Pending Allocation → Overview now shows this session's rows
- [ ] API response includes `pend_alc_rows > 0`

---

## Phase 6 — ARS_PEND_ALC Lifecycle (the anti-double-allocation layer)

**Where it lives:** Table `ARS_PEND_ALC` in the data database

This phase runs automatically — no user action needed until Phase 7.

### Table columns

| Column | Type | Meaning |
|--------|------|---------|
| `SESSION_ID` | NVARCHAR(50) | Which allocation run created this |
| `RDC` | NVARCHAR(20) | Warehouse / DC code |
| `ARTICLE_NUMBER` | NVARCHAR(30) | Variant article (colour + size) |
| `MAJ_CAT` | NVARCHAR(50) | Category (for reporting) |
| `ALLOC_QTY` | FLOAT | Total approved units for this article at this RDC |
| `DO_QTY` | FLOAT | SAP DO units received so far (incremented daily) |
| `PEND_QTY` | FLOAT (computed) | `ALLOC_QTY − DO_QTY` — what MSA deducts |
| `APPROVED_AT` | DATETIME | When this session was approved |
| `LAST_DO_AT` | DATETIME | Last DO entry timestamp |
| `IS_CLOSED` | BIT | 1 when `DO_QTY ≥ ALLOC_QTY` — excluded from MSA |

`PEND_QTY` is a **persisted computed column**. It is always consistent with ALLOC_QTY and DO_QTY. Application code never writes it directly.

### How MSA reads it (Step 6)

```python
SELECT RDC, ARTICLE_NUMBER, SUM(PEND_QTY) AS ARS_PEND
FROM ARS_PEND_ALC WITH (NOLOCK)
WHERE IS_CLOSED = 0 AND PEND_QTY > 0
GROUP BY RDC, ARTICLE_NUMBER
```

This result is merged into `msa_pivot` on `(ST_CD, ARTICLE_NUMBER)`. For every match, `PEND_QTY += ARS_PEND`. Then `FNL_Q = max(STK_QTY − PEND_QTY − HOLD_QTY, 0)`.

### Example: FNL_Q change after approve

| Moment | STK_QTY | PEND_QTY | HOLD_QTY | FNL_Q |
|--------|---------|----------|----------|-------|
| Before approve | 200 | 0 | 0 | **200** |
| After approve (ALLOC_QTY=50) | 200 | **50** | 0 | **150** |
| After DO batch 1 (30 units) | 200 | **20** | 0 | **180** |
| After DO batch 2 (20 units) | 200 | **0** | 0 | **200** |

---

## Phase 7 — Daily DO Entry

**Who:** Operations / planning (whoever receives SAP DO confirmations)
**When:** Each day that SAP generates Delivery Orders for approved allocations
**Page:** Pending Allocation → Daily DO Entry (`/pend-alc/do-entry`)

### When to do this

SAP issues Delivery Orders (DOs) 1–5 working days after a warehouse allocation is approved. Each day you receive a DO confirmation file from SAP, enter the quantities in ARS so that the PEND_QTY is reduced accordingly.

**Do this before running the day's MSA** so that the MSA calculation reflects the DO and gives the correct FNL_Q for the next allocation.

### How to enter DO quantities

**Option A — CSV upload**

Prepare a CSV with these columns (aliases accepted):

| Required column | Accepted aliases |
|----------------|-----------------|
| `RDC` | `Receiving Store`, `WERKS` |
| `ARTICLE_NUMBER` | `Material No`, `Material`, `MATNR` |
| `DO_QTY` | `DO Qty`, `Qty`, `Quantity` |

Upload the file. The parser will reject rows where DO_QTY is zero or where any field is blank.

**Option B — Manual entry**

Click **Add Row** for each article, fill in RDC, Article Number, and DO_QTY, then click **Submit DO Update**.

### What happens on submit

`POST /pend-alc/do-update` calls `apply_do_deductions()`:

```sql
-- 1. Update ARS_PEND_ALC
UPDATE P
   SET DO_QTY     = DO_QTY + :do_qty,
       LAST_DO_AT = GETDATE(),
       IS_CLOSED  = CASE WHEN DO_QTY + :do_qty >= ALLOC_QTY THEN 1 ELSE 0 END
FROM ARS_PEND_ALC P
WHERE P.RDC = :rdc AND P.ARTICLE_NUMBER = :article AND P.IS_CLOSED = 0

-- 2. Update HOLD tracking (for TBL/NL hold articles)
UPDATE H
   SET HOLD_REM  = max(HOLD_REM - :do_qty, 0),
       IS_CLOSED = CASE WHEN HOLD_REM - :do_qty <= 0 THEN 1 ELSE 0 END
FROM ARS_NL_TBL_HOLD_TRACKING H
WHERE H.WERKS = :rdc AND H.VAR_ART = :article AND H.IS_CLOSED = 0
```

Both tables are updated in a single connection. The same DO entry closes both the PEND_QTY deduction and the HOLD_QTY deduction simultaneously.

### After DO entry

- The History section on the page shows the row with updated `DO_QTY` and `PEND_QTY`
- Articles with `IS_CLOSED = 1` show a **CLOSED** badge (green)
- Open articles show an **OPEN** badge (amber) with remaining `PEND_QTY`

### Checklist

- [ ] DO file received from SAP (or DO confirmation in your inbox)
- [ ] All articles from today's DO entered (CSV or manual)
- [ ] `pend_alc/overview` shows reduced PEND_QTY totals
- [ ] Fully-covered articles show IS_CLOSED = 1
- [ ] **Run MSA again** after DO entry to get corrected FNL_Q

---

## Phase 8 — BDC Export (Delivery to Warehouse)

**Who:** Operations
**When:** After Approve and before DO cutoff
**Page:** Data Preparation → BDC Creation

BDC Creation reads `ARS_ALLOC_WORKING` (the live allocation) and produces the warehouse upload file. When DO quantities are uploaded through BDC's own delivery-order upload, `apply_do_deductions()` is also called — so BDC uploads and manual Daily DO Entry both correctly update `ARS_PEND_ALC`.

---

## Worked example — end to end

**Article:** 1234567890 (MENS SHIRTS, AW26)
**RDC:** DC01
**Warehouse stock (ET_STORE_STOCK):** 200 units

### Day 1 — Morning

**MSA run (Phase 2):**
- STK_QTY = 200, PEND_QTY = 0, HOLD_QTY = 0
- FNL_Q = **200**

ARS_PEND_ALC: *empty for this article*

**Listing & Allocation (Phase 4):**
- MSA sees 200 → allocator distributes to stores
- Session AW26-S01 allocates 50 units of article 1234567890 at DC01 → ALLOC_QTY = 50
- HOLD_QTY = 0 (it's RL, not TBL)

**Approve (Phase 5):**
- ARS_ALLOC_HISTORY: 50 rows written for this session
- ARS_PEND_ALC: INSERT → `SESSION_ID='AW26-S01', RDC='DC01', ARTICLE='1234567890', ALLOC_QTY=50, DO_QTY=0, PEND_QTY=50`

---

### Day 1 — Evening (MSA re-run)

**MSA (Phase 2):**
- Reads ARS_PEND_ALC → ARS_PEND = 50 for (DC01, 1234567890)
- PEND_QTY = 50
- FNL_Q = max(200 − 50 − 0, 0) = **150**

*Without ARS_PEND_ALC: MSA would still see 200 and could allocate the same 50 units again → double-allocation.*

---

### Day 2 — SAP issues first DO batch: 30 units

**Daily DO Entry (Phase 7):**
- Enter: DC01 / 1234567890 / DO_QTY = 30
- ARS_PEND_ALC updated: DO_QTY = 30, PEND_QTY = 20, IS_CLOSED = 0

**MSA (Phase 2, after DO entry):**
- ARS_PEND = 20
- FNL_Q = max(200 − 20 − 0, 0) = **180**

The 30 units are now in SAP's system; warehouse stock will drop from 200 to 170 on the next stock upload.

---

### Day 3 — SAP issues second DO batch: remaining 20 units

**Daily DO Entry (Phase 7):**
- Enter: DC01 / 1234567890 / DO_QTY = 20
- ARS_PEND_ALC: DO_QTY = 50, PEND_QTY = 0, **IS_CLOSED = 1**

**MSA (Phase 2, after DO entry):**
- IS_CLOSED = 1 → row excluded from Step 6
- ARS_PEND = 0
- STK_QTY now = 150 (warehouse sync updated ET_STORE_STOCK after the 50 DOs)
- FNL_Q = max(150 − 0 − 0, 0) = **150**

The pending deduction is gone. Correct stock is visible. New allocation can proceed.

---

## Daily operations checklist (printable)

### Morning (before allocations)

- [ ] Upload fresh stock snapshot → Data Management → Upload Data
- [ ] Upload fresh sales data (if available)
- [ ] Run MSA → Data Preparation → MSA Stock Calculation
- [ ] Verify `SUM(FNL_Q)` in MSA output is reasonable
- [ ] Check Pending Allocation → Overview: any sessions pending > 5 days? Escalate to SAP team.

### During allocation run

- [ ] Grids are ACTIVE (Grid Builder → check status)
- [ ] Select correct Season (SSN) in Listing page
- [ ] Click Generate — wait for Part 8 completion
- [ ] Review OPT_TYPE distribution (pie chart)
- [ ] Spot-check 5–10 stores in Working tab
- [ ] Review parked run — confirm ALLOC_QTY makes sense

### After approval

- [ ] Check API response: `pend_alc_rows > 0`
- [ ] Pending Allocation → Overview: new session appears with IS_CLOSED = 0
- [ ] Proceed to BDC Creation for warehouse export

### Afternoon (after SAP DO confirmations)

- [ ] Open SAP DO confirmation file
- [ ] Pending Allocation → Daily DO Entry
- [ ] Upload DO CSV (or enter manually)
- [ ] Verify PEND_QTY dropped for covered articles
- [ ] Run MSA again to get corrected FNL_Q before next day's allocation

---

## Key tables and their role

| Table | Written by | Read by | Purpose |
|-------|-----------|---------|---------|
| `ET_STORE_STOCK` | Upload / SAP sync | MSA (Step 1) | Raw warehouse stock |
| `ARS_MSA_VAR_ART` | MSA run | Listing Part 3.55, Allocator | Shippable pool per article |
| `ARS_GRID_MJ` | Grid Builder | Listing Parts 4a–4e | MBQ and store demand reference |
| `ARS_LISTING` | Listing Part 1–3 | Listing Parts 4–8 | Full listing workspace |
| `ARS_LISTING_WORKING` | Listing Part 7 | Allocator (Part 8) | Eligible allocation candidates |
| `ARS_ALLOC_WORKING` | Allocator (Part 8) | Auto-park | Live allocation plan |
| `ARS_ALLOC_PARKED` | Auto-park | Approve/Reject UI | Pending review queue |
| `ARS_ALLOC_HISTORY` | Approve | Reports, pend_alc writer | Permanent allocation record |
| `ARS_PEND_ALC` | Approve (write_pend_alc) | MSA Step 6, DO entry, reports | Anti-double-allocation tracker |
| `ARS_NL_TBL_HOLD_TRACKING` | Allocator (TBL rows) | MSA Step 6.5, DO entry | Hold reservation tracker |
| `ARS_STORE_RANKING` | Listing Part 6 | Allocator | Store priority order |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| FNL_Q = 0 for articles with stock | PEND_QTY inflated | Check `ARS_PEND_ALC` — sessions pending > 5 days without DO. Enter DO or manually close IS_CLOSED=1 if stock already cleared in SAP. |
| Double-allocation complaints from warehouse | ARS_PEND_ALC rows not written on approve | Check server log for `[pend_alc] write failed`. Re-run `scripts/run_012_pend_alc.py` to ensure table exists. |
| PEND_QTY not dropping after DO entry | Wrong RDC or Article format in DO file | Compare RDC codes in DO file vs `ARS_PEND_ALC`. Codes must match exactly (case-sensitive). |
| MSA shows same FNL_Q before and after DO entry | MSA was not re-run after DO entry | Always run MSA after DO entry, before the next allocation. |
| Session pending for 7+ days (no DO) | SAP not issuing DO / blocked at warehouse | Escalate to SAP team. If stock is confirmed cleared in SAP, manually update `IS_CLOSED=1` via Data Editor. |
| Listing runs but 0 alloc rows | MSA FNL_Q = 0 everywhere | PEND_QTY > STK_QTY. Check `ARS_PEND_ALC` for stale open sessions. Enter missing DOs. |
| Grid join warning in listing log | Grid ACTIVE but no matching rows | Regenerate grid for affected MAJ_CAT before re-running listing. |

---

## Navigation quick-reference

| Task | Sidebar path | URL |
|------|-------------|-----|
| Upload stock/sales | Data Management → Upload Data | `/upload` |
| Run MSA | Data Preparation → MSA Stock Calculation | `/msa` |
| Build grids | Data Preparation → Grid Builder | `/data-prep/store-stock` |
| Run Listing & Allocate | Data Preparation → Listing | `/data-prep/listing` |
| Review parked runs | Listing page → Parked Runs (bottom) | `/data-prep/listing` |
| View pending overview | Pending Allocation → Overview | `/pend-alc/overview` |
| Enter daily DOs | Pending Allocation → Daily DO Entry | `/pend-alc/do-entry` |
| Pending allocation report | Reports → Pending Allocation | `/reports/pend-alc` |
| Export BDC | Data Preparation → BDC Creation | `/bdc` |
| Hold dashboard | Reports → Hold Dashboard | `/reports/hold` |
