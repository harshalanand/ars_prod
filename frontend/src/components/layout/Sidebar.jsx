import { NavLink, useLocation } from 'react-router-dom'
import {
  LayoutDashboard, Table2, Upload, PackageCheck, Users, Shield, Eye, ScrollText,
  ChevronLeft, ChevronRight, Box, ChevronDown, FolderOpen, FilePlus, FileUp, Plus,
  FileDown, Edit3, Settings, Database, Columns, BarChart3, Cpu, Cog, Activity,
  Clock, Truck, FileText, ClipboardCheck, ClipboardList, ShieldCheck, LayoutGrid, Search, TrendingUp, List,
  HardDrive, Lock, CalendarDays, History, FolderKanban, ListTodo, GitMerge,
  AlertTriangle, BookOpen, GitBranch, Sliders, Boxes, Layers, ListOrdered,
  XCircle, Store, Sparkles, Container, Server,
} from 'lucide-react'
import useAuthStore from '@/store/authStore'
import clsx from 'clsx'
import { useState, useRef, useEffect, useLayoutEffect } from 'react'

const navItems = [
  { label: 'ARS Dashboard', path: '/ars-dashboard', icon: LayoutGrid, permission: 'ALLOC_READ' },
  { label: 'Alloc Review', path: '/alc-review', icon: History, permission: 'ALLOC_READ' },
]

// Data Management submenu
const dataManagementItems = [
  { label: 'All Tables', path: '/tables', icon: Table2, permission: 'DATA_VIEW', end: true },
  { label: 'Create Table', path: '/tables/create', icon: FilePlus, permission: 'TABLE_CREATE' },
  { label: 'Upload Data', path: '/upload', icon: FileUp, permission: 'DATA_UPLOAD' },
  { label: 'Export Data', path: '/export', icon: FileDown, permission: 'DATA_EXPORT' },
  { label: 'Jobs Dashboard', path: '/jobs', icon: Activity, permission: 'JOBS_VIEW' },
  { label: 'Data Editor', path: '/editor', icon: Edit3, permission: 'DATA_EDITOR' },
  { label: 'Data Dictionary', path: '/data-dictionary', icon: BookOpen },
]

// Data Preparation submenu
const dataPreparationItems = [
  { label: 'MSA Stock Calculation', path: '/msa', icon: BarChart3, permission: 'MSA_VIEW' },
  { label: 'Grid Builder', path: '/data-prep/store-stock', icon: LayoutGrid, permission: 'GRID_VIEW' },
  { label: 'Merge Rules', path: '/data-prep/merge-rules', icon: GitMerge, permission: 'GRID_VIEW' },
  { label: 'Listing', path: '/data-prep/listing', icon: List },
]

// Bin Allocation submenu — greedy best-fit bin→store engine (no bin split),
// fixed/cascading eligibility thresholds. ARS-native (server-side, job-driven,
// audited); specced in the BRD/FSD (Bin-Allocation). Pages WIP.
const binAllocItems = [
  { label: 'Bin Master',       path: '/bin-alloc/bin-master',  icon: FileUp },
  { label: 'Requirement',      path: '/bin-alloc/requirement', icon: ClipboardList },
  { label: 'Run Allocation',   path: '/bin-alloc/run',         icon: Cpu },
  { label: 'Results & Export', path: '/bin-alloc/results',     icon: ClipboardCheck },
  { label: 'Eligibility Log',  path: '/bin-alloc/log',         icon: ScrollText },
  { label: 'Help',             path: '/bin-alloc/help',        icon: BookOpen },
]

// Adhoc submenu
const adhocItems = [
  { label: 'Lookup Art Master', path: '/data-prep/lookup-art-master', icon: Search, permission: 'LOOKUP_VIEW' },
]

// Contribution Percentage submenu
const contributionItems = [
  { label: 'Presets', path: '/contribution/presets', icon: Settings, permission: 'CONTRIB_PRESETS' },
  { label: 'Mappings', path: '/contribution/mappings', icon: Columns, permission: 'CONTRIB_MAPPINGS' },
  { label: 'Execute', path: '/contribution/execute', icon: Cpu, permission: 'CONTRIB_EXECUTE' },
  { label: 'Review', path: '/contribution/review', icon: ClipboardCheck, permission: 'CONTRIB_REVIEW' },
]

