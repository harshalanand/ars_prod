# MSA Stock Calculation — ARS Manual

## BRD — Why this exists

MSA (Merchandise Stock Availability) is the first computed stage of the ARS
listing/allocation pipeline. It answers one question: **for every warehouse
(RDC) and article, how much stock is genuinely free to allocate right now?**
Raw warehouse stock is not the answer — a chunk of it is already promised.
Some is sitting under an approved-but-not-yet-delivered pending allocation
(SAP has not cut the Delivery Order yet), and some is reserved against a prior
TBL/NL hold for a specific store. Allocating on gross stock would double-commit
the same physical units.

The planner (merchandising ops) runs MSA at the start of a replenishment cycle.
They pick a stock date and the SLOCs (shelf/storage locations) that count as
"available to sell", set a survival threshold, and Generate. MSA reads the
source stock view, deducts open pending and open holds, and produces the free
quantity `FNL_Q` at variant, colour, and total grain.

Everything downstream — Grid Builder, Listing, and the rule engine allocation
— consumes MSA's `FNL_Q` and the pending/hold tallies it reconciles. If MSA is
wrong (a pending obligation lands on no row, or a colour rolls up across the
wrong RDC), the error silently propagates into every store's allocation. That
is why MSA carries hard reconciliation guarantees and a five-check validation
suite.

## FSD — How it works

### Inputs & outputs

**Consumed (all in Rep_data DB):**

| Source | Grain | Role |
|---|---|---|
| `VW_ET_MSA_STK_WITH_MASTER` (over `ET_MSA_STK`) | (DATE, ST_CD, SLOC, ARTICLE_NUMBER, …) | Warehouse stock. Filtered `SEG IN ('APP','GM')` and `SLOC IN (selected)`. |
| `ARS_PEND_ALC` | (RDC, ARTICLE_NUMBER) open rows | Open pending (`IS_CLOSED=0, PEND_QTY>0`) → deducted as `PEND_QTY`. |
| `ARS_NL_TBL_HOLD_TRACKING` | (WERKS, VAR_ART) open rows | Open holds (`IS_CLOSED=0, HOLD_REM>0`) → deducted as `HOLD_QTY`. WERKS→RDC via store master. |
| `vw_master_product` | variant catalogue | Master variant list for universe backfill (zero-stock placeholder rows). |
| `Master_ALC_INPUT_ST_MASTER` | (ST_CD) | WERKS→RDC mapping for holds. |
| `ARS_MSA_SLOC_SETTINGS` | (sloc) | **Pool classification** `sloc_type ∈ {FRESH, GRT}` (exclusive — no BOTH), `is_active`. Drives the Step-12 row-per-type split. A SLOC absent from the table is treated as FRESH (warning logged). |

**Produced (all in Rep_data DB — despite a stale docstring saying "Main DB"):**

| Table | Grain | Notes |
|---|---|---|
| `ARS_MSA_TOTAL` | variant × type: one row per (RDC, ARTICLE_NUMBER, …, SZ, **ALLOC_TYPE**) | The full pivot, expanded row-per-type in Step 12. Per folded type, `SUM(PEND_QTY)`/`SUM(HOLD_QTY)` reconcile to open source rows. |
| `ARS_MSA_VAR_ART` | same variant × type grain, threshold-filtered | Groups that passed the Step-10 engagement threshold, then typed. |
| `ARS_MSA_GEN_ART` | colour × type: one row per (RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, **ALLOC_TYPE**) | Rollup of VAR_ART, per type. |
| `MSA_Calculation_Sequence` | one row per run | date, slocs (JSON), threshold, row counts, created_by/at, status. |

Output column `RDC` is the renamed `ST_CD` (renamed right after the pivot).
`ALLOC_TYPE ∈ {'FRESH','GRT'}` on every output row — one SKU with stock in both
pools emits **two rows**; downstream traces uniformly with `WHERE ALLOC_TYPE=…`.

### Rules & invariants

- **RDC rename happens right after the Step-5 pivot**, before any downstream
  step reads column names. Renaming late caused the historical GEN_ART
  RDC-collapse bug (aggregation ran before rename, `"RDC" not in columns`,
  `agg("first")` stamped an arbitrary RDC).
- **Stock contribution stays SLOC-scoped.** A product whose stock sits only on
  a non-selected shelf is NOT pulled into MSA by stock alone. The universe
  expands only when a real obligation (PEND or HOLD) is attached to the
  (RDC, GEN_ART) pair.
- **Every open obligation must land on a row.** Universe backfill (Step 6)
  guarantees `SUM(TOTAL.PEND_QTY) == SUM(open ARS_PEND_ALC)` and
  `SUM(TOTAL.HOLD_QTY) == SUM(open HOLD_REM)`.
