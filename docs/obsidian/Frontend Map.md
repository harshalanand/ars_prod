---
title: Frontend Map
tags: [ars, frontend, react]
updated: 2026-07-17
---

# Frontend Map

React + Vite SPA (`frontend/`), served static from `backend/static` in prod. Default route `/` → `/ars-dashboard`. All routes wrapped in `Layout` behind `ProtectedRoute` (permission or `superadminOnly`); pages lazy-loaded. Sidebar grouping in `components/layout/Sidebar.jsx` (`SECTIONS` registry).

## Route map (by subsystem)
| Subsystem | Route → Page |
|-----------|--------------|
| Dashboard/Review | `/ars-dashboard`→ArsDashboardPage · `/alc-review`→AlcReviewPage |
| Data Mgmt | `/tables`, `/tables/create`, `/tables/:t`, `/upload`, `/export`, `/jobs`, `/editor`, `/data-dictionary` |
| [[MSA Stock Calculation\|MSA]] | `/msa`→MSAStockCalculationPage |
| [[Grid Builder\|Grid]]/[[Merge Rules\|Merge]] | `/data-prep/store-stock`→GridBuilderPage · `/data-prep/merge-rules`→MergeRulesPage |
| [[Listing]] | `/data-prep/listing`→ListingPage · `/data-prep/listing/logs`→ListingLogsPage |
| [[Contribution and CONT\|Contrib]] | `/contribution/{presets,mappings,execute,review}` |
| Auto-Contrib (superadmin) | `/auto-cont/{presets,mappings,execute,jobs,review}` |
| ALC_Fixture (superadmin) | `/alc-fixture/{tunables,execute,review,dashboard,jobs}` — score-based engine (mig 020) |
| Data Validation | `/data-validation/store-sloc`→StoreStockPage · `/data-validation/checklist`→ChecklistPage |
| Trends | `/trends/{dashboard,upload,review,admin}` |
| Reports | `/reports/generation`→ReportGenerationPage · `/reports/pend-alc` · `/reports/hold`→HoldDashboardPage · `/reports/gap` |
| [[Pending Allocation and Hold\|PendAlc]] | `/pend-alc/{overview,manual-entry,adhoc-close,open-bdc,do-entry,reco,schedule,schedule-audit,operations}` |
| Project Tracker | `/pt`, `/pt/projects`, `/pt/projects/:id`, `/pt/my-tasks` |
| Manual | `/manual/gallery` · `/manual/:module`→TrainingManualPage (renders `public/docs/manual/*.md`) |
| Admin/Platform | `/settings`, `/settings/{tables,users,roles,rls,audit,tempdb,activity-log}` |

Legacy `/admin/*` → `/settings/*`; `/process/*` and `/reports/open-bdc` are back-compat aliases.

## API client (`frontend/src/services/api.js`)
Axios, 5-min timeout, JWT via `localStorage`, 401→refresh→`/login`, `{quiet:true}` for background pollers. Exports (one per subsystem):
`authAPI` · `usersAPI`/`rolesAPI`/`rlsAPI` · `tablesAPI` · `dataAPI` · `uploadAPI` · `msaAPI` (config/calculate/pivot/save, sequences, FRESH/GRT sloc settings) · `auditAPI`/`settingsAPI` · `storeStockAPI` · `gridBuilderAPI` (grids, run/run-all, sec-cap growth matrix) · `mergeRulesAPI` · `dataDictionaryAPI` · `listingAPI` (generate/preview/summary/export, parking, alloc progress/retry, drill-downs, parked approve/reject, history) · `arsDashboardAPI` · `gapReportAPI` · `lookupArtMasterAPI` · `contribAPI` · `autoContAPI` · `checklistAPI` · `trendsAPI` · `maintenanceAPI` (tempdb, DB files, reset) · `reportsAPI` · `reportGenAPI` · `holdDashboardAPI` · `pendAlcAPI` · `ptAPI` · `activityLogAPI`.

## State (`frontend/src/store/`)
Only **`authStore.js`** (Zustand): `user`, `isAuthenticated`, `permissions[]`, `roles[]`, `loginTime`; actions `login`/`fetchUser`/`logout`; helpers `hasPermission` (SUPER_ADMIN bypass), `hasRole`, `isSuperAdmin`. Tokens in `localStorage`. No other global stores — page state is component-local. Theme: `theme/colors.js` + Tailwind. **Client-side gating is convenience only — enforcement is server-side.**

## Cross-links
[[Platform and Infrastructure]] (backend routes these call) · [[Pipeline Overview]].
