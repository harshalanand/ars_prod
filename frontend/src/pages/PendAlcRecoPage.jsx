/**
 * PendAlcRecoPage — Full reconciliation view for ARS_PEND_ALC
 * Shows aging, mode/source breakdown, per-RDC, BDC-unconfirmed, and filterable detail.
 * Actions: Generate BDC (download Excel), trigger MSA patch.
 */
import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { pendAlcAPI } from '@/services/api'
import toast from 'react-hot-toast'
import { RefreshCw, BarChart2, Download, AlertTriangle, ChevronDown, ChevronRight, AlertOctagon } from 'lucide-react'
import DataGrid from '@/components/DataGrid'

const C = {
  primary: '#4f46e5', blue: '#0891b2', green: '#16a34a',
  amber: '#d97706', red: '#dc2626', text: '#1e293b',
  textSub: '#64748b', textMuted: '#94a3b8', border: '#e2e8f0',
  bg: '#f8fafc', card: '#ffffff',
}

const AGING_ACCENT = { '0-7d': C.green, '8-30d': C.blue, '31-60d': C.amber, '60d+': C.red }

function fmt(n) {
  return typeof n === 'number' ? n.toLocaleString(undefined, { maximumFractionDigits: 0 }) : '—'
}

function AgingTile({ band, rows, pend_qty, alloc_qty, active, onClick, onExport, exporting }) {
  const accent = AGING_ACCENT[band] || C.textSub
  const pct = alloc_qty > 0 ? (100 * pend_qty / alloc_qty).toFixed(0) : '—'
  return (
    <div onClick={onClick}
      style={{ background: C.card, border: `1px solid ${active ? accent : C.border}`,
               borderRadius: 8, padding: '12px 14px',
               borderLeft: `3px solid ${accent}`,
               cursor: onClick ? 'pointer' : 'default',
               boxShadow: active ? `0 0 0 1px ${accent} inset` : 'none',
               position: 'relative' }}>
      <div style={{ fontSize: 9, fontWeight: 700, color: accent, letterSpacing: '.06em',
                    marginBottom: 4 }}>{band}{active && ' ●'}</div>
      <div style={{ fontSize: 20, fontWeight: 800, color: C.text, lineHeight: 1 }}>
        {fmt(pend_qty)}
      </div>
      <div style={{ fontSize: 9, color: C.textMuted, marginTop: 3 }}>
        {rows} rows · {pct}% of alloc
      </div>
      {onExport && (
        <button onClick={(e) => { e.stopPropagation(); onExport() }}
          disabled={exporting}
          title={`Export ${band} rows to Excel`}
          style={tileExportBtn(accent, exporting)}>
          <Download size={10}/>
        </button>
      )}
    </div>
  )
}

function StatusTile({ color, label, rows, qty, hint, active, onClick, onExport, exporting }) {
  return (
    <div onClick={onClick} title={hint || ''}
      style={{ background: C.card, border: `1px solid ${active ? color : C.border}`,
               borderRadius: 8, padding: '10px 12px',
               borderTop: `3px solid ${color}`,
               cursor: onClick ? 'pointer' : 'default',
               boxShadow: active ? `0 0 0 1px ${color} inset` : 'none',
               position: 'relative' }}>
      <div style={{ fontSize: 9, fontWeight: 700, color, letterSpacing: '.06em',
                    marginBottom: 3 }}>{label}{active && ' ●'}</div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
        <div style={{ fontSize: 18, fontWeight: 800, color: C.text, lineHeight: 1 }}>{fmt(rows)}</div>
        <div style={{ fontSize: 9, color: C.textMuted }}>rows</div>
      </div>
      <div style={{ fontSize: 9, color: C.textMuted, marginTop: 3 }}>
        {fmt(qty)} units
      </div>
      {onExport && (
        <button onClick={(e) => { e.stopPropagation(); onExport() }}
          disabled={exporting}
          title={`Export ${label} rows to Excel`}
          style={tileExportBtn(color, exporting)}>
          <Download size={10}/>
        </button>
      )}
    </div>
  )
}

const tileExportBtn = (color, exporting) => ({
  position: 'absolute', top: 6, right: 6,
  background: '#fff', border: `1px solid ${color}40`,
  color, borderRadius: 3, padding: '2px 4px',
  cursor: exporting ? 'wait' : 'pointer',
  display: 'flex', alignItems: 'center',
  opacity: exporting ? 0.5 : 1,
})

function ModeBadge({ value }) {
  const color = value === 'MANUAL' ? C.amber : C.primary
  return (
    <span style={{ fontSize: 8, fontWeight: 700, padding: '2px 6px', borderRadius: 3,
                   background: color + '22', color }}>
      {value || 'AUTO'}
    </span>
  )
}

function StatusBadge({ closed }) {
  return (
    <span style={{ fontSize: 8, fontWeight: 700, padding: '2px 6px', borderRadius: 3,
                   background: closed ? '#dcfce7' : '#fef3c7',
                   color: closed ? C.green : C.amber }}>
      {closed ? 'CLOSED' : 'OPEN'}
    </span>
  )
}

function BdcStatusBadge({ value }) {
  if (!value) return <span style={{color:C.textMuted}}>—</span>
  const map = {
    NEVER_SENT:     { bg:'#f1f5f9',  fg:C.textMuted, label:'NEVER SENT'    },
    OPEN:           { bg:'#fef3c7',  fg:C.amber,     label:'OPEN'          },
    PARTIAL:        { bg:'#dbeafe',  fg:C.blue,      label:'PARTIAL'       },
    CLOSED_PARTIAL: { bg:'#fde68a',  fg:'#b45309',   label:'CLOSED PARTIAL'},
    CONFIRMED:      { bg:'#dcfce7',  fg:C.green,     label:'CONFIRMED'     },
    CANCELLED:      { bg:'#fee2e2',  fg:C.red,       label:'CANCELLED'     },
  }
  const s = map[value] || { bg:'#f1f5f9', fg:C.textSub, label:value }
  return (
    <span style={{ fontSize:8, fontWeight:700, padding:'2px 6px', borderRadius:3,
                   background:s.bg, color:s.fg }}>
      {s.label}
    </span>
  )
}

const TH = ({ children, right }) => (
  <th style={{ padding: '7px 10px', textAlign: right ? 'right' : 'left',
               fontSize: 9, fontWeight: 700, color: C.textSub, letterSpacing: '.05em',
               borderBottom: `1px solid ${C.border}`, whiteSpace: 'nowrap' }}>
    {children}
  </th>
)

