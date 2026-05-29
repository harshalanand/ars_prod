/**
 * GapReportPage — multi-category GAP review surface.
 *
 * Layout (top → bottom):
 *   1. Header + scope filter bar (date | sid | mc | werks | rdc)
 *   2. KPI strip — 8 cards, one per gap category, count + qty + age.
 *      Cards are clickable: clicking jumps to the matching tab.
 *   3. Tab bar grouped Algorithm | Quantity | Lifecycle.
 *   4. Active tab body — per-tab controls + FlatDrillTable + Export.
 *
 * Scope helpers (scopeParams, EMPTY_SCOPE, scopeKey) are reused from
 * ArsDashboardPage. URL state lives entirely in ?tab=…&date=…&sid=…&…
 */
import { useState, useEffect, useMemo, useCallback } from 'react'
import { useSearchParams, Link } from 'react-router-dom'
import {
  AlertTriangle, RefreshCw, Loader2, Download, Filter,
  PackageX, Boxes, Lock, AlertCircle, Clock, TrendingDown, Truck, Archive,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { gapReportAPI } from '../services/api'
import {
  FlatDrillTable,
  EMPTY_SCOPE, scopeParams, scopeKey,
} from './ArsDashboardPage'

const fmt = (n) => (n == null || isNaN(n)) ? '—' : Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 })

/* ─────────────────────────────────────────────────────────────────────────
   Category catalog — single source of truth used by KPI strip + tab bar.
───────────────────────────────────────────────────────────────────────── */
const CATEGORIES = [
  { key: 'excess-stk',       label: 'Excess Stock',          family: 'algorithm', icon: Boxes,
    hint: 'STK above 2× MBQ — stock the store should release.' },
  { key: 'listed-not-alloc', label: 'Listed-Not-Allocated',  family: 'algorithm', icon: PackageX,
    hint: 'OPT eligible but allocation produced 0 ship/hold.' },
  { key: 'skip-reason',      label: 'Skip-Reason',           family: 'algorithm', icon: AlertCircle,
    hint: 'Rows the rule engine SKIPPED, by reason code.' },
  { key: 'hold-anomaly',     label: 'Hold-Without-Ship',     family: 'quantity',  icon: Lock,
    hint: 'TBL HOLD_QTY > 0 but SHIP_QTY = 0.' },
  { key: 'mbq-deviation',    label: 'MBQ Deviation',         family: 'quantity',  icon: TrendingDown,
    hint: 'Post-alloc under MBQ floor or over MJ_REQ ceiling.' },
  { key: 'pend-aging',       label: 'Pend-Alloc Aging',      family: 'lifecycle', icon: Clock,
    hint: 'IS_CLOSED=0 PEND_QTY rows older than threshold.' },
  { key: 'bdc-do-reco',      label: 'BDC vs DO',             family: 'lifecycle', icon: Truck,
    hint: 'BDC sent but SAP DO confirms less.' },
  { key: 'parked-drift',     label: 'Parked Drift',          family: 'lifecycle', icon: Archive,
    hint: 'Sessions stuck in PARKED, never approved/rejected.' },
]
const FAMILIES = [
  { key: 'algorithm', label: 'Algorithm Decision' },
  { key: 'quantity',  label: 'Quantity & Balance' },
  { key: 'lifecycle', label: 'Lifecycle' },
]

const FAMILY_BADGE = {
  algorithm: 'bg-indigo-50 text-indigo-700 border-indigo-200',
  quantity:  'bg-amber-50 text-amber-700 border-amber-200',
  lifecycle: 'bg-emerald-50 text-emerald-700 border-emerald-200',
}

