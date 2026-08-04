import { useEffect, useMemo, useState } from 'react'
import { Play, RefreshCw, Layers, Store, Boxes, X, Search, ChevronUp, ChevronDown, Filter, AlertTriangle, Download } from 'lucide-react'
import toast from 'react-hot-toast'
import { faConsAPI } from '@/services/api'
import { C } from '@/theme/colors'
import ColumnFilterPopover, { passColumn, filterActive, matchGlobal, exportCsv } from '@/components/facons/ColumnFilter'

const STREAMS = [{ k: 'ALL', label: 'All', icon: Layers }, { k: 'FA', label: 'FA', icon: Store }, { k: 'CONS', label: 'CONS', icon: Boxes }]
const SCOPES = [{ k: 'UPC', label: 'UPC' }, { k: 'OLD', label: 'Old' }, { k: 'ALL', label: 'All' }]
const num = v => v == null ? 0 : Math.round(+v).toLocaleString()

const COLS = [
  { key: 'priority', label: '#', num: true, w: 44 },
  { key: 'stream', label: 'Stream', w: 60 },
  { key: 'st_cd', label: 'Store', mono: true, w: 74 },
  { key: 'rdc', label: 'RDC', w: 60 },
  { key: 'ref_art', label: 'Ref Article', mono: true, w: 104 },
  { key: 'ref_art_desc', label: 'Description', w: 200 },
  { key: 'maj_cat', label: 'MAJ_CAT', w: 150 },
  { key: 'mbq', label: 'MBQ', num: true, w: 78 },
  { key: 'store_stock', label: 'Store Stk', num: true, w: 78 },
  { key: 'required', label: 'Required', num: true, w: 80 },
  { key: 'pack_sz', label: 'Pack', num: true, w: 56 },
  { key: 'alloc_qty', label: 'Alloc Qty', num: true, w: 84, alloc: true },
]

function fmtDT(iso) { return iso ? `${String(iso).slice(0, 10)} ${String(iso).slice(11, 16)}` : '' }

