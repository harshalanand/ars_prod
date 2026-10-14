// Sidebar menu registry — the single source for the Sidebar AND for
// Settings → Roles, which renders one permission card per father menu.
// `module` / `permission` on a section = its MOD_* switch; `permission`
// on an item = the page's own gate. Add a menu here and both stay in step.
// `perms` on an item (optional) = any-of page gate: the item shows, and its
// route opens, when the role holds ANY of these codes. `permission` stays the
// one code Settings → Roles ticks for that item. Use itemAllowed() to test.
import {
  LayoutDashboard, Table2, Upload, PackageCheck, Users, Shield, Eye, ScrollText,
  ChevronLeft, ChevronRight, Box, ChevronDown, FolderOpen, FilePlus, FileUp, Plus,
  FileDown, Edit3, Settings, Database, Columns, BarChart3, Cpu, Cog, Activity,
  Clock, Truck, FileText, ClipboardCheck, ClipboardList, ShieldCheck, LayoutGrid, Search, List,
  HardDrive, Lock, CalendarDays, History, GitMerge,
  AlertTriangle, BookOpen, GitBranch, Sliders, Layers, ListOrdered,
  XCircle, Store, Sparkles, Container, Server, Ruler,
  DatabaseZap, Snowflake, RefreshCw, FileSpreadsheet, PlugZap,
} from 'lucide-react'

// Top-level father menus with no submenu: `module` is their MOD_* switch.
export const navItems = [
  { label: 'ARS Dashboard', path: '/ars-dashboard', icon: LayoutGrid, permission: 'ALLOC_READ', module: 'MOD_ARS_DASHBOARD' },
  { label: 'Alloc Review', path: '/alc-review', icon: History, permission: 'ALLOC_READ', module: 'MOD_ALC_REVIEW' },
]

// Data Management submenu
const dataManagementItems = [
  { label: 'All Tables', path: '/tables', icon: Table2, permission: 'DATA_VIEW', end: true },
  { label: 'Create Table', path: '/tables/create', icon: FilePlus, permission: 'TABLE_CREATE' },
  { label: 'Upload Data', path: '/upload', icon: FileUp, permission: 'DATA_UPLOAD' },
  { label: 'Export Data', path: '/export', icon: FileDown, permission: 'DATA_EXPORT' },
  { label: 'Jobs Dashboard', path: '/jobs', icon: Activity, permission: 'JOBS_VIEW' },
  { label: 'Data Editor', path: '/editor', icon: Edit3, permission: 'DATA_EDITOR' },
  { label: 'Data Dictionary', path: '/data-dictionary', icon: BookOpen, permission: 'PAGE_DATA_DICTIONARY' },
]

// Data Preparation submenu
const dataPreparationItems = [
  { label: 'MSA Stock Calculation', path: '/msa', icon: BarChart3, permission: 'MSA_VIEW' },
  { label: 'Grid Builder', path: '/data-prep/store-stock', icon: LayoutGrid, permission: 'GRID_VIEW' },
  { label: 'Merge Rules', path: '/data-prep/merge-rules', icon: GitMerge, permission: 'GRID_VIEW' },
  { label: 'Listing', path: '/data-prep/listing', icon: List, permission: 'PAGE_LISTING' },
]

// Bin Allocation submenu — greedy best-fit bin→store engine (no bin split),
// fixed/cascading eligibility thresholds. ARS-native (server-side, job-driven,
// audited); specced in the BRD/FSD (Bin-Allocation). Pages WIP.
// GRT ALC — Bin-to-Bin Transfer. The numbered pages are the five steps in
// order; the rest are tools. Routes keep the /bin-alloc/ prefix so existing
// links and the MOD_GRT_ALC permission carry over.
const binAllocItems = [
  { label: 'Overview',                path: '/bin-alloc/overview',   icon: LayoutDashboard, permission: 'PAGE_GRT_OVERVIEW' },
  { label: '1 · Upload Data',         path: '/bin-alloc/upload',     icon: FileUp, permission: 'PAGE_GRT_UPLOAD' },
  { label: '2 · Settings',            path: '/bin-alloc/settings',   icon: Sliders, permission: 'PAGE_GRT_SETTINGS' },
  { label: '3 · Build MBQ',           path: '/bin-alloc/mbq',        icon: Layers, permission: 'PAGE_GRT_MBQ' },
  { label: '4 · Run Allocation',      path: '/bin-alloc/run',        icon: Cpu, permission: 'PAGE_GRT_RUN' },
  { label: '5 · Sessions & Pick List', path: '/bin-alloc/sessions',  icon: ClipboardCheck, permission: 'PAGE_GRT_SESSIONS' },
  { label: 'Gap Report',              path: '/bin-alloc/gap-report', icon: AlertTriangle, permission: 'PAGE_GRT_GAP_REPORT' },
  { label: 'Store × Article Extract', path: '/bin-alloc/extract',    icon: Search, permission: 'PAGE_GRT_EXTRACT' },
  { label: 'Help',                    path: '/bin-alloc/help',       icon: BookOpen, permission: 'PAGE_GRT_HELP' },
]

