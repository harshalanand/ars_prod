# ARS Rule Master — MSA, Grid, Listing, Allocation, Hold/Pend

**Audience:** anyone reviewing an ARS run output (MSA generate, grid build, listing, allocation, hold/pend). Use this as the single reference to answer "is the system following the rules?" and to suggest new rules where you see gaps.

**Authoritative sources (deep dives, kept in sync):**
- [.claude/agents/ars_flow_kb/INDEX.md](../.claude/agents/ars_flow_kb/INDEX.md) — per-module rule files
- [backend/app/services/](../backend/app/services/) — implementation
- [backend/app/api/v1/endpoints/listing.py](../backend/app/api/v1/endpoints/listing.py) — OPT_TYPE classification + listing pipeline

**Glossary (short form):**
- **OPT** = one allocation unit = `(WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR)`
- **WERKS** = store; **RDC** = regional DC (was `ST_CD` pre-rename)
- **STK / PEND / HOLD / FNL_Q** = stock / pending alloc / held / final available
- **MBQ** = Minimum Base Qty (per-size dispatch unit); **MJ_MBQ** = MAJ_CAT roll-up
- **SZ_MBQ** = size-level MBQ; **I_ROD** = ideal rounds of dispatch
- **PAK** = pack/multiple constraint at size grain
- **RL / TBC / TBL / MIX** = OPT classifications (see § Listing)

---

## 1. Pipeline at a glance

```
┌─────────────────┐   ┌────────────┐   ┌──────────┐   ┌──────────────┐   ┌─────────────┐
│ MSA Calculation │ → │ Grid Build │ → │ Listing  │ → │ Allocation   │ → │ Hold / Pend │
│ (stock supply)  │   │ (demand)   │   │ (tag OPT)│   │ (per-OPT eng)│   │ (lifecycle) │
└─────────────────┘   └────────────┘   └──────────┘   └──────────────┘   └─────────────┘
       │                    │                │                │                  │
   ARS_MSA_*           ARS_GRID_*       ARS_LISTING      ARS_LISTED /        ARS_PEND_ALC
   GEN_ART / VAR_ART   MJ_RNG_SEG +     OPT_TYPE         ARS_ALLOC_HISTORY   ARS_NL_TBL_
   / TOTAL             sec-cap grids    tagged                               HOLD_TRACKING
```

Each stage feeds the next; rules apply at each boundary. Reviewing a result means walking back through these stages with the rules below in hand.

---

## 2. Cross-cutting invariants

These apply across MSA / Grid / Listing / Allocation. A run that violates any of them is broken.

| # | Invariant | What it means | Where it gets violated |
|---|---|---|---|
| 1 | **OPT uniqueness** | One `(WERKS, MAJ_CAT, GEN_ART, CLR)` ⇒ exactly one `OPT_TYPE` (RL ∨ TBC ∨ TBL ∨ MIX) after Listing Part 3.6 | Hold/unpark paths if `OPT_TYPE` changes silently |
| 2 | **Growth at MJ + grid only** | Growth % is applied at `MAJ_CAT × grid` level. **Never per `OPT_TYPE`** | Any code that scales an OPT-row's MBQ by growth |
| 3 | **MBQ sparseness** | `MBQ = 0` means *no constraint* at that grain. Do not apply 1.30× breach when MBQ = 0 | Sec-cap path if it treats 0 as "zero allowed" |
| 4 | **Sec-cap dims must propagate** | `FAB`, `MACRO_MVGR`, `MICRO_MVGR`, `M_VND_CD`, `RNG_SEG` must survive listing → listed → alloc | Any pipeline stage that drops a column |
| 5 | **`ACS_D` ≠ daily sale** | `ACS_D` is an aggregated demand figure. For velocity, use `MAX_DAILY_SALE` | Anywhere `ACS_D` is divided by days as if it were a rate |
| 6 | **`RNG_SEG` = MRP tier** | Values are `E` / `V` / `P` / `SP` (essential / value / premium / super-premium). Not a free text field | UI filters that allow arbitrary values |

---

## 3. MSA Calculation rules

