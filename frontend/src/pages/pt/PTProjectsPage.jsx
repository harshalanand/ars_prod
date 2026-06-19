// Project Tracker — All Projects (list view with hierarchy indentation)
import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Plus, Search, X, FolderTree, Filter, Pencil, Archive,
         ArrowUp, ArrowDown, ChevronsUpDown } from 'lucide-react'
import toast from 'react-hot-toast'
import { ptAPI } from '@/services/api'
import { StatusBadge, PriorityChip, PhaseChip } from '@/components/pt/StatusBadge'
import StatusPicker from '@/components/pt/StatusPicker'
import ProjectForm from '@/components/pt/ProjectForm'

const inputStyle = {
  padding: '6px 10px', border: '1px solid #d1d5db', borderRadius: 6,
  fontSize: 12, background: '#fff', outline: 'none',
}

function ProgressBar({ pct }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <div style={{ flex: 1, height: 6, background: '#e5e7eb', borderRadius: 999, overflow: 'hidden', minWidth: 60 }}>
        <div style={{
          height: '100%', width: `${Math.max(0, Math.min(100, pct || 0))}%`,
          background: pct >= 100 ? '#16a34a' : '#4f46e5', borderRadius: 999,
        }} />
      </div>
      <span style={{ fontSize: 11, color: '#6b7280', minWidth: 32 }}>{pct ?? 0}%</span>
    </div>
  )
}

// Build a depth-aware ordered list from a flat tree (PARENT_ID linked).
function buildOrdered(rows) {
  const byParent = new Map()
  rows.forEach(r => {
    const k = r.PARENT_ID ?? null
    if (!byParent.has(k)) byParent.set(k, [])
    byParent.get(k).push(r)
  })
  const out = []
  const walk = (parentId, depth) => {
    const kids = byParent.get(parentId) || []
    kids.forEach(k => {
      out.push({ ...k, _depth: depth })
      walk(k.PROJECT_ID, depth + 1)
    })
  }
  walk(null, 0)
  // Append orphans (parent not in current rows — happens when filtering)
  const seen = new Set(out.map(o => o.PROJECT_ID))
  rows.filter(r => !seen.has(r.PROJECT_ID)).forEach(r => out.push({ ...r, _depth: 0 }))
  return out
}

