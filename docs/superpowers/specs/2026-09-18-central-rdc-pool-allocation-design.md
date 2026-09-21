> **SUPERSEDED (2026-09-19)** — replaced by
> `2026-09-19-central-rdc-pool-allocation-brd-fsd.md`, which carries the final BRD + FSD with the
> approved decisions (All RDCs = club + split, three split policies with `SINGLE_PREFERRED`
> default, one SHIP+HOLD ledger, tag stays authoritative). Kept for history only.

# Central RDC Pool Allocation — Design (for approval)

**Date:** 2026-09-18
**Status:** DRAFT — awaiting management approval
**Modules:** Listing & Allocation (`listing.py`, `rule_engine_new.py`, `rule_engine_per_opt.py`, `rule_engine_pandas.py`)
**Owner:** `ars_flow` / `rule_ars`
**Related dossier:** `frontend/public/docs/manual/listing.md`

---

## 1. Business requirement

> "In listing and alloc we currently have 2 RDCs. Management wants to allocate stock considering **all RDC stock in central** and allocate. After allocation, **tag RDC-wise** available stock basis, to generate the requirement from each RDC."

Two distinct steps that must not be conflated:

| Step | What it decides | Grain |
|---|---|---|
| **(a) Central allocation** | *How much* each store gets — against the sum of all RDC stock | store × option × size |
| **(b) RDC tagging** | *Which warehouse physically ships* each allocated line, within that warehouse's real stock | allocation line × source RDC |

Step (b) then produces the **RDC-wise picking requirement** that feeds the picklist / STO.

This document covers the **RDC-mapped** case (every store carries a valid RDC tag in
`Master_ALC_INPUT_ST_MASTER`), with the untagged/partially-tagged fallback specified in §7.

---

## 2. Current behaviour — RDC is a hard partition

RDC is baked into the pipeline at three levels.

### 2.1 Listing build

A store's RDC comes from `Master_ALC_INPUT_ST_MASTER`; `rdc_mode` decides which MSA rows it may see
(`listing.py:1212-1347`):

| `rdc_mode` | Stores | MSA options visible | Store↔MSA RDC match |
|---|---|---|---|
| `all` | every store | every RDC's options | **none** (`ON 1=1`, `listing.py:1339`) |
| `own` | stores of selected RDC(s) | that RDC's options only | `M.RDC = S.RDC` (`listing.py:1323`) |
| `cross` | stores of `cross_to` | options of `cross_from` | whole-run re-point |

`ARS_LISTING.RDC` is therefore a **store attribute**, and `MSA_FNL_Q` on the option row is meant to be
that one RDC's availability.

### 2.2 Rule engine

The stock pool is keyed **with RDC inside the key** (`rule_engine_per_opt.py:63`):

```
POOL_KEYS = ["RDC", "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "VAR_ART", "SZ"]
```

and is built by joining the listing row's RDC to the MSA size rows of the same RDC
(`rule_engine_new.py:873-874`). Two RDCs = two disjoint wallets; a store can only spend from its own.
`FNL_Q_REM` and `RDC_FNL_Q_REM_LIVE` (`rule_engine_new.py:3321-3366`) are per-RDC residuals.

### 2.3 Everything downstream

`ARS_NL_TBL_HOLD_TRACKING` carries its own `RDC` column (`listing.py:3668-3720`); `RL_HOLD_QTY`
(Part 3.54) is pool-scoped per RDC; `MASTER_ALC_PEND` deducts at `(RDC, GEN_ART, CLR)`;
the cockpit summary and `by_maj_cat_rdc` reporting group by RDC (`listing.py:5113-5220`).

### 2.4 Known defect in `all` mode (pre-existing)

In `all` mode the MSA quantity sub-query still groups **per RDC**, but the join back to the listing row
**drops the RDC predicate** (`listing.py:1634`, `listing.py:1670` — the `L.RDC = M.MSA_RDC` clause is
added only when `rdc_mode == "own"`). An option present at both RDCs therefore has two candidate
quantities and SQL Server's `UPDATE … FROM` resolves the ambiguity by picking **one arbitrarily** —
it neither sums them nor prefers the store's own RDC.

Consequence: a store's listing row can display the *other* RDC's `MSA_FNL_Q`, classify as RL/TBC/TBL on
stock it can never receive, pass the `NO_STOCK` eligibility gate, and then be skipped at the engine with
`POOL_EMPTY` / `NO_POOL_MSA`. Same ambiguity hits `VAR_COUNT` / `VAR_FNL_COUNT`, which drive the R07
TBL size-coverage gate.

