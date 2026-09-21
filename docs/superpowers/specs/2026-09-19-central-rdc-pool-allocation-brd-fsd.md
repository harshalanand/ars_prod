# Central RDC Pool Allocation — BRD & FSD

**Document version:** 1.0 (FINAL — pending management approval)
**Date:** 2026-09-19
**Module:** Listing & Allocation
**Systems touched:** `listing.py`, `rule_engine_new.py`, `rule_engine_per_opt.py`, `rule_engine_pandas.py`, `ListingPage.jsx`
**Owners:** `ars_flow` (listing / pipeline) · `rule_ars` (rule engine)
**Canonical dossier:** `frontend/public/docs/manual/listing.md` — to be updated after implementation
**Supersedes:** `2026-09-18-central-rdc-pool-allocation-design.md` (draft)

---

# PART A — BRD (Business Requirements)

## A1. Background

ARS currently operates two RDCs (regional distribution centres). Every store is permanently
tagged to exactly one RDC in the store master, and the allocation engine keeps each RDC's stock
in a **separate pool**. A store can only ever be served from the warehouse it is tagged to.

This was correct when the two warehouses served independent regions. It is no longer correct:
stock routinely sits in one warehouse while the demand for it sits in stores of the other, and
ARS has no way to see across the boundary.

## A2. Problem statement

**Stranded stock and unserved demand exist at the same time, in the same run.**

Measured on one option across three stores (full working in §B10, Example E1):

| | Demand | Shipped | Unmet | Stock left idle |
|---|---|---|---|---|
| Today (`All RDCs`) | 62 | **29 (47 %)** | 33 | **46 pcs** |

46 pieces sat in the warehouses while 33 pieces of genuine, qualified store demand went unserved —
purely because the demand and the stock were in different buildings.

Two secondary defects compound it:

- **D-1 — Listing over-promises.** In `All RDCs` mode the listing screen can display the *other*
  RDC's warehouse quantity against a store, so an option is classified and made eligible on stock
  that store can never receive. It then fails at the engine with `POOL_EMPTY`. Listing and
  allocation disagree. (Technical cause in §B3.)
- **D-2 — A missing RDC tag silently kills allocation.** A store with a blank or `'ALL'` RDC value
  matches no pool key and receives **zero** allocation, with no error and no warning.

## A3. Business objective

> Club the stock of all RDCs into a single central pool, allocate from that pool using the
> **existing, unchanged allocation rules**, and after allocation tag each shipment line with the
> RDC that will physically ship it — producing an RDC-wise picking requirement.

Expressed as two strictly separated steps:

| Step | Decides | Grain |
|---|---|---|
| **1. Central allocation** | *how much* each store gets, against the summed stock of all RDCs | store × option × size |
| **2. RDC split** | *which warehouse ships* each allocated line, within that warehouse's real stock | allocation line × source RDC |

## A4. Scope

### In scope

- `All RDCs` RDC-scope mode changes meaning: **club the stock, allocate, then split by RDC**
- A new post-allocation **RDC split pass** producing `SRC_RDC` per shipment and hold line
- An **RDC-wise picking requirement** output
- Cockpit controls for the split policy, and visibility of untagged stores
- Correction of defects D-1 and D-2

### Out of scope

- Any change to `Own` or `Cross` RDC-scope modes — **these must behave exactly as today** (§B13)
- Any change to allocation logic: OPT_TYPE classification, eligibility gates, MBQ / MJ_REQ /
  secondary-grid caps, `I_ROD` rounds, pack-size rounding, store ranking, dispatch mode,
  hold-release retry
- Inter-warehouse stock transfer documents (A → B movement) — assumed handled outside ARS
- Physical consolidation of the two warehouses
- Freight cost optimisation beyond simple RDC preference ordering

## A5. Approved business decisions

Confirmed with management on 2026-09-19:

| # | Decision | Chosen |
|---|---|---|
| **D1** | How a shipment line may be sourced when one RDC cannot cover it | All three policies offered as **radio buttons** in the run cockpit; **Policy 2 — single-source preferred, split only as last resort** is the **default** |
| **D2** | How warehouse holds (`HOLD_QTY`) are sourced | SHIP and HOLD are drawn from **one shared balance ledger**, own-RDC-first, in the same line order. Cross-RDC holds are allowed |
| **D3** | Status of the store→RDC tag | **Option A — the tag remains authoritative.** `Own` / `Cross` keep today's strict tag behaviour; under `All RDCs` the tag is a *sourcing preference* with a 3-tier fallback |

Rationale for D3: `Own` mode uses the tag to select which stores enter the run at all. Allowing
tags to decay would silently shrink the `Own` store list with no error — an unacceptable risk to
an existing production process.

## A6. Business rules

| ID | Rule |
|---|---|
| **BR-RDC-01** | Under `All RDCs`, the allocatable stock for an option-size is the **sum across all RDCs** in scope. |
| **BR-RDC-02** | Central pooling changes **only which pool a row draws from**, never how much a store is entitled to. Every existing gate, cap, round and priority rule applies unchanged. |
| **BR-RDC-03** | After allocation, every shipped and held piece must be tagged to exactly one source RDC. `Σ SRC_QTY = Σ SHIP_QTY` and `Σ SRC_HOLD = Σ HOLD_QTY`. |
| **BR-RDC-04** | No RDC may ever be tagged beyond its own physical stock for that option-size. |
| **BR-RDC-05** | Sourcing preference is the store's own RDC first; the shortfall comes from other RDCs in the configured priority order. |
| **BR-RDC-06** | SHIP and HOLD consume the **same** per-RDC balance ledger. A hold may never reserve stock already committed to a shipment, or vice versa. |
| **BR-RDC-07** | `Own` and `Cross` runs must produce output **identical to today**, verified row-for-row before release. |
| **BR-RDC-08** | A store with a missing or invalid RDC tag must still receive its full allocation under `All RDCs`; only its sourcing preference falls back. |
| **BR-RDC-09** | One shipment line may be sourced from at most `ALC_RDC_MAX_SPLIT` RDCs (default 2), so a single store-size can never be assembled from an unbounded number of warehouses. |
| **BR-RDC-10** | No silent truncation. Any line whose quantity the split pass reduces is stamped in `ALLOC_REMARKS`, rolled up to the option, and counted in the run log and cockpit. |

## A7. Expected benefit

Measured on the reference option (§B10 E1) — same stock, same rules, same store priority:

| Metric | Today | After | Change |
|---|---|---|---|
| Demand served | 29 of 62 | **60 of 62** | **+107 %** |
| Fill rate | 47 % | **97 %** | +50 pts |
| Stock left idle | 46 pcs | 15 pcs | −67 % |
| Stores fully served | 0 of 3 | 2 of 3 | +2 |

Secondary: listing and allocation stop disagreeing (D-1 fixed); no run can be silently zeroed by
dirty store-master data (D-2 fixed).

**Measurement at go-live:** run one live session in both modes and compare Σ `SHIP_QTY`, unmet
demand, and residual stock (verification V2, §B15).

