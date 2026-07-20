---
title: MSA Stock Calculation
tags: [ars, msa]
updated: 2026-07-17
---

# MSA Stock Calculation

Stage 1 of the [[Pipeline Overview|pipeline]]. MSA (Merchandise Stock Availability) answers: for every warehouse (`RDC`) and article, **how much stock is genuinely free to allocate right now?** Gross stock over-counts — some is promised to open [[Pending Allocation and Hold|pending]], some reserved against a prior hold. MSA deducts both and emits `FNL_Q`. Everything downstream consumes it.

- **Entry:** `POST /msa/calculate` → `msa_service.MSAService.calculate()` (`backend/app/services/msa_service.py:1154`).
- **Stock source:** `VW_ET_MSA_STK_WITH_MASTER` (view over base `ET_MSA_STK`), filtered `SEG IN ('APP','GM')` + selected SLOCs. **Never `ET_STORE_STOCK`.**
- **Outputs (Rep_Data):** `ARS_MSA_TOTAL` (variant×type), `ARS_MSA_VAR_ART` (variant×type, threshold-filtered — rule-engine basis), `ARS_MSA_GEN_ART` (colour rollup — listing input), `MSA_Calculation_Sequence` (one row/run).

## The 12-step algorithm
> The `calculate()` docstring still lists the old **9-step** flow — stale. The dossier `manual/msa.md` and this list are correct.

| # | Step | Rule |
|---|------|------|
| 1 | Filter SLOC | keep only selected SLOCs |
| 2 | Numeric safety | coerce `STK_Q`, NaN→0 |
| 3 | Fill dims | `CLR='A'`, `SZ='A'`, `M_VND_CD=0`, MP dims `'NA'` |
| 4 | SEG filter (+RLS) | keep `SEG IN ('APP','GM')`; apply per-user MAJ_CAT RLS |
| 5 | Pivot by SLOC | `STK_QTY = Σ SLOC cols`; **rename `ST_CD → RDC` immediately**; seed `PEND_QTY=0` |
| 6 | **Universe backfill** | `_load_universe` unions (A) stock in selected SLOCs, (B) open `ARS_PEND_ALC`, (C) open holds (WERKS→RDC). Missing master VAR_ARTs inserted as zero-stock placeholders so **every obligation lands on a row** |
| 6b | ATT_TYP gate | keep only `ATT_TYP ∈ MSA_ALLOWED_ATT_TYP` (default `['00','02']`); drop `01` headers + `11` prepack. Resolved from `vw_master_product`; fail-open |
| 7 | Merge PEND | `ARS_PEND_ALC → PEND_QTY` on `(RDC, ARTICLE_NUMBER)`; warn if match <99% |
| 8 | Merge HOLD | `ARS_NL_TBL_HOLD_TRACKING → HOLD_QTY` (WERKS→RDC) |
| 9 | **FNL_Q** | `FNL_Q = max(STK_QTY − PEND_QTY − HOLD_QTY, 0)` — never negative |
| 10 | Threshold | keep group iff `Σ FNL_Q + Σ PEND + Σ HOLD > threshold` (admits pend-only/hold-only) |
| 11 | Aggregate → GEN_ART | roll VAR_ART → `(RDC, MAJ_CAT, GEN_ART_NUMBER, CLR)` |
| 12 | **Row-per-type expansion** | every SKU → one `ALLOC_TYPE='FRESH'` row **always** (even all-zero = denominator) + `'GRT'` rows **per-OPT** if GRT signal at any size. `STK(type)=Σ` that type's SLOC cols; PEND/HOLD re-derived typed (legacy→FRESH); other type's cols zeroed. Non-fatal: falls back untyped + warning |

Key formulas:
```
STK_QTY = SUM(selected SLOC pivot columns)
FNL_Q   = MAX(STK_QTY − PEND_QTY − HOLD_QTY, 0)
engagement(group) = Σ FNL_Q + Σ PEND + Σ HOLD ; keep iff > threshold
```

## Reconciliation gate (R1–R6, Δ=0 before downstream use)
R1 `Σ TOTAL.PEND == Σ open PEND_ALC` · R2 `Σ TOTAL.HOLD == Σ open HOLD_REM` · R3 `Σ TOTAL.STK == Σ source STK_Q` · R4 per-group VAR_ART counts match · R5 `GEN_ART.X == Σ VAR_ART.X` per typed key · R6 typed-row shape (one FRESH/SKU, ≤one GRT, GRT ladder mirrors FRESH). **Any ≠0 ⇒ fix MSA first.**

## Job / async model
`/calculate` writes the sequence row synchronously then queues a **background storage job** (`msa_job_service.py`) — a single FIFO daemon thread, one job at a time, opens both DB sessions (Claude for job tracking, Rep_Data for storage). On completion emits `msa.completed` for the [[Report Generation Hub]]. HTTP response is a **500-row preview per frame** when auto-storing — never treat it as the full result. `msa_result_storage.py` TRUNCATE-then-bulk-inserts, auto-manages dynamic columns.

## SLOC pools (`ARS_MSA_SLOC_SETTINGS`)
Classifies warehouse SLOCs into exclusive pools `sloc_type ∈ {FRESH, GRT}` + `is_active`. Managed from the MSA page. Read in Step 12 to split typed rows; **unknown SLOC → FRESH**. Changes apply from the **next** generation — never rewrite existing MSA. See [[Fresh-GRT Allocation]].

## Gotchas
- **`MSA_ALLOWED_ATT_TYP`** (`config.py`, default `["00","02"]`) is the only real config knob; `threshold` is a per-run param (default 25).
- **FRESH rows always emitted, even all-zero** — they are the denominator of the listing size-coverage ratio; dropping them over-lists (303k→422k regression).
- **Universe is obligation-anchored, not stock-anchored** — a product with stock only on a non-selected shelf is not pulled in unless it has a real PEND/HOLD.
- **Bootstrap re-syncs** (`bootstrap_msa_pend_sync`/`_hold_sync`) run only on the **synchronous** `/msa/save` path, NOT the async job worker — a subtle source of auto-store vs manual-save tally differences.
- **Mid-listing rebuild hazard** — a manual MSA rebuild is not blocked while a listing run is in progress (see [[Known Risks and Doc Drift]]).

## Cross-links
- [[Grid Builder]] / [[Listing]] consume `FNL_Q` + PEND/HOLD tallies; MP columns (`FAB/MACRO_MVGR/MICRO_MVGR/M_VND_CD/RNG_SEG`) originate in the pivot column set.
- [[Fresh-GRT Allocation]] — Step 12 bakes both pools; each alloc run picks one.
- [[Rule Engine (per_opt)]] reads `ARS_MSA_VAR_ART.FNL_Q` filtered by `ALLOC_TYPE`.
- [[Pending Allocation and Hold]] approve/DO paths delta the matching typed MSA row.
