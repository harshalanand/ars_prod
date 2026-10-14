---
title: Contribution and CONT
tags: [ars, contribution, cont]
updated: 2026-10-14
---

# Contribution and CONT

Two related-but-distinct things share the name "contribution". Keep them apart.

## 1. Analytical contribution: REMOVED 2026-10-14
> [!warning] Contribution % and Auto Cont % were removed from the app
> Removed: Contribution % (`/contribution/*`, `contrib.py`, `Cont_presets`, `Cont_mappings`, `Cont_mapping_assignments`, `Cont_jobs`, `Cont_Percentage_*`) and Auto Cont % (`/auto-cont/*`, `auto_contrib.py`, `sp_AutoContCompute`, `AutoCont_*`, event `autocont.completed`). Neither ever fed grid, listing or allocation. The tables are dropped only after reviewing the DROP list from `backend/scripts/040_retire_removed_modules.sql` (Part 3 generates it and does not drop). See [[ARS Work Log]].
>
> The Listing RDC stock-vs-alloc chart (`/listing/contribution`) is unrelated and stays. Only the size-level CONT in section 2 is live.

## 2. Size-level CONT (allocation)
The **`CONT`** consumed by [[Grid Builder|Grid]]/[[Listing]]/[[Rule Engine (per_opt)|allocation]] — the share of each **size** within an option, summing to 1 per OPT:
```
SZ_MBQ  = ROUND(OPT_MBQ × CONT, 0)     ; OPT_MBQ = Σ SZ_MBQ
```
It lives in the grid/alloc tables (from `Master_CONT_<dim>` / `Master_CONT_MERGE_<dim>`). It never came from Contribution % (`Cont_presets`), which is removed. PAK_SZ rounding caps at `SZ_MBQ × I_ROD`.

### CONT fallback ladder (`_stage_b_fill_cont`, all engines delegate here)
Governed by `docs/2026-07-02-cont-fallback-ladder.md`.

**`SZ_APPLICABLE='Y'` (size curve matters):**
- Step 1 — site row `Master_CONT_SZ.ST_CD = WERKS`, joined on `(ST_CD, MAJ_CAT, SZ)` only (MAJ_CAT-level size curve per site).
- Step 2 — corporate default `ST_CD='CO'`, only for `(WERKS,MAJ_CAT)` groups whose site curve is entirely empty.
- **No Step 3** — the old uniform 1/N fallback was removed 2026-07-02; unmatched sizes keep `CONT=0 → SZ_MBQ=0 → no allocation`.

**`SZ_APPLICABLE='N'` (size-agnostic — belts/wallets/one-size):** after Steps 1+2, `cont_fallback_mode` fires:
- `P4_UNIFORM` (default) = `CONT = ROUND(1/COUNT(distinct SZ per OPT), 4)`
- `P3_FNL_Q` = `CONT = FNL_Q / Σ FNL_Q` (skips OPT when `Σ FNL_Q=0`)
- Then renormalize CONT to `Σ=1` per OPT. `SZ_APPLICABLE='Y'` MAJ_CATs are never touched by P3/P4.

## Cross-links
[[Grid Builder]] (pre-computes CONT/MBQ/OPT_CNT/DISP_Q) · [[Merge Rules]] (`Master_CONT_MERGE_*`) · [[Rule Engine (per_opt)]] (fills CONT in Stage B) · [[MSA Stock Calculation]].
