---
title: Rule Engine — Complete Pipeline Walkthrough (rule_engine_new.py)
category: Allocation
order: 11
source: backend/app/services/rule_engine_new.py, backend/app/api/v1/endpoints/listing.py
last_reviewed: 2026-05-17
---

# Rule Engine — Complete Pipeline Walkthrough

> **═══ OPERATOR GUIDE ═══**
> End-to-end walkthrough of the current rule engine (`rule_engine_new.py`).
> Read top-to-bottom the first time. Use the "What to verify" / "What to tune"
> blocks each cycle as your day-to-day reference.
>
> For the OLD allocator (legacy `rule_engine.py`) see `allocation_rule_engine.md`.
> For the score-based allocator see `allocation_engine_v2.md`.

## Pipeline at a glance

```
┌──────────────────────────────────────────────────────────────────────┐
│  RAW UPLOADS                                                          │
│  • Store Stock (ET_STORE_STOCK)   • Sales History                     │
│  • MSA Variant Pool (ARS_MSA_VAR_ART, ARS_MSA_GEN_ART)                │
│  • Grid Master (Master_CONT_SZ, ARS_GRID_BUILDER, MJ_VAR_ART grid)    │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  STEP 1 — LISTING GENERATION  (listing.py)                            │
│  Output: ARS_LISTING_WORKING   one row per (WERKS, MAJ, GEN_ART, CLR) │
│  Adds: ACS_D, MJ_MBQ, MJ_REQ, OPT_REQ, MSA_FNL_Q, PRI_CT%, OPT_TYPE   │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  STEP 2 — ALLOCATION  (rule_engine_new.py)                            │
│                                                                       │
│   ┌─────────────┐    ┌─────────────┐    ┌─────────────┐               │
│   │  Stage A    │ →  │  Stage B    │ →  │  Stage C    │               │
│   │  List OPTs  │    │  Explode    │    │  Waterfall  │               │
│   │  (R01..R09) │    │  to VAR×SZ  │    │  RL→TBC→TBL │               │
│   └─────────────┘    └─────────────┘    └─────────────┘               │
│         │                  │                  │                       │
│         ▼                  ▼                  ▼                       │
│  LISTED_FLAG=1     ARS_ALLOC_WORKING   SHIP_QTY/HOLD_QTY               │
│  OPT_PRIORITY_RANK rows created        per-size pool drained           │
│                                                                       │
│                              │                                        │
│                              ▼                                        │
│   ┌─────────────┐    ┌─────────────┐    ┌─────────────┐               │
│   │  Caps       │ →  │  Fallback   │ →  │  Stage D    │               │
│   │  MBQ/SEC/MJ │    │  (optional) │    │  Reflect    │               │
│   └─────────────┘    └─────────────┘    └─────────────┘               │
│                                                                       │
│  Output: ARS_ALLOC_WORKING (final) + ARS_LISTING_WORKING (totals)     │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  STEP 3 — PARK & DISPATCH                                             │
│  Park to ARS_ALLOC_PARKED → review → dispatch via /allocation/dispatch│
└──────────────────────────────────────────────────────────────────────┘
```

## Two tables, one handoff

The whole engine is a conversation between two tables at two different grains:

| Table | Grain | Who builds it | Key columns |
|---|---|---|---|
| `ARS_LISTING_WORKING` | `(WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR)` — the **OPT** | Listing Generation (Step 1) + Stage A | `OPT_TYPE`, `MJ_REQ`, `ACS_D`, `MJ_MBQ`, `MSA_FNL_Q`, `PRI_CT%`, `LISTED_FLAG`, `OPT_PRIORITY_RANK` |
| `ARS_LISTED_OPT` | same OPT grain | Stage A finalize | snapshot of `LISTED_FLAG=1` rows. The allocator reads from here, not the working table. |
| `ARS_ALLOC_WORKING` | `(WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ)` — **OPT × VAR × SIZE** | Stage B | `SZ_MBQ` = OPT_MBQ × CONT, `SZ_REQ` = max(0, SZ_MBQ − SZ_STK), `FNL_Q`, `SHIP_QTY`, `HOLD_QTY`, `ALLOC_QTY`, `SKIP_REASON`, `ALLOC_REASON` |

