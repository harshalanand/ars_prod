# Sec-Cap Hard-Block on Empty Grid Value or Empty MBQ Budget

**Date:** 2026-06-30
**Owner:** santosh@v2kart.com
**Scope:** `backend/app/services/rule_engine_per_opt.py` (per-OPT mode only)
**Status:** Spec — pending review before plan

---

## 1. Problem

The current sec-cap pre-gate in per-OPT mode (`rule_engine_per_opt.py:388-391`) silently **admits** OPTs when a sec-cap-applicable grid grain has no budget configured (`MBQ_ORIG = 0` explicit, or the grid value is `'NA'`). This was an intentional invariant (recorded in `.claude/agents/ars_flow_kb/merge_rules.md:26`): "MBQ=0 means grain unconfigured, do not enforce a cap here".

Observed case (session `20260629_154555_543`):

- OPT `(HB11, M_BOXER, 1114058292, ECRU_MEL)`, OPT_TYPE=TBL
- Grid `MJ_M_YARN_02`: `sec_cap_applicable=True`, `sec_cap_pct=120`, `GH_M_YARN_02=1` for `M_BOXER`
- Grid value on the OPT: `M_YARN_02 = 'NA'`
- Grid budget: `M_YARN_02_MBQ_ORIG = 0.0`
- Result today: OPT shipped 18 units + held 10 units, status=ALLOCATED, no SKIP_REASON

Business expectation (sign-off from santosh@v2kart.com, 2026-06-30): when sec-cap is enabled for a grid and the grid hierarchy says the grid applies to this MAJ_CAT, an OPT with **no fabric classification** or **no budget** must NOT be dispatched. Treat the empty-data condition as "data not ready, hold dispatch until upstream classifies."

## 2. Rule

For each OPT × sec-cap-applicable grid where `GH_<grid> = 1` in `ARS_GRID_HIERARCHY` for the OPT's MAJ_CAT:

**Block the OPT (status=SKIPPED, SHIP=0, HOLD=0)** if EITHER of these is true:

| Trigger | Condition | SKIP_REASON tag |
|---|---|---|
| Empty grid value | `<grid> IS NULL` or `<grid> = 'NA'` | `SEC_CAP_GRID_NULL[<grid>]` |
| Empty MBQ — explicit zero | `<grid>_MBQ_ORIG = 0.0` (NOT NULL) | `SEC_CAP_MBQ_ZERO[<grid>]` |
| Empty MBQ — NULL | `<grid>_MBQ_ORIG IS NULL` | `SEC_CAP_NULL[<grid>]` |

When more than one trigger fires for the same OPT × grid, concatenate the tags with `,` in `ALLOC_REMARKS`.

When `<grid>_MBQ_ORIG > 0`, behavior is unchanged: `ceiling = MBQ_ORIG × sec_cap_pct / 100` is enforced normally.

This rule **supersedes** invariant 3 in `merge_rules.md:26` for the per-OPT engine. The KB must be updated to reflect the new behavior.

## 3. Worked Example — the investigated OPT

**OPT:** Item `1114058292`, color `ECRU_MEL`, store `HB11`, category `M_BOXER`, OPT_TYPE=TBL

| field | value | trigger? |
|---|---|---|
| `M_YARN_02` | `'NA'` | yes → `SEC_CAP_GRID_NULL[MJ_M_YARN_02]` |
| `M_YARN_02_MBQ_ORIG` | `0.0` (explicit, NOT NULL) | yes → `SEC_CAP_MBQ_ZERO[MJ_M_YARN_02]` |
| Grid Builder config for `MJ_M_YARN_02` | `sec_cap_applicable=True`, `sec_cap_pct=120` | qualifies |
| `GH_M_YARN_02` for `M_BOXER` | `1` | qualifies |

**Today:** SHIP=18, HOLD=10, STATUS=ALLOCATED, ALLOC_REMARKS=`B[TBL.r1.rk4] ship=18 hold=10 ... seq=3131`

**After change:** SHIP=0, HOLD=0, STATUS=SKIPPED, ALLOC_REMARKS=`B[TBL.r1.rk4] ship=0 hold=0; SKIP=SEC_CAP_GRID_NULL[MJ_M_YARN_02],SEC_CAP_MBQ_ZERO[MJ_M_YARN_02] grain=(HB11,M_BOXER,NA)`

