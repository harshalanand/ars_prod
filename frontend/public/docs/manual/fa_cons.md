# FA & CONS — ARS Manual

*Project-Store Allocation (FA) & Consumables Allocation (CONS)*

> **Status:** Partially built. **DONE (2026-07-22):** the data foundation — per-stream
> SLOC selection (`ARS_FACONS_SLOC_SETTINGS`), the dedicated FA/CONS MSA + store-stock
> calc (`ARS_FACONS_STOCK` / `ARS_FACONS_MSA` / `ARS_FACONS_STOCK_SEQUENCE`), and the
> MBQ Master (`ARS_FACONS_MBQ`, 3-col upload + live store-master enrichment). Backend:
> `services/facons_stock_service.py`, `services/facons_mbq_service.py`,
> `endpoints/facons.py` (prefix `/fa-cons`), migration `scripts/025_facons_foundation.sql`.
> Frontend: `FaConsMbqMasterPage`, `FaConsSlocSettingsPage`, `FaConsStockPage`.
> **DEFERRED** (still placeholders): the allocation engine (OPT_MBQ injection), Gap
> Report ATR, pend_alc §F wiring. This dossier is the canonical spec — build to it.
>
> **Source SOP:** `\\file\0-V2\04-DEPARTMENT\07-REPLENISHMENT\12-PROJECT-TRACKER\NT ARS\SOP FOR PROJECT STORES & CONSUMABLES ALLOCATION REVISED.docx`.
> The `→` annotations in that document are the revised acceptance criteria and
> are folded into the FSD below.

---

## BRD — Why this exists

Two allocations run **outside** the main ARS pipeline today, on a hand-maintained
Excel SOP. FA & CONS brings both into ARS so they are automatic, auditable and
system-driven.

### 1. FA — Project Store Allocation (new / UPC project stores)

When a new store (or a re-laid-out "UPC" store) opens, the **project team (Aryan)**
issues a **new-store component master** that lists, per store, the reference
articles to stock and a **minimum-buy quantity (MBQ)** for each, plus a
**priority-wise dispatch order**. Replenishment loads this into a sheet and, each
day, allocates the **balance requirement** against whatever stock is available in
the DC and the store — repeating daily until the balance requirement against MBQ
reaches **zero**.

On top of the base master, the project team raises **additional requirements** for
UPC stores when reality diverges from plan:

- **Layout change** — the display plan changed → the MBQ must be **updated first**,
  then dispatch against the revised MBQ.
- **Damage received** — damaged stock must be **moved to the damage SLOC first** so
  system stock reflects reality, then allocate against the corrected stock.
- **Short receipt vs invoice** — the shortfall is **moved to the short/excess SLOC**
  so system stock updates and ARS dispatches the balance.
- **Repeated MBQ churn** — layout changes push MBQ up and down often, so a **Gap
  Report** must reconcile MBQ against what has already gone out on an alternate-day
  cadence.

**Pain today:** manual sheets, no control over how often MBQ is changed or why, and
a Gap Report that is worked by hand.

### 2. CONS — Consumables Allocation (pan-India stores)

Consumables (carry bags, tags, and similar trading goods) are replenished to all
stores. MBQ was originally derived by analogy (LYSM carry-bag sale → TYCM MBQ) and
supplied as a **month-wise sheet with cover days**, allocated **twice a week** by
hand.

**Pain today:** static, manually-supplied MBQ and a twice-weekly manual run.

### Revised acceptance criteria (the `→` notes, consolidated)

1. Consumables MBQ is **auto-computed** (see formula in FSD) and **auto-updated**,
   not supplied by sheet.
2. Fresh stock is **auto-collected from SQL** daily — no manual stock pull.
3. Allocation runs **daily inside ARS** for both streams (consumables was twice-weekly).
4. **All segments** must flow, not only APP and GM.
5. MBQ changes are **audit-controlled** — how many times an MBQ changed, and **why**
   (mandatory reason), must be visible.
6. A **Gap Report** with a **final ATR** recommendation is a first-class deliverable.
7. **Manual stock intervention is NOT accepted** — allocation is system-only; there
   is no manual override when physical and system stock disagree.

---

## FSD — How it will work

FA & CONS reuses the existing ARS engine wherever possible. The engine reads MBQ,
requirement and store priority purely from the **listing tables**, so a new stream
is, mechanically, "a different way of populating `OPT_MBQ` / `ST_RANK` before the
Stage A–D waterfall runs." No engine rewrite is required.

### A0. MBQ Master — single upload, auto-segregated by DIV (BUILT)

The MBQ Master is **one upload** (`ST_CD · REF_ART · MBQ_Q`), **not** per-stream. The
system **auto-segregates each row into FA vs CONS by the article's `DIV`** (looked up
from `VW_MASTER_PRODUCT`): **`DIV='CO'` → CONS** (consumables), **`DIV='FA'` → FA**
(fixed assets); unknown → FA. The derived stream is stored on `ARS_FACONS_MBQ.stream`
and shown as a badge; the page has an All / FA / CONS view filter only.
`reclassify_streams()` re-derives the stream of existing rows (used once to split the
initial 342-row load into 228 FA / 114 CONS). **Terminology: FA = Fixed Assets
(project store), CO/CONS = Consumables.**

**Upload = confirm-before-commit.** `POST /fa-cons/mbq/upload?dry_run=true` runs the
full ingest (auto-segregate + classify each row CREATE/UPDATE/UNCHANGED) **without
writing** and returns the breakdown + per-row `details`; the UI shows a review modal
(count tiles + detail table) and only `dry_run=false` (Confirm) commits. Safety against
accidental uploads. A global search box filters the master grid across all columns.

