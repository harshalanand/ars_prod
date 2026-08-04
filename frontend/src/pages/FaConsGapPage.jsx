import { useMemo, useRef, useState } from 'react'
import { Play, RefreshCw, Layers, Store, Boxes, X, Search, ChevronUp, ChevronDown, Filter, AlertTriangle, Download } from 'lucide-react'
import toast from 'react-hot-toast'
import { faConsAPI } from '@/services/api'
import { C } from '@/theme/colors'
import ColumnFilterPopover, { passColumn, filterActive, matchGlobal, exportCsv } from '@/components/facons/ColumnFilter'

const STREAMS = [{ k: 'ALL', label: 'All', icon: Layers }, { k: 'FA', label: 'FA', icon: Store }, { k: 'CONS', label: 'CONS', icon: Boxes }]
const SCOPES = [{ k: 'UPC', label: 'UPC' }, { k: 'OLD', label: 'Old' }, { k: 'ALL', label: 'All' }]
const num = v => v == null ? 0 : Math.round(+v).toLocaleString()

const STORE_COLS = [
  { key: 'priority', label: '#', num: true, w: 42 },
  { key: 'stream', label: 'Stream', w: 56 },
  { key: 'st_cd', label: 'Store', mono: true, w: 70 },
  { key: 'rdc', label: 'RDC', w: 56 },
  { key: 'ref_art', label: 'Ref Article', mono: true, w: 100 },
  { key: 'ref_art_desc', label: 'Description', w: 180 },
  { key: 'maj_cat', label: 'MAJ_CAT', w: 130 },
  { key: 'mbq', label: 'MBQ', num: true, w: 72 },
  { key: 'store_stock', label: 'Store Stk', num: true, w: 74 },
  { key: 'pending', label: 'Pending', num: true, w: 68 },
  { key: 'gap', label: 'Gap', num: true, w: 68 },
  { key: 'warehouse', label: 'Warehouse', num: true, w: 82 },
  { key: 'atr', label: 'Action', w: 132, badge: true },
  { key: 'dispatch', label: 'Dispatch', num: true, w: 74 },
  { key: 'purchase', label: 'Purchase', num: true, w: 74 },
  { key: 'return_qty', label: 'Return', num: true, w: 64 },
]
const PURCH_COLS = [
  { key: 'stream', label: 'Stream', w: 56 },
  { key: 'ref_art', label: 'Ref Article', mono: true, w: 110 },
  { key: 'ref_art_desc', label: 'Description', w: 220 },
  { key: 'maj_cat', label: 'MAJ_CAT', w: 150 },
  { key: 'stores', label: 'Stores', num: true, w: 66 },
  { key: 'demand', label: 'Demand (MBQ)', num: true, w: 110 },
  { key: 'store_stock', label: 'Store Stk', num: true, w: 84 },
  { key: 'warehouse', label: 'Warehouse', num: true, w: 90 },
  { key: 'dispatch', label: 'Dispatchable', num: true, w: 100 },
  { key: 'purchase', label: 'To Purchase', num: true, w: 100, strong: true },
]
const RETURN_COLS = [
  { key: 'stream', label: 'Stream', w: 56 },
  { key: 'st_cd', label: 'Store', mono: true, w: 74 },
  { key: 'ref_art', label: 'Ref Article', mono: true, w: 110 },
  { key: 'ref_art_desc', label: 'Description', w: 220 },
  { key: 'mbq', label: 'MBQ', num: true, w: 78 },
  { key: 'store_stock', label: 'Store Stk', num: true, w: 84 },
  { key: 'return_qty', label: 'Return Qty', num: true, w: 96, strong: true },
]
const ATR_FILTERS = ['All', 'DISPATCH', 'PURCHASE', 'DISPATCH+PURCHASE', 'STORE_RETURN', 'HOLD', 'OK']
const atrColor = a => ({ DISPATCH: C.amber, 'DISPATCH+PURCHASE': C.amber, PURCHASE: C.indigo, STORE_RETURN: C.red, HOLD: C.blue, OK: C.green }[a] || C.textSub)