- **GEN_ART = exact rollup of VAR_ART** for STK/PEND/HOLD/FNL_Q per colour key.
- **FNL_Q is never negative** — it is `max(…, 0)`.
- **Pools are exclusive and complete.** Every SLOC is FRESH xor GRT
  (`ARS_MSA_SLOC_SETTINGS`; no BOTH), so `STK(FRESH) + STK(GRT) = STK(total)` per
  SKU — types can be summed back to the untyped total without double-counting.
- **Legacy folds to FRESH.** Any pend/hold ledger row with `ALLOC_TYPE`
  NULL/'' deducts from the FRESH row (and drains away as DOs confirm / holds
  close). A GRT MSA row is therefore never reduced by an untyped obligation.
- **One FRESH row per SKU, always.** Even all-zero — it is the universe
  placeholder and the size-coverage denominator. GRT rows are additive.
- MBQ sparseness (invariant 3): when MSA outputs feed sec-cap math, `*_MBQ=0`
  means "no constraint at this grain", not zero budget.
- ACS_D ≠ daily sale (invariant 5): MSA does not compute velocity; downstream
  velocity uses `MAX_DAILY_SALE`, never `ACS_D`.

### Formulas

Untyped intermediate (Steps 5-9, unchanged):

```
STK_QTY   = SUM over selected SLOC pivot columns          (Step 5)
PEND_QTY  = ARS_PEND (open ARS_PEND_ALC, joined on RDC+ARTICLE_NUMBER)   (Step 7)
HOLD_QTY  = SUM(HOLD_REM) open holds, WERKS→RDC, joined on RDC+ARTICLE    (Step 8)
FNL_Q     = MAX(STK_QTY − PEND_QTY − HOLD_QTY, 0)         (Step 9)

engagement(group) = SUM(FNL_Q) + SUM(PEND_QTY) + SUM(HOLD_QTY)   (Step 10)
    group = (RDC, MAJ_CAT, GEN_ART_NUMBER, CLR)
    keep group iff engagement > threshold
```

Row-per-type expansion (Step 12 — final output values, per typed row):

```
folded(type) = 'FRESH' when ledger ALLOC_TYPE is NULL or '' (legacy), else the value

per SKU row → emit FRESH row (ALWAYS) and GRT row (only with signal):
  STK_QTY(T)  = Σ SLOC pivot columns classified sloc_type = T
                (unknown SLOC → FRESH; the OTHER type's SLOC cols are zeroed on this row)
  PEND_QTY(T) = Σ open ARS_PEND_ALC (ALLOC_QTY − DO_QTY) where folded(ALLOC_TYPE) = T,
                per (RDC, ARTICLE_NUMBER)                       [GEN grain: (RDC, GEN, CLR)]
  HOLD_QTY(T) = Σ open HOLD_REM where folded(ALLOC_TYPE) = T, WERKS→RDC
  FNL_Q(T)    = MAX(STK_QTY(T) − PEND_QTY(T) − HOLD_QTY(T), 0)

GRT emission rule: emit the GRT row iff STK(GRT) > 0 OR PEND(GRT) > 0 OR HOLD(GRT) > 0.
FRESH emission rule: ALWAYS emit — even all-zero. FRESH rows carry the
universe/placeholder role (they are the denominator of the listing
size-coverage ratio VAR_FNL_COUNT/VAR_COUNT; see 2026-07-10 recorded rule).
```

Step-11 aggregation (VAR_ART → GEN_ART), by column class:
- Stock/obligation columns (SLOC cols, STK_QTY, PEND_QTY, HOLD_QTY, FNL_Q,
  numeric pend cols) → `sum`.
- SZ-varying numeric master columns (MRP, densities, …) → `max` (so a
  zero-stock SZ row cannot override the meaningful value).
- Descriptive strings (invariant within a colour) → `first`.
- `ARTICLE_NUMBER`, `ARTICLE_DESC`, `SZ` are excluded from the hierarchy key.

### The 12-step flow (universe-anchored + row-per-type, July 2026)

