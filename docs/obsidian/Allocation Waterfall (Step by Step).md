---
title: Allocation Waterfall (Step by Step)
tags: [ars, rule-engine, waterfall, deep-dive]
updated: 2026-07-17
---

# Allocation Waterfall — Exhaustive Step-by-Step

The complete, ordered walkthrough of Stage C — the per-OPT sequential band. This is the deepest note in the vault; the [[Rule Engine (per_opt)]] note is the overview. Everything here is traced to the actual code in `backend/app/services/`:
- `rule_engine_per_opt.py` — the band (`_run_band_per_opt` + helpers)
- `rule_engine_pandas.py` — orchestration host (`_run_majcat_waterfall`, `run_listing_and_allocation_pandas`)
- `rule_engine_new.py` — shared Stage A/B/D

> per_opt is the ONLY band since 2026-07-10. `allocation_mode != per_opt` → HTTP 400.

## Constants & grains
- `ACS_SKIP_FACTOR=0.5`, `SEC_CAP_DEFAULT_PCT=130.0`, `OPT_TYPE_ORDER=[RL,TBC,TBL]`.
- `POOL_KEYS = [RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ]` — **RDC not WERKS**, so different stores on one RDC compete for the same pool (the core cross-store race).
- `OPT_KEYS` = `[WERKS, GEN_ART_NUMBER, CLR]` in the band; `[WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR]` for revalidation.

---

## 1. Setup before the waterfall

### Stage A — the listing verdict (SQL)
- `_stage_a_add_columns` — idempotent ALTER + reset of engine columns to PENDING/0.
- `_stage_a_apply_rules` — `LISTED_FLAG=1` iff no rule fragment fired. Rules: R01 `LISTING≠1` · R02 `OPT_TYPE=MIX` · R04 `MSA_FNL_Q≤0 AND RL_HOLD_QTY≤0` · R05 `OPT_REQ_WH<1` · R06 `PRI_CT%<100` (enforced types) · R07 TBL var-ratio (`VAR_FNL_COUNT/VAR_COUNT < size_threshold AND VAR_FNL_COUNT < min_size_count`) · **R09 headroom**: `(cap_pct × MJ_MBQ) − MJ_STK_TTL < 0.5 × ISNULL(NULLIF(ACS_D,0),1)` → `R09_HEADROOM_TRIVIAL`. `cap_pct`: RL/TBC from their `mbq_cap_pct÷100`; **TBL forced 1.0** (growth stays MJ+grid).
- `_stage_a_assign_tier` — TIER 1 focus-uncapped / 2 focus-capped / 3 regular.
- `_stage_a_assign_rank` — `ROW_NUMBER() OVER (PARTITION BY WERKS, OPT_TYPE, MAJ_CAT ORDER BY TIER ASC, SEC_CT% DESC, MAX_DAILY_SALE DESC, OPT_REQ_WH DESC, GEN_ART ASC, CLR ASC)`. Per-(store,opt_type,majcat), not global.
- `_stage_a_materialize_listed` → `ARS_LISTED_OPT` (`WHERE LISTED_FLAG=1`); propagates MP grid-extras via `_collect_grid_extra_cols` (invariant 4).

### Stage B — explode OPT → (VAR_ART × SZ) (SQL)
- `_stage_b_explode` — `ARS_LISTED_OPT × ARS_MSA_VAR_ART`. Seeds `FNL_Q = FNL_Q_REM = FNL_Q`; zeroes accumulators. **Fresh/GRT:** joins ONLY the matching typed MSA row (`ISNULL(V.ALLOC_TYPE,'FRESH')=:at`), stamps `ALLOC_TYPE`. Row filters: `FNL_Q>0`, PRI_CT%=100 (enforced types), `MJ_REQ ≥ 0.5 × ISNULL(NULLIF(ACS_D,0),18)` (meaningful-requirement gate).
- `_stage_b_fill_cont` — the [[Contribution and CONT|CONT ladder]].
- `_stage_b_fill_targets` — `SZ_MBQ = ROUND(OPT_MBQ × CONT, 0)` (floor-to-1 when CONT>0 & OPT_MBQ>0 but rounds to 0); `SZ_MBQ_WH` from `OPT_MBQ_WH`; `SZ_REQ`/`SZ_REQ_WH`.