/* ─────────────────────────────────────────────────────────────────────────
   Scope filter bar — single-row, URL-synced.
───────────────────────────────────────────────────────────────────────── */
function ScopeBar({ scope, setScope, onReset, busy, onRefresh }) {
  const csvToArr = (s) => (s || '').split(',').map(x => x.trim()).filter(Boolean)
  const arrToCsv = (a) => (a || []).join(',')
  const upd = (k, v) => setScope({ ...scope, [k]: v })

  return (
    <div className="flex flex-wrap items-end gap-2 p-3 bg-white border border-gray-200 rounded-lg">
      <div className="flex flex-col">
        <label className="text-[10px] text-gray-500 uppercase">Date</label>
        <input type="date" value={scope.date || ''}
               onChange={(e) => upd('date', e.target.value)}
               className="text-xs border border-gray-200 rounded px-2 py-1 w-[140px]" />
      </div>
      <div className="flex flex-col">
        <label className="text-[10px] text-gray-500 uppercase">Session ID</label>
        <input type="text" placeholder="20260528_…" value={scope.sid || ''}
               onChange={(e) => upd('sid', e.target.value)}
               className="text-xs border border-gray-200 rounded px-2 py-1 w-[200px] font-mono" />
      </div>
      <div className="flex flex-col">
        <label className="text-[10px] text-gray-500 uppercase">MAJ_CAT (csv)</label>
        <input type="text" placeholder="FW_K_SLIPPER,…" value={arrToCsv(scope.mc)}
               onChange={(e) => upd('mc', csvToArr(e.target.value))}
               className="text-xs border border-gray-200 rounded px-2 py-1 w-[200px]" />
      </div>
      <div className="flex flex-col">
        <label className="text-[10px] text-gray-500 uppercase">Store / WERKS (csv)</label>
        <input type="text" placeholder="HB05,V001,…" value={arrToCsv(scope.werks)}
               onChange={(e) => upd('werks', csvToArr(e.target.value))}
               className="text-xs border border-gray-200 rounded px-2 py-1 w-[160px]" />
      </div>
      <div className="flex flex-col">
        <label className="text-[10px] text-gray-500 uppercase">RDC (csv)</label>
        <input type="text" placeholder="DH24,2700,…" value={arrToCsv(scope.rdc)}
               onChange={(e) => upd('rdc', csvToArr(e.target.value))}
               className="text-xs border border-gray-200 rounded px-2 py-1 w-[140px]" />
      </div>
      <div className="ml-auto flex items-center gap-2">
        <button onClick={onReset}
                className="text-xs px-2.5 py-1.5 border border-gray-200 rounded hover:bg-gray-50">
          Reset
        </button>
        <button onClick={onRefresh} disabled={busy}
                className="text-xs px-3 py-1.5 bg-primary-600 hover:bg-primary-700 disabled:bg-gray-300 text-white rounded inline-flex items-center gap-1">
          {busy ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
          Refresh
        </button>
      </div>
    </div>
  )
}

/* ─────────────────────────────────────────────────────────────────────────
   KPI strip — 8 cards, click → activate tab.
───────────────────────────────────────────────────────────────────────── */
function KpiStrip({ summary, activeKey, onPick }) {
  const byKey = useMemo(() => {
    const m = {}
    for (const c of summary || []) m[c.key] = c
    return m
  }, [summary])

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-2">
      {CATEGORIES.map((c) => {
        const data = byKey[c.key] || {}
        const Icon = c.icon
        const isActive = c.key === activeKey
        const rows = data.rows ?? 0
        const qty  = data.qty  ?? 0
        const old  = data.oldest_days
        return (
          <button key={c.key} onClick={() => onPick(c.key)}
            className={`text-left p-2.5 border rounded-lg bg-white hover:shadow-md transition-shadow
              ${isActive ? 'border-primary-500 ring-1 ring-primary-300' : 'border-gray-200'}`}
            title={c.hint}>
            <div className="flex items-center gap-1.5 mb-1">
              <Icon size={13} className="text-gray-500" />
              <span className={`text-[9px] uppercase px-1 py-0.5 rounded border ${FAMILY_BADGE[c.family]}`}>
                {c.family[0]}
              </span>
            </div>
            <div className="text-[11px] font-medium text-gray-800">{c.label}</div>
            <div className="mt-1 text-lg font-bold tabular-nums">{fmt(rows)}</div>
            <div className="text-[10px] text-gray-500">
              {qty > 0 && <span>qty {fmt(qty)} · </span>}
              {old != null && <span>{old}d</span>}
              {!qty && old == null && <span>rows</span>}
            </div>
          </button>
        )
      })}
    </div>
  )
}

/* ─────────────────────────────────────────────────────────────────────────
   Tab bar grouped by family.
───────────────────────────────────────────────────────────────────────── */
function TabBar({ activeKey, onPick }) {
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-gray-200">
      {FAMILIES.map((fam) => (
        <div key={fam.key} className="flex items-center gap-1 py-1">
          <span className="text-[10px] uppercase text-gray-400 tracking-wider mr-1">{fam.label}</span>
          {CATEGORIES.filter(c => c.family === fam.key).map((c) => {
            const active = activeKey === c.key
            return (
              <button key={c.key} onClick={() => onPick(c.key)}
                className={`text-xs px-3 py-1.5 rounded-t-md border-b-2 transition-colors
                  ${active
                    ? 'border-primary-600 text-primary-700 font-semibold'
                    : 'border-transparent text-gray-600 hover:text-gray-900 hover:bg-gray-50'}`}>
                {c.label}
              </button>
            )
          })}
        </div>
      ))}
    </div>
  )
}

