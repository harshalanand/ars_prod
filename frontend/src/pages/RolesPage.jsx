import { useEffect, useState } from 'react'
import {
  Plus, Shield, Check, X, ChevronDown,
  PackageCheck, Database, Cpu, BarChart3,
  TrendingUp, Activity, ClipboardCheck, Settings,
} from 'lucide-react'
import { rolesAPI } from '@/services/api'
import toast from 'react-hot-toast'

// Mirrors sidebar menu sections exactly
const MENU_SECTIONS = [
  {
    key: 'allocations',
    label: 'Allocations',
    icon: PackageCheck,
    prefixes: ['ALLOC_'],
  },
  {
    key: 'data_management',
    label: 'Data Management',
    icon: Database,
    prefixes: ['DATA_', 'TABLE_', 'JOBS_'],
  },
  {
    key: 'data_preparation',
    label: 'Data Preparation',
    icon: Cpu,
    prefixes: ['MSA_', 'BDC_', 'GRID_', 'LOOKUP_'],
  },
  {
    key: 'contribution',
    label: 'Contribution %',
    icon: BarChart3,
    prefixes: ['CONTRIB_'],
  },
  {
    key: 'trends',
    label: 'Trends',
    icon: TrendingUp,
    prefixes: ['TRENDS_'],
  },
  {
    key: 'reports',
    label: 'Reports',
    icon: Activity,
    prefixes: ['REPORT_', 'REPORTS_'],
  },
  {
    key: 'data_validation',
    label: 'Data Validation',
    icon: ClipboardCheck,
    prefixes: ['STORE_', 'CHECKLIST_'],
  },
  {
    key: 'settings',
    label: 'Settings & Admin',
    icon: Settings,
    prefixes: ['ADMIN_', 'COLUMN_', 'PRODUCT_'],
  },
]

const PERM_LABELS = {
  // Allocations
  ALLOC_READ: 'View Allocations',
  ALLOC_CREATE: 'Create Allocations',
  ALLOC_UPDATE: 'Edit Allocations',
  ALLOC_DELETE: 'Delete Allocations',
  ALLOC_APPROVE: 'Approve Allocations',
  ALLOC_EXECUTE: 'Execute Allocations',
  // Data Management
  DATA_VIEW: 'View Tables',
  DATA_UPLOAD: 'Upload Data',
  DATA_EXPORT: 'Export Data',
  DATA_EDIT: 'Edit Data',
  DATA_CHANGE_LOG_VIEW: 'View Change Log',
  DATA_EDITOR: 'Data Editor',
  TABLE_CREATE: 'Create Tables',
  TABLE_ALTER: 'Alter / Manage Tables',
  TABLE_DELETE: 'Delete Tables',
  TABLE_READ: 'Read Tables',
  JOBS_VIEW: 'Jobs Dashboard',
  // Data Preparation
  MSA_VIEW: 'MSA Stock Calculation',
  BDC_VIEW: 'BDC Creation',
  GRID_VIEW: 'Grid Builder',
  LOOKUP_VIEW: 'Lookup Art Master',
  // Contribution %
  CONTRIB_PRESETS: 'Manage Presets',
  CONTRIB_MAPPINGS: 'Manage Mappings',
  CONTRIB_EXECUTE: 'Execute Calculation',
  CONTRIB_REVIEW: 'Review Results',
  // Trends
  TRENDS_DASHBOARD: 'View Dashboard',
  TRENDS_UPLOAD: 'Upload Trends Data',
  TRENDS_REVIEW: 'Review Trends',
  // Reports
  REPORT_VIEW: 'View Reports',
  REPORT_EXPORT: 'Export Reports',
  REPORTS_PEND_ALC: 'Pending Allocation Report',
  // Data Validation
  STORE_SLOC_VIEW: 'Store Sloc Validation',
  CHECKLIST_VIEW: 'Data Checklist',
  // Settings & Admin
  ADMIN_SETTINGS: 'App Settings',
  ADMIN_USERS_READ: 'View Users',
  ADMIN_USERS_CREATE: 'Create Users',
  ADMIN_USERS_UPDATE: 'Edit Users',
  ADMIN_USERS_DELETE: 'Delete Users',
  ADMIN_ROLES_MANAGE: 'Manage Roles & Permissions',
  ADMIN_RLS_MANAGE: 'Row-Level Security',
  ADMIN_AUDIT_READ: 'View Audit Log',
  COLUMN_EDIT_MANAGE: 'Column Restrictions',
  PRODUCT_MANAGE: 'Manage Products',
  PRODUCT_READ: 'View Products',
}

