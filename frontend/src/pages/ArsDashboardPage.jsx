/**
 * ArsDashboardPage — unified ARS analytics (rev 3)
 *
 * Tabs:
 *   1. Overview         — 8 charts, every chart has expand-modal + table-view toggle
 *   2. Product Drill    — 4-level dropdown drill; path toggle MJ→ST | ST→MJ
 *                         Reads from ARS_PEND_ALC via /drill/* endpoints so all
 *                         scope filters (Date/Session/MAJ_CAT/Store/RDC/HUB/Status/DIV/SSN)
 *                         actually take effect.
 *   3. Date & Session   — 6-level breadcrumb drill:
 *                         Date → Session → MAJ_CAT → Store → GEN_ART → Article
 *   4. Hold             — quick view + deep-link to /reports/hold
 *   5. Pending Alloc    — filterable pending table
 *   6. Gap Report       — gap rollup + Excel export
 *
 * Global filter bar (URL-synced):
 *   Date, Session, MAJ_CAT, Store, RDC, HUB, Status (OLD/UPC), DIV, SSN, Drill-path
 */
import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useSearchParams, Link } from 'react-router-dom'
import {
  LayoutGrid, RefreshCw, Loader2, ChevronRight, ChevronLeft,
  PackageCheck, AlertTriangle, Lock, Truck, Download, Filter, X, Search,
  Maximize2, BarChart3, Table as TableIcon,
} from 'lucide-react'
import {
  BarChart, Bar, PieChart, Pie, Cell, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer, LineChart, Line,
} from 'recharts'
import toast from 'react-hot-toast'
import { arsDashboardAPI, listingAPI, holdDashboardAPI } from '@/services/api'

/* ─────────────────────────────────────────────────────────────────────────
   Helpers
───────────────────────────────────────────────────────────────────────── */
const fmt = (n) => (n == null || isNaN(n)) ? '-' : Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 })

const STATUS_BADGE = {
  open:    'bg-rose-50 text-rose-700',
  partial: 'bg-amber-50 text-amber-700',
  closed:  'bg-emerald-50 text-emerald-700',
  aged:    'bg-rose-100 text-rose-800',
}
function StatusBadge({ status }) {
  const cls = STATUS_BADGE[status] || 'bg-gray-50 text-gray-700'
  return <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium ${cls}`}>● {status}</span>
}

const PIE_COLORS = ['#4f46e5', '#06b6d4', '#f59e0b', '#10b981', '#ef4444', '#8b5cf6', '#ec4899', '#14b8a6', '#0891b2', '#a855f7']

const EMPTY_SCOPE = { date:'', sid:'', mc:[], werks:[], rdc:[], hub:[], status:[], div:[], ssn:[] }

function scopeParams(scope) {
  const out = {}
  if (scope.date)         out.date    = scope.date
  if (scope.sid)          out.sid     = scope.sid
  if (scope.mc?.length)   out.mc      = scope.mc.join(',')
  if (scope.werks?.length) out.werks  = scope.werks.join(',')
  if (scope.rdc?.length)  out.rdc     = scope.rdc.join(',')
  if (scope.hub?.length)  out.hub     = scope.hub.join(',')
  if (scope.status?.length) out.status = scope.status.join(',')
  if (scope.div?.length)  out.div     = scope.div.join(',')
  if (scope.ssn?.length)  out.ssn     = scope.ssn.join(',')
  return out
}

// Stringify scope into a stable dep value for React.useEffect
function scopeKey(scope) {
  return JSON.stringify(scopeParams(scope))
}

/* ─────────────────────────────────────────────────────────────────────────
   Dropdowns
───────────────────────────────────────────────────────────────────────── */
function Dd({ label, value, onChange, options, disabled }) {
  return (
    <div className="flex items-center gap-1">
      <label className="text-[11px] text-gray-500">{label}</label>
      <select disabled={disabled} value={value || ''} onChange={e => onChange(e.target.value)}
              className="text-xs border border-gray-200 rounded-md px-2 py-1 bg-white disabled:bg-gray-100 disabled:text-gray-400 min-w-[140px]">
        {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </div>
  )
}

/**
 * SearchSelect — single-value searchable dropdown.
 * Used in DrillTab where L1 has 290+ MAJ_CATs; a plain <select> is unusable.
 */
function SearchSelect({ value, onChange, options, placeholder, disabled }) {
  const [open, setOpen] = useState(false)
  const [q,    setQ]    = useState('')
  const ref = useRef()

  useEffect(() => {
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [])

  const filtered = options.filter(o => !q || String(o.label).toLowerCase().includes(q.toLowerCase())).slice(0, 80)
  const current = options.find(o => o.value === value)

  return (
    <div className="relative" ref={ref}>
      <button disabled={disabled} onClick={() => setOpen(o => !o)}
              className={`w-full flex items-center justify-between gap-1 text-xs border border-gray-200 rounded px-2 py-1.5 bg-white text-left ${disabled ? 'bg-gray-100 text-gray-400 cursor-not-allowed' : 'hover:bg-gray-50'}`}>
        <span className={`truncate ${current ? '' : 'text-gray-400'}`}>{current ? current.label : (placeholder || 'Select…')}</span>
        <Search size={11} className="shrink-0 text-gray-400" />
      </button>
      {open && !disabled && (
        <div className="absolute top-full left-0 mt-1 z-30 w-full bg-white border border-gray-200 rounded-md shadow-lg p-1.5">
          <input autoFocus value={q} onChange={e => setQ(e.target.value)} placeholder="search…"
                 className="w-full text-xs border border-gray-200 rounded px-2 py-1 mb-1" />
          <div className="max-h-56 overflow-y-auto">
            {value && (
              <div onClick={() => { onChange(''); setOpen(false); setQ('') }}
                   className="px-2 py-1 text-xs text-rose-600 hover:bg-rose-50 cursor-pointer rounded">× clear selection</div>
            )}
            {filtered.length === 0 && <div className="px-2 py-2 text-[11px] text-gray-400">no match</div>}
            {filtered.map(o => (
              <div key={o.value}
                   onClick={() => { onChange(o.value); setOpen(false); setQ('') }}
                   className={`px-2 py-1 text-xs cursor-pointer rounded ${o.value === value ? 'bg-indigo-50 text-indigo-700 font-medium' : 'hover:bg-gray-50'}`}>
                {o.label}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function DdMulti({ label, values, onChange, options }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const ref = useRef()

  useEffect(() => {
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [])

  const filtered = options.filter(o => !q || String(o).toLowerCase().includes(q.toLowerCase())).slice(0, 50)
  const toggle = (v) => onChange(values.includes(v) ? values.filter(x => x !== v) : [...values, v])

  return (
    <div className="relative" ref={ref}>
      <button onClick={() => setOpen(o => !o)}
              className="flex items-center gap-1 text-xs border border-gray-200 rounded-md px-2 py-1 bg-white hover:bg-gray-50">
        <span className="text-gray-500">{label}</span>
        <span className="font-medium">{values.length === 0 ? 'All' : `${values.length} sel`}</span>
      </button>
      {open && (
        <div className="absolute top-full left-0 mt-1 z-30 w-56 bg-white border border-gray-200 rounded-md shadow-lg p-2">
          <div className="relative mb-1">
            <Search size={11} className="absolute left-2 top-1.5 text-gray-400" />
            <input value={q} onChange={e => setQ(e.target.value)} placeholder="search…"
                   className="w-full text-xs border border-gray-200 rounded pl-6 pr-2 py-1" />
          </div>
          <div className="max-h-48 overflow-y-auto">
            {filtered.length === 0 && <div className="text-[11px] text-gray-400 px-2 py-2">no match</div>}
            {filtered.map(o => (
              <label key={o} className="flex items-center gap-2 px-2 py-1 hover:bg-gray-50 cursor-pointer text-xs">
                <input type="checkbox" checked={values.includes(o)} onChange={() => toggle(o)} />
                <span>{o}</span>
              </label>
            ))}
          </div>
          {values.length > 0 && (
            <button onClick={() => onChange([])} className="mt-1 text-[10px] text-rose-600 hover:text-rose-800">clear</button>
          )}
        </div>
      )}
    </div>
  )
}

/* ─────────────────────────────────────────────────────────────────────────
   Filter bar
───────────────────────────────────────────────────────────────────────── */
function FilterBar({ scope, setScope, config, sessionsForDate, drillPath, setDrillPath }) {
  const set = (k, v) => setScope({ ...scope, [k]: v })
  const hasAny = scope.date || scope.sid || scope.mc.length || scope.werks.length || scope.rdc.length
    || scope.hub.length || scope.status.length || scope.div.length || scope.ssn.length

  return (
    <section className="bg-white border border-gray-200 rounded-xl px-3 py-2.5 mb-3 flex items-center gap-2 flex-wrap shadow-sm">
      <span className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mr-1">
        <Filter size={11} className="inline -mt-0.5 mr-1" /> Filters
      </span>

      <Dd label="Date" value={scope.date} onChange={v => { set('date', v); set('sid', '') }}
          options={[{ value: '', label: 'Last 7 days' }, ...(config.dates || []).map(d => ({ value: d, label: d }))]} />

      <Dd label="Session" value={scope.sid} onChange={v => set('sid', v)}
          disabled={!scope.date}
          options={[{ value: '', label: scope.date ? 'All sessions' : 'Pick a date first' },
                    ...sessionsForDate.map(s => ({ value: s.session_id, label: s.label }))]} />

      <DdMulti label="MAJ_CAT" values={scope.mc}     onChange={v => set('mc',     v)} options={config.maj_cats || []} />
      <DdMulti label="Store"   values={scope.werks}  onChange={v => set('werks',  v)} options={config.stores   || []} />
      <DdMulti label="RDC"     values={scope.rdc}    onChange={v => set('rdc',    v)} options={config.rdcs     || []} />
      {(config.hubs && config.hubs.length > 0) &&
        <DdMulti label="HUB"     values={scope.hub}    onChange={v => set('hub',    v)} options={config.hubs || []} />}
      {(config.statuses && config.statuses.length > 0) &&
        <DdMulti label="Status"  values={scope.status} onChange={v => set('status', v)} options={config.statuses || []} />}
      {(config.divs && config.divs.length > 0) &&
        <DdMulti label="DIV"     values={scope.div}    onChange={v => set('div',    v)} options={config.divs || []} />}
      {(config.ssns && config.ssns.length > 0) &&
        <DdMulti label="SSN"     values={scope.ssn}    onChange={v => set('ssn',    v)} options={config.ssns || []} />}

      {hasAny && (
        <button onClick={() => setScope({ ...EMPTY_SCOPE })}
                className="text-[11px] text-gray-500 hover:text-rose-600 flex items-center gap-1">
          <X size={12} /> clear
        </button>
      )}

      <div className="ml-auto flex items-center gap-2">
        <span className="text-[11px] text-gray-500">Drill</span>
        <div className="inline-flex border border-gray-200 rounded-md overflow-hidden">
          <button onClick={() => setDrillPath('mjst')}
                  className={`px-2.5 py-1 text-[11px] font-medium ${drillPath === 'mjst' ? 'bg-indigo-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
            MAJ_CAT → Store
          </button>
          <button onClick={() => setDrillPath('stmj')}
                  className={`px-2.5 py-1 text-[11px] font-medium ${drillPath === 'stmj' ? 'bg-indigo-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
            Store → MAJ_CAT
          </button>
        </div>
      </div>
    </section>
  )
}

/* ─────────────────────────────────────────────────────────────────────────
   KPI strip
───────────────────────────────────────────────────────────────────────── */
function KpiStrip({ summary, onTabJump }) {
  if (!summary) {
    return (
      <div className="grid grid-cols-4 gap-3 mb-4">
        {[1,2,3,4].map(i => <div key={i} className="bg-white border border-gray-200 rounded-xl p-3 shadow-sm h-[88px] animate-pulse" />)}
      </div>
    )
  }
  const items = [
    { id: 'overview', label: 'Allocated', value: summary.alloc_qty, icon: PackageCheck, accent: 'indigo',
      sub: `${fmt(summary.sessions)} sessions · ${fmt(summary.stores)} stores` },
    { id: 'pending',  label: 'Pending',   value: summary.pend_qty,  icon: Truck, accent: 'amber',
      sub: `${fmt(summary.open_rows)} open rows` },
    { id: 'hold',     label: 'On Hold',   value: summary.hold_qty,  icon: Lock,  accent: 'cyan',
      sub: `${fmt(summary.articles_hold)} articles` },
    { id: 'gap',      label: 'Open Gaps', value: summary.gap_rows,  icon: AlertTriangle, accent: 'rose',
      sub: `${fmt(summary.articles_pend)} articles` },
  ]
  const accents = {
    indigo: 'bg-indigo-50 text-indigo-600', amber: 'bg-amber-50 text-amber-600',
    cyan:   'bg-cyan-50 text-cyan-600',     rose:  'bg-rose-50 text-rose-600',
  }
  return (
    <div className="grid grid-cols-4 gap-3 mb-4">
      {items.map(it => (
        <button key={it.id} onClick={() => onTabJump(it.id)}
                className="text-left bg-white border border-gray-200 rounded-xl p-3 shadow-sm hover:shadow-md transition-shadow">
          <div className="flex items-center gap-2">
            <div className={`p-1.5 rounded-lg ${accents[it.accent]}`}><it.icon size={16} /></div>
            <div className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider">{it.label}</div>
          </div>
          <div className="mt-2 text-2xl font-bold">{fmt(it.value)}</div>
          <div className="text-xs text-gray-500">{it.sub}</div>
        </button>
      ))}
    </div>
  )
}

function Seg({ value, options, onChange }) {
  return (
    <div className="inline-flex border border-gray-200 rounded-md overflow-hidden">
      {options.map(o => (
        <button key={o.value} onClick={() => onChange(o.value)}
                className={`px-2 py-0.5 text-[10px] font-medium ${value === o.value ? 'bg-indigo-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
          {o.label}
        </button>
      ))}
    </div>
  )
}