## A8. Risks and mitigations

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | A change intended for `All RDCs` leaks into `Own` / `Cross` | Production allocation corrupted | Every change mode-gated (§B13); release gated on a row-for-row regression diff of a completed `Own` session |
| R2 | An RDC is tagged beyond its physical stock | Picklist cannot be executed | Single running balance ledger; hard assertion BR-RDC-04 in the pass and in tests |
| R3 | SHIP and HOLD double-book the same pieces | Phantom reservations | One shared ledger (D2 / BR-RDC-06) |
| R4 | Split lines create two pick documents for one store-size | Warehouse workload, possible process gap | Policy 2 default minimises splits; Policy 3 available if picking cannot execute them |
| R5 | Cross-shipping generates tiny uneconomic movements | Freight cost | `ALC_MIN_CROSS_SHIP_QTY` business rule reserved (inactive in phase 1) |
| R6 | Split rows break downstream row-grain assumptions | Approve / reporting errors | Splits stored in a **child table**, leaving `ARS_ALLOC_WORKING` one row per store-size (§B7) |
| R7 | Clubbed pool concentrates stock on high-rank stores, starving the tail | Distribution fairness | Store ranking and caps are unchanged; monitor the by-store distribution in the parallel-run phase |

## A9. Assumptions and dependencies

1. Stock physically remains at each RDC; "central" is a planning view, not a physical warehouse.
2. Inter-warehouse dispatch to any store is operationally permitted.
3. The picking / STO process can accept an RDC-wise requirement keyed by source RDC.
4. `Master_ALC_INPUT_ST_MASTER.RDC` continues to be maintained (D3 = Option A).
5. MSA (`ARS_MSA_GEN_ART`, `ARS_MSA_VAR_ART`) continues to publish stock per RDC — the clubbing
   happens in ARS, not in MSA.

## A10. Approval

| Role | Name | Decision | Date |
|---|---|---|---|
| Business owner | | ☐ Approved ☐ Rejected | |
| Supply chain / warehouse | | ☐ Approved ☐ Rejected | |
| ARS product owner | | ☐ Approved ☐ Rejected | |

Specific confirmation requested from the warehouse team on **D1** — whether picking can execute a
two-source line for a single store-size (if not, the default moves to Policy 3; see §B6.3).

---

# PART B — FSD (Functional Specification)

## B1. Solution overview

### B1.1 Mode matrix

| RDC SCOPE button | Stock pool | Store selection | Allocation rules | Post-alloc split | Status |
|---|---|---|---|---|---|
| **All RDCs** | **clubbed across RDCs** | all stores | **unchanged** | **new — RDC split pass** | **CHANGED** |
| **Own** | per-RDC | stores of selected RDC(s) | unchanged | n/a (= own RDC) | **UNCHANGED** |
| **Cross** | per-RDC | stores of `cross_to` | unchanged | n/a (= `cross_from`) | **UNCHANGED** |

No new mode is introduced. `All RDCs` changes meaning from *"run both RDCs side by side"* to
*"club, allocate, split"*.

### B1.2 What the store→RDC tag controls, per mode

| Tag's job | `Own` / `Cross` | `All RDCs` |
|---|---|---|
| Selects which stores enter the run | ✅ yes (unchanged) | ❌ no — all stores enter |
| Selects which stock the store can see | ✅ yes (unchanged) | ❌ no — clubbed pool |
| Selects which warehouse ships it | ✅ implied | ✅ **preference only** (3-tier fallback) |
| Blank / invalid tag | **blocks the run** (as today) | allocation unaffected; sourcing falls back |

## B2. End-to-end processing sequence

Existing Part numbers in `/listing/generate`; the only new step is **Part 8.37**.

| Part | Step | Change under `All RDCs` |
|---|---|---|
| 1-2 | Build `ARS_LISTING` (grid rows + MSA-only options) | none — `RDC` column keeps the **store's** RDC |
| 3.5 / 3.5a | `ACS_D`, `ALC_D`, `I_ROD` | none |
| 3.54 | `RL_HOLD_QTY` from hold tracking | **FS-RDC-02** — read ignoring RDC |
| 3.55 | `MSA_FNL_Q`, `VAR_COUNT`, `VAR_FNL_COUNT` | **FS-RDC-01** — summed across RDCs |
| 3.6 | OPT_TYPE classification | none (same code, clubbed input) |
| 4 / 4c | Grid columns, demand math | none |
| 6 / 6.6 | Store ranking, `ELIG_FLAG` | none |
| 7 | Working table, growth, grid-coverage flags | none |
| 8 | **Rule engine allocation** | **FS-RDC-03** — clubbed pool key |
| 8.35 / 8.36 | `ALLOC_TYPE`, run dates stamp | none |
| **8.37** | **RDC split pass** | **FS-RDC-04 — NEW** |
| 8.4 | Park alloc + listing snapshots | inherits `SRC_RDC` automatically |
| 8.5 / 8.55 | `OPT_STATUS`, hold release | none (Policy 3 exception, §B6.3) |
| 8.6 | NL/TBL hold-tracking schema + write | hold rows carry the **sourcing** RDC |

## B3. FS-RDC-01 — Clubbed MSA quantities

**Site:** `listing.py` Part 3.55 (`:1630-1654` for `MSA_FNL_Q`, `:1662-1700` for
`VAR_COUNT` / `VAR_FNL_COUNT`).

**Today.** The MSA sub-query groups per RDC, but the join back to the listing row adds the
`L.RDC = M.MSA_RDC` predicate **only when `rdc_mode = 'own'`**. In `all` mode an option present at
both RDCs therefore has two candidate rows, and SQL Server's `UPDATE … FROM` resolves the
ambiguity by picking **one arbitrarily** — neither summed nor own-RDC-preferred. This is defect
D-1.

**Required.** Under `All RDCs`:

| Column | New derivation |
|---|---|
| `MSA_FNL_Q` | `SUM(FNL_Q)` across all in-scope RDCs for the option, filtered to the run's `ALLOC_TYPE` |
| `VAR_COUNT` | distinct sizes present across all in-scope RDCs |
| `VAR_FNL_COUNT` | distinct sizes with **clubbed** `FNL_Q > 0` |

`VAR_FNL_COUNT` must be counted on the clubbed figure, not summed per RDC — a size held at both
RDCs is still one size, and this value drives the R07 TBL size-coverage gate.

Under `Own` / `Cross` the existing per-RDC join is retained unchanged.

**Effect:** OPT_TYPE classification and the `NO_STOCK` eligibility gate read a deterministic,
complete number. No change to their logic.

## B4. FS-RDC-02 — Hold read-back

**Site:** `listing.py` Part 3.54 (`:1541`).

`RL_HOLD_QTY` is the prior-run NL/TBL hold for a store-option, read from
`ARS_NL_TBL_HOLD_TRACKING` (which carries an `RDC` column). A hold belongs to a **store-option**;
the RDC records which warehouse is physically reserving it.

Under `All RDCs` the lookup must **ignore RDC** and sum the store-option's holds across
warehouses; otherwise a hold placed at RDC-B last run is invisible to an RDC-A-tagged store this
run, and the reserved stock is stranded permanently.

Under `Own` / `Cross`, unchanged.

## B5. FS-RDC-03 — Clubbed pool key

