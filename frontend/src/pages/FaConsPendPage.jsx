import { useEffect, useMemo, useState } from 'react'
import { RefreshCw, Plus, Truck, XCircle, RotateCcw, X, Search, ChevronUp, ChevronDown, Filter, CheckCircle2, ScrollText, Download, Pencil, FileText } from 'lucide-react'
import toast from 'react-hot-toast'
import { faConsAPI } from '@/services/api'
import { C } from '@/theme/colors'
import ColumnFilterPopover, { passColumn, filterActive, matchGlobal, exportCsv } from '@/components/facons/ColumnFilter'

const STREAMS = [{ k: '', label: 'All' }, { k: 'FA', label: 'FA' }, { k: 'CONS', label: 'CONS' }]
const STATUSES = [{ k: '', label: 'All' }, { k: 'OPEN', label: 'Open' }, { k: 'CLOSED', label: 'Closed' }]
const num = v => v == null ? 0 : Math.round(+v).toLocaleString()
function fmtDT(iso) { return iso ? `${String(iso).slice(0, 10)} ${String(iso).slice(11, 16)}` : '—' }

const COLS = [
  { key: 'source', label: 'Src', w: 54 },
  { key: 'stream', label: 'Stream', w: 58 },
  { key: 'st_cd', label: 'Store', mono: true, w: 72 },
  { key: 'ref_art', label: 'Ref Article', mono: true, w: 100 },
  { key: 'article_number', label: 'Article', mono: true, w: 100 },
  { key: 'sz', label: 'SZ', w: 48 },
  { key: 'qty', label: 'Promised', num: true, w: 80 },
  { key: 'delivered', label: 'Delivered', num: true, w: 80 },
  { key: 'balance', label: 'Balance', num: true, w: 78 },
  { key: 'do_number', label: 'DO #', mono: true, w: 110 },
  { key: 'status', label: 'Status', w: 76, badge: true },
]
const OCOLS = [
  { key: 'changed_at', label: 'When', fmt: fmtDT, w: 130 },
  { key: 'action', label: 'Action', w: 96, badge: true },
  { key: 'stream', label: 'Stream', w: 56 },
  { key: 'st_cd', label: 'Store', mono: true, w: 70 },
  { key: 'ref_art', label: 'Ref Article', mono: true, w: 100 },
  { key: 'article_number', label: 'Article', mono: true, w: 100 },
  { key: 'qty', label: 'Qty', num: true, w: 64 },
  { key: 'remarks', label: 'Remarks', w: 200 },
  { key: 'changed_by', label: 'By', w: 90 },
]
const statusCol = s => s === 'CLOSED' ? { bg: C.grayBg, fg: C.gray, bd: C.grayBd } : { bg: C.greenBg, fg: C.green, bd: C.greenBd }
const actionCol = a => ({ APPROVE: C.primary, MANUAL_ADD: C.blue, DELIVER: C.green, CLOSE: C.red, REOPEN: C.amber, GENERATE_DO: C.purple || '#7c3aed', UPDATE: C.blue }[a] || C.textSub)

