# FSD: Sec-Cap Hard-Block Precedence Over Primary Overshoot Admit

| Field | Value |
|---|---|
| **Date** | 2026-07-13 |
| **Owner** | santosh@v2kart.com |
| **File touched** | `backend/app/services/rule_engine_per_opt.py` (per-OPT allocation mode only) |
| **Lines touched** | 425-544 (single function: `_evaluate_sec_cap_per_opt`) |
| **SQL / schema changes** | None |
| **Feature flag** | None (bug fix, no flag) |
| **Related specs** | `docs/superpowers/specs/2026-06-30-sec-cap-empty-grid-hard-block-design.md` (hard-block spec), 2026-07-04 unified overshoot rule (see MEMORY entries `project_sec_cap_primary_overshoot`, `project_cap_validation_mbq_only`) |
| **Status** | Ready for implementation |

---

## 1. Executive summary

Two independently-correct features interact incorrectly today: the **2026-06-30 hard-block** (empty grid value or MBQ_ORIG=0 must veto an OPT) and the **2026-07-04 Primary overshoot admit** (breach ≤ 0.5 × intended → admit). Because `_evaluate_sec_cap_per_opt` returns on the first grid that reaches a decision, an OPT that breaches an earlier Primary grid at the boundary can be admitted via overshoot BEFORE the loop reaches a downstream grid that would have hard-blocked it. The OPT ships in violation of the veto.

**Fix:** Split `_evaluate_sec_cap_per_opt` into two passes. Pass 1 scans every grid for hard-block conditions only; if any grid vetoes, return block immediately. Pass 2 (only reached if no veto) runs today's Primary-first breach/override logic unchanged.

**Invariant established:** veto beats override, unconditionally.

---

## 2. Problem — reproducible on the dev server

### 2.1 Observed case

Session `20260707_170050_291`, WERKS `HO10`, MAJ_CAT `L_JEANS` (DW01), GEN_ART `1121112392`, CLR `D_GRY`. OPT_TYPE = TBL. Ship-side intent = 16 pcs across 6 sizes.

The `MJ_M_YARN_02` grid at (HO10, L_JEANS, **OD**) has `MBQ_ORIG = 0` and `DISP_Q = 0` — a textbook `SEC_CAP_MBQ_ZERO[MJ_M_YARN_02]` veto per the 2026-06-30 spec. Yet the OPT was **ALLOCATED** with 16 ship + 3 hold.

Two sibling OPTs at the same store, same MAJ_CAT, same `M_YARN_02=OD` grain **were** correctly blocked in the same session:

| OPT | Type | Intended | Outcome | Reason |
|---|---|---|---|---|
| GA 1121116404 OLV | RL | 10 | SKIPPED | `SEC_CAP_MBQ_ZERO[MJ_M_YARN_02]` ✓ |
| GA 1121114402 BRW | TBC | 5 | SKIPPED | `SEC_CAP_MBQ_ZERO[MJ_M_YARN_02]` ✓ |
| **GA 1121112392 D_GRY** | **TBL** | **16** | **ALLOCATED ✗** | admitted via MJ overshoot, MJ_M_YARN_02 never evaluated |

Difference: OLV and BRW had `intended_ship` small enough to fit inside the MJ Primary budget, so the loop passed MJ cleanly and reached MJ_M_YARN_02. D_GRY breached MJ (16 vs 14 free budget), the overshoot admit fired at MJ (overshoot=1 ≤ 0.5×16=8), and `_evaluate_sec_cap_per_opt` returned before iterating to MJ_M_YARN_02.

### 2.2 Reproducing the bug on the dev server

Run against `Rep_Data` DB on the dev SQL Server (SQLAlchemy engine `data_engine`):

```sql
-- The bug row — confirms an OPT with SEC_CAP_MBQ_ZERO grain got ALLOCATED
SELECT SESSION_ID, WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR, M_YARN_02,
       OPT_TYPE, SUM(SHIP_QTY) AS SHIP, SUM(HOLD_QTY) AS HOLD,
       MAX(ALLOC_STATUS) AS STATUS, MAX(ALLOC_REMARKS) AS REMARKS
  FROM ARS_ALLOC_HISTORY
 WHERE SESSION_ID   = '20260707_170050_291'
   AND WERKS        = 'HO10'
   AND GEN_ART_NUMBER = 1121112392
   AND CLR          = 'D_GRY'
 GROUP BY SESSION_ID, WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR, M_YARN_02, OPT_TYPE;
```

