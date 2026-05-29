---
title: Store Ranking — Who Gets Stock First
category: Listing
order: 23
source: backend/app/api/v1/endpoints/listing.py :: Part 6
last_reviewed: 2026-04-20
---

# Store Ranking — Who Gets Stock First

> **═══ USER GUIDE ═══**

## Current ranking snapshot (live)

<!-- @metric sql="SELECT COUNT(*) FROM ARS_STORE_RANKING" label="Rows in ARS_STORE_RANKING" -->

<!-- @metric sql="SELECT COUNT(DISTINCT WERKS) FROM ARS_STORE_RANKING" label="Stores ranked" -->

<!-- @metric sql="SELECT COUNT(DISTINCT MAJ_CAT) FROM ARS_STORE_RANKING" label="MAJ_CATs ranked" -->

<!-- @metric format="table" sql="SELECT TOP 5 MAJ_CAT, WERKS, ST_RANK, MJ_REQ, ROUND(FILL_RATE,2) AS FILL_RATE, W_SCORE FROM ARS_STORE_RANKING ORDER BY NEWID()" label="Sample of 5 rows from the rank table (random)" -->

## In plain English

When two or more stores want the same t-shirt and the warehouse can't serve them all, ARS needs a **fair order** to decide who gets served first. That order is called `ST_RANK`, and it's computed per category (`MAJ_CAT`) on every listing run.

**Think of it as priority queue:**
- `ST_RANK = 1` → first in line — gets stock first.
- `ST_RANK = 346` → last in line — only gets what's left after 345 others have taken their share.

The allocator uses `ST_RANK` to drain the warehouse pool from rank 1 outward.

## When this happens

Automatically, as **Part 6** of the Listing Generation pipeline. You don't trigger it separately. You care about this doc when:

- A good store got less than you expected → maybe the rank pushed it down.
- A slow store somehow got stock first → maybe the weights are biased the wrong way.
- You want to adjust fairness → change `req_weight` / `fill_weight`.

## Two ingredients — Need and Emptiness

Store rank combines two numbers, each ranked separately:

| Component | What it measures | Lower = "more important" |
|---|---|---|
| `REQ_RANK` | How much outstanding demand the store has in this category (`MJ_REQ`) | Stores needing more units get `REQ_RANK = 1`. |
| `FILL_RANK` | How "empty" the store is — `MJ_STK_TTL / MJ_MBQ` | The emptiest store gets `FILL_RANK = 1`. |

Actually wait — that's the opposite intuition. Let's look again:

- `REQ_RANK` is assigned by sorting `MJ_REQ` **ascending** → the store with the **smallest outstanding demand** gets `REQ_RANK = 1`.
- `FILL_RANK` is assigned by sorting `FILL_RATE` **descending** → the store with the **highest fill rate** gets `FILL_RANK = 1`.

Then a weighted score is computed:
```
W_SCORE  = REQ_RANK × req_weight + FILL_RANK × fill_weight
ST_RANK  = order of W_SCORE DESC  (highest W_SCORE gets ST_RANK = 1)
```

Wait — highest `W_SCORE` becomes `ST_RANK = 1`? Yes, because:
- Low `MJ_REQ` (well-satisfied store) → low `REQ_RANK` → **lower** W_SCORE.
- High fill rate (well-stocked) → low `FILL_RANK` → **lower** W_SCORE.
- So a **well-satisfied + well-stocked** store has the lowest W_SCORE → gets the highest `ST_RANK` number (last in line).
- Conversely, an **unsatisfied + empty** store has a high W_SCORE → gets `ST_RANK = 1` (first in line).

So the end result is: **stores that need more and have less get served first.** That's what you want.

## A worked example

Four stores in category `M_TEES_HS`. Weights: `req_weight = 0.4, fill_weight = 0.6` (defaults).

| WERKS | MJ_REQ | MJ_STK_TTL | MJ_MBQ | FILL_RATE | REQ_RANK | FILL_RANK | W_SCORE | **ST_RANK** |
|---|---|---|---|---|---|---|---|---|
| HN14 | 10 | 80 | 100 | 0.80 | 1 | 1 | 1.0 | 4 (last) |
| HN35 | 30 | 60 | 100 | 0.60 | 2 | 2 | 2.0 | 3 |
| HN21 | 50 | 40 | 100 | 0.40 | 3 | 3 | 3.0 | 2 |
| HN41 | 80 | 20 | 100 | 0.20 | 4 | 4 | 4.0 | **1 (first)** |

HN41 wants the most (80 units) and has the least (20 in stock, 20% filled). It wins `ST_RANK = 1` and gets served first when the allocator waterfalls.

HN14 already has 80% of its target and only needs 10 more → it's lowest priority (`ST_RANK = 4`). It only gets leftovers.

## Settings you can change

| Parameter | Default | If you raise it… |
|---|---|---|
| `req_weight` (Req%) | 0.4 | Stores with **high outstanding demand** get even more priority. |
| `fill_weight` (Fill%) | 0.6 | Stores with **very low fill rate** get even more priority. |

Together they don't need to sum to 1 — they're plain multipliers. You can also set either to 0 to disable that component.

**Common preset choices:**