**Flow:** Stage A decides *whether* an OPT is listed; Stage B explodes each listed OPT into all its VAR×SZ rows; Stage C walks them in `OPT_PRIORITY_RANK` order draining a per-`(RDC, MAJ_CAT, GEN_ART, CLR, VAR_ART, SZ)` pool held in temp table `#nre_pool`; Stage D writes the final per-size results back and aggregates totals onto `ARS_LISTING_WORKING`.

---

## STEP 1 — Listing Generation (`listing.py`)

**Trigger:** `POST /api/v1/listing/generate` → `generate_listing()`.

| Sub-step | What it does | Key columns written |
|---|---|---|
| Part 1 | Joins store stock + sales + MSA + grid master at OPT grain | `STK_TTL`, `MSA_FNL_Q`, `RL_HOLD_QTY` |
| Part 2 | Loads grids (MJ + secondary) | `MJ_MBQ`, `FAB_MBQ`, `MICRO_MVGR_MBQ`, etc. |
| Part 3 | ACS_D & per-store velocity | `ACS_D`, `MAX_DAILY_SALE` |
| **Part 3.6** | **OPT_TYPE classification** → MIX/RL/TBC/TBL | `OPT_TYPE` |
| Part 4c | OPT-level demand math | `OPT_MBQ`, `OPT_REQ`, `OPT_MBQ_WH`, `OPT_REQ_WH` |
| Part 4d | Excess stock | `EXCESS_STK` |
| Part 5 | MJ_REQ aggregation | `MJ_REQ`, `MJ_STK_TTL` |
| Part 5.5 | Primary contribution % gate | `PRI_CT%`, `SEC_CT%` |

### OPT_TYPE decision tree (Part 3.6, first match wins)

| Condition | Tag | Meaning |
|---|---|---|
| `STK_TTL < threshold × ACS_D` AND `MSA_FNL_Q = 0` AND `RL_HOLD_QTY = 0` | **MIX** | Nothing to ship, discontinue here |
| Sparse sizes: `VAR_FNL_COUNT / VAR_COUNT < threshold` OR `< MinSz` | **MIX** | Not a real option |
| `STK_TTL ≥ threshold × ACS_D` AND `MSA_FNL_Q > 0` | **RL** | Top-up replenishment |
| `0 < STK_TTL < threshold × ACS_D` AND (MSA or hold available) | **TBC** | Below target, partial top-up |
| `STK_TTL ≤ 0` AND (MSA or hold available) | **TBL** | Empty store, fresh launch |
| Otherwise | **MIX** | Catch-all |

Allocator priority order: **RL → TBC → TBL**. MIX is ignored entirely.

### What to verify

```sql
-- 1. OPT_TYPE distribution looks reasonable (not all MIX, not all TBL)
SELECT OPT_TYPE, COUNT(*) FROM ARS_LISTING_WORKING GROUP BY OPT_TYPE;

-- 2. MJ_REQ populated, ACS_D not zero everywhere
SELECT
  SUM(CASE WHEN MJ_REQ IS NULL THEN 1 ELSE 0 END) AS null_mj_req,
  SUM(CASE WHEN ACS_D = 0      THEN 1 ELSE 0 END) AS zero_acs_d,
  COUNT(*) AS total
FROM ARS_LISTING_WORKING;

-- 3. PRI_CT% spread — too many 0% suggests a grid build issue
SELECT TOP 10 [PRI_CT%], COUNT(*) FROM ARS_LISTING_WORKING
GROUP BY [PRI_CT%] ORDER BY COUNT(*) DESC;
```

### What to tune

| Setting | Default | Effect |
|---|---|---|
| `Stock%` | 0.6 | Higher → stricter "adequate stock" bar (more TBC/TBL) |
| `MinSz` | 3 | Higher → tag sparse colours as MIX |
| `default_acs_d` | 18 | Fallback for new stores with no sales |