### Load into pandas + per-MAJ_CAT slicing
`_load_tables`: `SELECT *` with deterministic `ORDER BY`; merges `ST_STATUS` + `SEG` for the hold-suppression mask; coerces types; `MJ_REQ_REM` NULL → falls back to `MJ_REQ` (never 0). Slices grouped by MAJ_CAT — disjoint, one worker per MAJ_CAT via `ProcessPoolExecutor`.

### Per-MAJ_CAT state built ONCE
- **`pool_dict`** = `max(FNL_Q)` per POOL_KEY — the live pool, mutated in place across all bands/rounds/OPTs.
- **`sec_cap_state`** (`build_sec_cap_state`) — per grid/grain: `ceilings = MBQ_ORIG × cap_factor`, `budgets = max(0, ceiling − STK_TTL)`, `configured`, `hard_block` reasons, `running` (mutated as OPTs ship), `gh_applies`.
- **`mj_req_rem_dict`** `{WERKS: MJ_REQ_REM}` — re-seeded from live working_df each round.
- **`mbq_budget`** (`_live_mbq_budget`, rebuilt each round): `budget = max(0, MJ_REQ_REM + ((cap_pct−100)/100) × MJ_MBQ_ORIG)`. cap 100 → =MJ_REQ_REM; cap 130 → +30% ORIG; cap≤0 → `{}`.

---

## 2. The three nested loops (`_run_majcat_waterfall`)

**Outer — OPT_TYPE** `[RL, TBC, TBL]`, all rounds each. For TBC/TBL, `_rerank_opt_priority_pandas` re-ranks survivors on **live** SIZE_RATIO (drops low-coverage → `R07_SIZE_RATIO_LIVE`); `_pre_band_check` applies `PRI_CT_REM<100` + store-broken (`MJ_REQ_REM < 0.5×ACS_D`) before round 1.

**Middle — round `r`** in `1..max(I_ROD)`: reset `ROUND_SHIP/HOLD=0`; `elig_mask = ot_mask & I_ROD≥r`; rebuild `mbq_budget` + re-seed `mj_req_rem_dict` from live values; run band; then `_revalidate_after_band`. Round N finishes for ALL stores before round N+1.

**Inner — OPT order within the band**: stable **mergesort** on `OPT_PRIORITY_RANK ASC, ST_RANK ASC, WERKS ASC, GEN_ART ASC, CLR ASC, SZ ASC`, then `groupby([WERKS,GEN_ART,CLR], sort=False)`. One group = one OPT.

> **Why fixed order matters:** allocation is a sequential race on the shared, mutating `pool_dict` (+ MJ_REQ + sec-cap running). The winner of any contested pool key is whichever OPT the sort visits first. Fixed sort + mergesort + deterministic SQL load `ORDER BY` = two runs on identical input produce identical ships.

---

## 3. One OPT — gate by gate, in exact execution order

For group `(werks, gen_art, clr)` → `opt_rows`:

| # | Gate | Trigger / condition | Pass | Fail |
|---|------|---------------------|------|------|
| **G0** | Empty / zero-demand short-circuit | `(need_pool≤0).all() AND (need_ship≤0).all()` | proceed | `continue` (no state touched) |
| **G1** | Read LIVE pool | — | `live_pool[i] = pool_dict.get(key,0)` | — |
| **G2** | R07 live size-ratio [TBL] | `sizes_with_pool < min_size_count OR ratio < size_threshold` (strict OR) | proceed | `R07_SIZE_RATIO_LIVE`; pool untouched; `continue` |
| **G3** | TBL MJ_REQ gate [TBL] | `OPT_MBQ>0 AND (tbl_mj_req_cap_pct/100)×MJ_REQ_REM[WERKS] < 0.5×OPT_MBQ` | proceed | `TBL_MJ_REQ_GATE_FAIL`; pool untouched; `continue` |
| **G4** | opt_need + PAK_SZ round | half-up `floor((raw+0.5×pak)/pak)×pak`; RL/TBC gated when `raw < 0.5×pak`; TBL builds `want_ship_pak + want_hold_pak` | `total_need` computed | rows gated to 0 |
| **G5** | Budget admission | TBL: `intended_tbl > mj_rem` → overshoot vs `0.5×intended`. RL/TBC COMPLETE: `total_need > werks_cap` → admit if `werks_cap ≥ 0.5×total_need`. SCALED: proportional round-then-shave | admit (may stamp `MBQ_CAP_OVERSHOOT`/`MBQ_CAP_SCALE`) | `MBQ_CAP_TBL`/`MBQ_CAP_RL`/`MBQ_CAP_TBC`; pool untouched; `continue` |
| **G6** | Sec-cap pre-gate (two-pass) | Pass 1 veto scan (any applicable grid hard-block) → block; Pass 2 Primary-first breach/overshoot | admit / override (stash) | block → `PRIMARY_CAP_PRE_<g>`/`SEC_CAP_PRE_<g>`; pool untouched; `continue` |
| **G7** | Hold-first draw [RL/TBC] | per size `from_hold = min(opt_need, pak-aligned hold_rem)`; `hold_dict[hk] -= take` | `opt_need -= from_hold` | — |
| **G8** | Pool draw + live decrement | `take_pool = min(opt_need, live_pool)` (TBL: SHIP-first then HOLD-from-remaining); `pool_dict[k] = max(0, − take_pool)` | — | — |
| **G9** | FNL_Q_REM write | authoritative `post_draw_pool` → `FNL_Q_REM` | — | — |
| **G10** | SHIP/HOLD split + ceiling/refund | RL/TBC ship-ceiling snaps down non-pak sliver, refunds `excess_pool` to `pool_dict` | — | — |
| **G11** | Advance sec-cap `running` | `actual_moved = round_ship.sum()` (MBQ-only, TBL hold excluded) → `running[g][grain] += actual_moved` for participating grids | — | skipped OPTs have empty `participating` → no advance |
| **G12** | Decrement mbq_budget | `mbq_budget[WERKS] -= consumed` (**no floor** → can go negative) | — | — |
| **G13** | Decrement mj_req_rem [TBL] | `mj_req_rem_dict[WERKS] -= round_ship.sum()` (**no floor**) | — | — |
| **G14** | Write-back | `POOL_CONSUMED += take_pool`, `SHIP_QTY += round_ship`, `HOLD_QTY += round_hold`, `FROM_HOLD_QTY += from_hold`, `ALLOC_WAVE="{ot}_R{r}"`; status from `target = max(I_ROD×SZ_MBQ − SZ_STK, 0)` | — | — |
| **G15** | Per-size remarks | `B[{ot}.r{r}.rk{k}] ship=… hold=… …`; `POOL_EMPTY(...)`; PAK markers | — | — |
| **G16** | Sec-cap override narrative | append `SEC_CAP_OVERSHOOT(…)` if admitted via override | — | — |

**Key asymmetry:** a gate that fails at G2/G3/G5/G6 does `continue` **before touching the pool** — the units stay live for lower-priority OPTs. That is the whole point of pre-validating every gate before drawing.

---

## 4. Per-size dispatch math

`need_ship = max(r×SZ_MBQ − SZ_STK − SHIP_QTY, 0)` (both types).

**RL / TBC:**
```
need_pool     = max(r×SZ_MBQ − SZ_STK − POOL_CONSUMED, 0)
gated         = need_pool < 0.5×pak
opt_need      = where(gated, 0, floor((need_pool + 0.5×pak)/pak)×pak)      # half-up
from_hold     = min(opt_need, pak-aligned hold_rem) ; opt_need -= from_hold
take_pool     = min(opt_need, live_pool)
ship_ceiling  = ceil(need_ship/pak)×pak                                    # may overshoot need_ship by up to pak−1
raw_ship      = min(take_pool + from_hold, ship_ceiling)
supply<pak    = (live_pool + from_hold) < pak
effective_ship= where(supply<pak, raw_ship, floor(raw_ship/pak)×pak)       # last sliver may ship non-pak
pool_used     = max(effective_ship − from_hold, 0)
excess_pool   = take_pool − pool_used                                       # refunded to pool_dict
round_ship    = effective_ship ; round_hold = 0
```

