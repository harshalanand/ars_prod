# UPC Store Tracking

> Module route: `/reports/upc-tracking` · API prefix: `/upc-store-track` ·
> Service: `backend/app/services/upc_store_track_service.py` ·
> Tables: `scripts/024_upc_store_tracking.sql`

The dynamic replacement for the manual **"STORE OPENING DATES *.xlsx"** workbook
the replenishment team maintained by hand (dragging the current proposed date
into a new weekly column). This module captures the same lifecycle — but the
history, counts and delays are derived automatically, and the MBQ / stock /
SLOC-wise / fill-rate figures are pulled live instead of pasted.

---

## BRD — why this exists

New (UPC) stores are opened on a *proposed opening date* that slips repeatedly.
The team needs to answer, at any moment and per store:

- What opening date is committed **now**, what was the **first** date given, and
  **when** was it first shared?
- **How many times** has a date been given, and **how many times** has it
  actually **changed**? What is the **total slippage** (first → latest)?
- Is the store **on track or delayed**, and by how many days?
- Is the **store layout generated**, when was it **received**, and when was the
  **display first shared**?
- What are the **latest remarks**, **how many times** have remarks changed, and
  what is the full remark trail?
- What is the store's readiness — **MBQ (100 % / 110 %)**, **total stock**,
  **SLOC-wise stock**, and **fill-rate %** — as of the latest trend.

Previously all of this lived in one 130-column spreadsheet with a manual weekly
"date matrix". This module makes it a first-class, auditable surface.

---

## FSD — how it works

### Sources (nothing duplicated)
| Data | Source | Note |
|---|---|---|
| Identity: site name, RDC, hub, status, op-date, priority | `Master_ALC_INPUT_ST_MASTER` (join on `ST_CD`) | live |
| MBQ 100 %, display qty, total stock, SLOC-wise | `ARS_GRID_MJ` **aggregated per store** — `SUM(...)` over the `WERKS`'s categories **whose SEG (from `MASTER_PRODUCT`) is in the selected segments** | live |

**Segment scope**: MBQ / stock / SLOC / fill-rate are computed for a
configurable set of segments (`SEG` in `MASTER_PRODUCT`) — **default APP + GM**.
A segment picker on the page (`APP GM MKT ACC NT FAB`) recomputes the metrics
live. Grid categories not present in `MASTER_PRODUCT` (the `B-`-prefixed ones)
carry no SEG and fall outside the filter (kept as-is, not matched).
| MBQ 110 % | derived = MBQ 100 % × 1.10 | computed |
| Fill-rate % | total stock ÷ MBQ × 100 | computed |

`SUM(MBQ) → MBQ 100%`, `SUM(STK_TTL) → Total Stock`, `SUM(DISP_Q) → display`.
SLOC-wise stock = the grid columns summed per store: `0001, 0002, 0004, 0006,
0099, 0017, HUB_INTRA, HUB_PRD_Q, ST_STK_V06_QTY, ST_STK_V07_QTY,
DH24_PTL_V07_Q, DH24_PTL_V18_Q, DH24_PTL_V25_Q, DW01_PTL_V07_Q,
DH24_STO_QTY_Q, DW01_STO_QTY_Q, PEND_ALC`. These map directly to the manual
workbook's SLOC-wise block (the workbook was built from grid data). The grid is
current-state (no version/date), so metrics reflect the live grid at read time.

### What is persisted (three tables)
- **`ARS_UPC_STORE_TRACK`** — one row per store: first/latest proposed date,
  first/latest share date, `date_given_count`, `date_change_count`, layout
  fields, actual-open date, status/priority, `last_remarks`,
  `remarks_change_count`.
- **`ARS_UPC_STORE_TRACK_DATE_HIST`** — one immutable row per proposed-date /
  share submission, with a `changed` flag + `prev_proposed_dt`.
- **`ARS_UPC_STORE_TRACK_REMARK_HIST`** — one immutable row per remark change.

### Input (upload or manual) — validate-then-record
Upload has **three mandatory columns**: **`ST_CD`**, **`PROPOSED_OPENING_DATE`**,
**`SHARE_DATE`**. A row missing any of them is rejected (reported by row number);
a file missing a column is rejected outright. Header matching is
case/space-insensitive; recognised aliases include
`PROPOSED_OPENING_DATE / OP_DATE / BGT_OP_DT` and `SHARE_DATE / DATE_OF_SHARING`.
Dates are parsed **ISO-first**, then Indian `DD-MM-YYYY` — never dayfirst on an
ISO string. **Remarks are NOT uploaded** — they are added per store in the UI
(add/edit) after review. A blank template is downloadable from the page.