function GridTable({ cols, rows, sort, setSort, colq, setColq, popFilter, setPopFilter, distinct, actions, emptyMsg, sel }) {
  const th = { padding: '5px 8px', fontSize: 10, fontWeight: 800, color: C.textSub, background: C.headerBg, position: 'sticky', top: 0, borderBottom: `2px solid ${C.cardBorder}`, userSelect: 'none' }
  const td = { padding: '3px 8px', fontSize: 10, color: C.textSub, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', borderTop: `1px solid ${C.cardBorder}` }
  const toggleSort = k => setSort(s => s.key === k ? { key: k, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key: k, dir: 'asc' })
  return (
    <div style={{ overflow: 'auto', maxHeight: 'calc(100vh - 320px)', background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 10 }}>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead><tr>
          {sel && (() => { const selectable = rows.filter(r => !sel.can || sel.can(r)); const allOn = selectable.length > 0 && selectable.every(r => sel.ids.has(r.id)); return (
            <th style={{ ...th, width: 28, textAlign: 'center' }}><input type="checkbox" checked={allOn} onChange={() => sel.toggleAll(selectable)} title="Select all" /></th>
          ) })()}
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
          {actions && <th style={{ ...th, textAlign: 'center', width: 120 }}>·</th>}
        </tr></thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.id} style={{ background: C.card }} onMouseEnter={e => e.currentTarget.style.background = C.rowAlt} onMouseLeave={e => e.currentTarget.style.background = C.card}>
              {sel && <td style={{ ...td, textAlign: 'center' }}>{(!sel.can || sel.can(r)) ? <input type="checkbox" checked={sel.ids.has(r.id)} onChange={() => sel.toggle(r.id)} /> : null}</td>}
              {cols.map(c => {
                if (c.badge) {
                  const col = c.key === 'status' ? statusCol(r[c.key]) : { fg: actionCol(r[c.key]), bg: 'transparent', bd: 'transparent' }
                  return <td key={c.key} style={td}><span style={{ fontSize: 9.5, fontWeight: 700, color: col.fg, ...(c.key === 'status' ? { padding: '1px 7px', borderRadius: 999, border: `1px solid ${col.bd}`, background: col.bg } : {}) }}>{r[c.key]}</span></td>
                }
                const v = c.fmt ? c.fmt(r[c.key]) : c.num ? num(r[c.key]) : (r[c.key] ?? '—')
                return <td key={c.key} style={{ ...td, textAlign: c.num ? 'right' : 'left', fontFamily: c.mono ? 'monospace' : 'inherit', color: c.num ? C.text : C.textSub, fontWeight: c.num ? 600 : 400 }}>{v}</td>
              })}
              {actions && <td style={{ ...td, textAlign: 'center' }}>{actions(r)}</td>}
            </tr>
          ))}
          {!rows.length && <tr><td colSpan={cols.length + (actions ? 1 : 0) + (sel ? 1 : 0)} style={{ padding: 24, textAlign: 'center', color: C.textMuted }}>{emptyMsg}</td></tr>}
        </tbody>
      </table>
      {popFilter && (() => { const c = cols.find(x => x.key === popFilter.key); return (
        <ColumnFilterPopover label={c?.label} values={distinct[popFilter.key] || []} value={colq[popFilter.key]} pos={popFilter}
          onChange={f => setColq(q => { const n = { ...q }; if (f) n[popFilter.key] = f; else delete n[popFilter.key]; return n })} onClose={() => setPopFilter(null)} />
      ) })()}
    </div>
  )
}