## 4. Code Changes

**One file:** `backend/app/services/rule_engine_per_opt.py`. No changes to `rule_engine_new.py`, `rule_engine_pandas.py`, `rule_engine_parallel_sql.py`, `rule_engine_parallel_python.py`, or any SQL.

### 4.1 `build_sec_cap_state` (lines 208–332)

Add a `hard_block` dict alongside the existing `configured` and `ceilings` dicts:

```python
hard_block[g_name][grain] = []   # list of reason tags for this grain
```

Populate while iterating grains:

- If `grain[grid_pos]` is `None` or `pd.isna(grain[grid_pos])` or `str(grain[grid_pos]).upper() == 'NA'`:
  → append `f"SEC_CAP_GRID_NULL[{g_name}]"`
- If `not mbq_null and float(_mbq_raw) == 0.0`:
  → append `f"SEC_CAP_MBQ_ZERO[{g_name}]"`
- If `mbq_null`:
  → append `f"SEC_CAP_NULL[{g_name}]"`

Only populate when `sec_cap_applicable` for the grid (already a precondition of the spec build) AND `gh_flag == 1` for the OPT's MAJ_CAT (already in the spec; verify it's threaded into `hard_block` evaluation).

### 4.2 `_evaluate_sec_cap_per_opt` (lines 335–465)

**Before** the existing per-grid loop's `ceiling <= 0 → continue` check, insert:

```python
reasons = sec_cap_state.get("hard_block", {}).get(g_name, {}).get(grain, [])
if reasons:
    return BreachResult(
        action="block",
        grid=g_name,
        grain=grain,
        reason=",".join(reasons),
    )
```

The existing `continue` for `ceiling <= 0` remains unchanged — it only fires for grains NOT in `hard_block` (i.e., a grain where the grid value is real and MBQ_ORIG is real but the upstream skip path still applies). In practice this branch becomes near-unreachable after the change; leaving it in place is defensive and zero-risk.

### 4.3 ALLOC_REMARKS formatting

The per-OPT loop already appends breach reasons to `ALLOC_REMARKS` when a block fires. Verify the existing format produces:

```
SKIP=<reason1>,<reason2> grain=(<RDC>,<MAJ_CAT>,<grid_val>)
```

If the current formatter only emits one reason, adjust it to accept the comma-joined reason string as-is.

### 4.4 Defensive log line

In `_run_band_per_opt`, when a hard-block fires, emit `logger.info(f"per_opt: hard-block OPT={opt_key} grid={g_name} grain={grain} reasons={reasons}")` so post-deploy DB scans can be cross-checked against application logs.

## 5. Test Plan

### 5.1 Unit cases (synthetic grains)

