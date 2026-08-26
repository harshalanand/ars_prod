# ARS V2 — Old vs New Implementation Comparison

**Old (local / `main` · `ARS_NEW` lineage)** — `e0b1f01`, 22-Jun-2026
**New (`ars_v2_prod`)** — `9bbb97a`, 04-Aug-2026

Generated 26-Aug-2026.

---

## §0 — Scope & method

### The branch situation

`ars_v2` and `ars_v2_prod` point at the **same tree**:

```
$ git rev-parse origin/ars_v2^{tree} origin/ars_v2_prod^{tree}
190f5725daa7764d121424d8a674917caf3b001c
190f5725daa7764d121424d8a674917caf3b001c
```

`ars_v2_prod` is `ars_v2` plus four merge commits that change no files. Diffing
that pair produces nothing. The comparison with real content is between the
**`main` / `ARS_NEW` lineage** and **`ars_v2_prod`**.

### The two lineages

Both descend from `ab3d085` ("update code 6-19", 19-Jun-2026) and have run in
parallel since:

```
$ git merge-base HEAD origin/ars_v2_prod
ab3d085870d18aa0e5ddb0d2831dd1966381e1c4

$ git rev-list --left-right --count HEAD...origin/ars_v2_prod
4       16
```

| | Old (local) | New (`ars_v2_prod`) |
|---|---|---|
| HEAD | `e0b1f01` | `9bbb97a` |
| Date | 22-Jun-2026 | 04-Aug-2026 |
| Commits since fork | 4 | 16 |

### Size of the change

```
$ git diff --stat HEAD origin/ars_v2_prod
327 files changed, 55481 insertions(+), 10593 deletions(-)
```

| Change type | Count |
|---|---|
| Added | 233 |
| Modified | 73 |
| Deleted | 20 |
| Renamed | 1 |

Roughly 10,900 of those inserted lines are vendored Obsidian theme CSS
(`docs/obsidian/.obsidian/themes/*/theme.css`) — third-party assets, not project
code.

### Reproducing this

```bash
git fetch origin ars_v2_prod:refs/remotes/origin/ars_v2_prod
git fetch origin ars_v2:refs/remotes/origin/ars_v2

git diff --stat  HEAD origin/ars_v2_prod
git diff --name-status HEAD origin/ars_v2_prod
git log --oneline ab3d085..origin/ars_v2_prod
git show origin/ars_v2_prod:<path>          # read a file on the new branch
```

---

## §1 — Executive summary

1. **The rule engine went from four engines to one.** The runtime
   `allocation_mode` selector (`pandas` / `per_opt` / `sequential`) is gone;
   `per_opt` is hard-pinned and any other value is rejected with a 400. Five files
   were deleted. The trigger was a reproducible divergence — identical inputs
   produced pandas ALLOC 7,259 vs per_opt 9,824 for `M_TEES_HS` — compounded by
   `/listing/retry-failed` silently inheriting whatever `ARS_PER_OPT_MODE` the last
   `/generate` left in the process environment.

2. **Secondary-grid cap was rewritten.** Primary grids now always participate
   regardless of `sec_cap_applicable`; the MJ_REQ_REM override became a unified
   overshoot rule; empty or zero grid data now hard-blocks instead of meaning "no
   constraint"; a two-pass veto-beats-override precedence was added; and an opt-in
   cont%-banded growth matrix can replace the flat cap.

3. **Fresh / GRT typed pools run end-to-end.** `alloc_type` is now a required field
   on `/listing/generate`, MSA emits one row per pool type, and `ALLOC_TYPE` flows
   through pend, hold, DO deduction and the hold dashboard.

4. **Six new backend subsystems** — FA & CONS, SAP integration, Snowflake config,
   Report Generation hub, UPC Store Tracking, Dev Sync — plus six smaller ones.
   11 new routers, 11 new SQL migrations, 26 new frontend pages, 2 new background
   schedulers.

5. **Dispatch behaviour changed materially.** RL/TBC moved from `SCALED` to
   `COMPLETE`-with-overshoot (SCALED is now unreachable), TBL SHIP and HOLD are
   pak-rounded independently, post-pass PAK rounding was retired because it shipped
   stock that did not exist, and `OPT_TYPE` / `OPT_STATUS` gained new states.

6. **Documentation was replaced wholesale.** `ProcessPage` and the 13
   `/docs/process/*.md` engine deep-dives were retired in favour of a
   screenshot-driven Training Manual, an Obsidian vault, and repo-side BRD /
   rule-master documents.

**Carrying the largest operational risk:** four commits of local work never merged
forward (§9.1), and four new modules ship with no authorization gate at all (§9.3).

---

## §2 — Commit timeline since the fork

Common ancestor: `ab3d085` — "update code 6-19" (19-Jun-2026).

### Old branch (local) — 4 commits

| Commit | Date | Author | Subject |
|---|---|---|---|
| `453476f` | 20-Jun | santosh kumar | export bdc schedular and hold from consumed |
| `379f424` | 20-Jun | HARSHAL ANAND | Merge PR #26 from `ARS_NEW` |
| `3c0329a` | 22-Jun | santosh kumar | merge art, pack multiple etc |
| `e0b1f01` | 22-Jun | HARSHAL ANAND | Merge PR #27 from `ARS_NEW` |

### New branch (`ars_v2_prod`) — 16 commits

| Commit | Date | Subject |
|---|---|---|
| `a518adb` | 26-Jun | after rollback |
| `2a8c87b` | 26-Jun | fix(per_opt): enforce SZ_MBQ × I_ROD ship cap for RL/TBC |
| `58ef3dc` | 26-Jun | fix(per_opt): pak-align TBL SHIP and HOLD independently |
| `f3ffb66` | 03-Jul | '2026-07-02' |
| `c68e10d` | 07-Jul | chore: rename local SQL Server references HOPC560 → HOPC866 |
| `95b0652` | 08-Jul | 08-07-2026 |
| `cb252dd` | 20-Jul | 20-07-26 |
| `2cae44f` | 20-Jul | obsidian |
| `0b7d009` | 20-Jul | Merge PR #29 |
| `5afde69` | 22-Jul | Add UPC Store Tracking |
| `3faced8` | 22-Jul | Merge PR #30 |
| `adb7e84` | 22-Jul | fix optional |
| `4c356fa` | 22-Jul | Merge PR #31 |
| `659d99d` | 23-Jul | Add ARS report stored procs + report-gen param dropdowns/fan-out |
| `2f193e7` | 04-Aug | add update fix fancons |
| `9bbb97a` | 04-Aug | Merge PR #32 |

---

## §3 — Allocation / rule engine

The largest behavioural surface. All paths are
`backend/app/services/rule_engine_per_opt.py` unless stated otherwise.

### 3.1 Multi-engine → single engine

**Old.** `/listing/generate` accepted `allocation_mode ∈ {pandas (default),
per_opt, sequential}` plus `exec_order`, dispatched at
`backend/app/api/v1/endpoints/listing.py:2631-2700`:

- `pandas` → `rule_engine_pandas.run_listing_and_allocation_pandas` with its own
  vectorised `_run_band` (cumulative-window race, all stores compete at once).
- `per_opt` → set `os.environ["ARS_PER_OPT_MODE"]="1"` and `ARS_EXEC_ORDER`, then
  **relabelled `mode = "pandas"`** and took the same path;
  `rule_engine_pandas._is_per_opt_mode()` read the env var per call and swapped
  `_run_band` → `rule_engine_per_opt._run_band_per_opt`.
- `sequential` → `rule_engine_new.run_listing_and_allocation`.

Also on disk but unreferenced: `rule_engine.py` (1,809 lines, called only inside an
`if False:` block at `listing.py:2615-2626`), `listing_allocator.py` (1,879),
`rule_engine_parallel_python.py` (532), `rule_engine_parallel_sql.py` (554),
`backend/sql/usp_ars_allocate_majcat.sql` (673).

**New.** Driven by `backend/app/docs/REMOVAL_PLAN_PER_OPT_ONLY.md` (v1.1,
phases 1–4 executed 10-Jul-2026). All five dead files deleted. `listing.py` now
carries a hard guard:

```python
_req_mode = (req.allocation_mode or "").strip().lower()
if _req_mode != "per_opt":
    raise HTTPException(400, f"allocation_mode '{req.allocation_mode}' was removed 2026-07-10 — only 'per_opt' is supported")
```

`exec_order` and both env vars are gone; `mode = "per_opt"` is a literal at the
dispatch site and in `/retry-failed`, which closes the env-inheritance bug.

**Role split in the new tree:**

| Module | Role |
|---|---|
| `rule_engine_per_opt.py` | the only band — `_run_band_per_opt` (Stage C) |
| `rule_engine_pandas.py` | orchestration host only — loaders, `ProcessPoolExecutor` worker, writer-queue thread, sec-cap spec build, Stage D, write-back |
| `rule_engine_new.py` | shared Stage A/B SQL library; its own sequential Stage C is dead-but-present (removal-plan phase 5) |

**Functions deleted:** `rule_engine_pandas._run_band` (~316 lines),
`_is_per_opt_mode`, `_hold_suppress_arr` (moved to `rule_engine_per_opt.py:67`),
`_build_mbq_budget`, the non-per_opt `FNL_Q_REM` aggregate recompute, and the
pre-band `_snapshot_fnl_q_rem(mask=…)` call.

**Worker plumbing:** `_pandas_run_one_majcat`'s `_extras` tuple grew from 3 to 14
elements (dispatch modes, matrix enabled/bands, `alloc_type` + 3 hold-suppression
flags, stock threshold, default `ACS_D`, retry flag).

### 3.2 Sec-cap — which grids participate

**Old.** Only grids with `ARS_GRID_BUILDER.sec_cap_applicable = 1`;
`cap_pct = sec_cap_pct or SEC_CAP_DEFAULT_PCT`.

**New.** Every grid with `grid_group = 'Primary'` participates **regardless of
`sec_cap_applicable`** (today MJ and MJ_MERGE_RNG_SEG). Secondary grids still opt
in. A Primary grid with a NULL `sec_cap_pct` anchors to **`mj_req_growth_pct`** (the
UI run headroom) rather than `SEC_CAP_DEFAULT_PCT`. Specs are sorted Primary-first
so the harder constraint wins the SKIP_REASON.

New SKIP_REASON namespace: `PRIMARY_CAP_PRE_<grid>(cap=N%)` alongside
`SEC_CAP_PRE_<grid>(cap=N%)`. `_stage_d_reflect` also matches
`SKIP_REASON LIKE 'PRIMARY_CAP_PRE_%'` when rolling the reason to OPT grain.

### 3.3 Sec-cap — override rule replaced

**Old** (`_evaluate_sec_cap_per_opt`): on breach, admit iff
`MJ_REQ_REM(werks) >= mbq_gate_factor × OPT_MBQ` (default 0.5); remark
`SEC_CAP_OVERRIDE(...)`.

**New** (unified overshoot, 04-Jul-2026):

```python
overshoot = run_before + intended_ship - budget
admit = configured and overshoot > 0 and overshoot <= 0.5 * intended_ship
```

Same formula for Primary and Secondary. Remark becomes
`SEC_CAP_OVERSHOOT(grid=…, cap=…%, reason=overshoot(n) <= 0.5xintended(m)=t, …)`.
Because `running` still advances by the full ship, **exactly one boundary OPT per
grain per band can breach** — the next one fails. Mirrored into the SQL post-pass
gate in `rule_engine_new._apply_sec_grid_cap_pre_gate`.

### 3.4 Sec-cap — hard-block on empty/zero grid data

`build_sec_cap_state` now emits `hard_block[grid][grain] = [reasons]`:

```python
if extras:  # any grid-extra in ('', 'NA', 'NONE', 'NAN') → SEC_CAP_GRID_NULL[<grid>]
if not mbq_null and mbq_val == 0.0: SEC_CAP_MBQ_ZERO[<grid>]
elif mbq_null:                      SEC_CAP_NULL[<grid>]
```

**Old:** an explicit `MBQ_ORIG = 0` meant "no constraint at this grain" and the gate
skipped it; a NULL / `'NA'` grid extra was not checked at all.

**New:** all three conditions **block** the OPT, pool untouched, remark
`SKIPPED by sec-cap hard-block | grid=… | reasons=… | pool_untouched=true`.

### 3.5 Sec-cap — two-pass veto beats override

`_evaluate_sec_cap_per_opt` was a single loop returning on the first breaching
grid. It is now two passes:

