/**
 * TrendDashboardPage — Dynamic visual trend charts, filters, and summary tables
 */
import { useState, useEffect, useMemo, useCallback } from 'react'
import { trendsAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  TrendingUp, Database, Calendar, BarChart3, RefreshCw, Download, X, Filter, ChevronDown, ChevronUp,
  Loader2, Table2, LineChart as LineChartIcon, PieChart as PieChartIcon, SlidersHorizontal
} from 'lucide-react'
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, Legend, ResponsiveContainer, Area, AreaChart, PieChart, Pie, Cell,
  ScatterChart, Scatter, ComposedChart
} from 'recharts'

const HIDDEN_SYS = new Set(['VERSION','UPLOAD_DATETIME','SYSTEM_IP','SYSTEM_NAME','SYSTEM_LOGIN_ID'])
const COLORS = ['#4f46e5','#06b6d4','#f59e0b','#10b981','#ef4444','#8b5cf6','#ec4899','#14b8a6','#f97316','#6366f1']
const AGG_FUNCS = ['SUM','AVG','COUNT','MAX','MIN']
const CHART_TYPES = ['bar','line','area','stacked','pie','composed']
const DATE_GRAINS = ['day','week','month','quarter','year']

export default function TrendDashboardPage() {
  const [tables, setTables] = useState([])
  const [sel, setSel] = useState('')
  const [schema, setSchema] = useState(null)
  const [data, setData] = useState([])
  const [loading, setLoading] = useState(false)
  const [chartType, setChartType] = useState('bar')
  const [groupCol, setGroupCol] = useState('')
  const [metricCols, setMetricCols] = useState([])
  const [summaryData, setSummaryData] = useState([])
  const [aggFunc, setAggFunc] = useState('SUM')
  const [sortDir, setSortDir] = useState('desc')
  const [showFilters, setShowFilters] = useState(false)
  const [limit, setLimit] = useState(10000)
  const [topN, setTopN] = useState(50)
  const [viewMode, setViewMode] = useState('summary')  // 'summary' | 'trend'
  const [dateGrain, setDateGrain] = useState('day')     // day | week | month | quarter | year
  const [trendData, setTrendData] = useState([])
  const [trendBreakdown, setTrendBreakdown] = useState('')  // optional: split trend by this column

  // Filters
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [colFilters, setColFilters] = useState({})       // { col: [val1, val2] }
  const [filterCol, setFilterCol] = useState('')          // column being filtered
  const [distinctVals, setDistinctVals] = useState([])    // distinct values for filterCol
  const [distinctLoading, setDistinctLoading] = useState(false)
  const [filterSearch, setFilterSearch] = useState('')

  useEffect(() => {
    trendsAPI.listTables().then(r => { const d = r.data?.data; setTables(d?.tables || (Array.isArray(d) ? d : [])) }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!sel) { setSchema(null); setData([]); setSummaryData([]); return }
    trendsAPI.getSchema(sel).then(r => {
      const s = r.data?.data || r.data
      setSchema(s)
      const cols = (s?.columns || s || []).map(c => typeof c === 'string' ? c : c.column_name || c.name)
      const visible = cols.filter(c => !HIDDEN_SYS.has(c))
      if (visible.length > 0) setGroupCol(visible[0])
      setMetricCols([]); setData([]); setSummaryData([])
      setColFilters({}); setDateFrom(''); setDateTo('')
    }).catch(() => toast.error('Failed to load schema'))
  }, [sel])

  const colNames = useMemo(() => {
    const cols = schema?.columns || schema || []
    return cols.map(c => typeof c === 'string' ? c : c.column_name || c.name)
  }, [schema])

  const colMeta = useMemo(() => {
    const cols = schema?.columns || schema || []
    const map = {}
    cols.forEach(c => {
      const name = typeof c === 'string' ? c : c.column_name || c.name
      const dt = (typeof c === 'object' ? (c.data_type || '') : '').toLowerCase()
      map[name] = { isNum: ['int','bigint','smallint','tinyint','float','real','decimal','numeric','money'].includes(dt), dt }
    })
    return map
  }, [schema])

  const visCols = useMemo(() => colNames.filter(c => !HIDDEN_SYS.has(c) && c !== groupCol), [colNames, groupCol])
  const textCols = useMemo(() => colNames.filter(c => !HIDDEN_SYS.has(c)), [colNames])
  const hasDateCol = colNames.includes('REPORT_DATE')
  const activeFilterCount = Object.keys(colFilters).filter(k => colFilters[k]?.length).length + (dateFrom ? 1 : 0) + (dateTo ? 1 : 0)

  // Load distinct values for filter column
  const loadDistinct = useCallback(async (col) => {
    if (!col || !sel) return
    setFilterCol(col)
    setDistinctLoading(true)
    setFilterSearch('')
    try {
      const r = await trendsAPI.getDistinct(sel, col)
      setDistinctVals(r.data?.data?.values || [])
    } catch { toast.error('Failed to load values') }
    finally { setDistinctLoading(false) }
  }, [sel])

  const toggleFilterVal = (col, val) => {
    setColFilters(prev => {
      const cur = prev[col] || []
      const next = cur.includes(val) ? cur.filter(v => v !== val) : [...cur, val]
      return { ...prev, [col]: next }
    })
  }

  const clearFilter = (col) => {
    setColFilters(prev => { const n = { ...prev }; delete n[col]; return n })
  }

  const fetchData = async () => {
    if (!sel) return
    setLoading(true)
    try {
      const filters = {}
      Object.entries(colFilters).forEach(([col, vals]) => { if (vals.length) filters[col] = vals })
      const r = await trendsAPI.review({
        table_name: sel, limit, filters,
        ...(dateFrom && { date_from: dateFrom }),
        ...(dateTo && { date_to: dateTo }),
      })
      const o = r.data?.data || r.data || {}
      const rows = o?.data || o?.rows || (Array.isArray(o) ? o : [])
      setData(rows)
      if (groupCol && metricCols.length > 0) buildSummary(rows)
      if (viewMode === 'trend' && metricCols.length > 0) buildTrend(rows)
      toast.success(`${rows.length.toLocaleString()} rows loaded`)
    } catch { toast.error('Fetch failed') }
    finally { setLoading(false) }
  }

  const buildSummary = (rows) => {
    if (!groupCol || !metricCols.length) { setSummaryData([]); return }
    const grouped = {}
    rows.forEach(row => {
      const key = String(row[groupCol] ?? 'Unknown')
      if (!grouped[key]) grouped[key] = { [groupCol]: key, _count: 0, _vals: {} }
      grouped[key]._count++
      metricCols.forEach(mc => {
        const val = Number(row[mc])
        if (!isNaN(val)) {
          if (!grouped[key]._vals[mc]) grouped[key]._vals[mc] = []
          grouped[key]._vals[mc].push(val)
        }
      })
    })

    const summary = Object.values(grouped).map(g => {
      const row = { [groupCol]: g[groupCol] }
      metricCols.forEach(mc => {
        const vals = g._vals[mc] || [0]
        const sum = vals.reduce((a, b) => a + b, 0)
        switch (aggFunc) {
          case 'SUM':   row[mc] = Math.round(sum * 100) / 100; break
          case 'AVG':   row[mc] = Math.round((sum / vals.length) * 100) / 100; break
          case 'COUNT': row[mc] = vals.length; break
          case 'MAX':   row[mc] = Math.round(Math.max(...vals) * 100) / 100; break
          case 'MIN':   row[mc] = Math.round(Math.min(...vals) * 100) / 100; break
          default:      row[mc] = Math.round(sum * 100) / 100
        }
      })
      return row
    })

    if (metricCols[0]) {
      summary.sort((a, b) => sortDir === 'desc' ? (b[metricCols[0]] || 0) - (a[metricCols[0]] || 0) : (a[metricCols[0]] || 0) - (b[metricCols[0]] || 0))
    }
    setSummaryData(summary)
  }

  // Build trend data: group by date grain, aggregate metrics
  const buildTrend = (rows) => {
    if (!metricCols.length || !hasDateCol) { setTrendData([]); return }

    const getDateKey = (dateStr) => {
      if (!dateStr) return 'Unknown'
      const d = new Date(dateStr)
      if (isNaN(d)) return String(dateStr)
      const yyyy = d.getFullYear(), mm = String(d.getMonth() + 1).padStart(2, '0'), dd = String(d.getDate()).padStart(2, '0')
      switch (dateGrain) {
        case 'day':     return `${yyyy}-${mm}-${dd}`
        case 'week': {
          const jan1 = new Date(yyyy, 0, 1)
          const wk = Math.ceil(((d - jan1) / 86400000 + jan1.getDay() + 1) / 7)
          return `${yyyy}-W${String(wk).padStart(2, '0')}`
        }
        case 'month':   return `${yyyy}-${mm}`
        case 'quarter': return `${yyyy}-Q${Math.ceil((d.getMonth() + 1) / 3)}`
        case 'year':    return `${yyyy}`
        default:        return `${yyyy}-${mm}-${dd}`
      }
    }

    const grouped = {}
    rows.forEach(row => {
      const dateKey = getDateKey(row.REPORT_DATE)
      // If breakdown column is selected, create composite key
      const breakKey = trendBreakdown ? String(row[trendBreakdown] ?? 'Unknown') : null

      if (!grouped[dateKey]) grouped[dateKey] = { _date: dateKey, _vals: {} }

      metricCols.forEach(mc => {
        const val = Number(row[mc])
        if (isNaN(val)) return
        if (breakKey) {
          const compKey = `${mc}__${breakKey}`
          if (!grouped[dateKey]._vals[compKey]) grouped[dateKey]._vals[compKey] = []
          grouped[dateKey]._vals[compKey].push(val)
        } else {
          if (!grouped[dateKey]._vals[mc]) grouped[dateKey]._vals[mc] = []
          grouped[dateKey]._vals[mc].push(val)
        }
      })
    })

    const applyAgg = (vals) => {
      if (!vals || !vals.length) return 0
      const sum = vals.reduce((a, b) => a + b, 0)
      switch (aggFunc) {
        case 'SUM': return Math.round(sum * 100) / 100
        case 'AVG': return Math.round((sum / vals.length) * 100) / 100
        case 'COUNT': return vals.length
        case 'MAX': return Math.round(Math.max(...vals) * 100) / 100
        case 'MIN': return Math.round(Math.min(...vals) * 100) / 100
        default: return Math.round(sum * 100) / 100
      }
    }

    const trend = Object.values(grouped).map(g => {
      const row = { date: g._date }
      Object.entries(g._vals).forEach(([key, vals]) => {
        row[key] = applyAgg(vals)
      })
      return row
    })
    trend.sort((a, b) => a.date.localeCompare(b.date))
    setTrendData(trend)
  }

  useEffect(() => { if (data.length) buildSummary(data) }, [groupCol, metricCols, aggFunc, sortDir])
  useEffect(() => { if (data.length && viewMode === 'trend') buildTrend(data) }, [metricCols, aggFunc, dateGrain, trendBreakdown, viewMode])

  const toggleMetric = (col) => {
    setMetricCols(prev => prev.includes(col) ? prev.filter(c => c !== col) : prev.length < 5 ? [...prev, col] : prev)
  }

  // Stats cards
  const stats = useMemo(() => {
    if (!metricCols.length || !summaryData.length) return []
    return metricCols.map(mc => {
      const vals = summaryData.map(r => r[mc] || 0)
      const total = vals.reduce((a, b) => a + b, 0)
      const avg = total / vals.length
      return {
        col: mc, total: Math.round(total * 100) / 100,
        avg: Math.round(avg * 100) / 100,
        max: Math.round(Math.max(...vals) * 100) / 100,
        min: Math.round(Math.min(...vals) * 100) / 100,
        groups: vals.length
      }
    })
  }, [summaryData, metricCols])

  const chartData = summaryData.slice(0, topN)

  // Trend line keys (metric or metric__breakdownValue)
  const trendKeys = useMemo(() => {
    if (!trendData.length) return []
    const keys = new Set()
    trendData.forEach(row => Object.keys(row).forEach(k => { if (k !== 'date') keys.add(k) }))
    return [...keys]
  }, [trendData])

  // Pie data for first metric
  const pieData = useMemo(() => {
    if (!metricCols[0] || !chartData.length) return []
    const top = chartData.slice(0, 10)
    const rest = chartData.slice(10).reduce((s, r) => s + (r[metricCols[0]] || 0), 0)
    const items = top.map(r => ({ name: r[groupCol], value: r[metricCols[0]] || 0 }))
    if (rest > 0) items.push({ name: 'Others', value: Math.round(rest * 100) / 100 })
    return items
  }, [chartData, metricCols, groupCol])

  const exportCSV = () => {
    if (!summaryData.length) return
    const cols = [groupCol, ...metricCols]
    const csv = [cols.join(','), ...summaryData.map(r => cols.map(c => `"${r[c] ?? ''}"`).join(','))].join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = `${sel}_summary.csv`; a.click()
  }

  const inp = { height: 22, fontSize: 9, padding: '0 5px', borderRadius: 3, border: '1px solid #e2e8f0', outline: 'none', background: '#fff' }
  const btnSm = { height: 20, padding: '0 6px', fontSize: 8, fontWeight: 600, borderRadius: 3, cursor: 'pointer', border: '1px solid #e2e8f0' }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {/* Header + Controls */}
      <div style={{ background: '#fff', borderRadius: 6, border: '1px solid #e2e8f0', padding: '6px 10px',
        display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginRight: 6 }}>
          <div style={{ width: 24, height: 24, borderRadius: 5, background: 'linear-gradient(135deg,#4f46e5,#7c3aed)',
            display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <BarChart3 size={12} style={{ color: '#fff' }}/>
          </div>
          <span style={{ fontSize: 12, fontWeight: 700, color: '#0f172a' }}>Trend Dashboard</span>
        </div>

        <select value={sel} onChange={e => setSel(e.target.value)} style={{ ...inp, flex: '1 1 130px', minWidth: 100, cursor: 'pointer' }}>
          <option value="">Select table...</option>
          {tables.map(t => { const n = t.table_name || t, rc = t.row_count
            return <option key={n} value={n}>{n}{rc != null ? ` (${Number(rc).toLocaleString()})` : ''}</option> })}
        </select>

        {colNames.length > 0 && (
          <>
            {/* View mode toggle */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 2, background: '#f1f5f9', borderRadius: 4, padding: 2 }}>
              {['summary', 'trend'].map(m => (
                <button key={m} onClick={() => setViewMode(m)}
                  style={{ height: 18, padding: '0 8px', fontSize: 8, fontWeight: 700, borderRadius: 3, cursor: 'pointer',
                    background: viewMode === m ? '#4f46e5' : 'transparent', color: viewMode === m ? '#fff' : '#64748b',
                    border: 'none', display: 'flex', alignItems: 'center', gap: 3 }}>
                  {m === 'trend' ? <><TrendingUp size={8}/> Trend</> : <><BarChart3 size={8}/> Summary</>}
                </button>
              ))}
            </div>

            {viewMode === 'summary' && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                <span style={{ fontSize: 8, fontWeight: 700, color: '#64748b' }}>GROUP BY:</span>
                <select value={groupCol} onChange={e => setGroupCol(e.target.value)} style={{ ...inp, width: 100, cursor: 'pointer' }}>
                  {textCols.map(c => <option key={c} value={c}>{c}</option>)}
                </select>
              </div>
            )}

            {viewMode === 'trend' && (
              <>
                <div style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                  <span style={{ fontSize: 8, fontWeight: 700, color: '#64748b' }}>GRAIN:</span>
                  <select value={dateGrain} onChange={e => setDateGrain(e.target.value)} style={{ ...inp, width: 70, cursor: 'pointer' }}>
                    {DATE_GRAINS.map(g => <option key={g} value={g}>{g.charAt(0).toUpperCase() + g.slice(1)}</option>)}
                  </select>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                  <span style={{ fontSize: 8, fontWeight: 700, color: '#64748b' }}>SPLIT BY:</span>
                  <select value={trendBreakdown} onChange={e => setTrendBreakdown(e.target.value)} style={{ ...inp, width: 100, cursor: 'pointer' }}>
                    <option value="">None</option>
                    {textCols.filter(c => c !== 'REPORT_DATE').map(c => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
              </>
            )}

            <div style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
              <span style={{ fontSize: 8, fontWeight: 700, color: '#64748b' }}>AGG:</span>
              <select value={aggFunc} onChange={e => setAggFunc(e.target.value)} style={{ ...inp, width: 60, cursor: 'pointer' }}>
                {AGG_FUNCS.map(f => <option key={f} value={f}>{f}</option>)}
              </select>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
              <span style={{ fontSize: 8, fontWeight: 700, color: '#64748b' }}>CHART:</span>
              {CHART_TYPES.map(t => (
                <button key={t} onClick={() => setChartType(t)}
                  style={{ ...btnSm, background: chartType === t ? '#4f46e5' : '#fff', color: chartType === t ? '#fff' : '#64748b',
                    border: `1px solid ${chartType === t ? '#4f46e5' : '#e2e8f0'}` }}>
                  {t === 'stacked' ? 'Stack' : t === 'composed' ? 'Combo' : t.charAt(0).toUpperCase() + t.slice(1)}
                </button>
              ))}
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
              <span style={{ fontSize: 8, fontWeight: 700, color: '#64748b' }}>TOP:</span>
              <select value={topN} onChange={e => setTopN(Number(e.target.value))} style={{ ...inp, width: 50, cursor: 'pointer' }}>
                {[10, 20, 30, 50, 100, 200, 500].map(n => <option key={n} value={n}>{n}</option>)}
              </select>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
              <span style={{ fontSize: 8, fontWeight: 700, color: '#64748b' }}>SORT:</span>
              <button onClick={() => setSortDir(d => d === 'desc' ? 'asc' : 'desc')}
                style={{ ...btnSm, background: '#fff', color: '#475569', display: 'flex', alignItems: 'center', gap: 2 }}>
                {sortDir === 'desc' ? <ChevronDown size={9}/> : <ChevronUp size={9}/>} {sortDir.toUpperCase()}
              </button>
            </div>
          </>
        )}

        <button onClick={() => setShowFilters(f => !f)} disabled={!sel}
          style={{ ...btnSm, height: 22, background: showFilters ? '#eff6ff' : '#fff', color: showFilters ? '#4f46e5' : '#64748b',
            border: `1px solid ${showFilters ? '#4f46e5' : '#e2e8f0'}`, display: 'inline-flex', alignItems: 'center', gap: 3,
            position: 'relative' }}>
          <Filter size={9}/> Filters
          {activeFilterCount > 0 && (
            <span style={{ position: 'absolute', top: -5, right: -5, width: 14, height: 14, borderRadius: 7,
              background: '#ef4444', color: '#fff', fontSize: 7, fontWeight: 800,
              display: 'flex', alignItems: 'center', justifyContent: 'center' }}>{activeFilterCount}</span>
          )}
        </button>

        <button onClick={fetchData} disabled={loading || !sel}
          style={{ height: 22, padding: '0 10px', borderRadius: 3, fontSize: 9, fontWeight: 700, color: '#fff',
            background: loading || !sel ? '#94a3b8' : '#4f46e5', border: 'none',
            cursor: loading || !sel ? 'not-allowed' : 'pointer', display: 'inline-flex', alignItems: 'center', gap: 3 }}>
          {loading ? <Loader2 size={9} className="animate-spin"/> : <RefreshCw size={9}/>} Load
        </button>

        {summaryData.length > 0 && (
          <button onClick={exportCSV} style={{ ...btnSm, height: 22, background: '#fff', color: '#475569',
            display: 'inline-flex', alignItems: 'center', gap: 3 }}>
            <Download size={9}/> CSV
          </button>
        )}
      </div>

      {/* Filters Panel */}
      {showFilters && colNames.length > 0 && (
        <div style={{ background: '#fff', borderRadius: 6, border: '1px solid #e2e8f0', padding: '8px 10px' }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start', flexWrap: 'wrap' }}>
            {/* Date range */}
            {hasDateCol && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                <span style={{ fontSize: 8, fontWeight: 700, color: '#64748b' }}>DATE RANGE (REPORT_DATE):</span>
                <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                  <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)}
                    style={{ ...inp, width: 110, fontSize: 8 }} placeholder="From"/>
                  <span style={{ fontSize: 8, color: '#94a3b8' }}>to</span>
                  <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)}
                    style={{ ...inp, width: 110, fontSize: 8 }} placeholder="To"/>
                  {(dateFrom || dateTo) && (
                    <button onClick={() => { setDateFrom(''); setDateTo('') }}
                      style={{ ...btnSm, color: '#ef4444', background: '#fff', padding: '0 4px' }}><X size={8}/></button>
                  )}
                </div>
              </div>
            )}

            {/* Column filter selector */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 3, flex: 1 }}>
              <span style={{ fontSize: 8, fontWeight: 700, color: '#64748b' }}>FILTER BY COLUMN:</span>
              <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                <select value={filterCol} onChange={e => loadDistinct(e.target.value)}
                  style={{ ...inp, width: 130, cursor: 'pointer' }}>
                  <option value="">Pick column...</option>
                  {textCols.map(c => <option key={c} value={c}>{c}</option>)}
                </select>
                {distinctLoading && <Loader2 size={10} className="animate-spin" style={{ color: '#4f46e5' }}/>}
              </div>

              {/* Distinct value checkboxes */}
              {filterCol && distinctVals.length > 0 && (
                <div style={{ border: '1px solid #e2e8f0', borderRadius: 4, padding: 4, maxHeight: 120, overflowY: 'auto' }}>
                  <input value={filterSearch} onChange={e => setFilterSearch(e.target.value)} placeholder="Search..."
                    style={{ ...inp, width: '100%', marginBottom: 3, fontSize: 8 }}/>
                  <div style={{ display: 'flex', gap: 2, marginBottom: 3 }}>
                    <button onClick={() => {
                      const filtered = distinctVals.filter(v => !filterSearch || String(v).toLowerCase().includes(filterSearch.toLowerCase()))
                      setColFilters(prev => ({ ...prev, [filterCol]: filtered.map(String) }))
                    }} style={{ fontSize: 7, color: '#4f46e5', background: 'none', border: 'none', cursor: 'pointer', fontWeight: 600 }}>Select All</button>
                    <button onClick={() => clearFilter(filterCol)}
                      style={{ fontSize: 7, color: '#ef4444', background: 'none', border: 'none', cursor: 'pointer', fontWeight: 600 }}>Clear</button>
                  </div>
                  {distinctVals
                    .filter(v => !filterSearch || String(v).toLowerCase().includes(filterSearch.toLowerCase()))
                    .slice(0, 200).map(val => {
                      const checked = (colFilters[filterCol] || []).includes(String(val))
                      return (
                        <label key={val} style={{ display: 'flex', alignItems: 'center', gap: 3, fontSize: 8, cursor: 'pointer', padding: '1px 0' }}>
                          <input type="checkbox" checked={checked} onChange={() => toggleFilterVal(filterCol, String(val))}
                            style={{ width: 10, height: 10 }}/>
                          {String(val)}
                        </label>
                      )
                    })}
                </div>
              )}
            </div>

            {/* Row limit */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
              <span style={{ fontSize: 8, fontWeight: 700, color: '#64748b' }}>ROW LIMIT:</span>
              <select value={limit} onChange={e => setLimit(Number(e.target.value))} style={{ ...inp, width: 80, cursor: 'pointer' }}>
                {[1000, 5000, 10000, 50000, 100000].map(n => <option key={n} value={n}>{n.toLocaleString()}</option>)}
              </select>
            </div>
          </div>

          {/* Active filters badges */}
          {activeFilterCount > 0 && (
            <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 6, paddingTop: 6, borderTop: '1px solid #f1f5f9' }}>
              {dateFrom && (
                <span style={{ fontSize: 8, padding: '1px 6px', borderRadius: 10, background: '#eff6ff', color: '#4f46e5', border: '1px solid #bfdbfe',
                  display: 'inline-flex', alignItems: 'center', gap: 3 }}>
                  From: {dateFrom} <X size={7} style={{ cursor: 'pointer' }} onClick={() => setDateFrom('')}/>
                </span>
              )}
              {dateTo && (
                <span style={{ fontSize: 8, padding: '1px 6px', borderRadius: 10, background: '#eff6ff', color: '#4f46e5', border: '1px solid #bfdbfe',
                  display: 'inline-flex', alignItems: 'center', gap: 3 }}>
                  To: {dateTo} <X size={7} style={{ cursor: 'pointer' }} onClick={() => setDateTo('')}/>
                </span>
              )}
              {Object.entries(colFilters).filter(([, v]) => v.length).map(([col, vals]) => (
                <span key={col} style={{ fontSize: 8, padding: '1px 6px', borderRadius: 10, background: '#f0fdf4', color: '#15803d', border: '1px solid #bbf7d0',
                  display: 'inline-flex', alignItems: 'center', gap: 3 }}>
                  {col}: {vals.length} value{vals.length > 1 ? 's' : ''} <X size={7} style={{ cursor: 'pointer' }} onClick={() => clearFilter(col)}/>
                </span>
              ))}
              <button onClick={() => { setColFilters({}); setDateFrom(''); setDateTo('') }}
                style={{ fontSize: 7, color: '#ef4444', background: 'none', border: 'none', cursor: 'pointer', fontWeight: 700, textDecoration: 'underline' }}>
                Clear all
              </button>
            </div>
          )}
        </div>
      )}

      {/* Metric selector */}
      {visCols.length > 0 && (
        <div style={{ background: '#fff', borderRadius: 6, border: '1px solid #e2e8f0', padding: '4px 10px' }}>
          <span style={{ fontSize: 8, fontWeight: 700, color: '#64748b', marginRight: 6 }}>METRICS (select up to 5):</span>
          <div style={{ display: 'inline-flex', gap: 3, flexWrap: 'wrap' }}>
            {visCols.map(col => {
              const active = metricCols.includes(col)
              const colorIdx = metricCols.indexOf(col)
              return (
                <button key={col} onClick={() => toggleMetric(col)}
                  style={{ height: 18, padding: '0 6px', fontSize: 8, fontWeight: active ? 700 : 400, borderRadius: 3, cursor: 'pointer',
                    background: active ? COLORS[colorIdx] + '18' : '#fff',
                    color: active ? COLORS[colorIdx] : '#94a3b8',
                    border: `1px solid ${active ? COLORS[colorIdx] : '#e2e8f0'}` }}>
                  {active && <span style={{ display: 'inline-block', width: 6, height: 6, borderRadius: 3, background: COLORS[colorIdx], marginRight: 3 }}/>}
                  {col}
                </button>
              )
            })}
          </div>
        </div>
      )}

      {/* Stats cards */}
      {stats.length > 0 && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {stats.map((st, i) => (
            <div key={st.col} style={{ flex: '1 1 140px', background: '#fff', borderRadius: 6, border: '1px solid #e2e8f0', padding: '8px 10px' }}>
              <div style={{ fontSize: 8, fontWeight: 600, color: COLORS[i], textTransform: 'uppercase', marginBottom: 4, display: 'flex', alignItems: 'center', gap: 3 }}>
                <span style={{ width: 6, height: 6, borderRadius: 3, background: COLORS[i] }}/>
                {st.col} <span style={{ fontSize: 7, color: '#94a3b8', fontWeight: 400 }}>({aggFunc})</span>
              </div>
              <div style={{ fontSize: 16, fontWeight: 800, color: '#0f172a', lineHeight: 1 }}>{st.total.toLocaleString()}</div>
              <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
                <span style={{ fontSize: 8, color: '#64748b' }}>Avg: <b>{st.avg.toLocaleString()}</b></span>
                <span style={{ fontSize: 8, color: '#059669' }}>Max: <b>{st.max.toLocaleString()}</b></span>
                <span style={{ fontSize: 8, color: '#dc2626' }}>Min: <b>{st.min.toLocaleString()}</b></span>
                <span style={{ fontSize: 8, color: '#64748b' }}>Groups: <b>{st.groups}</b></span>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Trend Chart */}
      {viewMode === 'trend' && trendData.length > 0 && trendKeys.length > 0 && (
        <div style={{ background: '#fff', borderRadius: 6, border: '1px solid #e2e8f0', padding: '10px' }}>
          <div style={{ fontSize: 9, fontWeight: 700, color: '#475569', marginBottom: 6, display: 'flex', alignItems: 'center', gap: 4 }}>
            <TrendingUp size={10}/> {aggFunc}({metricCols.join(', ')}) over time ({dateGrain})
            {trendBreakdown && <span style={{ fontSize: 8, color: '#94a3b8' }}>split by {trendBreakdown}</span>}
            <span style={{ fontSize: 8, fontWeight: 400, color: '#94a3b8' }}>({trendData.length} periods)</span>
          </div>
          <ResponsiveContainer width="100%" height={350}>
            <ComposedChart data={trendData} margin={{ top: 5, right: 10, left: 10, bottom: 50 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
              <XAxis dataKey="date" tick={{ fontSize: 7 }} angle={-35} textAnchor="end" height={55} interval={Math.max(0, Math.floor(trendData.length / 30))}/>
              <YAxis tick={{ fontSize: 8 }} tickFormatter={v => v >= 1000000 ? (v / 1000000).toFixed(1) + 'M' : v >= 1000 ? (v / 1000).toFixed(0) + 'K' : v}/>
              <Tooltip contentStyle={{ fontSize: 9, maxHeight: 200, overflowY: 'auto' }}
                formatter={v => typeof v === 'number' ? v.toLocaleString() : v}
                labelFormatter={l => `Date: ${l}`}/>
              <Legend wrapperStyle={{ fontSize: 8 }}/>
              {trendKeys.map((key, i) => {
                const label = key.includes('__') ? key.split('__').pop() : key
                return chartType === 'bar' || chartType === 'stacked'
                  ? <Bar key={key} dataKey={key} name={label} fill={COLORS[i % COLORS.length]}
                      stackId={chartType === 'stacked' ? 'a' : undefined} radius={chartType === 'stacked' ? undefined : [2, 2, 0, 0]}/>
                  : chartType === 'area'
                    ? <Area key={key} type="monotone" dataKey={key} name={label} fill={COLORS[i % COLORS.length] + '30'}
                        stroke={COLORS[i % COLORS.length]} strokeWidth={2}/>
                    : <Line key={key} type="monotone" dataKey={key} name={label} stroke={COLORS[i % COLORS.length]}
                        strokeWidth={2} dot={{ r: 1.5 }}/>
              })}
            </ComposedChart>
          </ResponsiveContainer>

          {/* Trend data table */}
          <div style={{ overflowX: 'auto', maxHeight: 250, marginTop: 8, borderTop: '1px solid #f1f5f9', paddingTop: 6 }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 8 }}>
              <thead>
                <tr style={{ background: '#f8fafc' }}>
                  <th style={{ padding: '3px 6px', textAlign: 'left', borderBottom: '1px solid #e2e8f0', color: '#475569', fontWeight: 700, position: 'sticky', top: 0, background: '#f8fafc' }}>Date</th>
                  {trendKeys.map((key, i) => {
                    const label = key.includes('__') ? key.split('__').pop() : key
                    return <th key={key} style={{ padding: '3px 6px', textAlign: 'right', borderBottom: '1px solid #e2e8f0', fontWeight: 700, position: 'sticky', top: 0, background: '#f8fafc', color: COLORS[i % COLORS.length] }}>{label}</th>
                  })}
                </tr>
              </thead>
              <tbody>
                {trendData.map((row, idx) => (
                  <tr key={idx} style={{ background: idx % 2 ? '#fafbfc' : '#fff' }}>
                    <td style={{ padding: '2px 6px', borderBottom: '1px solid #f1f5f9', fontWeight: 600, color: '#0f172a' }}>{row.date}</td>
                    {trendKeys.map(key => (
                      <td key={key} style={{ padding: '2px 6px', textAlign: 'right', borderBottom: '1px solid #f1f5f9', color: '#334155', fontFamily: 'monospace' }}>
                        {typeof row[key] === 'number' ? row[key].toLocaleString(undefined, { maximumFractionDigits: 2 }) : '—'}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Summary Chart */}
      {viewMode === 'summary' && chartData.length > 0 && metricCols.length > 0 && (
        <div style={{ background: '#fff', borderRadius: 6, border: '1px solid #e2e8f0', padding: '10px' }}>
          <div style={{ fontSize: 9, fontWeight: 700, color: '#475569', marginBottom: 6, display: 'flex', alignItems: 'center', gap: 4 }}>
            <LineChartIcon size={10}/> {aggFunc}({metricCols.join(', ')}) by {groupCol}
            <span style={{ fontSize: 8, fontWeight: 400, color: '#94a3b8' }}>
              (top {Math.min(topN, summaryData.length)} of {summaryData.length} groups)
            </span>
          </div>
          <ResponsiveContainer width="100%" height={320}>
            {chartType === 'pie' ? (
              <PieChart>
                <Pie data={pieData} cx="50%" cy="50%" outerRadius={120} dataKey="value" nameKey="name"
                  label={({ name, percent }) => `${name} ${(percent * 100).toFixed(0)}%`} labelLine={false}
                  style={{ fontSize: 8 }}>
                  {pieData.map((_, i) => <Cell key={i} fill={COLORS[i % COLORS.length]}/>)}
                </Pie>
                <Tooltip contentStyle={{ fontSize: 10 }} formatter={v => typeof v === 'number' ? v.toLocaleString() : v}/>
                <Legend wrapperStyle={{ fontSize: 9 }}/>
              </PieChart>
            ) : chartType === 'stacked' ? (
              <BarChart data={chartData} margin={{ top: 5, right: 10, left: 10, bottom: 40 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                <XAxis dataKey={groupCol} tick={{ fontSize: 7 }} angle={-35} textAnchor="end" height={50} interval={0}/>
                <YAxis tick={{ fontSize: 8 }} tickFormatter={v => v >= 1000 ? (v / 1000).toFixed(0) + 'K' : v}/>
                <Tooltip contentStyle={{ fontSize: 10 }} formatter={v => typeof v === 'number' ? v.toLocaleString() : v}/>
                <Legend wrapperStyle={{ fontSize: 9 }}/>
                {metricCols.map((mc, i) => <Bar key={mc} dataKey={mc} stackId="a" fill={COLORS[i]}/>)}
              </BarChart>
            ) : chartType === 'composed' ? (
              <ComposedChart data={chartData} margin={{ top: 5, right: 10, left: 10, bottom: 40 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                <XAxis dataKey={groupCol} tick={{ fontSize: 7 }} angle={-35} textAnchor="end" height={50} interval={0}/>
                <YAxis tick={{ fontSize: 8 }} tickFormatter={v => v >= 1000 ? (v / 1000).toFixed(0) + 'K' : v}/>
                <Tooltip contentStyle={{ fontSize: 10 }} formatter={v => typeof v === 'number' ? v.toLocaleString() : v}/>
                <Legend wrapperStyle={{ fontSize: 9 }}/>
                {metricCols.map((mc, i) => i === 0
                  ? <Bar key={mc} dataKey={mc} fill={COLORS[i]} radius={[2, 2, 0, 0]}/>
                  : <Line key={mc} type="monotone" dataKey={mc} stroke={COLORS[i]} strokeWidth={2} dot={{ r: 2 }}/>
                )}
              </ComposedChart>
            ) : chartType === 'bar' ? (
              <BarChart data={chartData} margin={{ top: 5, right: 10, left: 10, bottom: 40 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                <XAxis dataKey={groupCol} tick={{ fontSize: 7 }} angle={-35} textAnchor="end" height={50} interval={0}/>
                <YAxis tick={{ fontSize: 8 }} tickFormatter={v => v >= 1000 ? (v / 1000).toFixed(0) + 'K' : v}/>
                <Tooltip contentStyle={{ fontSize: 10 }} formatter={v => typeof v === 'number' ? v.toLocaleString() : v}/>
                <Legend wrapperStyle={{ fontSize: 9 }}/>
                {metricCols.map((mc, i) => <Bar key={mc} dataKey={mc} fill={COLORS[i]} radius={[2, 2, 0, 0]}/>)}
              </BarChart>
            ) : chartType === 'area' ? (
              <AreaChart data={chartData} margin={{ top: 5, right: 10, left: 10, bottom: 40 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                <XAxis dataKey={groupCol} tick={{ fontSize: 7 }} angle={-35} textAnchor="end" height={50} interval={0}/>
                <YAxis tick={{ fontSize: 8 }} tickFormatter={v => v >= 1000 ? (v / 1000).toFixed(0) + 'K' : v}/>
                <Tooltip contentStyle={{ fontSize: 10 }} formatter={v => typeof v === 'number' ? v.toLocaleString() : v}/>
                <Legend wrapperStyle={{ fontSize: 9 }}/>
                {metricCols.map((mc, i) => <Area key={mc} type="monotone" dataKey={mc} fill={COLORS[i] + '30'} stroke={COLORS[i]} strokeWidth={2}/>)}
              </AreaChart>
            ) : (
              <LineChart data={chartData} margin={{ top: 5, right: 10, left: 10, bottom: 40 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                <XAxis dataKey={groupCol} tick={{ fontSize: 7 }} angle={-35} textAnchor="end" height={50} interval={0}/>
                <YAxis tick={{ fontSize: 8 }} tickFormatter={v => v >= 1000 ? (v / 1000).toFixed(0) + 'K' : v}/>
                <Tooltip contentStyle={{ fontSize: 10 }} formatter={v => typeof v === 'number' ? v.toLocaleString() : v}/>
                <Legend wrapperStyle={{ fontSize: 9 }}/>
                {metricCols.map((mc, i) => <Line key={mc} type="monotone" dataKey={mc} stroke={COLORS[i]} strokeWidth={2} dot={{ r: 2 }}/>)}
              </LineChart>
            )}
          </ResponsiveContainer>
        </div>
      )}

      {/* Summary table */}
      {viewMode === 'summary' && summaryData.length > 0 && (
        <div style={{ background: '#fff', borderRadius: 6, border: '1px solid #e2e8f0', overflow: 'hidden' }}>
          <div style={{ padding: '4px 10px', background: '#f8fafc', borderBottom: '1px solid #e2e8f0', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              <Table2 size={9} style={{ color: '#64748b' }}/>
              <span style={{ fontSize: 9, fontWeight: 700, color: '#475569' }}>Summary Table ({summaryData.length} groups)</span>
            </div>
            <span style={{ fontSize: 8, color: '#94a3b8' }}>{aggFunc} · Sorted {sortDir.toUpperCase()} by {metricCols[0]}</span>
          </div>
          <div style={{ overflowX: 'auto', maxHeight: 350 }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 9 }}>
              <thead>
                <tr style={{ background: '#f8fafc' }}>
                  <th style={{ padding: '4px 8px', textAlign: 'center', borderBottom: '1px solid #e2e8f0', color: '#94a3b8', fontWeight: 600, position: 'sticky', top: 0, background: '#f8fafc', width: 30 }}>#</th>
                  <th style={{ padding: '4px 8px', textAlign: 'left', borderBottom: '1px solid #e2e8f0', color: '#475569', fontWeight: 700, position: 'sticky', top: 0, background: '#f8fafc' }}>{groupCol}</th>
                  {metricCols.map((mc, i) => (
                    <th key={mc} style={{ padding: '4px 8px', textAlign: 'right', borderBottom: '1px solid #e2e8f0', fontWeight: 700, position: 'sticky', top: 0, background: '#f8fafc', color: COLORS[i] }}>{mc}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {summaryData.map((row, idx) => (
                  <tr key={idx} style={{ background: idx % 2 ? '#fafbfc' : '#fff' }}>
                    <td style={{ padding: '3px 8px', textAlign: 'center', borderBottom: '1px solid #f1f5f9', color: '#94a3b8', fontSize: 8 }}>{idx + 1}</td>
                    <td style={{ padding: '3px 8px', borderBottom: '1px solid #f1f5f9', fontWeight: 500, color: '#0f172a' }}>{row[groupCol]}</td>
                    {metricCols.map(mc => (
                      <td key={mc} style={{ padding: '3px 8px', textAlign: 'right', borderBottom: '1px solid #f1f5f9', color: '#334155', fontFamily: 'monospace' }}>
                        {typeof row[mc] === 'number' ? row[mc].toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : row[mc] ?? '—'}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Empty state */}
      {!data.length && sel && !loading && (
        <div style={{ background: '#fff', borderRadius: 6, border: '1px solid #e2e8f0', padding: '30px', textAlign: 'center' }}>
          {viewMode === 'trend' ? (
            <>
              <TrendingUp size={24} style={{ color: '#c7d2fe', margin: '0 auto 8px' }}/>
              <div style={{ fontSize: 11, fontWeight: 600, color: '#475569' }}>Trend Analysis</div>
              <div style={{ fontSize: 9, color: '#94a3b8', marginTop: 2 }}>
                Select metrics, set date range in <b>Filters</b>, choose grain (Day/Week/Month), then click <b>Load</b>
              </div>
              {!hasDateCol && (
                <div style={{ fontSize: 9, color: '#ef4444', marginTop: 6, fontWeight: 600 }}>
                  This table has no REPORT_DATE column — trend view requires date data
                </div>
              )}
            </>
          ) : (
            <>
              <BarChart3 size={24} style={{ color: '#c7d2fe', margin: '0 auto 8px' }}/>
              <div style={{ fontSize: 11, fontWeight: 600, color: '#475569' }}>Ready to query</div>
              <div style={{ fontSize: 9, color: '#94a3b8', marginTop: 2 }}>Select metrics and click <b>Load</b></div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
