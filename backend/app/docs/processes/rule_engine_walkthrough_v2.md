---
title: Rule Engine — Stage-by-Stage Technical Review (pandas path)
category: Allocation
order: 13
source: backend/app/services/rule_engine_pandas.py, backend/app/services/rule_engine_new.py
last_reviewed: 2026-05-17
audience: backend developers
---

# Rule Engine — `rule_engine_pandas.py` Stage-by-Stage Technical Review

**Scope.** Pandas Stage-C engine only (`backend/app/services/rule_engine_pandas.py`,
2,204 lines). Stages A, B, D and the secondary-grid cap helper are imported
from `rule_engine_new.py`; this doc covers them only at the contract boundary.
Reference is mostly file:line in `rule_engine_pandas.py` unless prefixed `rne:`.

**SQL evidence** comes from the local HOPC560 SQL Server (rep_data DB) using
the project SQLAlchemy engine (`get_data_engine`). All numbers below are from
the *current* state of `ARS_ALLOC_WORKING` / `ARS_LISTING_WORKING` (latest
pandas run). The MAJ_CAT used as the running example is **M_TEES_PN_HS** —
the same one the previous session flagged.

**Status.** Review-only. No code changes have been made.

---

## Stage map (TOC)

| # | Stage | Function(s) | Lines |
|---|---|---|---|
| 1 | Public entry / orchestrator | `run_listing_and_allocation_pandas` | 376–878 |
| 2 | Stage A + B (delegated SQL) | `rne._stage_a_*`, `rne._stage_b_*` | 425–476 |
| 3 | Table load + type coercion | `_load_tables`, `_select_working_cols` | 884–1051 |
| 4 | Per-MAJ_CAT process pool dispatch | `_pandas_run_one_majcat`, writer queue | 79–370, 541–688 |
| 5 | Per-MAJ_CAT waterfall driver | `_run_majcat_waterfall` | 1134–1266 |
| 6 | Primary cap construction | `_build_mbq_budget`, `_live_mbq_budget` | 1057–1104 |
| 7 | Pre-band gate (PRI / store-broken) | `_pre_band_check`, `_propagate_skips_to_alloc` | 1656–1788 |
| 8 | Re-ranking between OPT_TYPEs | `_rerank_opt_priority_pandas` | 1269–1392 |
| 9 | Core band — pool draw + SHIP/HOLD | `_run_band` | 1395–1653 |
| 10 | Post-band revalidation | `_revalidate_after_band` | 1791–2026 |
| 11 | Finalise + Stage D + secondary-cap | finalise block in `run_listing_…` | 735–877 |
| 12 | Persistence | `_write_back_alloc`, `_write_back_working`, `_writer_thread_fn` | 280–370, 2041–2204 |

---

## Stage 1 — Public entry / orchestrator

### Inputs / Outputs / Invariants
- **Function:** `run_listing_and_allocation_pandas(...)` — lines 376–878.
- **Inputs (kwargs):** `working_table`, `listed_table`, `alloc_table`,
  `msa_var_table`, `var_grid_table`, `cont_table`, `n_workers`, `batch_id`,
  `only_majcats`, `size_threshold`, `min_size_count`, `tbl_trivial_factor`,
  `pri_ct_check_rl`, `pri_ct_check_tbc`, `rl_mbq_cap_pct`, `tbc_mbq_cap_pct`,
  `opt_types`, `use_writer_queue`, `apply_sec_cap_in_normal`.
- **No `tbl_mbq_cap_pct` kwarg.** Removed 2026-05-16 per the comment at 97–98.
  This is the structural root of the TBL bug — TBL has zero cap surface area
  at the public API level.
- **Outputs (dict):** `batch_id`, `listed_opts`, `alloc_rows`,
  `ship_qty_total`, `hold_qty_total`, `errors`, `duration_sec`,
  `deadlock_retries`, `done`, `failed`, `queue_summary`.
- **Invariants:**
  - **One OPT_TYPE per OPT** — `OPT_TYPE_ORDER = ["RL","TBC","TBL"]` (line 71)
    is iterated, and each MAJ_CAT row carries a single OPT_TYPE; the
    invariant is upheld upstream (Stage B).
  - **Growth at MJ+grid only.** Per-OPT_TYPE caps `rl_mbq_cap_pct` /
    `tbc_mbq_cap_pct` are conceptually different from the "growth %" that
    sits on `MJ_REQ` itself (computed upstream in `grid_calculations.py`).
    The engine then applies a *per-OPT_TYPE* ceiling *expressed against
    MJ_MBQ* — RL gets 130%, TBC gets 130%, TBL gets nothing. **This is
    the bug: the per-OPT_TYPE ceiling is partial — it only exists for two
    of three OPT_TYPEs, with no MJ-level total ship cap remaining.**

### Known bugs + proposed fixes

**B1 — No MJ-level ship cap on TBL (the original report, confirmed).**
- File:line: 1170–1198 (`_run_majcat_waterfall`) and 376–396 (signature).
- Code: `cap_pct_for_ot = eff_rl_cap if ot == 'RL' else eff_tbc_cap if ot == 'TBC' else 0.0`.
- Effect: with `cap_pct_for_ot = 0.0` for TBL, the gate at 1234–1237
  (`_live_mbq_budget` returns `{}`) and at 1564 (`if mbq_budget:`) is
  bypassed. TBL competes only against per-size `SZ_REQ` and the RDC pool.
  Because TBL rounds run *after* RL+TBC and TBL has the largest I_ROD
  (frequently 2 in M_TEES_PN_HS), it claims everything that RL/TBC left.
- Proposed fix (depends on user decision in Open Questions):
  - Option A (strict): hardcode `eff_tbl_cap = 100.0` and let TBL respect
    the live MJ_REQ_REM after RL+TBC have consumed their share. This is
    what `MEMORY.md` ("Growth at MJ+grid only") asks for.
  - Option B (growth-allowed): reintroduce a `tbl_mbq_cap_pct` kwarg with
    default 130 (matching RL/TBC default), wire it through `_pandas_run_one_majcat`
    args at 99–110 (the 16-tuple variant noted at 98 was retired — bring it
    back), and replace line 1197 with `eff_tbl_cap = tbl_mbq_cap_pct`.
  - Either option requires changing line 742 (post-run comment claims TBL
    has *no* MJ-cap intentionally) and Stage A's R09 in `rne._stage_a_apply_rules`
    line 333 (`WHEN 'TBL' THEN 1.0`) to match.

**B2 — `_live_mbq_budget` semantic mismatch with sec-cap MBQ rule.**
- File:line: 1078–1104.
- The cap is `max(0, MJ_REQ_REM + ((cap_pct−100)/100) × MJ_MBQ)`. Per the
  saved invariant "*_MBQ=0 means no constraint" — but here a store with
  `MJ_MBQ=0` *and* `MJ_REQ_REM=0` returns `budget=0`, blocking the store
  completely. This isn't strictly the sec-cap sparseness rule (that's for
  per-grid `*_MBQ` on FAB/MICRO_MVGR/etc., not the MJ aggregate), but
  worth a sanity check: a Primary grid with `GH_MJ=0` for a MAJ_CAT will
  still consume from this gate.
