---
title: Per-Grid SEC-CAP Applicability — Implemented
category: Allocation
order: 13
source: backend/app/services/rule_engine_new.py, backend/app/api/v1/endpoints/grid_builder.py, frontend/src/pages/GridBuilderPage.jsx
status: Implemented (2026-05-17)
last_reviewed: 2026-05-17
---

# Per-Grid SEC-CAP Applicability — Implemented

> **Status — Implemented 2026-05-17.** Decisions from the proposal stage are preserved below for traceability. See "Implementation log" at the bottom for the actual changes that landed.
>
> Operator action required: the backfill set `sec_cap_applicable=0` for every existing grid, so on the first listing run after deploy **no Secondary grids participate in sec-cap**. Open Grid Builder → toggle ON the grids you want capped (typically MJ_FAB / MJ_MICRO_MVGR / MJ_MACRO_MVGR) before the next dispatch cycle.

## Why this proposal exists

Today the Secondary-grid cap (`_apply_sec_grid_cap_pre_gate` in `rule_engine_new.py`) is one global toggle that applies the same `cap_pct` (~130%) to **every** Secondary grid that is `status='ACTIVE'`. From the operator's seat that creates three problems:

1. **No selective participation.** You can't ask the engine "cap on FAB and MICRO_MVGR but ignore CLR" — CLR is too granular and blocks otherwise-good OPTs because a single colour breach trips the whole OPT.
2. **Pivot-only grids leak into cap math.** `MJ_GEN_ART` and `MJ_VAR_ART` are flagged `pivot_only=1` (they exist for pivoting, not for cap arithmetic) but they still satisfy `grid_group='Secondary' AND status='ACTIVE'`, so they slip into the loop.
3. **One % fits all.** You cannot run FAB at 150% and MICRO_MVGR at 130%. The dial moves uniformly.

The fix is a per-grid flag (+ optional per-grid %) on `ARS_GRID_BUILDER`, surfaced in the Grid Builder UI.

## Proposed schema change (`ARS_GRID_BUILDER`)

Two new columns. Both follow the existing idempotent-ALTER pattern in `_ensure_grid_table` (alongside `pivot_only`, `use_for_opt_sale`, `weightage`).

| Column | Type | Default | Purpose |
|---|---|---|---|
| `sec_cap_applicable` | `BIT NOT NULL` | `0` | `1` → this Secondary grid participates in sec-cap math. `0` → engine ignores it for caps (still pivots, still feeds REQ_REM, but no cap math). |
| `sec_cap_pct` | `FLOAT NULL` | `NULL` | Per-grid override of the cap percentage. `NULL` → fall back to the global default (130). Set 120 / 140 / 150 / etc. for a specific dimension. |

### Backfill on first deploy (chosen: OFF for ALL Secondary)

```sql
UPDATE ARS_GRID_BUILDER
SET sec_cap_applicable = 0,
    sec_cap_pct        = NULL
WHERE sec_cap_applicable IS NULL;
```

**Operator effect on first run after migration:** zero rows participate in sec-cap. Every OPT becomes eligible to ship more than before. Operators then flip on the specific grids they want capping (e.g. FAB, MICRO_MVGR) and tune `sec_cap_pct` per grid.

> If anyone wants the preserve-current-behaviour path instead, the alternative
> backfill is documented in the conversation log: default ON for everything
> where `pivot_only=0 AND grid_group='Secondary'`. The choice on 2026-05-17 was
> OFF-for-all — note it for handover.

## Proposed UI change (Grid Builder page, `frontend/src/pages/`)

Add **two new columns** between the existing `GROUP` and `WT` columns:

| #  | GRID NAME | OUTPUT TABLE | HIERARCHY | KPI | GROUP | **SEC-CAP** | **CAP %** | WT | LAST RUN | STATUS |
|----|-----------|--------------|-----------|-----|-------|-------------|-----------|----|----------|--------|
| 1  | MJ              | ARS_GRID_MJ              | WERKS, MAJ_CAT          | All       | Primary   | — (locked)   | — | 1 | … | Active |
| 2  | MJ_RNG_SEG      | ARS_GRID_MJ_RNG_SEG      | …, RNG_SEG              | OPT_SALE  | Secondary | ☐ OFF        | (blank → 130) | 2 | … | Active |
| 3  | MJ_MACRO_MVGR   | ARS_GRID_MJ_MACRO_MVGR   | …, MACRO_MVGR           | All       | Secondary | ☐ OFF        | (blank → 130) | 3 | … | Active |
| 4  | MJ_MICRO_MVGR   | ARS_GRID_MJ_MICRO_MVGR   | …, MICRO_MVGR           | All       | Secondary | ☐ OFF        | (blank → 130) | 1 | … | Active |
| 5  | MJ_FAB          | ARS_GRID_MJ_FAB          | …, FAB                  | All       | Secondary | ☐ OFF        | (blank → 130) | 1 | … | Active |
| 6  | MJ_CLR          | ARS_GRID_MJ_CLR          | …, CLR                  | All       | Secondary | ☐ OFF        | (blank → 130) | 1 | … | Active |
| 7  | MJ_M_VND_CD     | ARS_GRID_MJ_M_VND_CD     | …, M_VND_CD             | All       | Secondary | ☐ OFF        | (blank → 130) | 1 | … | Active |
| 8  | MJ_GEN_ART      | ARS_GRID_MJ_GEN_ART      | …, GEN_ART_NUMBER, CLR  | PIVOT ONLY| Secondary | — (locked)   | — | 0 | … | Active |
| 9  | MJ_VAR_ART      | ARS_GRID_MJ_VAR_ART      | …, ARTICLE_NUMBER, …    | PIVOT ONLY| Secondary | — (locked)   | — | 0 | … | Active |

