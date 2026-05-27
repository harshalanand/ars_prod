# ARS Dashboard — Implementation Plan

**Status:** Proposal — awaiting review before development
**Author:** Santosh Kumar
**Date:** 2026-05-22  ·  **Revised:** 2026-05-22 (rev 2 — charts + product drill)
**Companion mockup:** [`/ars_dashboard_mockup.html`](../../../ars_dashboard_mockup.html) (open in browser)

---

## 1. Goal

A single landing dashboard that lets a planner answer "what happened today, what is stuck, where are the gaps, and *which MAJ_CAT/store/OPT/VAR caused them*?" without jumping between Listing, Allocations, Hold, and Pend Alc pages. Everything is driven by a global **Date → Session** picker + scope filters (MAJ_CAT, Store, RDC), with a dedicated **Product Drill** view that walks the hierarchy in either direction (`MAJ_CAT → Store → OPT → VAR` or `Store → MAJ_CAT → OPT → VAR`). The dashboard unifies the four data sources already in production:

| Data source                  | Source table                  | Used today via |
|------------------------------|-------------------------------|----------------|
| Approved allocations         | `ARS_ALLOC_HISTORY` (+ `alloc_header/detail`) | `allocations` endpoint |
| Pending allocations          | `ARS_PEND_ALC`                | `pend_alc` endpoint, `reports/pend-alc` |
| Hold inventory               | `ARS_NL_TBL_HOLD_TRACKING`    | `hold_dashboard` endpoint |
| Allocation sessions          | `SESSION_ID` on `ARS_PEND_ALC` + `ARS_ALLOC_PARKED` | `listing_allocator.py` |

The dashboard does **not** duplicate Hold Dashboard or Pend Alc Overview — it summarizes them and provides drill-through into the same underlying records, with deep-links back to the specialist pages.

---

## 2. Decisions confirmed with user (2026-05-22)

1. **Scope:** New unified page at `/ars-dashboard`. Existing `DashboardPage` and `HoldDashboardPage` remain untouched.
2. **Drill hierarchy (time):** **Date → Session → Detail.** Top level lists dates; clicking a date expands the sessions executed that day; clicking a session reveals row-level alloc/pend/hold detail.
3. **Gap definition:** **Unshipped pending — `PEND_QTY = ALLOC_QTY − DO_QTY > 0`** in `ARS_PEND_ALC` where `IS_CLOSED = 0`. Already persisted, no new computation required.
4. **Global picker (rev 2):** A two-step **Date dropdown → Session dropdown** at the top of the page; the session dropdown is populated from `/ars-dashboard/sessions-by-date` so only sessions executed on the picked date are listed. Picking a date alone scopes the whole page to that day; picking a session further narrows it to that single run. Cleared → "All dates / All sessions" (date range falls back to last 7 days).
5. **Product drill (rev 2):** A dedicated **Product Drill** tab with a **path toggle** — `MAJ_CAT → Store → OPT → VAR` (default) **or** `Store → MAJ_CAT → OPT → VAR`. Each level is a **dropdown** populated by the parent's selection. Each level also shows a small distribution chart and a rollup KPI strip. Backed by the existing listing endpoints (`store-by-majcat`, `opt-summary`, `var-summary`) — no new SQL required for the drill itself.
6. **Charts (rev 2):** The Overview tab renders **6 charts** (alloc by OPT_TYPE pie, alloc by RDC pie, top/bottom MAJ_CATs bar, top/bottom Stores bar, alloc-vs-pend-vs-hold trend over the picked window, gap qty by MAJ_CAT bar). All charts re-render when the global picker or scope filters change.

---

