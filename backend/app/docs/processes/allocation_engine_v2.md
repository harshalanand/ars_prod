---
title: Allocation Engine v2 — The Score-Based Allocator
category: Allocation
order: 12
source: backend/app/api/v1/endpoints/allocation_engine.py, backend/app/services/allocation/
last_reviewed: 2026-04-20
---

# Allocation Engine v2 — The Score-Based Allocator

> **═══ USER GUIDE ═══**

## In plain English

ARS has **two** allocators. The one you meet by default (inside Listing Generation → Part 8) is the **Rule Engine** — it uses grid-coverage waves. This doc is about the **other** one, Allocation Engine v2.

Where the rule engine asks *"is this article's coverage at primary 100%? 80%? ..."*, v2 asks *"what is this article **worth** to this store right now?"* It computes a **score** for every candidate article and then greedily fills store slots starting with the highest-scoring one.

**Use v2** when you want scoring to decide — typically for big launches, brand-wide rebalancing, or when you want to apply merchandising weights (national hero, focus category, vendor priority, etc.) explicitly.

## When to use v2 vs Rule Engine

| Situation | Use |
|---|---|
| Normal weekly / bi-weekly replenishment | **Rule Engine** (runs automatically in Listing Generation) |
| New product launch across all stores | **v2** (score-based — launches matter differently at different stores) |
| Brand-wide rebalancing after season end | **v2** |
| Regulatory/vendor priority weighting | **v2** (scoring is natural) |
| Only partial data available, but you want an allocation run | **v2** (more tolerant of missing grid coverage) |

Both engines write to **different tables** — they don't collide. You can run Rule Engine in the morning (weekly replen) and v2 in the afternoon (launch) and they'll keep separate records.

## The 5 sub-engines

v2 is a pipeline of 5 independent modules. Each runs in sequence; each writes to its own table.

```
1. BUDGET CASCADE          "How much can each store × category × segment spend?"
       ↓
2. ARTICLE SCORER          "For every candidate article, what's its score at each store?"
       ↓
3. GLOBAL GREEDY FILLER    "Pick articles top-down until each store's budget fills."
       ↓
4. SIZE ALLOCATOR          "Split the article's units into sizes (using contribution)."
       ↓
5. DO GENERATOR            "Group variant quantities into Delivery Orders the warehouse can pick."
```

## Step-by-step — running v2

### 1. Open Allocation Engine v2
URL: `POST /api/v1/allocation-engine/run` — typically invoked from a dedicated "Score-based Allocation" page (or via direct API call).

### 2. Configure the run
```json
{
  "run_name":           "2026W16_LAUNCH_RUN",
  "budget_mode":        "ACTIVE",
  "budget_source_table": "ARS_BUDGET_WEEKLY",
  "score_config_id":    3,
  "stores":             ["HN14", "HN21", ...],
  "maj_cats":           ["M_TEES_HS", "M_TEES_FS"],
  "dry_run":            false
}
```

| Field | Purpose |
|---|---|
| `run_name` | Anything descriptive — appears in reports |
| `budget_mode` | `ACTIVE` (use cascade) or `FIXED` (explicit per store) |
| `budget_source_table` | The table with store × category envelope |
| `score_config_id` | Which weights row in `alloc_score_config` to use |
| `stores` / `maj_cats` | Filters — omit for a full run |
| `dry_run` | `true` = write scores + picks but no DOs; use for preview |

### 3. Run
For 300+ stores × 200+ categories expect 5–20 minutes, depending on scoring complexity.

### 4. Review
All outputs are threaded by `run_id`:

| Table | What's in it |
|---|---|
| `alloc_runs` | Run metadata — parameters, status, total units, duration |
| `alloc_article_scores` | Why each article scored what it did — full breakdown per signal |
| `alloc_option_assignments` | Which articles won at which stores |
| `alloc_variant_assignments` | Size-level quantities (SHIP_QTY per variant) |
| `alloc_delivery_orders` | DO-ready export — grouped by RDC and DO number |

### 5. Export DOs
If `dry_run=false`, DOs are ready to download from `alloc_delivery_orders`.

## What the scorer looks at

The Article Scorer considers multiple signals. Weights live in `alloc_score_config`.

| Signal | Meaning | Typical weight |
|---|---|---|
| `ST_SPECIFIC` | Has this store sold this article before? | High if seasoning in |
| `NATIONAL_HERO` | Is it a top-selling article across the country? | Medium |
| `CORE_FOCUS` | Is it flagged as "focus" this season? | High for strategic bets |
| `MVGR` | Does the article's macro/micro MVGR match the store's mix? | Medium |
| `VENDOR` | Is it from a preferred vendor? | Low to medium |
| `MRP` | Does the price tier fit this store's customer? | Medium |
| `FABRIC`, `COLOR` | Does it match this store's fabric/colour preferences? | Low |
| `FRESHNESS` | How new is the article? | Medium — newer scores higher |

