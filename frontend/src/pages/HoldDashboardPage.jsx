/**
 * HoldDashboardPage — Review HOLD_QTY in various angles.
 *
 * Reads from ARS_NL_TBL_HOLD_TRACKING via the hold-dashboard endpoints.
 * Sections: KPI cards, by-RDC, by-store, by-article, by-status, age buckets,
 * timeline, drill-down detail, reconciliation banner.
 */
import { useState, useEffect, useCallback } from 'react'
import { holdDashboardAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  Lock, RefreshCw, Building2, Boxes, AlertTriangle, Calendar,
  Loader2, Filter, X, ChevronLeft, ChevronRight,
} from 'lucide-react'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer, LineChart, Line, PieChart, Pie, Cell,
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
            <button onClick={clearFilters}
                    className="text-xs px-2 py-1 border border-gray-200 rounded hover:bg-gray-50 flex items-center gap-1">
              <X size={12} /> Clear
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
              </tr>
            </thead>
            <tbody>
              {detailLoading ? (
                <tr><td colSpan={12} className="py-8 text-center text-gray-400">
                  <Loader2 size={18} className="inline animate-spin" />
                </td></tr>
              ) : detail.items.length === 0 ? (
                <tr><td colSpan={12} className="py-8 text-center text-gray-400">No rows match the filters</td></tr>
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
    </div>
  )
}
