/**
 * TrendReviewPage — Review & filter data from Trend_* tables
 */
import { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import { trendsAPI } from '@/services/api'
import { AgGridReact } from 'ag-grid-react'
import 'ag-grid-community/styles/ag-grid.css'
import 'ag-grid-community/styles/ag-theme-alpine.css'
import toast from 'react-hot-toast'
import {
  TrendingUp, Download, RefreshCw, ChevronDown, CheckCircle2,
  X, Search, Loader2, Database, Calendar, Plus,
  XCircle, CheckSquare, ClipboardPaste, Filter
} from 'lucide-react'

const HIDDEN_SYS = new Set(['VERSION','UPLOAD_DATETIME','SYSTEM_IP','SYSTEM_NAME','SYSTEM_LOGIN_ID'])

/* ── Multi-Select Dropdown for filter values ──────────────────────────── */
function MultiSelect({ col, tableName, selected, onChange }) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [values, setValues] = useState(null)
  const [loading, setLoading] = useState(false)
  const [hlIdx, setHlIdx] = useState(0)
  const [paste, setPaste] = useState(false)
  const [pText, setPText] = useState('')
  const ref = useRef(), btnRef = useRef(), listRef = useRef()
  const [pos, setPos] = useState({})

  const load = useCallback(async () => {
    if (values || loading) return
    setLoading(true)
    try {
      const r = await trendsAPI.getDistinct(tableName, col)
      const d = r.data?.data
      setValues(d?.values || (Array.isArray(d) ? d : []))
    } catch { setValues([]) }
    finally { setLoading(false) }
  }, [col, tableName, values, loading])

  useEffect(() => {
    if (!open) return
    load()
    if (btnRef.current) {
      const r = btnRef.current.getBoundingClientRect()
      const below = window.innerHeight - r.bottom
      setPos(below < 240 && r.top > 240
        ? { bottom: window.innerHeight - r.top + 2, left: r.left }
        : { top: r.bottom + 2, left: r.left })
    }
  }, [open, load])

  useEffect(() => {
    if (!open) return
    const h = e => { if (ref.current && !ref.current.contains(e.target) && !btnRef.current?.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [open])

  const filtered = useMemo(() => {
    if (!values) return []
    if (!search) return values
    const q = search.toLowerCase()
    return values.filter(v => String(v ?? '').toLowerCase().includes(q))
  }, [values, search])

  useEffect(() => { setHlIdx(0) }, [search])
  useEffect(() => { listRef.current?.children[hlIdx]?.scrollIntoView({ block: 'nearest' }) }, [hlIdx])

  const toggle = v => {
    const next = selected.includes(v) ? selected.filter(x => x !== v) : [...selected, v]
    onChange(next)
  }
  const onKey = e => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setHlIdx(i => Math.min(i + 1, filtered.length - 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setHlIdx(i => Math.max(i - 1, 0)) }
    else if (e.key === 'Enter' && filtered[hlIdx] != null) { e.preventDefault(); toggle(filtered[hlIdx]) }
    else if (e.key === 'Escape') setOpen(false)
  }
  const allSel = filtered.length > 0 && filtered.every(v => selected.includes(v))
  const togAll = () => { const next = allSel ? selected.filter(v => !filtered.includes(v)) : [...new Set([...selected, ...filtered])]; onChange(next) }

  const applyPaste = () => {
    const vals = [...new Set(pText.split(/[\n,;|\t]+/).map(x => x.trim()).filter(Boolean))]
    if (!vals.length) return
    onChange(vals)
    setPaste(false); setPText('')
    setOpen(false)
    toast.success(`${vals.length} values applied`)
  }

  return (
    <div style={{ position:'relative', display:'inline-flex' }}>
      <button ref={btnRef} onClick={() => setOpen(!open)}
        style={{ height:20, padding:'0 6px', fontSize:8, borderRadius:3, border:'1px solid #e2e8f0',
          background: selected.length ? '#eef2ff' : '#fff', color: selected.length ? '#4338ca' : '#64748b',
          cursor:'pointer', display:'inline-flex', alignItems:'center', gap:3, maxWidth:160, fontWeight: selected.length ? 600 : 400 }}>
        {selected.length ? `${selected.length} selected` : 'Select values...'}
        <ChevronDown size={7}/>
      </button>
      {open && (
        <div ref={ref} style={{ position:'fixed', zIndex:9999, width:220, ...pos,
          background:'#fff', borderRadius:5, boxShadow:'0 8px 24px rgba(0,0,0,.18)', border:'1px solid #e2e8f0' }}>
          <div style={{ padding:'3px 6px', background:'#f8fafc', borderBottom:'1px solid #e2e8f0',
            display:'flex', alignItems:'center', justifyContent:'space-between' }}>
            <span style={{ fontSize:8, fontWeight:700, color:'#64748b', textTransform:'uppercase' }}>{col}</span>
            <div style={{ display:'flex', gap:4 }}>
              <button onClick={() => setPaste(!paste)} style={{ fontSize:7, color: paste ? '#4f46e5' : '#94a3b8', cursor:'pointer', background:'none', border:'none', display:'flex', alignItems:'center', gap:1 }}>
                <ClipboardPaste size={8}/> Paste
              </button>
              <button onClick={() => { onChange([]); setOpen(false) }} style={{ fontSize:7, color:'#ef4444', cursor:'pointer', background:'none', border:'none', fontWeight:600 }}>Clear</button>
            </div>
          </div>
          {paste ? (
            <div style={{ padding:6 }}>
              <div style={{ fontSize:7, color:'#64748b', marginBottom:3 }}>Paste from Excel — one per line, comma, tab separated</div>
              <textarea value={pText} onChange={e => setPText(e.target.value)} autoFocus
                placeholder={"HA10\nHA20\nHA30"}
                style={{ width:'100%', height:90, fontSize:9, border:'1px solid #cbd5e1', borderRadius:3, padding:4, outline:'none', resize:'vertical', fontFamily:'monospace', lineHeight:1.4 }} />
              <div style={{ display:'flex', gap:3, marginTop:4 }}>
                <button onClick={applyPaste} style={{ flex:1, height:20, fontSize:9, fontWeight:700, background:'#4f46e5', color:'#fff', border:'none', borderRadius:3, cursor:'pointer' }}>Apply ({pText.split(/[\n,;|\t]+/).filter(x=>x.trim()).length})</button>
                <button onClick={() => { setPaste(false); setPText('') }} style={{ height:20, fontSize:8, padding:'0 8px', background:'#f1f5f9', color:'#64748b', border:'1px solid #e2e8f0', borderRadius:3, cursor:'pointer' }}>Cancel</button>
              </div>
            </div>
          ) : (
            <>
              <div style={{ padding:'3px 5px', borderBottom:'1px solid #f1f5f9' }}>
                <input type="text" value={search} onChange={e => setSearch(e.target.value)} onKeyDown={onKey}
                  placeholder="Search..." autoFocus
                  style={{ width:'100%', height:18, fontSize:9, padding:'0 4px', border:'1px solid #e2e8f0', borderRadius:2, outline:'none' }} />
              </div>
              {values && values.length > 0 && (
                <div onClick={togAll} style={{ padding:'2px 6px', borderBottom:'1px solid #f1f5f9', fontSize:8, color:'#4f46e5', cursor:'pointer', display:'flex', alignItems:'center', gap:3, fontWeight:600 }}>
                  {allSel ? <><XCircle size={7}/> Deselect</> : <><CheckSquare size={7}/> Select All</>}
                  <span style={{ color:'#94a3b8', fontWeight:400 }}>({filtered.length})</span>
                </div>
              )}
              <div ref={listRef} style={{ maxHeight:150, overflowY:'auto' }}>
                {loading ? (
                  <div style={{ padding:12, textAlign:'center' }}><Loader2 size={11} className="animate-spin" style={{ color:'#4f46e5', margin:'0 auto' }}/></div>
                ) : filtered.length === 0 ? (
                  <div style={{ padding:8, textAlign:'center', fontSize:8, color:'#94a3b8' }}>{search ? 'No match' : 'Empty'}</div>
                ) : filtered.map((val, i) => {
                  const sel = selected.includes(val)
                  return (
                    <div key={i} onClick={() => toggle(val)}
                      style={{ padding:'1px 6px', fontSize:9, cursor:'pointer', display:'flex', alignItems:'center', gap:4,
                        background: i === hlIdx ? (sel ? '#e0e7ff' : '#f8fafc') : (sel ? '#eef2ff' : 'transparent'),
                        color: sel ? '#4338ca' : '#334155' }}>
                      <span style={{ width:10, height:10, borderRadius:2, flexShrink:0,
                        border:`1px solid ${sel ? '#4f46e5' : '#cbd5e1'}`,
                        background: sel ? '#4f46e5' : '#fff', display:'flex', alignItems:'center', justifyContent:'center' }}>
                        {sel && <CheckCircle2 size={6} style={{ color:'#fff' }}/>}
                      </span>
                      <span style={{ overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap', lineHeight:'15px' }}>
                        {val == null ? '(null)' : String(val)}
                      </span>
                    </div>
                  )
                })}
              </div>
              {selected.length > 0 && (
                <div style={{ padding:'2px 6px', background:'#eef2ff', borderTop:'1px solid #c7d2fe', fontSize:7, fontWeight:600, color:'#4338ca' }}>{selected.length} selected</div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

/* ── Main Page ─────────────────────────────────────────────────────────── */
export default function TrendReviewPage() {
  const [tables, setTables] = useState([])
  const [sel, setSel] = useState('')
  const [schema, setSchema] = useState(null)
  const [filterRows, setFilterRows] = useState([]) // [{column, values:[]}]
  const [showFilters, setShowFilters] = useState(false)
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [limit, setLimit] = useState(1000)
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(false)
  const [fetched, setFetched] = useState(false)
  const [total, setTotal] = useState(0)
  const gridRef = useRef()

  useEffect(() => {
    trendsAPI.listTables().then(r => { const d = r.data?.data; setTables(d?.tables || (Array.isArray(d) ? d : [])) }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!sel) { setSchema(null); setFilterRows([]); setRows([]); setFetched(false); return }
    trendsAPI.getSchema(sel).then(r => {
      setSchema(r.data?.data || r.data)
      setFilterRows([]); setRows([]); setFetched(false)
    }).catch(() => toast.error('Failed to load schema'))
  }, [sel])

  const cols = useMemo(() => schema?.columns || schema || [], [schema])
  const colNames = useMemo(() => cols.map(c => typeof c === 'string' ? c : c.column_name || c.name), [cols])
  const visCols = useMemo(() => colNames.filter(c => !HIDDEN_SYS.has(c)), [colNames])

  const addFilter = () => {
    if (!visCols.length) return
    setFilterRows(p => [...p, { column: visCols[0], values: [] }])
    setShowFilters(true)
  }
  const updateFilterCol = (idx, col) => setFilterRows(p => p.map((f, i) => i === idx ? { column: col, values: [] } : f))
  const updateFilterVals = (idx, vals) => setFilterRows(p => p.map((f, i) => i === idx ? { ...f, values: vals } : f))
  const removeFilterRow = idx => setFilterRows(p => p.filter((_, i) => i !== idx))

  const buildFilters = () => {
    const obj = {}
    filterRows.forEach(f => { if (f.values.length) obj[f.column] = f.values })
    return obj
  }

  const doFetch = async () => {
    if (!sel) return
    setLoading(true)
    try {
      const payload = { table_name: sel, filters: buildFilters(), date_from: dateFrom || undefined, date_to: dateTo || undefined, limit: limit > 0 ? limit : 0 }
      const r = await trendsAPI.review(payload)
      const o = r.data?.data || r.data || {}
      const d = o?.data || o?.rows || (Array.isArray(o) ? o : [])
      setRows(d); setTotal(o?.total || d.length); setFetched(true)
      toast.success(`${d.length.toLocaleString()} rows`)
    } catch { toast.error('Fetch failed') }
    finally { setLoading(false) }
  }

  const dlCSV = async () => {
    if (!sel) return
    try {
      const r = await trendsAPI.downloadReview(sel, { filters: JSON.stringify(buildFilters()), date_from: dateFrom || undefined, date_to: dateTo || undefined, limit: limit > 0 ? limit : 0 })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(new Blob([r.data], { type: 'text/csv' }))
      a.download = `${sel}_review.csv`; a.click()
      toast.success('Downloaded')
    } catch { toast.error('Download failed') }
  }

  const numFmt = p => {
    if (p.value == null || p.value === '') return ''
    const n = Number(p.value)
    if (isNaN(n)) return p.value
    return Number.isInteger(n) ? n.toLocaleString() : n.toFixed(4)
  }

  const colDefs = useMemo(() => visCols.map(col => ({
    field: col, headerName: col, sortable: true, filter: true, resizable: true,
    minWidth: 90, valueFormatter: numFmt,
  })), [visCols])

  const defCol = useMemo(() => ({
    flex: 1, minWidth: 80, filter: 'agTextColumnFilter', floatingFilter: true,
    cellStyle: { fontSize: '10px', lineHeight: '24px' },
  }), [])

  const activeFilterCount = filterRows.filter(f => f.values.length > 0).length + (dateFrom ? 1 : 0) + (dateTo ? 1 : 0)

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:4 }}>
      {/* Header + controls */}
      <div style={{ background:'#fff', borderRadius:6, border:'1px solid #e2e8f0', padding:'5px 8px',
        display:'flex', gap:6, alignItems:'center', flexWrap:'wrap' }}>
        <div style={{ display:'flex', alignItems:'center', gap:5, marginRight:4 }}>
          <div style={{ width:22, height:22, borderRadius:5, background:'linear-gradient(135deg,#4f46e5,#7c3aed)',
            display:'flex', alignItems:'center', justifyContent:'center' }}>
            <TrendingUp size={11} style={{ color:'#fff' }}/>
          </div>
          <span style={{ fontSize:12, fontWeight:700, color:'#0f172a' }}>Trend Review</span>
        </div>

        <select value={sel} onChange={e => setSel(e.target.value)}
          style={{ height:22, fontSize:9, padding:'0 5px', borderRadius:3, border:'1px solid #e2e8f0', background:'#fff', cursor:'pointer', flex:'1 1 130px', minWidth:100 }}>
          <option value="">Table...</option>
          {tables.map(t => { const n = t.table_name || t, rc = t.row_count
            return <option key={n} value={n}>{n}{rc != null ? ` (${Number(rc).toLocaleString()})` : ''}</option> })}
        </select>

        <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)} title="Date From"
          style={{ height:22, fontSize:9, padding:'0 4px', borderRadius:3, border:'1px solid #e2e8f0', width:105 }} />
        <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)} title="Date To"
          style={{ height:22, fontSize:9, padding:'0 4px', borderRadius:3, border:'1px solid #e2e8f0', width:105 }} />

        <select value={limit} onChange={e => setLimit(Number(e.target.value))} title="Row limit"
          style={{ height:22, fontSize:9, padding:'0 4px', borderRadius:3, border:'1px solid #e2e8f0', width:68, cursor:'pointer' }}>
          {[500, 1000, 2000, 5000, 8000].map(n => <option key={n} value={n}>{n.toLocaleString()}</option>)}
          <option value={0}>No Limit</option>
        </select>

        {/* Filters toggle */}
        <button onClick={() => setShowFilters(!showFilters)}
          style={{ height:22, padding:'0 8px', borderRadius:3, fontSize:9, fontWeight:600, cursor:'pointer',
            background: showFilters ? '#f5f3ff' : '#fff', color: showFilters ? '#6d28d9' : '#64748b',
            border: `1px solid ${showFilters ? '#c4b5fd' : '#e2e8f0'}`,
            display:'inline-flex', alignItems:'center', gap:3 }}>
          <Filter size={9}/> Filters
          {activeFilterCount > 0 && (
            <span style={{ fontSize:7, fontWeight:800, color:'#fff', background:'#7c3aed', borderRadius:6, padding:'0 3px', lineHeight:'12px' }}>{activeFilterCount}</span>
          )}
        </button>

        <button onClick={doFetch} disabled={loading || !sel}
          style={{ height:22, padding:'0 10px', borderRadius:3, fontSize:9, fontWeight:700, color:'#fff',
            background: loading || !sel ? '#94a3b8' : '#4f46e5', border:'none',
            cursor: loading || !sel ? 'not-allowed' : 'pointer', display:'inline-flex', alignItems:'center', gap:3 }}>
          {loading ? <Loader2 size={9} className="animate-spin"/> : <Search size={9}/>} Fetch
        </button>
        {fetched && (
          <>
            <button onClick={dlCSV} style={{ height:22, padding:'0 8px', borderRadius:3, fontSize:9, fontWeight:600,
              color:'#059669', background:'#ecfdf5', border:'1px solid #a7f3d0', cursor:'pointer', display:'inline-flex', alignItems:'center', gap:2 }}>
              <Download size={9}/> CSV
            </button>
            <span style={{ fontSize:8, fontWeight:700, color:'#4338ca', background:'#eef2ff', padding:'1px 6px', borderRadius:8, border:'1px solid #c7d2fe' }}>
              {rows.length.toLocaleString()}{total > rows.length ? ` / ${total.toLocaleString()}` : ''} rows
            </span>
          </>
        )}
      </div>

      {/* Filters panel — like Export page: add filter rows */}
      {showFilters && (
        <div style={{ background:'#faf5ff', borderRadius:6, border:'1px solid #e9d5ff', padding:'5px 8px',
          display:'flex', flexWrap:'wrap', alignItems:'center', gap:4 }}>
          {filterRows.map((f, idx) => (
            <div key={idx} style={{ display:'flex', alignItems:'center', gap:3, background:'#fff',
              border:'1px solid #e2e8f0', borderRadius:4, padding:'2px 4px', boxShadow:'0 1px 2px rgba(0,0,0,.04)' }}>
              <select value={f.column} onChange={e => updateFilterCol(idx, e.target.value)}
                style={{ height:20, fontSize:8, border:'none', background:'transparent', outline:'none', cursor:'pointer', fontWeight:600, color:'#334155', maxWidth:100 }}>
                {visCols.map(c => <option key={c} value={c}>{c}</option>)}
              </select>
              <span style={{ fontSize:8, color:'#94a3b8' }}>IN</span>
              <MultiSelect col={f.column} tableName={sel} selected={f.values} onChange={vals => updateFilterVals(idx, vals)} />
              <button onClick={() => removeFilterRow(idx)} style={{ background:'none', border:'none', cursor:'pointer', padding:1 }}>
                <X size={10} style={{ color:'#ef4444' }}/>
              </button>
            </div>
          ))}
          <button onClick={addFilter} disabled={!schema}
            style={{ height:22, padding:'0 8px', fontSize:8, fontWeight:600, color:'#7c3aed',
              border:'1px solid #c4b5fd', borderRadius:3, background:'#fff', cursor:'pointer',
              display:'inline-flex', alignItems:'center', gap:3, opacity: schema ? 1 : .5 }}>
            <Plus size={9}/> Add Filter
          </button>
          {filterRows.length > 0 && (
            <button onClick={() => setFilterRows([])}
              style={{ fontSize:8, color:'#ef4444', cursor:'pointer', background:'none', border:'none', fontWeight:600, marginLeft:4 }}>
              Clear All
            </button>
          )}
        </div>
      )}

      {/* Grid */}
      {fetched && (
        <div style={{ background:'#fff', borderRadius:6, border:'1px solid #e2e8f0', overflow:'hidden' }}>
          <div style={{ padding:'2px 8px', background:'#f8fafc', borderBottom:'1px solid #e2e8f0',
            display:'flex', alignItems:'center', justifyContent:'space-between' }}>
            <span style={{ fontSize:8, fontWeight:700, color:'#475569' }}>
              {rows.length.toLocaleString()} rows
              {total > rows.length && <span style={{ fontWeight:400, color:'#d97706', marginLeft:3 }}>(of {total.toLocaleString()})</span>}
            </span>
            <button onClick={doFetch} style={{ height:16, padding:'0 5px', borderRadius:2, fontSize:7, color:'#64748b',
              background:'none', border:'1px solid #e2e8f0', cursor:'pointer', display:'inline-flex', alignItems:'center', gap:2 }}>
              <RefreshCw size={7}/> Refresh
            </button>
          </div>
          <style>{`
            .tr-grid .ag-header-cell-label { font-size:10px !important; font-weight:600 !important; }
            .tr-grid .ag-header-cell { padding:0 6px !important; }
            .tr-grid .ag-cell { padding:0 6px !important; overflow:hidden !important; text-overflow:ellipsis !important; white-space:nowrap !important; }
            .tr-grid .ag-floating-filter-input { font-size:9px !important; }
            .tr-grid .ag-paging-panel { font-size:9px !important; height:28px !important; }
            .tr-grid .ag-paging-page-size .ag-select { font-size:9px !important; }
            .tr-grid .ag-row { border-bottom: 1px solid #f1f5f9 !important; }
          `}</style>
          <div className="ag-theme-alpine tr-grid" style={{ width:'100%', height:'calc(100vh - 240px)', minHeight:400 }}>
            <AgGridReact
              ref={gridRef}
              rowData={rows}
              columnDefs={colDefs}
              defaultColDef={defCol}
              rowHeight={24}
              headerHeight={28}
              floatingFiltersHeight={24}
              pagination
              paginationPageSize={1000}
              paginationPageSizeSelector={[500, 1000, 2000, 5000, 8000]}
              rowSelection={{ mode: 'multiRow', enableClickSelection: false }}
              enableCellTextSelection
              ensureDomOrder
            />
          </div>
        </div>
      )}

      {!fetched && sel && !loading && (
        <div style={{ background:'#fff', borderRadius:6, border:'1px solid #e2e8f0', padding:'20px', textAlign:'center' }}>
          <Search size={16} style={{ color:'#c7d2fe', margin:'0 auto 6px' }}/>
          <div style={{ fontSize:10, fontWeight:600, color:'#475569' }}>Ready to query</div>
          <div style={{ fontSize:8, color:'#94a3b8', marginTop:1 }}>Click <b style={{ color:'#4f46e5' }}>Fetch</b> to load data</div>
        </div>
      )}
    </div>
  )
}