**TBL:**
```
tbl_cum       = SZ_MBQ_WH + (r−1)×SZ_MBQ
need_pool     = where(need_ship==0, 0, max(tbl_cum − SZ_STK − POOL_CONSUMED, 0))
want_ship_pak = where(need_ship < 0.5×pak, 0, floor((need_ship+0.5×pak)/pak)×pak)
hold_basis    = max(need_pool − want_ship_pak, 0)          # then hold-suppression zeroes suppressed rows
want_hold_pak = where((want_ship_pak==0)|(hold_basis<0.5×pak), 0, floor((hold_basis+0.5×pak)/pak)×pak)
opt_need      = want_ship_pak + want_hold_pak
# draw:
round_ship    = min(want_ship_pak, live_pool)
pool_after    = max(live_pool − round_ship, 0)
full_pak_ok   = (pool_after ≥ want_hold_pak) & (want_hold_pak > 0)
sliver_loose  = (0 < pool_after < pak) & (want_hold_pak > 0)
round_hold    = where(round_ship==0, 0, where(full_pak_ok, want_hold_pak, where(sliver_loose, pool_after, 0)))
take_pool     = round_ship + round_hold
```
OPT decision = sums: `total_need = opt_need.sum()`, `intended_ship_sc = want_ship_pak.sum()` (TBL) / `opt_need.sum()` (RL/TBC).

**SHIP and HOLD are pak-aligned INDEPENDENTLY** for TBL (each has its own `0.5×pak` half-up gate). Cap arithmetic (G11/G13) is **MBQ-only** — `round_hold` (WH buffer) never counts against sec-cap running or MJ_REQ.

---

## 5. State that carries between OPTs / rounds

| State | Mutated at | Effect on later OPTs |
|-------|-----------|----------------------|
| `pool_dict` (live FNL_Q) | G8 draw, G10 refund | shared per RDC — lower ST_RANK draws first; later store sees reduced pool → PARTIAL / `POOL_EMPTY` |
| `mj_req_rem_dict[WERKS]` | G13, no floor | **overshoot-then-fail**: a boundary TBL admits at `overshoot==threshold`, drives remainder negative → next TBL OPT in that WERKS fails G5 `MBQ_CAP_TBL` |
| sec-cap `running[grid][grain]` | G11 (participating only) | next OPT at the same grain: bigger `run_before` → breach more likely → override once (`overshoot≤0.5×intended`) then hard-block |
| `mbq_budget[WERKS]` (RL/TBC) | G12, no floor | COMPLETE overshoot → negative → later same-WERKS OPTs hit `werks_cap≤0` skip |

Between rounds, `_revalidate_after_band` decrements working `MSA_FNL_Q_REM` + grid `REQ_REM`, recomputes `H_*_REM`/`PRI_CT_REM`, and marks next-band OPTs `SKIP_MSA_EXHAUSTED` / `SKIP_MJ_EXHAUSTED` / `SKIP_PRI_BROKEN` / `SKIP_STORE_BROKEN` (scoped to `I_ROD ≥ r+1`).

---

## 6. TBL specifics
- TBL **creates** hold (`round_hold` = the `SZ_MBQ_WH` buffer excess on first dispatch); RL/TBC **consume** warehouse hold (`from_hold`, G7). TBL never reads `hold_dict`.
- Round-1 pool target is `SZ_MBQ_WH + (r−1)×SZ_MBQ` — round 1 pulls the buffer, later rounds add one MBQ each.
- **Hold-suppression mask** (`_hold_suppress_arr`): `suppress = (skip_hold_upc AND ST_STATUS=='UPC') OR (¬apply_hold_seg_app AND SEG=='APP') OR (¬apply_hold_seg_gm AND SEG=='GM')`. Applied to `hold_basis` **before** pak-align, so suppressed units stay in the live pool. See [[Fresh-GRT Allocation]].

---