export default function FaConsGapPage() {
  const [stream, setStream] = useState('ALL'); const [scope, setScope] = useState('UPC')
  const [running, setRunning] = useState(false)
  const [data, setData] = useState(null)
  const [tab, setTab] = useState('store')      // store | purchase | return
  const [atr, setAtr] = useState('All')
  const [gq, setGq] = useState(''); const [colq, setColq] = useState({}); const [popFilter, setPopFilter] = useState(null)
  const [sort, setSort] = useState({ key: 'gap', dir: 'desc' })

  const seq = useRef(0)
  const run = async () => {
    const my = ++seq.current      // only the latest request may apply its result (avoids races on rapid toggles)
    setRunning(true)
    try {
      const { data: r } = await faConsAPI.gapReport(stream, scope)
      if (my === seq.current) setData(r.data)
    } catch (e) { if (my === seq.current) toast.error(e.response?.data?.detail || 'Gap report failed') }
    finally { if (my === seq.current) setRunning(false) }
  }
  // Compute ONLY when the user clicks "Run" — changing stream/scope just sets the
  // selection; the toggles no longer trigger a re-compute on every click.

  const matchAtr = (a, l) => a === 'All' ? true : a === 'SHORT' ? l.gap > 0
    : a === 'DISPATCH_ANY' ? l.dispatch > 0 : a === 'PURCHASE_ANY' ? l.purchase > 0 : l.atr === a
  const cols = tab === 'store' ? STORE_COLS : tab === 'purchase' ? PURCH_COLS : RETURN_COLS
  const base = useMemo(() => {
    if (!data) return []
    if (tab === 'purchase') return data.purchase_req || []
    if (tab === 'return') return (data.lines || []).filter(l => l.atr === 'STORE_RETURN')
    return (data.lines || []).filter(l => matchAtr(atr, l))
  }, [data, tab, atr])

  const cellVal = (c, r) => String((c.fmt ? c.fmt(r[c.key]) : r[c.key]) ?? '')
  const distinct = useMemo(() => { const m = {}; cols.forEach(c => { const s = new Set(); base.forEach(r => s.add(cellVal(c, r))); m[c.key] = [...s].sort((a, b) => a.localeCompare(b, undefined, { numeric: true })) }); return m }, [base, cols])
  const view = useMemo(() => {
    let out = base.filter(r => cols.every(c => passColumn(colq[c.key], cellVal(c, r))))
    if (gq.trim()) out = out.filter(r => matchGlobal(gq, cols.map(c => cellVal(c, r))))
    if (sort.key && cols.find(c => c.key === sort.key)) {
      const isN = cols.find(c => c.key === sort.key)?.num
      out = [...out].sort((a, b) => { let x = a[sort.key], y = b[sort.key]; if (isN) { x = +x || 0; y = +y || 0; return sort.dir === 'asc' ? x - y : y - x } return sort.dir === 'asc' ? String(x ?? '').localeCompare(String(y ?? '')) : String(y ?? '').localeCompare(String(x ?? '')) })
    }
    return out
  }, [base, cols, colq, gq, sort])
  const toggleSort = k => setSort(s => s.key === k ? { key: k, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key: k, dir: 'desc' })

  const s = data?.summary
  // [label, value, color, filterKey] — clicking a card filters the Store Gap tab
  const tiles = s ? [['Short lines', s.short, C.red, 'SHORT'], ['To dispatch', num(s.to_dispatch), C.amber, 'DISPATCH_ANY'],
    ['To purchase', num(s.to_purchase), C.indigo, 'PURCHASE_ANY'], ['To return', num(s.to_return), C.red, 'STORE_RETURN'],
    ['To hold', num(s.to_hold), C.blue, 'HOLD'], ['OK', s.ok, C.green, 'OK']] : []
  const th = { padding: '5px 8px', fontSize: 10, fontWeight: 800, color: C.textSub, background: C.headerBg, position: 'sticky', top: 0, borderBottom: `2px solid ${C.cardBorder}`, userSelect: 'none' }
  const td = { padding: '3px 8px', fontSize: 10, color: C.textSub, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', borderTop: `1px solid ${C.cardBorder}` }

  return (
    <div className="p-4 space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <h1 className="page-title">FA &amp; CONS · Gap Report</h1>
          <p className="text-[11px] text-gray-500 mt-0.5 max-w-3xl">
            MBQ vs store stock + pending, validated against the warehouse (MSA). Recommends one action per line —
            <b> Dispatch</b> (warehouse has it), <b>Purchase</b> (buy the rest), <b>Hold</b> pending, or <b>Store-return</b> the excess.
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm">
            {STREAMS.map(x => <button key={x.k} onClick={() => setStream(x.k)} className={`flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-semibold ${stream === x.k ? 'bg-primary-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}><x.icon size={13} />{x.label}</button>)}
          </div>
          <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm">
            {SCOPES.map(x => <button key={x.k} onClick={() => setScope(x.k)} className={`px-2.5 py-1 text-[11px] font-semibold ${scope === x.k ? 'bg-gray-800 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>{x.label}</button>)}
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button onClick={run} disabled={running} className="btn-primary btn-sm flex items-center gap-1.5"><Play size={14} className={running ? 'animate-pulse' : ''} /> {running ? 'Computing…' : 'Run gap report'}</button>
        <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm">
          {[['store', 'Store Gap'], ['purchase', 'Purchase Requirement'], ['return', 'Overstock / Return']].map(([k, t]) => (
            <button key={k} onClick={() => { setTab(k); setColq({}); setPopFilter(null); setSort({ key: k === 'purchase' ? 'purchase' : k === 'return' ? 'return_qty' : 'gap', dir: 'desc' }) }} className={`px-3 py-1 text-[11px] font-semibold ${tab === k ? 'bg-gray-700 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>{t}</button>
          ))}
        </div>
      </div>

      {s && (
        <div className="flex flex-wrap gap-2">
          {tiles.map(([k, v, col, key]) => {
            const active = tab === 'store' && atr === key
            return (
              <button key={k} type="button" onClick={() => { setTab('store'); setAtr(a => (a === key ? 'All' : key)) }}
                title={`Show ${k.toLowerCase()}${active ? ' (click to clear)' : ''}`}
                className="inline-flex items-center gap-2 rounded-lg border bg-white px-3 py-1 transition-all"
                style={{ cursor: 'pointer', borderColor: active ? col : '#e5e7eb', boxShadow: active ? `0 0 0 2px ${col}22` : undefined }}>
                <span className="text-[9px] font-semibold uppercase tracking-wide text-gray-400">{k}{active ? ' ▾' : ''}</span>
                <span className="text-[13px] font-bold tabular-nums" style={{ color: col }}>{v}</span>
              </button>
            )
          })}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-1.5 text-[10px]">
        {tab === 'store' && (
          <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm">
            {ATR_FILTERS.map(a => <button key={a} onClick={() => setAtr(a)} className={`px-2 py-0.5 text-[10px] font-semibold ${atr === a ? 'bg-primary-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>{a === 'All' ? 'All' : a.replace('DISPATCH+PURCHASE', 'D+P').replace('STORE_RETURN', 'Return')}</button>)}
          </div>
        )}
        <div className="relative">
          <Search size={12} style={{ position: 'absolute', left: 7, top: 6, color: '#9ca3af' }} />
          <input value={gq} onChange={e => setGq(e.target.value)} placeholder="Search (a, b, c = any)…" className="input !py-0.5 !text-[10px]" style={{ paddingLeft: 24, width: 200 }} />
          {gq && <button onClick={() => setGq('')} style={{ position: 'absolute', right: 5, top: 5, background: 'none', border: 'none', cursor: 'pointer' }}><X size={11} color="#9ca3af" /></button>}
        </div>
        {Object.values(colq).some(filterActive) && <button onClick={() => setColq({})} className="btn-secondary btn-sm flex items-center gap-1 !py-0.5"><X size={12} /> Clear</button>}
        <button onClick={() => exportCsv(`facons_gap_${tab}_${stream}_${scope}`, cols, view)} disabled={!view.length} className="btn-secondary btn-sm flex items-center gap-1 !py-0.5"><Download size={12} /> Export</button>
        <span className="text-gray-400">{view.length}/{base.length}</span>
      </div>

      <div style={{ overflow: 'auto', maxHeight: 'calc(100vh - 320px)', background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 10 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead><tr>
            {cols.map(c => {
              const active = filterActive(colq[c.key])
              return (
                <th key={c.key} style={{ ...th, textAlign: c.num ? 'right' : 'left' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 3, justifyContent: c.num ? 'flex-end' : 'space-between' }}>
                    <span onClick={() => toggleSort(c.key)} title="Sort" style={{ cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 1 }}>{c.label}{sort.key === c.key && (sort.dir === 'asc' ? <ChevronUp size={11} /> : <ChevronDown size={11} />)}</span>
                    <button title="Filter" onClick={e => { const r = e.currentTarget.getBoundingClientRect(); setPopFilter(p => p?.key === c.key ? null : { key: c.key, x: r.left, y: r.bottom }) }} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}><Filter size={11} color={active ? C.primary : C.textMuted} fill={active ? C.primary : 'none'} /></button>
                  </div>
                </th>
              )
            })}
          </tr></thead>
          <tbody>
            {view.map((r, i) => (
              <tr key={i} style={{ background: C.card }} onMouseEnter={e => e.currentTarget.style.background = C.rowAlt} onMouseLeave={e => e.currentTarget.style.background = C.card}>
                {cols.map(c => {
                  if (c.badge) return <td key={c.key} style={td}><span style={{ fontSize: 9.5, fontWeight: 700, color: atrColor(r.atr) }}>{r.atr}</span></td>
                  const v = c.num ? num(r[c.key]) : (r[c.key] ?? '—')
                  const dim = c.num && !(+r[c.key])
                  return <td key={c.key} style={{ ...td, textAlign: c.num ? 'right' : 'left', fontFamily: c.mono ? 'monospace' : 'inherit', color: c.strong ? C.indigo : dim ? C.textMuted : c.num ? C.text : C.textSub, fontWeight: c.strong ? 800 : c.num ? 600 : 400 }}>{v}</td>
                })}
              </tr>
            ))}
            {!view.length && <tr><td colSpan={cols.length} style={{ padding: 24, textAlign: 'center', color: C.textMuted }}>{running ? 'Computing…' : 'No rows. Run the gap report.'}</td></tr>}
          </tbody>
        </table>
      </div>

      {popFilter && (() => { const c = cols.find(x => x.key === popFilter.key); return (
        <ColumnFilterPopover label={c?.label} values={distinct[popFilter.key] || []} value={colq[popFilter.key]} pos={popFilter}
          onChange={f => setColq(q => { const n = { ...q }; if (f) n[popFilter.key] = f; else delete n[popFilter.key]; return n })} onClose={() => setPopFilter(null)} />
      ) })()}
    </div>
  )
}