---

## STEP 2A — Stage A: List OPTs (`_stage_a_apply_rules`)

Sets `LISTED_FLAG = 1` for OPTs that pass all rules R01-R09 and assigns `OPT_PRIORITY_RANK` (1, 2, 3…) per `(WERKS, OPT_TYPE, MAJ_CAT)`.

### Rules — an OPT is dropped when…

| Rule | Drops when… | Why |
|---|---|---|
| **R01** | `LISTING ≠ 1` upstream | User blocklist |
| **R02** | `OPT_TYPE = 'MIX'` | MIX = "don't ship" |
| **R04** | `MSA_FNL_Q = 0` AND `RL_HOLD_QTY = 0` | No warehouse supply AND no prior hold |
| **R05** | `OPT_REQ_WH < 1` | Store has no warehouse demand |
| **R06** | `PRI_CT% < 100` AND `ALLOC_FLAG ≠ 1` AND `OPT_TYPE ∈ enforced` | Insufficient primary-grid coverage. TBL always enforces; RL/TBC controlled by UI toggles |
| **R07** | TBL only, `VAR_FNL_COUNT/VAR_COUNT < 0.6` AND `VAR_FNL_COUNT < 3` | Won't launch with too few sizes |
| **R09** | `(cap × MJ_MBQ − MJ_STK_TTL) < 0.5 × ACS_D` | Even with growth %, no meaningful gap to fill |

### Ranking order within each (WERKS, OPT_TYPE, MAJ_CAT)

1. `OPT_PRIORITY_TIER` (1=focus-uncapped, 2=focus-capped, 3=regular)
2. `SEC_CT%` DESC (higher contribution first)
3. `MAX_DAILY_SALE` DESC (higher velocity first)
4. `OPT_REQ_WH` DESC (more required first)
5. `GEN_ART_NUMBER`, `CLR` ASC (stable tie-breaker)

### What to verify

```sql
-- How many OPTs dropped at each rule?
SELECT LISTED_REASON, COUNT(*) FROM ARS_LISTING_WORKING
WHERE LISTED_FLAG = 0 GROUP BY LISTED_REASON ORDER BY COUNT(*) DESC;

-- Rank distribution per store/MAJ_CAT (should be 1..N contiguous)
SELECT WERKS, MAJ_CAT, OPT_TYPE,
       MIN(OPT_PRIORITY_RANK), MAX(OPT_PRIORITY_RANK), COUNT(*)
FROM ARS_LISTING_WORKING WHERE LISTED_FLAG = 1
GROUP BY WERKS, MAJ_CAT, OPT_TYPE;
```

### What to tune (UI sliders)

| Slider | Effect |
|---|---|
| `rl_mbq_cap_pct` / `tbc_mbq_cap_pct` / `tbl_mbq_cap_pct` | MJ-level growth caps per OPT type |
| `pri_ct_check_rl` / `pri_ct_check_tbc` | Strict 100% PRI gate vs MBQ-cap mode |

---

## STEP 2B — Stage B: Explode (`_stage_b_explode`)

For every `LISTED_FLAG=1` OPT, joins to `ARS_MSA_VAR_ART` and creates one row per `(VAR_ART, SZ)` in `ARS_ALLOC_WORKING`. Then `_stage_b_fill_cont` fills CONT% from `Master_CONT_SZ`, and `_stage_b_fill_targets` computes per-size targets.

### Per-size math

- `SZ_MBQ = ROUND(OPT_MBQ × CONT, 0)` — store target qty for this size
- `SZ_MBQ_WH = ROUND(OPT_MBQ_WH × CONT, 0)` — warehouse hold target
- `SZ_REQ = MAX(0, SZ_MBQ − SZ_STK)` — what to ship to reach target
- `SZ_REQ_WH = MAX(0, SZ_MBQ_WH − SZ_STK)` — warehouse-side gap
- `FNL_Q = MSA pool for (RDC, VAR_ART, SZ)` — what's available in the RDC

