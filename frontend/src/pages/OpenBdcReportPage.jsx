/**
 * OpenBdcReportPage — pending BDCs generated but still open.
 *
 * Reports against ARS_BDC_HISTORY. Default view: STATUS='OPEN' (BDC sent
 * to SAP, no DO received yet — what's in flight).
 *
 * Features:
 *   - Filter by status, allocation_no, RDC, ST_CD, ARTICLE, date range
 *   - Per-allocation "Re-download SAP file" button (the original 9-col Excel)
 *   - "Export current view to Excel" — full BDC history rows with DAYS_OPEN
 *
 * Defaults to the highest-value bucket (OPEN) but the user can switch the
 * status pill to inspect CLOSED_PARTIAL / CONFIRMED / CANCELLED too.
 */
import { useState, useEffect, useCallback, useMemo } from 'react'
import { pendAlcAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  AlertTriangle, RefreshCw, Download, FileDown, Filter, Search,
} from 'lucide-react'

const C = {
  primary: '#4f46e5', blue: '#0891b2', green: '#16a34a',
  amber: '#d97706', red: '#dc2626', text: '#1e293b',
  textSub: '#64748b', textMuted: '#94a3b8', border: '#e2e8f0',
  bg: '#f8fafc', card: '#ffffff',
}

const STATUS_OPTIONS = [
  { value: 'OPEN',           label: 'OPEN',           color: C.amber },
  { value: 'CLOSED_PARTIAL', label: 'CLOSED PARTIAL', color: '#b45309' },
  { value: 'CONFIRMED',      label: 'CONFIRMED',      color: C.green },
  { value: 'CANCELLED',      label: 'CANCELLED',      color: C.red },
  { value: '',               label: 'ALL',            color: C.textSub },
]

const fmt   = (n) => Number.isFinite(+n) ? Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 }) : '—'
const fmtDt = (s) => s ? s.slice(0, 16).replace('T', ' ') : '—'