- The comment at lines 1082–1088 documents the math, which is internally
  consistent. The risk is **misapplication if anyone later borrows this
  formula for sec-cap grids** — sparseness invariant would be violated.

**B3 — `apply_sec_cap_in_normal=True` is a no-op in production today.**
- File:line: 819–847.
- The call to `rne._apply_sec_grid_cap_pre_gate` only fires for grids
  where `sec_cap_applicable=True` in `ARS_GRID_BUILDER`. SQL evidence
  shows **every active grid has `sec_cap_applicable=False`** (probe 3
  output). So even though `apply_sec_cap_in_normal=True` by default, the
  helper returns `audit["grids_evaluated"] = 0` and exits at
  `rne:2730–2739`. The cap is wired but disabled by data.
- This means TBL's blow-up cannot currently be caught by sec-cap either.

### SQL evidence (latest run, M_TEES_PN_HS)
```
=== M_TEES_PN_HS by OPT_TYPE ===
OPT_TYPE | rows  | ship    | hold   | total
TBL      | 16976 | 45972.0 | 8815.0 | 54787.0   ← 91% of MAJ_CAT ship
RL       |  2138 |  4334.0 |    0.0 |  4334.0
TBC      |   634 |  1300.0 |    0.0 |  1300.0

=== HU45 ships per OPT_TYPE (MJ_MBQ=2874, MJ_REQ=138, MJ_STK_TTL=2736) ===
WERKS | OPT_TYPE | store_ship
HU45  | RL       |    52       ← respects cap
HU45  | TBC      |    54       ← respects cap
HU45  | TBL      |  1805       ← 13× MJ_REQ, 63% of MJ_MBQ

=== SEC_CAP-related SKIP_REASON counts ===
(no rows)                       ← sec-cap fired on zero TBL rows
```

### Suggested tests
```python
def test_tbl_mj_cap_respected(small_majcat_with_high_pool):
    """TBL ship must not exceed (1.30 × MJ_MBQ − MJ_STK_TTL − RL_ship − TBC_ship)."""
    result = run_listing_and_allocation_pandas(only_majcats=['M_TEES_PN_HS'])
    rows = sql("SELECT WERKS, SUM(SHIP_QTY) ship FROM ARS_ALLOC_WORKING "
               "WHERE MAJ_CAT='M_TEES_PN_HS' GROUP BY WERKS")
    for werks, ship in rows:
        mj = sql_one("SELECT MJ_MBQ, MJ_STK_TTL FROM ARS_LISTING_WORKING "
                     "WHERE WERKS=? AND MAJ_CAT='M_TEES_PN_HS'", werks)
        assert ship <= 1.30 * mj.MJ_MBQ - mj.MJ_STK_TTL + 1  # +1 slack
```

---

## Stage 2 — Stage A + Stage B (delegated SQL)

### Inputs / Outputs / Invariants
- Lines 425–476. Calls into `rne._stage_a_add_columns`, `_stage_a_apply_rules`,
  `_stage_a_assign_tier`, `_stage_a_assign_rank`, `_stage_a_materialize_listed`,
  `_stage_b_explode`, `_stage_b_fill_cont`, `_stage_b_fill_targets`,
  `_stage_b_indexes`, `_ensure_phase_reason_cols`, `_discover_primary_grids`,
  `_init_rem_columns`.
- **Skipped on retry** (`only_majcats is not None` branch at 477–481).
- **Invariants the pandas engine *expects* the SQL stages to uphold:**
  - `LISTED_FLAG=1` rows in `working_table` are eligible OPTs only.
  - Every row in `alloc_table` carries the sec-cap grid extras
    `FAB`, `MACRO_MVGR`, `MICRO_MVGR`, `M_VND_CD`, `RNG_SEG`
    so `_revalidate_after_band` can group by them at lines 1863–1875.
  - `MJ_REQ` reflects growth %. `MJ_REQ_REM` is seeded from `MJ_REQ`
    by `rne._init_rem_columns` (line 968).
  - OPT-uniqueness (one OPT_TYPE per WERKS×MAJ_CAT×GEN_ART×CLR) is
    materialized by Stage A — the pandas engine never re-checks.

### Known bugs + proposed fixes
- **B4 — Stage A's R09 cap factor uses `1.0` for TBL** (`rne:333`).
  This is consistent with the pandas TBL bug — TBL is "uncapped" both
  in pre-flight (R09) and in the live waterfall. If we fix B1 by adding
  a TBL cap %, R09 in `_stage_a_apply_rules` must change in lockstep
  or Stage A will list TBL rows that the new cap then immediately
  zeros (creating SKIPPED-with-SEC_CAP-style noise).

- **B5 — `_select_working_cols` (line 1032) does NOT load sec-cap extras
  from working into pandas memory unless they're tied to a grid in
  `_discover_primary_grids`.** Lines 1044–1050 iterate grids and append
  `req_rem`, `h_rem`, `gh_col`, and `meta.get('extras', [])`. `_discover_primary_grids`
  only returns *Primary* grids (`rne:906–907`). So FAB / MICRO_MVGR /
  MACRO_MVGR (some of which are Secondary) might not be on `working_df`
  during the pandas waterfall. They *are* loaded onto alloc_df via
  `SELECT *` at line 912 — so this is OK for `_run_band` (which only
  needs alloc columns), but `_revalidate_after_band` at 1851–1858 joins
  through working_df's `OPT_KEYS + extras` and silently drops decrements
  for grids whose `extras` aren't on working_df. (Today it doesn't hurt
  because Primary grids — MJ, MJ_MACRO_MVGR, MJ_RNG_SEG — *are* the only
  ones in `grids`. But it's brittle: enabling a new Primary grid with an
  extra not loaded will silently lose REQ_REM decrements.)

### SQL evidence
```
=== ARS_GRID_BUILDER contents ===
grid_name      | grp       | status | sec_cap_applicable
MJ             | Primary   | Active | False
MJ_MACRO_MVGR  | Primary   | Active | False
MJ_RNG_SEG     | Primary   | Active | False
MJ_CLR         | Secondary | Active | False
MJ_FAB         | Secondary | Active | False
MJ_M_VND_CD    | Secondary | Active | False
MJ_MICRO_MVGR  | Secondary | Active | False

=== sec-cap grid extras presence ===
table                | FAB MACRO MICRO M_VND RNG_SEG
ARS_LISTING_WORKING  |  1    1     1     1     1
ARS_LISTED_OPT       |  1    1     1     1     1
ARS_ALLOC_WORKING    |  1    1     1     1     1
```
Propagation invariant holds today.

### Suggested tests
```python
def test_alloc_table_carries_all_sec_cap_extras():
    cols = sql("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
               "WHERE TABLE_NAME='ARS_ALLOC_WORKING'")
    for ex in ('FAB','MACRO_MVGR','MICRO_MVGR','M_VND_CD','RNG_SEG'):
        assert ex in cols
```

---

## Stage 3 — Table load + type coercion

