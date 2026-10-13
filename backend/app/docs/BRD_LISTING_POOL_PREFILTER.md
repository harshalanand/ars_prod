# Business Requirements Document (BRD)
## Listing Generate — Pre-filter MSA DISTINCT options by RUN POOL + `FNL_Q > 0`

**Document Version:** 1.0
**Date:** 2026-10-09
**Author:** ARS Engineering
**Parent spec:** `FSD_FRESH_GRT_HOLD_CONTROL.md` (FS-02 — the gate this change hoists forward)
**Target DB:** HOPC866 (SQL Server) → Azure SQL on deploy
**Owner:** `ars_flow`

---

## 1. Problem

`/listing/generate` is spending hours on `[LISTING_TABLE]` scans and attribute merges because Part 2 fans **every distinct `(MAJ_CAT, GEN_ART_NUMBER, CLR)` option on MSA** across **every active store** as `IS_NEW = 1` rows, regardless of whether the option has any **pool stock** to allocate.

Measured on two consecutive production FRESH runs against the same scope (424 stores, all MAJ_CATs):

| Run | `msa_options` distinct | Part 2 rows inserted | Part 3.5a (attribute merge) |
|---|---:|---:|---:|
| `20261008_155816_679` | 21,948 | 7,426,762 | — |
| `20261009_161826_077` | 103,654 | 38,832,290 | **5h 6m hung on CXSYNC_PORT** (SPID 96) |

The 2026-10-09 run **hung for 5+ hours** in the Part 3.5a `UPDATE L SET M_VND_CD, FIT, FAB, MERGE_RNG_SEG, WEAVE_2, M_YARN_02, …` attribute merge against the 38.8 M-row LISTING, blocking 18+ downstream SELECTs. The MSA had grown 5× in distinct options between the two runs — nearly all of the new options carried **zero FRESH-pool stock**, so they could never ship a piece.

## 2. Business Requirement

**BR-LPP-01** — At listing-generation time, the DISTINCT MSA option set used by Part 2 (and reported by the diagnostic) MUST exclude options that:
1. Are typed to the **other** run pool (ALLOC_TYPE ≠ selected `alloc_type`), **and**
2. Carry **zero pool-net stock** (`FNL_Q = 0`).

Semantic justification: an option with no pool stock to allocate is already dropped by the `FNL_Q > 0` pre-filter in `_create_alloc_working` further downstream (FS-02 of `FSD_FRESH_GRT_HOLD_CONTROL`). Fanning it across every store, running the attribute merge on 10× more rows, computing OPT_TYPE / sec-cap / MBQ caps on it, and then dropping it in the allocator is pure waste. This change pulls that gate **earlier** — before the fan-out — with no change in output.

**BR-LPP-02** — The filter MUST be a **semantic no-op** vs. the current behaviour:
- Same final allocation decisions.
- Same `ARS_ALLOC_WORKING` / `ARS_ALLOC_HISTORY` / `ARS_LISTING_WORKING` contents.
- No change to Part 1 (grid-based `IS_NEW = 0` rows) — stores that already carry an article still appear with their on-hand stock even if the pool is empty, matching current reporting.

**BR-LPP-03** — The filter MUST be **backward compatible** with legacy MSA shapes:
- If `req.msa_table` is missing the `ALLOC_TYPE` column (older MSA before typed pools), the pool filter is skipped.
- If `req.msa_table` is missing the `FNL_Q` column (hypothetical), the stock filter is skipped.
- Legacy runs are byte-identical to today.

**BR-LPP-04** — The diagnostic log line MUST announce when the filter is active so operators can distinguish filtered-vs-unfiltered runs at a glance.

## 3. Scope

| In scope | Out of scope |
|---|---|
| `msa_sql` diagnostic DISTINCT (counts) | MSA generation (`msa_service.py`) |
| `msa_with_rdc` Part 2 INSERT DISTINCT | Grid-based Part 1 (unchanged) |
| Diagnostic log message suffix | Downstream Part 3.5a attribute merge SQL (unchanged — just scans fewer rows) |
| Recorded-rule entry in `listing.md` dossier | FS-02's downstream `_create_alloc_working` pre-filter (kept — defence in depth) |

## 4. Measured Impact (live validation 2026-10-09)

Rerun on the exact 424 stores + full MAJ_CAT scope + FRESH pool as the hung session `20261009_161826_077`:

| Stage | Pre-fix | Post-fix | Change |
|---|---:|---:|---:|
| `msa_options` distinct | 103,654 | **9,188** | **−91.1%** |
| Part 1 (Grid INSERT) rows | 6,294,188 | 6,294,188 | 0 |
| Part 1 INSERT time | 22.6s | 19.3s | −15% |
| **Part 2 (MSA missing) rows** | **38,832,290** | **3,112,304** | **−91.9%** |
| **Part 2 INSERT time** | **98.1s** | **9.3s** | **−90.5%** |
| **Part 2.5 (Indexes)** | **61.0s** | **6.4s** | **−89.5%** |

The 94,466 options removed had **`FNL_Q = 0`** in the FRESH pool — every one of them would have been dropped by `_create_alloc_working` later anyway, after being fanned across 424 stores and crawled by Parts 3.5a / 3.55 / 3.6 / 3.7.

## 5. Acceptance Criteria

| ID | Criterion |
|---|---|
| AC-01 | On an MSA with both `ALLOC_TYPE` and `FNL_Q`, the new diagnostic log line reads `Diagnostic: stores=…, grid_rows=…, msa_options=<N> (pool=<FRESH\|GRT>, FNL_Q>0)` |
| AC-02 | On an MSA missing `ALLOC_TYPE`, the suffix is absent and the count equals the pre-fix count (back-compat). |
| AC-03 | Part 2 row count equals `<msa_options> × <stores>` minus Part-1-overlap (NOT EXISTS dedup) — proven on 2026-10-09 rerun (`20261009_232448_185`). |
| AC-04 | `ARS_ALLOC_WORKING` / `ARS_ALLOC_HISTORY` row counts and SHIP/HOLD totals are identical to a pre-fix run with the same inputs (regression) — the dropped options would have shipped 0 regardless (FS-02 `FNL_Q > 0` gate). |
| AC-05 | A GRT run and a FRESH run on the same MSA produce different `msa_options` counts (proving the ALLOC_TYPE filter is in effect when both pools exist in the MSA). |

## 6. Traceability

| BR | FSD section |
|---|---|
| BR-LPP-01 | FS-LPP-01, FS-LPP-02 |
| BR-LPP-02 | FS-LPP-03 (semantic no-op proof) |
| BR-LPP-03 | FS-LPP-01 (column-presence guards) |
| BR-LPP-04 | FS-LPP-02 (log suffix) |
