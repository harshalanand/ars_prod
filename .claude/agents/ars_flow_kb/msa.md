# MSA Stock Calculation — rules & details

## Source files
- `backend/app/services/msa_service.py`
- `backend/app/services/msa_job_service.py`
- `backend/app/services/msa_result_storage.py`
- `backend/app/api/v1/endpoints/msa.py`
- `backend/app/api/v1/endpoints/msa_stock.py`

## Output tables (rep_data DB, NOT system DB)
- `ARS_MSA_TOTAL` (was `cl_msa`) — variant grain: one row per `(RDC, ARTICLE_NUMBER, …, SZ)`
- `ARS_MSA_VAR_ART` (was `cl_generated_color`) — same grain as TOTAL, filtered by threshold (Step 10)
- `ARS_MSA_GEN_ART` — color grain: one row per `(RDC, MAJ_CAT, GEN_ART_NUMBER, CLR)`
- `MSA_Calculation_Sequence` — sequence/run tracker (also in rep_data DB)
- Output column `RDC` (renamed from `ST_CD`)

> ⚠️ Gotcha: `msa_result_storage.py` docstring says "Main DB session, not Data DB" — that is wrong. All four tables above live in the Data DB.

## Three input sources
- `VW_ET_MSA_STK_WITH_MASTER` — stock per `(date, ST_CD, SLOC, ARTICLE_NUMBER, …)`. Step 1 filters by `SLOC IN (selected_slocs)` and `SEG IN ('APP','GM')`.
- `ARS_PEND_ALC` — open pending (`IS_CLOSED=0`, `PEND_QTY>0`). One row per `(RDC, ARTICLE_NUMBER)`.
- `ARS_NL_TBL_HOLD_TRACKING` — open holds (`IS_CLOSED=0`, `HOLD_REM>0`). Mapped WERKS→RDC via `Master_ALC_INPUT_ST_MASTER`, VAR_ART→GEN_ART via `vw_master_product`.
- `vw_master_product` — master catalogue. Slow VIEW; `_load_master_variants` uses temp-table JOIN with retry on transient connect-drop (10054/10053/08S01).

## 11-step MSA flow (universe-anchored, June 2026)

1. filter SLOC (drops rows from the source pivot scope)
2. normalize numerics
3. fill missing dims with defaults
4. SEG IN ('APP','GM')
5. pivot by SLOC → `msa_pivot`; rename ST_CD → RDC
6. **Universe backfill** — `_load_universe(slocs, date)` returns the
   union of (RDC, GEN_ART_NUMBER) keys from:
     A. stock in the **selected SLOCs only**
     B. open `ARS_PEND_ALC` rows
     C. open `ARS_NL_TBL_HOLD_TRACKING` rows
   For each (RDC, GEN_ART) in the universe, every VAR_ART from
   `vw_master_product` is inserted into `msa_pivot` as a zero-stock
   placeholder if missing. Guarantees PEND/HOLD always land on a row.
7. merge `ARS_PEND_ALC` → `PEND_QTY` on (RDC, ARTICLE_NUMBER)
8. merge `ARS_NL_TBL_HOLD_TRACKING` → `HOLD_QTY` (via WERKS → RDC)
9. `FNL_Q = max(STK − PEND − HOLD, 0)`
10. threshold (relaxed): keep groups where
    `sum(FNL_Q) + sum(PEND_QTY) + sum(HOLD_QTY) > threshold`
    — admits pend-only / hold-only groups into VAR_ART / GEN_ART
11. aggregate VAR_ART → GEN_ART by (RDC, MAJ_CAT, GEN_ART_NUMBER, CLR)

### Universe rule (post-correction, June 2026)
Stock contribution stays SLOC-scoped — products with stock only on
non-selected shelves are NOT pulled into MSA. The universe expands
ONLY when a real obligation (PEND or HOLD) is attached to the
(RDC, GEN_ART) pair. Pure cross-shelf stock alone is not enough.

### Reconciliation guarantees
After every MSA Generate:
- `SUM(TOTAL.PEND_QTY) == SUM(ARS_PEND_ALC.PEND_QTY WHERE IS_CLOSED=0)`
- `SUM(TOTAL.HOLD_QTY) == SUM(ARS_NL_TBL_HOLD_TRACKING.HOLD_REM WHERE IS_CLOSED=0)`
- Every per-group `GEN_ART = rollup(VAR_ART)` for STK/PEND/HOLD/FNL_Q