### The MJ_REQ gate (inclusive since May 2026, F1)

```sql
MJ_REQ >= 0.5 × ACS_D   -- only listed OPTs that pass this go into alloc table
```

OPTs with `MJ_REQ` exactly equal to `0.5 × ACS_D` are eligible (the strict `>` form was excluding borderline OPTs — fixed by F1 in May 2026).

### What to verify

```sql
-- Every listed OPT exploded into at least one row
SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR, COUNT(*) AS size_rows
FROM ARS_ALLOC_WORKING GROUP BY WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR;

-- CONT sums to ~1.0 per (WERKS, MAJ_CAT)
SELECT WERKS, MAJ_CAT, SUM(DISTINCT CONT) FROM ARS_ALLOC_WORKING
GROUP BY WERKS, MAJ_CAT HAVING ABS(SUM(DISTINCT CONT) - 1.0) > 0.05;
```

### What to tune

`Master_CONT_SZ` rows. Wrong CONT per size → wrong SZ_MBQ → wrong demand.

---

## STEP 2C — Stage C: Waterfall (`_stage_c_waterfall`)

The core allocation loop. Iterates `OPT_TYPE ∈ {RL, TBC, TBL}` → `round ∈ {1..I_ROD}` → `rank ∈ {1..max_rank}`. For each rank band, runs one batch SQL that:

1. Computes `need_pool` per row (units the row wants from the RDC pool)
2. Orders rows within each pool key `(RDC, MAJ_CAT, GEN_ART, CLR, VAR_ART, SZ)` by `ST_RANK ASC, OPT_PRIORITY_RANK ASC, WERKS ASC`
3. Cumulative window — each row takes `min(need_pool, FNL_Q_REM − prior_demand)`
4. Writes `SHIP_QTY`, `HOLD_QTY` (TBL only), decrements `#nre_pool.FNL_Q_REM`
5. Revalidates `MSA_FNL_Q_REM`, `MJ_REQ_REM`, `PRI_CT_REM`, grid `*_REQ_REM` on `ARS_LISTING_WORKING`
6. Skips PENDING rows whose revalidated metrics dropped below thresholds — sets `SKIP_REASON`

### `need_pool` formula

- RL/TBC: `need = r × SZ_MBQ − SZ_STK − POOL_CONSUMED` (no hold buffer)
- TBL: `need = SZ_MBQ_WH + r × SZ_MBQ − SZ_MBQ − SZ_STK − POOL_CONSUMED` (one-time hold buffer)

### Cross-type revalidation (`_revalidate_cross_type`)

After RL completes, TBC's pending rows are checked against the updated `MSA_FNL_Q_REM`, `PRI_CT_REM`, `MJ_REQ_REM`. Same after TBC → TBL. Stores whose MJ_REQ_REM drops below `0.5 × ACS_D` get `CROSS_SKIP_<ot>_STORE_BROKEN`.

### Possible SKIP_REASONs

| Reason | Meaning |
|---|---|
| `NO_REQ` | Store had no demand for this size (`SZ_REQ ≤ 0`) |
| `NO_POOL_MSA` | Had demand but per-size RDC pool empty when band fired |
| `ALREADY_STOCKED` | Stock already meets target |
| `MBQ_CAP_<ot>` | Trimmed by per-OPT-type MJ_MBQ cap |
| `SEC_CAP_<grid>` | Trimmed by Secondary grid cap (FAB, MICRO_MVGR, etc.) |
| `MJ_REQ_CAP` | Trimmed by global MJ_REQ cap (post-fallback) |
| `R09_HEADROOM_TRIVIAL` | Boosted MJ_MBQ − MJ_STK_TTL still < 0.5×ACS_D |
| `CROSS_SKIP_<ot>_MSA` | MSA pool exhausted by previous OPT_TYPE |
| `CROSS_SKIP_<ot>_PRI` | PRI_CT% dropped below 100 after previous OPT_TYPE |
| `CROSS_SKIP_<ot>_STORE_BROKEN` | MJ_REQ_REM < 0.5×ACS_D after previous OPT_TYPE |

