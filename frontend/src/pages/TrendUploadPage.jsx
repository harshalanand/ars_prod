/**
 * TrendUploadPage — Upload Excel to Trend_* tables
 */
import { useState, useEffect, useCallback, useRef } from 'react'
import { trendsAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  TrendingUp, Upload, Plus, Trash2, RefreshCw,
  Eye, AlertTriangle, CheckCircle2,
  Search, Loader2, UploadCloud, Download
} from 'lucide-react'
import { C } from '@/theme/colors'
const sInput = { height:28, fontSize:11, padding:'0 8px', borderRadius:5, border:`1px solid ${C.inputBorder}`, outline:'none', color:C.text, background:'#fff' }
const sSelect = { ...sInput, paddingRight:20, cursor:'pointer' }
const sBtn = (bg,color,bd) => ({ display:'inline-flex', alignItems:'center', gap:4, height:28, padding:'0 10px', fontSize:11, fontWeight:600, borderRadius:5, border:`1px solid ${bd||bg}`, background:bg, color, cursor:'pointer', whiteSpace:'nowrap' })
const sCard = { background:C.cardBg, border:`1px solid ${C.cardBorder}`, borderRadius:8, overflow:'hidden' }
const sSection = { padding:'10px 14px' }
const sLabel = { fontSize:10, fontWeight:600, color:C.textSub, marginBottom:3, display:'block' }

function fmtSize(bytes) {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB'
  return (bytes/1048576).toFixed(1) + ' MB'
}
function todayStr() {
  const d = new Date()
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0')
}

export default function TrendUploadPage() {
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
    trendsAPI.listTables().then(r => { const d = r.data?.data; setTables(d?.tables || (Array.isArray(d) ? d : [])) })
      .catch(() => toast.error('Failed to load trend tables'))
  }, [])

  const handleFile = useCallback((f) => {
    if (!f) return
    const ext = f.name.split('.').pop().toLowerCase()
    if (!['xlsx','xls'].includes(ext)) { toast.error('Only .xlsx / .xls files'); return }
    setFile(f)
    setPreview(null); setConflicts(null); setResult(null)
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
    <div style={{ padding:'0 4px' }}>
      <div style={{ display:'flex', alignItems:'center', gap:8, marginBottom:10 }}>
        <TrendingUp size={15} style={{ color:C.primary }}/>
        <span style={{ fontSize:14, fontWeight:700, color:C.text }}>Trend Upload</span>
      </div>

      <div style={{ display:'flex', flexDirection:'column', gap:12 }}>
        {/* Table + date row */}
        <div style={{ ...sCard }}>
          <div style={{ ...sSection, display:'flex', gap:12, flexWrap:'wrap', alignItems:'flex-end' }}>
            <div style={{ flex:'1 1 220px' }}>
              <label style={sLabel}>Target Table</label>
              <select value={selectedTable} onChange={e=>setSelectedTable(e.target.value)}
                style={{ ...sSelect, width:'100%' }}>
                <option value="">-- select --</option>
                {tables.map(t => { const name = t.table_name || t; return <option key={name} value={name}>{name}</option> })}
              </select>
            </div>
            <div style={{ flex:'0 0 150px' }}>
              <label style={sLabel}>Report Date</label>
              <input type="date" value={reportDate} onChange={e=>setReportDate(e.target.value)}
                style={{ ...sInput, width:'100%' }}/>
            </div>
            <button onClick={()=>{ trendsAPI.listTables().then(r=>{ const d=r.data?.data; setTables(d?.tables||(Array.isArray(d)?d:[])) }) }}
              title="Refresh tables" style={sBtn(C.primaryLight,C.primary,C.primaryBd)}>
              <RefreshCw size={11}/> Refresh
            </button>
            <button onClick={() => {
              const csv = 'Store_Code,MAJ_CAT,Article_Number,Trend_Qty,Report_Date\nS001,FOOTWEAR,1000000001,120,2026-04-27\nS002,APPAREL,1000000002,85,2026-04-27\n'
              const a = document.createElement('a')
              a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }))
              a.download = 'Trend_Upload_Sample.csv'
              a.click()
            }} title="Download sample template" style={sBtn('#fff',C.textSub,C.inputBorder)}>
              <Download size={11}/> Sample Template
            </button>
          </div>
        </div>

        {/* Drag & drop */}
        <div style={{ ...sCard }}>
          <div style={sSection}>
            <label style={sLabel}>Excel File</label>
            <div onDrop={onDrop} onDragOver={onDragOver} onDragLeave={onDragLeave}
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
                  <div style={{ flex:1, padding:'6px 10px', borderRadius:5, background:C.amberBg, border:`1px solid ${C.amberBd}`, fontSize:10, color:C.amber, display:'flex', alignItems:'center', gap:6 }}>
                    <AlertTriangle size={12}/>
                    <span><strong>{conflicts.conflict_count ?? 'Some'}</strong> conflicting rows found. Choose mode:</span>
                  </div>
                )}
                {conflicts && !conflicts.has_conflicts && (
                  <div style={{ padding:'6px 10px', borderRadius:5, background:C.greenBg, border:`1px solid ${C.greenBd}`, fontSize:10, color:C.green, display:'flex', alignItems:'center', gap:6 }}>
                    <CheckCircle2 size={12}/> No conflicts
                  </div>
                )}
              </div>

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

              <div style={{ marginTop:12, display:'flex', gap:8, alignItems:'center' }}>
                <button onClick={doUpload} disabled={uploading}
                  style={{ ...sBtn(C.primary,'#fff'), opacity:uploading?0.6:1, height:32, fontSize:12 }}>
                  {uploading ? <Loader2 size={12} className="animate-spin"/> : <Upload size={12}/>}
                  {uploading ? 'Uploading...' : 'Upload'}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Result */}
        {result && (
          <div style={{ ...sCard, padding:'10px 14px', background:C.greenBg, border:`1px solid ${C.greenBd}` }}>
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
    </div>
  )
}