**Change `reason` — MANDATORY, batch, captured at upload confirm.** `reason` is the
*why* of a change event (distinct from `remarks`, the persistent note stored on the MBQ
row). It is a **batch-level, required** value: the upload confirm modal has a "Reason for
this change" box (`POST …/upload?reason=…`), stamped onto **every** CREATE/UPDATE event
that upload logs — so Change Review answers "why did these rows change" without a per-row
prompt. `ingest_upload(reason)` → `save_mbq(reason)` → `_log_event(reason)`. **A commit
(`dry_run=false`) with a blank reason is rejected `400 "Reason for this change is
mandatory"`** — the guard runs *before* any write; the UI mirrors it (Confirm disabled +
inline toast until a reason is entered). Dry-run preview ignores reason (nothing is
written). The edit endpoint (`POST /fa-cons/mbq`) also carries `reason` for one-off manual
corrections. *(Chosen 2026-07-23 over per-row inline-edit and a per-row REASON sheet
column, given the upload-driven workflow; made mandatory same day.)* **Deletes are also
reason-gated:** `DELETE /fa-cons/mbq/{id}?reason=…` rejects a blank reason `400 "Reason
for this deletion is mandatory"` (guard before the delete); the UI shows a delete-confirm
modal with a required reason box (Delete disabled until filled). The DELETE event carries
that reason (source `edit`, no session) and appears in Change Review.

**MBQ Change Review** — a second tab in MBQ Master (`GET /fa-cons/mbq/change-review`,
read-only over `ARS_FACONS_MBQ_HIST`). On-demand audit of MBQ changes for **a single
date** (`date_from=date_to`) or **a date range**, with optional `stream`/`action`/
`st_cd`/`ref_art`/`changed_by` filters (default = last 7 days). Returns `{items, summary}`
where **summary reconciles**: `total = created + updated + deleted + approved`, plus
distinct `stores`/`ref_arts`, `net_delta` (Σ new_mbq − old_mbq), and `shown`/`capped`
(TOP `limit`, default 2000). Presets: Today / Yesterday / 7 / 30 days. `.../change-review/
export` streams the same query as xlsx. Scope is **MBQ changes only** (not stock/SLOC).
The per-row change-history is the single-key drill-down: each MBQ Master row shows a
**count badge = number of change events** (from `hist_count` in `list_mbq`, a grouped
count over `ARS_FACONS_MBQ_HIST` by `st_cd, ref_art`) **only when > 0** (no icon, nothing
when 0); **hovering** the badge shows a floating preview (recent events: when · action ·
old→new MBQ, lazily fetched + cached per row); **clicking** opens the full history modal.
- **Interactive summary cards.** All 8 cards are clickable: the four action cards
  (Created/Changed/Deleted/Approved) toggle a **client-side** action filter on the table
  (an active-filter chip + "Clear filters" button appear); Total / Stores / Ref-articles /
  Net-Δ clear it. The cards themselves always show the **full** counts for the queried
  date-range (action filtering is client-side, so the cards stay a stable legend);
  **Export** mirrors the visible card filter (sends `action=…`).
- **Clear filters** resets the session + card + search + stream filters; **Refresh**
  reloads the latest data AND drops all those filters ("show all changes").