**Sites:** `rule_engine_per_opt.py:63` (`POOL_KEYS`), `rule_engine_new.py:746` and `:873-874`
(pool build join), `rule_engine_new.py:3321-3366` (`RDC_FNL_Q_REM_LIVE` snapshot).

| | Today | Under `All RDCs` |
|---|---|---|
| Pool key | `(RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ)` | `(MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ)` |
| Pool build join | `L.RDC = V.RDC` | predicate dropped; `GROUP BY` without RDC |
| `FNL_Q_REM` | live residual of that RDC's pool | live residual of the **clubbed** pool |
| `RDC_FNL_Q_REM_LIVE` | per-RDC end-of-run residual | clubbed end-of-run residual |

Column names are retained to avoid churn in reports and audits; their semantics under
`All RDCs` are documented here and in the dossier.

**Nothing else in the engine changes.** The waterfall, gates, caps, rounds, pack-size rounding,
store ranking, dispatch mode and hold-release retry all operate on the clubbed pool exactly as
they operate on a per-RDC pool today.

## B6. FS-RDC-04 — The RDC split pass (NEW, Part 8.37)

### B6.1 Placement

Runs after the run-date stamp (`listing.py:3418`) and **before** parking (`:3467`), so
`ARS_ALLOC_PARKED` → `ARS_ALLOC_HISTORY` → Approve inherit the tagging with no change to
`parked_history.py` (its column-reconcile copies new columns automatically).

### B6.2 Inputs, ordering and ledger

| Input | Source |
|---|---|
| Allocation lines (`SHIP_QTY`, `HOLD_QTY`) | `ARS_ALLOC_WORKING` |
| Per-RDC availability at option-size grain | `ARS_MSA_VAR_ART`, filtered to the run's `ALLOC_TYPE` |
| Store's own RDC | `Master_ALC_INPUT_ST_MASTER` (via `ARS_LISTING_WORKING.RDC`) |
| Split policy, RDC priority | run parameters / business rules (§B8) |

**Processing order:** `ST_RANK`, then the allocation order within a store — identical to the
waterfall, so the result is deterministic and reproducible.

**One ledger (D2 / BR-RDC-06).** A single in-memory balance `rdc_avail[RDC, option, size]`,
initialised from MSA and decremented by **both** SHIP and HOLD draws as they are tagged. SHIP and
HOLD for a line are tagged consecutively, SHIP first.

### B6.3 Sourcing policies (D1 — cockpit radio buttons)

`preference_order(store)` = own RDC first, then remaining RDCs in configured priority order (§B6.4).

| Radio option | Value | Behaviour |
|---|---|---|
| Split whenever short | `SPLIT_ALWAYS` | Walk the preference order, take what each RDC has until the line is filled. Splits whenever the first RDC is short |
| **Single source preferred** *(default)* | `SINGLE_PREFERRED` | First pass: the first RDC in preference order that can cover the **whole** line takes it. Only if none can, fall back to the split walk |
| Strict single source | `SINGLE_STRICT` | Never split. If no RDC can cover the whole line, ship the largest single-RDC quantity available and **reduce the line** |

### B6.3.1 Maximum sources per line (`ALC_RDC_MAX_SPLIT`, default 2)

With two RDCs a split line means at most two pick documents. With three or more, an unconstrained
split walk could assemble one store-size from **every** warehouse — four picklists and four
dispatches for a single line. `ALC_RDC_MAX_SPLIT` bounds it.

| Value | Meaning |
|---|---|
| `1` | equivalent to `SINGLE_STRICT` — never more than one source |
| **`2`** *(default)* | at most two warehouses per line. **Cannot bind while only two RDCs exist** |
| `N` | at most N warehouses per line |

When the cap is reached and the line is still not filled, the remainder is handled exactly like
the `SINGLE_STRICT` shortfall (§B6.3.3): the line is reduced and stamped
`RDC_SPLIT_CAPPED(alloc=A,shipped=B)`. To maximise the fill achievable within the cap, the capped
walk orders candidates **own RDC first, then by availability descending** rather than by plain
priority order. Every capped line is counted and logged — the pass never truncates silently.

### B6.3.2 Algorithm

```
MAX_SPLIT = ALC_RDC_MAX_SPLIT                            # default 2

for each alloc line in (ST_RANK, alloc order):           # SHIP first, then HOLD
    remaining = qty
    order     = preference_order(store)                  # §B6.4 — own first, then priority list
    sources   = 0

    # pass 1 — whole-line cover (SINGLE_PREFERRED / SINGLE_STRICT only)
    if policy in (SINGLE_PREFERRED, SINGLE_STRICT):
        for rdc in order:
            if rdc_avail[rdc, option, size] >= remaining:
                emit(rdc, remaining); rdc_avail[rdc,…] -= remaining
                remaining = 0; sources = 1
                break

    # pass 2 — strict: one source only, reduce the rest
    if remaining > 0 and policy == SINGLE_STRICT:
        best = argmax(rdc_avail[rdc, option, size] for rdc in order)
        take = min(remaining, rdc_avail[best, option, size])
        emit(best, take); rdc_avail[best,…] -= take
        remaining -= take; sources = 1
        reduce_line(remaining, remark = 'RDC_SINGLE_SHORT'); remaining = 0

    # pass 3 — capped split walk
    if remaining > 0:
        candidates = own_rdc_first(order, then by rdc_avail desc)[:MAX_SPLIT]
        for rdc in candidates:
            take = min(remaining, rdc_avail[rdc, option, size])
            if take > 0:
                emit(rdc, take); rdc_avail[rdc,…] -= take
                remaining -= take; sources += 1
            if remaining == 0: break
        if remaining > 0:                                # cap bound before the line filled
            reduce_line(remaining, remark = 'RDC_SPLIT_CAPPED')
            remaining = 0

    assert remaining == 0                                # BR-RDC-03
    assert sources <= MAX_SPLIT                          # I-7
    assert all(v >= 0 for v in rdc_avail.values())       # BR-RDC-04
```

### B6.3.3 Line reduction

Two situations can reduce an allocated quantity — `SINGLE_STRICT` when no RDC covers the line,
and the `ALC_RDC_MAX_SPLIT` cap when it binds before the line is filled. Both must leave the
data internally consistent:

1. `SHIP_QTY` (or `HOLD_QTY`) on `ARS_ALLOC_WORKING` is reduced to the tagged quantity.
2. `ALLOC_REMARKS` gains `RDC_SINGLE_SHORT(alloc=A,shipped=B)` or
   `RDC_SPLIT_CAPPED(alloc=A,shipped=B)`.
3. The option-grain `ALLOC_QTY` / `HOLD_QTY` rollup on `ARS_LISTING_WORKING` is refreshed, so
   Part 8.5 `OPT_STATUS` (which runs after parking) judges the real shipped quantity.
4. The un-tagged remainder stays unallocated and appears in the residual stock report.
5. The run log and the cockpit report the count and quantity of reduced lines — **never a silent
   truncation**.

Under `SPLIT_ALWAYS` and `SINGLE_PREFERRED` with the cap unbound, **no quantity ever changes** —
the pass is pure tagging. With only two RDCs in operation, a cap of 2 can never bind, so the
default configuration is pure tagging.

