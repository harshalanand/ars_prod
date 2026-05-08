/**
 * LookupArtMasterPage
 * Upload Excel → pick join key → select VW_MASTER_PRODUCT columns → LEFT JOIN → preview / download.
 */
import { useState, useEffect, useCallback } from 'react'
import { lookupArtMasterAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  Upload, Search, Play, Download, X, CheckCircle2,
  FileSpreadsheet, Columns, Link2, RefreshCw, AlertCircle,
  ArrowRight, Zap, Table2
} from 'lucide-react'
import { C } from '@/theme/colors'

const Card = ({ children, style }) => (
  <div style={{ background:C.cardBg, border:`1px solid ${C.cardBorder}`, borderRadius:12,
    boxShadow:'0 1px 3px rgba(0,0,0,.06)', overflow:'hidden', ...style }}>{children}</div>
)

const StepHeader = ({ icon: Icon, step, title, subtitle, right }) => (
  <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', padding:'14px 18px',
    background:C.headerBg, borderBottom:`1px solid ${C.cardBorder}` }}>
    <div style={{ display:'flex', alignItems:'center', gap:10 }}>
      <div style={{ width:28, height:28, borderRadius:8, display:'flex', alignItems:'center', justifyContent:'center',
        background:C.primary, color:'#fff', fontSize:12, fontWeight:800 }}>{step}</div>
      <div>
        <div style={{ fontSize:13, fontWeight:700, color:C.text, display:'flex', alignItems:'center', gap:6 }}>
          <Icon size={14} color={C.primary}/> {title}
        </div>
        {subtitle && <div style={{ fontSize:11, color:C.textMuted, marginTop:1 }}>{subtitle}</div>}
      </div>
    </div>
    {right}
  </div>
)