1. Filter SLOC (drops rows outside the pivot scope).
2. Numeric safety — coerce `STK_Q`.
3. Fill missing dims with defaults (`CLR='A'`, `SZ='A'`, `M_VND_CD=0`, MP dims `'NA'`).
4. SEG filter `IN ('APP','GM')`.
5. Pivot by SLOC → `msa_pivot`; **rename `ST_CD → RDC`**; seed `PEND_QTY=0`.
6. **Universe backfill** — `_load_universe(slocs, date)` unions three sources:
   A. stock in selected SLOCs only; B. open `ARS_PEND_ALC`; C. open
   `ARS_NL_TBL_HOLD_TRACKING` (WERKS→RDC, VAR_ART→GEN via master). For each
   (RDC, GEN_ART) in the union, every master VAR_ART missing from the pivot is
   inserted as a zero-stock placeholder so PEND/HOLD always have a landing row.
   The backfill loader itself only pulls `ATT_TYP IN (config allowlist)`
   variants (see Step 6b) so it can never seed a generic header / structured
   article.
6b. **ATT_TYP gate (00/02 only)** — keep only real sellable SKUs per
    `settings.MSA_ALLOWED_ATT_TYP` (default `['00','02']` = single + variant);
    drop `01` generic headers and `11` structured/prepack articles. `ATT_TYP`
    is a master attribute the MSA source view does not carry, so it is resolved
    per `ARTICLE_NUMBER` from `vw_master_product` (`_load_att_typ_map`). Runs
    after Step 6 (covers stocked **and** backfilled rows in one pass) and
    before Step 7 (no obligation lands on an excluded article). Unmapped
    articles are dropped (an allocatable SKU must resolve to an allowed
    category). Fail-open: if `ATT_TYP` cannot be resolved the gate is skipped
    with a warning. Downstream Grid / Listing / Allocation inherit the filter.
7. Merge `ARS_PEND_ALC` → `PEND_QTY` on (RDC, ARTICLE_NUMBER); logs a mismatch
   warning if matched < 99% of expected.
8. Merge `ARS_NL_TBL_HOLD_TRACKING` → `HOLD_QTY` (via WERKS→RDC).
9. `FNL_Q = max(STK − PEND − HOLD, 0)`.
10. Relaxed engagement threshold (see formula) — admits pend-only / hold-only
    groups into VAR_ART / GEN_ART.
11. Aggregate VAR_ART → GEN_ART on (RDC, MAJ_CAT, GEN_ART_NUMBER, CLR).
12. **Row-per-type expansion** (`_expand_to_typed_rows`, applied to all three
    result frames before storage/return):
    - Classify SLOC pivot columns via `ARS_MSA_SLOC_SETTINGS.sloc_type`
      (GRT-classified vs everything-else-is-FRESH).
    - Split each row into a FRESH copy and a GRT copy; per copy, `STK_QTY` =
      Σ of that type's SLOC columns; the other type's SLOC columns are zeroed.
    - Re-derive `PEND_QTY`/`HOLD_QTY` **typed** live from the open ledgers with
      the folded-type rule (legacy NULL/'' → FRESH) — var grain
      (RDC, ARTICLE_NUMBER) for TOTAL/VAR_ART, gen grain (RDC, GEN, CLR) for GEN_ART.
    - Recompute `FNL_Q` per typed row.
    - Keep every FRESH row (placeholder/denominator role). Keep GRT rows
      **per-OPT, not per-variant** (2026-07-13): if an OPT
      (RDC, MAJ_CAT, GEN_ART_NUMBER, CLR) has GRT signal at **any** size, keep
      its **whole GRT size-ladder** (zero sizes as placeholders — the GRT
      coverage/CONT denominator), mirroring FRESH. Pure-FRESH OPTs emit no GRT
      rows. Each pool's ladder is completed independently (FRESH filled from
      FRESH signal, GRT from GRT signal). Non-fatal: on any exception the run
      falls back to untyped output and appends a warning (`self.warnings`).

(Steps 4b category-RLS and 7.5 master-variant backfill are sub-steps within
this flow; the master loader chunks inserts at 5000 rows and retries on
transient connection drops 10054/10053/08S01.)

### Type-aware lifecycle after storage (who updates typed MSA rows)

| Event | Updates | Typing rule |
|---|---|---|
| Approve (`apply_pend_alc_delta` +1) | MSA `PEND_QTY`/`FNL_Q` on TOTAL/VAR/GEN + `ARS_GRID_MJ*` rollups | Delta rows carry folded type; only the **matching typed MSA row** is updated. Grid rollups stay pool-agnostic. |
| Revert (`apply_pend_alc_delta` −1) | Same tables, reversed | Same type-matching — rollback deducts/restores the **correct typed FNL_Q**. |
| Hold re-sync (`bootstrap_msa_hold_sync`) | MSA `HOLD_QTY`/`FNL_Q` | `SUM(HOLD_REM)` per (RDC, VAR_ART, folded type) → typed row. |
| Pend re-sync (`bootstrap_msa_pend_sync`) | MSA `PEND_QTY`/`FNL_Q` | Per (RDC, ARTICLE, folded type) → typed row. |
| DO confirmation | **No MSA delta** (by design) | Reconciles at the next MSA generation. |
| Allocation read (Stage A/B) | Reads only `WHERE ALLOC_TYPE = run's alloc_type` | E-01 guard: 400 when the MSA has no rows of the requested type. |

