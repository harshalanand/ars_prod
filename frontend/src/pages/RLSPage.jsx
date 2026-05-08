import { useEffect, useState, useRef } from 'react'
import { Plus, Trash2, Eye, Search, Shield, Columns, Lock, Unlock, Save, Database, Users, ChevronDown, X } from 'lucide-react'
import { rlsAPI, usersAPI, tablesAPI } from '@/services/api'
import api from '@/services/api'
import toast from 'react-hot-toast'

function SearchSelect({ value, onChange, options, placeholder = 'Search...' }) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const ref = useRef()

  useEffect(() => {
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const filtered = options.filter(o => o.toLowerCase().includes(query.toLowerCase()))
  const displayValue = value || ''

  const select = (val) => {
    onChange(val)
    setQuery('')
    setOpen(false)
  }

  const clear = (e) => {
    e.stopPropagation()
    onChange('')
    setQuery('')
  }

  return (
    <div ref={ref} className="relative">
      <div
        onClick={() => setOpen(o => !o)}
        className="input flex items-center gap-2 cursor-pointer pr-8"
      >
        {open ? (
          <input
            autoFocus
            value={query}
            onChange={e => setQuery(e.target.value)}
            onClick={e => e.stopPropagation()}
            placeholder={placeholder}
            className="flex-1 bg-transparent outline-none text-sm"
          />
        ) : (
          <span className={`flex-1 text-sm truncate ${displayValue ? 'text-gray-900' : 'text-gray-400'}`}>
            {displayValue || placeholder}
          </span>
        )}
        <div className="absolute right-2 top-1/2 -translate-y-1/2 flex items-center gap-1">
          {displayValue && !open && (
            <button onClick={clear} className="text-gray-400 hover:text-gray-600">
              <X size={12} />
            </button>
          )}
          <ChevronDown size={13} className={`text-gray-400 transition-transform ${open ? 'rotate-180' : ''}`} />
        </div>
      </div>

      {open && (
        <div className="absolute z-50 mt-1 w-full bg-white border border-gray-200 rounded-lg shadow-lg max-h-56 overflow-y-auto">
          {filtered.length === 0 ? (
            <div className="px-3 py-2 text-xs text-gray-400">No tables found</div>
          ) : (
            filtered.map(opt => (
              <button
                key={opt}
                type="button"
                onClick={() => select(opt)}
                className={`w-full text-left px-3 py-1.5 text-xs hover:bg-primary-50 hover:text-primary-700 transition-colors ${
                  opt === value ? 'bg-primary-50 text-primary-700 font-medium' : 'text-gray-700'
                }`}
              >
                {opt}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  )
}

export default function RLSPage() {
  const [tab, setTab] = useState('store') // store | column | table
  const [users, setUsers] = useState([])
  const [roles, setRoles] = useState([])
  const [selectedUser, setSelectedUser] = useState(null)
  const [storeAccess, setStoreAccess] = useState([])
  const [regionAccess, setRegionAccess] = useState([])
  const [stores, setStores] = useState([])
  const [loading, setLoading] = useState(true)
  const [newStore, setNewStore] = useState('')
  const [newRegion, setNewRegion] = useState('')
  const [search, setSearch] = useState('')

  // Column permissions state
  const [tables, setTables] = useState([])
  const [selTable, setSelTable] = useState('')
  const [selRole, setSelRole] = useState('')
  const [colRestrictions, setColRestrictions] = useState([])
  const [tableColumns, setTableColumns] = useState([])
  const [colLoading, setColLoading] = useState(false)
  const [colSaving, setColSaving] = useState(false)

  // Table access state
  const [taTable, setTaTable] = useState('')
  const [taAccess, setTaAccess] = useState([]) // [{role_id, can_read, can_write, can_upload, can_export}]
  const [taLoading, setTaLoading] = useState(false)
  const [taSaving, setTaSaving] = useState(false)

  useEffect(() => {
    const load = async () => {
      try {
        const [u, s, r, t] = await Promise.allSettled([
          usersAPI.list(), rlsAPI.stores(),
          api.get('/roles'), tablesAPI.listAll(),
        ])
        if (u.status === 'fulfilled') setUsers(u.value.data.data?.users || [])
        if (s.status === 'fulfilled') setStores(s.value.data.data || [])
        if (r.status === 'fulfilled') setRoles(r.value.data.data || [])
        if (t.status === 'fulfilled') setTables(t.value.data.data || [])
      } finally { setLoading(false) }
    }
    load()
  }, [])

  // ── Store/Region Access ─────────────────────────────────────────────
  const selectUser = async (user) => {
    setSelectedUser(user)
    try {
      const [sa, ra] = await Promise.allSettled([
        rlsAPI.storeAccess(user.id), rlsAPI.regionAccess(user.id),
      ])
      if (sa.status === 'fulfilled') setStoreAccess(sa.value.data.data || [])
      if (ra.status === 'fulfilled') setRegionAccess(ra.value.data.data || [])
    } catch {}
  }

  const addStore = async () => {
    if (!selectedUser || !newStore.trim()) return
    try {
      await rlsAPI.addStoreAccess({ user_id: selectedUser.id, store_codes: [newStore.trim()] })
      toast.success('Store access added'); setNewStore(''); selectUser(selectedUser)
    } catch {}
  }
  const removeStore = async (code) => {
    if (!selectedUser) return
    try { await rlsAPI.deleteStoreAccess(selectedUser.id, code); toast.success('Removed'); selectUser(selectedUser) } catch {}
  }
  const addRegion = async () => {
    if (!selectedUser || !newRegion.trim()) return
    try {
      await rlsAPI.addRegionAccess({ user_id: selectedUser.id, region: newRegion.trim() })
      toast.success('Region added'); setNewRegion(''); selectUser(selectedUser)
    } catch {}
  }

  // ── Column Permissions ──────────────────────────────────────────────
  const loadColumnPerms = async (table, role) => {
    if (!table || !role) return
    setColLoading(true)
    try {
      const [schemaRes, restrictRes] = await Promise.all([
        tablesAPI.schema(table), rlsAPI.columnRestrictions(table),
      ])
      const cols = schemaRes.data.data?.columns || []
      setTableColumns(cols.map(c => c.column_name || c.name || c))
      const restrictions = restrictRes.data.data || []
      const roleRestrictions = restrictions.filter(r => r.role_id === Number(role))
      setColRestrictions(roleRestrictions)
    } catch { toast.error('Failed to load') }
    finally { setColLoading(false) }
  }

  useEffect(() => { if (selTable && selRole) loadColumnPerms(selTable, selRole) }, [selTable, selRole])

  const getColPerm = (colName) => {
    const r = colRestrictions.find(x => x.column_name === colName)
    return r || { is_visible: true, is_masked: false, can_edit: true, mask_pattern: null }
  }

  const setColPerm = (colName, field, value) => {
    setColRestrictions(prev => {
      const base = prev.find(x => x.column_name === colName)
        || { column_name: colName, role_id: Number(selRole), is_visible: true, is_masked: false, can_edit: true }
      const updated = { ...base, [field]: value }
      if (field === 'is_masked' && value === true) {
        updated.is_visible = false
        updated.can_edit = false
      }
      if ((field === 'is_visible' || field === 'can_edit') && value === true) {
        updated.is_masked = false
      }
      return prev.find(x => x.column_name === colName)
        ? prev.map(x => x.column_name === colName ? updated : x)
        : [...prev, updated]
    })
  }

  const saveColumnPerms = async () => {
    setColSaving(true)
    try {
      const restrictions = tableColumns.map(col => {
        const p = getColPerm(col)
        return { column_name: col, is_visible: p.is_visible, is_masked: p.is_masked, mask_pattern: p.mask_pattern, can_edit: p.can_edit }
      }).filter(r => !r.is_visible || r.is_masked || !r.can_edit) // Only save non-default
      await rlsAPI.bulkColumnRestrictions({ table_name: selTable, role_id: Number(selRole), restrictions })
      toast.success('Column permissions saved')
      loadColumnPerms(selTable, selRole)
    } catch { toast.error('Failed to save') }
    finally { setColSaving(false) }
  }

  const filteredUsers = users.filter(u =>
    (u.username || '').toLowerCase().includes(search.toLowerCase()) ||
    (u.full_name || '').toLowerCase().includes(search.toLowerCase())
  )

  const tabStyle = (t) => `px-4 py-2 text-[11px] font-semibold cursor-pointer border-b-2 transition-colors ${
    tab === t ? 'border-primary-600 text-primary-700 bg-primary-50/50' : 'border-transparent text-gray-500 hover:text-gray-700 hover:bg-gray-50'
  }`

  return (
    <div className="space-y-4">
      <div>
        <h1 className="page-title">Row-Level Security</h1>
        <p className="page-subtitle">Control data access by store, region, and column permissions</p>
      </div>

      {/* Tabs */}
      <div className="flex border-b border-gray-200">
        <button onClick={() => setTab('store')} className={tabStyle('store')}>
          <Shield size={13} className="inline mr-1.5" /> Store & Region Access
        </button>
        <button onClick={() => setTab('column')} className={tabStyle('column')}>
          <Columns size={13} className="inline mr-1.5" /> Column Permissions
        </button>
      </div>

      {/* ═══ Store & Region Tab ═══ */}
      {tab === 'store' && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* User list */}
          <div className="card">
            <div className="card-header">
              <div className="relative">
                <Search size={12} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
                <input value={search} onChange={e => setSearch(e.target.value)} className="input pl-7" placeholder="Filter users..." />
              </div>
            </div>
            <div className="max-h-[450px] overflow-y-auto divide-y">
              {loading ? <div className="p-4 text-gray-400 text-[11px]">Loading...</div> :
                filteredUsers.map(u => (
                  <button key={u.id} onClick={() => selectUser(u)}
                    className={`w-full flex items-center gap-2.5 px-3 py-2 text-left hover:bg-gray-50 transition-colors ${
                      selectedUser?.id === u.id ? 'bg-primary-50 border-l-2 border-primary-600' : ''}`}>
                    <Users size={13} className={selectedUser?.id === u.id ? 'text-primary-600' : 'text-gray-400'} />
                    <div>
                      <div className="text-[11px] font-semibold">{u.username}</div>
                      <div className="text-[10px] text-gray-500">{u.full_name}</div>
                    </div>
                  </button>
                ))}
            </div>
          </div>

          {/* Access panels */}
          <div className="lg:col-span-2 space-y-4">
            {selectedUser ? (
              <>
                <div className="card p-4">
                  <div className="flex items-center justify-between mb-3">
                    <h3 className="text-[12px] font-bold text-gray-900">Store Access: {selectedUser.username}</h3>
                    <span className="badge-gray">{storeAccess.length} store(s)</span>
                  </div>
                  <div className="flex gap-2 mb-3">
                    <input value={newStore} onChange={e => setNewStore(e.target.value)} className="input flex-1" placeholder="Store code" list="store-list" />
                    <datalist id="store-list">{stores.map(s => <option key={s.store_code || s} value={s.store_code || s} />)}</datalist>
                    <button onClick={addStore} className="btn-primary"><Plus size={12} /> Add</button>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {storeAccess.length === 0 ? <span className="text-[10px] text-gray-400">No stores assigned (may have full access via role)</span> :
                      storeAccess.map(s => {
                        const code = s.store_code || s
                        return <div key={code} className="flex items-center gap-1 bg-blue-50 text-blue-700 px-2 py-1 rounded text-[10px] font-medium">
                          {code} <button onClick={() => removeStore(code)} className="hover:text-red-500"><Trash2 size={10} /></button>
                        </div>
                      })}
                  </div>
                </div>
                <div className="card p-4">
                  <div className="flex items-center justify-between mb-3">
                    <h3 className="text-[12px] font-bold text-gray-900">Region Access</h3>
                    <span className="badge-gray">{regionAccess.length} region(s)</span>
                  </div>
                  <div className="flex gap-2 mb-3">
                    <input value={newRegion} onChange={e => setNewRegion(e.target.value)} className="input flex-1" placeholder="Region code" />
                    <button onClick={addRegion} className="btn-primary"><Plus size={12} /> Add</button>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {regionAccess.length === 0 ? <span className="text-[10px] text-gray-400">No regions assigned</span> :
                      regionAccess.map(r => <div key={r.id} className="bg-emerald-50 text-emerald-700 px-2 py-1 rounded text-[10px] font-medium">
                        {r.region || r.hub || r.division || 'Unknown'}
                      </div>)}
                  </div>
                </div>
              </>
            ) : (
              <div className="card p-12 text-center text-gray-400">
                <Users size={32} className="mx-auto mb-2 opacity-20" />
                <div className="text-[11px]">Select a user to manage access</div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ═══ Column Permissions Tab ═══ */}
      {tab === 'column' && (
        <div className="space-y-4">
          {/* Selectors */}
          <div className="card p-3 flex gap-3 items-end flex-wrap">
            <div className="flex-1 min-w-[200px]">
              <label className="label"><Database size={10} className="inline mr-1" />Table</label>
              <SearchSelect
                value={selTable}
                onChange={setSelTable}
                options={tables.map(t => t.table_name || t)}
                placeholder="Select table..."
              />
            </div>
            <div className="w-[200px]">
              <label className="label"><Shield size={10} className="inline mr-1" />Role</label>
              <select value={selRole} onChange={e => setSelRole(e.target.value)} className="input">
                <option value="">Select role...</option>
                {roles.map(r => <option key={r.id} value={r.id}>{r.role_name} ({r.role_code})</option>)}
              </select>
            </div>
            {selTable && selRole && (
              <button onClick={saveColumnPerms} disabled={colSaving} className="btn-primary">
                {colSaving ? 'Saving...' : <><Save size={12} /> Save Permissions</>}
              </button>
            )}
          </div>

          {/* Column table */}
          {selTable && selRole ? (
            <div className="card overflow-hidden">
              <div className="card-header flex items-center justify-between">
                <span className="text-[11px] font-semibold">{tableColumns.length} columns in {selTable}</span>
                <span className="text-[10px] text-gray-500">
                  Role: {roles.find(r => r.id === Number(selRole))?.role_name || selRole}
                </span>
              </div>
              {colLoading ? (
                <div className="p-8 text-center text-gray-400 text-[11px]">Loading columns...</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full">
                    <thead>
                      <tr className="bg-gray-50 border-b">
                        <th className="px-3 py-2 text-left">Column</th>
                        <th className="px-3 py-2 text-center w-20">Visible</th>
                        <th className="px-3 py-2 text-center w-20">Can Edit</th>
                        <th className="px-3 py-2 text-center w-20">Masked</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y">
                      {tableColumns.map(col => {
                        const p = getColPerm(col)
                        return (
                          <tr key={col} className={p.is_masked ? 'bg-amber-50 border-amber-100' : 'hover:bg-gray-50'}>
                            <td className="px-3 py-1.5">
                              <div className="flex items-center gap-2">
                                {p.is_masked ? (
                                  <>
                                    <Lock size={11} className="text-amber-500 shrink-0" />
                                    <span className="text-[11px] font-semibold text-amber-600 line-through">{col}</span>
                                    <span className="text-[10px] font-mono tracking-widest text-amber-400">*****</span>
                                  </>
                                ) : (
                                  <code className="text-[11px] font-semibold text-gray-800">{col}</code>
                                )}
                              </div>
                            </td>
                            <td className="px-3 py-1.5 text-center">
                              <input type="checkbox" checked={p.is_visible}
                                onChange={e => setColPerm(col, 'is_visible', e.target.checked)}
                                className="w-3.5 h-3.5 rounded border-gray-300 text-primary-600 cursor-pointer" />
                            </td>
                            <td className="px-3 py-1.5 text-center">
                              <input type="checkbox" checked={p.can_edit}
                                onChange={e => setColPerm(col, 'can_edit', e.target.checked)}
                                className="w-3.5 h-3.5 rounded border-gray-300 text-emerald-600 cursor-pointer" />
                            </td>
                            <td className="px-3 py-1.5 text-center">
                              <input type="checkbox" checked={p.is_masked}
                                onChange={e => setColPerm(col, 'is_masked', e.target.checked)}
                                className="w-3.5 h-3.5 rounded border-gray-300 text-amber-600 cursor-pointer" />
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          ) : (
            <div className="card p-12 text-center text-gray-400">
              <Lock size={32} className="mx-auto mb-2 opacity-20" />
              <div className="text-[11px]">Select a table and role to manage column permissions</div>
              <div className="text-[9px] text-gray-400 mt-1">
                <b>Visible</b> = column shown to user &nbsp;|&nbsp;
                <b>Can Edit</b> = column editable in data editor &nbsp;|&nbsp;
                <b>Masked</b> = column value hidden (e.g. *****)
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