### Inputs / Outputs / Invariants
- **Function:** `_load_tables` lines 884–1029; `_select_working_cols` 1032–1051.
- **Determinism:** explicit `ORDER BY` on both reads (lines 903–910) so the
  mergesort tie-break in `_run_band` is reproducible.
- **Coercion:**
  - `OPT_PRIORITY_RANK`, `ST_RANK`, `IS_NEW`, `I_ROD`, `SZ_MBQ`, `SZ_MBQ_WH`,
    `SZ_STK`, `FNL_Q`, `POOL_CONSUMED`, `SHIP_QTY`, `HOLD_QTY`, `ROUND_*`,
    `ALLOC_ROUND`, `ALLOC_QTY` → numeric, NaN→0 (921–929).
  - `PRI_CT_REM` nulls → `100.0` ("uninitialized assume eligible", 974–976).
  - `MJ_REQ_REM` nulls → falls back to `MJ_REQ` (988–997). Good.
  - `ALLOC_STATUS` is reset to `PENDING` except `INELIGIBLE` (939–944).
  - Per-row accumulators (`SHIP_QTY`, `HOLD_QTY`, …) are zeroed for non-
    INELIGIBLE rows (951–959).
- **Pool key strings** are normalised to `str` so the dict-key match in
  `_run_band` is stable (964–966).
- **Grid `extras`** on working_df are stringified for the same reason
  (1010–1013).

### Known bugs + proposed fixes
- **B6 — Per-row reset of `SHIP_QTY` etc. can lose a successful prior run.**
  Lines 951–957 zero all accumulators unconditionally for non-INELIGIBLE
  rows, even when invoked on a partial-retry batch (`only_majcats != None`).
  In retry mode the operator's intent is usually "redo just these MAJ_CATs"
  — and we *do* only load `only_majcats` slices (885–893) — so this is OK
  *in practice*. But the comment at 935–938 says it explicitly mirrors a
  full reset; if anyone changes the load filter to be more inclusive in
  the future, this resets shipped quantities that already left a previous
  successful run. Worth a note.

- **B7 — `ALLOC_STATUS` is reset to `PENDING` even when prior run wrote
  `ALLOCATED`.** Same lines 941–944. Comment acknowledges this is
  deliberate, but: a stale `PENDING` for a row that was `SKIPPED` upstream
  for a *non-INELIGIBLE* reason (e.g. `R07_VAR_RATIO_TBL`) will be
  re-evaluated. Stage A's `LISTED_FLAG` should already filter most of
  these out before they reach Stage B, but worth confirming `SKIPPED`
  rows from previous bands in the same Stage B output don't accidentally
  re-enter.

### SQL evidence
```
=== ALLOC_STATUS x OPT_TYPE distribution (post-pandas) ===
OPT_TYPE | ALLOCATED | PARTIAL | SKIPPED
RL       |  1006     |   83    |  1049
TBC      |   230     |   30    |   374
TBL      |  6876     | 1017    |  9083
```
The 9,083 TBL SKIPPED rows are mostly low-coverage skips, not load issues.

### Suggested tests
```python
def test_load_tables_resets_accumulators():
    eng = get_data_engine()
    sql_set(eng, "UPDATE ARS_ALLOC_WORKING SET SHIP_QTY=99 WHERE MAJ_CAT='X'")
    alloc_df, _, _ = _load_tables(eng, 'ARS_ALLOC_WORKING', 'ARS_LISTING_WORKING',
                                  {'MJ_REQ': MJ_META}, only_majcats=['X'])
    assert (alloc_df.loc[alloc_df['ALLOC_STATUS']=='PENDING', 'SHIP_QTY']==0).all()
```

---

## Stage 4 — Process-pool dispatch + writer thread

### Inputs / Outputs / Invariants
- **Function:** `_pandas_run_one_majcat` (79–269) per MAJ_CAT; orchestration
  in `run_listing_and_allocation_pandas` lines 502–671. Optional single
  writer thread (`_writer_thread_fn`, 280–370) drained from a bounded
  `queue.Queue`.
- **Defer-writes mode** (`USE_WRITER_QUEUE`, lines 524–539): subprocess
  returns the computed DataFrames; the parent writer thread does all
  UPDATEs serially through one DB connection.
- **Hold-tracking load** (lines 148–169): reads
  `ARS_NL_TBL_HOLD_TRACKING WHERE IS_CLOSED=0 AND HOLD_REM>0` and shapes
  into `{(WERKS, VAR_ART, SZ): hold_rem}`. Comment at line 153 says
  "RL/TBC consume-from-hold logic" — confirmed at 1459–1500.
- **Invariants:** disjoint MAJ_CAT slices (line 14 module doc); each
  subprocess owns its frames; no shared mutable state.

### Known bugs + proposed fixes
- **B8 — Hold-tracking comment claims `(WERKS, VAR_ART, SZ)` PK** (153)
  but the dict-build at 161–163 also coerces `r[1]` (VAR_ART) and
  `r[2]` (SZ) to str — line 164's filter compares `str(r[1])` against a
  set of `astype(str)` VAR_ARTs from alloc, so that's consistent. OK.

- **B9 — The 16-tuple "tbl_mbq_cap_pct" variant was deleted but the
  unpacking guard (99–110) still permits an arbitrary trailing `*_extras`
  list.** This is forward-compatible but cosmetically confusing if a
  future caller passes a positional arg in the wrong slot.

- **B10 — Worker-side reset of `DeadlockStats` at line 118 means the
  parent's run-level deadlock summary at lines 698–706 is reconstructed
  from each worker's snapshot.** Look at line 248 vs writer-queue line
  328–332. Both paths sum into `dl_caught` etc. So this is consistent.

