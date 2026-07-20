# Functional Specification Document (FSD)
## ARS V2 Retail — Fresh/GRT Allocation Selector & Selective Hold Control

**Document Version:** 1.0 (Draft)
**Date:** 2026-07-08
**Author:** ARS Engineering
**Parent Documents:** `BRD_FRESH_GRT_HOLD_CONTROL.md` (business intent), `BRD_ARS_V2.md` (system baseline)
**Target DB:** local SQL Server `HOPC866` → Azure SQL on deploy
**Engines:** `rule_engine_per_opt.py` (production default), `rule_engine_pandas.py`; shared Stage A/B in `rule_engine_new.py`

---

## 1. System Context

```
VW_ET_MSA_STK_WITH_MASTER (et_msa_stk)          [unchanged]
        │  msa_service.py — 9 steps, run ONCE, all SLOCs
        ▼
ARS_MSA_TOTAL / GEN_ART / VAR_ART                [unchanged — per-SLOC pivot columns preserved]
        │
        │  POST /listing/generate  { alloc_type*, skip_hold_upc, apply_hold_seg_app, apply_hold_seg_gm, … }
        ▼
Stage A/B (rule_engine_new.py)                   [FS-02: pool override; FS-04: ST_STATUS/SEG merges]
        ▼
ARS_ALLOC_WORKING                                [FS-07: ALLOC_TYPE stamp]
        │
        ▼
Waterfall — per_opt / pandas                     [FS-05/FS-06: hold suppression]
        │  approve
        ▼
ARS_NL_TBL_HOLD_TRACKING + MSA re-sync           [unchanged — tolerates HOLD_QTY = 0]
```

Design principle: MSA generation is untouched; all new behaviour activates at allocation time from the per-SLOC columns the MSA already emits.

---

## 2. Data Model Changes

### FS-DB-01 — `ARS_MSA_SLOC_SETTINGS` (create; table not yet deployed)

File: `backend/scripts/015_create_sloc_settings.sql` (rewrite in place — never executed on HOPC866).

```sql
CREATE TABLE ARS_MSA_SLOC_SETTINGS (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    sloc        NVARCHAR(50)  NOT NULL UNIQUE,
    kpi         NVARCHAR(200) NULL,
    sloc_type   NVARCHAR(10)  NOT NULL DEFAULT 'FRESH'
                CONSTRAINT CK_ARS_MSA_SLOC_SETTINGS_type
                CHECK (sloc_type IN ('FRESH','GRT')),
    is_active   BIT           NOT NULL DEFAULT 1,
    created_at  DATETIME      NOT NULL DEFAULT GETDATE(),
    updated_at  DATETIME      NOT NULL DEFAULT GETDATE()
);
```

Seeding: insert the 29 live SLOCs from `SELECT DISTINCT SLOC FROM VW_ET_MSA_STK_WITH_MASTER`. Initial classification: `V02_GRT → 'GRT'`, all others `'FRESH'` (pools are mutually exclusive — there is no shared/`BOTH` classification; GRT bins are the explicit exception the business maintains). Header comment must cite `VW_ET_MSA_STK_WITH_MASTER` as the source (correcting the stale `ET_STORE_STOCK` reference).

### FS-DB-02 — `ARS_ALLOC_WORKING.ALLOC_TYPE`

`ALLOC_TYPE NVARCHAR(10) NULL`. Verify presence via `INFORMATION_SCHEMA.COLUMNS` at implementation; if absent, add in the Stage B table build (`rule_engine_new._stage_b_explode`). Written per FS-07. Historical rows remain NULL.

### FS-DB-03 — `ALLOC_TYPE` on the pend/hold ledgers (pool-aware deduction)

| Table | Change | Populated by |
|---|---|---|
| `ARS_PEND_ALC` | `ALLOC_TYPE NVARCHAR(10) NULL` | `write_pend_alc` INSERT (`pend_alc_service.py:2282-2315`) — select `H.ALLOC_TYPE` from `ARS_ALLOC_HISTORY` (one run = one type; no grain conflict). |
| `ARS_NL_TBL_HOLD_TRACKING` | `ALLOC_TYPE NVARCHAR(10) NULL` — and the row **key widens** from (WERKS, VAR_ART, SZ) to (WERKS, VAR_ART, SZ, ALLOC_TYPE) so a FRESH and a GRT hold can coexist. Full ripple list in FS-10. | Step B source rows (from history). |
| `ARS_ALLOC_HISTORY` | `ALLOC_TYPE NVARCHAR(10) NULL` (verify whether the promote auto-reconcile at `parked_history.py:427-465` adds it; if not, ALTER) | Promotion from stamped `ARS_ALLOC_WORKING` (FS-07). |

Legacy rows keep `ALLOC_TYPE = NULL` → treated as deduct-from-**both**-pools (conservative); they drain out as DOs confirm / holds close.

**Deliberately untyped:** MSA `PEND_QTY`/`HOLD_QTY`/`FNL_Q` (stay total-based — see FS-02), grid rollups `ARS_GRID_MJ*.PEND_ALC/STK_TTL` (store-side budget capacity is pool-agnostic), snapshot/revert tables (mirror tracking schema automatically).

