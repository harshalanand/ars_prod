---
title: Fresh-GRT Allocation
tags: [ars, allocation, fresh-grt]
updated: 2026-07-17
---

# Fresh / GRT Allocation

Typed-pool segregation that runs through the whole [[Pipeline Overview|pipeline]]. Implemented 2026-07-09; design confirmed in `backend/app/docs/BRD_FRESH_GRT_HOLD_CONTROL.md` / `FSD_FRESH_GRT_HOLD_CONTROL.md`.

## Design (as built)
FRESH and GRT are two disjoint **stock pools** distinguished by warehouse SLOC (`ARS_MSA_SLOC_SETTINGS.sloc_type ∈ {FRESH, GRT}`, exclusive — no BOTH). Every SLOC is classified; **unknown → FRESH**.

- **One MSA generation, all SLOCs.** [[MSA Stock Calculation|MSA]] Step 12 **expands every SKU into typed rows** at generate time: one `ALLOC_TYPE='FRESH'` row **always** (even all-zero = denominator) plus `'GRT'` rows **per-OPT** when there's a GRT signal at any size. On a typed row, `STK = Σ` that pool's SLOC columns (the other pool's cols zeroed), and PEND/HOLD are re-derived **typed** from the open ledgers (legacy NULL/`''` folds to FRESH). So `Σ STK(FRESH) + Σ STK(GRT) = Σ STK(total)` per SKU.
- **Each allocation run picks one type.** [[Listing]] requires `alloc_type: FRESH|GRT` (422 if missing). [[Rule Engine (per_opt)|Stage B]] joins ONLY the matching typed MSA row and reads its baked `FNL_Q` directly (`NoPoolColumnsError` = E-01 → HTTP 400 if MSA has no rows of that type). Every alloc row is stamped `ALLOC_TYPE`.

> [!note] Design note (evolution)
> Earlier design docs described this as pure *alloc-time* pool selection (recompute `FNL_Q_EFF` from SLOC columns at run time). The **shipped** implementation bakes the typed rows at **MSA generate time** (Step 12) and the engine reads them directly. Same disjoint-pool arithmetic, moved upstream. See [[MSA Stock Calculation]].

## Typed deduction (`ALLOC_TYPE` flow)
`ALLOC_TYPE` flows `ARS_ALLOC_WORKING → ARS_ALLOC_HISTORY → ARS_PEND_ALC / ARS_NL_TBL_HOLD_TRACKING`. A GRT approval reduces only the GRT pool; legacy NULL/`''` rows match every type. The hold tracker PK is widened to 4-part `(WERKS, VAR_ART, SZ, ALLOC_TYPE)`; Step-A hold consumption drains the legacy `''` row first, then the typed row. See [[Pending Allocation and Hold]].

## Migrations & files
- `scripts/015_create_sloc_settings.sql` (adds `sloc_type`, seeds SLOCs — only `V02_GRT`=GRT), `scripts/016_alloc_type_columns.sql` (ALLOC_TYPE on pend/alloc tables; widens hold PK), `scripts/018_alloc_type_integrity.sql` (CHECK constraints), `scripts/019_rename_msa_sloc_settings.sql` (`ARS_SLOC_SETTINGS → ARS_MSA_SLOC_SETTINGS`). `ARS_ASYNC_JOBS` for cross-worker BDC.
- New service `backend/app/services/alloc_pool.py` — SLOC classification map, typed pool/pend/hold SQL helpers.

## Hold suppression (companion feature)
Independent toggles let a run skip building TBL ramp-up hold: `skip_hold_upc` (UPC stores), `apply_hold_seg_app`, `apply_hold_seg_gm`. Suppressed → `OPT_MBQ_WH=OPT_MBQ`, `HOLD_QTY=0`, no tracking row. Mask must zero the rounded hold **before** pak-alignment. Lives in the engines, not the pend/hold ledger.

## Status
Backend compiles, `vite build` clean, migrations live, typed-pool arithmetic cross-checked. Regression baseline: all-FRESH classification + FRESH run = pre-change behavior.

## Cross-links
[[MSA Stock Calculation]] (Step 12 row-per-type) · [[Rule Engine (per_opt)]] (Stage B typed join) · [[Pending Allocation and Hold]] (typed deduction, 4-part hold key) · [[Listing]] (`alloc_type` required).
