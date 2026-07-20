# Pending Allocation — ARS Manual

## BRD — Why this exists

When a listing session is **Approved**, the system has *decided* to send stock to stores but SAP has not yet cut the physical **Delivery Order (DO)**. Those approved-but-not-yet-shipped units are **PENDING**. If ARS kept treating that stock as freely available, the very next allocation run would allocate it a second time — double-shipping the warehouse into an empty inventory.

`ARS_PEND_ALC` is the ledger that closes this gap. Every approved allocation writes a pending row; MSA then subtracts pending from available stock (`FNL_Q = STK − PEND − HOLD`) so the next run only offers what is genuinely still on the shelf. As SAP confirms shipments via **DO upload**, pending is drawn down; when a row is fully shipped it closes. The module also drives **BDC** (the SAP-ready allocation file), keeps an **operations log** so any action is auditable and revertable, and exposes **reconciliation** so ops can see planned-vs-shipped at any moment.

Who uses it: the daily replenishment desk (DO entry, BDC generation, schedule) and category/ops leads (reconciliation, corrections, revert). Pipeline position: **the feedback loop** — it sits after Listing/Approve and feeds MSA on the next cycle. It is the single mechanism that makes ARS *stateful* across daily runs.

## FSD — How it works

### Inputs & outputs

| Table | Role | Grain |
|---|---|---|
| `ARS_ALLOC_HISTORY` | Approved allocation lines (source of pending) | one row per alloc line |
| `ARS_PEND_ALC` | Pending ledger (created & maintained here) | `SESSION_ID × RDC × ST_CD × ARTICLE_NUMBER × ALLOC_MODE` |
| `ARS_BDC_HISTORY` | Audit of every BDC line sent to SAP | one row per `(RDC, ST_CD, ARTICLE)` per BDC event |
| `ARS_PEND_ALC_OPERATIONS` | Operations log (all mutations, revert payloads) | one row per operation |
| `ARS_STORE_BDC_SCHEDULE` | Which stores' BDC runs on which weekday | one row per store |
| `Master_ALC_INPUT_ST_MASTER` | Maps destination `WERKS`(=ST_CD) → source `RDC` | one row per store |
| `ARS_MSA_TOTAL / VAR_ART / GEN_ART` | Consume `PEND_QTY` into `FNL_Q` | (RDC, ARTICLE) |

`ARS_PEND_ALC` key columns: `RDC` = **source warehouse** (drives MSA deduction), `ST_CD` = **destination store** (traceability). `PEND_QTY = ALLOC_QTY − DO_QTY` is a **persisted computed column**. `BDC_QTY` is cumulative-qty-sent, audit only (never used by MSA).

### Rules & invariants

- **PEND feeds back into MSA**: `FNL_Q = max(STK_QTY − PEND_QTY − HOLD_QTY, 0)`, recomputed inside every MSA-adjusting UPDATE. Open pending rows also anchor the MSA universe (Step 6) so a row always exists to land the deduction on.
- **`RDC ≠ ST_CD`**: `RDC` is the ship-from warehouse; `ST_CD` (= `WERKS` in alloc history) is the ship-to store. Both are stored; MSA aggregates `PEND_QTY` by `RDC`.
- **Only one row *closes* pending when fully covered**: `IS_CLOSED=1` when `DO_QTY ≥ ALLOC_QTY` (i.e. `PEND_QTY` hits 0).
- **DO upload does NOT touch MSA `STK_QTY`**: `STK_QTY` is a daily snapshot; the next full MSA run reconciles physical stock. DO only draws down `DO_QTY`/`PEND_QTY`.
- **No unique constraint** exists on the logical grain (only PK on `ID`). `write_pend_alc` enforces the grain with `NOT EXISTS`; `write_manual_pend_alc` does **not** — duplicate manual rows for one key silently double-count.
- **Blank `ST_CD` = wildcard** (any store for the `(RDC, ARTICLE)`) in adhoc close and BDC-history cancel. Guarded in the API (`confirm_close_all_stores`) but not in the service.
- **Every mutation is logged and revertable** via `ARS_PEND_ALC_OPERATIONS`; revert is gated by per-op `_check_*_revert` freshness checks.

### Lifecycle & operations — how and why

Row lifecycle: `PENDING (IS_CLOSED=0) → [BDC stamped] → [DO drawn down] → CLOSED (IS_CLOSED=1) | cancelled/reverted`.