---

## 3. FS-01 — SLOC Classification Semantics

| Rule | Behaviour |
|---|---|
| Classified SLOC | Pool membership per `sloc_type` — exactly one pool per SLOC (`FRESH` xor `GRT`); the pools are disjoint by construction. |
| Unclassified SLOC (present on MSA, absent from settings) | **Ignored** (not counted in any pool) with an E-03 warning naming the SLOC — on the pivoted MSA table an unknown column cannot be distinguished from a metric column, so `ARS_MSA_SLOC_SETTINGS` is the sole authority. Classify the SLOC to include it. |
| `is_active = 0` | Excluded from pool computation entirely. |
| Maintenance | Direct table edit (settings UI is a future enhancement). |

---

## 4. FS-02 — Pool Override at Stage A/B

**Location:** `rule_engine_new.py`, immediately after each MSA read: Stage A (`req.msa_table`, default `ARS_MSA_GEN_ART` — drives listing/REQ decisions) and Stage B (`ARS_MSA_VAR_ART` — drives the size-grain pool). One shared helper, called at both points, covering both engines.

```python
def apply_alloc_type_pool(df, alloc_type, engine):
    type_map  = read_sloc_type_map(engine)              # {sloc: FRESH|GRT}, is_active=1
    sloc_cols = [c for c in df.columns if c in type_map or is_sloc_column(c)]
    pool_cols = [c for c in sloc_cols
                 if type_map.get(c, 'FRESH') == alloc_type]   # unknown SLOC → FRESH (+ warn)
    if not pool_cols:
        raise NoPoolColumnsError(alloc_type)            # → HTTP 400, see §8

    # Typed deduction (live aggregates at RDC × ARTICLE grain):
    #   PEND_T = Σ open ARS_PEND_ALC.PEND_QTY  WHERE ALLOC_TYPE = :t OR ALLOC_TYPE IS NULL
    #   HOLD_T = Σ open tracking HOLD_REM       WHERE ALLOC_TYPE = :t OR ALLOC_TYPE IS NULL
    typed = read_typed_pend_hold(engine, alloc_type)    # merge on (RDC, ARTICLE_NUMBER)
    df = df.merge(typed, on=['RDC', 'ARTICLE_NUMBER'], how='left').fillna({'PEND_T':0,'HOLD_T':0})

    pool_stk = df[pool_cols].sum(axis=1)
    df['FNL_Q'] = np.minimum(pool_stk - df['PEND_T'] - df['HOLD_T'],
                             df['FNL_Q']).clip(lower=0)   # FNL_Q (total) is the safety cap
    return df
```

Pool formula (normative):

```
pool(T) = max( min( Σ SLOC-cols(T) − PEND_T − HOLD_T ,  FNL_Q_total ), 0 )
```

Rules:
- **Typed deduction (BR-04):** a GRT approval's pend/hold reduce only the GRT pool; FRESH symmetric. Legacy NULL-typed rows deduct from both pools.
- **Disjoint pools (BR-01/BR-16):** every SLOC belongs to exactly one pool, so the pools cannot overlap by construction. The `min(…, FNL_Q_total)` cap is retained as a safety net against mis-attributed deductions (untyped SAP DO FIFO, IM-9) — aggregate allocation can never exceed physical availability. It also keeps the existing `FNL_Q > 0` pre-filter in `_create_alloc_working` (`listing_allocator.py:167-179`) correct: total 0 ⇒ every pool 0.
- SLOC columns are **dynamic per MSA run** — classify what is actually on `df.columns`; never hard-code names.
- `STK_QTY` is overridden the same way wherever Stage A REQ logic reads it, so listing decisions and the allocation pool see the same stock.
- Store-side stock (`SZ_STK`) is untouched — it is store inventory, not warehouse pool.
- The typed live reads must use the same sources MSA generation bakes from (`ARS_PEND_ALC` open rows; tracking open rows) to avoid drift.

---

## 5. FS-03 — API Contract (`listing.py`)

### `GenerateRequest` additions (snake_case; no default for `alloc_type`)

```python
alloc_type: Literal["FRESH", "GRT"]          # required — 422 if missing
skip_hold_upc: bool = False
apply_hold_seg_app: bool = True
apply_hold_seg_gm: bool = True
```

### Threading

Session config (same pattern as `rl_mbq_cap_pct`) → engine kwargs: `run_listing_and_allocation_pandas` (signature at `rule_engine_pandas.py:498`) and the per_opt equivalent → `pool_args` (`:774`) → `_run_majcat_waterfall`. `alloc_type` additionally reaches Stage A/B (FS-02).

### Validation in `/generate`

| Condition | Response |
|---|---|
| `alloc_type` missing/invalid | 422 (Pydantic). |
| MSA has zero SLOC columns of requested type | 400 — see §8 message E-01. |

---

## 6. FS-04 — `ST_STATUS` and `SEG` Propagation

Neither column reaches the allocation frame today (verified). Two additions:

1. **`ST_STATUS`** — extend `_stores_sql` (`listing.py:963-969`) to `SELECT ST_CD, {rdc}, ST_STATUS FROM Master_ALC_INPUT_ST_MASTER`; carry through the WERKS pool (`listing.py:1055/1081`). Engines LEFT-merge onto the frame on `WERKS = ST_CD` after the `ARS_ALLOC_WORKING` load (pandas: after `read_sql` at `:1312`, before coercion at `:1322`; per_opt: equivalent load point). Missing status → treat as not-UPC (never suppress by accident).
2. **`SEG`** — LEFT-merge a `SELECT DISTINCT MAJ_CAT, SEG` lookup (from `VW_ET_MSA_STK_WITH_MASTER`) on `MAJ_CAT`. **Do not** use MSA's `SEG` column (pre-filtered to APP/GM at MSA Step 4 and not projected by Stage B). Missing SEG → row never suppressed by a SEG toggle.

Engine-side merges are deliberately self-contained — Stage B's column set and the sec-cap grid-extras propagation are untouched.

---

## 7. FS-05 / FS-06 — Hold Suppression (two layers, both engines)

### Suppression predicate (single definition, used by both layers)

```
suppress(row) =  (skip_hold_upc        AND row.ST_STATUS == 'UPC')
              OR (NOT apply_hold_seg_app AND row.SEG == 'APP')
              OR (NOT apply_hold_seg_gm  AND row.SEG == 'GM')
```

### FS-05 — Source layer (listing, `OPT_MBQ_WH` build)

Where `hold_days` uplifts `ALC_D` to build `OPT_MBQ_WH` for `OPT_TYPE='TBL'`: for suppressed rows set `OPT_MBQ_WH = OPT_MBQ`. The TBL option is thereby budgeted like RL/TBC from the start (BR-10).

### FS-06 — Waterfall layer (defence in depth)

**pandas** — TBL SHIP/HOLD split at `rule_engine_pandas.py:2180-2218`:

```python
round_ship = np.minimum(take, n_ship)                       # unchanged
round_hold = np.maximum(take - n_ship, 0.0)                 # unchanged
round_hold = np.where(suppress_mask, 0.0, round_hold)       # NEW
# pool_take = round_ship + round_hold  → un-held qty stays in pool (BR-11)
```

**per_opt** — mirror the identical mask at its TBL SHIP/HOLD split (locate at implementation; commit 58ef3dc touched this area — "pak-align TBL SHIP and HOLD independently").

Constraints:
- Ship-side math is **not modified**. The RL/TBC-style cap already lives in `need_ship = max(r × SZ_MBQ − SZ_STK − SHIP_QTY, 0)` at SZ grain. Do **not** cap ship by `OPT_MBQ`/`OPT_MBQ_WH` per row — those are OPT-level roll-ups; a per-row cap over-ships (verified grain mismatch).
- The existing `is_ship_met` gate (pandas `:2202-2206`) is preserved for non-suppressed rows.
- Downstream safety (verified): latest production batch has 35,936 TBL rows with `HOLD_QTY = 0` — the `ARS_NL_TBL_HOLD_TRACKING` MERGE and `parked_history._apply_hold_tracking_from_history` (`parked_history.py:1449`) already skip zero-hold rows.

---

## 8. FS-07 — `ALLOC_TYPE` Stamping

Write `req.alloc_type` on every `ARS_ALLOC_WORKING` row (Stage B build, or `listing_allocator.py:372-390` if that write path is live — replace any hardcoded `'PRIMARY'`). Value flows to `ARS_ALLOC_HISTORY` with the existing row copy. No SAP interface change.

## 9. FS-08 — Frontend (`ListingPage.jsx` only)

| Element | Spec |
|---|---|
| State (~`:756`) | `allocType` = `null` (forces choice) · `skipHoldUPC` = `false` · `applyHoldSegApp` = `true` · `applyHoldSegGm` = `true` |
| Alloc-type radio | Required. Options **Fresh** / **GRT**, no preselection. Placed at top of "Caps & Growth" ParamGroup (~`:2524`). |
| Hold toggles | Three `ToggleRow`s: "Skip hold — UPC stores", "Apply hold — APP", "Apply hold — GM", with hint text naming the affected population. |
| Generate guard | `handleGenerate` (`:1123`) returns early with error toast "Select allocation type (Fresh or GRT)" when `allocType` is null; no network call. |
| Payload (`:1123-1171`) | `alloc_type`, `skip_hold_upc`, `apply_hold_seg_app`, `apply_hold_seg_gm` — snake_case (no bridge layer). |
| MSA page | **No change.** |

---

## 10. Error Handling

| ID | Condition | HTTP | Message |
|---|---|---|---|
| E-01 | No SLOC columns of requested type on MSA | 400 | `MSA contains no {alloc_type} SLOC columns. Regenerate MSA including {alloc_type} SLOCs (see ARS_MSA_SLOC_SETTINGS).` |
| E-02 | `alloc_type` missing/invalid | 422 | Pydantic standard. |
| E-03 | Unclassified SLOC column encountered | — | Log warning `SLOC '{name}' not in ARS_MSA_SLOC_SETTINGS — ignored (classify it to include it in a pool)`; continue. |
| E-04 | `ST_STATUS`/`SEG` merge leaves NULLs | — | Row is never suppressed on a NULL attribute; count logged per run. |

