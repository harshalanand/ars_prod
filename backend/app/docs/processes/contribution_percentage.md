---
title: Contribution Percentage — The Size and Colour Splits
category: Data Prep
order: 50
source: backend/app/api/v1/endpoints/contrib.py
last_reviewed: 2026-04-20
---

# Contribution Percentage

> **═══ USER GUIDE ═══**

## Current contribution state (live)

<!-- @metric sql="SELECT COUNT(*) FROM Master_CONT_SZ WHERE WERKS IS NOT NULL" label="Store-level contribution rows (Master_CONT_SZ)" -->

<!-- @metric sql="SELECT COUNT(*) FROM Master_CONT_SZ WHERE WERKS IS NULL" label="Company-level contribution rows (fallback when no store data)" -->

## In plain English

When ARS decides *"Store HN14 gets 10 units of this t-shirt"*, it has to decide *"which sizes"* — because the warehouse ships variants, not options. The size split comes from **Contribution Percentage** (often just "Contrib"):

```
If size M sells 40% of the time, then 4 of 10 units go to size M.
If size S sells 10%, 1 goes to S. And so on.
```

That 40%, 10%, etc. per size is the **contribution %** — it's learned from historical sales.

But sizes are just one dimension. Contribution also exists for:
- **Cut** (slim-fit vs regular)
- **Sleeve** (half vs full)
- **Range segment** (classic vs fashion)
- **Any other merchandise dimension you define**

## When to use this

Run Contribution:
- **Quarterly** or when a new season starts — size splits drift over time.
- **After** a major new-size launch (e.g., "XXS" being introduced).
- **When** you notice the allocator is sending too much of one size.
- **After** adding or changing a mapping (which dimension to split on).

You do **not** need to run it for every replenishment cycle. Once calculated, the contribution table is reused across many listings.

## The 4 stages

```
1. PRESETS    — "What period of sales history do I train on?"
               (months, averaging window, KPI).

2. MAPPINGS   — "How do I split rows? By size? Cut? Sleeve? And what fallback
                do I use when the store doesn't have enough history?"

3. EXECUTE    — "Run the calculation." Produces Cont_Percentage_<preset>_ST
                (store-specific) and Cont_Percentage_<preset>_CO (company-wide).

4. REVIEW     — "Check results, export, then activate them into Master_CONT_SZ
                for the allocator to use."
```

## Step-by-step — how to run a contribution cycle

### 1. Create or pick a preset
Sidebar → **Contribution %** → **Presets**.

Fill in:
- **preset_name**: e.g. `CONT_SZ_2026Q1`
- **months**: comma-separated `YYYYMM`, e.g. `202511,202512,202601,202602`
- **avg_days**: typically 90 or 180 (how much to smooth)
- **kpi**: `L18M`, `L7D`, or custom
- **base_table**: usually `ET_STORE_SALES`

Save as `DRAFT` until you're ready.

### 2. Define mappings
Sidebar → **Contribution %** → **Mappings**.

Each mapping says *"for rows matching this criterion, group by these columns and output a contribution with this suffix"*. Example:

- **mapping_name**: `SIZE_SPLIT`
- **applies_to**: `*` (all MAJ_CATs) or `MAJ_CAT=M_TEES_HS`
- **group_by**: `SZ`
- **suffix**: `SZ`
- **fallback**: `{"default_cont": "1/n"}` — if no history, distribute evenly

You can have many mappings — one for size, one for sleeve, one for range segment.

### 3. Execute the preset
Sidebar → **Contribution %** → **Execute**.

Pick the preset → click Run. The server starts a background job. Progress is visible under **Jobs Dashboard**.

Depending on volume (millions of sales rows × many mappings), expect **2–15 minutes**.

### 4. Review
Sidebar → **Contribution %** → **Review**.

Preview the `Cont_Percentage_<preset>_ST` table (store-specific) and `_CO` (company-wide). Spot-check:
- Does size M typically dominate (30–50%) for men's t-shirts? Yes → sane.
- Is any store showing 95% in one size? Likely a data issue — investigate.

### 5. Activate
From Review, click **Activate**. This copies the results into `Master_CONT_SZ` (and other master tables depending on the mapping suffix). The allocator reads from `Master_CONT_SZ` at every listing run.

**Important:** activating overwrites the active contribution. Keep the prior version if you want to roll back — export to Excel before activating.

## How the allocator reads contribution

Inside the rule engine, `_enrich_size_cont` uses a 3-level fallback:

```
1. Look up CONT in Master_CONT_SZ for (GEN_ART, WERKS, SZ).
   Found → use ST-level contribution.

2. Not found → look up CONT for (GEN_ART, WERKS IS NULL, SZ).
   Found → use CO-level (company-wide average).

3. Still not found → fall back to 1 / size_count  (each size gets 1/N).
```