### B6.4 Sourcing preference resolution (D3 — Option A, 3 tiers)

Applied per store, in order, and stopping at the first tier that yields a preference:

| Tier | Preference order | Applies when | `PREF_TIER` |
|---|---|---|---|
| **1** | Store's own RDC **first**, then the remaining RDCs ordered per §B6.4.1 | tag present and is a real RDC code | `1` / `1F` |
| **2** | Global list `ALC_RDC_PRIORITY` alone | tag blank, `'ALL'`, or not a known RDC | `2` |
| **3** | Largest-available-first, evaluated per line | `ALC_RDC_PRIORITY` inactive or unset | `3` |

A tag naming an RDC with no stock needs no special handling: tier 1 finds nothing available there
and the walk proceeds to the next RDC in the order.

### B6.4.1 Ordering the RDCs after "own"

Two settings can supply that order. The first one defined wins:

| # | Setting | Grain | Keyed on | Rows to maintain |
|---|---|---|---|---|
| **a** | `ALC_RDC_FALLBACK_ORDER` *(optional, unset by default)* | **per RDC** | the store's **own** RDC | one per RDC |
| **b** | `ALC_RDC_PRIORITY` | global | nothing — one list for all | one |

**(a) Per-RDC fallback order.** One ordered list per warehouse, answering *"if a store's own
warehouse is X and X is short, which warehouse do we try next?"* This is the setting that captures
geography — a Delhi store's second-best warehouse is not a Chennai store's second-best — while
costing **N rows for N warehouses**, not one row per store.

| Own RDC | Then try | Effective order for a store tagged there |
|---|---|---|
| A | B, C, D | A, B, C, D |
| B | A, D, C | B, A, D, C |
| C | D, A, B | C, D, A, B |
| D | C, B, A | D, C, B, A |

Stored as the business rule `ALC_RDC_FALLBACK_ORDER`, JSON keyed by own RDC:
`{"A":["B","C","D"], "B":["A","D","C"], "C":["D","A","B"], "D":["C","B","A"]}`.

Rules for a partial or stale map — it is a preference, never a filter:

- An RDC **absent** from a list is appended at the end, in `ALC_RDC_PRIORITY` order, then
  alphabetically. No warehouse is ever excluded from sourcing by omission.
- An own-RDC key **missing** from the map falls through to (b) for that store's order only.
- A name in the map that is **not a live RDC** is ignored.
- The map applies **only to tagged stores** — it is keyed on the own RDC, so an untagged store has
  nothing to look up and resolves at tier 2.

Lines whose order came from the map are audited as `PREF_TIER = '1F'`, so the effect of the map is
measurable in any run.

**(b) Global priority list.** `ALC_RDC_PRIORITY` is an **ordered list naming every RDC in
operation** — `"A,B"` today, `"A,B,C,D"` if warehouses are added — not a pair. It is both the
fallback when no per-RDC map exists and the tie-break that completes a partial map. With two RDCs
the order after "own" is forced and the setting barely matters; from three RDCs onward it decides
most cross-ship routing and must be reviewed whenever an RDC is added. An RDC missing from the
list is appended alphabetically, so a newly created RDC can never be silently excluded.

### B6.4.2 What was deliberately not built

A **per-store** `RDC_PRIORITY` column (considered in the draft) is **dropped**: 451 rows of new
master data to curate, with no demonstrated case that two stores of the same RDC need different
fallback orders. The per-RDC map above captures the same geography at 1 % of the maintenance cost.
If a genuine per-store case ever appears, it slots in ahead of (a) without changing anything else —
the pass only calls `preference_order(store)` and does not care where the list came from.

**Recommended configuration today (2 RDCs):** leave `ALC_RDC_FALLBACK_ORDER` unset and set
`ALC_RDC_PRIORITY = "A,B"`. With two warehouses the per-RDC map can express nothing the own-first
rule does not already give you. Populate it when the third RDC goes live.

### B6.5 Outputs

| Output | Content |
|---|---|
| `ARS_ALLOC_RDC_SPLIT` (new) | one row per **alloc line × source RDC** — `SHIP_QTY`, `HOLD_QTY` |
| `ARS_ALLOC_WORKING.SRC_RDC` | the single source RDC, or `'MULTI'` when the line was split |
| `ARS_ALLOC_WORKING.SRC_SPLIT_CNT` | number of source RDCs for the line (1 or more) |
| RDC-wise picking requirement | `GROUP BY SRC_RDC` over `ARS_ALLOC_RDC_SPLIT` |

## B7. FS-RDC-05 — Data model

**Design point (risk R6):** a split line must **not** create a second row in
`ARS_ALLOC_WORKING`. That table's one-row-per-`(WERKS, option, VAR_ART, SZ)` grain is relied on by
parking, Approve, alloc review and reporting. The split therefore lives in a **child table**.

### New table — `ARS_ALLOC_RDC_SPLIT`

| Column | Type | Notes |
|---|---|---|
| `SESSION_ID` | NVARCHAR(50) | run session |
| `WERKS` | NVARCHAR(50) | store |
| `MAJ_CAT`, `GEN_ART_NUMBER`, `CLR`, `VAR_ART`, `SZ` | | allocation line key |
| `SRC_RDC` | NVARCHAR(20) | **sourcing warehouse** |
| `SHIP_QTY` | FLOAT | pieces shipped from this RDC |
| `HOLD_QTY` | FLOAT | pieces held at this RDC |
| `ALLOC_TYPE` | NVARCHAR(10) | FRESH / GRT |
| `STORE_RDC` | NVARCHAR(20) | store's own tag (`''` when untagged) — audit |
| `PREF_TIER` | NVARCHAR(2) | `1` own+global · `1F` own+per-RDC map · `2` global only · `3` largest-available (audit, §B6.4) |

Grain: one row per line per source RDC. Written for **every** line, including single-source ones,
so the picking requirement is a single clean query. Unique key:
`(SESSION_ID, WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ, SRC_RDC)`.

### Amended tables

| Table | Change |
|---|---|
| `ARS_ALLOC_WORKING` | `+ SRC_RDC NVARCHAR(20)` (`'MULTI'` when split), `+ SRC_SPLIT_CNT INT` |
| `ARS_ALLOC_PARKED` / `ARS_ALLOC_HISTORY` | inherit both columns via the existing column-reconcile — **no code change** |
| `ARS_NL_TBL_HOLD_TRACKING` | existing `RDC` column now stores the **sourcing** RDC under `All RDCs` |
| `ARS_RUN_PARAMS_AUDIT` | new rows: `rdc_mode`, `rdc_split_policy`, `rdc_priority`, `untagged_store_count` |
| `ARS_LISTING` / `ARS_LISTING_WORKING` | **no new columns**; `RDC` continues to mean the store's RDC |

### Business rules registry