**Source:** [backend/app/services/msa_service.py](../backend/app/services/msa_service.py) — `MSAService.calculate()`
**Trigger:** `POST /api/v1/msa/calculate` (via `msa_job_service`)
**Outputs (rep_data DB):** `ARS_MSA_TOTAL`, `ARS_MSA_VAR_ART`, `ARS_MSA_GEN_ART`, `MSA_Calculation_Sequence`

### 3.1 11-step MSA flow (universe-anchored, June 2026)

| Step | Action | Rule |
|---|---|---|
| 1 | Filter SLOC | Keep only rows where `SLOC IN (selected_slocs)`. Out-of-scope shelves never contribute stock. |
| 2 | Normalize numerics | Cast to numeric, NaN → 0 |
| 3 | Fill missing dims | Apply column defaults (e.g. `CLR='NA'`) |
| 4 | SEG filter | Keep `SEG IN ('APP','GM')` |
| 5 | Pivot by SLOC | Rename `ST_CD → RDC` **immediately after pivot** (must happen before any downstream column lookup — see § 3.5 bug) |
| 6 | **Universe backfill** | `_load_universe(slocs, date)` returns `(RDC, GEN_ART)` union of: (A) stock in selected SLOCs, (B) open `ARS_PEND_ALC`, (C) open `ARS_NL_TBL_HOLD_TRACKING`. Backfill VAR_ARTs from `vw_master_product` so every PEND/HOLD has a row. |
| 7 | Merge PEND | `ARS_PEND_ALC` → `PEND_QTY` on `(RDC, ARTICLE_NUMBER)` |
| 8 | Merge HOLD | `ARS_NL_TBL_HOLD_TRACKING` → `HOLD_QTY` (map `WERKS → RDC`) |
| 9 | Compute `FNL_Q` | `FNL_Q = max(STK − PEND − HOLD, 0)` |
| 10 | Threshold | Keep groups where `Σ FNL_Q + Σ PEND_QTY + Σ HOLD_QTY > threshold` (admits pend-only / hold-only groups) |
| 11 | Aggregate to GEN_ART | Group VAR_ART → GEN_ART by `(RDC, MAJ_CAT, GEN_ART_NUMBER, CLR)` |

### 3.2 Universe rule (post-correction, June 2026)

Stock contribution stays **SLOC-scoped**. Products with stock only on *non-selected* shelves do not enter MSA. The universe expands **only** when a real obligation (PEND or HOLD) attaches to the `(RDC, GEN_ART)` pair. Cross-shelf stock alone is never enough.

### 3.3 Reconciliation guarantees (must all hold after every Generate)

| # | Check | Pass condition |
|---|---|---|
| R1 | `Σ TOTAL.PEND_QTY == Σ ARS_PEND_ALC.PEND_QTY` for `IS_CLOSED=0` | Δ = 0 |
| R2 | `Σ TOTAL.HOLD_QTY == Σ ARS_NL_TBL_HOLD_TRACKING.HOLD_REM` for `IS_CLOSED=0` (via WERKS→RDC) | Δ = 0 |
| R3 | `Σ TOTAL.STK_QTY == Σ VW_ET_MSA_STK_WITH_MASTER.STK_Q` for run date, selected SLOCs, `SEG IN ('APP','GM')` | Δ = 0 |
| R4 | `count(distinct VAR_ART.ARTICLE_NUMBER) == count(distinct TOTAL.ARTICLE_NUMBER)` per passing group | Equal |
| R5 | Per `(RDC, MAJ_CAT, GEN_ART, CLR)`: `GEN_ART.X == Σ VAR_ART.X` for X ∈ {STK_QTY, PEND_QTY, HOLD_QTY, FNL_Q} | Equal |

> If any of R1–R5 ≠ 0, the bug is upstream of allocation — fix MSA first.

### 3.4 Eight write paths to `ARS_MSA_TOTAL / VAR_ART / GEN_ART`

