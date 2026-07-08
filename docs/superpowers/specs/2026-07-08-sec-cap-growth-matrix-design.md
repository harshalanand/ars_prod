# Sec-Cap Growth Matrix — cont%-driven ceiling override

**Date:** 2026-07-08
**Owner:** santosh@v2kart.com
**Scope:**
- `backend/app/services/rule_engine_per_opt.py`
- `backend/app/api/v1/endpoints/grid_builder.py`
- `backend/app/services/sec_cap_growth_matrix.py` (new)
- `frontend/src/pages/GridBuilderPage.jsx` (new UI section)
**Status:** Spec — pending review before plan

---

## 1. Problem

Today the secondary-grid cap uses a single fixed `sec_cap_pct` value per grid (stored in `ARS_GRID_BUILDER.sec_cap_pct`, defaulting to 120). That factor becomes the ceiling for every grain of that grid:

```
ceiling(grain) = grain.MBQ_ORIG × (sec_cap_pct / 100)
```

Business ask: **small contributors deserve a bigger stretch.** A niche fabric that represents 2% of a MAJ_CAT's total MBQ should be allowed to grow past 120% — 300% is fine because the absolute quantity is small. A dominant fabric that owns 60% of the MAJ_CAT MBQ should stay capped at 120% because a 300% breach there floods the store.

Current flat cap treats all grains alike and blocks reasonable growth of small contributors.

## 2. Rule

When a global toggle is ON, replace the per-grid `sec_cap_pct` with a per-grain growth% resolved from a cont%-band matrix. `cont%` is the grain's share of the MAJ_CAT's total MBQ at that site:

```
cont_pct(grain) = grain.MBQ_ORIG / SUM(MBQ_ORIG per WERKS × MAJ_CAT) × 100
growth_pct      = matrix_lookup(cont_pct)
ceiling(grain)  = grain.MBQ_ORIG × (growth_pct / 100)
```

Default matrix (editable in UI):

| cont % from | cont % to | growth % |
|---|---|---|
| 0  | 5   | 300 |
| 5  | 10  | 250 |
| 10 | 15  | 200 |
| 15 | 30  | 150 |
| 30 | ∞   | 120 |

Bands are half-open `[lo, hi)`. The final row has `hi = NULL` and matches all cont% ≥ its `lo`.

Toggle OFF → today's behavior (per-grid `sec_cap_pct`) unchanged.

## 3. Data model

### 3.1 New table `ARS_SEC_CAP_GROWTH_MATRIX` (Rep_data)

```sql
CREATE TABLE ARS_SEC_CAP_GROWTH_MATRIX (
    id            INT IDENTITY(1,1) PRIMARY KEY,
    cont_pct_lo   FLOAT NOT NULL,          -- inclusive lower bound (percent, 0-100)
    cont_pct_hi   FLOAT NULL,              -- exclusive upper bound; NULL = open-ended (∞)
    growth_pct    FLOAT NOT NULL,          -- e.g. 300, 250, 200, 150, 120
    seq           INT   NOT NULL,          -- 1..N in ascending band order
    updated_at    DATETIME NOT NULL DEFAULT GETDATE(),
    updated_by    NVARCHAR(100) NULL
);
```

Seeded with the five default rows above at first table creation. `_ensure_growth_matrix_table()` runs at startup like the other grid-builder DDL helpers.

### 3.2 Toggle in `AppSettings`

Single row keyed by `sec_cap.growth_matrix_enabled` (`true` / `false`, default `false`). Uses the existing `AppSettings` table conventions.

### 3.3 Audit into `ARS_RUN_PARAMS_AUDIT`

Every allocation run stamps a JSON payload under key `SEC_CAP_MATRIX`:

```json
{
  "enabled": true,
  "bands": [
    {"lo": 0,  "hi": 5,    "growth": 300},
    {"lo": 5,  "hi": 10,   "growth": 250},
    {"lo": 10, "hi": 15,   "growth": 200},
    {"lo": 15, "hi": 30,   "growth": 150},
    {"lo": 30, "hi": null, "growth": 120}
  ]
}
```

