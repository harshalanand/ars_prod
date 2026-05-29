/**
 * ContribReportPage — Full-table paginated report (curated columns).
 *
 *  • Top: Result-table picker · MAJ_CAT / SEG / STATUS filters · vendor search
 *         · "Show all columns" toggle · Refresh
 *  • Grid: server-side paginated (500 rows/page) view of the chosen
 *          Cont_Percentage_* table. Vendor code (M_VND_CD) hidden — only
 *          vendor name shown.
 *  • Footer: pagination controls (← Prev · page X of Y · Next →) + total count.
 */
import { useEffect, useMemo, useState, useCallback, useRef } from 'react'
import { contribAPI } from '@/services/api'
import toast from 'react-hot-toast'
import { ClipboardCheck, RefreshCw, ChevronDown, ChevronLeft, ChevronRight, Search } from 'lucide-react'
import { C } from '@/theme/colors'

const fmtCell = (v, col) => {
  if (v === null || v === undefined || v === '') return ''
  if (typeof v === 'number') {
    if (/CONT%|GAP|ATR|MERCH_INPUT|RMN/.test(col)) {
      return (Math.abs(v) <= 1.5 ? (v * 100).toFixed(2) + '%' : v.toFixed(2))
    }
    return v.toFixed(4)
  }
  return String(v)
}


function Dropdown({ label, value, options, onChange, width = 200, disabled }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const ref = useRef(null)
  useEffect(() => {
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [])
  const sel = options.find(o => o.value === value)
  const filtered = options.filter(o => (o.label || '').toLowerCase().includes(q.toLowerCase()))
  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <button onClick={() => !disabled && setOpen(!open)} disabled={disabled} style={{
        display: 'flex', alignItems: 'center', gap: 6, padding: '6px 10px', borderRadius: 6,
        border: `1px solid ${value ? C.primary : C.cardBorder}`,
        background: disabled ? C.grayBg : (value ? C.primaryLight : '#fff'),
        cursor: disabled ? 'not-allowed' : 'pointer', fontSize: 12,
        minWidth: width, justifyContent: 'space-between',
        color: value ? C.primary : C.text,
      }}>
        <span style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: width - 30 }}>
          {label}: <strong>{sel?.label || 'All'}</strong>
        </span>
        <ChevronDown size={12} />
      </button>
      {open && (
        <div style={{
          position: 'absolute', top: '100%', left: 0, marginTop: 4, zIndex: 100,
          background: '#fff', border: `1px solid ${C.cardBorder}`, borderRadius: 6,
          boxShadow: '0 8px 24px rgba(0,0,0,.12)', minWidth: width, maxHeight: 360, display: 'flex', flexDirection: 'column',
        }}>
          {options.length > 8 && (
            <div style={{ padding: 6, borderBottom: `1px solid ${C.cardBorder}` }}>
              <input value={q} onChange={e => setQ(e.target.value)} placeholder="Filter…" autoFocus
                style={{ width: '100%', fontSize: 12, padding: '4px 6px', border: `1px solid ${C.inputBorder}`, borderRadius: 4 }} />
            </div>
          )}
          <div style={{ overflowY: 'auto', maxHeight: 300 }}>
            <div onClick={() => { onChange(''); setOpen(false); setQ('') }}
              style={{ padding: '6px 10px', cursor: 'pointer', fontSize: 12, color: C.textMuted, fontStyle: 'italic',
                       background: !value ? C.primaryLight : '#fff' }}>(All)</div>
            {filtered.map(o => (
              <div key={o.value} onClick={() => { onChange(o.value); setOpen(false); setQ('') }} style={{
                padding: '6px 10px', cursor: 'pointer', fontSize: 12,
                background: value === o.value ? C.primaryLight : '#fff',
                color: value === o.value ? C.primary : C.text,
              }}>{o.label}</div>
            ))}
            {filtered.length === 0 && <div style={{ padding: 8, color: C.textMuted, fontSize: 12 }}>No matches</div>}
          </div>
        </div>
      )}
    </div>
  )
}