| # | Grid value | MBQ_ORIG | sec_cap_applicable | GH | Expected |
|---|---|---|---|---|---|
| 1 | `'NA'` | `0.0` | True | 1 | BLOCK, reasons=`SEC_CAP_GRID_NULL[X],SEC_CAP_MBQ_ZERO[X]` |
| 2 | `'NA'` | NULL | True | 1 | BLOCK, reasons=`SEC_CAP_GRID_NULL[X],SEC_CAP_NULL[X]` |
| 3 | `'COTTON'` | `0.0` | True | 1 | BLOCK, reasons=`SEC_CAP_MBQ_ZERO[X]` |
| 4 | `'COTTON'` | NULL | True | 1 | BLOCK, reasons=`SEC_CAP_NULL[X]` |
| 5 | `'COTTON'` | `50.0` | True | 1 | ADMIT, ceiling = 60.0 (50 × 120%) |
| 6 | `'NA'` | `0.0` | True | 0 | ADMIT (grid doesn't apply to this MAJ_CAT) |
| 7 | `'NA'` | `0.0` | False | 1 | ADMIT (grid not in sec-cap list) |
| 8 | `'COTTON'` | `0.0` | False | 1 | ADMIT (grid not in sec-cap list) |

### 5.2 Live regression on the investigated OPT

Re-run per-OPT mode on session `20260629_154555_543`'s MAJ_CAT slice for HB11/M_BOXER. Confirm:

- OPT `(HB11, M_BOXER, 1114058292, ECRU_MEL)` flips ALLOCATED→SKIPPED
- ALLOC_REMARKS contains both `SEC_CAP_GRID_NULL[MJ_M_YARN_02]` and `SEC_CAP_MBQ_ZERO[MJ_M_YARN_02]`
- All 18 OPTs in HB11/M_BOXER (which all have `M_YARN_02='NA'`) flip to SKIPPED
- An OPT with `M_YARN_02='COTTON'` and `M_YARN_02_MBQ_ORIG > 0` continues to ship per the normal cap

### 5.3 Full-session smoke

Run the engine end-to-end on the full session, expect:

- Roughly 62,363 OPTs flip ALLOCATED→SKIPPED
- Roughly 669,447 SHIP units stop dispatching
- No exceptions, no NULL-handling errors
- New rows in `ARS_ALLOC_HISTORY` with the new SKIP_REASON tags

## 6. Downstream Impact

Validated by `rule_ars` agent against current `backend/app/services/` (2026-06-30):

| Consumer | Reads ALLOC_STATUS? | Impact |
|---|---|---|
| Pak alignment (`rule_engine_per_opt.py:1054-1062`) | No — quantity-driven, runs inside per-OPT loop pre-SKIP | benign |
| HOLD process (`parked_history.py:1414-1498`) | No — filters `HOLD_QTY > 0` / `FROM_HOLD_QTY > 0` | benign |
| Pending allocation (`pend_alc_service.py:2304, 2331`) | No — filters `ALLOC_QTY > 0` | benign |
| Listing rebuild (`listing_allocator.py:1190-1205`) | No — deducts by `ROUND_ALLOC` (=0 for SKIPPED) | benign |
| MSA next cycle (`msa_service.py:76, 208`) | No — reads `ARS_PEND_ALC` only | benign; next cycle's `MSA_FNL_Q` rises because less PEND deducted (blocked allocations are delayed, not killed) |
| Grid Builder rebuild | No ALLOC_STATUS refs | benign |
| **Audit / gap report** (`gap_report.py:227-262`) | **Yes — counts SKIP_REASON taxonomy** | **needs new legend entries** for the three new tags, else they land as "other" |
| Dashboard (`ars_dashboard.py:1475, 1599`) | Displays ALLOC_STATUS as label only | benign (numbers change, semantics don't) |

**Operational expectation:** 90% of session SHIP volume will stop dispatching until upstream Grid Builder writes a non-zero `M_YARN_02_MBQ_ORIG` (and a non-`NA` `M_YARN_02` value) for those grains. These OPTs are not lost — they will dispatch in subsequent cycles once classification catches up.

## 7. Out of Scope

- Changes to `rule_engine_new.py`, `rule_engine_pandas.py`, or parallel variants. Per-OPT mode is the live path.
- Grid Builder fix for the `M_YARN_02='NA'` catch-all bucket. That's a separate upstream concern; this spec only changes the engine's response to the current (broken) upstream state.
- Operator UI / dashboard changes beyond appending the three new SKIP_REASON tags to `gap_report.py`'s legend.
- Backfill of historical `ARS_ALLOC_HISTORY` rows. The change applies to the next session run forward only.

## 8. Risks

| Risk | Mitigation |
|---|---|
| 90% drop in shipped units is large; operators may be surprised on first run | Pre-deploy: notify operator (santosh) with the impact table from `rule_ars` scan; suggest running once in a non-prod / shadow run first |
| `gap_report.py` legend not updated → blocked OPTs appear under "other" SKIP_REASON bucket | Include the legend update in the implementation plan |
| Edge case where `<grid>` is `'na'` (lowercase) or has whitespace (`'NA '`) | The condition uses `str(grain[grid_pos]).upper().strip() == 'NA'` to normalize |
| `merge_rules.md` and `INDEX.md` invariant 3 wording will become stale | Implementation plan includes a KB update to reflect the new behavior |

## 9. Acceptance

- All unit cases in §5.1 pass
- Live regression on the investigated OPT (§5.2) shows the expected SKIPPED outcome with the correct SKIP_REASON tags
- Full-session smoke (§5.3) shows the ~62k OPT / ~669k unit flip with no exceptions
- Gap report UI displays the three new SKIP_REASON tags as distinct rows in the skip-reason legend
- KB files updated (`merge_rules.md`, `INDEX.md`)