### SQL evidence
- The `ARS_NL_TBL_HOLD_TRACKING` table exists in rep_data; in the latest
  run, hold-draw rows are visible via `FROM_HOLD_QTY > 0`:
  ```
  SELECT COUNT(*), SUM(FROM_HOLD_QTY)
  FROM ARS_ALLOC_WORKING WHERE ISNULL(FROM_HOLD_QTY,0) > 0
  ```
  (Run this to confirm — wasn't included in initial probe because TBL
  doesn't draw from hold by design.)

### Suggested tests
- Integration test: spawn the pandas engine with 2 MAJ_CATs, then assert
  that `ARS_ALLOC_MAJCAT_QUEUE` rows transition `IN_PROGRESS → DONE` and
  no row stays `IN_PROGRESS` after `run_listing_and_allocation_pandas`
  returns.

---

## Stage 5 — Per-MAJ_CAT waterfall driver

### Inputs / Outputs / Invariants
- **Function:** `_run_majcat_waterfall` lines 1134–1266.
- **Order:** `OPT_TYPE_ORDER = ["RL","TBC","TBL"]` (71). RL all rounds →
  TBC all rounds → TBL all rounds.
- **Per-OPT cap selection** at 1194–1198:
  ```python
  cap_pct_for_ot = (
      eff_rl_cap  if ot == 'RL'
      else eff_tbc_cap if ot == 'TBC'
      else 0.0  # TBL: no MJ-cap; SZ_REQ alone bounds it
  )
  ```
- **Pool dict** at 1157–1163: per `POOL_KEYS = ["RDC","MAJ_CAT","GEN_ART","CLR","VAR_ART","SZ"]`,
  `pool_dict[k] = max(FNL_Q over OPT_TYPEs)`. Same value shared across
  RL/TBC/TBL rows for that size — the pool depletes once.
- **Re-rank** at 1202–1207 — only for TBC and TBL (`if i > 0`), not for
  RL (which uses Stage A's rank).
- **Pre-band check** at 1212–1214 — once per OPT_TYPE (not per round) —
  PRI_CT_REM + store-broken.
- **Round loop** at 1216–1264:
  - Reset `ROUND_SHIP` / `ROUND_HOLD` for the whole OPT_TYPE (1218–1219).
  - Snapshot `FNL_Q_REM` (1229).
  - Rebuild `mbq_budget` from live MJ_REQ_REM (1234–1237).
  - Run band (1247–1251).
  - Revalidate (1253–1258).

### Known bugs + proposed fixes
- **B1 (same as Stage 1)** — the `0.0` literal at 1197.
  Proposed change line 1197 from `else 0.0` to either:
  ```python
  else eff_tbl_cap   # plumb a tbl_mbq_cap_pct
  ```
  or
  ```python
  else 100.0         # strict TBL cap == MJ_REQ_REM
  ```

- **B11 — Re-rank not run for RL.** Line 1202 `if i > 0:` skips RL. The
  original SQL engine *also* skips RL re-rank (Stage A's rank is final).
  This is correct. Worth a comment confirming the intent — current comment
  says "Only for TBC and TBL — RL uses the initial rank" but doesn't
  explain why (RL doesn't need live SIZE_RATIO since it ships full
  cartons regardless of pool depth).

- **B12 — `_pre_band_check` runs once per OPT_TYPE, not per round.**
  Lines 1212–1214 are outside the round loop. If `MJ_REQ_REM` dips below
  threshold mid-OPT_TYPE (after round 1 of TBL but before round 2), the
  next round won't catch it until `_revalidate_after_band` does. That's
  acceptable because revalidation runs every band (1253–1258), but it
  means **the very first round of an OPT_TYPE can ship a store that
  store-broken would have caught had it been called per-round.** The
  in-OPT inconsistency only matters at the boundary between OPT_TYPEs;
  inside a single OPT_TYPE it's revalidation that does the catching.

### SQL evidence
```
=== MJ-level values for HU45 TBL (after run) ===
MJ_MBQ | MJ_STK_TTL | MJ_REQ | MJ_REQ_REM | ACS_D
 2874  |   2736     |  138   |    0       |  22
```
`MJ_REQ_REM=0` after the run — RL+TBC consumed 106 of MJ_REQ=138, then
TBL consumed the remaining 32 + an extra 1,773 because `cap_pct_for_ot=0`
disabled the gate.

### Suggested tests
```python
def test_tbl_does_not_overshoot_mj_req_rem(small_mjcat):
    """After TBL band, MJ_REQ_REM should be 0 (or close), not negative — but the
    total ship for that store should be ≈ MJ_REQ + sec-cap headroom."""
    ...
```

---

## Stage 6 — Primary cap construction

### Inputs / Outputs / Invariants
- **`_build_mbq_budget`** (1057–1075): static — `max(0, cap_pct/100 × MJ_MBQ − MJ_STK_TTL)`.
  Not currently called from `_run_majcat_waterfall` (greppable — it's
  referenced only in the legacy `rne._stage_c_apply_mbq_cap` finalise
  path, see `rne.py:174–177`). **Dead code in `rule_engine_pandas.py`.**
- **`_live_mbq_budget`** (1078–1104): dynamic — `max(0, MJ_REQ_REM + ((cap_pct − 100)/100) × MJ_MBQ)`.
  Called at line 1235 every band start.
  - `cap_pct = 100` → `budget = MJ_REQ_REM` (strict).
  - `cap_pct = 130` → `budget = MJ_REQ_REM + 30% × MJ_MBQ`.
  - `cap_pct ≤ 0` returns `{}` (cap disabled).
- **Invariant — Growth at MJ+grid only:** `_live_mbq_budget` operates at
  the `WERKS` grain (MAJ_CAT is implied because each subprocess sees one
  MAJ_CAT). Per-OPT_TYPE caps are passed in via `cap_pct_for_ot`. This
  *implements* per-OPT_TYPE cap which is exactly what the saved invariant
  says you must NOT do. **But** the cap is structurally tied to MJ_MBQ
  (the MAJ_CAT-level MBQ), so it's MJ-level math gated by OPT_TYPE. The
  invariant phrasing — "Growth lives at MAJ_CAT and grid level only" —
  is ambiguous: is having a *different cap %* per OPT_TYPE a violation,
  or only having a *different MJ_MBQ source* per OPT_TYPE?
  - Today: same `MJ_MBQ` source, different cap % per OPT_TYPE. Probably OK.
  - But the comment at 1170–1176 frames `eff_rl_cap` / `eff_tbc_cap` as
    "per-OPT MJ-level cap PCT" which is exactly what the invariant
    forbids. **Needs user clarification (Open Questions).**

### Known bugs + proposed fixes
- **B13 — `_build_mbq_budget` is dead code in this module.** Lines 1057–1075.
  - Proposed: delete or move to `rule_engine_new.py` where the legacy
    finalise still calls it.

- **B2 (recap)** — `_live_mbq_budget` returning `{}` when `cap_pct ≤ 0`
  is the gate that turns TBL into "no cap". Combined with B1, that's the
  exit point for the bug.

### SQL evidence
```
=== Live MJ_REQ_REM after the run (TBL) ===
HU45 M_TEES_PN_HS  MJ_REQ_REM=0   (RL+TBC consumed it; TBL went anyway)
HD24 1116113282 O_WHT TBL ship=142  (vs OPT_MBQ=70 — over by 2.03×)
HW32 1116114961 O_WHT TBL ship=86   (vs OPT_MBQ=34 — over by 2.53×)
```

### Suggested tests
```python
def test_live_mbq_budget_strict_at_100():
    work = pd.DataFrame([{'WERKS':'X','MJ_REQ_REM':50,'MJ_MBQ':200}])
    assert _live_mbq_budget(work, 100.0) == {'X': 50.0}

def test_live_mbq_budget_growth_at_130():
    work = pd.DataFrame([{'WERKS':'X','MJ_REQ_REM':50,'MJ_MBQ':200}])
    assert _live_mbq_budget(work, 130.0) == {'X': 50.0 + 60.0}
```

---

## Stage 7 — Pre-band gate (PRI / store-broken)

### Inputs / Outputs / Invariants
- **Function:** `_pre_band_check` lines 1712–1788, `_propagate_skips_to_alloc`
  1656–1709.
- **Rules** (lines 1751–1785):
  1. **PRI_CT_REM < 100** for `enforced` OPT_TYPEs (TBL always; RL/TBC
     when their gate flags are on). Sets `ALLOC_STATUS='SKIPPED'` +
     `ALLOC_REMARKS+=" SKIP_PRI_BROKEN(pri=NN);"`.
  2. **Store-broken**: `MJ_REQ_REM < ACS_SKIP_FACTOR × ACS_D` (with
     `ACS_SKIP_FACTOR=0.5`, `rne:48`) for the current OPT_TYPE and all
     remaining types. Sets `+= " SKIP_STORE_BROKEN(mj_rem=NN);"`.
- **Then** propagate to alloc_df via `_propagate_skips_to_alloc`.
- **Invariant — ACS_D is density, not daily sale.** Rule 2 uses ACS_D —
  this is correct given the rule's semantics ("display-size remaining
  too small to bother shipping"), not "daily sale × N days".

### Known bugs + proposed fixes
- **B14 — `_propagate_skips_to_alloc` zeroes HOLD_QTY even on previously
  ALLOCATED rows** (1700–1710). Comment at 1700–1702 justifies it. But
  this is destructive: if RL/TBC already shipped + held to a store that
  then gets store-broken before TBL, the HOLD is gone but SHIP_QTY
  stays. Could create downstream HOLD-vs-SHIP imbalance for ALC pipeline.
  - Acceptable per the comment intent. Worth surfacing in tests.

- **B15 — Pre-band check uses `working_df['ALLOC_STATUS']` not alloc_df's.**
  Lines 1746–1747 read working_df. `working_df['ALLOC_STATUS']` is reset
  in `_load_tables` lines 1014–1019 (NULL/empty → `'PENDING'`). Earlier
  bands in the *same run* mutate working_df at 1759, 1780. OK.

- **B16 — Cross-type store-broken can skip TBL even when MJ_REQ_REM was
  consumed *by RL+TBC*** (i.e., the store *did* allocate). At the boundary
  from TBC to TBL, working_df's `MJ_REQ_REM` is decremented by
  `_revalidate_after_band` after each TBC band, so by the time TBL's
  `_pre_band_check` runs, MJ_REQ_REM may already be < 0.5×ACS_D — and
  TBL is skipped. **This is the intended store-broken behaviour** (see
  the comment at 1727–1728), but combined with B1 it doesn't matter:
  M_TEES_PN_HS HU45 shows MJ_REQ_REM=0 *and yet TBL shipped 1,805 units*,
  meaning the pre-band check is *somehow not skipping TBL*. Hypothesis:
  pre-band runs before round 1 of TBL, but by that point MJ_REQ_REM has
  already been consumed by RL+TBC — so why isn't TBL being SKIP_STORE_BROKEN?
  - Answer: look at line 1767 — `ENABLE_STORE_BROKEN` must be True
    (`rne:43` says it is). And the check is `MJ_REQ_REM(0) < 0.5 × ACS_D(22) = 11` — that's True (0 < 11), so HU45 TBL *should* be SKIPPED.
    But it isn't. Either:
    - (a) `_pre_band_check` is being called with stale working_df state, OR
    - (b) only some OPTs at HU45 are being skipped — confirm with SQL.

### SQL evidence
```sql
-- Are HU45 TBL rows skipped or shipped?
SELECT ALLOC_STATUS, COUNT(*), SUM(SHIP_QTY) FROM ARS_ALLOC_WORKING
WHERE MAJ_CAT='M_TEES_PN_HS' AND WERKS='HU45' AND OPT_TYPE='TBL'
GROUP BY ALLOC_STATUS;
```
Result from prior probe:
```
HU45 TBL store_ship = 1805
```
So TBL is **not** being store-broken. Possible cause: `_pre_band_check`
runs *before* round 1, but `MJ_REQ_REM` is reset to `MJ_REQ` (138) at
the start of the run, RL consumes 52, TBC consumes 54, so when TBL's
pre-band runs `MJ_REQ_REM` ≈ 32 — which is **above** `0.5×22=11`. TBL
passes the gate. Then during TBL rounds, revalidation drops `MJ_REQ_REM`
to 0, but with B1, the lack of an in-band cap means rounds 1 and 2
already shipped a combined 1,805 units before revalidation could
SKIP-store-broken the next round (there is no next round).

### Suggested tests
```python
def test_pre_band_skips_when_mj_req_rem_below_factor():
    # Working with MJ_REQ_REM=10, ACS_D=22 → 10 < 11 → SKIP
    ...
```

---

## Stage 8 — Re-ranking between OPT_TYPEs

### Inputs / Outputs / Invariants
- **Function:** `_rerank_opt_priority_pandas` (1269–1392).
- **Step 1 — SKIP low-coverage OPTs** (1329–1347): for each
  (WERKS, GEN_ART, CLR) in target OT, compute live `SIZE_RATIO = sizes_with_pool / total_sizes`.
  If `SIZE_RATIO < size_threshold` AND `live_var_fnl < min_size_count`,
  mark SKIPPED with `R07_SIZE_RATIO_LIVE`.
- **Step 2 — RERANK** survivors using
  `TIER asc → SIZE_RATIO desc → SEC_CT% desc → MAX_DAILY_SALE desc → OPT_REQ_WH desc`.
- **Called once per OPT_TYPE** (line 1202, only when `i > 0`).
- **Invariants:**
  - Uses `MAX_DAILY_SALE` (not `ACS_D`) for velocity ranking — **correct**.
  - Per-row `OPT_PRIORITY_RANK` is overwritten via `rank_map` (1379–1388).

### Known bugs + proposed fixes
- **B17 — `_rerank_opt_priority_pandas` masks on `ALLOC_STATUS.isna()`**
  (line 1290). But `_load_tables` coerces `ALLOC_STATUS` to non-null
  string at lines 941–944 (sets it to `'PENDING'`). So `isna()` returns
  False for everything, and the mask matches only rows that are *currently*
  NaN — which is none. **Reranking never runs in production today.**
  - Repro: walk through `_load_tables`: `ALLOC_STATUS` defaults to
    `'PENDING'`, then 942 fills NaN with `''`, then 944 sets non-INELIGIBLE
    rows to `'PENDING'`. So `alloc_df['ALLOC_STATUS'].isna()` returns
    all False at the time `_rerank_opt_priority_pandas` runs.
  - Proposed fix: change line 1290 from
    `& alloc_df['ALLOC_STATUS'].isna()` to
    `& (alloc_df['ALLOC_STATUS'] == 'PENDING')`.
  - **This is a silent functional regression.** The whole re-rank +
    R07_SIZE_RATIO_LIVE logic is dead. Live SIZE_RATIO computed at
    1310–1319, low_cov computed at 1332, but `if low_cov.any():` is
    against an empty `sub` (because `mask` was empty), so nothing skipped.
  - **Severity: HIGH.** Combined with B1, this is the second-biggest
    source of M_TEES_PN_HS over-ship — TBC and TBL rows for stores with
    actually-depleted pools are not being SIZE_RATIO-skipped.

### SQL evidence
```
=== TBL skipped with R07_SIZE_RATIO_LIVE (post-pandas) ===
-- Should be in SKIP_REASON or ALLOC_REMARKS
SELECT COUNT(*) FROM ARS_ALLOC_WORKING
WHERE ALLOC_REMARKS LIKE '%R07_SIZE_RATIO_LIVE%'
   OR SKIP_REASON LIKE '%R07_SIZE_RATIO_LIVE%'
```
Run this — expect zero rows, confirming the dead-code hypothesis.

### Suggested tests
```python
def test_rerank_actually_runs(monkeypatch):
    """Plant a known low-coverage OPT and assert it's marked R07_SIZE_RATIO_LIVE."""
    alloc_df = pd.DataFrame([...])  # one OPT, 5 sizes, only 1 with pool, count=1, threshold=0.6
    _rerank_opt_priority_pandas(alloc_df, {key1: 1.0}, 'TBC', 0.6, 3)
    assert (alloc_df['ALLOC_REMARKS'].str.contains('R07_SIZE_RATIO_LIVE')).any()
```

---

## Stage 9 — Core band (pool draw, SHIP/HOLD split)

### Inputs / Outputs / Invariants
- **Function:** `_run_band` (1395–1653).
- **Eligibility mask** (1419–1423): same OPT_TYPE, `I_ROD ≥ r`, not in
  {SKIPPED, INELIGIBLE}.
- **`need_ship`** = `max(r × SZ_MBQ − SZ_STK − SHIP_QTY, 0)`.
- **`need_pool`**:
  - TBL: `tbl_cum = SZ_MBQ_WH + (r−1) × SZ_MBQ`; `need_pool = max(tbl_cum − SZ_STK − POOL_CONSUMED, 0)`. Then
    `need_pool = where(need_ship==0, 0, need_pool)` (1445–1447).
  - RL/TBC: `need_pool = max(r × SZ_MBQ − SZ_STK − POOL_CONSUMED, 0)`.
- **Hold-draw** (1455–1500): RL/TBC consume from `hold_dict` first,
  net the difference to `need_pool`. Hold-draw rows are written back
  immediately (1475–1500) — this is the only path that touches
  `alloc_df` *before* the pool filter.
- **TBL size-completeness gate** (1521–1530): mirrors R07 but on live pool.
- **Pool filter** (1532–1534): drop rows with no pool.
- **Stable sort** within pool key (1538–1540): POOL_KEYS → OPT_PRIORITY_RANK
  ASC → ST_RANK ASC → WERKS.
- **Pool take** (1543–1553): cumulative-window inside each pool key.
- **MBQ-cap** (1559–1582): per-WERKS budget, sorted by OPT_PRIORITY_RANK
  inside each WERKS. **Only fires when `mbq_budget` is non-empty** — and
  it's non-empty only when `cap_pct_for_ot > 0`.
- **SHIP/HOLD split** (1584–1596): TBL splits by `need_ship`; RL/TBC all
  pool-take ships.
- **Write-back** (1598–1636): POOL_CONSUMED, SHIP_QTY, HOLD_QTY, status.
- **Pool decrement** (1638–1649): subtract `pool_take` from `pool_dict`.

### Known bugs + proposed fixes
- **B1 (Stage 9 manifestation)** — MBQ-cap at 1564 is gated by
  `if mbq_budget:`. When `cap_pct_for_ot = 0` (TBL), `mbq_budget = {}`,
  and the cap is skipped. The within-pool-key sort still ensures
  fair-share among competing OPTs, but **the per-WERKS total ceiling
  is gone for TBL.**

- **B18 — `tbl_cum` in line 1445 uses `SZ_MBQ_WH + (r−1)×SZ_MBQ`.** The
  warehouse hold buffer is counted once. This is fine for round 1 (TBL
  rolls in `SZ_MBQ_WH` of physical hold) but interacts oddly with B1: a
  store with `SZ_STK=0`, `SZ_MBQ_WH=10`, `SZ_MBQ=8`, `I_ROD=2` has
  - round 1: `need_pool = max(10 − 0 − 0, 0) = 10`; `need_ship = max(8−0−0, 0) = 8`. SHIP=8, HOLD=2.
  - round 2: `need_pool = max(10 + 8 − 0 − 10, 0) = 8`; `need_ship = max(16 − 0 − 8, 0) = 8`. SHIP=8, HOLD=0.
  - Total: 16 SHIP + 2 HOLD = 18 units, against `SZ_REQ` only.
  
  See HU45 row 1 in SQL evidence below — that's exactly what happened:
  `SZ_MBQ=8, SZ_MBQ_WH=10, I_ROD=2, SHIP=16, HOLD=2`. Per-SZ behavior is
  by-design. The aggregate (sum across 30+ sizes × 60+ OPTs in HU45)
  hits 1,805 with no MJ cap to bound it.

- **B19 — TBL hold-buffer "counted once" logic in TBL_R2.** Comment at
  1443. But also at line 1611–1616: `is_ship_met = take >= n_ship`; if
  pool is too small to fully ship, `round_hold = 0`. Correct — hold
  only happens when ship is met.

- **B20 — Pool decrement uses `pool_take` not `take_pool`.** Line 1641
  computes `_taken = pool_take` (which is `round_ship + round_hold` for
  TBL, after the hold-met gate at 1611). Lines 1643–1649 group by pool
  keys and subtract. Correct.

### SQL evidence
```
=== Per-row TBL example HU45 (sample) ===
GEN_ART_NUMBER | CLR   | VAR_ART       | SZ | I_ROD | SZ_MBQ | SZ_MBQ_WH | SZ_STK | SHIP | HOLD | FNL_Q_REM
1116112830     | M_GRY | 1116112830004 | XL |   2   |   8    |    10     |   0    |  16  |  2   |   271
1116112918     | L_ONN | 1116112918004 | XL |   2   |   8    |    10     |   0    |  16  |  2   |   452
1116112926     | O_WHT | 1116112926004 | XL |   2   |   8    |    10     |   0    |  16  |  2   |   223
... [14 more like this for size XL alone]
1116112830     | M_GRY | 1116112830003 | L  |   2   |   7    |     8     |   0    |  14  |  1   |   619
... [many more for size L]
```
Each per-SZ row is internally consistent: `16 = r(2)×SZ_MBQ(8) = SHIP target`.
**The problem is not per-row math; it's the absence of a cross-OPT, per-
WERKS ceiling once the *correct* SHIPs are summed.**

### Suggested tests
```python
def test_band_respects_per_werks_mbq_budget():
    """Two OPTs competing at same WERKS with mbq_budget=10 — sum SHIP ≤ 10."""
    alloc = build_two_opts(...)
    pool = {k1: 100, k2: 100}
    _run_band(alloc, pool, 'TBL', 1, mbq_budget={'X': 10.0})
    assert alloc.loc[alloc['WERKS']=='X', 'SHIP_QTY'].sum() <= 10.5

def test_tbl_band_no_overship_when_cap_active():
    """Same as above but for TBL — currently FAILS because cap_pct_for_ot=0."""
```

---

## Stage 10 — Post-band revalidation

### Inputs / Outputs / Invariants
- **Function:** `_revalidate_after_band` (1791–2026).
- **Steps** (lines 1817–2026):
  1. Decrement `MSA_FNL_Q_REM` per OPT by `ROUND_SHIP+ROUND_HOLD`.
  2. Decrement each `<grid>_REQ_REM` at the grid's grain by `ROUND_SHIP`
     only (no hold).
  3. Recompute `H_<grid>_REM = (REQ_REM ≥ 0.5×ACS_D) AND (GH=1)`.
  4. `PRI_CT_REM = Σ(H_REM)/Σ(GH) × 100`.
  5. Skip MSA-exhausted and PRI-broken — **only for OPTs with a next band**
     (`I_ROD ≥ r+1`).
  6. Store-broken — same next-band scope.
- **Invariant — Sec-cap grid extras must propagate.** Lines 1851–1858 join
  band rows to working_df via `OPT_KEYS + extras` to pick up FAB/MICRO_MVGR
  etc. Today only Primary grids are in `grids` (per `_discover_primary_grids`),
  so extras for those Primary grids (MJ has none; MJ_MACRO_MVGR has
  `[MACRO_MVGR]`; MJ_RNG_SEG has `[RNG_SEG]`) must be on working_df.
  Confirmed by SQL evidence — all 5 propagation columns are on every table.

### Known bugs + proposed fixes
- **B21 — Step 5 marks OPTs SKIPPED only if they have a *next round*.**
  Lines 1921–1933. If `r` is the last round, OPTs that just newly
  qualified as MSA-exhausted are not marked. This is **intentional**
  (comment at 1913–1915) but limits the audit trail — last-round overshoots
  in the same opt_type stay in `ALLOC_STATUS='ALLOCATED'` with no skip
  remark.

- **B22 — Step 2 uses ROUND_SHIP only, not ROUND_SHIP+ROUND_HOLD.**
  Lines 1837–1838. For TBL where HOLD is meaningful, the grid `REQ_REM`
  is decremented only by ships, not by held units. **This is per
  rule_engine_new** (`rne:1117–1129` does the same), so consistent —
  but worth knowing: TBL hold sits "outside" the grid REQ math.

- **B23 — Step 3 inclusive-≥ threshold** (line 1896). Comment at
  1881–1883 says "Inclusive threshold (≥): a slot at exactly half-display
  still counts as eligible". Different from the legacy `rne:1129`
  which uses `>` (strict). The pandas engine is **looser** here than
  the SQL engine. Probably a real divergence, not a typo. Worth user
  confirmation.

### SQL evidence
```
=== MJ_REQ_REM after the run ===
For 80+ HU45 TBL rows, MJ_REQ_REM=0 after the run, MJ_REQ=138, ACS_D=22.
0 < 0.5×22 = 11 → store-broken should have fired for the *next* OPT_TYPE
(which doesn't exist after TBL). Consistent with B21.
```

### Suggested tests
```python
def test_revalidate_decrements_msa_fnl_q_rem():
    ...
def test_revalidate_skips_only_next_band_opts():
    ...
```

---

## Stage 11 — Finalise + Stage D + secondary-cap pre-gate

### Inputs / Outputs / Invariants
- Lines 735–877.
- **Safety-net 1** (744–748): zero HOLD_QTY where ALLOC_STATUS='SKIPPED'.
- **Safety-net 2** (752–757): reset POOL_CONSUMED on SHIP=0 AND HOLD=0
  (cancelled rows that still hold pool).
- **Recompute FNL_Q_REM** (759–775) from `FNL_Q − Σ(POOL_CONSUMED)` per
  pool key.
- **Set ALLOC_QTY = SHIP_QTY** (776).
- **ALLOC_STATUS + SKIP_REASON** finalization (777–814):
  - TBL ALLOCATED iff `SHIP+HOLD ≥ SZ_MBQ_WH + (I_ROD−1)×SZ_MBQ − SZ_STK`.
  - RL/TBC ALLOCATED iff `SHIP+HOLD ≥ I_ROD × SZ_MBQ − SZ_STK`.
- **Sec-cap pre-gate** (819–847) — only if `apply_sec_cap_in_normal=True`
  AND grids with `sec_cap_applicable=True` exist.
- **`_classify_alloc_reason`** (851) — Currently a stub no-op (`rne:2449`).
- **`_stage_d_reflect`** (853) — roll up alloc back to listing.
- **Totals** (855–862) and progress query (864–868).

### Known bugs + proposed fixes
- **B24 — `_classify_alloc_reason` is a no-op stub.** `rne:2449` logs
  `[_classify_alloc_reason] stub no-op`. SKIP_REASON values come only
  from the inline finalization above (Stage 11) and from
  `_propagate_skips_to_alloc` (Stage 7). The richer per-grid classification
  this function's docstring promises is missing.

- **B3 (recap)** — sec-cap is wired but disabled by `sec_cap_applicable=False`
  on every grid (confirmed by SQL evidence).

- **B25 — ALLOC_STATUS check at 781–797** is correct *per-row* but doesn't
  re-check the MJ aggregate. A TBL row with `SHIP+HOLD=18, SZ_MBQ_WH=10,
  I_ROD=2, SZ_MBQ=8, SZ_STK=0` → target is 10+8−0=18; row is ALLOCATED.
  Fine row-locally. The MJ-level overshoot lives one level up.

- **B26 — Comment at line 742** "No global MJ_REQ cap. Per-OPT MBQ-caps
  are the only MAJ_CAT-level ceilings; TBL has no MJ-cap (only SZ_REQ)"
  is the documented intent. **This contradicts the saved invariant
  `Growth at MJ+grid only`.** Either the comment is wrong (and TBL
  needs an MJ cap), or the invariant is being interpreted differently
  here (and the invariant doc needs updating).

### SQL evidence
```
=== SEC_CAP-related SKIP_REASON counts ===
(no rows)                       ← sec-cap fired on zero rows in entire run
```

### Suggested tests
```python
def test_finalise_sets_alloc_qty_equal_ship_qty():
    ...
def test_sec_cap_fires_when_grid_applicable():
    """With one grid sec_cap_applicable=True and a known overshoot."""
    ...
```

---

## Stage 12 — Persistence

### Inputs / Outputs / Invariants
- **`_write_back_alloc`** (2041–2127) — bulk MERGE via `#alloc_pd_writeback`
  temp table; `fast_executemany=True`; `SET DEADLOCK_PRIORITY LOW`;
  `OPTION (MAXDOP 1)`.
- **`_write_back_working`** (2130–2204) — same pattern.
- **Key columns:** alloc joins on `(WERKS, RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ)`.
  Working joins on `(WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR)` — OPT grain.
- **Writer queue** (`_writer_thread_fn`, 280–370): single-threaded
  drain serialises all UPDATEs.

### Known bugs + proposed fixes
- **B27 — `FROM_HOLD_QTY` DDL guard is per-call** (lines 2075–2086).
  Idempotent but cheap. OK.

- **B28 — `_write_back_working` only writes `_REM` family + status.**
  Lines 2135–2143. Other working_df mutations (e.g., grid extras) are
  *not* persisted. This is intentional (working is recomputed each run)
  but the comment at 2132–2134 should be clearer about which columns
  the engine guarantees to write.

### SQL evidence
```
=== Latest run row count ===
ARS_ALLOC_WORKING M_TEES_PN_HS rows: 19,748
of which                       PENDING+INELIGIBLE: 0
                               ALLOCATED:          8,112
                               PARTIAL:            1,130
                               SKIPPED:           10,506
```
Persistence completed — every row has a final ALLOC_STATUS.

### Suggested tests
- Integration: kick a pandas run, then `SELECT COUNT(*) FROM ARS_ALLOC_WORKING
  WHERE ALLOC_STATUS IS NULL` must be 0.

---

## Cross-cutting findings (severity-ordered)

| # | Severity | Stage | Finding |
|---|---|---|---|
| B1 | **CRITICAL** | 5, 9 | TBL has no MJ cap (`cap_pct_for_ot = 0.0` at line 1197). Causes ALLOC ≫ REQ. SQL evidence: M_TEES_PN_HS HU45 TBL ship=1805 vs MJ_REQ=138. |
| B17 | **HIGH** | 8 | `_rerank_opt_priority_pandas` masks on `ALLOC_STATUS.isna()` (line 1290) but `_load_tables` ensures it's never NaN. Re-rank + R07_SIZE_RATIO_LIVE skip is dead code. |
| B3 | **HIGH** | 11 | Sec-cap pre-gate fires on zero grids — `sec_cap_applicable=False` for every Active grid in `ARS_GRID_BUILDER`. The toggle exists but is data-disabled. |
| B26 | **MED** | 11 | Comment at line 742 documents "TBL has no MJ-cap" as intentional — contradicts saved invariant. Needs explicit user decision. |
| B5 | **MED** | 2 | `_select_working_cols` only loads grid extras for Primary grids (`_discover_primary_grids`). Adding a Primary grid whose extras aren't on working_df would silently lose REQ_REM decrements. |
| B23 | **LOW** | 10 | Pandas uses `≥` for `H_<grid>_REM` recompute (line 1896); SQL engine uses `>` (`rne:1129`). Functional divergence. |
| B13 | **LOW** | 6 | `_build_mbq_budget` is dead code in this module. |
| B24 | **LOW** | 11 | `_classify_alloc_reason` is a no-op stub. Richer reason classification is unimplemented. |
| B12, B15, B16, B19, B20, B22, B27 | INFO | various | Per-stage notes; behavior matches `rule_engine_new` or has documented intent. |

---

## Open questions / decisions needed

1. **TBL cap policy — strict (100%) or growth-allowed (130%)?**
   - Saved invariant says "Growth at MAJ_CAT+grid level only, never per
     OPT_TYPE". But the engine *does* set different cap % per OPT_TYPE
     today (RL 130%, TBC 130%, TBL 0%). Two clean readings:
     - **Reading A**: Invariant means "the cap source must be the MJ-level
       budget" — *different cap % per OPT_TYPE is fine* as long as the
       budget is `cap_pct × MJ_MBQ − MJ_STK_TTL`. **Fix B1 by adding
       `tbl_mbq_cap_pct` default 130** and revisiting whether RL/TBC
       defaults should also follow MJ growth (rather than being a UI knob).
     - **Reading B**: Invariant means "all OPT_TYPEs share one MJ cap %"
       — different % per OPT_TYPE is itself a violation. **Fix B1 by
       removing `rl_mbq_cap_pct` / `tbc_mbq_cap_pct` parameters and using
       a single `mj_growth_pct` plumbed through `_live_mbq_budget`.**
   - The TBL row-target math (`SZ_MBQ_WH + (r−1)×SZ_MBQ`) is independent of
     this decision; only the MJ ceiling changes.

2. **Should `SZ_MBQ_WH` be in `tbl_cum`?**
   - Line 1445: `tbl_cum = SZ_MBQ_WH + (r−1) × SZ_MBQ`. This rolls in
     the warehouse hold buffer once. If the policy is "TBL ships only
     up to `r × SZ_MBQ − SZ_STK` (same as RL/TBC) and any extras roll
     into HOLD via `need_ship` vs `need_pool`", then `SZ_MBQ_WH`
     belongs in the pool-demand expression but not in the ship-target.
   - Currently `need_ship = max(r × SZ_MBQ − SZ_STK − SHIP_QTY, 0)`
     uses `r × SZ_MBQ` (no WH), and `need_pool` is computed against
     `tbl_cum` (with WH). The TBL split at 1589–1591 then partitions
     pool-take into ship + hold using `need_ship`. This is internally
     consistent — but worth user confirmation that the WH-buffer-as-HOLD
     semantics are still desired.

3. **Reading-≥ vs `>` in `H_<grid>_REM` (B23).**
   - SQL engine: strict `>` (a row exactly at `0.5×ACS_D` is *not*
     eligible).
   - Pandas engine: `≥` (a row exactly at threshold *is* eligible).
   - Probably a typo in one of them; needs decision.

4. **Re-rank dead code (B17).**
   - Restoring `_rerank_opt_priority_pandas` *will* increase the SKIPPED
     count (more OPTs caught by R07_SIZE_RATIO_LIVE). Confirm operator
     expectations — historically the SQL engine has this logic active.

5. **Sec-cap default (B3).**
   - Should `sec_cap_applicable` default to True for new grids? Today
     the data has it False everywhere, which effectively means the
     pre-gate is off by default. Combined with B1, there is no
     *enforcement* of any cap on TBL.

---

## Appendix A — How SQL evidence was collected

```python
# from backend/, with PYTHONPATH=backend
from sqlalchemy import text
from app.database.session import get_data_engine

eng = get_data_engine()
with eng.connect() as c:
    rows = c.execute(text("...your query...")).fetchall()
```

Scripts used: `backend/scripts/_re_pd_review.py`,
`backend/scripts/_re_pd_review2.py`, `backend/scripts/_re_pd_review3.py`
(ephemeral; delete after review).

## Appendix B — Key constants (cross-reference)

| Name | Value | Source | Used at |
|---|---|---|---|
| `OPT_TYPE_ORDER` | `["RL","TBC","TBL"]` | line 71 | 1185, 1741, 1916, 1966 |
| `POOL_KEYS` | RDC,MAJ_CAT,GEN_ART,CLR,VAR_ART,SZ | line 72 | 964, 1158, 1297, 1539, 1643 |
| `OPT_KEYS` | WERKS,MAJ_CAT,GEN_ART,CLR | line 73 | 1684, 1818, 1924, 1928 |
| `ACS_SKIP_FACTOR` | 0.5 | rne:48 | 1777, 1896, 1974 |
| `ENABLE_STORE_BROKEN` | True | rne:43 | 1767, 1965 |
| `ENABLE_PER_OPT_REVALIDATION` | True | rne:47 | 1166, 1212, 1253 |
| `SEC_CAP_DEFAULT_PCT` | (rne) | rne:2671 | sec-cap factor |
| `SEC_CAP_PRE_OVERRIDE_OPT_REQ_PCT` | (rne) | rne:2719, 2846 | high-demand override |