| Rule | Default when inactive | Purpose |
|---|---|---|
| `ALC_RDC_SPLIT_POLICY` | `SINGLE_PREFERRED` | D1 policy when not set per-run |
| `ALC_RDC_PRIORITY` | *(unset → tier 3)* | ordered list of **every** RDC, e.g. `"A,B"` / `"A,B,C,D"` |
| `ALC_RDC_FALLBACK_ORDER` | *(unset → use `ALC_RDC_PRIORITY`)* | optional per-RDC fallback map (§B6.4.1a), JSON keyed by own RDC. Leave unset while only 2 RDCs exist |
| `ALC_RDC_MAX_SPLIT` | `2` | maximum source RDCs per line (§B6.3.1); `1` ≡ `SINGLE_STRICT` |
| `ALC_MIN_CROSS_SHIP_QTY` | *(inactive → no minimum)* | reserved for phase 2 freight guardrail |

## B8. FS-RDC-06 — UI (`ListingPage.jsx`)

Pattern: **progressive disclosure.** The three RDC SCOPE buttons are unchanged; a second panel
appears beside them **only** when `All RDCs` is selected, because that is the only mode whose
sourcing is configurable.

### B8.1 `Own` selected — today's screen, plus one warning line

```
┌ RDC SCOPE ──────────────────────────┐
│  [ All RDCs ]  [  Own ✓ ]  [ Cross ]│
│                                     │      (no sourcing panel —
│  RDC   [ A ×] [ C ×]            ▾   │       not applicable to Own)
│  ─────────────────────────────────  │
│  Separate pool per RDC              │
│  ⚠ 312 untagged stores excluded     │
└─────────────────────────────────────┘
```

### B8.2 `All RDCs` selected — sourcing panel appears

```
┌ RDC SCOPE ──────────────────────────┐  ┌ RDC SOURCING ───────────────────────┐
│  [ All RDCs ✓] [  Own  ]  [ Cross ] │  │  Split policy                       │
│                                     │  │   ( ) Split whenever short          │
│  ─────────────────────────────────  │  │   (•) Single source preferred       │
│  Pool: A + B + C + D  →  one pool   │  │   ( ) Strict single source      ⚠   │
│  Stores: 451                        │  │  ─────────────────────────────────  │
│  ⚠ 312 untagged → priority A,B,C,D  │  │  Priority   [A][B][C][D]   ⇅ drag   │
│                                     │  │  Max split per line    [ 2 ▾ ]      │
└─────────────────────────────────────┘  └─────────────────────────────────────┘
```

### B8.3 Control specification

| Control | Type | Behaviour |
|---|---|---|
| RDC SCOPE | existing segmented buttons | unchanged; `All RDCs` help text becomes *"club all RDC stock, allocate, then split RDC-wise"* |
| Live summary | read-only text under the buttons | names the pool composition and store count, so `All` is visibly different from `Own` before Generate |
| Split policy | radio group, 3 options | default `Single source preferred`. `Strict single source` shows the caption *"may reduce an allocated line when no single RDC can cover it"* |
| Priority | drag-to-reorder chips, one per RDC | seeded from `ALC_RDC_PRIORITY`; a new RDC appears appended. Ordering matters from three RDCs onward. When a per-RDC fallback map is active the chips are shown read-only with the note *"overridden per RDC — see Business Rules"* |
| Max split per line | numeric select, `1 / 2 / 3 / …` | default `2`. `1` displays *"equivalent to strict single source"*. Values above the RDC count are clamped |
| Untagged banner | warning line, **all** modes | `All RDCs`: *"⚠ 312 of 451 stores have no RDC tag → sourcing will use global priority A,B"* · `Own` / `Cross`: *"⚠ 312 of 451 stores have no RDC tag → these stores are excluded from this run"* |

The `Own` / `Cross` variant of the banner is information the system has never surfaced and
directly addresses defect D-2. It is a warning only — it never blocks or changes those runs.

`ALC_RDC_FALLBACK_ORDER` (§B6.4.1a) is **not** a cockpit control. It is standing configuration
that changes rarely and applies to every run, so it is edited at Settings → Business Rules with
the registry's existing SUPER_ADMIN gate and change audit — not re-decided per run.

### B8.4 Post-run — RDC SPLIT summary

A results block in the cockpit, reporting what the split pass actually did:

```
┌ RDC SPLIT ───────────────────────────────────────────────────────────────┐
│  PICK BY RDC          A  1,240      B  3,905      C  812      D  457     │
│  Cross-shipped        1,106 pcs  (17%)  to 214 stores                    │
│  Split lines             38  of 8,421   (0.5%)                           │
│  Reduced lines            0   (cap / strict)                             │
│  Untagged sourced     2,180 pcs by priority                              │
│                                                      [ Export picklist ] │
└──────────────────────────────────────────────────────────────────────────┘
```

Those five figures are what tell ops whether clubbing is working: how much moved between
warehouses, how often a line had to split, whether any line was reduced, and how much was routed
by fallback rather than by tag.

### B8.5 Run Parameters page

No work required — `rdc_split_policy`, `rdc_priority`, `rdc_max_split` and `untagged_store_count`
flow into the existing `ARS_RUN_PARAMS_AUDIT` capture and appear automatically in the Compare and
Trends views.

## B9. FS-RDC-07 — Reporting

| Report | Change |
|---|---|
| **RDC-wise picking requirement** (new) | `SELECT SRC_RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ, SUM(SHIP_QTY), SUM(HOLD_QTY) FROM ARS_ALLOC_RDC_SPLIT … GROUP BY …` |
| **Store dispatch summary** (new) | per store × source RDC — how many documents each store expects |
| `by_maj_cat_rdc` cockpit summary (`listing.py:5113-5220`) | under `All RDCs`, group by `SRC_RDC`; under `Own`/`Cross`, unchanged |
| Cross-ship exposure (new) | pieces shipped from an RDC other than the store's own tag — freight monitoring |

## B10. Worked examples

### E1 — End-to-end (the reference case)

**Store master**

| Store | Tagged RDC |
|---|---|
| HS11 | RDC-A |
| HS12 | RDC-A |
| HP04 | RDC-B |

**Warehouse stock** — option `M_TEES_HS / 1110116457 / OFF_WHT`

| RDC | S | M | L | Total |
|---|---|---|---|---|
| RDC-A | 10 | 0 | 5 | **15** |
| RDC-B | 0 | 40 | 20 | **60** |
| **Clubbed** | **10** | **40** | **25** | **75** |

**Store requirement** (after MBQ / I_ROD math)

| Store | RDC | S | M | L | Need |
|---|---|---|---|---|---|
| HS11 | A | 4 | 8 | 6 | 18 |
| HS12 | A | 4 | 8 | 12 | 24 |
| HP04 | B | 4 | 10 | 6 | 20 |
| | | | | | **62** |

**E1.1 — Today (`All RDCs`, two separate pools)**

| # | Store | RDC | Size | Need | Pool before | Shipped | Pool after | Reason |
|---|---|---|---|---|---|---|---|---|
| 1 | HS11 | A | S | 4 | 10 | **4** | 6 | ok |
| 2 | HS11 | A | M | 8 | 0 | **0** | 0 | `POOL_EMPTY` — B holds 40, invisible |
| 3 | HS11 | A | L | 6 | 5 | **5** | 0 | short by 1 |
| 4 | HS12 | A | S | 4 | 6 | **4** | 2 | ok |
| 5 | HS12 | A | M | 8 | 0 | **0** | 0 | `POOL_EMPTY` |
| 6 | HS12 | A | L | 12 | 0 | **0** | 0 | `POOL_EMPTY` |
| 7 | HP04 | B | S | 4 | 0 | **0** | 0 | `POOL_EMPTY` — A holds 2, invisible |
| 8 | HP04 | B | M | 10 | 40 | **10** | 30 | ok |
| 9 | HP04 | B | L | 6 | 20 | **6** | 14 | ok |

