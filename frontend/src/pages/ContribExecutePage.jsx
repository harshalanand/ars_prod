/**
 * ContribExecutePage – Job-based execution with background processing.
 * Jobs panel at top, config below, results from selected job.
 */
import { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import { contribAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  Cpu, Play, RefreshCw, CheckCircle2, XCircle, AlertCircle,
  Table2, Download, Search, X, ChevronDown, ChevronRight, Clock,
  Loader, StopCircle, Briefcase
} from 'lucide-react'
import { C } from '@/theme/colors'

const statusColors = {
  pending: { bg:'#f8fafc', fg:C.textMuted, icon: Clock },
  running: { bg:C.amberBg, fg:C.amber, icon: Loader },
  paused: { bg:'#dbeafe', fg:'#2563eb', icon: Clock },
  completed: { bg:C.greenBg, fg:C.green, icon: CheckCircle2 },
  failed: { bg:C.redBg, fg:C.red, icon: XCircle },
  cancelled: { bg:'#f1f5f9', fg:C.textMuted, icon: StopCircle },
}

/* ── Searchable multi-select dropdown ─────────────────────────────────────── */
function MultiSelectDropdown({ options, selected, onChange, placeholder, label }) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [hlIdx, setHlIdx] = useState(0)
  const listRef = useRef(null)
  const filtered = useMemo(() => options.filter(o => o.toLowerCase().includes(search.toLowerCase())), [options, search])

  useEffect(() => { setHlIdx(0) }, [search])

  const toggle = (v) => onChange(selected.includes(v) ? selected.filter(x=>x!==v) : [...selected, v])

  const handleKeyDown = (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setHlIdx(i => Math.min(i+1, filtered.length-1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setHlIdx(i => Math.max(i-1, 0)) }
    else if (e.key === 'Enter' && filtered[hlIdx]) { e.preventDefault(); toggle(filtered[hlIdx]) }
    else if (e.key === 'Escape') { setOpen(false) }
  }

  useEffect(() => {
    if (listRef.current?.children[hlIdx]) listRef.current.children[hlIdx].scrollIntoView({ block:'nearest' })
  }, [hlIdx])

  return (
    <div style={{ position:'relative' }}>
      {label && <div style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase', marginBottom:4 }}>{label}</div>}
      <div onClick={() => setOpen(!open)} style={{
        padding:'8px 12px', borderRadius:8, border:`1px solid ${open?C.primary:C.inputBorder}`, background:C.inputBg,
        cursor:'pointer', display:'flex', justifyContent:'space-between', alignItems:'center', minHeight:38,
      }}>
        <span style={{ fontSize:13, color: selected.length ? C.text : C.textMuted }}>
          {selected.length === 0 ? placeholder || 'All (none selected)' : `${selected.length} selected`}
        </span>
        <ChevronDown size={14} color={C.textMuted} style={{ transform:open?'rotate(180deg)':'none', transition:'.15s' }}/>
      </div>
      {open && (
        <div style={{ position:'absolute', top:'100%', left:0, right:0, zIndex:50, background:'#fff', border:`1px solid ${C.cardBorder}`, borderRadius:10, boxShadow:'0 8px 24px rgba(0,0,0,.12)', marginTop:4, maxHeight:300, display:'flex', flexDirection:'column' }}>
          <div style={{ padding:'8px 10px', borderBottom:`1px solid ${C.cardBorder}`, display:'flex', gap:6, alignItems:'center' }}>
            <Search size={13} color={C.textMuted}/>
            <input value={search} onChange={e => setSearch(e.target.value)} onKeyDown={handleKeyDown} placeholder="Search… (↑↓ Enter)" autoFocus
              style={{ flex:1, border:'none', outline:'none', fontSize:12, color:C.text, background:'transparent' }}/>
            <button onClick={() => onChange([...filtered])} style={{ fontSize:10, fontWeight:700, color:C.primary, background:'none', border:'none', cursor:'pointer', whiteSpace:'nowrap' }}>All</button>
            <button onClick={() => onChange([])} style={{ fontSize:10, fontWeight:700, color:C.red, background:'none', border:'none', cursor:'pointer' }}>Clear</button>
            <button onClick={() => setOpen(false)} style={{ background:'none', border:'none', cursor:'pointer' }}><X size={14} color={C.textMuted}/></button>
          </div>
          <div ref={listRef} style={{ overflowY:'auto', maxHeight:240 }}>
            {filtered.map((o, idx) => (
              <div key={o} onClick={() => toggle(o)} onMouseEnter={() => setHlIdx(idx)} style={{
                padding:'6px 12px', cursor:'pointer', fontSize:12, display:'flex', alignItems:'center', gap:8,
                background: idx===hlIdx ? '#e8eafc' : selected.includes(o) ? C.primaryLight : '#fff',
              }}>
                <span style={{ width:16, height:16, borderRadius:4, border:`1.5px solid ${selected.includes(o)?C.primary:'#d1d5db'}`, background:selected.includes(o)?C.primary:'#fff', display:'flex', alignItems:'center', justifyContent:'center', color:'#fff', fontSize:10, fontWeight:800, flexShrink:0 }}>
                  {selected.includes(o) ? '✓' : ''}
                </span>
                {o}
              </div>
            ))}
            {filtered.length===0 && <div style={{ padding:12, textAlign:'center', color:C.textMuted, fontSize:12 }}>No results</div>}
          </div>
        </div>
      )}
    </div>
  )
}

/* ── Result table ─────────────────────────────────────────────────────────── */
function ResultTable({ title, columns, preview, totalRows, onDownload, downloading }) {
  if (!preview?.length) return null
  return (
    <div style={{ background:C.cardBg, border:`1px solid ${C.cardBorder}`, borderRadius:12, overflow:'hidden' }}>
      <div style={{ padding:'10px 18px', background:C.headerBg, borderBottom:`1px solid ${C.cardBorder}`, display:'flex', alignItems:'center', gap:8 }}>
        <Table2 size={14} color={C.primary}/>
        <span style={{ fontSize:13, fontWeight:700 }}>{title}</span>
        <span style={{ fontSize:11, color:C.textMuted }}>({totalRows?.toLocaleString()} rows · {columns?.length} cols)</span>
        {onDownload && <button onClick={onDownload} disabled={downloading} style={{
          marginLeft:'auto', display:'flex', alignItems:'center', gap:4, padding:'4px 12px', borderRadius:6, fontSize:11, fontWeight:700,
          border:`1px solid ${downloading?'#fde68a':C.greenBd}`, background:downloading?'#fef9c3':C.greenBg,
          color:downloading?'#a16207':C.green, cursor:downloading?'wait':'pointer',
        }}>
          {downloading ? <><RefreshCw size={12} style={{animation:'spin 1s linear infinite'}}/> Downloading…</> : <><Download size={12}/> Download All</>}
        </button>}
      </div>
      <div style={{ overflowX:'auto', maxHeight:'40vh' }}>
        <table style={{ width:'100%', borderCollapse:'collapse', fontSize:11 }}>
          <thead style={{ position:'sticky', top:0, zIndex:1 }}>
            <tr style={{ background:'#f1f5f9' }}>
              <th style={{ padding:'6px 8px', fontSize:10, fontWeight:700, color:C.textMuted, borderBottom:`2px solid ${C.cardBorder}`, width:36 }}>#</th>
              {columns?.map(c => <th key={c} style={{ padding:'6px 8px', textAlign:'left', fontSize:9, fontWeight:700, color:C.textSub, whiteSpace:'nowrap', borderBottom:`2px solid ${C.cardBorder}` }}>{c}</th>)}
            </tr>
          </thead>
          <tbody>
            {preview.map((row, i) => (
              <tr key={i} style={{ borderBottom:`1px solid ${C.cardBorder}`, background:i%2===0?'#fff':'#fafbfc' }}>
                <td style={{ padding:'3px 8px', fontSize:10, color:C.textMuted, textAlign:'center' }}>{i+1}</td>
                {columns?.map(c => <td key={c} style={{ padding:'3px 8px', whiteSpace:'nowrap', maxWidth:120, overflow:'hidden', textOverflow:'ellipsis' }}>{row[c]!=null?String(row[c]):''}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════════ */
export default function ContribExecutePage() {
  const [presets, setPresets] = useState([])
  const [majcats, setMajcats] = useState([])
  const [groupingCols, setGroupingCols] = useState([])
  const [selectedPresets, setSelectedPresets] = useState([])
  const [selectedMajcats, setSelectedMajcats] = useState([])
  const [groupingColumn, setGroupingColumn] = useState('MACRO_MVGR')
  const [useSequence, setUseSequence] = useState(true)
  const [saveToDb, setSaveToDb] = useState(false)
  const [target, setTarget] = useState('Both')

  // Jobs
  const [jobs, setJobs] = useState([])
  const [activeJobId, setActiveJobId] = useState(null)
  const [activeJobData, setActiveJobData] = useState(null)
  const [resultTab, setResultTab] = useState('store')
  const pollRef = useRef(null)

  // Load config
  useEffect(() => {
    Promise.all([contribAPI.listPresets(), contribAPI.getGroupingColumns()])
      .then(([pr, gc]) => {
        setPresets(pr.data?.data?.presets || [])
        setGroupingCols(gc.data?.data?.columns || ['MACRO_MVGR'])
      }).catch(() => toast.error('Failed to load config'))
  }, [])

  useEffect(() => {
    if (!groupingColumn) return
    contribAPI.getMajcats(groupingColumn)
      .then(r => { setMajcats(r.data?.data?.majcats || []); setSelectedMajcats([]) })
      .catch(() => {})
  }, [groupingColumn])

  // Poll jobs
  const refreshJobs = useCallback(async () => {
    try {
      const { data } = await contribAPI.listJobs()
      setJobs(data.data?.jobs || [])
    } catch {}
  }, [])

  useEffect(() => {
    refreshJobs()
    pollRef.current = setInterval(refreshJobs, 5000)
    return () => clearInterval(pollRef.current)
  }, [refreshJobs])

  // Track which jobs we've already fetched results for & auto-deleted
  const fetchedRef = useRef(new Set())
  const deletedRef = useRef(new Set())

  // Load full job data when completed, then auto-delete from server after fetching results
  useEffect(() => {
    if (!activeJobId) return
    const job = jobs.find(j => j.id === activeJobId)
    if (!job) return

    // Fetch results when completed/failed (once)
    if ((job.status === 'completed' || job.status === 'failed') && !fetchedRef.current.has(activeJobId)) {
      fetchedRef.current.add(activeJobId)
      contribAPI.getJob(activeJobId).then(r => {
        const d = r.data?.data?.job
        setActiveJobData(d)
        if (d?.store_rows > 0) setResultTab('store')
        else if (d?.company_rows > 0) setResultTab('company')
      }).catch(() => {})
    }
  }, [activeJobId, jobs])

  // Auto-delete completed/failed/cancelled jobs from server (keep them out of jobs list)
  useEffect(() => {
    for (const j of jobs) {
      if (['completed', 'failed', 'cancelled'].includes(j.status) && !deletedRef.current.has(j.id)) {
        // Don't delete if it's the active job and we haven't fetched results yet
        if (j.id === activeJobId && j.status === 'completed' && !fetchedRef.current.has(j.id)) continue
        // For completed active job: wait a moment for results to load, then delete
        const delay = j.id === activeJobId ? 3000 : 500
        setTimeout(async () => {
          if (!deletedRef.current.has(j.id)) {
            deletedRef.current.add(j.id)
            try { await contribAPI.deleteJob(j.id) } catch {}
            refreshJobs()
          }
        }, delay)
      }
    }
  }, [jobs, activeJobId])

  const presetNames = presets.map(p => p.preset_name)

  const handleRun = async () => {
    try {
      const { data } = await contribAPI.execute({
        presets: selectedPresets,
        majcats: selectedMajcats,
        grouping_column: groupingColumn,
        save_to_db: saveToDb,
        use_sequence: useSequence,
        target,
      })
      const jobId = data.data?.job_id
      toast.success(`Job ${jobId} queued`)
      setActiveJobId(jobId)
      setActiveJobData(null)
      fetchedRef.current.delete(jobId)
      deletedRef.current.delete(jobId)
      refreshJobs()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Failed to create job')
    }
  }

  const handleCancel = async (id) => {
    try { await contribAPI.cancelJob(id); toast.success('Cancelled'); refreshJobs() }
    catch { toast.error('Cancel failed') }
  }

  const handlePause = async (id) => {
    try { await contribAPI.pauseJob(id); toast.success('Paused'); refreshJobs() }
    catch { toast.error('Pause failed') }
  }

  const handleResume = async (id) => {
    try { await contribAPI.resumeJob(id); toast.success('Resumed'); refreshJobs() }
    catch { toast.error('Resume failed') }
  }

  const handleDeleteJob = async (id) => {
    try {
      await contribAPI.deleteJob(id)
      if (activeJobId === id) { setActiveJobId(null); setActiveJobData(null) }
      refreshJobs()
    } catch {}
  }

  const [downloadingType, setDownloadingType] = useState(null)

  const handleDownload = async (type) => {
    if (!activeJobId || downloadingType) return
    setDownloadingType(type)
    toast('Preparing download…')
    try {
      const res = await contribAPI.downloadJobResult(activeJobId, type)
      const contentType = res.headers['content-type'] || ''
      const ext = contentType.includes('zip') ? 'zip' : 'csv'
      const a = document.createElement('a')
      a.href = URL.createObjectURL(new Blob([res.data]))
      a.download = `contrib_${type}_${activeJobId}.${ext}`
      a.click()
      toast.success('Download complete')
    } catch (e) {
      let msg = 'Download failed'
      try {
        if (e.response?.data instanceof Blob) {
          msg = await e.response.data.text()
          try { msg = JSON.parse(msg).detail || msg } catch {}
        } else {
          msg = e.response?.data?.detail || e.message || msg
        }
      } catch {}
      toast.error(typeof msg === 'string' ? msg.slice(0, 200) : 'Download failed')
    } finally { setDownloadingType(null) }
  }

  const activeJobs = jobs.filter(j => ['running','pending','paused'].includes(j.status))
  const runningCount = activeJobs.length
  const activeJob = jobs.find(j => j.id === activeJobId)

  return (
    <div style={{ color:C.text }}>
      <h1 style={{ fontSize:20, fontWeight:800, margin:'0 0 16px', display:'flex', alignItems:'center', gap:10 }}>
        <Cpu size={20} color={C.primary}/> Contribution % — Execute
      </h1>

      {/* ── ACTIVE JOBS (only pending/running/paused) ── */}
      {activeJobs.length > 0 && (
        <div style={{ background:C.amberBg, border:`1px solid ${C.amberBd}`, borderRadius:10, padding:'10px 18px', marginBottom:16 }}>
          {activeJobs.map(j => {
            const sc = statusColors[j.status] || statusColors.pending
            const Icon = sc.icon
            return (
              <div key={j.id} style={{ display:'flex', alignItems:'center', gap:10, padding:'6px 0' }}>
                <Icon size={14} color={sc.fg} style={j.status==='running' ? { animation:'spin 1s linear infinite' } : {}}/>
                <div style={{ flex:1, minWidth:0 }}>
                  <div style={{ fontSize:12, fontWeight:700, display:'flex', alignItems:'center', gap:6 }}>
                    <span>{j.id}</span>
                    <span style={{ fontSize:10, color:C.textMuted, fontWeight:400 }}>{j.label}</span>
                  </div>
                  <div style={{ fontSize:11, color:C.amber, fontWeight:600 }}>
                    {j.status === 'running' && j.progress}
                    {j.status === 'paused' && `Paused at ${j.progress}`}
                    {j.status === 'pending' && 'Queued...'}
                  </div>
                </div>
                {j.status === 'running' && (
                  <button onClick={() => handlePause(j.id)}
                    style={{ padding:'3px 8px', borderRadius:6, fontSize:10, fontWeight:700, border:'1px solid #93c5fd', background:'#dbeafe', color:'#2563eb', cursor:'pointer' }}>
                    Pause
                  </button>
                )}
                {j.status === 'paused' && (
                  <button onClick={() => handleResume(j.id)}
                    style={{ padding:'3px 8px', borderRadius:6, fontSize:10, fontWeight:700, border:`1px solid ${C.greenBd}`, background:C.greenBg, color:C.green, cursor:'pointer' }}>
                    Resume
                  </button>
                )}
                <button onClick={() => handleCancel(j.id)}
                  style={{ padding:'3px 8px', borderRadius:6, fontSize:10, fontWeight:700, border:'1px solid #fecaca', background:C.redBg, color:C.red, cursor:'pointer' }}>
                  Cancel
                </button>
              </div>
            )
          })}
        </div>
      )}

      {/* ── CONFIG ── */}
      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:16, marginBottom:16 }}>
        <div style={{ background:C.cardBg, border:`1px solid ${C.cardBorder}`, borderRadius:12, padding:18 }}>
          <div style={{ display:'flex', gap:12, marginBottom:14 }}>
            <div style={{ flex:1 }}>
              <div style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase', marginBottom:4 }}>Grouping Column</div>
              <select value={groupingColumn} onChange={e => setGroupingColumn(e.target.value)}
                style={{ width:'100%', padding:'8px 12px', borderRadius:8, border:`1px solid ${C.inputBorder}`, background:C.inputBg, color:C.text, fontSize:13, boxSizing:'border-box' }}>
                {groupingCols.map(c => <option key={c} value={c}>{c}</option>)}
              </select>
            </div>
            <div style={{ flex:1 }}>
              <div style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase', marginBottom:4 }}>Target</div>
              <select value={target} onChange={e => setTarget(e.target.value)}
                style={{ width:'100%', padding:'8px 12px', borderRadius:8, border:`1px solid ${C.inputBorder}`, background:C.inputBg, color:C.text, fontSize:13, boxSizing:'border-box' }}>
                <option value="Both">Both (Store + Company)</option>
                <option value="Store">Store Level Only</option>
                <option value="Company">Company Level Only</option>
              </select>
            </div>
          </div>
          <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:6 }}>
            <div style={{ fontSize:13, fontWeight:700 }}>Presets ({selectedPresets.length || 'all'})</div>
            <button onClick={() => setSelectedPresets(s => s.length===presetNames.length ? [] : [...presetNames])}
              style={{ fontSize:11, fontWeight:600, color:C.primary, background:'none', border:'none', cursor:'pointer' }}>
              {selectedPresets.length === presetNames.length ? 'Clear' : 'Select All'}
            </button>
          </div>
          <div style={{ display:'flex', flexWrap:'wrap', gap:5, maxHeight:120, overflowY:'auto', marginBottom:10 }}>
            {presetNames.map(p => {
              const sel = selectedPresets.includes(p)
              return <button key={p} onClick={() => setSelectedPresets(s => sel ? s.filter(x=>x!==p) : [...s,p])} style={{
                padding:'5px 12px', borderRadius:16, fontSize:12, fontWeight:600, cursor:'pointer',
                border:`1.5px solid ${sel?C.primary:'#e2e8f0'}`, background:sel?C.primary:'#fff', color:sel?'#fff':C.textSub,
              }}>{p}</button>
            })}
          </div>
          <label style={{ display:'flex', alignItems:'center', gap:6, fontSize:12, color:C.textSub, cursor:'pointer' }}>
            <input type="checkbox" checked={useSequence} onChange={e => setUseSequence(e.target.checked)}/> Execute in sequence order
          </label>
        </div>

        <div style={{ background:C.cardBg, border:`1px solid ${C.cardBorder}`, borderRadius:12, padding:18 }}>
          <MultiSelectDropdown
            label={`Major Categories (${selectedMajcats.length || 'all ' + majcats.length})`}
            options={majcats} selected={selectedMajcats} onChange={setSelectedMajcats}
            placeholder={`All ${majcats.length} categories`}
          />
          {selectedMajcats.length > 0 && (
            <div style={{ display:'flex', flexWrap:'wrap', gap:4, marginTop:8, maxHeight:160, overflowY:'auto' }}>
              {selectedMajcats.map(m => (
                <span key={m} style={{ display:'flex', alignItems:'center', gap:3, padding:'3px 8px', borderRadius:12, fontSize:10, fontWeight:600, background:C.greenBg, color:C.green, border:`1px solid ${C.greenBd}` }}>
                  {m} <button onClick={() => setSelectedMajcats(s => s.filter(x=>x!==m))} style={{ background:'none', border:'none', cursor:'pointer', padding:0 }}><X size={10} color={C.green}/></button>
                </span>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Execute bar */}
      <div style={{ background:C.cardBg, border:`1px solid ${C.cardBorder}`, borderRadius:12, padding:'14px 18px', display:'flex', alignItems:'center', gap:14, flexWrap:'wrap', marginBottom:16 }}>
        <label style={{ display:'flex', alignItems:'center', gap:6, fontSize:13, fontWeight:600, color:C.textSub, cursor:'pointer' }}>
          <input type="checkbox" checked={saveToDb} onChange={e => setSaveToDb(e.target.checked)}/> Save to database
        </label>
        <button onClick={handleRun} style={{
          display:'flex', alignItems:'center', gap:8, padding:'10px 28px', borderRadius:10,
          fontSize:14, fontWeight:800, border:'none', cursor:'pointer',
          background:'linear-gradient(135deg,#4f46e5,#7c3aed)', color:'#fff',
          boxShadow:'0 4px 14px rgba(79,70,229,.35)',
        }}>
          <Play size={15}/> Execute Pipeline
        </button>
        {runningCount > 0 && <span style={{ fontSize:12, color:C.amber, fontWeight:600 }}>{runningCount} job(s) running in background…</span>}
      </div>

      {/* ── ACTIVE JOB LOG ── */}
      {activeJobData?.log?.length > 0 && (
        <div style={{ background:C.cardBg, border:`1px solid ${C.cardBorder}`, borderRadius:12, overflow:'hidden', marginBottom:16 }}>
          <div style={{ padding:'10px 18px', background:C.headerBg, borderBottom:`1px solid ${C.cardBorder}`, fontSize:13, fontWeight:700 }}>
            Job {activeJobId} — Log
          </div>
          <div style={{ padding:12, maxHeight:160, overflowY:'auto' }}>
            {activeJobData.log.map((l, i) => (
              <div key={i} style={{ display:'flex', alignItems:'center', gap:8, padding:'3px 0', fontSize:12 }}>
                {l.status==='ok' && <CheckCircle2 size={13} color={C.green}/>}
                {l.status==='ok' && <CheckCircle2 size={13} color={C.green}/>}
                {l.status==='error' && <XCircle size={13} color={C.red}/>}
                {l.status==='empty' && <AlertCircle size={13} color={C.amber}/>}
                {l.action && <CheckCircle2 size={13} color={C.green}/>}
                {l.step && <span style={{ color:C.primary, fontWeight:700, fontSize:11 }}>[{l.step}]</span>}
                <span style={{ fontWeight:600 }}>{l.preset||l.action||''}</span>
                {l.rows!=null && <span style={{ color:C.textSub }}>{l.rows.toLocaleString()} rows</span>}
                {l.duration!=null && <span style={{ color:C.textMuted }}>{l.duration}s</span>}
                {l.table && <code style={{ fontSize:10, color:C.primary, background:C.primaryLight, padding:'1px 5px', borderRadius:3 }}>{l.table}</code>}
                {l.store_cols>0 && <span style={{ color:C.textMuted, fontSize:10 }}>{l.store_cols} store cols</span>}
                {l.company_cols>0 && <span style={{ color:C.textMuted, fontSize:10 }}>{l.company_cols} co cols</span>}
                {l.timing && (
                  <span style={{ fontSize:10, color:C.textMuted }}>
                    (sql:{l.timing.sql_data}s master:{l.timing.sql_master}s merge:{l.timing.merge}s agg:{l.timing.aggregate}s kpi:{l.timing.kpi}s)
                  </span>
                )}
                {l.error && <span style={{ color:C.red, fontSize:11 }}>{l.error}</span>}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── RESULTS ── */}
      {activeJobData && (activeJobData.store_preview?.length > 0 || activeJobData.company_preview?.length > 0) && (
        <>
          <div style={{ display:'flex', gap:4, marginBottom:12, background:'#fff', border:`1px solid ${C.cardBorder}`, borderRadius:8, padding:3, width:'fit-content' }}>
            {activeJobData.store_preview?.length > 0 && (
              <button onClick={() => setResultTab('store')} style={{
                padding:'6px 18px', borderRadius:6, fontSize:13, fontWeight:700, border:'none', cursor:'pointer',
                background:resultTab==='store'?C.primary:'transparent', color:resultTab==='store'?'#fff':C.textSub,
              }}>Store ({activeJobData.store_rows?.toLocaleString()})</button>
            )}
            {activeJobData.company_preview?.length > 0 && (
              <button onClick={() => setResultTab('company')} style={{
                padding:'6px 18px', borderRadius:6, fontSize:13, fontWeight:700, border:'none', cursor:'pointer',
                background:resultTab==='company'?C.green:'transparent', color:resultTab==='company'?'#fff':C.textSub,
              }}>Company ({activeJobData.company_rows?.toLocaleString()})</button>
            )}
          </div>
          {resultTab==='store' && <ResultTable title="Store-Level" columns={activeJobData.store_columns} preview={activeJobData.store_preview} totalRows={activeJobData.store_rows} onDownload={() => handleDownload('store')} downloading={downloadingType==='store'}/>}
          {resultTab==='company' && <ResultTable title="Company-Level" columns={activeJobData.company_columns} preview={activeJobData.company_preview} totalRows={activeJobData.company_rows} onDownload={() => handleDownload('company')} downloading={downloadingType==='company'}/>}
        </>
      )}

      {/* Active job running indicator */}
      {activeJob?.status === 'running' && (
        <div style={{ background:C.amberBg, border:`1px solid ${C.amberBd}`, borderRadius:12, padding:'20px 18px', textAlign:'center', marginTop:16 }}>
          <RefreshCw size={20} color={C.amber} style={{ animation:'spin 1s linear infinite', margin:'0 auto 8px', display:'block' }}/>
          <div style={{ fontSize:14, fontWeight:700, color:C.amber }}>Job {activeJobId} running…</div>
          <div style={{ fontSize:12, color:C.amber, marginTop:4 }}>{activeJob.progress}</div>
          <div style={{ fontSize:11, color:C.textMuted, marginTop:4 }}>You can navigate away — the job runs in the background.</div>
        </div>
      )}

      <style>{`@keyframes spin{to{transform:rotate(360deg);}}`}</style>
    </div>
  )
}