---

## 11. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NF-1 | Pool override adds one settings read + one vectorised column sum per MSA read — no measurable impact on the ≤2h run window. |
| NF-2 | Engine parity: per_opt and pandas produce identical SHIP/HOLD for identical inputs and flags (acceptance TC-10). |
| NF-3 | Backward compatibility: default flags + all-`FRESH` classification + FRESH run ⇒ byte-identical output vs. pre-change baseline (release gate). |
| NF-4 | All new logs carry `session_id` for auditability. |

---

## 12. Acceptance Test Cases (HOPC866, project SQLAlchemy engine)

| ID | Test | Expected |
|---|---|---|
| TC-01 | Run migration 015 | Table exists; 29 rows; `V02_GRT=GRT`, 28 × `FRESH`; CHECK constraint rejects any other value. |
| TC-02 | Generate MSA (all SLOCs) | MSA tables carry SLOC pivot columns; `FNL_Q` unchanged vs. pre-change MSA. |
| TC-03 | FRESH run, sample 5 articles | Engine pool = `max(min(Σ FRESH cols − PEND_F − HOLD_F, FNL_Q), 0)` per SQL spot-check. |
| TC-04 | GRT run, same MSA, same articles | Pool = GRT arithmetic (disjoint from FRESH); no MSA regeneration needed. |
| TC-05 | GRT request vs. fresh-only MSA | 400 with E-01 message. |
| TC-06 | `skip_hold_upc=true`, scope with UPC stores | UPC TBL rows: `HOLD_QTY=0`, `OPT_MBQ_WH=OPT_MBQ`; non-UPC rows hold normally. |
| TC-07 | `apply_hold_seg_gm=false` | GM TBL rows `HOLD_QTY=0`; APP rows unchanged. Symmetric on flip. |
| TC-08 | Approve a run containing suppressed rows | Tracking rows only for `HOLD_QTY>0`; MSA re-sync consistent. |
| TC-09 | Regression: defaults + all-`FRESH` + FRESH run | SHIP/HOLD totals identical to pre-change baseline for same scope. |
| TC-10 | Engine parity: TC-06 scope on per_opt AND pandas | Identical SHIP/HOLD per row. |
| TC-11 | UI: Generate without alloc_type | Toast, no network request (verify via devtools). |
| TC-12 | `ARS_ALLOC_WORKING.ALLOC_TYPE` | Equals the run's alloc_type on every row, both run types. |
| TC-13 | Approve a GRT run; regenerate nothing; run FRESH on same MSA | New `ARS_PEND_ALC` rows carry `ALLOC_TYPE='GRT'`; FRESH pool per FS-02 is **unchanged** by the GRT approval (typed deduction); GRT pool shrinks by the approved qty. |
| TC-14 | Approve a FRESH run creating TBL holds; then a GRT run on articles with those holds | Tracking rows carry `ALLOC_TYPE='FRESH'`; the GRT run's engine hold load excludes them (draws 0 from FRESH holds); a subsequent FRESH run can draw them. |
| TC-15 | Unclassified SLOC + aggregate cap | MSA containing a SLOC column absent from settings → treated FRESH with E-03 warning; and for any article, FRESH pool + GRT pool − legacy double-count ≤ physical stock; no pool ever exceeds `FNL_Q`. |
| TC-16 | Legacy NULL rows | Pre-change pend/hold rows (NULL type) reduce both pools; Step A consumes NULL holds before typed ones. |
| TC-17 | Grid rollups on approve | `ARS_GRID_MJ*.PEND_ALC/STK_TTL` deltas identical for FRESH and GRT approvals of equal qty (pool-agnostic). |
| TC-18 | Revert round-trip | Approve a GRT run (pend + typed holds created) → revert via Operations Log → `ARS_PEND_ALC` rows gone, tracking restored byte-identical **including `ALLOC_TYPE`**, MSA/grid deltas reversed, session back in Parked Runs; re-approve produces identical state. |
| TC-19 | Manual pend entry | Upload via Manual Entry → row has `SOURCE='MANUAL'`, `ALLOC_TYPE=NULL`; next FRESH **and** GRT runs both show the pool reduced by the manual qty. |
| TC-20 | Hold clear on typed rows | Article with a FRESH hold and a legacy NULL hold on the same (WERKS, VAR_ART, SZ): clear less than total → NULL row drains first; MSA `HOLD_QTY` re-sync equals remaining sum. |
| TC-21 | DO close on typed pend | DO upload covering a GRT pend row → `DO_QTY` rises, row closes, next GRT run's pool grows by the closed qty (live aggregate), FRESH pool unchanged. |

---

## 13. Traceability Matrix

