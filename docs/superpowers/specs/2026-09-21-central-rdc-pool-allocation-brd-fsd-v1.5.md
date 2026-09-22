# Central RDC Pool Allocation — BRD & FSD

**Document version:** 1.5
**Date:** 2026-09-21
**Module:** Listing & Allocation
**Systems touched:** `listing.py`, `rule_engine_new.py`, `rule_engine_per_opt.py`, `rule_engine_pandas.py`, `pend_alc_service.py`, `msa_service.py`, `alloc_pool.py`, `parked_history.py`, `ListingPage.jsx`
**Owners:** `ars_flow` (listing / pipeline) · `rule_ars` (rule engine)
**Canonical dossier:** `frontend/public/docs/manual/listing.md` — to be updated after implementation
**Supersedes:** v1.4 (2026-09-21), v1.2, and `2026-09-18-central-rdc-pool-allocation-design.md`

---

## Change log — v1.4 → v1.5

Nine corrections, all found by verifying v1.4's claims against branch `ars_v3`. Three of them
protect `Own`; six fix `All RDCs`.

| # | Change | Why | Sections |
|---|---|---|---|
| **M1** 🔴 | **FS-RDC-02 deleted entirely.** Part 3.54 already ignores RDC in *every* mode | v1.4 said *"Own keeps the per-RDC lookup"* — there is no per-RDC lookup. Implementing it literally would **add** a filter to Own. **This was the single real threat to Own in v1.4** | B4, B2, B13, B14, Part C Step 2 |
| **M2** 🔴 | B3 must gate **three** SQL fragments, not one | Dropping only `_rdc_join` leaves the `GROUP BY RDC` fan-out — D-1 survives unfixed | B3 |
| **M3** 🔴 | `SRC_RDC` / `SRC_SPLIT_CNT` go in the `SELECT … INTO` list, **not** an ALTER | `ARS_ALLOC_WORKING` is dropped and recreated every run; an ALTER is wiped by the next Generate | B7, Part C Step 1 |
| **M4** 🔴 | **New BR-RDC-12 — a HOLD is never split** | `PK_ARS_NL_TBL_HOLD_TRACKING = (WERKS, VAR_ART, SZ, ALLOC_TYPE)`. Two source RDCs for one hold = **identical primary keys**. v1.4 §B12 claimed the grain supports it; it does not | B6.3, B7.3, B12, E3.1 |
| **M5** 🔴 | Pend idempotency guard **and** `GROUP BY` must include `SRC_RDC` | Without it a split line's second source row is **silently dropped** — the guard keys on the store's RDC, which is identical for both | B7.3 |
| **M6** 🔴 | Approve reads **`ARS_ALLOC_RDC_SPLIT`**, not `ARS_ALLOC_HISTORY`; and the split table gets a lifecycle | `ARS_ALLOC_HISTORY.SRC_RDC` is `'MULTI'` on a split line — `'MULTI'` is not a warehouse. v1.4 also gave the split table no park / reject / revert / purge path | B7.2, B7.3, B14 |
| **M7** 🟠 | `msa_service` hold loaders must be rewritten; `alloc_pool` is already correctly shaped | Two hold-RDC paths exist today and **disagree**. v1.4's one-line instruction fixes the one already right and misses the broken one | B7.3, B14 |
| **M8** 🟠 | **New BR-RDC-13 — `ALC_RDC_CENTRAL_POOL` kill switch, default INACTIVE** | `ListingPage.jsx:756` already defaults `rdcMode` to `'all'`. Without a switch, deploying changes every user's default behaviour with no abort path short of a rollback deploy | B8.6, Part C |
| **M9** 🟠 | The mode is resolved **once** at run start and passed down | `rule_flag()` caches on a 30 s TTL. Independent reads across a long run can produce a **half-clubbed, half-split** run | B1.4 |
| — | V5 widened from 1 table to **4** | Steps 4-5 touch pend and hold; diffing only `ARS_ALLOC_WORKING` would miss them | B15 |

---

# PART A — BRD (Business Requirements)

## A1. Background

ARS currently operates two RDCs — **`DW01`** and **`DH24`**. Every store is tagged to exactly one
RDC in the store master, and the allocation engine keeps each RDC's stock in a **separate pool**.
A store can only ever be served from the warehouse it is tagged to.

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
  *Live check 2026-09-21: 0 of 521 stores are untagged — insurance, not an active fire.*

> **Measured 2026-09-21 (Step 0).** `All RDCs` has **never been run in production** — 0 of 293
> sessions. D-1 is therefore a *modelled* defect, correct but not yet observed. The exposure it
> would cause is real and large: of 3,382,403 listing rows in the last live run, **403,732 (11.9 %)
> have zero stock at the store's own RDC while the other RDC holds some**, and **11,303 of 12,647
> stocked options (89 %) exist at exactly one warehouse**. Full evidence:
> `2026-09-21-central-rdc-pool-step0-findings.md`.

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
- Propagation of `SRC_RDC` into the reservation ledgers (§B7.3)
- An **RDC-wise picking requirement** output
- Cockpit controls for the split policy, and visibility of untagged stores
- A master kill switch (`ALC_RDC_CENTRAL_POOL`, BR-RDC-13)
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
| **D1** | How a shipment line may be sourced when one RDC cannot cover it | All three policies offered as **radio buttons** in the run cockpit; **Policy 2 — single-source preferred** is the **default** |
| **D2** | How warehouse holds (`HOLD_QTY`) are sourced | SHIP and HOLD are drawn from **one shared balance ledger**, own-RDC-first, in the same line order. Cross-RDC holds are allowed. **Amended in v1.5: a hold may be cross-sourced but never *split* — see BR-RDC-12** |
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
| **BR-RDC-09** | One **shipment** line may be sourced from at most `ALC_RDC_MAX_SPLIT` RDCs (default 2). |
| **BR-RDC-10** | No silent truncation. Any line whose quantity the split pass reduces is stamped in `ALLOC_REMARKS`, rolled up to the option, and counted in the run log and cockpit. |
| **BR-RDC-11** | `Own` and `All RDCs` are separate paths that never mix. An Own / Cross run uses `RDC` and writes no `SRC_RDC` and no split rows; an All RDCs run uses `SRC_RDC`. No run ever uses both (§B1.3). |
| **BR-RDC-12** 🆕 | **A HOLD is never split across RDCs.** A hold is a physical reservation at one warehouse; assembling one from two is meaningless and the hold-tracking primary key cannot express it. A hold that no single RDC can cover is sourced from the largest available RDC and **reduced**, stamped `RDC_HOLD_SHORT`. |
| **BR-RDC-13** 🆕 | **Central pooling is switch-controlled.** The feature is active only when `rdc_mode = 'all'` **and** business rule `ALC_RDC_CENTRAL_POOL` is active. The switch defaults to **inactive**, so deploying the code changes nothing until it is turned on, and any problem is reverted by turning it off — no deploy, no migration, no data repair. |

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
| R1 | A change intended for `All RDCs` leaks into `Own` / `Cross` | Production allocation corrupted | Every change gated on BR-RDC-13; mode separation enforced by I-11 / V14; release gated on a 4-table regression diff (V5) |
| R2 | An RDC is tagged beyond its physical stock | Picklist cannot be executed | Single running balance ledger; hard assertion BR-RDC-04 |
| R3 | SHIP and HOLD double-book the same pieces | Phantom reservations | One shared ledger (D2 / BR-RDC-06) |
| R4 | Split lines create two pick documents for one store-size | Warehouse workload | Policy 2 default minimises splits; Policy 3 available if picking cannot execute them |
| R5 | Cross-shipping generates tiny uneconomic movements | Freight cost | `ALC_MIN_CROSS_SHIP_QTY` reserved (inactive in phase 1) |
| R6 | Split rows break downstream row-grain assumptions | Approve / reporting errors | Splits stored in a **child table**, leaving `ARS_ALLOC_WORKING` one row per store-size (§B7) |
| R7 | Clubbed pool concentrates stock on high-rank stores, starving the tail | Distribution fairness | Ranking and caps unchanged; monitor by-store distribution in the parallel-run phase |
| R8 | Pending / hold rows booked against the store's RDC instead of the sourcing RDC | **Severe and compounding** — MSA deducts from the wrong warehouse, so the sourcing RDC keeps counting committed pieces as free (double-allocation) while the store's RDC shows a phantom shortage | `SRC_RDC` column on the ledgers, read as `ISNULL(SRC_RDC, RDC)`; invariants I-9 / I-10, blocking check V12 (§B7.3) |
| **R9** 🆕 | A split line's second reservation row is silently dropped by the existing idempotency guard | Under-reserved stock; picking short | Guard and `GROUP BY` widened to include `SRC_RDC` (M5, §B7.3) |
| **R10** 🆕 | Two runs parked but not yet approved both draw the clubbed pool | Over-allocation | Under `Own` the pools are naturally separate; clubbing removes that isolation. `allow_multi_parked` is **refused** while `ALC_RDC_CENTRAL_POOL` is active (§B8.6) |

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

Specific confirmations requested from the warehouse team:
**O1** — whether picking can execute a two-source line for a single store-size (if not, the
default moves to Policy 3); and **O6** — acceptance of BR-RDC-12 (a short hold is reduced).

---

# PART B — FSD (Functional Specification)

## B1. Solution overview

### B1.1 Mode matrix

| RDC SCOPE button | Stock pool | Store selection | Allocation rules | Post-alloc split | Status |
|---|---|---|---|---|---|
| **All RDCs** *(switch ON)* | **clubbed across RDCs** | all stores | **unchanged** | **new — RDC split pass** | **CHANGED** |
| **All RDCs** *(switch OFF)* | per-RDC | all stores | unchanged | none | **as today** |
| **Own** | per-RDC | stores of selected RDC(s) | unchanged | n/a (= own RDC) | **UNCHANGED** |
| **Cross** | per-RDC | stores of `cross_to` | unchanged | n/a (= `cross_from`) | **UNCHANGED** |

No new mode is introduced. `All RDCs` changes meaning from *"run both RDCs side by side"* to
*"club, allocate, split"* — **but only once `ALC_RDC_CENTRAL_POOL` is active** (BR-RDC-13).

### B1.2 What the store→RDC tag controls, per mode

| Tag's job | `Own` / `Cross` | `All RDCs` |
|---|---|---|
| Selects which stores enter the run | ✅ yes (unchanged) | ❌ no — all stores enter |
| Selects which stock the store can see | ✅ yes (unchanged) | ❌ no — clubbed pool |
| Selects which warehouse ships it | ✅ implied | ✅ **preference only** (3-tier fallback) |
| Blank / invalid tag | **blocks the run** (as today) | allocation unaffected; sourcing falls back |

### B1.3 Mode separation contract — `RDC` vs `SRC_RDC`

`Own` and `All RDCs` are two separate paths that never mix. A run uses one warehouse column,
never both: `Own` / `Cross` → **`RDC`**. `All RDCs` → **`SRC_RDC`**.

| Object | `Own` / `Cross` run | `All RDCs` run |
|---|---|---|
| `ARS_LISTING.RDC` / `ARS_LISTING_WORKING.RDC` | store's RDC — drives pool and sourcing (as today) | store's RDC — used as a sourcing *preference* only |
| `ARS_ALLOC_WORKING.RDC` | store's RDC (as today) | store's RDC (unchanged) |
| `ARS_ALLOC_WORKING.SRC_RDC` | **NULL — never written** | always written (`'MULTI'` when split) |
| `ARS_ALLOC_WORKING.SRC_SPLIT_CNT` | NULL | 1 or more |
| `ARS_ALLOC_RDC_SPLIT` | **no rows produced** | one row per line per source RDC |
| `ARS_PEND_ALC.SRC_RDC` | NULL | written at Approve |
| `ARS_NL_TBL_HOLD_TRACKING.SRC_RDC` | NULL | written at Approve |
| MSA / pool deduction `ISNULL(SRC_RDC, RDC)` | resolves to `RDC` | resolves to `SRC_RDC` |
| Picking requirement | `GROUP BY RDC` (as today) | `GROUP BY SRC_RDC` |
| Split pass (Part 8.37) | does not execute | executes |