// Manual override: click a ref → pick actual articles (with remaining warehouse stock).
function ArticleModal({ sessionId, refRow, onClose, onSaved }) {
  const [data, setData] = useState(null)
  const [items, setItems] = useState([])
  const [saving, setSaving] = useState(false)
  const [q, setQ] = useState('')
  const [hideEmpty, setHideEmpty] = useState(true)   // hide articles with 0 warehouse stock
  useEffect(() => {
    faConsAPI.allocArticles(sessionId, refRow.id).then(({ data }) => {
      setData(data.data); setItems((data.data?.items || []).map(x => ({ ...x })))
    }).catch(() => setData({ ref: {}, items: [] }))
  }, [sessionId, refRow])
  const setQty = (art, v) => setItems(list => list.map(x => x.article_number === art ? { ...x, qty: v === '' ? '' : Math.max(0, +v || 0) } : x))
  const total = items.reduce((n, x) => n + (+x.qty || 0), 0)
  const target = data?.ref?.alloc_qty ?? 0
  const emptyCount = items.filter(x => !(x.avail_stock > 0) && !(+x.qty > 0)).length
  const shown = items.filter(x => {
    if (q.trim() && !`${x.article_number} ${x.sz || ''} ${x.clr || ''}`.toLowerCase().includes(q.trim().toLowerCase())) return false
    // hide 0-stock articles, but never hide one that already carries an assigned qty
    if (hideEmpty && !(x.avail_stock > 0) && !(+x.qty > 0)) return false
    return true
  })
  const save = async () => {
    setSaving(true)
    try {
      const { data: r } = await faConsAPI.allocSaveArticles(sessionId, refRow.id, items.filter(x => +x.qty > 0))
      toast.success(r.message || 'Saved'); onSaved?.(); onClose()
    } catch (e) { toast.error(e.response?.data?.detail || 'Save failed') }
    finally { setSaving(false) }
  }
  const td = { padding: '4px 8px', fontSize: 11, borderTop: `1px solid ${C.cardBorder}`, whiteSpace: 'nowrap' }
  return (
    <div onClick={() => !saving && onClose()} style={{ position: 'fixed', inset: 0, zIndex: 80, background: 'rgba(0,0,0,.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 16 }}>
      <div onClick={e => e.stopPropagation()} style={{ width: 720, maxWidth: '95vw', maxHeight: '88vh', display: 'flex', flexDirection: 'column', background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 12, overflow: 'hidden' }}>
        <div style={{ padding: '12px 16px', borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: C.text }}>
            Articles · <span style={{ fontFamily: 'monospace' }}>{refRow.st_cd}</span> / <span style={{ fontFamily: 'monospace' }}>{refRow.ref_art}</span>
            <span style={{ fontWeight: 400, color: C.textMuted }}> — allocate {num(target)} across actual articles (most warehouse stock first)</span>
          </div>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer' }}><X size={18} color={C.textMuted} /></button>
        </div>
        <div style={{ padding: '8px 12px', borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', alignItems: 'center', gap: 10 }}>
          <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search articles…" className="input !py-1 !text-[11px]" style={{ width: 220 }} />
          <span style={{ fontSize: 11, color: total === target ? C.green : C.amber, fontWeight: 700 }}>Assigned {num(total)} / {num(target)}{total !== target ? ' (mismatch)' : ' ✓'}</span>
          {emptyCount > 0 && (
            <button onClick={() => setHideEmpty(v => !v)} title="Show / hide articles with zero warehouse stock"
              className={`btn-sm ${hideEmpty ? 'btn-secondary' : 'btn-primary'}`} style={{ fontSize: 10, padding: '2px 8px' }}>
              {hideEmpty ? `Show 0-stock (${emptyCount})` : 'Hide 0-stock'}
            </button>
          )}
          <span style={{ fontSize: 10, color: C.textMuted, marginLeft: 'auto' }}>{shown.length} of {items.length} articles</span>
        </div>
        <div style={{ overflow: 'auto', flex: 1 }}>
          {data === null ? <div style={{ padding: 20, textAlign: 'center', color: C.textMuted, fontSize: 12 }}>Loading…</div>
            : <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead><tr>{['Article', 'SZ', 'CLR', 'Warehouse stock', 'Qty'].map((h, i) => <th key={h} style={{ ...td, position: 'sticky', top: 0, background: C.headerBg, fontWeight: 800, color: C.textSub, textAlign: i >= 3 ? 'right' : 'left', borderTop: 'none' }}>{h}</th>)}</tr></thead>
              <tbody>{shown.map(x => (
                <tr key={x.article_number}>
                  <td style={{ ...td, fontFamily: 'monospace' }}>{x.article_number}</td>
                  <td style={td}>{x.sz || '—'}</td>
                  <td style={td}>{x.clr || '—'}</td>
                  <td style={{ ...td, textAlign: 'right', color: x.avail_stock > 0 ? C.text : C.textMuted }}>{num(x.avail_stock)}</td>
                  <td style={{ ...td, textAlign: 'right' }}>
                    <input type="number" min="0" value={x.qty} onChange={e => setQty(x.article_number, e.target.value)}
                      className="input !py-0.5 !text-[11px]" style={{ width: 72, textAlign: 'right' }} />
                  </td>
                </tr>
              ))}
              {!shown.length && <tr><td colSpan={5} style={{ padding: 20, textAlign: 'center', color: C.textMuted, fontSize: 12 }}>No articles.</td></tr>}
              </tbody>
            </table>}
        </div>
        <div style={{ padding: '10px 16px', borderTop: `1px solid ${C.cardBorder}`, display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <button onClick={onClose} disabled={saving} className="btn-secondary btn-sm">Cancel</button>
          <button onClick={save} disabled={saving} className="btn-primary btn-sm">{saving ? 'Saving…' : 'Save split'}</button>
        </div>
      </div>
    </div>
  )
}

export default function FaConsAllocPage() {
  const [stream, setStream] = useState('ALL')
  const [scope, setScope] = useState('UPC')
  const [running, setRunning] = useState(false)
  const [sessions, setSessions] = useState([])
  const [sessionId, setSessionId] = useState(null)
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(false)
  const [colq, setColq] = useState({}); const [popFilter, setPopFilter] = useState(null)
  const [sort, setSort] = useState({ key: 'priority', dir: 'asc' }); const [gq, setGq] = useState('')
  const [artRef, setArtRef] = useState(null)   // ref row open in the article-override modal

  const loadSessions = async () => { try { const { data } = await faConsAPI.allocSessions(); setSessions(data.data?.items || []) } catch { /* */ } }
  const loadResults = async (sid) => {
    if (!sid) { setRows([]); return }
    setLoading(true)
    try { const { data } = await faConsAPI.allocResults(sid); setRows(data.data?.items || []) }
    catch (e) { toast.error(e.response?.data?.detail || 'Failed to load results') }
    finally { setLoading(false) }
  }
  useEffect(() => { loadSessions() }, [])
  useEffect(() => { loadResults(sessionId) }, [sessionId])

  const run = async () => {
    setRunning(true)
    try {
      const { data } = await faConsAPI.allocRun(stream, scope)
      toast.success(data.message || 'Allocation done')
      await loadSessions()
      setSessionId(data.data?.session_id ?? null)
    } catch (e) { toast.error(e.response?.data?.detail || 'Allocation failed') }
    finally { setRunning(false) }
  }

  const cellVal = (c, r) => String((c.num ? r[c.key] : r[c.key]) ?? '')
  const distinct = useMemo(() => {
    const m = {}; COLS.forEach(c => { const s = new Set(); rows.forEach(r => s.add(cellVal(c, r))); m[c.key] = [...s].sort((a, b) => a.localeCompare(b, undefined, { numeric: true })) }); return m
  }, [rows])
  const view = useMemo(() => {
    let out = rows.filter(r => COLS.every(c => passColumn(colq[c.key], cellVal(c, r))))
    if (gq.trim()) out = out.filter(r => matchGlobal(gq, COLS.map(c => cellVal(c, r))))
    if (sort.key) {
      const isN = COLS.find(c => c.key === sort.key)?.num
      out = [...out].sort((a, b) => { let x = a[sort.key], y = b[sort.key]; if (isN) { x = +x || 0; y = +y || 0; return sort.dir === 'asc' ? x - y : y - x } return sort.dir === 'asc' ? String(x ?? '').localeCompare(String(y ?? '')) : String(y ?? '').localeCompare(String(x ?? '')) })
    }
    return out
  }, [rows, colq, sort, gq])
  const toggleSort = k => setSort(s => s.key === k ? { key: k, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key: k, dir: 'asc' })

  const totUnits = rows.reduce((n, r) => n + (+r.alloc_qty || 0), 0)
  const totReq = rows.reduce((n, r) => n + (+r.required || 0), 0)
  const curSess = sessions.find(s => s.id === sessionId)
  const th = { padding: '5px 8px', fontSize: 10, fontWeight: 800, color: C.textSub, background: C.headerBg, position: 'sticky', top: 0, borderBottom: `2px solid ${C.cardBorder}`, userSelect: 'none' }
  const td = { padding: '3px 8px', fontSize: 10, color: C.textSub, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', borderTop: `1px solid ${C.cardBorder}` }

  return (
    <div className="p-4 space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <h1 className="page-title">FA &amp; CONS · Allocation</h1>
          <p className="text-[11px] text-gray-500 mt-0.5 max-w-3xl">
            One console for FA / CONS across UPC / Old / All stores. CENTRAL reference articles only, allocated from the DC pool,
            rounded to whole packs, oldest store first. Every run is a session you can review. <b>Ref-art level (Phase B.1)</b> — article split &amp; manual override next.
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm">
            {STREAMS.map(s => <button key={s.k} onClick={() => setStream(s.k)} className={`flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-semibold ${stream === s.k ? 'bg-primary-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}><s.icon size={13} />{s.label}</button>)}
          </div>
          <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm">
            {SCOPES.map(s => <button key={s.k} onClick={() => setScope(s.k)} className={`px-2.5 py-1 text-[11px] font-semibold ${scope === s.k ? 'bg-gray-800 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>{s.label}</button>)}
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button onClick={run} disabled={running} className="btn-primary btn-sm flex items-center gap-1.5"><Play size={14} className={running ? 'animate-pulse' : ''} /> {running ? 'Running…' : `Run ${stream} · ${scope}`}</button>
        <div>
          <select value={sessionId ?? ''} onChange={e => setSessionId(e.target.value ? +e.target.value : null)} className="input !py-1 !text-[11px]" style={{ maxWidth: 340 }}>
            <option value="">Select a session…</option>
            {sessions.map(s => <option key={s.id} value={s.id}>#{s.id} · {s.stream}/{s.store_scope} · {fmtDT(s.created_at)} · {num(s.n_units)}u</option>)}
          </select>
        </div>
        <button onClick={() => { loadSessions(); loadResults(sessionId) }} className="btn-secondary btn-sm flex items-center gap-1.5"><RefreshCw size={14} className={loading ? 'animate-spin' : ''} /> Refresh</button>
      </div>

      {curSess && (
        <div className="flex flex-wrap gap-2">
          {[['Ref lines', curSess.n_ref, C.text], ['Stores', curSess.n_stores, C.blue], ['Units allocated', num(totUnits), C.green], ['Total required', num(totReq), C.amber]].map(([k, v, col]) => (
            <div key={k} className="inline-flex items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 py-1">
              <span className="text-[9px] font-semibold uppercase tracking-wide text-gray-400">{k}</span>
              <span className="text-[13px] font-bold tabular-nums" style={{ color: col }}>{v}</span>
            </div>
          ))}
        </div>
      )}

      {curSess && totUnits === 0 && rows.length === 0 && totReq === 0 && (
        <div className="flex items-start gap-2 rounded-lg border p-2.5 text-[11px]" style={{ background: C.amberBg, borderColor: C.amberBd, color: C.amber }}>
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>This run allocated nothing because there is <b>no FA/CONS warehouse (DC) stock in <code>ET_MSA_STK</code> yet</b> — the pool the engine ships from (read via the MSA SLOCs on SLOC Settings). Store stock comes from <code>ET_STORE_STOCK</code>. Once FA/CONS stock is maintained in <code>ET_MSA_STK</code>, allocation will ship automatically — no change needed.</span>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-1.5 text-[10px]">
        <div className="relative">
          <Search size={12} style={{ position: 'absolute', left: 7, top: 6, color: '#9ca3af' }} />
          <input value={gq} onChange={e => setGq(e.target.value)} placeholder="Search all columns (a, b, c = any)…" className="input !py-0.5 !text-[10px]" style={{ paddingLeft: 24, width: 220 }} />
          {gq && <button onClick={() => setGq('')} style={{ position: 'absolute', right: 5, top: 5, background: 'none', border: 'none', cursor: 'pointer' }}><X size={11} color="#9ca3af" /></button>}
        </div>
        {Object.values(colq).some(filterActive) && <button onClick={() => setColq({})} className="btn-secondary btn-sm flex items-center gap-1 !py-0.5"><X size={12} /> Clear</button>}
        <button onClick={() => exportCsv(`facons_alloc_session_${sessionId || 'x'}`, COLS, view)} disabled={!view.length} className="btn-secondary btn-sm flex items-center gap-1 !py-0.5"><Download size={12} /> Export</button>
        <span className="text-gray-400">{view.length}/{rows.length}</span>
      </div>

      <div style={{ overflow: 'auto', maxHeight: 'calc(100vh - 300px)', background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 10 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead><tr>
            {COLS.map(c => {
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
            <th style={{ ...th, textAlign: 'center', width: 96 }}>Articles</th>
          </tr></thead>
          <tbody>
            {view.map(r => (
              <tr key={r.id} style={{ background: C.card }} onMouseEnter={e => e.currentTarget.style.background = C.rowAlt} onMouseLeave={e => e.currentTarget.style.background = C.card}>
                {COLS.map(c => {
                  if (c.alloc) return <td key={c.key} style={{ ...td, textAlign: 'right', fontWeight: 800, color: r.alloc_qty > 0 ? C.green : C.textMuted }}>{num(r.alloc_qty)}</td>
                  const v = c.num ? num(r[c.key]) : (r[c.key] ?? '—')
                  return <td key={c.key} style={{ ...td, textAlign: c.num ? 'right' : 'left', fontFamily: c.mono ? 'monospace' : 'inherit', color: c.num ? C.text : C.textSub, fontWeight: c.num ? 600 : 400 }}>{v}</td>
                })}
                <td style={{ ...td, textAlign: 'center' }}>
                  <button onClick={() => setArtRef(r)} title="Split into actual articles / manual override"
                    className="inline-flex items-center gap-1" style={{ cursor: 'pointer', border: `1px solid ${r.manual ? C.amberBd : C.cardBorder}`, background: r.manual ? C.amberBg : C.card, color: r.manual ? C.amber : C.primary, fontSize: 10, fontWeight: 700, borderRadius: 6, padding: '1px 7px' }}>
                    <Boxes size={11} /> {r.n_articles || 0}{r.manual ? '·M' : ''}
                  </button>
                </td>
              </tr>
            ))}
            {!view.length && <tr><td colSpan={COLS.length + 1} style={{ padding: 24, textAlign: 'center', color: C.textMuted }}>{loading ? 'Loading…' : sessionId ? 'No allocated lines in this session.' : 'Run an allocation or pick a session.'}</td></tr>}
          </tbody>
        </table>
      </div>

      {popFilter && (() => { const c = COLS.find(x => x.key === popFilter.key); return (
        <ColumnFilterPopover label={c?.label} values={distinct[popFilter.key] || []} value={colq[popFilter.key]} pos={popFilter}
          onChange={f => setColq(q => { const n = { ...q }; if (f) n[popFilter.key] = f; else delete n[popFilter.key]; return n })} onClose={() => setPopFilter(null)} />
      ) })()}

      {artRef && <ArticleModal sessionId={sessionId} refRow={artRef} onClose={() => setArtRef(null)} onSaved={() => loadResults(sessionId)} />}
    </div>
  )
}