| BRD | FSD |
|---|---|
| BR-01 | FS-DB-01, FS-01 |
| BR-02 | §1 (MSA unchanged), FS-02 |
| BR-03 | FS-03, FS-08 |
| BR-04 | FS-02 |
| BR-05 | FS-02, E-01 |
| BR-06 | FS-02 (existing re-sync) |
| BR-07 | FS-DB-02, FS-07 |
| BR-08 | FS-03, FS-08 |
| BR-09 | §7 predicate |
| BR-10 | FS-05, FS-06 |
| BR-11 | FS-06 (`pool_take` accounting) |
| BR-12 | FS-06 (verified tracking tolerance) |
| BR-13 | NF-3, TC-09 |
| BR-14 | FS-DB-03, FS-09 (group 2) |
| BR-15 | FS-DB-03, FS-09 (group 4 + typed consumption) |
| BR-16 | FS-02 safety cap, TC-15 |
| BR-17 | FS-09 (group 5), TC-17 |
| BR-18 | FS-11 (manual pend), TC-19 |
| BR-19 | FS-10 (items 4-7), FS-11 (revert), TC-18 |

---

## 13b. Implementation Status (2026-07-09)

**Implemented & statically verified** (compile + `vite build` clean, migrations live on HOPC866, typed-pool math cross-checked on real articles):
- Migrations 015 (SLOC_TYPE, 29 seeded, only `V02_GRT`=GRT) + 016 (ALLOC_TYPE on pend/history/working; hold-tracking + snapshot PK widened) — applied & confirmed via INFORMATION_SCHEMA.
- FS-01/02: `alloc_pool.py` helper + Stage A/B typed override; TC-01/03/05 pass on live data (FRESH pool = `V02_FRESH`; E-01 raised for GRT on the current fresh-only MSA; `FNL_Q_EFF` = `max(min(pool−pend−hold, total),0)` matches on sampled articles).
- FS-03/04/05: GenerateRequest fields, E-01/IM-3 guards, ST_STATUS+SEG merges, `OPT_MBQ_WH=OPT_MBQ` suppression source-fix.
- FS-06: suppression mask in pandas + per_opt, placed before hold pak-align (IM-1); typed hold reads (legacy `''` served to both pools).
- FS-07: ALLOC_TYPE stamped on `ARS_ALLOC_WORKING`; `/retry-failed` recovers pool+toggles.
- FS-09/10/11: typed approve chain — Step A two-pass (legacy-first), Step B typed MERGE, snapshot/restore 4-col, hold clear/revise ordered drain — behaviorally tested live by the implementing pass.
- FS-12: DO deduction_method (FIFO/SESSION_FIRST/SESSION_ONLY); SESSION_FIRST window SQL runs on HOPC866.
- FS-13: `ARS_ASYNC_JOBS` created; cross-worker write-through; Reco+DO poll error tolerance; stamp wildcard store-scoped fix; blocked-by-open-BDC count in failure message.
- UI: separate "Run Pool (Fresh/GRT)" + "Hold Control" blocks; DO deduction select + session picker — present in the production bundle.

**Requires a staging run** (needs the backend server + a seeded MSA/grid/listing scenario — not fabricated here): TC-06/07 (full allocation with suppression), TC-09 (regression parity), TC-13/14/18 (typed deduction/holds/revert through a real approve), TC-11 (UI guard click-through), TC-25 (multi-worker BDC).

## 14. Implementation Order

1. FS-DB-01 + FS-DB-03 migrations → 2. FS-02 pool override (typed) → 3. FS-03 API → 4. FS-04 merges → 5. FS-05/06 hold gate (per_opt first) → 6. FS-07 stamp → 7. FS-09 approve-chain typing + typed hold consumption → 8. FS-08 UI → 9. §12 acceptance suite.

Each step is independently verifiable before the next depends on it. Note: FS-09 depends on FS-07 (history rows must carry `ALLOC_TYPE` before `write_pend_alc` / Step B can read it).

---

## 15. Impact on Existing Allocation Rules

Unaffected by design (ship-side math untouched; verified against code and production data):
MJ_REQ sequential gate · SZ_MBQ × I_ROD ship caps · sec-cap grid budgets and their propagation chain · MBQ caps / growth % · `is_ship_met` gate · hold-tracking approve flow (production already contains 35,936 TBL rows with `HOLD_QTY = 0`).

Interaction points:

| ID | Interaction | Disposition |
|---|---|---|
| IM-1 | **Pak-align ordering (per_opt).** TBL SHIP/HOLD are pak-aligned independently (commit 58ef3dc). The suppression mask MUST zero `round_hold` **before** pak-alignment, else rounding could re-inflate a zeroed hold. | Hard implementation constraint for FS-06. |
| IM-2 | **`cont_fallback_mode = P3_FNL_Q`.** Fallback contributions derive from `FNL_Q`, which the pool override rewrites — P3 proportions become pool-specific. | Intended consequence; note in release comms. |
| IM-3 | **RESOLVED 2026-07-10.** per_opt is the only engine (see `REMOVAL_PLAN_PER_OPT_ONLY.md`); `allocation_mode != 'per_opt'` → HTTP 400. The original sequential/parallel-mode concern no longer applies. | Hard guard in `/generate`. |
| IM-4 | **Multiple unapproved runs.** Pool honesty across sequential Fresh/GRT runs relies on approve → MSA re-sync. | Existing `allow_multi_parked = False` default already blocks a second parked run; keep it. |
| IM-5 | **`is_active = 0` SLOCs become live.** Deactivating a SLOC now removes its stock from every pool. | Intended feature; document for table maintainers. |
| IM-6 | **`FROM_HOLD_QTY` draws shrink** for suppressed populations (nothing parked → nothing to draw). | Intended; hold-dashboard totals trend down for UPC/suppressed SEG. |
| IM-7 | **One-size allocation path** (`ARS_ONESIZE_ALLOCATION` carries `STK_QTY`). | Verify at implementation that its stock read passes through the FS-02 override; add to TC list if it reads MSA independently. |
| IM-8 | **Hold-dashboard mutations** (`clear-hold` / `revise-hold`) operate on (WERKS, ARTICLE) without a type filter; with typed tracking rows they act across both types of a key. | Deterministic drain order: NULL first, then oldest `LISTED_DATE` (FS-10). Type filter is a candidate enhancement. |
| IM-9 | **DO FIFO cross-type attribution.** `apply_do_deductions` partitions FIFO by (RDC, ST_CD, ARTICLE) ignoring type; a DO for a GRT shipment may close a FRESH pend row when both are open for the same key. SAP DO files carry no type, so this is not fully fixable. | Accepted: mis-attribution is temporary and reconciles at the next MSA generation (full re-bake). Monitor via reco report. |
| IM-10 | **Generic Upload page** (`upload.py:36/:136`) can upsert pend/hold/MSA tables with no MSA sync, no ops-log, no `ALLOC_TYPE` discipline. Pre-existing gap, more visible with typed pools. | Governance flag to the owner; out of scope for this change. |

---

## 16. FS-09 — Pool-Aware Approve Chain & Typed Hold Consumption

Approve (`parked_history.approve_parked`, `parked_history.py:551`) performs seven write groups. Disposition per group:

| # | Write group (file:line) | Change |
|---|---|---|
| 1 | Promote parked → 4 history tables (`:427-470`, targets `:59-84`) | None beyond FS-DB-03 (`ALLOC_TYPE` present on `ARS_ALLOC_HISTORY`). |
| 2 | `write_pend_alc` → `ARS_PEND_ALC` (`pend_alc_service.py:2245`, INSERT `:2282-2315`) | Select `H.ALLOC_TYPE` into the new column. |
| 3 | Hold snapshot tables (`:683-713`) | None (schema mirrors tracking). |
| 4 | Hold tracking Step A/B (`:1397-1498`) | **Step B**: `ALLOC_TYPE` joins the MERGE key (FS-DB-03). **Step A**: decrement joins add type matching — `FROM_HOLD_QTY` rows from a run of type T decrement holds `WHERE ALLOC_TYPE = T OR ALLOC_TYPE IS NULL` (NULL-first consumption order so legacy drains first). |
| 5 | `apply_pend_alc_delta` → MSA `PEND_QTY`/`FNL_Q` + `ARS_GRID_MJ*` `PEND_ALC`/`STK_TTL` (`:5227-5486`) | **None.** MSA stays total-based (FS-02 reads typed aggregates live); grid budget rollups are pool-agnostic by design. |
| 6 | `bootstrap_msa_hold_sync` → MSA `HOLD_QTY`/`FNL_Q` (`:5623-5816`) | **None** (total-based; feeds the FS-02 safety cap). |
| 7 | `log_operation('APPROVE')` (`:232`) | None. |

### Typed hold consumption (run side)

Every reader of `ARS_NL_TBL_HOLD_TRACKING` open rows gains the run-type filter `(ALLOC_TYPE = :alloc_type OR ALLOC_TYPE IS NULL)`:

| Reader | Location |
|---|---|
| Engine hold load (per MAJ_CAT) | `rule_engine_pandas.py:184-196` → band `:1809-1811`; consumed `rule_engine_per_opt.py:1155-1172` / pandas `:2032-2091` |
| Listing RL eligibility (`RL_HOLD_QTY` bake, Part 3.54) | `listing.py:1289-1310` |

Rationale: a hold created by a FRESH run reserves fresh stock; only FRESH runs (plus legacy NULL holds) may draw it. `HOLD_REM` decrement accounting prevents double-consumption of NULL holds across types.

---

## 17. FS-10 — Hold-Tracking Key Widening (complete edit list, verified)

The tracking row key widens to (WERKS, VAR_ART, SZ, **ALLOC_TYPE**). The snapshot/restore machinery is **fixed DDL with explicit column lists** — nothing propagates automatically. Every touch point:

| # | Edit | Location |
|---|---|---|
| 1 | Live table PK + idempotent `ALTER … ADD ALLOC_TYPE` (copy the RDC backfill pattern) | DDL `listing.py:2931-2949` (PK `:2947`); backfill pattern `parked_history.py:1332-1339` |
| 2 | Step B MERGE `ON` clause: `… AND ISNULL(T.ALLOC_TYPE,'') = ISNULL(R.ALLOC_TYPE,'')`; INSERT column list | `parked_history.py:1468-1470`, `:1486-1497` |
| 3 | Step A decrement join: type-match, NULL holds consumed first | `parked_history.py:1414-1446` |
| 4 | Snapshot table DDL + PK (SESSION_ID, WERKS, VAR_ART, SZ → + ALLOC_TYPE) | `parked_history.py:1309-1329` (PK `:1326`) |
| 5 | Scoped approve-time snapshot INSERT column list | `parked_history.py:693-707` |
| 6 | Full-table snapshot INSERT column list | `parked_history.py:1373-1381` |
| 7 | Revert `touched`-keys CTE + DELETE + restore UPDATE join & SET list | `parked_history.py:1538-1560`, `:1564-1583` — **miss this and revert silently drops ALLOC_TYPE** |
| 8 | Hold clear join + its ops-log revert | `pend_alc_service.py:4230-4283`, `:3340-3360` |
| 9 | Hold revise join + its ops-log revert | `pend_alc_service.py:4551-4579`, `:3420-3445` |
| 10 | Engine hold loads + listing RL_HOLD bake (already in FS-09) | `rule_engine_pandas.py:184-196`, `rule_engine_per_opt.py:1155-1172`, `listing.py:1289-1310` |

Hold clear/revise semantics with multiple typed rows per (WERKS, VAR_ART, SZ): mutations without an explicit type apply across rows ordered **NULL first, then oldest `LISTED_DATE`** (deterministic drain). The hold-dashboard **detail view and CSV export add an `ALLOC_TYPE` column** (display only; type-filtered mutations are a candidate enhancement). `bootstrap_msa_hold_sync` needs no change — it SUMs `HOLD_REM` across all rows per (RDC, VAR_ART), which remains the correct total.

---

## 18. FS-11 — Lifecycle Integrity (revert, manual, DO/BDC, generic upload)