- **Pass 1 — veto scan**, order-independent, over all applicable grids. Any grid
  carrying `hard_block` reasons vetoes; combined reasons are stamped;
  `participating` returns empty so `running` is **not** advanced for any grid the
  OPT touched.
- **Pass 2 — breach scan**, Primary-first, reached only when Pass 1 found nothing.

Concrete change: an OPT that MJ would admit by overshoot, but whose
`MJ_M_YARN_02` grain has `MBQ_ORIG = 0`, used to ship (MJ evaluated first and
returned). It is now blocked. Covered by
`backend/tests/test_evaluate_sec_cap_per_opt_precedence.py::test_downstream_veto_beats_primary_overshoot_admit`.

### 3.6 Sec-cap — cont%-banded growth matrix (new, default OFF)

New file `backend/app/services/sec_cap_growth_matrix.py`
(`load_matrix`, `resolve_growth`, `snapshot_matrix`, `validate_bands`, auto-DDL for
`ARS_SEC_CAP_GROWTH_MATRIX` + `_CFG`).

- `cont_pct(grain) = grain MBQ_ORIG / Σ MBQ_ORIG over (WERKS, MAJ_CAT) × 100`
- `growth_pct` = band lookup; `ceiling = MBQ_ORIG × growth_pct/100` **instead of**
  the flat `cap_factor`.
- Default bands (half-open): `[0,5)→300%`, `[5,10)→250%`, `[10,15)→200%`,
  `[15,30)→150%`, `[30,∞)→120%`. `growth < 100` is clamped to 100 at load — the
  matrix may only relax, never tighten.
- Toggle lives in `ARS_SEC_CAP_GROWTH_MATRIX_CFG` (id=1, seeded disabled). Load
  failure returns `(False, [])` → flat cap; never fatal.
- Audit: SKIP_REASON becomes `SEC_CAP_PRE_<grid>(cap=200%,cont=13%)`; a gap in a
  hand-edited table appends `SEC_CAP_MATRIX_GAP[<grid>]`. The run snapshot writes
  `SEC_CAP/sec_cap_mode = STANDARD|MATRIX` to `ARS_RUN_PARAMS_AUDIT`.
- API: `GET`/`PUT /grid-builder/growth-matrix`.

### 3.7 Sec-cap — `running` counter is now MBQ-only

**Old (5g.1):** `actual_moved = round_ship + (round_hold if TBL else from_hold)` —
warehouse holds counted against the grain ceiling.

**New:** `actual_moved = float(round_ship.sum())` only. TBL `round_hold` (the
`SZ_MBQ_WH` RDC buffer) is deliberately dropped, and RL/TBC `round_ship` already
contains the `from_hold` contribution. Later OPTs at the same grain therefore see a
smaller `already_shipped_this_run` and are skipped less often. On the input side,
`intended_ship_sc = want_ship_pak.sum()` for TBL (was `opt_need.sum()`, which
included the hold buffer).

### 3.8 MBQ_CAP for RL/TBC — SCALED → COMPLETE-with-overshoot

**Old.** On `total_need > werks_cap`, always proportional scale-down:
`opt_need = floor(opt_need * scale / pak) * pak`. `mbq_budget` decrement floored
at 0.

**New.** Per-OPT_TYPE `rl_dispatch_mode` / `tbc_dispatch_mode`:

- **`COMPLETE`** (the only reachable mode): if `werks_cap >= 0.5 * total_need`, ship
  the full need and stamp `MBQ_CAP_OVERSHOOT(ot,need=…,cap=…,overshoot=…)`;
  otherwise skip the whole OPT with `MBQ_CAP_<ot>(complete-mode:…)`.
- The 5h decrement lost its `max(0.0, …)` floor, so the budget goes **negative** and
  every later OPT in the same WERKS hits the `werks_cap <= 0` hard-skip.
- **`SCALED`** still exists behind `elif mode == 'SCALED':`. The in-code comment
  records the bug this guard fixed: it used to be an unguarded fall-through, so a
  COMPLETE overshoot-admit dropped into it and got rescaled, emitting a
  contradictory `OVERSHOOT` + `SCALE` double stamp (repro
  HB48 / M_W_TRSR / 1112108374 / CRM shipped 10 not 15).
- `listing.py` coerces the field so SCALED cannot be selected at all:
  `@field_validator("rl_dispatch_mode","tbc_dispatch_mode") … return "COMPLETE"`.

### 3.9 TBL admission gate (MJ_REQ)

**Old.** `opt_mbq_sum = Σ SZ_MBQ`; skip when `mj_rem < 0.5 * opt_mbq_sum`.

**New.** `intended_tbl = want_ship_pak.sum()` (MBQ-only, pak-converted). When
`mj_rem < intended_tbl`, `overshoot = intended_tbl - mj_rem`; skip only if
`overshoot > 0.5 * intended_tbl`, otherwise admit and stamp
`MBQ_CAP_OVERSHOOT(TBL,intended=…,mj_req_rem=…,overshoot=…)`. The 5h2 decrement
also lost its `max(0, …)` floor, so `mj_req_rem_dict[werks]` can go negative and the
next TBL OPT hard-skips.

### 3.10 TBL pack alignment — SHIP and HOLD rounded independently

Commit `58ef3dc` (26-Jun-2026).

**Old (5c.5 / 5e / 5g):** one half-up pak rounding of `opt_need` (= `need_pool`,
WH-cumulative), then `round_ship = min(take_pool, need_ship)` and
`round_hold = max(take_pool - need_ship, 0)` gated by `take_pool >= need_ship`.

**New:**

```python
ship_basis    = need_ship                              # SZ_MBQ-derived
want_ship_pak = 0 if ship_basis < .5*pak else floor((ship_basis + .5*pak)/pak)*pak
hold_basis    = max(need_pool - want_ship_pak, 0)      # WH-buffer overhead
want_hold_pak = 0 if (want_ship_pak == 0 or hold_basis < .5*pak) else half_up_pak(hold_basis)
opt_need      = want_ship_pak + want_hold_pak
```

Pool draw is **SHIP first, HOLD from what's left**:
`round_ship = min(ship_target, live_pool)`,
`round_hold = 0 if round_ship == 0 else min(hold_target, pool_after_ship)`. If SHIP
gates to 0, HOLD is forced to 0 — no orphan hold.

The commit's own verification: HP01 / VAR_ART 1210052666001, TBL R1, OPT_MBQ=16,
OPT_MBQ_WH=17, PAK_SZ=24 → **SHIP=24 HOLD=0** instead of SHIP=16 HOLD=8 loose; pool
draw unchanged at 24.

A later refinement (01-Aug-2026) changed HOLD from "full pak only, else
sliver-if-below-pak, else 0" to `min(hold_target, pool_after_ship)` — previously a
mid-size remainder was discarded (target 12, 8 left → held nothing).

New audit markers `PAK_SZ_SHIP(...)` / `PAK_SZ_HOLD(...)` with `loose` / `short` /
`gated` qualifiers for TBL; RL/TBC keep `PAK_SZ_GATE` / `PAK_SZ_ROUND`. FS-06 hold
suppression is applied to `hold_basis` **before** the half-up rounding so a
suppressed hold cannot be re-inflated by rounding.

### 3.11 RL/TBC ship cap

Commit `2a8c87b` (26-Jun-2026), reworked 01-Aug-2026.

Original defect: `round_ship = take_pool + from_hold` with no ceiling.
`need_pool = r×SZ_MBQ − SZ_STK − POOL_CONSUMED` inflates each round because
prior-round hold ships land in `SHIP_QTY` / `FROM_HOLD_QTY` but **not**
`POOL_CONSUMED`. Repro in the commit: session `20260623_221526_267`, OPT
(HH17, M_BRIEF, 1114093871, A), SZ_MBQ 5/5/3, I_ROD 2, hold 5/6/6 → expected ship
10/10/6, actual **5/15/9**.

`2a8c87b` added `ship_ceiling = ceil(need_ship/pak)*pak` with a floor-to-pak and a
refund of `excess_pool` to `pool_dict`. The **current** state replaces that
ceil-then-floor pair with a 50 % pack conversion MIN'd against supply:

```python
ship_target    = 0 if need_ship < 0.5*pak else floor((need_ship + 0.5*pak)/pak)*pak
effective_ship = np.minimum(take_pool + from_hold, ship_target)
```

The comment records what this fixed: the old `ceil` ceiling "rounded up blind of the
pool and let the post-pass net ship phantom units (DW01 / 1241092244001: pool 3,
shipped 6)". With `pak=6`: 7→6, 8→6, 9→12, 3→6, 2→0; a last partial pack drains
as-is (req 3, stock 3 → ship 3).

### 3.12 RL/TBC hold draw is now pak-aware

```python
if hold_rem >= pak_i: take_h = min(opt_need[i], floor(hold_rem/pak_i)*pak_i)
else:                 take_h = min(opt_need[i], hold_rem)   # sub-pak residue drains loose
```

Previously a flat `min(opt_need[i], hold_rem)`. Hold is still consumed **before**
pool, keyed `(WERKS, VAR_ART, SZ)`, RL/TBC only.

### 3.13 Post-pass PAK rounding retired

`rule_engine_pandas.ENABLE_POST_PASS_PAK_ROUNDING = False`; both
`_apply_pak_sz_rounding_df` and `_stage_d_apply_pak_sz_rounding` early-return.
Rationale in code: both rounded SHIP up to a whole pak **without checking the
pool**, shipping stock that did not exist. Pack conversion now happens once,
in-band, where the live pool is known.

### 3.14 R07 live size-ratio gate — one-size short-circuit removed

**Old:** `if total_sizes > 1:` guarded the ratio check, exempting one-size TBL
GEN_ARTs.

**New:** the guard is gone;
`ratio = sizes_with_pool/total_sizes if total_sizes > 0 else 0`. A one-size TBL OPT
whose live pool is drained now gets `R07_SIZE_RATIO_LIVE(0/1)`.

> This is a **regression relative to the local branch**, which added that guard in
> `3c0329a` — see §9.1.

### 3.15 Post-TBL hold release + one retry pass ("Option B")

New block at the end of `rule_engine_pandas._run_majcat_waterfall`, gated by
`rule_flag('ALC_TBL_HOLD_RETRY', True)` (31-Jul-2026):

1. Find TBL options that shipped but stayed under cover —
   `STK_TTL + Σ SHIP < stock_threshold_pct × ACS_D` (default 0.6 × ACS_D,
   ACS_D default 18).
2. Credit their `HOLD_QTY` back into the **live** `pool_dict`, stamp
   `;HOLD_RELEASED_NOT_COVERED(n)`, zero `HOLD_QTY` / `ROUND_HOLD`.
3. Run **one** extra `_run_band_per_opt('TBL', 1)` — every gate re-applies. A
   released option cannot re-take its own pieces (`POOL_CONSUMED` stays spent) and
   its covered sizes have `need_ship = 0`.

`business_rules.py` records a measured potential of "133/133 freed pcs had same-run
takers". Part 8.55 remains the post-run safety net.

### 3.16 `FNL_Q_REM` semantics

**Old:** post-waterfall SQL recomputed `FNL_Q_REM = FNL_Q − Σ POOL_CONSUMED` per
pool key for non-per_opt runs, and a pre-band snapshot overwrote per-OPT values.

**New:** both removed. The per-OPT live post-draw value (step 5f.1) is
authoritative. `_mark_opt_skip(..., live_pool=…)` now writes the live pool snapshot
on pre-pool-take skip sites (R07, MJ_REQ gate, MBQ_CAP), so a skipped row's audit
shows the pool actually visible at its turn instead of the stale pre-allocation
`FNL_Q`. `_mark_opt_skip` also writes `SKIP_REASON` now (only when empty,
preserving upstream stamps) — previously it wrote only `ALLOC_STATUS` +
`ALLOC_REMARKS`.

### 3.17 `OPT_TYPE` classification rewritten (`listing.py` Part 3.6)

**Old** — 4 branches, first match:

| Branch | Condition |
|---|---|
| `MIX` | `MSA_FNL_Q = 0 AND RL_HOLD_QTY = 0` (any stock) |
| `TBL` | `STK <= 0 AND MSA > 0` |
| `RL` | `STK >= thr × ACS_D` |
| `TBC` | else |

**New** — 6 branches:

| Branch | Condition |
|---|---|
| `MIX` | low stock + no MSA + no hold |
| **`L`** (new) | adequate stock, no MSA, no hold — report-only, never enters allocation |
| `RL` | `(STK >= thr×ACS_D AND (MSA>0 OR HOLD>0)) OR (STK<=0 AND MSA=0 AND HOLD>0)` |
| `TBC` | `0 < STK < thr×ACS_D AND (MSA>0 OR HOLD>0)` |
| `TBL` | `STK <= 0 AND MSA > 0` |
| `MIX` | else (unreachable safety net) |

Two behavioural deltas: an adequately-stocked option with *no* supply is now `L`,
not `MIX`; and a sold-out option carrying only a prior-run hold is now `RL` (ships
by releasing the hold), not `MIX`. The `LST_RL_REQUIRES_MSA` registry row documents
the intended "RL needs fresh MSA" branch but is marked `is_wired = 0`.

### 3.18 `OPT_STATUS` post-allocation taxonomy rewritten (`listing.py` Part 8.5)

**Old:** `RL→RL`; TBC covered→`RL`, else→`MIX`; TBL covered→`NL`, else→`TBL`;
`ELSE ISNULL(OPT_TYPE,'MIX')`.

**New:** `L→L`, `MIX→MIX`, `RL & alloc=0 → WRL`, `TBL & alloc=0 → UNQ`,
`post < thr×ACS_D → MIX`, `TBL→NL`, `TBC→RL`, `RL→RL`, leftovers
`alloc>0→RL else L`.

New statuses **`WRL`**, **`UNQ`**, **`L`**; `TBL` is no longer a terminal
OPT_STATUS. Gated by `rule_flag('LST_OPT_STATUS_STAMP', True)`.

### 3.19 CONT fallback ladder

`rule_engine_new._stage_b_fill_cont(conn, alloc_table, cont_table, mode=...)`.

**Old:** Step 1 site row → Step 2 `'CO'` where `CONT IS NULL` → **unconditional
uniform `1/COUNT(DISTINCT SZ)` per (WERKS, MAJ_CAT)** for anything still 0.

**New:** Step 1 site row; Step 2 `'CO'` is now group-gated by
`HAVING MAX(ISNULL(CONT,0)) = 0` per (WERKS, MAJ_CAT) and matches
`ISNULL(A.CONT,0) = 0` rather than `A.CONT IS NULL`; then Steps 3/4 fill **only for
MAJ_CATs with `ARS_GRID_HIERARCHY.SZ_APPLICABLE = 'N'`** (NULL treated as `'Y'`,
untouched), per `cont_fallback_mode`:

| Mode | Behaviour |
|---|---|
| `P4_UNIFORM` (default) | `1/COUNT(DISTINCT SZ)` **per OPT** (WERKS, MAJ_CAT, GEN_ART, CLR), not per MAJ_CAT |
| `P3_FNL_Q` | `FNL_Q / Σ FNL_Q` per OPT |
| `STRICT` | deprecated, no fill |

A renormalise pass then scales `SZ_APPLICABLE='N'` OPT groups so `Σ CONT = 1.0`.

Note: `docs/2026-07-02-cont-fallback-ladder.md` (Rule 3: "uniform 1/N has been
removed") describes an intermediate state. The shipped default `P4_UNIFORM`
reinstates uniform fill, scoped to size-agnostic MAJ_CATs and OPT-grained. The
`SZ_MBQ` min-1 guard (`CONT>0 AND OPT_MBQ>0`) is unchanged.

### 3.20 Fresh / GRT typed pool

New file `backend/app/services/alloc_pool.py` — `get_sloc_type_map`,
`typed_sloc_cols`, `pool_stk_expr`, `fnl_q_eff_expr`, `pend_agg_sql`,
`hold_agg_sql`, `NoPoolColumnsError`.

- `alloc_type: Literal["FRESH","GRT"]` is now a **required** request field (422 when
  missing). `/generate` pre-checks the MSA for typed rows and 400s via
  `NoPoolColumnsError`.
- `_stage_b_explode(..., alloc_type=...)` joins **only** the matching typed MSA row
  (`AND ISNULL(V.[ALLOC_TYPE],'FRESH') = :at_pool`), reads its baked `FNL_Q`, and
  stamps `ALLOC_TYPE` on every alloc row. `alloc_type=None` keeps exact legacy SQL.
- Worker hold load is type-scoped:
  `... AND ([ALLOC_TYPE] = :alloc_type OR ISNULL([ALLOC_TYPE],'') = '')` — legacy
  untyped holds drain for any run type.
- `fnl_q_eff_expr` codifies
  `max(min(Σ SLOC(type) − PEND_T − HOLD_T, FNL_Q), 0)`, retaining `FNL_Q` as a
  physical-availability safety cap.

### 3.21 Hold suppression (FS-06)

New run flags `skip_hold_upc`, `apply_hold_seg_app`, `apply_hold_seg_gm`. The
predicate `_hold_suppress_arr` (`rule_engine_per_opt.py:67`) returns `None` when all
flags are default, so the fast path is byte-identical. Two layers:

- **Source layer** (`listing.py` Part 4): suppressed rows get
  `OPT_MBQ_WH = OPT_MBQ` (no HOLD_DAYS uplift), so they are budgeted like RL/TBC
  from the start.
- **Band layer** (5c.5): `hold_basis` zeroed pre-rounding, so `want_hold_pak = 0`,
  `opt_need` loses the hold, and the un-held qty stays in the live pool.

`_load_tables` now joins `ST_STATUS` (from `Master_ALC_INPUT_ST_MASTER`) and `SEG`
(from `vw_master_product`, falling back to `VW_ET_MSA_STK_WITH_MASTER`) onto the
alloc frame via `.map` — LEFT-join semantics, cannot multiply rows. NULL never
suppresses.

### 3.22 Business-rules registry now drives engine behaviour

`rule_flag` / `rule_value` reads in `listing.py`:

| Rule key | Effect |
|---|---|
| `LST_IROD_AMIX_FLOOR` | A/A_MIX I_ROD floor — **was a hardcoded `SET I_ROD = 2`**; now a configurable floor with MAX semantics, and *inactive means no floor at all* |
| `LST_OPT_STATUS_STAMP` | gates the new OPT_STATUS taxonomy (§3.18) |
| `ALC_HOLD_RELEASE_855` | Part 8.55 post-run hold release |
| `ALC_TBL_HOLD_RETRY` | the in-run release + retry pass (§3.15) |
| `ALC_MULTI_PARKED` | multiple parked sessions (moved out of Settings) |
| `LST_AUDIT_ALL_TO_WORKING` | audit-mode (moved out of Settings) |

Rows marked `is_wired = 0` (`ALC_MJREQ_SKIP_FACTOR`, `GRD_R09_FACTOR`,
`GRD_SEC_CAP_DEFAULT`) are registered but not read — engine constants still apply.

### 3.23 Unchanged

`need_ship` / `need_pool` formulas are byte-identical:

```python
need_ship = max(r*SZ_MBQ - SZ_STK - SHIP_QTY, 0)
TBL:  need_pool = max(SZ_MBQ_WH + (r-1)*SZ_MBQ - SZ_STK - POOL_CONSUMED, 0)   # zeroed when need_ship == 0
else: need_pool = max(r*SZ_MBQ - SZ_STK - POOL_CONSUMED, 0)
```

Hold-before-pool source priority, the RL→TBC→TBL fixed order, and OPT sort
(`OPT_PRIORITY_RANK → ST_RANK → WERKS`) are unchanged. Growth still lives at
MJ + grid only (`MJ_MBQ_REV = MJ_MBQ × growth%`); no change found to the R09
headroom rule.

### 3.24 MSA changes

`backend/app/services/msa_service.py`, `backend/app/api/v1/endpoints/msa_stock.py`.

1. **Row-per-type (FRESH/GRT) output — new Step 12.** `_expand_to_typed_rows` /
   `_expand_frame` turn each of the three result frames (`msa_pivot`,
   `msa_gen_clr`, `msa_gen_clr_var`) into one row per SKU **per pool type**.
   Per-type `STK_QTY` = Σ of that type's SLOC pivot columns; the other type's SLOC
   columns are zeroed on the row; `PEND_QTY` / `HOLD_QTY` are re-derived **live**
   from the open ledgers folded to the row's type (legacy `ALLOC_TYPE` NULL/`''` →
   FRESH); `FNL_Q = max(STK − PEND − HOLD, 0)`. Non-fatal: on failure the untyped
   output is kept and a warning appended.
2. **Row-retention rules.** FRESH rows are **always** kept — they are the
   denominator of the listing size-coverage ratio `VAR_FNL_COUNT/VAR_COUNT`;
   dropping zero-stock FRESH rows inflated the ratio and over-listed options
   (baseline-parity fix, 10-Jul-2026). GRT rows are kept **per-OPT**
   `(RDC, MAJ_CAT, GEN_ART_NUMBER, CLR)`, not per-variant (13-Jul-2026) — keeping
   them per-variant fragmented the size ladder and produced a 1-size GRT pool with
   wrong CONT / SZ_MBQ on GRT runs.
3. **New Step 6b — `ATT_TYP` gate.** After Step 6 and before the PEND/HOLD merges,
   `msa_pivot` is filtered to `settings.MSA_ALLOWED_ATT_TYP` (default `00` single +
   `02` variant), dropping `01` generic headers and `11` structured/prepack.
   `ATT_TYP` is not on `VW_ET_MSA_STK_WITH_MASTER`, so `_load_att_typ_map` resolves
   it per `ARTICLE_NUMBER` from `vw_master_product`. **Unknown ATT_TYP is dropped.**
   The same allowlist is applied at the variant-backfill source. An unresolvable map
   skips the gate with a user-visible warning.
4. **Soft-failure surface.** `MSAService.warnings: List[str]` is returned by
   `calculate()` so the UI can toast partial failures (Steps 6b, 12, 7.5).
5. **`calculate_msa` returns a preview only** when auto-storing —
   `PREVIEW_ROWS = 500` per frame plus `preview: True`. Reason in code: row-per-type
   MSA pushed a full-universe calc past 2M rows, the browser could not `JSON.parse`
   it and the page crashed on `reading 'sequence_id'` (10-Jul-2026).
6. **New SLOC pool admin API** on `msa_stock.py`: `GET /sloc-settings`,
   `PUT /sloc-settings` (bulk, all-or-nothing validation,
   `sloc_type ∈ {FRESH,GRT}`), writing `ARS_MSA_SLOC_SETTINGS` with
   `type_changed_at` stamped only on an actual type change. Changes apply **from the
   next MSA generation** — they never rewrite an existing MSA.

### 3.25 Pending-allocation / hold changes

`backend/app/services/pend_alc_service.py`,
`backend/app/api/v1/endpoints/pend_alc.py`,
`backend/app/api/v1/endpoints/hold_dashboard.py`.

1. **`ALLOC_TYPE` column** on `ARS_PEND_ALC` (`_ENSURE_COLS`) and
   `ARS_NL_TBL_HOLD_TRACKING`, carried end-to-end. `write_pend_alc` selects
   `MAX(H.[ALLOC_TYPE])` from `ARS_ALLOC_HISTORY`; `write_manual_pend_alc` accepts a
   per-row `alloc_type` normalised by `_norm_alloc_type` (`None`/`''` → NULL =
   legacy → folds into FRESH; `FRESH`/`GRT` pass; anything else raises).
2. **`write_pend_alc` grain fix.** MAJ_CAT moved from `MAX(H.[MAJ_CAT])` into the
   GROUP BY, and the `ARS_LISTING_WORKING_HISTORY` join gained
   `AND ISNULL(W.[MAJ_CAT],'') = ISNULL(H.[MAJ_CAT],'')`. Previously an article
   spanning two MAJ_CATs collapsed into one pend row with an arbitrary MAJ_CAT and
   could pick up the wrong `OPT_TYPE` as `ALLOC_MODE`.
3. **Typed DO deduction.** `apply_do_deductions` splits the FIFO into type buckets:
   a row carrying `alloc_type` only drains PEND rows whose **folded** type matches
   (`ISNULL(ALLOC_TYPE,'')='' → 'FRESH'`). Typed buckets drain first within each
   `st_cd` scope, then untyped input absorbs residual capacity across all types.
   When no input row is typed, the emitted SQL is byte-identical to the old
   implementation.