// 1SZ ALC — size allocation of broken-size RDC stock to the stores short of
// those sizes. The menu follows the work: Overview → Prepare (tabs Supply,
// Demand, Gap) → Plan a run → Sessions; then Settings and Help.
// Overview needs only the module. Settings uses PAGE_1SZ_BASE. Prepare opens
// with ANY of its tab codes (Supply PAGE_1SZ_BASE, Demand PAGE_1SZ_REQ, Gap
// PAGE_1SZ_GAP) and shows only the tabs the role holds; the old URLs below keep
// their single code. No new permission code. Spec: manual/sz1_alloc.md.
const sz1AllocItems = [
  { label: 'Overview',                  path: '/sz1-alloc',          icon: LayoutDashboard, end: true },
  { label: 'Prepare',                   path: '/sz1-alloc/prepare',  icon: Layers, permission: 'PAGE_1SZ_BASE',
    perms: ['PAGE_1SZ_BASE', 'PAGE_1SZ_REQ', 'PAGE_1SZ_GAP'] },
  { label: 'Plan a run',                path: '/sz1-alloc/run',      icon: Cpu, permission: 'PAGE_1SZ_RUN' },
  { label: 'Sessions',                  path: '/sz1-alloc/sessions', icon: ClipboardCheck, permission: 'PAGE_1SZ_SESSIONS' },
  { label: 'Settings',                  path: '/sz1-alloc/settings', icon: Sliders, permission: 'PAGE_1SZ_BASE' },
  { label: 'Help',                      path: '/sz1-alloc/help',     icon: BookOpen, permission: 'PAGE_1SZ_HELP' },
]

// Adhoc submenu
const adhocItems = [
  { label: 'Lookup Art Master', path: '/data-prep/lookup-art-master', icon: Search, permission: 'LOOKUP_VIEW' },
]

// Reports submenu
const reportsItems = [
  { label: 'Report Generation', path: '/reports/generation', icon: FileText, permission: 'PAGE_REPORT_GENERATION' },
  { label: 'Hold Dashboard',  path: '/reports/hold',     icon: Lock, permission: 'PAGE_HOLD_DASHBOARD' },
  { label: 'GAP Report',      path: '/reports/gap',      icon: AlertTriangle, permission: 'ALLOC_READ' },
  { label: 'UPC Store Tracking', path: '/reports/upc-tracking', icon: Store, permission: 'PAGE_UPC_TRACKING' },
]

// FA & CONS — Project-store & consumables allocation (BRD scaffold; pages WIP,
// specced in public/docs/manual/fa_cons.md)
const faConsItems = [
  { label: 'SLOC Settings',       path: '/fa-cons/sloc-settings', icon: Sliders, permission: 'PAGE_FA_SLOC_SETTINGS' },
  { label: 'Stock & MSA',         path: '/fa-cons/stock',         icon: BarChart3, permission: 'PAGE_FA_STOCK' },
  { label: 'MBQ Master',          path: '/fa-cons/mbq-master',    icon: ClipboardList, permission: 'PAGE_FA_MBQ_MASTER' },
  { label: 'UPC Store List',      path: '/fa-cons/store-list',    icon: Store, permission: 'PAGE_FA_STORE_LIST' },
  { label: 'Allocation',          path: '/fa-cons/allocation',    icon: Cpu, permission: 'PAGE_FA_ALLOCATION' },
  { label: 'Pending Alloc',       path: '/fa-cons/pending',       icon: Truck, permission: 'PAGE_FA_PENDING' },
  { label: 'Gap Report',          path: '/fa-cons/gap-report',    icon: AlertTriangle, permission: 'PAGE_FA_GAP_REPORT' },
  { label: 'Help',                path: '/fa-cons/help',          icon: BookOpen, permission: 'PAGE_FA_HELP' },
]