**Revert of an approved session** (`_revert_approve`, `pend_alc_service.py:1574`) works with the typed design once FS-10 items 4-7 land: the −1 MSA/grid delta is total-based (unchanged, `:1600`); `DELETE ARS_PEND_ALC WHERE SESSION_ID` removes typed pend rows and the live typed aggregates self-correct (`:1608-1611`); snapshot restore returns tracking to its exact typed state (`:1617`); history→parked demotion (`:1626`) must carry `ALLOC_TYPE` back (verify the demote column reconciliation like the promote's).

**Manual pend entry** (`write_manual_pend_alc`, `pend_alc_service.py:2350`, INSERT `:2422-2426`, `SOURCE='MANUAL'`, bypasses approve): rows get `ALLOC_TYPE = NULL` → deduct from **both** pools (conservative, correct for stock the planner placed outside the pool system). Its per-chunk MSA/grid delta (`:2508`) stays total-based — **no code change needed** for correctness. Candidate enhancement: optional Fresh/GRT dropdown on the Manual Entry page.

**DO/BDC lifecycle**: no MSA delta at DO time (by design, `pend_alc.py:565-571`), so **no typed hook is needed** — `PEND_QTY` is a computed column, and the live typed aggregates shrink automatically as `DO_QTY` rises and rows close (`apply_do_deductions`, `pend_alc_service.py:3760-3772`). See IM-9 for the FIFO cross-type caveat.

**Adhoc close / BDC generate / schedules / reports**: read or close pend rows; typed aggregates self-correct; no changes.

**Generic Upload page** (`/upload` → `upload.py:36/:136`): can upsert **any** table including pend/hold/MSA with no MSA sync, no ops-log, and now no `ALLOC_TYPE` discipline — pre-existing governance gap that typed pools make more visible (IM-10).

---

## 19. Module Coverage Matrix (final review, all verified)

| Module (menu) | Touch with this feature | Change |
|---|---|---|
| MSA Stock Calc (`/msa`) | Produces the per-SLOC columns FS-02 reads | **None** |
| Grid Builder (`/data-prep/store-stock`) | Grids receive pool-agnostic `PEND_ALC`/`STK_TTL` deltas; `ST_STATUS` filter already includes UPC | **None** |
| Merge Rules | No pool/hold logic | **None** |
| Listing (`/data-prep/listing`) | Run parameters, pool override, hold gate, `OPT_MBQ_WH` source fix | FS-02/03/04/05/06/08 |
| Alloc Review (`/alc-review`) | Approve/reject entry; parked→history promotion | FS-07/09 (stamp flows through) |
| Hold Dashboard (`/reports/hold`) | Views + clear/revise mutations on typed rows | FS-10 items 8-9 + `ALLOC_TYPE` in detail/export |
| Pend Alc — Overview / Report / Open BDC / Schedule / Schedule Audit | Read-only over `ARS_PEND_ALC` / BDC history | **None** (detail views may add `ALLOC_TYPE` column later) |
| Pend Alc — Manual Entry | Direct pend INSERT bypassing approve | FS-11 (NULL semantics; no code change) |
| Pend Alc — Adhoc Close | Closes pend rows + MSA pend re-sync | **None** |
| Pend Alc — Daily DO Entry | `DO_QTY` FIFO; no MSA delta | **None** (IM-9 noted) |
| Pend Alc — Reconciliation (BDC push) | Stamps `BDC_QTY` | **None** |
| Pend Alc — Operations Log (revert) | Reverses approve/manual/hold ops | FS-11 + FS-10 items 4-7 |
| Upload Data (`/upload`) | Ungoverned table upsert | **None** (IM-10 governance flag) |
| ARS Dashboard / GAP Report | Read-only analytics | **None** |

---

## 20. FS-12 — Daily DO Entry: Deduction Method Option (user requirement 2026-07-09)

Today `apply_do_deductions` (`pend_alc_service.py:3541`) applies DO qty **FIFO** per (RDC, ST_CD, ARTICLE) via a windowed running total (`:3725-3742`, `ORDER BY P.APPROVED_AT, P.ID`), with an allocation-number pre-pass (`:3676-3691`) that resolves ST_CD from `ARS_BDC_HISTORY` when the CSV carries `Allocation_Number`. The DO CSV (`PendingDeliveryOrderPage.jsx:46-76`) has no session column.

**Change** — new request fields on `DoUpdateRequest` (`pend_alc.py:548`); note the existing `session_id` field is the upload/ops-log key, NOT a pend session — the new field is deliberately named differently:

```python
deduction_method: Literal["FIFO", "SESSION_FIRST", "SESSION_ONLY"] = "FIFO"
target_session_id: Optional[str] = None   # required when method != FIFO
```

| Method | Behaviour |
|---|---|
| `FIFO` (default) | Current behaviour, byte-identical. |
| `SESSION_FIRST` | Rows of `target_session_id` consume first, then FIFO for the remainder — prepend `CASE WHEN P.SESSION_ID = :tsid THEN 0 ELSE 1 END,` to both window ORDER BYs (`:3732`, `:3737`). |
| `SESSION_ONLY` | Deduct exclusively from that session — append `AND P.SESSION_ID = :tsid` to the open-rows WHERE (`:3742`); unmatched DO qty reports as unapplied. |

Threading: `pend_alc.py:598` (sync) and `:1442-1447` → `_do_run_job` → `:1292` (async, same per-slice call). `_revert_do` (`pend_alc_service.py:1320`) replays recorded per-row deltas — method-agnostic, **no change**. Ops-log payload (`pend_alc.py:615`) records the method for audit. Frontend: method `<select>` + session picker (via `pendAlcAPI.sessions()`, `api.js:651`) on `PendingDeliveryOrderPage.jsx`; both fields join the payloads at `:184-189` and `:229-234`.

## 21. FS-13 — Reconciliation "Generate BDC" Hardening (verified defects)

Verified root cause of the reported failures: the async job registry `_jobs` is a **per-process dict** (`pend_alc.py:993`) while production runs `gunicorn -w 4` (`startup.sh:12`) — poll/download requests that land on another worker 404 (`:1530`), and the Reco page's poll (`PendAlcRecoPage.jsx:491-493`) aborts on the **first** error. Meanwhile `stamp_bdc_qty` (`pend_alc_service.py:3530`) and `insert_bdc_history` (`:2631`) have already committed, so the next generate says "No open pending rows found for BDC" (`pend_alc.py:1071`) — compounding the "broken button" perception.

| # | Fix | Where |
|---|---|---|
| 1 | Persist async jobs to a small SQL table `ARS_ASYNC_JOBS` (job_id PK, kind, status, progress, error, result_json, zip_path, created_at) written at state transitions; `/async-jobs/{id}` and download read DB when the dict misses. Dict stays as a warm cache. | `pend_alc.py:993-1025`, `:1524-1565` |
| 2 | Poll error tolerance on the Reco page — mirror `POLL_ERROR_TOLERANCE` from `PendingDeliveryOrderPage.jsx:203-226`; 404 is retryable. | `PendAlcRecoPage.jsx:479-495` |
| 3 | Drop the `u.st_cd = ''` wildcard match in `stamp_bdc_qty` when the caller passed a store filter (over-stamps all stores for (RDC, ART)). | `pend_alc_service.py:3512` |
| 4 | Self-explanatory failure message: include the count of rows blocked by open BDC history when generate returns "No open pending rows". | `pend_alc.py:1071` |

### Additional test cases

| ID | Test | Expected |
|---|---|---|
| TC-22 | DO upload, `deduction_method=FIFO` | Byte-identical to current behaviour. |
| TC-23 | `SESSION_FIRST` with two open sessions on one key | Target session's rows drain to zero before the other session's oldest row is touched. |
| TC-24 | `SESSION_ONLY` with insufficient target-session qty | Only target rows deducted; remainder reported unapplied; other sessions untouched. |
| TC-25 | BDC generate under multi-worker | Poll succeeds from any worker (DB-backed job row); transient 404 does not abort the Reco page poll; re-generate after a client abort does not double-stamp. |