export default function PendAlcRecoPage() {
  const [recoSummary, setRecoSummary] = useState(null)
  const [summaryLoading, setSummaryLoading] = useState(false)
  const [sessions, setSessions] = useState([])
  const [sessionsLoading, setSessionsLoading] = useState(false)
  // Collapsed by default — long session lists were pushing the detail grid
  // below the fold on smaller screens.
  const [sessionsCollapsed, setSessionsCollapsed] = useState(true)

  // The grid manages its own data + loading internally; we only bump
  // `gridBumpKey` to force a refresh.
  const [gridBumpKey, setGridBumpKey] = useState(0)

  // Filters
  const [fDateFrom, setFDateFrom] = useState('')
  const [fDateTo,   setFDateTo]   = useState('')
  const [fRdc,      setFRdc]      = useState('')
  const [fMajCat,   setFMajCat]   = useState('')
  const [fMode,     setFMode]     = useState('')
  const [fClosed,   setFClosed]   = useState('open') // 'open' | 'closed' | 'all'
  const [fSession,  setFSession]  = useState('')
  // Tile-driven filters — set by clicking a status / aging tile. Empty
  // strings mean "no tile filter active". The status filter accepts CSV
  // of BDC_HISTORY.STATUS values (incl. 'NEVER_SENT' for rows that never
  // had a BDC sent); aging accepts CSV of '0-7d' / '8-30d' / '31-60d' / '60d+'.
  const [fBdcStatus,  setFBdcStatus]  = useState('')
  const [fAgingBand,  setFAgingBand]  = useState('')
  const [exporting,   setExporting]   = useState('')   // tile id currently exporting

  // Pending vs MSA gap report — open pending whose MSA pool can't cover it.
  // Self-contained (no DataGrid) — small dataset since it's the actionable
  // subset of pending where ops should intervene (adhoc-close or wait for
  // the next MSA refresh).
  const [gapCollapsed, setGapCollapsed] = useState(false)
  const [gapData, setGapData]           = useState(null)
  const [gapLoading, setGapLoading]     = useState(false)
  const [gapPage, setGapPage]           = useState(1)
  const [gapPageSize]                   = useState(200)
  const [gapSortBy, setGapSortBy]       = useState('gap')
  const [gapSortDir, setGapSortDir]     = useState('desc')
  const [gapStatusFilter, setGapStatusFilter] = useState('') // '' | NO_MSA | SHORT
  const [gapExporting, setGapExporting] = useState(false)

  // BDC
  const [bdcLoading, setBdcLoading] = useState(false)
  const [bdcModalOpen, setBdcModalOpen] = useState(false)
  const [bdcDate, setBdcDate] = useState(() => {
    // Default = tomorrow if today is Mon-Fri, else next Monday
    const d = new Date(); d.setDate(d.getDate() + 1)
    while (d.getDay() === 0) d.setDate(d.getDate() + 1) // skip Sunday
    return d.toISOString().slice(0, 10)
  })
  const [bdcScheduleStores, setBdcScheduleStores] = useState([])
  const [bdcSelectedStores, setBdcSelectedStores] = useState(new Set())
  const [bdcWeekday, setBdcWeekday] = useState('')
  const [bdcDateLoading, setBdcDateLoading] = useState(false)
  // Async job tracking — populated while the BDC backend job runs/finishes
  // so the modal can show progress + a "Job complete" confirmation banner.
  const [bdcJobStatus, setBdcJobStatus] = useState(null)
  const [bdcJobResult, setBdcJobResult] = useState(null)

  const loadSummary = useCallback(async () => {
    setSummaryLoading(true)
    try {
      const { data } = await pendAlcAPI.recoSummary()
      setRecoSummary(data?.data || null)
    } catch {
      toast.error('Failed to load reco summary')
    } finally {
      setSummaryLoading(false)
    }
  }, [])

  const loadGap = useCallback(async () => {
    setGapLoading(true)
    try {
      const params = { page: gapPage, page_size: gapPageSize,
                       sort_by: gapSortBy, sort_dir: gapSortDir }
      if (fRdc)            params.rdc     = fRdc
      if (fMajCat)         params.maj_cat = fMajCat
      if (gapStatusFilter) params.status  = gapStatusFilter
      const { data } = await pendAlcAPI.pendVsMsaGap(params)
      setGapData(data?.data || null)
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Failed to load gap report')
      setGapData(null)
    } finally {
      setGapLoading(false)
    }
  }, [fRdc, fMajCat, gapStatusFilter, gapPage, gapPageSize, gapSortBy, gapSortDir])

  const exportGap = async () => {
    setGapExporting(true)
    try {
      const params = {}
      if (fRdc)            params.rdc     = fRdc
      if (fMajCat)         params.maj_cat = fMajCat
      if (gapStatusFilter) params.status  = gapStatusFilter
      const { data: blob } = await pendAlcAPI.pendVsMsaGapExport(params)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a'); a.href = url
      const today = new Date().toISOString().slice(0,10).replace(/-/g,'')
      a.download = `PEND_VS_MSA_GAP_${today}.csv`
      document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
      toast.success('CSV downloaded')
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Export failed')
    } finally {
      setGapExporting(false)
    }
  }

  const toggleGapSort = (col) => {
    if (gapSortBy === col) setGapSortDir(d => d === 'asc' ? 'desc' : 'asc')
    else { setGapSortBy(col); setGapSortDir(col === 'rdc' || col === 'article_number' ? 'asc' : 'desc') }
    setGapPage(1)
  }

  const loadSessions = useCallback(async () => {
    setSessionsLoading(true)
    try {
      const { data } = await pendAlcAPI.sessions()
      setSessions(data?.data || [])
    } catch {
      // Silent — sessions panel is supplementary; the rest of the page
      // works fine without it.
    } finally {
      setSessionsLoading(false)
    }
  }, [])

  // The DataGrid component drives its own pagination/sort/per-column filter
  // and calls this fetcher whenever any of those change. The top-bar filters
  // (date range, RDC, etc.) are merged in here and changes bump `refreshKey`
  // so the grid re-fetches.
  // Autocomplete adapter — called by each text-filter column's `suggester`.
  // Returns up to 20 distinct values containing `q`; backend filters by
  // case-insensitive LIKE %q%.
  const suggestCol = useCallback(async (col, q) => {
    try {
      const { data } = await pendAlcAPI.recoSuggest(col, q || '', 20)
      return data?.values || []
    } catch { return [] }
  }, [])

  // Per-column DataGrid filter keys → backend query-param names.
  // The grid uses the column key (e.g. `rdc`) as the param; the backend
  // expects `f_rdc` (multi) or `q_article` (contains). Without this map
  // the header filter funnels were no-ops.
  const GRID_FILTER_MAP = {
    rdc:            'f_rdc',
    st_cd:          'f_st_cd',
    maj_cat:        'f_maj_cat',
    alloc_mode:     'f_alloc_mode',
    bdc_status:     'f_bdc_status',
    aging_band:     'f_aging_band',
    article_number: 'q_article',
    clr:            'q_clr',
    do_number:      'q_do_number',
    bdc_alloc_no:   'q_bdc_alloc_no',
  }

  const fetchReco = useCallback(async (gridParams) => {
    const params = {}
    Object.entries(gridParams || {}).forEach(([k, v]) => {
      params[GRID_FILTER_MAP[k] || k] = v
    })
    if (fDateFrom) params.date_from  = fDateFrom
    if (fDateTo)   params.date_to    = fDateTo
    if (fRdc)      params.rdc        = fRdc
    if (fMajCat)   params.maj_cat    = fMajCat
    if (fMode)     params.alloc_mode = fMode
    if (fSession)  params.session_id = fSession
    if (fClosed === 'open')   params.closed = false
    if (fClosed === 'closed') params.closed = true
    if (fBdcStatus) params.f_bdc_status = fBdcStatus
    if (fAgingBand) params.f_aging_band = fAgingBand
    return pendAlcAPI.reco(params)
  }, [fDateFrom, fDateTo, fRdc, fMajCat, fMode, fClosed, fSession,
      fBdcStatus, fAgingBand])

  // Bump this to make the grid re-fetch from page 1.
  const recoRefreshKey = useMemo(
    () => `${fDateFrom}|${fDateTo}|${fRdc}|${fMajCat}|${fMode}|${fClosed}|${fSession}|${fBdcStatus}|${fAgingBand}`,
    [fDateFrom, fDateTo, fRdc, fMajCat, fMode, fClosed, fSession,
     fBdcStatus, fAgingBand]
  )

  useEffect(() => { loadSummary(); loadSessions() }, [loadSummary, loadSessions])
  // Gap report — refetches whenever the user toggles status, sort, page,
  // or the page-level RDC/MAJ_CAT filters. Skipped while the section is
  // collapsed so we don't hammer the DB unnecessarily.
  useEffect(() => { if (!gapCollapsed) loadGap() }, [loadGap, gapCollapsed])

  // Tile → filter mapping. Each tile sets `closed`, `f_bdc_status`,
  // `f_aging_band` to scope the detail grid + Excel export. Click the same
  // tile again to clear. Aging tiles + status tiles are independent — you
  // can stack one of each.
  //
  // Mapping rationale:
  //   pending_bdc  → closed=false AND latest BDC history STATUS is null /
  //                   terminal (NEVER_SENT/CLOSED_PARTIAL/CONFIRMED/CANCELLED).
  //                   Same set that the next /bdc-generate would pick up.
  //   pending_do   → closed=false AND latest BDC history STATUS = 'OPEN'.
  //   partial      → closed=false AND latest BDC history STATUS = 'CLOSED_PARTIAL'.
  //                   (Legacy 'PARTIAL' rows pre-cutover keep the same chip.)
  //   closed       → IS_CLOSED = 1.
  //   aging:<band> → aging band CSV filter + closed=false.
  const STATUS_TILE_MAP = {
    pending_bdc: { closed: 'open', bdc_status: 'NEVER_SENT,CLOSED_PARTIAL,CONFIRMED,CANCELLED' },
    pending_do:  { closed: 'open', bdc_status: 'OPEN' },
    partial:     { closed: 'open', bdc_status: 'CLOSED_PARTIAL,PARTIAL' },
    closed:      { closed: 'closed', bdc_status: '' },
  }

  const tileActive = useMemo(() => {
    const out = { pending_bdc: false, pending_do: false, partial: false, closed: false, aging: '' }
    for (const [k, v] of Object.entries(STATUS_TILE_MAP)) {
      if (fClosed === v.closed && fBdcStatus === v.bdc_status) out[k] = true
    }
    // If a status tile isn't matching but the user has *some* status
    // filter active, mark none — keeps the UI honest when filters drift.
    out.aging = fAgingBand || ''
    return out
  }, [fClosed, fBdcStatus, fAgingBand])

  const applyTileFilter = (tileId) => {
    const m = STATUS_TILE_MAP[tileId]
    if (!m) return
    // Toggle off if already active
    if (tileActive[tileId]) {
      setFClosed('open'); setFBdcStatus('')
      return
    }
    setFClosed(m.closed); setFBdcStatus(m.bdc_status)
    setGridBumpKey(k => k + 1)
  }

  const applyAgingFilter = (band) => {
    if (fAgingBand === band) { setFAgingBand(''); return }   // toggle off
    setFAgingBand(band)
    setGridBumpKey(k => k + 1)
  }

  // Export current filtered detail view to CSV (no tile scope — uses
  // exactly what the grid is showing). Honours every active filter:
  // top-bar fields + any tile filter active.
  const exportFiltered = async () => {
    setExporting('filtered')
    try {
      const params = {}
      if (fDateFrom) params.date_from  = fDateFrom
      if (fDateTo)   params.date_to    = fDateTo
      if (fRdc)      params.rdc        = fRdc
      if (fMajCat)   params.maj_cat    = fMajCat
      if (fMode)     params.alloc_mode = fMode
      if (fSession)  params.session_id = fSession
      if (fClosed === 'open')   params.closed = false
      if (fClosed === 'closed') params.closed = true
      if (fBdcStatus) params.f_bdc_status = fBdcStatus
      if (fAgingBand) params.f_aging_band = fAgingBand
      const { data: blob } = await pendAlcAPI.recoExport(params)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a'); a.href = url
      const today = new Date().toISOString().slice(0,10).replace(/-/g,'')
      a.download = `PEND_ALC_RECO_FILTERED_${today}.csv`
      document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
      toast.success('CSV downloaded')
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Export failed')
    } finally {
      setExporting('')
    }
  }

  // Per-tile CSV export — hits /reco-export with that tile's scope.
  const exportTile = async (tileId) => {
    const isAging = tileId.startsWith('aging:')
    const m = isAging
      ? { closed: 'open', bdc_status: '' }
      : STATUS_TILE_MAP[tileId]
    if (!m) return
    setExporting(tileId)
    try {
      const params = {}
      if (fDateFrom) params.date_from  = fDateFrom
      if (fDateTo)   params.date_to    = fDateTo
      if (fRdc)      params.rdc        = fRdc
      if (fMajCat)   params.maj_cat    = fMajCat
      if (fMode)     params.alloc_mode = fMode
      if (fSession)  params.session_id = fSession
      if (m.closed === 'open')   params.closed = false
      if (m.closed === 'closed') params.closed = true
      if (m.bdc_status) params.f_bdc_status = m.bdc_status
      if (isAging) params.f_aging_band = tileId.slice('aging:'.length)
      const { data: blob } = await pendAlcAPI.recoExport(params)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a'); a.href = url
      const today = new Date().toISOString().slice(0,10).replace(/-/g,'')
      a.download = `PEND_ALC_RECO_${tileId.replace(':','_').toUpperCase()}_${today}.csv`
      document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
      toast.success('CSV downloaded')
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Export failed')
    } finally {
      setExporting('')
    }
  }

  // Load stores scheduled for the picked date (Mon-Sat schedule lookup)
  const loadScheduleForDate = useCallback(async (dateStr) => {
    if (!dateStr) return
    setBdcDateLoading(true)
    try {
      const { data } = await pendAlcAPI.scheduleStoresFor(dateStr)
      const stores = data?.stores || []
      setBdcScheduleStores(stores)
      setBdcSelectedStores(new Set(stores))      // pre-select all
      setBdcWeekday(data?.weekday || '')
    } catch {
      setBdcScheduleStores([]); setBdcSelectedStores(new Set()); setBdcWeekday('')
    } finally {
      setBdcDateLoading(false)
    }
  }, [])

  const openBdcModal = () => {
    setBdcModalOpen(true)
    loadScheduleForDate(bdcDate)
  }

  const toggleStore = (st) => {
    setBdcSelectedStores(prev => {
      const next = new Set(prev)
      next.has(st) ? next.delete(st) : next.add(st)
      return next
    })
  }

  // Async BDC generate: kick off background job, poll status, download ZIP,
  // show completion summary. Survives Cloudflare's 100s edge timeout on
  // large batches (the old sync call would hang the browser then fail).
  const handleBdcGenerate = async () => {
    setBdcLoading(true)
    setBdcJobStatus(null); setBdcJobResult(null)
    try {
      const params = {}
      if (fRdc)    params.rdc     = fRdc
      if (fMajCat) params.maj_cat = fMajCat
      if (bdcDate) params.target_date = bdcDate
      if (bdcScheduleStores.length > 0) {
        params.st_cd_list = [...bdcSelectedStores]
        if (params.st_cd_list.length === 0) {
          toast.error('Select at least one store')
          setBdcLoading(false); return
        }
      }

      const startResp = await pendAlcAPI.bdcGenerateAsync(params)
      const jobId = startResp.data?.job_id
      if (!jobId) throw new Error('No job_id in response')
      setBdcJobStatus({ status: 'pending', progress: 'queued' })

      // Poll every 2s until completed / failed.
      const poll = () => new Promise((resolve, reject) => {
        const timer = setInterval(async () => {
          try {
            const s = await pendAlcAPI.asyncJobStatus(jobId)
            const j = s.data?.data
            if (!j) return
            setBdcJobStatus(j)
            if (j.status === 'completed') { clearInterval(timer); resolve(j) }
            else if (j.status === 'failed') {
              clearInterval(timer)
              reject(new Error(j.error || 'Job failed'))
            }
          } catch (err) {
            clearInterval(timer); reject(err)
          }
        }, 2000)
      })

      const finalJob = await poll()

      // Job complete — fetch the ZIP and trigger browser download.
      const dl = await pendAlcAPI.asyncJobDownload(jobId)
      const url = URL.createObjectURL(new Blob([dl.data], { type: 'application/zip' }))
      const a = document.createElement('a')
      const stamp = (bdcDate || new Date().toISOString().slice(0, 10)).replace(/-/g, '')
      const alloc = finalJob.result?.allocation_no || ''
      a.href = url
      a.download = `ARS_BDC_${stamp}${alloc ? '_' + alloc : ''}.zip`
      document.body.appendChild(a); a.click()
      document.body.removeChild(a); URL.revokeObjectURL(url)

      setBdcJobResult(finalJob.result || null)
      toast.success(`BDC ready — ${finalJob.result?.rdc_count || 0} RDC file(s), `
        + `${(finalJob.result?.row_count || 0).toLocaleString()} rows`)
      loadSummary(); loadSessions(); setGridBumpKey(k => k + 1)
    } catch (e) {
      toast.error(e.response?.data?.detail || e.message || 'BDC generate failed')
      setBdcJobStatus(null)
    } finally {
      setBdcLoading(false)
    }
  }

  const _btn = (variant = 'default') => {
    if (variant === 'primary') return {
      fontSize: 10, fontWeight: 700, padding: '5px 14px', borderRadius: 4,
      border: 'none', background: C.primary, color: '#fff', cursor: 'pointer',
      display: 'flex', alignItems: 'center', gap: 5,
    }
    if (variant === 'amber') return {
      fontSize: 10, fontWeight: 700, padding: '5px 14px', borderRadius: 4,
      border: `1px solid ${C.amber}`, background: C.amber + '10', color: C.amber,
      cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 5,
    }
    return {
      fontSize: 10, padding: '4px 10px', borderRadius: 4,
      border: `1px solid ${C.border}`, background: '#fff', color: C.textSub,
      cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4,
    }
  }

  const inp = {
    fontSize: 10, padding: '4px 8px', border: `1px solid ${C.border}`,
    borderRadius: 4, outline: 'none', background: '#fff',
  }

  const s = recoSummary

  return (
    <div style={{ padding: '16px 20px', fontFamily: 'Inter,system-ui,sans-serif',
                  fontSize: 11, color: C.text, background: C.bg, minHeight: '100vh' }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <BarChart2 size={16} color={C.primary}/>
        <div style={{ fontSize: 13, fontWeight: 800, color: C.text }}>PEND_ALC Reconciliation</div>
        <div style={{ fontSize: 10, color: C.textMuted, marginLeft: 4 }}>
          RDC = source warehouse · ST_CD = destination store · Aging · BDC tracking · MSA sync
        </div>
        <div style={{ flex: 1 }}/>
        <button style={_btn()}
          onClick={() => { loadSummary(); loadSessions(); loadGap(); setGridBumpKey(k => k + 1) }}
          disabled={summaryLoading}>
          <RefreshCw size={11}
            style={{ animation: summaryLoading ? 'spin 1s linear infinite' : 'none' }}/>
          Refresh
        </button>
        <button style={_btn('primary')} onClick={openBdcModal} disabled={bdcLoading}>
          <Download size={11} style={{ animation: bdcLoading ? 'spin 1s linear infinite' : 'none' }}/>
          {bdcLoading ? 'Generating…' : 'Generate BDC'}
        </button>
      </div>

      {/* BDC Generate modal — date + scheduled stores */}
      {bdcModalOpen && (
        <div onClick={() => !bdcLoading && setBdcModalOpen(false)}
          style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.45)',
                   display: 'flex', alignItems: 'center', justifyContent: 'center',
                   zIndex: 50 }}>
          <div onClick={e => e.stopPropagation()}
            style={{ background: '#fff', borderRadius: 8, width: 580, maxWidth: '92vw',
                     maxHeight: '85vh', overflowY: 'auto', boxShadow: '0 12px 32px rgba(0,0,0,.2)' }}>
            <div style={{ padding: '12px 16px', borderBottom: `1px solid ${C.border}`,
                          display: 'flex', alignItems: 'center', gap: 10 }}>
              <Download size={14} color={C.primary}/>
              <div style={{ fontSize: 13, fontWeight: 800 }}>Generate BDC for date</div>
              <div style={{ flex: 1 }}/>
              <button onClick={() => setBdcModalOpen(false)} disabled={bdcLoading}
                style={{ background: 'none', border: 'none', cursor: 'pointer', fontSize: 16,
                         color: C.textMuted }}>×</button>
            </div>

            <div style={{ padding: 16 }}>
              {/* Date picker */}
              <div style={{ marginBottom: 14 }}>
                <div style={{ fontSize: 10, fontWeight: 700, color: C.textSub,
                              letterSpacing: '.05em', marginBottom: 6 }}>
                  TARGET DATE
                </div>
                <input type="date" value={bdcDate}
                  onChange={e => { setBdcDate(e.target.value); loadScheduleForDate(e.target.value) }}
                  style={{ fontSize: 12, padding: '6px 10px', borderRadius: 4,
                           border: `1px solid ${C.border}`, fontFamily: 'inherit' }}/>
                {bdcWeekday && (
                  <span style={{ fontSize: 11, color: C.textSub, marginLeft: 10, fontWeight: 600 }}>
                    {bdcWeekday}
                  </span>
                )}
              </div>

              {/* Scheduled stores */}
              <div>
                <div style={{ display: 'flex', alignItems: 'center',
                              marginBottom: 6 }}>
                  <div style={{ fontSize: 10, fontWeight: 700, color: C.textSub,
                                letterSpacing: '.05em' }}>
                    SCHEDULED STORES ({bdcScheduleStores.length})
                  </div>
                  <div style={{ flex: 1 }}/>
                  {bdcScheduleStores.length > 0 && (
                    <>
                      <button onClick={() => setBdcSelectedStores(new Set(bdcScheduleStores))}
                        style={{ fontSize: 9, padding: '3px 8px', borderRadius: 3,
                                 border: `1px solid ${C.primary}`, background: '#fff',
                                 color: C.primary, cursor: 'pointer', marginRight: 6 }}>
                        Select All
                      </button>
                      <button onClick={() => setBdcSelectedStores(new Set())}
                        style={{ fontSize: 9, padding: '3px 8px', borderRadius: 3,
                                 border: `1px solid ${C.border}`, background: '#fff',
                                 color: C.textSub, cursor: 'pointer' }}>
                        Clear
                      </button>
                    </>
                  )}
                </div>

                {bdcDateLoading ? (
                  <div style={{ padding: 20, textAlign: 'center', color: C.textMuted, fontSize: 11 }}>
                    Loading schedule…
                  </div>
                ) : bdcScheduleStores.length === 0 ? (
                  <div style={{ padding: 14, textAlign: 'center', fontSize: 11,
                                background: C.amber + '15', border: `1px solid ${C.amber}40`,
                                borderRadius: 4, color: C.amber, fontWeight: 600 }}>
                    No stores scheduled for {bdcWeekday || 'this date'}.
                    The BDC will include <b>all stores with open pending qty</b>.
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5,
                                maxHeight: 220, overflowY: 'auto', padding: 6,
                                background: C.bg, border: `1px solid ${C.border}`, borderRadius: 4 }}>
                    {bdcScheduleStores.map(st => {
                      const sel = bdcSelectedStores.has(st)
                      return (
                        <button key={st} onClick={() => toggleStore(st)}
                          style={{ fontSize: 10, padding: '4px 10px', borderRadius: 12,
                                   border: `1px solid ${sel ? C.primary : C.border}`,
                                   background: sel ? C.primary : '#fff',
                                   color: sel ? '#fff' : C.textSub,
                                   cursor: 'pointer', fontWeight: 600 }}>
                          {sel && '✓ '}{st}
                        </button>
                      )
                    })}
                  </div>
                )}

                {bdcScheduleStores.length > 0 && (
                  <div style={{ marginTop: 6, fontSize: 10, color: C.textMuted }}>
                    {bdcSelectedStores.size} of {bdcScheduleStores.length} selected
                  </div>
                )}

                {/* Live progress while async BDC job is running. */}
                {bdcLoading && bdcJobStatus && bdcJobStatus.status !== 'completed' && (
                  <div style={{ marginTop: 12, padding: '8px 10px', borderRadius: 4,
                                background: '#FFF7ED', border: `1px solid ${C.amber}`,
                                fontSize: 10, color: C.text }}>
                    <div style={{ fontWeight: 700, marginBottom: 2 }}>
                      Job running — status: {bdcJobStatus.status}
                    </div>
                    <div style={{ color: C.textSub }}>{bdcJobStatus.progress || '…'}</div>
                  </div>
                )}

                {/* Completion confirmation — stays in modal so user sees the
                    summary and can close manually after download fired. */}
                {bdcJobResult && (
                  <div style={{ marginTop: 12, padding: '10px 12px', borderRadius: 4,
                                background: '#ECFDF5', border: '1px solid #10b981',
                                fontSize: 10, color: C.text }}>
                    <div style={{ fontWeight: 800, color: '#047857', marginBottom: 4 }}>
                      ✓ Job complete — ZIP downloaded
                    </div>
                    <div>Allocation: <b>{bdcJobResult.allocation_no}</b></div>
                    <div>RDCs: <b>{bdcJobResult.rdc_count}</b> file(s)</div>
                    <div>Rows: <b>{(bdcJobResult.row_count || 0).toLocaleString()}</b></div>
                    <div>Units: <b>{(bdcJobResult.total_qty || 0).toLocaleString()}</b></div>
                  </div>
                )}
              </div>
            </div>

            <div style={{ padding: '10px 16px', borderTop: `1px solid ${C.border}`,
                          display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button onClick={() => { setBdcModalOpen(false); setBdcJobResult(null); setBdcJobStatus(null) }}
                disabled={bdcLoading}
                style={{ ..._btn(), border: `1px solid ${C.border}` }}>
                {bdcJobResult ? 'Close' : 'Cancel'}
              </button>
              <button onClick={handleBdcGenerate} disabled={bdcLoading || bdcDateLoading}
                style={_btn('primary')}>
                <Download size={11} style={{ animation: bdcLoading ? 'spin 1s linear infinite' : 'none' }}/>
                {bdcLoading
                  ? (bdcJobStatus?.progress ? `${bdcJobStatus.progress}…` : 'Generating…')
                  : (bdcJobResult ? 'Generate again' : 'Generate BDC')}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Status tiles — sit along the user's mental workflow:
            Approved, not in BDC     → ready for next /bdc-generate
            In flight (BDC awaiting DO) → BDC sent, DO not yet received
            Closed (shipped)          → DO ≥ ALLOC
          Both "open" tiles are anchored on PEND_ALC (not BDC_HISTORY) so
          aging total = Approved-not-in-BDC + In flight is an invariant.
          Each tile is clickable to filter the detail grid + has a small
          Download icon for per-tile CSV export. */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 8, marginBottom: 8 }}>
        {summaryLoading ? (
          <div style={{ gridColumn: '1/-1', padding: 16, textAlign: 'center',
                        color: C.textMuted, fontSize: 11 }}>Loading…</div>
        ) : (
          <>
            <StatusTile color={C.red}    label="Approved, not in BDC"
              hint="Click to filter · approved units with no open BDC yet — what the next Generate BDC will stamp"
              {...(s?.by_status?.pending_bdc_generate
                  || s?.by_status?.awaiting_bdc
                  || { rows: 0, qty: 0 })}
              active={tileActive.pending_bdc}
              onClick={() => applyTileFilter('pending_bdc')}
              exporting={exporting === 'pending_bdc'}
              onExport={() => exportTile('pending_bdc')}/>
            <StatusTile color={C.amber}  label="In flight (BDC awaiting DO)"
              hint="Click to filter · BDC sent to SAP, DO not yet received in full"
              {...(s?.by_status?.pending_do_against_bdc
                  || s?.by_status?.awaiting_do
                  || { rows: 0, qty: 0 })}
              active={tileActive.pending_do}
              onClick={() => applyTileFilter('pending_do')}
              exporting={exporting === 'pending_do'}
              onExport={() => exportTile('pending_do')}/>
            <StatusTile color={C.green}  label="Closed (shipped)"
              hint="Click to filter · DO ≥ ALLOC — fully covered"
              {...(s?.by_status?.closed       || { rows: 0, qty: 0 })}
              active={tileActive.closed}
              onClick={() => applyTileFilter('closed')}
              exporting={exporting === 'closed'}
              onExport={() => exportTile('closed')}/>
          </>
        )}
      </div>

      {/* Aging tiles — clickable filters + per-tile export */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 8, marginBottom: 14 }}>
        {summaryLoading ? (
          <div style={{ gridColumn: '1/-1', padding: 20, textAlign: 'center', color: C.textMuted }}>
            Loading…
          </div>
        ) : (s?.by_aging || []).length ? (
          ['0-7d','8-30d','31-60d','60d+'].map(band => {
            const b = s.by_aging.find(x => x.aging_band === band) || { rows: 0, pend_qty: 0, alloc_qty: 0 }
            return <AgingTile key={band} band={band} {...b}
              active={tileActive.aging === band}
              onClick={() => applyAgingFilter(band)}
              exporting={exporting === 'aging:' + band}
              onExport={() => exportTile('aging:' + band)}/>
          })
        ) : (
          <div style={{ gridColumn: '1/-1', padding: 20, textAlign: 'center', color: C.textMuted }}>
            No open pending rows.
          </div>
        )}
      </div>

      {/* Mode + RDC breakdown */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 14 }}>

        {/* By Mode */}
        <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8, overflow: 'hidden' }}>
          <div style={{ padding: '8px 12px', borderBottom: `1px solid ${C.border}`,
                        fontSize: 10, fontWeight: 700, color: C.textSub, letterSpacing: '.05em' }}>
            BY MODE (open rows)
          </div>
          {summaryLoading ? (
            <div style={{ padding: 20, textAlign: 'center', color: C.textMuted }}>Loading…</div>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10 }}>
              <thead><tr style={{ background: C.bg }}>
                <TH>MODE</TH><TH right>ALLOC</TH><TH right>BDC</TH>
                <TH right>DO</TH><TH right>PEND</TH>
                <TH right>PEND BDC</TH>
                <TH right>ROWS</TH>
              </tr></thead>
              <tbody>
                {(s?.by_mode || []).map((r, i) => (
                  <tr key={r.mode} style={{ borderBottom: `1px solid ${C.border}`,
                                            background: i % 2 === 0 ? '#fff' : C.bg }}>
                    <td style={{ padding: '6px 10px' }}><ModeBadge value={r.mode}/></td>
                    <td style={{ padding: '6px 10px', textAlign: 'right' }}>{fmt(r.alloc_qty)}</td>
                    <td style={{ padding: '6px 10px', textAlign: 'right', color: C.blue }}>{fmt(r.bdc_qty)}</td>
                    <td style={{ padding: '6px 10px', textAlign: 'right', color: C.green }}>{fmt(r.do_qty)}</td>
                    <td style={{ padding: '6px 10px', textAlign: 'right', fontWeight: 700,
                                 color: r.pend_qty > 0 ? C.amber : C.green }}>{fmt(r.pend_qty)}</td>
                    <td style={{ padding: '6px 10px', textAlign: 'right',
                                 color: r.pending_bdc_qty > 0 ? C.red : C.textMuted,
                                 fontWeight: r.pending_bdc_qty > 0 ? 600 : 400 }}
                        title="Qty waiting to be sent to SAP — what the next Generate BDC will pick up">
                      {fmt(r.pending_bdc_qty)}
                    </td>
                    <td style={{ padding: '6px 10px', textAlign: 'right', color: C.textMuted }}>{r.rows}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* By RDC */}
        <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8, overflow: 'hidden' }}>
          <div style={{ padding: '8px 12px', borderBottom: `1px solid ${C.border}`,
                        fontSize: 10, fontWeight: 700, color: C.textSub, letterSpacing: '.05em' }}>
            BY RDC (open rows)
          </div>
          {summaryLoading ? (
            <div style={{ padding: 20, textAlign: 'center', color: C.textMuted }}>Loading…</div>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10 }}>
              <thead><tr style={{ background: C.bg }}>
                <TH>RDC</TH><TH right>ALLOC</TH><TH right>BDC</TH>
                <TH right>DO</TH><TH right>PEND</TH>
                <TH right>PEND BDC</TH>
                <TH right>ROWS</TH>
              </tr></thead>
              <tbody>
                {(s?.by_rdc || []).map((r, i) => (
                  <tr key={r.rdc} style={{ borderBottom: `1px solid ${C.border}`,
                                           background: i % 2 === 0 ? '#fff' : C.bg }}>
                    <td style={{ padding: '6px 10px', fontWeight: 600 }}>{r.rdc}</td>
                    <td style={{ padding: '6px 10px', textAlign: 'right' }}>{fmt(r.alloc_qty)}</td>
                    <td style={{ padding: '6px 10px', textAlign: 'right', color: C.blue }}>{fmt(r.bdc_qty)}</td>
                    <td style={{ padding: '6px 10px', textAlign: 'right', color: C.green }}>{fmt(r.do_qty)}</td>
                    <td style={{ padding: '6px 10px', textAlign: 'right', fontWeight: 700,
                                 color: r.pend_qty > 0 ? C.amber : C.green }}>{fmt(r.pend_qty)}</td>
                    <td style={{ padding: '6px 10px', textAlign: 'right',
                                 color: r.pending_bdc_qty > 0 ? C.red : C.textMuted,
                                 fontWeight: r.pending_bdc_qty > 0 ? 600 : 400 }}
                        title="Qty waiting to be sent to SAP — what the next Generate BDC will pick up">
                      {fmt(r.pending_bdc_qty)}
                    </td>
                    <td style={{ padding: '6px 10px', textAlign: 'right', color: C.textMuted }}>{r.rows}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {/* BY SESSION — open allocation sessions with BDC / DO / pending split.
          Lets ops see "how much pending from which session" at a glance and
          click through to filter the detail grid. Collapsible — click the
          header (or the chevron) to expand. */}
      <div style={{ background: C.card, border: `1px solid ${C.border}`,
                    borderRadius: 8, overflow: 'hidden', marginBottom: 14 }}>
        <div onClick={() => setSessionsCollapsed(v => !v)}
             role="button" tabIndex={0}
             onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') setSessionsCollapsed(v => !v) }}
             style={{ padding: '8px 12px',
                      borderBottom: sessionsCollapsed ? 'none' : `1px solid ${C.border}`,
                      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                      cursor: 'pointer', userSelect: 'none' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            {sessionsCollapsed
              ? <ChevronRight size={12} color={C.textSub}/>
              : <ChevronDown  size={12} color={C.textSub}/>}
            <div style={{ fontSize: 10, fontWeight: 700, color: C.textSub, letterSpacing: '.05em' }}>
              BY SESSION (open rows)
            </div>
          </div>
          <div style={{ fontSize: 9, color: C.textMuted }}>
            {sessionsLoading ? 'loading…' : `${sessions.length} session${sessions.length === 1 ? '' : 's'}`}
          </div>
        </div>
        {sessionsCollapsed ? null : sessionsLoading ? (
          <div style={{ padding: 20, textAlign: 'center', color: C.textMuted }}>Loading…</div>
        ) : sessions.length === 0 ? (
          <div style={{ padding: 20, textAlign: 'center', color: C.textMuted, fontSize: 10 }}>
            No open sessions.
          </div>
        ) : (
          <div style={{ maxHeight: 280, overflow: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10 }}>
              <thead><tr style={{ background: C.bg, position: 'sticky', top: 0 }}>
                <TH>SESSION_ID</TH>
                <TH>APPROVED</TH>
                <TH>SOURCE</TH>
                <TH right>ARTICLES</TH>
                <TH right>ALLOC</TH>
                <TH right>BDC (last)</TH>
                <TH right>BDC IN FLIGHT</TH>
                <TH right>DO</TH>
                <TH right>PEND</TH>
                <TH></TH>
              </tr></thead>
              <tbody>
                {sessions.map((r, i) => (
                  <tr key={`${r.session_id}|${r.source}`}
                      style={{ borderBottom: `1px solid ${C.border}`,
                               background: i % 2 === 0 ? '#fff' : C.bg }}>
                    <td style={{ padding: '6px 10px', fontFamily: 'monospace', fontSize: 9 }}>
                      {r.session_id}
                    </td>
                    <td style={{ padding: '6px 10px', color: C.textMuted, fontSize: 9 }}>
                      {r.approved_at ? r.approved_at.slice(0, 16).replace('T', ' ') : '—'}
                    </td>
                    <td style={{ padding: '6px 10px' }}>
                      <ModeBadge value={r.source}/>
                    </td>
                    <td style={{ padding: '6px 10px', textAlign: 'right', color: C.textMuted }}>
                      {fmt(r.article_count)}
                    </td>
                    <td style={{ padding: '6px 10px', textAlign: 'right' }}>{fmt(r.alloc_qty)}</td>
                    <td style={{ padding: '6px 10px', textAlign: 'right', color: C.blue }}>
                      {fmt(r.bdc_qty)}
                    </td>
                    <td style={{ padding: '6px 10px', textAlign: 'right',
                                 color: r.bdc_in_flight_qty > 0 ? C.amber : C.textMuted,
                                 fontWeight: r.bdc_in_flight_qty > 0 ? 600 : 400 }}
                        title="Open BDC qty attributed to this session — split evenly when same (RDC, ST_CD, ARTICLE) is in multiple open sessions">
                      {fmt(r.bdc_in_flight_qty)}
                    </td>
                    <td style={{ padding: '6px 10px', textAlign: 'right', color: C.green }}>
                      {fmt(r.do_qty)}
                    </td>
                    <td style={{ padding: '6px 10px', textAlign: 'right', fontWeight: 700,
                                 color: r.pend_qty > 0 ? C.amber : C.green }}>
                      {fmt(r.pend_qty)}
                    </td>
                    <td style={{ padding: '6px 10px', textAlign: 'right' }}>
                      <button
                        onClick={() => { setFSession(r.session_id); setGridBumpKey(k => k + 1) }}
                        style={{ fontSize: 8, padding: '2px 6px', border: `1px solid ${C.border}`,
                                 background: '#fff', color: C.primary, borderRadius: 3,
                                 cursor: 'pointer' }}>
                        Filter ↓
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Pending vs MSA — Gap report. Surfaces open pending qty whose
          RDC+article has NO MSA row (bot mis-allocated) or where the
          MSA pool (STK − HOLD) is smaller than the pending. These are
          the actionable candidates for an Adhoc Close, so the section
          lives right above the detail filters. */}
      <div style={{ background: C.card, border: `1px solid ${C.border}`,
                    borderRadius: 8, overflow: 'hidden', marginBottom: 14 }}>
        <div onClick={() => setGapCollapsed(v => !v)}
             role="button" tabIndex={0}
             onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') setGapCollapsed(v => !v) }}
             style={{ padding: '8px 12px',
                      borderBottom: gapCollapsed ? 'none' : `1px solid ${C.border}`,
                      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                      cursor: 'pointer', userSelect: 'none' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            {gapCollapsed
              ? <ChevronRight size={12} color={C.textSub}/>
              : <ChevronDown  size={12} color={C.textSub}/>}
            <AlertOctagon size={12} color={C.red}/>
            <div style={{ fontSize: 10, fontWeight: 700, color: C.textSub, letterSpacing: '.05em' }}>
              PENDING vs MSA — GAP REPORT
            </div>
            <div style={{ fontSize: 9, color: C.textMuted, marginLeft: 6 }}>
              open pending whose MSA stock is missing or smaller than the pending qty
            </div>
          </div>
          <div style={{ fontSize: 9, color: C.textMuted }}>
            {gapLoading
              ? 'loading…'
              : gapData
                ? `${gapData.summary.rows_total.toLocaleString()} row${gapData.summary.rows_total === 1 ? '' : 's'} · gap ${fmt(gapData.summary.gap_total)}`
                : '—'}
          </div>
        </div>

        {gapCollapsed ? null : (
          <div style={{ padding: 12 }}>
            {/* Summary tiles. Clicking a tile toggles its status filter. */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)',
                          gap: 8, marginBottom: 10 }}>
              <StatusTile color={C.red} label="TOTAL GAP"
                hint="Open pending qty not covered by current MSA stock"
                rows={gapData?.summary.rows_total || 0}
                qty={gapData?.summary.gap_total || 0}
                active={gapStatusFilter === ''}
                onClick={() => { setGapStatusFilter(''); setGapPage(1) }}/>
              <StatusTile color={C.amber} label="NO MSA"
                hint="Click to filter · article has no MSA row for that RDC (bot mis-allocated)"
                rows={gapData?.summary.rows_no_msa || 0}
                qty={gapData?.summary.gap_no_msa || 0}
                active={gapStatusFilter === 'NO_MSA'}
                onClick={() => { setGapStatusFilter(s => s === 'NO_MSA' ? '' : 'NO_MSA'); setGapPage(1) }}/>
              <StatusTile color={C.blue} label="SHORT (MSA < PEND)"
                hint="Click to filter · MSA stock exists but is less than the pending qty"
                rows={gapData?.summary.rows_short || 0}
                qty={gapData?.summary.gap_short || 0}
                active={gapStatusFilter === 'SHORT'}
                onClick={() => { setGapStatusFilter(s => s === 'SHORT' ? '' : 'SHORT'); setGapPage(1) }}/>
            </div>

            {/* Action row */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              <div style={{ fontSize: 9, color: C.textMuted }}>
                Honours page-level RDC / MAJ_CAT filters · sorted by{' '}
                <b>{gapSortBy.toUpperCase()}</b> {gapSortDir}
              </div>
              <div style={{ flex: 1 }}/>
              <button style={_btn()} onClick={loadGap} disabled={gapLoading}>
                <RefreshCw size={10}
                  style={{ animation: gapLoading ? 'spin 1s linear infinite' : 'none' }}/>
                Refresh
              </button>
              <button onClick={exportGap} disabled={gapExporting || gapLoading}
                style={_btn('primary')}>
                <Download size={10}/>
                {gapExporting ? 'Exporting…' : 'Export CSV'}
              </button>
            </div>

            {/* Table */}
            {gapLoading && !gapData ? (
              <div style={{ padding: 20, textAlign: 'center', color: C.textMuted, fontSize: 11 }}>
                Loading gap report…
              </div>
            ) : !gapData || gapData.rows.length === 0 ? (
              <div style={{ padding: 20, textAlign: 'center', fontSize: 11,
                            background: C.green + '12', border: `1px solid ${C.green}40`,
                            borderRadius: 4, color: C.green, fontWeight: 600 }}>
                {gapData && gapData.summary.msa_available === false
                  ? 'ARS_MSA_TOTAL not found — cannot compute MSA-side stock.'
                  : 'No gap. Every open pending qty is fully covered by MSA stock.'}
              </div>
            ) : (
              <>
                <div style={{ maxHeight: 420, overflow: 'auto',
                              border: `1px solid ${C.border}`, borderRadius: 4 }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10 }}>
                    <thead><tr style={{ background: C.bg, position: 'sticky', top: 0 }}>
                      {[
                        ['rdc',            'RDC',         false],
                        ['article_number', 'ARTICLE',     false],
                        ['maj_cat',        'MAJ_CAT',     false],
                        [null,             'CLR',         false],
                        [null,             'STATUS',      false],
                        ['pend_qty',       'PEND',        true],
                        [null,             'STK',         true],
                        [null,             'HOLD',        true],
                        ['available',      'AVAILABLE',   true],
                        ['gap',            'GAP',         true],
                      ].map(([key, label, right], i) => (
                        <th key={i}
                            onClick={key ? () => toggleGapSort(key) : undefined}
                            style={{ padding: '7px 10px',
                                     textAlign: right ? 'right' : 'left',
                                     fontSize: 9, fontWeight: 700, color: C.textSub,
                                     letterSpacing: '.05em', whiteSpace: 'nowrap',
                                     borderBottom: `1px solid ${C.border}`,
                                     cursor: key ? 'pointer' : 'default',
                                     userSelect: 'none' }}>
                          {label}
                          {key && gapSortBy === key && (
                            <span style={{ marginLeft: 4, color: C.primary }}>
                              {gapSortDir === 'asc' ? '↑' : '↓'}
                            </span>
                          )}
                        </th>
                      ))}
                    </tr></thead>
                    <tbody>
                      {gapData.rows.map((r, i) => (
                        <tr key={`${r.rdc}|${r.article_number}`}
                            style={{ borderBottom: `1px solid ${C.border}`,
                                     background: i % 2 === 0 ? '#fff' : C.bg }}>
                          <td style={{ padding: '5px 10px', fontWeight: 600 }}>{r.rdc}</td>
                          <td style={{ padding: '5px 10px', fontFamily: 'monospace', fontSize: 9 }}>
                            {r.article_number}
                          </td>
                          <td style={{ padding: '5px 10px', color: C.textSub }}>{r.maj_cat || '—'}</td>
                          <td style={{ padding: '5px 10px', color: C.textSub }}>{r.clr || '—'}</td>
                          <td style={{ padding: '5px 10px' }}>
                            <span style={{ fontSize: 8, fontWeight: 700, padding: '2px 6px',
                                           borderRadius: 3,
                                           background: r.status === 'NO_MSA' ? C.amber + '22' : C.blue + '22',
                                           color:      r.status === 'NO_MSA' ? C.amber : C.blue }}>
                              {r.status}
                            </span>
                          </td>
                          <td style={{ padding: '5px 10px', textAlign: 'right', fontWeight: 600 }}>
                            {fmt(r.pend_qty)}
                          </td>
                          <td style={{ padding: '5px 10px', textAlign: 'right', color: C.textSub }}>
                            {fmt(r.stk_qty)}
                          </td>
                          <td style={{ padding: '5px 10px', textAlign: 'right', color: C.textMuted }}>
                            {fmt(r.hold_qty)}
                          </td>
                          <td style={{ padding: '5px 10px', textAlign: 'right' }}>
                            {fmt(r.available)}
                          </td>
                          <td style={{ padding: '5px 10px', textAlign: 'right',
                                       fontWeight: 800, color: C.red }}>
                            {fmt(r.gap)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {/* Paging */}
                {gapData.total > gapPageSize && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8,
                                marginTop: 8, fontSize: 10, color: C.textSub }}>
                    <span>
                      Page {gapPage} of {Math.ceil(gapData.total / gapPageSize)} ·{' '}
                      {gapData.total.toLocaleString()} rows total
                    </span>
                    <div style={{ flex: 1 }}/>
                    <button style={_btn()}
                      onClick={() => setGapPage(p => Math.max(1, p - 1))}
                      disabled={gapPage <= 1 || gapLoading}>Prev</button>
                    <button style={_btn()}
                      onClick={() => setGapPage(p => p + 1)}
                      disabled={gapPage * gapPageSize >= gapData.total || gapLoading}>Next</button>
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </div>

      {/* Filters */}
      <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8,
                    padding: '10px 12px', marginBottom: 10 }}>
        <div style={{ fontSize: 9, fontWeight: 700, color: C.textSub,
                      letterSpacing: '.05em', marginBottom: 8 }}>FILTERS</div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <label style={{ fontSize: 9, color: C.textMuted }}>From</label>
            <input type="date" value={fDateFrom} onChange={e => setFDateFrom(e.target.value)}
              style={{ ...inp, fontSize: 9 }}/>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <label style={{ fontSize: 9, color: C.textMuted }}>To</label>
            <input type="date" value={fDateTo} onChange={e => setFDateTo(e.target.value)}
              style={{ ...inp, fontSize: 9 }}/>
          </div>
          <input value={fRdc} onChange={e => setFRdc(e.target.value)}
            placeholder="RDC…" style={{ ...inp, width: 80 }}/>
          <input value={fMajCat} onChange={e => setFMajCat(e.target.value)}
            placeholder="MAJ_CAT…" style={{ ...inp, width: 100 }}/>
          <select value={fMode} onChange={e => setFMode(e.target.value)} style={inp}>
            <option value="">All Modes</option>
            <option value="AUTO">AUTO</option>
            <option value="MANUAL">MANUAL</option>
          </select>
          <select value={fClosed} onChange={e => setFClosed(e.target.value)} style={inp}>
            <option value="open">Open only</option>
            <option value="closed">Closed only</option>
            <option value="all">All rows</option>
          </select>
          <input value={fSession} onChange={e => setFSession(e.target.value)}
            placeholder="Session ID…" style={{ ...inp, width: 160 }}/>
          <button style={_btn()}
            onClick={() => setGridBumpKey(k => k + 1)}>
            <RefreshCw size={10}/> Apply
          </button>
          {/* Export current filtered view to CSV — same filters the grid
              is using right now, but uncapped (no page limit). */}
          <button onClick={exportFiltered} disabled={exporting === 'filtered'}
            title="Export the current filtered detail rows to CSV"
            style={_btn('primary')}>
            <Download size={10}/>
            {exporting === 'filtered' ? 'Exporting…' : 'Export CSV'}
          </button>
        </div>
      </div>

      {/* Detail grid — paged + sortable + per-column filter */}
      <DataGrid
        fetcher={fetchReco}
        refreshKey={`${recoRefreshKey}|${gridBumpKey}`}
        defaultPageSize={100}
        defaultSortBy="approved_at"
        defaultSortDir="desc"
        compact
        emptyText="No rows match the current filters."
        columns={[
          { key:'rdc', label:'RDC', sortable:true, filterType:'multi',
            filterOptions:['DH24','DH26','DW01'],
            render:r => <span style={{fontWeight:600}}>{r.rdc}</span> },
          { key:'st_cd', label:'ST_CD', sortable:true, filterType:'text',
            suggester: q => suggestCol('st_cd', q),
            render:r => <span style={{color:C.textSub}}>{r.st_cd || '—'}</span> },
          { key:'article_number', label:'ARTICLE', sortable:true, filterType:'text',
            suggester: q => suggestCol('article_number', q),
            render:r => <span style={{fontFamily:'monospace', fontSize:9}}>{r.article_number}</span> },
          { key:'maj_cat', label:'MAJ_CAT', sortable:true, filterType:'text',
            suggester: q => suggestCol('maj_cat', q),
            render:r => r.maj_cat || '—' },
          { key:'clr', label:'CLR', sortable:true, filterType:'text',
            suggester: q => suggestCol('clr', q),
            render:r => <span style={{color:C.textSub}}>{r.clr || '—'}</span> },
          { key:'alloc_mode', label:'MODE', sortable:true, filterType:'multi',
            filterOptions:['AUTO','MANUAL','RL','TBL','NL'],
            render:r => <ModeBadge value={r.alloc_mode}/> },
          { key:'alloc_qty', label:'ALLOC', sortable:true, align:'right',
            render:r => fmt(r.alloc_qty) },
          { key:'bdc_qty', label:'BDC', sortable:true, align:'right',
            render:r => <span style={{color:C.blue}}>{fmt(r.bdc_qty)}</span> },
          { key:'bdc_unconfirmed', label:'BDC UNCONF', sortable:true, align:'right',
            render:r => r.bdc_unconfirmed > 0
              ? <span style={{display:'inline-flex', alignItems:'center', gap:3, color:C.amber}}>
                  <AlertTriangle size={9}/>{fmt(r.bdc_unconfirmed)}
                </span>
              : <span style={{color:C.textMuted}}>—</span> },
          { key:'do_qty', label:'DO', sortable:true, align:'right',
            render:r => <span style={{color:C.green}}>{fmt(r.do_qty)}</span> },
          { key:'pend_qty', label:'PEND', sortable:true, align:'right',
            render:r => <span style={{fontWeight:700, color:r.pend_qty>0?C.amber:C.green}}>
              {fmt(r.pend_qty)}</span> },
          { key:'bdc_alloc_no', label:'BDC ALLOC #', sortable:true, filterType:'text',
            suggester: q => suggestCol('bdc_alloc_no', q),
            render:r => r.bdc_alloc_no
              ? <span style={{fontFamily:'monospace', fontSize:9, color:C.primary}}>{r.bdc_alloc_no}</span>
              : <span style={{color:C.textMuted}}>—</span> },
          { key:'bdc_status', label:'BDC STATUS', sortable:true, filterType:'multi',
            filterOptions:['NEVER_SENT','OPEN','PARTIAL','CLOSED_PARTIAL','CONFIRMED','CANCELLED'],
            render:r => <BdcStatusBadge value={r.bdc_status}/> },
          { key:'do_received', label:'DO RECVD', sortable:true, align:'right',
            render:r => r.do_received != null
              ? <span style={{color:C.green}}>{fmt(r.do_received)}</span>
              : <span style={{color:C.textMuted}}>—</span> },
          { key:'aging_band', label:'AGING', sortable:true, filterType:'multi',
            filterOptions:['0-7d','8-30d','31-60d','60d+'],
            render:r => {
              const accent = AGING_ACCENT[r.aging_band] || C.textSub
              return <span style={{fontSize:8, fontWeight:700, padding:'2px 5px',
                                   borderRadius:3, background:accent+'22', color:accent}}>
                {r.aging_band} ({r.aging_days}d)</span>
            }},
          { key:'do_number', label:'DO NUMBER', sortable:true, filterType:'text',
            suggester: q => suggestCol('do_number', q),
            render:r => <span style={{fontFamily:'monospace', fontSize:9, color:C.textSub}}>
              {r.do_number || '—'}</span> },
          { key:'approved_at', label:'APPROVED', sortable:true,
            render:r => <span style={{fontSize:9, color:C.textMuted}}>
              {r.approved_at ? r.approved_at.slice(0,16).replace('T',' ') : '—'}</span> },
          { key:'is_closed', label:'STATUS', sortable:true,
            render:r => <StatusBadge closed={r.is_closed}/> },
        ]}
      />

      <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
    </div>
  )
}