// Pending Allocation lifecycle submenu
const pendAlcItems = [
  { label: 'Overview',         path: '/pend-alc/overview',     icon: PackageCheck, permission: 'PAGE_PEND_OVERVIEW' },
  { label: 'Report',           path: '/reports/pend-alc',      icon: ClipboardCheck, permission: 'REPORTS_PEND_ALC' },
  { label: 'Manual Entry',     path: '/pend-alc/manual-entry', icon: ClipboardList, permission: 'PAGE_PEND_MANUAL_ENTRY' },
  { label: 'Adhoc Close',      path: '/pend-alc/adhoc-close',  icon: XCircle, permission: 'PAGE_PEND_ADHOC_CLOSE' },
  { label: 'Daily DO Entry',   path: '/pend-alc/do-entry',     icon: Truck, permission: 'PAGE_PEND_DO_ENTRY' },
  { label: 'Reconciliation',   path: '/pend-alc/reco',         icon: BarChart3, permission: 'PAGE_PEND_RECO' },
  { label: 'Open BDC Report',  path: '/pend-alc/open-bdc',     icon: AlertTriangle, permission: 'PAGE_PEND_OPEN_BDC' },
  { label: 'BDC Schedule',     path: '/pend-alc/schedule',     icon: CalendarDays, permission: 'PAGE_PEND_BDC_SCHEDULE' },
  { label: 'Schedule Audit',   path: '/pend-alc/schedule-audit', icon: History, permission: 'PAGE_PEND_SCHEDULE_AUDIT' },
  { label: 'Operations Log',   path: '/pend-alc/operations',   icon: History, permission: 'PAGE_PEND_OPERATIONS' },
]

// SAP Integration submenu — self-contained, read-only. Pulls data FROM SAP into
// local SAP_* staging tables via the universal-MCP gateway, on a schedule or on
// demand. No SAP SDK on the server; no impact on any other module.
const sapItems = [
  { label: 'Data Pulls',   path: '/sap/pulls',      icon: FileDown, permission: 'PAGE_SAP_PULLS' },
  { label: 'SAP Explorer', path: '/sap/explorer',   icon: Search, permission: 'PAGE_SAP_EXPLORER' },
  { label: 'Run History',  path: '/sap/runs',       icon: History, permission: 'PAGE_SAP_RUNS' },
  // Connection now lives in Settings → SAP.
  { label: 'Connection',   path: '/settings?tab=sap', icon: Cog, permission: 'PAGE_SAP_CONNECTION' },
]

// Get Data submenu — outside sources → local SQL (Rep_data) on a schedule or on
// demand, with AUTO/MANUAL run history. Phase 1 = Snowflake; the rest follow.
const getDataItems = [
  { label: 'Overview',        path: '/get-data/overview',        icon: LayoutDashboard, permission: 'PAGE_GD_OVERVIEW' },
  { label: 'Snowflake Views', path: '/get-data/snowflake/views', icon: Snowflake, permission: 'PAGE_GD_SNOWFLAKE_VIEWS' },
  { label: 'Sync Jobs',       path: '/get-data/snowflake/jobs',  icon: RefreshCw, permission: 'PAGE_GD_SYNC_JOBS' },
  { label: 'Run History',     path: '/get-data/runs',            icon: History, permission: 'PAGE_GD_RUNS' },
  { label: 'Excel (soon)',    path: '/get-data/excel',           icon: FileSpreadsheet, permission: 'PAGE_GD_EXCEL' },
  { label: 'DataV2 (soon)',   path: '/get-data/datav2',          icon: PlugZap, permission: 'PAGE_GD_DATAV2' },
  { label: 'SAP (soon)',      path: '/get-data/sap',             icon: Server, permission: 'PAGE_GD_SAP' },
  { label: 'Help',            path: '/get-data/help',            icon: BookOpen, permission: 'PAGE_GD_HELP' },
]

// Data Validation submenu
const dataValidationItems = [
  { label: 'Store Sloc Validation', path: '/data-validation/store-sloc', icon: ShieldCheck, permission: 'STORE_SLOC_VIEW' },
  { label: 'Data Checklist', path: '/data-validation/checklist', icon: ClipboardCheck, permission: 'CHECKLIST_VIEW' },
]

