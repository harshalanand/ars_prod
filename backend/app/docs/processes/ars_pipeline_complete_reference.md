---
title: ARS Pipeline — Complete Reference
category: Allocation
order: 2
source: backend/app/services/rule_engine_pandas.py, backend/app/services/rule_engine_new.py, backend/app/api/v1/endpoints/listing.py
last_reviewed: 2026-05-03
---

# ARS Pipeline — Complete Reference

> **Single source of truth for the full allocation pipeline.**
> Every stage, every formula, every decision point — with worked examples and live database numbers.
> Start with the health block, then use the table of contents to jump to what you need.

---

## Live system health

<!-- @metric sql="SELECT ISNULL(SUM(FNL_Q),0) FROM ARS_MSA_VAR_ART" label="Warehouse shippable units (MSA FNL_Q)" -->

<!-- @metric sql="SELECT COUNT(DISTINCT WERKS) FROM ARS_LISTING_WORKING WHERE LISTED_FLAG=1" label="Stores with eligible OPTs this run" -->

<!-- @metric sql="SELECT ISNULL(SUM(ALLOC_QTY),0) FROM ARS_LISTING_WORKING" label="Total SHIP units allocated" -->

<!-- @metric sql="SELECT ISNULL(SUM(HOLD_QTY),0) FROM ARS_ALLOC_WORKING" label="TBL hold units reserved at warehouse" -->

<!-- @metric sql="SELECT COUNT(*) FROM ARS_LISTING_WORKING WHERE LISTED_FLAG=1 AND ALLOC_STATUS='NOT_ALLOCATED'" label="NOT_ALLOCATED OPTs (investigate if > 0 with HOLD_QTY)" -->

<!-- @metric format="table" sql="SELECT OPT_TYPE, COUNT(*) AS OPTs, SUM(CASE WHEN ALLOC_STATUS='ALLOCATED' THEN 1 ELSE 0 END) AS Alloc_OK, SUM(CASE WHEN ALLOC_STATUS='PARTIAL' THEN 1 ELSE 0 END) AS Partial, SUM(CASE WHEN ALLOC_STATUS='SKIPPED' THEN 1 ELSE 0 END) AS Skipped, SUM(CASE WHEN ALLOC_STATUS='NOT_ALLOCATED' THEN 1 ELSE 0 END) AS Not_Alloc, ISNULL(SUM(ALLOC_QTY),0) AS Ship_Units, ISNULL(SUM(HOLD_QTY),0) AS Hold_Units FROM ARS_LISTING_WORKING WHERE LISTED_FLAG=1 GROUP BY OPT_TYPE ORDER BY OPT_TYPE" label="Allocation breakdown by OPT_TYPE" -->

<!-- @metric format="table" sql="SELECT TOP 10 MAJ_CAT, COUNT(*) AS OPTs, ISNULL(SUM(ALLOC_QTY),0) AS Units FROM ARS_LISTING_WORKING WHERE LISTED_FLAG=1 AND ALLOC_QTY>0 GROUP BY MAJ_CAT ORDER BY Units DESC" label="Top 10 MAJ_CATs by units shipped" -->

<!-- @metric format="table" sql="SELECT TOP 5 ALLOC_REMARKS, COUNT(*) AS OPTs FROM ARS_LISTING_WORKING WHERE ALLOC_STATUS='SKIPPED' AND ALLOC_REMARKS<>'' GROUP BY ALLOC_REMARKS ORDER BY OPTs DESC" label="Top skip reasons this run" -->

---

## Table of Contents

