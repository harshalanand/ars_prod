---
title: MSA Stock Calculation — What the Warehouse Can Actually Ship
category: Data Prep
order: 30
source: backend/app/services/msa_service.py, backend/app/api/v1/endpoints/msa_stock.py
last_reviewed: 2026-05-03
---

# MSA Stock Calculation

> **═══ USER GUIDE ═══**
> How to run MSA and interpret the output. Scroll to **Technical Reference** for the 9-step algorithm detail.

## Current MSA state (live)

<!-- @metric sql="SELECT COUNT(*) FROM ARS_MSA_VAR_ART" label="Variant rows in MSA" -->

<!-- @metric sql="SELECT COUNT(*) FROM ARS_MSA_GEN_ART WHERE FNL_Q > 0" label="Options with shippable stock" -->

<!-- @metric sql="SELECT ISNULL(SUM(FNL_Q),0) FROM ARS_MSA_VAR_ART" label="Total shippable units (FNL_Q)" -->

<!-- @metric sql="SELECT ISNULL(SUM(STK_QTY),0) FROM ARS_MSA_VAR_ART" label="Total warehouse stock (before pending)" -->

<!-- @metric sql="SELECT ISNULL(SUM(PEND_QTY),0) FROM ARS_MSA_VAR_ART" label="Total already-pending units" -->

<!-- @metric format="table" sql="SELECT TOP 10 RDC, COUNT(*) AS Variants, ISNULL(SUM(FNL_Q),0) AS Shippable FROM ARS_MSA_VAR_ART GROUP BY RDC ORDER BY Shippable DESC" label="Top 10 RDCs by shippable units" -->

## In plain English

**MSA = Main Storage Area.** It's the warehouse. The MSA calculation answers one question: *"For every article (colour + size) we sell, how many units can we actually ship right now — after subtracting what's already been promised to someone else?"*

The output is the **pool** the allocator draws from. Without a fresh MSA, ARS would try to allocate stock that was shipped two days ago, which would lead to angry warehouse teams and short deliveries to stores.

## When to use this

Run MSA:

- **At the start of every replenishment cycle** (usually 1–2 times per week).
- **After** fresh stock arrivals into the warehouse.
- **After** the previous cycle's allocation has been dispatched — so "pending" numbers are current.
- **Before** you run Listing Generation (MSA is a hard prerequisite).

**Don't run it** if upstream stock/pending tables are stale — the output will be wrong.

## Before you start

| Check | How |
|---|---|
| Latest warehouse stock loaded | `ET_STORE_STOCK` or the MSA staging table has today's data |
| Pending DOs entered | `ARS_PEND_ALC` open rows are current — open Pending Allocation → Overview to verify |
| Size master is fresh | `Master_CONT_SZ` contains the right size list for the categories in scope |

## Step-by-step: how to run it

### 1. Go to MSA Stock Calculation page
Sidebar → **Data Preparation** → **MSA Stock Calculation**.

### 2. Pick what to run for
- **ST_CD** (store code / RDC code) — which warehouses to include.
- **SLOC** (storage location) — which SAP storage locations to count.
- **SEG** — the segment (`APP` = apparel, `GM` = general merchandise). Usually both.
- **Date** — the snapshot date (defaults to yesterday).

### 3. (Optional) Upload a file
If your source is an Excel/CSV snapshot instead of the live `ET_STORE_STOCK`, upload it here. The endpoint will use the uploaded file as the source.

### 4. Click Calculate
You'll see progress logs appear. For a full company run (300+ stores × 200+ categories) expect **3–8 minutes** depending on data volume.

### 5. Check the output
Three tables are produced:

| Table | What's in it |
|---|---|
| `ARS_MSA_TOTAL` | Raw pivot by SLOC — detail of who has what in which location. Audit only. |
| `ARS_MSA_GEN_ART` | Summarised per option (`GEN_ART × CLR`) — **FNL_Q is the shippable total**. |
| `ARS_MSA_VAR_ART` | Down to variant + size level. This is what the allocator pool reads. |

## The 9 internal steps (for curious users)

MSA uses a 9-step algorithm. You don't choose the steps — they always run in order — but knowing them helps you debug odd numbers.

| # | What it does | Translation |
|---|---|---|
| 1 | Filter by SLOC codes | "Keep only the warehouse locations I said to include." |
| 2 | Normalize numbers | "Make sure stock is a number. Blanks → 0, negatives → 0." |
| 3 | Fill missing colours / vendors / sizes | "Where data is blank, use safe defaults so nothing gets dropped." |
| 4 | Keep only APP and GM segments | "Ignore 'services' and 'gift cards' and other non-merchandise." |
| 5 | Pivot by SLOC | "Put each warehouse as its own column. Sum across columns = `STK_QTY`." |
| 6 | Subtract ARS pending | "Look up `ARS_PEND_ALC` — approved allocations whose SAP DO has not yet been issued. Subtracts them so they can't be double-allocated." |
| 6.5 | Subtract open holds | "Look up `ARS_NL_TBL_HOLD_TRACKING` — NL/TBL hold reservations. Reduces FNL_Q for articles reserved for specific stores." |
| 7 | Compute `FNL_Q` | "**FNL_Q = max(STK_QTY − PEND_QTY − HOLD_QTY, 0)**. Never negative — zero means 'nothing to ship'." |
| 8 | Generate colour variants | "Expand each option into its colours and sizes (the variant grain)." |
| 9 | Aggregate to option level | "Group back to `GEN_ART × CLR` for the allocator's quick lookup." |

