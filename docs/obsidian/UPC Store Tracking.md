---
title: UPC Store Tracking
tags: [ars, report, module, tracking]
updated: 2026-07-30
---

# UPC Store Tracking

Store-opening lifecycle tracker at `/reports/upc-tracking` (API `/upc-store-track`).
The dynamic replacement for the manual **"STORE OPENING DATES *.xlsx"** workbook.
Backend `backend/app/services/upc_store_track_service.py`; page
`frontend/src/pages/UpcStoreTrackingPage.jsx`; tables `scripts/024_upc_store_tracking.sql`.
Full spec: `frontend/public/docs/manual/upc_tracking.md`. Related: [[Reports]] ·
[[Report Generation Hub]] · [[Data Model]] · [[Grid Builder]].

## What is stored vs live
- **Uploaded/edited (persisted):** `ARS_UPC_STORE_TRACK` (one row/store) + event
  history `…_DATE_HIST` / `…_REMARK_HIST` / `…_STATUS_HIST`. Upload = 3 mandatory
  cols: `ST_CD`, `PROPOSED_OPENING_DATE`, `SHARE_DATE`.
- **Live-joined at read:** identity from `Master_ALC_INPUT_ST_MASTER`; MBQ / stock /
  SLOC / FR from `ARS_GRID_MJ` summed over the selected **segments** (SEG from
  `VW_MASTER_PRODUCT`, default **APP+GM**).

## Key config (header)
- **Segments** — which SEGs feed MBQ/stock/SLOC/FR (default APP+GM).
- **Dispatch lead (days)** — default **10**. `dispatch date = opening − lead`
  (the last date stock must ship). Drives Bal Days & Repl Days.

## Columns — meaning & formula
| Column | Formula / source |
|---|---|
| Store | `ST_CD`. Click → per-store date-change line chart. |
| Name | site name (master `ST_NM`) |
| RDC / Hub | master `RDC` / `HUB` |
| 1st Shared | `first_share_dt` — first time any opening date was shared |
| 1st Date | `first_proposed_dt` — first proposed opening date ever |
| Latest Bgt | `latest_proposed_dt` — current committed opening date |
| Chg / Given | `date_change_count / date_given_count`; given = times shared, chg = times value actually changed (first = baseline) |
| Delay | `Latest Bgt − 1st Date` (total slippage since first given) |
| **D.GAP** | **`Latest Bgt − previous given date`** (the value before the *last* change). Most-recent shift only; `+N` later, `−N` earlier, `—` never changed. Backend surfaces `prev_proposed_dt` = `prev_proposed_dt` of the newest `changed=1` DATE_HIST row. |
| Repl Days | `a/b/c/d` = days up to **dispatch** from `1st-shared / latest-shared / layout-received / display-shared` (each = `dispatch − milestone`; `—` if missing) |
| Bal Days | `dispatch / opening` — `(opening−lead)−today` / `opening−today`; neg = overdue |
| MBQ 100% | `SUM(MBQ)` over selected segments (grid) |
| Total Stk | `SUM(STK_TTL)` over selected segments; click → SLOC-wise breakdown |
| FR% | `Total Stk ÷ MBQ × 100` |
| Layout | `layout_generated` ✓/✕ + `layout_rec_dt` (inline edit) |
| Display | `display_generated` ✓/✕ + `first_disp_dt` (inline edit) |
| Last Remark | latest remark (inline; history only on change) |
| R.Chg | `remarks_change_count` |
| Priority | store priority — **synced with master `MANUAL_ST_PRIORITY`** (master = source of truth; editing here writes the master); type directly in cell |
| Status | `ACTIVE`(default)/`OPENED`/`HOLD`/`CANCELLED`; inline; OPENED asks actual-open date; a new/changed date auto-reactivates a dormant store |

## Behaviours
- **Auto status on upload** (each upload = the full current schedule): date given →
  ACTIVE (reactivates); blank date → CANCELLED; any ACTIVE store **not in the
  upload** → CANCELLED (`auto-absent`, OPENED kept). Guarded on empty upload.
- **Reconcile vs master** card: Missing (master `ST_STATUS='UPC'` not tracked) /
  Extra (tracked but not a UPC store in the master). `GET /compare`.
- **Event history** date+time; a date row is written only on baseline or genuine
  change (unchanged re-share bumps *given* count + `latest_share_dt` stays put).
- **Inline editing** for status / remark / layout / display / priority.
- **Hover previews**: Store → date chart + proposed/shared list; Total Stk → SLOC split; Last Remark → full text; and every metric cell (Chg/Given, Delay, D.GAP, Repl Days, Bal Days) shows its formula worked out with the store's own numbers. **Help** button documents all columns + abbreviations.
- **Charts** (value-labelled, clickable to filter, chart⇄table, CSV, zoom):
  Status distribution, Stores by month, Balance-days buckets, Date-changes over
  time (click a point → stores changed that date). Per-store date chart via Store click.
- **Reset** (Settings → Application, superadmin): `POST /upc-store-track/reset` wipes the 4 owned tables only (TRUNCATE, or DELETE + `DBCC CHECKIDENT RESEED 0` — resets identity "from 0"; master/grid/view untouched; master priorities persist).
- **Filters** cascade; default view = Active only. **Help** button documents all
  columns. **Export All** (Excel, full) / **Export View** (CSV, on-screen report).

## SLOC columns are dynamic
The SLOC-wise stock breakdown is **discovered at runtime**, never hardcoded.
`_sloc_cols()` reads `INFORMATION_SCHEMA.COLUMNS` for `ARS_GRID_MJ` and returns
every numeric column except an exclude set `_GRID_NON_SLOC` (the grain, the
aggregates surfaced separately — MBQ / STK_TTL / DISP_Q — and derived metrics).
Result is cached per process. A column added/dropped in the grid appears/
disappears automatically, with no code change. `sloc` dict keys are the raw grid
column names. To keep a *new non-stock* numeric grid column out of the SLOC list,
add its name to `_GRID_NON_SLOC`.

## Known issues / fixes
- **2026-07-30 — SLOC column drift (500 on page open), now fixed permanently.**
  The old hardcoded `SLOC_COLS` dict listed `DH24_PTL_V07_Q / V18_Q / V25_Q`,
  which had been dropped from `ARS_GRID_MJ`. list_stores + charts threw pyodbc
  `42S22` "Invalid column name" → the whole page blanked. Fixed by replacing the
  hardcoded dict with runtime discovery (see *SLOC columns are dynamic* above), so
  grid schema drift can never break the page again.

## Cross-links
[[Reports]] · [[Report Generation Hub]] · [[ARS Work Log]] · [[Data Model]] · [[Known Risks and Doc Drift]]
