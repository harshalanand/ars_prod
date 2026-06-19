/**
 * HoldDashboardPage — Review HOLD_QTY in various angles.
 *
 * Reads from ARS_NL_TBL_HOLD_TRACKING via the hold-dashboard endpoints.
 * Sections: KPI cards, by-RDC, by-store, by-article, by-status, age buckets,
 * timeline, drill-down detail, reconciliation banner.
 */
import { useState, useEffect, useCallback, useMemo } from 'react'
import { holdDashboardAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  Lock, RefreshCw, Building2, Boxes, AlertTriangle, Calendar,
  Loader2, Filter, X, ChevronLeft, ChevronRight, Unlock, Upload, Send, Download, Plus,
  Maximize2, BarChart3, Table as TableIcon,
} from 'lucide-react'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer, LineChart, Line, PieChart, Pie, Cell, Brush,
} from 'recharts'

const COLORS = ['#4f46e5', '#06b6d4', '#f59e0b', '#10b981', '#ef4444',
                '#8b5cf6', '#ec4899', '#14b8a6', '#f97316', '#6366f1']

const fmt = (n) => (n == null ? '-' : Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 }))
const fmtFloat = (n) => (n == null ? '-' : Number(n).toLocaleString('en-IN', { maximumFractionDigits: 2 }))

function Card({ icon: Icon, label, value, sub, color = 'indigo' }) {
  const colors = {
    indigo: 'bg-indigo-50 text-indigo-600',
    cyan:   'bg-cyan-50 text-cyan-600',
    amber:  'bg-amber-50 text-amber-600',
    green:  'bg-emerald-50 text-emerald-600',
    rose:   'bg-rose-50 text-rose-600',
  }
  return (
    <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
      <div className="flex items-center gap-3">
        <div className={`p-2 rounded-lg ${colors[color]}`}>
          <Icon size={18} />
        </div>
        <div className="text-xs font-medium text-gray-500 uppercase tracking-wide">{label}</div>
      </div>
      <div className="mt-3 text-2xl font-bold text-gray-900">{value}</div>
      {sub && <div className="mt-1 text-xs text-gray-500">{sub}</div>}
    </div>
  )
}

// Definitions for the 4-card Daily Hold vs Consumption row. Sharing this
// table between the inline cards and the maximized modal keeps colors,
// labels, and table headers consistent.
const DAILY_CARDS = [
  { key: 'created',    label: 'Hold Created',       desc: 'Hold qty listed per day (LISTED_DATE)',
    color: '#4f46e5', kind: 'bar',  field: 'created_qty' },
  { key: 'consumed',   label: 'Hold Consumed',      desc: 'Hold qty consumed as rows closed (CLOSED_DATE)',
    color: '#10b981', kind: 'bar',  field: 'closed_qty' },
  { key: 'compare',    label: 'Created vs Consumed', desc: 'Side-by-side per day',
    color: '#4f46e5', kind: 'group' },
  { key: 'cumulative', label: 'Cumulative Net',     desc: 'Running (created − consumed) over the window',
    color: '#06b6d4', kind: 'line', field: 'cum_net' },
]

function buildDailyMetrics(timeline) {
  let running = 0
  return (timeline || []).map((t) => {
    const created = Number(t.created_qty) || 0
    const consumed = Number(t.closed_qty)  || 0
    running += created - consumed
    return {
      day:          t.day,
      created_qty:  created,
      closed_qty:   consumed,
      net:          created - consumed,
      cum_net:      running,
    }
  })
}

function renderDailyChart(card, data, height, withBrush = false) {
  const common = (
    <>
      <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
      <XAxis dataKey="day" tick={{ fontSize: 10 }} />
      <YAxis tick={{ fontSize: 11 }} />
      <Tooltip formatter={(v) => fmt(v)} />
    </>
  )
  if (card.kind === 'group') {
    return (
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={data}>
          {common}
          <Legend wrapperStyle={{ fontSize: 11 }} />
          <Bar dataKey="created_qty" fill="#4f46e5" name="Created"  radius={[3, 3, 0, 0]} />
          <Bar dataKey="closed_qty"  fill="#10b981" name="Consumed" radius={[3, 3, 0, 0]} />
          {withBrush && <Brush dataKey="day" height={26} stroke="#4f46e5" travellerWidth={8} />}
        </BarChart>
      </ResponsiveContainer>
    )
  }
  if (card.kind === 'line') {
    return (
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={data}>
          {common}
          <Line type="monotone" dataKey={card.field} stroke={card.color}
                strokeWidth={2} dot={false} name={card.label} />
          {withBrush && <Brush dataKey="day" height={26} stroke={card.color} travellerWidth={8} />}
        </LineChart>
      </ResponsiveContainer>
    )
  }
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data}>
        {common}
        <Bar dataKey={card.field} fill={card.color} name={card.label} radius={[3, 3, 0, 0]} />
        {withBrush && <Brush dataKey="day" height={26} stroke={card.color} travellerWidth={8} />}
      </BarChart>
    </ResponsiveContainer>
  )
}

