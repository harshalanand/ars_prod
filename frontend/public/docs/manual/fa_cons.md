# FA & CONS — ARS Manual

*Project-Store Allocation (FA) & Consumables Allocation (CONS)*

> **Status:** Specification. The sidebar section **FA & CONS** and its pages exist
> as navigable placeholders; the allocation logic, master tables, endpoints and
> services described in the FSD are **not yet built** and are listed under
> *Forward plan / deferred*. This dossier is the canonical spec — build to it.
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
4. **Cross-run cumulative dispatched:** already tracked in **`ARS_PEND_ALC.PEND_QTY`**
   (`pend_alc_service.py`); on MSA rebuild it is deducted from availability, so
   already-dispatched-not-yet-DO'd quantity reduces the next day's requirement.
5. **Pool selection:** reuse `alloc_type ∈ {FRESH, GRT}` on `GenerateRequest`
   (`listing.py:202`) + `ARS_MSA_SLOC_SETTINGS` (`alloc_pool.py`). Configure the SOP
   SLOCs — `0001`, `0002`, `DC/HUB TO ST INT`, `DC/HUB TO ST PRD`,
   `DC TO ST PICKING PENDING`, `0014`, `0015`, `0016` — as FRESH/GRT there.
6. **UPC extra-req handlers** (layout change / damage / short receipt) are stock-
   correction workflows that shift qty to damage / short-excess SLOCs and re-run
   allocation on the corrected stock; the layout-change path updates MBQ first.

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

---

## Recorded rules

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