export default function PTProjectsPage() {
  const nav = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()

  // Filter state — initialised from URL so dashboard tile clicks deep-link.
  // `open_only` defaults to true: hide COMPLETED/CANCELLED unless asked.
  const [filters, setFilters] = useState(() => ({
    status:    searchParams.get('status') || '',
    priority:  searchParams.get('priority') || '',
    phase:     searchParams.get('phase') || '',
    owner:     searchParams.get('owner') || '',
    q:         searchParams.get('q') || '',
    overdue:   searchParams.get('overdue') === '1',
    archived:  searchParams.get('archived') === '1',
    open_only: searchParams.get('open_only') !== '0',  // default ON
  }))

  const [rows, setRows]       = useState([])
  const [enums, setEnums]     = useState(null)
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [editing, setEditing] = useState(null)
  // Sort state — null key = use hierarchical order from buildOrdered
  const [sort, setSort] = useState({ key: null, dir: 'asc' })

  // Sync URL ↔ filters
  useEffect(() => {
    const sp = {}
    Object.entries(filters).forEach(([k, v]) => {
      if (k === 'open_only') {
        // Persist only when user explicitly disables it (default is ON)
        if (v === false) sp[k] = '0'
      } else if (v && v !== false) {
        sp[k] = v === true ? '1' : v
      }
    })
    setSearchParams(sp, { replace: true })
  }, [filters])

  const load = () => {
    setLoading(true)
    const params = {}
    if (filters.status)    params.status    = filters.status
    if (filters.priority)  params.priority  = filters.priority
    if (filters.phase)     params.phase     = filters.phase
    if (filters.owner)     params.owner     = filters.owner
    if (filters.q)         params.q         = filters.q
    if (filters.overdue)   params.overdue   = true
    if (filters.archived)  params.archived  = true
    if (filters.open_only) params.open_only = true
    ptAPI.list(params)
      .then(res => setRows(res.data?.data || []))
      .finally(() => setLoading(false))
  }

  useEffect(() => { load() }, [filters])
  useEffect(() => { ptAPI.enums().then(res => setEnums(res.data?.data)) }, [])

  // For the parent picker in the form, fetch the full unfiltered tree
  const [allProjects, setAllProjects] = useState([])
  useEffect(() => {
    ptAPI.tree({ archived: false }).then(res => setAllProjects(res.data?.data || []))
  }, [showForm])

  // Priority + status get semantic order rather than alphabetical
  const PRIORITY_RANK = { CRITICAL: 1, HIGH: 2, MEDIUM: 3, LOW: 4 }
  const STATUS_RANK   = { DRAFT: 1, NOT_STARTED: 2, IN_PROGRESS: 3,
                          BLOCKED: 4, ON_HOLD: 5, COMPLETED: 6, CANCELLED: 7 }

  const sortValue = (row, key) => {
    switch (key) {
      case 'PROJECT_CODE':   return row.PROJECT_CODE || ''
      case 'NAME':           return (row.NAME || '').toLowerCase()
      case 'STATUS':         return STATUS_RANK[row.STATUS] ?? 99
      case 'PRIORITY':       return PRIORITY_RANK[row.PRIORITY] ?? 99
      case 'PHASE':          return row.PHASE || ''
      case 'OWNER_USERNAME': return (row.OWNER_USERNAME || '').toLowerCase()
      case 'DUE_DATE':       return row.DUE_DATE || ''
      case 'PROGRESS_PCT':   return row.PROGRESS_PCT ?? 0
      default:               return ''
    }
  }

  const ordered = useMemo(() => {
    if (!sort.key) return buildOrdered(rows)
    // Active sort → flat list, hierarchy ignored
    const copy = [...rows]
    const sign = sort.dir === 'desc' ? -1 : 1
    copy.sort((a, b) => {
      const av = sortValue(a, sort.key)
      const bv = sortValue(b, sort.key)
      // Push empty strings / null-equivalents to the end regardless of dir
      const aEmpty = av === '' || av == null
      const bEmpty = bv === '' || bv == null
      if (aEmpty && !bEmpty) return 1
      if (!aEmpty && bEmpty) return -1
      if (av < bv) return -1 * sign
      if (av > bv) return  1 * sign
      return 0
    })
    return copy.map(r => ({ ...r, _depth: 0 }))
  }, [rows, sort])

  const toggleSort = (key) => {
    setSort(prev => {
      if (prev.key !== key) return { key, dir: 'asc' }
      if (prev.dir === 'asc') return { key, dir: 'desc' }
      return { key: null, dir: 'asc' }  // third click clears sort
    })
  }

  const SortIcon = ({ col }) => {
    if (sort.key !== col) return <ChevronsUpDown size={11} color="#cbd5e1" />
    return sort.dir === 'asc' ? <ArrowUp size={11} /> : <ArrowDown size={11} />
  }

  const handleCreate = () => { setEditing(null); setShowForm(true) }
  const handleEdit   = (row) => { setEditing(row); setShowForm(true) }

  const handleSave = async (payload) => {
    try {
      let newId = null
      if (editing) {
        await ptAPI.update(editing.PROJECT_ID, payload)
        toast.success('Updated')
        newId = editing.PROJECT_ID
      } else {
        const res = await ptAPI.create(payload)
        toast.success(`Created ${res.data?.data?.project_code}`)
        newId = res.data?.data?.project_id ?? null
      }
      setShowForm(false); setEditing(null)
      load()
      return newId
    } catch (e) { /* toast handled by axios interceptor */ }
  }

  const handleArchive = async (id, name) => {
    if (!confirm(`Archive '${name}' and all sub-projects?`)) return
    try {
      await ptAPI.archive(id)
      toast.success('Archived')
      load()
    } catch (e) {}
  }

  const setF = (k, v) => setFilters(f => ({ ...f, [k]: v }))
  const clearFilters = () => setFilters({
    status: '', priority: '', phase: '', owner: '', q: '',
    overdue: false, archived: false, open_only: true,
  })

  return (
    <div style={{ padding: 20, background: '#f8fafc', minHeight: '100%' }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
        <div>
          <div style={{ fontSize: 20, fontWeight: 700, color: '#111827' }}>All Projects</div>
          <div style={{ fontSize: 12, color: '#6b7280' }}>{ordered.length} record{ordered.length === 1 ? '' : 's'} {filters.archived ? '(archived)' : ''}</div>
        </div>
        <button onClick={handleCreate} style={{
          padding: '8px 14px', background: '#4f46e5', color: '#fff', border: 'none',
          borderRadius: 6, cursor: 'pointer', fontSize: 12, fontWeight: 600,
          display: 'flex', alignItems: 'center', gap: 6,
        }}>
          <Plus size={14} /> New project
        </button>
      </div>

      {/* Filter bar */}
      <div style={{
        background: '#fff', borderRadius: 10, padding: 12, border: '1px solid #e5e7eb',
        marginBottom: 12, display: 'grid', gridTemplateColumns: 'repeat(7, 1fr) auto', gap: 8,
      }}>
        <div style={{ position: 'relative', gridColumn: 'span 2' }}>
          <Search size={14} style={{ position: 'absolute', left: 8, top: 8, color: '#9ca3af' }} />
          <input style={{ ...inputStyle, paddingLeft: 28, width: '100%' }}
            placeholder="Search code, name, description"
            value={filters.q} onChange={e => setF('q', e.target.value)} />
        </div>
        <select style={inputStyle} value={filters.status} onChange={e => setF('status', e.target.value)}>
          <option value="">Status: any</option>
          {(enums?.status || []).map(v => <option key={v}>{v}</option>)}
        </select>
        <select style={inputStyle} value={filters.priority} onChange={e => setF('priority', e.target.value)}>
          <option value="">Priority: any</option>
          {(enums?.priority || []).map(v => <option key={v}>{v}</option>)}
        </select>
        <select style={inputStyle} value={filters.phase} onChange={e => setF('phase', e.target.value)}>
          <option value="">Phase: any</option>
          {(enums?.phase || []).map(v => <option key={v}>{v}</option>)}
        </select>
        <input style={inputStyle} placeholder="Owner" value={filters.owner}
          onChange={e => setF('owner', e.target.value)} />
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: '#374151', whiteSpace: 'nowrap' }}
                 title="Hide completed & cancelled">
            <input type="checkbox" checked={filters.open_only}
              onChange={e => setF('open_only', e.target.checked)} /> Open only
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: '#374151', whiteSpace: 'nowrap' }}>
            <input type="checkbox" checked={filters.overdue}
              onChange={e => setF('overdue', e.target.checked)} /> Overdue only
          </label>
        </div>
        <button onClick={clearFilters} style={{
          padding: '6px 12px', border: '1px solid #d1d5db', background: '#fff',
          borderRadius: 6, fontSize: 12, cursor: 'pointer', display: 'flex',
          alignItems: 'center', gap: 4, color: '#6b7280',
        }}><X size={12} /> Clear</button>
      </div>

      {/* Table */}
      <div style={{
        background: '#fff', borderRadius: 10, border: '1px solid #e5e7eb',
        overflow: 'auto',
      }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead>
            <tr style={{ borderBottom: '1px solid #e5e7eb', textAlign: 'left', color: '#6b7280', background: '#f9fafb' }}>
              <SortableTh col="PROJECT_CODE"   label="Code"     minWidth={120} sort={sort} onClick={toggleSort} SortIcon={SortIcon} />
              <SortableTh col="NAME"           label="Name"                            sort={sort} onClick={toggleSort} SortIcon={SortIcon} />
              <SortableTh col="STATUS"         label="Status"   nowrap                 sort={sort} onClick={toggleSort} SortIcon={SortIcon} />
              <SortableTh col="PRIORITY"       label="Priority"                        sort={sort} onClick={toggleSort} SortIcon={SortIcon} />
              <SortableTh col="PHASE"          label="Phase"                           sort={sort} onClick={toggleSort} SortIcon={SortIcon} />
              <SortableTh col="OWNER_USERNAME" label="Owner"                           sort={sort} onClick={toggleSort} SortIcon={SortIcon} />
              <SortableTh col="DUE_DATE"       label="Due"                             sort={sort} onClick={toggleSort} SortIcon={SortIcon} />
              <SortableTh col="PROGRESS_PCT"   label="Progress" minWidth={130}         sort={sort} onClick={toggleSort} SortIcon={SortIcon} />
              <th style={{ padding: '10px 12px' }}></th>
            </tr>
          </thead>
          <tbody>
            {loading && <tr><td colSpan={9} style={{ padding: 20, textAlign: 'center', color: '#6b7280' }}>Loading…</td></tr>}
            {!loading && ordered.length === 0 && (
              <tr><td colSpan={9} style={{ padding: 30, textAlign: 'center', color: '#6b7280' }}>
                No projects yet. Click <strong>New project</strong> to create one.
              </td></tr>
            )}
            {!loading && ordered.map(r => (
              <tr key={r.PROJECT_ID}
                  onClick={() => nav(`/pt/projects/${r.PROJECT_ID}`)}
                  style={{ borderBottom: '1px solid #f1f5f9', cursor: 'pointer' }}>
                <td style={{ padding: '8px 12px', fontFamily: 'monospace', color: '#4f46e5', fontWeight: 600 }}>
                  {r.PROJECT_CODE}
                </td>
                <td style={{ padding: '8px 12px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, paddingLeft: (r._depth || 0) * 18 }}>
                    {r._depth > 0 && <span style={{ color: '#9ca3af' }}>↳</span>}
                    <span style={{ fontWeight: 500 }}>{r.NAME}</span>
                    {r.CHILDREN_COUNT > 0 && (
                      <span style={{ fontSize: 10, color: '#6b7280' }}>
                        ({r.CHILDREN_COUNT} child{r.CHILDREN_COUNT === 1 ? '' : 'ren'})
                      </span>
                    )}
                  </div>
                </td>
                <td style={{ padding: '8px 12px' }} onClick={e => e.stopPropagation()}>
                  <StatusPicker value={r.STATUS} onChange={async (next) => {
                    await ptAPI.update(r.PROJECT_ID, { status: next })
                    toast.success(`${r.PROJECT_CODE} → ${next}`)
                    load()
                  }} />
                </td>
                <td style={{ padding: '8px 12px' }}><PriorityChip value={r.PRIORITY} /></td>
                <td style={{ padding: '8px 12px' }}><PhaseChip value={r.PHASE} /></td>
                <td style={{ padding: '8px 12px', color: '#374151' }}>{r.OWNER_USERNAME || '—'}</td>
                <td style={{ padding: '8px 12px', color: r.IS_OVERDUE ? '#dc2626' : '#374151', fontWeight: r.IS_OVERDUE ? 700 : 400 }}>
                  {r.DUE_DATE?.slice(0, 10) || '—'}
                </td>
                <td style={{ padding: '8px 12px' }}><ProgressBar pct={r.PROGRESS_PCT} /></td>
                <td style={{ padding: '8px 12px', whiteSpace: 'nowrap' }} onClick={e => e.stopPropagation()}>
                  <button onClick={() => handleEdit(r)} title="Edit" style={iconBtn}>
                    <Pencil size={13} />
                  </button>
                  <button onClick={() => handleArchive(r.PROJECT_ID, r.NAME)} title="Archive" style={{ ...iconBtn, color: '#dc2626' }}>
                    <Archive size={13} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {showForm && (
        <ProjectForm
          initial={editing}
          parents={allProjects}
          enums={enums}
          onSave={handleSave}
          onClose={() => { setShowForm(false); setEditing(null) }}
        />
      )}
    </div>
  )
}

const iconBtn = {
  background: 'transparent', border: 'none', padding: 6, cursor: 'pointer',
  color: '#6b7280', borderRadius: 4, marginLeft: 2,
}

function SortableTh({ col, label, minWidth, nowrap, sort, onClick, SortIcon }) {
  const active = sort.key === col
  return (
    <th onClick={() => onClick(col)}
        title={active
          ? `Sorted ${sort.dir === 'asc' ? 'ascending' : 'descending'} — click to ${sort.dir === 'asc' ? 'reverse' : 'clear'}`
          : `Sort by ${label}`}
        style={{
          padding: '10px 12px',
          minWidth: minWidth || undefined,
          whiteSpace: nowrap ? 'nowrap' : undefined,
          cursor: 'pointer',
          userSelect: 'none',
          color: active ? '#4f46e5' : undefined,
        }}>
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
        {label}
        <SortIcon col={col} />
      </span>
    </th>
  )
}