This matters because stores with thin sales history get a sensible default instead of an allocator crash.

## A worked example

Sales of `GEN_ART=1116111940, CLR=LT_PST` at store HN14 over last 180 days:

| SZ | qty_sold |
|---|---|
| S | 10 |
| M | 40 |
| L | 30 |
| XL | 20 |

Total = 100. Contribution: `S=0.10, M=0.40, L=0.30, XL=0.20`.

Allocator gets `OPT_MBQ_WH = 18` from listing. Size-M demand is `18 × 1 × 0.40 = 7.2 ≈ 7 units`. Size-S demand is `18 × 0.10 = 1.8 ≈ 2`. And so on.

If HN14 has no sales history at all, the allocator falls back to CO-level (the company-wide split for this article) or, if still missing, `1/4 = 0.25` each.

## Common questions (FAQ)

**Q: Does changing contribution require re-running MSA or Grids?**
No. Contribution is read at **allocation time only** (Part 8 of Listing). Just re-run Listing and the new CONT values take effect.

**Q: How do I know the allocator used ST-level vs CO-level vs fallback?**
Look at `ARS_ALLOC_WORKING.CONT` column after a run. Cross-check against `Master_CONT_SZ` — if the same (WERKS, GEN_ART, SZ) exists in that table with the same value, it's ST-level. If only the WERKS-NULL row matches, it's CO-level. If nothing matches, it's the `1/N` fallback (you'll notice it's exactly 0.25, 0.333, 0.5, etc.).

**Q: My new article gets 25% per size. Why?**
Because it has no sales history yet — the allocator fell back to `1/4`. This is usually fine for launch; after the first weeks of sale, re-run contribution and the article will use its real split.

**Q: Why are some mappings per-MAJ_CAT?**
Because splits are category-specific. Men's T-shirt sizes (S/M/L/XL/XXL) split differently from Women's Dresses (XS/S/M/L/XL) or Denim (28/30/32/34/36). A single "one size split fits all" would be wrong.

**Q: When I change a mapping's group_by, do existing presets need re-execution?**
Yes — existing Cont_Percentage_* tables are stale until you re-execute.

## Troubleshooting — "I see X"

| You see | Meaning | Fix |
|---|---|---|
| Allocator's `CONT` is always 0.25 / 0.333 | Fallback kicked in for everything — no match in Master_CONT_SZ | Activate the contribution preset; verify Master_CONT_SZ has rows. |
| Contribution job stuck in RUNNING | Worker died or data corrupted | Check `Cont_Jobs.error_message`. Restart job. |
| Execution error: "base_table not found" | Preset references a table that was renamed | Edit the preset, fix `base_table`, save, re-execute. |
| All stores show identical 25%/25%/25%/25% | Preset has zero months or months don't match any sales | Edit preset months, re-execute. |

## Verification

```sql
-- Which preset is "active" (latest activated)?
SELECT TOP 5 preset_name, status, activated_at
FROM Cont_presets ORDER BY activated_at DESC;

-- Does Master_CONT_SZ have ST-level rows?
SELECT COUNT(*) AS st_level FROM Master_CONT_SZ WHERE WERKS IS NOT NULL;
SELECT COUNT(*) AS co_level FROM Master_CONT_SZ WHERE WERKS IS NULL;

-- Spot-check one option at one store
SELECT WERKS, GEN_ART_NUMBER, SZ, CONT
FROM Master_CONT_SZ
WHERE WERKS = 'HN14' AND GEN_ART_NUMBER = 1116111940
ORDER BY SZ;
```

## Settings you can change

| Setting | Effect |
|---|---|
| Preset months | Lengthen to smooth; shorten to respond faster to trends |
| Preset avg_days | 90 = recent; 180 = balanced; 365 = very stable but slow |
| Mapping fallback rule | `1/n` (default) or specific per-size defaults |
| Mapping applies_to scope | Restrict to a MAJ_CAT or SSN |

---

> **═══ TECHNICAL REFERENCE ═══**

## Behind the scenes — for developers

- Endpoints: `backend/app/api/v1/endpoints/contrib.py` — `/contrib/presets`, `/contrib/mappings`, `/contrib/execute`, `/contrib/review`.
- Job tracking: `Cont_Jobs` table.
- Output tables: `Cont_Percentage_<preset>_ST` and `Cont_Percentage_<preset>_CO`.
- Activation copies into `Master_CONT_SZ` (for size), plus any other master tables the mappings target.
- Allocator read path: `rule_engine.py :: _enrich_size_cont` (ST → CO → `1/N`).

## How to update this doc

Update when a new mapping dimension is added, when the activation path changes (new master table), or when the 3-level fallback logic changes. Bump `last_reviewed`.
