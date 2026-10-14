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
| `ARS_ALLOC_RDC_SPLIT_HISTORY` (else `ARS_ALLOC_RDC_SPLIT_PARKED`) | Per-warehouse split of an All-RDCs line — first source of `SRC_RDC` on approve | split rows per source warehouse |
| `ARS_PEND_ALC` | Pending ledger (created & maintained here) | `SESSION_ID × SRC_RDC × ST_CD × ARTICLE_NUMBER × ALLOC_MODE` |
| `ARS_BDC_HISTORY` | Audit of every BDC line sent to SAP | one row per `(SRC_RDC, ST_CD, ARTICLE)` per BDC event |
| `ARS_PEND_ALC_OPERATIONS` | Operations log (all mutations, revert payloads) | one row per operation |
| `ARS_STORE_BDC_SCHEDULE` | Which stores' BDC runs on which weekday | one row per store |
| `Master_ALC_INPUT_ST_MASTER` | Maps store → the store's own RDC (fills the ledgers' `RDC`; late fallback for `SRC_RDC`) | one row per store |
| `ARS_MSA_TOTAL / VAR_ART / GEN_ART` | Consume `PEND_QTY` (by the ledger's `SRC_RDC`) into `FNL_Q` | (RDC, ARTICLE) |

`ARS_PEND_ALC` key columns: `SRC_RDC` = **dispatch warehouse** — where the qty ships from (drives the MSA deduction, the BDC `VENDOR` and DO matching); `RDC` = the **destination store's own warehouse** (store master by `ST_CD`/`WERKS`, descriptive only); `ST_CD` = **destination store** (traceability). Every reader takes the dispatch warehouse as `COALESCE(NULLIF(SRC_RDC,''), RDC)` (`pend_alc_service.src_rdc_sql`) — exact for rows written before `SRC_RDC` existed or by a server still on old code, whose `RDC` held the dispatch warehouse. `ARS_BDC_HISTORY` carries the same pair: `SRC_RDC` = `VENDOR`, `RDC` = receiving store's own warehouse. `PEND_QTY = ALLOC_QTY − DO_QTY` is a **persisted computed column**. `BDC_QTY` is cumulative-qty-sent, audit only (never used by MSA).

### Rules & invariants

- **PEND feeds back into MSA**: `FNL_Q = max(STK_QTY − PEND_QTY − HOLD_QTY, 0)`, recomputed inside every MSA-adjusting UPDATE. Open pending rows also anchor the MSA universe (Step 6, by `SRC_RDC`) so a row always exists to land the deduction on.
- **`SRC_RDC` ships, `RDC` describes, `ST_CD` receives**: `SRC_RDC` is the ship-from warehouse; `RDC` is the destination store's own warehouse (descriptive only — the two differ whenever stock ships from a warehouse other than the store's own); `ST_CD` (= `WERKS` in alloc history) is the ship-to store. All three are stored; MSA, BDC, DO and adhoc close key on `COALESCE(NULLIF(SRC_RDC,''), RDC)`, never on `RDC` alone. A split (All RDCs `'MULTI'`) line yields one pending row per source warehouse — same `RDC`, different `SRC_RDC`.
- **Only one row *closes* pending when fully covered**: `IS_CLOSED=1` when `DO_QTY ≥ ALLOC_QTY` (i.e. `PEND_QTY` hits 0).
- **DO upload does NOT touch MSA `STK_QTY`**: `STK_QTY` is a daily snapshot; the next full MSA run reconciles physical stock. DO only draws down `DO_QTY`/`PEND_QTY`.
- **No unique constraint** exists on the logical grain (only PK on `ID`). `write_pend_alc` enforces the grain with `NOT EXISTS`; `write_manual_pend_alc` does **not** — duplicate manual rows for one key silently double-count.
- **Blank `ST_CD` = wildcard** (every store served from that `(SRC_RDC, ARTICLE)`) in adhoc close and BDC-history cancel. Guarded in the API (`confirm_close_all_stores`) but not in the service.
- **Every mutation is logged and revertable** via `ARS_PEND_ALC_OPERATIONS`; revert is gated by per-op `_check_*_revert` freshness checks.

### Lifecycle & operations — how and why

Row lifecycle: `PENDING (IS_CLOSED=0) → [BDC stamped] → [DO drawn down] → CLOSED (IS_CLOSED=1) | cancelled/reverted`.

