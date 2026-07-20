---
title: Dashboards Trends and Admin Tools
tags: [ars, dashboards, trends, admin]
updated: 2026-07-17
---

# Dashboards, Trends & Admin Tools

The read-side analytics and operator/admin tooling that sit alongside the [[Pipeline Overview|pipeline]] but don't produce allocations. Consolidates the pages/routers not covered by a dedicated stage note. Detail on any of these can graduate to its own note using the `Module Note` template.

## Analytics / dashboards
| Tool | Router | Frontend | Purpose |
|------|--------|----------|---------|
| ARS Dashboard | `/ars-dashboard` | ArsDashboardPage | Unified analytics: summary / breakdown / trend / pending / gap + hierarchical drill-down + hold/pivot |
| Gap Report | `/gap-report` | GapReportPage | Multi-category algorithm-review GAP tiles + export |
| Hold Dashboard | `/hold-dashboard` | HoldDashboardPage | HOLD_QTY review — see [[Pending Allocation and Hold]] |
| Trends | `/trends` | Trend*Page (`/trends/{dashboard,upload,review,admin}`) | Trend tables/schema/upload/review/admin |
| Pend-Alc Report | `/reports` | PendAlcReportPage | Pending-allocation report data + download |

## Operator / admin tools
| Tool | Router | Frontend | Purpose |
|------|--------|----------|---------|
| Data Dictionary | `/data-dictionary` | DataDictionaryPage | `ARS_DATA_DICTIONARY` CRUD — column-level reference for every ARS column |
| Checklist | `/checklist` | ChecklistPage | `ARS_CHECKLIST` — tables the planner must keep current pre-allocation |
| Activity Log | `/activity-log` | DailyActivityLogPage | Superadmin — rolls `audit_log` + git commits into daily YES/NO-reviewable pointers |
| Project Tracker | `/project-tracker` | pt/PT*Page | Projects tree / tasks / dashboard |
| Table Management | `/tables` | TableManagementPage, TablesPage, DataEditorPage | Dynamic CREATE/ALTER/soft-delete, data grid, export/truncate jobs — see [[Platform and Infrastructure]] |
| Lookup Art Master | `/lookup-art-master` | LookupArtMasterPage | Article-master lookup / preview / download |
| SLOC Validation | `/sloc-validation` | StoreStockPage | Store-SLOC validation settings |
| Settings / Users / Roles / RLS / Audit / TempDB | `/settings`, `/users`, `/roles`, `/rls`, `/audit`, `/maintenance` | SettingsPage + admin pages | Platform admin — see [[Platform and Infrastructure]] |

## Notes
- All read dashboards honour MAJ_CAT **RLS** (see [[Platform and Infrastructure]]).
- The Data Dictionary is the column-level companion to this vault and the `/manual/*` dossiers — keep it in sync when engine columns change ([[Known Risks and Doc Drift]] maintenance convention).

## Cross-links
[[Frontend Map]] (full route/API list) · [[Platform and Infrastructure]] · [[Review and Approve]] · [[Pending Allocation and Hold]].