**Verification pending** — read from the SQL, not yet measured on live data. See §9 item V1.

---

## 3. Worked example

### 3.1 Setup

**Store master** — every store permanently tagged to one RDC:

| Store | Tagged RDC |
|---|---|
| HS11 | RDC-A |
| HS12 | RDC-A |
| HP04 | RDC-B |

**Warehouse stock (MSA)** for option `M_TEES_HS / 1110116457 / OFF_WHT`:

| RDC | S | M | L | Total |
|---|---|---|---|---|
| RDC-A | 10 | 0 | 5 | **15** |
| RDC-B | 0 | 40 | 20 | **60** |
| *(A + B merged)* | *10* | *40* | *25* | ***75*** |

**Store requirement** after MBQ / I_ROD math (size level):

| Store | RDC | S | M | L | Need |
|---|---|---|---|---|---|
| HS11 | A | 4 | 8 | 6 | 18 |
| HS12 | A | 4 | 8 | 12 | 24 |
| HP04 | B | 4 | 10 | 6 | 20 |
| | | | | | **62** |

### 3.2 Today — "All RDCs" selected

Two separate pools; a store draws only from its own:

| Pool key | Available |
|---|---|
| (RDC-A, …, S) | 10 |
| (RDC-A, …, M) | **0** |
| (RDC-A, …, L) | 5 |
| (RDC-B, …, S) | **0** |
| (RDC-B, …, M) | 40 |
| (RDC-B, …, L) | 20 |

Allocation in `ST_RANK` order:

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

| | Demand | Shipped | Unmet | Stock left idle |
|---|---|---|---|---|
| HS11 | 18 | 9 | 9 | |
| HS12 | 24 | 4 | 20 | |
| HP04 | 20 | 16 | 4 | |
| **Total** | **62** | **29 (47 %)** | **33** | **46 pcs** (2 at A, 44 at B) |

46 pieces sit unused while 33 pieces of real demand go unserved — solely because demand and stock are in
different buildings.

### 3.3 Proposed — Step 1: one merged pool

RDC drops out of the pool key:

| Pool key | Available |
|---|---|
| (…, S) | 10 |
| (…, M) | 40 |
| (…, L) | 25 |

### 3.4 Proposed — Step 2: allocate from the merged pool

Same `ST_RANK` order, RDC ignored. **No change to any gate, cap or priority rule.**

| # | Store | Size | Need | Pool before | Shipped | Pool after |
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

**29 → 60 shipped on exactly the same stock.**

### 3.5 Proposed — Step 3: tag the source RDC

Rule: **own RDC first, shortfall from the other RDC** (§5.2).

| Store | Own RDC | Size | Qty | From RDC-A | From RDC-B | Note |
|---|---|---|---|---|---|---|
| HS11 | A | S | 4 | **4** | — | own |
| HS11 | A | M | 8 | 0 avail | **8** | cross-ship from B |
| HS11 | A | L | 6 | **5** | **1** | ⚠ **split across 2 RDCs** |
| HS12 | A | S | 4 | **4** | — | own |
| HS12 | A | M | 8 | 0 avail | **8** | cross-ship |
| HS12 | A | L | 12 | 0 left | **12** | cross-ship |
| HP04 | B | S | 2 | **2** | 0 avail | cross-ship from A |
| HP04 | B | M | 10 | — | **10** | own |
| HP04 | B | L | 6 | — | **6** | own |

### 3.6 Proposed — Step 4: RDC-wise picking requirement

Falls out of Step 3 as a `GROUP BY SRC_RDC`:

| RDC | S | M | L | To pick | Its stock | Left |
|---|---|---|---|---|---|---|
| RDC-A | 10 | 0 | 5 | **15** | 15 | 0 |
| RDC-B | 0 | 26 | 19 | **45** | 60 | 15 |
| **Total** | 10 | 26 | 24 | **60** | 75 | 15 |

**Invariant:** no RDC is ever over-drawn. `Σ SRC_RDC qty per (RDC, option, size) ≤ that RDC's MSA stock`.

---

## 4. Design

### 4.1 New RDC scope: `central`

A fourth option on the cockpit's **RDC SCOPE** control, alongside `All RDCs` / `Own` / `Cross`:

| Mode | Pool | Store scope | Sourcing |
|---|---|---|---|
| `all` | per-RDC (unchanged) | all stores | implicit = own RDC |
| `own` | per-RDC (unchanged) | selected RDC(s) | implicit = own RDC |
| `cross` | per-RDC (unchanged) | `cross_to` stores | whole-run re-point |
| **`central`** (new) | **merged across selected RDCs** | all stores (or selected) | **explicit tagging pass** |

`central` accepts an optional RDC list — merging "all RDCs" is the default, but merging a subset
(e.g. A+B while C stays independent) uses the same code path.

**Existing modes are untouched.** `central` is additive; every current run reproduces bit-for-bit.

### 4.2 Pipeline changes, step by step

| Step | Today | Under `central` |
|---|---|---|
| Listing rows | store × option, `RDC` = store's RDC | unchanged — `RDC` stays the **store's** RDC for reporting continuity |
| `MSA_FNL_Q` (Part 3.55) | per-RDC value, arbitrary pick in `all` mode (§2.4) | **`SUM` across the merged RDC set**, joined without an RDC predicate — deterministic |
| `VAR_COUNT` / `VAR_FNL_COUNT` | same ambiguity | `SUM` / distinct-size count across merged RDCs |
| `OPT_TYPE` (Part 3.6) | reads per-RDC `MSA_FNL_Q` | unchanged code — now reads the merged figure |
| `ELIG_FLAG` `NO_STOCK` (Part 6.6) | per-RDC | unchanged code — merged figure |
| Engine pool build | join `L.RDC = V.RDC` | drop the RDC predicate; `GROUP BY` without RDC |
| `POOL_KEYS` | 6 keys incl. RDC | **5 keys** — RDC removed |
| Allocation waterfall | unchanged | **unchanged** — no gate, cap, round or priority rule changes |
| `FNL_Q_REM` / `RDC_FNL_Q_REM_LIVE` | per-RDC residual | central residual (semantics documented, column names kept) |
| **RDC tagging** | *does not exist* | **new pass after the waterfall**, before parking |
| Picking requirement | implicit (= store's RDC) | `GROUP BY SRC_RDC` on the tagged rows |

### 4.3 The tagging pass (new)

Runs once after the allocation waterfall completes and before Part 8.4 parking, so parked / history /
approve inherit the tag with no change to `parked_history.py` (its column-reconcile already copies new
columns automatically).

```
for each allocated row (WERKS, MAJ_CAT, GEN_ART, CLR, VAR_ART, SZ, SHIP_QTY):
    remaining = SHIP_QTY
    order    = preference_order(WERKS)           # §5.2 / §7

    # pass 1 — Hybrid (§5.1): first RDC in preference order that covers the
    #          WHOLE line takes it, so no split is created unnecessarily
    if split_policy == HYBRID:
        for rdc in order:
            if rdc_avail[rdc, option, size] >= remaining:
                emit row SRC_RDC = rdc, SRC_QTY = remaining
                rdc_avail[rdc, option, size] -= remaining
                remaining = 0
                break

    # pass 2 — split across RDCs in preference order (last resort under HYBRID,
    #          the only pass under SPLIT_ALLOWED)
    for rdc in order:
        if remaining == 0: break
        take = min(remaining, rdc_avail[rdc, option, size])
        if take > 0:
            emit row SRC_RDC = rdc, SRC_QTY = take
            rdc_avail[rdc, option, size] -= take
            remaining -= take

    assert remaining == 0        # guaranteed: Σ rdc_avail == central pool drawn
```

Processing order across rows is **`ST_RANK` then the allocation order** — identical to the waterfall — so
the tagging is deterministic and reproducible.

`HOLD_QTY` is tagged by the same pass and the same preference order: a warehouse hold physically
reserves stock in one building, so it must name that building.

---

## 5. Decisions requiring sign-off

These three change the output; the values below are the **proposed defaults**.

### 5.1 May one store-size line split across two RDCs?

| Option | Effect on the example | Trade-off |
|---|---|---|
| Split allowed | HS11's L-size = 5 from A + 1 from B → 2 pick lines | Nothing stranded; splits wherever the first RDC runs short |
| Single source only | HS11 takes all 6 from B and **A's 5 L-pcs are stranded** — the line may also go short | Clean picking, stock stranded — the problem we set out to fix |
| **Hybrid (proposed)** | Prefer a single RDC that can cover the whole line; split only when none can | Fewest split lines, still nothing stranded |

**Proposed: Hybrid.** Minimum split lines, no stranding.

**Effect on the §3.5 example.** §3.5 illustrates plain *own-first, split whenever short*. Under Hybrid the
L-size sourcing shifts: HS11 takes all 6 from B (B alone covers it), HS12 takes 12 from B, and the single
split lands on HP04's L-line (A 5 + B 1) because by then neither RDC can cover 6 alone. **The RDC totals
are identical** — A picks 15, B picks 45, one split line, nothing stranded. Only *which* line carries the
split moves.

### 5.2 Sourcing preference

| Option | Behaviour |
|---|---|
| **Own RDC first, shortfall from other (proposed)** | Minimises inter-warehouse freight; the tag stays meaningful |
| Drain the overstocked RDC first | Rebalances the network, maximises freight |
| Fixed global RDC priority | Simplest; concentrates picking in one warehouse |

**Proposed: own RDC first.**

### 5.3 Cross-ship guardrail

In the example, HP04 (a B store) pulls **2 pcs** from A. A 2-piece inter-warehouse movement may cost more
than it earns.

| Option | Behaviour |
|---|---|
| **No guardrail (proposed for phase 1)** | Every shortfall is cross-shipped |
| Minimum cross-ship qty (e.g. ≥ 6 pcs / ≥ 1 pack) | Small shortfalls simply go unserved |
| MAJ_CAT whitelist | Cross-ship only where margin justifies freight |

**Proposed: no guardrail in phase 1**, with the threshold available as a business rule
(`ALC_MIN_CROSS_SHIP_QTY`, inactive = no minimum) so it can be switched on without a code change.

---

## 6. Data model changes

| Object | Change | Notes |
|---|---|---|
| `ARS_ALLOC_WORKING` | `+ SRC_RDC NVARCHAR(20)`, `+ SRC_QTY FLOAT` | A split line becomes two rows; existing `RDC` keeps meaning **store's RDC** |
| `ARS_ALLOC_PARKED` / `ARS_ALLOC_HISTORY` | inherit both columns automatically | `parked_history.py` column-reconcile — no code change |
| `ARS_NL_TBL_HOLD_TRACKING` | `RDC` now means **sourcing RDC** under `central` | Existing column reused; hold must name the reserving warehouse |
| `ARS_LISTING` / `_WORKING` | no new columns | `RDC` stays the store's RDC |
| `ARS_RUN_PARAMS_AUDIT` | `+ rdc_mode=central`, `+ sourcing_rule`, `+ split_policy` | Run-level reproducibility |
| Business rules | `ALC_RDC_SOURCING_RULE`, `ALC_RDC_SPLIT_POLICY`, `ALC_MIN_CROSS_SHIP_QTY`, `ALC_RDC_PRIORITY` | Inactive ⇒ hardcoded default = proposed values above |

---

## 7. Fallback — stores with no RDC tag

### 7.1 Today

| Situation | Today's behaviour |
|---|---|
| Store master has **no RDC column** | Run blocked — `HTTP 400 "ST_MASTER missing RDC column"` (`listing.py:1140-1145`) |
| Column exists, **values blank** | Pool key `('', …)` matches nothing → every row `POOL_EMPTY`, zero allocation |
| Store tagged `'ALL'` | Same — `'ALL'` is not a real RDC code, nothing ships |

The tag is mandatory in practice, though nothing validates it.

### 7.2 Under `central`

Steps 1, 2 and 4 **do not use the store's RDC at all**. A missing tag costs **nothing in quantity** — every
store receives an identical allocation. It only removes the "own RDC first" tiebreaker in Step 3, which
then falls back to a stock-only rule:

| Priority | Preference source | Used when |
|---|---|---|
| 1 | Store's own RDC (`Master_ALC_INPUT_ST_MASTER`) | tag present and valid |
| 2 | Store's `RDC_PRIORITY` (new optional column, e.g. `"B,A"`) | tag blank, per-store override maintained |
| 3 | Global `ALC_RDC_PRIORITY` business rule (e.g. `"A,B"`) | nothing maintained at store level |
| 4 | Largest-available-first | nothing configured at all |

On the §3 example, tiers 3 and 4 both reproduce the §3.5 result — 60 shipped, 15/45 pick split, one split
line, nothing stranded. The design therefore degrades gracefully across fully tagged, partially tagged and
untagged store masters, and **no run ever fails for a missing tag again**.

### 7.3 Pre-run validation gate

The cockpit shows, before Generate:

> `312 of 451 stores have no RDC tag → sourcing will use global priority A,B`

Informational under `central` (never blocking); still blocking for `own` / `cross`, which genuinely
require the tag.

---

## 8. Impact map

| File | Site | Change |
|---|---|---|
| `listing.py` | `GenerateRequest.rdc_mode` (~`:81`) | accept `central` + optional RDC list |
| `listing.py` | Part 2 MSA pairing (`:1321-1347`) | `central` behaves like `all` (`ON 1=1`) — no change needed |
| `listing.py` | Part 3.55 `MSA_FNL_Q` (`:1630-1654`) | `SUM` across merged RDCs; **also fixes §2.4 for `all`** |
| `listing.py` | Part 3.55 `VAR_COUNT` / `VAR_FNL_COUNT` (`:1662-1700`) | same |
| `listing.py` | Part 3.54 `RL_HOLD_QTY` | hold lookup merged across RDCs under `central` |
| `listing.py` | new part after the waterfall, before Part 8.4 | **tagging pass** (§4.3) |
| `listing.py` | summary / `by_maj_cat_rdc` (`:5113-5220`) | report by `SRC_RDC` under `central` |
| `rule_engine_new.py` | pool build (`:746`, `:873-874`) | drop the `L.RDC = V.RDC` predicate; group without RDC |
| `rule_engine_new.py` | `RDC_FNL_Q_REM_LIVE` (`:3321-3366`) | central residual semantics |
| `rule_engine_per_opt.py` | `POOL_KEYS` (`:63`) | RDC-less key under `central` |
| `rule_engine_pandas.py` | orchestration | pass the mode through |
| `ListingPage.jsx` | RDC SCOPE control | 4th button + untagged-store warning |
| `frontend/public/docs/manual/listing.md` | FSD + Recorded rules | update after implementation |

**Explicitly unchanged:** OPT_TYPE classification logic, MBQ / sec-cap / MJ_REQ caps, `I_ROD` rounds,
pack-size rounding, `ST_RANK` priority, dispatch modes, hold-release retry. Central pooling changes
*which pool a row draws from*, never *how much it is entitled to*.

---

## 9. Verification plan

| # | Check | Method |
|---|---|---|
| V1 | Measure the §2.4 defect | Count `ARS_LISTING` rows in the last `all`-mode session whose `MSA_FNL_Q` matches the *other* RDC's figure |
| V2 | Central ≥ per-RDC on fill rate | Re-run a live session in both modes; compare Σ `SHIP_QTY` and unmet demand |
| V3 | No RDC over-drawn | `Σ SRC_QTY` per `(SRC_RDC, option, size)` ≤ that RDC's MSA `FNL_Q` — assert in the tagging pass and in tests |
| V4 | Conservation | `Σ SRC_QTY == Σ SHIP_QTY` for every allocation row |
| V5 | Existing modes unchanged | `all` / `own` / `cross` reproduce a prior session bit-for-bit |
| V6 | Untagged fallback | Same session with store RDC blanked → identical quantities, tags from global priority |

Unit tests alongside `backend/tests/test_alloc_round_stamp_per_opt.py`, covering: split line, cross-ship,
exact-fit, insufficient-central-pool, and the untagged fallback chain.

---

## 10. Rollout

1. **Phase 1** — `central` mode behind the cockpit selector, default **off**. Existing runs unaffected.
2. **Phase 2** — run both modes in parallel on live data (V2), present the fill-rate delta to management.
3. **Phase 3** — make `central` the default once the picking side confirms it can execute split /
   cross-ship lines; `own` remains available for single-RDC operations.

---

## 11. Open questions

1. §5.1 split policy, §5.2 sourcing rule, §5.3 cross-ship guardrail — confirm the proposed defaults.
2. Is the untagged store master a **permanent** state (sourcing always decided by stock) or a
   **temporary** data gap (own-RDC-first stays the long-term rule)?
3. Can the picking/dispatch side execute **two source RDCs for one store-size line**, or must the
   picklist be single-source per line?
4. Does an inter-warehouse movement need its own document (A → B transfer) before the store dispatch, or
   does the store STO issue directly from the sourcing RDC?