| | Demand | Shipped | Unmet | Idle stock |
|---|---|---|---|---|
| **Total** | 62 | **29 (47 %)** | 33 | 46 |

**E1.2 — Step 1: allocate from the clubbed pool** (same order, same gates, same caps)

| # | Store | Size | Need | Pool before | Ship | Pool after |
|---|---|---|---|---|---|---|
| 1 | HS11 | S | 4 | 10 | **4** | 6 |
| 2 | HS11 | M | 8 | 40 | **8** | 32 |
| 3 | HS11 | L | 6 | 25 | **6** | 19 |
| 4 | HS12 | S | 4 | 6 | **4** | 2 |
| 5 | HS12 | M | 8 | 32 | **8** | 24 |
| 6 | HS12 | L | 12 | 19 | **12** | 7 |
| 7 | HP04 | S | 4 | 2 | **2** | 0 |
| 8 | HP04 | M | 10 | 24 | **10** | 14 |
| 9 | HP04 | L | 6 | 7 | **6** | 1 |

| | Demand | Shipped | Unmet |
|---|---|---|---|
| **Total** | 62 | **60 (97 %)** | 2 |

**E1.3 — Step 2: split pass ledger** (default `SINGLE_PREFERRED`, own-RDC-first)

Balances are tracked **per size**. `A bal` / `B bal` are the balances for that line's size at the
moment it is processed.

| # | Store | Own | Size | Qty | A bal | B bal | Covered by | From A | From B | A after | B after |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | HS11 | A | S | 4 | 10 | 0 | A alone (own) | **4** | — | 6 | 0 |
| 2 | HS11 | A | M | 8 | 0 | 40 | B alone | — | **8** | 0 | 32 |
| 3 | HS11 | A | L | 6 | 5 | 20 | B alone (A has 5 < 6) | — | **6** | 5 | 14 |
| 4 | HS12 | A | S | 4 | 6 | 0 | A alone (own) | **4** | — | 2 | 0 |
| 5 | HS12 | A | M | 8 | 0 | 32 | B alone | — | **8** | 0 | 24 |
| 6 | HS12 | A | L | 12 | 5 | 14 | B alone | — | **12** | 5 | 2 |
| 7 | HP04 | B | S | 2 | 2 | 0 | A alone (own B empty) | **2** | — | 0 | 0 |
| 8 | HP04 | B | M | 10 | 0 | 24 | B alone (own) | — | **10** | 0 | 14 |
| 9 | HP04 | B | L | 6 | 5 | 2 | **neither** → split walk, own B first | **4** | **2** | 1 | 0 |
| | | | | **60** | | | | **14** | **46** | | |