## 3. Page layout

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ ARS Dashboard                                              [Date range ▾]  [↻ Refresh]│
│ High-level allocation analytics — date, session, hierarchy                            │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ FILTER BAR (global — applies to every tab)                                            │
│  Date ▾ │ Session ▾ │ MAJ_CAT (multi) ▾ │ Store ▾ │ RDC ▾ │ Drill path: [MJ→ST│ST→MJ]│
├────────────────────────────────────────────────────────────────────────────────────────┤
│  KPI 1 Allocated    KPI 2 Pending     KPI 3 On Hold     KPI 4 Open Gaps               │
│   12,438 pcs         3,201 pcs         1,876 pcs         428 rows                     │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ [Overview] [Product Drill] [Date & Session] [Hold] [Pending Alloc] [Gap Report]       │
├────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                       │
│   Active tab content — re-renders when filter bar changes                             │
│                                                                                       │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### 3.0 Global filter bar (above tabs, persisted in URL query string)

| Field        | Source                                           | Behaviour                                                                 |
|--------------|--------------------------------------------------|---------------------------------------------------------------------------|
| Date         | `GET /ars-dashboard/dates` (last 60 days)        | Pick one date → narrows the page to that day. Empty = last 7 days.        |
| Session      | `GET /ars-dashboard/sessions-by-date?date=…`     | Disabled until a date is picked. Pick one session → narrows to that run.  |
| MAJ_CAT      | `GET /listing/config` → `maj_cats`               | Multi-select. Empty = all.                                                |
| Store (WERKS)| `GET /listing/config` → `stores`                 | Multi-select. Empty = all.                                                |
| RDC          | `GET /listing/config` → `rdcs`                   | Multi-select. Empty = all.                                                |
| Drill path   | local toggle                                     | `MJ→ST` (default) or `ST→MJ` — only affects the Product Drill tab.        |

Filter state is mirrored to URL params (`?date=2026-05-22&sid=AW26-001&mc=MENWEAR,WOMENWEAR&path=mj2st`) so a planner can deep-link a view.

### 3.1 KPI strip (always visible)
Four cards driven by a single aggregation endpoint. Each card click jumps to the relevant tab pre-filtered.

| Card        | Source query                                                             | Click target            |
|-------------|--------------------------------------------------------------------------|-------------------------|
| Allocated   | `SUM(ALLOC_QTY)` + `COUNT(DISTINCT SESSION_ID)` over date range          | Date & Session tab      |
| Pending     | `SUM(PEND_QTY)` where `IS_CLOSED=0`                                      | Pending Alloc tab       |
| On Hold     | `SUM(HOLD_REM)` + `COUNT(DISTINCT VAR_ART)` where `IS_CLOSED=0`          | Hold Inventory tab      |
| Open Gaps   | `COUNT(*)` + `COUNT(DISTINCT ARTICLE)` where `PEND_QTY > 0`              | Gap Report tab          |

### 3.2 Tab 1 — Overview (default, rev 2)

The new default tab. **6 charts in a 3 × 2 grid**, every chart respects the global filter bar.

| # | Chart                                  | Library                | Source                                                |
|---|----------------------------------------|------------------------|-------------------------------------------------------|
| 1 | Alloc by OPT_TYPE (Pie — RL/TBC/TBL)   | recharts PieChart      | reuse `listingAPI.summary().by_opt_type`              |
| 2 | Alloc by RDC (Pie)                     | recharts PieChart      | reuse `listingAPI.summary().by_rdc`                   |
| 3 | Top/Bottom MAJ_CAT by alloc qty (Bar)  | recharts BarChart      | reuse `listingAPI.summary().by_maj_cat` + sort        |
| 4 | Top/Bottom Stores by alloc qty (Bar)   | recharts BarChart      | reuse `listingAPI.summary().by_werks`                 |
| 5 | Alloc vs Pending vs Hold (last 7d)     | recharts stacked Bar   | new `/ars-dashboard/trend?days=7`                     |
| 6 | Gap qty by MAJ_CAT (Bar, rose)         | recharts BarChart      | new `/ars-dashboard/gap?group_by=majcat`              |