Since the May 2026 fix (F2), cap reasons (`MBQ_CAP_*`, `SEC_CAP_*`, `MJ_REQ_CAP`, `R09_HEADROOM_TRIVIAL`, `CROSS_SKIP_*`) are preserved on finalize — earlier behaviour clobbered them to `NO_POOL_MSA`.

### What to verify

```sql
SELECT ISNULL(ALLOC_REASON,'(null)') AS reason, COUNT(*)
FROM ARS_ALLOC_WORKING GROUP BY ALLOC_REASON ORDER BY COUNT(*) DESC;

-- Drill into a specific row's audit trail
SELECT SHIP_QTY, HOLD_QTY, FNL_Q, FNL_Q_REM, RDC_FNL_Q_REM_LIVE,
       SKIP_REASON, ALLOC_REASON, ALLOC_REMARKS
FROM ARS_ALLOC_WORKING
WHERE WERKS = 'HB05' AND GEN_ART_NUMBER = '1114093352' AND CLR = 'BLK';
```

---

## STEP 2D — Caps (between Stage C and Stage D)

| Cap | When it fires | What it does |
|---|---|---|
| `_stage_c_apply_mbq_cap` | After waterfall, per OPT_TYPE | Trims rows whose cumulative `SHIP` per `(WERKS, MAJ_CAT)` exceeds `cap_pct × MJ_MBQ − MJ_STK_TTL`. Lowest-priority rows trimmed first |
| `_apply_sec_grid_cap_pre_gate` | Main pass, if enabled | OPT-level pre-gate: if shipping an OPT would push a Secondary grid past its cap (default 130%, per-grid override via `ARS_GRID_BUILDER.sec_cap_pct`), the OPT is skipped whole — unless `OPT_REQ ≥ SEC_CAP_PRE_OVERRIDE_OPT_REQ_PCT × OPT_MBQ` (high-demand override). Only grids with `sec_cap_applicable=1` participate; see `sec_cap_per_grid_proposal.md` for the per-grid opt-in flag added 2026-05-17. |

---

## STEP 2E — Fallback (REMOVED 2026-05-16)

The optional fallback phase (`_run_fallback_new`, F1 boost + F5 Primary-grid demotion + pack-round + post-trim sec-cap) has been **removed from the engine**. There is no `enable_fallback` toggle, no `fallback_growth_pct`, no fallback-related request fields on the listing endpoint. The orchestrator goes straight from Stage C waterfall + caps → Stage D reflect.

If you need under-allocated stock to surface again, raise the per-OPT-type MBQ cap sliders (`rl_mbq_cap_pct` / `tbc_mbq_cap_pct` / `tbl_mbq_cap_pct`) — those serve the same effect at the main pass.

See `backend/app/docs/processes/fallback_archived.md` for the full historical design if you ever want to rebuild it.

---

## STEP 2F — Stage D: Reflect (`_stage_d_reflect`)

Aggregates `ARS_ALLOC_WORKING` row-grain results back onto `ARS_LISTING_WORKING` OPT-grain. Updates:

- `ALLOC_QTY` = `SUM(SHIP_QTY)` over the OPT's VAR×SZ rows
- `HOLD_QTY` = `SUM(HOLD_QTY)`
- `ALLOC_STATUS` = ALLOCATED / PARTIAL / SKIPPED
- `ALLOC_REASON`, `FINAL_OPT_TYPE` (e.g. TBL → NL after first ship), `ALLOC_REMARKS`

Right before `_cleanup` drops `#nre_pool`, `_snapshot_live_pool_to_alloc` (F3, May 2026) captures the live pool state into a new column `RDC_FNL_Q_REM_LIVE` and appends `LIVE_POOL=<n>;` to every `NO_POOL_MSA` remark. This is the truthful answer to "what was actually left in the pool?" — the existing `FNL_Q_REM` column is the cap-restored value, not the live one.

---

## STEP 3 — Park & Dispatch

