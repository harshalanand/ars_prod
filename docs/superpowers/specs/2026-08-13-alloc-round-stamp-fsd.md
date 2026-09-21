# FSD — ALLOC_ROUND / ALLOC_WAVE stamp integrity (FS-11)

| | |
|---|---|
| **Document** | Functional Specification |
| **Module** | Allocation engine — `rule_engine_per_opt` band write-back, `rule_engine_pandas` hold-release retry |
| **Rule ID** | **FS-11** |
| **Date** | 2026-08-13 |
| **Status** | Implemented |
| **Evidence run** | `20260812_131757_589` |
| **Worked example** | `WERKS=HP04`, `GEN_ART_NUMBER=1110116457`, `CLR=OFF_WHT`, `MAJ_CAT=M_TEES_HS`, TBL, `I_ROD=2` |
| **Scope** | Audit labels only — no quantity, pool or allocation decision changes |

---

## 1. Purpose

`ALLOC_ROUND` and `ALLOC_WAVE` record which dispatch round last delivered stock to a
size. They were being overwritten by a later pass that delivered nothing, so multi-round
dispatches were reported as single-round. This corrects the stamp without touching any
quantity.

## 2. As-is behaviour

### 2.1 Two kinds of column

| Column | Semantics | Written as |
|---|---|---|
| `SHIP_QTY`, `HOLD_QTY`, `POOL_CONSUMED`, `FROM_HOLD_QTY` | cumulative | `previous + this round` |
| `ALLOC_ROUND`, `ALLOC_WAVE` | label | `= r` (replaced) |

Cumulative columns cannot lose history. Labels are last-writer-wins.

### 2.2 The stamp site

`rule_engine_per_opt._run_band_per_opt`, step 5i write-back — pre-fix:

```python
alloc_df.loc[opt_idx, 'ALLOC_WAVE']  = f"{ot}_R{r}"
alloc_df.loc[opt_idx, 'ALLOC_ROUND'] = float(r)
```

`opt_idx` is **every size row of the OPT**. The write is unconditional: it does not
consult `moved`, which is computed later at step 5j as
`(round_ship + round_hold + from_hold) > 0`. A size that received nothing in this pass is
stamped exactly like one that received stock.

### 2.3 The third pass

`rule_engine_pandas`, post-TBL hold-release retry (Option B, 2026-07-31). After the TBL
rounds finish, TBL options that shipped but did not reach display cover
(`STK_TTL + Σ SHIP < stock_threshold_pct × ACS_D`) have their warehouse hold released back
into the live pool, and the TBL band runs **one more time** so still-hungry options can
take the freed pieces in the same run. Pre-fix call:

```python
_run_band_per_opt(alloc_df, pool_dict, 'TBL', 1, ...)
                                        #    ↑ hard-coded r = 1
```

It re-enters with `r = 1` to reuse the round-1 need formula, but it is not round 1 — it
runs after rounds 1..N have completed. Combined with §2.2 it restamps every TBL OPT it
visits back to `TBL_R1 / 1`.

The pass is valuable and is **not** being removed: in the evidence run it freed
**5,033 pieces across 4,364 size rows** (`HOLD_RELEASED_NOT_COVERED` remarks) and offered
them to hungry options in the same run. Without it those pieces idle until the next run.

### 2.4 Why a single-size OPT would not show the defect

The band skips an OPT with no residual need before reaching the write-back. The defect
needs a **sibling size that still has need**: that size keeps the OPT alive through the
retry, and the already-satisfied siblings get restamped as collateral. This is the
mechanism, and it is why the regression test uses two sizes.

### 2.5 Worked example — `HP04 / 1110116457 / OFF_WHT`, size L

| pass | shipped | `SHIP_QTY` after | `ALLOC_ROUND` written |
|---|---|---|---|
| TBL round 1 | 4 | 4 | 1 |
| TBL round 2 | 4 | 8 | 2 ← correct |
| hold-release retry (`r=1`) | **0** | 8 | **1** ← overwrites |

Stored result: `SHIP_QTY = 8` (correct), `ALLOC_ROUND = 1`, `ALLOC_WAVE = TBL_R1` (both
wrong). `ALLOC_REMARKS` on the same row proves two rounds ran:
`B[TBL.r1.rk30] ship=4 hold=0; B[TBL.r2.rk30] ship=4 hold=0;`. Size 2XL of the same OPT
carries three `POOL_EMPTY(...)` remarks — one per pass — showing the third visit.

### 2.6 Measured scope

Taking a `.r2.` trace in `ALLOC_REMARKS` as ground truth for "a round-2 pass ran":

| OPT_TYPE | rows with r2 trace | `ALLOC_ROUND = 1` | `≥ 2` |
|---|---|---|---|
| RL | 1,287 | **0** | 1,287 |
| TBC | 540 | **0** | 540 |
| TBL | 332 | **37** | 295 |

Only TBL is affected — it is the only OPT_TYPE with a retry pass. 37 rows in the evidence
run. The distortion also shows in the wave counts: `TBL_R2` holds 397 rows against
`RL_R2`'s 4,155, despite TBL being the largest type by volume.

## 3. Impact