Each chart has a small header with **Top/Bottom N** toggle and **Maximize** button (opens a modal with the full version) — same pattern already in use on ListingPage (`ChartCard` component at `ListingPage.jsx:2493+`).

### 3.3 Tab 2 — Product Drill (rev 2 — the new high-level drill)

A single page with two big elements:

**(a) Path toggle (top)**
```
Drill path:  [ MAJ_CAT  →  Store  →  OPT  →  VAR ]    [ Store  →  MAJ_CAT  →  OPT  →  VAR ]
                  ●  active                                  ○
```

**(b) Four dropdown cards in a row** — each card represents one level. Dropdown of the card is populated by the parent's pick; a small KPI strip + mini-chart sits inside the card showing the distribution within the parent.

```
┌──── L1: MAJ_CAT ─────┐ ┌──── L2: Store ───────┐ ┌──── L3: OPT ─────────┐ ┌──── L4: VAR ────────┐
│ [MENWEAR        ▾]  │ │ [V004           ▾]  │ │ [10298110/RED   ▾]  │ │ (table — VAR × SZ) │
│ 12,438 alloc        │ │   640 alloc          │ │   60 alloc           │ │ row per VAR×SZ      │
│ 287 stores · 142 art│ │   28 articles        │ │   4 sizes            │ │                     │
│ ▮▮▮▮▮▮▮ bar          │ │ ▮▮▮▮▮▮ bar           │ │ ▮▮▮ bar              │ │ 36 / 24 / 18 / 12   │
└──────────────────────┘ └──────────────────────┘ └──────────────────────┘ └─────────────────────┘
```

Behaviour:
- Picking L1 fills L2's dropdown; picking L2 fills L3's; picking L3 reveals L4 detail.
- Path = **ST → MJ** flips the order — L1 becomes Store, L2 becomes MAJ_CAT — but L3/L4 stay OPT/VAR.
- Below the four cards: a wide chart showing the distribution **at the deepest selected level** (e.g. "Stores within MENWEAR by alloc qty" with the picked store highlighted).
- All four dropdowns reuse existing endpoints:

| Level | Endpoint                                                           |
|-------|--------------------------------------------------------------------|
| MAJ_CAT picks  | `listingAPI.summary().by_maj_cat` (already aggregated)             |
| Store picks    | `listingAPI.storeByMajCat(majCat, rdc)` — exists                   |
| OPT picks      | `listingAPI.optSummary(majCat, rdc, werks)` — exists               |
| VAR detail     | `listingAPI.varSummary(majCat, werks, genArt, clr, rdc)` — exists  |

Reverse path (Store → MAJ_CAT) reuses the same endpoints — just shuffles the argument order.

**Important**: this tab reads from `ARS_LISTING_WORKING` + `ARS_ALLOC_WORKING` — i.e. the **current** or **picked** session. When the global Session dropdown is set, the tab calls the same endpoints unchanged (they already read the working tables that the picked session created); when no session is picked, the tab reads the latest working snapshot (matches what Listing page shows).

### 3.4 Tab 3 — Date & Session (drill-through)

**Breadcrumb-style navigation.** Three levels, each level is a table that swaps in place when a row is clicked.

#### Level 1 — Dates
| Date | Sessions | Allocated Qty | Stores | DO Qty | Pending Qty | Hold Qty | Status |
|------|----------|---------------|--------|--------|-------------|----------|--------|
| Sorted desc by date. Status badge: `● all closed` (green), `● partial` (amber), `● open` (rose). |

#### Level 2 — Sessions on selected date
| Session ID | MAJ_CAT | RDC | Articles | Stores | Allocated | DO | Pending | Hold | Mode | Status |
|------------|---------|-----|----------|--------|-----------|----|---------|------|------|--------|
| One row per `SESSION_ID`. `Mode` = AUTO/MANUAL from `ALLOC_MODE`. |

