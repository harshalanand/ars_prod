---
title: Allocation — How ARS Decides Who Gets What
category: Allocation
order: 10
source: backend/app/services/rule_engine.py, backend/app/api/v1/endpoints/listing.py (Part 8)
last_reviewed: 2026-04-20
---

# Allocation — How ARS Decides Who Gets What

> **═══ USER GUIDE ═══**
> Sections 1–6 are for anyone who wants to understand the allocation output. Technical Reference at the bottom covers the engine internals.

## Current allocation state (live)

<!-- @metric sql="SELECT COUNT(*) FROM ARS_ALLOC_WORKING WHERE SHIP_QTY > 0" label="Variant rows with SHIP_QTY > 0" -->

<!-- @metric sql="SELECT ISNULL(SUM(SHIP_QTY),0) FROM ARS_ALLOC_WORKING" label="Total units shipped (latest run)" -->

<!-- @metric sql="SELECT ISNULL(SUM(HOLD_QTY),0) FROM ARS_ALLOC_WORKING" label="Total units held (warehouse buffer, TBL only)" -->

<!-- @metric format="table" sql="SELECT OPT_TYPE AS Tag, COUNT(*) AS Rows, ISNULL(SUM(SHIP_QTY),0) AS Units FROM ARS_ALLOC_WORKING WHERE SHIP_QTY > 0 GROUP BY OPT_TYPE ORDER BY Units DESC" label="Allocation by OPT_TYPE (latest run)" -->

## In plain English

After ARS has listed every `(store × option)` that needs stock, it has to divide a **limited warehouse pool** among the stores. That job is called **allocation**, and it is the single most important thing ARS does.

Allocation runs **automatically** as Part 8 of the Listing Generation button. You don't have to trigger it separately. This document explains what it does so you can read the output intelligently.

The short version: **stocks flow from highest-priority store to lowest, in waves, until the pool runs out.** Higher-priority stores are the ones that need the stock most (outstanding demand) and have least of it on shelf today. Priority is pre-computed as `ST_RANK` (see Store Ranking doc).

## When to use this

You do not run allocation directly in the UI — it runs inside Listing Generation. You only come to this doc when:

- You want to **understand** why a particular store got X units and another got Y.
- The listing run completed but the **Alloc tab is empty or short** — and you need to find why.
- You are **tuning** thresholds (`Stock%`, `Hold`, `Fallback`) and want to know the effect.

## The allocation story — a 4-wave waterfall

Imagine 1,000 units of a t-shirt in the warehouse, spread across 5 sizes. The allocator runs **4 waves** (rounds) of increasing looseness:

```
Wave 1 — PRI_100  "Perfect fit"
         Only stores where the article hits 100% of the primary grid coverage.
         They drink first.

Wave 2 — PRI_80   "Good fit"
         Stores with ≥80% primary coverage. Whatever's left after Wave 1.

Wave 3 — SEC_100  "OK fit (secondary)"
         100% secondary (fabric/colour/vendor) grid coverage.

Wave 4 — SEC_80   "Loose fit"
         ≥80% secondary coverage. Leftover pool goes here.
```

Inside each wave, three passes run in this order (because some types are more urgent):

```
Pass 1 — RL   (Replenishment — keep a living article stocked)
Pass 2 — TBC  (To-Be-Checked — partial stock with MSA top-up)
Pass 3 — TBL  (To-Be-Listed — new articles, fresh launch)
```

And inside each pass, multiple **rounds** run (1, 2, 3, …) because a store can take stock up to N days' worth of demand, and the scale goes up in each round. Round 1 gives everyone their baseline; Round 2 tops up to 2× baseline; and so on.

So the full nesting is:

```
4 waves × 3 OPT_TYPES × N rounds  =  a controlled waterfall
```

After every round the remaining pool is **smaller**, so later-round stores get less — which is exactly what you want: the neediest store + most ready option consumed the stock first.

## The hold split for new launches

For **TBL (new articles)**, ARS reserves a little extra from the warehouse as a **hold**:

- `OPT_MBQ_WH = OPT_MBQ + (hold days × daily sale)`
- `SHIP_QTY` = what actually goes to the store (without hold).
- `HOLD_QTY` = the buffer kept at the warehouse in case the launch sells faster than expected.
- `POOL_CONSUMED` = `SHIP_QTY + HOLD_QTY` (total drawn from the pool).

For **RL** and **TBC**, `HOLD_QTY = 0` because no separate hold is needed for running articles.

## Where allocation fits in the Listing pipeline

```
... Part 7  →  ARS_LISTING_WORKING ready with ALLOC_FLAG
     ↓
Part 8 — _allocate() from rule_engine.py
     ├── Step 1   Build ARS_ALLOC_WORKING (variant-grain rows)
     ├── Step 2   Enrich with variant stock + size contribution %
     ├── Step 3   Add tracking columns
     ├── Step 4   Create indexes
     ├── Step 5   Build #rule_pool (the shrinking warehouse pool)
     ├── Step 6   Mark base eligibility (flip MIX / unlisted to 0)
     ├── Step 7   RUN 4 WAVES × 3 OPT_TYPES × N ROUNDS
     │           Each inner iteration:
     │             a. Scale demand for the round
     │             b. Waterfall pool consumption by ST_RANK
     │             c. Size-availability break check (skip if too sparse)
     │             d. Commit SHIP/HOLD, drop the pool
     │             e. Sync MSA_FNL_Q for next round
     ├── Step 8   Reflect SHIP_QTY sum → ARS_LISTING_WORKING.ALLOC_QTY
     └── Step 9   Tally totals, clean temp tables
```