## 7. Stage D (finalize, SQL)
1. `_stage_d_apply_pak_sz_rounding` — SQL safety-net half-up round, **skips rows already carrying `PAK_SZ_` in ALLOC_REMARKS** (i.e. all per_opt rows). `req<0.5×pak` → SKIPPED `PAK_SZ_BELOW_HALF`.
2. `_stage_c_apply_opt_mj_req_gate(skip_tbl_branch=True)` — TBL already gated pre-alloc; RL/TBC not gated. **Never set `tbl_cap_pct=0`** (would zero all TBL).
3. Safety-net zeroing (HOLD=0 where SHIP=0 & SKIPPED; POOL_CONSUMED=0 where SHIP=HOLD=0). **No FNL_Q_REM recompute** (per_opt's G9 value is authoritative).
4. `ALLOC_QTY = SHIP_QTY`.
5. Final CASE: `ALLOC_STATUS` ALLOCATED (`SHIP+HOLD ≥ target`) / PARTIAL / SKIPPED; `SKIP_REASON` preserves any pre-stamped reason, else `ALREADY_STOCKED` (target≤0) / `NO_REQ` (SZ_REQ≤0) / `NO_POOL_MSA`.
6. Sec-cap post-pass SQL gate **skipped** when per_opt already ran (safety-net for spec-build failure only).
7. `*_REQ_REM` refund — recompute grid `REQ_REM = max(REQ − Σ SHIP, 0)`, `H_*_REM`, `PRI_CT_REM`.
8. `_classify_alloc_reason` — **no-op stub** (prevents a session-total-nuking AttributeError).
9. `_stage_d_reflect` — roll up to `ARS_LISTED_OPT`/working at OPT grain; `ALLOC_SEQ` via `ROW_NUMBER()`; append `ship=…; hold=…; sizes=x/y; seq=n`.

---

## 8. SKIP_REASON / ALLOC_REMARKS taxonomy (complete)
| Value | Meaning | Gate |
|-------|---------|------|
| `R07_SIZE_RATIO_LIVE` | TBL live size coverage below thr/min | G2 / rerank |
| `TBL_MJ_REQ_GATE_FAIL` | `cap%×MJ_REQ_REM < 0.5×OPT_MBQ` | G3 |
| `MBQ_CAP_TBL` | TBL overshoot `> 0.5×intended` | G5 |
| `MBQ_CAP_RL` / `MBQ_CAP_TBC` | cap_rem=0, or COMPLETE overshoot >0.5×need | G5 |
| `MBQ_CAP_OVERSHOOT(…)` / `MBQ_CAP_SCALE(…)` | admit-with-overshoot / scaled-partial (remark only) | G5 |
| `PRIMARY_CAP_PRE_<g>` / `SEC_CAP_PRE_<g>(cap=…%[,cont=…%])` | grid block | G6 |
| `SEC_CAP_GRID_NULL[g]` / `SEC_CAP_MBQ_ZERO[g]` / `SEC_CAP_NULL[g]` | hard-block reasons (comma-joined) | G6 |
| `SEC_CAP_MATRIX_GAP[g]` | matrix on, no band matched | G6 |
| `SEC_CAP_OVERSHOOT(…)` | admitted into capped grain (remark only) | G16 |
| `POOL_EMPTY(FNL_Q=…,live_pool=0,consumed_prior=…)` | size SHIP=0, no live pool | G15 |
| `B[{ot}.r{r}.rk{k}] ship=… hold=… [from_hold=…] [partial(…)]` | per-size ship trace | G15 |
| `PAK_SZ_GATE/ROUND/SHIP/HOLD(…)` | pak markers (RL/TBC ceiling; TBL independent) | G15 |
| `PAK_SZ_BELOW_HALF(pak=…)` | Stage D gated to 0 | Stage D |
| `SKIP_PRI_BROKEN` / `SKIP_STORE_BROKEN` | pre-band gate | `_pre_band_check` |
| `SKIP_MSA_EXHAUSTED` / `SKIP_MJ_EXHAUSTED` / `SKIP_PRI_BROKEN` / `SKIP_STORE_BROKEN` | post-band revalidate | `_revalidate_after_band` |
| `R09_HEADROOM_TRIVIAL` | headroom below `0.5×ACS_D` | Stage A / re-eval |
| `ALREADY_STOCKED` / `NO_REQ` / `NO_POOL_MSA` | finalize catch-alls | Stage D |

`_mark_opt_skip` sets SKIPPED, **appends** ` reason(remark);`, writes SKIP_REASON only if empty, snapshots live_pool to FNL_Q_REM. `_mark_opt_skip_sec_cap` **replaces** ALLOC_REMARKS (pool never touched).

---

## 9. Worked example

**MAJ_CAT `MSHIRT`, RDC `R1`.** Sec-cap not configured. `mbq_gate_factor=0.5`, RL cap 100% COMPLETE, `tbl_mj_req_cap_pct=100`.

### Part A — RL band, cross-store pool race
One RL OPT (GEN_ART 1001 / CLR BLU / VAR_ART V1), sizes S,M,L, `PAK_SZ=1`, `SZ_STK=0`, `I_ROD=1`. Both stores list it, both `OPT_PRIORITY_RANK=1`. Pool `FNL_Q`: S=10, M=10, L=10.

| store | ST_RANK | SZ_MBQ (S,M,L) | MJ_REQ_REM | mbq_budget |
|-------|--------|----------------|-----------|-----------|
| S1 | 1 | 4,6,4 | 14 | 14 |
| S2 | 2 | 4,6,4 | 14 | 14 |

- **S1 (ST_RANK 1):** live_pool [10,10,10]. total_need 14 ≤ 14 → full. `take_pool=[4,6,4]`. **Pool → S=6,M=4,L=6.** SHIP [4,6,4] = target → **ALLOCATED**. mbq_budget[S1]=0. FNL_Q_REM=[6,4,6].
- **S2 (ST_RANK 2):** live_pool now **[6,4,6]**. `take_pool = min([4,6,4],[6,4,6]) = [4,4,4]` (M capped by pool). **Pool → S=2,M=0,L=2.** SHIP [4,4,4] vs target [4,6,4] → S/L ALLOCATED, **M PARTIAL** (`B[RL.r1.rk1] ship=4 hold=0 partial(need=6,pool=4)`).

> Identical demand, but S2's M is short purely because S1 drew the shared RDC pool first.

### Part B — TBL band, R07 + gate + pak + 2 rounds + hold
One TBL OPT (store S1, GEN_ART 1003 / CLR GRN / VAR_ART V3), sizes S,M,L,XL, `PAK_SZ=2`, `SZ_STK=0`, `I_ROD=2`, `OPT_MBQ=10`. `SZ_MBQ=[2,3,3,2]`, `SZ_MBQ_WH=[3,4,4,3]`. Pool: S=8,M=8,L=8,**XL=0**. `MJ_REQ_REM(S1)=30`.

- **G2 R07:** 3/4 = 0.75; 3≥3 and 0.75≥0.6 → **pass**. **G3:** `1.0×30=30 ≥ 0.5×10=5` → **pass**.
- **Round 1:** need_pool=[3,4,4,3]. want_ship_pak=[2,4,4,2] (M/L 3→4). hold_basis=[1,0,0,1] → want_hold_pak=[2,0,0,2]. intended_tbl=12 ≤ 30 → full. round_ship=min([2,4,4,2],[8,8,8,0])=[2,4,4,**0**]. pool_after=[6,4,4,0]; full_pak_ok=[T,F,F,F] → round_hold=[2,0,0,0]. **Pool → S=4,M=4,L=4,XL=0.** SHIP=[2,4,4,0] HOLD=[2,0,0,0] → all PARTIAL. mj_req_rem → 30−10=**20**.
- **Round 2:** need_ship=[2,2,2,4]; tbl_cum=[5,7,7,5]−consumed[4,4,4,0] → need_pool=[1,3,3,5]. want_ship_pak=[2,2,2,4]; want_hold_pak=[0,2,2,2]; intended_tbl=10 ≤ 20 → full. round_ship=min([2,2,2,4],[4,4,4,0])=[2,2,2,**0**]. pool_after=[2,2,2,0]; full_pak_ok=[F,T,T,F] → round_hold=[0,2,2,0]. **Pool → S=2,M=0,L=0,XL=0.** SHIP cum=[4,6,6,0] vs target [4,6,6,4] → S/M/L **ALLOCATED**, **XL PARTIAL** (`POOL_EMPTY(...)`). mj_req_rem → 20−6=**14**.

> S/M/L fully replenished + WH hold buffer; XL never ships (pool was 0 all along). No overshoot fired because MJ_REQ_REM stayed ample; had `intended_tbl` exceeded MJ_REQ_REM by more than half, G5 would stamp `MBQ_CAP_TBL` and leave the pool for later OPTs.

---

## Cross-links
[[Rule Engine (per_opt)]] (overview) · [[Secondary-Grid Cap]] (G6 detail) · [[Contribution and CONT]] (Stage B CONT) · [[Listing]] (Stage A inputs) · [[MSA Stock Calculation]] (pool source) · [[Fresh-GRT Allocation]] (typed pool + hold suppression) · [[Known Risks and Doc Drift]].