## A worked example

Upload says:
```
GEN_ART=1116111940, CLR=LT_PST, SZ=M
MAJ_CAT=M_TEES_HS, SEG=APP
SLOC=V01_FRESH, STK_Q=10
SLOC=V02_RESERVE, STK_Q=5
```

After Step 5 pivot: `STK_QTY = 10 + 5 = 15`.

`ARS_PEND_ALC` has an open row for this article at this RDC: PEND_QTY = 2 (approved allocation, DO not yet received).

Step 7: `FNL_Q = max(15 − 2 − 0, 0) = 13`.

Step 9 aggregate: `ARS_MSA_GEN_ART` has one row `GEN_ART=1116111940, CLR=LT_PST, FNL_Q=13`.

The allocator later sees `13` available units of this variant to distribute.

## Common questions (FAQ)

**Q: MSA finished, but FNL_Q is 0 for an article I know we have stock of. Why?**
One of three things:
1. `PEND_QTY` is inflated — check `ARS_PEND_ALC` for this article. If the session is > 5 days old with no DO, enter the DO quantities in Pending Allocation → Daily DO Entry.
2. The SLOC you care about was excluded in Step 1 — look at your SLOC filter.
3. The SEG is neither APP nor GM — check `vw_master_product` for this `GEN_ART`.

**Q: I uploaded a fresh stock file but MSA still shows old numbers.**
The calculate step wasn't re-run after upload. Upload + Calculate are two separate clicks.

**Q: Do I have to run MSA every day, even if nothing changed?**
No. Only when stock or pending quantities change. Running it on unchanged data produces identical output but wastes 3–8 minutes.

**Q: Can I run MSA for just one category?**
Not through the UI — MSA is "all segments" by design because allocation runs holistically. If you need a single-category preview, query `ARS_MSA_GEN_ART` directly.

**Q: How is MSA different from "stock"?**
`STK_QTY` is what you have. `FNL_Q` is what you can **freely ship** (after deducting already-promised units). The allocator uses `FNL_Q` so two store requests don't double-count the same unit.

## Troubleshooting — "I see X"

| You see | Meaning | Fix |
|---|---|---|
| "0 rows in ARS_MSA_VAR_ART" | Filter wiped everything | Check your SLOC list — probably wrong code. |
| `FNL_Q` is huge (larger than `STK_QTY`) | PEND_QTY came out negative | Check `ARS_PEND_ALC` for negative ALLOC_QTY rows — should not happen normally. Re-run MSA. |
| MSA crashes with "memory error" | Dataset > 1 M rows, single-machine pandas pivot | Use the pipeline endpoint (`/pipeline/...`) which parallelises across workers. |
| `ST_CD` column missing / renamed | You're looking at an older MSA output table | April 2026: `ST_CD` was renamed to `RDC` in all MSA output tables. |

## Verification

```sql
-- Top FNL_Q totals per RDC
SELECT RDC, SUM(FNL_Q) shippable
FROM ARS_MSA_VAR_ART GROUP BY RDC ORDER BY shippable DESC;

-- Coverage: how many options do we have stock for?
SELECT COUNT(*) AS options_with_stock
FROM ARS_MSA_GEN_ART WHERE FNL_Q > 0;

-- Articles with big pending drains (warning signs of stale PEND)
SELECT TOP 20 GEN_ART_NUMBER, CLR, STK_QTY, PEND_QTY, FNL_Q
FROM ARS_MSA_VAR_ART
WHERE PEND_QTY > 0
ORDER BY PEND_QTY DESC;

-- How much did MSA lose to pending?
SELECT SUM(STK_QTY) stk, SUM(PEND_QTY) pend, SUM(FNL_Q) fnl
FROM ARS_MSA_VAR_ART;
-- fnl + pend should ≈ stk (unless clipped at 0)
```

## Settings you can change

| Setting | Where | Effect |
|---|---|---|
| Threshold (used in Step 8 variant filter) | Request body `threshold` | Filters out low-signal variants |
| SLOC allow-list | Request body `selected_slocs` | Restrict which warehouses count |

---

> **═══ TECHNICAL REFERENCE ═══**

## Behind the scenes — for developers

- Entry: `MSAService.calculate(df, slocs, threshold)` — `backend/app/services/msa_service.py` around line 338.
- Endpoint: `POST /api/v1/msa/calculate` — `msa_stock.py` around line 540.
- Output persistence: `MSAResultStorageService`.
- Parallel variant: the `/pipeline` endpoints (`pipeline.py`) chunk the input and run workers in parallel — used for >1 M row datasets.

All three output tables now use `RDC` instead of `ST_CD` (April 2026 rename).

## How to update this doc

Update when the 9-step order changes, when a new filter input is added, when output table schema changes, or when the parallel pipeline path changes. Bump `last_reviewed`.