## Worked example — one option, three stores

A t-shirt `GEN_ART=1116111940 CLR=LT_PST` in size `M`. Warehouse pool = **20 units** at RDC `DW01`.

Stores HN14, HN21, HN35 all want it. Each has different store priority (`ST_RANK`) and different target (`OPT_MBQ`):

| Store | ST_RANK | OPT_MBQ_WH | Current stock | Demand |
|---|---|---|---|---|
| HN14 | 1 (highest priority) | 18 | 0 | 18 |
| HN21 | 2 | 18 | 2 | 16 |
| HN35 | 3 | 18 | 0 | 18 |

With size contribution `M = 50%`, each store's size-M demand is `~9` units.

**Wave 1, RL pass, Round 1** runs. The waterfall visits the 3 stores in order of `ST_RANK`:

| Order | Store | Pool before | Wants | Gets | Pool after |
|---|---|---|---|---|---|
| 1 | HN14 | 20 | 9 | 9 | 11 |
| 2 | HN21 | 11 | 7 (only 7 needed after 2 in stock) | 7 | 4 |
| 3 | HN35 | 4 | 9 | 4 (partial — pool runs out) | 0 |

Final for this size:
- HN14: `SHIP_QTY = 9`, `ALLOC_STATUS = ALLOCATED`
- HN21: `SHIP_QTY = 7`, `ALLOC_STATUS = ALLOCATED`
- HN35: `SHIP_QTY = 4`, `ALLOC_STATUS = PARTIAL`

If this had been a TBL article (launch), each `SHIP_QTY` would split into `SHIP_QTY (to store)` and `HOLD_QTY (warehouse buffer)`.

## How to read the output — the `ARS_ALLOC_WORKING` columns

When you open the **Alloc** tab on the Listing page, these columns tell the story:

| Column | What it says |
|---|---|
| `SHIP_QTY` | Units actually going to this store (for this variant + size). |
| `HOLD_QTY` | Units reserved as hold at warehouse (TBL only). |
| `POOL_CONSUMED` | `SHIP_QTY + HOLD_QTY`. Total drawn from the warehouse for this store. |
| `ALLOC_STATUS` | `ALLOCATED` (met demand), `PARTIAL` (got some), `SKIPPED` (failed size-break check), `INELIGIBLE` (never qualified). |
| `SKIP_REASON` | Plain-English reason when not allocated (`B0:POOL_DRAINED_AT_SIZE`, `SZ<60pct@RANK=7;WAVE=PRI_100`, …). |
| `ALLOC_ROUND` | Which round decided this (1, 2, …). |
| `FOCUS_FLAG` | `WO_CAP` (no budget cap), `W_CAP` (with cap), `NORMAL`. |

The summed `SHIP_QTY` per `(WERKS, OPT)` is written back to `ARS_LISTING_WORKING.ALLOC_QTY` — this is the final "send this many units" number.

## Settings you can change

| Setting | Where | What changes |
|---|---|---|
| `stock_threshold_pct` | Listing page (Stock%) | How strict "adequate stock" is → changes OPT_TYPE, which changes allocation eligibility |
| `size_threshold` (0.6 = 60%) | Request body | Minimum size availability to keep an OPT; below this, SKIPPED |
| `hold_days` | Listing page (Hold) | How big the TBL hold buffer is |
| Wave list | `rule_engine.py :: DEFAULT_WAVES` (developer change) | Add/remove/reorder waves |
| OPT_TYPE order | `rule_engine.py :: OPT_TYPE_ORDER` (developer change) | Change RL→TBC→TBL priority |

## Common questions (FAQ)

**Q: Why did my best-performing store get 0 units of a hot article?**
Because another store had higher `ST_RANK` for that category and consumed the pool first. Check the Store Ranking doc. If the "winner" was a low-importance store, review your `req_weight` / `fill_weight` settings — maybe you want to tilt toward fill rate.

**Q: Why is `HOLD_QTY` 0 for my store when I expected a hold?**
HOLD is only created for **TBL** (new launches). For RL and TBC, everything ships. If your new article is tagged something other than TBL, check the classification doc.

**Q: The allocator says "POOL_DRAINED_AT_SIZE". What does that mean?**
The article had some pool left overall, but not at this specific size when it was this store's turn. Higher-ranked stores drank the size-M pool first, so your store got nothing for size-M even though size-L and -XL were still available. This is by design — size-level fairness.

**Q: Why didn't the allocator run even though I clicked Generate?**
Check the log for a warning `Rule-based allocation failed: …`. The most common causes:
1. An upstream column is missing from `ARS_LISTING_WORKING`.
2. MSA_VAR_ART has no rows for the run.
3. A temp-table name collision in a parallel session.