**1. Maintain `ARS_PEND_ALC` — auto write on Approve (`write_pend_alc`)**
- *Why*: turn an approved session's shipments into pending so MSA nets them out next cycle.
- *How*: called inside `approve_parked` (under the per-session applock). Reads `ARS_ALLOC_HISTORY` for the `SESSION_ID`, joins store master to resolve `RDC` from `WERKS`, and `ARS_LISTING_WORKING_HISTORY` to carry `OPT_TYPE` into `ALLOC_MODE`. One row per `(SESSION, RDC, ST_CD, ARTICLE, ALLOC_MODE)`, `ALLOC_QTY = SUM(alloc)`. Then `apply_pend_alc_delta_by_session(sign=+1)` immediately applies the +PEND delta to MSA + grid so the next run sees reduced `FNL_Q` without a rebuild.
- *Validate-before-create gate*: **idempotent** via `NOT EXISTS` on the grain + the session applock, so a double-Approve cannot write pending twice. Rows with `ALLOC_QTY ≤ 0` are excluded.

**2. Maintain `ARS_PEND_ALC` — Manual upload (`write_manual_pend_alc`, `POST /manual-upload`)**
- *Why*: inject pending the auto-flow didn't produce (manual DO plans, corrections, cross-loads).
- *How*: direct `fast_executemany` INSERT tagged `SOURCE='MANUAL'`; multi-chunk uploads share one `session_id` so they roll up to one revertable ops-log entry. `adjust_msa_after_pend_insert` refreshes MSA `PEND_QTY/FNL_Q` for the affected keys.
- *Validate-before-create gate*: **weak** — this is a known sharp edge. Negative `alloc_qty` is dropped (`>0` filter), but blank `rdc`/`article_number` and articles absent from `vw_master_product` are inserted and become **MSA orphans** (their delta UPDATE matches 0 rows). There is no grain de-dup, so duplicate manual rows double-count. Reviewers: validate the article exists and the key is unique before trusting a manual load.

**3. Daily DO Entry (`apply_do_deductions`, `POST /do-update` + `/do-update-async`)**
- *Why*: record what SAP actually shipped, so pending shrinks to reality and fully-shipped rows close.
- *How*: input `(rdc, article_number, do_qty, [st_cd], [do_number], [allocation_number])`. Increments `DO_QTY` across open rows for the key. `deduction_method`:
  - `FIFO` (default) — open rows drain `APPROVED_AT ASC, ID ASC`.
  - `SESSION_FIRST` — `target_session_id` rows drain first, then FIFO.
  - `SESSION_ONLY` — deduct only from `target_session_id`; excess surfaces in `overflow_rows`.
  A row hitting `PEND_QTY=0` sets `IS_CLOSED=1` and side-effect-closes its OPEN BDC history (`OPEN→CONFIRMED`, recorded as `auto_history_closes`). `allocation_number` resolves `ST_CD` from BDC history (BDC wins over user-supplied on conflict).
- *Validate-before-create gate*: `_validate_deduction_method` — **`target_session_id` is required** whenever method ≠ FIFO (else `ValueError`/400). Rows must be **open** to receive a deduction; a DO for a key with no open pending lands nowhere and its qty shows in `overflow_rows` (audit-only, not auto-corrected because the DO already happened in SAP). Does **not** touch MSA `STK_QTY` (see invariant). Logged `OP_TYPE='DO'`; chunk 1 (`is_first_chunk`) INSERTs the ops row, chunks 2..N MERGE their `pend_updates`/`history_updates` into it so the whole upload reverts as one unit.

**4. Adhoc correction / adhoc close (`apply_adhoc_close`, `POST /close-rows` + `/close-rows-file`)**
- *Why*: abandon a BDC that should not ship (classic case: bot issued a BDC for an article with no MSA stock) and release the in-flight lock so a corrected BDC can flow.
- *How*: per `(RDC, ST_CD, ARTICLE)` — flip open `ARS_PEND_ALC` rows to `IS_CLOSED=1` (prefix `REMARKS` with `ADHOC: <reason>`), and flip still-OPEN `ARS_BDC_HISTORY` rows to `STATUS='CANCELLED'` (releasing `_NO_OPEN_BDC_PREDICATE`). Pre-images captured for revert. Now calls `bootstrap_msa_pend_sync` after closing so MSA `PEND_QTY` no longer stays inflated until the next Generate.
- *Validate-before-create gate*: **reason required**; and a **blank-`ST_CD` wildcard is refused** (400 with the offending keys) unless `confirm_close_all_stores=true` — a blank `ST_CD` closes EVERY store for the `(RDC, ARTICLE)`, a footgun when a column was left empty by accident. Logged `OP_TYPE='ADHOC_CLOSE'`.

