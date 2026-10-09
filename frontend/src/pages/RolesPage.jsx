import { useEffect, useMemo, useState } from 'react'
import {
  Plus, Shield, Check, X, ChevronDown, Pencil, Trash2, EyeOff,
} from 'lucide-react'
import { rolesAPI } from '@/services/api'
import toast from 'react-hot-toast'
import { confirmDialog } from '@/components/ui/ConfirmDialog'
import { navItems, SECTIONS, MODULE_ACTIONS } from '@/components/layout/navRegistry'

// Friendlier names for action permissions; everything else falls back to the
// permission_name stored in rbac_permissions.
const PERM_LABELS = {
  ALLOC_CREATE: 'Create Allocations',
  ALLOC_UPDATE: 'Edit Allocations',
  ALLOC_DELETE: 'Delete Allocations',
  ALLOC_APPROVE: 'Approve Allocations',
  ALLOC_EXECUTE: 'Execute Allocations',
  MSA_EXECUTE: 'Run MSA Calculation',
  GRID_RUN: 'Run Grid Builder',
  GRID_MANAGE: 'Manage Grid Builder',
  BDC_VIEW: 'View BDC Creation',
  BDC_EXECUTE: 'Execute BDC Creation',
  TABLE_READ: 'Read Tables',
  TABLE_DELETE: 'Delete Tables',
  DATA_EDIT: 'Edit Data',
  DATA_CHANGE_LOG_VIEW: 'View Change Log',
  REPORT_VIEW: 'View Reports',
  REPORT_EXPORT: 'Export Reports',
  GET_DATA_RUN: 'Run Get Data Jobs',
  GET_DATA_MANAGE: 'Manage Views & Jobs',
  CHECKLIST_MANAGE: 'Manage Data Checklist',
  ADMIN_USERS_CREATE: 'Create Users',
  ADMIN_USERS_UPDATE: 'Edit Users',
  ADMIN_USERS_DELETE: 'Delete Users',
  ADMIN_PERMS_MANAGE: 'Assign Permissions',
  COLUMN_EDIT_MANAGE: 'Column Restrictions',
  PRODUCT_READ: 'View Products',
  PRODUCT_MANAGE: 'Manage Products',
  MOD_BACKUP: 'Backup Manager',
}

// One card per sidebar FATHER MENU, built from the same registry the Sidebar
// renders, so this page always matches what users actually see.
const MODULES = [
  ...navItems.map(i => ({
    key: i.module || i.path, title: i.label, icon: i.icon, moduleCode: i.module, items: [i],
  })),
  ...SECTIONS.map(s => ({
    key: s.permission || s.title, title: s.title, icon: s.icon, moduleCode: s.permission, items: s.items,
  })),
].map(m => ({ ...m, actions: MODULE_ACTIONS[m.moduleCode] || [] }))

// Every grantable permission a module card controls (module switch + pages + actions)
const moduleCodes = (m, available) => [...new Set([
  m.moduleCode,
  ...m.items.filter(i => !i.superadminOnly).map(i => i.permission),
  ...m.actions,
])].filter(c => c && available.has(c))