Expected today: SHIP=16, HOLD=3, STATUS=ALLOCATED, REMARKS contains
`MBQ_CAP_OVERSHOOT(TBL,...); B[TBL.r1.rk2] ship=1 hold=0; SEC_CAP_OVERSHOOT(grid=MJ, cap=110%, ..., would_have_blocked=true);`

```sql
-- Confirm the OD grain is hard-block-configured (MBQ = 0)
SELECT WERKS, MAJ_CAT, M_YARN_02, MBQ, DISP_Q, OPT_CNT, STK_TTL
  FROM ARS_GRID_MJ_M_YARN_02
 WHERE WERKS='HO10' AND MAJ_CAT='L_JEANS';
```

Expected: two rows — `DNM_SLD` (MBQ=1272), `OD` (**MBQ=0**).

```sql
-- Confirm siblings on the same OD grain WERE hard-blocked correctly
SELECT GEN_ART_NUMBER, CLR, OPT_TYPE, MAX(SKIP_REASON) AS SKIP
  FROM ARS_ALLOC_HISTORY
 WHERE SESSION_ID = '20260707_170050_291'
   AND WERKS = 'HO10' AND MAJ_CAT = 'L_JEANS' AND M_YARN_02 = 'OD'
 GROUP BY GEN_ART_NUMBER, CLR, OPT_TYPE
 ORDER BY OPT_TYPE, GEN_ART_NUMBER;
```

Expected: OLV (RL) and BRW (TBC) show `SEC_CAP_MBQ_ZERO[MJ_M_YARN_02]`; D_GRY (TBL) shows NULL/empty (allocated, no skip).

---

## 3. Rule (new invariant)

**Hard-block always wins.** For a given OPT, if ANY sec-cap grid (Primary or Secondary) carries a hard-block reason for the OPT's grain, the OPT is SKIPPED regardless of whether an earlier grid in the eval order would admit via the overshoot allowance.

Formally, `_evaluate_sec_cap_per_opt` becomes two-pass:

- **Pass 1 (veto scan, order-independent):** iterate every grid, collect every `hard_block_reasons` entry for the OPT's grain. If the collection is non-empty, return `action="block"` with the combined reasons list. Do NOT enter Pass 2.

- **Pass 2 (breach scan, Primary-first):** current logic, unchanged. Only reached when Pass 1 finds no vetoes.

`participating` — the list of grid/grain tuples returned to the caller for `running` advance — retains today's semantic: only grids the OPT actually contributes ship to are added. Vetoed OPTs contribute nothing, so `participating` stays empty on the Pass 1 return path.

---

## 4. Exact code change

**File:** `backend/app/services/rule_engine_per_opt.py`

**Function:** `_evaluate_sec_cap_per_opt` (currently lines 387–544)

Replace the function body between line 425 and line 544 with the two-pass version below. The signature, the return-type contract, and the caller in `_run_band_per_opt` are unchanged.

### 4.1 Before (current code, lines 425–544)

The current loop starts at line 425 with `for g_name, g_meta in sec_cap_state["grids"]:`, then inside a single iteration checks in order: `gh_map` filter → `hard_block` (line 436) → `configured/ceiling` skip (line 462) → `breach_now` (line 467) → Primary branch (line 480) → Secondary branch (line 511) → `return`. The function is quoted verbatim in the spec's git history at [rule_engine_per_opt.py:425-544](../../../backend/app/services/rule_engine_per_opt.py#L425-L544).

### 4.2 After (new code — full replacement for lines 425–544)