See the technical troubleshooting below.

**Q: Can I "undo" an allocation and try different thresholds?**
Yes — just re-run Listing Generation with the new thresholds. The old alloc table is dropped and rebuilt. Your previous choices are not kept anywhere.

**Q: Allocator is too slow (takes > 10 minutes).**
For 50k+ working rows with many `I_ROD` rounds, 5–8 min is normal. If it's > 10 min:
- Look at the log for `skip=0` loops that keep running — `B0:POOL_DRAINED_AT_SIZE` logs tell you a stuck iteration.
- Check that `ARS_ALLOC_WORKING` has its composite index (Step 4).
- Reduce rounds by lowering `I_ROD` cap per OPT_TYPE.

## Troubleshooting — "I see X"

| Log / UI says | Meaning | Fix |
|---|---|---|
| `Rule-based allocation failed: Invalid column name 'X'` | A column the engine expected is missing on `ARS_LISTING_WORKING` | Check Part 7 of Listing + Step 3 of this engine; column renamed upstream? |
| `Invalid object name '#rule_break'` | Temp table lost its session (sp_executesql issue) | Already fixed in the current code by inlining params — if this reappears, any new parameterised `SELECT INTO #temp` must be rewritten. |
| `Part 8 → 0 alloc rows` + success | Working rows all became INELIGIBLE | Check: is `ALLOC_FLAG=1` for any rows? Is MSA fresh? Are there any `PRI_CT% >= 100` rows? |
| Many rows with `SKIP_REASON = SZ<60pct@…` | Size sparsity kicking in — article has very few sizes in pool for the store | Raise `size_threshold` tolerance, or accept the miss (common for long-tail colours). |
| Same store gets 0 in every wave | Store's MAJ_CAT budget (`MJ_REQ`) is zero | Check `ARS_CALC_ST_MAJ_CAT.MJ_REQ` — the cascade table may be stale. |

## Verification queries

Run these after a successful generate to sanity-check:

```sql
-- How many units shipped, how many held?
SELECT SUM(SHIP_QTY) AS ship_total,
       SUM(HOLD_QTY) AS hold_total,
       COUNT(*)      AS alloc_rows
FROM   ARS_ALLOC_WORKING
WHERE  SHIP_QTY > 0 OR HOLD_QTY > 0;

-- Breakdown by OPT_TYPE
SELECT OPT_TYPE, COUNT(*) rows_, SUM(SHIP_QTY) units
FROM ARS_ALLOC_WORKING GROUP BY OPT_TYPE ORDER BY OPT_TYPE;

-- Store coverage — top 10 receivers
SELECT TOP 10 WERKS, SUM(SHIP_QTY) units, COUNT(DISTINCT MAJ_CAT) cats
FROM ARS_ALLOC_WORKING WHERE SHIP_QTY > 0
GROUP BY WERKS ORDER BY units DESC;

-- Stores that got nothing (but were working)
SELECT DISTINCT WERKS FROM ARS_LISTING_WORKING
EXCEPT
SELECT DISTINCT WERKS FROM ARS_ALLOC_WORKING WHERE SHIP_QTY > 0;

-- Sync check: listing working total must match alloc total
SELECT SUM(ALLOC_QTY) FROM ARS_LISTING_WORKING;
SELECT SUM(SHIP_QTY)  FROM ARS_ALLOC_WORKING;
```

---

> **═══ TECHNICAL REFERENCE ═══**
> Engine internals, SQL gotchas, and debugging for developers.

## Behind the scenes — for developers

Entry: `rule_engine.py :: run_rule_based_allocation(conn, final_table, alloc_table, size_threshold=0.6, waves=None)`. Called from `listing.py:1711`.

Flow summary is the 9-step list above. Key constants live near the top of `rule_engine.py`:

```python
OPT_TYPE_ORDER = ["RL", "TBC", "TBL"]
DEFAULT_WAVES = [
    ("PRI_100", "PRI_CT%", 100.0, "Primary grid 100% coverage"),
    ("PRI_80",  "PRI_CT%",  80.0, "Primary grid ≥80% coverage"),
    ("SEC_100", "SEC_CT%", 100.0, "Secondary grid 100% coverage"),
    ("SEC_80",  "SEC_CT%",  80.0, "Secondary grid ≥80% coverage"),
]
POOL_TABLE  = "#rule_pool"
BREAK_TABLE = "#rule_break"
```

**Important gotcha:** any `SELECT ... INTO #temp_table` in SQL Server run through pyodbc with bind parameters (`:foo`) gets wrapped in `sp_executesql`, and the temp table is scoped to that proc and gone after it returns. The break-table creation in Step 7c inlines `opt_type` and `size_threshold` as literals for this reason. If you add another `SELECT INTO #temp` later, keep it **unparameterised** or you will see `Invalid object name` on the next read.

## How to update this doc

Update when wave order or OPT_TYPE order changes, when a new `ALLOC_STATUS` value is introduced, or when the hold-split formula changes. Bump `last_reviewed`.