function Switch({ on, onClick, title }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-pressed={on}
      className={`w-9 h-5 rounded-full transition-colors shrink-0 relative ${on ? 'bg-primary-600' : 'bg-gray-300'}`}
    >
      <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all ${on ? 'left-4' : 'left-0.5'}`} />
    </button>
  )
}

function PermCheckbox({ code, label, sub, rolePerms, onToggle, dim }) {
  const active = rolePerms.includes(code)
  return (
    <label className={`flex items-center gap-2.5 px-3 py-2 rounded-lg border cursor-pointer transition-colors select-none ${
      active ? 'bg-primary-50 border-primary-300 text-primary-800' : 'bg-white border-gray-200 hover:border-gray-300 text-gray-600'
    } ${dim ? 'opacity-60' : ''}`}>
      <input type="checkbox" checked={active} onChange={() => onToggle(code)} className="rounded text-primary-600 shrink-0" />
      <span className="min-w-0">
        <span className="block text-xs font-medium leading-tight truncate">{label}</span>
        {sub && <span className="block text-[10px] text-gray-400 leading-tight truncate">{sub}</span>}
      </span>
    </label>
  )
}

// ── Module card ───────────────────────────────────────────────────────────────
function ModuleCard({ mod, open, onOpen, rolePerms, onToggle, onSetMany, available, permLabel }) {
  const { title, icon: Icon, moduleCode, items, actions } = mod
  const codes = moduleCodes(mod, available)
  const granted = codes.filter(c => rolePerms.includes(c)).length
  const hasSwitch = !!moduleCode && available.has(moduleCode)
  const moduleOn = !hasSwitch || rolePerms.includes(moduleCode)
  const allOn = codes.length > 0 && granted === codes.length
  const actionCodes = actions.filter(c => available.has(c))
  const seen = new Set() // a permission shared by two pages is one tick, shown on both

  return (
    <div className={`border rounded-xl overflow-hidden ${moduleOn ? 'border-gray-200' : 'border-dashed border-gray-300'}`}>
      <div
        role="button"
        tabIndex={0}
        aria-expanded={open}
        onClick={onOpen}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen() } }}
        className="w-full flex items-center gap-3 px-4 py-2.5 bg-gray-50 hover:bg-gray-100 transition-colors cursor-pointer"
      >
        <div className={`w-7 h-7 rounded-lg flex items-center justify-center shrink-0 ${moduleOn ? 'bg-primary-100' : 'bg-gray-200'}`}>
          <Icon size={14} className={moduleOn ? 'text-primary-600' : 'text-gray-400'} />
        </div>
        <div className="flex-1 min-w-0">
          <div className={`text-sm font-semibold truncate ${moduleOn ? 'text-gray-800' : 'text-gray-400'}`}>{title}</div>
          <div className="text-[10px] text-gray-400">
            {items.length > 0
              ? `${items.length} menu page${items.length === 1 ? '' : 's'}`
              : 'Not tied to a sidebar menu'}
            {items.length > 0 && actionCodes.length > 0 && ` · ${actionCodes.length} action${actionCodes.length === 1 ? '' : 's'}`}
            {!moduleOn && ' · hidden from sidebar'}
          </div>
        </div>
        <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${granted > 0 ? 'bg-primary-100 text-primary-700' : 'bg-gray-200 text-gray-500'}`}>
          {granted}/{codes.length}
        </span>
        {hasSwitch && (
          <div className="flex items-center gap-1.5" onClick={e => e.stopPropagation()}>
            <span className="text-[10px] text-gray-500 hidden sm:inline">Module access</span>
            <Switch on={moduleOn} onClick={() => onToggle(moduleCode)}
                    title={moduleOn ? 'Hide this menu from the role' : 'Show this menu to the role'} />
          </div>
        )}
        <ChevronDown size={14} className={`text-gray-400 transition-transform shrink-0 ${open ? 'rotate-180' : ''}`} />
      </div>

      {open && (
        <div className="p-3 space-y-3 bg-white">
          {!moduleOn && (
            <div className="flex items-center gap-2 text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-1.5">
              <EyeOff size={13} className="shrink-0" />
              Module access is off, so this menu is hidden for the role. The ticks below take effect once it is on.
            </div>
          )}
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">
              {items.length > 0 ? 'Menu pages' : 'Permissions'}
            </span>
            {codes.length > 0 && (
              <button type="button" onClick={() => onSetMany(codes, !allOn)}
                      className="text-[11px] text-primary-600 hover:underline">
                {allOn ? 'Clear all' : 'Grant all'}
              </button>
            )}
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
            {items.map(item => {
              const IIcon = item.icon
              if (item.superadminOnly || !item.permission || !available.has(item.permission)) {
                return (
                  <div key={item.path} className="flex items-center gap-2.5 px-3 py-2 rounded-lg border border-gray-100 bg-gray-50/60 text-gray-500">
                    <IIcon size={13} className="shrink-0 text-gray-400" />
                    <span className="text-xs font-medium truncate flex-1">{item.label}</span>
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 shrink-0">
                      {item.superadminOnly ? 'Super admin only' : 'Visible with module'}
                    </span>
                  </div>
                )
              }
              const shared = seen.has(item.permission)
              seen.add(item.permission)
              return (
                <PermCheckbox key={item.path} code={item.permission} label={item.label}
                  sub={shared ? `${item.permission} · same tick as above` : item.permission}
                  rolePerms={rolePerms} onToggle={onToggle} dim={!moduleOn} />
              )
            })}
          </div>
          {actionCodes.length > 0 && (
            <>
              {items.length > 0 && (
                <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-400 pt-1">Actions</div>
              )}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                {actionCodes.map(code => (
                  <PermCheckbox key={code} code={code} label={permLabel(code)} sub={code}
                    rolePerms={rolePerms} onToggle={onToggle} dim={!moduleOn} />
                ))}
              </div>
            </>
          )}
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

  const [editRole, setEditRole] = useState(null)

  const load = async () => {
    setLoading(true)
    try {
      const [r, p] = await Promise.allSettled([rolesAPI.list(), rolesAPI.permissions()])
      if (r.status === 'fulfilled') {
        const list = r.value.data.data || []
        setRoles(list)
        // Keep the open role's name/description fresh after an edit
        setSelectedRole(sel => (sel ? list.find(x => x.id === sel.id) || null : sel))
      }
      if (p.status === 'fulfilled') setPermissions(p.value.data.data || [])
    } finally { setLoading(false) }
  }

  const deleteRole = async (role) => {
    if (!await confirmDialog({
      tone: 'danger',
      title: `Delete role "${role.role_name}"?`,
      body: 'Its permission assignments are removed too. This cannot be undone. '
          + 'A role still assigned to users cannot be deleted.',
      confirmLabel: 'Delete role',
    })) return
    try {
      await rolesAPI.delete(role.id)
      toast.success(`Role "${role.role_name}" deleted`)
      setSelectedRole(null)
      setRolePerms([])
      load()
    } catch { /* api interceptor already shows the reason */ }
  }

  useEffect(() => { load() }, [])

  const selectRole = (role) => {
    setSelectedRole(role)
    setRolePerms(role.permissions || [])
  }

  const togglePerm = (code) => {
    setRolePerms(rp => rp.includes(code) ? rp.filter(c => c !== code) : [...rp, code])
  }
  const setMany = (codes, on) => {
    setRolePerms(rp => on ? [...new Set([...rp, ...codes])] : rp.filter(c => !codes.includes(c)))
  }

  const [openMods, setOpenMods] = useState(() => new Set())
  const toggleOpen = (key) => setOpenMods(s => {
    const n = new Set(s)
    n.has(key) ? n.delete(key) : n.add(key)
    return n
  })

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

  const available = useMemo(() => new Set(permissions.map(p => p.permission_code || p)), [permissions])
  const permNames = useMemo(
    () => Object.fromEntries(permissions.map(p => [p.permission_code, p.permission_name])), [permissions])
  const permLabel = (code) => PERM_LABELS[code] || permNames[code] || code
  // Permissions no module card claims; listed last so nothing is ever unreachable
  const otherCodes = useMemo(() => {
    const placed = new Set(MODULES.flatMap(m => moduleCodes(m, available)))
    return [...available].filter(c => !placed.has(c)).sort()
  }, [available])
  const modulesOn = MODULES.filter(m => !m.moduleCode || !available.has(m.moduleCode) || rolePerms.includes(m.moduleCode)).length
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
              {selectedRole?.is_system_role && (
                <span className="text-[11px] px-2 py-0.5 rounded-full bg-gray-100 text-gray-500 shrink-0"
                      title="Built-in roles can't be renamed or deleted">
                  System role
                </span>
              )}
            </div>
            {selectedRole && (
              <div className="flex items-center gap-2 shrink-0">
                {!selectedRole.is_system_role && (
                  <>
                    <button onClick={() => setEditRole(selectedRole)} className="btn-secondary btn-sm">
                      <Pencil size={14} /> Edit
                    </button>
                    <button onClick={() => deleteRole(selectedRole)}
                            className="btn-secondary btn-sm text-red-600 hover:bg-red-50">
                      <Trash2 size={14} /> Delete
                    </button>
                  </>
                )}
                <button onClick={savePerms} disabled={saving} className="btn-primary btn-sm">
                  <Check size={14} /> {saving ? 'Saving...' : 'Save'}
                </button>
              </div>
            )}
          </div>

          {selectedRole ? (
            <div className="p-4 space-y-3 overflow-y-auto max-h-[calc(100vh-220px)]">
              {/* One card per sidebar father menu, in sidebar order */}
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-gray-500">
                <span>
                  <b className="text-gray-700">{modulesOn}</b> of {MODULES.length} sidebar modules visible to this role.
                  Turn <b>Module access</b> off to hide a whole menu; tick pages and actions inside it.
                </span>
                <span className="flex gap-3 shrink-0">
                  <button type="button" className="text-primary-600 hover:underline"
                          onClick={() => setOpenMods(new Set([...MODULES.map(m => m.key), 'other']))}>Expand all</button>
                  <button type="button" className="text-primary-600 hover:underline"
                          onClick={() => setOpenMods(new Set())}>Collapse all</button>
                </span>
              </div>

              {MODULES.map(mod => (
                <ModuleCard key={mod.key} mod={mod} open={openMods.has(mod.key)} onOpen={() => toggleOpen(mod.key)}
                  rolePerms={rolePerms} onToggle={togglePerm} onSetMany={setMany}
                  available={available} permLabel={permLabel} />
              ))}

              {/* Permissions no sidebar module claims — kept reachable */}
              {otherCodes.length > 0 && (
                <ModuleCard
                  mod={{ key: 'other', title: 'Other permissions', icon: Shield, moduleCode: null, items: [], actions: otherCodes }}
                  open={openMods.has('other')} onOpen={() => toggleOpen('other')}
                  rolePerms={rolePerms} onToggle={togglePerm} onSetMany={setMany}
                  available={available} permLabel={permLabel} />
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
        <RoleModal onClose={() => setShowCreate(false)} onSaved={() => { setShowCreate(false); load() }} />
      )}
      {editRole && (
        <RoleModal role={editRole} onClose={() => setEditRole(null)} onSaved={() => { setEditRole(null); load() }} />
      )}
    </div>
  )
}

// ── Create / Edit Role Modal ──────────────────────────────────────────────────
function RoleModal({ role, onClose, onSaved }) {
  const isEdit = !!role
  const [name, setName] = useState(role?.role_name || '')
  const [desc, setDesc] = useState(role?.description || '')
  const [saving, setSaving] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!name.trim()) return toast.error('Role name required')
    setSaving(true)
    try {
      if (isEdit) {
        await rolesAPI.update(role.id, { role_name: name.trim(), description: desc })
        toast.success('Role updated')
      } else {
        await rolesAPI.create({ role_name: name.trim(), description: desc })
        toast.success('Role created')
      }
      onSaved()
    } catch { /* api interceptor already shows the reason */ }
    finally { setSaving(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md m-4">
        <div className="flex items-center justify-between px-6 py-4 border-b">
          <h2 className="text-lg font-semibold">{isEdit ? 'Edit Role' : 'Create Role'}</h2>
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
            <button type="submit" disabled={saving} className="btn-primary">
              {isEdit ? (saving ? 'Saving...' : 'Save') : (saving ? 'Creating...' : 'Create')}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