Only path 1 ever inserts rows or touches `STK_QTY`. Paths 2–8 are UPDATE-only and assume the row exists (that's why universe-anchored Step 6 matters).

| # | Function | Trigger | Writes |
|---|---|---|---|
| 1 | `msa_result_storage.store_results` | MSA Generate | TRUNCATE+INSERT all 3 tables; auto-runs paths 7+8 |
| 2 | `adjust_msa_after_pend_insert` | PEND_ALC INSERT (manual, CSV, approve_parked) | PEND_QTY/FNL_Q for affected `(RDC, ART)` |
| 3 | `apply_pend_alc_delta(sign=±1)` | PEND_ALC mutation with explicit sign | PEND_QTY/FNL_Q delta in all 3 |
| 4 | `apply_pend_alc_delta_by_session` | Approve Parked / session-scoped revert | Same as #3, scoped by SESSION_ID |
| 5 | `apply_hold_clear` | Hold Dashboard "Clear" | HOLD_QTY/FNL_Q for closed hold rows |
| 6 | `apply_hold_revise` | Hold Dashboard "Revise" | HOLD_QTY/FNL_Q for revised hold rows |
| 7 | `bootstrap_msa_pend_sync` | End of #1, post-revert, post-grid-build | Full reseed of PEND_QTY/FNL_Q |
| 8 | `bootstrap_msa_hold_sync` | End of #1, after `approve_parked` PEND delta | Reseed HOLD_QTY/FNL_Q |

**Invariant for all paths:** `FNL_Q = max(STK_QTY − PEND_QTY − HOLD_QTY, 0)` is recomputed inside every UPDATE. Rollup is always `TOTAL → VAR_ART → GEN_ART`.

### 3.5 Historical bug fixes (read git history for code)

- **GEN_ART RDC-grain bug** (Jun 2026): Step 11 lookup ran before `ST_CD → RDC` rename ⇒ `RDC` missing from hierarchy ⇒ collapsed across RDCs and `agg("first")` stamped arbitrary RDC. Fix: rename right after Step 5 pivot.
- **Pend-only articles dropped** (Jun 2026): pre-universe code only merged PEND onto pre-existing SLOC pivot rows ⇒ stationary-set items with stock only on non-selected SLOCs were silently dropped. Fix: Universe Source B picks up open-PEND `(RDC, GEN_ART)`.

---

## 4. Grid Builder rules

**Source:** [backend/app/services/grid_calculations.py](../backend/app/services/grid_calculations.py), [backend/app/api/v1/endpoints/grid_builder.py](../backend/app/api/v1/endpoints/grid_builder.py)

### 4.1 Grid taxonomy

| Grid | Type | Grain | Purpose |
|---|---|---|---|
| `MJ_RNG_SEG` | **Primary** | `MAJ_CAT × RNG_SEG` | Main MBQ & growth driver |
| `MJ_FAB` | Sec-cap | `MAJ_CAT × FAB` | Fabric breach gate |
| `MJ_MICRO_MVGR` | Sec-cap | `MAJ_CAT × MICRO_MVGR` | Micro-vendor-group gate |
| `MJ_<other>` | Sec-cap | `MAJ_CAT × <dim>` | Configurable extras |

`RNG_SEG` is the MRP tier: `E` (essential) / `V` (value) / `P` (premium) / `SP` (super-premium).

### 4.2 Required dimensions on every grid row

Every grid row **must** carry `FAB`, `MACRO_MVGR`, `MICRO_MVGR`, `M_VND_CD`, `RNG_SEG`. If any are dropped between listing → listed → alloc, sec-cap silently loses grids (invariant 4).

### 4.3 Growth % rule

Growth is applied **only at `MAJ_CAT + grid` level**. Never per `OPT_TYPE`. Holds for the main path and any fallback path (invariant 2).

### 4.4 Sec-cap dispatch (strict mode, default since 2026-06-16)

When `apply_sec_cap_in_normal = True` (default), for each grid where `ARS_GRID_BUILDER.sec_cap_applicable = 1`:

```
budget = max(0, MBQ_ORIG × sec_cap_pct% − STK_TTL)
breach ⇒ block the OPT row at the main-pass pre-gate
```

`sec_cap_pct` is per-grid (`ARS_GRID_BUILDER.sec_cap_pct`, default 130 when NULL). Strict binary semantics — there is no longer a high-demand override.

### 4.5 Grid schema management rules

| Rule | Reason |
|---|---|
| `_ensure_hierarchy_table` is **add-only** | Deleting a grid / setting Inactive does NOT drop a column from `ARS_GRID_HIERARCHY`; data survives accidental deletes |
| Physical column order is no longer reshuffled | Consumers reference by name, not ordinal |
| Orphan column removal goes through `POST /grid-builder/hierarchy/compact` | Explicit admin action; default `dry_run=true`. Includes `MERGE_<X>` orphans when parent `X` is gone |
| **No** `DBCC SHRINKFILE` after Run-All | Routine shrink fragments indexes; file regrows next run anyway |
| S5 `_insert_missing_msa_rows` skipped when `grid.pivot_only=1` | Article-grain grids would only get `(NA,NA,NA)` placeholders polluting downstream |
| `bootstrap_msa_pend_sync` at end of Run-All is **kept** | Cheap safety net; prevents PEND/FNL_Q drift after heavy rebuild |

---

## 5. Listing rules

**Source:** [backend/app/api/v1/endpoints/listing.py](../backend/app/api/v1/endpoints/listing.py) (Parts 1 → 8.5)

### 5.1 OPT_TYPE classification (Part 3.6) — decision table

Evaluated **top to bottom, first match wins**. Inputs: `STK_TTL`, `MSA_FNL_Q`, `RL_HOLD_QTY`, `VAR_COUNT`, `VAR_FNL_COUNT`, `ACS_D`. Parameters: `stock_threshold_pct` (default 0.6), `default_acs_d` (18), `min_size_count` (3).

Let `SUPPLY_OK = (MSA_FNL_Q > 0) OR (RL_HOLD_QTY > 0)` and `ADEQUATE_STK = STK_TTL ≥ stock_threshold_pct × COALESCE(NULLIF(ACS_D,0), default_acs_d)`.

| Order | Branch | Condition | Result |
|---|---|---|---|
| 1 | MIX (a) — nothing to send | `NOT ADEQUATE_STK` AND `MSA_FNL_Q = 0` AND `RL_HOLD_QTY = 0` | `MIX` |
| 2 | MIX (b) — poor color fill | `VAR_COUNT > 0` AND `(VAR_FNL_COUNT / VAR_COUNT < threshold` OR `VAR_FNL_COUNT < min_size_count)` | `MIX` |
| 3 | RL — adequate stock or live hold, plus fresh MSA | `(ADEQUATE_STK OR RL_HOLD_QTY > 0)` AND `MSA_FNL_Q > 0` | `RL` |
| 4 | TBC — low (positive) stock, supply available | `0 < STK_TTL < threshold × ACS_D` AND `SUPPLY_OK` | `TBC` |
| 5 | TBL — zero/negative stock, supply available | `STK_TTL ≤ 0` AND `SUPPLY_OK` | `TBL` |
| 6 | Catch-all | (none of the above) | `MIX` |

### 5.2 MIX aggregation (Part 3.7)

`MIX` rows are always aggregated. Modes:

| `mix_mode` | Behaviour |
|---|---|
| `st_maj_rng` (**default**) | 1 MIX row per `(WERKS, MAJ_CAT, RNG_SEG)` — finer |
| `st_maj` | 1 MIX row per `(WERKS, MAJ_CAT)` |
| `each` | Keep each MIX row as-is (tag only) |

Legacy aliases: `aggregate → st_maj`, `mark → each`.

### 5.3 Sequencing & ordering

Working / alloc rows are ordered: `ST_RANK → MAJ_CAT → OPT_TYPE (RL=1, TBC=2, TBL=3) → OPT_PRIORITY_RANK → WERKS [→ SZ for alloc]`. `ST_RANK` is the per-MAJ_CAT priority rank of the store.

### 5.4 OPT_MBQ rules

| Rule | Where |
|---|---|
| `HOLD_DAYS` extra days apply **only to** `OPT_TYPE='TBL'` | Listing Part 4 (`OPT_MBQ_WH`) |
| `OPT_MBQ = 0` when `OPT_TYPE = 'MIX'` | Listing |
| For new articles (`IS_NEW=1` AND `AGE < age_threshold`), use `PER_OPT_SALE` rate instead of `ACS_D` | `age_threshold` default 15 |

### 5.5 MJ_REQ caps (per OPT_TYPE downward cap)

After waterfall, `SUM(SHIP_QTY)` for each OPT_TYPE per `(WERKS, MAJ_CAT)` is clamped to:

```
cap_pct% × MJ_REQ
```

Defaults: `rl_mj_req_cap_pct = tbc_mj_req_cap_pct = tbl_mj_req_cap_pct = 100.0` (hard ceiling at MAJ_CAT requirement). 0 disables. Independent of MBQ caps below.

### 5.6 MBQ caps (Decision 4-B, per OPT_TYPE)

Anchored to `MJ_MBQ_ORIG` (NOT post-growth `MJ_MBQ_REV`). Defaults: `100` = no over-ship vs original MBQ. 0 disables.

### 5.7 Growth headroom (`mj_req_growth_pct`)

- `100` (default) = strict, waterfall stops at `MAJ_CAT` target
- `>100` scales `MJ_MBQ → MJ_MBQ_REV` (sibling column; original preserved). `MJ_REQ_REV = max(0, MJ_MBQ_REV − MJ_STK_TTL)`. `MJ_REQ_ORIG` retained for audit

### 5.8 Primary-grid coverage gate

| Toggle | Behaviour |
|---|---|
| `pri_ct_check_rl = False` (default) | RL allowed even if `PRI_CT% < 100` (boosted MBQ-cap path activates) |
| `pri_ct_check_tbc = False` (default) | Same for TBC |
| TBL | **Always** enforces `PRI_CT% ≥ 100` (R06 + revalidation `SKIP_PRI_BROKEN`) |

### 5.9 R07 size-coverage gate (TBL only)

Skip a TBL row when `VAR_FNL_COUNT / VAR_COUNT < size_threshold` AND `VAR_FNL_COUNT < min_size_count`. `size_threshold` defaults to 0.6 — independent of `stock_threshold_pct` (which only drives Part 3.6).

### 5.10 Post-alloc OPT_STATUS (Part 8.5)

| `OPT_TYPE` | `ALLOC_QTY` | Post-alloc `OPT_STATUS` |
|---|---|---|
| RL | any | `RL` |
| TBC | > 0 (regular) | `TBC` |
| TBC | > 0 (special) | (other branch — see code) |
| TBC | = 0 | `MIX` |
| TBL | > 0 | `TBL` (or branch) |
| TBL | = 0 | `TBL` |
| else | — | `ISNULL(OPT_TYPE, 'MIX')` |

Also: `TBL_LISTED_DATE = GETDATE()` when `OPT_TYPE='TBL'` AND `ALLOC_QTY > 0`.

---

## 6. Allocation rules (per-OPT engine)

**Source:** [backend/app/services/rule_engine_per_opt.py](../backend/app/services/rule_engine_per_opt.py), [backend/app/services/rule_engine_new.py](../backend/app/services/rule_engine_new.py), [backend/app/services/rule_engine_pandas.py](../backend/app/services/rule_engine_pandas.py)

### 6.1 Mode selection

| `allocation_mode` | What runs |
|---|---|
| `pandas` (default) | Vectorized cumulative-window race |
| `per_opt` | One-OPT-at-a-time sequential engine; flips `ARS_PER_OPT_MODE=1` |
| `sequential` | Single-thread SQL fallback |

`exec_order = opt_type_first` (default) = RL all rounds → TBC all rounds → TBL all rounds. `round_first` (R1 across all → R2 …) is configured but wiring still maps to `opt_type_first` today — **gap flagged below**.

### 6.2 Per-size dispatch formula (RL / TBC)

For each size of an `OPT_TYPE in ('RL','TBC')`, each round `r`:

```
need_ship   = max(r × SZ_MBQ − SZ_STK − SHIP_QTY_so_far, 0)
need_pool   = r × SZ_MBQ − SZ_STK − POOL_CONSUMED       (need_pool ← 0 when need_ship = 0)

# Source priority: HOLD first, then MSA pool
from_hold   = min(opt_need, RL_HOLD_QTY_remaining)      # per (WERKS, VAR_ART, SZ)
opt_need   -= from_hold
take_pool   = min(opt_need, live_pool)

# Per-size ship ceiling (commits 2a8c87b + 58ef3dc, June 2026)
ship_ceiling   = ceil(need_ship / pak) × pak
raw_ship       = min(take_pool + from_hold, ship_ceiling)
effective_ship = floor(raw_ship / pak) × pak    (or raw_ship when combined_supply = pak-multiple)
pool_used      = max(effective_ship − from_hold, 0)
# Excess pool is refunded to pool_dict
```

**Cap guarantee:** `Σ SHIP per size ≤ I_ROD × SZ_MBQ − SZ_STK` (plus up-to `pak−1` overshoot from pak rounding).

### 6.3 RL_HOLD_QTY semantics

`RL_HOLD_QTY` is **not** in-transit stock. It is store-specific stock **reserved/earmarked at the RDC** for a `(WERKS, VAR_ART, SZ)`, sourced from `ARS_NL_TBL_HOLD_TRACKING.HOLD_REM` where `IS_CLOSED=0 AND HOLD_REM>0`. The engine consumes from the reservation **first**, then taps the MSA pool — total ship per size is capped by the ship ceiling.

### 6.4 TBL targeting

TBL ship target =

```
SZ_MBQ_WH + (I_ROD − 1) × SZ_MBQ − SZ_STK     (hold counted once)
```

TBL SHIP and HOLD are pak-aligned **independently** (commit `58ef3dc`).

### 6.5 `FNL_Q_REM` semantics (per-OPT mode)

`FNL_Q_REM` on alloc rows is the **live pool AFTER that OPT's draw**. Pre-band snapshots in `rule_engine_pandas` and post-loop SQL recompute are gated off when per-OPT mode is on.

Audit pattern — combine `FNL_Q_REM` with `ALLOC_REMARKS`:

| `FNL_Q_REM` | `ALLOC_REMARKS` excerpt | Diagnosis |
|---|---|---|
| `0` | `PAK_SZ_ROUND(..., short=stock=N)` | Pool exhausted |
| `> 0` | `PAK_SZ_GATE(req=R, pak=P)` | Pak rule fired (stock available but not pak-aligned) |
| `> 0` | `from_hold=N` in round 2+ | Reservation replay (correct on RL/TBC) |

### 6.6 Revalidation invariants

- `SKIP_PRI_BROKEN` revalidation fires when an OPT lands without satisfying the active primary-grid gate
- Sec-cap dims (§4.2) must still be on every row — revalidation reads them
- `ALLOC_REMARKS` is the audit trail; never overwrite without preserving prior reasons

---

## 7. Hold / Pending Allocation rules

### 7.1 Lifecycles

```
PEND:   queued → approved → dispatched | cancelled (reverted)
HOLD:   active → parked (with reason) → unparked (back to active) | finalized
```

`parked_history` is the source of truth for HOLD transitions — every state change writes a row.

### 7.2 Revert correctness

A reverted allocation must restore `PEND` to its pre-allocation value at the OPT grain. Recent commits flagging this is active correctness area: `fecb6f9`, `3c7693b`, `c06d051` — read git log before editing.

### 7.3 Hold unpark must preserve OPT identity

A parked OPT must not re-enter active flow with a different `OPT_TYPE` (invariant 1) without an explicit transition row. Unparked rows must carry `FAB / MACRO_MVGR / MICRO_MVGR / M_VND_CD / RNG_SEG` back to alloc (invariant 4).

### 7.4 Known correctness risks (current behavior — see § 8 for fixes to consider)

| Risk | Where | Effect |
|---|---|---|
| `ARS_PEND_ALC` has only PK on `ID` — no unique on `(SESSION_ID, RDC, ST_CD, ARTICLE_NUMBER, ALLOC_MODE)` | `pend_alc_service.py:16` (docstring claims grain but `write_manual_pend_alc` doesn't enforce) | Duplicate manual rows silently double-count after `apply_pend_alc_delta` aggregates |
| Manual upload has no required Pydantic validation | `pend_alc.py:2222` | Blank `rdc` / orphan articles get inserted → MSA delta matches 0 rows → MSA stays stale until next bootstrap |
| `apply_adhoc_close` does **not** sync MSA (no `apply_pend_alc_delta(-1)`, no `bootstrap_msa_pend_sync`) | `pend_alc_service.py:2878` | `MSA_TOTAL.PEND_QTY` stays inflated until next full Generate; listings see lower `FNL_Q` than reality |
| Adhoc close with blank `ST_CD` is "any-store" wildcard | `pend_alc_service.py:2961` | Empty Excel `ST_CD` column closes every open row for `(RDC, ARTICLE)` across all stores |
| `apply_do_deductions` FIFO partition does **not** include `ALLOC_MODE` | `pend_alc_service.py:3371,3376` | RL DO can settle a TBC allocation (and vice versa) — cross-attribution risk |
| `update_bdc_history_with_do` `do_qty=0` cancellation has wildcard scope | `pend_alc_service.py:2772-2774` | Cancel with only `(rdc, art)` + `do_qty=0` flips every OPEN BDC for that pair |
| `_check_adhoc_close_revert` gates on `LAST_DO_AT/LAST_BDC_AT` but `apply_adhoc_close` doesn't touch them | `pend_alc_service.py:3011` | Revert always passes; if BDC was re-generated, two OPEN history rows can co-exist for the same `(RDC, ST_CD, ART)` |

---

## 8. Reviewer's checklist (run this when auditing a session)

Walk these in order — failure at step `N` makes step `N+1` meaningless.

### 8.1 MSA layer (read [§ 3.3](#33-reconciliation-guarantees-must-all-hold-after-every-generate))
- [ ] **R1** `Σ TOTAL.PEND == Σ PEND_ALC (open)`?
- [ ] **R2** `Σ TOTAL.HOLD == Σ HOLD_TRACKING (open)` (with WERKS→RDC)?
- [ ] **R3** `Σ TOTAL.STK == Σ stock view` for run date + selected SLOCs + APP/GM?
- [ ] **R4** Per group, VAR_ART article-count = TOTAL article-count?
- [ ] **R5** Per `(RDC, MAJ_CAT, GEN_ART, CLR)`, `GEN_ART.X = Σ VAR_ART.X` for X ∈ {STK, PEND, HOLD, FNL_Q}?

### 8.2 Grid layer
- [ ] Does every grid row carry `FAB`, `MACRO_MVGR`, `MICRO_MVGR`, `M_VND_CD`, `RNG_SEG`?
- [ ] Is growth % applied at `MAJ_CAT × grid`, not at OPT_TYPE level?
- [ ] For each grid row with `MBQ = 0`, was the 1.30× breach skipped?
- [ ] Was Run-All followed by `bootstrap_msa_pend_sync`?

### 8.3 Listing layer
- [ ] Does each row have exactly one `OPT_TYPE` after Part 3.6?
- [ ] For each row classified `MIX`, does it match a branch in § 5.1?
- [ ] Did `MIX` aggregation produce expected granularity per `mix_mode`?
- [ ] Per `(WERKS, MAJ_CAT)`, is `Σ SHIP_QTY` per OPT_TYPE ≤ `cap_pct × MJ_REQ` (§ 5.5)?
- [ ] Per OPT_TYPE, is `Σ SHIP_QTY` ≤ `mbq_cap_pct × MJ_MBQ_ORIG` (§ 5.6)?
- [ ] `HOLD_DAYS` extension applied only to TBL rows?

### 8.4 Allocation layer
- [ ] Per OPT, per size: `SHIP_QTY ≤ I_ROD × SZ_MBQ − SZ_STK + (pak−1)`?
- [ ] RL/TBC rows with `RL_HOLD_QTY > 0`: was hold consumed first, then pool?
- [ ] `FNL_Q_REM` consistent with `ALLOC_REMARKS` (§ 6.5 table)?
- [ ] `OPT_STATUS` per § 5.10?
- [ ] `TBL_LISTED_DATE` populated for shipped TBL rows?

### 8.5 Hold / Pend layer
- [ ] Every `parked_history` transition has a matching row?
- [ ] No reverted allocation left stale `PEND_QTY` in MSA?
- [ ] No adhoc-close run since last MSA Generate? (if yes, suspect inflated PEND — § 7.4)

---

## 9. Suggested rules / gaps spotted during this review

These are points where the implementation, comments, and recorded rules disagree — bring to the next review meeting.

### 9.1 Listing Part 3.6 — MIX(b) "poor color fill" branch contradicts the simplification target

**Observation:** [listing.py:1387-1390](../backend/app/api/v1/endpoints/listing.py#L1387-L1390) still classifies as `MIX` based on `VAR_FNL_COUNT / VAR_COUNT < threshold` *even when `MSA_FNL_Q > 0`*. The intent (clarified 2026-06-23) is that MIX is a **supply-only** condition (`MSA_FNL_Q = 0` AND `RL_HOLD_QTY = 0`); poor color fill should be a quality flag, not an `OPT_TYPE`.

**Symptom in production:** a Listing run produced 397 aggregate MIX lines, 396 of which had live MSA or hold supply, because MIX(b) was tagging supplied OPTs as MIX on color-fill alone. Those OPTs were then short-circuited out of the per-OPT engine and never shipped.

**Suggested rule:** split MIX(b) into a separate `POOR_COLOR_FILL` flag column. Keep the OPT in RL/TBC/TBL based on stock, but surface the fill quality for review. The current MIX(b) silently drops sellable inventory.

### 9.2 Per-OPT `exec_order=round_first` is configured but not wired

**Observation:** [listing.py:142-144](../backend/app/api/v1/endpoints/listing.py#L142-L144) — `exec_order` accepts `round_first` (R1 across all OPT_TYPEs → R2 → …), advertised as "fairer to TBL", but the engine maps both options to `opt_type_first`.

**Suggested rule:** either wire `round_first` end-to-end or drop the option from the UI / config. Today it silently no-ops.

### 9.3 `apply_adhoc_close` doesn't sync MSA — silent data drift

**Observation:** § 7.4 — `apply_adhoc_close` closes PEND rows but does not invoke `apply_pend_alc_delta(sign=-1)` or `bootstrap_msa_pend_sync`. Listings between an adhoc close and the next MSA Generate see lower `FNL_Q` than reality.

**Suggested rule:** make `apply_adhoc_close` end with a session-scoped `apply_pend_alc_delta(sign=-1)` (consistent with manual / approve / revert paths). Or, if intentional, gate listings from running between adhoc close and next MSA Generate.

### 9.4 Adhoc close + DO cancel — wildcard `ST_CD` semantics are unsafe defaults

**Observation:** § 7.4 — blank `ST_CD` in adhoc close = "any store"; `do_qty=0` with only `(rdc, art)` = "every open BDC". Copy-paste from Excel commonly leaves these blank.

**Suggested rule:** require explicit `ALL_STORES=true` in the payload to invoke the wildcard. Default blank `ST_CD` to a rejection with a confirmation prompt.

### 9.5 `ARS_PEND_ALC` grain claim vs reality

**Observation:** § 7.4 — the docstring claims `(SESSION_ID, RDC, ST_CD, ARTICLE_NUMBER, ALLOC_MODE)` is the grain, but only the PK exists. `write_manual_pend_alc` doesn't enforce it.

**Suggested rule:** either add a unique index that matches the documented grain, or update the docstring. The current mismatch enables duplicate-row double-counting.

### 9.6 DO FIFO partition ignores `ALLOC_MODE`

**Observation:** § 7.4 — `apply_do_deductions` partitions FIFO by `(RDC, ST_CD, ART)` without `ALLOC_MODE`. An RL DO can consume a TBC PEND row and vice versa.

**Suggested rule:** include `ALLOC_MODE` in the FIFO partition unless there is a stated reason for cross-attribution. If cross-attribution is intentional, document it as a rule and surface it in reporting (RL vs TBC "actual mode" columns).

### 9.7 `msa_result_storage.py` docstring is wrong about DB target

**Observation:** [.claude/agents/ars_flow_kb/msa.md:17](../.claude/agents/ars_flow_kb/msa.md) — `msa_result_storage.py` says "Main DB session, not Data DB" but all four MSA output tables live in the Data DB.

**Suggested rule:** fix the docstring. Cheap, but prevents the next engineer from chasing a non-bug.

### 9.8 `MATNR` / `QTY` legacy columns on `ARS_PEND_ALC`

**Observation:** § "Recorded rules" 2026-06-13 — `ARS_PEND_ALC` carries `MATNR` (bigint) and `QTY` (int) that are never written or read. Survived a schema migration.

**Suggested rule:** drop them in a maintenance window (one ALTER TABLE per column with prior backup). Until then, document them on the schema page as "unused — do not select".

---

## 10. Change log for this document

| Date | Change | By |
|---|---|---|
| 2026-06-26 | Initial consolidated rule master created | Santosh (drafted via Claude) |

> **How to extend this doc:** add new rules as a sub-bullet under the matching section. For module-specific deep dives, also append to the relevant `.claude/agents/ars_flow_kb/<area>.md` file with a dated bullet. The ars_flow agent reads those KB files on every invocation, so keeping them in sync makes the next debugging session faster.