// Auto Cont % — SQL-direct pipeline (superadmin-only during rollout)
const autoContItems = [
  { label: 'Presets',  path: '/auto-cont/presets',  icon: Settings,       superadminOnly: true },
  { label: 'Mappings', path: '/auto-cont/mappings', icon: Columns,        superadminOnly: true },
  { label: 'Execute',  path: '/auto-cont/execute',  icon: Cpu,            superadminOnly: true },
  { label: 'Jobs',     path: '/auto-cont/jobs',     icon: Activity,       superadminOnly: true },
  { label: 'Review',   path: '/auto-cont/review',   icon: ClipboardCheck, superadminOnly: true },
]

// ALC_Fixture — MSA-STK Allocation Engine (blueprint v1.0, superadmin-only during rollout)
const alcFixtureItems = [
  { label: 'Tunables',  path: '/alc-fixture/tunables',  icon: Sliders,         superadminOnly: true },
  { label: 'Execute',   path: '/alc-fixture/execute',   icon: Cpu,             superadminOnly: true },
  { label: 'Review',    path: '/alc-fixture/review',    icon: ClipboardCheck,  superadminOnly: true },
  { label: 'Dashboard', path: '/alc-fixture/dashboard', icon: LayoutDashboard, superadminOnly: true },
  { label: 'Jobs',      path: '/alc-fixture/jobs',      icon: Activity,        superadminOnly: true },
]

// Trends submenu
const trendsItems = [
  { label: 'Dashboard', path: '/trends/dashboard', icon: BarChart3, permission: 'TRENDS_DASHBOARD' },
  { label: 'Upload', path: '/trends/upload', icon: FileUp, permission: 'TRENDS_UPLOAD' },
  { label: 'Review', path: '/trends/review', icon: Eye, permission: 'TRENDS_REVIEW' },
]

// Reports submenu
const reportsItems = [
  { label: 'Report Generation', path: '/reports/generation', icon: FileText },
  { label: 'Hold Dashboard',  path: '/reports/hold',     icon: Lock },
  { label: 'GAP Report',      path: '/reports/gap',      icon: AlertTriangle, permission: 'ALLOC_READ' },
  { label: 'UPC Store Tracking', path: '/reports/upc-tracking', icon: Store },
]

// FA & CONS — Project-store & consumables allocation (BRD scaffold; pages WIP,
// specced in public/docs/manual/fa_cons.md)
const faConsItems = [
  { label: 'SLOC Settings',       path: '/fa-cons/sloc-settings', icon: Sliders },
  { label: 'Stock & MSA',         path: '/fa-cons/stock',         icon: BarChart3 },
  { label: 'MBQ Master',          path: '/fa-cons/mbq-master',    icon: ClipboardList },
  { label: 'UPC Store List',      path: '/fa-cons/store-list',    icon: Store },
  { label: 'Allocation',          path: '/fa-cons/allocation',    icon: Cpu },
  { label: 'Pending Alloc',       path: '/fa-cons/pending',       icon: Truck },
  { label: 'Gap Report',          path: '/fa-cons/gap-report',    icon: AlertTriangle },
  { label: 'Help',                path: '/fa-cons/help',          icon: BookOpen },
]

// Pending Allocation lifecycle submenu
const pendAlcItems = [
  { label: 'Overview',         path: '/pend-alc/overview',     icon: PackageCheck },
  { label: 'Report',           path: '/reports/pend-alc',      icon: ClipboardCheck, permission: 'REPORTS_PEND_ALC' },
  { label: 'Manual Entry',     path: '/pend-alc/manual-entry', icon: ClipboardList },
  { label: 'Adhoc Close',      path: '/pend-alc/adhoc-close',  icon: XCircle },
  { label: 'Daily DO Entry',   path: '/pend-alc/do-entry',     icon: Truck },
  { label: 'Reconciliation',   path: '/pend-alc/reco',         icon: BarChart3 },
  { label: 'Open BDC Report',  path: '/pend-alc/open-bdc',     icon: AlertTriangle },
  { label: 'BDC Schedule',     path: '/pend-alc/schedule',     icon: CalendarDays },
  { label: 'Schedule Audit',   path: '/pend-alc/schedule-audit', icon: History },
  { label: 'Operations Log',   path: '/pend-alc/operations',   icon: History },
]

