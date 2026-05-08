---
title: Listing Generation — The Main Button
category: Listing
order: 20
source: backend/app/api/v1/endpoints/listing.py :: generate_listing
last_reviewed: 2026-04-20
---

# Listing Generation — The Main Button

> **═══ USER GUIDE ═══**
> Sections 1–8 below are written for the person who clicks **Generate**. Jump to **Technical Reference** at the bottom for developer-level detail.

## Current state (live)

<!-- @metric sql="SELECT COUNT(*) FROM ARS_LISTING" label="ARS_LISTING row count" -->

<!-- @metric sql="SELECT COUNT(*) FROM ARS_LISTING_WORKING" label="ARS_LISTING_WORKING row count (eligible for allocation)" -->

<!-- @metric sql="SELECT ISNULL(SUM(ALLOC_QTY),0) FROM ARS_LISTING_WORKING" label="Total units allocated (latest run)" -->

<!-- @metric format="table" sql="SELECT OPT_TYPE AS Tag, COUNT(*) AS Rows FROM ARS_LISTING GROUP BY OPT_TYPE ORDER BY COUNT(*) DESC" label="Current OPT_TYPE distribution" -->

## In plain English

**Clicking "Generate" on the Listing page is the heart of ARS.** It takes all the data you prepared (stock, sales, master data, MSA, grids) and produces a table that says, for every store and every article: *should we replenish this? If yes, how much?* At the end of the run, the allocator attaches a concrete number of units (`ALLOC_QTY`) to each row, and you're ready to export a delivery order for the warehouse.

One click runs **8 sequential steps** behind the scenes (called Part 1 through Part 8). You don't have to understand the steps to use the button — but when a step fails, or the output looks off, this doc tells you where to look.

## When to use this

You run Listing Generation:

- **Every replenishment cycle** (usually weekly or bi-weekly, depending on your store clusters).
- **After** you have uploaded fresh stock/sales, rebuilt MSA, and refreshed grids.
- **After** any change to Contribution Percentage or Contrib Mappings.
- **Before** you export BDC (BDC reads the Listing output).

**Don't run it** if any of the upstream steps (MSA, grids) are stale or haven't finished — you'll get incomplete numbers.

## Before you start — the 5-point checklist

| # | Check | How to verify |
|---|---|---|
| 1 | Latest store stock loaded | Data Management → All Tables → `ET_STORE_STOCK` — check row count and upload date |
| 2 | Latest sales loaded | Same path, table `ET_STORE_SALES` |
| 3 | Master data fresh | Tables `vw_master_product`, `MASTER_GEN_ART_SALE`, `MASTER_GEN_ART_AGE` updated recently |
| 4 | MSA calculated | Tables `ARS_MSA_TOTAL`, `ARS_MSA_GEN_ART`, `ARS_MSA_VAR_ART` populated. See **MSA Stock Calculation** doc. |
| 5 | Grids rebuilt | `ARS_GRID_MJ`, `ARS_GRID_MJ_VAR_ART`, etc. present and recent. See **Grid Builder** doc. |

If any are missing, do those first. A rushed Listing run on stale data is worse than no run.

## Step-by-step: how to run it

### 1. Go to the Listing page
Sidebar → **Data Preparation** → **Listing**. The URL will be `/data-prep/listing`.

