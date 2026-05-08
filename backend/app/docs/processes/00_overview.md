---
title: Welcome — How ARS Works
category: Overview
order: 1
source: backend/app
last_reviewed: 2026-04-20
---

# Welcome to ARS

## What is ARS?

ARS (Auto Replenishment System) is the tool that decides, every replenishment cycle, **which stores get how many units of which article from which warehouse**. Before ARS, 20 separate Excel machines did this job by hand. ARS does it in minutes, for 320+ stores and hundreds of product categories, with full audit trail.

If you are a **Merchandiser, Planner, or Allocator**, this is the tool that produces your dispatch plan.
If you are a **Store Manager**, this is the tool that decides what your store receives.
If you are a **Developer**, this is the tool whose pipelines you maintain.

## The 5-minute mental model

Imagine you have:
- A **warehouse** full of stock (say, 1,200 units of a t-shirt in 5 sizes).
- A **set of stores** that each want some of it.
- A **budget** each store has for the category (e.g., "Store HN14 can get at most 50 t-shirts this week").
- **Sales history** that tells you which sizes and colours sell at which store.
- **Stock at the store** already, so you don't over-supply.

ARS looks at all of that, and outputs a list like:
```
Store HN14 → 8 units of GEN_ART 1116111940 colour LT_PST (sizes: 1×S, 3×M, 3×L, 1×XL)
Store HN21 → 6 units of GEN_ART 1116111940 colour LT_PST (sizes: 1×S, 2×M, 2×L, 1×XL)
...
```
That list becomes a **Delivery Order (DO)** that the warehouse picks and ships.

## How you typically use ARS — a normal week

```
MONDAY                    → Upload fresh store stock + sales files.
                            Use: Data Management → Upload Data

MONDAY afternoon          → Run MSA (Main Storage Area) calculation.
                            Use: Data Preparation → MSA Stock Calculation → Run

TUESDAY morning           → Run Grid Builder (if grids are stale).
                            Use: Data Preparation → Grid Builder → Run All

TUESDAY                   → Generate Listing → Allocation.
                            Use: Data Preparation → Listing → Generate

TUESDAY afternoon         → Review results, spot-check stores.
                            Use: Data Preparation → Listing → Preview tabs

WEDNESDAY                 → Export BDC (warehouse format).
                            Use: Data Preparation → BDC Creation → Process

WEDNESDAY                 → Warehouse picks and ships.
```

**First-time users**: start with the doc **"Listing Generation Pipeline"** — that is the main button you press. The rest of the docs explain what happens behind each step.

## What's in this Process Library

Each document in the left sidebar explains one part of the system in plain English and then shows the technical detail underneath. Use the list below to find what you need.

### For people who run the daily cycle
| Doc | You go there when you want to… |
|---|---|
| **Listing Generation Pipeline** | Run the main "Generate" button and understand what's happening |
| **OPT_TYPE Classification** | Understand why an item is tagged MIX / RL / TBC / TBL |
| **Store Ranking** | See why Store A received stock before Store B |
| **Allocation Rule Engine** | Understand the exact allocation math (waves, rounds) |
| **BDC Creation** | Produce the file the warehouse uploads |

### For people who prepare the inputs
| Doc | You go there when you want to… |
|---|---|
| **MSA Stock Calculation** | Refresh the shippable stock numbers |
| **Grid Builder** | Rebuild the stock/sale aggregates by category |
| **Contribution Percentage** | Update the size split for new or slow-moving articles |
| **Data Upload / Import** | Load any new master or transaction file |

### For analysts and admins
| Doc | You go there when you want to… |
|---|---|
| **Trends Pipeline** | Upload and chart a side dataset |
| **Allocation Engine v2** | Use the score-based allocator (new merchandise launches) |
| **RBAC / RLS / Audit** | Manage users, permissions, and see the audit trail |

## Quick vocabulary

You will meet these words on almost every page:

| Word | In plain English |
|---|---|
| **MAJ_CAT** | Major category — the top-level product family (e.g. `M_TEES_HS` = Men's Half-sleeve T-shirts). |
| **WERKS** | Internal code for a store. It's the SAP "plant" field. |
| **RDC** | Regional Distribution Centre — the warehouse that supplies a group of stores. |
| **GEN_ART** | Generic Article — the parent item (e.g. "Round-neck Cotton T-shirt Crew Logo"). One GEN_ART usually has many colours. |
| **CLR** | Colour (a GEN_ART + CLR combo is sometimes called an "option"). |
| **VAR_ART / ARTICLE_NUMBER** | Variant Article — colour + size (the thing you physically ship). |
| **SZ** | Size (S, M, L, 28, 30…). |
| **OPT** | Option = `(Store × MAJ_CAT × GEN_ART × Colour)`. Each OPT has several variants (sizes). |
| **MSA** | Main Storage Area — the warehouse inventory calculation. `MSA_FNL_Q` is how many shippable units you have after subtracting already-committed (pending) allocations. |
| **STK_TTL** | Total stock at the store for an option (summed across sizes). |
| **OPT_MBQ / OPT_MBQ_WH** | Target stock at the store, without / with a warehouse hold buffer. |
| **ALLOC_QTY** | Final number of units allocated for an OPT to a store. |
| **ALLOC_FLAG** | 1 = the row is eligible for the strict "primary" allocation. 0 = it will be picked up later in fallback waves. |

If you see an acronym that's not here, it's probably explained in the specific doc that uses it.

## A common question: which allocator is running?

There are **two** allocators in ARS. In most cases you want **Rule Engine** (it runs automatically as Part 8 of Listing Generation). The other one, **Allocation Engine v2**, is a separate tool you run manually for special cases (big launches, brand-wide resets).

| Feature | Rule Engine | Allocation Engine v2 |
|---|---|---|
| Runs as part of Listing Generation? | Yes, automatically | No, you run it manually |
| How it picks articles | Wave-based coverage thresholds | Weighted scoring per article |
| Output tables | `ARS_ALLOC_WORKING`, `ARS_LISTING_WORKING.ALLOC_QTY` | `alloc_runs`, `alloc_delivery_orders`, etc. |
| When to use | Normal weekly / monthly cycle | New launches, full-brand rebalancing |

## If something goes wrong

1. First, look at the **message the UI shows you** — it usually points at the right step.
2. Open the relevant doc in this library; each one has a **Troubleshooting** section.
3. If still stuck, look at the server log at `backend/logs/app.log` (dev/ops).
4. For permissions problems (red "Access denied"), contact your Super Admin — that's an `RBAC` / `RLS` issue, see that doc.

## Live data on this page

Many docs show **real numbers pulled live from the database** — not static screenshots. Look for:

| Row counts | Total rows in a table right now |
| Allocation totals | Units shipped in the last run |
| Freshness badges | "Up-to-date" vs "Source changed — review" |

These refresh **every 30 seconds automatically** while the tab is open, and instantly when you switch back to the tab. Toggle **Auto-refresh** in the sidebar to pause.

### Snapshot right now

<!-- @metric sql="SELECT COUNT(*) FROM ARS_LISTING" label="Rows in ARS_LISTING" -->

<!-- @metric sql="SELECT COUNT(*) FROM ARS_LISTING_WORKING" label="Working rows (eligible for allocation)" -->

<!-- @metric sql="SELECT ISNULL(SUM(ALLOC_QTY),0) FROM ARS_LISTING_WORKING" label="Total units allocated (latest run)" -->

<!-- @metric sql="SELECT COUNT(*) FROM ARS_MSA_GEN_ART" label="MSA options available" -->

<!-- @metric sql="SELECT COUNT(DISTINCT WERKS) FROM ARS_LISTING" label="Stores on latest listing" -->

## How this documentation stays current

Each doc is a markdown file under `backend/app/docs/processes/`. Three auto-sync features keep it honest:

1. **File change detection.** When the source code (`source:` in frontmatter) is modified after `last_reviewed`, the doc auto-flags as `STALE`. You'll see an amber triangle on its sidebar entry and a banner at the top. That tells developers: re-review this doc before the next code change.
2. **Live data.** Directives like `<!-- @metric sql="SELECT COUNT(*) FROM X" -->` pull **current** numbers when you view the page. No screenshots to update.
3. **Auto-refresh.** The page re-fetches the list of docs and the current doc's content every 30 seconds, and whenever you refocus the tab.

### How to fix a stale doc (developer)
1. Open `backend/app/docs/processes/<name>.md`.
2. Update the section whose code changed.
3. Bump `last_reviewed: YYYY-MM-DD` in the frontmatter.
4. The stale badge disappears within 30 seconds of the next load.

### How to suggest a change (any user)
Click **Edit** (coming soon) or tell your developer team. The markdown file is the single source of truth — change it, and every user sees the update on their next page refresh.
