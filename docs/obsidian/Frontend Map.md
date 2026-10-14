---
title: Frontend Map
tags: [ars, frontend, react]
updated: 2026-10-14
---

# Frontend Map

React + Vite SPA (`frontend/`), served static from `backend/static` in prod. Default route `/` → `/ars-dashboard`. All routes wrapped in `Layout` behind `ProtectedRoute` (permission or `superadminOnly`); pages lazy-loaded. Sidebar menu registry in `navRegistry.js` (`SECTIONS`, shared by Sidebar and Settings→Roles). `ModuleGate` derives URL gating from that registry, so a removed section must also lose its routes.

## Route map (by subsystem)
| Subsystem | Route → Page |
|-----------|--------------|
| Dashboard/Review | `/ars-dashboard`→ArsDashboardPage · `/alc-review`→AlcReviewPage |
| Data Mgmt | `/tables`, `/tables/create`, `/tables/:t`, `/upload`, `/export`, `/jobs`, `/editor`, `/data-dictionary` |
| [[MSA Stock Calculation\|MSA]] | `/msa`→MSAStockCalculationPage |
| [[Grid Builder\|Grid]]/[[Merge Rules\|Merge]] | `/data-prep/store-stock`→GridBuilderPage · `/data-prep/merge-rules`→MergeRulesPage |
| [[Listing]] | `/data-prep/listing`→ListingPage · `/data-prep/listing/logs`→ListingLogsPage |
| Data Validation | `/data-validation/store-sloc`→StoreStockPage · `/data-validation/checklist`→ChecklistPage |
| Reports | `/reports/generation`→ReportGenerationPage · `/reports/pend-alc` · `/reports/hold`→HoldDashboardPage · `/reports/gap` |
| [[Pending Allocation and Hold\|PendAlc]] | `/pend-alc/{overview,manual-entry,adhoc-close,open-bdc,do-entry,reco,schedule,schedule-audit,operations}` |
| Admin/Platform | `/settings`, `/settings/{tables,users,roles,rls,audit,tempdb,activity-log}` |

Legacy `/admin/*` → `/settings/*`; `/reports/open-bdc` is a back-compat alias.

**Removed 2026-10-14** (see [[ARS Work Log]]): `/contribution/*` (Contribution %), `/auto-cont/*` (Auto Cont %), `/alc-fixture/*` (ALC_Fixture placeholder), `/trends/*`, `/pt/*` (Project Tracker), `/manual/*` and `/process/*` (Training Manual viewer). Routes were deleted together with their nav sections.

## API client (`frontend/src/services/api.js`)
Axios, 5-min timeout, JWT via `localStorage`, 401→refresh→`/login`, `{quiet:true}` for background pollers. Exports (one per subsystem):
`authAPI` · `usersAPI`/`rolesAPI`/`rlsAPI` · `tablesAPI` · `dataAPI` · `uploadAPI` · `msaAPI` (config/calculate/pivot/save, sequences, FRESH/GRT sloc settings) · `auditAPI`/`settingsAPI` · `storeStockAPI` · `gridBuilderAPI` (grids, run/run-all, sec-cap growth matrix) · `mergeRulesAPI` · `dataDictionaryAPI` · `listingAPI` (generate/preview/summary/export, parking, alloc progress/retry, drill-downs, parked approve/reject, history) · `arsDashboardAPI` · `gapReportAPI` · `lookupArtMasterAPI` · `checklistAPI` · `maintenanceAPI` (tempdb, DB files, reset) · `reportsAPI` · `reportGenAPI` · `holdDashboardAPI` · `pendAlcAPI` · `activityLogAPI`.

## State (`frontend/src/store/`)
Only **`authStore.js`** (Zustand): `user`, `isAuthenticated`, `permissions[]`, `roles[]`, `loginTime`; actions `login`/`fetchUser`/`logout`; helpers `hasPermission` (SUPER_ADMIN bypass), `hasRole`, `isSuperAdmin`. Tokens in `localStorage`. No other global stores — page state is component-local. Theme: `theme/colors.js` + Tailwind. **Client-side gating is convenience only — enforcement is server-side.**

## Cross-links
[[Platform and Infrastructure]] (backend routes these call) · [[Pipeline Overview]].