4. **New DO deduction methods (FS-12).**
   `apply_do_deductions(..., deduction_method, target_session_id)`:
   - `FIFO` (default, unchanged — `APPROVED_AT ASC, ID ASC`)
   - `SESSION_FIRST` — window `ORDER BY` prefixed with
     `CASE WHEN P.SESSION_ID = :tsid THEN 0 ELSE 1 END`
   - `SESSION_ONLY` — open-rows predicate `AND P.SESSION_ID = :tsid`; excess DO qty
     surfaces in `overflow_rows` as unapplied

   `target_session_id` is required for the two non-FIFO methods, validated at both
   the service and the endpoint via `_validate_deduction_method`.
5. **`stamp_bdc_qty(..., store_scoped=False)`** — FS-13 defect #3. The legacy
   predicate `(u.st_cd = '' OR ISNULL(P.ST_CD,'') = u.st_cd)` let a pair whose pend
   row had an empty `ST_CD` stamp **every** store's rows for that (RDC, ARTICLE).
   With `store_scoped=True` (set when the generate request carries a store filter)
   the predicate becomes exact: `ISNULL(P.ST_CD,'') = u.st_cd`.
6. **Hold revert paths carry type.** `_revert_hold_clear` / `_revert_hold_revise`
   temp tables gained `alloc_type NVARCHAR(10) NOT NULL`, so a revert restores the
   correct typed hold row.
7. **Endpoint surface.** `pend_alc.py` adds an `alloc_type` filter
   (`FRESH | GRT | LEGACY`) on summary / list / export, a `by_alloc_type` breakdown
   in the summary, `default_alloc_type` on bulk upload, the `deduction_method` /
   `target_session_id` fields on the DO-update request (sync and async variants),
   and new `GET /dispatch-gap` + `/dispatch-gap/export` endpoints plus an async-job
   table.
8. **Hold dashboard** gains `_has_alloc_type_col` (pre-migration-safe) and
   `_alloc_type_filter`, an `alloc_type` query param on the KPI / list / detail
   endpoints, and a `LEGACY|FRESH|GRT` split of open `HOLD_REM`
   (`CASE WHEN ISNULL(ALLOC_TYPE,'')='' THEN 'LEGACY' ELSE ALLOC_TYPE END`).
9. **Part 8.55 post-run hold release** on `OPT_STATUS='MIX'` options now also zeroes
   `HOLD_QTY` on **this session's `ARS_ALLOC_PARKED`** — parked feeds hold-tracking
   at Approve, so zeroing only the working copies would still commit the hold.

---

## §4 — New backend subsystems

### 4.1 FA & CONS

**Purpose.** A self-contained allocation stream for **FA** (Project-Store
Allocation — new / UPC project stores) and **CONS** (Consumables Allocation, pan
India). Deliberately runs *beside* core ARS with its own tables, SLOC selection,
MSA/store-stock calc, MBQ master, allocation engine and pending pipeline. It does
not touch core ARS listing / allocation / pending tables.

**Files.** `backend/app/api/v1/endpoints/facons.py` (585 lines,
`APIRouter(prefix="/fa-cons")`); services `facons_{sloc→stock,mbq,store_list,alloc,gap,pend}_service.py`;
migration `backend/scripts/025_facons_foundation.sql`; 9 frontend pages plus
`frontend/src/components/facons/*`; manual `frontend/public/docs/manual/fa_cons.md`.

**Endpoints** (all `Depends(get_current_user)`; no additional gate — see §9.3)

| Area | Method + path |
|---|---|
| SLOC settings | `GET/PUT /fa-cons/sloc-settings`, `GET /fa-cons/sloc-settings/stock-totals`, `POST /fa-cons/sloc-settings/sync` |
| Stock calc | `POST /fa-cons/stock/calculate`, `GET /fa-cons/stock/results`, `GET /fa-cons/stock/sequences` |
| MBQ master | `GET/POST /fa-cons/mbq`, `POST /fa-cons/mbq/source-type`, `DELETE /fa-cons/mbq/{row_id}`, `GET /fa-cons/mbq/change-review[/export]`, `GET /fa-cons/mbq/sessions`, `GET /fa-cons/mbq/history`, `POST /fa-cons/mbq/upload`, `GET /fa-cons/mbq/template`, `GET /fa-cons/mbq/export` |
| Allocation | `POST /fa-cons/alloc/run`, `GET /fa-cons/alloc/sessions`, `GET /fa-cons/alloc/results`, `GET/POST /fa-cons/alloc/articles` |
| Gap report | `GET /fa-cons/gap` |
| Pending | `GET /fa-cons/pend`, `POST /fa-cons/pend/approve-session`, `POST /fa-cons/pend/manual`, `POST /fa-cons/pend/{id}/{deliver,close,reopen}`, `POST /fa-cons/pend/generate-do`, `PUT /fa-cons/pend/{id}`, `GET /fa-cons/pend/ops` |
| Store list | `GET/POST /fa-cons/store-list`, `GET /fa-cons/store-list/validation`, `DELETE /fa-cons/store-list/{id}`, `POST /fa-cons/store-list/{add-missing,upload}`, `GET /fa-cons/store-list/{history,template}` |

**Tables** (Data DB `Rep_data`; created by migration 025 *and* self-healed by each
service): `ARS_FACONS_SLOC_SETTINGS`, `ARS_FACONS_MBQ` + `_HIST` + `_SESSION`,
`ARS_FACONS_STOCK_SEQUENCE` / `_STOCK` / `_MSA` / `_SLOC_STOCK_CACHE`,
`ARS_FACONS_ALLOC_SESSION` / `_REF` / `_ART`, `ARS_FACONS_PEND` / `_OPS`,
`ARS_FACONS_STORE_LIST` / `_HIST`.

**Notable.**
- Upload is intentionally 3 columns (`ST_CD, REF_ART, MBQ_Q`); store identity
  (name / RDC / hub / OP_DT / status) is **joined live** from
  `Master_ALC_INPUT_ST_MASTER`, never copied — same pattern as UPC Store Tracking.
- UPC vs OLD store status is **derived live** from `ST_STATUS`, so a master flip
  auto-propagates.
- Allocation: `required = max(MBQ − store stock, 0)`,
  `alloc_qty = min(required, remaining central pool)` rounded to whole packs
  (`PAK_SZ` from `VW_MASTER_PRODUCT`). Stores are processed OLDER→NEWER by `OP_DT`
  so older stores draw the shared per-RDC×ref central pool first. Only
  `source_type = CENTRAL` is considered. Article-level split is "Phase B.2",
  partially implemented.
- Gap report is read-only and returns one recommended action per store×ref:
  `DISPATCH` / `DISPATCH+PURCHASE` / `PURCHASE` / `HOLD` / `STORE_RETURN` / `OK`,
  plus a network-level Purchase Requirement roll-up.
- All segments are considered (no `SEG='APP'/'GM'` filter) — an explicit FA/CONS
  deviation. Only `DIV ∈ {FA, CO}` counts.

### 4.2 SAP Integration

**Purpose.** A **read-only** module that pulls data *from* SAP into local `SAP_*`
staging tables on a schedule or on demand. No SAP SDK / `pyrfc` on the ARS server —
the app calls the V2 universal-MCP gateway worker over HTTPS with an `X-API-Key`
header, and the worker relays to SAP via `RFC_READ_TABLE` or OData.

**Files.** `backend/app/api/v1/endpoints/sap.py`; services `sap_client.py`,
`sap_config_service.py`, `sap_pull_service.py`, `sap_scheduler_service.py`,
`sap_snowflake_client.py`; migration `backend/scripts/026_sap_foundation.sql`;
4 frontend pages plus `components/sap/WhereBuilder.jsx`, `store/sapUiStore.js`,
`utils/sapWhere.js`.

**Endpoints.** `GET /sap/connection` (any user), `PUT /sap/connection`
(**SUPER_ADMIN**), `POST /sap/connection/test` (ADMIN/SUPER_ADMIN);
`GET/POST /sap/pulls`, `GET/PUT/DELETE /sap/pulls/{id}`,
`POST /sap/pulls/{id}/{enable,run}`; `GET /sap/runs`, `GET /sap/scheduler/status`;
`POST /sap/preview` (limit clamped 1..500); `POST /sap/snowflake/test`,
`GET /sap/snowflake/tables`, `GET /sap/odata-services`.

**Tables.** `SAP_CONNECTION` (single row id=1: worker URL, Fernet-encrypted API key,
env, display mode, enabled, last status), `SAP_PULL_DEF` (three doors —
`rfc_table` / `odata` / `snowflake` — target table, write mode, trigger type,
schedule config, `NEXT_RUN_AT`), `SAP_PULL_RUN`, and `SAP_*` target tables created
lazily on first run (all `NVARCHAR`, plus `_RUN_ID` / `_PULLED_AT`).

**Notable.**
- Gateway tools over JSON-RPC: `sap_read_table`, `sap_odata_pull`,
  `sap_odata_services`, `v2_rfc_status`. HTTP timeout 120 s.
- Paging constants: RFC page 5000, OData page 2000, column max length 1000, and a
  2000 bound-parameter cap to stay under pyodbc's 2100 limit.
- The scheduler **reuses `report_scheduler_service.compute_next_run`** so both
  modules' schedule maths are identical. Claim-and-run: atomic
  `UPDATE ... WHERE NEXT_RUN_AT <= now` so a pull cannot double-fire across uvicorn
  workers; already-running pulls are skipped, not stacked; orphaned `running` rows
  reconcile to `failed` at startup. Manual "Run now" bypasses the scheduler.
- `sap_snowflake_client.py` is now a 34-line adapter delegating to
  `snowflake_config_service`, re-raising `SnowflakeError` as `SapError`.

### 4.3 Snowflake configuration

**Purpose.** The single app-wide Snowflake connection shared by the SAP Snowflake
door *and* the Report Generation engine. Previously embedded in the SAP connection;
extracted here.

**Files.** `backend/app/api/v1/endpoints/snowflake_config.py`
(`prefix="/settings/snowflake"`); services `snowflake_config_service.py`,
`snowflake_sync.py`; `frontend/src/pages/SnowflakeConnectionPage.jsx`.

**Endpoints.** `GET /settings/snowflake/config` (any user, secrets masked),
`PUT /settings/snowflake/config` (**SUPER_ADMIN**), `POST /settings/snowflake/test`
(ADMIN/SUPER_ADMIN), `GET /settings/snowflake/tables`.

**Tables.** `SNOWFLAKE_CONNECTION` (single row id=1; keypair or password auth,
Fernet-encrypted secrets) — **no numbered migration exists for it**;
`ARS_SF_WATERMARKS` created by `snowflake_sync.ensure_watermark_table()`.

**Notable.** Key-pair (JWT) auth matching the global Snowflake MCP setup, plus
username/password; 120 s query timeout. `snowflake_sync.py` implements incremental
upsert: filter rows newer than the saved watermark → `write_pandas()` into a
transient stage table → `MERGE INTO` target on key columns → save new watermark. It
is import-safe when `snowflake-connector-python` is absent. See §9.5 for the
hardcoded defaults.

### 4.4 Report Generation hub

**Purpose.** Build a report as an ordered list of steps (SQL stored procs, raw
queries, or registered code steps), run it on a schedule / on demand / on process
completion, and deliver to a folder, Snowflake, email, WhatsApp or SMS.

**Files.** `backend/app/api/v1/endpoints/report_gen.py` (527 lines); services
`report_engine.py`, `report_worker.py`, `report_scheduler_service.py` (664),
`report_delivery.py`, `report_code_steps.py`, `data_export_service.py` (898);
migration `backend/scripts/017_report_generation.sql`; SQL assets
`backend/sql/usp_ars_{fresh_lorry,grid_report,msa_master}.sql`,
`ars_proc_param_values.sql`, `fresh_lorry.sql`;
`frontend/src/pages/ReportGenerationPage.jsx` (1,230) and `RunParamsPage.jsx`.

**Endpoints.** `GET/POST /report-gen/reports`, `GET/PUT/DELETE
/report-gen/reports/{id}`, `POST /report-gen/reports/{id}/{toggle,run,cancel}`,
`GET /report-gen/reports/{id}/runs`, `DELETE
/report-gen/reports/{id}/runs/{run_id}`, `POST
/report-gen/reports/{id}/runs/bulk-delete`, `GET /report-gen/{code-steps,
procedures, proc-params, events, status}`.

