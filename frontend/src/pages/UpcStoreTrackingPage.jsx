/**
 * UpcStoreTrackingPage — store-opening lifecycle tracker.
 *
 * The dynamic replacement for the manual "STORE OPENING DATES *.xlsx" workbook.
 * You upload only ST_CD + proposed opening date + share date; identity is
 * joined live from the store master and metrics (MBQ / stock / SLOC-wise /
 * fill-rate) from the latest TREND_ST row. Date & remark changes are tracked
 * as event history — counts, first/last, and delays are all derived.
 * See services/upc_store_track_service.py + manual/upc_tracking.md.
 */
import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { upcTrackAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  RefreshCw, Upload, Plus, Download, Search, Trash2, Pencil, Eye,
  CalendarClock, AlertTriangle, CheckCircle2, X, Store, History, Clock, FileDown,
  Maximize2, BarChart3, Table as TableIcon, Columns, HelpCircle,
} from 'lucide-react'
import {
  BarChart, Bar, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Cell, LabelList,
} from 'recharts'
import { C } from '@/theme/colors'

const fmtN = (n) => (n == null ? '—' : Number(n).toLocaleString('en-IN'))
// treat NaN-like junk from old uploads as empty
const cleanTxt = (s) => {
  if (s == null) return ''
  const t = String(s).trim()
  return ['nan','none','nat'].includes(t.toLowerCase()) ? '' : t
}
const fmtD = (iso) => {
  if (!iso) return '—'
  const d = new Date(iso)
  return `${String(d.getDate()).padStart(2,'0')} ${d.toLocaleString('en',{month:'short'})} ${String(d.getFullYear()).slice(2)}`
}
// date + time, for event timestamps (who/when audit)
const fmtDT = (iso) => {
  if (!iso) return '—'
  const d = new Date(iso)
  const t = `${d.getHours()%12||12}:${String(d.getMinutes()).padStart(2,'0')}${d.getHours()>=12?'pm':'am'}`
  return `${String(d.getDate()).padStart(2,'0')} ${d.toLocaleString('en',{month:'short'})} ${String(d.getFullYear()).slice(2)}, ${t}`
}
// opening month derived from the (latest budgeted) proposed date
const monthLabel = (iso) => {
  if (!iso) return '—'
  const d = new Date(iso)
  return `${d.toLocaleString('en',{month:'short'})} ${String(d.getFullYear()).slice(2)}`
}
const monthKey = (iso) => (iso ? iso.slice(0,7) : '')   // YYYY-MM for grouping/filter

// lifecycle status → label + pill tone
const STATUS_META = {
  ACTIVE:    { label: 'Active',        tone: 'info'  },
  OPENED:    { label: 'Opened',        tone: 'ok'    },
  HOLD:      { label: 'Hold',          tone: 'warn'  },
  CANCELLED: { label: 'Reject/Cancel', tone: 'error' },
}
const STATUS_ORDER = ['ACTIVE', 'OPENED', 'HOLD', 'CANCELLED']

// ── day math for Bal Days (dispatch/opening) + replenishment-window column ──
const DAYMS = 86400000
const _msOf = (iso) => (iso ? Date.parse(String(iso).slice(0, 10)) : null)
const _diffDays = (a, b) => (a == null || b == null) ? null : Math.round((a - b) / DAYMS)
// dispatch date = opening (latest proposed) − lead days; replenishment window =
// days from each milestone (1st share / layout / display) to the dispatch date.
function calcDays(r, lead, todayMs) {
  const lp = _msOf(r.latest_proposed_dt)
  const dispatch = lp == null ? null : lp - lead * DAYMS
  return {
    _dispatchISO:    dispatch == null ? null : new Date(dispatch).toISOString().slice(0, 10),
    _balOpen:        _diffDays(lp, todayMs),        // days to opening
    _balDispatch:    _diffDays(dispatch, todayMs),  // days to last-dispatch date
    _replShare:      _diffDays(dispatch, _msOf(r.first_share_dt)),   // from 1st-ever share
    _replLatestShare:_diffDays(dispatch, _msOf(r.latest_share_dt)),  // from current date's share
    _replLayout:     _diffDays(dispatch, _msOf(r.layout_rec_dt)),
    _replDisplay:    _diffDays(dispatch, _msOf(r.first_disp_dt)),
    _dgap:           _diffDays(lp, _msOf(r.prev_proposed_dt)),  // latest − last-given proposed
  }
}
const _n = (v) => (v == null ? '' : v)

// formatted value per column for the "current view" CSV export
const _d = (iso) => (iso ? fmtD(iso) : '')
const VIEW_CSV = {
  st_cd:               r => r.st_cd,
  site_name:           r => r.site_name || '',
  rdc:                 r => `${r.rdc || ''} / ${r.hub || ''}`,
  first_share_dt:      r => _d(r.first_share_dt),
  first_proposed_dt:   r => _d(r.first_proposed_dt),
  latest_proposed_dt:  r => _d(r.latest_proposed_dt),
  chg_given:           r => `${r.date_change_count || 0}/${r.date_given_count || 0}`,
  total_delay_days:    r => r.total_delay_days ?? '',
  d_gap:               r => r._dgap ?? '',
  repl_days:           r => `${_n(r._replShare)}/${_n(r._replLatestShare)}/${_n(r._replLayout)}/${_n(r._replDisplay)}`,
  bal_days:            r => (r.actual_open_dt ? 'opened' : (r._balOpen == null ? '' : `${_n(r._balDispatch)}/${r._balOpen}`)),
  mbq_100:             r => r.mbq_100 ?? '',
  total_stock:         r => r.total_stock ?? '',
  fill_rate_pct:       r => (r.fill_rate_pct != null ? `${r.fill_rate_pct}%` : ''),
  layout_generated:    r => (r.layout_generated ? `Yes${r.layout_rec_dt ? ' ' + fmtD(r.layout_rec_dt) : ''}` : 'No'),
  display_generated:   r => (r.display_generated ? `Yes${r.first_disp_dt ? ' ' + fmtD(r.first_disp_dt) : ''}` : 'No'),
  last_remarks:        r => (r.last_remarks || '').replace(/^(nan|none|nat)$/i, ''),
  remarks_change_count:r => r.remarks_change_count || 0,
  status_priority:     r => (r.status_priority ?? ''),
  status:              r => (STATUS_META[r.status || 'ACTIVE']?.label || r.status || ''),
}
// Safely turn any axios error into a STRING. FastAPI 422s return `detail` as an
// array of objects; passing that straight to toast() renders an object as a
// React child and crashes react-hot-toast (blanking the whole app).
const errMsg = (e, fallback = 'Something went wrong') => {
  const d = e?.response?.data?.detail
  if (typeof d === 'string') return d
  if (Array.isArray(d)) return d.map(x => x?.msg || x?.detail || JSON.stringify(x)).join('; ')
  if (d && typeof d === 'object') return d.msg || JSON.stringify(d)
  return e?.response?.data?.message || e?.message || fallback
}

const dl = (blob, name) => {
  const url = URL.createObjectURL(blob); const a = document.createElement('a')
  a.href = url; a.download = name; a.click(); URL.revokeObjectURL(url)
}

const Btn = ({ icon:Icon, label, onClick, disabled, tone='primary', small, title }) => {
  const t = {
    primary: { bg:C.primary, fg:'#fff', bd:C.primary },
    ghost:   { bg:C.cardBg, fg:C.textSub, bd:C.cardBorder },
    green:   { bg:C.green, fg:'#fff', bd:C.green },
    danger:  { bg:C.cardBg, fg:C.red, bd:C.redBd },
  }[tone]
  return (
    <button onClick={onClick} disabled={disabled} title={title} style={{
      display:'inline-flex', alignItems:'center', gap:6, cursor:disabled?'not-allowed':'pointer',
      padding: small?'4px 8px':'7px 12px', fontSize:small?12:13, fontWeight:600,
      borderRadius:6, border:`1px solid ${t.bd}`, background:t.bg, color:t.fg, opacity:disabled?0.55:1,
    }}>{Icon && <Icon size={small?12:14}/>}{label}</button>
  )
}

const TONES = {
  ok:{ bg:C.greenBg, fg:C.green, bd:C.greenBd }, error:{ bg:C.redBg, fg:C.red, bd:C.redBd },
  warn:{ bg:C.amberBg, fg:C.amber, bd:C.amberBd }, info:{ bg:C.blueBg, fg:C.blue, bd:C.blueBd },
  idle:{ bg:C.grayBg, fg:C.textMuted, bd:C.grayBd },
}
const Pill = ({ children, tone='idle' }) => {
  const t = TONES[tone] || TONES.idle
  return <span style={{ fontSize:11, fontWeight:700, padding:'2px 7px', borderRadius:20,
    background:t.bg, color:t.fg, border:`1px solid ${t.bd}`, whiteSpace:'nowrap' }}>{children}</span>
}

const Tile = ({ label, value, sub, tone }) => (
  <div style={{ flex:1, minWidth:150, background:C.cardBg, border:`1px solid ${C.cardBorder}`,
    borderRadius:10, padding:'12px 14px' }}>
    <div style={{ fontSize:11, fontWeight:700, color:C.textMuted, textTransform:'uppercase', letterSpacing:0.4 }}>{label}</div>
    <div style={{ fontSize:24, fontWeight:800, color:tone||C.text, marginTop:2 }}>{value}</div>
    {sub && <div style={{ fontSize:11, color:C.textMuted, marginTop:1 }}>{sub}</div>}
  </div>
)

const Panel = ({ title, children, right }) => (
  <div style={{ background:C.cardBg, border:`1px solid ${C.cardBorder}`, borderRadius:10, padding:14, flex:1, minWidth:320 }}>
    <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:8 }}>
      <div style={{ fontSize:13, fontWeight:700, color:C.text }}>{title}</div>{right}
    </div>
    {children}
  </div>
)

const MiniBtn = ({ icon:Icon, title, onClick }) => (
  <button title={title} onClick={onClick} style={{ border:`1px solid ${C.cardBorder}`, background:C.cardBg,
    borderRadius:5, padding:'3px 5px', cursor:'pointer', display:'inline-flex' }}>
    <Icon size={13} color={C.textSub} />
  </button>
)