function downloadBlob(blob, fname) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = fname
  document.body.appendChild(a); a.click(); a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export default function OpenBdcReportPage() {
  const [rows, setRows]       = useState([])
  const [allocations, setAllocations] = useState([])     // ALL allocations matching the filter (uncapped)
  const [loading, setLoading] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [redownloading, setRedownloading] = useState({})  // alloc_no → bool

  // Filters
  const [status,     setStatus]     = useState('OPEN')
  const [allocNo,    setAllocNo]    = useState('')
  const [rdc,        setRdc]        = useState('')
  const [stCd,       setStCd]       = useState('')
  const [article,    setArticle]    = useState('')
  const [dateFrom,   setDateFrom]   = useState('')
  const [dateTo,     setDateTo]     = useState('')

  const params = useMemo(() => {
    // Bumped from 5,000 → 20,000 (endpoint max). With 3+ open allocations
    // totalling 340k+ rows on production, 5k clipped the second + third
    // alloc out of view entirely. 20k now covers the typical day; export
    // covers the rest.
    const p = { limit: 20000 }
    if (status)   p.status        = status
    if (allocNo)  p.allocation_no = allocNo.trim()
    if (rdc)      p.rdc           = rdc.trim()
    if (stCd)     p.st_cd         = stCd.trim()
    if (article)  p.article       = article.trim()
    return p
  }, [status, allocNo, rdc, stCd, article])

  // Allocations list params — independent of the detail-row params.
  const allocParams = useMemo(() => {
    const p = {}
    if (status)   p.status    = status
    if (rdc)      p.rdc       = rdc.trim()
    if (dateFrom) p.date_from = dateFrom
    if (dateTo)   p.date_to   = dateTo
    return p
  }, [status, rdc, dateFrom, dateTo])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [rowsResp, allocResp] = await Promise.all([
        pendAlcAPI.bdcHistory(params),
        pendAlcAPI.bdcHistoryAllocations(allocParams),
      ])
      let arr = rowsResp.data?.data || []
      // Client-side date filtering on the row list (backend /bdc-history
      // doesn't yet take a date range — keeps that endpoint simple).
      if (dateFrom) arr = arr.filter(r => (r.bdc_date || '') >= dateFrom)
      if (dateTo)   arr = arr.filter(r => (r.bdc_date || '') <= dateTo + 'T23:59:59')
      setRows(arr)
      setAllocations(allocResp.data?.data || [])
    } catch {
      toast.error('Failed to load BDC history')
    } finally { setLoading(false) }
  }, [params, allocParams, dateFrom, dateTo])

  useEffect(() => { load() }, [load])

  // Stats from the UNCAPPED allocations list — accurate totals even when
  // the detail table is row-limited. Falls back to in-view sums if the
  // allocations endpoint returned nothing.
  const stats = useMemo(() => {
    if (allocations.length > 0) {
      let lines = 0, bdcSum = 0, doSum = 0, shortSum = 0
      for (const a of allocations) {
        lines    += +a.lines     || 0
        bdcSum   += +a.bdc_qty   || 0
        doSum    += +a.do_qty    || 0
        shortSum += +a.short_qty || 0
      }
      return { rowCount: lines, bdcSum, doSum, shortSum, allocCount: allocations.length }
    }
    let rowCount = rows.length, bdcSum = 0, doSum = 0, shortSum = 0
    const set = new Set()
    for (const r of rows) {
      bdcSum   += +r.bdc_qty     || 0
      doSum    += +r.do_received || 0
      shortSum += +r.short_qty   || 0
      if (r.allocation_number) set.add(r.allocation_number)
    }
    return { rowCount, bdcSum, doSum, shortSum, allocCount: set.size }
  }, [allocations, rows])

  // Per-allocation chips for the re-download strip — sourced from the
  // uncapped allocations list so a row-limited table can't hide an
  // allocation behind the cap.
  const distinctAllocs = useMemo(() => allocations.map(a => ({
    allocation_number: a.allocation_number,
    lines:             a.lines,
    bdc_sum:           a.bdc_qty,
    date:              a.last_date || a.first_date,
  })), [allocations])

  const handleExport = async () => {
    setExporting(true)
    try {
      const exportParams = { ...params }
      if (dateFrom) exportParams.date_from = dateFrom
      if (dateTo)   exportParams.date_to   = dateTo
      delete exportParams.limit
      const { data: blob } = await pendAlcAPI.bdcHistoryExport(exportParams)
      const tag = (status || 'ALL').toUpperCase()
      const today = new Date().toISOString().slice(0, 10).replace(/-/g, '')
      downloadBlob(blob, `BDC_HISTORY_${tag}_${today}.csv`)
      toast.success('CSV downloaded')
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Export failed')
    } finally { setExporting(false) }
  }

  const handleRedownload = async (alloc) => {
    setRedownloading(prev => ({ ...prev, [alloc]: true }))
    try {
      const { data: blob } = await pendAlcAPI.bdcHistoryRedownload(alloc)
      downloadBlob(blob, `ARS_BDC_${alloc}_redownload.xlsx`)
      toast.success(`Re-downloaded ${alloc}`)
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Re-download failed')
    } finally {
      setRedownloading(prev => { const n = { ...prev }; delete n[alloc]; return n })
    }
  }

  const clearFilters = () => {
    setStatus('OPEN'); setAllocNo(''); setRdc(''); setStCd('')
    setArticle(''); setDateFrom(''); setDateTo('')
  }

  return (
    <div style={{ padding: '16px 20px', fontFamily: 'Inter,system-ui,sans-serif',
                  fontSize: 11, color: C.text, background: C.bg, minHeight: '100vh' }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <AlertTriangle size={16} color={C.amber}/>
        <div>
          <div style={{ fontSize: 13, fontWeight: 800 }}>Open BDC Report</div>
          <div style={{ fontSize: 10, color: C.textMuted }}>
            BDCs generated but still awaiting DO. Default view shows STATUS=OPEN
            (what's in flight to SAP). Re-download any allocation's original SAP file.
          </div>
        </div>
        <div style={{ flex: 1 }}/>
        <button onClick={load} disabled={loading}
          style={btn(C.border, '#fff', C.textSub)}>
          <RefreshCw size={11} style={{ animation: loading ? 'spin 1s linear infinite' : 'none' }}/>
          Refresh
        </button>
        <button onClick={handleExport} disabled={exporting || rows.length === 0}
          style={btn('none', C.primary, '#fff', true)}>
          <FileDown size={11}/>
          {exporting ? 'Exporting…' : 'Export to CSV'}
        </button>
      </div>

      {/* Stats strip */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5,1fr)', gap: 8, marginBottom: 12 }}>
        <StatTile color={C.amber}   label="Open BDC rows"     value={fmt(stats.rowCount)}/>
        <StatTile color={C.primary} label="Allocations"        value={fmt(stats.allocCount)}/>
        <StatTile color={C.blue}    label="BDC qty (sent)"     value={fmt(stats.bdcSum)}/>
        <StatTile color={C.green}   label="DO received"        value={fmt(stats.doSum)}/>
        <StatTile color={C.red}     label="Short qty (BDC−DO)" value={fmt(stats.shortSum)}/>
      </div>

      {/* Filters */}
      <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8,
                    padding: 10, marginBottom: 10, display: 'flex', gap: 8,
                    alignItems: 'center', flexWrap: 'wrap' }}>
        <Filter size={12} color={C.textMuted}/>
        <span style={{ fontSize: 10, fontWeight: 600, color: C.textSub }}>Status:</span>
        {STATUS_OPTIONS.map(o => (
          <button key={o.value || 'all'} onClick={() => setStatus(o.value)}
            style={{
              fontSize: 10, padding: '4px 10px', borderRadius: 4, cursor: 'pointer',
              border: `1px solid ${status === o.value ? o.color : C.border}`,
              background: status === o.value ? o.color : '#fff',
              color: status === o.value ? '#fff' : C.textSub, fontWeight: 600,
            }}>
            {o.label}
          </button>
        ))}

        <div style={{ width: 1, height: 18, background: C.border, margin: '0 4px' }}/>

        <input value={allocNo} onChange={e => setAllocNo(e.target.value)}
          placeholder="Allocation #" style={{ ...inp, width: 130 }}/>
        <input value={rdc}     onChange={e => setRdc(e.target.value)}
          placeholder="RDC"     style={{ ...inp, width: 80 }}/>
        <input value={stCd}    onChange={e => setStCd(e.target.value)}
          placeholder="ST_CD"   style={{ ...inp, width: 80 }}/>
        <input value={article} onChange={e => setArticle(e.target.value)}
          placeholder="Article" style={{ ...inp, width: 130 }}/>
        <label style={{ fontSize: 10, color: C.textMuted }}>From</label>
        <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)} style={inp}/>
        <label style={{ fontSize: 10, color: C.textMuted }}>To</label>
        <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)} style={inp}/>

        <div style={{ flex: 1 }}/>
        <button onClick={clearFilters}
          style={{ fontSize: 10, padding: '4px 10px', border: `1px solid ${C.border}`,
                   background: '#fff', color: C.textSub, borderRadius: 4, cursor: 'pointer' }}>
          Clear
        </button>
        <button onClick={load} disabled={loading} style={btn(C.primary, C.primary, '#fff')}>
          <Search size={11}/> Apply
        </button>
      </div>

      {/* Re-download strip (per allocation_number in current view) */}
      {distinctAllocs.length > 0 && (
        <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8,
                      padding: 10, marginBottom: 10 }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: C.textSub,
                        letterSpacing: '.05em', marginBottom: 6 }}>
            RE-DOWNLOAD ORIGINAL SAP FILE ({distinctAllocs.length})
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {distinctAllocs.map(a => (
              <button key={a.allocation_number}
                onClick={() => handleRedownload(a.allocation_number)}
                disabled={!!redownloading[a.allocation_number]}
                title={`Re-download SAP-ready 9-column Excel for ${a.allocation_number}`}
                style={{ fontSize: 10, padding: '5px 10px', borderRadius: 4,
                         border: `1px solid ${C.primary}`, background: '#fff', color: C.primary,
                         cursor: redownloading[a.allocation_number] ? 'wait' : 'pointer',
                         display: 'flex', alignItems: 'center', gap: 5, fontWeight: 600 }}>
                <Download size={10} style={{ animation: redownloading[a.allocation_number] ? 'spin 1s linear infinite' : 'none' }}/>
                {a.allocation_number}
                <span style={{ color: C.textMuted, fontWeight: 400 }}>
                  · {fmt(a.lines)} lines · {fmt(a.bdc_sum)} units
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Table */}
      <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8,
                    overflow: 'hidden' }}>
        <div style={{ padding: '8px 12px', borderBottom: `1px solid ${C.border}`,
                      fontSize: 10, fontWeight: 700, color: C.textSub, letterSpacing: '.05em',
                      display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span>BDC HISTORY · {status || 'ALL'} · showing {rows.length.toLocaleString()} of {stats.rowCount.toLocaleString()} row(s)</span>
          <span style={{ fontSize: 9, color: C.textMuted, fontWeight: 400 }}>
            Table capped at 20,000 rows — use CSV export above for the full {stats.rowCount.toLocaleString()}
          </span>
        </div>
        {loading ? (
          <div style={{ padding: 30, textAlign: 'center', color: C.textMuted }}>Loading…</div>
        ) : rows.length === 0 ? (
          <div style={{ padding: 30, textAlign: 'center', color: C.textMuted }}>
            No rows match the current filter.
          </div>
        ) : (
          <div style={{ maxHeight: 520, overflow: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10 }}>
              <thead><tr style={{ background: C.bg, position: 'sticky', top: 0, zIndex: 1 }}>
                <TH>BDC DATE</TH>
                <TH>ALLOCATION #</TH>
                <TH>RDC</TH>
                <TH>ST_CD</TH>
                <TH>ARTICLE</TH>
                <TH>MAJ_CAT</TH>
                <TH right>BDC QTY</TH>
                <TH right>DO RECVD</TH>
                <TH right>SHORT</TH>
                <TH>STATUS</TH>
                <TH right>DAYS OPEN</TH>
                <TH>LAST DO AT</TH>
                <TH>BY</TH>
              </tr></thead>
              <tbody>
                {rows.map((r, i) => {
                  const daysOpen = r.bdc_date
                    ? Math.floor((Date.now() - new Date(r.bdc_date).getTime()) / 86400000)
                    : null
                  return (
                    <tr key={r.id} style={{ borderBottom: `1px solid ${C.border}`,
                                            background: i % 2 === 0 ? '#fff' : C.bg }}>
                      <td style={td}>{fmtDt(r.bdc_date)}</td>
                      <td style={{ ...td, fontFamily: 'monospace', fontSize: 9 }}>{r.allocation_number}</td>
                      <td style={{ ...td, fontWeight: 600 }}>{r.rdc}</td>
                      <td style={td}>{r.st_cd || '—'}</td>
                      <td style={{ ...td, fontFamily: 'monospace', fontSize: 9 }}>{r.article_number}</td>
                      <td style={td}>{r.maj_cat || '—'}</td>
                      <td style={{ ...td, textAlign: 'right', color: C.blue, fontWeight: 600 }}>{fmt(r.bdc_qty)}</td>
                      <td style={{ ...td, textAlign: 'right', color: C.green }}>{fmt(r.do_received)}</td>
                      <td style={{ ...td, textAlign: 'right', color: r.short_qty > 0 ? C.red : C.textMuted, fontWeight: r.short_qty > 0 ? 600 : 400 }}>
                        {fmt(r.short_qty)}
                      </td>
                      <td style={td}><StatusBadge value={r.status}/></td>
                      <td style={{ ...td, textAlign: 'right',
                                   color: daysOpen != null && daysOpen > 7 ? C.red : C.textMuted,
                                   fontWeight: daysOpen != null && daysOpen > 7 ? 600 : 400 }}>
                        {daysOpen != null ? daysOpen : '—'}
                      </td>
                      <td style={td}>{fmtDt(r.last_do_at)}</td>
                      <td style={td}>{r.created_by || '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

function StatTile({ color, label, value }) {
  return (
    <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8,
                  padding: '10px 12px', borderTop: `3px solid ${color}` }}>
      <div style={{ fontSize: 9, fontWeight: 700, color, letterSpacing: '.06em',
                    marginBottom: 3 }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 800, color: C.text, lineHeight: 1 }}>{value}</div>
    </div>
  )
}

function StatusBadge({ value }) {
  const map = {
    OPEN:           { bg: '#fef3c7', fg: C.amber,    label: 'OPEN' },
    PARTIAL:        { bg: '#dbeafe', fg: C.blue,     label: 'PARTIAL' },
    CLOSED_PARTIAL: { bg: '#fde68a', fg: '#b45309',  label: 'CLOSED PARTIAL' },
    CONFIRMED:      { bg: '#dcfce7', fg: C.green,    label: 'CONFIRMED' },
    CANCELLED:      { bg: '#fee2e2', fg: C.red,      label: 'CANCELLED' },
  }
  const s = map[value] || { bg: '#f1f5f9', fg: C.textSub, label: value || '—' }
  return (
    <span style={{ fontSize: 8, fontWeight: 700, padding: '2px 6px', borderRadius: 3,
                   background: s.bg, color: s.fg, whiteSpace: 'nowrap' }}>
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

const inp = { fontSize: 10, padding: '4px 8px', border: `1px solid ${C.border}`, borderRadius: 4 }
const td  = { padding: '5px 10px', whiteSpace: 'nowrap' }
const btn = (borderColor, bg, color, _strong = false) => ({
  fontSize: 10, padding: '5px 10px', border: `1px solid ${borderColor}`,
  background: bg, color, borderRadius: 4, cursor: 'pointer',
  display: 'flex', alignItems: 'center', gap: 5, fontWeight: 600,
})
