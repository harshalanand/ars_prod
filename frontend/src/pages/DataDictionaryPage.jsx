/**
 * DataDictionaryPage — searchable, editable reference of every important
 * column: full form, purpose, related tables, and the formula behind it.
 */
import { useEffect, useMemo, useState } from 'react'
import { dataDictionaryAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  BookOpen, Search, Plus, Pencil, Trash2, X, Save, Loader2, RefreshCw, Download,
} from 'lucide-react'

const MODULE_COLORS = {
  MSA:        { bg: '#ecfeff', fg: '#0891b2', bd: '#a5f3fc' },
  Grid:       { bg: '#ecfdf5', fg: '#059669', bd: '#a7f3d0' },
  Listing:    { bg: '#eef2ff', fg: '#4f46e5', bd: '#c7d2fe' },
  Allocation: { bg: '#fffbeb', fg: '#d97706', bd: '#fde68a' },
  Master:     { bg: '#f5f3ff', fg: '#7c3aed', bd: '#ddd6fe' },
}

const EMPTY_FORM = { column_name: '', abbreviation: '', purpose: '', related_tables: '', formula: '', module: 'Listing' }

function ModuleTag({ module }) {
  if (!module) return null
  const c = MODULE_COLORS[module] || { bg: '#f1f5f9', fg: '#64748b', bd: '#e2e8f0' }
  return (
    <span className="inline-block text-[10px] font-bold px-2 py-0.5 rounded-full whitespace-nowrap"
      style={{ background: c.bg, color: c.fg, border: `1px solid ${c.bd}` }}>
      {module}
    </span>
  )
}

function EntryForm({ initial, onSave, onCancel, saving }) {
  const [form, setForm] = useState(initial)
  const set = (k) => (e) => setForm(f => ({ ...f, [k]: e.target.value }))
  const input = 'w-full text-xs border border-gray-200 rounded-md px-2.5 py-1.5 bg-white focus:outline-none focus:ring-2 focus:ring-primary-300'
  return (
    <div className="bg-primary-50/50 border border-primary-200 rounded-lg p-4 space-y-3">
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div>
          <label className="block text-[10px] font-bold uppercase tracking-wide text-gray-500 mb-1">Column name *</label>
          <input className={input} value={form.column_name} onChange={set('column_name')} placeholder="e.g. FNL_Q" autoFocus/>
        </div>
        <div>
          <label className="block text-[10px] font-bold uppercase tracking-wide text-gray-500 mb-1">Abbreviation / full form</label>
          <input className={input} value={form.abbreviation || ''} onChange={set('abbreviation')} placeholder="e.g. Final Quantity"/>
        </div>
        <div>
          <label className="block text-[10px] font-bold uppercase tracking-wide text-gray-500 mb-1">Module</label>
          <select className={input} value={form.module || ''} onChange={set('module')}>
            {['MSA', 'Grid', 'Listing', 'Allocation', 'Master', 'Other'].map(m => <option key={m} value={m}>{m}</option>)}
          </select>
        </div>
      </div>
      <div>
        <label className="block text-[10px] font-bold uppercase tracking-wide text-gray-500 mb-1">Purpose — what it means, in plain words</label>
        <textarea className={input} rows={2} value={form.purpose || ''} onChange={set('purpose')}
          placeholder="e.g. Free-to-allocate stock per option per RDC…"/>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div>
          <label className="block text-[10px] font-bold uppercase tracking-wide text-gray-500 mb-1">Related tables (comma-separated)</label>
          <input className={input} value={form.related_tables || ''} onChange={set('related_tables')}
            placeholder="e.g. ARS_MSA_GEN_ART, ARS_MSA_TOTAL"/>
        </div>
        <div>
          <label className="block text-[10px] font-bold uppercase tracking-wide text-gray-500 mb-1">Algorithm / formula (if derived)</label>
          <input className={input + ' font-mono'} value={form.formula || ''} onChange={set('formula')}
            placeholder="e.g. FNL_Q = MAX(STK_QTY − PEND_QTY, 0)"/>
        </div>
      </div>
      <div className="flex items-center gap-2 pt-1">
        <button onClick={() => onSave(form)} disabled={saving || !form.column_name.trim()}
          className="inline-flex items-center gap-1.5 text-xs font-bold text-white bg-primary-600 hover:bg-primary-700 disabled:bg-gray-300 rounded-md px-4 py-1.5">
          {saving ? <Loader2 size={13} className="animate-spin"/> : <Save size={13}/>} Save
        </button>
        <button onClick={onCancel}
          className="inline-flex items-center gap-1.5 text-xs font-semibold text-gray-600 bg-white border border-gray-200 hover:bg-gray-50 rounded-md px-3 py-1.5">
          <X size={13}/> Cancel
        </button>
      </div>
    </div>
  )
}