export default function LookupArtMasterPage() {
  const [file, setFile]                   = useState(null)
  const [uploadCols, setUploadCols]       = useState([])
  const [uploadRows, setUploadRows]       = useState(0)
  const [masterCols, setMasterCols]       = useState([])
  const [joinColumn, setJoinColumn]       = useState('')
  const [masterColumn, setMasterColumn]   = useState('')
  const [selectedCols, setSelectedCols]   = useState([])
  const [searchMaster, setSearchMaster]   = useState('')
  const [result, setResult]               = useState(null)
  const [loading, setLoading]             = useState(false)
  const [downloading, setDownloading]     = useState(false)
  const [dragOver, setDragOver]           = useState(false)
  const [elapsed, setElapsed]             = useState(0)

  useEffect(() => {
    lookupArtMasterAPI.getColumns()
      .then(r => setMasterCols(r.data?.data?.columns || []))
      .catch(() => toast.error('Failed to load master columns'))
  }, [])

  // Timer while loading
  useEffect(() => {
    if (!loading) { setElapsed(0); return }
    const t0 = Date.now()
    const iv = setInterval(() => setElapsed(((Date.now() - t0) / 1000).toFixed(1)), 100)
    return () => clearInterval(iv)
  }, [loading])

  const handleFile = useCallback(async (f) => {
    if (!f) return
    const ext = f.name.split('.').pop().toLowerCase()
    if (!['csv','xlsx','xls'].includes(ext)) { toast.error('Upload CSV or Excel file'); return }
    setFile(f); setResult(null); setJoinColumn(''); setSelectedCols([])
    const fd = new FormData(); fd.append('file', f)
    try {
      const { data } = await lookupArtMasterAPI.preview(fd)
      setUploadCols(data.data.columns || [])
      setUploadRows(data.data.row_count || 0)
      toast.success(`${data.data.row_count} rows, ${data.data.columns.length} columns loaded`)
    } catch { toast.error('Failed to read file') }
  }, [])

  const onDrop = useCallback(e => { e.preventDefault(); setDragOver(false); handleFile(e.dataTransfer.files[0]) }, [handleFile])
  const clearFile = () => { setFile(null); setUploadCols([]); setUploadRows(0); setJoinColumn(''); setSelectedCols([]); setResult(null) }
  const toggleCol = col => setSelectedCols(p => p.includes(col) ? p.filter(c => c !== col) : [...p, col])

  const selectAllFiltered = () => {
    const f = masterCols.filter(c => c.toLowerCase().includes(searchMaster.toLowerCase()))
    setSelectedCols(p => { const s = new Set(p); f.forEach(c => s.add(c)); return [...s] })
  }

  const handleRun = async () => {
    if (!file || !joinColumn || !masterColumn || !selectedCols.length) {
      toast.error('Complete all steps first'); return
    }
    setLoading(true); setResult(null)
    try {
      const fd = new FormData()
      fd.append('file', file); fd.append('join_column', joinColumn)
      fd.append('master_column', masterColumn); fd.append('select_columns', JSON.stringify(selectedCols))
      const { data } = await lookupArtMasterAPI.run(fd)
      setResult(data.data); toast.success(data.message)
    } catch (e) { toast.error(e.response?.data?.detail || 'Lookup failed') }
    finally { setLoading(false) }
  }

  const handleDownload = async () => {
    if (!file || !joinColumn || !masterColumn || !selectedCols.length) return
    setDownloading(true)
    try {
      const fd = new FormData()
      fd.append('file', file); fd.append('join_column', joinColumn)
      fd.append('master_column', masterColumn); fd.append('select_columns', JSON.stringify(selectedCols))
      const res = await lookupArtMasterAPI.download(fd)
      const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([res.data]))
      a.download = 'lookup_result.xlsx'; a.click(); toast.success('Download started')
    } catch { toast.error('Download failed') }
    finally { setDownloading(false) }
  }

  const filteredMaster = masterCols.filter(c => c.toLowerCase().includes(searchMaster.toLowerCase()))
  const canRun = file && joinColumn && masterColumn && selectedCols.length > 0

  return (
    <div style={{ color:C.text }}>
      {/* Header */}
      <div style={{ marginBottom:20 }}>
        <h1 style={{ fontSize:20, fontWeight:800, margin:0, display:'flex', alignItems:'center', gap:10 }}>
          <div style={{ width:32, height:32, borderRadius:10, display:'flex', alignItems:'center', justifyContent:'center',
            background:'linear-gradient(135deg,#4f46e5,#7c3aed)', color:'#fff' }}>
            <Zap size={18}/>
          </div>
          Lookup Art Master
        </h1>
        <p style={{ fontSize:13, color:C.textSub, margin:'6px 0 0', display:'flex', alignItems:'center', gap:6, flexWrap:'wrap' }}>
          Upload Excel
          <ArrowRight size={12} color={C.textMuted}/>
          Select join key
          <ArrowRight size={12} color={C.textMuted}/>
          Pick columns from <code style={{ background:'#f1f5f9', color:C.primary, padding:'1px 6px', borderRadius:4, fontSize:11, fontWeight:700, border:`1px solid ${C.primaryBd}` }}>VW_MASTER_PRODUCT</code>
          <ArrowRight size={12} color={C.textMuted}/>
          Preview &amp; Download
        </p>
      </div>

      <div style={{ display:'flex', flexDirection:'column', gap:16 }}>

        {/* ── STEP 1: Upload ── */}
        <Card>
          <StepHeader icon={FileSpreadsheet} step="1" title="Upload File" subtitle="Excel (.xlsx, .xls) or CSV"/>
          <div style={{ padding:18 }}>
            {!file ? (
              <div onDragOver={e => { e.preventDefault(); setDragOver(true) }} onDragLeave={() => setDragOver(false)}
                onDrop={onDrop} onClick={() => document.getElementById('lkp-file').click()}
                style={{ border:`2px dashed ${dragOver ? C.primary : '#d1d5db'}`, borderRadius:12, padding:'40px 20px',
                  textAlign:'center', cursor:'pointer', background:dragOver ? C.primaryLight : '#fafbfc', transition:'all .15s' }}>
                <div style={{ width:48, height:48, borderRadius:12, background:C.primaryLight, display:'flex',
                  alignItems:'center', justifyContent:'center', margin:'0 auto 12px' }}>
                  <Upload size={22} color={C.primary}/>
                </div>
                <div style={{ fontSize:14, fontWeight:700, color:C.text }}>Drop file here or click to browse</div>
                <div style={{ fontSize:12, color:C.textMuted, marginTop:4 }}>Supports .xlsx, .xls, .csv</div>
                <input id="lkp-file" type="file" accept=".csv,.xlsx,.xls" hidden onChange={e => handleFile(e.target.files[0])}/>
              </div>
            ) : (
              <div style={{ display:'flex', alignItems:'center', gap:14, padding:'12px 16px',
                background:C.greenBg, border:`1px solid ${C.greenBd}`, borderRadius:10 }}>
                <div style={{ width:40, height:40, borderRadius:10, background:'#d1fae5', display:'flex',
                  alignItems:'center', justifyContent:'center', flexShrink:0 }}>
                  <CheckCircle2 size={20} color={C.green}/>
                </div>
                <div style={{ flex:1 }}>
                  <div style={{ fontSize:14, fontWeight:700, color:C.text }}>{file.name}</div>
                  <div style={{ fontSize:12, color:C.textSub }}>{uploadRows.toLocaleString()} rows &middot; {uploadCols.length} columns</div>
                </div>
                <button onClick={clearFile} style={{ background:'none', border:'none', cursor:'pointer', padding:6, borderRadius:6 }}>
                  <X size={16} color={C.textMuted}/>
                </button>
              </div>
            )}
          </div>
        </Card>

        {/* ── STEP 2: Join Key ── */}
        {file && uploadCols.length > 0 && (
          <Card>
            <StepHeader icon={Link2} step="2" title="Select Join Key" subtitle="Map your file column to VW_MASTER_PRODUCT"/>
            <div style={{ padding:18 }}>
              <div style={{ display:'flex', gap:16, alignItems:'end', flexWrap:'wrap' }}>
                <div style={{ flex:1, minWidth:200 }}>
                  <label style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase', letterSpacing:'.06em', display:'block', marginBottom:5 }}>
                    Your File Column
                  </label>
                  <select value={joinColumn} onChange={e => setJoinColumn(e.target.value)}
                    style={{ width:'100%', padding:'9px 12px', borderRadius:8, border:`1px solid ${C.inputBorder}`,
                      background:C.inputBg, color:C.text, fontSize:13, fontWeight:600 }}>
                    <option value="">-- select --</option>
                    {uploadCols.map(c => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
                <div style={{ padding:'0 0 10px', display:'flex', alignItems:'center', gap:8 }}>
                  <div style={{ width:32, height:32, borderRadius:'50%', background:C.primaryLight, display:'flex',
                    alignItems:'center', justifyContent:'center' }}>
                    <Link2 size={14} color={C.primary}/>
                  </div>
                </div>
                <div style={{ flex:1, minWidth:200 }}>
                  <label style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase', letterSpacing:'.06em', display:'block', marginBottom:5 }}>
                    VW_MASTER_PRODUCT Column
                  </label>
                  <select value={masterColumn} onChange={e => setMasterColumn(e.target.value)}
                    style={{ width:'100%', padding:'9px 12px', borderRadius:8, border:`1px solid ${C.inputBorder}`,
                      background:C.inputBg, color:C.text, fontSize:13, fontWeight:600 }}>
                    <option value="">-- select --</option>
                    {masterCols.map(c => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
              </div>
            </div>
          </Card>
        )}

        {/* ── STEP 3: Pick Columns ── */}
        {joinColumn && masterColumn && (
          <Card>
            <StepHeader icon={Columns} step="3" title="Select Columns from VW_MASTER_PRODUCT"
              subtitle={`${selectedCols.length} selected`}
              right={
                <div style={{ display:'flex', gap:6 }}>
                  <button onClick={selectAllFiltered} style={{ padding:'5px 12px', borderRadius:7, fontSize:11, fontWeight:700,
                    border:`1px solid ${C.primaryBd}`, background:C.primaryLight, color:C.primary, cursor:'pointer' }}>Select All</button>
                  <button onClick={() => setSelectedCols([])} style={{ padding:'5px 12px', borderRadius:7, fontSize:11, fontWeight:700,
                    border:`1px solid ${C.cardBorder}`, background:'#fff', color:C.textSub, cursor:'pointer' }}>Clear</button>
                </div>
              }
            />
            <div style={{ padding:18 }}>
              <div style={{ position:'relative', marginBottom:12 }}>
                <Search size={13} style={{ position:'absolute', left:10, top:'50%', transform:'translateY(-50%)', color:C.textMuted }}/>
                <input type="text" value={searchMaster} placeholder="Search columns…"
                  onChange={e => setSearchMaster(e.target.value)}
                  style={{ width:'100%', padding:'8px 12px 8px 32px', borderRadius:8, border:`1px solid ${C.inputBorder}`,
                    background:C.inputBg, color:C.text, fontSize:13, outline:'none', boxSizing:'border-box' }}/>
              </div>
              <div style={{ display:'flex', flexWrap:'wrap', gap:6, maxHeight:180, overflowY:'auto', padding:'2px 0' }}>
                {filteredMaster.map(col => {
                  const sel = selectedCols.includes(col)
                  return (
                    <button key={col} onClick={() => toggleCol(col)} style={{
                      padding:'6px 14px', borderRadius:20, fontSize:12, fontWeight:600, cursor:'pointer',
                      border:`1.5px solid ${sel ? C.primary : '#e2e8f0'}`,
                      background:sel ? C.primary : '#fff', color:sel ? '#fff' : C.textSub,
                      transition:'all .12s', boxShadow:sel ? '0 2px 8px rgba(79,70,229,.25)' : 'none',
                    }}>
                      {sel && <span style={{ marginRight:4 }}>&#10003;</span>}{col}
                    </button>
                  )
                })}
              </div>
            </div>
          </Card>
        )}

        {/* ── ACTION BAR ── */}
        {canRun && (
          <Card>
            <div style={{ padding:'14px 18px', display:'flex', gap:12, alignItems:'center', flexWrap:'wrap' }}>
              <button onClick={handleRun} disabled={loading} style={{
                display:'flex', alignItems:'center', gap:8, padding:'10px 24px', borderRadius:10,
                fontSize:14, fontWeight:800, border:'none', cursor:loading?'wait':'pointer',
                background:'linear-gradient(135deg,#4f46e5,#7c3aed)', color:'#fff',
                opacity:loading?0.7:1, boxShadow:'0 4px 14px rgba(79,70,229,.35)', transition:'all .15s',
              }}>
                {loading ? <RefreshCw size={15} style={{ animation:'spin 1s linear infinite' }}/> : <Play size={15}/>}
                {loading ? `Running… ${elapsed}s` : 'Run Lookup'}
              </button>

              {result && (
                <button onClick={handleDownload} disabled={downloading} style={{
                  display:'flex', alignItems:'center', gap:8, padding:'10px 24px', borderRadius:10,
                  fontSize:14, fontWeight:800, cursor:'pointer',
                  border:`2px solid ${C.greenBd}`, background:C.greenBg, color:C.green,
                  opacity:downloading?0.6:1, transition:'all .15s',
                }}>
                  <Download size={15}/> {downloading ? 'Downloading…' : 'Download Excel'}
                </button>
              )}

              {result && (
                <div style={{ display:'flex', gap:16, marginLeft:'auto' }}>
                  <div style={{ textAlign:'center' }}>
                    <div style={{ fontSize:20, fontWeight:800, color:C.primary }}>{result.total_rows?.toLocaleString()}</div>
                    <div style={{ fontSize:10, fontWeight:600, color:C.textMuted, textTransform:'uppercase' }}>Total</div>
                  </div>
                  <div style={{ textAlign:'center' }}>
                    <div style={{ fontSize:20, fontWeight:800, color:C.green }}>{result.matched_rows?.toLocaleString()}</div>
                    <div style={{ fontSize:10, fontWeight:600, color:C.textMuted, textTransform:'uppercase' }}>Matched</div>
                  </div>
                  <div style={{ textAlign:'center' }}>
                    <div style={{ fontSize:20, fontWeight:800, color:C.red }}>{(result.total_rows - result.matched_rows)?.toLocaleString()}</div>
                    <div style={{ fontSize:10, fontWeight:600, color:C.textMuted, textTransform:'uppercase' }}>Unmatched</div>
                  </div>
                </div>
              )}
            </div>
          </Card>
        )}

        {/* ── RESULT TABLE ── */}
        {result && result.preview?.length > 0 && (
          <Card>
            <div style={{ padding:'12px 18px', borderBottom:`1px solid ${C.cardBorder}`, background:C.headerBg,
              display:'flex', alignItems:'center', gap:8 }}>
              <Table2 size={14} color={C.primary}/>
              <span style={{ fontSize:13, fontWeight:700, color:C.text }}>Result Preview</span>
              <span style={{ fontSize:11, color:C.textMuted }}>
                (showing {Math.min(500, result.total_rows).toLocaleString()} of {result.total_rows.toLocaleString()})
              </span>
            </div>
            <div style={{ overflowX:'auto', maxHeight:'60vh' }}>
              <table style={{ width:'100%', borderCollapse:'collapse', fontSize:12 }}>
                <thead style={{ position:'sticky', top:0, zIndex:1 }}>
                  <tr style={{ background:'#f1f5f9' }}>
                    <th style={{ padding:'8px 14px', textAlign:'center', fontSize:10, fontWeight:700, color:C.textMuted,
                      borderBottom:`2px solid ${C.cardBorder}`, width:50 }}>#</th>
                    {result.columns.map(col => (
                      <th key={col} style={{
                        padding:'8px 14px', textAlign:'left', fontSize:10, fontWeight:700,
                        color: selectedCols.includes(col) ? C.primary : C.textSub,
                        textTransform:'uppercase', letterSpacing:'.04em', whiteSpace:'nowrap',
                        borderBottom:`2px solid ${C.cardBorder}`,
                        background: selectedCols.includes(col) ? C.primaryLight : '#f1f5f9',
                      }}>{col}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {result.preview.map((row, i) => (
                    <tr key={i} style={{ borderBottom:`1px solid ${C.cardBorder}`, background:i%2===0?'#fff':'#fafbfc' }}>
                      <td style={{ padding:'5px 14px', textAlign:'center', fontSize:11, color:C.textMuted, fontWeight:600 }}>{i+1}</td>
                      {result.columns.map(col => (
                        <td key={col} style={{ padding:'5px 14px', color:C.text, whiteSpace:'nowrap',
                          maxWidth:220, overflow:'hidden', textOverflow:'ellipsis',
                          fontFamily: col === joinColumn ? 'Consolas,monospace' : 'inherit',
                          fontWeight: col === joinColumn ? 700 : 400,
                        }}>
                          {row[col] != null ? String(row[col]) : <span style={{ color:C.textMuted, fontStyle:'italic' }}>null</span>}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {result.total_rows > 500 && (
              <div style={{ padding:'10px 18px', fontSize:12, color:C.amber, fontWeight:600,
                background:C.amberBg, borderTop:`1px solid ${C.amberBd}`, display:'flex', alignItems:'center', gap:6 }}>
                <AlertCircle size={13}/> Showing first 500 rows. Download Excel for full {result.total_rows.toLocaleString()} rows.
              </div>
            )}
          </Card>
        )}
      </div>

      <style>{`@keyframes spin{to{transform:rotate(360deg);}}`}</style>
    </div>
  )
}