// Settings submenu (admin features)
const settingsItems = [
  { label: 'App Settings', path: '/settings', icon: Cog, permission: 'ADMIN_SETTINGS', end: true },
  { label: 'Business Rules', path: '/settings/business-rules', icon: Sliders, superadminOnly: true },
  { label: 'Table Management', path: '/settings/tables', icon: Columns, permission: 'TABLE_ALTER' },
  { label: 'Users', path: '/settings/users', icon: Users, permission: 'ADMIN_USERS_READ' },
  { label: 'Roles', path: '/settings/roles', icon: Shield, permission: 'ADMIN_ROLES_MANAGE' },
  { label: 'Row-Level Security', path: '/settings/rls', icon: Eye, permission: 'ADMIN_RLS_MANAGE' },
  { label: 'Audit Log', path: '/settings/audit', icon: ScrollText, permission: 'ADMIN_AUDIT_READ' },
  { label: 'Daily Activity Log', path: '/settings/activity-log', icon: ClipboardCheck, superadminOnly: true },
  { label: 'TempDB Maintenance', path: '/settings/tempdb', icon: HardDrive, superadminOnly: true },
  { label: 'Dev Sync (PROD→DEV)', path: '/settings/dev-sync', icon: Database, superadminOnly: true },
  { label: "What's New (Release Notes)", path: '/release-notes', icon: Sparkles, superadminOnly: true },
]

// Single registry drives rendering, the accordion, route detection, and
// keyboard navigation — adding a section here is all that's needed.
export const SECTIONS = [
  { title: 'Data Management',   icon: Database,       items: dataManagementItems, permission: 'MOD_DATA_MGMT' },
  { title: 'Listing & Alloc',   icon: Cpu,            items: dataPreparationItems, permission: 'MOD_LISTING_ALLOC' },
  { title: 'GRT ALC',           icon: Container,      items: binAllocItems, permission: 'MOD_GRT_ALC' },
  { title: '1SZ ALC',           icon: Ruler,          items: sz1AllocItems, permission: 'MOD_1SZ_ALC' },
  { title: 'Adhoc',             icon: FolderOpen,     items: adhocItems, permission: 'MOD_ADHOC' },
  { title: 'Reports',           icon: Activity,       items: reportsItems, permission: 'MOD_REPORTS' },
  { title: 'Get Data',          icon: DatabaseZap,    items: getDataItems, permission: 'MOD_GET_DATA' },
  { title: 'SAP',               icon: Server,         items: sapItems, permission: 'MOD_SAP' },
  { title: 'FA & CONS',         icon: Store,          items: faConsItems, permission: 'MOD_FA_CONS' },
  { title: 'Pending Allocation',icon: Truck,          items: pendAlcItems, permission: 'MOD_PEND_ALC' },
  { title: 'Data Validation',   icon: ClipboardCheck, items: dataValidationItems, permission: 'MOD_DATA_VALIDATION' },
  { title: 'Settings',          icon: Settings,       items: settingsItems, permission: 'MOD_SETTINGS' },
]

// Action permissions that live inside a module but don't gate a menu page
// (run / execute / manage / delete buttons). Settings → Roles lists them
// under the module's card; any permission not placed here or on a menu item
// falls into an "Other" card there, so nothing is ever hidden.
export const MODULE_ACTIONS = {
  MOD_DATA_MGMT:       ['TABLE_READ', 'TABLE_DELETE', 'DATA_EDIT', 'DATA_CHANGE_LOG_VIEW'],
  MOD_LISTING_ALLOC:   ['MSA_EXECUTE', 'GRID_RUN', 'GRID_MANAGE', 'BDC_VIEW', 'BDC_EXECUTE',
                        'ALLOC_CREATE', 'ALLOC_UPDATE', 'ALLOC_DELETE', 'ALLOC_APPROVE', 'ALLOC_EXECUTE'],
  MOD_REPORTS:         ['REPORT_VIEW', 'REPORT_EXPORT'],
  MOD_GET_DATA:        ['GET_DATA_RUN', 'GET_DATA_MANAGE'],
  MOD_DATA_VALIDATION: ['CHECKLIST_MANAGE'],
  // 1SZ ALC: the Prepare tabs Demand and Gap (not menu items of their own any more)
  MOD_1SZ_ALC:         ['PAGE_1SZ_REQ', 'PAGE_1SZ_GAP'],
  MOD_SETTINGS:        ['ADMIN_USERS_CREATE', 'ADMIN_USERS_UPDATE', 'ADMIN_USERS_DELETE',
                        'ADMIN_PERMS_MANAGE', 'COLUMN_EDIT_MANAGE', 'PRODUCT_READ', 'PRODUCT_MANAGE',
                        'MOD_BACKUP'],
}