Matches the pattern already in place for `cont_fallback_mode` — historic runs can be traced back to the matrix active at the time.

## 4. Backend

### 4.1 New module `app/services/sec_cap_growth_matrix.py`

```python
from typing import List, Optional, Tuple

Band = Tuple[float, Optional[float], float]   # (lo, hi, growth_pct)

def load_matrix(engine) -> Tuple[bool, List[Band]]:
    """Return (enabled_flag, bands_sorted_by_lo)."""

def resolve_growth(cont_pct: float, bands: List[Band], fallback_pct: float) -> Tuple[float, bool]:
    """Return (growth_pct, matched_band_flag).
    If cont_pct falls in a defined band → (band.growth_pct, True).
    If cont_pct falls in no band (matrix gap) → (fallback_pct, False) with warning.
    """

def snapshot_matrix(enabled: bool, bands: List[Band]) -> dict:
    """Return the JSON dict to stamp into ARS_RUN_PARAMS_AUDIT."""
```

### 4.2 `build_sec_cap_state()` — [rule_engine_per_opt.py:233](../../backend/app/services/rule_engine_per_opt.py#L233)

Add two kwargs and thread them through:

```python
def build_sec_cap_state(
    working_df: pd.DataFrame,
    grid_specs: List[Tuple[str, Dict[str, Any]]],
    matrix_enabled: bool = False,
    matrix_bands: Optional[List[Band]] = None,
) -> Dict[str, Any]:
```

Inside the per-grid loop (replacing the current line 288 `cap_factor = float(...)` and line 334 `ceiling = mbq_val * cap_factor`). The existing `g_meta` carries two keys: `cap_factor` (the numeric multiplier, e.g. 1.30) and `cap_pct` (the percent form used in remarks, e.g. 130.0). Both are already populated by the callers; we only need `cap_factor` as the OFF-mode default.

```python
grid_default_factor = float(g_meta.get("cap_factor") or 1.20)
grid_default_pct    = grid_default_factor * 100.0

# MAJ_CAT total MBQ (grains sum per WERKS, MAJ_CAT). Cheap: groupby on already-aggregated frame.
maj_total = (agg[anchor_col]
             .groupby(level=[0, 1])
             .sum()
             .to_dict())

for grain_idx, row in agg.iterrows():
    grain = grain_idx if isinstance(grain_idx, tuple) else (grain_idx,)
    grain = tuple(str(g) if g is not None else '' for g in grain)

    mbq_raw = row[anchor_col]
    mbq_null = pd.isna(mbq_raw)
    mbq_val = 0.0 if mbq_null else float(mbq_raw)

    # cont% at MAJ_CAT × WERKS grain
    total = float(maj_total.get((grain[0], grain[1]), 0.0))
    cont_pct = (mbq_val / total * 100.0) if total > 0 else 0.0

    # Resolve growth% (matrix or per-grid flat)
    if matrix_enabled and matrix_bands:
        growth_pct, matched = resolve_growth(cont_pct, matrix_bands, grid_default_pct)
        cap_factor_grain = growth_pct / 100.0
    else:
        growth_pct = grid_default_pct
        cap_factor_grain = grid_default_factor
        matched = True

    ceiling = mbq_val * cap_factor_grain
    # ... rest of the current grain-loop body unchanged
    cmap[grain] = ceiling
    cpmap[grain] = cont_pct           # NEW state dict
    gpmap[grain] = growth_pct         # NEW state dict
    gmatch[grain] = matched           # NEW state dict, warn on gaps
```

Add three new state dicts to `state`:
- `state["cont_pcts"][g_name][grain] = cont_pct`
- `state["resolved_growths"][g_name][grain] = growth_pct`
- `state["band_matched"][g_name][grain] = bool`

### 4.3 `_evaluate_sec_cap_per_opt()` — [rule_engine_per_opt.py:387](../../backend/app/services/rule_engine_per_opt.py#L387)

**No logic change.** The block / override decision uses the ceiling produced by 4.2 — the matrix path just changes how that ceiling is computed. Only additions:

- On both `block` and `override` return paths, include `cont_pct`, `growth_pct`, `band_matched` in `breach_info`.
- Callers that build `ALLOC_REMARKS` use these fields to emit
  `SEC_CAP_PRE_<grid>(cap=200%,cont=13%)` instead of the old `cap=120%`.
- When `band_matched=False` and matrix was enabled, append `SEC_CAP_MATRIX_GAP[<grid>]` to `ALLOC_REMARKS` so the operator sees the fallback fired.

### 4.4 Rule-engine wiring

All four engines call `build_sec_cap_state()`. Load the matrix once per run in each caller and pass it in:

- `rule_engine_new.py` — main SQL orchestrator, ~line 159
- `rule_engine_pandas.py` — ~line 598
- `rule_engine_parallel_sql.py` — ~line 361
- `rule_engine_parallel_python.py` — ~line 198

Pattern for each:

```python
from app.services.sec_cap_growth_matrix import load_matrix, snapshot_matrix

matrix_enabled, matrix_bands = load_matrix(engine)
state = build_sec_cap_state(
    working_df, grid_specs,
    matrix_enabled=matrix_enabled,
    matrix_bands=matrix_bands,
)
# stamp snapshot into ARS_RUN_PARAMS_AUDIT under key 'SEC_CAP_MATRIX'
```

`rule_engine_per_opt.py` reuses the state via the existing plumbing — no separate load path.

### 4.5 CRUD endpoints on `grid_builder` router

Add to [backend/app/api/v1/endpoints/grid_builder.py](../../backend/app/api/v1/endpoints/grid_builder.py):

| Method | Path | Body / Response |
|---|---|---|
| GET  | `/grid-builder/growth-matrix` | `{ "enabled": bool, "bands": [ {lo, hi, growth} ] }` |
| PUT  | `/grid-builder/growth-matrix` | Body `{ "enabled": bool, "bands": [...] }` — replaces atomically inside a transaction |

`PUT` validation (400 on failure, no partial write):