- Dates use the **local** calendar day (not `toISOString`, which is UTC and would drop
  today's events from the default range for IST users).
- **Session-wise review.** Every upload commit opens a **session** (batch) row in
  `ARS_FACONS_MBQ_SESSION` (source, filename, reason, per-action counts, user, time) and
  stamps its `id` on every change event it logs (`ARS_FACONS_MBQ_HIST.session_id`).
  Change Review has an **"Upload session"** dropdown (`GET /fa-cons/mbq/sessions`, recent
  50) — picking one scopes the whole review (table + reconciling cards + export) to that
  one upload, **ignoring the date window** (all of that batch's events, whenever). The
  table's **Session** column shows `#id`; clicking it jumps to that session. Manual edits
  (source `edit`) carry no session (`—`). Pre-session events show `—`.

### A. Master data

**`ARS_FACONS_MBQ`** *(new table — deferred)* — grain **`WERKS (ST_CD) × reference-article`**
(optionally `× MAJ_CAT × OPT_TYPE`). Columns (proposed):

| Column | Meaning |
|---|---|
| `STREAM` | `FA` (project store) or `CONS` (consumables) |
| `WERKS` / `ST_CD` | store |
| `REF_ARTICLE` (`GEN_ART_NUMBER` [, `CLR`]) | reference article the MBQ is for |
| `MAJ_CAT`, `OPT_TYPE` | resolved grain for the engine |
| `MBQ` | minimum buy quantity (FA: given by project team; CONS: auto-computed) |
| `PRIORITY` | priority-wise dispatch rank (FA) |
| `COVER_DAYS` | per-store cover days (CONS) |
| `REASON` | mandatory reason captured on every insert/change |
| `EFF_FROM` / `EFF_TO` | effective window |
| `SOURCE`, `UPDATED_BY`, `UPDATED_AT` | provenance |

> **Note:** an article-level `MANUAL_MBQ` column previously existed on the ART input
> masters but was **renamed to `MANUAL_DENSITY`** and dropped from the MAJ_CAT tables
> (`grid_calculations.py:1099-1116`). FA & CONS revives that intent as a dedicated
> master rather than a column, so it does not collide with density.

### B. FA — Project Store Allocation

> **Grain — MBQ at REF_ART, final allocation at ARTICLE_NUMBER.** FA MBQ is supplied
> **per `ST_CD` × `REF_ART`** (reference article). `REF_ART` is a real column on
> **`VW_MASTER_PRODUCT`** — a reference grouping *above* GEN_ART: ~819K of 3.6M master
> rows carry one (281K distinct), and **one `REF_ART` spans many `ARTICLE_NUMBER`s**
> (and many GEN_ARTs). `'NA'`, `'0'` and `''` mean *no reference* → fall back to the
> article's own `GEN_ART_NUMBER`.
>
> **Allocation calculation (the rule):**
> 1. Compute demand at the **`REF_ART`** grain: for `(ST_CD, REF_ART)` the requirement
>    is `MBQ_Q − store-stock(REF_ART)` (store stock summed over the FA store-scope SLOCs).
> 2. Resolve the `REF_ART` to its member `ARTICLE_NUMBER`s via
>    `VW_MASTER_PRODUCT` (all articles where `REF_ART` matches).
> 3. **Fill MAX-quantity article first within the REF_ART** — rank the member articles
>    by available DC-pool stock (`ARS_FACONS_MSA`, the MSA-scope SLOCs) descending and
>    allocate down the list until the REF_ART requirement is met (balance → 0).
> 4. The **final allocation quantity is stamped per `ARTICLE_NUMBER`** (the SAP-shippable
>    material), not at REF_ART.
>
> So the demand/budget is REF_ART-grained; only the final ship qty is exploded to
> `ARTICLE_NUMBER`. (This differs from the core ARS OPT→variant×size explode; FA does
> not split by size-contribution — it distributes the REF_ART requirement across its
> articles max-first.)

1. **Ingest** the project-team master into `ARS_FACONS_MBQ` (`STREAM='FA'`) — an
   upload page mirroring the UPC Store Tracking upload (see `upc_store_track_service.py`).
2. **MBQ injection seam:** in `listing.py`, immediately **after Part 4c**
   (`OPT_MBQ`/`OPT_REQ` computed, ~`listing.py:2199`) and **before** MJ_REQ
   derivation (~`listing.py:2432`), overwrite `OPT_MBQ` / `OPT_MBQ_WH` from the
   master where a row exists (keyed `WERKS × ref-article → GEN_ART/CLR`). Everything
   downstream recomputes unchanged:
   - `OPT_REQ = MAX(0, OPT_MBQ − STK_TTL)` (`listing.py:2196`)
   - `MJ_MBQ` (grid-merged, `listing.py:1901-1960`), `MJ_REQ` (`:2432-2479`)
   - the running-balance `_REM` loop — `rule_engine_new.py::_init_rem_columns`
     (`:1119`) seeds `MJ_REQ_REM` / `<grid>_REQ_REM`, and `_revalidate_after_band`
     (`:1168`) drains them after each OPT ships. **"Balance requirement till MBQ = 0"
     is exactly draining `MJ_REQ_REM` / `OPT_REQ` toward zero** — no new balance
     engine is needed.
3. **Priority injection:** mirror the existing **`MANUAL_ST_PRIORITY → ST_RANK`**
   override (`listing.py:2538-2702`, which already includes duplicate-priority
   validation) to pin the project team's dispatch order per MAJ_CAT.
4. **Balance-to-MBQ (the "till BAL REQ = 0" loop):** FA runs daily and each run must
   ship only the shortfall against MBQ, per **`ST_CD × ref-article`**. Two horizons:
   - *Within a run:* draining `OPT_REQ` / `MJ_REQ_REM` (§B.2) gives balance-vs-stock.
   - *Across runs:* `ARS_PEND_ALC` carries `ST_CD` + `GEN_ART_NUMBER` + `CLR`, so
     `SUM(PEND_QTY) GROUP BY ST_CD, GEN_ART_NUMBER[, CLR]` is the already-committed-
     not-yet-delivered qty; on MSA rebuild the per-`(RDC, ARTICLE_NUMBER)` readback
     (`msa_service._load_ars_pending` `:68-92`) deducts it from availability.
   - ⚠ **Gap:** `PEND_QTY` and the MSA readback only see **open** rows — once a line
     is DO'd and `IS_CLOSED=1` it drops out. "Balance vs MBQ" is *MBQ − cumulative
     **delivered***, which must include closed rows. So FA needs a dedicated
     **delivered-vs-MBQ** view: `MBQ − (Σ DO_QTY over open+closed pend for that
     ST_CD × ref-art)`, not just open `PEND_QTY`. Build this as the FA balance source
     rather than relying on the availability readback alone.
5. **Pool selection:** reuse `alloc_type ∈ {FRESH, GRT}` on `GenerateRequest`
   (`listing.py:202`) + `ARS_MSA_SLOC_SETTINGS` (`alloc_pool.py`). Configure the SOP
   SLOCs — `0001`, `0002`, `DC/HUB TO ST INT`, `DC/HUB TO ST PRD`,
   `DC TO ST PICKING PENDING`, `0014`, `0015`, `0016` — as FRESH/GRT there.
6. **UPC extra-req handlers** (layout change / damage / short receipt) are stock-
   correction workflows that shift qty to damage / short-excess SLOCs and re-run
   allocation on the corrected stock; the layout-change path updates MBQ first.

### B2. Stock foundation — dedicated FA/CONS MSA + store-stock calc (BUILT)

The two scopes are sourced from **different** stock tables, split by the per-stream,
per-scope **SLOC selection** in `ARS_FACONS_SLOC_SETTINGS` — **all segments** (no `SEG`
filter). Dedicated calc (`services/facons_stock_service.py`); core ARS MSA untouched.

- **Only DIV = FA / CO** (identified from `VW_MASTER_PRODUCT.DIV`, joined on
  `ARTICLE_NUMBER=MATNR`). Fixed default, overridable via `?divs=` / the Stock-page
  chips. All other divisions are out of scope.
- **SLOC config reuses the two LISTING tables** (no standalone table): additive
  columns `fa_role` / `co_role` (`STORE` | `DC` | NULL) + `facons_updated_by/at` on
  **`ARS_STORE_SLOC_SETTINGS`** and **`ARS_MSA_SLOC_SETTINGS`**. Listing's own columns
  (`status`/`kpi`/`sloc_type`/`is_active`) are read-only here and never written by
  FA/CONS. Per stream, `role=STORE`→store-stock bucket, `role=DC`→DC-pool bucket,
  NULL=off. The old `ARS_FACONS_SLOC_SETTINGS` was migrated (scope→role) and dropped.
- **Both scopes source `ET_STORE_STOCK`** — that is where FA/CO stock actually lives
  (`ET_MSA_STK`/`VW_ET_MSA_STK_WITH_MASTER` is **fashion-only** and carries *no* FA/CO,
  so it is unused for FA/CONS). Columns: `WERKS`(store), `RDC`(dc), `MATNR`, `SLOC`, qty
  `PARTICULARS_VALUE`. GEN_ART/CLR/MAJ_CAT resolved via `VW_MASTER_PRODUCT`.
  - **STORE scope** groups by `WERKS` (store), summing the store-floor SLOCs
    (`0001, 0002, …`) tagged KPI=STK.
  - **MSA / DC-pool scope** groups by `RDC` (warehouse), summing the DC-side SLOCs
    (`DW01_PRD_QTY, DW01_STO_QTY_Q, HUB_PRD_Q, DH24_*_QTY_Q, …`) tagged KPI=STK.
  - Both discover the SAME SLOC universe (ET_STORE_STOCK); the planner activates
    store-floor SLOCs under STORE and DC SLOCs under MSA. "Consider FA/CO stock
    wherever available."
- **`kpi` is FUNCTIONAL, like Grid Builder.** Each active SLOC is bucketed by its KPI;
  the **stock total sums only SLOCs whose KPI = `STK`** (blank treated as STK for
  back-compat). Active SLOCs with any other KPI (`INT`/`PRD`/`STO`/`PEND`/`EXCL`/…) are
  kept but **excluded** from the total (mirrors grid_builder's `stk_slocs = KPI='STK'`
  → `STK_TTL`). So inclusion needs **Active + KPI∈{STK,blank}**; the calc returns the
  STK SLOCs used and the excluded (active non-STK) ones. UI offers the standard KPI
  values as a datalist plus free text.
- **UI:** SLOC Settings is a two-column (STORE | MSA) layout with per-card search,
  active counts, All/None bulk toggles. MBQ Master is an AG-Grid (sortable headers +
  per-column filters + pagination); Stream/Source are not shown as columns; each row has
  a change-history view. Every MBQ create/modify/delete is logged to
  `ARS_FACONS_MBQ_HIST` (`GET /fa-cons/mbq/history`); upload is an upsert.

- **SLOC selection** is per `(stream ∈ {FA,CONS}, scope ∈ {STORE, MSA})`, each SLOC
  `is_active` toggle + free-text `kpi` label. `Sync` discovers every SLOC in the stock
  view and inserts new ones **inactive** (never silently summed). Only certain default
  seeded: `V02_FRESH` → MSA scope active. The SOP's `INT/PRD/STO` are labels, not codes
  — the real SLOCs are `HUB_INTRA` (INT), `HUB_PRD`/`ST_PRD` (PRD), `V01/V06/V07`, `0014`,
  `0099`, etc.; the planner activates + labels them.
- **`calculate(stream)`** sums `STK_Q` over each scope's active SLOCs, grouped by
  `(ST_CD/RDC, MAJ_CAT, GEN_ART_NUMBER, CLR)` → `store_stk_ttl` / `dc_stk_ttl`. Keeps
  the latest run per stream + a sequence record. So the total is entirely driven by the
  SLOC selection (verified: activating `HUB_INTRA` flipped FA store total 0 → 279,321).
- FNL_Q (pool − PEND − HOLD) refinement for the MSA scope is a later step; v1 is the
  gross selected-SLOC total.

### C. CONS — Consumables Allocation

**Auto-MBQ formula (from the SOP):**

```
MBQ = MIN(max_hold_capacity, 15_day_sale) + (per_day_sale × cover_days)
```

Mapped to existing building blocks:

| Term | Existing source |
|---|---|
| `max_hold_capacity` | display/hold capacity ≈ `ACS_D` / `DISP_Q` (`grid_calculations.py:63-64`, default 18; per-article override via `MANUAL_DENSITY`) |
| `per_day_sale` | `SAL_PD` (`grid_calculations.py:_step_sal_pd` `:438-469`) or velocity `MAX_DAILY_SALE` (`listing.py:2312-2334`) |
| `cover_days` | `ALC_D = INT_DAYS + PRD_DAYS + SL_CVR` (`grid_calculations.py:_step_sal_d` `:376-432`), where `SL_CVR` = std cover (the "10-day 0002 cover"), `PRD_DAYS` = DO-picking (~3 days), `INT_DAYS` = store-wise interval |
| **`MIN(capacity, 15_day_sale)`** | **new** — this clamp is not yet computed anywhere |

`per_day_sale` and stock are read from the **live** sources (`MASTER_GEN_ART_SALE` /
`ST_MAJ_CAT` and `VW_ET_MSA_STK_WITH_MASTER.STK_Q` via `msa_service.py`) — **not**
the `store_sales` / `store_stock` scaffold tables (those are seed/test only).

**Daily auto-update:** register a schedule or hook `msa.completed` on
`report_scheduler_service.py` (in-process daily scheduler; events at
`report_scheduler_service.py:42-47`) to recompute `ARS_FACONS_MBQ` (`STREAM='CONS'`)
each day, then run allocation. The weekly-MBQ conversion in the old SOP
(`(monthly/30)×7`) is **not required** — daily MBQ replaces it.

### D. Gap Report

Reconcile **MBQ ↔ dispatched (BDC/DO) ↔ pending** and propose a **final ATR** action.
Reuse:

- `gap_report.py` (`/gap-report/*`) for the multi-category gap surface, and
  `pend_alc.py` `GET /dispatch-gap` (`:960`, builder `:897-957`) which already lists
  open pending lines tagged with `blocked_by`.
- **HOLD action** (hold picking-pending/PRD stock): supported today via the three
  dispatch-control tables — `ARS_HOLD_ARTICLE_BDC`, `ARS_DIVISION_DELETE_BDC`,
  `ARS_DIVISION_DELETE_ON_MAJ_CAT_BDC` (`pend_alc.py:119-149`). `bdc-generate` ANDs
  the `NOT EXISTS` forms so held lines drop out of dispatch; the Gap Report uses the
  `EXISTS` forms to surface what was held. "Holdable" = open pending not yet
  BDC-stamped (`_NO_OPEN_BDC_PREDICATE`, `pend_alc.py:109-117`).
- **Return-from-store action** (stock already dispatched from DC, must come back):
  **net-new**. ⚠ **Do not call it "GRT"** — see naming caution below.

### E. Cross-cutting revised rules

- **All segments:** the two-segment restriction is hard-coded as
  `SEG IN ('APP','GM')` at `msa_service.py:498` and `:1241` (and mirrored in
  `contrib.py:316/318/613/907`, `auto_contrib.py:510`). Relaxing it to all segments
  is the **single highest-risk change** — it widens the MSA universe and touches
  every downstream calc; it must be gated and validated in isolation, not bundled
  with the FA & CONS scaffold.
- **Audit-controlled MBQ changes:** change counts are derivable from
  `data_change_log` filtered on `column_name IN ('MBQ','MJ_MBQ','OPT_MBQ',…)`
  (`models/audit.py`, writer `services/audit_service.py::log_bulk_changes`). A
  **mandatory `REASON` per change is new** — `data_change_log`/`audit_log` only have
  free-text `notes`; capture reason on `ARS_FACONS_MBQ` and/or extend the change log,
  following the pattern of the hold endpoints which already enforce `reason`.
- **⚠ Naming caution — GRT:** in this codebase **`GRT` already means the "Growth"
  stock pool** (the Fresh/GRT allocation selector; `ARS_MSA_SLOC_SETTINGS.sloc_type`,
  `alloc_pool.py`, `BRD_FRESH_GRT_HOLD_CONTROL.md`). It is **not** goods-return. The
  SOP's "GRT from store" return action must use a **distinct token** (e.g.
  `STORE_RETURN`) to avoid a semantic collision across the whole allocation layer.
- **No manual stock intervention:** allocation stays system-only. Do **not** add a
  path that lets a user override system stock when it disagrees with physical stock;
  corrections must flow through the SLOC stock-shift workflows (§B.6) so they are
  auditable.

### F. Pending-Allocation (pend_alc) management for FA & CONS

**Requirement (confirmed):** FA/CONS must maintain **pend_alc at `ARTICLE_NUMBER`
grain — NOT REF_ART** — and maintain **alloc-detail history exactly like the normal
listing process**, plus the **operation log**, so history and reporting work identically
to core ARS. (REF_ART is only the *demand/budget* grain in §B; every persisted
allocation, pend and dispatch row is per `ARTICLE_NUMBER`.)

The whole pend_alc lifecycle — **approve → BDC generate → DO drawdown → close**, plus
reco, operations-log/revert, dispatch-control holds and the store BDC schedule — is
generic and **reused as-is** for FA and CONS:
- **Alloc detail + history:** write per-`ARTICLE_NUMBER` rows to the same working/parked/
  history tables the normal listing uses (`ARS_ALLOC_WORKING` → `ARS_ALLOC_PARKED` →
  `ARS_ALLOC_HISTORY`, with `ARS_LISTING*`/`ARS_LISTING_WORKING*` siblings), tagged with
  the FA/CONS run, so the existing Alloc Review, GAP and reco reports light up unchanged.
- **Pend:** `ARS_PEND_ALC` grain `(SESSION_ID, RDC, ST_CD, ARTICLE_NUMBER, ALLOC_MODE)`
  (per-variant; carries `GEN_ART_NUMBER`/`CLR`/`MAJ_CAT`).
- **Operation log:** every mutation (approve, BDC, DO, adhoc-close, hold) writes an
  `ARS_PEND_ALC_OPERATIONS` row with a revert payload — reuse `log_operation`.

SAP is **file-based** (BDC Excel out, DO CSV in) — no live link. Route FA/CONS through
the same machinery, tagged, with three correctness fixes that are **mandatory before
FA and CONS pend can coexist** with each other or with the core RL/TBC/TBL streams.

**How to tag the streams (no schema change):**

- Set **`ALLOC_MODE = 'FA'`** and **`ALLOC_MODE = 'CONS'`** on their pend rows.
  `ALLOC_MODE` already takes `RL/TBC/TBL/NL/AUTO/MANUAL`, is indexed with `IS_CLOSED`,
  and is already faceted in `/pend-alc/summary`, `/reco`, `/detail`, `/sessions`. So
  FA/CONS rows appear on every existing manage page automatically; the UI just needs a
  mode filter/toggle. Keep `SOURCE = AUTO` for engine runs, `MANUAL` for uploads, and
  `ALLOC_TYPE ∈ {FRESH, GRT}` unchanged.
- `write_pend_alc` currently derives `ALLOC_MODE` from
  `ARS_LISTING_WORKING_HISTORY.OPT_TYPE`. FA/CONS need either an FA/CONS-aware approve→
  pend writer or a stamped stream tag, so the mode is `FA`/`CONS` rather than the
  OPT_TYPE. This is the one new write-path piece.

**Reuse verbatim:** BDC generate (`/pend-alc/bdc-generate`), DO entry
(`/pend-alc/do-entry` → `/do-update`), adhoc close (`/pend-alc/adhoc-close`),
reconciliation (`/pend-alc/reco`), open-BDC report, operations log + revert, and the
store BDC schedule. No new pages needed — add an **ALLOC_MODE = FA / CONS filter** on
the existing pend pages (and, optionally, deep-link into them pre-filtered from the FA &
CONS section). The FA & CONS "Gap Report" page is the `dispatch-gap` + `reco` surface
pre-scoped to `FA`/`CONS`.

**⚠ Mandatory fixes before coexistence (correctness):**

1. **Extend the DO FIFO drawdown partition to include `ALLOC_MODE`
   (and `ALLOC_TYPE`).** `apply_do_deductions` (`pend_alc_service.py:3627`) drains open
   rows FIFO by `(RDC, ARTICLE_NUMBER)` **ignoring `ALLOC_MODE`**. If an FA line and a
   CONS (or RL) line exist for the same `(RDC, ST_CD, ARTICLE)`, a DO for one stream
   can silently draw down the other. The FIFO partition must include `ALLOC_MODE`
   (ideally `ALLOC_TYPE` too) so a stream's DO only closes that stream's pend.
2. **Enforce the grain for manual/stream inserts.** Only `ID` is a PK;
   `write_manual_pend_alc` does not guard the 5-part grain, so duplicate FA/CONS rows
   can double-count. Add the same `NOT EXISTS` guard `write_pend_alc` uses, or a filtered
   unique index on `(SESSION_ID, RDC, ST_CD, ARTICLE_NUMBER, ALLOC_MODE)`.
3. **Scope adhoc-close by mode.** A close CSV with a blank `ST_CD` is an **any-store
   wildcard** (`pend_alc.py:3104`); for FA/CONS require the stream (`ALLOC_MODE`) in the
   close key so a project-store close cannot sweep consumable pend (or vice-versa).

**Dispatch control for FA/CONS:** reuse the three `*_BDC` control tables
(`pend_alc.py:119-165`) for holds (they key on GEN_ART/CLR/MAJ_CAT/ST_CD). If project
stores need stream-specific hold rules (layout-change / damage / short-receipt →
withhold until MBQ or stock is corrected), add a parallel mode-scoped control table
following the identical `EXISTS`/`NOT EXISTS` pattern — do not overload the shared ones.

**Cadence:** FA and CONS both run **daily**; the existing per-store BDC schedule
(`StoreBdcSchedulePage`) governs *when* each store's BDC is emitted, so it already
supports different dispatch days per store without change.

---

## Forward plan / deferred (not built in the scaffold pass)

1. `ARS_FACONS_MBQ` master + migration script (next number in `scripts/`).
2. Backend endpoint + service for FA (upload, list, edit, audit) mirroring
   `upc_store_track` (`endpoints/upc_store_track.py` + `services/upc_store_track_service.py`).
3. Consumables auto-MBQ calculator + daily scheduler hook.
4. The `listing.py` MBQ-overwrite + priority-override merge steps (§B.2–B.3).
5. Gap Report ATR logic + the new `STORE_RETURN` action.
6. All-segment enablement (isolated, gated change).
7. Mandatory MBQ `REASON` capture/enforcement.
8. Replace the four placeholder pages with real UIs.
9. **pend_alc for FA/CONS (§F):** FA/CONS-aware approve→pend writer stamping
   `ALLOC_MODE`; the three mandatory correctness fixes (FIFO partition by
   `ALLOC_MODE`/`ALLOC_TYPE`, grain guard on manual/stream inserts, mode-scoped adhoc
   close); ALLOC_MODE filter on the existing pend pages; FA delivered-vs-MBQ balance
   view (§B.4).

---

## Phase 2 — Allocation engine + SEPARATE pending (finalized 2026-07-24)

> **This section supersedes §F's "reuse core pend_alc + ALLOC_MODE tag + 3 fixes"
> approach.** Per the owner's decision, FA & CONS gets a **self-contained** allocation
> and pending-allocation system. **The existing Pending Allocation menu, tables and
> logic are NOT touched** — no `ALLOC_MODE` tagging of core pend, no FIFO/manual/adhoc
> changes to `pend_alc_service`. FA/CONS gets its own tables + its own screens under the
> FA & CONS section. (The three "mandatory coexistence fixes" no longer apply.)

**Locked decisions (2026-07-24):**
1. **Separate FA/CONS pending** — own tables mirroring the pend lifecycle; core pend
   untouched. One FA/CONS pending schema, rows tagged `stream` (FA/CONS) + `session_id`
   (split into two tables later only if needed).
2. **Store priority = OP_DT, OLDER → NEWER** (older opening date ranks first; tie-break
   by store code). Mirrors `MANUAL_ST_PRIORITY → ST_RANK` shape.
3. **`source_type` (CENTRAL/LOCAL) is a REF-ART property** carried on MBQ (applies to
   every store); a small per-`(st_cd, ref_art)` **exception** table overrides it when a
   specific store needs the opposite. **Engine runs CENTRAL only**; a UI toggle flips a
   ref (or a store-exception) between CENTRAL/LOCAL so RDC dispatch can be turned on/off.
4. **Ref-art → article split default = MAX-QTY article first**; alternative lowest-first;
   **manual override**: click a ref-art → UI suggests its actual articles + remaining
   stock so the planner can re-pick. Save both the ref-art plan and the article result.
5. **Pack size (`PAK_SZ`)**: allocate in whole packs — **round to nearest multiple,
   minimum 1 pack**.
6. **UPC vs OLD store status is DERIVED from the store master** — when the master shows a
   store as opened/old, the system **auto-converts UPC → OLD** (no manual flip needed);
   opened stores are then treated as old for selection/priority.

**New tables (all app-owned, `ARS_FACONS_*`; nothing shared with core):**
- `ARS_FACONS_STORE_LIST` — the UPC store list: uploaded stores + `status` (UPC/OPEN,
  derived from master), validation flags, history/events. Validation shows (a) stores
  **not in the store master** and (b) master stores **missing from the list** — mirror
  the **BDC Schedule** store-validation pattern.
- `ARS_FACONS_ALLOC_SESSION` — one row per alloc run (stream, store-scope UPC/OLD/ALL,
  central-only, counts, user, time) — everything is session-wise for tracking.
- `ARS_FACONS_ALLOC_REF` — ref-art-level plan (session, store, ref_art, source_type,
  req/pack-rounded qty).
- `ARS_FACONS_ALLOC_ART` — article-level result (session, store, ref_art,
  article_number, qty, split-rule, manual_override flag).
- `ARS_FACONS_PEND` — pending at **article grain** (session, stream, store, article,
  qty, delivered, status) + its own **operations log** and **manual-entry** — a private
  mirror of the pend lifecycle, never the core tables.

**Allocation flow:** pick store-scope (UPC / OLD / ALL) + stream (FA / CONS / ALL) on a
single **allocation console** → rank stores by OP_DT (older first) → for each CENTRAL
ref-art compute qty, round to pack size → split ref→article (max-qty-first, override-able)
→ write ref + article tables + article-grain pending, all stamped with the session.
Goods allocation stays a separate run (§ not in FA/CONS).

**Pending / ops-log / manual for FA/CONS (SEPARATE, layman summary):** same
"promise → approve → dispatch → deliver → close" idea as the old system, but on the
FA/CONS-only tables, filtered by session. Manual entry = planner adds a line by hand
(store + ref/article + qty + reason). Operations log = a diary of every action
(approve, dispatch, deliver, close, manual add, local↔central flip, ref→article re-pick)
stamped with stream + session + user + time. Because it is physically separate, it can
never disturb the core Pending Allocation screens.

**Build order:** (A) `source_type` on MBQ + UPC Store List page + OP_DT priority →
(B) allocation console with ref→article conversion, manual override, pack rounding →
(C) FA/CONS-only pending + ops-log + manual entry, session-wise.

---

## Recorded rules

- **2026-07-24** — **Gap Report BUILT** (`/fa-cons/gap-report`, replaces the placeholder;
  `facons_gap_service.compute`). Per store×ref: `gap = MBQ − (store stock + open pending)`;
  when short, **validates the warehouse (MSA) pool** and splits the action into
  **DISPATCH** (pack-rounded, drawn from the pool oldest-store-first) + **PURCHASE** (the
  rest); shelf ≥ MBQ with open pending → **HOLD**; shelf > MBQ → **STORE_RETURN**; else
  **OK**. Three views: **Store Gap** (line-level + ATR filter), **Purchase Requirement**
  (per-ref net buy across the network), **Overstock / Return**. `GET /fa-cons/gap?stream=
  &store_scope=`. Since `ET_MSA_STK` has no FA/CO yet, every shortfall reads as PURCHASE
  today (correct); flips to DISPATCH once warehouse stock is maintained. **FA & CONS
  Phases A–C + Gap Report all built.**
- **2026-07-24** — **Phase C BUILT: separate FA/CONS Pending Allocation + operations log
  + manual entry.** Own tables `ARS_FACONS_PEND` / `ARS_FACONS_PEND_OPS` — the core
  Pending Allocation menu/tables/logic are **untouched**. Lifecycle: **approve** an alloc
  session (`POST /pend/approve-session`) → its article lines become OPEN pending lines →
  **deliver** (`/pend/{id}/deliver`, DO) reduces the balance and auto-**CLOSE**s when
  fully delivered → adhoc **close** (`/pend/{id}/close`, reason mandatory) / **reopen**.
  **Manual entry** (`/pend/manual`, reason mandatory) adds a line by hand. Every action is
  written to the operations log (APPROVE / MANUAL_ADD / DELIVER / CLOSE / REOPEN, with
  stream + store + qty + user + time). Page `/fa-cons/pending` (sidebar "Pending Alloc"):
  Pending | Operations Log tabs, stream/status filters, faceted column filters + search,
  summary tiles (lines/open/closed/promised/delivered/balance). Verified end-to-end via
  manual add → deliver → auto-close → ops log. **This completes FA & CONS Phases A–C.**
- **2026-07-24** — **Phase B.2 BUILT: ref → article split + manual override.** The
  allocation now splits each ref line's qty into actual article numbers **max-qty-first**
  (article with the most warehouse stock ships first), drawing down a shared per-(RDC,
  article) pool across stores in the same older-first order; stored in
  `ARS_FACONS_ALLOC_ART`. **Manual override:** the Allocation grid's per-row *Articles*
  button opens a picker listing the ref's member articles (from `VW_MASTER_PRODUCT`) with
  their **remaining `ET_MSA_STK` warehouse stock** and an editable qty; save marks the
  line `manual`. Endpoints `GET/POST /fa-cons/alloc/articles`. Article warehouse stock
  reads `ET_MSA_STK` by article (MATNR) per the MSA SLOCs — same source as the pool, so
  it stays 0 for FA/CO until that data is maintained.
- **2026-07-24** — **Phase B.1 BUILT: Allocation console (ref-art level).** SEPARATE
  from core pend_alc — own tables `ARS_FACONS_ALLOC_SESSION/_REF/_ART`, session-wise.
  `POST /fa-cons/alloc/run?stream=&store_scope=` runs FA/CONS/ALL × UPC/OLD/ALL: CENTRAL
  refs only, `required=max(MBQ−store stock,0)`, drawn from the DC pool oldest-store-first,
  rounded to whole packs (`PAK_SZ`, min 1). Console at `/fa-cons/allocation` (sidebar
  "Allocation" — replaced the two alloc placeholders). **⚠ Blocker:** the FA/CONS DC pool
  (`ARS_FACONS_MSA`) is empty because the MSA-scope calc reads the fashion-only
  `VW_ET_MSA_STK_WITH_MASTER`; so allocation ships 0 until the pool is sourced from
  `ET_STORE_STOCK` DC-side SLOCs (grouped by RDC). **B.2 next:** ref→article split
  (max-qty-first using article stock) + manual override.
- **2026-07-24** — **Store list: bulk add-missing.** Validation adds a
  `master_upc_not_listed` report; `POST /store-list/add-missing?source=master_upc|master|mbq`
  bulk-adds. Used to load all 43 UPC stores from the master.
- **2026-07-24** — **Phase A.2 BUILT: UPC Store List** (`/fa-cons/store-list`,
  `ARS_FACONS_STORE_LIST` + `_HIST`, `facons_store_list_service.py`). Upload/add the
  stores FA/CONS runs against; each row enriched LIVE from `Master_ALC_INPUT_ST_MASTER`
  with a **derived UPC/OLD status** (from `ST_STATUS`; opened→OLD automatically) and an
  **OP_DT older→newer priority** rank. **Validation** (like BDC Schedule): `missing_in_master`
  (listed stores absent from master) + `missing_in_list` (stores with FA/CONS MBQ not in
  the list, one-click **Add**). Faceted filters + comma search + UPC/Old/All toggle +
  per-row history. Endpoints under `/fa-cons/store-list` (GET, /validation, POST, DELETE,
  /upload, /history, /template). Sidebar: FA & CONS → UPC Store List.
- **2026-07-24** — **Phase A.1 BUILT: MBQ `source_type` (CENTRAL/LOCAL).** Added to
  `ARS_FACONS_MBQ` (default CENTRAL); optional `SOURCE_TYPE` column in the upload
  template + ingest; shown in MBQ Master as a clickable **Type** badge that flips
  CENTRAL↔LOCAL **ref-art-wide** (`POST /fa-cons/mbq/source-type` → `set_source_type`
  updates every store row of that ref). Per-store exception override deferred.
  **Update (same day):** in MBQ Master, Type is a **top segmented filter**
  (All types / Central / Local) like the FA/CONS toggle — **not** a table column; it
  filters the view. Type is set via the upload `SOURCE_TYPE` column (the ref-art-wide
  `set_source_type` endpoint still exists for a future in-app control).
- **2026-07-24** — **Phase 2 finalized (see Phase 2 section).** FA & CONS gets a
  SEPARATE allocation + pending system; **core Pending Allocation is not touched** —
  this supersedes the §F shared-pipeline/ALLOC_MODE plan and voids its 3 "coexistence
  fixes". Decisions: separate FA/CONS pend; store priority OP_DT older→newer;
  CENTRAL/LOCAL is a ref-art property (+ per-store exceptions), engine runs CENTRAL only
  with a UI toggle; ref→article split max-qty-first (override-able); pack size rounded,
  min 1; UPC→OLD auto-derived from the store master.
- **2026-07-22** — Module created as a spec + navigable sidebar scaffold ("FA & CONS"
  section, 4 placeholder pages). No backend / allocation logic yet.
- **2026-07-22** — `GRT` is reserved for the Growth stock pool; the project-store
  "return from store" ATR must use a different name (proposed `STORE_RETURN`).
- **2026-07-22** — FA MBQ maps to the `OPT_MBQ` grain; the injection seam is
  `listing.py` right after Part 4c (~`:2199`). The existing `_REM` loop already
  implements "balance requirement until MBQ = 0" — do not build a second balance engine.
- **2026-07-22** — Consumables auto-MBQ reuses `ACS_D`/`DISP_Q` (capacity), `SAL_PD`/
  `MAX_DAILY_SALE` (per-day sale) and `ALC_D` (cover days); only the
  `MIN(capacity, 15-day sale)` clamp is new. Read sales/stock from the live
  `MASTER_GEN_ART_SALE` / `VW_ET_MSA_STK_WITH_MASTER`, never the `store_sales` /
  `store_stock` scaffold tables.
- **2026-07-22** — FA allocates **by reference article** = the OPT grain
  `(WERKS, MAJ_CAT, GEN_ART, CLR)`; the engine is already ref-art-grain, so no new
  grain is needed. Single (`ATT_TYP='00'`) articles (most project components +
  consumables) collapse to one row `SZ_MBQ = OPT_MBQ` with no size split.
- **2026-07-22** — pend_alc for FA/CONS: **reuse the whole lifecycle**, tag rows with
  `ALLOC_MODE='FA'`/`'CONS'` (no schema change). Three fixes are **mandatory before
  streams coexist**: (1) add `ALLOC_MODE`(+`ALLOC_TYPE`) to the DO FIFO drawdown
  partition in `apply_do_deductions` — today it drains by `(RDC, ARTICLE_NUMBER)` only
  and would cross-attribute a DO between streams; (2) enforce the 5-part grain on
  manual/stream inserts (only `ID` is a PK today); (3) scope adhoc-close by
  `ALLOC_MODE` (blank `ST_CD` is an any-store wildcard).
- **2026-07-22 — Data foundation BUILT.** Per-stream/per-scope SLOC selection
  (`ARS_FACONS_SLOC_SETTINGS`), dedicated FA/CONS stock calc (single source =
  `VW_ET_MSA_STK_WITH_MASTER`, all segments, totals driven by the active SLOC set), and
  the 3-col MBQ Master (`ARS_FACONS_MBQ`, enriched live from `Master_ALC_INPUT_ST_MASTER`).
  Deviation from plan: store-stock is sourced from the MSA view (the SOP's plant),
  **not** `ET_STORE_STOCK`, and totals use a long-form SUM per scope rather than the
  dynamic-column pivot — simpler, safer, and faithful to "FROM MRST_FA & CO".
- **2026-07-22 — REF_ART clarified.** `REF_ART` is a column on `VW_MASTER_PRODUCT`
  (819K/3.6M populated, 281K distinct; one REF_ART → many ARTICLE_NUMBERs; `'NA'`/`'0'`/
  `''` = no-ref → fall back to GEN_ART). FA MBQ is per `(ST_CD, REF_ART)`; allocation
  computes demand at REF_ART, resolves member ARTICLE_NUMBERs, ships **MAX-qty article
  first within the REF_ART**, and stamps the **final qty per ARTICLE_NUMBER** (§B).
- **2026-07-22 — pend/detail/op-log grain confirmed.** pend_alc, alloc-detail history
  (working→parked→history like normal listing) and the operation log are all maintained
  at **`ARTICLE_NUMBER`** grain (not REF_ART), so existing history/reports work as-is (§F).
- **2026-07-22 — SLOC Settings shows ALL actual SLOCs.** The page lists every SLOC in the
  stock view by scope (unsaved = "new", inactive), raw code shown — **no** label
  conversion (INT/PRD/STO, MSA=V02_FRESH are not hardcoded); the planner activates/
  inactivates per FA and CONS independently.
- **2026-07-23 — MBQ upload is confirm-before-commit.** `dry_run=true` previews the
  create/update/unchanged breakdown + per-row details WITHOUT writing; a review modal
  gates the commit (`dry_run=false`). Global search added to the master grid. Prevents
  accidental bulk overwrites.
- **2026-07-23 — MBQ Change Review BUILT** (tab in MBQ Master; MBQ-only scope).
  `GET /fa-cons/mbq/change-review` (read-only over `ARS_FACONS_MBQ_HIST`) audits changes
  for a single date (`date_from=date_to`) or a date range (+ stream/action/store/ref/by
  filters), default last 7 days. Summary **reconciles**:
  `total = created + updated + deleted + approved` (the `approved` bucket was added so
  legacy `APPROVE` events don't leave the tiles under-counting the total), plus distinct
  stores/ref-arts and `net_delta`. Presets Today/Yesterday/7/30d; xlsx export mirrors the
  query. Verified on real data: 20 events = 16 UPDATE + 4 APPROVE, date filter splits
  6 (22-Jul) / 14 (23-Jul).
- **2026-07-22 — OPEN QUESTION** (confirm before build): do FA project stores and CONS
  dispatch through the **same** file-based SAP BDC/DO flow as the core streams, or a
  different channel?
