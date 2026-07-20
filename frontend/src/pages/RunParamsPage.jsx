/**
 * RunParamsPage — review every tunable + condition that produced a listing run.
 *
 * Reads ARS_RUN_PARAMS_AUDIT via GET /listing/sessions/{sid}/run-params.
 * Pure read-only. Pick one session to view its params, or add more run
 * columns (N-way compare) to put several sessions side-by-side with per-row
 * diff highlighting — including the sec-grid-cap fallback (STANDARD vs MATRIX).
 *
 * Opened as its own page (like View Logs) from the Listing cockpit; an
 * optional ?session=<sid> query param preselects the first run.
 * Tabs: Compare (side-by-side runs) and Trends (charts across runs).
 */
import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { listingAPI } from '@/services/api'
import { ChevronLeft, RefreshCw, Plus, GitCompare, X, Sliders, Loader2, TrendingUp } from 'lucide-react'
import {
  LineChart, Line, BarChart, Bar, AreaChart, Area,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from 'recharts'
import { C } from '@/theme/colors'

const MAX_COLS = 6
const LETTERS = ['A', 'B', 'C', 'D', 'E', 'F']
const COL_COLORS = ['#4f46e5', '#0891b2', '#059669', '#d97706', '#db2777', '#7c3aed']
const TREND_COLORS = ['#4f46e5', '#0891b2', '#059669', '#d97706', '#db2777', '#7c3aed',
  '#dc2626', '#0284c7', '#65a30d', '#c026d3']

const GROUP_META = {
  LISTING:    ['#0891b2', 'Listing'],
  RANKING:    ['#7c3aed', 'Ranking'],
  ALLOCATION: ['#059669', 'Allocation'],
  SEC_CAP:    ['#db2777', 'Sec-Cap'],
  GRIDS:      ['#0d9488', 'Grids'],
  FLAGS:      ['#d97706', 'Flags'],
}

const shortTs = (ts) => (ts ? String(ts).replace('T', ' ').slice(5, 16) : '—')  // MM-DD HH:MM
const COND_META = [
  ['sec_cap_mode',            'Sec-Cap', '#db2777'],
  ['apply_sec_cap_in_normal', 'Sec-Cap in normal', '#db2777'],
  ['cont_fallback_mode',      'CONT fallback', '#0891b2'],
  ['rl_dispatch_mode',        'RL dispatch', '#059669'],
  ['tbc_dispatch_mode',       'TBC dispatch', '#059669'],
  ['alloc_type',              'Pool', '#7c3aed'],
  ['mix_mode',                'MIX', '#64748b'],
  ['rdc_mode',                'RDC', '#64748b'],
  ['run_mode',                'Run', '#64748b'],
]

const pill = (color) => ({
  fontSize: 10, fontWeight: 700, color, background: `${color}15`, padding: '2px 8px',
  borderRadius: 4, border: `1px solid ${color}40`, display: 'inline-flex', alignItems: 'center', gap: 4,
})

function formatValue(name, value) {
  if (name === 'growth_matrix' && typeof value === 'string' && value.trim().startsWith('{')) {
    try {
      const gm = JSON.parse(value)
      const n = Array.isArray(gm.bands) ? gm.bands.length : 0
      return `${gm.enabled ? 'MATRIX' : 'STANDARD'} · ${n} band${n === 1 ? '' : 's'}`
    } catch { /* fall through */ }
  }
  return value == null || value === '' ? '—' : String(value)
}

const flatMap = (groups) => {
  const flat = {}
  Object.values(groups || {}).forEach(rows => (rows || []).forEach(r => { flat[r.name] = r.value }))
  return flat
}

const sessLabel = (s) => {
  const ts = (s.started_at || '').replace('T', ' ').slice(0, 19)
  return `${s.session_id}  ·  ${s.status || '—'}${ts ? '  ·  ' + ts : ''}`
}

const chartCard = { background: '#fff', border: `1px solid ${C.cardBorder}`, borderRadius: 8, padding: '10px 12px' }
const chartTitle = { fontSize: 10, fontWeight: 800, letterSpacing: '.05em', textTransform: 'uppercase', color: C.textSub, marginBottom: 8 }

// Trend as a data table — rows = runs (ascending), columns = each selected param.
function TrendTable({ series }) {
  const ref = series.find(s => s.points?.length)?.points || []
  return (
    <div style={{ ...chartCard, overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
        <thead>
          <tr style={{ fontSize: 9, color: C.textMuted, textAlign: 'left' }}>
            <th style={{ padding: '3px 6px', fontWeight: 800 }}>Run</th>
            {series.map(s => (
              <th key={s.name} style={{ padding: '3px 6px', fontWeight: 800, whiteSpace: 'nowrap' }}>{s.name}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ref.map((p, i) => (
            <tr key={i} style={{ borderBottom: '1px solid #f1f5f9' }}>
              <td style={{ padding: '3px 6px', whiteSpace: 'nowrap' }}>
                <span style={{ color: C.text }}>{shortTs(p.run_ts)}</span>
                <span style={{ color: C.textMuted, fontFamily: 'monospace', marginLeft: 6, fontSize: 9 }}>{p.session_id}</span>
              </td>
              {series.map(s => {
                const v = s.points[i]?.value
                return (
                  <td key={s.name} style={{ padding: '3px 6px', color: C.text, whiteSpace: 'nowrap' }}>
                    {v == null || v === '' ? '—' : String(v)}
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

// Renders the trend series in the chosen form: numeric params share one
// chart (line / bar / area); each categorical param is its own step chart;
// 'table' shows everything as a data grid instead.
function TrendCharts({ series, type = 'line' }) {
  const numeric = series.filter(s => s.numeric && s.points.some(p => p.num != null))
  const categorical = series.filter(s => !s.numeric)
  const ref = series.find(s => s.points?.length)?.points || []
  const numData = ref.map((p, i) => {
    const row = { x: shortTs(p.run_ts) }
    numeric.forEach(s => { row[s.name] = s.points[i]?.num ?? null })
    return row
  })
  if (!numeric.length && !categorical.length) {
    return <div style={{ fontSize: 11, color: C.textMuted }}>No plottable data for the selected params.</div>
  }
  if (type === 'table') return <TrendTable series={series}/>

  const NumChart = type === 'bar' ? BarChart : type === 'area' ? AreaChart : LineChart
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {numeric.length > 0 && (
        <div style={chartCard}>
          <div style={chartTitle}>Numeric trend · {numeric.length} series · {ref.length} runs</div>
          <ResponsiveContainer width="100%" height={300}>
            <NumChart data={numData} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7"/>
              <XAxis dataKey="x" tick={{ fontSize: 10 }} angle={-25} textAnchor="end" height={52}/>
              <YAxis tick={{ fontSize: 10 }}/>
              <Tooltip contentStyle={{ fontSize: 11 }}/>
              <Legend wrapperStyle={{ fontSize: 10 }}/>
              {numeric.map((s, i) => {
                const col = TREND_COLORS[i % TREND_COLORS.length]
                if (type === 'bar') return <Bar key={s.name} dataKey={s.name} fill={col}/>
                if (type === 'area') return (
                  <Area key={s.name} type="monotone" dataKey={s.name} connectNulls
                    stroke={col} fill={col} fillOpacity={0.18} strokeWidth={2}/>
                )
                return (
                  <Line key={s.name} type="monotone" dataKey={s.name} connectNulls
                    stroke={col} strokeWidth={2} dot={{ r: 2 }}/>
                )
              })}
            </NumChart>
          </ResponsiveContainer>
        </div>
      )}
      {categorical.map((s, ci) => {
        const cats = [...new Set(s.points.map(p => (p.value == null ? '—' : String(p.value))))]
        const data = s.points.map(p => ({ x: shortTs(p.run_ts), y: cats.indexOf(p.value == null ? '—' : String(p.value)) }))
        return (
          <div key={s.name} style={chartCard}>
            <div style={chartTitle}>{s.name} · categorical</div>
            <ResponsiveContainer width="100%" height={110 + cats.length * 14}>
              <LineChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7"/>
                <XAxis dataKey="x" tick={{ fontSize: 10 }} angle={-25} textAnchor="end" height={52}/>
                <YAxis type="number" domain={[-0.5, cats.length - 0.5]} ticks={cats.map((_, i) => i)}
                  tickFormatter={(i) => cats[i] ?? ''} tick={{ fontSize: 10 }} width={110}/>
                <Tooltip formatter={(v) => cats[v] ?? v} contentStyle={{ fontSize: 11 }}/>
                <Line type="stepAfter" dataKey="y" stroke={TREND_COLORS[ci % TREND_COLORS.length]}
                  strokeWidth={2} dot={{ r: 3 }}/>
              </LineChart>
            </ResponsiveContainer>
          </div>
        )
      })}
    </div>
  )
}

export default function RunParamsPage() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const preselect = searchParams.get('session') || ''

  const [sessions, setSessions] = useState([])
  const [sids, setSids] = useState([''])        // one entry per run column; [0] is primary
  const [dataMap, setDataMap] = useState({})    // sid -> run-params payload
  const [loading, setLoading] = useState(false)

  // Trends tab state
  const [view, setView] = useState('compare')   // 'compare' | 'trends'
  const [catalog, setCatalog] = useState([])    // [{group,name,numeric}]
  const [trendSel, setTrendSel] = useState([])  // selected param names
  const [trendLimit, setTrendLimit] = useState(30)
  const [chartType, setChartType] = useState('line')  // line | bar | area | table
  const [trendSeries, setTrendSeries] = useState([])
  const [trendLoading, setTrendLoading] = useState(false)

  const loadSessions = async () => {
    try {
      const { data } = await listingAPI.sessions({ limit: 100 })
      const list = data?.sessions || []
      setSessions(list)
      setSids(prev => {
        if (prev.some(Boolean)) return prev            // keep existing selection on refresh
        const def = (preselect && list.find(s => s.session_id === preselect)?.session_id)
          || list[0]?.session_id || ''
        return [def]
      })
    } catch { /* ignore */ }
  }
  useEffect(() => { loadSessions() }, [])              // eslint-disable-line react-hooks/exhaustive-deps

  // Fetch params for every selected run (server-side is cheap + effectively cached).
  useEffect(() => {
    const uniq = [...new Set(sids.filter(Boolean))]
    if (!uniq.length) { setDataMap({}); return }
    setLoading(true)
    Promise.all(uniq.map(s =>
      listingAPI.runParams(s).then(({ data }) => [s, data]).catch(() => [s, null])
    )).then(pairs => setDataMap(Object.fromEntries(pairs)))
      .finally(() => setLoading(false))
  }, [sids])

  // Trends: load the param catalog once.
  useEffect(() => {
    listingAPI.runParamCatalog().then(({ data }) => setCatalog(data?.params || [])).catch(() => {})
  }, [])

  // Trends: fetch series whenever the selection or run-count changes.
  useEffect(() => {
    if (view !== 'trends' || !trendSel.length) { setTrendSeries([]); return }
    setTrendLoading(true)
    listingAPI.runParamTrend(trendSel, trendLimit)
      .then(({ data }) => setTrendSeries(data?.series || []))
      .catch(() => setTrendSeries([]))
      .finally(() => setTrendLoading(false))
  }, [view, trendSel, trendLimit])

  const toggleTrend = (name) =>
    setTrendSel(prev => prev.includes(name) ? prev.filter(n => n !== name) : [...prev, name])

  const setSidAt = (i, v) => setSids(prev => prev.map((s, idx) => (idx === i ? v : s)))
  const addSlot   = () => setSids(prev => (prev.length < MAX_COLS ? [...prev, ''] : prev))
  const removeSlot = (i) => setSids(prev => (prev.length > 1 ? prev.filter((_, idx) => idx !== i) : prev))

  // Active columns = slots with a session chosen.
  const cols = useMemo(() => sids
    .map((sid, i) => ({ sid, i, label: LETTERS[i] || `#${i + 1}`, color: COL_COLORS[i] || '#64748b',
      data: sid ? dataMap[sid] : null }))
    .filter(c => c.sid), [sids, dataMap])
  const multi = cols.length > 1
  const flats = useMemo(() => cols.map(c => flatMap(c.data?.groups)), [cols])

  const groupKeys = useMemo(() => {
    const keys = new Set()
    cols.forEach(c => Object.keys(c.data?.groups || {}).forEach(k => keys.add(k)))
    const order = Object.keys(GROUP_META)
    return [...order.filter(k => keys.has(k)), ...[...keys].filter(k => !order.includes(k))]
  }, [cols])

  // Ordered group keys present in the trend param catalog.
  const catGroupKeys = useMemo(() => {
    const present = new Set(catalog.map(p => p.group))
    const order = Object.keys(GROUP_META)
    return [...order.filter(g => present.has(g)), ...[...present].filter(g => !order.includes(g))]
  }, [catalog])

  const selStyle = { height: 30, fontSize: 11, padding: '0 8px', borderRadius: 6,
    border: `1px solid ${C.cardBorder}`, background: '#fff', color: C.text, minWidth: 320, cursor: 'pointer' }

  const condStrip = (data) => {
    const c = data?.conditions || {}
    const pills = COND_META.filter(([k]) => c[k] != null)
    if (!pills.length) return null
    return (
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
        {pills.map(([k, label, color]) => (
          <span key={k} style={pill(color)}>{label}: {String(c[k]).toUpperCase()}</span>
        ))}
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, padding: 14 }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        background: 'linear-gradient(135deg, #fff 0%, #f8fafc 100%)',
        border: `1px solid ${C.cardBorder}`, borderRadius: 10, padding: '10px 14px',
        boxShadow: '0 1px 3px rgba(0,0,0,0.04)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <button onClick={() => navigate('/data-prep/listing')}
            style={{ height: 28, padding: '0 10px', borderRadius: 6, background: '#fff',
              border: `1px solid ${C.cardBorder}`, cursor: 'pointer', fontSize: 11, fontWeight: 600,
              color: C.text, display: 'flex', alignItems: 'center', gap: 4 }}>
            <ChevronLeft size={12}/> Back to Listing
          </button>
          <div>
            <h1 style={{ fontSize: 15, fontWeight: 700, color: C.text, margin: 0,
              display: 'flex', alignItems: 'center', gap: 10 }}>
              <div style={{ width: 28, height: 28, borderRadius: 7,
                background: `linear-gradient(135deg, ${C.primary}, #7c3aed)`,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                boxShadow: '0 2px 6px rgba(79,70,229,0.3)' }}>
                <Sliders size={14} color="#fff"/>
              </div>
              Run Parameters
            </h1>
            <div style={{ fontSize: 10, color: C.textMuted, marginTop: 4, paddingLeft: 38 }}>
              Every tunable + condition that produced a run (incl. sec-cap STANDARD vs MATRIX) · add runs to compare several side-by-side
            </div>
          </div>
        </div>
        <button onClick={loadSessions}
          style={{ height: 32, padding: '0 14px', borderRadius: 8, fontSize: 12, fontWeight: 700,
            color: '#fff', cursor: 'pointer', border: 'none',
            background: 'linear-gradient(135deg, #4f46e5, #7c3aed)',
            display: 'flex', alignItems: 'center', gap: 6 }}>
          <RefreshCw size={13}/> Refresh
        </button>
      </div>

      {/* View toggle: Compare | Trends */}
      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
        {[['compare', 'Compare', GitCompare], ['trends', 'Trends', TrendingUp]].map(([v, label, Icon]) => (
          <button key={v} onClick={() => setView(v)}
            style={{ height: 30, padding: '0 16px', borderRadius: 8, fontSize: 12, fontWeight: 700,
              cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 6,
              color: view === v ? '#fff' : C.textSub,
              background: view === v ? C.primary : '#fff',
              border: `1px solid ${view === v ? C.primary : C.cardBorder}` }}>
            <Icon size={13}/> {label}
          </button>
        ))}
      </div>

      {view === 'compare' && (<>
      {/* Run selectors — compact single row; Compare adds a 2nd run, then Add run for more */}
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap',
        background: '#fff', border: `1px solid ${C.cardBorder}`, borderRadius: 8, padding: '10px 12px' }}>
        <span style={{ fontSize: 11, fontWeight: 700, color: C.textMuted, textTransform: 'uppercase' }}>
          {multi ? 'Runs' : 'Session'}
        </span>
        {sids.map((sid, i) => {
          const taken = new Set(sids.filter((s, idx) => s && idx !== i))
          const color = COL_COLORS[i] || '#64748b'
          return (
            <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
              {multi && (
                <span style={{ ...pill(color), minWidth: 20, justifyContent: 'center', fontWeight: 800, padding: '2px 6px' }}>
                  {LETTERS[i] || i + 1}
                </span>
              )}
              <select value={sid} onChange={e => setSidAt(i, e.target.value)} style={selStyle}>
                <option value="">— select a run —</option>
                {sessions.filter(s => s.session_id === sid || !taken.has(s.session_id)).map(s =>
                  <option key={s.session_id} value={s.session_id}>{sessLabel(s)}</option>)}
              </select>
              {sids.length > 1 && (
                <button onClick={() => removeSlot(i)} title="Remove this run"
                  style={{ height: 24, width: 24, borderRadius: 6, background: '#fff',
                    border: `1px solid ${C.cardBorder}`, cursor: 'pointer', color: C.textSub,
                    display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                  <X size={12}/>
                </button>
              )}
            </span>
          )
        })}
        <button onClick={addSlot} disabled={sids.length >= MAX_COLS}
          title={sids.length >= MAX_COLS ? `Max ${MAX_COLS} runs`
            : (multi ? 'Add another run to compare' : 'Compare with another run')}
          style={{ height: 30, padding: '0 14px', borderRadius: 8, fontSize: 12, fontWeight: 700,
            cursor: sids.length >= MAX_COLS ? 'not-allowed' : 'pointer',
            color: sids.length >= MAX_COLS ? C.textMuted : (multi ? C.primary : '#fff'),
            background: multi ? '#fff' : C.primary,
            border: `1px solid ${sids.length >= MAX_COLS ? C.cardBorder : C.primary}`,
            opacity: sids.length >= MAX_COLS ? 0.6 : 1,
            display: 'flex', alignItems: 'center', gap: 6 }}>
          {multi ? <Plus size={13}/> : <GitCompare size={13}/>}
          {multi ? 'Add run' : 'Compare'}
        </button>
        {loading && <Loader2 size={14} className="animate-spin" color={C.primary}/>}
        {multi && <span style={{ fontSize: 10, color: C.textMuted }}>· differing rows highlighted</span>}
      </div>

      {/* Conditions — compact, one tight line per run in a single card */}
      {cols.some(c => c.data) && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, background: '#fff',
          border: `1px solid ${C.cardBorder}`, borderRadius: 8, padding: '10px 12px' }}>
          {cols.map(c => (
            <div key={c.sid} style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
              {multi && <span style={{ ...pill(c.color), fontWeight: 800, padding: '2px 7px' }}>{c.label}</span>}
              <span style={{ fontSize: 10, color: C.textMuted, fontFamily: 'monospace' }}>{c.sid}</span>
              {c.data?.run_ts && (
                <span style={{ fontSize: 10, color: C.textMuted }}>· {String(c.data.run_ts).replace('T', ' ').slice(0, 19)} UTC</span>
              )}
              {c.data?.note && <span style={{ fontSize: 10, color: C.textMuted }}>· {c.data.note}</span>}
              {condStrip(c.data)}
            </div>
          ))}
        </div>
      )}

      {/* Grouped tables — N columns */}
      <div style={{ display: 'grid', gridTemplateColumns: multi ? '1fr' : 'repeat(auto-fit, minmax(340px, 1fr))', gap: 10 }}>
        {groupKeys.map(g => {
          const [color, label] = GROUP_META[g] || ['#64748b', g]
          const names = new Set()
          cols.forEach(c => (c.data?.groups?.[g] || []).forEach(r => names.add(r.name)))
          if (!names.size) return null
          return (
            <div key={g} style={{ background: '#fff', border: `1px solid ${C.cardBorder}`,
              borderRadius: 8, padding: '10px 12px', overflowX: 'auto' }}>
              <div style={{ fontSize: 10, fontWeight: 800, letterSpacing: '.05em', textTransform: 'uppercase',
                color, marginBottom: 6 }}>{label}</div>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                {multi && (
                  <thead>
                    <tr style={{ fontSize: 9, color: C.textMuted, textAlign: 'left' }}>
                      <th style={{ padding: '2px 6px', fontWeight: 700 }}>param</th>
                      {cols.map(c => (
                        <th key={c.sid} title={c.sid} style={{ padding: '2px 6px', fontWeight: 800, color: c.color }}>
                          {c.label}
                        </th>
                      ))}
                    </tr>
                  </thead>
                )}
                <tbody>
                  {[...names].sort().map(nm => {
                    const vals = cols.map((c, ci) => flats[ci][nm])
                    const distinct = new Set(vals.map(v => String(v ?? '')))
                    const diff = multi && distinct.size > 1
                    return (
                      <tr key={nm} style={{ borderBottom: '1px solid #f1f5f9', background: diff ? '#fff7ed' : 'transparent' }}>
                        <td style={{ padding: '3px 6px', color: C.textSub, fontFamily: 'monospace', whiteSpace: 'nowrap' }}>{nm}</td>
                        {cols.map((c, ci) => (
                          <td key={c.sid} style={{ padding: '3px 6px',
                            color: diff ? '#c2410c' : C.text, fontWeight: diff ? 700 : 400 }}>
                            {formatValue(nm, vals[ci])}
                          </td>
                        ))}
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )
        })}
      </div>

      {!cols.length && !loading && (
        <div style={{ fontSize: 11, color: C.textMuted }}>Select a run to see its tunables and conditions.</div>
      )}
      </>)}

      {view === 'trends' && (
        <>
          {/* Param picker */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, background: '#fff',
            border: `1px solid ${C.cardBorder}`, borderRadius: 8, padding: '10px 12px' }}>
            <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
              <span style={{ fontSize: 11, fontWeight: 700, color: C.textMuted, textTransform: 'uppercase' }}>
                Pick params to trend
              </span>
              <span style={{ fontSize: 10, color: C.textMuted }}>{trendSel.length} selected</span>
              {trendSel.length > 0 && (
                <button onClick={() => setTrendSel([])}
                  style={{ fontSize: 10, color: C.primary, background: 'none', border: 'none', cursor: 'pointer' }}>clear</button>
              )}
              <div style={{ flex: 1 }}/>
              {/* View-as selector */}
              <span style={{ fontSize: 11, color: C.textMuted }}>View</span>
              <div style={{ display: 'flex', border: `1px solid ${C.cardBorder}`, borderRadius: 6, overflow: 'hidden' }}>
                {['line', 'bar', 'area', 'table'].map(t => (
                  <button key={t} onClick={() => setChartType(t)}
                    style={{ fontSize: 10, fontWeight: 700, padding: '4px 9px', cursor: 'pointer', border: 'none',
                      textTransform: 'capitalize',
                      background: chartType === t ? C.primary : '#fff',
                      color: chartType === t ? '#fff' : C.textSub,
                      borderRight: t === 'table' ? 'none' : `1px solid ${C.cardBorder}` }}>
                    {t}
                  </button>
                ))}
              </div>
              <span style={{ fontSize: 11, color: C.textMuted }}>Last</span>
              <select value={trendLimit} onChange={e => setTrendLimit(Number(e.target.value))}
                style={{ height: 26, fontSize: 11, borderRadius: 6, border: `1px solid ${C.cardBorder}`, padding: '0 6px', cursor: 'pointer' }}>
                {[10, 20, 50, 100].map(n => <option key={n} value={n}>{n} runs</option>)}
              </select>
              {trendLoading && <Loader2 size={14} className="animate-spin" color={C.primary}/>}
            </div>
            <div style={{ maxHeight: 210, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 6 }}>
              {catalog.length === 0 && (
                <div style={{ fontSize: 10, color: C.textMuted }}>No audited params yet — run a listing generate first.</div>
              )}
              {catGroupKeys.map(g => {
                const items = catalog.filter(p => p.group === g)
                if (!items.length) return null
                const [color, label] = GROUP_META[g] || ['#64748b', g]
                return (
                  <div key={g} style={{ display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'flex-start' }}>
                    <span style={{ fontSize: 9, fontWeight: 800, textTransform: 'uppercase', color, minWidth: 66, paddingTop: 3 }}>{label}</span>
                    {items.map(p => {
                      const on = trendSel.includes(p.name)
                      return (
                        <button key={p.name} onClick={() => toggleTrend(p.name)}
                          title={p.numeric ? 'numeric' : 'categorical'}
                          style={{ fontSize: 10, padding: '2px 7px', borderRadius: 4, cursor: 'pointer',
                            border: `1px solid ${on ? C.primary : C.cardBorder}`,
                            background: on ? `${C.primary}15` : '#fff',
                            color: on ? C.primary : C.textSub, fontWeight: on ? 700 : 500 }}>
                          {p.name}{p.numeric ? '' : ' ∙'}
                        </button>
                      )
                    })}
                  </div>
                )
              })}
            </div>
            <div style={{ fontSize: 9, color: C.textMuted }}>∙ = categorical (step chart / column in table); the rest are numeric (line/bar/area, or column in table).</div>
          </div>
          {/* Charts */}
          {trendSel.length === 0
            ? <div style={{ fontSize: 11, color: C.textMuted }}>Select one or more params above to plot their trend across runs.</div>
            : (trendSeries.length === 0 && !trendLoading)
              ? <div style={{ fontSize: 11, color: C.textMuted }}>No data — needs at least one audited run.</div>
              : <TrendCharts series={trendSeries} type={chartType}/>}
        </>
      )}
    </div>
  )
}
