/**
 * TrendsPage — Upload, Review & Admin for Trend_* tables
 * Compact inline-styled layout matching the ARS design system.
 */
import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { trendsAPI } from '@/services/api'
import { AgGridReact } from 'ag-grid-react'
import 'ag-grid-community/styles/ag-grid.css'
import 'ag-grid-community/styles/ag-theme-alpine.css'
import toast from 'react-hot-toast'
import {
  TrendingUp, Upload, Eye, Settings, Plus, Trash2, Download, RefreshCw,
  FileSpreadsheet, ChevronDown, ChevronRight, AlertTriangle, CheckCircle2,
  X, Database, Columns, Search, Filter, Loader2, Edit3, UploadCloud
} from 'lucide-react'
import { C } from '@/theme/colors'

/* ── tiny icon button ──────────────────────────────────────────────────────── */
const IBtn = ({icon:Icon,title,onClick,color,bg,bd,disabled,size=24,iconSize=11}) => (
  <button onClick={onClick} title={title} disabled={disabled} style={{
    display:'inline-flex',alignItems:'center',justifyContent:'center',
    width:size,height:size,borderRadius:5,border:`1px solid ${bd}`,
    background:bg,color,cursor:disabled?'not-allowed':'pointer',padding:0,
    opacity:disabled?.5:1,
  }}><Icon size={iconSize}/></button>
)

/* ── shared styles ─────────────────────────────────────────────────────────── */
const sInput = {
  height:28, fontSize:11, padding:'0 8px', borderRadius:5,
  border:`1px solid ${C.inputBorder}`, outline:'none', color:C.text,
  background:'#fff',
}
const sSelect = { ...sInput, paddingRight:20, cursor:'pointer' }
const sBtn = (bg,color,bd) => ({
  display:'inline-flex', alignItems:'center', gap:4, height:28,
  padding:'0 10px', fontSize:11, fontWeight:600, borderRadius:5,
  border:`1px solid ${bd||bg}`, background:bg, color, cursor:'pointer',
  whiteSpace:'nowrap',
})
const sCard = {
  background:C.cardBg, border:`1px solid ${C.cardBorder}`,
  borderRadius:8, overflow:'hidden',
}
const sSection = { padding:'10px 14px' }
const sLabel = { fontSize:10, fontWeight:600, color:C.textSub, marginBottom:3, display:'block' }

/* ── helpers ───────────────────────────────────────────────────────────────── */
function fmtSize(bytes) {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB'
  return (bytes/1048576).toFixed(1) + ' MB'
}
function todayStr() {
  const d = new Date()
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0')
}