| Goal | Weights |
|---|---|
| "Keep all stores at similar fill %" (default) | 0.4 / 0.6 |
| "Prioritise stores with huge holes in inventory" | 0.2 / 0.8 |
| "Just fulfill quantity needs, ignore fill ratio" | 0.8 / 0.2 |
| "50/50 split" | 0.5 / 0.5 |

## Common questions (FAQ)

**Q: A small new store beat a big flagship store. Why?**
Because the new store had a huge `MJ_REQ` (big demand) and very low `FILL_RATE` (empty shelves). The weight formula cares about **need at this moment**, not store size or sales velocity. If you want to protect flagships, add a bias elsewhere (focus flags, budget caps) rather than tweaking `ST_RANK`.

**Q: My store hasn't sold anything this MAJ_CAT, why is it even in the rank?**
The rank table is built from `ARS_LISTING`, which lists every store × category combination where at least one option is viable (non-MIX). If a store has 0 demand and 0 stock, it won't show up for that MAJ_CAT.

**Q: Does `ST_RANK = 1` mean the store gets ALL the stock?**
No — it means it goes first. It takes its target (`OPT_MBQ_WH`), then the pool moves to rank 2, and so on. A high-priority store doesn't hoard the pool; it just starts the waterfall.

**Q: Why does the same store have different `ST_RANK` for different MAJ_CATs?**
Rank is per-category. Store X may be priority 1 in `M_TEES_HS` but priority 200 in `W_DRESSES_SS` — it depends on how much of each category it needs and has on hand.

**Q: Can I manually override a rank?**
Not through the UI today. If you need one store at a specific rank, talk to the developer team about extending the ranking logic or using focus flags as a tiebreaker.

## Troubleshooting — "I see X"

| You see | Meaning | Fix |
|---|---|---|
| `ARS_STORE_RANKING: 0 rows` in log | Listing table had no non-MIX rows | Check classification — everything is MIX? See OPT_TYPE doc. |
| `ST_RANK` missing on `ARS_LISTING_WORKING` rows | Part 6 didn't run or the update step skipped | Re-run Listing. |
| Same rank for every store | Weights both 0, or all stores tied | Check `req_weight`/`fill_weight` are non-zero. |
| New weight didn't take effect | You changed the variable but didn't click Generate | Change only takes effect after next Generate. |

## Verification

```sql
-- Top 5 priority stores in each MAJ_CAT
SELECT * FROM (
  SELECT MAJ_CAT, WERKS, ST_RANK, MJ_REQ,
         ROUND(FILL_RATE, 3) AS FILL_RATE,
         W_SCORE,
         ROW_NUMBER() OVER (PARTITION BY MAJ_CAT ORDER BY ST_RANK) rn
  FROM ARS_STORE_RANKING
) t WHERE rn <= 5 ORDER BY MAJ_CAT, ST_RANK;

-- Does every listing row have a ST_RANK?
SELECT COUNT(*) AS rows_without_rank
FROM ARS_LISTING
WHERE ST_RANK IS NULL AND OPT_TYPE <> 'MIX';

-- Are the weights applied correctly?
SELECT MAJ_CAT, WERKS, REQ_RANK, FILL_RANK, W_SCORE,
       ROUND(REQ_RANK*0.4 + FILL_RANK*0.6, 2) AS recomputed
FROM ARS_STORE_RANKING
WHERE ABS(W_SCORE - ROUND(REQ_RANK*0.4 + FILL_RANK*0.6, 2)) > 0.01;
-- rows here mean weights differ from 0.4/0.6
```

---

> **═══ TECHNICAL REFERENCE ═══**

## Behind the scenes — for developers

Location: `listing.py` around lines 1440–1494. The ranking is a single CTE:

```sql
;WITH StoreAgg AS (
  SELECT MAJ_CAT, WERKS,
         MAX(MJ_REQ) MJ_REQ, MAX(MJ_MBQ) MJ_MBQ, MAX(MJ_STK_TTL) MJ_STK,
         CASE WHEN MAX(MJ_MBQ)=0 THEN 0
              ELSE ROUND(MAX(MJ_STK_TTL)/NULLIF(MAX(MJ_MBQ),0),4) END AS FILL_RATE
  FROM ARS_LISTING WHERE OPT_TYPE <> 'MIX'
  GROUP BY MAJ_CAT, WERKS
),
Ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (PARTITION BY MAJ_CAT ORDER BY MJ_REQ ASC)   AS REQ_RANK,
         ROW_NUMBER() OVER (PARTITION BY MAJ_CAT ORDER BY FILL_RATE DESC) AS FILL_RANK
  FROM StoreAgg
)
SELECT *,
       ROUND(REQ_RANK * :rw + FILL_RANK * :fw, 2) AS W_SCORE,
       ROW_NUMBER() OVER (PARTITION BY MAJ_CAT ORDER BY W_SCORE DESC) AS ST_RANK
INTO ARS_STORE_RANKING
FROM Ranked;
```

Note the **`WHERE OPT_TYPE <> 'MIX'`** filter — MIX rows don't enter the rank, so clearance noise doesn't affect priorities.

After building `ARS_STORE_RANKING`, an `UPDATE` join copies `ST_RANK` back into `ARS_LISTING.ST_RANK` on `(WERKS, MAJ_CAT)`. The allocator reads `ST_RANK` from the working table.

## How to update this doc

Update when the rank formula changes (new component), when tiebreakers are added, or when the MIX filter is removed/changed. Bump `last_reviewed`.