## Sequencing
- Driven by `MSA_Calculation_Sequence` table.
- Each run records: `date_filter`, `slocs` (JSON), `threshold`, row counts per output table, `created_by`, `created_at`, `status`.
- Latest sequence_id is read by `bootstrap_msa_pend_sync` / downstream UPDATE paths to find the rows to refresh.

## Five validation checks (run after every MSA Generate)
1. **TOTAL ↔ PEND_ALC**: `SUM(TOTAL.PEND_QTY) ≈ SUM(ARS_PEND_ALC.PEND_QTY WHERE IS_CLOSED=0)`. Catches PEND rows with no matching MSA variant.
2. **TOTAL ↔ HOLD_TRACKING**: `SUM(TOTAL.HOLD_QTY) ≈ SUM(ARS_NL_TBL_HOLD_TRACKING.HOLD_REM WHERE IS_CLOSED=0)` (with WERKS→RDC mapping). Catches hold rows that don't land.
3. **TOTAL ↔ source view**: `SUM(TOTAL.STK_QTY) ≈ SUM(VW_ET_MSA_STK_WITH_MASTER.STK_Q WHERE SLOC IN selected AND SEG IN ('APP','GM'))` for the run date. Catches a regression in pivot or filter.
4. **VAR_ART ⊂ TOTAL**: per passing `(RDC, MAJ_CAT, GEN_ART_NUMBER, CLR)` group, `COUNT(DISTINCT VAR_ART.ARTICLE_NUMBER) == COUNT(DISTINCT TOTAL.ARTICLE_NUMBER)`. Catches a passing group losing articles (e.g. if anyone adds article-level filtering to Step 10).
5. **GEN_ART = rollup(VAR_ART)**: per `(RDC, MAJ_CAT, GEN_ART_NUMBER, CLR)`, `GEN_ART.X == SUM(VAR_ART.X)` for X ∈ {STK_QTY, PEND_QTY, HOLD_QTY, FNL_Q}. Catches the historical GEN_ART grain bug — RDC-collapse during Step 11 aggregation.

After the universe-anchored build (June 2026), all five should sit at zero deltas. Wire them into a `POST /api/v1/msa/validate` endpoint or run inline at the tail of `store_results()`.

## Historical bug fixes (read git history for the actual code)
- **GEN_ART RDC-grain bug** (fixed via Option A, June 2026): the Step 11 hierarchy lookup was `[c for c in hierarchy_keys if c in msa_gen_clr_var.columns]` BEFORE the `ST_CD → RDC` rename. Result: `"RDC" not in columns`, GEN_ART collapsed across RDCs, `agg("first")` stamped an arbitrary RDC value. Bootstrap then matched on that arbitrary RDC, dropping ~half the per-RDC PEND/HOLD. Fix: do the rename right after the Step 5 pivot, before any downstream step reads column names.
- **Pend-only articles dropped** (fixed via universe anchor, June 2026): before Step 6 was rewritten, only articles already in the SLOC pivot got a row; PEND/HOLD obligations for articles outside the SLOC scope were silently dropped at merge time. The "missing 2" symptom (`SUM(TOTAL.PEND_QTY) = 1,222,865` vs `ARS_PEND_ALC = 1,222,867`) traced to one stationary-set article whose stock sat in V04/V02_GRT/ST_PRD while the run scope was V02_FRESH only. Universe Source B now picks up its GEN_ART via the open PEND row.

## Invariants relevant here
- MBQ sparseness (invariant 3): when computing sec-cap inputs from MSA, MBQ=0 means no constraint.
- ACS_D ≠ daily sale (invariant 5): for velocity inputs use `MAX_DAILY_SALE`, not `ACS_D`.

## Diagnostic recipes
- **Compare sums across the three tables**: `SELECT SUM(STK_QTY), SUM(PEND_QTY), SUM(HOLD_QTY), SUM(FNL_Q) FROM ARS_MSA_TOTAL` (then VAR_ART, GEN_ART). TOTAL.PEND should match `ARS_PEND_ALC` open sum; VAR_ART.PEND < TOTAL.PEND if threshold drops groups; GEN_ART = VAR_ART rollup if RDC grain is intact.
- **Find the missing article**: `(RDC, ARTICLE_NUMBER)` in `ARS_PEND_ALC` open rows but absent from `ARS_MSA_TOTAL` → it's either a master-product gap or (pre-June-2026) a SLOC scope drop.
- **Probe the universe**: `MSAService(session)._load_universe(slocs, date)` returns `(RDC, GEN_ART_NUMBER)` from the three sources. Useful to predict the row-count delta before a fresh Generate.

## Recorded rules
<!-- ars_flow appends dated bullets below. One rule per line. -->
