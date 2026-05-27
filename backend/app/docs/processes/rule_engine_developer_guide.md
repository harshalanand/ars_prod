---
title: Rule Engine — Developer Reference Guide (pandas path)
category: Allocation
order: 12
source: backend/app/services/rule_engine_pandas.py, backend/app/services/rule_engine_new.py
last_reviewed: 2026-05-17
audience: backend developers
---

# Rule Engine — Developer Reference Guide

> **Who this is for.** Backend developers who have read the codebase but
> do not yet understand the business rules. The goal is to leave you able
> to safely **modify** or **add** rules without breaking the engine.
>
> **Scope.** The pandas allocation path: `rule_engine_pandas.py`. Stage A
> and Stage B are SQL (they live in `rule_engine_new.py` and are called
> by pandas), Stage C is pure pandas, Stage D and the sec-cap pre-gate
> are SQL again. No code audit, no SQL evidence dumps — examples are
> illustrative and self-consistent.

---

## 0. Glossary

| Term | Definition |
|------|------------|
| **OPT** | One row at `(WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR)` grain. The smallest unit the listing rules reason about. |
| **OPT_TYPE** | `RL` (regular listing, replenishment), `TBC` (to-be-cleared, fast turnover), `TBL` (to-be-listed, new option that needs a full display). Mutually exclusive per OPT. |
| **MAJ_CAT / MJ** | Major Category, e.g. `M_TEES_PN_HS` (Men's Tees Plain Half-Sleeve). All grids and growth caps live at this level. |
| **WERKS** | SAP plant code. One WERKS = one physical store. |
| **RDC** | Regional Distribution Centre. The SLOC the store pulls stock from. Was `ST_CD` in older MSA tables. |
| **GEN_ART** | Generic Article (e.g. style 12345). A WERKS × GEN_ART × CLR is the OPT identity (per MAJ_CAT). |
| **CLR** | Colour code. |
| **SIZE / SZ** | Garment size (S, M, L, XL …). |
| **VAR_ART** | Variant article number — the GEN_ART expanded by colour. One VAR_ART has many SIZE rows. |
| **MBQ** | Minimum Backstop Quantity — the display-fill target. `SZ_MBQ` is the per-size MBQ; `MJ_MBQ` aggregates per (WERKS, MAJ_CAT). |
| **SZ_MBQ_WH** | The **warehouse-hold buffer** size of an MBQ — extra stock held centrally per size on top of the rotating MBQ. Only TBL allocations write into HOLD. |
| **ACS_D** | **Accessories density** — the display quantity of ONE OPT. *NOT* a daily sale. Used as the "is this OPT meaningful at this store" yardstick. |
| **MAX_DAILY_SALE** | The actual sales-velocity column. Use this when you mean "fast-moving". |
| **I_ROD** | Index of Rounds Of Dispatch. How many rotations of MBQ this OPT is allowed to ship in one cycle. `r=2, I_ROD=3` means it gets three rounds. |
| **RNG_SEG** | Range Segment = MRP tier: `E` (Entry), `V` (Value), `P` (Premium), `SP` (Super Premium). |
| **MJ_RNG_SEG** | The primary grid keyed at `(WERKS, MAJ_CAT, RNG_SEG)`. Always present. |
| **MJ_FAB / MJ_MACRO_MVGR / MJ_MICRO_MVGR / MJ_M_VND_CD** | Example **secondary** grids — fabric / macro merchandise group / micro merchandise group / vendor. Optional, configured in `ARS_GRID_BUILDER`. |
| **PRI / PRI_CT%** | Primary Coverage %. Of all primary grids the OPT belongs to, what fraction still need stock. `100` = every primary slot is open; `<100` = at least one is already filled. |
| **MSA** | Multi-Store Allocation. The upstream stock pool that the allocator consumes (`ARS_MSA_VAR_ART`). `FNL_Q` = available units per (RDC, MAJ_CAT, GEN_ART, CLR, VAR_ART, SZ). |
| **SHIP** | Units shipped to the store **right now**. |
| **HOLD** | Units committed but parked in the warehouse for the next round (TBL only). |
| **FNL_Q / FNL_Q_REM** | The pool's stock — initial vs. live remaining after consumption. |
| **REQ_REM** | The remaining MAJ_CAT (or grid) requirement at a store, after deductions across opt_types and rounds. |
| **Sec-cap** | Secondary-grid cap — a 130% ceiling on grid-level shipments to stop one fabric/colour/vendor flooding a store. |
| **Growth %** | A cap percentage applied at MAJ_CAT × grid level (never per OPT_TYPE) that lets MBQ headroom temporarily exceed 100% to drain a fat pool. |
| **Fallback** | Removed 2026-05-16. The 130% pre-gate sec-cap is the only post-main "second look". See `fallback_archived.md`. |

---

## 1. The big picture

```
┌─────────────────────────────────────────────────────────────────────────┐
│  INPUTS (already on disk before the engine runs)                         │
│  • ARS_LISTING_WORKING   one row per OPT                                │
│  • ARS_MSA_VAR_ART       per-size pool (FNL_Q)                          │
│  • Master_CONT_SZ        size-contribution % by MAJ_CAT × SZ            │
│  • ARS_GRID_BUILDER      grid definitions (primary + secondary)         │
└─────────────────────────────────────────────────────────────────────────┘
                              │
       ┌──────────────────────┴────────────────────────┐
       │ run_listing_and_allocation_pandas()           │
       │ rule_engine_pandas.py:376                     │
       └───────────────────────┬───────────────────────┘
                               │
   STAGE A (SQL, one-shot)     │  rne._stage_a_apply_rules     listing.py:258
       reads  ARS_LISTING_WORKING
       writes LISTED_FLAG, LISTED_REASON, OPT_PRIORITY_TIER, OPT_PRIORITY_RANK
       output: ARS_LISTED_OPT  (one row per surviving OPT)
                               │
   STAGE B (SQL, one-shot)     │  rne._stage_b_explode         listing.py:692
       reads  ARS_LISTED_OPT + ARS_MSA_VAR_ART + Master_CONT_SZ
       writes ARS_ALLOC_WORKING (one row per OPT × VAR × SZ)
       fills  SZ_MBQ, SZ_MBQ_WH, SZ_STK, SZ_REQ, FNL_Q
                               │
   STAGE C (pandas, per MAJ)   │  _run_majcat_waterfall        pandas:1134
       loop  RL → TBC → TBL
       loop    round r = 1..max(I_ROD)
       loop      _run_band  (vectorised pool draw)
       loop      _revalidate_after_band  (MJ_REQ_REM, PRI_CT_REM)
                               │
   STAGE D (SQL, one-shot)     │  finalise + sec-cap + reflect
       1) _apply_sec_grid_cap_pre_gate (blocks OPTs that breach 130%)
       2) _classify_alloc_reason       (writes ALLOC_REASON / SKIP_REASON)
       3) _stage_d_reflect             (aggregates SHIP/HOLD onto LISTING)
                               │
                               ▼
       OUTPUT: ARS_ALLOC_WORKING (per-size) + ARS_LISTING_WORKING (per-OPT)
       OUTPUT: snapshotted into ARS_*_PARKED by parked_history.snapshot_run()
```

Two grains, two tables. Stage A decides **whether** an OPT lists; Stage B
**explodes** each listed OPT to every (VAR_ART × SIZE) row; Stage C
**drains** the per-size pool; Stage D **aggregates** size-level totals
back up to the OPT grain.

---

## 2. Stage A — Listing rules

Stage A is a chain of `CASE … END` expressions concatenated into
`LISTED_REASON`. If the chain is empty after evaluation,
`LISTED_FLAG = 1` and the OPT survives. All rules sit in
`_stage_a_apply_rules` at [rule_engine_new.py:258](backend/app/services/rule_engine_new.py#L258).

Feature flags are at [rule_engine_new.py:31-39](backend/app/services/rule_engine_new.py#L31) —
turn any single rule off without touching SQL.

### Rule R01 — must be explicitly listed

**Purpose.** Buyer choice. The user has uploaded a column `LISTING` on the
working table where `1` means "consider this OPT". If they wrote anything
else (0, NULL, "DROP"), business intent is "don't list" and the engine
respects it.

**Inputs.** `LISTING` (TRY_CAST INT).

**Logic.**
```
listed_reason += 'R01_LISTING;' if LISTING <> 1
```

**Worked example** (MAJ = M_TEES_PN_HS):

| WERKS | GEN_ART | CLR  | LISTING | R01 fires? |
|-------|---------|------|---------|------------|
| HU45  | 12345   | 0001 | 1       | no — passes |
| HU45  | 12345   | 0002 | 0       | yes → dropped |
| HU45  | 67890   | 0001 | NULL    | yes → dropped |

**Where to change.** [rule_engine_new.py:279-280](backend/app/services/rule_engine_new.py#L279).
**Common extension.** To accept multiple "listed" codes (1 = list, 2 =
test-list), edit the CASE: `WHEN ISNULL(TRY_CAST([LISTING] AS INT),0) NOT IN (1, 2) THEN …`.

### Rule R02 — never list a MIX OPT

**Purpose.** MIX OPT_TYPEs are a planning artefact, not a sellable
display. Letting them through would explode in Stage B against an
MSA that has no MIX-tagged pool.

**Logic.** `OPT_TYPE = 'MIX' → drop`.

**Where to change.** [rule_engine_new.py:281-282](backend/app/services/rule_engine_new.py#L281).

### Rule R04 — MSA pool must be positive (or warehouse hold inherited)

**Purpose.** No stock = no allocation. But an OPT that had stock parked
in `RL_HOLD_QTY` from a prior cycle (the ARS_NL in-transit qty) is
genuinely fillable from hold even if MSA is empty today.

**Logic.**
```
listed_reason += 'R04_MSA_POS;' if MSA_FNL_Q <= 0 AND RL_HOLD_QTY <= 0
```

**Worked example:**

| WERKS | GEN_ART | MSA_FNL_Q | RL_HOLD_QTY | R04 fires? |
|-------|---------|-----------|-------------|------------|
| HU45  | 100     | 12        | 0           | no |
| HU45  | 200     | 0         | 8           | no — hold rescues it |
| HU45  | 300     | 0         | 0           | yes → dropped |

**Where to change.** [rule_engine_new.py:287-292](backend/app/services/rule_engine_new.py#L287).
**Common extension.** If you grow a third hold source (e.g. inter-store
transfer in flight), add it to the OR: `… AND ISNULL(IST_HOLD_QTY,0) <= 0`.

### Rule R05 — OPT_REQ_WH must be at least 1

**Purpose.** If the OPT's warehouse-side requirement is less than one
unit, there is nothing meaningful to ship. Saves Stage B from creating
rows that will all skip.

**Where to change.** [rule_engine_new.py:293-294](backend/app/services/rule_engine_new.py#L293).

### Rule R06 — PRI_CT% gate

**Purpose.** `PRI_CT%` measures what fraction of an OPT's primary-grid
slots still need stock at this store. **TBL** must have all primaries
open (a new option needs a full display); **RL** and **TBC** only get
this gate when the buyer turns it on per OPT_TYPE.

**Logic.**
```
opt_types_enforced = {'TBL'}
if pri_ct_check_rl:  opt_types_enforced.add('RL')
if pri_ct_check_tbc: opt_types_enforced.add('TBC')

listed_reason += 'R06_PRI_100;'
    if PRI_CT% < 100
       AND ALLOC_FLAG != 1
       AND OPT_TYPE IN opt_types_enforced
```

**Worked example** (`pri_ct_check_rl = False`):

| WERKS | OPT_TYPE | PRI_CT% | ALLOC_FLAG | R06 fires? |
|-------|----------|---------|------------|------------|
| HU45  | RL       | 80      | 0          | no — RL not enforced |
| HU45  | TBL      | 100     | 0          | no |
| HU45  | TBL      | 80      | 0          | yes → dropped |
| HU45  | TBC      | 80      | 1          | no — ALLOC_FLAG=1 bypass |

**Where to change.** [rule_engine_new.py:295-306](backend/app/services/rule_engine_new.py#L295).
The two flags `pri_ct_check_rl` and `pri_ct_check_tbc` are wired through
the API (`/listing/run-allocation`) → `run_listing_and_allocation_pandas`
([rule_engine_pandas.py:389-390](backend/app/services/rule_engine_pandas.py#L389)).

### Rule R07 — TBL size-coverage ratio

**Purpose.** A "to be listed" option deserves a meaningful display, not
two stray sizes. If too few sizes have stock, skip.

**Logic.**
```
listed_reason += 'R07_VAR_RATIO_TBL;'
    if OPT_TYPE = 'TBL'
       AND VAR_FNL_COUNT / VAR_COUNT < size_threshold (0.6)
       AND VAR_FNL_COUNT < min_size_count (3)
```
Both gates must be true to skip — a low **ratio** alone is acceptable if
absolute fill is healthy, and vice versa.

**Worked example** (thr=0.6, min=3):

| WERKS | OPT_TYPE | VAR_COUNT | VAR_FNL_COUNT | ratio | R07 fires? |
|-------|----------|-----------|---------------|-------|------------|
| HU45  | TBL      | 6         | 2             | 0.33  | yes (ratio<0.6 AND fnl<3) |
| HU45  | TBL      | 6         | 4             | 0.67  | no |
| HU45  | TBL      | 6         | 3             | 0.50  | no (fnl == 3, ≥ min) |
| HU45  | RL       | 6         | 1             | 0.17  | no (only fires on TBL) |

**Where to change.** [rule_engine_new.py:307-314](backend/app/services/rule_engine_new.py#L307).
Threshold knobs are passed in from `run_listing_and_allocation_pandas`
([rule_engine_pandas.py:386-387](backend/app/services/rule_engine_pandas.py#L386)).
There is also a **live** version of this rule that re-fires during Stage C
once pool has drained — see `_rerank_opt_priority_pandas`
([rule_engine_pandas.py:1269](backend/app/services/rule_engine_pandas.py#L1269)).

### Rule R09 — headroom-trivial check (unified)

**Purpose.** Even if all the above pass, the gap between the boosted
MBQ ceiling and the stock already in the store may be smaller than
half an OPT's display. Shipping into that tiny gap wastes pool — the
store can't house even half a display.

**Logic** (one expression for RL / TBC / TBL):
```
cap_factor = case OPT_TYPE
    when 'RL'  then rl_mbq_cap_pct  / 100  (or 1.0 if cap=0)
    when 'TBC' then tbc_mbq_cap_pct / 100  (or 1.0 if cap=0)
    when 'TBL' then 1.0                    (TBL has no MJ-cap)

headroom = cap_factor × MJ_MBQ − MJ_STK_TTL
listed_reason += 'R09_HEADROOM_TRIVIAL;' if headroom < 0.5 × ACS_D
```

**Worked example** (cap_pct=130 → factor=1.3, ACS_D=18, threshold=9):

| WERKS | OPT_TYPE | MJ_MBQ | MJ_STK_TTL | headroom | R09 fires? |
|-------|----------|--------|------------|----------|------------|
| HU45  | RL       | 100    | 80         | 50       | no |
| HU45  | RL       | 100    | 125        | 5        | yes (<9) |
| HU45  | TBL      | 100    | 95         | 5        | yes (factor=1.0) |

**Where to change.** [rule_engine_new.py:326-342](backend/app/services/rule_engine_new.py#L326).
**Common extension.** To make TBL use a 110% boost cap, replace `WHEN
'TBL' THEN 1.0` with `WHEN 'TBL' THEN 1.10`. Remember the saved invariant
— this is the *only* place TBL caps live. Do **not** introduce a separate
per-OPT_TYPE growth %; growth lives at MAJ_CAT + grid level.

### After the chain: tier and rank

Once `LISTED_FLAG=1` is set, `_stage_a_assign_tier`
([rule_engine_new.py:447](backend/app/services/rule_engine_new.py#L447))
buckets surviving OPTs into focus tiers (1 = focus-uncapped, 2 =
focus-capped, 3 = regular), and `_stage_a_assign_rank`
([rule_engine_new.py:460](backend/app/services/rule_engine_new.py#L460))
assigns `OPT_PRIORITY_RANK = ROW_NUMBER()` within each
`(WERKS, OPT_TYPE, MAJ_CAT)` ordered by:

```
OPT_PRIORITY_TIER  ASC,
SEC_CT%            DESC,   -- secondary contribution %
MAX_DAILY_SALE     DESC,   -- velocity
OPT_REQ_WH         DESC    -- size of the warehouse need
```

GEN_ART + CLR are appended as identity tie-breakers so reruns produce
the same rank on the same input.

Finally `_stage_a_materialize_listed`
([rule_engine_new.py:652](backend/app/services/rule_engine_new.py#L652))
INSERTs the survivors into `ARS_LISTED_OPT`. **Critical:** that SELECT
also carries forward every grid-extra column (FAB, MICRO_MVGR, RNG_SEG,
…) discovered by `_collect_grid_extra_cols`
([rule_engine_new.py:616](backend/app/services/rule_engine_new.py#L616)).
If you add a new grid extra column to working_table, no further work is
needed here — it propagates automatically. **Saved invariant**: grid
extras MUST propagate listing → listed → alloc; without it Stage D's
sec-cap silently drops grids.

---

## 3. Stage B — OPT_TYPE & cap construction

Stage B turns each listed OPT into one row per `(VAR_ART, SZ)` and
attaches the per-size targets the waterfall will draw against.

### Step B1 — Explode

`_stage_b_explode` ([rule_engine_new.py:692](backend/app/services/rule_engine_new.py#L692))
joins `ARS_LISTED_OPT` × `ARS_MSA_VAR_ART` on `(MAJ_CAT, GEN_ART_NUMBER,
CLR, RDC)`. Each LISTED row matches one row per variant-size in the MSA
pool. Filters applied at explode time:

| Filter | Why |
|--------|-----|
| `FNL_Q > 0` | no pool, no row |
| `PRI_CT% = 100` for enforced OPT_TYPEs | mirrors R06 at size grain |
| `MJ_REQ ≥ 0.5 × ACS_D` | inclusive boundary — borderline OPTs survive |

The result is `ARS_ALLOC_WORKING`, one row per OPT × VAR_ART × SZ. The
columns `OPT_TYPE`, `I_ROD`, `OPT_PRIORITY_RANK`, all grid extras, and
`FNL_Q` are copied from the parent OPT.

### Step B2 — Fill CONT (size contribution)

`_stage_b_fill_cont` ([rule_engine_new.py:782](backend/app/services/rule_engine_new.py#L782))
looks up `Master_CONT_SZ` in a three-step fallback:

1. Store-specific: `ST_CD = WERKS AND MAJ_CAT = … AND SZ = …`.
2. Chain default: `ST_CD = 'CO' AND MAJ_CAT = … AND SZ = …`.
3. Uniform: `CONT = 1.0 / COUNT(DISTINCT SZ)` per (WERKS, MAJ_CAT).

`CONT` is the fraction of an OPT's MBQ that this size should carry
(e.g. S=15%, M=30%, L=30%, XL=20%, XXL=5%).

### Step B3 — Compute per-size MBQ / REQ

`_stage_b_fill_targets` ([rule_engine_new.py:814](backend/app/services/rule_engine_new.py#L814)):

```
SZ_MBQ    = ROUND(OPT_MBQ    × CONT)       -- rotating MBQ per size
SZ_MBQ_WH = ROUND(OPT_MBQ_WH × CONT)       -- with warehouse hold buffer
SZ_STK    = current store stock at that size (or 0 if grid table missing)
SZ_REQ    = max(0, SZ_MBQ    − SZ_STK)
SZ_REQ_WH = max(0, SZ_MBQ_WH − SZ_STK)
```

`SZ_MBQ_WH` is always ≥ `SZ_MBQ`. The difference is the warehouse-hold
buffer the engine is allowed to stage centrally for the next round
(TBL only).

### How `MJ_REQ_REM` is built and depleted

`MJ_REQ_REM` lives on the **working** table at OPT grain. Its initial
value is `MJ_REQ` (= `MJ_MBQ` − `MJ_STK_TTL`, with floor at 0). After
every band, `_revalidate_after_band`
([rule_engine_pandas.py:1791](backend/app/services/rule_engine_pandas.py#L1791))
decrements it by `Σ(ROUND_SHIP)` over all the band's OPT rows in this
MAJ_CAT, at every active **primary** grid grain.

When MJ_REQ_REM at a store falls below `0.5 × ACS_D`, the engine marks
the store **broken** for *all remaining* OPT_TYPEs — the store has
finished its meaningful share for this MAJ_CAT this cycle. See
`_pre_band_check` ([rule_engine_pandas.py:1712](backend/app/services/rule_engine_pandas.py#L1712))
and the `SKIP_STORE_BROKEN` audit string.

### Worked example — one MAJ_CAT × three stores

Setup: MAJ_CAT = M_TEES_PN_HS, ACS_D = 18, RL cap_pct = 130.

| WERKS | MJ_MBQ | MJ_STK_TTL | MJ_REQ | MJ_REQ_REM₀ | budget (live, before RL) |
|-------|--------|------------|--------|-------------|--------------------------|
| HU45  | 100    | 30         | 70     | 70          | max(0, 70 + 0.30×100) = 100 |
| HU22  | 80     | 70         | 10     | 10          | max(0, 10 + 0.30×80)  = 34  |
| HU99  | 120    | 50         | 70     | 70          | max(0, 70 + 0.30×120) = 106 |

After RL ships 40 units to HU45 and 20 to HU99 (band totals):

| WERKS | MJ_REQ_REM (after RL) | budget (live, before TBC) |
|-------|-----------------------|---------------------------|
| HU45  | 30                    | 30 + 30 = 60              |
| HU22  | 10                    | 10 + 24 = 34              |
| HU99  | 50                    | 50 + 36 = 86              |

`_live_mbq_budget` ([rule_engine_pandas.py:1078](backend/app/services/rule_engine_pandas.py#L1078))
**rebuilds the dict from MJ_REQ_REM at the start of every band**. One
source of truth — no double-deduction.

**Where to change** the cap formula. [rule_engine_pandas.py:1093](backend/app/services/rule_engine_pandas.py#L1093):
`budget = max(0, MJ_REQ_REM + ((cap_pct − 100) / 100) × MJ_MBQ)`. Note
the saved invariant — cap_pct is **at MAJ_CAT level**, not per OPT_TYPE.
RL and TBC each have their own cap_pct knob; TBL is always 0 (no MJ-cap).

---

## 4. Stage C — The band loop

This is the heart of the engine and the biggest section. Read it
top-to-bottom the first time.

### 4.1 The waterfall structure

```
for ot in [RL, TBC, TBL]:                      # OPT_TYPE order is fixed
    if ot != 'RL':
        _rerank_opt_priority_pandas(...)       # skip dead OPTs, re-rank survivors
    _pre_band_check(...)                       # PRI gate + store-broken eval
    for r in 1..max(I_ROD for this ot):        # round counter
        _run_band(ot, r)                       # vectorised pool draw
        _revalidate_after_band(ot, r)          # decrement REMs, mark skips
```

[rule_engine_pandas.py:1184-1265](backend/app/services/rule_engine_pandas.py#L1184).

Important: **all stores compete in one band call**. The sort inside
`_run_band` enforces priority — there is no per-store outer loop.

### 4.2 Per-OPT pool / ship math

For each row in the band ([rule_engine_pandas.py:1440-1450](backend/app/services/rule_engine_pandas.py#L1440)):

```python
need_ship = max(r × SZ_MBQ − SZ_STK − SHIP_QTY_so_far, 0)

if ot == 'TBL':
    tbl_cum   = SZ_MBQ_WH + (r − 1) × SZ_MBQ          # hold buffer counted once
    need_pool = max(tbl_cum − SZ_STK − POOL_CONSUMED, 0)
    if need_ship == 0: need_pool = 0                  # don't pull pool just to HOLD
else:  # RL or TBC
    need_pool = max(r × SZ_MBQ − SZ_STK − POOL_CONSUMED, 0)
```

In plain English:
- **need_ship** is "how much more must physically move to the store in
  this round to keep the rolling MBQ × round target met".
- **need_pool** is the same, but at the SZ_MBQ_WH ceiling for TBL (so
  TBL is allowed to pull more than ships — the excess goes to HOLD).
- POOL_CONSUMED is a running total across rounds; SHIP_QTY is the same
  for the shippable portion only.

### 4.3 Rotation logic — `I_ROD`

`I_ROD` is "rounds of dispatch" — typically 1 for stable RL, 2-3 for
TBC churn, 1-2 for TBL fresh listings. The band loop runs `1..max(I_ROD)`,
but only rows where `I_ROD >= r` are eligible in round `r`. So an OPT
with `I_ROD = 1` ships once and goes idle; one with `I_ROD = 3` gets
three chances. The target target grows linearly: round 2 needs
`2 × SZ_MBQ`, round 3 needs `3 × SZ_MBQ`.

### 4.4 `SZ_MBQ_WH` — the warehouse-hold buffer

For RL/TBC, `SZ_MBQ_WH == SZ_MBQ` (no buffer). For TBL the buffer is
real: the warehouse holds `(SZ_MBQ_WH − SZ_MBQ)` extra units **per
size** so the next dispatch can re-fill instantly. In the math above,
TBL's first round may pull `SZ_MBQ_WH − SZ_STK` from pool — that whole
quantity is "owed" to the OPT, but only `SZ_MBQ − SZ_STK` of it physically
ships. The rest is recorded as HOLD.

### 4.5 The MJ-cap budget

After computing `need_pool`, the band applies the per-WERKS MJ-cap
([rule_engine_pandas.py:1564-1582](backend/app/services/rule_engine_pandas.py#L1564)):

```python
sub.sort_values(['WERKS', 'OPT_PRIORITY_RANK'])
cum_budget_used = cumsum(take_pool) per WERKS
row_remaining   = max(budget_before − prev_cum, 0)
take_pool       = min(take_pool, row_remaining)
```

Highest-priority OPT eats the per-WERKS budget first. Once exhausted,
later OPTs at the same store see `take_pool = 0`.

**TBL bypasses this MJ-cap** (`cap_pct_for_ot = 0.0` →
`mbq_budget = {}` → the block is a no-op). The comment at
[rule_engine_pandas.py:741-742](backend/app/services/rule_engine_pandas.py#L741)
documents the intent: TBL is bounded only by per-size `SZ_REQ` (since
`Σ SZ_REQ across sizes == MJ_REQ`). If you want TBL to obey a hard
MJ-cap, set its `cap_pct_for_ot` non-zero — but think twice; the engine
was redesigned around this in May-2026 (see `fallback_archived.md`).

### 4.6 Pre-band gate

`_pre_band_check` ([rule_engine_pandas.py:1712](backend/app/services/rule_engine_pandas.py#L1712))
runs **once per OPT_TYPE** (before round 1) and applies two rules:

1. **PRI_CT_REM < 100** → SKIP, for enforced OPT_TYPEs (TBL always;
   RL/TBC when their flag is set). Translation: at least one primary
   grid slot is already filled, so dispatching this OPT to this store
   would over-allocate that primary.
2. **MJ_REQ_REM < 0.5 × ACS_D** → store-broken, SKIP this OPT and all
   remaining OPT_TYPEs at this store. The store has no meaningful gap
   left.

Both write descriptive `ALLOC_REMARKS` (`SKIP_PRI_BROKEN(pri=85.0)`,
`SKIP_STORE_BROKEN(mj_rem=4.2)`) so downstream users see the trigger
value, not a generic flag.

### 4.7 Re-rank between OPT_TYPEs

`_rerank_opt_priority_pandas` ([rule_engine_pandas.py:1269](backend/app/services/rule_engine_pandas.py#L1269))
fires before TBC and before TBL (not RL — RL uses the Stage A rank).
Two steps:

1. **Skip low-coverage OPTs** using *live* `SIZE_RATIO` (computed from
   pool_dict at this moment, not the static Stage A value). Same
   "ratio AND count both below" logic as R07.
2. **Re-rank survivors** using the same Stage A ordering keys but with
   live SIZE_RATIO instead of static, giving a per-WERKS 1..N priority
   for this OPT_TYPE.

### 4.8 Post-band revalidation

After each `_run_band`, `_revalidate_after_band`
([rule_engine_pandas.py:1791](backend/app/services/rule_engine_pandas.py#L1791))
does six things, in order:

1. **MSA_FNL_Q_REM** ← `MSA_FNL_Q_REM − Σ(ROUND_SHIP + ROUND_HOLD)` per OPT.
2. **<grid>_REQ_REM** ← `<grid>_REQ_REM − Σ(ROUND_SHIP)` per grid grain,
   for every primary grid in `_discover_primary_grids`.
3. **H_<grid>_REM** ← `1` if `REQ_REM >= 0.5 × ACS_D AND GH = 1`, else `0`.
4. **PRI_CT_REM** ← `Σ(H_REM) / Σ(GH) × 100`.
5. **Skip-mark** OPTs that have a next band (`I_ROD >= r+1`) and now fail
   one of: `MSA_FNL_Q_REM ≤ 0` → `SKIP_MSA_EXHAUSTED`; PRI gate → 
   `SKIP_PRI_BROKEN`; store-broken → `SKIP_STORE_BROKEN`.
6. Propagate those new skips back to the size-level alloc_df.

The "next band" scoping is important — re-skipping an OPT whose rounds
are already exhausted is meaningless and just adds noise to
SKIP_REASON.

### 4.9 Worked example — RL Round 1 and Round 2 for one OPT

Setup: M_TEES_PN_HS, GEN_ART 12345, CLR 0001, OPT_TYPE = RL,
I_ROD = 2, sizes S/M/L with CONT = 0.20 / 0.50 / 0.30.

**Round 1, size M, store HU45:**

| Field | Value |
|-------|------:|
| OPT_MBQ          | 10   |
| CONT (M)         | 0.50 |
| SZ_MBQ           | 5    |
| SZ_STK           | 1    |
| SHIP_QTY_so_far  | 0    |
| POOL_CONSUMED    | 0    |
| pool key FNL_Q   | 12   |
| need_ship        | max(1×5 − 1 − 0, 0) = 4 |
| need_pool        | max(1×5 − 1 − 0, 0) = 4 |
| take_pool        | min(need_pool, FNL_Q − cum_prev) = min(4, 12) = 4 |
| ROUND_SHIP       | 4 (RL = pool take all ships) |
| ROUND_HOLD       | 0 |

After round 1: SHIP_QTY = 4, POOL_CONSUMED = 4, FNL_Q_REM = 8.

**Round 2 same row:**

| Field | Value |
|-------|------:|
| need_ship        | max(2×5 − 1 − 4, 0) = 5 |
| need_pool        | max(2×5 − 1 − 4, 0) = 5 |
| take_pool        | min(5, 8) = 5 |
| ROUND_SHIP       | 5 |

After round 2: SHIP_QTY = 9, POOL_CONSUMED = 9, FNL_Q_REM = 3. Target
`I_ROD × SZ_MBQ − SZ_STK = 10 − 1 = 9` is met → `ALLOC_STATUS = ALLOCATED`.

### 4.10 Cross-store competition in one band

Both HU45 (rank 1) and HU22 (rank 2) want the same (RDC, M_TEES_PN_HS,
12345, 0001, VAR_ART_X, size M). Pool = 12, both need 8 in round 1.

Sort inside `_run_band` puts HU45 first.
- HU45 cumulative demand = 8 → `remaining = 12 − 0 = 12` → `take = 8`.
- HU22 cumulative demand = 16 → `remaining = 12 − 8 = 4` → `take = 4`.

HU45 ships 8, HU22 partial-ships 4. Pool now 0. HU22 status =
`PARTIAL`.

---

## 5. Stage D — Sec-cap & finalisation

The fallback phase was removed 2026-05-16. What remains in Stage D:

### 5.1 Secondary-grid cap (pre-gate)

`_apply_sec_grid_cap_pre_gate` ([rule_engine_new.py:2612](backend/app/services/rule_engine_new.py#L2612))
runs once after the waterfall finishes and **before** Stage D's reflect.

**What it does.** For every active secondary grid (in
`ARS_GRID_BUILDER`, `grid_group = 'Secondary'`, `sec_cap_applicable = 1`):

1. Compute the **budget** per `(WERKS, MAJ_CAT, <grid_extras>)`:
   `MAX(<grid>_MBQ) × 1.30` (or per-grid `sec_cap_pct` override).
2. Aggregate `ARS_ALLOC_WORKING` to OPT grain, carrying every grid's
   extras (FAB, MICRO_MVGR, etc.).
3. Walk OPTs in priority order, keep per-grid running totals.
4. For each OPT: if any participating grid would exceed budget, block
   the whole OPT (SHIP=0, HOLD=0, `SKIP_REASON='SEC_CAP_PRE_<grid>'`).
5. **High-demand override**: an OPT may still ship if `OPT_REQ ≥ 100%
   × OPT_MBQ` — the store needs a full display.

The cap walks every grid for every OPT (no short-circuit on first
match) so the blame attribution and audit are complete.

**Saved invariants applied here.**
- **MBQ sparseness**: a grid where `<grid>_MBQ = 0` at this grain is
  treated as "no constraint" and skipped, not "zero budget". See
  [rule_engine_new.py:2644](backend/app/services/rule_engine_new.py#L2644).
  Never alter this to treat 0 as a real budget.
- **Grid-extras propagation**: the cap only runs for grids whose extras
  exist on both working_table and alloc_table. If `_collect_grid_extra_cols`
  doesn't pull a column through, the cap silently drops that grid. See
  the propagation chain at [rule_engine_new.py:660-661 and 712-714](backend/app/services/rule_engine_new.py#L660).

### 5.2 Worked example — a sec-cap breach vs. an override

Setup: MJ_FAB grid is Secondary (cap = 130%), grain
`(WERKS, MAJ_CAT, FAB)`. M_TEES_PN_HS at HU45, FAB = COTTON, MBQ = 50.
Budget = 50 × 1.30 = 65.

Two OPTs both have `FAB = COTTON` and want to ship to HU45 in this
order:

| OPT | OPT_PRIORITY_RANK | OPT_REQ | OPT_MBQ | SUM(SHIP_QTY) |
|-----|-------------------|---------|---------|---------------|
| A   | 1                 | 30      | 30      | 30 |
| B   | 2                 | 20      | 25      | 20 |

Walk:
- OPT A: running 0, intended 30 → total 30 ≤ 65 → admit. Running = 30.
- OPT B: running 30, intended 20 → total 50 ≤ 65 → admit. Running = 50.

Third OPT C also FAB=COTTON, OPT_REQ=18, OPT_MBQ=20, SUM(SHIP)=18.
- OPT C: running 50, intended 18 → total 68 > 65 → would breach.
  Override gate: `OPT_REQ (18) ≥ 100% × OPT_MBQ (20)`? **No** (18 < 20)
  → BLOCK. C's rows get SHIP=0, `SKIP_REASON='SEC_CAP_PRE_MJ_FAB'`.
  Running stays at 50.

If C had had `OPT_MBQ = 18` (so `OPT_REQ ≥ OPT_MBQ`), the override
would have fired: C admitted, audit remark `SEC_CAP_PRE_OVERRIDE(...)`,
running advances to 68.

### 5.3 Reason classification and reflect

`_classify_alloc_reason` ([rule_engine_new.py:2434](backend/app/services/rule_engine_new.py#L2434))
fills `ALLOC_REASON` on alloc_table for human consumption.

`_stage_d_reflect` ([rule_engine_new.py:2179](backend/app/services/rule_engine_new.py#L2179))
aggregates `SHIP_QTY` / `HOLD_QTY` from alloc_table back to working_table
at OPT grain, stamps `ALLOC_SEQ` (execution order), and writes the final
per-OPT `ALLOC_STATUS` (`ALLOCATED` / `PARTIAL` / `NOT_ALLOCATED`).

---

## 6. Stage E — Persistence

After the pandas engine returns:

- **`ARS_ALLOC_WORKING`** — one row per OPT × VAR × SZ with the final
  `SHIP_QTY`, `HOLD_QTY`, `ALLOC_QTY = SHIP_QTY`, `POOL_CONSUMED`,
  `FNL_Q_REM`, `ALLOC_STATUS`, `SKIP_REASON`, `ALLOC_WAVE` (e.g. `RL_R2`),
  `ALLOC_ROUND`.
- **`ARS_LISTING_WORKING`** — one row per OPT with the same `SHIP_QTY` /
  `HOLD_QTY` aggregated, plus `ALLOC_SEQ` and a human `ALLOC_REMARKS`
  audit string (`ship=20; hold=5; sizes=4/6; seq=12`).

**SHIP vs. HOLD downstream.**
- `SHIP_QTY` is what physically moves to the store on dispatch.
- `HOLD_QTY` is reserved at the warehouse for the next round (TBL only)
  and tracked in `ARS_NL_TBL_HOLD_TRACKING`. Next-cycle RL/TBC bands
  **consume from hold first**, before touching the RDC pool — see
  step 1b of `_run_band` ([rule_engine_pandas.py:1459-1500](backend/app/services/rule_engine_pandas.py#L1459)).

**Park snapshot.** `parked_history.snapshot_run` copies the three live
tables (ALLOC_WORKING, LISTING_WORKING, LISTING) plus the MSA tables
into `*_PARKED` tagged with the session ID. On approve they promote to
`*_HISTORY`; on reject they stay parked with `PARK_STATUS='REJECTED'`
for audit.

**Audit columns to know.**
- `SKIP_REASON` — coded reason at size grain (`R07_VAR_RATIO_TBL`,
  `SEC_CAP_PRE_MJ_FAB`, `NO_POOL_MSA`, `NO_REQ`, …).
- `ALLOC_REMARKS` — free-form audit chain. Multiple events concatenated
  with `;`. Values include the trigger numbers (`SKIP_PRI_BROKEN(pri=85.0)`).
- `ALLOC_WAVE` / `ALLOC_ROUND` — which band/round filled the row.

---

## 7. End-to-end worked example

**Pick:** M_TEES_PN_HS / store HU45 / GEN_ART 12345 / CLR 0001 /
OPT_TYPE = RL / I_ROD = 2 / ACS_D = 18 / 3 sizes (S, M, L).

### Stage A — listing

Inputs on `ARS_LISTING_WORKING` for the OPT:

| col | val |
|-----|----:|
| LISTING        | 1   |
| OPT_TYPE       | RL  |
| MSA_FNL_Q      | 60  |
| RL_HOLD_QTY    | 0   |
| OPT_REQ_WH     | 30  |
| PRI_CT%        | 100 |
| MJ_MBQ         | 100 |
| MJ_STK_TTL     | 30  |
| ACS_D          | 18  |
| VAR_COUNT      | 3   |
| VAR_FNL_COUNT  | 3   |

Rule chain (with `rl_mbq_cap_pct = 130`, factor = 1.3):
- R01 LISTING=1 → ''
- R02 OPT_TYPE='RL' (not MIX) → ''
- R04 MSA_FNL_Q=60>0 → ''
- R05 OPT_REQ_WH=30≥1 → ''
- R06 PRI_CT%=100 → '' (would not have fired for RL anyway)
- R07 OPT_TYPE!='TBL' → ''
- R09 headroom = 1.3 × 100 − 30 = 100 ≥ 0.5 × 18 = 9 → ''

→ `LISTED_FLAG = 1`, `OPT_PRIORITY_RANK = 3` (say it ranked third for
HU45 in this MJ).

### Stage B — explode + cap construction

OPT explodes into 3 size rows (S, M, L). CONT lookup gives 0.20 / 0.50 /
0.30. OPT_MBQ = 10 → `SZ_MBQ = 2 / 5 / 3`. OPT_MBQ_WH = 15 →
`SZ_MBQ_WH = 3 / 8 / 5`. Suppose `SZ_STK = 0 / 1 / 1` and pool `FNL_Q =
4 / 12 / 8` per size.

| SZ | SZ_MBQ | SZ_MBQ_WH | SZ_STK | SZ_REQ | FNL_Q |
|----|-------:|----------:|-------:|-------:|------:|
| S  | 2      | 3         | 0      | 2      | 4     |
| M  | 5      | 8         | 1      | 4      | 12    |
| L  | 3      | 5         | 1      | 2      | 8     |

`MJ_REQ = 100 − 30 = 70`. The live MJ budget for HU45 before RL band 1
= `70 + 0.30 × 100 = 100`.

### Stage C — band loop

**RL round 1** for this OPT × HU45 (assume ranks 1 and 2 have taken
their share but pool is still 4/12/8 because they wanted different
sizes; budget at HU45 reduced to 80 after their take).

For each size row of our OPT:
```
need_ship = max(1×SZ_MBQ − SZ_STK − 0, 0)
need_pool = max(1×SZ_MBQ − SZ_STK − 0, 0)    # RL: not WH-buffered
take_pool = min(need_pool, FNL_Q_remaining)   # all available
```

| SZ | need_ship | need_pool | take_pool | round_ship |
|----|----------:|----------:|----------:|-----------:|
| S  | 2         | 2         | 2         | 2          |
| M  | 4         | 4         | 4         | 4          |
| L  | 2         | 2         | 2         | 2          |

Total round 1 take = 8. MJ budget used so far at HU45 = 8. Remaining
budget = 72.

After `_revalidate_after_band`:
- `MJ_REQ_REM` ← 70 − 8 = 62.
- `MSA_FNL_Q_REM` ← 60 − 8 = 52.
- This OPT has `I_ROD = 2` so round 2 is coming → no skip.

**RL round 2** (`I_ROD ≥ 2`):
```
need_ship = max(2×SZ_MBQ − SZ_STK − SHIP_so_far, 0)
need_pool = same (RL)
```

| SZ | need_ship | need_pool | take | round_ship |
|----|----------:|----------:|-----:|-----------:|
| S  | max(4−0−2, 0) = 2 | 2 | 2 | 2 |
| M  | max(10−1−4, 0) = 5 | 5 | 5 | 5 |
| L  | max(6−1−2, 0) = 3  | 3 | 3 | 3 |

Total round 2 = 10. MJ budget remaining = 72 − 10 = 62.

Cumulative for the OPT: `SHIP_QTY = 4/9/5` per size = 18 total.
`target = 2×SZ_MBQ − SZ_STK = 4/9/5`. All sizes met →
`ALLOC_STATUS = ALLOCATED` for each size row.

### Stage D — sec-cap and reflect

If FAB grid is active and this OPT's COTTON budget at HU45 is, say,
50 with current running 35 before us, our 18 would total 53 ≤ 65 = 50
× 1.30 → admit. No block.

`_stage_d_reflect` aggregates: `ALLOC_QTY (OPT-level) = 18`,
`HOLD_QTY = 0`, `ALLOC_STATUS = ALLOCATED`, `ALLOC_SEQ = (next available
within HU45's M_TEES_PN_HS)`.

### Stage E — persistence

Three rows land in `ARS_ALLOC_WORKING` with SHIP_QTY 4/9/5. One row in
`ARS_LISTING_WORKING` updated with ALLOC_QTY = 18, ALLOC_REMARKS
`ship=18; hold=0; sizes=3/3; seq=12`. Both snapshotted into
`ARS_ALLOC_PARKED` / `ARS_LISTING_WORKING_PARKED` for review.

---

## 8. How to add a new rule — cookbook

### Recipe 1 — add a new exclusion at Stage A

**Goal.** Block any OPT where `OPT_AGE > 365` (anti-stale guard).

1. Add a feature flag at [rule_engine_new.py:31-39](backend/app/services/rule_engine_new.py#L31):
   ```python
   RULE_R10_OPT_AGE = True
   ```
2. Inside `_stage_a_apply_rules`
   ([rule_engine_new.py:258](backend/app/services/rule_engine_new.py#L258)),
   after the existing `if RULE_R09_TBL_TRIVIAL` block, append:
   ```python
   if RULE_R10_OPT_AGE:
       pieces.append(
         "CASE WHEN ISNULL(TRY_CAST([OPT_AGE] AS INT), 0) > 365 "
         "      THEN 'R10_OPT_AGE;' ELSE '' END"
       )
   ```
3. Confirm `OPT_AGE` exists on `ARS_LISTING_WORKING`. If not, add it
   upstream in the listing pipeline.
4. Tests: run `/listing/run-allocation` on a MAJ_CAT you know has aged
   OPTs and grep `ARS_LISTING_WORKING.LISTED_REASON LIKE '%R10_OPT_AGE%'`.

### Recipe 2 — add a new grid for sec-cap

**Goal.** Add an `MJ_BRAND` secondary grid keyed at
`(WERKS, MAJ_CAT, BRAND)`.

1. Add the grid to `ARS_GRID_BUILDER`:
   ```sql
   INSERT INTO ARS_GRID_BUILDER (grid_name, hierarchy_columns,
       grid_group, sec_cap_applicable, sec_cap_pct, seq, status)
   VALUES ('MJ_BRAND', '["MAJ_CAT","BRAND"]', 'Secondary',
           1, 130, 15, 'ACTIVE');
   ```
2. Confirm `BRAND` column is present on `ARS_LISTING_WORKING`. If not,
   add it during listing generation.
3. Add the per-grid columns to `ARS_LISTING_WORKING`: `BRAND_MBQ`,
   `BRAND_REQ`, `BRAND_REQ_REM`, `BRAND_STK_TTL`, `GH_BRAND`, `H_BRAND`,
   `H_BRAND_REM`. Use the grid-builder helper that builds these for
   existing grids as your template.
4. No code change in `rule_engine_pandas.py` or `rule_engine_new.py` is
   required — `_discover_primary_grids` /
   `_discover_all_active_grids` read GRID_BUILDER on every run, and
   `_collect_grid_extra_cols`
   ([rule_engine_new.py:616](backend/app/services/rule_engine_new.py#L616))
   propagates `BRAND` through listing → listed → alloc automatically.
5. Tests: run a small batch, then check
   `SELECT DISTINCT SKIP_REASON FROM ARS_ALLOC_WORKING WHERE SKIP_REASON
   LIKE 'SEC_CAP_PRE_MJ_BRAND%'`.

**Saved invariant check.** Every new grid extra column must propagate
listing → listed → alloc. `_collect_grid_extra_cols` handles this if the
column exists on `ARS_LISTING_WORKING` at the time Stage A runs.

### Recipe 3 — change the growth cap per MAJ_CAT

**Goal.** Different MAJ_CATs get different RL growth caps (TEES = 130%,
SHIRTS = 110%).

The current engine takes `rl_mbq_cap_pct` as a single float for the
whole run. To make it per-MAJ_CAT:

1. Add a config table (e.g. `ARS_MJ_CAP_CONFIG (MAJ_CAT, RL_CAP_PCT,
   TBC_CAP_PCT)`).
2. In `_pandas_run_one_majcat`
   ([rule_engine_pandas.py:79](backend/app/services/rule_engine_pandas.py#L79))
   right after unpacking `mc`, look up that MAJ_CAT's caps from the
   config table:
   ```python
   eng = get_data_engine()
   with eng.connect() as c:
       row = c.execute(text(
         "SELECT RL_CAP_PCT, TBC_CAP_PCT FROM ARS_MJ_CAP_CONFIG "
         "WHERE MAJ_CAT = :mc"), {"mc": mc}).fetchone()
   if row:
       rl_mbq_cap_pct, tbc_mbq_cap_pct = float(row[0]), float(row[1])
   ```
3. Pass the overridden values into `_run_majcat_waterfall`. They
   already flow through to `_live_mbq_budget` per band.
4. Tests: set TEES = 130 and SHIRTS = 100 in the config; run; check that
   the per-MJ ship totals at a high-pressure store reflect the
   difference.

**Saved invariant check.** Growth lives at MAJ_CAT × grid level, never
per OPT_TYPE. RL and TBC each have their own knob — that's per
OPT_TYPE at MAJ_CAT grain, which is allowed. Do **not** introduce a
fourth knob "TBL_CAP_PCT" — TBL is uncapped by design.

---

## 9. Saved invariants (must always hold)

These are non-negotiable. Every PR touching the engine should be
mentally checked against this list.

1. **ACS_D is accessories density, NOT daily sale.** It is the display
   quantity for ONE OPT. For velocity use `MAX_DAILY_SALE`. The 0.5×
   ACS_D floor everywhere means "below half a meaningful display".

2. **One OPT = one OPT_TYPE.** Per `(WERKS, MAJ_CAT, GEN_ART, CLR)` there
   is exactly one of `RL` / `TBC` / `TBL`. The waterfall depends on this
   for correctness — if you ever see two OPT_TYPEs at the same OPT key,
   that is a data bug.

3. **MBQ sparseness.** In any secondary-grid budget computation,
   `<grid>_MBQ = 0` means **"no constraint at this grain"**, not "zero
   budget". Never apply the 1.30× breach formula when MBQ = 0 — see
   [rule_engine_new.py:2644](backend/app/services/rule_engine_new.py#L2644).

4. **Growth / cap % at MAJ_CAT + grid level only.** Never per OPT_TYPE
   at OPT grain. RL and TBC each have a single store-wide cap_pct knob;
   TBL has none. Anywhere a cap is computed inside the band loop, the
   factor must reduce to `f(MAJ_CAT, grid)` — never `f(OPT_TYPE, OPT)`.

5. **Sec-cap grid extras must propagate.** `FAB`, `MACRO_MVGR`,
   `MICRO_MVGR`, `M_VND_CD`, `RNG_SEG` (and any future grid extra) must
   flow `listing_working → listed → alloc`. Drop any of these along the
   way and the sec-cap pre-gate silently skips that grid. Handler:
   `_collect_grid_extra_cols`.

6. **RNG_SEG is the MRP tier (E/V/P/SP). `MJ_RNG_SEG` is the primary
   grid.** Examples of secondary grids include `MJ_FAB` and
   `MJ_MICRO_MVGR`. Don't confuse RNG_SEG with vendor or merch group.

If you find any of these violated in code, fix the code — don't relax
the rule.