**Tables.** `ARS_REPORTS` (JSON `STEPS`, output type, base dir, file format, plus
`SPLIT_CONFIG` / `EMAIL_CONFIG` / `WHATSAPP_CONFIG` / `SMS_CONFIG` /
`FOLDER_PER_RUN` added by `ensure_report_tables()` self-heal, **not** in migration
017); `ARS_REPORT_RUNS`; `ARS_PROC_PARAM_VALUES` (generic dropdown registry for
stored-proc params — `FANOUT=1` means a comma-list value makes the engine run the
proc once per value and write a separate suffixed file); `ARS_SF_WATERMARKS`.

**Notable.**
- **Process isolation:** the scheduler launches
  `python -m app.services.report_worker <payload.json>` so a heavy multi-million-row
  proc runs in a separate OS process and never holds the API worker's GIL. Exit 0 =
  worker finalised its own run row; non-zero/killed = parent marks failed or
  cancelled.
- **Concurrency:** one daemon thread ticks every N seconds and claims due reports
  with an atomic `UPDATE ... WHERE NEXT_RUN_AT <= now`, so 4 uvicorn workers cannot
  double-fire. A `ThreadPoolExecutor(max_parallel)` runs claimed reports; an
  already-running report is *skipped* (logged as a `skipped` run) rather than
  stacked. Times are UTC against `SYSUTCDATETIME()`.
- **Events:** `emit_event()` is fire-and-forget. Emit sites —
  `listing.approved` + `pendalc.approved` at
  `backend/app/api/v1/endpoints/listing.py:3987-3988`; `msa.completed` at
  `backend/app/services/msa_job_service.py:252`; `autocont.completed` at
  `backend/app/api/v1/endpoints/auto_contrib.py:619`.
- **Security boundary:** a `code` step names a function from the
  `report_code_steps.py` registry, never an arbitrary import path. Report
  definitions are user-editable, so arbitrary import/exec is explicitly forbidden.
- **Storage guardrails** (`app_settings.json` → `reports`): `retention_days` (7),
  `min_free_mb` (500, pre-flight fail-fast), `cleanup_enabled`.
- **Delivery:** converts each output file to `source | xlsx | pdf | docx`, optionally
  zips (auto-zip when >1 attachment), emails via stdlib `smtplib`. **Sending is
  disabled until `settings.SMTP_HOST` is set** — `send_report_email()` returns a
  non-fatal error so a run never crashes on missing SMTP. Caps: `MAX_PDF_ROWS =
  2000`, `MAX_DOCX_ROWS = 5000`.
- **New stored procs:** `usp_ars_fresh_lorry` (FRESH LORRY report from
  `ARS_ALLOC_HISTORY` latest `SESSION_ID`); `usp_ars_grid_report` (332 lines —
  complete grid report for `ARS_GRID_MJ` and every secondary grid, `@DimCol`
  auto-detected); `usp_ars_msa_master` (204 lines — MSA opening-vs-closing
  reconciliation with a `@Level` roll-up `DETAIL > ARTICLE > GEN_CLR > MAJ_CAT >
  RDC`, backed by view `vw_ars_msa_master_levels`).

### 4.5 UPC Store Tracking

**Purpose.** Store-opening lifecycle tracker replacing the manual
"STORE OPENING DATES *.xlsx" workbook. Persists only *tracking state*; everything
else is joined live.

**Files.** `backend/app/api/v1/endpoints/upc_store_track.py` (193);
`backend/app/services/upc_store_track_service.py` (959); migration
`scripts/024_upc_store_tracking.sql`;
`frontend/src/pages/UpcStoreTrackingPage.jsx` (1,346).

**Endpoints.** `GET /upc-store-track?segments=APP,GM`, `GET
/upc-store-track/{charts,compare,export,template}`, `GET /upc-store-track/{st_cd}`,
`POST /upc-store-track`, `PUT/DELETE /upc-store-track/{st_cd}`, `POST
/upc-store-track/{upload,compact-history}`, `POST /upc-store-track/reset`
(**SUPER_ADMIN** — the only role-gated route in the module).

**Tables.** `ARS_UPC_STORE_TRACK` (one row per store: first/latest proposed and
share dates, date given/change counts, layout and display flags, actual open date,
status ACTIVE|OPENED|HOLD|CANCELLED, remarks + change count) plus three immutable
event tables `_DATE_HIST`, `_REMARK_HIST`, `_STATUS_HIST`.

**Notable.** Upload is only `ST_CD` + proposed opening date + share date (+ optional
remarks/layout). Identity joins live from `Master_ALC_INPUT_ST_MASTER`; metrics join
live from `ARS_GRID_MJ` per store (`SUM(MBQ)`, `SUM(STK_TTL)`, `SUM(DISP_Q)`,
fill-rate = stock ÷ MBQ, plus SLOC-wise columns). History is event-based, so
"how many times a date was given/changed" is derivable and auditable rather than
hand-maintained. Gated by business rule `UPC_TRACKING_ENABLED`.

### 4.6 Dev Sync

**Purpose.** One-way table refresh from PROD (`HOPC866`) → DEV (`ARSDBPRO`).

**Files.** `backend/app/api/v1/endpoints/dev_sync.py` (193);
`backend/app/services/dev_sync_service.py` (725); migration
`scripts/023_dev_sync_tables.sql`; `docs/DEV_SYNC_PLAN.md`;
`frontend/src/pages/DevSyncManagerPage.jsx` (501).

**Endpoints — every one is `_require_superadmin`.** `GET/PUT /dev-sync/settings`,
`POST /dev-sync/{test-connection,setup-linkserver}`, `GET
/dev-sync/{discover,tables}`, `POST/PUT/DELETE /dev-sync/tables[/{tid}]`, `POST
/dev-sync/tables/{bulk-toggle,bulk-delete,clear}`, `POST /dev-sync/run`.

**Storage.** `ARS_DEV_SYNC_TABLES` lives on the **DEV (target)** DB only.
**Connection settings are deliberately NOT in the DB** — they live in
`backend/app_settings.json` under `dev_sync`, so nothing is written to prod.

**Notable.** Movement is server-side `INSERT … SELECT` over a linked server created
*on the target* pointing at the source, so the source is only ever read. Modes:
`incremental` (append rows with key > `MAX(key)` on dev — cheap, daily) and `full`
(`TRUNCATE` + `INSERT` in a transaction — weekly, reconciles drift). A table whose
prod and dev row counts already match is skipped unless forced. Safety: **refuses to
run if `source_server == target_server`**; each table runs in its own transaction so
one failure rolls back only that table.

### 4.7 Smaller subsystems

| Module | Files | Gate | Notes |
|---|---|---|---|
| **Daily Activity Log** | `endpoints/activity_log.py` (438) | SUPER_ADMIN in-body | Rolls `audit_log` into one bullet per (ARS module × action) per day; superadmin marks each YES/NO into `ars_daily_activity_validation` (per reviewer + day). Also shells out to **git** for commit-derived developer pointers. |
| **Business Rules** | `endpoints/business_rules.py` (56) + `services/business_rules.py` (515) | `RequireRoles(["SUPER_ADMIN"])` | Module-wise switches with Active/Inactive + value. `rule_value()` / `rule_flag()`; **inactive or missing always falls back to the caller's default**, so switching everything off restores hardcoded behaviour and a broken table cannot take a run down. 16 seed keys. `is_wired=0` marks registered-but-unread. Seed *metadata* self-heals only on rows still owned by `'seed'`. |
| **Data Dictionary** | `endpoints/data_dictionary.py` (388) | **none** | CRUD reference of every column (abbreviation, purpose, tables, formula, module). Large in-code `SEED` self-heals; refresh only touches rows whose `updated_by` is `'seed'`/`'code-sweep'` — user edits are never overwritten. Governed by `DAT_DICT_SELF_HEAL`. |
| **Release Notes** | `endpoints/release_notes.py` (76) + `services/release_notes_service.py` (243) | **none** | Per-day changelog tagged by area, auto-compiled newest-first; frontend generates a shareable plain-text note. Ships a curated `_SEED` covering the 01-Jul-2026 window forward. |
| **WhatsApp Config** | `endpoints/whatsapp_config.py` (175) + `services/whatsapp_settings_service.py` (397) | `RequirePermissions(["ADMIN_SETTINGS"])`; token change SUPER_ADMIN | Meta Cloud API config for report delivery. `APP_WHATSAPP_SETTINGS` (Fernet token) + `APP_WHATSAPP_AUDIT_LOG`. Token is masked in reads **and in the audit log**; client IP and machine name captured per change. Live calls to `graph.facebook.com/v20.0`, 20 s timeout. |
| **Crypto** | `backend/app/core/crypto.py` (96) | — | Fernet (AES-128-CBC + HMAC-SHA256) for secrets at rest. Key is `APP_ENC_KEY` in `backend/.env`; if absent, `_load_or_create_key()` **generates one and appends it to `.env`**. Format `enc:<token>`; anything without the prefix is treated as plaintext, so it is safe to introduce over existing plaintext columns. `encrypt_secret` is idempotent; `decrypt_secret` returns `''` on `InvalidToken` and never raises. Rotating the key makes existing secrets undecryptable. |

---

## §5 — Platform & infra deltas

### 5.1 Routers (`backend/app/api/v1/router.py`)

**11 added, none removed:** `whatsapp_config`, `data_dictionary`, `report_gen`,
`activity_log`, `dev_sync`, `upc_store_track`, `facons`, `release_notes`,
`business_rules`, `sap`, `snowflake_config`.

### 5.2 App lifespan (`backend/main.py`)

Two new background daemons start/stop with the app, each wrapped in try/except so a
failure only warns: `report_scheduler.start()/.stop()` and
`sap_scheduler.start()/.stop()`.

### 5.3 Config (`backend/app/core/config.py`)

| Key | Old | New | Note |
|---|---|---|---|
| `DB_SERVER` | `HOPC560` | `HOPC866` | see §5.6 |
| `DB_TEMPDB_CLEANUP_SCHEDULE` | — | `"weekly_sunday_midnight"` | the 5-min interval caused DBCC SHRINKFILE convoys behind long-running jobs (01-Aug-2026 gridlock) |
| `AUTO_FREE_AFTER_JOB` | `True` | `False` | per-job shrinks piled into a DBCC convoy; re-enable via env if needed |
| `MSA_ALLOWED_ATT_TYP` | — | `["00", "02"]` | MSA article-category allowlist (§3.24) |
| `SMTP_HOST/PORT/USER/PASSWORD/FROM/USE_TLS` | — | `""`/`587`/`""`/`""`/`""`/`True` | emailed reports; sending disabled until `SMTP_HOST` is set |
| `Config.extra` | default | `"ignore"` | tolerates operational `.env` keys not modelled here (`APP_ENC_KEY`, `DB_PORT`) |

`backend/.env.example`: only change is `DB_SERVER=HOPC560 → HOPC866`.

`app_settings.json` (via `backend/app/api/v1/endpoints/settings.py`) gained four
declared blocks with mask-preserving update handling — **`snowflake`**,
**`whatsapp`** (provider `meta_cloud`), **`sms`** (provider `msg91`),
**`reports`** — plus a fifth, undeclared-in-defaults block **`dev_sync`** written by
`dev_sync_service`.

### 5.4 Dependencies (`backend/requirements.txt`)

Three additions: `python-docx>=1.1.0` and `fpdf2>=2.7.0` (Word/PDF export for
emailed reports), `snowflake-connector-python[pandas]>=3.12.0` (incremental upsert).
`cryptography`/Fernet was already present.

### 5.5 SQL migrations

