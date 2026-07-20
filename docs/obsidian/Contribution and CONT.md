---
title: Contribution and CONT
tags: [ars, contribution, cont]
updated: 2026-07-17
---

# Contribution and CONT

Two related-but-distinct things share the name "contribution". Keep them apart.

## 1. Analytical contribution (`Cont_presets` / auto-contrib)
The **upstream KPI** that decides each grouping bucket's *share of shelf/allocation* — `INITIAL AUTO CONT%`. Two implementations, same output shape:
- **Manual / pandas** — `backend/app/api/v1/endpoints/contrib.py`. Reads `COUNT_STOCK_DATA_18M` × `vw_master_product` (SEG APP/GM), computes FIX/DISP_AREA/GM%/STR/SALES-PSF/`STOCK_CONT%`/`SALE_CONT%`/`ALGO` → normalized `INITIAL AUTO CONT%`. Output → `Cont_Percentage_*`.
- **Auto Cont %** — `backend/app/api/v1/endpoints/auto_contrib.py`. Same KPIs but **in SQL** via `sp_AutoContCompute` (needs the proc installed). Output → `AutoCont_FINAL_<gc>_<ts>` (+ `_CO_`). Richer job control (pause/resume). Emits `autocont.completed` for the [[Report Generation Hub]].

**Config tables (shared):** `Cont_presets` (time window: `months`, `avg_days`, `kpi_type` L7D/L30D/L18M), `Cont_mappings` (SSN→preset-suffix rules), `Cont_mapping_assignments` (which mapping feeds which output column). Flow: **presets → mappings/assignments → execute → review** (frontend `Contrib*Page.jsx` / `AutoCont*Page.jsx`).

## 2. Size-level CONT (allocation)
The **`CONT`** consumed by [[Grid Builder|Grid]]/[[Listing]]/[[Rule Engine (per_opt)|allocation]] — the share of each **size** within an option, summing to 1 per OPT:
```
SZ_MBQ  = ROUND(OPT_MBQ × CONT, 0)     ; OPT_MBQ = Σ SZ_MBQ
```
It lives in the grid/alloc tables (from `Master_CONT_<dim>` / `Master_CONT_MERGE_<dim>`), not in `Cont_presets`. PAK_SZ rounding caps at `SZ_MBQ × I_ROD`.

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