// SAP Integration submenu — self-contained, read-only. Pulls data FROM SAP into
// local SAP_* staging tables via the universal-MCP gateway, on a schedule or on
// demand. No SAP SDK on the server; no impact on any other module.
const sapItems = [
  { label: 'Data Pulls',   path: '/sap/pulls',      icon: FileDown },
  { label: 'SAP Explorer', path: '/sap/explorer',   icon: Search },
  { label: 'Run History',  path: '/sap/runs',       icon: History },
  // Connection now lives in Settings → SAP.
  { label: 'Connection',   path: '/settings?tab=sap', icon: Cog },
]

// Data Validation submenu
const dataValidationItems = [
  { label: 'Store Sloc Validation', path: '/data-validation/store-sloc', icon: ShieldCheck, permission: 'STORE_SLOC_VIEW' },
  { label: 'Data Checklist', path: '/data-validation/checklist', icon: ClipboardCheck, permission: 'CHECKLIST_VIEW' },
]

// Project Tracker submenu — enterprise-style task management
const projectTrackerItems = [
  { label: 'Dashboard',     path: '/pt',          icon: LayoutDashboard, end: true },
  { label: 'All Projects',  path: '/pt/projects', icon: FolderKanban },
  { label: 'My Tasks',      path: '/pt/my-tasks', icon: ListTodo },
]