Because an Own run leaves `SRC_RDC` NULL everywhere, `ISNULL(SRC_RDC, RDC)` resolves to `RDC` and
every downstream consumer behaves exactly as it does today — **no branch, no migration, no dual
meaning**. The same expression serves both modes correctly.

This is enforced, not merely intended: invariant **I-11** and blocking check **V14** fail the
build if an Own or Cross run writes a single `SRC_RDC` value or a single split row.

### B1.4 🆕 The mode is resolved ONCE per run (M9)

`business_rules.rule_flag()` reads a module-level cache with a **30-second TTL**
(`business_rules.py:280`). A long Generate spans many cache refreshes. If Part 3.55 and
Part 8.37 each call `rule_flag('ALC_RDC_CENTRAL_POOL')` independently, an admin toggling the
switch mid-run produces a **half-clubbed, half-split run** — the worst possible state.

**Rule:** resolve once, immediately after the session `RUNNING` row is inserted
(`listing.py:~697`), and pass it down as an explicit argument through
`rule_engine_pandas` → `rule_engine_new` → `rule_engine_per_opt`:

```python
_CENTRAL_POOL = (req.rdc_mode == "all"
                 and rule_flag("ALC_RDC_CENTRAL_POOL", default=False))
```

Stamp `central_pool_active` into `ARS_RUN_PARAMS_AUDIT` alongside `rdc_mode`
(`listing.py:962`) so every run records which engine it used. **No site below calls
`rule_flag` again.**

## B2. End-to-end processing sequence

Existing Part numbers in `/listing/generate`; the only new step is **Part 8.37**.

| Part | Step | Change under `All RDCs` |
|---|---|---|
| 1-2 | Build `ARS_LISTING` (grid rows + MSA-only options) | none — `RDC` column keeps the **store's** RDC |
| 3.5 / 3.5a | `ACS_D`, `ALC_D`, `I_ROD` | none |
| **3.54** | `RL_HOLD_QTY` from hold tracking | **none — already RDC-blind in every mode (M1)** |
| 3.55 | `MSA_FNL_Q`, `VAR_COUNT`, `VAR_FNL_COUNT` | **FS-RDC-01** — summed across RDCs |
| 3.6 | OPT_TYPE classification | none (same code, clubbed input) |
| 4 / 4c | Grid columns, demand math | none |
| 6 / 6.6 | Store ranking, `ELIG_FLAG` | none |
| 7 | Working table, growth, grid-coverage flags | none |
| 8 | **Rule engine allocation** | **FS-RDC-03** — clubbed pool key |
| 8.35 / 8.36 | `ALLOC_TYPE`, run dates stamp | none |
| **8.37** | **RDC split pass** | **FS-RDC-04 — NEW** |
| 8.4 | Park alloc + listing snapshots | inherits `SRC_RDC` automatically |
| 8.5 / 8.55 | `OPT_STATUS`, hold release | hold release cascades to split rows (§B6.6) |
| 8.6 | NL/TBL hold-tracking schema + backfill | backfill guarded to `SRC_RDC IS NULL` (§B7.3) |

## B3. FS-RDC-01 — Clubbed MSA quantities

**Site:** `listing.py` Part 3.55 (`:1630-1654` for `MSA_FNL_Q`, `:1662-1700` for
`VAR_COUNT` / `VAR_FNL_COUNT`).

**Today.** The MSA sub-query groups per RDC, but the join back to the listing row adds the
`L.RDC = M.MSA_RDC` predicate **only when `rdc_mode = 'own'`**. In `all` mode an option present at
both RDCs therefore has two candidate rows, and SQL Server's `UPDATE … FROM` resolves the
ambiguity by picking **one arbitrarily** — neither summed nor own-RDC-preferred. This is D-1.

### 🆕 M2 — three fragments must be gated together, not one

v1.4 said *"predicate dropped"*. That is **not sufficient**. The code builds three fragments and
only the join is currently mode-aware:

```python
_rdc_select = f", LTRIM(RTRIM(CAST([{msa_rdc_col}] AS NVARCHAR(50)))) AS MSA_RDC"  # always on
_rdc_group  = f", LTRIM(RTRIM(CAST([{msa_rdc_col}] AS NVARCHAR(50))))"             # always on ← real cause
_rdc_join   = "AND L.[RDC] = M.[MSA_RDC]" if _has_msa_rdc and req.rdc_mode == "own" else ""
```

Dropping only `_rdc_join` leaves the subquery returning **one row per RDC**, so the
`UPDATE … FROM` still picks one arbitrarily. **D-1 survives.** All three must move together:

```python
_club = _CENTRAL_POOL                       # §B1.4 — resolved once at run start

_rdc_select = "" if _club else f", LTRIM(RTRIM(CAST([{msa_rdc_col}] AS NVARCHAR(50)))) AS MSA_RDC"
_rdc_group  = "" if _club else f", LTRIM(RTRIM(CAST([{msa_rdc_col}] AS NVARCHAR(50))))"
_rdc_join   = "" if _club else ("AND L.[RDC] = M.[MSA_RDC]"
                                if _has_msa_rdc and req.rdc_mode == "own" else "")
```

The false branch reproduces **today's exact strings**, including the inner `== "own"` test — so
`Cross` keeps its current (no-join) behaviour untouched. The same shape applies to
`vrdc_select` / `vrdc_group` / `vrdc_join` at `:1668-1670`.

**Required derivation under `All RDCs`:**

| Column | New derivation |
|---|---|
| `MSA_FNL_Q` | `SUM(FNL_Q)` across all in-scope RDCs for the option, filtered to the run's `ALLOC_TYPE` |
| `VAR_COUNT` | distinct sizes present across all in-scope RDCs |
| `VAR_FNL_COUNT` | distinct sizes with **clubbed** `FNL_Q > 0` |

`VAR_FNL_COUNT` must be counted on the clubbed figure, not summed per RDC — a size held at both
RDCs is still one size, and this value drives the R07 TBL size-coverage gate.

**Effect:** OPT_TYPE classification and the `NO_STOCK` eligibility gate read a deterministic,
complete number. No change to their logic.

## B4. 🆕 Part 3.54 hold read-back — NO CHANGE REQUIRED (M1)

**v1.4's FS-RDC-02 is withdrawn.**

v1.4 asked for a fix here and stated *"Under `Own` / `Cross`, unchanged — keeps per-RDC lookup."*
**There is no per-RDC lookup.** `listing.py:1586` already reads:

```sql
GROUP BY [WERKS], [MAJ_CAT], [GEN_ART_NUMBER], ISNULL([CLR],'')
```

with no RDC predicate, in **every** mode. The desired behaviour — *sum a store-option's holds
across warehouses* — is what the code already does.

**Implementing FS-RDC-02 literally would ADD an RDC filter to the Own branch** — a behaviour
change in the one mode that must not change. This was the only real threat to `Own` in v1.4.

**Action:** no code change. FS-RDC-02 is removed from §B2, §B13, §B14 and Part C Step 2.
A regression test asserts that Part 3.54's SQL is mode-independent.

## B5. FS-RDC-03 — Clubbed pool key

**Sites:** `rule_engine_per_opt.py:63` (`POOL_KEYS`), `rule_engine_new.py:746` and `:873-874`
(pool build join), `rule_engine_new.py:3321-3366` (`RDC_FNL_Q_REM_LIVE` snapshot).

| | Today | Under `All RDCs` |
|---|---|---|
| Pool key | `(RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ)` | `(MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ)` |
| Pool build join | `L.RDC = V.RDC` | predicate dropped; `GROUP BY` without RDC |
| `FNL_Q_REM` | live residual of that RDC's pool | live residual of the **clubbed** pool |
| `RDC_FNL_Q_REM_LIVE` | per-RDC end-of-run residual | clubbed end-of-run residual |

`POOL_KEYS` is **passed in from the orchestrator**, not hard-coded, so `Own` keeps the 6-key list.

Dropping `L.RDC = V.RDC` is also what fixes **D-2**: today a store with a blank or `'ALL'` tag
matches no `V.RDC` and drops out of the allocation entirely, with no error.

**Residual reporting note.** `RDC_FNL_Q_REM_LIVE` becomes a clubbed number. It remains valid for
`NO_POOL_MSA` diagnostics but **must not** be used for the residual-by-warehouse report — derive
that from the split ledger instead (§B9).

**Nothing else in the engine changes.** The waterfall, gates, caps, rounds, pack-size rounding,
store ranking, dispatch mode and hold-release retry all operate on the clubbed pool exactly as
they operate on a per-RDC pool today.

### B5.1 Parallelism — no new contention

Allocation runs `parallel_workers` threads, one per MAJ_CAT. `POOL_KEYS` **still contains
`MAJ_CAT`** after clubbing, so two workers can never touch the same pool row. Clubbing removes
only the RDC dimension. **No locking change is required.**

The split pass is the opposite — its ledger is global. **Part 8.37 runs once, single-threaded,
after every MAJ_CAT worker has finished.**

## B6. FS-RDC-04 — The RDC split pass (NEW, Part 8.37)

### B6.1 Placement

Runs after the run-date stamp (`listing.py:3423`) and **before** parking (`:3425`), so
`ARS_ALLOC_PARKED` → `ARS_ALLOC_HISTORY` inherit the tagging via the existing column reconcile.

Skipped entirely unless `_CENTRAL_POOL` (§B1.4).

### B6.2 Inputs, ordering and ledger

| Input | Source |
|---|---|
| Allocation lines (`SHIP_QTY`, `HOLD_QTY`) | `ARS_ALLOC_WORKING` |
| Per-RDC availability at option-size grain | `ARS_MSA_VAR_ART`, filtered to the run's `ALLOC_TYPE` |
| Store's own RDC | `Master_ALC_INPUT_ST_MASTER` (via `ARS_LISTING_WORKING.RDC`) |
| Split policy, RDC priority | run parameters / business rules (§B7.4) |

**Processing order:** `ST_RANK`, then the allocation order within a store — identical to the
waterfall, so the result is deterministic and reproducible.

**One ledger (D2 / BR-RDC-06).** A single in-memory balance `rdc_avail[RDC, VAR_ART, SZ]`,
initialised from MSA and decremented by **both** SHIP and HOLD draws as they are tagged. SHIP and
HOLD for a line are tagged consecutively, **SHIP first**. Two independent passes would both see
the full balance and double-book it.

**🆕 Idempotency (M6).** The pass opens with:
```sql
DELETE FROM ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID = :sid;
```
`ARS_ALLOC_WORKING` is dropped and rebuilt every Generate but the split table is durable, so a
re-run would otherwise hit the PK or leave stale rows. Covered by check V16.

### B6.3 Sourcing policies (D1 — cockpit radio buttons)

`preference_order(store)` = own RDC first, then remaining RDCs in configured priority order (§B6.4).

| Radio option | Value | Behaviour |
|---|---|---|
| Split whenever short | `SPLIT_ALWAYS` | Walk the preference order, take what each RDC has until the line is filled |
| **Single source preferred** *(default)* | `SINGLE_PREFERRED` | The first RDC in preference order that can cover the **whole** line takes it. Only if none can, fall back to the split walk |
| Strict single source | `SINGLE_STRICT` | Never split. If no RDC can cover the whole line, ship the largest single-RDC quantity and **reduce the line** |

**🆕 These policies apply to SHIP lines only. Every HOLD line runs `SINGLE_STRICT` with a cap of
1, regardless of the selected policy (BR-RDC-12, §B6.3.4).**

#### B6.3.1 Maximum sources per line (`ALC_RDC_MAX_SPLIT`, default 2)

With two RDCs a split line means at most two pick documents. With three or more, an unconstrained
split walk could assemble one store-size from **every** warehouse.

| Value | Meaning |
|---|---|
| `1` | equivalent to `SINGLE_STRICT` — never more than one source |
| **`2`** *(default)* | at most two warehouses per line. **Cannot bind while only two RDCs exist** |
| `N` | at most N warehouses per line |

