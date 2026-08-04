import { useEffect, useMemo, useState } from 'react'
import { RefreshCw, Save, Store, Boxes, Database, Search, Info, CheckCircle2, Circle, ArrowUp, ArrowDown } from 'lucide-react'
import toast from 'react-hot-toast'
import { faConsAPI } from '@/services/api'

const STREAMS = [
  { key: 'FA', label: 'FA', icon: Store },
  { key: 'CONS', label: 'CONS', icon: Boxes },
]
// Each listing SLOC table IS a bucket. Active SLOCs of that table roll into that bucket.
const SOURCES = [
  { key: 'STORE', label: 'Store SLOCs', table: 'ARS_STORE_SLOC_SETTINGS',
    hint: 'Active → Store stock (from ET_STORE_STOCK) — reduces the requirement.' },
  { key: 'MSA', label: 'MSA / DC SLOCs', table: 'ARS_MSA_SLOC_SETTINGS',
    hint: 'Active → MSA / DC pool (from ET_MSA_STK) — available to ship.' },
]

function SortTh({ label, col, sort, setSort, align = 'left', className = '', title }) {
  const on = sort.key === col
  const dir = on ? sort.dir : null
  return (
    <th title={title}
      className={`px-3 py-1.5 font-semibold text-gray-600 select-none cursor-pointer hover:text-primary-600 ${
        align === 'right' ? 'text-right' : align === 'center' ? 'text-center' : 'text-left'} ${className}`}
      onClick={() => setSort(s => s.key === col ? { key: col, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key: col, dir: (col === 'qty' || col === 'pct') ? 'desc' : 'asc' })}>
      <span className={`inline-flex items-center gap-0.5 ${align === 'right' ? 'justify-end' : ''}`}>
        {label}{on && (dir === 'asc' ? <ArrowUp size={11} /> : <ArrowDown size={11} />)}
      </span>
    </th>
  )
}

