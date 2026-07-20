---
title: Secondary-Grid Cap
tags: [ars, rule-engine, sec-cap]
updated: 2026-07-17
---

# Secondary-Grid Cap

The grid-level ceiling applied during [[Rule Engine (per_opt)|allocation]] (`_evaluate_sec_cap_per_opt`, `rule_engine_per_opt.py`). Stops any single fabric/vendor/tier from flooding a store beyond a multiple of its grid budget.

## Which grids (live, 2026-07-17)
- **Primary** (count to `PRI_CT%`, NOT sec-capped): `MJ`, `MJ_MERGE_RNG_SEG`.
- **Sec-cap-applicable** (`sec_cap_applicable=1`, all 130%): `MJ_RNG_SEG`, `MJ_FIT`, `MJ_M_YARN_02`.
- Other Secondary grids (`MJ_FAB`, `MJ_CLR`, `MJ_M_VND_CD`, `MJ_WEAVE_2`) exist but are **not currently cap-applicable**. `MJ_MACRO_MVGR`/`MJ_MICRO_MVGR` are Inactive.

> [!note] The old KB used `MJ_FAB`/`MJ_MICRO_MVGR` as canonical sec-cap examples — they are NOT cap-applicable right now. Use `MJ_RNG_SEG`/`MJ_FIT`/`MJ_M_YARN_02`. And note [[ARS Glossary#RNG_SEG|RNG_SEG]] is a price tier (E/V/P/SP).

## Breach / overshoot math
```
ceiling = MBQ_ORIG × sec_cap_pct%          # per-grid, default 130
budget  = max(0, ceiling − STK_TTL)
breach  ⇒ run_before + intended_ship > budget
overshoot = run_before + intended_ship − budget
admit (override) when: configured AND overshoot > 0 AND overshoot ≤ 0.5 × intended_ship
```
Cap arithmetic is **MBQ-only** — a TBL row's WH hold-buffer (`want_hold_pak`) is excluded from `intended_ship`.

## Sparse MBQ — 0 means "no constraint" (with an exception)
Per-grid MBQ columns are populated for only a subset of grains (~58% of CLR grains have `CLR_MBQ=0`). In the **breach path**, `budget=0`/`MBQ=0` means *no constraint* — skip (invariant 3). **But** the **veto path** hard-blocks an *applicable* grid with explicit `MBQ_ORIG=0` (`SEC_CAP_MBQ_ZERO`). This tension is a deliberate 2026-06-30 design change — see [[Known Risks and Doc Drift]].

## Two-pass veto precedence (since 2026-07-13)
`_evaluate_sec_cap_per_opt` is two-pass — **invariant: veto > override**:
1. **Pass 1** scans **every** applicable grid for hard-block reasons (`SEC_CAP_GRID_NULL` empty/'NA' extra, `SEC_CAP_MBQ_ZERO` explicit 0, `SEC_CAP_NULL` NULL MBQ_ORIG) — blocks the OPT if ANY grid vetoes.
2. **Pass 2** runs Primary-first breach/overshoot only when Pass 1 finds no veto.

The `GH_<grid>` applicability guard is re-applied in Pass 1 — a grid with `GH=0` for the MAJ_CAT is skipped and **cannot veto**. Vetoed OPTs leave `participating` empty so `running` never advances.

**Why:** the old single-pass loop returned on the first grid to decide, admitting an OPT that breached a Primary grid by an overshoot-eligible margin before reaching a downstream grid that should have hard-blocked it. Tests: `backend/tests/test_evaluate_sec_cap_per_opt_precedence.py`.

## Extras must propagate (listing → listed → alloc)
The sec-cap dimension columns must survive into `ARS_ALLOC_WORKING` or the grid silently drops. Populated in [[Listing]] Part 4 pre-resolve; carried through Stage A/B via `_collect_grid_extra_cols` (derived at runtime from `ARS_GRID_BUILDER.hierarchy_columns`, so **adding a grid propagates automatically**). Live dimension set: `RNG_SEG, MERGE_RNG_SEG, FIT, M_YARN_02, WEAVE_2, FAB, CLR, M_VND_CD` (broader than the KB's old 5-col list).

## Sec-cap growth matrix (optional, off by default)
`ARS_SEC_CAP_GROWTH_MATRIX` (+ `_CFG`): when enabled, per-grain `cont% = MBQ_grain/MBQ_MAJ_total × 100` maps to a banded `growth%` (default 0-5%→300, 5-10%→250, 10-15%→200, 15-30%→150, 30%+→120) replacing the flat per-grid cap. Bands half-open, growth clamped ≥100 (relax-only). No band → `SEC_CAP_MATRIX_GAP`, falls back to flat cap. (`sec_cap_growth_matrix.py`.)

## Cross-links
[[Rule Engine (per_opt)]] · [[Grid Builder]] · [[Known Risks and Doc Drift]].