### 2. Pick what you want to generate for
- **Select Store** — All, or a specific store. For daily use, "All" is normal.
- **Select MAJ_CAT** — All, or a specific major category. Use this when you want to test-run one category.
- **RDC Mode** — `All`, `Own` (only this store's RDC), or `Cross` (cross-RDC transfers allowed). Default is `Own`.
- **Run Mode** — `Listing` (just regenerate the listing table) or `Full Pipeline` (re-runs grids + MSA too). Use `Listing` unless you were told otherwise.

### 3. Tune the variables (optional)
The variables row at the top controls the math. Defaults are fine for most runs. Change only when instructed:

| Variable | Meaning | Default |
|---|---|---|
| **Stock%** | How much stock (as % of avg sale) counts as "adequate". Higher → more TBC/TBL. | 0.6 (60%) |
| **Excess×** | Multiplier over MBQ that counts as excess stock. | 2.0 |
| **Hold** | Warehouse hold buffer in days. | 15 |
| **AGE<** | Below this age (days), an article is considered new. | 15 |
| **Req%** | Weight of "need" in store ranking. | 0.4 |
| **Fill%** | Weight of "current fill rate" in store ranking. | 0.6 |
| **ACS_D** | Fallback average daily sale when a store has no history. | 18 |
| **MinSz** | Minimum filled variant count to count as non-MIX. | 3 (off = unchecked) |
| **Fallback** | Allow fallback wave for under-covered options? | unchecked |

### 4. Click Generate
A progress message appears. Depending on data volume, this takes **1–10 minutes**. You can watch the console logs (server side) or just wait for the success toast.

### 5. Review the output
After success, 4 UI elements update:

- **Summary card** (top right) — Total rows, New vs Existing split, store count, tag distribution.
- **OPT_TYPE Distribution pie chart** — MIX / RL / TBC / TBL proportions.
- **Allocation by Type bar chart** — Units allocated per tag.
- **Alloc QTY by RDC** — Units per warehouse.

The data grid at the bottom has three tabs:
- **Working** — the rows eligible for allocation (`ARS_LISTING_WORKING`).
- **Full Listing** — every generated row (`ARS_LISTING`).
- **Alloc** — the allocation plan (`ARS_ALLOC_WORKING`).

### 6. Spot-check a few stores
Pick any store and filter the Working tab by `WERKS=<your store>`. Scroll through:
- Does the `OPT_TYPE` assignment make sense?
- Is `ALLOC_QTY` a reasonable number for the `OPT_MBQ`?
- Any red flags (e.g., 0 allocations everywhere)?

### 7. Export to BDC (next step)
When you're happy, go to **Data Preparation → BDC Creation** to produce the warehouse upload file.

---

> **═══ TECHNICAL REFERENCE ═══**
> From here on, this is the developer-level detail. Part breakdowns, log formats, failure modes, and code anchors.

## What happens behind the scenes — the 8 Parts

You don't need to know these to use the button. But when something fails, the log says "Part 4e failed" or "Part 8 → 0 alloc rows" — use this table to know which step that is.

| Part | Plain-English summary | If this fails… |
|---|---|---|
| **Part 1** | Copy every row from the variant grid into a new `ARS_LISTING` table. These are articles with existing stock (IS_NEW=0). | Grid not built → run Grid Builder first. |
| **Part 2** | Add MSA-only rows (articles with warehouse stock but no store stock yet). These become IS_NEW=1 candidates. | MSA not calculated → run MSA first. |
| **Part 2.5** | Create database indexes so the next steps run fast. Only done when the table is > 5k rows. | Rare; usually a permissions issue. |
| **Part 3.5a** | Bring average daily sale, allocation period, and store focus flags from the calculation tables. | If ACS_D is 0 everywhere → cascade tables stale. Regenerate calc tables. |
| **Part 3.5b** | Bring auto sale reference from `MASTER_GEN_ART_SALE`. | Master sale table outdated — re-upload. |
| **Part 3.5c** | Bring article age from `MASTER_GEN_ART_AGE`. | Master age table outdated — re-upload. |
| **Part 3.55** | Attach the MSA warehouse stock number (`MSA_FNL_Q`) to each row. | `ARS_MSA_VAR_ART` empty or mismatched — re-run MSA. |
| **Part 3.6** | **Classify each row as MIX / RL / TBC / TBL.** (See OPT_TYPE doc.) | `untagged` count > 0 in log → missing stock or MSA data. |
| **Part 3.7** | Consolidate all MIX rows to max 1 per store × category. | Rarely fails; check `mix_mode` setting. |
| **Part 4 pre-resolve** | Attach fabric, vendor, range, colour family from master product view. | `vw_master_product` view broken — contact dev team. |
| **Part 4a** | Join all active grids (MJ, MJ_RNG_SEG, MJ_FAB, …) to bring in stock, sale, MBQ, and contribution per level. | Log shows "skipping grid X" → check `ARS_GRID_BUILDER.status='ACTIVE'`. |
| **Part 4b** | Compute per-option expected sale (`PER_OPT_SALE`). | Depends on the `use_for_opt_sale` grid — usually MJ_RNG_SEG. |
| **Part 4c** | Compute `OPT_MBQ`, `OPT_REQ`, `OPT_MBQ_WH`, `MAX_DAILY_SALE` — the core demand numbers. | Check ACS_D / ALC_D in calc tables. |
| **Part 4d** | Calculate article-level excess stock (`ART_EXCESS`). MIX rows excluded. | Rare. |
| **Part 4e** | Compute `<level>_REQ` per grid level (demand gap after excess deduction). | Missing MBQ at a level → grid not built. |
| **Part 5** | Final indexes on `ARS_LISTING`. | Permissions. |
| **Part 6** | **Build `ARS_STORE_RANKING`** — rank stores by need within each category. | If 0 rows → all rows are MIX (fix classification). |
| **Part 7** | **Build `ARS_LISTING_WORKING`** — the subset with MSA>0 and OPT_REQ_WH≥1. Add grid coverage flags (`PRI_CT%`, `SEC_CT%`, `ALLOC_FLAG`). | If 0 rows → Part 4c produced no demand. |
| **Part 8** | **Run the allocator.** Consumes MSA pool wave by wave, writes `SHIP_QTY` / `HOLD_QTY`, syncs `ALLOC_QTY` back. | See **Allocation Rule Engine** doc. |

## What you see in the logs

A successful run shows timings like this:

```
Part 1 (Grid data INSERT)           1.0s    ←  156K rows
Part 2 (MSA missing INSERT)         0.7s    ←   43K new
Part 3.5a (LISTING/I_ROD/CLR/FOCUS) 3.3s
Part 3.5 (ACS_D/ALC_D/AGE)          1.0s
Part 3.55 (MSA_FNL_Q + VAR_COUNT)   0.9s
Part 3.6 (OPT_TYPE classification)  3.9s    ←  MIX=123K TBL=42K TBC=2K RL=33K
Part 3.7 (MIX handling)             1.7s
Part 4 pre-resolve                  1.8s
Part 4a (Grid column joins)         5.6s
Part 4b (PER_OPT_SALE)              0.6s
Part 4c (OPT_MBQ + OPT_REQ)         2.8s
Part 4d (ART_EXCESS)                1.0s
Part 4e (per-grid REQ)              4.5s
Part 5 (Final indexes)              0.1s
Part 6 (Store Ranking)              0.7s    ←  346 stores ranked
Part 7 (Working table)              1.5s    ←  47K eligible rows
Part 8 (Allocation)                 400s    ←  2.7K alloc rows
TOTAL                               430s
```

Part 8 is almost always the slowest. If it takes more than 10 minutes for ~50K working rows, something is off — see the **Allocation Rule Engine** doc for tuning.

## Tuning — which variable does what

Use this table to understand what changes when you bump a value:

| If you want to… | Change | Direction |
|---|---|---|
| Catch more "on the edge" stock as TBC instead of RL | **Stock%** | Raise from 0.6 → 0.7 |
| Treat partially-filled colours as MIX rather than TBL | **MinSz** | Raise from 3 → 4 |
| Reserve more warehouse buffer for long-lead stores | **Hold** | Raise from 15 → 30 days |
| Make "emptier" stores outrank "needier" stores | **Fill%** up, **Req%** down | e.g. 0.8 / 0.2 |
| Prioritise stores missing the most units | **Req%** up, **Fill%** down | e.g. 0.7 / 0.3 |
| Run allocation even for under-covered options | Tick **Fallback** | — |
| Simulate with a safer assumption on slow stores | **ACS_D** | Lower default (e.g., 12) |

## Common questions (FAQ)

**Q: Do I need to click Generate every day?**
Usually no — once per replenishment cycle (weekly/bi-weekly). Intra-week you may run it again if fresh stock arrives or a store reports a problem.

**Q: Why does the "untagged" count say 0 usually, but 50K sometimes?**
If `untagged > 0`, one of the classification inputs (`STK_TTL` or `MSA_FNL_Q`) didn't load. Usually means a grid or MSA didn't finish. Re-run those, then Generate again.

**Q: What's the difference between "Full Listing" and "Working" tabs?**
Full Listing has **all** rows, including MIX and those with no demand. Working is only the rows the allocator will actually touch (`MSA>0` and `OPT_REQ_WH≥1`). If the two are similar, most of your stock needs replenishment; if Working is much smaller, most of your stock is already adequately filled.

**Q: The Allocation tab is empty. Why?**
Either Part 8 failed (check logs for "Rule-based allocation failed") or no rows survived Part 7. Re-read the log from bottom to top until you see an error.

**Q: Can I undo a Generate?**
No, but it's idempotent — just click Generate again to rebuild. The previous output is overwritten, not layered.

**Q: How do I regenerate just one MAJ_CAT without rebuilding everything?**
Use the "Select MAJ_CAT" filter and pick the one you want. Other MAJ_CATs in `ARS_LISTING` are left alone.

## Troubleshooting — "I see X, what do I do?"

| You see | What's happening | Fix |
|---|---|---|
| "Part 1: 0 rows" | Variant grid empty | Run Grid Builder → Run All |
| "Part 2: 0 rows" | No new MSA options to add | Normal if MSA hasn't changed since last run |
| "Part 3.6 untagged = …" | Classification failed for some rows | Re-check ACS_D and MSA_FNL_Q — usually upstream data issue |
| "Part 7: 0 rows (MSA_FNL_Q>0, OPT_REQ_WH>=1)" | No demand anywhere | All stores have stock ≥ target. Usually not a bug — verify by spot-checking a few stores. |
| "Part 8 → 0 alloc rows" + warning in logs | Allocator errored | See Allocation Rule Engine doc §9 troubleshooting |
| "500 error on /listing/summary" | Summary query broke | Usually a column rename upstream. Contact dev. |
| Frontend grid empty | Frontend fetched before backend finished writing | Click Fetch again / wait 5 seconds |

## Glossary — terms you'll see on the UI

- **IS_NEW** — `1` means new article (no store stock yet), `0` means existing.
- **OPT_TYPE** — MIX / RL / TBC / TBL. See its own doc.
- **ALLOC_FLAG** — `1` means the row is eligible for the strict allocation wave.
- **ALLOC_QTY** — Final units allocated after Part 8.
- **FINAL_OPT_TYPE** — For some allocations the type gets rewritten (TBC → RL, TBL → NL) after the ship decision. This column shows the post-allocation tag.

## Behind the scenes — for developers

Entry point: `listing.py :: generate_listing(req: GenerateRequest)` around line 291.

All Part timings are logged via the `_time_step()` helper (around line 360). Each step either runs SQL through `_run()` (which commits automatically) or calls a Python service.

**Key SQL files / services:**
- `backend/app/services/grid_calculations.py` — grid math
- `backend/app/services/rule_engine.py` — Part 8 allocator
- `backend/app/services/listing_allocator.py` — legacy allocator (still in repo, swapped out in favour of rule_engine)

**Tunable settings live in:**
- `AppSettings` table (persistent defaults per user)
- Request body overrides (temporary, for this one run)

## Park-then-promote history (alloc + listing-working)

Every successful run snapshots **both** `ARS_ALLOC_WORKING` AND `ARS_LISTING_WORKING` into matching parking tables so results can be validated **before** they're promoted to permanent history. Both snapshots ride the same `SESSION_ID`; Approve/Reject act on both atomically.

### Lifecycle

```
ARS_ALLOC_WORKING        (live, dropped each run)        ARS_LISTING_WORKING        (live, dropped each run)
    │                                                          │
    │ Part 8.4 ─ snapshot_session_to_parked(session_id) ────────┤
    ▼                                                          ▼
ARS_ALLOC_PARKED                                       ARS_LISTING_WORKING_PARKED
(PARK_STATUS='PARKED')                                 (PARK_STATUS='PARKED')
    │                                                          │
    │  ┌──── Approve (atomic across both) ────────────────┐   │
    │  │                                                    │   │
    └──┴────► ARS_ALLOC_HISTORY        ARS_LISTING_WORKING_HISTORY ◄─┘
                (permanent)                  (permanent)
    │                                                          │
    └──► Reject ──► PARK_STATUS='REJECTED' on both tables ◄─────┘
                    (kept for audit until TTL)
```

### Tables

| Table | Lifecycle | Notes |
|---|---|---|
| `ARS_ALLOC_WORKING` | dropped/recreated every run | live workspace (size-grain alloc plan) |
| `ARS_LISTING_WORKING` | dropped/recreated every run | live workspace (option-grain working table) |
| `ARS_ALLOC_PARKED` | append-on-park, delete-on-approve | columns auto-reconciled from `ARS_ALLOC_WORKING` at every snapshot |
| `ARS_LISTING_WORKING_PARKED` | append-on-park, delete-on-approve | columns auto-reconciled from `ARS_LISTING_WORKING` at every snapshot |
| `ARS_ALLOC_HISTORY` | append-on-approve, never auto-deleted | row-grain; idempotency via `IF NOT EXISTS` guard |
| `ARS_LISTING_WORKING_HISTORY` | append-on-approve, never auto-deleted | row-grain; idempotency via `IF NOT EXISTS` guard |

The schema-drift handling (`ALTER TABLE … ADD <new col> NULL`) runs at **every** snapshot and at **every** Approve, so dynamic columns added by future Parts (`H_*`, `GH_*`, `ALLOC_FLAG`, `PRI_CT%`, …) flow into parked + history tables automatically — no manual migration.

### Atomicity

Approve and Reject open a single SQLAlchemy connection and commit once. Either both source-table snapshots move to history, or neither does. If the Approve INSERT for `ARS_LISTING_WORKING_PARKED → ARS_LISTING_WORKING_HISTORY` fails after `ARS_ALLOC_*` succeeds in the same transaction, the rollback unwinds both. Idempotency: a duplicate Approve checks each history table for existing rows under the SESSION_ID and returns `{already_approved: true}` without re-inserting.

### Endpoints

- `GET  /listing/parked-runs` — sessions awaiting review, with row counts from BOTH parked tables (`alloc_parked_rows`, `listing_parked_rows`).
- `GET  /listing/parked-runs/{session_id}?which=alloc|listing` — paginated detail rows from one of the two parked tables (default `which=alloc` for back-compat).
- `POST /listing/parked-runs/{session_id}/approve` — promote both tables to history atomically (idempotent). Returns `{approved_rows, by_table: {alloc, listing}, already_approved}`.
- `POST /listing/parked-runs/{session_id}/reject` — flip `PARK_STATUS='REJECTED'` on both tables; audit_log entry.
- `GET  /listing/alloc-history` — query approved alloc history.
- `GET  /listing/listing-history` — query approved listing-working history.
- `POST /listing/parked-runs/purge` — TTL helper: deletes PARKED >14d and REJECTED >30d on **both** parked tables.

### Concurrency

`POST /listing/generate` returns **409 Conflict** when another run is still `STATUS='RUNNING'`. This prevents two overlapping runs from racing on the `DROP TABLE [ARS_ALLOC_WORKING]` that happens in Part 7/Part 8.

### Tables-affected summary

`ARS_LISTING_SESSIONS` now carries two extra columns:

- `TABLES_AFFECTED` — JSON array `[{table, action, rows}, ...]` for `ARS_LISTING`, `ARS_LISTING_WORKING`, `ARS_LISTED_OPT`, `ARS_ALLOC_WORKING`, `ARS_MSA_TOTAL`, `ARS_MSA_GEN_ART`, `ARS_MSA_VAR_ART`. `action` is `CREATED` / `RECREATED` / `TRUNCATED` / `UPSERTED` / `MISSING`. Captured by a single post-run sweep.
- `PARKED_STATUS` — `PARKED` (snapshot succeeded), `SKIPPED_ERROR` (snapshot failed; listing still SUCCESS), `SKIPPED_EMPTY` (nothing to park).

The Listing UI shows the tables-affected list inline below the KPI tiles after a SUCCESS, and surfaces the parked-runs review queue as a collapsible section.

### Implementation files

- `backend/app/services/parked_history.py` — service. Multi-target by design: `_SNAPSHOT_TARGETS` is a list of `(label, source, parked, history)` triples; adding a third triple makes the whole pipeline (snapshot, approve, reject, purge, list, detail) cover it too. Public functions: `snapshot_session_to_parked`, `approve_parked`, `reject_parked`, `list_parked_runs`, `get_parked_detail(which=…)`, `list_alloc_history`, `list_listing_history`, `purge_old_parked`, `tables_affected_summary`, `has_running_session`.
- `backend/app/api/v1/endpoints/listing.py` — Part 8.4 wiring + new endpoints (`/parked-runs`, `/parked-runs/{sid}` with `which`, `/approve`, `/reject`, `/alloc-history`, `/listing-history`, `/purge`).
- `backend/app/services/listing_sessions.py` — extra columns on the sessions table (`TABLES_AFFECTED`, `PARKED_STATUS`).
- `frontend/src/services/api.js` — `listingAPI.parkedRuns / parkedRunDetail({which}) / approveParked / rejectParked / allocHistory / listingHistory`.
- `frontend/src/pages/ListingPage.jsx` — completion panel, Parked Runs queue with both row counts, drawer with `[Alloc rows | Listing rows]` tab toggle.

## How to update this doc

Update when you add / remove / reorder a Part in `generate_listing`, change a tunable default, or change the log format that this doc shows. Bump `last_reviewed`.
