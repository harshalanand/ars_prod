import { useEffect, useMemo, useRef, useState } from 'react'
import { Play, RefreshCw, Store, Boxes, PackageOpen, Warehouse, ArrowUp, ArrowDown, Eye, EyeOff, Layers, Filter, X, Search, Download } from 'lucide-react'
import toast from 'react-hot-toast'
import { faConsAPI } from '@/services/api'
import ColumnFilterPopover, { passColumn, filterActive, matchGlobal, exportCsv } from '@/components/facons/ColumnFilter'

const STREAMS = [
  { key: 'ALL', label: 'All', icon: Layers },
  { key: 'FA', label: 'FA', icon: Store },
  { key: 'CONS', label: 'CONS', icon: Boxes },
]
const SCOPES = [
  { key: 'STORE', label: 'Store stock', icon: PackageOpen },
  { key: 'MSA', label: 'MSA / DC pool', icon: Warehouse },
]
const GRAINS = [
  { key: 'ref', label: 'Ref article', icon: Layers },
  { key: 'article', label: 'Actual article', icon: Boxes },
]
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
function fmtDate(iso) {
  if (!iso) return '—'
  const m = String(iso).slice(0, 10).match(/^(\d{4})-(\d{2})-(\d{2})$/)
  return m ? `${m[3]}-${MONTHS[+m[2] - 1]}-${m[1]}` : iso
}
const DIV_OPTIONS = ['FA', 'CO']
const streamDiv = (s) => (s === 'FA' ? ['FA'] : s === 'CONS' ? ['CO'] : ['FA', 'CO'])
const num = (v) => (v == null ? 0 : v)
// Full dataset is loaded so totals/filters are exact; render only this many rows for perf.
const RENDER_CAP = 1000