### Event rules (the heart of the module)
On every submission (upload row, add, or edit):
1. **Date event** — a DATE_HIST row is written **only for the baseline (first
   date ever) or a genuine change** (value differs from the previous latest).
   Re-sharing the **same** date does **not** create a history row — it only bumps
   `date_given_count` (how many times a date was shared) and `latest_share_dt`.
   So the history list stays clean: baseline + real changes only. The *first*
   date is the baseline, not a change, so `date_change_count` starts at 0.
   `first_proposed_dt` / `first_share_dt` are set once and never overwritten.
2. **Remark event** — appended to REMARK_HIST **only if** the text differs from
   `last_remarks`; then `remarks_change_count` increments. Blank / NaN-like
   values (`nan`, `none`) are ignored. Remarks are entered in the UI after review.
3. **Layout / outcome** fields (layout generated + received date, first display
   date, actual open date, status/priority) are plain state — no history.

The **Layout generated** checkbox means the store's display/planogram layout has
been prepared. It drives the table's Layout column (✓ / ✕) and pairs with the
layout-received and 1st-display-shared dates.

### Editing & cleanup
- **Inline editing from the table** (no need to open Edit each time): the
  **Status** dropdown, the **Last Remark** cell (click → remark popover), the
  **Layout** cell (click → *layout generated* toggle + received date), the
  **Display** cell (click → *display shared* toggle + 1st-display date), and the
  **Priority** cell (type the number straight in the cell — saves on Enter or
  when you click away) all save on the spot. Ticking a
  layout/display toggle auto-fills its date with today if empty. Layout and
  Display are separate ✓/✕ columns, each showing its date.
- The **Edit** form is pre-filled with the store's last saved values (current
  proposed date, share date, layout/display dates, remarks). Re-saving without
  changing the date is a no-op — no spurious change is recorded.
- **Clearing a date works**: emptying a date field (e.g. Actual open date) in
  the Edit form and saving now sets it to NULL. (Convention: empty = clear,
  omitted/None = leave unchanged, so inline edits never wipe other fields.)
### Help
A **Help** button in the header opens an on-page reference: all abbreviations
(ST_CD, RDC, Hub, MBQ, FR%, SLOC, SEG, dispatch date, …) and, for every column,
its meaning + formula (including the live segment scope and dispatch-lead in use).

### Export
Two exports:
- **Export All** (header) — complete data: every tracked store with all fields
  (identity + tracking + live MBQ/stock/fill-rate + all SLOC columns) for the
  current segment scope. Server-generated `.xlsx`.
- **Export View** (top-right of the table) — the report exactly as shown on
  screen: honours the active filters, sort order, and visible columns.
  Client-generated `.csv`.

(A backend `compact-history` routine still exists for data maintenance but is no
longer exposed as a button.)

### Status (lifecycle)
Each store carries a **status**: `ACTIVE` (default — still being tracked),
`OPENED`, `HOLD`, or `CANCELLED` (Reject/Cancel). It can be changed **inline
from the table** (the Status column is a dropdown — no need to open Edit) or from
the Edit form. Choosing **OPENED** prompts for the **actual open date** (defaults
to today) so it is always captured. **Auto-reactivation**: when a *new/changed*
proposed date arrives for a dormant store (CANCELLED / HOLD / unset), it flips
back to **ACTIVE** automatically (recorded in status history as `auto-date`) —
re-sharing the same date does not. Every change is **recorded as an event** in
`ARS_UPC_STORE_TRACK_STATUS_HIST` (from-status → to-status, who, when) and shown
in the store's detail drawer under *Status history*. Status affects the delay
logic: only **ACTIVE** stores past their latest budgeted date are flagged
**delayed**; OPENED/HOLD/CANCELLED are never counted as delayed. An unset status
is treated as OPENED if an actual-open date exists, else ACTIVE.
**Priority** is a store-level number synced with the **store master**
(`Master_ALC_INPUT_ST_MASTER.MANUAL_ST_PRIORITY`), which is the **single source
of truth**: editing the Priority cell writes it to the master (so allocation /
ranking see it) and mirrors a copy into the tracker; the cell reads the master
value live, so a master-side change (or re-upload) shows up in UPC automatically.
Blank clears it (NULL); non-integers are ignored.