/* ─────────────────────────────────────────────────────────────────────────
   Per-tab body. Each tab has its own group_by / source / threshold knobs.
───────────────────────────────────────────────────────────────────────── */
const GROUP_OPTIONS = {
  'excess-stk':       [['majcat','MAJ_CAT'],['rdc','RDC'],['store','Store'],['article','GEN_ART · CLR'],['session','Session']],
  'listed-not-alloc': [['majcat','MAJ_CAT'],['opt_type','OPT_TYPE'],['store','Store'],['rdc','RDC'],['article','GEN_ART']],
  'skip-reason':      [['skip_reason','SKIP_REASON'],['majcat','MAJ_CAT'],['opt_type','OPT_TYPE'],['store','Store'],['rdc','RDC']],
  'hold-anomaly':     [['majcat','MAJ_CAT'],['opt_type','OPT_TYPE'],['store','Store'],['rdc','RDC'],['article','GEN_ART']],
  'mbq-deviation':    [['majcat','MAJ_CAT'],['opt_type','OPT_TYPE'],['store','Store'],['rdc','RDC']],
  'pend-aging':       [['majcat','MAJ_CAT'],['rdc_article','RDC + Article'],['store','Store'],['session','Session']],
  'bdc-do-reco':      [['rdc_article','RDC + Article'],['store','Store'],['alloc_no','Alloc No'],['majcat','MAJ_CAT']],
  'parked-drift':     [], // no group_by (always by session)
}

// Map a group_by value (left col in GROUP_OPTIONS) to the row column key that
// carries its value AND the scope key it should narrow when clicked. If
// `scopeKey` is null the click only advances group_by (no filter narrowing).
const DRILL_MAP = {
  majcat:      { rowKey: 'maj_cat',        scopeKey: 'mc'    },
  rdc:         { rowKey: 'rdc',            scopeKey: 'rdc'   },
  store:       { rowKey: 'werks',          scopeKey: 'werks' },
  article:     { rowKey: 'gen_art_number', scopeKey: null    },  // no article scope filter
  session:     { rowKey: 'session_id',     scopeKey: 'sid'   },
  opt_type:    { rowKey: 'opt_type',       scopeKey: null    },
  skip_reason: { rowKey: 'skip_reason',    scopeKey: null    },  // handled via skip_like
  rdc_article: { rowKey: 'rdc',            scopeKey: 'rdc'   },  // composite — narrow on RDC
  alloc_no:    { rowKey: 'allocation_number', scopeKey: null },
}

function colsToTableSchema(cols) {
  // Map backend column slugs to FlatDrillTable column descriptors.
  return (cols || []).map((c) => {
    const isNumber = /qty|rows_n|stores|rdcs|sessions|days|age|do_qty|bdc_qty|pend_qty|alloc_qty|gap_qty|shortfall|overshoot/i.test(c)
    return {
      k: c,
      l: c.replace(/_/g, ' ').toUpperCase(),
      align: isNumber ? 'right' : 'left',
      fmt:   isNumber,
      type:  isNumber ? 'number' : 'text',
    }
  })
}