```python
    # =========================================================================
    # Pass 1 — veto scan (order-independent).
    # Every hard-block condition (per 2026-06-30 spec: MBQ_ORIG NULL, MBQ_ORIG=0
    # explicit, grid-extra NULL/'NA') must veto the OPT regardless of what any
    # other grid says. This runs BEFORE Pass 2's Primary-first breach/override
    # so an earlier-grid overshoot admit cannot shadow a downstream veto.
    # See docs/superpowers/specs/2026-07-13-sec-cap-hard-block-precedence-design.md
    # =========================================================================
    vetoing: List[Tuple[str, tuple, Dict[str, Any], list]] = []
    for g_name, g_meta in sec_cap_state["grids"]:
        gh_map = sec_cap_state["gh_applies"].get(g_name, {})
        if gh_map and not gh_map.get(maj_cat, True):
            continue
        extras = list(g_meta.get("extras") or [])
        grain = tuple([str(werks_v), maj_cat] + [str(row0.get(e, "")) for e in extras])
        hb_reasons = sec_cap_state.get("hard_block", {}).get(g_name, {}).get(grain)
        if hb_reasons:
            vetoing.append((g_name, grain, g_meta, list(hb_reasons)))

    if vetoing:
        v_grid, v_grain, v_meta, _ = vetoing[0]
        combined_reasons: list = []
        for _, _, _, rs in vetoing:
            for r in rs:
                if r not in combined_reasons:
                    combined_reasons.append(r)
        info = {
            "action":             "block",
            "grid":               v_grid,
            "grain":              v_grain,
            "budget":             sec_cap_state["budgets"].get(v_grid, {}).get(v_grain, 0.0),
            "ceiling":            sec_cap_state["ceilings"].get(v_grid, {}).get(v_grain, 0.0),
            "stk":                sec_cap_state["stks"].get(v_grid, {}).get(v_grain, 0.0),
            "mbq":                sec_cap_state["mbqs"].get(v_grid, {}).get(v_grain, 0.0),
            "cap_pct":            float(v_meta.get("cap_pct") or 100.0),
            "run_before":         sec_cap_state["running"][v_grid].get(v_grain, 0.0),
            "configured":         sec_cap_state.get("configured", {}).get(v_grid, {}).get(v_grain, True),
            "mj_req_rem":         float((mj_req_rem_dict or {}).get(str(werks_v), 0.0)),
            "opt_mbq":            0.0,
            "threshold":          0.0,
            "intended_ship":      intended_ship,
            "hard_block_reasons": combined_reasons,
        }
        # participating stays empty — vetoed OPTs contribute no ship, so no
        # `running` advance should happen for any grid they touched.
        return info, participating

    # =========================================================================
    # Pass 2 — breach scan (Primary-first). Current logic; hard_block branch
    # removed (already handled above in Pass 1).
    # =========================================================================
    for g_name, g_meta in sec_cap_state["grids"]:
        gh_map = sec_cap_state["gh_applies"].get(g_name, {})
        if gh_map and not gh_map.get(maj_cat, True):
            continue
        extras = list(g_meta.get("extras") or [])
        grain = tuple([str(werks_v), maj_cat] + [str(row0.get(e, "")) for e in extras])
        # `configured` is False ONLY when MBQ_ORIG was NULL at this grain.
        # NULL-MBQ grains would have been vetoed in Pass 1; reaching Pass 2
        # implies configured is True or MBQ_ORIG > 0 (explicit-zero MBQ was
        # also vetoed in Pass 1 as of the 2026-06-30 spec, so `ceiling <= 0`
        # here means an unusual data state — leave the legacy skip in place
        # for safety).
        configured = sec_cap_state.get("configured", {}).get(g_name, {}).get(grain, True)
        ceiling = sec_cap_state["ceilings"].get(g_name, {}).get(grain, 0.0)
        if configured and ceiling <= 0:
            continue
        budget = sec_cap_state["budgets"].get(g_name, {}).get(grain, 0.0)
        run_before = sec_cap_state["running"][g_name].get(grain, 0.0)
        participating.append((g_name, grain))
        breach_now = (not configured) or (run_before + intended_ship > budget)
        if breach_now:
            if bool(g_meta.get("is_primary", False)):
                overshoot = float(run_before + intended_ship - budget)
                primary_threshold = 0.5 * intended_ship
                overshoot_admit_ok = (
                    configured
                    and overshoot > 0
                    and overshoot <= primary_threshold
                )
                action = "override" if overshoot_admit_ok else "block"
                info = {
                    "action":        action,
                    "grid":          g_name,
                    "grain":         grain,
                    "budget":        budget,
                    "ceiling":       ceiling if configured else 0.0,
                    "stk":           sec_cap_state["stks"].get(g_name, {}).get(grain, 0.0),
                    "mbq":           sec_cap_state["mbqs"].get(g_name, {}).get(grain, 0.0),
                    "cap_pct":       float(g_meta.get("cap_pct") or 100.0),
                    "run_before":    run_before,
                    "configured":    configured,
                    "mj_req_rem":    float((mj_req_rem_dict or {}).get(str(werks_v), 0.0)),
                    "opt_mbq":       0.0,
                    "threshold":     primary_threshold,
                    "intended_ship": intended_ship,
                    "overshoot":     overshoot,
                }
                if action == "block":
                    info["primary_block"] = True
                else:
                    info["overshoot_admit"] = True
                return info, participating
            overshoot = float(run_before + intended_ship - budget)
            threshold = 0.5 * intended_ship
            override_eligible = (
                configured
                and overshoot > 0
                and overshoot <= threshold
            )
            action = "override" if override_eligible else "block"
            info = {
                "action":        action,
                "grid":          g_name,
                "grain":         grain,
                "budget":        budget,
                "ceiling":       ceiling if configured else 0.0,
                "stk":           sec_cap_state["stks"].get(g_name, {}).get(grain, 0.0),
                "mbq":           sec_cap_state["mbqs"].get(g_name, {}).get(grain, 0.0),
                "cap_pct":       float(g_meta.get("cap_pct") or 130.0),
                "run_before":    run_before,
                "configured":    configured,
                "mj_req_rem":    float((mj_req_rem_dict or {}).get(str(werks_v), 0.0)),
                "opt_mbq":       0.0,
                "threshold":     threshold,
                "intended_ship": intended_ship,
                "overshoot":     overshoot,
            }
            if action == "override":
                info["overshoot_admit"] = True
            return info, participating
    return None, participating
```