export default function ContribReportPage() {
  const [tables, setTables] = useState([])
  const [tableName, setTableName] = useState('')

  const [filters, setFilters] = useState({ majcat: '', seg: '', status: '', q: '' })
  const [showAll, setShowAll] = useState(false)

  const [page, setPage] = useState(1)
  const PAGE_SIZE = 500

  const [data, setData] = useState({ columns: [], rows: [], total: 0, filter_options: {}, is_store: false })
  const [loading, setLoading] = useState(false)

  // Load tables once
  useEffect(() => {
    contribAPI.reportTables().then(({ data }) => {
      const list = data?.data?.tables || []
      setTables(list)
      if (list.length && !tableName) setTableName(list[0].table_name)
    }).catch(e => toast.error('Failed to load tables: ' + (e.message || e)))
  }, [])

  // Reset to page 1 when table or any filter changes
  useEffect(() => { setPage(1) }, [tableName, filters.majcat, filters.seg, filters.status, filters.q, showAll])

  const loadPage = useCallback(async () => {
    if (!tableName) return
    setLoading(true)
    try {
      const params = { table: tableName, page, page_size: PAGE_SIZE, show_all: showAll }
      if (filters.majcat) params.majcat = filters.majcat
      if (filters.seg)    params.seg    = filters.seg
      if (filters.status) params.status = filters.status
      if (filters.q)      params.q      = filters.q
      const { data: resp } = await contribAPI.reportPage(params)
      setData(resp?.data || { columns: [], rows: [], total: 0, filter_options: {}, is_store: false })
    } catch (e) {
      toast.error('Failed to load page: ' + (e.response?.data?.detail || e.message))
      setData({ columns: [], rows: [], total: 0, filter_options: {}, is_store: false })
    } finally {
      setLoading(false)
    }
  }, [tableName, page, filters, showAll])

  useEffect(() => { loadPage() }, [loadPage])

  const totalPages = Math.max(1, Math.ceil((data.total || 0) / PAGE_SIZE))

  const tableMeta = tables.find(t => t.table_name === tableName)

  const filterOpts = data.filter_options || {}

  return (
    <div style={{ padding: 14, background: C.pageBg, minHeight: '100vh' }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700, color: C.text, display: 'flex', alignItems: 'center', gap: 8 }}>
            <ClipboardCheck size={18} /> Contribution Report
          </h2>
          <p style={{ margin: '4px 0 0', color: C.textMuted, fontSize: 12 }}>
            Full-table view · curated columns · paginated 500 rows / page
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button onClick={loadPage} disabled={loading} style={btn('ghost')}>
            <RefreshCw size={13} /> Refresh
          </button>
        </div>
      </div>

      {/* Filter bar */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap', alignItems: 'center',
                    padding: 10, background: '#fff', border: `1px solid ${C.cardBorder}`, borderRadius: 6 }}>
        <Dropdown
          label="Table"
          value={tableName}
          width={260}
          options={tables.map(t => ({
            value: t.table_name,
            label: `${t.table_name} · ${t.level} · ${t.rows.toLocaleString()} rows`,
          }))}
          onChange={setTableName}
        />
        <Dropdown
          label="MAJ_CAT"
          value={filters.majcat}
          width={180}
          options={(filterOpts.MAJ_CAT || []).map(v => ({ value: v, label: v }))}
          onChange={v => setFilters(f => ({ ...f, majcat: v }))}
        />
        <Dropdown
          label="SEG"
          value={filters.seg}
          width={120}
          options={(filterOpts.SEG || []).map(v => ({ value: v, label: v }))}
          onChange={v => setFilters(f => ({ ...f, seg: v }))}
        />
        {data.is_store && (
          <Dropdown
            label="STATUS"
            value={filters.status}
            width={140}
            options={(filterOpts.STATUS || []).map(v => ({ value: v, label: v }))}
            onChange={v => setFilters(f => ({ ...f, status: v }))}
          />
        )}
        <div style={{ position: 'relative' }}>
          <Search size={12} color={C.textMuted}
            style={{ position: 'absolute', left: 8, top: '50%', transform: 'translateY(-50%)' }} />
          <input
            placeholder="Search vendor name…"
            value={filters.q}
            onChange={e => setFilters(f => ({ ...f, q: e.target.value }))}
            style={{ padding: '6px 8px 6px 26px', borderRadius: 6, border: `1px solid ${C.cardBorder}`,
                     fontSize: 12, width: 200, color: C.text, background: '#fff' }}
          />
        </div>
        <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: C.text, cursor: 'pointer', marginLeft: 'auto' }}>
          <input type="checkbox" checked={showAll} onChange={e => setShowAll(e.target.checked)}
            style={{ cursor: 'pointer', accentColor: C.primary }} />
          Show all columns
        </label>
      </div>

      {/* Grid */}
      <div style={{ background: '#fff', border: `1px solid ${C.cardBorder}`, borderRadius: 6,
                    overflow: 'auto', maxHeight: '70vh' }}>
        {loading ? (
          <div style={{ padding: 40, textAlign: 'center', color: C.textMuted }}>Loading…</div>
        ) : !tableName ? (
          <div style={{ padding: 40, textAlign: 'center', color: C.textMuted }}>Pick a result table to begin.</div>
        ) : !data.rows.length ? (
          <div style={{ padding: 40, textAlign: 'center', color: C.textMuted }}>
            No rows {Object.values(filters).some(Boolean) ? 'match the current filters.' : 'in this table.'}
          </div>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
            <thead style={{ position: 'sticky', top: 0, background: C.headerBg, zIndex: 5 }}>
              <tr>
                <th style={th}>#</th>
                {data.columns.map(c => <th key={c} style={th}>{c}</th>)}
              </tr>
            </thead>
            <tbody>
              {data.rows.map((r, i) => (
                <tr key={i} style={{ background: i % 2 ? C.rowAlt : '#fff' }}>
                  <td style={{ ...td, color: C.textMuted, fontVariantNumeric: 'tabular-nums' }}>
                    {(page - 1) * PAGE_SIZE + i + 1}
                  </td>
                  {data.columns.map(c => {
                    const v = r[c]
                    const isNum = typeof v === 'number'
                    return (
                      <td key={c} style={{ ...td, textAlign: isNum ? 'right' : 'left',
                                            fontVariantNumeric: 'tabular-nums',
                                            fontFamily: isNum ? 'ui-monospace, monospace' : 'inherit' }}>
                        {fmtCell(v, c)}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Pagination footer */}
      <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: 11, color: C.textMuted }}>
        <span>
          {data.total > 0 ? (
            <>Showing rows <strong>{(page-1)*PAGE_SIZE+1}</strong>–<strong>{Math.min(page*PAGE_SIZE, data.total)}</strong> of <strong>{data.total.toLocaleString()}</strong> · {data.columns.length} columns{showAll ? '' : ` (of ${data.all_columns?.length || '?'})`}</>
          ) : (
            <>Total: 0</>
          )}
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <button onClick={() => setPage(p => Math.max(1, p-1))} disabled={page <= 1 || loading} style={pageBtn(page <= 1)}>
            <ChevronLeft size={12} />
          </button>
          <span style={{ padding: '0 6px' }}>Page <strong>{page}</strong> of <strong>{totalPages}</strong></span>
          <button onClick={() => setPage(p => Math.min(totalPages, p+1))} disabled={page >= totalPages || loading} style={pageBtn(page >= totalPages)}>
            <ChevronRight size={12} />
          </button>
        </span>
      </div>

      {tableMeta && (
        <div style={{ marginTop: 4, fontSize: 10, color: C.textMuted }}>
          {tableMeta.level} table · vendor_col=<code>{tableMeta.vendor_col || '—'}</code> · total rows in table: {tableMeta.rows.toLocaleString()}
        </div>
      )}
    </div>
  )
}

const th = {
  padding: '6px 8px', borderBottom: `1px solid ${C.cardBorder}`, borderRight: `1px solid ${C.cardBorder}`,
  fontSize: 10, fontWeight: 700, color: C.text, textAlign: 'left', whiteSpace: 'nowrap', background: C.headerBg,
}
const td = {
  padding: '4px 8px', borderBottom: `1px solid ${C.cardBorder}`, borderRight: `1px solid ${C.cardBorder}`,
  whiteSpace: 'nowrap', color: C.text,
}
const btn = (variant) => ({
  display: 'flex', alignItems: 'center', gap: 6, padding: '6px 12px', borderRadius: 6,
  border: variant === 'primary' ? 'none' : `1px solid ${C.cardBorder}`,
  background: variant === 'primary' ? C.primary : '#fff',
  color: variant === 'primary' ? '#fff' : C.text,
  cursor: 'pointer', fontSize: 12, fontWeight: 600,
})
const pageBtn = (disabled) => ({
  display: 'inline-flex', alignItems: 'center', padding: '4px 8px',
  border: `1px solid ${C.cardBorder}`, background: disabled ? C.grayBg : '#fff',
  color: disabled ? C.textMuted : C.text,
  borderRadius: 4, cursor: disabled ? 'not-allowed' : 'pointer',
})