**1. Maintain `ARS_PEND_ALC` — auto write on Approve (`write_pend_alc`)**
- *Why*: turn an approved session's shipments into pending so MSA nets them out next cycle.
- *How*: called inside `approve_parked` (under the per-session applock). Reads `ARS_ALLOC_HISTORY` for the `SESSION_ID`, joins store master to resolve the store's `RDC` from `WERKS`, and `ARS_LISTING_WORKING_HISTORY` to carry `OPT_TYPE` into `ALLOC_MODE`. `SRC_RDC` is resolved per line, first hit wins:
  1. `ARS_ALLOC_RDC_SPLIT_HISTORY` rows for the session, joined on `WERKS + MAJ_CAT + VAR_ART` (`MAJ_CAT` stops an article listed under two MAJ_CATs taking the other's qty);
  2. `ARS_ALLOC_RDC_SPLIT_PARKED` rows, when the session has none in history (an approve that never promoted the split triple);
  3. `ARS_ALLOC_HISTORY.SRC_RDC`, unless `'MULTI'` or blank;
  4. the RDC the run allocated from (`ARS_ALLOC_HISTORY.RDC` — Own and Cross runs consume the store's own warehouse pool: Stage B joins `L.RDC = V.RDC` outside central pooling; `cross_from` only filters the option set);
  5. store master, then `WERKS`.

  `RDC` = store master RDC (fallback run RDC, then `WERKS`). A `MULTI` line with no split rows logs a warning and falls back to the run RDC. One row per `(SESSION, SRC_RDC, ST_CD, ARTICLE, ALLOC_MODE)`, `ALLOC_QTY = SUM(alloc)`. Then `apply_pend_alc_delta_by_session(sign=+1)` immediately applies the +PEND delta to MSA (keyed on `SRC_RDC`) + grid so the next run sees reduced `FNL_Q` without a rebuild.
- *Validate-before-create gate*: **idempotent** via `NOT EXISTS` on the grain + the session applock, so a double-Approve cannot write pending twice. Rows with `ALLOC_QTY ≤ 0` are excluded.

**2. Maintain `ARS_PEND_ALC` — Manual upload (`write_manual_pend_alc`, `POST /manual-upload`)**
- *Why*: inject pending the auto-flow didn't produce (manual DO plans, corrections, cross-loads).
- *How*: direct `fast_executemany` INSERT tagged `SOURCE='MANUAL'`; multi-chunk uploads share one `session_id` so they roll up to one revertable ops-log entry. The uploaded warehouse (field `src_rdc`, old name `rdc` still accepted; template column `SRC_RDC`) is the dispatch warehouse → `SRC_RDC`; `RDC` is set from the store master by `ST_CD` (stays = `SRC_RDC` when `ST_CD` is blank or the store is unknown). `apply_pend_alc_delta` refreshes MSA `PEND_QTY/FNL_Q` for the affected keys (by `SRC_RDC`).
- *Validate-before-create gate*: **weak** — this is a known sharp edge. Negative `alloc_qty` is dropped (`>0` filter), but blank `src_rdc`/`article_number` and articles absent from `vw_master_product` are inserted and become **MSA orphans** (their delta UPDATE matches 0 rows). There is no grain de-dup, so duplicate manual rows double-count. Reviewers: validate the article exists and the key is unique before trusting a manual load.

**3. Daily DO Entry (`apply_do_deductions`, `POST /do-update` + `/do-update-async`)**
- *Why*: record what SAP actually shipped, so pending shrinks to reality and fully-shipped rows close.
- *How*: input `(src_rdc, article_number, do_qty, [st_cd], [do_number], [allocation_number])` — `src_rdc` (old name `rdc`, still accepted; template column `SRC_RDC`) is the **dispatch** warehouse: the SAP DO file's warehouse is the one that shipped (owner-confirmed 2026-10-14). Increments `DO_QTY` across open rows for the key; the FIFO partition, row matching, overflow report and BDC auto-close all key on `SRC_RDC`. `deduction_method`:
  - `FIFO` (default) — open rows drain `APPROVED_AT ASC, ID ASC`.
  - `SESSION_FIRST` — `target_session_id` rows drain first, then FIFO.
  - `SESSION_ONLY` — deduct only from `target_session_id`; excess surfaces in `overflow_rows`.
  A row hitting `PEND_QTY=0` sets `IS_CLOSED=1` and side-effect-closes its OPEN BDC history (`OPEN→CONFIRMED`, recorded as `auto_history_closes`). `allocation_number` resolves `ST_CD` from BDC history (BDC wins over user-supplied on conflict).
- *Validate-before-create gate*: `_validate_deduction_method` — **`target_session_id` is required** whenever method ≠ FIFO (else `ValueError`/400). Rows must be **open** to receive a deduction; a DO for a key with no open pending lands nowhere and its qty shows in `overflow_rows` (audit-only, not auto-corrected because the DO already happened in SAP). Does **not** touch MSA `STK_QTY` (see invariant). Logged `OP_TYPE='DO'`; chunk 1 (`is_first_chunk`) INSERTs the ops row, chunks 2..N MERGE their `pend_updates`/`history_updates` into it so the whole upload reverts as one unit.

**4. Adhoc correction / adhoc close (`apply_adhoc_close`, `POST /close-rows` + `/close-rows-file`)**
- *Why*: abandon a BDC that should not ship (classic case: bot issued a BDC for an article with no MSA stock) and release the in-flight lock so a corrected BDC can flow.
- *How*: per `(SRC_RDC, ST_CD, ARTICLE)` (upload template column `SRC_RDC`; `RDC` still accepted) — flip open `ARS_PEND_ALC` rows to `IS_CLOSED=1` (prefix `REMARKS` with `ADHOC: <reason>`), and flip still-OPEN `ARS_BDC_HISTORY` rows to `STATUS='CANCELLED'` (releasing `_NO_OPEN_BDC_PREDICATE`). Pre-images captured for revert. Now calls `bootstrap_msa_pend_sync` after closing so MSA `PEND_QTY` no longer stays inflated until the next Generate.
- *Validate-before-create gate*: **reason required**; and a **blank-`ST_CD` wildcard is refused** (400 with the offending keys) unless `confirm_close_all_stores=true` — a blank `ST_CD` closes EVERY store served from that `SRC_RDC` + article, a footgun when a column was left empty by accident. Logged `OP_TYPE='ADHOC_CLOSE'`.

**5. Create BDC (`POST /bdc-generate` + `/bdc-generate-async`, `stamp_bdc_qty` + `insert_bdc_history`)**
- *Why*: produce the SAP-ready 9-column allocation file and record what was sent.
- *How*: store selection priority = explicit `st_cd_list` > `target_date` schedule lookup > all stores with open pending. Phase 1 read-only aggregation (`WITH (NOLOCK)`) of open pending by `(SRC_RDC, ST_CD, ARTICLE, MAJ_CAT)`; one allocation number per `(FY, SRC_RDC)`; `VENDOR = SRC_RDC`. Phase 2 short write txn: `stamp_bdc_qty` (matches on `SRC_RDC`) bumps `BDC_QTY` + `LAST_BDC_AT`, `insert_bdc_history` appends one OPEN history row per line with `SRC_RDC = VENDOR` and `RDC` = the receiving store's own warehouse (reading back by `ID > prev_max_id` so it never grabs a prior op's rows). The `rdc` query param on `bdc-preview` / `bdc-generate` / `dispatch-gap` / `bdc-history*` is the dispatch warehouse. Excel columns: Serial No, Allocation Date, Allocation Number, VENDOR, MATERIAL NO, BDC-QTY, RECEIVING STORE, Picking Date, Remark.
- *Validate-before-create gate*: the candidate filter is `IS_CLOSED=0 AND PEND_QTY>0 AND _NO_OPEN_BDC_PREDICATE` — a row is only eligible if it has pending qty **and no already-OPEN BDC history** for the same `(SRC_RDC, ST_CD, ARTICLE)`, so the same pending is never double-BDC'd. `404` if nothing qualifies; `400` if `target_date` resolves to zero scheduled stores.

**6. Reconciliation (`GET /reco`, `/reco-export`, `/reco-summary`, `/pend-vs-msa-gap`)**
- *Why*: show planned-vs-shipped and surface stuck/over-shipped pending; verify MSA agrees with the ledger.
- *How*: paged `reco` joins each pending row to its latest BDC (`ALLOCATION_NUMBER`, `STATUS`, `DO_RECEIVED`); no BDC ⇒ `BDC_STATUS='NEVER_SENT'`. Filters by date/warehouse/MAJ_CAT/mode/source/closed/session, per-column multi-value filters, aging bands. The page's top warehouse filter is the **dispatch** warehouse (sent as `src_rdc` to `/reco` and its exports, as `rdc` to the BDC / gap endpoints); the grids show **SRC RDC** and **STORE RDC** columns; `/reco` and `/detail` accept `rdc` (store warehouse) and `src_rdc` (dispatch) as CSV multi-value. `/reco-summary` `by_rdc` = dispatch warehouse, `by_store_rdc` = store warehouse. `pend-vs-msa-gap` cross-checks `SUM(PEND_QTY)` in the ledger, grouped by `SRC_RDC`, against MSA `PEND_QTY` per `(RDC, ARTICLE)`.
- *Validate-before-create (close) gate*: reconciliation is where **shipped vs planned** is confirmed before a pending is considered settled — a row only closes when `DO_QTY ≥ ALLOC_QTY`; a partial DO leaves it open with the residual visible here. Use reco to confirm the gap is real before an adhoc close.

**7. Open BDC report (`GET /bdc-history`, `/bdc-history-allocations`, `/bdc-history-redownload`, `/bdc-history-export`)**
- *Why*: answer "every BDC ever sent for this store/article and how much SAP confirmed", and let ops re-download a prior BDC file.
- *How*: reads `ARS_BDC_HISTORY` (`STATUS ∈ OPEN/CLOSED_PARTIAL/CONFIRMED/CANCELLED`, `DO_RECEIVED` running total). `bdc-history-redownload` regenerates the exact Excel for a past `ALLOCATION_NUMBER`. `bdc-recover-orphans` / `close-orphan-bdc-history` repair rows stamped `BDC_QTY>0` with no matching history. Orphan recovery, close-orphan-history and re-download all match on `SRC_RDC`.
- *Cancel gate*: `update_bdc_history_with_do` treats `do_qty=0` as a CANCEL; with only `(src_rdc, art)` and `do_qty=0` the cancel scope is **global** for that key — confirm the allocation number before a zero-qty cancel.

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

# Dispatch warehouse — the key every reader uses (pend_alc_service.src_rdc_sql)
dispatch_wh = COALESCE(NULLIF(SRC_RDC,''), RDC)

# MSA feedback (Step 9) — recomputed inside every pend/hold delta UPDATE
PEND_QTY(MSA RDC, ARTICLE) = SUM(open PEND_QTY) grouped by (dispatch_wh, ARTICLE_NUMBER)
FNL_Q = max(STK_QTY - PEND_QTY - HOLD_QTY, 0)

# DO draw-down (FIFO) — running-sum window over open rows, APPROVED_AT ASC, ID ASC
apply_qty(row) = min(remaining_do_qty_for_key, row.PEND_QTY)
DO_QTY += apply_qty

# BDC eligibility
eligible = IS_CLOSED = 0 AND PEND_QTY > 0
           AND NOT EXISTS (open ARS_BDC_HISTORY for (SRC_RDC, ST_CD, ARTICLE))

# BDC audit (not used by MSA)
BDC_QTY += stamped_qty ; LAST_BDC_AT = now
```

### Key columns (`ARS_PEND_ALC`)

| Column | Meaning | Formula / source |
|---|---|---|
| `SESSION_ID` | Approve session (or `MANUAL-…`) | approve / manual upload |
| `SRC_RDC` | **Dispatch warehouse** — ships the qty (drives MSA deduction, BDC `VENDOR`, DO matching, open-BDC lock) | approve: split history → split parked → `ARS_ALLOC_HISTORY.SRC_RDC` unless `MULTI`/blank → run `RDC` → store master → `WERKS`; manual: uploaded `src_rdc`; 1SZ: the line's `SRC_RDC` |
| `RDC` | Destination store's own warehouse (descriptive only) | store master by `ST_CD`/`WERKS`; falls back to the run RDC, then `WERKS` (manual: = `SRC_RDC` when `ST_CD` is blank/unknown) |
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
- 2026-10-12 — **1SZ ALC sessions post here on approve (SOURCE '1SZ').** `sz1_approve_service.approve` writes one ARS_PEND_ALC row per ship-from RDC (= the 1SZ line's SRC_RDC, not the store's RDC) × ST_CD × ARTICLE × ALLOC_TYPE with SESSION_ID `1SZ-<sz1 session>`, SOURCE / ALLOC_MODE `1SZ`, idempotent on the grain; then `apply_pend_alc_delta_by_session(+1)` and an `APPROVE_1SZ` operation, all in one transaction (Pend ALC helpers get a no-commit connection wrapper so they don't commit half-way). Revert goes through this module's dispatcher (`_check_approve_1sz_revert` / `_revert_approve_1sz`): refused once any row has BDC_QTY or DO_QTY; −1 delta, delete the rows, 1SZ session back to PARKED — no listing hold-tracking / history steps. From then on BDC, DO, reco and adhoc close treat 1SZ rows like any other pending row; Reco / Pending pages list '1SZ' in their MODE / SOURCE filters. Previewing or reverting an APPROVE_1SZ operation from the Operations Log (`preview-revert`, `revert`, `revert-async`) requires ACT_1SZ_APPROVE, the same as on the 1SZ screen (SUPER_ADMIN passes); other operation types still need only a login.
- 2026-10-14 — **`RDC` = destination store's own warehouse, `SRC_RDC` = dispatch warehouse, on `ARS_PEND_ALC` and `ARS_BDC_HISTORY`** (code on branch `for_merge`, not yet deployed or migrated). `RDC` is now descriptive only (`Master_ALC_INPUT_ST_MASTER.RDC` by ST_CD/WERKS); `SRC_RDC` is the warehouse the qty ships from, and every reader uses `COALESCE(NULLIF(SRC_RDC,''), RDC)` (`pend_alc_service.src_rdc_sql`) — exact for rows written before `SRC_RDC` or by a server still on old code. **Reverses commit `8274ede` (2026-10-05)**, which had made `RDC` itself the dispatch warehouse; the earlier 2026-09-22 `SRC_RDC` (commit `32f0b96`) was written but never read, so MSA had debited the store's warehouse (session `20261003_130926_698`: 7,653 of 12,730 pcs wrong). Grain is `(SESSION_ID, SRC_RDC, ST_CD, ARTICLE_NUMBER, ALLOC_MODE)`; a split (`'MULTI'`) line gives one row per source warehouse. `write_pend_alc` resolves `SRC_RDC` per line (FSD §1): split history on `WERKS + MAJ_CAT + VAR_ART` → split parked when the session has none in history (session `20261010_163031_450`: 7,892 open rows / 40,890 pcs had landed on the store's warehouse) → `ARS_ALLOC_HISTORY.SRC_RDC` unless `MULTI`/blank → run RDC → store master → `WERKS`. Verified read-only: `20261013_172226_383` SRC_RDC totals DH24 133,680 / DW01 109,226 = split table exactly; `20261010_163031_450` now DH24 185,794 / DW01 126,600 (= parked split) instead of 111,247 / 201,147. Manual upload, DO upload and adhoc close take the warehouse as `src_rdc` (old name `rdc` still accepted) = dispatch; BDC groups, numbers `(FY, SRC_RDC)`, `VENDOR`, the open-BDC lock, `stamp_bdc_qty`, orphan recovery and re-download all key on `SRC_RDC`; 1SZ approve writes `SRC_RDC` = the 1SZ line's `SRC_RDC`, `RDC` = store master (supersedes the "RDC = ship-from" wording of the 2026-10-12 bullet above). Where an earlier bullet keys pending / BDC / DO on `RDC` (the `(rdc, art)` delta, the FIFO partition, the blank-ST_CD wildcard, the zero-qty cancel), read `SRC_RDC`. Also in this change: dead `adjust_msa_after_pend_insert` removed (no callers — manual upload uses `apply_pend_alc_delta`); `_revert_manual` now reverts with `ALLOC_TYPE`, so a GRT row is reverted against the GRT MSA row; `/pend-vs-msa-gap` and its export no longer fail with "Invalid object name '#gap_…'" when an RDC/MAJ_CAT filter is set (parameterised `SELECT INTO #temp` under `sp_prepexec`; filters are now inlined as escaped literals); the `/detail` RDC column filter and `/reco` multi-select now work. **Migration** `backend/scripts/041_src_rdc_concept.py` (dry run by default): `--phase add --yes` (add nullable `SRC_RDC`, backfill `SRC_RDC = RDC` on all rows, build `IX_ARS_PEND_ALC_open_src` and `IX_ARS_BDC_HISTORY_store_art`; safe while old code runs) → deploy the new code → `--phase restamp --yes` (re-run the backfill, then `RDC` = store master RDC; never touches `SRC_RDC`; `--resync-msa` optional) → `--phase verify`. **Never restamp before the new code is live.** `scripts/drop_src_rdc_columns.py` is retired (deleted). Open: the legacy `/bdc/*` endpoints' `_rebuild_pend_alc` DROP TABLE `ARS_pend_alc` risk is a separate task. see also: msa.md, hold.md, sz1_alloc.md, listing.md