### 4.3 Also update the function docstring

The docstring at lines 394–418 references the current single-pass contract. Update to reflect the new two-pass contract. Suggested replacement for the paragraph starting "Override rule (unified 2026-07-04 …)":

```
    Two-pass evaluation (2026-07-13):

    Pass 1 — veto scan (order-independent). Iterate every grid and collect
    hard_block reasons per the 2026-06-30 spec (empty grid value, explicit
    MBQ_ORIG=0, NULL MBQ_ORIG). If any grid vetoes, return action="block"
    with the combined `hard_block_reasons` list. `participating` stays
    empty so `running` is not advanced.

    Pass 2 — breach scan (Primary-first, unchanged from 2026-07-04). Only
    reached when Pass 1 finds no veto. Applies the unified overshoot rule
    (admit when breach <= 0.5 × intended_ship) to Primary and Secondary
    grids alike. Returns on the first grid that reaches a decision.

    Invariant: veto > override. A hard-block on ANY grid vetoes the OPT
    regardless of what any other grid would admit.
```

### 4.4 Caller — unchanged

The single caller ([rule_engine_per_opt.py:981-1010](../../../backend/app/services/rule_engine_per_opt.py#L981-L1010)) already handles `action="block"` with `hard_block_reasons`; the new Pass 1 return matches that shape exactly. No caller-side changes required.

---

## 5. Test plan

### 5.1 Regression against the observed session (required before deploy)

Run the per-OPT allocator on a single-MAJCAT slice (HO10 / L_JEANS) with the patch applied. Slice the ARS_LISTING inputs from session `20260707_170050_291`.

Assertions:

1. OPT `(HO10, L_JEANS, 1121112392, D_GRY, TBL)` — `ALLOC_STATUS` flips from `ALLOCATED` to `SKIPPED`, `SHIP_QTY` sum → 0, `HOLD_QTY` sum → 0.
2. `SKIP_REASON` for that OPT contains `SEC_CAP_MBQ_ZERO[MJ_M_YARN_02]`.
3. `POOL_CONSUMED` sum for that OPT → 0 (was 19). FNL_Q_REM for the (RDC, GEN_ART, SZ) restored.
4. `mj_req_rem_dict['HO10']` after the D_GRY OPT stays at **14** (was decremented to **−2**).
5. Sibling OPTs on the same OD grain — GA `1121116404` OLV RL and GA `1121114402` BRW TBC — remain `SKIPPED` with `SEC_CAP_MBQ_ZERO[MJ_M_YARN_02]`. Their outcome is unchanged.
6. `SEC_CAP_OVERSHOOT(grid=MJ, ...)` narrative is NOT stamped on the D_GRY OPT's ALLOC_REMARKS (`_stamp_sec_cap_override` never fires because Pass 1 short-circuits Pass 2).

### 5.2 Overshoot still works (no false-positive block)

Construct a synthetic OPT that breaches MJ Primary by ≤ 0.5 × intended AND has NO downstream veto (all its Secondary grids have valid MBQ). Expected: `ALLOC_STATUS=ALLOCATED`, `SEC_CAP_OVERSHOOT(grid=MJ,...)` stamped, behavior identical to pre-change.

### 5.3 Multi-grid veto composition

Construct a synthetic OPT that would be vetoed by MJ_FIT AND MJ_M_YARN_02 simultaneously. Expected: `hard_block_reasons` list carries BOTH `SEC_CAP_MBQ_ZERO[MJ_FIT]` and `SEC_CAP_MBQ_ZERO[MJ_M_YARN_02]`; `info["grid"]` is the first-encountered vetoing grid (deterministic per `sec_cap_state["grids"]` sort order); `SHIP=0, HOLD=0`.

### 5.4 Veto short-circuits Primary breach

Construct a synthetic OPT that would breach MJ Primary (overshoot-eligible) AND has a downstream veto. Expected: no `SEC_CAP_OVERSHOOT` narrative in ALLOC_REMARKS, `SKIP_REASON` is the veto tag not `PRIMARY_CAP_PRE_MJ`, `sec_cap_state["running"]` for MJ is NOT advanced (`participating` empty on veto return).

### 5.5 Unit test file

Add `backend/tests/test_evaluate_sec_cap_per_opt_precedence.py`. Use pandas DataFrames built inline (no DB). Cover the four cases above. Aim for ≥ 4 pytest cases, all green.

---

## 6. Post-deploy verification (run on the prod server)

After the deploy in Section 7 completes and the next allocation session finishes:

```sql
-- (a) No new SEC_CAP_OVERSHOOT admits on OPTs sitting on an OD-style veto grain
SELECT COUNT(*) AS shadowed_overshoots
  FROM ARS_ALLOC_HISTORY h
  JOIN ARS_GRID_MJ_M_YARN_02 g
    ON g.WERKS = h.WERKS AND g.MAJ_CAT = h.MAJ_CAT AND g.M_YARN_02 = h.M_YARN_02
 WHERE h.SESSION_ID       = '<NEW_SESSION_ID>'
   AND h.ALLOC_STATUS     = 'ALLOCATED'
   AND h.ALLOC_REMARKS LIKE '%SEC_CAP_OVERSHOOT%'
   AND g.MBQ              = 0;
```

Expected: **0**. If non-zero, the fix is not effective for that grid pattern — escalate.

```sql
-- (b) Skips with SEC_CAP_MBQ_ZERO or SEC_CAP_GRID_NULL should appear in the new session
SELECT SKIP_REASON, COUNT(DISTINCT CONCAT(WERKS,'|',MAJ_CAT,'|',GEN_ART_NUMBER,'|',CLR)) AS opts
  FROM ARS_ALLOC_HISTORY
 WHERE SESSION_ID = '<NEW_SESSION_ID>'
   AND SKIP_REASON LIKE '%SEC_CAP_MBQ_ZERO%'
    OR SKIP_REASON LIKE '%SEC_CAP_GRID_NULL%'
 GROUP BY SKIP_REASON
 ORDER BY opts DESC;
```

Expected: non-zero counts, consistent with prior sessions plus the newly-caught shadowed cases.

```sql
-- (c) Overshoot admits still fire for legitimate cases (should be non-zero, roughly
--     the pre-fix count minus shadowed cases)
SELECT COUNT(*) AS legit_overshoots
  FROM ARS_ALLOC_HISTORY
 WHERE SESSION_ID = '<NEW_SESSION_ID>'
   AND ALLOC_REMARKS LIKE '%SEC_CAP_OVERSHOOT%'
   AND ALLOC_STATUS = 'ALLOCATED';
```

Expected: > 0. If 0, the overshoot path may have regressed — investigate.

---

## 7. Deploy steps

Standard V2 Retail Azure App Service deploy per `CLAUDE.md`:

```bash
# 1. Pull latest, apply the patch, run tests locally
git checkout -b fix/sec-cap-hard-block-precedence
# ... apply Section 4.2 changes to backend/app/services/rule_engine_per_opt.py ...
cd backend
./venv/Scripts/python.exe -m pytest tests/test_evaluate_sec_cap_per_opt_precedence.py -v

# 2. Commit and push (dev branch first — do NOT push directly to main)
git add app/services/rule_engine_per_opt.py tests/test_evaluate_sec_cap_per_opt_precedence.py
git commit -m "fix(per_opt): sec-cap hard-block precedence over Primary overshoot admit

Split _evaluate_sec_cap_per_opt into two passes: veto scan then breach scan.
Prevents Primary overshoot admits from shadowing downstream hard-blocks.

Fixes shadowing case: HO10/L_JEANS/1121112392/D_GRY in session
20260707_170050_291 — allocated 16+3 despite MJ_M_YARN_02 OD grain
having MBQ_ORIG=0.

Ref: docs/superpowers/specs/2026-07-13-sec-cap-hard-block-precedence-design.md"

git push origin fix/sec-cap-hard-block-precedence

# 3. Merge PR → main after review

# 4. Deploy to Azure (from CLAUDE.md deploy block)
TOKEN=$(curl -s -X POST "https://login.microsoftonline.com/$AZURE_TENANT/oauth2/v2.0/token" \
  -d "client_id=$AZURE_CLIENT" -d "client_secret=$AZURE_SECRET" \
  -d "scope=https://management.azure.com/.default" -d "grant_type=client_credentials" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
cd backend
zip -r /tmp/ars-deploy.zip . -x "__pycache__/*" "venv/*" "logs/*" "*.pyc" ".env"
curl -X POST "https://ars-v2retail-api.scm.azurewebsites.net/api/zipdeploy?isAsync=true" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/zip" \
  --data-binary @/tmp/ars-deploy.zip
sleep 120 && curl -s "https://ars-v2retail-api.azurewebsites.net/health"

# 5. Kick off a validation session on a small slice (one MAJCAT, few stores)
#    and run Section 6 verification queries against the new SESSION_ID.
```

---

## 8. Rollback plan

- **Trigger:** any of the Section 6 queries returns unexpected results, or an operator-visible regression appears (spike in `SEC_CAP_MBQ_ZERO` skips beyond expectation, missing overshoot narratives).
- **Action:** revert the commit and redeploy the previous artifact.

```bash
git revert <commit-sha>
git push origin main
# repeat Section 7 step 4 (Azure zipdeploy)
```

The change is a pure Python-side behavior change with no data migration; revert is atomic on the next deploy. Historical `ARS_ALLOC_HISTORY` rows are not touched by either the fix or the revert.

---

## 9. Non-goals

- **No change to the hard-block trigger set.** The set of vetoes (`SEC_CAP_MBQ_ZERO`, `SEC_CAP_NULL`, `SEC_CAP_GRID_NULL`) and the `hard_block` map built by `build_sec_cap_state` are unchanged.
- **No change to the overshoot arithmetic.** Threshold stays `0.5 × intended_ship`. Primary and Secondary overshoot logic unchanged.
- **No change to `running` accounting semantics.** Admitted OPTs still advance `running` via `participating`. Vetoed OPTs still don't.
- **No SQL / no post-pass gate touched.** The legacy SQL sec-cap gate in `rule_engine_new.py` is out of scope.
- **`_stamp_sec_cap_override` unchanged.** It only fires on `action="override"`; that path never runs when Pass 1 vetoes.
- **No change to `rule_engine_new.py`, `rule_engine_pandas.py`, `rule_engine_parallel_*.py`, or any other file.**

---

## 10. Open questions (park until after main fix lands)

1. **Should the audit trail carry both the primary overshoot narrative AND the downstream veto?** Current spec drops the overshoot narrative when Pass 1 short-circuits. Alternative: run Pass 2 anyway for the narrative, but let Pass 1's veto override the final status. Adds complexity; useful for post-mortems. Recommend: defer, revisit if operators request it.

2. **Is `is_primary=True` sort ordering still needed** after this fix? With hard-blocks moved out of the ordering-sensitive path, Primary-first only affects the choice between multiple non-veto breaches. If two grids both breach with overshoot-admit-eligible margins, Primary wins today. Retaining this feels right (Primary is the tighter constraint). Recommend: keep, no change.

3. **Are there other veto-style conditions elsewhere in the pipeline** (R07, TBL_MJ_REQ_GATE, ALREADY_STOCKED) that could suffer analogous shadowing by overshoot admits? Out of scope for this spec; commission a follow-up audit if there is any doubt.