Rules for the controls:
- **SEC-CAP toggle is locked OFF** when `pivot_only = 1` or `grid_group = 'Primary'`. Primary uses the MJ_REQ cap path, not sec-cap.
- **CAP % input** is hidden until the toggle is ON. Blank = fall back to global `apply_sec_cap_in_normal` percentage. Numeric override stored in `sec_cap_pct`.
- Save uses the existing `PUT /api/v1/grid-builder/{id}` endpoint with the two new fields added to the update body.

## Proposed engine changes

| Where | Change | Why |
|---|---|---|
| `_discover_all_active_grids` (`rule_engine_new.py:2526`) | Pull `sec_cap_applicable` and `sec_cap_pct` into the returned dict — two new keys per grid. | Single source of truth. |
| `_apply_sec_grid_cap_pre_gate` (`rule_engine_new.py:2594`) | Filter to `grid["sec_cap_applicable"] is True` before iterating. Use `grid["sec_cap_pct"] or default_cap_pct` for that grid's cap. | Main-pass sec-cap honours the flag. |
| `_apply_sec_grid_cap` (legacy post-trim, used inside fallback) | Same filter + per-grid pct. | Fallback consistency. |
| `run_listing_and_allocation` orchestrator log | Add a line: `sec-cap participating: X of Y secondary grids ({grid: pct}, ...)`. | Audit visibility. |

**Backwards-compatibility shim:** if the two new columns are missing on the table (dev environments that didn't run the ALTER), `_discover_all_active_grids` should default `sec_cap_applicable = 0` and `sec_cap_pct = None` so the engine simply does nothing for sec-cap — safe.

## Migration plan (zero-downtime, three phases)

1. **Phase 1 — Additive schema.** Deploy the ALTERs only. Backfill SQL above runs once. No code changes yet → today's behaviour preserved (sec-cap still runs against every Secondary grid because the engine doesn't yet know about the new column).
2. **Phase 2 — Engine reads the flag.** Deploy engine changes. Because backfill set `sec_cap_applicable = 0` everywhere, the engine now caps **nothing**. This is intentional — it's the "off-by-default" choice from 2026-05-17. Run a listing cycle and confirm baseline shipped-units changes (likely increases — fewer rows blocked).
3. **Phase 3 — Operators opt in.** Toggle ON for the specific grids you want capping (FAB / MICRO_MVGR / MACRO_MVGR are the natural starting set). Re-run a cycle. Compare via the verification queries below.

UI deployment can land alongside Phase 2 or Phase 3 — there's no hard order. Backend tolerates the missing UI; UI tolerates the missing backend (rows would just show "—" in the SEC-CAP column).

## Verification queries

```sql
-- 1. Configuration snapshot — what's currently set
SELECT grid_name, grid_group, pivot_only,
       sec_cap_applicable, sec_cap_pct,
       weightage, seq, status
FROM ARS_GRID_BUILDER
ORDER BY ISNULL(seq, 999), grid_name;

-- 2. After a listing run, which grids actually triggered SEC_CAP?
--    Should only mention grids where sec_cap_applicable=1.
SELECT LEFT(SKIP_REASON, 40) AS reason, COUNT(*) AS rows_n
FROM ARS_ALLOC_WORKING
WHERE SKIP_REASON LIKE 'SEC_CAP_%'
GROUP BY LEFT(SKIP_REASON, 40)
ORDER BY rows_n DESC;

-- 3. Diff before/after toggling a grid: count of SHIPPED rows per MAJ_CAT.
--    Turn a grid OFF → expect SHIPPED to go UP (fewer blocks).
--    Turn a grid ON  → expect SHIPPED to go DOWN (new blocks).
SELECT MAJ_CAT,
       SUM(CASE WHEN ALLOC_STATUS = 'ALLOCATED' THEN 1 ELSE 0 END) AS allocated,
       SUM(CASE WHEN ALLOC_STATUS = 'PARTIAL'   THEN 1 ELSE 0 END) AS partial_,
       SUM(CASE WHEN ALLOC_STATUS = 'SKIPPED'   THEN 1 ELSE 0 END) AS skipped,
       SUM(CASE WHEN SKIP_REASON LIKE 'SEC_CAP_%' THEN 1 ELSE 0 END) AS sec_cap_blocks
FROM ARS_ALLOC_WORKING
GROUP BY MAJ_CAT
ORDER BY sec_cap_blocks DESC;
```