### Table & filters
The **header row is frozen** (sticky) while the table scrolls. **Store** and
**Name** are separate columns; clicking the **Store** code opens that store's
**date-change chart** (like Total Stk → SLOC). A **Columns** button lets you
hide/show any column (remembered per browser) so the grid fits your screen.
Every column is **click-to-sort** (▲ asc / ▼ desc; dates sort chronologically).
Columns include **1st Date** (first proposed) and **1st Shared** (first share
date), a combined **Chg/Given** column, separate **Layout** and **Display** ✓/✕
columns (each with its date), and a **Status** dropdown (last column). **Hover previews** (no click needed): hovering the **Store** code opens that
store's date-change chart **plus the list of proposed/shared dates below it**;
hovering **Total Stk** shows the SLOC-wise breakdown; hovering **Last Remark**
shows the full remark text (click it only to edit). Hovering any **metric cell**
— Chg/Given, Delay, D.GAP, Repl Days, Bal Days — shows its **formula worked out
with that store's numbers** (e.g. `Delay = Latest Bgt − 1st Date = 68 days`;
`Bal Days: dispatch (17 Jul) −4 / opening (27 Jul) 6`). The header bar filters by
**status**, **month**, free-text search, and a
**delayed-only** toggle, with a **Reset** button. Filters are **cascading** —
each dropdown only lists values available under the other active filters (the
current selection always stays selectable). The **default view is Active only**;
Reset returns to it. CANCELLED/HOLD/OPENED stores are hidden until selected.

### Derived at read time
- `total_delay_days` = latest proposed − first proposed (how far the date has
  slipped since it was first given).
- **Bal Days** shows **two** balances (like Chg/Given): **`dispatch / opening`**.
  - **opening** = latest budgeted opening date − today.
  - **dispatch** = (opening date − **lead days**) − today, where *lead days* is
    configurable in the header (**default 10**). The dispatch date is the last
    date stock must go out to open on time.
  - Positive = days remaining; negative = overdue (red). `OPENED` → "opened"
    pill; `CANCELLED` → blank.
  - Example (today 21 Jul 26, lead 10): opening 27 Jul 26 → opening bal **6**;
    dispatch 17 Jul 26 → dispatch bal **−4**; cell shows **`-4/6`**.
- **Repl Days** column (before Bal Days) = **`a / b / c / d`** = days available
  for replenishment measured up to the **dispatch date** `(opening − lead)` from
  each milestone: **1st shared / latest shared / layout received / display
  shared**. (*Latest shared* = when the current budgeted date was shared.) `—`
  when that milestone date is missing.
- `total_delay_days` = latest proposed − first proposed (date slippage).
- Charts show their value labels on each bar / point.

### Charts
Every chart card has a toolbar: **chart⇄table** toggle, **CSV export**, and
**zoom** (opens large). All charts are **clickable to filter the table**.
1. **Status distribution** — click a bar → filter by status.
2. **Stores by opening month** — click a bar → filter by month.
3. **Balance days to opening** — buckets `delayed / 0-7 / 8-30 / 30+ / opened /
   no_date`; click any bucket to filter the table to it (click again to clear).
4. **Date changes over time** — genuine change events by share date; click a
   point to filter to the exact stores that changed on that date.

Each store also has its own **date-change chart** (click the Store code, or the
detail drawer). All history entries show **date + time**.

### API
`GET /upc-store-track` (list + live metrics) · `GET /{st_cd}` (detail + full
history + SLOC) · `GET /charts` · `GET /export` (xlsx) ·
`POST /` (add) · `PUT /{st_cd}` (edit) · `POST /upload` · `DELETE /{st_cd}`.

---

## Recorded rules
- The **first** date given is the baseline, never counted as a change
  (`date_change_count` starts at 0; `date_given_count` starts at 1).
- A **remark** is only recorded (and counted) when it differs from the previous
  `last_remarks` — identical re-submissions are ignored.
- Metrics are **live**, never snapshotted here: MBQ/stock/SLOC/fill-rate are
  `SUM`-aggregated from `ARS_GRID_MJ` per store at read time, so they always
  reflect the current grid (they change when the grid is rebuilt).
- MBQ 110 % is always MBQ 100 % × 1.10 (matches the manual workbook).
- Deleting a store removes its history too (hard delete of all three tables).