#### Level 3 — Row-level detail for selected session
| Article | Store | Size | Allocated | DO | Pending | Hold | Gap % | Status |
|---------|-------|------|-----------|----|---------|------|-------|--------|
| Filter row above: Article, Store, "Only open", "Only with gaps". Export to Excel button. |

### 3.5 Tab 4 — Hold Inventory
Compact KPIs (3) + bar chart by RDC + top-15 articles table. **"Open full Hold Dashboard →"** link to existing `/reports/hold` for deeper analysis. No duplicate logic — this tab calls existing `holdDashboardAPI.summary` / `byRdc` / `byArticle`.

### 3.6 Tab 5 — Pending Allocation
KPIs (3: open rows, total pend qty, avg age days) + bar chart of pend qty by session + filterable table.

Filters: `SESSION_ID`, `RDC`, `ST_CD`, `ALLOC_MODE`, age bucket (0–7 / 8–30 / 31+ days).
Columns: `SESSION_ID, RDC, ST_CD, ARTICLE_NUMBER, ALLOC_QTY, DO_QTY, PEND_QTY, %_PENDING, AGE_DAYS, STATUS`.
Row click → opens the row in the existing `/pend-alc/overview` page with that row pre-selected.

### 3.7 Tab 6 — Gap Report
Banner explaining the definition: *"Gap = ALLOC_QTY − DO_QTY where IS_CLOSED = 0."*

| KPI 1 | KPI 2 | KPI 3 | KPI 4 |
|-------|-------|-------|-------|
| Open gap rows | Total gap qty | Top RDC by gap | Oldest gap age (days) |

Two views, toggle:
- **By RDC × Article** — grouped table, `SUM(PEND_QTY)`, sorted by gap qty desc.
- **By Session × Article** — same data grouped by SESSION_ID.

Export Excel button (reuse the pattern from `reports.py:65-100`).

---

## 4. Backend — endpoints

All new endpoints under a new module `backend/app/api/v1/endpoints/ars_dashboard.py`, registered in `router.py` after the Pend Alc router. Read-only, no writes.

### 4.0 New endpoints

| Method | Path                                              | Purpose                                                                 |
|--------|---------------------------------------------------|-------------------------------------------------------------------------|
| GET    | `/ars-dashboard/summary?date=&sid=&mc=&werks=&rdc=` | Returns the 4 KPI values for the current filter scope.                |
| GET    | `/ars-dashboard/dates?from=&to=`                  | Date list with rollup counts (Date & Session tab L1 + Date dropdown).   |
| GET    | `/ars-dashboard/sessions-by-date?date=`           | **(new in rev 2)** Sessions executed on the given date — feeds the global Session dropdown. |
| GET    | `/ars-dashboard/sessions?date=`                   | Date & Session tab L2 — full rollup per session.                        |
| GET    | `/ars-dashboard/session-detail?session_id=&page=` | Date & Session tab L3 — paged row-level detail for a session.           |
| GET    | `/ars-dashboard/trend?days=7`                     | **(new in rev 2)** Alloc / Pend / Hold totals per day for stacked-bar trend chart. |
| GET    | `/ars-dashboard/pending?…`                        | Pending tab — filterable list (reuses Pend Alc service).                |
| GET    | `/ars-dashboard/gap?group_by=rdc_article|session_article|majcat&…` | Gap report rows (now also supports `majcat` grouping for chart 6).|
| GET    | `/ars-dashboard/gap/export?…`                     | Excel download (mirrors `reports.py` pattern).                          |

### 4.1 Endpoints reused from `listing.py` (NO new code)

These power the Overview charts and the entire Product Drill tab. They already exist and are battle-tested on the Listing page.