/* ── Confirmation modal ────────────────────────────────────────────────────── */
function ConfirmModal({ open, title, message, onConfirm, onCancel, danger }) {
  if (!open) return null
  return (
    <div style={{
      position:'fixed',inset:0,zIndex:9999,display:'flex',alignItems:'center',justifyContent:'center',
      background:'rgba(0,0,0,.35)',
    }}>
      <div style={{ ...sCard, width:380, padding:20 }}>
        <div style={{ fontSize:13, fontWeight:700, color:danger?C.red:C.text, marginBottom:8, display:'flex', alignItems:'center', gap:6 }}>
          {danger && <AlertTriangle size={14}/>} {title}
        </div>
        <div style={{ fontSize:11, color:C.textSub, marginBottom:16, lineHeight:1.5 }}>{message}</div>
        <div style={{ display:'flex', justifyContent:'flex-end', gap:6 }}>
          <button onClick={onCancel} style={sBtn('#fff',C.textSub,C.inputBorder)}>Cancel</button>
          <button onClick={onConfirm} style={sBtn(danger?C.red:C.primary,'#fff')}>
            {danger ? 'Yes, proceed' : 'Confirm'}
          </button>
        </div>
      </div>
    </div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════════
   TAB 1 — UPLOAD
   ══════════════════════════════════════════════════════════════════════════════ */
function UploadTab() {
  const [tables, setTables] = useState([])
  const [selectedTable, setSelectedTable] = useState('')
  const [file, setFile] = useState(null)
  const [reportDate, setReportDate] = useState(todayStr())
  const [preview, setPreview] = useState(null)
  const [conflicts, setConflicts] = useState(null)
  const [conflictMode, setConflictMode] = useState('append')
  const [uploading, setUploading] = useState(false)
  const [checking, setChecking] = useState(false)
  const [result, setResult] = useState(null)
  const [dragOver, setDragOver] = useState(false)
  const fileRef = useRef()

  useEffect(() => {
    trendsAPI.listTables().then(r => setTables(r.data?.data || r.data || []))
      .catch(() => toast.error('Failed to load trend tables'))
  }, [])

  const handleFile = useCallback((f) => {
    if (!f) return
    const ext = f.name.split('.').pop().toLowerCase()
    if (!['xlsx','xls'].includes(ext)) { toast.error('Only .xlsx / .xls files'); return }
    setFile(f)
    setPreview(null); setConflicts(null); setResult(null)
    // auto preview
    const fd = new FormData()
    fd.append('file', f)
    trendsAPI.uploadPreview(fd).then(r => setPreview(r.data?.data || r.data))
      .catch(() => toast.error('Preview failed'))
  }, [])

  const onDrop = (e) => { e.preventDefault(); setDragOver(false); handleFile(e.dataTransfer.files[0]) }
  const onDragOver = (e) => { e.preventDefault(); setDragOver(true) }
  const onDragLeave = () => setDragOver(false)

  const checkConflicts = async () => {
    if (!selectedTable || !file) { toast.error('Select table & file'); return }
    setChecking(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      fd.append('table_name', selectedTable)
      fd.append('report_date', reportDate)
      const r = await trendsAPI.checkConflicts(fd)
      const d = r.data?.data || r.data
      setConflicts(d)
      if (!d?.has_conflicts) toast.success('No conflicts — ready to upload')
    } catch { toast.error('Conflict check failed') }
    finally { setChecking(false) }
  }

  const doUpload = async () => {
    if (!selectedTable || !file) { toast.error('Select table & file'); return }
    setUploading(true); setResult(null)
    try {
      const fd = new FormData()
      fd.append('file', file)
      fd.append('table_name', selectedTable)
      fd.append('report_date', reportDate)
      fd.append('conflict_mode', conflictMode)
      const r = await trendsAPI.upload(fd)
      setResult(r.data?.data || r.data)
      toast.success('Upload complete')
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Upload failed')
    } finally { setUploading(false) }
  }

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:12 }}>
      {/* Table + date row */}
      <div style={{ ...sCard }}>
        <div style={{ ...sSection, display:'flex', gap:12, flexWrap:'wrap', alignItems:'flex-end' }}>
          <div style={{ flex:'1 1 220px' }}>
            <label style={sLabel}>Target Table</label>
            <select value={selectedTable} onChange={e=>setSelectedTable(e.target.value)}
              style={{ ...sSelect, width:'100%' }}>
              <option value="">-- select --</option>
              {tables.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <div style={{ flex:'0 0 150px' }}>
            <label style={sLabel}>Report Date</label>
            <input type="date" value={reportDate} onChange={e=>setReportDate(e.target.value)}
              style={{ ...sInput, width:'100%' }}/>
          </div>
          <button onClick={()=>{ trendsAPI.listTables().then(r=>setTables(r.data?.data||r.data||[])) }}
            title="Refresh tables" style={sBtn(C.primaryLight,C.primary,C.primaryBd)}>
            <RefreshCw size={11}/> Refresh
          </button>
        </div>
      </div>

      {/* Drag & drop */}
      <div style={{ ...sCard }}>
        <div style={sSection}>
          <label style={sLabel}>Excel File</label>
          <div
            onDrop={onDrop} onDragOver={onDragOver} onDragLeave={onDragLeave}
            onClick={()=>fileRef.current?.click()}
            style={{
              border:`2px dashed ${dragOver?C.primary:C.inputBorder}`,
              borderRadius:6, padding:'18px 12px', textAlign:'center',
              cursor:'pointer', background:dragOver?C.primaryLight:'#fafbfc',
              transition:'all .15s',
            }}>
            <UploadCloud size={22} style={{ color:C.primary, margin:'0 auto 6px' }}/>
            <div style={{ fontSize:11, fontWeight:600, color:C.text }}>
              {file ? file.name : 'Drop .xlsx/.xls here or click to browse'}
            </div>
            {file && <div style={{ fontSize:10, color:C.textMuted, marginTop:2 }}>{fmtSize(file.size)}</div>}
            <input ref={fileRef} type="file" accept=".xlsx,.xls" hidden
              onChange={e=>handleFile(e.target.files[0])}/>
          </div>
        </div>
      </div>

      {/* Preview */}
      {preview && (
        <div style={{ ...sCard }}>
          <div style={{ ...sSection, background:C.headerBg, borderBottom:`1px solid ${C.cardBorder}`,
            display:'flex', alignItems:'center', gap:6, fontSize:12, fontWeight:600, color:C.text }}>
            <Eye size={13}/> Preview
          </div>
          <div style={sSection}>
            <div style={{ display:'flex', gap:16, fontSize:10, color:C.textSub, marginBottom:8 }}>
              <span><strong>Rows:</strong> {preview.row_count?.toLocaleString() ?? '—'}</span>
              <span><strong>Columns:</strong> {preview.columns?.length ?? '—'}</span>
            </div>
            {preview.columns && (
              <div style={{ overflowX:'auto' }}>
                <table style={{ width:'100%', borderCollapse:'collapse', fontSize:10 }}>
                  <thead>
                    <tr style={{ background:C.headerBg }}>
                      <th style={{ padding:'4px 8px', textAlign:'left', borderBottom:`1px solid ${C.cardBorder}`, color:C.textSub, fontWeight:600 }}>Column</th>
                      <th style={{ padding:'4px 8px', textAlign:'left', borderBottom:`1px solid ${C.cardBorder}`, color:C.textSub, fontWeight:600 }}>Inferred Type</th>
                      <th style={{ padding:'4px 8px', textAlign:'left', borderBottom:`1px solid ${C.cardBorder}`, color:C.textSub, fontWeight:600 }}>Sample</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.columns.map((col,i) => (
                      <tr key={i} style={{ background:i%2?C.rowAlt:'#fff' }}>
                        <td style={{ padding:'3px 8px', fontWeight:500, color:C.text }}>{col.name || col}</td>
                        <td style={{ padding:'3px 8px', color:C.textMuted, fontFamily:'monospace' }}>{col.dtype || col.type || '—'}</td>
                        <td style={{ padding:'3px 8px', color:C.textMuted }}>{col.sample ?? '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Conflict check + upload */}
      {file && selectedTable && (
        <div style={{ ...sCard }}>
          <div style={sSection}>
            <div style={{ display:'flex', gap:8, alignItems:'center', flexWrap:'wrap' }}>
              <button onClick={checkConflicts} disabled={checking}
                style={sBtn(C.amberBg,C.amber,C.amberBd)}>
                {checking ? <Loader2 size={11} className="animate-spin"/> : <Search size={11}/>}
                Check Conflicts
              </button>

              {conflicts?.has_conflicts && (
                <div style={{
                  flex:1, padding:'6px 10px', borderRadius:5,
                  background:C.amberBg, border:`1px solid ${C.amberBd}`,
                  fontSize:10, color:C.amber, display:'flex', alignItems:'center', gap:6,
                }}>
                  <AlertTriangle size={12}/>
                  <span><strong>{conflicts.conflict_count ?? 'Some'}</strong> conflicting rows found. Choose mode:</span>
                </div>
              )}
              {conflicts && !conflicts.has_conflicts && (
                <div style={{
                  padding:'6px 10px', borderRadius:5,
                  background:C.greenBg, border:`1px solid ${C.greenBd}`,
                  fontSize:10, color:C.green, display:'flex', alignItems:'center', gap:6,
                }}>
                  <CheckCircle2 size={12}/> No conflicts
                </div>
              )}
            </div>

            {/* Conflict mode selector */}
            <div style={{ display:'flex', gap:6, marginTop:10, flexWrap:'wrap' }}>
              {[
                { key:'append', label:'Append new rows', icon:Plus },
                { key:'upsert', label:'Update/Insert (Upsert)', icon:RefreshCw },
                { key:'replace', label:'Delete existing & re-upload', icon:Trash2 },
              ].map(m => (
                <button key={m.key} onClick={()=>setConflictMode(m.key)} style={{
                  ...sBtn(conflictMode===m.key?C.primaryLight:'#fff',
                    conflictMode===m.key?C.primary:C.textSub,
                    conflictMode===m.key?C.primaryBd:C.inputBorder),
                  fontWeight: conflictMode===m.key?700:500,
                }}>
                  <m.icon size={10}/> {m.label}
                </button>
              ))}
            </div>

            {/* Upload button */}
            <div style={{ marginTop:12, display:'flex', gap:8, alignItems:'center' }}>
              <button onClick={doUpload} disabled={uploading}
                style={{ ...sBtn(C.primary,'#fff'), opacity:uploading?.6:1, height:32, fontSize:12 }}>
                {uploading ? <Loader2 size={12} className="animate-spin"/> : <Upload size={12}/>}
                {uploading ? 'Uploading...' : 'Upload'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Result */}
      {result && (
        <div style={{
          ...sCard, padding:'10px 14px',
          background:C.greenBg, border:`1px solid ${C.greenBd}`,
        }}>
          <div style={{ fontSize:12, fontWeight:700, color:C.green, marginBottom:4, display:'flex', alignItems:'center', gap:6 }}>
            <CheckCircle2 size={14}/> Upload Complete
          </div>
          <div style={{ fontSize:10, color:C.textSub, lineHeight:1.6 }}>
            {result.rows_inserted != null && <div>Rows inserted: <strong>{result.rows_inserted}</strong></div>}
            {result.rows_updated != null && <div>Rows updated: <strong>{result.rows_updated}</strong></div>}
            {result.rows_deleted != null && <div>Rows deleted: <strong>{result.rows_deleted}</strong></div>}
            {result.message && <div>{result.message}</div>}
          </div>
        </div>
      )}
    </div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════════
   TAB 2 — REVIEW
   ══════════════════════════════════════════════════════════════════════════════ */
function ReviewTab() {
  const [tables, setTables] = useState([])
  const [selectedTable, setSelectedTable] = useState('')
  const [schema, setSchema] = useState(null)
  const [filters, setFilters] = useState({})       // { colName: [val1, val2] }
  const [filterOpen, setFilterOpen] = useState({})  // which col filter dropdown is open
  const [distinctVals, setDistinctVals] = useState({})
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [rowLimit, setRowLimit] = useState(1000)
  const [rowData, setRowData] = useState([])
  const [loading, setLoading] = useState(false)
  const [fetched, setFetched] = useState(false)
  const gridRef = useRef()

  useEffect(() => {
    trendsAPI.listTables().then(r => setTables(r.data?.data || r.data || []))
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (!selectedTable) { setSchema(null); setFilters({}); setRowData([]); setFetched(false); return }
    trendsAPI.getSchema(selectedTable).then(r => {
      setSchema(r.data?.data || r.data)
      setFilters({}); setFilterOpen({}); setDistinctVals({}); setRowData([]); setFetched(false)
    }).catch(() => toast.error('Failed to load schema'))
  }, [selectedTable])

  const loadDistinct = async (col) => {
    if (distinctVals[col]) return
    try {
      const r = await trendsAPI.getDistinct(selectedTable, col)
      setDistinctVals(prev => ({ ...prev, [col]: r.data?.data || r.data || [] }))
    } catch { toast.error(`Failed to load values for ${col}`) }
  }

  const toggleFilter = async (col) => {
    const isOpen = filterOpen[col]
    setFilterOpen(prev => ({ ...prev, [col]: !isOpen }))
    if (!isOpen) await loadDistinct(col)
  }

  const toggleFilterValue = (col, val) => {
    setFilters(prev => {
      const curr = prev[col] || []
      const next = curr.includes(val) ? curr.filter(v => v !== val) : [...curr, val]
      if (next.length === 0) { const { [col]: _, ...rest } = prev; return rest }
      return { ...prev, [col]: next }
    })
  }

  const removeFilter = (col) => {
    setFilters(prev => { const { [col]: _, ...rest } = prev; return rest })
    setFilterOpen(prev => ({ ...prev, [col]: false }))
  }

  const fetchData = async () => {
    if (!selectedTable) { toast.error('Select a table'); return }
    setLoading(true)
    try {
      const payload = {
        table_name: selectedTable,
        filters,
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        limit: rowLimit,
      }
      const r = await trendsAPI.review(payload)
      const data = r.data?.data || r.data || []
      setRowData(Array.isArray(data) ? data : data.rows || [])
      setFetched(true)
      toast.success(`Loaded ${(Array.isArray(data)?data:data.rows||[]).length} rows`)
    } catch { toast.error('Fetch failed') }
    finally { setLoading(false) }
  }

  const downloadCSV = async () => {
    if (!selectedTable) return
    try {
      const r = await trendsAPI.downloadReview(selectedTable, {
        filters: JSON.stringify(filters),
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        limit: rowLimit,
      })
      const blob = new Blob([r.data], { type:'text/csv' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob); a.download = `${selectedTable}_review.csv`; a.click()
      toast.success('CSV downloaded')
    } catch { toast.error('Download failed') }
  }

  const columns = useMemo(() => schema?.columns || schema || [], [schema])
  const colNames = useMemo(() =>
    columns.map(c => typeof c === 'string' ? c : c.column_name || c.name)
  , [columns])

  const columnDefs = useMemo(() =>
    colNames.map(col => ({
      field: col,
      headerName: col,
      sortable: true,
      filter: true,
      resizable: true,
      minWidth: 90,
    }))
  , [colNames])

  const defaultColDef = useMemo(() => ({
    flex: 1, minWidth: 90,
    filter: 'agTextColumnFilter',
    floatingFilter: true,
  }), [])

  const activeFilterCols = Object.keys(filters)

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:10 }}>
      {/* Controls */}
      <div style={{ ...sCard }}>
        <div style={{ ...sSection, display:'flex', gap:10, flexWrap:'wrap', alignItems:'flex-end' }}>
          <div style={{ flex:'1 1 200px' }}>
            <label style={sLabel}>Table</label>
            <select value={selectedTable} onChange={e=>setSelectedTable(e.target.value)}
              style={{ ...sSelect, width:'100%' }}>
              <option value="">-- select --</option>
              {tables.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <div style={{ flex:'0 0 130px' }}>
            <label style={sLabel}>Date From</label>
            <input type="date" value={dateFrom} onChange={e=>setDateFrom(e.target.value)}
              style={{ ...sInput, width:'100%' }}/>
          </div>
          <div style={{ flex:'0 0 130px' }}>
            <label style={sLabel}>Date To</label>
            <input type="date" value={dateTo} onChange={e=>setDateTo(e.target.value)}
              style={{ ...sInput, width:'100%' }}/>
          </div>
          <div style={{ flex:'0 0 90px' }}>
            <label style={sLabel}>Limit</label>
            <select value={rowLimit} onChange={e=>setRowLimit(Number(e.target.value))}
              style={{ ...sSelect, width:'100%' }}>
              {[100,500,1000,2000,5000].map(n => <option key={n} value={n}>{n}</option>)}
            </select>
          </div>
          <button onClick={fetchData} disabled={loading||!selectedTable}
            style={{ ...sBtn(C.primary,'#fff'), height:28, opacity:(loading||!selectedTable)?.5:1 }}>
            {loading ? <Loader2 size={11} className="animate-spin"/> : <Search size={11}/>}
            Fetch
          </button>
          {fetched && (
            <button onClick={downloadCSV} style={sBtn(C.greenBg,C.green,C.greenBd)}>
              <Download size={11}/> CSV
            </button>
          )}
        </div>
      </div>

      {/* Column filters */}
      {colNames.length > 0 && (
        <div style={{ ...sCard }}>
          <div style={{ ...sSection }}>
            <div style={{ display:'flex', alignItems:'center', gap:6, marginBottom:6 }}>
              <Filter size={11} style={{ color:C.textMuted }}/>
              <span style={{ fontSize:10, fontWeight:600, color:C.textSub }}>Column Filters</span>
              {activeFilterCols.length > 0 && (
                <span style={{
                  fontSize:9, padding:'1px 6px', borderRadius:10,
                  background:C.primaryLight, color:C.primary, fontWeight:700,
                }}>{activeFilterCols.length} active</span>
              )}
            </div>
            <div style={{ display:'flex', gap:4, flexWrap:'wrap' }}>
              {colNames.map(col => {
                const active = !!filters[col]
                const isOpen = filterOpen[col]
                return (
                  <div key={col} style={{ position:'relative' }}>
                    <button onClick={()=>toggleFilter(col)} style={{
                      ...sBtn(active?C.primaryLight:'#fff', active?C.primary:C.textMuted,
                        active?C.primaryBd:C.inputBorder),
                      fontSize:9, height:22, padding:'0 6px',
                    }}>
                      {col} {active && <span style={{ fontSize:8 }}>({filters[col].length})</span>}
                      <ChevronDown size={8}/>
                    </button>
                    {active && (
                      <span onClick={()=>removeFilter(col)} style={{
                        position:'absolute', top:-4, right:-4, width:12, height:12,
                        borderRadius:6, background:C.red, color:'#fff', fontSize:8,
                        display:'flex', alignItems:'center', justifyContent:'center', cursor:'pointer',
                      }}><X size={7}/></span>
                    )}
                    {isOpen && (
                      <div style={{
                        position:'absolute', top:'100%', left:0, zIndex:999, marginTop:2,
                        background:'#fff', border:`1px solid ${C.cardBorder}`, borderRadius:6,
                        boxShadow:'0 4px 12px rgba(0,0,0,.12)', maxHeight:200, width:180,
                        overflowY:'auto',
                      }}>
                        {!distinctVals[col] ? (
                          <div style={{ padding:8, fontSize:10, color:C.textMuted, textAlign:'center' }}>
                            <Loader2 size={12} className="animate-spin" style={{ margin:'0 auto' }}/>
                          </div>
                        ) : distinctVals[col].length === 0 ? (
                          <div style={{ padding:8, fontSize:10, color:C.textMuted, textAlign:'center' }}>No values</div>
                        ) : distinctVals[col].map((val,i) => {
                          const sel = (filters[col]||[]).includes(val)
                          return (
                            <div key={i} onClick={()=>toggleFilterValue(col,val)} style={{
                              padding:'3px 8px', fontSize:10, cursor:'pointer',
                              background:sel?C.primaryLight:'transparent', color:sel?C.primary:C.text,
                              display:'flex', alignItems:'center', gap:5,
                            }}>
                              <span style={{
                                width:12, height:12, borderRadius:3, flexShrink:0,
                                border:`1px solid ${sel?C.primary:C.inputBorder}`,
                                background:sel?C.primary:'#fff', display:'flex',
                                alignItems:'center', justifyContent:'center',
                              }}>
                                {sel && <CheckCircle2 size={8} style={{ color:'#fff' }}/>}
                              </span>
                              <span style={{ overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
                                {val == null ? '(null)' : String(val)}
                              </span>
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      )}

      {/* Grid */}
      {fetched && (
        <div style={{ ...sCard }}>
          <div style={{
            ...sSection, background:C.headerBg, borderBottom:`1px solid ${C.cardBorder}`,
            display:'flex', alignItems:'center', justifyContent:'space-between',
          }}>
            <span style={{ fontSize:11, fontWeight:600, color:C.text }}>
              Showing {rowData.length.toLocaleString()} rows
            </span>
          </div>
          <div className="ag-theme-alpine" style={{ width:'100%', height:Math.min(600, Math.max(250, rowData.length*28+60)) }}>
            <AgGridReact
              ref={gridRef}
              rowData={rowData}
              columnDefs={columnDefs}
              defaultColDef={defaultColDef}
              animateRows
              pagination
              paginationPageSize={100}
              paginationPageSizeSelector={[50,100,500]}
              suppressRowClickSelection
              loading={loading}
            />
          </div>
        </div>
      )}
    </div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════════
   TAB 3 — ADMIN
   ══════════════════════════════════════════════════════════════════════════════ */
const SQL_TYPES = [
  'SMALLINT','INT','BIGINT','FLOAT','BIT','DECIMAL(18,2)',
  'DATE','DATETIME2',
  'NVARCHAR(50)','NVARCHAR(100)','NVARCHAR(255)','NVARCHAR(4000)','NVARCHAR(MAX)',
]

function AdminTab() {
  const [tables, setTables] = useState([])
  const [createOpen, setCreateOpen] = useState(true)
  const [maintOpen, setMaintOpen] = useState(true)

  const refreshTables = () => {
    trendsAPI.listTables().then(r => setTables(r.data?.data || r.data || []))
      .catch(() => {})
  }
  useEffect(refreshTables, [])

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:12 }}>
      <CreateTableSection tables={tables} open={createOpen} toggle={()=>setCreateOpen(p=>!p)} onCreated={refreshTables}/>
      <MaintenanceSection tables={tables} open={maintOpen} toggle={()=>setMaintOpen(p=>!p)} onChanged={refreshTables}/>
    </div>
  )
}

/* ── Create new table ──────────────────────────────────────────────────────── */
function CreateTableSection({ open, toggle, onCreated }) {
  const [tableSuffix, setTableSuffix] = useState('')
  const [file, setFile] = useState(null)
  const [columns, setColumns] = useState([]) // [{name, dtype, pk}]
  const [creating, setCreating] = useState(false)
  const fileRef = useRef()

  const onFile = async (f) => {
    if (!f) return
    setFile(f)
    const fd = new FormData(); fd.append('file', f)
    try {
      const r = await trendsAPI.uploadPreview(fd)
      const data = r.data?.data || r.data
      const cols = (data.columns || []).map(c => ({
        name: c.name || c,
        dtype: c.dtype || c.type || 'NVARCHAR(255)',
        pk: false,
      }))
      setColumns(cols)
    } catch { toast.error('Preview failed') }
  }

  const setColType = (i, dtype) => {
    setColumns(prev => prev.map((c,j) => j===i ? { ...c, dtype } : c))
  }
  const setColPk = (i, pk) => {
    setColumns(prev => prev.map((c,j) => j===i ? { ...c, pk } : c))
  }

  const doCreate = async (withData) => {
    if (!tableSuffix.trim()) { toast.error('Enter table name suffix'); return }
    if (columns.length === 0) { toast.error('Upload a file to define columns'); return }
    setCreating(true)
    try {
      const fd = new FormData()
      fd.append('table_name', 'Trend_' + tableSuffix.trim())
      fd.append('columns', JSON.stringify(columns.map(c => ({ name:c.name, type:c.dtype, primary_key:c.pk }))))
      if (withData && file) fd.append('file', file)
      fd.append('with_data', withData ? 'true' : 'false')
      await trendsAPI.createTable(fd)
      toast.success(`Table Trend_${tableSuffix.trim()} created${withData ? ' with data' : ''}`)
      setTableSuffix(''); setFile(null); setColumns([])
      onCreated()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Create failed')
    } finally { setCreating(false) }
  }

  return (
    <div style={{ ...sCard }}>
      <div onClick={toggle} style={{
        ...sSection, background:C.headerBg, borderBottom:`1px solid ${C.cardBorder}`,
        display:'flex', alignItems:'center', gap:6, cursor:'pointer',
      }}>
        {open ? <ChevronDown size={12}/> : <ChevronRight size={12}/>}
        <Plus size={12} style={{ color:C.primary }}/>
        <span style={{ fontSize:12, fontWeight:700, color:C.text }}>Create New Table</span>
      </div>
      {open && (
        <div style={sSection}>
          {/* Name */}
          <div style={{ display:'flex', alignItems:'center', gap:6, marginBottom:10 }}>
            <span style={{ fontSize:11, color:C.textMuted, fontFamily:'monospace' }}>Trend_</span>
            <input value={tableSuffix} onChange={e=>setTableSuffix(e.target.value)}
              placeholder="table_suffix" style={{ ...sInput, flex:1 }}/>
          </div>

          {/* File for schema */}
          <div style={{ marginBottom:10 }}>
            <label style={sLabel}>Upload file to infer schema</label>
            <button onClick={()=>fileRef.current?.click()} style={sBtn(C.primaryLight,C.primary,C.primaryBd)}>
              <FileSpreadsheet size={11}/> {file ? file.name : 'Choose file'}
            </button>
            <input ref={fileRef} type="file" accept=".xlsx,.xls" hidden
              onChange={e=>onFile(e.target.files[0])}/>
          </div>

          {/* Columns editor */}
          {columns.length > 0 && (
            <div style={{ overflowX:'auto', marginBottom:10 }}>
              <table style={{ width:'100%', borderCollapse:'collapse', fontSize:10 }}>
                <thead>
                  <tr style={{ background:C.headerBg }}>
                    <th style={{ padding:'4px 6px', textAlign:'left', borderBottom:`1px solid ${C.cardBorder}`, color:C.textSub, fontWeight:600, width:30 }}>PK</th>
                    <th style={{ padding:'4px 6px', textAlign:'left', borderBottom:`1px solid ${C.cardBorder}`, color:C.textSub, fontWeight:600 }}>Column</th>
                    <th style={{ padding:'4px 6px', textAlign:'left', borderBottom:`1px solid ${C.cardBorder}`, color:C.textSub, fontWeight:600, width:180 }}>Type</th>
                  </tr>
                </thead>
                <tbody>
                  {columns.map((col,i) => (
                    <tr key={i} style={{ background:i%2?C.rowAlt:'#fff' }}>
                      <td style={{ padding:'3px 6px', textAlign:'center' }}>
                        <input type="checkbox" checked={col.pk} onChange={e=>setColPk(i,e.target.checked)}
                          style={{ width:13, height:13, cursor:'pointer' }}/>
                      </td>
                      <td style={{ padding:'3px 6px', fontWeight:500, color:C.text, fontFamily:'monospace' }}>{col.name}</td>
                      <td style={{ padding:'3px 6px' }}>
                        <select value={col.dtype} onChange={e=>setColType(i,e.target.value)}
                          style={{ ...sSelect, width:'100%', height:24, fontSize:10 }}>
                          {SQL_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                        </select>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Action buttons */}
          {columns.length > 0 && (
            <div style={{ display:'flex', gap:8 }}>
              <button onClick={()=>doCreate(false)} disabled={creating}
                style={{ ...sBtn(C.primary,'#fff'), opacity:creating?.5:1 }}>
                {creating ? <Loader2 size={11} className="animate-spin"/> : <Database size={11}/>}
                Create Table Only
              </button>
              {file && (
                <button onClick={()=>doCreate(true)} disabled={creating}
                  style={{ ...sBtn(C.green,'#fff'), opacity:creating?.5:1 }}>
                  {creating ? <Loader2 size={11} className="animate-spin"/> : <Upload size={11}/>}
                  Create & Upload Data
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/* ── Table maintenance ─────────────────────────────────────────────────────── */
function MaintenanceSection({ tables, open, toggle, onChanged }) {
  const [selectedTable, setSelectedTable] = useState('')
  const [schema, setSchema] = useState(null)
  const [confirm, setConfirm] = useState(null) // { action, title, message }

  // Column operations
  const [addColName, setAddColName] = useState('')
  const [addColType, setAddColType] = useState('NVARCHAR(255)')
  const [dropCol, setDropCol] = useState('')
  const [alterCol, setAlterCol] = useState('')
  const [alterType, setAlterType] = useState('NVARCHAR(255)')
  const [renameCol, setRenameCol] = useState('')
  const [renameNew, setRenameNew] = useState('')
  const [opLoading, setOpLoading] = useState(false)

  useEffect(() => {
    if (!selectedTable) { setSchema(null); return }
    trendsAPI.getSchema(selectedTable).then(r => setSchema(r.data?.data || r.data))
      .catch(() => {})
  }, [selectedTable])

  const colNames = useMemo(() => {
    if (!schema) return []
    const cols = schema.columns || schema || []
    return cols.map(c => typeof c === 'string' ? c : c.column_name || c.name)
  }, [schema])

  const doTruncate = () => {
    setConfirm({
      action: async () => {
        try {
          await trendsAPI.truncateTable(selectedTable)
          toast.success(`${selectedTable} truncated`)
        } catch { toast.error('Truncate failed') }
      },
      title: 'Truncate Table',
      message: `This will delete ALL rows in "${selectedTable}". This cannot be undone.`,
      danger: true,
    })
  }

  const doDrop = () => {
    setConfirm({
      action: async () => {
        try {
          await trendsAPI.dropTable(selectedTable)
          toast.success(`${selectedTable} dropped`)
          setSelectedTable(''); onChanged()
        } catch { toast.error('Drop failed') }
      },
      title: 'Drop Table',
      message: `This will permanently DELETE the table "${selectedTable}" and all its data. This cannot be undone.`,
      danger: true,
    })
  }

  const handleConfirm = async () => {
    if (confirm?.action) await confirm.action()
    setConfirm(null)
  }

  const doColumnOp = async (op, data) => {
    if (!selectedTable) return
    setOpLoading(true)
    try {
      await trendsAPI.alterColumns(selectedTable, { operation: op, ...data })
      toast.success(`Column operation "${op}" completed`)
      // refresh schema
      const r = await trendsAPI.getSchema(selectedTable)
      setSchema(r.data?.data || r.data)
      setAddColName(''); setDropCol(''); setAlterCol(''); setRenameCol(''); setRenameNew('')
    } catch (e) {
      toast.error(e.response?.data?.detail || `Column op failed`)
    } finally { setOpLoading(false) }
  }

  return (
    <>
      <ConfirmModal
        open={!!confirm}
        title={confirm?.title || ''}
        message={confirm?.message || ''}
        danger={confirm?.danger}
        onConfirm={handleConfirm}
        onCancel={()=>setConfirm(null)}
      />
      <div style={{ ...sCard }}>
        <div onClick={toggle} style={{
          ...sSection, background:C.headerBg, borderBottom:`1px solid ${C.cardBorder}`,
          display:'flex', alignItems:'center', gap:6, cursor:'pointer',
        }}>
          {open ? <ChevronDown size={12}/> : <ChevronRight size={12}/>}
          <Settings size={12} style={{ color:C.amber }}/>
          <span style={{ fontSize:12, fontWeight:700, color:C.text }}>Table Maintenance</span>
        </div>
        {open && (
          <div style={sSection}>
            {/* Table selector */}
            <div style={{ marginBottom:12 }}>
              <label style={sLabel}>Select Table</label>
              <select value={selectedTable} onChange={e=>setSelectedTable(e.target.value)}
                style={{ ...sSelect, width:'100%', maxWidth:300 }}>
                <option value="">-- select --</option>
                {tables.map(t => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>

            {selectedTable && (
              <>
                {/* Danger zone */}
                <div style={{
                  padding:10, borderRadius:6, marginBottom:14,
                  background:C.redBg, border:`1px solid ${C.redBd}`,
                }}>
                  <div style={{ fontSize:10, fontWeight:700, color:C.red, marginBottom:8, display:'flex', alignItems:'center', gap:4 }}>
                    <AlertTriangle size={11}/> Danger Zone
                  </div>
                  <div style={{ display:'flex', gap:6 }}>
                    <button onClick={doTruncate} style={sBtn('#fff',C.red,C.redBd)}>
                      <Trash2 size={10}/> Truncate Table
                    </button>
                    <button onClick={doDrop} style={sBtn(C.red,'#fff')}>
                      <Trash2 size={10}/> Drop Table
                    </button>
                  </div>
                </div>

                {/* Column operations */}
                <div style={{ fontSize:11, fontWeight:700, color:C.text, marginBottom:8, display:'flex', alignItems:'center', gap:4 }}>
                  <Columns size={12}/> Column Operations
                </div>

                {/* Add column */}
                <div style={{
                  padding:8, borderRadius:5, border:`1px solid ${C.cardBorder}`,
                  marginBottom:8, display:'flex', gap:6, alignItems:'flex-end', flexWrap:'wrap',
                }}>
                  <div>
                    <label style={sLabel}>Add Column</label>
                    <input value={addColName} onChange={e=>setAddColName(e.target.value)}
                      placeholder="column_name" style={{ ...sInput, width:140 }}/>
                  </div>
                  <div>
                    <label style={sLabel}>Type</label>
                    <select value={addColType} onChange={e=>setAddColType(e.target.value)}
                      style={{ ...sSelect, width:150 }}>
                      {SQL_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                    </select>
                  </div>
                  <button onClick={()=>doColumnOp('add',{column_name:addColName,column_type:addColType})}
                    disabled={!addColName.trim()||opLoading}
                    style={{ ...sBtn(C.primary,'#fff'), opacity:(!addColName.trim()||opLoading)?.5:1 }}>
                    <Plus size={10}/> Add
                  </button>
                </div>

                {/* Drop column */}
                <div style={{
                  padding:8, borderRadius:5, border:`1px solid ${C.cardBorder}`,
                  marginBottom:8, display:'flex', gap:6, alignItems:'flex-end', flexWrap:'wrap',
                }}>
                  <div>
                    <label style={sLabel}>Drop Column</label>
                    <select value={dropCol} onChange={e=>setDropCol(e.target.value)}
                      style={{ ...sSelect, width:200 }}>
                      <option value="">-- select --</option>
                      {colNames.map(c => <option key={c} value={c}>{c}</option>)}
                    </select>
                  </div>
                  <button onClick={()=>doColumnOp('drop',{column_name:dropCol})}
                    disabled={!dropCol||opLoading}
                    style={{ ...sBtn(C.red,'#fff'), opacity:(!dropCol||opLoading)?.5:1 }}>
                    <Trash2 size={10}/> Drop
                  </button>
                </div>

                {/* Alter column type */}
                <div style={{
                  padding:8, borderRadius:5, border:`1px solid ${C.cardBorder}`,
                  marginBottom:8, display:'flex', gap:6, alignItems:'flex-end', flexWrap:'wrap',
                }}>
                  <div>
                    <label style={sLabel}>Alter Column Type</label>
                    <select value={alterCol} onChange={e=>setAlterCol(e.target.value)}
                      style={{ ...sSelect, width:160 }}>
                      <option value="">-- select column --</option>
                      {colNames.map(c => <option key={c} value={c}>{c}</option>)}
                    </select>
                  </div>
                  <div>
                    <label style={sLabel}>New Type</label>
                    <select value={alterType} onChange={e=>setAlterType(e.target.value)}
                      style={{ ...sSelect, width:150 }}>
                      {SQL_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                    </select>
                  </div>
                  <button onClick={()=>doColumnOp('alter',{column_name:alterCol,column_type:alterType})}
                    disabled={!alterCol||opLoading}
                    style={{ ...sBtn(C.amber,'#fff'), opacity:(!alterCol||opLoading)?.5:1 }}>
                    <Edit3 size={10}/> Alter
                  </button>
                </div>

                {/* Rename column */}
                <div style={{
                  padding:8, borderRadius:5, border:`1px solid ${C.cardBorder}`,
                  display:'flex', gap:6, alignItems:'flex-end', flexWrap:'wrap',
                }}>
                  <div>
                    <label style={sLabel}>Rename Column</label>
                    <select value={renameCol} onChange={e=>setRenameCol(e.target.value)}
                      style={{ ...sSelect, width:160 }}>
                      <option value="">-- select column --</option>
                      {colNames.map(c => <option key={c} value={c}>{c}</option>)}
                    </select>
                  </div>
                  <div>
                    <label style={sLabel}>New Name</label>
                    <input value={renameNew} onChange={e=>setRenameNew(e.target.value)}
                      placeholder="new_column_name" style={{ ...sInput, width:160 }}/>
                  </div>
                  <button onClick={()=>doColumnOp('rename',{column_name:renameCol,new_name:renameNew})}
                    disabled={!renameCol||!renameNew.trim()||opLoading}
                    style={{ ...sBtn(C.primary,'#fff'), opacity:(!renameCol||!renameNew.trim()||opLoading)?.5:1 }}>
                    <Edit3 size={10}/> Rename
                  </button>
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </>
  )
}

/* ══════════════════════════════════════════════════════════════════════════════
   MAIN PAGE
   ══════════════════════════════════════════════════════════════════════════════ */
const TABS = [
  { key:'upload', label:'Upload', icon:Upload },
  { key:'review', label:'Review', icon:Eye },
  { key:'admin',  label:'Admin',  icon:Settings },
]

export default function TrendsPage() {
  const [tab, setTab] = useState('upload')

  return (
    <div style={{ padding:'0 4px' }}>
      {/* Header */}
      <div style={{ display:'flex', alignItems:'center', gap:8, marginBottom:10 }}>
        <TrendingUp size={18} style={{ color:C.primary }}/>
        <span style={{ fontSize:15, fontWeight:700, color:C.text }}>Trends</span>
      </div>

      {/* Tab bar */}
      <div style={{
        display:'flex', gap:0, marginBottom:12,
        borderBottom:`2px solid ${C.cardBorder}`,
      }}>
        {TABS.map(t => {
          const active = tab === t.key
          return (
            <button key={t.key} onClick={()=>setTab(t.key)} style={{
              display:'inline-flex', alignItems:'center', gap:5,
              padding:'6px 14px', fontSize:11, fontWeight:active?700:500,
              color:active?C.primary:C.textSub, background:'transparent',
              border:'none', borderBottom:`2px solid ${active?C.primary:'transparent'}`,
              marginBottom:-2, cursor:'pointer', transition:'all .15s',
            }}>
              <t.icon size={13}/> {t.label}
            </button>
          )
        })}
      </div>

      {/* Tab content */}
      {tab === 'upload' && <UploadTab/>}
      {tab === 'review' && <ReviewTab/>}
      {tab === 'admin'  && <AdminTab/>}
    </div>
  )
}