### Validation gates — validate first, then create

Run these five reconciliation checks after every Generate, **before** treating
the sequence as usable downstream. Frame each as "validate the sum matches
before publishing this sequence":

1. **TOTAL ↔ PEND_ALC** — `SUM(TOTAL.PEND_QTY) ≈ SUM(ARS_PEND_ALC.PEND_QTY WHERE IS_CLOSED=0)`, and **per folded type**: `SUM(TOTAL.PEND_QTY WHERE ALLOC_TYPE=T) ≈ SUM(open pend WHERE folded(ALLOC_TYPE)=T)`. Catches pending rows with no matching MSA variant or landing on the wrong pool. |
2. **TOTAL ↔ HOLD_TRACKING** — same shape for `HOLD_QTY`/`HOLD_REM` (WERKS→RDC), overall **and per folded type**. Catches holds that don't land or land untyped.
3. **TOTAL ↔ source view** — `SUM(TOTAL.STK_QTY) ≈ SUM(source STK_Q WHERE SLOC IN selected AND SEG IN ('APP','GM'))` for the run date (types sum back to the total because pools are exclusive: `Σ STK(FRESH) + Σ STK(GRT) = Σ STK`). Catches a pivot/filter/classification regression.
4. **VAR_ART ⊂ TOTAL** — per passing colour key, `COUNT(DISTINCT VAR_ART article) == COUNT(DISTINCT TOTAL article)`. Catches a passing group silently losing articles.
5. **GEN_ART = rollup(VAR_ART)** — per (colour key, **ALLOC_TYPE**), `GEN_ART.X == SUM(VAR_ART.X)` for X ∈ {STK_QTY, PEND_QTY, HOLD_QTY, FNL_Q}. Catches the RDC-grain collapse bug and cross-pool leakage.
6. **Typed-row shape** — every SKU in TOTAL has exactly one FRESH row (always) and at most one GRT row; `SELECT … GROUP BY key HAVING COUNT(*) > 2` returns nothing; no row has `ALLOC_TYPE` outside {'FRESH','GRT'}.

After the universe-anchored build, all five should sit at zero delta. At store
time, `store_results()` additionally runs `bootstrap_msa_pend_sync` and
`bootstrap_msa_hold_sync` as idempotent safety-net re-seeds of PEND/HOLD/FNL_Q
across the three tables.

### Key columns

| Column | Meaning | Formula / source |
|---|---|---|
| `RDC` | Warehouse / regional DC axis | Renamed from `ST_CD` after Step-5 pivot |
| `STK_QTY` | Total stock across selected SLOCs | `SUM(SLOC pivot cols)` |
| `PEND_QTY` | Open pending reserved against stock | `ARS_PEND` from open `ARS_PEND_ALC` (Step 7) |
| `HOLD_QTY` | Open hold reserved for a store | `SUM(HOLD_REM)` open holds, WERKS→RDC (Step 8) |
| `FNL_Q` | **Free-to-allocate quantity** | `MAX(STK_QTY − PEND_QTY − HOLD_QTY, 0)` |
| `GEN_ART_NUMBER` | Generic (colour-level) article | From `vw_master_product` / source |
| `CLR` | Colour | Source; default `'A'` |
| `SZ` | Size | Excluded from GEN_ART hierarchy; summed away in rollup |
| `MAJ_CAT` | Major category | Source; RLS-filtered per user |
| `SEG` | Segment | Filtered to `APP` / `GM` |
| SLOC columns (e.g. `V02_FRESH`) | Per-shelf stock | Pivot values; on a typed row only that type's SLOC columns are populated (other type's = 0) |
| `ALLOC_TYPE` | **Pool of this row** | `'FRESH'` or `'GRT'` (Step 12). FRESH row always exists per SKU; GRT row only with signal. All STK/PEND/HOLD/FNL_Q on the row are that pool's values. |

## Recorded rules
<!-- dated appendable bullets; leave this comment and add any dated rules already known -->