**5. Create BDC (`POST /bdc-generate` + `/bdc-generate-async`, `stamp_bdc_qty` + `insert_bdc_history`)**
- *Why*: produce the SAP-ready 9-column allocation file and record what was sent.
- *How*: store selection priority = explicit `st_cd_list` > `target_date` schedule lookup > all stores with open pending. Phase 1 read-only aggregation (`WITH (NOLOCK)`) of open pending by `(RDC, ST_CD, ARTICLE, MAJ_CAT)`; one allocation number per `(FY, RDC)`. Phase 2 short write txn: `stamp_bdc_qty` bumps `BDC_QTY` + `LAST_BDC_AT`, `insert_bdc_history` appends one OPEN history row per line (reading back by `ID > prev_max_id` so it never grabs a prior op's rows). Excel columns: Serial No, Allocation Date, Allocation Number, VENDOR, MATERIAL NO, BDC-QTY, RECEIVING STORE, Picking Date, Remark.
- *Validate-before-create gate*: the candidate filter is `IS_CLOSED=0 AND PEND_QTY>0 AND _NO_OPEN_BDC_PREDICATE` — a row is only eligible if it has pending qty **and no already-OPEN BDC history** for the same `(RDC, ST_CD, ARTICLE)`, so the same pending is never double-BDC'd. `404` if nothing qualifies; `400` if `target_date` resolves to zero scheduled stores.

**6. Reconciliation (`GET /reco`, `/reco-export`, `/reco-summary`, `/pend-vs-msa-gap`)**
- *Why*: show planned-vs-shipped and surface stuck/over-shipped pending; verify MSA agrees with the ledger.
- *How*: paged `reco` joins each pending row to its latest BDC (`ALLOCATION_NUMBER`, `STATUS`, `DO_RECEIVED`); no BDC ⇒ `BDC_STATUS='NEVER_SENT'`. Filters by date/RDC/MAJ_CAT/mode/source/closed/session, per-column multi-value filters, aging bands. `pend-vs-msa-gap` cross-checks `SUM(PEND_QTY)` in the ledger against MSA `PEND_QTY` per `(RDC, ARTICLE)`.
- *Validate-before-create (close) gate*: reconciliation is where **shipped vs planned** is confirmed before a pending is considered settled — a row only closes when `DO_QTY ≥ ALLOC_QTY`; a partial DO leaves it open with the residual visible here. Use reco to confirm the gap is real before an adhoc close.

**7. Open BDC report (`GET /bdc-history`, `/bdc-history-allocations`, `/bdc-history-redownload`, `/bdc-history-export`)**
- *Why*: answer "every BDC ever sent for this store/article and how much SAP confirmed", and let ops re-download a prior BDC file.
- *How*: reads `ARS_BDC_HISTORY` (`STATUS ∈ OPEN/CLOSED_PARTIAL/CONFIRMED/CANCELLED`, `DO_RECEIVED` running total). `bdc-history-redownload` regenerates the exact Excel for a past `ALLOCATION_NUMBER`. `bdc-recover-orphans` / `close-orphan-bdc-history` repair rows stamped `BDC_QTY>0` with no matching history.
- *Cancel gate*: `update_bdc_history_with_do` treats `do_qty=0` as a CANCEL; with only `(rdc, art)` and `do_qty=0` the cancel scope is **global** for that key — confirm the allocation number before a zero-qty cancel.

**8. BDC schedule (`GET/POST/DELETE /schedule`, `get_stores_for_date`)**
- *Why*: drive which stores' BDC runs on which weekday so `bdc-generate?target_date=` selects the right store set.
- *How*: `ARS_STORE_BDC_SCHEDULE` keyed by store with per-weekday flags (Mon–Sat; Sunday yields none). Upserts write an audit trail (`/schedule/audit`). `get_stores_for_date` maps a date → weekday → store list.
- *Validate-before-create gate*: `schedule/stores-for-date` lets the desk preview the resolved store set before generating; `bdc-generate` returns `400` if the date has no scheduled stores (never silently sends nothing).

**9. Operations log (`GET /operations`, `POST /operations/{id}/preview-revert` + `/revert` + `/revert-async`)**
- *Why*: one auditable, revertable record of every mutation (APPROVE, DO, BDC, MANUAL, ADHOC_CLOSE, HOLD_CLEAR, HOLD_REVISE).
- *How*: `log_operation` / `log_operation_upsert` write the op with a payload carrying per-row pre-images. `revert_operation` dispatches to `_revert_{bdc,do,manual,approve,adhoc_close,hold_clear,hold_revise}`, undoing the exact rows/qtys and re-syncing MSA.
- *Validate-before-revert gate*: `preview_revert` runs the op-specific `_check_*_revert` freshness check first — e.g. a DO revert is blocked if newer downstream activity would be corrupted; an approve revert deletes its PEND_ALC rows, applies the `−1` MSA/grid delta, and restores hold tracking from the snapshot. Revert is refused (with reasons) if the check fails, rather than silently corrupting state.

> Note on `alloc_queue.py` / `alloc_cancellation.py`: these back the **parallel MAJ_CAT allocation queue** and the "Cancel Batch / Kill Job" controls used during *listing generate* (not the PEND ledger itself). `ARS_ALLOC_MAJCAT_QUEUE` rows move `PENDING → IN_PROGRESS → DONE/FAILED`, or `→ CANCELLED` (terminal) on user cancel; `CANCELLED` can never be resurrected by a late worker. They are listed here for completeness but are upstream of pending.

### Formulas

```
# Persisted computed column
PEND_QTY = ALLOC_QTY - DO_QTY            # >0 = still pending; 0 = fully shipped

# Close rule
IS_CLOSED = 1   when DO_QTY >= ALLOC_QTY  (PEND_QTY <= 0)

# MSA feedback (Step 9) — recomputed inside every pend/hold delta UPDATE
FNL_Q = max(STK_QTY - PEND_QTY - HOLD_QTY, 0)

# DO draw-down (FIFO) — running-sum window over open rows, APPROVED_AT ASC, ID ASC
apply_qty(row) = min(remaining_do_qty_for_key, row.PEND_QTY)
DO_QTY += apply_qty

# BDC eligibility
eligible = IS_CLOSED = 0 AND PEND_QTY > 0
           AND NOT EXISTS (open ARS_BDC_HISTORY for (RDC, ST_CD, ARTICLE))

# BDC audit (not used by MSA)
BDC_QTY += stamped_qty ; LAST_BDC_AT = now
```

### Key columns (`ARS_PEND_ALC`)

| Column | Meaning | Formula / source |
|---|---|---|
| `SESSION_ID` | Approve session (or `MANUAL-…`) | approve / manual upload |
| `RDC` | Source warehouse (drives MSA) | store master via `WERKS`; falls back to `WERKS` |
| `ST_CD` | Destination store | `ARS_ALLOC_HISTORY.WERKS` |
| `ARTICLE_NUMBER` | Variant article | alloc history `VAR_ART` |
| `MAJ_CAT`, `GEN_ART_NUMBER`, `CLR` | Category / generic / colour | alloc history |
| `ALLOC_MODE` | RL/TBC/TBL/AUTO | `MAX(OPT_TYPE)` from listing working history |
| `ALLOC_TYPE` | Typed pool (`FRESH`/`GRT`) | alloc history |
| `SOURCE` | `AUTO` or `MANUAL` | write path |
| `ALLOC_QTY` | Approved qty | `SUM(alloc)` at the grain |
| `DO_QTY` | Shipped-confirmed qty | incremented by DO upload |
| `PEND_QTY` | Still pending | **persisted** `ALLOC_QTY − DO_QTY` |
| `BDC_QTY` | Cumulative BDC-sent (audit) | `stamp_bdc_qty` |
| `IS_CLOSED` | 1 when fully shipped/abandoned | DO draw-down or adhoc close |
| `LAST_BDC_AT` / `LAST_DO_AT` | Freshness stamps for revert gates | BDC / DO ops |
| `REMARKS` | Adhoc note (`ADHOC: <reason>`) | adhoc close |

## Recorded rules
<!-- ars_flow appends dated bullets below. One rule per line. -->
- 2026-07-18 — **Dispatch-control exclusions in Generate BDC.** Three tables suppress BDC so controlled stock is never dispatched (stays HELD as open pending; MSA still nets it): `ARS_HOLD_ARTICLE_BDC(GEN_ART_NUMBER, CLR)`, `ARS_DIVISION_DELETE_BDC(STORE, DIV)` — DIV resolved from `MAJ_CAT` via `ARS_MSA_GEN_ART` — and `ARS_DIVISION_DELETE_ON_MAJ_CAT_BDC(STORE, MAJ_CAT)`. Applied as correlated `NOT EXISTS` in `/bdc-generate`, `/bdc-generate-async`, and `/bdc-preview` (`pend_alc.py`, constants `_MATCH_ARTICLE_HOLD/_MATCH_DIV_DELETE/_MATCH_STORE_MAJCAT` + `_existing_dispatch_controls`); missing tables degrade to "no exclusion" (never 500). Migration `022`: renamed `ARS_DIVISION_DELETE_BDC.STATUS`→`DIV` and stripped `-DEL` (`KIDS-DEL`→`KIDS`). New `GET /pend-alc/dispatch-gap` (+`/export`) surfaces the held lines with per-rule `blocked_by`; shown in a **Dispatch Control — Held from BDC** section on `PendAlcRecoPage` (3 tiles + detail grid + Excel). Also fixed the legacy file-based `bdc.py` `_process_bdc` which had the same 3 filters but referenced non-existent columns `GEN_ART_CLR`/`MAJCAT` (→ crash once tables had rows) and hardcoded `KIDS` — now reads `GEN_ART_NUMBER`/`MAJ_CAT`/`DIV`. Owned by `ars_flow`.
- 2026-06-13 — `ARS_PEND_ALC` has NO unique constraint on the logical grain (only PK on `ID`). `write_pend_alc` enforces it with `NOT EXISTS`; `write_manual_pend_alc` does not. Why: duplicate manual rows for the same key silently double-count after `apply_pend_alc_delta` aggregates by `(rdc, art)`.
- 2026-06-13 — Adhoc close / BDC-history cancel treat blank `ST_CD` as any-store and `do_qty=0` as a global cancel for the `(RDC, ART)`. The `/close-rows` API guards blank-ST_CD with `confirm_close_all_stores`, but the service layer does not. Why: footgun on copy-pasted CSVs.
- 2026-06-13 — `apply_do_deductions` FIFO partition is `(RDC, ISNULL(ST_CD,''), ARTICLE)` — it excludes `ALLOC_MODE`. A DO can settle an RL row with a TBC DO (and vice versa), silently re-categorising the lifecycle.
- 2026-07-08 — Adhoc close NOW calls `bootstrap_msa_pend_sync` after closing (non-fatal on failure). MSA `PEND_QTY` no longer stays inflated until the next Generate. (Supersedes the earlier "adhoc close skips MSA sync" note.)
- 2026-07-09 — DO upload never touches MSA `STK_QTY` (daily snapshot; next full MSA reconciles) — it only draws down `DO_QTY`/`PEND_QTY`. Touching `FNL_Q` here would over-state the pool because shipped stock left the WH but `STK_QTY` wasn't re-read. see also: hold.md
- 2026-07-14 — Approve is ONCE-ONLY per session. `approve_parked` now gates on an active (non-reverted) `APPROVE` row in `ARS_PEND_ALC_OPERATIONS` (`OP_KEY`=SESSION_ID) BEFORE promoting/logging — a second click (any user) returns `{already_approved:true, approved_by, approved_at}` and logs nothing. The old gate required ALL snapshot targets to have history rows, which never held when a target was parked empty (e.g. `ARS_MSA_TOTAL` on a FRESH run), so duplicates fell through (observed: session `20260713_164356_383`, two APPROVE ops 35s apart, only one reverted → history empty). Reverting an APPROVE now clears EVERY active APPROVE op for that session (not just the clicked one) so the op log can't keep a stale "active" row after the data is demoted.
- 2026-07-16 — `write_pend_alc` (the `has_lwh` branch) now joins `ARS_ALLOC_HISTORY` H → `ARS_LISTING_WORKING_HISTORY` W on `MAJ_CAT` as well as `(SESSION_ID, WERKS, GEN_ART_NUMBER, CLR)`, and `MAJ_CAT` is a real `GROUP BY` key (was `MAX(H.MAJ_CAT)`). Why: the join omitted MAJ_CAT, so an article/colour legitimately listed under two MAJ_CATs (e.g. `1130113366/BLK` as `JB_SHIRT_HS` + `YB_SHIRT_HS`) matched two W rows and its `ALLOC_QTY` was fanned out — either a phantom extra `ALLOC_MODE` row (different OPT_TYPE) or a doubled qty (same OPT_TYPE). This inflated `SUM(ARS_PEND_ALC.ALLOC_QTY)` above `SUM(ARS_ALLOC_HISTORY.ALLOC_QTY)` at approve→save (observed: session `20260715_150042_931`, hist 452807 vs pend 452812, +5 across 3 groups). Fix aligns the pending grain with OPT-uniqueness `(WERKS, MAJ_CAT, GEN_ART, CLR)`. Corrects future approves only — already-written phantom/doubled rows need a one-off cleanup. Fallback (`else`) branch has no W join, so it was never affected.