Final score = weighted sum of all signal contributions. Ties broken by freshness, then by article number ASC.

## A worked example — one store, one category

Budget cascade: Store `HN14 × MAJ_CAT M_TEES_HS × SEG APP = 500 units`.

Scorer ranks candidate articles (GEN_ART × CLR):

| Article | Score breakdown | Total |
|---|---|---|
| 1116111940 × LT_PST | ST_SPEC 22 + HERO 18 + FOCUS 15 + MVGR 8 + VND 5 + MRP 9 + FAB 4 + CLR 3 = | 84 |
| 1116113205 × L_BRW | ST_SPEC 18 + HERO 14 + FOCUS 15 + MVGR 8 + VND 5 + MRP 9 + FAB 7 + CLR 3 = | 79 |
| 1116112895 × D_GRY | ST_SPEC 16 + HERO 12 + FOCUS 15 + MVGR 7 + VND 4 + MRP 9 + FAB 7 + CLR 4 = | 74 |
| ... | ... | ... |

Filler picks top articles until 500-unit budget exhausts. Each chosen article at a target of e.g. 80 units expands to sizes via contribution (32 × M, 24 × L, 16 × XL, 8 × S for 40/30/20/10 split). DO Generator groups the variant assignments into picks by RDC and emits delivery order numbers.

## Common questions (FAQ)

**Q: Can I preview without actually committing?**
Yes — use `dry_run=true`. Scores and picks are written but DOs are not.

**Q: How do I tune the weights?**
Edit `alloc_score_config`. Add or adjust weight rows, increment `score_config_id`, point your run at the new config. Previous runs keep using their own config.

**Q: Why did a "national hero" article lose at my store?**
Either:
- Store-specific signals dominated (ST_SPEC 0 for a store that never sold it).
- Budget was too small to reach it after higher-score picks.

The full breakdown is in `alloc_article_scores` for the run — dig there.

**Q: Rule Engine's output is also present. Which one wins?**
They write to different tables. Your downstream process decides — BDC typically reads from `ARS_ALLOC_WORKING` (rule engine). For v2 output, pipeline BDC from `alloc_delivery_orders`.

**Q: Can I run v2 on just one category?**
Yes — set `maj_cats: ["M_TEES_HS"]` in the request.

**Q: What if the score config has no weight for one of the signals?**
Missing weight defaults to 0 — that signal doesn't contribute. Explicit 0 and missing are equivalent.

## Troubleshooting — "I see X"

| You see | Meaning | Fix |
|---|---|---|
| "Budget cascade: 0 for HN14 × M_TEES_HS" | Source table missing row for that pair | Refresh `ARS_BUDGET_WEEKLY` — probably stale |
| Same article chosen for every store | NATIONAL_HERO weight dominates | Drop it in a new score config, re-run |
| DO generation fails halfway | Lock contention on `alloc_delivery_orders` | Wait for other run, retry |
| No articles scored for a store | Scope filter (`stores` or `maj_cats`) too narrow | Widen the filter |

## Verification

```sql
-- Last 10 runs
SELECT TOP 10 run_id, run_name, status, total_units, duration_sec, started_at
FROM alloc_runs ORDER BY id DESC;

-- Score distribution for one run
SELECT WERKS, AVG(score_total) avg_score, MAX(score_total) max_score, COUNT(*) candidates
FROM alloc_article_scores WHERE run_id=:rid
GROUP BY WERKS ORDER BY avg_score DESC;

-- Top scoring picks for a store × category
SELECT TOP 10 GEN_ART_NUMBER, CLR, score_total,
       st_specific, national_hero, core_focus, mvgr
FROM alloc_article_scores
WHERE run_id=:rid AND WERKS='HN14' AND MAJ_CAT='M_TEES_HS'
ORDER BY score_total DESC;

-- DOs generated
SELECT DO_NO, COUNT(*) lines, SUM(QTY) units
FROM alloc_delivery_orders WHERE run_id=:rid GROUP BY DO_NO ORDER BY units DESC;
```

## Settings you can change

| Setting | Effect |
|---|---|
| Score weights in `alloc_score_config` | Change what matters |
| Budget source table | Point at different weekly/monthly budgets |
| `dry_run` | Preview vs commit |
| Run scope (stores / maj_cats) | Limit the run |

---

> **═══ TECHNICAL REFERENCE ═══**

## Behind the scenes — for developers

- Endpoint: `POST /api/v1/allocation-engine/run` — `allocation_engine.py` around line 81–113.
- Orchestrator: `backend/app/services/allocation/engine.py`.
- Sub-engines: `budget_cascade.py`, `scorer.py`, `filler.py`, `size_allocator.py`, `do_generator.py`.
- Output tables all prefixed `alloc_` — threaded by `run_id` for isolation.

## How to update this doc

Update when a sub-engine is replaced, new score signals are introduced, or output table schema changes. Bump `last_reviewed`.
