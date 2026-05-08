import { NavLink } from 'react-router-dom'
import {
  LayoutDashboard, Table2, Upload, PackageCheck, Users, Shield, Eye, ScrollText,
  ChevronLeft, ChevronRight, Box, ChevronDown, FolderOpen, FilePlus, FileUp, Plus,
  FileDown, Edit3, Settings, Database, Columns, BarChart3, Cpu, Cog, Activity,
  Clock, Truck, FileText, ClipboardCheck, ClipboardList, ShieldCheck, LayoutGrid, Search, TrendingUp, List, BookOpen,
  HardDrive, Code2, Lock, CalendarDays, History, FolderKanban, ListTodo
} from 'lucide-react'
import useAuthStore from '@/store/authStore'
import clsx from 'clsx'
import { useState, useRef, useEffect } from 'react'

const navItems = [
  { label: 'Dashboard', path: '/', icon: LayoutDashboard, end: true },
  { label: 'Allocations', path: '/allocations', icon: PackageCheck, permission: 'ALLOC_READ' },
  { label: 'Process', path: '/process', icon: BookOpen },
  // Developer Guide — superadmin only. Auto-introspecting, no SOP rot.
  { label: 'Developer Guide', path: '/dev-guide', icon: Code2, superadminOnly: true },
]

// Data Management submenu
const dataManagementItems = [
  { label: 'All Tables', path: '/tables', icon: Table2, permission: 'DATA_VIEW', end: true },
  { label: 'Create Table', path: '/tables/create', icon: FilePlus, permission: 'TABLE_CREATE' },
  { label: 'Upload Data', path: '/upload', icon: FileUp, permission: 'DATA_UPLOAD' },
  { label: 'Export Data', path: '/export', icon: FileDown, permission: 'DATA_EXPORT' },
  { label: 'Jobs Dashboard', path: '/jobs', icon: Activity, permission: 'JOBS_VIEW' },
  { label: 'Data Editor', path: '/editor', icon: Edit3, permission: 'DATA_EDITOR' },
]

// Data Preparation submenu
const dataPreparationItems = [
  { label: 'MSA Stock Calculation', path: '/msa', icon: BarChart3, permission: 'MSA_VIEW' },
  { label: 'BDC Creation', path: '/bdc', icon: FileText, permission: 'BDC_VIEW' },
  { label: 'Grid Builder', path: '/data-prep/store-stock', icon: LayoutGrid, permission: 'GRID_VIEW' },
  { label: 'Lookup Art Master', path: '/data-prep/lookup-art-master', icon: Search, permission: 'LOOKUP_VIEW' },
  { label: 'Listing', path: '/data-prep/listing', icon: List },
]

// Contribution Percentage submenu
const contributionItems = [
  { label: 'Presets', path: '/contribution/presets', icon: Settings, permission: 'CONTRIB_PRESETS' },
  { label: 'Mappings', path: '/contribution/mappings', icon: Columns, permission: 'CONTRIB_MAPPINGS' },
  { label: 'Execute', path: '/contribution/execute', icon: Cpu, permission: 'CONTRIB_EXECUTE' },
  { label: 'Review', path: '/contribution/review', icon: ClipboardCheck, permission: 'CONTRIB_REVIEW' },
]

// Reports submenu
const reportsItems = [
  { label: 'Hold Dashboard', path: '/reports/hold', icon: Lock },
]