export default function FaConsPendPage() {
  const [mode, setMode] = useState('pending')     // 'pending' | 'ops'
  const [stream, setStream] = useState(''); const [status, setStatus] = useState('')
  const [rows, setRows] = useState([]); const [summary, setSummary] = useState(null)
  const [ops, setOps] = useState([]); const [loading, setLoading] = useState(false)
  const [gq, setGq] = useState(''); const [colq, setColq] = useState({}); const [popFilter, setPopFilter] = useState(null)
  const [sort, setSort] = useState({ key: 'status', dir: 'asc' })
  const [sessions, setSessions] = useState([]); const [approveSess, setApproveSess] = useState('')
  const [manual, setManual] = useState(null)      // manual-add form
  const [deliverRow, setDeliverRow] = useState(null); const [dq, setDq] = useState(''); const [drm, setDrm] = useState('')
  const [closeRow, setCloseRow] = useState(null); const [crsn, setCrsn] = useState('')
  const [busy, setBusy] = useState(false)
  const [selIds, setSelIds] = useState(new Set())          // selected pending lines (for DO)
  const [doModal, setDoModal] = useState(false); const [doNum, setDoNum] = useState('')
  const [editRow, setEditRow] = useState(null)             // edit line at actual-article grain

  const load = async () => {
    setLoading(true)
    try {
      const { data } = await faConsAPI.pendList(stream || undefined, status || undefined)
      setRows(data.data?.items || []); setSummary(data.data?.summary || null); setSelIds(new Set())
      const { data: od } = await faConsAPI.pendOps({ ...(stream ? { stream } : {}) })
      setOps(od.data?.items || [])
    } catch (e) { toast.error(e.response?.data?.detail || 'Failed to load pending') }
    finally { setLoading(false) }
  }
  const toggleSel = (id) => setSelIds(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  const toggleAllSel = (rowsSel) => setSelIds(s => (rowsSel.length && rowsSel.every(r => s.has(r.id))) ? new Set() : new Set(rowsSel.map(r => r.id)))
  const doGenerateDo = async () => {
    setBusy(true)
    try { const { data } = await faConsAPI.pendGenerateDo({ pend_ids: [...selIds], do_number: doNum.trim() || undefined }); toast.success(data.message); setDoModal(false); setDoNum(''); setSelIds(new Set()); load() }
    catch (e) { toast.error(e.response?.data?.detail || 'DO generation failed') } finally { setBusy(false) }
  }
  const saveEdit = async () => {
    if (!editRow.reason?.trim()) { toast.error('Reason required'); return }
    setBusy(true)
    try { const { data } = await faConsAPI.pendUpdate(editRow.id, { article_number: editRow.article_number, sz: editRow.sz, clr: editRow.clr, qty: +editRow.qty, reason: editRow.reason }); toast.success(data.message); setEditRow(null); load() }
    catch (e) { toast.error(e.response?.data?.detail || 'Update failed') } finally { setBusy(false) }
  }
  const loadSessions = async () => { try { const { data } = await faConsAPI.allocSessions(); setSessions(data.data?.items || []) } catch { /* */ } }
  useEffect(() => { load() /* eslint-disable-next-line */ }, [stream, status])
  useEffect(() => { loadSessions() }, [])

  const src = mode === 'pending' ? rows : ops
  const cols = mode === 'pending' ? COLS : OCOLS
  const cellVal = (c, r) => String((c.fmt ? c.fmt(r[c.key]) : r[c.key]) ?? '')
  const distinct = useMemo(() => { const m = {}; cols.forEach(c => { const s = new Set(); src.forEach(r => s.add(cellVal(c, r))); m[c.key] = [...s].sort((a, b) => a.localeCompare(b, undefined, { numeric: true })) }); return m }, [src, cols])
  const view = useMemo(() => {
    let out = src.filter(r => cols.every(c => passColumn(colq[c.key], cellVal(c, r))))
    if (gq.trim()) out = out.filter(r => matchGlobal(gq, cols.map(c => cellVal(c, r))))
    if (sort.key) { const isN = cols.find(c => c.key === sort.key)?.num; out = [...out].sort((a, b) => { let x = a[sort.key], y = b[sort.key]; if (isN) { x = +x || 0; y = +y || 0; return sort.dir === 'asc' ? x - y : y - x } return sort.dir === 'asc' ? String(x ?? '').localeCompare(String(y ?? '')) : String(y ?? '').localeCompare(String(x ?? '')) }) }
    return out
  }, [src, cols, colq, gq, sort])

  const doApprove = async () => {
    if (!approveSess) { toast.error('Pick an allocation session'); return }
    try { const { data } = await faConsAPI.pendApprove(+approveSess); toast.success(data.message); setApproveSess(''); load() }
    catch (e) { toast.error(e.response?.data?.detail || 'Approve failed') }
  }
  const saveManual = async () => {
    setBusy(true)
    try { const { data } = await faConsAPI.pendManual(manual); toast.success(data.message); setManual(null); load() }
    catch (e) { toast.error(e.response?.data?.detail || 'Add failed') } finally { setBusy(false) }
  }
  const doDeliver = async () => {
    setBusy(true)
    try { const { data } = await faConsAPI.pendDeliver(deliverRow.id, { qty: +dq, remarks: drm || undefined }); toast.success(data.message); setDeliverRow(null); setDq(''); setDrm(''); load() }
    catch (e) { toast.error(e.response?.data?.detail || 'Deliver failed') } finally { setBusy(false) }
  }
  const doClose = async () => {
    if (!crsn.trim()) { toast.error('Reason required'); return }
    setBusy(true)
    try { const { data } = await faConsAPI.pendClose(closeRow.id, { reason: crsn }); toast.success(data.message); setCloseRow(null); setCrsn(''); load() }
    catch (e) { toast.error(e.response?.data?.detail || 'Close failed') } finally { setBusy(false) }
  }
  const doReopen = async (r) => { try { await faConsAPI.pendReopen(r.id); toast.success('Reopened'); load() } catch (e) { toast.error(e.response?.data?.detail || 'Reopen failed') } }

  const tiles = summary ? [['Lines', summary.lines, C.text], ['Open', summary.open, C.green], ['Closed', summary.closed, C.gray],
    ['Promised', num(summary.promised), C.blue], ['Delivered', num(summary.delivered), C.primary], ['Balance', num(summary.balance), C.amber]] : []
  const lbl = { fontSize: 10, fontWeight: 700, color: C.textMuted, textTransform: 'uppercase', letterSpacing: '.03em', marginBottom: 3 }
  const rowActions = (r) => r.status === 'CLOSED'
    ? <button onClick={() => doReopen(r)} title="Reopen" className="inline-flex items-center gap-1" style={{ background: 'none', border: 'none', cursor: 'pointer', color: C.amber, fontSize: 10, fontWeight: 700 }}><RotateCcw size={12} /> Reopen</button>
    : <>
      <button onClick={() => setEditRow({ ...r, qty: r.qty, reason: '' })} title="Edit actual article / qty" style={{ background: 'none', border: 'none', cursor: 'pointer', color: C.blue, marginRight: 10 }}><Pencil size={12} /></button>
      <button onClick={() => { setDeliverRow(r); setDq(''); setDrm('') }} title="Record delivery" style={{ background: 'none', border: 'none', cursor: 'pointer', color: C.green, marginRight: 10 }}><Truck size={13} /></button>
      <button onClick={() => { setCloseRow(r); setCrsn('') }} title="Adhoc close" style={{ background: 'none', border: 'none', cursor: 'pointer', color: C.red }}><XCircle size={13} /></button>
    </>

  return (
    <div className="p-4 space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <h1 className="page-title">FA &amp; CONS · Pending Allocation</h1>
          <p className="text-[11px] text-gray-500 mt-0.5 max-w-3xl">
            A self-contained pending pipeline for FA/CONS — approve an allocation into pending, record deliveries, close lines, or add a line manually.
            Every action is logged. <b>The core Pending Allocation menu is not affected.</b>
          </p>
        </div>
        <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm shrink-0">
          {[['pending', 'Pending'], ['ops', 'Operations Log']].map(([m, t]) => (
            <button key={m} onClick={() => { setMode(m); setColq({}); setPopFilter(null); setSort(m === 'pending' ? { key: 'status', dir: 'asc' } : { key: 'changed_at', dir: 'desc' }) }} className={`px-3 py-1.5 text-[11px] font-semibold ${mode === m ? 'bg-gray-800 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>{t}</button>
          ))}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-1.5 text-[10px]">
        <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm">
          {STREAMS.map(s => <button key={s.k} onClick={() => setStream(s.k)} className={`px-3 py-0.5 text-[11px] font-semibold ${stream === s.k ? 'bg-primary-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>{s.label}</button>)}
        </div>
        {mode === 'pending' && (
          <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm">
            {STATUSES.map(s => <button key={s.k} onClick={() => setStatus(s.k)} className={`px-3 py-0.5 text-[11px] font-semibold ${status === s.k ? 'bg-gray-700 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>{s.label}</button>)}
          </div>
        )}
        {mode === 'pending' && <>
          <select value={approveSess} onChange={e => setApproveSess(e.target.value)} className="input !py-0.5 !text-[10px]" style={{ maxWidth: 260 }}>
            <option value="">Approve alloc session…</option>
            {sessions.map(s => <option key={s.id} value={s.id}>#{s.id} · {s.stream}/{s.store_scope} · {num(s.n_units)}u</option>)}
          </select>
          <button onClick={doApprove} disabled={!approveSess} className="btn-secondary btn-sm flex items-center gap-1 !py-0.5"><CheckCircle2 size={12} /> Approve</button>
          <button onClick={() => setManual({ stream: 'FA', st_cd: '', ref_art: '', article_number: '', qty: '', reason: '' })} className="btn-primary btn-sm flex items-center gap-1 !py-0.5"><Plus size={12} /> Manual add</button>
          {selIds.size > 0 && <button onClick={() => { setDoModal(true); setDoNum('') }} className="btn-secondary btn-sm flex items-center gap-1 !py-0.5" style={{ borderColor: '#7c3aed', color: '#7c3aed' }}><FileText size={12} /> Generate DO ({selIds.size})</button>}
        </>}
        <div className="relative">
          <Search size={12} style={{ position: 'absolute', left: 7, top: 6, color: '#9ca3af' }} />
          <input value={gq} onChange={e => setGq(e.target.value)} placeholder="Search (a, b, c = any)…" className="input !py-0.5 !text-[10px]" style={{ paddingLeft: 24, width: 200 }} />
          {gq && <button onClick={() => setGq('')} style={{ position: 'absolute', right: 5, top: 5, background: 'none', border: 'none', cursor: 'pointer' }}><X size={11} color="#9ca3af" /></button>}
        </div>
        {Object.values(colq).some(filterActive) && <button onClick={() => setColq({})} className="btn-secondary btn-sm flex items-center gap-1 !py-0.5"><X size={12} /> Clear</button>}
        <button onClick={() => exportCsv(`facons_${mode === 'pending' ? 'pending' : 'ops'}_${stream || 'all'}`, cols, view)} disabled={!view.length} className="btn-secondary btn-sm flex items-center gap-1 !py-0.5"><Download size={12} /> Export</button>
        <button onClick={load} className="btn-secondary btn-sm flex items-center gap-1 !py-0.5"><RefreshCw size={12} className={loading ? 'animate-spin' : ''} /> Refresh</button>
        <span className="text-gray-400">{view.length}/{src.length}</span>
      </div>

      {mode === 'pending' && summary && (
        <div className="flex flex-wrap gap-2">
          {tiles.map(([k, v, col]) => <div key={k} className="inline-flex items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 py-1"><span className="text-[9px] font-semibold uppercase tracking-wide text-gray-400">{k}</span><span className="text-[13px] font-bold tabular-nums" style={{ color: col }}>{v}</span></div>)}
        </div>
      )}

      <GridTable cols={cols} rows={view} sort={sort} setSort={setSort} colq={colq} setColq={setColq} popFilter={popFilter} setPopFilter={setPopFilter} distinct={distinct}
        actions={mode === 'pending' ? rowActions : null}
        sel={mode === 'pending' ? { ids: selIds, toggle: toggleSel, toggleAll: toggleAllSel, can: (r) => r.status === 'OPEN' } : null}
        emptyMsg={loading ? 'Loading…' : mode === 'pending' ? 'No pending lines. Approve an allocation session or add one manually.' : 'No operations logged yet.'} />

      {/* Manual add */}
      {manual && (
        <div onClick={() => !busy && setManual(null)} style={{ position: 'fixed', inset: 0, zIndex: 80, background: 'rgba(0,0,0,.45)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div onClick={e => e.stopPropagation()} style={{ width: 460, background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 12, padding: 16 }}>
            <div style={{ fontSize: 14, fontWeight: 700, color: C.text, marginBottom: 12 }}>Add pending line (manual)</div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <div style={{ flex: '0 0 90px' }}><div style={lbl}>Stream</div><select value={manual.stream} onChange={e => setManual(m => ({ ...m, stream: e.target.value }))} className="input !py-1 !text-[12px]" style={{ width: '100%' }}><option>FA</option><option>CONS</option></select></div>
              <div style={{ flex: '1 1 110px' }}><div style={lbl}>Store *</div><input value={manual.st_cd} onChange={e => setManual(m => ({ ...m, st_cd: e.target.value }))} className="input !py-1 !text-[12px]" style={{ width: '100%' }} /></div>
              <div style={{ flex: '0 0 90px' }}><div style={lbl}>Qty *</div><input type="number" value={manual.qty} onChange={e => setManual(m => ({ ...m, qty: e.target.value }))} className="input !py-1 !text-[12px]" style={{ width: '100%' }} /></div>
            </div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 8 }}>
              <div style={{ flex: '1 1 140px' }}><div style={lbl}>Ref Article *</div><input value={manual.ref_art} onChange={e => setManual(m => ({ ...m, ref_art: e.target.value }))} className="input !py-1 !text-[12px]" style={{ width: '100%' }} /></div>
              <div style={{ flex: '1 1 140px' }}><div style={lbl}>Article (optional)</div><input value={manual.article_number} onChange={e => setManual(m => ({ ...m, article_number: e.target.value }))} className="input !py-1 !text-[12px]" style={{ width: '100%' }} /></div>
            </div>
            <div style={{ marginTop: 8 }}><div style={lbl}>Reason *</div><input value={manual.reason} onChange={e => setManual(m => ({ ...m, reason: e.target.value }))} placeholder="Why is this being added by hand?" className="input !py-1 !text-[12px]" style={{ width: '100%' }} /></div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 14 }}>
              <button onClick={() => setManual(null)} disabled={busy} className="btn-secondary btn-sm">Cancel</button>
              <button onClick={saveManual} disabled={busy || !manual.st_cd.trim() || !manual.ref_art.trim() || !(+manual.qty > 0) || !manual.reason.trim()} className="btn-primary btn-sm">Add line</button>
            </div>
          </div>
        </div>
      )}

      {/* Deliver */}
      {deliverRow && (
        <div onClick={() => !busy && setDeliverRow(null)} style={{ position: 'fixed', inset: 0, zIndex: 80, background: 'rgba(0,0,0,.45)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div onClick={e => e.stopPropagation()} style={{ width: 400, background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 12, padding: 16 }}>
            <div style={{ fontSize: 14, fontWeight: 700, color: C.text, marginBottom: 4 }}>Record delivery</div>
            <div style={{ fontSize: 11, color: C.textMuted, marginBottom: 12 }}><span style={{ fontFamily: 'monospace' }}>{deliverRow.st_cd}</span> / <span style={{ fontFamily: 'monospace' }}>{deliverRow.article_number || deliverRow.ref_art}</span> · balance <b>{num(deliverRow.balance)}</b> of {num(deliverRow.qty)}</div>
            <div style={lbl}>Delivered qty *</div>
            <input autoFocus type="number" min="0" value={dq} onChange={e => setDq(e.target.value)} className="input !py-1 !text-[13px]" style={{ width: '100%', marginBottom: 8 }} />
            <div style={lbl}>Remarks (DO ref etc.)</div>
            <input value={drm} onChange={e => setDrm(e.target.value)} className="input !py-1 !text-[13px]" style={{ width: '100%' }} />
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 14 }}>
              <button onClick={() => setDeliverRow(null)} disabled={busy} className="btn-secondary btn-sm">Cancel</button>
              <button onClick={doDeliver} disabled={busy || !(+dq > 0)} className="btn-primary btn-sm">Record</button>
            </div>
          </div>
        </div>
      )}

      {/* Close */}
      {closeRow && (
        <div onClick={() => !busy && setCloseRow(null)} style={{ position: 'fixed', inset: 0, zIndex: 80, background: 'rgba(0,0,0,.45)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div onClick={e => e.stopPropagation()} style={{ width: 400, background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 12, padding: 16 }}>
            <div style={{ fontSize: 14, fontWeight: 700, color: C.text, marginBottom: 10 }}>Adhoc close pending line</div>
            <div style={{ fontSize: 11, color: C.textSub, marginBottom: 10 }}>Close <span style={{ fontFamily: 'monospace' }}>{closeRow.st_cd}</span> / <span style={{ fontFamily: 'monospace' }}>{closeRow.article_number || closeRow.ref_art}</span> (balance {num(closeRow.balance)} won't be fulfilled).</div>
            <div style={lbl}>Reason *</div>
            <input autoFocus value={crsn} onChange={e => setCrsn(e.target.value)} className="input !py-1 !text-[13px]" style={{ width: '100%', borderColor: crsn.trim() ? undefined : C.red }} />
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 14 }}>
              <button onClick={() => setCloseRow(null)} disabled={busy} className="btn-secondary btn-sm">Cancel</button>
              <button onClick={doClose} disabled={busy || !crsn.trim()} className="btn-primary btn-sm" style={{ background: C.red, borderColor: C.red }}>Adhoc close</button>
            </div>
          </div>
        </div>
      )}

      {/* Generate DO (DO Entry) */}
      {doModal && (
        <div onClick={() => !busy && setDoModal(false)} style={{ position: 'fixed', inset: 0, zIndex: 80, background: 'rgba(0,0,0,.45)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div onClick={e => e.stopPropagation()} style={{ width: 420, background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 12, padding: 16 }}>
            <div style={{ fontSize: 14, fontWeight: 700, color: C.text, marginBottom: 6 }}>Generate DO</div>
            <div style={{ fontSize: 11, color: C.textMuted, marginBottom: 12 }}>Stamp a Delivery Order on <b>{selIds.size}</b> selected open line(s). Type the real DO reference, or leave blank to auto-assign <code>FACONS-DO-000n</code>.</div>
            <div style={lbl}>DO number (optional)</div>
            <input autoFocus value={doNum} onChange={e => setDoNum(e.target.value)} placeholder="e.g. 4900001234 — or leave blank for auto" className="input !py-1 !text-[13px]" style={{ width: '100%' }} />
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 14 }}>
              <button onClick={() => setDoModal(false)} disabled={busy} className="btn-secondary btn-sm">Cancel</button>
              <button onClick={doGenerateDo} disabled={busy} className="btn-primary btn-sm" style={{ background: '#7c3aed', borderColor: '#7c3aed' }}>Generate DO</button>
            </div>
          </div>
        </div>
      )}

      {/* Edit line (actual article grain) */}
      {editRow && (
        <div onClick={() => !busy && setEditRow(null)} style={{ position: 'fixed', inset: 0, zIndex: 80, background: 'rgba(0,0,0,.45)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div onClick={e => e.stopPropagation()} style={{ width: 460, background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 12, padding: 16 }}>
            <div style={{ fontSize: 14, fontWeight: 700, color: C.text, marginBottom: 4 }}>Edit pending line</div>
            <div style={{ fontSize: 11, color: C.textMuted, marginBottom: 12 }}><span style={{ fontFamily: 'monospace' }}>{editRow.st_cd}</span> / ref <span style={{ fontFamily: 'monospace' }}>{editRow.ref_art}</span> · delivered {num(editRow.delivered)} (qty can't go below this)</div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <div style={{ flex: '1 1 150px' }}><div style={lbl}>Actual article</div><input value={editRow.article_number || ''} onChange={e => setEditRow(m => ({ ...m, article_number: e.target.value }))} className="input !py-1 !text-[12px]" style={{ width: '100%' }} /></div>
              <div style={{ flex: '0 0 70px' }}><div style={lbl}>SZ</div><input value={editRow.sz || ''} onChange={e => setEditRow(m => ({ ...m, sz: e.target.value }))} className="input !py-1 !text-[12px]" style={{ width: '100%' }} /></div>
              <div style={{ flex: '0 0 70px' }}><div style={lbl}>CLR</div><input value={editRow.clr || ''} onChange={e => setEditRow(m => ({ ...m, clr: e.target.value }))} className="input !py-1 !text-[12px]" style={{ width: '100%' }} /></div>
              <div style={{ flex: '0 0 90px' }}><div style={lbl}>Qty *</div><input type="number" value={editRow.qty} onChange={e => setEditRow(m => ({ ...m, qty: e.target.value }))} className="input !py-1 !text-[12px]" style={{ width: '100%' }} /></div>
            </div>
            <div style={{ marginTop: 8 }}><div style={lbl}>Reason *</div><input value={editRow.reason} onChange={e => setEditRow(m => ({ ...m, reason: e.target.value }))} placeholder="Why is this line being changed?" className="input !py-1 !text-[12px]" style={{ width: '100%', borderColor: editRow.reason?.trim() ? undefined : C.red }} /></div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 14 }}>
              <button onClick={() => setEditRow(null)} disabled={busy} className="btn-secondary btn-sm">Cancel</button>
              <button onClick={saveEdit} disabled={busy || !(+editRow.qty > 0) || !editRow.reason?.trim()} className="btn-primary btn-sm">Save</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