Reporting and audit only. `SHIP_QTY`, `HOLD_QTY`, `ALLOC_QTY`, `POOL_CONSUMED` and
`FNL_Q_REM` accumulate and were always correct — nothing was under- or over-shipped, and
no allocation decision changed. The per-OPT engine does not read `ALLOC_ROUND` back; the
consumers are the exports (`listing.py`) and the dashboard's `MAX(ALLOC_WAVE)`
(`ars_dashboard.py`). A second, quieter cost: retry-sourced shipments were indistinguishable
from ordinary round-1 ones, so the value recovered by the hold-release pass was invisible.

## 4. To-be behaviour — FS-11

Three rules.

**R1 — stamp only rows that moved.** The label is written for the subset of `opt_idx`
where `(round_ship + round_hold + from_hold) > 0`, the same predicate step 5j already uses
for `moved`. A pass that delivers nothing to a size leaves that size's labels untouched.

**R2 — `ALLOC_ROUND` never decreases.** `ALLOC_ROUND = MAX(existing, r)`. Even when a
later pass at a lower `r` does deliver, it cannot drag a higher round stamp down.

**R3 — the retry carries its own wave label.** `_run_band_per_opt` gains an optional
`wave_label` parameter (default `None` → `f"{ot}_R{r}"`). The hold-release retry passes
`wave_label='TBL_RETRY'`, so pieces delivered by the retry are distinguishable from
genuine round-1 deliveries for the first time.

R1 and R2 are independent safeguards: R1 handles the no-op visit (the reported case), R2
handles a retry that genuinely ships.

### 4.1 Post-fix result for the worked example

| | pre-fix | post-fix |
|---|---|---|
| `SHIP_QTY` | 8 | 8 (unchanged) |
| `ALLOC_ROUND` | 1 | **2** |
| `ALLOC_WAVE` | `TBL_R1` | **`TBL_R2`** |

## 5. Changes

| File | Change |
|---|---|
| `rule_engine_per_opt.py` — signature | New optional `wave_label: Optional[str] = None`. |
| `rule_engine_per_opt.py` — step 5i | Stamp block replaces the two unconditional assignments: computes `_stamp_moved`, stamps only that subset, applies `np.maximum(previous, r)` to `ALLOC_ROUND`, uses `wave_label` when supplied. |
| `rule_engine_pandas.py` — hold-release retry | Passes `wave_label='TBL_RETRY'`. |

No schema change, no new column, no migration. `rule_engine_new.py`'s SQL-path stamps
(`_stage_c_run_band`) are untouched — that path is not the production engine.

## 6. Out of scope

| Item | Reason |
|---|---|
| The hold-release retry itself | Recovers real stock (5,033 pieces in the evidence run). Only its labelling changes. |
| Any quantity, pool or gate | FS-11 is label-only by construction. |
| Historic rows in `ARS_ALLOC_HISTORY` | Not back-filled. Past sessions keep their stale stamps; `ALLOC_REMARKS` remains the ground truth for those. |
| `rule_engine_new.py` SQL band | Non-production path. |

## 7. Verification

`backend/tests/test_alloc_round_stamp_per_opt.py` — pure pandas, no DB. Four cases:

| Test | Asserts |
|---|---|
| `test_satisfied_size_keeps_round_two_when_a_sibling_size_pulls_the_retry_in` | The reported defect in its real two-size shape: L finishes in round 2, 2XL stays hungry and pulls the OPT into the retry, L keeps `RL_R2 / 2` while 2XL takes the retry stock. |
| `test_retry_that_ships_carries_its_own_wave_label` | A retry that does deliver records `RL_RETRY`, not `RL_R1`. |
| `test_stamp_is_not_applied_to_rows_that_did_not_move` | R1 — a zero-delivery pass leaves the labels empty. |
| `test_alloc_round_never_decreases` | R2 — a later lower-`r` delivering pass keeps the higher round. |

**All four fail against the pre-fix code and pass after** (verified 2026-08-13 by
temporarily restoring the old stamp). Full suite: 9 passed.

### 7.1 Post-deployment check

On the next run, the invariant that was violated should hold for every OPT_TYPE:

```sql
SELECT OPT_TYPE, COUNT(*) AS rows_with_r2_trace,
       SUM(CASE WHEN ISNULL(ALLOC_ROUND,0) = 1 THEN 1 ELSE 0 END) AS wrongly_says_round_1
FROM ARS_ALLOC_WORKING WITH (NOLOCK)
WHERE ALLOC_REMARKS LIKE '%.r2.%'
GROUP BY OPT_TYPE;
```

Expect `wrongly_says_round_1 = 0` for RL, TBC **and** TBL. Additionally
`SELECT ALLOC_WAVE, COUNT(*) … WHERE ALLOC_WAVE = 'TBL_RETRY'` should be non-zero on any
run where `HOLD_RELEASED_NOT_COVERED` appears — that is the recovered-stock volume, now
measurable.

## 8. Rollback

Revert the three edits. There is no flag: the change cannot alter allocation output, only
the two label columns, so a runtime switch would add risk without reducing any.

## 9. Documentation updated

- `frontend/public/docs/manual/listing.md` — `## Recorded rules (engine)`.
- Data dictionary needs no change: `ALLOC_WAVE` / `ALLOC_ROUND` descriptions remain
  accurate; the fix makes the stored values match them.