// Reusable chart card: chart⇄table toggle, CSV export, zoom, (chart stays clickable
// via the caller's renderChart). `data`/`columns` drive both the table and CSV.
function ChartCard({ title, data = [], columns = [], csvName = 'chart', renderChart, hint }) {
  const [mode, setMode] = useState('chart')
  const [zoom, setZoom] = useState(false)
  const cell = (c, r) => (c.fmt ? c.fmt(r[c.key], r) : (r[c.key] ?? '—'))
  const exportCsv = () => {
    const head = columns.map(c => `"${c.label}"`).join(',')
    const body = data.map(r => columns.map(c => `"${String(cell(c, r) ?? '').replace(/"/g, '""')}"`).join(',')).join('\n')
    dl(new Blob(['﻿' + head + '\n' + body], { type: 'text/csv;charset=utf-8' }), csvName + '.csv')
  }
  const ChartAt = ({ h }) => <div style={{ height:h }}><ResponsiveContainer width="100%" height="100%">{renderChart()}</ResponsiveContainer></div>
  const TableView = ({ max }) => (
    <div style={{ overflow:'auto', maxHeight:max }}>
      <table style={{ width:'100%', borderCollapse:'collapse', fontSize:12 }}>
        <thead><tr>{columns.map(c => <th key={c.key} style={{ padding:'6px 8px', textAlign:'left', color:C.textSub,
          position:'sticky', top:0, background:C.headerBg, whiteSpace:'nowrap' }}>{c.label}</th>)}</tr></thead>
        <tbody>{data.length ? data.map((r,i) => (
          <tr key={i} style={{ borderTop:`1px solid ${C.cardBorder}` }}>
            {columns.map(c => <td key={c.key} style={{ padding:'5px 8px', color:C.textSub, whiteSpace:'nowrap' }}>{cell(c, r)}</td>)}
          </tr>)) : <tr><td colSpan={columns.length} style={{ padding:16, textAlign:'center', color:C.textMuted }}>No data</td></tr>}
        </tbody>
      </table>
    </div>
  )
  const toolbar = (
    <div style={{ display:'flex', gap:4 }}>
      <MiniBtn icon={mode==='chart'?TableIcon:BarChart3} title={mode==='chart'?'Table view':'Chart view'} onClick={()=>setMode(m=>m==='chart'?'table':'chart')} />
      <MiniBtn icon={Download} title="Export CSV" onClick={exportCsv} />
      <MiniBtn icon={Maximize2} title="Zoom" onClick={()=>setZoom(true)} />
    </div>
  )
  return (
    <Panel title={title} right={toolbar}>
      {hint && <div style={{ fontSize:10, color:C.textMuted, marginTop:-4, marginBottom:4 }}>{hint}</div>}
      {mode==='chart' ? <ChartAt h={200} /> : <TableView max={220} />}
      {zoom && (
        <Overlay onClose={()=>setZoom(false)}>
          <div style={{ width:'90vw', maxWidth:1100, maxHeight:'90vh', overflow:'auto', background:C.cardBg,
            borderRadius:12, padding:18, border:`1px solid ${C.cardBorder}` }} onClick={e=>e.stopPropagation()}>
            <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:10 }}>
              <div style={{ fontSize:16, fontWeight:800, color:C.text }}>{title}</div>
              <div style={{ display:'flex', gap:6 }}>{toolbar}<MiniBtn icon={X} title="Close" onClick={()=>setZoom(false)} /></div>
            </div>
            {mode==='chart' ? <ChartAt h={460} /> : <TableView max={'70vh'} />}
          </div>
        </Overlay>
      )}
    </Panel>
  )
}

const Field = ({ label, children }) => (
  <label style={{ display:'block', marginBottom:10 }}>
    <div style={{ fontSize:11, fontWeight:700, color:C.textMuted, marginBottom:3 }}>{label}</div>
    {children}
  </label>
)