export default function DataDictionaryPage() {
  const [entries, setEntries] = useState([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [moduleFilter, setModuleFilter] = useState('All')
  const [adding, setAdding] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [saving, setSaving] = useState(false)
  const [exporting, setExporting] = useState(false)

  // Download the dictionary as .xlsx, honouring the active search filter.
  const handleExport = async () => {
    setExporting(true)
    try {
      const res = await dataDictionaryAPI.exportXlsx(search.trim() || undefined)
      const url = window.URL.createObjectURL(new Blob([res.data]))
      const a = document.createElement('a')
      a.href = url
      a.download = 'ars_data_dictionary.xlsx'
      document.body.appendChild(a)
      a.click()
      a.remove()
      window.URL.revokeObjectURL(url)
      toast.success('Data dictionary exported')
    } catch (e) {
      toast.error('Export failed')
    } finally { setExporting(false) }
  }

  const load = async () => {
    setLoading(true)
    try {
      const res = await dataDictionaryAPI.list()
      setEntries(res.data?.data || [])
    } catch (e) {
      toast.error('Could not load data dictionary')
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { load() }, [])

  const modules = useMemo(
    () => ['All', ...new Set(entries.map(e => e.module).filter(Boolean))],
    [entries])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return entries.filter(e => {
      if (moduleFilter !== 'All' && e.module !== moduleFilter) return false
      if (!q) return true
      return ['column_name', 'abbreviation', 'purpose', 'related_tables', 'formula', 'module']
        .some(k => (e[k] || '').toLowerCase().includes(q))
    })
  }, [entries, search, moduleFilter])

  const handleCreate = async (form) => {
    setSaving(true)
    try {
      await dataDictionaryAPI.create(form)
      toast.success(`${form.column_name} added`)
      setAdding(false)
      load()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Save failed')
    } finally { setSaving(false) }
  }

  const handleUpdate = async (id, form) => {
    setSaving(true)
    try {
      await dataDictionaryAPI.update(id, form)
      toast.success(`${form.column_name} updated`)
      setEditingId(null)
      load()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Update failed')
    } finally { setSaving(false) }
  }

  const handleDelete = async (entry) => {
    if (!window.confirm(`Delete "${entry.column_name}" from the dictionary?`)) return
    try {
      await dataDictionaryAPI.remove(entry.id)
      toast.success(`${entry.column_name} deleted`)
      setEntries(prev => prev.filter(x => x.id !== entry.id))
    } catch (e) {
      toast.error('Delete failed')
    }
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 bg-white border border-gray-200 rounded-xl px-4 py-3 shadow-sm">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-primary-600 to-purple-600 flex items-center justify-center shadow">
            <BookOpen size={16} className="text-white"/>
          </div>
          <div>
            <h1 className="text-base font-bold text-gray-900 leading-tight">Data Dictionary</h1>
            <div className="text-[11px] text-gray-500">
              What every column means — full form, purpose, related tables, and the formula behind it
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={load} title="Reload"
            className="inline-flex items-center justify-center w-8 h-8 rounded-md border border-gray-200 bg-white text-gray-500 hover:text-gray-800 hover:bg-gray-50">
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''}/>
          </button>
          <button onClick={handleExport} disabled={exporting} title="Download as Excel (.xlsx)"
            className="inline-flex items-center gap-1.5 text-xs font-semibold text-gray-700 bg-white border border-gray-200 hover:bg-gray-50 disabled:opacity-50 rounded-md px-3 py-2">
            {exporting ? <Loader2 size={14} className="animate-spin"/> : <Download size={14}/>} Export
          </button>
          <button onClick={() => { setAdding(a => !a); setEditingId(null) }}
            className="inline-flex items-center gap-1.5 text-xs font-bold text-white bg-primary-600 hover:bg-primary-700 rounded-md px-4 py-2">
            <Plus size={14}/> Add entry
          </button>
        </div>
      </div>

      {/* Add form */}
      {adding && <EntryForm initial={EMPTY_FORM} onSave={handleCreate} onCancel={() => setAdding(false)} saving={saving}/>}

      {/* Search + module filter */}
      <div className="flex flex-wrap items-center gap-2 bg-white border border-gray-200 rounded-xl px-3 py-2.5 shadow-sm">
        <div className="relative flex-1 min-w-[220px]">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400"/>
          <input value={search} onChange={e => setSearch(e.target.value)}
            placeholder="Search column, meaning, table, or formula…"
            className="w-full text-xs border border-gray-200 rounded-md pl-8 pr-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-300"/>
        </div>
        <div className="flex items-center gap-1 flex-wrap">
          {modules.map(m => (
            <button key={m} onClick={() => setModuleFilter(m)}
              className={'text-[11px] font-semibold rounded-full px-3 py-1 border transition ' +
                (moduleFilter === m
                  ? 'bg-primary-600 text-white border-primary-600'
                  : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50')}>
              {m}
            </button>
          ))}
        </div>
        <div className="text-[11px] text-gray-400 ml-auto whitespace-nowrap">
          {filtered.length} of {entries.length} entries
        </div>
      </div>

      {/* Table */}
      <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full text-xs">
            <thead className="bg-gray-50 text-gray-600">
              <tr>
                {['Column', 'Full form', 'Purpose', 'Related tables', 'Algorithm / formula', 'Module', ''].map(h => (
                  <th key={h} className="px-3 py-2.5 text-left font-bold text-[10px] uppercase tracking-wide border-b border-gray-200 whitespace-nowrap">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr><td colSpan={7} className="px-4 py-10 text-center text-gray-400">
                  <Loader2 size={18} className="animate-spin inline-block mr-2"/> Loading…
                </td></tr>
              )}
              {!loading && filtered.length === 0 && (
                <tr><td colSpan={7} className="px-4 py-10 text-center text-gray-400">
                  No entries match — try a different search, or add one.
                </td></tr>
              )}
              {!loading && filtered.map(e => (
                editingId === e.id ? (
                  <tr key={e.id}>
                    <td colSpan={7} className="p-3">
                      <EntryForm initial={e} saving={saving}
                        onSave={(form) => handleUpdate(e.id, form)}
                        onCancel={() => setEditingId(null)}/>
                    </td>
                  </tr>
                ) : (
                  <tr key={e.id} className="border-b border-gray-100 hover:bg-gray-50/60 align-top">
                    <td className="px-3 py-2.5 font-mono font-bold text-primary-700 whitespace-nowrap">{e.column_name}</td>
                    <td className="px-3 py-2.5 text-gray-800 font-medium min-w-[140px]">{e.abbreviation || '—'}</td>
                    <td className="px-3 py-2.5 text-gray-600 max-w-[360px]">{e.purpose || '—'}</td>
                    <td className="px-3 py-2.5 max-w-[220px]">
                      <div className="flex flex-wrap gap-1">
                        {(e.related_tables || '').split(',').map(t => t.trim()).filter(Boolean).map(t => (
                          <span key={t} className="font-mono text-[10px] bg-gray-100 text-gray-600 rounded px-1.5 py-0.5">{t}</span>
                        ))}
                        {!e.related_tables && <span className="text-gray-400">—</span>}
                      </div>
                    </td>
                    <td className="px-3 py-2.5 max-w-[280px]">
                      {e.formula
                        ? <code className="text-[10.5px] bg-yellow-50 text-red-600 border border-yellow-200 rounded px-2 py-1 inline-block whitespace-pre-wrap">{e.formula}</code>
                        : <span className="text-gray-400">—</span>}
                    </td>
                    <td className="px-3 py-2.5"><ModuleTag module={e.module}/></td>
                    <td className="px-3 py-2.5 whitespace-nowrap">
                      <button onClick={() => { setEditingId(e.id); setAdding(false) }} title="Edit"
                        className="inline-flex items-center justify-center w-7 h-7 rounded-md text-gray-400 hover:text-primary-600 hover:bg-primary-50">
                        <Pencil size={13}/>
                      </button>
                      <button onClick={() => handleDelete(e)} title="Delete"
                        className="inline-flex items-center justify-center w-7 h-7 rounded-md text-gray-400 hover:text-red-600 hover:bg-red-50">
                        <Trash2 size={13}/>
                      </button>
                    </td>
                  </tr>
                )
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