function SourceCard({ src, items, dirty, onToggle, onAll, totals, snapDate }) {
  const [q, setQ] = useState('')
  const [onlyStock, setOnlyStock] = useState(false)
  const [sort, setSort] = useState({ key: 'qty', dir: 'desc' })
  // Clamp negative net-stock to 0 defensively: a SLOC whose issues exceed receipts has no
  // dispatchable stock. Guards % share / totals even if a stale cache row is still negative
  // (otherwise one big negative, e.g. CONS MSA 0044, shrinks the denominator → 201%).
  const qtyOf = (sloc) => Math.max(0, Number(totals?.[sloc] || 0))
  const totalQty = items.reduce((n, r) => n + qtyOf(r.sloc), 0)
  const pctOf = (qv) => (totalQty > 0 ? (qv / totalQty) * 100 : 0)
  const filtered = useMemo(() => {
    const toks = q.toLowerCase().split(',').map(t => t.trim()).filter(Boolean)   // comma = match any (OR)
    let arr = items
    if (toks.length) arr = arr.filter(r => {
      const hay = `${r.sloc} ${r.kpi || ''}`.toLowerCase()
      return toks.some(t => hay.includes(t))
    })
    if (onlyStock) arr = arr.filter(r => qtyOf(r.sloc) > 0)
    const d = sort.dir === 'asc' ? 1 : -1
    const listingStr = (r) => `${r.listing_status || ''} ${r.kpi || ''}`.trim()
    const cmp = {
      sloc: (a, b) => String(a.sloc).localeCompare(b.sloc, undefined, { numeric: true }) * d,
      qty: (a, b) => ((qtyOf(a.sloc) - qtyOf(b.sloc)) * d) || String(a.sloc).localeCompare(b.sloc),
      pct: (a, b) => ((qtyOf(a.sloc) - qtyOf(b.sloc)) * d) || String(a.sloc).localeCompare(b.sloc),
      listing: (a, b) => (listingStr(a).localeCompare(listingStr(b)) * d) || String(a.sloc).localeCompare(b.sloc),
      active: (a, b) => ((a.active === b.active) ? 0 : (a.active ? 1 : -1) * d) || String(a.sloc).localeCompare(b.sloc),
    }[sort.key]
    return [...arr].sort(cmp)
  }, [items, q, onlyStock, totals, sort]) // eslint-disable-line
  const active = items.filter(r => r.active).length
  const withStock = items.filter(r => qtyOf(r.sloc) > 0).length
  // "on" coverage: how much of the total stock the currently-ACTIVE SLOCs capture
  const activeQty = items.filter(r => r.active).reduce((n, r) => n + qtyOf(r.sloc), 0)
  const coverPct = pctOf(activeQty)

  return (
    <div className="card flex flex-col overflow-hidden" style={{ maxHeight: 'calc(100vh - 300px)' }}>
      <div className="card-header flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 flex-wrap">
          <Database size={14} className="text-primary-500" />
          <span>{src.label}</span>
          <span className="badge-gray font-mono !text-[9px]">{src.table}</span>
          <span className="badge-primary">{active} active / {items.length}</span>
          <span className="badge-gray !text-[9px]" title="SLOCs that carry stock right now">{withStock} with stock</span>
        </div>
        <div className="flex items-center gap-1.5">
          <button onClick={() => onAll(src.key, true)} className="text-[10px] font-semibold text-emerald-600 hover:underline">All</button>
          <span className="text-gray-300">·</span>
          <button onClick={() => onAll(src.key, false)} className="text-[10px] font-semibold text-gray-500 hover:underline">None</button>
        </div>
      </div>
      <div className="px-3 py-2 border-b border-gray-100 flex items-center gap-2">
        <div className="relative flex-1">
          <Search size={13} className="absolute left-2.5 top-2 text-gray-400" />
          <input value={q} onChange={e => setQ(e.target.value)} placeholder="Filter SLOC / KPI (a, b = any)…" className="input !pl-8 !py-1" />
        </div>
        <button onClick={() => setOnlyStock(v => !v)} title="Show only SLOCs that carry stock"
          className={`shrink-0 inline-flex items-center gap-1 px-2 py-1 rounded-md text-[10px] font-semibold border ${
            onlyStock ? 'bg-primary-600 text-white border-primary-600' : 'bg-white text-gray-500 border-gray-300'}`}>
          <Boxes size={12} /> With stock
        </button>
      </div>
      <div className="overflow-y-auto">
        <table className="w-full text-[11px]">
          <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
            <tr>
              <SortTh label="SLOC" col="sloc" sort={sort} setSort={setSort} className="w-[30%]" />
              <SortTh label="Stock qty" col="qty" sort={sort} setSort={setSort} align="right" className="w-[20%]"
                title={`Live stock in this SLOC${snapDate ? ` · ${snapDate}` : ''}`} />
              <SortTh label="% share" col="pct" sort={sort} setSort={setSort} align="right" className="w-[12%]"
                title="Share of this source's total stock" />
              <SortTh col="listing" sort={sort} setSort={setSort}
                label={<>Listing <span className="font-normal normal-case">(read-only)</span></>} />
              <SortTh label="Active" col="active" sort={sort} setSort={setSort} align="center" className="w-[84px]" />
            </tr>
          </thead>
          <tbody>
            {filtered.map(r => {
              const key = `${r.source}:${r.sloc}`
              const qv = qtyOf(r.sloc)
              const pv = pctOf(qv)
              return (
                <tr key={key} className={`border-b border-gray-50 ${dirty.has(key) ? 'bg-amber-50' : 'hover:bg-gray-50'}`}>
                  <td className="px-3 py-1 font-mono text-gray-900">{r.sloc}</td>
                  <td className={`px-3 py-1 text-right tabular-nums ${qv > 0 ? 'font-semibold text-primary-700' : 'text-gray-300'}`}>
                    {qv > 0 ? Math.round(qv).toLocaleString() : '—'}
                  </td>
                  <td className={`px-3 py-1 text-right tabular-nums ${qv > 0 ? 'text-gray-500' : 'text-gray-300'}`}>
                    {qv > 0 ? `${pv.toFixed(pv < 10 ? 1 : 0)}%` : '—'}
                  </td>
                  <td className="px-3 py-1 text-gray-400">{r.listing_status}{r.kpi ? ` · ${r.kpi}` : ''}</td>
                  <td className="px-3 py-1 text-center">
                    <button onClick={() => onToggle(r)}
                      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold ${
                        r.active ? 'bg-emerald-100 text-emerald-700' : 'bg-gray-100 text-gray-400'}`}>
                      {r.active ? <CheckCircle2 size={11} /> : <Circle size={11} />}{r.active ? 'On' : 'Off'}
                    </button>
                  </td>
                </tr>
              )
            })}
            {!filtered.length && <tr><td colSpan={5} className="px-3 py-6 text-center text-gray-400">No SLOCs.</td></tr>}
          </tbody>
          {!!items.length && (
            <tfoot className="sticky bottom-0 bg-gray-50 border-t border-gray-200">
              <tr>
                <td className="px-3 py-1.5 font-semibold text-gray-600">
                  Total {snapDate && <span className="font-normal text-[9px] text-gray-400">@ {snapDate}</span>}
                </td>
                <td className="px-3 py-1.5 text-right font-bold text-primary-700 tabular-nums">{Math.round(totalQty).toLocaleString()}</td>
                <td colSpan={3} className="px-3 py-1.5 text-[10px] text-gray-500">
                  <span title="Stock captured by the SLOCs currently switched ON, vs all stock in this source">
                    On covers <b className="text-emerald-700 tabular-nums">{Math.round(activeQty).toLocaleString()}</b> of {Math.round(totalQty).toLocaleString()}
                    {' '}<b className="text-emerald-700">({coverPct.toFixed(coverPct < 10 ? 1 : 0)}%)</b>
                  </span>
                </td>
              </tr>
            </tfoot>
          )}
        </table>
      </div>
    </div>
  )
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
function fmtDate(iso) {
  if (!iso) return null
  const m = String(iso).slice(0, 10).match(/^(\d{4})-(\d{2})-(\d{2})$/)
  return m ? `${m[3]}-${MONTHS[+m[2] - 1]}-${m[1]}` : iso
}

export default function FaConsSlocSettingsPage() {
  const [stream, setStream] = useState('FA')
  const [items, setItems] = useState([])
  const [dirty, setDirty] = useState(new Set())
  const [loading, setLoading] = useState(false)
  const [totals, setTotals] = useState({})       // { STORE: {sloc: qty}, MSA: {sloc: qty} }
  const [totalDates, setTotalDates] = useState({})
  const [syncing, setSyncing] = useState(false)

  const load = async (s = stream) => {
    setLoading(true)
    try {
      const { data } = await faConsAPI.listSloc(s)
      setItems(data.data?.items || [])
      setDirty(new Set())
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Failed to load SLOC settings')
    } finally { setLoading(false) }
  }

  // Returns the # of newly-discovered SLOCs (so the caller can reload the list).
  const loadTotals = async (s = stream, { force } = {}) => {
    try {
      const { data } = await faConsAPI.slocStockTotals(s, { force })
      setTotals(data.data?.totals || {})
      setTotalDates(data.data?.dates || {})
      return Number(data.data?.added ?? 0)
    } catch { setTotals({}); setTotalDates({}); return 0 }
  }

  // On page active / stream change: render the SLOC list fast, then fill live qty from the
  // cached probe (which also discovers new SLOCs). No separate DISTINCT-scan sync on this path.
  useEffect(() => {
    let alive = true
    ;(async () => {
      if (alive) await load(stream)
      setSyncing(true)
      const added = await loadTotals(stream)
      if (!alive) return
      setSyncing(false)
      if (added > 0) { toast(`${added} new SLOC${added > 1 ? 's' : ''} discovered`, { icon: '🔄' }); await load(stream) }
    })()
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stream])

  const markDirty = keys => setDirty(prev => { const n = new Set(prev); keys.forEach(k => n.add(k)); return n })
  const onToggle = (row) => {
    setItems(prev => prev.map(r => (r.source === row.source && r.sloc === row.sloc) ? { ...r, active: !r.active } : r))
    markDirty([`${row.source}:${row.sloc}`])
  }
  const onAll = (source, val) => {
    setItems(prev => prev.map(r => r.source === source ? { ...r, active: val } : r))
    markDirty(items.filter(r => r.source === source).map(r => `${r.source}:${r.sloc}`))
  }
  // Manual button = thorough: full DISTINCT-scan discovery + a FORCED qty rebuild (bypass cache).
  const onSync = async () => {
    setSyncing(true)
    try {
      const { data } = await faConsAPI.syncSloc(stream)
      toast.success(data.message || 'Synced')
      await load(stream)
      await loadTotals(stream, { force: true })
    } catch (e) { toast.error(e.response?.data?.detail || 'Sync failed') }
    finally { setSyncing(false) }
  }
  const onSave = async () => {
    const changed = items.filter(r => dirty.has(`${r.source}:${r.sloc}`))
      .map(r => ({ source: r.source, sloc: r.sloc, stream, active: !!r.active }))
    if (!changed.length) { toast('Nothing changed'); return }
    try { const { data } = await faConsAPI.bulkSloc(changed); toast.success(data.message || 'Saved'); load(stream) }
    catch (e) { toast.error(e.response?.data?.detail || 'Save failed') }
  }

  return (
    <div className="p-4 space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="page-title">FA &amp; CONS · SLOC Settings</h1>
          <p className="text-[11px] text-gray-500 mt-0.5">
            Turn each SLOC on/off per stream. <b>Store</b> table active SLOCs → store stock; <b>MSA/DC</b> table active SLOCs → DC pool. Only DIV = FA/CO. Listing columns are read-only.
          </p>
        </div>
        <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm">
          {STREAMS.map(s => (
            <button key={s.key} onClick={() => setStream(s.key)}
              className={`flex items-center gap-1.5 px-3 py-1.5 text-[11px] font-semibold transition-colors ${
                stream === s.key ? 'bg-primary-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
              <s.icon size={14} /> {s.label}
            </button>
          ))}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button onClick={onSync} disabled={syncing} className="btn-secondary btn-sm flex items-center gap-1.5">
          <RefreshCw size={14} className={syncing || loading ? 'animate-spin' : ''} /> {syncing ? 'Syncing…' : 'Sync from stock'}
        </button>
        <button onClick={onSave} className="btn-primary btn-sm flex items-center gap-1.5">
          <Save size={14} /> Save changes{dirty.size ? ` (${dirty.size})` : ''}
        </button>
        <div className="flex items-start gap-1.5 text-[10.5px] text-gray-500 ml-auto max-w-[520px]">
          <Info size={13} className="text-primary-500 shrink-0 mt-0.5" />
          <span>Active/Inactive is independent per stream. The <b>{stream}</b> tab edits {stream}; switch tabs for the other. Run <b>Stock &amp; MSA</b> after changing this.</span>
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        {SOURCES.map(src => (
          <SourceCard key={src.key} src={src} items={items.filter(r => r.source === src.key)}
            dirty={dirty} onToggle={onToggle} onAll={onAll}
            totals={totals[src.key] || {}} snapDate={fmtDate(totalDates[src.key])} />
        ))}
      </div>
    </div>
  )
}