## Open risks

- **Pandas engine restart path** (`rule_engine_pandas.py:840`) re-seeds `#nre_pool` from `alloc.FNL_Q_REM`. The flag does not touch that path — but anyone implementing this should re-read `_apply_sec_grid_cap` in the pandas engine to confirm the same filter is applied there.
- **Sec-cap pre-gate overrides** (`SEC_CAP_PRE_OVERRIDE_OPT_REQ_PCT`) are still global. If you want per-grid override thresholds too (e.g. "FAB allows OPT_REQ ≥ 150% override; MICRO_MVGR allows OPT_REQ ≥ 200%"), that's a follow-up — out of scope here.
- **Audit trail** — the existing `SEC_CAP_<grid>` `SKIP_REASON` already names the responsible grid, so once Phase 2 lands the audit story is unchanged. The new log line ("sec-cap participating: …") is the only new audit surface.

## Decision log

| Date | Decision | Notes |
|---|---|---|
| 2026-05-17 | Backfill = OFF for all Secondary | Opt-in path chosen over preserve-current. Operators expected to flip the right subset before next dispatch cycle. |
| 2026-05-17 | Surface both flag and per-grid % | Full control needed — uniform 130% wasn't fitting FAB vs CLR uniformly. |
| 2026-05-17 | Persist proposal as doc | Lives at this path; not yet implemented. |

## Approval & implementation

Implemented 2026-05-17 in a single change set. The five-step plan in the original proposal was followed verbatim.

## Implementation log

| Step | File | What landed |
|---|---|---|
| 1 | `backend/app/api/v1/endpoints/grid_builder.py` | `_ensure_grid_table` adds two idempotent ALTERs: `sec_cap_applicable BIT NOT NULL DEFAULT 0` and `sec_cap_pct FLOAT NULL`. The `NOT NULL DEFAULT 0` on the new flag column means every existing row gets `0` automatically (the OFF-for-all backfill) — no separate UPDATE statement needed. |
| 2 | `backend/app/api/v1/endpoints/grid_builder.py` | `GridCreate` and `GridUpdate` Pydantic models gained `sec_cap_applicable: bool` and `sec_cap_pct: Optional[float]`. `_row_to_dict` surfaces both at positions r[19]/r[20]. All three SELECT statements include the two new columns. `create_grid` writes them into the INSERT (forced OFF for Primary/None/pivot_only). `update_grid` adds them to the dynamic SET-clause builder and runs a post-update normalization that auto-zeros the flag whenever the row no longer qualifies. |
| 3 | `backend/app/services/rule_engine_new.py` | `_discover_all_active_grids` SELECT now pulls `sec_cap_applicable` and `sec_cap_pct`. Each grid in the returned dict carries them. Backwards-compat fallback SELECT for older deployments without the two columns. MJ entry hardcoded to `False/None` (Primary never participates). |
| 3 | `backend/app/services/rule_engine_new.py` | `_apply_sec_grid_cap_pre_gate` filters out grids where `sec_cap_applicable` is false, records them in `audit["grids_skipped_not_applicable"]`. Budget computation uses each grid's own `cap_factor` (derived from `sec_cap_pct` if set, else the global default). Audit dict gains `cap_pct_by_grid`. Closing log line renders per-grid caps inline. |
| 4 | `frontend/src/pages/GridBuilderPage.jsx` | `EMPTY_FORM` includes the two fields. Modal gains "Apply Sec-Cap" toggle + "Sec-Cap %" input. Both locked when `grid_group !== 'Secondary'` or `pivot_only=1`. Table gains a Sec-Cap column showing `ON @<pct>%` / `OFF` / `—` (the dash for grids where the flag is locked). |
| 5 | `backend/app/docs/processes/rule_engine_walkthrough.md` | Step 2D row for `_apply_sec_grid_cap_pre_gate` updated to mention the flag + per-grid override. `last_reviewed` bumped to 2026-05-17. |

### Smoke test after deploy

1. Restart the API. First call to `GET /api/v1/grid-builder/grids` runs `_ensure_grid_table` and creates the two columns. Confirm:
   ```sql
   SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
   WHERE TABLE_NAME='ARS_GRID_BUILDER'
     AND COLUMN_NAME IN ('sec_cap_applicable','sec_cap_pct');
   -- expect 2 rows
   ```
2. Open Grid Builder UI. Every Secondary grid should show **Sec-Cap = OFF**. Pivot-only and Primary grids show **—**.
3. Toggle on MJ_FAB (for example), set Sec-Cap % to 130. Save.
4. Run listing + allocation. Check the engine log line `[sec_cap_pre] override>=... caps={MJ_FAB@130}`. Skipped grids appear in the same line.
5. The new column `cap_pct_by_grid` shows up in the result dict from `run_listing_and_allocation` when the audit is logged.