- **Park:** `POST /api/v1/allocation/park` snapshots `ARS_ALLOC_WORKING` → `ARS_ALLOC_PARKED` with a `SESSION_ID` and `PARK_STATUS='PARKED'`. Multiple sessions can park side by side for comparison.
- **Review:** Open `ListingPage` or `AllocationPage`. Compare two parked sessions via `scripts/inspect_parked_diffs.py`.
- **Dispatch:** `POST /api/v1/allocation/dispatch` flips `PARK_STATUS='DISPATCHED'` and pushes the allocation to SAP. After dispatch the parked session is the source of truth for in-transit hold tracking.

---

## Day-in-the-life checklist

When you run the pipeline for a new MAJ_CAT, do these in order:

1. **Pre-flight:** Uploads for store_stock, sales, MSA, grids are fresh.
2. **Generate listing:** `POST /api/v1/listing/generate` with `Stock%`, `MinSz`, OPT-type slider settings.
3. **Inspect OPT_TYPE split:** if all MIX → MSA is dry; if all TBL → no store stock loaded. Fix upstream and re-run.
4. **Generate allocation:** `POST /api/v1/allocation/run` (or the listing page button).
5. **Verify reason distribution** — `SHIPPED_FULL + SHIPPED_PARTIAL` should be a meaningful fraction. If `BLOCKED_NO_POOL_MSA` dominates, MSA is too thin — consider enabling fallback or growing MBQ caps.
6. **Drill into specific rows** using `scripts/audit_no_pool_msa.py` or the verification queries above. The combination of `SKIP_REASON`, `ALLOC_REMARKS`, and `RDC_FNL_Q_REM_LIVE` tells you exactly what blocked any row.
7. **Park & compare** before dispatching. `scripts/inspect_parked_diffs.py` shows row-level drift between two parked sessions.
8. **Dispatch** when satisfied.

---

## Quick reference — where each thing lives in code

| Concern | File:line |
|---|---|
| OPT_TYPE classification | `backend/app/api/v1/endpoints/listing.py :: _classify_opt_type` (Part 3.6) |
| Stage A rules R01-R09 | `backend/app/services/rule_engine_new.py :: _stage_a_apply_rules` |
| OPT_PRIORITY_RANK assignment | `rule_engine_new.py :: _stage_a_assign_rank` |
| Stage B explode + MJ_REQ gate | `rule_engine_new.py :: _stage_b_explode` |
| Stage B fill CONT + SZ_MBQ | `rule_engine_new.py :: _stage_b_fill_cont` / `_stage_b_fill_targets` |
| Waterfall band (the heart) | `rule_engine_new.py :: _run_band_and_revalidate_batched` |
| Caps (MBQ / SEC / MJ_REQ) | `rule_engine_new.py :: _stage_c_apply_mbq_cap`, `_apply_sec_grid_cap_pre_gate`, `_stage_c_apply_mj_req_cap` |
| Fallback orchestration | `rule_engine_new.py :: _run_fallback_new` |
| Stage D reflect + finalize | `rule_engine_new.py :: _stage_d_reflect` |
| Live pool snapshot (F3 fix) | `rule_engine_new.py :: _snapshot_live_pool_to_alloc` |
| Audit script | `backend/scripts/audit_no_pool_msa.py` |
| Pipeline doc (deep) | `backend/app/docs/processes/ars_pipeline_complete_reference.md` |
| OPT_TYPE doc (operator-friendly) | `backend/app/docs/processes/opt_type_classification.md` |

---

## How to update this doc

This doc is **the operator's hands-on reference** for the current rule engine.
A scheduled routine re-reads `rule_engine_new.py` + `listing.py` periodically
and refreshes the SKIP_REASONs, rule numbers, code anchors, and tunable
defaults. Bump `last_reviewed` whenever you edit by hand.

If you change the engine, the things most likely to need an update here are:
- The OPT_TYPE decision-tree table (Step 1)
- The rules table in Stage A (R01-R09 — add a new row when a rule is added)
- The SKIP_REASON table in Stage C
- The code-anchor table at the bottom (function names / line ranges)