// Pending Allocation lifecycle submenu
const pendAlcItems = [
  { label: 'Overview',         path: '/pend-alc/overview',     icon: PackageCheck },
  { label: 'Report',           path: '/reports/pend-alc',      icon: ClipboardCheck, permission: 'REPORTS_PEND_ALC' },
  { label: 'Manual Entry',     path: '/pend-alc/manual-entry', icon: ClipboardList },
  { label: 'Daily DO Entry',   path: '/pend-alc/do-entry',     icon: Truck },
  { label: 'Reconciliation',   path: '/pend-alc/reco',         icon: BarChart3 },
  { label: 'BDC Schedule',     path: '/pend-alc/schedule',     icon: CalendarDays },
  { label: 'Schedule Audit',   path: '/pend-alc/schedule-audit', icon: History },
  { label: 'Operations Log',   path: '/pend-alc/operations',   icon: History },
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

// Settings submenu (admin features)
const settingsItems = [
  { label: 'App Settings', path: '/settings', icon: Cog, permission: 'ADMIN_SETTINGS', end: true },
  { label: 'Table Management', path: '/settings/tables', icon: Columns, permission: 'TABLE_ALTER' },
  { label: 'Users', path: '/settings/users', icon: Users, permission: 'ADMIN_USERS_READ' },
  { label: 'Roles', path: '/settings/roles', icon: Shield, permission: 'ADMIN_ROLES_MANAGE' },
  { label: 'Row-Level Security', path: '/settings/rls', icon: Eye, permission: 'ADMIN_RLS_MANAGE' },
  { label: 'Audit Log', path: '/settings/audit', icon: ScrollText, permission: 'ADMIN_AUDIT_READ' },
  { label: 'TempDB Maintenance', path: '/settings/tempdb', icon: HardDrive, superadminOnly: true },
]

function SideLink({ item, collapsed }) {
  return (
    <NavLink
      to={item.path}
      end={item.end}
      title={collapsed ? item.label : undefined}
      className={({ isActive }) => clsx(
        'flex items-center gap-2 px-2.5 py-1.5 rounded-md text-[11px] font-medium transition-all duration-150',
        collapsed && 'justify-center',
        isActive
          ? 'bg-gradient-to-r from-primary-600 to-primary-500 text-white shadow-md shadow-primary-600/25'
          : 'text-sidebar-text hover:bg-sidebar-hover hover:text-white'
      )}
    >
      <item.icon size={15} className={!collapsed && 'shrink-0'} />
      {!collapsed && <span>{item.label}</span>}
    </NavLink>
  )
}

function SubMenu({ title, icon: Icon, items, collapsed, hasPermission, isSuperAdmin, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen)
  const [showPopup, setShowPopup] = useState(false)
  const popupRef = useRef()
  const buttonRef = useRef()

  const visibleItems = items.filter(i => {
    if (i.superadminOnly && !isSuperAdmin) return false
    return !i.permission || hasPermission(i.permission)
  })
  
  if (visibleItems.length === 0) return null

  // Collapsed mode: show popup on hover
  if (collapsed) {
    return (
      <div 
        className="relative group"
        onMouseEnter={() => setShowPopup(true)}
        onMouseLeave={() => setShowPopup(false)}
      >
        <button
          ref={buttonRef}
          className={clsx(
            'flex items-center justify-center w-full px-2.5 py-1.5 rounded-md text-[11px] font-medium transition-all duration-150',
            'text-sidebar-text hover:bg-sidebar-hover hover:text-white',
            showPopup && 'bg-sidebar-hover text-white'
          )}
          title={title}
        >
          <Icon size={18} />
        </button>
        
        {/* Popup menu - using fixed positioning to escape overflow */}
        {showPopup && (
          <div 
            ref={popupRef}
            className="fixed ml-2 w-48 bg-gray-900 border border-gray-700 rounded-lg shadow-2xl py-1"
            style={{
              left: buttonRef.current ? buttonRef.current.getBoundingClientRect().right + 8 : 64,
              top: buttonRef.current ? buttonRef.current.getBoundingClientRect().top : 0,
              zIndex: 9999,
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
                className={({ isActive }) => clsx(
                  'flex items-center gap-2 px-3 py-1.5 text-[11px] transition-all duration-150',
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

  // Expanded mode
  return (
    <div className="space-y-0.5">
      <button
        onClick={() => setOpen(o => !o)}
        className={clsx(
          'flex items-center justify-between w-full px-2.5 py-1.5 rounded-md text-[11px] font-medium transition-all duration-150',
          'text-sidebar-text hover:bg-sidebar-hover hover:text-white'
        )}
      >
        <div className="flex items-center gap-2.5">
          <Icon size={18} className="shrink-0" />
          <span>{title}</span>
        </div>
        <ChevronDown size={14} className={clsx('transition-transform shrink-0', open && 'rotate-180')} />
      </button>
      {open && (
        <div className="ml-3 space-y-0.5 border-l-2 border-gray-700/50 pl-2.5">
          {visibleItems.map(item => (
            <NavLink
              key={item.path}
              to={item.path}
              end={item.end}
              className={({ isActive }) => clsx(
                'flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-[11px] transition-all duration-150',
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

  return (
    <aside className={clsx(
      'flex flex-col bg-sidebar-bg border-r border-gray-800 transition-all duration-200',
      collapsed ? 'w-16' : 'w-60'
    )}>
      {/* Logo */}
      <div className="flex items-center gap-2.5 px-3 py-4 border-b border-gray-800">
        <div className="w-7 h-7 rounded-lg bg-primary-600 flex items-center justify-center">
          <Box size={16} className="text-white" />
        </div>
        {!collapsed && <span className="text-white font-bold text-base tracking-tight">ARS</span>}
      </div>

      {/* Nav */}
      <nav className="flex-1 px-2 py-3 space-y-0.5 overflow-y-auto">
        {/* Top-level items: hide superadmin-only entries from non-superadmins,
            and respect each item's `permission` flag so users without the
            required permission don't see broken links (e.g. Viewer without
            ALLOC_READ should not see "Allocations"). */}
        {navItems
          .filter(item => !(item.superadminOnly && !superadmin))
          .filter(item => !item.permission || hasPermission(item.permission))
          .map(item => <SideLink key={item.path} item={item} collapsed={collapsed} />)}
        
        {/* Data Management submenu */}
        <SubMenu 
          title="Data Management" 
          icon={Database} 
          items={dataManagementItems} 
          collapsed={collapsed}
          hasPermission={hasPermission}
        />

        {/* Data Preparation submenu */}
        <SubMenu 
          title="Data Preparation" 
          icon={Cpu} 
          items={dataPreparationItems} 
          collapsed={collapsed}
          hasPermission={hasPermission}
        />

        {/* Contribution Percentage submenu */}
        <SubMenu
          title="Contribution %"
          icon={BarChart3}
          items={contributionItems}
          collapsed={collapsed}
          hasPermission={hasPermission}
        />

        {/* Trends submenu */}
        <SubMenu
          title="Trends"
          icon={TrendingUp}
          items={[
            { label: 'Dashboard', path: '/trends/dashboard', icon: BarChart3, permission: 'TRENDS_DASHBOARD' },
            { label: 'Upload', path: '/trends/upload', icon: FileUp, permission: 'TRENDS_UPLOAD' },
            { label: 'Review', path: '/trends/review', icon: Eye, permission: 'TRENDS_REVIEW' },
          ]}
          collapsed={collapsed}
          hasPermission={hasPermission}
        />

        {/* Reports submenu */}
        <SubMenu
          title="Reports"
          icon={Activity}
          items={reportsItems}
          collapsed={collapsed}
          hasPermission={hasPermission}
        />

        {/* Pending Allocation lifecycle */}
        <SubMenu
          title="Pending Allocation"
          icon={Truck}
          items={pendAlcItems}
          collapsed={collapsed}
          hasPermission={hasPermission}
        />

        {/* Data Validation submenu */}
        <SubMenu
          title="Data Validation"
          icon={ClipboardCheck}
          items={dataValidationItems}
          collapsed={collapsed}
          hasPermission={hasPermission}
        />

        {/* Project Tracker submenu — hierarchical project & task management */}
        <SubMenu
          title="Project Tracker"
          icon={FolderKanban}
          items={projectTrackerItems}
          collapsed={collapsed}
          hasPermission={hasPermission}
        />

        {/* Settings submenu */}
        <SubMenu
          title="Settings"
          icon={Settings}
          items={settingsItems}
          collapsed={collapsed}
          hasPermission={hasPermission}
          isSuperAdmin={superadmin}
          defaultOpen={false}
        />
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
          className="flex items-center justify-center w-full py-2 border-t border-gray-800 text-sidebar-text hover:text-white transition-colors"
        >
          {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
        </button>
      </div>
    </aside>
  )
}