export default function FaConsStockPage() {
  const [stream, setStream] = useState('FA')
  const [scope, setScope] = useState('STORE')
  const [date, setDate] = useState('')
  const [divs, setDivs] = useState(streamDiv('FA'))
  const [running, setRunning] = useState(false)
  const [results, setResults] = useState(null)
  const [sort, setSort] = useState({ key: 'stk_ttl', dir: 'desc' })
  const [colq, setColq] = useState({})
  const [hidden, setHidden] = useState(new Set())   // hidden SLOC columns
  const [hover, setHover] = useState(null)          // { r, x, y } SLOC-wise breakup card
  const [popFilter, setPopFilter] = useState(null)  // { key, x, y } open filter popover
  const [gq, setGq] = useState('')                  // global search (comma = OR)
  const [syncing, setSyncing] = useState(false)     // auto SLOC sync in progress
  const [grain, setGrain] = useState('ref')         // 'ref' (clubbed) | 'article' (actual)

  const loadResults = async (s = stream, sc = scope, g = grain) => {
    try {
      const { data } = await faConsAPI.stockResults(s, sc, undefined, g)
      setResults(data.data)
    } catch (e) { toast.error(e.response?.data?.detail || 'Failed to load results') }
  }
  // Reset filters only when the DATASET changes (stream / scope) — NOT on a Ref⇄Actual
  // grain switch, so a filter you set to find one ref carries into its actual articles
  // (drill-down). colq entries for columns absent in the current grain are simply ignored.
  const prevDs = useRef({ stream, scope })
  useEffect(() => {
    if (prevDs.current.stream !== stream || prevDs.current.scope !== scope) {
      setGq(''); setColq({}); setPopFilter(null); setSort({ key: 'stk_ttl', dir: 'desc' })
      prevDs.current = { stream, scope }
    }
    loadResults(stream, scope, grain)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stream, scope, grain])

  // Auto-sync SLOCs whenever the page becomes active — discovers any new SLOCs in the
  // live stock so they can be activated on SLOC Settings. Silent; toasts only on change.
  useEffect(() => {
    let alive = true
    ;(async () => {
      setSyncing(true)
      try {
        const streams = stream === 'ALL' ? ['FA', 'CONS'] : [stream]
        const res = await Promise.all(streams.map(s => faConsAPI.syncSloc(s).catch(() => null)))
        if (!alive) return
        const added = res.reduce((n, r) => n + Number(r?.data?.data?.added ?? r?.data?.added ?? 0), 0)
        if (added > 0) toast(`${added} new SLOC${added > 1 ? 's' : ''} discovered — review on SLOC Settings`, { icon: '🔄' })
      } catch { /* silent */ } finally { if (alive) setSyncing(false) }
    })()
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stream])

  const toggleDiv = (d) => setDivs(prev => prev.includes(d) ? prev.filter(x => x !== d) : [...prev, d])
  const toggleHide = (sl) => setHidden(prev => { const n = new Set(prev); n.has(sl) ? n.delete(sl) : n.add(sl); return n })
  const toggleSort = (key) => setSort(s => s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: key === 'stk_ttl' ? 'desc' : 'asc' })

  const onCalc = async () => {
    if (!divs.length) { toast.error('Select at least one DIV (FA / CO)'); return }
    setRunning(true)
    try {
      const { data } = await faConsAPI.stockCalc(stream, date || undefined, divs)
      toast.success(data.message || 'Calculated')
      ;(data.data?.warnings || []).forEach(w => toast(w, { icon: '⚠️' }))
      loadResults(stream, scope)
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Calculation failed')
    } finally { setRunning(false) }
  }

  const slocCols = results?.columns || []
  // Hide all SLOC columns BY DEFAULT — reveal them via "Show SLOC cols" / per-chip.
  // Re-applies whenever the SLOC set changes (new calc / scope / stream switch).
  const slocKey = slocCols.join('|')
  useEffect(() => {
    if (slocCols.length) setHidden(new Set(slocCols))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slocKey])

  const cols = useMemo(() => {
    const streamCol = stream === 'ALL' ? [{ key: 'stream', label: 'Stream' }] : []
    // Product identity differs by grain: REF view clubs articles (shows Ref + #Art),
    // ARTICLE view drills to the actual article (shows Article + CLR + SZ).
    const prodCore = grain === 'article'
      ? [
        { key: 'maj_cat', label: 'MAJ_CAT' },
        { key: 'ref_art', label: 'Ref Article', mono: true },
        { key: 'article_number', label: 'Article', mono: true },
        { key: 'ref_art_desc', label: 'Description' },
        { key: 'clr', label: 'CLR' },
        { key: 'sz', label: 'SZ' },
      ]
      : [
        { key: 'maj_cat', label: 'MAJ_CAT' },
        { key: 'ref_art', label: 'Ref Article', mono: true },
        { key: 'ref_art_desc', label: 'Description' },
        { key: 'articles', label: '#Art', num: true },
      ]
    // MSA / DC pool is warehouse-level → product attributes (SEG/DIV/SUB_DIV).
    // Store scope keeps store attributes (name/hub/status/op-date).
    const fixed = scope === 'MSA'
      ? [...streamCol, { key: 'loc', label: 'RDC', mono: true },
        { key: 'seg', label: 'SEG' }, { key: 'div', label: 'DIV' }, { key: 'sub_div', label: 'SUB_DIV' },
        ...prodCore]
      : [...streamCol, { key: 'loc', label: 'Store', mono: true },
        { key: 'site_name', label: 'Store Name' }, { key: 'rdc', label: 'RDC' }, { key: 'hub', label: 'HUB' },
        { key: 'st_status', label: 'Status' }, { key: 'op_dt', label: 'OP Date', fmt: fmtDate },
        ...prodCore]
    const sloc = slocCols.filter(s => !hidden.has(s)).map(s => ({ key: `sloc:${s}`, label: s, sloc: s, num: true }))
    return [...fixed, ...sloc, { key: 'stk_ttl', label: 'STK_TTL', num: true, ttl: true }]
  }, [slocCols, hidden, scope, stream, grain])

  const raw = (r, c) => c.sloc ? num(r.slocs?.[c.sloc]) : c.key === 'stk_ttl' ? num(r.stk_ttl) : r[c.key]
  const disp = (r, c) => c.fmt ? c.fmt(raw(r, c)) : c.num ? Math.round(num(raw(r, c))).toLocaleString() : (raw(r, c) ?? '—')
  const cellStr = (r, c) => String(c.num ? num(raw(r, c)) : (raw(r, c) ?? ''))

  // distinct values per visible column — feeds the faceted header filter
  const distinct = useMemo(() => {
    const items = results?.items || []
    const m = {}
    cols.forEach(c => {
      const set = new Set()
      items.forEach(r => set.add(cellStr(r, c)))
      m[c.key] = [...set].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
    })
    return m
  }, [results, cols]) // eslint-disable-line

  const view = useMemo(() => {
    let arr = results?.items || []
    arr = arr.filter(r => cols.every(c => passColumn(colq[c.key], cellStr(r, c))))
    if (gq.trim()) arr = arr.filter(r => matchGlobal(gq, cols.map(c => cellStr(r, c))))
    const c = cols.find(x => x.key === sort.key)
    if (c) arr = [...arr].sort((a, b) => {
      if (c.num) return sort.dir === 'asc' ? num(raw(a, c)) - num(raw(b, c)) : num(raw(b, c)) - num(raw(a, c))
      const va = String(raw(a, c) ?? ''), vb = String(raw(b, c) ?? '')
      return sort.dir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va)
    })
    return arr
  }, [results, cols, colq, sort, gq]) // eslint-disable-line

  // Live aggregates over the FILTERED view — cards + per-SLOC qty react to the filter.
  const agg = useMemo(() => {
    let ttl = 0
    const perSloc = {}
    slocCols.forEach(s => { perSloc[s] = 0 })
    view.forEach(r => {
      ttl += num(r.stk_ttl)
      Object.entries(r.slocs || {}).forEach(([s, q]) => { perSloc[s] = (perSloc[s] || 0) + num(q) })
    })
    return { rows: view.length, ttl, perSloc }
  }, [view, slocKey]) // eslint-disable-line

  const filtered = view.length !== (results?.items?.length ?? 0)

  return (
    <div className="p-4 space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="page-title">FA &amp; CONS · Stock &amp; MSA</h1>
          <p className="text-[11px] text-gray-500 mt-0.5">
            SLOC-wise stock — one column per active SLOC + a <b>STK_TTL</b> total. Sort &amp; filter any column; hide SLOCs you don't need. DIV = {divs.join('/') || '—'}.
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm">
            {STREAMS.map(s => (
              <button key={s.key} onClick={() => { setStream(s.key); setDivs(streamDiv(s.key)) }}
                className={`flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-semibold transition-colors ${
                  stream === s.key ? 'bg-primary-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
                <s.icon size={13} /> {s.label}
              </button>
            ))}
          </div>
          <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm">
            {SCOPES.map(sc => (
              <button key={sc.key} onClick={() => setScope(sc.key)}
                className={`flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-semibold transition-colors ${
                  scope === sc.key ? 'bg-gray-800 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
                <sc.icon size={13} /> {sc.label}
              </button>
            ))}
          </div>
          <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm" title="Ref view clubs articles under their reference article; Actual view drills to each article">
            {GRAINS.map(g => (
              <button key={g.key} onClick={() => setGrain(g.key)}
                className={`flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-semibold transition-colors ${
                  grain === g.key ? 'bg-indigo-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
                <g.icon size={13} /> {g.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <label className="text-[11px] text-gray-500 flex items-center gap-1.5">Stock date
          <input type="date" value={date} onChange={e => setDate(e.target.value)} className="input !w-40 !py-0.5" />
        </label>
        <div className="flex items-center gap-1.5 pl-2 ml-1 border-l border-gray-200">
          <span className="text-[11px] text-gray-500">DIV</span>
          {DIV_OPTIONS.map(d => (
            <button key={d} onClick={() => toggleDiv(d)}
              className={`px-2 py-0.5 rounded-full text-[10px] font-semibold border ${
                divs.includes(d) ? 'bg-primary-600 text-white border-primary-600' : 'bg-white text-gray-500 border-gray-300'}`}>{d}</button>
          ))}
        </div>
        <button onClick={onCalc} disabled={running} className="btn-primary btn-sm flex items-center gap-1.5">
          <Play size={14} className={running ? 'animate-pulse' : ''} /> {running ? 'Calculating…' : `Calculate ${stream}`}
        </button>
        <button onClick={() => loadResults()} className="btn-secondary btn-sm flex items-center gap-1.5"><RefreshCw size={14} /> Refresh</button>
      </div>

      {results && (
        <div className="flex flex-wrap items-center gap-2">
          <div className={`inline-flex items-center gap-2 rounded-lg border px-3 py-1 ${filtered ? 'border-amber-300 bg-amber-50' : 'border-gray-200 bg-white'}`}>
            <span className="text-[9px] font-semibold uppercase tracking-wide text-gray-400">{stream} · Rows{filtered ? ' (filtered)' : ''}</span>
            <span className="text-[13px] font-bold text-gray-900 tabular-nums">{Number(filtered ? agg.rows : results.row_count).toLocaleString()}</span>
            {filtered && <span className="text-[10px] text-gray-500">of {Number(results.row_count).toLocaleString()}</span>}
          </div>
          <div className={`inline-flex items-center gap-2 rounded-lg border px-3 py-1 ${filtered ? 'border-amber-300 bg-amber-50' : 'border-gray-200 bg-white'}`}>
            <span className="text-[9px] font-semibold uppercase tracking-wide text-gray-400">STK_TTL · {scope}{filtered ? ' (filtered)' : ''}</span>
            <span className="text-[13px] font-bold text-primary-700 tabular-nums">{Math.round(filtered ? agg.ttl : num(results.total_qty)).toLocaleString()}</span>
            {filtered && <span className="text-[10px] text-gray-500">of {Math.round(num(results.total_qty)).toLocaleString()}</span>}
          </div>
          {syncing && (
            <div className="inline-flex items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-1 text-[10px] text-gray-500">
              <RefreshCw size={12} className="animate-spin" /> Syncing SLOCs…
            </div>
          )}
          {results.calc_date && (
            <div className="inline-flex items-center gap-2 rounded-lg border px-3 py-1" style={{ background: '#eff6ff', borderColor: '#bfdbfe' }} title="The stock snapshot date, and when this calc was run">
              <span className="text-[9px] font-semibold uppercase tracking-wide" style={{ color: '#2563eb' }}>Stock date</span>
              <span className="text-[13px] font-bold" style={{ color: '#1d4ed8' }}>{fmtDate(results.calc_date)}</span>
              {results.calculated_at && <span className="text-[10px] text-gray-500">· calculated {fmtDate(results.calculated_at)} {String(results.calculated_at).slice(11, 16)}</span>}
            </div>
          )}
        </div>
      )}

      {/* Search + SLOC hide/show — one line */}
      {results && (
        <div className="flex flex-wrap items-center gap-1.5 text-[10px]">
          <div className="relative">
            <Search size={12} style={{ position: 'absolute', left: 7, top: 6, color: '#9ca3af' }} />
            <input value={gq} onChange={e => setGq(e.target.value)} placeholder="Search all columns (a, b, c = any)…"
              className="input !py-0.5 !text-[10px]" style={{ paddingLeft: 24, width: 220 }} />
            {gq && <button onClick={() => setGq('')} style={{ position: 'absolute', right: 5, top: 5, background: 'none', border: 'none', cursor: 'pointer' }}><X size={11} color="#9ca3af" /></button>}
          </div>
          {Object.values(colq).some(filterActive) && (
            <button onClick={() => setColq({})} className="btn-secondary btn-sm flex items-center gap-1 !py-0.5"><X size={12} /> Clear</button>
          )}
          <button onClick={() => {
            const fixedCols = cols.filter(c => !c.sloc && !c.ttl)
            const allSloc = slocCols.map(s => ({ key: `sloc:${s}`, label: s, sloc: s, num: true }))
            const expCols = [...fixedCols, ...allSloc, { key: 'stk_ttl', label: 'STK_TTL', num: true, ttl: true }]
            exportCsv(`facons_stock_${stream}_${scope}`, expCols, view,
              (c, r) => c.sloc ? (r.slocs?.[c.sloc] ?? 0) : c.ttl ? (r.stk_ttl ?? 0) : c.fmt ? c.fmt(r[c.key]) : (r[c.key] ?? ''))
          }} disabled={!view.length} className="btn-secondary btn-sm flex items-center gap-1 !py-0.5"><Download size={12} /> Export</button>
          <span className="text-gray-400">{view.length}/{results.items?.length ?? 0}</span>
          {slocCols.length > 0 && <>
            <span className="text-gray-300">|</span>
            <span className="text-[9px] font-semibold uppercase tracking-wide text-gray-400">SLOC-wise qty</span>
            <button onClick={() => setHidden(hidden.size === slocCols.length ? new Set() : new Set(slocCols))}
              className="btn-secondary btn-sm flex items-center gap-1 !py-0.5">
              {hidden.size === slocCols.length ? <><Eye size={12} /> Show SLOC</> : <><EyeOff size={12} /> Hide SLOC</>}
            </button>
            {slocCols.map(s => {
              const q = Math.round(num(agg.perSloc[s]))
              const empty = q === 0
              return (
                <button key={s} onClick={() => toggleHide(s)}
                  title={`${s}: ${q.toLocaleString()} in view — click to ${hidden.has(s) ? 'show' : 'hide'} column`}
                  className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full font-mono font-semibold ${
                    hidden.has(s) ? 'bg-gray-100 text-gray-400 line-through'
                      : empty ? 'bg-gray-50 text-gray-400 border border-gray-200'
                        : 'bg-primary-50 text-primary-700'}`}>
                  {hidden.has(s) ? <EyeOff size={10} /> : <Eye size={10} />}{s}
                  <span className={`tabular-nums ${hidden.has(s) ? '' : empty ? 'text-gray-400' : 'text-primary-900'}`}>· {q.toLocaleString()}</span>
                </button>
              )
            })}
          </>}
        </div>
      )}

      <div className="table-container" style={{ maxHeight: 'calc(100vh - 250px)', overflow: 'auto' }}>
        <table className="w-full text-[10px]">
          <thead className="border-b-2 border-gray-200">
            <tr>
              {cols.map(c => {
                const active = filterActive(colq[c.key])
                return (
                  <th key={c.key} style={{ position: 'sticky', top: 0, zIndex: 20, background: c.ttl ? '#dbeafe' : '#f1f5f9' }}
                    className={`px-2 py-1 !text-[10px] font-semibold text-gray-700 whitespace-nowrap ${c.num ? 'text-right' : 'text-left'}`}>
                    <div className={`flex items-center gap-1 ${c.num ? 'justify-end' : 'justify-between'}`}>
                      <span onClick={() => toggleSort(c.key)} title="Sort" className="inline-flex items-center gap-0.5 cursor-pointer select-none">
                        {c.label}{sort.key === c.key && (sort.dir === 'asc' ? <ArrowUp size={11} /> : <ArrowDown size={11} />)}
                      </span>
                      <button title="Filter" onClick={e => { const r = e.currentTarget.getBoundingClientRect(); setPopFilter(p => p?.key === c.key ? null : { key: c.key, x: r.left, y: r.bottom }) }}
                        className="shrink-0 leading-none">
                        <Filter size={11} className={active ? 'text-primary-600' : 'text-gray-400'} />
                      </button>
                    </div>
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {view.slice(0, RENDER_CAP).map((r, i) => (
              <tr key={i} className="border-b border-gray-100 hover:bg-gray-50">
                {cols.map(c => {
                  // hoverable breakdowns: STK_TTL → SLOC-wise; #Art → actual articles + stock
                  const hk = c.ttl ? 'sloc' : (c.key === 'articles' && (r.arts?.length)) ? 'art' : null
                  return (
                    <td key={c.key}
                      onMouseEnter={hk ? (e) => setHover({ r, kind: hk, x: e.clientX, y: e.clientY }) : undefined}
                      onMouseMove={hk ? (e) => setHover(h => h ? { ...h, x: e.clientX, y: e.clientY } : h) : undefined}
                      onMouseLeave={hk ? () => setHover(null) : undefined}
                      className={`px-2 py-0.5 !text-[10px] whitespace-nowrap ${c.num ? 'text-right' : ''} ${c.mono ? 'font-mono' : ''} ${
                        hk === 'art' ? 'cursor-help text-primary-700 font-semibold underline decoration-dotted underline-offset-2' : ''} ${
                        c.ttl ? 'font-bold text-gray-900 bg-primary-50/40' : c.num ? 'text-gray-800' : 'text-gray-700'}`}>
                      {c.num && !raw(r, c) ? '—' : disp(r, c)}
                    </td>
                  )
                })}
              </tr>
            ))}
            {!view.length && (
              <tr><td colSpan={cols.length} className="px-3 py-8 text-center text-gray-400">
                No results. Activate SLOCs on SLOC Settings, then Calculate.
              </td></tr>
            )}
            {view.length > RENDER_CAP && (
              <tr><td colSpan={cols.length} className="px-3 py-2 text-center text-[10px] text-gray-500 bg-gray-50">
                Showing first {RENDER_CAP.toLocaleString()} of {view.length.toLocaleString()} rows — totals above cover <b>all</b> rows. Use search / column filters to narrow.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Column faceted filter popover — distinct values + search + multi-select */}
      {popFilter && (() => {
        const c = cols.find(x => x.key === popFilter.key)
        return (
          <ColumnFilterPopover label={c?.label} values={distinct[popFilter.key] || []} value={colq[popFilter.key]} pos={popFilter}
            onChange={f => setColq(q => { const n = { ...q }; if (f) n[popFilter.key] = f; else delete n[popFilter.key]; return n })}
            onClose={() => setPopFilter(null)} />
        )
      })()}

      {/* Floating breakup card — SLOC-wise (STK_TTL) or actual-article-wise (#Art) */}
      {hover && (() => {
        const isArt = hover.kind === 'art'
        const rows = isArt
          ? (hover.r.arts || []).map(a => [a.article_number, a.qty])
          : Object.entries(hover.r.slocs || {})
        return (
          <div style={{
            position: 'fixed', left: Math.min(hover.x + 14, window.innerWidth - 250),
            top: Math.min(hover.y + 14, window.innerHeight - 280), zIndex: 70, width: 232,
            background: '#fff', border: '1px solid #e5e7eb', borderRadius: 8,
            boxShadow: '0 8px 24px rgba(0,0,0,.12)', padding: 10, pointerEvents: 'none',
          }}>
            <div style={{ fontSize: 10.5, color: '#6b7280', marginBottom: 6 }}>
              {isArt ? 'Actual articles' : 'SLOC-wise stock'} · <span style={{ fontFamily: 'monospace' }}>{hover.r.loc}</span> / <span style={{ fontFamily: 'monospace' }}>{hover.r.ref_art}</span>
            </div>
            <div style={{ maxHeight: 190, overflow: 'auto' }}>
              {rows.length ? rows.map(([k, q]) => (
                <div key={k} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, padding: '1px 0' }}>
                  <span style={{ fontFamily: 'monospace', color: '#374151' }}>{k}</span>
                  <span style={{ color: q < 0 ? '#dc2626' : '#111827' }}>{Math.round(q).toLocaleString()}</span>
                </div>
              )) : <div style={{ fontSize: 11, color: '#9ca3af' }}>no breakdown</div>}
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11.5, fontWeight: 700, borderTop: '1px solid #e5e7eb', marginTop: 6, paddingTop: 5 }}>
              <span>{isArt ? `${rows.length} article${rows.length !== 1 ? 's' : ''}` : 'STK_TTL'}</span>
              <span>{Math.round(hover.r.stk_ttl || 0).toLocaleString()}</span>
            </div>
          </div>
        )
      })()}
    </div>
  )
}
