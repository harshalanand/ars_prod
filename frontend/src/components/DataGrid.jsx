/**
 * DataGrid — server-side paged table with per-column sort + filter.
 *
 * Wraps the data-fetching pattern used by /pend-alc/reco and /pend-alc/detail:
 *   {data, total_rows, page, page_size, total_pages}
 *
 * Props:
 *   columns: [
 *     { key, label, render?(row), align?, width?,
 *       sortable?, filterType?: 'multi' | 'text', filterOptions?: string[] }
 *   ]
 *   fetcher: async (params) => ({data, total_rows, page, page_size, total_pages})
 *     // Receives {page, page_size, sort_by, sort_dir, ...filters}
 *   pageSizes?: number[]            // default [50, 100, 250, 500, 1000]
 *   defaultPageSize?: number        // default 100
 *   defaultSortBy?: string          // default null (server uses its own default)
 *   defaultSortDir?: 'asc' | 'desc' // default 'desc'
 *   refreshKey?: any                // change to force a refetch
 *   rowKey?: (row, idx) => key      // default: row.id ?? idx
 *   stickyHeader?: boolean          // default true
 *   onRowClick?: (row) => void
 *   emptyText?: string
 *   compact?: boolean               // tighter row height
 *
 * The active filter and sort state are kept inside this component (not URL-
 * synced — keeping it simple). External filter changes can force a refetch
 * via `refreshKey`.
 */
import { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import { ChevronUp, ChevronDown, Filter, X } from 'lucide-react'

const C = {
  primary: '#4f46e5', text: '#1e293b', textSub: '#64748b', textMuted: '#94a3b8',
  border: '#e2e8f0', bg: '#f8fafc', card: '#ffffff', amber: '#d97706',
}

const DEFAULT_PAGE_SIZES = [50, 100, 250, 500, 1000]

export default function DataGrid({
  columns,
  fetcher,
  pageSizes = DEFAULT_PAGE_SIZES,
  defaultPageSize = 100,
  defaultSortBy = null,
  defaultSortDir = 'desc',
  refreshKey,
  rowKey = (r, i) => r?.id ?? i,
  stickyHeader = true,
  onRowClick,
  emptyText = 'No rows',
  compact = false,
}) {
  const [page, setPage]         = useState(1)
  const [pageSize, setPageSize] = useState(defaultPageSize)
  const [sortBy, setSortBy]     = useState(defaultSortBy)
  const [sortDir, setSortDir]   = useState(defaultSortDir)
  const [filters, setFilters]   = useState({})  // { columnKey: value | array }
  const [data, setData]         = useState([])
  const [total, setTotal]       = useState(0)
  const [totalPages, setTotalPages] = useState(1)
  const [loading, setLoading]   = useState(false)
  const [popoverCol, setPopoverCol] = useState(null)
  const popoverAnchorRef = useRef(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = { page, page_size: pageSize, sort_dir: sortDir }
      if (sortBy) params.sort_by = sortBy
      // filters → CSV strings
      Object.entries(filters).forEach(([k, v]) => {
        if (v == null || v === '' || (Array.isArray(v) && v.length === 0)) return
        params[k] = Array.isArray(v) ? v.join(',') : v
      })
      const res = await fetcher(params)
      const body = res?.data ?? res
      setData(body?.data || [])
      setTotal(body?.total_rows || 0)
      setTotalPages(body?.total_pages || 1)
    } catch (e) {
      console.error('DataGrid fetch failed', e)
      setData([]); setTotal(0); setTotalPages(1)
    } finally { setLoading(false) }
  }, [fetcher, page, pageSize, sortBy, sortDir, filters])

  useEffect(() => { load() }, [load, refreshKey])

  // Reset to page 1 when filters/sort change
  useEffect(() => { setPage(1) }, [sortBy, sortDir, JSON.stringify(filters), pageSize])

  const cycleSort = (key) => {
    if (sortBy !== key) {
      setSortBy(key); setSortDir('asc'); return
    }
    if (sortDir === 'asc') { setSortDir('desc'); return }
    setSortBy(null); setSortDir('desc')
  }

  const setColFilter = (key, value) => {
    setFilters(f => ({ ...f, [key]: value }))
  }
  const clearColFilter = (key) => {
    setFilters(f => { const n = {...f}; delete n[key]; return n })
  }
  const clearAllFilters = () => setFilters({})

  const activeFilterCount = useMemo(
    () => Object.values(filters).filter(v =>
      v != null && v !== '' && (!Array.isArray(v) || v.length > 0)
    ).length,
    [filters]
  )

  const startRow = total === 0 ? 0 : (page - 1) * pageSize + 1
  const endRow   = Math.min(page * pageSize, total)
  const tdPad    = compact ? '4px 8px' : '6px 10px'

  return (
    <div style={{ background: C.card, border: `1px solid ${C.border}`,
                  borderRadius: 8, overflow: 'hidden' }}>

      {/* Toolbar */}
      <div style={{ padding: '8px 12px', borderBottom: `1px solid ${C.border}`,
                    background: C.bg, display: 'flex', alignItems: 'center',
                    gap: 12, fontSize: 11 }}>
        <div style={{ color: C.textSub, fontWeight: 600 }}>
          {loading ? 'Loading…' : (
            total === 0 ? 'No rows' :
            `${startRow.toLocaleString()}–${endRow.toLocaleString()} of ${total.toLocaleString()}`
          )}
        </div>
        {activeFilterCount > 0 && (
          <button onClick={clearAllFilters}
            style={{ fontSize: 10, padding: '3px 9px', borderRadius: 3,
                     border: `1px solid ${C.amber}`, background: C.amber + '15',
                     color: C.amber, cursor: 'pointer', fontWeight: 600,
                     display: 'flex', alignItems: 'center', gap: 4 }}>
            <X size={10}/> Clear filters ({activeFilterCount})
          </button>
        )}
        <div style={{ flex: 1 }}/>
        <span style={{ color: C.textSub }}>Page size:</span>
        <select value={pageSize} onChange={e => setPageSize(parseInt(e.target.value, 10))}
          style={{ fontSize: 11, padding: '3px 6px', borderRadius: 3,
                   border: `1px solid ${C.border}`, outline: 'none' }}>
          {pageSizes.map(n => <option key={n} value={n}>{n}</option>)}
        </select>
        <Pager page={page} totalPages={totalPages} setPage={setPage}/>
      </div>

      {/* Table */}
      <div style={{ overflow: 'auto', maxHeight: '70vh' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
          <thead style={stickyHeader ? { position: 'sticky', top: 0, zIndex: 2 } : {}}>
            <tr style={{ background: C.bg }}>
              {columns.map(col => {
                const sorted = sortBy === col.key
                const filtered = filters[col.key] != null
                                 && filters[col.key] !== ''
                                 && (!Array.isArray(filters[col.key]) || filters[col.key].length > 0)
                return (
                  <th key={col.key}
                    style={{
                      padding: '6px 10px', textAlign: col.align || 'left',
                      fontSize: 9, fontWeight: 700,
                      color: C.textSub, letterSpacing: '.05em',
                      borderBottom: `2px solid ${C.border}`,
                      whiteSpace: 'nowrap', userSelect: 'none',
                      background: C.bg, width: col.width,
                    }}>
                    <div style={{ display: 'flex', alignItems: 'center',
                                  gap: 4, justifyContent: col.align === 'right' ? 'flex-end' : 'flex-start' }}>
                      <span onClick={col.sortable !== false ? () => cycleSort(col.key) : undefined}
                        style={{
                          cursor: col.sortable !== false ? 'pointer' : 'default',
                          color: sorted ? C.primary : C.textSub,
                          display: 'flex', alignItems: 'center', gap: 3,
                        }}>
                        {col.label}
                        {col.sortable !== false && sorted && (
                          sortDir === 'asc'
                            ? <ChevronUp size={10}/>
                            : <ChevronDown size={10}/>
                        )}
                      </span>
                      {col.filterType && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation()
                            popoverAnchorRef.current = e.currentTarget
                            setPopoverCol(popoverCol === col.key ? null : col.key)
                          }}
                          style={{
                            background: filtered ? C.primary : 'transparent',
                            color: filtered ? '#fff' : C.textMuted,
                            border: 'none', padding: 2, borderRadius: 3,
                            cursor: 'pointer', display: 'inline-flex',
                          }}>
                          <Filter size={9}/>
                        </button>
                      )}
                    </div>
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {data.length === 0 ? (
              <tr><td colSpan={columns.length}
                style={{ padding: 30, textAlign: 'center', color: C.textMuted }}>
                {loading ? 'Loading…' : emptyText}
              </td></tr>
            ) : data.map((row, idx) => (
              <tr key={rowKey(row, idx)}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                style={{ borderBottom: `1px solid ${C.border}`,
                         cursor: onRowClick ? 'pointer' : 'default' }}>
                {columns.map(col => (
                  <td key={col.key} style={{
                    padding: tdPad, textAlign: col.align || 'left',
                    color: C.text, whiteSpace: 'nowrap',
                  }}>
                    {col.render ? col.render(row) : (row[col.key] ?? '—')}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Filter popover */}
      {popoverCol && (() => {
        const col = columns.find(c => c.key === popoverCol)
        if (!col) return null
        return (
          <FilterPopover
            anchorEl={popoverAnchorRef.current}
            col={col}
            value={filters[popoverCol]}
            onChange={v => setColFilter(popoverCol, v)}
            onClear={() => clearColFilter(popoverCol)}
            onClose={() => setPopoverCol(null)}
          />
        )
      })()}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Pager — first / prev / next / last + page input
// ---------------------------------------------------------------------------
function Pager({ page, totalPages, setPage }) {
  const btn = (disabled) => ({
    fontSize: 10, padding: '3px 8px', borderRadius: 3,
    border: `1px solid ${C.border}`,
    background: disabled ? '#f1f5f9' : '#fff',
    color: disabled ? C.textMuted : C.textSub,
    cursor: disabled ? 'not-allowed' : 'pointer',
  })
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11 }}>
      <button style={btn(page <= 1)} disabled={page <= 1} onClick={() => setPage(1)}>⏮</button>
      <button style={btn(page <= 1)} disabled={page <= 1} onClick={() => setPage(p => Math.max(1, p-1))}>◀</button>
      <span style={{ minWidth: 80, textAlign: 'center', color: C.textSub }}>
        Page <input type="number" min="1" max={totalPages} value={page}
          onChange={e => {
            const n = parseInt(e.target.value, 10)
            if (n >= 1 && n <= totalPages) setPage(n)
          }}
          style={{ width: 50, fontSize: 11, padding: '2px 4px',
                   border: `1px solid ${C.border}`, borderRadius: 3,
                   textAlign: 'center', outline: 'none' }}/>
        {' '}/ {totalPages}
      </span>
      <button style={btn(page >= totalPages)} disabled={page >= totalPages}
        onClick={() => setPage(p => Math.min(totalPages, p+1))}>▶</button>
      <button style={btn(page >= totalPages)} disabled={page >= totalPages}
        onClick={() => setPage(totalPages)}>⏭</button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// FilterPopover — multi-select checkboxes (categorical) or text contains
// ---------------------------------------------------------------------------
function FilterPopover({ anchorEl, col, value, onChange, onClear, onClose }) {
  const ref = useRef(null)
  const [search, setSearch] = useState('')

  // Position relative to anchor
  const [pos, setPos] = useState({ top: 0, left: 0 })
  useEffect(() => {
    if (!anchorEl) return
    const rect = anchorEl.getBoundingClientRect()
    setPos({ top: rect.bottom + 4, left: rect.left })
  }, [anchorEl])

  // Close on outside click
  useEffect(() => {
    const onDoc = (e) => {
      if (ref.current && !ref.current.contains(e.target)
          && anchorEl && !anchorEl.contains(e.target)) onClose()
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [onClose, anchorEl])

  if (col.filterType === 'text') {
    return (
      <div ref={ref}
        style={{ position: 'fixed', top: pos.top, left: pos.left, zIndex: 10,
                 background: '#fff', border: `1px solid ${C.border}`,
                 borderRadius: 5, boxShadow: '0 6px 20px rgba(0,0,0,.12)',
                 padding: 10, width: 220 }}>
        <div style={{ fontSize: 9, fontWeight: 700, color: C.textSub,
                      letterSpacing: '.05em', marginBottom: 6 }}>
          FILTER {col.label.toUpperCase()}
        </div>
        <input value={value || ''} autoFocus
          placeholder="contains…"
          onChange={e => onChange(e.target.value)}
          style={{ width: '100%', fontSize: 11, padding: '5px 8px',
                   borderRadius: 3, border: `1px solid ${C.border}`,
                   outline: 'none', boxSizing: 'border-box' }}/>
        <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
          <button onClick={() => { onClear(); onClose() }}
            style={popBtn(C.border, '#fff', C.textSub)}>Clear</button>
          <div style={{ flex: 1 }}/>
          <button onClick={onClose}
            style={popBtn(C.primary, C.primary, '#fff')}>Done</button>
        </div>
      </div>
    )
  }

  // multi-select
  const options = col.filterOptions || []
  const selected = Array.isArray(value) ? value : []
  const filtered = options.filter(o =>
    !search || String(o).toLowerCase().includes(search.toLowerCase())
  )
  const toggle = (v) => {
    onChange(selected.includes(v)
      ? selected.filter(x => x !== v)
      : [...selected, v])
  }

  return (
    <div ref={ref}
      style={{ position: 'fixed', top: pos.top, left: pos.left, zIndex: 10,
               background: '#fff', border: `1px solid ${C.border}`,
               borderRadius: 5, boxShadow: '0 6px 20px rgba(0,0,0,.12)',
               padding: 10, width: 240, maxHeight: 360, overflow: 'hidden',
               display: 'flex', flexDirection: 'column' }}>
      <div style={{ fontSize: 9, fontWeight: 700, color: C.textSub,
                    letterSpacing: '.05em', marginBottom: 6 }}>
        FILTER {col.label.toUpperCase()}
      </div>
      <input value={search} onChange={e => setSearch(e.target.value)}
        placeholder="Search…"
        style={{ width: '100%', fontSize: 11, padding: '5px 8px',
                 borderRadius: 3, border: `1px solid ${C.border}`,
                 outline: 'none', boxSizing: 'border-box', marginBottom: 6 }}/>
      <div style={{ flex: 1, overflowY: 'auto', minHeight: 80 }}>
        {filtered.length === 0 ? (
          <div style={{ padding: 10, textAlign: 'center', color: C.textMuted, fontSize: 10 }}>
            No values
          </div>
        ) : filtered.map(o => (
          <label key={o}
            style={{ display: 'flex', alignItems: 'center', gap: 6,
                     padding: '3px 4px', fontSize: 11, cursor: 'pointer',
                     borderRadius: 3 }}
            onMouseEnter={e => e.currentTarget.style.background = C.bg}
            onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
            <input type="checkbox" checked={selected.includes(o)}
              onChange={() => toggle(o)}/>
            <span>{o}</span>
          </label>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
        <button onClick={() => { onClear(); onClose() }}
          style={popBtn(C.border, '#fff', C.textSub)}>Clear</button>
        <button onClick={() => onChange(filtered)}
          style={popBtn(C.border, '#fff', C.textSub)}>All</button>
        <div style={{ flex: 1 }}/>
        <button onClick={onClose}
          style={popBtn(C.primary, C.primary, '#fff')}>Done</button>
      </div>
    </div>
  )
}

const popBtn = (border, bg, color) => ({
  fontSize: 10, fontWeight: 700, padding: '4px 10px', borderRadius: 3,
  border: `1px solid ${border}`, background: bg, color,
  cursor: 'pointer',
})