When the cap binds before the line is filled, the remainder is reduced and stamped
`RDC_SPLIT_CAPPED(alloc=A,shipped=B)`. To maximise fill within the cap, the capped walk orders
candidates **own RDC first, then by availability descending**.

#### B6.3.2 Algorithm

```
MAX_SPLIT = ALC_RDC_MAX_SPLIT                     # default 2

for line in (ST_RANK, alloc order):               # SHIP first, then HOLD
    remaining = qty
    order     = preference_order(store)           # §B6.4
    sources   = 0

    if draw == HOLD:                              # BR-RDC-12 — holds never split
        policy_here, cap_here = SINGLE_STRICT, 1
    else:
        policy_here, cap_here = policy, MAX_SPLIT

    # pass 1 — whole-line cover
    if policy_here in (SINGLE_PREFERRED, SINGLE_STRICT):
        for rdc in order:
            if rdc_avail[rdc, var, sz] >= remaining:
                emit(rdc, remaining); rdc_avail[rdc] -= remaining
                remaining = 0; sources = 1; break

    # pass 2 — strict: largest single source, reduce the rest
    if remaining > 0 and policy_here == SINGLE_STRICT:
        best = argmax(rdc_avail[rdc, var, sz] for rdc in order)
        take = min(remaining, rdc_avail[best, var, sz])
        if take > 0:
            emit(best, take); rdc_avail[best] -= take
            remaining -= take; sources = 1
        reduce_line(remaining,
                    'RDC_HOLD_SHORT' if draw == HOLD else 'RDC_SINGLE_SHORT')
        remaining = 0

    # pass 3 — capped split walk (SHIP only)
    if remaining > 0:
        candidates = own_first(order, then rdc_avail desc)[:cap_here]
        for rdc in candidates:
            take = min(remaining, rdc_avail[rdc, var, sz])
            if take > 0:
                emit(rdc, take); rdc_avail[rdc] -= take
                remaining -= take; sources += 1
            if remaining == 0: break
        if remaining > 0:
            reduce_line(remaining, 'RDC_SPLIT_CAPPED')
            remaining = 0

    assert remaining == 0                         # BR-RDC-03
    assert sources <= cap_here                    # I-7, I-12
    assert all(v >= 0 for v in rdc_avail.values())  # BR-RDC-04
```

#### B6.3.3 Line reduction

Three situations can reduce an allocated quantity — `SINGLE_STRICT` on a SHIP line, a binding
`ALC_RDC_MAX_SPLIT` cap, and a HOLD no single RDC can cover (BR-RDC-12). All must leave the data
internally consistent:

1. `SHIP_QTY` / `HOLD_QTY` on `ARS_ALLOC_WORKING` reduced to the tagged quantity.
2. `ALLOC_REMARKS` gains `RDC_SINGLE_SHORT(…)`, `RDC_SPLIT_CAPPED(…)` or `RDC_HOLD_SHORT(…)`.
3. The option-grain `ALLOC_QTY` / `HOLD_QTY` rollup on `ARS_LISTING_WORKING` is refreshed, so
   Part 8.5 `OPT_STATUS` judges the real shipped quantity.
4. The un-tagged remainder stays unallocated and appears in the residual stock report.
5. The run log and the cockpit report the count and quantity of reduced lines — **never silent**.

**Under the default configuration (2 RDCs, `SINGLE_PREFERRED`, cap 2) no SHIP quantity can ever
change — the pass is pure tagging for shipments.** Only holds can reduce, and only when neither
warehouse can cover one whole.

#### B6.3.4 🆕 BR-RDC-12 — why a HOLD is never split (M4)

```sql
CONSTRAINT PK_ARS_NL_TBL_HOLD_TRACKING
    PRIMARY KEY CLUSTERED ([WERKS], [VAR_ART], [SZ], [ALLOC_TYPE])
```

`RDC` is **not** in the key, and adding `SRC_RDC` as a plain column does not change that. A hold
split across two RDCs would require two rows with **identical primary keys** — the insert fails,
or one row silently overwrites the other and pieces are reserved in the system but in no
warehouse. v1.4 §B12 claimed *"Both tables' existing grain supports this"*; for hold tracking that
is false.

**Two ways out were considered:**

| Option | Cost | Verdict |
|---|---|---|
| Widen the PK to include `SRC_RDC` | Migration across 2,750,316 hold rows + 6,558,761 snapshot rows; every consumer's grain assumption re-checked | rejected for phase 1 |
| **Never split a hold** | One conditional in the pass; a hold that no single RDC covers is reduced | **chosen** |

A hold is a *physical reservation at one warehouse*. Assembling one from two is meaningless in
the warehouse, and a hold is advisory — reducing it is safe, unlike reducing a shipment.
**Cross-RDC holds remain fully permitted** (D2 is unchanged); only *splitting* one is forbidden.

### B6.4 Sourcing preference resolution (D3 — Option A, 3 tiers)

Applied per store, stopping at the first tier that yields a preference:

| Tier | Preference order | Applies when | `PREF_TIER` |
|---|---|---|---|
| **1** | Store's own RDC **first**, then the remaining RDCs ordered per §B6.4.1 | tag present and is a real RDC code | `1` / `1F` |
| **2** | Global list `ALC_RDC_PRIORITY` alone | tag blank, `'ALL'`, or not a known RDC | `2` |
| **3** | Largest-available-first, evaluated per line | `ALC_RDC_PRIORITY` inactive or unset | `3` |

A tag naming an RDC with no stock needs no special handling: tier 1 finds nothing available there
and the walk proceeds to the next RDC.

#### B6.4.1 Ordering the RDCs after "own"

| # | Setting | Grain | Keyed on | Rows to maintain |
|---|---|---|---|---|
| **a** | `ALC_RDC_FALLBACK_ORDER` *(optional, unset by default)* | **per RDC** | the store's **own** RDC | one per RDC |
| **b** | `ALC_RDC_PRIORITY` | global | nothing — one list for all | one |

**(a) Per-RDC fallback order.** One ordered list per warehouse, answering *"if a store's own
warehouse is X and X is short, which warehouse do we try next?"* — captures geography at **N rows
for N warehouses**. JSON keyed by own RDC:
`{"A":["B","C","D"], "B":["A","D","C"], "C":["D","A","B"], "D":["C","B","A"]}`.

Rules for a partial or stale map — **it is a preference, never a filter**:

- An RDC **absent** from a list is appended at the end, in `ALC_RDC_PRIORITY` order, then
  alphabetically. No warehouse is ever excluded by omission.
- An own-RDC key **missing** from the map falls through to (b) for that store only.
- A name that is **not a live RDC** is ignored.
- The map applies **only to tagged stores** — untagged stores resolve at tier 2.

Lines whose order came from the map are audited as `PREF_TIER = '1F'`.

**(b) Global priority list.** `ALC_RDC_PRIORITY` is an **ordered list naming every RDC in
operation** — `"DW01,DH24"` today, `"DW01,DH24,C,D"` if warehouses are added. An RDC missing from
the list is appended alphabetically, so a newly created RDC can never be silently excluded.

**Recommended configuration today (2 RDCs):** leave `ALC_RDC_FALLBACK_ORDER` unset and set
`ALC_RDC_PRIORITY = "DW01,DH24"`. Populate the map when the third RDC goes live.

#### B6.4.2 What was deliberately not built

A **per-store** `RDC_PRIORITY` column is **dropped**: 521 rows of new master data to curate, with
no demonstrated case that two stores of the same RDC need different fallback orders.

#### B6.4.3 Extension point — `ARS_STORE_RDC_PRIORITY` (specified, NOT built)

If the per-RDC map ever proves too coarse, the next grain is **HUB**, not store. Evidence from the
live master: **521 stores, 110 hubs, and no hub spans two RDCs** — 21 real hubs cover ~432 stores
(10-30 each) and the remaining 89 hubs hold one store apiece. Stores in a hub share a truck, so
they physically cannot have different sourcing preferences.

```sql
CREATE TABLE ARS_STORE_RDC_PRIORITY (
    SCOPE      NVARCHAR(10) NOT NULL,   -- 'HUB' or 'STORE'
    SCOPE_CD   NVARCHAR(20) NOT NULL,   -- HUB code, or ST_CD for an exception
    SEQ        INT          NOT NULL,   -- 1,2,3… order AFTER the store's own RDC
    RDC        NVARCHAR(20) NOT NULL,
    IS_ACTIVE  BIT          NOT NULL DEFAULT 1,
    UPDATED_BY NVARCHAR(50) NULL,
    UPDATED_AT DATETIME     NULL,
    CONSTRAINT PK_ARS_STORE_RDC_PRIORITY PRIMARY KEY (SCOPE, SCOPE_CD, SEQ),
    CONSTRAINT UQ_ARS_STORE_RDC_PRIORITY UNIQUE (SCOPE, SCOPE_CD, RDC)
);
```

Resolution chain with the extension present — each tier optional, empty by default:

| Order | Source | `PREF_TIER` | Rows at 4 RDCs |
|---|---|---|---|
| 1 | Store's own RDC, always first | — | from the tag |
| 2 | `ARS_STORE_RDC_PRIORITY` STORE row | `1S` | exceptions only |
| 3 | `ARS_STORE_RDC_PRIORITY` HUB row | `1H` | ~63 |
| 4 | `ALC_RDC_FALLBACK_ORDER` per-RDC map | `1F` | 4 |
| 5 | `ALC_RDC_PRIORITY` global list | `1` / `2` | 1 |
| 6 | Largest-available-first | `3` | 0 |

~63 hub rows versus 521 for a per-store column. **Not built now, deliberately** — with two RDCs
every row would be a no-op. The chain is designed so this tier slots in ahead of the per-RDC map
without touching the pass, which only ever calls `preference_order(store)`.

### B6.5 Outputs

| Output | Content |
|---|---|
| `ARS_ALLOC_RDC_SPLIT` (new) | one row per **alloc line × source RDC** — `SHIP_QTY`, `HOLD_QTY` |
| `ARS_ALLOC_WORKING.SRC_RDC` | the single source RDC, or `'MULTI'` when the line was split |
| `ARS_ALLOC_WORKING.SRC_SPLIT_CNT` | number of source RDCs for the line (1 or more) |
| RDC-wise picking requirement | `GROUP BY SRC_RDC` over `ARS_ALLOC_RDC_SPLIT` |

### B6.6 🆕 Hold release (Part 8.55) must cascade

Part 8.55 releases warehouse holds on not-covered options and runs **after** parking (Part 8.4).
When it releases a hold it must also zero `HOLD_QTY` on the matching `ARS_ALLOC_RDC_SPLIT` rows —
otherwise the picking requirement still reserves stock the run has given back.

## B7. FS-RDC-05 — Data model

**Design point (risk R6):** a split line must **not** create a second row in
`ARS_ALLOC_WORKING`. That table's one-row-per-`(WERKS, option, VAR_ART, SZ)` grain is relied on by
parking, Approve, alloc review and reporting. The split therefore lives in a **child table**.

### B7.1 New table — `ARS_ALLOC_RDC_SPLIT`