// per-store proposed-opening-date trend (used in the detail drawer + Store popover)
function StoreDateChart({ history, height = 200 }) {
  const pts = (history || []).filter(h => h.proposed_opening_dt)
    .map(h => ({ x: h.share_dt || (h.changed_at||'').slice(0,10), proposed: Date.parse(h.proposed_opening_dt) }))
  if (pts.length < 2) return <div style={{ fontSize:12, color:C.textMuted, padding:'8px 0' }}>Not enough date history to chart (need ≥2 points).</div>
  return (
    <div>
      <div style={{ height }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={pts} margin={{ top:6, right:12, left:-6, bottom:0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke={C.cardBorder} />
            <XAxis dataKey="x" tickFormatter={fmtD} tick={{ fontSize:10, fill:C.textMuted }} />
            <YAxis domain={['dataMin','dataMax']} tickFormatter={ms=>fmtD(new Date(ms).toISOString())} tick={{ fontSize:10, fill:C.textMuted }} width={64} />
            <Tooltip labelFormatter={fmtD} formatter={ms=>[fmtD(new Date(ms).toISOString()),'Proposed open']} />
            <Line type="monotone" dataKey="proposed" stroke={C.primary} strokeWidth={2} dot={{ r:3 }} activeDot={{ r:5 }} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div style={{ fontSize:10, color:C.textMuted, textAlign:'center' }}>Proposed opening date over time (shared date on X)</div>
    </div>
  )
}

// ── On-page Help: abbreviations + per-column meaning & formula ──────────────────
function HelpModal({ onClose, leadDays = 10, segments = ['APP','GM'] }) {
  const segTxt = (segments && segments.length ? segments.join('+') : 'APP+GM')
  const ABBR = [
    ['ST_CD', 'Store code'], ['RDC', 'Regional distribution centre'], ['Hub', 'Feeding hub / warehouse'],
    ['Bgt', 'Budgeted (opening date)'], ['Chg', 'Number of times changed'], ['Given', 'Times a date was shared/given'],
    ['Repl', 'Replenishment'], ['Bal', 'Balance (days remaining)'], ['MBQ', 'Minimum base quantity (target stock)'],
    ['Stk', 'Stock'], ['FR%', 'Fill rate % = stock ÷ MBQ × 100'], ['SLOC', 'Storage location (stock bucket)'],
    ['SEG', 'Segment (APP=apparel, GM=general merch, …)'], ['R.Chg', 'Remark change count'],
    ['Dispatch date', `Opening date − lead days (currently ${leadDays}) = last date to ship`],
  ]
  const COLS = [
    ['Store', 'Store code (ST_CD). Click it to open that store\'s date-change chart.'],
    ['Name', 'Site name — from the store master.'],
    ['RDC / Hub', 'RDC and feeding hub — from the store master.'],
    ['1st Shared', 'First date the opening date was ever shared (first share date).'],
    ['1st Date', 'First proposed opening date ever given.'],
    ['Latest Bgt', 'Current (latest) budgeted opening date.'],
    ['Chg / Given', 'changes / times-given. Given = how many times an opening date was shared; Chg = how many of those actually changed the date. (First date is the baseline, not a change.)'],
    ['Delay', 'Total slippage = Latest Bgt − 1st Date (days the date has moved since first given).'],
    ['D.GAP', 'Most-recent shift = Latest Bgt − previous given date (the value before the last change). +N = pushed later, −N = pulled earlier, — = never changed. (Delay is the total since day one; D.GAP is just the last move.)'],
    ['Repl Days', `Days available for replenishment, counted up to the DISPATCH date (opening − ${leadDays}d), from each milestone → a/b/c/d = (dispatch − 1st-shared) / (dispatch − latest-shared) / (dispatch − layout-received) / (dispatch − display-shared). "—" if that milestone date is missing.`],
    ['Bal Days', `dispatch / opening. opening = Latest Bgt − today; dispatch = (Latest Bgt − ${leadDays}d) − today. Positive = days left, negative (red) = overdue. "opened" once an actual open date is set.`],
    ['MBQ 100%', `Store target stock = SUM(MBQ) over the selected segments (${segTxt}) from the grid (ARS_GRID_MJ), category SEG from VW_MASTER_PRODUCT.`],
    ['Total Stk', 'Store stock = SUM(STK_TTL) over the selected segments. Click the value to see the SLOC-wise breakdown.'],
    ['FR%', 'Fill rate = Total Stk ÷ MBQ 100% × 100. Green 80–150%, amber <80%, red >150%.'],
    ['Layout', 'Layout generated ✓/✕ + received date. Click to edit (ticking auto-fills today).'],
    ['Display', 'Display shared ✓/✕ + 1st-display date. Click to edit.'],
    ['Last Remark', 'Most recent remark. Click to add/edit (recorded in history only if it changes).'],
    ['R.Chg', 'How many times the remark has changed.'],
    ['Priority', 'Store priority (number). Synced with the store master MANUAL_ST_PRIORITY — editing here updates the master too. Type directly in the cell.'],
    ['Status', 'Lifecycle: Active (default) / Opened / Hold / Reject-Cancel. Change inline; Opened asks for the actual open date. A new/changed date auto-reactivates a dormant store.'],
  ]
  const th = { textAlign:'left', padding:'6px 10px', fontSize:11, fontWeight:800, color:C.textSub, position:'sticky', top:0, background:C.headerBg }
  const td = { padding:'6px 10px', fontSize:12.5, color:C.textSub, borderTop:`1px solid ${C.cardBorder}`, verticalAlign:'top' }
  return (
    <Overlay onClose={onClose}>
      <div style={{ width:760, maxWidth:'95vw', maxHeight:'90vh', overflow:'auto', background:C.cardBg,
        borderRadius:12, padding:20, border:`1px solid ${C.cardBorder}` }} onClick={e=>e.stopPropagation()}>
        <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:6 }}>
          <div style={{ fontSize:17, fontWeight:800, color:C.text }}>UPC Store Tracking — Help</div>
          <button onClick={onClose} style={{ background:'none', border:'none', cursor:'pointer' }}><X size={18} color={C.textMuted}/></button>
        </div>
        <div style={{ fontSize:12, color:C.textMuted, marginBottom:14 }}>
          Dates & counts come from what you upload/edit; MBQ / stock / FR% are live from the grid for the selected
          segments; dispatch = opening − <b>{leadDays}</b> days (set “Dispatch lead” in the header).
        </div>

        <div style={{ fontSize:13, fontWeight:800, color:C.text, margin:'6px 0' }}>Abbreviations</div>
        <table style={{ width:'100%', borderCollapse:'collapse', marginBottom:16 }}>
          <thead><tr><th style={{...th, width:130}}>Term</th><th style={th}>Meaning</th></tr></thead>
          <tbody>{ABBR.map(([a,m])=>(
            <tr key={a}><td style={{...td, fontWeight:700, color:C.text, whiteSpace:'nowrap'}}>{a}</td><td style={td}>{m}</td></tr>
          ))}</tbody>
        </table>

        <div style={{ fontSize:13, fontWeight:800, color:C.text, margin:'6px 0' }}>Columns — meaning & formula</div>
        <table style={{ width:'100%', borderCollapse:'collapse' }}>
          <thead><tr><th style={{...th, width:110}}>Column</th><th style={th}>How it works</th></tr></thead>
          <tbody>{COLS.map(([c,d])=>(
            <tr key={c}><td style={{...td, fontWeight:700, color:C.text, whiteSpace:'nowrap'}}>{c}</td><td style={td}>{d}</td></tr>
          ))}</tbody>
        </table>

        <div style={{ fontSize:12.5, fontWeight:800, color:C.text, margin:'14px 0 4px' }}>Hover previews</div>
        <div style={{ fontSize:12, color:C.textSub }}>
          Hover any metric cell — <b>Chg/Given, Delay, D.GAP, Repl Days, Bal Days</b> — to see its formula worked out
          with this store's own numbers. Hover the <b>Store</b> code for its date-change chart + history, <b>Total Stk</b>
          for the SLOC-wise split, and <b>Last Remark</b> for the full text.
        </div>
        <div style={{ fontSize:11, color:C.textMuted, marginTop:12 }}>
          Tip: click column headers to sort, use “Columns” to hide/show, and the charts to filter. Set “Dispatch lead”
          and the “MBQ/Stock segments” in the header. Export All = full data (Excel); Export View = the on-screen report (CSV).
        </div>
      </div>
    </Overlay>
  )
}

const inputStyle = { width:'100%', padding:'7px 9px', fontSize:13, borderRadius:6,
  border:`1px solid ${C.inputBorder||C.inputBd}`, background:C.inputBg, color:C.text, boxSizing:'border-box' }

const BLANK = {
  st_cd:'', proposed_opening_dt:'', share_dt:'', remarks:'',
  layout_generated:false, layout_rec_dt:'', display_generated:false, first_disp_dt:'', actual_open_dt:'',
  status:'ACTIVE', status_priority:'',
}

export default function UpcStoreTrackingPage() {
  const [rows, setRows]       = useState([])
  const [chart, setChart]     = useState(null)
  const [loading, setLoading] = useState(false)
  const [q, setQ]             = useState('')
  const [onlyDelayed, setOnlyDelayed] = useState(false)
  const [statusFilter, setStatusFilter] = useState('ACTIVE')   // default view: Active only
  const [monthFilter, setMonthFilter]   = useState('ALL')
  const [edit, setEdit]       = useState(null)     // form object for add/edit
  const [detail, setDetail]   = useState(null)     // store detail (with history)
  const [sort, setSort]       = useState({ key: null, dir: 'asc' })
  const [remarkEdit, setRemarkEdit] = useState(null)   // { st_cd, value }
  const [layoutEdit, setLayoutEdit] = useState(null)   // { st_cd, layout_generated, layout_rec_dt }
  const [displayEdit, setDisplayEdit] = useState(null) // { st_cd, display_generated, first_disp_dt }
  const [openedPrompt, setOpenedPrompt] = useState(null) // { st_cd, actual_open_dt } — asks open date
  const [stockView, setStockView] = useState(null)       // { st_cd, sloc, total } — SLOC popover
  const [segments, setSegments] = useState(['APP','GM'])  // MBQ/stock scope (SEG); default APP+GM
  const SEG_OPTIONS = ['APP','GM','MKT','ACC','NT','FAB','NA']
  const [balBucket, setBalBucket] = useState(null)   // chart drill: balance-days bucket
  const [changeDrill, setChangeDrill] = useState(null) // chart drill: {date, set:Set<st_cd>} from date-changes
  const [dateChartView, setDateChartView] = useState(null) // { store } — per-store date-history popover
  const [hiddenCols, setHiddenCols] = useState(() => {
    try { return new Set(JSON.parse(localStorage.getItem('upc_hidden_cols') || '[]')) } catch { return new Set() }
  })
  const [colMenu, setColMenu] = useState(false)
  const [helpOpen, setHelpOpen] = useState(false)
  const [hoverCard, setHoverCard] = useState(null)   // { kind:'remark'|'stock', r, x, y } — hover preview
  const showHover = (kind, r, e) => setHoverCard({ kind, r, x: e.clientX, y: e.clientY })
  const [leadDays, setLeadDays] = useState(() => {
    const v = parseInt(localStorage.getItem('upc_lead_days'), 10); return Number.isFinite(v) ? v : 10
  })
  const setLead = (v) => { const n = Math.max(0, parseInt(v, 10) || 0); setLeadDays(n); try { localStorage.setItem('upc_lead_days', String(n)) } catch {} }
  const todayMs = useMemo(() => Date.parse(new Date().toISOString().slice(0, 10)), [])
  const toggleCol = (key) => setHiddenCols(cur => {
    const n = new Set(cur); n.has(key) ? n.delete(key) : n.add(key)
    try { localStorage.setItem('upc_hidden_cols', JSON.stringify([...n])) } catch {}
    return n
  })
  const fileRef = useRef()

  const toggleSort = (key) => setSort(s =>
    s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'asc' })

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [l, c] = await Promise.all([upcTrackAPI.list(segments), upcTrackAPI.charts(segments)])
      setRows(l.data?.data?.items || [])
      setChart(c.data?.data || null)
    } catch (e) { toast.error(errMsg(e, 'Load failed')) }
    finally { setLoading(false) }
  }, [segments])
  useEffect(() => { load() }, [load])

  // balance-days bucket for a row (mirrors the backend chart buckets)
  const rowBucket = (r) =>
    r.actual_open_dt ? 'opened'
    : r.bal_days==null ? 'no_date'
    : r.bal_days<0 ? 'delayed'
    : r.bal_days<=7 ? '0-7'
    : r.bal_days<=30 ? '8-30' : '30+'

  // one row-matcher used for both the table and the cascading option lists.
  // `skip` lets a filter's OWN dropdown ignore itself when computing its options.
  const matchRow = useCallback((r, skip) => {
    const s = q.trim().toLowerCase()
    return (skip==='delayed' || !onlyDelayed || r.delayed) &&
      (skip==='status' || statusFilter === 'ALL' || (r.status || 'ACTIVE') === statusFilter) &&
      (skip==='month'  || monthFilter === 'ALL'  || monthKey(r.latest_proposed_dt) === monthFilter) &&
      (!balBucket || rowBucket(r) === balBucket) &&
      (!changeDrill || changeDrill.set.has(r.st_cd)) &&
      (skip==='q' || !s || `${r.st_cd} ${r.site_name||''} ${r.rdc||''} ${r.hub||''}`.toLowerCase().includes(s))
  }, [q, onlyDelayed, statusFilter, monthFilter, balBucket, changeDrill])

  // rows enriched with dispatch/opening balance + replenishment-window days
  const enriched = useMemo(() => rows.map(r => ({ ...r, ...calcDays(r, leadDays, todayMs) })),
    [rows, leadDays, todayMs])

  const filtered = useMemo(() => enriched.filter(r => matchRow(r)), [enriched, matchRow])

  // Cascading options: each dropdown shows only values available under the OTHER
  // active filters (plus the currently-selected value, so the control stays valid).
  const statusOptions = useMemo(() => {
    const present = new Set(enriched.filter(r => matchRow(r, 'status')).map(r => r.status || 'ACTIVE'))
    return STATUS_ORDER.filter(s => present.has(s) || s === statusFilter)
  }, [enriched, matchRow, statusFilter])

  const monthOptions = useMemo(() => {
    const seen = new Map()
    enriched.filter(r => matchRow(r, 'month')).forEach(r => {
      const k = monthKey(r.latest_proposed_dt); if (k) seen.set(k, monthLabel(r.latest_proposed_dt))
    })
    if (monthFilter !== 'ALL' && !seen.has(monthFilter)) seen.set(monthFilter, monthFilter)
    return [...seen.entries()].sort().map(([k, label]) => ({ k, label }))
  }, [enriched, matchRow, monthFilter])

  const onUpload = async (e) => {
    const f = e.target.files?.[0]; if (!f) return
    const fd = new FormData(); fd.append('file', f)
    const tid = toast.loading('Uploading…')
    try {
      const res = await upcTrackAPI.upload(fd)
      toast.success(res.data?.message || 'Uploaded', { id:tid })
      load()
    } catch (er) { toast.error(errMsg(er, 'Upload failed'), { id:tid }) }
    finally { if (fileRef.current) fileRef.current.value = '' }
  }

  const save = async () => {
    if (!edit.st_cd?.trim()) { toast.error('ST_CD is required'); return }
    const tid = toast.loading('Saving…')
    try {
      const body = { ...edit, st_cd: edit.st_cd.trim().toUpperCase() }
      if (edit._isNew) await upcTrackAPI.save(body)
      else await upcTrackAPI.update(body.st_cd, body)
      toast.success('Saved', { id:tid }); setEdit(null); load()
    } catch (er) { toast.error(errMsg(er, 'Save failed'), { id:tid }) }
  }

  const remove = async (stCd) => {
    if (!window.confirm(`Remove tracking for ${stCd}? History will be deleted.`)) return
    try { await upcTrackAPI.remove(stCd); toast.success('Removed'); load() }
    catch (e) { toast.error(errMsg(e, 'Delete failed')) }
  }

  // inline status change from the table — records an audited status event.
  const changeStatus = async (stCd, status, actual_open_dt) => {
    // Opening a store must capture WHEN it opened — prompt for the date first.
    if (status === 'OPENED' && actual_open_dt === undefined) {
      setOpenedPrompt({ st_cd: stCd, actual_open_dt: new Date().toISOString().slice(0,10) })
      return
    }
    try {
      const body = { st_cd: stCd, status }
      if (actual_open_dt !== undefined) body.actual_open_dt = actual_open_dt || ''
      await upcTrackAPI.update(stCd, body)
      toast.success(`${stCd} → ${STATUS_META[status]?.label || status}`)
      setOpenedPrompt(null); load()
    } catch (e) { toast.error(errMsg(e, 'Status update failed')) }
  }

  // inline remark add/edit (recorded in remark history only if it changed)
  const saveRemark = async () => {
    const { st_cd, value } = remarkEdit
    try {
      await upcTrackAPI.update(st_cd, { st_cd, remarks: value })
      toast.success(`Remark saved for ${st_cd}`); setRemarkEdit(null); load()
    } catch (e) { toast.error(errMsg(e, 'Remark save failed')) }
  }

  // inline layout edit — toggling generated also captures the received date
  const saveLayout = async () => {
    const { st_cd, layout_generated, layout_rec_dt } = layoutEdit
    try {
      await upcTrackAPI.update(st_cd, { st_cd, layout_generated, layout_rec_dt: layout_rec_dt || '' })
      toast.success(`Layout updated for ${st_cd}`); setLayoutEdit(null); load()
    } catch (e) { toast.error(errMsg(e, 'Layout save failed')) }
  }

  // inline display edit — toggling shared also captures the 1st-display date
  const saveDisplay = async () => {
    const { st_cd, display_generated, first_disp_dt } = displayEdit
    try {
      await upcTrackAPI.update(st_cd, { st_cd, display_generated, first_disp_dt: first_disp_dt || '' })
      toast.success(`Display updated for ${st_cd}`); setDisplayEdit(null); load()
    } catch (e) { toast.error(errMsg(e, 'Display save failed')) }
  }

  // inline priority — typed directly in the cell, saved on blur/Enter
  const savePriority = async (stCd, value) => {
    try {
      await upcTrackAPI.update(stCd, { st_cd: stCd, status_priority: value })
      toast.success(`Priority ${value || 'cleared'} · ${stCd}`); load()
    } catch (e) { toast.error(errMsg(e, 'Priority save failed')) }
  }

  const openDetail = async (stCd) => {
    try { const r = await upcTrackAPI.get(stCd); setDetail(r.data?.data) }
    catch (e) { toast.error(errMsg(e, 'Could not load detail')) }
  }

  // clicking the Store column opens its date-change chart (like Total Stk → SLOC)
  const openStoreChart = async (stCd) => {
    try { const r = await upcTrackAPI.get(stCd); setDateChartView(r.data?.data) }
    catch (e) { toast.error(errMsg(e, 'Could not load store history')) }
  }

  // complete dataset (all stores, all fields incl. SLOC) as Excel from the backend
  const exportXlsx = async () => {
    try { const r = await upcTrackAPI.export(segments); dl(r.data, 'upc_store_tracking_full.xlsx') }
    catch (e) { toast.error(errMsg(e, 'Export failed')) }
  }
  // the report exactly as shown on screen: current filters, sort order & visible columns
  const exportView = () => {
    const cols = visibleCols.filter(c => c.key !== '_actions')
    const text = (c, r) => {
      const v = VIEW_CSV[c.key] ? VIEW_CSV[c.key](r) : (c.get ? c.get(r) : '')
      return `"${String(v ?? '').replace(/"/g, '""')}"`
    }
    const head = cols.map(c => `"${c.label}"`).join(',')
    const body = sorted.map(r => cols.map(c => text(c, r)).join(',')).join('\n')
    dl(new Blob(['﻿' + head + '\n' + body], { type: 'text/csv;charset=utf-8' }), 'upc_store_tracking_view.csv')
  }

  const downloadTemplate = async () => {
    try { const r = await upcTrackAPI.template(); dl(r.data, 'upc_store_tracking_template.xlsx') }
    catch (e) { toast.error('Template download failed') }
  }

  const BUCKET_TONE = { delayed:C.red, '0-7':C.amber, '8-30':C.blue, '30+':C.green, opened:C.gray, no_date:C.grayBd }

  // Pre-fill the edit form from the store's LAST saved values, so re-saving an
  // unchanged date is a no-op (backend records history only on a real change).
  const openEdit = (r) => setEdit({
    st_cd: r.st_cd,
    proposed_opening_dt: r.latest_proposed_dt || '',
    share_dt: r.latest_share_dt || '',
    remarks: cleanTxt(r.last_remarks),
    layout_generated: r.layout_generated,
    layout_rec_dt: r.layout_rec_dt || '',
    display_generated: r.display_generated,
    first_disp_dt: r.first_disp_dt || '',
    actual_open_dt: r.actual_open_dt || '',
    status: r.status || 'ACTIVE',
    status_priority: r.status_priority || '',
    latest_proposed_dt: r.latest_proposed_dt,
  })

  // Column registry drives the header (sortable) AND the cells.
  const iconBtn = (onClick, title, Icon, color, bd=C.cardBorder) => (
    <button onClick={onClick} title={title}
      style={{ border:`1px solid ${bd}`, background:C.cardBg, borderRadius:5, padding:4, cursor:'pointer' }}>
      <Icon size={13} color={color}/></button>
  )
  const COLS = [
    { key:'st_cd', label:'Store', get:r=>r.st_cd, cell:r=>(
        <span title="Hover for date-change chart"
          onMouseEnter={e=>showHover('chart', r, e)} onMouseLeave={()=>setHoverCard(null)}
          style={{ fontWeight:700, color:C.blue, cursor:'default', textDecoration:'underline dotted' }}>{r.st_cd}</span>) },
    { key:'site_name', label:'Name', get:r=>r.site_name||'', cell:r=>r.site_name||'—', td:{ color:C.textSub, maxWidth:160, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' } },
    { key:'rdc', label:'RDC/Hub', get:r=>r.rdc||'', cell:r=>`${r.rdc||'—'} / ${r.hub||'—'}`, td:{ color:C.textSub, whiteSpace:'nowrap' } },
    { key:'first_share_dt', label:'1st Shared', get:r=>r.first_share_dt||'', cell:r=>fmtD(r.first_share_dt), td:{ whiteSpace:'nowrap', color:C.textSub } },
    { key:'first_proposed_dt', label:'1st Date', get:r=>r.first_proposed_dt||'', cell:r=>fmtD(r.first_proposed_dt), td:{ whiteSpace:'nowrap' } },
    { key:'latest_proposed_dt', label:'Latest Bgt', get:r=>r.latest_proposed_dt||'', cell:r=>fmtD(r.latest_proposed_dt), td:{ whiteSpace:'nowrap', fontWeight:600 } },
    { key:'chg_given', label:'Chg/Given', align:'center', get:r=>r.date_change_count||0, cell:r=>(
        <span title={`Chg / Given = ${r.date_change_count||0} change(s) / ${r.date_given_count||0} time(s) a date was given (shared).\nGiven = every share; Chg = shares that actually changed the date (the 1st date is the baseline, not a change).`}>
        <b style={{ color:r.date_change_count>0?C.amber:C.textSub }}>{r.date_change_count||0}</b>
        <span style={{ color:C.textMuted }}>/{r.date_given_count||0}</span></span>) },
    { key:'total_delay_days', label:'Delay', align:'center', get:r=>r.total_delay_days??-1e9,
      cell:r=><span title={`Delay = Latest Bgt (${fmtD(r.latest_proposed_dt)}) − 1st Date (${fmtD(r.first_proposed_dt)}) = ${r.total_delay_days ?? '—'} days\n(total slippage since the date was first given)`}>{r.total_delay_days ?? '—'}</span> },
    { key:'d_gap', label:'D.GAP', align:'center', get:r=>r._dgap??-1e9,
      cell:r=>{ const v=r._dgap; return v==null ? <span title="D.GAP — no change yet (only the baseline date exists)">—</span>
        : <span title={`D.GAP = Latest Bgt (${fmtD(r.latest_proposed_dt)}) − previous given (${fmtD(r.prev_proposed_dt)}) = ${v>0?'+':''}${v} days\n(the most-recent shift; +later, −earlier)`}
            style={{ color:v>0?C.amber:v<0?C.blue:C.textSub, fontWeight:600 }}>{v>0?`+${v}`:v}</span> } },
    { key:'repl_days', label:'Repl Days', align:'center', get:r=>r._replShare??-1e9,
      cell:r=>{ const f=v=>v==null?'—':v; const sep=<span style={{color:C.textMuted}}>/</span>; return (
        <span title={`Repl Days = days from each milestone to the DISPATCH date ${fmtD(r._dispatchISO)} (= opening − ${leadDays}d):\n`
          + `1st shared (${fmtD(r.first_share_dt)}) → ${f(r._replShare)}\n`
          + `latest shared (${fmtD(r.latest_share_dt)}) → ${f(r._replLatestShare)}\n`
          + `layout (${fmtD(r.layout_rec_dt)}) → ${f(r._replLayout)}\n`
          + `display (${fmtD(r.first_disp_dt)}) → ${f(r._replDisplay)}`}>
          {f(r._replShare)}{sep}{f(r._replLatestShare)}{sep}{f(r._replLayout)}{sep}{f(r._replDisplay)}
        </span>) } },
    { key:'bal_days', label:'Bal Days', align:'center', get:r=>r._balOpen??-1e9, cell:r=>{
        if (r.actual_open_dt) return <Pill tone="ok" >opened</Pill>
        if (r._balOpen==null) return '—'
        const col = v => v==null?C.textMuted : v<0?C.red : v<=7?C.amber : C.text
        return (
          <span title={`Bal Days = dispatch / opening (days remaining from today):\n`
            + `dispatch (${fmtD(r._dispatchISO)} = opening − ${leadDays}d) → ${r._balDispatch==null?'—':r._balDispatch}\n`
            + `opening  (${fmtD(r.latest_proposed_dt)}) → ${r._balOpen}\n(negative = overdue)`}>
            <b style={{color:col(r._balDispatch)}}>{r._balDispatch==null?'—':r._balDispatch}</b>
            <span style={{color:C.textMuted}}>/</span>
            <b style={{color:col(r._balOpen)}}>{r._balOpen}</b>
          </span>) } },
    { key:'mbq_100', label:'MBQ 100%', align:'right', get:r=>r.mbq_100??-1e9, cell:r=>fmtN(r.mbq_100) },
    { key:'total_stock', label:'Total Stk', align:'right', get:r=>r.total_stock??-1e9, cell:r=>(
        <span title="Hover for SLOC-wise stock"
          onMouseEnter={e=>showHover('stock', r, e)} onMouseMove={e=>showHover('stock', r, e)} onMouseLeave={()=>setHoverCard(null)}
          style={{ color:r.total_stock!=null?C.blue:C.textMuted, fontWeight:600,
                   textDecoration:r.total_stock!=null?'underline dotted':'none' }}>{fmtN(r.total_stock)}</span>) },
    { key:'fill_rate_pct', label:'FR%', align:'right', get:r=>r.fill_rate_pct??-1e9, cell:r=>
        r.fill_rate_pct==null ? '—'
        : <span style={{ color:r.fill_rate_pct>150?C.red:r.fill_rate_pct<80?C.amber:C.green, fontWeight:600 }}>{r.fill_rate_pct}%</span> },
    { key:'layout_generated', label:'Layout', align:'center', get:r=>r.layout_generated?1:0, cell:r=>(
        <button title="Edit layout (click)" onClick={()=>setLayoutEdit({ st_cd:r.st_cd,
            layout_generated:!!r.layout_generated, layout_rec_dt:r.layout_rec_dt||'' })}
          style={{ background:'none', border:'none', cursor:'pointer', display:'inline-flex', alignItems:'center', gap:3 }}>
          {r.layout_generated ? <CheckCircle2 size={15} color={C.green}/> : <X size={15} color={C.textMuted}/>}
          {r.layout_rec_dt && <span style={{ fontSize:10, color:C.textMuted }}>{fmtD(r.layout_rec_dt)}</span>}
        </button>) },
    { key:'display_generated', label:'Display', align:'center', get:r=>r.display_generated?1:0, td:{ borderLeft:`1px solid ${C.cardBorder}` }, cell:r=>(
        <button title="Edit display (click)" onClick={()=>setDisplayEdit({ st_cd:r.st_cd,
            display_generated:!!r.display_generated, first_disp_dt:r.first_disp_dt||'' })}
          style={{ background:'none', border:'none', cursor:'pointer', display:'inline-flex', alignItems:'center', gap:3 }}>
          {r.display_generated ? <CheckCircle2 size={15} color={C.green}/> : <X size={15} color={C.textMuted}/>}
          {r.first_disp_dt && <span style={{ fontSize:10, color:C.textMuted }}>{fmtD(r.first_disp_dt)}</span>}
        </button>) },
    { key:'last_remarks', label:'Last Remark', get:r=>cleanTxt(r.last_remarks), td:{ maxWidth:200 }, cell:r=>{
        const t = cleanTxt(r.last_remarks)
        return (
          <div onClick={()=>setRemarkEdit({ st_cd:r.st_cd, value:t })}
            onMouseEnter={t ? (e=>showHover('remark', r, e)) : undefined}
            onMouseMove={t ? (e=>showHover('remark', r, e)) : undefined} onMouseLeave={()=>setHoverCard(null)}
            style={{ display:'flex', alignItems:'center', gap:5, cursor:'pointer' }}>
            <Pencil size={11} color={C.textMuted} style={{ flexShrink:0 }}/>
            <span style={{ whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis', color:t?C.textSub:C.textMuted }}>{t||'add…'}</span>
          </div>) } },
    { key:'remarks_change_count', label:'R.Chg', align:'center', get:r=>r.remarks_change_count||0, cell:r=> r.remarks_change_count>0 ? <Pill tone="info">{r.remarks_change_count}</Pill> : '0' },
    { key:'status_priority', label:'Priority', align:'center', get:r=> r.status_priority!=null&&r.status_priority!=='' ? Number(r.status_priority) : -1e9, cell:r=>{
        const cur = r.status_priority!=null ? String(r.status_priority) : ''
        return (
          <input type="number" min="0" step="1" inputMode="numeric" defaultValue={cur}
            key={r.st_cd + '|' + cur} title="Type priority (number) — saves on Enter / when you click away"
            onClick={e=>e.stopPropagation()}
            onKeyDown={e=>{ if(e.key==='Enter') e.target.blur() }}
            onBlur={e=>{ const v=e.target.value.replace(/[^0-9]/g,''); if(v!==cur) savePriority(r.st_cd, v) }}
            style={{ width:54, padding:'3px 6px', fontSize:12, textAlign:'center', borderRadius:6,
                     border:`1px solid ${C.inputBorder||C.inputBd}`, background:C.inputBg, color:C.text }} />
        ) } },
    { key:'status', label:'Status', align:'center', get:r=>r.status||'ACTIVE', cell:r=>{
        const cur = r.status || 'ACTIVE'
        const t = TONES[(STATUS_META[cur]||STATUS_META.ACTIVE).tone]
        return (
          <select value={cur} title="Change status (recorded in history)"
            onClick={e=>e.stopPropagation()}
            onChange={e=>changeStatus(r.st_cd, e.target.value)}
            style={{ fontSize:11, fontWeight:700, borderRadius:20, padding:'2px 6px', cursor:'pointer',
                     border:`1px solid ${t.bd}`, background:t.bg, color:t.fg, appearance:'none' }}>
            {STATUS_ORDER.map(s => <option key={s} value={s} style={{ background:C.cardBg, color:C.text }}>{STATUS_META[s].label}</option>)}
          </select>) } },
    { key:'_actions', label:'', sortable:false, td:{ whiteSpace:'nowrap' }, cell:r=>(
        <div style={{ display:'flex', gap:4 }}>
          {iconBtn(()=>openDetail(r.st_cd), 'History', Eye, C.textSub)}
          {iconBtn(()=>openEdit(r), 'Edit', Pencil, C.blue)}
          {iconBtn(()=>remove(r.st_cd), 'Remove', Trash2, C.red, C.redBd)}
        </div>) },
  ]

  const sorted = (() => {
    if (!sort.key) return filtered
    const col = COLS.find(c => c.key === sort.key)
    if (!col?.get) return filtered
    const arr = [...filtered].sort((a, b) => {
      const x = col.get(a), y = col.get(b)
      if (typeof x === 'number' && typeof y === 'number') return x - y
      return String(x).localeCompare(String(y))
    })
    return sort.dir === 'desc' ? arr.reverse() : arr
  })()

  // st_cd + actions always shown; everything else is hideable via the chooser.
  const ALWAYS = new Set(['st_cd', '_actions'])
  const visibleCols = COLS.filter(c => !hiddenCols.has(c.key))
  const hideableCols = COLS.filter(c => !ALWAYS.has(c.key) && c.label)

  return (
    <div style={{ padding:'18px 20px', maxWidth:1600, margin:'0 auto' }}>
      {/* Header */}
      <div style={{ display:'flex', alignItems:'center', gap:10, marginBottom:14, flexWrap:'wrap' }}>
        <Store size={22} color={C.primary} />
        <div style={{ flex:1 }}>
          <h1 style={{ fontSize:19, fontWeight:800, color:C.text, margin:0 }}>UPC Store Tracking</h1>
          <div style={{ fontSize:12, color:C.textMuted }}>
            Opening-date & remarks history · live MBQ / stock / SLOC / fill-rate
          </div>
        </div>
        <input ref={fileRef} type="file" accept=".xlsx,.xls" hidden onChange={onUpload} />
        <Btn icon={FileDown} label="Template" tone="ghost" onClick={downloadTemplate} />
        <Btn icon={Upload} label="Upload" tone="ghost" onClick={() => fileRef.current?.click()} />
        <Btn icon={Plus} label="Add Store" tone="ghost" onClick={() => setEdit({ ...BLANK, _isNew:true })} />
        <Btn icon={Download} label="Export All" tone="ghost" onClick={exportXlsx} title="Complete data (Excel) — all stores & fields" />
        <Btn icon={HelpCircle} label="Help" tone="ghost" onClick={()=>setHelpOpen(true)} title="Column meanings & formulas" />
        <Btn icon={RefreshCw} label="" tone="ghost" onClick={load} disabled={loading} />
      </div>

      {/* Segment scope — MBQ / stock / SLOC / fill-rate are computed for these SEGs */}
      <div style={{ display:'flex', alignItems:'center', gap:8, marginBottom:12, flexWrap:'wrap' }}>
        <span style={{ fontSize:12, fontWeight:700, color:C.textMuted }}>MBQ / Stock segments:</span>
        {SEG_OPTIONS.map(s => {
          const on = segments.includes(s)
          return (
            <button key={s} onClick={()=>setSegments(cur => cur.includes(s) ? cur.filter(x=>x!==s) : [...cur, s])}
              style={{ fontSize:11, fontWeight:700, padding:'3px 10px', borderRadius:20, cursor:'pointer',
                       border:`1px solid ${on?C.primary:C.cardBorder}`, background:on?C.primaryLt||C.blueBg:C.cardBg,
                       color:on?C.primary:C.textMuted }}>{s}</button>
          )
        })}
        <span style={{ fontSize:11, color:C.textMuted }}>
          {segments.length ? `showing ${segments.join(' + ')}` : 'none → defaults to APP + GM'}
        </span>
        <span style={{ width:1, height:18, background:C.cardBorder, margin:'0 4px' }} />
        <span style={{ fontSize:12, fontWeight:700, color:C.textMuted }}>Dispatch lead (days):</span>
        <input type="number" min="0" step="1" value={leadDays} onChange={e=>setLead(e.target.value)}
               title="Last-dispatch date = proposed opening − this many days"
               style={{ ...inputStyle, width:64, padding:'3px 8px', textAlign:'center' }} />
        <span style={{ fontSize:11, color:C.textMuted }}>dispatch = opening − {leadDays}d</span>
      </div>

      {/* Stat tiles */}
      <div style={{ display:'flex', gap:10, flexWrap:'wrap', marginBottom:14 }}>
        <Tile label="Tracked Stores" value={fmtN(chart?.total_stores ?? rows.length)} />
        <Tile label="Delayed" value={fmtN(chart?.delayed_stores ?? 0)} tone={C.red}
              sub="past latest budgeted date" />
        <Tile label="Opened" value={fmtN(chart?.opened_stores ?? 0)} tone={C.green} />
        <Tile label="Date Changes (total)" value={fmtN((chart?.date_changes||[]).reduce((a,b)=>a+b.changes,0))}
              tone={C.amber} sub="proposed date revised" />
      </div>

      {/* Charts — each: chart⇄table toggle, CSV, zoom, and clickable to filter */}
      <div style={{ display:'flex', gap:12, flexWrap:'wrap', marginBottom:14 }}>
        <ChartCard title="Status distribution" csvName="upc_status"
          hint="Click a bar to filter by status"
          data={chart?.status_dist||[]}
          columns={[{key:'status',label:'Status',fmt:v=>STATUS_META[v]?.label||v},{key:'count',label:'Stores'}]}
          renderChart={()=>(
            <BarChart data={chart?.status_dist||[]} margin={{ top:6, right:10, left:-18, bottom:0 }}
              onClick={e=>{ const p=e?.activePayload?.[0]?.payload; if(p) setStatusFilter(p.status) }}>
              <CartesianGrid strokeDasharray="3 3" stroke={C.cardBorder} />
              <XAxis dataKey="status" tickFormatter={v=>STATUS_META[v]?.label||v} tick={{ fontSize:10, fill:C.textMuted }} />
              <YAxis allowDecimals={false} tick={{ fontSize:10, fill:C.textMuted }} />
              <Tooltip />
              <Bar dataKey="count" radius={[4,4,0,0]} cursor="pointer">
                <LabelList dataKey="count" position="top" style={{ fontSize:11, fontWeight:700, fill:C.text }} />
                {(chart?.status_dist||[]).map((b,i)=><Cell key={i} fill={TONES[(STATUS_META[b.status]||STATUS_META.ACTIVE).tone].fg} />)}
              </Bar>
            </BarChart>
          )} />

        <ChartCard title="Stores by opening month" csvName="upc_by_month"
          hint="Click a bar to filter by month"
          data={chart?.month_dist||[]}
          columns={[{key:'month',label:'Month',fmt:v=>monthLabel(v+'-01')},{key:'count',label:'Stores'}]}
          renderChart={()=>(
            <BarChart data={chart?.month_dist||[]} margin={{ top:6, right:10, left:-18, bottom:0 }}
              onClick={e=>{ const p=e?.activePayload?.[0]?.payload; if(p) setMonthFilter(p.month) }}>
              <CartesianGrid strokeDasharray="3 3" stroke={C.cardBorder} />
              <XAxis dataKey="month" tickFormatter={v=>monthLabel(v+'-01')} tick={{ fontSize:10, fill:C.textMuted }} />
              <YAxis allowDecimals={false} tick={{ fontSize:10, fill:C.textMuted }} />
              <Tooltip labelFormatter={v=>monthLabel(v+'-01')} />
              <Bar dataKey="count" fill={C.primary} radius={[4,4,0,0]} cursor="pointer">
                <LabelList dataKey="count" position="top" style={{ fontSize:11, fontWeight:700, fill:C.text }} />
              </Bar>
            </BarChart>
          )} />

        <ChartCard title="Balance days to opening" csvName="upc_bal_days"
          hint={balBucket ? `Filtering: ${balBucket} — click again to clear` : 'Click a bucket to filter the table'}
          data={chart?.bal_buckets||[]}
          columns={[{key:'bucket',label:'Bucket'},{key:'count',label:'Stores'}]}
          renderChart={()=>(
            <BarChart data={chart?.bal_buckets||[]} margin={{ top:6, right:10, left:-18, bottom:0 }}
              onClick={e=>{ const b=e?.activePayload?.[0]?.payload?.bucket; if(b){ setStatusFilter('ALL'); setBalBucket(cur=>cur===b?null:b) } }}>
              <CartesianGrid strokeDasharray="3 3" stroke={C.cardBorder} />
              <XAxis dataKey="bucket" tick={{ fontSize:10, fill:C.textMuted }} />
              <YAxis allowDecimals={false} tick={{ fontSize:10, fill:C.textMuted }} />
              <Tooltip />
              <Bar dataKey="count" radius={[4,4,0,0]} cursor="pointer">
                <LabelList dataKey="count" position="top" style={{ fontSize:11, fontWeight:700, fill:C.text }} />
                {(chart?.bal_buckets||[]).map((b,i)=><Cell key={i} fill={balBucket && balBucket!==b.bucket ? C.grayBd : (BUCKET_TONE[b.bucket]||C.gray)} />)}
              </Bar>
            </BarChart>
          )} />

        <ChartCard title="Date changes over time" csvName="upc_date_changes"
          hint={changeDrill ? `Filtering: ${changeDrill.set.size} store(s) changed ${fmtD(changeDrill.date)} — click again to clear` : 'Click a point → stores that changed on that date'}
          data={chart?.date_changes||[]}
          columns={[{key:'date',label:'Date',fmt:fmtD},{key:'changes',label:'Changes'}]}
          renderChart={()=>(
            <LineChart data={chart?.date_changes||[]} margin={{ top:6, right:10, left:-18, bottom:0 }}
              onClick={e=>{ const p=e?.activePayload?.[0]?.payload; if(p?.date){ setStatusFilter('ALL')
                setChangeDrill(cur => cur?.date===p.date ? null : { date:p.date, set:new Set(p.stores||[]) }) } }}>
              <CartesianGrid strokeDasharray="3 3" stroke={C.cardBorder} />
              <XAxis dataKey="date" tickFormatter={fmtD} tick={{ fontSize:10, fill:C.textMuted }} />
              <YAxis allowDecimals={false} tick={{ fontSize:10, fill:C.textMuted }} />
              <Tooltip labelFormatter={fmtD} />
              <Line type="monotone" dataKey="changes" stroke={C.primary} strokeWidth={2} dot={{ r:3, cursor:'pointer' }} activeDot={{ r:5, cursor:'pointer' }}>
                <LabelList dataKey="changes" position="top" style={{ fontSize:10, fontWeight:700, fill:C.textSub }} />
              </Line>
            </LineChart>
          )} />
      </div>

      {/* Filter bar */}
      <div style={{ display:'flex', gap:10, alignItems:'center', marginBottom:8, flexWrap:'wrap' }}>
        <div style={{ position:'relative', flex:1, minWidth:220, maxWidth:360 }}>
          <Search size={14} style={{ position:'absolute', left:9, top:9, color:C.textMuted }} />
          <input value={q} onChange={e=>setQ(e.target.value)} placeholder="Search store / RDC / hub…"
                 style={{ ...inputStyle, paddingLeft:28 }} />
        </div>
        <select value={statusFilter} onChange={e=>setStatusFilter(e.target.value)}
                style={{ ...inputStyle, width:'auto', minWidth:130 }}>
          <option value="ALL">All statuses</option>
          {statusOptions.map(s => <option key={s} value={s}>{STATUS_META[s].label}</option>)}
        </select>
        <select value={monthFilter} onChange={e=>setMonthFilter(e.target.value)}
                style={{ ...inputStyle, width:'auto', minWidth:120 }}>
          <option value="ALL">All months</option>
          {monthOptions.map(m => <option key={m.k} value={m.k}>{m.label}</option>)}
        </select>
        <label style={{ fontSize:12, color:C.textSub, display:'flex', alignItems:'center', gap:6, cursor:'pointer' }}>
          <input type="checkbox" checked={onlyDelayed} onChange={e=>setOnlyDelayed(e.target.checked)} />
          Delayed only
        </label>
        {(statusFilter!=='ACTIVE'||monthFilter!=='ALL'||onlyDelayed||q||balBucket||changeDrill) &&
          <Btn label="Reset" tone="ghost" small onClick={()=>{ setStatusFilter('ACTIVE'); setMonthFilter('ALL'); setOnlyDelayed(false); setQ(''); setBalBucket(null); setChangeDrill(null) }} />}
        <div style={{ position:'relative' }}>
          <Btn icon={Columns} label="Columns" tone="ghost" small onClick={()=>setColMenu(v=>!v)} />
          {colMenu && (
            <div style={{ position:'absolute', top:'110%', right:0, zIndex:20, background:C.cardBg, border:`1px solid ${C.cardBorder}`,
              borderRadius:8, padding:10, minWidth:170, boxShadow:'0 6px 20px rgba(0,0,0,0.15)', maxHeight:300, overflowY:'auto' }}>
              <div style={{ fontSize:11, fontWeight:700, color:C.textMuted, marginBottom:6 }}>Show columns</div>
              {hideableCols.map(c => (
                <label key={c.key} style={{ display:'flex', alignItems:'center', gap:7, fontSize:12, color:C.textSub, padding:'3px 0', cursor:'pointer' }}>
                  <input type="checkbox" checked={!hiddenCols.has(c.key)} onChange={()=>toggleCol(c.key)} />{c.label}
                </label>
              ))}
              {hiddenCols.size>0 && <button onClick={()=>{ setHiddenCols(new Set()); try{localStorage.setItem('upc_hidden_cols','[]')}catch{} }}
                style={{ marginTop:6, fontSize:11, color:C.blue, background:'none', border:'none', cursor:'pointer', padding:0 }}>Show all</button>}
            </div>
          )}
        </div>
        <span style={{ fontSize:12, color:C.textMuted, marginLeft:'auto' }}>{filtered.length} of {rows.length}</span>
      </div>

      {/* Export the current on-screen report — top-right, above the table */}
      <div style={{ display:'flex', justifyContent:'flex-end', marginBottom:6 }}>
        <Btn icon={FileDown} label="Export View" tone="ghost" small onClick={exportView}
             title="Download the current view (filters, sort & columns) as CSV" />
      </div>

      {/* Table — sticky header, scrolls within its own box so headers stay frozen */}
      <div style={{ overflow:'auto', maxHeight:'calc(100vh - 220px)', background:C.cardBg,
                    border:`1px solid ${C.cardBorder}`, borderRadius:10 }}>
        <table style={{ width:'100%', borderCollapse:'collapse', fontSize:12 }}>
          <thead>
            <tr style={{ color:C.textSub }}>
              {visibleCols.map(c => {
                const active = sort.key === c.key
                const sortable = c.sortable !== false
                return (
                  <th key={c.key} onClick={sortable ? () => toggleSort(c.key) : undefined}
                      style={{ padding:'8px 9px', fontWeight:700, whiteSpace:'nowrap',
                               textAlign:c.align||'left', userSelect:'none',
                               cursor:sortable?'pointer':'default', color:active?C.primary:C.textSub,
                               position:'sticky', top:0, zIndex:2, background:C.headerBg,
                               boxShadow:`inset 0 -1px 0 ${C.cardBorder}`,
                               borderLeft:c.td?.borderLeft }}>
                    {c.label}{active ? (sort.dir==='asc' ? ' ▲' : ' ▼') : (sortable && c.label ? ' ↕' : '')}
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {sorted.map((r,i) => (
              <tr key={r.st_cd} style={{ borderTop:`1px solid ${C.cardBorder}`, background:i%2?C.rowAlt:'transparent' }}>
                {visibleCols.map(c => (
                  <td key={c.key} style={{ padding:'7px 9px', textAlign:c.align||'left', ...(c.td||{}) }}>
                    {c.cell(r)}
                  </td>
                ))}
              </tr>
            ))}
            {!sorted.length && (
              <tr><td colSpan={visibleCols.length} style={{ padding:30, textAlign:'center', color:C.textMuted }}>
                {loading ? 'Loading…'
                  : rows.length ? 'No stores match the current filters.'
                  : 'No stores tracked yet. Upload a sheet (ST_CD, proposed opening date, share date) or add a store.'}
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

      {edit && <EditModal edit={edit} setEdit={setEdit} onSave={save} />}
      {detail && <DetailDrawer detail={detail} onClose={()=>setDetail(null)} />}
      {helpOpen && <HelpModal onClose={()=>setHelpOpen(false)} leadDays={leadDays} segments={segments} />}
      {hoverCard && (() => {
        const w = hoverCard.kind==='chart' ? 420 : hoverCard.kind==='stock' ? 260 : 300
        const h = hoverCard.kind==='chart' ? 330 : 220
        const left = Math.min(hoverCard.x + 14, window.innerWidth - w - 12)
        const top = Math.min(hoverCard.y + 14, window.innerHeight - h)
        const sloc = Object.entries(hoverCard.r.sloc||{}).filter(([,v])=>v!=null&&v!==0)
        return (
          <div style={{ position:'fixed', left, top, width:w, zIndex:2000, pointerEvents:'none',
            background:C.cardBg, border:`1px solid ${C.cardBorder}`, borderRadius:8, padding:'8px 10px',
            boxShadow:'0 8px 24px rgba(0,0,0,0.2)' }}>
            {hoverCard.kind==='chart' ? (
              <>
                <div style={{ fontSize:10, fontWeight:700, color:C.textMuted, marginBottom:2 }}>
                  DATE CHANGES · {hoverCard.r.st_cd} · {hoverCard.r.date_given_count||0} given · {hoverCard.r.date_change_count||0} changed
                </div>
                <StoreDateChart history={hoverCard.r.date_history} height={150} />
                <div style={{ marginTop:6, maxHeight:130, overflow:'hidden', borderTop:`1px solid ${C.cardBorder}`, paddingTop:4 }}>
                  {(hoverCard.r.date_history||[]).map((h,i)=>(
                    <div key={i} style={{ display:'flex', justifyContent:'space-between', gap:10, fontSize:11, padding:'1px 0' }}>
                      <b style={{ color:C.text }}>{fmtD(h.proposed_opening_dt)}</b>
                      <span style={{ color:C.textMuted }}>shared {fmtD(h.share_dt)}</span>
                    </div>
                  ))}
                  {!(hoverCard.r.date_history||[]).length && <div style={{ fontSize:11, color:C.textMuted }}>No date history.</div>}
                </div>
              </>
            ) : hoverCard.kind==='remark' ? (
              <>
                <div style={{ fontSize:10, fontWeight:700, color:C.textMuted, marginBottom:3 }}>REMARK · {hoverCard.r.st_cd}</div>
                <div style={{ fontSize:12.5, color:C.text, whiteSpace:'pre-wrap', wordBreak:'break-word' }}>{cleanTxt(hoverCard.r.last_remarks) || '—'}</div>
              </>
            ) : (
              <>
                <div style={{ fontSize:10, fontWeight:700, color:C.textMuted, marginBottom:4 }}>
                  SLOC-WISE · {hoverCard.r.st_cd} · total {fmtN(hoverCard.r.total_stock)}
                </div>
                {sloc.length ? (
                  <table style={{ width:'100%', borderCollapse:'collapse', fontSize:11.5 }}><tbody>
                    {sloc.map(([k,v])=>(
                      <tr key={k}><td style={{ padding:'2px 4px', color:C.textSub }}>{k.replace('STK_','')}</td>
                        <td style={{ padding:'2px 4px', textAlign:'right', color:C.text, fontWeight:600 }}>{fmtN(v)}</td></tr>
                    ))}
                  </tbody></table>
                ) : <div style={{ fontSize:12, color:C.textMuted }}>No live stock.</div>}
              </>
            )}
          </div>
        )
      })()}
      {remarkEdit && (
        <Overlay onClose={()=>setRemarkEdit(null)}>
          <div style={{ width:420, maxWidth:'94vw', background:C.cardBg, borderRadius:12, padding:18, border:`1px solid ${C.cardBorder}` }} onClick={e=>e.stopPropagation()}>
            <div style={{ fontSize:15, fontWeight:800, color:C.text, marginBottom:4 }}>Remark · {remarkEdit.st_cd}</div>
            <div style={{ fontSize:11, color:C.textMuted, marginBottom:8 }}>Recorded in history only if it differs from the last remark.</div>
            <textarea autoFocus style={{ ...inputStyle, minHeight:80, resize:'vertical' }} value={remarkEdit.value}
              onChange={e=>setRemarkEdit(s=>({ ...s, value:e.target.value }))} />
            <div style={{ display:'flex', justifyContent:'flex-end', gap:8, marginTop:12 }}>
              <Btn label="Cancel" tone="ghost" onClick={()=>setRemarkEdit(null)} />
              <Btn icon={CheckCircle2} label="Save Remark" tone="primary" onClick={saveRemark} />
            </div>
          </div>
        </Overlay>
      )}
      {layoutEdit && (
        <Overlay onClose={()=>setLayoutEdit(null)}>
          <div style={{ width:420, maxWidth:'94vw', background:C.cardBg, borderRadius:12, padding:18, border:`1px solid ${C.cardBorder}` }} onClick={e=>e.stopPropagation()}>
            <div style={{ fontSize:15, fontWeight:800, color:C.text, marginBottom:10 }}>Layout · {layoutEdit.st_cd}</div>
            <label style={{ display:'flex', alignItems:'center', gap:8, fontSize:13, color:C.textSub, marginBottom:12 }}>
              <input type="checkbox" checked={!!layoutEdit.layout_generated}
                onChange={e=>setLayoutEdit(s=>({ ...s, layout_generated:e.target.checked,
                  layout_rec_dt: e.target.checked && !s.layout_rec_dt ? new Date().toISOString().slice(0,10) : s.layout_rec_dt }))} />
              Layout generated
            </label>
            <Field label="Layout received date">
              <input type="date" style={inputStyle} value={layoutEdit.layout_rec_dt||''}
                onChange={e=>setLayoutEdit(s=>({ ...s, layout_rec_dt:e.target.value }))} />
            </Field>
            <div style={{ display:'flex', justifyContent:'flex-end', gap:8, marginTop:6 }}>
              <Btn label="Cancel" tone="ghost" onClick={()=>setLayoutEdit(null)} />
              <Btn icon={CheckCircle2} label="Save Layout" tone="primary" onClick={saveLayout} />
            </div>
          </div>
        </Overlay>
      )}
      {displayEdit && (
        <Overlay onClose={()=>setDisplayEdit(null)}>
          <div style={{ width:420, maxWidth:'94vw', background:C.cardBg, borderRadius:12, padding:18, border:`1px solid ${C.cardBorder}` }} onClick={e=>e.stopPropagation()}>
            <div style={{ fontSize:15, fontWeight:800, color:C.text, marginBottom:10 }}>Display · {displayEdit.st_cd}</div>
            <label style={{ display:'flex', alignItems:'center', gap:8, fontSize:13, color:C.textSub, marginBottom:12 }}>
              <input type="checkbox" checked={!!displayEdit.display_generated}
                onChange={e=>setDisplayEdit(s=>({ ...s, display_generated:e.target.checked,
                  first_disp_dt: e.target.checked && !s.first_disp_dt ? new Date().toISOString().slice(0,10) : s.first_disp_dt }))} />
              Display shared
            </label>
            <Field label="1st display shared date">
              <input type="date" style={inputStyle} value={displayEdit.first_disp_dt||''}
                onChange={e=>setDisplayEdit(s=>({ ...s, first_disp_dt:e.target.value }))} />
            </Field>
            <div style={{ display:'flex', justifyContent:'flex-end', gap:8, marginTop:6 }}>
              <Btn label="Cancel" tone="ghost" onClick={()=>setDisplayEdit(null)} />
              <Btn icon={CheckCircle2} label="Save Display" tone="primary" onClick={saveDisplay} />
            </div>
          </div>
        </Overlay>
      )}
      {openedPrompt && (
        <Overlay onClose={()=>setOpenedPrompt(null)}>
          <div style={{ width:380, maxWidth:'94vw', background:C.cardBg, borderRadius:12, padding:18, border:`1px solid ${C.cardBorder}` }} onClick={e=>e.stopPropagation()}>
            <div style={{ fontSize:15, fontWeight:800, color:C.text, marginBottom:4 }}>Open store · {openedPrompt.st_cd}</div>
            <div style={{ fontSize:12, color:C.textMuted, marginBottom:10 }}>Marking this store OPENED — please confirm the actual opening date.</div>
            <Field label="Actual open date">
              <input type="date" autoFocus style={inputStyle} value={openedPrompt.actual_open_dt||''}
                onChange={e=>setOpenedPrompt(s=>({ ...s, actual_open_dt:e.target.value }))} />
            </Field>
            <div style={{ display:'flex', justifyContent:'flex-end', gap:8, marginTop:6 }}>
              <Btn label="Cancel" tone="ghost" onClick={()=>setOpenedPrompt(null)} />
              <Btn icon={CheckCircle2} label="Mark Opened" tone="green"
                onClick={()=>changeStatus(openedPrompt.st_cd, 'OPENED', openedPrompt.actual_open_dt)} />
            </div>
          </div>
        </Overlay>
      )}
      {stockView && (
        <Overlay onClose={()=>setStockView(null)}>
          <div style={{ width:440, maxWidth:'94vw', background:C.cardBg, borderRadius:12, padding:18, border:`1px solid ${C.cardBorder}` }} onClick={e=>e.stopPropagation()}>
            <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:10 }}>
              <div style={{ fontSize:15, fontWeight:800, color:C.text }}>SLOC-wise stock · {stockView.st_cd}</div>
              <button onClick={()=>setStockView(null)} style={{ background:'none', border:'none', cursor:'pointer' }}><X size={18} color={C.textMuted}/></button>
            </div>
            <div style={{ fontSize:12, color:C.textMuted, marginBottom:10 }}>Total: <b style={{ color:C.text }}>{fmtN(stockView.total)}</b> (live from grid)</div>
            {(() => {
              const rows = Object.entries(stockView.sloc||{}).filter(([,v])=>v!=null)
              if (!rows.length) return <div style={{ fontSize:12, color:C.textMuted }}>No live stock for this store.</div>
              return (
                <table style={{ width:'100%', borderCollapse:'collapse', fontSize:12 }}>
                  <thead><tr><th style={{ textAlign:'left', padding:'5px 8px', color:C.textSub }}>SLOC</th>
                    <th style={{ textAlign:'right', padding:'5px 8px', color:C.textSub }}>Qty</th></tr></thead>
                  <tbody>{rows.map(([k,v])=>(
                    <tr key={k} style={{ borderTop:`1px solid ${C.cardBorder}` }}>
                      <td style={{ padding:'5px 8px', color:C.textSub }}>{k.replace('STK_','')}</td>
                      <td style={{ padding:'5px 8px', textAlign:'right', color:v?C.text:C.textMuted, fontWeight:v?600:400 }}>{fmtN(v)}</td>
                    </tr>))}
                  </tbody>
                </table>
              )
            })()}
          </div>
        </Overlay>
      )}
      {dateChartView && (
        <Overlay onClose={()=>setDateChartView(null)}>
          <div style={{ width:620, maxWidth:'94vw', background:C.cardBg, borderRadius:12, padding:18, border:`1px solid ${C.cardBorder}` }} onClick={e=>e.stopPropagation()}>
            <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:6 }}>
              <div style={{ fontSize:15, fontWeight:800, color:C.text }}>Date-change history · {dateChartView.st_cd} {dateChartView.site_name?`· ${dateChartView.site_name}`:''}</div>
              <button onClick={()=>setDateChartView(null)} style={{ background:'none', border:'none', cursor:'pointer' }}><X size={18} color={C.textMuted}/></button>
            </div>
            <div style={{ fontSize:12, color:C.textMuted, marginBottom:10 }}>
              1st {fmtD(dateChartView.first_proposed_dt)} → latest {fmtD(dateChartView.latest_proposed_dt)} · {dateChartView.date_given_count||0} given · {dateChartView.date_change_count||0} changed
            </div>
            <StoreDateChart history={dateChartView.date_history} height={240} />
            <div style={{ maxHeight:220, overflowY:'auto', marginTop:10 }}>
              {(dateChartView.date_history||[]).map((h,i)=>(
                <div key={i} style={{ display:'flex', gap:8, padding:'5px 0', borderBottom:`1px solid ${C.cardBorder}`, fontSize:12 }}>
                  <Clock size={12} color={h.changed?C.amber:C.textMuted} style={{ marginTop:2 }} />
                  <div style={{ flex:1 }}>
                    <b style={{ color:C.text }}>{fmtD(h.proposed_opening_dt)}</b>
                    {h.changed && h.prev_proposed_dt && <span style={{ color:C.textMuted }}> (was {fmtD(h.prev_proposed_dt)})</span>}
                    <span style={{ color:C.textMuted }}> · shared {fmtD(h.share_dt)} · {h.changed_by||'—'} · {fmtDT(h.changed_at)}</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </Overlay>
      )}
    </div>
  )
}

// ── Add / Edit modal ──────────────────────────────────────────────────────────
function EditModal({ edit, setEdit, onSave }) {
  const set = (k,v) => setEdit(e => ({ ...e, [k]:v }))
  return (
    <Overlay onClose={()=>setEdit(null)}>
      <div style={{ width:440, maxWidth:'94vw', background:C.cardBg, borderRadius:12, padding:18,
        border:`1px solid ${C.cardBorder}`, maxHeight:'90vh', overflowY:'auto' }} onClick={e=>e.stopPropagation()}>
        <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:12 }}>
          <div style={{ fontSize:15, fontWeight:800, color:C.text }}>
            {edit._isNew ? 'Add Store' : `Edit ${edit.st_cd}`}
          </div>
          <button onClick={()=>setEdit(null)} style={{ background:'none', border:'none', cursor:'pointer' }}><X size={18} color={C.textMuted}/></button>
        </div>
        {edit._isNew && (
          <Field label="ST_CD *">
            <input style={inputStyle} value={edit.st_cd} onChange={e=>set('st_cd', e.target.value)} placeholder="e.g. HK32" />
          </Field>
        )}
        <div style={{ display:'flex', gap:10 }}>
          <div style={{ flex:1 }}><Field label="Proposed opening date">
            <input type="date" style={inputStyle} value={edit.proposed_opening_dt||''} onChange={e=>set('proposed_opening_dt', e.target.value)} />
          </Field></div>
          <div style={{ flex:1 }}><Field label="Share date">
            <input type="date" style={inputStyle} value={edit.share_dt||''} onChange={e=>set('share_dt', e.target.value)} />
          </Field></div>
        </div>
        {!edit._isNew && edit.latest_proposed_dt && (
          <div style={{ fontSize:11, color:C.textMuted, marginTop:-4, marginBottom:8 }}>
            Current latest: {fmtD(edit.latest_proposed_dt)} — a new value appends a change event.
          </div>
        )}
        <Field label="Remarks">
          <textarea style={{ ...inputStyle, minHeight:60, resize:'vertical' }} value={edit.remarks||''}
            onChange={e=>set('remarks', e.target.value)} placeholder="A new remark is recorded in history only if it differs from the last." />
        </Field>
        <div style={{ display:'flex', gap:10 }}>
          <div style={{ flex:1 }}><Field label="Layout received date">
            <input type="date" style={inputStyle} value={edit.layout_rec_dt||''} onChange={e=>set('layout_rec_dt', e.target.value)} />
          </Field></div>
          <div style={{ flex:1 }}><Field label="1st display shared">
            <input type="date" style={inputStyle} value={edit.first_disp_dt||''} onChange={e=>set('first_disp_dt', e.target.value)} />
          </Field></div>
        </div>
        <div style={{ display:'flex', gap:10 }}>
          <div style={{ flex:1 }}><Field label="Status">
            <select style={inputStyle} value={edit.status||'ACTIVE'} onChange={e=>set('status', e.target.value)}>
              {STATUS_ORDER.map(s => <option key={s} value={s}>{STATUS_META[s].label}</option>)}
            </select>
          </Field></div>
          <div style={{ flex:1 }}><Field label="Actual open date">
            <input type="date" style={inputStyle} value={edit.actual_open_dt||''} onChange={e=>set('actual_open_dt', e.target.value)} />
          </Field></div>
        </div>
        <Field label="Priority (number)">
          <input type="number" min="0" step="1" inputMode="numeric" style={inputStyle}
                 value={edit.status_priority||''}
                 onChange={e=>set('status_priority', e.target.value.replace(/[^0-9]/g,''))}
                 placeholder="e.g. 1, 2, 3 …" />
        </Field>
        <label style={{ display:'flex', alignItems:'center', gap:8, fontSize:13, color:C.textSub, margin:'4px 0 2px' }}>
          <input type="checkbox" checked={!!edit.layout_generated} onChange={e=>set('layout_generated', e.target.checked)} />
          Layout generated
        </label>
        <label style={{ display:'flex', alignItems:'center', gap:8, fontSize:13, color:C.textSub, margin:'6px 0 2px' }}>
          <input type="checkbox" checked={!!edit.display_generated} onChange={e=>set('display_generated', e.target.checked)} />
          Display shared
        </label>
        <div style={{ fontSize:11, color:C.textMuted, margin:'0 0 14px 24px' }}>
          Tick <b>Layout generated</b> once the display/planogram layout is prepared (drives the Layout column +
          received date), and <b>Display shared</b> once the store display is first shared (drives the Display
          column + 1st-display date).
        </div>
        <div style={{ display:'flex', justifyContent:'flex-end', gap:8 }}>
          <Btn label="Cancel" tone="ghost" onClick={()=>setEdit(null)} />
          <Btn icon={CheckCircle2} label="Save" tone="primary" onClick={onSave} />
        </div>
      </div>
    </Overlay>
  )
}

// ── Detail drawer (history + SLOC) ─────────────────────────────────────────────
function DetailDrawer({ detail, onClose }) {
  const sloc = Object.entries(detail.sloc||{}).filter(([,v]) => v!=null && v!==0)
  return (
    <Overlay onClose={onClose} align="flex-end">
      <div style={{ width:520, maxWidth:'96vw', height:'100%', background:C.cardBg, padding:18,
        borderLeft:`1px solid ${C.cardBorder}`, overflowY:'auto' }} onClick={e=>e.stopPropagation()}>
        <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:6 }}>
          <div>
            <div style={{ fontSize:16, fontWeight:800, color:C.text }}>{detail.st_cd} · {detail.site_name||'—'}</div>
            <div style={{ fontSize:12, color:C.textMuted }}>{detail.rdc||'—'} / {detail.hub||'—'} · {detail.st_status||''}</div>
          </div>
          <button onClick={onClose} style={{ background:'none', border:'none', cursor:'pointer' }}><X size={18} color={C.textMuted}/></button>
        </div>

        <div style={{ display:'flex', gap:8, flexWrap:'wrap', margin:'10px 0 16px' }}>
          <Tile label="MBQ 100%" value={fmtN(detail.mbq_100)} />
          <Tile label="Total Stock" value={fmtN(detail.total_stock)} />
          <Tile label="Fill Rate" value={detail.fill_rate_pct!=null?`${detail.fill_rate_pct}%`:'—'} />
        </div>

        <SectionTitle icon={CalendarClock} text={`Date history — ${detail.date_given_count||0} given · ${detail.date_change_count||0} changed`} />
        <div style={{ marginBottom:12 }}><StoreDateChart history={detail.date_history} height={170} /></div>
        <div style={{ marginBottom:16 }}>
          {(detail.date_history||[]).map((h,i) => (
            <div key={i} style={{ display:'flex', gap:10, padding:'6px 0', borderBottom:`1px solid ${C.cardBorder}` }}>
              <Clock size={13} color={h.changed?C.amber:C.textMuted} style={{ marginTop:2 }} />
              <div style={{ flex:1 }}>
                <div style={{ fontSize:13, color:C.text }}>
                  <b>{fmtD(h.proposed_opening_dt)}</b>
                  {h.changed && h.prev_proposed_dt && <span style={{ color:C.textMuted }}> (was {fmtD(h.prev_proposed_dt)})</span>}
                  {h.changed && <span style={{ marginLeft:6 }}><Pill tone="warn">changed</Pill></span>}
                </div>
                <div style={{ fontSize:11, color:C.textMuted }}>
                  shared {fmtD(h.share_dt)} · {h.source} · {h.changed_by||'—'} · {fmtDT(h.changed_at)}
                </div>
              </div>
            </div>
          ))}
          {!(detail.date_history||[]).length && <Empty />}
        </div>

        <SectionTitle icon={History} text={`Remark history — ${detail.remarks_change_count||0} change(s)`} />
        <div style={{ marginBottom:16 }}>
          {(detail.remark_history||[]).map((h,i) => (
            <div key={i} style={{ padding:'6px 0', borderBottom:`1px solid ${C.cardBorder}` }}>
              <div style={{ fontSize:13, color:C.text }}>{h.remarks}</div>
              <div style={{ fontSize:11, color:C.textMuted }}>{h.source} · {h.changed_by||'—'} · {fmtDT(h.changed_at)}</div>
            </div>
          ))}
          {!(detail.remark_history||[]).length && <Empty />}
        </div>

        <SectionTitle icon={History} text={`Status history — ${(detail.status_history||[]).length} change(s)`} />
        <div style={{ marginBottom:16 }}>
          {(detail.status_history||[]).map((h,i) => (
            <div key={i} style={{ display:'flex', alignItems:'center', gap:8, padding:'6px 0', borderBottom:`1px solid ${C.cardBorder}` }}>
              <Pill tone={(STATUS_META[h.prev_status]||STATUS_META.ACTIVE).tone}>{STATUS_META[h.prev_status]?.label||h.prev_status||'—'}</Pill>
              <span style={{ color:C.textMuted }}>→</span>
              <Pill tone={(STATUS_META[h.status]||STATUS_META.ACTIVE).tone}>{STATUS_META[h.status]?.label||h.status}</Pill>
              <span style={{ fontSize:11, color:C.textMuted, marginLeft:'auto' }}>{h.changed_by||'—'} · {fmtDT(h.changed_at)}</span>
            </div>
          ))}
          {!(detail.status_history||[]).length && <Empty text="No status changes yet." />}
        </div>

        <SectionTitle icon={Store} text={`SLOC-wise stock${detail.metric_dt?` (as of ${fmtD(detail.metric_dt)})`:''}`} />
        <div style={{ display:'flex', flexWrap:'wrap', gap:6 }}>
          {sloc.map(([k,v]) => (
            <div key={k} style={{ background:C.statBg||C.grayBg, borderRadius:6, padding:'5px 9px', fontSize:12 }}>
              <span style={{ color:C.textMuted }}>{k.replace('STK_','')}</span>{' '}
              <b style={{ color:C.text }}>{fmtN(v)}</b>
            </div>
          ))}
          {!sloc.length && <Empty text="No live stock for this store." />}
        </div>
      </div>
    </Overlay>
  )
}

const SectionTitle = ({ icon:Icon, text }) => (
  <div style={{ display:'flex', alignItems:'center', gap:6, fontSize:12, fontWeight:700, color:C.textSub, margin:'4px 0 6px' }}>
    <Icon size={14} color={C.primary} />{text}
  </div>
)
const Empty = ({ text='No history yet.' }) => <div style={{ fontSize:12, color:C.textMuted, padding:'6px 0' }}>{text}</div>

function Overlay({ children, onClose, align='center' }) {
  return (
    <div onClick={onClose} style={{ position:'fixed', inset:0, background:'rgba(0,0,0,0.4)', zIndex:1000,
      display:'flex', alignItems: align==='flex-end'?'stretch':'center', justifyContent: align==='flex-end'?'flex-end':'center' }}>
      {children}
    </div>
  )
}