1. `bands[i].lo` ≥ 0 and < 100 (except last row where `lo` can be up to any finite value).
2. `bands` sorted ascending by `lo`.
3. Contiguous: `bands[i].hi == bands[i+1].lo` for every i < N-1.
4. Exactly one row has `hi = None` and it must be the last row.
5. Every row has `growth_pct >= 100` (a value below 100 tightens below MBQ, which conflicts with invariant 3 "explicit MBQ=0 means no constraint"; the cap should never *reduce* MBQ).
6. If bands are non-monotonic (growth% doesn't decrease as cont% increases), return a **warning** in the response but still save — some operators may want this deliberately.

## 5. Frontend

### 5.1 New panel on `GridBuilderPage.jsx`

Placement: collapsible section above the grid list, below the page header. Collapsed by default so operators who don't touch sec-cap don't see it.

```
┌─ Sec-Cap Growth Matrix ─────────────────────────── [ Save ] ┐
│ [x] Enabled   (OFF → grid's own sec_cap_pct is used)         │
│                                                              │
│   cont % from   cont % to    growth %              actions   │
│  ┌───────────┬────────────┬───────────┐                      │
│  │ 0         │ 5          │ 300       │  [edit] [delete]     │
│  │ 5         │ 10         │ 250       │  [edit] [delete]     │
│  │ 10        │ 15         │ 200       │  [edit] [delete]     │
│  │ 15        │ 30         │ 150       │  [edit] [delete]     │
│  │ 30        │ (open)     │ 120       │  [edit] [delete]     │
│  └───────────┴────────────┴───────────┘                      │
│                                            [+ Add band]      │
│                                                              │
│  Preview: at cont=13%, cap = 200% × MBQ                      │
└──────────────────────────────────────────────────────────────┘
```

Editing rules mirror the backend validator (contiguity, monotonic warning, growth ≥ 100). Client-side validation prevents the Save button firing when invalid; error messages are shown inline per row.

`Save` calls `PUT /grid-builder/growth-matrix` with the full state; server treats it as a full replacement (no PATCH semantics — keeps the transaction simple).

`Preview` at the bottom is a small helper input: type a cont% (0–100), see which band + growth% resolve. Purely UI; no backend call.

### 5.2 Visibility guard

Toggle is disabled (greyed) if the matrix table has 0 rows. Turning ON with an empty matrix must be blocked with a tooltip: "Add at least one band before enabling."

## 6. Worked example

**Site HJ24, MAJ_CAT `M_K_SHIRT_HS`, grid `MJ_FAB`, 4 grains:**

| Grain | FAB | MBQ_ORIG | STK_TTL |
|---|---|---|---|
| G1 | COTTON | 600 | 400 |
| G2 | POLY   | 300 | 100 |
| G3 | SILK   | 80  | 20  |
| G4 | LINEN  | 20  | 5   |

Total MBQ_ORIG at (HJ24, M_K_SHIRT_HS) = 1000.

**Matrix ON (default bands):**

| Grain | cont% | Band | Growth% | Ceiling = MBQ × factor | Budget = max(0, ceiling − STK) |
|---|---|---|---|---|---|
| COTTON | 60.0% | 30–∞  | 120 | 720 | 320 |
| POLY   | 30.0% | 15–30 | 150 | 450 | 350 |
| SILK   |  8.0% | 5–10  | 250 | 200 | 180 |
| LINEN  |  2.0% | 0–5   | 300 |  60 |  55 |

**Matrix OFF (grid's sec_cap_pct = 120):**

| Grain | Growth% | Ceiling | Budget |
|---|---|---|---|
| COTTON | 120 | 720 | 320 |
| POLY   | 120 | 360 | 260 |
| SILK   | 120 |  96 |  76 |
| LINEN  | 120 |  24 |  19 |

**Interpretation.** Cotton is the dominant fabric — its cap is identical in both modes. Poly, Silk, and Linen all gain material headroom under the matrix. Linen — a 2%-contributor — goes from 19 units of headroom to 55, letting the store actually grow its niche fabrics.

**Downstream effect on `_evaluate_sec_cap_per_opt`:**

- OPT `(HJ24, M_K_SHIRT_HS, 1110114512, BRW)` mapped to LINEN grain with `OPT_MBQ=23`:
  - Matrix OFF: `budget=19`, `run_before=0`, breach fires at 4th unit → OPT routed to override path.
  - Matrix ON:  `budget=55`, whole OPT ships without triggering override.
- `ALLOC_REMARKS` for the ON case reads `SEC_CAP_PRE_MJ_FAB(cap=300%,cont=2%)` on any OPT that does eventually breach.

## 7. Edge cases & fallback

| Case | Behavior |
|---|---|
| Matrix table empty | `load_matrix()` returns `(enabled=False, [])` regardless of toggle. Logs warning `[sec_cap_matrix] table empty, matrix disabled`. |
| Toggle OFF | `matrix_bands` ignored. Every grain uses `g_meta['cap_factor']` = today's behavior, byte-identical. |
| `total_mbq = 0` for a (WERKS, MAJ_CAT) | `cont_pct = 0.0` → falls in the 0-band → highest growth%. But `mbq_val = 0.0` → `ceiling = 0` → grain still hard-blocks via existing `SEC_CAP_MBQ_ZERO` logic. Matrix path can't rescue a MBQ=0 grain. |
| cont% falls in NO band | Fallback to grid's own `sec_cap_pct` and append `SEC_CAP_MATRIX_GAP[<grid>]` to `ALLOC_REMARKS`. The contiguity validator should prevent this in normal use — this is a defence for hand-edited DB rows. |
| `mbq_null = True` (NULL MBQ_ORIG) | Existing hard-block via `SEC_CAP_NULL` fires before matrix lookup runs. Unchanged. |
| Grid has empty extra value (`'NA'`, `''`) | Existing `SEC_CAP_GRID_NULL` hard-block fires. Matrix has no effect. |
| Concurrent matrix edit during a run | `load_matrix()` reads once at run start; the snapshot in `ARS_RUN_PARAMS_AUDIT` locks in what that run used. Subsequent runs pick up the edit. |
| `growth_pct < 100` snuck into DB | Backend rejects on PUT; if it exists (legacy insert), `build_sec_cap_state` clamps to 100 and logs a warning. Never lets ceiling < MBQ. |

## 8. Deployment / rollout

1. Ship code with toggle default **OFF**. Zero production impact.
2. Verify UI editor + validation on staging with the default seed rows.
3. Turn toggle ON in staging, run one full allocation, spot-check `ALLOC_REMARKS` for the new tags and diff sec-cap breach counts vs matrix-OFF baseline.
4. Flip toggle ON in production. Audit table captures the switch.
5. Rollback = flip toggle OFF; no schema rollback needed.

## 9. Coverage matrix

| File | Change |
|---|---|
| `backend/app/services/sec_cap_growth_matrix.py` | NEW module — `load_matrix`, `resolve_growth`, `snapshot_matrix` |
| `backend/app/services/rule_engine_per_opt.py` | `build_sec_cap_state` accepts `matrix_enabled`, `matrix_bands`; adds `cont_pcts`, `resolved_growths`, `band_matched` to state; `_evaluate_sec_cap_per_opt` includes them in `breach_info` |
| `backend/app/api/v1/endpoints/grid_builder.py` | `_ensure_growth_matrix_table` DDL + seed; `GET /growth-matrix`; `PUT /growth-matrix` with validator |
| `backend/app/services/rule_engine_new.py` | Load matrix, pass to state builder, snapshot to audit |
| `backend/app/services/rule_engine_pandas.py` | Same as above |
| `backend/app/services/rule_engine_parallel_sql.py` | Same as above |
| `backend/app/services/rule_engine_parallel_python.py` | Same as above |
| `frontend/src/pages/GridBuilderPage.jsx` | New collapsible panel with toggle + editable band table + preview helper |
| `frontend/src/api/gridBuilder.js` (or equivalent) | `getGrowthMatrix()`, `saveGrowthMatrix()` client wrappers |
| `.claude/agents/ars_flow_kb/grid.md` | Append dated rule for the matrix + toggle semantics |

## 10. Testing

### 10.1 Unit tests
- `resolve_growth(2.0, default_bands, fallback=120)` → `(300, True)`
- `resolve_growth(5.0, default_bands, fallback=120)` → `(250, True)` (edge: `lo` inclusive)
- `resolve_growth(9.999, default_bands, fallback=120)` → `(250, True)` (edge: `hi` exclusive)
- `resolve_growth(30.0, default_bands, fallback=120)` → `(120, True)` (last-band `lo` inclusive)
- `resolve_growth(200.0, default_bands, fallback=120)` → `(120, True)` (open-ended last band)
- `resolve_growth(7.5, bands_with_gap, fallback=120)` → `(120, False)` (gap fallback)

### 10.2 Integration tests
- Full allocation on a fixture with 3 grains, matrix OFF → produces baseline breach counts.
- Same fixture, matrix ON → LINEN-like grain admits more units; COTTON-like grain unchanged.
- Fixture with `total_mbq = 0` grain → grain still hard-blocked regardless of toggle.

### 10.3 Manual UI checks
- Add a band, contiguity broken → Save button greyed, inline error.
- Toggle ON with empty table → tooltip blocks the toggle.
- Save round-trip: values persist, `updated_at` / `updated_by` populated.

## 11. Open questions

None outstanding after brainstorm (2026-07-03 through 2026-07-08). Design fully specified.

## 12. Non-goals

- **Per-grid matrix override.** Explicitly out of scope — matrix is global. If a specific grid needs different bands, revisit after usage data lands.
- **Time-varying bands.** No effective-dated bands; the matrix is single-current. Audit table records what each historic run used.
- **Growth% below 100.** Rejected by validator. The matrix relaxes caps; it never tightens below MBQ.
- **UI-driven cont% definition switch.** cont% is fixed as MBQ share of MAJ_CAT total. Switching to STK-share or sales-share is a separate feature.