```sql
IF OBJECT_ID('dbo.ARS_ALLOC_RDC_SPLIT','U') IS NULL
CREATE TABLE dbo.ARS_ALLOC_RDC_SPLIT (
    SESSION_ID      NVARCHAR(50)   NOT NULL,
    WERKS           NVARCHAR(50)   NOT NULL,   -- DESTINATION store
    MAJ_CAT         NVARCHAR(200)  NULL,
    GEN_ART_NUMBER  BIGINT         NULL,
    CLR             NVARCHAR(200)  NULL,
    VAR_ART         BIGINT         NOT NULL,
    SZ              NVARCHAR(50)   NOT NULL,
    SRC_RDC         NVARCHAR(20)   NOT NULL,   -- SOURCING warehouse
    SHIP_QTY        FLOAT          NOT NULL DEFAULT 0,
    HOLD_QTY        FLOAT          NOT NULL DEFAULT 0,
    ALLOC_TYPE      NVARCHAR(10)   NOT NULL DEFAULT '',   -- FRESH / GRT
    STORE_RDC       NVARCHAR(20)   NULL,       -- store's own tag, '' if untagged (audit)
    PREF_TIER       NVARCHAR(2)    NULL,       -- 1 | 1F | 2 | 3
    IS_CROSS        BIT            NOT NULL DEFAULT 0,    -- SRC_RDC <> STORE_RDC
    CREATED_AT      DATETIME       NOT NULL DEFAULT GETDATE(),
    CONSTRAINT PK_ARS_ALLOC_RDC_SPLIT PRIMARY KEY CLUSTERED
        (SESSION_ID, WERKS, VAR_ART, SZ, SRC_RDC, ALLOC_TYPE)
);

CREATE INDEX IX_ARS_ALLOC_RDC_SPLIT_PICK
    ON dbo.ARS_ALLOC_RDC_SPLIT (SESSION_ID, SRC_RDC) INCLUDE (SHIP_QTY, HOLD_QTY);
CREATE INDEX IX_ARS_ALLOC_RDC_SPLIT_PEND
    ON dbo.ARS_ALLOC_RDC_SPLIT (SESSION_ID, WERKS, VAR_ART, SZ);
```

`ALLOC_TYPE` sits in the PK, mirroring the Fresh/GRT widening already applied to
`ARS_NL_TBL_HOLD_TRACKING`. `IX_..._PEND` exists specifically to make the Approve-time join (M6)
cheap. Rows are written for **every** line, including single-source ones, so the picking
requirement is a single clean query.

### B7.2 🆕 Split-table lifecycle (M6) — v1.4 defined none

| Event | Action on `ARS_ALLOC_RDC_SPLIT` | Where |
|---|---|---|
| Generate | `DELETE WHERE SESSION_ID`, then rows written by Part 8.37 | `listing.py` |
| Park (8.4) | **nothing** — rows are already `SESSION_ID`-keyed and durable | — |
| Approve | **read** to write per-source reservation rows (§B7.3); rows themselves stay as the picking record | `parked_history.py` |
| Reject | `DELETE WHERE SESSION_ID = :sid` | `parked_history.reject_parked` |
| Revert approval | `DELETE WHERE SESSION_ID = :sid` | `parked_history.revert_approved_to_parked` |
| Purge | TTL sweep on `CREATED_AT`, same window as history | `parked_history.purge_old_history` |
| Post-run sweep | add to `_AFFECTED_TABLES` | `parked_history.py:98` |

Without this, rejecting a run leaves orphan picking rows that look live.

### B7.3 Reservation ledgers — `SRC_RDC`, never a redefinition

**The problem.** In every ARS-written reservation table, `RDC` means the **store's** RDC — it is
backfilled from the store master (`listing.py:3704-3716`). That is only safe while a store can be
served solely by its own warehouse. Under central pooling the pieces physically sit at the
sourcing warehouse, and MSA deducts these ledgers at `(RDC, GEN_ART_NUMBER, CLR)` when computing
`FNL_Q = max(STK − PEND − HOLD, 0)`.

**Worked failure (R8)** — store HS11 (own `DW01`) allocated 10 pcs sourced from `DH24`, 4 held:

| Booked as | Next MSA run | Consequence |
|---|---|---|
| `RDC = DW01` (store's) | deducts 4 from `DW01` | `DW01`'s free stock understated by 4 — real stock becomes invisible |
| | `DH24` still counts those 4 as free | `DH24` overstated by 4 — the same pieces are allocated a second time |

**The fix — add a column, never redefine one.**

| Table | Existing rows | Change |
|---|---|---|
| `ARS_PEND_ALC` | 17,948,674 | `+ SRC_RDC NVARCHAR(20) NULL` |
| `ARS_NL_TBL_HOLD_TRACKING` | 2,750,316 | `+ SRC_RDC NVARCHAR(20) NULL` |
| `ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT` | 6,558,761 | `+ SRC_RDC NVARCHAR(20) NULL` |
| MSA / pool deduction | — | read `ISNULL(SRC_RDC, RDC)` |

`ISNULL(SRC_RDC, RDC)` is backward compatible by construction: every legacy row has
`SRC_RDC = NULL` and falls back to `RDC`, which for an own-RDC run *is* the sourcing RDC. No
backfill and no migration of 27M rows is required, and a nullable column with no default is a
**metadata-only ALTER** in SQL Server — instant even at 17.8M rows.

#### Four sites that must be guarded or changed

**(i) Hold backfill guard** — `listing.py:3704-3716` stamps the store's RDC onto rows where
`RDC IS NULL`. It must be restricted so it can never overwrite a centrally-sourced row:

```sql
WHERE (T.[RDC] IS NULL OR T.[RDC] = '')
  AND T.[SRC_RDC] IS NULL              -- ← added
```

**(ii) 🆕 Approve must read the SPLIT table, not history (M6).**
v1.4 said *"carry `SRC_RDC` from `ARS_ALLOC_HISTORY`"*. But §B6.5 defines
`ARS_ALLOC_WORKING.SRC_RDC = 'MULTI'` on a split line — and `'MULTI'` is not a warehouse. History
cannot tell you the line was `DH24` 2 + `DW01` 4.

Approve must aggregate `ARS_ALLOC_RDC_SPLIT` by `(WERKS, VAR_ART, SRC_RDC)` and write **one
reservation row per source RDC**.

**(iii) 🆕 The pend idempotency guard and GROUP BY must include `SRC_RDC` (M5).**
`pend_alc_service.write_pend_alc` (`:2304`) currently guards on:

```sql
WHERE NOT EXISTS (SELECT 1 FROM ARS_PEND_ALC P
    WHERE P.SESSION_ID = :sid AND P.RDC = src.RDC
      AND ISNULL(P.ST_CD,'') = ISNULL(src.ST_CD,'')
      AND P.ARTICLE_NUMBER = src.VAR_ART AND P.ALLOC_MODE = src.ALLOC_MODE)
```

A split line has **two source RDCs but one store RDC**, so both candidate rows share the guard
key and **the second is silently dropped**. The `GROUP BY` has the same defect.

Both must add `SRC_RDC`. The primary key is on `ID` (identity), so no constraint blocks the second
row once the guard is widened. The existing `rdc_expr` stays as-is — `RDC` keeps meaning the
store's RDC — and `SRC_RDC` is populated as a new, separate column:

```sql
ISNULL(SPL.SRC_RDC, NULL) AS SRC_RDC       -- NULL on Own/Cross: no split rows exist
```

**(iv) 🆕 The MSA hold loaders ignore the hold table's RDC entirely (M7).**
Two hold-RDC resolution paths exist today and **they disagree**:

| Code | How it resolves a hold's warehouse | Status |
|---|---|---|
| `alloc_pool.py:192` | `COALESCE(NULLIF(H.[RDC],''), SM.[RDC])` — **reads the hold row** | ✅ correctly shaped; add `SRC_RDC` in front |
| `msa_service.py:94` `_load_open_holds` | `SELECT S.[RDC] … JOIN store_master S ON S.ST_CD = H.WERKS` — **ignores `H.RDC`** | ❌ must be rewritten |
| `msa_service.py:211` `_load_typed_hold` | same as above | ❌ must be rewritten |

v1.4's single instruction *"MSA / alloc_pool.py … read `ISNULL(SRC_RDC, RDC)`"* fixes the one
already right and misses the broken one. **Adding `SRC_RDC` to the hold table changes nothing for
MSA until both loaders are changed to:**

```sql
COALESCE(NULLIF(H.[SRC_RDC],''), NULLIF(H.[RDC],''), S.[{rdc_col}]) AS RDC
```

Legacy rows (both columns NULL) fall through to the store master — today's exact behaviour.

#### Unaffected, and why

| Object | Impact | Reason |
|---|---|---|
| Grid tables (`ARS_GRID_*`) | none | Keyed by `WERKS` only; RDC is joined in from the store master at listing build (`listing.py:1307`) |
| `ARS_MSA_TOTAL` / `_GEN_ART` / `_VAR_ART` build | none | MSA stays per-RDC, which is correct — clubbing happens at read time, never at build time |
| `Master_ALC_PEND` (385,014 rows) | none | Uploaded externally; its RDC is the warehouse physically holding the pending — already sourcing semantics |
| `ARS_FACONS_PEND` (2,170 rows) | **none — O5 CLOSED 2026-09-21** | Verified in code: it is a **self-contained FA/CONS pipeline** (`facons_pend_service.py:1-12` — *"its OWN tables… the core ARS Pending Allocation tables / logic are NOT touched"*). `approve_session` (`:107`) seeds it from `ARS_FACONS_ALLOC_ART` / `_REF`, taking `rdc` from the FA/CONS allocation header — never from `ARS_ALLOC_HISTORY` or the store master. `msa_service.py` and `alloc_pool.py` contain **zero** references to it, so it does not participate in the MSA `FNL_Q` deduction. **No `SRC_RDC` needed.** |
| `bdc.py` (DO / picking file) | none | Reads `ARS_PEND_ALC.RDC`; correct automatically once (ii)/(iii) land — **no code change** |

### B7.4 Amended tables and rules

| Table | Change |
|---|---|
| `ARS_ALLOC_WORKING` | `SRC_RDC NVARCHAR(20)`, `SRC_SPLIT_CNT INT` — **added to the `SELECT … INTO` list, NOT by ALTER (M3)** |
| `ARS_ALLOC_PARKED` / `ARS_ALLOC_HISTORY` | inherit both via the existing column reconcile — no code change |
| `ARS_RUN_PARAMS_AUDIT` | new rows: `central_pool_active`, `rdc_split_policy`, `rdc_priority`, `rdc_max_split`, `untagged_store_count` |
| `ARS_LISTING` / `ARS_LISTING_WORKING` | no new columns; `RDC` continues to mean the store's RDC |

> **🆕 M3 — why not an ALTER.** `ARS_ALLOC_WORKING` is **dropped and recreated by
> `SELECT … INTO`** on every run (`rule_engine_new.py:829-867`). An `ALTER TABLE` would be wiped
> by the next Generate and `SRC_RDC` would silently disappear. The columns must be declared in
> that SELECT list, exactly like the existing `ALLOC_WAVE`:
> ```sql
> CAST(NULL AS NVARCHAR(20)) AS SRC_RDC,
> CAST(NULL AS INT)          AS SRC_SPLIT_CNT,
> ```
> `ARS_PEND_ALC` and the two hold tables **are** persistent, so those are genuine ALTERs.

**Business rules registry**

| Rule | Default | `is_active` | Purpose |
|---|---|---|---|
| **`ALC_RDC_CENTRAL_POOL`** 🆕 | — | **0** | **BR-RDC-13 master kill switch** |
| `ALC_RDC_SPLIT_POLICY` | `SINGLE_PREFERRED` | 1 | D1 policy when not set per-run |
| `ALC_RDC_PRIORITY` | *(unset → tier 3)* | 1 | ordered list of every RDC, e.g. `"DW01,DH24"` |
| `ALC_RDC_FALLBACK_ORDER` | *(unset → use `ALC_RDC_PRIORITY`)* | 0 | optional per-RDC map (§B6.4.1a) |
| `ALC_RDC_MAX_SPLIT` | `2` | 1 | max source RDCs per **ship** line; `1` ≡ `SINGLE_STRICT` |
| `ALC_MIN_CROSS_SHIP_QTY` | *(inactive)* | 0 | reserved for phase 4 freight guardrail |

## B8. FS-RDC-06 — UI (`ListingPage.jsx`)

Pattern: **progressive disclosure.** The three RDC SCOPE buttons are unchanged; a second panel
appears beside them **only** when `All RDCs` is selected.

> **Correction to v1.4's mock.** v1.4 §B8.1 showed an `RDC [A ×][C ×]` multi-select for `Own`.
> **That control does not exist.** RDCs are auto-derived from the selected stores
> (`ListingPage.jsx:1275`, `autoRdcs`) and rendered as **read-only pills**. `Cross` uses toggle
> buttons. The mocks below are the real screens.

### B8.1 `Own` selected — today's screen plus one advisory line

```
┌ RDC Scope ──────────────────────────────┐
│ [ All RDCs ] [ Own ✓ ] [ Cross ]        │   ← existing 3 buttons, untouched
│ Detected:  (DW01)  (DH24)               │   ← existing read-only pills
│ ⚠ 0 of 521 selected stores have no RDC  │   ← NEW, text only
│   tag — excluded from this run          │
└─────────────────────────────────────────┘
```

`autoRdcs` uses `.filter(Boolean)`, so untagged stores vanish today with **no message** — the
front-end half of D-2. The banner never blocks and never alters the payload.

### B8.2 `All RDCs` selected — sourcing panel appears

```
┌ RDC Scope ──────────────────────┐  ┌ RDC Sourcing ────────────────────────────┐
│ [ All RDCs ✓] [ Own ] [ Cross ] │  │ Split policy                             │
│                                 │  │  ( ) Split whenever short                │
│ Pool:   DW01 + DH24 → one pool  │  │  (•) Single source preferred    default  │
│ Stores: 521   (0 untagged)      │  │  ( ) Strict single source           ⚠    │
│                                 │  │      may reduce an allocated line        │
│ ⓘ Central pool: OFF             │  │ ─────────────────────────────────────────│
│   ALC_RDC_CENTRAL_POOL          │  │ Priority   [DW01][DH24]     ⇅ drag       │
│   → behaves as today            │  │ Max split per line   [ 2 ▾ ]             │
│                                 │  │ Holds: always single-source  BR-RDC-12   │
└─────────────────────────────────┘  └──────────────────────────────────────────┘
```

New React state: `rdcSplitPolicy` (default `'SINGLE_PREFERRED'`), `rdcMaxSplit` (default `2`).
Payload assembly at `:1489` gains a **third branch only** — the Own and Cross branches are not
touched:

```js
if (rdcMode === 'own')        { payload.rdc_values = autoRdcs }
else if (rdcMode === 'cross') { payload.cross_from = crossFrom; payload.cross_to = autoRdcs }
else if (rdcMode === 'all')   { payload.rdc_split_policy = rdcSplitPolicy
                                payload.rdc_max_split    = rdcMaxSplit }
```

### B8.3 Control specification

| Control | Type | Behaviour |
|---|---|---|
| RDC SCOPE | existing segmented buttons | unchanged; `All RDCs` help text becomes *"club all RDC stock, allocate, then split RDC-wise"* |
| Live summary | read-only text | pool composition + store count + untagged count, so `All` is visibly different from `Own` **before** Generate |
| **Central-pool badge** 🆕 | read-only | reads `ALC_RDC_CENTRAL_POOL`; when OFF says *"behaves as today"* so nobody is surprised |
| Split policy | radio group, 3 | default `Single source preferred`; `Strict` shows the reduction caption |
| Priority | drag-to-reorder chips | seeded from `ALC_RDC_PRIORITY`. Read-only with a note when a per-RDC map is active |
| Max split per line | select `1 / 2 / 3 …` | default `2`; `1` displays *"equivalent to strict single source"*; clamped to the live RDC count |
| **Hold note** 🆕 | static caption | *"Holds are always sourced from a single warehouse (BR-RDC-12)"* |
| Untagged banner | warning text, **all** modes | `All RDCs`: *"→ sourcing uses the fallback order"* · `Own` / `Cross`: *"→ these stores are excluded from this run"* |

`ALC_RDC_FALLBACK_ORDER` is **not** a cockpit control — it is standing configuration edited at
Settings → Business Rules with the existing SUPER_ADMIN gate and change audit.

### B8.4 Post-run — RDC SPLIT summary

```
┌ RDC SPLIT ───────────────────────────────────────────────────────────────┐
│  PICK BY SOURCE RDC   DW01  1,240      DH24  3,905                       │
│  Demand by store RDC  DW01  2,100      DH24  3,045     ← existing        │
│  Cross-shipped        1,106 pcs  (17%)  to 214 stores                    │
│  Split lines             38  of 8,421   (0.5%)                           │
│  Reduced lines            0   (cap / strict / hold-short)                │
│  Untagged sourced         0   by fallback order                          │
│  Residual by RDC      DW01  1  ·  DH24  14                               │
│                                                      [ Export picklist ] │
└──────────────────────────────────────────────────────────────────────────┘
```

Both RDC rows are shown deliberately — *demand by store's RDC* (existing) and *pick by source
RDC* (new) mean different things. Replacing one with the other would silently change a number ops
reads daily.

### B8.5 Run Parameters page

No work required — the new params flow into the existing `ARS_RUN_PARAMS_AUDIT` capture and appear
automatically in the Compare and Trends views.

### B8.6 🆕 Alloc Review and concurrency (M8 / R10)

**Alloc Review pivot.** `AlcReviewPage.jsx` pivots MAJ_CAT × RDC at several drill levels
(`:98`, `:254`, `:324`, `:400`). That RDC is the **store's**. After clubbing, a reviewer sees
*demand by store's RDC* while the warehouse picks by *source RDC* — in E1 that is **DW01: 42**
on screen versus **DW01: 14** on the picklist. **Phase 1: relabel the column `Store RDC`.**
Phase 2 (optional): add a source-RDC toggle. Open item **O7**.

**Concurrency.** Stock is only reserved at Approve. Under `Own`, two simultaneous parked runs
draw from **different pools** — the isolation is accidental but real. Clubbing destroys it: two
parked All-RDCs sessions would both allocate the same pieces. Production runs single-parked
today (409 guard, `listing.py:603`), so this is latent.
**Required: refuse `allow_multi_parked` while `ALC_RDC_CENTRAL_POOL` is active**, with a clear
error message.

## B9. FS-RDC-07 — Reporting

| Report | Change |
|---|---|
| **RDC-wise picking requirement** (new) | `SELECT SRC_RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ, SUM(SHIP_QTY), SUM(HOLD_QTY) FROM ARS_ALLOC_RDC_SPLIT … GROUP BY …` |
| **Store dispatch summary** (new) | per store × source RDC — how many documents each store expects |
| **Cross-ship exposure** (new) | `WHERE IS_CROSS = 1`, grouped by `(STORE_RDC, SRC_RDC)` — freight monitoring |
| **Residual by warehouse** (new) | `RDC stock − Σ(SHIP+HOLD)` from the split ledger. **Do not use `RDC_FNL_Q_REM_LIVE`** — it is clubbed under All RDCs (§B5) |
| `by_maj_cat_rdc` cockpit summary (`listing.py:5113-5220`) | **add** a pick-by-`SRC_RDC` block; **keep** the existing demand-by-store-RDC block |

## B10. Worked examples

### E1 — End-to-end (the reference case)

**Store master:** HS11 → `DW01`, HS12 → `DW01`, HP04 → `DH24`.

**Warehouse stock** — option `M_TEES_HS / 1110116457 / OFF_WHT`, pool FRESH

| RDC | S (5001) | M (5002) | L (5003) | Total |
|---|---|---|---|---|
| `DW01` | 10 | 0 | 5 | **15** |
| `DH24` | 0 | 40 | 20 | **60** |
| **Clubbed** | **10** | **40** | **25** | **75** |

**Store requirement** (after MBQ / I_ROD math — unchanged by this feature)

| Store | Own RDC | S | M | L | Need |
|---|---|---|---|---|---|
| HS11 | `DW01` | 4 | 8 | 6 | 18 |
| HS12 | `DW01` | 4 | 8 | 12 | 24 |
| HP04 | `DH24` | 4 | 10 | 6 | 20 |
| | | | | | **62** |

#### E1.1 — Today (`All RDCs`, two separate pools)

| # | Store | RDC | SZ | Need | Pool before | Shipped | Reason |
|---|---|---|---|---|---|---|---|
| 1 | HS11 | `DW01` | S | 4 | 10 | **4** | ok |
| 2 | HS11 | `DW01` | M | 8 | 0 | **0** | `POOL_EMPTY` — `DH24` holds 40, invisible |
| 3 | HS11 | `DW01` | L | 6 | 5 | **5** | short by 1 |
| 4 | HS12 | `DW01` | S | 4 | 6 | **4** | ok |
| 5 | HS12 | `DW01` | M | 8 | 0 | **0** | `POOL_EMPTY` |
| 6 | HS12 | `DW01` | L | 12 | 0 | **0** | `POOL_EMPTY` |
| 7 | HP04 | `DH24` | S | 4 | 0 | **0** | `POOL_EMPTY` — `DW01` holds 2, invisible |
| 8 | HP04 | `DH24` | M | 10 | 40 | **10** | ok |
| 9 | HP04 | `DH24` | L | 6 | 20 | **6** | ok |

| | Demand | Shipped | Unmet | Idle stock |
|---|---|---|---|---|
| **Total** | 62 | **29 (47 %)** | 33 | 46 |

#### E1.2 — Step 1: allocate from the clubbed pool (same order, same gates, same caps)

| # | Store | SZ | Need | Pool before | Ship | Pool after |
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

#### E1.3 — Step 2: split pass ledger (default `SINGLE_PREFERRED`, own-RDC-first)

Balances tracked per size, at the moment each line is processed.

| # | Store | Own | SZ | Qty | `DW01` bal | `DH24` bal | Covered by | ←`DW01` | ←`DH24` |
|---|---|---|---|---|---|---|---|---|---|
| 1 | HS11 | `DW01` | S | 4 | 10 | 0 | `DW01` alone (own) | **4** | — |
| 2 | HS11 | `DW01` | M | 8 | 0 | 40 | `DH24` alone | — | **8** |
| 3 | HS11 | `DW01` | L | 6 | 5 | 20 | `DH24` alone (own has 5 < 6) | — | **6** |
| 4 | HS12 | `DW01` | S | 4 | 6 | 0 | `DW01` alone (own) | **4** | — |
| 5 | HS12 | `DW01` | M | 8 | 0 | 32 | `DH24` alone | — | **8** |
| 6 | HS12 | `DW01` | L | 12 | 5 | 14 | `DH24` alone | — | **12** |
| 7 | HP04 | `DH24` | S | 2 | 2 | 0 | `DW01` alone (own empty) | **2** | — |
| 8 | HP04 | `DH24` | M | 10 | 0 | 24 | `DH24` alone (own) | — | **10** |
| 9 | HP04 | `DH24` | L | 6 | 5 | 2 | **neither** → split, own first | **4** | **2** |
| | | | | **60** | | | | **14** | **46** |

One split line (#9), four cross-ships. `14 + 46 = 60` ✓ · no balance ever negative ✓

#### E1.4 — `ARS_ALLOC_RDC_SPLIT` rows written (session `S-4471`, FRESH)

| WERKS | VAR_ART | SZ | **SRC_RDC** | SHIP_QTY | STORE_RDC | PREF_TIER | IS_CROSS |
|---|---|---|---|---|---|---|---|
| HS11 | 5001 | S | `DW01` | 4 | `DW01` | 1 | 0 |
| HS11 | 5002 | M | `DH24` | 8 | `DW01` | 1 | **1** |
| HS11 | 5003 | L | `DH24` | 6 | `DW01` | 1 | **1** |
| HS12 | 5001 | S | `DW01` | 4 | `DW01` | 1 | 0 |
| HS12 | 5002 | M | `DH24` | 8 | `DW01` | 1 | **1** |
| HS12 | 5003 | L | `DH24` | 12 | `DW01` | 1 | **1** |
| HP04 | 5001 | S | `DW01` | 2 | `DH24` | 1 | **1** |
| HP04 | 5002 | M | `DH24` | 10 | `DH24` | 1 | 0 |
| HP04 | 5003 | L | `DH24` | 2 | `DH24` | 1 | 0 |
| HP04 | 5003 | L | `DW01` | 4 | `DH24` | 1 | **1** |

**10 rows for 9 allocation lines.** `ARS_ALLOC_WORKING` still holds exactly **9** rows, with
HP04/5003/L carrying `SRC_RDC = 'MULTI'`, `SRC_SPLIT_CNT = 2`.

#### E1.5 — RDC-wise picking requirement

| RDC | S | M | L | **To pick** | Its stock | Left |
|---|---|---|---|---|---|---|
| `DW01` | 10 | 0 | 4 | **14** | 15 | 1 |
| `DH24` | 0 | 26 | 20 | **46** | 60 | 14 |
| | 10 | 26 | 24 | **60** | 75 | 15 |

#### E1.6 — Store dispatch summary

| Store | From `DW01` | From `DH24` | Total | Documents |
|---|---|---|---|---|
| HS11 | 4 (S4) | 14 (M8, L6) | 18 | 2 |
| HS12 | 4 (S4) | 20 (M8, L12) | 24 | 2 |
| HP04 | 6 (S2, L4) | 12 (M10, L2) | 18 | 2 |

#### E1.7 🆕 — Reservation ledgers at Approve (the R8 / M5 / M6 fix)

Aggregating the split rows by `(WERKS, VAR_ART, SRC_RDC)`:

| Row | `RDC` *(store — unchanged)* | **`SRC_RDC`** *(new)* | PEND qty |
|---|---|---|---|
| HS11 / 5001 | `DW01` | `DW01` | 4 |
| HS11 / 5002 | `DW01` | **`DH24`** | 8 |
| HS11 / 5003 | `DW01` | **`DH24`** | 6 |
| HS12 / 5001 | `DW01` | `DW01` | 4 |
| HS12 / 5002 | `DW01` | **`DH24`** | 8 |
| HS12 / 5003 | `DW01` | **`DH24`** | 12 |
| HP04 / 5001 | `DH24` | **`DW01`** | 2 |
| HP04 / 5002 | `DH24` | `DH24` | 10 |
| HP04 / 5003 | `DH24` | `DH24` | **2** |
| HP04 / 5003 | `DH24` | **`DW01`** | **4** |

**The last two rows are the M5 case:** same `SESSION_ID`, same `RDC` (`DH24`), same `ST_CD`, same
`ARTICLE_NUMBER`, same `ALLOC_MODE`. Without `SRC_RDC` in the guard, **the second is silently
dropped** and 4 pieces are never reserved.

Next MSA run deducts on `ISNULL(SRC_RDC, RDC)`:

| RDC | Pend deducted | Stock | Free after | Correct? |
|---|---|---|---|---|
| `DW01` | 14 | 15 | 1 | ✅ |
| `DH24` | 46 | 60 | 14 | ✅ |

Booked the old way (`RDC` only) it would have been `DW01` 42 / `DH24` 18 — leaving **32 phantom
pieces** at `DH24` for the next run to allocate again.

### E2 — The three split policies compared

L-size only: stock `DW01 = 5`, `DH24 = 20`; allocated HS11 6, HS12 12, HP04 6.

| Policy | HS11 (6) | HS12 (12) | HP04 (6) | `DW01` picks | `DH24` picks | Split lines | Dispatched |
|---|---|---|---|---|---|---|---|
| `SPLIT_ALWAYS` | `DW01`5 + `DH24`1 | `DH24`12 | `DH24`6 | 5 | 19 | 1 | **24 ✓** |
| `SINGLE_PREFERRED` *(default)* | `DH24`6 | `DH24`12 | `DH24`2 + `DW01`4 | 4 | 20 | 1 | **24 ✓** |
| `SINGLE_STRICT` | `DH24`6 | `DH24`12 | `DW01`5 only | 5 | 18 | 0 | **23 ✗ — 1 pc lost** |

Both non-strict policies dispatch the full 24 with exactly one split line — they differ only in
*which* line splits. `SINGLE_STRICT` reduces HP04 from 6 to 5 and loses a piece of served demand.

### E3 — SHIP + HOLD on one ledger (D2)

TBL option, size M, stock `DW01 = 3`, `DH24 = 9` (clubbed 12). Allocation: HS11 (own `DW01`)
ship 5 + hold 2; HP04 (own `DH24`) ship 4 + hold 1. SHIP tagged before HOLD; one shared ledger.

| # | Store | Own | Draw | Qty | `DW01` bal | `DH24` bal | Covered by | ←`DW01` | ←`DH24` |
|---|---|---|---|---|---|---|---|---|---|
| 1 | HS11 | `DW01` | SHIP | 5 | 3 | 9 | `DH24` alone (own has 3 < 5) | — | **5** |
| 2 | HS11 | `DW01` | HOLD | 2 | 3 | 4 | `DW01` alone (own) | **2** | — |
| 3 | HP04 | `DH24` | SHIP | 4 | 1 | 4 | `DH24` alone (own) | — | **4** |
| 4 | HP04 | `DH24` | HOLD | 1 | 1 | 0 | `DW01` alone (own empty) | **1** | — |
| | | | | **12** | | | | **3** | **9** |

Every hold landed on **one** warehouse, so each writes a single `ARS_NL_TBL_HOLD_TRACKING` row
and the PK holds. Line 4 is the **cross-RDC hold** D2 permits — HP04 is a `DH24` store but `DH24`
has nothing left, so its hold is physically reserved at `DW01`.

**Two failure modes the shared ledger prevents:**

| Wrong approach | What breaks |
|---|---|
| Hold tagged to the store's own RDC regardless of stock | HS11's 2 pcs tagged to `DW01`, which has 0 left → **phantom hold** |
| SHIP and HOLD tagged in two independent passes | Both see `DH24` = 9 and together book 12 → **double-booked stock** |

### E3.1 🆕 — When a hold cannot be covered by one warehouse (BR-RDC-12)

Change line 2 to a hold of **5**, with `DW01` at 3 and `DH24` at 4.

**What a split hold would require — and why it is impossible:**

| WERKS | VAR_ART | SZ | ALLOC_TYPE | SRC_RDC | HOLD_REM |
|---|---|---|---|---|---|
| HS11 | 5002 | M | FRESH | `DW01` | 3 |
| HS11 | 5002 | M | FRESH | `DH24` | 2 |

`PK = (WERKS, VAR_ART, SZ, ALLOC_TYPE)` — **identical primary keys**. The insert fails, or one row
silently overwrites the other and 2 or 3 pieces are reserved in the system but in no warehouse.

**Under BR-RDC-12:**

| Step | Result |
|---|---|
| Largest single source | `DH24` (4 > 3) |
| Hold placed | **4 at `DH24`** — one row, PK intact |
| Remainder | 1 piece dropped |
| Stamp | `ALLOC_REMARKS += 'RDC_HOLD_SHORT(alloc=5,held=4);'` |
| Counted | "Reduced lines: 1" in the run log and cockpit |

A hold is advisory, so reducing it is safe — unlike reducing a shipment.

### E4 — Mixed store master (D3, Option A)

**Store master** (deliberately messy — note 0 of 521 live stores are actually untagged today)

| Store | `ST_MASTER.RDC` | Tier used |
|---|---|---|
| HS11 | `DW01` | 1 — own |
| HS12 | *(blank)* | 2 — global `"DW01,DH24"` |
| HP04 | `DH24` | 1 — own |
| HP09 | `ALL` | 2 — global |

**Stock, L-size:** `DW01 = 15`, `DH24 = 20` → clubbed 35. Allocation from the clubbed pool — the
tag is not read, so every store is served:

| Store | Allocated | Today's result |
|---|---|---|
| HS11 | 10 | 10 |
| HS12 | **12** | **0** — blank tag matches no pool key |
| HP04 | 4 | 4 |
| HP09 | **3** | **0** — `'ALL'` is not a real RDC code |
| | **29** | 14 |

**Split pass** (`SINGLE_PREFERRED`, `ALC_RDC_PRIORITY = "DW01,DH24"`)

| # | Store | Tier / order | Need | `DW01` bal | `DH24` bal | Covered by | ←`DW01` | ←`DH24` |
|---|---|---|---|---|---|---|---|---|
| 1 | HS11 | `1` — own `DW01` | 10 | 15 | 20 | `DW01` | **10** | — |
| 2 | HS12 | `2` — `DW01,DH24` | 12 | 5 | 20 | `DH24` | — | **12** |
| 3 | HP04 | `1` — own `DH24` | 4 | 5 | 8 | `DH24` | — | **4** |
| 4 | HP09 | `2` — `DW01,DH24` | 3 | 5 | 4 | **either** | **3** | — |
| | | | **29** | | | | **13** | **16** |

**Only line 4 was decided by preference** — both warehouses could serve it:

| If HP09's order came from | Ships from | Store receives |
|---|---|---|
| tier 1, a real tag `DH24` | `DH24` | 3 |
| tier 2, `"DW01,DH24"` | `DW01` | 3 |
| tier 2, `"DH24,DW01"` | `DH24` | 3 |
| tier 3, largest-available | `DW01` (5 > 4) | 3 |

**An untagged store never loses stock — it only loses the ability to state a preference, and only
on lines where both warehouses could have served it.**

## B11. Invariants

| ID | Invariant | Enforcement |
|---|---|---|
| I-1 | `Σ ARS_ALLOC_RDC_SPLIT.SHIP_QTY = Σ ARS_ALLOC_WORKING.SHIP_QTY` (per session) | assertion + V4 |
| I-2 | `Σ ARS_ALLOC_RDC_SPLIT.HOLD_QTY = Σ ARS_ALLOC_WORKING.HOLD_QTY` | same |
| I-3 | Per `(SRC_RDC, VAR_ART, SZ)`: `Σ (SHIP_QTY + HOLD_QTY) ≤ that RDC's MSA FNL_Q` | ledger never negative; V3 |
| I-4 | Every allocation line with `SHIP_QTY > 0` has ≥ 1 split row | test |
| I-5 | `Own` / `Cross` output is byte-identical to the pre-change build, across **4 tables** | V5 — **release gate** |
| I-6 | The pass is deterministic: same inputs → same `SRC_RDC` | fixed ordering; V8 |
| I-7 | No **ship** line is sourced from more than `ALC_RDC_MAX_SPLIT` RDCs | assertion; V9 |
| I-8 | Every reduced line carries `RDC_SINGLE_SHORT`, `RDC_SPLIT_CAPPED` or `RDC_HOLD_SHORT`, and is counted | test |
| I-9 | Every `ARS_PEND_ALC` / hold-tracking row created by an All RDCs run carries a non-null `SRC_RDC` | Approve assertion; V12 |
| I-10 | Per warehouse and option-size: `Σ PEND + Σ HOLD ≤ that warehouse's physical stock`, on `ISNULL(SRC_RDC, RDC)` | V12 |
| I-11 | Mode separation (BR-RDC-11): an Own / Cross run writes zero `SRC_RDC` values and zero split rows | V14 — **blocking** |
| **I-12** 🆕 | **No HOLD line is ever sourced from more than one RDC** (BR-RDC-12) | assertion in the pass; V15 |
| **I-13** 🆕 | **No split rows survive a reject or a reverted approval** | V17 |

**Exception to I-1:** `SINGLE_STRICT`, a binding `ALC_RDC_MAX_SPLIT`, and a short hold reduce
`ARS_ALLOC_WORKING` **before** the comparison, so the invariant holds post-pass. Under the default
configuration with two RDCs neither of the first two can trigger.

## B12. Edge cases

| Case | Handling |
|---|---|
| Store tag names an RDC with no stock for the option | Tier 1 finds nothing; walk proceeds. No special code |
| Option exists at only one RDC | Every line sources there; no splits possible |
| `SHIP_QTY = 0`, `HOLD_QTY > 0` | Hold is tagged normally (single-source); no ship row emitted |
| Line reduced to 0 under `SINGLE_STRICT` | Row stays with `SHIP_QTY = 0` and the remark; treated as not-allocated by Part 8.5 |
| **A hold no single RDC can cover** 🆕 | BR-RDC-12 — largest single source, remainder dropped, `RDC_HOLD_SHORT` stamped (E3.1) |
| Part 8.55 releases a hold after tagging | Hold released on the working, parked **and split** rows alike (§B6.6) |
| **Generate re-run on the same session** 🆕 | Part 8.37 opens with `DELETE … WHERE SESSION_ID`; V16 |
| **A split line reaches Approve** | Writes one `ARS_PEND_ALC` row per source RDC. Requires the widened guard and `GROUP BY` (§B7.3 iii). Hold tracking is never split, so one row only |
| Three or more RDCs in future | The pass is written for **N** RDCs. Two settings become material at N ≥ 3: `ALC_RDC_PRIORITY` must name every RDC, and `ALC_RDC_MAX_SPLIT` starts to bind |
| A new RDC is created and not added to `ALC_RDC_PRIORITY` | Appended alphabetically — never silently excluded |
| `ALC_RDC_FALLBACK_ORDER` partial / stale / naming a dead RDC | Preference, never a filter — missing RDCs appended, unknown names ignored |
| Legacy pend / hold rows written before this change | `SRC_RDC IS NULL` → `ISNULL(SRC_RDC, RDC)` resolves to `RDC`. No backfill needed; V13 |
| Rounding | The pass moves whole allocated pieces only — it never re-derives a quantity, so pack-size rounding is preserved exactly |

## B13. Backward compatibility — protecting `Own` and `Cross`

Every change is gated on **BR-RDC-13**: `rdc_mode == 'all'` **AND** `ALC_RDC_CENTRAL_POOL` active,
resolved once at run start (§B1.4).

| # | Change | Gate | `Own` / `Cross` behaviour |
|---|---|---|---|
| FS-RDC-01 | Clubbed `MSA_FNL_Q` / `VAR_*` (3 fragments) | `_CENTRAL_POOL` | keeps today's exact SQL strings |
| ~~FS-RDC-02~~ | ~~Hold read-back~~ | — | **withdrawn (M1) — no code change** |
| FS-RDC-03 | RDC dropped from pool key + join | `_CENTRAL_POOL` | keeps 6-key pool |
| FS-RDC-04 | Split pass (Part 8.37) | `_CENTRAL_POOL` | **does not execute** |
| FS-RDC-05 | `SRC_RDC` / split rows written | `_CENTRAL_POOL` | never written — stays NULL / no rows |
| FS-RDC-06 | Split-policy radios | rendered only under `All RDCs` | not shown |
| §B7.3 (i)-(iv) | Ledger reads and writes | `ISNULL` / `COALESCE` chains | **inert by inspection** — NULL falls back to today's value |

**Three independent release gates, all blocking:**

1. **I-5 / V5 — output identity.** Re-run a completed `Own` session on the new build and diff
   **four tables** row-for-row against the baseline: `ARS_ALLOC_WORKING`, `ARS_ALLOC_HISTORY`,
   `ARS_PEND_ALC`, `ARS_NL_TBL_HOLD_TRACKING`.
2. **I-11 / V14 — mode separation.** The same Own run must leave `SRC_RDC` NULL everywhere and
   produce zero `ARS_ALLOC_RDC_SPLIT` rows.
3. **V18 🆕 — switch inertness.** An `Own` run with `ALC_RDC_CENTRAL_POOL` ON and OFF must produce
   identical output. Same for `Cross` (V19).

Together these mean an Own run cannot be affected by this work **even in principle** — it does not
enter the new code, it does not populate the new columns, and the switch cannot reach it.

## B14. Impact map

| File | Site | Change |
|---|---|---|
| `listing.py` | `~:697`, `:962` | 🆕 **M9** — resolve `_CENTRAL_POOL` once; stamp to run-params audit |
| `listing.py` | ~~Part 3.54 (`:1541`)~~ | 🆕 **M1 — NO CHANGE.** FS-RDC-02 withdrawn |
| `listing.py` | Part 3.55 (`:1630-1654`, `:1662-1700`) | **M2** — gate all **three** fragments |
| `listing.py` | **new Part 8.37** between `:3423` and `:3425` | FS-RDC-04 — split pass, opening with the idempotent DELETE |
| `listing.py` | Part 8.55 (`:3556`) | 🆕 **B6.6** — hold release cascades to split rows |
| `listing.py` | hold backfill (`:3704-3716`) | guard: `AND T.[SRC_RDC] IS NULL` |
| `listing.py` | `:5113-5220` | FS-RDC-07 — add pick-by-`SRC_RDC`, keep demand-by-store-RDC |
| `listing.py` | `GenerateRequest` (`:80`) | `rdc_split_policy`, `rdc_max_split` + validators |
| `listing.py` | `:603` | 🆕 **R10** — refuse `allow_multi_parked` while the switch is active |
| `rule_engine_new.py` | **`:829-867`** | 🆕 **M3** — `SRC_RDC` / `SRC_SPLIT_CNT` in the `SELECT … INTO` list |
| `rule_engine_new.py` | `:873-874` | FS-RDC-03 — pool build without RDC |
| `rule_engine_new.py` | `:3321-3366` | clubbed residual semantics (diagnostics only) |
| `rule_engine_per_opt.py` | `:63` | FS-RDC-03 — `POOL_KEYS` passed in, not hard-coded |
| `rule_engine_pandas.py` | orchestration | pass `_CENTRAL_POOL` through |
| **`pend_alc_service.py`** | **`:2304-2360`** | 🆕 **M5** — `SRC_RDC` column; widen the `NOT EXISTS` guard **and** the `GROUP BY` |
| **`msa_service.py`** | **`:94`, `:211`** | 🆕 **M7** — rewrite both hold loaders to `COALESCE(NULLIF(H.SRC_RDC,''), NULLIF(H.RDC,''), S.RDC)` |
| `alloc_pool.py` | `:192` | add `SRC_RDC` to the front of the existing `COALESCE` |
| **`parked_history.py`** | `:98`, `~:1620`, reject / revert / purge | 🆕 **M6** — split-table lifecycle; Approve reads `ARS_ALLOC_RDC_SPLIT`, not `'MULTI'` |
| `bdc.py` | — | **no change** — inherits the correct RDC once `pend_alc_service` lands |
| `AlcReviewPage.jsx` | `:98`, `:254`, `:324`, `:400` | 🆕 **O7** — relabel the pivot column `Store RDC` |
| `ListingPage.jsx` | `:1489`, `:2797` | FS-RDC-06 — third payload branch, sourcing panel, banners |
| Migration | new | `ARS_ALLOC_RDC_SPLIT` + 3 ledger `SRC_RDC` columns + 6 business rules |
| `frontend/public/docs/manual/listing.md` | FSD + Recorded rules | update after implementation |
| `data_dictionary.py` | hardcoded list | register the new table and columns |

**Explicitly unchanged:** OPT_TYPE classification, `ELIG_FLAG` gates, `PRI_CT%` / `ALLOC_FLAG`,
MBQ / MJ_REQ / secondary-grid caps, `I_ROD` rounds and per-size ceiling, pack-size rounding, store
ranking and manual priority, COMPLETE dispatch + overshoot, hold-release retry, Part 8.5
`OPT_STATUS`, **Part 3.54 hold read-back**, MSA build (stays per-RDC), grid tables, `bdc.py`.

## B15. Verification plan

| # | Check | Method | Gate |
|---|---|---|---|
| V1 | Quantify defect D-1 | Count `ARS_LISTING` rows in the last `All RDCs` session whose `MSA_FNL_Q` equals the *other* RDC's figure | evidence |
| V2 | Fill-rate improvement | Re-run a live session in both modes; compare Σ `SHIP_QTY`, unmet, residual | business sign-off |
| V3 | No RDC over-drawn (I-3) | `Σ (SHIP+HOLD)` per `(SRC_RDC, VAR_ART, SZ)` vs MSA `FNL_Q` | **blocking** |
| V4 | Conservation (I-1, I-2) | `Σ split.SHIP = Σ working.SHIP`; same for HOLD | **blocking** |
| **V5** | `Own` / `Cross` unchanged (I-5) — **4 tables** 🆕 | Row-for-row diff of `ARS_ALLOC_WORKING`, `ARS_ALLOC_HISTORY`, `ARS_PEND_ALC`, `ARS_NL_TBL_HOLD_TRACKING` vs the Step-0 baseline | **blocking** |
| V6 | Untagged fallback | Same session with tags blanked → identical quantities, `PREF_TIER = '2'` | **blocking** |
| V7 | Policy equivalence | `SPLIT_ALWAYS` vs `SINGLE_PREFERRED` → identical per-RDC totals | regression |
| V8 | Determinism (I-6) | Run the pass twice → identical `SRC_RDC` | regression |
| V9 | Split cap (I-7, I-8) | N=4 fixture where a ship line needs 3 sources → capped at 2, remainder reduced and stamped | **blocking** |
| V10 | Cap cannot bind at N=2 | Current 2-RDC data with `ALC_RDC_MAX_SPLIT = 2` → zero reduced ship lines | regression |
| V11 | Fallback order | N=4 fixture: full map (`1F`), partial map appends, absent key falls to global (`1`), unset reproduces V2 | **blocking** |
| V12 | Reservation ledger integrity (I-9, I-10) | After a central run + Approve: every new pend/hold row has `SRC_RDC`; and per `(ISNULL(SRC_RDC,RDC), option, size)`, `Σ PEND + Σ HOLD ≤` that warehouse's MSA stock | **blocking** |
| V13 | Legacy ledger fallback | Pre-change rows (`SRC_RDC IS NULL`) deduct exactly as before — re-run an MSA build and diff `FNL_Q` | **blocking** |
| V14 | Mode separation (I-11) | After an Own run: `COUNT(SRC_RDC) = 0` on all three tables and zero split rows. After an All RDCs run: `SRC_RDC` non-null on every allocated row | **blocking** |
| **V15** 🆕 | **Holds never split (I-12)** | Fixture where a hold exceeds every single RDC → exactly one hold row, quantity reduced, `RDC_HOLD_SHORT` stamped, counted | **blocking** |
| **V16** 🆕 | **Re-run idempotency** | Generate twice on one session → no duplicate or stale split rows; `Σ split = Σ working` | **blocking** |
| **V17** 🆕 | **Split-row lifecycle (I-13)** | Reject a parked central run, then revert an approved one → zero split rows remain for that session | **blocking** |
| **V18** 🆕 | **Switch inertness — `Own`** | `Own` run with `ALC_RDC_CENTRAL_POOL` ON vs OFF → identical output | **blocking** |
| **V19** 🆕 | **Switch inertness — `Cross`** | Same for `Cross`. ⚠️ **Blocked:** all Cross history was purged by the 30-day TTL (last run 2026-07-25), so no baseline exists. **One fresh Cross session must be run and baselined in Step 0** before Step 2 begins | **blocking** |
| **V20** 🆕 | **Split-line reservation survives** | E1.7 fixture: HP04's split L line produces **two** `ARS_PEND_ALC` rows, not one | **blocking** |
| **V21** 🆕 | **Performance** | Part 8.37 completes in ≤ 15 s per 10,000 allocation lines; bulk insert, not row-by-row | **blocking** |
| **V22** 🆕 | **Part 3.54 is mode-independent (M1)** | Assert the generated SQL contains no RDC predicate in any of the three modes | regression |

Unit tests alongside `backend/tests/test_alloc_round_stamp_per_opt.py`, covering: single source,
split ship line, cross-ship, exact-fit, `SINGLE_STRICT` reduction, `MAX_SPLIT` cap reduction,
`MAX_SPLIT = 1 ≡ SINGLE_STRICT`, SHIP+HOLD shared ledger, **short hold reduction**, all three
fallback tiers, priority ordering with an unlisted RDC appended, and an N=4 RDC case.

---

# PART C — Implementation runbook

Seven steps, each independently verifiable and independently revertible. **No step changes an
existing `Own` or `Cross` run**; the release gate at every step is that a completed Own session
re-runs byte-identical across four tables.

The worked example E1 (§B10) is the reference fixture throughout: 3 stores, `DW01` S10 M0 L5 and
`DH24` S0 M40 L20, demand 62. Today it ships 29; after Step 3 it must ship 60.

## Step 0 — Evidence and baseline (no code)

| | |
|---|---|
| **Do** | Measure defect D-1 (V1) — ✅ done. Capture a completed **`Own`** session — ✅ done (`own_20260919_125317_275`). **Run and baseline one small `Cross` session** — 🔴 required, none survives the TTL. Capture both — export all **four** tables to `docs/baselines/<sid>/`. Answer O1, O2, O5, O6 |
| **Why first** | **V5 is impossible without it.** Once code changes, the "before" state is gone and Own can never be proven unchanged |
| **Gate** | Baselines exported and checksummed; O1, O2, O6 answered in writing |
| **Revert** | n/a |

## Step 1 — Schema and switch only (additive, zero behaviour change)

| | |
|---|---|
| **Do** | Create `ARS_ALLOC_RDC_SPLIT` + 2 indexes. Add `SRC_RDC` to `ARS_PEND_ALC`, `ARS_NL_TBL_HOLD_TRACKING`, `ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT`. Add `SRC_RDC` / `SRC_SPLIT_CNT` to the **`SELECT … INTO` list** in `rule_engine_new.py:829-867` (**M3 — not an ALTER**). Seed all 6 business rules with `ALC_RDC_CENTRAL_POOL` **inactive** |
| **Example** | `ALTER TABLE ARS_PEND_ALC ADD SRC_RDC NVARCHAR(20) NULL` — metadata-only, instant at 17.8M rows |
| **Gate** | V5 (4-table, byte-identical) · V13 (MSA `FNL_Q` identical — all legacy rows are NULL) · V14 (Own leaves every new column NULL) |
| **Revert** | Drop the columns and the table. Nothing reads them yet |

## Step 2 — Clubbed read path, switch-gated

| | |
|---|---|
| **Do** | **M9** resolve `_CENTRAL_POOL` once at run start and thread it through. **M2** FS-RDC-01 — gate all three SQL fragments. FS-RDC-03 — RDC out of `POOL_KEYS` and the pool-build join. **M1 — do NOT touch Part 3.54** |
| **Example** | E1: HS11's M-size now sees a pool of 40 instead of 0. `MSA_FNL_Q` becomes a deterministic sum instead of an arbitrary per-RDC pick |
| **Gate** | V5, V18, V19 (switch inertness) — blocking. V22 (3.54 untouched). V2: the E1 fixture ships **60**, not 29 |
| **Revert** | Switch off |

> At the end of this step the allocation is correct but **untagged** — every row still carries only
> the store's RDC. **Do not Approve a central run before Step 4.**

## Step 3 — The split pass (new Part 8.37)

| | |
|---|---|
| **Do** | Implement §B6.3.2 — three passes, the `ALC_RDC_MAX_SPLIT` cap, **BR-RDC-12 single-source holds**, the §B6.4 preference chain, the opening idempotent DELETE. Write `ARS_ALLOC_RDC_SPLIT` and stamp `SRC_RDC` / `SRC_SPLIT_CNT`. Cascade Part 8.55 hold release |
| **Example** | E1 ledger (§B10 E1.3): `DW01` picks 14, `DH24` picks 46, one split line (HP04's L = `DH24`2 + `DW01`4), four cross-ships |
| **Gate** | V3, V4, V9, V15, V16, V21 — blocking. V7, V8, V10 — regression |
| **Revert** | Skip the pass; rows keep `SRC_RDC` NULL and remain un-dispatchable — which is why Step 4 must not ship before this is green |

## Step 4 — Downstream propagation (the R8 fix) 🔴

| | |
|---|---|
| **Do** | **M6** Approve aggregates `ARS_ALLOC_RDC_SPLIT` (not `'MULTI'` from history) and writes one reservation row per source RDC. **M5** widen the pend `NOT EXISTS` guard **and** `GROUP BY` with `SRC_RDC`. **M7** rewrite both `msa_service` hold loaders; extend `alloc_pool`'s `COALESCE`. Guard the hold backfill to `SRC_RDC IS NULL`. **M6** split-table lifecycle on reject / revert / purge |
| **Example** | E1.7 — HP04's split L line produces **two** pend rows (`DH24` 2, `DW01` 4). Without M5 the second is silently dropped. Next MSA deducts `DW01` 14 / `DH24` 46, not `DW01` 42 / `DH24` 18 |
| **Gate** | V12, V13, V17, V20 — all blocking |
| **Revert** | Revert the Approve write; existing rows are harmless because `SRC_RDC` is additive |

> **This is the step that must not be skipped or reordered.** Steps 2-3 are safe without it only
> so long as no central run is Approved.

## Step 5 — UI and reporting

| | |
|---|---|
| **Do** | §B8 — sourcing panel, central-pool badge, live pool summary, untagged banner in **all** modes, post-run RDC SPLIT block, picklist export. §B9 four reports. **O7** relabel the Alloc Review pivot `Store RDC`. **R10** refuse `allow_multi_parked` while the switch is active |
| **Example** | Selecting `All RDCs` reveals the sourcing panel; the post-run block reports pick-by-source-RDC alongside the existing demand-by-store-RDC |
| **Gate** | UAT with ops; the untagged banner must also appear (warning only) in `Own` |
| **Revert** | Hide the panel; engine defaults apply |

## Step 6 — Parallel run (switch still OFF for routine work)

| | |
|---|---|
| **Do** | Run the same live session in `Own` (this is the run that ships) and again with the switch ON in a **non-parked** session, purely to measure. Repeat across ≥ 3 sessions with different MAJ_CAT mixes |
| **Example** | Expect the E1 pattern at scale: higher fill, lower residual, a small number of split lines, zero reduced ship lines |
| **Gate** | V2 delta accepted in writing by management; warehouse confirms O1 and O3 |
| **Revert** | Keep using `Own` |

## Step 7 — Cutover

| | |
|---|---|
| **Do** | Announce. Flip `ALC_RDC_CENTRAL_POOL` to active. Watch the first three runs: reduced ship lines must be 0; cross-ship % plausible; `Σ split = Σ working`. Keep `Own` available |
| **Gate** | Three clean runs |
| **Revert** | **Flip the switch off.** Next Generate reverts to today's behaviour — no deploy, no migration, no data repair |

> **Code-review rule:** no change may be written that the switch cannot undo. This is what makes
> every step above single-click reversible.

## Step 8 — Post-implementation (required by `CLAUDE.md`)

| # | Task | Status |
|---|---|---|
| 1 | `frontend/public/docs/manual/listing.md` — new FSD section *"RDC Scope — per-warehouse pools vs the central pool"*, 5 new key columns, dated Recorded rule | ✅ **done 2026-09-22** |
| 2 | `data_dictionary.py` SEED — `SRC_RDC`, `SRC_SPLIT_CNT`, `PREF_TIER`, `IS_CROSS` | ✅ **done** (idempotent top-up seeds them on next use) |
| 3 | `docs/RULE_MASTER.md` — new §5.11 with BR-RDC-01..13 | ✅ **done** |
| 4 | `.claude/agents/ars_flow_kb/listing.md` — extract + the 8 gotchas a future editor needs | ✅ **done** |
| 5 | **Screenshots** (`node tools/manual/capture_steps.js`) | ⏸ **deferred to Step 7** |
| 6 | **Release note** | ⏸ **deferred to Step 7** |

**Why 5 and 6 are deferred, not skipped.** Both describe what a *user sees*, and
with `ALC_RDC_CENTRAL_POOL` inactive a user sees nothing new: the RDC Sourcing
panel and the post-run RDC SPLIT block only render when clubbing is live, so a
capture today would photograph the old screen, and a release note would announce
a change that has not happened. Both belong at cutover, in this order: flip the
switch → run one session → capture → publish the note.

### Release note drafted for Step 7 (do not publish before cutover)

> **Area:** Allocation engine · **Title:** All RDCs now pools stock across warehouses
>
> Choosing **All RDCs** in RDC Scope now adds up the stock of every warehouse,
> allocates against that single pool using exactly the same rules as before, and
> then decides which warehouse physically ships each line. Stores are no longer
> limited to what their own warehouse happens to hold.
>
> *Example:* a store tagged to DW01 needs 8 medium tees. DW01 has none, DH24 has
> 40. Previously the store got zero. Now it gets its 8, picked at DH24, and DH24's
> picking list shows them.
>
> **Own** and **Cross** are completely unchanged.

---

## B16. Open items

| # | Item | Owner | Needed by |
|---|---|---|---|
| **O1** | Warehouse confirmation that picking can execute a two-source line for one store-size — if not, the default moves to `SINGLE_STRICT` | Supply chain | **Step 0** |
| **O6** 🆕 | **Confirm BR-RDC-12** — a hold is never split; a hold no single RDC can cover is reduced and stamped | Supply chain | **Step 0** |
| **O2** | Value for `ALC_RDC_PRIORITY` — ordered list naming every RDC (`"DW01,DH24"`) | Business owner | Step 1 |
| ~~**O5**~~ | ~~Confirm whether `ARS_FACONS_PEND` is fed from ARS allocations~~ **CLOSED 2026-09-21 — NO.** Separate FA/CONS pipeline, seeded from `ARS_FACONS_ALLOC_ART`; absent from `msa_service` and `alloc_pool`. No change required (§B7.3) | ARS product owner | ✅ done |
| **O7** 🆕 | Alloc Review pivot — relabel to `Store RDC` now, or build a source-RDC toggle? | Product owner | Step 5 |
| **O8** 🆕 | Picking report scope — RDC-scoped (warehouse sees its whole picklist) or store-RLS-scoped (filtered, and therefore **incomplete as a picking document**)? | Business owner | Step 5 |
| O3 | Inter-warehouse transfer document, or STO direct from the sourcing RDC? | Supply chain / SAP | Step 6 |
| O2a | Confirm `ALC_RDC_MAX_SPLIT = 2`. Cannot bind while only two RDCs exist | Supply chain | before a 3rd RDC |
| O2b | Populate `ALC_RDC_FALLBACK_ORDER` per RDC (freight distance / transit days) | Supply chain | before a 3rd RDC |
| O4 | Target date to clean untagged stores. **Live check: 0 of 521 untagged** — insurance, not a live gap | Business owner | Step 7 |

## B17. Glossary

| Term | Meaning |
|---|---|
| **RDC** | Regional distribution centre (warehouse). Currently two: `DW01`, `DH24` |
| **Clubbed / central pool** | The sum of all in-scope RDCs' stock for one option-size, used as one allocatable quantity |
| **Split pass** | The new post-allocation step (Part 8.37) that assigns a source RDC to every shipped and held piece |
| **`SRC_RDC`** | The warehouse that will **physically ship** a line. NULL on every `Own` / `Cross` run |
| **Store's own RDC** | `Master_ALC_INPUT_ST_MASTER.RDC` — under `All RDCs` a sourcing *preference* |
| **Cross-ship** | A line sourced from an RDC other than the store's own. **Permitted for both ship and hold** |
| **Split line** | One store-size **shipment** sourced from two or more RDCs. Holds are never split (BR-RDC-12) |
| **OPT** | `MAJ_CAT + GEN_ART_NUMBER + CLR` |
| **Line** | One allocation row: store × option × `VAR_ART` × size |
| **The switch** | Business rule `ALC_RDC_CENTRAL_POOL` (BR-RDC-13) — the single control that turns central pooling on or off |

## B18. Review checklist before build starts

| # | Item | Status |
|---|---|---|
| 1 | D1 / D2 / D3 decisions confirmed (§A5) | ✅ approved 2026-09-19 |
| 2 | **O1** — picking can execute a two-source line | ⬜ open |
| 3 | **O6** — BR-RDC-12, holds never split | ⬜ open |
| 4 | **O5** — `ARS_FACONS_PEND` provenance | ✅ closed 2026-09-21 — no change needed |
| 5 | O2 / O2a / O2b — priority list, split cap, fallback map | ⬜ open |
| 6 | O7 / O8 — review pivot label, picking-report scope | ⬜ open |
| 7 | Step 0 baselines captured (**Own and Cross, 4 tables each**) | ⬜ pending |
| 8 | V3-V5, V9, V11-V21 accepted as blocking release gates | ⬜ sign-off |
| 9 | `Own` / `Cross` out of scope and protected by V5 + V14 + V18/V19 | ✅ §A4, §B13 |
| 10 | Code-review rule agreed: nothing ships that the switch cannot undo | ⬜ sign-off |