- **2026-06 (from KB)** — Universe expands only on a real PEND/HOLD obligation; pure cross-shelf stock alone never pulls a GEN_ART into MSA. Why: keeps stock contribution SLOC-scoped and avoids inflating the universe.
- **2026-07-14** — The parallel MSA rebuild (`run_parallel_pipeline`, `clear_previous=True`) TRUNCATEs `ARS_MSA_TOTAL`/`GEN_ART`/`VAR_ART` up front, then appends per batch and `_store_batch_results` silently skips empty frames. If the `'msa'` (TOTAL) aggregate comes back empty while gen/var don't, `ARS_MSA_TOTAL` is left truncated-and-empty and the run still reported "completed". This now **fails** the run (guard: `clear_previous and total_msa_rows==0 and gen/var>0`). Downstream impact seen on session `20260713_164356_383`: a rebuild ran mid-listing (32-min FRESH run), emptying `ARS_MSA_TOTAL` before Part 8.4, so park captured 0 MSA_TOTAL rows and Approve promoted 0 to `ARS_MSA_TOTAL_HISTORY` (alloc/listing were fine). Park now surfaces an expected-but-empty target via `snapshot_session_to_parked → empty_targets`, `parked_status='PARKED_PARTIAL'`, and a warning log — no longer a silent clean `PARKED`. Open follow-up: a manual MSA rebuild is NOT blocked while a listing run is in progress (no `has_running_session` guard on `/pipeline`). see also: pendalc.md
- **2026-06 (from KB)** — `ST_CD → RDC` rename must occur immediately after the Step-5 pivot, before any step reads column names. Why: late rename caused the GEN_ART RDC-collapse bug (`agg("first")` stamped an arbitrary RDC and bootstrap then dropped ~half the per-RDC PEND/HOLD).
- **2026-06 (from KB)** — All four MSA output tables live in the Data (Rep_data) DB, not the Main/system DB — the `msa_result_storage.py` docstring saying otherwise is wrong.
- **2026-07-10** — MSA output is **row-per-type**: every SKU emits an `ALLOC_TYPE='FRESH'` row and (when it has signal) a `'GRT'` row, per `ARS_MSA_SLOC_SETTINGS.sloc_type` (FRESH|GRT, exclusive — no BOTH). STK/PEND/HOLD/FNL_Q are baked per typed row; legacy/untyped pend & hold fold into the FRESH row; each row zeroes the *other* type's SLOC columns. Why: uniform `WHERE ALLOC_TYPE=` tracing across MSA/alloc/pend/hold and type-correct FNL_Q on approve/rollback.
- **2026-07-10** — **FRESH rows are always emitted, even all-zero.** They carry the universe/placeholder role of the old untyped rows: zero rows are the denominator of the listing size-coverage ratio (`VAR_FNL_COUNT/VAR_COUNT`). Dropping them (initial implementation) inflated the ratio and over-listed options (LISTED_OPTS 303k → 422k on identical inputs). GRT rows remain additive (emitted only with stock/pend/hold).
- **2026-07-10** — `/msa/calculate` returns a **preview only** (first 500 rows per frame, `preview: true`) when `auto_store_results=true`; full frames are persisted by the storage job. Why: a full-universe row-per-type calc exceeds 2M rows — the old full-inline JSON response (hundreds of MB) was unparseable by the browser and crashed the MSA page ("reading 'sequence_id'"). Manual-save mode (`auto_store_results=false`) still returns full frames because the Save button posts them back.
- **2026-07-10** — Warehouse SLOC pool classification table renamed `ARS_MSA_SLOC_SETTINGS` (migration 019) to avoid colliding with the legacy STORE-sloc table name, and is now managed from the **MSA Stock Calculation page → "Warehouse SLOC Pools (Fresh / GRT)" panel** (per-SLOC FRESH/GRT toggle, active flag, Sync from the warehouse view, change audit `updated_by`/`type_changed_at`). Pool changes apply from the NEXT MSA generation. Endpoints: `GET/PUT /msa/sloc-settings`, `POST /msa/sloc-settings/sync`.
- **2026-07-13** — MSA keeps only articles whose `ATT_TYP` (SAP article category, from `vw_master_product`) is in `settings.MSA_ALLOWED_ATT_TYP` (**default `['00','02']`** = single + variant); it drops `01` generic headers and `11` structured/prepack articles. Enforced at MSA generation (new **Step 6b**, `_load_att_typ_map`) so Grid/Listing/Allocation all inherit it; the Step-6 backfill loader also filters `ATT_TYP IN (allowlist)` so headers/structured can never be seeded. Why: a generic header (01) must never be allocated directly — only its 02 variants — and 11 structured articles aren't individually replenished. It is a fixed config rule (not a per-run toggle); unmapped articles are dropped (must resolve to an allowed category); fail-open if `ATT_TYP` can't be resolved. On current data 0 rows are dropped (MSA was already all 00/02) — this is a permanent guardrail, not a numbers change.