| Frontend wrapper              | Backend route                  | Used for                                          |
|-------------------------------|--------------------------------|---------------------------------------------------|
| `listingAPI.summary()`        | `GET /listing/summary`         | Overview charts 1–4 (OPT_TYPE pie, RDC pie, top MAJ_CATs, top stores). Returns `by_opt_type`, `by_rdc`, `by_maj_cat`, `by_werks`. |
| `listingAPI.contribution(mc)` | `GET /listing/contribution`    | Live RDC stock-vs-alloc when MAJ_CAT filter changes. |
| `listingAPI.storeByMajCat`    | `GET /listing/store-by-majcat` | Drill L2 — store picks under a chosen MAJ_CAT.    |
| `listingAPI.optSummary`       | `GET /listing/opt-summary`     | Drill L3 — OPT picks under (MAJ_CAT, RDC, WERKS). |
| `listingAPI.varSummary`       | `GET /listing/var-summary`     | Drill L4 — VAR × SZ detail under one OPT.         |
| `listingAPI.sessions()`       | `GET /listing/sessions`        | Source for `sessions-by-date` (we wrap & filter). |

### 4.2 Reuse for other tabs
- **Hold tab** → call existing `holdDashboardAPI.summary` / `byRdc` / `byArticle` directly. No new backend.
- **Pending list** → wraps `pend_alc_service.list_pending()` with the dashboard filter set.
- **Excel export** → reuse `pandas.DataFrame.to_excel` pattern from `backend/app/api/v1/endpoints/reports.py:65-100`.

### 4.2 SQL sketch (Level 1 — Dates)
```sql
SELECT
    CAST(APPROVED_AT AS DATE)        AS run_date,
    COUNT(DISTINCT SESSION_ID)       AS sessions,
    SUM(ALLOC_QTY)                   AS allocated_qty,
    SUM(DO_QTY)                      AS do_qty,
    SUM(PEND_QTY)                    AS pending_qty,
    COUNT(DISTINCT ST_CD)            AS stores,
    SUM(CASE WHEN IS_CLOSED = 0 THEN 1 ELSE 0 END) AS open_rows
FROM ARS_PEND_ALC
WHERE APPROVED_AT BETWEEN :from AND :to
GROUP BY CAST(APPROVED_AT AS DATE)
ORDER BY run_date DESC;
```
Index already exists on `SESSION_ID`; `APPROVED_AT` filter benefits from `(ALLOC_MODE, IS_CLOSED)` only partially — consider adding `IX_ARS_PEND_ALC_APPROVED_AT` if EXPLAIN shows a scan on large windows.

---

## 5. Frontend — new files

| File                                                | Purpose                                                                          |
|-----------------------------------------------------|----------------------------------------------------------------------------------|
| `frontend/src/pages/ArsDashboardPage.jsx`           | Main page — global filter bar, KPI strip, tab router, URL-state sync.            |
| `frontend/src/pages/arsDashboard/FilterBar.jsx`     | **(new)** Date / Session / MAJ_CAT / Store / RDC / Path toggle.                  |
| `frontend/src/pages/arsDashboard/OverviewTab.jsx`   | **(new)** 6 recharts charts + Top/Bottom toggles + maximize.                     |
| `frontend/src/pages/arsDashboard/DrillTab.jsx`      | **(new)** 4-level dropdown drill, path toggle, per-level mini-chart.             |
| `frontend/src/pages/arsDashboard/DateSessionTab.jsx`| Date → Session → Detail breadcrumb drill.                                        |
| `frontend/src/pages/arsDashboard/HoldTab.jsx`       | Compact view, deep-link to `/reports/hold`.                                      |
| `frontend/src/pages/arsDashboard/PendingTab.jsx`    | Filterable pending table.                                                        |
| `frontend/src/pages/arsDashboard/GapTab.jsx`        | Gap rollup + export.                                                             |
| `frontend/src/pages/arsDashboard/ChartCard.jsx`     | Shared wrapper — title, Top/Bottom toggle, Maximize button (mirrors ListingPage). |