// Training Manual — the canonical ARS Manual: BRD + FSD + step-by-step +
// gallery per module. Dossiers in public/docs/manual/ double as Claude's
// "ARS memory"; images in public/docs/guide/, captions in guideSteps.js.
const processItems = [
  { label: 'Screenshot Gallery',          path: '/manual/gallery',    icon: LayoutGrid },
  { label: 'Getting Started',             path: '/manual/start',      icon: BookOpen },
  { label: 'Step 1 · MSA Stock',          path: '/manual/msa',        icon: BarChart3 },
  { label: 'Step 2 · Grid Builder',       path: '/manual/grid',       icon: LayoutGrid },
  { label: 'Step 3 · Merge Rules',        path: '/manual/merge',      icon: GitMerge },
  { label: 'Step 4 · Listing & Alloc',    path: '/manual/listing',    icon: List },
  { label: 'Step 5 · Review Results',     path: '/manual/review',     icon: ClipboardCheck },
  { label: 'Step 6 · Hold Process',       path: '/manual/hold',       icon: Lock },
  { label: 'Step 7 · Pending Allocation', path: '/manual/pendalc',    icon: Truck },
  { label: 'Data Dictionary',             path: '/manual/dictionary', icon: Search },
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
const SECTIONS = [
  { title: 'Data Management',   icon: Database,       items: dataManagementItems, permission: 'MOD_DATA_MGMT' },
  { title: 'Listing & Alloc',   icon: Cpu,            items: dataPreparationItems, permission: 'MOD_LISTING_ALLOC' },
  { title: 'GRT ALC',           icon: Container,      items: binAllocItems, permission: 'MOD_GRT_ALC' },
  { title: 'Adhoc',             icon: FolderOpen,     items: adhocItems, permission: 'MOD_ADHOC' },
  { title: 'Contribution %',    icon: BarChart3,      items: contributionItems, permission: 'MOD_CONTRIB' },
  { title: 'Auto Cont %',       icon: Cpu,            items: autoContItems, permission: 'MOD_AUTO_CONT' },
  { title: 'ALC_Fixture',       icon: Boxes,          items: alcFixtureItems, permission: 'MOD_ALC_FIXTURE' },
  { title: 'Trends',            icon: TrendingUp,     items: trendsItems, permission: 'MOD_TRENDS' },
  { title: 'Reports',           icon: Activity,       items: reportsItems, permission: 'MOD_REPORTS' },
  { title: 'SAP',               icon: Server,         items: sapItems, permission: 'MOD_SAP' },
  { title: 'FA & CONS',         icon: Store,          items: faConsItems, permission: 'MOD_FA_CONS' },
  { title: 'Pending Allocation',icon: Truck,          items: pendAlcItems, permission: 'MOD_PEND_ALC' },
  { title: 'Data Validation',   icon: ClipboardCheck, items: dataValidationItems, permission: 'MOD_DATA_VALIDATION' },
  { title: 'Project Tracker',   icon: FolderKanban,   items: projectTrackerItems, permission: 'MOD_PROJECT_TRACKER' },
  { title: 'Training Manual',   icon: BookOpen,       items: processItems, permission: 'MOD_TRAINING' },
  { title: 'Settings',          icon: Settings,       items: settingsItems, permission: 'MOD_SETTINGS' },
]

const OPEN_SECTION_LS_KEY = 'ars_sidebar_open_section'

// Only one collapsed-mode flyout may be open at a time — each flyout
// registers its close function here and closes the previous one on open.
let closeActiveFlyout = null

const itemMatchesPath = (item, pathname) =>
  item.end ? pathname === item.path : (pathname === item.path || pathname.startsWith(item.path + '/'))

const sectionForPath = (pathname) =>
  SECTIONS.find(s => s.items.some(i => itemMatchesPath(i, pathname)))?.title || null

function SideLink({ item, collapsed }) {
  return (
    <NavLink
      to={item.path}
      end={item.end}
      data-nav-row="link"
      title={collapsed ? item.label : undefined}
      className={({ isActive }) => clsx(
        'flex items-center gap-2 px-2.5 py-1.5 rounded-md text-[11px] font-medium transition-all duration-150',
        collapsed && 'justify-center',
        isActive
          ? 'bg-gradient-to-r from-primary-600 to-primary-500 text-white shadow-md shadow-primary-600/25'
          : 'text-sidebar-text hover:bg-sidebar-hover hover:text-white'
      )}
    >
      <item.icon size={15} className={clsx(!collapsed && 'shrink-0')} />
      {!collapsed && <span>{item.label}</span>}
    </NavLink>
  )
}

function SubMenu({ title, icon: Icon, items, collapsed, hasPermission, isSuperAdmin, open, onToggle, activeInside }) {
  const [showPopup, setShowPopup] = useState(false)
  const [popupTop, setPopupTop] = useState(0)
  const popupRef = useRef()
  const buttonRef = useRef()
  const hideTimer = useRef(null)
  // Set when the flyout is opened via keyboard — after the popup commits,
  // focus moves to its first link (hover-opens must not steal focus).
  const focusOnOpen = useRef(false)

  const visibleItems = items.filter(i => {
    if (i.superadminOnly && !isSuperAdmin) return false
    return !i.permission || hasPermission(i.permission)
  })

  // Clamp the flyout inside the viewport (long menus scroll internally),
  // and close it if the sidebar scrolls underneath it.
  useLayoutEffect(() => {
    if (!showPopup || !buttonRef.current) return
    const btnTop = buttonRef.current.getBoundingClientRect().top
    const popupH = popupRef.current?.offsetHeight || 0
    setPopupTop(Math.max(8, Math.min(btnTop, window.innerHeight - popupH - 8)))
    if (focusOnOpen.current) {
      popupRef.current?.querySelector('a')?.focus()
      focusOnOpen.current = false
    }
    const nav = buttonRef.current.closest('nav')
    const close = () => setShowPopup(false)
    nav?.addEventListener('scroll', close)
    return () => nav?.removeEventListener('scroll', close)
  }, [showPopup])

  useEffect(() => () => clearTimeout(hideTimer.current), [])

  if (visibleItems.length === 0) return null

  // Collapsed mode: flyout opens on hover OR keyboard focus + Enter/→
  if (collapsed) {
    const closeNow = () => { clearTimeout(hideTimer.current); setShowPopup(false) }
    const openPopup = () => {
      clearTimeout(hideTimer.current)
      // close whichever other section's flyout is open — never stack them
      if (closeActiveFlyout && closeActiveFlyout !== closeNow) closeActiveFlyout()
      closeActiveFlyout = closeNow
      setShowPopup(true)
    }
    const scheduleClose = () => {
      clearTimeout(hideTimer.current)
      hideTimer.current = setTimeout(() => setShowPopup(false), 250)
    }

    return (
      <div className="relative" onMouseEnter={openPopup} onMouseLeave={scheduleClose}>
        <button
          ref={buttonRef}
          data-nav-row="header"
          data-section={title}
          aria-haspopup="menu"
          aria-expanded={showPopup}
          onFocus={openPopup}
          onBlur={(e) => {
            // focus left the button — close unless it moved into the flyout
            if (!popupRef.current?.contains(e.relatedTarget)) scheduleClose()
          }}
          onKeyDown={(e) => {
            if (e.key === 'Escape') { closeNow() }
            else if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowRight') {
              e.preventDefault()
              e.stopPropagation()
              focusOnOpen.current = true // focus first flyout link after commit
              openPopup()
              if (showPopup) { // already open — just move focus in
                popupRef.current?.querySelector('a')?.focus()
                focusOnOpen.current = false
              }
            }
          }}
          className={clsx(
            'relative flex items-center justify-center w-full px-2.5 py-1.5 rounded-md text-[11px] font-medium transition-all duration-150',
            'text-sidebar-text hover:bg-sidebar-hover hover:text-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary-400',
            showPopup && 'bg-sidebar-hover text-white'
          )}
          title={title}
        >
          <Icon size={18} />
          {activeInside && (
            <span className="absolute top-1 right-1 w-1.5 h-1.5 rounded-full bg-primary-400" />
          )}
        </button>

        {showPopup && (
          <div
            ref={popupRef}
            role="menu"
            className="fixed w-48 bg-gray-900 border border-gray-700 rounded-lg shadow-2xl py-1 overflow-y-auto"
            style={{
              left: buttonRef.current ? buttonRef.current.getBoundingClientRect().right + 8 : 64,
              top: popupTop,
              maxHeight: 'calc(100vh - 16px)',
              zIndex: 9999,
            }}
            onMouseEnter={openPopup}
            onMouseLeave={scheduleClose}
            onFocus={openPopup}
            onKeyDown={(e) => {
              const links = [...(popupRef.current?.querySelectorAll('a') || [])]
              const idx = links.indexOf(document.activeElement)
              if (e.key === 'ArrowDown') { e.preventDefault(); e.stopPropagation(); (links[idx + 1] || links[0])?.focus() }
              else if (e.key === 'ArrowUp') { e.preventDefault(); e.stopPropagation(); (links[idx - 1] || links[links.length - 1])?.focus() }
              else if (e.key === 'Escape' || e.key === 'ArrowLeft') {
                e.preventDefault(); e.stopPropagation()
                closeNow()
                buttonRef.current?.focus()
              }
            }}
            onBlur={(e) => {
              // close when focus leaves the flyout entirely (keyboard Tab-out)
              if (!e.currentTarget.contains(e.relatedTarget) && e.relatedTarget !== buttonRef.current) closeNow()
            }}
          >
            <div className="px-3 py-1.5 text-[10px] font-semibold text-gray-400 uppercase tracking-wide border-b border-gray-700">
              {title}
            </div>
            {visibleItems.map(item => (
              <NavLink
                key={item.path}
                to={item.path}
                end={item.end}
                role="menuitem"
                onClick={closeNow}
                className={({ isActive }) => clsx(
                  'flex items-center gap-2 px-3 py-1.5 text-[11px] transition-all duration-150',
                  'focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary-400',
                  isActive
                    ? 'bg-primary-600/30 text-primary-400 font-medium'
                    : 'text-gray-300 hover:bg-gray-800 hover:text-white'
                )}
              >
                <item.icon size={14} className="shrink-0" />
                <span>{item.label}</span>
              </NavLink>
            ))}
          </div>
        )}
      </div>
    )
  }

  // Expanded mode — accordion: open/close is owned by the Sidebar
  return (
    <div className="space-y-0.5">
      <button
        data-nav-row="header"
        data-section={title}
        aria-expanded={open}
        onClick={() => onToggle(title)}
        className={clsx(
          'flex items-center justify-between w-full px-2.5 py-1.5 rounded-md text-[11px] font-medium transition-all duration-150',
          'text-sidebar-text hover:bg-sidebar-hover hover:text-white focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary-400',
          activeInside && !open && 'text-white'
        )}
      >
        <div className="flex items-center gap-2.5">
          <Icon size={18} className="shrink-0" />
          <span>{title}</span>
          {activeInside && !open && (
            <span className="w-1.5 h-1.5 rounded-full bg-primary-400 shrink-0" title="Contains the current page" />
          )}
        </div>
        <ChevronDown size={14} className={clsx('transition-transform shrink-0', open && 'rotate-180')} />
      </button>
      {open && (
        <div data-section-body={title} className="ml-3 space-y-0.5 border-l-2 border-gray-700/50 pl-2.5">
          {visibleItems.map(item => (
            <NavLink
              key={item.path}
              to={item.path}
              end={item.end}
              data-nav-row="link"
              className={({ isActive }) => clsx(
                'flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-[11px] transition-all duration-150',
                'focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary-400',
                isActive
                  ? 'bg-primary-600/20 text-primary-400 font-medium border-l-2 border-primary-400 -ml-[2px] pl-[12px]'
                  : 'text-sidebar-text/80 hover:bg-sidebar-hover hover:text-white'
              )}
            >
              <item.icon size={14} className="shrink-0" />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </div>
      )}
    </div>
  )
}

export default function Sidebar({ collapsed, onToggle }) {
  const { hasPermission, isSuperAdmin } = useAuthStore()
  const superadmin = isSuperAdmin()
  const location = useLocation()
  const navRef = useRef(null)
  // Accordion: at most one section open; the active route's section wins on
  // load, then the last user choice persists across reloads.
  const [openSection, setOpenSection] = useState(() => {
    try { return localStorage.getItem(OPEN_SECTION_LS_KEY) || sectionForPath(window.location.pathname) } catch { return null }
  })
  // When → expands a section via keyboard, focus its first link after render
  const pendingChildFocus = useRef(null)

  const setOpenPersist = (title) => {
    setOpenSection(title)
    try {
      if (title) localStorage.setItem(OPEN_SECTION_LS_KEY, title)
      else localStorage.removeItem(OPEN_SECTION_LS_KEY)
    } catch { /* private mode */ }
  }
  const toggleSection = (title) => setOpenPersist(openSection === title ? null : title)

  // Route awareness: navigating into a section opens it (and closes the rest)
  useEffect(() => {
    const active = sectionForPath(location.pathname)
    if (active && active !== openSection) setOpenPersist(active)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname])

  // On mount, bring the active link into view
  useEffect(() => {
    const el = navRef.current?.querySelector('[aria-current="page"]')
    el?.scrollIntoView({ block: 'nearest' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Focus the first child link after a keyboard-driven expand
  useEffect(() => {
    if (pendingChildFocus.current && pendingChildFocus.current === openSection) {
      navRef.current
        ?.querySelector(`[data-section-body="${CSS.escape(openSection)}"] [data-nav-row]`)
        ?.focus()
      pendingChildFocus.current = null
    }
  }, [openSection])

  // Arrow-key navigation over every visible header and link
  const onNavKeyDown = (e) => {
    if (!['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return
    const rows = [...(navRef.current?.querySelectorAll('[data-nav-row]') || [])]
    if (rows.length === 0) return
    const cur = document.activeElement
    const idx = rows.indexOf(cur)
    const isHeader = cur?.getAttribute?.('data-nav-row') === 'header'
    const focusAt = (i) => rows[(i + rows.length) % rows.length]?.focus()

    if (e.key === 'ArrowDown')      { e.preventDefault(); focusAt(idx < 0 ? 0 : idx + 1) }
    else if (e.key === 'ArrowUp')   { e.preventDefault(); focusAt(idx < 0 ? rows.length - 1 : idx - 1) }
    else if (e.key === 'Home')      { e.preventDefault(); focusAt(0) }
    else if (e.key === 'End')       { e.preventDefault(); focusAt(rows.length - 1) }
    else if (e.key === 'ArrowRight' && isHeader && !collapsed) {
      e.preventDefault()
      const title = cur.getAttribute('data-section')
      if (openSection !== title) {
        pendingChildFocus.current = title
        setOpenPersist(title)
      } else {
        focusAt(idx + 1) // already open — step into the first child
      }
    }
    else if (e.key === 'ArrowLeft') {
      e.preventDefault()
      if (isHeader) {
        if (!collapsed && openSection === cur.getAttribute('data-section')) setOpenPersist(null)
      } else {
        // from a child link, jump back to its section header
        for (let i = idx - 1; i >= 0; i--) {
          if (rows[i].getAttribute('data-nav-row') === 'header') { rows[i].focus(); break }
        }
      }
    }
  }

  return (
    <aside className={clsx(
      'flex flex-col bg-sidebar-bg border-r border-gray-800 transition-all duration-200',
      collapsed ? 'w-16' : 'w-60'
    )}>
      {/* Logo */}
      <div className="flex items-center gap-2.5 px-3 py-4 border-b border-gray-800">
        <img src="/v2-logo.png" alt="V2" className="h-7 w-7 object-contain shrink-0" />
        {!collapsed && <span className="text-white font-bold text-base tracking-tight">ARS</span>}
      </div>

      {/* Nav */}
      <nav ref={navRef} onKeyDown={onNavKeyDown}
        className="flex-1 px-2 py-3 space-y-0.5 overflow-y-auto">
        {/* Top-level items: hide superadmin-only entries from non-superadmins,
            and respect each item's `permission` flag so users without the
            required permission don't see broken links (e.g. Viewer without
            ALLOC_READ should not see "Allocations"). */}
        {navItems
          .filter(item => !(item.superadminOnly && !superadmin))
          .filter(item => !item.permission || hasPermission(item.permission))
          .map(item => <SideLink key={item.path} item={item} collapsed={collapsed} />)}

        {/* Module gate (2026-07-31): each father menu carries a MOD_*
            permission — one tick per role in Settings → Roles shows/hides
            the whole module. Superadmin always sees everything. */}
        {SECTIONS
          .filter(section => superadmin || !section.permission || hasPermission(section.permission))
          .map(section => (
          <SubMenu
            key={section.title}
            title={section.title}
            icon={section.icon}
            items={section.items}
            collapsed={collapsed}
            hasPermission={hasPermission}
            isSuperAdmin={superadmin}
            open={openSection === section.title}
            onToggle={toggleSection}
            activeInside={section.items.some(i => itemMatchesPath(i, location.pathname))}
          />
          ))}
      </nav>

      {/* Footer: Version + Collapse */}
      <div className="border-t border-gray-800 mt-auto">
        {!collapsed && (
          <div className="px-3 py-2 space-y-0.5">
            <div className="text-[9px] font-bold text-gray-500 uppercase tracking-widest">ARS v2.0</div>
            <div className="text-[8px] text-gray-600">Auto Replenishment System</div>
            <div className="text-[8px] text-gray-600">Designed & Developed by</div>
            <div className="text-[9px] font-semibold text-gray-400">Santosh Kumar</div>
            <div className="text-[7px] text-gray-700 mt-0.5">© {new Date().getFullYear()} All rights reserved</div>
          </div>
        )}
        <button
          onClick={onToggle}
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          className="flex items-center justify-center w-full py-2 border-t border-gray-800 text-sidebar-text hover:text-white transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary-400"
        >
          {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
        </button>
      </div>
    </aside>
  )
}