// ── Direct-URL module + page gate ────────────────────────────────────────────
// A URL belongs to the sidebar item (or extra prefix below) whose path is its
// longest match; opening it needs that item's module AND its own permission,
// exactly what the sidebar needs to show the link. Extras cover routes that
// aren't sidebar items (detail pages, logs, legacy links): a plain string is
// gated by the module only, [prefix, permission, label] also by a page.
const MODULE_ROUTE_EXTRAS = {
  MOD_DATA_MGMT:       ['/tables'],
  MOD_LISTING_ALLOC:   ['/data-prep/listing'],
  MOD_GRT_ALC:         ['/bin-alloc'],
  MOD_1SZ_ALC:         ['/sz1-alloc', ['/sz1-alloc/base', 'PAGE_1SZ_BASE', 'Prepare › Supply'],
                        ['/sz1-alloc/req', 'PAGE_1SZ_REQ', 'Prepare › Demand'], ['/sz1-alloc/gap', 'PAGE_1SZ_GAP', 'Prepare › Gap']],
  MOD_REPORTS:         ['/reports'],
  MOD_GET_DATA:        ['/get-data'],
  MOD_SAP:             ['/sap', ['/sap/connection', 'PAGE_SAP_CONNECTION', 'Connection']],
  MOD_FA_CONS:         ['/fa-cons'],
  MOD_PEND_ALC:        ['/pend-alc', ['/reports/open-bdc', 'PAGE_PEND_OPEN_BDC', 'Open BDC Report']],
  MOD_DATA_VALIDATION: ['/data-validation'],
  MOD_SETTINGS:        ['/settings', '/admin'],
}

/** Does the role pass this item's own page gate? (`perms` any-of, else `permission`, else open.) */
export function itemAllowed(item, hasPermission) {
  if (item.perms?.length) return item.perms.some(p => hasPermission(p))
  return !item.permission || hasPermission(item.permission)
}

const ROUTE_MODULES = (() => {
  const out = []
  // page = one code or an any-of array; kept as `pages` (array) + `page` (the first, for messages)
  const add = (prefix, code, title, page = null, pageLabel = null) => {
    const pages = page == null ? [] : [].concat(page)
    // '/settings?tab=sap' is a link into another module's page, not a route it owns
    if (code && prefix && !prefix.includes('?')) out.push({ prefix, code, title, page: pages[0] || null, pages, pageLabel })
  }
  // superadminOnly pages keep their own route checks; only the module gates them here
  const pageOf = i => (i.superadminOnly ? null : (i.perms?.length ? i.perms : i.permission) || null)
  navItems.forEach(i => add(i.path, i.module, i.label, pageOf(i), i.label))
  SECTIONS.forEach(s => {
    s.items.forEach(i => add(i.path, s.permission, s.title, pageOf(i), i.label))
    ;(MODULE_ROUTE_EXTRAS[s.permission] || []).forEach(e =>
      Array.isArray(e) ? add(e[0], s.permission, s.title, e[1], e[2]) : add(e, s.permission, s.title))
  })
  // longest prefix wins; on a tie the sidebar item (added first) beats the extra
  return out.sort((a, b) => b.prefix.length - a.prefix.length)
})()

/**
 * What a URL needs: { code, title } of its module plus, when it is (or sits
 * under) a gated sidebar page, { page, pages, pageLabel } — the role needs ANY
 * code in `pages` (`page` = the first, for messages). null if no module owns it.
 */
export function moduleForPath(pathname) {
  const hit = ROUTE_MODULES.find(r => pathname === r.prefix || pathname.startsWith(r.prefix + '/'))
  return hit ? { code: hit.code, title: hit.title, page: hit.page, pages: hit.pages, pageLabel: hit.pageLabel } : null
}

/** First sidebar page this user can open, used as the home page and the way out of a blocked page. */
export function firstAllowedPage(hasPermission, superadmin) {
  const ok = (item, module) =>
    !item.path.includes('?')
    && (superadmin || !module || hasPermission(module))
    && !(item.superadminOnly && !superadmin)
    && itemAllowed(item, hasPermission)
  for (const i of navItems) if (ok(i, i.module)) return { path: i.path, label: i.label }
  for (const s of SECTIONS) {
    const i = s.items.find(x => ok(x, s.permission))
    if (i) return { path: i.path, label: `${s.title} › ${i.label}` }
  }
  return null
}