function DailyChartTable({ card, data, maxHeight = 220 }) {
  const cols = card.kind === 'group'
    ? [{ k: 'created_qty', label: 'Created' }, { k: 'closed_qty', label: 'Consumed' }]
    : [{ k: card.field, label: card.label }]
  return (
    <div className="overflow-y-auto" style={{ maxHeight }}>
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-white border-b border-gray-200 text-gray-500 uppercase text-[10px]">
          <tr>
            <th className="text-left py-1.5 px-2 font-medium">Day</th>
            {cols.map(c => (
              <th key={c.k} className="text-right py-1.5 px-2 font-medium">{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.length === 0 ? (
            <tr><td colSpan={cols.length + 1} className="py-6 text-center text-gray-400">No data</td></tr>
          ) : data.map((d, i) => (
            <tr key={i} className="border-b border-gray-100 hover:bg-gray-50">
              <td className="py-1 px-2 text-gray-600 font-mono">{d.day}</td>
              {cols.map(c => (
                <td key={c.k} className="py-1 px-2 text-right">{fmt(d[c.k])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DailyHoldConsumptionRow({ timeline, view, setView, onMaximize }) {
  const data = useMemo(() => buildDailyMetrics(timeline), [timeline])
  const totals = useMemo(() => ({
    created:  data.reduce((s, d) => s + d.created_qty, 0),
    consumed: data.reduce((s, d) => s + d.closed_qty,  0),
    net:      data.reduce((s, d) => s + d.net,         0),
  }), [data])

  return (
    <div>
      <div className="flex items-start justify-between mb-2">
        <div>
          <h3 className="font-semibold text-gray-900 text-sm">Daily Hold vs Consumption</h3>
          <p className="text-xs text-gray-500 mt-0.5">
            Last 60 days · Created {fmt(totals.created)} · Consumed {fmt(totals.consumed)} ·
            Net {fmt(totals.net)} · click <Maximize2 size={11} className="inline" /> to zoom,
            <TableIcon size={11} className="inline ml-1" /> to switch to table
          </p>
        </div>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
        {DAILY_CARDS.map(card => {
          const mode = view[card.key] || 'chart'
          return (
            <div key={card.key} className="bg-white border border-gray-200 rounded-xl p-3 shadow-sm flex flex-col">
              <div className="flex items-start justify-between gap-2 mb-2">
                <div className="min-w-0">
                  <h4 className="text-sm font-semibold text-gray-900 truncate" title={card.label}>{card.label}</h4>
                  <p className="text-[10px] text-gray-500 truncate" title={card.desc}>{card.desc}</p>
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  <button
                    onClick={() => setView(v => ({ ...v, [card.key]: mode === 'chart' ? 'table' : 'chart' }))}
                    title={mode === 'chart' ? 'Show table' : 'Show chart'}
                    className="p-1 text-gray-400 hover:text-indigo-600 hover:bg-indigo-50 rounded"
                  >
                    {mode === 'chart' ? <TableIcon size={13} /> : <BarChart3 size={13} />}
                  </button>
                  <button
                    onClick={() => onMaximize(card)}
                    title="Maximize (zoomable)"
                    className="p-1 text-gray-400 hover:text-indigo-600 hover:bg-indigo-50 rounded"
                  >
                    <Maximize2 size={13} />
                  </button>
                </div>
              </div>
              {data.length === 0 ? (
                <p className="text-xs text-gray-400 py-10 text-center">No data</p>
              ) : mode === 'chart' ? (
                renderDailyChart(card, data, 200, false)
              ) : (
                <DailyChartTable card={card} data={data} maxHeight={200} />
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function Section({ title, subtitle, children, right }) {
  return (
    <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="font-semibold text-gray-900 text-sm">{title}</h3>
          {subtitle && <p className="text-xs text-gray-500 mt-0.5">{subtitle}</p>}
        </div>
        {right}
      </div>
      {children}
    </div>
  )
}

export default function HoldDashboardPage() {
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [summary, setSummary] = useState(null)
  const [byStore, setByStore] = useState([])
  const [byRdc, setByRdc] = useState([])
  const [byArticle, setByArticle] = useState([])
  const [byStatus, setByStatus] = useState([])
  const [byAge, setByAge] = useState([])
  const [timeline, setTimeline] = useState([])
  const [recon, setRecon] = useState(null)

  // Detail filters & pagination
  const [filters, setFilters] = useState({ werks: '', rdc: '', gen_art: '', status: '', only_open: true })
  const [page, setPage] = useState(1)
  const [pageSize] = useState(50)
  const [detail, setDetail] = useState({ items: [], total: 0 })
  const [detailLoading, setDetailLoading] = useState(false)

  // Adhoc clear-hold modal state. `clearTarget` is the row being cleared
  // (single-row from the table) or null for the bulk-file flow.
  const [clearTarget, setClearTarget] = useState(null)
  const [clearReason, setClearReason] = useState('')
  const [clearReleaseQty, setClearReleaseQty] = useState('')   // blank = full close
  const [clearSubmitting, setClearSubmitting] = useState(false)
  const [bulkClearOpen, setBulkClearOpen] = useState(false)
  const [bulkClearFile, setBulkClearFile] = useState(null)

  // Adhoc revise-hold modal state (increase HOLD_REM on an existing row).
  const [reviseTarget, setReviseTarget] = useState(null)
  const [reviseAddQty, setReviseAddQty] = useState('')
  const [reviseReason, setReviseReason] = useState('')
  const [bulkReviseOpen, setBulkReviseOpen] = useState(false)
  const [bulkReviseFile, setBulkReviseFile] = useState(null)

  // Per-card view ('chart' | 'table') for the Daily Hold vs Consumption row.
  // Maximized chart opens a modal with a zoomable Brush range slider.
  const [chartView, setChartView] = useState({})
  const [maximized, setMaximized] = useState(null)

  const loadAll = useCallback(async () => {
    try {
      const [s, st, rdc, art, status, age, tl, rc] = await Promise.all([
        holdDashboardAPI.summary(),
        holdDashboardAPI.byStore({ limit: 15, only_open: true }),
        holdDashboardAPI.byRdc({ only_open: true }),
        holdDashboardAPI.byArticle({ limit: 15, only_open: true }),
        holdDashboardAPI.byStatus(),
        holdDashboardAPI.byAge(),
        holdDashboardAPI.timeline({ days: 60 }),
        holdDashboardAPI.reconciliation(),
      ])
      setSummary(s.data?.data || null)
      setByStore(st.data?.data?.items || [])
      setByRdc(rdc.data?.data?.items || [])
      setByArticle(art.data?.data?.items || [])
      setByStatus(status.data?.data?.items || [])
      setByAge(age.data?.data?.items || [])
      setTimeline(tl.data?.data?.items || [])
      setRecon(rc.data?.data || null)
    } catch (e) {
      toast.error('Failed to load hold dashboard')
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [])

  const loadDetail = useCallback(async () => {
    setDetailLoading(true)
    try {
      const params = { page, page_size: pageSize, only_open: filters.only_open }
      if (filters.werks) params.werks = filters.werks
      if (filters.rdc) params.rdc = filters.rdc
      if (filters.gen_art) params.gen_art = filters.gen_art
      if (filters.status) params.status = filters.status
      const r = await holdDashboardAPI.detail(params)
      setDetail(r.data?.data || { items: [], total: 0 })
    } catch (e) {
      toast.error('Failed to load detail rows')
    } finally {
      setDetailLoading(false)
    }
  }, [page, pageSize, filters])

  useEffect(() => { loadAll() }, [loadAll])
  useEffect(() => { loadDetail() }, [loadDetail])

  const handleRefresh = () => {
    setRefreshing(true)
    loadAll()
    loadDetail()
  }

  const clearFilters = () => {
    setFilters({ werks: '', rdc: '', gen_art: '', status: '', only_open: true })
    setPage(1)
  }

  // Open the single-row clear-hold modal pre-populated with the row's key.
  const openRowClear = (row) => {
    setClearTarget(row)
    setClearReason('')
    setClearReleaseQty('')   // default = full close
  }

  const closeRowClear = () => {
    setClearTarget(null); setClearReason(''); setClearReleaseQty('')
  }

  const submitRowClear = async () => {
    if (!clearTarget) return
    const rqRaw = clearReleaseQty.trim()
    const rq = rqRaw === '' ? null : Number(rqRaw)
    if (rq !== null && (!Number.isFinite(rq) || rq <= 0)) {
      toast.error('Release qty must be a positive number, or blank for full close')
      return
    }
    if (!clearReason.trim()) {
      toast.error('Reason is mandatory')
      return
    }
    const action = (rq === null || rq >= clearTarget.hold_rem)
      ? `fully close (HOLD_REM ${fmt(clearTarget.hold_rem)} → 0)`
      : `release ${fmt(rq)} of ${fmt(clearTarget.hold_rem)}`
    if (!confirm(
      `Adhoc clear hold:\n\n` +
      `  WERKS:   ${clearTarget.werks}\n` +
      `  VAR_ART: ${clearTarget.var_art}\n` +
      `  SZ:      ${clearTarget.sz || '(blank)'}\n\n` +
      `Action: ${action}\n\nMSA HOLD_QTY/FNL_Q will be re-synced.`
    )) return

    setClearSubmitting(true)
    try {
      const { data } = await holdDashboardAPI.clearHold({
        reason: clearReason.trim() || null,
        rows: [{
          werks:   clearTarget.werks,
          var_art: String(clearTarget.var_art),
          sz:      clearTarget.sz || '',
          ...(rq != null ? { release_qty: rq } : {}),
        }],
      })
      toast.success(
        `Cleared ${data.hold_rows_updated} hold row · ` +
        `released ${fmt(data.qty_released)} units · ` +
        `MSA total updated: ${data.msa_total_updated}`
      )
      closeRowClear()
      loadAll(); loadDetail()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Clear hold failed')
    } finally {
      setClearSubmitting(false)
    }
  }

  const [exporting, setExporting] = useState(false)
  const exportDetail = async () => {
    setExporting(true)
    try {
      const params = { only_open: filters.only_open }
      if (filters.werks)   params.werks   = filters.werks
      if (filters.rdc)     params.rdc     = filters.rdc
      if (filters.gen_art) params.gen_art = filters.gen_art
      if (filters.status)  params.status  = filters.status

      const res = await holdDashboardAPI.detailExport(params)
      // Pull filename from Content-Disposition if present
      const cd = res.headers?.['content-disposition'] || ''
      const m  = /filename="?([^";]+)"?/i.exec(cd)
      const fname = m ? m[1]
                      : `hold_detail_${filters.only_open ? 'open' : 'all'}.csv`
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url; a.download = fname
      document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
      toast.success(`Exported ${fname}`)
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Export failed')
    } finally {
      setExporting(false)
    }
  }

  const downloadBulkTemplate = () => {
    // Row 1: clear every size for that (WERKS, VAR_ART) — blank SZ + blank RELEASE_QTY.
    // Row 2: same key, specific SZ, blank RELEASE_QTY → close that size only.
    // Row 3: specific SZ + RELEASE_QTY → partial release on that one size.
    const csv =
      'WERKS,VAR_ART,SZ,RELEASE_QTY\n' +
      'HA10,1114093375001,,\n' +
      'HA10,1114093375002,M,\n' +
      'HA10,1114093375003,L,5\n'
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'clear_hold_template.csv'
    document.body.appendChild(a); a.click(); a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  // ── Revise (increase) hold ──────────────────────────────────────────
  const openRowRevise = (row) => {
    setReviseTarget(row); setReviseAddQty(''); setReviseReason('')
  }
  const closeRowRevise = () => {
    setReviseTarget(null); setReviseAddQty(''); setReviseReason('')
  }

  const submitRowRevise = async () => {
    if (!reviseTarget) return
    const addRaw = reviseAddQty.trim()
    const add = Number(addRaw)
    if (!addRaw || !Number.isFinite(add) || add <= 0) {
      toast.error('Add qty must be a positive number')
      return
    }
    if (!reviseReason.trim()) {
      toast.error('Reason is mandatory')
      return
    }
    if (!confirm(
      `Increase hold:\n\n` +
      `  WERKS:   ${reviseTarget.werks}\n` +
      `  VAR_ART: ${reviseTarget.var_art}\n` +
      `  SZ:      ${reviseTarget.sz || '(blank)'}\n\n` +
      `HOLD_REM ${fmt(reviseTarget.hold_rem)} → ${fmt(reviseTarget.hold_rem + add)}\n` +
      `MSA HOLD_QTY/FNL_Q will be re-synced.`
    )) return

    setClearSubmitting(true)
    try {
      const { data } = await holdDashboardAPI.reviseHold({
        reason: reviseReason.trim() || null,
        rows: [{
          werks:   reviseTarget.werks,
          var_art: String(reviseTarget.var_art),
          sz:      reviseTarget.sz || '',
          add_qty: add,
        }],
      })
      toast.success(
        `Revised ${data.hold_rows_updated} hold row · ` +
        `+${fmt(data.qty_added)} units · ` +
        `MSA total updated: ${data.msa_total_updated}`
      )
      closeRowRevise()
      loadAll(); loadDetail()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Revise hold failed')
    } finally {
      setClearSubmitting(false)
    }
  }

  const submitBulkRevise = async () => {
    if (!bulkReviseFile) return
    if (!reviseReason.trim()) {
      toast.error('Reason is mandatory')
      return
    }
    if (!confirm(
      `Submit ${bulkReviseFile.name} for bulk hold revise?\n\n` +
      `Each row adds ADD_QTY to the matching tracker row(s).\n` +
      `MSA HOLD_QTY/FNL_Q is re-synced.`
    )) return

    setClearSubmitting(true)
    try {
      const { data } = await holdDashboardAPI.reviseHoldFile(
        bulkReviseFile, reviseReason.trim() || null
      )
      toast.success(
        `Revised ${data.hold_rows_updated} hold row(s) · ` +
        `+${fmt(data.qty_added)} units`
      )
      setBulkReviseOpen(false); setBulkReviseFile(null); setReviseReason('')
      loadAll(); loadDetail()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Bulk revise failed')
    } finally {
      setClearSubmitting(false)
    }
  }

  const downloadReviseTemplate = () => {
    // Required columns: WERKS, VAR_ART, ADD_QTY. SZ is optional (blank =
    // every size for the (WERKS, VAR_ART)).
    const csv =
      'WERKS,VAR_ART,SZ,ADD_QTY\n' +
      'HA10,1114093375001,,10\n' +
      'HA10,1114093375002,M,5\n'
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = 'revise_hold_template.csv'
    document.body.appendChild(a); a.click(); a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  const submitBulkClear = async () => {
    if (!bulkClearFile) return
    if (!clearReason.trim()) {
      toast.error('Reason is mandatory')
      return
    }
    if (!confirm(
      `Submit ${bulkClearFile.name} for bulk hold clear?\n\n` +
      `Each row will be fully closed unless RELEASE_QTY is provided.\n` +
      `MSA HOLD_QTY/FNL_Q will be re-synced for all affected (RDC, ARTICLE) keys.`
    )) return

    setClearSubmitting(true)
    try {
      const { data } = await holdDashboardAPI.clearHoldFile(
        bulkClearFile, clearReason.trim() || null
      )
      toast.success(
        `Cleared ${data.hold_rows_updated} hold row(s) · ` +
        `released ${fmt(data.qty_released)} units`
      )
      setBulkClearOpen(false); setBulkClearFile(null); setClearReason('')
      loadAll(); loadDetail()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Bulk clear failed')
    } finally {
      setClearSubmitting(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 size={32} className="animate-spin text-indigo-600" />
      </div>
    )
  }

  const totalPages = Math.max(1, Math.ceil((detail.total || 0) / pageSize))
  const driftBig = recon && recon.tracker_vs_msa_drift != null && Math.abs(recon.tracker_vs_msa_drift) > 1
  const lastUpdated = summary?.last_updated ? new Date(summary.last_updated).toLocaleString() : '—'

  return (
    <div className="p-6 space-y-5 max-w-[1600px] mx-auto">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
            <Lock size={22} className="text-indigo-600" /> Hold Dashboard
          </h1>
          <p className="text-sm text-gray-500 mt-1">
            Review TBL/NL hold reservations from ARS_NL_TBL_HOLD_TRACKING. Last updated: {lastUpdated}
          </p>
        </div>
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="flex items-center gap-2 px-3 py-1.5 text-sm bg-white border border-gray-200 rounded-lg hover:bg-gray-50 disabled:opacity-50"
        >
          <RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} /> Refresh
        </button>
      </div>

      {/* Reconciliation banner */}
      {recon && (
        <div className={`border rounded-xl p-3 text-sm flex items-start gap-3 ${
          driftBig ? 'bg-amber-50 border-amber-200 text-amber-900'
                   : 'bg-blue-50 border-blue-200 text-blue-900'
        }`}>
          {driftBig ? <AlertTriangle size={18} className="mt-0.5 shrink-0" />
                    : <Filter size={18} className="mt-0.5 shrink-0" />}
          <div className="flex-1">
            <div className="font-medium">
              Reconciliation — Tracker (open): <b>{fmt(recon.tracker_open_qty)}</b> &middot;
              Latest run HOLD: <b>{fmt(recon.latest_run_hold_qty)}</b> &middot;
              MSA HOLD_QTY: <b>{fmt(recon.msa_hold_qty)}</b>
              {recon.tracker_vs_msa_drift != null && (
                <> &middot; Drift (Tracker − MSA): <b>{fmtFloat(recon.tracker_vs_msa_drift)}</b></>
              )}
            </div>
            {driftBig && (
              <div className="text-xs mt-1">
                Drift &gt; 1 unit. Re-run MSA so Step 6.5 picks up the latest tracker; if drift persists,
                check Master_ALC_INPUT_ST_MASTER for missing WERKS → RDC mappings.
              </div>
            )}
          </div>
        </div>
      )}

      {/* KPI cards */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        <Card icon={Lock}     label="Open SKUs"        value={fmt(summary?.open_rows)}
              sub={`${fmt(summary?.closed_rows)} closed`} color="indigo" />
        <Card icon={Boxes}    label="Open hold qty"    value={fmt(summary?.open_qty)}
              sub={`${fmt(summary?.open_initial)} initial`} color="cyan" />
        <Card icon={Boxes}    label="Consumed qty"     value={fmt(summary?.consumed_qty)}
              sub="ever shipped from holds" color="green" />
        <Card icon={Building2} label="Stores"          value={fmt(summary?.distinct_stores)}
              sub="with open holds" color="indigo" />
        <Card icon={Boxes}    label="Articles"          value={fmt(summary?.distinct_articles)}
              sub={`${fmt(summary?.distinct_skus)} unique SKUs`} color="amber" />
        <Card icon={Calendar} label="Oldest open"      value={`${fmt(summary?.oldest_open_days)}d`}
              sub="days since first listed" color={summary?.oldest_open_days > 30 ? 'rose' : 'green'} />
      </div>

      {/* Row 1: by-RDC + by-status */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Section title="By RDC (Warehouse)" subtitle="Open hold quantity per warehouse">
          {byRdc.length === 0 ? (
            <p className="text-sm text-gray-400 py-12 text-center">No data</p>
          ) : (
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={byRdc.slice(0, 10)} layout="vertical" margin={{ left: 60 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                <XAxis type="number" tick={{ fontSize: 11 }} />
                <YAxis dataKey="rdc" type="category" tick={{ fontSize: 11 }} width={70} />
                <Tooltip />
                <Bar dataKey="open_qty" fill="#4f46e5" name="Open hold qty" radius={[0, 4, 4, 0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </Section>

        <Section title="By Status" subtitle="NL vs TBL split">
          {byStatus.length === 0 ? (
            <p className="text-sm text-gray-400 py-12 text-center">No data</p>
          ) : (
            <ResponsiveContainer width="100%" height={260}>
              <PieChart>
                <Pie
                  data={byStatus.filter(s => s.open_qty > 0)}
                  dataKey="open_qty" nameKey="status"
                  cx="50%" cy="50%" innerRadius={45} outerRadius={85}
                  label={({ status, open_qty }) => `${status}: ${fmt(open_qty)}`}
                  labelLine={false}
                >
                  {byStatus.map((_, i) => (
                    <Cell key={i} fill={COLORS[i % COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip formatter={(v) => fmt(v)} />
              </PieChart>
            </ResponsiveContainer>
          )}
        </Section>

        <Section title="Age Buckets" subtitle="Open holds by days since LISTED_DATE">
          {byAge.length === 0 ? (
            <p className="text-sm text-gray-400 py-12 text-center">No data</p>
          ) : (
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={byAge}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                <XAxis dataKey="bucket" tick={{ fontSize: 10 }} angle={-20} textAnchor="end" height={50} />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip />
                <Bar dataKey="open_qty" fill="#06b6d4" name="Open qty" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </Section>
      </div>

      {/* Row 2: by-store + by-article */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Section title="Top Stores" subtitle="Stores with the most open hold qty">
          {byStore.length === 0 ? (
            <p className="text-sm text-gray-400 py-12 text-center">No data</p>
          ) : (
            <ResponsiveContainer width="100%" height={320}>
              <BarChart data={byStore} layout="vertical" margin={{ left: 60 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                <XAxis type="number" tick={{ fontSize: 11 }} />
                <YAxis dataKey="werks" type="category" tick={{ fontSize: 11 }} width={70} />
                <Tooltip />
                <Bar dataKey="open_qty" fill="#10b981" name="Open hold qty" radius={[0, 4, 4, 0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </Section>

        <Section title="Top Articles" subtitle="Articles with the most open hold qty">
          {byArticle.length === 0 ? (
            <p className="text-sm text-gray-400 py-12 text-center">No data</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-gray-500 uppercase border-b border-gray-200">
                  <tr>
                    <th className="text-left py-2 font-medium">GEN_ART</th>
                    <th className="text-left py-2 font-medium">MAJ_CAT</th>
                    <th className="text-right py-2 font-medium">Stores</th>
                    <th className="text-right py-2 font-medium">Variants</th>
                    <th className="text-right py-2 font-medium">Open qty</th>
                    <th className="text-right py-2 font-medium">Initial</th>
                  </tr>
                </thead>
                <tbody>
                  {byArticle.map((a, i) => (
                    <tr key={i} className="border-b border-gray-100 hover:bg-gray-50">
                      <td className="py-1.5 font-mono">{a.gen_art_number}</td>
                      <td className="py-1.5 text-gray-600">{a.maj_cat}</td>
                      <td className="py-1.5 text-right">{fmt(a.stores)}</td>
                      <td className="py-1.5 text-right">{fmt(a.variants)}</td>
                      <td className="py-1.5 text-right font-semibold">{fmt(a.open_qty)}</td>
                      <td className="py-1.5 text-right text-gray-500">{fmt(a.initial_qty)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Section>
      </div>

      {/* Daily Hold vs Consumption — 4 focused cards, each with chart/table toggle and maximize. */}
      <DailyHoldConsumptionRow
        timeline={timeline}
        view={chartView}
        setView={setChartView}
        onMaximize={(card) => setMaximized(card)}
      />

      {/* Timeline */}
      <Section title="Timeline (last 60 days)" subtitle="Holds created vs closed per day">
        {timeline.length === 0 ? (
          <p className="text-sm text-gray-400 py-12 text-center">No data</p>
        ) : (
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={timeline}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
              <XAxis dataKey="day" tick={{ fontSize: 10 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Line type="monotone" dataKey="created" stroke="#4f46e5" strokeWidth={2}
                    name="Created (rows)" dot={false} />
              <Line type="monotone" dataKey="closed"  stroke="#10b981" strokeWidth={2}
                    name="Closed (rows)"  dot={false} />
              <Line type="monotone" dataKey="created_qty" stroke="#06b6d4" strokeWidth={1.5}
                    strokeDasharray="4 4" name="Created qty" dot={false} />
              <Line type="monotone" dataKey="closed_qty"  stroke="#f59e0b" strokeWidth={1.5}
                    strokeDasharray="4 4" name="Closed qty"  dot={false} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </Section>

      {/* Detail table with filters */}
      <Section
        title="Detail (drill-down)"
        subtitle={`${fmt(detail.total)} rows · page ${page} of ${totalPages}`}
        right={
          <div className="flex items-center gap-2">
            <label className="text-xs text-gray-600 flex items-center gap-1">
              <input type="checkbox" checked={filters.only_open}
                     onChange={e => { setFilters({ ...filters, only_open: e.target.checked }); setPage(1) }} />
              Only open
            </label>
            <button onClick={exportDetail} disabled={exporting}
                    title={filters.only_open
                        ? 'Export rows where Closed? = open (current filters)'
                        : 'Export all rows matching current filters'}
                    className="text-xs px-2 py-1 border border-indigo-200 bg-indigo-50 text-indigo-700 rounded hover:bg-indigo-100 disabled:opacity-60 flex items-center gap-1">
              {exporting ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />}
              {exporting ? 'Exporting…' : 'Export'}
            </button>
            <button onClick={() => { setBulkClearOpen(true); setBulkClearFile(null); setClearReason('') }}
                    className="text-xs px-2 py-1 border border-rose-200 bg-rose-50 text-rose-700 rounded hover:bg-rose-100 flex items-center gap-1">
              <Unlock size={12} /> Bulk clear
            </button>
            <button onClick={() => { setBulkReviseOpen(true); setBulkReviseFile(null); setReviseReason('') }}
                    className="text-xs px-2 py-1 border border-emerald-200 bg-emerald-50 text-emerald-700 rounded hover:bg-emerald-100 flex items-center gap-1">
              <Plus size={12} /> Bulk revise
            </button>
            <button onClick={clearFilters}
                    className="text-xs px-2 py-1 border border-gray-200 rounded hover:bg-gray-50 flex items-center gap-1">
              <X size={12} /> Clear filters
            </button>
          </div>
        }
      >
        {/* Filter inputs */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-3">
          <input type="text" placeholder="WERKS (store)" value={filters.werks}
                 onChange={e => { setFilters({ ...filters, werks: e.target.value }); setPage(1) }}
                 className="text-xs px-2 py-1.5 border border-gray-200 rounded" />
          <input type="text" placeholder="RDC" value={filters.rdc}
                 onChange={e => { setFilters({ ...filters, rdc: e.target.value }); setPage(1) }}
                 className="text-xs px-2 py-1.5 border border-gray-200 rounded" />
          <input type="text" placeholder="GEN_ART_NUMBER" value={filters.gen_art}
                 onChange={e => { setFilters({ ...filters, gen_art: e.target.value }); setPage(1) }}
                 className="text-xs px-2 py-1.5 border border-gray-200 rounded" />
          <select value={filters.status}
                  onChange={e => { setFilters({ ...filters, status: e.target.value }); setPage(1) }}
                  className="text-xs px-2 py-1.5 border border-gray-200 rounded">
            <option value="">Any status</option>
            <option value="NL">NL</option>
            <option value="TBL">TBL</option>
          </select>
        </div>

        {/* Table */}
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-gray-500 uppercase border-b border-gray-200 sticky top-0 bg-white">
              <tr>
                <th className="text-left py-2 font-medium px-2">WERKS</th>
                <th className="text-left py-2 font-medium px-2">MAJ_CAT</th>
                <th className="text-left py-2 font-medium px-2">GEN_ART</th>
                <th className="text-left py-2 font-medium px-2">CLR</th>
                <th className="text-left py-2 font-medium px-2">VAR_ART</th>
                <th className="text-left py-2 font-medium px-2">SZ</th>
                <th className="text-center py-2 font-medium px-2">Status</th>
                <th className="text-right py-2 font-medium px-2">Initial</th>
                <th className="text-right py-2 font-medium px-2">Remaining</th>
                <th className="text-right py-2 font-medium px-2">Age (d)</th>
                <th className="text-left py-2 font-medium px-2">Listed</th>
                <th className="text-center py-2 font-medium px-2">Closed?</th>
                <th className="text-left py-2 font-medium px-2">Last change</th>
                <th className="text-center py-2 font-medium px-2">Action</th>
              </tr>
            </thead>
            <tbody>
              {detailLoading ? (
                <tr><td colSpan={14} className="py-8 text-center text-gray-400">
                  <Loader2 size={18} className="inline animate-spin" />
                </td></tr>
              ) : detail.items.length === 0 ? (
                <tr><td colSpan={14} className="py-8 text-center text-gray-400">No rows match the filters</td></tr>
              ) : detail.items.map((r, i) => (
                <tr key={i} className={`border-b border-gray-100 hover:bg-gray-50 ${r.is_closed ? 'opacity-60' : ''}`}>
                  <td className="px-2 py-1.5">{r.werks}</td>
                  <td className="px-2 py-1.5 text-gray-600">{r.maj_cat}</td>
                  <td className="px-2 py-1.5 font-mono">{r.gen_art_number}</td>
                  <td className="px-2 py-1.5">{r.clr}</td>
                  <td className="px-2 py-1.5 font-mono">{r.var_art}</td>
                  <td className="px-2 py-1.5">{r.sz}</td>
                  <td className="px-2 py-1.5 text-center">
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                      r.opt_status === 'NL'  ? 'bg-emerald-100 text-emerald-700' :
                      r.opt_status === 'TBL' ? 'bg-amber-100 text-amber-700'    :
                                              'bg-gray-100 text-gray-600'
                    }`}>{r.opt_status || '—'}</span>
                  </td>
                  <td className="px-2 py-1.5 text-right">{fmt(r.hold_qty_initial)}</td>
                  <td className="px-2 py-1.5 text-right font-semibold">{fmt(r.hold_rem)}</td>
                  <td className={`px-2 py-1.5 text-right ${r.age_days > 30 ? 'text-rose-600 font-semibold' : ''}`}>{r.age_days}</td>
                  <td className="px-2 py-1.5 text-gray-500">{r.listed_date?.slice(0, 10)}</td>
                  <td className="px-2 py-1.5 text-center">
                    {r.is_closed ? <span className="text-gray-400">✓</span> : <span className="text-emerald-600">open</span>}
                  </td>
                  <td className="px-2 py-1.5 max-w-[200px]"
                      title={
                        r.last_remarks
                          ? `${r.last_remarks}${r.last_updated_by ? `\n— by ${r.last_updated_by}` : ''}`
                          : ''
                      }>
                    {r.last_remarks ? (
                      <div className="leading-tight">
                        <div className="text-gray-700 truncate">{r.last_remarks}</div>
                        {r.last_updated_by && (
                          <div className="text-[10px] text-gray-500">
                            by <span className="font-medium">{r.last_updated_by}</span>
                          </div>
                        )}
                      </div>
                    ) : (
                      <span className="text-gray-300">—</span>
                    )}
                  </td>
                  <td className="px-2 py-1.5 text-center">
                    {r.is_closed ? (
                      <button onClick={() => openRowRevise(r)}
                              title="Re-open this closed hold by adding qty"
                              className="inline-flex items-center justify-center w-6 h-6 rounded border border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-100">
                        <Plus size={11} />
                      </button>
                    ) : (
                      <div className="inline-flex items-center gap-1">
                        <button onClick={() => openRowClear(r)}
                                title="Cancel or release stock from this hold row"
                                className="inline-flex items-center justify-center w-6 h-6 rounded border border-rose-200 bg-rose-50 text-rose-700 hover:bg-rose-100">
                          <Unlock size={11} />
                        </button>
                        <button onClick={() => openRowRevise(r)}
                                title="Increase HOLD_REM (revise upward)"
                                className="inline-flex items-center justify-center w-6 h-6 rounded border border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-100">
                          <Plus size={11} />
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex items-center justify-between mt-3 text-xs">
            <button onClick={() => setPage(Math.max(1, page - 1))} disabled={page <= 1}
                    className="px-2 py-1 border border-gray-200 rounded hover:bg-gray-50 disabled:opacity-40 flex items-center gap-1">
              <ChevronLeft size={12} /> Prev
            </button>
            <span className="text-gray-600">Page {page} of {totalPages}</span>
            <button onClick={() => setPage(Math.min(totalPages, page + 1))} disabled={page >= totalPages}
                    className="px-2 py-1 border border-gray-200 rounded hover:bg-gray-50 disabled:opacity-40 flex items-center gap-1">
              Next <ChevronRight size={12} />
            </button>
          </div>
        )}
      </Section>

      {/* Single-row clear-hold modal — opened from the table's Clear button. */}
      {clearTarget && (
        <div onClick={() => !clearSubmitting && closeRowClear()}
             className="fixed inset-0 z-40 bg-black/40 flex items-center justify-center">
          <div onClick={e => e.stopPropagation()}
               className="bg-white rounded-xl shadow-xl w-[480px] max-w-[92vw]">
            <div className="px-4 py-3 border-b border-gray-200 flex items-center gap-2">
              <Unlock size={16} className="text-rose-600" />
              <div className="font-semibold text-sm">Clear hold row</div>
              <div className="flex-1" />
              <button onClick={closeRowClear} disabled={clearSubmitting}
                      className="text-gray-400 hover:text-gray-600 disabled:opacity-50">
                <X size={16} />
              </button>
            </div>
            <div className="p-4 space-y-3 text-xs">
              <div className="grid grid-cols-2 gap-x-3 gap-y-1 bg-gray-50 border border-gray-200 rounded p-2">
                <div><span className="text-gray-500">WERKS</span> <b>{clearTarget.werks}</b></div>
                <div><span className="text-gray-500">VAR_ART</span> <b className="font-mono">{clearTarget.var_art}</b></div>
                <div><span className="text-gray-500">SZ</span> <b>{clearTarget.sz || '(blank)'}</b></div>
                <div><span className="text-gray-500">MAJ_CAT</span> <b>{clearTarget.maj_cat}</b></div>
                <div><span className="text-gray-500">Initial</span> <b>{fmt(clearTarget.hold_qty_initial)}</b></div>
                <div><span className="text-gray-500">Remaining</span> <b>{fmt(clearTarget.hold_rem)}</b></div>
              </div>

              <div>
                <label className="block text-gray-600 mb-1">
                  Release qty <span className="text-gray-400">(blank = full close)</span>
                </label>
                <input type="number" min="0" step="1"
                       value={clearReleaseQty}
                       onChange={e => setClearReleaseQty(e.target.value)}
                       placeholder={`up to ${fmt(clearTarget.hold_rem)}`}
                       className="w-full px-2 py-1.5 border border-gray-200 rounded" />
                <div className="text-[10px] text-gray-500 mt-1">
                  Anything ≥ {fmt(clearTarget.hold_rem)} fully closes the row. A smaller
                  value reduces HOLD_REM and leaves the row open.
                </div>
              </div>

              <div>
                <label className="block text-gray-600 mb-1">
                  Reason <span className="text-rose-500">*</span>
                </label>
                <input value={clearReason}
                       onChange={e => setClearReason(e.target.value)}
                       placeholder="e.g. stock physically depleted / bot mis-held"
                       className={`w-full px-2 py-1.5 border rounded ${
                         clearReason.trim() ? 'border-gray-200' : 'border-rose-300 bg-rose-50/30'
                       }`} />
                {!clearReason.trim() && (
                  <div className="text-[10px] text-rose-600 mt-1">Required — used for audit.</div>
                )}
              </div>

              <div className="text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded p-2 flex gap-2">
                <AlertTriangle size={12} className="mt-0.5 shrink-0" />
                <div>
                  Re-syncs MSA HOLD_QTY/FNL_Q for the affected (RDC, ARTICLE) so the
                  released qty is allocatable in the next run. Logged as
                  <code className="px-1 bg-amber-100 rounded">HOLD_CLEAR</code> in the
                  Operations Log and stamped on the row as
                  <code className="px-1 bg-amber-100 rounded">LAST_REMARKS</code> + <code className="px-1 bg-amber-100 rounded">LAST_UPDATED_BY</code>.
                </div>
              </div>
            </div>
            <div className="px-4 py-3 border-t border-gray-200 flex justify-end gap-2">
              <button onClick={closeRowClear} disabled={clearSubmitting}
                      className="px-3 py-1.5 text-xs border border-gray-200 rounded hover:bg-gray-50 disabled:opacity-50">
                Cancel
              </button>
              <button onClick={submitRowClear}
                      disabled={clearSubmitting || !clearReason.trim()}
                      className="px-3 py-1.5 text-xs font-semibold bg-rose-600 text-white rounded hover:bg-rose-700 disabled:opacity-50 flex items-center gap-1">
                <Send size={12} /> {clearSubmitting ? 'Clearing…' : 'Clear hold'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Bulk clear-hold modal — CSV/Excel upload. */}
      {bulkClearOpen && (
        <div onClick={() => !clearSubmitting && setBulkClearOpen(false)}
             className="fixed inset-0 z-40 bg-black/40 flex items-center justify-center">
          <div onClick={e => e.stopPropagation()}
               className="bg-white rounded-xl shadow-xl w-[520px] max-w-[92vw]">
            <div className="px-4 py-3 border-b border-gray-200 flex items-center gap-2">
              <Unlock size={16} className="text-rose-600" />
              <div className="font-semibold text-sm">Bulk clear hold</div>
              <div className="flex-1" />
              <button onClick={downloadBulkTemplate}
                      title="Download a sample CSV with the four columns"
                      className="text-xs px-2 py-1 border border-gray-200 rounded text-indigo-600 hover:bg-gray-50 flex items-center gap-1">
                <Download size={12} /> Template
              </button>
              <button onClick={() => setBulkClearOpen(false)} disabled={clearSubmitting}
                      className="text-gray-400 hover:text-gray-600 disabled:opacity-50">
                <X size={16} />
              </button>
            </div>
            <div className="p-4 space-y-3 text-xs">
              <div className="text-gray-600">
                CSV / Excel with columns: <code className="px-1 bg-gray-100 rounded">WERKS, VAR_ART</code>.
                Optional: <code className="px-1 bg-gray-100 rounded">SZ, RELEASE_QTY</code>.
                <div className="mt-1 text-[10px] text-gray-500">
                  Blank <code className="px-1 bg-gray-100 rounded">SZ</code> matches every size for that
                  <code className="px-1 bg-gray-100 rounded">(WERKS, VAR_ART)</code>. Blank
                  <code className="px-1 bg-gray-100 rounded">RELEASE_QTY</code> fully closes each matched row.
                </div>
              </div>

              <input type="file" accept=".csv,.xlsx,.xls"
                     onChange={e => setBulkClearFile(e.target.files?.[0] || null)}
                     className="w-full text-xs" />

              <div>
                <label className="block text-gray-600 mb-1">
                  Reason <span className="text-rose-500">*</span>
                </label>
                <input value={clearReason}
                       onChange={e => setClearReason(e.target.value)}
                       placeholder="applies to all rows in the file"
                       className={`w-full px-2 py-1.5 border rounded ${
                         clearReason.trim() ? 'border-gray-200' : 'border-rose-300 bg-rose-50/30'
                       }`} />
                {!clearReason.trim() && (
                  <div className="text-[10px] text-rose-600 mt-1">Required — used for audit.</div>
                )}
              </div>

              <div className="text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded p-2 flex gap-2">
                <AlertTriangle size={12} className="mt-0.5 shrink-0" />
                <div>
                  Each row fully closes its hold unless <code className="px-1 bg-amber-100 rounded">RELEASE_QTY</code> is given.
                  MSA HOLD_QTY/FNL_Q is re-synced for every affected (RDC, ARTICLE).
                </div>
              </div>
            </div>
            <div className="px-4 py-3 border-t border-gray-200 flex justify-end gap-2">
              <button onClick={() => setBulkClearOpen(false)} disabled={clearSubmitting}
                      className="px-3 py-1.5 text-xs border border-gray-200 rounded hover:bg-gray-50 disabled:opacity-50">
                Cancel
              </button>
              <button onClick={submitBulkClear}
                      disabled={clearSubmitting || !bulkClearFile || !clearReason.trim()}
                      className="px-3 py-1.5 text-xs font-semibold bg-rose-600 text-white rounded hover:bg-rose-700 disabled:opacity-50 flex items-center gap-1">
                <Upload size={12} /> {clearSubmitting ? 'Uploading…' : 'Submit file'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Single-row revise-hold modal — opened from the table's Plus button. */}
      {reviseTarget && (
        <div onClick={() => !clearSubmitting && closeRowRevise()}
             className="fixed inset-0 z-40 bg-black/40 flex items-center justify-center">
          <div onClick={e => e.stopPropagation()}
               className="bg-white rounded-xl shadow-xl w-[480px] max-w-[92vw]">
            <div className="px-4 py-3 border-b border-gray-200 flex items-center gap-2">
              <Plus size={16} className="text-emerald-600" />
              <div className="font-semibold text-sm">
                {reviseTarget.is_closed ? 'Re-open hold' : 'Increase hold qty'}
              </div>
              <div className="flex-1" />
              <button onClick={closeRowRevise} disabled={clearSubmitting}
                      className="text-gray-400 hover:text-gray-600 disabled:opacity-50">
                <X size={16} />
              </button>
            </div>
            <div className="p-4 space-y-3 text-xs">
              <div className="grid grid-cols-2 gap-x-3 gap-y-1 bg-gray-50 border border-gray-200 rounded p-2">
                <div><span className="text-gray-500">WERKS</span> <b>{reviseTarget.werks}</b></div>
                <div><span className="text-gray-500">VAR_ART</span> <b className="font-mono">{reviseTarget.var_art}</b></div>
                <div><span className="text-gray-500">SZ</span> <b>{reviseTarget.sz || '(blank)'}</b></div>
                <div><span className="text-gray-500">MAJ_CAT</span> <b>{reviseTarget.maj_cat}</b></div>
                <div><span className="text-gray-500">Initial</span> <b>{fmt(reviseTarget.hold_qty_initial)}</b></div>
                <div><span className="text-gray-500">Remaining</span> <b>{fmt(reviseTarget.hold_rem)}</b></div>
              </div>

              <div>
                <label className="block text-gray-600 mb-1">
                  Add qty <span className="text-rose-500">*</span>
                </label>
                <input type="number" min="1" step="1"
                       value={reviseAddQty}
                       onChange={e => setReviseAddQty(e.target.value)}
                       placeholder="positive number — added to HOLD_REM and HOLD_QTY_INITIAL"
                       className="w-full px-2 py-1.5 border border-gray-200 rounded" />
                <div className="text-[10px] text-gray-500 mt-1">
                  {reviseTarget.is_closed
                    ? 'Row is closed — adding qty re-opens it with this value as the new initial.'
                    : `Adds to current HOLD_REM (${fmt(reviseTarget.hold_rem)}) and HOLD_QTY_INITIAL (${fmt(reviseTarget.hold_qty_initial)}).`}
                </div>
              </div>

              <div>
                <label className="block text-gray-600 mb-1">
                  Reason <span className="text-rose-500">*</span>
                </label>
                <input value={reviseReason}
                       onChange={e => setReviseReason(e.target.value)}
                       placeholder="e.g. ops requested extra carve-out for promo"
                       className={`w-full px-2 py-1.5 border rounded ${
                         reviseReason.trim() ? 'border-gray-200' : 'border-rose-300 bg-rose-50/30'
                       }`} />
                {!reviseReason.trim() && (
                  <div className="text-[10px] text-rose-600 mt-1">Required — used for audit.</div>
                )}
              </div>

              <div className="text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded p-2 flex gap-2">
                <AlertTriangle size={12} className="mt-0.5 shrink-0" />
                <div>
                  Re-syncs MSA HOLD_QTY/FNL_Q for the affected (RDC, ARTICLE), so the
                  extra hold reduces FNL_Q in the next allocation. Logged as
                  <code className="px-1 bg-amber-100 rounded">HOLD_REVISE</code> and stamped on the row.
                </div>
              </div>
            </div>
            <div className="px-4 py-3 border-t border-gray-200 flex justify-end gap-2">
              <button onClick={closeRowRevise} disabled={clearSubmitting}
                      className="px-3 py-1.5 text-xs border border-gray-200 rounded hover:bg-gray-50 disabled:opacity-50">
                Cancel
              </button>
              <button onClick={submitRowRevise}
                      disabled={clearSubmitting || !reviseReason.trim()}
                      className="px-3 py-1.5 text-xs font-semibold bg-emerald-600 text-white rounded hover:bg-emerald-700 disabled:opacity-50 flex items-center gap-1">
                <Send size={12} /> {clearSubmitting ? 'Saving…' : 'Increase hold'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Maximized chart modal — zoomable via Brush, with chart/table toggle. */}
      {maximized && (
        <div onClick={() => setMaximized(null)}
             className="fixed inset-0 z-40 bg-black/50 flex items-center justify-center p-4">
          <div onClick={e => e.stopPropagation()}
               className="bg-white rounded-xl shadow-xl w-[92vw] h-[85vh] max-w-[1400px] flex flex-col overflow-hidden">
            <div className="px-4 py-3 border-b border-gray-200 flex items-center gap-3">
              <BarChart3 size={16} className="text-indigo-600" />
              <div className="min-w-0">
                <div className="font-semibold text-sm text-gray-900 truncate">{maximized.label}</div>
                <div className="text-xs text-gray-500 truncate">
                  {maximized.desc} · drag the slider at the bottom of the chart to zoom into a date range
                </div>
              </div>
              <div className="flex-1" />
              <button
                onClick={() => setChartView(v => ({
                  ...v,
                  [maximized.key]: (v[maximized.key] || 'chart') === 'chart' ? 'table' : 'chart',
                }))}
                className="text-xs px-2 py-1 border border-gray-200 rounded hover:bg-gray-50 flex items-center gap-1"
              >
                {(chartView[maximized.key] || 'chart') === 'chart'
                  ? <><TableIcon size={12} /> Table</>
                  : <><BarChart3 size={12} /> Chart</>}
              </button>
              <button onClick={() => setMaximized(null)}
                      className="text-gray-400 hover:text-gray-600 p-1 rounded hover:bg-gray-100">
                <X size={16} />
              </button>
            </div>
            <div className="flex-1 p-4 overflow-hidden min-h-0">
              {(chartView[maximized.key] || 'chart') === 'chart'
                ? renderDailyChart(maximized, buildDailyMetrics(timeline), '100%', true)
                : <DailyChartTable card={maximized} data={buildDailyMetrics(timeline)} maxHeight="100%" />}
            </div>
          </div>
        </div>
      )}

      {/* Bulk revise-hold modal — CSV/Excel upload. */}
      {bulkReviseOpen && (
        <div onClick={() => !clearSubmitting && setBulkReviseOpen(false)}
             className="fixed inset-0 z-40 bg-black/40 flex items-center justify-center">
          <div onClick={e => e.stopPropagation()}
               className="bg-white rounded-xl shadow-xl w-[520px] max-w-[92vw]">
            <div className="px-4 py-3 border-b border-gray-200 flex items-center gap-2">
              <Plus size={16} className="text-emerald-600" />
              <div className="font-semibold text-sm">Bulk revise hold</div>
              <div className="flex-1" />
              <button onClick={downloadReviseTemplate}
                      title="Download a sample CSV"
                      className="text-xs px-2 py-1 border border-gray-200 rounded text-indigo-600 hover:bg-gray-50 flex items-center gap-1">
                <Download size={12} /> Template
              </button>
              <button onClick={() => setBulkReviseOpen(false)} disabled={clearSubmitting}
                      className="text-gray-400 hover:text-gray-600 disabled:opacity-50">
                <X size={16} />
              </button>
            </div>
            <div className="p-4 space-y-3 text-xs">
              <div className="text-gray-600">
                CSV / Excel with columns: <code className="px-1 bg-gray-100 rounded">WERKS, VAR_ART, ADD_QTY</code>.
                Optional: <code className="px-1 bg-gray-100 rounded">SZ</code>.
                <div className="mt-1 text-[10px] text-gray-500">
                  Blank <code className="px-1 bg-gray-100 rounded">SZ</code> matches every size for that
                  <code className="px-1 bg-gray-100 rounded">(WERKS, VAR_ART)</code>.
                  <code className="px-1 bg-gray-100 rounded">ADD_QTY</code> must be a positive number.
                </div>
              </div>

              <input type="file" accept=".csv,.xlsx,.xls"
                     onChange={e => setBulkReviseFile(e.target.files?.[0] || null)}
                     className="w-full text-xs" />

              <div>
                <label className="block text-gray-600 mb-1">
                  Reason <span className="text-rose-500">*</span>
                </label>
                <input value={reviseReason}
                       onChange={e => setReviseReason(e.target.value)}
                       placeholder="applies to all rows in the file"
                       className={`w-full px-2 py-1.5 border rounded ${
                         reviseReason.trim() ? 'border-gray-200' : 'border-rose-300 bg-rose-50/30'
                       }`} />
                {!reviseReason.trim() && (
                  <div className="text-[10px] text-rose-600 mt-1">Required — used for audit.</div>
                )}
              </div>
            </div>
            <div className="px-4 py-3 border-t border-gray-200 flex justify-end gap-2">
              <button onClick={() => setBulkReviseOpen(false)} disabled={clearSubmitting}
                      className="px-3 py-1.5 text-xs border border-gray-200 rounded hover:bg-gray-50 disabled:opacity-50">
                Cancel
              </button>
              <button onClick={submitBulkRevise}
                      disabled={clearSubmitting || !bulkReviseFile || !reviseReason.trim()}
                      className="px-3 py-1.5 text-xs font-semibold bg-emerald-600 text-white rounded hover:bg-emerald-700 disabled:opacity-50 flex items-center gap-1">
                <Upload size={12} /> {clearSubmitting ? 'Uploading…' : 'Submit file'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