| Section | What it covers |
|---|---|
| [Stage 0 — Pipeline overview](#stage-0--pipeline-overview) | End-to-end map, all tables, timing |
| [Stage 1 — OPT_TYPE classification](#stage-1--opt_type-classification) | MIX / RL / TBC / TBL rules with examples |
| [Stage 2 — Listing rules (R01–R09)](#stage-2--listing-rules-r01r09) | Every rule that gates LISTED_FLAG=1 |
| [Stage 3 — Store ranking (ST_RANK)](#stage-3--store-ranking-st_rank) | Formula, worked 5-store example |
| [Stage 4 — OPT priority (OPT_PRIORITY_RANK)](#stage-4--opt-priority-opt_priority_rank) | Tier + rank with 3-OPT example |
| [Stage 5 — H_REM and PRI_CT_REM](#stage-5--h_rem-and-pri_ct_rem) | Grid satisfaction tracking with example |
| [Stage 6 — Pre-band check](#stage-6--pre-band-check) | Gates evaluated before each OPT_TYPE starts |
| [Stage 7 — The waterfall bands](#stage-7--the-waterfall-bands) | Pool drain, rounds, MBQ cap, full example |
| [Stage 8 — TBL HOLD mechanics](#stage-8--tbl-hold-mechanics) | SHIP/HOLD split formula, hold tracking |
| [Stage 9 — Revalidation after each band](#stage-9--revalidation-after-each-band) | How PRI_CT_REM drops and propagates |
| [Stage 10 — Reflect to working table](#stage-10--reflect-to-working-table) | ALLOC_STATUS rules, write-back |
| [Application review guide](#application-review-guide) | Step-by-step: where to click, what to check |
| [Settings and tuning](#settings-and-tuning) | Every configurable knob and its effect |
| [Troubleshooting decision tree](#troubleshooting-decision-tree) | Symptom → root cause → fix |
| [Key constants reference](#key-constants-reference) | All feature flags and constants |
| [Worked end-to-end example](#worked-end-to-end-example) | One article, 4 stores, every step |

---

## Stage 0 — Pipeline overview

### End-to-end map

```
╔══════════════════════════════════════════════════════════════════════════╗
║  ARS WEEKLY REPLENISHMENT PIPELINE                                        ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                           ║
║  ① UPLOAD          ② MSA CALC         ③ GRID BUILDER   ④ LISTING GEN     ║
║  ─────────         ──────────         ──────────────   ───────────────    ║
║  Store stock  →    FNL_Q per          MBQ, REQ, STK    Part 1  classify   ║
║  Store sales       (RDC, VAR_ART)     per (ST, CAT)    Part 2  build WK   ║
║  Master data       FNL_Q = STK        OPT_PRIORITY     Part 3  grid cols  ║
║                     − PEND_QTY        ST_RANK          Part 4  contrib%   ║
║                         │                  │           Part 5  store rank  ║
║                    ARS_MSA_VAR_ART    ARS_GRID_MJ      Part 6  extra cols  ║
║                         │                  │           Part 7  flag elig.  ║
║                         └──────────────────┘           Part 8  ALLOCATE   ║
║                                  feeds                      │             ║
║                                                    ARS_ALLOC_WORKING      ║
║                                                    ARS_LISTING_WORKING    ║
║                                                    .ALLOC_QTY             ║
║                                                         │                 ║
║  ⑤ APPROVE         ⑥ PEND_ALC         ⑦ DO ENTRY      ⑧ CLOSE & REPEAT   ║
║  ──────────        ──────────         ──────────      ─────────────────   ║
║  Review Alloc  →   ARS_PEND_ALC  →    Daily DO   →   IS_CLOSED=1          ║
║  Approve run       PEND_QTY set       entry           FNL_Q restored      ║
║                    MSA deducts        SAP ships        in next MSA run     ║
╚══════════════════════════════════════════════════════════════════════════╝
```

### Key tables

| Stage | Table | Grain | Key columns |
|---|---|---|---|
| MSA run | `ARS_MSA_VAR_ART` | (RDC, VAR_ART) | FNL_Q, PEND_QTY |
| Grid Builder | `ARS_CALC_ST_MAJ_CAT` | (WERKS, MAJ_CAT) | MJ_MBQ, MJ_REQ, MJ_STK_TTL, ACS_D |
| Grid Builder | `ARS_STORE_RANKING` | (WERKS, MAJ_CAT) | ST_RANK, W_SCORE, FILL_RATE |
| Listing | `ARS_LISTING` | (WERKS, MAJ_CAT, GEN_ART, CLR) | OPT_TYPE, LISTING |
| Listing | `ARS_LISTING_WORKING` | (WERKS, MAJ_CAT, GEN_ART, CLR) | LISTED_FLAG, ALLOC_QTY, HOLD_QTY, ALLOC_STATUS |
| Allocation | `ARS_ALLOC_WORKING` | (WERKS, MAJ_CAT, GEN_ART, CLR, VAR_ART, SZ) | SHIP_QTY, HOLD_QTY, POOL_CONSUMED, ALLOC_STATUS |
| Hold tracking | `ARS_NL_TBL_HOLD_TRACKING` | (WERKS, VAR_ART) | HOLD_REM |
| Pending | `ARS_PEND_ALC` | (WERKS, VAR_ART) | PEND_QTY, IS_CLOSED |

### Timing (typical run, 320 stores, 242 MAJ_CATs)

| Step | Typical time | Notes |
|---|---|---|
| MSA calculation | 2–4 min | Depends on stock file size |
| Grid Builder | 1–2 min | Usually cached |
| Listing Parts 1–7 | 30–60 sec | SQL transforms |
| Listing Part 8 (pandas allocation) | 2–5 min | 4 parallel workers |
| Total | ~7–12 min | |

---

## Stage 1 — OPT_TYPE classification

Every combination `(WERKS × MAJ_CAT × GEN_ART_NUMBER × CLR)` — called an **OPT** — is tagged with one of four types at the start of Listing Generation.

### The four types

| OPT_TYPE | When it applies | What allocation does |
|---|---|---|
| **MIX** | Article has overlapping stock AND sale in conflicting grids — ambiguous grid coverage | **Excluded** — never allocated, never LISTED |
| **RL** | Article is actively selling at this store (stock exists, sales history active) | Ships units to store. No warehouse hold. |
| **TBC** | Partial situation — store has some stock or some sales but not clearly RL or TBL | Ships units to store. No warehouse hold. |
| **TBL** | Brand new launch — article never sold at this store or stock is zero with no history | Ships units to store **AND** reserves a HOLD_QTY buffer at the warehouse |

### How the type is determined — worked example

Suppose store HN14 has article 1116112544 colour NAVY in MAJ_CAT M_TEES_HS:

```
Store STK  = 0   (no stock at HN14 for this article)
SALE_7D    = 0   (no sales in last 7 days at HN14)
MSA_FNL_Q  = 120 (warehouse has 120 units available)
```

→ New article, zero stock, zero sale history → **OPT_TYPE = TBL**

Now same article at store HN22:

```
Store STK  = 8   (has 8 units on shelf)
SALE_7D    = 12  (sold 12 units in 7 days)
OPT_MBQ    = 30  (target stock = 30 units)
STK/MBQ    = 27% (well below target)
```

→ Actively selling, below target → **OPT_TYPE = RL**

### Review queries

```sql
-- OPT_TYPE distribution this run
SELECT OPT_TYPE, COUNT(*) AS OPTs
FROM ARS_LISTING
GROUP BY OPT_TYPE ORDER BY OPTs DESC;

-- Articles tagged MIX (not allocated — check if classification is correct)
SELECT TOP 20 WERKS, GEN_ART_NUMBER, CLR, OPT_TYPE
FROM ARS_LISTING WHERE OPT_TYPE = 'MIX';

-- Stores with TBL articles (new launches)
SELECT WERKS, COUNT(*) AS tbl_opts,
       COUNT(DISTINCT GEN_ART_NUMBER) AS articles
FROM ARS_LISTING WHERE OPT_TYPE = 'TBL'
GROUP BY WERKS ORDER BY tbl_opts DESC;
```

---

## Stage 2 — Listing rules (R01–R09)

**Where:** `rule_engine_new.py` → `_stage_a_apply_rules`

Each rule appends a code to `LISTED_REASON`. If `LISTED_REASON` is empty, `LISTED_FLAG = 1` (eligible). Any non-empty reason → `LISTED_FLAG = 0` (excluded).

Rules run **in parallel** (all applied in one SQL UPDATE, reasons concatenated). An OPT failing multiple rules shows e.g. `R04_MSA_POS;R05_REQ_POS;`.

### Rule table

| Rule | Code | What it checks | Blocks when |
|---|---|---|---|
| R01 | `R01_LISTING` | `LISTING` flag column on the row | `LISTING <> 1` — admin turned off this article |
| R02 | `R02_NOT_MIX` | OPT_TYPE | `OPT_TYPE = 'MIX'` — ambiguous article |
| R04 | `R04_MSA_POS` | Warehouse stock | `MSA_FNL_Q <= 0 AND RL_HOLD_QTY <= 0` — nothing to ship |
| R05 | `R05_REQ_POS` | Store requirement | `OPT_REQ_WH < 1` — store needs less than 1 unit |
| R06 | `R06_PRI_100` | Primary grid coverage % | `PRI_CT% < 100` for enforced types (TBL always; RL/TBC if gate is ON) |
| R07 | `R07_VAR_RATIO_TBL` | Size availability for TBL | `VAR_FNL_COUNT / VAR_COUNT < size_threshold (0.6) AND VAR_FNL_COUNT < min_size_count (3)` |
| R08 | `R08_MJ_REQ_BOOSTED` | Boosted requirement gap | When PRI gate is OFF + cap configured: `MJ_MBQ × cap% − STK < 0.5 × ACS_D` |
| R09 | `R09_TBL_TRIVIAL` | TBL trivial requirement | TBL: `MJ_REQ < 0.5 × MAX_DAILY_SALE` — too small to bother launching |

### Worked example — why an OPT gets dropped

Store HN07, article 1116113400, CLR=BLACK, OPT_TYPE=TBL:

```
MSA_FNL_Q  = 45      ✓  passes R04
OPT_REQ_WH = 24      ✓  passes R05
PRI_CT%    = 66.7    ✗  fails R06  (66.7 < 100, TBL always enforces)
VAR_FNL_COUNT = 4
VAR_COUNT     = 5
ratio = 4/5 = 0.80   ✓  passes R07 (0.80 ≥ 0.6, even though < min_count)
```

Result: `LISTED_REASON = 'R06_PRI_100;'`, `LISTED_FLAG = 0` → OPT excluded.

The store had only 2 of 3 primary grids with meaningful remaining demand. The article cannot get a balanced size set, so it is skipped in Stage A.

### Review queries

```sql
-- Why specific OPTs are not listed
SELECT WERKS, GEN_ART_NUMBER, CLR, OPT_TYPE, LISTED_FLAG, LISTED_REASON
FROM ARS_LISTING_WORKING
WHERE LISTED_FLAG = 0
ORDER BY LISTED_REASON, WERKS;

-- Count dropped by rule
SELECT LISTED_REASON, COUNT(*) AS dropped
FROM ARS_LISTING_WORKING WHERE LISTED_FLAG=0 AND LISTED_REASON<>''
GROUP BY LISTED_REASON ORDER BY dropped DESC;

-- OPTs failing R06 (PRI_CT% gate) — by store and type
SELECT OPT_TYPE, WERKS, COUNT(*) AS blocked,
       AVG(TRY_CAST([PRI_CT%] AS FLOAT)) AS avg_pri_ct
FROM ARS_LISTING_WORKING
WHERE LISTED_REASON LIKE '%R06_PRI_100%'
GROUP BY OPT_TYPE, WERKS ORDER BY blocked DESC;
```

---

## Stage 3 — Store ranking (ST_RANK)

**Where:** `listing.py` Part 6 → writes `ARS_STORE_RANKING` → hydrated onto `ARS_LISTING_WORKING`

ST_RANK answers: **within one MAJ_CAT, which store should drink from the warehouse pool first?**

### The formula step-by-step

```
For each (WERKS, MAJ_CAT):

  Step 1 — Aggregate (ignoring MIX rows)
    MJ_REQ    = store's total remaining requirement in the category
    MJ_MBQ    = store's category-level target stock (Maximum Buy Quantity)
    MJ_STK    = store's current total stock in the category
    ACS_D     = Average Customer Sale per Day (for the store × category)

  Step 2 — Fill rate
    FILL_RATE = MJ_STK / MJ_MBQ
    (If MJ_MBQ = 0,  FILL_RATE = 0)

  Step 3 — Within-category sub-ranks
    REQ_RANK  = ROW_NUMBER() OVER (PARTITION BY MAJ_CAT ORDER BY MJ_REQ ASC)
    — stores with SMALLER requirement get rank 1
    — logic: smaller stores are easier to "complete" with limited pool

    FILL_RANK = ROW_NUMBER() OVER (PARTITION BY MAJ_CAT ORDER BY FILL_RATE DESC)
    — stores with HIGHER fill rate get rank 1
    — logic: a store already well-stocked should not be skipped

  Step 4 — Weighted score
    W_SCORE = REQ_RANK × req_wt + FILL_RANK × fill_wt
    defaults: req_wt = 0.40,  fill_wt = 0.60

  Step 5 — Final rank
    ST_RANK = ROW_NUMBER() OVER (PARTITION BY MAJ_CAT ORDER BY W_SCORE DESC)
    — highest W_SCORE → ST_RANK = 1 (goes first in the pool)
```

### Worked example — 5 stores in MAJ_CAT M_TEES_HS

| Store | MJ_REQ | MJ_STK | MJ_MBQ | FILL_RATE | REQ_RANK | FILL_RANK | W_SCORE | ST_RANK |
|---|---|---|---|---|---|---|---|---|
| HN07 | 12 | 19 | 20 | 0.95 | 1 | 1 | 1×0.4+1×0.6=**1.0** | **1** |
| HN14 | 18 | 14 | 20 | 0.70 | 2 | 3 | 2×0.4+3×0.6=**2.6** | 3 |
| HN22 | 24 | 12 | 40 | 0.30 | 3 | 5 | 3×0.4+5×0.6=**4.2** | 5 |
| HN35 | 16 | 15 | 20 | 0.75 | ... | ... | ... | ... |
| HN42 | 10 | 18 | 20 | 0.90 | ... | ... | ... | ... |

*Full calculation not shown for HN35/HN42, but the pattern is clear:*
HN07 has the **smallest requirement** AND the **highest fill rate** → it drinks first (ST_RANK=1).
HN22 has the **biggest gap** AND the **lowest fill** → it waits last (ST_RANK=5).

**Why this seems counterintuitive:** You might expect the most-empty store to go first. But the design intent is to "top up" stores that are close to target first — those are easiest to "complete" with limited stock. Stores that need a lot will receive what's left.

If you want to favour under-stocked stores, increase `req_wt` and decrease `fill_wt` in the tuning panel.

### Review queries

```sql
-- Full ST_RANK table for one MAJ_CAT
SELECT WERKS, MJ_REQ, MJ_STK_TTL AS STK, MJ_MBQ,
       ROUND(MJ_STK_TTL * 1.0 / NULLIF(MJ_MBQ,0), 2) AS FILL_RATE,
       REQ_RANK, FILL_RANK, W_SCORE, ST_RANK
FROM ARS_STORE_RANKING
WHERE MAJ_CAT = 'M_TEES_HS'
ORDER BY ST_RANK;

-- Stores ranked first (going to get pool first) across all MAJ_CATs
SELECT MAJ_CAT, WERKS, W_SCORE, MJ_REQ, MJ_STK_TTL
FROM ARS_STORE_RANKING WHERE ST_RANK = 1
ORDER BY MAJ_CAT;

-- Compare two stores' rank for the same category
SELECT a.WERKS, a.ST_RANK, a.MJ_REQ, a.FILL_RATE, a.W_SCORE
FROM ARS_STORE_RANKING a
WHERE a.MAJ_CAT = 'M_TEES_HS'
  AND a.WERKS IN ('hn07', 'hn22')
ORDER BY a.ST_RANK;
```

---

## Stage 4 — OPT priority (OPT_PRIORITY_RANK)

**Where:** `rule_engine_new.py` → `_stage_a_assign_tier` then `_stage_a_assign_rank`

OPT_PRIORITY_RANK answers: **for a given store, which OPT should get pool allocation first?**

This is separate from ST_RANK. ST_RANK is about which *store* goes first. OPT_PRIORITY_RANK is about which *article* inside a store goes first.

### Step 1 — OPT_PRIORITY_TIER (focus bucket)

```sql
OPT_PRIORITY_TIER =
    CASE WHEN FOCUS_WO_CAP = 1 THEN 1   -- focus article, no budget cap
         WHEN FOCUS_W_CAP  = 1 THEN 2   -- focus article, with budget cap
         ELSE                       3   -- regular article
    END
```

TIER 1 articles are always placed before TIER 2, which are placed before TIER 3.
If `ENABLE_FOCUS_TIERING = False`, every row is set to TIER 3 (flat priority).

### Step 2 — OPT_PRIORITY_RANK (within store × OPT_TYPE)

```
ORDER BY:
  1. OPT_PRIORITY_TIER ASC        (focus before regular)
  2. SEC_CT% DESC                 (higher secondary grid coverage first)
  3. MAX_DAILY_SALE DESC          (higher sales velocity first)
  4. OPT_REQ_WH DESC              (bigger requirement with hold first)
  5. GEN_ART_NUMBER ASC           (deterministic tie-breaker)
  6. CLR ASC                      (deterministic tie-breaker)

PARTITION BY (WERKS, OPT_TYPE)
```

**Partition is per (WERKS, OPT_TYPE):** store HN07's TBL rank-1 article is independent of store HN22's TBL rank-1 article. They don't compete on this ranking — they compete at pool-drain time when both want the same size.

### Worked example — 3 TBL articles at store HN07

| GEN_ART | CLR | TIER | SEC_CT% | MAX_DAILY_SALE | OPT_REQ_WH | OPT_PRIORITY_RANK |
|---|---|---|---|---|---|---|
| 1116112544 | NAVY | 1 (focus) | 88 | 4.2 | 48 | **1** |
| 1116113400 | BLACK | 3 (regular) | 95 | 5.1 | 36 | **2** |
| 1116111201 | WHITE | 3 (regular) | 72 | 3.0 | 30 | **3** |

Article 1116112544 is rank 1 because it is a focus article (TIER=1). Among Tier-3 articles, 1116113400 beats 1116111201 because it has higher `SEC_CT%` (95 > 72).

**Effect:** When the pool is limited, article 1116112544 gets its sizes filled first. If pool runs out, 1116111201 may get nothing.

### Review queries

```sql
-- All OPTs for one store, showing their priority
SELECT OPT_TYPE, GEN_ART_NUMBER, CLR,
       OPT_PRIORITY_TIER, [SEC_CT%], MAX_DAILY_SALE, OPT_REQ_WH,
       OPT_PRIORITY_RANK
FROM ARS_LISTING_WORKING
WHERE WERKS = 'hn07' AND LISTED_FLAG = 1
ORDER BY OPT_TYPE, OPT_PRIORITY_RANK;

-- Focus articles (TIER=1) across all stores
SELECT WERKS, GEN_ART_NUMBER, CLR, OPT_TYPE, OPT_PRIORITY_RANK
FROM ARS_LISTING_WORKING
WHERE OPT_PRIORITY_TIER = 1 AND LISTED_FLAG = 1
ORDER BY WERKS, OPT_PRIORITY_RANK;

-- The combined ordering the waterfall sees (pool competition order)
-- for a specific article+size:
SELECT WERKS, OPT_PRIORITY_RANK, ST_RANK,
       ROW_NUMBER() OVER (ORDER BY OPT_PRIORITY_RANK, ISNULL(ST_RANK,999999)) AS pool_order
FROM ARS_ALLOC_WORKING
WHERE GEN_ART_NUMBER = 1116112544 AND SZ = 'M'
ORDER BY pool_order;
```

---

## Stage 5 — H_REM and PRI_CT_REM

**Where:** `rule_engine_new.py` → `_init_rem_columns` (seed) + `_revalidate_after_band` (update)

These two values track **how much of the store's primary demand is still unmet** as allocation progresses. They are the input to the PRI_CT gate in Stage 6.

### What is a "primary grid"?

A primary grid is a sub-dimension of a store's demand, tracked separately. For example, a MAJ_CAT with primary grids for Size Groups (S/M, L/XL, 2XL+) or Colour Families (Darks, Brights, Whites).

Each primary grid has:
- `GH` column = 1 if this store needs stock in this grid (from Grid Builder)
- `REQ_<grid>` column = how many units the store needs from this grid
- `H_<grid>_REM` = 1 if remaining requirement is still meaningful, 0 if satisfied

### H_REM formula

```
H_<grid>_REM = 1  if  REQ_<grid>_REM > ACS_SKIP_FACTOR × ACS_D
                       (ACS_SKIP_FACTOR = 0.5 by default)
             = 0  otherwise (grid is effectively satisfied)
```

**In plain English:** if the remaining requirement in this grid is still more than half a day's average sales, the grid is still "hungry" (H_REM=1). Below that threshold, it's considered satisfied.

### PRI_CT_REM formula

```
PRI_CT_REM = (Σ grids where H_<grid>_REM = 1 AND GH = 1)
             ───────────────────────────────────────────── × 100
             (Σ grids where GH = 1)
```

**Range:** 0.0 to 100.0
- `100.0` = every primary grid still has meaningful remaining demand
- `66.7` = 2 out of 3 primary grids still hungry
- `0.0` = all primary grids are satisfied (store doesn't need any more)

### Worked example — how PRI_CT_REM drops

Store HN22, MAJ_CAT M_TEES_HS, 3 primary grids (S_GROUP_1, S_GROUP_2, S_GROUP_3):

**Before any allocation:**

| Grid | GH | REQ_REM | ACS_D | H_REM |
|---|---|---|---|---|
| S_GROUP_1 | 1 | 12 | 4 | 1 (12 > 2.0) |
| S_GROUP_2 | 1 | 8 | 4 | 1 (8 > 2.0) |
| S_GROUP_3 | 1 | 6 | 4 | 1 (6 > 2.0) |

`PRI_CT_REM = 3/3 × 100 = 100.0` → ✓ all grids active

**After RL allocation ships 10 units to S_GROUP_1 and 7 units to S_GROUP_2:**

| Grid | GH | REQ_REM | ACS_D | H_REM |
|---|---|---|---|---|
| S_GROUP_1 | 1 | 2 | 4 | 0 (2 ≤ 2.0) ← satisfied |
| S_GROUP_2 | 1 | 1 | 4 | 0 (1 ≤ 2.0) ← satisfied |
| S_GROUP_3 | 1 | 6 | 4 | 1 (6 > 2.0) ← still hungry |

`PRI_CT_REM = 1/3 × 100 = 33.3`

**Effect on TBL:** When TBL's pre-band check runs, it sees `PRI_CT_REM = 33.3 < 100` → this TBL OPT at HN22 gets `ALLOC_STATUS = SKIPPED`, `ALLOC_REMARKS = ' SKIP_PRI_BROKEN;'`. The pool and hold_dict are not touched.

### Review queries

```sql
-- PRI_CT_REM distribution for TBL OPTs (after run)
SELECT PRI_CT_REM, COUNT(*) AS OPTs, ALLOC_STATUS
FROM ARS_LISTING_WORKING
WHERE OPT_TYPE = 'TBL' AND LISTED_FLAG = 1
GROUP BY PRI_CT_REM, ALLOC_STATUS
ORDER BY PRI_CT_REM;

-- OPTs still at PRI_CT_REM = 100 (could get TBL allocation)
SELECT WERKS, GEN_ART_NUMBER, CLR, PRI_CT_REM, MJ_REQ_REM
FROM ARS_LISTING_WORKING
WHERE OPT_TYPE = 'TBL' AND PRI_CT_REM = 100 AND LISTED_FLAG = 1;
```

---

## Stage 6 — Pre-band check

**Where:** `rule_engine_pandas.py` → `_pre_band_check`

Called once before the **first round** of each OPT_TYPE (`RL` → `TBC` → `TBL`). Actively evaluates two gates using the **current** working_df values (not just propagating already-skipped rows).

### Gate 1 — PRI_CT_REM gate (enforced types)

**Enforced types:**
- TBL: **always enforced** (cannot be turned off)
- RL: enforced when `pri_ct_check_rl = True` (UI toggle)
- TBC: enforced when `pri_ct_check_tbc = True` (UI toggle)

```
IF OPT_TYPE in enforced_set AND PRI_CT_REM < 100:
    → ALLOC_STATUS = 'SKIPPED'
    → ALLOC_REMARKS += ' SKIP_PRI_BROKEN;'
```

**Why this gate exists:** After RL bands run and fill some primary grids, TBL OPTs for the same store may find that their primary coverage is no longer 100%. Launching a new article into a store where the primary demand is already partially satisfied produces an unbalanced size assortment. The gate prevents this.

**Example:**
- Before RL runs: TBL article at HN22 has `PRI_CT_REM = 100` → would be eligible
- RL runs 3 rounds, ships to grids 1 and 2
- Revalidation updates `PRI_CT_REM = 33.3`
- Pre-band check for TBL fires: `33.3 < 100` → SKIP

### Gate 2 — Store-broken gate (cross-type)

```
remaining_types = [current OPT_TYPE, all later OPT_TYPEs]
e.g. when processing TBC → remaining = [TBC, TBL]

IF OPT_TYPE in remaining_types AND MJ_REQ_REM < 0.5 × ACS_D:
    → ALLOC_STATUS = 'SKIPPED'
    → ALLOC_REMARKS += ' SKIP_STORE_BROKEN;'
```

**Why cross-type:** Once a store's overall remaining category requirement is tiny (< half a day's sales), it has effectively been satisfied by earlier OPT_TYPEs. Allocating more — including launching TBL articles — would over-supply the store.

**Example:**
- Store HN07, MAJ_CAT M_TEES_HS: `ACS_D = 8`
- After RL runs, `MJ_REQ_REM = 3`
- Check: `3 < 0.5 × 8 = 4` → TRUE
- When pre-band check runs for TBC: all HN07 M_TEES_HS rows (TBC + TBL) → SKIPPED

### Propagation step

After both gates are applied to working_df, all newly-SKIPPED OPTs are propagated to the size-level alloc_df:

```python
skipped_opts = working_df[working_df.ALLOC_STATUS == 'SKIPPED'][OPT_KEYS]
→ alloc_df rows matching those OPT_KEYS → ALLOC_STATUS = 'SKIPPED', SKIP_REASON = 'REVALIDATION_SKIP'
```

### Review queries

```sql
-- Count and type of each skip reason
SELECT
  CASE
    WHEN ALLOC_REMARKS LIKE '%SKIP_PRI_BROKEN%'    THEN 'PRI_CT gate'
    WHEN ALLOC_REMARKS LIKE '%SKIP_STORE_BROKEN%'  THEN 'Store broken (cross-type)'
    WHEN ALLOC_REMARKS LIKE '%REVALIDATION_SKIP%'  THEN 'Propagated skip'
    ELSE 'Other / Stage A'
  END AS skip_type,
  OPT_TYPE, COUNT(*) AS OPTs
FROM ARS_LISTING_WORKING
WHERE ALLOC_STATUS = 'SKIPPED'
GROUP BY
  CASE WHEN ALLOC_REMARKS LIKE '%SKIP_PRI_BROKEN%' THEN 'PRI_CT gate'
       WHEN ALLOC_REMARKS LIKE '%SKIP_STORE_BROKEN%' THEN 'Store broken (cross-type)'
       WHEN ALLOC_REMARKS LIKE '%REVALIDATION_SKIP%' THEN 'Propagated skip'
       ELSE 'Other / Stage A' END,
  OPT_TYPE
ORDER BY OPTs DESC;
```

---

## Stage 7 — The waterfall bands

**Where:** `rule_engine_pandas.py` → `_run_majcat_waterfall` → `_run_band`

### The nesting structure

```
FOR each MAJ_CAT (parallelised across 4 workers):
  │
  ├── Build pool_dict: {(RDC,MAJ_CAT,GEN_ART,CLR,VAR_ART,SZ) → FNL_Q}
  │
  FOR each OPT_TYPE in [RL, TBC, TBL]:
    │
    ├── _pre_band_check()   ← skip PRI_CT < 100 and store-broken rows
    │
    FOR each round r = 1 … max_I_ROD:
      │
      ├── _run_band(ot, r)    ← all stores compete simultaneously
      │    ├── compute need_pool = r × SZ_MBQ − STK − POOL_CONSUMED
      │    ├── for TBL: consume hold_dict before pool
      │    ├── apply MBQ budget cap (if configured)
      │    ├── sort by (POOL_KEYS → ST_RANK → OPT_PRIORITY_RANK)
      │    ├── cumsum drain: take_pool = min(need_pool, pool_rem)
      │    ├── split SHIP / HOLD (TBL only)
      │    └── decrement pool_dict
      │
      └── _revalidate_after_band()  ← update REQ_REM, H_REM, PRI_CT_REM
```

### What is I_ROD?

`I_ROD` (Integer Rounds of Demand) is the number of rounds a size participates in. Round 1 = 1 day's demand (`1 × SZ_MBQ`). Round 2 = 2 days' demand (`2 × SZ_MBQ`). Etc.

All stores compete in Round 1 simultaneously. Only after every store has had its Round 1 allocation does Round 2 begin. This is **fair**: no store monopolises the pool; each gets one "bite" per round.

### Pool drain mechanics — step by step

**Pool key grain:** `(RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ)`

Within one band (one OPT_TYPE × one round), all rows that want the same pool key compete:

```
Step 1 — Compute need_pool per row
  RL/TBC:  need_pool = r × SZ_MBQ − SZ_STK − POOL_CONSUMED_SO_FAR
  TBL:     need_pool = SZ_MBQ_WH + (r−1) × SZ_MBQ − SZ_STK − POOL_CONSUMED
           (SZ_MBQ_WH = SZ_MBQ + hold_fraction — includes the hold buffer)

Step 2 — Sort by priority
  primary sort: POOL_KEYS  (so we work within one pool key at a time)
  secondary:    ST_RANK ASC  (lower rank number = higher priority = goes first)
  tertiary:     OPT_PRIORITY_RANK ASC  (lower = more important OPT for this store)
  final:        WERKS ASC  (stable tie-breaker)

Step 3 — Cumulative demand within each pool key
  cum_demand = CUMSUM(need_pool) sorted as above

Step 4 — Allocate
  cum_prev  = cum_demand − need_pool
  remaining = max(0,  FNL_Q_REM − cum_prev)
  take_pool = min(need_pool, remaining)

Step 5 — Decrement pool
  pool_dict[key] −= sum(take_pool for this key)
```

### Worked example — 3 stores competing for size M pool

Article 1116112544, CLR=NAVY, SZ=M, RDC=DW01: `FNL_Q = 20 units`

Round 1, OPT_TYPE=TBL:

| Store | ST_RANK | OPT_PR_RANK | SZ_MBQ_WH | SZ_STK | need_pool | cum_demand | take_pool | pool_left |
|---|---|---|---|---|---|---|---|---|
| HN07 | 1 | 1 | 10 | 0 | 10 | 10 | **10** | 10 |
| HN14 | 2 | 1 | 10 | 2 | 8 | 18 | **8** | 2 |
| HN22 | 3 | 1 | 10 | 0 | 10 | 28 | **2** (pool exhausted) | 0 |

**Results:**
- HN07: take=10. SZ_MBQ=8, SZ_MBQ_WH=10 → SHIP=8, HOLD=2. `ALLOC_STATUS=ALLOCATED`
- HN14: take=8. SZ_MBQ=6 (need_ship based on remaining after stk=2), HOLD=2. `ALLOC_STATUS=ALLOCATED`
- HN22: take=2 (partial). All 2 units go to SHIP (need_ship=10, take=2, so all ships). `ALLOC_STATUS=PARTIAL`

Pool after this band: `pool_dict[(DW01, M_TEES_HS, 1116112544, NAVY, VA_001, M)] = 0`

### MBQ budget cap (RL and TBC)

When `pri_ct_check_rl = False` (PRI gate OFF) and `rl_mbq_cap_pct > 0`, each store has a spending budget:

```
budget = max(0,  MJ_MBQ × (rl_mbq_cap_pct / 100)  −  MJ_STK_TTL)
```

**The MJ_STK_TTL subtraction is critical:** it deducts what the store already has, so the cap is "net capacity" not "gross target".

**Example:** Store HN07, M_TEES_HS:
```
MJ_MBQ        = 80
rl_mbq_cap_pct = 90%
MJ_STK_TTL    = 30 (currently has 30 units on shelf)

budget = max(0,  80 × 0.90 − 30) = max(0,  72 − 30) = 42 units
```

The store can receive at most 42 more RL units total across all articles in M_TEES_HS. Once `Σ SHIP_QTY ≥ 42`, further RL rows are capped out.

### Review queries

```sql
-- Pool utilisation by RDC and article
SELECT RDC, GEN_ART_NUMBER, CLR, SZ,
       SUM(SHIP_QTY + HOLD_QTY) AS pool_taken,
       MAX(FNL_Q_REM) AS pool_rem
FROM ARS_ALLOC_WORKING
GROUP BY RDC, GEN_ART_NUMBER, CLR, SZ
ORDER BY pool_taken DESC;

-- Stores that got partial (pool ran out on them)
SELECT WERKS, GEN_ART_NUMBER, CLR, SZ,
       SHIP_QTY, HOLD_QTY, ALLOC_STATUS, ALLOC_ROUND
FROM ARS_ALLOC_WORKING
WHERE ALLOC_STATUS = 'PARTIAL'
ORDER BY WERKS, GEN_ART_NUMBER, SZ;

-- Round-by-round allocation progress for one OPT
SELECT ALLOC_ROUND, ALLOC_WAVE, SUM(SHIP_QTY) ship, SUM(HOLD_QTY) hold
FROM ARS_ALLOC_WORKING
WHERE GEN_ART_NUMBER = 1116112544 AND CLR = 'NAVY'
GROUP BY ALLOC_ROUND, ALLOC_WAVE
ORDER BY ALLOC_ROUND;
```

---

## Stage 8 — TBL HOLD mechanics

**Where:** `rule_engine_pandas.py` → `_run_band` (TBL section)

For TBL articles only, the pool take is split into two parts: what goes to the **store** today (SHIP_QTY) and what stays at the **warehouse** as a buffer (HOLD_QTY).

### The SHIP/HOLD split formula

```
OPT_MBQ_WH = OPT_MBQ + (hold_days × MAX_DAILY_SALE)

For a TBL row, pool take = take_pool units:

  need_ship = r × SZ_MBQ − SZ_STK − SHIP_QTY_SO_FAR
             (how many units the store actually needs to ship today)

  ROUND_SHIP = min(take_pool, need_ship)      ← goes to store
  ROUND_HOLD = max(take_pool − need_ship, 0)  ← stays at warehouse

  POOL_CONSUMED += ROUND_SHIP + ROUND_HOLD
```

**Example:** `hold_days = 3`, article MAX_DAILY_SALE = 2 units/day

```
SZ_MBQ    = 8   (store needs 8 units in this size)
SZ_MBQ_WH = 8 + 3×2 = 14  (warehouse takes 14 to cover store + 3-day hold)
SZ_STK    = 0

Round 1 take_pool = 14 (full amount available and needed)
  need_ship = 1 × 8 − 0 − 0 = 8
  ROUND_SHIP = min(14, 8) = 8   → sent to store today
  ROUND_HOLD = 14 − 8 = 6       → held at warehouse
```

The **store receives 8 units**. The **warehouse holds 6 units** as a safety buffer in case the launch sells faster than expected.

### Existing hold consumption (ARS_NL_TBL_HOLD_TRACKING)

If a prior run already reserved hold for this store + article, that existing hold is consumed first before drawing from the FNL_Q pool:

```
hold_dict[(WERKS, VAR_ART)] = HOLD_REM  (from ARS_NL_TBL_HOLD_TRACKING)

For each TBL row:
  hold_avail = hold_dict.get((WERKS, VAR_ART), 0)
  take_hold  = min(hold_avail, need_pool)
  need_pool  = need_pool − take_hold     ← reduced pool draw
  need_ship  = need_ship − take_hold     ← hold covers part of ship demand

After the band:
  hold_dict[(WERKS, VAR_ART)] −= take_hold  (hold is consumed)
```

**Why this matters:** Without hold tracking, every run would draw fresh pool even when the warehouse already holds units for that store. The tracking prevents double-counting and ensures the pool drain reflects only the true additional demand.

**This applies only to TBL.** RL and TBC have no hold tracking — their demand is always satisfied directly from the pool.

### Worked example — hold tracking

Store HN14 has prior hold for VAR_ART=VA_001_M: `HOLD_REM = 6`

Current run, TBL Round 1:
```
need_pool = 14  (as computed above)
hold_avail = 6
take_hold  = min(6, 14) = 6
need_pool  = 14 − 6 = 8   ← only 8 from pool
need_ship  = 8 − 6 = 2    ← only 2 need to ship (hold covers 6)

Pool draw = 8 (not 14)
SHIP_QTY  = 2
HOLD_QTY  = 6  (from existing hold tracking + pool top-up split)
hold_dict[(HN14, VA_001_M)] = 6 − 6 = 0  ← hold fully consumed
```

### Review queries

```sql
-- All TBL ship+hold this run
SELECT WERKS, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
       SHIP_QTY, HOLD_QTY,
       SHIP_QTY + HOLD_QTY AS pool_consumed
FROM ARS_ALLOC_WORKING
WHERE OPT_TYPE = 'TBL'
  AND (SHIP_QTY > 0 OR HOLD_QTY > 0)
ORDER BY HOLD_QTY DESC;

-- Hold tracking balance before this run
SELECT WERKS, VAR_ART, HOLD_REM
FROM ARS_NL_TBL_HOLD_TRACKING
ORDER BY HOLD_REM DESC;

-- TBL articles with HOLD but no SHIP (pure-hold rows — should show ALLOCATED not NOT_ALLOCATED)
SELECT WERKS, GEN_ART_NUMBER, CLR, SZ,
       SHIP_QTY, HOLD_QTY, ALLOC_STATUS
FROM ARS_ALLOC_WORKING
WHERE OPT_TYPE = 'TBL' AND SHIP_QTY = 0 AND HOLD_QTY > 0;

-- TBL OPT-level SHIP vs HOLD totals
SELECT WERKS, GEN_ART_NUMBER, CLR,
       SUM(SHIP_QTY) AS total_ship,
       SUM(HOLD_QTY) AS total_hold,
       SUM(SHIP_QTY + HOLD_QTY) AS total_pool
FROM ARS_ALLOC_WORKING
WHERE OPT_TYPE = 'TBL'
GROUP BY WERKS, GEN_ART_NUMBER, CLR
ORDER BY total_pool DESC;
```

---

## Stage 9 — Revalidation after each band

**Where:** `rule_engine_pandas.py` → `_revalidate_after_band`

Called after every single round × OPT_TYPE band. Updates the working_df shadow columns so the next round starts with accurate remaining-requirement values.

### What gets updated

```
(1) MSA_FNL_Q_REM  ← subtract ROUND_SHIP + ROUND_HOLD per OPT
(2) REQ_<grid>_REM ← subtract ROUND_SHIP per primary grid (grain varies per grid)
(3) H_<grid>_REM   ← recompute: 1 if REQ_REM > 0.5×ACS_D else 0
(4) PRI_CT_REM     ← recompute: Σ(H_REM) / Σ(GH) × 100
(5) Skip rules     ← re-apply PRI_CT and store-broken gates for pending OPTs
```

**Important:** If `band_take_total = 0` (no units moved in this round), revalidation exits early — nothing changed, no point recomputing.

### Why revalidation happens after EVERY round (not just between OPT_TYPEs)

Consider a large article with many rounds. After Round 1, some primary grids may already be satisfied (H_REM drops to 0). If we didn't revalidate, Round 2 would still see the stale `PRI_CT_REM = 100` and keep allocating into grids that are already full.

By revalidating after each round:
- Round 2's `need_pool` naturally shrinks (POOL_CONSUMED already includes Round 1 take)
- `PRI_CT_REM` accurately reflects which grids are still hungry
- Stores that are "finished" in a grid stop competing for it in subsequent rounds

### Worked example — PRI_CT_REM evolution across rounds

Store HN22, MAJ_CAT M_TEES_HS, RL OPT_TYPE, 3 primary grids:

| After round | Grid 1 REQ_REM | Grid 2 REQ_REM | Grid 3 REQ_REM | H_REM sums | PRI_CT_REM |
|---|---|---|---|---|---|
| Start | 12 | 8 | 6 | 3 active | 100.0 |
| After RL round 1 | 8 | 4 | 5 | 3 active | 100.0 |
| After RL round 2 | 2 | 0 | 3 | 2 active | 66.7 |
| After RL round 3 | 0 | 0 | 1 | 1 active | 33.3 |

When TBL pre-band check fires: `PRI_CT_REM = 33.3 < 100` → TBL skipped. The store was already nearly fully satisfied by RL.

---

## Stage 10 — Reflect to working table

**Where:** `rule_engine_new.py` → `_stage_d_reflect`

After all bands complete, size-level results in `ARS_ALLOC_WORKING` are aggregated back to OPT-level in `ARS_LISTING_WORKING`.

### What is written

```sql
UPDATE ARS_LISTING_WORKING SET
    ALLOC_QTY   = SUM(SHIP_QTY),          -- units going to store
    HOLD_QTY    = SUM(HOLD_QTY),          -- warehouse buffer (TBL only)
    ALLOC_STATUS = CASE
        WHEN SUM(SHIP_QTY + HOLD_QTY) = 0
             AND alloc_status_from_alloc_working = 'SKIPPED'
             THEN 'SKIPPED'
        WHEN SUM(SHIP_QTY) + SUM(HOLD_QTY) = 0
             THEN 'NOT_ALLOCATED'           -- nothing shipped AND no hold
        WHEN filled_rows < total_eligible_sz_rows
             THEN 'PARTIAL'                 -- some sizes got stock, not all
        ELSE 'ALLOCATED'
    END
```

**`filled_rows`** counts size rows where `SHIP_QTY + HOLD_QTY > 0` — both contribute. A TBL row with SHIP=0, HOLD=4 is a filled row.

### ALLOC_STATUS decision tree

```
SUM(SHIP + HOLD) = 0 and all sizes SKIPPED in alloc_df?
    → SKIPPED

SUM(SHIP + HOLD) = 0 otherwise?
    → NOT_ALLOCATED  (had demand but pool was 0 for all sizes)

Some sizes got stock (SHIP+HOLD > 0), but not all?
    → PARTIAL  (pool ran out partway through)

All eligible sizes got stock?
    → ALLOCATED
```

### ALLOC_STATUS meanings in the UI

| Status | What to do |
|---|---|
| **ALLOCATED** | ✅ Good — full demand met for all sizes |
| **PARTIAL** | ⚠️ Pool ran out — store got something but not everything. Check which sizes are missing. |
| **SKIPPED** | ℹ️ Pre-band gate blocked this OPT. See `ALLOC_REMARKS` for reason. Usually correct. |
| **NOT_ALLOCATED** | ❌ Had demand, had listing, but got zero units. Investigate. |

### Review queries

```sql
-- Final status distribution
SELECT ALLOC_STATUS,
       COUNT(*) AS OPTs,
       ISNULL(SUM(ALLOC_QTY), 0) AS units,
       ISNULL(SUM(HOLD_QTY),  0) AS hold
FROM ARS_LISTING_WORKING
WHERE LISTED_FLAG = 1
GROUP BY ALLOC_STATUS
ORDER BY OPTs DESC;

-- NOT_ALLOCATED with HOLD > 0 — should be zero (would indicate reflect bug)
SELECT WERKS, GEN_ART_NUMBER, CLR, OPT_TYPE,
       ALLOC_QTY, HOLD_QTY, ALLOC_STATUS, ALLOC_REMARKS
FROM ARS_LISTING_WORKING
WHERE ALLOC_STATUS = 'NOT_ALLOCATED'
  AND ISNULL(HOLD_QTY, 0) > 0;

-- PARTIAL OPTs — which sizes are missing?
SELECT w.WERKS, w.GEN_ART_NUMBER, w.CLR, w.OPT_TYPE,
       a.SZ, a.SHIP_QTY, a.HOLD_QTY, a.ALLOC_STATUS AS sz_status,
       a.SKIP_REASON
FROM ARS_LISTING_WORKING w
JOIN ARS_ALLOC_WORKING a
  ON w.WERKS=a.WERKS
 AND w.GEN_ART_NUMBER=a.GEN_ART_NUMBER
 AND ISNULL(w.CLR,'') = ISNULL(a.CLR,'')
WHERE w.ALLOC_STATUS = 'PARTIAL'
  AND (a.SHIP_QTY = 0 AND ISNULL(a.HOLD_QTY,0) = 0)
ORDER BY w.WERKS, w.GEN_ART_NUMBER, a.SZ;

-- Stores where nothing got allocated despite being listed
SELECT DISTINCT WERKS, MAJ_CAT
FROM ARS_LISTING_WORKING
WHERE LISTED_FLAG = 1
  AND ALLOC_STATUS = 'NOT_ALLOCATED'
ORDER BY WERKS, MAJ_CAT;
```

---

## Application review guide

### Where each thing lives in the UI

| Question | Page | Where exactly |
|---|---|---|
| Did the run complete without errors? | Data Prep → Listing | Status bar below the Generate button; look for green "Complete" |
| How long did it take? | Listing → Log tab | Timestamps on each Part log line |
| Total units allocated | Listing → Summary tab | "Total ALLOC_QTY" card |
| Status breakdown (ALLOCATED/PARTIAL/SKIPPED) | Listing → Listing tab | ALLOC_STATUS column; use column filter |
| Why an OPT was skipped | Listing → Listing tab | ALLOC_STATUS = SKIPPED → ALLOC_REMARKS column |
| Size-level detail (which sizes got what) | Listing → Alloc tab | One row per (WERKS, GEN_ART, CLR, VAR_ART, SZ) |
| TBL hold units per store | Listing → Alloc tab | HOLD_QTY column, filter OPT_TYPE=TBL |
| Store ranking | Dev Guide → DB Tables → ARS_STORE_RANKING | ST_RANK, W_SCORE columns |
| OPT priority order for a store | Dev Guide → DB Tables → ARS_LISTING_WORKING | OPT_PRIORITY_RANK column |
| Pending allocation (awaiting DO) | Reports → Pending Allocation | Open sessions |

### Step-by-step review checklist

**Step 1 — Confirm run completed**

On the Listing page, the status bar should show:
```
Part 8 complete — allocated N OPTs, shipped X units, held Y units  [duration]
```

If it shows "Part 8 failed" or hangs, look at the log for the error. Most common causes:
- Missing column on ARS_LISTING_WORKING → re-run listing from Part 1
- MSA_FNL_Q = 0 everywhere → run MSA first

**Step 2 — Check top-level health numbers**

Run this in the DB (or read from the live metrics above):

```sql
SELECT
    COUNT(*)                                            AS total_listed,
    SUM(CASE WHEN ALLOC_STATUS='ALLOCATED'     THEN 1 END) AS ok,
    SUM(CASE WHEN ALLOC_STATUS='PARTIAL'       THEN 1 END) AS partial,
    SUM(CASE WHEN ALLOC_STATUS='SKIPPED'       THEN 1 END) AS skipped,
    SUM(CASE WHEN ALLOC_STATUS='NOT_ALLOCATED' THEN 1 END) AS not_alloc,
    ISNULL(SUM(ALLOC_QTY), 0)                          AS ship_units,
    ISNULL(SUM(HOLD_QTY),  0)                          AS hold_units
FROM ARS_LISTING_WORKING WHERE LISTED_FLAG = 1;
```

Expected healthy run:
- `ok` + `partial` = majority of rows
- `skipped` = some (PRI_CT and store-broken skips are expected)
- `not_alloc` = small number (articles where pool is genuinely 0); zero is ideal

**Step 3 — Investigate any NOT_ALLOCATED rows**

```sql
-- Full detail on NOT_ALLOCATED
SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR, OPT_TYPE,
       MJ_REQ, MJ_MBQ, MJ_STK_TTL, MSA_FNL_Q,
       ALLOC_QTY, HOLD_QTY, ALLOC_STATUS, ALLOC_REMARKS, LISTED_REASON
FROM ARS_LISTING_WORKING
WHERE LISTED_FLAG=1 AND ALLOC_STATUS='NOT_ALLOCATED'
ORDER BY OPT_TYPE, WERKS;
```

If `MSA_FNL_Q = 0` → warehouse genuinely out of stock. Expected.
If `MSA_FNL_Q > 0` → pool existed but nothing was taken. Investigate PRI_CT_REM and ST_RANK.

**Step 4 — Drill into a specific store**

```sql
-- Full picture for store hn22 — all OPTs, all statuses
SELECT OPT_TYPE, GEN_ART_NUMBER, CLR,
       ALLOC_QTY, HOLD_QTY,
       PRI_CT_REM, MJ_REQ_REM, ACS_D,
       ALLOC_STATUS, ALLOC_REMARKS,
       OPT_PRIORITY_RANK, ST_RANK
FROM ARS_LISTING_WORKING
WHERE WERKS = 'hn22' AND LISTED_FLAG = 1
ORDER BY OPT_TYPE, ALLOC_STATUS, OPT_PRIORITY_RANK;
```

**Step 5 — Verify TBL hold balance**

```sql
-- Per-article SHIP vs HOLD totals (TBL only)
SELECT GEN_ART_NUMBER, CLR,
       COUNT(DISTINCT WERKS) AS stores,
       SUM(SHIP_QTY)        AS total_ship,
       SUM(HOLD_QTY)        AS total_hold,
       SUM(SHIP_QTY + HOLD_QTY) AS total_pool
FROM ARS_ALLOC_WORKING
WHERE OPT_TYPE = 'TBL'
GROUP BY GEN_ART_NUMBER, CLR
ORDER BY total_pool DESC;

-- Confirm no TBL row is NOT_ALLOCATED with non-zero HOLD
SELECT COUNT(*) AS bug_rows
FROM ARS_LISTING_WORKING
WHERE OPT_TYPE='TBL' AND ALLOC_STATUS='NOT_ALLOCATED'
  AND ISNULL(HOLD_QTY,0) > 0;
-- Should be: 0
```

**Step 6 — Check MBQ cap (if RL/TBC cap is enabled)**

```sql
-- Stores that hit the RL cap
SELECT w.WERKS, w.MAJ_CAT,
       w.MJ_MBQ, w.MJ_STK_TTL,
       w.MJ_MBQ * 0.9 - w.MJ_STK_TTL AS cap_at_90pct,
       ISNULL(SUM(a.SHIP_QTY), 0)     AS actually_allocated,
       CASE WHEN ISNULL(SUM(a.SHIP_QTY),0) >= w.MJ_MBQ * 0.9 - w.MJ_STK_TTL
            THEN 'HIT CAP' ELSE 'under cap' END AS cap_status
FROM ARS_LISTING_WORKING w
LEFT JOIN ARS_ALLOC_WORKING a
       ON w.WERKS=a.WERKS AND w.MAJ_CAT=a.MAJ_CAT AND w.OPT_TYPE=a.OPT_TYPE
WHERE w.OPT_TYPE = 'RL' AND w.LISTED_FLAG = 1
GROUP BY w.WERKS, w.MAJ_CAT, w.MJ_MBQ, w.MJ_STK_TTL
ORDER BY cap_status DESC, actually_allocated DESC;
```

**Step 7 — Spot-check top priority articles**

For each important article (FOCUS_WO_CAP=1), confirm they were allocated:

```sql
SELECT w.WERKS, w.GEN_ART_NUMBER, w.CLR, w.OPT_TYPE,
       w.OPT_PRIORITY_TIER, w.OPT_PRIORITY_RANK,
       w.ALLOC_QTY, w.HOLD_QTY, w.ALLOC_STATUS, w.ALLOC_REMARKS
FROM ARS_LISTING_WORKING w
WHERE w.OPT_PRIORITY_TIER = 1   -- focus articles only
  AND w.LISTED_FLAG = 1
ORDER BY w.ALLOC_STATUS, w.WERKS;
```

---

## Settings and tuning

### Every configurable parameter

| Parameter | Where to set | Default | Effect |
|---|---|---|---|
| `hold_days` | Listing → Hold Days field | 0 | Days of sales to hold at warehouse for TBL. 0 = no hold. |
| `stock_threshold_pct` | Listing → Stock% slider | — | If store's stock ≥ threshold% of MBQ → RL; else TBC or TBL |
| `req_weight` | Listing → Tuning panel | 0.40 | Weight of MJ_REQ in ST_RANK score. Higher → favour stores with more need. |
| `fill_weight` | Listing → Tuning panel | 0.60 | Weight of FILL_RATE in ST_RANK score. Higher → favour well-stocked stores. |
| `rl_mbq_cap_pct` | Listing → Advanced | 0 | Cap RL allocation at X% of store's MBQ (net of current stock). 0 = no cap. |
| `tbc_mbq_cap_pct` | Listing → Advanced | 0 | Same for TBC. |
| `pri_ct_check_rl` | Listing → Advanced | True | Enforce PRI_CT_REM=100 gate for RL. |
| `pri_ct_check_tbc` | Listing → Advanced | True | Enforce PRI_CT_REM=100 gate for TBC. |
| `size_threshold` | Listing → Advanced | 0.6 | Minimum fraction of sizes with available pool. Below this, TBL OPT is skipped. |
| `min_size_count` | Listing → Advanced | 3 | Minimum absolute count of sizes with pool for TBL. |

### When to change what

**Too many TBL SKIP_PRI_BROKEN?**
- RL or TBC is satisfying grids before TBL gets a chance
- Option A: Acceptable — those stores don't need the new article
- Option B: Turn off `pri_ct_check_rl` (RL won't consume primary grids before TBL)
- Option C: Lower `stock_threshold_pct` so fewer stores are classified RL

**Important store getting nothing?**
- Check its ST_RANK: if rank is high (= low number), something else consumed the pool first
- Run the ST_RANK query for that MAJ_CAT — identify who rank=1 is
- If the priority ordering seems wrong: increase `req_weight`, decrease `fill_weight`

**TBL articles not launching at enough stores?**
- Check `PRI_CT%` in `ARS_LISTING` — if < 100 at listing time, article fails R06 in Stage A
- Run Grid Builder to rebuild grid coverage
- Or lower `size_threshold` / `min_size_count` if the issue is sparse size availability

**Hold units growing too large?**
- Increase `hold_days` → bigger HOLD_QTY
- Decrease `hold_days` → smaller HOLD_QTY (or 0 for no hold)
- Ensure DO entries are being recorded so hold is released back to pool

**RL taking too many units from a large store?**
- Set `pri_ct_check_rl = False` and `rl_mbq_cap_pct = 90` (or similar)
- This imposes a budget cap: store can only receive up to (MJ_MBQ × 90% − current_stock) total RL units

---

## Troubleshooting decision tree

### Symptom: Store X got 0 units for article Y

```
1. Check ALLOC_STATUS in ARS_LISTING_WORKING for (WERKS=X, GEN_ART=Y)
   │
   ├── LISTED_FLAG = 0 (not even eligible)
   │     → Read LISTED_REASON — which rule(s) blocked it?
   │     → R04_MSA_POS: MSA_FNL_Q=0 → re-run MSA
   │     → R06_PRI_100: PRI_CT% < 100 → check grid coverage
   │     → R09_TBL_TRIVIAL: MJ_REQ too small → normal
   │
   ├── ALLOC_STATUS = SKIPPED
   │     → Read ALLOC_REMARKS
   │     → SKIP_PRI_BROKEN: PRI_CT_REM dropped below 100 before TBL ran → expected
   │     → SKIP_STORE_BROKEN: MJ_REQ_REM < ACS_D/2 → store already supplied → expected
   │
   ├── ALLOC_STATUS = NOT_ALLOCATED
   │     → MSA_FNL_Q = 0? → Warehouse out of stock for this article
   │     → MSA_FNL_Q > 0? → Pool existed but store didn't compete
   │          → Check ST_RANK: was store ranked last? (high ST_RANK number)
   │          → Check OPT_PRIORITY_RANK: was this OPT ranked very low?
   │          → Check size-level: did all sizes show SKIP_REASON in ARS_ALLOC_WORKING?
   │
   └── ALLOC_STATUS = PARTIAL
         → Some sizes got stock; pool ran out for other sizes
         → Check ARS_ALLOC_WORKING for size SZ = 'M' (or whichever) → FNL_Q_REM at time of allocation
```

### Symptom: TBL articles allocated but HOLD_QTY = 0

```
1. hold_days setting = 0?
   → Yes: expected. Set hold_days > 0 if hold is needed.
   → No: check MAX_DAILY_SALE in ARS_LISTING_WORKING
         → If MAX_DAILY_SALE = 0: hold formula gives 0 (0 × hold_days = 0)
         → Fix: ensure sale data is loaded for the article
```

### Symptom: NOT_ALLOCATED with HOLD_QTY > 0

```
This was a bug fixed in May 2026. Should not occur after the fix.
If it appears:
1. Check backend version — is rule_engine_new.py updated with the _stage_d_reflect fix?
2. The fix: filled_rows counts SHIP+HOLD (not just SHIP)
3. ALLOC_STATUS check uses SHIP+HOLD = 0 (not just SHIP = 0)
```

### Symptom: Allocation takes > 15 minutes

```
1. Check PROCESS_POOL_MIN_MAJCATS — if < 3 MAJ_CATs it runs single-threaded
2. Check ARS_PARALLEL_WORKERS env var (default 4)
3. Look for stuck rounds: in the log, look for a MAJ_CAT that shows
   "RL round=5/5, ship=0, hold=0" many times → I_ROD too high for that cat
4. Ensure ARS_ALLOC_WORKING has its composite indexes (Stage B creates them)
5. Pool exhausted early → many empty rounds still spin → reduce max I_ROD
```

---

## Key constants reference

### Feature flags in `rule_engine_new.py`

| Constant | Default | What it controls |
|---|---|---|
| `RULE_R01_LISTING` | True | Enforce LISTING=1 flag gate |
| `RULE_R02_NOT_MIX` | True | Exclude MIX OPT_TYPE |
| `RULE_R04_MSA_POS` | True | Require FNL_Q > 0 |
| `RULE_R05_REQ_POS` | True | Require OPT_REQ_WH ≥ 1 |
| `RULE_R06_PRI_100` | True | PRI_CT% gate (scoped to enforced types) |
| `RULE_R07_VAR_RATIO_TBL` | True | TBL size availability check |
| `RULE_R08_MJ_REQ_BOOSTED` | True | MBQ cap → trivial skip when gap is tiny |
| `RULE_R09_TBL_TRIVIAL` | True | Skip TBL when MJ_REQ < 0.5×DAILY_SALE |
| `ENABLE_FOCUS_TIERING` | True | OPT_PRIORITY_TIER 1/2/3 (focus articles first) |
| `ENABLE_STORE_BROKEN` | True | Cross-type store-broken gate |
| `ENABLE_PER_OPT_REVALIDATION` | True | Revalidate after each band |
| `ACS_SKIP_FACTOR` | 0.5 | H_REM threshold: REQ_REM > 0.5×ACS_D |
| `BAND_SIZE` | 1 | Rank band width (1 = one OPT at a time; required for per-OPT revalidation) |

### Grain keys in `rule_engine_pandas.py`

```python
OPT_TYPE_ORDER = ["RL", "TBC", "TBL"]     # allocation sequence
OPT_KEYS  = ["WERKS", "MAJ_CAT", "GEN_ART_NUMBER", "CLR"]   # OPT grain
POOL_KEYS = ["RDC", "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "VAR_ART", "SZ"]  # size grain
```

### Worker settings

```
DEFAULT_WORKERS = 4   (override with ARS_PARALLEL_WORKERS env var)
MAX_WORKERS     = 8
PROCESS_POOL_MIN_MAJCATS = 3  (below this, runs single-threaded)
```

---

## Worked end-to-end example

**Article:** GEN_ART=1116112544, CLR=NAVY, MAJ_CAT=M_TEES_HS
**Run date:** 2026-05-03
**hold_days = 3**, `pri_ct_check_rl = True`, `pri_ct_check_tbc = True`

### Setup — 4 stores, pool at warehouse

```
MSA_FNL_Q for VAR_ART=VA_001 (size M) at RDC DW01 = 30 units

Store HN07: OPT_TYPE=TBL, OPT_MBQ=20, OPT_MBQ_WH=20+3×2=26, STK=0, PRI_CT%=100, ST_RANK=1
Store HN14: OPT_TYPE=RL,  OPT_MBQ=15, STK=6, PRI_CT%=100, ST_RANK=2
Store HN22: OPT_TYPE=TBL, OPT_MBQ=20, OPT_MBQ_WH=26, STK=0, PRI_CT%=66.7, ST_RANK=3
Store HN35: OPT_TYPE=RL,  OPT_MBQ=18, STK=4, PRI_CT%=100, ST_RANK=4
```

### Stage A — listing rules

| Store | Type | PRI_CT% | Rule check | LISTED_FLAG |
|---|---|---|---|---|
| HN07 | TBL | 100 | All pass | **1** |
| HN14 | RL | 100 | All pass | **1** |
| HN22 | TBL | 66.7 | R06_PRI_100 fails (66.7 < 100, TBL enforced) | **0** → dropped |
| HN35 | RL | 100 | All pass | **1** |

HN22 is excluded in Stage A. Only HN07, HN14, HN35 proceed.

### Stage C — RL bands first

Pre-band check for RL: PRI_CT_REM = 100 for all RL stores → no skips.

**RL Round 1, size M (pool=30):**

| Store | ST_RANK | SZ_MBQ | STK | need_pool | cum | take |
|---|---|---|---|---|---|---|
| HN14 | 2 | 7 | 3 | **4** | 4 | 4 |
| HN35 | 4 | 9 | 2 | **7** | 11 | 7 |

Pool after RL Round 1: 30 − 4 − 7 = **19 units remaining**

Revalidation after RL Round 1:
- HN14: REQ_REM reduced by 4. H_REM still 1 for all grids. PRI_CT_REM stays 100.
- HN35: REQ_REM reduced by 7. H_REM still 1 for all grids. PRI_CT_REM stays 100.

### Stage C — TBL bands next

Pre-band check for TBL:
- HN07: PRI_CT_REM = 100 → ✓ eligible
- HN22: already excluded (LISTED_FLAG=0), not in the frame

**TBL Round 1, size M (pool=19):**

| Store | ST_RANK | SZ_MBQ_WH | SZ_MBQ | STK | need_pool | need_ship | cum | take | SHIP | HOLD |
|---|---|---|---|---|---|---|---|---|---|---|
| HN07 | 1 | 13 | 10 | 0 | 13 | 10 | 13 | **13** | 10 | 3 |

Pool after TBL Round 1: 19 − 13 = **6 units remaining**

HN07 gets SHIP=10 (goes to store), HOLD=3 (stays at warehouse).

Final reflect for size M:
- HN14: ALLOC_QTY += 4, ALLOC_STATUS = ALLOCATED (full need met)
- HN35: ALLOC_QTY += 7, ALLOC_STATUS = ALLOCATED
- HN07: ALLOC_QTY += 10, HOLD_QTY += 3, ALLOC_STATUS = ALLOCATED
- HN22: ALLOC_QTY = 0, HOLD_QTY = 0, ALLOC_STATUS = NOT_ALLOCATED (excluded in Stage A, not skipped)

---

## How to update this doc

Edit `backend/app/docs/processes/ars_pipeline_complete_reference.md`.
Bump `last_reviewed: YYYY-MM-DD` in the frontmatter.
The page auto-refreshes for all users within 30 seconds — no deploy needed.

Source files tracked for staleness detection:
- `backend/app/services/rule_engine_pandas.py`
- `backend/app/services/rule_engine_new.py`
- `backend/app/api/v1/endpoints/listing.py`

If any of these changes after `last_reviewed`, this doc gets a **STALE** badge in the sidebar.
