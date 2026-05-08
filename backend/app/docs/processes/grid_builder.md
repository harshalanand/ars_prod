---
title: Grid Builder — The Stock/Sale Pivot Tables
category: Data Prep
order: 40
source: backend/app/api/v1/endpoints/grid_builder.py, backend/app/services/grid_calculations.py
last_reviewed: 2026-04-20
---

# Grid Builder

> **═══ USER GUIDE ═══**

## Current grid state (live)

<!-- @metric format="table" sql="SELECT grid_name, grid_group, status, seq FROM ARS_GRID_BUILDER ORDER BY seq" label="Configured grids (from ARS_GRID_BUILDER)" -->

## In plain English

Before ARS can decide what to ship, it needs **summary tables** that answer:
- *"How much stock does Store X have of all t-shirts combined?"*
- *"What's the store's total sale of black t-shirts across all sizes?"*
- *"How many distinct options does the store carry in the Poly-cotton fabric?"*

Each of those summaries is called a **grid**. Grid Builder creates these tables in the database. The listing pipeline then joins every listing row to every active grid to bring in those summary numbers.

A grid is just a smart pivot table, stored as a SQL table so it can be queried fast.

## When to use this

Run Grid Builder:
- **After** you load fresh stock or sales data.
- **When** you add or change a grid configuration (a rare event).
- **Before** you run Listing Generation — grids are an upstream input.

**Don't run it repeatedly on unchanged data** — it just rebuilds tables with the same numbers. Each run drops and recreates the grid tables.

## Before you start

| Check | How |
|---|---|
| Store stock loaded | `ET_STORE_STOCK` has recent rows |
| Sales loaded | `ET_STORE_SALES` if using sale-driven grids |
| Master data fresh | `vw_master_product` returns the right product attributes |
| Grid configs exist | `ARS_GRID_BUILDER` has rows with `status='ACTIVE'` |

## Step-by-step: how to run it

### 1. Go to Grid Builder page
Sidebar → **Data Preparation** → **Grid Builder**. URL `/data-prep/store-stock`.

### 2. Look at the grid list
You'll see rows like:

| grid_name | hierarchy_columns | grid_group | status | seq |
|---|---|---|---|---|
| MJ | ["WERKS","MAJ_CAT"] | Primary | ACTIVE | 10 |
| MJ_RNG_SEG | ["WERKS","MAJ_CAT","RNG_SEG"] | Secondary | ACTIVE | 20 |
| MJ_MACRO_MVGR | ["WERKS","MAJ_CAT","MACRO_MVGR"] | Secondary | ACTIVE | 22 |
| MJ_MICRO_MVGR | ["WERKS","MAJ_CAT","MICRO_MVGR"] | Secondary | ACTIVE | 24 |
| MJ_FAB | ["WERKS","MAJ_CAT","FAB"] | Secondary | ACTIVE | 30 |
| MJ_CLR | ["WERKS","MAJ_CAT","CLR"] | Secondary | ACTIVE | 40 |
| MJ_M_VND_CD | ["WERKS","MAJ_CAT","M_VND_CD"] | Secondary | ACTIVE | 50 |
| MJ_VAR_ART | ["WERKS","MAJ_CAT","GEN_ART_NUMBER","CLR","VAR_ART","SZ"] | Primary | ACTIVE | 99 |

### 3. Click "Run All"
This triggers every `ACTIVE` grid in `seq` order. Each grid gets its own output table named `ARS_GRID_<grid_name>`.

### 4. (Alternative) Run one grid
Click "Run" next to a specific row to rebuild just that grid.

### 5. Confirm outputs
Each active grid produces a table with 6 summary columns:

```
<level>_STK_TTL    -- total stock at that grouping
<level>_STR        -- store count at that grouping
<level>_CONT       -- contribution %
<level>_MBQ        -- minimum base quantity (target stock)
<level>_OPT_CNT    -- distinct option count
<level>_DISP_Q     -- dispatch quantity (shipped)
```

For the `MJ_FAB` grid, the columns are `FAB_STK_TTL`, `FAB_STR`, `FAB_CONT`, `FAB_MBQ`, `FAB_OPT_CNT`, `FAB_DISP_Q`.

## Primary vs Secondary — why grids have a "group"

| Group | Used for |
|---|---|
| **Primary** | The strict "must cover" level. Usually `MJ` (store × major category). Drives `PRI_CT%` in listing Part 7. |
| **Secondary** | The "nice to have" levels — fabric, colour, vendor, range. Drives `SEC_CT%`. |

When the allocator evaluates an option, it asks:
- Primary grid fully covered? → allocate in wave `PRI_100`.
- Primary 80%+ covered? → wave `PRI_80`.
- Otherwise fall to secondary waves.

So the `grid_group` choice controls allocation eligibility.

## A worked example

Store `HN14`, category `M_TEES_HS`, 50 units of stock in the "Cotton" fabric and 30 units in "Poly-cotton".

After Grid Builder runs:

- `ARS_GRID_MJ`          row: `WERKS=HN14, MAJ_CAT=M_TEES_HS, MJ_STK_TTL=80, MJ_OPT_CNT=12, MJ_MBQ=60`
- `ARS_GRID_MJ_FAB`      row 1: `FAB=Cotton, FAB_STK_TTL=50, FAB_MBQ=40`
- `ARS_GRID_MJ_FAB`      row 2: `FAB=Poly-cotton, FAB_STK_TTL=30, FAB_MBQ=20`
- `ARS_GRID_MJ_CLR`      row per colour in HN14's M_TEES_HS inventory…
- …and so on for each active grid.

The listing pipeline Part 4a then joins these grids onto every listing row and attaches the `*_STK_TTL`, `*_MBQ`, etc. values. That's how the row sees "this store has 50 Cotton t-shirts out of a target of 40".

## Special grid: `ARS_GRID_MJ_VAR_ART`

This one is at the variant level (`GEN_ART × CLR × VAR_ART × SZ`). It's the finest-grain grid and is used as:
- **Part 1 seed** for the listing (each row starts from this grid).
- **Variant stock enrichment** in the allocator (Step 2 of rule_engine reads it to attach `STK_TTL`).

If `ARS_GRID_MJ_VAR_ART` doesn't exist, Listing Generation's Part 1 produces 0 rows and the whole pipeline fails. Always keep this grid active.

## Settings you can change

To add or change a grid, edit `ARS_GRID_BUILDER` (typically via the UI or direct SQL):

| Column | What to set |
|---|---|
| `grid_name` | Short unique name. Used as `ARS_GRID_<name>` suffix. |
| `hierarchy_columns` | JSON list of columns, e.g. `["WERKS","MAJ_CAT","SSN"]`. |
| `grid_group` | `Primary` or `Secondary`. |
| `status` | `ACTIVE` to run, `INACTIVE` to skip. |
| `seq` | Run order (10, 20, 30…). Lower runs first. |
| `use_for_opt_sale` | `1` on the grid whose STK_TTL feeds `PER_OPT_SALE` in listing Part 4b. Usually `MJ_RNG_SEG`. |

## Common questions (FAQ)

**Q: Listing Part 4a says "skipping grid X" — what happened?**
The grid is either `status != ACTIVE` in `ARS_GRID_BUILDER`, missing required columns, or its output table doesn't exist. Run Grid Builder first, then re-check Listing.

**Q: A grid shows up but all `*_CONT` columns are 0 or NULL.**
The grid was built but the "contribution" step didn't populate. Check `grid_calculations.py :: calculate_contribution` — usually means sales data is missing for the grouping period.

**Q: Can I create my own custom grid?**
Yes. Add a row to `ARS_GRID_BUILDER` with your `hierarchy_columns` JSON and `status=ACTIVE`. Next Grid Builder run will include it. Remember: the listing pipeline will automatically pick it up too, so your `*_REQ` column needs downstream logic in Part 4e.

**Q: Why are there so many grids?**
Each grid measures coverage at a different angle. The allocator uses them together to see if an article's coverage is "balanced" (fabric, colour, vendor, range-segment) — so we don't oversell a fabric type while another goes empty.

## Troubleshooting — "I see X"

| You see | Meaning | Fix |
|---|---|---|
| Grid Builder errors "Table X missing column Y" | Master product view is out of date | Refresh `vw_master_product` metadata. |
| Some grids run, others fail | Bad hierarchy column in the config | Edit the JSON to use a column that exists in `ET_STORE_STOCK` or `vw_master_product`. |
| No `ARS_GRID_MJ_VAR_ART` table | Variant grid deactivated / missing | Edit `ARS_GRID_BUILDER` → `status='ACTIVE'`, re-run. |
| Listing Part 4a completes but every grid has "skip" | Grids deactivated en masse (e.g., mass toggle) | Reactivate in `ARS_GRID_BUILDER`. |

## Verification

```sql
-- Which grids are active?
SELECT grid_name, grid_group, status, seq
FROM ARS_GRID_BUILDER ORDER BY seq;

-- Row counts for each grid
SELECT 'MJ'            AS grid, COUNT(*) FROM ARS_GRID_MJ
UNION ALL SELECT 'MJ_FAB',       COUNT(*) FROM ARS_GRID_MJ_FAB
UNION ALL SELECT 'MJ_RNG_SEG',   COUNT(*) FROM ARS_GRID_MJ_RNG_SEG
UNION ALL SELECT 'MJ_VAR_ART',   COUNT(*) FROM ARS_GRID_MJ_VAR_ART;

-- Freshness check
SELECT name, modify_date
FROM sys.tables
WHERE name LIKE 'ARS_GRID_%'
ORDER BY modify_date DESC;
```

---

> **═══ TECHNICAL REFERENCE ═══**

## Behind the scenes — for developers

- Entry: `grid_builder.py` endpoints. See `POST /grid-builder/grids/{id}/run` and `POST /grid-builder/run-all`.
- Calculation service: `backend/app/services/grid_calculations.py`.
- Registry of all grid paths: `ARS_GRID_HIERARCHY` (written as each grid runs) — read by listing to know which `GH_/H_` flag columns to set in Part 7.

## How to update this doc

Update when a new metric column is added to grid output, when a new grid group is introduced, or when the variant-grid's role in Part 1 changes. Bump `last_reviewed`.