### 5.1 API client additions — `frontend/src/services/api.js`
```js
export const arsDashboardAPI = {
  summary:        (params) => api.get('/ars-dashboard/summary',          { params }),
  dates:          (params) => api.get('/ars-dashboard/dates',            { params }),
  sessionsByDate: (date)   => api.get('/ars-dashboard/sessions-by-date', { params: { date } }),
  sessions:       (params) => api.get('/ars-dashboard/sessions',         { params }),
  sessionDetail:  (params) => api.get('/ars-dashboard/session-detail',   { params }),
  trend:          (params) => api.get('/ars-dashboard/trend',            { params }),
  pending:        (params) => api.get('/ars-dashboard/pending',          { params }),
  gap:            (params) => api.get('/ars-dashboard/gap',              { params }),
  exportGap:      (params) => api.get('/ars-dashboard/gap/export',       { params, responseType: 'blob' }),
}
// NOTE: Overview charts and Drill tab reuse listingAPI.summary / contribution /
// storeByMajCat / optSummary / varSummary directly — no wrapping needed.
```

### 5.2 Routing — `frontend/src/App.jsx`
```jsx
const ArsDashboardPage = lazy(() => import('@/pages/ArsDashboardPage'))
// inside <Routes>:
<Route path="/ars-dashboard" element={<ProtectedRoute permission="ALLOC_READ"><ArsDashboardPage/></ProtectedRoute>} />
```

### 5.3 Sidebar — `frontend/src/components/layout/Sidebar.jsx:13-19`
Add as a top-level item right under "Dashboard":
```jsx
{ label: 'ARS Dashboard', path: '/ars-dashboard', icon: LayoutGrid, permission: 'ALLOC_READ' },
```

---

## 6. Permissions

Reuse existing `ALLOC_READ`. No new permission codes — anyone who can see allocations can see this dashboard. Gap export reuses `REPORTS_PEND_ALC` (same as the existing Pend Alc Report).

---

## 7. Phased rollout (rev 2)

| Phase | Scope                                                                                                            | Effort   |
|-------|------------------------------------------------------------------------------------------------------------------|----------|
| **1** | Backend: `summary`, `dates`, `sessions-by-date`, `sessions`, `session-detail`, `trend`. Frontend: page shell, **filter bar** (URL sync), KPI strip, Date & Session tab drill. | 1.5 days |
| **2** | **Overview tab** — 6 charts, ChartCard component, Top/Bottom toggles, maximize modal.                            | 1.0 day  |
| **3** | **Product Drill tab** — path toggle, 4 dropdown cards, per-level mini-charts, deepest-level table. Pure frontend (reuses existing listing endpoints). | 1.0 day  |
| **4** | Hold tab + Pending tab (existing APIs).                                                                          | 0.5 day  |
| **5** | Gap Report tab (with `group_by=majcat` for chart 6) + Excel export.                                              | 0.5 day  |
| **6** | Sidebar entry, permission wiring, smoke test against HOPC560.                                                    | 0.25 day |

Total ~ 4.75 dev-days end-to-end.

---

## 8. Out of scope (explicitly)

- Editing pend_alc rows from this dashboard (use `/pend-alc/manual-entry`).
- BDC scheduling (use `/pend-alc/schedule`).
- Approving parked allocations (use Allocations page).
- Trend / time-series charts beyond the date list — defer to a v2 if needed.

---

## 9. Open questions for review

1. Should the date range default to **today only**, **last 7 days**, or **last 30 days**? (Mockup defaults to last 7 days.)
2. Should Tab 4 (Gap Report) include the *Held-back inventory blocking allocation* dimension later, or is the unshipped definition sufficient long-term?
3. Do we need a CSV export in addition to Excel, or is Excel sufficient (matches existing reports)?

---

## 10. How to review the mockup

Open `D:\ARS_PROD\ars_prod\ars_dashboard_mockup.html` in any browser. It is fully self-contained (Tailwind via CDN, vanilla JS for tab switching and drill-through). All numbers are sample data. Click date rows → session rows → detail to see the drill flow.