/* ─────────────────────────────────────────────────────────────────────────
   ExpandableChart — wraps any chart with: expand-to-modal + table-view toggle
───────────────────────────────────────────────────────────────────────── */
function ExpandableChart({
  title, chip, right,
  tableColumns, tableData,
  isLoading = false, isEmpty = false, emptyReason,
  children, height = 200,
}) {
  const [showTable, setShowTable] = useState(false)
  const [zoomed,    setZoomed]    = useState(false)

  // Inline loading / empty placeholder
  const Placeholder = ({ big = false }) => {
    const h = big ? 480 : height
    if (isLoading) return (
      <div style={{ height: h }} className="w-full flex flex-col items-center justify-center gap-2 text-gray-400">
        <Loader2 size={big ? 24 : 18} className="animate-spin text-indigo-500" />
        <span className="text-[11px]">Loading…</span>
      </div>
    )
    return (
      <div style={{ height: h }} className="w-full flex flex-col items-center justify-center gap-1 text-gray-400 px-4 text-center">
        <BarChart3 size={big ? 26 : 20} className="opacity-40" />
        <span className="text-xs font-medium">No data</span>
        {emptyReason && <span className="text-[10px] text-gray-500">{emptyReason}</span>}
      </div>
    )
  }

  const ChartBody = (isLoading || isEmpty) ? <Placeholder /> : (
    <div style={{ height }} className="w-full">
      <ResponsiveContainer width="100%" height="100%">{children}</ResponsiveContainer>
    </div>
  )

  const TableBody = (
    <div style={{ maxHeight: height + 60 }} className="overflow-y-auto">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 sticky top-0">
          <tr>
            {tableColumns.map(c => (
              <th key={c.key} className={`px-2 py-1.5 text-[10px] uppercase tracking-wider text-gray-500 ${c.align || 'text-left'}`}>{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {(!tableData || tableData.length === 0) && (
            <tr><td colSpan={tableColumns.length} className="text-center text-xs text-gray-400 italic py-4">no data</td></tr>
          )}
          {(tableData || []).map((row, i) => (
            <tr key={i} className="border-t border-gray-100">
              {tableColumns.map(c => (
                <td key={c.key} className={`px-2 py-1 ${c.align || ''}`}>
                  {c.render ? c.render(row) : (c.format ? c.format(row[c.key]) : row[c.key])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )

  const noData = isLoading || isEmpty
  const Header = (
    <div className="flex items-center justify-between mb-2 gap-2">
      <div className="text-sm font-semibold truncate">{title}</div>
      {chip && <span className="text-[10px] bg-indigo-50 text-indigo-600 font-semibold px-2 py-0.5 rounded shrink-0">{chip}</span>}
      <div className="ml-auto flex items-center gap-1 shrink-0">
        {right}
        <button onClick={() => setShowTable(s => !s)} disabled={noData}
                title={showTable ? 'Show chart' : 'Show table'}
                className={`p-1 rounded hover:bg-gray-100 disabled:opacity-30 ${showTable ? 'text-indigo-600' : 'text-gray-500'}`}>
          {showTable ? <BarChart3 size={13} /> : <TableIcon size={13} />}
        </button>
        <button onClick={() => setZoomed(true)} disabled={noData}
                title="Expand"
                className="p-1 rounded hover:bg-gray-100 disabled:opacity-30 text-gray-500">
          <Maximize2 size={13} />
        </button>
      </div>
    </div>
  )

  return (
    <>
      <div className="border border-gray-200 rounded-xl p-3 bg-white">
        {Header}
        {noData ? <Placeholder /> : (showTable ? TableBody : ChartBody)}
      </div>

      {zoomed && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-6"
             onClick={() => setZoomed(false)}>
          <div className="bg-white rounded-xl shadow-2xl w-full max-w-5xl max-h-[90vh] overflow-hidden flex flex-col"
               onClick={e => e.stopPropagation()}>
            <div className="px-4 py-3 border-b border-gray-200 flex items-center justify-between">
              <div className="text-base font-semibold">{title}</div>
              <div className="flex items-center gap-2">
                <button onClick={() => setShowTable(s => !s)}
                        className={`text-xs px-3 py-1 rounded border ${showTable ? 'bg-indigo-50 text-indigo-700 border-indigo-200' : 'border-gray-200 text-gray-600 hover:bg-gray-50'}`}>
                  {showTable ? <><BarChart3 size={11} className="inline -mt-0.5 mr-1" />Chart</> : <><TableIcon size={11} className="inline -mt-0.5 mr-1" />Table</>}
                </button>
                <button onClick={() => setZoomed(false)} className="p-1 rounded hover:bg-gray-100"><X size={16} /></button>
              </div>
            </div>
            <div className="flex-1 overflow-y-auto p-4">
              {showTable ? (
                <div className="overflow-y-auto">
                  <table className="w-full text-sm">
                    <thead className="bg-gray-50 sticky top-0">
                      <tr>{tableColumns.map(c => <th key={c.key} className={`px-3 py-2 text-xs uppercase tracking-wider text-gray-500 ${c.align || 'text-left'}`}>{c.label}</th>)}</tr>
                    </thead>
                    <tbody>
                      {(tableData || []).map((row, i) => (
                        <tr key={i} className="border-t border-gray-100">
                          {tableColumns.map(c => (
                            <td key={c.key} className={`px-3 py-1.5 ${c.align || ''}`}>
                              {c.render ? c.render(row) : (c.format ? c.format(row[c.key]) : row[c.key])}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                noData ? <Placeholder big /> :
                <div style={{ height: 480 }}><ResponsiveContainer width="100%" height="100%">{children}</ResponsiveContainer></div>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  )
}

/* ─────────────────────────────────────────────────────────────────────────
   TAB 1 — Overview (8 charts, all wrapped in ExpandableChart)
───────────────────────────────────────────────────────────────────────── */
function OverviewTab({ scope }) {
  const [breakdown,     setBreakdown]   = useState({ by_opt_type: [], by_rdc: [], by_maj_cat: [], by_store: [], by_hub: [], by_status: [], by_div: [], by_ssn: [] })
  const [trend,         setTrend]       = useState([])
  const [trendSessions, setTrendSessions] = useState([])
  const [gapMc,         setGapMc]       = useState([])
  const [holdByRdc,     setHoldByRdc]   = useState([])
  const [mcDir,         setMcDir]       = useState('top')
  const [stDir,         setStDir]       = useState('top')
  // Per-card loading: progressive render so user sees something fast
  const [loading,       setLoading]     = useState({ bk: true, tr: true, ts: true, gp: true, hr: true })

  const sk = scopeKey(scope)

  useEffect(() => {
    let cancel = false
    setLoading({ bk: true, tr: true, ts: true, gp: true, hr: true })

    // Kick off all loads in parallel; render each chart the moment its data arrives.
    arsDashboardAPI.breakdown({ ...scopeParams(scope), limit: 15 }).then(b => {
      if (cancel) return
      const d = b?.data?.data || {}
      const safe = (k) => Array.isArray(d[k]) ? d[k] : []
      setBreakdown({
        by_opt_type: safe('by_opt_type'), by_rdc:    safe('by_rdc'),
        by_maj_cat:  safe('by_maj_cat'),  by_store:  safe('by_store'),
        by_hub:      safe('by_hub'),      by_status: safe('by_status'),
        by_div:      safe('by_div'),      by_ssn:    safe('by_ssn'),
      })
    }).catch(() => {}).finally(() => !cancel && setLoading(l => ({ ...l, bk: false })))

    arsDashboardAPI.trend({ ...scopeParams(scope), days: 7 }).then(t => {
      if (!cancel) setTrend(Array.isArray(t?.data?.data?.items) ? t.data.data.items : [])
    }).catch(() => {}).finally(() => !cancel && setLoading(l => ({ ...l, tr: false })))

    arsDashboardAPI.trendSessions({ ...scopeParams(scope), limit: 12 }).then(ts => {
      if (!cancel) setTrendSessions(Array.isArray(ts?.data?.data?.items) ? ts.data.data.items : [])
    }).catch(() => {}).finally(() => !cancel && setLoading(l => ({ ...l, ts: false })))

    arsDashboardAPI.gap({ ...scopeParams(scope), group_by: 'majcat', limit: 20 }).then(g => {
      if (!cancel) setGapMc(Array.isArray(g?.data?.data?.items) ? g.data.data.items : [])
    }).catch(() => {}).finally(() => !cancel && setLoading(l => ({ ...l, gp: false })))

    arsDashboardAPI.holdByRdc({ only_open: true }).then(h => {
      if (!cancel) setHoldByRdc(Array.isArray(h?.data?.data?.items) ? h.data.data.items : [])
    }).catch(() => {}).finally(() => !cancel && setLoading(l => ({ ...l, hr: false })))

    return () => { cancel = true }
  }, [sk])

  // Pie data
  const pieData = (src) => src.filter(r => Number(r.qty) > 0).map((r, i) => ({
    name: r.name, value: Number(r.qty), color: PIE_COLORS[i % PIE_COLORS.length]
  }))

  const optTypeData = useMemo(() => pieData(breakdown.by_opt_type), [breakdown.by_opt_type])
  const rdcData     = useMemo(() => pieData(breakdown.by_rdc),     [breakdown.by_rdc])
  const statusData  = useMemo(() => pieData(breakdown.by_status),  [breakdown.by_status])
  const divData     = useMemo(() => pieData(breakdown.by_div),     [breakdown.by_div])
  const ssnData     = useMemo(() => pieData(breakdown.by_ssn),     [breakdown.by_ssn])
  const hubData     = useMemo(() => pieData(breakdown.by_hub),     [breakdown.by_hub])

  const mcData = useMemo(() => [...breakdown.by_maj_cat]
    .sort((a, b) => mcDir === 'top' ? Number(b.qty) - Number(a.qty) : Number(a.qty) - Number(b.qty))
    .slice(0, 8).map(r => ({ name: r.name, qty: Number(r.qty) })), [breakdown.by_maj_cat, mcDir])

  const stData = useMemo(() => [...breakdown.by_store]
    .sort((a, b) => stDir === 'top' ? Number(b.qty) - Number(a.qty) : Number(a.qty) - Number(b.qty))
    .slice(0, 8).map(r => ({ name: r.name, qty: Number(r.qty) })), [breakdown.by_store, stDir])

  const trendData = useMemo(() => trend.map(t => ({
    date: t.date?.slice(5) || '', alloc: t.alloc_qty, pend: t.pend_qty
  })), [trend])

  const sessionTrendData = useMemo(() => trendSessions.map(s => ({
    session: s.session_id?.slice(0, 12) || '', alloc: s.alloc_qty, pend: s.pend_qty
  })), [trendSessions])

  // Lightweight per-card spinner overlay
  const CardSpin = () => <div className="h-48 flex items-center justify-center"><Loader2 size={18} className="animate-spin text-indigo-500" /></div>

  // Shared table columns for the pie/bar charts (name + qty)
  const nameQtyCols = [
    { key: 'name', label: 'Name' },
    { key: 'qty',  label: 'Qty', align: 'text-right', format: fmt },
  ]
  // For pies, the chart data uses {name, value}, but the table uses {name, qty}
  const pieTableRows = (src) => src.map(r => ({ name: r.name, qty: r.qty }))

  return (
    <div className="space-y-4">
      {/* Row 1 — OPT_TYPE, RDC, Trend by date */}
      <div className="grid grid-cols-3 gap-4">
        <ExpandableChart title="Alloc by OPT_TYPE" chip="RL / TBC / TBL"
                         isLoading={loading.bk} isEmpty={optTypeData.length === 0}
                         emptyReason="OPT_TYPE comes from ARS_ALLOC_WORKING (current run only) — no active run."
                         tableColumns={nameQtyCols} tableData={pieTableRows(breakdown.by_opt_type)}>
          <PieChart>
            <Pie data={optTypeData} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={70}
                 label={({value, percent}) => `${fmt(value)} (${Math.round(percent*100)}%)`}>
              {optTypeData.map((d, i) => <Cell key={i} fill={d.color} />)}
            </Pie>
            <Tooltip formatter={(v) => fmt(v)} />
            <Legend iconSize={8} wrapperStyle={{ fontSize: 10 }} />
          </PieChart>
        </ExpandableChart>

        <ExpandableChart title="Alloc by RDC" chip="warehouse"
                         isLoading={loading.bk} isEmpty={rdcData.length === 0}
                         emptyReason="No alloc rows in current scope"
                         tableColumns={nameQtyCols} tableData={pieTableRows(breakdown.by_rdc)}>
          <PieChart>
            <Pie data={rdcData} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={70}
                 label={({value, percent}) => `${fmt(value)} (${Math.round(percent*100)}%)`}>
              {rdcData.map((d, i) => <Cell key={i} fill={d.color} />)}
            </Pie>
            <Tooltip formatter={(v) => fmt(v)} />
            <Legend iconSize={8} wrapperStyle={{ fontSize: 10 }} />
          </PieChart>
        </ExpandableChart>

        <ExpandableChart title="Alloc vs Pending — last 7 days" chip="by date"
                         isLoading={loading.tr} isEmpty={trendData.length === 0}
                         emptyReason="No allocations in the last 7 days within scope"
                         tableColumns={[
                           { key:'date', label:'Date' },
                           { key:'alloc', label:'Alloc', align:'text-right', format: fmt },
                           { key:'pend',  label:'Pending', align:'text-right', format: fmt },
                         ]}
                         tableData={trendData}>
          <BarChart data={trendData}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
            <XAxis dataKey="date" fontSize={10} />
            <YAxis fontSize={10} />
            <Tooltip formatter={(v) => fmt(v)} />
            <Legend iconSize={8} wrapperStyle={{ fontSize: 10 }} />
            <Bar dataKey="alloc" name="Alloc" stackId="a" fill="#4f46e5" />
            <Bar dataKey="pend"  name="Pending" stackId="a" fill="#f59e0b" />
          </BarChart>
        </ExpandableChart>
      </div>

      {/* Row 2 — Top MAJ_CAT, Top Stores, Alloc vs Pending by Session */}
      <div className="grid grid-cols-3 gap-4">
        <ExpandableChart title={`${mcDir === 'top' ? 'Top' : 'Bottom'} MAJ_CATs by alloc qty`}
          isLoading={loading.bk} isEmpty={mcData.length === 0}
          emptyReason="No MAJ_CAT alloc rows in current scope"
          right={<Seg value={mcDir} onChange={setMcDir} options={[{value:'top',label:'Top'},{value:'bot',label:'Bot'}]} />}
          tableColumns={nameQtyCols} tableData={mcData}>
          <BarChart data={mcData} layout="vertical" margin={{ top:4, right:30, left:6, bottom:0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
            <XAxis type="number" fontSize={10} />
            <YAxis type="category" dataKey="name" fontSize={10} width={90} interval={0} />
            <Tooltip formatter={(v) => fmt(v)} />
            <Bar dataKey="qty" fill={mcDir === 'top' ? '#4f46e5' : '#9ca3af'} radius={[0,4,4,0]} />
          </BarChart>
        </ExpandableChart>

        <ExpandableChart title={`${stDir === 'top' ? 'Top' : 'Bottom'} Stores by alloc qty`}
          isLoading={loading.bk} isEmpty={stData.length === 0}
          emptyReason="No store alloc rows in current scope"
          right={<Seg value={stDir} onChange={setStDir} options={[{value:'top',label:'Top'},{value:'bot',label:'Bot'}]} />}
          tableColumns={nameQtyCols} tableData={stData}>
          <BarChart data={stData} layout="vertical" margin={{ top:4, right:30, left:6, bottom:0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
            <XAxis type="number" fontSize={10} />
            <YAxis type="category" dataKey="name" fontSize={10} width={70} interval={0} />
            <Tooltip formatter={(v) => fmt(v)} />
            <Bar dataKey="qty" fill={stDir === 'top' ? '#06b6d4' : '#9ca3af'} radius={[0,4,4,0]} />
          </BarChart>
        </ExpandableChart>

        <ExpandableChart title="Alloc vs Pending — by Session" chip={`${trendSessions.length} sessions`}
          isLoading={loading.ts} isEmpty={trendSessions.length === 0}
          emptyReason="No sessions in current scope"
          tableColumns={[
            { key:'session_id', label:'Session' },
            { key:'maj_cat', label:'MAJ_CAT' },
            { key:'alloc_qty', label:'Alloc', align:'text-right', format: fmt },
            { key:'pend_qty', label:'Pending', align:'text-right', format: fmt },
          ]} tableData={trendSessions}>
          <BarChart data={sessionTrendData}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
            <XAxis dataKey="session" fontSize={9} angle={-15} textAnchor="end" height={50} />
            <YAxis fontSize={10} />
            <Tooltip formatter={(v) => fmt(v)} />
            <Legend iconSize={8} wrapperStyle={{ fontSize: 10 }} />
            <Bar dataKey="alloc" name="Alloc" fill="#4f46e5" />
            <Bar dataKey="pend"  name="Pending" fill="#f59e0b" />
          </BarChart>
        </ExpandableChart>
      </div>

      {/* Row 3 — Gap by MAJ_CAT, Status (OLD/UPC), HUB */}
      <div className="grid grid-cols-3 gap-4">
        <ExpandableChart title="Gap qty by MAJ_CAT" chip="PEND_QTY > 0"
          isLoading={loading.gp} isEmpty={gapMc.length === 0}
          emptyReason="No open gaps (PEND_QTY > 0) in current scope"
          tableColumns={[
            { key:'maj_cat', label:'MAJ_CAT' },
            { key:'gap_qty', label:'Gap', align:'text-right', format: fmt },
            { key:'alloc_qty', label:'Alloc', align:'text-right', format: fmt },
            { key:'oldest_days', label:'Oldest (d)', align:'text-right' },
          ]} tableData={gapMc}>
          <BarChart data={gapMc.slice(0,8)} layout="vertical" margin={{ top:4, right:30, left:6, bottom:0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
            <XAxis type="number" fontSize={10} />
            <YAxis type="category" dataKey="maj_cat" fontSize={10} width={90} interval={0} />
            <Tooltip formatter={(v) => fmt(v)} />
            <Bar dataKey="gap_qty" fill="#ef4444" radius={[0,4,4,0]} />
          </BarChart>
        </ExpandableChart>

        <ExpandableChart title="Alloc by Store Status" chip="OLD / UPC"
          isLoading={loading.bk} isEmpty={statusData.length === 0}
          emptyReason="ST_STATUS not populated for stores in scope"
          tableColumns={nameQtyCols} tableData={pieTableRows(breakdown.by_status)}>
          <PieChart>
            <Pie data={statusData} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={70}
                 label={({value, percent}) => `${fmt(value)} (${Math.round(percent*100)}%)`}>
              {statusData.map((d, i) => <Cell key={i} fill={d.color} />)}
            </Pie>
            <Tooltip formatter={(v) => fmt(v)} />
            <Legend iconSize={8} wrapperStyle={{ fontSize: 10 }} />
          </PieChart>
        </ExpandableChart>

        <ExpandableChart title="Alloc by HUB" chip="store grouping"
          isLoading={loading.bk} isEmpty={hubData.length === 0}
          emptyReason="HUB column is empty in Master_ALC_INPUT_ST_MASTER — populate via upload to enable this chart"
          tableColumns={nameQtyCols} tableData={pieTableRows(breakdown.by_hub)}>
          <BarChart data={hubData.map(d => ({ name: d.name, qty: d.value }))} layout="vertical" margin={{ top:4, right:30, left:6, bottom:0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
            <XAxis type="number" fontSize={10} />
            <YAxis type="category" dataKey="name" fontSize={10} width={80} interval={0} />
            <Tooltip formatter={(v) => fmt(v)} />
            <Bar dataKey="qty" fill="#8b5cf6" radius={[0,4,4,0]} />
          </BarChart>
        </ExpandableChart>
      </div>

      {/* Row 4 — Hold INT vs REM by RDC (always shown — important ops chart) */}
      <div className="grid grid-cols-1 gap-4">
        <ExpandableChart title="Hold qty by RDC — Initial vs Remaining" chip={`${holdByRdc.length} RDCs`}
          isLoading={loading.hr} isEmpty={holdByRdc.length === 0}
          emptyReason="No open holds (IS_CLOSED = 0) in ARS_NL_TBL_HOLD_TRACKING — try Hold tab for closed records"
          tableColumns={[
            { key: 'rdc',         label: 'RDC' },
            { key: 'hold_int',    label: 'Initial',   align: 'text-right', format: fmt },
            { key: 'hold_rem',    label: 'Remaining', align: 'text-right', format: fmt },
            { key: 'reduced',     label: 'Reduced',   align: 'text-right', format: fmt },
            { key: 'reduced_pct', label: '% Reduced', align: 'text-right',
              render: (r) => `${r.reduced_pct}%` },
          ]} tableData={holdByRdc}
          height={Math.min(320, 60 + holdByRdc.length * 22)}>
          <BarChart data={holdByRdc} layout="vertical" margin={{ top:4, right:40, left:6, bottom:0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
            <XAxis type="number" fontSize={10} />
            <YAxis type="category" dataKey="rdc" fontSize={10} width={60} interval={0} />
            <Tooltip formatter={(v) => fmt(v)} />
            <Legend iconSize={8} wrapperStyle={{ fontSize: 10 }} />
            <Bar dataKey="hold_int" name="Initial Hold" fill="#0891b2" radius={[0,4,4,0]} />
            <Bar dataKey="hold_rem" name="Remaining"    fill="#f59e0b" radius={[0,4,4,0]} />
          </BarChart>
        </ExpandableChart>
      </div>

      {/* Row 5 — DIV, SSN — always shown, with empty-state messaging */}
      <div className="grid grid-cols-2 gap-4">
        <ExpandableChart title="Alloc by Division (DIV)" chip="product"
          isLoading={loading.bk} isEmpty={divData.length === 0}
          emptyReason="No DIV data — PEND_ALC rows not matched in VW_MASTER_PRODUCT (check MATNR linkage)"
          tableColumns={nameQtyCols} tableData={pieTableRows(breakdown.by_div)}>
          <BarChart data={divData.map(d => ({ name: d.name, qty: d.value }))} layout="vertical" margin={{ top:4, right:30, left:6, bottom:0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
            <XAxis type="number" fontSize={10} />
            <YAxis type="category" dataKey="name" fontSize={10} width={80} interval={0} />
            <Tooltip formatter={(v) => fmt(v)} />
            <Bar dataKey="qty" fill="#10b981" radius={[0,4,4,0]} />
          </BarChart>
        </ExpandableChart>

        <ExpandableChart title="Alloc by Season (SSN)" chip="product"
          isLoading={loading.bk} isEmpty={ssnData.length === 0}
          emptyReason="No SSN data — PEND_ALC rows not matched in VW_MASTER_PRODUCT (check MATNR linkage)"
          tableColumns={nameQtyCols} tableData={pieTableRows(breakdown.by_ssn)}>
          <PieChart>
            <Pie data={ssnData} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={70}
                 label={({value, percent}) => `${fmt(value)} (${Math.round(percent*100)}%)`}>
              {ssnData.map((d, i) => <Cell key={i} fill={d.color} />)}
            </Pie>
            <Tooltip formatter={(v) => fmt(v)} />
            <Legend iconSize={8} wrapperStyle={{ fontSize: 10 }} />
          </PieChart>
        </ExpandableChart>
      </div>
    </div>
  )
}

/* ─────────────────────────────────────────────────────────────────────────
   TAB 2 — Product Drill — wide pivot grid (listing-page style)
   L1: MAJ_CAT × RDC pivot. Click row → L2 stores (flat).
   L2: Stores under MAJ_CAT. Click → L3 OPTs.
   L3: OPTs under (MAJ_CAT, Store). Click → L4 variants.
   L4: Article variants.
───────────────────────────────────────────────────────────────────────── */
function PivotGrid({ data, filter, onRowClick }) {
  const { rdcs = [], items = [], totals = {} } = data || {}
  const filtered = useMemo(() => {
    if (!filter) return items
    const q = filter.toLowerCase()
    return items.filter(it => it.maj_cat.toLowerCase().includes(q))
  }, [items, filter])

  // Heatmap-ish colouring helpers
  const pctClass = (p) => p >= 80 ? 'text-emerald-700 font-semibold'
                       : p >= 50 ? 'text-emerald-600'
                       : p >= 25 ? 'text-amber-600'
                       : p >  0  ? 'text-rose-600'
                       :           'text-gray-300'
  const pendClass = (p) => p > 0 ? 'text-amber-700 font-semibold' : 'text-gray-400'

  // Per-RDC sub-columns
  const subCols = [
    { k: 'alloc',    label: 'ALLOC' },
    { k: 'do_qty',   label: 'DO' },
    { k: 'pend',     label: 'PEND' },
    { k: 'pend_pct', label: '%PEND', pct: true },
    { k: 'stores',   label: 'ST' },
  ]
  const totSubCols = [
    { k: 'alloc',    label: 'ALLOC' },
    { k: 'do_qty',   label: 'DO' },
    { k: 'pend',     label: 'PEND' },
    { k: 'pend_pct', label: '%PEND', pct: true },
    { k: 'fill_pct', label: '%FILL', pct: true },
    { k: 'stores',   label: 'ST' },
    { k: 'articles', label: 'ART' },
  ]

  return (
    <div className="border border-gray-200 rounded-lg bg-white overflow-auto" style={{ maxHeight: 'calc(100vh - 380px)' }}>
      <table className="text-[11px] border-collapse" style={{ minWidth: '100%' }}>
        <thead className="bg-gray-50 sticky top-0 z-20">
          {/* RDC group header row */}
          <tr>
            <th rowSpan={2} className="px-2 py-1.5 text-left text-[10px] uppercase tracking-wider text-gray-500 sticky left-0 bg-gray-50 z-30 border-b border-r border-gray-200" style={{ minWidth: 36 }}>#</th>
            <th rowSpan={2} className="px-2 py-1.5 text-left text-[10px] uppercase tracking-wider text-gray-500 sticky left-9 bg-gray-50 z-30 border-b border-r border-gray-200" style={{ minWidth: 180 }}>MAJ_CAT</th>
            {rdcs.map(rdc => (
              <th key={rdc} colSpan={subCols.length} className="px-2 py-1.5 text-center text-[10px] uppercase tracking-wider text-indigo-700 bg-indigo-50 border-l border-r border-gray-200">
                {rdc}
              </th>
            ))}
            <th colSpan={totSubCols.length} className="px-2 py-1.5 text-center text-[10px] uppercase tracking-wider text-gray-800 bg-amber-50 border-l border-gray-200">TOTAL</th>
          </tr>
          <tr>
            {rdcs.map(rdc => subCols.map(sc => (
              <th key={`${rdc}.${sc.k}`} className="px-1.5 py-1 text-right text-[9px] font-semibold text-gray-500 border-b border-gray-200 bg-gray-50">{sc.label}</th>
            )))}
            {totSubCols.map(sc => (
              <th key={`tot.${sc.k}`} className="px-1.5 py-1 text-right text-[9px] font-semibold text-gray-700 border-b border-gray-200 bg-amber-50">{sc.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {filtered.length === 0 && (
            <tr><td colSpan={2 + rdcs.length * subCols.length + totSubCols.length} className="text-center text-xs text-gray-400 py-6">no rows</td></tr>
          )}
          {filtered.map((row, i) => (
            <tr key={row.maj_cat} onClick={() => onRowClick(row)}
                className="hover:bg-indigo-50 cursor-pointer border-b border-gray-100">
              <td className="px-2 py-1 text-gray-400 sticky left-0 bg-white border-r border-gray-200">{i + 1}</td>
              <td className="px-2 py-1 font-medium text-indigo-700 sticky left-9 bg-white border-r border-gray-200 truncate" title={row.maj_cat}>{row.maj_cat}</td>
              {rdcs.map(rdc => {
                const c = row.by_rdc[rdc] || {}
                return subCols.map(sc => {
                  const v = c[sc.k]
                  const cls = sc.pct ? pctClass(v || 0) :
                              sc.k === 'pend' ? pendClass(v || 0) : 'text-gray-700'
                  return (
                    <td key={`${row.maj_cat}.${rdc}.${sc.k}`} className={`px-1.5 py-1 text-right font-mono ${cls}`}>
                      {v == null ? <span className="text-gray-200">—</span> : (sc.pct ? `${v}%` : fmt(v))}
                    </td>
                  )
                })
              })}
              {totSubCols.map(sc => {
                const v = row.tot[sc.k]
                const cls = sc.pct ? pctClass(v || 0) :
                            sc.k === 'pend' ? pendClass(v || 0) : 'text-gray-900 font-semibold'
                return (
                  <td key={`${row.maj_cat}.tot.${sc.k}`} className={`px-1.5 py-1 text-right font-mono bg-amber-50/30 ${cls}`}>
                    {v == null ? '—' : (sc.pct ? `${v}%` : fmt(v))}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
        {filtered.length > 0 && (
          <tfoot className="bg-gray-100 sticky bottom-0 z-10">
            <tr>
              <td colSpan={2} className="px-2 py-1.5 text-[10px] font-bold text-gray-600 sticky left-0 bg-gray-100 border-r border-gray-200">TOTAL ({fmt(filtered.length)} MAJ_CATs)</td>
              <td colSpan={rdcs.length * subCols.length} className="px-2 py-1.5 text-[10px] text-gray-400 text-center">—</td>
              {totSubCols.map(sc => {
                const v = totals[sc.k]
                return (
                  <td key={`gtot.${sc.k}`} className="px-1.5 py-1 text-right font-mono font-bold text-gray-900 bg-amber-100">
                    {v == null ? '—' : (sc.pct ? `${v}%` : fmt(v))}
                  </td>
                )
              })}
            </tr>
          </tfoot>
        )}
      </table>
    </div>
  )
}

/* Flat drill table — for L2/L3/L4 (after a MAJ_CAT is picked) */
function FlatDrillTable({ rows, columns, onRowClick, emptyText }) {
  return (
    <div className="border border-gray-200 rounded-lg bg-white overflow-auto" style={{ maxHeight: 'calc(100vh - 380px)' }}>
      <table className="w-full text-sm">
        <thead className="bg-gray-50 sticky top-0">
          <tr>{columns.map(c => <th key={c.k} className={`px-3 py-2 text-[10px] uppercase tracking-wider text-gray-500 ${c.align === 'right' ? 'text-right' : 'text-left'}`}>{c.l}</th>)}</tr>
        </thead>
        <tbody>
          {rows.length === 0 && <tr><td colSpan={columns.length} className="text-center text-xs text-gray-400 py-6">{emptyText || 'no rows'}</td></tr>}
          {rows.map((r, i) => (
            <tr key={i} onClick={() => onRowClick && onRowClick(r)}
                className={`border-t border-gray-100 ${onRowClick ? 'hover:bg-indigo-50 cursor-pointer' : ''}`}>
              {columns.map(c => {
                const v = r[c.k]
                const isWarn = c.warn && Number(v) > 0
                return (
                  <td key={c.k} className={`px-3 py-1.5 ${c.align === 'right' ? 'text-right font-mono' : ''} ${c.cls || ''} ${isWarn ? 'text-amber-700 font-semibold' : ''}`}>
                    {c.render ? c.render(r) : (c.fmt ? fmt(v) : (v ?? '—'))}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DrillTab({ scope, drillPath }) {
  // State for the drill path
  const [crumb, setCrumb] = useState({ maj_cat: '', st_cd: '', gen_art: '', clr: '' })

  // Each level loads from its own endpoint
  const [pivot,     setPivot]     = useState({ rdcs: [], items: [], totals: {} })
  const [stores,    setStores]    = useState([])
  const [genArts,   setGenArts]   = useState([])
  const [articles,  setArticles]  = useState([])
  const [filter,    setFilter]    = useState('')
  const [busy,      setBusy]      = useState(false)

  const sk = scopeKey(scope)
  const level = crumb.gen_art ? 4 : crumb.st_cd ? 3 : crumb.maj_cat ? 2 : 1

  // Load when crumb or scope changes
  useEffect(() => {
    let cancel = false
    setBusy(true)
    let p
    if (level === 1) {
      p = arsDashboardAPI.pivotMajCatRdc(scopeParams(scope))
       .then(r => { if (!cancel) setPivot(r?.data?.data || { rdcs: [], items: [], totals: {} }) })
    } else if (level === 2) {
      p = arsDashboardAPI.drillStores({ ...scopeParams(scope), mc: crumb.maj_cat })
       .then(r => { if (!cancel) setStores(Array.isArray(r?.data?.data?.items) ? r.data.data.items : []) })
    } else if (level === 3) {
      p = arsDashboardAPI.drillGenArts({ ...scopeParams(scope), mc: crumb.maj_cat, werks: crumb.st_cd })
       .then(r => { if (!cancel) setGenArts(Array.isArray(r?.data?.data?.items) ? r.data.data.items : []) })
    } else {
      p = arsDashboardAPI.drillArticles({ ...scopeParams(scope), mc: crumb.maj_cat, werks: crumb.st_cd, gen_art: crumb.gen_art, clr: crumb.clr })
       .then(r => { if (!cancel) setArticles(Array.isArray(r?.data?.data?.items) ? r.data.data.items : []) })
    }
    p.catch(() => {}).finally(() => !cancel && setBusy(false))
    return () => { cancel = true }
  }, [level, crumb.maj_cat, crumb.st_cd, crumb.gen_art, crumb.clr, sk])

  // Reset to L1 when scope or path changes
  useEffect(() => {
    setCrumb({ maj_cat: '', st_cd: '', gen_art: '', clr: '' })
    setFilter('')
  }, [sk, drillPath])

  // Drill into a row
  const drillInto = (row) => {
    if (level === 1) setCrumb({ ...crumb, maj_cat: row.maj_cat })
    else if (level === 2) setCrumb({ ...crumb, st_cd: row.name })
    else if (level === 3) setCrumb({ ...crumb, gen_art: String(row.gen_art_number), clr: row.clr || '' })
    setFilter('')
  }

  // Climb breadcrumb
  const goLevel = (lv) => {
    const c = { ...crumb }
    if (lv < 2) c.maj_cat = ''
    if (lv < 3) c.st_cd = ''
    if (lv < 4) { c.gen_art = ''; c.clr = '' }
    setCrumb(c)
    setFilter('')
  }

  // Excel export — only for L1 (the pivot grid)
  const doExport = () => {
    // Re-aggregate via the gap endpoint's export? Simplest: build CSV client-side from current pivot data
    const { rdcs, items } = pivot
    if (!items.length) return
    const headerRdc = rdcs.flatMap(r => [`${r} ALLOC`, `${r} DO`, `${r} PEND`, `${r} %PEND`, `${r} ST`])
    const headers = ['#', 'MAJ_CAT', ...headerRdc,
                     'TOT ALLOC', 'TOT DO', 'TOT PEND', 'TOT %PEND', 'TOT %FILL', 'TOT ST', 'TOT ART']
    const lines = [headers.join(',')]
    items.forEach((row, i) => {
      const cells = [i + 1, row.maj_cat]
      rdcs.forEach(r => {
        const c = row.by_rdc[r] || {}
        cells.push(c.alloc ?? '', c.do_qty ?? '', c.pend ?? '', c.pend_pct != null ? c.pend_pct + '%' : '', c.stores ?? '')
      })
      const t = row.tot
      cells.push(t.alloc, t.do_qty, t.pend, t.pend_pct + '%', t.fill_pct + '%', t.stores, t.articles)
      lines.push(cells.map(v => `"${v ?? ''}"`).join(','))
    })
    const blob = new Blob([lines.join('\n')], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = 'ars_dashboard_pivot.csv'; a.click()
    URL.revokeObjectURL(url)
  }

  // Header KPI strip — totals from pivot (L1) or from current rows
  const headerKpi = useMemo(() => {
    if (level === 1) {
      const t = pivot.totals || {}
      return { alloc: t.alloc || 0, do: t.do_qty || 0, pend: t.pend || 0, rows: pivot.items?.length || 0 }
    }
    const rows = level === 2 ? stores : level === 3 ? genArts : articles
    return {
      alloc: rows.reduce((s, r) => s + Number(r.alloc_qty || 0), 0),
      do:    rows.reduce((s, r) => s + Number(r.do_qty    || 0), 0),
      pend:  rows.reduce((s, r) => s + Number(r.pend_qty  || 0), 0),
      rows:  rows.length,
    }
  }, [level, pivot, stores, genArts, articles])

  const Crumb = ({ label, onClick, active }) => (
    <button onClick={onClick} className={active ? 'text-gray-900 font-semibold cursor-default' : 'text-indigo-600 hover:underline cursor-pointer'}>
      {label}
    </button>
  )

  // Flat-table column configs
  const storeCols = [
    { k: 'name',      l: 'Store',    cls: 'text-indigo-700 font-medium' },
    { k: 'hub',       l: 'HUB' },
    { k: 'status',    l: 'St Status' },
    { k: 'articles',  l: 'Articles', align: 'right', fmt: true },
    { k: 'alloc_qty', l: 'Alloc',    align: 'right', fmt: true },
    { k: 'do_qty',    l: 'DO',       align: 'right', fmt: true },
    { k: 'pend_qty',  l: 'Pending',  align: 'right', fmt: true, warn: true },
    { k: 'rows_n',    l: 'Rows',     align: 'right', fmt: true },
  ]
  const genArtCols = [
    { k: 'name',      l: 'GEN_ART · CLR', cls: 'text-indigo-700 font-medium font-mono text-[11px]' },
    { k: 'articles',  l: 'Variants', align: 'right', fmt: true },
    { k: 'stores',    l: 'Stores',   align: 'right', fmt: true },
    { k: 'alloc_qty', l: 'Alloc',    align: 'right', fmt: true },
    { k: 'do_qty',    l: 'DO',       align: 'right', fmt: true },
    { k: 'pend_qty',  l: 'Pending',  align: 'right', fmt: true, warn: true },
  ]
  const articleCols = [
    { k: 'article_number', l: 'Article', cls: 'font-mono text-[11px]' },
    { k: 'st_cd',          l: 'Store' },
    { k: 'rdc',            l: 'RDC' },
    { k: 'alloc_qty',      l: 'Alloc',   align: 'right', fmt: true },
    { k: 'do_qty',         l: 'DO',      align: 'right', fmt: true },
    { k: 'pend_qty',       l: 'Pending', align: 'right', fmt: true, warn: true },
    { k: 'approved_at',    l: 'Approved', cls: 'text-[11px] text-gray-500',
      render: (r) => r.approved_at ? r.approved_at.slice(0, 10) : '—' },
  ]

  const filterLower = filter.toLowerCase()
  const filteredStores   = useMemo(() => filter ? stores  .filter(r => String(r.name)?.toLowerCase().includes(filterLower)) : stores,   [stores,   filter])
  const filteredGenArts  = useMemo(() => filter ? genArts .filter(r => String(r.name)?.toLowerCase().includes(filterLower)) : genArts,  [genArts,  filter])
  const filteredArticles = useMemo(() => filter ? articles.filter(r => String(r.article_number).toLowerCase().includes(filterLower)) : articles, [articles, filter])

  return (
    <div className="space-y-3">
      {/* KPI summary strip + breadcrumb */}
      <div className="flex items-center flex-wrap gap-3 px-3 py-2 bg-white border border-gray-200 rounded-lg">
        <div className="flex items-center gap-1 text-xs flex-wrap">
          <Crumb label={drillPath === 'mjst' ? 'MAJ_CAT' : 'Store'} onClick={() => goLevel(1)} active={level === 1} />
          {crumb.maj_cat && <><ChevronRight size={11} className="text-gray-400" /><Crumb label={crumb.maj_cat} onClick={() => goLevel(2)} active={level === 2} /></>}
          {crumb.st_cd && <><ChevronRight size={11} className="text-gray-400" /><Crumb label={crumb.st_cd} onClick={() => goLevel(3)} active={level === 3} /></>}
          {crumb.gen_art && <><ChevronRight size={11} className="text-gray-400" /><Crumb label={`${crumb.gen_art}${crumb.clr ? ' · ' + crumb.clr : ''}`} onClick={() => goLevel(4)} active={level === 4} /></>}
        </div>
        <div className="ml-auto flex items-center gap-4 text-[11px] text-gray-700">
          <span>Rows <b className="text-gray-900">{fmt(headerKpi.rows)}</b></span>
          <span>Alloc <b className="text-indigo-700">{fmt(headerKpi.alloc)}</b></span>
          <span>DO <b className="text-gray-900">{fmt(headerKpi.do)}</b></span>
          <span>Pend <b className="text-amber-700">{fmt(headerKpi.pend)}</b></span>
        </div>
      </div>

      {/* Toolbar — filter + export */}
      <div className="flex items-center gap-2">
        <div className="relative">
          <Search size={12} className="absolute left-2 top-2 text-gray-400" />
          <input value={filter} onChange={e => setFilter(e.target.value)}
                 placeholder={level === 1 ? 'Filter MAJ_CAT…' : level === 2 ? 'Filter store…' : level === 3 ? 'Filter GEN_ART/CLR…' : 'Filter article…'}
                 className="text-xs border border-gray-200 rounded pl-7 pr-2 py-1.5 bg-white w-64" />
        </div>
        <span className="text-[11px] text-gray-500">Level <b>{level}</b> · {['MAJ_CAT × RDC pivot','Stores','OPT (GEN_ART · CLR)','Article variants'][level-1]}</span>
        {busy && <Loader2 size={12} className="animate-spin text-indigo-500" />}
        {level === 1 && (
          <button onClick={doExport}
                  className="ml-auto bg-emerald-600 text-white text-xs px-3 py-1.5 rounded hover:bg-emerald-700 flex items-center gap-1">
            <Download size={12} /> Excel
          </button>
        )}
      </div>

      {/* Grid / table */}
      {level === 1 && <PivotGrid data={pivot} filter={filter} onRowClick={drillInto} />}
      {level === 2 && <FlatDrillTable rows={filteredStores}   columns={storeCols}   onRowClick={drillInto} emptyText="no stores in scope" />}
      {level === 3 && <FlatDrillTable rows={filteredGenArts}  columns={genArtCols}  onRowClick={drillInto} emptyText="no OPTs in scope" />}
      {level === 4 && <FlatDrillTable rows={filteredArticles} columns={articleCols} onRowClick={null}       emptyText="no variants" />}
    </div>
  )
}


/**
 * LevelColumn — clickable scrollable list panel for the Product Drill tab.
 * Each item: name + alloc qty + pend chip.
 * Search-as-you-type filters in place. Click a row → onPick(value).
 */
function LevelColumn({ idx, title, chip, items, value, onPick, disabled, loading, getValue, getLabel, getQty, getPend, getMeta }) {
  const [q, setQ] = useState('')
  const filtered = useMemo(() => {
    if (!q) return items
    const lq = q.toLowerCase()
    return items.filter(it => String(getLabel(it)).toLowerCase().includes(lq))
  }, [items, q, getLabel])

  return (
    <div className={`border border-gray-200 rounded-xl bg-white flex flex-col ${disabled ? 'opacity-50' : ''}`} style={{ minHeight: 340, maxHeight: 420 }}>
      {/* Header */}
      <div className="px-3 py-2 border-b border-gray-100 flex items-center justify-between">
        <div className="text-[10px] font-bold text-gray-500 uppercase tracking-wider">L{idx} · {title}</div>
        <span className="text-[10px] bg-indigo-50 text-indigo-600 font-semibold px-2 py-0.5 rounded">{chip}</span>
      </div>
      {/* Search */}
      <div className="px-2 pt-2">
        <div className="relative">
          <Search size={11} className="absolute left-2 top-1.5 text-gray-400" />
          <input value={q} disabled={disabled || items.length === 0}
                 onChange={e => setQ(e.target.value)} placeholder="search…"
                 className="w-full text-xs border border-gray-200 rounded pl-6 pr-2 py-1 bg-white disabled:bg-gray-50" />
        </div>
      </div>
      {/* List */}
      <div className="flex-1 overflow-y-auto px-1 py-1 mt-1">
        {loading && (
          <div className="h-full flex items-center justify-center text-gray-400 text-[11px] gap-1">
            <Loader2 size={12} className="animate-spin" /> loading…
          </div>
        )}
        {!loading && disabled && (
          <div className="h-full flex items-center justify-center text-gray-400 text-[11px] italic px-3 text-center">
            pick L{idx - 1} first
          </div>
        )}
        {!loading && !disabled && items.length === 0 && (
          <div className="h-full flex items-center justify-center text-gray-400 text-[11px] italic">no items</div>
        )}
        {!loading && !disabled && filtered.map((it, i) => {
          const v = getValue(it)
          const lbl = getLabel(it)
          const qty = getQty(it)
          const pend = getPend ? getPend(it) : 0
          const meta = getMeta ? getMeta(it) : ''
          const sel = String(v) === String(value)
          return (
            <button key={String(v) + i} onClick={() => onPick(v, it)}
                    className={`w-full text-left px-2 py-1.5 rounded text-xs mb-0.5 transition-colors ${sel ? 'bg-indigo-100 border border-indigo-300 text-indigo-900 font-medium' : 'hover:bg-gray-50 border border-transparent'}`}>
              <div className="flex items-center justify-between gap-1">
                <span className="truncate" title={lbl}>{lbl}</span>
                <span className={`font-mono shrink-0 ${sel ? 'text-indigo-700' : 'text-gray-700'}`}>{fmt(qty)}</span>
              </div>
              {(pend > 0 || meta) && (
                <div className="flex items-center justify-between mt-0.5 text-[10px] text-gray-500">
                  <span className="truncate">{meta}</span>
                  {pend > 0 && <span className="text-amber-700">pend {fmt(pend)}</span>}
                </div>
              )}
            </button>
          )
        })}
        {!loading && !disabled && filtered.length === 0 && items.length > 0 && (
          <div className="text-[11px] text-gray-400 italic px-2 py-2">no match for "{q}"</div>
        )}
      </div>
    </div>
  )
}

/* ─────────────────────────────────────────────────────────────────────────
   TAB 3 — Date & Session  →  6-level breadcrumb drill
   Levels: Date  →  Session  →  MAJ_CAT  →  Store  →  GEN_ART  →  Article
───────────────────────────────────────────────────────────────────────── */
function DateSessionTab({ scope }) {
  const [level, setLevel] = useState(1)
  const [crumbs, setCrumbs] = useState({
    date: '', sid: '', maj_cat: '', st_cd: '', gen_art: '', clr: '',
  })
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(false)

  const sk = scopeKey(scope)

  // Helper: build scope+crumb override for the given level
  const buildScope = (lv) => {
    const s = { ...scopeParams(scope) }
    if (lv >= 2 && crumbs.date)    s.date    = crumbs.date
    if (lv >= 3 && crumbs.sid)     s.sid     = crumbs.sid
    if (lv >= 4 && crumbs.maj_cat) s.mc      = crumbs.maj_cat
    if (lv >= 5 && crumbs.st_cd)   s.werks   = crumbs.st_cd
    if (lv >= 6 && crumbs.gen_art) { s.gen_art = crumbs.gen_art; s.clr = crumbs.clr || '' }
    return s
  }

  // Loader at each level
  useEffect(() => {
    setLoading(true)
    let p
    if (level === 1)      p = arsDashboardAPI.dates(scopeParams(scope))
    else if (level === 2) p = arsDashboardAPI.sessions({ date: crumbs.date })
    else if (level === 3) p = arsDashboardAPI.drillMajCats(buildScope(3))
    else if (level === 4) p = arsDashboardAPI.drillStores(buildScope(4))
    else if (level === 5) p = arsDashboardAPI.drillGenArts(buildScope(5))
    else                  p = arsDashboardAPI.drillArticles(buildScope(6))
    p.then(r => setRows(Array.isArray(r?.data?.data?.items) ? r.data.data.items : []))
     .catch(() => setRows([]))
     .finally(() => setLoading(false))
  }, [level, sk, crumbs.date, crumbs.sid, crumbs.maj_cat, crumbs.st_cd, crumbs.gen_art, crumbs.clr])

  // Reset deeper crumbs when going up
  const goLevel = (lv) => {
    setCrumbs(prev => {
      const c = { ...prev }
      if (lv < 2) c.date = ''
      if (lv < 3) c.sid = ''
      if (lv < 4) c.maj_cat = ''
      if (lv < 5) c.st_cd = ''
      if (lv < 6) { c.gen_art = ''; c.clr = '' }
      return c
    })
    setLevel(lv)
  }

  // Click row → next level
  const onRowClick = (row) => {
    if (level === 1) { setCrumbs(c => ({ ...c, date: row.date })); setLevel(2) }
    else if (level === 2) { setCrumbs(c => ({ ...c, sid: row.session_id })); setLevel(3) }
    else if (level === 3) { setCrumbs(c => ({ ...c, maj_cat: row.name })); setLevel(4) }
    else if (level === 4) { setCrumbs(c => ({ ...c, st_cd: row.name })); setLevel(5) }
    else if (level === 5) { setCrumbs(c => ({ ...c, gen_art: String(row.gen_art_number), clr: row.clr || '' })); setLevel(6) }
  }

  // Breadcrumb pieces
  const Crumb = ({ label, onClick, active }) => (
    <button onClick={onClick} className={active ? 'text-gray-900 font-medium cursor-default' : 'text-indigo-600 hover:underline'}>
      {label}
    </button>
  )

  // Table column config per level
  const cols = useMemo(() => {
    if (level === 1) return [
      { k: 'date', l: 'Date', cls: 'text-indigo-600 font-medium' },
      { k: 'sessions', l: 'Sessions', align: 'right' },
      { k: 'alloc_qty', l: 'Alloc', align: 'right', fmt: true },
      { k: 'do_qty', l: 'DO', align: 'right', fmt: true },
      { k: 'pend_qty', l: 'Pending', align: 'right', fmt: true, warn: true },
      { k: 'stores', l: 'Stores', align: 'right' },
      { k: 'status', l: 'Status', badge: true },
    ]
    if (level === 2) return [
      { k: 'session_id', l: 'Session', cls: 'text-indigo-600 font-medium' },
      { k: 'maj_cat', l: 'MAJ_CAT' },
      { k: 'rdc', l: 'RDC' },
      { k: 'articles', l: 'Articles', align: 'right' },
      { k: 'stores', l: 'Stores', align: 'right' },
      { k: 'alloc_qty', l: 'Alloc', align: 'right', fmt: true },
      { k: 'do_qty', l: 'DO', align: 'right', fmt: true },
      { k: 'pend_qty', l: 'Pending', align: 'right', fmt: true, warn: true },
      { k: 'mode', l: 'Mode' },
      { k: 'status', l: 'Status', badge: true },
    ]
    if (level === 3) return [
      { k: 'name', l: 'MAJ_CAT', cls: 'text-indigo-600 font-medium' },
      { k: 'stores', l: 'Stores', align: 'right' },
      { k: 'articles', l: 'Articles', align: 'right' },
      { k: 'alloc_qty', l: 'Alloc', align: 'right', fmt: true },
      { k: 'do_qty', l: 'DO', align: 'right', fmt: true },
      { k: 'pend_qty', l: 'Pending', align: 'right', fmt: true, warn: true },
    ]
    if (level === 4) return [
      { k: 'name', l: 'Store', cls: 'text-indigo-600 font-medium' },
      { k: 'hub', l: 'HUB' },
      { k: 'status', l: 'St Status' },
      { k: 'articles', l: 'Articles', align: 'right' },
      { k: 'alloc_qty', l: 'Alloc', align: 'right', fmt: true },
      { k: 'do_qty', l: 'DO', align: 'right', fmt: true },
      { k: 'pend_qty', l: 'Pending', align: 'right', fmt: true, warn: true },
    ]
    if (level === 5) return [
      { k: 'name', l: 'GEN_ART · CLR', cls: 'text-indigo-600 font-medium' },
      { k: 'articles', l: 'Variants', align: 'right' },
      { k: 'alloc_qty', l: 'Alloc', align: 'right', fmt: true },
      { k: 'do_qty', l: 'DO', align: 'right', fmt: true },
      { k: 'pend_qty', l: 'Pending', align: 'right', fmt: true, warn: true },
    ]
    return [
      { k: 'article_number', l: 'Article', cls: 'font-mono text-[11px]' },
      { k: 'st_cd', l: 'Store' },
      { k: 'rdc', l: 'RDC' },
      { k: 'alloc_qty', l: 'Alloc', align: 'right', fmt: true },
      { k: 'do_qty', l: 'DO', align: 'right', fmt: true },
      { k: 'pend_qty', l: 'Pending', align: 'right', fmt: true, warn: true },
      { k: 'approved_at', l: 'Approved', cls: 'text-[11px] text-gray-500',
        fn: (v) => v ? String(v).slice(0,10) : '—' },
    ]
  }, [level])

  return (
    <div>
      {/* Breadcrumb */}
      <div className="text-xs text-gray-500 mb-3 flex items-center flex-wrap gap-1">
        <Crumb label="All Dates" onClick={() => goLevel(1)} active={level === 1} />
        {crumbs.date    && <><ChevronRight size={11} /><Crumb label={crumbs.date}    onClick={() => goLevel(2)} active={level === 2} /></>}
        {crumbs.sid     && <><ChevronRight size={11} /><Crumb label={crumbs.sid}     onClick={() => goLevel(3)} active={level === 3} /></>}
        {crumbs.maj_cat && <><ChevronRight size={11} /><Crumb label={crumbs.maj_cat} onClick={() => goLevel(4)} active={level === 4} /></>}
        {crumbs.st_cd   && <><ChevronRight size={11} /><Crumb label={crumbs.st_cd}   onClick={() => goLevel(5)} active={level === 5} /></>}
        {crumbs.gen_art && <><ChevronRight size={11} /><Crumb label={`${crumbs.gen_art}${crumbs.clr ? ' · ' + crumbs.clr : ''}`} onClick={() => goLevel(6)} active={level === 6} /></>}
      </div>

      <div className="text-[11px] text-gray-500 mb-2">
        Level <b>{level}</b> — {['Dates','Sessions on date','MAJ_CATs in session','Stores in MAJ_CAT','GEN_ART/CLR in store','Variants in OPT'][level-1]}
        · <span className="font-mono">{rows.length}</span> rows
      </div>

      {loading ? <Loading /> : (
        <table className="w-full bg-white border border-gray-200 rounded-lg overflow-hidden text-sm">
          <thead className="bg-gray-50 text-[10px] uppercase tracking-wider text-gray-500">
            <tr>{cols.map(c => <th key={c.k} className={`px-3 py-2 ${c.align === 'right' ? 'text-right' : 'text-left'}`}>{c.l}</th>)}</tr>
          </thead>
          <tbody>
            {rows.length === 0 && <tr><td colSpan={cols.length} className="px-3 py-6 text-center text-xs text-gray-400">no rows at this level</td></tr>}
            {rows.map((row, i) => (
              <tr key={i} onClick={() => level < 6 && onRowClick(row)}
                  className={`border-t border-gray-100 ${level < 6 ? 'hover:bg-gray-50 cursor-pointer' : ''}`}>
                {cols.map(c => {
                  const v = row[c.k]
                  const isWarn = c.warn && Number(v) > 0
                  return (
                    <td key={c.k} className={`px-3 py-2 ${c.align === 'right' ? 'text-right' : ''} ${c.cls || ''} ${isWarn ? 'text-amber-700 font-semibold' : ''}`}>
                      {c.badge ? <StatusBadge status={v} /> :
                       c.fn   ? c.fn(v) :
                       c.fmt  ? fmt(v) : (v ?? '—')}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

/* ─────────────────────────────────────────────────────────────────────────
   TAB 4 — Hold
───────────────────────────────────────────────────────────────────────── */
function HoldTab() {
  const [summary, setSummary] = useState(null)
  const [byRdc,   setByRdc]   = useState([])
  const [byArt,   setByArt]   = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.allSettled([
      holdDashboardAPI.summary(),
      holdDashboardAPI.byRdc({ only_open: true }),
      holdDashboardAPI.byArticle({ limit: 12, only_open: true }),
    ]).then(([s, r, a]) => {
      if (s.status === 'fulfilled') setSummary(s.value?.data?.data || null)
      if (r.status === 'fulfilled') setByRdc(Array.isArray(r.value?.data?.data?.items) ? r.value.data.data.items : [])
      if (a.status === 'fulfilled') setByArt(Array.isArray(a.value?.data?.data?.items) ? a.value.data.data.items : [])
      setLoading(false)
    })
  }, [])

  if (loading) return <Loading />
  return (
    <div>
      <div className="text-sm text-gray-500 mb-3">
        Quick view. For deeper analysis: <Link to="/reports/hold" className="text-indigo-600 hover:underline">Open full Hold Dashboard →</Link>
      </div>
      <div className="grid grid-cols-3 gap-3 mb-4">
        <KpiTile label="Open Hold Qty"      value={summary?.totals?.hold_qty} accent="cyan" />
        <KpiTile label="Articles On Hold"   value={summary?.totals?.articles} accent="cyan" />
        <KpiTile label="Oldest Hold (days)" value={summary?.totals?.oldest_age_days} accent="cyan" />
      </div>
      <div className="grid grid-cols-2 gap-4">
        <ExpandableChart title="Hold qty by RDC" chip={`${byRdc.length} RDCs`}
          isEmpty={byRdc.length === 0}
          emptyReason="No open holds in ARS_NL_TBL_HOLD_TRACKING"
          tableColumns={[
            { key:'rdc', label:'RDC' },
            { key:'hold_qty', label:'Qty', align:'text-right', format: fmt },
          ]} tableData={byRdc}>
          <BarChart data={byRdc} layout="vertical" margin={{ top:4, right:30, left:6, bottom:0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
            <XAxis type="number" fontSize={10} />
            <YAxis type="category" dataKey="rdc" fontSize={10} width={60} />
            <Tooltip formatter={(v) => fmt(v)} />
            <Bar dataKey="hold_qty" fill="#06b6d4" radius={[0,4,4,0]} />
          </BarChart>
        </ExpandableChart>
        <div className="border border-gray-200 rounded-xl p-3 bg-white">
          <div className="text-sm font-semibold mb-2">Top articles on hold</div>
          <table className="w-full text-sm">
            <thead className="text-[10px] uppercase tracking-wider text-gray-500">
              <tr><th className="text-left py-1">Article</th><th className="text-right py-1">Qty</th><th className="text-right py-1">Age</th></tr>
            </thead>
            <tbody>
              {byArt.map((a, i) => (
                <tr key={i} className="border-t border-gray-100">
                  <td className="py-1 font-mono text-[11px]">{a.gen_art || a.var_art}</td>
                  <td className="py-1 text-right">{fmt(a.hold_qty)}</td>
                  <td className="py-1 text-right">{fmt(a.age_days)}d</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

function KpiTile({ label, value, accent }) {
  const colors = { cyan: 'bg-cyan-50 border-cyan-100 text-cyan-700', amber: 'bg-amber-50 border-amber-100 text-amber-700', rose: 'bg-rose-50 border-rose-100 text-rose-700' }
  return (
    <div className={`border rounded-lg p-3 ${colors[accent]}`}>
      <div className="text-xs uppercase font-semibold">{label}</div>
      <div className="text-2xl font-bold mt-1 text-gray-900">{value == null ? '—' : fmt(value)}</div>
    </div>
  )
}

/* ─────────────────────────────────────────────────────────────────────────
   TAB 5 — Pending Alloc
───────────────────────────────────────────────────────────────────────── */
function PendingTab({ scope }) {
  const [data, setData] = useState({ items: [], total: 0, pend_total: 0, avg_age: 0 })
  const [page, setPage] = useState(1)
  const [age,  setAge]  = useState('')
  const [loading, setLoading] = useState(true)
  const sk = scopeKey(scope)

  useEffect(() => {
    setLoading(true)
    arsDashboardAPI.pending({ ...scopeParams(scope), page, page_size: 50, age_bucket: age || undefined })
      .then(r => setData(r?.data?.data || { items: [] }))
      .catch(() => setData({ items: [] }))
      .finally(() => setLoading(false))
  }, [page, age, sk])

  const totalPages = Math.max(1, Math.ceil(data.total / 50))
  return (
    <div>
      <div className="grid grid-cols-3 gap-3 mb-4">
        <KpiTile label="Open Rows" value={data.total} accent="amber" />
        <KpiTile label="Total Pending Qty" value={data.pend_total} accent="amber" />
        <KpiTile label="Avg Age (days)" value={data.avg_age} accent="amber" />
      </div>
      <div className="flex items-center gap-2 mb-3">
        <span className="text-[11px] text-gray-500">Age</span>
        <Seg value={age} options={[{value:'',label:'Any'},{value:'0_7',label:'0–7'},{value:'8_30',label:'8–30'},{value:'31+',label:'31+'}]} onChange={(v) => { setAge(v); setPage(1) }} />
        <span className="ml-auto text-xs text-gray-500">{fmt(data.items?.length)} of {fmt(data.total)} rows</span>
      </div>

      {loading ? <Loading /> : (
        <>
          <table className="w-full bg-white border border-gray-200 rounded-lg overflow-hidden text-sm">
            <thead className="bg-gray-50 text-[10px] uppercase tracking-wider text-gray-500">
              <tr><th className="px-3 py-2 text-left">Session</th><th className="px-3 py-2">RDC</th>
                  <th className="px-3 py-2">Store</th><th className="px-3 py-2">Article</th>
                  <th className="px-3 py-2 text-right">Alloc</th><th className="px-3 py-2 text-right">DO</th>
                  <th className="px-3 py-2 text-right">Pending</th><th className="px-3 py-2 text-right">% Pend</th>
                  <th className="px-3 py-2 text-right">Age</th><th className="px-3 py-2">Status</th></tr>
            </thead>
            <tbody>
              {data.items.length === 0 && <tr><td colSpan={10} className="px-3 py-6 text-center text-xs text-gray-400">No pending rows in scope</td></tr>}
              {data.items.map((p, i) => (
                <tr key={i} className="border-t border-gray-100">
                  <td className="px-3 py-2 text-indigo-600 font-medium">{p.session_id}</td>
                  <td className="px-3 py-2">{p.rdc}</td>
                  <td className="px-3 py-2">{p.st_cd}</td>
                  <td className="px-3 py-2 font-mono text-[11px]">{p.article}</td>
                  <td className="px-3 py-2 text-right">{fmt(p.alloc_qty)}</td>
                  <td className="px-3 py-2 text-right">{fmt(p.do_qty)}</td>
                  <td className="px-3 py-2 text-right text-amber-700 font-semibold">{fmt(p.pend_qty)}</td>
                  <td className="px-3 py-2 text-right">{p.pend_pct}%</td>
                  <td className="px-3 py-2 text-right">{p.age_days}d</td>
                  <td className="px-3 py-2"><StatusBadge status={p.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          {totalPages > 1 && (
            <div className="flex items-center justify-end gap-2 mt-2 text-xs">
              <button disabled={page <= 1} onClick={() => setPage(p => p - 1)} className="border border-gray-200 rounded px-2 py-1 disabled:opacity-40"><ChevronLeft size={12} /></button>
              <span>{page} / {totalPages}</span>
              <button disabled={page >= totalPages} onClick={() => setPage(p => p + 1)} className="border border-gray-200 rounded px-2 py-1 disabled:opacity-40"><ChevronRight size={12} /></button>
            </div>
          )}
        </>
      )}
    </div>
  )
}

/* ─────────────────────────────────────────────────────────────────────────
   TAB 6 — Gap Report
───────────────────────────────────────────────────────────────────────── */
function GapTab({ scope }) {
  const [groupBy, setGroupBy] = useState('rdc_article')
  const [rows, setRows] = useState([])
  const [cols, setCols] = useState([])
  const [loading, setLoading] = useState(true)
  const sk = scopeKey(scope)

  useEffect(() => {
    setLoading(true)
    arsDashboardAPI.gap({ ...scopeParams(scope), group_by: groupBy, limit: 500 })
      .then(r => { setRows(Array.isArray(r?.data?.data?.items) ? r.data.data.items : []); setCols(r?.data?.data?.columns || []) })
      .catch(() => { setRows([]); setCols([]) })
      .finally(() => setLoading(false))
  }, [groupBy, sk])

  const doExport = async () => {
    try {
      const r = await arsDashboardAPI.exportGap({ ...scopeParams(scope), group_by: groupBy })
      const url = URL.createObjectURL(new Blob([r.data]))
      const a = document.createElement('a')
      a.href = url
      a.download = `ars_dashboard_gap_${groupBy}.xlsx`
      a.click()
      URL.revokeObjectURL(url)
    } catch { toast.error('Export failed') }
  }

  return (
    <div>
      <div className="bg-rose-50 border border-rose-200 text-rose-800 text-xs rounded-lg p-3 mb-4">
        <b>Gap definition:</b> rows in <code>ARS_PEND_ALC</code> where <code>PEND_QTY = ALLOC_QTY − DO_QTY &gt; 0</code> and <code>IS_CLOSED = 0</code>.
      </div>

      <div className="flex items-center gap-2 mb-3">
        <span className="text-[11px] text-gray-500">Group by</span>
        <Seg value={groupBy} onChange={setGroupBy} options={[
          { value: 'rdc_article',     label: 'RDC × Article' },
          { value: 'session_article', label: 'Session × Article' },
          { value: 'majcat',          label: 'MAJ_CAT' },
          { value: 'rdc_majcat',      label: 'RDC × MAJ_CAT' },
          { value: 'store',           label: 'Store' },
        ]} />
        <button onClick={doExport}
                className="ml-auto bg-emerald-600 text-white text-xs px-3 py-1.5 rounded hover:bg-emerald-700 flex items-center gap-1">
          <Download size={12} /> Export Excel
        </button>
      </div>

      {loading ? <Loading /> : (
        <table className="w-full bg-white border border-gray-200 rounded-lg overflow-hidden text-sm">
          <thead className="bg-gray-50 text-[10px] uppercase tracking-wider text-gray-500">
            <tr>
              {cols.map(c => <th key={c} className="px-3 py-2 text-left">{c}</th>)}
              <th className="px-3 py-2 text-right">Stores</th>
              <th className="px-3 py-2 text-right">Alloc</th>
              <th className="px-3 py-2 text-right">DO</th>
              <th className="px-3 py-2 text-right">Gap Qty</th>
              <th className="px-3 py-2 text-right">% Pend</th>
              <th className="px-3 py-2 text-right">Oldest (d)</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && <tr><td colSpan={cols.length + 6} className="px-3 py-6 text-center text-xs text-gray-400">No open gaps in scope</td></tr>}
            {rows.map((r, i) => (
              <tr key={i} className="border-t border-gray-100">
                {cols.map(c => <td key={c} className="px-3 py-2 font-mono text-[11px]">{r[c.toLowerCase()]}</td>)}
                <td className="px-3 py-2 text-right">{fmt(r.stores)}</td>
                <td className="px-3 py-2 text-right">{fmt(r.alloc_qty)}</td>
                <td className="px-3 py-2 text-right">{fmt(r.do_qty)}</td>
                <td className="px-3 py-2 text-right text-rose-700 font-semibold">{fmt(r.gap_qty)}</td>
                <td className="px-3 py-2 text-right">{r.gap_pct}%</td>
                <td className="px-3 py-2 text-right">{r.oldest_days}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

/* ─────────────────────────────────────────────────────────────────────────
   Shared
───────────────────────────────────────────────────────────────────────── */
function Loading() {
  return <div className="h-64 flex items-center justify-center"><Loader2 size={28} className="animate-spin text-indigo-500" /></div>
}

/* ─────────────────────────────────────────────────────────────────────────
   Main page
───────────────────────────────────────────────────────────────────────── */
const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'drill',    label: 'Product Drill' },
  { id: 'date',     label: 'Date & Session' },
  { id: 'hold',     label: 'Hold' },
  { id: 'pending',  label: 'Pending Alloc' },
  { id: 'gap',      label: 'Gap Report' },
]

export default function ArsDashboardPage() {
  const [searchParams, setSearchParams] = useSearchParams()

  const [scope, setScopeState] = useState(() => ({
    date:   searchParams.get('date') || '',
    sid:    searchParams.get('sid')  || '',
    mc:     (searchParams.get('mc')     || '').split(',').filter(Boolean),
    werks:  (searchParams.get('werks')  || '').split(',').filter(Boolean),
    rdc:    (searchParams.get('rdc')    || '').split(',').filter(Boolean),
    hub:    (searchParams.get('hub')    || '').split(',').filter(Boolean),
    status: (searchParams.get('status') || '').split(',').filter(Boolean),
    div:    (searchParams.get('div')    || '').split(',').filter(Boolean),
    ssn:    (searchParams.get('ssn')    || '').split(',').filter(Boolean),
  }))
  const [tab,       setTab]       = useState(searchParams.get('tab')  || 'overview')
  const [drillPath, setDrillPath] = useState(searchParams.get('path') || 'mjst')

  const [config,         setConfig]         = useState({ maj_cats: [], stores: [], rdcs: [], dates: [], hubs: [], statuses: [], divs: [], ssns: [] })
  const [sessionsForDate, setSessionsForDate] = useState([])
  const [summary,        setSummary]        = useState(null)
  const [refreshing,     setRefreshing]     = useState(false)

  const setScope = (s) => {
    setScopeState(s)
    const p = new URLSearchParams()
    if (s.date)         p.set('date',  s.date)
    if (s.sid)          p.set('sid',   s.sid)
    if (s.mc.length)    p.set('mc',    s.mc.join(','))
    if (s.werks.length) p.set('werks', s.werks.join(','))
    if (s.rdc.length)   p.set('rdc',   s.rdc.join(','))
    if (s.hub.length)   p.set('hub',   s.hub.join(','))
    if (s.status.length) p.set('status', s.status.join(','))
    if (s.div.length)   p.set('div',   s.div.join(','))
    if (s.ssn.length)   p.set('ssn',   s.ssn.join(','))
    p.set('tab', tab); p.set('path', drillPath)
    setSearchParams(p, { replace: true })
  }
  useEffect(() => {
    const p = new URLSearchParams(searchParams)
    p.set('tab', tab); p.set('path', drillPath)
    setSearchParams(p, { replace: true })
  }, [tab, drillPath])

  // Initial config load
  useEffect(() => {
    Promise.allSettled([
      listingAPI.config({ quiet: true }),
      arsDashboardAPI.dates({ days: 60 }),
      arsDashboardAPI.configExtras(),
    ]).then(([c, d, e]) => {
      const lc  = c.status === 'fulfilled' ? (c.value?.data || {}) : {}
      const dts = d.status === 'fulfilled' ? (d.value?.data?.data?.items || []).map(x => x.date) : []
      const ex  = e.status === 'fulfilled' ? (e.value?.data?.data || {}) : {}
      setConfig({
        maj_cats: (ex.maj_cats && ex.maj_cats.length ? ex.maj_cats : (lc.maj_cats || lc.MAJ_CATS || [])),
        stores:   (lc.stores || lc.STORES || []).map(s => typeof s === 'string' ? s : (s.werks || s.store_code || s)),
        rdcs:     lc.rdcs || lc.RDCS || [],
        dates:    dts,
        hubs:     ex.hubs     || [],
        statuses: ex.statuses || [],
        divs:     ex.divs     || [],
        ssns:     ex.ssns     || [],
      })
    })
  }, [])

  // Refresh session dropdown when date changes
  useEffect(() => {
    if (!scope.date) { setSessionsForDate([]); return }
    arsDashboardAPI.sessionsByDate(scope.date)
      .then(r => setSessionsForDate(Array.isArray(r?.data?.data?.items) ? r.data.data.items : []))
      .catch(() => setSessionsForDate([]))
  }, [scope.date])

  // KPI summary reloader (depends on scope)
  const sk = scopeKey(scope)
  const loadSummary = useCallback(() => {
    setRefreshing(true)
    arsDashboardAPI.summary(scopeParams(scope))
      .then(r => setSummary(r?.data?.data || null))
      .catch(() => setSummary(null))
      .finally(() => setRefreshing(false))
  }, [sk])
  useEffect(() => { loadSummary() }, [loadSummary])

  return (
    <div className="p-5 space-y-3 max-w-[1700px] mx-auto">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-900 flex items-center gap-2">
            <LayoutGrid size={20} className="text-indigo-600" /> ARS Dashboard
          </h1>
          <p className="text-xs text-gray-500 mt-0.5">High-level allocation analytics — date, session &amp; hierarchy</p>
        </div>
        <button onClick={loadSummary} disabled={refreshing}
                className="flex items-center gap-2 px-3 py-1.5 text-xs bg-white border border-gray-200 rounded-lg hover:bg-gray-50 disabled:opacity-50">
          <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} /> Refresh
        </button>
      </div>

      <FilterBar scope={scope} setScope={setScope} config={config}
                 sessionsForDate={sessionsForDate}
                 drillPath={drillPath} setDrillPath={setDrillPath} />

      <KpiStrip summary={summary} onTabJump={setTab} />

      <div className="bg-white border border-gray-200 rounded-xl shadow-sm">
        <div className="flex items-center border-b border-gray-200 px-3 overflow-x-auto">
          {TABS.map(t => (
            <button key={t.id} onClick={() => setTab(t.id)}
                    className={`px-3 py-2 text-sm font-medium border-b-2 transition-colors whitespace-nowrap ${tab === t.id ? 'text-indigo-600 border-indigo-600' : 'text-gray-500 border-transparent hover:text-gray-900'}`}>
              {t.label}
            </button>
          ))}
        </div>
        <div className="p-4">
          {tab === 'overview' && <OverviewTab    scope={scope} />}
          {tab === 'drill'    && <DrillTab       scope={scope} drillPath={drillPath} />}
          {tab === 'date'     && <DateSessionTab scope={scope} />}
          {tab === 'hold'     && <HoldTab />}
          {tab === 'pending'  && <PendingTab     scope={scope} />}
          {tab === 'gap'      && <GapTab         scope={scope} />}
        </div>
      </div>
    </div>
  )
}