function getLabel(code) {
  return PERM_LABELS[code] || code.replace(/_/g, ' ').toLowerCase().replace(/\b\w/g, c => c.toUpperCase())
}

function groupBySection(allCodes) {
  const assigned = new Set()
  const sections = MENU_SECTIONS.map(sec => {
    const codes = allCodes.filter(c => sec.prefixes.some(p => c.startsWith(p)))
    codes.forEach(c => assigned.add(c))
    return { ...sec, codes }
  })
  const other = allCodes.filter(c => !assigned.has(c))
  return { sections, other }
}

// ── Section Accordion ─────────────────────────────────────────────────────────
function PermSection({ section, rolePerms, onToggle }) {
  const [open, setOpen] = useState(true)
  const { codes, label, icon: Icon } = section

  if (codes.length === 0) return null

  const granted = codes.filter(c => rolePerms.includes(c)).length
  const allGranted = granted === codes.length
  const someGranted = granted > 0 && !allGranted

  const toggleAll = (e) => {
    e.stopPropagation()
    if (allGranted) codes.forEach(c => rolePerms.includes(c) && onToggle(c))
    else codes.forEach(c => !rolePerms.includes(c) && onToggle(c))
  }

  return (
    <div className="border border-gray-200 rounded-xl overflow-hidden">
      {/* Section Header */}
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-3 px-4 py-3 bg-gray-50 hover:bg-gray-100 transition-colors text-left"
      >
        <div className="w-7 h-7 rounded-lg bg-primary-100 flex items-center justify-center shrink-0">
          <Icon size={14} className="text-primary-600" />
        </div>
        <span className="flex-1 text-sm font-semibold text-gray-800">{label}</span>
        <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${granted > 0 ? 'bg-primary-100 text-primary-700' : 'bg-gray-200 text-gray-500'}`}>
          {granted}/{codes.length}
        </span>
        {/* Grant / Revoke All toggle */}
        <div
          role="button"
          tabIndex={0}
          onClick={toggleAll}
          onKeyDown={e => e.key === 'Enter' && toggleAll(e)}
          title={allGranted ? 'Revoke All' : 'Grant All'}
          className={`w-9 h-5 rounded-full transition-colors shrink-0 relative ${allGranted ? 'bg-primary-600' : someGranted ? 'bg-primary-300' : 'bg-gray-300'}`}
        >
          <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all ${allGranted ? 'left-4' : 'left-0.5'}`} />
        </div>
        <ChevronDown size={14} className={`text-gray-400 transition-transform shrink-0 ${open ? 'rotate-180' : ''}`} />
      </button>

      {/* Permission Checkboxes */}
      {open && (
        <div className="p-3 grid grid-cols-2 gap-2 bg-white">
          {codes.map(code => {
            const active = rolePerms.includes(code)
            return (
              <label
                key={code}
                className={`flex items-center gap-2.5 px-3 py-2 rounded-lg border cursor-pointer transition-colors select-none ${
                  active ? 'bg-primary-50 border-primary-300 text-primary-800' : 'bg-white border-gray-200 hover:border-gray-300 text-gray-600'
                }`}
              >
                <input
                  type="checkbox"
                  checked={active}
                  onChange={() => onToggle(code)}
                  className="rounded text-primary-600 shrink-0"
                />
                <span className="text-xs font-medium leading-tight">{getLabel(code)}</span>
              </label>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────────────
export default function RolesPage() {
  const [roles, setRoles] = useState([])
  const [permissions, setPermissions] = useState([])
  const [loading, setLoading] = useState(true)
  const [selectedRole, setSelectedRole] = useState(null)
  const [rolePerms, setRolePerms] = useState([])
  const [saving, setSaving] = useState(false)
  const [showCreate, setShowCreate] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const [r, p] = await Promise.allSettled([rolesAPI.list(), rolesAPI.permissions()])
      if (r.status === 'fulfilled') setRoles(r.value.data.data || [])
      if (p.status === 'fulfilled') setPermissions(p.value.data.data || [])
    } finally { setLoading(false) }
  }

  useEffect(() => { load() }, [])

  const selectRole = (role) => {
    setSelectedRole(role)
    setRolePerms(role.permissions || [])
  }

  const togglePerm = (code) => {
    setRolePerms(rp => rp.includes(code) ? rp.filter(c => c !== code) : [...rp, code])
  }

  const savePerms = async () => {
    if (!selectedRole) return
    setSaving(true)
    try {
      await rolesAPI.assignPermissions(selectedRole.id, { permission_codes: rolePerms })
      toast.success('Permissions saved')
      load()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Save failed')
    } finally { setSaving(false) }
  }

  const allCodes = permissions.map(p => p.permission_code || p)
  const { sections, other } = groupBySection(allCodes)
  const totalGranted = rolePerms.length

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Roles & Permissions</h1>
          <p className="text-gray-500 text-sm mt-0.5">Manage role definitions and permission assignments</p>
        </div>
        <button onClick={() => setShowCreate(true)} className="btn-primary"><Plus size={16} /> Create Role</button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-5">
        {/* ── Role List ── */}
        <div className="card">
          <div className="card-header"><h3 className="font-semibold">Roles</h3></div>
          <div className="divide-y">
            {loading ? (
              <div className="p-4 text-gray-400 text-sm">Loading...</div>
            ) : roles.map(r => (
              <button
                key={r.id}
                onClick={() => selectRole(r)}
                className={`w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-gray-50 transition-colors ${selectedRole?.id === r.id ? 'bg-primary-50 border-l-2 border-primary-600' : ''}`}
              >
                <Shield size={15} className={selectedRole?.id === r.id ? 'text-primary-600' : 'text-gray-400'} />
                <div className="min-w-0">
                  <div className="text-sm font-medium text-gray-900 truncate">{r.role_name}</div>
                  <div className="text-xs text-gray-400 truncate">{r.description || 'No description'}</div>
                </div>
              </button>
            ))}
          </div>
        </div>

        {/* ── Permission Panel ── */}
        <div className="lg:col-span-3 card flex flex-col">
          {/* Panel Header */}
          <div className="card-header flex items-center justify-between gap-3">
            <div className="flex items-center gap-2 min-w-0">
              <h3 className="font-semibold truncate">
                {selectedRole ? `Permissions: ${selectedRole.role_name}` : 'Select a role'}
              </h3>
              {selectedRole && (
                <span className="text-xs text-gray-500 shrink-0">{totalGranted} granted</span>
              )}
            </div>
            {selectedRole && (
              <button onClick={savePerms} disabled={saving} className="btn-primary btn-sm shrink-0">
                <Check size={14} /> {saving ? 'Saving...' : 'Save'}
              </button>
            )}
          </div>

          {selectedRole ? (
            <div className="p-4 space-y-3 overflow-y-auto max-h-[calc(100vh-220px)]">
              {/* Sections matching sidebar */}
              {sections.map(sec => (
                <PermSection key={sec.key} section={sec} rolePerms={rolePerms} onToggle={togglePerm} />
              ))}

              {/* Any permissions not matched to a section */}
              {other.length > 0 && (
                <PermSection
                  section={{ key: 'other', label: 'Other', icon: Shield, codes: other }}
                  rolePerms={rolePerms}
                  onToggle={togglePerm}
                />
              )}
            </div>
          ) : (
            <div className="flex-1 flex flex-col items-center justify-center py-20 text-gray-400 gap-2">
              <Shield size={36} strokeWidth={1} />
              <p className="text-sm">Select a role from the left to manage its permissions</p>
            </div>
          )}
        </div>
      </div>

      {showCreate && (
        <CreateRoleModal onClose={() => setShowCreate(false)} onCreated={() => { setShowCreate(false); load() }} />
      )}
    </div>
  )
}

// ── Create Role Modal ─────────────────────────────────────────────────────────
function CreateRoleModal({ onClose, onCreated }) {
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [saving, setSaving] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!name.trim()) return toast.error('Role name required')
    setSaving(true)
    try {
      await rolesAPI.create({ role_name: name.trim(), description: desc })
      toast.success('Role created')
      onCreated()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Create failed')
    } finally { setSaving(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md m-4">
        <div className="flex items-center justify-between px-6 py-4 border-b">
          <h2 className="text-lg font-semibold">Create Role</h2>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded-lg"><X size={18} /></button>
        </div>
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          <div>
            <label className="label">Role Name*</label>
            <input value={name} onChange={e => setName(e.target.value)} className="input" required placeholder="e.g. Planner" />
          </div>
          <div>
            <label className="label">Description</label>
            <input value={desc} onChange={e => setDesc(e.target.value)} className="input" placeholder="Short description" />
          </div>
          <div className="flex justify-end gap-3">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button type="submit" disabled={saving} className="btn-primary">{saving ? 'Creating...' : 'Create'}</button>
          </div>
        </form>
      </div>
    </div>
  )
}