function TabBody({ tab, scope, setScope, onError }) {
  const [group_by, setGroupBy] = useState(() => (GROUP_OPTIONS[tab]?.[0]?.[0]) || '')
  const [source,   setSource]  = useState('both')
  const [side,     setSide]    = useState('under')        // mbq-deviation
  const [kind,     setKind]    = useState('both')         // parked-drift
  const [minDays,  setMinDays] = useState(tab === 'pend-aging' ? 7 : (tab === 'parked-drift' ? 3 : 0))
  const [skipLike, setSkipLike] = useState('')            // skip-reason filter
  const [data, setData] = useState({ items: [], columns: [], totals: {} })
  const [busy, setBusy] = useState(false)
  const [downloading, setDownloading] = useState(false)

  useEffect(() => {
    // Reset group_by when the tab changes
    setGroupBy((GROUP_OPTIONS[tab]?.[0]?.[0]) || '')
  }, [tab])

  const buildParams = useCallback(() => {
    const p = { ...scopeParams(scope) }
    if (group_by) p.group_by = group_by
    if (['excess-stk','listed-not-alloc','skip-reason','hold-anomaly','mbq-deviation'].includes(tab)) {
      p.source = source
    }
    if (tab === 'mbq-deviation') p.side = side
    if (tab === 'parked-drift')  p.kind = kind
    if (tab === 'pend-aging' && minDays != null) p.min_days = minDays
    if (tab === 'parked-drift' && minDays != null) p.min_days = minDays
    if (tab === 'skip-reason' && skipLike) p.skip_like = skipLike
    return p
  }, [scope, group_by, source, side, kind, minDays, skipLike, tab])

  const apiFn = useMemo(() => ({
    'excess-stk':       gapReportAPI.excessStk,
    'listed-not-alloc': gapReportAPI.listedNotAlloc,
    'skip-reason':      gapReportAPI.skipReason,
    'hold-anomaly':     gapReportAPI.holdAnomaly,
    'mbq-deviation':    gapReportAPI.mbqDeviation,
    'pend-aging':       gapReportAPI.pendAging,
    'bdc-do-reco':      gapReportAPI.bdcDoReco,
    'parked-drift':     gapReportAPI.parkedDrift,
  })[tab], [tab])

  const fetchData = useCallback(async () => {
    if (!apiFn) return
    setBusy(true)
    try {
      const { data } = await apiFn(buildParams())
      const payload = data?.data || {}
      setData({
        items:   payload.items   || [],
        columns: payload.columns || [],
        totals:  payload.totals  || {},
      })
    } catch (e) {
      onError?.(e?.message || 'fetch failed')
    } finally {
      setBusy(false)
    }
  }, [apiFn, buildParams, onError])

  useEffect(() => { fetchData() }, [fetchData])

  const onExport = useCallback(async () => {
    setDownloading(true)
    try {
      const params = { ...buildParams(), gap_type: tab }
      const res = await gapReportAPI.export(params)
      const blob = new Blob([res.data], {
        type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `gap_report_${tab}.xlsx`
      a.click()
      URL.revokeObjectURL(url)
      toast.success('Downloaded')
    } catch (e) {
      toast.error(e?.message || 'export failed')
    } finally {
      setDownloading(false)
    }
  }, [tab, buildParams])

  const columns = useMemo(() => colsToTableSchema(data.columns), [data.columns])
  const groupOpts = GROUP_OPTIONS[tab] || []

  return (
    <div className="space-y-3">
      {/* Tab-specific controls */}
      <div className="flex flex-wrap items-end gap-3 p-3 bg-gray-50 border border-gray-200 rounded-lg">
        {groupOpts.length > 0 && (
          <div className="flex flex-col">
            <label className="text-[10px] text-gray-500 uppercase">Group by</label>
            <select value={group_by} onChange={(e) => setGroupBy(e.target.value)}
                    className="text-xs border border-gray-200 rounded px-2 py-1">
              {groupOpts.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </div>
        )}
        {['excess-stk','listed-not-alloc','skip-reason','hold-anomaly','mbq-deviation'].includes(tab) && (
          <div className="flex flex-col">
            <label className="text-[10px] text-gray-500 uppercase">Source</label>
            <select value={source} onChange={(e) => setSource(e.target.value)}
                    className="text-xs border border-gray-200 rounded px-2 py-1">
              <option value="both">Parked + History</option>
              <option value="parked">Parked only</option>
              <option value="history">History only</option>
            </select>
          </div>
        )}
        {tab === 'mbq-deviation' && (
          <div className="flex flex-col">
            <label className="text-[10px] text-gray-500 uppercase">Side</label>
            <select value={side} onChange={(e) => setSide(e.target.value)}
                    className="text-xs border border-gray-200 rounded px-2 py-1">
              <option value="under">Under (post-alloc &lt; 0.7×MBQ)</option>
              <option value="over">Over (alloc &gt; 1.10×MJ_REQ)</option>
            </select>
          </div>
        )}
        {tab === 'parked-drift' && (
          <div className="flex flex-col">
            <label className="text-[10px] text-gray-500 uppercase">Kind</label>
            <select value={kind} onChange={(e) => setKind(e.target.value)}
                    className="text-xs border border-gray-200 rounded px-2 py-1">
              <option value="both">Listing + Alloc</option>
              <option value="listing">Listing only</option>
              <option value="alloc">Alloc only</option>
            </select>
          </div>
        )}
        {(tab === 'pend-aging' || tab === 'parked-drift') && (
          <div className="flex flex-col">
            <label className="text-[10px] text-gray-500 uppercase">Min days</label>
            <input type="number" min={0} max={365} value={minDays}
                   onChange={(e) => setMinDays(parseInt(e.target.value || '0', 10))}
                   className="text-xs border border-gray-200 rounded px-2 py-1 w-[80px]" />
          </div>
        )}
        <div className="ml-auto flex items-center gap-2">
          <span className="text-[11px] text-gray-500">
            {busy ? 'Loading…' : `${fmt(data.items.length)} rows`}
            {data.totals?.rows_n != null && data.items.length === 0 && ' (no gap)'}
          </span>
          <button onClick={fetchData} disabled={busy}
                  className="text-xs px-2.5 py-1.5 border border-gray-200 rounded hover:bg-gray-50 inline-flex items-center gap-1">
            {busy ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            Reload
          </button>
          <button onClick={onExport} disabled={downloading || busy || data.items.length === 0}
                  className="text-xs px-3 py-1.5 bg-emerald-600 hover:bg-emerald-700 disabled:bg-gray-300 text-white rounded inline-flex items-center gap-1">
            {downloading ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />}
            Excel
          </button>
        </div>
      </div>

      {/* Totals bar */}
      {Object.keys(data.totals || {}).length > 0 && (
        <div className="flex flex-wrap gap-3 px-3 py-2 bg-amber-50/50 border border-amber-200 rounded text-[11px] text-amber-800">
          <span className="font-semibold">Totals:</span>
          {Object.entries(data.totals).map(([k, v]) => (
            <span key={k} className="font-mono">
              {k}=<b>{fmt(v)}</b>
            </span>
          ))}
        </div>
      )}

      {/* Active scope chips — let the user undo a drill without resetting everything */}
      {(scope.mc.length || scope.werks.length || scope.rdc.length || scope.sid || skipLike) ? (
        <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-gray-600">
          <span className="text-[10px] uppercase text-gray-400">Drilled to:</span>
          {scope.mc.map((v) => (
            <button key={'mc-'+v} onClick={() => setScope({ ...scope, mc: scope.mc.filter(x => x !== v) })}
                    className="px-2 py-0.5 bg-indigo-50 text-indigo-700 border border-indigo-200 rounded hover:bg-indigo-100">
              MAJ_CAT={v} ×
            </button>
          ))}
          {scope.rdc.map((v) => (
            <button key={'rdc-'+v} onClick={() => setScope({ ...scope, rdc: scope.rdc.filter(x => x !== v) })}
                    className="px-2 py-0.5 bg-indigo-50 text-indigo-700 border border-indigo-200 rounded hover:bg-indigo-100">
              RDC={v} ×
            </button>
          ))}
          {scope.werks.map((v) => (
            <button key={'werks-'+v} onClick={() => setScope({ ...scope, werks: scope.werks.filter(x => x !== v) })}
                    className="px-2 py-0.5 bg-indigo-50 text-indigo-700 border border-indigo-200 rounded hover:bg-indigo-100">
              WERKS={v} ×
            </button>
          ))}
          {scope.sid && (
            <button onClick={() => setScope({ ...scope, sid: '' })}
                    className="px-2 py-0.5 bg-indigo-50 text-indigo-700 border border-indigo-200 rounded hover:bg-indigo-100 font-mono">
              SID={scope.sid} ×
            </button>
          )}
          {skipLike && (
            <button onClick={() => setSkipLike('')}
                    className="px-2 py-0.5 bg-indigo-50 text-indigo-700 border border-indigo-200 rounded hover:bg-indigo-100">
              SKIP_REASON LIKE {skipLike} ×
            </button>
          )}
        </div>
      ) : null}

      <FlatDrillTable
        rows={data.items}
        columns={columns}
        onRowClick={(r) => handleDrill(r)}
        emptyText={busy ? 'loading…' : 'no gap rows for this scope'}
      />
    </div>
  )

  function handleDrill(row) {
    const opts = GROUP_OPTIONS[tab] || []
    const idx  = opts.findIndex(([v]) => v === group_by)
    const cur  = group_by
    const dm   = DRILL_MAP[cur]

    // 1) Skip-reason: the SKIP_REASON value isn't a scope filter — use the
    // skip_like backend param and advance to MAJ_CAT.
    if (cur === 'skip_reason') {
      const sr = row.skip_reason || row.SKIP_REASON
      if (sr) {
        setSkipLike(sr)
        setGroupBy('majcat')
      }
      return
    }

    // 2) Session row → jump straight to Alloc Review (rich per-session view).
    const sid = row.session_id || row.SESSION_ID
    if (cur === 'session' || (sid && !dm)) {
      if (sid) window.open(`/alc-review?sid=${encodeURIComponent(sid)}`, '_blank')
      return
    }

    // 3) Narrow scope (if this dim has a scope key) AND/OR advance group_by.
    let nextScope = scope
    if (dm && dm.scopeKey && dm.rowKey) {
      const v = row[dm.rowKey] != null ? String(row[dm.rowKey]) : null
      if (v) {
        if (dm.scopeKey === 'sid') {
          nextScope = { ...scope, sid: v }
        } else {
          const arr = scope[dm.scopeKey] || []
          if (!arr.includes(v)) nextScope = { ...scope, [dm.scopeKey]: [...arr, v] }
        }
        setScope(nextScope)
      }
    }

    // Advance to the next group_by in the ladder if one exists. Skip past
    // dims whose value is already pinned in scope (no point grouping by a
    // dim with only one value).
    for (let i = idx + 1; i < opts.length; i++) {
      const [nextDim] = opts[i]
      const ndm = DRILL_MAP[nextDim]
      if (ndm && ndm.scopeKey && Array.isArray(nextScope[ndm.scopeKey]) && nextScope[ndm.scopeKey].length === 1) {
        continue
      }
      setGroupBy(nextDim)
      return
    }
    // At the deepest level — if row has a session_id, open alc-review.
    if (sid) window.open(`/alc-review?sid=${encodeURIComponent(sid)}`, '_blank')
  }
}

/* ─────────────────────────────────────────────────────────────────────────
   Top-level page
───────────────────────────────────────────────────────────────────────── */
export default function GapReportPage() {
  const [search, setSearch] = useSearchParams()

  // Init scope from URL
  const initialScope = useMemo(() => ({
    ...EMPTY_SCOPE,
    date:  search.get('date')  || '',
    sid:   search.get('sid')   || '',
    mc:    (search.get('mc')    || '').split(',').filter(Boolean),
    werks: (search.get('werks') || '').split(',').filter(Boolean),
    rdc:   (search.get('rdc')   || '').split(',').filter(Boolean),
  }), [])  // eslint-disable-line react-hooks/exhaustive-deps

  const [scope,   setScope]   = useState(initialScope)
  const [tab,     setTab]     = useState(search.get('tab') || 'excess-stk')
  const [summary, setSummary] = useState([])
  const [busy,    setBusy]    = useState(false)

  // Push scope + tab to URL whenever they change
  useEffect(() => {
    const next = new URLSearchParams()
    const p = scopeParams(scope)
    for (const [k, v] of Object.entries(p)) next.set(k, v)
    if (tab) next.set('tab', tab)
    setSearch(next, { replace: true })
  }, [scope, tab])  // eslint-disable-line react-hooks/exhaustive-deps

  const reloadSummary = useCallback(async () => {
    setBusy(true)
    try {
      const { data } = await gapReportAPI.summary(scopeParams(scope))
      setSummary(data?.data?.categories || [])
    } catch (e) {
      toast.error(e?.message || 'summary failed')
    } finally {
      setBusy(false)
    }
  }, [scope])

  useEffect(() => { reloadSummary() }, [scopeKey(scope)])  // eslint-disable-line react-hooks/exhaustive-deps

  const reset = () => setScope(EMPTY_SCOPE)

  return (
    <div className="p-4 space-y-4">
      {/* Header */}
      <div className="flex items-center gap-3">
        <div className="p-2 bg-rose-50 rounded">
          <AlertTriangle size={18} className="text-rose-600" />
        </div>
        <div>
          <h1 className="text-base font-semibold text-gray-900">GAP Report</h1>
          <p className="text-[11px] text-gray-500">
            Algorithm-driven anomalies across listing &amp; allocation runs — parked, history, hold &amp; pending.
          </p>
        </div>
        <Link to="/ars-dashboard"
              className="ml-auto text-[11px] text-primary-600 hover:underline">
          ← back to ARS Dashboard
        </Link>
      </div>

      {/* Scope filter */}
      <ScopeBar scope={scope} setScope={setScope} onReset={reset}
                busy={busy} onRefresh={reloadSummary} />

      {/* KPI strip */}
      <KpiStrip summary={summary} activeKey={tab} onPick={setTab} />

      {/* Tab bar */}
      <TabBar activeKey={tab} onPick={setTab} />

      {/* Tab body */}
      <TabBody tab={tab} scope={scope} setScope={setScope} onError={(m) => toast.error(m)} />
    </div>
  )
}
