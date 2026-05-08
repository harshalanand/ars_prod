/**
 * ContribReviewPage – List result tables, preview with filters, background export with auto-download.
 * Export jobs: show only while pending/running, auto-download on complete, then auto-delete.
 */
import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { contribAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  ClipboardCheck, Download, Trash2, RefreshCw, Table2, Eye, Search,
  ChevronDown, CheckCircle2, XCircle, Loader, Clock, FileDown, Filter, X
} from 'lucide-react'
import { C } from '@/theme/colors'

/* ── Small filter dropdown ── */
function FilterDropdown({ column, options, selected, onChange }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const ref = useRef(null)
  const filtered = useMemo(() => options.filter(o => o.toLowerCase().includes(q.toLowerCase())), [options, q])

  useEffect(() => {
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const toggle = (v) => onChange(column, selected.includes(v) ? selected.filter(x => x !== v) : [...selected, v])

  return (
    <div ref={ref} style={{ position: 'relative', display: 'inline-block' }}>
      <button onClick={() => setOpen(!open)} style={{
        padding: '2px 6px', borderRadius: 4, fontSize: 9, fontWeight: 700, cursor: 'pointer',
        border: `1px solid ${selected.length ? C.primary : '#e2e8f0'}`,
        background: selected.length ? C.primaryLight : '#fff',
        color: selected.length ? C.primary : C.textMuted,
        display: 'flex', alignItems: 'center', gap: 2,
      }}>
        <Filter size={8} /> {selected.length ? `${selected.length}` : ''}
        <ChevronDown size={8} />
      </button>
      {open && (
        <div style={{
          position: 'absolute', top: '100%', left: 0, zIndex: 100, background: '#fff',
          border: `1px solid ${C.cardBorder}`, borderRadius: 8, boxShadow: '0 8px 24px rgba(0,0,0,.15)',
          marginTop: 2, width: 200, maxHeight: 280, display: 'flex', flexDirection: 'column',
        }}>
          <div style={{ padding: '6px 8px', borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', gap: 4, alignItems: 'center' }}>
            <Search size={11} color={C.textMuted} />
            <input value={q} onChange={e => setQ(e.target.value)} placeholder={`Filter ${column}...`} autoFocus
              style={{ flex: 1, border: 'none', outline: 'none', fontSize: 11, background: 'transparent' }} />
            {selected.length > 0 && (
              <button onClick={() => onChange(column, [])} style={{ fontSize: 9, color: C.red, background: 'none', border: 'none', cursor: 'pointer', fontWeight: 700 }}>Clear</button>
            )}
          </div>
          <div style={{ overflowY: 'auto', maxHeight: 220 }}>
            {filtered.map(o => (
              <div key={o} onClick={() => toggle(o)} style={{
                padding: '4px 10px', cursor: 'pointer', fontSize: 11, display: 'flex', alignItems: 'center', gap: 6,
                background: selected.includes(o) ? C.primaryLight : '#fff',
              }}>
                <span style={{
                  width: 14, height: 14, borderRadius: 3, border: `1.5px solid ${selected.includes(o) ? C.primary : '#d1d5db'}`,
                  background: selected.includes(o) ? C.primary : '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center',
                  color: '#fff', fontSize: 8, fontWeight: 800, flexShrink: 0,
                }}>{selected.includes(o) ? '✓' : ''}</span>
                {o}
              </div>
            ))}
            {filtered.length === 0 && <div style={{ padding: 8, textAlign: 'center', color: C.textMuted, fontSize: 11 }}>No results</div>}
          </div>
        </div>
      )}
    </div>
  )
}


export default function ContribReviewPage() {
  const [tables, setTables] = useState([])
  const [loading, setLoading] = useState(false)
  const [selected, setSelected] = useState(null)
  const [preview, setPreview] = useState(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [search, setSearch] = useState('')

  // Filters: { col: [val1, val2] }
  const [filters, setFilters] = useState({})
  const [filterOptions, setFilterOptions] = useState({}) // { col: [distinct values] }

  // Export jobs — only track active (pending/running) ones
  const [activeExports, setActiveExports] = useState([])  // [{id, table_name, status, processed_rows, total_rows}]
  const downloadedRef = useRef(new Set())  // track already auto-downloaded export IDs
  const pollRef = useRef(null)
  const [exporting, setExporting] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const { data } = await contribAPI.listTables()
      setTables(data.data?.tables || [])
    } catch { toast.error('Failed to load tables') }
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  // Poll export jobs — only when there are active ones
  const refreshExports = useCallback(async () => {
    try {
      const { data } = await contribAPI.listExports()
      const all = data.data?.exports || []
      setActiveExports(all)

      // Auto-download completed jobs, then delete them
      for (const exp of all) {
        if (exp.status === 'completed' && !downloadedRef.current.has(exp.id)) {
          downloadedRef.current.add(exp.id)
          // Trigger download
          try {
            const res = await contribAPI.downloadExport(exp.id)
            const ct = res.headers?.['content-type'] || ''
            const ext = ct.includes('zip') ? 'zip' : 'csv'
            const a = document.createElement('a')
            a.href = URL.createObjectURL(new Blob([res.data]))
            a.download = `${exp.table_name}.${ext}`
            a.click()
            URL.revokeObjectURL(a.href)
            toast.success(`Downloaded: ${exp.table_name}`)
          } catch {
            toast.error(`Download failed: ${exp.table_name}`)
          }
          // Auto-delete after download
          try { await contribAPI.deleteExport(exp.id) } catch {}
        }
        // Also auto-delete failed jobs after 10 seconds
        if (exp.status === 'failed' && exp.finished_at) {
          const elapsed = (Date.now() - new Date(exp.finished_at).getTime()) / 1000
          if (elapsed > 10) {
            try { await contribAPI.deleteExport(exp.id) } catch {}
          }
        }
      }
    } catch {}
  }, [])

  useEffect(() => {
    refreshExports()
    pollRef.current = setInterval(refreshExports, 2000)
    return () => clearInterval(pollRef.current)
  }, [refreshExports])

  // Only show pending/running exports
  const visibleExports = activeExports.filter(e => e.status === 'pending' || e.status === 'running')

  // Fetch preview from server with current filters
  const fetchPreview = useCallback(async (name, currentFilters) => {
    setPreviewLoading(true)
    try {
      const { data } = await contribAPI.previewTable(name, 200, currentFilters)
      setPreview(data.data)
      setFilterOptions(data.data?.filter_options || {})
    } catch { toast.error('Preview failed') }
    finally { setPreviewLoading(false) }
  }, [])

  const handlePreview = async (name) => {
    setSelected(name); setPreview(null); setFilters({})
    fetchPreview(name, {})
  }

  const handleFilterChange = (col, vals) => {
    const newFilters = { ...filters, [col]: vals }
    setFilters(newFilters)
    if (selected) fetchPreview(selected, newFilters)
  }

  const clearAllFilters = () => {
    setFilters({})
    if (selected) fetchPreview(selected, {})
  }

  const activeFilterCount = Object.values(filters).filter(v => v.length > 0).length

  // Start background export job with current filters
  const handleExport = async (name) => {
    if (exporting) return
    setExporting(true)
    try {
      // Build filter object — only include non-empty filters
      const exportFilters = {}
      for (const [col, vals] of Object.entries(filters)) {
        if (vals.length > 0) exportFilters[col] = vals
      }
      await contribAPI.startExport(name, exportFilters)
      toast.success('Export started — will auto-download when ready')
      refreshExports()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Failed to start export')
    } finally { setExporting(false) }
  }

  const handleDelete = async (name) => {
    if (!confirm(`Delete table "${name}"? This cannot be undone.`)) return
    try {
      await contribAPI.deleteTable(name); toast.success('Deleted')
      if (selected === name) { setSelected(null); setPreview(null); setFilters({}); setFilterOptions({}) }
      load()
    } catch { toast.error('Delete failed') }
  }

  const filtered = tables.filter(t => t.toLowerCase().includes(search.toLowerCase()))

  return (
    <div style={{ color: C.text }}>
      <h1 style={{ fontSize: 20, fontWeight: 800, margin: '0 0 16px', display: 'flex', alignItems: 'center', gap: 10 }}>
        <ClipboardCheck size={20} color={C.primary} /> Contribution % — Review & Export
      </h1>

      {/* ── ACTIVE EXPORT JOBS (only pending/running) ── */}
      {visibleExports.length > 0 && (
        <div style={{ background: C.amberBg, border: `1px solid ${C.amberBd}`, borderRadius: 10, padding: '10px 18px', marginBottom: 16 }}>
          {visibleExports.map(exp => {
            const progress = exp.total_rows > 0 ? Math.round(exp.processed_rows / exp.total_rows * 100) : 0
            return (
              <div key={exp.id} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '4px 0' }}>
                <Loader size={14} color={C.amber} style={{ animation: 'spin 1s linear infinite' }} />
                <span style={{ fontSize: 12, fontWeight: 700, color: C.text }}>{exp.table_name}</span>
                <span style={{ fontSize: 11, color: C.amber, fontWeight: 600 }}>
                  {exp.status === 'running'
                    ? `${exp.processed_rows?.toLocaleString()} / ${exp.total_rows?.toLocaleString()} rows (${progress}%)`
                    : 'Queued...'}
                </span>
                {exp.status === 'running' && (
                  <div style={{ flex: 1, maxWidth: 200, height: 4, background: '#fde68a', borderRadius: 2, overflow: 'hidden' }}>
                    <div style={{ height: '100%', background: C.amber, borderRadius: 2, width: `${progress}%`, transition: 'width .3s' }} />
                  </div>
                )}
                <span style={{ fontSize: 10, color: C.textMuted }}>Auto-downloads when ready</span>
              </div>
            )
          })}
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '320px 1fr', gap: 16 }}>
        {/* Left: Table list */}
        <div style={{ background: C.cardBg, border: `1px solid ${C.cardBorder}`, borderRadius: 12, overflow: 'hidden' }}>
          <div style={{ padding: '12px 14px', background: C.headerBg, borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span style={{ fontSize: 13, fontWeight: 700 }}>{tables.length} Result Tables</span>
            <button onClick={load} style={{ background: 'none', border: 'none', cursor: 'pointer' }}>
              <RefreshCw size={14} color={C.textMuted} style={{ animation: loading ? 'spin 1s linear infinite' : 'none' }} />
            </button>
          </div>

          <div style={{ padding: '8px 10px', borderBottom: `1px solid ${C.cardBorder}` }}>
            <div style={{ position: 'relative' }}>
              <Search size={13} style={{ position: 'absolute', left: 8, top: '50%', transform: 'translateY(-50%)', color: C.textMuted }} />
              <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search tables..."
                style={{ width: '100%', padding: '6px 8px 6px 28px', borderRadius: 6, border: `1px solid ${C.inputBorder}`, background: C.inputBg, color: C.text, fontSize: 12, boxSizing: 'border-box' }} />
            </div>
          </div>

          <div style={{ maxHeight: 'calc(100vh - 280px)', overflowY: 'auto' }}>
            {filtered.map(t => (
              <div key={t} style={{
                padding: '10px 14px', borderBottom: `1px solid ${C.cardBorder}`, cursor: 'pointer',
                background: selected === t ? C.primaryLight : '#fff',
              }} onClick={() => handlePreview(t)}>
                <div style={{ fontSize: 12, fontWeight: 600, color: C.text, wordBreak: 'break-all' }}>{t}</div>
                <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                  <button onClick={e => { e.stopPropagation(); handlePreview(t) }}
                    style={{ padding: '3px 8px', borderRadius: 5, fontSize: 10, fontWeight: 600, border: `1px solid ${C.primaryBd}`, background: C.primaryLight, color: C.primary, cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 3 }}>
                    <Eye size={10} /> Preview
                  </button>
                  <button onClick={e => { e.stopPropagation(); handleDelete(t) }}
                    style={{ padding: '3px 8px', borderRadius: 5, fontSize: 10, fontWeight: 600, border: '1px solid #fecaca', background: '#fef2f2', color: C.red, cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 3 }}>
                    <Trash2 size={10} /> Delete
                  </button>
                </div>
              </div>
            ))}
            {filtered.length === 0 && (
              <div style={{ padding: 30, textAlign: 'center', color: C.textMuted, fontSize: 13 }}>
                {tables.length === 0 ? 'No result tables yet. Run Execute first.' : 'No tables match search.'}
              </div>
            )}
          </div>
        </div>

        {/* Right: Preview with filters */}
        <div style={{ background: C.cardBg, border: `1px solid ${C.cardBorder}`, borderRadius: 12, overflow: 'hidden' }}>
          {!selected ? (
            <div style={{ padding: 60, textAlign: 'center', color: C.textMuted }}>
              <Table2 size={32} color={C.cardBorder} style={{ margin: '0 auto 12px' }} />
              <div style={{ fontSize: 14, fontWeight: 600 }}>Select a table to preview</div>
            </div>
          ) : previewLoading ? (
            <div style={{ padding: 60, textAlign: 'center', color: C.textMuted }}>
              <RefreshCw size={20} style={{ animation: 'spin 1s linear infinite', margin: '0 auto 10px', display: 'block' }} />
              Loading preview...
            </div>
          ) : preview ? (
            <>
              {/* Header with export button */}
              <div style={{ padding: '10px 18px', background: C.headerBg, borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <Table2 size={14} color={C.primary} />
                <span style={{ fontSize: 13, fontWeight: 700 }}>{selected}</span>
                <span style={{ fontSize: 11, color: C.textMuted }}>
                  {preview.total_rows?.toLocaleString()} rows · {preview.columns?.length} cols
                  {activeFilterCount > 0 && ` · ${preview.filtered_rows?.toLocaleString()} matched`}
                </span>
                <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
                  {activeFilterCount > 0 && (
                    <button onClick={clearAllFilters} style={{
                      display: 'flex', alignItems: 'center', gap: 3, padding: '4px 10px', borderRadius: 6, fontSize: 10, fontWeight: 700,
                      border: '1px solid #fecaca', background: '#fef2f2', color: C.red, cursor: 'pointer',
                    }}><X size={10} /> Clear {activeFilterCount} filter{activeFilterCount > 1 ? 's' : ''}</button>
                  )}
                  <button onClick={() => handleExport(selected)} disabled={exporting} style={{
                    display: 'flex', alignItems: 'center', gap: 4, padding: '5px 14px', borderRadius: 6, fontSize: 12, fontWeight: 700,
                    border: `1px solid ${exporting ? C.amberBd : C.greenBd}`, background: exporting ? C.amberBg : C.greenBg,
                    color: exporting ? C.amber : C.green, cursor: exporting ? 'wait' : 'pointer',
                  }}>
                    {exporting
                      ? <><Loader size={12} style={{ animation: 'spin 1s linear infinite' }} /> Starting...</>
                      : <><FileDown size={12} /> Export{activeFilterCount > 0 ? ' (filtered)' : ''}</>}
                  </button>
                </div>
              </div>

              {/* Active filter tags */}
              {activeFilterCount > 0 && (
                <div style={{ padding: '6px 18px', background: '#fefce8', borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
                  <Filter size={11} color={C.amber} />
                  <span style={{ fontSize: 10, fontWeight: 700, color: C.amber }}>Filters:</span>
                  {Object.entries(filters).filter(([, v]) => v.length > 0).map(([col, vals]) => (
                    <span key={col} style={{
                      display: 'flex', alignItems: 'center', gap: 3, padding: '2px 8px', borderRadius: 10, fontSize: 10, fontWeight: 600,
                      background: C.primaryLight, color: C.primary, border: `1px solid ${C.primaryBd}`,
                    }}>
                      {col}: {vals.length <= 2 ? vals.join(', ') : `${vals.length} selected`}
                      <button onClick={() => handleFilterChange(col, [])} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}>
                        <X size={9} color={C.primary} />
                      </button>
                    </span>
                  ))}
                </div>
              )}

              {/* Table with filter icons in headers */}
              <div style={{ overflowX: 'auto', maxHeight: 'calc(100vh - 320px)' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                  <thead style={{ position: 'sticky', top: 0, zIndex: 2 }}>
                    <tr style={{ background: '#f1f5f9' }}>
                      <th style={{ padding: '6px 10px', fontSize: 10, fontWeight: 700, color: C.textMuted, borderBottom: `2px solid ${C.cardBorder}`, width: 40 }}>#</th>
                      {preview.columns?.map(c => (
                        <th key={c} style={{ padding: '4px 8px', textAlign: 'left', fontSize: 9, fontWeight: 700, color: C.textSub, whiteSpace: 'nowrap', borderBottom: `2px solid ${C.cardBorder}` }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                            {c}
                            {filterOptions[c] && (
                              <FilterDropdown
                                column={c}
                                options={filterOptions[c]}
                                selected={filters[c] || []}
                                onChange={handleFilterChange}
                              />
                            )}
                          </div>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {preview.preview.map((row, i) => (
                      <tr key={i} style={{ borderBottom: `1px solid ${C.cardBorder}`, background: i % 2 === 0 ? '#fff' : '#fafbfc' }}>
                        <td style={{ padding: '4px 10px', fontSize: 10, color: C.textMuted, textAlign: 'center' }}>{i + 1}</td>
                        {preview.columns?.map(c => (
                          <td key={c} style={{ padding: '4px 10px', whiteSpace: 'nowrap', maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                            {row[c] != null ? String(row[c]) : ''}
                          </td>
                        ))}
                      </tr>
                    ))}
                    {preview.preview.length === 0 && (
                      <tr><td colSpan={999} style={{ padding: 30, textAlign: 'center', color: C.textMuted, fontSize: 12 }}>No rows match current filters</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </>
          ) : null}
        </div>
      </div>
      <style>{`@keyframes spin{to{transform:rotate(360deg);}}`}</style>
    </div>
  )
}