One split line (#9), one cross-ship to a B-store (#7). `14 + 46 = 60` ✓; no balance ever
negative ✓. Note line 9: the split walk starts at HP04's **own** RDC (B, which has 2), then takes
the remaining 4 from A.

**E1.4 — Step 3: RDC-wise picking requirement**

| RDC | S | M | L | To pick | Its stock | Left |
|---|---|---|---|---|---|---|
| RDC-A | 10 | 0 | 4 | **14** | 15 | 1 |
| RDC-B | 0 | 26 | 20 | **46** | 60 | 14 |
| | 10 | 26 | 24 | **60** | 75 | 15 |

**E1.5 — Store dispatch summary**

| Store | From RDC-A | From RDC-B | Total | Documents |
|---|---|---|---|---|
| HS11 | 4 (S4) | 14 (M8, L6) | 18 | 2 |
| HS12 | 4 (S4) | 20 (M8, L12) | 24 | 2 |
| HP04 | 6 (S2, L4) | 12 (M10, L2) | 18 | 2 |

### E2 — The three split policies compared

L-size only: stock **A = 5, B = 20**; allocated HS11 6, HS12 12, HP04 6.

Own RDC: HS11 and HS12 → A, HP04 → B.

| Policy | HS11 (6) | HS12 (12) | HP04 (6) | A picks | B picks | Split lines | Dispatched |
|---|---|---|---|---|---|---|---|
| `SPLIT_ALWAYS` | **A5 + B1** | B12 | B6 | 5 | 19 | 1 | **24 ✓** |
| `SINGLE_PREFERRED` *(default)* | B6 | B12 | **B2 + A4** | 4 | 20 | 1 | **24 ✓** |
| `SINGLE_STRICT` | B6 | B12 | **A5 only** — no RDC covers 6 | 5 | 18 | 0 | **23 ✗ — 1 pc lost** |

Reading the table:

- Both non-strict policies **dispatch the full 24 pieces with exactly one split line**. They differ
  only in *which* line splits — `SPLIT_ALWAYS` splits the first store that runs short (HS11, the
  highest-ranked), `SINGLE_PREFERRED` defers the split to the last line that cannot be covered
  whole (HP04, the lowest-ranked). The per-RDC totals shift by the same 1 piece.
- `SINGLE_STRICT` produces no splits, but HP04's line cannot be covered whole by either RDC, so it
  is **reduced from 6 to 5**. Allocation and dispatch now disagree unless the line is written back
  (§B6.3) — and one piece of served demand is lost.

### E3 — SHIP + HOLD on one ledger (D2)

TBL option, size M, stock **A = 3, B = 9** (clubbed 12). Allocation: HS11 (own A) ship 5 + hold 2;
HP04 (own B) ship 4 + hold 1.

Policy `SINGLE_PREFERRED`; SHIP is tagged before HOLD on each line; both draw the same ledger.

| # | Store | Own | Draw | Qty | A bal | B bal | Covered by | From A | From B | A after | B after |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | HS11 | A | SHIP | 5 | 3 | 9 | B alone (A has 3 < 5) | — | **5** | 3 | 4 |
| 2 | HS11 | A | HOLD | 2 | 3 | 4 | A alone (own) | **2** | — | 1 | 4 |
| 3 | HP04 | B | SHIP | 4 | 1 | 4 | B alone (own) | — | **4** | 1 | 0 |
| 4 | HP04 | B | HOLD | 1 | 1 | 0 | A alone (own B empty) | **1** | — | 0 | 0 |
| | | | | **12** | | | | **3** | **9** | ✓ | ✓ |

Every piece is tagged, neither warehouse is over-drawn, and the 12 allocated pieces exactly
consume the 12 available. Hold tracking records HS11 → 2 pcs held at **A**, HP04 → 1 pc held at
**B's** shortfall source, **A**. Next run those pieces are already netted out of A's pool and each
store's `RL_HOLD_QTY` releases correctly.

Line 4 is the cross-RDC hold case D2 permits: HP04 is a B-store, but B has nothing left, so its
hold is physically reserved at A. The alternative — *hold at own RDC or not at all* — would give
HP04 no hold and leave its new listing under-covered into the next run.

**Two failure modes this prevents:**

| Wrong approach | What breaks |
|---|---|
| Hold tagged to the store's own RDC regardless of stock | HS11's 2 pcs tagged to A, which has 0 left → **phantom hold**; next run deducts stock that was never reserved and HS11 waits forever |
| SHIP and HOLD tagged in two independent passes | Both see B = 9 and together book 12 from B → **double-booked stock** |

### E4 — Mixed store master (D3, Option A)

**Store master** (deliberately messy)

| Store | `ST_MASTER.RDC` | Tier used |
|---|---|---|
| HS11 | `RDC-A` | 1 — own |
| HS12 | *(blank)* | 2 — global `"A,B"` |
| HP04 | `RDC-B` | 1 — own |
| HP09 | `ALL` | 2 — global `"A,B"` |

**Stock, L-size:** A = 15, B = 20 → clubbed 35. Allocation from the clubbed pool — **the tag is
not read**, so every store is served:

| Store | Allocated | Today's result |
|---|---|---|
| HS11 | 10 | 10 |
| HS12 | **12** | **0** — blank tag matches no pool key |
| HP04 | 4 | 4 |
| HP09 | **3** | **0** — `'ALL'` is not a real RDC code |
| | **29** | 14 |

**Split pass** (`SINGLE_PREFERRED`, `ALC_RDC_PRIORITY = "A,B"`)

| # | Store | Tier / preference | Need | A bal | B bal | Covered by | From A | From B | A after | B after |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | HS11 | 1 — own `A` | 10 | 15 | 20 | A | **10** | — | 5 | 20 |
| 2 | HS12 | 2 — `A,B` | 12 | 5 | 20 | B | — | **12** | 5 | 8 |
| 3 | HP04 | 1 — own `B` | 4 | 5 | 8 | B | — | **4** | 5 | 4 |
| 4 | HP09 | 2 — `A,B` | 3 | 5 | 4 | **both** | **3** | — | 2 | 4 |
| | | | **29** | | | | **13** | **16** | | |

**Where the tier actually matters.** Only line 4 — both warehouses could serve it:

| If HP09's preference came from | Ships from | Store receives |
|---|---|---|
| tier 1, a real tag `RDC-B` | B | 3 |
| tier 2, `ALC_RDC_PRIORITY = "A,B"` | A | 3 |
| tier 2, `ALC_RDC_PRIORITY = "B,A"` | B | 3 |
| tier 3, largest-available | A (5 > 4) | 3 |

Lines 1-3 were decided by **availability**, not preference. **An untagged store never loses
stock — it only loses the ability to state a preference, and only on lines where both warehouses
could have served it.**

## B11. Invariants

| ID | Invariant | Enforcement |
|---|---|---|
| I-1 | `Σ ARS_ALLOC_RDC_SPLIT.SHIP_QTY = Σ ARS_ALLOC_WORKING.SHIP_QTY` (per session) | assertion in the pass + test V4 |
| I-2 | `Σ ARS_ALLOC_RDC_SPLIT.HOLD_QTY = Σ ARS_ALLOC_WORKING.HOLD_QTY` | same |
| I-3 | Per `(SRC_RDC, option, size)`: `Σ (SHIP_QTY + HOLD_QTY) ≤ that RDC's MSA FNL_Q` | ledger can never go negative; test V3 |
| I-4 | Every allocation line with `SHIP_QTY > 0` has ≥ 1 split row | test |
| I-5 | `Own` / `Cross` output is byte-identical to the pre-change build | regression diff V5 — **release gate** |
| I-6 | The pass is deterministic: same inputs → same `SRC_RDC` assignment | fixed ordering; test re-run |
| I-7 | No line is sourced from more than `ALC_RDC_MAX_SPLIT` RDCs | assertion in the pass; test V9 |
| I-8 | Every reduced line carries `RDC_SINGLE_SHORT` or `RDC_SPLIT_CAPPED`, and is counted in the run log | test |

Exception to I-1: `SINGLE_STRICT` and a binding `ALC_RDC_MAX_SPLIT` reduce
`ARS_ALLOC_WORKING.SHIP_QTY` **before** the comparison, so the invariant still holds post-pass.
Under the default configuration with two RDCs neither can trigger, so I-1 holds on the raw
allocation.

## B12. Edge cases

| Case | Handling |
|---|---|
| Store tag names an RDC with no stock for the option | Tier 1 finds nothing; walk proceeds to the next RDC. No special code |
| Option exists at only one RDC | Every line sources there; no splits possible |
| `SHIP_QTY = 0`, `HOLD_QTY > 0` | Hold is tagged normally; no ship row emitted |
| Line reduced to 0 under `SINGLE_STRICT` | Row stays with `SHIP_QTY = 0` and remark `RDC_SINGLE_SHORT`; treated as not-allocated by Part 8.5 |
| Part 8.55 releases a hold after tagging | Hold released on the working / parked / split rows alike; the pieces stay unallocated in the RDC that held them |
| Three or more RDCs in future | The pass is written for **N** RDCs — the preference order is a list that simply grows, and no two-RDC assumption exists anywhere in the design. Two settings become material at N ≥ 3: `ALC_RDC_PRIORITY` must name every RDC in order (§B6.4), and `ALC_RDC_MAX_SPLIT` starts to bind (§B6.3.1). Both exist from day one so no redesign is needed when an RDC is added |
| A new RDC is created and not added to `ALC_RDC_PRIORITY` | Appended at the end in alphabetical order — a new warehouse can never be silently excluded from sourcing |
| `ALC_RDC_FALLBACK_ORDER` lists only some RDCs for an own RDC | Missing RDCs appended in `ALC_RDC_PRIORITY` order, then alphabetically. The map is a preference, never a filter |
| `ALC_RDC_FALLBACK_ORDER` has no entry for a store's own RDC | That store resolves its order from `ALC_RDC_PRIORITY` instead (`PREF_TIER = '1'`); other stores are unaffected |
| `ALC_RDC_FALLBACK_ORDER` names a decommissioned RDC | Ignored — it simply holds no stock and the walk moves on |
| `ALC_RDC_FALLBACK_ORDER` set while stores are untagged | No effect on those stores — the map is keyed on the own RDC, so untagged stores resolve at tier 2 |
| Rounding | The pass moves whole allocated pieces only — it never re-derives a quantity, so pack-size rounding done in the engine is preserved exactly |

## B13. Backward compatibility — protecting `Own` and `Cross`

Every change is gated on `rdc_mode == 'all'`:

| # | Change | Gate | `Own` / `Cross` behaviour |
|---|---|---|---|
| FS-RDC-01 | Clubbed `MSA_FNL_Q` / `VAR_*` | mode check | keeps `L.RDC = M.MSA_RDC` join |
| FS-RDC-02 | Hold read-back ignoring RDC | mode check | keeps per-RDC lookup |
| FS-RDC-03 | RDC dropped from pool key + join | mode check | keeps 6-key pool |
| FS-RDC-04 | Split pass (Part 8.37) | mode check | **does not execute** |
| FS-RDC-06 | Split-policy radios | rendered only under `All RDCs` | not shown |

**Release gate (I-5).** Re-run a completed `Own` session on the new build and diff
`ARS_ALLOC_WORKING` row-for-row against its parked snapshot. Any difference is a defect, not a
feature. This must pass before the change is deployed.

## B14. Impact map

| File | Site | Change |
|---|---|---|
| `listing.py` | Part 3.54 (`:1541`) | FS-RDC-02 — hold read-back ignoring RDC |
| `listing.py` | Part 3.55 (`:1630-1654`, `:1662-1700`) | FS-RDC-01 — clubbed `MSA_FNL_Q` / `VAR_COUNT` / `VAR_FNL_COUNT` |
| `listing.py` | **new Part 8.37** between `:3418` and `:3467` | FS-RDC-04 — split pass |
| `listing.py` | `:5113-5220` | FS-RDC-07 — report by `SRC_RDC` under `All RDCs` |
| `listing.py` | `GenerateRequest` (`:81`) | `rdc_split_policy` field + validator |
| `rule_engine_new.py` | `:746`, `:873-874` | FS-RDC-03 — pool build without RDC |
| `rule_engine_new.py` | `:3321-3366` | clubbed residual semantics |
| `rule_engine_per_opt.py` | `:63` | FS-RDC-03 — `POOL_KEYS` |
| `rule_engine_pandas.py` | orchestration | pass the mode through |
| `parked_history.py` | — | **no change** (column reconcile is automatic) |
| `ListingPage.jsx` | RDC SCOPE block | FS-RDC-06 — radios + untagged banner |
| Migration | new | `ARS_ALLOC_RDC_SPLIT` + `SRC_RDC` / `SRC_SPLIT_CNT` columns + 3 business rules |
| `frontend/public/docs/manual/listing.md` | FSD + Recorded rules | update after implementation |

**Explicitly unchanged:** OPT_TYPE classification, `ELIG_FLAG` gates, `PRI_CT%` / `ALLOC_FLAG`,
MBQ / MJ_REQ / secondary-grid caps, `I_ROD` rounds and per-size ceiling, pack-size rounding, store
ranking and manual priority, COMPLETE dispatch + overshoot, hold-release retry, Part 8.5
`OPT_STATUS`, Part 8.55 hold release.

## B15. Verification plan

| # | Check | Method | Gate |
|---|---|---|---|
| V1 | Quantify defect D-1 | Count `ARS_LISTING` rows in the last `All RDCs` session whose `MSA_FNL_Q` equals the *other* RDC's figure | evidence |
| V2 | Fill-rate improvement | Re-run a live session in both modes; compare Σ `SHIP_QTY`, unmet demand, residual | business sign-off |
| V3 | No RDC over-drawn (I-3) | `Σ SRC_QTY` per `(SRC_RDC, option, size)` vs MSA `FNL_Q` | **blocking** |
| V4 | Conservation (I-1, I-2) | `Σ SRC_QTY = Σ SHIP_QTY`, `Σ SRC_HOLD = Σ HOLD_QTY` | **blocking** |
| V5 | `Own` / `Cross` unchanged (I-5) | Row-for-row diff of a completed session vs its parked snapshot | **blocking** |
| V6 | Untagged fallback | Same session with tags blanked → identical quantities, tier-2 sourcing | **blocking** |
| V7 | Policy equivalence | `SPLIT_ALWAYS` vs `SINGLE_PREFERRED` → identical per-RDC totals | regression |
| V8 | Determinism (I-6) | Run the pass twice on the same session → identical `SRC_RDC` | regression |
| V9 | Split cap (I-7, I-8) | Synthetic N=4 fixture where a line needs 3 sources → capped at 2, remainder reduced and stamped, counted in the log | **blocking** |
| V10 | Cap cannot bind at N=2 | Run the current 2-RDC data with `ALC_RDC_MAX_SPLIT = 2` → zero reduced lines | regression |
| V11 | Per-RDC fallback map | N=4 fixture: full map honoured (`PREF_TIER='1F'`); partial map appends the missing RDCs; absent own-RDC key falls to global (`'1'`); unset map reproduces V2 output exactly | **blocking** |

Unit tests alongside `backend/tests/test_alloc_round_stamp_per_opt.py`, covering: single source,
split line, cross-ship, exact-fit, `SINGLE_STRICT` reduction, `MAX_SPLIT` cap reduction,
`MAX_SPLIT = 1` ≡ `SINGLE_STRICT` equivalence, SHIP+HOLD shared ledger, all three fallback tiers,
priority-list ordering with an unlisted RDC appended, and an N=4 RDC case.

## B16. Rollout

| Phase | Content | Exit criteria |
|---|---|---|
| **1** | Build behind the existing `All RDCs` button; `Own` / `Cross` untouched | V3, V4, V5, V6 pass; unit tests green |
| **2** | Parallel run on live data — same session in `Own` and `All RDCs` | V2 delta presented and accepted by management |
| **3** | `All RDCs` adopted as the routine mode; `Own` retained for single-RDC operations | picking confirms it can execute split + cross-ship lines |
| **4** *(optional)* | Enable `ALC_MIN_CROSS_SHIP_QTY` freight guardrail | freight data shows uneconomic small movements |

## B17. Glossary

| Term | Meaning |
|---|---|
| **RDC** | Regional distribution centre (warehouse). Currently two |
| **Clubbed / central pool** | The sum of all in-scope RDCs' stock for one option-size, used as one allocatable quantity |
| **Split pass** | The new post-allocation step that assigns a source RDC to every shipped and held piece |
| **`SRC_RDC`** | The warehouse that will physically ship a line |
| **Store's own RDC** | `Master_ALC_INPUT_ST_MASTER.RDC` — after this change, a sourcing *preference* under `All RDCs` |
| **Cross-ship** | A line sourced from an RDC other than the store's own |
| **Split line** | One store-size sourced from two or more RDCs |
| **OPT** | `MAJ_CAT + GEN_ART_NUMBER + CLR` |
| **Line** | One allocation row: store × option × `VAR_ART` × size |

## B18. Open items

| # | Item | Owner | Needed by |
|---|---|---|---|
| O1 | Warehouse confirmation that picking can execute a two-source line for one store-size — if not, the default moves from `SINGLE_PREFERRED` to `SINGLE_STRICT` | Supply chain | before Phase 1 build |
| O2 | Value for `ALC_RDC_PRIORITY` — an ordered list naming every RDC (e.g. `"A,B"`) | Business owner | before Phase 1 test |
| O2a | Confirm `ALC_RDC_MAX_SPLIT = 2` as the default. Cannot bind while only two RDCs exist, so this is a decision for the day a third is added | Supply chain | before a 3rd RDC goes live |
| O2b | Populate `ALC_RDC_FALLBACK_ORDER` — one ordered list per RDC reflecting freight distance / transit days. Leave unset today; it can express nothing useful with two warehouses | Supply chain | before a 3rd RDC goes live |
| O3 | Whether an inter-warehouse transfer document is required before a cross-ship, or the store STO issues directly from the sourcing RDC | Supply chain / SAP | before Phase 3 |
| O4 | Target date to clean the untagged stores in the store master (D3 = Option A treats this as a gap to close) | Business owner | before Phase 3 |