| Script | Change |
|---|---|
| `backend/scripts/016_alloc_type_columns.sql` | `ALLOC_TYPE NVARCHAR(10) NULL` on `ARS_PEND_ALC`, `ARS_ALLOC_HISTORY`, `ARS_ALLOC_WORKING`; widens the key of `ARS_NL_TBL_HOLD_TRACKING` and `_SNAPSHOT` to `(WERKS, VAR_ART, SZ, ALLOC_TYPE)` with `NOT NULL DEFAULT ''` (PK columns can't be NULL, so `''` is the untyped sentinel; code treats `''` and NULL alike). Adds `IX_ARS_PEND_ALC_alloc_type` |
| `backend/scripts/017_msa_alloc_type_rows.sql` | `ALLOC_TYPE NVARCHAR(10) NOT NULL DEFAULT 'FRESH'` on the three MSA tables via cursor. Row-per-type MSA; legacy/untyped folds into FRESH |
| `backend/scripts/017_report_generation.sql` | `ARS_REPORTS` + `ARS_REPORT_RUNS` and indexes (predates the SPLIT/EMAIL/WHATSAPP/SMS/FOLDER_PER_RUN columns) |
| `backend/scripts/018_alloc_type_integrity.sql` | CHECK constraints so no writer can store an invalid pool value. `ARS_ALLOC_WORKING` is DROP+SELECT INTO per run so it is normalised in code instead. Adds `ARS_LISTING_SESSIONS.ALLOC_TYPE`, backfilled via `JSON_VALUE` from `REQUEST_JSON` |
| `backend/scripts/019_rename_msa_sloc_settings.sql` | Renames `ARS_SLOC_SETTINGS` → `ARS_MSA_SLOC_SETTINGS` — the old name collided with the legacy STORE-sloc table that `sloc_validation.py` migrates into `ARS_STORE_SLOC_SETTINGS`. Only renames if the table has a `sloc_type` column; otherwise creates fresh. Adds `updated_by`, `type_changed_at` |
| `backend/scripts/021_add_manual_st_priority.sql` | `MANUAL_ST_PRIORITY INT NULL` on `Master_ALC_INPUT_ST_MASTER`. NULL/0/negative = rank by `W_SCORE`; positive P pins `ST_RANK = P` in every MAJ_CAT the store is listed in, non-pinned stores shift into the smallest unoccupied ranks |
| `backend/scripts/022_dispatch_control_bdc.sql` | Three Generate-BDC gate tables: `ARS_HOLD_ARTICLE_BDC`, `ARS_DIVISION_DELETE_BDC`, `ARS_DIVISION_DELETE_ON_MAJ_CAT_BDC`. Renames `ARS_DIVISION_DELETE_BDC.STATUS → DIV` and strips the legacy `-DEL` suffix (`KIDS-DEL → KIDS`) |
| `backend/scripts/025_facons_foundation.sql` | FA & CONS foundation (§4.1) |
| `backend/scripts/026_sap_foundation.sql` | SAP foundation (§4.2) |
| `scripts/023_dev_sync_tables.sql` | `ARS_DEV_SYNC_TABLES` on the DEV target (§4.6) |
| `scripts/024_upc_store_tracking.sql` | UPC tracking head + 3 history tables (§4.5) |

Also modified (not new): `backend/scripts/015_create_sloc_settings.sql`,
`backend/scripts/migrations/2026_05_17_drop_fb_columns.sql` (HOPC rename only).

See §9.6 for the numbering and coverage problems.

### 5.6 The HOPC560 → HOPC866 rename

Commit `c68e10d` (07-Jul-2026). Pure chore: 10 files, 22 insertions / 22 deletions.
Stated rationale — **match `backend/app_settings.json`, which already used
`hopc866` as the source of truth.**

Touched `backend/app/core/config.py` (the `DB_SERVER` default),
`backend/.env.example`, `README.md`, `.claude/agents/rule_ars.md`,
`backend/scripts/build_listing_alloc_doc.py`,
`backend/scripts/migrations/2026_05_17_drop_fb_columns.sql`,
`frontend/public/docs/process/{listing-build,workflow}.md`,
`scripts/load_review_findings.py`, and renamed `scripts/mcp_hopc560.py` →
`scripts/mcp_hopc866.py` with its internal env vars updated.

**Why it matters:** `DB_SERVER` is the *default* used when `.env` doesn't override
it, so any deployment relying on the code default pointed at a server name that no
longer matched the operational source of truth. `HOPC866` is now also hardcoded as
`dev_sync_service._DEFAULTS["source_server"]` and `link_server_name`, so the Dev
Sync PROD→DEV direction depends on this name being right.

### 5.7 ORM models

Exactly **one** new ORM model: `UserSession` in `backend/app/models/rbac.py` → table
`rbac_user_sessions` (id, user_id FK, username, `jti` UNIQUE+indexed, ip_address,
created_at, last_seen, is_active indexed, revoked_by, revoked_at).

Every other new table across all subsystems is **raw-SQL / self-healing DDL**, not
SQLAlchemy ORM — modules create their tables lazily via `ensure_*()` on first use,
with the numbered `.sql` scripts serving as documentation and explicit provisioning.

---

## §6 — Auth / RBAC

Three distinct changes, all dated 31-Jul-2026 in the code comments.

### 6.1 Server-side session registry + single-login

- New table `rbac_user_sessions` (§5.7).
- `backend/app/services/auth_service.py::login` mints a `jti` (uuid4 hex) that rides
  inside **both** the access and refresh tokens, and inserts a `UserSession` row
  with the client IP. If business rule `SEC_SINGLE_LOGIN` is **ACTIVE**, all the
  user's other active sessions are revoked with `revoked_by='SINGLE_LOGIN'`. Default
  is INACTIVE = unlimited concurrent logins (legacy behaviour). Session bookkeeping
  failure never blocks a login.
- `backend/app/security/dependencies.py::get_current_user` rejects a token whose
  session row is explicitly `is_active = False` with 401 *"Session terminated —
  signed in on another device or revoked by an admin"*. Deliberate failure policy:
  **fails OPEN on infra errors** (never lock the app out on a DB blip), **fails
  CLOSED on an explicitly deactivated session**. Legacy tokens with no `jti` stay
  valid until expiry (migration grace).
- `refresh_token` honours the same check — a revoked session cannot be resurrected
  via refresh.
- New endpoints in `backend/app/api/v1/endpoints/auth.py`: `POST /auth/logout`
  (deactivates the calling token's session), `GET /auth/sessions?active_only=`
  (**SUPER_ADMIN**, max 500 rows), `DELETE /auth/sessions/{session_id}`
  (**SUPER_ADMIN** revoke).

### 6.2 Module-access permissions (`MOD_*`)

`seed_permissions_if_needed` appends **18** `MOD_*` permissions (module
`module_access`, action `READ`, resource `module`): `MOD_ARS_DASHBOARD`,
`MOD_ALC_REVIEW`, `MOD_DATA_MGMT`, `MOD_LISTING_ALLOC`, `MOD_GRT_ALC`, `MOD_ADHOC`,
`MOD_CONTRIB`, `MOD_AUTO_CONT`, `MOD_ALC_FIXTURE`, `MOD_TRENDS`, `MOD_REPORTS`,
`MOD_SAP`, `MOD_FA_CONS`, `MOD_PEND_ALC`, `MOD_DATA_VALIDATION`,
`MOD_PROJECT_TRACKER`, `MOD_TRAINING`, `MOD_SETTINGS`.

Granting/revoking one `MOD_*` shows/hides a whole sidebar father-menu; fine-grained
permissions still gate individual pages and actions inside.

**Visibility-preserving migration:** a brand-new `MOD_*` is granted to all active
roles once (`granted_by='SYSTEM_MODULE_SEED'`), so no module disappears for anyone
the day the gate lands. This runs only for perms created in that same startup, so it
never re-grants after an admin revokes.

> **Enforcement gap — see §9.4.** `MOD_*` is checked only in the frontend sidebar.

### 6.3 Per-endpoint gates on the new modules

| Module | Gate |
|---|---|
| Dev Sync | `_require_superadmin` on **every** route |
| Activity Log | SUPER_ADMIN check inside both handlers |
| Business Rules | `RequireRoles(["SUPER_ADMIN"])` on all 3 routes |
| WhatsApp Config | `RequirePermissions(["ADMIN_SETTINGS"])`; token change additionally SUPER_ADMIN |
| SAP | reads open to any authenticated user; writes/preview ADMIN or SUPER_ADMIN; `PUT /sap/connection` SUPER_ADMIN |
| Snowflake Config | `GET` open; `PUT` SUPER_ADMIN; test/tables ADMIN-or-SUPER_ADMIN |
| UPC Store Tracking | only `POST /reset` is SUPER_ADMIN; everything else any authenticated user |
| **Report Generation** | **no role or permission gate at all** |
| **FA & CONS** | **no role or permission gate at all** |
| **Data Dictionary** | **no role or permission gate** |
| **Release Notes** | **no role or permission gate** |

---

## §7 — Frontend

132 frontend files changed, +15,619 / −3,535, plus ~51 new PNG screenshots.

### 7.1 Routes added (`frontend/src/App.jsx`)

| Module | Route | Component |
|---|---|---|
| Data Mgmt | `/data-dictionary` | `DataDictionaryPage` |
| Listing | `/data-prep/listing/run-params` | `RunParamsPage` |
| Training Manual | `/manual` → `/manual/start`, `/manual/gallery`, `/manual/:module` | `ManualGalleryPage`, `TrainingManualPage` |
| Reports | `/reports/generation`, `/reports/upc-tracking` | `ReportGenerationPage`, `UpcStoreTrackingPage` |
| Admin | `/release-notes` | `ReleaseNotesPage` |
| SAP | `/sap/{connection,pulls,explorer,runs}` | 4 pages |
| FA & CONS | `/fa-cons/{mbq-master,sloc-settings,stock,store-list,allocation,pending,gap-report,help}` | real pages |
| FA & CONS | `/fa-cons/{project-store,consumables}` | `FaConsPlaceholderPage` |
| Bin Alloc | `/bin-alloc/{bin-master,requirement,run,results,log}` | `BinAllocPlaceholderPage` |
| Bin Alloc | `/bin-alloc/help` | `BinAllocHelpPage` |
| Settings | `/settings/{business-rules,activity-log,dev-sync}` (superadmin) | 3 pages |

**Routes removed:** `/process` (previously → `/process/overview`, now → `/manual/start`)
and `/process/:slug` (`ProcessPage` deleted; back-compat `Navigate` to
`/manual/start` — see §9.2).

Other `App.jsx` changes: `<FaConsBusyBar />` mounted at app root outside `<Suspense>`.
`main.jsx` gains a `ToasterBoundary` error boundary around `<Toaster>` — a non-string
toast payload (a FastAPI `detail` object) previously crashed the root and blanked the
whole app (documented incident on `/pend-alc/adhoc-close`, 03-Aug-2026).
`Layout.jsx` persists sidebar collapsed state to `localStorage`
(`ars_sidebar_collapsed`).

### 7.2 Sidebar (`frontend/src/components/layout/Sidebar.jsx`, +312/−171)

Structural rewrite: fifteen-plus hand-written `<SubMenu>` blocks replaced by a
single `SECTIONS` registry array that drives rendering, accordion state, route
detection and keyboard nav.

- **Accordion**: at most one section open; persisted to `localStorage`
  (`ars_sidebar_open_section`); navigating into a section auto-opens it.
- **Module permission gate**: each section carries a `MOD_*` permission; superadmin
  bypasses.
- **Full keyboard a11y**: arrow / Home / End roving focus, ArrowRight expands,
  ArrowLeft collapses or jumps to parent, Escape closes; collapsed-mode flyouts are
  `role="menu"` with focus management, viewport clamping, hover-close debounce and
  single-flyout-at-a-time enforcement.
- Active-route dot indicator on collapsed sections; active link scrolled into view.

**Sections added:** `GRT ALC` (Bin Allocation), `SAP`, `FA & CONS`.
**Section renamed and fully re-populated:** `Process` → `Training Manual`.

| Removed (Process, 13 items on `/process/*`) | Added (Training Manual, 10 items on `/manual/*`) |
|---|---|
| Overview, Workflow Chart, Listing (intro), Listing Build 1-5, Stage A · Rank, Stage B · Explode, Stage C · Waterfall, Stage D · Finalize, Primary & Sec-Cap, Allocation, Pending Allocation, Fallback (archived), Variables Glossary | Screenshot Gallery, Getting Started, Step 1 · MSA Stock, Step 2 · Grid Builder, Step 3 · Merge Rules, Step 4 · Listing & Alloc, Step 5 · Review Results, Step 6 · Hold Process, Step 7 · Pending Allocation, Data Dictionary |

Also added: Data Management → Data Dictionary; Reports → Report Generation, UPC
Store Tracking; Settings → Business Rules, Daily Activity Log, Dev Sync, What's New
(all superadmin-only).

Note: `/fa-cons/project-store` and `/fa-cons/consumables` are routed but never
linked from the sidebar — reachable by URL only.

### 7.3 New pages

**FA & CONS.** `FaConsSlocSettingsPage` (pick which SLOCs feed the Store-stock
bucket vs the MSA/DC pool per stream), `FaConsStockPage` (runs the dedicated
FA/CONS MSA + store-stock calc; 1000-row render cap over a fully loaded dataset),
`FaConsMbqMasterPage` (805 lines — MBQ target master per store × reference article,
single Excel upload auto-segregated FA/CONS by DIV, dry-run, per-row edit with
mandatory reason, change-review + session history), `FaConsStoreListPage`,
`FaConsAllocPage` (with a modal to manually pick actual articles behind a reference
article), `FaConsPendPage`, `FaConsGapPage`, `FaConsHelpPage`,
`FaConsPlaceholderPage`.

**Bin Allocation / GRT ALC.** `BinAllocHelpPage` (BRD/FSD for the greedy best-fit
bin→store engine — bins never split; eligibility % = matched qty ÷ bin qty; fixed vs
cascading thresholds; MAJ_CAT / SIZE / MAJ_CAT+SIZE levels), `BinAllocPlaceholderPage`.

**Documentation / Training.** `TrainingManualPage` (four tabs per module —
Overview=BRD, Rules=FSD + recorded rules, Manual=step cards, Gallery),
`ManualGalleryPage`, `guide/MarkdownDoc.jsx` (GFM + mermaid renderer, "recovered
from the retired ProcessPage"), `guide/guideSteps.js` (9 modules × step cards),
`DataDictionaryPage`.

**SAP.** `SapConnectionPage`, `SapPullsPage` (CRUD across the three doors),
`SapExplorerPage` (ad-hoc read-only preview, never lands data; field picker with
autocomplete chips + visual WHERE builder), `SapRunsPage`,
`SnowflakeConnectionPage` (no route — rendered only as Settings → Snowflake).

**Reporting / Ops / Admin.** `ReportGenerationPage` (1,230),
`UpcStoreTrackingPage` (1,346), `RunParamsPage` (544 — read-only audit of every
tunable that produced a listing run, with an N-way Compare tab up to 6 runs and a
Trends tab), `ReleaseNotesPage`, `BusinessRulesPage` (rules whose backend wiring is
pending render read-only "wiring pending" so the toggle never lies),
`DailyActivityLogPage`, `DevSyncManagerPage`.

**New shared components.** `components/facons/ColumnFilter.jsx` (shared faceted
column-filter popover; filter shape `{sel, q, mode}`),
`components/facons/FaConsBusyBar.jsx` (full-screen loader driven by the in-flight
`/fa-cons/` request counter in `api.js` — no per-button wiring),
`components/facons/HelpPage.jsx` (reusable help shell with scroll-spy),
`components/sap/WhereBuilder.jsx`, `store/sapUiStore.js` (zustand — the **second**
global store in the app, previously only `authStore`), `utils/sapWhere.js` (mirrors
`build_where_clause` in `sap_pull_service.py`; must be kept in sync by hand).

### 7.4 Retired docs and their replacement

**Deleted:** `frontend/src/pages/ProcessPage.jsx` (270 lines) and all 13
`frontend/public/docs/process/*.md` (~2,344 lines) — `overview`, `workflow`,
`listing`, `listing-build`, `stage-a-rank`, `stage-b-explode`, `stage-c-waterfall`,
`stage-d-finalize`, `sec-cap`, `allocation`, `pending-alc`, `fallback`, `variables`.

**Replaced by a three-part manual system:**

1. **Dossiers** `frontend/public/docs/manual/*.md` — 13 new files (~2,283 lines):
   `start`, `msa`, `grid`, `merge`, `listing`, `review`, `hold`, `pendalc`,
   `dictionary`, `fa_cons`, `bin_alloc`, `dev_sync`, `upc_tracking`. Fixed structure:
   `## BRD — Why this exists`, `## FSD — How it works`, `## Recorded rules`.
2. **~51 screenshots** under `frontend/public/docs/guide/<module>/step-NN-name.png`,
   captioned in `guide/guideSteps.js`.
3. **Viewers** — `TrainingManualPage` and `ManualGalleryPage`.

**Coverage change worth noting.** The old set had dedicated per-stage documents
(Stage A/B/C/D, Listing Build 1-5, sec-cap, variables glossary, workflow chart). The
new set has none; that material now lives condensed inside `manual/listing.md`'s FSD
+ "Recorded rules (engine)" section and in the repo-side `docs/ARS_BRD_END_TO_END.md`
/ `docs/RULE_MASTER.md`, which are **not** served to the app. The `variables`
glossary is functionally succeeded by the DB-backed Data Dictionary page. Whether
every rule survived the condensation was not verified line by line.

### 7.5 Modified page behaviour

**`ListingPage.jsx` (+795/−273) — the largest single change**
- **Engine simplification:** the `allocation_mode` radios (Pandas / Per-OPT /
  Sequential) and the Order A/B `exec_order` selector are **removed**. The payload
  sends `allocation_mode: 'per_opt'` explicitly. Workers + Writer-Queue toggles
  remain.
- **Collapsible page shell:** four `<Section>` blocks — Key Numbers, Run Setup,
  Tunable Parameters, Insights & Charts — with per-browser persisted collapse state
  (`ars_listing_hidden_sections`); Insights starts collapsed because the charts are
  heavy.
- **Store / MAJ_CAT search rewritten:** `SearchSelect` takes `labels` (code → name),
  `extra` (searchable "RDC HUB" or "SEG DIV SUB_DIV SSN" tokens) and `groups`.
  Typing an RDC/HUB or SEG/DIV code surfaces bulk select/deselect rows. Includes a
  match-ranking fix (exact → prefix → substring, shorter code wins; cap 6 → 8) for a
  bug where a short query like "s" pushed the exact `SSN:S` group off the list.
- **Run dates (information-only):** a **mandatory** Stock-consider date (defaults to
  D-1) and an **optional** Picking date, stamped onto every output row
  (`ARS_ALLOC_WORKING → PARKED → HISTORY`); never used in any calculation.
- **BDC-driven store auto-select:** changing the Picking Date auto-loads the store
  selection from `ARS_STORE_BDC_SCHEDULE` (same `get_stores_for_date` source as
  Reconciliation's Generate BDC). Sunday/no schedule clears; only codes present in
  the run config are kept; a blank date is a no-op so page load never clobbers a
  manual selection.
- **Fresh/GRT typed pool selector** (defaults FRESH).
- **Hold-suppression toggles (FS-05/FS-06):** three `skip_hold_seg_*` checkboxes
  (unchecked = hold applied); the payload **inverts** them into the backend's
  `apply_hold_seg_*` contract. Initial state derives from the pool: FRESH ⇒ skip
  hold on UPC + GM; GRT ⇒ skip on all three.
- **Dispatch mode:** a single RL/TBC knob (sent as both `rl_dispatch_mode` and
  `tbc_dispatch_mode`), **locked to COMPLETE**; SCALED disabled and any stale saved
  `SCALED` normalised.
- **CONT fallback for `SZ_APPLICABLE='N'`:** `P3_FNL_Q` ("Based on Stock") and
  `P4_UNIFORM` ("Uniform 1/N", default); legacy persisted `STRICT` silently upgraded.

**`MSAStockCalculationPage.jsx`** — new Warehouse SLOC Pools (Fresh/GRT) panel with
dirty tracking, bulk save of only dirty rows, and a Sync button; per-pool FRESH/GRT
count tiles from `ALLOC_TYPE`; stored sequences now load on page open (previously
only after a successful calc, so the panel wrongly showed "No stored sequences yet");
a defensive envelope check that reports the approximate raw MB instead of a
`TypeError` on a truncated response.

**`HoldDashboardPage.jsx` (+270/−93)** — click-to-filter drill-down across every KPI
card, chart bar, pie slice and top-article row (`drillTo()`, toggles off when
re-clicked); active drill-downs surface as removable chips; export reads the same
filter state so a drill-down exports exactly what's on screen. Allocation-type filter
pills `FRESH` / `GRT` / `LEGACY` applied to KPIs, breakdowns, detail and export, plus
a permanent split row from `summary.by_type`. Visual restyle to a flat
white/hairline surface.

**`SettingsPage.jsx` (+319/−108)** — four new tabs (WhatsApp, SMS, Snowflake, SAP;
the latter two render their pages `embedded`); deep-linkable tabs via `?tab=`; a full
WhatsApp Meta Cloud API tab with Configuration / Status / Testing sections.
**Removed:** the Parking mode toggle and the audit-mode toggle — both moved to
Settings → Business Rules as `ALC_MULTI_PARKED` / `LST_AUDIT_ALL_TO_WORKING`; the
Application tab now shows a pointer card.

**`PendAlcRecoPage.jsx` (+221/−44)** — new "Dispatch Control — Held from BDC" panel
showing open pending lines that Generate BDC will skip because they match
`ARTICLE_HOLD` / `DIV_DELETE` / `STORE_MAJCAT`, with clickable per-rule summary tiles
and a dedicated CSV export (explicitly distinct from the MSA gap). Typed pend filter
+ `alloc_type` column. Poll resilience: `POLL_ERROR_TOLERANCE = 5` consecutive
errors tolerated before declaring a BDC-generate job failed — a transient 502/504 or
a 404 from a gunicorn worker that doesn't own the job no longer aborts a generate
whose stamps already committed.

**`PendingDeliveryOrderPage.jsx` (+128)** — optional `Alloc_Type` column in the DO
upload (several header spellings accepted) scoping the FIFO deduction to matching
typed PEND rows; FS-12 `deductionMethod` selector where non-FIFO methods require
naming a target allocation session (options lazy-loaded from `/pend-alc/sessions`,
deduped to one per `SESSION_ID`), with a client-side guard mirroring the backend 400.

**`ManualPendAlcPage.jsx` (+71)** — typed pend support: `alloc_type` parsed from the
CSV (several aliases), per-row select override, page-level default for rows without
their own value; invalid values fall back to the page default.

**`PendingAllocationPage.jsx` (+24)** — typed pend filter plus a sortable `TYPE`
column.

**`GridBuilderPage.jsx` (+279)** — new Sec-Cap Growth Matrix panel (collapsed by
default): an editable cont%-band table with a global on/off toggle. Client-side
validation enforces sorted, contiguous, exactly-one-null-hi bands and
`growth >= 100`, with a monotonicity warning and a live preview for a test cont%.

**`StoreBdcSchedulePage.jsx`** and **`components/DataGrid.jsx`** are net removals —
see §9.1.

### 7.6 API client delta (`frontend/src/services/api.js`, +260/−12)

**New exported groups (11):** `faConsAPI` (~35 methods across SLOC settings, stock,
MBQ master, store list, allocation, pending, gap report), `sapAPI`,
`snowflakeConfigAPI`, `reportGenAPI`, `upcTrackAPI`, `devSyncAPI`,
`dataDictionaryAPI`, `releaseNotesAPI`, `businessRulesAPI`, `activityLogAPI`,
`whatsappConfigAPI`.

**Additions to existing groups:** `msaAPI` gains `slocSettings`,
`updateSlocSetting`, `bulkSlocSettings`, `syncSlocSettings`; `gridBuilderAPI` gains
`getGrowthMatrix` / `saveGrowthMatrix`; `listingAPI` gains `runParams(sid)`,
`runParamCatalog()`, `runParamTrend(names, limit)`; `holdDashboardAPI`'s
`summary`/`byStatus`/`byAge` now accept a `params` object for the `alloc_type` filter
(previously zero-arg); `pendAlcAPI` gains `dispatchGap` + `exportDispatchGap`, and
`closeRowsFile` gains a `confirmCloseAllStores` flag mirroring the JSON endpoint's
guard (rows with a blank `ST_CD` close every store for the RDC+ARTICLE, so the
backend 400s unless the caller opts in).

**Removed:** `pendAlcAPI.recoSuggest` — see §9.1.

**New non-API export `faBusy`:** a module-level in-flight counter over all
`/fa-cons/` requests, installed in the axios interceptors. `_faLabel(config)` maps
the URL to a human label (`Calculating stock & MSA…`, `Uploading MBQ…`, …);
`<FaConsBusyBar/>` subscribes to it.

### 7.7 `frontend/vite.config.js`

Three changes: dev port is now `process.env.PORT ?? 3000`; `strictPort: false` added
so it falls forward to a free port instead of failing; and the `/api` proxy
`proxyTimeout` raised **600000 → 1200000 ms** (10 → 20 minutes), consistent with the
long-running FA & CONS / report / dev-sync jobs.

---

## §8 — Docs & tooling

### `tools/manual/`

- **`capture_steps.js`** (303) — puppeteer-core step-capture harness. Per step:
  navigate → act (fill/click/scroll, never destructive) → inject a red outline +
  numbered badge on the target → screenshot (cropped). Manifest at the top via an
  `S(module, step, name, route, opts)` helper; supports `noAuth`, `wait`,
  `highlight` (CSS selector *or* `{sel, text}`), `clip`/`clipPad`, `scrollY`, `pre:`
  action lists and `inject:` fixtures. Login injected through `/auth/login`. Steps
  that would mutate data (Generate, Approve, DO entry) only *highlight* the button.
  See §9.5 for its hardcoded path.
- **`doc_drift_hook.mjs`** (45) — a Claude Code `PostToolUse` hook (Edit/Write/
  MultiEdit) wired in `.claude/settings.json`. Reads the tool event from stdin, maps
  the edited backend path to a module via a 7-rule regex table, and emits
  `hookSpecificOutput.additionalContext` reminding the agent to update
  `frontend/public/docs/manual/<module>.md` and keep `ARS_DATA_DICTIONARY` in sync.
  It only reminds, never edits, and always exits 0.
- **`REFRESH.md`** (47) — the maintenance contract for the manual: what goes stale
  and how to refresh each (screenshots → `node tools/manual/capture_steps.js`; column
  formulas → a code-sweep that upserts `ARS_DATA_DICTIONARY` rows tagged
  `updated_by='code-sweep'`; rules → hand-edit the dossier; step captions →
  hand-edit `guideSteps.js`).

### `docs/` (repo-side, not served to the app)

- **`ARS_BRD_END_TO_END.md`** (903) — consolidated end-to-end BRD (ingest → MSA →
  grid → merge → listing → allocation → review → hold/pending → BDC/DO to SAP).
  Declares its source of truth as `frontend/public/docs/manual/*.md` +
  `docs/RULE_MASTER.md` + engine code, and states explicitly that where the BRD and
  code disagree, the code and dossiers win. Status: "July 2026 code state — 12-step
  MSA, per_opt-only engine, typed FRESH/GRT pools".
- **`RULE_MASTER.md`** (464) + `RULE_MASTER.docx` — a rule-audit reference across
  MSA, Grid, Listing, Allocation, Hold/Pend.
- **`build_rule_master.py`** (670) — python-docx + openpyxl generator rendering
  `RULE_MASTER.docx` and `.xlsx` from rule data embedded as Python literals in the
  script itself. That makes it a **second copy** of the rules, kept in sync with
  `RULE_MASTER.md` by hand.
- **`DEV_SYNC_PLAN.md`** (220) — the design doc behind `DevSyncManagerPage`. Still
  marked "FOR REVIEW — nothing implemented yet" (20-Jul-2026), which now predates the
  page shipping.
- **`docs/obsidian/`** — a full Obsidian vault (~30 module notes, daily notes, 4
  templates, 3 design specs), including `Known Risks and Doc Drift` — a dated
  register of 7+ open correctness risks with file pointers. Several notes are empty
  placeholders (0 bytes): `Bin Allocation module.md`, `Untitled.md`,
  `dispatch-complete-only.md`, `run-dates-metadata.md`,
  `rounding-and-data-dictionary.md`, `reference_snowflake_sap_bronze_lake.md`.

---

## §9 — Regressions and risks

### 9.1 Local work that never reached `ars_v2_prod`

`ars_v2_prod` forked at `ab3d085` on 19-Jun and **never merged PR #26 (`453476f`) or
PR #27 (`3c0329a`)**. Those commits' content is absent from prod:

| File | What prod is missing | Evidence |
|---|---|---|
| `frontend/src/pages/StoreBdcSchedulePage.jsx` | The "Export current view to CSV" button and its `exportCsv()` implementation (plus the `FileDown` icon import). Import and Download-template remain; no replacement export exists. | `git diff HEAD origin/ars_v2_prod -- <file>` is `+1 / −36`, deletions only |
| `frontend/src/components/DataGrid.jsx` | `TextFilterPopover` and the entire `col.suggester` typeahead contract — debounced fetch, request-id race guard, arrow-key highlight, Enter-to-commit. Text filters fall back to a plain inline "contains…" input. | `+25 / −126` |
| `frontend/src/services/api.js` | `pendAlcAPI.recoSuggest` (the `/pend-alc/reco-suggest` distinct-value autocomplete) | removed in prod |
| `frontend/src/pages/PendAlcRecoPage.jsx` | the `suggestCol` adapter and `GRID_FILTER_MAP`; `clr` and `do_number` lost `filterType:'text'` | removed in prod |
| `backend/app/services/rule_engine_per_opt.py` | The one-size R07 short-circuit (`if total_sizes > 1:`). Without it, a one-size TBL OPT whose live pool was drained by earlier shipping OPTs is stamped `R07_SIZE_RATIO_LIVE(0/1)` — which reads as a size-mix failure rather than "warehouse empty". | §3.14 |
| `GIT_WORKFLOW.md` | deleted entirely (264 lines) | `+0 / −264` |

The DataGrid/`recoSuggest` group is internally consistent — prod removed the whole
typeahead feature, not half of it. The R07 guard and the BDC export are the two that
look like genuine losses.

### 9.2 Deep-link loss on the retired Process docs

The `/process/:slug` back-compat route is a `Navigate` to `/manual/start` that
**discards the slug**. Every old bookmark or shared link — `/process/stage-c-waterfall`,
`/process/variables`, etc. — lands on the manual start page rather than the
equivalent module.

### 9.3 Four modules ship with no authorization gate

Verified by grep across `origin/ars_v2_prod`: `report_gen.py`, `facons.py`,
`data_dictionary.py` and `release_notes.py` contain **zero** occurrences of
`RequireRoles`, `RequirePermissions`, `superadmin` or `SUPER_ADMIN`. Every route is
plain `Depends(get_current_user)`.

Practical consequence: any authenticated user can create, edit and delete report
definitions and delete run history; run and modify FA & CONS allocations; and edit
or delete Data Dictionary and Release Notes entries.

### 9.4 `MOD_*` is a UI gate, not an API boundary

`MOD_*` appears in `backend/app` **only** in the seed list inside
`backend/app/services/auth_service.py` (23 lines, one file). No endpoint anywhere in
the backend checks a `MOD_*` permission. Enforcement lives entirely in
`frontend/src/components/layout/Sidebar.jsx`.

Revoking `MOD_FA_CONS` from a role therefore hides the menu but leaves every
`/fa-cons/*` endpoint reachable by direct call. Module gating is presentation-level.

### 9.5 Hardcoded environment-specific values

- `backend/app/services/snowflake_config_service.py` ships non-secret defaults in
  `_DEFAULT` including a developer's Windows private-key path under
  `C:\Users\santosh.kumar3\.snowflake\`, plus account, user, role, warehouse and
  database.
- `tools/manual/capture_steps.js` has a hardcoded absolute Windows output path
  (`D:/ARS_PROD/ars_prod/frontend/public/docs/guide`) and targets `localhost:3000` +
  `127.0.0.1:8000`.
- `backend/app/core/crypto.py` will **generate an encryption key and append it to
  `.env`** if `APP_ENC_KEY` is absent. Convenient in dev; in a multi-node deploy each
  node would mint its own key and be unable to decrypt the others' secrets.

### 9.6 Migration hygiene

- Numbering collides — there are **three `017_*` files**
  (`017_msa_alloc_type_rows.sql`, `017_report_generation.sql`, and the pre-existing
  `017_add_module_permissions.sql`) — and slot `020` is skipped.
- Several tables have **no numbered migration at all** and rely entirely on runtime
  `ensure_*()` DDL: `SNOWFLAKE_CONNECTION`, `ARS_SF_WATERMARKS`,
  `ARS_BUSINESS_RULES` + `_LOG`, `ARS_DATA_DICTIONARY`, `ARS_RELEASE_NOTES`,
  `APP_WHATSAPP_SETTINGS` + `_AUDIT_LOG`, `ars_daily_activity_validation`,
  `rbac_user_sessions`, and most `ARS_FACONS_*` tables.
- `ARS_REPORTS`' `SPLIT_CONFIG` / `EMAIL_CONFIG` / `WHATSAPP_CONFIG` / `SMS_CONFIG` /
  `FOLDER_PER_RUN` columns exist only in the self-heal path, not in migration 017.

### 9.7 Configured but not implemented

An `sms` block exists in `app_settings.json` defaults and an `SMS_CONFIG` column
exists on `ARS_REPORTS`, but there is no SMS delivery service file. The path is
configurable and does nothing.

### 9.8 Documentation drift found during this sweep

- `scripts/024_upc_store_tracking.sql`'s header says metrics come from the latest
  `TREND_ST` row per store; `upc_store_track_service.py` reads `ARS_GRID_MJ`. The
  service is what runs.
- `backend/scripts/025_facons_foundation.sql` defines `ARS_FACONS_SLOC_SETTINGS`, but
  `facons_stock_service.py` now treats it as `LEGACY_SLOC_TBL` and folds it into the
  listing SLOC tables `ARS_STORE_SLOC_SETTINGS` / `ARS_MSA_SLOC_SETTINGS`. The
  migration and the running model have diverged.
- `docs/superpowers/specs/2026-07-13-sec-cap-hard-block-precedence-design.md` is
  referenced by `rule_engine_per_opt.py` and by the test file but **is not present**
  in the branch.
- `snowflake_sync.py`'s docstring still references `SF_ACCOUNT` / `SF_USER` /
  `SF_PASSWORD` settings and reads a fallback config from `app_settings.json`,
  inconsistent with the new DB-backed `SNOWFLAKE_CONNECTION`.
- `docs/2026-07-02-cont-fallback-ladder.md` (Rule 3: "uniform 1/N has been removed")
  describes an intermediate state; the shipped default `P4_UNIFORM` reinstates
  uniform fill (§3.19).
- `docs/DEV_SYNC_PLAN.md` is still marked "nothing implemented yet" although the page
  has shipped.

### 9.9 Committed junk

Present in `ars_v2_prod`, should be gitignored:

```
.tmp_per_opt_jun26.txt
docs/~$26-07-02-cont-fallback-ladder.docx     # Word lock file, 162 bytes
~$S_BRD_V2.docx                               # Word lock file, 162 bytes
```

(`.tmp_audit_deepdive.md` exists on both branches.)

Also: `docs/obsidian/.obsidian/themes/{AnuPpuccin,Obsidian Nord,Sodalite,Wasp}/theme.css`
is ~10,900 lines of vendored third-party CSS, roughly 20 % of the diff's insertions.

---

## §10 — Merge-forward recommendation

`ars_v2_prod` is unambiguously the newer and more capable implementation; it should
be the base going forward. The question is only what to salvage from the four
local-only commits.

**Worth cherry-picking into `ars_v2_prod`:**

1. **The one-size R07 short-circuit** in `rule_engine_per_opt.py` (from `3c0329a`).
   Restores `if total_sizes > 1:` around the live size-ratio gate. Small, local, and
   fixes a genuinely misleading audit stamp on one-size TBL OPTs. Needs re-basing by
   hand — the surrounding block was substantially rewritten in prod (§3.9–3.11), so
   this is not a clean cherry-pick.
2. **The BDC Schedule CSV export** in `StoreBdcSchedulePage.jsx` (from `453476f`).
   Self-contained (`exportCsv()` + one button + one icon import), no backend
   dependency, and its columns mirror the same CSV pipeline used for import.

**Verify intent before restoring:**

3. **The `DataGrid` typeahead** (`TextFilterPopover` / `col.suggester`) and its
   companions `pendAlcAPI.recoSuggest` and `PendAlcRecoPage`'s `suggestCol`. Prod
   removed all four together, which reads as a deliberate feature removal rather than
   a merge accident. Confirm with the author before reinstating.

**Do not restore:**

4. **`GIT_WORKFLOW.md`** — superseded by the manual/Obsidian documentation system,
   unless the branch conventions it recorded are still in force.
5. The `listing.py` / `pend_alc.py` / `rule_engine_new.py` changes from the local
   commits — those files were rewritten far more extensively in prod, and the local
   versions' concerns appear addressed there.

**Recommended next actions, in priority order:**

1. Add role gates to Report Generation, FA & CONS, Data Dictionary and Release Notes
   (§9.3) — this is the highest-impact item and is purely additive.
2. Decide whether `MOD_*` should become a real backend dependency or be documented
   as UI-only (§9.4).
3. Move `snowflake_config_service._DEFAULT` and `capture_steps.js`'s output path into
   configuration (§9.5).
4. Cherry-pick items 1 and 2 above.
5. Gitignore the Word lock files and `.tmp_*`, and reconcile the migration numbering
   (§9.6, §9.9).
